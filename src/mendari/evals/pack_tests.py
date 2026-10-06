"""Run the behaviour tests a pack ships in `tests/cases.yaml`."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any

from mendari.core import input_schema
from mendari.core.decision import Decision
from mendari.core.origin import shape_problem
from mendari.core.pack import Pack, not_json
from mendari.core.patterns import COMMUNITY_TIME_BUDGET_S, OutOfTime, time_budget
from mendari.errors import PackValidationError


@dataclass(frozen=True)
class CaseOutcome:
    name: str
    passed: bool
    failures: tuple[str, ...]


def load_cases(pack: Pack) -> list[dict[str, Any]]:
    """The pack's behaviour tests. Each is a mapping with an `input` mapping, an optional `name`
    (text) and an optional `expect` mapping, whose `answers` (if any) is a mapping and whose
    `outcome_in` (if any) a list, all JSON; any other case is a `PackValidationError` naming
    the case by number and what is wrong (never its text)."""
    if pack.manifest.tests is None:
        return []
    cases = pack.read_yaml(pack.manifest.tests) or []
    if not isinstance(cases, list):
        raise PackValidationError(pack.id, [f"{pack.manifest.tests}: expected a list of cases"])
    problems = [
        f"{pack.manifest.tests} case {number}: {problem}"
        for number, case in enumerate(cases, start=1)
        if (problem := _case_problem(case)) is not None
    ]
    if problems:
        raise PackValidationError(pack.id, problems)
    return cases


def _case_problem(case: Any) -> str | None:
    if not isinstance(case, dict):
        return "not a mapping (a case has an `input`, and may have a `name` and an `expect`)"
    if not isinstance(case.get("input"), dict):
        return "`input` must be a mapping"
    if not isinstance(case.get("name", ""), str):
        return "`name` must be text"
    expect = case.get("expect", {})
    if not isinstance(expect, dict):
        return "`expect` must be a mapping"
    if not isinstance(expect.get("answers", {}), dict):
        return "`expect.answers` must be a mapping of question to label"
    if not isinstance(expect.get("outcome_in", []), list):
        return "`expect.outcome_in` must be a list"
    what = not_json(case)
    return None if what is None else f"not JSON, it holds {what}"


def validate_pack(pack: Pack) -> None:
    """`mendari validate`: every provider variant builds, the FHIR mapping (if any) loads, and
    the behaviour tests' inputs match the input schema; a `PackValidationError` otherwise."""
    from mendari.providers import build_provider

    for slot, spec in pack.manifest.providers.items():
        for variant in spec.variants:
            build_provider(pack, slot, variant)
    if pack.manifest.inputs.fhir:
        from mendari.standards.fhir import FhirAdapter

        FhirAdapter.for_pack(pack)  # parse and check the mapping, not only that it exists
    problems = input_problems(pack)
    if problems:
        raise PackValidationError(pack.id, problems)


def input_problems(pack: Pack) -> list[str]:
    """Behaviour-test inputs must match the input schema, unless the test expects a BLOCK;
    otherwise the test decides a BLOCK and fails confusingly (e.g. unquoted YAML flow-mapping
    text split into extra keys)."""
    validator = input_schema.validator(pack.input_schema, formats=False, community=pack.community)
    problems = []
    for number, case in enumerate(load_cases(pack), start=1):
        if (case.get("expect") or {}).get("outcome") == "BLOCK":
            continue  # a test of the input contract itself
        where = f"{pack.manifest.tests} case {number} ({case.get('name', 'unnamed')})"
        shape = shape_problem(case.get("input")) if pack.community else None
        if shape is not None:
            problems.append(f"{where}: its input is too large: {shape}")
            continue
        try:  # a community pack's schema runs within the time budget a decision gives it
            with time_budget() if pack.community else nullcontext():
                for error in validator.iter_errors(case.get("input")):
                    problems.append(f"{where}: input {error.message}")
        except OutOfTime as error:
            problems.append(f"{where}: checking its input against the schema stopped: {error}")
        except (RecursionError, OverflowError):
            problems.append(
                f"{where}: the schema cannot check its input: it is nested too deep, or holds a "
                "number too large"
            )
    return problems


def run_pack_tests(decision: Decision) -> list[CaseOutcome]:
    return [_run(decision, case) for case in load_cases(decision.pack)]


def _run(decision: Decision, case: dict[str, Any]) -> CaseOutcome:
    result = decision.decide(case["input"])
    expect = case.get("expect", {})
    failures = []

    def check(label: str, actual: Any, ok: bool, wanted: Any) -> None:
        if not ok:
            failures.append(f"{label}: expected {wanted!r}, got {actual!r}")

    outcome = result.outcome.value
    if "outcome" in expect:
        check("outcome", outcome, outcome == expect["outcome"], expect["outcome"])
    if "outcome_in" in expect:
        check("outcome", outcome, outcome in expect["outcome_in"], expect["outcome_in"])
    if "not_outcome" in expect:
        check("outcome", outcome, outcome != expect["not_outcome"], f"not {expect['not_outcome']}")
    if "recommendation" in expect:
        wanted = expect["recommendation"]
        check("recommendation", result.recommendation, result.recommendation == wanted, wanted)
    if "priority" in expect:
        actual = result.priority.value
        check("priority", actual, actual == expect["priority"], expect["priority"])
    for question, wanted in expect.get("answers", {}).items():
        answer = result.answers.get(question)
        label = answer.label if answer else None
        check(f"answers.{question}", label, label == wanted, wanted)
    if failures and result.errors:  # why a provider gave no answer, e.g. `time_budget`
        failures.append(f"provider errors: {dict(sorted(result.errors.items()))}")
        if "time_budget" in result.errors.values():
            failures.append(
                f"its patterns or schema checks ran out of the {COMMUNITY_TIME_BUDGET_S:g} s time "
                "budget a community pack has in each decision: shorten long counts and classes "
                "repeated many times"
            )
    return CaseOutcome(case.get("name", "unnamed case"), not failures, tuple(failures))
