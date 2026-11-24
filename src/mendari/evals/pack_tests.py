# SPDX-License-Identifier: Apache-2.0
"""Run the behaviour tests a pack ships in `tests/cases.yaml`, and the regression tests
`mendari probe` wrote beside them (`PROBE_TESTS`)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mendari.core.decision import Decision
from mendari.core.origin import bounded_reader
from mendari.core.pack import Pack, not_json
from mendari.core.patterns import COMMUNITY_TIME_BUDGET_S
from mendari.core.spec import Outcome, PackManifest, Priority
from mendari.errors import PackValidationError

# What a behaviour test may say (spec §10): a case is these keys, and its `expect` at least one of
# EXPECT_KEYS and nothing else, so a typo or an empty expectation cannot pass by accident (LP-15).
CASE_KEYS = ("name", "input", "expect")
EXPECT_KEYS = ("outcome", "outcome_in", "not_outcome", "recommendation", "priority", "answers")
# Where `mendari probe --write` keeps the failures it found, as behaviour tests
# (`evals/probe_tests.py`). The file has this one place in every agent and no manifest names it:
# an agent that holds it is still an agent to an engine from before the probe, which runs the
# agent's own tests and leaves this file unread. Each of its cases is a behaviour test with one
# more key, `probe`: where the case came from, its `id` at least.
PROBE_TESTS = "tests/probe.yaml"
PROBE_KEY = "probe"
# And where it keeps the tests whose expected answer a model changed, until a person confirms
# each (`mendari probe --confirm`): the same cases, never run and held to nothing while here.
PROBE_UNCONFIRMED = "tests/probe-unconfirmed.yaml"


@dataclass(frozen=True)
class CaseOutcome:
    name: str
    passed: bool
    failures: tuple[str, ...]


def load_cases(pack: Pack) -> list[dict[str, Any]]:
    """The pack's own behaviour tests: the file its manifest names. Each is a mapping with an
    `input` mapping, an optional `name` (text) and an `expect` mapping saying what the pack must
    decide: at least one of `EXPECT_KEYS` and nothing else, each value one the pack can decide
    (an outcome, a priority, the primary question's label, a question's label), all JSON. Any
    other case is a `PackValidationError` naming the case by number and name and what is wrong
    (never its input)."""
    if pack.manifest.tests is None:
        return []
    return _load(pack, pack.manifest.tests, CASE_KEYS)


def probe_cases(pack: Pack) -> list[dict[str, Any]]:
    """The regression tests the probe found and a person or the probe kept (`PROBE_TESTS`), or
    none: each a behaviour test as `load_cases` reads one, with a `probe` mapping that holds its
    `id` (text, one case's only). A test still waiting for a person to confirm what it expects
    is in another file, and is never read here."""
    if not pack.path(PROBE_TESTS).is_file():
        return []
    own = pack.manifest.tests
    if own is not None and pack.path(own) == pack.path(PROBE_TESTS):
        return []  # the manifest names this file as the agent's own tests: read once, as those
    cases = _load(pack, PROBE_TESTS, (*CASE_KEYS, PROBE_KEY))
    first: dict[str, int] = {}
    for number, case in enumerate(cases, start=1):
        found = first.setdefault(case[PROBE_KEY]["id"], number)
        if found != number:
            where = f"{PROBE_TESTS} case {number}{_named(case)}"
            problem = f"{where}: its id {case[PROBE_KEY]['id']} is case {found}'s too"
            raise PackValidationError(pack.id, [problem])
    return cases


def every_case(pack: Pack) -> list[tuple[str, int, dict[str, Any]]]:
    """Every behaviour test `mendari test` runs, as (the file it is in, its number there, the
    case): the pack's own (`load_cases`), then the ones the probe found (`probe_cases`)."""
    own = [(str(pack.manifest.tests), case) for case in load_cases(pack)]
    found = [(PROBE_TESTS, case) for case in probe_cases(pack)]
    return [
        (file, number, case)
        for cases in (own, found)
        for number, (file, case) in enumerate(cases, start=1)
    ]


def _load(pack: Pack, relative: str, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    """The cases of one file of behaviour tests, each holding `keys` and no other."""
    cases = pack.read_yaml(relative) or []
    if not isinstance(cases, list):
        raise PackValidationError(pack.id, [f"{relative}: expected a list of cases"])
    problems = []
    for number, case in enumerate(cases, start=1):
        problem = _case_problem(case, keys)
        if problem is None:
            problem = _value_problem(pack.manifest, case["expect"])
        if problem is not None:
            problems.append(f"{relative} case {number}{_named(case)}: {problem}")
    if problems:
        raise PackValidationError(pack.id, problems)
    return cases


def _named(case: Any) -> str:
    """A case's name as a problem shows it after the case's number, if it has one."""
    name = case.get("name") if isinstance(case, dict) else None
    return f" ({name})" if isinstance(name, str) and name else ""


def _case_problem(case: Any, keys: tuple[str, ...] = CASE_KEYS) -> str | None:
    if not isinstance(case, dict):
        return "not a mapping (a case has an `input`, an `expect` and may have a `name`)"
    extra = sorted(str(key) for key in case if key not in keys)
    if extra:
        listed = ", ".join(f"`{key}`" for key in keys[:-1]) + f" and `{keys[-1]}`"
        return f"unknown key {extra[0]!r} (a case has {listed})"
    if not isinstance(case.get("input"), dict):
        return "`input` must be a mapping"
    if not isinstance(case.get("name", ""), str):
        return "`name` must be text"
    if "expect" not in case:
        return f"no `expect`: say what the agent must decide ({', '.join(EXPECT_KEYS)})"
    expect = case["expect"]
    if not isinstance(expect, dict):
        return "`expect` must be a mapping"
    if not expect:
        return f"`expect` is empty: say what the agent must decide ({', '.join(EXPECT_KEYS)})"
    unknown = sorted(str(key) for key in expect if key not in EXPECT_KEYS)
    if unknown:
        return f"unknown `expect` key {unknown[0]!r} (allowed: {', '.join(EXPECT_KEYS)})"
    if not isinstance(expect.get("answers", {}), dict):
        return "`expect.answers` must be a mapping of question to label"
    if "answers" in expect and not expect["answers"]:
        return "`expect.answers` is empty: name at least one question and its label"
    if not isinstance(expect.get("outcome_in", []), list):
        return "`expect.outcome_in` must be a list"
    if "outcome_in" in expect and not expect["outcome_in"]:
        return "`expect.outcome_in` is empty: list the outcomes the test accepts"
    if PROBE_KEY in keys:
        if PROBE_KEY not in case:
            return (
                f"no `{PROBE_KEY}`: a test in this file says where `mendari probe` found it "
                "(its `id` at least)"
            )
        found = case[PROBE_KEY]
        if not isinstance(found, dict) or not isinstance(found.get("id"), str) or not found["id"]:
            return f"`{PROBE_KEY}` must be a mapping with an `id`"
    what = not_json(case)
    return None if what is None else f"not JSON, it holds {what}"


def _value_problem(manifest: PackManifest, expect: dict[str, Any]) -> str | None:
    """A value the pack can never decide: a test that expects it can never pass, and one that
    expects not to see it (`not_outcome`) always passes."""
    outcomes = [o.value for o in Outcome]
    named = [expect[key] for key in ("outcome", "not_outcome") if key in expect]
    for wanted in [*named, *expect.get("outcome_in", [])]:
        if wanted not in outcomes:
            return f"outcome {wanted!r} is not one of {', '.join(outcomes)}"
    priorities = [p.value for p in Priority]
    if "priority" in expect and expect["priority"] not in priorities:
        return f"priority {expect['priority']!r} is not one of {', '.join(priorities)}"
    primary = manifest.policy.primary
    labels = manifest.labels(primary)
    wanted = expect.get("recommendation")
    if "recommendation" in expect and wanted is not None and wanted not in labels:
        return f"recommendation {wanted!r} is not a label of {primary!r} ({', '.join(labels)})"
    for question, label in expect.get("answers", {}).items():
        if question not in manifest.questions:
            return f"`expect.answers` names {question!r}, which is not one of the agent's questions"
        if label not in manifest.labels(question):
            return (
                f"`expect.answers.{question}` is {label!r}, not one of its labels "
                f"({', '.join(manifest.labels(question))})"
            )
    return None


def validate_pack(pack: Pack) -> None:
    """`mendari validate` (`evals/validation.py`, the one validation function, LP-20b)."""
    from mendari.evals.validation import validate

    validate(pack)


def dataset_problems(pack: Pack) -> list[str]:
    """`validation.dataset_problems`, as lines."""
    from mendari.evals.validation import dataset_problems

    return [str(problem) for problem in dataset_problems(pack)]


def input_problems(pack: Pack) -> list[str]:
    """`validation.input_problems`, as lines."""
    from mendari.evals.validation import input_problems

    return [str(problem) for problem in input_problems(pack)]


def run_pack_tests(decision: Decision) -> list[CaseOutcome]:
    return [_run(decision, case) for _, _, case in every_case(decision.pack)]


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
            reader = bounded_reader(decision.pack.id, decision.pack.origin)
            failures.append(
                f"its patterns or schema checks ran out of the {COMMUNITY_TIME_BUDGET_S:g} s time "
                f"budget {reader} has in each decision: shorten long counts and classes repeated "
                "many times"
            )
    return CaseOutcome(case.get("name", "unnamed case"), not failures, tuple(failures))
