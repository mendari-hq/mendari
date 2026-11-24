# SPDX-License-Identifier: Apache-2.0
"""What a caller without a credential can make the server do (Phase 4b, stage A): the
capability statement takes no lock, sign-in fetches the identity provider's configuration once
an hour and purges a bounded number of rows, public health is three fields and no database
work, and no request body passes MENDARI_MAX_BODY_BYTES."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from mendari.core import audit as audit_module
from mendari.core import db as db_module
from mendari.core.audit import _CHAIN_LOCK, SESSION_PURGE_BATCH, AuditStore
from mendari.errors import FhirError, MendariError
from mendari.server.app import create_app
from mendari.server.auth import Auth, AuthSettings, Principal, person_id
from mendari.server.scopes import DECIDE
from mendari.standards import oidc
from mendari.standards.fhir_client import GuardedHttp
from tests.conftest import message, run_async, without_request_id
from tests.test_auth import IDP, FakeIdp, key_for, sign_in, signing_in_with

FIXTURE = Path(__file__).parent / "fixtures" / "us-core" / "inbox.json"
INBOX = "/api/agents/mendari/patient-inbox-router"
PUBLIC_HEALTH = {"status", "version", "sign_in"}
SMALL = 64 * 1024  # the smallest cap a deployment may set


@pytest.fixture
def sessions(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Every database session opened from here on, as its `exclusive` flag."""
    opened: list[bool] = []
    for backend in (db_module.SqliteBackend, db_module.PostgresBackend):

        def spy(self: Any, *, exclusive: bool, _original: Any = backend.session) -> Any:
            opened.append(exclusive)
            return _original(self, exclusive=exclusive)

        monkeypatch.setattr(backend, "session", spy)
    return opened


@pytest.fixture
def signing_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with signing_in_with(FakeIdp(), tmp_path, monkeypatch) as client:
        yield client


# -- the capability statement --------------------------------------------------------------


def test_the_capability_statement_never_takes_the_chain_lock(
    db_target: str, sessions: list[bool]
) -> None:
    first = TestClient(create_app(db_path=db_target))
    date = first.get("/fhir/metadata").json()["date"]  # records this version, once
    sessions.clear()
    for _ in range(10):
        assert first.get("/fhir/metadata").json()["date"] == date
    assert sessions == []  # this process remembers it
    another = TestClient(create_app(db_path=db_target))  # another instance, the same version
    sessions.clear()
    assert another.get("/fhir/metadata").json()["date"] == date
    assert sessions == [False]  # one shared read, no lock


def test_a_new_version_is_recorded_once_and_a_later_one_gets_a_new_time(db_target: str) -> None:
    with AuditStore(db_target) as one, AuditStore(db_target) as other:
        since = one.capability_since("local", "v1")
        assert other.capability_since("local", "v1") == since  # read, not recorded again
        later = other.capability_since("local", "v2")
        assert later > since
        assert one.capability_since("local", "v2") == later
        assert other.capability_since("local", "v1") > later  # served again: a new time


def test_a_new_version_is_later_even_when_the_clock_stands_still_or_goes_back(
    db_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Versions recorded within one millisecond, or by an instance whose clock is behind, still
    get times in order: the statement's `date` never repeats or goes back."""
    clock = ["2026-10-09T12:00:00.000+00:00"]
    monkeypatch.setattr(audit_module, "_now", lambda: clock[0])
    with AuditStore(db_target) as one, AuditStore(db_target) as other:
        times = [
            one.capability_since("local", "v1"),
            other.capability_since("local", "v2"),
            other.capability_since("local", "v1"),
        ]
        clock[0] = "2026-10-09T11:59:59.000+00:00"
        times.append(one.capability_since("local", "v3"))
        assert other.capability_since("local", "v3") == times[-1]  # read, not recorded again
    assert times == [f"2026-10-09T12:00:00.00{n}+00:00" for n in range(4)]


def test_the_capability_statement_answers_while_the_chain_is_locked(db_target: str) -> None:
    """On Postgres a decision appends under an advisory lock; the public statement must not
    queue behind it (or behind a caller who holds it)."""
    if not db_target.startswith("postgres"):
        pytest.skip("the chain's lock is a Postgres advisory lock")
    import psycopg

    TestClient(create_app(db_path=db_target)).get("/fhir/metadata")  # the version is recorded
    client = TestClient(create_app(db_path=db_target))
    answered: list[int] = []
    with psycopg.connect(db_target, autocommit=True) as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (_CHAIN_LOCK,))
        call = threading.Thread(
            target=lambda: answered.append(client.get("/fhir/metadata").status_code)
        )
        call.start()
        call.join(timeout=1)
        in_time = list(answered)
        holder.execute("SELECT pg_advisory_unlock(%s)", (_CHAIN_LOCK,))
        call.join()
    assert in_time == [200]


