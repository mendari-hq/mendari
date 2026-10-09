# SPDX-License-Identifier: Apache-2.0
"""Blind labelling of hidden evaluation sets, in the store (LP-11h, `core/evalsets.py`,
ADR-0011): sampling, assignment, a labeller's read and label, adjudication, progress and the
measured agreement and coverage. No route serves any of it yet, so a person cannot label: the
labeller's two calls (`next_case`, `record_label`) are tested here, at the store.

- A labeller is shown a case's input and the set's questions, never another label, an attribute,
  the intended label or a model's output; the second labeller's view is the same before and
  after the first answers, and neither is told how their answer compares. The adjudicator works
  blind too; the majority on each question settles a disputed case, and a three-way split
  leaves it unresolved.
- Only a labeller of the set's organization who does not manage its sets is assigned (separation
  of duties); an assignment whose membership ends moves to another labeller on the next run.
- Sampling is seeded within strata and records each drawn case's weight; pairs and labellers are
  balanced.
- The chain holds ids, keyed case hashes, user ids, counts and commitments, never a label value
  or any word of the pack's labels; `audit verify --evalsets` recomputes each label's commitment
  and finds a label or an assignment altered, moved, removed or forged.
- `measure` gives what `rater_agreement` and `coverage` give on the same labels, sealed as the
  set's aggregates; progress names no case.
On SQLite and Postgres (`db_target`); keys are made per test, cases are hand-written synthetic
text."""

from __future__ import annotations

import base64
import json
import random
import re
import shlex
import uuid
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any

import pytest

from mendari import AuditStore, Decision, Pack
from mendari.cli import main
from mendari.core import accounts
from mendari.core.evalset_keys import EvalsetKeys, evalset_keys
from mendari.core.evalsets import (
    AGGREGATES_FORMAT,
    NO_ASSIGNMENT,
    EvalsetStore,
    label_ref,
)
from mendari.core.phi import PhiPolicy, key_id
from mendari.core.registry import resolve_pack
from mendari.errors import MendariError
from mendari.evals.coverage import coverage
from mendari.evals.harness import Dataset
from mendari.evals.rater_agreement import agreement, with_derived
from mendari.standards import audit_fhir, fhir_structure
from tests.conftest import ROOT, evalset_keys_for_test, evalset_on, hand_written
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

ISSUER = "https://idp.example.org"
ROUTINE = {"destination": "nurse", "urgency": "routine"}
# Every word of the inbox pack's labels: none may be in the chain (conflict 36).
_MANIFEST = resolve_pack(INBOX).manifest
LABEL_WORDS = sorted({word for q in _MANIFEST.questions for word in _MANIFEST.labels(q)})
LABEL_WORD = re.compile(r"\b(?:" + "|".join(map(re.escape, LABEL_WORDS)) + r")\b", re.IGNORECASE)
TABLES = (
    "evalset_sets",
    "evalset_cases",
    "evalset_index",
    "evalset_assignments",
    "evalset_labels",
    "audit_events",
    "settings",
)
# The keys each labelling event's payload holds, and a label's commitment's.
PAYLOADS = {
    "evalset.sampled": {"set", "org", "n", "seed"},
    "evalset.assigned": {"set", "org", "case", "assignment", "labeller", "round"},
    "evalset.case_read": {"set", "org", "case", "assignment", "labeller"},
    "evalset.labelled": {"set", "org", "case", "label_ref", "labeller", "round"},
    "evalset.measured": {"set", "org", "n"},
}
USER = re.compile(r"^user:[0-9a-f]{16}$")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
CASE_HASH = re.compile(r"^hmac-sha256:[0-9a-f]{16}:[0-9a-f]{64}$")

Script = Callable[[int, int], Mapping[str, str]]  # (a case's number, its labels so far) -> one


# -- the world ------------------------------------------------------------------------------------


@dataclass
class World:
    store: AuditStore
    hidden: EvalsetStore
    keys: EvalsetKeys
    pack: Pack
    set_id: str


@pytest.fixture
def world(db_target: str, inbox_pack: Pack) -> Iterator[World]:
    keys = evalset_keys_for_test()
    with AuditStore(db_target) as store:
        hidden = EvalsetStore(store, keys)
        yield World(store, hidden, keys, inbox_pack, a_set(hidden, inbox_pack))


def person(
    store: AuditStore, sub: str, *, org: str = CLINIC, roles: Sequence[str] = ("labeller",)
) -> str:
    """A person with a membership of `org` holding `roles` (the organization's record made with
    the first): their user id."""
    made = store.record_user(issuer=ISSUER, subject=sub, email=None, name=sub)
    if store.organization(org) is None:
        store.create_organization(org=org, name=org.title(), created_by="test")
    store.add_membership(org=org, roles=list(roles), created_by="test", user_id=made["id"])
    return str(made["id"])


def labellers(store: AuditStore, count: int, *, org: str = CLINIC, start: int = 0) -> list[str]:
    return [person(store, f"labeller-{org}-{n}", org=org) for n in range(start, start + count)]


def membership_of(store: AuditStore, user: str, org: str = CLINIC) -> str:
    found = store.membership_in_force(user, org)
    assert found is not None
    return str(found["id"])


def cases(
    count: int, *, start: int = 0, strata: Sequence[str] = (MARKS["stratum"],)
) -> list[dict[str, Any]]:
    """Hand-written cases numbered from `start` (the number is in the input, all a labeller
    sees), each with the planted marks and an intended label."""
    return [
        {
            **hand_written(
                f"{MARKS['input']} case {number}: my knee aches after the long walk",
                author=MARKS["author"],
                stratum=strata[number % len(strata)],
                language=MARKS["language"],
            ),
            "labels": {"urgency": "routine"},
        }
        for number in range(start, start + count)
    ]


def number_of(value: Mapping[str, Any]) -> int:
    """The number a case's input carries (`cases`, `planted`): what a labeller reads."""
    found = re.search(r"(\d+):", value["message"]["body"])
    assert found is not None
    return int(found[1])


def agree(number: int, nth: int) -> Mapping[str, str]:
    return ROUTINE


def split(number: int, nth: int) -> Mapping[str, str]:
    """Case 1 disputed, then resolved (nurse, physician, nurse); case 2 disputed, then a
    three-way split (nurse, physician, billing); every other case agreed."""
    plan = {1: ("nurse", "physician", "nurse"), 2: ("nurse", "physician", "billing")}
    return {"destination": plan.get(number, ("nurse",) * 3)[nth], "urgency": "routine"}


def majority(number: int, nth: int) -> Mapping[str, str]:
    """`split`'s cases, and two more the majority on each question settles: case 3 (nurse,
    physician, physician), the adjudicator siding with the second labeller; case 4
    (nurse/routine, physician/routine, physician/urgent), the first two agreeing on urgency
    alone and the adjudicator differing on it, settled physician/routine a question at a
    time."""
    plan = {
        3: (("nurse", "routine"), ("physician", "routine"), ("physician", "routine")),
        4: (("nurse", "routine"), ("physician", "routine"), ("physician", "urgent")),
    }
    if number not in plan:
        return split(number, nth)
    destination, urgency = plan[number][nth]
    return {"destination": destination, "urgency": urgency}


