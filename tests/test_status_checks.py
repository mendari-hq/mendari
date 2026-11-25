# SPDX-License-Identifier: Apache-2.0
"""The status checks (deploy/status/checks.yaml, scripts/status_checks.py, LP-21b) and the
status page (/status): every check passes on a working deployment, and readiness, sign-in and
a decision fail in a database outage while /api/health stays up (why the checks never use it);
the export gives a status provider every check and every key the checker enforces, never a
key's value; the page is public, static and does no database work."""

from __future__ import annotations

import ast
import copy
import importlib.util
import inspect
import json
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from mendari.core import db as db_module
from mendari.server import readiness
from mendari.server.app import create_app
from tests.conftest import ROOT
from tests.test_auth import FakeIdp, key_for, signing_in_with
from tests.test_readiness import break_sqlite

ROOT_DIR = Path(__file__).resolve().parents[1]
# The bundled demo agent the status check decides with, beside the tests' fixture agents.
SERVED = [ROOT_DIR / "agents", ROOT_DIR / "tests" / "fixtures" / "agents"]

KEY = "STATUS_DECIDE_KEY"


def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "status_checks", ROOT / "scripts" / "status_checks.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["status_checks"] = module
    spec.loader.exec_module(module)
    return module


def elsewhere(status: int) -> httpx.Client:
    """The registry or the Hub, answering every request with `status`."""
    return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status)))


def outcomes(results: list[Any]) -> dict[str, str]:
    return {result.name: result.outcome for result in results}


def test_every_check_passes_on_a_working_site_and_readiness_sign_in_and_decide_fail_in_an_outage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks = script()
    monkeypatch.setattr(readiness, "READY_SECONDS", 0.0)
    idp = FakeIdp()
    monkeypatch.setenv("MENDARI_AGENTS_PATH", str(ROOT / "agents"))  # the agent it decides with
    with signing_in_with(idp, tmp_path, monkeypatch) as site:
        key = key_for(site, idp)
        clients = {"api": site, "registry": elsewhere(200), "hub": elsewhere(200)}
        bases = {"api": str(site.base_url), "registry": "https://r.test", "hub": "https://h.test"}
        spec = checks.load()
        results = checks.run(spec, bases, clients.__getitem__, {KEY: key})
        assert outcomes(results) == {
            "readiness": "ok",
            "sign-in": "ok",
            "decide": "ok",
            "registry": "ok",
            "hub": "ok",
        }
        break_sqlite(tmp_path / "a.db", "garbage")
        assert site.get("/api/health").status_code == 200  # liveness stays up: no use here
        # A status check sees the server's 500s as answers, not as the test's exceptions.
        clients["api"] = TestClient(site.app, base_url=bases["api"], raise_server_exceptions=False)
        results = checks.run(spec, bases, clients.__getitem__, {KEY: key})
        assert outcomes(results) == {
            "readiness": "failed",
            "sign-in": "failed",
            "decide": "failed",
            "registry": "ok",
            "hub": "ok",
        }
        for result in results:
            assert key not in result.detail


def test_a_check_is_skipped_without_its_base_its_key_or_sign_in(tmp_path: Path) -> None:
    checks = script()
    site = TestClient(
        create_app(db_path=tmp_path / "a.db", agents_paths=SERVED)
    )  # open mode: no sign-in
    results = checks.run(checks.load(), {"api": str(site.base_url)}, lambda base: site, {})
    assert outcomes(results) == {
        "readiness": "ok",
        "sign-in": "skipped",
        "decide": "skipped",
        "registry": "skipped",
        "hub": "skipped",
    }
    assert {r.name: r.detail for r in results}["decide"] == f"set {KEY}"


def test_a_redirect_that_stays_here_or_an_answer_without_an_outcome_fails() -> None:
    checks = script()
    expect = {"status": [302], "location_leaves": "https://site.test"}
    here = httpx.Response(302, headers={"location": "https://site.test/auth/error"})
    assert checks._problem(here, expect, "https://site.test")
    moved = httpx.Response(302, headers={"location": "https://public.test/auth/login?next=%2F"})
    assert checks._problem(moved, expect, "https://site.test") == (
        "the redirect goes to another of Mendari's addresses: give --api that one"
    )
    away = httpx.Response(302, headers={"location": "https://idp.test/authorize?x=1"})
    assert checks._problem(away, expect, "https://site.test") is None
    empty = httpx.Response(200, json={"id": "d1"})
    assert checks._problem(empty, {"status": [200], "json_has": "outcome"}, "https://x") == (
        "the answer has no outcome"
    )
    down = elsewhere(503)
    results = checks.run(checks.load(), {"registry": "https://r.test"}, lambda base: down, {})
    assert outcomes(results)["registry"] == "failed"


