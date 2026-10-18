# SPDX-License-Identifier: Apache-2.0
"""The registry's rate limits and security headers (LP-18f), asserted by exact value on every
route; and the Hub's half, which only CI's `hub-site` job runs (Playwright and the OWASP ZAP
baseline are not pytest), pinned here by its steps (LP-18h)."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from mendari.errors import MendariError
from mendari.hub.registry import (
    REGISTRY_CSP,
    RegistryStore,
    create_registry_app,
    registry_limit_settings,
)
from tests.conftest import REGISTRY_ADMIN

ROOT = Path(__file__).resolve().parents[1]
HUB_TESTS = ROOT / "hub" / "tests"
# A pinned image: a version and its digest.
ZAP_BASELINE = re.compile(
    r"\bdocker run .*ghcr\.io/zaproxy/zaproxy:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}\s+"
    r"zap-baseline\.py -t http://127\.0\.0\.1:3100 -J zap\.json\b"
)

EXPECTED = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "content-security-policy": (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
    "cross-origin-embedder-policy": "require-corp",
    "cache-control": "no-store",
}


def _example(path: str) -> str:
    return (
        path.replace("{namespace}", "mendari")
        .replace("{name}", "referral-router")
        .replace("{version}", "0.0.1")
    )


def test_every_registry_route_answers_with_the_headers(tmp_path: Path) -> None:
    """On every route, as every method it takes, answered or refused: the exact values, and
    HSTS but on this machine."""
    app = create_registry_app(tmp_path / "r.db", admin_token=REGISTRY_ADMIN)
    with TestClient(app, base_url="https://hub.example.org") as remote:
        local = TestClient(app, base_url="http://localhost")
        seen = 0
        for route in app.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in route.methods or ():
                answer = remote.request(method, _example(route.path))
                for name, value in EXPECTED.items():
                    assert answer.headers.get(name) == value, (method, route.path, name)
                assert answer.headers["strict-transport-security"] == "max-age=63072000"
                assert (
                    "strict-transport-security"
                    not in local.request(method, _example(route.path)).headers
                )
                seen += 1
    assert seen >= 15
    assert EXPECTED["content-security-policy"] == REGISTRY_CSP


def test_writes_are_limited_per_token_before_their_body_is_read(
    db_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_REGISTRY_RATE_LIMITS", "on,registry_write=2/h:2")
    app = create_registry_app(db_target, admin_token=REGISTRY_ADMIN)
    with TestClient(app) as client:
        admin = {"authorization": f"Bearer {REGISTRY_ADMIN}"}
        for name in ("a", "b"):
            assert (
                client.post("/v1/namespaces", json={"name": name}, headers=admin).status_code == 201
            )
        refused = client.post("/v1/namespaces", json={"name": "c"}, headers=admin)
        assert refused.status_code == 429 and int(refused.headers["retry-after"]) >= 1
        assert refused.headers["x-frame-options"] == "DENY"
        # Another token has its own bucket; a refused write stored and chained nothing.
        other = {"authorization": "Bearer ohr_someone_else"}
        assert client.post("/v1/namespaces", json={"name": "d"}, headers=other).status_code == 401
    with RegistryStore(db_target) as store, store._db.session(exclusive=False) as db:
        names = {row["name"] for row in db.all("SELECT name FROM hub_namespaces")}
    assert "c" not in names and {"a", "b"} <= names


def test_reads_are_limited_per_address_and_log_mode_never_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_REGISTRY_RATE_LIMITS", "on,registry_read=3/min:3")
    with TestClient(create_registry_app(tmp_path / "a.db")) as client:
        codes = [client.get("/v1/health").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    monkeypatch.setenv("MENDARI_REGISTRY_RATE_LIMITS", "log,registry_read=3/min:3")
    with TestClient(create_registry_app(tmp_path / "b.db")) as client:
        assert {client.get("/v1/health").status_code for _ in range(5)} == {200}


@pytest.mark.parametrize("value", ["maybe", "registry_write=often", "decide=10/min"])
def test_a_bad_setting_stops_the_registry(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("MENDARI_REGISTRY_RATE_LIMITS", value)
    with pytest.raises(MendariError, match="MENDARI_REGISTRY_RATE_LIMITS"):
        registry_limit_settings()


def test_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MENDARI_REGISTRY_RATE_LIMITS", raising=False)
    settings: Any = registry_limit_settings()
    assert settings.mode == "on"
    assert {name: str(rate) for name, rate in settings.rates.items()} == {
        "registry_read": "600/min:200",
        "registry_write": "120/h:30",
        "registry_report": "60/h:10",
    }


def test_made_up_tokens_share_their_senders_bucket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The LP-18f review: each made-up bearer token had a bucket of its own, so writes with a
    fresh one each time were never limited. A token the registry did not issue counts against
    its sender's address."""
    monkeypatch.setenv("MENDARI_REGISTRY_RATE_LIMITS", "on,registry_write=2/h:2")
    with TestClient(create_registry_app(tmp_path / "a.db", admin_token=REGISTRY_ADMIN)) as client:
        codes = [
            client.post(
                "/v1/namespaces",
                json={"name": "x"},
                headers={"authorization": f"Bearer made-up-{n}"},
            ).status_code
            for n in range(4)
        ]
    assert codes == [401, 401, 429, 429]


