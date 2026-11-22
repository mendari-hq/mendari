# SPDX-License-Identifier: Apache-2.0
"""A draft set's statuses and weights, and every set's stored aggregates, covered by commitments
(LP-11p, `core/evalsets.py`, `core/audit.py`, ADR-0011).

LP-11o's review found three edits that nothing caught: a draft case's status changed by SQL (a
disputed case made `agreed`, which freezing then took with its first labeller's answer), a drawn
case's weight re-sealed or its attributes put back from before the draw, and a set's stored
aggregates re-sealed or put back. Each is a regression test here, failing without the fix:

- A case's status is the one its labels (each covered by its `label_ref`) and the set's latest
  draw give it: `audit verify --evalsets` breaks at the case's `evalset.case_added` (at a frozen
  set's `evalset.frozen`), `rotate --check` names the case by its hash, and `freeze` and
  `measure` refuse the set, naming no case and writing nothing.
- Each draw chains a commitment to every drawn case's weight (`weight_refs` on
  `evalset.sampled`, by keyed hash, a new salt each time): a weight re-sealed, or put back from
  before that draw, breaks verification at the draw; a weight on a case no draw took breaks it at
  the case's event. A draw takes what the chain drew before, never a status edited by SQL.
- Each store of a set's aggregates chains their commitment (`aggregates_ref` on
  `evalset.measured` and `evalset.frozen`): aggregates re-sealed, put back, removed or written
  without their event break verification (or count `unchained`), and `show`, the curators' read
  (`GET /api/evalsets/{set_id}`) and `mendari evalset show` refuse them, naming the set only.
- Each label is the one the chain recorded, in the round its `evalset.labelled` chained, never
  as its assignment row says now, and every label the chain recorded must be there; an
  adjudicator's label settles only a case whose two first-pass labels differ. So a label deleted
  and a round moved by SQL (the review of this PR: freezing then took one labeller's answer) are
  found, `freeze` and `measure` refuse the set, and `assign` and `adjudicate`, which open
  nothing, refuse a status the chain's draw and labels do not allow, so no case is given to a
  labeller on it. An agreed case edited to `disputed` still reaches an adjudicator (telling it
  from a disputed one takes the labels' values), but stays found.
- Sets made before these commitments still verify and stay ready to rotate: their draws and
  aggregates are read as before, and `predating` counts their rows (a count, never an id).
- A draw, a label or a measure written while a verification reads is never taken for a break,
  and a draw or a label landing while `freeze` or `measure` reads is "run it again", never a
  refusal.

On SQLite and Postgres (`db_target`); keys are made per test, cases are hand-written synthetic
text."""

from __future__ import annotations

import base64
import functools
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from mendari import AuditStore, Pack
from mendari.cli import main
from mendari.core import evalsets
from mendari.core.audit import GENESIS, _canonical, _chain_hash
from mendari.core.evalset_keys import EvalsetKeys
from mendari.core.evalsets import EvalsetStore
from mendari.errors import MendariError
from tests.conftest import evalset_keys_for_test, evalset_on, hand_written
from tests.test_evalset_labelling import (
    cases,
    events,
    label_all,
    labellers,
    rows,
    split,
)
from tests.test_evalset_routes import SET, memberships, started, url
from tests.test_evalset_store import CLINIC, a_set, served, tamper

CHANGED = "stored aggregates are not those its chain committed to"


def deployed(monkeypatch: pytest.MonkeyPatch, keys: EvalsetKeys) -> None:
    """The key holder's shell, holding `keys`."""
    monkeypatch.setenv("MENDARI_EVALSET_KEY", base64.b64encode(keys.key).decode())
    monkeypatch.setenv("MENDARI_EVALSET_INDEX_KEY", base64.b64encode(keys.index_key).decode())
    monkeypatch.delenv("MENDARI_EVALSET_READ_KEYS", raising=False)


@dataclass
class World:
    store: AuditStore
    hidden: EvalsetStore
    keys: EvalsetKeys
    pack: Pack
    set_id: str
    users: list[str] = field(default_factory=list)  # the organization's labellers, once made

    def labelled(self, count: int = 3, *, set_id: str | None = None) -> None:
        """`count` cases of a set drawn and labelled blind by two labellers (`split`: case 0
        agreed, cases 1 and 2 disputed), nothing adjudicated yet."""
        set_id = set_id or self.set_id
        self.hidden.add_cases(set_id, cases(count))
        self.hidden.sample(set_id, per_stratum=count)
        self.users = self.users or labellers(self.store, 3)
        self.hidden.assign(set_id, self.users[:2])
        label_all(self.hidden, self.users[:2], split)

    def settled(self, *, set_id: str | None = None) -> None:
        """`labelled`, then case 1 resolved by a third labeller and case 2 (a three-way split)
        removed: every drawn case settled, as freezing needs."""
        set_id = set_id or self.set_id
        self.labelled(set_id=set_id)
        self.hidden.adjudicate(set_id, self.users[2:])
        label_all(self.hidden, self.users[2:], lambda number, _: split(number, 2))
        for row in rows(self.store, "evalset_cases", set_id=set_id, status="unresolved"):
            self.hidden.remove_case(set_id, row["case_hash"])

    def verified(self) -> dict[str, Any]:
        return self.store.verify(evalsets=self.keys)

    def failed(self) -> set[tuple[str, str | None, str]]:
        """What `rotate --check` lists: (kind, the case's hash or None, what did not match),
        each failure naming its set and no more."""
        checked = self.hidden.check()
        assert not checked["ready"]
        assert all(set(f) <= {"kind", "set", "case", "key"} for f in checked["failed"])
        return {(f["kind"], f.get("case"), f["key"]) for f in checked["failed"]}


@pytest.fixture
def world(db_target: str, inbox_pack: Pack) -> Iterator[World]:
    keys = evalset_keys_for_test()
    with AuditStore(db_target) as store:
        hidden = EvalsetStore(store, keys)
        yield World(store, hidden, keys, inbox_pack, a_set(hidden, inbox_pack))


def case_of(world: World, number: int, set_id: str | None = None) -> dict[str, Any]:
    """The row of the case whose input carries `number` (`cases`), opened to find it."""
    set_id = set_id or world.set_id
    for row in rows(world.store, "evalset_cases", set_id=set_id):
        value, _ = world.keys.open("input", row["input"], set_id, row["id"])
        if f"case {number}:" in value["message"]["body"]:
            return row
    raise AssertionError(f"no case {number}")


