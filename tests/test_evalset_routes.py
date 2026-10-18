# SPDX-License-Identifier: Apache-2.0
"""An organization's curators' routes over its hidden evaluation sets (LP-11k, `server/app.py`
`add_evalset_routes`, ADR-0011): `GET /api/evalsets` (its sets), `GET /api/evalsets/{set_id}`
(one set: its state, labelling and stored aggregates), and `POST /api/evalsets/{set_id}/sample`,
`.../assignments` and `.../adjudication`.

- Answers are closed models, pinned here field by field down to the stored aggregates' last
  field: ids, counts, user ids, the commitment and the stored aggregates (each rater pair's
  kappa without the pair's label counts), never an input, a label given, an attribute or a case
  id, refusals included; a label word only in one set's answer, to its organization's curators
  (conflict 57 as amended in LP-11k).
- Only a person holding `curate` (an administrator of the organization they act for) reaches
  them: anonymous `401`; a member of every other role, the labeller included, `403`; every key
  (one of each machine role, a legacy key of each other role, the grading key) the
  `people_only` `403`; an EHR launch's cookie `401`; a person of no organization
  `NoOrganization`.
- A set's aggregates are shown only once stored, by the key holder's `mendari evalset
  agreement` (or `coverage`) or at freeze: no route computes statistics, freezes or retires,
  and a set no longer a draft is drawn and assigned no more.
- A curator is never assigned, themselves included (separation of duties).
- Two organizations stay isolated: another's set, an unknown one and a malformed id are one
  `404` naming no id, and write nothing; one's labellers are no labellers of the other's.
- Each draw and assignment is chained (`evalset.sampled`, `evalset.assigned`) and logged with
  the set's id only; what each route opens of the sealed set is what threat model row I13 says.
- Mounted only in memberships mode with both evaluation-set keys.
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
from pydantic import BaseModel, ValidationError

from mendari import AuditStore, Pack
from mendari.cli import main
from mendari.core.evalset_keys import EvalsetKeys
from mendari.core.evalsets import NO_SET, EvalsetStore
from mendari.evals import coverage as coverage_module
from mendari.evals import rater_agreement
from mendari.server.app import (
    AgreementReport,
    AgreementSeeds,
    CaseStatuses,
    Coefficient,
    CoverageMix,
    CoverageReport,
    CriticalCount,
    DistinctCount,
    EvalsetDetail,
    EvalsetListed,
    Interval,
    LabelCount,
    LabellerCounts,
    NamedDataset,
    PairKappa,
    PoolAlpha,
    QuestionAgreement,
    QuestionCoverage,
    RoundCounts,
    RunsCount,
    SetAssigned,
    SetSampled,
    Share,
    StoredAggregates,
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
from tests.test_evalset_labelling import LABEL_WORD, ROUTINE, cases, dump, events
from tests.test_evalset_store import CLINIC, MARKS
from tests.test_labelling_routes import SEALED_WORDS
from tests.test_security_log import logged
from tests.test_threat_model import rows

HTTPS = "https://mendari.test"
OTHER = "clinic-b"
SETS = "/api/evalsets"
SET = "/api/evalsets/{set_id}"
SAMPLE = "/api/evalsets/{set_id}/sample"
ASSIGN = "/api/evalsets/{set_id}/assignments"
ADJUDICATE = "/api/evalsets/{set_id}/adjudication"
ROUTES = {("GET", SETS), ("GET", SET), ("POST", SAMPLE), ("POST", ASSIGN), ("POST", ADJUDICATE)}
PHYSICIAN = {"destination": "physician", "urgency": "routine"}
STRATA = ("zq-stratum-one", "zq-stratum-two")
# Every closed answer model, with the fields it holds: a field added fails here until it is
# listed, and reviewed for what it would show (no input, label given, attribute or case id).
PINNED: dict[type[BaseModel], set[str]] = {
    EvalsetListed: {"set", "name", "pack", "state", "created_at", "cases"},
    EvalsetDetail: {
        "set",
        "name",
        "pack",
        "questions",
        "state",
        "created_at",
        "cases",
        "statuses",
        "labellers",
        "frozen_at",
        "commitment",
        "max_runs",
        "max_age_days",
        "runs",
        "graded_until",
        "retired_at",
        "aggregates",
    },
    CaseStatuses: {"draft", "sampled", "agreed", "disputed", "resolved", "unresolved"},
    LabellerCounts: {"first", "adjudication"},
    RoundCounts: {"assigned", "labelled", "open", "withdrawn"},
    RunsCount: {"used", "left"},
    StoredAggregates: {"format", "set", "measured_at", "n", "coverage", "agreement"},
    CoverageReport: {
        "format",
        "pack",
        "dataset",
        "norm",
        "cases",
        "distinct",
        "duplicate_groups",
        "conflicting_groups",
        "questions",
        "critical",
        "mix",
        "warnings",
    },
    NamedDataset: {"name"},
    DistinctCount: {"exact", "normalised"},
    QuestionCoverage: {"labelled", "labels"},
    LabelCount: {"cases", "distinct"},
    CriticalCount: {"question", "label", "cases", "distinct", "insufficient"},
    CoverageMix: {"language", "source", "stratum"},
    AgreementReport: {"format", "pack", "level", "seeds", "questions"},
    AgreementSeeds: {"bootstrap", "resamples"},
    QuestionAgreement: {"cases", "percent", "kappa", "alpha"},
    Share: {"k", "n", "low", "high"},
    Coefficient: {"estimate", "status", "reason", "interval"},
    PairKappa: {"estimate", "status", "reason", "interval", "raters", "n"},
    PoolAlpha: {"estimate", "status", "reason", "interval", "cases", "ratings"},
    Interval: {"status", "low", "high", "reason", "dropped"},
    SetSampled: {"set", "drawn", "sampled", "seed"},
    SetAssigned: {"set", "round", "assigned", "withdrawn"},
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

    def curator(self, org: str = CLINIC) -> dict[str, str]:
        """`org`'s administrator, signed in: who holds `curate` there."""
        return self.person(f"curator-{org}", "admin", org=org)[1]

    def made(self, count: int = 2, *, org: str = CLINIC) -> str:
        """A draft set of `org`'s with `count` hand-written cases over two strata, as the key
        holder makes one: its id."""
        set_id = str(self.hidden.create_set(org, self.pack, MARKS["name"], created_by="ops")["set"])
        self.hidden.add_cases(set_id, cases(count, strata=STRATA))
        return set_id

    def labelled(self, user: str, labels: dict[str, str], *, org: str = CLINIC) -> int:
        """Every case waiting for `user`, labelled `labels` at the store: how many."""
        done = 0
        while (shown := self.hidden.next_case(user, org=org)) is not None:
            self.hidden.record_label(shown["assignment"], user, labels, org=org)
            done += 1
        return done

    def call(self, method: str, path: str, headers: dict[str, str], **sent: Any) -> Any:
        return self.client.request(method, path, headers=headers, **sent)


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


