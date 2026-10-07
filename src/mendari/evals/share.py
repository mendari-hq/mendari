# SPDX-License-Identifier: Apache-2.0
"""What of an evaluation report a site can share: metrics, never cases.

A shared report (`format: mendari.site-report/1`) is a closed schema, the same on both sides:
the client builds it with `site_report` and the registry refuses anything else. It holds the
pack, the providers and policy, the metrics per question, the outcome counts and rates, the
critical counts, the threshold sweep, latency, cost, error counts and the dataset's size. It
never holds a case id, case text, the dataset's name or hash, who approved a governance
override (only that one applied), or subgroup cells: a subgroup's values come from a site's
data (a clinic, a clinician, a date), and nothing can tell a category from a name until a pack
declares each subgroup's values. Every string is a name the pack or Mendari defines (a
question, label, slot, variant, composite member, provider type, model or error code), short and
ASCII; every number is bounded and rounded. A composite provider (`any_of`, `cascade`, `vote`)
is shared as it is recorded: its model lists each member's as `member:model`, joined as the
composite joins them (" + ", or a cascade's " → ", the one character that is not ASCII), and a
failed member's error is `member:code`. Calibration bins with fewer than `MIN_GROUP` cases are
dropped.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
)

from mendari.errors import MendariError
from mendari.evals.result import normalize
from mendari.providers import PROVIDER_TYPES
from mendari.providers.composite import CompositeProvider

MIN_GROUP = 10
MAX_MEMBERS = 16  # the most members a shared composite may list
SITE_REPORT_FORMAT = "mendari.site-report/1"

_NAME_PART = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"
_MODEL_PART = r"[A-Za-z0-9][A-Za-z0-9._@:/+-]{0,127}"
_NAME = rf"^{_NAME_PART}$"
# An error code, or a composite's failed members as `member:code` (", " between several, as
# `CompositeProvider.output` records them).
_MEMBER_CODE = rf"{_NAME_PART}:{_NAME_PART}"
_ERROR_KEY = rf"^(?:{_NAME_PART}|{_MEMBER_CODE}(?:, {_MEMBER_CODE}){{0,{MAX_MEMBERS - 1}}})$"

Name = Annotated[str, Field(pattern=_NAME)]
ErrorKey = Annotated[str, Field(pattern=_ERROR_KEY)]
Count = Annotated[int, Field(ge=0, le=10**9)]
# Rounded on both sides: the digits past these say nothing about a pack and could say something
# about a single case.
Rate = Annotated[
    float, Field(ge=0.0, le=1.0, allow_inf_nan=False), AfterValidator(lambda v: round(v, 4))
]
Positive = Annotated[
    float, Field(ge=0.0, le=1e9, allow_inf_nan=False), AfterValidator(lambda v: round(v, 3))
]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class SharedPack(_Closed):
    id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}/[a-z0-9][a-z0-9-]{0,62}$")]
    version: Annotated[str, Field(pattern=r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$")]
    digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


# How each composite type joins its members' `member:model` in its own model.
JOINERS: dict[str, str] = {
    name: kind.joiner
    for name, kind in PROVIDER_TYPES.items()
    if issubclass(kind, CompositeProvider)
}
_MODEL = re.compile(_MODEL_PART)
_COMPOSITE_MODEL = {
    name: re.compile(
        rf"{_NAME_PART}:{_MODEL_PART}"
        rf"(?:{re.escape(joiner)}{_NAME_PART}:{_MODEL_PART}){{1,{MAX_MEMBERS - 1}}}"
    )
    for name, joiner in JOINERS.items()
}


class SharedProvider(_Closed):
    variant: Name
    type: Name
    model: str | None

    @field_validator("model")
    @classmethod
    def _a_model_name(cls, model: str | None, info: ValidationInfo) -> str | None:
        """A model name; a composite's lists its members' (each a name, a colon and a model
        name), joined as that type joins them, and only a composite's does."""
        pattern = _COMPOSITE_MODEL.get(info.data.get("type", ""), _MODEL)
        if model is not None and not pattern.fullmatch(model):
            raise ValueError("must be a model name, or a composite's members and their models")
        return model


