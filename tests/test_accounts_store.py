# SPDX-License-Identifier: Apache-2.0
"""People, organizations, memberships, invitations and access requests in the store (LP-12d,
ADR-0008): every write chains ids and digests only; the bootstrap adds and rewrites nothing; an
invitation is single-use, expiring and bound to a verified address; no organization is left
without an administrator; no membership holds `grading`."""

from __future__ import annotations

import json
from typing import Any

import pytest

from mendari import AuditStore, Decision, Pack
from mendari.core import accounts
from mendari.errors import AuthError
from tests.conftest import message

IDP = "https://idp.example.org"


def person(store: AuditStore, subject: str, email: str | None) -> dict[str, Any]:
    return store.record_user(issuer=IDP, subject=subject, email=email, name=subject)


def dump(store: AuditStore, *tables: str) -> str:
    """Every row of `tables`, in order, as text."""
    with store._db.session(exclusive=False) as db:
        return json.dumps(
            {t: [dict(r) for r in db.all(f"SELECT * FROM {t} ORDER BY 1")] for t in tables},
            sort_keys=True,
            default=str,
        )


def test_the_tables_are_added_and_no_older_database_reads_as_unmigrated(db_target: str) -> None:
    store = AuditStore(db_target)
    assert store.organizations() == [] and store.memberships() == []
    assert AuditStore(db_target, read_only=True).unmigrated == []


def test_the_bootstrap_adds_the_owner_once_and_rewrites_nothing(
    store: AuditStore, inbox_pack: Pack
) -> None:
    inputs = message("Can I get a refill?")
    store.record(Decision(inbox_pack)(inputs), inputs)
    store.create_api_key(name="k", role="viewer", org="default", created_by="Ada")
    others = ("decisions", "api_keys", "automation_holds", "governance_approvals")
    before = dump(store, *others)
    first = store.bootstrap(email="Owner@Example.org", created_by="cli")
    assert first["organization"]["id"] == "default"
    assert first["membership"]["pending_email"] == "owner@example.org"
    events = store.verify()["events"]
    assert store.bootstrap(email="owner@example.org", created_by="cli") == {
        "organization": None,
        "membership": None,
    }
    assert store.verify()["events"] == events  # nothing twice
    assert dump(store, *others) == before  # and nothing rewritten
    owner = person(store, "owner", "owner@example.org")
    [bound] = store.bind_pending(owner["id"], "owner@example.org")
    assert (bound["user_id"], bound["roles"]) == (owner["id"], ["admin"])
    assert store.bootstrap(email="owner@example.org", created_by="cli")["membership"] is None


def test_the_bootstrap_never_picks_a_person_by_a_stored_address(store: AuditStore) -> None:
    """An address stored at an earlier sign-in proves nothing now (the account may have changed
    hands): the owner's membership waits for a sign-in that verifies it."""
    earlier = person(store, "earlier", "owner@example.org")
    made = store.bootstrap(email="owner@example.org", created_by="cli")["membership"]
    assert (made["user_id"], made["pending_email"]) == (None, "owner@example.org")
    assert store.memberships(org="default", user_id=earlier["id"]) == []


def test_organizations_are_named_records_and_default_is_reserved(store: AuditStore) -> None:
    store.create_organization(org="clinic-a", name="Clinic A", created_by="cli")
    for org in ("default", "Clinic", "-a", "a", "x" * 64, "clinic a"):
        with pytest.raises(ValueError):
            store.create_organization(org=org, name="X", created_by="cli")
    with pytest.raises(ValueError, match="already exists"):
        store.create_organization(org="clinic-a", name="Again", created_by="cli")
    assert [o["id"] for o in store.organizations()] == ["clinic-a"]


