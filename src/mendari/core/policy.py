# SPDX-License-Identifier: Apache-2.0
"""Turn provider answers into one of AUTO / HUMAN_REVIEW / ABSTAIN / BLOCK.

Order of evaluation:
1. Policy rules, first match wins (e.g. "possible emergency -> stat nurse review").
2. Otherwise, confidence thresholds on the primary question.
3. Caps that can only make a decision *less* automated: per-label limits, provider failures,
   and the agent's risk level. A deploying organisation may
   lift the risk cap for levels 2-3 with a named, recorded governance approval; level 4 can
   never run autonomously.
4. The `site_code` cap (LP-23i): an input that took a value through a site's translation of its
   own code (`core/site_codes.py`) never automates, and a rule's BLOCK on it goes to a person
   (a block is final at once). Nothing lifts it, governance included: a site's configuration
   never makes AUTO, or a final BLOCK, that the agent alone could not. A decision already going
   to a person (HUMAN_REVIEW, ABSTAIN) keeps its outcome and gets the same reason.
5. The `not_offered` cap, alike: a list read without a source the site declared its EHR does
   not offer (`standards/ehr_declarations.py`) may be incomplete, so the decision never
   automates and its rule's BLOCK goes to a person, the limitation a reason whatever the
   outcome. Without the declaration the agent alone abstains, so nothing lifts this one either.
6. The `canned` cap: on a server with sign-in, answers an agent read from its `mock_file`
   (`MENDARI_AGENT_URL` unset, `providers/agent.py`) never automate: AUTO and a rule's BLOCK
   (final at once) become HUMAN_REVIEW, and the reason says so whatever the outcome. Nothing
   lifts it, governance included. Without sign-in (`mendari demo`, tests, a local dashboard)
   the server passes none and nothing changes.
7. The caps a provider found while it answered (`lowered`, ADR-0024, `providers/signals.py`),
   in this order: the version cap (the agent changed since its confidence was calibrated), then
   the second signal's (repeated runs disagreed, then a second model disagreed). They come
   first of the two new caps because they are about the answers the steps above just decided
   from: how far the confidence can be believed, then whether the answer held when it was asked
   again. AUTO becomes HUMAN_REVIEW; a decision already going to a person (HUMAN_REVIEW,
   ABSTAIN) keeps its outcome and gets the same reason; a BLOCK stands, with the cap as a note
   (a block is final: no person decides). Nothing lifts one, governance included, and no
   manifest switches one off.
8. The `actions` cap (spec §13.7, ADR-0023), last: what the agent asks to do, once what it
   answered is settled. A proposed action that could not be released with an AUTO decision (one
   the manifest does not declare, arguments that do not match their schema, a composite
   member's proposal, or an action never released without a person) turns AUTO into
   HUMAN_REVIEW, with the reason; a decision already going to a person keeps its outcome and
   gets the same reason, and a BLOCK stands and says nothing more (it blocks every action).
   Nothing lifts it.

Steps 3 to 8 only lower automation: none turns any outcome into AUTO, and none turns a
decision that goes to a person into a BLOCK. `apply_policy` is all eight, and it is the only
way a decision, or a replay of one (`PolicyInputs.apply`), gets its outcome.

How the reasons read. The first says what the rules or the thresholds decided; each cap that
applies follows, in the order above, once. A reason of steps 7 and 8 ends "; a person decides"
(`PERSON_DECIDES`) wherever a person does, so several read as one list:

    confidence 0.97 >= auto threshold 0.90
    the agent changed since its confidence was calibrated (was 2.1.0, is 2.2.0); a person decides
    2 of 3 runs agreed on request; a person decides
    action 'renew_prescription' (order) is never released without a person; a person decides

What a declared action needs before it is released is here too, beside the risk caps and never
in a manifest: `EFFECT_GATES` (an effect's gate), `gate_of` (a manifest, the agent's origin, a
second signal that did not agree on the proposal and arguments that can hold free text may only
tighten it) and `disposition` (the gate, by the decision's state).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Literal

from mendari.core.actions import Checked, Disposition, Reason
from mendari.core.spec import AgentManifest, Condition, Effect, Outcome, Priority, RiskLevel
from mendari.providers.base import Answer

# Highest risk level at which AUTO is permitted without a governance override.
MAX_AUTONOMOUS_RISK = RiskLevel.CLINICAL_INFORMATION
OVERRIDABLE_RISK = (RiskLevel.CLINICAL_SUPPORT, RiskLevel.PATIENT_PREDICTION)
# How the confidence thresholds' reason begins (step 2), in each of its three forms below.
_THRESHOLD_REASON = re.compile(r"confidence \d\.\d\d (?:>=|<|is below) ")


def threshold_reason(reason: str) -> bool:
    """Whether `reason` is the confidence thresholds' (step 2): it gives the primary answer's
    confidence and a threshold as bare figures, with no interval, so a CDS Hooks card leaves it
    out (`standards/cds_hooks.py`). The audit trail keeps it as it is."""
    return _THRESHOLD_REASON.match(reason) is not None


@dataclass(frozen=True)
class GovernanceOverride:
    """A deploying organisation's recorded decision to allow AUTO for a level 2-3 agent."""

    approved_by: str
    reference: str
    allow_auto: bool = True

    def __post_init__(self) -> None:
        if not self.approved_by.strip() or not self.reference.strip():
            raise ValueError("a governance override needs an approver and a reference")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class PolicyResult:
    outcome: Outcome
    recommendation: str | None
    priority: Priority
    reasons: tuple[str, ...]
    rule: str | None = None