def seq_added(world: World, row: dict[str, Any]) -> int:
    (added,) = [
        e for e in events(world.store, "evalset.case_added")
        if e["payload"]["case"] == row["case_hash"]
    ]  # fmt: skip
    return int(added["seq"])


def attributes(world: World, row: dict[str, Any]) -> dict[str, Any]:
    return dict(world.keys.open("attributes", row["attributes"], row["set_id"], row["id"])[0])


def resealed(world: World, row: dict[str, Any], body: dict[str, Any]) -> None:
    """A case's attributes sealed again by someone holding the key: its own commitment
    (`case_ref`) still holds, since the weight sits beside what it covers."""
    sealed = world.keys.seal("attributes", body, row["set_id"], row["id"])
    tamper(world.store, "UPDATE evalset_cases SET attributes = ? WHERE id = ?", (sealed, row["id"]))


def status_of(world: World, row: dict[str, Any], status: str) -> None:
    tamper(world.store, "UPDATE evalset_cases SET status = ? WHERE id = ?", (status, row["id"]))


def unchanged(world: World) -> str:
    tables = ("evalset_sets", "evalset_cases", "evalset_assignments", "evalset_labels")
    with world.store._db.session(exclusive=False) as db:
        return json.dumps(
            {
                table: db.all(f"SELECT * FROM {table} ORDER BY 1")
                for table in (*tables, "audit_events")
            },
            default=str,
        )


def refused_naming_no_case(world: World, write: Callable[[], Any], match: str) -> str:
    """`write` refused with `match`, naming no case by its hash or id, and nothing written."""
    before = unchanged(world)
    with pytest.raises(MendariError, match=match) as caught:
        write()
    said = str(caught.value)
    for row in rows(world.store, "evalset_cases"):
        assert row["case_hash"] not in said and row["id"] not in said
    assert unchanged(world) == before
    return said


# -- a draft case's status ------------------------------------------------------------------------


STATUS_EDITS: dict[str, tuple[int, str]] = {
    # The finding: a disputed case made `agreed`, which freezing took with its first labeller's
    # answer, never adjudicated.
    "a disputed case agreed": (1, "agreed"),
    "an agreed case disputed": (0, "disputed"),
    "a disputed case resolved without its third label": (2, "resolved"),
    "a drawn case put back to draft": (0, "draft"),
    "a labelled case put back to sampled": (0, "sampled"),
}
# The edits the chain shows without opening a label (its draw, and how many labels of each round
# it recorded): `assign` and `adjudicate`, which open nothing (threat model I13), refuse these.
# Telling `agreed` from `disputed` takes the labels' values.
SHAPED_EDITS = {edit for edit, (_, to) in STATUS_EDITS.items() if to not in ("agreed", "disputed")}


@pytest.mark.parametrize("edit", sorted(STATUS_EDITS))
def test_a_draft_status_its_labels_do_not_give_breaks_verify_and_check_and_is_never_frozen(
    world: World, edit: str
) -> None:
    """A status edited by SQL on a draft set: `verify` breaks at the case's `evalset.case_added`,
    `rotate --check` names the case by its hash, and `freeze` and `measure` refuse the set naming
    no case, writing nothing; so do `assign` and `adjudicate` (the key holder's, and a curator's
    for the set's organization) when the chain shows the status wrong without opening a label,
    so no case is given to a labeller on such a status. Put back, everything holds again."""
    world.labelled()
    number, status = STATUS_EDITS[edit]
    row = case_of(world, number)
    assert world.verified()["ok"] and world.hidden.check()["ready"]
    status_of(world, row, status)
    verified = world.verified()
    assert not verified["ok"] and verified["broken_at"] == seq_added(world, row), verified
    assert ("case", row["case_hash"], "status") in world.failed()
    hidden, users = world.hidden, world.users
    writes: list[tuple[Callable[[], Any], str]] = [
        (lambda: hidden.freeze(world.set_id, max_runs=3, max_age_days=30), "frozen"),
        (lambda: hidden.measure(world.set_id, resamples=20), "measured"),
    ]
    if edit in SHAPED_EDITS:
        writes += [
            (lambda: hidden.assign(world.set_id, users[:2]), "assigned"),
            (lambda: hidden.adjudicate(world.set_id, users[2:]), "assigned"),
            (lambda: hidden.adjudicate(world.set_id, users[2:], org=CLINIC), "assigned"),
        ]
    for write, done in writes:
        said = refused_naming_no_case(world, write, "a status its labels and draw do not give")
        assert f"nothing was {done}" in said and world.set_id in said
    status_of(world, row, row["status"])
    assert world.verified()["ok"] and world.hidden.check()["ready"]


def test_the_disputed_case_the_review_froze_as_agreed_is_now_refused(world: World) -> None:
    """The review's probe as written: two labellers disagree on case 1, its status is edited to
    `agreed`, and the set is frozen. Freezing now refuses, so the first labeller's answer is
    never committed to as the case's label; once the case is adjudicated, it freezes."""
    world.labelled()
    disputed = case_of(world, 1)
    assert disputed["status"] == "disputed"
    status_of(world, disputed, "agreed")
    status_of(world, case_of(world, 2), "agreed")  # the other disputed case too
    refused_naming_no_case(
        world,
        lambda: world.hidden.freeze(world.set_id, max_runs=3, max_age_days=30, resamples=20),
        "holds 2 case\\(s\\) with a status its labels and draw do not give: nothing was frozen",
    )
    assert events(world.store, "evalset.frozen") == []
    status_of(world, disputed, "disputed")
    status_of(world, case_of(world, 2), "disputed")
    world.hidden.adjudicate(world.set_id, world.users[2:])
    label_all(world.hidden, world.users[2:], lambda number, _: split(number, 2))
    world.hidden.remove_case(world.set_id, case_of(world, 2)["case_hash"])  # a three-way split
    made = world.hidden.freeze(world.set_id, max_runs=3, max_age_days=30, resamples=20)
    assert made["state"] == "frozen" and world.verified()["ok"]


def test_a_frozen_set_whose_status_its_labels_do_not_give_breaks_at_its_freeze(
    world: World,
) -> None:
    """On a frozen set, a status its labels do not give breaks verification at the set's
    `evalset.frozen`, as its content does, and `rotate --check` names the case too."""
    world.settled()
    world.hidden.freeze(world.set_id, max_runs=3, max_age_days=30, resamples=20)
    (freeze,) = events(world.store, "evalset.frozen")
    resolved = case_of(world, 1)
    status_of(world, resolved, "agreed")
    verified = world.verified()
    assert not verified["ok"] and verified["broken_at"] == freeze["seq"]
    assert ("case", resolved["case_hash"], "status") in world.failed()


