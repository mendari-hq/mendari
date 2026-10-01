"""Turn provider answers into one of AUTO / HUMAN_REVIEW / ABSTAIN / BLOCK.

Order of evaluation:
1. Policy rules, first match wins (e.g. "possible emergency -> stat nurse review").
2. Otherwise, confidence thresholds on the primary question.
3. Caps that can only make a decision *less* automated: per-label limits, provider failures,
   and the pack's risk level. A deploying organisation may lift the risk cap for levels 2-3
   with a named, recorded governance approval; level 4 can never run autonomously.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass

from mendari.core.spec import Condition, Outcome, PackManifest, Priority, RiskLevel
from mendari.providers.base import Answer

# Highest risk level at which AUTO is permitted without a governance override.
MAX_AUTONOMOUS_RISK = RiskLevel.CLINICAL_INFORMATION
OVERRIDABLE_RISK = (RiskLevel.CLINICAL_SUPPORT, RiskLevel.PATIENT_PREDICTION)


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
) -> PolicyResult:
    """`variants` is the running variant of each provider slot, for `automation_requires`;
    without it, a pack that sets `automation_requires` never automates."""
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

    if outcome is Outcome.AUTO:
        outcome = _cap(manifest, recommendation, failures, governance, variants or {}, reasons)
    if outcome is Outcome.ABSTAIN:
        recommendation = None
    return PolicyResult(outcome, recommendation, priority, tuple(reasons), rule_name)


def matches(condition: Condition, answer: Answer | None) -> bool:
    if answer is None or answer.confidence < condition.min_confidence:
        return False
    if condition.min_probability is not None:
        mass = sum(answer.probabilities.get(label, 0.0) for label in condition.labels)
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
