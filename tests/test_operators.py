# SPDX-License-Identifier: Apache-2.0
"""Who runs a deployment, set down in its configuration (the one-install path):

- **The first administrator** (`MENDARI_BOOTSTRAP_ADMIN`, memberships mode): the first person
  who signs in with that address, verified by the identity provider, is made an administrator
  of the operator organization, whose record is made with it. Once: the database is marked in
  the same transaction, and from then on the setting gives nothing, whoever signs in and
  whatever it names. Chained as made by the setting.
- **The operator organization** (`MENDARI_OPERATOR_ORG`): its administrators, signed in as
  people, hold `operate`, and the deployment's own views (the whole chain's verification, the
  deployment records) answer them. In memberships mode nobody holds it without the setting, and
  never another organization's administrator; in claims mode every administrator does unless
  the setting names one organization.

SQLite and Postgres."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore
from mendari.core import accounts
from mendari.core.audit import FIRST_ADMIN, FIRST_ADMIN_BY
from mendari.errors import MendariError
from mendari.server.app import create_app
from mendari.server.auth import person_id
from tests.conftest import DASHBOARD, encrypt_mode, encrypted_store, member, signed_in
from tests.test_auth import CLIENT, IDP, FakeIdp, sign_in, signing_in_with
from tests.test_memberships import HTTPS, events, me, memberships_app

ADA = "ada@hospital.example"
SYSTEM = "/api/system/checks"
DEPLOYMENT_VIEWS = ("/api/audit/verify", "/api/deployments")


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp()


def first_admin(monkeypatch: pytest.MonkeyPatch, address: str = ADA, org: str = "default") -> None:
    monkeypatch.setenv("MENDARI_BOOTSTRAP_ADMIN", address)
    monkeypatch.setenv("MENDARI_OPERATOR_ORG", org)
    monkeypatch.setenv("MENDARI_BOOTSTRAP_ORG_NAME", "St Mary's Hospital")


def chained(store: AuditStore) -> int:
    with store._db.session(exclusive=False) as db:
        row = db.one("SELECT COUNT(*) AS n FROM audit_events")
    return int(row["n"]) if row else 0


def marked(store: AuditStore) -> str | None:
    with store._db.session(exclusive=False) as db:
        row = db.one("SELECT value FROM settings WHERE name = ?", (FIRST_ADMIN,))
    return None if row is None else str(row["value"])


# -- the first administrator ----------------------------------------------------------------------


def test_the_first_administrator_is_made_once_for_that_address_at_its_first_sign_in(
    db_target: str, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_admin(monkeypatch, "Ada@Hospital.Example")  # letter case never tells addresses apart
    with (
        memberships_app(db_target, idp, monkeypatch) as client,
        encrypted_store(db_target) as store,
    ):
        # Anyone else signs in as before: no organization, no record, nothing chained.
        sign_in(client, idp, sub="bo", email="bo@hospital.example", email_verified=True)
        assert me(client)["principal"]["org"] is None
        # The address alone is not enough: the identity provider must have verified it.
        sign_in(client, idp, sub="mallory", email=ADA, email_verified=False)
        assert me(client)["principal"]["org"] is None
        sign_in(client, idp, sub="mallory", email=ADA)
        assert me(client)["principal"]["org"] is None
        assert chained(store) == 0 and marked(store) is None and store.organizations() == []

        answer = sign_in(client, idp, sub="ada", email=ADA, email_verified=True, name="Ada L.")
        assert answer.status_code == 302
        shown = me(client)["principal"]
        assert (shown["org"], shown["roles"]) == ("default", ["admin"])
        assert "operate" in shown["permissions"]
        ada = person_id(IDP, "ada")
        (membership,) = store.memberships(org="default")
        assert (membership["user_id"], membership["roles"]) == (ada, ["admin"])
        assert membership["created_by"] == FIRST_ADMIN_BY
        assert store.organization("default")["name"] == "St Mary's Hospital"  # type: ignore[index]
        assert marked(store) == membership["id"]
        made = events(store, "organization.created", "user.recorded", "membership.created")
        assert [event["event"] for event in made] == [
            "organization.created",
            "user.recorded",
            "membership.created",
        ]
        assert made[0]["created_by"] == FIRST_ADMIN_BY and made[0]["id"] == "default"
        assert made[0]["name_digest"] == accounts.name_digest("St Mary's Hospital")
        assert (made[2]["user"], made[2]["roles"], made[2]["created_by"]) == (
            ada,
            ["admin"],
            FIRST_ADMIN_BY,
        )
        assert ADA not in str(made) and "Ada L." not in str(made)  # ids and digests only
        assert store.verify()["ok"]
        assert client.get(SYSTEM).status_code == 200  # the deployment's operator, in the console

        # Their next sign-in changes nothing.
        before = chained(store)
        sign_in(client, idp, sub="ada", email=ADA, email_verified=True)
        assert chained(store) == before and len(store.memberships(org="default")) == 1
        # Someone else who later holds the address (another subject) gets nothing: used once.
        sign_in(client, idp, sub="ada-again", email=ADA, email_verified=True)
        assert me(client)["principal"]["org"] is None
        assert len(store.memberships(org="default")) == 1


def test_the_setting_is_ignored_afterwards_whatever_it_names_and_whoever_administers(
    tmp_path: Path, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After its one use the setting gives nothing: not to its own address once that person is
    no longer a member, not after a restart, and not to another address written in its place."""
    db = str(tmp_path / "a.db")
    first_admin(monkeypatch)
    with memberships_app(db, idp, monkeypatch) as client, encrypted_store(db) as store:
        sign_in(client, idp, sub="ada", email=ADA, email_verified=True)
        (first,) = store.memberships(org="default")
        # A second administrator, so the first can be removed (never the last one).
        bo = member(store, org="default", roles=["admin"], sub="bo", email="bo@hospital.example")
        removed = client.delete(f"/api/auth/members/{first['id']}", headers=bo)
        assert removed.status_code == 204
        sign_in(client, idp, sub="ada", email=ADA, email_verified=True)
        assert me(client)["principal"]["org"] is None  # the setting does not bring them back
        assert marked(store) == first["id"]
    # A restart, and another address in the setting: still nothing.
    first_admin(monkeypatch, "eve@hospital.example")
    with memberships_app(db, idp, monkeypatch) as client, encrypted_store(db) as store:
        sign_in(client, idp, sub="eve", email="eve@hospital.example", email_verified=True)
        assert me(client)["principal"]["org"] is None
        assert [m["user_id"] for m in store.memberships(org="default")] == [person_id(IDP, "bo")]
        assert marked(store) == first["id"] and store.verify()["ok"]


