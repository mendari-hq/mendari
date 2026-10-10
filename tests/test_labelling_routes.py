# SPDX-License-Identifier: Apache-2.0
"""A labeller's routes over hidden evaluation sets (LP-11j, `server/app.py`
`add_labelling_routes`, ADR-0011): `GET /api/labelling` (their own counts, set by set),
`GET /api/labelling/next` (their next case: its input and the set's questions, or `204`) and
`POST /api/labelling/{assignment_id}` (their label: `{remaining}`).

- Blind over HTTP: the second labeller is shown the input and the questions only, the same
  before and after the first answers, with a decision on the same input kept beside the set;
  neither is told how their label compares.
- Someone else's assignment, another organization's, an unknown one, a malformed one and a
  closed one (answered, withdrawn, or in a set no longer a draft) answer one `404` and write
  nothing; a `422` names the question path, never a value.
- Only a person holding `label` reaches them: anonymous `401`; a member of each other role,
  administrators included, `403`; every key (a legacy key whose role is `labeller` and the
  grading key among them) the `people_only` `403`; a person of no organization
  `NoOrganization`. Whoever also manages the organization's sets is served nothing (separation
  of duties), and a curator is never assigned. The notice does not gate labelling.
- Mounted only in memberships mode with both evaluation-set keys: absent in claims mode (where
  the paths read as any route no rule lists, `admin`) and without the keys.
- Each read is chained before it is answered (`evalset.case_read`), each label too
  (`evalset.labelled`); the security log names set ids only. What each route opens of the sealed
  set is what the threat model says (I13). Answers are closed models, and the reference documents
  the routes under their own tag.
On SQLite and Postgres (`db_target`) where the store is written; keys are made per test, cases
are hand-written synthetic text."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from mendari import AuditStore, Decision, Pack
from mendari.core.evalset_keys import EvalsetKeys
from mendari.core.evalsets import NO_ASSIGNMENT, EvalsetStore
from mendari.errors import MendariError
from mendari.server.app import (
    LabelledSet,
    LabellingCase,
    LabellingCounts,
    LabelRecorded,
    create_app,
    mounted_routes,
)
from mendari.server.auth import ROLES, permission_for, person_id
from tests.conftest import (
    encrypt_mode,
    encrypted_store,
    evalset_on,
    launched,
    member,
    signed_in,
    without_request_id,
)
from tests.test_auth import CLIENT, IDP, legacy_key
from tests.test_evalset_labelling import ROUTINE, cases, dump, events
from tests.test_evalset_store import CLINIC, MARKS, a_set, tamper
from tests.test_security_log import logged
from tests.test_threat_model import rows

HTTPS = "https://mendari.test"
OTHER = "clinic-b"
LABELLING = "/api/labelling"
NEXT = "/api/labelling/next"
LABEL = "/api/labelling/{assignment_id}"
ROUTES = {("GET", LABELLING), ("GET", NEXT), ("POST", LABEL)}
CASE_KEYS = {"assignment", "questions", "input"}
PHYSICIAN = {"destination": "physician", "urgency": "routine"}
# How the threat model names each kind of sealed value a route opens (row I13).
SEALED_WORDS = {
    "header": "header",
    "input": "input",
    "attributes?": "attributes",
    "labels?": "label",
    "freeze": "freeze",
    "aggregates": "aggregates",
}


@dataclass
class World:
    client: TestClient
    store: AuditStore
    hidden: EvalsetStore
    keys: EvalsetKeys
    pack: Pack
    db: str

    def person(self, sub: str, *roles: str, org: str = CLINIC) -> tuple[str, dict[str, str]]:
        """A person signed in with a membership of `org` holding `roles` (`labeller` unless
        given; the one they hold already, if any): their user id, and their session's headers."""
        headers = member(self.store, org=org, roles=list(roles or ("labeller",)), sub=sub)
        return person_id(IDP, sub), headers

    def drawn(self, users: list[str], count: int = 1, *, org: str = CLINIC) -> str:
        """A draft set of `org`'s with `count` hand-written cases, drawn and assigned to
        `users`, two labellers a case: its id."""
        set_id = a_set(self.hidden, self.pack, org)
        self.hidden.add_cases(set_id, cases(count))
        self.hidden.sample(set_id, per_stratum=count)
        self.hidden.assign(set_id, users)
        return set_id

    def next(self, headers: dict[str, str]) -> Any:
        return self.client.get(NEXT, headers=headers)

    def label(self, assignment: str, labels: Any, headers: dict[str, str]) -> Any:
        return self.client.post(label_url(assignment), json={"labels": labels}, headers=headers)


