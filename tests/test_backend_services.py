# SPDX-License-Identifier: Apache-2.0
"""SMART Backend Services (server/backend_services.py, ADR-0004): clients, the token endpoint,
access tokens and discovery, on SQLite and Postgres. Synthetic keys and data only."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi.testclient import TestClient

from mendari.core.audit import AuditStore
from mendari.errors import ClientError, MendariError
from mendari.server import scopes
from mendari.server.app import create_app
from mendari.server.backend_services import (
    ASSERTION_TYPE,
    DISCOVERY_PATH,
    MAX_FORM_BYTES,
    check_jwks,
    default_scope,
)
from mendari.server.scopes import DECIDE, OUTCOME
from mendari.standards import bulk
from mendari.standards.fhir_client import GuardedHttp
from tests.conftest import encrypt_mode, message, security_lines, signed_in
from tests.jwt_signers import DROP, PUBLIC_URL, TOKEN_URL, Signer, assertion, forged

INBOX = "mendari/patient-inbox-router"
INVALID_CLIENT = {"error": "invalid_client", "error_description": "client authentication failed"}


RSA_KEY = Signer("RS384", "rsa-1", rsa.generate_private_key(public_exponent=65537, key_size=2048))
EC_KEY = Signer("ES384", "ec-1", ec.generate_private_key(ec.SECP384R1()))
STRANGER = Signer("RS384", "rsa-1", rsa.generate_private_key(public_exponent=65537, key_size=2048))


def on(monkeypatch: pytest.MonkeyPatch, *, public_url: str | None = PUBLIC_URL) -> None:
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari-org")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    monkeypatch.setenv("MENDARI_BACKEND_SERVICES", "on")
    if public_url is None:
        monkeypatch.delenv("MENDARI_PUBLIC_URL", raising=False)
    else:
        monkeypatch.setenv("MENDARI_PUBLIC_URL", public_url)


@dataclass
class Server:
    http: TestClient
    db: str

    def person(self, role: str = "admin", org: str = "default") -> dict[str, str]:
        with AuditStore(self.db) as store:
            return signed_in(store, role=role, org=org)

    def register(
        self,
        *,
        role: str = "service",
        scope: str | None = None,
        keys: tuple[Signer, ...] = (RSA_KEY, EC_KEY),
        org: str = "default",
    ) -> str:
        body: dict[str, Any] = {"name": f"{org} {role}", "role": role}
        body |= {"jwks": {"keys": [k.jwk for k in keys]}}
        if scope is not None:
            body["scope"] = scope
        made = self.http.post("/api/auth/clients", json=body, headers=self.person(org=org))
        assert made.status_code == 201, made.text
        return str(made.json()["client_id"])

    def token(self, client_id: str, scope: str = "system/*.rs", signer: Signer = RSA_KEY) -> str:
        answer = ask(self.http, assertion(signer, client_id), scope)
        assert answer.status_code == 200, answer.text
        return str(answer.json()["access_token"])

    def rows(self, table: str) -> int:
        with AuditStore(self.db) as store, store._db.session(exclusive=False) as session:
            row = session.one(f"SELECT COUNT(*) AS n FROM {table}")
        assert row is not None
        return int(row["n"])


@pytest.fixture
def server(db_target: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    on(monkeypatch)
    with TestClient(create_app(db_path=db_target), base_url=PUBLIC_URL) as http:
        yield Server(http, db_target)


def ask(http: TestClient, signed: str, scope: str | None = "system/*.rs", **form: Any) -> Any:
    data = {
        "grant_type": "client_credentials",
        "client_assertion_type": ASSERTION_TYPE,
        "client_assertion": signed,
        "scope": scope,
        **form,
    }
    return http.post("/auth/token", data={k: v for k, v in data.items() if v is not None})


def bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


# -- settings -------------------------------------------------------------------------------------


def test_it_is_off_unless_turned_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    on(monkeypatch)
    monkeypatch.delenv("MENDARI_BACKEND_SERVICES")
    http = TestClient(create_app(db_path=tmp_path / "off.db"), base_url=PUBLIC_URL)
    assert http.get(DISCOVERY_PATH).status_code == 404
    assert http.post("/auth/token", data={"grant_type": "client_credentials"}).status_code in (
        404,
        405,
    )
    security = http.get("/fhir/metadata").json()["rest"][0]["security"]
    assert "service" not in security and "not a SMART on FHIR" in security["description"]
    paths = {r.path for r in http.app.routes}  # type: ignore[attr-defined]
    assert "/api/auth/clients" not in paths
    assert "/api/auth/clients/{client_id}/history" not in paths
    assert "/api/auth/clients/{client_id}/keys" not in paths


@pytest.mark.parametrize(
    ("env", "problem"),
    [
        ({"MENDARI_OIDC_ISSUER": None, "MENDARI_OIDC_CLIENT_ID": None}, "needs sign-in"),
        ({"MENDARI_PUBLIC_URL": None}, "needs MENDARI_PUBLIC_URL"),
        ({"MENDARI_PUBLIC_URL": "http://mendari.example.org"}, "https"),
        ({"MENDARI_PUBLIC_URL": "https://mendari.example.org/?x=1"}, "query"),
        ({"MENDARI_BACKEND_SERVICES": "yes"}, "on or off"),
    ],
)
def test_the_server_refuses_to_start_without_what_it_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: dict[str, str | None], problem: str
) -> None:
    """On, it needs sign-in (no tokens in open mode) and a public URL (the assertions' audience,
    never the Host header)."""
    on(monkeypatch)
    for name, value in env.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    with pytest.raises(MendariError, match=problem):
        create_app(db_path=tmp_path / "refused.db")


# -- discovery and the CapabilityStatement -------------------------------------------------------


def test_discovery_says_what_is_true_here_as_plain_json(server: Server) -> None:
    for accept in (None, "application/fhir+json", "text/html", "application/xml"):
        headers = {"accept": accept} if accept else {}
        answer = server.http.get(DISCOVERY_PATH, headers=headers)  # public: no credential
        assert answer.status_code == 200, accept
        assert answer.headers["content-type"] == "application/json"
    assert answer.json() == {
        "token_endpoint": TOKEN_URL,
        "grant_types_supported": ["client_credentials"],
        "token_endpoint_auth_methods_supported": ["private_key_jwt"],
        "token_endpoint_auth_signing_alg_values_supported": ["RS384", "ES384"],
        "scopes_supported": scopes.supported(),
        "capabilities": ["client-confidential-asymmetric", "permission-v2", "permission-v1"],
        "code_challenge_methods_supported": ["S256"],
    }
    assert "system/NamingSystem.rs" not in answer.json()["scopes_supported"]  # no such search


def test_the_capability_statement_says_smart_only_when_it_is_on(server: Server) -> None:
    security = server.http.get("/fhir/metadata").json()["rest"][0]["security"]
    assert security["cors"] is False
    assert security["service"] == [
        {
            "coding": [
                {
                    "system": "http://terminology.hl7.org/CodeSystem/restful-security-service",
                    "code": "SMART-on-FHIR",
                    "display": "SMART-on-FHIR",
                }
            ]
        }
    ]
    text = security["description"]
    assert "SMART Backend Services" in text and "no authorization endpoint" in text
    assert DECIDE in text and OUTCOME in text and "not a SMART on FHIR" not in text


# -- clients ---------------------------------------------------------------------------------------


def test_a_person_registers_a_client_and_the_chain_records_it(server: Server) -> None:
    admin = server.person()
    body = {"name": "ehr", "role": "service", "jwks": {"keys": [RSA_KEY.jwk, EC_KEY.jwk]}}
    made = server.http.post("/api/auth/clients", json=body, headers=admin)
    assert made.status_code == 201
    client = made.json()
    assert len(client["client_id"]) == 24 and client["token_endpoint"] == TOKEN_URL
    assert client["scope"] == default_scope("service")  # everything the role can use
    assert DECIDE in client["scope"] and OUTCOME in client["scope"]
    assert [(k["kid"], k["kty"]) for k in client["keys"]] == [("rsa-1", "RSA"), ("ec-1", "EC")]
    assert client["created_by"] == "admin@default.example.org" and client["org"] == "default"
    listed = server.http.get("/api/auth/clients", headers=admin).json()
    assert [c["client_id"] for c in listed] == [client["client_id"]]
    assert listed[0]["revoked_at"] is None and "jwks" not in listed[0]
    narrow = {**body, "role": "viewer", "scope": "system/Task.read"}
    viewer = server.http.post("/api/auth/clients", json=narrow, headers=admin).json()
    assert viewer["scope"] == "system/Task.read" and default_scope("viewer") == (
        "system/*.rs system/AuditEvent.rs system/CodeSystem.rs system/NamingSystem.r "
        "system/Provenance.rs system/Task.rs"
    )

    revoked = server.http.delete(f"/api/auth/clients/{client['client_id']}", headers=admin)
    assert revoked.status_code == 204
    again = server.http.delete(f"/api/auth/clients/{client['client_id']}", headers=admin)
    assert again.status_code == 404
    listed = server.http.get("/api/auth/clients", headers=admin).json()
    assert listed[0]["revoked_by"] == "admin@default.example.org" and listed[0]["revoked_at"]

    with AuditStore(server.db) as store, store._db.session(exclusive=False) as session:
        rows = session.all(
            "SELECT decision_id, event, payload FROM audit_events "
            "WHERE decision_id LIKE ? ORDER BY seq",
            ("client:%",),
        )
    with AuditStore(server.db) as store:
        assert store.verify()["ok"] is True
    events = [{**row, "payload": json.loads(row["payload"])} for row in rows]
    names = [(e["decision_id"], e["event"]) for e in events]
    assert names == [
        (f"client:{client['client_id']}", "client.registered"),
        (f"client:{viewer['client_id']}", "client.registered"),
        (f"client:{client['client_id']}", "client.revoked"),
    ]
    chained = json.dumps([e["payload"] for e in events])
    assert RSA_KEY.jwk["n"] not in chained  # ids and thumbprints, not the keys themselves
    assert events[0]["payload"]["keys"][0]["thumbprint"] == client["keys"][0]["thumbprint"]


def test_clients_are_managed_by_people_with_the_keys_permission_only(
    server: Server, tmp_path: Path
) -> None:
    """No key or token registers a client (it would outlive the credential that made it)."""
    from tests.test_auth import legacy_key

    body = {"name": "x", "role": "service", "jwks": {"keys": [RSA_KEY.jwk]}}
    admin_key = bearer(legacy_key(server.db, "admin"))
    client_id = server.register()
    token = bearer(server.token(client_id, f"system/*.rs {DECIDE}"))
    for headers in (admin_key, token, server.person("viewer"), server.person("governance")):
        for method, path in (
            ("POST", "/api/auth/clients"),
            ("GET", "/api/auth/clients"),
            ("DELETE", f"/api/auth/clients/{client_id}"),
            ("GET", f"/api/auth/clients/{client_id}/history"),
            ("POST", f"/api/auth/clients/{client_id}/keys"),
        ):
            refused = server.http.request(method, path, json=body, headers=headers)
            assert refused.status_code == 403, (headers, method)
    assert server.http.post("/api/auth/clients", json=body).status_code == 401
    assert server.http.get(f"/api/auth/clients/{client_id}/history").status_code == 401
    assert server.http.post(f"/api/auth/clients/{client_id}/keys", json=body).status_code == 401
    other = server.person(org="clinic-b")  # an admin of another organization
    assert server.http.delete(f"/api/auth/clients/{client_id}", headers=other).status_code == 404
    assert server.http.get("/api/auth/clients", headers=other).json() == []
    history = server.http.get(f"/api/auth/clients/{client_id}/history", headers=other)
    assert history.status_code == 404
    replaced = server.http.post(f"/api/auth/clients/{client_id}/keys", json=body, headers=other)
    assert replaced.status_code == 404


def jwk(signer: Signer, **changes: Any) -> dict[str, Any]:
    return {k: v for k, v in {**signer.jwk, **changes}.items() if v is not DROP}


def private_jwk(signer: Signer) -> dict[str, Any]:
    family = jwt.algorithms.RSAAlgorithm if signer.alg == "RS384" else jwt.algorithms.ECAlgorithm
    return {**json.loads(family.to_jwk(signer.private)), "kid": signer.kid}


SMALL = rsa.generate_private_key(public_exponent=65537, key_size=1024)
P256 = ec.generate_private_key(ec.SECP256R1())


@pytest.mark.parametrize(
    ("keys", "problem"),
    [
        ([private_jwk(RSA_KEY)], "private or symmetric"),
        ([private_jwk(EC_KEY)], "private or symmetric"),
        ([{"kty": "oct", "kid": "h", "k": "c2VjcmV0"}], "private or symmetric"),
        ([{"kty": "OKP", "kid": "e", "crv": "Ed25519", "x": "AA"}], "kty must be RSA"),
        ([jwk(Signer("RS384", "small", SMALL))], "2048"),
        ([jwk(Signer("ES384", "p256", P256))], "P-384"),
        ([jwk(EC_KEY, crv="P-256")], "P-384"),
        ([jwk(EC_KEY, x="AAAA")], "not a valid EC public key"),
        ([jwk(RSA_KEY, alg="RS256")], "signs RS384"),
        ([jwk(RSA_KEY, use="enc")], "use must be sig"),
        ([jwk(RSA_KEY, key_ops=["sign", "verify"])], "key_ops"),
        ([jwk(RSA_KEY, key_ops=[["verify"]])], "key_ops"),  # never hashed: no TypeError
        ([jwk(RSA_KEY, key_ops=["verify", {}])], "key_ops"),
        ([jwk(RSA_KEY, kty=["RSA"])], "kty must be RSA"),
        ([jwk(EC_KEY, kty={"EC": 1})], "kty must be RSA"),
        ([jwk(RSA_KEY, kid=DROP)], "needs a kid"),
        ([jwk(RSA_KEY, kid="a b")], "needs a kid"),
        ([jwk(RSA_KEY), jwk(STRANGER)], "same kid"),
        ([jwk(RSA_KEY, n="not-base64!")], "not a valid RSA public key"),
        ([], "1 to 10 keys"),
        ([jwk(RSA_KEY, kid=f"k{n}") for n in range(11)], "1 to 10 keys"),
    ],
)
def test_a_jwks_with_anything_but_public_signing_keys_is_refused(
    keys: list[dict[str, Any]], problem: str
) -> None:
    with pytest.raises(ClientError, match=problem) as refused:
        check_jwks({"keys": keys})
    assert "AQAB" not in str(refused.value) and "c2VjcmV0" not in str(refused.value)


def test_registration_keeps_only_the_public_key_and_refuses_bad_requests(server: Server) -> None:
    extra = {**RSA_KEY.jwk, "x5c": ["MIIB"], "ext": True}
    assert check_jwks({"keys": [extra]}) == [RSA_KEY.jwk]  # nothing but the public members
    admin = server.person()
    good = {"name": "x", "role": "service", "jwks": {"keys": [RSA_KEY.jwk]}}
    for change, status in (
        ({"jwks": {"keys": [private_jwk(RSA_KEY)]}}, 422),
        ({"role": "admin"}, 422),
        ({"role": "reviewer"}, 422),
        ({"scope": "patient/Task.read"}, 422),
        ({"scope": "system/Task.c"}, 422),
        ({"role": "viewer", "scope": DECIDE}, 422),  # beyond the role
        ({"scope": ""}, 422),
        ({"jwks": {"nokeys": []}}, 422),
    ):
        refused = server.http.post("/api/auth/clients", json={**good, **change}, headers=admin)
        assert refused.status_code == status, change
        assert "AQAB" not in refused.text and private_jwk(RSA_KEY)["d"] not in refused.text
    assert server.rows("oauth_clients") == 0


# -- the token endpoint ----------------------------------------------------------------------


@pytest.mark.parametrize("signer", [RSA_KEY, EC_KEY], ids=["RS384", "ES384"])
def test_a_client_gets_a_five_minute_token_and_reads_with_it(
    server: Server, signer: Signer
) -> None:
    client_id = server.register()
    answer = ask(server.http, assertion(signer, client_id), "system/Task.rs system/Patient.rs")
    assert answer.status_code == 200
    assert answer.headers["cache-control"] == "no-store"
    assert answer.headers["pragma"] == "no-cache"
    body = answer.json()
    assert set(body) == {"access_token", "token_type", "expires_in", "scope"}
    assert body["token_type"] == "bearer" and body["expires_in"] == 300
    assert body["scope"] == "system/Task.rs"  # what was asked, within what is registered
    assert body["access_token"].startswith("mat_")
    headers = bearer(body["access_token"])
    assert server.http.get("/fhir/Task", headers=headers).status_code == 200
    me = server.http.get("/api/auth/me", headers=headers).json()["principal"]
    assert me["kind"] == "client" and me["name"] == f"client {client_id}"
    assert me["scope"] == "system/Task.rs" and me["permissions"] == ["read"]
    assert me["expires_at"] and me["org"] == "default"
    with AuditStore(server.db) as store, store._db.session(exclusive=False) as session:
        stored = session.all("SELECT * FROM oauth_tokens")
    assert body["access_token"] not in json.dumps(stored)  # stored as SHA-256
    assert set(stored[0]) == {"token_sha256", "client_id", "scope", "expires_at", "keys_version"}


def refusals(client_id: str) -> list[tuple[str, str]]:
    """(why, assertion) for assertions that must not authenticate `client_id`."""
    now = int(time.time())
    claims = {"iss": client_id, "sub": client_id, "aud": TOKEN_URL, "exp": now + 60, "jti": "j"}
    good_header = {"alg": "RS384", "kid": "rsa-1", "typ": "JWT"}
    return [
        ("alg none", forged({"alg": "none", "kid": "rsa-1"}, claims, b"")),
        ("HS256", forged({**good_header, "alg": "HS256"}, claims)),
        ("RS256", assertion(RSA_KEY, client_id, algorithm="RS256")),
        ("PS384", assertion(RSA_KEY, client_id, algorithm="PS384")),
        ("alg a list", forged({**good_header, "alg": ["RS384"]}, claims)),
        ("jku", assertion(RSA_KEY, client_id, headers={"jku": "https://evil.example/jwks"})),
        ("x5u", assertion(RSA_KEY, client_id, headers={"x5u": "https://evil.example/c"})),
        ("x5c", assertion(RSA_KEY, client_id, headers={"x5c": ["MIIB"]})),
        ("jwk", assertion(STRANGER, client_id, headers={"jwk": STRANGER.jwk})),
        ("crit", forged({**good_header, "crit": ["exp"]}, claims)),
        ("typ at+jwt", assertion(RSA_KEY, client_id, headers={"typ": "at+jwt"})),
        ("no kid", forged({"alg": "RS384", "typ": "JWT"}, claims)),
        ("unknown kid", assertion(RSA_KEY, client_id, headers={"kid": "nope"})),
        ("EC kid under RS384", assertion(RSA_KEY, client_id, headers={"kid": "ec-1"})),
        ("another client's key", assertion(STRANGER, client_id)),
        ("bad signature", forged(good_header, claims)),
        ("another audience", assertion(RSA_KEY, client_id, aud="https://other.example/token")),
        ("the FHIR base", assertion(RSA_KEY, client_id, aud=f"{PUBLIC_URL}/fhir/auth/token")),
        ("audience a list", assertion(RSA_KEY, client_id, aud=[TOKEN_URL])),
        ("no audience", assertion(RSA_KEY, client_id, aud=DROP)),
        ("no exp", assertion(RSA_KEY, client_id, exp=DROP)),
        ("exp an hour ahead", assertion(RSA_KEY, client_id, lifetime=3600)),
        ("expired", assertion(RSA_KEY, client_id, lifetime=-120)),
        ("iat ahead", assertion(RSA_KEY, client_id, iat=now + 600)),
        ("nbf ahead", assertion(RSA_KEY, client_id, nbf=now + 600)),
        ("sub not iss", assertion(RSA_KEY, client_id, sub="someone")),
        ("no sub", assertion(RSA_KEY, client_id, sub=DROP)),
        ("unknown client", assertion(RSA_KEY, "0" * 24)),
        ("not a client id", assertion(RSA_KEY, "../../etc\nx")),
        ("no jti", assertion(RSA_KEY, client_id, jti=DROP)),
        ("empty jti", assertion(RSA_KEY, client_id, jti="")),
        ("long jti", assertion(RSA_KEY, client_id, jti="j" * 257)),
        ("jti a number", assertion(RSA_KEY, client_id, jti=7)),
        ("too large", assertion(RSA_KEY, client_id, pad="x" * 9000)),
        ("not a JWT", "not-a-jwt"),
        ("two segments", "a.b"),
        ("not JSON", "bm90.anNvbg.c2ln"),
    ]


def test_every_assertion_that_does_not_authenticate_gets_one_answer_and_writes_nothing(
    server: Server,
) -> None:
    client_id = server.register()
    server.register(keys=(STRANGER,))  # another client, whose key has the same kid
    for why, signed in refusals(client_id):
        for scope in ("system/*.rs", "patient/*.read"):  # authentication comes before scope
            refused = ask(server.http, signed, scope)
            assert refused.status_code == 401, why
            assert refused.json() == INVALID_CLIENT, why
            assert refused.headers["cache-control"] == "no-store", why
    assert server.rows("oauth_tokens") == 0
    assert server.rows("smart_sessions WHERE kind = 'jti'") == 0  # nothing was claimed
    # A signed assertion refused for its audience claimed nothing: its jti still works, once.
    stale = assertion(RSA_KEY, client_id, jti="x", aud="https://other.example/token")
    assert ask(server.http, stale).status_code == 401
    fresh = assertion(RSA_KEY, client_id, jti="x")
    assert ask(server.http, fresh).status_code == 200
    assert ask(server.http, fresh).json() == INVALID_CLIENT


def test_a_wrong_assertion_type_or_none_is_invalid_client(server: Server) -> None:
    client_id = server.register()
    signed = assertion(RSA_KEY, client_id)
    for form in (
        {"client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:saml2-bearer"},
        {"client_assertion_type": None},
        {"client_assertion": None},
        {"client_id": other_id()},  # names another client than the assertion
    ):
        refused = ask(server.http, signed, **form)
        assert (refused.status_code, refused.json()) == (401, INVALID_CLIENT), form
    assert ask(server.http, signed, client_id=client_id).status_code == 200


def other_id() -> str:
    return "f" * 24


def test_a_replayed_assertion_is_refused_and_a_jti_is_per_client(server: Server) -> None:
    first, second = server.register(), server.register()
    signed = assertion(RSA_KEY, first, jti="once")
    assert ask(server.http, signed).status_code == 200
    replayed = ask(server.http, signed)
    assert (replayed.status_code, replayed.json()) == (401, INVALID_CLIENT)
    assert ask(server.http, assertion(RSA_KEY, first, jti="once")).status_code == 401
    assert ask(server.http, assertion(RSA_KEY, second, jti="once")).status_code == 200
    # A logout cannot erase a claim (stage 1): the cookie names a row of its own kind only.
    cookie = {"cookie": f'__Host-mendari_session="{first}\\012once"'}
    assert server.http.post("/auth/logout", headers=cookie).status_code == 204
    assert ask(server.http, signed).status_code == 401


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"grant_type": "authorization_code"}, "unsupported_grant_type"),
        ({"grant_type": "password"}, "unsupported_grant_type"),
        ({"grant_type": None}, "invalid_request"),
        ({"scope": None}, "invalid_request"),
        ({"scope": " "}, "invalid_request"),
        ({"client_secret": "s"}, "invalid_request"),
    ],
)
def test_oauth_errors_for_requests_that_are_not_a_client_credentials_grant(
    server: Server, change: dict[str, Any], error: str
) -> None:
    signed = assertion(RSA_KEY, server.register())
    refused = ask(server.http, signed, **change)
    assert (refused.status_code, refused.json()["error"]) == (400, error)
    assert set(refused.json()) == {"error", "error_description"}
    assert signed not in refused.text
    assert ask(server.http, signed).status_code == 200  # the assertion was not used up


def test_the_form_must_be_a_form_once_and_small(server: Server) -> None:
    client_id = server.register()
    signed = assertion(RSA_KEY, client_id)
    form = {
        "grant_type": "client_credentials",
        "client_assertion_type": ASSERTION_TYPE,
        "client_assertion": signed,
        "scope": "system/*.rs",
    }
    as_json = server.http.post("/auth/token", json=form)
    assert (as_json.status_code, as_json.json()["error"]) == (400, "invalid_request")
    twice = "&".join(f"{k}={v}" for k, v in form.items()) + "&scope=system/Task.rs"
    kind = {"content-type": "application/x-www-form-urlencoded"}
    repeated = server.http.post("/auth/token", content=twice, headers=kind)
    assert (repeated.status_code, repeated.json()["error"]) == (400, "invalid_request")
    padded = server.http.post("/auth/token", data={**form, "pad": "x" * MAX_FORM_BYTES})
    assert (padded.status_code, padded.json()["error"]) == (400, "invalid_request")
    with_header = server.http.post("/auth/token", data=form, headers=bearer("mat_x"))
    assert (with_header.status_code, with_header.json()["error"]) == (400, "invalid_request")
    leaked = server.http.post("/auth/token", data={**form, "access_token": "mat_x"})
    assert (leaked.status_code, leaked.json()["error"]) == (400, "invalid_request")
    chunked = server.http.post("/auth/token", content=iter([b"a=" * MAX_FORM_BYTES]), headers=kind)
    assert (chunked.status_code, chunked.json()["error"]) == (400, "invalid_request")
    assert server.http.post("/auth/token", data=form).status_code == 200


def test_the_scope_granted_is_what_was_asked_within_what_was_registered(server: Server) -> None:
    viewer = server.register(role="viewer", scope="system/Task.rs system/AuditEvent.r")
    for asked, granted in (
        ("system/*.rs", "system/AuditEvent.r system/Task.rs"),
        ("system/Task.read", "system/Task.rs"),  # SMART v1
        ("system/Task.r system/Provenance.rs", "system/Task.r"),
        ("system/Task.cruds system/Task.s?status=requested", "system/Task.rs"),
        (f"system/Task.s {DECIDE}", "system/Task.s"),  # a viewer cannot decide
    ):
        answer = ask(server.http, assertion(RSA_KEY, viewer), asked)
        assert answer.json()["scope"] == granted, asked
    for asked in ("patient/*.read", "user/Task.rs", DECIDE, "system/Patient.rs", "launch openid"):
        refused = ask(server.http, assertion(RSA_KEY, viewer), asked)
        assert refused.status_code == 400 and refused.json()["error"] == "invalid_scope", asked


def test_a_token_reaches_only_what_its_scope_names(server: Server) -> None:
    client_id = server.register(scope=f"system/*.rs {DECIDE} {OUTCOME}")
    reads = bearer(server.token(client_id, "system/Task.rs"))
    assert server.http.get("/fhir/Task", headers=reads).status_code == 200
    refused = server.http.get("/fhir/AuditEvent", headers=reads)
    assert refused.status_code == 403
    assert refused.headers["www-authenticate"] == (
        'Bearer realm="mendari", error="insufficient_scope", scope="system/AuditEvent.s"'
    )
    for path in ("/api/audit", "/api/stats", "/openapi.json", "/cds-services"):
        assert server.http.get(path, headers=reads).status_code == 403, path
    assert server.http.get("/", headers=reads).status_code == 403  # nor the dashboard
    decide = server.http.post(f"/api/packs/{INBOX}/decide", json=message("hi"), headers=reads)
    assert decide.status_code == 403
    decider = bearer(server.token(client_id, DECIDE))
    made = server.http.post(f"/api/packs/{INBOX}/decide", json=message("refill"), headers=decider)
    assert made.status_code == 200
    assert server.http.get("/fhir/Task", headers=decider).status_code == 403


def test_each_request_reads_the_client_as_it_is_now(server: Server) -> None:
    """Role, scopes and revocation come from the client on every request, never from the token:
    a change applies to tokens already issued, and revoking stops them at once."""
    client_id = server.register(scope=f"system/*.rs {DECIDE}")
    token = bearer(server.token(client_id, f"system/Task.rs {DECIDE}"))
    assert server.http.get("/fhir/Task", headers=token).status_code == 200
    with AuditStore(server.db) as store, store._db.session(exclusive=True) as session:
        session.all("UPDATE oauth_clients SET scopes = ? WHERE client_id = ?", (DECIDE, client_id))
    narrowed = server.http.get("/fhir/Task", headers=token)
    assert narrowed.status_code == 403  # the registration no longer covers Task
    with AuditStore(server.db) as store, store._db.session(exclusive=True) as session:
        session.all(
            "UPDATE oauth_clients SET scopes = ?, role = 'viewer' WHERE client_id = ?",
            (f"system/*.rs {DECIDE}", client_id),
        )
    me = server.http.get("/api/auth/me", headers=token).json()
    assert me["signed_in"] is False  # the stored scope is beyond the role now: refused
    with AuditStore(server.db) as store, store._db.session(exclusive=True) as session:
        session.all(
            "UPDATE oauth_clients SET scopes = 'system/*.rs', role = 'service' WHERE client_id = ?",
            (client_id,),
        )
    assert server.http.get("/fhir/Task", headers=token).status_code == 200
    revoked = server.http.delete(f"/api/auth/clients/{client_id}", headers=server.person())
    assert revoked.status_code == 204
    refused = server.http.get("/fhir/Task", headers=token)
    assert refused.status_code == 401
    assert refused.headers["www-authenticate"] == 'Bearer realm="mendari", error="invalid_token"'
    again = ask(server.http, assertion(RSA_KEY, client_id))
    assert (again.status_code, again.json()) == (401, INVALID_CLIENT)


def test_a_token_acts_in_its_clients_organization_only(server: Server) -> None:
    a = server.register(org="clinic-a", scope=f"system/*.rs {DECIDE}")
    b = server.register(org="clinic-b", scope="system/*.rs")
    deciding = bearer(server.token(a, f"system/*.rs {DECIDE}"))
    made = server.http.post(f"/api/packs/{INBOX}/decide", json=message("refill"), headers=deciding)
    decision_id = made.json()["id"]
    reader_b = bearer(server.token(b))
    assert server.http.get(f"/fhir/Task/{decision_id}", headers=deciding).status_code == 200
    assert server.http.get(f"/fhir/Task/{decision_id}", headers=reader_b).status_code == 404
    found = server.http.get("/fhir/Task", params={"_total": "accurate"}, headers=reader_b)
    assert found.json()["total"] == 0
    audit = server.http.get("/fhir/AuditEvent", params={"_total": "accurate"}, headers=deciding)
    assert audit.json()["total"] >= 1


def test_tokens_stop_working_when_backend_services_is_turned_off(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = bearer(server.token(server.register()))
    monkeypatch.setenv("MENDARI_BACKEND_SERVICES", "off")
    off = TestClient(create_app(db_path=server.db), base_url=PUBLIC_URL)
    refused = off.get("/fhir/Task", headers=token)
    assert refused.status_code == 401
    assert refused.headers["www-authenticate"] == 'Bearer realm="mendari", error="invalid_token"'


def test_an_expired_token_is_refused_and_purged(server: Server) -> None:
    client_id = server.register()
    with AuditStore(server.db) as store:
        expired, _ = store.issue_token(client_id, "system/*.rs", 0)
        assert store.access_token(expired) is None
        live, until = store.issue_token(client_id, "system/*.rs", 300)  # purges the expired one
        assert store.access_token(live)["client_id"] == client_id  # type: ignore[index]
        assert until > time.time()
    assert server.rows("oauth_tokens") == 1
    assert server.http.get("/fhir/Task", headers=bearer(expired)).status_code == 401


def test_the_store_registers_machines_only(store: AuditStore) -> None:
    keys = check_jwks({"keys": [RSA_KEY.jwk]})
    for role in ("reviewer", "governance", "admin"):
        with pytest.raises(ValueError, match="role"):
            store.register_client(
                name="x", role=role, org="default", created_by="t", scopes="system/*.rs", jwks=keys
            )
    with pytest.raises(ValueError, match="scopes"):
        store.register_client(
            name="x", role="viewer", org="default", created_by="t", scopes=" ", jwks=keys
        )
    made = store.register_client(
        name="x", role="viewer", org="default", created_by="t", scopes="system/*.rs", jwks=keys
    )
    assert store.client(made["client_id"])["jwks"] == keys  # type: ignore[index]
    assert store.revoke_client(made["client_id"], "clinic-b", revoked_by="t") is False
    assert store.revoke_client(made["client_id"], revoked_by="t") is True
    assert store.client(made["client_id"]) is None


# -- replacing a client's key set, and its history (LP-12h) ---------------------------------


def replace(
    server: Server, client_id: str, *keys: Signer, headers: dict[str, str] | None = None
) -> Any:
    body = {"jwks": {"keys": [k.jwk for k in keys]}}
    return server.http.post(
        f"/api/auth/clients/{client_id}/keys", json=body, headers=headers or server.person()
    )


def client_chain(db: str, client_id: str) -> list[dict[str, Any]]:
    with AuditStore(db) as store, store._db.session(exclusive=False) as session:
        rows = session.all(
            "SELECT event, payload FROM audit_events WHERE decision_id = ? ORDER BY seq",
            (f"client:{client_id}",),
        )
    return [{"event": row["event"], **json.loads(row["payload"])} for row in rows]


def reads(server: Server, token: str) -> int:
    return int(server.http.get("/fhir/Task", headers=bearer(token)).status_code)


def test_replacing_a_clients_key_set_keeps_its_id_and_drops_its_tokens(
    server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    client_id = server.register(keys=(RSA_KEY,))
    old_token = server.token(client_id)
    assert reads(server, old_token) == 200
    capsys.readouterr()
    answer = replace(server, client_id, EC_KEY)
    assert answer.status_code == 200, answer.text
    replaced = answer.json()
    assert replaced["client_id"] == client_id and replaced["org"] == "default"
    assert [(k["kid"], k["kty"]) for k in replaced["keys"]] == [("ec-1", "EC")]
    assert [(k["kid"], k["kty"]) for k in replaced["keys_before"]] == [("rsa-1", "RSA")]
    assert replaced["replaced_by"] == "admin@default.example.org"
    assert replaced["tokens_dropped"] == 1
    [listed] = server.http.get("/api/auth/clients", headers=server.person()).json()
    assert listed["client_id"] == client_id and listed["keys"] == replaced["keys"]
    assert listed["keys_replaced_at"] == replaced["replaced_at"]
    assert (listed["role"], listed["scope"]) == ("service", default_scope("service"))
    assert reads(server, old_token) == 401  # its tokens stop at once
    refused = ask(server.http, assertion(RSA_KEY, client_id))
    assert (refused.status_code, refused.json()) == (401, INVALID_CLIENT)
    new_token = server.token(client_id, signer=EC_KEY)
    assert reads(server, new_token) == 200  # issued against the new set: it works
    logged = [e for e in security_lines(capsys.readouterr().out) if e["event"].startswith("admin.")]
    assert [(e["event"], e["ids"]) for e in logged] == [("admin.client.keys_replaced", [client_id])]
    chain = client_chain(server.db, client_id)
    assert [e["event"] for e in chain] == ["client.registered", "client.keys_replaced"]
    assert chain[1]["org"] == "default" and chain[1]["tokens_dropped"] == 1
    assert chain[1]["keys_before"] == chain[0]["keys"] and chain[1]["keys"] == replaced["keys"]
    chained = json.dumps(chain)
    assert RSA_KEY.jwk["n"] not in chained and EC_KEY.jwk["x"] not in chained
    assert '"n"' not in chained and '"x"' not in chained and '"y"' not in chained
    assert old_token not in chained and new_token not in chained
    with AuditStore(server.db) as store:
        assert store.verify()["ok"] is True


def test_a_token_verified_against_the_old_key_set_is_refused_after_a_replacement(
    store: AuditStore,
) -> None:
    """The race `keys_version` closes: a token request reads the client's keys and checks the
    assertion with them; the keys are replaced (its tokens dropped); then the token is
    inserted. It carries the version it was checked against, and is refused."""
    made = store.register_client(
        name="x", role="viewer", org="default", created_by="t", scopes="system/*.rs",
        jwks=check_jwks({"keys": [RSA_KEY.jwk]}),
    )  # fmt: skip
    client_id = made["client_id"]
    checked = store.client(client_id)
    assert checked is not None and checked["keys_version"] == 0
    store.replace_client_keys(
        client_id, "default", replaced_by="t", jwks=check_jwks({"keys": [EC_KEY.jwk]})
    )
    raced, _ = store.issue_token(
        client_id, "system/*.rs", 300, keys_version=checked["keys_version"]
    )
    assert store.access_token(raced) is None
    now = store.client(client_id)
    assert now is not None and now["keys_version"] == 1 and now["jwks"][0]["kid"] == "ec-1"
    fresh, _ = store.issue_token(client_id, "system/*.rs", 300, keys_version=now["keys_version"])
    assert store.access_token(fresh) is not None


def test_a_token_raced_against_a_replacement_at_the_endpoint_is_refused(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same race through the token endpoint: the keys are replaced after the assertion was
    checked (here, while its `jti` is claimed) and before the token is stored."""
    client_id = server.register(keys=(RSA_KEY,))
    claim = AuditStore.smart_claim
    raced: list[str] = []

    def replaced_meanwhile(self: AuditStore, kind: str, key: str, until: int) -> bool:
        claimed = claim(self, kind, key, until)
        if kind == "jti" and not raced:
            raced.append(key)
            with AuditStore(server.db) as other:
                other.replace_client_keys(
                    client_id, replaced_by="t", jwks=check_jwks({"keys": [EC_KEY.jwk]})
                )
        return claimed

    monkeypatch.setattr(AuditStore, "smart_claim", replaced_meanwhile)
    answer = ask(server.http, assertion(RSA_KEY, client_id))
    assert answer.status_code == 200 and raced  # checked against the old keys, then issued
    assert reads(server, answer.json()["access_token"]) == 401
    assert reads(server, server.token(client_id, signer=EC_KEY)) == 200


