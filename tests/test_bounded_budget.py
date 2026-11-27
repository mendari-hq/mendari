# SPDX-License-Identifier: Apache-2.0
"""The patterns of an agent the deployment does not own share one time budget per decision
(`core/patterns.py`).

Such an agent (here, one pulled from a registry whose digest is no bundled agent's:
`tests/support/engine.py`) runs on the bounded engine. RE2 is linear and the caps bound how many
patterns run and how large they are, but an agent within every cap could still take 5 s on
10,000 characters of emoji, or 17 s with negated patterns (the focused check of B11), and its
schema minutes. The budget, in the decision thread's CPU time, makes the bound hard: once spent,
the agent's rules or its schema check stop (`time_budget`) and the decision abstains, for a
person to decide.
"""

from __future__ import annotations

import copy
import time
from pathlib import Path
from typing import Any

import pytest

from mendari import Decision, load_agent
from mendari.core import patterns
from mendari.core.origin import Origin
from mendari.core.spec import Outcome
from mendari.errors import AgentValidationError
from mendari.evals.behaviour_tests import _run
from tests.support.engine import SORTER_SCHEMA, pulled, sorter

EMOJI = "\U0001f600" * 10_000
# Each takes about half a second on EMOJI and the nine stay within every cap (2,700 of 3,000).
SLOW = [rf"\W{{1,299}}{i}" for i in range(9)]
# The budget (1 s of CPU), one step under way (about 2.5 s at worst) and headroom for a busy
# runner: before the budget these took 5.5 s, 17.5 s and minutes.
BOUND_S = 6.0


def long_message(schema: dict[str, Any]) -> None:
    schema["properties"]["message"].pop("maxLength")


def slow_rules(*, negatable: bool = False) -> Any:
    def edit(rules: dict[str, Any]) -> None:
        if negatable:
            rules["negation_cues"] = ["no"]
            rules["negation_window"] = 200
            found = [rf"\W{{1,299}}b{i}|\U0001f600" for i in range(9)]
        else:
            found = SLOW
        rules["questions"]["topic"]["labels"]["billing"] = [
            {"name": f"slow{i}", "pattern": p, "weight": 8, "negatable": negatable}
            for i, p in enumerate(found)
        ]

    return edit


def timed(agent: Any, inputs: dict[str, Any]) -> tuple[Any, float]:
    started = time.monotonic()
    result = Decision(agent).decide(inputs)
    return result, time.monotonic() - started


def test_a_bounded_agents_rules_stop_at_the_budget_and_a_person_decides(tmp_path: Path) -> None:
    """The rules fail as any provider that cannot answer does: the decision abstains, with no
    recommendation, and a person decides."""
    agent = pulled(sorter(tmp_path, rules=slow_rules(), schema=long_message))
    assert agent.bounded
    result, took = timed(agent, {"message": EMOJI})
    assert took < BOUND_S
    assert result.outcome is Outcome.ABSTAIN and result.recommendation is None
    assert set(result.errors.values()) == {"time_budget"}


def test_negated_patterns_stop_at_the_budget_too(tmp_path: Path) -> None:
    """Before, each negated match made RE2 scan ahead again: 17.5 s within every cap."""
    agent = pulled(sorter(tmp_path, rules=slow_rules(negatable=True), schema=long_message))
    text = (("no " + "\U0001f600" * 190 + " ") * 60)[:10_000]
    result, took = timed(agent, {"message": text})
    assert took < BOUND_S
    assert set(result.errors.values()) == {"time_budget"}


def stopped_for_a_person(result: Any) -> bool:
    """The schema check ran out: not the input's fault, so no refusal; a person decides."""
    return (
        result.outcome is Outcome.ABSTAIN
        and set(result.errors.values()) == {"time_budget"}
        and not any(reason.startswith("input rejected") for reason in result.reasons)
    )


