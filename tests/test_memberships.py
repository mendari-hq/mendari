# SPDX-License-Identifier: Apache-2.0
"""Memberships mode (`MENDARI_ACCOUNTS=memberships`, LP-12e and LP-12f, ADR-0008): a person's
organization and roles come from records, read on every request, never from claims; a sign-in
without a membership acts for no organization (never `default`) and is refused before every
route that reads or writes an organization's data; choosing an organization, asking to join and
accepting an invitation are a person's; an organization's administrators, people only, manage
its invitations, members and requests to join, in their own organization only; and claims mode,
the default, never reads or writes the records."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
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
from mendari.errors import AuthError, MendariError, NoOrganization
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
from tests.test_auth import (
    CLIENT,
    IDP,
    FakeIdp,
    attempt,
    legacy_key,
    sign_in,
    signing_in_with,
)
from tests.test_security_log import logged

HTTPS = "https://mendari.test"
SESSION = sessions.cookie_name(sessions.SESSION, "mendari.test")
PUBLIC_HEALTH = {"status", "version", "sign_in"}
ACCEPT = "/api/auth/invitations/accept"
# A person's own routes, with an organization or none (`person`).
OWN_ROUTES = ("/api/auth/organization", "/api/auth/access-requests", ACCEPT)
# What an organization's administrators read of its people: addresses, for `keys` only.
PEOPLE_LISTS = ("/api/auth/members", "/api/auth/invitations", "/api/auth/access-requests")


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
        for path in OWN_ROUTES:
            refused = client.post(path, json={"org": "clinic-a"})
            assert refused.status_code == 403 and "may not admin" in refused.json()["detail"]
        for path in PEOPLE_LISTS:  # not there either: read as any route no rule lists
            refused = client.get(path)
            assert refused.status_code == 403 and "may not admin" in refused.json()["detail"]
        sign_in(client, idp, roles=["admin"])
        for path in OWN_ROUTES:
            assert client.post(path, json={"org": "clinic-a"}).status_code == 404
        for path in PEOPLE_LISTS:
            assert client.get(path).status_code == 404
        assert store.verify()["events"] == before
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


def test_an_ehr_launch_is_no_person_here(
    db_target: str, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the SMART launch on and the EHR registered to an organization with a record: the
    launch opens the dashboard page, and is no credential for choosing an organization."""
    ehr = "https://ehr.example.org"
    monkeypatch.setenv("MENDARI_SMART_CLIENT_ID", "mendari-smart")
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", ehr)
    monkeypatch.setenv("MENDARI_SMART_EHRS", f"{ehr}/fhir=clinic-e:view")
    store = AuditStore(db_target)
    store.create_organization(org="clinic-e", name="Clinic E", created_by="t")
    cookie = {"cookie": launched(store)["cookie"]}
    with memberships_app(db_target, idp, monkeypatch) as client:
        assert client.get("/", headers=cookie, follow_redirects=False).status_code == 200
        asked = client.post("/api/auth/organization", json={"org": "clinic-e"}, headers=cookie)
        assert asked.status_code == 401  # the launch cookie is a credential on /api/smart/


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


def test_choosing_near_the_absolute_limit_ends_the_session_there(
    members: TestClient, store: AuditStore
) -> None:
    headers = member(store, org="clinic-a", roles=["viewer"], sub="ada")
    sid = headers["cookie"].partition("=")[2]
    row = store.smart_read(sid, MEMBER)
    assert row is not None
    soon = int(time.time()) + 60
    assert store.smart_replace(sid, MEMBER, {**row[0], "absolute_at": soon}, soon)
    chosen = members.post("/api/auth/organization", json={"org": "clinic-a"}, headers=headers)
    assert chosen.status_code == 200
    after = store.smart_read(sid, MEMBER)
    assert after is not None and after[1] == soon  # never now + idle, past the limit


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


ORG_ROUTES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "/api/holds", None),
    ("POST", "/api/holds", {"reason": "drill"}),
    ("DELETE", "/api/holds/h1", None),
    ("POST", "/api/auth/keys", {"name": "k", "role": "viewer"}),
    ("GET", "/api/auth/keys", None),
    ("POST", "/api/auth/keys/k1/rotate", None),
    ("DELETE", "/api/auth/keys/k1", None),
    ("GET", "/api/auth/keys/k1/history", None),
    ("POST", "/api/auth/clients", {"name": "c", "role": "viewer", "jwks": {"keys": []}}),
    ("GET", "/api/auth/clients", None),
    ("DELETE", "/api/auth/clients/c1", None),
    ("GET", "/api/auth/clients/c1/history", None),
    ("POST", "/api/auth/clients/c1/keys", {"jwks": {"keys": []}}),
    ("GET", "/api/audit/verify", None),
    ("GET", "/api/audit/export/fhir", None),
    ("POST", "/api/auth/invitations", {"email": "a@example.org", "roles": ["viewer"]}),
    ("GET", "/api/auth/invitations", None),
    ("DELETE", "/api/auth/invitations/i1", None),
    ("GET", "/api/auth/members", None),
    ("PUT", "/api/auth/members/m1/roles", {"roles": ["viewer"]}),
    ("DELETE", "/api/auth/members/m1", None),
    ("GET", "/api/auth/access-requests", None),
    ("POST", "/api/auth/access-requests/r1", {"outcome": "refused"}),
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
        "verify", "iter_events", "org_usage", "create_invitation", "invitations",
        "revoke_invitation", "members", "set_membership_roles", "remove_membership",
        "open_access_requests", "handle_access_request", "subject_history",
        "replace_client_keys",
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
    """Every route the server mounts, but the public ones, the pages and a person's own three:
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


def test_health_tells_any_principal_of_no_organization_only_the_public_fields(
    as_nobody: TestClient,
) -> None:
    """Even one holding every role: the organization decides."""
    assert set(as_nobody.get("/api/health").json()) == PUBLIC_HEALTH


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
    role gets. Only a person's own three routes differ: any person's in memberships mode (with
    an organization or none; never a key's, never anonymous), an administrator's in claims
    mode."""
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
    assert own == {("POST", path) for path in OWN_ROUTES}
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


