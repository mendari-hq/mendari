# SPDX-License-Identifier: Apache-2.0
"""Signed CDS client tokens (server/cds_clients.py, LP-23b, ADR-0014): the registry
(`MENDARI_CDS_CLIENTS`, checked at startup), the keys (by value, or from a pinned `jwks_uri`
through the allow-listed client) and the verifier, through the guard (`Auth.check`), so each
accepted token is shown to be a `cds` principal and each refusal its one `401` with the reason
in the security log. Keys are made at run time; no request leaves the test (the EHR's JWK Set
is a `MockTransport`)."""

from __future__ import annotations

import ast
import base64
import hashlib
import hmac
import json
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi.testclient import TestClient
from starlette.requests import Request

from mendari.core.audit import SESSION_PURGE_BATCH, AuditStore
from mendari.errors import AuthError, MendariError
from mendari.server import auth as auth_module
from mendari.server import cds_clients as cds_module
from mendari.server.app import create_app
from mendari.server.auth import CDS_PERMISSIONS, Auth, AuthSettings, Principal
from mendari.server.cds_clients import (
    REASONS,
    CdsRegistry,
    JwksCache,
    cds_clients,
    fhir_server_allowed,
)
from mendari.standards.fhir_client import GuardedHttp
from mendari.standards.smart import Registration
from tests.conftest import encrypt_mode, security_lines
from tests.jwt_signers import DROP, PUBLIC_URL, Signer, cds_token, forged

ROOT = Path(__file__).resolve().parents[1]
EHR = "https://ehr.example.org"
BASE = f"{EHR}/fhir"
JWKS_URI = f"{EHR}/.well-known/jwks.json"
DISCOVERY = "/cds-services"
HOOK = "/cds-services/mendari.referral-router"
INVALID = 'Bearer realm="mendari", error="invalid_token"'
# The EHR's keys, made for this run: RSA and EC, and another party's with the same kid.
RSA_EHR = Signer("RS384", "ehr-rsa", rsa.generate_private_key(public_exponent=65537, key_size=2048))
EC_EHR = Signer("ES384", "ehr-ec", ec.generate_private_key(ec.SECP384R1()))
STRANGER = Signer(
    "RS384", "ehr-rsa", rsa.generate_private_key(public_exponent=65537, key_size=2048)
)
OTHER_EHR = Signer("ES384", "other-ec", ec.generate_private_key(ec.SECP384R1()))


def entry(client_id: str = "epic", **fields: Any) -> dict[str, Any]:
    """A registry entry: the EHR at `EHR`, its keys by value (RSA and EC), organization
    `default`, its one FHIR base; each field overridden by `fields`, or left out with `DROP`."""
    given: dict[str, Any] = {
        "id": client_id,
        "title": "Synthetic EHR",
        "iss": EHR,
        "org": "default",
        "fhir_servers": [BASE],
        "jwks": {"keys": [RSA_EHR.jwk, EC_EHR.jwk]},
    }
    given |= fields
    return {name: value for name, value in given.items() if value is not DROP}


def register(
    monkeypatch: pytest.MonkeyPatch,
    *entries: dict[str, Any],
    allowed: str = EHR,
    public_url: str | None = PUBLIC_URL,
) -> None:
    """`MENDARI_CDS_CLIENTS` set to `entries` (by default, `entry()` alone), with the EHR on the
    FHIR allow-list and the public URL the tokens are addressed to."""
    monkeypatch.setenv("MENDARI_CDS_CLIENTS", json.dumps(list(entries) or [entry()]))
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", allowed)
    if public_url is None:
        monkeypatch.delenv("MENDARI_PUBLIC_URL", raising=False)
    else:
        monkeypatch.setenv("MENDARI_PUBLIC_URL", public_url)


def token(signer: Signer = RSA_EHR, path: str = DISCOVERY, *, iss: str = EHR, **claims: Any) -> str:
    """A token from the EHR for `path` on this deployment (`aud` its public URL and the path)."""
    return cds_token(signer, iss, f"{PUBLIC_URL}{path}", **claims)


SETTINGS = AuthSettings(
    issuer="https://idp.example.org",
    client_id="mendari",
    client_secret=None,
    role_claim="roles",
    role_map={},
    org_claim=None,
    public_url=PUBLIC_URL,
)


@dataclass
class Guard:
    """The sign-in guard with the registry `MENDARI_CDS_CLIENTS` gives, over a store: what it
    answers a request (without running the route), and the jti claims it made."""

    auth: Auth
    store: AuditStore
    seen: list[Principal] = field(default_factory=list)

    def ask(self, sent: str | None, path: str = DISCOVERY, method: str = "GET") -> int:
        """The guard's status for one request with `Authorization: Bearer <sent>` (200: it may
        go on, and the principal it let through is kept in `seen`)."""
        headers = [] if sent is None else [(b"authorization", f"Bearer {sent}".encode())]
        request = Request(
            {
                "type": "http",
                "scheme": "https",
                "server": ("mendari.test", 443),
                "method": method,
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "headers": headers,
            }
        )
        try:
            refused = self.auth.check(request)
        except AuthError as error:
            if error.status == 401:
                assert error.challenge == INVALID, error.challenge
                assert str(error) == "the credential is not valid"
            return error.status
        if refused is not None:
            return refused.status_code
        principal = getattr(request.state, "principal", None)
        if principal is not None:
            self.seen.append(principal)
        return 200

    def accepts(self, sent: str, path: str = DISCOVERY, method: str = "GET") -> Principal:
        """A token the guard lets through as a `cds` principal (the registry is on)."""
        assert self.auth.cds is not None and self.auth.cds.clients, "the registry is on"
        assert self.ask(sent, path, method) == 200
        principal = self.seen[-1]
        assert principal.kind == "cds" and principal.permissions() == CDS_PERMISSIONS
        return principal

    def claims(self) -> int:
        with self.store._db.session(exclusive=False) as db:
            row = db.one("SELECT COUNT(*) AS n FROM smart_sessions WHERE kind = 'cds-jti'")
        assert row is not None
        return int(row["n"])