def memberships(monkeypatch: pytest.MonkeyPatch) -> EvalsetKeys:
    """A deployment in memberships mode with sign-in, PHI mode `encrypt` and the evaluation-set
    keys (throwaway ones, made here): those keys."""
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", CLIENT)
    encrypt_mode(monkeypatch)
    return EvalsetKeys(*evalset_on(monkeypatch))


@contextmanager
def started(db: str, pack: Pack, keys: EvalsetKeys) -> Iterator[World]:
    """The deployment started on `db`, and the key holder's store beside it."""
    with (
        encrypted_store(db) as store,
        TestClient(create_app(db_path=db), base_url=HTTPS) as client,
    ):
        yield World(client, store, EvalsetStore(store, keys), keys, pack, db)


@pytest.fixture
def world(db_target: str, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    with started(db_target, inbox_pack, memberships(monkeypatch)) as world:
        yield world


def label_url(assignment: str) -> str:
    return f"/api/labelling/{assignment}"


def case_ids(world: World, set_id: str) -> list[str]:
    with world.store._db.session(exclusive=False) as db:
        found = db.all("SELECT id FROM evalset_cases WHERE set_id = ? ORDER BY id", (set_id,))
    return [str(row["id"]) for row in found]


# -- blind ----------------------------------------------------------------------------------------


def test_a_second_labeller_never_sees_the_first_labellers_answer_or_any_model_output(
    world: World,
) -> None:
    """Over HTTP, with a decision on the same input kept beside the set: the second labeller is
    shown the input and the set's questions only, the same before and after the first answers;
    each answer says how many cases are left, never how a label compares."""
    (first, a), (second, b) = world.person("labeller-a"), world.person("labeller-b")
    case = cases(1)[0]
    set_id = a_set(world.hidden, world.pack)
    world.hidden.add_cases(set_id, [case])
    decided = Decision(world.pack, store=world.store)(case["input"])  # what a model made of it
    world.hidden.sample(set_id, cases=1)
    world.hidden.assign(set_id, [first, second])
    before, shown = world.next(b), world.next(a)
    assert before.status_code == shown.status_code == 200
    assert before.headers["cache-control"] == "no-store"
    assert set(shown.json()) == set(before.json()) == CASE_KEYS
    assert shown.json()["input"] == before.json()["input"] == case["input"]
    manifest = world.pack.manifest
    asked = {question: manifest.labels(question) for question in manifest.questions}
    assert shown.json()["questions"] == before.json()["questions"] == asked
    told = world.label(shown.json()["assignment"], {**PHYSICIAN, "urgency": "urgent"}, a)
    assert (told.status_code, told.json()) == (200, {"remaining": 0})
    after = world.next(b)
    assert after.json() == before.json()  # nothing of the first answer reaches the second
    text = after.text + json.dumps(dict(after.headers))
    for private in (
        decided.id,
        set_id,
        *case_ids(world, set_id),
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
        "hmac-sha256",
    ):
        assert private not in text, private
    told = world.label(after.json()["assignment"], ROUTINE, b)
    assert (told.status_code, told.json()) == (200, {"remaining": 0})
    assert world.next(b).status_code == 204
    assert world.hidden.progress(set_id)["statuses"]["disputed"] == 1  # neither was told


def test_each_read_and_label_is_chained_and_logged_by_set_id_only(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """A read is chained before it is answered, every time it is shown (`evalset.case_read`),
    and a label as its commitment (`evalset.labelled`); the security log says `evalset.case.read`
    and `evalset.labelled` with the set's id alone, and the access line the route's template:
    no assignment, case, label or input. The counts follow, set by set."""
    (first, a), (second, _) = world.person("labeller-a"), world.person("labeller-b")
    set_id = world.drawn([first, second], count=2)
    counts = world.client.get(LABELLING, headers=a)
    assert counts.status_code == 200
    assert counts.json() == {"sets": [{"set": set_id, "open": 2, "labelled": 0}], "remaining": 2}
    capsys.readouterr()
    shown = world.next(a).json()
    assert world.next(a).json() == shown  # the oldest open case, until it is answered
    reads = events(world.store, "evalset.case_read")
    assert [e["payload"]["assignment"] for e in reads] == [shown["assignment"]] * 2
    assert world.label(shown["assignment"], ROUTINE, a).json() == {"remaining": 1}
    (labelled,) = events(world.store, "evalset.labelled")
    assert labelled["payload"]["labeller"] == first
    lines = logged(capsys)
    said = [(line["event"], line.get("ids"), line["outcome"]) for line in lines]
    assert [entry for entry in said if entry[0] != "request"] == [
        ("evalset.case.read", [set_id], "ok"),
        ("evalset.case.read", [set_id], "ok"),
        ("evalset.labelled", [set_id], "ok"),
    ]
    assert {line["route"] for line in lines if line["event"] == "request"} == {NEXT, LABEL}
    assert {line["actor"] for line in lines} == {first}
    written = json.dumps(lines)
    for private in (shown["assignment"], *case_ids(world, set_id), MARKS["input"], "routine"):
        assert private not in written, private
    assert world.client.get(LABELLING, headers=a).json() == {
        "sets": [{"set": set_id, "open": 1, "labelled": 1}],
        "remaining": 1,
    }
    assert world.store.verify(evalsets=world.keys)["ok"]


def test_what_each_route_opens_is_what_the_threat_model_says(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every sealed value each route opens in the server (`EvalsetKeys.open`), as threat model
    row I13 says of it: the counts, nothing; the next case, the set's header and the case's
    input; a label, the set's header (to check it) and every label the case holds (to settle
    it), the other labeller's among them, though it answers only how many cases are left."""
    (first, a), (second, b) = world.person("labeller-a"), world.person("labeller-b")
    world.drawn([first, second])
    opened: list[tuple[str, str]] = []
    real = EvalsetKeys.open

    def spied(
        keys: EvalsetKeys, kind: Any, token: str, set_id: str, case_id: str = "", label_id: str = ""
    ) -> tuple[Any, str]:
        opened.append((kind, label_id))
        return real(keys, kind, token, set_id, case_id, label_id)

    monkeypatch.setattr(EvalsetKeys, "open", spied)

    def opens(method: str, path: str, headers: dict[str, str], **sent: Any) -> tuple[Any, set[str]]:
        opened.clear()
        answer = world.client.request(method, path, headers=headers, **sent)
        assert answer.is_success, (path, answer.text)
        return answer, {kind for kind, _ in opened}

    _, counted = opens("GET", LABELLING, a)
    shown, read = opens("GET", NEXT, a)
    url = label_url(shown.json()["assignment"])
    _, checked = opens("POST", url, a, json={"labels": ROUTINE})
    shown, read_again = opens("GET", NEXT, b)
    url = label_url(shown.json()["assignment"])
    told, settled = opens("POST", url, b, json={"labels": PHYSICIAN})
    assert told.json() == {"remaining": 0}
    others = {label_id for kind, label_id in opened if kind == "label"}
    (theirs, _) = events(world.store, "evalset.labelled")
    assert theirs["payload"]["label_ref"]["id"] in others  # the first's, on the second's request
    seen = {
        ("GET", LABELLING): counted,
        ("GET", NEXT): read | read_again,
        ("POST", LABEL): checked | settled,
    }
    assert seen == {
        ("GET", LABELLING): set(),
        ("GET", NEXT): {"header", "input"},
        ("POST", LABEL): {"header", "label"},
    }
    (row,) = [line for line in rows() if line.startswith("| I13 |")]
    for (method, path), kinds in seen.items():
        said = re.search(rf"`{method} {re.escape(path)}` opens ([^;.]*)", row)
        assert said is not None, (method, path)
        named = {kind for word, kind in SEALED_WORDS.items() if re.search(rf"\b{word}\b", said[1])}
        assert named == kinds, (method, path, said[1])


def test_three_labellers_through_a_set_over_http(world: World) -> None:
    """Three cases, labelled over HTTP only: two first passes that disagree on every case, then
    a third labeller adjudicating each from the input alone; every answer counts down, each
    labeller's counts end at their three, and the set verifies."""
    users = [world.person(f"labeller-{n}") for n in range(3)]
    set_id = world.drawn([users[0][0], users[1][0]], count=3)

    def label_all(headers: dict[str, str], labels: dict[str, str]) -> list[int]:
        left = []
        while (shown := world.next(headers)).status_code == 200:
            assert set(shown.json()) == CASE_KEYS
            left.append(
                world.label(shown.json()["assignment"], labels, headers).json()["remaining"]
            )
        assert (shown.status_code, shown.content) == (204, b"")
        return left

    assert label_all(users[0][1], ROUTINE) == [2, 1, 0]
    assert label_all(users[1][1], PHYSICIAN) == [2, 1, 0]
    assert world.hidden.progress(set_id)["statuses"]["disputed"] == 3
    world.hidden.adjudicate(set_id, [users[2][0]])
    assert label_all(users[2][1], ROUTINE) == [2, 1, 0]
    assert world.hidden.progress(set_id)["statuses"]["resolved"] == 3
    for _, headers in users:
        assert world.client.get(LABELLING, headers=headers).json() == {
            "sets": [{"set": set_id, "open": 0, "labelled": 3}],
            "remaining": 0,
        }
    assert world.store.verify(evalsets=world.keys)["ok"]


# -- one answer for every assignment not yours ----------------------------------------------------


def test_someone_elses_assignment_answers_as_an_unknown_one_does(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """A labeller of the same organization, a labeller of another, and the labeller themselves
    once it is answered: each `404` is the one an unknown id or a malformed one gets, naming no
    id; nothing is written or chained, and the security log says `no_assignment` with no id."""
    (first, a), (second, b) = world.person("labeller-a"), world.person("labeller-b")
    _, stranger = world.person("labeller-z", org=OTHER)
    set_id = world.drawn([first, second])
    mine = world.next(a).json()["assignment"]

    def refused(assignment: str, headers: dict[str, str]) -> dict[str, Any]:
        before = dump(world.store)
        answer = world.label(assignment, ROUTINE, headers)
        assert answer.status_code == 404, (assignment, answer.text)
        assert dump(world.store) == before, assignment
        assert assignment not in answer.text
        return without_request_id(answer.json(), answer)

    capsys.readouterr()
    answers = [
        refused(mine, b),  # another labeller's of this organization
        refused(mine, stranger),  # another organization's labeller
        refused(str(uuid.uuid4()), a),  # unknown
        refused("not-an-id", a),  # malformed
        refused(mine.upper(), a),  # not as the store minted it
    ]
    assert world.label(mine, ROUTINE, a).status_code == 200
    answers.append(refused(mine, a))  # closed: answered, never changed
    assert answers == [{"error": "NoAssignment", "detail": NO_ASSIGNMENT}] * len(answers)
    lines = [line for line in logged(capsys) if line["event"] == "evalset.labelled"]
    assert [(line["outcome"], line.get("reason"), line.get("ids")) for line in lines] == [
        *[("refused", "no_assignment", None)] * 5,
        ("ok", None, [set_id]),
        ("refused", "no_assignment", None),
    ]


def test_a_withdrawn_assignment_and_a_frozen_set_are_closed_to_labelling(world: World) -> None:
    """An assignment withdrawn once its membership ended (and given to another), and an open
    assignment on a frozen set (which no store call leaves: written behind the store's back),
    are served by nothing and refused as an unknown one is; the counts show no case waiting,
    and nothing is written or chained."""
    (first, a), (second, b), (third, c) = (world.person(f"labeller-{n}") for n in range(3))
    set_id = world.drawn([first, second])
    taken = world.next(b).json()["assignment"]
    membership = world.store.membership_in_force(second, CLINIC)
    assert membership is not None
    world.store.remove_membership(membership["id"], org=CLINIC, removed_by="test")
    world.hidden.assign(set_id, [first, third])  # the second's case moves to the third
    _, rejoined = world.person("labeller-1")  # a new membership: the old work is not back
    assert world.next(rejoined).status_code == 204
    gone = world.label(taken, ROUTINE, rejoined)
    assert (gone.status_code, gone.json()["error"]) == (404, "NoAssignment")
    assert world.client.get(LABELLING, headers=rejoined).json() == {"sets": [], "remaining": 0}
    for headers in (a, c):
        assert world.label(world.next(headers).json()["assignment"], ROUTINE, headers).is_success
    world.hidden.freeze(set_id, max_runs=3, max_age_days=30, resamples=50)
    held = world.store.membership_in_force(first, CLINIC)
    assert held is not None
    opened = str(uuid.uuid4())
    tamper(
        world.store,
        "INSERT INTO evalset_assignments (id, org, set_id, case_id, user_id, membership_id, "
        "round, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (opened, CLINIC, set_id, case_ids(world, set_id)[0], first, held["id"], "first", "2026"),
    )
    before = dump(world.store)
    assert world.next(a).status_code == 204
    frozen = world.label(opened, ROUTINE, a)
    assert (frozen.status_code, frozen.json()["detail"]) == (404, NO_ASSIGNMENT)
    assert world.client.get(LABELLING, headers=a).json() == {
        "sets": [{"set": set_id, "open": 0, "labelled": 1}],
        "remaining": 0,
    }
    assert dump(world.store) == before


def test_a_422_names_the_question_path_never_a_value(world: World) -> None:
    """A label that answers a question with no label of its own, leaves one out, names one the
    set does not ask, or answers with something not text: a `422` naming the question (or
    none), never what was sent; nothing written or chained, and the case still waits."""
    (first, a), (second, _) = world.person("labeller-a"), world.person("labeller-b")
    world.drawn([first, second])
    assignment = world.next(a).json()["assignment"]
    value = "zqvaluemark"
    before = dump(world.store)
    for labels, said in (
        ({"destination": value, "urgency": "routine"}, "labels.destination is not one of its"),
        ({"destination": "nurse"}, "labels answer every question: urgency unanswered"),
        ({**ROUTINE, value: "nurse"}, "labels name a question this set does not ask"),
    ):
        answer = world.label(assignment, labels, a)
        assert answer.status_code == 422, labels
        assert answer.json()["error"] == "LabelError" and said in answer.json()["detail"]
        assert value not in answer.text
    typed = world.label(assignment, {"destination": 7, "urgency": "routine"}, a)
    assert typed.status_code == 422
    assert [(p["loc"], p["type"]) for p in typed.json()["detail"]] == [
        (["body", "labels", "destination"], "string_type")
    ]
    assert "7" not in json.dumps(typed.json()["detail"])
    missing = world.client.post(label_url(assignment), json={}, headers=a)
    assert [p["loc"] for p in missing.json()["detail"]] == [["body", "labels"]]
    assert dump(world.store) == before
    assert world.next(a).json()["assignment"] == assignment


# -- who reaches them -----------------------------------------------------------------------------


def test_only_a_person_holding_label_reaches_them(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """Anonymous: `401`. A member of each role but the labeller's, administrators included:
    `403`. Every key, whatever its role (a key of each machine role, a legacy key of each other
    role, the labeller's among them, and the grading key): the `people_only` `403`. An EHR
    launch's cookie is no credential here: `401`. A person of no organization:
    `NoOrganization`. The labeller: answered."""
    (first, a), (second, _) = world.person("labeller-a"), world.person("labeller-b")
    world.drawn([first, second])
    secrets = {
        role: world.store.create_api_key(name=role, role=role, org=CLINIC, created_by="t")["key"]
        for role in ("viewer", "service")
    }
    for role in sorted({*ROLES, "labeller"} - set(secrets)):
        secrets[f"legacy {role}"] = legacy_key(world.db, role, CLINIC)
    grading = world.store.create_grading_key(org=CLINIC, created_by="t", reason="the matrix")
    secrets["grading"] = grading["key"]
    people = {role: world.person(f"member-{role}", role)[1] for role in ROLES}
    launch = launched(world.store)
    nobody = member(world.store, org=None, sub="nobody")
    capsys.readouterr()

    def answer(method: str, path: str, headers: dict[str, str]) -> Any:
        sent: dict[str, Any] = {"json": {"labels": ROUTINE}} if method == "POST" else {}
        return world.client.request(method, path, headers=headers, **sent)

    for method, path in (("GET", LABELLING), ("GET", NEXT), ("POST", label_url(str(uuid.uuid4())))):
        assert answer(method, path, {}).status_code == 401, path
        assert answer(method, path, launch).status_code == 401, path
        for role, headers in people.items():
            refused = answer(method, path, headers)
            assert (refused.status_code, refused.json()["detail"]) == (
                403,
                "your role may not label here",
            ), (path, role)
        for name, secret in secrets.items():
            refused = answer(method, path, {"authorization": f"Bearer {secret}"})
            assert (refused.status_code, refused.json()["detail"]) == (
                403,
                "only a person who signed in may do this",
            ), (path, name)
        refused = answer(method, path, nobody)
        assert (refused.status_code, refused.json()["error"]) == (403, "NoOrganization"), path
    lines = logged(capsys)
    refusals = {line["reason"] for line in lines if line["event"] == "auth.permission.refused"}
    assert refusals == {"label", "people_only", "no_organization"}
    assert not [line for line in lines if line["event"].startswith("evalset.")]
    assert world.client.get(LABELLING, headers=a).status_code == 200
    assert world.next(a).status_code == 200


def test_whoever_manages_the_sets_labels_nothing_and_is_never_assigned(world: World) -> None:
    """Separation of duties: an administrator is never assigned a case, also when they hold the
    labeller role too; a labeller made an administrator since is served nothing of what they
    were given, and their label is refused as an unknown assignment is."""
    (first, a), (second, _) = world.person("labeller-a"), world.person("labeller-b")
    curator, _ = world.person("curator", "admin", "labeller")
    set_id = world.drawn([first, second])
    with pytest.raises(MendariError, match="manages hidden sets"):
        world.hidden.assign(set_id, [curator, first])
    assignment = world.next(a).json()["assignment"]
    membership = world.store.membership_in_force(first, CLINIC)
    assert membership is not None
    world.store.set_membership_roles(
        membership["id"], ["labeller", "admin"], org=CLINIC, changed_by="test"
    )
    assert world.next(a).status_code == 204
    assert world.client.get(LABELLING, headers=a).json() == {"sets": [], "remaining": 0}
    before = dump(world.store)
    refused = world.label(assignment, ROUTINE, a)
    assert (refused.status_code, refused.json()["error"]) == (404, "NoAssignment")
    assert dump(world.store) == before


def test_labelling_is_not_gated_by_the_notice(
    db_target: str, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The notice gates deciding and reviewing (conflict 20): a labeller who never acknowledged
    it labels, and is never asked to."""
    keys = memberships(monkeypatch)
    monkeypatch.setenv("MENDARI_NOTICE_ACKNOWLEDGEMENT", "required")
    with started(db_target, inbox_pack, keys) as world:
        (first, a), (second, _) = world.person("labeller-a"), world.person("labeller-b")
        world.drawn([first, second])
        shown = world.next(a)
        assert shown.status_code == 200 and "mendari-notice-required" not in shown.headers
        told = world.label(shown.json()["assignment"], ROUTINE, a)
        assert (told.status_code, told.json()) == (200, {"remaining": 0})


def test_a_labellers_calls_are_limited_for_each_labeller(
    db_target: str, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `label` class (server/limits.py): a labeller's calls, counted for each person, since
    each case shown is chained. Over the limit: a `429` with `Retry-After`, the case not read
    (nothing chained); another labeller is not held up."""
    keys = memberships(monkeypatch)
    monkeypatch.setenv("MENDARI_RATE_LIMITS", "on,label=2/min:2")
    with started(db_target, inbox_pack, keys) as world:
        (first, a), (second, b) = world.person("labeller-a"), world.person("labeller-b")
        world.drawn([first, second])
        assert world.client.get(LABELLING, headers=a).status_code == 200
        assert world.next(a).status_code == 200
        before = dump(world.store)
        refused = world.next(a)
        assert refused.status_code == 429 and int(refused.headers["retry-after"]) >= 1
        assert dump(world.store) == before
        assert world.next(b).status_code == 200


# -- where they are mounted, and what they say ----------------------------------------------------


def labelling_routes(app: Any) -> set[tuple[str, str]]:
    return {(m, p) for m, p in mounted_routes(app) if p.startswith(LABELLING)}


def test_the_routes_are_absent_in_claims_mode_and_without_the_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With memberships mode and both keys: the three routes, each needing `label`. Claims mode
    with the keys (the hosted site has neither: owner decision 1): none mounted, and the paths
    read as any route no rule lists, `admin` (an administrator's `404`, anyone else's `403`).
    Memberships mode without the keys: none mounted, a labeller's `404`."""
    memberships(monkeypatch)
    assert labelling_routes(create_app(db_path=tmp_path / "both.db")) == ROUTES
    assert {permission_for(m, p) for m, p in ROUTES} == {"label"}
    monkeypatch.setenv("MENDARI_ACCOUNTS", "claims")
    claims = create_app(db_path=tmp_path / "claims.db")
    assert labelling_routes(claims) == set()
    with AuditStore(tmp_path / "claims.db") as store:
        admin, viewer = signed_in(store, role="admin"), signed_in(store, role="viewer")
    client = TestClient(claims, base_url=HTTPS)
    for method, path in (("GET", LABELLING), ("GET", NEXT), ("POST", label_url("x"))):
        assert client.request(method, path, headers=admin).status_code == 404, path
        assert client.request(method, path, headers=viewer).status_code == 403, path
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    for name in ("MENDARI_EVALSET_KEY", "MENDARI_EVALSET_INDEX_KEY"):
        monkeypatch.delenv(name)
    keyless = create_app(db_path=tmp_path / "keyless.db")
    assert labelling_routes(keyless) == set()
    with AuditStore(tmp_path / "keyless.db") as store:
        labeller = member(store, org=CLINIC, roles=["labeller"], sub="labeller-a")
    assert TestClient(keyless, base_url=HTTPS).get(NEXT, headers=labeller).status_code == 404


def test_the_answers_are_closed_and_documented(world: World) -> None:
    """Every answer model forbids a field it does not list, so nothing the store might add
    reaches a labeller; the reference lists the routes under their own tag, for a person's
    session only, with the `204` of nothing to label."""
    for model in (LabellingCase, LabelledSet, LabellingCounts, LabelRecorded):
        assert model.model_config.get("extra") == "forbid", model.__name__
    assert set(LabellingCase.model_fields) == CASE_KEYS
    assert set(LabelRecorded.model_fields) == {"remaining"}
    with pytest.raises(ValidationError, match="Extra inputs"):
        LabellingCase.model_validate({"assignment": "a", "questions": {}, "input": {}, "x": 1})
    admin = member(world.store, org=CLINIC, roles=["admin"], sub="lead")
    schema = world.client.get("/openapi.json", headers=admin).json()
    for method, path in ROUTES:
        operation = schema["paths"][path][method.lower()]
        assert operation["tags"] == ["Labelling"], (method, path)
        assert operation["security"] == [{"session": []}], (method, path)
        assert "holding the labeller role" in operation["description"], (method, path)
    assert "204" in schema["paths"][NEXT]["get"]["responses"]
    assert "Labelling" in {tag["name"] for tag in schema["tags"]}