def composite_members(provider: SharedProvider) -> list[str] | None:
    """The members a composite's model lists, in order; None for any other provider."""
    joiner = JOINERS.get(provider.type)
    if joiner is None or provider.model is None:
        return None
    return [part.split(":", 1)[0] for part in provider.model.split(joiner)]


def failed_members(error: str) -> list[str]:
    """The composite members an error key names (none for a plain error code)."""
    return [part.split(":", 1)[0] for part in error.split(", ") if ":" in part]


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
    errors: Annotated[dict[ErrorKey, Count], Field(max_length=64)]


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


# A question's figures a site report carries (mendari.site-report/1): what a mendari.eval/2
# report adds (each proportion's interval, the bootstrap Brier interval, the equal-mass
# calibration) stays with the site.
_QUESTION_KEPT = ("n", "accuracy", "macro_f1", "per_label", "confusion", "brier")


def site_report(report: Mapping[str, Any], *, min_group: int = MIN_GROUP) -> SiteReport:
    """The shareable part of an evaluation report, validated: small bins dropped; case ids,
    subgroups, the dataset's identity and the override's approver left out. Raises
    `ValidationError` if anything that remains is not a bounded name or number, `MendariError`
    if the report is not shaped like one."""
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
        kept = {key: question.get(key) for key in _QUESTION_KEPT if key in question}
        questions[name] = {**kept, "calibration": {"ece": calibration.get("ece"), "bins": bins}}
    decisions = dict(_object(report.get("decisions"), "decisions"))
    decisions["critical"] = [
        {key: _object(item, "decisions.critical").get(key) for key in _CRITICAL_KEPT}
        for item in _list(decisions.get("critical"), "decisions.critical")
    ]
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
            "threshold_sweep": [
                {key: row.get(key) for key in ("threshold", "coverage", "accuracy", "errors")}
                for row in _list(report.get("threshold_sweep"), "threshold_sweep")
                if isinstance(row, dict)
            ],
            "latency_ms": normalize(report)["run"].get("latency_ms"),
            "cost": report.get("cost"),
            "errors": report.get("errors") or {},
        }
    )


def _field_names() -> frozenset[str]:
    models: list[type[BaseModel]] = [SiteReport]
    names: set[str] = {"report", "site", "body", "query", "path"}
    while models:
        model = models.pop()
        for name, field in model.model_fields.items():
            names.add(name)
            for kind in _models_in(field.annotation):
                if kind.__name__ not in {m.__name__ for m in models}:
                    models.append(kind)
    return frozenset(names)


def _models_in(annotation: Any) -> list[type[BaseModel]]:
    found: list[type[BaseModel]] = []
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        found.append(annotation)
    for argument in getattr(annotation, "__args__", ()) or ():
        found += _models_in(argument)
    return found


def where(loc: tuple[Any, ...] | list[Any]) -> str:
    """A validation error's location without the data in it: list positions and the share
    models' field names are shown, anything else (a mapping key, an unknown field) is `<key>`."""
    known = _FIELD_NAMES
    shown = [
        str(part) if isinstance(part, int) or part in known else "<key>"
        for part in loc
        if part != "[key]"
    ]
    return ".".join(shown)


def shareable(report: Mapping[str, Any], *, min_group: int = MIN_GROUP) -> dict[str, Any]:
    """`site_report` as the JSON-ready dict that is sent. A report that cannot be shared is a
    `MendariError` naming the fields (never their values)."""
    try:
        return site_report(report, min_group=min_group).model_dump(mode="json")
    except ValidationError as error:
        fields = sorted({where(e["loc"]) for e in error.errors()})
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


_FIELD_NAMES = _field_names()
