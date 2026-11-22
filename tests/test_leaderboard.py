# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari import Decision, Pack, load_pack
from mendari.cli import main
from mendari.core.card import render_card
from mendari.evals import evaluate, leaderboard, resolve_dataset
from mendari.server.app import create_app
from tests.conftest import INBOX_DIR

PACK = "/api/packs/mendari/patient-inbox-router"


def report(pack: Pack, dataset: str, **providers: str) -> dict[str, Any]:
    return evaluate(Decision(pack, providers=providers), resolve_dataset(pack, dataset))


def test_labels_name_remote_models_only() -> None:
    assert (
        leaderboard.config_label(
            {
                "safety_screen": {
                    "variant": "rules_and_jev",
                    "type": "any_of",
                    "model": "rules:rules@6614 + jev:jev-1.13.0",
                },
                "router": {"variant": "llm", "type": "llm", "model": "qwen3:8b"},
            }
        )
        == "safety_screen=rules_and_jev (jev-1.13.0) · router=llm (qwen3:8b)"
    )
    assert (
        leaderboard.config_label(
            {"router": {"variant": "naive_bayes", "type": "naive_bayes", "model": "naive_bayes@6b"}}
        )
        == "router=naive_bayes"
    )


def test_rows_merge_by_configuration_and_dataset(inbox_pack: Pack) -> None:
    rules = report(inbox_pack, "red_flags", router="rules")
    nb = report(inbox_pack, "red_flags", router="naive_bayes")
    board = leaderboard.build(inbox_pack, [rules, nb])
    assert [r["config"] for r in board["rows"]] == [
        "safety_screen=rules · router=naive_bayes",
        "safety_screen=rules · router=rules",
    ]
    row = board["rows"][0]
    assert row["dataset"] == "red_flags" and row["n"] == 38
    assert row["critical"][0]["question"] == "urgency" and row["critical"][0]["n"] == 28
    assert "input" not in json.dumps(board)  # no case text

    again = leaderboard.build(inbox_pack, [report(inbox_pack, "red_flags", router="rules")], board)
    assert len(again["rows"]) == 2  # replaced, not duplicated
    assert again["rows"][1]["report_id"] != board["rows"][1]["report_id"]

    other = dict(rules, pack={**rules["pack"], "id": "someone/else"})
    with pytest.raises(ValueError, match="someone/else"):
        leaderboard.build(inbox_pack, [other])


def test_markdown_and_card(inbox_pack: Pack, tmp_path: Path) -> None:
    board = leaderboard.build(inbox_pack, [report(inbox_pack, "red_flags_holdout")])
    table = "\n".join(leaderboard.markdown(board))
    assert "**`red_flags_holdout`** (n=30; k of n [exact 95% interval" in table
    # 20 urgent positives: shown with the evidence rule beside them, and no rank (LP-17g).
    assert "| Configuration | Critical detected |" in table and "| Rank" not in table
    assert "\n| safety_screen=rules · router=naive_bayes | 4 of 20 [" in table
    assert "(0 automated; insufficient evidence)" in table

    root = tmp_path / "pack"
    shutil.copytree(INBOX_DIR, root)
    (root / leaderboard.PATH).write_text(json.dumps(board))
    card = render_card(load_pack(root))
    assert "## Provider comparison" in card and "red_flags_holdout" in card
    assert "Public set: regression check, not a ranking." in card
    assert load_pack(root).digest == load_pack(INBOX_DIR).digest  # evals/ is not the pack


