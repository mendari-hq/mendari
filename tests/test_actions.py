# SPDX-License-Identifier: Apache-2.0
"""The gate on actions (spec sections 13.4 and 13.7, ADR-0023): `core/policy.py` says what each
effect needs before an action is released, and `core/actions.py` checks a proposal against the
manifest.

The table is written out here, cell by cell, and held against the code; then properties over
every effect, manifest key, origin and decision state prove what the table alone cannot: that
nothing a manifest declares and nothing an agent proposes loosens a gate or releases what a
stricter reading would not, and that the cap never raises an outcome."""

from __future__ import annotations

import itertools
import json
from typing import Any, get_args

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mendari.core import actions
from mendari.core.actions import (
    MAX_ARGUMENT_CHARS,
    MAX_ARGUMENT_DEPTH,
    MAX_PROPOSED,
    REFUSALS,
    ActionResult,
    Checked,
    Proposed,
)
from mendari.core.phi import PhiPolicy
from mendari.core.policy import (
    EFFECT_GATES,
    GATE_TEXT,
    GATES,
    Gate,
    GovernanceOverride,
    Review,
    action_stops,
    apply_policy,
    disposition,
    gate_of,
    risk_permits_auto,
    stricter,
)
from mendari.core.spec import ActionSpec, AgentManifest, Effect, Outcome, RiskLevel
from mendari.providers.base import Answer, ProviderInfo
from tests.conftest import INPUT_HASH_KEY
from tests.test_spec_04 import raw_manifest

MANIFEST = AgentManifest.model_validate(raw_manifest())
CHECKERS = actions.validators(MANIFEST)
REVIEWS: tuple[Review, ...] = get_args(Review)
DECISION, AUTO, PERSON = Gate.DECISION, Gate.AUTO, Gate.PERSON
# How released each disposition is, for "never more released".
RANK = {"released": 2, "held": 1, "blocked": 0, "denied": 0}


def check(name: str, arguments: dict[str, Any] | None = None, **more: Any) -> Checked:
    return actions.check(MANIFEST, CHECKERS, Proposed(name, arguments or {}, **more))


# -- an effect's gate (Table A) -------------------------------------------------------------------


def test_each_effects_gate_is_the_runtimes_and_the_table_is_total() -> None:
    assert set(EFFECT_GATES) == set(Effect) and set(GATE_TEXT) == set(Gate)
    assert GATES == (Gate.DECISION, Gate.AUTO, Gate.PERSON)  # loosest first
    assert {effect.value: gate.value for effect, gate in EFFECT_GATES.items()} == {
        "read": "decision",
        "draft": "decision",
        "write": "auto",
        "contact": "auto",
        "order": "person",
    }


@pytest.mark.parametrize(
    ("effect", "vouched", "not_vouched"),
    [
        (Effect.READ, DECISION, AUTO),
        (Effect.DRAFT, DECISION, AUTO),
        (Effect.WRITE, AUTO, AUTO),
        (Effect.CONTACT, AUTO, AUTO),
        (Effect.ORDER, PERSON, PERSON),
    ],
)
def test_the_gate_of_each_effect_by_who_vouches_for_the_agent(
    effect: Effect, vouched: Gate, not_vouched: Gate
) -> None:
    assert gate_of(effect) is vouched
    assert gate_of(effect, vouched=False) is not_vouched
    # `allow_auto: false` is never without a person, whatever the effect and the origin.
    assert gate_of(effect, allow_auto=False) is PERSON
    assert gate_of(effect, allow_auto=False, vouched=False) is PERSON


# -- the disposition (Table B), every cell --------------------------------------------------------