def test_the_access_line_names_the_person_and_no_organization(
    members: TestClient, store: AuditStore, capsys: pytest.CaptureFixture[str]
) -> None:
    headers = member(store, org=None, sub="ada")
    capsys.readouterr()
    members.post("/api/auth/access-requests", json={"org": "clinic-z"}, headers=headers)
    [line] = [line for line in logged(capsys) if line["event"] == "request"]
    assert line["actor"] == person_id(IDP, "ada") and line.get("org") is None


# -- invitations and an organization's people (LP-12f) ----------------------------------------

UNKNOWN_TOKEN = "ohi_" + "A" * 43  # well formed, made by no one
# Every route an organization's administrators manage its people with (`keys`), by template.
PEOPLE_ROUTES = [
    ("POST", "/api/auth/invitations"),
    ("GET", "/api/auth/invitations"),
    ("DELETE", "/api/auth/invitations/{invitation_id}"),
    ("GET", "/api/auth/members"),
    ("PUT", "/api/auth/members/{membership_id}/roles"),
    ("DELETE", "/api/auth/members/{membership_id}"),
    ("GET", "/api/auth/access-requests"),
    ("POST", "/api/auth/access-requests/{request_id}"),
]


def admin_of(store: AuditStore, org: str = "clinic-a", sub: str = "lead") -> dict[str, str]:
    """An administrator of `org` (its record made if it has none), signed in with a verified
    address: request headers."""
    return member(store, org=org, roles=["admin"], sub=sub, email=f"{sub}@example.org")


def invite(
    client: TestClient,
    headers: dict[str, str],
    email: str = "ada@example.org",
    roles: tuple[str, ...] = ("viewer",),
) -> dict[str, Any]:
    made = client.post(
        "/api/auth/invitations", json={"email": email, "roles": list(roles)}, headers=headers
    )
    assert made.status_code == 201, made.text
    answer: dict[str, Any] = made.json()
    return answer


def token_of(made: dict[str, Any]) -> str:
    return str(made["link"]).removeprefix("/invite#")


def accept(client: TestClient, token: str, headers: dict[str, str] | None = None) -> Any:
    return client.post(ACCEPT, json={"token": token}, headers=headers)


def chained_since(store: AuditStore, count: int) -> list[dict[str, Any]]:
    """The events chained after the first `count`, oldest first, with their payloads."""
    with store._db.session(exclusive=False) as db:
        rows = db.all("SELECT event, payload FROM audit_events ORDER BY seq")
    return [{"event": r["event"], **json.loads(r["payload"])} for r in rows[count:]]


def every_row(store: AuditStore) -> str:
    """Every table a sign-in, an invitation or a membership writes, as text."""
    tables = ("invitations", "memberships", "users", "organizations", "audit_events")
    with store._db.session(exclusive=False) as db:
        return json.dumps(
            {t: [dict(r) for r in db.all(f"SELECT * FROM {t}")] for t in tables}, default=str
        )


def concrete_people_routes(store: AuditStore, org: str = "clinic-a") -> list[tuple[str, str, Any]]:
    """`PEOPLE_ROUTES` with real ids of `org` (an open invitation, a viewer's membership, an open
    request) and bodies that would act: a route let through by mistake would change them."""
    made = store.create_invitation(
        org=org, email="cy@example.org", roles=["viewer"], created_by="t"
    )
    viewer = store.add_membership(
        org=org, roles=["viewer"], created_by="t", user_id=recorded(store, f"viewer-of-{org}")
    )
    asked = store.request_access(user_id=recorded(store, f"asker-of-{org}"), org=org)
    return [
        ("POST", "/api/auth/invitations", {"email": "dee@example.org", "roles": ["admin"]}),
        ("GET", "/api/auth/invitations", None),
        ("DELETE", f"/api/auth/invitations/{made['id']}", None),
        ("GET", "/api/auth/members", None),
        ("PUT", f"/api/auth/members/{viewer['id']}/roles", {"roles": ["admin"]}),
        ("DELETE", f"/api/auth/members/{viewer['id']}", None),
        ("GET", "/api/auth/access-requests", None),
        ("POST", f"/api/auth/access-requests/{asked['id']}", {"outcome": "granted"}),
    ]


def test_an_administrator_invites_and_the_link_is_shown_once(
    members: TestClient, store: AuditStore
) -> None:
    lead = admin_of(store)
    made = members.post(
        "/api/auth/invitations",
        json={"email": " Ada@Example.org", "roles": ["reviewer", "viewer"], "days": 3},
        headers=lead,
    )
    assert made.status_code == 201
    shown = made.json()
    assert re.fullmatch(r"/invite#ohi_[A-Za-z0-9_-]{43}", shown["link"])
    assert "token" not in shown
    assert (shown["org"], shown["email"], shown["roles"], shown["status"]) == (
        "clinic-a",
        "ada@example.org",
        ["reviewer", "viewer"],
        "open",
    )
    assert shown["created_by"] == person_id(IDP, "lead")  # the administrator's id
    token = token_of(shown)
    listed = members.get("/api/auth/invitations", headers=lead)
    assert listed.status_code == 200
    [row] = listed.json()
    assert (row["id"], row["email"], row["status"]) == (shown["id"], "ada@example.org", "open")
    assert {"token", "token_sha256", "link"}.isdisjoint(row)
    assert (
        token not in listed.text and hashlib.sha256(token.encode()).hexdigest() not in listed.text
    )
    assert token not in every_row(store)  # its digest only
    assert store.verify()["ok"]


def test_an_invited_person_with_no_record_accepts_and_is_recorded_once(
    members: TestClient, store: AuditStore, idp: FakeIdp
) -> None:
    made = invite(members, admin_of(store), roles=("reviewer",))
    sign_in(members, idp, sub="ada", email="ada@example.org", email_verified=True, name="Ada")
    ada = person_id(IDP, "ada")
    assert store.user(ada) is None and me(members)["principal"]["org"] is None
    before = store.verify()["events"]
    accepted = accept(members, token_of(made))
    assert accepted.status_code == 200
    assert accepted.json() == {
        "org": "clinic-a",
        "name": "Clinic-A",
        "roles": ["reviewer"],
        "selected": True,
    }
    principal = me(members)["principal"]
    assert (principal["org"], principal["roles"]) == ("clinic-a", ["reviewer"])
    chained = chained_since(store, before)
    assert [e["event"] for e in chained] == [
        "user.recorded",
        "invitation.accepted",
        "membership.created",
    ]
    assert (chained[2]["user"], chained[2]["created_by"]) == (ada, f"invitation:{made['id']}")
    user = store.user(ada)
    assert user is not None and (user["email"], user["name"]) == ("ada@example.org", "Ada")
    sign_in(members, idp, sub="ada", email="ada@example.org", email_verified=True, name="Ada")
    assert len(events(store, "user.recorded")) == 2  # the administrator, and Ada once
    assert me(members)["principal"]["org"] == "clinic-a"  # her one membership, at sign-in
    assert store.verify()["ok"]


