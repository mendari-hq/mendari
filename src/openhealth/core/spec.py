"""Decision Pack manifest, spec version 0.1.

A pack declares *questions* (a fixed set of labels, each with human-readable criteria).
Providers answer questions with probability distributions; the policy turns answers into
one of four outcomes. See docs/spec/decision-pack-0.1.md for the normative description.
"""

from __future__ import annotations

import re
from enum import IntEnum, StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SPEC_VERSION = "0.1"

_PACK_ID = re.compile(r"^[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*$")
_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
_KEY = re.compile(r"^[a-z][a-z0-9_]*$")


class Outcome(StrEnum):
    """What the runtime is allowed to do with a decision."""

    AUTO = "AUTO"  # act without a human in the loop
    HUMAN_REVIEW = "HUMAN_REVIEW"  # a human confirms or corrects the recommendation
    ABSTAIN = "ABSTAIN"  # no recommendation; hand off to the existing manual workflow
    BLOCK = "BLOCK"  # the action must not happen (invalid input or a blocking policy rule)


class RiskLevel(IntEnum):
    ADMINISTRATIVE = 0
    CLINICAL_INFORMATION = 1
    CLINICAL_SUPPORT = 2
    PATIENT_PREDICTION = 3
    TIME_CRITICAL = 4


class Priority(StrEnum):
    """FHIR R4 request-priority codes, so decisions map onto Task.priority unchanged."""

    ROUTINE = "routine"
    URGENT = "urgent"
    ASAP = "asap"
    STAT = "stat"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Publisher(_Strict):
    name: str
    type: Literal["community", "verified", "institutional"] = "community"
    url: str | None = None


class Risk(_Strict):
    level: RiskLevel
    category: str
    rationale: str


class Evidence(_Strict):
    title: str
    url: str | None = None
    note: str | None = None


class Card(_Strict):
    """Human-facing Decision Card content that cannot be derived from the rest of the pack."""

    intended_use: str
    intended_users: list[str] = Field(min_length=1)
    patient_population: str
    out_of_scope: list[str] = []
    known_failure_modes: list[str] = []
    evidence: list[Evidence] = []


class Inputs(_Strict):
    json_schema: str = Field(alias="schema")
    fhir: str | None = None


class Question(_Strict):
    instructions: str = Field(min_length=1)
    criteria: dict[str, str]

    @field_validator("criteria")
    @classmethod
    def _labels(cls, criteria: dict[str, str]) -> dict[str, str]:
        if not 2 <= len(criteria) <= 255:
            raise ValueError("a question needs between 2 and 255 labels")
        for label, description in criteria.items():
            if not _KEY.match(label):
                raise ValueError(f"label {label!r} must match {_KEY.pattern}")
            if not description.strip():
                raise ValueError(f"label {label!r} needs a non-empty criterion")
        return criteria


class ProviderVariant(BaseModel):
    """One concrete way to answer a slot's questions. Extra keys are provider-specific config."""

    model_config = ConfigDict(extra="allow", frozen=True)

    type: str


class ProviderSlot(_Strict):
    answers: list[str] = Field(min_length=1)
    use: str
    variants: dict[str, ProviderVariant] = Field(min_length=1)

    @model_validator(mode="after")
    def _default_exists(self) -> ProviderSlot:
        if self.use not in self.variants:
            raise ValueError(f"default variant {self.use!r} is not one of {sorted(self.variants)}")
        return self


class Condition(_Strict):
    question: str
    labels: list[str] = Field(min_length=1)
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class PolicyRule(_Strict):
    name: str
    when: Condition
    outcome: Outcome
    recommendation: str | None = None
    priority: Priority | None = None
    reason: str


class Thresholds(_Strict):
    auto: float = Field(ge=0.0, le=1.0)
    review: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _ordered(self) -> Thresholds:
        if self.review > self.auto:
            raise ValueError("thresholds.review must be <= thresholds.auto")
        return self


class LabelPolicy(_Strict):
    allow_auto: bool = True
    auto_threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class Policy(_Strict):
    primary: str
    thresholds: Thresholds
    rules: list[PolicyRule] = []
    labels: dict[str, LabelPolicy] = {}
    fallback: str
    default_priority: Priority = Priority.ROUTINE