def apply_policy(
    manifest: AgentManifest,
    answers: Mapping[str, Answer],
    failures: Mapping[str, str],
    governance: GovernanceOverride | None = None,
    variants: Mapping[str, str] | None = None,
    *,
    hold: str | None = None,
    blocks_to_person: str | None = None,
    site_code: Sequence[str] = (),
    not_offered: Sequence[str] = (),
    canned: str | None = None,
    caps: Sequence[str] = (),
    actions: Sequence[str] = (),
) -> PolicyResult:
    """`variants` is the running variant of each provider slot, for `automation_requires`;
    without it, an agent that sets `automation_requires` never automates. A
    `hold` (why automation is stopped: an operator's hold, a used-up quota) turns AUTO into
    HUMAN_REVIEW and changes nothing else. `blocks_to_person` (why an agent from a registry may
    not act alone) turns its own rule's BLOCK into HUMAN_REVIEW as well. `site_code` names the
    code sets a site's translation gave the input a value through (LP-23i): with any, AUTO and
    a rule's BLOCK become HUMAN_REVIEW, the reasons name the code sets whatever the outcome, and
    nothing else changes. `not_offered` names each limitation of a list read without a source the
    site declared its EHR does not offer (`limitation`): the same cap, its own reason. `canned`
    says why the answers are canned (`ProviderOutput.canned`): AUTO and a rule's BLOCK become
    HUMAN_REVIEW, and the reason is given whatever the outcome. `caps` are the reasons a provider
    found that the decision may not be automated (`Signals.reasons`: the version cap, then the
    second signal's), and `actions` says, for each proposed action that could not be released
    with an AUTO decision, why (`action_stops`): for either, AUTO becomes HUMAN_REVIEW, and
    nothing else changes (`lowered`, `_action_capped`)."""
    translated = tuple(sorted(set(site_code)))
    limits = tuple(dict.fromkeys(not_offered))
    policy = manifest.policy
    primary = answers.get(policy.primary)
    reasons: list[str] = []
    rule_name = None

    for rule in policy.rules:
        if matches(rule.when, answers.get(rule.when.question)):
            rule_name = rule.name
            outcome = rule.outcome
            recommendation = rule.recommendation or (primary.label if primary else None)
            priority = rule.priority or policy.default_priority
            reasons.append(f"rule {rule.name}: {rule.reason}")
            break
    else:
        priority = policy.default_priority
        if primary is None:
            outcome, recommendation = Outcome.ABSTAIN, None
            reasons.append(
                f"no answer for {policy.primary!r} "
                f"({failures.get(policy.primary, 'provider unavailable')})"
            )
        else:
            label_policy = policy.labels.get(primary.label)
            auto_at = policy.thresholds.auto
            if label_policy and label_policy.auto_threshold is not None:
                auto_at = max(label_policy.auto_threshold, policy.thresholds.review)
            recommendation = primary.label
            if primary.confidence < policy.thresholds.review:
                outcome, recommendation = Outcome.ABSTAIN, None
                reasons.append(
                    f"confidence {primary.confidence:.2f} < review threshold "
                    f"{policy.thresholds.review:.2f}"
                )
            elif primary.confidence >= auto_at:
                outcome = Outcome.AUTO
                reasons.append(
                    f"confidence {primary.confidence:.2f} >= auto threshold {auto_at:.2f}"
                )
            else:
                outcome = Outcome.HUMAN_REVIEW
                reasons.append(
                    f"confidence {primary.confidence:.2f} is below the auto threshold "
                    f"{auto_at:.2f} for {primary.label!r}"
                )

    was_block = rule_name is not None and outcome is Outcome.BLOCK
    if blocks_to_person and was_block:
        # An agent from a registry not approved here, or recalled (`core/decision.py`): its own
        # rule's BLOCK, final at once and seen by nobody, goes to a person too.
        reasons.append(f"{blocks_to_person}: a person decides instead of its rule's BLOCK")
        outcome = Outcome.HUMAN_REVIEW
    if translated and was_block:
        # A rule's BLOCK on an input read through a site's translation (LP-23i): a wrong
        # translation must reach a person, and a block is final at once and seen by nobody.
        reasons.append(
            f"site code translated ({', '.join(translated)}): a person decides instead of its "
            "rule's BLOCK"
        )
        outcome = Outcome.HUMAN_REVIEW
    if limits and was_block:
        # A rule's BLOCK on a list the EHR could not give whole: a person decides, as above.
        reasons.extend(f"{limit}: a person decides instead of its rule's BLOCK" for limit in limits)
        outcome = Outcome.HUMAN_REVIEW
    if canned is not None and was_block:
        # A rule's BLOCK on canned answers: a block is final at once and seen by nobody, and
        # canned answers never decide alone on a server with sign-in.
        reasons.append(f"{canned}: a person decides instead of its rule's BLOCK")
        outcome = Outcome.HUMAN_REVIEW
    if outcome is Outcome.AUTO and recommendation is None:
        outcome = Outcome.ABSTAIN
        reasons.append("automation requires a recommendation")
    if outcome is Outcome.AUTO:
        outcome = _cap(
            manifest,
            recommendation,
            failures,
            governance,
            variants or {},
            reasons,
            hold,
            translated,
            limits,
            canned,
        )
    site_reason = f"site code translated ({', '.join(translated)}): a person decides"
    if translated and not any(reason.startswith(site_reason) for reason in reasons):
        # Already HUMAN_REVIEW or ABSTAIN: the reason only says a translation was read, for the
        # person who decides; it changes no outcome.
        reasons.append(site_reason)
    for limit in limits:
        if not any(reason.startswith(f"{limit}: a person decides") for reason in reasons):
            reasons.append(f"{limit}: a person decides")  # likewise: no outcome changes
    if canned is not None and not any(reason.startswith(canned) for reason in reasons):
        reasons.append(canned_reason(canned))  # likewise, last as its step is (6)
    if outcome is Outcome.ABSTAIN:
        recommendation = None
    decided = PolicyResult(outcome, recommendation, priority, tuple(reasons), rule_name)
    # Steps 7 and 8, after every other cap and in this order: each only adds reasons, and
    # turns AUTO into HUMAN_REVIEW and no other outcome into anything.
    return _action_capped(lowered(decided, caps), actions)