def guard_for(
    store: AuditStore,
    *,
    signed_in: bool = True,
    ehrs: dict[str, Registration] | None = None,
    keys: JwksCache | None = None,
) -> Guard:
    registry = cds_clients(signed_in=signed_in, ehrs=ehrs or {})
    assert registry is not None
    if keys is not None:
        registry.keys = keys
    return Guard(Auth(SETTINGS if signed_in else None, store, cds=registry), store)


@pytest.fixture
def guard(db_target: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Guard]:
    register(monkeypatch)
    with AuditStore(db_target) as store:
        yield guard_for(store)


def reasons(out: str) -> list[str]:
    """The reasons of the credential refusals the security log holds."""
    return [
        line["reason"] for line in security_lines(out) if line["event"] == "auth.credential.refused"
    ]


# -- tokens that verify ------------------------------------------------------------------------


@pytest.mark.parametrize("signer", [RSA_EHR, EC_EHR], ids=["RS384", "ES384"])
def test_a_valid_token_is_accepted(guard: Guard, signer: Signer) -> None:
    """A token the EHR signs (CDS Hooks 2.0's header and claims, Inferno's form) is a `cds`
    principal of the registered client, in its organization, on discovery and on a hook, each
    with its own `aud`; its jti is claimed once."""
    principal = guard.accepts(token(signer))
    assert (principal.id, principal.actor, principal.org) == ("cds:epic", "cds epic", "default")
    assert principal.roles == frozenset() and principal.grant is None
    hooked = guard.accepts(token(signer, HOOK), HOOK, "POST")
    assert hooked.id == "cds:epic"
    assert guard.claims() == 2
    now = int(time.time())
    within_skew = token(signer, iat=now - 100, exp=now - 10)  # expired 10 s ago: within 30 s
    assert guard.accepts(within_skew).id == "cds:epic"


def test_a_wrong_issuer_is_refused(guard: Guard, capsys: pytest.CaptureFixture[str]) -> None:
    """An `iss` no client registered, one that differs from the registered one only in case or a
    trailing slash, or none at all; and a token naming the registered `iss` but signed with
    another key that has the same `kid`."""
    guard.accepts(token())
    capsys.readouterr()
    for iss in ("https://ehr.example.org/", "HTTPS://EHR.EXAMPLE.ORG", "https://evil.example"):
        assert guard.ask(token(iss=iss)) == 401, iss
    assert guard.ask(token(iss=DROP)) == 401  # type: ignore[arg-type]
    assert guard.ask(token(STRANGER)) == 401
    assert reasons(capsys.readouterr().out) == ["cds_issuer"] * 4 + ["cds_signature"]
    assert guard.claims() == 1


def test_a_wrong_audience_is_refused(guard: Guard, capsys: pytest.CaptureFixture[str]) -> None:
    """`aud` is the public URL and the path called, compared exactly: a discovery token on a hook
    and the reverse, another host, the `Host` header's URL, a trailing slash, a list (even of
    the right one), and none."""
    guard.accepts(token())
    capsys.readouterr()
    sent = [
        (token(path=DISCOVERY), HOOK, "POST"),
        (token(path=HOOK), DISCOVERY, "GET"),
        (cds_token(RSA_EHR, EHR, f"https://other.example{DISCOVERY}"), DISCOVERY, "GET"),
        (cds_token(RSA_EHR, EHR, f"http://testserver{DISCOVERY}"), DISCOVERY, "GET"),
        (cds_token(RSA_EHR, EHR, f"{PUBLIC_URL}{DISCOVERY}/"), DISCOVERY, "GET"),
        (cds_token(RSA_EHR, EHR, [f"{PUBLIC_URL}{DISCOVERY}"]), DISCOVERY, "GET"),  # type: ignore[arg-type]
        (cds_token(RSA_EHR, EHR, DROP), DISCOVERY, "GET"),  # type: ignore[arg-type]
    ]
    for sent_token, path, method in sent:
        assert guard.ask(sent_token, path, method) == 401, path
    assert reasons(capsys.readouterr().out) == ["cds_audience"] * 6 + ["cds_claims"]
    assert guard.claims() == 1


def test_an_expired_token_is_refused(guard: Guard, capsys: pytest.CaptureFixture[str]) -> None:
    guard.accepts(token())
    capsys.readouterr()
    now = int(time.time())
    assert guard.ask(token(exp=now - 31, iat=now - 300)) == 401
    assert guard.ask(token(lifetime=-120)) == 401
    assert reasons(capsys.readouterr().out) == ["cds_expired", "cds_expired"]
    assert guard.claims() == 1


