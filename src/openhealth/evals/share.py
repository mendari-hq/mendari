"""What of an evaluation report a site can share: metrics, never cases.

Reports already hold no case text, but they do hold case ids (`misclassified`), the dataset's
name and digest, and subgroup cells that can be as small as one case. A shared report keeps
the pack, providers and policy, the metrics per question, the outcome rates, the threshold
sweep, latency, cost and error counts, and only subgroup cells with at least `MIN_GROUP` cases.
Calibration bins with fewer cases are dropped the same way. The registry applies the same filter
to whatever it is sent.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

MIN_GROUP = 10
_KEPT = (
    "format",
    "pack",
    "providers",
    "policy",
    "primary",
    "decisions",
    "threshold_sweep",
    "latency_ms",
    "cost",
    "errors",
)


def shareable(report: Mapping[str, Any], *, min_group: int = MIN_GROUP) -> dict[str, Any]:
    shared: dict[str, Any] = {key: report[key] for key in _KEPT if key in report}
    questions = {}
    for name, question in (report.get("questions") or {}).items():
        kept = dict(question)
        calibration = question.get("calibration")
        if isinstance(calibration, Mapping):
            bins = [b for b in calibration.get("bins", []) if (b.get("n") or 0) >= min_group]
            kept["calibration"] = {**calibration, "bins": bins}
        questions[name] = kept
    shared["questions"] = questions
    shared["dataset"] = {"n": (report.get("dataset") or {}).get("n")}
    subgroups: dict[str, dict[str, Any]] = {}
    for attribute, cells in (report.get("subgroups") or {}).items():
        kept = {value: cell for value, cell in cells.items() if (cell.get("n") or 0) >= min_group}
        if kept:
            subgroups[attribute] = kept
    shared["subgroups"] = subgroups
    return shared


def summary(report: Mapping[str, Any]) -> dict[str, Any]:
    """The few numbers a site-to-site comparison shows."""
    primary = report.get("primary")
    question = (report.get("questions") or {}).get(primary, {})
    decisions = report.get("decisions") or {}
    return {
        "n": (report.get("dataset") or {}).get("n"),
        "primary": primary,
        "accuracy": question.get("accuracy"),
        "macro_f1": question.get("macro_f1"),
        "automation_rate": (decisions.get("outcome_rates") or {}).get("AUTO"),
        "false_automation_rate": (decisions.get("automation") or {}).get("false_automation_rate"),
        "ece": (question.get("calibration") or {}).get("ece"),
    }
