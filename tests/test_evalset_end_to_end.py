# SPDX-License-Identifier: Apache-2.0
"""Hidden evaluation sets end to end (LP-11o, ADR-0011): one set's whole lifecycle, in the order
docs/operations/evalsets.md walks it ("A set from start to end"), through the key holder's
command line (`main`, as `mendari` runs it) and the routes as each role calls them, on one
deployment in memberships mode with sign-in, PHI mode `encrypt`, both evaluation-set keys and
its rate limits as deployed:

1. two organizations (`mendari access org create`), each with an administrator, the first with
   three labellers and a reviewer;
2. the deployment in use with the keys on: health names their ids to a reader, the sets' routes
   are mounted, and it has served a decision (the key holder's commands write only to a
   database a server has used);
3. `mendari evalset create` and `add`: a draft set of hand-written cases for each organization;
4. the first organization's administrator draws its cases and assigns them to two labellers
   through the curators' routes, and is refused the other organization's set;
5. the two label through their own routes, blind, and disagree on one case; `mendari evalset
   progress` counts the cases by status and each labeller's work;
6. the administrator gives the disputed case to a third labeller, who settles it;
7. `mendari evalset agreement` and `coverage` measure the set and store the aggregates, which the
   administrator's read then shows;
8. `mendari evalset freeze`, as the runbook writes it: the commitment and the rotation rule
   fixed, labelling closed;
9. `mendari access grader-key`, as the runbook writes it, mints the organization's grading key,
   which grades the set with the runbook's `curl` body through `POST /api/evalsets/{set_id}/runs`
   with aggregates only (the other organization's key is answered as for an unknown set);
   `mendari evalset show`, as the runbook writes it, reads it;
10. `mendari evalset retire`, as the runbook writes it: the set grades nothing more;
11. `mendari audit verify --evalsets` passes, `mendari evalset rotate --check` is ready, and the
    set's chain holds its events in the lifecycle's order.

Each step asserts what that step promises. Throughout, every answer (body and headers), every
log record and security-log line and every command's output is kept and scanned for the planted
marks of each case's content (its input, its author, the id its author gave it): none holds
one, but a labeller's own next case, which is the case's input by design (LP-11j), and which
therefore shows the scanner can see the mark. The leak sweep (tests/test_evalset_leaks.py,
LP-11g) covers every route and principal; this test covers the order. On SQLite and Postgres
(`db_target`); keys are made per test, cases are hand-written synthetic text."""

from __future__ import annotations

import json
import logging
import re
import shlex
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore
from mendari.cli import main
from mendari.server.app import create_app
from mendari.server.auth import person_id
from tests.conftest import (
    ROOT,
    encrypt_mode,
    encrypted_store,
    evalset_on,
    evalsets_are_on,
    member,
    runbook_body,
)
from tests.test_auth import CLIENT, IDP
from tests.test_evalset_labelling import events
from tests.test_evalset_lifecycle import documented
from tests.test_evalset_store import chained