BLOCKED = ("blocked", "decision_blocked")
# (outcome, has a recommendation, review, automating, refused before) -> each gate's cell.
Cell = tuple[str, str]
TABLE: list[tuple[tuple[Outcome, bool, Review, bool, bool], tuple[Cell, Cell, Cell]]] = []
for _review, _automating, _refused, _recommended in itertools.product(
    REVIEWS, (True, False), (True, False), (True, False)
):
    # 1. A BLOCK decision blocks every action, whatever else is so.
    TABLE.append(((Outcome.BLOCK, _recommended, _review, _automating, _refused), (BLOCKED,) * 3))
    for _outcome in (Outcome.AUTO, Outcome.HUMAN_REVIEW, Outcome.ABSTAIN):
        released: Cell = ("released", "with_decision")  # 2. the gate `decision`
        if not _recommended:  # 3. nothing for a reviewer to confirm
            rest: tuple[Cell, Cell] = (("blocked", "no_recommendation"),) * 2
        elif _review == "pending":  # 4. it waits for the reviewer
            rest = (("held", "awaiting_review"),) * 2
        elif _review in ("confirmed", "overridden"):  # 5. asked after the review
            rest = (("blocked", "review_closed"),) * 2
        elif _outcome is Outcome.AUTO:  # 6. an automated decision
            auto: Cell = (
                ("blocked", "earlier_refusal")
                if _refused
                else ("released", "automated")
                if _automating
                else ("blocked", "not_automated")
            )
            rest = (auto, ("blocked", "no_reviewer"))
        else:  # 7. no reviewer to wait for
            rest = (("blocked", "no_reviewer"),) * 2
        TABLE.append(((_outcome, _recommended, _review, _automating, _refused), (released, *rest)))


def test_the_table_has_every_state_once() -> None:
    assert len(TABLE) == 4 * 2 * 4 * 2 * 2 == len({state for state, _ in TABLE})


@pytest.mark.parametrize(("state", "cells"), TABLE)
def test_every_cell_of_the_table(
    state: tuple[Outcome, bool, Review, bool, bool], cells: tuple[Cell, Cell, Cell]
) -> None:
    outcome, recommended, review, automating, refused = state
    for gate, cell in zip(GATES, cells, strict=True):
        found = disposition(
            gate,
            outcome=outcome,
            recommendation="refill" if recommended else None,
            review=review,
            automating=automating,
            refused_before=refused,
        )
        assert found == cell, (gate, state)


def test_the_rows_the_spec_prints() -> None:
    """Spec section 13.7's own table, as a decision made with the answer reads it."""

    def cell(gate: Gate, outcome: Outcome, recommendation: str | None) -> Cell:
        review: Review = "pending" if outcome in (Outcome.HUMAN_REVIEW, Outcome.ABSTAIN) else "none"
        return disposition(gate, outcome=outcome, recommendation=recommendation, review=review)

    assert [cell(gate, Outcome.BLOCK, None) for gate in GATES] == [BLOCKED] * 3
    assert [cell(gate, Outcome.ABSTAIN, None) for gate in GATES] == [
        ("released", "with_decision"),
        ("blocked", "no_recommendation"),
        ("blocked", "no_recommendation"),
    ]
    assert [cell(gate, Outcome.AUTO, "refill") for gate in (DECISION, AUTO)] == [
        ("released", "with_decision"),
        ("released", "automated"),
    ]
    assert [cell(gate, Outcome.HUMAN_REVIEW, "refill") for gate in GATES] == [
        ("released", "with_decision"),
        ("held", "awaiting_review"),
        ("held", "awaiting_review"),
    ]
    # A review without a recommendation holds nothing: nobody can confirm it.
    assert cell(AUTO, Outcome.HUMAN_REVIEW, None) == ("blocked", "no_recommendation")


# -- nothing loosens ------------------------------------------------------------------------------

effects = st.sampled_from(list(Effect))
gates = st.sampled_from(list(Gate))
outcomes = st.sampled_from(list(Outcome))
reviews = st.sampled_from(REVIEWS)
recommendations = st.one_of(st.none(), st.just("refill"))
states = st.fixed_dictionaries(
    {
        "outcome": outcomes,
        "recommendation": recommendations,
        "review": reviews,
        "automating": st.booleans(),
        "refused_before": st.booleans(),
    }
)