def label_all(
    hidden: EvalsetStore, users: Sequence[str], script: Script = agree, *, org: str = CLINIC
) -> int:
    """Each of `users` in turn labels the case served to them until none is left; `script`
    picks each answer from what the labeller is shown (the case's number) and how many labels
    the case has had: never from the store. How many labels were given."""
    given: Counter[int] = Counter()
    done = 0
    while True:
        progressed = False
        for user in users:
            shown = hidden.next_case(user, org=org)
            if shown is None:
                continue
            number = number_of(shown["input"])
            labels = dict(script(number, given[number]))
            hidden.record_label(shown["assignment"], user, labels, org=org)
            given[number] += 1
            done += 1
            progressed = True
        if not progressed:
            return done


def labelled_set(world: World, count: int = 3, people: int = 3) -> list[str]:
    """`count` cases drawn and labelled blind by the first two of `people` labellers (`split`),
    the disputed ones adjudicated by the others: case 0 agreed, case 1 resolved, case 2
    unresolved. The labellers' user ids."""
    hidden = world.hidden
    hidden.add_cases(world.set_id, cases(count))
    hidden.sample(world.set_id, per_stratum=count)
    users = labellers(world.store, people)
    hidden.assign(world.set_id, users[:2])
    label_all(hidden, users[:2], split)
    hidden.adjudicate(world.set_id, users[2:])
    label_all(hidden, users[2:], lambda number, _: split(number, 2))
    return users


def dump(store: AuditStore) -> str:
    with store._db.session(exclusive=False) as db:
        return json.dumps(
            {table: db.all(f"SELECT * FROM {table} ORDER BY 1") for table in TABLES}, default=str
        )


def rows(store: AuditStore, table: str, **where: str) -> list[dict[str, Any]]:
    clause = " AND ".join(f"{column} = ?" for column in where) or "1 = 1"
    with store._db.session(exclusive=False) as db:
        return db.all(f"SELECT * FROM {table} WHERE {clause} ORDER BY id", tuple(where.values()))


def statuses(world: World) -> dict[str, int]:
    return {k: v for k, v in world.hidden.progress(world.set_id)["statuses"].items() if v}


def opened_labels(world: World) -> list[dict[str, Any]]:
    """Every label, opened with the key holder's keys, with its assignment's labeller and round."""
    found = []
    for label in rows(world.store, "evalset_labels"):
        (assignment,) = rows(world.store, "evalset_assignments", id=label["assignment_id"])
        body, _ = world.keys.open(
            "label", label["body"], label["set_id"], label["case_id"], label["id"]
        )
        found.append({**label, "user": assignment["user_id"], "round": assignment["round"],
                      "values": body["values"], "salt": body["salt"]})  # fmt: skip
    return found


def events(store: AuditStore, kind: str) -> list[dict[str, Any]]:
    return [event for event in chained(store) if event["event"] == kind]


def run(
    capsys: pytest.CaptureFixture[str], *argv: str, allowed: Sequence[str] = ()
) -> tuple[int, str, str]:
    """A command, its output holding no planted mark but those `allowed` (conflict 55: the
    stratum and language in `evalset coverage`)."""
    code = main(list(argv))
    out, err = capsys.readouterr()
    for name, mark in MARKS.items():
        if name not in allowed:
            assert mark not in out + err, (argv, name)
    return code, out, err


# -- blind ----------------------------------------------------------------------------------------


def test_a_second_labeller_never_sees_the_first_labellers_answer_or_model_output(
    world: World,
) -> None:
    """With a decision on the same input kept beside the set: the second labeller is shown the
    input and the questions only, the same before and after the first answers; neither is told
    how their answer compares."""
    store, hidden = world.store, world.hidden
    case = cases(1)[0]
    hidden.add_cases(world.set_id, [case])
    decided = Decision(world.pack, store=store)(case["input"])  # what a model made of it
    first, second = labellers(store, 2)
    hidden.sample(world.set_id, cases=1)
    hidden.assign(world.set_id, [first, second])
    before = hidden.next_case(second, org=CLINIC)
    shown = hidden.next_case(first, org=CLINIC)
    assert shown is not None and before is not None
    assert set(shown) == set(before) == {"assignment", "questions", "input"}
    assert shown["input"] == before["input"] == case["input"]
    manifest = world.pack.manifest
    asked = {question: manifest.labels(question) for question in manifest.questions}
    assert shown["questions"] == before["questions"] == asked
    answered = {"destination": "physician", "urgency": "urgent"}
    assert hidden.record_label(shown["assignment"], first, answered, org=CLINIC) == {"remaining": 0}
    after = hidden.next_case(second, org=CLINIC)
    assert after == before  # nothing of the first answer reaches the second labeller
    text = json.dumps(after)
    for private in (
        decided.id,
        MARKS["author"],
        MARKS["stratum"],
        MARKS["language"],
        "confidence",
        "outcome",
        "intended",
        "attributes",
        "salt",
        "weight",
        "agreed",
        "disputed",
    ):
        assert private not in text, private
    assert hidden.record_label(after["assignment"], second, ROUTINE, org=CLINIC) == {"remaining": 0}
    assert statuses(world) == {"disputed": 1}  # the store knows; neither labeller was told