def test_a_bogus_or_used_link_records_no_one(
    members: TestClient, store: AuditStore, idp: FakeIdp
) -> None:
    used = invite(members, admin_of(store), "bo@example.org")
    sign_in(members, idp, sub="bo", email="bo@example.org", email_verified=True)
    assert accept(members, token_of(used)).status_code == 200
    sign_in(members, idp, sub="ada", email="ada@example.org", email_verified=True)
    before = store.verify()["events"]
    for token in (UNKNOWN_TOKEN, token_of(used)):
        refused = accept(members, token)
        assert refused.status_code == 404 and refused.json()["error"] == "InvitationRefused"
        assert refused.json()["detail"] == "this invitation is not valid: ask for a new one"
        assert refused.json()["reason"] == "invalid"
    assert store.user(person_id(IDP, "ada")) is None
    assert store.verify()["events"] == before
    assert store.verify()["ok"]


def test_an_expired_link_is_not_valid(members: TestClient, store: AuditStore, idp: FakeIdp) -> None:
    lead = admin_of(store)
    made = invite(members, lead)
    with store._db.session(exclusive=True) as db:
        db.all(
            "UPDATE invitations SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", made["id"]),
        )
    sign_in(members, idp, sub="ada", email="ada@example.org", email_verified=True)
    assert accept(members, token_of(made)).status_code == 404
    [row] = members.get("/api/auth/invitations", headers=lead).json()
    assert row["status"] == "expired"
    revoked = members.delete(f"/api/auth/invitations/{made['id']}", headers=lead)
    assert revoked.status_code == 204  # an expired one can still be revoked
    [row] = members.get("/api/auth/invitations", headers=lead).json()
    assert row["status"] == "revoked"
    assert store.verify()["ok"]


def test_a_used_link_is_not_valid(members: TestClient, store: AuditStore, idp: FakeIdp) -> None:
    lead = admin_of(store)
    made = invite(members, lead)
    sign_in(members, idp, sub="ada", email="ada@example.org", email_verified=True)
    assert accept(members, token_of(made)).status_code == 200
    ada = person_id(IDP, "ada")
    held = store.membership_in_force(ada, "clinic-a")
    assert held is not None
    assert members.delete(f"/api/auth/members/{held['id']}", headers=lead).status_code == 204
    assert accept(members, token_of(made)).status_code == 404  # removed: not back by the link
    sign_in(members, idp, sub="ada-again", email="ada@example.org", email_verified=True)
    assert accept(members, token_of(made)).status_code == 404  # nor another account
    [row] = members.get("/api/auth/invitations", headers=lead).json()
    assert (row["status"], row["accepted_by"]) == ("accepted", ada)
    assert store.verify()["ok"]


def test_a_revoked_link_is_not_valid(members: TestClient, store: AuditStore, idp: FakeIdp) -> None:
    lead = admin_of(store)
    made = invite(members, lead)
    assert members.delete(f"/api/auth/invitations/{made['id']}", headers=lead).status_code == 204
    sign_in(members, idp, sub="ada", email="ada@example.org", email_verified=True)
    assert accept(members, token_of(made)).status_code == 404
    again = members.delete(f"/api/auth/invitations/{made['id']}", headers=lead)
    assert again.status_code == 404 and again.json()["detail"] == "no open invitation"
    [row] = members.get("/api/auth/invitations", headers=lead).json()
    assert (row["status"], row["revoked_by"]) == ("revoked", person_id(IDP, "lead"))
    assert store.verify()["ok"]


def test_another_address_keeps_the_invitation_open(
    members: TestClient, store: AuditStore, idp: FakeIdp, capsys: pytest.CaptureFixture[str]
) -> None:
    made = invite(members, admin_of(store))
    sign_in(members, idp, sub="bo", email="bo@example.org", email_verified=True)
    before = store.verify()["events"]
    capsys.readouterr()
    refused = accept(members, token_of(made))
    assert refused.status_code == 403 and refused.json()["error"] == "InvitationRefused"
    assert refused.json()["detail"] == "this invitation is for another e-mail address"
    assert refused.json()["reason"] == "other_address"  # what the /invite page reads
    [line] = [line for line in logged(capsys) if line["event"] == "auth.invitation.accepted"]
    assert (line["outcome"], line["reason"], line.get("org")) == ("refused", "other_address", None)
    assert store.user(person_id(IDP, "bo")) is None and store.verify()["events"] == before
    sign_in(members, idp, sub="ada", email="ada@example.org", email_verified=True)
    assert accept(members, token_of(made)).status_code == 200
    assert store.verify()["ok"]


def test_an_unverified_address_cannot_accept(
    members: TestClient, store: AuditStore, idp: FakeIdp
) -> None:
    lead = admin_of(store)
    made = invite(members, lead)
    sign_in(members, idp, sub="ada", email="ada@example.org", email_verified=False)
    before = store.verify()["events"]
    refused = accept(members, token_of(made))
    assert refused.status_code == 403 and "verified" in refused.json()["detail"]
    assert refused.json()["reason"] == "unverified"
    assert store.user(person_id(IDP, "ada")) is None and store.verify()["events"] == before
    [row] = members.get("/api/auth/invitations", headers=lead).json()
    assert row["status"] == "open"
    assert store.verify()["ok"]


def test_a_member_accepting_again_is_refused_and_the_link_stays_open(
    members: TestClient, store: AuditStore
) -> None:
    lead = admin_of(store)
    viewer = member(store, org="clinic-a", roles=["viewer"], sub="ada", email="ada@example.org")
    made = invite(members, lead, roles=("admin",))
    before = store.verify()["events"]
    refused = accept(members, token_of(made), headers=viewer)
    assert refused.status_code == 409 and refused.json()["error"] == "InvitationRefused"
    held = store.membership_in_force(person_id(IDP, "ada"), "clinic-a")
    assert held is not None and held["roles"] == ["viewer"]
    [row] = members.get("/api/auth/invitations", headers=lead).json()
    assert row["status"] == "open"  # for an administrator to revoke
    assert store.verify()["events"] == before
    assert store.verify()["ok"]


