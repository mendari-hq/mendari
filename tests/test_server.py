# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import gc
import itertools
import json
import re
import statistics
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from mendari import load_agent
from mendari.core import db as db_module
from mendari.core import patterns
from mendari.core.audit import AuditStore
from mendari.core.decision import Decision
from mendari.core.registry import SearchPath, discover
from mendari.evals import evaluate, resolve_dataset
from mendari.providers import slots_without_model
from mendari.server.app import (
    DASHBOARD_DIR,
    DEMO_AGENT,
    LabelRequest,
    create_app,
)
from mendari.standards.cds_hooks import service_id
from tests.conftest import FIXTURE_AGENTS, ROOT, TRIAGE_DIR
from tests.support.platform import (
    BUNDLE,
    COMMUNICATION,
    PATIENT,
    TRIAGE,
    TRIAGE_API,
    attested,
    bundled_agents,
    local_site,
    names_its_benchmark,
)
from tests.test_auth import FakeIdp, sign_in, signing_in_with

AGENT = TRIAGE_API
INBOX = TRIAGE  # the agent the tests decide with: the triage fixture
LOCAL = "local-site-example"  # its local site's dataset (`local_site`)
# The triage fixture's slots on its default providers: the keyword rules in both, where it
# offers a model too (a canned or live agent, an LLM).
OFFLINE = {"router": "rules", "screen": "rules"}


@pytest.fixture
def client(db_target: str, tmp_path: Path) -> TestClient:
    datasets = local_site(tmp_path / "datasets")
    return TestClient(create_app(db_path=db_target, datasets_dir=datasets))


@pytest.fixture
def attested_client(db_target: str, tmp_path: Path, agent_copy: Callable[..., Path]) -> TestClient:
    """A server of the triage fixture as a maintained agent commits itself, with its benchmark
    and leaderboard (`attested`; the fixture itself commits neither), and the local site."""
    root = agent_copy(manifest=names_its_benchmark)
    attested(root)
    datasets = local_site(tmp_path / "datasets")
    return TestClient(create_app(db_path=db_target, datasets_dir=datasets, agents_paths=[root]))


def test_agents_and_card(client: TestClient) -> None:
    agents = client.get("/api/agents").json()
    assert [p["id"] for p in agents] == [agent.id for agent in bundled_agents()]
    detail = client.get(AGENT).json()
    triage = load_agent(TRIAGE_DIR).manifest
    assert {slot: p["variant"] for slot, p in detail["active_providers"].items()} == {
        slot: spec.use for slot, spec in triage.providers.items()
    }
    assert detail["without_model"] == ["router", "screen"]  # no model set up here
    assert LOCAL in detail["datasets"]
    assert detail["benchmark"] is None  # the fixture commits none (`attested_client` does)
    assert client.get("/api/agents/mendari/missing").status_code == 404


def test_decide_review_and_audit_flow(client: TestClient) -> None:
    decision = client.post(
        f"{AGENT}/decide", json={"message": {"body": "Crushing chest pain"}}
    ).json()
    assert decision["outcome"] == "HUMAN_REVIEW" and decision["priority"] == "stat"

    queue = client.get("/api/review").json()
    assert [item["id"] for item in queue] == [decision["id"]]

    resolved = client.post(
        f"/api/review/{decision['id']}", json={"final_label": "nurse", "reviewer": "rn-1"}
    )
    assert resolved.status_code == 200 and resolved.json()["human_override"] is False
    again = client.post(
        f"/api/review/{decision['id']}", json={"final_label": "nurse", "reviewer": "rn-1"}
    )
    assert again.status_code == 409

    record = client.get(f"/api/audit/{decision['id']}").json()
    assert [e["event"] for e in record["events"]] == ["decision.created", "review.resolved"]
    assert client.get("/api/audit/verify").json()["ok"] is True
    assert client.get("/api/stats").json()["reviews_resolved"] == 1