def test_blind_adjudication_the_majority_rule_and_an_unresolved_case(world: World) -> None:
    """The majority on each question settles a disputed case, whichever first-pass labeller the
    adjudicator sides with and question by question (`majority`); a three-way split leaves it
    unresolved."""
    store, hidden = world.store, world.hidden
    hidden.add_cases(world.set_id, cases(5))
    hidden.sample(world.set_id, per_stratum=5)
    users = labellers(store, 3)
    hidden.assign(world.set_id, users[:2])
    views: dict[int, list[dict[str, Any]]] = {}
    given: Counter[int] = Counter()
    for user in users[:2]:
        while (shown := hidden.next_case(user, org=CLINIC)) is not None:
            number = number_of(shown["input"])
            views.setdefault(number, []).append(shown)
            labels = dict(majority(number, given[number]))
            hidden.record_label(shown["assignment"], user, labels, org=CLINIC)
            given[number] += 1
    assert statuses(world) == {"agreed": 1, "disputed": 4}
    with pytest.raises(MendariError, match="needs a third labeller"):
        hidden.adjudicate(world.set_id, users[:2])  # neither of a case's first two
    made = hidden.adjudicate(world.set_id, [users[2]])
    assert made["assigned"] == 4 and {a["labeller"] for a in made["assignments"]} == {users[2]}
    assert {a["round"] for a in made["assignments"]} == {"adjudication"}
    left = []
    while (shown := hidden.next_case(users[2], org=CLINIC)) is not None:
        number = number_of(shown["input"])
        # The adjudicator is shown what the first labellers were: no label of theirs.
        assert {k: v for k, v in shown.items() if k != "assignment"} == {
            k: v for k, v in views[number][0].items() if k != "assignment"
        }
        labels = dict(majority(number, 2))
        left.append(hidden.record_label(shown["assignment"], users[2], labels, org=CLINIC))
    assert left == [{"remaining": n} for n in (3, 2, 1, 0)]
    settled = {}
    for row in rows(store, "evalset_cases", set_id=world.set_id):
        value, _ = world.keys.open("input", row["input"], world.set_id, row["id"])
        settled[number_of(value)] = row["status"]
    assert settled == {
        0: "agreed",
        1: "resolved",  # with the first labeller: nurse
        2: "unresolved",  # nurse, physician, billing
        3: "resolved",  # with the second: physician
        4: "resolved",  # physician (two of three), routine (the first two)
    }
    covered = hidden.measure(world.set_id, resamples=50)["coverage"]["questions"]
    # Settled labels only, a question at a time: nurse/routine (cases 0 and 1) and
    # physician/routine (3 and 4); the unresolved case has none.
    assert covered["destination"]["labelled"] == covered["urgency"]["labelled"] == 4
    assert covered["destination"]["labels"]["nurse"] == {"cases": 2, "distinct": 2}
    assert covered["destination"]["labels"]["physician"] == {"cases": 2, "distinct": 2}
    assert covered["urgency"]["labels"]["routine"] == {"cases": 4, "distinct": 4}
    assert covered["urgency"]["labels"].get("urgent", {"cases": 0})["cases"] == 0


# -- who may label --------------------------------------------------------------------------------


def test_separation_of_duties_refuses_anyone_but_a_labeller_who_does_not_curate(
    world: World,
) -> None:
    store, hidden = world.store, world.hidden
    hidden.add_cases(world.set_id, cases(2))
    hidden.sample(world.set_id, per_stratum=2)
    first, second = labellers(store, 2)
    admin = person(store, "admin-1", roles=("admin",))
    both = person(store, "both-1", roles=("admin", "labeller"))
    viewer = person(store, "viewer-1", roles=("viewer",))
    elsewhere = person(store, "elsewhere-1", org="clinic-b")
    nobody = accounts.person_id(ISSUER, "nobody")
    before = dump(store)
    for pool, reason in (
        ([first, admin], "manages hidden sets in clinic-a"),
        ([first, both], "manages hidden sets in clinic-a"),
        ([first, viewer], "holds no labeller role"),
        ([first, elsewhere], "holds no labeller role"),
        ([first, nobody], "holds no labeller role"),
        ([first], "at least 2 different labellers"),
        ([first, first], "at least 2 different labellers"),
        ([first, "admin@example.org"], "named by their user id"),
    ):
        with pytest.raises(MendariError, match=reason):
            hidden.assign(world.set_id, pool)
    assert dump(store) == before  # the refused runs wrote nothing
    hidden.assign(world.set_id, [first, second])
    label_all(hidden, [first, second])
    tamper(store, "UPDATE evalset_cases SET status = 'disputed'")  # so a third is wanted
    after = dump(store)
    for pool, reason in (([admin], "manages hidden sets"), ([viewer], "holds no labeller role")):
        with pytest.raises(MendariError, match=reason):
            hidden.adjudicate(world.set_id, pool)
    assert dump(store) == after
    assert "admin@example.org" not in json.dumps(chained(store))


def test_an_open_assignment_moves_on_when_its_membership_ends_or_turns_curator(
    world: World,
) -> None:
    store, hidden = world.store, world.hidden
    hidden.add_cases(world.set_id, cases(2))
    hidden.sample(world.set_id, per_stratum=2)
    a, b, c = labellers(store, 3)
    made = hidden.assign(world.set_id, [a, b])
    assert made["assigned"] == 4 and made["withdrawn"] == 0
    held_by_b = [x["assignment"] for x in made["assignments"] if x["labeller"] == b]
    store.remove_membership(membership_of(store, b), org=CLINIC, removed_by="test")
    assert hidden.next_case(b, org=CLINIC) is None
    with pytest.raises(MendariError, match=f"^{NO_ASSIGNMENT}$"):
        hidden.record_label(held_by_b[0], b, ROUTINE, org=CLINIC)
    with pytest.raises(MendariError, match="holds no labeller role"):
        hidden.assign(world.set_id, [a, b])
    store.add_membership(org=CLINIC, roles=["labeller"], created_by="test", user_id=b)
    assert hidden.next_case(b, org=CLINIC) is None  # a new membership: the old work is not back
    moved = hidden.assign(world.set_id, [a, c])
    assert moved["withdrawn"] == 2 and moved["assigned"] == 2
    assert {x["labeller"] for x in moved["assignments"]} == {c}
    assert hidden.assign(world.set_id, [a, b, c])["assigned"] == 0  # each case has its two
    # A labeller made an administrator is served nothing, and their work moves on too.
    store.set_membership_roles(
        membership_of(store, c), ["admin", "labeller"], org=CLINIC, changed_by="test"
    )
    assert hidden.next_case(c, org=CLINIC) is None
    again = hidden.assign(world.set_id, [a, b])
    assert again["withdrawn"] == 2 and {x["labeller"] for x in again["assignments"]} == {b}
    assert label_all(hidden, [a, b, c]) == 4
    assert statuses(world) == {"agreed": 2}
    shown = hidden.progress(world.set_id)["labellers"]
    assert shown[b]["first"] == {"assigned": 4, "labelled": 2, "open": 0, "withdrawn": 2}
    assert shown[c]["first"] == {"assigned": 2, "labelled": 0, "open": 0, "withdrawn": 2}
    assert shown[a]["first"] == {"assigned": 2, "labelled": 2, "open": 0, "withdrawn": 0}
    for label in opened_labels(world):  # each case's two first passes are two people
        assert label["user"] in (a, b)
    assert world.store.verify(evalsets=world.keys)["ok"]


# -- sampling and balance -------------------------------------------------------------------------


def attributes_of(world: World) -> dict[str, dict[str, Any]]:
    return {
        row["id"]: world.keys.open("attributes", row["attributes"], world.set_id, row["id"])[0]
        for row in rows(world.store, "evalset_cases", set_id=world.set_id)
    }


