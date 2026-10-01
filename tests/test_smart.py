"""SMART on FHIR EHR launch (standards/smart.py, server/smart_routes.py) against a fake EHR: an
authorization server that checks PKCE and signs RS256 id_tokens, and a FHIR server that answers
from the US Core referral fixture."""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from mendari.core.audit import AuditStore
from mendari.server import smart_routes
from mendari.server.app import create_app
from mendari.standards.fhir_client import FhirClient, GuardedHttp

EHR = "https://ehr.example.org"
ISS = f"{EHR}/fhir"
APP = "https://mendari.test"
CLIENT = "mendari-app"
REFERRAL = "mendari/referral-router"
FIXTURE = json.loads((Path(__file__).parent / "fixtures/us-core/referral.json").read_text())


class FakeEhr:
    """Authorization server and FHIR server in one MockTransport handler."""

    def __init__(self) -> None:
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
        assert params["response_type"] == "code" and params["aud"] == ISS
        assert params["code_challenge_method"] == "S256"
        code = f"code-{len(self.codes)}"
        self.codes[code] = params
        return code

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == f"{ISS}/.well-known/smart-configuration":
            config = {
                "issuer": ISS,
                "jwks_uri": f"{EHR}/keys",
                "authorization_endpoint": f"{EHR}/auth/authorize",
                "token_endpoint": f"{EHR}/auth/token",
                "code_challenge_methods_supported": ["S256"],
            }
            return httpx.Response(200, json={**config, **self.config_overrides})
        if url == f"{EHR}/keys":
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
            return httpx.Response(200, json={"keys": [{**jwk, "kid": "k1", "alg": "RS256"}]})
        if url == f"{EHR}/auth/token":
            return self.token(dict(parse_qs(request.content.decode())))
        if url.startswith(ISS):
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
        claims = {"iss": ISS, "aud": CLIENT, "exp": now + 300, "iat": now, "nonce": code["nonce"]}
        claims |= {"fhirUser": "Practitioner/pr-1", **self.claims}
        id_token = jwt.encode(
            claims, self.signing_key, algorithm=self.algorithm, headers={"kid": "k1"}
        )
        body = {"access_token": access, "token_type": "Bearer", "expires_in": 3600}
        body |= {"scope": code["scope"], "patient": self.patient, "encounter": "enc-1"}
        return httpx.Response(200, json={**body, "id_token": id_token})

    def fhir(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization", "").removeprefix("Bearer ") not in self.tokens:
            return httpx.Response(401, json={"resourceType": "OperationOutcome"})
        path = request.url.path.removeprefix("/fhir/")
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


def launch(app: TestClient, pack: str = REFERRAL) -> Any:
    return app.get(
        "/smart/launch", params={"iss": ISS, "launch": "xyz", "pack": pack}, follow_redirects=False
    )


def full_launch(app: TestClient, ehr: FakeEhr) -> Any:
    started = launch(app)
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
    assert done.headers["location"] == f"{APP}/?pack={REFERRAL}#patient"
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
    }
    assert "access" not in json.dumps(session)  # the token stays on the server


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
    done = full_launch(app, ehr)
    sid = done.cookies.get(smart_routes.COOKIE) or app.cookies.get(smart_routes.COOKIE)
    assert sid
    with AuditStore(tmp_path / "a.db")._db.session(exclusive=False) as db:
        keys = [row["key_sha256"] for row in db.all("SELECT key_sha256 FROM smart_sessions")]
    assert hashlib.sha256(sid.encode()).hexdigest() in keys and sid not in keys


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
    store.smart_put("state-1", "pending", {"iss": ISS}, now + 60)
    assert store.smart_get("state-1", "session") is None  # kinds are separate
    assert store.smart_take("state-1", "pending") == {"iss": ISS}
    assert store.smart_take("state-1", "pending") is None  # works once
    store.smart_put("old", "session", {"patient": "p"}, now - 1)
    assert store.smart_get("old", "session") is None
    store.smart_put("sid", "session", {"patient": "p"}, now + 60)
    store.smart_delete("sid")
    assert store.smart_get("sid", "session") is None
