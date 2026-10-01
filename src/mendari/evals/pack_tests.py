"""Run the behaviour tests a pack ships in `tests/cases.yaml`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mendari.core.decision import Decision
from mendari.core.pack import Pack
from mendari.errors import PackValidationError


@dataclass(frozen=True)
class CaseOutcome:
    name: str
    passed: bool
    failures: tuple[str, ...]


def load_cases(pack: Pack) -> list[dict[str, Any]]:
    """The pack's behaviour tests; each has a `name`, an `input` and an `expect`."""
    if pack.manifest.tests is None:
        return []
    cases = pack.read_yaml(pack.manifest.tests) or []
    if not isinstance(cases, list):
        raise PackValidationError(pack.id, [f"{pack.manifest.tests}: expected a list of cases"])
    return cases


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
    return CaseOutcome(case.get("name", "unnamed case"), not failures, tuple(failures))