def test_sampling_is_seeded_within_strata_and_records_weights(world: World) -> None:
    store, hidden = world.store, world.hidden
    hidden.add_cases(world.set_id, cases(10, strata=("ward", "ward", "ward", "home", "home")))
    strata: dict[str, list[str]] = {}
    for case_id, body in sorted(attributes_of(world).items()):
        strata.setdefault(body["attributes"]["stratum"], []).append(case_id)
    assert {name: len(ids) for name, ids in strata.items()} == {"ward": 6, "home": 4}
    drawn = hidden.sample(world.set_id, per_stratum=2, seed=7)
    assert drawn == {"set": world.set_id, "drawn": 4, "sampled": 4, "seed": 7}
    rng = random.Random(7)  # the documented draw: strata by name, cases by id
    expected = {case for name in sorted(strata) for case in rng.sample(strata[name], 2)}
    sampled = {row["id"] for row in rows(store, "evalset_cases") if row["status"] == "sampled"}
    assert sampled == expected
    weights = {case: body.get("weight") for case, body in attributes_of(world).items()}
    stratum_of = {case: name for name, ids in strata.items() for case in ids}
    assert weights == {
        case: ({"ward": 3.0, "home": 2.0}[stratum_of[case]] if case in sampled else None)
        for case in weights
    }
    (event,) = events(store, "evalset.sampled")
    assert event["payload"] == {"set": world.set_id, "org": CLINIC, "n": 4, "seed": 7}
    # A second draw, of 3 in all, shared out by what is left (4 and 2): 2 and 1.
    assert hidden.sample(world.set_id, cases=3, seed=1)["sampled"] == 7
    weights = {case: body.get("weight") for case, body in attributes_of(world).items()}
    by_stratum = Counter(stratum_of[case] for case, weight in weights.items() if weight)
    assert by_stratum == {"ward": 4, "home": 3}
    assert {weights[c] for c in strata["ward"] if weights[c]} == {6 / 4}
    assert {weights[c] for c in strata["home"] if weights[c]} == {4 / 3}
    with pytest.raises(MendariError, match="has 3 case\\(s\\) left to draw, not 4"):
        hidden.sample(world.set_id, cases=4)
    assert hidden.sample(world.set_id, per_stratum=10)["drawn"] == 3
    assert {body["weight"] for body in attributes_of(world).values()} == {1.0}
    for given, reason in (
        ({"per_stratum": 1}, "drawn already"),
        ({}, "one of the two"),
        ({"per_stratum": 1, "cases": 1}, "one of the two"),
        ({"cases": 0}, "1 case or more"),
    ):
        with pytest.raises(MendariError, match=reason):
            hidden.sample(world.set_id, **given)
    # The weights sit beside the attributes, outside each case's commitment.
    assert store.verify(evalsets=world.keys)["ok"]
    text = json.dumps([e["payload"] for e in events(store, "evalset.sampled")])
    assert "ward" not in text and "home" not in text


