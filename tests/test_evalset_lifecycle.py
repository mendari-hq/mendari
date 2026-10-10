# SPDX-License-Identifier: Apache-2.0
"""The lifecycle of hidden evaluation sets (LP-11i, `core/evalsets.py`, ADR-0011): freezing, the
in-process grading read, the rotation rule and retirement. No route reads a set yet (LP-11l
calls `grade_read`), so the grading read is tested here, at the store.

- Freezing fixes a draft set's content: its hash covers the header, the rotation rule and every
  case by id (whatever order its rows come in), and the chain holds only its salted commitment;
  the unsalted hash never leaves the sealed freeze record. Freezing is refused until every
  drawn case is settled (an undrawn one is removed with `drop_unsampled`, each removal
  chained) and without both limits.
- Once frozen, every write refuses the set, writing and chaining nothing; a rotation re-seals
  it, changing ciphertext only. An edit behind the store's back (a weight re-sealed, a status,
  a limit, the freeze time or the freeze record changed) breaks `audit verify --evalsets` at the
  set's `evalset.frozen`, fails `rotate --check`, and makes the grading read and retirement
  refuse, naming no case. A state moved back breaks verification at the event it contradicts,
  and so does a draft's write, an activation or a read chained where the lifecycle allows none;
  a retired set grades nothing more, whatever its row says. A ring lacking a key this database
  knows is refused by that key's id, never as a changed set.
- The grading read serves a live grading key of the set's organization only, while the rotation
  rule holds (runs, and age by an injected clock), never a retired set; its first read activates
  a frozen set, and each read is chained before `run` receives the cases, in one transaction
  with the rule's check, so concurrent reads never exceed the runs allowed. Only what `run`
  returns comes back; nothing is stored.
- The key holder's `freeze`, `show` and `retire`, as the runbook writes them, a second lifecycle
  beside the first, and `mendari audit verify --evalsets` holding throughout.
On SQLite and Postgres (`db_target`); keys are made per test, cases are hand-written synthetic
text."""

from __future__ import annotations

import base64
import json
import re
import shlex
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from mendari import AuditStore, Pack
from mendari.cli import main
from mendari.core import evalsets as evalsets_module
from mendari.core.evalset_keys import EvalsetKeys, evalset_keys
from mendari.core.evalsets import (
    CONTENT_FORMAT,
    NO_ASSIGNMENT,
    EvalsetStore,
    content_hash,
    freeze_commitment,
    frozen_holds,
)
from mendari.core.phi import key_id
from mendari.core.registry import resolve_pack
from mendari.errors import MendariError
from mendari.evals.harness import Dataset
from mendari.standards import audit_fhir, fhir_structure
from tests.conftest import ROOT, evalset_keys_for_test, evalset_on, hand_written
from tests.test_evalset_labelling import (
    LABEL_WORD,
    cases,
    events,
    label_all,
    labellers,
    number_of,
    rows,
    split,
)
from tests.test_evalset_store import (
    CLINIC,
    INBOX,
    MARKS,
    a_set,
    cases_file,
    chained,
    served,
    tamper,
)