# -- each label as the chain recorded it ----------------------------------------------------------


def labels_of(world: World, row: dict[str, Any]) -> list[dict[str, Any]]:
    """A case's label rows, oldest first, each with its values opened."""
    found = []
    held = rows(world.store, "evalset_labels", case_id=row["id"])
    for label in sorted(held, key=lambda label: label["created_at"]):
        body, _ = world.keys.open(
            "label", label["body"], label["set_id"], label["case_id"], label["id"]
        )
        found.append({**label, "values": body["values"]})
    return found


def round_of(world: World, label: dict[str, Any], round_: str) -> None:
    """The assignment a label answers moved to another round by SQL (no key needed)."""
    tamper(
        world.store,
        "UPDATE evalset_assignments SET round = ? WHERE id = ?",
        (round_, label["assignment_id"]),
    )


def every_write_refused(world: World, done: str = "") -> set[str]:
    """`freeze`, `measure`, `assign` and `adjudicate` each refuse the set, naming no case and
    writing nothing; what each said."""
    hidden, users = world.hidden, world.users
    writes: list[tuple[Callable[[], Any], str]] = [
        (lambda: hidden.freeze(world.set_id, max_runs=3, max_age_days=30, resamples=20), "frozen"),
        (lambda: hidden.measure(world.set_id, resamples=20), "measured"),
        (lambda: hidden.assign(world.set_id, users[:2]), "assigned"),
        (lambda: hidden.adjudicate(world.set_id, users[2:]), "assigned"),
        (lambda: hidden.adjudicate(world.set_id, users[2:], org=CLINIC), "assigned"),
    ]
    said = set()
    for write, not_done in writes:
        refused = refused_naming_no_case(world, write, f"{done}.*nothing was {not_done}")
        assert world.set_id in refused
        said.add(refused)
    return said


def test_a_label_deleted_and_a_round_moved_by_sql_never_freeze_one_labellers_answer(
    world: World,
) -> None:
    """The review's probe: two labellers disagree on case 1; with no key, the second label's
    row is deleted, the first label's assignment moved to the adjudication round and the case's
    status set to `resolved`. Before, `freeze` took each label's round from its assignment row,
    so the one label left settled the case `resolved` and the set froze with that labeller's
    answer, never adjudicated, and `rotate --check` was ready. Now each label's round is the one
    its `evalset.labelled` chained, and every label the chain holds must be there: the case's
    labels and status do not hold, and every write that reads them refuses the set."""
    world.labelled()
    disputed = case_of(world, 1)
    first, second = labels_of(world, disputed)
    assert first["values"] != second["values"]
    world.hidden.remove_case(world.set_id, case_of(world, 2)["case_hash"])  # the other dispute
    tamper(world.store, "DELETE FROM evalset_labels WHERE id = ?", (second["id"],))
    round_of(world, first, "adjudication")
    status_of(world, disputed, "resolved")
    said = every_write_refused(world, "whose labels are not those its chain recorded")
    assert all("a status its labels and draw do not give" in one for one in said)
    failed = world.failed()
    assert {("case", disputed["case_hash"], k) for k in ("labels", "status")} <= failed
    assert not world.verified()["ok"]
    assert events(world.store, "evalset.frozen") == []


def test_a_round_moved_by_sql_on_a_case_one_labeller_answered_is_never_frozen(
    world: World,
) -> None:
    """The review's second probe, nothing deleted: a case only its first labeller has answered,
    that label's assignment moved to the adjudication round and the status set `resolved`.
    The label's round is the chained one (`first`), so the case is `sampled` by its labels:
    every write refuses the set and `rotate --check` names the case."""
    world.hidden.add_cases(world.set_id, cases(1))
    world.hidden.sample(world.set_id, cases=1)
    world.users = labellers(world.store, 3)
    world.hidden.assign(world.set_id, world.users[:2])
    shown = world.hidden.next_case(world.users[0], org=CLINIC)
    assert shown is not None
    world.hidden.record_label(shown["assignment"], world.users[0], dict(split(1, 0)), org=CLINIC)
    (row,) = rows(world.store, "evalset_cases", set_id=world.set_id)
    (only,) = labels_of(world, row)
    round_of(world, only, "adjudication")
    status_of(world, row, "resolved")
    every_write_refused(world, "a status its labels and draw do not give")
    assert ("case", row["case_hash"], "status") in world.failed()
    assert not world.verified()["ok"]


def test_an_adjudicators_label_settles_only_a_case_whose_two_first_labels_differ(
    world: World,
) -> None:
    """A second labeller's open assignment moved to the adjudication round by SQL before they
    answer: their label is chained in that round, beside a single first-pass label. Before, the
    two settled the case `resolved` (a majority of two), which every check accepted and freezing
    took. An adjudicator's label now settles only a case whose two first-pass labels differ: the
    case stays `sampled`, which no labels give, so it is found and never frozen."""
    world.hidden.add_cases(world.set_id, cases(1))
    world.hidden.sample(world.set_id, cases=1)
    world.users = labellers(world.store, 3)
    world.hidden.assign(world.set_id, world.users[:2])
    answer = dict(split(0, 0))
    for nth, user in enumerate(world.users[:2]):
        shown = world.hidden.next_case(user, org=CLINIC)
        assert shown is not None
        if nth == 1:  # the second labeller's assignment, still open, moved to adjudication
            round_of(world, {"assignment_id": shown["assignment"]}, "adjudication")
        world.hidden.record_label(shown["assignment"], user, answer, org=CLINIC)
    (row,) = rows(world.store, "evalset_cases", set_id=world.set_id)
    assert row["status"] == "sampled"  # one first-pass label and an adjudicator's: unsettled
    rounds = [e["payload"]["round"] for e in events(world.store, "evalset.labelled")]
    assert rounds == ["first", "adjudication"]
    every_write_refused(world, "a status its labels and draw do not give")
    assert ("case", row["case_hash"], "status") in world.failed()
    assert not world.verified()["ok"]