# -- sign-in -------------------------------------------------------------------------------


def test_ten_sign_ins_fetch_the_identity_providers_configuration_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idp = FakeIdp()
    with signing_in_with(idp, tmp_path, monkeypatch) as client:
        for _ in range(10):
            assert sign_in(client, idp, roles=["viewer"]).status_code == 302
    assert idp.discovered == 1


def test_discovery_is_kept_for_its_time_checked_on_each_use_and_never_kept_failed() -> None:
    idp = FakeIdp()
    transport = httpx.MockTransport(idp)
    allowed = GuardedHttp(transport=transport, allowed={IDP})
    discovery = oidc.Discovery()
    config = discovery.get(IDP, allowed)
    assert discovery.get(IDP, allowed) == config and idp.discovered == 1
    # Another caller's allow-list still applies to the configuration kept.
    with pytest.raises(FhirError, match="allow-list"):
        discovery.get(IDP, GuardedHttp(transport=transport, allowed={"https://other.example"}))
    expired = oidc.Discovery(ttl=0)
    expired.get(IDP, allowed)
    expired.get(IDP, allowed)
    assert idp.discovered == 3
    failing = oidc.Discovery()
    down = GuardedHttp(transport=httpx.MockTransport(lambda r: httpx.Response(503)), allowed={IDP})
    with pytest.raises(FhirError):
        failing.get(IDP, down)
    failing.get(IDP, allowed)  # the failure was not kept
    assert idp.discovered == 4


def test_a_put_purges_at_most_a_batch_of_expired_rows(store: AuditStore) -> None:
    """A flood of anonymous sign-ins leaves expired pending rows; a later put deletes a bounded
    number of them, on the expiry index, never all of them at once."""
    with store._db.session(exclusive=False) as db:
        db.all(
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 5000) "
            "INSERT INTO smart_sessions (key_sha256, kind, expires_at, data) "
            "SELECT 'expired-' || i, 'oidc-pending', 1, '{}' FROM n"
        )
        if store.backend == "sqlite":
            index = "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?"
        else:
            index = "SELECT indexname FROM pg_indexes WHERE indexname = ?"
        assert db.one(index, ("smart_sessions_expiry",)) is not None
    store.smart_put("fresh", "user", {"name": "x"}, int(time.time()) + 60)
    with store._db.session(exclusive=False) as db:
        left = db.one("SELECT COUNT(*) AS n FROM smart_sessions WHERE expires_at <= 1")
    assert SESSION_PURGE_BATCH == 100
    assert left == {"n": 5000 - SESSION_PURGE_BATCH}
    assert store.smart_get("fresh", "user") == {"name": "x"}


