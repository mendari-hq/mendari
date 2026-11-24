# SPDX-License-Identifier: Apache-2.0
"""Grading rounds of written output, in the store (`core/written.py`, ADR-0025): a round made
from a run, its items given to graders, a grader's read and grades, and the closing that
computes the report and erases the texts. The grader's two calls (`next_item`,
`record_grades`) are tested here, at the store; the routes over them, in
tests/test_written_routes.py.

- A grader is shown the written text, the context and the rubric's words, never a judge's grade
  or another grader's; the second grader's view is the same before and after the first grades,
  and neither is told how their grade compares. No report exists before the round closes.
- Only a labeller of the round's organization who does not manage its sets is assigned; an
  assignment whose membership ends moves to another grader on the next run.
- Texts follow notes: sealed under the PHI key in `encrypt`, plain in `off` (never in a
  protected database), refused in `hash`; re-sealed by a key rotation, erased by retention and
  by closing. The chain holds ids, hashes and counts.
On SQLite and Postgres (`db_target`); keys are made per test, texts are synthetic."""

from __future__ import annotations

import base64
import json
import os
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from mendari import AuditStore, Decision, load_pack
from mendari.cli import main
from mendari.core import accounts
from mendari.core.evalsets import NO_ASSIGNMENT
from mendari.core.pack import Pack
from mendari.core.phi import PhiPolicy, key_id
from mendari.core.written import (
    CLOSED,
    CLOSING_NOTE,
    NO_ROUND,
    OPEN,
    REPORT_MISMATCH,
    NoRound,
    WrittenStore,
    grade_ref,
    rubric_header,
)
from mendari.errors import LabelError, MendariError, NoAssignment
from mendari.evals import resolve_dataset, written
from mendari.evals.written import INSUFFICIENT, WrittenLedger, WrittenRun
from mendari.providers.judge import Judge
from mendari.standards import audit_fhir, fhir_structure
from tests.conftest import HASH_KEY_B64, PHI_KEY_B64, ROOT
from tests.test_evalset_labelling import ISSUER, membership_of, person
from tests.test_evalset_store import CLINIC, chained, served, tamper

MINI = ROOT / "tests" / "fixtures" / "mini-agents" / "written-mini"
RUBRIC = "reply_quality"
KEY, HASH = os.urandom(32), os.urandom(32)
PASS = {"safe_to_send": "yes", "addresses_request": "fully", "plain_language": "yes"}
FAIL = {"safe_to_send": "no", "addresses_request": "partly", "plain_language": "yes"}
TABLES = ("written_rounds", "written_items", "written_assignments", "audit_events")
# What each event's payload holds, and nothing else.
PAYLOADS = {
    "written.round_created": {
        *("round", "org", "pack_id", "pack_digest", "rubric", "rubric_sha256", "ledger", "items"),
        "seed",
    },
    "written.assigned": {"round", "org", "item", "assignment", "grader"},
    "written.item_read": {"round", "org", "item", "assignment", "grader"},
    "written.graded": {"round", "org", "item", "assignment", "grader", "grade_ref"},
    "written.round_closed": {"round", "org", "graded", "erased", "report_sha256", "by"},
    "written.erased": {"round", "org", "erased", "reason", "retention_days"},
}


@pytest.fixture(scope="module")
def pack() -> Pack:
    return load_pack(MINI)


@pytest.fixture(scope="module")
def the_run(pack: Pack) -> WrittenRun:
    decision = Decision(pack, origin_cap=False)
    dataset = resolve_dataset(pack, "written")
    return written.run(decision, dataset, RUBRIC, judge=Judge(pack, RUBRIC, mode="canned"))


@dataclass
class World:
    store: AuditStore
    rounds: WrittenStore
    pack: Pack
    run: WrittenRun

    def round(self, sample: int = 10, **options: Any) -> str:
        made = self.rounds.create_round(
            CLINIC, self.pack, RUBRIC, self.run, sample=sample, created_by="ops", **options
        )
        return str(made["round"])


@pytest.fixture
def world(db_target: str, pack: Pack, the_run: WrittenRun) -> Iterator[World]:
    with AuditStore(db_target, phi=PhiPolicy("encrypt", KEY, HASH)) as store:
        yield World(store, WrittenStore(store), pack, the_run)


def graders(store: AuditStore, count: int, *, org: str = CLINIC) -> list[str]:
    return [person(store, f"grader-{org}-{n}", org=org) for n in range(count)]


def rows(store: AuditStore, table: str, **where: Any) -> list[dict[str, Any]]:
    clause = " AND ".join(f"{column} = ?" for column in where) or "1 = 1"
    with store._db.session(exclusive=False) as db:
        return db.all(f"SELECT * FROM {table} WHERE {clause} ORDER BY id", tuple(where.values()))


def dump(store: AuditStore) -> str:
    with store._db.session(exclusive=False) as db:
        return json.dumps(
            {table: db.all(f"SELECT * FROM {table} ORDER BY 1") for table in TABLES}, default=str
        )


def events(store: AuditStore, kind: str) -> list[dict[str, Any]]:
    return [event for event in chained(store) if event["event"] == kind]


def grade_all(world: World, users: list[str], values: dict[str, str] = PASS) -> int:
    """Each grader grades everything that waits for them; how many grades were given."""
    given = 0
    for user in users:
        while (shown := world.rounds.next_item(user, org=CLINIC)) is not None:
            world.rounds.record_grades(shown["assignment"], user, values, org=CLINIC)
            given += 1
    return given


# -- a round is made from a run -----------------------------------------------------------------


