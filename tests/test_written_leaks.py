# SPDX-License-Identifier: Apache-2.0
"""No text in any report (ADR-0025): a distinct mark is planted in every text the agent writes
and in every context field a grader reads, and then everything written output produces is
searched for them: the ledger file, the report as JSON and as text, every `mendari written`
command's output, every route's answer, the security log, the audit chain and every table of
the database.

A mark may be in two places only: `GET /api/written/labelling/next` answered to the grader the
item was assigned to, and `written_items.body` under PHI mode `off` (plain there, never in a
protected database). The search must find the marks in both, so a search that finds nothing
fails too. `mendari demo`'s own output is searched in tests/test_written_demo.py. Every text
here is synthetic."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, load_pack
from mendari.cli import main
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


def test_no_report_log_table_or_answer_holds_a_text_but_the_graders_own(
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
        Decision(load_pack(folder), store=store)({"message": {"body": "Can I get a refill?"}})
        a = member(store, org=CLINIC, roles=["labeller"], sub="grader-a")
        b = member(store, org=CLINIC, roles=["labeller"], sub="grader-b")
        viewer = member(store, org=CLINIC, roles=["viewer"], sub="reader")
        admin = member(store, org=CLINIC, roles=["admin"], sub="lead")
        outsider = member(store, org="clinic-b", roles=["labeller"], sub="grader-z")
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

    # Over HTTP: every answer, to everyone. Only the assigned grader's next item holds a text.
    shown_texts = []
    with TestClient(create_app(db_path=db, packs_paths=[folder.parent]), base_url=HTTPS) as client:
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
        # The security log so far: it names what was read and graded, by round id only.
        logged = capsys.readouterr()
        assert '"written.item.read"' in logged.out and '"written.graded"' in logged.out
        assert round_id in logged.out
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
    # And every line the server printed after the round closed.
    logged = capsys.readouterr()
    clean("the security log after the round closed", logged.out + logged.err)
    # The chain and every table, once the round is closed: the texts are erased.
    kept = tables(db)
    assert {"written_rounds", "written_items", "written_assignments", "audit_events"} <= set(kept)
    for name, text in kept.items():
        clean(f"table {name}", text)
    assert round_id in kept["audit_events"] and "written.graded" in kept["audit_events"]
    with encrypted_store(db) as store:
        assert store.verify()["ok"]


def test_under_off_the_items_hold_the_texts_and_nothing_else_does(tmp_path: Path) -> None:
    """PHI mode `off` on synthetic data, in a database no protected mode ever kept: the items'
    bodies are plain, which the search finds (so it can find a text), and no other column of
    any table, the chain included, holds one."""
    folder = marked_agent(tmp_path)
    pack = load_pack(folder)
    db = tmp_path / "local.db"
    with AuditStore(db) as store:
        run = written.run(
            Decision(pack, origin_cap=False),
            resolve_dataset(pack, "written"),
            RUBRIC,
            judge=Judge(pack, RUBRIC, mode="canned"),
        )
        clean("the ledger", run.ledger.dumps().decode())
        clean("the run's repr", repr(run))
        found = written.report(run.ledger)
        clean("the report", json.dumps(found))
        rounds = WrittenStore(store)
        made = rounds.create_round(CLINIC, pack, RUBRIC, run, sample=3, created_by="ops")
        clean("a round's answer", json.dumps(made))
        clean("its progress", json.dumps(rounds.progress(made["round"])))
        clean("the rounds", json.dumps(rounds.rounds(CLINIC)))
    kept = tables(db)
    assert kept["written_items"].count(OUT_MARK) == 3 and kept["written_items"].count(CTX_MARK) == 3
    with sqlite3.connect(db) as connection:
        others = connection.execute(
            "SELECT id, round_id, org, item, sealed, created_at, erased_at FROM written_items"
        ).fetchall()
    clean("an item's other columns", json.dumps(others, default=str))
    for name, text in kept.items():
        if name != "written_items":
            clean(f"table {name}", text)
    with AuditStore(db) as store:
        closed: dict[str, Any] = WrittenStore(store).close(made["round"], by="ops", resamples=50)
        clean("the closing's answer", json.dumps(closed))
    for name, text in tables(db).items():  # closed: erased there too
        clean(f"table {name} after closing", text)