def test_a_token_from_the_future_or_living_too_long_is_refused(
    guard: Guard, capsys: pytest.CaptureFixture[str]
) -> None:
    """`iat` ahead of now beyond the skew, `nbf` ahead, more than 330 s between `iat` and `exp`
    or from now to `exp`; and `exp`, `iat` or `jti` missing or of the wrong kind."""
    guard.accepts(token())
    capsys.readouterr()
    now = int(time.time())
    refused = {
        "iat ahead": (token(iat=now + 120, exp=now + 300), "cds_not_yet_valid"),
        "nbf ahead": (token(nbf=now + 120), "cds_not_yet_valid"),
        "an hour ahead": (token(lifetime=3600), "cds_lifetime"),
        "331 s from iat": (token(iat=now - 100, exp=now + 231), "cds_lifetime"),
        "340 s from now": (token(iat=now + 20, exp=now + 340), "cds_lifetime"),
        "no exp": (token(exp=DROP), "cds_claims"),
        "no iat": (token(iat=DROP), "cds_claims"),
        "no jti": (token(jti=DROP), "cds_claims"),
        "iat a string": (token(iat=str(now)), "cds_claims"),
        "exp a string": (token(exp=str(now + 60)), "cds_claims"),
        "exp true": (token(exp=True), "cds_expired"),  # PyJWT reads it as 1
        "jti a number": (token(jti=7), "cds_claims"),
        "empty jti": (token(jti=""), "cds_jti"),
        "long jti": (token(jti="j" * 257), "cds_jti"),
    }
    for why, (sent, _) in refused.items():
        assert guard.ask(sent) == 401, why
    assert reasons(capsys.readouterr().out) == [reason for _, reason in refused.values()]
    assert guard.claims() == 1
    assert guard.accepts(token(iat=now - 30, exp=now + 300)).id == "cds:epic"  # 330 s exactly