def test_an_organization_that_has_an_administrator_takes_no_first_one(
    tmp_path: Path, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing deployment that gains the setting: its organization has an administrator who
    signed in, so the setting's address is given nothing, and nothing is marked or chained."""
    db = str(tmp_path / "a.db")
    first_admin(monkeypatch)
    with memberships_app(db, idp, monkeypatch) as client, encrypted_store(db) as store:
        member(store, org="default", roles=["admin"], sub="lead")
        before = chained(store)
        sign_in(client, idp, sub="ada", email=ADA, email_verified=True)
        assert me(client)["principal"]["org"] is None
        assert chained(store) == before and marked(store) is None


def test_a_member_the_setting_names_becomes_an_administrator(
    tmp_path: Path, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Someone given another role from the command line, in an organization nobody administers:
    the setting makes them its administrator, chained as a change of roles."""
    db = str(tmp_path / "a.db")
    first_admin(monkeypatch, org="st-marys")
    with memberships_app(db, idp, monkeypatch) as client, encrypted_store(db) as store:
        store.create_organization(org="st-marys", name="St Mary's", created_by="t")
        store.record_user(issuer=IDP, subject="ada", email=None, name="Ada")
        ada = person_id(IDP, "ada")
        store.add_membership(org="st-marys", roles=["viewer"], created_by="t", user_id=ada)
        sign_in(client, idp, sub="ada", email=ADA, email_verified=True)
        shown = me(client)["principal"]
        assert (shown["org"], shown["roles"]) == ("st-marys", ["admin", "viewer"])
        (changed,) = events(store, "membership.roles_changed")
        assert changed["previous"] == ["viewer"] and changed["changed_by"] == FIRST_ADMIN_BY
        assert events(store, "organization.created")[0]["created_by"] == "t"  # not made twice
        assert store.verify()["ok"]


def test_the_first_administrator_setting_needs_memberships_mode_and_an_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_BOOTSTRAP_ADMIN", ADA)
    with pytest.raises(MendariError, match="needs memberships mode"):
        create_app(db_path=tmp_path / "open.db")  # sign-in off
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", CLIENT)
    encrypt_mode(monkeypatch)
    with pytest.raises(MendariError, match="needs memberships mode"):
        create_app(db_path=tmp_path / "claims.db")  # roles from claims: nothing to give
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    for bad in ("ada", "ada@", "ädä@hospital.example"):
        monkeypatch.setenv("MENDARI_BOOTSTRAP_ADMIN", bad)
        with pytest.raises(MendariError, match="MENDARI_BOOTSTRAP_ADMIN"):
            create_app(db_path=tmp_path / "bad.db")
    monkeypatch.setenv("MENDARI_BOOTSTRAP_ADMIN", ADA)
    monkeypatch.setenv("MENDARI_BOOTSTRAP_ORG_NAME", "x" * 201)
    with pytest.raises(MendariError, match="at most 200"):
        create_app(db_path=tmp_path / "long.db")


def test_the_organization_is_named_after_the_public_address_when_no_name_is_given(
    tmp_path: Path, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = str(tmp_path / "a.db")
    monkeypatch.setenv("MENDARI_BOOTSTRAP_ADMIN", ADA)
    monkeypatch.setenv("MENDARI_OPERATOR_ORG", "default")
    monkeypatch.setenv("MENDARI_PUBLIC_URL", HTTPS)
    with memberships_app(db, idp, monkeypatch) as client, encrypted_store(db) as store:
        sign_in(client, idp, sub="ada", email=ADA, email_verified=True)
        assert store.organization("default")["name"] == "mendari.test"  # type: ignore[index]


# -- the operator organization --------------------------------------------------------------------


def answers(client: TestClient, headers: dict[str, str], path: str) -> int:
    return client.get(path, headers=headers).status_code


def test_the_operator_organizations_administrators_operate_and_nobody_else(
    db_target: str, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Memberships mode, two organizations: with `MENDARI_OPERATOR_ORG=clinic-a` the System
    routes and the deployment's own views answer clinic-a's administrators, signed in as
    people, and nobody else: not its other roles, not its keys, not clinic-b's administrator."""
    monkeypatch.setenv("MENDARI_OPERATOR_ORG", "clinic-a")
    with (
        memberships_app(db_target, idp, monkeypatch) as client,
        encrypted_store(db_target) as store,
    ):
        operator = member(store, org="clinic-a", roles=["admin"], sub="lead-a")
        other = member(store, org="clinic-b", roles=["admin"], sub="lead-b")
        assert "operate" in me(client, operator)["principal"]["permissions"]
        assert "operate" not in me(client, other)["principal"]["permissions"]
        assert answers(client, operator, SYSTEM) == 200
        for path in DEPLOYMENT_VIEWS:
            assert answers(client, operator, path) == 200, path
        verified: dict[str, Any] = client.get("/api/audit/verify", headers=operator).json()
        assert verified["ok"] is True and verified["events"] > 0
        # Another organization's administrator: refused the System routes, and the deployment's
        # views are not there for them, as before the setting.
        assert answers(client, other, SYSTEM) == 403
        for path in DEPLOYMENT_VIEWS:
            assert answers(client, other, path) == 404, path
        for method in ("audit/verify", "alerts/test", "ehr-check"):
            refused = client.post(f"/api/system/{method}", headers=other, json={"base": "x"})
            assert refused.status_code == 403, method
        # The operator organization's other roles: none of them operates.
        for role in ("viewer", "reviewer", "service", "governance"):
            person = member(store, org="clinic-a", roles=[role], sub=f"a-{role}")
            assert "operate" not in me(client, person)["principal"]["permissions"], role
            assert answers(client, person, SYSTEM) == 403, role
            assert answers(client, person, "/api/deployments") == 404, role
            assert answers(client, person, "/api/audit/verify") == 403, role  # needs `admin`
        # Nor a machine of the operator organization, whatever its role.
        for role in ("viewer", "service"):
            key = store.create_api_key(name=role, role=role, org="clinic-a", created_by="t")
            bearer = {"authorization": f"Bearer {key['key']}"}
            assert answers(client, bearer, SYSTEM) == 403, role
            assert answers(client, bearer, "/api/deployments") == 404, role
        # Nor anyone signed out.
        assert client.get(SYSTEM, headers={"cookie": ""}).status_code == 401


def test_memberships_mode_without_the_setting_is_as_it_was(
    tmp_path: Path, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = str(tmp_path / "a.db")
    with memberships_app(db, idp, monkeypatch) as client, encrypted_store(db) as store:
        lead = member(store, org="default", roles=["admin"], sub="lead")
        assert "operate" not in me(client, lead)["principal"]["permissions"]
        assert answers(client, lead, SYSTEM) == 403
        for path in DEPLOYMENT_VIEWS:
            assert answers(client, lead, path) == 404, path


def test_in_claims_mode_every_administrator_operates_unless_one_organization_is_named(
    tmp_path: Path, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "a.db"
    with signing_in_with(idp, tmp_path, monkeypatch) as client, encrypted_store(db) as store:
        north = signed_in(store, role="admin", org="north")
        south = signed_in(store, role="admin", org="south")
        for person in (north, south):
            assert "operate" in me(client, person)["principal"]["permissions"]
            assert answers(client, person, SYSTEM) == 200
            for path in DEPLOYMENT_VIEWS:
                assert answers(client, person, path) == 200, path
        reviewer = signed_in(store, role="reviewer", org="north")
        assert answers(client, reviewer, SYSTEM) == 403
    monkeypatch.setenv("MENDARI_OPERATOR_ORG", "north")
    app = create_app(db_path=db)
    with TestClient(app, base_url=HTTPS, headers=DASHBOARD) as client:
        assert answers(client, north, SYSTEM) == 200
        assert answers(client, south, SYSTEM) == 403
        for path in DEPLOYMENT_VIEWS:
            assert answers(client, north, path) == 200, path
            assert answers(client, south, path) == 404, path


def test_the_operator_organization_is_an_organization_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for bad in ("Clinic A", "x", "clinic_a"):
        monkeypatch.setenv("MENDARI_OPERATOR_ORG", bad)
        with pytest.raises(MendariError, match="MENDARI_OPERATOR_ORG"):
            create_app(db_path=tmp_path / "a.db")
