"""SMART on FHIR EHR launch (standards/smart.py, server/smart_routes.py) against a fake EHR: an
authorization server that checks PKCE and signs RS256 id_tokens, and a FHIR server that answers
from the US Core referral fixture."""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from mendari.core.audit import AuditStore
from mendari.errors import MendariError
from mendari.server import smart_routes
from mendari.server.app import create_app
from mendari.server.auth import ehr_id
from mendari.server.scopes import DECIDE
from mendari.standards.fhir_client import FhirClient, GuardedHttp
from mendari.standards.smart import normalize_base, smart_ehrs
from tests.conftest import message, security_lines, signed_in
from tests.test_backend_services import RSA_KEY, ask, assertion

EHR = "https://ehr.example.org"
ISS = f"{EHR}/fhir"
APP = "https://mendari.test"
CLIENT = "mendari-app"
REFERRAL = "mendari/referral-router"
INBOX = "mendari/patient-inbox-router"
ROUTINE = "Can you send a refill for metformin to my pharmacy on Main Street?"
FIXTURE = json.loads((Path(__file__).parent / "fixtures/us-core/referral.json").read_text())


class FakeEhr:
    """Authorization server and FHIR server in one MockTransport handler: the FHIR base `iss`,
    the authorization server and keys under `root`. The id_token names a practitioner
    (`clinician-1`, `Practitioner/pr-1`); `claims` overrides it, a `None` taking a claim out."""

    def __init__(self, iss: str = ISS, root: str = EHR) -> None:
        self.iss = iss
        self.root = root
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.codes: dict[str, dict[str, Any]] = {}
        self.tokens: set[str] = set()
        self.requests: list[httpx.Request] = []
        self.claims: dict[str, Any] = {}  # overrides for the next id_token
        self.signing_key: Any = self.key
        self.algorithm = "RS256"
        self.patient = "ref-p1"
        self.config_overrides: dict[str, Any] = {}
        self.resources = [e["resource"] for e in FIXTURE["entry"]]

    def authorize(self, url: str) -> str:
        """The browser at the authorization endpoint: approve and return the code."""
        params = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert params["response_type"] == "code"
        assert params["aud"].lower().rstrip("/") == self.iss.lower()  # the base it sent
        assert params["code_challenge_method"] == "S256"
        code = f"code-{len(self.codes)}"
        self.codes[code] = params
        return code

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == f"{self.iss}/.well-known/smart-configuration":
            config = {
                "issuer": self.iss,
                "jwks_uri": f"{self.root}/keys",
                "authorization_endpoint": f"{self.root}/auth/authorize",
                "token_endpoint": f"{self.root}/auth/token",
                "code_challenge_methods_supported": ["S256"],
            }
            return httpx.Response(200, json={**config, **self.config_overrides})
        if url == f"{self.root}/keys":
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
            return httpx.Response(200, json={"keys": [{**jwk, "kid": "k1", "alg": "RS256"}]})
        if url == f"{self.root}/auth/token":
            return self.token(dict(parse_qs(request.content.decode())))
        if url.startswith(self.iss):
            return self.fhir(request)
        return httpx.Response(404, json={})

    def token(self, form: dict[str, list[str]]) -> httpx.Response:
        code = self.codes.pop(form["code"][0], None)
        if code is None:
            return httpx.Response(
                400, json={"error": "invalid_grant", "error_description": "no <b>"}
            )
        verifier = form["code_verifier"][0]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(
            b"="
        )
        assert challenge.decode() == code["code_challenge"], "PKCE verifier does not match"
        assert form["redirect_uri"][0] == code["redirect_uri"] == f"{APP}/smart/callback"
        assert form["client_id"][0] == CLIENT
        access = f"access-{len(self.tokens)}"
        self.tokens.add(access)
        now = int(time.time())
        claims = {"iss": self.iss, "aud": CLIENT, "exp": now + 300, "iat": now}
        claims |= {"nonce": code["nonce"], "sub": "clinician-1", "fhirUser": "Practitioner/pr-1"}
        claims = {k: v for k, v in {**claims, **self.claims}.items() if v is not None}
        id_token = jwt.encode(
            claims, self.signing_key, algorithm=self.algorithm, headers={"kid": "k1"}
        )
        body = {"access_token": access, "token_type": "Bearer", "expires_in": 3600}
        body |= {"scope": code["scope"], "patient": self.patient, "encounter": "enc-1"}
        return httpx.Response(200, json={**body, "id_token": id_token})

    def fhir(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization", "").removeprefix("Bearer ") not in self.tokens:
            return httpx.Response(401, json={"resourceType": "OperationOutcome"})
        path = request.url.path.removeprefix(f"{urlsplit(self.iss).path}/")
        headers = {"content-type": "application/fhir+json"}
        if "/" in path:
            kind, rid = path.split("/", 1)
            found = [r for r in self.resources if r["resourceType"] == kind and r["id"] == rid]
            return httpx.Response(
                200 if found else 404, json=found[0] if found else {}, headers=headers
            )
        matches = [r for r in self.resources if r["resourceType"] == path]
        entries = [{"resource": r} for r in matches]
        return httpx.Response(
            200, json={"resourceType": "Bundle", "entry": entries}, headers=headers
        )


@pytest.fixture
def ehr() -> FakeEhr:
    return FakeEhr()


@pytest.fixture
def app(tmp_path: Path, ehr: FakeEhr, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
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
    with TestClient(create_app(db_path=tmp_path / "a.db"), base_url=APP) as client:
        yield client


def launch(app: TestClient, pack: str = REFERRAL, iss: str = ISS) -> Any:
    return app.get(
        "/smart/launch", params={"iss": iss, "launch": "xyz", "pack": pack}, follow_redirects=False
    )


def full_launch(app: TestClient, ehr: FakeEhr, iss: str | None = None) -> Any:
    """The EHR launches the app (from `iss`, the EHR's own base unless given) and the browser
    comes back with the code."""
    started = launch(app, iss=iss or ehr.iss)
    assert started.status_code == 302, started.text
    code = ehr.authorize(started.headers["location"])
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    return app.get("/smart/callback", params={"code": code, "state": state}, follow_redirects=False)


def test_an_ehr_launch_opens_the_patients_view_with_a_server_side_session(
    app: TestClient, ehr: FakeEhr
) -> None:
    started = launch(app)
    authorize = urlsplit(started.headers["location"])
    params = parse_qs(authorize.query)
    assert f"{authorize.scheme}://{authorize.netloc}{authorize.path}" == f"{EHR}/auth/authorize"
    assert params["launch"] == ["xyz"] and params["client_id"] == [CLIENT]
    assert set(params["scope"][0].split()) == {"launch", "openid", "fhirUser", "patient/*.rs"}
    code = ehr.authorize(started.headers["location"])
    done = app.get(
        "/smart/callback",
        params={"code": code, "state": params["state"][0]},
        follow_redirects=False,
    )
    assert done.status_code == 302
    cookie = done.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    session = app.get("/api/smart/session").json()
    assert session == {
        "active": True,
        "iss": ISS,
        "patient": "ref-p1",
        "encounter": "enc-1",
        "fhir_user": "Practitioner/pr-1",
        "scope": "launch openid fhirUser patient/*.rs",
        "expires_at": session["expires_at"],
        "pack": REFERRAL,
        "launch_id": session["launch_id"],
        "packs": session["packs"],
    }
    # The window opens on this launch's patient view, pinned to the launch.
    assert done.headers["location"] == f"{APP}/?pack={REFERRAL}#patient/{session['launch_id']}"
    assert "access" not in json.dumps(session)  # the token stays on the server


def test_each_launch_names_itself_and_the_window_it_opens(app: TestClient, ehr: FakeEhr) -> None:
    """The callback opens the window on `#patient/<launch_id>`, the session names the same
    launch and the packs it may use, and a later launch in the same browser is another launch:
    the first window can tell it was replaced (dashboard/patient.js)."""
    first = full_launch(app, ehr)
    one = app.get("/api/smart/session").json()
    assert first.headers["location"].endswith(f"#patient/{one['launch_id']}")
    assert re.fullmatch(r"[0-9a-f]{16}", one["launch_id"])
    listed = {pack["id"]: pack["name"] for pack in app.get("/api/packs").json()}
    assert one["packs"] == [{"id": i, "name": n} for i, n in sorted(listed.items())]
    second = full_launch(app, ehr)
    two = app.get("/api/smart/session").json()
    assert two["launch_id"] != one["launch_id"]
    assert second.headers["location"].endswith(f"#patient/{two['launch_id']}")


def test_the_patient_banner_and_work_items_come_from_the_ehr(app: TestClient, ehr: FakeEhr) -> None:
    full_launch(app, ehr)
    patient = app.get("/api/smart/patient").json()
    assert patient == {
        "id": "ref-p1",
        "name": "Case ref-us-core-1 Synthetic",
        "gender": "female",
        "birthDate": "1956-06-15",
    }
    items = app.get("/api/smart/focus", params={"pack_id": REFERRAL}).json()
    assert [(i["ref"], i["priority"]) for i in items] == [("ServiceRequest/ref-sr1", "routine")]
    assert items[0]["text"].startswith("Atrial fibrillation")


def test_deciding_a_work_item_reads_the_record_and_audits_the_decision(
    app: TestClient, ehr: FakeEhr
) -> None:
    full_launch(app, ehr)
    ehr.requests.clear()
    response = app.post(
        "/api/smart/decide", json={"pack_id": REFERRAL, "focus": "ServiceRequest/ref-sr1"}
    )
    assert response.status_code == 200, response.text
    decision = response.json()["decision"]
    assert decision["input_issues"] == []  # the fixture is US Core
    read = sorted(r.url.path.removeprefix("/fhir/") for r in ehr.requests)
    assert read == [
        "DiagnosticReport",
        "DocumentReference",
        "Observation",
        "Patient/ref-p1",
        "ServiceRequest/ref-sr1",
    ]
    assert app.get(f"/api/audit/{decision['id']}").status_code == 200


def test_logging_out_ends_the_session(app: TestClient, ehr: FakeEhr) -> None:
    full_launch(app, ehr)
    assert app.post("/api/smart/logout").status_code == 204
    assert app.get("/api/smart/session").json() == {"active": False}
    assert app.get("/api/smart/patient").status_code == 401


def test_a_launch_state_works_once(app: TestClient, ehr: FakeEhr) -> None:
    started = launch(app)
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    code = ehr.authorize(started.headers["location"])
    assert (
        app.get(
            "/smart/callback", params={"code": code, "state": state}, follow_redirects=False
        ).status_code
        == 302
    )
    again = app.get("/smart/callback", params={"code": code, "state": state})
    assert again.status_code == 400
    assert "unknown, used or expired" in again.text
    assert again.headers["content-type"].startswith("text/html")


def test_a_launch_finished_in_another_browser_is_refused(app: TestClient, ehr: FakeEhr) -> None:
    """Launch CSRF: a callback URL from someone else's launch must not put their patient's
    context into this browser."""
    started = launch(app)
    assert started.headers["set-cookie"].startswith("__Host-mendari_launch=")
    code = ehr.authorize(started.headers["location"])
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    other = TestClient(app.app, base_url=APP)
    landed = other.get("/smart/callback", params={"code": code, "state": state})
    assert landed.status_code == 400 and "started in another browser" in landed.text
    assert other.get("/api/smart/session").json() == {"active": False}


def test_a_launch_on_another_name_moves_to_the_public_url_first(
    app: TestClient, ehr: FakeEhr, tmp_path: Path
) -> None:
    """The launch binding cookie is host-only and the callback comes back to MENDARI_PUBLIC_URL:
    a launch on another name begins again there, before anything is bound or written."""
    alias = TestClient(app.app, base_url="https://mendari-abc.vercel.app")
    params = {"iss": ISS, "launch": "xyz", "pack": REFERRAL}
    moved = alias.get("/smart/launch", params=params, follow_redirects=False)
    assert moved.status_code == 302 and "set-cookie" not in moved.headers
    location = urlsplit(moved.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == f"{APP}/smart/launch"
    assert parse_qs(location.query) == {k: [v] for k, v in {**params, "moved": "1"}.items()}
    with closing(sqlite3.connect(tmp_path / "a.db")) as db:
        assert db.execute("SELECT COUNT(*) FROM smart_sessions").fetchone() == (0,)
    started = alias.get(moved.headers["location"], follow_redirects=False)
    assert started.headers["location"].startswith(f"{EHR}/auth/authorize?")
    code = ehr.authorize(started.headers["location"])
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    done = alias.get(
        f"{APP}/smart/callback", params={"code": code, "state": state}, follow_redirects=False
    )
    assert done.status_code == 302
    assert done.headers["location"].startswith(f"{APP}/?pack={REFERRAL}#patient/")
    assert alias.get(f"{APP}/api/smart/session").json()["active"] is True


def test_a_launch_session_ends_when_idle(
    app: TestClient, ehr: FakeEhr, monkeypatch: pytest.MonkeyPatch
) -> None:
    done = full_launch(app, ehr)
    cookie = next(c for c in done.headers.get_list("set-cookie") if "mendari_smart" in c)
    assert "max-age" not in cookie.lower()  # ends with the browser too
    assert app.get("/api/smart/session").json()["active"] is True
    later = time.time() + 31 * 60
    monkeypatch.setattr("mendari.core.audit.time.time", lambda: later)
    assert app.get("/api/smart/session").json() == {"active": False}


def test_an_unknown_state_is_refused(app: TestClient) -> None:
    assert app.get("/smart/callback", params={"code": "c", "state": "forged"}).status_code == 400


def test_an_authorization_error_is_reported_without_echoing_the_server(app: TestClient) -> None:
    started = launch(app)
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    refused = app.get("/smart/callback", params={"state": state, "error": "access_denied"})
    assert refused.status_code == 400 and "(access_denied)" in refused.text
    odd = app.get("/smart/callback", params={"state": "x", "error": "<script>alert(1)</script>"})
    assert "<script>" not in odd.text


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"aud": "someone-else"}, "InvalidAudienceError"),
        ({"iss": "https://evil.example/fhir"}, "InvalidIssuerError"),
        ({"exp": int(time.time()) - 3600}, "ExpiredSignatureError"),
        ({"nonce": "replayed"}, "nonce does not match"),
    ],
)
def test_an_id_token_with_bad_claims_is_refused(
    app: TestClient, ehr: FakeEhr, change: dict[str, Any], reason: str
) -> None:
    ehr.claims = change
    done = full_launch(app, ehr)
    assert done.status_code == 400 and reason in done.text
    assert app.get("/api/smart/session").json() == {"active": False}


