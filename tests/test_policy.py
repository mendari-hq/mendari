from __future__ import annotations

import pytest
from pydantic import ValidationError

from mendari import GovernanceOverride, Outcome, Pack, Priority, RiskLevel
from mendari.core.policy import apply_policy
from mendari.core.spec import PackManifest, Risk
from mendari.providers import Answer, ProviderInfo

INFO = ProviderInfo(slot="test", variant="test", type="test")
# The variants running: the inbox's policy automates only while its urgency screen includes a
# model (automation_requires).
SCREENED = {"safety_screen": "rules_and_jev", "router": "naive_bayes"}


def answer(question: str, label: str, confidence: float) -> Answer:
    return Answer(question, label, confidence, {label: confidence}, INFO)


def at_level(manifest: PackManifest, level: RiskLevel) -> PackManifest:
    return manifest.model_copy(
        update={"risk": Risk(level=level, category="test", rationale="test")}
    )


def routine(label: str, confidence: float) -> dict[str, Answer]:
    return {
        "destination": answer("destination", label, confidence),
        "urgency": answer("urgency", "routine", 0.95),
    }


@pytest.mark.parametrize(
    ("label", "confidence", "outcome"),
    [
        ("nurse", 0.95, Outcome.AUTO),
        ("nurse", 0.90, Outcome.AUTO),
        ("nurse", 0.89, Outcome.HUMAN_REVIEW),
        ("nurse", 0.50, Outcome.HUMAN_REVIEW),
        ("nurse", 0.49, Outcome.ABSTAIN),
        # Non-clinical destinations need more confidence (label-specific threshold 0.95).
        ("billing", 0.93, Outcome.HUMAN_REVIEW),
        ("billing", 0.96, Outcome.AUTO),
    ],
)
def test_thresholds(inbox_pack: Pack, label: str, confidence: float, outcome: Outcome) -> None:
    result = apply_policy(inbox_pack.manifest, routine(label, confidence), {}, None, SCREENED)
    assert result.outcome is outcome
    assert result.recommendation == (None if outcome is Outcome.ABSTAIN else label)


def test_emergency_rule_wins_over_confident_routing(inbox_pack: Pack) -> None:
    answers = {
        "destination": answer("destination", "billing", 0.99),
        "urgency": answer("urgency", "urgent", 0.40),
    }
    result = apply_policy(inbox_pack.manifest, answers, {})
    assert result.outcome is Outcome.HUMAN_REVIEW
    assert result.recommendation == "nurse"
    assert result.priority is Priority.STAT
    assert result.rule == "possible-emergency"


def test_emergency_rule_respects_its_confidence_floor(inbox_pack: Pack) -> None:
    answers = {
        "destination": answer("destination", "billing", 0.99),
        "urgency": answer("urgency", "urgent", 0.30),
    }
    assert apply_policy(inbox_pack.manifest, answers, {}).rule is None


def test_provider_failure_never_automates(inbox_pack: Pack) -> None:
    answers = {"destination": answer("destination", "nurse", 0.99)}
    result = apply_policy(inbox_pack.manifest, answers, {"urgency": "timeout"})
    assert result.outcome is Outcome.HUMAN_REVIEW
    assert any("timeout" in reason for reason in result.reasons)


def test_missing_primary_answer_abstains(inbox_pack: Pack) -> None:
    answers = {"urgency": answer("urgency", "routine", 0.9)}
    result = apply_policy(inbox_pack.manifest, answers, {"destination": "rate_limited"})
    assert result.outcome is Outcome.ABSTAIN
    assert result.recommendation is None
    assert "rate_limited" in result.reasons[0]


@pytest.mark.parametrize("level", [RiskLevel.CLINICAL_SUPPORT, RiskLevel.PATIENT_PREDICTION])
def test_risk_levels_2_and_3_require_review_unless_governance_allows(
    inbox_pack: Pack, level: RiskLevel
) -> None:
    manifest = at_level(inbox_pack.manifest, level)
    unapproved = apply_policy(manifest, routine("nurse", 0.99), {}, None, SCREENED)
    assert unapproved.outcome is Outcome.HUMAN_REVIEW
    governance = GovernanceOverride(approved_by="CMIO", reference="AI-committee-2026-09")
    result = apply_policy(manifest, routine("nurse", 0.99), {}, governance, SCREENED)
    assert result.outcome is Outcome.AUTO
    assert any("governance override" in reason for reason in result.reasons)


def test_risk_level_4_is_never_autonomous(inbox_pack: Pack) -> None:
    manifest = at_level(inbox_pack.manifest, RiskLevel.TIME_CRITICAL)
    governance = GovernanceOverride(approved_by="CMIO", reference="AI-committee-2026-09")
    result = apply_policy(manifest, routine("nurse", 0.99), {}, governance, SCREENED)
    assert result.outcome is Outcome.HUMAN_REVIEW


def test_governance_override_needs_accountability() -> None:
    with pytest.raises(ValueError, match="approver"):
        GovernanceOverride(approved_by=" ", reference="x")


def test_automation_can_require_provider_variants(inbox_pack: Pack) -> None:
    raw = inbox_pack.manifest.model_dump(by_alias=True)
    raw["policy"]["automation_requires"] = {"safety_screen": ["rules_and_jev", "rules_and_llm"]}
    manifest = PackManifest.model_validate(raw)
    answers = routine("nurse", 0.99)
    guarded = apply_policy(manifest, answers, {}, None, {"safety_screen": "rules", "router": "jev"})
    assert guarded.outcome is Outcome.HUMAN_REVIEW
    assert (
        "automation requires safety_screen = rules_and_jev or rules_and_llm (running: rules)"
        in (guarded.reasons)
    )
    allowed = {"safety_screen": "rules_and_jev", "router": "jev"}
    assert apply_policy(manifest, answers, {}, None, allowed).outcome is Outcome.AUTO
    assert apply_policy(manifest, answers, {}).outcome is Outcome.HUMAN_REVIEW  # not told: no AUTO

    raw["policy"]["automation_requires"] = {"safety_screen": ["nope"], "missing": ["x"]}
    with pytest.raises(ValidationError, match="unknown slot 'missing'") as error:
        PackManifest.model_validate(raw)
    assert "unknown: ['nope']" in str(error.value)
