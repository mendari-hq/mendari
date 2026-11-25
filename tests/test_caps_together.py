# SPDX-License-Identifier: Apache-2.0
"""The version cap, the second signal and proposed actions in one agent (ADR-0023, ADR-0024).

Each was built on its own; here they run together. The policy applies them in one order
(`core/policy.py`: the version cap, the second signal's, then the actions' cap) and their
reasons read as one list. No combination of a manifest, a proposal and a run raises an outcome
or releases an action a plainer decision would not. A proposal the second signal did not agree
on (only some repeated runs proposed it, or a check's second member proposed another) is held
for the decision's reviewer and never released without one. And the operating point stays
exact with all of it on: each case keeps what every cap did to its automation."""

from __future__ import annotations

import itertools
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mendari import AuditStore, Decision, load_pack
from mendari.core import actions
from mendari.core.actions import Proposed
from mendari.core.policy import (
    PERSON_DECIDES,
    Gate,
    GovernanceOverride,
    PolicyInputs,
    action_stops,
    apply_policy,
    disposition,
    gate_of,
    lowered,
)
from mendari.core.spec import Effect, Outcome, PackManifest
from mendari.errors import ProviderError
from mendari.evals import operating_point as op
from mendari.evals.harness import build_evaluation, load_dataset
from mendari.providers.base import Answer, ProviderInfo
from mendari.providers.signals import KINDS, SECOND_SIGNALS, Cap, Checked, Signals
from tests.support.confidence_agents import (
    REFILL,
    VERSION,
    Simulated,
    answer,
    calibration_file,
    live_member,
    scaffold,
    serve,
    variant,
)
from tests.test_llm import MODEL, STATE_FIELDS, completion, install
from tests.test_spec_04 import raw_manifest

QUEUE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["queue"],
    "properties": {"queue": {"type": "string", "enum": ["refill", "scheduling", "front_desk"]}},
}
DECLARED: dict[str, Any] = {
    "look_up_medications": {
        "description": "Read the patient's active medication list.",
        "effect": "read",
    },
    "assign_queue": {
        "description": "Put the message in the refill, scheduling or front-desk queue.",
        "effect": "write",
        "schema": QUEUE,
    },
    "send_acknowledgement": {
        "description": "Send the patient a short acknowledgement.",
        "effect": "contact",
        "allow_auto": False,
    },
    "renew_prescription": {
        "description": "Renew the prescription as it was last written.",
        "effect": "order",
    },
}
LOOK_UP = {"name": "look_up_medications", "arguments": {}}
TO_REFILL = {"name": "assign_queue", "arguments": {"queue": "refill"}}
TO_SCHEDULING = {"name": "assign_queue", "arguments": {"queue": "scheduling"}}
ACKNOWLEDGE = {"name": "send_acknowledgement", "arguments": {}}
RENEW = {"name": "renew_prescription", "arguments": {}}
UNDECLARED = {"name": "page_the_surgeon", "arguments": {"reason": "a synthetic secret"}}
CHANGED = "the agent changed since its confidence was calibrated (was 2.1.0, is 2.2.0)"
CONFIDENT = "confidence 0.97 >= auto threshold 0.90"
TIMEOUT = ProviderError("timeout", "the agent took too long")


def proposing(*proposed: dict[str, Any], **said: Any) -> dict[str, Any]:
    """What an agent's service answers (`answer`), with the actions it proposes."""
    return {**answer(**said), "actions": [dict(item) for item in proposed]}


def acting(tmp_path: Path, name: str = "agent", **more: Any) -> Path:
    """A scaffolded agent on spec 0.4 that declares `DECLARED`; `more` are keys of its variant."""

    def add(manifest: dict[str, Any]) -> None:
        manifest["actions"] = DECLARED
        variant(manifest).update(more)

    return scaffold(tmp_path, name, edit=add)


def with_all(tmp_path: Path, name: str = "all", temperature: float = 1.0) -> Path:
    """One agent with every feature on: a calibration, three repeated runs, declared actions."""
    root = acting(tmp_path, name, repeat=3, calibration="models/c.json")
    calibration_file(root, {"request": temperature}, relative="models/c.json")
    return root


def checking(tmp_path: Path, second: dict[str, Any] | None = None, name: str = "checked") -> Path:
    """An agent that declares `DECLARED`, answered by one live agent and checked by another
    (or by the variant `second` gives)."""

    def add(manifest: dict[str, Any]) -> None:
        manifest["actions"] = DECLARED
        reader = manifest["providers"]["reader"]
        reader["variants"]["checked"] = {
            "type": "check",
            "members": {
                "agent": live_member(manifest, agent_id="first-agent"),
                "second": second or live_member(manifest, agent_id="second-agent"),
            },
        }
        reader["use"] = "checked"

    return scaffold(tmp_path, name, edit=add)


def two(first: Any, second: Any) -> Any:
    """A service that answers the member that answers with `first`, the one that checks with
    `second`."""

    def respond(body: dict[str, Any]) -> Any:
        return first if body["agent_id"] == "first-agent" else second

    return respond


def decide(root: Path, **kwargs: Any) -> Any:
    return Decision(load_pack(root), **kwargs)(REFILL)