@given(effect=effects, allow_auto=st.booleans(), vouched=st.booleans())
def test_no_manifest_key_and_no_origin_gives_a_gate_looser_than_the_effects(
    effect: Effect, allow_auto: bool, vouched: bool
) -> None:
    gate = gate_of(effect, allow_auto=allow_auto, vouched=vouched)
    assert GATES.index(gate) >= GATES.index(EFFECT_GATES[effect])
    # Each of the two only tightens, alone and together.
    assert GATES.index(gate_of(effect, allow_auto=False, vouched=vouched)) >= GATES.index(gate)
    assert GATES.index(gate_of(effect, allow_auto=allow_auto, vouched=False)) >= GATES.index(gate)
    assert stricter(gate, EFFECT_GATES[effect]) is gate


@given(
    description=st.text(min_size=1, max_size=40).filter(lambda text: text.strip()),
    effect=effects,
    allow_auto=st.one_of(st.none(), st.booleans()),
    vouched=st.booleans(),
)
def test_no_action_a_manifest_can_declare_has_a_gate_looser_than_its_effects(
    description: str, effect: Effect, allow_auto: bool | None, vouched: bool
) -> None:
    """Every `ActionSpec` there is: the model takes nothing but these keys (and a schema, which
    is no part of the gate), so this is every way a manifest can word an action."""
    raw: dict[str, Any] = {"description": description, "effect": effect.value}
    if allow_auto is not None:
        raw["allow_auto"] = allow_auto
    spec = ActionSpec.model_validate(raw)
    gate = gate_of(spec.effect, allow_auto=spec.allow_auto, vouched=vouched)
    assert GATES.index(gate) >= GATES.index(EFFECT_GATES[effect])
    assert set(ActionSpec.model_fields) == {"description", "effect", "allow_auto", "json_schema"}


@given(loose=gates, strict=gates, state=states)
def test_a_stricter_gate_is_never_more_released(
    loose: Gate, strict: Gate, state: dict[str, Any]
) -> None:
    if GATES.index(strict) < GATES.index(loose):
        loose, strict = strict, loose
    assert RANK[disposition(strict, **state)[0]] <= RANK[disposition(loose, **state)[0]]


@given(gate=gates, state=states)
def test_what_released_and_held_mean(gate: Gate, state: dict[str, Any]) -> None:
    found, reason = disposition(gate, **state)
    outcome = state["outcome"]
    if found == "released":
        by_decision = gate is DECISION and outcome is not Outcome.BLOCK
        automated = (
            gate is AUTO
            and outcome is Outcome.AUTO
            and state["recommendation"] is not None
            and state["review"] == "none"
            and state["automating"]
            and not state["refused_before"]
        )
        assert by_decision or automated
        assert reason == ("with_decision" if by_decision else "automated")
    if found == "held":
        assert gate is not DECISION and outcome is not Outcome.BLOCK
        assert state["review"] == "pending" and state["recommendation"] is not None
        assert reason == "awaiting_review"
    # The gate `person` is never released by the table: only a reviewer releases it.
    if gate is PERSON:
        assert found != "released"
    assert found in ("released", "held", "blocked")  # `denied` is a reviewer's, never the table's
    # A worse state never releases more: a hold or an earlier refusal only blocks.
    worse = {**state, "automating": False, "refused_before": True}
    assert RANK[disposition(gate, **worse)[0]] <= RANK[found]


@given(effect=effects, allow_auto=st.booleans(), state=states)
def test_an_agent_nobody_vouched_for_gets_nothing_released_without_a_person(
    effect: Effect, allow_auto: bool, state: dict[str, Any]
) -> None:
    """Such an agent never automates (`apply_policy`'s registry cap), so with
    its decisions never AUTO nothing it proposes is released by the table."""
    gate = gate_of(effect, allow_auto=allow_auto, vouched=False)
    assert gate is not DECISION
    for outcome in (Outcome.HUMAN_REVIEW, Outcome.ABSTAIN, Outcome.BLOCK):
        assert disposition(gate, **{**state, "outcome": outcome})[0] != "released"


