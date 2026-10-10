# SPDX-License-Identifier: Apache-2.0
"""The organization in the chain (LP-12g, ADR-0008).

A database a memberships-mode server opened is marked (`org_bound`): from then on every event
names the organization of the row it is about, whichever store writes it (the command line's
too), and `verify` checks that name against the row, so a row moved to another organization, or
removed, breaks it. An unmarked database chains the kinds of before as it did, and the previous
release's `verify` still passes a bound chain. On SQLite and Postgres (`db_target`)."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from mendari import AuditStore, Decision, Pack
from mendari.core import audit as audit_module
from mendari.core import tenancy
from mendari.core.audit import (
    ADMINISTRATIVE_KINDS,
    BOUND_WHEN_MARKED,
    DECISION_KINDS,
    DEPLOYMENT_KINDS,
    PERSON_KINDS,
    SUBJECT_TABLES,
)
from mendari.core.evalset_keys import EvalsetKeys
from mendari.core.evalsets import EvalsetStore
from mendari.core.phi import PhiPolicy, key_id
from mendari.server.app import create_app
from mendari.standards import audit_fhir
from tests.conftest import encrypt_mode, evalset_keys_for_test, hand_written, message
from tests.test_audit_fhir import NOT_ON_A_DECISION, recorded_kinds

KEY = os.urandom(32)
EVALSETS = evalset_keys_for_test()  # hidden evaluation sets' throwaway keys (LP-11e)
NEXT_EVALSET_KEY = evalset_keys_for_test().key  # what they rotate to (LP-11f)
URGENT = "Crushing chest pain and my left arm is numb"  # HUMAN_REVIEW: waits for a reviewer
INBOX = "mendari/patient-inbox-router"
CLINIC = "clinic-a"


def keeping(target: str, *, bind_org: bool = False, **options: Any) -> AuditStore:
    """A store under PHI mode `encrypt`, so inputs and notes are kept apart (and can be read,
    erased and purged)."""
    phi = PhiPolicy(mode="encrypt", key=KEY)
    return AuditStore(target, phi=phi, bind_org=bind_org, **options)


def chained(store: AuditStore) -> list[dict[str, Any]]:
    with store._db.session(exclusive=False) as db:
        rows = db.all(
            "SELECT seq, decision_id, event, payload, hash FROM audit_events ORDER BY seq"
        )
    return [{**row, "payload": json.loads(row["payload"])} for row in rows]


def tamper(store: AuditStore, sql: str, params: tuple[Any, ...] = ()) -> None:
    """Edit the database behind the store's back, as an attacker with DB access could."""
    with store._db.session(exclusive=True) as db:
        db.all(sql, params)


def jwks() -> list[dict[str, Any]]:
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm

    from mendari.server.backend_services import check_jwks

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = {**json.loads(RSAAlgorithm.to_jwk(key.public_key())), "kid": "k"}
    return check_jwks({"keys": [public]})


@contextmanager
def acting(org: str | None) -> Iterator[None]:
    """As a server acting for `org`, or as the command line (no organization) for None."""
    if org is None:
        yield
        return
    with tenancy.acting_for(org):
        yield