def test_adjudication_gives_no_third_labeller_a_case_its_labels_do_not_dispute(
    world: World,
) -> None:
    """The review's third probe: a case only its first labeller answered, its status edited from
    `sampled` to `disputed` by SQL. Before, `adjudicate` chose cases by their status, gave this
    one a third labeller, and the case then settled `resolved` on one first-pass label and the
    adjudicator's, which verification, `rotate --check` and freezing all accepted. Now it refuses
    the set, the curators' route too, and no one more is shown the case."""
    world.hidden.add_cases(world.set_id, cases(1))
    world.hidden.sample(world.set_id, cases=1)
    world.users = labellers(world.store, 3)
    world.hidden.assign(world.set_id, world.users[:2])
    shown = world.hidden.next_case(world.users[0], org=CLINIC)
    assert shown is not None
    world.hidden.record_label(shown["assignment"], world.users[0], dict(split(0, 0)), org=CLINIC)
    (row,) = rows(world.store, "evalset_cases", set_id=world.set_id)
    status_of(world, row, "disputed")
    assigned = len(events(world.store, "evalset.assigned"))
    for org in (None, CLINIC):  # the key holder's command, then a curator's route
        adjudicated = functools.partial(world.hidden.adjudicate, world.set_id, world.users[2:])
        refused_naming_no_case(
            world,
            functools.partial(adjudicated, org=org),
            "a status its labels and draw do not give: nothing was assigned",
        )
    assert len(events(world.store, "evalset.assigned")) == assigned
    assert world.hidden.next_case(world.users[2], org=CLINIC) is None
    status_of(world, row, "sampled")  # put back: nothing is disputed, nothing to adjudicate
    assert world.hidden.adjudicate(world.set_id, world.users[2:])["assigned"] == 0
    assert world.verified()["ok"] and world.hidden.check()["ready"]


def test_an_agreed_case_edited_to_disputed_stays_found_after_its_adjudication(
    world: World,
) -> None:
    """An agreed case's status edited to `disputed` by SQL: the chain cannot tell it from a
    disputed one without opening its labels, which `adjudicate` never does (threat model I13), so
    the case is given to a third labeller (the residual the threat model names). Before, that
    label settled it `resolved`, and verification, `rotate --check` and freezing all passed: the
    edit was gone. An adjudicator's label now settles only a case whose two first-pass labels
    differ, so the status stays as edited, and every check still finds it."""
    world.labelled()
    agreed = case_of(world, 0)
    status_of(world, agreed, "disputed")
    world.hidden.adjudicate(world.set_id, world.users[2:])
    label_all(world.hidden, world.users[2:], lambda number, _: split(number, 2))
    (row,) = rows(world.store, "evalset_cases", id=agreed["id"])
    assert row["status"] == "disputed"  # never `resolved`: nothing settles it
    assert len(labels_of(world, row)) == 3
    verified = world.verified()
    assert not verified["ok"] and verified["broken_at"] == seq_added(world, agreed)
    assert ("case", agreed["case_hash"], "status") in world.failed()
    refused_naming_no_case(
        world,
        lambda: world.hidden.freeze(world.set_id, max_runs=3, max_age_days=30, resamples=20),
        "a status its labels and draw do not give: nothing was frozen",
    )


# -- what freezing and measuring read while the store writes --------------------------------------


def a_draw(world: World) -> None:
    world.hidden.sample(world.set_id, cases=1)


def a_label(world: World) -> None:
    shown = world.hidden.next_case(world.users[1], org=CLINIC)
    assert shown is not None
    world.hidden.record_label(shown["assignment"], world.users[1], dict(split(0, 1)), org=CLINIC)


LANDING: dict[str, Callable[[World], None]] = {"a draw": a_draw, "a label": a_label}
READING: dict[str, tuple[Callable[[World], Any], str]] = {
    "measure": (lambda w: w.hidden.measure(w.set_id, resamples=20), "measured"),
    "freeze": (
        lambda w: w.hidden.freeze(w.set_id, max_runs=3, max_age_days=30, resamples=20),
        "frozen",
    ),
}


@pytest.mark.parametrize("command", sorted(READING))
@pytest.mark.parametrize("write", sorted(LANDING))
def test_a_write_landing_while_freeze_or_measure_reads_is_run_again_never_a_tamper(
    world: World, monkeypatch: pytest.MonkeyPatch, write: str, command: str
) -> None:
    """A draw or a label that commits between the rows `freeze` or `measure` reads and the
    chain it reads after them (they read outside the chain's lock): the rows look as no
    commitment gives them, so they are read again under the lock, where they hold, and the
    answer is the one for a set that changed while it was read: run it again. Before, it was a
    refusal sending the operator to look for a tamper that never happened. Nothing is written,
    verification holds, and run again, measuring succeeds."""
    world.hidden.add_cases(world.set_id, cases(4))
    world.hidden.sample(world.set_id, cases=2)
    world.users = labellers(world.store, 2)
    world.hidden.assign(world.set_id, world.users)
    label_all(world.hidden, world.users[:1])
    original = evalsets._chain
    landed: list[str] = []

    def landing(db: Any, set_id: str) -> Any:
        if not landed:
            landed.append(write)
            LANDING[write](world)  # commits in its own transaction, beside the read
        return original(db, set_id)

    monkeypatch.setattr(evalsets, "_chain", landing)
    run, done = READING[command]
    before = unchanged(world)
    with pytest.raises(MendariError, match=f"changed while it was {done}: run it again") as caught:
        run(world)
    assert "audit verify" not in str(caught.value) and landed == [write]
    assert unchanged(world) != before  # what landed, and nothing else: no aggregate, no freeze
    assert events(world.store, "evalset.frozen") == events(world.store, "evalset.measured") == []
    assert world.verified()["ok"] and world.hidden.check()["ready"]
    assert world.hidden.measure(world.set_id, resamples=20)["n"] >= 2


# -- a drawn case's weight ------------------------------------------------------------------------


def test_each_draw_commits_to_every_drawn_cases_weight_with_a_new_salt(world: World) -> None:
    """`evalset.sampled` names each drawn case by its keyed hash with its weight's commitment,
    every drawn case at every draw (so the chain does not show whose weight changed), never a
    weight or a stratum; each case's salt is new at each draw."""
    world.hidden.add_cases(world.set_id, cases(4))
    world.hidden.sample(world.set_id, cases=2)
    first = {row["id"]: attributes(world, row) for row in rows(world.store, "evalset_cases")}
    world.hidden.sample(world.set_id, cases=1)
    second = {row["id"]: attributes(world, row) for row in rows(world.store, "evalset_cases")}
    one, two = (e["payload"]["weight_refs"] for e in events(world.store, "evalset.sampled"))
    assert (len(one), len(two)) == (2, 3) and set(one) < set(two)
    assert not set(one.values()) & set(two.values())  # every commitment new
    for case, body in first.items():
        if "weight" in body:  # drawn by the first draw: re-sealed with a new salt and weight
            assert body["weight_salt"] != second[case]["weight_salt"]
            assert (body["weight"], second[case]["weight"]) == (2.0, 4 / 3)
    assert world.verified()["ok"] and world.hidden.check()["ready"]


