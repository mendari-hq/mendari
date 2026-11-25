# SPDX-License-Identifier: Apache-2.0
"""People, organizations, memberships, invitations and access requests in the store (LP-12d,
ADR-0008): every write chains ids and digests only; the bootstrap adds and rewrites nothing; an
invitation is single-use, expiring and bound to a verified address; no organization is left
without an administrator; no membership holds `grading`."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from mendari import AuditStore, Decision, Pack
from mendari.core import accounts
from mendari.errors import AuthError, InvitationRefused
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
    with pytest.raises(AuthError, match="another e-mail address") as other:
        store.accept_invitation(made["token"], user_id=bo["id"], verified_email="bo@example.org")
    assert isinstance(other.value, InvitationRefused)
    assert (other.value.status, other.value.reason) == (403, "other_address")
    with pytest.raises(InvitationRefused, match="verified") as unverified:
        store.accept_invitation(made["token"], user_id=ada["id"], verified_email=None)
    assert (unverified.value.status, unverified.value.reason) == (403, "unverified")
    joined = store.accept_invitation(
        made["token"], user_id=ada["id"], verified_email="ADA@example.org"
    )
    assert (joined["org"], joined["user_id"], joined["roles"]) == (
        "clinic-a",
        ada["id"],
        ["reviewer"],
    )
    again = store.create_invitation(
        org="clinic-a", email="ada@example.org", roles=["admin"], created_by="cli"
    )
    events = store.verify()["events"]
    with pytest.raises(InvitationRefused, match="already a member") as member:
        store.accept_invitation(again["token"], user_id=ada["id"], verified_email="ada@example.org")
    assert (member.value.status, member.value.reason) == (409, "member")
    assert store.verify()["events"] == events  # nothing written: it stays open, roles as they were
    [still] = [i for i in store.invitations("clinic-a") if i["id"] == again["id"]]
    assert still["status"] == "open"
    assert store.memberships(org="clinic-a", user_id=ada["id"])[0]["roles"] == ["reviewer"]
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
        with pytest.raises(InvitationRefused, match="not valid") as refused:
            store.accept_invitation(token, user_id=cy["id"], verified_email="cy@example.org")
        # One answer: used, revoked, expired or unknown.
        assert (refused.value.status, refused.value.reason) == (404, "invalid")
    assert {i["id"]: i["status"] for i in store.invitations("clinic-a")} == {
        made["id"]: "accepted",
        again["id"]: "open",
        revoked["id"]: "revoked",
        expired["id"]: "expired",
    }
    with pytest.raises(ValueError):
        store.create_invitation(
            org="clinic-a", email="cy@example.org", roles=["viewer"], created_by="cli", days=31
        )
    tables = ("invitations", "memberships", "users", "organizations", "audit_events")
    assert made["token"] not in dump(store, *tables)  # its hash only


def test_accepting_with_an_asker_records_the_person_only_on_success(store: AuditStore) -> None:
    """Who signed in is recorded with the membership, after every check and in the same
    transaction: a link that is not valid, or not theirs, records no one."""
    store.create_organization(org="clinic-a", name="Clinic A", created_by="cli")
    made = store.create_invitation(
        org="clinic-a", email="ada@example.org", roles=["viewer"], created_by="cli"
    )
    ada = accounts.person_id(IDP, "ada")
    asker = {"issuer": IDP, "subject": "ada", "email": "ada@example.org", "name": "Ada"}
    before = store.verify()["events"]
    for token, address in (("ohi_unknown", "ada@example.org"), (made["token"], "bo@example.org")):
        with pytest.raises(InvitationRefused):
            store.accept_invitation(token, user_id=ada, verified_email=address, asker=asker)
    with pytest.raises(ValueError, match="not the person"):
        store.accept_invitation(
            made["token"],
            user_id=accounts.person_id(IDP, "bo"),
            verified_email="ada@example.org",
            asker=asker,
        )
    with pytest.raises(ValueError, match="no such person"):  # no asker, no record: nothing kept
        store.accept_invitation(made["token"], user_id=ada, verified_email="ada@example.org")
    assert store.user(ada) is None and store.verify()["events"] == before
    joined = store.accept_invitation(
        made["token"], user_id=ada, verified_email="ada@example.org", asker=asker
    )
    assert (joined["user_id"], joined["created_by"]) == (ada, f"invitation:{made['id']}")
    user = store.user(ada)
    assert user is not None
    assert (user["email"], user["name"], user["last_sign_in_at"]) == (
        "ada@example.org",
        "Ada",
        None,
    )
    assert store.verify()["events"] == before + 3  # the person, the acceptance, the membership
    assert store.verify()["ok"]


def test_refusals_carry_their_reason(store: AuditStore) -> None:
    """A membership change the store refuses says why (`MembershipRefused`, a `ValueError`, so
    the command line's one-line error still reads it), with the messages as they were."""
    store.create_organization(org="clinic-a", name="Clinic A", created_by="cli")
    ada = person(store, "ada", "ada@example.org")
    admin = store.add_membership(
        org="clinic-a", roles=["admin"], created_by="cli", user_id=ada["id"]
    )
    with pytest.raises(accounts.MembershipRefused, match="already a member of 'clinic-a'") as twice:
        store.add_membership(org="clinic-a", roles=["viewer"], created_by="cli", user_id=ada["id"])
    assert twice.value.reason == "member"
    with pytest.raises(accounts.MembershipRefused, match="without an administrator") as last:
        store.set_membership_roles(admin["id"], ["viewer"], org="clinic-a", changed_by="cli")
    assert last.value.reason == "last_admin"
    with pytest.raises(accounts.MembershipRefused, match="without an administrator") as removed:
        store.remove_membership(admin["id"], org="clinic-a", removed_by="cli")
    assert removed.value.reason == "last_admin"
    with pytest.raises(accounts.MembershipRefused, match="no such membership") as elsewhere:
        store.set_membership_roles(admin["id"], ["viewer"], org="clinic-b", changed_by="cli")
    assert elsewhere.value.reason == "no_membership" and isinstance(elsewhere.value, ValueError)
    bo = person(store, "bo", "bo@example.org")
    asked = store.request_access(user_id=bo["id"], org="clinic-a")
    store.add_membership(org="clinic-a", roles=["viewer"], created_by="cli", user_id=bo["id"])
    with store._db.session(exclusive=True) as db:  # left open, as a release before this one did
        db.all(
            "UPDATE access_requests SET handled_at = NULL, outcome = NULL WHERE id = ?",
            (asked["id"],),
        )
    with pytest.raises(accounts.MembershipRefused, match="already a member") as granted:
        store.handle_access_request(
            asked["id"], org="clinic-a", outcome="granted", handled_by="cli"
        )
    assert granted.value.reason == "member"
    assert [r["id"] for r in store.open_access_requests("clinic-a")] == [asked["id"]]  # still open


def test_removing_a_member_revokes_the_invitations_made_out_to_them(store: AuditStore) -> None:
    """An invitation they could not accept while a member (`member`, it stays open) never lets
    them back in once removed: the organization's open invitations to their recorded address
    are revoked with the membership, by whoever removed it. Others' invitations, another
    organization's, and the ones they made stay as they were."""
    for org in ("clinic-a", "clinic-b"):
        store.create_organization(org=org, name=org, created_by="cli")
    lead = person(store, "lead", "lead@example.org")
    store.add_membership(org="clinic-a", roles=["admin"], created_by="cli", user_id=lead["id"])
    bo = person(store, "bo", "bo@example.org")
    held = store.add_membership(
        org="clinic-a", roles=["viewer"], created_by="cli", user_id=bo["id"]
    )

    def invitation(org: str, email: str, created_by: str = lead["id"]) -> dict[str, Any]:
        return store.create_invitation(org=org, email=email, roles=["admin"], created_by=created_by)

    left_open = invitation("clinic-a", "bo@example.org")
    with pytest.raises(InvitationRefused) as member:
        store.accept_invitation(
            left_open["token"], user_id=bo["id"], verified_email="bo@example.org"
        )
    assert member.value.reason == "member"
    untouched = [
        invitation("clinic-b", "bo@example.org"),  # another organization's
        invitation("clinic-a", "cy@example.org"),  # another address
        invitation("clinic-a", "dee@example.org", created_by=bo["id"]),  # one they made
    ]
    with store._db.session(exclusive=False) as db:
        before = len(db.all("SELECT seq FROM audit_events"))
    assert store.remove_membership(held["id"], org="clinic-a", removed_by=lead["id"]) is True
    with store._db.session(exclusive=False) as db:
        rows = db.all("SELECT event, payload FROM audit_events ORDER BY seq")[before:]
    assert [(r["event"], json.loads(r["payload"])) for r in rows] == [
        ("membership.removed", {"id": held["id"], "org": "clinic-a", "removed_by": lead["id"]}),
        (
            "invitation.revoked",
            {"id": left_open["id"], "org": "clinic-a", "revoked_by": lead["id"]},
        ),
    ]
    status = {
        i["id"]: (i["status"], i["revoked_by"])
        for org in ("clinic-a", "clinic-b")
        for i in store.invitations(org)
    }
    assert status[left_open["id"]] == ("revoked", lead["id"])
    assert [status[i["id"]] for i in untouched] == [("open", None)] * 3
    with pytest.raises(InvitationRefused) as gone:
        store.accept_invitation(
            left_open["token"], user_id=bo["id"], verified_email="bo@example.org"
        )
    assert (gone.value.status, gone.value.reason) == (404, "invalid")
    assert store.membership_in_force(bo["id"], "clinic-a") is None
    # A pending membership revokes the invitations to its address too; a person with no address
    # on record revokes nothing.
    pending = store.add_membership(
        org="clinic-a", roles=["viewer"], created_by="cli", email="eve@example.org"
    )
    to_eve = store.create_invitation(
        org="clinic-a", email="eve@example.org", roles=["admin"], created_by=lead["id"]
    )
    kim = person(store, "kim", None)
    unaddressed = store.add_membership(
        org="clinic-a", roles=["viewer"], created_by="cli", user_id=kim["id"]
    )
    for removed in (pending, unaddressed):
        assert store.remove_membership(removed["id"], org="clinic-a", removed_by=lead["id"])
    after = {
        i["id"]: (i["status"], i["revoked_by"])
        for org in ("clinic-a", "clinic-b")
        for i in store.invitations(org)
    }
    assert after.pop(to_eve["id"]) == ("revoked", lead["id"])
    assert after == status
    assert store.verify()["ok"]


def test_a_request_closes_when_its_person_joins_another_way(store: AuditStore) -> None:
    """Accepting an invitation, a membership given to them, or one bound at sign-in closes the
    person's open request to that organization in the same transaction (`access.handled`,
    outcome `joined`, handled by the membership): it no longer counts against their open
    requests, and no administrator grants it into a conflict."""
    orgs = ("clinic-a", "clinic-b", "clinic-c")
    for org in orgs:
        store.create_organization(org=org, name=org, created_by="cli")
    ada = person(store, "ada", "ada@example.org")
    asked = {org: store.request_access(user_id=ada["id"], org=org)["id"] for org in orgs}
    made = store.create_invitation(
        org="clinic-a", email="ada@example.org", roles=["viewer"], created_by="cli"
    )
    invited = store.accept_invitation(
        made["token"], user_id=ada["id"], verified_email="ada@example.org"
    )
    given = store.add_membership(
        org="clinic-b", roles=["viewer"], created_by="cli", user_id=ada["id"]
    )
    store.add_membership(
        org="clinic-c", roles=["viewer"], created_by="cli", email="ada@example.org"
    )
    [bound] = store.bind_pending(ada["id"], "ada@example.org")
    by_org = {"clinic-a": invited, "clinic-b": given, "clinic-c": bound}
    expected = {(asked[org], "joined", f"membership:{m['id']}") for org, m in by_org.items()}
    with store._db.session(exclusive=False) as db:
        closed = db.all("SELECT * FROM access_requests WHERE handled_at IS NOT NULL")
        chained = db.all("SELECT payload FROM audit_events WHERE event = 'access.handled'")
    assert {(r["id"], r["outcome"], r["handled_by"]) for r in closed} == expected
    payloads = [json.loads(r["payload"]) for r in chained]
    assert {(p["id"], p["outcome"], p["handled_by"]) for p in payloads} == expected
    assert len(payloads) == 3
    assert store.access_requests() == []  # none counts against Ada's open requests any more
    assert store.verify()["ok"]


def test_two_accepts_of_one_link_make_one_membership(store: AuditStore, db_target: str) -> None:
    """Instances accepting one link at once, through their own connections (accounts with the
    same verified address, say): one joins, the others get the one answer for a used link, and
    the chain holds one acceptance."""
    store.create_organization(org="clinic-a", name="Clinic A", created_by="cli")
    made = store.create_invitation(
        org="clinic-a", email="ada@example.org", roles=["viewer"], created_by="cli"
    )
    people = [person(store, f"ada-{n}", "ada@example.org")["id"] for n in range(6)]
    start = threading.Barrier(len(people))

    def accept(user_id: str) -> str:
        with AuditStore(db_target) as other:
            start.wait()
            try:
                other.accept_invitation(
                    made["token"], user_id=user_id, verified_email="ada@example.org"
                )
            except InvitationRefused as refused:
                return refused.reason
            return "joined"

    with ThreadPoolExecutor(len(people)) as pool:
        answers = sorted(pool.map(accept, people))
    assert answers == ["invalid"] * (len(people) - 1) + ["joined"]
    with store._db.session(exclusive=False) as db:
        accepted = db.all("SELECT seq FROM audit_events WHERE event = 'invitation.accepted'")
    assert len(accepted) == 1 and len(store.memberships(org="clinic-a")) == 1
    assert store.verify()["ok"]


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
    assert set(accounts.MEMBERSHIP_ROLES) == {
        *ROLES,
        "pack_author",
        "labeller",
        "registry_reviewer",
    }


def test_a_persons_memberships_are_read_as_they_are_now(store: AuditStore) -> None:
    store.create_organization(org="clinic-a", name="Clinic A", created_by="cli")
    ada = person(store, "ada", "ada@example.org")
    assert store.membership_in_force(ada["id"], "clinic-a") is None
    made = store.add_membership(
        org="clinic-a", roles=["viewer"], created_by="cli", user_id=ada["id"]
    )
    found = store.membership_in_force(ada["id"], "clinic-a")
    assert found is not None and (found["id"], found["roles"]) == (made["id"], ["viewer"])
    assert store.member_of(ada["id"]) == [
        {"org": "clinic-a", "name": "Clinic A", "roles": ["viewer"]}
    ]
    assert store.member_of(person(store, "bo", None)["id"]) == []
    assert not store.pending_for("cy@example.org")
    store.add_membership(org="clinic-a", roles=["viewer"], created_by="cli", email="Cy@example.org")
    assert store.pending_for("cy@example.org")


def test_asking_to_join_needs_a_record_no_membership_and_few_open_requests(
    store: AuditStore,
) -> None:
    ada = person(store, "ada", "ada@example.org")
    before = store.verify()["events"]
    with pytest.raises(accounts.AccessRefused) as unknown:
        store.request_access(user_id=ada["id"], org="clinic-z")
    assert unknown.value.reason == "unknown_organization"
    store.create_organization(org="clinic-a", name="Clinic A", created_by="cli")
    store.add_membership(org="clinic-a", roles=["viewer"], created_by="cli", user_id=ada["id"])
    with pytest.raises(accounts.AccessRefused) as joined:
        store.request_access(user_id=ada["id"], org="clinic-a")
    assert joined.value.reason == "member"
    assert store.verify()["events"] == before + 2  # the organization and the membership only
    for n in range(accounts.OPEN_REQUESTS_MAX):
        store.create_organization(org=f"clinic-{n}", name=str(n), created_by="cli")
        store.request_access(user_id=ada["id"], org=f"clinic-{n}")
    store.create_organization(org="clinic-x", name="X", created_by="cli")
    with pytest.raises(accounts.AccessRefused) as many:
        store.request_access(user_id=ada["id"], org="clinic-x")
    assert many.value.reason == "open_requests"
    asked_again = store.request_access(user_id=ada["id"], org="clinic-0")  # an open one: as is
    assert asked_again["org"] == "clinic-0"


def test_keeping_a_person_current_chains_nothing(store: AuditStore) -> None:
    first = person(store, "ada", "ada@example.org")
    events = store.verify()["events"]
    again = store.record_user(issuer=IDP, subject="ada", email="ADA@example.org", name="Ada L.")
    assert again["id"] == first["id"] and again["name"] == "Ada L."
    assert store.verify()["events"] == events