def shown(result: Any) -> list[tuple[str, str, str]]:
    return [(a.name, a.disposition, a.reason) for a in result.actions or ()]


# -- the order of the caps, and how their reasons read ---------------------------------------------


def test_the_three_caps_apply_in_one_order_and_read_as_one_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The version cap, then the second signal's, then the actions': what the answers are
    worth first, what the agent asks to do last. Each says a person decides."""
    runs = [
        proposing(TO_REFILL, RENEW, version="2.2.0"),
        proposing(TO_REFILL, RENEW, request="appointment", version="2.2.0"),
        proposing(TO_REFILL, RENEW, version="2.2.0"),
    ]
    serve(monkeypatch, runs)
    result = decide(with_all(tmp_path))
    assert (result.outcome, result.recommendation) == (Outcome.HUMAN_REVIEW, "refill")
    assert result.reasons == (
        CONFIDENT,
        f"{CHANGED}; a person decides",
        "2 of 3 runs agreed on request; a person decides",
        "action 'renew_prescription' (order) is never released without a person; a person decides",
    )
    assert shown(result) == [
        ("assign_queue", "held", "awaiting_review"),
        ("renew_prescription", "held", "awaiting_review"),
    ]
    inputs = result.policy_inputs
    assert inputs is not None
    assert inputs.caps == (CHANGED, "2 of 3 runs agreed on request")
    assert inputs.actions == (
        "action 'renew_prescription' (order) is never released without a person",
    )
    # Each alone lowers the same decision, and none of them is needed for another to hold.
    plain = PolicyInputs(inputs.answers, {}, None, inputs.variants)
    manifest = load_pack(tmp_path / "all").manifest
    assert plain.apply(manifest).outcome is Outcome.AUTO
    for caps, stops in ((inputs.caps[:1], ()), (inputs.caps[1:], ()), ((), inputs.actions)):
        alone = PolicyInputs(inputs.answers, {}, None, inputs.variants, caps=caps, actions=stops)
        assert alone.apply(manifest).outcome is Outcome.HUMAN_REVIEW


def test_the_caps_are_given_in_their_order_whatever_order_they_were_found_in() -> None:
    check = Cap("check", "agent and second disagree on request (agent: refill, second: other)")
    repeat = Cap("repeat", "2 of 3 runs agreed on request")
    version = Cap("calibration", CHANGED)
    assert KINDS == ("calibration", "repeat", "check")
    for found in itertools.permutations([check, repeat, version]):
        assert Signals(caps=found).reasons() == (version.reason, repeat.reason, check.reason)
    assert Signals(caps=(check, repeat, version)).reasons(SECOND_SIGNALS) == (
        repeat.reason,
        check.reason,
    )


@pytest.mark.parametrize(
    ("said", "outcome", "first"),
    [
        ({"confidence": 0.6}, Outcome.HUMAN_REVIEW, "confidence 0.60 is below the auto threshold"),
        ({"confidence": 0.4}, Outcome.ABSTAIN, "confidence 0.40 < review threshold 0.50"),
        ({"urgent": "yes"}, Outcome.HUMAN_REVIEW, "rule same-day-symptoms"),
    ],
)
def test_a_decision_already_going_to_a_person_gets_the_same_reasons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    said: dict[str, Any],
    outcome: Outcome,
    first: str,
) -> None:
    serve(monkeypatch, [proposing(RENEW, version="2.2.0", **said)])
    result = decide(with_all(tmp_path))
    assert result.outcome is outcome
    assert result.reasons[0].startswith(first)
    assert result.reasons[1:] == (
        f"{CHANGED}; a person decides",
        "action 'renew_prescription' (order) is never released without a person; a person decides",
    )


def test_a_block_stands_with_the_caps_as_notes_and_blocks_every_action() -> None:
    """A rule's BLOCK is final: no cap turns it into a review, a provider's cap is a note that
    does not say a person decides, and the actions' cap says nothing (every action is blocked)."""
    manifest = PackManifest.model_validate(raw_manifest())
    answers = {
        "request": said("request", "refill", 0.99),
        "abusive": said("abusive", "yes", 0.99),
    }
    stops = ("action 'renew_prescription' (order) is never released without a person",)
    plain = apply_policy(manifest, answers, {})
    capped = apply_policy(manifest, answers, {}, caps=(CHANGED,), actions=stops)
    assert plain.outcome is capped.outcome is Outcome.BLOCK
    assert capped.reasons == (*plain.reasons, CHANGED)
    for gate in Gate:
        found = disposition(
            gate, outcome=capped.outcome, recommendation=capped.recommendation, review="none"
        )
        assert found == ("blocked", "decision_blocked")


# -- no manifest, proposal or run raises an outcome ------------------------------------------------

INFO = ProviderInfo(slot="reader", variant="agent", type="agent")
BASE = raw_manifest()
NAMES = list(BASE["actions"])
RANK = {"released": 2, "held": 1, "blocked": 0, "denied": 0}
REVIEWED = (Outcome.HUMAN_REVIEW, Outcome.ABSTAIN)


