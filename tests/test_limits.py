"""Rate limits and per-organization quotas (server/limits.py, ADR-0005), on SQLite and Postgres.

`tests/conftest.py` turns limits off for every other test; each test here turns them on. Synthetic
data only."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.routing import Route
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from mendari import cli
from mendari.core import clock, tenancy
from mendari.core.audit import AuditStore
from mendari.core.decision import CallBudget, Decision
from mendari.errors import MendariError, ProviderError
from mendari.server import app as app_module
from mendari.server import auth as auth_module
from mendari.server.app import create_app, error_response
from mendari.server.auth import permission_for
from mendari.server.limits import (
    DEFAULT_RATES,
    Limiter,
    Rate,
    client_ip_source,
    limit_settings,
)
from mendari.standards.fhir_client import GuardedHttp
from tests.conftest import ROOT, message, signed_in
from tests.test_auth import IDP, FakeIdp
from tests.test_backend_services import (
    PUBLIC_URL,
    RSA_KEY,
    STRANGER,
    Server,
    ask,
    assertion,
)
from tests.test_backend_services import on as backend_on

INBOX = "mendari/patient-inbox-router"
DECIDE = f"/api/packs/{INBOX}/decide"
ROUTINE = "Can you send a refill for metformin to my pharmacy on Main Street?"
NO_STORE = "no-store"


def sign_in_on(monkeypatch: pytest.MonkeyPatch, limits: str) -> None:
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari-org")
    monkeypatch.setenv("MENDARI_RATE_LIMITS", limits)


def service_key(db: str) -> dict[str, str]:
    with AuditStore(db) as store:
        made = store.create_api_key(name="ehr", role="service", org="default", created_by="t")
    return {"authorization": f"Bearer {made['key']}"}


def person(db: str, role: str = "admin") -> dict[str, str]:
    with AuditStore(db) as store:
        return signed_in(store, role=role)


def buckets(db: str) -> list[str]:
    with AuditStore(db) as store, store._db.session(exclusive=False) as session:
        return [row["bucket"] for row in session.all("SELECT bucket FROM rate_limits")]


def assert_retry_after(answer: Any) -> int:
    assert answer.status_code == 429, answer.text
    assert re.fullmatch(r"[1-9]\d*", answer.headers["retry-after"])
    assert answer.headers["cache-control"] == NO_STORE
    return int(answer.headers["retry-after"])


# -- settings -------------------------------------------------------------------------------------


def test_limits_are_on_with_sign_in_and_off_in_open_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MENDARI_RATE_LIMITS")
    assert limit_settings(sign_in=True).mode == "on"
    assert limit_settings(sign_in=False).mode == "off"  # every caller there is one principal
    assert limit_settings(sign_in=True).rates == DEFAULT_RATES
    assert {name: str(rate) for name, rate in DEFAULT_RATES.items()} == {
        "signin": "60/min",
        "token": "60/min",
        "token_failures": "10/10min",
        "decide": "600/min:100",
        "guard": "60/min",
        "evaluate": "6/h",
        "export": "10/h",
    }
    monkeypatch.setenv("MENDARI_RATE_LIMITS", "log, decide=1200/min:200, evaluate=20/h")
    chosen = limit_settings(sign_in=False)
    assert chosen.mode == "log"
    assert chosen.rates["decide"] == Rate(1200, 60, 200)
    assert chosen.rates["evaluate"] == Rate(20, 3600, 20)
    monkeypatch.setenv("MENDARI_RATE_LIMITS", "guard=30/min")  # overrides alone: default mode
    assert limit_settings(sign_in=True).mode == "on"


@pytest.mark.parametrize(
    ("variable", "value", "problem"),
    [
        ("MENDARI_RATE_LIMITS", "sometimes", "one mode"),
        ("MENDARI_RATE_LIMITS", "on,log", "one mode"),
        ("MENDARI_RATE_LIMITS", "on,decide=fast", "must look like"),
        ("MENDARI_RATE_LIMITS", "on,decide=600/day", "must look like"),
        ("MENDARI_RATE_LIMITS", "on,read=600/min", "no limit named"),
        ("MENDARI_RATE_LIMITS", "on,decide=0/min", "at least one"),
        ("MENDARI_RATE_LIMITS", "on,decide=600/min:0", "at least one"),
        ("MENDARI_ORG_DAILY_LIMITS", "decisions=many", "must look like"),
        ("MENDARI_ORG_DAILY_LIMITS", "tokens=5", "must look like"),
        ("MENDARI_ORG_DAILY_LIMITS", "decisions=5,decisions=6", "at most once"),
    ],
)
def test_a_bad_setting_fails_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str, value: str, problem: str
) -> None:
    monkeypatch.setenv(variable, value)
    with pytest.raises(MendariError, match=problem):
        create_app(db_path=tmp_path / "bad.db")


def test_a_quota_alert_webhook_is_checked_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_ORG_DAILY_LIMITS", "decisions=5000")
    monkeypatch.setenv("MENDARI_ALERT_WEBHOOK", "http://alerts.example.org/hook")
    with pytest.raises(MendariError, match="https"):
        create_app(db_path=tmp_path / "hook.db")


# -- the algorithm --------------------------------------------------------------------------------


def test_a_bucket_admits_its_burst_then_one_per_interval(store: AuditStore) -> None:
    start = 1_800_000_000_000
    take = store.rate_take
    assert [take("b", 1000, 3000, start) for _ in range(3)] == [None, None, None]
    assert take("b", 1000, 3000, start) == 1000  # the burst is used: room again in a second
    assert store.rate_peek("b", 1000, 3000, start) == 1000  # a peek counts nothing
    assert take("b", 1000, 3000, start + 999) == 1
    assert take("b", 1000, 3000, start + 1000) is None  # one interval later, one more
    assert take("b", 1000, 3000, start + 1000) == 1000
    assert take("other", 1000, 3000, start) is None  # buckets are independent
    # Once the bucket has refilled it is the same as no row, and a purge deletes it.
    store.rate_purge(start + 4000, 100)
    assert store.rate_peek("b", 1000, 3000, start + 4000) is None
    with store._db.session(exclusive=False) as session:
        assert session.all("SELECT bucket FROM rate_limits") == []


def test_sixteen_threads_never_exceed_the_limit(db_target: str) -> None:
    """160 attempts on one bucket from 16 threads, each through its own connection, against
    20 an hour (no refill during the test): exactly 20 are admitted."""
    store = AuditStore(db_target)
    rate = Rate(20, 3600, 20)
    admitted: list[bool] = []
    lock = threading.Lock()

    def attempt() -> None:
        for _ in range(10):
            now = int(time.time() * 1000)
            ok = store.rate_take("hot", rate.interval_ms, rate.tolerance_ms, now) is None
            with lock:
                admitted.append(ok)

    threads = [threading.Thread(target=attempt) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    store.close()
    assert len(admitted) == 160 and sum(admitted) == 20


def test_two_app_instances_share_one_count(db_target: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Serverless instances and replicas share nothing in memory: the count is in the database."""
    sign_in_on(monkeypatch, "on,decide=3/min")
    first = TestClient(create_app(db_path=db_target))
    second = TestClient(create_app(db_path=db_target))
    key = service_key(db_target)
    answers = [
        instance.post(DECIDE, json=message(ROUTINE), headers=key).status_code
        for instance in (first, second, first)
    ]
    assert answers == [200, 200, 200]
    assert_retry_after(second.post(DECIDE, json=message(ROUTINE), headers=key))
    assert_retry_after(first.post(DECIDE, json=message(ROUTINE), headers=key))
    # Another principal has its own bucket.
    assert first.post(DECIDE, json=message(ROUTINE), headers=person(db_target)).status_code == 200