def every_writer(store: AuditStore, pack: Pack, org: str | None) -> dict[str, Any]:
    """Each kind of event the store writes for an organization's rows, with the organization
    acting (`org`), or with none, as the command line and the scheduled purge write; the ids
    of what was made."""
    made: dict[str, Any] = {}
    with acting(org):
        first = Decision(pack, store=store)(message(URGENT)).id
        store.resolve_review(first, final_label="nurse", reviewer="rn-1", note="a note")
        rating = store.record_feedback(first, rating="down", reporter="rn-2", note="a note")
        store.record_outcome(first, final_label="nurse", source="ehr", recorded_by="ehr-feed")
        store.record_event(first, "pas.submitted", {"outcome": "queued"}, note="the payer's")
        store.get(first, reader="rn-7")  # `input.read`
        second = Decision(pack, store=store)(message(URGENT)).id
        third = Decision(pack, store=store)(message(URGENT)).id
        store.resolve_review(third, final_label="nurse", reviewer="rn-1", note="a note")
        approval = store.approve_automation(
            pack_id=INBOX,
            pack_digest="d" * 64,
            risk_level=2,
            approver="cmo",
            reference="ticket-1",
            expires_at="2999-01-01T00:00:00+00:00",
        )
        store.revoke_approval(approval["id"], revoked_by="cmo")
        report = {"id": f"r-{org}", "created_at": "2026-10-01T00:00:00+00:00"}
        store.save_report({**report, "pack": {"id": INBOX}, "dataset": {"name": "local"}})
        store.acknowledge_notice(f"notice:v1:person-{org}", version="v1", actor="rn-1")
    home = org or tenancy.DEFAULT_ORG
    # The command line's and the operator's writes: each names the row's organization.
    store.erase_note(rating["id"], erased_by="dpo", reason="request")
    store.erase_input(first, erased_by="dpo", reason="request")
    store.purge(0, source="command")  # `input.purged` (second, third), `note.erased` (third)
    hold = store.hold_automation(
        org=home, pack_id=None, pack_digest=None, reason="drill", set_by="ops"
    )
    store.lift_hold(hold["id"], lifted_by="ops", org=None)
    everyone = store.hold_automation(
        org=None, pack_id=None, pack_digest=None, reason="drill", set_by="ops"
    )
    store.lift_hold(everyone["id"], lifted_by="ops", org=None)  # the deployment's: None
    key = store.create_api_key(name="k", role="service", org=home, created_by="ops")
    store.rotate_api_key(key["id"], rotated_by="ops")
    store.revoke_api_key(key["id"], revoked_by="ops")
    client = store.register_client(
        name="c", role="viewer", org=home, created_by="ops", scopes="system/Task.rs", jwks=jwks()
    )
    store.revoke_client(client["client_id"], revoked_by="ops")
    if home == tenancy.DEFAULT_ORG:  # the owner's, made by `mendari access bootstrap` only
        store.bootstrap(email="owner@example.org", created_by="ops")
    else:
        store.create_organization(org=home, name="Clinic A", created_by="ops")
    member = store.add_membership(
        org=home, roles=["viewer"], created_by="ops", email="kim@example.org"
    )
    store.set_membership_roles(member["id"], ["reviewer"], org=home, changed_by="ops")
    store.remove_membership(member["id"], org=home, removed_by="ops")
    invitation = store.create_invitation(
        org=home, email="lee@example.org", roles=["viewer"], created_by="ops"
    )
    store.revoke_invitation(invitation["id"], org=home, revoked_by="ops")
    # A hidden evaluation set (LP-11e): made, a case added (the index key's epoch) and removed;
    # then its keys' lifecycle (LP-11f), the deployment's: the index rebuilt, the next key read
    # first and swapped in, and the set re-sealed under it.
    hidden = EvalsetStore(store, EVALSETS)
    made_set = hidden.create_set(home, pack, "a hidden set", created_by="ops")["set"]
    added = hidden.add_cases(made_set, [hand_written(f"A hidden case of {home}")])
    hidden.remove_case(made_set, added["cases"][0]["case"])
    hidden.reindex()
    store.evalset_ready(EvalsetKeys(EVALSETS.key, EVALSETS.index_key, (NEXT_EVALSET_KEY,)))
    swapped = EvalsetStore(
        store, EvalsetKeys(NEXT_EVALSET_KEY, EVALSETS.index_key, (EVALSETS.key,))
    )
    swapped.rotate(to=key_id(NEXT_EVALSET_KEY))
    # Its blind labelling (LP-11h), each event the set's organization's: cases drawn, two
    # labellers of `home` assigned, each reading and labelling (they disagree), a third
    # adjudicating, and the set measured.
    people = []
    for n in range(3):
        person = store.record_user(
            issuer="https://idp.example.org", subject=f"labeller-{home}-{n}", email=None, name=None
        )
        store.add_membership(org=home, roles=["labeller"], created_by="ops", user_id=person["id"])
        people.append(str(person["id"]))
    swapped.add_cases(made_set, [hand_written(f"A labelled case {n} of {home}") for n in range(2)])
    swapped.sample(made_set, cases=2)
    swapped.assign(made_set, people[:2])
    for labeller, destination in zip(people, ("nurse", "physician", "nurse"), strict=True):
        if labeller == people[2]:
            swapped.adjudicate(made_set, [labeller])
        while (served := swapped.next_case(labeller, org=home)) is not None:
            labels = {"destination": destination, "urgency": "routine"}
            swapped.record_label(served["assignment"], labeller, labels, org=home)
    swapped.measure(made_set, resamples=50)
    made.update(decisions=[first, second, third], home=home)
    return made