def test_what_a_key_set_replacement_refuses(server: Server) -> None:
    from tests.test_auth import legacy_key

    client_id = server.register(keys=(RSA_KEY,))
    gone = server.register(keys=(RSA_KEY,))
    assert (
        server.http.delete(f"/api/auth/clients/{gone}", headers=server.person()).status_code == 204
    )
    server.token(client_id)
    admin_key = bearer(legacy_key(server.db, "admin"))

    def state() -> Any:
        with AuditStore(server.db) as store, store._db.session(exclusive=False) as session:
            clients = session.all(
                "SELECT client_id, jwks, keys_version, keys_replaced_at FROM oauth_clients "
                "ORDER BY client_id"
            )
            tokens = session.all("SELECT token_sha256 FROM oauth_tokens ORDER BY token_sha256")
            return clients, tokens, store.verify()["events"]

    before = state()
    eleven = [jwk(EC_KEY, kid=f"k{n}") for n in range(11)]
    for why, target, body, headers, status in (
        ("unknown", "0" * 24, None, None, 404),
        ("another organization's", client_id, None, server.person(org="clinic-b"), 404),
        ("revoked", gone, None, None, 409),
        ("a private key", client_id, [private_jwk(EC_KEY)], None, 422),
        ("eleven keys", client_id, eleven, None, 422),
        ("no keys", client_id, [], None, 422),
        ("a key", client_id, None, admin_key, 403),
        ("anonymous", client_id, None, {}, 401),
    ):
        sent = {"jwks": {"keys": body if body is not None else [EC_KEY.jwk]}}
        refused = server.http.post(
            f"/api/auth/clients/{target}/keys",
            json=sent,
            headers=server.person() if headers is None else headers,
        )
        assert refused.status_code == status, (why, refused.text)
        assert target not in refused.text, why  # no id echoed
        assert private_jwk(EC_KEY)["d"] not in refused.text, why
    assert state() == before  # nothing written, nothing dropped