def test_a_bounded_agents_schema_patterns_stop_at_the_budget(tmp_path: Path) -> None:
    """The input fails the first patterns (no `0` in it): those errors refuse it, and the rest
    are not searched once the budget is spent."""

    def slow_schema(schema: dict[str, Any]) -> None:
        long_message(schema)
        schema["properties"]["message"]["allOf"] = [{"pattern": p} for p in SLOW]

    agent = pulled(sorter(tmp_path, schema=slow_schema))
    result, took = timed(agent, {"message": EMOJI})
    assert took < BOUND_S and result.outcome is Outcome.BLOCK
    assert 0 < len(result.reasons) < len(SLOW)
    assert all("does not match the pattern" in reason for reason in result.reasons)


def test_a_schema_that_fans_out_without_patterns_stops_at_the_budget(tmp_path: Path) -> None:
    """`$ref`s that double at each level, with no pattern: 53 s for one tiny input before."""

    def fan_out(schema: dict[str, Any]) -> None:
        schema["properties"]["note"] = {"type": "string", "$ref": "#/$defs/t0"}
        schema["$defs"] = {
            f"t{i}": {"allOf": [{"$ref": f"#/$defs/t{i + 1}"}, {"$ref": f"#/$defs/t{i + 1}"}]}
            for i in range(24)
        } | {"t24": {"minLength": 1}}

    agent = pulled(sorter(tmp_path, schema=fan_out))
    assert timed(agent, {"message": "my bill"})[1] < 1  # without the field, nothing fans out
    result, took = timed(agent, {"message": "my bill", "note": "x"})
    assert took < BOUND_S and stopped_for_a_person(result)


def test_an_enum_is_short_and_stops_at_the_budget_on_many_items(tmp_path: Path) -> None:
    """Each item is compared with every listed value: 8 s for a 30 KB body before. An enum lists
    at most 500 values now, and the values an input holds are capped."""

    def codes(listed: list[int]) -> Any:
        def edit(schema: dict[str, Any]) -> None:
            schema["properties"]["codes"] = {"type": "array", "items": {"enum": listed}}

        return edit

    with pytest.raises(AgentValidationError) as error:
        pulled(sorter(tmp_path / "long", schema=codes(list(range(501)))))
    assert any("lists 501 values, at most 500" in problem for problem in error.value.problems)
    agent = pulled(sorter(tmp_path / "short", schema=codes(list(range(500)))))
    result, took = timed(agent, {"message": "my bill", "codes": [499] * 9_990})
    assert took < BOUND_S and result.outcome is not Outcome.BLOCK


@pytest.mark.parametrize(
    ("keyword", "value"),
    [
        ("uniqueItems", True),
        ("uniqueItems", False),
        ("contains", {"enum": [1]}),
        ("dependentRequired", {"a": ["b"]}),
        ("$dynamicRef", "#/$defs/x"),
        ("definitions", {}),
    ],
)
def test_a_bounded_agents_schema_uses_only_the_keywords_mendari_bounds(
    tmp_path: Path, keyword: str, value: Any
) -> None:
    """`contains` applied its subschema to every item outside the budget (48 s), `uniqueItems`
    compares every pair at once (40 s): only keywords whose work is bounded are allowed."""

    def used(schema: dict[str, Any]) -> None:
        schema["$defs"] = {"x": {"type": "integer"}}
        schema["properties"]["codes"] = {"type": "array", keyword: value}

    with pytest.raises(AgentValidationError) as error:
        pulled(sorter(tmp_path, schema=used))
    assert any(
        f"properties/codes/{keyword}: a registry or store agent's schema may not use {keyword}"
        in problem
        for problem in error.value.problems
    )