def test_other_questions_are_shown_where_configurations_differ(inbox_pack: Pack) -> None:
    rules = report(inbox_pack, "red_flags", router="rules")
    nb = report(inbox_pack, "red_flags", router="naive_bayes")
    board = leaderboard.build(inbox_pack, [rules, nb])
    assert board["rows"][0]["questions"]["urgency"]["n"] == 38
    # Both use the same safety screen: urgency is identical, so no second table.
    assert "Other questions" not in "\n".join(leaderboard.markdown(board))

    screened = json.loads(json.dumps(nb))
    screened["id"] = "other-screen"
    screened["providers"]["safety_screen"] = {"variant": "jev", "type": "jev", "model": "jev-x"}
    screened["questions"]["urgency"]["accuracy"] = 0.5
    table = "\n".join(leaderboard.markdown(leaderboard.build(inbox_pack, [rules, nb, screened])))
    assert "| Configuration | `urgency` |" in table
    assert "| safety_screen=jev (jev-x) · router=naive_bayes | 19 of 38 [" in table
    assert "`destination`" not in table.split("Other questions")[1]  # the primary has its own


def test_several_critical_labels_are_named() -> None:
    critical = [
        {"question": "note_pregnancy", "label": "yes", "n": 5, "detected": 5, "auto_missed": 0},
        {"question": "egfr_30_or_more", "label": "no", "n": 0, "detected": 0, "auto_missed": 0},
    ]
    assert leaderboard.critical_text({"critical": critical}) == (
        "note_pregnancy=yes 5 of 5 [47.8-100.0%] (0 automated; insufficient evidence)"
    )
    assert leaderboard.critical_text({"critical": critical[:1]}) == (
        "5 of 5 [47.8-100.0%] (0 automated; insufficient evidence)"
    )


def test_cli_runs_configurations_and_merges(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "leaderboard.json"
    base = ["leaderboard", str(INBOX_DIR), "--out", str(out)]
    assert (
        main(
            [
                *base,
                "--run",
                "router=rules",
                "--run",
                "router=naive_bayes",
                "--dataset",
                "red_flags",
            ]
        )
        == 0
    )
    assert len(json.loads(out.read_text())["rows"]) == 2
    assert main([*base, "--run", "router=rules", "--dataset", "test", "--limit", "20"]) == 0
    board = json.loads(out.read_text())
    assert {(r["dataset"], r["n"]) for r in board["rows"]} == {("red_flags", 38), ("test", 20)}
    saved = tmp_path / "report.json"
    saved.write_text(json.dumps(report(load_pack(INBOX_DIR), "red_flags_holdout")))
    assert main([*base, "--report", str(saved)]) == 0
    assert len(json.loads(out.read_text())["rows"]) == 4
    assert "red_flags_holdout" in capsys.readouterr().out


def test_the_cli_says_what_its_tables_are_as_the_card_does(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`mendari leaderboard` prints the card's opening above its tables (LP-17g): "public set:
    regression check, not a ranking", the order the rows are listed in, and the scoreboard's
    link. It calls only the pack's own sets public: a board can hold a site's own file, here a
    copy of one under another name, which is in neither the repository nor the package."""
    site = tmp_path / "our-own-cases.jsonl"
    shutil.copyfile(INBOX_DIR / "data" / "red_flags.jsonl", site)
    out = tmp_path / "leaderboard.json"
    args = ["leaderboard", str(INBOX_DIR), "--out", str(out), "--run", "router=rules"]
    assert main([*args, "--dataset", str(site)]) == 0
    printed = capsys.readouterr().out
    assert f"{leaderboard.preface()}\n\n**`our-own-cases`**" in printed  # above its tables
    assert "Public set: regression check, not a ranking." in printed
    assert "every other row follows, by configuration name" in printed
    assert "the pack's own sets are public" in printed
    assert "Every set" not in printed  # never that every set it shows is public


def test_deployments_evaluate_other_configurations(db_target: str) -> None:
    client = TestClient(create_app(db_path=db_target))
    response = client.post(
        f"{PACK}/evals", json={"dataset": "red_flags", "providers": {"router": "rules"}}
    )
    assert response.status_code == 200
    assert response.json()["providers"]["router"]["variant"] == "rules"
    bad = client.post(f"{PACK}/evals", json={"dataset": "red_flags", "providers": {"x": "y"}})
    assert bad.status_code == 422
    assert "leaderboard" in client.get(PACK).json()
