# SPDX-License-Identifier: Apache-2.0
"""Turn provider answers into one of AUTO / HUMAN_REVIEW / ABSTAIN / BLOCK.

Order of evaluation:
1. Policy rules, first match wins (e.g. "possible emergency -> stat nurse review").
2. Otherwise, confidence thresholds on the primary question.
3. Caps that can only make a decision *less* automated: a community pack (never AUTO, and a
   rule's BLOCK goes to a person instead, since a block is final at once), per-label limits,
   provider failures, and the pack's risk level. A deploying organisation may
   lift the risk cap for levels 2-3 with a named, recorded governance approval; level 4 can
   never run autonomously, nor can a community pack at any level.
4. The `site_code` cap (LP-23i): an input that took a value through a site's translation of its
   own code (`core/site_codes.py`) never automates, and a rule's BLOCK on it goes to a person
   (a block is final at once). Nothing lifts it, governance included: a site's configuration
   never makes AUTO, or a final BLOCK, that the pack alone could not. A decision already going
   to a person (HUMAN_REVIEW, ABSTAIN) keeps its outcome and gets the same reason.
5. The `not_offered` cap, alike: a list read without a source the site declared its EHR does
   not offer (`standards/ehr_declarations.py`) may be incomplete, so the decision never
   automates and its rule's BLOCK goes to a person, the limitation a reason whatever the
   outcome. Without the declaration the pack alone abstains, so nothing lifts this one either.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

from mendari.core.spec import Condition, Outcome, PackManifest, Priority, RiskLevel
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
    """A deploying organisation's recorded decision to allow AUTO for a level 2-3 pack."""

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
    manifest: PackManifest,
    answers: Mapping[str, Answer],
    failures: Mapping[str, str],
    governance: GovernanceOverride | None = None,
    variants: Mapping[str, str] | None = None,
    *,
    community: bool = False,
    hold: str | None = None,
    blocks_to_person: str | None = None,
    site_code: Sequence[str] = (),
    not_offered: Sequence[str] = (),
) -> PolicyResult:
    """`variants` is the running variant of each provider slot, for `automation_requires`;
    without it, a pack that sets `automation_requires` never automates. A `community` pack
    (`core/origin.py`) recommends and never automates, whatever its rules or confidence. A
    `hold` (why automation is stopped: an operator's hold, a used-up quota) turns AUTO into
    HUMAN_REVIEW and changes nothing else. `blocks_to_person` (why a pack from a registry may
    not act alone) turns its own rule's BLOCK into HUMAN_REVIEW as well. `site_code` names the
    code sets a site's translation gave the input a value through (LP-23i): with any, AUTO and
    a rule's BLOCK become HUMAN_REVIEW, the reasons name the code sets whatever the outcome, and
    nothing else changes. `not_offered` names each limitation of a list read without a source the
    site declared its EHR does not offer (`limitation`): the same cap, its own reason."""
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
                auto_at = label_policy.auto_threshold
            recommendation = primary.label
            if primary.confidence >= auto_at:
                outcome = Outcome.AUTO
                reasons.append(
                    f"confidence {primary.confidence:.2f} >= auto threshold {auto_at:.2f}"
                )
            elif primary.confidence >= policy.thresholds.review:
                outcome = Outcome.HUMAN_REVIEW
                reasons.append(
                    f"confidence {primary.confidence:.2f} is below the auto threshold "
                    f"{auto_at:.2f} for {primary.label!r}"
                )
            else:
                outcome, recommendation = Outcome.ABSTAIN, None
                reasons.append(
                    f"confidence {primary.confidence:.2f} < review threshold "
                    f"{policy.thresholds.review:.2f}"
                )

    if blocks_to_person and rule_name is not None and outcome is Outcome.BLOCK:
        # A pack from a registry not approved here, or recalled (`core/decision.py`): its own
        # rule's BLOCK, final at once and seen by nobody, goes to a person too.
        reasons.append(f"{blocks_to_person}: a person decides instead of its rule's BLOCK")
        outcome = Outcome.HUMAN_REVIEW
    if community and rule_name is not None and outcome is Outcome.BLOCK:
        # A block is final at once and no person sees it; a community pack's rule never decides
        # alone (its load refuses such a rule; this covers a pack loaded by path).
        reasons.append(
            "a community pack (not clinically reviewed) never blocks on its own: a person decides"
        )
        outcome = Outcome.HUMAN_REVIEW
    if translated and rule_name is not None and outcome is Outcome.BLOCK:
        # A rule's BLOCK on an input read through a site's translation (LP-23i): a wrong
        # translation must reach a person, and a block is final at once and seen by nobody.
        reasons.append(
            f"site code translated ({', '.join(translated)}): a person decides instead of its "
            "rule's BLOCK"
        )
        outcome = Outcome.HUMAN_REVIEW
    if limits and rule_name is not None and outcome is Outcome.BLOCK:
        # A rule's BLOCK on a list the EHR could not give whole: a person decides, as above.
        reasons.extend(f"{limit}: a person decides instead of its rule's BLOCK" for limit in limits)
        outcome = Outcome.HUMAN_REVIEW
    if outcome is Outcome.AUTO:
        outcome = _cap(
            manifest,
            recommendation,
            failures,
            governance,
            variants or {},
            reasons,
            community,
            hold,
            translated,
            limits,
        )
    site_reason = f"site code translated ({', '.join(translated)}): a person decides"
    if translated and not any(reason.startswith(site_reason) for reason in reasons):
        # Already HUMAN_REVIEW or ABSTAIN: the reason only says a translation was read, for the
        # person who decides; it changes no outcome.
        reasons.append(site_reason)
    for limit in limits:
        if not any(reason.startswith(f"{limit}: a person decides") for reason in reasons):
            reasons.append(f"{limit}: a person decides")  # likewise: no outcome changes
    if outcome is Outcome.ABSTAIN:
        recommendation = None
    return PolicyResult(outcome, recommendation, priority, tuple(reasons), rule_name)


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
    manifest: PackManifest,
    recommendation: str | None,
    failures: Mapping[str, str],
    governance: GovernanceOverride | None,
    variants: Mapping[str, str],
    reasons: list[str],
    community: bool,
    hold: str | None = None,
    site_code: Sequence[str] = (),
    not_offered: Sequence[str] = (),
) -> Outcome:
    """AUTO, or HUMAN_REVIEW with why. The site code and not-offered caps and a hold keep the
    reason the other caps would give too."""
    outcome = _caps(manifest, recommendation, failures, governance, variants, reasons, community)
    if site_code:
        reasons.append(f"site code translated ({', '.join(site_code)}): a person decides")
        outcome = Outcome.HUMAN_REVIEW
    for limit in not_offered:
        reasons.append(f"{limit}: a person decides")
        outcome = Outcome.HUMAN_REVIEW
    if hold is not None:
        reasons.append(f"automation is on hold: {hold}")
        return Outcome.HUMAN_REVIEW
    return outcome


def _caps(
    manifest: PackManifest,
    recommendation: str | None,
    failures: Mapping[str, str],
    governance: GovernanceOverride | None,
    variants: Mapping[str, str],
    reasons: list[str],
    community: bool,
) -> Outcome:
    if community:
        reasons.append("a community pack (not clinically reviewed) recommends and never automates")
        return Outcome.HUMAN_REVIEW
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