@dataclass(frozen=True)
class PolicyInputs:
    """Everything `apply_policy` decides from besides the manifest, as one decision gave it. A
    decision keeps its own in memory (`DecisionResult.policy_inputs`), so its policy can be
    applied again to the same answers under a manifest that differs in one setting, with no
    new call to a provider (`evals/operating_point.py`: another automation threshold)."""

    answers: Mapping[str, Answer]
    failures: Mapping[str, str]
    governance: GovernanceOverride | None = None
    variants: Mapping[str, str] | None = None
    hold: str | None = None
    blocks_to_person: str | None = None
    site_code: Sequence[str] = ()
    not_offered: Sequence[str] = ()
    canned: str | None = None
    # Why a provider says the decision may not be automated (`lowered`): each a reason, the
    # version cap of a calibration first, then the second signal's (`Signals.reasons`).
    caps: Sequence[str] = ()
    # Why each proposed action could not be released with an AUTO decision (`action_stops`).
    actions: Sequence[str] = ()

    def apply(self, manifest: AgentManifest) -> PolicyResult:
        return apply_policy(
            manifest,
            self.answers,
            self.failures,
            self.governance,
            self.variants,
            hold=self.hold,
            blocks_to_person=self.blocks_to_person,
            site_code=self.site_code,
            not_offered=self.not_offered,
            canned=self.canned,
            caps=self.caps,
            actions=self.actions,
        )