def test_a_round_keeps_a_seeded_sample_sealed_and_the_ledger_whole(world: World) -> None:
    made = world.rounds.create_round(
        CLINIC, world.pack, RUBRIC, world.run, sample=6, seed=3, created_by="ops"
    )
    assert made == {
        "round": made["round"],
        "items": 6,
        "ledger": world.run.ledger.id,
        "state": OPEN,
        "seed": 3,
    }
    (kept,) = rows(world.store, "written_rounds")
    assert (kept["org"], kept["pack_id"], kept["state"]) == (CLINIC, world.pack.id, OPEN)
    assert kept["pack_digest"] == world.pack.digest and kept["report"] is None
    assert kept["rubric_sha256"] == written.rubric_sha256(world.pack.manifest, RUBRIC)
    assert WrittenLedger.loads(kept["ledger"].encode()).id == world.run.ledger.id
    assert json.loads(kept["header"]) == {**rubric_header(world.pack, RUBRIC), "seed": 3}
    items = rows(world.store, "written_items")
    assert len(items) == 6 and {item["sealed"] for item in items} == {1}
    by_id = {item.item: item for item in world.run.items}
    for item in items:
        assert "Thank you" not in item["body"] and "{" not in item["body"]  # sealed, not JSON
        body, opened = world.store.phi.open("written", item["body"], item["id"])
        assert opened == key_id(KEY)
        assert body == {"written": by_id[item["item"]].text, "context": by_id[item["item"]].context}
        # Bound to its own row: it opens for no other.
        other = next(i for i in items if i["id"] != item["id"])
        with pytest.raises(MendariError):
            world.store.phi.open("written", item["body"], other["id"])
    # Seeded: the same draw again, another with another seed; never more than the run has.
    again = world.rounds.create_round(
        CLINIC, world.pack, RUBRIC, world.run, sample=6, seed=3, created_by="ops"
    )
    drawn = {item["item"] for item in items}
    assert {i["item"] for i in rows(world.store, "written_items", round_id=again["round"])} == drawn
    other_seed = world.rounds.create_round(
        CLINIC, world.pack, RUBRIC, world.run, sample=6, seed=4, created_by="ops"
    )
    assert {
        i["item"] for i in rows(world.store, "written_items", round_id=other_seed["round"])
    } != drawn
    (created,) = [
        e
        for e in events(world.store, "written.round_created")
        if e["payload"]["round"] == made["round"]
    ]
    assert created["decision_id"] == f"written:{made['round']}"
    assert created["payload"] == {
        "round": made["round"],
        "org": CLINIC,
        "pack_id": world.pack.id,
        "pack_digest": world.pack.digest,
        "rubric": RUBRIC,
        "rubric_sha256": kept["rubric_sha256"],
        "ledger": world.run.ledger.id,
        "items": 6,
        "seed": 3,
    }
    assert world.store.verify()["ok"]


@pytest.mark.parametrize(
    ("options", "said"),
    [
        ({"sample": 0}, "a round samples 1 output or more"),
        ({"sample": 11}, "the run has 10 outputs: a round samples at most that many"),
        ({"sample": True}, "a round samples 1 output or more"),
        ({"sample": 3, "seed": -1}, "the seed is a whole number, 0 or more"),
    ],
)
def test_a_round_that_cannot_be_made_is_refused(
    world: World, options: dict[str, Any], said: str
) -> None:
    with pytest.raises(MendariError, match=said):
        world.rounds.create_round(
            CLINIC, world.pack, RUBRIC, world.run, created_by="ops", **options
        )
    assert rows(world.store, "written_rounds") == []


def test_a_run_of_another_rubric_or_agent_is_refused(world: World) -> None:
    with pytest.raises(MendariError, match="another agent or rubric"):
        world.rounds.create_round(
            CLINIC, world.pack, "draft_check", world.run, sample=1, created_by="ops"
        )
    with pytest.raises(MendariError, match="has no rubric 'tone'"):
        world.rounds.create_round(CLINIC, world.pack, "tone", world.run, sample=1, created_by="ops")
    header = json.loads(json.dumps(world.run.ledger.header))
    header["rubric"]["sha256"] = "0" * 64
    reworded = WrittenRun(WrittenLedger(header, world.run.ledger.cases), world.run.items)
    with pytest.raises(MendariError, match="another wording of the rubric"):
        world.rounds.create_round(CLINIC, world.pack, RUBRIC, reworded, sample=1, created_by="ops")
    with pytest.raises(MendariError):
        world.rounds.create_round(
            "Not An Org", world.pack, RUBRIC, world.run, sample=1, created_by="x"
        )
    assert rows(world.store, "written_rounds") == []


def test_texts_follow_notes_under_each_phi_mode(
    db_target: str, pack: Pack, the_run: WrittenRun
) -> None:
    # `hash` keeps no patient text: refused.
    with AuditStore(db_target, phi=PhiPolicy("hash", b"", HASH)) as store:
        with pytest.raises(MendariError, match="PHI mode hash keeps no patient text"):
            WrittenStore(store).create_round(
                CLINIC, pack, RUBRIC, the_run, sample=2, created_by="o"
            )
        assert rows(store, "written_rounds") == []
    # A database a protected mode kept never takes plaintext again.
    with AuditStore(db_target) as store, pytest.raises(MendariError, match="is protected"):
        WrittenStore(store).create_round(CLINIC, pack, RUBRIC, the_run, sample=2, created_by="o")


def test_under_off_in_an_unprotected_database_the_texts_are_plain(
    tmp_path: Path, pack: Pack, the_run: WrittenRun
) -> None:
    with AuditStore(tmp_path / "local.db") as store:
        world = World(store, WrittenStore(store), pack, the_run)
        round_id = world.round(3)
        items = rows(store, "written_items")
        assert {item["sealed"] for item in items} == {0}
        assert all(json.loads(item["body"])["written"].startswith("Thank you") for item in items)
        a, b = graders(store, 2)
        world.rounds.assign(round_id, [a, b])
        shown = world.rounds.next_item(a, org=CLINIC)
        assert shown is not None and shown["written"]["text"].startswith("Thank you")
        world.rounds.close(round_id, by="ops")
        assert {item["body"] for item in rows(store, "written_items")} == {None}


# -- assignment ---------------------------------------------------------------------------------


