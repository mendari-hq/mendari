# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari import Agent, Decision, load_agent
from mendari.cli import main
from mendari.core.card import render_card
from mendari.evals import evaluate, leaderboard, resolve_dataset
from mendari.server.app import create_app
from tests.conftest import TRIAGE_DIR

AGENT = "/api/agents/mendari-test/triage"


def report(agent: Agent, dataset: str, **providers: str) -> dict[str, Any]:
    return evaluate(Decision(agent, providers=providers), resolve_dataset(agent, dataset))


def test_labels_name_remote_models_only() -> None:
    assert (
        leaderboard.config_label(
            {
                "screen": {
                    "variant": "checked",
                    "type": "check",
                    "model": "agent:triage-screen + second:rules@5304a41e46f5",
                },
                "router": {"variant": "llm", "type": "llm", "model": "qwen3:8b"},
            }
        )
        == "screen=checked (triage-screen) · router=llm (qwen3:8b)"
    )
    assert (
        leaderboard.config_label(
            {"router": {"variant": "rules", "type": "rules", "model": "rules@76b3e340d149"}}
        )
        == "router=rules"
    )


def test_rows_merge_by_configuration_and_dataset(triage_agent: Agent) -> None:
    rules = report(triage_agent, "critical", router="rules")
    canned = report(triage_agent, "critical", router="agent")
    board = leaderboard.build(triage_agent, [rules, canned])
    assert [r["config"] for r in board["rows"]] == [
        "router=agent (triage-router) · screen=rules",
        "router=rules · screen=rules",
    ]
    row = board["rows"][0]
    assert row["dataset"] == "critical" and row["n"] == 40
    assert row["critical"][0]["question"] == "urgency" and row["critical"][0]["n"] == 40
    assert "input" not in json.dumps(board)  # no case text

    again = leaderboard.build(
        triage_agent, [report(triage_agent, "critical", router="rules")], board
    )
    assert len(again["rows"]) == 2  # replaced, not duplicated
    assert again["rows"][1]["report_id"] != board["rows"][1]["report_id"]

    other = dict(rules, agent={**rules["agent"], "id": "someone/else"})
    with pytest.raises(ValueError, match="someone/else"):
        leaderboard.build(triage_agent, [other])


def test_markdown_and_card(triage_agent: Agent, tmp_path: Path) -> None:
    board = leaderboard.build(triage_agent, [report(triage_agent, "critical")])
    table = "\n".join(leaderboard.markdown(board))
    assert "**`critical`** (n=40; k of n [exact 95% interval" in table
    # 40 same-day positives: shown with the evidence rule beside them, and no rank (LP-17g).
    assert "| Configuration | Critical detected |" in table and "| Rank" not in table
    assert "\n| router=rules · screen=rules | 16 of 40 [" in table
    assert "(0 automated; insufficient evidence)" in table

    root = tmp_path / "agent"
    shutil.copytree(TRIAGE_DIR, root)
    (root / "evals").mkdir()
    (root / leaderboard.PATH).write_text(json.dumps(board))
    card = render_card(load_agent(root))
    assert "## Provider comparison" in card and "critical" in card
    assert "Public set: regression check, not a ranking." in card
    assert load_agent(root).digest == load_agent(TRIAGE_DIR).digest  # evals/ is not the agent


def test_other_questions_are_shown_where_configurations_differ(triage_agent: Agent) -> None:
    rules = report(triage_agent, "critical", router="rules")
    canned = report(triage_agent, "critical", router="agent")
    board = leaderboard.build(triage_agent, [rules, canned])
    assert board["rows"][0]["questions"]["urgency"]["n"] == 40
    # Both use the same screen: urgency and abuse are identical, so no second table.
    assert "Other questions" not in "\n".join(leaderboard.markdown(board))

    screened = json.loads(json.dumps(canned))
    screened["id"] = "other-screen"
    screened["providers"]["screen"] = {"variant": "llm", "type": "llm", "model": "llm-x"}
    screened["questions"]["urgency"]["accuracy"] = 0.5
    table = "\n".join(
        leaderboard.markdown(leaderboard.build(triage_agent, [rules, canned, screened]))
    )
    assert "| Configuration | `urgency` |" in table
    assert "| router=agent (triage-router) · screen=llm (llm-x) | 20 of 40 [" in table
    assert "`destination`" not in table.split("Other questions")[1]  # the primary has its own


