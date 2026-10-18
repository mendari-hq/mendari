# SPDX-License-Identifier: Apache-2.0
"""An EHR calls the CDS Hooks services with its own signed token, over HTTP (LP-23b,
ADR-0014): with the registry on, a verified token is a `cds` principal of its registered
organization that lists and calls the services and reaches nothing else; every token that does
not verify is the one `401`, logged by its reason only; open mode verifies too; the
`fhirServer` it sends must be one it registered. Every token test shows the registry is on
(health lists the client) and that what a valid token makes is a `cds` principal (the access
log's caller and the chain's `requested_by`). Keys are made at run time."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Route

from mendari.core.audit import AuditStore
from mendari.server.app import create_app
from mendari.server.auth import CDS_PATH, permission_for
from tests.conftest import encrypt_mode, security_lines, signed_in
from tests.jwt_signers import DROP, PUBLIC_URL, cds_token, forged
from tests.test_cds_clients import (
    BASE,
    DISCOVERY,
    EC_EHR,
    EHR,
    HOOK,
    INVALID,
    JWKS_URI,
    OTHER_EHR,
    RSA_EHR,
    STRANGER,
    entry,
    jwks_server,
    register,
    token,
)
from tests.test_fhir_client import allow_ehr, referral_ehr
from tests.test_limits import order_sign

NOT_VALID = {"error": "AuthError", "detail": "the credential is not valid"}


def bearer(sent: str) -> dict[str, str]:
    return {"authorization": f"Bearer {sent}"}


def sign_in_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off


@dataclass
class Served:
    http: TestClient
    db: str

    def admin(self, org: str = "default") -> dict[str, str]:
        with AuditStore(self.db) as store:
            return signed_in(store, role="admin", org=org)

    def asked_by(self) -> list[str | None]:
        """Who each decision recorded asked for it (`requested_by`), oldest first."""
        with AuditStore(self.db) as store:
            events = store.events()
        return [
            e["payload"].get("requested_by") for e in events if e["event"] == "decision.created"
        ]

    def claims(self) -> int:
        with AuditStore(self.db) as store, store._db.session(exclusive=False) as db:
            row = db.one("SELECT COUNT(*) AS n FROM smart_sessions WHERE kind = 'cds-jti'")
        assert row is not None
        return int(row["n"])

    def registry_is_on(self, *ids: str, org: str = "default") -> None:
        shown = self.http.get("/api/health", headers=self.admin(org)).json()
        assert shown["cds_clients"] == list(ids or ["epic"]), shown["cds_clients"]


@pytest.fixture
def served(db_target: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Served]:
    """Sign-in on (its identity provider never called), the EHR registered by value."""
    sign_in_on(monkeypatch)
    register(monkeypatch)
    with TestClient(create_app(db_path=db_target), base_url=PUBLIC_URL) as http:
        yield Served(http, db_target)


def callers(out: str) -> list[str | None]:
    """Who the access lines name, request by request."""
    return [line["actor"] for line in security_lines(out) if line["event"] == "request"]


def test_a_valid_token_reads_discovery_and_calls_the_hook(
    served: Served, capsys: pytest.CaptureFixture[str]
) -> None:
    """With sign-in on, the EHR's own token, RS384 or ES384, each for the URL it calls, reads
    discovery and calls a hook: the decision is recorded and chained as asked for by
    `cds epic`, in the client's organization, and the access log names the caller `cds:epic`."""
    served.registry_is_on()
    capsys.readouterr()
    listed = served.http.get(DISCOVERY, headers=bearer(token()))
    assert listed.status_code == 200, listed.text
    assert "mendari.referral-router" in {s["id"] for s in listed.json()["services"]}
    for signer in (RSA_EHR, EC_EHR):
        called = served.http.post(HOOK, json=order_sign(1), headers=bearer(token(signer, HOOK)))
        assert called.status_code == 200, called.text
        assert len(called.json()["cards"]) == 1
    assert callers(capsys.readouterr().out) == ["cds:epic"] * 3
    assert served.asked_by() == ["cds epic", "cds epic"]
    with AuditStore(served.db) as store, store._db.session(exclusive=False) as db:
        orgs = {row["org"] for row in db.all("SELECT org FROM decisions")}
    assert orgs == {"default"}
    assert served.claims() == 3


