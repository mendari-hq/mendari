# SPDX-License-Identifier: Apache-2.0
"""Memberships mode (`MENDARI_ACCOUNTS=memberships`, LP-12e, ADR-0008): a person's organization
and roles come from records, read on every request, never from claims; a sign-in without a
membership acts for no organization (never `default`) and is refused before every route that
reads or writes an organization's data; choosing an organization and asking to join are a
person's; and claims mode, the default, never reads or writes the records."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from starlette.routing import Route

from mendari import AuditStore, Decision, Pack
from mendari.core import tenancy
from mendari.errors import MendariError, NoOrganization
from mendari.server import auth as auth_module
from mendari.server import sessions
from mendari.server.app import create_app
from mendari.server.auth import (
    MEMBER,
    ROLES,
    Auth,
    AuthSettings,
    Principal,
    auth_settings,
    permission_for,
    person_id,
)
from mendari.standards.fhir_client import GuardedHttp
from mendari.standards.smart import Registration
from tests.conftest import launched, member, message, signed_in
from tests.test_auth import CLIENT, IDP, FakeIdp, attempt, sign_in, signing_in_with
from tests.test_security_log import logged

HTTPS = "https://mendari.test"
SESSION = sessions.cookie_name(sessions.SESSION, "mendari.test")
PUBLIC_HEALTH = {"status", "version", "sign_in"}


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp()


def memberships_app(db: str, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A server in memberships mode whose sign-in uses `idp`."""
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", CLIENT)
    transport = httpx.MockTransport(idp)
    monkeypatch.setattr(
        auth_module, "GuardedHttp", lambda **kw: GuardedHttp(transport=transport, **kw)
    )
    return TestClient(create_app(db_path=db), base_url=HTTPS)