def test_accepting_does_not_switch_a_person_acting_for_another_organization(
    members: TestClient, store: AuditStore
) -> None:
    made = invite(members, admin_of(store, org="clinic-b", sub="lead-b"), roles=("reviewer",))
    ada = member(store, org="clinic-a", roles=["viewer"], sub="ada", email="ada@example.org")
    accepted = accept(members, token_of(made), headers=ada)
    assert accepted.status_code == 200
    assert (accepted.json()["org"], accepted.json()["selected"]) == ("clinic-b", False)
    shown = me(members, ada)
    assert shown["principal"]["org"] == "clinic-a"
    assert [m["org"] for m in shown["memberships"]] == ["clinic-a", "clinic-b"]
    pinned = {**ada, "x-mendari-org": "clinic-a"}  # a window left on clinic-a still works
    assert members.get("/api/audit", headers=pinned).status_code == 200
    assert store.verify()["ok"]


def test_another_organizations_invitation_member_or_request_is_not_found(
    members: TestClient, store: AuditStore
) -> None:
    lead_a = admin_of(store, org="clinic-a", sub="lead-a")
    admin_of(store, org="clinic-b", sub="lead-b")
    theirs = concrete_people_routes(store, org="clinic-b")
    ours = invite(members, lead_a, "dee@example.org")
    asker = member(store, org=None, sub="fay", email="fay@example.org")
    asked = members.post("/api/auth/access-requests", json={"org": "clinic-a"}, headers=asker)
    assert asked.status_code == 202
    before = store.verify()["events"]
    for method, path, body in theirs:
        if method == "GET" or path == "/api/auth/invitations":
            continue  # the lists and creating: of the caller's organization by construction
        answer = members.request(method, path, json=body, headers=lead_a)
        assert answer.status_code == 404, (method, path, answer.status_code)
    assert store.verify()["events"] == before
    listed = members.get("/api/auth/invitations", headers=lead_a).json()
    assert [i["id"] for i in listed] == [ours["id"]]
    people = members.get("/api/auth/members", headers=lead_a).json()
    assert [m["user_id"] for m in people] == [person_id(IDP, "lead-a")]
    requests = members.get("/api/auth/access-requests", headers=lead_a).json()
    assert [r["user_id"] for r in requests] == [person_id(IDP, "fay")]
    assert store.verify()["ok"]


def test_an_organization_keeps_a_signed_in_administrator(
    members: TestClient, store: AuditStore
) -> None:
    lead = admin_of(store)
    mine = store.membership_in_force(person_id(IDP, "lead"), "clinic-a")
    assert mine is not None
    roles, membership = f"/api/auth/members/{mine['id']}/roles", f"/api/auth/members/{mine['id']}"
    for pending in (False, True):  # alone; then with a pending administrator, who counts for none
        if pending:
            store.add_membership(
                org="clinic-a", roles=["admin"], created_by="t", email="x@example.org"
            )
        demoted = members.put(roles, json={"roles": ["viewer"]}, headers=lead)
        assert demoted.status_code == 409 and "without an administrator" in demoted.json()["detail"]
        assert members.delete(membership, headers=lead).status_code == 409
    second = admin_of(store, sub="second")
    assert members.delete(membership, headers=lead).status_code == 204  # another one can act
    assert me(members, lead)["principal"]["org"] is None  # from their next request
    theirs = store.membership_in_force(person_id(IDP, "second"), "clinic-a")
    assert theirs is not None
    roles = f"/api/auth/members/{theirs['id']}/roles"
    assert members.put(roles, json={"roles": ["viewer"]}, headers=second).status_code == 409
    admin_of(store, sub="third")
    changed = members.put(roles, json={"roles": ["viewer"]}, headers=second)
    assert changed.status_code == 200 and changed.json()["roles"] == ["viewer"]
    assert members.get("/api/auth/members", headers=second).status_code == 403  # at once
    assert store.verify()["ok"]


def test_only_an_administrator_manages_people(members: TestClient, store: AuditStore) -> None:
    admin_of(store)
    routes = concrete_people_routes(store)
    roles = ("viewer", "reviewer", "governance", "service")
    people = {role: member(store, org="clinic-a", roles=[role], sub=f"as-{role}") for role in roles}
    before = store.verify()["events"]
    for role, headers in people.items():
        for method, path, body in routes:
            answer = members.request(method, path, json=body, headers=headers)
            assert answer.status_code == 403, (role, method, path, answer.status_code)
            assert answer.json()["detail"] == "your role may not keys here"
    assert store.verify()["events"] == before
    assert store.verify()["ok"]


def test_a_key_never_manages_people(members: TestClient, store: AuditStore, db_target: str) -> None:
    """A service key, and an administrator's key from before Phase 4: neither invites, lists,
    changes or removes anyone, nor accepts an invitation."""
    made = invite(members, admin_of(store), "ada@example.org")
    routes = [*concrete_people_routes(store), ("POST", ACCEPT, {"token": token_of(made)})]
    service = store.create_api_key(name="k", role="service", org="clinic-a", created_by="t")
    old_admin = legacy_key(db_target, "admin", "clinic-a")
    before = store.verify()["events"]
    for key in (service["key"], old_admin):
        bearer = {"authorization": f"Bearer {key}"}
        assert members.get("/api/audit", headers=bearer).status_code == 200  # a valid key
        for method, path, body in routes:
            answer = members.request(method, path, json=body, headers=bearer)
            assert answer.status_code == 403, (method, path, answer.status_code)
            assert answer.json()["detail"] == "only a person who signed in may do this"
    assert store.verify()["events"] == before
    assert store.verify()["ok"]


