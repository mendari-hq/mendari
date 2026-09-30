"""Organization sign-in, roles and API keys (server/auth.py), against a fake identity provider."""

from __future__ import annotations

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
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from openhealth.core import tenancy
from openhealth.core.audit import AuditStore
from openhealth.errors import OpenHealthError
from openhealth.server import auth as auth_module
from openhealth.server.app import create_app
from openhealth.server.auth import auth_settings, permission_for
from openhealth.standards.fhir_client import GuardedHttp
from tests.conftest import message

IDP = "https://idp.example.org"
CLIENT = "openhealth-org"
INBOX = "openhealth/patient-inbox-router"
ROUTINE = "Can you send a refill for metformin to my pharmacy on Main Street?"


class FakeIdp:
    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.codes: dict[str, dict[str, Any]] = {}
        self.claims: dict[str, Any] = {}

    def authorize(self, url: str, **claims: Any) -> str:
        params = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        code = f"code-{len(self.codes)}"
        self.codes[code] = {"nonce": params["nonce"], "claims": claims}
        return code

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{IDP}/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": IDP,
                    "authorization_endpoint": f"{IDP}/authorize",
                    "token_endpoint": f"{IDP}/token",
                    "jwks_uri": f"{IDP}/jwks",
                },
            )
        if url == f"{IDP}/jwks":
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
            return httpx.Response(200, json={"keys": [{**jwk, "kid": "k1"}]})
        if url == f"{IDP}/token":
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
    monkeypatch.setenv("OPENHEALTH_OIDC_ISSUER", IDP)
    monkeypatch.setenv("OPENHEALTH_OIDC_CLIENT_ID", CLIENT)
    monkeypatch.setenv("OPENHEALTH_OIDC_ROLE_MAP", "clinicians=reviewer")
    transport = httpx.MockTransport(idp)
    monkeypatch.setattr(
        auth_module, "GuardedHttp", lambda **kw: GuardedHttp(transport=transport, **kw)
    )
    with TestClient(
        create_app(db_path=tmp_path / "a.db"), base_url="https://openhealth.test"
    ) as client:
        yield client


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
    cookie = done.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    me = app.get("/api/auth/me").json()
    assert me["principal"] == {
        "kind": "user",
        "name": "Nurse Kim",
        "roles": ["reviewer"],
        "permissions": ["decide", "outcome", "read", "review"],
        "org": "default",
    }
    assert app.post("/auth/logout").status_code == 204
    assert app.get("/api/audit").status_code == 401


def test_each_role_can_do_exactly_its_part(app: TestClient, idp: FakeIdp) -> None:
    decide = f"/api/packs/{INBOX}/decide"
    evals = f"/api/packs/{INBOX}/evals"
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
    sign_in(app, idp, name="Nurse Kim", roles=["clinicians"])
    emergency = message("Crushing chest pain and my left arm is numb")  # waits for review
    decided = app.post(f"/api/packs/{INBOX}/decide", json=emergency).json()
    posed_as = {"final_label": "nurse", "reviewer": "Dr Somebody Else"}
    assert app.post(f"/api/review/{decided['id']}", json=posed_as).status_code == 200
    rating = {"rating": "up", "reporter": "Dr Somebody Else"}
    assert app.post(f"/api/audit/{decided['id']}/feedback", json=rating).status_code == 200
    record = app.get(f"/api/audit/{decided['id']}").json()
    assert record["reviewer"] == "Nurse Kim"
    assert [f["reporter"] for f in app.get("/api/feedback").json()] == ["Nurse Kim"]


def test_an_account_without_a_role_is_refused(app: TestClient, idp: FakeIdp) -> None:
    done = sign_in(app, idp, roles=["marketing"])
    assert done.status_code == 403 and "no OpenHealth role" in done.text