def test_an_id_token_signed_by_another_key_is_refused(app: TestClient, ehr: FakeEhr) -> None:
    ehr.signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    done = full_launch(app, ehr)
    assert done.status_code == 400 and "InvalidSignatureError" in done.text


def test_an_unsigned_or_symmetric_id_token_is_refused(app: TestClient, ehr: FakeEhr) -> None:
    ehr.signing_key, ehr.algorithm = "shared-secret-that-is-long-enough-for-hs256", "HS256"
    done = full_launch(app, ehr)
    assert done.status_code == 400 and "HS256" in done.text


def test_a_launch_without_a_patient_is_refused(app: TestClient, ehr: FakeEhr) -> None:
    ehr.patient = None  # type: ignore[assignment]
    assert "did not send a patient" in full_launch(app, ehr).text


def test_a_server_off_the_allow_list_is_never_called(app: TestClient, ehr: FakeEhr) -> None:
    response = app.get("/smart/launch", params={"iss": "https://other.example/fhir", "launch": "x"})
    assert response.status_code == 422 and "allow-list" in response.text
    assert ehr.requests == []


def test_endpoints_the_configuration_names_must_be_allowed_too(
    app: TestClient, ehr: FakeEhr
) -> None:
    ehr.config_overrides = {"token_endpoint": "https://collector.example/token"}
    assert launch(app).status_code == 422


