"""Run the behaviour tests a pack ships in `tests/cases.yaml`."""

from __future__ import annotations

import json
import math
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
    problems = [
        f"{pack.manifest.tests} case {number}: not JSON, it holds {what}"
        for number, case in enumerate(cases, start=1)
        if (what := _not_json(case)) is not None
    ]
    if problems:
        raise PackValidationError(pack.id, problems)
    return cases


def _not_json(value: Any) -> str | None:
    """What in `value` a JSON response cannot carry (encoded as UTF-8, numbers finite), named
    by its type, or None. YAML builds dates, bytes (`!!binary`), sets and strings with a lone
    surrogate (`"\\ud800"`), none of which an input, an example or a decision can hold. The text
    is never part of the answer: it is whatever the pack wrote."""
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except RecursionError:
        return "values nested too deep"
    except (TypeError, ValueError):  # UnicodeEncodeError is a ValueError
        pass
    else:
        return None
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item)  # the keys
            stack.extend(item.values())
        elif isinstance(item, list | tuple):
            stack.extend(item)
        elif isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeEncodeError:
                return "a string with a lone surrogate"
        elif isinstance(item, float) and not math.isfinite(item):
            return "a number that is not finite"
        elif not (item is None or isinstance(item, bool | int | float)):
            return f"a value of type {type(item).__name__}"
    return "a value JSON cannot hold"


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