def test_one_grader_an_item_and_a_seeded_share_a_second_different_one(world: World) -> None:
    round_id = world.round(10)
    a, b, c = graders(world.store, 3)
    made = world.rounds.assign(round_id, [a, b, c], double=0.25)
    assert made["assigned"] == 13 and made["withdrawn"] == 0  # 10, and ceil(0.25 x 10) = 3 twice
    assert set(made) == {"round", "assigned", "withdrawn", "graders"}  # never which are twice
    assert sum(made["graders"].values()) == 13
    assert max(made["graders"].values()) - min(made["graders"].values()) <= 1  # balanced
    held = rows(world.store, "written_assignments")
    by_item = Counter(row["item_id"] for row in held)
    assert sorted(by_item.values()) == [1] * 7 + [2] * 3
    for item, count in by_item.items():
        users = [row["user_id"] for row in held if row["item_id"] == item]
        assert len(set(users)) == count  # a second grader is another grader
    assert {row["membership_id"] for row in held} == {
        membership_of(world.store, u) for u in (a, b, c)
    }
    assert world.rounds.assign(round_id, [a, b, c], double=0.25)["assigned"] == 0
    assigned = events(world.store, "written.assigned")
    assert len(assigned) == 13 and all(
        set(e["payload"]) == PAYLOADS["written.assigned"] for e in assigned
    )
    # The same seed gives the same pairs in another round of the same items.
    second = world.round(10)
    world.rounds.assign(second, [a, b, c], double=0.25)
    items = {row["id"]: row["item"] for row in rows(world.store, "written_items")}

    def pairs(of: str) -> set[tuple[str, str]]:
        return {
            (items[row["item_id"]], row["user_id"])
            for row in rows(world.store, "written_assignments", round_id=of)
        }

    assert pairs(round_id) == pairs(second)


def test_who_may_be_assigned(world: World) -> None:
    round_id = world.round(4)
    a, b = graders(world.store, 2)
    curator = person(world.store, "curator", roles=["admin", "labeller"])
    stranger = person(world.store, "stranger", org="clinic-b")
    viewer = person(world.store, "viewer", roles=["viewer"])
    with pytest.raises(MendariError, match="manages hidden sets"):
        world.rounds.assign(round_id, [a, curator])
    for nobody in (stranger, viewer, accounts.person_id(ISSUER, "nobody")):
        with pytest.raises(MendariError, match="holds no labeller role"):
            world.rounds.assign(round_id, [a, nobody])
    with pytest.raises(MendariError, match="at least 2 different labellers"):
        world.rounds.assign(round_id, [a])  # a second grader needs two graders
    with pytest.raises(MendariError, match="at least 2 different labellers"):
        world.rounds.assign(round_id, [a, a])
    with pytest.raises(MendariError, match="named by their user id"):
        world.rounds.assign(round_id, ["someone@example.org", a])
    for bad in (-0.1, 1.5, float("nan"), True):
        with pytest.raises(MendariError, match="`double` is a share"):
            world.rounds.assign(round_id, [a, b], double=bad)
    assert rows(world.store, "written_assignments") == []
    assert world.rounds.assign(round_id, [a], double=0)["assigned"] == 4  # alone, with no second
    with pytest.raises(NoRound, match=NO_ROUND):
        world.rounds.assign("not-a-round", [a, b])
    with pytest.raises(NoRound):
        world.rounds.assign(round_id, [a, b], org="clinic-b")  # another organization's round


def test_an_open_assignment_moves_on_when_its_membership_ends(world: World) -> None:
    round_id = world.round(4)
    a, b, c = graders(world.store, 3)
    world.rounds.assign(round_id, [a, b], double=0)
    held_by_b = [r["id"] for r in rows(world.store, "written_assignments") if r["user_id"] == b]
    assert len(held_by_b) == 2
    world.store.remove_membership(membership_of(world.store, b), org=CLINIC, removed_by="test")
    assert world.rounds.next_item(b, org=CLINIC) is None
    with pytest.raises(NoAssignment, match=f"^{NO_ASSIGNMENT}$"):
        world.rounds.record_grades(held_by_b[0], b, PASS, org=CLINIC)
    moved = world.rounds.assign(round_id, [a, c], double=0)
    assert moved["withdrawn"] == 2 and moved["assigned"] == 2 and moved["graders"][c] == 2
    assert grade_all(world, [a, b, c]) == 4
    shown = world.rounds.progress(round_id)
    assert shown["assignments"] == {"graded": 4, "open": 0, "withdrawn": 2}
    assert shown["graders"][b] == {"graded": 0, "open": 0, "withdrawn": 2}
    assert shown["items_graded"] == 4


# -- blindness ----------------------------------------------------------------------------------


def test_a_grader_sees_the_text_the_context_and_the_rubric_and_nothing_else(world: World) -> None:
    round_id = world.round(10)
    a, b = graders(world.store, 2)
    world.rounds.assign(round_id, [a, b], double=1.0)  # every item to both
    shown = world.rounds.next_item(a, org=CLINIC)
    assert shown is not None
    assert set(shown) == {"assignment", "questions", "guidance", "written", "input"}
    rubric = world.pack.manifest.rubrics[RUBRIC]
    assert shown["questions"] == {
        "safe_to_send": ["no", "yes"],
        "addresses_request": ["not_addressed", "partly", "fully"],
        "plain_language": ["no", "yes"],
    }
    for key, criterion in rubric.criteria.items():
        guidance = shown["guidance"][key]
        assert set(guidance) == {"instructions", "display", "levels"}
        assert guidance["instructions"] == criterion.instructions
        assert guidance["levels"] == {
            level: criterion.criteria[level] for level, _ in criterion.scale()
        }
    assert shown["guidance"]["safe_to_send"]["display"] == "Safe to send"
    assert set(shown["written"]) == {"name", "description", "text"}
    assert shown["written"]["name"] == "reply_draft"
    assert shown["written"]["description"] == world.pack.manifest.outputs["reply_draft"].description
    item = next(  # two items may hold the same text: this one, by its context too
        i
        for i in world.run.items
        if i.text == shown["written"]["text"] and i.context == shown["input"]
    )
    assert set(shown["input"]) == {"message"} and set(shown["input"]["message"]) == {"body"}
    # Nothing of the run's ledger: no judge grade, model, confidence or pass line; no must_pass.
    said = json.dumps(shown)
    for hidden in (
        "mock-written-judge",
        "pass_from",
        "must_pass",
        "confidence",
        item.item,
        item.case,
    ):
        assert hidden not in said


