# SPDX-License-Identifier: Apache-2.0
"""The driver of Inferno's scripted suites (scripts/inferno_session.py, LP-23l) against recorded
sessions: what it asks Inferno's REST API, in order, what it sends, the client's part of each
wait it runs in the engine's container, and the results it writes for scripts/inferno_check.py.
No Inferno, no Docker and no network: Inferno's answers are replayed from a local run's
recording (tests/fixtures/inferno/), and the engine's commands are recorded, never run."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "inferno"
SMART = "smart_client_stu2_2"
BULK = "bulk_data_v200_client"
SMART_OPTION = {"client_type": "SMART,authorization_code,public"}


def _load(name: str) -> Any:
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


driver = _load("inferno_session")
check = _load("inferno_check")
inputs_script = _load("inferno_scripted_inputs")


def _recorded(suite: str) -> list[dict[str, Any]]:
    loaded = json.loads((FIXTURES / f"{suite}_session.json").read_text(encoding="utf-8"))
    assert loaded["suite"] == suite
    exchanges: list[dict[str, Any]] = loaded["exchanges"]
    return exchanges


class Replay:
    """Inferno's web app, replayed: each request gets the next recorded answer to the same
    method and path, and is kept with what it sent."""

    def __init__(self, exchanges: Sequence[Mapping[str, Any]]) -> None:
        self.waiting = [dict(e) for e in exchanges]
        self.sent: list[tuple[str, str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode()
        body = json.loads(request.content) if request.content else None
        self.sent.append((request.method, path, body))
        for exchange in self.waiting:
            if (exchange["method"], exchange["path"]) == (request.method, path):
                self.waiting.remove(exchange)
                if exchange["body"] is None:
                    return httpx.Response(exchange["status"])
                return httpx.Response(exchange["status"], json=exchange["body"])
        return httpx.Response(404, json={"errors": f"not recorded: {request.method} {path}"})


class FakeEngine(driver.Engine):  # type: ignore[name-defined]
    """The engine's container: each command and the environment it would get, recorded."""

    def __init__(self, status: int = 0) -> None:
        super().__init__()
        self.status = status
        self.ran: list[tuple[list[str], dict[str, str]]] = []

    def run(self, args: Sequence[str], env: Mapping[str, str] | None = None) -> int:
        self.ran.append((self.command(args, sorted(env or {})), dict(env or {})))
        return int(self.status)


def _session(suite: str, replay: Replay, engine: FakeEngine | None = None, **kwargs: Any) -> Any:
    http = httpx.Client(base_url=driver.INFERNO, transport=httpx.MockTransport(replay))
    return driver.Session(
        suite, http, engine=engine or FakeEngine(), sleep=lambda _s: None, **kwargs
    )


def _run(
    session: Any, tmp_path: Path, preset: Mapping[str, Any], *, options: Mapping[str, str] = {}
) -> int:
    given = {item["name"]: item["value"] for item in preset["inputs"]}
    return int(
        driver.run(
            session,
            inputs=given,
            groups=[],
            options=options,
            out=tmp_path / "results.json",
            tests=tmp_path / "tests.json",
        )
    )