def before_the_draw(world: World) -> dict[str, Any]:
    """A drawn case, its attributes put back from a copy made before the draw gave it its
    weight (no key needed: the row's own sealed value, as it was)."""
    world.hidden.add_cases(world.set_id, cases(3))
    saved = {row["id"]: row["attributes"] for row in rows(world.store, "evalset_cases")}
    world.hidden.sample(world.set_id, cases=2)
    row = rows(world.store, "evalset_cases", set_id=world.set_id, status="sampled")[0]
    tamper(
        world.store,
        "UPDATE evalset_cases SET attributes = ? WHERE id = ?",
        (saved[row["id"]], row["id"]),
    )
    return row


def from_an_earlier_draw(world: World) -> dict[str, Any]:
    """A drawn case's attributes put back from a copy made after an earlier draw, whose weight
    and salt that draw committed to, and the latest no longer does."""
    world.hidden.add_cases(world.set_id, cases(4))
    world.hidden.sample(world.set_id, cases=2)
    row = rows(world.store, "evalset_cases", set_id=world.set_id, status="sampled")[0]
    world.hidden.sample(world.set_id, cases=2)
    tamper(
        world.store,
        "UPDATE evalset_cases SET attributes = ? WHERE id = ?",
        (row["attributes"], row["id"]),
    )
    return row


def resealed_with_another_weight(world: World) -> dict[str, Any]:
    """A drawn case's weight re-sealed by someone holding the key, its salt kept."""
    world.hidden.add_cases(world.set_id, cases(3))
    world.hidden.sample(world.set_id, cases=2)
    row = rows(world.store, "evalset_cases", set_id=world.set_id, status="sampled")[0]
    resealed(world, row, {**attributes(world, row), "weight": 9.0})
    return row


WEIGHT_EDITS: dict[str, Callable[[World], dict[str, Any]]] = {
    "put back from before the draw": before_the_draw,
    "put back from an earlier draw": from_an_earlier_draw,
    "re-sealed with another weight": resealed_with_another_weight,
}


@pytest.mark.parametrize("edit", sorted(WEIGHT_EDITS))
def test_a_drawn_cases_weight_its_draw_did_not_commit_to_breaks_verify_and_check(
    world: World, edit: str
) -> None:
    """A drawn case's weight changed behind the store's back: `verify` breaks at the latest
    `evalset.sampled`, which committed to the weight, `rotate --check` names the case, and
    `freeze` and `measure` refuse the set, naming no case. The case's own commitment still
    holds: before LP-11p, each of these passed."""
    row = WEIGHT_EDITS[edit](world)
    draw = events(world.store, "evalset.sampled")[-1]
    verified = world.verified()
    assert not verified["ok"] and verified["broken_at"] == draw["seq"], verified
    failed = world.failed()
    assert ("case", row["case_hash"], "weight") in failed
    assert ("case", row["case_hash"], "commitment") not in failed  # its `case_ref` holds
    writes: list[Callable[[], Any]] = [
        lambda: world.hidden.freeze(world.set_id, max_runs=3, max_age_days=30, drop_unsampled=True),
        lambda: world.hidden.measure(world.set_id, resamples=20),
    ]
    for write in writes:
        refused_naming_no_case(
            world, write, "holds 1 case\\(s\\) with a weight its draw's commitment does not cover"
        )


def test_a_weight_on_a_case_no_draw_took_breaks_at_the_cases_event(world: World) -> None:
    world.hidden.add_cases(world.set_id, cases(3))
    world.hidden.sample(world.set_id, cases=2)
    (undrawn,) = rows(world.store, "evalset_cases", set_id=world.set_id, status="draft")
    resealed(world, undrawn, {**attributes(world, undrawn), "weight": 3.0})
    verified = world.verified()
    assert not verified["ok"] and verified["broken_at"] == seq_added(world, undrawn)
    assert ("case", undrawn["case_hash"], "weight") in world.failed()


def test_a_frozen_sets_weight_resealed_still_breaks_at_its_freeze(world: World) -> None:
    """On a frozen set, a weight its draw did not commit to breaks verification at the set's
    `evalset.frozen`, as the content does; `rotate --check` names the case and the set."""
    world.settled()
    world.hidden.freeze(world.set_id, max_runs=3, max_age_days=30, resamples=20)
    (freeze,) = events(world.store, "evalset.frozen")
    row = case_of(world, 0)
    resealed(world, row, {**attributes(world, row), "weight": 2.0})
    verified = world.verified()
    assert not verified["ok"] and verified["broken_at"] == freeze["seq"]
    failed = world.failed()
    assert ("case", row["case_hash"], "weight") in failed and ("set", None, "commitment") in failed


def test_a_draw_takes_what_the_chain_drew_never_a_status_edited_by_sql(world: World) -> None:
    """Which cases were drawn before is what the latest draw committed to: a drawn case edited to
    `draft` is not drawn again (and still breaks verification), and an undrawn one edited to
    `sampled` is still left to draw, never committed to as drawn without a draw."""
    world.hidden.add_cases(world.set_id, cases(4))
    world.hidden.sample(world.set_id, cases=1)
    (drawn,) = rows(world.store, "evalset_cases", set_id=world.set_id, status="sampled")
    left = rows(world.store, "evalset_cases", set_id=world.set_id, status="draft")
    status_of(world, drawn, "draft")
    status_of(world, left[0], "sampled")
    # Three cases are left by the chain, whatever their statuses: all three are drawn.
    assert world.hidden.sample(world.set_id, cases=3)["drawn"] == 3
    (latest,) = events(world.store, "evalset.sampled")[-1:]
    assert set(latest["payload"]["weight_refs"]) == {
        row["case_hash"] for row in rows(world.store, "evalset_cases", set_id=world.set_id)
    }
    verified = world.verified()  # the drawn case's status stays as edited: found
    assert not verified["ok"] and verified["broken_at"] == seq_added(world, drawn)
    status_of(world, drawn, "sampled")
    assert world.verified()["ok"]


# Seven cases: five routine, two urgent; six: the same without one routine case.
WITH_ONE_MORE = ("routine", "routine", "urgent", "routine", "routine", "urgent", "routine")
WITHOUT_IT = ("routine", "routine", "urgent", "routine", "urgent", "routine")