def test_pkce_s256_is_required(app: TestClient, ehr: FakeEhr) -> None:
    ehr.config_overrides = {"code_challenge_methods_supported": ["plain"]}
    response = launch(app)
    assert response.status_code == 400 and "S256" in response.text


def test_a_work_item_about_another_patient_is_refused(app: TestClient, ehr: FakeEhr) -> None:
    full_launch(app, ehr)
    other = {
        **next(r for r in ehr.resources if r["resourceType"] == "ServiceRequest"),
        "id": "other-sr",
        "subject": {"reference": "Patient/someone-else"},
    }
    ehr.resources.append(other)
    denied = app.post(
        "/api/smart/decide", json={"pack_id": REFERRAL, "focus": "ServiceRequest/other-sr"}
    )
    assert denied.status_code == 403
    wrong = app.post("/api/smart/decide", json={"pack_id": REFERRAL, "focus": "Patient/ref-p1"})
    assert wrong.status_code == 422


def test_launch_state_and_session_ids_are_stored_hashed(
    app: TestClient, ehr: FakeEhr, tmp_path: Path
) -> None:
    full_launch(app, ehr)
    sid = app.cookies.get(f"__Host-{smart_routes.COOKIE}")
    assert sid
    with AuditStore(tmp_path / "a.db")._db.session(exclusive=False) as db:
        keys = [row["key_sha256"] for row in db.all("SELECT key_sha256 FROM smart_sessions")]
    assert hashlib.sha256(b"session\x00" + sid.encode()).hexdigest() in keys and sid not in keys