def test_an_invitation_is_single_use_expiring_and_for_its_verified_address(
    store: AuditStore,
) -> None:
    store.create_organization(org="clinic-a", name="Clinic A", created_by="cli")
    ada = person(store, "ada", "ada@example.org")
    bo = person(store, "bo", "bo@example.org")
    made = store.create_invitation(
        org="clinic-a", email="Ada@Example.org", roles=["reviewer"], created_by="cli"
    )
    with pytest.raises(AuthError, match="another e-mail address"):
        store.accept_invitation(made["token"], user_id=bo["id"], verified_email="bo@example.org")
    with pytest.raises(AuthError, match="verified"):
        store.accept_invitation(made["token"], user_id=ada["id"], verified_email=None)
    joined = store.accept_invitation(
        made["token"], user_id=ada["id"], verified_email="ADA@example.org"
    )
    assert (joined["org"], joined["user_id"], joined["roles"]) == (
        "clinic-a",
        ada["id"],
        ["reviewer"],
    )
    revoked = store.create_invitation(
        org="clinic-a", email="cy@example.org", roles=["viewer"], created_by="cli"
    )
    assert store.revoke_invitation(revoked["id"], org="clinic-a", revoked_by="cli") is True
    expired = store.create_invitation(
        org="clinic-a", email="cy@example.org", roles=["viewer"], created_by="cli"
    )
    with store._db.session(exclusive=True) as db:
        db.all(
            "UPDATE invitations SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", expired["id"]),
        )
    cy = person(store, "cy", "cy@example.org")
    for token in (made["token"], revoked["token"], expired["token"], "ohi_unknown"):
        with pytest.raises(AuthError, match="not valid") as refused:
            store.accept_invitation(token, user_id=cy["id"], verified_email="cy@example.org")
        assert refused.value.status == 404  # one answer: used, revoked, expired or unknown
    with pytest.raises(ValueError):
        store.create_invitation(
            org="clinic-a", email="cy@example.org", roles=["viewer"], created_by="cli", days=31
        )
    tables = ("invitations", "memberships", "users", "organizations", "audit_events")
    assert made["token"] not in dump(store, *tables)  # its hash only


def test_roles_grading_is_never_held_and_an_organization_keeps_an_admin(
    store: AuditStore,
) -> None:
    store.create_organization(org="clinic-a", name="Clinic A", created_by="cli")
    ada = person(store, "ada", "ada@example.org")
    with pytest.raises(ValueError, match="grading"):
        store.add_membership(org="clinic-a", roles=["grading"], created_by="cli", user_id=ada["id"])
    with pytest.raises(ValueError, match="unknown role"):
        store.add_membership(org="clinic-a", roles=["root"], created_by="cli", user_id=ada["id"])
    admin = store.add_membership(
        org="clinic-a",
        roles=["admin", "labeller", "pack_author"],
        created_by="cli",
        user_id=ada["id"],
    )
    assert admin["roles"] == ["admin", "labeller", "pack_author"]
    with pytest.raises(ValueError, match="already a member"):
        store.add_membership(org="clinic-a", roles=["viewer"], created_by="cli", user_id=ada["id"])
    with pytest.raises(ValueError, match="without an administrator"):
        store.set_membership_roles(admin["id"], ["viewer"], org="clinic-a", changed_by="cli")
    with pytest.raises(ValueError, match="without an administrator"):
        store.remove_membership(admin["id"], org="clinic-a", removed_by="cli")
    bo = person(store, "bo", "bo@example.org")
    second = store.add_membership(
        org="clinic-a", roles=["admin"], created_by="cli", user_id=bo["id"]
    )
    assert store.remove_membership(admin["id"], org="clinic-a", removed_by="cli") is True
    assert store.remove_membership(admin["id"], org="clinic-a", removed_by="cli") is False
    assert store.remove_membership(second["id"], org="other", removed_by="cli") is False
    assert [m["user_id"] for m in store.memberships(org="clinic-a")] == [bo["id"]]


def test_only_an_administrator_who_signed_in_keeps_an_organization_administered(
    store: AuditStore,
) -> None:
    """A pending administrator's membership (nobody has taken it up) never counts as the one that
    keeps an organization administered, and removing one never lowers how many can act."""
    store.create_organization(org="clinic-a", name="Clinic A", created_by="cli")
    ada = person(store, "ada", "ada@example.org")
    acting = store.add_membership(
        org="clinic-a", roles=["admin"], created_by="cli", user_id=ada["id"]
    )
    pending = store.add_membership(
        org="clinic-a", roles=["admin"], created_by="cli", email="bo@example.org"
    )
    with pytest.raises(ValueError, match="without an administrator"):
        store.remove_membership(acting["id"], org="clinic-a", removed_by="cli")
    with pytest.raises(ValueError, match="without an administrator"):
        store.set_membership_roles(acting["id"], ["viewer"], org="clinic-a", changed_by="cli")
    assert store.set_membership_roles(pending["id"], ["viewer"], org="clinic-a", changed_by="cli")
    assert store.remove_membership(pending["id"], org="clinic-a", removed_by="cli") is True


