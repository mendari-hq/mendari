# SPDX-License-Identifier: Apache-2.0
"""Organization sign-in, roles and API keys (server/auth.py), against a fake identity provider."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from starlette.routing import Route

from mendari.core import tenancy
from mendari.core.audit import MACHINE_ROLES, AuditStore
from mendari.errors import AuthError, MendariError
from mendari.server import auth as auth_module
from mendari.server import scopes as scopes_module
from mendari.server.app import MAX_SEARCH_FORM_BYTES, create_app
from mendari.server.auth import (
    CDS_PATH,
    Auth,
    AuthSettings,
    auth_settings,
    permission_for,
    read_body,
)
from mendari.server.backend_services import check_jwks, default_scope
from mendari.server.cds_clients import CdsClient, CdsOpenMode, CdsRegistry
from mendari.server.scopes import DECIDE, OUTCOME, needed, parse, parse_lenient
from mendari.standards.fhir_client import GuardedHttp
from mendari.standards.smart import Registration
from tests.conftest import (
    DASHBOARD,
    calm_agent,
    encrypt_mode,
    launched,
    message,
    run_async,
    signed_in,
    without_request_id,
)
from tests.jwt_signers import cds_token, forged
from tests.support.fixture_records import SHOWCASE
from tests.support.platform import CHART
from tests.test_cds_clients import BASE, EHR, RSA_EHR

IDP = "https://idp.example.org"
REALM = 'Bearer realm="mendari"'
INVALID = f'{REALM}, error="invalid_token"'
SEARCHED = ("Task", "AuditEvent", "Provenance", "CodeSystem")
CLIENT = "mendari-org"
INBOX = "mendari-test/triage"  # the agent these tests decide with
ROUTINE = "Can you send a refill for metformin to my pharmacy on Main Street?"
# Its providers that automate with sign-in on: its router and its screen's agent are a live
# service (`calm_agent`), the screen's checked by its keyword rules. A refill is automated; a
# message with chest pain waits for a person.
CHECKED = {"router": "agent", "screen": "checked"}
# What the server still holds of features wave 6 removes, until their code goes: the routes of
# Da Vinci's DTR package (G4) and of the guard (G12), and hidden evaluation sets' permissions and
# role (G13). Set aside, so these tables hold what stays.
REMOVED_ROUTES = {
    ("POST", "/fhir/Questionnaire/$questionnaire-package"),
    ("POST", "/api/agents/{namespace}/{name}/guard"),
}
REMOVED_PERMISSIONS = {"curate", "grade"}
REMOVED_ROLES = {"grading"}


class FakeIdp:
    def __init__(self, token_url: str = f"{IDP}/token", jwks_url: str = f"{IDP}/jwks") -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.codes: dict[str, dict[str, Any]] = {}
        self.claims: dict[str, Any] = {}
        self.token_url = token_url  # Google's, for one, are not on the issuer's host
        self.jwks_url = jwks_url
        self.authorize_url = f"{IDP}/authorize"  # may carry a query of its own (RFC 6749 3.1)
        self.discovered = 0  # fetches of the OpenID configuration

    def authorize(self, url: str, **claims: Any) -> str:
        params = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        code = f"code-{len(self.codes)}"
        self.codes[code] = {"nonce": params["nonce"], "claims": claims}
        return code

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{IDP}/.well-known/openid-configuration":
            self.discovered += 1
            return httpx.Response(
                200,
                json={
                    "issuer": IDP,
                    "authorization_endpoint": self.authorize_url,
                    "token_endpoint": self.token_url,
                    "jwks_uri": self.jwks_url,
                },
            )
        if url == self.jwks_url:
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
            return httpx.Response(200, json={"keys": [{**jwk, "kid": "k1"}]})
        if url == self.token_url:
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            code = self.codes.pop(form["code"], None)
            if code is None or not form.get("code_verifier"):
                return httpx.Response(400, json={"error": "invalid_grant"})
            now = int(time.time())
            claims = {
                "iss": IDP,
                "aud": CLIENT,
                "exp": now + 300,
                "iat": now,
                "nonce": code["nonce"],
                "sub": "u1",
                **code["claims"],
            }
            token = jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "k1"})
            return httpx.Response(
                200, json={"access_token": "a", "token_type": "Bearer", "id_token": token}
            )
        return httpx.Response(404)


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp()


@pytest.fixture
def app(tmp_path: Path, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Routine messages are automated: the triage fixture's screen asks a live agent service,
    checked by its rules (`CHECKED`)."""
    monkeypatch.setenv("MENDARI_OIDC_ROLE_MAP", "clinicians=reviewer")
    calm_agent(monkeypatch)
    with signing_in_with(idp, tmp_path, monkeypatch, providers=CHECKED) as client:
        yield client


def signing_in_with(
    idp: FakeIdp,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    providers: dict[str, str] | None = None,
) -> TestClient:
    """A server whose sign-in uses `idp` (the role settings come from the test's environment),
    called as the dashboard's browser calls it: with Mendari's own request header."""
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", CLIENT)
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    transport = httpx.MockTransport(idp)
    monkeypatch.setattr(
        auth_module, "GuardedHttp", lambda **kw: GuardedHttp(transport=transport, **kw)
    )
    app = create_app(db_path=tmp_path / "a.db", providers=providers)
    return TestClient(app, base_url="https://mendari.test", headers=DASHBOARD)


def sign_in(app: TestClient, idp: FakeIdp, **claims: Any) -> Any:
    app.cookies.clear()
    started = app.get("/auth/login", params={"next": "/#review"}, follow_redirects=False)
    code = idp.authorize(started.headers["location"], **claims)
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    return app.get("/auth/callback", params={"code": code, "state": state}, follow_redirects=False)


def test_the_dashboard_and_api_need_a_sign_in(app: TestClient) -> None:
    page = app.get("/", follow_redirects=False)
    assert page.status_code == 302 and page.headers["location"] == "/auth/login?next=/"
    assert app.get("/api/audit").status_code == 401
    assert app.get("/api/health").status_code == 200  # load balancers still see it
    assert app.get("/api/health").json()["sign_in"] is True
    assert app.get("/api/auth/me").json() == {"mode": "sign-in", "signed_in": False}


def test_signing_in_starts_a_session_with_the_mapped_role(app: TestClient, idp: FakeIdp) -> None:
    done = sign_in(app, idp, name="Nurse Kim", roles=["clinicians"])
    assert done.status_code == 302 and done.headers["location"] == "/#review"
    [cookie] = [c for c in done.headers.get_list("set-cookie") if "mendari_session" in c]
    assert cookie.startswith("__Host-mendari_session=")  # this host only, never a sibling's
    cookie = cookie.lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    assert "path=/" in cookie and "domain" not in cookie
    assert "max-age" not in cookie and "expires" not in cookie  # ends with the browser too
    me = app.get("/api/auth/me").json()
    assert me["principal"] == {
        "kind": "user",
        "name": "Nurse Kim",
        "roles": ["reviewer"],
        "permissions": ["decide", "discover", "outcome", "read", "read_input", "review"],
        "org": "default",
        "actor": f"{IDP}|u1",  # no verified email: the subject, qualified by its issuer
    }
    assert app.post("/auth/logout").status_code == 204
    assert app.get("/api/audit").status_code == 401


def test_each_role_can_do_exactly_its_part(app: TestClient, idp: FakeIdp) -> None:
    decide = f"/api/agents/{INBOX}/decide"
    evals = f"/api/agents/{INBOX}/evals"
    sign_in(app, idp, roles=["viewer"])
    assert app.get("/api/audit").status_code == 200
    assert app.post(decide, json=message(ROUTINE)).status_code == 403
    sign_in(app, idp, roles=["clinicians"])  # reviewer
    decided = app.post(decide, json=message(ROUTINE))
    assert decided.status_code == 200
    assert app.post(evals, json={"dataset": "test", "limit": 1}).status_code == 403
    assert app.post("/api/auth/keys", json={"name": "x", "role": "viewer"}).status_code == 403
    sign_in(app, idp, roles=["admin"])
    assert app.post("/api/auth/keys", json={"name": "worker", "role": "service"}).status_code == 201


def test_reviews_and_ratings_are_recorded_for_whoever_is_signed_in(
    app: TestClient, idp: FakeIdp
) -> None:
    """Under their verified email, never the display name (anyone can rename themselves at the
    identity provider) nor what the body says."""
    kim = {"email": "kim@clinic.example.org", "email_verified": True}
    sign_in(app, idp, name="Nurse Kim", roles=["clinicians"], **kim)
    emergency = message("Crushing chest pain and my left arm is numb")  # waits for review
    decided = app.post(f"/api/agents/{INBOX}/decide", json=emergency).json()
    posed_as = {"final_label": "nurse", "reviewer": "Dr Somebody Else"}
    assert app.post(f"/api/review/{decided['id']}", json=posed_as).status_code == 200
    rating = {"rating": "up", "reporter": "Dr Somebody Else"}
    assert app.post(f"/api/audit/{decided['id']}/feedback", json=rating).status_code == 200
    record = app.get(f"/api/audit/{decided['id']}").json()
    assert record["reviewer"] == "kim@clinic.example.org"
    assert [f["reporter"] for f in app.get("/api/feedback").json()] == ["kim@clinic.example.org"]
    assert app.get("/api/auth/me").json()["principal"]["actor"] == "kim@clinic.example.org"


@pytest.mark.parametrize(
    ("claims", "chained"),
    [
        ({"email": "kim@clinic.example.org", "email_verified": True}, "kim@clinic.example.org"),
        ({"email": "kim@clinic.example.org", "email_verified": "true"}, f"{IDP}|u1"),
        ({"email": "kim@clinic.example.org"}, f"{IDP}|u1"),  # anyone can claim an address
        ({}, f"{IDP}|u1"),
    ],
)
def test_people_are_chained_by_verified_email_else_by_issuer_and_subject(
    app: TestClient, idp: FakeIdp, claims: dict[str, Any], chained: str
) -> None:
    """The owner's choice (ADR-0005): chained events are never rewritten, so the chain names a
    person by what the identity provider vouches for, never by a display name."""
    sign_in(app, idp, name="Nurse Kim", roles=["clinicians"], **claims)
    emergency = message("Crushing chest pain and my left arm is numb")  # waits for review
    decided = app.post(f"/api/agents/{INBOX}/decide", json=emergency).json()
    app.post(f"/api/review/{decided['id']}", json={"final_label": "nurse", "reviewer": "x"})
    record = app.get(f"/api/audit/{decided['id']}").json()
    assert record["reviewer"] == chained
    assert "Nurse Kim" not in json.dumps(record["events"])


