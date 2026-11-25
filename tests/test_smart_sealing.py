# SPDX-License-Identifier: Apache-2.0
"""An EHR launch's rows sealed at rest (core/session_seal.py, ADR-0006, threat I11): the session
row (the EHR's access token, the patient) under the session cookie, the pending launch (the PKCE
verifier, the nonce) under the launch's binding cookie. A database reader opens neither; a row
that does not open (another row's data, a changed byte, plaintext from before sealing) ends the
launch; an EHR that refuses the token ends it too. People's sign-in rows are left as they were.
Both backends (`db_target` runs on SQLite and Postgres)."""

from __future__ import annotations

import functools
import json
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from mendari.core import audit
from mendari.core.audit import AuditStore
from mendari.core.session_seal import SealError, seal, unseal
from mendari.server import auth as auth_module
from mendari.server import sessions, smart_routes
from mendari.server.app import create_app
from mendari.standards.fhir_client import FhirClient, GuardedHttp
from tests.conftest import DASHBOARD, encrypt_mode, signed_in
from tests.test_auth import IDP, FakeIdp, sign_in
from tests.test_security_log import logged
from tests.test_smart import APP, CLIENT, EHR, ISS, REFERRAL, FakeEhr, full_launch, launch

SMART_COOKIE = f"__Host-{sessions.SMART}"
LAUNCH_COOKIE = f"__Host-{sessions.LAUNCH}"


@pytest.fixture
def ehr() -> FakeEhr:
    return FakeEhr()