# How a reason of steps 7 and 8 ends wherever a person decides (as the `canned` cap's does).
PERSON_DECIDES = "; a person decides"


def lowered(result: PolicyResult, caps: Sequence[str]) -> PolicyResult:
    """`result` under the caps its providers found (step 7, ADR-0024): with any, AUTO becomes
    HUMAN_REVIEW, each cap a reason that says a person decides. A result that already goes to a
    person (HUMAN_REVIEW, ABSTAIN) keeps its outcome, its recommendation, its priority and its
    rule, and gets the same reasons; a BLOCK keeps them too and gets each cap as it is (a block
    is final: no person decides). It can only lower automation: AUTO is never what it returns
    unless there is no cap and `result` was AUTO, and no other outcome changes."""
    found = tuple(dict.fromkeys(caps))
    if not found:
        return result
    if result.outcome is not Outcome.BLOCK:
        found = tuple(f"{reason}{PERSON_DECIDES}" for reason in found)
    added = tuple(reason for reason in found if reason not in result.reasons)
    outcome = Outcome.HUMAN_REVIEW if result.outcome is Outcome.AUTO else result.outcome
    return replace(result, outcome=outcome, reasons=(*result.reasons, *added))


def _action_capped(result: PolicyResult, stops: Sequence[str]) -> PolicyResult:
    """`result` under the `actions` cap (step 8): with any stop, AUTO becomes HUMAN_REVIEW, and
    a decision already going to a person keeps its outcome; either way each stop is a reason
    that says a person decides, once. A BLOCK stands and says nothing more: it blocks every
    action. Never raises an outcome."""
    if not stops or result.outcome is Outcome.BLOCK:
        return result
    found = (f"{stop}{PERSON_DECIDES}" for stop in dict.fromkeys(stops))
    added = tuple(reason for reason in found if reason not in result.reasons)
    outcome = Outcome.HUMAN_REVIEW if result.outcome is Outcome.AUTO else result.outcome
    return replace(result, outcome=outcome, reasons=(*result.reasons, *added))


# -- actions (spec §13, ADR-0023) -----------------------------------------------------------------
# What a declared action needs before it is released. The runtime's, as the risk caps are
# (ADR-0002): a manifest names an action's effect and may tighten its gate (`allow_auto:
# false`); nothing in a manifest, and nothing an agent proposes, loosens one. What a manifest
# says of an action's arguments tightens it too (ADR-0027): where they can hold free text, the
# gate `auto` becomes `person`, so no automated decision releases words nobody read.


class Gate(StrEnum):
    """What an action needs before it is released, loosest first (`GATES`)."""

    DECISION = "decision"  # a decision that is not BLOCK
    AUTO = "auto"  # an AUTO decision, or else the decision's reviewer confirming it
    PERSON = "person"  # the decision's reviewer confirming it: never released without a person


