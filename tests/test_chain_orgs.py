# SPDX-License-Identifier: Apache-2.0
"""The organization in the chain (LP-12g, ADR-0008).

A database a memberships-mode server opened is marked (`org_bound`): from then on every event
names the organization of the row it is about, whichever store writes it (the command line's
too), and `verify` checks that name against the row, so a row moved to another organization, or
removed, breaks it. An unmarked database chains the kinds of before as it did, and the previous
release's `verify` still passes a bound chain. On SQLite and Postgres (`db_target`)."""

from __future__ import annotations

import ast
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
from mendari.core.phi import PhiPolicy
from mendari.server.app import create_app
from mendari.standards import audit_fhir
from tests.conftest import ROOT, message
from tests.test_audit_fhir import NOT_ON_A_DECISION

KEY = os.urandom(32)
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


SUBJECTS: dict[str, tuple[str, str, Make]] = {  # table, its id column, what makes a row
    "decisions": ("decisions", "id", _decision),
    "api_keys": ("api_keys", "id", _key),
    "oauth_clients": ("oauth_clients", "client_id", _client),
    "governance_approvals": ("governance_approvals", "id", _approval),
    "automation_holds": ("automation_holds", "id", _hold),
    "eval_reports": ("eval_reports", "id", _report),
    "organizations": ("organizations", "id", _organization),
    "memberships": ("memberships", "id", _membership),
    "invitations": ("invitations", "id", _invitation),
    "access_requests": ("access_requests", "id", _request),
}


def test_every_subject_table_is_checked() -> None:
    assert {table for table, _, _ in SUBJECT_TABLES.values()} | {"decisions"} == set(SUBJECTS)


@pytest.mark.parametrize("change", ["moved", "removed"])
@pytest.mark.parametrize("subject", sorted(SUBJECTS))
def test_verify_finds_a_row_moved_to_another_organization_or_removed(
    db_target: str, inbox_pack: Pack, subject: str, change: str
) -> None:
    table, column, make = SUBJECTS[subject]
    with keeping(db_target, bind_org=True) as store:
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


def previous_verify(store: AuditStore) -> bool:
    """`verify` as the release before LP-12g had it: the hashes and each record's digest."""
    with store._db.session(exclusive=False) as db:
        events = db.all("SELECT * FROM audit_events ORDER BY seq")
        records = {
            row["id"]: json.loads(row["record"])
            for row in db.all("SELECT id, record FROM decisions")
        }
    prev = audit_module.GENESIS
    for event in events:
        expected = audit_module._chain_hash(
            prev, event["decision_id"], event["event"], event["payload"], event["created_at"]
        )
        if event["prev_hash"] != prev or event["hash"] != expected:
            return False
        if event["event"] == "decision.created":
            record = records.get(event["decision_id"])
            digest = json.loads(event["payload"])["record_sha256"]
            if record is None or digest != audit_module._sha(record):
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


def recorded_kinds() -> set[str]:
    """Every event kind the code chains, by its literal at each call (test_audit_fhir.py)."""
    recorded: set[str] = set()
    for path in (ROOT / "src" / "mendari").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            position = {"_append": 2, "_append_many": 1, "record_event": 1}.get(node.func.attr)
            if position is None or len(node.args) <= position:
                continue
            event = node.args[position]
            if isinstance(event, ast.Name) and event.id == "event":
                continue  # `record_event` passing its caller's literal on
            assert isinstance(event, ast.Constant), f"{path}: an event name that is not a literal"
            recorded.add(str(event.value))
    return recorded


def test_every_kind_is_in_exactly_one_class() -> None:
    classes = [DECISION_KINDS, set(ADMINISTRATIVE_KINDS), PERSON_KINDS, DEPLOYMENT_KINDS]
    assert sum(len(kinds) for kinds in classes) == len(set().union(*classes))  # disjoint
    assert set().union(*classes) == recorded_kinds()
    assert set(audit_fhir._EVENTS) == DECISION_KINDS
    assert set().union(*classes[1:]) == NOT_ON_A_DECISION
    assert DECISION_KINDS | set(ADMINISTRATIVE_KINDS) >= BOUND_WHEN_MARKED
    assert set(ADMINISTRATIVE_KINDS.values()) == set(SUBJECT_TABLES)