def said(question: str, label: str, confidence: float) -> Answer:
    labels = ("yes", "no") if question == "abusive" else ("refill", "appointment", "other")
    rest = (1.0 - confidence) / (len(labels) - 1)
    probabilities = {name: (confidence if name == label else rest) for name in labels}
    return Answer(question, label, confidence, probabilities, INFO)


manifests = st.fixed_dictionaries(
    {
        "auto": st.sampled_from([0.5, 0.7, 0.9, 1.0]),
        "review": st.sampled_from([0.0, 0.3, 0.5]),
        "level": st.integers(0, 4),
        "never": st.sampled_from([None, "refill", "other"]),
        "rule": st.sampled_from(["BLOCK", "HUMAN_REVIEW", "ABSTAIN", "AUTO"]),
        "tightened": st.sets(st.sampled_from(NAMES)),
        "effects": st.lists(
            st.sampled_from(list(Effect)), min_size=len(NAMES), max_size=len(NAMES)
        ),
    }
)
decided = st.fixed_dictionaries(
    {
        "request": st.sampled_from(["refill", "appointment", "other"]),
        "confidence": st.sampled_from([0.2, 0.5, 0.7, 0.9, 0.95, 1.0]),
        "abusive": st.sampled_from(["no", "no", "yes"]),
        "sure": st.sampled_from([0.6, 0.95]),
        "failed": st.booleans(),
        "approved": st.booleans(),
        "hold": st.sampled_from([None, "an operator's hold"]),
        "blocks_to_person": st.sampled_from([None, "not approved here"]),
        "site_code": st.sampled_from([(), ("t2d",)]),
        "not_offered": st.sampled_from([(), ("medications may be incomplete",)]),
        "canned": st.sampled_from([None, "canned answers: MENDARI_AGENT_URL is not set"]),
    }
)
found_caps = st.lists(
    st.builds(
        lambda kind, text: Cap(kind, f"{kind} {text}"),
        st.sampled_from(KINDS),
        st.text(min_size=1, max_size=20),
    ),
    max_size=3,
)
proposals = st.lists(
    st.builds(
        Proposed,
        name=st.sampled_from([*NAMES, "never_declared"]),
        arguments=st.sampled_from([{}, {"message": "x"}, {"medication": "y"}, {"note": 3}]),
        refused=st.sampled_from([None, None, "composite_member"]),
        disputed=st.booleans(),
    ),
    max_size=4,
)


def manifest_of(made: dict[str, Any]) -> PackManifest:
    raw = raw_manifest()
    raw["policy"]["thresholds"] = {
        "auto": made["auto"],
        "review": min(made["review"], made["auto"]),
    }
    raw["risk"]["level"] = made["level"]
    if made["never"] is not None:
        raw["policy"]["labels"] = {made["never"]: {"allow_auto": False}}
    # A rule may ask for AUTO only where the risk level lets an agent automate at all.
    asks_auto = made["rule"] == "AUTO" and made["level"] >= 2
    raw["policy"]["rules"][0]["outcome"] = "HUMAN_REVIEW" if asks_auto else made["rule"]
    for name, effect in zip(NAMES, made["effects"], strict=True):
        raw["actions"][name]["effect"] = effect.value
        raw["actions"][name]["allow_auto"] = name not in made["tightened"]
    return PackManifest.model_validate(raw)


def inputs_of(case: dict[str, Any], caps: Any = (), stops: Any = ()) -> PolicyInputs:
    answers = {"request": said("request", case["request"], case["confidence"])}
    failures = {"abusive": "timeout"}
    if not case["failed"]:
        answers["abusive"] = said("abusive", case["abusive"], case["sure"])
        failures = {}
    governance = GovernanceOverride("CMIO", "AI-committee-1") if case["approved"] else None
    return PolicyInputs(
        answers,
        failures,
        governance,
        {"reader": "agent"},
        hold=case["hold"],
        blocks_to_person=case["blocks_to_person"],
        site_code=case["site_code"],
        not_offered=case["not_offered"],
        canned=case["canned"],
        caps=caps,
        actions=stops,
    )