# -- refusals -------------------------------------------------------------------------------------


def test_a_refused_request_calls_no_provider_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in_on(monkeypatch, "on,decide=1/min")
    db = str(tmp_path / "refused.db")
    client = TestClient(create_app(db_path=db))
    key = service_key(db)
    decided: list[str] = []
    real = Decision.decide

    def counted(self: Decision, *args: Any, **kwargs: Any) -> Any:
        decided.append(self.pack.id)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Decision, "decide", counted)
    assert client.post(DECIDE, json=message(ROUTINE), headers=key).status_code == 200
    with AuditStore(db) as store:
        before = (len(store.query(limit=500)), store.verify()["events"])
    for route in (DECIDE, f"/api/packs/{INBOX}/fhir/decide", "/fhir/$decide"):
        assert_retry_after(client.post(route, json=message(ROUTINE), headers=key))
    assert decided == [INBOX]  # the pack ran once, for the request admitted
    with AuditStore(db) as store:
        assert (len(store.query(limit=500)), store.verify()["events"]) == before
        assert store.verify()["ok"]


def test_each_refusal_says_when_to_retry_in_the_routes_own_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in_on(monkeypatch, "on,decide=1/min")
    db = str(tmp_path / "shapes.db")
    client = TestClient(create_app(db_path=db))
    key = service_key(db)
    # The limit counts before the body is looked at: an empty body uses the request too.
    assert client.post(DECIDE, json={}, headers=key).status_code == 200  # a recorded BLOCK
    api = client.post(DECIDE, json=message(ROUTINE), headers=key)
    assert 55 <= assert_retry_after(api) <= 60
    assert api.json() == {
        "error": "RateLimited",
        "detail": f"too many requests (decide: 1/min); retry in {api.headers['retry-after']} s",
    }
    fhir = client.post("/fhir/$decide", json={"resourceType": "Parameters"}, headers=key)
    assert_retry_after(fhir)
    assert fhir.headers["content-type"].startswith("application/fhir+json")
    issue = fhir.json()["issue"][0]
    assert fhir.json()["resourceType"] == "OperationOutcome" and issue["code"] == "throttled"
    hooks = client.post("/cds-services/patient-inbox-router", json={}, headers=key)
    assert assert_retry_after(hooks) and hooks.json()["error"] == "RateLimited"