def test_a_replayed_jti_is_refused_and_a_jti_is_per_client(
    db_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Over HTTP, through two instances of the app on one database (SQLite and Postgres): a
    token used once is refused on the other instance, and the same `jti` from another client is
    its own."""
    encrypt_mode(monkeypatch)
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    other = entry("other", iss="https://other-ehr.example.org", jwks={"keys": [OTHER_EHR.jwk]})
    register(monkeypatch, entry(), other)
    first = TestClient(create_app(db_path=db_target), base_url=PUBLIC_URL)
    second = TestClient(create_app(db_path=db_target), base_url=PUBLIC_URL)
    sent = token(jti="the-same-jti")
    once = first.get(DISCOVERY, headers={"authorization": f"Bearer {sent}"})
    assert once.status_code == 200, once.text
    again = second.get(DISCOVERY, headers={"authorization": f"Bearer {sent}"})
    assert again.status_code == 401 and again.headers["www-authenticate"] == INVALID
    theirs = token(OTHER_EHR, iss="https://other-ehr.example.org", jti="the-same-jti")
    assert second.get(DISCOVERY, headers={"authorization": f"Bearer {theirs}"}).status_code == 200
    assert first.get(DISCOVERY, headers={"authorization": f"Bearer {theirs}"}).status_code == 401
    with AuditStore(db_target) as store, store._db.session(exclusive=False) as db:
        rows = db.all("SELECT key_sha256, data FROM smart_sessions WHERE kind = 'cds-jti'")
    assert len(rows) == 2 and {row["data"] for row in rows} == {"{}"}
    assert all("the-same-jti" not in row["key_sha256"] for row in rows)  # stored as a digest


def test_a_subject_or_tenant_other_than_registered_is_refused(
    db_target: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pinned `sub` and `tenant` must be sent exactly; a `tenant` from a client that pins none
    is refused (an EHR shared by several organizations is not supported), null included."""
    pinned = entry("pinned", sub="ehr-client-1", tenant="tenant-a")
    plain = entry("plain", iss="https://other-ehr.example.org", jwks={"keys": [OTHER_EHR.jwk]})
    register(monkeypatch, pinned, plain)
    with AuditStore(db_target) as store:
        guard = guard_for(store)
        assert guard.accepts(token(sub="ehr-client-1", tenant="tenant-a")).id == "cds:pinned"
        theirs: dict[str, Any] = {"signer": OTHER_EHR, "iss": "https://other-ehr.example.org"}
        assert guard.accepts(token(**theirs, sub="anyone")).id == "cds:plain"  # not pinned
        capsys.readouterr()
        refused = [
            (token(sub="ehr-client-2", tenant="tenant-a"), "cds_subject"),
            (token(tenant="tenant-a"), "cds_subject"),
            (token(sub="ehr-client-1", tenant="tenant-b"), "cds_tenant"),
            (token(sub="ehr-client-1"), "cds_tenant"),
            (token(**theirs, tenant="tenant-a"), "cds_tenant"),
            (token(**theirs, tenant=None), "cds_tenant"),
        ]
        for sent, _ in refused:
            assert guard.ask(sent) == 401
        assert reasons(capsys.readouterr().out) == [reason for _, reason in refused]
        assert guard.claims() == 2


# -- the header and the algorithm ----------------------------------------------------------------


def hs384(header: dict[str, Any], claims: dict[str, Any], secret: bytes) -> str:
    """An HS384 token, MACed with `secret` (an attacker's use of the EHR's public key)."""

    def part(value: Any) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    signing = f"{part(header)}.{part(claims)}"
    mac = hmac.new(secret, signing.encode(), hashlib.sha384).digest()
    return f"{signing}.{base64.urlsafe_b64encode(mac).rstrip(b'=').decode()}"


def good_claims() -> dict[str, Any]:
    now = int(time.time())
    return {"iss": EHR, "aud": f"{PUBLIC_URL}{DISCOVERY}", "iat": now, "exp": now + 120}


def test_a_header_outside_alg_kid_typ_jku_is_refused(
    guard: Guard, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only `alg`, `kid`, `typ` (`JWT`) and `jku`: a header naming a key or certificate (`x5u`,
    `x5c`, `jwk`), `crit`, `b64` or `cty`, a `typ` missing or other, and a `kid` missing or not a
    string are refused before any key is looked at."""
    guard.accepts(token())
    capsys.readouterr()
    claims = {**good_claims(), "jti": "x"}
    header = {"alg": "RS384", "typ": "JWT", "kid": "ehr-rsa"}
    refused = [
        token(headers={"x5u": f"{EHR}/cert.pem"}),
        token(headers={"x5c": ["MIIB"]}),
        token(STRANGER, headers={"jwk": STRANGER.jwk}),
        token(headers={"crit": ["exp"]}),
        forged({**header, "b64": False}, claims),
        token(headers={"cty": "JWT"}),
        token(headers={"typ": None}),
        token(headers={"typ": "at+jwt"}),
        token(headers={"typ": "jwt"}),
        forged({"alg": "RS384", "typ": "JWT"}, claims),
        forged({**header, "kid": 1}, claims),
        forged({**header, "kid": ""}, claims),
    ]
    for sent in refused:
        assert guard.ask(sent) == 401, sent.split(".")[0]
    assert reasons(capsys.readouterr().out) == ["cds_header"] * len(refused)
    assert guard.claims() == 1


def test_none_hs384_and_rs256_are_refused(guard: Guard, capsys: pytest.CaptureFixture[str]) -> None:
    """Only RS384 and ES384: `none` (unsigned), HS384 MACed with the EHR's own public key (the
    algorithm confusion attack), RS256, PS384 and ES256 with the EHR's keys, an `alg` given as a
    list, and an `alg` that names the other key type than its `kid`'s key."""
    guard.accepts(token())
    capsys.readouterr()
    claims = {**good_claims(), "jti": uuid.uuid4().hex}
    public_pem = RSA_EHR.private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    p256 = ec.generate_private_key(ec.SECP256R1())
    refused = [
        (forged({"alg": "none", "typ": "JWT", "kid": "ehr-rsa"}, claims, b""), "cds_header"),
        (hs384({"alg": "HS384", "typ": "JWT", "kid": "ehr-rsa"}, claims, public_pem), "cds_header"),
        (token(algorithm="RS256"), "cds_header"),
        (token(algorithm="PS384"), "cds_header"),
        (token(Signer("ES384", "ehr-ec", p256), algorithm="ES256"), "cds_header"),
        (forged({"alg": ["RS384"], "typ": "JWT", "kid": "ehr-rsa"}, claims), "cds_header"),
        (token(headers={"kid": "ehr-ec"}), "cds_key"),  # RS384 under the EC key's kid
        (token(headers={"kid": "unknown"}), "cds_key"),
        (forged({"alg": "RS384", "typ": "JWT", "kid": "ehr-rsa"}, claims), "cds_signature"),
    ]
    for sent, _ in refused:
        assert guard.ask(sent) == 401
    assert reasons(capsys.readouterr().out) == [reason for _, reason in refused]
    assert guard.claims() == 1


def test_an_unregistered_jku_is_refused(
    db_target: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A client registered by `jwks_uri` takes a `jku` only when it is that URL exactly (not
    another host's, nor the same URL in another case or with a slash), and its keys are never
    fetched for one that is not; a client whose keys are given by value takes no `jku`."""
    fetched = jwks_server(monkeypatch, {"keys": [RSA_EHR.jwk]})
    by_uri = entry("by-uri", jwks=DROP, jwks_uri=JWKS_URI)
    by_value = entry(
        "by-value", iss="https://other-ehr.example.org", jwks={"keys": [OTHER_EHR.jwk]}
    )
    register(monkeypatch, by_uri, by_value)
    with AuditStore(db_target) as store:
        guard = guard_for(store)
        capsys.readouterr()
        refused = [
            token(jku="https://evil.example/jwks.json"),
            token(jku=JWKS_URI.upper()),
            token(jku=f"{JWKS_URI}/"),
            token(jku=f"{JWKS_URI}?x=1"),
            token(headers={"jku": None}),
            token(OTHER_EHR, iss="https://other-ehr.example.org", jku=JWKS_URI),
        ]
        for sent in refused:
            assert guard.ask(sent) == 401
        assert reasons(capsys.readouterr().out) == ["cds_jku"] * len(refused)
        assert fetched == [] and guard.claims() == 0
        assert guard.accepts(token(jku=JWKS_URI)).id == "cds:by-uri"
        assert guard.accepts(token()).id == "cds:by-uri"  # jku is optional (CDS Hooks 2.0)
        assert fetched == [JWKS_URI]  # kept for the second
        theirs = token(OTHER_EHR, iss="https://other-ehr.example.org")
        assert guard.accepts(theirs).id == "cds:by-value"


# -- keys from a jwks_uri ----------------------------------------------------------------------


def jwks_server(monkeypatch: pytest.MonkeyPatch, *answers: Any) -> list[str]:
    """The EHR's JWK Set endpoint, answering `answers` in turn (the last one again after them):
    a JSON body, or an `httpx.Response`. Returns the URLs fetched, in order."""
    fetched: list[str] = []
    queue = list(answers)

    def answer(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        body = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, json=body, headers={"content-type": "application/json"})

    transport = httpx.MockTransport(answer)
    monkeypatch.setattr(
        cds_module, "GuardedHttp", lambda **kw: GuardedHttp(transport=transport, **kw)
    )
    return fetched


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_an_unknown_kid_refetches_at_most_once_a_minute(
    db_target: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `kid` the kept keys do not hold fetches them again, but at most once per client a
    minute, however many such tokens come: a flood of made-up `kid`s costs the EHR one request a
    minute. A key the EHR rotates in is found at the next fetch; kept keys are fetched again
    after an hour."""
    rotated = Signer("ES384", "ehr-ec-2", ec.generate_private_key(ec.SECP384R1()))
    fetched = jwks_server(
        monkeypatch, {"keys": [RSA_EHR.jwk]}, {"keys": [RSA_EHR.jwk]}, {"keys": [rotated.jwk]}
    )
    register(monkeypatch, entry(jwks=DROP, jwks_uri=JWKS_URI))
    clock = Clock()
    with AuditStore(db_target) as store:
        guard = guard_for(store, keys=JwksCache(allowed={EHR}, clock=clock))
        assert guard.accepts(token()).id == "cds:epic"
        assert len(fetched) == 1
        capsys.readouterr()
        for number in range(100):  # within the minute: no fetch
            assert guard.ask(token(headers={"kid": f"made-up-{number}"})) == 401
        assert len(fetched) == 1
        assert set(reasons(capsys.readouterr().out)) == {"cds_key"}
        clock.now += 61
        assert guard.ask(token(headers={"kid": "made-up"})) == 401
        assert len(fetched) == 2  # one fetch, a minute on
        for _ in range(50):
            assert guard.ask(token(headers={"kid": "made-up"})) == 401
        assert len(fetched) == 2
        assert guard.accepts(token()).id == "cds:epic"  # the kept key still works
        assert guard.ask(token(rotated)) == 401  # rotated in at the EHR, not fetched yet
        clock.now += 61
        assert guard.accepts(token(rotated)).id == "cds:epic"
        assert len(fetched) == 3
        assert guard.ask(token()) == 401  # the EHR no longer publishes it
        clock.now += 3600  # stale: fetched again on the next token
        assert guard.accepts(token(rotated)).id == "cds:epic"
        assert len(fetched) == 4 and set(fetched) == {JWKS_URI}


def test_a_jwks_answer_with_a_private_key_is_refused_and_nothing_is_cached(
    db_target: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An answer that is not a set of public signing keys (a private member, a symmetric key,
    an RSA key under 2048 bits) is refused, keeps nothing and drops what was kept, and its
    log line holds no key material; an unreachable or failed answer keeps what was kept."""
    private = {**RSA_EHR.jwk, "d": "c2VjcmV0LWV4cG9uZW50"}
    weak = Signer("RS384", "weak", rsa.generate_private_key(public_exponent=65537, key_size=1024))
    fetched = jwks_server(
        monkeypatch,
        {"keys": [private]},
        {"keys": [RSA_EHR.jwk]},
        httpx.Response(500, json={"error": "down"}, headers={"content-type": "application/json"}),
        {"keys": [{"kty": "oct", "kid": "ehr-rsa", "k": "c2VjcmV0"}]},
        {"keys": [weak.jwk]},
    )
    register(monkeypatch, entry(jwks=DROP, jwks_uri=JWKS_URI))
    clock = Clock()
    with AuditStore(db_target) as store:
        guard = guard_for(store, keys=JwksCache(allowed={EHR}, clock=clock))
        assert guard.ask(token()) == 401  # a private member: refused, nothing kept
        clock.now += 30
        assert guard.ask(token()) == 401  # nothing kept, and no fetch within the minute
        assert len(fetched) == 1
        clock.now += 31
        assert guard.accepts(token()).id == "cds:epic"  # the next answer is public keys
        clock.now += 61
        assert guard.ask(token(headers={"kid": "unknown"})) == 401  # a 500: what was kept holds
        assert guard.accepts(token()).id == "cds:epic"
        for _ in range(2):  # a symmetric key, then a weak one: each drops what was kept
            clock.now += 61
            assert guard.ask(token(headers={"kid": "unknown"})) == 401
            assert guard.ask(token()) == 401
        assert len(fetched) == 5
        out = capsys.readouterr().out
    jwks_lines = [line for line in security_lines(out) if line["event"] == "cds.jwks.refused"]
    assert [line["reason"] for line in jwks_lines] == [
        "invalid_keys", "unreachable", "invalid_keys", "invalid_keys",
    ]  # fmt: skip
    assert {line["actor"] for line in jwks_lines} == {"cds:epic"}
    assert reasons(out).count("cds_keys_unavailable") == 6
    for secret in ("c2VjcmV0LWV4cG9uZW50", "c2VjcmV0", RSA_EHR.jwk["n"], weak.jwk["n"]):
        assert secret not in out


def test_two_requests_share_one_fetch(db_target: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A request that finds another's fetch under way waits for it rather than fetching again:
    two threads at a cold start make one request to the EHR, and both are accepted."""
    started, release = threading.Event(), threading.Event()
    fetched: list[str] = []

    def slow(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        started.set()
        release.wait(5)
        body = {"keys": [RSA_EHR.jwk]}
        return httpx.Response(200, json=body, headers={"content-type": "application/json"})

    transport = httpx.MockTransport(slow)
    monkeypatch.setattr(
        cds_module, "GuardedHttp", lambda **kw: GuardedHttp(transport=transport, **kw)
    )
    register(monkeypatch, entry(jwks=DROP, jwks_uri=JWKS_URI))
    with AuditStore(db_target) as store:
        guard = guard_for(store)
        answers: list[int] = []
        first = threading.Thread(target=lambda: answers.append(guard.ask(token())))
        first.start()
        assert started.wait(5)
        second = threading.Thread(target=lambda: answers.append(guard.ask(token())))
        second.start()
        time.sleep(0.2)
        release.set()
        first.join(10)
        second.join(10)
    assert answers == [200, 200] and fetched == [JWKS_URI]


# -- what is written ----------------------------------------------------------------------------


def test_a_refused_token_claims_no_jti(guard: Guard, capsys: pytest.CaptureFixture[str]) -> None:
    """Every refusal comes before the `jti` is claimed, the one write a token may cause: none of
    them writes, and a signed token refused for its audience leaves its `jti` free."""
    now = int(time.time())
    claims = {**good_claims(), "jti": "kept"}
    refused = [
        "not-a-jwt.",
        "a.b",
        "a.b.c.d",
        "bm90.anNvbg.c2ln",
        "e30.e30.c2ln",
        "e30=.e30.c2ln",
        token(pad="x" * 9000),
        forged({"alg": "none", "kid": "ehr-rsa", "typ": "JWT"}, claims, b""),
        forged({"alg": "RS384", "kid": "ehr-rsa", "typ": "JWT"}, claims),
        token(iss="https://evil.example", jti="kept"),
        token(STRANGER, jti="kept"),
        token(path=HOOK, jti="kept"),
        token(lifetime=-120, jti="kept"),
        token(iat=now + 600, jti="kept"),
        token(lifetime=3600, jti="kept"),
        token(sub="x", tenant="t", jti="kept"),
        token(jti="j" * 257),
    ]
    for sent in refused:
        assert guard.ask(sent) == 401, sent[:40]
    out = capsys.readouterr().out
    assert len(reasons(out)) == len(refused)
    assert set(reasons(out)) <= {f"cds_{reason}" for reason in REASONS}
    assert guard.claims() == 0
    assert guard.accepts(token(jti="kept")).id == "cds:epic"
    assert guard.ask(token(jti="kept")) == 401
    assert reasons(capsys.readouterr().out) == ["cds_replayed"]
    assert guard.claims() == 1


def test_expired_claims_are_purged_in_bounded_batches(store: AuditStore) -> None:
    """A claim deletes at most `SESSION_PURGE_BATCH` expired rows of the session table, as a put
    does, so a CDS client's claims (one per call) do not pile up; live rows stay."""
    now = int(time.time())
    with store._db.session(exclusive=True) as db:
        for number in range(SESSION_PURGE_BATCH * 2 + 50):
            db.all(
                "INSERT INTO smart_sessions (key_sha256, kind, expires_at, data) "
                "VALUES (?, ?, ?, '{}')",
                (f"expired-{number}", "cds-jti", now - 60 - number),
            )
    assert store.smart_claim("jti", "client\nlive", now + 300)  # a live claim of another kind

    def rows(where: str) -> int:
        with store._db.session(exclusive=False) as db:
            row = db.one(f"SELECT COUNT(*) AS n FROM smart_sessions WHERE {where}")
        assert row is not None
        return int(row["n"])

    assert rows(f"expires_at <= {now}") == SESSION_PURGE_BATCH + 50
    assert store.smart_claim("cds-jti", "epic\na", now + 300)
    assert rows(f"expires_at <= {now}") == 50
    assert store.smart_claim("cds-jti", "epic\nb", now + 300)
    assert rows(f"expires_at <= {now}") == 0
    assert rows(f"expires_at > {now}") == 3
    assert not store.smart_claim("cds-jti", "epic\na", now + 300)  # a live claim holds


# -- the registry --------------------------------------------------------------------------------

PRIVATE = {**RSA_EHR.jwk, "d": "cHJpdmF0ZS1leHBvbmVudC1tYXRlcmlhbA"}
SMALL = Signer("RS384", "small", rsa.generate_private_key(public_exponent=65537, key_size=1024))
P256 = Signer("ES384", "p256", ec.generate_private_key(ec.SECP256R1()))
# (what MENDARI_CDS_CLIENTS holds, the message's words): each refused at startup.
REFUSED_REGISTRIES: list[tuple[Any, str]] = [
    ("not json", "is not JSON"),
    ({"id": "epic"}, "a JSON list of 1 to 50 clients"),
    ([], "a JSON list of 1 to 50 clients"),
    ([entry(f"e{n}", iss=f"{EHR}/{n}") for n in range(51)], "a JSON list of 1 to 50 clients"),
    (["epic"], "entry 1 is not an object"),
    ([entry(key="value")], "entry 1: a field is not one of"),
    ([entry(id="Epic")], "entry 1: id must be"),
    ([entry(id=DROP)], "entry 1: id must be"),
    ([entry(title=DROP)], "entry 1: title must be"),
    ([entry(title="a\nb")], "entry 1: title must be"),
    ([entry(iss=DROP)], "entry 1: iss is required"),
    ([entry(iss="has space")], "entry 1: iss must be"),
    ([entry(org=DROP)], "entry 1: org is required"),
    ([entry(fhir_servers=DROP)], "entry 1: fhir_servers must be a list of 1 to 20"),
    ([entry(fhir_servers=[])], "entry 1: fhir_servers must be a list of 1 to 20"),
    ([entry(fhir_servers=[f"{BASE}?a=1"])], "entry 1: fhir_servers 1 is not a FHIR base"),
    ([entry(fhir_servers=["https://other.example/fhir"])], "fhir_servers 1 is not on MENDARI_F"),
    ([entry(fhir_servers=[BASE, f"{BASE}/"])], "entry 1: fhir_servers 2 is given twice"),
    ([entry(fhir_servers=["http://[::1"])], "entry 1: fhir_servers 1 is not a FHIR base"),
    ([entry(origins=["https://[::1"])], "entry 1: origins 1 is not an origin"),
    ([entry(origins=[f"{EHR}?a=1"])], "entry 1: origins 1 is not an origin"),
    ([entry(origins=["https://ehr.example.org/app"])], "entry 1: origins 1 is not an origin"),
    ([entry(origins=["null"])], "entry 1: origins 1 is not an origin"),
    ([entry(jwks_uri=JWKS_URI)], "entry 1: give its keys as jwks (by value) or as jwks_uri"),
    ([entry(jwks=DROP)], "entry 1: give its keys as jwks (by value) or as jwks_uri"),
    ([entry(jwks={"keys": [PRIVATE]})], "entry 1: jwks: key 1 is private or symmetric"),
    ([entry(jwks={"keys": [SMALL.jwk]})], "entry 1: jwks: key 1: an RSA key needs 2048"),
    ([entry(jwks={"keys": [P256.jwk]})], "entry 1: jwks: key 1: an EC key must be on P-384"),
    ([entry(jwks=DROP, jwks_uri="http://ehr.example.org/jwks")], "entry 1: jwks_uri must be h"),
    ([entry(jwks=DROP, jwks_uri=f"{JWKS_URI}#k")], "entry 1: jwks_uri must be https"),
    ([entry(jwks=DROP, jwks_uri="https://keys.example/j")], "jwks_uri's origin is not on MENDA"),
    ([entry(sub="a b")], "entry 1: sub must be"),
    ([entry(tenant=7)], "entry 1: tenant must be"),
    ([entry(iss_is_fhir_server="yes")], "entry 1: iss_is_fhir_server must be true or false"),
    ([entry(iss_is_fhir_server=True)], "entry 1: iss_is_fhir_server is set, but iss is not"),
    ([entry(), entry(iss=f"{EHR}/2")], "entry 2: its id is another entry's too"),
    ([entry(), entry("two")], "entry 2: its iss is another entry's too"),
]


@pytest.mark.parametrize(
    ("registry", "words"), REFUSED_REGISTRIES, ids=[w for _, w in REFUSED_REGISTRIES]
)
def test_the_registry_is_checked_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry: Any, words: str
) -> None:
    """Each refusal stops the server, naming the setting, the entry and the field, never a value
    or any key material."""
    register(monkeypatch)
    monkeypatch.setenv(
        "MENDARI_CDS_CLIENTS", registry if isinstance(registry, str) else json.dumps(registry)
    )
    with pytest.raises(MendariError) as refused:
        create_app(db_path=tmp_path / "refused.db")
    message = str(refused.value)
    assert message.startswith("MENDARI_CDS_CLIENTS") and words in message, message
    for secret in (PRIVATE["d"], RSA_EHR.jwk["n"], EC_EHR.jwk["x"], SMALL.jwk["n"], P256.jwk["x"]):
        assert secret not in message
    for value in ("Epic", "has space", "a b", "ehr.example.org/app", "keys.example", "?a=1"):
        assert value not in message


@pytest.mark.parametrize(
    ("env", "words"),
    [
        ({"MENDARI_PUBLIC_URL": None}, "MENDARI_CDS_CLIENTS needs MENDARI_PUBLIC_URL"),
        ({"MENDARI_PUBLIC_URL": "http://mendari.example.org"}, "must be an https URL"),
        ({"MENDARI_PUBLIC_URL": "https://mendari.example.org/?a=1"}, "must be an https URL"),
        ({"MENDARI_OIDC_ISSUER": None}, "entry 1: with sign-in off, org must be default"),
        (
            {"MENDARI_SMART_EHRS": f"{BASE}=default:decide"},
            "fhir_servers 1 is registered in MENDARI_SMART_EHRS to another organization",
        ),
    ],
)
def test_the_registry_needs_its_public_url_and_matching_organizations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: dict[str, str | None], words: str
) -> None:
    """A token's `aud` is the public URL, so a registry without one stops startup; with sign-in
    off every client is `default`'s; a base registered for EHR launches is the same
    organization's for its hooks."""
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)
    register(monkeypatch, entry(org="north"))
    for name, value in env.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    if "MENDARI_OIDC_ISSUER" in env:
        monkeypatch.delenv("MENDARI_OIDC_CLIENT_ID")
        monkeypatch.setenv("MENDARI_PHI_MODE", "off")
    with pytest.raises(MendariError, match=words.replace("(", r"\(")):
        create_app(db_path=tmp_path / "refused.db")


def test_a_valid_registry_is_read_as_given() -> None:
    """What startup keeps: bases normalized, origins as origins, keys by value with their public
    members only, the rest as given."""
    ehrs = {BASE: Registration(base=BASE, org="north", level="decide")}
    given = entry(
        org="north",
        fhir_servers=["HTTPS://EHR.example.org/fhir/"],
        origins=["https://EHR.example.org:443/"],
        jwks={"keys": [{**RSA_EHR.jwk, "ext": True}]},
        sub="client-1",
        tenant="t1",
        iss_is_fhir_server=False,
    )
    with pytest.MonkeyPatch.context() as patch:
        second = entry("two", iss="other", org="north", jwks=DROP, jwks_uri=JWKS_URI)
        register(patch, given, second)
        registry = cds_clients(signed_in=True, ehrs=ehrs)
    assert registry is not None and registry.public_url == PUBLIC_URL
    epic, two = registry.clients
    assert epic.fhir_servers == (BASE,) and epic.origins == (EHR,)
    assert epic.jwks is not None and set(epic.jwks[0]) == {"kty", "kid", "key_ops", "n", "e"}
    assert (epic.sub, epic.tenant, epic.iss_is_fhir_server) == ("client-1", "t1", False)
    assert (two.jwks, two.jwks_uri) == (None, JWKS_URI)
    assert registry.ids("north") == ["epic", "two"] and registry.ids("south") == []
    assert registry.ids(None) == ["epic", "two"]
    assert registry.audience(HOOK) == f"{PUBLIC_URL}{HOOK}"


def test_unset_means_no_cds_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset (or blank), there is no registry: in either mode a JWT on `/cds-services` is
    refused, a key or no credential works as before, and nothing reads keys."""
    monkeypatch.delenv("MENDARI_CDS_CLIENTS", raising=False)
    assert cds_clients(signed_in=True, ehrs={}) is None
    monkeypatch.setenv("MENDARI_CDS_CLIENTS", "  ")
    assert cds_clients(signed_in=False, ehrs={}) is None
    open_mode = TestClient(create_app(db_path=tmp_path / "open.db"))
    assert open_mode.get("/api/health").json()["cds_clients"] is None
    signed = {"authorization": f"Bearer {token()}"}
    assert open_mode.get(DISCOVERY).status_code == 200
    assert open_mode.get(DISCOVERY, headers={"authorization": "Bearer ohk_x"}).status_code == 200
    refused = open_mode.get(DISCOVERY, headers=signed)
    assert refused.status_code == 401 and refused.headers["www-authenticate"] == INVALID
    with AuditStore(tmp_path / "plain.db") as store:
        assert Auth(None, store).cds is None


# -- the closed set of reasons -------------------------------------------------------------------


def _literal_reasons(path: Path, call: str) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == call and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.add(first.value)
            elif isinstance(first, ast.JoinedStr):
                found.add(ast.unparse(first))
    return found


def test_each_refusal_reason_comes_from_a_closed_set() -> None:
    """Every reason the verifier and the key cache raise is one of `REASONS`, and the guard logs
    each one as `cds_<reason>`, beside its own four."""
    server = ROOT / "src" / "mendari" / "server"
    raised = _literal_reasons(server / "cds_clients.py", "CdsRefused")
    assert raised == set(REASONS)
    logged = _literal_reasons(server / "auth.py", "refused_credential")
    cds = {reason for reason in logged if reason.startswith(("cds_", "f'cds_"))}
    assert cds == {
        "cds_unregistered",
        "cds_headers",
        "cds_no_organization_record",
        "f'cds_{refused.reason}'",
    }


def test_a_fhir_server_is_one_the_client_registered() -> None:
    client = cds_module.CdsClient(
        id="epic", title="EHR", iss=BASE, org="default", fhir_servers=(BASE,), jwks=()
    )
    pinned = cds_module.CdsClient(
        id="pinned", title="EHR", iss=EHR, org="default", fhir_servers=(BASE,), jwks=(),
        iss_is_fhir_server=True,
    )  # fmt: skip
    for allowed in (None, "", BASE, f"{BASE}/", "HTTPS://EHR.EXAMPLE.ORG/fhir"):
        assert fhir_server_allowed(client, allowed), allowed
    for refused in (
        f"{BASE}/../admin",
        f"{EHR}/FHIR",
        f"{EHR}:443/fhir",
        "x",
        7,
        ["a"],
        "http://[",
    ):
        assert not fhir_server_allowed(client, refused), refused
    assert not fhir_server_allowed(pinned, BASE)  # its iss is the host, not this base


def test_the_principal_is_confined_to_cds_services(
    db_target: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only the guard's CDS branch, on `/cds-services`, makes a `cds` principal; should one reach
    any other path (say, through a future change to `authenticate`), the guard refuses it there
    (`cds_only`), and on `/cds-services` a route it holds no permission for."""
    register(monkeypatch)
    cds = Principal(kind="cds", name="CDS client epic", id="cds:epic", actor="cds epic")
    with AuditStore(db_target) as store:
        guard = guard_for(store)
        monkeypatch.setattr(Auth, "authenticate", lambda self, request: cds)
        for method, path in (("GET", "/api/packs"), ("GET", "/api/audit"), ("POST", "/api/x")):
            assert guard.ask(None, path, method) == 403
        assert guard.ask(token(path="/cds-services/x/y"), "/cds-services/x/y", "POST") == 403
    lines = security_lines(capsys.readouterr().out)
    refused = [line["reason"] for line in lines if line["event"] == "auth.permission.refused"]
    assert refused == ["cds_only"] * 3 + ["admin"]
    assert {line["actor"] for line in lines if line["event"] == "auth.permission.refused"} == {
        "cds:epic"
    }
    assert auth_module.CDS_PATH.match("/cds-services") and auth_module.CDS_PATH.match(HOOK)
    assert not auth_module.CDS_PATH.match("/cds-servicesx")


def test_the_registry_can_be_built_for_a_test() -> None:
    """A registry made in code (as the route matrix makes one) holds what it was given."""
    client = cds_module.CdsClient(
        id="epic", title="EHR", iss=EHR, org="default", fhir_servers=(BASE,),
        jwks=(RSA_EHR.jwk,),
    )  # fmt: skip
    registry = CdsRegistry(public_url=f"{PUBLIC_URL}/", clients=[client])
    assert registry.public_url == PUBLIC_URL and registry.client("epic") is client
    assert registry.by_iss == {EHR: client} and registry.client("other") is None