@settings(max_examples=400, deadline=None)
@given(made=manifests, case=decided, caps=found_caps, proposed=proposals, vouched=st.booleans())
def test_no_manifest_proposal_or_run_raises_an_outcome(
    made: dict[str, Any],
    case: dict[str, Any],
    caps: list[Cap],
    proposed: list[Proposed],
    vouched: bool,
) -> None:
    """With the version cap, the second signal and proposed actions all in play, over generated
    manifests, answers, deployments, caps and proposals: every one of the eight ways of leaving
    caps out decides the same thing or something more automated, never less. So no cap, and no
    combination of them, makes AUTO of anything, turns a review into a BLOCK, or changes a
    recommendation; and no action is released that the plain decision would not release."""
    manifest = manifest_of(made)
    checked = [actions.check(manifest, actions.validators(manifest), item) for item in proposed]
    signals = Signals(caps=tuple(caps))
    version = signals.reasons(("calibration",))
    second = signals.reasons(SECOND_SIGNALS)
    stops = action_stops(checked, vouched=vouched)
    plain = inputs_of(case).apply(manifest)
    full = inputs_of(case, signals.reasons(), stops).apply(manifest)
    assert signals.reasons() == (*version, *second)

    for use_version, use_second, use_stops in itertools.product([False, True], repeat=3):
        some = (*(version if use_version else ()), *(second if use_second else ()))
        result = inputs_of(case, some, stops if use_stops else ()).apply(manifest)
        capped = bool(some or (use_stops and stops))
        if plain.outcome is Outcome.AUTO:
            assert result.outcome is (Outcome.HUMAN_REVIEW if capped else Outcome.AUTO)
        else:
            assert result.outcome is plain.outcome  # no other outcome is ever changed
        assert (result.recommendation, result.priority, result.rule) == (
            plain.recommendation,
            plain.priority,
            plain.rule,
        )
        assert result.reasons[: len(plain.reasons)] == plain.reasons  # only ever added to
        assert len(set(result.reasons)) == len(result.reasons)
        if result.outcome is Outcome.BLOCK:  # a block is final: nothing says a person decides
            assert not any(r.endswith(PERSON_DECIDES) for r in result.reasons[len(plain.reasons) :])
        else:
            assert all(r.endswith(PERSON_DECIDES) for r in result.reasons[len(plain.reasons) :])
    # The reasons of all three, in their order: the version cap, the second signal, the actions.
    added = full.reasons[len(plain.reasons) :]
    if full.outcome is not Outcome.BLOCK:
        expected = [*dict.fromkeys([*version, *second]), *stops]
        assert list(added) == [
            f"{reason}{PERSON_DECIDES}"
            for reason in dict.fromkeys(expected)
            if f"{reason}{PERSON_DECIDES}" not in plain.reasons
        ]
    # `lowered` is step 7 on its own, and it is what the whole policy applies.
    assert lowered(plain, signals.reasons()) == inputs_of(case, signals.reasons()).apply(manifest)

    # What may be done with each proposal, under the decision with every cap and without any.
    def cell(item: Any, policy: Any) -> str:
        if item.refusal is not None or item.spec is None:
            return "blocked"
        gate = gate_of(
            item.spec.effect,
            allow_auto=item.spec.allow_auto,
            vouched=vouched,
            agreed=not item.proposed.disputed,
        )
        return disposition(
            gate,
            outcome=policy.outcome,
            recommendation=policy.recommendation,
            review="pending" if policy.outcome in REVIEWED else "none",
        )[0]

    for item in checked:
        with_caps, without = cell(item, full), cell(item, plain)
        if with_caps == "released":
            assert without == "released"  # the caps release nothing the plain decision would not
            assert not item.proposed.disputed and item.refusal is None
            assert item.spec is not None and item.spec.allow_auto
            if item.spec.effect not in (Effect.READ, Effect.DRAFT) or not vouched:
                assert full.outcome is plain.outcome is Outcome.AUTO and not stops and not caps
        if item.proposed.disputed or item.refusal is not None or item.spec is None:
            assert with_caps != "released" and without != "released"
        if with_caps == "held":
            assert full.outcome is Outcome.HUMAN_REVIEW and full.recommendation is not None


@given(
    effect=st.sampled_from(list(Effect)),
    allow_auto=st.booleans(),
    vouched=st.booleans(),
    outcome=st.sampled_from(list(Outcome)),
    recommendation=st.sampled_from([None, "refill"]),
    review=st.sampled_from(["none", "pending", "confirmed", "overridden"]),
    automating=st.booleans(),
    refused_before=st.booleans(),
)
def test_a_disputed_proposal_is_never_released_without_a_person(
    effect: Effect,
    allow_auto: bool,
    vouched: bool,
    outcome: Outcome,
    recommendation: str | None,
    review: Any,
    automating: bool,
    refused_before: bool,
) -> None:
    """Whatever its effect, and a `read` too: its gate is `person`, which no decision releases
    on its own, and never looser than the same proposal's when the runs agreed."""
    gate = gate_of(effect, allow_auto=allow_auto, vouched=vouched, agreed=False)
    assert gate is Gate.PERSON
    state: dict[str, Any] = {
        "outcome": outcome,
        "recommendation": recommendation,
        "review": review,
        "automating": automating,
        "refused_before": refused_before,
    }
    found = disposition(gate, **state)[0]
    assert found != "released"
    agreed = disposition(gate_of(effect, allow_auto=allow_auto, vouched=vouched), **state)[0]
    assert RANK[found] <= RANK[agreed] or (found, agreed) == ("held", "blocked")
    if found == "held":
        assert review == "pending" and recommendation is not None and outcome is not Outcome.BLOCK


# -- repeated runs that do not propose the same ----------------------------------------------------


def test_runs_that_propose_the_same_actions_change_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, [proposing(LOOK_UP, TO_REFILL)] * 3)
    result = decide(acting(tmp_path, repeat=3))
    assert result.outcome is Outcome.AUTO and result.reasons == (CONFIDENT,)
    assert shown(result) == [
        ("look_up_medications", "released", "with_decision"),
        ("assign_queue", "released", "automated"),
    ]
    assert result.signals.checked == (Checked("reader", "agent", "repeat", 3, 2),)
    # The same decision as one run alone makes.
    serve(monkeypatch, [proposing(LOOK_UP, TO_REFILL)])
    once = decide(acting(tmp_path, "once"))
    assert (once.outcome, shown(once)) == (result.outcome, shown(result))