@pytest.mark.parametrize("org", [CLINIC, None], ids=["as-a-server", "as-the-command-line"])
def test_a_marked_database_names_its_row_s_organization_in_every_event(
    db_target: str, inbox_pack: Pack, org: str | None
) -> None:
    with keeping(db_target, bind_org=True) as store:
        assert store.binds_org
        made = every_writer(store, inbox_pack, org)
        events = chained(store)
        with store._db.session(exclusive=False) as db:
            owners = {row["id"]: row["org"] for row in db.all("SELECT id, org FROM decisions")}
            subjects = audit_module._subject_orgs(db)
        assert store.verify() == {"ok": True, "events": len(events), "head": events[-1]["hash"]}
    kinds = {event["event"] for event in events}
    assert kinds >= BOUND_WHEN_MARKED  # each kind of before was written here
    for event in events:
        kind, payload = event["event"], event["payload"]
        if kind in DEPLOYMENT_KINDS:
            assert "org" not in payload, event
        elif kind in DECISION_KINDS:
            assert payload["org"] == owners[event["decision_id"]] == made["home"], event
        elif kind in ADMINISTRATIVE_KINDS:
            assert payload["org"] == subjects[event["decision_id"]], event
        else:
            assert kind in PERSON_KINDS and payload["org"] == org, event
    held = [e for e in events if e["event"] == "automation.lifted"]
    assert [e["payload"]["org"] for e in held] == [made["home"], None]  # NULL: every one's


def test_an_unmarked_database_chains_the_kinds_of_before_as_it_did(
    db_target: str, inbox_pack: Pack
) -> None:
    with keeping(db_target) as store:
        assert not store.binds_org
        every_writer(store, inbox_pack, CLINIC)
        events = chained(store)
        assert store.verify()["ok"]
    before = [event for event in events if event["event"] in BOUND_WHEN_MARKED]
    assert {event["event"] for event in before} == BOUND_WHEN_MARKED
    assert [event for event in before if "org" in event["payload"]] == []
    # The kinds that named it already still do, marked or not.
    for event in events:
        if event["event"] not in BOUND_WHEN_MARKED | DEPLOYMENT_KINDS:
            assert "org" in event["payload"], event


def test_an_answer_is_the_same_whether_its_event_is_bound(db_target: str, inbox_pack: Pack) -> None:
    """The review and rating answers repeat what was chained; binding adds `org` to the chained
    copy only."""
    with keeping(db_target, bind_org=True) as store, acting(CLINIC):
        decision_id = Decision(inbox_pack, store=store)(message(URGENT)).id
        resolved = store.resolve_review(decision_id, final_label="nurse", reviewer="rn-1")
        rated = store.record_feedback(decision_id, rating="up")
        events = chained(store)
    assert "org" not in resolved and "org" not in rated
    assert {e["payload"].get("org") for e in events if e["event"] != "input_hash.epoch"} == {CLINIC}


def test_the_mark_binds_every_store_opened_after_it(db_target: str) -> None:
    with AuditStore(db_target) as fresh:
        assert not fresh.binds_org
    with AuditStore(db_target, bind_org=True, marks=False) as unmarking:
        assert not unmarking.binds_org  # a read-only tool's `marks=False` marks nothing
    with AuditStore(db_target, bind_org=True) as server:
        assert server.binds_org
    with AuditStore(db_target) as command, AuditStore(db_target, marks=False) as checking:
        assert command.binds_org and checking.binds_org
    with AuditStore(db_target, read_only=True) as reading:
        assert reading.binds_org  # reads the mark, writes nothing


