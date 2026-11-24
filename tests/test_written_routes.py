# SPDX-License-Identifier: Apache-2.0
"""A grader's routes over written output, and the rounds' reports (`server/app.py`
`add_written_routes`, ADR-0025): `GET /api/written/labelling` (their own counts, round by
round), `GET /api/written/labelling/next` (their next text, or `204`),
`POST /api/written/labelling/{assignment_id}` (their grades: `{remaining}`) and
`GET /api/written/rounds` (the rounds, and each closed one's report).

- Blind over HTTP: the second grader is shown the same before and after the first grades; the
  answer's keys are exactly `WrittenCase`'s; neither is told how their grade compares; a planted
  judge reaches no grader; and no report exists before the round closes.
- Someone else's assignment, another organization's, an unknown one and a closed one answer one
  `404` and write nothing; a `422` names the criterion, never a value.
- Only a person holding `label` reaches a grader's routes; the rounds are any reader's of the
  organization, and never another organization's.
- Mounted only in memberships mode under PHI mode `encrypt`.
- Each read is chained before it is answered; the security log names round ids only.
On SQLite and Postgres (`db_target`); texts are synthetic."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from mendari import AuditStore, Decision, load_pack
from mendari.core.pack import Pack
from mendari.core.written import WrittenStore
from mendari.evals import resolve_dataset, written
from mendari.evals.written import WrittenLedger, WrittenRun
from mendari.providers.judge import Judge
from mendari.server.app import (
    CriterionGuidance,
    LabelRecorded,
    WrittenCase,
    WrittenRound,
    WrittenText,
    create_app,
    mounted_routes,
)
from mendari.server.auth import ROLES, permission_for, person_id
from tests.conftest import ROOT, encrypt_mode, encrypted_store, launched, member, signed_in
from tests.test_auth import CLIENT, IDP, legacy_key
from tests.test_evalset_store import CLINIC, chained
from tests.test_security_log import logged

MINI = ROOT / "tests" / "fixtures" / "mini-agents" / "written-mini"
RUBRIC = "reply_quality"
HTTPS = "https://mendari.test"
OTHER = "clinic-b"
LABELLING = "/api/written/labelling"
NEXT = "/api/written/labelling/next"
GRADE = "/api/written/labelling/{assignment_id}"
ROUNDS = "/api/written/rounds"
GRADER_ROUTES = {("GET", LABELLING), ("GET", NEXT), ("POST", GRADE)}
ROUTES = {*GRADER_ROUTES, ("GET", ROUNDS)}
CASE_KEYS = {"assignment", "questions", "guidance", "written", "input"}
PASS = {"safe_to_send": "yes", "addresses_request": "fully", "plain_language": "yes"}
FAIL = {"safe_to_send": "no", "addresses_request": "partly", "plain_language": "yes"}
JUDGE_MARK = "PLANTED-JUDGE-MODEL-9117"


@pytest.fixture(scope="module")
def pack() -> Pack:
    return load_pack(MINI)


@pytest.fixture(scope="module")
def the_run(pack: Pack) -> WrittenRun:
    """A run of the fixture agent whose judge is named by a planted mark."""
    made = written.run(
        Decision(pack, origin_cap=False),
        resolve_dataset(pack, "written"),
        RUBRIC,
        judge=Judge(pack, RUBRIC, mode="canned"),
    )
    header = json.loads(json.dumps(made.ledger.header))
    header["judge"]["model"] = JUDGE_MARK
    header["models"] = [
        JUDGE_MARK if model == "mock-written-judge" else model for model in header["models"]
    ]
    return WrittenRun(WrittenLedger(header, made.ledger.cases), made.items)


@dataclass
class World:
    client: TestClient
    store: AuditStore
    rounds: WrittenStore
    pack: Pack
    run: WrittenRun
    db: str

    def person(self, sub: str, *roles: str, org: str = CLINIC) -> tuple[str, dict[str, str]]:
        headers = member(self.store, org=org, roles=list(roles or ("labeller",)), sub=sub)
        return person_id(IDP, sub), headers

    def round(
        self, users: list[str], sample: int = 4, *, org: str = CLINIC, double: float = 1.0
    ) -> str:
        """A round of `org`'s with `sample` items, each given to `users` (to both, unless said)."""
        made = self.rounds.create_round(
            org, self.pack, RUBRIC, self.run, sample=sample, created_by="ops"
        )
        self.rounds.assign(str(made["round"]), users, double=double)
        return str(made["round"])

    def next(self, headers: dict[str, str]) -> Any:
        return self.client.get(NEXT, headers=headers)

    def grade(self, assignment: str, grades: Any, headers: dict[str, str]) -> Any:
        return self.client.post(grade_url(assignment), json={"labels": grades}, headers=headers)


