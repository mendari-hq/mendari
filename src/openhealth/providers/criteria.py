"""Structured criteria: yes / no / unknown from coded data, dates and numbers, computed in code.

Eligibility-style questions ("HbA1c between 7.0 and 10.5 % in the last 90 days", "on metformin
for at least 90 days") need arithmetic on values, units and dates. That is code's job: models,
Jev included, are unreliable at numbers and date comparison (Jev's documentation says as much).
This provider evaluates such criteria deterministically from the pack input, and says `unknown`
whenever the data needed is missing, out of date or in an unrecognised unit, never a guess.

Each question has an ordered list of checks; the first check that applies decides, and
`default` answers when none does:

    questions:
      hba1c_in_range:
        checks:
          - observation: {codes: ["4548-4"], within_days: 90, min: 7.0, max: 10.5, unit: "%",
                          convert: {"mmol/mol": {multiply: 0.0915, add: 2.15}}}
        default: unknown
      t2d_diagnosis:
        checks:
          - condition: {codes: ["E11*", "44054006"], then: "yes"}
          - condition: {codes: ["E10*", "46635009"], then: "no"}
        default: unknown

Two more kinds work on any part of the input: `exists` finds an item of a given type in a
list within a date window (a document, report or lab on file), and `value` compares a field
with a set of values (e.g. the priority a referrer chose):

      ecg_on_file:
        checks:
          - exists: {in: documents, codes: ["11524-6"], within_days: 365, then: "yes"}
        default: "no"
      marked_urgent:
        checks:
          - value: {field: referral.priority, in: [urgent, asap, stat], then: "yes"}
        default: "no"

A code ending in `*` matches by prefix (ICD-10 families). This is a deliberately small stepping
stone; CQL (spec work in v0.4) is the standard way to express such logic.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from openhealth.errors import PackValidationError
from openhealth.providers.base import Provider, ProviderOutput, fingerprint, get_path

Label = Literal["yes", "no", "unknown"]
DECIDED = 0.98  # probability given to a determined answer; the rest is shared evenly


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Conversion(_Model):
    multiply: float = 1.0
    add: float = 0.0


class NumberCheck(_Model):
    field: str
    min: float | None = None
    max: float | None = None
    in_range: Label = "yes"
    out_of_range: Label = "no"


class ObservationCheck(_Model):
    codes: list[str] = Field(min_length=1)
    within_days: int | None = Field(default=None, ge=0)
    min: float | None = None
    max: float | None = None
    unit: str | None = None
    convert: dict[str, Conversion] = {}
    in_range: Label = "yes"
    out_of_range: Label = "no"


class ConditionCheck(_Model):
    codes: list[str] = Field(min_length=1)
    statuses: list[str] | None = None  # e.g. [active]; None = any status
    within_days: int | None = Field(default=None, ge=0)  # onset within the window
    then: Label


class MedicationCheck(_Model):
    codes: list[str] = []
    names: list[str] = []  # case-insensitive substrings of the medication name
    statuses: list[str] = ["active"]
    min_days: int | None = Field(default=None, ge=0)  # started at least this long ago
    within_days: int | None = Field(default=None, ge=0)  # taken (or ended) within the window
    then: Label
    too_recent: Label = "no"  # present, but started less than `min_days` ago

    @model_validator(mode="after")
    def _something_to_match(self) -> MedicationCheck:
        if not self.codes and not self.names:
            raise ValueError("a medication check needs `codes` or `names`")
        return self


class ExistsCheck(_Model):
    source: str = Field(alias="in")  # input path of a list, e.g. `documents`
    codes: list[str] = Field(min_length=1)
    code_field: str = "code"
    date_field: str = "date"
    within_days: int | None = Field(default=None, ge=0)  # dated no later than this before as_of
    then: Label
    undated: Label = "unknown"  # a matching item without a date, when there is a window


class ValueCheck(_Model):
    field: str
    values: list[str] = Field(alias="in", min_length=1)
    then: Label


KINDS = ("number", "observation", "condition", "medication", "exists", "value")


class Check(_Model):
    number: NumberCheck | None = None
    observation: ObservationCheck | None = None
    condition: ConditionCheck | None = None
    medication: MedicationCheck | None = None
    exists: ExistsCheck | None = None
    value: ValueCheck | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Check:
        if sum(getattr(self, kind) is not None for kind in KINDS) != 1:
            raise ValueError(f"each check is exactly one of {'/'.join(KINDS)}")
        return self


class QuestionCriteria(_Model):
    checks: list[Check] = Field(min_length=1)
    default: Label = "unknown"


class CriteriaConfig(_Model):
    as_of: str = "as_of"  # input field with the screening date; today when absent
    conditions: str = "conditions"
    observations: str = "observations"
    medications: str = "medications"
    questions: dict[str, QuestionCriteria]


Decision = tuple[Label, str]  # (label, evidence)


class CriteriaProvider(Provider):
    type = "criteria"

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        relative = self.config.get("config")
        if not isinstance(relative, str):
            raise PackValidationError(self.pack.id, [f"{self.info.slot}: `config` is required"])
        try:
            self.criteria = CriteriaConfig.model_validate(self.pack.read_yaml(relative))
        except ValidationError as error:
            raise PackValidationError(self.pack.id, [f"{relative}: {error}"]) from None
        problems = []
        if set(self.criteria.questions) != set(self.questions):
            problems.append(f"{relative}: must define exactly the slot's questions")
        for key, spec in self.criteria.questions.items():
            question = self.questions.get(key)
            if question is None:
                continue
            used = {spec.default}.union(*(_labels(check) for check in spec.checks))
            missing = sorted(used - set(question.criteria))
            if missing:
                problems.append(f"{relative}: {key} answers {missing}, not labels of the question")
        if problems:
            raise PackValidationError(self.pack.id, problems)
        self.info = replace(self.info, model=f"criteria@{fingerprint(self.pack, relative)}")

    def answer(self, inputs: Mapping[str, Any]) -> ProviderOutput:
        as_of = _date(get_path(inputs, self.criteria.as_of)) or datetime.now(UTC).date()
        records = {
            "conditions": _items(get_path(inputs, self.criteria.conditions)),
            "observations": _items(get_path(inputs, self.criteria.observations)),
            "medications": _items(get_path(inputs, self.criteria.medications)),
        }
        answers = {}
        for key, spec in self.criteria.questions.items():
            label, evidence = _decide(spec, inputs, records, as_of)
            labels = list(self.questions[key].criteria)
            rest = (1 - DECIDED) / (len(labels) - 1)
            probabilities = {lab: (DECIDED if lab == label else rest) for lab in labels}
            answers[key] = self.make_answer(key, probabilities, (evidence,))
        return ProviderOutput(answers=answers)


def _labels(check: Check) -> set[str]:
    """Every label a check can answer (for validation against the question's labels)."""
    if check.number:
        return {check.number.in_range, check.number.out_of_range}
    if check.observation:
        return {check.observation.in_range, check.observation.out_of_range, "unknown"}
    if check.condition:
        dated = check.condition.within_days is not None
        return {check.condition.then, *(["unknown"] if dated else [])}
    if check.medication:
        timed = check.medication.min_days is not None
        return {check.medication.then, *([check.medication.too_recent, "unknown"] if timed else [])}
    if check.exists:
        windowed = check.exists.within_days is not None
        return {check.exists.then, *([check.exists.undated] if windowed else [])}
    if check.value:
        return {check.value.then}
    return set()


def _decide(
    spec: QuestionCriteria,
    inputs: Mapping[str, Any],
    records: Mapping[str, list[Mapping[str, Any]]],
    as_of: date,
) -> Decision:
    for check in spec.checks:
        if check.number:
            decided = _number(check.number, inputs)
        elif check.observation:
            decided = _observation(check.observation, records["observations"], as_of)
        elif check.condition:
            decided = _condition(check.condition, records["conditions"], as_of)
        elif check.medication:
            decided = _medication(check.medication, records["medications"], as_of)
        elif check.exists:
            decided = _exists(check.exists, _items(get_path(inputs, check.exists.source)), as_of)
        elif check.value:
            decided = _value(check.value, inputs)
        else:
            decided = None
        if decided is not None:
            return decided
    return spec.default, f"no data decides this criterion (default: {spec.default})"


def _number(check: NumberCheck, inputs: Mapping[str, Any]) -> Decision | None:
    value = get_path(inputs, check.field)
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    inside = (check.min is None or value >= check.min) and (check.max is None or value <= check.max)
    label = check.in_range if inside else check.out_of_range
    return label, f"{check.field} = {value:g}, {_verdict(inside, check.min, check.max)}"


def _observation(
    check: ObservationCheck, observations: list[Mapping[str, Any]], as_of: date
) -> Decision | None:
    candidates = []
    for item in observations:
        when = _date(item.get("date"))
        if not _code_matches(item.get("code"), check.codes) or when is None or when > as_of:
            continue
        if check.within_days is not None and (as_of - when).days > check.within_days:
            continue
        candidates.append((when, item))
    if not candidates:
        return None
    when, latest = max(candidates, key=lambda pair: pair[0])
    value, unit = latest.get("value"), latest.get("unit")
    if not isinstance(value, int | float) or isinstance(value, bool):
        return "unknown", f"{latest.get('code')} on {when} has no numeric value"
    shown = f"{latest.get('code')} {value:g} {unit or ''}".strip()
    if check.unit is not None and unit not in (None, check.unit):
        conversion = check.convert.get(str(unit))
        if conversion is None:
            return "unknown", f"{shown} on {when}: unit {unit!r} not recognised"
        value = value * conversion.multiply + conversion.add
        shown += f" (= {value:.2f} {check.unit})"
    inside = (check.min is None or value >= check.min) and (check.max is None or value <= check.max)
    label = check.in_range if inside else check.out_of_range
    age = (as_of - when).days
    verdict = _verdict(inside, check.min, check.max)
    return label, f"{shown} on {when} ({age} d before screening), {verdict}"


def _condition(
    check: ConditionCheck, conditions: list[Mapping[str, Any]], as_of: date
) -> Decision | None:
    undated = None
    for item in conditions:
        if not _code_matches(item.get("code"), check.codes):
            continue
        if check.statuses is not None and item.get("status") not in check.statuses:
            continue
        name = f"{item.get('code')} {item.get('text') or ''}".strip()
        if check.within_days is None:
            return check.then, f"condition {name} ({item.get('status') or 'status not given'})"
        onset = _date(item.get("onset"))
        if onset is None:
            undated = name
            continue
        if 0 <= (as_of - onset).days <= check.within_days:
            return check.then, f"condition {name} with onset {onset}"
    if undated is not None:
        return "unknown", f"condition {undated} has no onset date"
    return None


def _medication(
    check: MedicationCheck, medications: list[Mapping[str, Any]], as_of: date
) -> Decision | None:
    for item in medications:
        name = str(item.get("name") or "")
        named = any(n.lower() in name.lower() for n in check.names)
        if not (_code_matches(item.get("code"), check.codes) or named):
            continue
        status = item.get("status")
        end = _date(item.get("end"))
        recent = check.within_days is not None and (
            end is not None and 0 <= (as_of - end).days <= check.within_days
        )
        if status not in check.statuses and not recent:
            continue
        shown = f"{name or item.get('code')} ({status})"
        if check.min_days is None:
            return check.then, f"medication {shown}"
        start = _date(item.get("start"))
        if start is None:
            return "unknown", f"medication {shown} has no start date"
        days = (as_of - start).days
        if days < check.min_days:
            return check.too_recent, f"medication {shown} started {start}, {days} d ago"
        return check.then, f"medication {shown} since {start} ({days} d)"
    return None


def _exists(check: ExistsCheck, items: list[Mapping[str, Any]], as_of: date) -> Decision | None:
    undated = None
    for item in items:
        code = item.get(check.code_field)
        if not _code_matches(code, check.codes):
            continue
        if check.within_days is None:
            return check.then, f"{check.source}: {code} on file"
        when = _date(item.get(check.date_field))
        if when is None:
            undated = code
            continue
        if 0 <= (as_of - when).days <= check.within_days:
            return check.then, f"{check.source}: {code} on {when} ({(as_of - when).days} d ago)"
    if undated is not None:
        return check.undated, f"{check.source}: {undated} has no date"
    return None


def _value(check: ValueCheck, inputs: Mapping[str, Any]) -> Decision | None:
    value = get_path(inputs, check.field)
    if isinstance(value, str) and value in check.values:
        return check.then, f"{check.field} = {value}"
    return None


def _code_matches(code: Any, patterns: list[str]) -> bool:
    if not isinstance(code, str):
        return False
    return any(
        code.startswith(pattern[:-1]) if pattern.endswith("*") else code == pattern
        for pattern in patterns
    )


def _items(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _date(value: Any) -> date | None:
    """A FHIR date or dateTime; partial dates are placed mid-period."""
    if not isinstance(value, str) or len(value) < 4:
        return None
    parts = value[:10].split("-")
    try:
        if len(parts) == 1:
            return date(int(parts[0]), 7, 1)
        if len(parts) == 2:
            return date(int(parts[0]), int(parts[1]), 15)
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


def _verdict(inside: bool, low: float | None, high: float | None) -> str:
    bounds = f"[{'-∞' if low is None else f'{low:g}'}, {'∞' if high is None else f'{high:g}'}]"
    return f"{'within' if inside else 'outside'} {bounds}"