def test_an_action_only_some_runs_proposed_is_held_and_never_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runs agree on every answer and one of them did not ask to do the same: the decision
    is capped, and what only some runs proposed waits for the reviewer, a `read` too, which any
    decision that is not blocked would otherwise release."""
    serve(
        monkeypatch,
        [proposing(LOOK_UP, TO_REFILL), proposing(TO_REFILL), proposing(LOOK_UP, TO_REFILL)],
    )
    with AuditStore(":memory:") as store:
        result = decide(acting(tmp_path, repeat=3), store=store)
        assert (result.outcome, result.recommendation) == (Outcome.HUMAN_REVIEW, "refill")
        assert result.reasons == (
            CONFIDENT,
            "2 of 3 runs proposed action 'look_up_medications'; a person decides",
        )
        assert shown(result) == [
            ("look_up_medications", "held", "awaiting_review"),  # never `with_decision`
            ("assign_queue", "held", "awaiting_review"),  # every run's, and the decision waits
        ]
        assert result.answers["request"].evidence == ("repeat: 3 of 3 runs agreed",)
        (checked,) = result.signals.checked
        assert (checked.disagreed, checked.proposals, checked.differed) == ((), 1, True)
        assert result.policy_inputs is not None
        assert result.policy_inputs.caps == ("2 of 3 runs proposed action 'look_up_medications'",)
        assert result.policy_inputs.actions == ()  # the cap is the second signal's
        # The reviewer confirms the decision, and with it what waited.
        confirmed = store.resolve_review(result.id, final_label="refill", reviewer="rn-1")
        assert [(a["name"], a["disposition"], a["reason"]) for a in confirmed["actions"]] == [
            ("look_up_medications", "released", "confirmed"),
            ("assign_queue", "released", "confirmed"),
        ]
        assert store.verify()["ok"]


def test_runs_that_propose_an_action_with_different_arguments_hold_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, [proposing(TO_REFILL), proposing(TO_SCHEDULING), proposing(TO_REFILL)])
    with AuditStore(":memory:") as store:
        result = decide(acting(tmp_path, repeat=3), store=store)
        assert result.outcome is Outcome.HUMAN_REVIEW
        assert result.reasons[-1] == (
            "the 3 runs proposed action 'assign_queue' with different arguments; a person decides"
        )
        held = result.actions or ()
        assert [(a.name, a.arguments, a.disposition) for a in held] == [
            ("assign_queue", {"queue": "refill"}, "held"),
            ("assign_queue", {"queue": "scheduling"}, "held"),
        ]
        # A reviewer who overrides the recommendation denies both.
        resolved = store.resolve_review(result.id, final_label="appointment", reviewer="rn-1")
        assert [(a["disposition"], a["reason"]) for a in resolved["actions"]] == [
            ("denied", "overridden")
        ] * 2


def test_a_run_that_failed_proposed_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(
        monkeypatch,
        [proposing(LOOK_UP), TIMEOUT, proposing(LOOK_UP)],
    )
    result = decide(acting(tmp_path, repeat=3))
    assert result.outcome is Outcome.HUMAN_REVIEW
    assert result.reasons[1:] == (
        "2 of 3 runs agreed on request (1 failed: timeout); a person decides",
        "2 of 3 runs agreed on urgent_symptoms (1 failed: timeout); a person decides",
        "2 of 3 runs proposed action 'look_up_medications'; a person decides",
    )
    assert shown(result) == [("look_up_medications", "held", "awaiting_review")]


def test_a_disputed_action_has_nobody_to_wait_for_on_an_abstention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Held means a reviewer will see it. An abstention has no recommendation to confirm, so
    what the runs did not agree on is blocked; what they all proposed is released as a `read`
    is with any decision that is not blocked."""
    low = {"confidence": 0.4}
    serve(
        monkeypatch,
        [proposing(LOOK_UP, RENEW, **low), proposing(LOOK_UP, **low), proposing(LOOK_UP, **low)],
    )
    result = decide(acting(tmp_path, repeat=3))
    assert result.outcome is Outcome.ABSTAIN
    assert shown(result) == [
        ("look_up_medications", "released", "with_decision"),
        ("renew_prescription", "blocked", "no_recommendation"),
    ]


def test_a_reason_never_names_an_undeclared_action_nor_an_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, [proposing(), proposing(UNDECLARED), proposing()])
    result = decide(acting(tmp_path, repeat=3))
    assert result.outcome is Outcome.HUMAN_REVIEW
    assert result.reasons[1:] == (
        "1 of 3 runs proposed an action the manifest does not declare; a person decides",
        "the agent proposed an action its manifest does not declare; a person decides",
    )
    assert shown(result) == [("page_the_surgeon", "blocked", "undeclared")]
    text = " ".join(result.reasons) + str(result.to_record())
    assert "page_the_surgeon" not in text and "synthetic secret" not in text