def drawn_weights(world: World, set_id: str) -> list[tuple[str, float]]:
    """Each case of a set as (stratum, weight), sorted: what its weighted figures are made of."""
    found = []
    for row in rows(world.store, "evalset_cases", set_id=set_id):
        body, _ = world.keys.open("attributes", row["attributes"], set_id, row["id"])
        found.append((body["attributes"]["stratum"], body["weight"]))
    return sorted(found)


def routine_share(weights: list[tuple[str, float]]) -> float | None:
    """A weighted figure: the weighted share of routine cases (Hajek's estimate)."""
    from mendari.evals.stats import weighted_proportion

    hits = [stratum == "routine" for stratum, _ in weights]
    return weighted_proportion(hits, [weight for _, weight in weights]).estimate


def test_removing_a_drawn_case_reweighs_its_stratum_as_if_it_never_was(world: World) -> None:
    """A drawn case removed: its stratum's other drawn cases are re-weighted at once (N_h and n_h
    without it), every drawn case re-sealed with a new salt and the new weights committed to as a
    draw does (`evalset.sampled`, `n` 0), so a freeze straight after commits to the weights, and
    gives the weighted figures, of a set that never held the case. Before, the weights stayed
    those of the draw (5/3 for routine) until the next draw."""
    world.users = labellers(world.store, 2)
    other = a_set(world.hidden, world.pack)
    # The set that held it: 3 of 5 routine drawn (5/3), both urgent (1); a routine one removed.
    world.hidden.add_cases(world.set_id, cases(7, strata=WITH_ONE_MORE))
    world.hidden.sample(world.set_id, per_stratum=3)
    removed = next(
        row
        for row in rows(world.store, "evalset_cases", set_id=world.set_id, status="sampled")
        if attributes(world, row)["attributes"]["stratum"] == "routine"
    )
    # The set that never held it: 2 of 4 routine drawn (2), both urgent (1).
    world.hidden.add_cases(other, cases(6, start=100, strata=WITHOUT_IT))
    world.hidden.sample(other, per_stratum=2)
    for set_id in (world.set_id, other):
        world.hidden.assign(set_id, world.users)
    label_all(world.hidden, world.users)
    drawn_before = events(world.store, "evalset.sampled")[0]["payload"]["weight_refs"]
    world.hidden.remove_case(world.set_id, removed["case_hash"])
    redrawn = events(world.store, "evalset.sampled")[-1]
    assert redrawn["decision_id"] == f"evalset:{world.set_id}" and redrawn["payload"]["n"] == 0
    refs = redrawn["payload"]["weight_refs"]
    assert set(refs) == set(drawn_before) - {removed["case_hash"]}
    assert not set(refs.values()) & set(drawn_before.values())  # every commitment new
    assert world.verified()["ok"] and world.hidden.check()["ready"]
    for set_id in (world.set_id, other):
        world.hidden.freeze(set_id, max_runs=3, max_age_days=30, drop_unsampled=True, resamples=20)
    held, never = drawn_weights(world, world.set_id), drawn_weights(world, other)
    assert held == never == [("routine", 2.0), ("routine", 2.0), ("urgent", 1.0), ("urgent", 1.0)]
    assert routine_share(held) == routine_share(never) == 2 / 3
    assert world.verified()["ok"]


def test_removing_an_undrawn_case_reweighs_its_stratum_too(world: World) -> None:
    """An undrawn case removed from a set with a draw leaves its stratum smaller: N_h without it."""
    world.hidden.add_cases(world.set_id, cases(4))
    world.hidden.sample(world.set_id, cases=2)
    undrawn = rows(world.store, "evalset_cases", set_id=world.set_id, status="draft")[0]
    world.hidden.remove_case(world.set_id, undrawn["case_hash"])
    drawn = rows(world.store, "evalset_cases", set_id=world.set_id, status="sampled")
    assert [attributes(world, row)["weight"] for row in drawn] == [1.5, 1.5]  # 3 / 2, not 4 / 2
    assert world.verified()["ok"] and world.hidden.check()["ready"]


# -- every set's stored aggregates ----------------------------------------------------------------


def measured_twice(world: World) -> str:
    """The set measured twice; its first aggregates' sealed value, as stored then."""
    world.labelled()
    world.hidden.measure(world.set_id, resamples=20)
    (row,) = rows(world.store, "evalset_sets", id=world.set_id)
    world.hidden.measure(world.set_id, seed=1, resamples=20)
    return str(row["aggregates"])


def put_back(world: World) -> None:
    earlier = measured_twice(world)
    tamper(
        world.store, "UPDATE evalset_sets SET aggregates = ? WHERE id = ?", (earlier, world.set_id)
    )


def resealed_aggregates(world: World) -> None:
    """The aggregates re-sealed by someone holding the key, their salt kept: a count changed."""
    measured_twice(world)
    (row,) = rows(world.store, "evalset_sets", id=world.set_id)
    stored, _ = world.keys.open("aggregates", row["aggregates"], world.set_id)
    stored["aggregates"]["n"] += 10
    sealed = world.keys.seal("aggregates", stored, world.set_id)
    tamper(
        world.store, "UPDATE evalset_sets SET aggregates = ? WHERE id = ?", (sealed, world.set_id)
    )


def resealed_as_before(world: World) -> None:
    """The aggregates re-sealed in the shape they had before LP-11p, without their salt."""
    measured_twice(world)
    (row,) = rows(world.store, "evalset_sets", id=world.set_id)
    stored, _ = world.keys.open("aggregates", row["aggregates"], world.set_id)
    sealed = world.keys.seal("aggregates", stored["aggregates"], world.set_id)
    tamper(
        world.store, "UPDATE evalset_sets SET aggregates = ? WHERE id = ?", (sealed, world.set_id)
    )


def removed(world: World) -> None:
    measured_twice(world)
    tamper(world.store, "UPDATE evalset_sets SET aggregates = NULL WHERE id = ?", (world.set_id,))


AGGREGATE_EDITS: dict[str, Callable[[World], None]] = {
    "put back from an earlier measure": put_back,
    "re-sealed with another count": resealed_aggregates,
    "re-sealed without their salt": resealed_as_before,
    "removed": removed,
}


