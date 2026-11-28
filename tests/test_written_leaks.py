# SPDX-License-Identifier: Apache-2.0
"""No text in any report (ADR-0025), and none kept but a held one (ADR-0027): a distinct mark
is planted in every text the agent writes and in every context field a grader reads, and then
everything written output produces is searched for them: the ledger file, the report as JSON
and as text, every `mendari written` command's output, every route's answer, the security log,
the audit chain and every table of the database.

A text is in no record, reason, log line, report or chained event, under any PHI mode. A mark
may be in four places only:

- `GET /api/written/labelling/next`, answered to the grader the item was assigned to;
- `written_items.body` under PHI mode `off` (plain there, never in a protected database);
- the `notes` table, for a text held for a decision's reviewer: sealed under `encrypt`, in
  clear under `off`;
- the review queue and the decision's record, answered to whoever may read inputs.

A text of a decision that automates is kept nowhere. The search must find the marks where they
may be, so a search that finds nothing fails too. A blocked decision's text, PHI mode `hash`
and an agent that keeps only its input's hash are searched in tests/test_held_texts.py, and
`mendari demo`'s own output in tests/test_written_demo.py. Every text here is synthetic."""

from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, load_agent
from mendari.cli import main
from mendari.core import tenancy
from mendari.core.decision import DecisionResult
from mendari.core.spec import Outcome
from mendari.core.written import WrittenStore
from mendari.evals import resolve_dataset, written
from mendari.providers.judge import Judge
from mendari.server.app import create_app
from mendari.server.auth import person_id
from tests.conftest import ROOT, encrypt_mode, encrypted_store, member
from tests.test_auth import CLIENT, IDP

MINI = ROOT / "tests" / "fixtures" / "mini-agents" / "written-mini"
RUBRIC = "reply_quality"
CLINIC = "clinic-a"
HTTPS = "https://mendari.test"
OUT_MARK = "ZQOUTPUTMARK"  # in every text the agent writes
CTX_MARK = "ZQCONTEXTMARK"  # in every message, the one context field the rubric names
MARKS = (OUT_MARK, CTX_MARK)
PASS = {"safe_to_send": "yes", "addresses_request": "fully", "plain_language": "yes"}


def marked_agent(tmp_path: Path) -> Path:
    """A copy of the fixture agent whose every written text and every message holds a mark."""
    folder = tmp_path / "agents" / "written-mini"
    shutil.copytree(MINI, folder)
    fixture = json.loads((folder / "fixtures" / "mock_agent.json").read_text())
    for number, response in enumerate(
        [*(c["response"] for c in fixture["cases"]), fixture["default"]]
    ):
        response["outputs"] = {"reply_draft": f"Thank you. {OUT_MARK}-{number} We will reply."}
    (folder / "fixtures" / "mock_agent.json").write_text(json.dumps(fixture))
    lines = [
        json.dumps({"id": f"leak-{n}", "input": {"message": {"body": f"{body} {CTX_MARK}-{n}"}}})
        for n, body in enumerate(("Where do I park?", "Refill my lisinopril.", "Chest pain."))
    ]
    (folder / "data" / "written.jsonl").write_text("\n".join(lines) + "\n")
    return folder


def clean(what: str, text: str) -> None:
    for mark in MARKS:
        assert mark not in text, f"{mark} in {what}"


def decided(store: AuditStore, folder: Path) -> tuple[DecisionResult, DecisionResult]:
    """Two decisions of the marked agent, each carrying a marked text: one that waits for a
    person (the agent's automation is on hold), whose text is held for its reviewer, and one
    recorded by hand as automated, whichever policy serves, whose text is kept nowhere."""
    case = {"message": {"body": "Can I get a refill?"}}
    waiting = Decision(load_agent(folder), store=store, hold_source=lambda _: "a drill")(case)
    assert waiting.outcome is Outcome.HUMAN_REVIEW and OUT_MARK in waiting.outputs["reply_draft"]
    automated = replace(waiting, id=str(uuid.uuid4()), outcome=Outcome.AUTO)
    store.record(automated, case)
    return waiting, automated