def test_the_sign_in_check_runs_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A credential lookup is a database read: the event loop must not wait on it."""
    on_the_loop: list[bool] = []
    check = Auth.check

    def spy(self: Auth, request: Request) -> Any:
        try:
            asyncio.get_running_loop()
            on_the_loop.append(True)
        except RuntimeError:
            on_the_loop.append(False)
        return check(self, request)

    monkeypatch.setattr(Auth, "check", spy)
    assert TestClient(create_app(db_path=tmp_path / "a.db")).get("/api/agents").status_code == 200
    assert on_the_loop == [False]


# -- principals ------------------------------------------------------------------------------


def test_each_principal_has_a_stable_id_that_names_nobody(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Principal | None] = []
    authenticate = Auth.authenticate

    def spy(self: Auth, request: Request) -> Principal | None:
        seen.append(authenticate(self, request))
        return seen[-1]

    monkeypatch.setattr(Auth, "authenticate", spy)
    idp = FakeIdp()
    signing_in = signing_in_with(idp, tmp_path, monkeypatch)
    sign_in(signing_in, idp, roles=["viewer"], name="Nurse Kim", email="kim@example.org")
    signing_in.get("/api/audit")
    person = seen[-1]
    assert person is not None and person.id == person_id(IDP, "u1")
    assert person.id.startswith("user:") and len(person.id) == len("user:") + 16
    assert "Kim" not in person.id and "example" not in person.id
    sign_in(signing_in, idp, roles=["admin"])
    created = signing_in.post("/api/auth/keys", json={"name": "ehr", "role": "service"}).json()
    signing_in.cookies.clear()
    signing_in.get("/api/audit", headers={"authorization": f"Bearer {created['key']}"})
    key = seen[-1]
    assert key is not None and key.id == f"key:{created['id']}"


def test_a_session_from_before_the_issuer_was_stored_is_of_the_configured_issuer(
    tmp_path: Path,
) -> None:
    settings = AuthSettings(IDP, "c", None, "roles", {}, None, None)
    with AuditStore(tmp_path / "a.db") as store:
        old = {"sub": "u1", "name": "Nurse Kim", "roles": ["viewer"], "org": "default"}
        store.smart_put("sid", "user", old, int(time.time()) + 60)
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"cookie", b"mendari_session=sid")],
        }
        principal = Auth(settings, store).authenticate(Request(scope))
    assert principal is not None and principal.id == person_id(IDP, "u1")


def test_an_id_token_without_a_subject_signs_nobody_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idp = FakeIdp()
    with signing_in_with(idp, tmp_path, monkeypatch) as client:
        refused = sign_in(client, idp, roles=["viewer"], sub="")
        assert refused.status_code == 400 and "no subject" in refused.text
        assert "set-cookie" not in refused.headers


def test_a_principal_needs_an_id() -> None:
    with pytest.raises(TypeError):
        Principal(kind="user", name="x")  # type: ignore[call-arg]


# -- health ----------------------------------------------------------------------------------


def test_anonymous_health_is_three_fields_and_no_database_work(
    signing_in: TestClient, sessions: list[bool]
) -> None:
    health = signing_in.get("/api/health")
    assert health.status_code == 200
    assert set(health.json()) == PUBLIC_HEALTH and health.json()["sign_in"] is True
    assert sessions == []
    junk = signing_in.get("/api/health", headers={"authorization": "Bearer ohk_unknown"})
    assert set(junk.json()) == PUBLIC_HEALTH  # a credential that is not valid: the same


def test_the_settings_in_health_need_the_read_permission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idp = FakeIdp()
    with signing_in_with(idp, tmp_path, monkeypatch) as client:
        sign_in(client, idp, roles=["viewer"])
        details = client.get("/api/health").json()
        assert {"phi_mode", "packs", "providers", "jev", "llm"} <= set(details)
        assert details["sign_in"] is True and details["phi_mode"] == "encrypt"  # never off
        viewer = {"authorization": f"Bearer {key_for(client, idp, role='viewer')}"}
        assert "phi_mode" in client.get("/api/health", headers=viewer).json()
        sign_in(client, idp, roles=["admin"])
        body = {"name": "decides only", "role": "service", "scope": DECIDE}
        narrow = client.post("/api/auth/keys", json=body).json()["key"]
        client.cookies.clear()
        decides = client.get("/api/health", headers={"authorization": f"Bearer {narrow}"})
        assert set(decides.json()) == PUBLIC_HEALTH  # may decide, may not read


def test_open_mode_health_shows_everyone_the_settings(tmp_path: Path) -> None:
    health = TestClient(create_app(db_path=tmp_path / "a.db")).get("/api/health").json()
    assert health["sign_in"] is False and health["phi_mode"] == "off" and "providers" in health


# -- request bodies --------------------------------------------------------------------------


def asgi_post(app: Any, path: str, headers: dict[str, str], body: bytes) -> tuple[int, int]:
    """POST `body` straight through ASGI: (the status, how many body bytes the app took)."""
    left = [body]
    taken = 0

    async def receive() -> dict[str, Any]:
        nonlocal taken
        if left:
            chunk = left.pop()
            taken += len(chunk)
            return {"type": "http.request", "body": chunk, "more_body": False}
        return {"type": "http.disconnect"}

    sent: list[dict[str, Any]] = []

    async def send(event: dict[str, Any]) -> None:
        sent.append(event)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
        "server": ("mendari.test", 443),
        "client": ("127.0.0.1", 50000),
    }
    run_async(app(scope, receive, send))
    return next(e["status"] for e in sent if e["type"] == "http.response.start"), taken


@pytest.fixture
def capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("MENDARI_MAX_BODY_BYTES", str(SMALL))
    return create_app(db_path=tmp_path / "a.db")


def test_a_declared_body_over_the_cap_is_refused_unread(capped: Any) -> None:
    declared = {"content-type": "application/json", "content-length": str(SMALL + 1)}
    assert asgi_post(capped, f"{INBOX}/decide", declared, b"x" * (SMALL + 1)) == (413, 0)
    fits = {**declared, "content-length": str(SMALL)}
    status, taken = asgi_post(capped, f"{INBOX}/decide", fits, b"{" + b" " * (SMALL - 2) + b"}")
    assert (status, taken) == (200, SMALL)  # read and decided (a BLOCK: no message in it)


def test_sign_in_answers_before_the_cap_and_reads_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_MAX_BODY_BYTES", str(SMALL))
    with signing_in_with(FakeIdp(), tmp_path, monkeypatch) as client:
        app = client.app
    headers = {"content-type": "application/json", "content-length": str(SMALL * 4)}
    assert asgi_post(app, f"{INBOX}/decide", headers, b"x" * SMALL * 4) == (401, 0)
    headers["content-length"] = str(SMALL)
    assert asgi_post(app, f"{INBOX}/decide", headers, b"x" * SMALL) == (401, 0)


def test_a_chunked_body_is_refused_once_it_passes_the_cap(capped: Any) -> None:
    client = TestClient(capped)
    chunks = [b'{"message": {"body": "', *[b"x" * 8192] * 10, b'"}}']  # no declared length
    refused = client.post(f"{INBOX}/decide", content=iter(chunks))
    assert refused.status_code == 413
    assert without_request_id(refused.json(), refused) == {
        "detail": "a request body is 65536 bytes at most"
    }
    assert client.get("/api/audit").json() == []  # nothing was decided
    fhir = {"content-type": "application/fhir+json"}
    for content in (b" " * (SMALL + 1), iter([b" " * SMALL, b" "])):  # declared, then chunked
        outcome = client.post("/fhir/$decide", content=content, headers=fhir)
        assert outcome.status_code == 413
        assert outcome.headers["content-type"].startswith("application/fhir+json")
        assert outcome.json()["issue"][0]["code"] == "too-costly"
        assert outcome.headers["content-security-policy"]  # the guard's headers too


def test_a_three_mebibyte_bundle_is_taken_by_default(tmp_path: Path) -> None:
    bundle = json.loads(FIXTURE.read_text(encoding="utf-8"))
    padding = {"resourceType": "Basic", "code": {"text": "x" * 1000}}
    bundle["entry"] += [
        {"fullUrl": f"urn:uuid:pad-{i}", "resource": {**padding, "id": f"pad-{i}"}}
        for i in range(3100)
    ]
    body = json.dumps(bundle).encode()
    assert 3 * 2**20 < len(body) < 4 * 2**20
    client = TestClient(create_app(db_path=tmp_path / "a.db"))
    decided = client.post(
        f"{INBOX}/fhir/decide", content=body, headers={"content-type": "application/fhir+json"}
    )
    assert decided.status_code == 200, decided.text[:200]


@pytest.mark.parametrize("value", ["4", "4MiB", "-1", str(SMALL - 1)])
def test_a_cap_that_is_not_a_number_of_bytes_fails_at_startup(
    value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_MAX_BODY_BYTES", value)
    with pytest.raises(MendariError, match="MENDARI_MAX_BODY_BYTES"):
        create_app(db_path=tmp_path / "a.db")


def test_decisions_still_work_under_the_cap(capped: Any) -> None:
    decided = TestClient(capped).post(f"{INBOX}/decide", json=message("refill please"))
    assert decided.status_code == 200
