# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from mendari import Agent, Decision, Outcome
from mendari.core.card import render_card
from mendari.errors import MendariError
from mendari.evals import (
    compare,
    evaluate,
    load_dataset,
    metrics,
    resolve_dataset,
    run_behaviour_tests,
)
from tests.conftest import calm_agent
from tests.support.engine import benchmark

DRUGS = ("metformin", "lisinopril", "atorvastatin", "levothyroxine", "amlodipine", "sertraline")


def local_site(base: Path, triage: Agent) -> Path:
    """A site's own cases for the triage fixture (synthetic): its `test` set, and 60 text
    messages asking whether a medicine is ready, in words its keyword rules cannot place, so
    the site's routing degrades on `sms`."""
    records = [dict(record) for record in resolve_dataset(triage, "test").records]
    for i in range(60):
        body = f"Is my {DRUGS[i % len(DRUGS)]} ready to collect at the counter? ({i})"
        records.append(
            {
                "id": f"local-{i + 1:03d}",
                "input": {"message": {"body": body, "channel": "sms"}},
                "labels": {"destination": "pharmacy", "urgency": "no", "abusive": "no"},
                "attributes": {"source": "synthetic", "channel": "sms"},
            }
        )
    path = base / "local-site.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


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


def test_benchmark_report_shape(triage_agent: Agent) -> None:
    """With the screen's canned agent, which the policy lets automate, about half the cases are
    automated. Its canned answers miss some same-day symptoms, on purpose: the report counts
    each one it automated (`auto_missed`), for the gate to refuse."""
    decision = Decision(triage_agent, providers={"screen": "agent"})
    dataset = resolve_dataset(triage_agent, "calibration")
    report = evaluate(decision, dataset)
    assert report["dataset"]["n"] == len(dataset.records) == 200
    assert set(report["questions"]) == {"destination", "urgency", "abusive"}
    decisions = report["decisions"]
    assert sum(decisions["outcome_counts"].values()) == 200
    assert decisions["outcome_counts"]["AUTO"] > 80
    automated = [
        record
        for record in dataset.records
        if record["labels"].get("urgency") == "yes"
        and decision.decide(record["input"]).outcome is Outcome.AUTO
    ]
    assert decisions["critical"][0]["auto_missed"] == len(automated) > 0
    assert set(report["subgroups"]) == {"channel"}
    json.dumps(report, allow_nan=False)  # reports must be strict JSON


def test_reports_never_contain_case_text(triage: Decision) -> None:
    dataset = resolve_dataset(triage.agent, "calibration")
    serialized = json.dumps(evaluate(triage, dataset))
    assert dataset.records[0]["input"]["message"]["body"] not in serialized


def test_local_validation_localises_degradation(triage_agent: Agent, tmp_path: Path) -> None:
    baseline = benchmark()  # the triage fixture's test set, on its default providers
    local = evaluate(Decision(triage_agent), load_dataset(local_site(tmp_path, triage_agent)))
    comparison = compare(local, baseline, triage_agent.manifest.evaluation.degradation_tolerance)
    assert comparison["status"] == "degraded"
    assert comparison["delta"] < -0.03
    subjects = {finding["subject"] for finding in comparison["findings"]}
    assert "channel=sms" in subjects
    # Emergencies stay protected where routing degrades: with the screen's keyword rules alone,
    # nothing is automated.
    assert local["decisions"]["outcome_counts"]["AUTO"] == 0
    assert local["decisions"]["critical"][0]["auto_missed"] == 0


def test_comparison_with_itself_is_clean() -> None:
    baseline = benchmark()
    comparison = compare(baseline, baseline, 0.03)
    assert comparison["status"] == "ok" and comparison["findings"] == []


def test_a_subgroup_the_benchmark_lacks_is_compared_with_its_overall_figure() -> None:
    """Never the whole benchmark's count shown as if it were the subgroup's."""
    from mendari.evals.render import comparison_text

    baseline = benchmark()
    local = copy.deepcopy(baseline)
    group = {"n": 40, "labelled": 40, "accuracy": 0.5, "auto_rate": 0.0, "abstention_rate": 0.0}
    local["subgroups"]["channel"]["xx"] = {**group, "false_automation_rate": None}
    comparison = compare(local, baseline, 0.03)
    (finding,) = [f for f in comparison["findings"] if f["subject"] == "channel=xx"]
    assert finding["baseline_scope"] == "overall" and finding["local_n"] == 40
    primary = baseline["questions"][baseline["primary"]]
    overall = round(primary["accuracy"] * primary["n"])  # 266 of 300
    line = next(x for x in comparison_text(comparison).splitlines() if "channel=xx" in x)
    assert line.startswith(f"  - channel=xx: benchmark overall {overall}")
    assert line.endswith("-> 20 of 40 [33.8-66.2%]")