def test_an_expired_session_is_gone(
    app: TestClient, ehr: FakeEhr, monkeypatch: pytest.MonkeyPatch
) -> None:
    full_launch(app, ehr)
    later = time.time() + 7200
    monkeypatch.setattr("mendari.core.audit.time.time", lambda: later)
    assert app.get("/api/smart/session").json() == {"active": False}


def test_smart_is_off_until_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MENDARI_SMART_CLIENT_ID", raising=False)
    client = TestClient(create_app(db_path=tmp_path / "a.db"))
    assert client.get("/api/health").json()["smart_launch"] is False
    response = client.get("/smart/launch", params={"iss": ISS, "launch": "x"})
    assert response.status_code == 404 and "not configured" in response.text


def test_the_store_keeps_launches_single_use_and_expires_them(store: AuditStore) -> None:
    """Both backends (the `store` fixture runs on SQLite and Postgres)."""
    now = int(time.time())
    store.smart_put("state-1", "pending", {"iss": ISS}, now + 60, secret="binding")
    assert store.smart_get("state-1", "session") is None  # kinds are separate
    assert store.smart_take("state-1", "pending", secret="binding") == {"iss": ISS}
    assert store.smart_take("state-1", "pending", secret="binding") is None  # works once
    store.smart_put("old", "session", {"patient": "p"}, now - 1)
    assert store.smart_get("old", "session") is None
    store.smart_put("sid", "session", {"patient": "p"}, now + 60)
    store.smart_delete("sid", "session")
    assert store.smart_get("sid", "session") is None


# -- the launch is a principal (C5, ADR-0006): sign-in on, EHRs registered ------------------------

TENANTS = "https://ehrs.example.org"  # one host, two tenants, two organizations
TENANT_A = f"{TENANTS}/tenant-a/fhir"
TENANT_B = f"{TENANTS}/tenant-b/fhir"
REGISTERED = f"{TENANT_A}=clinic-a:decide,{TENANT_B}=clinic-b:decide"
DECIDE_BODY = {"pack_id": REFERRAL, "focus": "ServiceRequest/ref-sr1"}


def tenant(iss: str) -> FakeEhr:
    return FakeEhr(iss=iss, root=iss.removesuffix("/fhir"))


def routed(*ehrs: FakeEhr) -> httpx.MockTransport:
    """One transport for several EHRs, each answering under its own root."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for ehr in ehrs:
            if url.startswith(f"{ehr.root}/"):
                return ehr(request)
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)


def signed_site(
    db: Path | str, monkeypatch: pytest.MonkeyPatch, registered: str | None, *ehrs: FakeEhr
) -> TestClient:
    """Mendari with sign-in on (no one needs to sign in to launch) and `registered` as
    MENDARI_SMART_EHRS; `None` leaves sign-in off (open mode)."""
    monkeypatch.setenv("MENDARI_SMART_CLIENT_ID", CLIENT)
    monkeypatch.setenv("MENDARI_PUBLIC_URL", APP)
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", f"{EHR},{TENANTS}")
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari-org")
    monkeypatch.setenv("MENDARI_SMART_EHRS", registered or "")
    if registered is None:
        monkeypatch.delenv("MENDARI_OIDC_ISSUER")
        monkeypatch.delenv("MENDARI_OIDC_CLIENT_ID")
    transport = routed(*ehrs)
    monkeypatch.setattr(
        smart_routes, "GuardedHttp", functools.partial(GuardedHttp, transport=transport)
    )
    monkeypatch.setattr(
        smart_routes, "FhirClient", functools.partial(FhirClient, transport=transport)
    )
    return TestClient(create_app(db_path=db), base_url=APP)


def admin(app: TestClient, db: Path | str, org: str) -> TestClient:
    """A browser of `org`'s administrator, signed in (and nothing else)."""
    client = TestClient(app.app, base_url=APP)
    with AuditStore(db) as store:
        client.headers.update(signed_in(store, role="admin", org=org))
    return client