def grade_url(assignment: str) -> str:
    return f"/api/written/labelling/{assignment}"


def memberships(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment in memberships mode with sign-in and PHI mode `encrypt`."""
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", CLIENT)
    encrypt_mode(monkeypatch)


@contextmanager
def started(db: str, pack: Pack, run: WrittenRun) -> Iterator[World]:
    with (
        encrypted_store(db) as store,
        TestClient(create_app(db_path=db), base_url=HTTPS) as client,
    ):
        yield World(client, store, WrittenStore(store), pack, run, db)


@pytest.fixture
def world(
    db_target: str, pack: Pack, the_run: WrittenRun, monkeypatch: pytest.MonkeyPatch
) -> Iterator[World]:
    memberships(monkeypatch)
    with started(db_target, pack, the_run) as world:
        yield world


def events(store: AuditStore, kind: str) -> list[dict[str, Any]]:
    return [event for event in chained(store) if event["event"] == kind]


def written_routes(app: Any) -> set[tuple[str, str]]:
    return {(m, p) for m, p in mounted_routes(app) if p.startswith("/api/written")}


# -- blind --------------------------------------------------------------------------------------


def test_the_second_graders_view_is_the_same_before_and_after_the_first_grades(
    world: World,
) -> None:
    (first, a), (second, b) = world.person("grader-a"), world.person("grader-b")
    round_id = world.round([first, second], sample=1)
    before, shown = world.next(b), world.next(a)
    assert before.status_code == shown.status_code == 200
    assert before.headers["cache-control"] == "no-store"
    assert set(shown.json()) == set(before.json()) == CASE_KEYS == set(WrittenCase.model_fields)
    mine, theirs = shown.json(), before.json()
    assert mine["assignment"] != theirs["assignment"]
    assert {k: v for k, v in mine.items() if k != "assignment"} == {
        k: v for k, v in theirs.items() if k != "assignment"
    }
    assert mine["written"]["text"].startswith("Thank you for your message")
    assert set(mine["input"]) == {"message"}  # the context the rubric names, nothing else
    told = world.grade(mine["assignment"], FAIL, a)
    assert (told.status_code, told.json()) == (200, {"remaining": 0})
    after = world.next(b)
    assert after.json() == theirs  # nothing of the first grader's answer reaches the second
    text = after.text + json.dumps(dict(after.headers))
    (item,) = [row["item"] for row in rows(world.store, "written_items")]
    for private in (
        JUDGE_MARK,
        round_id,
        item,
        first,
        "pass_from",
        "must_pass",
        "confidence",
        "kappa",
        "salt",
        "mock-written-agent",
        'partly": 1',
    ):
        assert private not in text, private
    told = world.grade(theirs["assignment"], PASS, b)
    assert (told.status_code, told.json()) == (200, {"remaining": 0})  # never how it compares
    assert world.next(b).status_code == 204 and world.next(b).content == b""


def rows(store: AuditStore, table: str) -> list[dict[str, Any]]:
    with store._db.session(exclusive=False) as db:
        return db.all(f"SELECT * FROM {table} ORDER BY id")


def test_no_report_exists_before_the_round_closes_and_a_planted_judge_reaches_no_grader(
    world: World,
) -> None:
    (first, a), (second, b) = world.person("grader-a"), world.person("grader-b")
    viewer = world.person("reader", "viewer")[1]
    round_id = world.round([first, second], sample=4)
    said = []
    for headers in (a, b):
        said.append(world.client.get(LABELLING, headers=headers).text)
        while (shown := world.next(headers)).status_code == 200:
            said.append(shown.text)
            said.append(world.grade(shown.json()["assignment"], PASS, headers).text)
    assert len(said) == 18 and JUDGE_MARK not in "".join(said)
    listed = world.client.get(ROUNDS, headers=viewer)
    assert listed.status_code == 200
    (found,) = listed.json()
    assert set(found) == set(WrittenRound.model_fields)
    assert (found["id"], found["state"], found["report"]) == (round_id, "open", None)
    assert (found["items"], found["graded"], found["open"]) == (4, 8, 0)
    assert JUDGE_MARK not in listed.text and "kappa" not in listed.text
    # A grader may not read the rounds: their role holds `label`, and nothing else.
    assert world.client.get(ROUNDS, headers=a).status_code == 403
    world.rounds.close(round_id, by="ops")
    (closed,) = world.client.get(ROUNDS, headers=viewer).json()
    report = closed["report"]
    assert closed["state"] == "closed" and report["format"] == "mendari.written/1"
    assert report["judge"]["model"] == JUDGE_MARK  # the report names the judge, once closed
    assert set(report["graders"]) == {first, second}
    assert "Thank you" not in json.dumps(closed) and "written-0" not in json.dumps(closed)
    assert world.next(a).status_code == 204
    # Only this agent's, when asked; and never another organization's.
    by_agent = {"pack_id": "mendari-test/written-mini"}
    assert len(world.client.get(ROUNDS, params=by_agent, headers=viewer).json()) == 1
    assert world.client.get(ROUNDS, params={"pack_id": "other/agent"}, headers=viewer).json() == []
    stranger = world.person("reader-b", "viewer", org=OTHER)[1]
    assert world.client.get(ROUNDS, headers=stranger).json() == []


# -- one answer, one refusal --------------------------------------------------------------------


def test_someone_elses_assignment_answers_as_an_unknown_one_does(world: World) -> None:
    (first, a), (second, b) = world.person("grader-a"), world.person("grader-b")
    (third, c) = world.person("grader-c", org=OTHER)
    world.round([first, second], sample=1, double=0)
    theirs = world.round(
        [third, world.person("grader-d", org=OTHER)[0]], sample=1, org=OTHER, double=0
    )
    headers = next(h for h in (a, b) if world.next(h).status_code == 200)  # who holds it
    other = b if headers is a else a
    assignment = world.next(headers).json()["assignment"]
    before = json.dumps(rows(world.store, "written_assignments"), default=str)
    graded = len(events(world.store, "written.graded"))
    for sender, which in (
        (other, assignment),  # another grader's
        (c, assignment),  # another organization's
        (headers, str(uuid.uuid4())),  # unknown
        (headers, "not-an-id"),
    ):
        refused = world.grade(which, PASS, sender)
        assert refused.status_code == 404, refused.text
        assert refused.json()["detail"] == "no open assignment of this labeller has that id"
    assert json.dumps(rows(world.store, "written_assignments"), default=str) == before
    assert len(events(world.store, "written.graded")) == graded
    assert world.grade(assignment, PASS, headers).status_code == 200
    assert world.grade(assignment, FAIL, headers).status_code == 404  # once, never changed
    # Another organization's grader counts their own organization's round, never this one.
    counted = world.client.get(LABELLING, headers=c).json()["sets"]
    assert {found["set"] for found in counted} <= {theirs}


def test_a_closed_round_takes_no_grade(world: World) -> None:
    (first, a), (second, _) = world.person("grader-a"), world.person("grader-b")
    round_id = world.round([first, second], sample=2)
    assignment = world.next(a).json()["assignment"]
    world.rounds.close(round_id, by="ops")
    assert world.grade(assignment, PASS, a).status_code == 404
    assert world.next(a).status_code == 204
    assert world.client.get(LABELLING, headers=a).json() == {"sets": [], "remaining": 0}


def test_a_422_names_the_criterion_never_a_value(world: World) -> None:
    (first, a), (second, _) = world.person("grader-a"), world.person("grader-b")
    world.round([first, second], sample=1)
    assignment = world.next(a).json()["assignment"]
    mark = "PLANTED-GRADE-VALUE"
    for grades, said in (
        ({**PASS, "addresses_request": mark}, "grades.addresses_request is not one of its levels"),
        (
            {"safe_to_send": "yes"},
            "grades answer every criterion: addresses_request, plain_language ungraded",
        ),
        ({**PASS, mark: "yes"}, "grades name a criterion this rubric does not have"),
    ):
        refused = world.grade(assignment, grades, a)
        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"] == said and mark not in refused.text
    malformed = world.client.post(grade_url(assignment), json={"labels": [mark]}, headers=a)
    assert malformed.status_code == 422 and mark not in malformed.text
    assert events(world.store, "written.graded") == []
    assert world.grade(assignment, PASS, a).status_code == 200


# -- who reaches them ---------------------------------------------------------------------------


def test_only_a_person_holding_label_reaches_a_graders_routes(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    (first, a), (second, _) = world.person("grader-a"), world.person("grader-b")
    world.round([first, second], sample=1)
    secrets = {
        role: world.store.create_api_key(name=role, role=role, org=CLINIC, created_by="t")["key"]
        for role in ("viewer", "service")
    }
    for role in sorted({*ROLES, "labeller"} - set(secrets)):
        secrets[f"legacy {role}"] = legacy_key(world.db, role, CLINIC)
    people = {role: world.person(f"member-{role}", role)[1] for role in ROLES}
    launch = launched(world.store)
    nobody = member(world.store, org=None, sub="nobody")
    capsys.readouterr()

    def answer(method: str, path: str, headers: dict[str, str]) -> Any:
        sent: dict[str, Any] = {"json": {"labels": PASS}} if method == "POST" else {}
        return world.client.request(method, path, headers=headers, **sent)

    for method, path in (("GET", LABELLING), ("GET", NEXT), ("POST", grade_url(str(uuid.uuid4())))):
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
    assert not [line for line in lines if line["event"].startswith("written.")]
    assert world.client.get(LABELLING, headers=a).status_code == 200
    assert world.next(a).status_code == 200
    # The rounds: anyone who may read, a key too; never anonymous.
    assert world.client.get(ROUNDS).status_code == 401
    for role in ("viewer", "reviewer", "admin"):
        assert world.client.get(ROUNDS, headers=people[role]).status_code == 200, role
    keyed = world.client.get(ROUNDS, headers={"authorization": f"Bearer {secrets['viewer']}"})
    assert keyed.status_code == 200 and len(keyed.json()) == 1


def test_whoever_manages_the_sets_grades_nothing_and_is_never_assigned(world: World) -> None:
    (first, _), (second, _) = world.person("grader-a"), world.person("grader-b")
    curator, headers = world.person("lead", "admin", "labeller")
    made = world.rounds.create_round(
        CLINIC, world.pack, RUBRIC, world.run, sample=2, created_by="o"
    )
    with pytest.raises(Exception, match="manages hidden sets"):
        world.rounds.assign(str(made["round"]), [first, curator])
    world.rounds.assign(str(made["round"]), [first, second])
    assert world.next(headers).status_code == 204
    assert world.client.get(LABELLING, headers=headers).json() == {"sets": [], "remaining": 0}


# -- chained and logged -------------------------------------------------------------------------


def test_each_read_and_grade_is_chained_and_logged_by_round_id_only(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    (first, a), (second, _) = world.person("grader-a"), world.person("grader-b")
    round_id = world.round([first, second], sample=2)
    counts = world.client.get(LABELLING, headers=a)
    assert counts.json() == {"sets": [{"set": round_id, "open": 2, "labelled": 0}], "remaining": 2}
    capsys.readouterr()
    shown = world.next(a).json()
    assert world.next(a).json() == shown  # the oldest open item, until it is graded
    reads = events(world.store, "written.item_read")
    assert [e["payload"]["assignment"] for e in reads] == [shown["assignment"]] * 2
    assert world.grade(shown["assignment"], PASS, a).json() == {"remaining": 1}
    (graded,) = events(world.store, "written.graded")
    assert graded["payload"]["grader"] == first
    refused = world.grade(str(uuid.uuid4()), PASS, a)
    assert refused.status_code == 404
    lines = logged(capsys)
    said = [(line["event"], line.get("ids"), line["outcome"], line.get("reason")) for line in lines]
    assert [entry for entry in said if entry[0] != "request"] == [
        ("written.item.read", [round_id], "ok", None),
        ("written.item.read", [round_id], "ok", None),
        ("written.graded", [round_id], "ok", None),
        ("written.graded", None, "refused", "no_assignment"),
    ]
    assert {line["route"] for line in lines if line["event"] == "request"} == {NEXT, GRADE}
    assert {line["actor"] for line in lines} == {first}
    logged_text = json.dumps(lines)
    for private in (shown["assignment"], shown["written"]["text"], "Thank you", "fully"):
        assert private not in logged_text, private
    assert world.client.get(LABELLING, headers=a).json() == {
        "sets": [{"set": round_id, "open": 1, "labelled": 1}],
        "remaining": 1,
    }
    assert world.store.verify()["ok"]


# -- mounting and documentation -----------------------------------------------------------------


def test_the_routes_are_mounted_in_memberships_mode_under_encrypt_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memberships(monkeypatch)
    assert written_routes(create_app(db_path=tmp_path / "both.db")) == ROUTES
    assert {permission_for(m, p) for m, p in GRADER_ROUTES} == {"label"}
    assert permission_for("GET", ROUNDS) == "read"
    # Claims mode: none mounted, and the paths read as any route no rule lists, `admin`.
    monkeypatch.setenv("MENDARI_ACCOUNTS", "claims")
    claims = create_app(db_path=tmp_path / "claims.db")
    assert written_routes(claims) == set()
    with encrypted_store(tmp_path / "claims.db") as store:
        admin, viewer = signed_in(store, role="admin"), signed_in(store, role="viewer")
    client = TestClient(claims, base_url=HTTPS)
    for method, path in (
        ("GET", LABELLING),
        ("GET", NEXT),
        ("POST", grade_url("x")),
        ("GET", ROUNDS),
    ):
        assert client.request(method, path, headers=admin).status_code == 404, path
        assert client.request(method, path, headers=viewer).status_code == 403, path
    # Memberships mode under PHI mode `hash`, which keeps no text to grade: none mounted.
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    monkeypatch.setenv("MENDARI_PHI_MODE", "hash")
    monkeypatch.delenv("MENDARI_PHI_KEY")
    hashed = create_app(db_path=tmp_path / "hashed.db")
    assert written_routes(hashed) == set()
    # Open mode (no sign-in): none either.
    for name in (
        "MENDARI_OIDC_ISSUER",
        "MENDARI_OIDC_CLIENT_ID",
        "MENDARI_ACCOUNTS",
        "MENDARI_PHI_MODE",
    ):
        monkeypatch.delenv(name)
    monkeypatch.delenv("MENDARI_INPUT_HASH_KEY")
    assert written_routes(create_app(db_path=tmp_path / "open.db")) == set()


def test_the_answers_are_closed_and_documented(world: World) -> None:
    for model in (WrittenCase, WrittenText, CriterionGuidance, WrittenRound, LabelRecorded):
        assert model.model_config.get("extra") == "forbid", model.__name__
    assert set(WrittenCase.model_fields) == CASE_KEYS
    good = {
        "assignment": "a",
        "questions": {"c": ["no", "yes"]},
        "guidance": {
            "c": {"instructions": "?", "display": None, "levels": {"no": "N", "yes": "Y"}}
        },
        "written": {"name": "reply_draft", "description": "A reply.", "text": "Hello."},
        "input": {},
    }
    WrittenCase.model_validate(good)
    for extra in (
        {**good, "judge": {"c": "yes"}},
        {**good, "written": {**good["written"], "item": "x"}},  # type: ignore[dict-item]
        {**good, "guidance": {"c": {**good["guidance"]["c"], "pass_from": "yes"}}},  # type: ignore[index]
    ):
        with pytest.raises(ValidationError, match="Extra inputs"):
            WrittenCase.model_validate(extra)
    admin = member(world.store, org=CLINIC, roles=["admin"], sub="lead")
    schema = world.client.get("/openapi.json", headers=admin).json()
    for method, path in ROUTES:
        operation = schema["paths"][path][method.lower()]
        assert operation["tags"] == ["Written output"], (method, path)
    for method, path in GRADER_ROUTES:
        operation = schema["paths"][path][method.lower()]
        assert operation["security"] == [{"session": []}], (method, path)
    assert "204" in schema["paths"][NEXT]["get"]["responses"]
    assert "Written output" in {tag["name"] for tag in schema["tags"]}


def test_a_report_is_served_only_as_the_audit_trail_has_it(world: World) -> None:
    """A stored report edited in the database is not served: the round's `note` says it does
    not match the audit trail. A round left closing says so, with how to finish it."""
    (first, a), (second, b) = world.person("grader-a"), world.person("grader-b")
    viewer = world.person("reader", "viewer")[1]
    round_id = world.round([first, second], sample=2)
    for headers in (a, b):
        while (shown := world.next(headers)).status_code == 200:
            world.grade(shown.json()["assignment"], PASS, headers)
    world.rounds.close(round_id, by="ops")
    (closed,) = world.client.get(ROUNDS, headers=viewer).json()
    assert closed["note"] is None and closed["report"]["seeds"]["resamples"] == 2000
    forged = dict(closed["report"])
    forged["criteria"] = {
        key: {**found, "validation": {"state": "validated", "reasons": []}}
        for key, found in forged["criteria"].items()
    }
    with world.store._db.session(exclusive=True) as db:
        db.all("UPDATE written_rounds SET report = ? WHERE id = ?", (json.dumps(forged), round_id))
    (served,) = world.client.get(ROUNDS, headers=viewer).json()
    assert served["report"] is None and "does not match the audit trail" in served["note"]
    assert "validated" not in json.dumps(served)
    assert world.store.verify()["reason"] == "written"
    with world.store._db.session(exclusive=True) as db:
        db.all("UPDATE written_rounds SET state = 'closing', report = NULL")
    (left,) = world.client.get(ROUNDS, headers=viewer).json()
    assert left["state"] == "closing" and left["report"] is None
    assert "Run `mendari written round close` on it again to finish" in left["note"]