def test_a_request_for_access_is_asked_once_and_granted_or_refused(store: AuditStore) -> None:
    store.create_organization(org="clinic-a", name="Clinic A", created_by="cli")
    ada = person(store, "ada", "ada@example.org")
    asked = store.request_access(user_id=ada["id"], org="clinic-a")
    assert store.request_access(user_id=ada["id"], org="clinic-a")["id"] == asked["id"]
    assert [r["id"] for r in store.access_requests("clinic-a")] == [asked["id"]]
    granted = store.handle_access_request(
        asked["id"], org="clinic-a", outcome="granted", handled_by="cli", roles=["reviewer"]
    )
    assert granted is not None and granted["membership"]["roles"] == ["reviewer"]
    assert store.access_requests() == []
    assert (
        store.handle_access_request(
            asked["id"], org="clinic-a", outcome="refused", handled_by="cli"
        )
        is None
    )


def test_the_chain_holds_ids_and_digests_never_an_address_or_a_name(store: AuditStore) -> None:
    store.bootstrap(email="owner@example.org", created_by="cli")
    store.create_organization(org="clinic-a", name="Clinic of Northfield", created_by="cli")
    ada = person(store, "ada", "ada@example.org")
    made = store.create_invitation(
        org="clinic-a", email="ada@example.org", roles=["viewer"], created_by="cli"
    )
    store.accept_invitation(made["token"], user_id=ada["id"], verified_email="ada@example.org")
    asked = store.request_access(user_id=ada["id"], org="default")
    store.handle_access_request(asked["id"], org="default", outcome="refused", handled_by="cli")
    payloads = dump(store, "audit_events")
    assert "@" not in payloads and "Northfield" not in payloads
    with store._db.session(exclusive=False) as db:
        [requested] = db.all("SELECT payload FROM audit_events WHERE event = 'access.requested'")
    assert json.loads(requested["payload"])["org"] == "default"  # the id, not a digest of it
    with store._db.session(exclusive=False) as db:
        chained = {r["event"] for r in db.all("SELECT event FROM audit_events")}
    assert {
        "user.recorded", "organization.created", "membership.created", "invitation.created",
        "invitation.accepted", "access.requested", "access.handled",
    } <= chained  # fmt: skip
    assert store.verify()["ok"]


def test_an_address_is_ascii_lower_cased_and_never_case_folded(store: AuditStore) -> None:
    """Case-folding maps different mailboxes to one ("ß" to "ss", the Kelvin sign to "k"), and
    the match proves who someone is: an address here is ASCII, its letters lower-cased."""
    assert accounts.normalise_email(" Ada@Example.ORG ") == "ada@example.org"
    for address in ("stra\u00dfe@example.org", "\u212aim@example.org", "ada@ex\u00e4mple.org"):
        with pytest.raises(ValueError, match="ASCII"):
            accounts.normalise_email(address)
    store.create_organization(org="clinic-a", name="Clinic A", created_by="cli")
    with pytest.raises(ValueError, match="ASCII"):
        store.create_invitation(
            org="clinic-a", email="stra\u00dfe@example.org", roles=["viewer"], created_by="cli"
        )
    signed_in = person(store, "kim", "\u212aim@example.org")  # still signs in, matches nothing
    assert signed_in["email"] is None


def test_an_address_digest_is_keyed_when_there_is_an_input_hash_key() -> None:
    from mendari.core.phi import PhiPolicy

    plain = accounts.email_digest(PhiPolicy(), "Ada@Example.org")
    keyed = accounts.email_digest(PhiPolicy(hash_key=b"k" * 32), "ada@example.org ")
    assert plain.startswith("sha256:") and keyed.startswith("hmac-sha256:")
    assert keyed == accounts.email_digest(PhiPolicy(hash_key=b"k" * 32), "ADA@example.org")


def test_the_membership_roles_are_the_servers_and_the_reserved_ones() -> None:
    from mendari.server.auth import ROLES

    assert accounts.PERSON_ROLES == ROLES
    assert set(accounts.MEMBERSHIP_ROLES) == {*ROLES, "pack_author", "labeller"}


def test_an_older_database_reads_as_having_no_records_every_row_defaults(
    db_target: str, inbox_pack: Pack
) -> None:
    """Read-only, a database from before these records, or before tenancy's `org` column, is
    read as it is (SQLite and Postgres): no memberships, no requests, every decision
    `default`'s."""
    store = AuditStore(db_target)
    inputs = message("Can I get a refill?")
    store.record(Decision(inbox_pack)(inputs), inputs)
    with store._db.session(exclusive=True) as db:
        for table in ("access_requests", "invitations", "memberships", "organizations"):
            db.all(f"DROP TABLE {table}")
        db.all("ALTER TABLE decisions DROP COLUMN org")
    store.close()
    older = AuditStore(db_target, read_only=True)
    assert older.memberships() == [] and older.access_requests() == []
    assert older.organizations() == []
    assert older.organizations_in_use() == [
        {"org": "default", "decisions": 1, "recorded": False, "recordable": True}
    ]