@pytest.fixture
def tenants() -> tuple[FakeEhr, FakeEhr]:
    return tenant(TENANT_A), tenant(TENANT_B)


def test_a_launch_decides_in_its_registered_organization(
    db_target: str,
    monkeypatch: pytest.MonkeyPatch,
    tenants: tuple[FakeEhr, FakeEhr],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With sign-in on, a launch is an `ehr` principal of its EHR's registered organization: its
    decision is that organization's (another's administrator gets 404), the security log names
    it by an opaque id, and its organization's signed-in health lists the registration. Both
    backends."""
    a, b = tenants
    app = signed_site(db_target, monkeypatch, REGISTERED, a, b)
    capsys.readouterr()
    assert full_launch(app, a).status_code == 302
    decided = app.post("/api/smart/decide", json=DECIDE_BODY)
    assert decided.status_code == 200, decided.text
    decision = decided.json()["decision"]["id"]
    assert admin(app, db_target, "clinic-a").get(f"/api/audit/{decision}").status_code == 200
    for other in ("clinic-b", "default"):
        assert admin(app, db_target, other).get(f"/api/audit/{decision}").status_code == 404
    who = ehr_id(TENANT_A, "clinician-1")
    lines = security_lines(capsys.readouterr().out)
    ok = next(e for e in lines if e["event"] == "smart.launch.ok")
    assert (ok["actor"], ok["org"]) == (who, "clinic-a")
    access = next(e for e in lines if e.get("route") == "/api/smart/decide")
    assert (access["actor"], access["org"], access["status"]) == (who, "clinic-a", 200)
    assert "clinician-1" not in json.dumps(lines)  # the subject itself never
    health = admin(app, db_target, "clinic-a").get("/api/health").json()
    assert health["smart_ehrs"] == [{"base": TENANT_A, "org": "clinic-a", "level": "decide"}]


def test_decision_created_names_who_asked(
    db_target: str, monkeypatch: pytest.MonkeyPatch, tenants: tuple[FakeEhr, FakeEhr]
) -> None:
    """With sign-in on, every new `decision.created` names who asked, as the chain names them
    (`Principal.actor`): a person by verified email, a key and a client by id, an EHR user by the
    EHR's issuer and subject. Open mode names nobody, an event chained before is never rewritten,
    and `audit verify` still passes. Both backends."""
    a, b = tenants
    opened = signed_site(db_target, monkeypatch, None, a)  # open mode, before sign-in was on
    before = opened.post(f"/api/packs/{INBOX}/decide", json=message(ROUTINE))
    assert before.status_code == 200, before.text
    with AuditStore(db_target) as store:
        earlier = store.events()
    monkeypatch.setenv("MENDARI_BACKEND_SERVICES", "on")
    app = signed_site(db_target, monkeypatch, REGISTERED, a, b)
    asked: dict[str, str] = {}

    def decide(headers: dict[str, str]) -> str:
        made = app.post(f"/api/packs/{INBOX}/decide", json=message(ROUTINE), headers=headers)
        assert made.status_code == 200, made.text
        return str(made.json()["id"])

    with AuditStore(db_target) as store:
        person = signed_in(store, role="admin", org="clinic-a")
    asked[decide(person)] = "admin@clinic-a.example.org"
    key = app.post("/api/auth/keys", json={"name": "worker", "role": "service"}, headers=person)
    assert key.status_code == 201, key.text
    asked[decide({"authorization": f"Bearer {key.json()['key']}"})] = f"key {key.json()['id']}"
    body = {"name": "integration", "role": "service", "jwks": {"keys": [RSA_KEY.jwk]}}
    client = app.post("/api/auth/clients", json=body, headers=person).json()["client_id"]
    token = ask(app, assertion(RSA_KEY, client), DECIDE).json()["access_token"]
    asked[decide({"authorization": f"Bearer {token}"})] = f"client {client}"
    assert full_launch(app, a).status_code == 302
    launched = app.post("/api/smart/decide", json=DECIDE_BODY)
    assert launched.status_code == 200, launched.text
    asked[launched.json()["decision"]["id"]] = f"{TENANT_A}|clinician-1"

    with AuditStore(db_target) as store:
        events = store.events()
        assert store.verify()["ok"] is True
    assert events[: len(earlier)] == earlier  # never rewritten
    created = {e["decision_id"]: e["payload"] for e in events if e["event"] == "decision.created"}
    assert set(created[before.json()["id"]]) == {"record_sha256"}  # open mode: nobody
    for decision, actor in asked.items():
        assert set(created[decision]) == {"record_sha256", "requested_by"}
        assert created[decision]["requested_by"] == actor
    trail = admin(app, db_target, "clinic-a").get(
        f"/api/audit/{launched.json()['decision']['id']}/fhir"
    )
    agents = [r["resource"]["agent"] for r in trail.json()["entry"]]
    assert agents[0][1] == {"who": {"display": f"{TENANT_A}|clinician-1"}, "requestor": True}


def test_two_tenants_on_one_origin_map_to_two_organizations(
    db_target: str, monkeypatch: pytest.MonkeyPatch, tenants: tuple[FakeEhr, FakeEhr]
) -> None:
    """Registrations are keyed on the whole FHIR base, never its origin: two tenants of one
    EHR host are two organizations. The `iss` an EHR sends is normalized first. Both
    backends."""
    a, b = tenants
    app = signed_site(db_target, monkeypatch, REGISTERED, a, b)
    made = {}
    for ehr, sent in ((a, TENANT_A), (b, "HTTPS://EHRS.example.org/tenant-b/fhir/")):
        app.cookies.clear()
        assert full_launch(app, ehr, iss=sent).status_code == 302
        decided = app.post("/api/smart/decide", json=DECIDE_BODY)
        assert decided.status_code == 200, decided.text
        made[ehr.iss] = decided.json()["decision"]["id"]
    for org, mine, theirs in (
        ("clinic-a", made[TENANT_A], made[TENANT_B]),
        ("clinic-b", made[TENANT_B], made[TENANT_A]),
    ):
        reader = admin(app, db_target, org)
        assert reader.get(f"/api/audit/{mine}").status_code == 200
        assert reader.get(f"/api/audit/{theirs}").status_code == 404
    assert normalize_base("HTTPS://EHRS.example.org/tenant-b/fhir/") == TENANT_B
    assert normalize_base("https://ehrs.example.org/Tenant-B/fhir") != TENANT_B  # the path kept


def test_an_unregistered_ehr_is_refused_before_any_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tenants: tuple[FakeEhr, FakeEhr],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An allow-listed EHR that is not registered: with sign-in on, refused at `/smart/launch`
    before anything leaves for it (no discovery, no binding cookie, no row)."""
    a, b = tenants
    elsewhere = tenant(f"{TENANTS}/tenant-c/fhir")
    app = signed_site(tmp_path / "a.db", monkeypatch, REGISTERED, a, b, elsewhere)
    capsys.readouterr()
    refused = launch(app, iss=elsewhere.iss)
    assert refused.status_code == 403 and "not registered here" in refused.text
    assert "set-cookie" not in refused.headers
    assert elsewhere.requests == [] and a.requests == [] and b.requests == []
    events = [e for e in security_lines(capsys.readouterr().out) if e["event"] != "request"]
    assert [(e["event"], e["reason"]) for e in events] == [("smart.launch.refused", "unregistered")]
    # With sign-in on and nothing registered, every launch is refused, and the server starts.
    none = signed_site(tmp_path / "b.db", monkeypatch, "", a)
    assert launch(none, iss=TENANT_A).status_code == 403 and a.requests == []
    assert none.get("/api/health").status_code == 200


