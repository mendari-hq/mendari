# SPDX-License-Identifier: Apache-2.0
"""Run the behaviour tests a pack ships in `tests/cases.yaml`."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any

from mendari.core import input_schema
from mendari.core.decision import Decision
from mendari.core.origin import bounded_reader, shape_problem
from mendari.core.pack import Pack, not_json
from mendari.core.patterns import COMMUNITY_TIME_BUDGET_S, OutOfTime, time_budget
from mendari.core.spec import Outcome, PackManifest, Priority
from mendari.errors import PackValidationError

# What a behaviour test may say (spec §10): a case is these keys, and its `expect` at least one of
# EXPECT_KEYS and nothing else, so a typo or an empty expectation cannot pass by accident (LP-15).
CASE_KEYS = ("name", "input", "expect")
EXPECT_KEYS = ("outcome", "outcome_in", "not_outcome", "recommendation", "priority", "answers")


@dataclass(frozen=True)
class CaseOutcome:
    name: str
    passed: bool
    failures: tuple[str, ...]


def load_cases(pack: Pack) -> list[dict[str, Any]]:
    """The pack's behaviour tests. Each is a mapping with an `input` mapping, an optional `name`
    (text) and an `expect` mapping saying what the pack must decide: at least one of
    `EXPECT_KEYS` and nothing else, each value one the pack can decide (an outcome, a priority,
    the primary question's label, a question's label), all JSON. Any other case is a
    `PackValidationError` naming the case by number and name and what is wrong (never its
    input)."""
    if pack.manifest.tests is None:
        return []
    cases = pack.read_yaml(pack.manifest.tests) or []
    if not isinstance(cases, list):
        raise PackValidationError(pack.id, [f"{pack.manifest.tests}: expected a list of cases"])
    problems = []
    for number, case in enumerate(cases, start=1):
        problem = _case_problem(case)
        if problem is None:
            problem = _value_problem(pack.manifest, case["expect"])
        if problem is not None:
            name = case.get("name") if isinstance(case, dict) else None
            named = f" ({name})" if isinstance(name, str) and name else ""
            problems.append(f"{pack.manifest.tests} case {number}{named}: {problem}")
    if problems:
        raise PackValidationError(pack.id, problems)
    return cases


def _case_problem(case: Any) -> str | None:
    if not isinstance(case, dict):
        return "not a mapping (a case has an `input`, an `expect` and may have a `name`)"
    extra = sorted(str(key) for key in case if key not in CASE_KEYS)
    if extra:
        return f"unknown key {extra[0]!r} (a case has `name`, `input` and `expect`)"
    if not isinstance(case.get("input"), dict):
        return "`input` must be a mapping"
    if not isinstance(case.get("name", ""), str):
        return "`name` must be text"
    if "expect" not in case:
        return f"no `expect`: say what the pack must decide ({', '.join(EXPECT_KEYS)})"
    expect = case["expect"]
    if not isinstance(expect, dict):
        return "`expect` must be a mapping"
    if not expect:
        return f"`expect` is empty: say what the pack must decide ({', '.join(EXPECT_KEYS)})"
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
            return f"`expect.answers` names {question!r}, which is not one of the pack's questions"
        if label not in manifest.labels(question):
            return (
                f"`expect.answers.{question}` is {label!r}, not one of its labels "
                f"({', '.join(manifest.labels(question))})"
            )
    return None


def validate_pack(pack: Pack) -> None:
    """`mendari validate`: every provider variant passes its closed config
    (`providers.config_problems`: what `load_pack` warns an owned pack of, `Pack.warnings`, and
    refuses a pack the deployment does not own for) and builds, the FHIR mapping (if any)
    loads, the behaviour tests' inputs match the input schema, and every declared dataset is one
    the pack can evaluate (`dataset_problems`); a `PackValidationError` otherwise."""
    from mendari.providers import build_provider, config_problems

    # Every variant's problems, each naming it; the trained files are read whatever the pack
    # was loaded with. Then what only a build finds: the deployment's model settings.
    problems = config_problems(pack)
    if problems:
        raise PackValidationError(pack.id, problems)
    for slot, spec in pack.manifest.providers.items():
        for variant in spec.variants:
            build_provider(pack, slot, variant)
    if pack.manifest.inputs.fhir:
        from mendari.standards.fhir import FhirAdapter

        FhirAdapter.for_pack(pack)  # parse and check the mapping, not only that it exists
    problems = input_problems(pack) + dataset_problems(pack)
    if problems:
        raise PackValidationError(pack.id, problems)


# At most this many problems are named per dataset; the rest are counted.
MAX_DATASET_PROBLEMS = 10


def dataset_problems(pack: Pack) -> list[str]:
    """Every dataset `evaluation.datasets` declares reads as JSON Lines, each input matches the
    input schema as a decision checks it, and each record's labels are the pack's questions and
    labels (`record_problems`). Problems name the file and the case id, never its text."""
    from mendari.errors import MendariError
    from mendari.evals.harness import record_problems, resolve_dataset

    decision = Decision(pack)
    problems: list[str] = []
    for name, path in pack.manifest.evaluation.datasets.items():
        try:
            dataset = resolve_dataset(pack, name)
        except MendariError as error:
            problems.append(str(error))
            continue
        found = [f"{path}: {problem}" for problem in record_problems(pack.manifest, dataset)]
        for record in dataset.records:
            refused = decision.validate(record["input"])
            if refused:
                found.append(f"{path} case {record['id']}: input {refused[0]}")
        problems += found[:MAX_DATASET_PROBLEMS]
        if len(found) > MAX_DATASET_PROBLEMS:
            problems.append(f"{path}: and {len(found) - MAX_DATASET_PROBLEMS} more problems")
    return problems


def input_problems(pack: Pack) -> list[str]:
    """Behaviour-test inputs must match the input schema, unless the test expects a BLOCK;
    otherwise the test decides a BLOCK and fails confusingly (e.g. unquoted YAML flow-mapping
    text split into extra keys)."""
    bounded = pack.bounded  # on the community engine, as in a decision
    reader = bounded_reader(pack.id, pack.origin)
    validator = input_schema.validator(pack.input_schema, formats=False, community=bounded)
    problems = []
    for number, case in enumerate(load_cases(pack), start=1):
        if (case.get("expect") or {}).get("outcome") == "BLOCK":
            continue  # a test of the input contract itself
        where = f"{pack.manifest.tests} case {number} ({case.get('name', 'unnamed')})"
        shape = shape_problem(case.get("input"), reader) if bounded else None
        if shape is not None:
            problems.append(f"{where}: its input is too large: {shape}")
            continue
        try:  # such a pack's schema runs within the time budget a decision gives it
            with time_budget() if bounded else nullcontext():
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
            reader = bounded_reader(decision.pack.id, decision.pack.origin)
            failures.append(
                f"its patterns or schema checks ran out of the {COMMUNITY_TIME_BUDGET_S:g} s time "
                f"budget {reader} has in each decision: shorten long counts and classes repeated "
                "many times"
            )
    return CaseOutcome(case.get("name", "unnamed case"), not failures, tuple(failures))