@pytest.mark.parametrize(
    ("origin", "hsts"),
    [("https://hub.example.org", "max-age=63072000"), ("http://localhost", None)],
)
def test_an_unhandled_error_is_a_bare_500_with_the_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: str, hsts: str | None
) -> None:
    """Every header every answer carries, HSTS too but on this machine (the LP-18h review: the
    500 is answered outside the middleware, and had no HSTS)."""
    app = create_registry_app(tmp_path / "a.db")
    with TestClient(app, base_url=origin, raise_server_exceptions=False) as client:
        monkeypatch.setattr(RegistryStore, "latest", lambda self: 1 / 0)
        answer = client.get("/v1/search")
    assert answer.status_code == 500 and answer.json()["detail"] == "internal error"
    for name, value in EXPECTED.items():
        assert answer.headers.get(name) == value, name
    assert answer.headers.get("strict-transport-security") == hsts


def test_the_hub_site_job_runs_the_hubs_tests_and_the_pinned_scanner() -> None:
    """The Hub's half of LP-18's tests 4 and 5 (exact headers on every page; a script in
    publisher text is inert) runs only in CI's `hub-site`: the registry seeded with the hostile
    example, the Hub's Playwright tests (`headers.spec.ts`, and the smoke test on publisher
    text), then the pinned ZAP baseline over the local site and `scripts/zap_check.py`, which
    fails on a medium or high risk. A step dropped, reordered, allowed to fail or switched off
    with `if:`, the job switched off, or `npm test` made to run fewer tests fails here."""
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    assert "pull_request" in ci[True]  # YAML reads the key `on` as true
    job = ci["jobs"]["hub-site"]
    steps = job["steps"]
    assert "continue-on-error" not in job
    assert not [step for step in steps if step.get("continue-on-error")]
    needs = job.get("needs", [])
    for name in ["hub-site", *([needs] if isinstance(needs, str) else needs)]:
        assert "if" not in ci["jobs"][name], name  # a skipped job it needs skips it too

    def step(matches: Any) -> int:
        found = [i for i, s in enumerate(steps) if matches(s)]
        assert len(found) == 1, found
        assert "if" not in steps[found[0]], steps[found[0]]
        return found[0]

    seed = step(lambda s: "scripts/hub_seed.py --registry" in s.get("run", ""))
    assert "--hostile-example" in steps[seed]["run"]
    assert "mendari registry verify" in steps[seed]["run"]
    tests = step(lambda s: s.get("run") == "npm test" and s.get("working-directory") == "hub")
    started = step(
        lambda s: "npm run start" in s.get("run", "") and s.get("working-directory") == "hub"
    )
    scan = step(lambda s: ZAP_BASELINE.search(" ".join(s.get("run", "").split())) is not None)
    check = step(lambda s: s.get("run") == "uv run python scripts/zap_check.py /tmp/zap/zap.json")
    assert "-v /tmp/zap:/zap/wrk" in steps[scan]["run"]  # where zap_check reads the report
    assert seed < tests < started < scan < check
    headers = (HUB_TESTS / "headers.spec.ts").read_text()
    assert 'test("every page and asset carries the headers, each nonce fresh"' in headers
    assert '"/packs/hub-test/hostile-text"' in headers
    smoke = (HUB_TESTS / "smoke.spec.ts").read_text()
    title = "publisher text is shown as text: no script runs and no unsafe link is made"
    assert f'test("{title}"' in smoke
    for spec in (headers, smoke):  # no test skipped, and no `.only` that leaves the rest out
        assert not re.search(r"\.(only|skip|fixme)\(", spec)
    # `npm test` runs every test under hub/tests, whatever its file is called.
    package = json.loads((ROOT / "hub" / "package.json").read_text())
    assert package["scripts"]["test"] == "playwright test"
    config = (ROOT / "hub" / "playwright.config.ts").read_text()
    assert 'testDir: "tests"' in config
    assert not re.search(r"\b(testMatch|testIgnore|grep|grepInvert|shard)\b", config)


def _zap_check() -> ModuleType:
    path = ROOT / "scripts" / "zap_check.py"
    spec = importlib.util.spec_from_file_location("zap_check", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("risk", "code"), [(0, 0), (1, 0), (2, 1), (3, 1)])
def test_zap_check_fails_on_a_medium_or_high_risk_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], risk: int, code: int
) -> None:
    """`scripts/zap_check.py` (CI's `security` and `hub-site`): informational and low alerts
    are listed and pass; a medium or high one fails, and so does a report holding one among
    others."""
    zap_check = _zap_check()
    assert zap_check.ACCEPTED == {}  # no medium or high risk is accepted
    alert = {"riskcode": str(risk), "pluginid": "10038", "name": "A finding", "count": "2"}
    quiet = {"riskcode": "0", "pluginid": "10049", "name": "Informational", "count": "1"}
    report = tmp_path / "zap.json"
    report.write_text(json.dumps({"site": [{"alerts": [quiet]}, {"alerts": [alert]}]}))
    assert zap_check.main(report) == code
    printed = capsys.readouterr().out
    assert ("FAIL" in printed) is bool(code)
    assert printed.rstrip().endswith(
        "medium or high risks found" if code else "no medium or high risks"
    )