DIGEST = "sha256:" + "d" * 64  # a pack digest a grading read is for
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
CASE_HASH = re.compile(r"^hmac-sha256:[0-9a-f]{16}:[0-9a-f]{64}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
TABLES = (
    "evalset_sets",
    "evalset_cases",
    "evalset_index",
    "evalset_assignments",
    "evalset_labels",
    "api_keys",
    "audit_events",
    "settings",
)
# The keys each lifecycle event's payload holds.
PAYLOADS = {
    "evalset.frozen": {"set", "org", "commitment", "n", "max_runs", "max_age_days"},
    "evalset.activated": {"set", "org"},
    "evalset.graded": {"set", "org", "commitment", "grader", "pack_digest", "n", "run"},
    "evalset.retired": {"set", "org", "commitment"},
}
LIFECYCLE = tuple(PAYLOADS)
CHANGED = "does not hold what it was frozen with"
T0 = datetime(2026, 10, 1, 12, tzinfo=UTC)


# -- the world ------------------------------------------------------------------------------------


@dataclass
class World:
    store: AuditStore
    hidden: EvalsetStore
    keys: EvalsetKeys
    pack: Pack
    set_id: str
    now: list[datetime]  # the store's clock, which a test moves
    users: list[str] = field(default_factory=list)  # the organization's labellers, once made

    def grader(self, org: str = CLINIC) -> str:
        """A new grading key of `org`, as the chain names it (`key:<id>`)."""
        made = self.store.create_grading_key(org=org, created_by="ops", reason="the tests")
        return f"key:{made['id']}"


@pytest.fixture
def world(db_target: str, inbox_pack: Pack) -> Iterator[World]:
    keys = evalset_keys_for_test()
    now = [T0]
    with AuditStore(db_target) as store:
        hidden = EvalsetStore(store, keys, clock=lambda: now[0])
        yield World(store, hidden, keys, inbox_pack, a_set(hidden, inbox_pack), now)


def settled(world: World, count: int = 3) -> list[str]:
    """`count` cases drawn and labelled blind by two labellers (`split`: case 0 agreed, case 1
    resolved by a third, case 2 a three-way split, removed), so every drawn case is settled.
    The labellers' user ids (the same three for every set of a test)."""
    hidden, set_id = world.hidden, world.set_id
    hidden.add_cases(set_id, cases(count))
    hidden.sample(set_id, per_stratum=count)
    users = world.users = world.users or labellers(world.store, 3)
    hidden.assign(set_id, users[:2])
    label_all(hidden, users[:2], split)
    if hidden.progress(set_id)["statuses"]["disputed"]:
        hidden.adjudicate(set_id, users[2:])
        label_all(hidden, users[2:], lambda number, _: split(number, 2))
    for row in rows(world.store, "evalset_cases", set_id=set_id, status="unresolved"):
        hidden.remove_case(set_id, row["case_hash"])
    return users


def frozen(world: World, count: int = 3, *, max_runs: int = 5, max_age_days: int = 30) -> None:
    settled(world, count)
    world.hidden.freeze(world.set_id, max_runs=max_runs, max_age_days=max_age_days, resamples=50)


def dump(store: AuditStore, *, without: Sequence[str] = ()) -> str:
    with store._db.session(exclusive=False) as db:
        return json.dumps(
            {
                table: db.all(f"SELECT * FROM {table} ORDER BY 1")
                for table in TABLES
                if table not in without
            },
            default=str,
        )


def record_of(world: World, set_id: str | None = None) -> dict[str, Any]:
    """A set's freeze record, opened with the key holder's keys."""
    set_id = set_id or world.set_id
    (row,) = rows(world.store, "evalset_sets", id=set_id)
    return dict(world.keys.open("freeze", row["freeze_record"], set_id)[0])


def recomputed(world: World, keys: EvalsetKeys | None = None) -> tuple[bool, list[dict[str, Any]]]:
    """Whether the set's rows still give its chained commitment (`frozen_holds`)."""
    with world.store._db.session(exclusive=False) as db:
        head = db.one(
            f"SELECT {evalsets_module._FROZEN_COLUMNS} FROM evalset_sets WHERE id = ?",
            (world.set_id,),
        )
        found, labels = evalsets_module._frozen_rows(db, world.set_id)
    (event,) = events(world.store, "evalset.frozen")
    assert head is not None
    return frozen_holds(
        keys or world.keys, world.set_id, head, found, labels, event["payload"]["commitment"]
    )


def count(dataset: Dataset) -> int:
    """A grading run that answers how many cases it was given."""
    return len(dataset.records)


def read(world: World, grader: str, run: Callable[[Dataset], Any] = count) -> Any:
    return world.hidden.grade_read(world.set_id, grader, DIGEST, run)


# -- freezing -------------------------------------------------------------------------------------


def test_freezing_fixes_the_content_hash(world: World) -> None:
    """The content is the header, the rotation rule and every case by id, its final labels (a
    resolved case's majority) and attributes, never the set's name: hashed the same whatever
    order the rows come in, and the same once recomputed; the commitment is the hash salted, a
    salt changing the commitment only; the chain holds the commitment, the count and the
    limits."""
    settled(world)
    made = world.hidden.freeze(world.set_id, max_runs=4, max_age_days=21, resamples=50)
    (row,) = rows(world.store, "evalset_sets", id=world.set_id)
    assert (row["state"], row["max_runs"], row["max_age_days"]) == ("frozen", 4, 21)
    assert row["frozen_at"] == made["frozen_at"] == T0.isoformat(timespec="milliseconds")
    record = record_of(world)
    salt = base64.b64decode(record["salt"])
    assert len(salt) == 32 and SHA256.fullmatch(record["content_hash"])
    commitment = freeze_commitment(salt, record["content_hash"])
    assert made["commitment"] == row["commitment"] == commitment
    assert commitment != record["content_hash"]
    (event,) = events(world.store, "evalset.frozen")
    assert event["payload"] == {
        "set": world.set_id,
        "org": CLINIC,
        "commitment": commitment,
        "n": 2,
        "max_runs": 4,
        "max_age_days": 21,
    }
    # The same content however its rows are ordered, and recomputed from the database.
    with world.store._db.session(exclusive=False) as db:
        found, labels = evalsets_module._frozen_rows(db, world.set_id)
    header, _ = world.keys.open("header", row["header"], world.set_id)
    limits = (4, 21, row["frozen_at"])

    def content(order: Callable[[list[Any]], list[Any]]) -> dict[str, Any]:
        made, _ = evalsets_module._frozen_content(
            world.keys, world.set_id, header, limits, order(found), order(labels),
            record["derived"],
        )  # fmt: skip
        return made

    forward, backward = content(list), content(lambda items: list(reversed(items)))
    assert forward == backward and content_hash(forward) == record["content_hash"]
    assert recomputed(world)[0]
    assert forward["format"] == CONTENT_FORMAT
    assert set(forward["header"]) == {
        "pack", "questions", "derived", "critical", "max_runs", "max_age_days", "frozen_at",
    }  # fmt: skip
    assert MARKS["name"] not in json.dumps(forward)  # the set's name is not content
    ids = [case["id"] for case in forward["cases"]]
    assert ids == sorted(ids) == sorted(str(case["id"]) for case in found)
    for case in forward["cases"]:
        assert set(case) == {
            "id", "input", "labels", "status", "source", "stratum", "weight", "language",
            "author", "template",
        }  # fmt: skip
        assert case["labels"] == {"destination": "nurse", "urgency": "routine"}  # 1's majority
        assert (case["source"], case["template"], case["weight"]) == ("hand_written", None, 1.0)
    assert {number_of(case["input"]) for case in forward["cases"]} == {0, 1}
    assert {(number_of(case["input"]), case["status"]) for case in forward["cases"]} == {
        (0, "agreed"),
        (1, "resolved"),
    }
    # Another salt: another commitment, the same content hash.
    other = freeze_commitment(bytes(32), record["content_hash"])
    assert other != commitment and content_hash(backward) == record["content_hash"]


def test_freezing_is_refused_until_every_drawn_case_is_settled_and_both_limits_are_given(
    world: World,
) -> None:
    hidden, set_id = world.hidden, world.set_id

    def refused(match: str, **given: Any) -> None:
        before = dump(world.store)
        limits = {"max_runs": 3, "max_age_days": 30, "resamples": 50, **given}
        with pytest.raises(MendariError, match=match):
            hidden.freeze(set_id, **limits)
        assert dump(world.store) == before

    hidden.add_cases(set_id, cases(3))
    refused("has 3 case\\(s\\) not drawn: draw and label them")
    refused("has no drawn case to freeze", drop_unsampled=True)
    for wrong in (0, -1, True, 3.0, "3", 10**6):
        refused("--max-runs is a whole number from 1 to 100000", max_runs=wrong)
    refused("--max-age-days is a whole number from 1 to 3650", max_age_days=0)
    refused("--max-age-days is a whole number from 1 to 3650", max_age_days=3651)
    hidden.sample(set_id, per_stratum=3)
    hidden.add_cases(set_id, cases(1, start=3))  # added after the draw: never drawn
    users = labellers(world.store, 3)
    hidden.assign(set_id, users[:2])
    refused("3 unlabelled", drop_unsampled=True)
    label_all(hidden, users[:1], split)
    refused("3 unlabelled", drop_unsampled=True)  # one label a case settles none
    label_all(hidden, users[1:2], lambda number, _: split(number, 1))
    statuses = {row["status"] for row in rows(world.store, "evalset_cases", set_id=set_id)}
    assert statuses == {"draft", "agreed", "disputed"}
    refused("drawn cases not settled \\(2 disputed\\)", drop_unsampled=True)
    hidden.adjudicate(set_id, users[2:])
    label_all(hidden, users[2:], lambda number, _: split(number, 2))
    refused("\\(1 unresolved\\).*remove an unresolved one", drop_unsampled=True)
    (unresolved,) = rows(world.store, "evalset_cases", set_id=set_id, status="unresolved")
    hidden.remove_case(set_id, unresolved["case_hash"])
    refused("has 1 case\\(s\\) not drawn")  # without --drop-unsampled
    (undrawn,) = rows(world.store, "evalset_cases", set_id=set_id, status="draft")
    removals = len(events(world.store, "evalset.case_removed"))
    made = hidden.freeze(set_id, max_runs=3, max_age_days=30, drop_unsampled=True, resamples=50)
    assert (made["n"], made["dropped"], made["state"]) == (2, 1, "frozen")
    dropped = events(world.store, "evalset.case_removed")[removals:]
    assert [e["payload"] for e in dropped] == [
        {"set": set_id, "org": CLINIC, "case": undrawn["case_hash"]}
    ]
    assert rows(world.store, "evalset_cases", id=undrawn["id"]) == []
    with world.store._db.session(exclusive=False) as db:
        assert db.all("SELECT hash FROM evalset_index WHERE case_id = ?", (undrawn["id"],)) == []
    kinds = [event["event"] for event in chained(world.store)]
    assert kinds[-2:] == ["evalset.case_removed", "evalset.frozen"]  # one transaction
    assert world.store.verify(evalsets=world.keys)["ok"]


def test_any_change_after_freeze_is_refused(world: World) -> None:
    """Adding, removing, drawing, assigning, adjudicating, labelling, measuring and freezing
    again are each refused once a set is frozen, active or retired: nothing written, nothing
    chained. A labeller is served nothing from it."""
    users = settled(world)
    hidden, set_id, store = world.hidden, world.set_id, world.store
    hidden.freeze(set_id, max_runs=3, max_age_days=30, resamples=50)
    (case,) = rows(store, "evalset_cases", set_id=set_id)[:1]
    membership = store.membership_in_force(users[0], CLINIC)
    assert membership is not None
    # An open assignment on the frozen set, which no store call leaves: its label is refused.
    assignment = str(uuid.uuid4())
    tamper(
        store,
        "INSERT INTO evalset_assignments (id, org, set_id, case_id, user_id, membership_id, "
        "round, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (assignment, CLINIC, set_id, case["id"], users[0], membership["id"], "first", "2026"),
    )
    grader = world.grader()
    for state in ("frozen", "active", "retired"):
        if state == "active":
            read(world, grader)
        if state == "retired":
            hidden.retire(set_id)
        before = dump(store)
        for write in (
            lambda: hidden.add_cases(set_id, [hand_written("A new case about a sore ear")]),
            lambda: hidden.remove_case(set_id, case["case_hash"]),
            lambda: hidden.sample(set_id, cases=1),
            lambda: hidden.assign(set_id, users[:2]),
            lambda: hidden.adjudicate(set_id, users[2:]),
            lambda: hidden.measure(set_id, resamples=50),
            lambda: hidden.freeze(set_id, max_runs=3, max_age_days=30, resamples=50),
        ):
            with pytest.raises(MendariError, match=f"is {state}: only a draft changes"):
                write()
        with pytest.raises(MendariError, match=NO_ASSIGNMENT):
            hidden.record_label(assignment, users[0], dict(split(0, 0)), org=CLINIC)
        assert hidden.next_case(users[0], org=CLINIC) is None
        assert dump(store) == before, state


# -- what breaks a frozen set ---------------------------------------------------------------------


def reweighed(world: World) -> None:
    """A case's weight re-sealed by someone with the key: its own commitment (`case_ref`) does
    not cover it, only the frozen content does."""
    row = rows(world.store, "evalset_cases", set_id=world.set_id)[0]
    body, _ = world.keys.open("attributes", row["attributes"], world.set_id, row["id"])
    sealed = world.keys.seal("attributes", {**body, "weight": 2.0}, world.set_id, row["id"])
    tamper(world.store, "UPDATE evalset_cases SET attributes = ? WHERE id = ?", (sealed, row["id"]))


EDITS: dict[str, Callable[[World], None]] = {
    "a weight re-sealed": reweighed,
    "a status": lambda w: tamper(
        w.store, "UPDATE evalset_cases SET status = 'disputed' WHERE set_id = ?", (w.set_id,)
    ),
    # Its final labels stay those of its two agreeing labels: only the status in the content
    # catches it.
    "an agreed case resolved": lambda w: tamper(
        w.store,
        "UPDATE evalset_cases SET status = 'resolved' WHERE set_id = ? AND status = 'agreed'",
        (w.set_id,),
    ),
    "a limit": lambda w: tamper(
        w.store, "UPDATE evalset_sets SET max_runs = max_runs + 5 WHERE id = ?", (w.set_id,)
    ),
    "the freeze time": lambda w: tamper(
        w.store,
        "UPDATE evalset_sets SET frozen_at = ? WHERE id = ?",
        ((T0 + timedelta(days=9)).isoformat(timespec="milliseconds"), w.set_id),
    ),
    "the freeze record removed": lambda w: tamper(
        w.store, "UPDATE evalset_sets SET freeze_record = NULL WHERE id = ?", (w.set_id,)
    ),
    "the freeze record re-sealed": lambda w: tamper(
        w.store,
        "UPDATE evalset_sets SET freeze_record = ? WHERE id = ?",
        (w.keys.seal("freeze", {**record_of(w), "content_hash": "0" * 64}, w.set_id), w.set_id),
    ),
}


@pytest.mark.parametrize("edit", sorted(EDITS))
def test_a_direct_edit_breaks_verify_check_grading_and_retiring(world: World, edit: str) -> None:
    """An edit behind the store's back, each one only the frozen content catches (the cases'
    and labels' own commitments still hold): `audit verify --evalsets` breaks at the set's
    `evalset.frozen`, `rotate --check` lists the set, and the grading read and retirement
    refuse, naming the set, never a case; nothing is chained."""
    frozen(world)
    store = world.store
    grader = world.grader()
    assert read(world, grader) == 2
    (freeze,) = events(store, "evalset.frozen")
    hashes = [row["case_hash"] for row in rows(store, "evalset_cases", set_id=world.set_id)]
    ids = [row["id"] for row in rows(store, "evalset_cases", set_id=world.set_id)]
    EDITS[edit](world)
    verified = store.verify(evalsets=world.keys)
    assert not verified["ok"] and verified["broken_at"] == freeze["seq"], verified
    checked = world.hidden.check()
    assert not checked["ready"]
    assert {"kind": "set", "set": world.set_id, "key": "commitment"} in checked["failed"]
    before = dump(store)
    for refused in (lambda: read(world, grader), lambda: world.hidden.retire(world.set_id)):
        with pytest.raises(MendariError, match=CHANGED) as caught:
            refused()
        assert world.set_id in str(caught.value)
        assert not any(name in str(caught.value) for name in (*hashes, *ids))
    assert dump(store) == before


def test_a_case_or_a_state_written_behind_the_store_s_back_is_found(world: World) -> None:
    frozen(world)
    store, set_id = world.store, world.set_id
    failed = {"kind": "set", "set": set_id, "key": "commitment"}
    (freeze,) = events(store, "evalset.frozen")
    tamper(store, "UPDATE evalset_sets SET state = 'draft' WHERE id = ?", (set_id,))
    verified = store.verify(evalsets=world.keys)
    assert not verified["ok"] and verified["broken_at"] == freeze["seq"]
    assert failed in world.hidden.check()["failed"]  # the chain froze it, whatever its row says
    tamper(store, "UPDATE evalset_sets SET state = 'active' WHERE id = ?", (set_id,))
    verified = store.verify(evalsets=world.keys)  # active, never activated
    assert not verified["ok"] and verified["unchained"] == 1
    tamper(store, "UPDATE evalset_sets SET state = 'frozen' WHERE id = ?", (set_id,))
    assert store.verify(evalsets=world.keys)["ok"]
    # A case added to the frozen set without its event: unchained, and the content differs.
    (row,) = rows(store, "evalset_cases", set_id=set_id)[:1]
    copy = {**row, "id": str(uuid.uuid4()), "case_hash": "hmac-sha256:" + "0" * 16 + ":" + "1" * 64}
    columns = ", ".join(copy)
    tamper(store, f"INSERT INTO evalset_cases ({columns}) VALUES ({', '.join('?' for _ in copy)})",
           tuple(copy.values()))  # fmt: skip
    verified = store.verify(evalsets=world.keys)
    assert not verified["ok"]
    with pytest.raises(MendariError, match=CHANGED):
        read(world, world.grader())
    # A draft set given a state of the lifecycle without its event.
    other = a_set(world.hidden, world.pack)
    tamper(store, "DELETE FROM evalset_cases WHERE id = ?", (copy["id"],))
    tamper(store, "UPDATE evalset_sets SET state = 'frozen' WHERE id = ?", (other,))
    verified = store.verify(evalsets=world.keys)
    assert not verified["ok"] and verified["unchained"] == 1
    tamper(store, "UPDATE evalset_sets SET state = 'draft' WHERE id = ?", (other,))
    assert store.verify(evalsets=world.keys)["ok"]
    # Moved to draft and back between two verifications, a draft's writes chained meanwhile (its
    # aggregates re-sealed, a case added and removed): the first write after its
    # `evalset.frozen` breaks it, though its row is frozen again and its content the same.
    tamper(store, "UPDATE evalset_sets SET state = 'draft' WHERE id = ?", (set_id,))
    world.hidden.measure(set_id, resamples=50)
    added = world.hidden.add_cases(set_id, [hand_written("A late case about a sore knee")])
    world.hidden.remove_case(set_id, added["cases"][0]["case"])
    tamper(store, "UPDATE evalset_sets SET state = 'frozen' WHERE id = ?", (set_id,))
    (measured,) = events(store, "evalset.measured")
    verified = store.verify(evalsets=world.keys)
    assert not verified["ok"] and verified["broken_at"] == measured["seq"], verified
    # Frozen again through the store while a draft: the check holds the set to its first
    # commitment, and verification breaks at the second freeze.
    tamper(store, "UPDATE evalset_sets SET state = 'draft' WHERE id = ?", (set_id,))
    world.hidden.freeze(set_id, max_runs=5, max_age_days=30, resamples=50)
    first, again = events(store, "evalset.frozen")
    assert again["payload"]["commitment"] != first["payload"]["commitment"]
    assert failed in world.hidden.check()["failed"]
    verified = store.verify(evalsets=world.keys)
    assert not verified["ok"] and verified["broken_at"] == again["seq"]


def test_a_retired_set_grades_nothing_more_whatever_its_row_says(world: World) -> None:
    """A retired set's state edited back by SQL, to active or to frozen: the grading read and a
    second retirement still refuse it as retired, from its chained `evalset.retired`, writing
    and chaining nothing, and verification breaks at the event the state contradicts."""
    frozen(world)
    store, set_id = world.store, world.set_id
    grader = world.grader()
    assert read(world, grader) == 2
    world.hidden.retire(set_id)
    (activated,) = events(store, "evalset.activated")
    (retired,) = events(store, "evalset.retired")
    for state, contradicted in (("active", retired), ("frozen", activated)):
        tamper(store, "UPDATE evalset_sets SET state = ? WHERE id = ?", (state, set_id))
        verified = store.verify(evalsets=world.keys)
        assert not verified["ok"] and verified["broken_at"] == contradicted["seq"], verified
        before = dump(store)
        with pytest.raises(MendariError, match="is retired: only a frozen or active set is graded"):
            read(world, grader)
        with pytest.raises(
            MendariError, match="is retired: only a frozen or active set is retired"
        ):
            world.hidden.retire(set_id)
        assert dump(store) == before, state
    tamper(store, "UPDATE evalset_sets SET state = 'retired' WHERE id = ?", (set_id,))
    assert store.verify(evalsets=world.keys)["ok"]


def read_after_retiring(world: World, grader: str) -> str:
    """Graded, retired, edited to active and graded again, then edited back to retired."""
    assert read(world, grader) == 2
    world.hidden.retire(world.set_id)
    tamper(world.store, "UPDATE evalset_sets SET state = 'active' WHERE id = ?", (world.set_id,))
    assert read(world, grader) == 2
    tamper(world.store, "UPDATE evalset_sets SET state = 'retired' WHERE id = ?", (world.set_id,))
    return "evalset.graded"


def activated_after_retiring(world: World, grader: str) -> str:
    """Retired before any read, edited to frozen and graded (which activates it), then edited
    back to retired."""
    world.hidden.retire(world.set_id)
    tamper(world.store, "UPDATE evalset_sets SET state = 'frozen' WHERE id = ?", (world.set_id,))
    assert read(world, grader) == 2
    tamper(world.store, "UPDATE evalset_sets SET state = 'retired' WHERE id = ?", (world.set_id,))
    return "evalset.activated"


def activated_twice(world: World, grader: str) -> str:
    """Graded (so active), edited to frozen and graded again: activated a second time."""
    assert read(world, grader) == 2
    tamper(world.store, "UPDATE evalset_sets SET state = 'frozen' WHERE id = ?", (world.set_id,))
    assert read(world, grader) == 2
    return "evalset.activated"


@pytest.mark.parametrize(
    "lifecycle",
    [read_after_retiring, activated_after_retiring, activated_twice],
    ids=lambda made: made.__name__,
)
def test_verify_breaks_at_an_activation_or_a_read_the_lifecycle_allows_none(
    world: World, monkeypatch: pytest.MonkeyPatch, lifecycle: Callable[[World, str], str]
) -> None:
    """What a store that did not read the chain's retirement would chain, its state edited
    around it: a read after `evalset.retired` (of a set graded before, or of one never
    activated, which the read activates), or a second activation. Verification breaks at that
    event, though the row's state is back to what the chain says."""
    monkeypatch.setattr(evalsets_module, "_retired", lambda db, set_id: False)
    frozen(world)
    kind = lifecycle(world, world.grader())
    last = events(world.store, kind)[-1]
    verified = world.store.verify(evalsets=world.keys)
    assert not verified["ok"] and verified["broken_at"] == last["seq"], verified


def test_a_ring_lacking_a_key_this_database_knows_is_refused_by_that_key(world: World) -> None:
    """A rotation done out of order: the next key made known here (deployed as a read key),
    then swapped in without the old one as a read key, before anything is re-sealed. The
    grading read and retirement refuse, naming the set and the key id the ring lacks, never a
    case, and not as a changed set (verify counts those values `unopenable`, no break); nothing
    is written or chained."""
    frozen(world)
    store, set_id = world.store, world.set_id
    grader = world.grader()
    nxt = evalset_keys_for_test().key
    store.evalset_ready(EvalsetKeys(world.keys.key, world.keys.index_key, (nxt,)))  # read first
    swapped = EvalsetKeys(nxt, world.keys.index_key)  # the old key left out
    lacking = EvalsetStore(store, swapped, clock=lambda: world.now[0])
    found = rows(store, "evalset_cases", set_id=set_id)
    named = [*(row["case_hash"] for row in found), *(row["id"] for row in found)]
    old = key_id(world.keys.key)
    before = dump(store)
    for refused in (
        lambda: lacking.grade_read(set_id, grader, DIGEST, count),
        lambda: lacking.retire(set_id),
    ):
        with pytest.raises(MendariError, match=f"sealed under key\\(s\\) {old}, which") as caught:
            refused()
        message = str(caught.value)
        assert set_id in message and CHANGED not in message
        assert "MENDARI_EVALSET_READ_KEYS" in message
        assert not any(name in message for name in named)
    assert dump(store) == before
    verified = store.verify(evalsets=swapped)
    assert not verified["ok"] and verified.get("broken_at") is None, verified
    assert old in verified["unopenable"]


# -- the grading read -----------------------------------------------------------------------------


def test_a_frozen_set_becomes_active_on_its_first_read(world: World) -> None:
    """The first read activates the set, chained in the same transaction as the read itself;
    the cases go to `run` named by their keyed hashes with their final labels and frozen
    attributes, never an id, a file or a digest; only what `run` returns comes back, and nothing
    is stored."""
    frozen(world)
    store = world.store
    grader = world.grader()
    received: list[Dataset] = []
    sentinel = object()

    def run(dataset: Dataset) -> object:
        received.append(dataset)
        return sentinel

    before = dump(store, without=("audit_events", "evalset_sets"))
    sets = rows(store, "evalset_sets", id=world.set_id)
    assert read(world, grader, run) is sentinel
    assert dump(store, without=("audit_events", "evalset_sets")) == before  # nothing stored
    (after,) = rows(store, "evalset_sets", id=world.set_id)
    assert {k for k, v in after.items() if sets[0][k] != v} == {"state"}
    assert after["state"] == "active"
    (dataset,) = received
    hashes = {row["case_hash"] for row in rows(store, "evalset_cases", set_id=world.set_id)}
    ids = {row["id"] for row in rows(store, "evalset_cases", set_id=world.set_id)}
    assert {record["id"] for record in dataset.records} == hashes
    assert (dataset.name, dataset.sha256, len(dataset.case_key)) == (world.set_id, "", 32)
    for record in dataset.records:
        assert set(record) == {"id", "input", "labels", "attributes"}
        assert record["labels"] == {"destination": "nurse", "urgency": "routine"}
        assert set(record["attributes"]) == {
            "source", "stratum", "language", "author", "template", "weight",
        }  # fmt: skip
    assert not any(case_id in json.dumps(dataset.records) for case_id in ids)
    kinds = [event for event in chained(store) if event["event"] in LIFECYCLE]
    activated, graded = kinds[-2:]
    assert (activated["event"], graded["event"]) == ("evalset.activated", "evalset.graded")
    assert graded["seq"] == activated["seq"] + 1  # one transaction: nothing between
    commitment = after["commitment"]
    assert activated["payload"] == {"set": world.set_id, "org": CLINIC}
    assert graded["payload"] == {
        "set": world.set_id,
        "org": CLINIC,
        "commitment": commitment,
        "grader": grader,
        "pack_digest": DIGEST,
        "n": 2,
        "run": 1,
    }
    assert read(world, grader) == 2  # the second read activates nothing more
    assert len(events(store, "evalset.activated")) == 1
    assert [e["payload"]["run"] for e in events(store, "evalset.graded")] == [1, 2]
    for event in chained(store):
        if event["event"] in PAYLOADS:
            assert set(event["payload"]) == PAYLOADS[event["event"]]
            assert LABEL_WORD.search(json.dumps(event["payload"])) is None
    assert store.verify(evalsets=world.keys)["ok"]


def test_the_grading_read_is_chained_before_run_receives_the_cases(
    world: World, db_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    frozen(world)
    grader = world.grader()

    def run(dataset: Dataset) -> int:
        with AuditStore(db_target, read_only=True) as reader:  # another connection: committed
            last = chained(reader)[-1]
        assert last["event"] == "evalset.graded" and last["payload"]["run"] == 1
        return len(dataset.records)

    assert read(world, grader, run) == 2
    real = world.store._append_many

    def failing(db: Any, event: str, entries: Any) -> Any:
        if event == "evalset.graded":
            raise RuntimeError("the chain refused")
        return real(db, event, entries)

    monkeypatch.setattr(world.store, "_append_many", failing)
    called: list[Dataset] = []
    before = dump(world.store)
    with pytest.raises(RuntimeError, match="the chain refused"):
        read(world, grader, called.append)
    assert called == [] and dump(world.store) == before  # no case unless its read is chained


def test_only_a_live_grading_key_of_the_set_s_organization_reads_it(world: World) -> None:
    frozen(world)
    store = world.store
    elsewhere = world.grader("clinic-b")
    revoked = world.grader()
    assert store.revoke_api_key(revoked.removeprefix("key:"), revoked_by="ops")
    expired = world.grader()
    past = "2000-01-01T00:00:00.000+00:00"
    tamper(store, "UPDATE api_keys SET expires_at = ? WHERE id = ?", (past, expired[4:]))
    service = store.create_api_key(name="s", role="service", org=CLINIC, created_by="ops")
    grader = world.grader()
    other = a_set(world.hidden, world.pack)  # a draft
    before = dump(store)
    for wrong_key in (elsewhere, revoked, expired, f"key:{service['id']}", "key:" + "0" * 12):
        with pytest.raises(MendariError, match=f"no evaluation set {world.set_id} is graded with"):
            read(world, wrong_key)
    unknown = str(uuid.uuid4())
    with pytest.raises(MendariError, match=f"no evaluation set {unknown} is graded with"):
        world.hidden.grade_read(unknown, grader, DIGEST, count)
    for wrong in ("user:0123456789abcdef", "key:x", f"key {grader[4:]}", ""):
        with pytest.raises(MendariError, match="a grader is a grading key"):
            read(world, wrong)
    for digest in ("d" * 64, "sha256:" + "D" * 64, "sha1:" + "d" * 40):
        with pytest.raises(MendariError, match="a pack digest is sha256:"):
            world.hidden.grade_read(world.set_id, grader, digest, count)
    with pytest.raises(MendariError, match="is draft: only a frozen or active set is graded"):
        world.hidden.grade_read(other, grader, DIGEST, count)
    with pytest.raises(MendariError, match="hidden evaluation sets are off"):
        EvalsetStore(store).grade_read(world.set_id, grader, DIGEST, count)
    assert dump(store) == before


# -- the rotation rule ----------------------------------------------------------------------------


def test_the_rotation_rule_refuses_exhausted_runs_an_exceeded_age_and_a_retired_set(
    world: World,
) -> None:
    frozen(world, max_runs=2, max_age_days=10)
    grader = world.grader()
    assert read(world, grader) == read(world, grader) == 2
    before = dump(world.store)
    with pytest.raises(MendariError, match="has had its 2 grading run\\(s\\): retire it"):
        read(world, grader)
    assert dump(world.store) == before
    # Another set: its age counts from its freeze, by the store's clock.
    world.set_id = a_set(world.hidden, world.pack)
    world.now[0] = T0 + timedelta(days=3)
    frozen(world, max_runs=50, max_age_days=10)
    world.now[0] = T0 + timedelta(days=13) - timedelta(milliseconds=1)
    assert read(world, grader) == 2
    world.now[0] = T0 + timedelta(days=13)
    before = dump(world.store)
    with pytest.raises(MendariError, match="was frozen 10 day\\(s\\) ago or more: retire it"):
        read(world, grader)
    assert dump(world.store) == before
    retired = world.hidden.retire(world.set_id)
    assert retired == {
        "set": world.set_id,
        "state": "retired",
        "retired_at": world.now[0].isoformat(timespec="milliseconds"),
        "commitment": rows(world.store, "evalset_sets", id=world.set_id)[0]["commitment"],
        "runs": 1,
    }
    (event,) = events(world.store, "evalset.retired")
    payload = {"set": world.set_id, "org": CLINIC, "commitment": retired["commitment"]}
    assert event["payload"] == payload
    draft = a_set(world.hidden, world.pack)
    before = dump(world.store)
    with pytest.raises(MendariError, match="is retired: only a frozen or active set is graded"):
        read(world, grader)
    with pytest.raises(MendariError, match="is retired: only a frozen or active set is retired"):
        world.hidden.retire(world.set_id)
    with pytest.raises(MendariError, match="is draft: only a frozen or active set is retired"):
        world.hidden.retire(draft)
    assert dump(world.store) == before
    assert world.store.verify(evalsets=world.keys)["ok"]


def test_concurrent_reads_never_exceed_the_runs_allowed(world: World, db_target: str) -> None:
    """The rule's check and the read's event are one exclusive transaction: of eight readers at
    once, as many as the rule allows read, the others are refused."""
    frozen(world, max_runs=3)
    grader = world.grader()
    start = threading.Barrier(8)
    outcomes: list[str] = []
    stores = [AuditStore(db_target) for _ in range(8)]  # each reader its own, opened first

    def reader(store: AuditStore) -> None:
        hidden = EvalsetStore(store, world.keys, clock=lambda: T0)
        start.wait()
        try:
            hidden.grade_read(world.set_id, grader, DIGEST, count)
            outcomes.append("read")
        except MendariError as error:
            outcomes.append(str(error))

    threads = [threading.Thread(target=reader, args=(store,)) for store in stores]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        for store in stores:
            store.close()
    assert outcomes.count("read") == 3, outcomes
    assert all("has had its 3 grading run(s)" in o for o in outcomes if o != "read"), outcomes
    assert sorted(e["payload"]["run"] for e in events(world.store, "evalset.graded")) == [1, 2, 3]
    assert len(events(world.store, "evalset.activated")) == 1
    assert world.store.verify(evalsets=world.keys)["ok"]


# -- rotation, verification and the check ---------------------------------------------------------


def test_rotating_a_frozen_set_changes_ciphertext_only(world: World) -> None:
    frozen(world)
    store = world.store
    grader = world.grader()
    assert read(world, grader) == 2
    sealed = {
        (table, column, row["id"]): row[column]
        for table, kinds in evalsets_module.SEALED_COLUMNS.items()
        for row in rows(store, table)
        for column in kinds.columns
        if row[column] is not None
    }
    (head,) = rows(store, "evalset_sets", id=world.set_id)
    record = record_of(world)
    nxt = evalset_keys_for_test().key
    store.evalset_ready(EvalsetKeys(world.keys.key, world.keys.index_key, (nxt,)))  # read first
    swapped = EvalsetStore(store, EvalsetKeys(nxt, world.keys.index_key, (world.keys.key,)))
    rotated = swapped.rotate(to=key_id(nxt))
    assert rotated["unopenable"] == [] and rotated["from"] == {key_id(world.keys.key): len(sealed)}
    alone = EvalsetKeys(nxt, world.keys.index_key)
    after = {
        (table, column, row["id"]): row[column]
        for table, kinds in evalsets_module.SEALED_COLUMNS.items()
        for row in rows(store, table)
        for column in kinds.columns
        if row[column] is not None
    }
    assert set(after) == set(sealed) and not set(after.values()) & set(sealed.values())
    (moved,) = rows(store, "evalset_sets", id=world.set_id)
    assert {k for k, v in moved.items() if head[k] != v} == {
        "header",
        "freeze_record",
        "aggregates",
    }
    assert alone.open("freeze", moved["freeze_record"], world.set_id)[0] == record
    world.keys, world.hidden = alone, EvalsetStore(store, alone, clock=lambda: world.now[0])
    assert recomputed(world, alone)[0]
    assert read(world, grader) == 2  # the same content, under the new key
    checked = world.hidden.check()
    assert checked["ready"], checked["failed"]
    verified = store.verify(evalsets=alone)
    assert verified["ok"] and verified["evalsets"]["frozen"] == 1


def test_verify_counts_a_frozen_set_unchecked_without_keys_and_unopenable_without_its_key(
    world: World,
) -> None:
    frozen(world)
    store = world.store
    verified = store.verify(evalsets=world.keys)
    assert verified["ok"] and verified["evalsets"]["frozen"] == 1
    without = store.verify(evalsets=True)
    assert without["ok"] and without["evalsets"]["frozen"] == 0
    counted = verified["evalsets"]
    assert without["evalsets"]["unchecked"] == counted["checked"] + counted["labels"] + 1
    stranger = EvalsetKeys(evalset_keys_for_test().key, world.keys.index_key)
    store_known = store.verify(evalsets=stranger)  # this database's key is not in the ring
    assert not store_known["ok"] and key_id(world.keys.key) in store_known["unopenable"]


# -- the key holder's commands --------------------------------------------------------------------


def documented(command: str, set_id: str, db: str) -> list[str]:
    """The runbook's `mendari evalset <command>` (docs/operations/evalsets.md) as written, for
    `set_id` on `db`: what `main` takes."""
    text = (ROOT / "docs/operations/evalsets.md").read_text().replace("\\\n", " ")
    lines = [line for line in text.splitlines() if line.startswith(f"mendari evalset {command} ")]
    assert len(lines) == 1, command
    line = lines[0].replace("<set id>", set_id).replace('"$DATABASE_URL"', db)
    return shlex.split(line)[1:]


def run(
    capsys: pytest.CaptureFixture[str], *argv: str, allowed: Sequence[str] = ()
) -> tuple[int, str, str]:
    """A command, its output holding no planted mark but those `allowed` (conflict 55: the
    stratum and language in `evalset show`)."""
    code = main(list(argv))
    out, err = capsys.readouterr()
    for name, mark in MARKS.items():
        if name not in allowed:
            assert mark not in out + err, (argv, name)
    return code, out, err


def labelled_by_command(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, db: str, users: Sequence[str], count: int
) -> tuple[str, list[str]]:
    """A set made, filled with `count` cases, drawn and assigned from the command line, and
    labelled at the store (`split`), its disputed cases adjudicated: its id and case hashes."""
    on = ("--db", db)
    _, out, _ = run(capsys, "evalset", "create", INBOX, "--org", CLINIC, "--name", "n", *on)
    set_id = json.loads(out)["set"]
    path = cases_file(tmp_path, cases(count))
    _, out, _ = run(capsys, "evalset", "add", set_id, str(path), *on)
    hashes = [case["case"] for case in json.loads(out)["cases"]]
    assert run(capsys, "evalset", "sample", set_id, "--per-stratum", str(count), *on)[0] == 0
    pool = ("--labeller", users[0], "--labeller", users[1])
    assert run(capsys, "evalset", "assign", set_id, *pool, *on)[0] == 0
    keys = evalset_keys()
    assert keys is not None
    with AuditStore(db) as store:
        hidden = EvalsetStore(store, keys)
        label_all(hidden, users[:2], split)
    if count > 1:
        assert run(capsys, "evalset", "adjudicate", set_id, "--labeller", users[2], *on)[0] == 0
        with AuditStore(db) as store:
            label_all(EvalsetStore(store, keys), users[2:], lambda number, _: split(number, 2))
    return set_id, hashes


def grade(db: str, set_id: str, *, org: str = CLINIC) -> int:
    """One grading read of `set_id`, in the process, as LP-11l's route will make it."""
    keys = evalset_keys()
    assert keys is not None
    with AuditStore(db) as store:
        made = store.create_grading_key(org=org, created_by="ops", reason="a grading run")
        return int(EvalsetStore(store, keys).grade_read(set_id, f"key:{made['id']}", DIGEST, count))


def test_audit_verify_passes_after_labelling_adjudication_and_freeze(
    db_target: str,
    tmp_path: Path,
    inbox_pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """LP-11i's acceptance: the key holder makes a set, draws its cases, assigns them and has the
    disputed ones adjudicated from the command line, the labellers labelling at the store; a
    case added after the draw and the unresolved one keep it from freezing until the unresolved
    one is removed and the undrawn one left out; `freeze` prints the commitment and each critical
    label's distinct positives, warning under 100; `show` reads it; it is graded in the process
    and retired; `mendari audit verify --evalsets` passes, the frozen set recomputed."""
    evalset_on(monkeypatch)
    served(db_target, inbox_pack)
    with AuditStore(db_target) as store:
        users = labellers(store, 3)
    on = ("--db", db_target)
    set_id, hashes = labelled_by_command(capsys, tmp_path, db_target, users, 3)
    late = cases_file(tmp_path, cases(1, start=3))
    assert run(capsys, "evalset", "add", set_id, str(late), *on)[0] == 0
    freeze = ("evalset", "freeze", set_id, "--max-runs", "3", "--max-age-days", "90", *on)
    code, _, err = run(capsys, *freeze)
    assert code == 1 and "has 1 case(s) not drawn" in err
    code, _, err = run(capsys, *freeze, "--drop-unsampled")
    assert code == 1 and "(1 unresolved)" in err and LABEL_WORD.search(err) is None
    code, out, _ = run(capsys, "evalset", "progress", set_id, *on)
    statuses = {k: v for k, v in json.loads(out)["statuses"].items() if v}
    assert statuses == {"draft": 1, "agreed": 1, "resolved": 1, "unresolved": 1}
    with AuditStore(db_target) as store:
        (unresolved,) = rows(store, "evalset_cases", set_id=set_id, status="unresolved")
    assert run(capsys, "evalset", "remove-case", set_id, unresolved["case_hash"], *on)[0] == 0
    code, out, err = run(capsys, *freeze, "--drop-unsampled")
    made = json.loads(out)
    assert code == 0 and made["state"] == "frozen" and SHA256.fullmatch(made["commitment"])
    assert (made["n"], made["dropped"], made["max_runs"], made["max_age_days"]) == (2, 1, 3, 90)
    assert made["critical"] == [
        {"question": "urgency", "label": "urgent", "cases": 0, "distinct": 0, "insufficient": True}
    ]
    assert "warning: urgency=urgent has 0 distinct positive(s), under 100" in err
    assert "keep its commitment" in err
    assert not any(hashed in out + err for hashed in hashes)
    code, out, _ = run(capsys, "evalset", "show", set_id, *on, allowed=("stratum", "language"))
    shown = json.loads(out)
    assert code == 0 and (shown["state"], shown["commitment"]) == ("frozen", made["commitment"])
    assert (shown["cases"], shown["runs"]) == (2, {"used": 0, "left": 3})
    assert shown["aggregates"]["coverage"]["cases"] == 2
    assert shown["aggregates"]["agreement"]["format"] == "mendari.agreement/1"
    assert grade(db_target, set_id) == 2
    code, out, _ = run(capsys, "evalset", "show", set_id, *on, allowed=("stratum", "language"))
    shown = json.loads(out)
    assert (shown["state"], shown["runs"]) == ("active", {"used": 1, "left": 2})
    code, out, err = run(capsys, "evalset", "retire", set_id, *on)
    retired = json.loads(out)
    assert code == 0 and (retired["state"], retired["runs"]) == ("retired", 1)
    assert "nothing is published" in err
    code, out, _ = run(capsys, "audit", "verify", "--evalsets", *on)
    status = json.loads(out)
    assert code == 0 and status["ok"]
    assert status["evalsets"] == {
        "sets": 1, "checked": 2, "removed": 2, "unchecked": 0, "assignments": 5, "labels": 5,
        "frozen": 1,
    }  # fmt: skip
    assert LABEL_WORD.search(out) is None
    code, out, _ = run(capsys, "evalset", "rotate", "--check", *on)
    assert code == 0 and json.loads(out)["ready"]
    with AuditStore(db_target) as store:
        kinds = [event["event"] for event in chained(store) if event["event"] in LIFECYCLE]
    assert kinds == ["evalset.frozen", "evalset.activated", "evalset.graded", "evalset.retired"]


def test_a_second_lifecycle_by_the_documented_steps(
    db_target: str,
    tmp_path: Path,
    inbox_pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The runbook's freeze, show and retire as written, twice (lesson of 2026-10-06), with the
    writes a live deployment makes between them: the first set graded while the second is
    labelled, frozen and graded, then retired after the second freezes."""
    evalset_on(monkeypatch)
    served(db_target, inbox_pack)
    with AuditStore(db_target) as store:
        users = labellers(store, 3)

    def verified() -> None:
        assert main(["audit", "verify", "--evalsets", "--db", db_target]) == 0
        capsys.readouterr()

    first, _ = labelled_by_command(capsys, tmp_path, db_target, users, 2)
    code, out, err = run(capsys, *documented("freeze", first, db_target))
    assert code == 0, err
    limits = json.loads(out)
    assert grade(db_target, first) == 2
    verified()
    second, _ = labelled_by_command(capsys, tmp_path, db_target, users, 2)
    assert grade(db_target, first) == 2  # graded while the second is labelled
    code, out, err = run(capsys, *documented("freeze", second, db_target))
    assert code == 0, err
    verified()
    assert grade(db_target, second) == 2
    code, out, _ = run(capsys, *documented("retire", first, db_target))
    assert code == 0 and json.loads(out)["runs"] == 2
    for set_id, state, used in ((first, "retired", 2), (second, "active", 1)):
        code, out, _ = run(
            capsys, *documented("show", set_id, db_target), allowed=("stratum", "language")
        )
        shown = json.loads(out)
        assert (shown["state"], shown["runs"]["used"]) == (state, used)
        assert (shown["max_runs"], shown["max_age_days"]) == (
            limits["max_runs"],
            limits["max_age_days"],
        )
    verified()
    keys = evalset_keys()
    assert keys is not None
    with AuditStore(db_target) as store:
        assert store.verify(evalsets=keys)["evalsets"]["frozen"] == 2
    code, out, _ = run(capsys, "evalset", "rotate", "--check", "--db", db_target)
    assert code == 0 and json.loads(out)["ready"]


def test_the_unsalted_content_hash_never_leaves_the_sealed_store(
    db_target: str,
    tmp_path: Path,
    inbox_pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Neither the content hash nor the freeze salt is in any output of the key holder's
    commands, in `audit verify`, the check, the sealed export, the chain, any table or what a
    grading run receives (its records, digest and name): only the sealed freeze record holds
    them."""
    evalset_on(monkeypatch)
    served(db_target, inbox_pack)
    with AuditStore(db_target) as store:
        users = labellers(store, 3)
    on = ("--db", db_target)
    set_id, _ = labelled_by_command(capsys, tmp_path, db_target, users, 2)
    seen: list[str] = []
    allowed = ("stratum", "language")

    def ran(*commands: Sequence[str]) -> None:
        for argv in commands:
            code, out, err = run(capsys, *argv, *on, allowed=allowed)
            assert code == 0, (argv, err)
            seen.append(out + err)

    ran(
        ("evalset", "freeze", set_id, "--max-runs", "2", "--max-age-days", "9"),
        ("evalset", "show", set_id),
        ("audit", "verify", "--evalsets"),
        ("evalset", "rotate", "--check"),
    )
    keys = evalset_keys()
    assert keys is not None
    with AuditStore(db_target) as store:  # what a grading run receives
        made = store.create_grading_key(org=CLINIC, created_by="ops", reason="the tests")
        received = EvalsetStore(store, keys).grade_read(
            set_id,
            f"key:{made['id']}",
            DIGEST,
            lambda dataset: json.dumps(
                [dataset.records, dataset.sha256, dataset.name, str(dataset.path)], default=str
            ),
        )
    assert '"input"' in received  # the cases themselves
    seen.append(received)
    ran(
        ("evalset", "retire", set_id),
        ("evalset", "show", set_id),
        ("evalset", "list"),
        ("evalset", "progress", set_id),
        ("audit", "export", "--administrative"),
    )
    exported = tmp_path / "backup" / "evalsets.jsonl"
    exported.parent.mkdir()
    assert run(capsys, "evalset", "export", "--out", str(exported), *on)[0] == 0
    seen.append(exported.read_text())
    with AuditStore(db_target) as store:
        seen.append(dump(store))
        (row,) = rows(store, "evalset_sets", id=set_id)
    record = keys.open("freeze", row["freeze_record"], set_id)[0]
    salt = base64.b64decode(record["salt"])
    secret = (record["content_hash"], record["salt"], salt.hex())
    for text in seen:
        assert not any(value in text for value in secret)
    assert record["content_hash"] != row["commitment"]


def test_freeze_derives_labels_from_the_pack_s_decision_table(world: World) -> None:
    """A pack with a derived question (AI Guard's `verdict`): the frozen content holds the label
    its decision table derives from each case's final member labels, kept in the freeze record
    so a grading read recomputes it without the pack."""
    guard = resolve_pack("mendari/ai-guard")
    hidden, store = world.hidden, world.store
    world.set_id = set_id = a_set(hidden, guard)
    members = [q for q in guard.manifest.questions if q not in guard.manifest.derived]
    unlabelled = [{k: v for k, v in case.items() if k != "labels"} for case in cases(2)]
    hidden.add_cases(set_id, unlabelled)  # an intended label is of the inbox pack's questions
    hidden.sample(set_id, per_stratum=2)
    users = labellers(store, 2)
    hidden.assign(set_id, users)

    def script(number: int, nth: int) -> Mapping[str, str]:
        unsafe = "yes" if number == 1 else "no"
        return {q: unsafe if q == "unsafe_advice" else "no" for q in members}

    label_all(hidden, users, script)
    hidden.freeze(set_id, max_runs=2, max_age_days=5, resamples=50)
    record = record_of(world)
    found = {row["id"]: row for row in rows(store, "evalset_cases", set_id=set_id)}
    numbers = {
        case_id: number_of(world.keys.open("input", row["input"], set_id, case_id)[0])
        for case_id, row in found.items()
    }
    assert {numbers[c]: d for c, d in record["derived"].items()} == {
        0: {"verdict": "pass"},
        1: {"verdict": "block"},
    }
    received: list[Dataset] = []
    world.hidden.grade_read(set_id, world.grader(), DIGEST, received.append)
    verdicts = {record["labels"]["verdict"] for record in received[0].records}
    assert verdicts == {"pass", "block"}
    assert store.verify(evalsets=world.keys)["evalsets"]["frozen"] == 1


def test_show_reads_without_writing_and_without_the_keys(
    world: World, db_target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    hidden, set_id = world.hidden, world.set_id
    shown = hidden.show(set_id)
    assert (shown["state"], shown["commitment"], shown["runs"]) == (
        "draft",
        None,
        {"used": 0, "left": None},
    )
    assert shown["aggregates"] is None and shown["graded_until"] is None
    frozen(world, max_runs=4, max_age_days=10)
    before = dump(world.store)
    shown = hidden.show(set_id)
    assert dump(world.store) == before  # nothing written, nothing chained
    assert shown["graded_until"] == (T0 + timedelta(days=10)).isoformat(timespec="milliseconds")
    assert shown["runs"] == {"used": 0, "left": 4} and shown["cases"] == 2
    assert shown["aggregates"]["coverage"]["cases"] == 2
    assert EvalsetStore(world.store).show(set_id)["aggregates"] is None  # without the keys
    code = main(["evalset", "show", set_id, "--db", db_target])
    out, err = capsys.readouterr()
    assert code == 0 and json.loads(out)["aggregates"] is None
    assert "its aggregates are sealed" in err
    with pytest.raises(MendariError, match="no evaluation set"):
        hidden.show(str(uuid.uuid4()))


def test_the_lifecycle_events_are_the_organizations_administrative_events(world: World) -> None:
    frozen(world)
    grader = world.grader()
    read(world, grader)
    world.hidden.retire(world.set_id)
    items = world.store.administrative_events(CLINIC, limit=500).rows
    assert set(LIFECYCLE) <= {item["event"] for item in items}
    assert [i for i in world.store.administrative_events("clinic-b", limit=500).rows
            if i["event"].startswith("evalset.")] == []  # fmt: skip
    rendered = [
        audit_fhir.administrative_event(item) for item in items if item["event"] in LIFECYCLE
    ]
    for resource in rendered:
        assert fhir_structure.check(resource) == [], resource
        kind = resource["subtype"][0]["code"]
        agent = resource["agent"][0]["who"]["display"]
        details = {d["type"]: d["valueString"] for d in resource["entity"][0]["detail"]}
        if kind == "evalset.graded":
            assert agent == grader and resource["action"] == "R"
            assert set(details) == {"hash", "org", "pack_digest"}
        else:
            assert set(details) == {"hash", "org"}, details  # never the commitment or limits
    assert LABEL_WORD.search(json.dumps(rendered)) is None


def test_the_lifecycle_commands_refuse_while_the_keys_are_unset(
    tmp_path: Path, inbox_pack: Pack, capsys: pytest.CaptureFixture[str]
) -> None:
    db = str(tmp_path / "mendari.db")
    served(db, inbox_pack)
    before = Path(db).read_bytes()
    set_id = str(uuid.uuid4())
    for argv in (
        ("evalset", "freeze", set_id, "--max-runs", "1", "--max-age-days", "1"),
        ("evalset", "retire", set_id),
    ):
        code, _, err = run(capsys, *argv, "--db", db)
        assert code == 1 and err.startswith("error: hidden evaluation sets are off"), err
    code, _, err = run(capsys, "evalset", "show", set_id, "--db", db)  # read-only, no key
    assert code == 1 and err.strip() == f"error: no evaluation set {set_id}"
    assert Path(db).read_bytes() == before