def test_sign_in_is_skipped_when_health_does_not_say(tmp_path: Path) -> None:
    """Open mode in an outage: /api/health reads the database there and fails, so whether
    sign-in is on is not known; the check says so rather than failing."""
    checks = script()
    app = create_app(db_path=tmp_path / "a.db", agents_paths=SERVED)
    break_sqlite(tmp_path / "a.db", "garbage")
    site = TestClient(app, raise_server_exceptions=False)
    results = checks.run(checks.load(), {"api": str(site.base_url)}, lambda base: site, {})
    found = {r.name: (r.outcome, r.detail) for r in results}
    assert found["sign-in"] == ("skipped", "/api/health did not say")
    assert found["readiness"][0] == "failed"


def test_the_command_prints_one_line_a_check_and_exits_1_on_a_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    checks = script()

    def nothing_answers(self: Any, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", nothing_answers)
    monkeypatch.setenv(KEY, "ohk_secret-value")
    assert checks.main(["--api", "https://mendari.invalid"]) == 1
    out = capsys.readouterr().out
    assert "readiness: failed (no answer (ConnectError))" in out
    assert "registry: skipped (no --registry given)" in out
    assert "ohk_secret-value" not in out


# -- the export ----------------------------------------------------------------------------------

API, REGISTRY, HUB = "https://mendari.example.org", "https://r.example.org", "https://h.example.org"


def keys_read(function: Callable[..., Any], name: str) -> set[str]:
    """The keys `function` reads of its variable `name`: `name["k"]`, `name.get("k")` and
    `"k" in name`."""

    def text(node: ast.expr) -> str:
        assert isinstance(node, ast.Constant) and isinstance(node.value, str), ast.dump(node)
        return node.value

    def named(node: ast.expr) -> bool:
        return isinstance(node, ast.Name) and node.id == name

    found: set[str] = set()
    for node in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(function)))):
        if isinstance(node, ast.Subscript) and named(node.value):
            found.add(text(node.slice))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and named(node.func.value)
        ):
            found.add(text(node.args[0]))
        elif (
            isinstance(node, ast.Compare)
            and isinstance(node.ops[0], ast.In)
            and named(node.comparators[0])
        ):
            found.add(text(node.left))
    return found


def changed(value: Any) -> Any:
    """`value` changed, of the same type."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, str):
        return value + "x"
    if isinstance(value, list):
        return [changed(item) for item in value]
    if isinstance(value, dict):
        return {**value, "x": "x"}
    raise AssertionError(f"no change made for {value!r}")


def not_exported(checks: ModuleType, spec: dict[str, Any]) -> list[str]:
    """Each key the export lets through but does not give the provider: one `export` does not
    read, or one a check of `spec` uses that can change without the export changing."""
    known = {"check": checks.CHECK_KEYS, "request": checks.REQUEST_KEYS}
    known["expect"] = set(checks.EXPECT_FIELDS)
    missing = [
        f"{part} key {key}: the export does not read it"
        for part, keys in known.items()
        for key in sorted(set(keys) - keys_read(checks.export, part))
    ]
    bases = {"api": API, "registry": REGISTRY, "hub": HUB}
    before = checks.export(spec, bases)
    for index, check in enumerate(spec["checks"]):
        places = [("check", key) for key in check if key not in ("request", "expect")]
        places += [(part, key) for part in ("request", "expect") for key in check[part]]
        for part, key in places:
            after = copy.deepcopy(spec)
            where = after["checks"][index]
            where = where if part == "check" else where[part]
            where[key] = changed(where[key])
            if checks.export(after, bases) == before:
                missing.append(f"{check['name']}, {part} {key}: the export does not change with it")
    return missing


def test_the_export_gives_every_check_and_every_key_the_checker_enforces(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A key the checker starts to read (`run`, `_problem`) fails here until the export gives it
    to the provider; a key the checks file starts to use fails until the export knows it."""
    checks = script()
    enforced = keys_read(checks._problem, "expect")
    assert "status" in enforced  # the scan reads the checker
    assert enforced == set(checks.EXPECT_FIELDS)
    assert keys_read(checks.run, "request") <= checks.REQUEST_KEYS
    assert keys_read(checks.run, "check") <= checks.CHECK_KEYS
    assert not_exported(checks, checks.load()) == []
    monkeypatch.setenv(KEY, "ohk_secret-value")
    argv = ["--export", "json", "--api", API, "--registry", REGISTRY, "--hub", HUB]
    assert checks.main(argv) == 0
    out, err = capsys.readouterr()
    assert err == "" and "ohk_secret-value" not in out
    spec = checks.load()
    exported = {entry["name"]: entry for entry in json.loads(out)["checks"]}
    assert list(exported) == [check["name"] for check in spec["checks"]]
    for check in spec["checks"]:
        entry = exported[check["name"]]
        assert entry["method"] == check["request"]["method"]
        assert entry["url"].startswith(("https://mendari.", "https://r.", "https://h."))
        assert "{" not in entry["url"]
        assert entry["interval_seconds"] == spec["interval_seconds"] == 300
        assert entry["follow_redirects"] is False  # the checker never follows one
        assert entry.get("json_body") == check["request"].get("json")
        for key in check["expect"]:
            for field in checks.EXPECT_FIELDS[key]:
                assert field in entry, (check["name"], key, field)
        assert entry.get("only_with_sign_in", False) is check.get("only_with_sign_in", False)
    assert exported["readiness"]["url"] == f"{API}/api/health/ready"
    assert exported["readiness"]["expected_statuses"] == [200]
    assert exported["sign-in"]["expected_statuses"] == [302]
    assert exported["sign-in"]["location_leaves_host"] == "mendari.example.org"
    assert exported["sign-in"]["location_path_not"] == "/auth/login"
    assert exported["decide"]["headers"] == {
        "Content-Type": "application/json",
        "Authorization": "Bearer ${STATUS_DECIDE_KEY}",
    }
    assert exported["decide"]["secret_from_environment"] == KEY
    assert exported["decide"]["body_keyword"] == '"outcome":'
    assert exported["registry"]["url"] == f"{REGISTRY}/v1/health"
    assert exported["hub"]["url"] == f"{HUB}/"