@pytest.mark.parametrize("edit", sorted(AGGREGATE_EDITS))
def test_stored_aggregates_their_chain_did_not_commit_to_break_verify_and_are_never_shown(
    world: World,
    edit: str,
    db_target: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A draft set's stored aggregates changed behind the store's back: `verify` breaks at the
    latest `evalset.measured`, `rotate --check` names the set, and `show`, the curators' read
    (`summary`) and `mendari evalset show` refuse them, naming the set only. Before LP-11p each
    passed, and `show` read them."""
    AGGREGATE_EDITS[edit](world)
    latest = events(world.store, "evalset.measured")[-1]
    verified = world.verified()
    assert not verified["ok"] and verified["broken_at"] == latest["seq"], verified
    assert ("set", None, "aggregates") in world.failed()
    for read in (
        lambda: world.hidden.show(world.set_id),
        lambda: world.hidden.summary(world.set_id, org=CLINIC),
    ):
        with pytest.raises(MendariError, match=CHANGED) as caught:
            read()
        said = str(caught.value)
        assert world.set_id in said
        assert not any(row["case_hash"] in said for row in rows(world.store, "evalset_cases"))
    deployed(monkeypatch, world.keys)
    code = main(["evalset", "show", world.set_id, "--db", db_target])
    _, err = capsys.readouterr()
    assert code == 1 and CHANGED in err
    world.hidden.measure(world.set_id, resamples=20)  # measured again: the commitment holds
    assert world.verified()["ok"] and world.hidden.show(world.set_id)["aggregates"] is not None


def test_a_frozen_sets_aggregates_put_back_from_before_its_freeze_break_at_its_freeze(
    world: World,
) -> None:
    """The aggregates freezing stored are committed to by `evalset.frozen`: those of the measure
    before it, put back, break verification there, and `show` refuses them."""
    world.settled()
    world.hidden.measure(world.set_id, resamples=20)
    (row,) = rows(world.store, "evalset_sets", id=world.set_id)
    world.hidden.freeze(world.set_id, max_runs=3, max_age_days=30, resamples=20)
    (freeze,) = events(world.store, "evalset.frozen")
    tamper(
        world.store,
        "UPDATE evalset_sets SET aggregates = ? WHERE id = ?",
        (row["aggregates"], world.set_id),
    )
    verified = world.verified()
    assert not verified["ok"] and verified["broken_at"] == freeze["seq"]
    assert ("set", None, "aggregates") in world.failed()
    with pytest.raises(MendariError, match=CHANGED):
        world.hidden.show(world.set_id)


def test_aggregates_no_event_stored_are_unchained_and_never_shown(world: World) -> None:
    """Aggregates sealed into a set that was never measured, by someone holding the key: no
    event stored them, so verification counts them `unchained` and `show` refuses them."""
    world.labelled()
    measured = world.hidden.measure(world.set_id, resamples=20)
    other = a_set(world.hidden, world.pack)
    wrapped = {"salt": base64.b64encode(bytes(32)).decode(), "aggregates": measured}
    sealed = world.keys.seal("aggregates", wrapped, other)
    tamper(world.store, "UPDATE evalset_sets SET aggregates = ? WHERE id = ?", (sealed, other))
    verified = world.verified()
    assert not verified["ok"] and verified["unchained"] == 1 and "broken_at" not in verified
    with pytest.raises(MendariError, match=CHANGED):
        world.hidden.show(other)


def test_the_curators_read_refuses_aggregates_put_back_naming_the_set_only(
    db_target: str, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`GET /api/evalsets/{set_id}`: aggregates put back from an earlier measure are refused
    (`400`), the answer naming the set and no case; measured again, they are read."""
    keys = memberships(monkeypatch)
    with started(db_target, inbox_pack, keys) as routes:
        curator = routes.curator()
        set_id = routes.made(2)
        people = [routes.person(f"labeller-{n}")[0] for n in "ab"]
        routes.hidden.sample(set_id, cases=2)
        routes.hidden.assign(set_id, people)
        for person in people:
            routes.labelled(person, {"destination": "nurse", "urgency": "routine"})
        routes.hidden.measure(set_id, resamples=20)
        (row,) = rows(routes.store, "evalset_sets", id=set_id)
        routes.hidden.measure(set_id, seed=1, resamples=20)
        assert routes.call("GET", url(SET, set_id), curator).status_code == 200
        tamper(
            routes.store,
            "UPDATE evalset_sets SET aggregates = ? WHERE id = ?",
            (row["aggregates"], set_id),
        )
        answered = routes.call("GET", url(SET, set_id), curator)
        assert answered.status_code == 400 and CHANGED in answered.json()["detail"]
        assert set_id in answered.text
        hashes = [r["case_hash"] for r in rows(routes.store, "evalset_cases", set_id=set_id)]
        assert not any(hashed in answered.text for hashed in hashes)
        routes.hidden.measure(set_id, resamples=20)
        again = routes.call("GET", url(SET, set_id), curator)
        assert again.status_code == 200 and again.json()["aggregates"]["n"] == 2


# -- sets made before these commitments ----------------------------------------------------------


def as_before(store: AuditStore, keys: EvalsetKeys) -> None:
    """The database as the store wrote it before LP-11p: each draw without its weight
    commitments, each store of aggregates without theirs (the chain recomputed after them, as
    the store would have chained it), each case's attributes without a weight's salt and each
    set's aggregates without theirs."""
    with store._db.session(exclusive=True) as db:
        for row in db.all("SELECT id, set_id, attributes FROM evalset_cases"):
            body, _ = keys.open("attributes", row["attributes"], row["set_id"], row["id"])
            if "weight_salt" in body:
                del body["weight_salt"]
                sealed = keys.seal("attributes", body, row["set_id"], row["id"])
                db.all("UPDATE evalset_cases SET attributes = ? WHERE id = ?", (sealed, row["id"]))
        for row in db.all("SELECT id, aggregates FROM evalset_sets WHERE aggregates IS NOT NULL"):
            stored, _ = keys.open("aggregates", row["aggregates"], row["id"])
            sealed = keys.seal("aggregates", stored["aggregates"], row["id"])
            db.all("UPDATE evalset_sets SET aggregates = ? WHERE id = ?", (sealed, row["id"]))
        prev = GENESIS
        for event in db.all("SELECT * FROM audit_events ORDER BY seq"):
            payload = json.loads(event["payload"])
            for name in ("weight_refs", "aggregates_ref"):
                payload.pop(name, None)
            body = _canonical(payload)
            chained_now = _chain_hash(
                prev, event["decision_id"], event["event"], body, event["created_at"]
            )
            db.all(
                "UPDATE audit_events SET payload = ?, prev_hash = ?, hash = ? WHERE seq = ?",
                (body, prev, chained_now, event["seq"]),
            )
            prev = chained_now


def test_sets_made_before_the_commitments_verify_as_before_and_say_how_many_rows_predate(
    world: World,
    db_target: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A draft set drawn and measured, and a frozen one, as the store wrote them before LP-11p:
    `verify` and `rotate --check` hold, each counting the rows no commitment covers yet
    (`predating`: the drawn cases and the sets' aggregates), `show` reads the aggregates as they
    were sealed, and nothing names a case. A status its labels do not give is still found, and
    the set still freezes; a new draw commits to every drawn case's weight again."""
    world.labelled()  # 3 drawn
    world.hidden.measure(world.set_id, resamples=20)
    frozen = a_set(world.hidden, world.pack)
    world.settled(set_id=frozen)  # 2 drawn, once the three-way split is removed
    world.hidden.freeze(frozen, max_runs=3, max_age_days=30, resamples=20)
    as_before(world.store, world.keys)
    assert all("weight_refs" not in e["payload"] for e in events(world.store, "evalset.sampled"))
    verified = world.verified()
    assert verified["ok"], verified
    assert verified["evalsets"]["predating"] == 3 + 2 + 2  # cases, then both sets' aggregates
    checked = world.hidden.check()
    assert checked["ready"] and checked["predating"] == 3 + 2 + 2
    for set_id in (world.set_id, frozen):
        shown = world.hidden.show(set_id)["aggregates"]
        assert shown is not None and shown["set"] == set_id
    deployed(monkeypatch, world.keys)
    assert main(["audit", "verify", "--evalsets", "--db", db_target]) == 0
    out, _ = capsys.readouterr()
    assert json.loads(out)["evalsets"]["predating"] == 7
    for row in rows(world.store, "evalset_cases"):
        assert row["id"] not in out and row["case_hash"] not in out
    # A status its labels do not give, on the old draft: still found, by the labels alone.
    disputed = case_of(world, 1)
    status_of(world, disputed, "agreed")
    broken = world.verified()
    assert not broken["ok"] and broken["broken_at"] == seq_added(world, disputed)
    status_of(world, disputed, "disputed")
    # The old draft settles and freezes, its weights taken as they were drawn.
    world.hidden.adjudicate(world.set_id, world.users[2:])
    label_all(world.hidden, world.users[2:], lambda number, _: split(number, 2))
    world.hidden.remove_case(world.set_id, case_of(world, 2)["case_hash"])
    world.hidden.add_cases(world.set_id, cases(1, start=3))
    # A new draw commits to every drawn case's weight, the old ones' too.
    world.hidden.sample(world.set_id, cases=1)
    assert len(events(world.store, "evalset.sampled")[-1]["payload"]["weight_refs"]) == 3
    world.hidden.assign(world.set_id, world.users[:2])
    label_all(world.hidden, world.users[:2], lambda number, nth: split(0, nth))
    world.hidden.freeze(world.set_id, max_runs=3, max_age_days=30, resamples=20)
    after = world.verified()
    assert after["ok"] and after["evalsets"]["predating"] == 2 + 1  # the other set's
    assert world.hidden.check()["ready"]


# -- what a verification reads while the store writes ----------------------------------------------


def test_a_draw_a_label_or_a_measure_written_while_verify_reads_is_never_a_break(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each write lands between the events a verification reads and the rows it reads after
    them: a label that settles a case's status, a measure that replaces the aggregates, a draw
    that re-seals every drawn case's weight. Each is chained after the events read, so it
    excuses what it wrote, and the next verification checks it."""
    world.hidden.add_cases(world.set_id, cases(4))
    world.hidden.sample(world.set_id, cases=2)
    first, second = world.users = labellers(world.store, 2)
    world.hidden.assign(world.set_id, world.users)
    label_all(world.hidden, [first])
    shown = world.hidden.next_case(second, org=CLINIC)
    assert shown is not None
    world.hidden.record_label(shown["assignment"], second, dict(split(0, 1)), org=CLINIC)
    world.hidden.measure(world.set_id, resamples=20)
    assert world.verified()["ok"]

    def landing(write: Callable[[], Any]) -> dict[str, Any]:
        original = AuditStore._evalset_rows

        def read_after(self: AuditStore, db: Any, after: int) -> dict[str, Any]:
            write()
            return original(self, db, after)

        with monkeypatch.context() as patched:
            patched.setattr(AuditStore, "_evalset_rows", read_after)
            return world.verified()

    waiting = world.hidden.next_case(second, org=CLINIC)
    assert waiting is not None
    answer = dict(split(0, 1))
    settled = landing(
        lambda: world.hidden.record_label(waiting["assignment"], second, answer, org=CLINIC)
    )
    assert settled["ok"], settled
    assert statuses_of(world) == {"agreed": 2, "draft": 2}
    measured = landing(lambda: world.hidden.measure(world.set_id, seed=2, resamples=20))
    assert measured["ok"], measured
    drawn = landing(lambda: world.hidden.sample(world.set_id, cases=1))
    assert drawn["ok"], drawn
    assert world.verified()["ok"] and world.hidden.check()["ready"]


def statuses_of(world: World) -> dict[str, int]:
    return {k: v for k, v in world.hidden.progress(world.set_id)["statuses"].items() if v}


def test_the_commands_print_the_predating_count_and_no_case(
    tmp_path: Path,
    inbox_pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`mendari audit verify --evalsets` and `mendari evalset rotate --check` each print how many
    rows predate the commitments (0 on a set the store made now), never a case."""
    db = str(tmp_path / "mendari.db")
    key, index = evalset_on(monkeypatch)
    served(db, inbox_pack)
    keys = EvalsetKeys(key, index)
    with AuditStore(db) as store:
        hidden = EvalsetStore(store, keys)
        set_id = a_set(hidden, inbox_pack)
        written = [hand_written(f"A note about a sore wrist, number {n}") for n in range(3)]
        hidden.add_cases(set_id, written)
        hidden.sample(set_id, cases=2)
        hashes = [row["case_hash"] for row in rows(store, "evalset_cases")]
    for argv, counts in (
        (["audit", "verify", "--evalsets"], lambda said: said["evalsets"]),
        (["evalset", "rotate", "--check"], lambda said: said),
    ):
        assert main([*argv, "--db", db]) == 0
        out, _ = capsys.readouterr()
        assert counts(json.loads(out))["predating"] == 0
        assert not any(hashed in out for hashed in hashes)