@pytest.mark.parametrize(
    "reference",
    ["#", "#/$defs/h/default", "#/properties/message", "#/$defs/missing"],
)
def test_a_bounded_agents_ref_names_one_of_its_defs(tmp_path: Path, reference: str) -> None:
    """A `$ref` into data (`default`, `const`) made it a schema, out of this check's sight, and
    one to the root brought back jsonschema's own validator."""

    def referred(schema: dict[str, Any]) -> None:
        schema["$defs"] = {"h": {"type": "object", "default": {"uniqueItems": True}}}
        schema["properties"]["meta"] = {"$ref": reference}

    with pytest.raises(AgentValidationError) as error:
        pulled(sorter(tmp_path, schema=referred))
    expected = "is not in the schema" if reference.endswith("missing") else "a $ref names one"
    assert any(expected in problem for problem in error.value.problems)


@pytest.mark.parametrize(
    ("keyword", "value", "problem"),
    [
        ("required", [f"n{i}" for i in range(101)], "lists 101 names, at most 100"),
        ("enum", [{"a": 1}], "lists an object or a list"),
        ("const", {"a": 1}, "is an object or a list"),
    ],
)
def test_lists_and_values_a_schema_compares_at_once_are_short_and_flat(
    tmp_path: Path, keyword: str, value: Any, problem: str
) -> None:
    """One `required` application yielded an error per missing name (27 s, 7.9 MB of reasons)."""

    def listed(schema: dict[str, Any]) -> None:
        schema["properties"]["meta"] = {"type": "object", keyword: value}

    with pytest.raises(AgentValidationError) as error:
        pulled(sorter(tmp_path, schema=listed))
    assert any(problem in found for found in error.value.problems)


def test_the_budget_counts_the_decisions_own_cpu_time_not_waiting() -> None:
    """Waiting (a model's answer, another request) costs nothing: before, a slow model call
    ahead of the rules made them fail on ordinary text, and load changed outcomes."""
    with patterns.time_budget(0.2):
        time.sleep(0.5)
        patterns.check_time()
        with pytest.raises(patterns.OutOfTime):
            while True:
                patterns.check_time()


def test_an_agent_test_that_runs_out_of_time_says_so(tmp_path: Path) -> None:
    agent = pulled(sorter(tmp_path, rules=slow_rules(), schema=long_message))
    case = {"name": "slow", "input": {"message": EMOJI}, "expect": {"recommendation": "billing"}}
    failures = _run(Decision(agent), case).failures
    assert any("time_budget" in failure for failure in failures)
    assert any("ran out of the 1 s time budget" in failure for failure in failures)


def test_the_budget_belongs_to_one_decision(tmp_path: Path) -> None:
    """Spent in one decision, it is not in the next; outside a decision there is none."""
    agent = pulled(sorter(tmp_path, rules=slow_rules(), schema=long_message))
    decision = Decision(agent)
    assert set(decision.decide({"message": EMOJI}).errors.values()) == {"time_budget"}
    assert patterns._DEADLINE.get() is None
    quick = decision.decide({"message": "a question about my bill !0"})  # `\W{1,299}0`
    assert quick.errors == {} and quick.recommendation == "billing"
    patterns.check_time()  # no budget, no limit


def test_an_agent_that_is_not_bounded_has_no_budget(tmp_path: Path) -> None:
    """The same agent as the operator's own: its patterns run on `re`, as before, with no budget
    (a short text: `re` would take long on the slow one)."""
    root = sorter(tmp_path, rules=slow_rules(), schema=long_message, agent_id="someone/sorter")
    result = Decision(load_agent(root)).decide({"message": "a question about my bill 0"})
    assert not load_agent(root).bounded and result.errors == {}


def test_the_budget_is_spent_once_and_restored() -> None:
    with patterns.time_budget(0):
        sum(range(100_000))  # a little CPU
        with pytest.raises(patterns.OutOfTime):
            patterns.check_time()
        with patterns.time_budget(60):
            patterns.check_time()  # an inner budget of its own
        with pytest.raises(patterns.OutOfTime):
            patterns.check_time()
    patterns.check_time()