def test_several_critical_labels_are_named() -> None:
    critical = [
        {"question": "urgency", "label": "yes", "n": 5, "detected": 5, "auto_missed": 0},
        {"question": "abusive", "label": "yes", "n": 0, "detected": 0, "auto_missed": 0},
    ]
    assert leaderboard.critical_text({"critical": critical}) == (
        "urgency=yes 5 of 5 [47.8-100.0%] (0 automated; insufficient evidence)"
    )
    assert leaderboard.critical_text({"critical": critical[:1]}) == (
        "5 of 5 [47.8-100.0%] (0 automated; insufficient evidence)"
    )


def test_cli_runs_configurations_and_merges(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "leaderboard.json"
    base = ["leaderboard", str(TRIAGE_DIR), "--out", str(out)]
    assert (
        main([*base, "--run", "router=rules", "--run", "router=agent", "--dataset", "critical"])
        == 0
    )
    assert len(json.loads(out.read_text())["rows"]) == 2
    assert main([*base, "--run", "router=rules", "--dataset", "test", "--limit", "20"]) == 0
    board = json.loads(out.read_text())
    assert {(r["dataset"], r["n"]) for r in board["rows"]} == {("critical", 40), ("test", 20)}
    saved = tmp_path / "report.json"
    saved.write_text(json.dumps(report(load_agent(TRIAGE_DIR), "examples")))
    assert main([*base, "--report", str(saved)]) == 0
    assert len(json.loads(out.read_text())["rows"]) == 4
    assert "examples" in capsys.readouterr().out


def test_the_cli_refuses_a_run_with_no_dataset_to_run_it_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--run` evaluates a configuration on each `--dataset`: with none it ran nothing, and
    beside a `--report` it said nothing and exited 0, the board without the configuration's
    row. Refused before anything is read or written."""
    refused = "--run evaluates a configuration on each --dataset: name at least one"
    out = tmp_path / "leaderboard.json"
    saved = tmp_path / "report.json"
    saved.write_text(json.dumps(report(load_agent(TRIAGE_DIR), "examples")))
    base = ["leaderboard", str(TRIAGE_DIR), "--out", str(out), "--run", "router=agent"]
    for more in ([], ["--report", str(saved)]):
        assert main([*base, *more]) == 1, more
        out_text, err = capsys.readouterr()
        assert out_text == "", more
        assert err == f"error: {refused}\n", more
        assert not out.exists(), more


def test_the_cli_refuses_what_only_a_run_reads_without_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--dataset`, `--limit` and `--workers` are a `--run`'s: beside a `--report` alone they
    were ignored and the command exited 0. Refused before anything is read or written."""
    out = tmp_path / "leaderboard.json"
    saved = tmp_path / "report.json"
    saved.write_text(json.dumps(report(load_agent(TRIAGE_DIR), "examples")))
    base = ["leaderboard", str(TRIAGE_DIR), "--out", str(out), "--report", str(saved)]
    for option in (["--dataset", "examples"], ["--limit", "3"], ["--workers", "2"]):
        assert main([*base, *option]) == 1, option
        out_text, err = capsys.readouterr()
        assert out_text == "" and err == f"error: {option[0]} is for --run: add one\n", option
        assert not out.exists(), option


def test_the_cli_says_what_its_tables_are_as_the_card_does(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`mendari leaderboard` prints the card's opening above its tables (LP-17g): "public set:
    regression check, not a ranking" and the order the rows are listed in. It calls only the
    agent's own sets public: a board can hold a site's own file, here a copy of one under another
    name, which is in neither the repository nor the package."""
    site = tmp_path / "our-own-cases.jsonl"
    shutil.copyfile(TRIAGE_DIR / "data" / "critical.jsonl", site)
    out = tmp_path / "leaderboard.json"
    args = ["leaderboard", str(TRIAGE_DIR), "--out", str(out), "--run", "router=rules"]
    assert main([*args, "--dataset", str(site)]) == 0
    printed = capsys.readouterr().out
    assert f"{leaderboard.preface()}\n\n**`our-own-cases`**" in printed  # above its tables
    assert "Public set: regression check, not a ranking." in printed
    assert "every other row follows, by configuration name" in printed
    assert "the agent's own sets are public" in printed
    assert "Every set" not in printed  # never that every set it shows is public


def test_deployments_evaluate_other_configurations(db_target: str) -> None:
    client = TestClient(create_app(db_path=db_target))
    response = client.post(
        f"{AGENT}/evals", json={"dataset": "critical", "providers": {"router": "agent"}}
    )
    assert response.status_code == 200
    assert response.json()["providers"]["router"]["variant"] == "agent"
    bad = client.post(f"{AGENT}/evals", json={"dataset": "critical", "providers": {"x": "y"}})
    assert bad.status_code == 422
    assert "leaderboard" in client.get(AGENT).json()