def test_datasets_are_listed_with_their_size(client: TestClient, tmp_path: Path) -> None:
    datasets = {d["name"]: d["n"] for d in client.get(f"{AGENT}/datasets").json()}
    detail = client.get(AGENT).json()
    assert list(datasets) == detail["datasets"]  # the same names, in the same order
    local = tmp_path / "datasets" / f"{LOCAL}.jsonl"
    assert datasets[LOCAL] == len(local.read_text().splitlines())
    assert all(n > 0 for n in datasets.values())
    assert client.get("/api/agents/mendari/missing/datasets").status_code == 404


def test_invalid_input_is_a_recorded_block_not_an_http_error(client: TestClient) -> None:
    response = client.post(f"{AGENT}/decide", json={"message": {"body": ""}})
    assert response.status_code == 200 and response.json()["outcome"] == "BLOCK"


def test_fhir_endpoint_returns_task(client: TestClient) -> None:
    body = client.post(f"{AGENT}/fhir/decide", json=BUNDLE).json()
    assert body["task"]["resourceType"] == "Task"
    missing = client.post(f"{AGENT}/fhir/decide", json={"resourceType": "Bundle", "entry": []})
    assert missing.status_code == 422


def test_cds_hooks_endpoints(client: TestClient) -> None:
    services = {s["id"]: s for s in client.get("/cds-services").json()["services"]}
    # Every bundled agent that declares a CDS hook, and no other (read from their manifests).
    assert set(services) == {
        service_id(agent) for agent in bundled_agents() if agent.manifest.cds_hooks is not None
    }
    triage = services[service_id(load_agent(TRIAGE_DIR))]
    response = client.post(
        f"/cds-services/{triage['id']}",
        json={
            "hook": triage["hook"],
            "hookInstance": "d1577c69-dfbe-44ad-ba6d-3e05e953b2ea",
            "context": {},
            "prefetch": {"communication": COMMUNICATION, "patient": PATIENT},
        },
    )
    assert response.status_code == 200
    assert response.json()["cards"][0]["indicator"] == "critical"  # a possible emergency


def test_local_validation_via_api(attested_client: TestClient) -> None:
    client = attested_client
    # Each agent is offered only the local datasets that label its questions.
    assert "clinical-local-site-example" not in client.get(AGENT).json()["datasets"]
    report = client.post(f"{AGENT}/evals", json={"dataset": LOCAL}).json()
    assert report["comparison"]["status"] == "degraded"
    assert client.get("/api/evals").json()[0]["run"]["id"] == report["run"]["id"]
    # Evaluation traffic does not enter the operational audit log.
    assert client.get("/api/stats").json()["decisions"] == 0
    assert client.post(f"{AGENT}/evals", json={"dataset": "../../etc/passwd"}).status_code == 404
    wrong_agent = client.post(f"{AGENT}/evals", json={"dataset": "clinical-local-site-example"})
    assert wrong_agent.status_code == 404


def test_reports_and_leaderboards_are_served_with_their_figures(
    attested_client: TestClient, db_target: str
) -> None:
    """Figures (mendari.evals.figures) are worked out on every read and never stored, so a
    served report still hashes to its result_sha256."""
    from mendari import AuditStore
    from mendari.evals import figures
    from mendari.evals.result import result_of, result_sha256

    client = attested_client
    detail = client.get(AGENT).json()
    published = detail["benchmark"]
    assert published["dataset"]["name"] == "test"  # its benchmark set
    measured = published["questions"]["destination"]
    assert published["figures"]["accuracy"] == figures.counted(measured["accuracy"], measured["n"])
    board = detail["leaderboard"]
    assert board["ranking"]["method"] == "mendari.rank/1" and board["groups"]
    assert all("figures" in row and "standing_text" in row for row in board["rows"])
    report = client.post(f"{AGENT}/evals", json={"dataset": LOCAL}).json()
    assert report["figures"]["accuracy"] == figures.counted(
        report["questions"]["destination"]["accuracy"], report["questions"]["destination"]["n"]
    )
    assert report["figures"]["comparison"]["findings"]  # the local site degrades
    assert result_sha256(result_of(report)) == report["result_sha256"]
    (listed,) = client.get("/api/evals").json()
    assert listed["figures"] == report["figures"]
    (stored,) = AuditStore(db_target).reports()
    assert "figures" not in stored
    periods = client.get("/api/stats").json()["periods"]
    assert set(periods) == {"7", "30", "all"} and periods["7"]["now"]["decisions"] == 0