@pytest.mark.parametrize("level", list(RiskLevel))
def test_risk_permits_auto_is_what_the_risk_cap_decides(level: RiskLevel) -> None:
    """`risk_permits_auto` restates `_caps`' last step; here the two are held equal, through
    `apply_policy` itself, with and without a governance approval."""
    raw = raw_manifest()
    raw["risk"]["level"] = int(level)
    manifest = AgentManifest.model_validate(raw)
    info = ProviderInfo("reader", "agent", "agent")
    answers = {
        "request": Answer("request", "refill", 0.95, {"refill": 0.95}, info),
        "abusive": Answer("abusive", "no", 0.97, {"no": 0.97}, info),
    }
    approved = GovernanceOverride("committee", "ref-1")
    refused = GovernanceOverride("committee", "ref-2", allow_auto=False)
    for governance in (None, approved, refused):
        decided = apply_policy(manifest, answers, {}, governance).outcome
        assert (decided is Outcome.AUTO) is risk_permits_auto(level, governance), governance
    assert risk_permits_auto(level, None) is (level <= RiskLevel.CLINICAL_INFORMATION)
    assert risk_permits_auto(level, approved) is (level <= RiskLevel.PATIENT_PREDICTION)


# -- the cap's reasons ----------------------------------------------------------------------------


def test_what_stops_a_decision_from_being_automated() -> None:
    assert action_stops([]) == ()
    assert action_stops([check("look_up_medications"), check("file_note", {"note": "x"})]) == ()
    assert action_stops([check("send_reply", {"message": "Received."})]) == ()
    assert action_stops([check("renew_prescription")]) == (
        "action 'renew_prescription' (order) is never released without a person",
    )
    assert action_stops([check("queue_refill", {"medication": "x"})]) == (
        "action 'queue_refill' is never released without a person (allow_auto: false)",
    )
    assert action_stops([check("send_reply", {"message": 5})]) == (
        "action 'send_reply': its arguments do not match its schema",
    )
    composite = "a composite's member proposed an action, which this version does not take"
    assert action_stops([check("send_reply", {"message": "x"}, refused="composite_member")]) == (
        composite,
    )
    undeclared = "the agent proposed an action its manifest does not declare"
    assert action_stops([check("delete_chart"), check("wipe_everything")]) == (undeclared,)
    # In order, each once.
    several = [
        check("renew_prescription"),
        check("delete_chart"),
        check("renew_prescription", {"again": True}),
        check("look_up_medications"),
    ]
    assert action_stops(several) == (
        "action 'renew_prescription' (order) is never released without a person",
        undeclared,
    )


def test_an_unvouched_agents_read_is_no_stop_of_its_own() -> None:
    """Its decision cannot be AUTO anyway; the stop is for what AUTO itself could not release."""
    assert action_stops([check("look_up_medications")], vouched=False) == ()
    assert action_stops([check("renew_prescription")], vouched=False) != ()


names = st.from_regex(r"[a-z][a-z0-9_]{0,20}", fullmatch=True)
json_values = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(-5, 5), st.text(max_size=12)),
    lambda inner: st.one_of(
        st.lists(inner, max_size=3), st.dictionaries(st.text(max_size=6), inner, max_size=3)
    ),
    max_leaves=8,
)
proposed = st.builds(
    Proposed,
    name=st.one_of(names, st.sampled_from(list(MANIFEST.actions))),
    arguments=st.dictionaries(st.text(max_size=8), json_values, max_size=4),
    refused=st.one_of(st.none(), st.sampled_from(["composite_member", "undeclared"])),
)


