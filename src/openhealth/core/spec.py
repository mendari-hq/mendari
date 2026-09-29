"""Decision Pack manifest, spec versions 0.1 and 0.2.

A pack declares *questions* (a fixed set of labels, each with human-readable criteria).
Providers answer questions with probability distributions; the policy turns answers into
one of four outcomes. Spec 0.2 adds `boolean` and `score` question types and *derived*
questions computed from other answers by a decision table. See docs/spec/decision-pack-0.2.md.
"""

from __future__ import annotations

import re
from enum import IntEnum, StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SPEC_VERSION = "0.2"
MAX_DERIVE_ROWS = 10

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


class ScoreLevel(_Strict):
    score: float
    criterion: str = Field(min_length=1)


class DeriveRow(_Strict):
    """One decision-table row: `{any: L, then: X}`, `{all: L, then: X}` or `{else: X}`.

    `in: [members]` limits a row to some of the members, for tables whose members answer in
    different directions (e.g. criteria where `no` fails, and exclusions where `yes` fails).
    """

    any: str | None = None
    all: str | None = None
    then: str | None = None
    otherwise: str | None = Field(default=None, alias="else")
    scope: list[str] | None = Field(default=None, alias="in", min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> DeriveRow:
        if self.otherwise is not None:
            if self.any is not None or self.all is not None or self.then is not None:
                raise ValueError("an `else` row takes no other keys")
            if self.scope is not None:
                raise ValueError("an `else` row applies to all members; it takes no `in`")
        elif (self.any is None) == (self.all is None) or self.then is None:
            raise ValueError(
                "a row is {any: <label>, then: <label>}, {all: <label>, then: <label>} "
                "or {else: <label>}"
            )
        return self

    @property
    def result(self) -> str:
        return self.otherwise if self.otherwise is not None else str(self.then)

    def applies_to(self, member: str) -> bool:
        return self.scope is None or member in self.scope

    def describe(self) -> str:
        if self.otherwise is not None:
            return f"else -> {self.otherwise}"
        kind, label = ("any", self.any) if self.any is not None else ("all", self.all)
        within = f" in [{', '.join(self.scope)}]" if self.scope else ""
        return f"{kind} {label}{within} -> {self.then}"


class Derive(_Strict):
    """A question computed from other questions' answers: the first matching row wins."""

    members: list[str] = Field(alias="from", min_length=1)
    table: list[DeriveRow] = Field(min_length=1)

    @model_validator(mode="after")
    def _table(self) -> Derive:
        if len(set(self.members)) != len(self.members):
            raise ValueError("derive `from` lists a question more than once")
        if self.table[-1].otherwise is None:
            raise ValueError("the last row of a derive table must be {else: <label>}")
        if any(row.otherwise is not None for row in self.table[:-1]):
            raise ValueError("only the last row may be an `else` row")
        if len(self.table) - 1 > MAX_DERIVE_ROWS:
            raise ValueError(f"a derive table has at most {MAX_DERIVE_ROWS} rows plus `else`")
        for row in self.table:
            outside = sorted(set(row.scope or []) - set(self.members))
            if outside:
                raise ValueError(f"row `in` names questions not in `from`: {outside}")
        return self


class Question(_Strict):
    type: Literal["choice", "boolean", "score"] = "choice"
    instructions: str = Field(min_length=1)
    criteria: dict[str, str]
    levels: dict[str, ScoreLevel] | None = None
    derive: Derive | None = None

    @model_validator(mode="before")
    @classmethod
    def _implied_criteria(cls, data: Any) -> Any:
        """`boolean` questions always have labels yes/no; `score` labels come from levels."""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if data.get("type") == "boolean":
            given = data.get("criteria") or {}
            extra = set(given) - {"yes", "no"}
            if extra:
                raise ValueError(f"a boolean question only has labels yes/no, not {sorted(extra)}")
            data["criteria"] = {"yes": given.get("yes", "Yes."), "no": given.get("no", "No.")}
        elif data.get("type") == "score" and "criteria" not in data:
            levels = data.get("levels") or {}
            data["criteria"] = {
                label: level.get("criterion", "") if isinstance(level, dict) else ""
                for label, level in levels.items()
            }
        return data

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

    @model_validator(mode="after")
    def _levels(self) -> Question:
        if self.type == "score":
            if not self.levels or set(self.levels) != set(self.criteria):
                raise ValueError("a score question needs `levels` for exactly its labels")
        elif self.levels is not None:
            raise ValueError("only score questions have `levels`")
        return self

    @property
    def labels(self) -> list[str]:
        """Labels in declaration order; score questions are ordered by score."""
        levels = self.levels
        if self.type == "score" and levels:
            return sorted(levels, key=lambda label: levels[label].score)
        return list(self.criteria)

    def expected_score(self, probabilities: dict[str, float]) -> float | None:
        levels = self.levels
        if self.type != "score" or not levels:
            return None
        return sum(p * levels[label].score for label, p in probabilities.items() if label in levels)


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
    labels: list[str] = []
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    # The total probability of `labels`, whichever label is most likely: acts on a meaningful
    # chance of a critical label (e.g. P(urgent) >= 0.35), not only when that label wins.
    min_probability: float | None = Field(default=None, gt=0.0, le=1.0)
    # Score questions: inclusive bounds on the expected score.
    min_score: float | None = None
    max_score: float | None = None

    @model_validator(mode="after")
    def _something_to_match(self) -> Condition:
        if not self.labels and self.min_score is None and self.max_score is None:
            raise ValueError("a condition needs `labels`, `min_score` or `max_score`")
        if self.min_probability is not None:
            if not self.labels:
                raise ValueError("`min_probability` needs the `labels` it applies to")
            if self.min_confidence:
                raise ValueError("use `min_probability` or `min_confidence`, not both")
        return self


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


class CdsCard(_Strict):
    """Card wording for one pack. Keys are the primary question's labels, or `none` when the
    decision has no recommendation; `{recommendation}` and `{priority}` are filled in."""

    summaries: dict[str, str] = Field(min_length=1)
    indicators: dict[str, Literal["info", "warning", "critical"]] = {}
    # Recommendations that produce a card at all; e.g. a patient-view service stays silent for
    # patients who are clearly not eligible. None: every decision produces a card.
    show: list[str] | None = None
    suggestion: str = "Create a task"


class CdsHooksConfig(_Strict):
    hook: str
    title: str
    description: str
    prefetch: dict[str, str] = {}
    card: CdsCard | None = None


class PackManifest(_Strict):
    spec_version: Literal["0.1", "0.2"]
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
        return self.questions[question].labels

    @property
    def derived(self) -> dict[str, Question]:
        return {key: q for key, q in self.questions.items() if q.derive is not None}


def cross_reference_problems(m: PackManifest) -> list[str]:
    """Consistency rules that a JSON-shape check cannot express."""
    problems: list[str] = []
    for key in m.questions:
        if not _KEY.match(key):
            problems.append(f"question key {key!r} must match {_KEY.pattern}")
    derived = {key for key, q in m.questions.items() if q.derive is not None}
    if m.spec_version == "0.1":
        newer = [k for k, q in m.questions.items() if q.type != "choice" or q.derive is not None]
        newer += [
            f"rule {rule.name}"
            for rule in m.policy.rules
            if rule.when.min_probability is not None
            or rule.when.min_score is not None
            or rule.when.max_score is not None
        ]
        if newer:
            problems.append(f"{newer} use spec 0.2 features; set spec_version: '0.2'")

    answered_by: dict[str, str] = {}
    for slot_name, slot in m.providers.items():
        for question in slot.answers:
            if question in derived:
                problems.append(f"derived question {question!r} cannot be answered by a slot")
            elif question not in m.questions:
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
        if question not in answered_by and question not in derived:
            problems.append(f"question {question!r} is not answered by any provider slot")
    problems.extend(_derive_problems(m, derived))

    policy = m.policy
    if policy.primary not in m.questions:
        return [*problems, f"policy.primary {policy.primary!r} is not a question"]
    primary_labels = set(m.questions[policy.primary].criteria)

    for rule in policy.rules:
        condition = rule.when
        target = m.questions.get(condition.question)
        if target is None:
            problems.append(
                f"rule {rule.name!r} references unknown question {condition.question!r}"
            )
        else:
            unknown = set(condition.labels) - set(target.criteria)
            if unknown:
                problems.append(f"rule {rule.name!r} references unknown labels {sorted(unknown)}")
            bounded = condition.min_score is not None or condition.max_score is not None
            if bounded and target.type != "score":
                problems.append(f"rule {rule.name!r}: score bounds need a score question")
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

    card = m.cds_hooks.card if m.cds_hooks is not None else None
    if card is not None:
        allowed = set(primary_labels) | {"none"}
        named = set(card.summaries) | set(card.indicators) | set(card.show or [])
        unknown_keys = sorted(named - allowed)
        if unknown_keys:
            problems.append(f"cds_hooks.card names unknown recommendations {unknown_keys}")

    for critical in m.evaluation.critical:
        critical_question = m.questions.get(critical.question)
        if critical_question is None or critical.label not in critical_question.criteria:
            problems.append(f"evaluation.critical {critical.question}={critical.label} is unknown")
    if (
        m.evaluation.benchmark_dataset
        and m.evaluation.benchmark_dataset not in m.evaluation.datasets
    ):
        problems.append("evaluation.benchmark_dataset must name one of evaluation.datasets")
    return problems


def _derive_problems(m: PackManifest, derived: set[str]) -> list[str]:
    problems = []
    for key in sorted(derived):
        spec = m.questions[key].derive
        if spec is None:
            continue
        for member in spec.members:
            if member not in m.questions or member in derived:
                problems.append(f"{key}: derive member {member!r} must be a non-derived question")
        for row in spec.table:
            label = row.any if row.any is not None else row.all
            scoped = [q for q in spec.members if row.applies_to(q) and q in m.questions]
            labels = {lab for q in scoped for lab in m.questions[q].criteria}
            if label is not None and label not in labels:
                problems.append(f"{key}: row label {label!r} is not a label of its members")
            if row.result not in m.questions[key].criteria:
                problems.append(f"{key}: row result {row.result!r} is not one of its labels")
    return problems