def test_a_smart_launch_runs_from_the_launch_url_inferno_made_then_resumes_the_test(
    tmp_path: Path,
) -> None:
    """The recorded public-client session: the session takes the suite option, the run starts
    with the preset's inputs, the access test waits with Inferno's launch URL, the engine opens
    it (`mendari sandbox smart-launch --launch-url`), and the driver calls the test's resume URL
    on its own way in to Inferno (127.0.0.1:4580), not on the compose network's host."""
    replay = Replay(_recorded(SMART))
    engine = FakeEngine()
    session = _session(SMART, replay, engine)
    assert _run(session, tmp_path, inputs_script.smart(), options=SMART_OPTION) == 0
    method, path, created = replay.sent[1]
    assert (method, path) == ("POST", "/api/test_sessions")
    assert created["suite_options"] == [
        {"id": "client_type", "value": "SMART,authorization_code,public"}
    ]
    _, _, started = next(s for s in replay.sent if s[1] == "/api/test_runs")
    sent = {item["name"]: item["value"] for item in started["inputs"]}
    assert sent["client_id"] == "mendari-sandbox"
    assert sent["smart_launch_urls"] == "http://engine:8000/smart/launch"
    ((command, env),) = engine.ran
    assert command[:6] == ["docker", "compose", "-f", driver.COMPOSE_FILE, "exec", "-T"]
    assert command[6:11] == ["engine", "mendari", "sandbox", "smart-launch", "--engine"]
    launch = command[command.index("--launch-url") + 1]
    assert launch.startswith(
        "http://engine:8000/smart/launch?iss=http%3A%2F%2Fnginx%2Fcustom%2Fsmart_client_stu2_2%2Ffhir"
        "&launch="
    )
    assert env == {}
    resumed = [path for _, path, _ in replay.sent if "resume_pass" in path]
    assert resumed == ["/custom/smart_client_stu2_2/resume_pass?token=mendari-sandbox"]
    assert not replay.waiting  # every recorded answer was asked for, in its order
    listing = json.loads((tmp_path / "tests.json").read_text())
    assert listing["groups"] == ["3", "5"]  # the groups the option chose
    assert [t["short_id"] for t in listing["tests"]] == ["3.01", "5.03", "5.07", "5.10", "5.12"]
    assert listing["tests"][0]["requirements"] == [
        "hl7.fhir.uv.smart-app-launch_2.2.0@20",
        "hl7.fhir.uv.smart-app-launch_2.2.0@21",
    ]


def test_the_results_are_what_the_checker_reads_and_match_the_golden(tmp_path: Path) -> None:
    """Written as `inferno execute --outputter json` prints them (a JSON array, pretty-printed),
    so scripts/inferno_check.py, the verdict, reads them, and the recorded run gives the
    committed expected results."""
    session = _session(SMART, Replay(_recorded(SMART)))
    assert _run(session, tmp_path, inputs_script.smart(), options=SMART_OPTION) == 0
    raw = (tmp_path / "results.json").read_text()
    assert raw.splitlines()[0] == "["
    found = check.run_results(raw)
    assert found[f"{SMART}-smart_client_access-smart_client_access_alp_interaction"] == "pass"
    golden = ROOT / "sandbox" / "inferno" / f"{SMART}_expected.json"
    args = [str(tmp_path / "results.json"), "--expected", str(golden)]
    assert check.main([*args, "--tests", str(tmp_path / "tests.json")]) == 0


def test_a_bulk_export_signs_in_with_the_runs_key_given_by_environment_only(
    tmp_path: Path,
) -> None:
    """The client's key is made for the run: its public half registers the client
    (`smart_jwk_set`, an EC P-384 key with no private member), and its private half reaches the
    export in the engine through the environment, named on the command line (`-e NAME`) and
    never written there."""
    replay = Replay(_recorded(BULK))
    engine = FakeEngine()
    session = _session(BULK, replay, engine)
    assert _run(session, tmp_path, inputs_script.bulk()) == 0
    _, _, started = next(s for s in replay.sent if s[1] == "/api/test_runs")
    sent = {item["name"]: item["value"] for item in started["inputs"]}
    assert (sent["client_id"], sent["export_type"]) == ("mendari-bulk-sandbox", "Patient")
    (key,) = json.loads(sent["smart_jwk_set"])["keys"]
    assert (key["kty"], key["crv"], key["alg"], key["kid"]) == (
        "EC",
        "P-384",
        "ES384",
        "mendari-bulk-run",
    )
    assert "d" not in key
    ((command, env),) = engine.ran
    assert command[6:9] == ["-e", driver.BULK_ENV, "engine"]
    assert command[9:12] == ["mendari", "bulk", "export"]
    assert command[command.index("--fhir") + 1] == "http://nginx/custom/bulk_data_v200_client/fhir"
    assert command[command.index("--client-id") + 1] == "mendari-bulk-sandbox"
    assert command[command.index("--key-id") + 1] == key["kid"]
    assert "--private-key" not in command
    assert env[driver.BULK_ENV].startswith("-----BEGIN EC PRIVATE KEY-----")
    assert not any("BEGIN" in part or "\n" in part for part in command)  # no key material
    assert [p for _, p, _ in replay.sent if "resume_pass" in p] == [
        "/custom/bulk_data_v200_client/resume_pass?id=mendari-bulk-sandbox"
    ]
    golden = ROOT / "sandbox" / "inferno" / f"{BULK}_expected.json"
    assert check.main([str(tmp_path / "results.json"), "--expected", str(golden)]) == 0


