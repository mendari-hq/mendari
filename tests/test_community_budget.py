"""A community pack's patterns share one time budget per decision (`core/patterns.py`).

RE2 is linear and the caps bound how many patterns run and how large they are, but a pack within
every cap could still take 5 s on 10,000 characters of emoji, or 17 s with negated patterns
(the focused check of B11). The budget makes the bound hard: once spent, the pack's rules fail
(`time_budget`) and the decision abstains, for a person to decide, or its schema check stops
and the input is refused.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from mendari import Decision, load_pack
from mendari.core import patterns
from mendari.core.spec import Outcome
from tests.test_community_caps import sorter

EMOJI = "\U0001f600" * 10_000
# Each takes about half a second on EMOJI and the nine stay within every cap (2,700 of 3,000).
SLOW = [rf"\W{{1,299}}{i}" for i in range(9)]
BOUND_S = 3.0  # the budget (1 s), one search under way (about 1 s at worst), and headroom


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


def test_a_community_packs_schema_patterns_stop_at_the_budget(tmp_path: Path) -> None:
    def slow_schema(schema: dict[str, Any]) -> None:
        long_message(schema)
        schema["properties"]["message"]["allOf"] = [{"pattern": p} for p in SLOW]

    pack = load_pack(sorter(tmp_path, schema=slow_schema))
    result, took = timed(pack, {"message": EMOJI})
    assert took < BOUND_S
    assert result.outcome is Outcome.BLOCK
    assert result.reasons == (
        "input rejected: (root): checking it against the schema's patterns stopped: its "
        "patterns took more than 1 s",
    )


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
        time.sleep(0.001)
        with pytest.raises(patterns.OutOfTime):
            patterns.check_time()
        with patterns.time_budget(60):
            patterns.check_time()  # an inner budget of its own
        with pytest.raises(patterns.OutOfTime):
            patterns.check_time()
    patterns.check_time()