def smart_app(db_target: str, ehr: FakeEhr, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("MENDARI_SMART_CLIENT_ID", CLIENT)
    monkeypatch.setenv("MENDARI_PUBLIC_URL", APP)
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", EHR)
    transport = httpx.MockTransport(ehr)
    monkeypatch.setattr(
        smart_routes, "GuardedHttp", functools.partial(GuardedHttp, transport=transport)
    )
    monkeypatch.setattr(
        smart_routes, "FhirClient", functools.partial(FhirClient, transport=transport)
    )
    return TestClient(create_app(db_path=db_target), base_url=APP, headers=DASHBOARD)  # a browser


@pytest.fixture
def app(db_target: str, ehr: FakeEhr, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with smart_app(db_target, ehr, monkeypatch) as client:
        yield client


def raw_rows(db_target: str, kind: str) -> list[dict[str, Any]]:
    """The rows of `kind` as the database holds them: what a reader of the table, a backup or
    the WAL sees."""
    with AuditStore(db_target) as store, store._db.session(exclusive=False) as db:
        rows = db.all("SELECT key_sha256, data FROM smart_sessions WHERE kind = ?", (kind,))
    return [dict(row) for row in rows]


def set_data(db_target: str, key_sha256: str, data: str) -> None:
    with AuditStore(db_target) as store, store._db.session(exclusive=False) as db:
        db.all("UPDATE smart_sessions SET data = ? WHERE key_sha256 = ?", (data, key_sha256))


def assert_sealed(data: str) -> None:
    stored = json.loads(data)
    assert list(stored) == ["sealed"] and stored["sealed"].startswith("s1:")


def test_a_launch_session_row_holds_nothing_readable(
    app: TestClient, ehr: FakeEhr, db_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    issued = ehr.token

    def with_refresh(form: dict[str, list[str]]) -> httpx.Response:
        body = issued(form).json()  # an EHR that sends a refresh token unasked
        return httpx.Response(200, json={**body, "refresh_token": "refresh-secret-1"})

    monkeypatch.setattr(ehr, "token", with_refresh)
    assert full_launch(app, ehr).status_code == 302
    [row] = raw_rows(db_target, "session")
    assert_sealed(row["data"])
    readables = ("access-0", "ref-p1", "enc-1", "Practitioner", "ehr.example.org", "refresh")
    for readable in (*readables, "clinician-1"):  # who launched is sealed too (ADR-0006)
        assert readable not in row["data"], readable
    # The session works, and what it keeps opens only with the browser's cookie.
    assert app.get("/api/smart/session").json()["patient"] == "ref-p1"
    assert app.get("/api/smart/patient").status_code == 200
    sid = app.cookies.get(SMART_COOKIE)
    assert sid
    with AuditStore(db_target) as store:
        kept = store.smart_get(sid, "session")
    assert kept is not None and kept["access_token"] == "access-0"
    assert "refresh_token" not in kept and "id_token" not in kept
    assert set(kept) == {
        *("iss", "access_token", "scope", "patient", "encounter", "fhir_user", "expires_at"),
        *("pack", "launch_id", "absolute_at", "identity", "actor"),
    }
    assert kept["identity"] == {"iss": ISS, "sub": "clinician-1"}  # no email kept


def test_a_pending_launch_row_holds_no_readable_verifier_or_state(
    app: TestClient, ehr: FakeEhr, db_target: str
) -> None:
    started = launch(app)
    params = {k: v[0] for k, v in parse_qs(urlsplit(started.headers["location"]).query).items()}
    [row] = raw_rows(db_target, "pending")
    assert_sealed(row["data"])
    for readable in (params["state"], params["nonce"], ISS, "/smart/callback", "verifier"):
        assert readable not in row["data"], readable
    # Sealed under the binding cookie, which only this browser holds; never under the state,
    # which travels in URLs (the EHR's logs, the browser's history).
    stored = json.loads(row["data"])
    with pytest.raises(SealError):
        unseal(stored, secret=params["state"], kind="pending", row_key=row["key_sha256"])
    binding = app.cookies.get(LAUNCH_COOKIE)
    assert binding
    pending = unseal(stored, secret=binding, kind="pending", row_key=row["key_sha256"])
    assert "verifier" in pending and "state" not in pending and "browser" not in pending
    code = ehr.authorize(started.headers["location"])
    done = app.get("/smart/callback", params={"code": code, "state": params["state"]})
    assert done.status_code == 200 and app.get("/api/smart/session").json()["active"] is True


def test_a_sealed_row_opens_only_in_its_own_row_and_kind(store: AuditStore, db_target: str) -> None:
    """Swapping sealed values between two sessions, or between a session and a pending launch
    under the same secret, opens neither: each is bound to its row and kind."""
    later = int(time.time()) + 600
    store.smart_put("sid-a", "session", {"patient": "a"}, later)
    store.smart_put("sid-b", "session", {"patient": "b"}, later)
    store.smart_put("same", "session", {"patient": "s"}, later)
    store.smart_put("same", "pending", {"verifier": "v"}, later, secret="same")
    data = {
        row["key_sha256"]: row["data"]
        for kind in ("session", "pending")
        for row in raw_rows(db_target, kind)
    }
    key = audit._session_key
    swaps = [
        (key("session", "sid-a"), key("session", "sid-b")),
        (key("session", "same"), key("pending", "same")),
    ]
    for one, other in swaps:
        set_data(db_target, one, data[other])
        set_data(db_target, other, data[one])
    assert store.smart_get("sid-a", "session") is None
    assert store.smart_get("sid-b", "session") is None
    assert store.smart_get("same", "session") is None
    with pytest.raises(SealError):
        store.smart_take("same", "pending", secret="same")
    assert raw_rows(db_target, "session") == [] and raw_rows(db_target, "pending") == []


def test_a_tampered_or_plaintext_launch_row_ends_the_launch(
    app: TestClient, ehr: FakeEhr, db_target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # A changed byte.
    full_launch(app, ehr)
    [row] = raw_rows(db_target, "session")
    token = json.loads(row["data"])["sealed"]
    # A character well inside the tag: near the end, a base64 character may carry only padding
    # bits, and changing those changes no byte.
    flipped = token[:-6] + ("A" if token[-6] != "A" else "B") + token[-5:]
    set_data(db_target, row["key_sha256"], json.dumps({"sealed": flipped}))
    assert app.get("/api/smart/patient").status_code == 401
    assert app.get("/api/smart/session").json() == {"active": False}
    assert raw_rows(db_target, "session") == []  # deleted, not left to expire
    # A plaintext row, as a release before sealing wrote it, or as a database writer would.
    full_launch(app, ehr)
    [row] = raw_rows(db_target, "session")
    sid = app.cookies.get(SMART_COOKIE)
    assert sid
    with AuditStore(db_target) as store:
        clear = store.smart_get(sid, "session")
    assert clear is not None
    set_data(db_target, row["key_sha256"], json.dumps({**clear, "patient": "other-p9"}))
    ehr.requests.clear()
    assert app.get("/api/smart/patient").status_code == 401
    assert ehr.requests == []  # the EHR was never asked for the other patient
    assert raw_rows(db_target, "session") == []
    # A pending launch in plaintext (in flight at the upgrade): the browser launches again.
    started = launch(app)
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    code = ehr.authorize(started.headers["location"])
    [row] = raw_rows(db_target, "pending")
    binding = app.cookies.get(LAUNCH_COOKIE)
    assert binding
    opened = unseal(
        json.loads(row["data"]), secret=binding, kind="pending", row_key=row["key_sha256"]
    )
    set_data(db_target, row["key_sha256"], json.dumps({**opened, "state": state}))
    capsys.readouterr()
    refused = app.get("/smart/callback", params={"code": code, "state": state})
    assert refused.status_code == 400 and "start it again" in refused.text
    assert [e.get("reason") for e in logged(capsys) if e["event"] == "smart.launch.refused"] == [
        "browser"
    ]
    assert raw_rows(db_target, "pending") == [] and raw_rows(db_target, "session") == []


def test_a_launch_ends_at_its_sealed_absolute_limit(
    app: TestClient, ehr: FakeEhr, db_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database writer can move a row's clear `expires_at`, never the sealed `absolute_at`."""
    full_launch(app, ehr)
    [row] = raw_rows(db_target, "session")
    with AuditStore(db_target) as store, store._db.session(exclusive=False) as db:
        db.all(
            "UPDATE smart_sessions SET expires_at = ? WHERE key_sha256 = ?",
            (int(time.time()) + 30 * 24 * 3600, row["key_sha256"]),
        )
    later = time.time() + 2 * 3600  # past the EHR token's hour
    monkeypatch.setattr("mendari.core.audit.time.time", lambda: later)
    monkeypatch.setattr("mendari.server.sessions.time.time", lambda: later)
    assert app.get("/api/smart/session").json() == {"active": False}
    assert app.get("/api/smart/patient").status_code == 401


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/smart/patient", None),
        ("GET", f"/api/smart/focus?pack_id={REFERRAL}", None),
        ("POST", "/api/smart/decide", {"pack_id": REFERRAL, "focus": "ServiceRequest/ref-sr1"}),
    ],
)
def test_an_ehr_that_refuses_the_token_ends_the_launch(
    app: TestClient,
    ehr: FakeEhr,
    db_target: str,
    method: str,
    path: str,
    body: dict[str, str] | None,
) -> None:
    full_launch(app, ehr)
    assert app.get("/api/smart/patient").status_code == 200
    ehr.tokens.clear()  # revoked at the EHR, or expired there before its `expires_in`
    ended = app.request(method, path, json=body)
    assert ended.status_code == 401, ended.text
    assert "the EHR ended this launch" in ended.json()["detail"]
    assert "access-" not in ended.text
    assert app.get("/api/smart/session").json() == {"active": False}
    assert raw_rows(db_target, "session") == []
    with AuditStore(db_target) as store, store._db.session(exclusive=False) as db:
        assert db.one("SELECT COUNT(*) AS n FROM decisions", ())["n"] == 0


def test_an_ehr_error_other_than_401_leaves_the_launch(app: TestClient, ehr: FakeEhr) -> None:
    full_launch(app, ehr)
    focus = {"pack_id": REFERRAL, "focus": "ServiceRequest/not-there"}
    missing = app.post("/api/smart/decide", json=focus)  # the EHR answers 404
    assert missing.status_code == 422
    assert app.get("/api/smart/session").json()["active"] is True


@pytest.mark.parametrize(
    "token",
    ["has space", "line\r\nbreak", "café", "x" * (16 * 1024 + 1), ""],
    ids=["space", "crlf", "non-ascii", "too-long", "empty"],
)
def test_a_token_outside_the_oauth_syntax_is_refused_at_launch(
    app: TestClient, ehr: FakeEhr, db_target: str, monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    issued = ehr.token

    def odd(form: dict[str, list[str]]) -> httpx.Response:
        return httpx.Response(200, json={**issued(form).json(), "access_token": token})

    monkeypatch.setattr(ehr, "token", odd)
    done = full_launch(app, ehr)
    assert done.status_code == 400, done.text
    assert raw_rows(db_target, "session") == []
    assert app.get("/api/smart/session").json() == {"active": False}


def test_the_longest_valid_token_is_accepted(
    app: TestClient, ehr: FakeEhr, monkeypatch: pytest.MonkeyPatch
) -> None:
    longest = "A" * (16 * 1024 - 2) + "~!"
    issued = ehr.token

    def long_one(form: dict[str, list[str]]) -> httpx.Response:
        ehr.tokens.add(longest)
        return httpx.Response(200, json={**issued(form).json(), "access_token": longest})

    monkeypatch.setattr(ehr, "token", long_one)
    assert full_launch(app, ehr).status_code == 302
    assert app.get("/api/smart/patient").status_code == 200


def test_offline_access_is_refused_at_launch(
    db_target: str,
    ehr: FakeEhr,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Never at startup (a refusal there would take sign-in down with it): the launch is
    refused, before any request to the EHR."""
    monkeypatch.setenv("MENDARI_SMART_SCOPES", "launch openid fhirUser patient/*.rs offline_access")
    with smart_app(db_target, ehr, monkeypatch) as client:
        assert client.get("/api/health").status_code == 200
        capsys.readouterr()
        refused = client.get(
            "/smart/launch", params={"iss": ISS, "launch": "xyz", "pack": REFERRAL}
        )
        assert refused.status_code == 400 and "offline_access" in refused.text
        assert ehr.requests == []
        assert raw_rows(db_target, "pending") == []
        reasons = [e.get("reason") for e in logged(capsys) if e["event"] == "smart.launch.refused"]
        assert reasons == ["configuration"]


def test_sign_in_sessions_are_untouched(db_target: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """`user` and `oidc-pending` rows hold no token and stay as they were (sealing them would
    sign everyone out): plaintext, read by the releases on either side of this one."""
    idp = FakeIdp()
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari-org")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    monkeypatch.setenv("MENDARI_OIDC_ROLE_MAP", "clinicians=reviewer")
    transport = httpx.MockTransport(idp)
    monkeypatch.setattr(
        auth_module, "GuardedHttp", lambda **kw: GuardedHttp(transport=transport, **kw)
    )
    app = create_app(db_path=db_target)
    with TestClient(app, base_url="https://mendari.test", headers=DASHBOARD) as client:
        started = client.get("/auth/login", params={"next": "/"}, follow_redirects=False)
        [pending] = raw_rows(db_target, "oidc-pending")
        assert "verifier" in json.loads(pending["data"])  # as before: deferred (ADR-0006)
        code = idp.authorize(started.headers["location"], name="Nurse Kim", roles=["clinicians"])
        state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
        done = client.get(
            "/auth/callback", params={"code": code, "state": state}, follow_redirects=False
        )
        assert done.status_code == 302
        [person] = raw_rows(db_target, "user")
        assert json.loads(person["data"])["name"] == "Nurse Kim"
        assert client.get("/api/auth/me").json()["signed_in"] is True
        assert sign_in(client, idp, roles=["clinicians"]).status_code == 302
        # The CI ZAP job writes a person's row straight into the store, and it still works.
        with AuditStore(db_target) as store:
            sid = "zap-session-id"
            row = {"sub": "zap", "name": "zap", "claims": {"roles": ["admin"]}, "org": "default"}
            store.smart_put(sid, "user", row, int(time.time()) + 7200)
            assert store.smart_get(sid, "user") == row
            viewer = signed_in(store, role="viewer")
        client.cookies.clear()
        cookie = {"cookie": f"__Host-{sessions.SESSION}={sid}"}
        assert client.get("/api/audit", headers=cookie).status_code == 200
        assert client.get("/api/audit", headers=viewer).status_code == 200


def test_a_pending_launch_must_be_given_its_secret(store: AuditStore) -> None:
    """Its key, the `state`, travels in URLs: sealing under it by default would seal nothing."""
    with pytest.raises(ValueError, match="binding cookie"):
        store.smart_put("state", "pending", {}, int(time.time()) + 60)
    with pytest.raises(ValueError, match="binding cookie"):
        store.smart_take("state", "pending")
    sealed = seal({"a": 1}, secret="s", kind="pending", row_key="k")
    with pytest.raises(SealError):
        unseal(sealed, secret="", kind="pending", row_key="k")