def test_a_stored_report_figures_cannot_be_read_from_is_served_with_none(
    client: TestClient, db_target: str
) -> None:
    from mendari import AuditStore

    agent = load_agent(TRIAGE_DIR)
    odd = evaluate(Decision(agent), resolve_dataset(agent, "critical"))
    critical = odd["decisions"]["critical"][0]
    critical["detected"] = 99  # more than its positives
    assert critical["n"] < 99
    AuditStore(db_target).save_report(odd)
    (listed,) = client.get("/api/evals").json()
    assert listed["figures"] is None  # n/a on the page, never a failed history tab


# Two behaviour tests, the second written as the first's input with one change (a YAML anchor
# and a merge key), as an author may write many cases around one.
ANCHORED = """\
- name: a refill goes to the pharmacy
  input:
    message: &refill {subject: Refill, body: "Please renew my prescription for metformin."}
  expect: {outcome: HUMAN_REVIEW, recommendation: pharmacy}
- name: the same refill by text message goes to the pharmacy too
  input:
    message:
      <<: *refill
      channel: sms
  expect: {outcome: HUMAN_REVIEW, recommendation: pharmacy}
"""


def test_examples_are_the_agents_behaviour_tests(
    client: TestClient, tmp_path: Path, agent_copy: Callable[..., Path]
) -> None:
    examples = client.get(f"{AGENT}/examples").json()
    cases = yaml.safe_load((TRIAGE_DIR / "tests" / "cases.yaml").read_text())
    assert [e["name"] for e in examples] == [case["name"] for case in cases]
    emergency = next(e for e in examples if e["name"].startswith("chest pain is a possible"))
    assert emergency["expect"]["recommendation"] == "nurse"
    decision = client.post(f"{AGENT}/decide", json=emergency["input"]).json()
    assert (decision["recommendation"], decision["priority"]) == ("nurse", "stat")
    assert decision["reasons"][0].startswith("rule possible-emergency:")
    assert client.get("/api/agents/mendari/missing/examples").status_code == 404
    # YAML anchors are resolved: every example is a complete input that decides as tested.
    copy = agent_copy()
    (copy / "tests" / "cases.yaml").write_text(ANCHORED)
    served = TestClient(create_app(db_path=tmp_path / "anchored.db", agents_paths=[copy]))
    first, second = served.get(f"{AGENT}/examples").json()
    assert second["input"] == {"message": {**first["input"]["message"], "channel": "sms"}}
    decided = served.post(f"{AGENT}/decide", json=second["input"]).json()
    assert decided["recommendation"] == second["expect"]["recommendation"] == "pharmacy"