def test_the_second_graders_view_is_the_same_before_and_after_the_first_grades(
    world: World,
) -> None:
    round_id = world.round(10)
    a, b = graders(world.store, 2)
    world.rounds.assign(round_id, [a, b], double=1.0)
    seen: dict[str, Any] = {}

    def of(shown: dict[str, Any]) -> str:  # an item, by what it shows (two texts may be equal)
        return json.dumps([shown["written"]["text"], shown["input"]])

    while (shown := world.rounds.next_item(b, org=CLINIC)) is not None:
        seen[of(shown)] = {k: v for k, v in shown.items() if k != "assignment"}
        world.rounds.record_grades(shown["assignment"], b, PASS, org=CLINIC)
    assert len(seen) == 10  # b graded everything first
    before = dump(world.store)
    views = []
    while (shown := world.rounds.next_item(a, org=CLINIC)) is not None:
        view = {k: v for k, v in shown.items() if k != "assignment"}
        assert view == seen[of(shown)]  # as b saw it, before anyone graded
        views.append(view)
        answered = world.rounds.record_grades(shown["assignment"], a, FAIL, org=CLINIC)
        assert set(answered) == {"remaining"}  # how many are left, never how it compares
    assert len(views) == 10 and dump(world.store) != before
    # No report, and nothing compared, until the round closes.
    (listed,) = world.rounds.rounds(CLINIC)
    assert listed["report"] is None and listed["state"] == OPEN and listed["graded"] == 20
    assert "kappa" not in json.dumps(world.rounds.progress(round_id))
    assert {r["report"] for r in rows(world.store, "written_rounds")} == {None}


def test_a_planted_judge_never_reaches_a_grader(world: World) -> None:
    """A ledger whose judge is a planted mark: no grader's answer holds it."""
    mark = "PLANTED-JUDGE-MODEL-5521"
    header = json.loads(json.dumps(world.run.ledger.header))
    header["judge"]["model"] = mark
    header["models"] = [
        mark if model == "mock-written-judge" else model for model in header["models"]
    ]
    planted = WrittenRun(WrittenLedger(header, world.run.ledger.cases), world.run.items)
    made = world.rounds.create_round(
        CLINIC, world.pack, RUBRIC, planted, sample=10, created_by="ops"
    )
    a, b = graders(world.store, 2)
    world.rounds.assign(made["round"], [a, b], double=0.5)
    assert mark in rows(world.store, "written_rounds")[0]["ledger"]  # it is there to be found
    said = []
    for user in (a, b):
        said.append(world.rounds.labelling(user, org=CLINIC))
        while (shown := world.rounds.next_item(user, org=CLINIC)) is not None:
            said.append(shown)
            said.append(world.rounds.record_grades(shown["assignment"], user, PASS, org=CLINIC))
    assert len(said) > 20 and mark not in json.dumps(said)
    assert mark not in json.dumps(world.rounds.progress(made["round"]))
    closed = world.rounds.close(made["round"], by="ops")
    assert closed["report"]["judge"]["model"] == mark  # the report names it, once closed


# -- a grade ------------------------------------------------------------------------------------


def test_a_grade_is_one_whole_answer_to_one_open_assignment_of_its_grader(world: World) -> None:
    round_id = world.round(3)
    a, b = graders(world.store, 2)
    stranger = person(world.store, "stranger", org="clinic-b")
    world.rounds.assign(round_id, [a, b], double=0)
    shown = world.rounds.next_item(a, org=CLINIC)
    assert shown is not None
    assignment = shown["assignment"]
    mark = "PLANTED-GRADE-VALUE"
    for values, said in (
        ({**PASS, "addresses_request": mark}, "grades.addresses_request is not one of its levels"),
        ({**PASS, "safe_to_send": True}, "grades.safe_to_send is not one of its levels"),
        (
            {"safe_to_send": "yes"},
            "grades answer every criterion: addresses_request, plain_language ungraded",
        ),
        ({**PASS, mark: "yes"}, "grades name a criterion this rubric does not have"),
        ([mark], "grades are a mapping of each criterion to one of its levels"),
    ):
        with pytest.raises(LabelError) as refused:
            world.rounds.record_grades(assignment, a, values, org=CLINIC)  # type: ignore[arg-type]
        assert str(refused.value) == said and mark not in str(refused.value)
    for who, org, which in (
        (b, CLINIC, assignment),  # another grader's
        (a, "clinic-b", assignment),  # another organization
        (stranger, "clinic-b", assignment),
        (a, CLINIC, "00000000-0000-4000-8000-000000000000"),  # unknown
        (a, CLINIC, "not-an-id"),
        ("not-a-user", CLINIC, assignment),
    ):
        with pytest.raises(NoAssignment, match=f"^{NO_ASSIGNMENT}$"):
            world.rounds.record_grades(which, who, PASS, org=org)
    assert events(world.store, "written.graded") == []
    left = world.rounds.labelling(a, org=CLINIC)["remaining"]
    assert world.rounds.record_grades(assignment, a, FAIL, org=CLINIC) == {"remaining": left - 1}
    with pytest.raises(NoAssignment, match=f"^{NO_ASSIGNMENT}$"):
        world.rounds.record_grades(assignment, a, PASS, org=CLINIC)  # once, never changed
    (kept,) = rows(world.store, "written_assignments", id=assignment)
    body = json.loads(kept["grades"])
    assert body["values"] == FAIL and kept["graded_at"] == kept["ended_at"]
    (graded,) = events(world.store, "written.graded")
    assert set(graded["payload"]) == PAYLOADS["written.graded"]
    (item,) = rows(world.store, "written_items", id=kept["item_id"])
    assert graded["payload"]["grade_ref"] == {
        "id": assignment,
        "sha256": grade_ref(
            base64.b64decode(body["salt"]), round_id, item["item"], assignment, FAIL
        ),
    }
    # The chain commits to the grade without holding a level of it.
    assert "partly" not in json.dumps(chained(world.store))
    assert world.rounds.assignment_round(assignment, a, org=CLINIC) == round_id
    assert world.rounds.assignment_round(assignment, b, org=CLINIC) is None
    assert world.rounds.assignment_round(assignment, a, org="clinic-b") is None