def test_reserved_roles_may_be_given_and_grant_nothing(
    members: TestClient, store: AuditStore, idp: FakeIdp
) -> None:
    lead = admin_of(store)
    made = invite(members, lead, roles=("pack_author", "labeller"))
    assert made["roles"] == ["labeller", "pack_author"]
    sign_in(members, idp, sub="ada", email="ada@example.org", email_verified=True)
    accepted = accept(members, token_of(made))
    assert accepted.status_code == 200 and accepted.json()["roles"] == ["labeller", "pack_author"]
    refused = members.get("/api/audit")
    assert refused.status_code == 403 and "may not read" in refused.json()["detail"]
    held = store.membership_in_force(person_id(IDP, "ada"), "clinic-a")
    assert held is not None
    changed = members.put(
        f"/api/auth/members/{held['id']}/roles", json={"roles": ["labeller"]}, headers=lead
    )
    assert changed.status_code == 200 and changed.json()["roles"] == ["labeller"]
    assert store.verify()["ok"]


def test_grading_is_never_given(members: TestClient, store: AuditStore) -> None:
    lead = admin_of(store)
    viewer = store.add_membership(
        org="clinic-a", roles=["viewer"], created_by="t", user_id=recorded(store, "bo")
    )
    asked = store.request_access(user_id=recorded(store, "cy"), org="clinic-a")
    invitation = "/api/auth/invitations"
    roles = f"/api/auth/members/{viewer['id']}/roles"
    grant = f"/api/auth/access-requests/{asked['id']}"
    before = store.verify()["events"]
    for method, path, body in (
        ("POST", invitation, {"email": "ada@example.org", "roles": ["grading"]}),
        ("PUT", roles, {"roles": ["grading"]}),
        ("POST", grant, {"outcome": "granted", "roles": ["grading"]}),
        ("POST", invitation, {"email": "ada@example.org", "roles": ["root"]}),
        ("PUT", roles, {"roles": []}),
        ("POST", grant, {"outcome": "granted", "roles": ["viewer", "root"]}),
        ("POST", grant, {"outcome": "maybe"}),
        ("POST", invitation, {"email": "ada.example.org", "roles": ["viewer"]}),
        ("POST", invitation, {"email": "ada@example.org", "roles": ["viewer"], "days": 31}),
    ):
        answer = members.request(method, path, json=body, headers=lead)
        assert answer.status_code == 422, (path, body, answer.status_code)
        assert "ada@example.org" not in answer.text and "ada.example.org" not in answer.text
    assert store.verify()["events"] == before
    assert store.verify()["ok"]


def test_an_administrator_grants_or_refuses_a_request(
    members: TestClient, store: AuditStore, idp: FakeIdp
) -> None:
    lead = admin_of(store)
    bo = member(store, org=None, sub="bo", email="bo@example.org")
    assert members.post(
        "/api/auth/access-requests", json={"org": "clinic-a"}, headers=bo
    ).is_success
    sign_in(members, idp, sub="ada", email="ada@example.org", email_verified=True, name="Ada")
    assert members.post("/api/auth/access-requests", json={"org": "clinic-a"}).is_success
    ada = person_id(IDP, "ada")
    listed = members.get("/api/auth/access-requests", headers=lead).json()
    assert [(r["user_id"], r["name"], r["email"]) for r in listed] == [
        (person_id(IDP, "bo"), "bo", "bo@example.org"),
        (ada, "Ada", "ada@example.org"),
    ]
    before = store.verify()["events"]
    granted = members.post(
        f"/api/auth/access-requests/{listed[1]['id']}",
        json={"outcome": "granted", "roles": ["reviewer"]},
        headers=lead,
    )
    assert granted.status_code == 200
    answer = granted.json()
    assert (answer["id"], answer["outcome"]) == (listed[1]["id"], "granted")
    assert (answer["membership"]["user_id"], answer["membership"]["roles"]) == (ada, ["reviewer"])
    assert [e["event"] for e in chained_since(store, before)] == [
        "access.handled",
        "membership.created",
    ]
    assert [m["org"] for m in me(members)["memberships"]] == ["clinic-a"]  # Ada's next request
    refused = members.post(
        f"/api/auth/access-requests/{listed[0]['id']}", json={"outcome": "refused"}, headers=lead
    )
    assert refused.status_code == 200 and refused.json()["membership"] is None
    assert members.get("/api/auth/access-requests", headers=lead).json() == []
    assert store.membership_in_force(person_id(IDP, "bo"), "clinic-a") is None
    assert store.verify()["ok"]


def test_joining_another_way_closes_an_open_request(
    members: TestClient, store: AuditStore, idp: FakeIdp
) -> None:
    """Accepting an invitation closes the person's open request to that organization, in the
    same transaction (`access.handled`, outcome `joined`): no administrator is left to grant it
    into a conflict. One left open from before that is a conflict to grant (`409`), and closes
    when refused."""
    lead = admin_of(store)
    made = invite(members, lead)
    sign_in(members, idp, sub="ada", email="ada@example.org", email_verified=True)
    assert members.post("/api/auth/access-requests", json={"org": "clinic-a"}).is_success
    [asked] = members.get("/api/auth/access-requests", headers=lead).json()
    handle = f"/api/auth/access-requests/{asked['id']}"
    before = store.verify()["events"]
    assert accept(members, token_of(made)).status_code == 200
    chained = chained_since(store, before)
    assert [e["event"] for e in chained] == [
        "invitation.accepted",
        "membership.created",
        "access.handled",
    ]
    assert (chained[2]["id"], chained[2]["outcome"], chained[2]["handled_by"]) == (
        asked["id"],
        "joined",
        f"membership:{chained[1]['id']}",
    )
    assert members.get("/api/auth/access-requests", headers=lead).json() == []
    gone = members.post(handle, json={"outcome": "granted"}, headers=lead)
    assert gone.status_code == 404 and gone.json()["detail"] == "no open request"
    with store._db.session(exclusive=True) as db:  # as a release before this one left it
        db.all(
            "UPDATE access_requests SET handled_at = NULL, handled_by = NULL, outcome = NULL "
            "WHERE id = ?",
            (asked["id"],),
        )
    before = store.verify()["events"]
    conflict = members.post(handle, json={"outcome": "granted"}, headers=lead)
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == (
        "already a member of 'clinic-a': refuse the request to close it"
    )
    assert store.verify()["events"] == before
    listed = members.get("/api/auth/access-requests", headers=lead).json()
    assert [r["id"] for r in listed] == [asked["id"]]
    assert members.post(handle, json={"outcome": "refused"}, headers=lead).status_code == 200
    assert members.get("/api/auth/access-requests", headers=lead).json() == []
    assert store.verify()["ok"]