@settings(max_examples=200, deadline=None)
@given(proposals=st.lists(proposed, max_size=6), vouched=st.booleans())
def test_no_proposal_is_released_past_its_gate_and_a_reason_names_declared_actions_only(
    proposals: list[Proposed], vouched: bool
) -> None:
    checked = [actions.check(MANIFEST, CHECKERS, proposal) for proposal in proposals]
    stops = action_stops(checked, vouched=vouched)
    for item in checked:
        declared = MANIFEST.actions.get(item.proposed.name)
        assert item.spec is declared
        if declared is None:
            assert item.refusal in REFUSALS  # what is not declared is refused, whatever it says
        if item.refusal is not None or declared is None or not declared.allow_auto:
            assert stops  # and it stops automation
        elif gate_of(declared.effect, vouched=vouched) is PERSON:
            assert stops
        for problem in item.problems:  # where and which rule: no key the agent wrote
            where = problem.split(": ", 1)[0]
            assert set(where.split("/")) <= {"(root)", "*", "message", "medication",
                                             "days_supply", "note"}  # fmt: skip
    # No reason holds a name the manifest does not declare, nor any argument.
    undeclared = {p.name for p in proposals if p.name not in MANIFEST.actions and len(p.name) > 3}
    for stop in stops:
        assert not any(repr(name) in stop for name in undeclared)
        named = [name for name in MANIFEST.actions if repr(name) in stop]
        assert len(named) <= 1
    if not stops:  # everything proposed could be released with an AUTO decision
        for item in checked:
            assert item.refusal is None and item.spec is not None
            gate = gate_of(item.spec.effect, allow_auto=item.spec.allow_auto, vouched=vouched)
            assert gate is not PERSON


# -- checking a proposal --------------------------------------------------------------------------


def test_a_declared_action_with_arguments_that_match_is_not_refused() -> None:
    for name, arguments in (
        ("look_up_medications", {}),
        ("look_up_medications", {"anything": [1, {"at": "all"}]}),  # no schema: not checked
        ("draft_reply", {"message": "Your refill is with the pharmacy."}),
        ("queue_refill", {"medication": "lisinopril 10 mg"}),
        ("queue_refill", {"medication": "lisinopril 10 mg", "days_supply": 90}),
        ("renew_prescription", {}),
    ):
        found = check(name, arguments)
        assert (found.refusal, found.problems) == (None, ()), name
        assert found.spec is MANIFEST.actions[name]


@pytest.mark.parametrize(
    ("name", "arguments", "problems"),
    [
        ("draft_reply", {}, ["(root): a required field is missing"]),
        ("draft_reply", {"message": ""}, ["message: must have at least 1 characters"]),
        ("draft_reply", {"message": "x" * 2001}, ["message: must have at most 2000 characters"]),
        ("draft_reply", {"message": 5}, ["message: must be of type string"]),
        ("draft_reply", {"message": None}, ["message: must be of type string"]),
        (
            "draft_reply",
            {"message": "ok", "SENTINEL": 1},
            ["(root): has a field the schema does not allow"],
        ),
        (
            "queue_refill",
            {"medication": "x", "days_supply": 0},
            ["days_supply: must be at least 1"],
        ),
        (
            "queue_refill",
            {"medication": "x", "days_supply": 91},
            ["days_supply: must be at most 90"],
        ),
        (
            "queue_refill",
            {"medication": "x", "days_supply": 1.5},
            ["days_supply: must be of type integer"],
        ),
        (
            "queue_refill",
            {"days_supply": "SENTINEL"},
            ["(root): a required field is missing", "days_supply: must be of type integer"],
        ),
    ],
)
def test_arguments_that_fail_the_schema_are_refused_with_where_and_which_rule(
    name: str, arguments: dict[str, Any], problems: list[str]
) -> None:
    found = check(name, arguments)
    assert found.refusal == "invalid_arguments" and found.spec is MANIFEST.actions[name]
    assert list(found.problems) == problems
    assert "SENTINEL" not in json.dumps(found.problems)


