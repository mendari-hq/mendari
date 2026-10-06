"""What of an evaluation report a site can share: metrics, never cases.

A shared report (`format: mendari.site-report/1`) is a closed schema, the same on both sides:
the client builds it with `site_report` and the registry refuses anything else. It holds the
pack, the providers and policy, the metrics per question, the outcome counts and rates, the
critical counts, the threshold sweep, latency, cost, error counts, the dataset's size and the
subgroup cells. It never holds a case id, case text, the dataset's name or hash, or who approved
a governance override (only that one applied). Every string is a name (a question, label, slot,
variant, provider type, model, error code or subgroup value) of bounded length and alphabet, and
every number is bounded, so free text has nowhere to go. Subgroup cells and calibration bins
with fewer than `MIN_GROUP` cases are dropped, and so is a subgroup value that is not a short
name.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mendari.errors import MendariError

MIN_GROUP = 10
SITE_REPORT_FORMAT = "mendari.site-report/1"

_NAME = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
# Subgroup values come from a site's data (e.g. "es", "65+", "18-39"): short words only.
_VALUE = r"^\w[\w .+-]{0,39}$"
_VALUE_RE = re.compile(_VALUE)

Name = Annotated[str, Field(pattern=_NAME)]
Value = Annotated[str, Field(pattern=_VALUE)]
Count = Annotated[int, Field(ge=0, le=10**9)]
Rate = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
Positive = Annotated[float, Field(ge=0.0, le=1e9, allow_inf_nan=False)]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class SharedPack(_Closed):
    id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}/[a-z0-9][a-z0-9-]{0,62}$")]
    version: Annotated[str, Field(pattern=r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$")]
    digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class SharedProvider(_Closed):
    variant: Name
    type: Name
    model: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._@:/+-]{0,127}$")] | None


class SharedThresholds(_Closed):
    auto: Rate
    review: Rate


class SharedPolicy(_Closed):
    risk_level: Annotated[int, Field(ge=0, le=4)]
    thresholds: SharedThresholds
    governance_override: bool


class SharedDataset(_Closed):
    n: Count


class LabelMetrics(_Closed):
    precision: Rate | None
    recall: Rate | None
    f1: Rate | None
    support: Count


class CalibrationBin(_Closed):
    low: Rate
    high: Rate
    n: Annotated[int, Field(ge=MIN_GROUP, le=10**9)]
    confidence: Rate | None
    accuracy: Rate | None


class Calibration(_Closed):
    ece: Rate | None
    bins: Annotated[list[CalibrationBin], Field(max_length=20)]


class QuestionMetrics(_Closed):
    n: Count
    accuracy: Rate | None
    macro_f1: Rate | None
    per_label: Annotated[dict[Name, LabelMetrics], Field(max_length=64)]
    confusion: Annotated[
        dict[Name, Annotated[dict[Name, Count], Field(max_length=65)]], Field(max_length=64)
    ]
    calibration: Calibration
    brier: Positive | None


class Automation(_Closed):
    n: Count
    accuracy: Rate | None
    false_automation_rate: Rate | None
    false_automation_share: Rate | None


class Review(_Closed):
    n: Count
    expected_override_rate: Rate | None


class CriticalCounts(_Closed):
    question: Name
    label: Name
    n: Count
    detected: Count
    detection_rate: Rate | None
    auto_missed: Count


Outcome = Literal["AUTO", "HUMAN_REVIEW", "ABSTAIN", "BLOCK"]


class Decisions(_Closed):
    outcome_counts: dict[Outcome, Count]
    outcome_rates: dict[Outcome, Rate | None]
    abstention_rate: Rate | None
    automation: Automation
    review: Review
    recommendation_accuracy: Rate | None
    critical: Annotated[list[CriticalCounts], Field(max_length=64)]


class SweepRow(_Closed):
    threshold: Rate
    coverage: Rate | None
    accuracy: Rate | None
    errors: Count


class Latency(_Closed):
    mean: Positive | None
    p50: Positive | None
    p95: Positive | None
    max: Positive | None


class Cost(_Closed):
    total_usd: Positive | None
    per_decision_usd: Positive | None
    input_tokens: Count
    output_tokens: Count


class SubgroupCell(_Closed):
    n: Annotated[int, Field(ge=MIN_GROUP, le=10**9)]
    accuracy: Rate | None
    auto_rate: Rate | None
    abstention_rate: Rate | None
    false_automation_rate: Rate | None


class SiteReport(_Closed):
    """Everything a site may share about one evaluation, and nothing else."""

    format: Literal["mendari.site-report/1"]
    pack: SharedPack
    providers: Annotated[dict[Name, SharedProvider], Field(max_length=64)]
    policy: SharedPolicy
    dataset: SharedDataset
    primary: Name
    questions: Annotated[dict[Name, QuestionMetrics], Field(max_length=64)]
    decisions: Decisions
    threshold_sweep: Annotated[list[SweepRow], Field(max_length=101)]
    latency_ms: Latency
    cost: Cost
    errors: Annotated[dict[Name, Count], Field(max_length=64)]
    subgroups: Annotated[
        dict[Name, Annotated[dict[Value, SubgroupCell], Field(max_length=64)]],
        Field(max_length=32),
    ]


_CRITICAL_KEPT = ("question", "label", "n", "detected", "detection_rate", "auto_missed")


def _object(value: Any, where: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise MendariError(f"this report cannot be shared: {where} is not an object")
    return value


def _list(value: Any, where: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise MendariError(f"this report cannot be shared: {where} is not a list")
    return value


def site_report(report: Mapping[str, Any], *, min_group: int = MIN_GROUP) -> SiteReport:
    """The shareable part of an evaluation report, validated: small cells dropped, case ids,
    the dataset's identity and the override's approver left out. Raises `ValidationError` if
    anything that remains is not a bounded name or number, `MendariError` if the report is not
    shaped like one."""
    min_group = max(min_group, MIN_GROUP)
    questions = {}
    for name, question in _object(report.get("questions"), "questions").items():
        question = _object(question, f"questions.{name}")
        calibration = _object(question.get("calibration"), f"questions.{name}.calibration")
        bins = [
            b
            for b in _list(calibration.get("bins"), f"questions.{name}.calibration.bins")
            if (_object(b, "a calibration bin").get("n") or 0) >= min_group
        ]
        questions[name] = {**question, "calibration": {**calibration, "bins": bins}}
    decisions = dict(_object(report.get("decisions"), "decisions"))
    decisions["critical"] = [
        {key: _object(item, "decisions.critical").get(key) for key in _CRITICAL_KEPT}
        for item in _list(decisions.get("critical"), "decisions.critical")
    ]
    subgroups: dict[str, dict[str, Any]] = {}
    for attribute, cells in _object(report.get("subgroups"), "subgroups").items():
        kept = {
            value: cell
            for value, cell in _object(cells, f"subgroups.{attribute}").items()
            if (_object(cell, f"subgroups.{attribute}").get("n") or 0) >= min_group
            and _VALUE_RE.fullmatch(str(value))
        }
        if kept:
            subgroups[attribute] = kept
    policy = _object(report.get("policy"), "policy")
    return SiteReport.model_validate(
        {
            "format": SITE_REPORT_FORMAT,
            "pack": report.get("pack"),
            "providers": report.get("providers"),
            "policy": {
                "risk_level": policy.get("risk_level"),
                "thresholds": policy.get("thresholds"),
                "governance_override": policy.get("governance_override") is not None,
            },
            "dataset": {"n": _object(report.get("dataset"), "dataset").get("n")},
            "primary": report.get("primary"),
            "questions": questions,
            "decisions": decisions,
            "threshold_sweep": report.get("threshold_sweep"),
            "latency_ms": report.get("latency_ms"),
            "cost": report.get("cost"),
            "errors": report.get("errors") or {},
            "subgroups": subgroups,
        }
    )


def shareable(report: Mapping[str, Any], *, min_group: int = MIN_GROUP) -> dict[str, Any]:
    """`site_report` as the JSON-ready dict that is sent. A report that cannot be shared is a
    `MendariError` naming the fields (never their values)."""
    try:
        return site_report(report, min_group=min_group).model_dump(mode="json")
    except ValidationError as error:
        fields = sorted({".".join(str(part) for part in e["loc"]) for e in error.errors()})
        shown = ", ".join(fields[:5]) + (f" and {len(fields) - 5} more" if len(fields) > 5 else "")
        raise MendariError(
            f"this report cannot be shared: {shown} must be a short name or a number in range"
        ) from None


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
