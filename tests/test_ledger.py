# SPDX-License-Identifier: Apache-2.0
"""The per-case ledger and the mendari.eval/2 result (LP-10): a result is computed only from its
ledger and seed, byte for byte the same on a second run and when recomputed from the parsed
ledger; the ledger holds no input text and no case id; a mendari.eval/1 report still loads; a
stored report is chained; and two runs compare case by case."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from mendari import Decision, Pack, load_pack
from mendari.cli import main
from mendari.core import clock
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
    subgroup_labelled,
)

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
    raw = (
        ROOT
        / "tests"
        / "fixtures"
        / "reference-packs"
        / "patient-inbox-router"
        / "data"
        / "test.jsonl"
    ).read_bytes()
    one = case_hash(case_key(raw), "pub-01800")
    assert one == case_hash(case_key(raw), "pub-01800")
    assert one != case_hash(case_key(raw + b"\n"), "pub-01800")  # another file, another key
    assert one != case_hash(case_key(raw), 1800) != case_hash(case_key(raw), "1800")


def test_two_cases_with_one_id_are_refused_before_any_decision(
    tmp_path: Path, inbox_pack: Pack
) -> None:
    line = json.dumps({"id": "x", "input": {"message": {"body": "Refill please."}}})
    (tmp_path / "twice.jsonl").write_text(f"{line}\n{line}\n")
    decision = Decision(inbox_pack)
    calls: list[int] = []
    decide = decision.decide

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return decide(*args, **kwargs)

    decision.decide = counted  # type: ignore[method-assign]
    with pytest.raises(MendariError, match="another case has the same id"):
        evaluation(decision, load_dataset(tmp_path / "twice.jsonl"))
    assert calls == []  # refused before a provider is asked


def test_a_dataset_without_its_case_key_is_refused(runs: Any, inbox_pack: Pack) -> None:
    _, _, dataset = runs
    with pytest.raises(MendariError, match="no case key"):
        evaluation(Decision(inbox_pack), replace(dataset.head(3), case_key=b""))


@pytest.mark.parametrize(
    ("data", "says"),
    [
        (b"not json\n", "not a ledger"),
        (b'{"format":"other"}\n', "not a mendari.ledger/1"),
        (b'{"format":"mendari.ledger/1","cases":{"n":2}}\n{"c":"a"}\n', "header is not"),
        (b'{"format":"mendari.ledger/1","cases":"n"}\n', "header is not"),
    ],
)
def test_what_is_not_a_ledger_is_refused(data: bytes, says: str) -> None:
    with pytest.raises(MendariError, match=says):
        Ledger.loads(data)


def test_a_ledger_with_a_case_of_another_shape_or_count_is_refused(runs: Any) -> None:
    run, _, _ = runs
    lines = run.ledger.dumps().decode().splitlines()
    with pytest.raises(MendariError, match="a case line is not a ledger case"):
        Ledger.loads("\n".join([lines[0], '{"c":"a"}', *lines[2:]]).encode())
    with pytest.raises(MendariError, match="disagree on how many"):
        Ledger.loads("\n".join(lines[:-1]).encode())


def test_a_ledger_with_a_case_twice_is_refused(runs: Any) -> None:
    """Before, a ledger holding one case's line twice loaded, and `mendari compare` paired the
    case twice: every count and its McNemar p-value were of a doubled case."""
    run, _, _ = runs
    lines = run.ledger.dumps().decode().splitlines()
    header = json.loads(lines[0])
    header["cases"]["n"] = len(lines)  # the count agrees: only the hash repeats
    doubled = [canonical(header), *lines[1:], lines[1]]
    with pytest.raises(MendariError, match="two cases have the same hash"):
        Ledger.loads("\n".join(doubled).encode())


def _with_entry(run: Any, change: Any) -> bytes:
    """The run's ledger with its first answered entry changed by `change(entry)`."""
    lines = run.ledger.dumps().decode().splitlines()
    for number, line in enumerate(lines[1:], start=1):
        case = json.loads(line)
        if case["q"]:
            change(next(iter(case["q"].values())))
            lines[number] = canonical(case)
            break
    return "\n".join(lines).encode()


@pytest.mark.parametrize(
    "change",
    [
        lambda entry: entry.pop(),  # five fields, not six
        lambda entry: entry.__setitem__(1, "not one of its labels"),
        lambda entry: entry.__setitem__(2, 1.5),  # a confidence over 1
        lambda entry: entry.__setitem__(3, [0.5]),  # one probability for several labels
        lambda entry: entry.__setitem__(4, 99),  # a model the header does not list
    ],
)
def test_a_case_entry_a_reader_would_index_wrongly_is_refused(runs: Any, change: Any) -> None:
    """Before, these loaded and failed later: an IndexError or a ValueError in the comparison
    (a strict zip over the probabilities), or a confidence outside every calibration bin that
    still counted in its n."""
    run, _, _ = runs
    with pytest.raises(MendariError, match="a case line is not a ledger case"):
        Ledger.loads(_with_entry(run, change))