def test_a_view_level_launch_cannot_decide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tenants: tuple[FakeEhr, FakeEhr]
) -> None:
    a, b = tenants
    registered = f"{TENANT_A}=clinic-a:view,{TENANT_B}=clinic-b:decide"
    app = signed_site(tmp_path / "a.db", monkeypatch, registered, a, b)
    assert full_launch(app, a).status_code == 302
    assert app.get("/api/smart/patient").status_code == 200
    assert app.get("/api/smart/focus", params={"pack_id": REFERRAL}).status_code == 200
    a.requests.clear()
    refused = app.post("/api/smart/decide", json=DECIDE_BODY)
    assert refused.status_code == 403 and "to view, not to decide" in refused.text
    assert (
        refused.headers["www-authenticate"] == 'Bearer realm="mendari", error="insufficient_scope"'
    )
    assert a.requests == []  # refused by the guard: no read, no decision


@pytest.mark.parametrize(
    ("claims", "reason", "status"),
    [
        ({"fhirUser": "Patient/p1"}, "user_type", 403),  # a patient portal
        ({"fhirUser": f"{TENANT_A}/RelatedPerson/r1"}, "user_type", 403),
        ({"fhirUser": "Person/x"}, "user_type", 403),
        ({"fhirUser": None}, "user_type", 403),  # nobody named
        ({"sub": None}, "id_token", 400),
    ],
)
def test_a_patient_or_related_person_cannot_launch_with_sign_in_on(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tenants: tuple[FakeEhr, FakeEhr],
    capsys: pytest.CaptureFixture[str],
    claims: dict[str, Any],
    reason: str,
    status: int,
) -> None:
    """Only a practitioner launches with sign-in on: a launch whose user is a Patient,
    RelatedPerson or anyone else, or whose id_token names no subject, gets no session."""
    a, b = tenants
    app = signed_site(tmp_path / "a.db", monkeypatch, REGISTERED, a, b)
    a.claims = claims
    capsys.readouterr()
    done = full_launch(app, a)
    assert done.status_code == status, done.text
    assert app.get("/api/smart/session").json() == {"active": False}
    events = [e for e in security_lines(capsys.readouterr().out) if e["event"] != "request"]
    assert [(e["event"], e["reason"]) for e in events] == [("smart.launch.refused", reason)]
    # A PractitionerRole is a clinician, relative or absolute.
    a.claims = {"fhirUser": f"{TENANT_A}/PractitionerRole/role-1"}
    assert full_launch(app, a).status_code == 302
    assert app.get("/api/smart/session").json()["fhir_user"].endswith("PractitionerRole/role-1")


def test_scopes_that_do_not_say_who_launched_are_refused_with_sign_in_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tenants: tuple[FakeEhr, FakeEhr]
) -> None:
    """`openid fhirUser` name the clinician: without them, refused at launch (the server
    starts, and nothing leaves for the EHR)."""
    a, b = tenants
    monkeypatch.setenv("MENDARI_SMART_SCOPES", "launch openid patient/*.rs")
    app = signed_site(tmp_path / "a.db", monkeypatch, REGISTERED, a, b)
    refused = launch(app, iss=TENANT_A)
    assert refused.status_code == 400 and "openid and fhirUser" in refused.text
    assert a.requests == []