def test_a_key_the_export_lets_through_but_drops_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Headers the checker started to send, let through by REQUEST_KEYS but not exported: the
    provider would send another request than the checker, and nothing would say so."""
    checks = script()
    monkeypatch.setattr(checks, "REQUEST_KEYS", checks.REQUEST_KEYS | {"headers"})
    spec = copy.deepcopy(checks.load())
    spec["checks"][0]["request"]["headers"] = {"X-Probe": "1"}
    (readiness_entry, *_), _ = checks.export(spec, {"api": API})
    assert "headers" not in readiness_entry  # dropped, with no error
    assert not_exported(checks, spec) == [
        "request key headers: the export does not read it",
        "readiness, request headers: the export does not change with it",
    ]


def provider_says_up(entry: dict[str, Any], answer: httpx.Response) -> bool:
    """Whether a provider set up from an exported check calls `answer` up, reading the status
    and Location fields as docs/operations/status-checks.md says."""
    if answer.status_code not in entry["expected_statuses"]:
        return False
    if "location_leaves_host" in entry:
        there = urlsplit(answer.headers.get("location", ""))
        if there.netloc in ("", entry["location_leaves_host"]):
            return False
        if there.path == entry.get("location_path_not"):
            return False
    return True


@pytest.mark.parametrize(
    ("status", "location"),
    [
        (302, "https://idp.example.com/authorize?redirect_uri=https%3A%2F%2Fmendari.example.org"),
        (302, "https://public.example.org/auth/login?next=%2F"),  # another of Mendari's addresses
        (302, "https://mendari.example.org/auth/error"),
        (302, "http://mendari.example.org/auth/callback"),  # the same host, another scheme
        (302, "/auth/login?next=%2F"),
        (302, None),
        (200, None),
    ],
)
def test_a_provider_set_up_from_the_export_calls_a_sign_in_redirect_what_the_checker_does(
    status: int, location: str | None
) -> None:
    checks = script()
    spec = checks.load()
    (expect,) = [check["expect"] for check in spec["checks"] if check["name"] == "sign-in"]
    (entry,) = [e for e in checks.export(spec, {"api": API})[0] if e["name"] == "sign-in"]
    answer = httpx.Response(status, headers={"location": location} if location else {})
    assert provider_says_up(entry, answer) is (checks._problem(answer, expect, API) is None)


def test_the_body_keyword_is_in_a_real_decision_and_not_in_an_answer_without_one(
    tmp_path: Path,
) -> None:
    checks = script()
    (entry,) = [e for e in checks.export(checks.load(), {"api": API})[0] if e["name"] == "decide"]
    site = TestClient(
        create_app(db_path=tmp_path / "a.db", agents_paths=SERVED)
    )  # open mode: no key needed
    answer = site.post(entry["url"].removeprefix(API), json=entry["json_body"])
    assert answer.status_code == 200 and entry["body_keyword"] in answer.text
    assert entry["body_keyword"] not in json.dumps({"id": "d1", "note": "outcome"})


def test_a_check_without_its_base_is_left_out_and_said_so(
    capsys: pytest.CaptureFixture[str],
) -> None:
    checks = script()
    assert checks.main(["--export", "json", "--api", API + "/"]) == 0
    out, err = capsys.readouterr()
    names = [entry["name"] for entry in json.loads(out)["checks"]]
    assert names == ["readiness", "sign-in", "decide"]
    assert json.loads(out)["checks"][0]["url"] == f"{API}/api/health/ready"
    assert err.splitlines() == [
        "registry: skipped (no --registry given)",
        "hub: skipped (no --hub given)",
    ]
    with pytest.raises(SystemExit):
        checks.main(["--export", "json", "--api", "mendari.example.org"])
    assert "--api must be an absolute URL" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("part", "key"), [("expect", "header_has"), ("request", "timeout"), (None, "regions")]
)
def test_a_key_the_export_does_not_know_is_refused_never_dropped(
    part: str | None, key: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checks = script()
    spec = copy.deepcopy(checks.load())
    check = spec["checks"][0]
    (check if part is None else check[part])[key] = "x"
    with pytest.raises(ValueError, match=f"the export does not know {key}"):
        checks.export(spec, {"api": API})
    changed = tmp_path / "checks.yaml"
    changed.write_text(json.dumps(spec))  # JSON is YAML
    assert checks.main(["--export", "json", "--api", API, "--checks", str(changed)]) == 1
    out, err = capsys.readouterr()
    where = "check readiness" if part is None else f"check readiness, {part}"
    assert out == "" and f"error: {where}: the export does not know {key};" in err


# -- the status page -----------------------------------------------------------------------------


def test_the_status_page_is_public_static_and_does_no_database_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[bool] = []
    with signing_in_with(FakeIdp(), tmp_path, monkeypatch) as site:
        real = db_module.SqliteBackend.session

        def spy(self: Any, *, exclusive: bool) -> Any:
            opened.append(exclusive)
            return real(self, exclusive=exclusive)

        def pinged(self: Any, sql: str, timeout_s: float) -> None:
            opened.append(False)

        for backend in (db_module.SqliteBackend, db_module.PostgresBackend):
            monkeypatch.setattr(backend, "session", spy)
            monkeypatch.setattr(backend, "ping", pinged)
        page = site.get("/status")
        assert page.status_code == 200 and "text/html" in page.headers["content-type"]
        assert opened == []  # nothing read, not even readiness: the browser asks for it
        assert "/static/status.js?v=" in page.text  # stamped, like every page's modules
        policy = page.headers["content-security-policy"]
        assert "'unsafe-inline'" not in policy and page.headers["cache-control"] == "no-cache"
        assert site.get("/status", headers={"authorization": "Bearer ohk_junk"}).status_code == 200


@pytest.mark.parametrize("mounted", [True, False])
def test_the_registry_check_reads_the_mounted_registry(
    mounted: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under the engine (LP-19j) the registry's base is the engine's origin with `/registry`: the
    same check, unchanged, is up there when the registry is mounted and fails when it is not,
    while the engine's own checks stay up either way."""
    checks = script()
    if mounted:
        monkeypatch.setenv("MENDARI_REGISTRY", "on")
    else:
        monkeypatch.delenv("MENDARI_REGISTRY", raising=False)
    app = create_app(
        db_path=tmp_path / "a.db", agents_paths=SERVED
    )  # open mode: the registry needs no sign-in
    try:
        with TestClient(app) as site:
            api = str(site.base_url)
            bases = {"api": api, "registry": f"{api}/registry"}
            results = checks.run(checks.load(), bases, lambda base: site, {})
            found = {r.name: (r.outcome, r.detail) for r in results}
            assert found["readiness"][0] == "ok"
            if mounted:
                assert found["registry"] == ("ok", "200")
            else:
                assert found["registry"][0] == "failed"
                assert "404" in found["registry"][1]
    finally:
        store = getattr(app.state, "registry_store", None)
        if store is not None:
            store.close()