def test_a_v1_report_still_loads_everywhere(tmp_path: Path) -> None:
    assert normalize(V1)["run"]["id"] == V1["id"]
    assert leaderboard.row(V1)["report_id"] == V1["id"]
    copy = tmp_path / "patient-inbox-router"
    shutil.copytree(ROOT / "tests" / "fixtures" / "reference-packs" / "patient-inbox-router", copy)
    shutil.copy(ROOT / "tests" / "fixtures" / "eval-v1-inbox.json", copy / "evals/benchmark.json")
    card = render_card(load_pack(copy))  # the card reads a v1 benchmark as it did
    assert f"| Last validation | {clock.local_date(V1['created_at'])} on `test`" in card
    assert f"{V1['latency_ms']['p50']:.2f} / " in card
    # Its sweep in counts: 598 of the 600 scored cases at 0.50 or more, 3 of them wrong.
    assert "| 0.50 | 598 of 600 [98.8-99.96%] | 595 of 598 [98.5-99.9%] |" in card


def test_a_benchmark_of_the_same_version_is_never_called_an_earlier_one(tmp_path: Path) -> None:
    copy = tmp_path / "patient-inbox-router"
    shutil.copytree(ROOT / "tests" / "fixtures" / "reference-packs" / "patient-inbox-router", copy)
    manifest = copy / "pack.yaml"
    changed = manifest.read_text().replace("summary: >-\n", "summary: >-\n  Changed.\n", 1)
    manifest.write_text(changed)
    pack = load_pack(copy)
    card = render_card(pack)  # a new digest, the version unchanged
    assert f"This benchmark was measured on version {pack.version} before its last change" in card
    assert f"earlier version {pack.version}" not in card


def test_a_v1_subgroup_is_k_of_n_only_when_its_labelled_members_are_known() -> None:
    every = {"primary": "q", "questions": {"q": {"n": 40}}, "dataset": {"n": 40}}
    some = {"primary": "q", "questions": {"q": {"n": 30}}, "dataset": {"n": 40}}
    assert subgroup_labelled(every, {"n": 12, "accuracy": 0.5}) == 12
    assert subgroup_labelled(some, {"n": 12, "accuracy": 0.5}) is None  # not "6 of 12"
    assert subgroup_labelled(some, {"n": 12, "labelled": 8, "accuracy": 0.5}) == 8


def test_evaluating_again_keeps_exactly_one_ledger(tmp_path: Path) -> None:
    out = tmp_path / "evals" / "report.json"
    argv = ["eval", "mendari/ai-guard", "--dataset", "test", "--baseline", "none"]
    assert main([*argv, "--limit", "20", "--out", str(out)]) == 0
    assert main([*argv, "--limit", "30", "--out", str(out)]) == 0  # another ledger
    report = json.loads(out.read_text())
    assert [p.name for p in (out.parent / "ledgers").iterdir()] == [
        report["ledger"]["id"].removeprefix("sha256:") + ".jsonl"
    ]


def test_an_out_that_is_not_a_file_gets_the_report_and_no_ledger(
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ["eval", "mendari/ai-guard", "--dataset", "test", "--limit", "5", "--baseline", "none"]
    assert main([*argv, "--out", os.devnull]) == 0
    assert "no ledger is written beside it" in capsys.readouterr().err


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
    assert "different" in words and "k of n [exact 95% interval, Clopper-Pearson]" in words
    assert "1.0000" not in words  # bounds as percentages, never raw fractions


@pytest.mark.parametrize(
    ("flips", "alpha", "evidence"),
    [(5, 0.05, "insufficient"), (6, 0.05, "sufficient"), (7, 0.01, "insufficient"),
     (8, 0.01, "sufficient"), (5, 0.1, "sufficient")],
)  # fmt: skip
def test_the_evidence_floor_follows_alpha(
    runs: Any, flips: int, alpha: float, evidence: str
) -> None:
    run, _, dataset = runs
    result = paired.compare(
        run.ledger, _flipped(run.ledger, flips), dataset_sha256=dataset.sha256, alpha=alpha
    )
    accuracy = next(f for f in result["figures"] if f["figure"] == "accuracy")
    assert accuracy["evidence"] == evidence  # the smallest p that many cases give, vs alpha


def test_runs_on_another_dataset_or_pack_are_refused(runs: Any) -> None:
    run, _, dataset = runs
    with pytest.raises(MendariError, match="A was not run on this dataset"):
        paired.compare(run.ledger, run.ledger, dataset_sha256="0" * 64)
    other = Ledger({**run.ledger.header, "pack": {"id": "x/y"}}, run.ledger.cases)
    with pytest.raises(MendariError, match="different packs"):
        paired.compare(run.ledger, other, dataset_sha256=dataset.sha256)
