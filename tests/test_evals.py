# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from mendari import Decision, Pack
from mendari.core.card import benchmark, render_card
from mendari.errors import MendariError
from mendari.evals import (
    compare,
    evaluate,
    load_dataset,
    metrics,
    resolve_dataset,
    run_pack_tests,
)
from tests.conftest import LOCAL_DATASET


def test_classification_metrics() -> None:
    result = metrics.classification(["a", "a", "b", "b"], ["a", "b", "b", None], ["a", "b"])
    assert result["accuracy"] == 0.5
    assert result["per_label"]["a"] == {
        "precision": 1.0,
        "recall": 0.5,
        "f1": pytest.approx(2 / 3),
        "support": 2,
    }
    assert result["confusion"]["b"]["none"] == 1  # a failed answer counts as wrong


def test_expected_calibration_error() -> None:
    # Two bins: 0.9 confidence with 50% accuracy, 0.6 confidence with 100% accuracy.
    calibration = metrics.calibration([0.9, 0.9, 0.6, 0.6], [True, False, True, True])
    assert calibration["ece"] == pytest.approx(0.5 * 0.4 + 0.5 * 0.4)
    assert [b["n"] for b in calibration["bins"]] == [2, 2]


def test_brier_score() -> None:
    assert metrics.brier([{"a": 1.0, "b": 0.0}], ["a"]) == 0.0
    assert metrics.brier([{"a": 0.0, "b": 1.0}], ["a"]) == 2.0


def test_threshold_sweep() -> None:
    rows = metrics.threshold_sweep([0.95, 0.8, 0.6], [True, False, True], thresholds=(0.5, 0.9))
    assert rows[0] == {
        "threshold": 0.5,
        "coverage": 1.0,
        "accuracy": pytest.approx(2 / 3),
        "errors": 1,
        "chosen": 3,
        "scored": 3,
    }
    assert rows[1] == {
        "threshold": 0.9,
        "coverage": pytest.approx(1 / 3),
        "accuracy": 1.0,
        "errors": 0,
        "chosen": 1,
        "scored": 3,
    }


def test_dataset_loading_errors(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"input": {}}\n{"labels": {}}\n')
    with pytest.raises(MendariError, match=r"bad\.jsonl:2"):
        load_dataset(bad)


def test_benchmark_report_shape(inbox_pack: Pack, screened: dict[str, str]) -> None:
    """With the deployed screen (a fake Jev that finds nothing urgent), most cases are automated:
    an emergency the screen misses must still never be."""
    dataset = resolve_dataset(inbox_pack, "calibration")
    report = evaluate(Decision(inbox_pack, providers=screened), dataset)
    assert report["dataset"]["n"] == len(dataset.records) == 300
    assert set(report["questions"]) == {"destination", "urgency"}
    decisions = report["decisions"]
    assert sum(decisions["outcome_counts"].values()) == 300
    assert decisions["outcome_counts"]["AUTO"] > 250
    assert decisions["critical"][0]["auto_missed"] == 0
    assert set(report["subgroups"]) == {"language", "channel", "age_band", "topic"}
    json.dumps(report, allow_nan=False)  # reports must be strict JSON


def test_reports_never_contain_case_text(router: Decision) -> None:
    dataset = resolve_dataset(router.pack, "calibration")
    serialized = json.dumps(evaluate(router, dataset))
    assert dataset.records[0]["input"]["message"]["body"] not in serialized


def test_local_validation_localises_degradation(inbox_pack: Pack, screened: dict[str, str]) -> None:
    baseline = benchmark(inbox_pack)
    assert baseline is not None
    local = evaluate(Decision(inbox_pack, providers=screened), load_dataset(LOCAL_DATASET))
    comparison = compare(local, baseline, inbox_pack.manifest.evaluation.degradation_tolerance)
    assert comparison["status"] == "degraded"
    assert comparison["delta"] < -0.03
    subjects = {finding["subject"] for finding in comparison["findings"]}
    assert "language=es" in subjects
    # Emergencies stay protected even where routing degrades and the model misses them.
    assert local["decisions"]["outcome_counts"]["AUTO"] > 300
    assert local["decisions"]["critical"][0]["auto_missed"] == 0


def test_comparison_with_itself_is_clean(inbox_pack: Pack) -> None:
    baseline = benchmark(inbox_pack)
    assert baseline is not None
    comparison = compare(baseline, baseline, 0.03)
    assert comparison["status"] == "ok" and comparison["findings"] == []


