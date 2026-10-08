# SPDX-License-Identifier: Apache-2.0
"""The status checks (deploy/status/checks.yaml, scripts/status_checks.py, LP-21b) and the
status page (/status): every check passes on a working deployment, and readiness, sign-in and
a decision fail in a database outage while /api/health stays up (why the checks never use it);
the page is public, static and does no database work."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from mendari.core import db as db_module
from mendari.server import readiness
from mendari.server.app import create_app
from tests.conftest import ROOT
from tests.test_auth import FakeIdp, key_for, signing_in_with
from tests.test_readiness import break_sqlite

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
    site = TestClient(create_app(db_path=tmp_path / "a.db"))  # open mode: no sign-in
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
    here = httpx.Response(302, headers={"location": "https://site.test/auth/error"})
    assert checks._problem(
        here, {"status": [302], "location_leaves": "https://site.test"}, "https://site.test"
    )
    away = httpx.Response(302, headers={"location": "https://idp.test/authorize?x=1"})
    expect = {"status": [302], "location_leaves": "https://site.test"}
    assert checks._problem(away, expect, "https://site.test") is None
    empty = httpx.Response(200, json={"id": "d1"})
    assert checks._problem(empty, {"status": [200], "json_has": "outcome"}, "https://x") == (
        "the answer has no outcome"
    )
    down = elsewhere(503)
    results = checks.run(checks.load(), {"registry": "https://r.test"}, lambda base: down, {})
    assert outcomes(results)["registry"] == "failed"


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

        monkeypatch.setattr(db_module.SqliteBackend, "session", spy)
        page = site.get("/status")
        assert page.status_code == 200 and "text/html" in page.headers["content-type"]
        assert opened == []  # nothing read: the browser asks readiness itself
        assert "/static/status.js?v=" in page.text  # stamped, like every page's modules
        policy = page.headers["content-security-policy"]
        assert "'unsafe-inline'" not in policy and page.headers["cache-control"] == "no-cache"
        assert site.get("/status", headers={"authorization": "Bearer ohk_junk"}).status_code == 200
