# SPDX-License-Identifier: Apache-2.0
"""The per-case ledger and the mendari.eval/2 result (LP-10): a result is computed only from its
ledger and seed, byte for byte the same on a second run and when recomputed from the parsed
ledger; the ledger holds no input text and no case id; a mendari.eval/1 report still loads; a
stored report is chained; and two runs compare case by case."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from mendari import Decision, Pack
from mendari.cli import main
from mendari.core.audit import AuditStore
from mendari.core.card import render_card
from mendari.errors import MendariError
from mendari.evals import leaderboard, paired
from mendari.evals.harness import evaluation, load_dataset, resolve_dataset
from mendari.evals.ledger import Ledger, canonical, case_hash, case_key
from mendari.evals.result import (
    FORMAT,
    PREDICTED,
    normalize,
    result_from_ledger,
    result_of,
    result_sha256,
)
from mendari.evals.share import site_report

ROOT = Path(__file__).resolve().parents[1]
V1 = json.loads((ROOT / "tests" / "fixtures" / "eval-v1-inbox.json").read_text())


@pytest.fixture(scope="module")
def runs() -> tuple[Any, Any, Any]:
    from mendari.core.registry import resolve_pack

    pack = resolve_pack("mendari/patient-inbox-router")
    dataset = resolve_dataset(pack, "test")
    return evaluation(Decision(pack), dataset), evaluation(Decision(pack), dataset), dataset


def test_a_second_run_gives_byte_identical_results_and_ledgers(runs: Any) -> None:
    first, second, _ = runs
    assert canonical(result_of(first.report)) == canonical(result_of(second.report))
    assert first.ledger.dumps() == second.ledger.dumps()
    assert first.report["run"]["id"] != second.report["run"]["id"]  # the run differs, apart


def test_the_result_is_recomputed_from_the_parsed_ledger(runs: Any) -> None:
    run, _, _ = runs
    parsed = Ledger.loads(run.ledger.dumps())
    assert parsed.id == run.ledger.id == run.report["ledger"]["id"]
    again = result_from_ledger(parsed)
    assert canonical(again) == canonical(result_of(run.report))
    assert result_sha256(again) == run.report["result_sha256"]
    assert run.report["format"] == FORMAT


def test_every_figure_of_the_v1_report_is_kept(runs: Any) -> None:
    """The committed mendari.eval/1 benchmark, recomputed from a ledger: the same figures (case
    ids aside, which are hashes now), to rounding of the probabilities to 6 places."""
    run, _, _ = runs

    def same(old: Any, new: Any, where: str) -> None:
        if isinstance(old, dict):
            for key, value in old.items():
                if key not in ("missed_ids",):
                    same(value, new[key], f"{where}.{key}")
        elif isinstance(old, list):
            assert len(old) == len(new), where
            for i, (a, b) in enumerate(zip(old, new, strict=True)):
                same(a, b, f"{where}[{i}]")
        elif isinstance(old, float):
            assert new == pytest.approx(old, abs=1e-6), where
        else:
            assert old == new, where

    for key in ("questions", "decisions", "threshold_sweep", "cost", "errors", "subgroups"):
        same(V1[key], run.report[key], key)


def test_proportions_carry_their_exact_intervals(runs: Any) -> None:
    run, _, _ = runs
    report = run.report
    accuracy = report["proportions"]["accuracy"]
    question = report["questions"][report["primary"]]
    assert accuracy == question["proportion"]
    assert accuracy["n"] == question["n"] and accuracy["k"] / accuracy["n"] == question["accuracy"]
    assert accuracy["low"] < question["accuracy"] < accuracy["high"]
    (critical,) = report["decisions"]["critical"]
    assert report["proportions"]["critical"]["urgency=urgent"] == critical["proportion"]
    assert critical["proportion"]["k"] == critical["detected"]
    assert (
        question["brier_interval"]["low"] <= question["brier"] <= question["brier_interval"]["high"]
    )
    assert question["calibration"]["equal_mass"]["status"] == "ok"
    assert report["seeds"] == {"bootstrap": 0, "resamples": 2000, "level": 0.95}


def test_the_ledger_holds_no_case_text_and_no_case_id(runs: Any) -> None:
    run, _, dataset = runs
    text = run.ledger.dumps().decode()
    report = json.dumps(run.report)
    for record in dataset.records:
        assert f'"{record["id"]}"' not in text and f'"{record["id"]}"' not in report
    body = dataset.records[0]["input"]["message"]["body"]
    assert body not in text and body not in report
    missed = [c["case"] for c in run.report["misclassified"]]
    assert missed and all(len(case) == 32 for case in missed)


def test_a_case_hash_is_keyed_by_the_datasets_own_bytes(tmp_path: Path) -> None:
    raw = (ROOT / "packs" / "patient-inbox-router" / "data" / "test.jsonl").read_bytes()
    one = case_hash(case_key(raw), "pub-01800")
    assert one == case_hash(case_key(raw), "pub-01800")
    assert one != case_hash(case_key(raw + b"\n"), "pub-01800")  # another file, another key
    assert one != case_hash(case_key(raw), 1800) != case_hash(case_key(raw), "1800")


def test_two_cases_with_one_id_are_refused(tmp_path: Path, inbox_pack: Pack) -> None:
    line = json.dumps({"id": "x", "input": {"message": {"body": "Refill please."}}})
    (tmp_path / "twice.jsonl").write_text(f"{line}\n{line}\n")
    with pytest.raises(MendariError, match="two cases have the same id"):
        evaluation(Decision(inbox_pack), load_dataset(tmp_path / "twice.jsonl"))


@pytest.mark.parametrize(
    ("data", "says"),
    [
        (b"not json\n", "not a ledger"),
        (b'{"format":"other"}\n', "not a mendari.ledger/1"),
        (b'{"format":"mendari.ledger/1","cases":{"n":2}}\n{"c":"a"}\n', "disagree"),
    ],
)
def test_what_is_not_a_ledger_is_refused(data: bytes, says: str) -> None:
    with pytest.raises(MendariError, match=says):
        Ledger.loads(data)


def test_a_v1_report_still_loads_everywhere(tmp_path: Path, inbox_pack: Pack) -> None:
    assert normalize(V1)["run"]["id"] == V1["id"]
    assert leaderboard.row(V1)["report_id"] == V1["id"]
    assert site_report(V1).questions  # shareable as before
    assert "Last validation" in render_card(inbox_pack)  # the card reads either format


def test_a_stored_report_keeps_its_ledger_and_is_chained(tmp_path: Path) -> None:
    db = tmp_path / "audit.db"
    out = tmp_path / "evals" / "report.json"
    argv = ["eval", "mendari/patient-inbox-router", "--dataset", "red_flags_holdout"]
    assert main([*argv, "--baseline", "none", "--db", str(db), "--out", str(out)]) == 0
    report = json.loads(out.read_text())
    ledger_file = out.parent / "ledgers" / (report["ledger"]["id"][7:] + ".jsonl")
    assert Ledger.loads(ledger_file.read_bytes()).id == report["ledger"]["id"]
    with closing(sqlite3.connect(db)) as connection:
        stored = connection.execute("SELECT ledger FROM eval_reports").fetchone()[0]
        (event,) = connection.execute(
            "SELECT decision_id, payload FROM audit_events WHERE event = 'eval.recorded'"
        ).fetchall()
    assert stored.encode() == ledger_file.read_bytes()
    assert event[0] == f"eval:{report['run']['id']}"
    assert json.loads(event[1]) == {
        "report": report["run"]["id"],
        "result_sha256": report["result_sha256"],
        "ledger": report["ledger"]["id"],
        "pack_id": "mendari/patient-inbox-router",
        "pack_digest": report["pack"]["digest"],
        "dataset_sha256": report["dataset"]["sha256"],
        "org": "default",
    }
    with AuditStore(db, read_only=True) as store:
        assert store.verify()["ok"]


def _flipped(ledger: Ledger, cases: int) -> Ledger:
    """B: A with the primary answer of its first `cases` labelled cases made wrong."""
    header, rows = deepcopy(ledger.header), deepcopy(ledger.cases)
    primary = header["primary"]
    labels = header["questions"][primary]
    changed = 0
    for row in rows:
        entry = row["q"].get(primary)
        if changed < cases and entry and entry[0] is not None and entry[PREDICTED] == entry[0]:
            wrong = next(label for label in labels if label != entry[0])
            entry[PREDICTED] = wrong
            entry[3] = [1.0 if label == wrong else 0.0 for label in labels]  # sure, and wrong
            changed += 1
    return Ledger(header, rows)


def test_a_paired_comparison_counts_the_cases_that_changed(runs: Any) -> None:
    run, _, dataset = runs
    worse = _flipped(run.ledger, 12)
    result = paired.compare(run.ledger, worse, dataset_sha256=dataset.sha256, seed=3)
    accuracy = next(f for f in result["figures"] if f["figure"] == "accuracy")
    assert (accuracy["only_a"], accuracy["only_b"]) == (12, 0)
    assert accuracy["a"]["k"] - accuracy["b"]["k"] == 12
    assert accuracy["p"] == pytest.approx(2 * 0.5**12) and accuracy["different"] is True
    assert result["holm_family"] == [f["figure"] for f in result["figures"]]
    assert len(result["changed"]) == 12 and result["dataset"]["paired"] == 600
    assert result["brier"]["a_minus_b"] < 0  # A is better calibrated than the made-worse B
    words = paired.text(
        result, {case_hash(dataset.case_key, r["id"]): r["id"] for r in dataset.records}
    )
    assert "accuracy: A 596 of 600" in words and "pub-" in words


def test_runs_on_another_dataset_or_pack_are_refused(runs: Any) -> None:
    run, _, dataset = runs
    with pytest.raises(MendariError, match="A was not run on this dataset"):
        paired.compare(run.ledger, run.ledger, dataset_sha256="0" * 64)
    other = Ledger({**run.ledger.header, "pack": {"id": "x/y"}}, run.ledger.cases)
    with pytest.raises(MendariError, match="different packs"):
        paired.compare(run.ledger, other, dataset_sha256=dataset.sha256)