def test_a_subgroup_the_benchmark_lacks_is_compared_with_its_overall_figure(
    inbox_pack: Pack,
) -> None:
    """Never the whole benchmark's count shown as if it were the subgroup's."""
    from mendari.evals.render import comparison_text

    baseline = benchmark(inbox_pack)
    assert baseline is not None
    local = copy.deepcopy(baseline)
    group = {"n": 40, "labelled": 40, "accuracy": 0.5, "auto_rate": 0.0, "abstention_rate": 0.0}
    local["subgroups"]["language"]["xx"] = {**group, "false_automation_rate": None}
    comparison = compare(local, baseline, 0.03)
    (finding,) = [f for f in comparison["findings"] if f["subject"] == "language=xx"]
    assert finding["baseline_scope"] == "overall" and finding["local_n"] == 40
    primary = baseline["questions"][baseline["primary"]]
    line = next(x for x in comparison_text(comparison).splitlines() if "language=xx" in x)
    assert line.startswith(f"  - language=xx: benchmark overall {round(primary['accuracy'] * 600)}")
    assert line.endswith("-> 20 of 40 [33.8-66.2%]")


def test_a_baseline_without_its_counts_is_compared_with_n_a(inbox_pack: Pack) -> None:
    """The counts each figure is of are read from a published benchmark only where they are."""
    from mendari.evals.render import comparison_text

    baseline = benchmark(inbox_pack)
    assert baseline is not None
    baseline["decisions"]["automation"]["false_automation_rate"] = 0.0
    local = copy.deepcopy(baseline)
    local["decisions"]["automation"].update(false_automation_rate=0.5, n=10)
    label, figures = next(iter(local["questions"][local["primary"]]["per_label"].items()))
    figures.update(recall=0.0, support=50)
    question = baseline["questions"][baseline["primary"]]
    del question["n"], question["per_label"][label]["support"]
    del baseline["decisions"]["automation"]["n"]
    comparison = compare(local, baseline, 0.03)
    kinds = {f["kind"] for f in comparison["findings"]}
    assert {"label_recall", "false_automation"} <= kinds
    assert "false automation rate (of automated): n/a -> 5 of 10" in comparison_text(comparison)


@pytest.mark.parametrize(
    "spoil",
    [
        lambda b: b["questions"][b["primary"]].update(accuracy=None),
        lambda b: b["questions"][b["primary"]].update(per_label=[1]),
        lambda b: b["questions"][b["primary"]].update(calibration={"ece": "low"}),
        lambda b: b.update(subgroups={"language": [1]}),
        lambda b: b.update(decisions={"automation": {"false_automation_rate": "x"}}),
    ],
    ids=["accuracy", "per-label", "calibration", "subgroups", "automation"],
)
def test_a_baseline_with_malformed_figures_cannot_be_compared(
    inbox_pack: Pack, spoil: Callable[[dict[str, Any]], None]
) -> None:
    """A published benchmark is the pack author's file: one whose accuracy was null passed the
    card's checks and made POST .../evals answer 500 (TypeError in compare)."""
    baseline = benchmark(inbox_pack)
    assert baseline is not None
    local = copy.deepcopy(baseline)
    spoil(baseline)
    comparison = compare(local, baseline, 0.03)
    assert comparison["status"] == "unknown"
    assert "published benchmark" in comparison["reason"] and comparison["findings"] == []


def test_pack_behaviour_tests_pass(router: Decision) -> None:
    outcomes = run_pack_tests(router)
    assert outcomes and all(o.passed for o in outcomes), [o for o in outcomes if not o.passed]


def test_decision_card_renders(inbox_pack: Pack) -> None:
    card = render_card(inbox_pack)
    for heading in (
        "## Intended use",
        "## Decision policy",
        "## Validation",
        "## Known failure modes",
    ):
        assert heading in card
    assert inbox_pack.digest in card


def test_parallel_evaluation_matches_sequential(inbox_pack: Pack, screened: dict[str, str]) -> None:
    router = Decision(inbox_pack, providers=screened)  # remote: evaluated in parallel
    dataset = resolve_dataset(router.pack, "calibration").head(60)
    sequential = evaluate(router, dataset)
    parallel = evaluate(router, dataset, workers=4)
    for key in ("questions", "decisions", "subgroups", "misclassified"):
        assert parallel[key] == sequential[key]
    assert parallel["dataset"] == {**sequential["dataset"], "limit": 60} == sequential["dataset"]