def test_unregistering_an_ehr_ends_its_launches_at_the_next_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tenants: tuple[FakeEhr, FakeEhr]
) -> None:
    """The organization and level are the registration the server started with (read at
    startup), never stored with the launch: after a restart without it, the same launch cookie
    reaches nothing; registered to view, it no longer decides."""
    a, b = tenants
    db = tmp_path / "a.db"
    app = signed_site(db, monkeypatch, REGISTERED, a, b)
    assert full_launch(app, a).status_code == 302
    assert app.post("/api/smart/decide", json=DECIDE_BODY).status_code == 200
    cookies = dict(app.cookies)
    viewing = signed_site(db, monkeypatch, f"{TENANT_A}=clinic-a:view", a, b)
    viewing.cookies.update(cookies)
    assert viewing.get("/api/smart/session").json()["active"] is True
    assert viewing.post("/api/smart/decide", json=DECIDE_BODY).status_code == 403
    gone = signed_site(db, monkeypatch, f"{TENANT_B}=clinic-b:decide", a, b)
    gone.cookies.update(cookies)
    assert gone.get("/api/smart/session").json() == {"active": False}
    assert gone.get("/api/smart/patient").status_code == 401
    assert gone.post("/api/smart/decide", json=DECIDE_BODY).status_code == 401


def test_signing_out_of_mendari_ends_the_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tenants: tuple[FakeEhr, FakeEhr]
) -> None:
    """On a shared workstation, signing out of Mendari ends this browser's EHR launch too."""
    a, b = tenants
    db = tmp_path / "a.db"
    app = signed_site(db, monkeypatch, REGISTERED, a, b)
    assert full_launch(app, a).status_code == 302
    sid = app.cookies.get(f"__Host-{smart_routes.COOKIE}")
    assert sid and app.get("/api/smart/session").json()["active"] is True
    out = app.post("/auth/logout")
    assert out.status_code == 204
    assert any(c.startswith("__Host-mendari_smart=") for c in out.headers.get_list("set-cookie"))
    assert AuditStore(db).smart_get(sid, "session") is None
    app.cookies.set(f"__Host-{smart_routes.COOKIE}", sid, domain="mendari.test")
    assert app.get("/api/smart/session").json() == {"active": False}
    assert app.get("/api/smart/patient").status_code == 401


def test_a_later_launch_in_the_same_browser_ends_the_earlier_one(
    db_target: str, monkeypatch: pytest.MonkeyPatch, tenants: tuple[FakeEhr, FakeEhr]
) -> None:
    """One browser holds one launch: the callback deletes the launch the browser's cookie named,
    so a copy of the earlier cookie reaches nothing, and signing out leaves no launch behind.
    Both backends."""
    a, b = tenants
    app = signed_site(db_target, monkeypatch, REGISTERED, a, b)
    name = f"__Host-{smart_routes.COOKIE}"
    assert full_launch(app, a).status_code == 302
    first = app.cookies.get(name)
    assert full_launch(app, a).status_code == 302
    second = app.cookies.get(name)
    assert first and second and first != second
    copied = TestClient(app.app, base_url=APP)
    copied.cookies.set(name, first, domain="mendari.test")
    assert copied.get("/api/smart/session").json() == {"active": False}
    assert copied.get("/api/smart/patient").status_code == 401
    assert app.get("/api/smart/patient").status_code == 200
    assert app.post("/auth/logout").status_code == 204
    with AuditStore(db_target) as store:
        assert store.smart_get(first, "session") is None
        assert store.smart_get(second, "session") is None


def test_a_launch_made_with_sign_in_off_is_refused_once_sign_in_is_on(
    db_target: str, monkeypatch: pytest.MonkeyPatch, tenants: tuple[FakeEhr, FakeEhr]
) -> None:
    """A launch made in open mode (an EHR registered, so it was let in) skipped the checks
    sign-in on makes at the callback: its user may be a patient portal's. After a restart with
    sign-in on against the same database it is no principal, and a fresh launch is checked.
    Both backends."""
    a, b = tenants
    signed_site(db_target, monkeypatch, None, a, b)  # open mode, the fake EHRs wired
    monkeypatch.setenv("MENDARI_SMART_EHRS", REGISTERED)
    pilot = TestClient(create_app(db_path=db_target), base_url=APP)
    a.claims = {"fhirUser": "Patient/p1"}  # a patient portal: refused with sign-in on
    assert full_launch(pilot, a).status_code == 302
    assert pilot.get("/api/smart/session").json()["fhir_user"] == "Patient/p1"
    cookies = dict(pilot.cookies)
    app = signed_site(db_target, monkeypatch, REGISTERED, a, b)
    app.cookies.update(cookies)
    assert app.get("/api/smart/session").json() == {"active": False}
    assert app.get("/api/smart/patient").status_code == 401
    assert app.post("/api/smart/decide", json=DECIDE_BODY).status_code == 401
    a.claims = {}  # the clinician launches again, and is checked
    assert full_launch(app, a).status_code == 302
    assert app.post("/api/smart/decide", json=DECIDE_BODY).status_code == 200