def test_an_undeclared_name_and_a_carriers_refusal() -> None:
    undeclared = check("delete_chart", {"why": "SENTINEL"})
    assert (undeclared.spec, undeclared.refusal, undeclared.problems) == (None, "undeclared", ())
    # A composite's refusal wins over what the action is, declared or not, well formed or not.
    for name, arguments in (("send_reply", {"message": "x"}), ("send_reply", {}), ("nope", {})):
        carried = check(name, arguments, refused="composite_member")
        assert carried.refusal == "composite_member" and carried.problems == ()


def test_a_check_that_cannot_run_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """What cannot be checked is refused: a check that runs out of depth, of time or of
    numbers is `invalid_arguments`, never a release and never a crash."""
    from mendari.core.patterns import OutOfTime

    for failure in (RecursionError(), OverflowError(), OutOfTime("spent")):

        class Failing:
            def __init__(self, error: BaseException) -> None:
                self.error = error

            def iter_errors(self, _: Any) -> Any:
                raise self.error

        failing: Any = {"send_reply": Failing(failure)}
        found = actions.check(MANIFEST, failing, Proposed("send_reply", {"message": "x"}))
        assert found.refusal == "invalid_arguments"
        assert found.problems == ("(root): it cannot be checked",)


def test_at_most_ten_problems_are_kept() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {f"p{n}": {"type": "string"} for n in range(30)},
    }
    raw = raw_manifest()
    raw["actions"]["many"] = {"description": "Many.", "effect": "read", "schema": schema}
    manifest = AgentManifest.model_validate(raw)
    wrong = {f"p{n}": n for n in range(30)}
    found = actions.check(manifest, actions.validators(manifest), Proposed("many", wrong))
    assert found.refusal == "invalid_arguments" and len(found.problems) == actions.MAX_PROBLEMS


# -- the shape of a proposal ----------------------------------------------------------------------


def deep(levels: int) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for _ in range(levels - 1):
        value = {"in": value}
    return value


@pytest.mark.parametrize(
    ("name", "arguments", "said"),
    [
        ("Send", {}, "name must match"),
        ("send-reply", {}, "name must match"),
        ("", {}, "name must match"),
        ("a" * 65, {}, "name must match"),
        ("send_reply\n", {}, "name must match"),
        (None, {}, "name must match"),
        (5, {}, "name must match"),
        ("send_reply", None, "must be an object"),
        ("send_reply", [], "must be an object"),
        ("send_reply", "{}", "must be an object"),
        ("send_reply", {1: "x"}, "text keys"),
        ("send_reply", {"a": {2: "x"}}, "text keys"),
        ("send_reply", deep(MAX_ARGUMENT_DEPTH + 1), f"at most {MAX_ARGUMENT_DEPTH} deep"),
        ("send_reply", {"a": float("nan")}, "a number that is not finite"),
        ("send_reply", {"a": float("inf")}, "a number that is not finite"),
        ("send_reply", {"a": {1, 2}}, "a value of type set"),
        ("send_reply", {"a": "\ud800"}, "lone surrogate"),
        ("send_reply", {"a": "x" * MAX_ARGUMENT_CHARS}, "characters as JSON"),
    ],
)
def test_a_proposal_that_is_not_of_the_contracts_shape(
    name: Any, arguments: Any, said: str
) -> None:
    problem = actions.shape_problem(name, arguments)
    assert problem is not None and said in problem
    assert "send_reply" not in problem  # never echoes the name, nor a value


def test_the_limits_themselves_are_allowed() -> None:
    assert actions.shape_problem("a" * 64, {}) is None
    assert actions.shape_problem("send_reply", deep(MAX_ARGUMENT_DEPTH)) is None
    lists = {"a": [[[[[[["x"]]]]]]]}  # eight deep, lists counted as objects are
    assert actions.shape_problem("send_reply", lists) is None
    assert actions.shape_problem("send_reply", {"a": [[[[[[[["x"]]]]]]]]}) is not None
    fits = {"a": "x" * (MAX_ARGUMENT_CHARS - len('{"a":""}'))}
    assert len(actions.canonical(fits)) == MAX_ARGUMENT_CHARS
    assert actions.shape_problem("send_reply", fits) is None