def test_open_mode_names_are_identifiers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """In open mode the client names the reviewer, reporter and approver; the chain keeps the
    name for good, so it must be an identifier: no spaces, at most 100 characters."""
    monkeypatch.delenv("MENDARI_OIDC_ISSUER", raising=False)
    client = TestClient(create_app(db_path=tmp_path / "open.db"))
    emergency = message("Crushing chest pain and my left arm is numb")  # waits for review
    decided = client.post(f"/api/agents/{INBOX}/decide", json=emergency).json()
    review, rate = f"/api/review/{decided['id']}", f"/api/audit/{decided['id']}/feedback"
    for name in ("Dr Somebody", "x" * 101, "nurse\tkim", "nurse\nkim", " nurse", "nurse\u00a0kim"):
        refused = client.post(review, json={"final_label": "nurse", "reviewer": name})
        assert refused.status_code == 422 and "reviewer must be an identifier" in refused.text
        refused = client.post(rate, json={"rating": "up", "reporter": name})
        assert refused.status_code == 422 and "reporter must be an identifier" in refused.text
        approval = {"pack_id": "mendari-test/clinical-review", "reference": "minutes 1"}
        refused = client.post("/api/governance", json={**approval, "approver": name})
        assert refused.status_code == 422 and "approver must be an identifier" in refused.text
    assert client.post(rate, json={"rating": "up", "reporter": "nurse.kim@clinic"}).is_success
    assert client.post(rate, json={"rating": "up"}).is_success  # no one named: allowed
    done = client.post(review, json={"final_label": "nurse", "reviewer": "sandbox-nurse"})
    assert done.status_code == 200
    assert client.get(f"/api/audit/{decided['id']}").json()["reviewer"] == "sandbox-nurse"


def test_an_account_without_a_role_is_refused(app: TestClient, idp: FakeIdp) -> None:
    done = sign_in(app, idp, roles=["marketing"])
    assert done.status_code == 403 and "no Mendari role" in done.text


def test_api_keys_work_for_machines_are_hashed_and_revocable(
    app: TestClient, idp: FakeIdp, tmp_path: Path
) -> None:
    sign_in(app, idp, roles=["admin"])
    created = app.post("/api/auth/keys", json={"name": "ehr integration", "role": "service"}).json()
    secret = created["key"]
    app.cookies.clear()
    machine = {"authorization": f"Bearer {secret}"}
    assert (
        app.post(f"/api/agents/{INBOX}/decide", json=message(ROUTINE), headers=machine).status_code
        == 200
    )
    assert app.get(f"/api/agents/{INBOX}/feedback/export", headers=machine).status_code == 403
    with AuditStore(tmp_path / "a.db")._db.session(exclusive=False) as db:
        stored = [row["key_sha256"] for row in db.all("SELECT key_sha256 FROM api_keys")]
    assert stored == [hashlib.sha256(secret.encode()).hexdigest()]
    sign_in(app, idp, roles=["admin"])
    assert [k["name"] for k in app.get("/api/auth/keys").json()] == ["ehr integration"]
    assert "key" not in app.get("/api/auth/keys").json()[0]
    assert app.delete(f"/api/auth/keys/{created['id']}").status_code == 204
    app.cookies.clear()
    assert app.get("/api/audit", headers=machine).status_code == 401
    assert app.get("/api/audit", headers={"authorization": "Bearer ohk_made_up"}).status_code == 401


def test_the_return_address_stays_on_this_site(app: TestClient, idp: FakeIdp) -> None:
    started = app.get(
        "/auth/login", params={"next": "//evil.example/steal"}, follow_redirects=False
    )
    code = idp.authorize(started.headers["location"], roles=["viewer"])
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    done = app.get("/auth/callback", params={"code": code, "state": state}, follow_redirects=False)
    assert done.headers["location"] == "/"


def test_a_sign_in_works_once(app: TestClient, idp: FakeIdp) -> None:
    started = app.get("/auth/login", follow_redirects=False)
    code = idp.authorize(started.headers["location"], roles=["viewer"])
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    assert (
        app.get(
            "/auth/callback", params={"code": code, "state": state}, follow_redirects=False
        ).status_code
        == 302
    )
    again = app.get("/auth/callback", params={"code": code, "state": state})
    assert again.status_code == 400 and "unknown, used or expired" in again.text


def test_open_mode_is_the_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MENDARI_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("MENDARI_OIDC_CLIENT_ID", raising=False)
    client = TestClient(create_app(db_path=tmp_path / "a.db"))
    assert client.get("/api/auth/me").json()["mode"] == "open"
    assert client.get("/api/audit").status_code == 200
    assert client.get("/auth/login").status_code == 404