def fan_out(depth: int) -> dict[str, Any]:
    """`$defs/t{i}` is allOf two `$ref`s to `t{i+1}`: the leaf applies 2**depth times."""
    defs = {
        f"t{i}": {"allOf": [{"$ref": f"#/$defs/t{i + 1}"}, {"$ref": f"#/$defs/t{i + 1}"}]}
        for i in range(depth)
    }
    return defs | {f"t{depth}": {"minLength": 1}}


@pytest.mark.parametrize("keyword", ["unevaluatedProperties", "unevaluatedItems"])
def test_keywords_jsonschema_walks_on_its_own_are_refused(tmp_path: Path, keyword: str) -> None:
    """jsonschema walks the schema for them outside any keyword: 43 s for a 53-byte input."""

    def walked(schema: dict[str, Any]) -> None:
        schema["properties"]["extra"] = {keyword: False, "$ref": "#/$defs/t0"}
        schema["$defs"] = fan_out(20)

    with pytest.raises(AgentValidationError) as error:
        pulled(sorter(tmp_path, schema=walked))
    assert any(f"may not use {keyword}" in problem for problem in error.value.problems)


def test_a_property_named_like_a_keyword_is_allowed(tmp_path: Path) -> None:
    def named(schema: dict[str, Any]) -> None:
        schema["properties"]["uniqueItems"] = {"type": "string", "x-note": "a name, not a keyword"}

    assert pulled(sorter(tmp_path, schema=named)).bounded


def test_a_bounded_agents_input_holds_a_bounded_number_of_values(tmp_path: Path) -> None:
    """Its schema applies to every value: a subschema with no keyword on 2 million items took
    15.7 s before (the budget never checked)."""

    def many(schema: dict[str, Any]) -> None:
        schema["properties"]["xs"] = {"type": "array", "items": {"x-note": 1}}

    agent = pulled(sorter(tmp_path, schema=many))
    result, took = timed(agent, {"message": "my bill", "xs": [1] * 2_000_000})
    assert took < BOUND_S and result.outcome is Outcome.BLOCK  # refused before any keyword
    assert result.reasons == (
        "input rejected: (root): more than 10,000 values, the most an agent from a registry reads",
    )
    assert timed(agent, {"message": "my bill", "xs": [1] * 9_000})[0].outcome is not Outcome.BLOCK


def test_a_bounded_agents_input_is_nested_at_most_32_deep(tmp_path: Path) -> None:
    """Before, 200 levels crashed the check (RecursionError, a 500)."""

    def nested(schema: dict[str, Any]) -> None:
        schema["properties"]["tree"] = {"$ref": "#/$defs/node"}
        node = {"type": "object", "properties": {"n": {"$ref": "#/$defs/node"}}}
        schema["$defs"] = {"node": node}

    deep: dict[str, Any] = {}
    for _ in range(200):
        deep = {"n": deep}
    result = Decision(pulled(sorter(tmp_path, schema=nested))).decide(
        {"message": "my bill", "tree": deep}
    )
    assert result.reasons == (
        "input rejected: (root): nested more than 32 deep, the most an agent from a registry reads",
    )


def test_the_bounded_validator_keeps_its_class_on_the_root(tmp_path: Path) -> None:
    """jsonschema picks a validator by the schema's `$schema`: evolving to the root (a `$ref`
    to it, now refused at load) brought back its own one, with patterns on `re`."""
    from mendari.core import input_schema

    schema = copy.deepcopy(SORTER_SCHEMA)
    validator = input_schema.validator(schema, bounded=True)
    assert isinstance(validator.schema, dict) and "$schema" not in validator.schema
    assert type(validator.evolve(schema=validator.schema)) is type(validator)