def test_arguments_too_deep_for_recursion_are_a_problem_and_never_a_crash() -> None:
    value: Any = "x"
    for _ in range(100_000):
        value = [value]
    assert "deep" in str(actions.shape_problem("send_reply", {"a": value}))


@pytest.mark.parametrize(
    "raw",
    [
        "send_reply",
        {"name": "send_reply"},
        [{"name": "send_reply"}] * (MAX_PROPOSED + 1),
        ["send_reply"],
        [{"arguments": {}}],
        [{"name": "send_reply", "arguments": {}, "effect": "read"}],
        [{"name": "send_reply", "arguments": []}],
        [{"name": "Send Reply"}],
        [{"name": "send_reply", "arguments": None}],
    ],
)
def test_a_responses_actions_that_the_contract_does_not_allow(raw: Any) -> None:
    assert actions.proposals_problem(raw) is not None


def test_a_responses_actions_that_it_allows() -> None:
    assert actions.proposals_problem(None) is None
    assert actions.proposals_problem([]) is None
    full = [{"name": f"action_{n}", "arguments": {"n": n}} for n in range(MAX_PROPOSED)]
    assert actions.proposals_problem(full) is None
    assert actions.proposals_problem([{"name": "look_up_medications"}]) is None


def test_two_proposals_with_the_same_name_and_arguments_are_one() -> None:
    first = Proposed("send_reply", {"message": "x", "to": "portal"}, slot="reader")
    again = Proposed("send_reply", {"to": "portal", "message": "x"}, slot="other")
    other = Proposed("send_reply", {"message": "y"})
    draft = Proposed("draft_reply", {"message": "x", "to": "portal"})
    assert actions.distinct([first, again, other, draft, first]) == [first, other, draft]


# -- what the chain keeps, and what it does not -------------------------------------------------


def result(**changes: Any) -> ActionResult:
    base: dict[str, Any] = {
        "id": "a-1",
        "name": "send_reply",
        "effect": Effect.CONTACT,
        "declared": True,
        "disposition": "held",
        "reason": "awaiting_review",
        "arguments": {"message": "SENTINEL-TEXT"},
        "arguments_hash": "sha256:abc",
        "slot": "reader",
    }
    return ActionResult(**{**base, **changes})


def test_the_chains_half_holds_no_argument_and_no_undeclared_name() -> None:
    declared = result()
    assert declared.chained() == {
        "action_id": "a-1",
        "name": "send_reply",
        "effect": "contact",
        "disposition": "held",
        "reason": "awaiting_review",
        "arguments_hash": "sha256:abc",
        "source": "answer",
        "slot": "reader",
    }
    undeclared = result(
        name="SENTINEL_NAME",
        effect=None,
        declared=False,
        disposition="blocked",
        reason="undeclared",
    )
    chained = undeclared.chained()
    assert chained["name"] is None and chained["effect"] is None
    for kept in (declared.chained(), chained):
        assert "SENTINEL" not in json.dumps(kept)
    # The other half is the note: the name as proposed and the arguments.
    assert json.loads(undeclared.kept()) == {
        "name": "SENTINEL_NAME",
        "arguments": {"message": "SENTINEL-TEXT"},
    }
    # The caller's own answer has both: the caller sent them, or its agent did.
    shown = undeclared.to_dict()
    assert shown["name"] == "SENTINEL_NAME" and shown["arguments"] == {"message": "SENTINEL-TEXT"}
    assert "problems" not in shown and "source" not in shown
    assert result(problems=("message: must be of type string",)).to_dict()["problems"] == [
        "message: must be of type string"
    ]


