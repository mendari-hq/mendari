# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from mendari import GovernanceOverride, Outcome, Pack, Priority, RiskLevel, load_pack
from mendari.core.policy import PolicyInputs, apply_policy, threshold_reason
from mendari.core.spec import PackManifest, Risk
from mendari.providers import Answer, ProviderInfo
from tests.conftest import TRIAGE_DIR

INFO = ProviderInfo(slot="test", variant="test", type="test")
# The variants running: the triage fixture's policy automates only while its screen runs an
# agent (automation_requires).
SCREENED = {"router": "rules", "screen": "agent"}


def answer(question: str, label: str, confidence: float) -> Answer:
    return Answer(question, label, confidence, {label: confidence}, INFO)


def at_level(manifest: PackManifest, level: RiskLevel) -> PackManifest:
    return manifest.model_copy(
        update={"risk": Risk(level=level, category="test", rationale="test")}
    )


def routine(label: str, confidence: float) -> dict[str, Answer]:
    """A team, and a screen that finds no same-day symptom and no insult."""
    return {
        "destination": answer("destination", label, confidence),
        "urgency": answer("urgency", "no", 0.95),
        "abusive": answer("abusive", "no", 0.95),
    }


def emergency(urgency_probability: float = 0.40) -> dict[str, Answer]:
    """A confident team, and a screen that gives same-day symptoms `urgency_probability`."""
    return {
        "destination": answer("destination", "billing", 0.99),
        "urgency": answer("urgency", "yes", urgency_probability),
        "abusive": answer("abusive", "no", 0.95),
    }


@pytest.mark.parametrize(
    ("label", "confidence", "outcome"),
    [
        ("nurse", 0.95, Outcome.AUTO),
        ("nurse", 0.90, Outcome.AUTO),
        ("nurse", 0.89, Outcome.HUMAN_REVIEW),
        ("nurse", 0.50, Outcome.HUMAN_REVIEW),
        ("nurse", 0.49, Outcome.ABSTAIN),
        # The front desk needs more confidence (its own threshold, 0.95).
        ("front_desk", 0.93, Outcome.HUMAN_REVIEW),
        ("front_desk", 0.96, Outcome.AUTO),
        # Billing is never automated (`allow_auto: false`), however sure.
        ("billing", 0.99, Outcome.HUMAN_REVIEW),
    ],
)
def test_thresholds(triage_agent: Pack, label: str, confidence: float, outcome: Outcome) -> None:
    result = apply_policy(triage_agent.manifest, routine(label, confidence), {}, None, SCREENED)
    assert result.outcome is outcome
    assert result.recommendation == (None if outcome is Outcome.ABSTAIN else label)


def test_emergency_rule_wins_over_confident_routing(triage_agent: Pack) -> None:
    result = apply_policy(triage_agent.manifest, emergency(), {})
    assert result.outcome is Outcome.HUMAN_REVIEW
    assert result.recommendation == "nurse"
    assert result.priority is Priority.STAT
    assert result.rule == "possible-emergency"


def test_emergency_rule_respects_its_confidence_floor(triage_agent: Pack) -> None:
    assert apply_policy(triage_agent.manifest, emergency(0.30), {}).rule is None


def test_provider_failure_never_automates(triage_agent: Pack) -> None:
    answers = {"destination": answer("destination", "nurse", 0.99)}
    result = apply_policy(triage_agent.manifest, answers, {"urgency": "timeout"})
    assert result.outcome is Outcome.HUMAN_REVIEW
    assert any("timeout" in reason for reason in result.reasons)


def test_the_confidence_thresholds_reason_is_known_as_theirs(triage_agent: Pack) -> None:
    """Each of the thresholds' three reasons (AUTO, below the auto threshold, below the review
    threshold) is theirs (`threshold_reason`), for a CDS Hooks card to leave out its bare
    figures; a rule's, a cap's and a missing answer's are not."""
    thresholds = ((0.95, Outcome.AUTO), (0.60, Outcome.HUMAN_REVIEW), (0.30, Outcome.ABSTAIN))
    for confidence, outcome in thresholds:
        result = apply_policy(
            triage_agent.manifest, routine("nurse", confidence), {}, None, SCREENED
        )
        assert result.outcome is outcome and result.rule is None
        first, *others = result.reasons
        assert threshold_reason(first) and not any(map(threshold_reason, others)), result.reasons
    capped = apply_policy(triage_agent.manifest, routine("nurse", 0.99), {"urgency": "timeout"})
    assert [threshold_reason(reason) for reason in capped.reasons] == [True, False]
    missing = apply_policy(triage_agent.manifest, {}, {"destination": "timeout"})
    for result in (apply_policy(triage_agent.manifest, emergency(), {}), missing):
        assert result.reasons and not any(map(threshold_reason, result.reasons)), result.reasons