def test_canned_answers_are_asked_once_and_their_proposals_stand(tmp_path: Path) -> None:
    """Canned answers cannot vary, so nothing was repeated and nothing is disputed."""
    root = acting(tmp_path, repeat=3)
    mock = root / "fixtures" / "mock_agent.json"
    canned = json.loads(mock.read_text())
    for case in canned["cases"]:
        case["response"]["actions"] = [LOOK_UP, TO_REFILL]
    mock.write_text(json.dumps(canned))
    result = decide(root)
    assert result.signals.caps == () and result.signals.checked[0].canned
    assert result.outcome is Outcome.AUTO
    assert shown(result) == [
        ("look_up_medications", "released", "with_decision"),
        ("assign_queue", "released", "automated"),
    ]


# -- a check whose members do not propose the same -------------------------------------------------


def test_the_first_members_proposals_are_the_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check is the one composite whose proposals are taken: its first member answers, so
    what it proposes is what the slot proposes. Two members that agree change nothing."""
    serve(monkeypatch, two(proposing(LOOK_UP, TO_REFILL), proposing(TO_REFILL, LOOK_UP)))
    result = decide(checking(tmp_path))
    assert result.outcome is Outcome.AUTO and result.reasons == (CONFIDENT,)
    assert shown(result) == [
        ("look_up_medications", "released", "with_decision"),
        ("assign_queue", "released", "automated"),
    ]
    assert result.signals.checked == (Checked("reader", "checked", "check", 2, 1),)


@pytest.mark.parametrize(
    ("first", "second", "reason", "held"),
    [
        (
            [LOOK_UP, TO_REFILL],
            [TO_REFILL],
            "agent proposed action 'look_up_medications' and second did not",
            ["look_up_medications", "assign_queue"],
        ),
        (
            [TO_REFILL],
            [TO_SCHEDULING],
            "agent and second proposed action 'assign_queue' with different arguments",
            ["assign_queue"],
        ),
        (
            [TO_REFILL],
            [TO_REFILL, RENEW],
            "second proposed action 'renew_prescription' and agent did not",
            ["assign_queue"],  # the second member's own proposal is never taken
        ),
        (
            [],
            [UNDECLARED],
            "second proposed an action the manifest does not declare and agent did not",
            [],
        ),
    ],
)
def test_members_that_do_not_propose_the_same_cap_the_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first: list[dict[str, Any]],
    second: list[dict[str, Any]],
    reason: str,
    held: list[str],
) -> None:
    serve(monkeypatch, two(proposing(*first), proposing(*second)))
    result = decide(checking(tmp_path))
    assert (result.outcome, result.recommendation) == (Outcome.HUMAN_REVIEW, "refill")
    assert result.reasons == (CONFIDENT, f"{reason}; a person decides")
    assert shown(result) == [(name, "held", "awaiting_review") for name in held]
    (checked,) = result.signals.checked
    assert checked.disagreed == () and checked.proposals == 1
    assert result.answers["request"].evidence == ("check: second agrees",)


def test_a_second_member_that_failed_checked_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, two(proposing(LOOK_UP), TIMEOUT))
    result = decide(checking(tmp_path))
    assert result.outcome is Outcome.HUMAN_REVIEW
    assert result.reasons[1:] == (
        "provider failure (second:timeout); safety checks may be incomplete",
        "second failed, so what agent proposed to do was not checked; a person decides",
    )
    assert shown(result) == [("look_up_medications", "held", "awaiting_review")]


def test_a_second_member_that_cannot_propose_checks_the_answers_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model that reads the message a second time answers the questions and proposes nothing:
    the first member's proposals stand as proposed, each under its own gate."""
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "gw-test-key")
    monkeypatch.setenv("LLM_MODEL", MODEL)
    reads = {"request": "refill"}

    def handler(request: httpx.Request) -> httpx.Response:
        schema = json.loads(request.content)["response_format"]["json_schema"]["schema"]
        content = {}
        for question, spec in schema["properties"].items():
            top = reads.get(question, "no")
            content[question] = {
                label: 0.96 if label == top else 0.02 for label in spec["properties"]
            }
        return httpx.Response(200, json=completion(content))

    install(monkeypatch, handler)
    root = checking(tmp_path, {"type": "llm", "state_fields": STATE_FIELDS, "batch": False})
    serve(monkeypatch, [proposing(LOOK_UP, TO_REFILL)])
    agreed = decide(root)
    assert agreed.outcome is Outcome.AUTO and agreed.reasons == (CONFIDENT,)
    assert shown(agreed) == [
        ("look_up_medications", "released", "with_decision"),
        ("assign_queue", "released", "automated"),
    ]
    assert agreed.signals.checked[0].proposals == 0
    reads["request"] = "appointment"
    differed = decide(root)
    assert differed.outcome is Outcome.HUMAN_REVIEW
    assert differed.reasons[-1].startswith("agent and second disagree on request")
    assert shown(differed) == [  # the gates' own table: a `read` with any decision not blocked
        ("look_up_medications", "released", "with_decision"),
        ("assign_queue", "held", "awaiting_review"),
    ]