def test_wrong_iss_wrong_aud_expired_replayed_and_unregistered_jku_are_each_refused_over_http(
    served: Served, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each is the one `401` (`invalid_token`, the same body), decides nothing and claims
    nothing; the security log says which it was."""
    served.registry_is_on()
    used = token(path=HOOK)
    assert served.http.post(HOOK, json=order_sign(1), headers=bearer(used)).status_code == 200
    capsys.readouterr()
    refused = {
        "cds_issuer": token(path=HOOK, iss="https://evil.example"),
        "cds_audience": token(path=DISCOVERY),
        "cds_expired": token(path=HOOK, lifetime=-120),
        "cds_replayed": used,
        "cds_jku": token(path=HOOK, jku="https://evil.example/jwks.json"),
    }
    for sent in refused.values():
        answer = served.http.post(HOOK, json=order_sign(1), headers=bearer(sent))
        assert answer.status_code == 401
        assert answer.headers["www-authenticate"] == INVALID
        body = answer.json()
        assert body.pop("request_id") == answer.headers["x-request-id"]
        assert body == NOT_VALID
        assert sent not in answer.text
    lines = security_lines(capsys.readouterr().out)
    logged = [line["reason"] for line in lines if line["event"] == "auth.credential.refused"]
    assert logged == list(refused)
    assert served.asked_by() == ["cds epic"] and served.claims() == 1


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x1", path)


def test_a_cds_client_reaches_nothing_but_cds_services(
    served: Served, capsys: pytest.CaptureFixture[str]
) -> None:
    """Over the route inventory: a valid token, made for the very URL it is sent to, is the one
    `401` on every route that is not public and not under `/cds-services` (it is never read as a
    credential there, so it claims nothing), and lets the caller through on the CDS services
    only. Public routes need no credential and read none (`tests/test_auth.py`)."""
    served.registry_is_on()
    app = served.http.app
    routes = sorted(
        (method, route.path)
        for route in app.routes  # type: ignore[attr-defined]
        if isinstance(route, Route)
        for method in route.methods or ()
    )
    cds = [r for r in routes if CDS_PATH.match(r[1])]
    assert cds == [("GET", "/cds-services"), ("POST", "/cds-services/{service_id}")]
    elsewhere = [r for r in routes if not CDS_PATH.match(r[1]) and permission_for(*r) != "public"]
    assert len(elsewhere) > 60
    capsys.readouterr()
    for method, path in elsewhere:
        concrete = _concrete(path)
        sent = token(path=concrete)
        answer = served.http.request(method, concrete, headers=bearer(sent))
        assert answer.status_code == 401, (method, path, answer.status_code)
        assert answer.headers["www-authenticate"] == INVALID, (method, path)
    out = capsys.readouterr().out
    assert "cds:epic" not in out and served.claims() == 0
    reasons = {
        line["reason"] for line in security_lines(out) if line["event"] == "auth.credential.refused"
    }
    assert reasons == {"malformed"}  # an RS384 token is past the bearer cap everywhere else
    assert served.http.get(DISCOVERY, headers=bearer(token())).status_code == 200
    hooked = served.http.post(HOOK, json=order_sign(1), headers=bearer(token(path=HOOK)))
    assert hooked.status_code == 200
    assert callers(capsys.readouterr().out) == ["cds:epic", "cds:epic"]


def test_open_mode_refuses_an_unverified_jwt_on_cds_services(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With sign-in off a JWT on `/cds-services` is verified too: without a registry every one is
    refused (a forged token got cards before LP-23b), while a request with no credential, or with
    any other, is answered as before. With a registry (organization `default`), a valid token is
    a `cds` principal whose decisions name it."""
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", EHR)
    plain = TestClient(create_app(db_path=tmp_path / "open.db"))
    unsigned = forged(
        {"alg": "none", "typ": "JWT", "kid": "ehr-rsa"},
        {"iss": EHR, "aud": f"http://testserver{HOOK}", "exp": 9999999999, "jti": "x"},
        b"",
    )
    capsys.readouterr()
    for sent in (token(), token(EC_EHR), unsigned, "a.b.c"):
        assert plain.get(DISCOVERY, headers=bearer(sent)).status_code == 401
        refused = plain.post(HOOK, json=order_sign(1), headers=bearer(sent))
        assert refused.status_code == 401 and refused.headers["www-authenticate"] == INVALID
    reasons = [
        line["reason"]
        for line in security_lines(capsys.readouterr().out)
        if line["event"] == "auth.credential.refused"
    ]
    assert reasons == ["cds_unregistered"] * 8
    for headers in ({}, bearer("ohk_not-a-key"), {"authorization": "Basic dXNlcjpwYXNz"}):
        assert plain.get(DISCOVERY, headers=headers).status_code == 200
    assert plain.post(HOOK, json=order_sign(1)).status_code == 200  # no credential, as before
    with AuditStore(tmp_path / "open.db") as store:
        assert len(store.query(limit=10)) == 1

    register(monkeypatch)
    registered = TestClient(create_app(db_path=tmp_path / "registered.db"), base_url=PUBLIC_URL)
    assert registered.get("/api/health").json()["cds_clients"] == ["epic"]
    capsys.readouterr()
    called = registered.post(HOOK, json=order_sign(1), headers=bearer(token(path=HOOK)))
    assert called.status_code == 200, called.text
    assert callers(capsys.readouterr().out) == ["cds:epic"]
    assert registered.post(HOOK, json=order_sign(1), headers=bearer(unsigned)).status_code == 401
    stranger = registered.post(HOOK, json=order_sign(1), headers=bearer(token(STRANGER, HOOK)))
    assert stranger.status_code == 401
    with AuditStore(tmp_path / "registered.db") as store:
        events = store.events()
    created = [e["payload"] for e in events if e["event"] == "decision.created"]
    assert [payload.get("requested_by") for payload in created] == ["cds epic"]


def test_a_fhir_server_the_client_did_not_register_is_refused_before_any_read(
    db_target: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `fhirServer` on the allow-list but not among the client's bases (or, for a client that
    pins it, not its `iss`), or not a URL at all, is a `403` before anything is charged, fetched
    or decided; the client's own base is read as before."""
    sign_in_on(monkeypatch)
    monkeypatch.setenv("MENDARI_PRINCIPAL_DAILY_LIMIT", "10")
    request, _, handler, seen = referral_ehr()
    allow_ehr(monkeypatch, handler)
    elsewhere = entry(fhir_servers=[f"{EHR}/other"])
    pinned = entry(
        "pinned", iss=BASE, fhir_servers=[f"{EHR}/other", BASE], iss_is_fhir_server=True,
        jwks={"keys": [OTHER_EHR.jwk]},
    )  # fmt: skip
    register(monkeypatch, elsewhere, pinned)
    http = TestClient(create_app(db_path=db_target), base_url=PUBLIC_URL)
    served = Served(http, db_target)
    served.registry_is_on("epic", "pinned")
    capsys.readouterr()
    refused = [
        (token(path=HOOK), request),  # epic did not register this base
        (token(path=HOOK), {**request, "fhirServer": ["https://ehr.example.org/fhir"]}),
        (token(path=HOOK), {**request, "fhirServer": f"{BASE}/../other"}),
        (token(path=HOOK), {**request, "fhirServer": "https://[ehr.example.org/fhir"}),
        (token(OTHER_EHR, HOOK, iss=BASE), {**request, "fhirServer": f"{EHR}/other"}),
    ]
    for sent, body in refused:
        answer = http.post(HOOK, json=body, headers=bearer(sent))
        assert answer.status_code == 403, answer.text
        assert "not one registered for this CDS client" in answer.text
    assert seen == [] and served.asked_by() == []
    with AuditStore(db_target) as store:
        assert store.principal_usage("cds:epic") == store.principal_usage("cds:pinned") == 0
    lines = security_lines(capsys.readouterr().out)
    assert [line["reason"] for line in lines if line["event"] == "auth.permission.refused"] == [
        "cds_fhir_server_not_registered"
    ] * 5
    requests = [line["actor"] for line in lines if line["event"] == "request"]
    assert requests == ["cds:epic"] * 4 + ["cds:pinned"]
    answer = http.post(HOOK, json=request, headers=bearer(token(OTHER_EHR, HOOK, iss=BASE)))
    assert answer.status_code == 200, answer.text
    assert len(seen) == 4 and served.asked_by() == ["cds pinned"]


def test_a_cds_client_decides_in_its_registered_organization(
    db_target: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Memberships mode: a client of an organization with a record decides in it; one whose
    organization has no record is refused (`cds_no_organization_record`), decides nothing and
    claims nothing: its token's `jti` stays free."""
    sign_in_on(monkeypatch)
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    south = entry("south-ehr", iss=f"{EHR}/south", org="south", jwks={"keys": [OTHER_EHR.jwk]})
    register(monkeypatch, entry(org="north"), south)
    with AuditStore(db_target) as store:
        store.create_organization(org="north", name="North", created_by="test")
    http = TestClient(create_app(db_path=db_target), base_url=PUBLIC_URL)
    capsys.readouterr()
    called = http.post(HOOK, json=order_sign(1), headers=bearer(token(path=HOOK)))
    assert called.status_code == 200, called.text
    theirs = token(OTHER_EHR, HOOK, iss=f"{EHR}/south")
    refused = http.post(HOOK, json=order_sign(1), headers=bearer(theirs))
    assert refused.status_code == 401 and refused.headers["www-authenticate"] == INVALID
    lines = security_lines(capsys.readouterr().out)
    assert [line["actor"] for line in lines if line["event"] == "request"] == [
        "cds:epic",
        "anonymous",
    ]
    (refusal,) = [line for line in lines if line["event"] == "auth.credential.refused"]
    assert (refusal["reason"], refusal["actor"]) == ("cds_no_organization_record", "cds:south-ehr")
    with AuditStore(db_target) as store, store._db.session(exclusive=False) as db:
        orgs = [row["org"] for row in db.all("SELECT org FROM decisions")]
    assert orgs == ["north"]
    assert Served(http, db_target).claims() == 1  # north's call only


def test_a_long_jwt_elsewhere_is_still_malformed(
    served: Served, capsys: pytest.CaptureFixture[str]
) -> None:
    """Off `/cds-services` a JWT is what it was before LP-23b: past the 512-character bearer cap
    `malformed`, under it a prefix Mendari does not issue; on `/cds-services` a token past
    8 KiB is refused by its size. Keys and tokens Mendari issues are unchanged (their own tests),
    and the registry is on."""
    served.registry_is_on()
    capsys.readouterr()
    long, short = token(path="/api/packs"), token(EC_EHR, "/api/packs")
    assert len(long) > 512 >= len(short)
    for sent in (long, short):
        assert served.http.get("/api/packs", headers=bearer(sent)).status_code == 401
    huge = token(pad="x" * 9000)
    assert served.http.get(DISCOVERY, headers=bearer(huge)).status_code == 401
    lines = security_lines(capsys.readouterr().out)
    logged = [line["reason"] for line in lines if line["event"] == "auth.credential.refused"]
    assert logged == ["malformed", "unknown_prefix", "cds_size"]


def test_each_refusal_is_logged_with_its_reason_and_no_token_or_claim(
    served: Served, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refusal's line names the reason, and the registered client once its `iss` names one
    (`cds:<id>`), never the token, its `kid`, `jti`, `sub`, `aud` or a claimed `iss`."""
    served.registry_is_on()
    capsys.readouterr()
    jti, sub, kid = "jti-marker-4f2a", "sub-marker-9c1d", "kid-marker-77e0"
    sent = [
        (token(iss="https://iss-marker.example", jti=jti), None),
        (token(STRANGER, jti=jti, sub=sub), "cds:epic"),
        (token(headers={"kid": kid}, jti=jti), "cds:epic"),
        (token(lifetime=-120, jti=jti), "cds:epic"),
        (token(tenant="tenant-marker", jti=jti), "cds:epic"),
    ]
    for each, _ in sent:
        assert served.http.get(DISCOVERY, headers=bearer(each)).status_code == 401
    out = capsys.readouterr().out
    lines = [line for line in security_lines(out) if line["event"] == "auth.credential.refused"]
    assert [(line["reason"], line.get("actor")) for line in lines] == [
        ("cds_issuer", None),
        ("cds_signature", "cds:epic"),
        ("cds_key", "cds:epic"),
        ("cds_expired", "cds:epic"),
        ("cds_tenant", "cds:epic"),
    ]
    for secret in (jti, sub, kid, "iss-marker", "tenant-marker", PUBLIC_URL, *(s for s, _ in sent)):
        assert secret not in out


def test_health_shows_client_ids_to_read_principals_only(
    db_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Signed in, `cds_clients` lists the ids of the caller's organization's clients; signed
    out, to a scoped key, or to a CDS client's own token, health shows its three fields only."""
    sign_in_on(monkeypatch)
    monkeypatch.setenv("MENDARI_OIDC_ORG_CLAIM", "org")
    north = entry("north-ehr", iss=f"{EHR}/north", org="north", jwks={"keys": [OTHER_EHR.jwk]})
    register(monkeypatch, entry(), north)
    served = Served(TestClient(create_app(db_path=db_target), base_url=PUBLIC_URL), db_target)
    served.registry_is_on("epic")
    served.registry_is_on("north-ehr", org="north")
    with AuditStore(db_target) as store:
        viewer = signed_in(store, role="viewer", org="elsewhere")
        key = store.create_api_key(
            name="hooks", role="service", org="default", created_by="t",
            scopes="https://mendari.co/fhir/scope/decide",
        )["key"]  # fmt: skip
    assert served.http.get("/api/health", headers=viewer).json()["cds_clients"] == []
    public = {"status", "version", "sign_in"}
    for headers in ({}, bearer(key), bearer(token(path="/api/health"))):
        assert set(served.http.get("/api/health", headers=headers).json()) == public


def test_a_client_with_a_jwks_uri_is_verified_over_http(
    db_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The EHR's keys by their pinned URL, fetched through the allow-listed client once and kept:
    a token with `jku` equal to it, and one without, are verified."""
    sign_in_on(monkeypatch)
    fetched = jwks_server(monkeypatch, {"keys": [RSA_EHR.jwk, EC_EHR.jwk]})
    register(monkeypatch, entry(jwks=DROP, jwks_uri=JWKS_URI))
    http = TestClient(create_app(db_path=db_target), base_url=PUBLIC_URL)
    for sent in (token(jku=JWKS_URI), token(EC_EHR), token()):
        assert http.get(DISCOVERY, headers=bearer(sent)).status_code == 200
    assert fetched == [JWKS_URI]
    assert http.get(DISCOVERY, headers=bearer(cds_token(RSA_EHR, EHR, "x"))).status_code == 401