def url(path: str, set_id: str) -> str:
    return path.replace("{set_id}", set_id)


def ids_of(world: World, table: str, set_id: str) -> list[str]:
    with world.store._db.session(exclusive=False) as db:
        found = db.all(f"SELECT id FROM {table} WHERE set_id = ? ORDER BY id", (set_id,))
    return [str(row["id"]) for row in found]


def case_hashes(world: World, set_id: str) -> list[str]:
    with world.store._db.session(exclusive=False) as db:
        found = db.all("SELECT case_hash FROM evalset_cases WHERE set_id = ?", (set_id,))
    return [str(row["case_hash"]) for row in found]


# -- what a curator does, and what they are told ---------------------------------------------------


def test_a_curator_lists_reads_draws_assigns_and_adjudicates_their_sets(world: World) -> None:
    """The whole round through the routes: the set listed, read before anything is drawn,
    drawn (per stratum), assigned to two labellers who then disagree on one case, adjudicated
    by a third, and read again; each answer as its model pins it, each write chained, and the
    set verifies."""
    (first, _), (second, _), (third, _) = (world.person(f"labeller-{n}") for n in range(3))
    curator = world.curator()
    set_id = world.made(4)
    listed = world.call("GET", SETS, curator)
    assert listed.status_code == 200
    (item,) = listed.json()
    assert item == {
        "set": set_id,
        "name": MARKS["name"],
        "pack": world.pack.id,
        "state": "draft",
        "created_at": item["created_at"],
        "cases": 4,
    }
    shown = world.call("GET", url(SET, set_id), curator).json()
    manifest = world.pack.manifest
    asked = {q: manifest.labels(q) for q in manifest.questions if q not in manifest.derived}
    assert (shown["name"], shown["pack"], shown["questions"]) == (
        MARKS["name"],
        world.pack.id,
        asked,
    )
    assert (shown["state"], shown["cases"], shown["labellers"], shown["aggregates"]) == (
        "draft",
        4,
        {},
        None,
    )
    assert shown["statuses"] == dict.fromkeys(PINNED[CaseStatuses], 0) | {"draft": 4}
    assert (shown["commitment"], shown["runs"], shown["frozen_at"]) == (
        None,
        {"used": 0, "left": None},
        None,
    )
    drawn = world.call("POST", url(SAMPLE, set_id), curator, json={"per_stratum": 1, "seed": 7})
    assert (drawn.status_code, drawn.json()) == (
        200,
        {"set": set_id, "drawn": 2, "sampled": 2, "seed": 7},
    )
    sent = {"labellers": [first, second]}
    assigned = world.call("POST", url(ASSIGN, set_id), curator, json=sent)
    assert (assigned.status_code, assigned.json()) == (
        200,
        {"set": set_id, "round": "first", "assigned": 4, "withdrawn": 0},
    )
    assert world.labelled(first, ROUTINE) == 2
    shown_case = world.hidden.next_case(second, org=CLINIC)
    assert shown_case is not None
    world.hidden.record_label(shown_case["assignment"], second, PHYSICIAN, org=CLINIC)
    assert world.labelled(second, ROUTINE) == 1  # one disputed, one agreed
    third_round = world.call("POST", url(ADJUDICATE, set_id), curator, json={"labellers": [third]})
    assert (third_round.status_code, third_round.json()) == (
        200,
        {"set": set_id, "round": "adjudication", "assigned": 1, "withdrawn": 0},
    )
    assert world.labelled(third, ROUTINE) == 1
    shown = world.call("GET", url(SET, set_id), curator).json()
    assert {k: v for k, v in shown["statuses"].items() if v} == {
        "draft": 2,
        "agreed": 1,
        "resolved": 1,
    }
    counted = {"assigned": 2, "labelled": 2, "open": 0, "withdrawn": 0}
    nothing = dict.fromkeys(counted, 0)
    assert shown["labellers"] == {
        first: {"first": counted, "adjudication": nothing},
        second: {"first": counted, "adjudication": nothing},
        third: {"first": nothing, "adjudication": {**nothing, "assigned": 1, "labelled": 1}},
    }
    assert [e["payload"]["n"] for e in events(world.store, "evalset.sampled")] == [2]
    rounds = [e["payload"]["round"] for e in events(world.store, "evalset.assigned")]
    assert rounds == ["first"] * 4 + ["adjudication"]
    assert world.store.verify(evalsets=world.keys)["ok"]