def test_a_window_pinned_to_a_replaced_launch_reads_nothing(app: TestClient, ehr: FakeEhr) -> None:
    """The dashboard sends the launch its window is pinned to (`X-Mendari-Launch`) with the
    patient, focus and decide requests: once a later launch replaced it, the server answers
    `409` before reading anything from the EHR, so the window never shows the other launch's
    patient. Without the header the routes answer as before."""
    full_launch(app, ehr)
    first = app.get("/api/smart/session").json()["launch_id"]
    full_launch(app, ehr)
    second = app.get("/api/smart/session").json()["launch_id"]
    ehr.requests.clear()
    stale = {"x-mendari-launch": first}
    for answer in (
        app.get("/api/smart/patient", headers=stale),
        app.get("/api/smart/focus", params={"pack_id": REFERRAL}, headers=stale),
        app.post("/api/smart/decide", json=DECIDE_BODY, headers=stale),
    ):
        assert answer.status_code == 409, answer.text
        assert "replaced" in answer.json()["detail"]
    assert ehr.requests == []
    assert app.get("/api/audit").json() == []  # nothing decided
    current = {"x-mendari-launch": second}
    assert app.get("/api/smart/patient", headers=current).status_code == 200
    assert app.post("/api/smart/decide", json=DECIDE_BODY, headers=current).status_code == 200
    assert app.get("/api/smart/patient").status_code == 200


def test_a_launch_opens_the_dashboard_page_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tenants: tuple[FakeEhr, FakeEhr]
) -> None:
    """With sign-in on, the launch cookie lets the dashboard page load (no detour to sign in),
    and is a credential on `/api/smart/` only: the store, packs and API reference stay shut.
    What the dashboard's launch-only mode boots on answers: the session, with the launch's id
    and the packs it may use, and who is signed in (no one)."""
    a, b = tenants
    app = signed_site(tmp_path / "a.db", monkeypatch, REGISTERED, a, b)
    location = full_launch(app, a).headers["location"]
    session = app.get("/api/smart/session").json()
    assert location == f"{APP}/?pack={REFERRAL}#patient/{session['launch_id']}"
    assert {"id": REFERRAL, "name": "Referral Router"} in session["packs"]
    assert app.get("/", follow_redirects=False).status_code == 200
    for path in ("/api/audit", "/api/packs", "/api/review", "/openapi.json"):
        assert app.get(path).status_code == 401, path
    assert app.get("/docs/reference", follow_redirects=False).status_code == 302
    assert app.get("/api/auth/me").json() == {"mode": "sign-in", "signed_in": False}


def test_open_mode_launches_as_before(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tenants: tuple[FakeEhr, FakeEhr],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without sign-in and with no EHR registered: any allow-listed EHR launches, a patient
    portal's user included, and decides in open mode's one organization. Registering EHRs in
    open mode only limits which may launch."""
    a, b = tenants
    a.claims = {"fhirUser": "Patient/p1", "sub": None}
    app = signed_site(tmp_path / "a.db", monkeypatch, None, a, b)
    capsys.readouterr()
    assert full_launch(app, a).status_code == 302
    decided = app.post("/api/smart/decide", json=DECIDE_BODY)
    assert decided.status_code == 200, decided.text
    assert app.get(f"/api/audit/{decided.json()['decision']['id']}").status_code == 200
    ok = next(e for e in security_lines(capsys.readouterr().out) if e["event"] == "smart.launch.ok")
    assert "org" not in ok and "actor" not in ok
    monkeypatch.setenv("MENDARI_SMART_EHRS", f"{TENANT_B}=clinic-b:view")
    gated = TestClient(create_app(db_path=tmp_path / "b.db"), base_url=APP)
    a.requests.clear()
    assert launch(gated, iss=TENANT_A).status_code == 403 and a.requests == []
    assert full_launch(gated, b).status_code == 302
    assert gated.post("/api/smart/decide", json=DECIDE_BODY).status_code == 200  # open mode


def test_a_registration_is_keyed_on_the_normalized_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", f"{TENANTS},https://ehr.example.org:8443")
    monkeypatch.setenv(
        "MENDARI_SMART_EHRS",
        " HTTPS://EHRS.Example.org/tenant-a/fhir/ =urn:org:a:view , "
        "https://ehr.example.org:8443/R4=clinic-b:decide",
    )
    registered = smart_ehrs()
    assert {base: (r.org, r.level) for base, r in registered.items()} == {
        TENANT_A: ("urn:org:a", "view"),
        "https://ehr.example.org:8443/R4": ("clinic-b", "decide"),
    }
    monkeypatch.delenv("MENDARI_SMART_EHRS")
    assert smart_ehrs() == {}


@pytest.mark.parametrize(
    ("value", "message_part"),
    [
        (f"{TENANT_A}=clinic-a", "must look like"),
        (f"{TENANT_A}=clinic-a:admin", "must look like"),
        (f"{TENANT_A}=:decide", "must look like"),
        ("ehrs.example.org/fhir=clinic-a:view", "must look like"),
        (f"{TENANT_A}?x=1=clinic-a:view", "must look like"),
        (f"{TENANT_A}=clinic-a:view,{TENANT_A}/=clinic-b:view", "registered twice"),
        ("https://other.example.org/fhir=clinic-a:view", "not on MENDARI_FHIR_ALLOWED_SERVERS"),
    ],
)
def test_a_malformed_or_unlisted_registration_fails_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str, message_part: str
) -> None:
    """The only SMART setting that stops the server: it is set, and wrong."""
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", TENANTS)
    monkeypatch.setenv("MENDARI_SMART_EHRS", value)
    with pytest.raises(MendariError, match=message_part):
        smart_ehrs()
    with pytest.raises(MendariError, match=message_part):
        create_app(db_path=tmp_path / "a.db")