class CriticalLabel(_Strict):
    """A (question, label) whose true cases must never be automated. Tracked in every eval."""

    question: str
    label: str


class Evaluation(_Strict):
    datasets: dict[str, str] = {}
    benchmark: str | None = None
    benchmark_dataset: str | None = None
    critical: list[CriticalLabel] = []
    subgroups: list[str] = []
    degradation_tolerance: float = Field(default=0.03, gt=0.0, lt=1.0)


class AuditConfig(_Strict):
    store_input: Literal["full", "hash"] = "full"


class CdsHooksConfig(_Strict):
    hook: str
    title: str
    description: str
    prefetch: dict[str, str] = {}


class PackManifest(_Strict):
    spec_version: Literal["0.1"]
    id: str
    version: str
    name: str
    summary: str
    publisher: Publisher
    license: str
    risk: Risk
    card: Card
    inputs: Inputs
    questions: dict[str, Question] = Field(min_length=1)
    providers: dict[str, ProviderSlot] = Field(min_length=1)
    policy: Policy
    evaluation: Evaluation = Evaluation()
    tests: str | None = None
    audit: AuditConfig = AuditConfig()
    cds_hooks: CdsHooksConfig | None = None

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not _PACK_ID.match(value):
            raise ValueError("id must look like 'namespace/name' (lowercase, digits, dashes)")
        return value

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        if not _SEMVER.match(value):
            raise ValueError("version must be semantic (MAJOR.MINOR.PATCH)")
        return value

    @model_validator(mode="after")
    def _cross_references(self) -> PackManifest:
        problems = cross_reference_problems(self)
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def labels(self, question: str) -> list[str]:
        return list(self.questions[question].criteria)


def cross_reference_problems(m: PackManifest) -> list[str]:
    """Consistency rules that a JSON-shape check cannot express."""
    problems: list[str] = []
    for key in m.questions:
        if not _KEY.match(key):
            problems.append(f"question key {key!r} must match {_KEY.pattern}")

    answered_by: dict[str, str] = {}
    for slot_name, slot in m.providers.items():
        for question in slot.answers:
            if question not in m.questions:
                problems.append(
                    f"provider slot {slot_name!r} answers unknown question {question!r}"
                )
            elif question in answered_by:
                problems.append(
                    f"question {question!r} is answered by both {answered_by[question]!r} "
                    f"and {slot_name!r}"
                )
            else:
                answered_by[question] = slot_name
    for question in m.questions:
        if question not in answered_by:
            problems.append(f"question {question!r} is not answered by any provider slot")

    policy = m.policy
    if policy.primary not in m.questions:
        return [*problems, f"policy.primary {policy.primary!r} is not a question"]
    primary_labels = set(m.questions[policy.primary].criteria)

    for rule in policy.rules:
        condition = rule.when
        if condition.question not in m.questions:
            problems.append(
                f"rule {rule.name!r} references unknown question {condition.question!r}"
            )
        else:
            unknown = set(condition.labels) - set(m.questions[condition.question].criteria)
            if unknown:
                problems.append(f"rule {rule.name!r} references unknown labels {sorted(unknown)}")
        if rule.recommendation is not None and rule.recommendation not in primary_labels:
            problems.append(
                f"rule {rule.name!r} recommends {rule.recommendation!r}, "
                f"which is not a label of {policy.primary!r}"
            )
        if rule.outcome is Outcome.AUTO and m.risk.level >= RiskLevel.CLINICAL_SUPPORT:
            problems.append(
                f"rule {rule.name!r} requests AUTO, which risk level {int(m.risk.level)} forbids"
            )
    for label in policy.labels:
        if label not in primary_labels:
            problems.append(f"policy.labels has unknown label {label!r}")

    for critical in m.evaluation.critical:
        target = m.questions.get(critical.question)
        if target is None or critical.label not in target.criteria:
            problems.append(f"evaluation.critical {critical.question}={critical.label} is unknown")
    if (
        m.evaluation.benchmark_dataset
        and m.evaluation.benchmark_dataset not in m.evaluation.datasets
    ):
        problems.append("evaluation.benchmark_dataset must name one of evaluation.datasets")
    return problems