def test_a_number_past_checking_is_refused_not_a_crash(tmp_path: Path) -> None:
    """For every agent, pulled or the deployment's own: `multipleOf: 0.01` on a 4,000-digit
    integer raised OverflowError."""

    def money(schema: dict[str, Any]) -> None:
        schema["properties"]["amount"] = {"type": "number", "multipleOf": 0.01}

    for name, loaded in (("pulled", pulled), ("own", load_agent)):
        root = sorter(tmp_path / name, schema=money)
        result = Decision(loaded(root)).decide({"message": "my bill", "amount": 10**4000})
        assert result.outcome is Outcome.BLOCK
        refused = (
            "input rejected: (root): the schema cannot check it: it is nested too deep or holds "
            "a number too large, or the schema refers to itself without end"
        )
        assert result.reasons == (refused,)


def test_errors_found_before_the_budget_runs_out_still_refuse_the_input(tmp_path: Path) -> None:
    def checked_first(schema: dict[str, Any]) -> None:
        schema["properties"] = {
            "a": {"type": "integer"},
            **schema["properties"],
            "note": {"type": "string", "$ref": "#/$defs/t0"},
        }
        schema["$defs"] = fan_out(24)

    agent = pulled(sorter(tmp_path, schema=checked_first))
    result, took = timed(agent, {"a": "one", "message": "my bill", "note": "x"})
    assert took < BOUND_S and result.outcome is Outcome.BLOCK
    assert result.reasons == ("input rejected: a: must be of type integer",)


def test_mendari_validate_checks_test_inputs_within_the_budget(tmp_path: Path) -> None:
    from mendari.evals.behaviour_tests import input_problems

    def noted(schema: dict[str, Any]) -> None:
        schema["properties"]["note"] = {"type": "string", "$ref": "#/$defs/t0"}
        schema["$defs"] = fan_out(24)

    root = sorter(tmp_path, schema=noted)
    cases = root / "tests" / "cases.yaml"
    cases.write_text(
        cases.read_text() + '- name: noted\n  input: {message: "my bill", note: x}\n'
        "  expect: {recommendation: billing}\n"
    )
    started = time.monotonic()
    found = input_problems(pulled(root))
    assert time.monotonic() - started < BOUND_S
    assert any("checking its input against the schema stopped" in problem for problem in found)


@pytest.mark.parametrize("name", ["h/default", "h%2Fdefault", "h~1default", "a b"])
def test_a_defs_name_is_one_a_ref_names_exactly(tmp_path: Path, name: str) -> None:
    """A JSON pointer splits on `/` and reads `%2F` as one: a def named `h/default` let
    `#/$defs/h/default` reach the `default` of `h`, data no check walked (uniqueItems 44 s,
    patterns on `re`)."""

    def hidden(schema: dict[str, Any]) -> None:
        unique = {"type": "array", "uniqueItems": True}
        schema["$defs"] = {"h": {"type": "array", "default": unique}, name: {}}
        schema["properties"]["codes"] = {"$ref": f"#/$defs/{name}"}

    with pytest.raises(AgentValidationError) as error:
        pulled(sorter(tmp_path, schema=hidden))
    assert any(
        f"$defs/{name}: a $defs name is letters" in p or "is not in the schema" in p
        for p in error.value.problems
    )


def test_the_error_limit_and_short_reasons_are_a_bounded_agents_only(tmp_path: Path) -> None:
    """Every agent the deployment owns lists every missing name; like a bounded agent, it lists
    the reason once, not once per missing name (`tests/test_decision.py`)."""

    def many(schema: dict[str, Any]) -> None:
        schema["properties"]["meta"] = {"type": "object", "required": [f"n{i}" for i in range(9)]}

    outside = load_agent(sorter(tmp_path / "a", schema=many, agent_id="someone/sorter"))
    reasons = Decision(outside).decide({"message": "my bill", "meta": {}}).reasons
    assert reasons == (f"input rejected: meta: {', '.join(f'n{i}' for i in range(9))} is required",)
    bounded = pulled(sorter(tmp_path / "b", schema=many))
    reasons = Decision(bounded).decide({"message": "my bill", "meta": {}}).reasons
    assert reasons == ("input rejected: meta: n0, n1, n2, n3, n4 and 4 more is required",)