def test_a_daily_budget_or_quota_says_retry_after_midnight() -> None:
    for code in ("budget_exhausted", "quota_exhausted"):
        answer = error_response("/api/x", 429, ProviderError(code))
        seconds = int(answer.headers["retry-after"])
        assert abs(seconds - clock.seconds_until_tomorrow()) <= 1 and 1 <= seconds <= 25 * 3600
        assert answer.headers["cache-control"] == NO_STORE
    assert "retry-after" not in error_response("/api/x", 502, ProviderError("timeout")).headers


def test_log_mode_counts_and_never_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`log` is the shadow mode (CI's ZAP scan, a first week in production): the limiter runs,
    says once what it would refuse, and refuses nothing."""
    sign_in_on(monkeypatch, "log,decide=2/min")
    db = str(tmp_path / "log.db")
    client = TestClient(create_app(db_path=db))
    key = service_key(db)
    with caplog.at_level(logging.WARNING, logger="mendari.limits"):
        answers = [client.post(DECIDE, json={}, headers=key).status_code for _ in range(5)]
    assert answers == [200] * 5
    reached = [r.getMessage() for r in caplog.records if "rate limit reached" in r.getMessage()]
    assert len(reached) == 1 and "log mode: not refused" in reached[0]
    assert [b.partition(":")[0] for b in buckets(db)] == ["decide|key"]  # counted all the same


def test_open_mode_has_no_limits_unless_turned_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MENDARI_RATE_LIMITS")
    db = str(tmp_path / "open.db")
    client = TestClient(create_app(db_path=db))
    assert all(client.post(DECIDE, json={}).status_code == 200 for _ in range(3))
    assert buckets(db) == []


# -- keys and addresses ---------------------------------------------------------------------------


def signing_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limits: str,
    *,
    address: str = "203.0.113.5",
    wrap: bool = False,
) -> tuple[TestClient, FakeIdp]:
    idp = FakeIdp()
    sign_in_on(monkeypatch, limits)
    transport = httpx.MockTransport(idp)
    monkeypatch.setattr(
        auth_module, "GuardedHttp", lambda **kw: GuardedHttp(transport=transport, **kw)
    )
    app: Any = create_app(db_path=tmp_path / "signin.db")
    if wrap:  # uvicorn's own proxy handling, as `mendari serve` runs it
        app = ProxyHeadersMiddleware(app, trusted_hosts="198.51.100.1")
    return TestClient(app, base_url=PUBLIC_URL, client=(address, 50000)), idp


def logins(client: TestClient, forwarded: list[str | None]) -> list[int]:
    return [
        client.get(
            "/auth/login",
            headers={"x-forwarded-for": f} if f else {},
            follow_redirects=False,
        ).status_code
        for f in forwarded
    ]


def test_sign_in_is_limited_per_address_with_a_page_that_says_when(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, idp = signing_in(tmp_path, monkeypatch, "on,signin=2/min")
    assert logins(client, [None, None]) == [302, 302]
    page = client.get("/auth/login", follow_redirects=False)
    assert_retry_after(page)
    assert page.headers["content-type"].startswith("text/html") and "Too many" in page.text
    assert idp.discovered == 1  # the refused sign-in fetched nothing
    other = TestClient(client.app, base_url=PUBLIC_URL, client=("203.0.113.6", 50000))
    assert logins(other, [None]) == [302]  # another address has its own bucket
    # A callback is not limited here (an unknown state costs one indexed delete); the edge
    # limits /auth/ per address.
    assert client.get("/auth/callback", params={"state": "x", "code": "y"}).status_code == 400


def test_a_forwarded_address_counts_only_where_it_is_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Self-hosted without a trusted proxy: X-Forwarded-For is the caller's to write, so ignored.
    client, _ = signing_in(tmp_path / "a", monkeypatch, "on,signin=1/min")
    assert logins(client, ["192.0.2.1", "192.0.2.2"]) == [302, 429]
    # On Vercel, the platform sets it.
    monkeypatch.setenv("VERCEL", "1")
    client, _ = signing_in(tmp_path / "b", monkeypatch, "on,signin=1/min")
    assert logins(client, ["192.0.2.1", "192.0.2.2", "192.0.2.1"]) == [302, 302, 429]
    assert logins(client, ["not an address"]) == [302]  # one shared bucket for junk
    assert logins(client, ["also junk"]) == [429]
    monkeypatch.delenv("VERCEL")
    # Behind uvicorn's proxy handling: only from the proxy it trusts.
    proxied, _ = signing_in(
        tmp_path / "c", monkeypatch, "on,signin=1/min", address="198.51.100.1", wrap=True
    )
    assert logins(proxied, ["192.0.2.1", "192.0.2.2"]) == [302, 302]
    stranger, _ = signing_in(
        tmp_path / "d", monkeypatch, "on,signin=1/min", address="203.0.113.9", wrap=True
    )
    assert logins(stranger, ["192.0.2.1", "192.0.2.2"]) == [302, 429]


def test_bucket_keys_hold_no_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = signing_in(tmp_path, monkeypatch, "on")
    db = str(tmp_path / "signin.db")
    key = service_key(db)
    session = person(db, "reviewer")
    smart = "smart-session-cookie-value-0123456789"
    client.get("/auth/login", follow_redirects=False)
    client.post(DECIDE, json={}, headers=key)
    client.post(DECIDE, json={}, headers=session)
    client.post("/api/smart/decide", json={}, headers={"cookie": f"mendari_smart={smart}"})
    found = buckets(db)
    secrets = [
        key["authorization"].split()[1],
        session["cookie"].split("=", 1)[1],
        smart,
    ]
    assert not [s for s in secrets if any(s in b or s[4:20] in b for b in found)]
    patterns = [
        r"signin\|ip:203\.0\.113\.5",
        r"decide\|key:[0-9a-f-]+",
        r"decide\|user:[0-9a-f]{16}",
        r"decide\|smart:[0-9a-f]{16}",
    ]
    assert len(found) == len(patterns)
    for pattern in patterns:
        assert any(re.fullmatch(pattern, b) for b in found), (pattern, found)


# -- the token endpoint ---------------------------------------------------------------------------


@pytest.fixture
def limited_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    backend_on(monkeypatch)
    monkeypatch.setenv("MENDARI_RATE_LIMITS", "on,token_failures=2/10min,token=6/min")
    db = str(tmp_path / "token.db")
    with TestClient(create_app(db_path=db), base_url=PUBLIC_URL) as http:
        yield Server(http, db)


def test_failed_assertions_are_limited_per_address_and_client(limited_server: Server) -> None:
    server = limited_server
    client_id, other = server.register(), server.register()
    for _ in range(2):  # signed by a key the client did not register
        assert ask(server.http, assertion(STRANGER, client_id)).status_code == 401
    refused = ask(server.http, assertion(RSA_KEY, client_id))  # valid, but too many failures
    assert assert_retry_after(refused) > 60
    assert refused.json() == {
        "error": "temporarily_unavailable",
        "error_description": "too many token requests: retry later",
    }
    assert refused.headers["pragma"] == "no-cache"
    # Never the client alone: from here, another client is not affected.
    assert ask(server.http, assertion(RSA_KEY, other)).status_code == 200
    assert server.rows("oauth_tokens") == 1


def test_the_token_endpoint_is_limited_per_address_before_its_body(
    limited_server: Server,
) -> None:
    server = limited_server
    answers = [server.http.post("/auth/token").status_code for _ in range(6)]
    assert answers == [400] * 6
    refused = server.http.post("/auth/token", data={"grant_type": "client_credentials"})
    assert assert_retry_after(refused) and refused.json()["error"] == "temporarily_unavailable"


# -- every route ----------------------------------------------------------------------------------


EXPECTED_CLASSES = {
    ("GET", "/auth/login"): "signin",
    ("GET", "/smart/launch"): "signin",
    ("POST", "/auth/token"): "token",
    ("POST", "/api/smart/decide"): "decide",
    ("POST", "/api/packs/{namespace}/{name}/decide"): "decide",
    ("POST", "/api/packs/{namespace}/{name}/fhir/decide"): "decide",
    ("POST", "/api/packs/{namespace}/{name}/pas"): "decide",
    ("POST", "/cds-services/{service_id}"): "decide",
    ("POST", "/fhir/$decide"): "decide",
    ("POST", "/fhir/Questionnaire/$questionnaire-package"): "decide",
    ("POST", "/api/packs/{namespace}/{name}/guard"): "guard",
    ("POST", "/api/packs/{namespace}/{name}/evals"): "evaluate",
    ("GET", "/api/audit/export/fhir"): "export",
    ("GET", "/api/packs/{namespace}/{name}/feedback/export"): "export",
}


def test_costly_routes_have_their_class_reads_none_and_one_call_of_each_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every route the app serves (Backend Services on): the class it counts against, from its
    permission and path, and with the default limits on, one call of each as an admin (the
    route matrix's other half: limits never change who may call what) is never a 429."""
    backend_on(monkeypatch)
    client, _ = signing_in(tmp_path, monkeypatch, "on")
    limiter = Limiter(AuditStore(tmp_path / "classes.db"), limit_settings(sign_in=True))
    routes = sorted(
        (method, route.path)
        for route in client.app.routes  # type: ignore[attr-defined]
        if isinstance(route, Route)
        for method in route.methods or ()
        if method != "HEAD"
    )
    classes = {r: limiter.classify(*r, permission_for(*r)) for r in routes}
    assert {r: c for r, c in classes.items() if c is not None} == EXPECTED_CLASSES
    admin = person(str(tmp_path / "signin.db"))
    for method, path in routes:
        url = re.sub(r"\{[^}]+\}", "x", path)
        answer = client.request(method, url, headers=admin, follow_redirects=False)
        assert answer.status_code != 429, (method, path)


# -- organization quotas --------------------------------------------------------------------------


def test_each_organization_has_its_own_quota_and_evaluations_spend_none(
    store: AuditStore,
) -> None:
    reached: list[tuple[str, str, int]] = []
    budget = CallBudget(
        store,
        {"jev": None},
        org_limits={"decisions": 2, "jev": 1},
        on_quota_reached=lambda *args: reached.append(args),
    )
    with tenancy.acting_for("north"):
        budget.admit_decision()
        budget.admit_decision()
        with pytest.raises(ProviderError, match="quota_exhausted"):
            budget.admit_decision()
        budget.acquire("jev")
        with pytest.raises(ProviderError, match="quota_exhausted"):
            budget.acquire("jev")
        evaluation = budget.for_evaluation()
        evaluation.admit_decision()  # no quota: an evaluation is not the organization's work
        evaluation.acquire("jev")  # counted in the deployment's budget only
    with tenancy.acting_for("south"):
        budget.admit_decision()
    assert store.org_usage("north") == {"decisions": 2, "jev": 1}
    assert store.org_usage("south") == {"decisions": 1}
    assert store.usage("jev") == 2
    assert reached == [("north", "decisions", 2), ("north", "jev", 1)]  # once each, when reached


def test_over_its_quota_a_decision_falls_back_to_human_review_with_one_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_ORG_DAILY_LIMITS", "decisions=2")
    monkeypatch.setenv("MENDARI_ALERT_WEBHOOK", "https://alerts.example.org/hook")
    posted: list[dict[str, Any]] = []

    def post(url: str, **kwargs: Any) -> httpx.Response:
        posted.append({"url": url, **kwargs})
        return httpx.Response(204, request=httpx.Request("POST", url))

    monkeypatch.setattr(app_module.httpx, "post", post)
    db = str(tmp_path / "quota.db")
    client = TestClient(create_app(db_path=db))
    # Evaluations run on the deployment's budgets, never the organization's quota.
    run = client.post(f"/api/packs/{INBOX}/evals", json={"dataset": "test", "limit": 5})
    assert run.status_code == 200, run.text
    outcomes = [client.post(DECIDE, json=message(ROUTINE)).json() for _ in range(4)]
    assert [o["outcome"] for o in outcomes[:2]] == ["AUTO", "AUTO"]
    for over in outcomes[2:]:  # never automated: a person decides
        assert over["outcome"] in ("HUMAN_REVIEW", "ABSTAIN")
        assert set(over["errors"].values()) == {"quota_exhausted"}
    assert [p["url"] for p in posted] == ["https://alerts.example.org/hook"]
    alert = posted[0]["json"]
    assert alert["organization"] == "default" and alert["alerts"][0]["kind"] == "quota_reached"
    assert ROUTINE not in str(alert)
    health = client.get("/api/health").json()
    assert health["org_quotas"] == {
        "daily_limits": {"decisions": 2},
        "used_today": {"decisions": 2},
    }
    with AuditStore(db) as store:
        assert {d["outcome"] for d in store.pending_reviews(None, limit=10)} <= {
            "HUMAN_REVIEW",
            "ABSTAIN",
        }
        assert len(store.pending_reviews(None, limit=10)) == 2


def test_a_quota_alert_that_fails_never_fails_the_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("MENDARI_ORG_DAILY_LIMITS", "decisions=1")
    monkeypatch.setenv("MENDARI_ALERT_WEBHOOK", "https://alerts.example.org/hook")

    def down(url: str, **_: Any) -> httpx.Response:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(app_module.httpx, "post", down)
    client = TestClient(create_app(db_path=tmp_path / "down.db"))
    with caplog.at_level(logging.WARNING, logger="mendari.limits"):
        assert client.post(DECIDE, json=message(ROUTINE)).json()["outcome"] == "AUTO"
    assert "webhook failed (ConnectError)" in caplog.text


# -- health and serving ---------------------------------------------------------------------------


def test_health_shows_the_limits_to_someone_signed_in_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in_on(monkeypatch, "on,decide=1200/min:200")
    db = str(tmp_path / "health.db")
    client = TestClient(create_app(db_path=db))
    assert "rate_limits" not in client.get("/api/health").json()
    shown = client.get("/api/health", headers=person(db, "viewer")).json()
    assert shown["rate_limits"]["mode"] == "on"
    assert shown["rate_limits"]["limits"]["decide"] == "1200/min:200"
    assert shown["rate_limits"]["client_ip_source"].startswith("the connection")
    assert shown["org_quotas"] == {"daily_limits": {}, "used_today": {}}


def test_mendari_serve_trusts_forwarded_addresses_only_from_the_proxies_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: dict[str, Any] = {}
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "restored after the test")  # serve sets it
    monkeypatch.delenv("FORWARDED_ALLOW_IPS")
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: ran.update(kwargs))
    db = str(tmp_path / "serve.db")
    assert cli.main(["serve", "--db", db]) == 0
    assert ran["proxy_headers"] is True and ran["forwarded_allow_ips"] == "127.0.0.1"
    assert client_ip_source() == "x-forwarded-for from 127.0.0.1, else the connection"
    cli.main(["serve", "--db", db, "--forwarded-allow-ips", "10.0.0.0/8"])
    assert ran["forwarded_allow_ips"] == "10.0.0.0/8"
    assert client_ip_source() == "x-forwarded-for from 10.0.0.0/8, else the connection"
    cli.main(["serve", "--db", db, "--no-proxy-headers"])
    assert ran["proxy_headers"] is False
    assert client_ip_source() == "the connection (x-forwarded-for not trusted)"
    monkeypatch.setenv("VERCEL", "1")
    assert client_ip_source() == "x-forwarded-for (set by Vercel)"