GATES = (Gate.DECISION, Gate.AUTO, Gate.PERSON)  # loosest first
EFFECT_GATES: Mapping[Effect, Gate] = {
    Effect.READ: Gate.DECISION,
    Effect.DRAFT: Gate.DECISION,
    Effect.WRITE: Gate.AUTO,
    Effect.CONTACT: Gate.AUTO,
    Effect.ORDER: Gate.PERSON,
}
# A gate in words: the Decision Card's and the console's, from here only.
GATE_TEXT: Mapping[Gate, str] = {
    Gate.DECISION: "With any decision that is not blocked.",
    Gate.AUTO: "Only with an automated decision; otherwise when a reviewer confirms it.",
    Gate.PERSON: "Never without a person: when a reviewer confirms the decision.",
}
# The gate `person` in words for an action whose arguments can hold free text (ADR-0027): what
# `gate_words` gives in place of `GATE_TEXT`'s, whatever made the gate `person`. Confirming the
# decision does not release such an action: the review has to name it (`release_actions`,
# `AuditStore.resolve_review`), and the reader is told so, and why.
FREE_TEXT_GATE = (
    "Never without a person: when a reviewer confirms the decision and names this action, "
    "since its arguments can hold free text."
)
# Where a decision's review stands: none is needed, one is awaited, or a reviewer confirmed or
# overrode the recommendation.
Review = Literal["none", "pending", "confirmed", "overridden"]


def stricter(one: Gate, other: Gate) -> Gate:
    """The stricter of two gates."""
    return max(one, other, key=GATES.index)


def gate_of(
    effect: Effect,
    *,
    allow_auto: bool = True,
    vouched: bool = True,
    agreed: bool = True,
    free_text: bool = False,
) -> Gate:
    """A declared action's gate: its effect's (`EFFECT_GATES`), and never looser. The manifest
    tightens it with `allow_auto: false` (never without a person). For an agent the deployment
    has not `vouched` for (one from a registry whose digest its organization has not approved,
    or that is recalled), an effect is a claim nobody here
    checked: nothing it proposes is released without a person, so `read` and `draft` take the
    gate of `write`. A proposal its second signal did not agree on (`agreed` false: only some
    repeated runs proposed it, or a check's second member proposed another; `Proposed.disputed`)
    is never released without a person either, whatever its effect: an agent unsure of what to
    do is not left to do it.

    A fourth way a gate tightens (ADR-0027): arguments that can hold free text (`free_text`:
    `ActionSpec.free_text`). Where the gate would be `auto`, by the action's effect or because
    nobody vouched for the agent, it is `person`: an automated decision would send words nobody
    read to a chart or a patient. So a `write` or a `contact` with them is a person's, as is a
    `read` or a `draft` of an agent nobody vouched for. A `read` or a `draft` of an agent the
    deployment vouches for keeps the gate `decision` (a read changes nothing, and a person sees
    a draft before it takes effect), and an `order` was a person's already."""
    gate = EFFECT_GATES[effect]
    if not vouched:
        gate = stricter(gate, Gate.AUTO)
    if free_text and gate is Gate.AUTO:
        gate = Gate.PERSON
    if not allow_auto or not agreed:
        gate = Gate.PERSON
    return gate


def gate_words(
    effect: Effect, *, allow_auto: bool = True, vouched: bool = True, free_text: bool = False
) -> str:
    """A declared action's gate in words, as the Decision Card, the console and the committee
    packet show it: `GATE_TEXT`'s for its gate (`gate_of`, with its free text), and
    `FREE_TEXT_GATE` wherever that gate is `person` and the action's arguments can hold free
    text. That is a `write` or a `contact` the free text made a person's, and also an `order`
    and an action the manifest tightens (`allow_auto: false`): a review releases a held action
    with free text only by name, whatever made its gate `person`, so none of them is said to be
    released "when a reviewer confirms the decision". A `read` or a `draft` of an agent the
    deployment vouches for keeps `GATE_TEXT`'s words: its gate is `decision`, and it waits for
    no review."""
    gate = gate_of(effect, allow_auto=allow_auto, vouched=vouched, free_text=free_text)
    if free_text and gate is Gate.PERSON:
        return FREE_TEXT_GATE
    return GATE_TEXT[gate]