@pytest.fixture
def members(db_target: str, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with memberships_app(db_target, idp, monkeypatch) as client:
        yield client


def me(client: TestClient, headers: dict[str, str] | None = None) -> dict[str, Any]:
    answer: dict[str, Any] = client.get("/api/auth/me", headers=headers).json()
    return answer


def events(store: AuditStore, *kinds: str) -> list[dict[str, Any]]:
    """The chained events of `kinds`, oldest first, with their payloads."""
    marks = ", ".join("?" for _ in kinds)
    with store._db.session(exclusive=False) as db:
        rows = db.all(
            f"SELECT event, payload FROM audit_events WHERE event IN ({marks}) ORDER BY seq",
            kinds,
        )
    return [{"event": r["event"], **json.loads(r["payload"])} for r in rows]


def recorded(store: AuditStore, sub: str) -> str:
    """A person who signed in once (at the fake identity provider), recorded: their id."""
    return str(store.record_user(issuer=IDP, subject=sub, email=None, name=sub)["id"])


def refused_for_no_organization(answer: Any) -> bool:  # TestClient's response
    return answer.status_code == 403 and answer.json().get("error") == "NoOrganization"


# -- the setting ------------------------------------------------------------------------------


def test_accounts_is_claims_or_memberships_and_memberships_needs_sign_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MENDARI_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("MENDARI_OIDC_CLIENT_ID", raising=False)
    assert auth_settings() is None  # open mode, as before
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    with pytest.raises(MendariError, match="needs sign-in"):
        auth_settings()
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", CLIENT)
    for value, mode in (("memberships", "memberships"), (" Memberships ", "memberships")):
        monkeypatch.setenv("MENDARI_ACCOUNTS", value)
        settings = auth_settings()
        assert settings is not None and settings.accounts == mode
    monkeypatch.setenv("MENDARI_ACCOUNTS", "records")
    with pytest.raises(MendariError, match="claims or memberships"):
        auth_settings()
    for value in ("claims", ""):
        monkeypatch.setenv("MENDARI_ACCOUNTS", value)
        settings = auth_settings()
        assert settings is not None and settings.accounts == "claims"


def test_claims_mode_never_reads_or_writes_the_records(
    tmp_path: Path, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance test: with `MENDARI_ACCOUNTS` unset a sign-in is today's, by claims, and
    the records are neither read (a waiting membership stays waiting) nor written (no person,
    nothing chained); the memberships routes are not there, and their rule reads as any
    unlisted route's: `admin`."""
    with signing_in_with(idp, tmp_path, monkeypatch) as client:
        store = AuditStore(tmp_path / "a.db")
        store.create_organization(org="clinic-a", name="Clinic A", created_by="t")
        store.add_membership(
            org="clinic-a", roles=["admin"], created_by="t", email="kim@example.org"
        )
        before = store.verify()["events"]
        sign_in(client, idp, roles=["viewer"], email="kim@example.org", email_verified=True)
        shown = me(client)
        assert (shown["principal"]["org"], shown["principal"]["roles"]) == ("default", ["viewer"])
        assert "accounts" not in shown and "memberships" not in shown
        assert store.user(person_id(IDP, "u1")) is None
        [waiting] = store.memberships(org="clinic-a")
        assert (waiting["user_id"], waiting["pending_email"]) == (None, "kim@example.org")
        assert store.verify()["events"] == before
        for path in ("/api/auth/organization", "/api/auth/access-requests"):
            refused = client.post(path, json={"org": "clinic-a"})
            assert refused.status_code == 403 and "may not admin" in refused.json()["detail"]
        sign_in(client, idp, roles=["admin"])
        for path in ("/api/auth/organization", "/api/auth/access-requests"):
            assert client.post(path, json={"org": "clinic-a"}).status_code == 404
        assert member(store, org="default", roles=["admin"]) != {}
        client.cookies.clear()  # a memberships session is no session here
        assert client.get("/api/audit", headers=member(store, org="default")).status_code == 401


def test_use_another_account_asks_the_identity_provider_which(
    members: TestClient,
) -> None:
    asked = members.get("/auth/login", params={"account": "choose"}, follow_redirects=False)
    assert parse_qs(urlsplit(asked.headers["location"]).query)["prompt"] == ["select_account"]
    for other in ("none", "login", ""):
        plain = members.get("/auth/login", params={"account": other}, follow_redirects=False)
        assert "prompt" not in parse_qs(urlsplit(plain.headers["location"]).query)


# -- signing in by records ----------------------------------------------------------------------


def test_the_owners_bootstrap_binds_at_sign_in_and_reads_earlier_decisions(
    members: TestClient, store: AuditStore, idp: FakeIdp, inbox_pack: Pack
) -> None:
    inputs = message("Can I get a refill?")
    store.record(Decision(inbox_pack)(inputs), inputs)
    store.bootstrap(email="owner@example.org", created_by="cli")
    signed = sign_in(members, idp, sub="owner", email="Owner@Example.org", email_verified=True)
    assert signed.status_code == 302
    shown = me(members)
    assert shown["accounts"] == "memberships"
    assert (shown["principal"]["org"], shown["principal"]["roles"]) == ("default", ["admin"])
    assert shown["memberships"] == [
        {"org": "default", "name": "The owner's organization", "roles": ["admin"]}
    ]
    assert len(members.get("/api/audit").json()) == 1  # the data `default` already held
    owner = person_id(IDP, "owner")
    assert [e["event"] for e in events(store, "user.recorded", "membership.bound")] == [
        "user.recorded",
        "membership.bound",
    ]
    user = store.user(owner)
    assert user is not None and user["email"] == "owner@example.org"
    assert store.verify()["ok"]


@pytest.mark.parametrize("verified", [False, "true", None])
def test_an_unverified_address_binds_nothing_and_records_no_one(
    members: TestClient, store: AuditStore, idp: FakeIdp, verified: Any
) -> None:
    store.bootstrap(email="owner@example.org", created_by="cli")
    claims: dict[str, Any] = {"sub": "owner", "email": "owner@example.org"}
    if verified is not None:
        claims["email_verified"] = verified
    assert sign_in(members, idp, **claims).status_code == 302
    assert me(members)["principal"]["org"] is None
    assert store.user(person_id(IDP, "owner")) is None
    [waiting] = store.memberships(org="default")
    assert waiting["user_id"] is None


def test_a_non_ascii_address_signs_in_with_no_organization(
    members: TestClient, store: AuditStore, idp: FakeIdp
) -> None:
    signed = sign_in(members, idp, sub="kim", email="kïm@example.org", email_verified=True)
    assert signed.status_code == 302 and me(members)["principal"]["org"] is None


def test_a_sign_in_without_a_membership_acts_for_no_organization_never_default(
    members: TestClient, store: AuditStore, idp: FakeIdp, inbox_pack: Pack
) -> None:
    inputs = message("Can I get a refill?")
    store.record(Decision(inbox_pack)(inputs), inputs)
    signed = sign_in(
        members, idp, sub="stranger", email="x@example.org", email_verified=True, roles=["admin"]
    )
    assert signed.status_code == 302  # never refused for having none
    shown = me(members)
    assert shown["signed_in"] is True and shown["memberships"] == []
    assert (shown["principal"]["org"], shown["principal"]["roles"]) == (None, [])
    assert refused_for_no_organization(members.get("/api/audit"))
    assert members.get("/", follow_redirects=False).status_code == 200  # the no-access page
    assert set(members.get("/api/health").json()) == PUBLIC_HEALTH
    assert store.user(person_id(IDP, "stranger")) is None  # anyone else leaves no record


def test_role_and_org_claims_are_ignored_in_memberships_mode(
    db_target: str, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_OIDC_ROLE_MAP", "clinicians=admin")
    monkeypatch.setenv("MENDARI_OIDC_ORG_CLAIM", "org")
    with memberships_app(db_target, idp, monkeypatch) as client:
        AuditStore(db_target).create_organization(org="clinic-a", name="A", created_by="t")
        sign_in(client, idp, roles=["clinicians"], org="clinic-a")
        principal = me(client)["principal"]
        assert (principal["org"], principal["roles"]) == (None, [])


def test_one_membership_is_chosen_at_sign_in_and_several_wait_for_a_choice(
    members: TestClient, store: AuditStore, idp: FakeIdp
) -> None:
    ada = recorded(store, "ada")
    for org, name in (("clinic-a", "Clinic A"), ("clinic-b", "Clinic B"), ("clinic-c", "C")):
        store.create_organization(org=org, name=name, created_by="t")
    store.add_membership(org="clinic-a", roles=["reviewer"], created_by="t", user_id=ada)
    sign_in(members, idp, sub="ada")
    principal = me(members)["principal"]
    assert (principal["org"], principal["roles"]) == ("clinic-a", ["reviewer"])
    store.add_membership(org="clinic-b", roles=["viewer"], created_by="t", user_id=ada)
    sign_in(members, idp, sub="ada")
    shown = me(members)
    assert shown["principal"]["org"] is None
    assert [m["org"] for m in shown["memberships"]] == ["clinic-a", "clinic-b"]
    chosen = members.post("/api/auth/organization", json={"org": "clinic-b"})
    assert chosen.status_code == 200
    assert chosen.json() == {"org": "clinic-b", "name": "Clinic B", "roles": ["viewer"]}
    principal = me(members)["principal"]
    assert (principal["org"], principal["roles"]) == ("clinic-b", ["viewer"])
    for org, status in (("clinic-c", 403), ("nowhere", 403), ("Clinic B", 422), ("x", 422)):
        assert members.post("/api/auth/organization", json={"org": org}).status_code == status
    assert me(members)["principal"]["org"] == "clinic-b"  # refused: the session as it was


def test_choosing_needs_a_person_from_this_site(members: TestClient, store: AuditStore) -> None:
    store.create_organization(org="clinic-a", name="Clinic A", created_by="t")
    key = store.create_api_key(name="k", role="service", org="clinic-a", created_by="t")["key"]
    as_key = members.post(
        "/api/auth/organization",
        json={"org": "clinic-a"},
        headers={"authorization": f"Bearer {key}"},
    )
    assert as_key.status_code == 403 and "only a person" in as_key.json()["detail"]
    anonymous = members.post("/api/auth/organization", json={"org": "clinic-a"})
    assert anonymous.status_code == 401
    headers = member(store, org="clinic-a", roles=["viewer"], sub="ada")
    elsewhere = members.post(
        "/api/auth/organization",
        json={"org": "clinic-a"},
        headers={**headers, "origin": "https://evil.example.org", "sec-fetch-site": "cross-site"},
    )
    assert elsewhere.status_code == 403
    assert (
        members.post(
            "/api/auth/organization", json={"org": "clinic-a"}, headers=headers
        ).status_code
        == 200
    )


def test_an_ehr_launch_is_no_person_here(members: TestClient, store: AuditStore) -> None:
    cookie = launched(store)["cookie"]
    asked = members.post(
        "/api/auth/organization", json={"org": "default"}, headers={"cookie": cookie}
    )
    assert asked.status_code == 401  # the launch cookie is a credential on /api/smart/ only


def test_choosing_never_extends_the_session(members: TestClient, store: AuditStore) -> None:
    ada = person_id(IDP, "ada")
    store.create_organization(org="clinic-b", name="Clinic B", created_by="t")
    headers = member(store, org="clinic-a", roles=["viewer"], sub="ada")
    store.add_membership(org="clinic-b", roles=["viewer"], created_by="t", user_id=ada)
    sid = headers["cookie"].partition("=")[2]
    before = store.smart_read(sid, MEMBER)
    assert before is not None
    chosen = members.post("/api/auth/organization", json={"org": "clinic-b"}, headers=headers)
    assert chosen.status_code == 200
    after = store.smart_read(sid, MEMBER)
    assert after is not None and after[0]["org"] == "clinic-b"
    assert after[0]["absolute_at"] == before[0]["absolute_at"]
    assert after[1] <= after[0]["absolute_at"]


def test_choosing_after_the_session_ended_is_refused(
    members: TestClient, store: AuditStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sign-out in another tab between this request's sign-in check and its write."""
    headers = member(store, org="clinic-a", roles=["viewer"], sub="ada")
    monkeypatch.setattr(AuditStore, "smart_replace", lambda *a, **k: False)
    ended = members.post("/api/auth/organization", json={"org": "clinic-a"}, headers=headers)
    assert ended.status_code == 401


# -- sessions ---------------------------------------------------------------------------------


def test_a_claims_session_is_no_session_in_memberships_mode(
    members: TestClient, store: AuditStore
) -> None:
    claims_session = signed_in(store, role="admin")
    assert members.get("/api/audit", headers=claims_session).status_code == 401
    assert me(members, claims_session)["signed_in"] is False


def test_a_removed_membership_ends_access_at_the_next_request(
    members: TestClient, store: AuditStore
) -> None:
    ada = person_id(IDP, "ada")
    headers = member(store, org="clinic-a", roles=["admin"], sub="ada")
    store.add_membership(
        org="clinic-a", roles=["admin"], created_by="t", user_id=recorded(store, "bo")
    )
    assert members.get("/api/audit", headers=headers).status_code == 200
    held = store.membership_in_force(ada, "clinic-a")
    assert held is not None
    store.set_membership_roles(held["id"], ["viewer"], org="clinic-a", changed_by="t")
    assert me(members, headers)["principal"]["roles"] == ["viewer"]  # at the next request
    assert members.get("/api/auth/keys", headers=headers).status_code == 403
    store.remove_membership(held["id"], org="clinic-a", removed_by="t")
    assert refused_for_no_organization(members.get("/api/audit", headers=headers))
    assert me(members, headers)["principal"]["org"] is None
    store.add_membership(org="clinic-a", roles=["admin"], created_by="t", user_id=ada)
    assert refused_for_no_organization(members.get("/api/audit", headers=headers))  # chosen anew
    chosen = members.post("/api/auth/organization", json={"org": "clinic-a"}, headers=headers)
    assert chosen.status_code == 200
    assert members.get("/api/audit", headers=headers).status_code == 200


def test_reserved_roles_grant_nothing(members: TestClient, store: AuditStore) -> None:
    headers = member(store, org="clinic-a", roles=["labeller", "pack_author"], sub="ada")
    principal = me(members, headers)["principal"]
    assert (principal["org"], principal["roles"], principal["permissions"]) == (
        "clinic-a",
        [],
        [],
    )
    assert members.get("/api/audit", headers=headers).status_code == 403


def test_the_session_row_is_sealed_and_holds_no_roles(
    members: TestClient, store: AuditStore, idp: FakeIdp
) -> None:
    store.create_organization(org="clinic-a", name="Clinic A", created_by="t")
    store.add_membership(
        org="clinic-a", roles=["admin"], created_by="t", user_id=recorded(store, "ada")
    )
    sign_in(members, idp, sub="ada", email="ada@example.org", email_verified=True)
    sid = members.cookies.get(SESSION)
    assert sid is not None
    with store._db.session(exclusive=False) as db:
        [raw] = db.all("SELECT data FROM smart_sessions WHERE kind = ?", (MEMBER,))
    assert "clinic-a" not in raw["data"] and "ada@example.org" not in raw["data"]
    row = store.smart_read(sid, MEMBER)
    assert row is not None
    assert {"roles", "claims"}.isdisjoint(row[0]) and row[0]["org"] == "clinic-a"


def test_signing_out_and_signing_in_again_end_the_browsers_session(
    members: TestClient, store: AuditStore, idp: FakeIdp
) -> None:
    sign_in(members, idp, sub="ada")
    first = members.cookies.get(SESSION)
    assert first is not None and store.smart_read(first, MEMBER) is not None
    started = members.get("/auth/login", follow_redirects=False)  # this browser, again
    code = idp.authorize(started.headers["location"], sub="bo")
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    members.get("/auth/callback", params={"code": code, "state": state}, follow_redirects=False)
    second = members.cookies.get(SESSION)
    assert second is not None and second != first
    assert store.smart_read(first, MEMBER) is None  # one browser, one person
    assert members.post("/auth/logout").status_code == 204
    assert store.smart_read(second, MEMBER) is None


# -- a principal of no organization ---------------------------------------------------------


NOBODY = Principal(
    kind="user",
    name="Nobody",
    roles=frozenset({"admin"}),  # every role, and still nothing: the organization decides
    org=None,
    id="user:0000000000000000",
    actor="nobody@example.org",
)


@pytest.fixture
def as_nobody(db_target: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Memberships mode with Backend Services on (the most routes), every request from a person
    of no organization who holds every role."""
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", CLIENT)
    monkeypatch.setenv("MENDARI_PUBLIC_URL", HTTPS)
    monkeypatch.setenv("MENDARI_BACKEND_SERVICES", "on")
    app = create_app(db_path=db_target)
    monkeypatch.setattr(Auth, "authenticate", lambda self, request: NOBODY)
    with TestClient(app, base_url=HTTPS) as client:
        yield client


ORG_ROUTES = [
    ("GET", "/api/holds", None),
    ("POST", "/api/holds", {"reason": "drill"}),
    ("DELETE", "/api/holds/h1", None),
    ("POST", "/api/auth/keys", {"name": "k", "role": "viewer"}),
    ("GET", "/api/auth/keys", None),
    ("POST", "/api/auth/keys/k1/rotate", None),
    ("DELETE", "/api/auth/keys/k1", None),
    ("POST", "/api/auth/clients", {"name": "c", "role": "viewer", "jwks": {"keys": []}}),
    ("GET", "/api/auth/clients", None),
    ("DELETE", "/api/auth/clients/c1", None),
    ("GET", "/api/audit/verify", None),
    ("GET", "/api/audit/export/fhir", None),
]


def test_a_principal_of_no_organization_is_refused_before_every_route_that_takes_one(
    as_nobody: TestClient, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The routes that give the store an organization themselves, and the whole chain, read or
    streamed: refused before the route runs (a store call here fails the test), so nothing is
    read for every organization, written for none, or streamed and then cut off."""

    def reached(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a route of no organization reached the store")

    for name in (
        "holds", "hold_automation", "lift_hold", "create_api_key", "api_keys",
        "rotate_api_key", "revoke_api_key", "register_client", "clients", "revoke_client",
        "verify", "iter_events", "org_usage",
    ):  # fmt: skip
        monkeypatch.setattr(AuditStore, name, reached)
    capsys.readouterr()
    for method, path, body in ORG_ROUTES:
        answer = as_nobody.request(method, path, json=body)
        assert refused_for_no_organization(answer), (method, path, answer.status_code)
    lines = logged(capsys)
    assert not [line for line in lines if line["event"] == "export.audit"]
    refusals = [line for line in lines if line["event"] == "auth.permission.refused"]
    assert {line["reason"] for line in refusals} == {"no_organization"}
    accessed = [line for line in lines if line["event"] == "request"]
    assert accessed and {line.get("org") for line in accessed} == {None}


def test_no_route_serves_a_principal_of_no_organization(
    as_nobody: TestClient, store: AuditStore
) -> None:
    """Every route the server mounts, but the public ones, the pages and a person's own two:
    refused (`403`), as an OperationOutcome on FHIR routes, and nothing chained."""
    before = store.verify()["events"]
    served = sorted(
        (method, route.path)
        for route in as_nobody.app.routes  # type: ignore[attr-defined]
        if isinstance(route, Route)
        for method in route.methods or ()
        if method != "HEAD"
    )
    checked = 0
    for method, path in served:
        if permission_for(method, path) in ("public", "page", "person"):
            continue
        concrete = re.sub(r"\{[^}]+\}", "x1", path)
        answer = as_nobody.request(method, concrete, json={} if method != "GET" else None)
        assert answer.status_code == 403, (method, path, answer.status_code)
        body = answer.json()
        fhir = concrete.startswith("/fhir/") or "/fhir/" in concrete
        assert (
            body.get("resourceType") == "OperationOutcome"
            if fhir
            else (body.get("error") == "NoOrganization")
        ), (method, path, body)
        checked += 1
    assert checked > 40
    assert store.verify()["events"] == before


def test_health_tells_a_person_of_no_organization_only_the_public_fields(
    members: TestClient, store: AuditStore
) -> None:
    headers = member(store, org=None, sub="ada")
    assert set(members.get("/api/health", headers=headers).json()) == PUBLIC_HEALTH
    assert len(
        members.get(
            "/api/health", headers=member(store, org="clinic-a", roles=["viewer"], sub="bo")
        ).json()
    ) > len(PUBLIC_HEALTH)


def test_a_person_of_no_organization_acts_for_nobody(
    members: TestClient, store: AuditStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On their own routes too: any organization data reached through tenancy is refused."""
    store.create_organization(org="clinic-a", name="Clinic A", created_by="t")
    headers = member(store, org=None, sub="ada")
    seen: list[str] = []
    in_force = AuditStore.membership_in_force

    def spy(self: AuditStore, user_id: str, org: str) -> dict[str, Any] | None:
        try:
            seen.append(str(tenancy.current()))
        except NoOrganization:
            seen.append("nobody")
        return in_force(self, user_id, org)

    monkeypatch.setattr(AuditStore, "membership_in_force", spy)
    members.post("/api/auth/organization", json={"org": "clinic-a"}, headers=headers)
    assert seen == ["nobody"]


# -- asking to join, and what `me` shows -------------------------------------------------------


def test_asking_to_join_is_recorded_once_and_chained(
    members: TestClient, store: AuditStore, idp: FakeIdp
) -> None:
    store.create_organization(org="clinic-a", name="Clinic A", created_by="t")
    sign_in(members, idp, sub="ada", email="ada@example.org", email_verified=True, name="Ada")
    ada = person_id(IDP, "ada")
    assert store.user(ada) is None
    for _ in range(2):
        asked = members.post("/api/auth/access-requests", json={"org": "clinic-a"})
        assert (asked.status_code, asked.json()) == (202, {"org": "clinic-a", "requested": True})
    [request] = store.access_requests("clinic-a")
    assert request["user_id"] == ada
    user = store.user(ada)
    assert user is not None and (user["email"], user["name"]) == ("ada@example.org", "Ada")
    assert [e["org"] for e in events(store, "access.requested")] == ["clinic-a"]
    assert store.verify()["ok"]


def test_asking_answers_the_same_and_records_nothing_when_it_may_not_be_asked(
    members: TestClient, store: AuditStore, idp: FakeIdp
) -> None:
    """An organization with no record, one already joined, past five open requests: the same
    `202`, nothing recorded, nothing chained, no person recorded for it."""
    sign_in(members, idp, sub="stranger")
    before = store.verify()["events"]
    asked = members.post("/api/auth/access-requests", json={"org": "clinic-z"})
    assert (asked.status_code, asked.json()) == (202, {"org": "clinic-z", "requested": True})
    assert store.access_requests() == [] and store.verify()["events"] == before
    assert store.user(person_id(IDP, "stranger")) is None
    headers = member(store, org="clinic-a", roles=["viewer"], sub="ada")
    assert (
        members.post(
            "/api/auth/access-requests", json={"org": "clinic-a"}, headers=headers
        ).status_code
        == 202
    )
    assert store.access_requests() == []
    orgs = [f"clinic-{n}" for n in range(6)]
    for org in orgs:
        store.create_organization(org=org, name=org, created_by="t")
        assert (
            members.post(
                "/api/auth/access-requests", json={"org": org}, headers=headers
            ).status_code
            == 202
        )
    assert len(store.access_requests()) == 5  # at most five open at once


def test_me_lists_only_ones_own_memberships(members: TestClient, store: AuditStore) -> None:
    ada = member(store, org="clinic-a", roles=["viewer"], sub="ada")
    member(store, org="clinic-b", roles=["admin"], sub="bo")
    assert [m["org"] for m in me(members, ada)["memberships"]] == ["clinic-a"]
    key = store.create_api_key(name="k", role="viewer", org="clinic-a", created_by="t")["key"]
    as_key = me(members, {"authorization": f"Bearer {key}"})
    assert as_key["accounts"] == "memberships" and "memberships" not in as_key


def test_a_window_on_another_organization_than_the_one_chosen_is_refused(
    members: TestClient, store: AuditStore
) -> None:
    headers = member(store, org="clinic-a", roles=["viewer"], sub="ada")
    assert members.get("/api/audit", headers=headers).status_code == 200
    assert (
        members.get("/api/audit", headers={**headers, "x-mendari-org": "clinic-a"}).status_code
        == 200
    )
    moved = members.get("/api/audit", headers={**headers, "x-mendari-org": "clinic-b"})
    assert moved.status_code == 409 and moved.json()["error"] == "OrganizationChanged"


# -- credentials of an organization with no record ----------------------------------------------


def test_a_key_of_an_organization_with_no_record_is_refused(
    members: TestClient, store: AuditStore
) -> None:
    key = store.create_api_key(name="k", role="viewer", org="clinic-z", created_by="t")["key"]
    bearer = {"authorization": f"Bearer {key}"}
    assert members.get("/api/audit", headers=bearer).status_code == 401
    store.create_organization(org="clinic-z", name="Clinic Z", created_by="t")
    assert members.get("/api/audit", headers=bearer).status_code == 200


def test_a_launch_of_an_organization_with_no_record_is_refused(tmp_path: Path) -> None:
    settings = AuthSettings(IDP, CLIENT, None, "roles", {}, None, None, accounts="memberships")
    base = "https://ehr.example.org/fhir"
    store = AuditStore(tmp_path / "a.db")
    ehrs = {base: Registration(base=base, org="clinic-e", level="decide")}
    cookie = launched(store, iss=base)["cookie"]

    def launch_request() -> Request:  # a new request each time: a request keeps its launch
        return Request(
            {
                "type": "http",
                "scheme": "https",
                "server": ("mendari.test", 443),
                "method": "GET",
                "path": "/api/smart/patient",
                "headers": [(b"cookie", cookie.encode())],
            }
        )

    assert Auth(settings, store, ehrs=ehrs).authenticate(launch_request()) is None
    store.create_organization(org="clinic-e", name="Clinic E", created_by="t")
    principal = Auth(settings, store, ehrs=ehrs).authenticate(launch_request())
    assert principal is not None and (principal.kind, principal.org) == ("ehr", "clinic-e")


def test_a_member_is_let_through_exactly_as_a_claims_person_of_the_same_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Over every route the server mounts (Backend Services on): a person in memberships mode
    holding a role in `default` gets the guard's answer a person signed in by claims with that
    role gets. Only a person's own two routes differ: any person's in memberships mode (with an
    organization or none; never a key's, never anonymous), an administrator's in claims mode."""
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", CLIENT)
    monkeypatch.setenv("MENDARI_PUBLIC_URL", HTTPS)
    monkeypatch.setenv("MENDARI_BACKEND_SERVICES", "on")
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    app = create_app(db_path=tmp_path / "routes.db")
    routes = sorted(
        (method, route.path)
        for route in app.routes
        if isinstance(route, Route)
        for method in route.methods or ()
    )
    store = AuditStore(tmp_path / "matrix.db")
    by_claims = Auth(AuthSettings(IDP, CLIENT, None, "roles", {}, None, None), store)
    settings = AuthSettings(IDP, CLIENT, None, "roles", {}, None, None, accounts="memberships")
    by_records = Auth(settings, store)
    own = {r for r in routes if permission_for(*r) == "person"}
    assert own == {("POST", "/api/auth/organization"), ("POST", "/api/auth/access-requests")}
    differ = []
    for role in ROLES:
        as_member = member(store, org="default", roles=[role], sub=f"member-{role}")
        as_person = signed_in(store, role=role)
        for method, path in routes:
            if (method, path) in own:
                assert attempt(by_records, method, path, as_member)[0] == 200
                claims_answer = attempt(by_claims, method, path, as_person)[0]
                assert claims_answer == (200 if role == "admin" else 403)
                continue
            if attempt(by_records, method, path, as_member) != attempt(
                by_claims, method, path, as_person
            ):
                differ.append((role, method, path))
    assert differ == []
    nobody = member(store, org=None, sub="nobody")
    key = store.create_api_key(name="k", role="service", org="default", created_by="t")["key"]
    for method, path in own:
        assert attempt(by_records, method, path, nobody)[0] == 200
        assert attempt(by_records, method, path, {})[0] == 401
        assert attempt(by_records, method, path, {"authorization": f"Bearer {key}"})[0] == 403