def test_dashboard_is_served(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200 and "Mendari" in page.text
    # Asset URLs carry a content hash so a deploy never serves stale JS/CSS: the import map
    # stamps every module main.js reaches (every module but the /docs landing's and the
    # invitation page's), and the entry script and stylesheet are stamped in place.
    assert re.search(r'<script type="module" src="/static/main\.js\?v=[0-9a-f]{12}">', page.text)
    assert re.search(r'/static/style\.css\?v=[0-9a-f]{12}"', page.text)
    found = re.search(r'<script type="importmap">(.*?)</script>', page.text)
    assert found
    stamped = json.loads(found[1])["imports"]
    modules = sorted(
        p.name
        for p in DASHBOARD_DIR.glob("*.js")
        if p.name not in ("docs.js", "invite.js", "status.js")
    )
    assert sorted(stamped) == [f"/static/{name}" for name in modules]
    for url in stamped.values():
        assert re.fullmatch(r"/static/[a-z]+\.js\?v=[0-9a-f]{12}", url)
        assert f'<link rel="modulepreload" href="{url}">' in page.text
        assert "javascript" in client.get(url).headers["content-type"]


def test_dashboard_modules_import_only_files_that_exist() -> None:
    for module in DASHBOARD_DIR.glob("*.js"):
        source = module.read_text()
        imports = re.findall(r'^import (?:\{[^}]*\} from )?"\./([^"]+)";$', source, re.M)
        assert all((DASHBOARD_DIR / name).is_file() for name in imports), module.name


def test_the_favicon_is_served(client: TestClient) -> None:
    icon = client.get("/favicon.ico")
    assert icon.status_code == 200 and icon.headers["content-type"] == "image/x-icon"
    assert icon.content[:4] == b"\x00\x00\x01\x00"  # an ICO header
    assert client.get("/static/favicon.svg").headers["content-type"] == "image/svg+xml"
    page = client.get("/").text
    assert 'rel="icon" href="/favicon.ico"' in page and 'href="/static/favicon.svg"' in page


def test_health_reports_deployment_state(client: TestClient) -> None:
    health = client.get("/api/health").json()
    assert health["storage"] in {"sqlite", "postgres"}
    assert health["timezone"] == "America/New_York"
    assert health["providers"][TRIAGE] == OFFLINE
    # No model set up: every agent runs its offline readers where it offers a model.
    assert health["without_model"][TRIAGE] == ["router", "screen"]
    assert health["without_model"] == {
        agent.id: slots for agent in bundled_agents() if (slots := slots_without_model(agent, {}))
    }
    assert set(health["llm"]) >= {"endpoint", "credentials", "default_model", "calls_today"}


def llm_set_up(monkeypatch: pytest.MonkeyPatch, *, key: str | None = "test-key") -> None:
    """An LLM endpoint of the deployment's (a remote one, so it needs `LLM_API_KEY`), never
    called: a test fakes what a call does."""
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.test/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    if key is None:
        monkeypatch.delenv("LLM_API_KEY", raising=False)
    else:
        monkeypatch.setenv("LLM_API_KEY", key)


def test_remote_evaluations_are_capped_and_budgeted(
    db_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mendari.errors import ProviderError
    from mendari.providers import llm

    def unreachable(*_: object, **__: object) -> None:
        raise ProviderError("timeout")

    llm_set_up(monkeypatch)
    monkeypatch.setattr(llm, "post_json", unreachable)
    app = create_app(db_path=db_target, providers={"router": "llm"}, budget_limits={"llm": 5})
    report = TestClient(app).post(f"{AGENT}/evals", json={"dataset": "test"}).json()
    assert report["dataset"]["n"] == 200 and report["dataset"]["limit"] == 200
    assert report["providers"]["router"]["model"] == "test-model"
    # Five calls fit the budget (and time out); the rest are refused by the budget.
    assert report["errors"] == {"timeout": 5, "budget_exhausted": 195}
    assert report["decisions"]["automation"]["n"] == 0  # nothing automated without the model


def test_calls_that_cannot_authenticate_use_no_budget(
    db_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm_set_up(monkeypatch, key=None)
    client = TestClient(
        create_app(db_path=db_target, providers={"router": "llm"}, budget_limits={"llm": 5})
    )
    report = client.post(f"{AGENT}/evals", json={"dataset": "critical"}).json()
    critical = (TRIAGE_DIR / "data" / "critical.jsonl").read_text().splitlines()
    assert report["errors"] == {"credential_unavailable": len(critical)}
    assert client.get("/api/health").json()["llm"]["calls_today"] == 0


def test_the_api_reference_describes_every_operation(client: TestClient) -> None:
    """/docs is the product's face to developers: every operation is grouped, named and
    explained, and "Try it out" starts from a synthetic input that really decides."""
    schema = client.get("/openapi.json").json()
    tags = {tag["name"] for tag in schema["tags"]}
    operations = [
        (method.upper(), path, operation)
        for path, methods in schema["paths"].items()
        for method, operation in methods.items()
    ]
    for method, path, operation in operations:
        assert len(operation.get("tags", [])) == 1 and operation["tags"][0] in tags, (method, path)
        assert operation.get("summary") and operation.get("description"), (method, path)
    assert "securitySchemes" not in schema.get("components", {})  # sign-in is off here
    assert "Sign-in is off here" in schema["info"]["description"]
    decide = schema["paths"]["/api/agents/{namespace}/{name}/decide"]["post"]
    examples = decide["requestBody"]["content"]["application/json"]["examples"]
    # No demo agent among the tests' fixtures: the packs come by id (the demo agent first where a
    # release serves it: test_a_release_offers_the_demo_agent_first), one example each.
    exampled = [example["summary"].split(":")[0] for example in examples.values()]
    assert exampled == [agent.id for agent in bundled_agents()]
    for agent_id, example in zip(exampled, examples.values(), strict=True):
        decided = client.post(f"/api/agents/{agent_id}/decide", json=example["value"]).json()
        assert decided["outcome"] != "BLOCK", (agent_id, decided["reasons"])

    def offered(path: str) -> list[str]:
        operation = schema["paths"][path]["post"]
        name = next(p for p in operation["parameters"] if p["name"] == "name")
        return list(name["examples"])

    # The FHIR route offers the agents with a FHIR mapping, and only them.
    mapped = [agent.id.split("/")[1] for agent in bundled_agents() if agent.manifest.inputs.fhir]
    assert offered("/api/agents/{namespace}/{name}/fhir/decide") == mapped
    assert "triage" in mapped and "clinical-review" not in mapped
    smart = schema["paths"]["/api/smart/decide"]["post"]
    assert "Needs an EHR launch" in smart["description"]  # not a session or a key


def test_a_release_offers_the_demo_agent_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a release's server, which serves only the bundled demo agent, every example of the
    reference names it: "Try it out" decides with its own examples, the FHIR example is its
    FHIR mapping's, and the label examples are its own."""
    monkeypatch.delenv("MENDARI_AGENT_URL", raising=False)  # its offline fixture
    client = TestClient(create_app(db_path=tmp_path / "a.db", agents_paths=[ROOT / "agents"]))
    schema = client.get("/openapi.json").json()
    decide = schema["paths"]["/api/agents/{namespace}/{name}/decide"]["post"]
    examples = decide["requestBody"]["content"]["application/json"]["examples"]
    assert list(examples) == ["portal-message-agent"]
    for example in examples.values():
        decided = client.post(f"/api/agents/{DEMO_AGENT}/decide", json=example["value"]).json()
        assert decided["outcome"] != "BLOCK", decided["reasons"]
    fhir = schema["paths"]["/api/agents/{namespace}/{name}/fhir/decide"]["post"]
    name = next(p for p in fhir["parameters"] if p["name"] == "name")
    assert list(name["examples"]) == ["portal-message-agent"]
    bundle = fhir["requestBody"]["content"]["application/json"]["examples"]["message"]["value"]
    decided = client.post(f"/api/agents/{DEMO_AGENT}/fhir/decide", json=bundle).json()
    assert decided["task"]["resourceType"] == "Task"
    assert decided["decision"]["input_issues"] == []  # the example conforms to US Core
    assert decided["decision"]["recommendation"] == "refill"
    # A grader's grades (`LabelRequest`), as read: the demo agent's rubric, each criterion with
    # one of its levels.
    (agent,) = discover([ROOT / "agents"]).agents
    (labels,) = LabelRequest.model_json_schema()["properties"]["labels"]["examples"]
    (rubric,) = agent.manifest.rubrics.values()
    levels = {name: [level for level, _ in c.scale()] for name, c in rubric.criteria.items()}
    assert labels.keys() == levels.keys(), labels
    assert all(level in levels[name] for name, level in labels.items()), labels


# -- health does no agent work in the request (LP-16d) ----------------------------------------
# Every served agent is built at start-up and health reads what start-up found: it builds no
# Decision, records no deployment and waits on no lock, whatever an agent holds or a decision is
# doing (a signed-in caller's session may be refreshed, as on any route).
# An agent's deployment is recorded on its first use instead (tests/test_governance.py).
HEALTH_BOUND_S = 0.1  # health alone takes milliseconds
BUSY_HEALTH_BOUND_S = 0.5  # beside a decision spending its whole CPU budget in another thread
# Ten thousand characters (the most a bounded agent reads) on which each of `SLOW` takes about
# half a second: the eight take four times the budget together, and stay within every cap beside
# the fixture's own patterns (2,840 of 3,000).
EMOJI = "\U0001f600" * 10_000
SLOW = [rf"\W{{1,299}}{i}" for i in range(8)]


def outside_copy(
    agent_copy: Callable[..., Path],
    agent_id: str,
    billing: list[dict[str, Any]],
    *,
    any_length: bool = False,
) -> Path:
    """A copy of the triage fixture under `agent_id`, its router's billing patterns `billing`;
    `any_length` lifts its schema's limit on a message's length."""

    def own_id(manifest: dict[str, Any]) -> None:
        manifest["id"] = agent_id

    root = agent_copy(manifest=own_id)
    path = root / "logic" / "router_rules.yaml"
    rules = yaml.safe_load(path.read_text())
    rules["questions"]["destination"]["labels"]["billing"] = billing
    path.write_text(yaml.safe_dump(rules, sort_keys=False))
    if any_length:
        path = root / "schemas" / "input.schema.json"
        schema = json.loads(path.read_text())
        schema["properties"]["message"]["properties"]["body"].pop("maxLength")
        path.write_text(json.dumps(schema))
    return root


def decisions_built(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The agent id of every Decision built from here on (one that fails to build included)."""
    built: list[str] = []
    original = Decision.__init__

    def init(self: Decision, agent: Any, **kwargs: Any) -> None:
        built.append(agent.id)
        original(self, agent, **kwargs)

    monkeypatch.setattr(Decision, "__init__", init)
    return built


def exclusive_sessions(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """One entry for every exclusive (writing) database session opened from here on."""
    opened: list[bool] = []
    for backend in (db_module.SqliteBackend, db_module.PostgresBackend):

        def spy(self: Any, *, exclusive: bool, _original: Any = backend.session) -> Any:
            if exclusive:
                opened.append(exclusive)
            return _original(self, exclusive=exclusive)

        monkeypatch.setattr(backend, "session", spy)
    return opened


def searches_timed(monkeypatch: pytest.MonkeyPatch) -> list[tuple[float, float]]:
    """When each step of a bounded agent's pattern searches started and ended from here on, by
    `time.perf_counter`: RE2's own work, up to each match a search yields and up to its end (or
    to `OutOfTime`)."""
    steps: list[tuple[float, float]] = []
    original = patterns._Re2Pattern.finditer

    def finditer(self: patterns._Re2Pattern, text: str) -> Iterator[patterns.Match]:
        found = original(self, text)
        while True:
            started = time.perf_counter()
            try:
                match = next(found, None)
            finally:
                steps.append((started, time.perf_counter()))
            if match is None:
                return
            yield match

    monkeypatch.setattr(patterns._Re2Pattern, "finditer", finditer)
    return steps


def health_span(client: TestClient) -> tuple[float, float]:
    """When one health call started and ended, by `time.perf_counter`."""
    started = time.perf_counter()
    health = client.get("/api/health")
    ended = time.perf_counter()
    assert health.status_code == 200 and "providers" in health.json(), health.text
    return started, ended


def timed_health(client: TestClient) -> float:
    started, ended = health_span(client)
    return ended - started


@contextmanager
def interpreter_ticks() -> Iterator[list[float]]:
    """The process's CPU time (`time.process_time`) each time a thread that asks for the
    interpreter lock every few milliseconds got it, while the block runs: between two ticks,
    the process computed at least as long as anything computing held the lock (a pattern on
    `re` computes all the while; a runner pausing the process, or a call waiting on the disk,
    does not). A full collection holds the lock too, for as long as walking the session's heap
    takes, so the collector runs before and is off in the block (as in conftest's `cpu_time`).
    The first tick is taken before the block starts and the last after it ends, so a hold at
    either end is measured too."""
    ticks: list[float] = []
    ticking = threading.Event()
    stop = threading.Event()

    def tick() -> None:
        ticks.append(time.process_time())
        ticking.set()
        while not stop.wait(0.005):
            ticks.append(time.process_time())
        ticks.append(time.process_time())

    gc.collect()
    collecting = gc.isenabled()
    gc.disable()
    ticker = threading.Thread(target=tick)
    ticker.start()
    ticking.wait()
    try:
        yield ticks
    finally:
        stop.set()
        ticker.join()
        if collecting:
            gc.enable()


def test_health_builds_no_agent_in_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idp = FakeIdp()
    with signing_in_with(idp, tmp_path, monkeypatch) as client:
        sign_in(client, idp, roles=["viewer"])
        built = decisions_built(monkeypatch)
        for _ in range(3):
            health = client.get("/api/health").json()
            assert health["providers"][INBOX] == OFFLINE
            assert health["invalid"] == {}
        assert built == []


def test_health_records_no_deployment(db_target: str, monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(create_app(db_path=db_target))
    writes = exclusive_sessions(monkeypatch)
    for _ in range(3):
        timed_health(client)
    assert writes == []
    assert client.get("/api/deployments").json() == []  # nor did start-up record anything
    message = {"message": {"body": "Can I move my appointment to Friday?"}}
    for _ in range(2):
        assert client.post(f"{AGENT}/decide", json=message).status_code == 200
    with AuditStore(db_target) as store:
        rows = store.deployments(None)
        with store._db.session(exclusive=False) as session:
            chained = session.all(
                "SELECT event FROM audit_events WHERE decision_id LIKE ?", ("deployment:%",)
            )
    assert [row["agent_id"] for row in rows] == [INBOX]  # the agent used, recorded once
    assert [row["event"] for row in chained] == ["deployment.recorded"]


def test_health_answers_within_its_bound_with_a_pathological_agent_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_copy: Callable[..., Path]
) -> None:
    """An owned agent runs its patterns on `re`, so a decision on this one would hold the
    interpreter for as long as it backtracks (LP-16 conflict 15); loaded, it costs health
    nothing, from the first call on."""
    nested = [{"name": "nested", "pattern": "(a+)+$", "weight": 8}]
    root = outside_copy(agent_copy, "someone/triage", nested)
    paths = [SearchPath(root.parent, "local"), SearchPath(FIXTURE_AGENTS, "maintained")]
    client = TestClient(create_app(db_path=tmp_path / "a.db", agents_paths=paths))
    built = decisions_built(monkeypatch)
    for _ in range(3):
        assert timed_health(client) < HEALTH_BOUND_S
    health = client.get("/api/health").json()
    assert health["providers"]["someone/triage"] == OFFLINE
    assert health["agents_by_origin"]["local"] == 1
    assert built == []


def test_health_answers_within_its_bound_while_a_registry_agents_decision_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_copy: Callable[..., Path]
) -> None:
    """An agent the deployment does not own runs its patterns on RE2, outside the interpreter
    lock, within a CPU budget per decision: while one decision spends all of it, health keeps
    answering, and neither builds a Decision."""
    slow = [{"name": f"slow{i}", "pattern": p, "weight": 8} for i, p in enumerate(SLOW)]
    root = outside_copy(agent_copy, "someone/triage", slow, any_length=True)
    paths = [SearchPath(root.parent, "hub")]
    client = TestClient(create_app(db_path=tmp_path / "a.db", agents_paths=paths))
    built = decisions_built(monkeypatch)
    searches = searches_timed(monkeypatch)
    assert client.get("/api/health").json()["agents_by_origin"] == {"hub": 1}
    answer: dict[str, Any] = {}

    def decide() -> None:
        answer["decision"] = client.post(
            "/api/agents/someone/triage/decide", json={"message": {"body": EMOJI}}
        )

    worker = threading.Thread(target=decide)
    calls: list[tuple[float, float]] = []
    with interpreter_ticks() as ticks:
        worker.start()
        while worker.is_alive():
            calls.append(health_span(client))
        worker.join()
    # Health keeps answering while the patterns run: calls start and end inside a search, which
    # none can behind a pattern holding the interpreter lock (no other thread runs Python until
    # it returns) or behind a lock health takes too, and typically within the bound. Nor does
    # anything hold the interpreter lock, computing, for the whole budget at any point (as a
    # stretch of the decision on `re` would: health would wait that long). How long the slowest
    # health call took is not bounded: that cannot tell a block from a stall of health's own
    # work (its database, on a busy disk) or of the whole process.
    inside = [
        (started, ended)
        for started, ended in calls
        if any(begun <= started and ended <= done for begun, done in searches)
    ]
    steps = [round(done - begun, 3) for begun, done in searches]
    assert len(inside) >= 5, f"{len(inside)} of {len(calls)} health calls inside searches {steps}"
    took = [ended - started for started, ended in calls]
    assert statistics.median(took) < BUSY_HEALTH_BOUND_S, took
    held = max((later - earlier for earlier, later in itertools.pairwise(ticks)), default=0.0)
    assert held < patterns.BOUNDED_TIME_BUDGET_S, f"{held:.3f} s computed between two ticks"
    assert built == []
    decided = answer["decision"]
    assert decided.status_code == 200, decided.text
    assert set(decided.json()["errors"].values()) == {"time_budget"}
    assert decided.json()["outcome"] != "AUTO"


def test_an_unbuildable_agent_is_built_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_copy: Callable[..., Path]
) -> None:
    """Start-up builds every served agent once; one whose providers cannot be built is never
    built again: health lists it, and each decision on it answers 422 with the same reason."""
    agent_id = "outsider/triage"

    def unbuildable(manifest: dict[str, Any]) -> None:
        manifest["id"] = agent_id
        router = manifest["providers"]["router"]
        router["use"] = "agent"  # its default, whose config has a key no agent variant takes
        router["variants"]["agent"]["bogus"] = 1

    outside = agent_copy(manifest=unbuildable)
    built = decisions_built(monkeypatch)
    client = TestClient(
        create_app(db_path=tmp_path / "c.db", agents_paths=[outside, FIXTURE_AGENTS])
    )
    for _ in range(5):
        invalid = client.get("/api/health").json()["invalid"]
        assert list(invalid) == [agent_id]
        assert "Extra inputs are not permitted" in invalid[agent_id]
    case = yaml.safe_load((TRIAGE_DIR / "tests" / "cases.yaml").read_text())[0]
    refused = [client.post(f"/api/agents/{agent_id}/decide", json=case["input"]) for _ in range(2)]
    assert [answer.status_code for answer in refused] == [422, 422]
    reasons = [(answer.json()["error"], answer.json()["detail"]) for answer in refused]
    assert reasons == [("AgentValidationError", invalid[agent_id])] * 2
    assert sorted(built) == sorted(set(built)) and built.count(agent_id) == 1
    assert len(built) == len(bundled_agents()) + 1  # every served agent, once


def test_a_negative_limit_is_not_no_limit(client: TestClient) -> None:
    """`min(limit, 500)` had no floor: SQLite read `LIMIT -1` as every row (past the 500 cap,
    each input read and chained under PHI mode) and Postgres answered 500 for it."""
    for _ in range(3):
        assert (
            client.post(f"{AGENT}/decide", json={"message": {"body": "Chest pain"}}).status_code
            == 200
        )
    for path in ("/api/audit", "/api/review", "/api/feedback", "/api/deployments"):
        listed = client.get(path, params={"limit": -1})
        assert listed.status_code == 200, path
        assert listed.json() == [], path  # clamped to 0, never "no limit"
    assert len(client.get("/api/audit", params={"limit": 2}).json()) == 2


def test_an_offset_past_any_integer_is_clamped_never_a_500(client: TestClient) -> None:
    """`offset` was clamped below only: 20 digits overflowed the database driver's integer."""
    huge = client.get("/api/audit", params={"offset": "9" * 20})
    assert huge.status_code == 200 and huge.json() == []