@pytest.mark.parametrize(
    ("env", "message_part"),
    [
        ({"MENDARI_OIDC_ISSUER": IDP}, "set both"),
        (
            {"MENDARI_OIDC_ISSUER": "http://idp.example.org", "MENDARI_OIDC_CLIENT_ID": "c"},
            "https",
        ),
        (
            {
                "MENDARI_OIDC_ISSUER": IDP,
                "MENDARI_OIDC_CLIENT_ID": "c",
                "MENDARI_OIDC_ROLE_MAP": "a=root",
            },
            "not a role",
        ),
    ],
)
def test_bad_settings_fail_at_startup(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], message_part: str
) -> None:
    for name in ("MENDARI_OIDC_ISSUER", "MENDARI_OIDC_CLIENT_ID", "MENDARI_OIDC_ROLE_MAP"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(MendariError, match=message_part):
        auth_settings()


# Mounted only with SMART Backend Services on: its discovery and its token endpoint.
BACKEND_PUBLIC = {("GET", "/fhir/.well-known/smart-configuration"), ("POST", "/auth/token")}
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC = {
    ("GET", "/api/health"),
    # Readiness, for status pages, uptime checks and probes (HEAD as many send it): one cached,
    # bounded database read per instance whoever asks.
    ("GET", "/api/health/ready"),
    ("HEAD", "/api/health/ready"),
    ("GET", "/api/auth/me"),
    ("GET", "/favicon.ico"),
    ("GET", "/auth/login"),
    ("GET", "/auth/callback"),
    ("POST", "/auth/logout"),
    ("GET", "/smart/launch"),
    ("GET", "/smart/callback"),
    ("GET", "/api/smart/session"),
    ("POST", "/api/smart/logout"),
    # How to talk to the FHIR base: discoverable without signing in.
    ("GET", "/fhir/metadata"),
    ("GET", "/fhir/OperationDefinition/{resource_id}"),
    # The developer landing (static), and a redirect from ReDoc's old address to the reference.
    ("GET", "/docs"),
    ("GET", "/redoc"),
    # The status page: static, it reads readiness in the browser.
    ("GET", "/status"),
    # The scheduled purge and monitor: their own bearer secret (CRON_SECRET), checked there.
    ("GET", "/api/internal/retention"),
    ("GET", "/api/internal/monitor"),
    ("GET", "/api/internal/checkpoint"),
    # A backup reported by the schedule that made it: the same secret, checked there.
    ("POST", "/api/internal/backup"),
    # The signed daily checkpoint (LP-21k): a hash and a day, published.
    ("GET", "/api/audit/checkpoint"),
}


def test_every_route_has_the_permission_it_should(tmp_path: Path) -> None:
    """The matrix: nothing is public by accident, and writes need more than reading."""
    app = create_app(db_path=tmp_path / "a.db")
    routes = sorted(
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
    )
    public = {(m, p) for m, p in routes if permission_for(m, p) == "public"}
    assert public == PUBLIC
    # POST [type]/_search is a FHIR search with its criteria in a form body: a read (R4).
    searches = {(m, p) for m, p in routes if m == "POST" and p.endswith("/_search")}
    assert {permission_for(m, p) for m, p in searches} == {"read"}
    writes = [
        (m, p)
        for m, p in routes
        if m in ("POST", "PUT", "PATCH", "DELETE") and (m, p) not in public | searches
    ]
    # Nothing that writes needs only what every role holds (`read`, `discover`).
    assert all(permission_for(m, p) not in ("read", "discover") for m, p in writes), writes
    # `discover` reaches the two lists of agents to decide with, and nothing else.
    assert {r for r in routes if permission_for(*r) == "discover"} == {
        ("GET", "/api/agents"),
        ("GET", "/cds-services"),
    }
    assert permission_for("POST", "/api/agents/a/b/evals") == "evaluate"
    assert permission_for("POST", "/fhir/$decide") == "decide"
    # CDS Hooks card feedback (LP-23f): its own rule before the catch-all, which needs `admin`.
    assert permission_for("POST", "/cds-services/s1/feedback") == "decide"
    assert permission_for("POST", "/cds-services/s1/other") == "admin"
    assert permission_for("GET", "/fhir/CodeSystem/x") == "read"  # pack labels and criteria
    assert permission_for("GET", "/fhir/Patient/p1") == "admin"  # not a conformance resource
    assert permission_for("GET", "/fhir/Task/t1/_history/2") == "read"
    assert permission_for("GET", "/fhir/Task/t1/_history") == "admin"  # no history Bundles
    assert permission_for("POST", "/fhir/Task") == "admin"  # no create
    # The pages: a signed-out browser is sent to sign in. FastAPI's own docs pages are gone.
    pages = {(m, p) for m, p in routes if permission_for(m, p) == "page"}
    assert pages == {("GET", "/"), ("GET", "/docs/reference")}
    assert not [p for _, p in routes if p.startswith("/docs/") and p != "/docs/reference"]
    # Managing keys and clients: people only (`keys`), and nothing under them falls to read.
    keys = {(m, p) for m, p in routes if p.startswith("/api/auth/keys")}
    assert keys and {permission_for(m, p) for m, p in keys} == {"keys"}
    assert permission_for("GET", "/api/auth/clients") == "keys"
    assert permission_for("GET", "/api/auth/clients/c1") == "keys"
    assert permission_for("GET", "/api/audit/verify") == "admin"
    assert permission_for("GET", "/api/audit/export/fhir") == "admin"
    assert permission_for("GET", "/api/audit/administrative") == "admin"
    assert permission_for("GET", "/api/usage") == "admin"  # what the organization spent
    assert permission_for("GET", "/api/audit/administrative/export/fhir") == "admin"
    assert permission_for("GET", "/api/audit/d1") == "read"
    # The deployment's readiness and its operator's actions: `operate`, for every method and
    # path under theirs, before the catch-all read.
    system = {(m, p) for m, p in routes if p.startswith("/api/system")}
    assert system == {
        ("GET", "/api/system/checks"),
        ("POST", "/api/system/audit/verify"),
        ("POST", "/api/system/alerts/test"),
        ("POST", "/api/system/ehr-check"),
    }
    assert {permission_for(m, p) for m, p in system} == {"operate"}
    for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"):
        for path in ("/api/system", "/api/system/", "/api/system/x/y"):
            assert permission_for(method, path) == "operate", (method, path)
    assert permission_for("GET", "/api/systems") == "read"  # another path: not theirs
    # A person's accountability: no machine role has these (ROLE_PERMISSIONS below).
    assert permission_for("POST", "/api/review/d1") == "review"
    assert permission_for("POST", "/api/audit/d1/feedback") == "review"
    assert permission_for("POST", "/api/governance") == "approve"
    assert permission_for("DELETE", "/api/governance/a1") == "approve"
    assert permission_for("POST", "/api/audit/d1/outcome") == "outcome"
    # An EHR launch's own routes: only an `ehr` principal holds these (server/auth.py EHR_ONLY).
    launch = {(m, p) for m, p in routes if permission_for(m, p) in auth_module.EHR_ONLY}
    assert launch == {
        ("GET", "/api/smart/patient"),
        ("GET", "/api/smart/focus"),
        ("POST", "/api/smart/decide"),
    }
    assert permission_for("POST", "/api/smart/decide") == "launch_decide"
    # A grader's routes over written output (ADR-0025, memberships mode: not mounted here):
    # `label`, people only, never `read` or `discover`, whatever the catch-all says.
    for method, path in WRITTEN_GRADING:
        assert permission_for(method, re.sub(r"\{[^}]+\}", "x1", path)) == "label", path
    assert permission_for("GET", "/api/evals") == "read"


# A grader's routes over written output (ADR-0025): the labeller's role, a person's.
WRITTEN_GRADING = {
    ("GET", "/api/written/labelling"),
    ("GET", "/api/written/labelling/next"),
    ("POST", "/api/written/labelling/{assignment_id}"),
}


# -- the matrix: route x principal kind x role x scope (ADR-0004) -------------------------------

# What each role may do (server/auth.py PERMISSIONS), written out as the oracle.
ROLE_PERMISSIONS = {  # every role also lists the packs (`discover`, ADR-0004 as amended)
    "viewer": {"read", "discover"},
    "reviewer": {"read", "discover", "read_input", "decide", "review", "outcome"},
    "service": {"read", "discover", "decide", "outcome"},
    "governance": {"read", "discover", "approve", "promote"},
    "admin": {
        "read",
        "discover",
        "read_input",
        "decide",
        "review",
        "outcome",
        "approve",
        "evaluate",
        "admin",
        "keys",
        "promote",
        # The deployment's readiness and its operator's actions: here every administrator's
        # (claims mode, no operator organization named; tests/test_operators.py has the rest).
        "operate",
    },
    # A labeller grades written output (ADR-0025): a person, memberships mode only.
    "labeller": {"label"},
    # The pack registry's reviewers (LP-19e): in its one review organization only.
    "registry_reviewer": {"registry_review"},
    # An organization's pack store (LP-20f): its authors, people only.
    "pack_author": {"author"},
}
PEOPLE_ONLY = {"keys", "label", "registry_review", "author", "promote", "operate"}
# Every route a scoped key (or token) may reach, and the one scope that reaches it. Everything
# else that is not public is a 403 for a scoped principal, whatever its role.
ROUTE_SCOPE = {
    ("GET", "/fhir/Task/{resource_id}"): "system/Task.r",
    ("GET", "/fhir/Task/{resource_id}/_history/{version_id}"): "system/Task.r",
    ("GET", "/fhir/AuditEvent/{resource_id}"): "system/AuditEvent.r",
    ("GET", "/fhir/Provenance/{resource_id}"): "system/Provenance.r",
    ("GET", "/fhir/CodeSystem/{resource_id}"): "system/CodeSystem.r",
    ("GET", "/fhir/NamingSystem/{resource_id}"): "system/NamingSystem.r",
    **{("GET", f"/fhir/{kind}"): f"system/{kind}.s" for kind in SEARCHED},
    **{("POST", f"/fhir/{kind}/_search"): f"system/{kind}.s" for kind in SEARCHED},
    ("POST", "/api/agents/{namespace}/{name}/decide"): DECIDE,
    ("POST", "/api/agents/{namespace}/{name}/fhir/decide"): DECIDE,
    ("POST", "/api/agents/{namespace}/{name}/actions"): DECIDE,  # authorizing an action
    ("POST", "/cds-services/{service_id}"): DECIDE,
    ("POST", "/cds-services/{service_id}/feedback"): DECIDE,  # a card's feedback (LP-23f)
    ("POST", "/fhir/$decide"): DECIDE,
    ("POST", "/api/audit/{decision_id}/outcome"): OUTCOME,
    # Deciding implies listing the agents to decide with (`discover`, ADR-0004 as amended).
    ("GET", "/api/agents"): DECIDE,
    ("GET", "/cds-services"): DECIDE,
}
SCOPED_KEYS = [
    ("viewer", "system/*.rs"),
    ("viewer", "system/Task.r"),
    ("viewer", "system/Task.s system/NamingSystem.r"),
    ("viewer", "system/AuditEvent.read system/Provenance.rs"),
    ("service", DECIDE),
    ("service", OUTCOME),
    ("service", f"{DECIDE} {OUTCOME} system/*.rs"),
    ("service", "system/CodeSystem.rs system/*.r"),
]


def covers(scope: str, needed: str) -> bool:
    """The oracle for one granted scope: a URI exactly; a read scope by type (or `*`) and letter
    (v1 `.read` is `.rs`)."""
    if needed in (DECIDE, OUTCOME) or scope in (DECIDE, OUTCOME):
        return scope == needed
    kind, letter = needed.removeprefix("system/").split(".")
    granted, letters = scope.removeprefix("system/").split(".")
    return granted in ("*", kind) and letter in ("rs" if letters == "read" else letters)


# What an EHR launch may do, by its EHR's registered level (server/auth.py EHR_LEVELS).
EHR_PERMISSIONS = {"view": {"launch"}, "decide": {"launch", "launch_decide"}}
EHR_VIEW = "https://ehr-view.example.org/fhir"
EHR_DECIDE = "https://ehr-decide.example.org/fhir"


def expected(kind: str, role: str, scope: str | None, method: str, path: str) -> tuple[int, str]:
    """The status and `WWW-Authenticate` a request should get (200: it may go on). An EHR
    launch (`ehr`, `role` its level) is a credential on `/api/smart/` and the page `/` only;
    with a person's session too (`ehr+user`, `role` "<level>/<person's role>") the person is
    the principal everywhere else; with a bearer header (`ehr+key`, `ehr+junk`) the header
    decides alone, everywhere. A person's session alone is no credential on `/api/smart/`."""
    permission = permission_for(method, path)
    on_cds = CDS_PATH.match(path) is not None
    if kind in ("open-remote", "cds-open-remote") and on_cds:
        # Open mode answers CDS Hooks on this machine only (LP-23c), whatever is sent, a valid
        # token included, and before any route is public there.
        return 403, ""
    if kind in ("open", "open-remote") or permission == "public":
        return 200, ""
    if kind.startswith(("cds", "jwt")):  # a CDS client's token (LP-23b)
        if kind == "cds-open-remote":  # off `/cds-services`, open mode reads no credential
            return 200, ""
        if kind == "jwt-open":  # open mode without a registry: no JWT there, nothing else read
            return (401, INVALID) if on_cds else (200, "")
        if kind == "cds-open" and not on_cds:
            return 200, ""
        if kind == "cds+junk" or not on_cds:  # anywhere else it is not a credential
            return 401, INVALID
        if permission in {"discover", "decide"}:
            return 200, ""
        return 403, f'{REALM}, error="insufficient_scope"'
    if kind == "ehr+key":
        return expected("key", role, scope, method, path)
    if kind == "ehr+junk":
        return 401, INVALID
    if kind.startswith("ehr"):
        level, _, person = role.partition("/")
        if kind == "ehr+user" and person not in auth_module.ROLES:
            return expected("ehr", level, scope, method, path)  # the session gives nothing
        if path.startswith("/api/smart/"):
            if permission in EHR_PERMISSIONS[level]:
                return 200, ""
            return 403, f'{REALM}, error="insufficient_scope"'
        if kind == "ehr+user":
            return expected("user", person, scope, method, path)
        return (200, "") if path == "/" else expected("anonymous", "", None, method, path)
    if kind == "user" and path.startswith("/api/smart/"):
        return expected("anonymous", "", None, method, path)  # a session is no launch there
    if kind == "anonymous":
        return (302, "") if permission == "page" else (401, REALM)
    if kind == "user" and role not in auth_module.ROLES:
        # Claims mode: a reserved role never comes from a claim, and a person whose claims map
        # to no role is not signed in (`roles_from`).
        return expected("anonymous", "", None, method, path)
    allowed = ROLE_PERMISSIONS[role] - (set() if kind == "user" else PEOPLE_ONLY)
    if permission != "page" and permission not in allowed:
        return 403, f'{REALM}, error="insufficient_scope"'
    if scope is not None:
        needed = ROUTE_SCOPE.get((method, path))
        if needed is None or not any(covers(name, needed) for name in scope.split()):
            attribute = f', scope="{needed}"' if needed else ""
            return 403, f'{REALM}, error="insufficient_scope"{attribute}'
    return 200, ""


def attempt(auth: Auth, method: str, path: str, headers: dict[str, str]) -> tuple[int, str]:
    """What the guard answers (200: the request may go on), without running the route."""
    concrete = re.sub(r"\{[^}]+\}", "x1", path)
    request = Request(
        {
            "type": "http",
            "scheme": "https",
            "server": ("mendari.test", 443),
            "method": method,
            "path": concrete,
            "raw_path": concrete.encode(),
            "query_string": b"",
            "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
        }
    )
    try:
        refused = auth.check(request)
    except AuthError as error:
        return error.status, error.challenge or ""
    return (200, "") if refused is None else (refused.status_code, "")


def test_every_route_principal_role_and_scope_gets_what_it_should(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every route the app serves (from `app.routes`, SMART Backend Services on) for: open mode,
    no one, a person of each role, an unscoped key of each role (viewer and service as made now;
    the others as keys from before Phase 4), scoped keys, and access tokens of clients of each
    machine role (registered with everything the role can use, or with the scoped keys'
    scopes), and EHR launches at each level, alone, with a person's session, and with a bearer
    header, valid or not. A scope only narrows, and a scoped key or a token reaches only the
    routes `ROUTE_SCOPE` names, with that scope. Only an EHR launch reaches the launch's
    routes, and nothing else with its cookie. A CDS client's token (LP-23b), made for the very
    URL it is sent to, reaches the CDS services and nothing else, with sign-in on (alone, and
    with a person's session) and in open mode; a forged one, or any JWT in open mode without a
    registry, is refused there. Open mode without the sandbox flag answers the CDS services on
    this machine only (LP-23c): from elsewhere (the guard's requests come from no machine) each
    is a `403`, a valid token's too, which claims nothing, while every other route answers as
    before."""
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", CLIENT)
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    monkeypatch.setenv("MENDARI_PUBLIC_URL", "https://mendari.test")
    monkeypatch.setenv("MENDARI_BACKEND_SERVICES", "on")
    app = create_app(db_path=tmp_path / "routes.db")
    routes = sorted(
        (method, route.path)
        for route in app.routes
        if isinstance(route, Route)
        for method in route.methods or ()
        if (method, route.path) not in REMOVED_ROUTES
    )
    assert {r for r in routes if permission_for(*r) == "public"} == PUBLIC | BACKEND_PUBLIC
    assert {r for r in routes if r[1].startswith("/api/auth/clients")} == {
        ("GET", "/api/auth/clients"),
        ("POST", "/api/auth/clients"),
        ("DELETE", "/api/auth/clients/{client_id}"),
        ("GET", "/api/auth/clients/{client_id}/history"),
        ("POST", "/api/auth/clients/{client_id}/keys"),
    }
    assert set(ROUTE_SCOPE) <= set(routes)
    assert {r: needed(*r) for r in routes if needed(*r)} == ROUTE_SCOPE
    lists = {("GET", "/api/agents"), ("GET", "/cds-services")}
    for route, scope in ROUTE_SCOPE.items():  # a scope stands for the route's own permission
        implied = (
            "discover" if route in lists else next(iter(parse(scope).permissions - {"discover"}))
        )
        assert permission_for(*route) == implied, route
    assert {
        role: set(p) - REMOVED_PERMISSIONS
        for role, p in auth_module.PERMISSIONS.items()
        if role not in REMOVED_ROLES
    } == ROLE_PERMISSIONS

    settings = AuthSettings(
        issuer=IDP,
        client_id=CLIENT,
        client_secret=None,
        role_claim="roles",
        role_map={},
        org_claim=None,
        public_url=None,
    )
    store = AuditStore(tmp_path / "matrix.db")
    principals: list[tuple[str, str, str | None, dict[str, str]]] = [
        ("open", "admin", None, {}),
        ("anonymous", "", None, {}),
    ]
    for role in ROLE_PERMISSIONS:
        principals.append(("user", role, None, signed_in(store, role=role)))
        if role in MACHINE_ROLES:
            made = store.create_api_key(name=role, role=role, org="default", created_by="t")
            secret = made["key"]
        else:
            secret = legacy_key(tmp_path / "matrix.db", role)
        principals.append(("key", role, None, {"authorization": f"Bearer {secret}"}))
    for role, scope in SCOPED_KEYS:
        granted = parse(scope, permissions=auth_module.PERMISSIONS[role]).scope
        made = store.create_api_key(
            name=scope, role=role, org="default", created_by="t", scopes=granted
        )
        principals.append(("key", role, scope, {"authorization": f"Bearer {made['key']}"}))
    jwks = check_jwks(
        {"keys": [{**json.loads(RSAAlgorithm.to_jwk(_KEY.public_key())), "kid": "k"}]}
    )
    # (role, registered, asked): everything; less than registered; more than registered.
    everything = " ".join(scopes_module.supported())
    clients = [(role, default_scope(role), everything) for role in MACHINE_ROLES]
    clients += [(role, default_scope(role), scope) for role, scope in SCOPED_KEYS]
    clients += [(role, scope, everything) for role, scope in SCOPED_KEYS]
    for role, scope, asked in clients:
        registered = parse(scope, permissions=auth_module.PERMISSIONS[role]).scope
        client = store.register_client(
            name=scope, role=role, org="default", created_by="t", scopes=registered, jwks=jwks
        )
        granted = parse_lenient(asked).narrow(parse(registered)).scope
        token, _ = store.issue_token(client["client_id"], granted, 300)
        principals.append(("client", role, granted, {"authorization": f"Bearer {token}"}))

    ehrs = {
        EHR_VIEW: Registration(base=EHR_VIEW, org="default", level="view"),
        EHR_DECIDE: Registration(base=EHR_DECIDE, org="default", level="decide"),
    }
    service_key = store.create_api_key(name="ehr", role="service", org="default", created_by="t")
    for level, base in (("view", EHR_VIEW), ("decide", EHR_DECIDE)):
        cookie = launched(store, iss=base)["cookie"]
        principals.append(("ehr", level, None, {"cookie": cookie}))
        for person in ROLE_PERMISSIONS:
            both = f"{cookie}; {signed_in(store, role=person)['cookie']}"
            principals.append(("ehr+user", f"{level}/{person}", None, {"cookie": both}))
        bearer = {"authorization": f"Bearer {service_key['key']}", "cookie": cookie}
        principals.append(("ehr+key", "service", None, bearer))
        junk = {"authorization": "Bearer ohk_not-a-key", "cookie": cookie}
        principals.append(("ehr+junk", "", None, junk))

    # CDS clients' tokens (LP-23b): one registered client, its key by value; each request gets
    # a fresh token addressed to the URL it is sent to.
    epic = CdsClient(
        id="epic", title="EHR", iss=EHR, org="default", fhir_servers=(BASE,),
        jwks=tuple(check_jwks({"keys": [RSA_EHR.jwk]})),
    )  # fmt: skip
    registry = CdsRegistry(public_url="https://mendari.test", clients=[epic])

    def cds(path: str) -> dict[str, str]:
        minted = cds_token(RSA_EHR, EHR, f"https://mendari.test{re.sub(r'{[^}]+}', 'x1', path)}")
        return {"authorization": f"Bearer {minted}"}

    unsigned = forged({"alg": "RS384", "typ": "JWT", "kid": "ehr-rsa"}, {"iss": EHR})
    person_cookie = signed_in(store, role="admin")["cookie"]
    tokens: dict[str, Any] = {
        "cds": cds,
        "cds+user": lambda path: {**cds(path), "cookie": person_cookie},
        "cds+junk": lambda path: {"authorization": f"Bearer {unsigned}"},
        "cds-open": cds,
        "jwt-open": cds,
        "cds-open-remote": cds,
    }
    principals += [(kind, "", None, {}) for kind in tokens]
    principals.append(("open-remote", "admin", None, {}))

    # Open mode as the suite runs it (the sandbox flag: the guard's requests come from no
    # machine), and as it runs without the flag, where only this machine's requests reach the
    # CDS services (LP-23c).
    anywhere, here_only = CdsOpenMode(sandbox=True), CdsOpenMode()
    guards = {
        "open": Auth(None, store, ehrs=ehrs, cds_open=anywhere),
        "cds-open": Auth(None, store, ehrs=ehrs, cds=registry, cds_open=anywhere),
        "jwt-open": Auth(None, store, ehrs=ehrs, cds_open=anywhere),
        "open-remote": Auth(None, store, ehrs=ehrs, cds=registry, cds_open=here_only),
        "cds-open-remote": Auth(None, store, ehrs=ehrs, cds=registry, cds_open=here_only),
    }
    signed = Auth(settings, store, tokens=True, ehrs=ehrs, cds=registry)

    def sent(kind: str, headers: dict[str, str], path: str) -> dict[str, str]:
        return dict(tokens[kind](path)) if kind in tokens else headers

    wrong = [
        (kind, role, scope, method, path, got, want)
        for kind, role, scope, headers in principals
        for method, path in routes
        for got, want in [
            (
                attempt(guards.get(kind, signed), method, path, sent(kind, headers, path)),
                expected(kind, role, scope, method, path),
            )
        ]
        if got != want
    ]
    assert wrong == []
    # Not vacuous: the registry was on, and each of the three kinds a valid token is verified
    # for (sign-in on alone and with a session, open mode) was let through on the three CDS
    # routes (discovery, a hook and its card feedback, LP-23f).
    assert signed.cds is registry and registry.clients
    with store._db.session(exclusive=False) as db:
        claimed = db.one("SELECT COUNT(*) AS n FROM smart_sessions WHERE kind = 'cds-jti'")
    assert claimed is not None and claimed["n"] == 3 * 3
    reached = {
        (method, path)
        for kind, role, scope, headers in principals
        if scope is not None
        for method, path in routes
        if attempt(signed, method, path, headers)[0] == 200
    }
    # Each reachable by some key.
    assert reached == PUBLIC | BACKEND_PUBLIC | set(ROUTE_SCOPE)


def test_no_fhir_data_route_is_public(tmp_path: Path) -> None:
    """Every `/fhir` route but the two that say how to talk to the server reads something an
    organization owns or a pack defines. Without a signed-in organization the store's scope is
    every organization's rows, so none of them may ever be public."""
    app = create_app(db_path=tmp_path / "a.db")
    fhir = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/fhir/")
        for method in route.methods or ()
    }
    discovery = {("GET", "/fhir/metadata"), ("GET", "/fhir/OperationDefinition/{resource_id}")}
    data = {(m, p) for m, p in fhir if p.split("/")[2] in ("Task", "AuditEvent", "Provenance")}
    assert len(data) == 10  # reads, the Task vread, and GET and POST search of each
    assert {permission_for(m, p) for m, p in data} == {"read"}
    assert all(permission_for(m, p) != "public" for m, p in fhir - discovery)
    assert permission_for("GET", "/api/agents/a/b/feedback/export") == "admin"
    assert permission_for("DELETE", "/api/something/new") == "admin"  # deny by default


def test_organizations_never_see_each_others_work(
    app: TestClient, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One deployment, two organizations: each sees only its own decisions, reviews and stats."""
    monkeypatch.setenv("MENDARI_OIDC_ORG_CLAIM", "org")
    transport = httpx.MockTransport(idp)
    monkeypatch.setattr(
        auth_module, "GuardedHttp", lambda **kw: GuardedHttp(transport=transport, **kw)
    )
    client = TestClient(
        create_app(db_path=tmp_path / "two-orgs.db"),
        base_url="https://mendari.test",
        headers=DASHBOARD,
    )
    keys = {}
    for org in ("clinic-a", "clinic-b"):
        sign_in(client, idp, roles=["admin"], org=org)
        body = {"name": f"{org} service", "role": "service"}
        created = client.post("/api/auth/keys", json=body).json()
        keys[org] = {"authorization": f"Bearer {created['key']}"}
        with AuditStore(tmp_path / "two-orgs.db") as store:  # reviewers are people
            keys[f"{org} reviewer"] = signed_in(store, role="reviewer", org=org)
    client.cookies.clear()
    emergency = message("Crushing chest pain and my left arm is numb")  # waits for review
    decided = client.post(
        f"/api/agents/{INBOX}/decide", json=emergency, headers=keys["clinic-a"]
    ).json()
    a_list = client.get("/api/audit", headers=keys["clinic-a"]).json()
    b_list = client.get("/api/audit", headers=keys["clinic-b"]).json()
    assert [d["id"] for d in a_list] == [decided["id"]] and b_list == []
    assert client.get(f"/api/audit/{decided['id']}", headers=keys["clinic-b"]).status_code == 404
    assert client.get("/api/stats", headers=keys["clinic-b"]).json()["decisions"] == 0
    assert client.get("/api/review", headers=keys["clinic-b"]).json() == []
    assert (
        client.post(
            f"/api/audit/{decided['id']}/outcome",
            json={"final_label": "nurse", "source": "x"},
            headers=keys["clinic-b"],
        ).status_code
        == 409
    )  # unknown, not a leak
    # The export is the whole chain: administrators only, and each sees its own organization's.
    assert client.get("/api/audit/export/fhir", headers=keys["clinic-b"]).status_code == 403
    assert client.get("/api/stats", headers=keys["clinic-a"]).json()["decisions"] == 1

    rated = client.post(
        f"/api/audit/{decided['id']}/feedback",
        json={"rating": "up"},
        headers=keys["clinic-a reviewer"],
    )
    assert rated.status_code == 200
    refused = client.post(
        f"/api/audit/{decided['id']}/feedback",
        json={"rating": "down"},
        headers=keys["clinic-b reviewer"],
    )
    assert refused.status_code == 409  # unknown to clinic-b
    assert len(client.get("/api/feedback", headers=keys["clinic-a"]).json()) == 1
    assert client.get("/api/feedback", headers=keys["clinic-b"]).json() == []

    report: dict[str, Any] = {"id": "r1", "created_at": "2026-09-30T00:00:00+00:00"}
    report |= {"pack": {"id": INBOX}, "dataset": {"name": "local"}}
    with tenancy.acting_for("clinic-a"):
        AuditStore(tmp_path / "two-orgs.db").save_report(report)
    assert [r["id"] for r in client.get("/api/evals", headers=keys["clinic-a"]).json()] == ["r1"]
    assert client.get("/api/evals", headers=keys["clinic-b"]).json() == []
    sign_in(client, idp, roles=["admin"], org="clinic-b")
    assert client.get("/api/audit/export/fhir").text == ""
    sign_in(client, idp, roles=["admin"], org="clinic-a")
    assert decided["id"] in client.get("/api/audit/export/fhir").text


def test_a_role_from_an_email_needs_the_provider_to_have_verified_it(
    tmp_path: Path, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """For providers without roles (Google): one person, one role, by verified email."""
    monkeypatch.setenv("MENDARI_OIDC_ROLE_CLAIM", "email")
    monkeypatch.setenv("MENDARI_OIDC_ROLE_MAP", "owner@example.org=admin")
    with signing_in_with(idp, tmp_path, monkeypatch) as app:
        unverified = sign_in(app, idp, email="owner@example.org", email_verified=False)
        assert unverified.status_code == 403
        claimed = sign_in(app, idp, email="owner@example.org", email_verified="true")
        assert claimed.status_code == 403  # a string is not the provider's boolean
        stranger = sign_in(app, idp, email="someone@example.org", email_verified=True)
        assert stranger.status_code == 403
        owner = sign_in(app, idp, email="owner@example.org", email_verified=True)
        assert owner.status_code == 302
        me = app.get("/api/auth/me").json()
        assert me["signed_in"] and me["principal"]["roles"] == ["admin"]


def test_the_providers_endpoints_on_other_hosts_must_be_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idp = FakeIdp(
        token_url="https://tokens.example.net/token", jwks_url="https://keys.example.net/k"
    )
    monkeypatch.setenv("MENDARI_OIDC_ROLE_MAP", "clinicians=reviewer")
    with signing_in_with(idp, tmp_path, monkeypatch) as app:
        refused = app.get("/auth/login", follow_redirects=False)
        assert refused.status_code == 502
        assert "MENDARI_OIDC_ENDPOINT_ORIGINS" in refused.text
    monkeypatch.setenv(
        "MENDARI_OIDC_ENDPOINT_ORIGINS", "https://tokens.example.net,https://keys.example.net"
    )
    with signing_in_with(idp, tmp_path, monkeypatch) as app:
        assert sign_in(app, idp, roles=["clinicians"]).status_code == 302
        assert app.get("/api/auth/me").json()["principal"]["roles"] == ["reviewer"]


@pytest.mark.parametrize("value", ["http://tokens.example.net", "https://keys.example.net/k"])
def test_endpoint_origins_must_be_https_origins(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", CLIENT)
    monkeypatch.setenv("MENDARI_OIDC_ENDPOINT_ORIGINS", value)
    with pytest.raises(MendariError, match="MENDARI_OIDC_ENDPOINT_ORIGINS"):
        auth_settings()


def test_the_api_reference_asks_a_browser_to_sign_in(app: TestClient, idp: FakeIdp) -> None:
    """The landing at /docs is public (tests/test_landing.py); the reference and the OpenAPI
    document behind it are not."""
    moved = app.get("/redoc", follow_redirects=False)  # FastAPI's ReDoc page was here
    assert (moved.status_code, moved.headers["location"]) == (308, "/docs/reference")
    landing = app.get("/docs", follow_redirects=False)
    assert landing.status_code == 200 and "Sign-in is on here" in landing.text
    docs = app.get("/docs/reference", follow_redirects=False)
    target = "/auth/login?next=/docs/reference"
    assert docs.status_code == 302 and docs.headers["location"] == target
    assert app.get("/openapi.json").status_code == 401
    sign_in(app, idp, roles=["clinicians"])
    reference = app.get("/docs/reference")
    assert reference.status_code == 200 and "swagger-ui-bundle.js" in reference.text
    schema = app.get("/openapi.json").json()
    schemes = schema["components"]["securitySchemes"]
    # No token scheme: Backend Services is off. The launch's cookie is its own scheme.
    assert set(schemes) == {"apiKey", "session", "smartLaunch"}
    session = schemes["session"]  # the cookie sign-in sets, on this (non-local) host
    assert session["name"] == "__Host-mendari_session" and "30 minutes" in session["description"]
    assert schemes["smartLaunch"]["name"] == "__Host-mendari_smart"
    operations = {
        (method.upper(), path): operation
        for path, methods in schema["paths"].items()
        for method, operation in methods.items()
    }
    assert operations[("GET", "/api/health")]["security"] == []
    decide = operations[("POST", "/api/agents/{namespace}/{name}/decide")]
    assert decide["security"] == [{"apiKey": []}, {"session": []}]
    assert "the `decide` permission" in decide["description"]
    # Only an EHR launch decides there: never a session or a key.
    launch_decide = operations[("POST", "/api/smart/decide")]
    assert launch_decide["security"] == [{"smartLaunch": []}]
    assert "registered here to decide" in launch_decide["description"]
    assert operations[("GET", "/api/smart/patient")]["security"] == [{"smartLaunch": []}]
    assert operations[("GET", "/api/smart/session")]["security"] == []
    assert "Sign-in is on" in schema["info"]["description"]


# -- bearer credentials (ADR-0004) ---------------------------------------------------------------


def legacy_key(db: Path | str, role: str, org: str = "default") -> str:
    """A key of a role no route makes (keys are viewer or service, ADR-0004), with no scopes,
    written past the store as someone with the database could: whatever its role, a key never
    does what only a person may. It expires as every key does."""
    secret = "ohk_" + secrets.token_urlsafe(32)
    row = (secrets.token_hex(6), hashlib.sha256(secret.encode()).hexdigest(), f"old {role}")
    expires = (datetime.now(UTC) + timedelta(days=30)).isoformat(timespec="milliseconds")
    with AuditStore(db) as store, store._db.session(exclusive=True) as session:
        session.all(
            "INSERT INTO api_keys (id, key_sha256, name, role, org, created_by, created_at, "
            "expires_at) VALUES (?, ?, ?, ?, ?, 'before', '2026-09-01T00:00:00.000+00:00', ?)",
            (*row, role, org, expires),
        )
    return secret


def key_for(app: TestClient, idp: FakeIdp, role: str = "service") -> str:
    sign_in(app, idp, roles=["admin"])
    secret = app.post("/api/auth/keys", json={"name": f"a {role}", "role": role}).json()["key"]
    app.cookies.clear()
    return str(secret)


@pytest.mark.parametrize(
    "header",
    [
        "Bearer junk",  # no prefix Mendari issues
        "Bearer ohk_unknown",  # a key's prefix, no such key
        "Bearer mat_0123456789",  # an access token, and Backend Services is off here
        "Bearer ohk_a b",  # not one token
        "Bearer ",
        "Bearer ohk_a=b",  # not a b64token
        "Bearer ohk_" + "a" * 600,  # longer than any credential Mendari issues
    ],
)
def test_a_credential_that_is_not_valid_never_falls_back_to_the_session(
    app: TestClient, idp: FakeIdp, header: str
) -> None:
    sign_in(app, idp, roles=["admin"])  # a valid session cookie rides along
    assert app.get("/api/audit").status_code == 200
    refused = app.get("/api/audit", headers={"authorization": header})
    assert refused.status_code == 401
    assert refused.headers["www-authenticate"] == INVALID
    assert without_request_id(refused.json(), refused) == {
        "error": "AuthError",
        "detail": "the credential is not valid",
    }
    me = app.get("/api/auth/me", headers={"authorization": header}).json()
    assert me == {"mode": "sign-in", "signed_in": False}


def test_a_bearer_header_never_falls_back_to_the_launch_cookie(
    tmp_path: Path, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0004 on the launch's routes too (threat S5): with an `Authorization` header the
    launch cookie riding along is never looked at. A credential that is not valid is a `401`;
    a valid key is a key, which no launch route admits (`403`); a person's session there is no
    credential at all."""
    ehr = "https://ehr.example.org/fhir"
    monkeypatch.setenv("MENDARI_SMART_CLIENT_ID", "mendari-app")
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", "https://ehr.example.org")
    monkeypatch.setenv("MENDARI_SMART_EHRS", f"{ehr}=default:decide")
    monkeypatch.setenv("MENDARI_OIDC_ROLE_MAP", "clinicians=reviewer")
    app = signing_in_with(idp, tmp_path, monkeypatch)
    store = AuditStore(tmp_path / "a.db")
    cookie = launched(store, iss=ehr)
    assert app.get("/api/smart/session", headers=cookie).json()["active"] is True
    key = store.create_api_key(name="worker", role="service", org="default", created_by="t")
    body = {"pack_id": CHART, "focus": f"Patient/{SHOWCASE.id}"}
    for header, status in (("Bearer ohk_not-a-key", 401), (f"Bearer {key['key']}", 403)):
        sent = {**cookie, "authorization": header}
        for method, path in (
            ("POST", "/api/smart/decide"),
            ("GET", "/api/smart/patient"),
            ("GET", f"/api/smart/focus?pack_id={CHART}"),
        ):
            refused = app.request(method, path, json=body, headers=sent)
            assert refused.status_code == status, (header, path, refused.text)
        assert app.get("/api/smart/session", headers=sent).json() == {"active": False}
    person = signed_in(store, role="reviewer")
    refused = app.post("/api/smart/decide", json=body, headers=person)
    assert refused.status_code == 401 and "open Mendari again from the EHR" in refused.text


@pytest.mark.parametrize("header", ["Basic dXNlcjpwYXNz", "Token ohk_x", "ohk_x", ""])
def test_another_scheme_is_refused_without_an_error_code(
    app: TestClient, idp: FakeIdp, header: str
) -> None:
    """RFC 6750: a request with no bearer credential gets the challenge, but no error code."""
    sign_in(app, idp, roles=["admin"])
    refused = app.get("/api/audit", headers={"authorization": header})
    assert refused.status_code == 401
    assert refused.headers["www-authenticate"] == REALM


def test_the_scheme_matches_in_any_case(app: TestClient, idp: FakeIdp) -> None:
    secret = key_for(app, idp)
    for scheme in ("Bearer", "bearer", "BEARER", "bEaReR"):
        sent = app.get("/api/audit", headers={"authorization": f"{scheme} {secret}"})
        assert sent.status_code == 200, scheme


def test_two_authorization_headers_are_refused(app: TestClient, idp: FakeIdp) -> None:
    secret = key_for(app, idp)
    both = [("authorization", f"Bearer {secret}"), ("authorization", "Bearer ohk_other")]
    refused = app.get("/api/audit", headers=both)
    assert refused.status_code == 400
    assert refused.headers["www-authenticate"] == f'{REALM}, error="invalid_request"'


def test_a_refusal_never_echoes_the_credential(app: TestClient, idp: FakeIdp) -> None:
    secret = key_for(app, idp)
    sign_in(app, idp, roles=["admin"])
    app.delete(f"/api/auth/keys/{app.get('/api/auth/keys').json()[0]['id']}")
    app.cookies.clear()
    for path in ("/api/audit", "/fhir/Task"):
        refused = app.get(path, headers={"authorization": f"Bearer {secret}"})
        assert refused.status_code == 401 and secret not in refused.text
        assert refused.headers["www-authenticate"] == INVALID


@pytest.mark.parametrize(
    ("method", "path", "form"),
    [
        ("GET", "/api/audit?access_token={key}", None),
        ("GET", "/api/health?Access_Token={key}", None),  # public routes too, any case
        ("GET", "/fhir/Task?access%5Ftoken={key}", None),
        ("GET", "/?access_token={key}", None),
        ("POST", "/fhir/Task/_search", "status=completed&access_token={key}"),
        ("POST", "/fhir/Task/_search", "ACCESS%5FTOKEN+={key}"),
    ],
)
def test_a_credential_in_a_url_or_a_form_is_refused(
    app: TestClient, idp: FakeIdp, method: str, path: str, form: str | None
) -> None:
    """A form is read by its route, after the sign-in check: there the request also carries a
    valid credential, and the one in the form is refused all the same."""
    secret = key_for(app, idp, role="viewer")
    content = form.format(key=secret) if form else None
    headers = {"content-type": "application/x-www-form-urlencoded"} if form else {}
    if form:
        headers["authorization"] = f"Bearer {key_for(app, idp, role='viewer')}"
    refused = app.request(method, path.format(key=secret), content=content, headers=headers)
    assert refused.status_code == 400
    assert refused.headers["www-authenticate"] == f'{REALM}, error="invalid_request"'
    assert secret not in refused.text


def test_no_body_is_read_before_the_sign_in_check(
    app: TestClient, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller without a credential never makes Mendari read a body, however large."""

    async def unread(*args: object) -> AsyncIterator[bytes]:  # Starlette makes one, unread
        raise AssertionError("a body was read before the sign-in check")
        yield b""

    async def unread_body(*args: object) -> bytes:
        raise AssertionError("a body was read before the sign-in check")

    monkeypatch.setattr(Request, "stream", unread)
    monkeypatch.setattr(Request, "body", unread_body)
    form = {"content-type": "application/x-www-form-urlencoded"}
    for path in ("/api/governance", "/fhir/Task/_search", "/api/review/x", "/nowhere"):
        refused = app.post(path, content=b"a&" * 1000, headers=form)
        assert refused.status_code == 401, path


def test_a_search_form_is_read_only_up_to_its_limit(app: TestClient, idp: FakeIdp) -> None:
    secret = key_for(app, idp, role="viewer")
    headers = {
        "authorization": f"Bearer {secret}",
        "content-type": "application/x-www-form-urlencoded",
    }
    found = app.post("/fhir/Task/_search", content="_summary=count", headers=headers)
    assert found.status_code == 200 and found.json()["total"] == 0
    padded = "_summary=count&pad=" + "x" * MAX_SEARCH_FORM_BYTES
    for content in (padded, iter([padded.encode()])):  # declared, then chunked
        too_large = app.post("/fhir/Task/_search", content=content, headers=headers)
        assert too_large.status_code == 413
        assert too_large.json()["issue"][0]["code"] == "too-costly"


def test_a_chunked_body_is_read_only_until_it_passes_the_limit() -> None:
    received = 0

    async def receive() -> dict[str, object]:
        nonlocal received
        received += 1
        return {"type": "http.request", "body": b"x" * 1024, "more_body": True}  # never ends

    request = Request({"type": "http", "method": "POST", "headers": []}, receive)
    assert run_async(read_body(request, 4096)) is None
    assert received == 5


def test_a_credential_in_a_url_is_refused_in_open_mode_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MENDARI_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("MENDARI_OIDC_CLIENT_ID", raising=False)
    client = TestClient(create_app(db_path=tmp_path / "a.db"))
    assert client.get("/api/audit", params={"access_token": "ohk_x"}).status_code == 400
    assert client.get("/api/audit").status_code == 200


def test_every_refusal_on_the_api_and_fhir_says_how_to_authenticate(
    app: TestClient, idp: FakeIdp
) -> None:
    """RFC 7235 and 6750: a 401 or 403 carries `WWW-Authenticate`; `insufficient_scope` when the
    credential is valid but not enough."""
    for path in ("/api/audit", "/fhir/Task", "/fhir/Task/x", "/openapi.json", "/api/smart/patient"):
        refused = app.get(path)
        assert refused.status_code == 401, path
        assert refused.headers["www-authenticate"] == REALM, path
    viewer = {"authorization": f"Bearer {key_for(app, idp, role='viewer')}"}
    for method, path in (
        ("POST", f"/api/agents/{INBOX}/decide"),
        ("POST", "/fhir/$decide"),
        ("GET", "/api/audit/verify"),
    ):
        refused = app.request(method, path, json={}, headers=viewer)
        assert refused.status_code == 403, path
        assert refused.headers["www-authenticate"] == f'{REALM}, error="insufficient_scope"'
    fhir = app.post("/fhir/$decide", json={}, headers=viewer)
    assert fhir.json()["resourceType"] == "OperationOutcome"
    assert app.get("/api/health").headers.get("www-authenticate") is None


def test_keys_are_managed_by_people_only(app: TestClient, idp: FakeIdp, tmp_path: Path) -> None:
    """A key never manages keys or clients, whatever its role (an admin key from before Phase 4
    included): a stolen key must not mint a credential that outlives it."""
    admin_key = {"authorization": f"Bearer {legacy_key(tmp_path / 'a.db', 'admin')}"}
    insufficient = f'{REALM}, error="insufficient_scope"'
    for method, path in (
        ("GET", "/api/auth/keys"),
        ("POST", "/api/auth/keys"),
        ("DELETE", "/api/auth/keys/abc"),
        ("GET", "/api/auth/keys/abc/history"),
        ("GET", "/api/auth/clients"),
        ("POST", "/api/auth/clients"),
        ("GET", "/api/auth/clients/abc/history"),
        ("POST", "/api/auth/clients/abc/keys"),
    ):
        refused = app.request(method, path, json={"name": "x", "role": "viewer"}, headers=admin_key)
        assert refused.status_code == 403, (method, path)
        assert refused.headers["www-authenticate"] == insufficient
        assert refused.json()["detail"] == "only a person who signed in may do this"
    assert app.get("/api/audit/verify", headers=admin_key).status_code == 200  # still an admin
    me = app.get("/api/auth/me", headers=admin_key).json()["principal"]
    assert me["kind"] == "key" and "admin" in me["permissions"] and "keys" not in me["permissions"]
    sign_in(app, idp, roles=["admin"])
    assert "keys" in app.get("/api/auth/me").json()["principal"]["permissions"]
    assert app.get("/api/auth/keys").status_code == 200
    sign_in(app, idp, roles=["governance"])
    assert app.get("/api/auth/keys").status_code == 403


def test_an_empty_organization_is_never_one_to_act_for(
    idp: FakeIdp, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An organization claim that names none ("" or blanks, or not a string) refuses the
    sign-in, never placing the person in `default`; a credential minted for a blank
    organization acts for none (403), never writing into `default` while reading nothing."""
    monkeypatch.setenv("MENDARI_OIDC_ORG_CLAIM", "org")
    client = signing_in_with(idp, tmp_path, monkeypatch)
    for claimed in ("", "   ", ["clinic-a"]):
        refused = sign_in(client, idp, roles=["admin"], org=claimed)
        assert refused.status_code == 403 and "organization claim is empty" in refused.text
        assert client.get("/api/audit").status_code == 401  # no session
    with AuditStore(tmp_path / "a.db") as store:
        blank = store.create_api_key(name="k", role="service", org=" ", created_by="ops")
    client.cookies.clear()
    machine = {"authorization": f"Bearer {blank['key']}"}
    assert client.get("/api/audit", headers=machine).status_code == 403
    decided = client.post(f"/api/agents/{INBOX}/decide", json=message(ROUTINE), headers=machine)
    assert decided.status_code == 403
    with AuditStore(tmp_path / "a.db") as store:
        assert store.query() == []  # nothing written into `default`


def test_claims_mode_reads_its_own_organizations_credential_history(
    idp: FakeIdp, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A key's history (LP-12h) in claims mode with an organization claim: an administrator
    reads their own organization's keys' only; another organization's key is a `404` that says
    no more than an unknown one, and neither a viewer nor a key may read one."""
    monkeypatch.setenv("MENDARI_OIDC_ORG_CLAIM", "org")
    client = signing_in_with(idp, tmp_path, monkeypatch)
    sign_in(client, idp, roles=["admin"], org="clinic-a", email="lead@example.org")
    made = client.post("/api/auth/keys", json={"name": "ehr", "role": "service"}).json()
    rotated = client.post(f"/api/auth/keys/{made['id']}/rotate", json={"overlap_hours": 1})
    assert rotated.status_code == 201
    history = client.get(f"/api/auth/keys/{made['id']}/history")
    assert history.status_code == 200
    assert [e["event"] for e in history.json()["events"]] == ["key.created", "key.rotated"]
    assert history.json()["org"] == "clinic-a"
    assert made["key"] not in history.text and rotated.json()["key"] not in history.text
    as_key = {"authorization": f"Bearer {rotated.json()['key']}"}
    sign_in(client, idp, roles=["admin"], org="clinic-b")
    for key_id in (made["id"], "0" * 12):  # another organization's, and none at all
        refused = client.get(f"/api/auth/keys/{key_id}/history")
        assert refused.status_code == 404
        assert refused.json()["detail"] == "no such key" and key_id not in refused.text
    sign_in(client, idp, roles=["viewer"], org="clinic-a")
    assert client.get(f"/api/auth/keys/{made['id']}/history").status_code == 403
    client.cookies.clear()
    refused = client.get(f"/api/auth/keys/{made['id']}/history", headers=as_key)
    assert refused.status_code == 403
    assert client.get(f"/api/auth/keys/{made['id']}/history").status_code == 401


def test_verifying_and_exporting_the_chain_need_admin(app: TestClient, idp: FakeIdp) -> None:
    for role in ("viewer", "clinicians", "governance"):
        sign_in(app, idp, roles=[role])
        assert app.get("/api/audit/verify").status_code == 403, role
        assert app.get("/api/audit/export/fhir").status_code == 403, role
        assert app.get("/api/audit").status_code == 200, role
    service = {"authorization": f"Bearer {key_for(app, idp)}"}
    assert app.get("/api/audit/verify", headers=service).status_code == 403
    sign_in(app, idp, roles=["admin"])
    assert app.get("/api/audit/verify").json()["ok"] is True
    assert app.get("/api/audit/export/fhir").status_code == 200


def test_api_answers_are_never_stored(app: TestClient, idp: FakeIdp) -> None:
    """`no-store` on /api, whoever asks and whatever the answer; not on the dashboard page or
    static files, which hold no data."""
    assert app.get("/api/health").headers["cache-control"] == "no-store"
    assert app.get("/api/audit").headers["cache-control"] == "no-store"  # a 401 too
    sign_in(app, idp, roles=["admin"])
    for path in ("/api/audit", "/api/auth/me", "/api/auth/keys", "/api/audit/export/fhir"):
        assert app.get(path).headers["cache-control"] == "no-store", path
    assert app.get("/").headers["cache-control"] == "no-cache"
    assert app.get("/favicon.ico").headers["cache-control"] == "public, max-age=86400"
    assert "no-store" not in app.get("/static/style.css").headers.get("cache-control", "")
    assert app.get("/fhir/metadata").headers["cache-control"] == "no-cache"


def test_an_outcome_is_recorded_for_whoever_sends_it(app: TestClient, idp: FakeIdp) -> None:
    """Like a review's reviewer: the signed-in person or the key, whatever the body says."""
    machine = {"authorization": f"Bearer {key_for(app, idp)}"}
    decided = app.post(f"/api/agents/{INBOX}/decide", json=message(ROUTINE), headers=machine)
    outcome = {"final_label": "nurse", "source": "ehr", "recorded_by": "Dr Somebody Else"}
    recorded = app.post(f"/api/audit/{decided.json()['id']}/outcome", json=outcome, headers=machine)
    key_id = app.get("/api/auth/me", headers=machine).json()["principal"]["actor"]
    assert re.fullmatch(r"key [0-9a-f]+", key_id)  # its id: the name is free text
    assert recorded.json()["recorded_by"] == key_id
    sign_in(app, idp, name="Nurse Kim", roles=["clinicians"])
    events = app.get(f"/api/audit/{decided.json()['id']}").json()["events"]
    assert outcomes(events)[-1]["recorded_by"] == key_id  # the reviewer's read chains after it
    assert "Dr Somebody Else" not in json.dumps(events)
    app.post(f"/api/audit/{decided.json()['id']}/outcome", json=outcome)
    events = app.get(f"/api/audit/{decided.json()['id']}").json()["events"]
    assert outcomes(events)[-1]["recorded_by"] == f"{IDP}|u1"  # no verified email


def outcomes(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The payloads of a decision's `outcome.recorded` events, in chain order."""
    return [e["payload"] for e in events if e["event"] == "outcome.recorded"]


def test_the_api_reference_says_keys_are_managed_by_people(app: TestClient, idp: FakeIdp) -> None:
    sign_in(app, idp, roles=["admin"])
    paths = app.get("/openapi.json").json()["paths"]
    create = paths["/api/auth/keys"]["post"]
    assert create["security"] == [{"session": []}]
    assert "never a key" in create["description"]
    verify = paths["/api/audit/verify"]["get"]
    assert "the `admin` permission" in verify["description"]
    outcome = paths["/api/audit/{decision_id}/outcome"]["post"]
    assert "the `outcome` permission" in outcome["description"]
    assert f"for a scoped key, the scope `{OUTCOME}`" in outcome["description"]
    search = paths["/fhir/Task"]["get"]
    assert "for a scoped key, the scope `system/Task.s`" in search["description"]
    assert "a scoped key may not call this" in paths["/api/audit"]["get"]["description"]
    assert "a scoped key may not call this" in verify["description"]
    asked = app.get("/openapi.json").json()["components"]["schemas"]["KeyRequest"]["properties"]
    assert asked["role"]["enum"] == ["viewer", "service"]
    assert (asked["days"]["default"], asked["days"]["maximum"]) == (90, 365)
    assert "scope" in asked


# -- scoped keys that expire (ADR-0004) -----------------------------------------------------------


def test_new_keys_are_for_machines_only(app: TestClient, idp: FakeIdp) -> None:
    """Review and approval are a person's: a key is a viewer or a service."""
    sign_in(app, idp, roles=["admin"])
    for role in ("reviewer", "governance", "admin", "labeller"):
        refused = app.post("/api/auth/keys", json={"name": "x", "role": role})
        assert refused.status_code == 422, role
        assert refused.json()["detail"][0]["loc"] == ["body", "role"]
    for role in ("viewer", "service"):
        made = app.post("/api/auth/keys", json={"name": role, "role": role})
        assert made.status_code == 201 and made.json()["role"] == role
    assert len(app.get("/api/auth/keys").json()) == 2


def test_a_key_expires_in_90_days_unless_asked_and_365_at_most(
    app: TestClient, idp: FakeIdp
) -> None:
    sign_in(app, idp, roles=["admin"])
    for days in (0, 366, -5):
        body = {"name": "x", "role": "viewer", "days": days}
        assert app.post("/api/auth/keys", json=body).status_code == 422, days
    made = app.post("/api/auth/keys", json={"name": "d", "role": "viewer"}).json()
    one = app.post("/api/auth/keys", json={"name": "1", "role": "viewer", "days": 1}).json()
    year = app.post("/api/auth/keys", json={"name": "y", "role": "viewer", "days": 365}).json()
    now = datetime.now(UTC)
    for key, days in ((made, 90), (one, 1), (year, 365)):
        left = (datetime.fromisoformat(key["expires_at"]) - now) / timedelta(days=1)
        assert days - 0.1 < left <= days, key["name"]


def test_an_expired_key_is_refused_like_any_credential_that_is_not_valid(
    app: TestClient, idp: FakeIdp, tmp_path: Path
) -> None:
    secret = key_for(app, idp, role="viewer")
    machine = {"authorization": f"Bearer {secret}"}
    assert app.get("/api/audit", headers=machine).status_code == 200
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat(timespec="milliseconds")
    with AuditStore(tmp_path / "a.db")._db.session(exclusive=True) as db:
        db.all("UPDATE api_keys SET expires_at = ?", (past,))
    refused = app.get("/api/audit", headers=machine)
    assert refused.status_code == 401
    assert refused.headers["www-authenticate"] == INVALID
    assert without_request_id(refused.json(), refused) == {
        "error": "AuthError",
        "detail": "the credential is not valid",
    }


@pytest.mark.parametrize(
    ("role", "scope", "named"),
    [
        ("service", "system/Task.c", "system/Task.c"),  # no REST create (ADR-0003)
        ("service", "system/Patient.rs", "system/Patient.rs"),
        ("service", "system/NamingSystem.rs", "system/NamingSystem.rs"),
        ("service", "patient/Task.rs", "patient/Task.rs"),
        ("service", "system/Task.rs?status=completed", "system/Task.rs?status=completed"),
        ("service", "system/*.write", "system/*.write"),
        ("service", "openid system/Task.rs", "openid"),
        ("viewer", f"system/Task.rs {DECIDE}", DECIDE),  # a viewer cannot decide
        ("viewer", OUTCOME, OUTCOME),
        ("service", "", None),
    ],
)
def test_a_scope_mendari_does_not_grant_is_refused_by_name(
    app: TestClient, idp: FakeIdp, role: str, scope: str, named: str | None
) -> None:
    sign_in(app, idp, roles=["admin"])
    refused = app.post("/api/auth/keys", json={"name": "x", "role": role, "scope": scope})
    assert refused.status_code == 422
    assert refused.json()["error"] == "ScopeError"
    if named:
        assert repr(named) in refused.json()["detail"]
    assert app.get("/api/auth/keys").json() == []  # nothing made


def test_a_scoped_key_reaches_only_what_its_scopes_name(app: TestClient, idp: FakeIdp) -> None:
    sign_in(app, idp, roles=["admin"])
    body = {"name": "ehr", "role": "service", "scope": f"{DECIDE} system/Task.r {DECIDE}"}
    made = app.post("/api/auth/keys", json=body)
    assert made.status_code == 201
    assert made.json()["scope"] == f"{DECIDE} system/Task.r"  # deduplicated, sorted
    app.cookies.clear()
    machine = {"authorization": f"Bearer {made.json()['key']}"}
    decided = app.post(f"/api/agents/{INBOX}/decide", json=message(ROUTINE), headers=machine)
    assert decided.status_code == 200
    task = app.get(f"/fhir/Task/{decided.json()['id']}", headers=machine)
    assert task.status_code == 200 and task.json()["resourceType"] == "Task"
    insufficient = f'{REALM}, error="insufficient_scope"'
    for method, path, scope in (
        ("GET", "/fhir/Task", "system/Task.s"),  # read, not search
        ("GET", f"/fhir/AuditEvent/{decided.json()['id']}-1", "system/AuditEvent.r"),
        ("POST", f"/api/audit/{decided.json()['id']}/outcome", OUTCOME),
        ("GET", "/api/audit", None),  # no scope reaches Mendari's own reads
        ("GET", f"/api/audit/{decided.json()['id']}", None),
        ("GET", "/openapi.json", None),
        ("GET", f"/api/agents/{INBOX}", None),  # a pack's detail: not a list to decide from
        ("GET", f"/api/agents/{INBOX}/examples", None),
    ):
        refused = app.request(
            method, path, json={"final_label": "x", "source": "ehr"}, headers=machine
        )
        assert refused.status_code == 403, path
        want = insufficient + (f', scope="{scope}"' if scope else "")
        assert refused.headers["www-authenticate"] == want, path
        assert "scopes do not cover" in refused.text
    page = app.get("/", headers=machine, follow_redirects=False)
    assert page.status_code == 403  # not even the dashboard page
    # Deciding finds what to decide with: the lists of packs and of CDS services (only).
    packs = app.get("/api/agents", headers=machine)
    assert packs.status_code == 200 and INBOX in {p["id"] for p in packs.json()}
    assert app.get("/cds-services", headers=machine).status_code == 200
    me = app.get("/api/auth/me", headers=machine).json()["principal"]
    assert me["kind"] == "key" and me["roles"] == ["service"]
    assert me["scope"] == f"{DECIDE} system/Task.r"
    assert me["permissions"] == ["decide", "discover", "read"]  # the role's, narrowed
    assert me["expires_at"] == made.json()["expires_at"]


def test_a_scope_never_widens_a_role(app: TestClient, idp: FakeIdp) -> None:
    """Viewer keys with every read scope still cannot decide; scopes are checked after the
    role, which answers first."""
    sign_in(app, idp, roles=["admin"])
    body = {"name": "reader", "role": "viewer", "scope": "system/*.rs"}
    machine = {"authorization": f"Bearer {app.post('/api/auth/keys', json=body).json()['key']}"}
    app.cookies.clear()
    assert app.get("/fhir/Task", headers=machine).status_code == 200
    assert app.get("/fhir/NamingSystem/x", headers=machine).status_code == 404  # reached
    refused = app.post(f"/api/agents/{INBOX}/decide", json=message(ROUTINE), headers=machine)
    assert (
        refused.status_code == 403 and refused.json()["detail"] == "your role may not decide here"
    )
    assert refused.headers["www-authenticate"] == f'{REALM}, error="insufficient_scope"'


def test_an_unscoped_key_keeps_its_role(app: TestClient, idp: FakeIdp) -> None:
    machine = {"authorization": f"Bearer {key_for(app, idp)}"}
    assert app.get("/api/audit", headers=machine).status_code == 200
    me = app.get("/api/auth/me", headers=machine).json()["principal"]
    assert me["scope"] is None and me["permissions"] == ["decide", "discover", "outcome", "read"]


def test_keys_list_scope_expiry_and_who_revoked_them_never_the_key(
    app: TestClient, idp: FakeIdp, tmp_path: Path
) -> None:
    sign_in(app, idp, name="Ada Admin", roles=["admin"])
    body = {"name": "ehr", "role": "viewer", "scope": "system/Task.rs", "days": 30}
    made = app.post("/api/auth/keys", json=body).json()
    assert app.delete(f"/api/auth/keys/{made['id']}").status_code == 204
    assert app.delete(f"/api/auth/keys/{made['id']}").status_code == 404  # once
    [listed] = app.get("/api/auth/keys").json()
    assert listed["scope"] == "system/Task.rs" and listed["expires_at"] == made["expires_at"]
    assert listed["created_by"] == listed["revoked_by"] == f"{IDP}|u1"
    assert listed["revoked_at"] and "key" not in listed
    assert made["key"] not in app.get("/api/auth/keys").text
    store = AuditStore(tmp_path / "a.db")
    with store._db.session(exclusive=False) as db:
        events = db.all(
            "SELECT event, payload FROM audit_events WHERE decision_id = ? ORDER BY seq",
            (f"key:{made['id']}",),
        )
    assert [e["event"] for e in events] == ["key.created", "key.revoked"]
    assert made["key"] not in json.dumps(events)
    assert store.verify()["ok"] is True


def test_an_authorization_endpoint_with_a_query_keeps_it(app: TestClient, idp: FakeIdp) -> None:
    """An endpoint published as `…/authorize?tenant=x` got a second `?` before Mendari's
    parameters, and the provider misread them; the query is extended instead."""
    idp.authorize_url = f"{IDP}/authorize?tenant=x"
    started = app.get("/auth/login", follow_redirects=False)
    location = urlsplit(started.headers["location"])
    assert location.path == "/authorize" and location.query.count("?") == 0
    params = parse_qs(location.query)
    assert params["tenant"] == ["x"] and params["response_type"] == ["code"] and "state" in params
    assert sign_in(app, idp, roles=["viewer"]).status_code == 302  # and the sign-in completes


def test_a_scoped_credential_gets_the_public_health_only(app: TestClient, idp: FakeIdp) -> None:
    """A key scoped to deciding reaches only the routes its scope names; `/api/health` gave it
    the deployment's settings (allow-lists, key ids, quotas, holds) as it gives a reader."""
    sign_in(app, idp, roles=["admin"])
    made = app.post("/api/auth/keys", json={"name": "ehr", "role": "service", "scope": DECIDE})
    full = app.get("/api/health").json()
    assert "packs" in full and "retention_days" in full
    app.cookies.clear()
    scoped = app.get("/api/health", headers={"authorization": f"Bearer {made.json()['key']}"})
    assert scoped.status_code == 200
    assert set(scoped.json()) == {"status", "version", "sign_in"}
    unscoped = app.get("/api/health", headers={"authorization": f"Bearer {key_for(app, idp)}"})
    assert "packs" in unscoped.json()  # a key with its role's reach still reads it


def test_head_needs_what_get_needs(app: TestClient, idp: FakeIdp) -> None:
    """The rules name GET, so HEAD resolved to `admin`: the static files, which serve HEAD as
    GET, refused an anonymous `HEAD /static/main.js` (link checkers, uptime tools) with 401, and
    `HEAD /` was a 401 instead of the sign-in redirect."""
    assert (
        permission_for("HEAD", "/api/agents") == permission_for("GET", "/api/agents") == "discover"
    )
    assert permission_for("HEAD", "/api/audit") == permission_for("GET", "/api/audit") == "read"
    assert permission_for("HEAD", "/static/main.js") == "public"
    assert permission_for("HEAD", "/api/auth/keys") == permission_for("GET", "/api/auth/keys")
    assert app.head("/static/main.js").status_code == 200
    assert app.head("/", follow_redirects=False).status_code == 302
    assert app.head("/api/agents").status_code == 401  # still a credential's route
    sign_in(app, idp, roles=["viewer"])
    assert app.head("/api/agents").status_code == 405  # past the guard; the route serves GET only


def test_signing_in_again_ends_the_browsers_earlier_session(app: TestClient, idp: FakeIdp) -> None:
    """Claims mode kept the earlier session's row live after a new sign-in in the same browser,
    for up to its idle time; memberships mode already ended it ("one browser, one person")."""
    assert sign_in(app, idp, roles=["viewer"]).status_code == 302
    first = dict(app.cookies)
    assert first and app.get("/api/agents").status_code == 200
    # The same browser, cookie still held, signs in again (`sign_in` would clear it first).
    started = app.get("/auth/login", params={"next": "/"}, follow_redirects=False)
    code = idp.authorize(started.headers["location"], roles=["viewer"])
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    again = app.get("/auth/callback", params={"code": code, "state": state}, follow_redirects=False)
    assert again.status_code == 302
    assert app.get("/api/agents").status_code == 200  # the new session
    app.cookies.clear()
    for name, value in first.items():
        app.cookies.set(name, value)
    assert app.get("/api/agents").status_code == 401  # the earlier one is gone