@pytest.mark.parametrize(
    ("keyword", "value"),
    [
        ("required", ["x" * 201]),
        ("enum", ["x" * 201]),
        ("const", "x" * 201),
    ],
)
def test_a_schemas_texts_are_short(tmp_path: Path, keyword: str, value: Any) -> None:
    """Long `required` names made 19 MB of reasons per request; long `enum` texts are copied
    into every error jsonschema makes."""

    def long(schema: dict[str, Any]) -> None:
        schema["properties"]["meta"] = {"type": "object", keyword: value}

    with pytest.raises(AgentValidationError) as error:
        pulled(sorter(tmp_path, schema=long))
    assert any("longer than 200 characters" in p for p in error.value.problems)


def test_validate_called_alone_holds_a_bounded_agents_text_to_its_length(
    tmp_path: Path,
) -> None:
    """`mendari data check` validates a dataset's inputs without deciding them: the length cap
    applies too."""
    agent = pulled(sorter(tmp_path, schema=long_message))
    assert Decision(agent).validate({"message": "x" * 10_001}) == [
        "message: longer than 10,000 characters, the most an agent from a registry reads"
    ]


def test_refused_keywords_say_what_to_write_instead(tmp_path: Path) -> None:
    def used(schema: dict[str, Any]) -> None:
        schema["properties"]["meta"] = {"type": "object", "dependentRequired": {"a": ["b"]}}
        schema["definitions"] = {}

    with pytest.raises(AgentValidationError) as error:
        pulled(sorter(tmp_path, schema=used))
    found = " ".join(error.value.problems)
    assert "dependentRequired" in found and "use if/then" in found and "use $defs" in found


def test_enum_and_const_errors_do_not_copy_the_schema_or_the_input() -> None:
    """jsonschema's own messages write out every allowed value for each failing value: inside
    `anyOf` on many items, one decision held about 225 MB. Same verdicts, short messages."""
    from mendari.core import input_schema

    listed = [f"{i:03d}" + "y" * 197 for i in range(500)]
    schema = {"type": "object", "properties": {"a": {"enum": listed}, "b": {"const": 1}}}
    validator = input_schema.validator(schema, bounded=True)
    errors = list(validator.iter_errors({"a": "z" * 9_000, "b": True}))
    assert sorted(e.message for e in errors) == [
        "is not one of the values the schema allows",
        "is not the value the schema requires",
    ]
    assert not list(validator.iter_errors({"a": listed[7], "b": 1.0}))  # 1.0 is 1, True is not


@pytest.mark.parametrize("origin", ["hub", "local"])
def test_a_root_id_that_is_not_text_is_refused(tmp_path: Path, origin: Origin) -> None:
    """Before, it failed the load with an AttributeError (on every agent, pulled or the
    deployment's own)."""
    with pytest.raises(AgentValidationError) as error:
        load_agent(sorter(tmp_path, schema=lambda s: s.update({"$id": 5})), origin=origin)
    assert "input schema: $id: $id is a text" in error.value.problems


@pytest.mark.parametrize(
    ("keyword", "words"),
    [
        ("example", "not one of the keywords the guide lists: use examples"),
        ("nullable", 'not one of the keywords the guide lists: use a type list with "null"'),
        ("vendorThing", "not one of the keywords the guide lists: an annotation of your own"),
        ("contains", "cannot keep within a decision's time budget: check it in a rule"),
    ],
)
def test_a_refused_keyword_says_why_and_what_instead(
    tmp_path: Path, keyword: str, words: str
) -> None:
    def used(schema: dict[str, Any]) -> None:
        schema["properties"]["meta"] = {"type": "object", keyword: {"enum": [1]}}

    with pytest.raises(AgentValidationError) as error:
        pulled(sorter(tmp_path, schema=used))
    assert any(words in problem for problem in error.value.problems), error.value.problems