def test_the_arguments_hash_is_keyed_where_the_deployment_has_a_key() -> None:
    arguments = {"message": "Received.", "to": "portal"}
    reordered = {"to": "portal", "message": "Received."}
    plain = actions.plain_hash(arguments)
    assert plain.startswith("sha256:") and plain == actions.plain_hash(reordered)
    assert PhiPolicy().arguments_hash(arguments) == plain  # mode off, no key: the plain hash
    keyed = PhiPolicy(mode="hash", hash_key=INPUT_HASH_KEY)
    hashed = keyed.arguments_hash(arguments)
    assert hashed.startswith("hmac-sha256:") and hashed == keyed.arguments_hash(reordered)
    assert hashed != keyed.arguments_hash({**arguments, "to": "sms"})
    # Its own prefix: never the input hash of the same value, so one never confirms the other.
    assert hashed != keyed.input_hash(arguments)
    assert hashed.split(":")[1] == keyed.input_hash(arguments).split(":")[1]  # the same key id


# -- a decision's actions, read back from its events ----------------------------------------------


def event(kind: str, payload: dict[str, Any], **more: Any) -> dict[str, Any]:
    return {"event": kind, "payload": payload, "created_at": "2026-11-24T10:00:00Z", **more}


def test_state_folds_a_decisions_events_into_its_actions() -> None:
    proposed_payload = result().chained()
    kept = result().kept()
    events = [
        event("decision.created", {"record_sha256": "x"}),
        event(
            "action.proposed",
            {**proposed_payload, "note_ref": {"id": "n-1", "sha256": "c"}},
            note_status="open",
            note=kept,
        ),
        event(
            "action.proposed",
            {
                **proposed_payload,
                "action_id": "a-2",
                "name": None,
                "effect": None,
                "disposition": "blocked",
                "reason": "undeclared",
                "note_ref": {"id": "n-2", "sha256": "c"},
            },
            note_status="open",
            note=json.dumps({"name": "delete_chart", "arguments": {"why": "x"}}),
        ),
        event("action.proposed", {**proposed_payload, "action_id": "a-3", "note_ref": None}),
        event(
            "action.proposed",
            {**proposed_payload, "action_id": "a-4", "note_ref": {"id": "n-4", "sha256": "c"}},
            note_status="sealed",
            note=None,
        ),
        event("review.resolved", {"final_label": "refill", "reviewer": "rn-1"}),
        event(
            "action.resolved",
            {
                "action_id": "a-1",
                "name": "send_reply",
                "effect": "contact",
                "disposition": "released",
                "reason": "confirmed",
                "reviewer": "rn-1",
            },
            created_at="2026-11-24T11:00:00Z",
        ),
        event("action.resolved", {"action_id": "unknown", "disposition": "released"}),
    ]
    first, second, third, fourth = actions.state(events)
    assert first == {
        "id": "a-1",
        "name": "send_reply",
        "effect": "contact",
        "disposition": "released",
        "reason": "confirmed",
        "source": "answer",
        "slot": "reader",
        "arguments_hash": "sha256:abc",
        "proposed_at": "2026-11-24T10:00:00Z",
        "resolved_at": "2026-11-24T11:00:00Z",
        "resolved_by": "rn-1",
        "arguments_status": "open",
        "arguments": {"message": "SENTINEL-TEXT"},
    }
    # An undeclared action's name is shown only with its arguments, when they are open.
    assert (second["name"], second["proposed_name"]) == (None, "delete_chart")
    assert second["arguments"] == {"why": "x"} and second["resolved_at"] is None
    assert (third["arguments_status"], third["arguments"]) == ("not_kept", None)
    assert (fourth["arguments_status"], fourth["arguments"]) == ("sealed", None)
    assert "proposed_name" not in fourth
    assert [a["id"] for a in actions.held(actions.state(events))] == ["a-3", "a-4"]
    assert actions.state([]) == [] and actions.state(events[:1]) == []


def test_state_reads_only_a_note_that_is_a_kept_proposal() -> None:
    payload = {**result().chained(), "note_ref": {"id": "n", "sha256": "c"}}
    for note in ("not json", "[1, 2]", json.dumps({"name": "x"}), json.dumps({"arguments": 5})):
        (item,) = actions.state([event("action.proposed", payload, note_status="open", note=note)])
        assert item["arguments"] is None and item["arguments_status"] == "open"