def test_a_site_code_is_named_in_the_reasons_whatever_the_outcome(triage_agent: Pack) -> None:
    """The `site_code` cap (LP-23i) turns AUTO into HUMAN_REVIEW; a decision already going to a
    person (below the auto threshold, below the review threshold, a rule's HUMAN_REVIEW) keeps
    its outcome and recommendation, and its reasons name the translation too, once."""
    translated = "site code translated (t1d, t2d): a person decides"
    cases = [
        (routine("nurse", 0.95), Outcome.AUTO, Outcome.HUMAN_REVIEW),
        (routine("nurse", 0.70), Outcome.HUMAN_REVIEW, Outcome.HUMAN_REVIEW),
        (routine("nurse", 0.30), Outcome.ABSTAIN, Outcome.ABSTAIN),
        (emergency(), Outcome.HUMAN_REVIEW, Outcome.HUMAN_REVIEW),
    ]
    for answers, alone, through in cases:
        plain = apply_policy(triage_agent.manifest, answers, {}, None, SCREENED)
        site = apply_policy(
            triage_agent.manifest, answers, {}, None, SCREENED, site_code=("t2d", "t1d", "t2d")
        )
        assert (plain.outcome, site.outcome) == (alone, through)
        assert not any("site code" in reason for reason in plain.reasons)
        assert site.reasons.count(translated) == 1, site.reasons
        if alone is not Outcome.AUTO:
            assert site.reasons == (*plain.reasons, translated)
            assert (site.recommendation, site.priority, site.rule) == (
                plain.recommendation,
                plain.priority,
                plain.rule,
            )


def test_canned_answers_never_block_alone(triage_agent: Pack) -> None:
    """The `canned` cap turns a rule's BLOCK (final at once, seen by nobody) into HUMAN_REVIEW,
    as it turns AUTO: the rule, recommendation and priority stay, and the reason says why."""
    canned = "canned answers: MENDARI_AGENT_URL is not set"
    policy = triage_agent.manifest.policy
    blocking = policy.rules[0].model_copy(update={"outcome": Outcome.BLOCK})
    manifest = triage_agent.manifest.model_copy(
        update={"policy": policy.model_copy(update={"rules": [blocking, *policy.rules[1:]]})}
    )
    live = apply_policy(manifest, emergency(), {})
    assert live.outcome is Outcome.BLOCK and live.rule == "possible-emergency"
    held = apply_policy(manifest, emergency(), {}, canned=canned)
    assert held.outcome is Outcome.HUMAN_REVIEW
    assert (held.rule, held.recommendation, held.priority) == (
        live.rule,
        live.recommendation,
        live.priority,
    )
    instead = f"{canned}: a person decides instead of its rule's BLOCK"
    assert held.reasons == (*live.reasons, instead)


def test_missing_primary_answer_abstains(triage_agent: Pack) -> None:
    answers = {"urgency": answer("urgency", "no", 0.9), "abusive": answer("abusive", "no", 0.9)}
    result = apply_policy(triage_agent.manifest, answers, {"destination": "rate_limited"})
    assert result.outcome is Outcome.ABSTAIN
    assert result.recommendation is None
    assert "rate_limited" in result.reasons[0]


@pytest.mark.parametrize("level", [RiskLevel.CLINICAL_SUPPORT, RiskLevel.PATIENT_PREDICTION])
def test_risk_levels_2_and_3_require_review_unless_governance_allows(
    triage_agent: Pack, level: RiskLevel
) -> None:
    manifest = at_level(triage_agent.manifest, level)
    unapproved = apply_policy(manifest, routine("nurse", 0.99), {}, None, SCREENED)
    assert unapproved.outcome is Outcome.HUMAN_REVIEW
    governance = GovernanceOverride(approved_by="CMIO", reference="AI-committee-2026-09")
    result = apply_policy(manifest, routine("nurse", 0.99), {}, governance, SCREENED)
    assert result.outcome is Outcome.AUTO
    assert any("governance override" in reason for reason in result.reasons)


def test_risk_level_4_is_never_autonomous(triage_agent: Pack) -> None:
    manifest = at_level(triage_agent.manifest, RiskLevel.TIME_CRITICAL)
    governance = GovernanceOverride(approved_by="CMIO", reference="AI-committee-2026-09")
    result = apply_policy(manifest, routine("nurse", 0.99), {}, governance, SCREENED)
    assert result.outcome is Outcome.HUMAN_REVIEW


def test_governance_override_needs_accountability() -> None:
    with pytest.raises(ValueError, match="approver"):
        GovernanceOverride(approved_by=" ", reference="x")