def test_aggregates_appear_only_after_evalset_agreement_or_freeze(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A set's answer carries its aggregates only once the key holder stored them: none while it
    is drawn and labelled, then those `mendari evalset agreement` measured (with when), then
    those freezing stored, with the commitment and the runs left. No route computes statistics,
    freezes or retires: every route call here (each read of the set, its draw, its assignment
    and adjudication, and each write refused on the frozen set) runs with each such store call
    and both statistics failing the test, and chains no `evalset.measured`, `evalset.frozen` or
    `evalset.retired`; only the key holder's command and store, outside, do."""
    (first, _), (second, _), (third, _) = (world.person(f"labeller-{n}") for n in "abc")
    curator = world.curator()
    set_id = world.made(2)

    made: list[str] = []  # each call `never` saw, should a route swallow its error

    def never(*args: Any, **kwargs: Any) -> Any:
        made.append("a route computed, froze or retired")
        raise AssertionError(made[-1])

    def routed(method: str, path: str, **sent: Any) -> Any:
        """The curator's call, with every store call and statistic that measures, freezes,
        retires or grades failing the test; it chains none of their events."""
        with monkeypatch.context() as routes_only:
            for name in ("measure", "freeze", "retire", "grade_read"):
                routes_only.setattr(EvalsetStore, name, never)
            routes_only.setattr(rater_agreement, "agreement", never)
            routes_only.setattr(coverage_module, "coverage", never)
            before = len(events_of(world))
            answer = world.call(method, url(path, set_id), curator, **sent)
            chained = {e["event"] for e in events_of(world)[before:]}
        assert made == [], path
        assert not chained & {"evalset.measured", "evalset.frozen", "evalset.retired"}, path
        return answer

    def answered() -> dict[str, Any]:
        before = [e["event"] for e in events_of(world)]
        shown = routed("GET", SET)
        assert [e["event"] for e in events_of(world)] == before  # a read chains nothing
        assert shown.status_code == 200, shown.text
        return dict(shown.json())

    assert answered()["aggregates"] is None
    for path, sent in (
        (SAMPLE, {"cases": 2}),
        (ASSIGN, {"labellers": [first, second]}),
    ):
        written = routed("POST", path, json=sent)
        assert written.status_code == 200, (path, written.text)
    for user in (first, second):
        world.labelled(user, ROUTINE)
    adjudicated = routed("POST", ADJUDICATE, json={"labellers": [third]})  # none disputed
    assert (adjudicated.status_code, adjudicated.json()["assigned"]) == (200, 0)
    assert answered()["aggregates"] is None  # labelled, never measured
    argv = ["evalset", "agreement", set_id, "--db", world.db, "--resamples", "50", "--json"]
    assert main(argv) == 0
    capsys.readouterr()
    measured = answered()["aggregates"]
    assert measured is not None
    assert set(measured) == PINNED[StoredAggregates]
    assert (measured["set"], measured["n"]) == (set_id, 2)
    assert measured["agreement"]["seeds"] == {"bootstrap": 0, "resamples": 50}
    urgency = measured["agreement"]["questions"]["urgency"]
    assert urgency["percent"]["k"] == urgency["percent"]["n"] == 2
    ((pair),) = urgency["kappa"]
    assert sorted(pair["raters"]) == sorted((first, second))
    assert pair["status"] == "not_estimable" and "interval" not in pair  # left out, not null
    assert measured["coverage"]["mix"]["stratum"] == {STRATA[0]: 1, STRATA[1]: 1}
    assert measured["coverage"]["dataset"] == {"name": set_id}
    world.hidden.freeze(set_id, max_runs=3, max_age_days=30, resamples=50)
    frozen = answered()
    assert frozen["state"] == "frozen" and frozen["commitment"] is not None
    assert (frozen["max_runs"], frozen["max_age_days"], frozen["runs"]) == (
        3,
        30,
        {"used": 0, "left": 3},
    )
    assert frozen["aggregates"]["measured_at"] >= measured["measured_at"]
    assert frozen["aggregates"]["agreement"]["seeds"]["resamples"] == 50
    done = {e["event"] for e in events_of(world)}
    assert {"evalset.measured", "evalset.frozen"} <= done and "evalset.retired" not in done
    for path in (SAMPLE, ASSIGN, ADJUDICATE):  # a frozen set is drawn and assigned no more
        before = dump(world.store)
        sent = {"cases": 1} if path == SAMPLE else {"labellers": [first, second]}
        refused = routed("POST", path, json=sent)
        assert refused.status_code == 400 and "only a draft changes" in refused.json()["detail"]
        assert dump(world.store) == before


def test_a_curator_never_reads_a_labellers_labels_in_the_aggregates(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """Three labellers, each pair sharing one case, each labeller giving labels of their own:
    each pair's kappa in the stored agreement counts each rater's labels over the cases they
    share, which here are what each of them answered on that one case. The set's answer to its
    curator leaves those counts out (no pair names a labeller beside a label word), while the
    key holder's store keeps them (`evalset show`, `evalset agreement`)."""
    given = (
        {"destination": "nurse", "urgency": "routine"},
        {"destination": "physician", "urgency": "urgent"},
        {"destination": "billing", "urgency": "routine"},
    )
    labellers = [world.person(f"labeller-{n}")[0] for n in "abc"]
    curator = world.curator()
    set_id = world.made(3)
    world.call("POST", url(SAMPLE, set_id), curator, json={"cases": 3})
    assigned = world.call("POST", url(ASSIGN, set_id), curator, json={"labellers": labellers})
    assert assigned.json()["assigned"] == 6
    for user, labels in zip(labellers, given, strict=True):
        assert world.labelled(user, labels) == 2
    argv = ["evalset", "agreement", set_id, "--db", world.db, "--resamples", "20", "--json"]
    assert main(argv) == 0
    capsys.readouterr()
    shown = world.call("GET", url(SET, set_id), curator)
    assert shown.status_code == 200, shown.text
    questions = shown.json()["aggregates"]["agreement"]["questions"]
    pairs = [pair for question in questions.values() for pair in question["kappa"]]
    assert len(pairs) == 6 and all(pair["n"] == 1 for pair in pairs)
    for pair in pairs:
        assert "labels" not in pair, pair
        assert LABEL_WORD.search(json.dumps(pair)) is None, pair
    stored = world.hidden.show(set_id)["aggregates"]["agreement"]["questions"]
    kept = stored["destination"]["kappa"]
    assert {user: dict(counts) for pair in kept for user, counts in pair["labels"].items()} == {
        user: {labels["destination"]: 1} for user, labels in zip(labellers, given, strict=True)
    }


def events_of(world: World) -> list[dict[str, Any]]:
    with world.store._db.session(exclusive=False) as db:
        return db.all("SELECT event FROM audit_events ORDER BY seq")


def test_no_answer_holds_an_input_a_label_given_an_attribute_or_a_case_id(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every answer of every route over a set drawn, labelled, measured and frozen, and a
    refusal that concerns a case (an adjudication with no labeller left who qualifies): no
    input, author, author's id, custom attribute, case id, case hash, assignment, label id or
    salt; the set's name to its curators (conflict 38), its strata and languages in its
    aggregates (conflict 55), and a label word only in the one set's answer (its questions and
    aggregates: conflict 57 as amended in LP-11k), never in a list, a draw or an assignment's
    answer."""
    (first, _), (second, _) = world.person("labeller-a"), world.person("labeller-b")
    curator = world.curator()
    set_id = world.made(2)
    answers: dict[str, list[str]] = {}

    def called(method: str, path: str, status: int = 200, **sent: Any) -> None:
        answer = world.call(method, url(path, set_id), curator, **sent)
        assert answer.status_code == status, (path, answer.text)
        answers.setdefault(path, []).append(answer.text + json.dumps(dict(answer.headers)))

    called("GET", SETS)
    called("POST", SAMPLE, json={"per_stratum": 1})
    called("POST", ASSIGN, json={"labellers": [first, second]})
    world.labelled(first, ROUTINE)
    world.labelled(second, PHYSICIAN)  # both cases disputed
    before = dump(world.store)
    called("POST", ADJUDICATE, 400, json={"labellers": [first]})  # one of each case's first two
    assert dump(world.store) == before
    called("POST", ADJUDICATE, json={"labellers": [world.person("labeller-c")[0]]})
    called("GET", SET)
    assert main(["evalset", "coverage", set_id, "--db", world.db]) == 0
    capsys.readouterr()
    called("GET", SET)
    private = [
        MARKS["input"],
        MARKS["author"],
        MARKS["author_id"],
        MARKS["site"],
        "intended",
        "salt",
        "weight",
        "hmac-sha256",
        *ids_of(world, "evalset_cases", set_id),
        *case_hashes(world, set_id),
        *ids_of(world, "evalset_assignments", set_id),
        *ids_of(world, "evalset_labels", set_id),
    ]
    for path, texts in answers.items():
        for text in texts:
            assert [mark for mark in private if mark in text] == [], path
            if path != SET:
                assert LABEL_WORD.search(text) is None, path
    detail = answers[SET][-1]
    assert MARKS["name"] in detail and MARKS["language"] in detail
    assert all(stratum in detail for stratum in STRATA)
    assert LABEL_WORD.search(detail) is not None


# -- who reaches them ------------------------------------------------------------------------------


def test_every_role_but_admin_is_refused_the_labeller_included(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """Anonymous: `401`. A member of each role but the administrator's, and a labeller: `403`.
    Every key, whatever its role (each machine role's, a legacy key of each other role, the
    administrator's and the labeller's among them, and the grading key): the `people_only`
    `403`. An EHR launch's cookie is no credential here: `401`. A person of no organization:
    `NoOrganization`. Nothing is written or logged as a hidden set's; the administrator is
    answered."""
    (labeller, labelling) = world.person("labeller-a")
    set_id = world.made(2)
    secrets = {
        role: world.store.create_api_key(name=role, role=role, org=CLINIC, created_by="t")["key"]
        for role in ("viewer", "service")
    }
    for role in sorted({*ROLES, "labeller"} - set(secrets)):
        secrets[f"legacy {role}"] = legacy_key(world.db, role, CLINIC)
    grading = world.store.create_grading_key(org=CLINIC, created_by="t", reason="the matrix")
    secrets["grading"] = grading["key"]
    people = {role: world.person(f"member-{role}", role)[1] for role in ROLES if role != "admin"}
    people["labeller"] = labelling
    launch = launched(world.store)
    nobody = member(world.store, org=None, sub="nobody")
    sent = {
        SAMPLE: {"cases": 1},
        ASSIGN: {"labellers": [labeller, labeller]},
        ADJUDICATE: {"labellers": [labeller]},
    }
    before = dump(world.store)
    capsys.readouterr()

    def answer(method: str, path: str, headers: dict[str, str]) -> Any:
        body = {"json": sent[path]} if method == "POST" else {}
        return world.call(method, url(path, set_id), headers, **body)

    for method, path in sorted(ROUTES):
        assert answer(method, path, {}).status_code == 401, path
        assert answer(method, path, launch).status_code == 401, path
        for role, headers in people.items():
            refused = answer(method, path, headers)
            assert (refused.status_code, refused.json()["detail"]) == (
                403,
                "your role may not curate here",
            ), (path, role)
        for name, secret in secrets.items():
            refused = answer(method, path, {"authorization": f"Bearer {secret}"})
            assert (refused.status_code, refused.json()["detail"]) == (
                403,
                "only a person who signed in may do this",
            ), (path, name)
        refused = answer(method, path, nobody)
        assert (refused.status_code, refused.json()["error"]) == (403, "NoOrganization"), path
    assert dump(world.store) == before
    lines = logged(capsys)
    refusals = {line["reason"] for line in lines if line["event"] == "auth.permission.refused"}
    assert refusals == {"curate", "people_only", "no_organization"}
    assert not [line for line in lines if line["event"].startswith("evalset.")]
    assert world.call("GET", SETS, world.curator()).status_code == 200


def test_a_curator_cannot_assign_themselves_or_any_administrator(world: World) -> None:
    """Separation of duties: a curator naming themselves, another administrator, or an
    administrator who also holds the labeller role, to label or to adjudicate, is refused
    (`400`, naming whom by user id) and nothing is written or chained; their labellers alone
    are assigned."""
    (first, _), (second, _) = world.person("labeller-a"), world.person("labeller-b")
    curator = world.curator()
    me = person_id(IDP, f"curator-{CLINIC}")
    other, _ = world.person("admin-b", "admin")
    both, _ = world.person("admin-labeller", "admin", "labeller")
    set_id = world.made(2)
    world.call("POST", url(SAMPLE, set_id), curator, json={"cases": 2})
    for path, named in (
        (ASSIGN, [me, first]),
        (ASSIGN, [first, other]),
        (ASSIGN, [both, second]),
        (ADJUDICATE, [me]),
    ):
        before = dump(world.store)
        refused = world.call("POST", url(path, set_id), curator, json={"labellers": named})
        assert refused.status_code == 400, (path, named)
        assert "who curates a set is never assigned its cases" in refused.json()["detail"]
        assert dump(world.store) == before
    told = world.call("POST", url(ASSIGN, set_id), curator, json={"labellers": [first, second]})
    assert told.json()["assigned"] == 4
    assert {e["payload"]["labeller"] for e in events(world.store, "evalset.assigned")} == {
        first,
        second,
    }


# -- two organizations -----------------------------------------------------------------------------


def test_another_organizations_set_answers_as_an_unknown_one_does(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """Another organization's administrator, on this one's set (draft or frozen): every route's
    `404` is the one an unknown id, a malformed one and one not as the store minted it get,
    naming no id; nothing is written or chained; the security log says `no_set` with no id. Its
    list holds its own sets only, and this organization's labellers are none of its own."""
    (first, _), (second, _) = world.person("labeller-a"), world.person("labeller-b")
    (stranger, _) = world.person("labeller-z", org=OTHER)
    ours, frozen = world.made(2), world.made(1)
    world.hidden.sample(ours, cases=2)
    world.hidden.sample(frozen, cases=1)
    world.hidden.assign(frozen, [first, second])
    for user in (first, second):
        world.labelled(user, ROUTINE)
    world.hidden.freeze(frozen, max_runs=1, max_age_days=1, resamples=1)
    theirs = world.made(1, org=OTHER)
    outsider, curator = world.curator(OTHER), world.curator()
    sent = {SAMPLE: {"cases": 1}, ASSIGN: {"labellers": [first, second]}}
    sent[ADJUDICATE] = {"labellers": [first]}
    capsys.readouterr()

    def refused(method: str, path: str, set_id: str, headers: dict[str, str]) -> dict[str, Any]:
        before = dump(world.store)
        body = {"json": sent[path]} if method == "POST" else {}
        answer = world.call(method, url(path, set_id), headers, **body)
        assert answer.status_code == 404, (path, set_id, answer.text)
        assert dump(world.store) == before, (path, set_id)
        assert set_id not in answer.text
        return without_request_id(answer.json(), answer)

    answers = []
    for method, path in sorted(ROUTES - {("GET", SETS)}):
        for set_id in (ours, frozen, str(uuid.uuid4()), "not-an-id", ours.upper()):
            answers.append(refused(method, path, set_id, outsider))
    assert answers == [{"error": "NoEvalset", "detail": NO_SET}] * len(answers)
    lines = logged(capsys)
    said = [(line["event"], line["outcome"], line.get("reason"), line.get("ids")) for line in lines]
    writes = [entry for entry in said if entry[0].startswith("evalset.")]
    assert writes and set(writes) == {
        ("evalset.sampled", "refused", "no_set", None),
        ("evalset.assigned", "refused", "no_set", None),
        ("evalset.adjudication.assigned", "refused", "no_set", None),
    }
    assert [item["set"] for item in world.call("GET", SETS, outsider).json()] == [theirs]
    assert [item["set"] for item in world.call("GET", SETS, curator).json()] == [ours, frozen]
    told = []
    for named in (stranger, f"user:{'0' * 16}"):  # their labeller, and no one
        before = dump(world.store)
        answer = world.call("POST", url(ASSIGN, ours), curator, json={"labellers": [named, first]})
        assert answer.status_code == 400 and dump(world.store) == before
        told.append(answer.json()["detail"].replace(named, "<user>"))
    assert told == [f"<user> holds no labeller role on a membership in force in {CLINIC}"] * 2
    assert world.call("GET", url(SET, theirs), curator).status_code == 404
    assert world.call("GET", url(SET, frozen), curator).json()["state"] == "frozen"


def test_the_two_organizations_answers_stay_their_own_when_the_other_works(
    world: World,
) -> None:
    """One organization's list and set answer the same before and after the other draws,
    assigns and adjudicates its own set through the routes."""
    (first, _), (second, _) = world.person("labeller-a"), world.person("labeller-b")
    (x_first, _), (x_second, _) = (
        world.person("labeller-x", org=OTHER),
        world.person("labeller-y", org=OTHER),
    )
    ours, theirs = world.made(2), world.made(2, org=OTHER)
    curator, outsider = world.curator(), world.curator(OTHER)
    world.call("POST", url(SAMPLE, ours), curator, json={"cases": 2})
    world.call("POST", url(ASSIGN, ours), curator, json={"labellers": [first, second]})

    def ours_now() -> list[Any]:
        return [
            world.call("GET", SETS, curator).json(),
            world.call("GET", url(SET, ours), curator).json(),
        ]

    before = ours_now()
    world.call("POST", url(SAMPLE, theirs), outsider, json={"cases": 2})
    assigned = world.call(
        "POST", url(ASSIGN, theirs), outsider, json={"labellers": [x_first, x_second]}
    )
    assert assigned.json()["assigned"] == 4
    world.labelled(x_first, ROUTINE, org=OTHER)
    world.labelled(x_second, PHYSICIAN, org=OTHER)
    (x_third, _) = world.person("labeller-w", org=OTHER)
    third = world.call("POST", url(ADJUDICATE, theirs), outsider, json={"labellers": [x_third]})
    assert third.json()["assigned"] == 2
    assert ours_now() == before
    shown = world.call("GET", url(SET, theirs), outsider).json()
    assert set(shown["labellers"]) == {x_first, x_second, x_third}


# -- chained, logged and opened --------------------------------------------------------------------


def test_each_draw_and_assignment_is_chained_and_logged_by_set_id_only(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """A draw chains `evalset.sampled` (how many, the seed, each drawn case's weight's
    commitment by its keyed hash) and each assignment
    `evalset.assigned`; the security log says `evalset.sampled`, `evalset.assigned` and
    `evalset.adjudication.assigned` with the set's id and how many, the reads say nothing but
    their access lines (the routes' templates), and no line holds a case, an assignment, a
    label word or a planted mark. The operator's guide lists each event (§7)."""
    (first, _), (second, _), (third, _) = (world.person(f"labeller-{n}") for n in range(3))
    curator = world.curator()
    set_id = world.made(2)
    capsys.readouterr()
    world.call("GET", SETS, curator)
    world.call("POST", url(SAMPLE, set_id), curator, json={"cases": 2, "seed": 3})
    world.call("POST", url(ASSIGN, set_id), curator, json={"labellers": [first, second]})
    lines = logged(capsys)
    world.labelled(first, ROUTINE)
    world.labelled(second, PHYSICIAN)
    capsys.readouterr()
    world.call("POST", url(ADJUDICATE, set_id), curator, json={"labellers": [third]})
    world.call("GET", url(SET, set_id), curator)
    lines += logged(capsys)
    said = [(line["event"], line.get("ids"), line.get("count"), line["outcome"]) for line in lines]
    assert [entry for entry in said if entry[0] != "request"] == [
        ("evalset.sampled", [set_id], 2, "ok"),
        ("evalset.assigned", [set_id], 4, "ok"),
        ("evalset.adjudication.assigned", [set_id], 2, "ok"),
    ]
    assert {line["route"] for line in lines if line["event"] == "request"} == {
        SETS,
        SET,
        SAMPLE,
        ASSIGN,
        ADJUDICATE,
    }
    assert {line["actor"] for line in lines} == {person_id(IDP, f"curator-{CLINIC}")}
    written = json.dumps(lines)
    for private in (
        *ids_of(world, "evalset_cases", set_id),
        *ids_of(world, "evalset_assignments", set_id),
        *case_hashes(world, set_id),
        *MARKS.values(),
        *STRATA,
    ):
        assert private not in written, private
    assert LABEL_WORD.search(written) is None
    (drawn,) = events(world.store, "evalset.sampled")
    weights = drawn["payload"].pop("weight_refs")  # each drawn case's, committed to (LP-11p)
    assert drawn["payload"] == {"set": set_id, "org": CLINIC, "n": 2, "seed": 3}
    assert sorted(weights) == sorted(case_hashes(world, set_id))
    assert len(events(world.store, "evalset.assigned")) == 6
    guide = (Path(__file__).resolve().parents[1] / "docs" / "deploying-with-phi.md").read_text()
    assert [event for event, *_ in said if event != "request" and f"`{event}`" not in guide] == []


def test_what_each_route_opens_is_what_the_threat_model_says(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every sealed value each route opens in the server (`EvalsetKeys.open`), as threat model
    row I13 says of it: the list, each set's header; a set, its header and its stored
    aggregates; a draw, the attributes of the set's cases (to draw within each stratum, and to
    seal each drawn case's weight); assigning and adjudicating, nothing."""
    (first, _), (second, _), (third, _) = (world.person(f"labeller-{n}") for n in range(3))
    curator = world.curator()
    set_id = world.made(2)
    opened: list[str] = []
    real = EvalsetKeys.open

    def spied(
        keys: EvalsetKeys, kind: Any, token: str, set_id: str, case_id: str = "", label_id: str = ""
    ) -> tuple[Any, str]:
        opened.append(kind)
        return real(keys, kind, token, set_id, case_id, label_id)

    def opens(method: str, path: str, **sent: Any) -> set[str]:
        opened.clear()
        with monkeypatch.context() as spying:
            spying.setattr(EvalsetKeys, "open", spied)
            answer = world.call(method, url(path, set_id), curator, **sent)
        assert answer.is_success, (path, answer.text)
        return set(opened)

    seen = {("GET", SETS): opens("GET", SETS)}
    seen[("POST", SAMPLE)] = opens("POST", SAMPLE, json={"cases": 2})
    seen[("POST", ASSIGN)] = opens("POST", ASSIGN, json={"labellers": [first, second]})
    world.labelled(first, ROUTINE)
    world.labelled(second, PHYSICIAN)
    seen[("POST", ADJUDICATE)] = opens("POST", ADJUDICATE, json={"labellers": [third]})
    world.labelled(third, ROUTINE)
    assert main(["evalset", "agreement", set_id, "--db", world.db, "--resamples", "20"]) == 0
    capsys.readouterr()
    seen[("GET", SET)] = opens("GET", SET)
    assert seen == {
        ("GET", SETS): {"header"},
        ("GET", SET): {"header", "aggregates"},
        ("POST", SAMPLE): {"attributes"},
        ("POST", ASSIGN): set(),
        ("POST", ADJUDICATE): set(),
    }
    (row,) = [line for line in rows() if line.startswith("| I13 |")]
    for (method, path), kinds in seen.items():
        said = re.search(rf"`{method} {re.escape(path)}` opens ([^;.]*)", row)
        assert said is not None, (method, path)
        named = {kind for word, kind in SEALED_WORDS.items() if re.search(rf"\b{word}\b", said[1])}
        assert named == kinds, (method, path, said[1])


# -- what is refused, and where they are mounted ---------------------------------------------------


def test_a_request_the_store_refuses_names_no_value(world: World) -> None:
    """A draw of neither or both counts, or of none; a labeller who is not a user id; a seed
    that is not a whole number: refused before anything is written, never repeating what was
    sent."""
    curator = world.curator()
    set_id = world.made(2)
    value = "zqvaluemark"
    before = dump(world.store)
    for path, sent, status in (
        (SAMPLE, {}, 400),
        (SAMPLE, {"cases": 1, "per_stratum": 1}, 400),
        (SAMPLE, {"cases": 0}, 422),
        (SAMPLE, {"cases": 1, "seed": -1}, 422),
        (SAMPLE, {"cases": 1, "seed": value}, 422),
        (ASSIGN, {"labellers": [value, value + "2"]}, 400),
        (ASSIGN, {"labellers": []}, 422),
        (ASSIGN, {}, 422),
        (ADJUDICATE, {"labellers": [value]}, 400),
    ):
        answer = world.call("POST", url(path, set_id), curator, json=sent)
        assert answer.status_code == status, (path, sent, answer.text)
        assert value not in answer.text, (path, sent)
    assert dump(world.store) == before


def evalset_routes(app: Any) -> set[tuple[str, str]]:
    """The curators' routes mounted: those under theirs, but the grading route (LP-11l), which
    is a grading key's and mounted with the keys in both account modes."""
    return {
        (m, p)
        for m, p in mounted_routes(app)
        if p.startswith(SETS) and permission_for(m, p) != "grade"
    }


def test_the_routes_are_absent_in_claims_mode_and_without_the_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With memberships mode and both keys: the five routes, each needing `curate`. Claims mode
    with the keys (the hosted site has neither: owner decision 1): none mounted, and the paths
    read as any route no rule lists, `admin` (an administrator's `404`, anyone else's `403`).
    Memberships mode without the keys: none mounted, an administrator's `404`."""
    memberships(monkeypatch)
    assert evalset_routes(create_app(db_path=tmp_path / "both.db")) == ROUTES
    assert {permission_for(m, p) for m, p in ROUTES} == {"curate"}
    monkeypatch.setenv("MENDARI_ACCOUNTS", "claims")
    claims = create_app(db_path=tmp_path / "claims.db")
    assert evalset_routes(claims) == set()
    with AuditStore(tmp_path / "claims.db") as store:
        admin, viewer = signed_in(store, role="admin"), signed_in(store, role="viewer")
    client = TestClient(claims, base_url=HTTPS)
    for method, path in sorted(ROUTES):
        concrete = url(path, str(uuid.uuid4()))
        assert client.request(method, concrete, headers=admin).status_code == 404, path
        assert client.request(method, concrete, headers=viewer).status_code == 403, path
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    for name in ("MENDARI_EVALSET_KEY", "MENDARI_EVALSET_INDEX_KEY"):
        monkeypatch.delenv(name)
    keyless = create_app(db_path=tmp_path / "keyless.db")
    assert evalset_routes(keyless) == set()
    with AuditStore(tmp_path / "keyless.db") as store:
        curator = member(store, org=CLINIC, roles=["admin"], sub="curator")
    assert TestClient(keyless, base_url=HTTPS).get(SETS, headers=curator).status_code == 404


def test_the_answers_are_closed_pinned_and_documented(world: World) -> None:
    """Every answer model forbids a field it does not list, and lists exactly the fields pinned
    here, down to the stored aggregates' last; people are named by user id only; the reference
    lists the routes under their own tag, for a person's session only."""
    for model, fields in PINNED.items():
        assert model.model_config.get("extra") == "forbid", model.__name__
        assert set(model.model_fields) == fields, model.__name__
    with pytest.raises(ValidationError, match="Extra inputs"):
        SetAssigned.model_validate(
            {"set": "s", "round": "first", "assigned": 1, "withdrawn": 0, "assignments": []}
        )
    with pytest.raises(ValidationError, match="Extra inputs"):
        CoverageReport.model_validate({"duplicates": [["a case"]]})
    with pytest.raises(ValidationError, match="pattern"):  # a rater is a user id, nothing else
        PairKappa.model_validate(
            {"estimate": None, "status": "not_estimable", "raters": ["a@example.org"], "n": 1}
        )
    schema = world.client.get("/openapi.json", headers=world.curator()).json()
    for method, path in ROUTES:
        operation = schema["paths"][path][method.lower()]
        assert operation["tags"] == ["Evaluation sets"], (method, path)
        assert operation["security"] == [{"session": []}], (method, path)
        assert "administrator of the organization" in operation["description"], (method, path)
    assert "Evaluation sets" in {tag["name"] for tag in schema["tags"]}