@pytest.mark.parametrize(
    ("count", "people"),
    # Every pair as often as every other (the first four), and a pool of six or more, where
    # putting labellers before pairs gave one pair three cases while others shared none.
    [(6, 4), (12, 3), (15, 6), (45, 10), (12, 6), (30, 6)],
)
def test_pairs_and_labellers_are_balanced_and_deterministic(
    world: World, count: int, people: int
) -> None:
    """Pairs differ by one case at most and labellers by two; when the cases go evenly into the
    pairs, every pair shares as many and every labeller has as many."""
    store, hidden = world.store, world.hidden
    hidden.add_cases(world.set_id, cases(count))
    hidden.sample(world.set_id, per_stratum=count)
    users = labellers(store, people)
    made = hidden.assign(world.set_id, users, seed=5)["assignments"]
    by_case: dict[str, list[str]] = {}
    for assignment in made:
        by_case.setdefault(assignment["case"], []).append(assignment["labeller"])
    assert len(by_case) == count and all(len(set(two)) == 2 for two in by_case.values())
    shared = Counter(tuple(sorted(two)) for two in by_case.values())
    pairs = [shared[pair] for pair in combinations(sorted(users), 2)]
    loads = [Counter(a["labeller"] for a in made)[user] for user in users]
    assert max(pairs) - min(pairs) <= 1, sorted(pairs)
    assert max(loads) - min(loads) <= 2, sorted(loads)
    if count % len(pairs) == 0:
        assert pairs == [count // len(pairs)] * len(pairs)
        assert loads == [2 * count // people] * people
    assert hidden.assign(world.set_id, users, seed=5)["assigned"] == 0  # nothing more to give
    # The same set and seed give the same assignments.
    tamper(store, "DELETE FROM evalset_assignments")
    again = hidden.assign(world.set_id, users, seed=5)["assignments"]
    assert sorted((a["case"], a["labeller"]) for a in again) == sorted(
        (a["case"], a["labeller"]) for a in made
    )


# -- labels ---------------------------------------------------------------------------------------


def test_a_label_is_one_whole_answer_to_one_open_assignment_of_its_labeller(world: World) -> None:
    store, hidden = world.store, world.hidden
    hidden.add_cases(world.set_id, cases(1))
    hidden.sample(world.set_id, cases=1)
    a, b = labellers(store, 2)
    hidden.assign(world.set_id, [a, b])
    mine, theirs = hidden.next_case(a, org=CLINIC), hidden.next_case(b, org=CLINIC)
    assert mine is not None and theirs is not None
    before = dump(store)
    for assignment, user, acting in (
        (str(uuid.uuid4()), a, CLINIC),  # unknown
        (theirs["assignment"], a, CLINIC),  # another labeller's
        ("not-an-assignment", a, CLINIC),
        (mine["assignment"], b, CLINIC),
        (mine["assignment"], "user:not-a-user", CLINIC),
        (mine["assignment"], a, "clinic-b"),  # acting for another organization
    ):
        with pytest.raises(MendariError, match=f"^{NO_ASSIGNMENT}$"):
            hidden.record_label(assignment, user, ROUTINE, org=acting)
    refused: list[tuple[Any, str]] = [
        ({"destination": "nurse"}, "labels answer every question: urgency unanswered"),
        ({**ROUTINE, "zqother": "x"}, "labels name a question this set does not ask"),
        ({"destination": "nurse", "urgency": "zqsoon"}, "labels.urgency is not one of its labels"),
        ({"destination": "nurse", "urgency": 3}, "labels.urgency is not one of its labels"),
        ("nurse", "labels are a mapping"),
    ]
    for values, reason in refused:
        with pytest.raises(MendariError, match=reason) as raised:
            hidden.record_label(mine["assignment"], a, values, org=CLINIC)
        assert "zq" not in str(raised.value)  # what was given is never repeated
    assert dump(store) == before  # nothing written or chained
    assert hidden.record_label(mine["assignment"], a, ROUTINE, org=CLINIC) == {"remaining": 0}
    with pytest.raises(MendariError, match=f"^{NO_ASSIGNMENT}$"):  # one answer, never changed
        hidden.record_label(
            mine["assignment"], a, {**ROUTINE, "destination": "billing"}, org=CLINIC
        )
    (label,) = opened_labels(world)
    assert label["values"] == ROUTINE and label["user"] == a
    (event,) = events(store, "evalset.labelled")
    salt = base64.b64decode(label["salt"])
    ref = label_ref(salt, world.set_id, label["case_id"], label["id"], mine["assignment"], ROUTINE)
    assert event["payload"]["label_ref"] == {"id": label["id"], "sha256": ref}
    # Only a draft set is labelled: a frozen one serves and takes nothing.
    tamper(store, "UPDATE evalset_sets SET state = 'frozen'")
    assert hidden.next_case(b, org=CLINIC) is None
    with pytest.raises(MendariError, match=f"^{NO_ASSIGNMENT}$"):
        hidden.record_label(theirs["assignment"], b, ROUTINE, org=CLINIC)
    for write in (
        lambda: hidden.assign(world.set_id, [a, b]),
        lambda: hidden.sample(world.set_id, cases=1),
        lambda: hidden.measure(world.set_id),
    ):
        with pytest.raises(MendariError, match="only a draft changes"):
            write()


def test_a_labellers_read_is_chained_before_the_case_is_returned(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, hidden = world.store, world.hidden
    hidden.add_cases(world.set_id, cases(1))
    hidden.sample(world.set_id, cases=1)
    a, b = labellers(store, 2)
    stranger = person(store, "stranger-1", org="clinic-b")
    made = hidden.assign(world.set_id, [a, b])
    assert hidden.next_case(stranger, org=CLINIC) is None  # no membership of the set's org
    assert hidden.next_case(a, org="clinic-b") is None  # their assignments are clinic-a's
    assert events(store, "evalset.case_read") == []
    shown = hidden.next_case(a, org=CLINIC)
    assert shown is not None
    case = next(x["case"] for x in made["assignments"] if x["labeller"] == a)
    (read,) = events(store, "evalset.case_read")
    assert read["decision_id"] == f"evalset:{world.set_id}"
    assert read["payload"] == {
        "set": world.set_id,
        "org": CLINIC,
        "case": case,
        "assignment": shown["assignment"],
        "labeller": a,
    }
    assert hidden.next_case(a, org=CLINIC) == shown and len(events(store, "evalset.case_read")) == 2
    real = store._append_many

    def failing(db: Any, event: str, entries: Any) -> Any:
        if event == "evalset.case_read":
            raise RuntimeError("the chain refused")
        return real(db, event, entries)

    monkeypatch.setattr(store, "_append_many", failing)
    before = dump(store)
    with pytest.raises(RuntimeError, match="the chain refused"):
        hidden.next_case(b, org=CLINIC)  # nothing is returned unless its read is chained
    assert dump(store) == before


# -- the chain and verify -------------------------------------------------------------------------


def test_no_label_value_or_label_word_is_in_the_chain(world: World, db_target: str) -> None:
    labelled_set(world)
    world.hidden.measure(world.set_id, resamples=50)
    every = chained(world.store)
    text = json.dumps(every)
    assert LABEL_WORD.search(text) is None, LABEL_WORD.search(text)
    kinds = {event["event"] for event in every}
    assert set(PAYLOADS) <= kinds
    for event in every:
        if event["event"] in PAYLOADS:
            payload = event["payload"]
            assert set(payload) == PAYLOADS[event["event"]], event
            assert payload["org"] == CLINIC and payload["set"] == world.set_id
            if "case" in payload:
                assert CASE_HASH.fullmatch(payload["case"])
            if "labeller" in payload:
                assert USER.fullmatch(payload["labeller"])
            if "assignment" in payload:
                assert UUID.fullmatch(payload["assignment"])
            if "label_ref" in payload:
                assert set(payload["label_ref"]) == {"id", "sha256"}
            if "round" in payload:
                assert payload["round"] in ("first", "adjudication")
    stored = dump(world.store)
    assert LABEL_WORD.search(stored) is None
    for mark in MARKS.values():
        assert mark not in stored
    if not db_target.startswith("postgresql"):
        raw = b"".join(
            Path(f"{db_target}{suffix}").read_bytes()
            for suffix in ("", "-wal")
            if Path(f"{db_target}{suffix}").exists()
        )
        for word in LABEL_WORDS:
            assert word.encode() not in raw, word


def test_audit_verify_recomputes_each_label_and_finds_it_altered(world: World) -> None:
    store, keys = world.store, world.keys
    labelled_set(world)
    checked = store.verify(evalsets=keys)
    assert checked["ok"] and checked["evalsets"] == {
        "sets": 1, "checked": 3, "removed": 0, "unchecked": 0, "assignments": 8, "labels": 8,
    }  # fmt: skip
    without = store.verify(evalsets=True)
    assert without["ok"] and without["evalsets"]["unchecked"] == 3 + 8
    labels = opened_labels(world)
    seq = {e["payload"]["label_ref"]["id"]: e["seq"] for e in events(store, "evalset.labelled")}
    one, other = labels[0], labels[1]
    first = min(seq[one["id"]], seq[other["id"]])

    def broken_at(expected: int) -> None:
        result = store.verify(evalsets=keys)
        assert not result["ok"] and result.get("broken_at") == expected, result

    # Two labels' bodies swapped: each is bound to its own id.
    for label, body in ((one, other["body"]), (other, one["body"])):
        tamper(store, "UPDATE evalset_labels SET body = ? WHERE id = ?", (body, label["id"]))
    broken_at(first)
    failed = world.hidden.check()["failed"]
    assert {(f["kind"], f.get("label")) for f in failed} >= {("label", one["id"])}
    for label in (one, other):
        tamper(
            store, "UPDATE evalset_labels SET body = ? WHERE id = ?", (label["body"], label["id"])
        )
    assert store.verify(evalsets=keys)["ok"]
    # Re-sealed with other values by someone holding the key: its commitment no longer matches.
    other_label = "front_desk" if one["values"]["destination"] != "front_desk" else "billing"
    forged = keys.seal(
        "label",
        {"salt": one["salt"], "values": {**one["values"], "destination": other_label}},
        world.set_id, one["case_id"], one["id"],
    )  # fmt: skip
    tamper(store, "UPDATE evalset_labels SET body = ? WHERE id = ?", (forged, one["id"]))
    broken_at(seq[one["id"]])
    assert ("label", one["id"], "commitment") in {
        (f["kind"], f.get("label"), f["key"]) for f in world.hidden.check()["failed"]
    }
    tamper(store, "UPDATE evalset_labels SET body = ? WHERE id = ?", (one["body"], one["id"]))
    # Pointed at another assignment; its assignment given to another labeller; removed; forged.
    tamper(store, "UPDATE evalset_labels SET assignment_id = ? WHERE id = ?",
           (other["assignment_id"] + "x", one["id"]))  # fmt: skip
    broken_at(seq[one["id"]])
    tamper(store, "UPDATE evalset_labels SET assignment_id = ? WHERE id = ?",
           (one["assignment_id"], one["id"]))  # fmt: skip
    assigned = {e["payload"]["assignment"]: e["seq"] for e in events(store, "evalset.assigned")}
    (row,) = rows(store, "evalset_assignments", id=one["assignment_id"])
    stranger = accounts.person_id(ISSUER, "stranger")
    tamper(store, "UPDATE evalset_assignments SET user_id = ? WHERE id = ?", (stranger, row["id"]))
    broken_at(assigned[row["id"]])
    tamper(store, "UPDATE evalset_assignments SET user_id = ? WHERE id = ?",
           (row["user_id"], row["id"]))  # fmt: skip
    tamper(store, "DELETE FROM evalset_labels WHERE id = ?", (one["id"],))
    broken_at(seq[one["id"]])
    restored = {k: one[k] for k in ("id", "org", "set_id", "case_id", "assignment_id", "body",
                                    "created_at")}  # fmt: skip
    columns = ", ".join(restored)
    marks = ", ".join("?" for _ in restored)
    tamper(store, f"INSERT INTO evalset_labels ({columns}) VALUES ({marks})",
           tuple(restored.values()))  # fmt: skip
    assert store.verify(evalsets=keys)["ok"]
    extra = {**restored, "id": str(uuid.uuid4()), "assignment_id": str(uuid.uuid4())}
    tamper(store, f"INSERT INTO evalset_labels ({columns}) VALUES ({marks})",
           tuple(extra.values()))  # fmt: skip
    result = store.verify(evalsets=keys)
    assert not result["ok"] and result["unchained"] == 1


def test_audit_verify_passes_after_labelling_and_adjudication(
    db_target: str,
    tmp_path: Path,
    inbox_pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """LP-11h's acceptance: the key holder makes a set, draws its cases, assigns them and has
    the disputed ones adjudicated from the command line; the labellers label at the store (no
    route yet); progress, agreement and coverage are read; `mendari audit verify --evalsets`
    passes, every case, assignment and label checked."""
    evalset_on(monkeypatch)
    served(db_target, inbox_pack)
    with AuditStore(db_target) as store:
        users = labellers(store, 3)
    db = ("--db", db_target)
    code, out, _ = run(capsys, "evalset", "create", INBOX, "--org", CLINIC, "--name", "n", *db)
    set_id = json.loads(out)["set"]
    code, out, _ = run(capsys, "evalset", "add", set_id, str(cases_file(tmp_path, cases(3))), *db)
    hashes = [case["case"] for case in json.loads(out)["cases"]]
    code, out, err = run(
        capsys, "evalset", "sample", set_id, "--per-stratum", "3", "--seed", "4", *db
    )
    assert code == 0 and json.loads(out) == {"set": set_id, "drawn": 3, "sampled": 3, "seed": 4}
    assert "evalset assign" in err
    pool = ("--labeller", users[0], "--labeller", users[1])
    code, out, _ = run(capsys, "evalset", "assign", set_id, *pool, *db)
    assigned = json.loads(out)
    assert code == 0 and assigned["assigned"] == 6 and assigned["withdrawn"] == 0
    assert {a["case"] for a in assigned["assignments"]} == set(hashes)

    def labelled(people: Sequence[str], script: Script) -> int:
        keys = evalset_keys()
        assert keys is not None
        with AuditStore(db_target) as store:
            return label_all(EvalsetStore(store, keys), people, script)

    assert labelled(users[:2], split) == 6
    code, out, _ = run(capsys, "evalset", "progress", set_id, *db)
    progress = json.loads(out)
    assert code == 0 and {k: v for k, v in progress["statuses"].items() if v} == {
        "agreed": 1,
        "disputed": 2,
    }
    code, out, _ = run(capsys, "evalset", "adjudicate", set_id, "--labeller", users[2], *db)
    assert code == 0 and json.loads(out)["assigned"] == 2
    assert labelled(users[2:], lambda number, _: split(number, 2)) == 2
    code, out, _ = run(capsys, "evalset", "progress", set_id, *db)
    progress = json.loads(out)
    assert {k: v for k, v in progress["statuses"].items() if v} == {
        "agreed": 1, "resolved": 1, "unresolved": 1,
    }  # fmt: skip
    assert set(progress["labellers"]) == set(users)
    code, text, err = run(capsys, "evalset", "agreement", set_id, "--resamples", "100", *db)
    assert code == 0 and "percent agreement: 1 of 3" in text and "stored sealed" in err
    code, out, _ = run(capsys, "evalset", "agreement", set_id, "--json", "--seed", "1",
                       "--resamples", "100", *db)  # fmt: skip
    measured = json.loads(out)
    assert measured["seeds"] == {"bootstrap": 1, "resamples": 100}
    assert measured["questions"]["destination"]["kappa"][0]["raters"] == sorted(users[:2])
    allowed = ("stratum", "language")  # conflict 55
    code, text, _ = run(capsys, "evalset", "coverage", set_id, *db, allowed=allowed)
    assert code == 0 and "3 cases" in text
    code, out, _ = run(capsys, "evalset", "coverage", set_id, "--json", *db, allowed=allowed)
    covered = json.loads(out)
    assert covered["cases"] == 3 and covered["dataset"] == {"name": set_id}
    for output in (text, out, json.dumps(progress), json.dumps(measured)):
        assert not any(hashed in output for hashed in hashes)  # aggregates name no case
    code, out, _ = run(capsys, "audit", "verify", "--evalsets", *db)
    status = json.loads(out)
    assert code == 0 and status["ok"]
    assert status["evalsets"] == {
        "sets": 1, "checked": 3, "removed": 0, "unchecked": 0, "assignments": 8, "labels": 8,
    }  # fmt: skip
    assert LABEL_WORD.search(out) is None
    assert run(capsys, "audit", "verify", *db)[0] == 0
    code, out, _ = run(capsys, "evalset", "rotate", "--check", *db)
    checked = json.loads(out)
    assert code == 0 and checked["ready"]
    assert sum(checked["by_key"]["evalset_labels"].values()) == 8


def test_a_second_labelling_round_by_the_documented_steps(world: World) -> None:
    """The runbook's steps twice (lesson of 2026-10-06), with the writes a live deployment makes
    between them: the first round's last labels arrive after the second round's draw."""
    store, hidden, keys = world.store, world.hidden, world.keys
    users = labellers(store, 3)
    hidden.add_cases(world.set_id, cases(4))
    assert hidden.sample(world.set_id, cases=2)["sampled"] == 2
    first = hidden.assign(world.set_id, users[:2])
    assert first["assigned"] == 4
    one = hidden.next_case(users[0], org=CLINIC)  # one label of the first round, for now
    assert one is not None
    labels = dict(split(number_of(one["input"]), 0))
    hidden.record_label(one["assignment"], users[0], labels, org=CLINIC)
    assert store.verify(evalsets=keys)["ok"]
    # The second round: more cases written, drawn and assigned while the first is labelled.
    hidden.add_cases(world.set_id, cases(3, start=4))
    assert hidden.sample(world.set_id, cases=3)["sampled"] == 5
    # The first round's other labels arrive while the second round is drawn and assigned.
    label_all(hidden, [users[1]], lambda number, _: split(number, 1))
    assert store.verify(evalsets=keys)["ok"]
    second = hidden.assign(world.set_id, users)
    assert second["assigned"] == 6  # the three new draws only: the first round's keep theirs
    first_cases = {a["case"] for a in first["assignments"]}
    assert not first_cases & {a["case"] for a in second["assignments"]}
    label_all(hidden, users, split)
    hidden.adjudicate(world.set_id, users)
    label_all(hidden, users, lambda number, _: split(number, 2))
    assert store.verify(evalsets=keys)["ok"]
    shown = hidden.progress(world.set_id)
    assert shown["cases"] == 7 and shown["statuses"]["draft"] == 2
    assert sum(shown["statuses"].values()) == 7
    assert shown["statuses"]["sampled"] == shown["statuses"]["disputed"] == 0
    every = opened_labels(world)
    assert len({label["case_id"] for label in every}) == 5
    for case in {label["case_id"] for label in every}:
        given = [label for label in every if label["case_id"] == case]
        firsts = {x["user"] for x in given if x["round"] == "first"}
        thirds = {x["user"] for x in given if x["round"] == "adjudication"}
        assert len(firsts) == 2 and not firsts & thirds and len(given) == 2 + len(thirds)
    aggregates = hidden.measure(world.set_id, resamples=50)
    assert aggregates["n"] == 5 and aggregates["coverage"]["cases"] == 5
    assert store.verify(evalsets=keys)["ok"]


def documented(command: str, set_id: str, db: str, people: Sequence[str]) -> list[str]:
    """The runbook's `mendari evalset <command>` (docs/operations/evalsets.md) as written, for
    `set_id` on `db`, the user ids of its `assign` standing for `people`'s, in order: what
    `main` takes."""
    text = (ROOT / "docs/operations/evalsets.md").read_text().replace("\\\n", " ")
    steps = {
        line.split()[2]: line for line in text.splitlines() if line.startswith("mendari evalset ")
    }
    named = dict.fromkeys(re.findall(r"user:[0-9a-f]{16}", steps["assign"]))
    standing = dict(zip(named, people, strict=True))
    line = steps[command].replace("<set id>", set_id).replace('"$DATABASE_URL"', db)
    return [standing.get(word, word) for word in shlex.split(line)[1:]]


def test_the_runbooks_assign_and_adjudicate_run_as_written(
    db_target: str,
    tmp_path: Path,
    inbox_pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Steps 2 and 4 of the runbook as written, its three user ids standing for three
    labellers: the adjudicators it names hold, for every disputed case, someone who is not one
    of its first two (with three labellers, each is a first-pass labeller of two cases in
    three)."""
    evalset_on(monkeypatch)
    served(db_target, inbox_pack)
    with AuditStore(db_target) as store:
        users = labellers(store, 3)
    db = ("--db", db_target)
    code, out, _ = run(capsys, "evalset", "create", INBOX, "--org", CLINIC, "--name", "n", *db)
    set_id = json.loads(out)["set"]
    run(capsys, "evalset", "add", set_id, str(cases_file(tmp_path, cases(3))), *db)
    assert run(capsys, "evalset", "sample", set_id, "--per-stratum", "3", *db)[0] == 0
    code, out, err = run(capsys, *documented("assign", set_id, db_target, users))
    assert code == 0 and json.loads(out)["assigned"] == 6, err
    keys = evalset_keys()
    assert keys is not None
    with AuditStore(db_target) as store:
        assert label_all(EvalsetStore(store, keys), users, split) == 6
    code, out, err = run(capsys, *documented("adjudicate", set_id, db_target, users))
    assert code == 0, err
    assert json.loads(out)["assigned"] == 2  # cases 1 and 2, disputed


# -- measure and progress -------------------------------------------------------------------------


def test_measure_gives_what_rater_agreement_and_coverage_give_on_the_same_labels(
    world: World,
) -> None:
    store, hidden = world.store, world.hidden
    hidden.add_cases(world.set_id, cases(4))
    with pytest.raises(MendariError, match="is drawn: draw them first"):
        hidden.measure(world.set_id)
    hidden.sample(world.set_id, per_stratum=4)
    users = labellers(store, 3)
    hidden.assign(world.set_id, users)  # pairs vary from case to case
    label_all(hidden, users, split)
    # One more case drawn and labelled once: until its second label is in, agreement leaves it
    # out (conflict 41).
    hidden.add_cases(world.set_id, cases(1, start=4))
    hidden.sample(world.set_id, cases=1)
    waiting, _ = hidden.assign(world.set_id, users)["assignments"]
    shown = hidden.next_case(waiting["labeller"], org=CLINIC)
    assert shown is not None and shown["assignment"] == waiting["assignment"]
    hidden.record_label(shown["assignment"], waiting["labeller"], ROUTINE, org=CLINIC)
    measured = hidden.measure(world.set_id, seed=3, resamples=200)
    assert set(measured) == {"format", "set", "measured_at", "n", "coverage", "agreement"}
    assert (measured["format"], measured["set"], measured["n"]) == (
        AGGREGATES_FORMAT,
        world.set_id,
        5,
    )
    # The same labels, opened by the key holder: first passes of the cases both labellers
    # answered, in case-id order, raters by user id.
    by_case: dict[str, dict[str, dict[str, str]]] = {}
    for label in opened_labels(world):
        if label["round"] == "first":
            by_case.setdefault(label["case_id"], {})[label["user"]] = label["values"]
    rated = [by_case[case] for case in sorted(by_case) if len(by_case[case]) == 2]
    assert len(by_case) == 5 and len(rated) == 4
    manifest = world.pack.manifest
    assert measured["agreement"] == agreement(manifest, rated, pack=INBOX, seed=3, resamples=200)
    assert {q: v["cases"] for q, v in measured["agreement"]["questions"].items()} == dict.fromkeys(
        manifest.questions, 4
    )
    records = []
    for row in rows(store, "evalset_cases", set_id=world.set_id):
        given = list(by_case.get(row["id"], {}).values())
        settled = given[0] if row["status"] == "agreed" else {}
        value, _ = world.keys.open("input", row["input"], world.set_id, row["id"])
        body, _ = world.keys.open("attributes", row["attributes"], world.set_id, row["id"])
        records.append({"id": row["case_hash"], "input": value, "attributes": body["attributes"],
                        "labels": with_derived(manifest, settled) if settled else {}})  # fmt: skip
    expected = coverage(manifest, Dataset(world.set_id, Path("x"), records, ""), pack=INBOX)
    assert measured["coverage"] == {**expected, "dataset": {"name": world.set_id}}
    (head,) = rows(store, "evalset_sets", id=world.set_id)
    assert world.keys.open("aggregates", head["aggregates"], world.set_id)[0] == measured
    (event,) = events(store, "evalset.measured")
    assert event["payload"] == {"set": world.set_id, "org": CLINIC, "n": 5}
    guard = resolve_pack("mendari/ai-guard")
    with pytest.raises(MendariError, match="does not ask the set's questions"):
        hidden.measure(world.set_id, pack=guard)
    assert len(events(store, "evalset.measured")) == 1


def test_progress_names_no_case_and_needs_no_key(world: World, db_target: str) -> None:
    users = labelled_set(world)
    shown = world.hidden.progress(world.set_id)
    assert set(shown) == {"set", "org", "state", "cases", "statuses", "labellers"}
    assert shown["cases"] == 3 and set(shown["labellers"]) == set(users)
    text = json.dumps(shown)
    named = [
        value
        for table, columns in (
            ("evalset_cases", ("id", "case_hash")),
            ("evalset_assignments", ("id",)),
            ("evalset_labels", ("id",)),
        )
        for row in rows(world.store, table)
        for value in (row[column] for column in columns)
    ]
    assert named and not any(value in text for value in named)
    for labeller, rounds in shown["labellers"].items():
        for counted in rounds.values():
            assert counted["assigned"] == sum(
                counted[k] for k in ("labelled", "open", "withdrawn")
            ), labeller
    with AuditStore(db_target, read_only=True) as reading:
        assert EvalsetStore(reading).progress(world.set_id) == shown  # no key, nothing opened
    with pytest.raises(MendariError, match="no evaluation set"):
        world.hidden.progress(str(uuid.uuid4()))


# -- what else holds ------------------------------------------------------------------------------


def test_a_removed_case_takes_its_assignments_and_labels_with_it(world: World) -> None:
    store, hidden = world.store, world.hidden
    labelled_set(world)
    unresolved = next(row for row in rows(store, "evalset_cases") if row["status"] == "unresolved")
    hidden.remove_case(world.set_id, unresolved["case_hash"])  # so the set may freeze (LP-11i)
    assert rows(store, "evalset_labels", case_id=unresolved["id"]) == []
    assert rows(store, "evalset_assignments", case_id=unresolved["id"]) == []
    checked = store.verify(evalsets=world.keys)
    assert checked["ok"] and checked["evalsets"]["removed"] == 1
    assert (checked["evalsets"]["assignments"], checked["evalsets"]["labels"]) == (5, 5)


def test_retention_erasure_and_phi_rotation_leave_labelling_alone(
    db_target: str, inbox_pack: Pack
) -> None:
    old, new, hash_key = (evalset_keys_for_test().key for _ in range(3))
    keys = evalset_keys_for_test()
    with AuditStore(db_target, phi=PhiPolicy("encrypt", old, hash_key)) as store:
        urgent = {"message": {"body": "Crushing chest pain and my left arm is numb"}}
        decided = [Decision(inbox_pack, store=store)(urgent).id for _ in "ab"]
        hidden = EvalsetStore(store, keys)
        world = World(store, hidden, keys, inbox_pack, a_set(hidden, inbox_pack))
        labelled_set(world)
        before = {t: v for t, v in json.loads(dump(store)).items() if t.startswith("evalset_")}
    with AuditStore(db_target, phi=PhiPolicy("encrypt", new, hash_key, (old,))) as store:
        store.rotate_phi(to=key_id(new))
        store.erase_input(decided[1], erased_by="dpo", reason="request")
        store.purge(0, source="command")
        after = {t: v for t, v in json.loads(dump(store)).items() if t.startswith("evalset_")}
        assert after == before
        assert store.verify(evalsets=keys)["ok"]


def test_the_labelling_events_are_the_organizations_administrative_events(world: World) -> None:
    users = labelled_set(world)
    world.hidden.measure(world.set_id, resamples=50)
    items = world.store.administrative_events(CLINIC, limit=500).rows
    kinds = {item["event"] for item in items}
    assert set(PAYLOADS) <= kinds
    assert world.store.administrative_events("clinic-b", limit=500).rows == []
    rendered = [
        audit_fhir.administrative_event(item) for item in items if item["event"] in PAYLOADS
    ]
    for resource in rendered:
        assert fhir_structure.check(resource) == [], resource
        kind = resource["subtype"][0]["code"]
        agent = resource["agent"][0]["who"]["display"]
        if kind in ("evalset.case_read", "evalset.labelled"):
            assert agent in users  # the labeller acted
        details = {d["type"] for d in resource["entity"][0]["detail"]}
        assert details <= {"hash", "org"}, details  # no case hash, assignment or commitment
    assert LABEL_WORD.search(json.dumps(rendered)) is None


def test_the_labelling_commands_refuse_while_the_keys_are_unset(
    tmp_path: Path, inbox_pack: Pack, capsys: pytest.CaptureFixture[str]
) -> None:
    db = str(tmp_path / "mendari.db")
    served(db, inbox_pack)
    before = Path(db).read_bytes()
    set_id = str(uuid.uuid4())
    user = accounts.person_id(ISSUER, "x")
    for argv in (
        ("evalset", "sample", set_id, "--cases", "1"),
        ("evalset", "assign", set_id, "--labeller", user),
        ("evalset", "adjudicate", set_id, "--labeller", user),
        ("evalset", "agreement", set_id),
        ("evalset", "coverage", set_id),
    ):
        code, _, err = run(capsys, *argv, "--db", db)
        assert code == 1 and err.startswith("error: hidden evaluation sets are off"), err
    code, _, err = run(capsys, "evalset", "progress", set_id, "--db", db)  # read-only, no key
    assert code == 1 and err.strip() == f"error: no evaluation set {set_id}"
    code, _, err = run(capsys, "evalset", "agreement", set_id, "--resamples", "0", "--db", db)
    assert code == 1 and "--resamples is 1 to" in err
    assert Path(db).read_bytes() == before


def test_a_database_from_before_labelling_reads_as_having_none(
    world: World, db_target: str
) -> None:
    world.hidden.add_cases(world.set_id, cases(2))
    with world.store._db.session(exclusive=True) as db:
        db.all("DROP TABLE evalset_labels")
        db.all("DROP TABLE evalset_assignments")
    with AuditStore(db_target, read_only=True) as older:
        hidden = EvalsetStore(older, world.keys)
        shown = hidden.progress(world.set_id)
        assert shown["labellers"] == {} and shown["statuses"]["draft"] == 2
        checked = older.verify(evalsets=world.keys)
        assert checked["ok"] and checked["evalsets"]["assignments"] == 0
        assert hidden.check()["ready"]
        assert {line.get("table") for line in hidden.export()[1:]} == {
            "evalset_sets",
            "evalset_cases",
        }


def test_the_roles_that_curate_and_label_are_the_servers() -> None:
    from mendari.server.auth import PERMISSIONS

    curating = {role for role, granted in PERMISSIONS.items() if "curate" in granted}
    assert set(accounts.CURATING) == curating
    assert PERMISSIONS[accounts.LABELLER] == {"label"}
    assert accounts.USER_ID.fullmatch(accounts.person_id(ISSUER, "anyone"))