def test_automation_can_require_provider_variants(triage_agent: Pack) -> None:
    raw = triage_agent.manifest.model_dump(by_alias=True)
    raw["policy"]["automation_requires"] = {"screen": ["checked", "repeated"]}
    manifest = PackManifest.model_validate(raw)
    answers = routine("nurse", 0.99)
    guarded = apply_policy(manifest, answers, {}, None, {"router": "agent", "screen": "rules"})
    assert guarded.outcome is Outcome.HUMAN_REVIEW
    assert "automation requires screen = checked or repeated (running: rules)" in guarded.reasons
    allowed = {"router": "agent", "screen": "checked"}
    assert apply_policy(manifest, answers, {}, None, allowed).outcome is Outcome.AUTO
    assert apply_policy(manifest, answers, {}).outcome is Outcome.HUMAN_REVIEW  # not told: no AUTO

    raw["policy"]["automation_requires"] = {"screen": ["nope"], "missing": ["x"]}
    with pytest.raises(ValidationError, match="unknown slot 'missing'") as error:
        PackManifest.model_validate(raw)
    assert "unknown: ['nope']" in str(error.value)


# -- the actions cap (spec 0.4): a proposal that could not be released with AUTO ---------------

STOP = "action 'renew_prescription' (order) is never released without a person"
UNDECLARED = "the agent proposed an action its manifest does not declare"


def test_a_stopped_action_turns_auto_into_human_review_and_changes_nothing_else(
    triage_agent: Pack,
) -> None:
    manifest = triage_agent.manifest
    answers = routine("nurse", 0.95)
    plain = apply_policy(manifest, answers, {}, None, SCREENED)
    assert plain.outcome is Outcome.AUTO
    stopped = apply_policy(manifest, answers, {}, None, SCREENED, actions=(STOP,))
    assert stopped.outcome is Outcome.HUMAN_REVIEW
    assert (stopped.recommendation, stopped.priority, stopped.rule) == (
        plain.recommendation,
        plain.priority,
        plain.rule,
    )
    assert stopped.reasons == (*plain.reasons, f"{STOP}; a person decides")
    # Each stop once, in order, however often it is given.
    several = apply_policy(manifest, answers, {}, None, SCREENED, actions=(STOP, UNDECLARED, STOP))
    assert several.reasons[-2:] == (f"{STOP}; a person decides", f"{UNDECLARED}; a person decides")
    assert apply_policy(manifest, answers, {}, None, SCREENED, actions=()) == plain


@pytest.mark.parametrize(
    ("label", "confidence", "outcome"),
    [("nurse", 0.80, Outcome.HUMAN_REVIEW), ("nurse", 0.40, Outcome.ABSTAIN)],
)
def test_a_decision_already_going_to_a_person_keeps_its_outcome_and_gets_the_reason(
    triage_agent: Pack, label: str, confidence: float, outcome: Outcome
) -> None:
    manifest = triage_agent.manifest
    answers = routine(label, confidence)
    plain = apply_policy(manifest, answers, {}, None, SCREENED)
    stopped = apply_policy(manifest, answers, {}, None, SCREENED, actions=(STOP,))
    assert plain.outcome is stopped.outcome is outcome
    assert (stopped.recommendation, stopped.priority) == (plain.recommendation, plain.priority)
    assert stopped.reasons == (*plain.reasons, f"{STOP}; a person decides")
    assert [reason for reason in stopped.reasons if reason.startswith(STOP)] == [
        f"{STOP}; a person decides"
    ]


def test_a_block_stands_and_says_nothing_of_the_action(triage_agent: Pack) -> None:
    """A rule's BLOCK blocks every action: the cap neither lowers it to a person nor adds a
    reason. (Unlike the caps that turn a rule's BLOCK into HUMAN_REVIEW, this one has nothing
    to protect by doing so: no action is released under a BLOCK.)"""
    raw = triage_agent.manifest.model_dump(by_alias=True)
    raw["policy"]["rules"].insert(
        0,
        {
            "name": "block-billing",
            "when": {"question": "destination", "labels": ["billing"]},
            "outcome": "BLOCK",
            "reason": "Billing messages are not routed here.",
        },
    )
    manifest = PackManifest.model_validate(raw)
    answers = routine("billing", 0.99)
    plain = apply_policy(manifest, answers, {}, None, SCREENED)
    stopped = apply_policy(manifest, answers, {}, None, SCREENED, actions=(STOP, UNDECLARED))
    assert plain.outcome is Outcome.BLOCK and stopped == plain