def test_the_member_list_names_people_and_pending_addresses(
    members: TestClient, store: AuditStore
) -> None:
    lead = admin_of(store, org="default")
    store.bootstrap(email="Owner@Example.org", created_by="cli")  # pending until they sign in
    listed = members.get("/api/auth/members", headers=lead)
    assert listed.status_code == 200
    bound, pending = listed.json()
    assert bound | {"created_at": None, "id": None} == {
        "id": None,
        "user_id": person_id(IDP, "lead"),
        "name": "lead",
        "email": "lead@example.org",
        "pending": False,
        "roles": ["admin"],
        "created_by": "test",
        "created_at": None,
        "you": True,
    }
    assert (pending["user_id"], pending["name"], pending["email"]) == (
        None,
        None,
        "owner@example.org",
    )
    assert (pending["pending"], pending["roles"], pending["you"]) == (True, ["admin"], False)
    assert store.verify()["ok"]


def test_the_chain_names_the_administrator_by_id_never_by_address(
    members: TestClient, store: AuditStore
) -> None:
    lead = admin_of(store)
    bo = store.add_membership(
        org="clinic-a", roles=["viewer"], created_by="t", user_id=recorded(store, "bo")
    )
    asked = store.request_access(user_id=recorded(store, "cy"), org="clinic-a")
    before = store.verify()["events"]
    made = invite(members, lead)
    assert members.delete(f"/api/auth/invitations/{made['id']}", headers=lead).status_code == 204
    roles = f"/api/auth/members/{bo['id']}/roles"
    assert members.put(roles, json={"roles": ["reviewer"]}, headers=lead).status_code == 200
    assert members.delete(f"/api/auth/members/{bo['id']}", headers=lead).status_code == 204
    granted = members.post(
        f"/api/auth/access-requests/{asked['id']}", json={"outcome": "granted"}, headers=lead
    )
    assert granted.status_code == 200
    chained = chained_since(store, before)
    assert [e["event"] for e in chained] == [
        "invitation.created",
        "invitation.revoked",
        "membership.roles_changed",
        "membership.removed",
        "access.handled",
        "membership.created",
    ]
    assert "@" not in json.dumps(chained)
    named = ("created_by", "revoked_by", "changed_by", "removed_by", "handled_by")
    who = {e[key] for e in chained for key in named if key in e}
    assert who == {person_id(IDP, "lead")}
    assert store.verify()["ok"]


def test_a_removed_member_cannot_rejoin_with_an_invitation_left_open(
    members: TestClient, store: AuditStore
) -> None:
    """Bo, a viewer, cannot accept an administrator's invitation while a member (`409`, it stays
    open); removing him revokes it, so the old link does not bring him back as an
    administrator."""
    lead = admin_of(store)
    bo = member(store, org="clinic-a", roles=["viewer"], sub="bo", email="bo@example.org")
    made = invite(members, lead, "bo@example.org", ("admin",))
    assert accept(members, token_of(made), headers=bo).status_code == 409
    held = store.membership_in_force(person_id(IDP, "bo"), "clinic-a")
    assert held is not None
    before = store.verify()["events"]
    assert members.delete(f"/api/auth/members/{held['id']}", headers=lead).status_code == 204
    chained = chained_since(store, before)
    assert [e["event"] for e in chained] == ["membership.removed", "invitation.revoked"]
    assert (chained[1]["id"], chained[1]["revoked_by"]) == (made["id"], person_id(IDP, "lead"))
    refused = accept(members, token_of(made), headers=bo)
    assert refused.status_code == 404 and refused.json()["error"] == "InvitationRefused"
    assert store.membership_in_force(person_id(IDP, "bo"), "clinic-a") is None
    [row] = members.get("/api/auth/invitations", headers=lead).json()
    assert (row["status"], row["revoked_by"]) == ("revoked", person_id(IDP, "lead"))
    assert store.verify()["ok"]


