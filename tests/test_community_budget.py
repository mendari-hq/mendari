"""A community pack's patterns share one time budget per decision (`core/patterns.py`).

RE2 is linear and the caps bound how many patterns run and how large they are, but a pack within
every cap could still take 5 s on 10,000 characters of emoji, or 17 s with negated patterns
(the focused check of B11), and its schema minutes. The budget, in the decision thread's CPU time,
makes the bound hard: once spent, the pack's rules or its schema check stop (`time_budget`) and
the decision abstains, for a person to decide.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from mendari import Decision, load_pack
from mendari.core import patterns
from mendari.core.spec import Outcome
from mendari.errors import PackValidationError
from mendari.evals.pack_tests import _run
from tests.test_community_caps import sorter

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


def timed(pack: Any, inputs: dict[str, Any]) -> tuple[Any, float]:
    started = time.monotonic()
    result = Decision(pack).decide(inputs)
    return result, time.monotonic() - started


def test_a_community_packs_rules_stop_at_the_budget_and_a_person_decides(tmp_path: Path) -> None:
    """The rules fail as any provider that cannot answer does: the decision abstains, with no
    recommendation, and a person decides."""
    pack = load_pack(sorter(tmp_path, rules=slow_rules(), schema=long_message))
    assert pack.community
    result, took = timed(pack, {"message": EMOJI})
    assert took < BOUND_S
    assert result.outcome is Outcome.ABSTAIN and result.recommendation is None
    assert set(result.errors.values()) == {"time_budget"}


def test_negated_patterns_stop_at_the_budget_too(tmp_path: Path) -> None:
    """Before, each negated match made RE2 scan ahead again: 17.5 s within every cap."""
    pack = load_pack(sorter(tmp_path, rules=slow_rules(negatable=True), schema=long_message))
    text = (("no " + "\U0001f600" * 190 + " ") * 60)[:10_000]
    result, took = timed(pack, {"message": text})
    assert took < BOUND_S
    assert set(result.errors.values()) == {"time_budget"}


def stopped_for_a_person(result: Any) -> bool:
    """The schema check ran out: not the input's fault, so no refusal; a person decides."""
    return (
        result.outcome is Outcome.ABSTAIN
        and set(result.errors.values()) == {"time_budget"}
        and not any(reason.startswith("input rejected") for reason in result.reasons)
    )


def test_a_community_packs_schema_patterns_stop_at_the_budget(tmp_path: Path) -> None:
    """Before, a schema that ran out refused the (valid) input: BLOCK, final, never reviewed."""

    def slow_schema(schema: dict[str, Any]) -> None:
        long_message(schema)
        schema["properties"]["message"]["allOf"] = [{"pattern": p} for p in SLOW]

    pack = load_pack(sorter(tmp_path, schema=slow_schema))
    result, took = timed(pack, {"message": EMOJI})
    assert took < BOUND_S and stopped_for_a_person(result)


def test_a_schema_that_fans_out_without_patterns_stops_at_the_budget(tmp_path: Path) -> None:
    """`$ref`s that double at each level, with no pattern: 53 s for one tiny input before."""

    def fan_out(schema: dict[str, Any]) -> None:
        schema["properties"]["note"] = {"type": "string", "$ref": "#/$defs/t0"}
        schema["$defs"] = {
            f"t{i}": {"allOf": [{"$ref": f"#/$defs/t{i + 1}"}, {"$ref": f"#/$defs/t{i + 1}"}]}
            for i in range(24)
        } | {"t24": {"minLength": 1}}

    pack = load_pack(sorter(tmp_path, schema=fan_out))
    assert timed(pack, {"message": "my bill"})[1] < 1  # without the field, nothing fans out
    result, took = timed(pack, {"message": "my bill", "note": "x"})
    assert took < BOUND_S and stopped_for_a_person(result)


def test_an_enum_on_many_items_stops_at_the_budget(tmp_path: Path) -> None:
    """Each item is compared with every listed value: 8 s for a 30 KB body before."""

    def codes(schema: dict[str, Any]) -> None:
        listed = list(range(5_000))
        schema["properties"]["codes"] = {"type": "array", "items": {"enum": listed}}

    pack = load_pack(sorter(tmp_path, schema=codes))
    result, took = timed(pack, {"message": "my bill", "codes": [4_999] * 5_000})
    assert took < BOUND_S and stopped_for_a_person(result)


def test_unique_items_is_refused_in_a_community_packs_schema(tmp_path: Path) -> None:
    """One application compares every pair of items, past any budget."""

    def unique(schema: dict[str, Any]) -> None:
        schema["properties"]["codes"] = {"type": "array", "uniqueItems": True}

    with pytest.raises(PackValidationError) as error:
        load_pack(sorter(tmp_path, schema=unique))
    assert any("uniqueItems" in problem for problem in error.value.problems)
    allowed = sorter(
        tmp_path / "b",
        schema=lambda s: s["properties"].update(codes={"type": "array", "uniqueItems": False}),
    )
    assert load_pack(allowed).community
    unevaluated = {"patternProperties": {"^x": {}}, "unevaluatedProperties": False}
    both = sorter(tmp_path / "c", schema=lambda s: s["properties"].update(extra=unevaluated))
    with pytest.raises(PackValidationError):  # `false` still counts for other keywords
        load_pack(both)


def test_the_budget_counts_the_decisions_own_cpu_time_not_waiting() -> None:
    """Waiting (a model's answer, another request) costs nothing: before, a slow model call
    ahead of the rules made them fail on ordinary text, and load changed outcomes."""
    with patterns.time_budget(0.2):
        time.sleep(0.5)
        patterns.check_time()
        with pytest.raises(patterns.OutOfTime):
            while True:
                patterns.check_time()


def test_a_pack_test_that_runs_out_of_time_says_so(tmp_path: Path) -> None:
    pack = load_pack(sorter(tmp_path, rules=slow_rules(), schema=long_message))
    case = {"name": "slow", "input": {"message": EMOJI}, "expect": {"recommendation": "billing"}}
    failures = _run(Decision(pack), case).failures
    assert any("time_budget" in failure for failure in failures)
    assert any("ran out of the 1 s time budget" in failure for failure in failures)


def test_the_budget_belongs_to_one_decision(tmp_path: Path) -> None:
    """Spent in one decision, it is not in the next; outside a decision there is none."""
    pack = load_pack(sorter(tmp_path, rules=slow_rules(), schema=long_message))
    decision = Decision(pack)
    assert set(decision.decide({"message": EMOJI}).errors.values()) == {"time_budget"}
    assert patterns._DEADLINE.get() is None
    quick = decision.decide({"message": "a question about my bill !0"})  # `\W{1,299}0`
    assert quick.errors == {} and quick.recommendation == "billing"
    patterns.check_time()  # no budget, no limit


def test_a_pack_that_is_not_a_community_pack_has_no_budget(tmp_path: Path) -> None:
    """Its patterns run on `re`, as before, with no budget (a short text: `re` would take long
    on the slow one)."""
    root = sorter(tmp_path, rules=slow_rules(), schema=long_message, pack_id="someone/sorter")
    result = Decision(load_pack(root)).decide({"message": "a question about my bill 0"})
    assert not load_pack(root).community and result.errors == {}


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