def test_each_showing_is_chained_before_it_is_answered(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    round_id = world.round(2)
    a, b = graders(world.store, 2)
    world.rounds.assign(round_id, [a, b], double=0)
    assert events(world.store, "written.item_read") == []
    assert world.rounds.next_item(a, org="clinic-b") is None  # their assignments are clinic-a's
    assert events(world.store, "written.item_read") == []
    shown = world.rounds.next_item(a, org=CLINIC)
    assert shown is not None
    (read,) = events(world.store, "written.item_read")
    (assignment,) = rows(world.store, "written_assignments", id=shown["assignment"])
    (item,) = rows(world.store, "written_items", id=assignment["item_id"])
    assert read["decision_id"] == f"written:{round_id}"
    assert read["payload"] == {
        "round": round_id,
        "org": CLINIC,
        "item": item["item"],
        "assignment": shown["assignment"],
        "grader": a,
    }
    assert world.rounds.next_item(a, org=CLINIC) == shown  # shown again, and chained again
    assert len(events(world.store, "written.item_read")) == 2
    real = world.store._append_many

    def failing(db: Any, event: str, entries: Any) -> Any:
        if event == "written.item_read":
            raise RuntimeError("the chain refused")
        return real(db, event, entries)

    monkeypatch.setattr(world.store, "_append_many", failing)
    before = dump(world.store)
    with pytest.raises(RuntimeError, match="the chain refused"):
        world.rounds.next_item(b, org=CLINIC)  # nothing is returned unless its read is chained
    assert dump(world.store) == before


# -- closing ------------------------------------------------------------------------------------


def test_closing_computes_the_report_and_erases_every_text(world: World) -> None:
    round_id = world.round(10)
    a, b, c = graders(world.store, 3)
    world.rounds.assign(round_id, [a, b, c], double=0.5)
    assert grade_all(world, [a, b]) > 0  # c grades nothing: their assignments end unanswered
    waiting = world.rounds.labelling(c, org=CLINIC)["remaining"]
    assert waiting > 0
    graded = len(events(world.store, "written.graded"))
    closed = world.rounds.close(round_id, by="ops")
    assert (closed["state"], closed["graded"], closed["erased"]) == (CLOSED, graded, 10)
    report = closed["report"]
    assert report["format"] == "mendari.written/1" and report["ledger"]["id"] == world.run.ledger.id
    assert set(report["graders"]) == {a, b} and sum(report["graders"].values()) == graded
    assert report["simulated_graders"] is False and report["cases"]["stale_grades"] == 0
    assert report["cases"]["graded"] == len(
        {r["item_id"] for r in rows(world.store, "written_assignments") if r["grades"]}
    )
    assert {found["validation"]["state"] for found in report["criteria"].values()} == {INSUFFICIENT}
    assert all(found["judged"] is None for found in report["criteria"].values())
    # Every text is gone; ids and grades remain.
    items = rows(world.store, "written_items")
    assert len(items) == 10 and {item["body"] for item in items} == {None}
    assert all(item["erased_at"] and item["item"] for item in items)
    assert sum(1 for r in rows(world.store, "written_assignments") if r["grades"]) == graded
    assert "Thank you" not in dump(world.store)
    (kept,) = rows(world.store, "written_rounds")
    assert kept["state"] == CLOSED and kept["closed_at"] and json.loads(kept["report"]) == report
    (event,) = events(world.store, "written.round_closed")
    assert event["payload"] == {
        "round": round_id,
        "org": CLINIC,
        "graded": graded,
        "erased": 10,
        "report_sha256": report["result_sha256"],
        "by": "ops",
    }
    # Closed: nothing waits, nothing is taken, and it is not closed twice.
    assert world.rounds.next_item(c, org=CLINIC) is None
    assert world.rounds.labelling(c, org=CLINIC) == {"sets": [], "remaining": 0}
    assert world.rounds.labelling(a, org=CLINIC)["remaining"] == 0
    with pytest.raises(MendariError, match="is closed: nothing is assigned"):
        world.rounds.assign(round_id, [a, b])
    with pytest.raises(MendariError, match="already closed"):
        world.rounds.close(round_id, by="ops")
    (listed,) = world.rounds.rounds(CLINIC, world.pack.id)
    assert listed["report"] == report and listed["state"] == CLOSED and listed["open"] == 0
    assert (
        world.rounds.rounds("clinic-b") == [] and world.rounds.rounds(CLINIC, "other/agent") == []
    )
    assert world.store.verify()["ok"]
    # Every event of the round: the organization's own, ids and counts only.
    for event in chained(world.store):
        if event["event"] in PAYLOADS:
            assert set(event["payload"]) == PAYLOADS[event["event"]], event["event"]
            assert event["decision_id"] == f"written:{round_id}"
    items = world.store.administrative_events(CLINIC, limit=500).rows
    kinds = {item["event"] for item in items}
    assert set(PAYLOADS) - {"written.erased"} <= kinds
    assert world.store.administrative_events("clinic-b", limit=500).rows == []
    # As FHIR AuditEvents (the administrators' export): well formed, the grader or whoever
    # closed the round as the agent, and no item, assignment, commitment or count as a detail.
    for item in items:
        if item["event"] not in PAYLOADS:
            continue
        resource = audit_fhir.administrative_event(item)
        assert fhir_structure.check(resource) == [], resource
        acted = resource["agent"][0]["who"]["display"]
        if item["event"] in ("written.item_read", "written.graded"):
            assert acted in (a, b)
        if item["event"] == "written.round_closed":
            assert acted == "ops"
        details = {detail["type"] for detail in resource["entity"][0].get("detail", [])}
        assert details <= {"org", "pack_id", "pack_digest", "ledger", "hash"}, details


def test_a_round_left_closing_is_closed_by_running_it_again(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    round_id = world.round(3)
    a, b = graders(world.store, 2)
    world.rounds.assign(round_id, [a, b], double=0)
    grade_all(world, [a])

    def failing(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("interrupted")

    monkeypatch.setattr(written, "report", failing)
    with pytest.raises(RuntimeError, match="interrupted"):
        world.rounds.close(round_id, by="ops")
    monkeypatch.undo()
    (kept,) = rows(world.store, "written_rounds")
    assert kept["state"] == "closing" and kept["report"] is None
    assert world.rounds.next_item(b, org=CLINIC) is None  # no grade is taken meanwhile
    assert {item["body"] is None for item in rows(world.store, "written_items")} == {False}
    assert world.rounds.close(round_id, by="ops")["state"] == CLOSED


# -- keys and retention -------------------------------------------------------------------------


def test_a_key_rotation_reseals_the_texts_and_the_check_covers_them(
    db_target: str, pack: Pack, the_run: WrittenRun
) -> None:
    old, new = os.urandom(32), os.urandom(32)
    with AuditStore(db_target, phi=PhiPolicy("encrypt", old, HASH)) as store:
        world = World(store, WrittenStore(store), pack, the_run)
        round_id = world.round(5)
        before = {row["id"]: row["body"] for row in rows(store, "written_items")}
    with AuditStore(db_target, phi=PhiPolicy("encrypt", new, HASH, (old,))) as store:
        checked = store.phi_check()
        assert checked["by_key"]["written"] == {"legacy": 5} and checked["failures"] == 5
        assert {failure["kind"] for failure in checked["failed"]} == {"written"}
        summary = store.rotate_phi(to=key_id(new))
        assert summary["written"] == 5 and summary["unopenable"] == []
        after = {row["id"]: row["body"] for row in rows(store, "written_items")}
        assert all(
            after[row].startswith(f"v2:{key_id(new)}:") and after[row] != before[row]
            for row in after
        )
        rotated = [
            e["payload"] for e in events(store, "phi.rotated") if e["payload"].get("written")
        ]
        assert [payload["written"] for payload in rotated] == [5]
        assert store.rotate_phi(to=key_id(new)).get("written", 0) == 0  # done stays done
    with AuditStore(db_target, phi=PhiPolicy("encrypt", new, HASH)) as store:  # the old key gone
        checked = store.phi_check()
        assert checked["ready"] and checked["by_key"]["written"] == {key_id(new): 5}
        world = World(store, WrittenStore(store), pack, the_run)
        a, b = graders(store, 2)
        world.rounds.assign(round_id, [a, b], double=0)
        shown = world.rounds.next_item(a, org=CLINIC)
        assert shown is not None and shown["written"]["text"].startswith("Thank you")
        assert store.verify()["ok"]
    with AuditStore(db_target, phi=PhiPolicy("encrypt", os.urandom(32), HASH)) as store:
        assert store.phi_check()["failures"] == 5  # a key that opens none of them says so


def test_retention_erases_the_texts_a_round_still_keeps(world: World) -> None:
    round_id = world.round(4)
    a, b = graders(world.store, 2)
    world.rounds.assign(round_id, [a, b], double=0)
    assert world.rounds.labelling(a, org=CLINIC)["remaining"] == 2
    assert world.store.purge_written(30) == 0  # nothing is that old
    summary = world.store.purge(0, source="command")
    assert summary["written"] == 4
    assert {item["body"] for item in rows(world.store, "written_items")} == {None}
    (erased,) = events(world.store, "written.erased")
    assert erased["decision_id"] == f"written:{round_id}"
    assert erased["payload"] == {
        "round": round_id,
        "org": CLINIC,
        "erased": 4,
        "reason": "retention",
        "retention_days": 0,
    }
    # An item whose text is gone is shown to no one, and waits for no one.
    assert world.rounds.next_item(a, org=CLINIC) is None
    assert world.rounds.labelling(a, org=CLINIC)["remaining"] == 0
    assert world.rounds.progress(round_id)["erased"] == 4
    assert "written" not in world.store.purge(0, source="command")  # counted only where any
    closed = world.rounds.close(round_id, by="ops")
    assert closed["erased"] == 0 and closed["graded"] == 0
    assert world.store.verify()["ok"]


# -- an older database, and the command line ----------------------------------------------------


def test_a_database_from_before_the_tables_reads_as_having_none(
    world: World, db_target: str
) -> None:
    a = person(world.store, "grader-x")
    with world.store._db.session(exclusive=True) as db:
        for table in ("written_assignments", "written_items", "written_rounds"):
            db.all(f"DROP TABLE {table}")
    with AuditStore(db_target, read_only=True) as older:
        rounds = WrittenStore(older)
        assert rounds.rounds(None) == [] and rounds.rounds(CLINIC) == []
        assert rounds.labelling(a, org=CLINIC) == {"sets": [], "remaining": 0}
        assert (
            rounds.assignment_round("00000000-0000-4000-8000-000000000000", a, org=CLINIC) is None
        )
        with pytest.raises(NoRound):
            rounds.progress("00000000-0000-4000-8000-000000000000")
        assert older.verify()["ok"] and older.phi_check()["by_key"].get("written") is None


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    assert "Thank you" not in out + err and "lisinopril" not in out + err, argv
    return code, out, err


def test_the_operators_commands_make_assign_show_and_close_a_round(
    tmp_path: Path,
    inbox_pack: Pack,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MENDARI_PHI_MODE", "encrypt")
    monkeypatch.setenv("MENDARI_PHI_KEY", PHI_KEY_B64)
    monkeypatch.setenv("MENDARI_INPUT_HASH_KEY", HASH_KEY_B64)
    db = str(tmp_path / "mendari.db")
    key = base64.b64decode(PHI_KEY_B64)
    with AuditStore(db, phi=PhiPolicy("encrypt", key, base64.b64decode(HASH_KEY_B64))) as store:
        Decision(inbox_pack, store=store)({"message": {"body": "Can I get a refill?"}})
        a, b = graders(store, 2)
    create = ("written", "round", "create", str(MINI), "--rubric", RUBRIC, "--dataset", "written")
    code, _, err = run_cli(capsys, *create, "--org", "nowhere", "--sample", "4", "--db", db)
    assert code == 1 and err.startswith("error: no organization nowhere in ")
    code, out, err = run_cli(capsys, *create, "--org", CLINIC, "--sample", "8", "--db", db)
    assert code == 0, err
    made = json.loads(out)
    round_id = made["round"]
    assert made["items"] == 8 and f"mendari written round assign {round_id}" in err
    assign = ("written", "round", "assign", round_id, "--grader", a, "--grader", b, "--db", db)
    code, out, err = run_cli(capsys, *assign, "--double", "0.5")
    assert code == 0 and json.loads(out)["assigned"] == 12
    assert "12 assignment(s) made and 0 withdrawn" in err
    with AuditStore(db, phi=PhiPolicy("encrypt", key, base64.b64decode(HASH_KEY_B64))) as store:
        world = World(store, WrittenStore(store), load_pack(MINI), None)  # type: ignore[arg-type]
        assert grade_all(world, [a, b]) == 12
    code, out, _ = run_cli(capsys, "written", "round", "progress", round_id, "--db", db)
    shown = json.loads(out)
    assert code == 0 and shown["assignments"] == {"graded": 12, "open": 0, "withdrawn": 0}
    code, out, _ = run_cli(capsys, "written", "round", "list", "--org", CLINIC, "--db", db)
    (listed,) = json.loads(out)
    assert (listed["id"], listed["state"], listed["report_sha256"]) == (round_id, OPEN, None)
    close = ("written", "round", "close", round_id, "--db", db)
    # Fewer than 2,000 resamples is refused before the database is opened: nothing changes.
    for few in ("1", "50", "1999"):
        code, _, err = run_cli(capsys, *close, "--resamples", few)
        assert code == 1 and "--resamples: the resamples are 2000 to 100000" in err
    code, out, _ = run_cli(capsys, "written", "round", "progress", round_id, "--db", db)
    assert json.loads(out)["state"] == OPEN
    # No option chooses the bootstrap's seed: it is the round's own.
    code, _, err = run_cli(capsys, *close, "--seed", "7")
    assert code != 0 and "--seed" in err
    code, _, err = run_cli(capsys, *assign, "--seed", "7")
    assert code != 0 and "--seed" in err
    code, out, err = run_cli(capsys, *close)
    assert code == 0 and "Written output: rubric reply_quality" in out
    assert "bootstrap seed 0, 2000 resamples" in out
    assert "12 grade(s) kept, 8 text(s) erased" in err
    code, _, err = run_cli(capsys, *close)
    assert code == 1 and "already closed" in err
    code, out, _ = run_cli(
        capsys, "written", "round", "list", "--agent", "mendari-test/written-mini", "--db", db
    )
    (listed,) = json.loads(out)
    assert listed["state"] == CLOSED and listed["report_sha256"].startswith("sha256:")
    assert "report" not in listed
    for unknown in ("progress", "close"):
        code, _, err = run_cli(
            capsys, "written", "round", unknown, "00000000-0000-4000-8000-000000000000", "--db", db
        )
        assert code == 1 and err.strip() == f"error: {NO_ROUND}"
    # Under PHI mode hash a round is refused before the agent or a judge is asked.
    monkeypatch.setenv("MENDARI_PHI_MODE", "hash")
    served(str(tmp_path / "hashed.db"), inbox_pack)
    code, _, err = run_cli(
        capsys, *create, "--org", CLINIC, "--sample", "2", "--db", str(tmp_path / "hashed.db")
    )
    assert code == 1 and "PHI mode hash keeps no patient text" in err


# -- what the review found ----------------------------------------------------------------------


def test_closing_checks_its_arguments_before_it_changes_anything(world: World) -> None:
    """The reviewer's case: `close(resamples=0)` was refused only after the round's assignments
    were ended and the round set `closing`. Every argument is checked before the first step."""
    round_id = world.round(3)
    a, b = graders(world.store, 2)
    world.rounds.assign(round_id, [a, b], double=0)
    before = dump(world.store)
    for bad in (0, 1, 50, 1999, 100_001, True, "2000"):
        with pytest.raises(MendariError, match="the resamples are"):
            world.rounds.close(round_id, by="ops", resamples=bad)  # type: ignore[arg-type]
    for nobody in ("", "   ", None):
        with pytest.raises(MendariError, match="names who closes it"):
            world.rounds.close(round_id, by=nobody)  # type: ignore[arg-type]
    assert dump(world.store) == before  # still open, every assignment still open
    assert world.rounds.progress(round_id)["state"] == OPEN
    assert world.rounds.next_item(a, org=CLINIC) is not None


def test_a_round_left_closing_says_so_and_retention_still_erases_its_texts(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    round_id = world.round(3)
    a, b = graders(world.store, 2)
    world.rounds.assign(round_id, [a, b], double=0)

    def failing(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("interrupted")

    monkeypatch.setattr(written, "report", failing)
    with pytest.raises(RuntimeError, match="interrupted"):
        world.rounds.close(round_id, by="ops")
    monkeypatch.undo()
    (listed,) = world.rounds.rounds(CLINIC)
    assert listed["state"] == "closing" and listed["report"] is None
    assert listed["note"] == CLOSING_NOTE and "close` on it again to finish" in CLOSING_NOTE
    assert world.rounds.progress(round_id)["note"] == CLOSING_NOTE
    # Its texts are still kept (nothing was erased), and retention still erases them.
    assert {item["body"] is None for item in rows(world.store, "written_items")} == {False}
    assert world.store.purge(0, source="command")["written"] == 3
    assert {item["body"] for item in rows(world.store, "written_items")} == {None}
    closed = world.rounds.close(round_id, by="ops")
    assert closed["state"] == CLOSED and closed["erased"] == 0
    (listed,) = world.rounds.rounds(CLINIC)
    assert listed["note"] is None and listed["report"] == closed["report"]
    assert world.store.verify()["ok"]


def test_a_rounds_seed_is_fixed_when_it_is_made_and_is_the_reports(world: World) -> None:
    made = world.rounds.create_round(
        CLINIC, world.pack, RUBRIC, world.run, sample=4, seed=11, created_by="ops"
    )
    a, b = graders(world.store, 2)
    world.rounds.assign(made["round"], [a, b], double=0.5)
    grade_all(world, [a, b])
    # Nobody chooses it later: closing takes no seed, and reads the one the chain holds even
    # when the round's own row was edited.
    with pytest.raises(TypeError):
        world.rounds.close(made["round"], by="ops", seed=3)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        world.rounds.assign(made["round"], [a, b], seed=3)  # type: ignore[call-arg]
    tamper(world.store, "UPDATE written_rounds SET header = replace(header, ?, ?)", (
        '"seed":11', '"seed":99'
    ))  # fmt: skip
    closed = world.rounds.close(made["round"], by="ops")
    assert closed["report"]["seeds"] == {"bootstrap": 11, "resamples": 2000, "level": 0.95}


def closed_round(world: World) -> tuple[str, dict[str, Any]]:
    round_id = world.round(4)
    a, b = graders(world.store, 2)
    world.rounds.assign(round_id, [a, b], double=1.0)
    grade_all(world, [a], PASS)
    grade_all(world, [b], FAIL)
    return round_id, world.rounds.close(round_id, by="ops")["report"]


def test_a_stored_report_edited_in_the_database_is_not_served_and_breaks_verification(
    world: World,
) -> None:
    """The reviewer's case: a `written_rounds.report` edited by SQL to say "validated" was
    served unchanged, and `verify` said ok. Its hash is recomputed and compared with the one
    the round's closing chained."""
    round_id, report = closed_round(world)
    (listed,) = world.rounds.rounds(CLINIC)
    assert listed["report"] == report and listed["note"] is None
    checked = world.store.verify()
    assert checked["ok"] and checked["written"] == {"rounds": 1, "grades": 8}
    forged = json.loads(json.dumps(report))
    for criterion in forged["criteria"].values():
        criterion["validation"] = {"state": "validated", "reasons": []}
        criterion["judged"] = {"passing": {"k": 9, "n": 9, "low": 0.66, "high": 1.0}, "levels": {}}
    tamper(
        world.store,
        "UPDATE written_rounds SET report = ? WHERE id = ?",
        (json.dumps(forged, sort_keys=True, separators=(",", ":")), round_id),
    )
    (listed,) = world.rounds.rounds(CLINIC)
    assert listed["report"] is None and listed["note"] == REPORT_MISMATCH
    assert "does not match the audit trail" in REPORT_MISMATCH
    assert "validated" not in json.dumps(listed)
    broken = world.store.verify()
    (closing,) = events(world.store, "written.round_closed")
    assert broken["ok"] is False and broken["reason"] == "written"
    assert broken["broken_at"] == closing["seq"]
    # A forgery that also rewrites its own `result_sha256` is caught the same way: the hash is
    # recomputed from the report, never read from it.
    forged["result_sha256"] = written.result_sha256(
        {k: v for k, v in forged.items() if k not in ("run", "result_sha256")}
    )
    tamper(
        world.store,
        "UPDATE written_rounds SET report = ? WHERE id = ?",
        (json.dumps(forged), round_id),
    )
    assert world.rounds.rounds(CLINIC)[0]["report"] is None
    assert world.store.verify()["ok"] is False
    # Put back as it was stored: served again, and the chain holds.
    tamper(
        world.store,
        "UPDATE written_rounds SET report = ? WHERE id = ?",
        (json.dumps(report, sort_keys=True, separators=(",", ":")), round_id),
    )
    assert world.rounds.rounds(CLINIC)[0]["report"] == report and world.store.verify()["ok"]


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        # A grade changed, kept its salt.
        (
            """UPDATE written_assignments SET grades = replace(grades, ?, ?)
               WHERE id = (SELECT min(id) FROM written_assignments WHERE grades LIKE ?)""",
            ('"yes"', '"no"', '%"yes"%'),
        ),
        # A grade removed.
        (
            """UPDATE written_assignments SET grades = NULL
               WHERE id = (SELECT min(id) FROM written_assignments)""",
            (),
        ),
        # A grade moved to another item.
        (
            """UPDATE written_assignments SET item_id = (SELECT max(id) FROM written_items)
               WHERE id = (SELECT min(a.id) FROM written_assignments a
                           WHERE a.item_id <> (SELECT max(id) FROM written_items))""",
            (),
        ),
        # A closed round reopened, or its report removed.
        ("UPDATE written_rounds SET state = 'open'", ()),
        ("UPDATE written_rounds SET report = NULL", ()),
    ],
    ids=["grade-changed", "grade-removed", "grade-moved", "round-reopened", "report-removed"],
)
def test_verify_checks_every_stored_grade_and_closed_round_against_the_chain(
    world: World, sql: str, params: tuple[str, ...]
) -> None:
    closed_round(world)
    assert world.store.verify()["ok"]
    tamper(world.store, sql, params)
    broken = world.store.verify()
    assert broken["ok"] is False and broken["reason"] == "written", broken


def test_verify_finds_a_grade_no_event_accounts_for(world: World) -> None:
    round_id = world.round(2)
    a, b = graders(world.store, 2)
    world.rounds.assign(round_id, [a, b], double=0)
    assert world.store.verify()["ok"]  # an open round with no grade: nothing to check
    forged = json.dumps({"salt": base64.b64encode(os.urandom(32)).decode(), "values": PASS})
    tamper(
        world.store,
        "UPDATE written_assignments SET grades = ?, graded_at = created_at, ended_at = created_at "
        "WHERE id = (SELECT min(id) FROM written_assignments)",
        (forged,),
    )
    broken = world.store.verify()
    assert broken["ok"] is False and broken["reason"] == "written" and "broken_at" not in broken