def risk_permits_auto(level: RiskLevel, governance: GovernanceOverride | None) -> bool:
    """Whether an agent of this risk level may automate at all, as `_caps` decides it: levels 0
    and 1 always, 2 and 3 with a governance approval in force, 4 never. So an action released
    only with an AUTO decision is released without a person only where this holds."""
    if level <= MAX_AUTONOMOUS_RISK:
        return True
    return level in OVERRIDABLE_RISK and governance is not None and governance.allow_auto


def action_stops(checked: Sequence[Checked], *, vouched: bool = True) -> tuple[str, ...]:
    """Why each proposed action could not be released with an AUTO decision, in order and once
    each: what `apply_policy` takes as `actions`. An agent that proposes what it never declared,
    or arguments its own schema refuses, is not trusted to automate; an action never released
    without a person gets one only if the decision goes to a person. That is one the manifest
    tightens (`allow_auto: false`), one whose effect is a person's (an `order`), and one whose
    arguments can carry free text where its gate would be `auto` (ADR-0027): each says which,
    the first that applies in that order. A reason names a declared action only: any other name
    is the agent's text."""
    stops: list[str] = []
    for item in checked:
        name = item.proposed.name
        if item.refusal == "invalid_arguments" and item.spec is not None:
            stops.append(f"action {name!r}: its arguments do not match its schema")
        elif item.refusal == "composite_member":
            stops.append(
                "a composite's member proposed an action, which this version does not take"
            )
        elif item.refusal is not None or item.spec is None:
            stops.append("the agent proposed an action its manifest does not declare")
        elif not item.spec.allow_auto:
            stops.append(f"action {name!r} is never released without a person (allow_auto: false)")
        elif gate_of(item.spec.effect, vouched=vouched) is Gate.PERSON:
            effect = item.spec.effect.value
            stops.append(f"action {name!r} ({effect}) is never released without a person")
        else:
            gate = gate_of(item.spec.effect, vouched=vouched, free_text=item.spec.free_text)
            if gate is Gate.PERSON:  # only its free text made it so: the effect alone did not
                stops.append(
                    f"action {name!r} can carry free text and is never released without a person"
                )
    return tuple(dict.fromkeys(stops))


def disposition(
    gate: Gate,
    *,
    outcome: Outcome,
    recommendation: str | None,
    review: Review,
    automating: bool = True,
    refused_before: bool = False,
) -> tuple[Disposition, Reason]:
    """What may be done with a declared, well-formed action, by its gate and its decision's
    state (the table of spec section 13.7, its rules in this order):

    1. A BLOCK decision blocks every action.
    2. The gate `decision` needs nothing more: released.
    3. Without a recommendation (an abstention, or a review that has none) there is nothing for
       a reviewer to confirm: blocked.
    4. A review that is awaited holds the action for the reviewer.
    5. A review already resolved is closed: an action asked for afterwards is blocked (the
       reviewer never saw it).
    6. An AUTO decision releases the gate `auto`, unless an earlier proposal on the decision was
       refused (`refused_before`) or the agent may not automate now (`automating`: a hold, an
       approval no longer in force, a recall); the gate `person` has nobody to wait for.
    7. Anything else has no reviewer: blocked.

    `held` is returned only for a decision that awaits review and has a recommendation, and
    `released` only by rules 2 and 6."""
    if outcome is Outcome.BLOCK:
        return "blocked", "decision_blocked"
    if gate is Gate.DECISION:
        return "released", "with_decision"
    if recommendation is None:
        return "blocked", "no_recommendation"
    if review == "pending":
        return "held", "awaiting_review"
    if review in ("confirmed", "overridden"):
        return "blocked", "review_closed"
    if outcome is Outcome.AUTO and gate is Gate.AUTO:
        if refused_before:
            return "blocked", "earlier_refusal"
        if not automating:
            return "blocked", "not_automated"
        return "released", "automated"
    return "blocked", "no_reviewer"