def test_a_gapless_switch_is_two_replacements(server: Server) -> None:
    """Old and new keys, then the new alone; twice in a row, by the documented steps, with
    tokens taken between them (lessons: a lifecycle operation, run twice)."""
    client_id = server.register(keys=(RSA_KEY,))
    for old, new in ((RSA_KEY, EC_KEY), (EC_KEY, RSA_KEY)):
        held = server.token(client_id, signer=old)
        assert replace(server, client_id, old, new).status_code == 200
        assert reads(server, held) == 401  # each step drops its tokens
        both = [server.token(client_id, signer=signer) for signer in (old, new)]
        assert [reads(server, token) for token in both] == [200, 200]
        assert replace(server, client_id, new).status_code == 200
        refused = ask(server.http, assertion(old, client_id))
        assert (refused.status_code, refused.json()) == (401, INVALID_CLIENT)
        assert reads(server, server.token(client_id, signer=new)) == 200
    with AuditStore(server.db) as store:
        current = store.client(client_id)
        assert current is not None and current["keys_version"] == 4
        assert store.verify()["ok"] is True
    events = [e["event"] for e in client_chain(server.db, client_id)]
    assert events == ["client.registered", *["client.keys_replaced"] * 4]


def test_a_clients_history_is_its_chain_in_order_and_its_organizations_only(
    server: Server,
) -> None:
    a = server.register(org="clinic-a", keys=(RSA_KEY,))
    admin_a, admin_b = server.person(org="clinic-a"), server.person(org="clinic-b")
    assert replace(server, a, EC_KEY, headers=admin_a).status_code == 200
    assert server.http.delete(f"/api/auth/clients/{a}", headers=admin_a).status_code == 204
    found = server.http.get(f"/api/auth/clients/{a}/history", headers=admin_a)
    assert found.status_code == 200
    history = found.json()
    assert (history["subject"], history["org"], history["truncated"]) == (
        f"client:{a}",
        "clinic-a",
        False,
    )
    assert [e["event"] for e in history["events"]] == [
        "client.registered",
        "client.keys_replaced",
        "client.revoked",
    ]
    assert all(set(e) == {"event", "at", "payload"} for e in history["events"])
    assert [e["at"] for e in history["events"]] == sorted(e["at"] for e in history["events"])
    assert history["events"][1]["payload"]["replaced_by"] == "admin@clinic-a.example.org"
    assert "hash" not in found.text and "seq" not in found.text
    before = client_chain(server.db, a)
    for path, method, body in (
        (f"/api/auth/clients/{a}/history", "GET", None),
        (f"/api/auth/clients/{a}/keys", "POST", {"jwks": {"keys": [RSA_KEY.jwk]}}),
        (f"/api/auth/clients/{'0' * 24}/history", "GET", None),
    ):
        refused = server.http.request(method, path, json=body, headers=admin_b)
        assert refused.status_code == 404, path
        assert refused.json()["detail"] == "no such client"  # the same for unknown and another's
    assert client_chain(server.db, a) == before