def test_api_keys_work_for_machines_are_hashed_and_revocable(
    app: TestClient, idp: FakeIdp, tmp_path: Path
) -> None:
    sign_in(app, idp, roles=["admin"])
    created = app.post("/api/auth/keys", json={"name": "ehr integration", "role": "service"}).json()
    secret = created["key"]
    app.cookies.clear()
    machine = {"authorization": f"Bearer {secret}"}
    assert (
        app.post(f"/api/packs/{INBOX}/decide", json=message(ROUTINE), headers=machine).status_code
        == 200
    )
    assert app.get(f"/api/packs/{INBOX}/feedback/export", headers=machine).status_code == 403
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
    monkeypatch.delenv("OPENHEALTH_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("OPENHEALTH_OIDC_CLIENT_ID", raising=False)
    client = TestClient(create_app(db_path=tmp_path / "a.db"))
    assert client.get("/api/auth/me").json()["mode"] == "open"
    assert client.get("/api/audit").status_code == 200
    assert client.get("/auth/login").status_code == 404


@pytest.mark.parametrize(
    ("env", "message_part"),
    [
        ({"OPENHEALTH_OIDC_ISSUER": IDP}, "set both"),
        (
            {"OPENHEALTH_OIDC_ISSUER": "http://idp.example.org", "OPENHEALTH_OIDC_CLIENT_ID": "c"},
            "https",
        ),
        (
            {
                "OPENHEALTH_OIDC_ISSUER": IDP,
                "OPENHEALTH_OIDC_CLIENT_ID": "c",
                "OPENHEALTH_OIDC_ROLE_MAP": "a=root",
            },
            "not a role",
        ),
    ],
)
def test_bad_settings_fail_at_startup(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], message_part: str
) -> None:
    for name in ("OPENHEALTH_OIDC_ISSUER", "OPENHEALTH_OIDC_CLIENT_ID", "OPENHEALTH_OIDC_ROLE_MAP"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(OpenHealthError, match=message_part):
        auth_settings()


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
    assert public == {
        ("GET", "/api/health"),
        ("GET", "/api/auth/me"),
        ("GET", "/favicon.ico"),
        ("GET", "/auth/login"),
        ("GET", "/auth/callback"),
        ("POST", "/auth/logout"),
        ("GET", "/smart/launch"),
        ("GET", "/smart/callback"),
        ("GET", "/api/smart/session"),
        ("POST", "/api/smart/logout"),
        ("GET", "/api/smart/patient"),
        ("GET", "/api/smart/focus"),
        ("POST", "/api/smart/decide"),
    }
    writes = [
        (m, p)
        for m, p in routes
        if m in ("POST", "PUT", "PATCH", "DELETE") and (m, p) not in public
    ]
    assert all(permission_for(m, p) != "read" for m, p in writes), writes
    assert permission_for("POST", "/api/packs/a/b/evals") == "evaluate"
    assert permission_for("GET", "/api/packs/a/b/feedback/export") == "admin"
    assert permission_for("DELETE", "/api/something/new") == "admin"  # deny by default


def test_organizations_never_see_each_others_work(
    app: TestClient, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One deployment, two organizations: each sees only its own decisions, reviews and stats."""
    monkeypatch.setenv("OPENHEALTH_OIDC_ORG_CLAIM", "org")
    transport = httpx.MockTransport(idp)
    monkeypatch.setattr(
        auth_module, "GuardedHttp", lambda **kw: GuardedHttp(transport=transport, **kw)
    )
    client = TestClient(
        create_app(db_path=tmp_path / "two-orgs.db"), base_url="https://openhealth.test"
    )
    keys = {}
    for org in ("clinic-a", "clinic-b"):
        sign_in(client, idp, roles=["admin"], org=org)
        for role in ("service", "reviewer"):
            body = {"name": f"{org} {role}", "role": role}
            created = client.post("/api/auth/keys", json=body).json()
            keys[f"{org} {role}"] = {"authorization": f"Bearer {created['key']}"}
        keys[org] = keys[f"{org} service"]
    client.cookies.clear()
    emergency = message("Crushing chest pain and my left arm is numb")  # waits for review
    decided = client.post(
        f"/api/packs/{INBOX}/decide", json=emergency, headers=keys["clinic-a"]
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
    assert client.get("/api/audit/export/fhir", headers=keys["clinic-b"]).text == ""
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