HTTPS = "https://mendari.test"
INBOX = "mendari/patient-inbox-router"
CLINIC, OTHER = "clinic-a", "clinic-b"
RUNBOOK = ROOT / "docs/operations/evalsets.md"
# Planted in each case's content: no answer, log line or command output may hold one, but a
# labeller's own next case its input.
INPUT = "zqendtoendinputmark"
AUTHOR = "zqendtoendauthormark"
AUTHOR_ID = "zqendtoendcaseidmark"
CONTENT = (INPUT, AUTHOR, AUTHOR_ID)
STRATA = ("portal", "after-hours")
NURSE = {"destination": "nurse", "urgency": "routine"}
PHYSICIAN = {"destination": "physician", "urgency": "routine"}
DISPUTED = 1  # the case the second labeller answers otherwise
CASE_HASH = re.compile(r"^hmac-sha256:[0-9a-f]{16}:[0-9a-f]{64}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
# A set's events, phase by phase: its chain holds them in this order (a labeller's reads and
# labels, and the adjudicator's assignment, interleave within the labelling).
LIFECYCLE = (
    ("evalset.created",),
    ("evalset.case_added",),
    ("evalset.sampled",),
    ("evalset.assigned", "evalset.case_read", "evalset.labelled"),
    ("evalset.measured",),
    ("evalset.frozen",),
    ("evalset.activated",),
    ("evalset.graded",),
    ("evalset.retired",),
)


@dataclass
class World:
    client: TestClient
    store: AuditStore
    db: str
    capsys: pytest.CaptureFixture[str]
    seen: list[tuple[str, str]] = field(default_factory=list)  # (where, what it said)
    shown: list[dict[str, Any]] = field(default_factory=list)  # the labellers' next cases

    def drained(self) -> None:
        """What was printed since the last read: the security log's lines (the access line
        included), and anything else written to the console."""
        out, err = self.capsys.readouterr()
        self.seen.append(("the console", out + err))

    def call(self, method: str, path: str, headers: Mapping[str, str], **sent: Any) -> Any:
        """A request as `headers`' principal, its answer kept to scan (body and headers)."""
        answer = self.client.request(method, path, headers=dict(headers), **sent)
        self.seen.append((f"{method} {path}", answer.text + json.dumps(dict(answer.headers))))
        return answer

    def next_case(self, headers: Mapping[str, str]) -> Any:
        """A labeller's next case: its body is their own case's input by design (LP-11j), kept
        apart from what is scanned; its headers are scanned."""
        answer = self.client.get("/api/labelling/next", headers=dict(headers))
        self.seen.append(("GET /api/labelling/next (headers)", json.dumps(dict(answer.headers))))
        if answer.status_code == 200:
            self.shown.append(answer.json())
        return answer

    def run(self, argv: list[str]) -> tuple[int, str, str]:
        """`mendari <argv>`, its output kept to scan."""
        self.drained()
        code = main(argv)
        out, err = self.capsys.readouterr()
        self.seen.append((f"mendari {' '.join(argv[:2])}", out + err))
        return code, out, err

    def command(self, *argv: str) -> tuple[int, str, str]:
        """`mendari <argv> --db <the deployment's database>`."""
        return self.run([*argv, "--db", self.db])


def deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Memberships mode with sign-in, PHI mode `encrypt` and both evaluation-set keys (throwaway
    ones, made here), in the environment the server and the commands read; the rate limits on,
    as sign-in has them by default (the suite turns them off)."""
    monkeypatch.setenv("MENDARI_RATE_LIMITS", "on")
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", CLIENT)
    encrypt_mode(monkeypatch)
    evalset_on(monkeypatch)


@pytest.fixture
def world(
    db_target: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> Iterator[World]:
    caplog.set_level(logging.INFO)
    deployment(monkeypatch)
    with (
        encrypted_store(db_target) as store,
        TestClient(create_app(db_path=db_target), base_url=HTTPS) as client,
    ):
        yield World(client, store, db_target, capsys)


def cases_file(tmp_path: Path, org: str, count: int) -> Path:
    """`count` hand-written cases of `org`, as their author writes them (a JSONL file outside
    the repository): each input carries its number, all a labeller reads, and the input mark;
    each author and author's id a mark of their own; two strata."""
    path = tmp_path / org / "cases.jsonl"
    path.parent.mkdir()
    records = [
        {
            "id": f"{AUTHOR_ID}-{org}-{number}",
            "input": {
                "message": {
                    "body": f"{INPUT} {org} case {number}: my knee aches after the long walk",
                    "channel": "portal",
                }
            },
            "attributes": {
                "author": AUTHOR,
                "language": "en",
                "stratum": STRATA[number % len(STRATA)],
                "source": "hand_written",
                "template": None,
            },
        }
        for number in range(count)
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def number_of(case: Mapping[str, Any]) -> int:
    """The number the case's input carries: what the labeller reads to answer."""
    found = re.search(r" case (\d+):", case["input"]["message"]["body"])
    assert found is not None
    return int(found[1])


def label_through_the_routes(
    world: World,
    headers: Mapping[str, str],
    answer: Callable[[int], dict[str, str]],
    *,
    expected: int,
) -> None:
    """A labeller labels every case waiting for them through their routes: shown each one's
    input and the set's questions only, answering what `answer` picks from the case's number;
    told only how many wait. `expected`: how many they label."""
    counts = world.call("GET", "/api/labelling", headers).json()
    assert counts["remaining"] == expected
    done = 0
    while (shown := world.next_case(headers)).status_code == 200:
        case = shown.json()
        assert set(case) == {"assignment", "questions", "input"}
        assert set(case["questions"]) == {"destination", "urgency"}
        labelled = world.call(
            "POST",
            f"/api/labelling/{case['assignment']}",
            headers,
            json={"labels": answer(number_of(case))},
        )
        done += 1
        assert (labelled.status_code, labelled.json()) == (200, {"remaining": expected - done})
    assert shown.status_code == 204 and done == expected


def the_runbooks_grading(world: World, org: str) -> tuple[dict[str, str], dict[str, Any]]:
    """The runbook's grading key for `org` (its `mendari access grader-key` line, as written),
    minted on the deployment's database, and its run's `curl` body: the key's request headers
    and that body."""
    text = RUNBOOK.read_text().replace("\\\n", " ")
    (minting,) = [line for line in text.splitlines() if line.startswith("mendari access grader-")]
    argv = shlex.split(minting.replace("<org>", org).replace('"$DATABASE_URL"', world.db))
    code, out, _ = world.run(argv[1:])
    assert code == 0
    found = re.findall(r"curl -sS -X POST \"\$MENDARI/api/evalsets/<set id>/runs\".*", text)
    (posted,) = [line for line in found if '"arms"' not in line]  # the scoreboard's is LP-17k's
    key = json.loads(out)["key"]
    body = json.loads(shlex.split(posted)[-1])
    sent = runbook_body(body, INBOX, variants={"router": "rules"})
    return {"authorization": f"Bearer {key}"}, sent


def test_a_hidden_set_from_creation_to_retirement_through_the_cli_and_http(
    world: World, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = world.store

    # 1. Two organizations, made on the command line; their people signed in.
    for org in (CLINIC, OTHER):
        code, out, _ = world.command("access", "org", "create", org, "--name", org.title())
        assert code == 0 and json.loads(out)["id"] == org
    curator = member(store, org=CLINIC, roles=["admin"], sub="curator-a")
    other_curator = member(store, org=OTHER, roles=["admin"], sub="curator-b")
    reviewer = member(store, org=CLINIC, roles=["reviewer"], sub="reviewer-a")
    labeller = {
        name: member(store, org=CLINIC, roles=["labeller"], sub=f"labeller-{name}")
        for name in ("first", "second", "third")
    }
    user = {name: person_id(IDP, f"labeller-{name}") for name in labeller}

    # 2. The deployment in use with the keys on: health names their ids to a reader and the sets'
    #    routes are mounted; it serves a decision, so its database is a server's.
    evalsets_are_on(world.client, curator)
    decided = world.call(
        "POST",
        f"/api/agents/{INBOX}/decide",
        reviewer,
        json={"message": {"body": "Can I get a refill of my lisinopril?", "channel": "portal"}},
    )
    assert decided.status_code == 200, decided.text

    # 3. A draft set for each organization, of hand-written cases, from the command line: ids and
    #    keyed case hashes printed, never a case.
    made, hashes = {}, {}
    for org, count in ((CLINIC, 4), (OTHER, 2)):
        code, out, _ = world.command("evalset", "create", INBOX, "--org", org, "--name", "Spring")
        assert code == 0
        made[org] = json.loads(out)["set"]
        cases = cases_file(tmp_path, org, count)
        code, out, _ = world.command("evalset", "add", made[org], str(cases))
        added = json.loads(out)
        assert code == 0 and added["added"] == count
        assert [case["line"] for case in added["cases"]] == list(range(1, count + 1))
        hashes[org] = [case["case"] for case in added["cases"]]
        assert all(CASE_HASH.match(hashed) for hashed in hashes[org])
    set_id = made[CLINIC]

    # 4. The administrator draws the cases and gives each to two labellers, through the curators'
    #    routes; the other organization's set is no set of theirs.
    listed = world.call("GET", "/api/evalsets", curator)
    assert [(item["set"], item["state"], item["cases"]) for item in listed.json()] == [
        (set_id, "draft", 4)
    ]
    refused = world.call("GET", f"/api/evalsets/{made[OTHER]}", curator)
    assert refused.status_code == 404 and made[OTHER] not in refused.text
    drawn = world.call(
        "POST", f"/api/evalsets/{set_id}/sample", curator, json={"per_stratum": 2, "seed": 0}
    )
    assert (drawn.status_code, drawn.json()) == (
        200,
        {"set": set_id, "drawn": 4, "sampled": 4, "seed": 0},
    )
    pair = {"labellers": [user["first"], user["second"]]}
    assigned = world.call("POST", f"/api/evalsets/{set_id}/assignments", curator, json=pair)
    assert (assigned.status_code, assigned.json()) == (
        200,
        {"set": set_id, "round": "first", "assigned": 8, "withdrawn": 0},
    )

    # 5. The two label blind through their own routes, and disagree on one case; the key holder
    #    follows the labelling by status and by labeller.
    label_through_the_routes(world, labeller["first"], lambda number: NURSE, expected=4)
    label_through_the_routes(
        world,
        labeller["second"],
        lambda number: PHYSICIAN if number == DISPUTED else NURSE,
        expected=4,
    )
    assert world.next_case(labeller["third"]).status_code == 204  # not assigned yet
    code, out, _ = world.command("evalset", "progress", set_id)
    progress = json.loads(out)
    assert code == 0 and (progress["state"], progress["cases"]) == ("draft", 4)
    assert {status: n for status, n in progress["statuses"].items() if n} == {
        "agreed": 3,
        "disputed": 1,
    }
    assert {name: work["first"]["labelled"] for name, work in progress["labellers"].items()} == {
        user["first"]: 4,
        user["second"]: 4,
    }

    # 6. The disputed case goes to a third labeller, who works from its input alone.
    third = {"labellers": [user["third"]]}
    adjudicated = world.call("POST", f"/api/evalsets/{set_id}/adjudication", curator, json=third)
    assert (adjudicated.status_code, adjudicated.json()) == (
        200,
        {"set": set_id, "round": "adjudication", "assigned": 1, "withdrawn": 0},
    )
    label_through_the_routes(world, labeller["third"], lambda number: NURSE, expected=1)
    detail = world.call("GET", f"/api/evalsets/{set_id}", curator).json()
    assert {status: n for status, n in detail["statuses"].items() if n} == {
        "agreed": 3,
        "resolved": 1,
    }
    assert detail["aggregates"] is None  # nothing measured yet

    # 7. The key holder measures agreement and coverage; the administrator's read shows them.
    code, out, _ = world.command("evalset", "agreement", set_id, "--resamples", "50", "--json")
    agreement = json.loads(out)
    assert code == 0 and agreement["format"] == "mendari.agreement/1"
    percent = agreement["questions"]["destination"]["percent"]
    assert (percent["k"], percent["n"]) == (3, 4)
    code, out, _ = world.command("evalset", "coverage", set_id, "--json")
    covered = json.loads(out)
    assert code == 0 and covered["format"] == "mendari.coverage/1" and covered["cases"] == 4
    aggregates = world.call("GET", f"/api/evalsets/{set_id}", curator).json()["aggregates"]
    assert (aggregates["set"], aggregates["n"]) == (set_id, 4)

    # 8. Frozen under its rotation rule, as the runbook writes it: the commitment printed and
    #    chained, labelling closed.
    code, out, _ = world.run(documented("freeze", set_id, world.db))
    frozen = json.loads(out)
    assert code == 0 and (frozen["state"], frozen["n"], frozen["dropped"]) == ("frozen", 4, 0)
    assert SHA256.match(frozen["commitment"])
    rule = (frozen["max_runs"], frozen["max_age_days"])
    commitments = [event["payload"] for event in events(store, "evalset.frozen")]
    assert [(p["set"], p["commitment"]) for p in commitments] == [(set_id, frozen["commitment"])]
    for headers in labeller.values():
        assert world.next_case(headers).status_code == 204

    # 9. The organization's grading key, minted as the runbook writes it, grades it with the
    #    runbook's body through the grading route, with aggregates only; the other organization's
    #    key is told of no set, and no person grades.
    grading, run = the_runbooks_grading(world, CLINIC)
    other_grading, _ = the_runbooks_grading(world, OTHER)
    url = f"/api/evalsets/{set_id}/runs"
    elsewhere = world.call("POST", url, other_grading, json=run)
    assert (elsewhere.status_code, elsewhere.json()["reason"]) == (404, "no_set")
    assert world.call("POST", url, curator, json=run).status_code == 403
    graded = world.call("POST", url, grading, json=run)
    assert graded.status_code == 200, graded.text
    result = graded.json()
    assert (result["format"], result["set"], result["commitment"]) == (
        "mendari.hidden_result/1",
        set_id,
        frozen["commitment"],
    )
    assert (result["n"], result["run"], result["runs_left"]) == (4, 1, rule[0] - 1)
    assert not any(hashed in graded.text for hashed in hashes[CLINIC])  # no case named
    code, out, _ = world.run(documented("show", set_id, world.db))
    shown = json.loads(out)
    assert code == 0 and (shown["state"], shown["commitment"]) == ("active", frozen["commitment"])
    assert (shown["max_runs"], shown["max_age_days"]) == rule
    assert shown["runs"] == {"used": 1, "left": rule[0] - 1}

    # 10. Retired, as the runbook writes it: it grades nothing more.
    code, out, _ = world.run(documented("retire", set_id, world.db))
    retired = json.loads(out)
    assert code == 0 and (retired["state"], retired["runs"]) == ("retired", 1)
    again = world.call("POST", url, grading, json=run)
    assert (again.status_code, again.json()["reason"]) == (409, "not_gradable")

    # 11. The chain and the sealed rows hold: verification passes, the key is ready to rotate,
    #     and the set's chain holds its events in the lifecycle's order.
    code, out, _ = world.command("audit", "verify", "--evalsets")
    verified = json.loads(out)
    assert code == 0 and verified["ok"]
    assert verified["evalsets"] == {
        "sets": 2, "checked": 6, "removed": 0, "unchecked": 0, "assignments": 9, "labels": 9,
        "frozen": 1, "predating": 0,
    }  # fmt: skip
    code, out, _ = world.command("evalset", "rotate", "--check")
    assert code == 0 and json.loads(out)["ready"]
    kinds = [e["event"] for e in chained(store) if e["decision_id"] == f"evalset:{set_id}"]
    phase = {kind: number for number, phased in enumerate(LIFECYCLE) for kind in phased}
    phases = [phase[kind] for kind in kinds]  # a kind outside the lifecycle fails here
    assert phases == sorted(phases) and set(phases) == set(range(len(LIFECYCLE)))
    assert Counter(kinds) == {
        "evalset.created": 1, "evalset.case_added": 4, "evalset.sampled": 1,
        "evalset.assigned": 9, "evalset.case_read": 9, "evalset.labelled": 9,
        "evalset.measured": 2, "evalset.frozen": 1, "evalset.activated": 1,
        "evalset.graded": 1, "evalset.retired": 1,
    }  # fmt: skip

    # The other organization's set stayed its own, and as it was made.
    code, out, _ = world.command("evalset", "list")
    assert [(item["set"], item["state"]) for item in json.loads(out)] == [
        (set_id, "retired"),
        (made[OTHER], "draft"),
    ]
    assert world.call("GET", "/api/evalsets", other_curator).json()[0]["state"] == "draft"

    # Throughout: no answer, log or command output held a case's content, but each labeller's own
    # next case its input (nine shown: four, four and one, all of their own organization's), which
    # shows the scanner sees it; the security log's lines were among what was scanned.
    world.drained()
    world.seen.append(("the log", caplog.text))
    assert len(world.shown) == 9
    assert all(INPUT in case["input"]["message"]["body"] for case in world.shown)
    assert not any(mark in json.dumps(world.shown) for mark in (AUTHOR, AUTHOR_ID, OTHER))
    console = "".join(said for where, said in world.seen if where == "the console")
    assert '"evalset.case.read"' in console and '"evalset.graded"' in console
    for where, said in world.seen:
        for mark in CONTENT:
            assert mark not in said, (where, mark)