def test_the_api_reference_says_what_is_limited_only_when_limits_are_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in_on(monkeypatch, "on")
    db = str(tmp_path / "docs.db")
    schema = TestClient(create_app(db_path=db)).get("/openapi.json", headers=person(db)).json()
    paths = schema["paths"]
    assert (
        "Retry-After"
        in paths["/api/packs/{namespace}/{name}/decide"]["post"]["responses"]["429"]["headers"]
    )
    assert "429" in paths["/api/audit/export/fhir"]["get"]["responses"]
    assert "429" not in paths["/api/packs"]["get"]["responses"]  # reads are not limited here
    assert "**Rate limits.**" in schema["info"]["description"]
    assert "decisions 600/min:100" in schema["info"]["description"]
    monkeypatch.setenv("MENDARI_RATE_LIMITS", "log")
    db = str(tmp_path / "log.db")
    schema = TestClient(create_app(db_path=db)).get("/openapi.json", headers=person(db)).json()
    assert "Rate limits" not in schema["info"]["description"]
    assert "429" not in schema["paths"]["/api/packs/{namespace}/{name}/decide"]["post"]["responses"]


def test_the_firewall_rule_starts_in_log_mode_on_signed_out_routes_only() -> None:
    """The edge layer (deploy/vercel/firewall-rules.json): per address, only where nobody has
    signed in yet, above the app's own limits so the app answers ordinary bursts, no
    persistent block, and in log mode until the owner enforces it."""
    rules = json.loads((ROOT / "deploy" / "vercel" / "firewall-rules.json").read_text())["rules"]
    assert len(rules) == 1  # Hobby allows one rate-limit rule
    mitigate = rules[0]["action"]["mitigate"]
    rate = mitigate["rateLimit"]
    assert mitigate["action"] == "rate_limit" and mitigate["actionDuration"] is None
    assert rate["action"] == "log" and rate["keys"] == ["ip"] and rate["window"] == 60
    assert rate["limit"] >= DEFAULT_RATES["signin"].count + DEFAULT_RATES["token"].count
    paths = {
        (c["type"], c["op"], c["value"])
        for g in rules[0]["conditionGroup"]
        for c in g["conditions"]
    }
    assert paths == {("path", "pre", "/auth/"), ("path", "pre", "/smart/")}