def test_a_memberships_server_marks_its_database_and_a_claims_one_does_not(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    create_app(db_path=tmp_path / "claims.db")
    with AuditStore(tmp_path / "claims.db") as store:
        assert not store.binds_org
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    create_app(db_path=tmp_path / "members.db")
    with AuditStore(tmp_path / "members.db") as store:
        assert store.binds_org


# -- verify -----------------------------------------------------------------------------------

Make = Callable[[AuditStore, Pack], str]  # makes a row and returns its chain id


def _decision(store: AuditStore, pack: Pack) -> str:
    return Decision(pack, store=store)(message(URGENT)).id


def _key(store: AuditStore, _: Pack) -> str:
    return "key:" + store.create_api_key(name="k", role="service", org=CLINIC, created_by="t")["id"]


def _client(store: AuditStore, _: Pack) -> str:
    made = store.register_client(
        name="c", role="viewer", org=CLINIC, created_by="t", scopes="system/Task.rs", jwks=jwks()
    )
    return f"client:{made['client_id']}"


def _approval(store: AuditStore, _: Pack) -> str:
    made = store.approve_automation(
        pack_id=INBOX,
        pack_digest="d" * 64,
        risk_level=2,
        approver="cmo",
        reference="ticket-1",
        expires_at="2999-01-01T00:00:00+00:00",
    )
    return f"governance:{made['id']}"


def _registry_approval(store: AuditStore, _: Pack) -> str:
    made = store.approve_registry_pack(
        pack_id="someone/pulled", pack_digest="d" * 64, registry="x", approver="cmo", reference="r"
    )
    return f"registry_approval:{made['id']}"


def _hold(store: AuditStore, _: Pack) -> str:
    made = store.hold_automation(
        org=CLINIC, pack_id=None, pack_digest=None, reason="drill", set_by="ops"
    )
    return f"hold:{made['id']}"


def _report(store: AuditStore, _: Pack) -> str:
    report = {"id": "r1", "created_at": "2026-10-01T00:00:00+00:00"}
    store.save_report({**report, "pack": {"id": INBOX}, "dataset": {"name": "local"}})
    return "eval:r1"


def _organization(store: AuditStore, _: Pack) -> str:
    store.create_organization(org="clinic-b", name="Clinic B", created_by="t")
    return "org:clinic-b"


def _membership(store: AuditStore, _: Pack) -> str:
    made = store.add_membership(
        org=CLINIC, roles=["viewer"], created_by="t", email="kim@example.org"
    )
    return f"membership:{made['id']}"


def _invitation(store: AuditStore, _: Pack) -> str:
    made = store.create_invitation(
        org=CLINIC, email="lee@example.org", roles=["viewer"], created_by="t"
    )
    return f"invitation:{made['id']}"


def _request(store: AuditStore, _: Pack) -> str:
    person = store.record_user(
        issuer="https://idp.example.org", subject="s-1", email=None, name=None
    )
    return "access:" + store.request_access(user_id=person["id"], org=CLINIC)["id"]


def _evalset(store: AuditStore, pack: Pack) -> str:
    made = EvalsetStore(store, EVALSETS).create_set(CLINIC, pack, "a hidden set", created_by="t")
    return f"evalset:{made['set']}"


SUBJECTS: dict[str, tuple[str, str, Make]] = {  # table, its id column, what makes a row
    "decisions": ("decisions", "id", _decision),
    "api_keys": ("api_keys", "id", _key),
    "oauth_clients": ("oauth_clients", "client_id", _client),
    "governance_approvals": ("governance_approvals", "id", _approval),
    "registry_approvals": ("registry_approvals", "id", _registry_approval),
    "automation_holds": ("automation_holds", "id", _hold),
    "eval_reports": ("eval_reports", "id", _report),
    "organizations": ("organizations", "id", _organization),
    "memberships": ("memberships", "id", _membership),
    "invitations": ("invitations", "id", _invitation),
    "access_requests": ("access_requests", "id", _request),
    "evalset_sets": ("evalset_sets", "id", _evalset),
}


def test_every_subject_table_is_checked() -> None:
    assert {table for table, _, _ in SUBJECT_TABLES.values()} | {"decisions"} == set(SUBJECTS)


@pytest.mark.parametrize("change", ["moved", "removed"])
@pytest.mark.parametrize(
    ("subject", "marked"),
    [(subject, True) for subject in sorted(SUBJECTS)]
    # Claims mode (unmarked): every kind but a decision's named its organization already.
    + [(subject, False) for subject in sorted(SUBJECTS) if subject != "decisions"],
)
def test_verify_finds_a_row_moved_to_another_organization_or_removed(
    db_target: str, inbox_pack: Pack, subject: str, change: str, marked: bool
) -> None:
    table, column, make = SUBJECTS[subject]
    with keeping(db_target, bind_org=marked) as store:
        assert store.binds_org is marked
        with tenancy.acting_for(CLINIC):
            store.create_organization(org=CLINIC, name="Clinic A", created_by="t")
            chain_id = make(store, inbox_pack)
        row_id = chain_id.split(":", 1)[1] if ":" in chain_id else chain_id
        assert store.verify()["ok"]
        first = next(e["seq"] for e in chained(store) if e["decision_id"] == chain_id)
        if change == "removed" or table == "organizations":  # an organization is its own id
            if table == "decisions":  # what keeps a decision's input goes with it
                tamper(store, "DELETE FROM decision_inputs WHERE decision_id = ?", (row_id,))
            tamper(store, f"DELETE FROM {table} WHERE {column} = ?", (row_id,))
        else:
            tamper(store, f"UPDATE {table} SET org = 'clinic-z' WHERE {column} = ?", (row_id,))
        result = store.verify()
    if table == "decisions" and change == "removed":
        # Its record is gone, which `decision.created` finds first: a hash-chain failure.
        assert result == {"ok": False, "events": result["events"], "broken_at": first}
        return
    assert result == {
        "ok": False,
        "events": result["events"],
        "broken_at": first,
        "reason": "organization",
    }


def test_an_unbound_older_decision_moved_by_sql_still_verifies(
    db_target: str, inbox_pack: Pack
) -> None:
    """The residual (threat model T1): events chained before the mark name no organization, so
    `verify` cannot tell where their row belonged."""
    with keeping(db_target) as store:
        with tenancy.acting_for(CLINIC):
            decision_id = Decision(inbox_pack, store=store)(message(URGENT)).id
        tamper(store, "UPDATE decisions SET org = 'clinic-z' WHERE id = ?", (decision_id,))
    with keeping(db_target, bind_org=True) as store:
        assert store.verify()["ok"]
        with tenancy.acting_for("clinic-z"):  # bound from now on: its row's, as moved
            store.resolve_review(decision_id, final_label="nurse", reviewer="rn-1")
        assert chained(store)[-1]["payload"]["org"] == "clinic-z"
        assert store.verify()["ok"]


LATER = (
    "review.resolved",
    "outcome.recorded",
    "feedback.recorded",
    "pas.submitted",
    "input.read",
    "input.purged",
    "input.erased",
    "note.erased",
)


@pytest.mark.parametrize("kind", LATER)
def test_verify_finds_a_decision_moved_after_a_bound_later_event(
    db_target: str, inbox_pack: Pack, kind: str
) -> None:
    """The upgrade path: a decision made before the mark, whose first event names no
    organization, is checked from its first event after it, of each kind on a decision."""
    with keeping(db_target) as store, tenancy.acting_for(CLINIC):  # before the mark
        decision_id = Decision(inbox_pack, store=store)(message(URGENT)).id
        if kind == "outcome.recorded":
            store.resolve_review(decision_id, final_label="nurse", reviewer="rn-1")
        if kind == "note.erased":  # its only bound event: the note's erasure
            rated = store.record_feedback(decision_id, rating="down", note="a note")
    writes: dict[str, Callable[[AuditStore], object]] = {
        "review.resolved": lambda s: s.resolve_review(
            decision_id, final_label="nurse", reviewer="rn-1"
        ),
        "outcome.recorded": lambda s: s.record_outcome(
            decision_id, final_label="nurse", source="ehr", recorded_by="ehr-feed"
        ),
        "feedback.recorded": lambda s: s.record_feedback(decision_id, rating="up"),
        "pas.submitted": lambda s: s.record_event(
            decision_id, "pas.submitted", {"outcome": "queued"}
        ),
        "input.read": lambda s: s.get(decision_id, reader="rn-7"),
        "input.purged": lambda s: s.purge(0, source="command"),
        "input.erased": lambda s: s.erase_input(decision_id, erased_by="dpo", reason="request"),
        "note.erased": lambda s: s.erase_note(rated["id"], erased_by="dpo", reason="request"),
    }
    with keeping(db_target, bind_org=True) as store:
        with tenancy.acting_for(CLINIC):
            writes[kind](store)
        assert store.verify()["ok"]
        first = next(
            e for e in chained(store) if e["decision_id"] == decision_id and "org" in e["payload"]
        )
        assert (first["event"], first["payload"]["org"]) == (kind, CLINIC)
        tamper(store, "UPDATE decisions SET org = 'clinic-z' WHERE id = ?", (decision_id,))
        result = store.verify()
    assert result == {
        "ok": False,
        "events": result["events"],
        "broken_at": first["seq"],
        "reason": "organization",
    }


def test_a_store_opened_before_the_mark_chains_as_before_until_it_is_opened_again(
    db_target: str, inbox_pack: Pack
) -> None:
    """A store reads the mark when it opens: one serving while another marks the database (a
    deployment's previous instance) keeps chaining the kinds of before unbound, even where
    mode `off` reads the database's marks again, so a write never finds the mark half-read."""
    with AuditStore(db_target, phi=PhiPolicy(mode="off")) as warm:
        decision_id = Decision(inbox_pack, store=warm)(message(URGENT)).id
        with AuditStore(db_target, bind_org=True) as marking:
            assert marking.binds_org
        warm.refuse_plaintext()  # under `off`, reads the PHI marks again
        assert not warm.binds_org
        warm.resolve_review(decision_id, final_label="nurse", reviewer="rn-1", note="a note")
        assert "org" not in chained(warm)[-1]["payload"]
        assert warm.verify()["ok"]
    with AuditStore(db_target, phi=PhiPolicy(mode="off")) as reopened:
        assert reopened.binds_org


def test_a_decision_from_before_tenancy_is_bound_as_default(
    db_target: str, inbox_pack: Pack
) -> None:
    """decisions.org is NULL on rows from before v0.6: every writer binds it as `verify` reads
    it, `default` (`_org_of`), never None."""
    with keeping(db_target) as store:
        first = Decision(inbox_pack, store=store)(message(URGENT)).id
        Decision(inbox_pack, store=store)(message(URGENT))
        tamper(store, "UPDATE decisions SET org = NULL")
    with keeping(db_target, bind_org=True) as store, acting(tenancy.DEFAULT_ORG):
        store.resolve_review(first, final_label="nurse", reviewer="rn-1", note="a note")
        rating = store.record_feedback(first, rating="down", reporter="rn-2", note="a note")
        store.record_outcome(first, final_label="nurse", source="ehr", recorded_by="ehr-feed")
        store.record_event(first, "pas.submitted", {"outcome": "queued"})
        store.get(first, reader="rn-7")  # `input.read`
        store.erase_note(rating["id"], erased_by="dpo", reason="request")
        store.erase_input(first, erased_by="dpo", reason="request")
        store.purge(0, source="command")  # `input.purged` (the second), `note.erased`
        events = chained(store)
        assert store.verify()["ok"]
    bound = [e for e in events if e["event"] in DECISION_KINDS - {"decision.created"}]
    assert {e["event"] for e in bound} == DECISION_KINDS - {"decision.created"}
    assert {e["payload"]["org"] for e in bound} == {tenancy.DEFAULT_ORG}


def test_the_empty_organization_is_bound_as_itself(db_target: str) -> None:
    """ "" is an organization (core/tenancy.py): bound as it is, never as `default`."""
    with keeping(db_target, bind_org=True) as store:
        key = store.create_api_key(name="k", role="service", org="", created_by="ops")
        store.rotate_api_key(key["id"], rotated_by="ops")
        store.revoke_api_key(key["id"], revoked_by="ops")
        events = chained(store)
        assert store.verify()["ok"]
    assert {e["payload"]["org"] for e in events if e["event"].startswith("key.")} == {""}


def test_a_batch_over_several_organizations_names_each_row_s_own(
    db_target: str, inbox_pack: Pack
) -> None:
    """The scheduled purge acts for no organization, so one batch spans them all: each
    `input.purged` and `note.erased` names its own decision's organization, as does each
    `input.read` of a read over several."""
    with keeping(db_target, bind_org=True) as store:
        owners: dict[str, str] = {}
        for org in (CLINIC, "clinic-b"):
            with tenancy.acting_for(org):
                owners[Decision(inbox_pack, store=store)(message(URGENT)).id] = org
        store.pending_reviews(reader="rn-7")  # `input.read`, both in one batch
        for decision_id, org in owners.items():
            with tenancy.acting_for(org):
                store.resolve_review(decision_id, final_label="nurse", reviewer="rn-1", note="n")
        store.purge(0, source="schedule")  # `input.purged`, `note.erased`: both in one batch
        batched = [
            e for e in chained(store) if e["event"] in {"input.read", "input.purged", "note.erased"}
        ]
        assert store.verify()["ok"]
    assert len(batched) == 6
    assert all(e["payload"]["org"] == owners[e["decision_id"]] for e in batched), batched


def previous_verify(store: AuditStore) -> bool:
    """`verify` as the release before LP-12g had it: the hashes and each record's digest. Its
    formulas are copied here, not imported, so a change to the hashing fails this test."""
    with store._db.session(exclusive=False) as db:
        events = db.all("SELECT * FROM audit_events ORDER BY seq")
        records = {
            row["id"]: json.loads(row["record"])
            for row in db.all("SELECT id, record FROM decisions")
        }
    prev = "0" * 64
    for event in events:
        fields = (prev, event["decision_id"], event["event"], event["payload"], event["created_at"])
        expected = hashlib.sha256("\x1f".join(fields).encode()).hexdigest()
        if event["prev_hash"] != prev or event["hash"] != expected:
            return False
        if event["event"] == "decision.created":
            record = records.get(event["decision_id"])
            digest = json.loads(event["payload"])["record_sha256"]
            canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
            if record is None or digest != hashlib.sha256(canonical.encode()).hexdigest():
                return False
        prev = event["hash"]
    return True


def test_the_previous_verify_passes_a_bound_chain(db_target: str, inbox_pack: Pack) -> None:
    """A rollback to the release before keeps verifying: what binding adds is in the hashed
    payload, which that release recomputes as stored, and the mark is a settings row it never
    reads."""
    with keeping(db_target, bind_org=True) as store:
        every_writer(store, inbox_pack, CLINIC)
        assert previous_verify(store) and store.verify()["ok"]


def test_verify_reads_an_older_database_as_it_is(tmp_path: Any, inbox_pack: Pack) -> None:
    """A database from before the subject tables, the decisions' organization and the mark,
    read as it is: nothing to check against, and nothing fails for it."""
    import sqlite3
    from contextlib import closing

    db = tmp_path / "older.db"
    with AuditStore(db, bind_org=True) as store, tenancy.acting_for(CLINIC):
        Decision(inbox_pack, store=store)(message(URGENT))  # bound: names clinic-a
    with closing(sqlite3.connect(db)) as connection, connection:
        for table in ("settings", *(table for table, _, _ in SUBJECT_TABLES.values())):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("ALTER TABLE decisions DROP COLUMN org")
    with AuditStore(db, read_only=True) as reading:
        assert {"settings", "decisions.org"} <= set(reading.unmigrated)
        assert not reading.binds_org
        result = reading.verify()
    assert result["ok"] and result["events"] == 1  # the decision


def test_a_kind_from_a_later_release_is_left_unchecked(db_target: str) -> None:
    """A rollback meets kinds this release does not know: they name an organization, and there
    is no row here to check it against, so `verify` passes them."""
    with AuditStore(db_target, bind_org=True) as store:
        with store._db.session(exclusive=True) as db:
            store._append(db, "store:s1", "store.created", {"id": "s1", "org": "anyone"})
        assert store.verify()["ok"]


# -- writers ------------------------------------------------------------------------------------


def test_record_event_refuses_a_payload_naming_an_organization(
    db_target: str, inbox_pack: Pack
) -> None:
    with keeping(db_target) as store:
        decision_id = Decision(inbox_pack, store=store)(message(URGENT)).id
        with pytest.raises(ValueError, match="organization"):
            store.record_event(decision_id, "pas.submitted", {"org": "clinic-z"})


def test_a_writer_that_leaves_out_the_organization_is_refused(db_target: str) -> None:
    """New kinds name it always; the kinds of before, on a marked database; the deployment's,
    never. Refused before anything is chained."""
    with AuditStore(db_target) as unmarked, unmarked._db.session(exclusive=True) as db:
        with pytest.raises(ValueError, match="names its organization"):
            unmarked._append(db, "store:s1", "store.created", {"id": "s1"})
        unmarked._append(db, "key:k1", "key.revoked", {"id": "k1"})  # as before: unmarked
        unmarked._append(db, "retention", "retention.purged", {"inputs": 0})
    with AuditStore(db_target, bind_org=True) as marked:
        with (
            marked._db.session(exclusive=True) as db,
            pytest.raises(ValueError, match="names its organization"),
        ):
            marked._append(db, "key:k1", "key.revoked", {"id": "k1"})
        assert len(chained(marked)) == 2


def test_every_kind_is_in_exactly_one_class() -> None:
    classes = [DECISION_KINDS, set(ADMINISTRATIVE_KINDS), PERSON_KINDS, DEPLOYMENT_KINDS]
    assert sum(len(kinds) for kinds in classes) == len(set().union(*classes))  # disjoint
    assert set().union(*classes) == recorded_kinds()
    assert set(audit_fhir._EVENTS) == DECISION_KINDS
    assert set().union(*classes[1:]) == NOT_ON_A_DECISION
    assert DECISION_KINDS | set(ADMINISTRATIVE_KINDS) >= BOUND_WHEN_MARKED
    assert set(ADMINISTRATIVE_KINDS.values()) == set(SUBJECT_TABLES)


def test_a_subject_s_history_is_its_row_s_organization_s_and_a_deployment_hold_no_one_s(
    db_target: str,
) -> None:
    """`subject_history` (LP-12h) reads `SUBJECT_TABLES` as `verify` does: a hold set for every
    organization (NULL) is the deployment's, never `default`'s."""
    with AuditStore(db_target, bind_org=True) as store:
        mine = store.hold_automation(
            org=CLINIC, pack_id=None, pack_digest=None, reason="r", set_by="o"
        )
        every = store.hold_automation(
            org=None, pack_id=None, pack_digest=None, reason="r", set_by="o"
        )
        assert store.subject_history("hold", mine["id"], CLINIC)["org"] == CLINIC  # type: ignore[index]
        assert store.subject_history("hold", mine["id"], "clinic-z") is None
        assert store.subject_history("hold", every["id"], tenancy.DEFAULT_ORG) is None
        operators = store.subject_history("hold", every["id"], None)  # the command line's
        assert operators is not None and operators["org"] is None
        assert [e["event"] for e in operators["events"]] == ["automation.held"]


def test_mendari_audit_verify_passes_after_membership_invitation_and_key_events(
    db_target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """LP-12's acceptance: the operator's `mendari audit verify` on a database a memberships-mode
    server marked, after the owner's bootstrap, an organization, its memberships (made, bound,
    changed, removed), invitations (made, accepted, revoked), a request to join, and keys and a
    client (made, rotated, revoked), each event naming its organization."""
    from mendari.cli import main

    with AuditStore(db_target, bind_org=True) as store:
        store.bootstrap(email="owner@example.org", created_by="ops")
        store.create_organization(org=CLINIC, name="Clinic A", created_by="ops")
        lead = store.record_user(issuer="https://idp.example.org", subject="lead", email=None,
                                 name=None)  # fmt: skip
        member = store.add_membership(
            org=CLINIC, roles=["admin"], created_by="ops", user_id=lead["id"]
        )
        pending = store.add_membership(
            org=CLINIC, roles=["viewer"], created_by="ops", email="kim@example.org"
        )
        store.set_membership_roles(member["id"], ["admin", "reviewer"], org=CLINIC,
                                   changed_by="ops")  # fmt: skip
        bound = store.add_membership(
            org=CLINIC, roles=["viewer"], created_by="ops", email="ann@example.org"
        )
        ann = store.record_user(
            issuer="https://idp.example.org", subject="ann", email="ann@example.org", name=None
        )
        assert [m["id"] for m in store.bind_pending(ann["id"], "ann@example.org")] == [bound["id"]]
        store.remove_membership(pending["id"], org=CLINIC, removed_by="ops")
        joined = store.create_invitation(
            org=CLINIC, email="lee@example.org", roles=["viewer"], created_by="ops"
        )
        lee = store.record_user(issuer="https://idp.example.org", subject="lee",
                                email="lee@example.org", name=None)  # fmt: skip
        store.accept_invitation(joined["token"], user_id=lee["id"],
                                verified_email="lee@example.org")  # fmt: skip
        dropped = store.create_invitation(
            org=CLINIC, email="max@example.org", roles=["viewer"], created_by="ops"
        )
        store.revoke_invitation(dropped["id"], org=CLINIC, revoked_by="ops")
        asker = store.record_user(issuer="https://idp.example.org", subject="asker", email=None,
                                  name=None)  # fmt: skip
        store.request_access(user_id=asker["id"], org=CLINIC)
        key = store.create_api_key(name="k", role="service", org=CLINIC, created_by="ops")
        store.rotate_api_key(key["id"], rotated_by="ops")
        store.revoke_api_key(key["id"], revoked_by="ops")
        client = store.register_client(
            name="c", role="viewer", org=CLINIC, created_by="ops", scopes="system/Task.rs",
            jwks=jwks(),
        )  # fmt: skip
        store.replace_client_keys(client["client_id"], CLINIC, replaced_by="ops", jwks=jwks())
        store.revoke_client(client["client_id"], revoked_by="ops")
        kinds = {event["event"] for event in chained(store)}
    assert {"membership.created", "membership.bound", "membership.roles_changed",
            "membership.removed", "invitation.created", "invitation.accepted",
            "invitation.revoked", "access.requested", "key.created", "key.rotated",
            "key.revoked", "client.registered", "client.keys_replaced",
            "client.revoked"} <= kinds  # fmt: skip
    capsys.readouterr()
    assert main(["audit", "verify", "--db", db_target]) == 0
    assert '"ok": true' in capsys.readouterr().out