def test_health_says_whether_backend_services_is_on(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    health = server.http.get("/api/health", headers=server.person()).json()
    assert health["backend_services"] == {"token_endpoint": TOKEN_URL}
    assert "backend_services" not in server.http.get("/api/health").json()  # signed out
    monkeypatch.setenv("MENDARI_BACKEND_SERVICES", "off")
    off = TestClient(create_app(db_path=server.db), base_url=PUBLIC_URL)
    assert off.get("/api/health", headers=server.person()).json()["backend_services"] is None
    assert "backend_services" not in off.get("/api/health").json()


def test_refusals_are_logged_by_client_id_only(
    server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    """In the security log (server/security_log.py, which these lines were dropped before):
    the reason, and the client's id once it is known, never the assertion or the token."""
    client_id = server.register()
    bad_signature = assertion(STRANGER, client_id)
    stranger = assertion(RSA_KEY, "0" * 24)
    capsys.readouterr()
    ask(server.http, bad_signature)
    ask(server.http, stranger)
    ask(server.http, forged({"alg": "RS384", "kid": "rsa-1"}, {"iss": "x\ninjected"}))
    good = assertion(RSA_KEY, client_id)
    token = ask(server.http, good).json()["access_token"]
    logged = capsys.readouterr().out
    tokens = [
        (e["event"], e.get("reason"), e.get("actor"))
        for e in security_lines(logged)
        if e["event"].startswith("auth.token.")
    ]
    assert tokens == [
        ("auth.token.refused", "signature", f"client:{client_id}"),
        ("auth.token.refused", "client", None),
        ("auth.token.refused", "identity", None),
        ("auth.token.issued", None, f"client:{client_id}"),
    ]
    for secret in (bad_signature, stranger, good, token, "0" * 24, "injected"):
        assert secret not in logged
    assert "Signature verification failed" not in logged  # no library text


# -- end to end: Mendari's own Backend Services client ---------------------------------------


def through(http: TestClient, seen: list[dict[str, str]]) -> httpx.MockTransport:
    """An httpx transport that hands requests to the app (and keeps the token forms sent)."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/token":
            seen.append(dict(httpx.QueryParams(request.content.decode())))
        answer = http.request(
            request.method, str(request.url), headers=dict(request.headers), content=request.content
        )
        return httpx.Response(answer.status_code, headers=answer.headers, content=answer.content)

    return httpx.MockTransport(handle)


@pytest.mark.parametrize("signer", [RSA_KEY, EC_KEY], ids=["RS384", "ES384"])
def test_mendaris_bulk_client_gets_a_token_and_reads_tasks(server: Server, signer: Signer) -> None:
    client_id = server.register(scope=f"system/Task.rs {DECIDE}")
    deciding = bearer(server.token(client_id, DECIDE))
    made = server.http.post(f"/api/packs/{INBOX}/decide", json=message("refill"), headers=deciding)
    sent: list[dict[str, str]] = []
    with GuardedHttp(allowed={PUBLIC_URL}, transport=through(server.http, sent)) as http:
        token = bulk.backend_token(
            f"{PUBLIC_URL}/fhir",
            client_id=client_id,
            private_key=signer.pem(),
            key_id=signer.kid,
            scope="system/Task.rs system/AuditEvent.rs",
            http=http,
        )
        headers = {"accept": "application/fhir+json", **bearer(token)}
        bundle = http.get_json(f"{PUBLIC_URL}/fhir/Task", headers=headers)
        assert [e["resource"]["id"] for e in bundle["entry"]] == [made.json()["id"]]
        task = http.get_json(f"{PUBLIC_URL}/fhir/Task/{made.json()['id']}", headers=headers)
        assert task["resourceType"] == "Task"
        status, _, _ = http.raw("GET", f"{PUBLIC_URL}/fhir/AuditEvent", headers=headers)
        assert status == 403  # asked for, but not registered: not granted
    assert jwt.get_unverified_header(sent[0]["client_assertion"])["alg"] == signer.alg
    replayed = ask(server.http, sent[0]["client_assertion"], sent[0]["scope"])
    assert (replayed.status_code, replayed.json()) == (401, INVALID_CLIENT)


def test_the_api_reference_describes_the_token_flow(server: Server) -> None:
    schema = server.http.get("/openapi.json", headers=server.person()).json()
    flow = schema["components"]["securitySchemes"]["smartBackendServices"]
    assert flow["type"] == "oauth2"
    assert flow["flows"]["clientCredentials"]["tokenUrl"] == TOKEN_URL
    assert list(flow["flows"]["clientCredentials"]["scopes"]) == scopes.supported()
    assert "cannot run that flow" in flow["description"]  # no client secret, ever
    # A token is pasted as a plain bearer, where its scope reaches.
    token = schema["components"]["securitySchemes"]["accessToken"]
    assert (token["type"], token["scheme"]) == ("http", "bearer") and "mat_" in token["description"]
    paths = schema["paths"]
    search = paths["/fhir/Task"]["get"]
    assert search["security"] == [
        {"apiKey": []},
        {"smartBackendServices": ["system/Task.s"]},
        {"accessToken": []},
        {"session": []},
    ]
    assert "a scoped key or an access token, the scope `system/Task.s`" in search["description"]
    assert {"smartBackendServices": [DECIDE]} in paths["/fhir/$decide"]["post"]["security"]
    stats = paths["/api/stats"]["get"]
    assert stats["security"] == [{"apiKey": []}, {"session": []}]
    assert "a scoped key or an access token may not call this" in stats["description"]
    assert paths["/api/auth/clients"]["post"]["security"] == [{"session": []}]
    assert paths["/api/auth/clients/{client_id}/keys"]["post"]["security"] == [{"session": []}]
    assert paths["/api/auth/clients/{client_id}/history"]["get"]["security"] == [{"session": []}]
    assert paths["/auth/token"]["post"]["security"] == []
    assert paths[DISCOVERY_PATH]["get"]["security"] == []
    form = paths["/auth/token"]["post"]["requestBody"]["content"]
    assert set(form["application/x-www-form-urlencoded"]["schema"]["required"]) == {
        "grant_type",
        "client_assertion_type",
        "client_assertion",
        "scope",
    }
    assert "SMART Backend Services is on" in schema["info"]["description"]
    assert TOKEN_URL in schema["info"]["description"]