def test_another_composites_members_still_may_not_propose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def vote(manifest: dict[str, Any]) -> None:
        manifest["actions"] = DECLARED
        reader = manifest["providers"]["reader"]
        reader["variants"]["voted"] = {
            "type": "vote",
            "members": {
                "agent": live_member(manifest, agent_id="first-agent"),
                "second": live_member(manifest, agent_id="second-agent"),
            },
        }
        reader["use"] = "voted"

    serve(monkeypatch, two(proposing(LOOK_UP), proposing(LOOK_UP)))
    result = decide(scaffold(tmp_path, "voted", edit=vote))
    assert result.outcome is Outcome.HUMAN_REVIEW
    assert shown(result) == [("look_up_medications", "blocked", "composite_member")]


# -- whatever the runs answer and propose ----------------------------------------------------------

RUN = st.one_of(
    st.none(),  # a run that fails
    st.tuples(
        st.sampled_from(["refill", "refill", "appointment", "other"]),
        st.sampled_from([0.4, 0.6, 0.91, 0.95, 0.99]),
        st.sampled_from(["no", "no", "no", "yes"]),
        st.sampled_from([VERSION, VERSION, VERSION, "2.2.0"]),
        st.sampled_from(
            [
                (),
                (LOOK_UP,),
                (LOOK_UP,),
                (TO_REFILL,),
                (LOOK_UP, TO_REFILL),
                (TO_SCHEDULING,),
                (ACKNOWLEDGE,),
                (UNDECLARED,),
            ]
        ),
    ),
)


# Runs that automate on their own, and what one of three may do instead: every combination of
# them is tried too, so the property is put to the test where the features have work to do.
SOUND = [
    (request, confidence, "no", VERSION, proposed)
    for request in ("refill", "appointment")
    for confidence in (0.91, 0.99)
    for proposed in ((), (LOOK_UP,), (TO_REFILL,), (LOOK_UP, TO_REFILL))
]
INSTEAD = [
    None,
    ("other", 0.99, "no", VERSION, ()),
    ("refill", 0.6, "no", VERSION, (LOOK_UP,)),
    ("refill", 0.99, "yes", VERSION, (LOOK_UP,)),
    ("refill", 0.99, "no", "2.2.0", (LOOK_UP,)),
    ("refill", 0.99, "no", VERSION, (TO_SCHEDULING,)),
    ("refill", 0.99, "no", VERSION, (ACKNOWLEDGE,)),
    ("refill", 0.99, "no", VERSION, (UNDECLARED,)),
    ("appointment", 0.99, "no", VERSION, (RENEW,)),
]


def an_answer(run: Any) -> Any:
    if run is None:
        return TIMEOUT
    request, confidence, urgent, version, proposed = run
    return proposing(
        *proposed, request=request, confidence=confidence, urgent=urgent, version=version
    )