def test_the_stop_is_kept_with_the_cap_that_also_lowered_the_decision(triage_agent: Pack) -> None:
    """A hold, a provider failure or a risk cap lowers the decision first; the action's reason
    is still given, so a reviewer reads why an action waits."""
    manifest = triage_agent.manifest
    answers = routine("nurse", 0.95)
    held = apply_policy(
        manifest, answers, {}, None, SCREENED, hold="an operator's hold", actions=(STOP,)
    )
    assert held.outcome is Outcome.HUMAN_REVIEW
    assert held.reasons[-2:] == (
        "automation is on hold: an operator's hold",
        f"{STOP}; a person decides",
    )
    risky = at_level(manifest, RiskLevel.TIME_CRITICAL)
    capped = apply_policy(risky, answers, {}, None, SCREENED, actions=(STOP,))
    assert capped.outcome is Outcome.HUMAN_REVIEW
    assert capped.reasons[-2:] == (
        "risk level 4 requires human review",
        f"{STOP}; a person decides",
    )
    # Governance lifts the risk cap at levels 2 and 3, and never this one.
    approved = GovernanceOverride(approved_by="CMIO", reference="AI-committee-2026-09")
    level_2 = at_level(manifest, RiskLevel.CLINICAL_SUPPORT)
    assert apply_policy(level_2, answers, {}, approved, SCREENED).outcome is Outcome.AUTO
    lifted = apply_policy(level_2, answers, {}, approved, SCREENED, actions=(STOP,))
    assert lifted.outcome is Outcome.HUMAN_REVIEW


def test_the_policys_inputs_keep_the_stops_so_it_is_applied_again_the_same(
    triage_agent: Pack,
) -> None:
    manifest = triage_agent.manifest
    inputs = PolicyInputs(routine("nurse", 0.95), {}, None, SCREENED, actions=(STOP,))
    assert inputs.apply(manifest) == apply_policy(
        manifest, routine("nurse", 0.95), {}, None, SCREENED, actions=(STOP,)
    )
    assert inputs.apply(manifest).outcome is Outcome.HUMAN_REVIEW
    assert PolicyInputs(routine("nurse", 0.95), {}, None, SCREENED).actions == ()


_TRIAGE = load_pack(TRIAGE_DIR).manifest
_ORDER = {Outcome.AUTO: 0, Outcome.HUMAN_REVIEW: 1, Outcome.ABSTAIN: 1, Outcome.BLOCK: 1}
_STOPS = st.lists(
    st.one_of(st.just(STOP), st.just(UNDECLARED), st.text(min_size=1, max_size=30)), max_size=4
)


@settings(max_examples=300, deadline=None)
@given(
    destination=st.sampled_from(list(_TRIAGE.questions["destination"].criteria)),
    confidence=st.floats(0.0, 1.0, allow_nan=False),
    urgency=st.sampled_from(list(_TRIAGE.questions["urgency"].criteria)),
    urgent_confidence=st.floats(0.0, 1.0, allow_nan=False),
    abusive=st.sampled_from(list(_TRIAGE.questions["abusive"].criteria)),
    abusive_confidence=st.floats(0.0, 1.0, allow_nan=False),
    level=st.sampled_from(list(RiskLevel)),
    governed=st.booleans(),
    failed=st.booleans(),
    hold=st.one_of(st.none(), st.just("a hold")),
    canned=st.one_of(st.none(), st.just("canned answers")),
    stops=_STOPS,
)
def test_no_proposal_raises_an_outcome(
    destination: str,
    confidence: float,
    urgency: str,
    urgent_confidence: float,
    abusive: str,
    abusive_confidence: float,
    level: RiskLevel,
    governed: bool,
    failed: bool,
    hold: str | None,
    canned: str | None,
    stops: list[str],
) -> None:
    """Policy only lowers automation, the actions cap included: whatever the answers, the caps
    and the stops, the outcome with stops is the outcome without, or HUMAN_REVIEW where that
    was AUTO; the recommendation, the priority and the rule do not change; and nothing a
    proposal says ever makes a decision AUTO that was not."""
    manifest = at_level(_TRIAGE, level)
    answers = {
        "destination": answer("destination", destination, confidence),
        "urgency": answer("urgency", urgency, urgent_confidence),
        "abusive": answer("abusive", abusive, abusive_confidence),
    }
    governance = GovernanceOverride("cmo", "ref") if governed else None
    failures = {"urgency": "timeout"} if failed else {}
    options: dict[str, Any] = {"hold": hold, "canned": canned}
    plain = apply_policy(manifest, answers, failures, governance, SCREENED, **options)
    stopped = apply_policy(
        manifest, answers, failures, governance, SCREENED, actions=tuple(stops), **options
    )
    if plain.outcome is Outcome.AUTO and stops:
        assert stopped.outcome is Outcome.HUMAN_REVIEW
    else:
        assert stopped.outcome is plain.outcome
    assert _ORDER[stopped.outcome] >= _ORDER[plain.outcome]
    assert (stopped.recommendation, stopped.priority, stopped.rule) == (
        plain.recommendation,
        plain.priority,
        plain.rule,
    )
    assert stopped.reasons[: len(plain.reasons)] == plain.reasons  # reasons are only added
    if stopped.outcome is Outcome.AUTO:
        assert not stops and stopped == plain