def test_repeated_bulk_runs_have_separate_export_directories() -> None:
    engine = FakeEngine()
    session = _session(BULK, Replay(_recorded(BULK)), engine)
    for _ in range(2):
        driver.bulk_export(session, {}, {"client_id": "mendari-bulk-sandbox"})
    destinations = {cmd[cmd.index("--out") + 1] for cmd, _env in engine.ran}
    assert len(destinations) == 2
    assert all(path.startswith("/tmp/inferno-bulk-") for path in destinations)


def test_the_engine_command_names_its_variables_and_never_their_values() -> None:
    engine = driver.Engine(compose_file="x.yml")
    assert engine.command(["mendari", "--version"], ["A_KEY"]) == [
        "docker", "compose", "-f", "x.yml", "exec", "-T", "-e", "A_KEY", "engine",
        "mendari", "--version",
    ]  # fmt: skip


def test_a_failed_client_step_is_reported_and_the_run_still_finishes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The engine's export failed: said once, the test resumed anyway so the run ends and its
    results are written for the check, and the driver exits 1."""
    session = _session(BULK, Replay(_recorded(BULK)), FakeEngine(status=1))
    assert _run(session, tmp_path, inputs_script.bulk()) == 1
    assert "the Bulk export in the engine failed" in capsys.readouterr().err
    assert (tmp_path / "results.json").read_text().startswith("[")


def _with(exchanges: list[dict[str, Any]], change: Callable[[dict[str, Any]], None]) -> Any:
    changed = copy.deepcopy(exchanges)
    for exchange in changed:
        change(exchange)
    return changed


def test_a_run_that_outlasts_its_timeout_is_cancelled_and_fails(tmp_path: Path) -> None:
    """A run that never ends (here every poll answers `running`) is cancelled after
    `--timeout`; what the session holds is written, and the driver fails."""

    def running(exchange: dict[str, Any]) -> None:
        if exchange["path"].startswith("/api/test_runs/") and exchange["method"] == "GET":
            exchange["body"]["status"] = "running"

    polls = [e for e in _recorded(SMART) if e["path"].startswith("/api/test_runs/")]
    replay = Replay(_with(_recorded(SMART), running) + _with(polls, running) * 5)
    clock = iter(range(0, 10_000, 10))
    session = _session(SMART, replay, timeout=25.0, clock=lambda: float(next(clock)))
    with pytest.raises(driver.SessionError, match="did not finish within 25 s"):
        _run(session, tmp_path, inputs_script.smart(), options=SMART_OPTION)
    assert any(method == "DELETE" for method, _, _ in replay.sent)  # the run is cancelled
    assert (tmp_path / "results.json").exists()


def test_inferno_refusing_an_input_stops_the_run_with_its_own_words(tmp_path: Path) -> None:
    def refused(exchange: dict[str, Any]) -> None:
        if exchange["path"] == "/api/test_runs" and exchange["method"] == "POST":
            exchange["status"] = 422
            exchange["body"] = {"errors": "Missing the following required inputs: group_id"}

    session = _session(BULK, Replay(_with(_recorded(BULK), refused)))
    with pytest.raises(driver.SessionError, match=r"HTTP 422 \(Missing .* group_id\)"):
        _run(session, tmp_path, inputs_script.bulk())


def test_a_session_that_does_not_take_its_options_is_refused(tmp_path: Path) -> None:
    session = _session(SMART, Replay(_recorded(SMART)))
    with pytest.raises(driver.SessionError, match="did not take the options"):
        _run(session, tmp_path, inputs_script.smart(), options={"client_type": "other"})


def test_inferno_that_never_answers_fails_after_the_wait() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    clock = iter(range(0, 10_000, 100))
    http = httpx.Client(base_url=driver.INFERNO, transport=httpx.MockTransport(down))
    session = driver.Session(SMART, http, sleep=lambda _s: None, clock=lambda: float(next(clock)))
    with pytest.raises(driver.SessionError, match="did not answer within 300 s"):
        session.wait_until_up()


# -- the parts of a session, on a session tree of CRD's shape (nested groups) ------------------

TREE = {
    "id": "crd_server_v221",
    "test_groups": [
        {"id": "g1", "short_id": "1", "tests": [{"id": "t1", "short_id": "1.01", "title": "a"}]},
        {
            "id": "g3",
            "short_id": "3",
            "test_groups": [
                {
                    "id": "g34",
                    "short_id": "3.4",
                    "inputs": [
                        {"name": "base_url"},
                        {"name": "manual_continuation", "default": "no"},
                        {"name": "order_sign_service_ids"},
                        {"name": "unknown_and_unheld"},
                    ],
                    "test_groups": [
                        {
                            "id": "g341",
                            "short_id": "3.4.1",
                            "tests": [
                                {
                                    "id": "t3411",
                                    "short_id": "3.4.1.01",
                                    "title": "Invoke",
                                    "verifies_requirements": [],
                                }
                            ],
                        }
                    ],
                    "tests": [
                        {
                            "id": "t34",
                            "short_id": "3.4.02",
                            "title": "b",
                            "verifies_requirements": ["hl7.fhir.us.davinci-crd_2.2.1@resp-1"],
                        }
                    ],
                }
            ],
        },
    ],
}


def _tree_session(held: list[dict[str, str]] | None = None) -> Any:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=held or [])

    http = httpx.Client(base_url=driver.INFERNO, transport=httpx.MockTransport(answer))
    session = driver.Session("crd_server_v221", http, sleep=lambda _s: None)
    session.id, session.tree = "s1", copy.deepcopy(TREE)
    return session


def test_a_nested_group_is_found_by_its_short_id_and_its_tests_listed_in_order() -> None:
    session = _tree_session()
    (group,) = session.runnables(["3.4"])
    assert group["id"] == "g34"
    assert driver.tests_of([group]) == [
        {"id": "t3411", "short_id": "3.4.1.01", "title": "Invoke", "requirements": []},
        {
            "id": "t34",
            "short_id": "3.4.02",
            "title": "b",
            "requirements": ["hl7.fhir.us.davinci-crd_2.2.1@resp-1"],
        },
    ]
    assert session.runnables([]) == [session.tree]  # no group: the suite
    with pytest.raises(driver.SessionError, match=r"no group 3\.7"):
        session.runnables(["3.7"])


def test_each_input_is_the_presets_else_the_sessions_else_its_default() -> None:
    """As Inferno's own command line fills a run's inputs: what the preset gives, else what the
    session holds (an earlier test's output), else the input's default; an input with none is
    not sent."""
    session = _tree_session([{"name": "order_sign_service_ids", "value": "held"}])
    group = session.runnables(["3.4"])[0]
    assert session.inputs_for(group, {"base_url": "http://engine-signin:8000"}) == [
        {"name": "base_url", "value": "http://engine-signin:8000"},
        {"name": "manual_continuation", "value": "no"},
        {"name": "order_sign_service_ids", "value": "held"},
    ]


def test_a_crd_hook_wait_gets_no_client_step() -> None:
    """Inferno's own job calls the hooks and resumes the test: nothing runs in the engine."""
    engine = FakeEngine()
    session = _tree_session()
    session.engine = engine
    session.client_step(
        {"id": "r1", "test_id": "crd_server_v221-g34-crd_v221_server_invoke_hook_test"}, {}
    )
    assert engine.ran == [] and session.problems == []


@pytest.mark.parametrize(
    "url",
    ["http://elsewhere/custom/x/resume_pass?id=1", "https://nginx/custom/x", "http://nginx.evil/x"],
)
def test_a_resume_url_off_infernos_host_is_refused(url: str) -> None:
    with pytest.raises(driver.SessionError, match="off Inferno's host"):
        driver._on_this_machine(url, driver.INFERNO)


def test_a_resume_url_is_called_on_the_drivers_own_way_in() -> None:
    assert (
        driver._on_this_machine("http://nginx/custom/s/resume_pass?id=c", "http://127.0.0.1:4580")
        == "http://127.0.0.1:4580/custom/s/resume_pass?id=c"
    )