def test_no_run_raises_an_outcome_or_releases_an_action_with_every_feature_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever three runs answer, propose and report as their version, and whatever a second
    agent does: a decision that is AUTO with every feature on is AUTO for each of those runs on
    its own with every feature off, and an action released with every feature on is released
    for each run on its own. So the three together make AUTO of nothing, and release nothing.
    (The calibration here is the identity, T = 1: its version cap is all it can change. A
    fitted map is a measured correction and may move a confidence either way.)"""
    plain = Decision(load_pack(acting(tmp_path, "plain")))
    everything = Decision(load_pack(with_all(tmp_path)))
    checked = Decision(load_pack(checking(tmp_path)))

    def alone(run: Any) -> Any:
        serve(monkeypatch, [an_answer(run)])
        return plain(REFILL)

    def released(result: Any) -> set[tuple[str, str]]:
        return {
            (a.name, actions.canonical(a.arguments))
            for a in result.actions or ()
            if a.disposition == "released"
        }

    automated: list[bool] = []

    def holds(runs: list[Any]) -> None:
        serve(monkeypatch, [an_answer(run) for run in runs])
        together = everything(REFILL)
        serve(monkeypatch, two(an_answer(runs[0]), an_answer(runs[1])))
        with_check = checked(REFILL)
        each = [alone(run) for run in runs]
        automated.append(together.outcome is Outcome.AUTO)
        if together.outcome is Outcome.AUTO:
            assert [one.outcome for one in each] == [Outcome.AUTO] * 3
            assert len({run[0] for run in runs}) == 1  # they agreed on the answer,
            assert len({frozenset(map(str, run[4])) for run in runs}) == 1  # on what to do,
            assert {run[3] for run in runs} == {VERSION}  # and the agent had not changed
            assert together.signals.caps == ()
        for one in each:
            assert released(together) <= released(one)
        if with_check.outcome is Outcome.AUTO:
            assert each[0].outcome is Outcome.AUTO and runs[1] is not None
            assert frozenset(map(str, runs[0][4])) == frozenset(map(str, runs[1][4]))
        assert released(with_check) <= released(each[0])
        for result in (together, with_check):
            inputs = result.policy_inputs
            assert inputs is not None
            assert inputs.apply(result_manifest(result)).outcome is result.outcome
            if inputs.caps or inputs.actions:
                assert result.outcome is not Outcome.AUTO

    manifests_by_digest = {
        decision.pack.digest: decision.pack.manifest for decision in (everything, checked)
    }

    def result_manifest(result: Any) -> PackManifest:
        return manifests_by_digest[result.pack_digest]

    for run in SOUND:  # three runs that agree, then each one of them doing something else
        holds([run, run, run])
        assert automated[-1]
        for other, place in itertools.product(INSTEAD, range(3)):
            holds([*[run] * place, other, *[run] * (2 - place)])
            assert not automated[-1]
    assert sum(automated) == len(SOUND)

    @settings(
        max_examples=200,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(st.lists(RUN, min_size=3, max_size=3))
    def whatever(runs: list[Any]) -> None:
        holds(runs)

    whatever()


# -- the operating point stays exact ---------------------------------------------------------------

_CASE = re.compile(r"\b(test-(\d{5}))\b")


def test_applying_the_policy_again_gives_the_run_back_with_all_three_in_play(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each case keeps what the version cap, the second signal and its proposed actions did to
    its automation (`PolicyInputs.caps` and `.actions`), so the policy applied again at the
    manifest's threshold gives the run's outcomes back case for case, and one case has all
    three. A case any of them holds is automated at no threshold."""
    root = with_all(tmp_path, temperature=1.3)
    simulated = Simulated(temperature=1.3, flaky=0.6)
    simulated.write(root, "test", 300, seed=5)

    def respond(body: dict[str, Any]) -> dict[str, Any]:
        response = simulated(body)
        found = _CASE.search(body["input"]["message"]["body"])
        assert found is not None
        token, number = found.group(1), int(found.group(2))
        if number >= 250:  # the service was upgraded part of the way through
            response["agent_version"] = "2.2.0"
        queue = "refill" if response["answers"]["request"]["value"] == "refill" else "scheduling"
        proposed = [{"name": "assign_queue", "arguments": {"queue": queue}}]
        if number % 7 == 0:  # a message to the patient: never released without a person
            proposed.append(ACKNOWLEDGE)
        if number % 7 > 1 or simulated.asked[token] % 3 != 2:
            proposed.append(LOOK_UP)  # which the second of three runs leaves out, on some cases
        return {**response, "actions": proposed}

    serve(monkeypatch, respond)
    pack = load_pack(root)
    manifest = pack.manifest
    dataset = load_dataset(root / "data" / "test.jsonl")
    decision = Decision(pack, origin_cap=False)
    results = [decision(record["input"]) for record in dataset.records]
    run = build_evaluation(decision, dataset, results)
    cases = op.cases_of(run.ledger, results)
    recorded = [(result.outcome.value, result.recommendation) for result in results]

    assert all(case.replays for case in cases)
    assert op.decided(manifest, cases) == recorded
    assert op.decided(op.at_threshold(manifest, 0.90), cases) == recorded  # its own, set again
    assert op.tally(cases, recorded, 1) == op.tally_of(run.report)
    automated = sum(outcome == "AUTO" for outcome, _ in recorded)
    assert 0 < automated < 250

    def kept(result: Any) -> tuple[bool, bool, bool]:
        """Which of the three a case carries: the version cap, a second signal's, a stop."""
        assert result.policy_inputs is not None
        return (
            bool(result.signals.reasons(("calibration",))),
            bool(result.signals.reasons(SECOND_SIGNALS)),
            bool(result.policy_inputs.actions),
        )

    carried = [kept(result) for result in results]
    assert sum(version for version, _, _ in carried) == 50
    assert sum(second for _, second, _ in carried) > 40
    assert sum(stop for _, _, stop in carried) == len(range(0, 300, 7))
    assert (True, True, True) in carried  # one case, at least, has all three
    # Some runs differed on a proposal alone: the answers agreed, and the case is still held.
    on_proposals_alone = [
        result
        for result in results
        if any(c.proposals and not c.disagreed for c in result.signals.checked)
    ]
    assert on_proposals_alone and all(r.outcome is not Outcome.AUTO for r in on_proposals_alone)
    for result in results:  # what a case kept is all its caps: the reasons it was given
        assert result.policy_inputs is not None
        assert tuple(result.policy_inputs.caps) == result.signals.reasons()
        if result.outcome is Outcome.AUTO:
            assert kept(result) == (False, False, False)
            assert all(a.disposition == "released" for a in result.actions or ())
    held = [index for index, found in enumerate(carried) if any(found)]
    assert len(held) > 80
    for threshold in op.sequence(manifest.policy.thresholds.review):
        at = op.decided(op.at_threshold(manifest, threshold), cases)
        assert all(at[index][0] != "AUTO" for index in held), threshold
    # And without the caps those answers would have automated more: the caps are what held them.
    freed = [
        PolicyInputs(r.policy_inputs.answers, r.policy_inputs.failures, None, {}).apply(manifest)
        for r in results
        if r.policy_inputs is not None
    ]
    assert sum(found.outcome is Outcome.AUTO for found in freed) > automated
    # The evaluation counts the cases its second signal kept from automation, a proposal's too.
    signal = run.report["run"]["second_signal"]
    assert signal["signals"][0]["disagreed"] == sum(
        any(c.differed for c in result.signals.checked) for result in results
    )
    assert signal["kept_from_automation"]["k"] > 0