def test_a_subgroup_with_few_labelled_members_is_never_flagged() -> None:
    """Before, the smallest group worth flagging (MIN_GROUP) was counted in members, not in the
    labelled members an accuracy is over: 40 cases with 2 labelled, both wrong, were flagged."""
    baseline = benchmark()
    local = copy.deepcopy(baseline)
    group = {"n": 40, "labelled": 2, "accuracy": 0.0, "auto_rate": 0.0, "abstention_rate": 0.0}
    local["subgroups"]["channel"]["xx"] = {**group, "false_automation_rate": None}
    comparison = compare(local, baseline, 0.03)
    assert not [f for f in comparison["findings"] if f["subject"] == "channel=xx"]


@pytest.mark.parametrize(
    "spoil",
    [
        lambda b: b.pop("dataset"),
        lambda b: (
            [b.pop("primary")]  # with subgroups counted as mendari.eval/1 counts them
            + [m.pop("labelled") for v in b["subgroups"].values() for m in v.values()]
        ),
        lambda b: next(iter(b["subgroups"]["channel"].values())).update(labelled="many"),
    ],
    ids=["dataset", "primary", "labelled"],
)
def test_a_baseline_without_what_it_measured_cannot_be_compared(
    spoil: Callable[[dict[str, Any]], None],
) -> None:
    """Before, these raised a KeyError or a ValueError in `compare` (a 500 from POST .../evals),
    as the malformed figures below once did."""
    baseline = benchmark()
    local = copy.deepcopy(baseline)
    for members in local["subgroups"]["channel"].values():
        members["accuracy"] = 0.0  # every subgroup reads the baseline's
    spoil(baseline)
    assert compare(local, baseline, 0.03)["status"] == "unknown"


def test_a_baseline_without_its_counts_is_compared_with_n_a() -> None:
    """The counts each figure is of are read from a published benchmark only where they are."""
    from mendari.evals.render import comparison_text

    baseline = benchmark()
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
        lambda b: b.update(subgroups={"channel": [1]}),
        lambda b: b.update(decisions={"automation": {"false_automation_rate": "x"}}),
    ],
    ids=["accuracy", "per-label", "calibration", "subgroups", "automation"],
)
def test_a_baseline_with_malformed_figures_cannot_be_compared(
    spoil: Callable[[dict[str, Any]], None],
) -> None:
    """A published benchmark is the agent author's file: one whose accuracy was null passed the
    card's checks and made POST .../evals answer 500 (TypeError in compare)."""
    baseline = benchmark()
    local = copy.deepcopy(baseline)
    spoil(baseline)
    comparison = compare(local, baseline, 0.03)
    assert comparison["status"] == "unknown"
    assert "published benchmark" in comparison["reason"] and comparison["findings"] == []


def test_agent_behaviour_tests_pass(triage: Decision) -> None:
    outcomes = run_behaviour_tests(triage)
    assert outcomes and all(o.passed for o in outcomes), [o for o in outcomes if not o.passed]


def test_decision_card_renders(triage_agent: Agent) -> None:
    card = render_card(triage_agent)
    for heading in (
        "## Intended use",
        "## Decision policy",
        "## Validation",
        "## Known failure modes",
    ):
        assert heading in card
    assert triage_agent.digest in card


def test_parallel_evaluation_matches_sequential(
    triage_agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A live agent service in both slots (in place of the network): evaluated in parallel.
    router = Decision(triage_agent, providers=calm_agent(monkeypatch))
    dataset = resolve_dataset(router.agent, "calibration").head(60)
    sequential = evaluate(router, dataset)
    parallel = evaluate(router, dataset, workers=4)
    for key in ("questions", "decisions", "subgroups", "misclassified"):
        assert parallel[key] == sequential[key]
    assert parallel["dataset"] == {**sequential["dataset"], "limit": 60} == sequential["dataset"]


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85"])
def test_a_line_separator_inside_a_case_is_not_a_line_end(tmp_path: Path, separator: str) -> None:
    """`json.dumps(ensure_ascii=False)` (the feedback export) leaves U+2028 raw inside a string;
    `str.splitlines` broke the line there and the dataset would not load again."""
    path = tmp_path / "cases.jsonl"
    record = {"id": "c1", "input": {"message": {"body": f"first{separator}second"}}}
    path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    assert load_dataset(path).records[0]["input"]["message"]["body"] == f"first{separator}second"


def test_the_card_lists_a_boolean_property_schema() -> None:
    """JSON Schema's `"note": true` (anything) loads and decides; the card's input table read it
    as an object and raised AttributeError."""
    from mendari.core.card import _schema_rows

    schema = {"properties": {"note": True, "age": {"type": "integer"}}, "required": ["note"]}
    assert _schema_rows(schema) == ["| `note` | any | yes |", "| `age` | integer | no |"]