def tables(db: Path) -> dict[str, str]:
    """Every table of the database, each as one text."""
    with sqlite3.connect(db) as connection:
        names = [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        return {
            name: json.dumps(connection.execute(f'SELECT * FROM "{name}"').fetchall(), default=str)
            for name in names
        }


def printed(out: str) -> Any:
    """The JSON document a command printed, after any line the security log printed before."""
    start = out.rfind("\n{\n")
    return json.loads(out if start < 0 else out[start + 1 :])


def cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    clean(f"`mendari {' '.join(argv[:3])}`", out + err)
    return code, out


def test_no_report_log_table_or_answer_holds_a_text_but_the_graders_and_the_reviewers_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = marked_agent(tmp_path)
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", CLIENT)
    encrypt_mode(monkeypatch)
    db = tmp_path / "mendari.db"

    # The run on the command line: its report as text and as JSON, and the files it writes.
    out = tmp_path / "out" / "written.json"
    args = ("written", "eval", str(folder), "--rubric", RUBRIC, "--dataset", "written")
    assert cli(capsys, *args, "--out", str(out))[0] == 0
    assert cli(capsys, *args, "--json")[0] == 0
    for path in sorted(out.parent.rglob("*")):
        if path.is_file():
            clean(f"the file {path.name}", path.read_text())
    assert len(list((out.parent / "ledgers").glob("*.jsonl"))) == 1

    # A round, made and assigned by the operator's commands on the server's database.
    with encrypted_store(db) as store:
        with tenancy.acting_for(CLINIC):
            waiting, automated = decided(store, folder)
        a = member(store, org=CLINIC, roles=["labeller"], sub="grader-a")
        b = member(store, org=CLINIC, roles=["labeller"], sub="grader-b")
        viewer = member(store, org=CLINIC, roles=["viewer"], sub="reader")
        admin = member(store, org=CLINIC, roles=["admin"], sub="lead")
        reviewer = member(store, org=CLINIC, roles=["reviewer"], sub="nurse")
        outsider = member(store, org="clinic-b", roles=["labeller"], sub="grader-z")
        stranger = member(store, org="clinic-b", roles=["reviewer"], sub="nurse-z")
    users = [person_id(IDP, sub) for sub in ("grader-a", "grader-b")]
    create = ("written", "round", "create", str(folder), "--rubric", RUBRIC, "--dataset", "written")
    code, made = cli(capsys, *create, "--org", CLINIC, "--sample", "3", "--db", str(db))
    assert code == 0
    round_id = printed(made)["round"]
    assign = ("written", "round", "assign", round_id, "--db", str(db), "--double", "1")
    assert cli(capsys, *assign, "--grader", users[0], "--grader", users[1])[0] == 0
    assert cli(capsys, "written", "round", "progress", round_id, "--db", str(db))[0] == 0
    assert cli(capsys, "written", "round", "list", "--db", str(db))[0] == 0
    for name, text in tables(db).items():  # sealed: no table holds a text, the items' included
        clean(f"table {name} before the round closes", text)

    # Over HTTP: every answer, to everyone. Only the assigned grader's next item holds a text,
    # and what a decision's reviewer reads of a text it holds.
    shown_texts = []
    held_texts = []
    with TestClient(create_app(db_path=db, agents_paths=[folder.parent]), base_url=HTTPS) as client:
        # A held text (ADR-0027) is shown where the decision's input is, to whoever may read
        # inputs: in the review queue and in the decision's record. To nobody else; and nothing
        # says what the automated decision's text was, to anyone.
        held, auto = f"/api/audit/{waiting.id}", f"/api/audit/{automated.id}"
        others = (
            ("a reader", viewer),
            ("a grader", a),
            ("another organization's reviewer", stranger),
            ("another organization's grader", outsider),
        )
        for url in ("/api/review", held, auto, "/api/audit", f"{held}/fhir"):
            for who, headers in others:
                clean(f"{url} for {who}", client.get(url, headers=headers).text)
        for who, headers in (("a reviewer", reviewer), ("an administrator", admin)):
            for url in ("/api/review", held):
                read = client.get(url, headers=headers)
                assert read.status_code == 200, read.text
                held_texts.append(read.text)  # the answers a held text may be in
            for url in (auto, "/api/audit", f"{held}/fhir", "/api/audit/export/fhir"):
                clean(f"{url} for {who}", client.get(url, headers=headers).text)
        # The review: a refusal repeats nothing it was sent, and its answer holds no text.
        review = {"final_label": "refill", "reviewer": "x"}
        for texts in ({"reply_draft": waiting.outputs["reply_draft"]}, {OUT_MARK: "release"}):
            refused = client.post(
                f"/api/review/{waiting.id}", json={**review, "texts": texts}, headers=reviewer
            )
            assert refused.status_code == 422
            clean("a refused review", refused.text)
        missing = client.post(f"/api/review/{waiting.id}", json=review, headers=reviewer)
        assert missing.status_code == 409 and "'reply_draft'" in missing.text
        clean("a review without its verdict", missing.text)
        given = client.post(
            f"/api/review/{waiting.id}",
            json={**review, "texts": {"reply_draft": "withhold"}},
            headers=reviewer,
        )
        assert given.status_code == 200, given.text
        clean("the review's answer", given.text)
        for who, headers in (("another organization's grader", outsider), ("a reader", viewer)):
            for url in ("/api/written/labelling", "/api/written/labelling/next"):
                clean(f"{url} for {who}", client.get(url, headers=headers).text)
        for headers in (a, b):
            clean("a grader's counts", client.get("/api/written/labelling", headers=headers).text)
            while (
                shown := client.get("/api/written/labelling/next", headers=headers)
            ).status_code == 200:
                shown_texts.append(shown.text)  # the one answer a text may be in
                assignment = shown.json()["assignment"]
                told = client.post(
                    f"/api/written/labelling/{assignment}", json={"labels": PASS}, headers=headers
                )
                clean("the answer to a grade", told.text)
                refused = client.post(
                    f"/api/written/labelling/{assignment}",
                    json={"labels": {**PASS, "plain_language": OUT_MARK}},
                    headers=headers,
                )
                assert refused.status_code == 404  # graded already: and never the value sent
                clean("a refusal", refused.text)
        for who, headers in (("a reader", viewer), ("an administrator", admin)):
            clean(f"the rounds for {who}", client.get("/api/written/rounds", headers=headers).text)
        clean(
            "the administrative events",
            client.get("/api/audit/administrative?limit=500", headers=admin).text,
        )
        # The security log so far: it names what was read and graded, by round id only, and
        # the decision whose held text and input a reviewer read, by its id only.
        logged = capsys.readouterr()
        assert '"written.item.read"' in logged.out and '"written.graded"' in logged.out
        assert round_id in logged.out
        assert '"phi.inputs.read"' in logged.out and waiting.id in logged.out
        clean("the security log", logged.out + logged.err)
        # Closed by the operator: the report, and what every route answers afterwards.
        code, report = cli(capsys, "written", "round", "close", round_id, "--db", str(db), "--json")
        assert code == 0 and printed(report)["cases"]["graded"] == 3
        for headers in (a, b, viewer, admin):
            for url in (
                "/api/written/labelling",
                "/api/written/labelling/next",
                "/api/written/rounds",
            ):
                clean(f"{url} after the round closed", client.get(url, headers=headers).text)
    # The assigned graders were shown the texts and the context, whole: the search finds them.
    assert len(shown_texts) == 6
    for text in shown_texts:
        assert OUT_MARK in text and CTX_MARK in text
    # And those who may read inputs, the held text, in the queue and in the record.
    assert len(held_texts) == 4
    for text in held_texts:
        assert waiting.outputs["reply_draft"] in text
    # And every line the server printed after the round closed.
    logged = capsys.readouterr()
    clean("the security log after the round closed", logged.out + logged.err)
    # The chain and every table, once the round is closed: the texts are erased.
    kept = tables(db)
    assert {"written_rounds", "written_items", "written_assignments", "audit_events"} <= set(kept)
    for name, text in kept.items():  # the held text is a sealed note: no table holds a word
        clean(f"table {name}", text)
    assert round_id in kept["audit_events"] and "written.graded" in kept["audit_events"]
    # The chain says a text was held and what its reviewer decided, by the output's name; the
    # automated decision's says nothing of its text.
    assert waiting.id in kept["notes"] and automated.id not in kept["notes"]
    with encrypted_store(db) as store:
        assert store.verify()["ok"] and store.verify(notes=True)["ok"]
        events = store.decision_events([waiting.id, automated.id])
        of_texts = [e for e in events[waiting.id] if e["event"].startswith("output.")]
        assert [(e["event"], e["payload"]["output"]) for e in of_texts] == [
            ("output.held", "reply_draft"),
            ("output.resolved", "reply_draft"),
        ]
        # The automated one: its record, and its input read by those who opened it.
        assert {e["event"] for e in events[automated.id]} == {"decision.created", "input.read"}


def test_under_off_the_items_and_a_held_texts_note_hold_the_texts_and_nothing_else_does(
    tmp_path: Path,
) -> None:
    """PHI mode `off` on synthetic data, in a database no protected mode ever kept: the items'
    bodies are plain, and so is the note of a text held for a decision's reviewer, which the
    search finds (so it can find a text). No other column of any table, the chain included,
    holds one, and the text of a decision that automated is nowhere."""
    folder = marked_agent(tmp_path)
    agent = load_agent(folder)
    db = tmp_path / "local.db"
    with AuditStore(db) as store:
        run = written.run(
            Decision(agent, origin_cap=False),
            resolve_dataset(agent, "written"),
            RUBRIC,
            judge=Judge(agent, RUBRIC, mode="canned"),
        )
        clean("the ledger", run.ledger.dumps().decode())
        clean("the run's repr", repr(run))
        found = written.report(run.ledger)
        clean("the report", json.dumps(found))
        rounds = WrittenStore(store)
        made = rounds.create_round(CLINIC, agent, RUBRIC, run, sample=3, created_by="ops")
        clean("a round's answer", json.dumps(made))
        clean("its progress", json.dumps(rounds.progress(made["round"])))
        clean("the rounds", json.dumps(rounds.rounds(CLINIC)))
        waiting, automated = decided(store, folder)
        clean("the automated decision", json.dumps(store.get(automated.id)))
    kept = tables(db)
    assert kept["written_items"].count(OUT_MARK) == 3 and kept["written_items"].count(CTX_MARK) == 3
    # The held text is one note, in clear; the automated decision's is no note.
    assert kept["notes"].count(OUT_MARK) == 1 and waiting.outputs["reply_draft"] in kept["notes"]
    assert waiting.id in kept["notes"] and automated.id not in kept["notes"]
    with sqlite3.connect(db) as connection:
        others = connection.execute(
            "SELECT id, round_id, org, item, sealed, created_at, erased_at FROM written_items"
        ).fetchall()
    clean("an item's other columns", json.dumps(others, default=str))
    with sqlite3.connect(db) as connection:
        beside = connection.execute(
            "SELECT id, decision_id, event, org, created_at, erased_at, erased_by FROM notes"
        ).fetchall()
    clean("a note's other columns", json.dumps(beside, default=str))
    for name, text in kept.items():
        if name not in ("written_items", "notes"):
            clean(f"table {name}", text)
    with AuditStore(db) as store:
        closed: dict[str, Any] = WrittenStore(store).close(made["round"], by="ops")
        clean("the closing's answer", json.dumps(closed))
        # Whoever may read the decision reads its held text (under `off`, as its input).
        shown = store.get(waiting.id)
        assert shown is not None and shown["outputs"][0]["text"] == waiting.outputs["reply_draft"]
    for name, text in tables(db).items():  # closed: the items are erased; the note stays
        if name != "notes":
            clean(f"table {name} after closing", text)
    with AuditStore(db) as store:  # until retention removes it with the inputs
        assert store.purge(0, source="command")["notes"] == 1
        assert store.verify()["ok"] and store.verify(notes=True)["ok"]
    for name, text in tables(db).items():
        clean(f"table {name} after the purge", text)