def test_each_administrators_change_is_logged_by_its_ids(
    members: TestClient, store: AuditStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """The security log names who acted, the organization and what changed by ids only: never
    an address or a token."""
    lead = admin_of(store)
    bo = store.add_membership(
        org="clinic-a", roles=["viewer"], created_by="t", user_id=recorded(store, "bo")
    )
    asked = [store.request_access(user_id=recorded(store, s), org="clinic-a")["id"] for s in "xy"]
    capsys.readouterr()
    made = invite(members, lead)
    assert members.delete(f"/api/auth/invitations/{made['id']}", headers=lead).status_code == 204
    roles = f"/api/auth/members/{bo['id']}/roles"
    assert members.put(roles, json={"roles": ["reviewer"]}, headers=lead).status_code == 200
    assert members.delete(f"/api/auth/members/{bo['id']}", headers=lead).status_code == 204
    handle = "/api/auth/access-requests/{}"
    granted = members.post(handle.format(asked[0]), json={"outcome": "granted"}, headers=lead)
    refused = members.post(handle.format(asked[1]), json={"outcome": "refused"}, headers=lead)
    assert granted.status_code == refused.status_code == 200
    lines = logged(capsys)
    lead_id = person_id(IDP, "lead")
    assert [
        (line["event"], line["outcome"], line["actor"], line["org"], line["ids"])
        for line in lines
        if line["event"].startswith("admin.")
    ] == [
        ("admin.invitation.created", "ok", lead_id, "clinic-a", [made["id"]]),
        ("admin.invitation.revoked", "ok", lead_id, "clinic-a", [made["id"]]),
        ("admin.membership.roles_changed", "ok", lead_id, "clinic-a", [bo["id"]]),
        ("admin.membership.removed", "ok", lead_id, "clinic-a", [bo["id"]]),
        (
            "admin.access.granted",
            "ok",
            lead_id,
            "clinic-a",
            [asked[0], granted.json()["membership"]["id"]],
        ),
        ("admin.access.refused", "ok", lead_id, "clinic-a", [asked[1]]),
    ]
    shown = json.dumps(lines)
    assert "@" not in shown and token_of(made)[4:] not in shown


def test_the_invitation_token_is_shown_once_and_never_logged_or_echoed(
    members: TestClient, store: AuditStore, idp: FakeIdp, capsys: pytest.CaptureFixture[str]
) -> None:
    lead = admin_of(store)
    capsys.readouterr()
    made = invite(members, lead)
    token = token_of(made)
    sign_in(members, idp, sub="ada", email="ada@example.org", email_verified=True)
    for sent in (token + "x", token.replace("ohi_", "ohk_"), f" {token}"):
        malformed = members.post(ACCEPT, json={"token": sent})
        assert malformed.status_code == 422 and token[4:] not in malformed.text
    unknown = accept(members, UNKNOWN_TOKEN)
    assert unknown.status_code == 404 and UNKNOWN_TOKEN[4:] not in unknown.text
    assert accept(members, token).status_code == 200
    lines = logged(capsys)
    assert lines and token[4:] not in json.dumps(lines) and "@" not in json.dumps(lines)
    assert [
        (line["event"], line["outcome"], line.get("reason"), line.get("org"))
        for line in lines
        if line["event"].startswith(("admin.invitation", "auth.invitation"))
    ] == [
        ("admin.invitation.created", "ok", None, "clinic-a"),
        ("auth.invitation.accepted", "refused", "invalid", None),
        ("auth.invitation.accepted", "ok", None, "clinic-a"),
    ]
    assert store.verify()["ok"]


def test_accepting_needs_a_person_from_this_site(members: TestClient, store: AuditStore) -> None:
    made = invite(members, admin_of(store))
    body = {"token": token_of(made)}
    key = store.create_api_key(name="k", role="service", org="clinic-a", created_by="t")["key"]
    as_key = members.post(ACCEPT, json=body, headers={"authorization": f"Bearer {key}"})
    assert as_key.status_code == 403 and "only a person" in as_key.json()["detail"]
    assert members.post(ACCEPT, json=body).status_code == 401
    headers = member(store, org=None, sub="ada", email="ada@example.org")
    elsewhere = members.post(
        ACCEPT,
        json=body,
        headers={**headers, "origin": "https://evil.example.org", "sec-fetch-site": "cross-site"},
    )
    assert elsewhere.status_code == 403
    assert members.post(ACCEPT, json=body, headers=headers).status_code == 200


def test_people_routes_ignore_the_window_pin_but_administration_does_not(
    members: TestClient, store: AuditStore
) -> None:
    lead = admin_of(store)
    made = invite(members, lead)
    ada = member(store, org="clinic-b", roles=["viewer"], sub="ada", email="ada@example.org")
    assert accept(members, token_of(made), headers={**ada, "x-mendari-org": "clinic-z"}).is_success
    stale = members.get("/api/auth/members", headers={**lead, "x-mendari-org": "clinic-b"})
    assert stale.status_code == 409 and stale.json()["error"] == "OrganizationChanged"
    shown = members.get("/api/auth/members", headers={**lead, "x-mendari-org": "clinic-a"})
    assert shown.status_code == 200


def test_the_new_routes_have_the_permissions_they_should(tmp_path: Path) -> None:
    """Memberships mode's routes: a person's own (`person`) and an organization's people
    (`keys`); in claims mode, where none is mounted, each reads as any route no rule lists
    (`admin`), as a person's own did before, so the guard, the notice check and the rate limits
    answer there as for any unknown route."""
    assert permission_for("POST", ACCEPT) == "person"
    assert permission_for("POST", "/api/auth/access-requests") == "person"
    assert permission_for("POST", "/api/auth/organization") == "person"
    for method, path in PEOPLE_ROUTES:
        concrete = re.sub(r"\{[^}]+\}", "x", path)
        assert permission_for(method, concrete) == "keys", (method, path)
    # Nothing under them falls to `read` (addresses) or is a person's.
    for method, path in (
        ("GET", "/api/auth/invitations/x"),
        ("GET", "/api/auth/members/x"),
        ("PATCH", "/api/auth/members/x/roles"),
        ("DELETE", "/api/auth/invitations/accept"),
        ("GET", "/api/auth/invitations/accept"),
    ):
        assert permission_for(method, path) == "keys", (method, path)
    for method, path in (("POST", "/api/auth/invitationsx"), ("POST", "/api/auth/membersx")):
        assert permission_for(method, path) == "admin", (method, path)  # unlisted: deny
    store = AuditStore(tmp_path / "a.db")
    by_records = Auth(
        AuthSettings(IDP, CLIENT, None, "roles", {}, None, None, accounts="memberships"), store
    )
    by_claims = Auth(AuthSettings(IDP, CLIENT, None, "roles", {}, None, None), store)
    every = [*PEOPLE_ROUTES, *(("POST", path) for path in OWN_ROUTES)]
    for method, path in every:
        concrete = re.sub(r"\{[^}]+\}", "x", path)
        assert by_records.permission(method, concrete) == permission_for(method, concrete)
        assert by_claims.permission(method, concrete) == "admin", (method, path)
    for method, path in (("GET", "/api/auth/keys"), ("POST", "/api/auth/clients")):
        assert by_claims.permission(method, path) == "keys"  # claims mode's own, as before
    assert by_claims.permission("GET", "/api/auth/me") == "public"


def test_default_administrators_may_invite(
    members: TestClient, store: AuditStore, idp: FakeIdp
) -> None:
    store.bootstrap(email="owner@example.org", created_by="cli")
    sign_in(members, idp, sub="owner", email="owner@example.org", email_verified=True)
    made = members.post(
        "/api/auth/invitations", json={"email": "ada@example.org", "roles": ["viewer"]}
    )
    assert made.status_code == 201 and made.json()["org"] == "default"
    sign_in(members, idp, sub="ada", email="ada@example.org", email_verified=True)
    accepted = accept(members, token_of(made.json()))
    assert accepted.status_code == 200 and accepted.json()["org"] == "default"
    assert me(members)["principal"]["org"] == "default"
    assert store.verify()["ok"]


def test_inviting_and_granting_wait_for_the_notice_and_accepting_does_not(
    db_target: str, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Giving someone access is minting one (`mendari.notices`): an administrator acknowledges
    the notice first. Accepting is not gated: a new member is asked before they decide."""
    monkeypatch.setenv("MENDARI_NOTICE_ACKNOWLEDGEMENT", "required")
    store = AuditStore(db_target)
    lead = admin_of(store)
    made = store.create_invitation(
        org="clinic-a", email="ada@example.org", roles=["viewer"], created_by="t"
    )
    asked = store.request_access(user_id=recorded(store, "bo"), org="clinic-a")
    with memberships_app(db_target, idp, monkeypatch) as client:
        gated = [
            client.post(
                "/api/auth/invitations",
                json={"email": "cy@example.org", "roles": ["viewer"]},
                headers=lead,
            ),
            client.post(
                f"/api/auth/access-requests/{asked['id']}",
                json={"outcome": "granted"},
                headers=lead,
            ),
        ]
        assert [answer.json()["error"] for answer in gated] == ["NoticeNotAcknowledged"] * 2
        ada = member(store, org=None, sub="ada", email="ada@example.org")
        assert accept(client, made["token"], headers=ada).status_code == 200


def test_the_reference_documents_the_memberships_routes(
    members: TestClient, store: AuditStore
) -> None:
    schema = members.get("/openapi.json", headers=admin_of(store)).json()
    for method, path in [*PEOPLE_ROUTES, ("POST", ACCEPT)]:
        operation = schema["paths"][path][method.lower()]
        assert operation["tags"] == ["Sign-in"], (method, path)
        assert operation["summary"] and operation["description"], (method, path)
        assert operation["security"] == [{"session": []}], (method, path)


# -- rate limits --------------------------------------------------------------------------------


def test_a_persons_own_routes_count_as_signing_in_in_memberships_mode_only(
    tmp_path: Path, db_target: str, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Memberships mode: choosing and asking, open to any account that signs in, count per
    address with signing in. Claims mode: the paths are not there, and an administrator's
    requests to them stay `404`s that count against nothing, as before."""
    monkeypatch.setenv("MENDARI_RATE_LIMITS", "on,signin=2/min:2")
    with signing_in_with(idp, tmp_path, monkeypatch) as claims:
        admin = signed_in(AuditStore(tmp_path / "a.db"), role="admin")
        answers = [
            claims.post("/api/auth/organization", json={"org": "clinic-a"}, headers=admin)
            for _ in range(4)
        ]
        assert [a.status_code for a in answers] == [404] * 4
        assert claims.get("/auth/login", follow_redirects=False).status_code == 302
    with memberships_app(db_target, idp, monkeypatch) as client:
        headers = member(AuditStore(db_target), org="clinic-a", roles=["viewer"], sub="ada")
        answers = [
            client.post("/api/auth/access-requests", json={"org": "clinic-b"}, headers=headers)
            for _ in range(4)
        ]
        assert 429 in [a.status_code for a in answers]


def test_accepting_counts_as_signing_in(
    db_target: str, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Open to any account that signs in, and a guess at a token: counted per address."""
    monkeypatch.setenv("MENDARI_RATE_LIMITS", "on,signin=2/min:2")
    with memberships_app(db_target, idp, monkeypatch) as client:
        headers = member(AuditStore(db_target), org=None, sub="ada", email="ada@example.org")
        answers = [accept(client, UNKNOWN_TOKEN, headers=headers) for _ in range(3)]
        assert [a.status_code for a in answers] == [404, 404, 429]
        assert int(answers[2].headers["retry-after"]) > 0


# -- defence in depth -------------------------------------------------------------------------


def test_a_route_reached_without_an_organization_is_refused_never_widened(
    tmp_path: Path,
) -> None:
    """Should a principal of no organization ever reach a route that gives the store an
    organization, it is refused there too; and with sign-in on, a route reached with no
    principal at all is refused, never served as open mode (every organization)."""
    assert auth_module.org_of(Principal(kind="key", name="k", id="key:1", actor="key 1")) == (
        "default"
    )
    with pytest.raises(NoOrganization):
        auth_module.org_of(NOBODY)
    with pytest.raises(NoOrganization):
        auth_module.scope_of(NOBODY)
    assert auth_module.scope_of(auth_module.OPEN) is None
    request = Request({"type": "http", "method": "GET", "path": "/api/holds", "headers": []})
    store = AuditStore(tmp_path / "a.db")
    settings = AuthSettings(IDP, CLIENT, None, "roles", {}, None, None, accounts="memberships")
    with pytest.raises(AuthError) as unset:
        Auth(settings, store).acting(request)
    assert unset.value.status == 401
    with pytest.raises(AuthError):
        auth_module.let_through(request)
    assert Auth(None, store).acting(request) is auth_module.OPEN


# -- credentials and settings ------------------------------------------------------------------


def test_a_client_of_an_organization_with_no_record_is_refused(
    db_target: str, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm

    from mendari.server.backend_services import check_jwks, default_scope

    monkeypatch.setenv("MENDARI_BACKEND_SERVICES", "on")
    monkeypatch.setenv("MENDARI_PUBLIC_URL", HTTPS)
    store = AuditStore(db_target)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwks = check_jwks({"keys": [{**json.loads(RSAAlgorithm.to_jwk(key.public_key())), "kid": "k"}]})
    client = store.register_client(
        name="c", role="viewer", org="clinic-z", created_by="t", scopes=default_scope("viewer"),
        jwks=jwks,
    )  # fmt: skip
    token, _ = store.issue_token(client["client_id"], default_scope("viewer"), 300)
    bearer = {"authorization": f"Bearer {token}"}
    with memberships_app(db_target, idp, monkeypatch) as app:
        assert app.get("/fhir/Task", headers=bearer).status_code == 401  # what its scope reads
        store.create_organization(org="clinic-z", name="Clinic Z", created_by="t")
        assert app.get("/fhir/Task", headers=bearer).status_code == 200


def test_ignored_claim_settings_are_named_at_startup(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", CLIENT)
    monkeypatch.setenv("MENDARI_OIDC_ROLE_MAP", "clinicians=admin")
    monkeypatch.setenv("MENDARI_OIDC_ORG_CLAIM", "org")
    with caplog.at_level(logging.WARNING, logger="mendari.auth"):
        auth_settings()  # claims mode: they are its settings
        assert caplog.records == []
        monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
        auth_settings()
    [warned] = caplog.records
    assert "MENDARI_OIDC_ROLE_MAP, MENDARI_OIDC_ORG_CLAIM ignored" in warned.getMessage()