def canned_reason(canned: str) -> str:
    """The `canned` cap's reason: why the answers are canned, and that a person decides."""
    return f"{canned}{PERSON_DECIDES}"


def limitation(name: str, types: Sequence[str], attested_by: str) -> str:
    """How a reason says list `name` was read without `types`, which the site declared its EHR
    does not offer (`standards/ehr_declarations.py`), and who attested that."""
    return (
        f"{name} may be incomplete: read without {', '.join(types)}, which this EHR does not "
        f"offer (declared, {attested_by})"
    )


def matches(condition: Condition, answer: Answer | None) -> bool:
    if answer is None or answer.confidence < condition.min_confidence:
        return False
    if condition.min_probability is not None:
        mass = sum(answer.probabilities.get(label, 0.0) for label in frozenset(condition.labels))
        if mass < condition.min_probability:
            return False
    elif condition.labels and answer.label not in condition.labels:
        return False
    if condition.min_score is not None and (
        answer.value is None or answer.value < condition.min_score
    ):
        return False
    return not (
        condition.max_score is not None
        and (answer.value is None or answer.value > condition.max_score)
    )


def _cap(
    manifest: AgentManifest,
    recommendation: str | None,
    failures: Mapping[str, str],
    governance: GovernanceOverride | None,
    variants: Mapping[str, str],
    reasons: list[str],
    hold: str | None = None,
    site_code: Sequence[str] = (),
    not_offered: Sequence[str] = (),
    canned: str | None = None,
) -> Outcome:
    """AUTO, or HUMAN_REVIEW with why. The site code, not-offered and canned caps and a hold
    keep the reason the other caps would give too."""
    outcome = _caps(manifest, recommendation, failures, governance, variants, reasons)
    if site_code:
        reasons.append(f"site code translated ({', '.join(site_code)}): a person decides")
        outcome = Outcome.HUMAN_REVIEW
    for limit in not_offered:
        reasons.append(f"{limit}: a person decides")
        outcome = Outcome.HUMAN_REVIEW
    if canned is not None:
        reasons.append(canned_reason(canned))
        outcome = Outcome.HUMAN_REVIEW
    if hold is not None:
        reasons.append(f"automation is on hold: {hold}")
        return Outcome.HUMAN_REVIEW
    return outcome


def _caps(
    manifest: AgentManifest,
    recommendation: str | None,
    failures: Mapping[str, str],
    governance: GovernanceOverride | None,
    variants: Mapping[str, str],
    reasons: list[str],
) -> Outcome:
    label_policy = manifest.policy.labels.get(recommendation or "")
    if label_policy is not None and not label_policy.allow_auto:
        reasons.append(f"policy never automates {recommendation!r}")
        return Outcome.HUMAN_REVIEW
    if failures:
        reasons.append(
            f"provider failure ({', '.join(sorted(set(failures.values())))}); "
            "safety checks may be incomplete"
        )
        return Outcome.HUMAN_REVIEW
    for slot, allowed in manifest.policy.automation_requires.items():
        running = variants.get(slot)
        if running not in allowed:
            reasons.append(
                f"automation requires {slot} = {' or '.join(allowed)} (running: {running})"
            )
            return Outcome.HUMAN_REVIEW
    level = manifest.risk.level
    if level <= MAX_AUTONOMOUS_RISK:
        return Outcome.AUTO
    if level in OVERRIDABLE_RISK and governance is not None and governance.allow_auto:
        reasons.append(
            f"risk level {int(level)} AUTO permitted by governance override "
            f"({governance.approved_by}, {governance.reference})"
        )
        return Outcome.AUTO
    reasons.append(f"risk level {int(level)} requires human review")
    return Outcome.HUMAN_REVIEW
