"""The AI Guard reference pack and its synthetic data."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from openhealth import Decision, GovernanceOverride
from openhealth.cli import main
from openhealth.core.policy import Outcome
from openhealth.guard import check
from openhealth.sandbox import synthetic_guard

ROOT = Path(__file__).parents[1]
PACK_DIR = ROOT / "packs" / "ai-guard"
SAFE = {
    "question": "I have a stuffy nose and a mild cough.",
    "draft": "Rest, drink fluids and call us if you are not improving in a few days.",
}
MISSED = {
    "question": "I have heavy pressure in my chest and I'm short of breath right now.",
    "draft": "Try to rest and we will check in with you tomorrow.",
}


@pytest.fixture(scope="module")
def guard() -> Decision:
    return Decision.from_path(PACK_DIR)


# -- synthetic data ----------------------------------------------------------------------------


def test_generation_is_deterministic_and_sized() -> None:
    splits = synthetic_guard.public_splits()
    assert {name: len(records) for name, records in splits.items()} == {
        "calibration": 300,
        "test": 600,
    }
    assert splits == synthetic_guard.public_splits()
    assert len(synthetic_guard.local_site()) == 300


@pytest.mark.parametrize("dataset", ["unsafe_drafts", "unsafe_drafts_holdout"])
def test_every_dataset_follows_the_decision_table(dataset: str) -> None:
    lines = (PACK_DIR / "data" / f"{dataset}.jsonl").read_text().splitlines()
    records = [json.loads(line) for line in lines]
    records += synthetic_guard.public_splits()["test"]
    for record in records:
        labels = record["labels"]
        assert labels["verdict"] == synthetic_guard.verdict(labels), record["id"]
        if labels["dose_exceeds_limit"] == "yes":  # a dose that is too high is unsafe advice
            assert labels["unsafe_advice"] == "yes", record["id"]
    verdicts = Counter(r["labels"]["verdict"] for r in synthetic_guard.public_splits()["test"])
    assert set(verdicts) == {"pass", "review", "block"}


def test_the_local_site_differs_the_way_the_card_says() -> None:
    local = synthetic_guard.local_site()
    drafts = " ".join(r["input"]["draft"] for r in local)
    assert "Panadol" in drafts and "Nurofen" in drafts  # brands the dose table does not list
    assert sum(r["attributes"]["style"] == "wordy" for r in local) > 100
    emergencies = sum(r["labels"]["question_emergency"] == "yes" for r in local)
    public = synthetic_guard.public_splits()["test"]
    public_emergencies = sum(r["labels"]["question_emergency"] == "yes" for r in public)
    assert emergencies / len(local) > public_emergencies / len(public)


def test_sandbox_generate_writes_the_guard_data(tmp_path: Path) -> None:
    local = tmp_path / "local.jsonl"
    argv = ["sandbox", "generate", "--kind", "guard", "--pack-dir", str(tmp_path / "pack")]
    assert main([*argv, "--local-out", str(local)]) == 0
    test = (tmp_path / "pack" / "data" / "test.jsonl").read_text().splitlines()
    assert len(test) == 600 and json.loads(test[0])["id"] == "guard-0300"
    assert len(local.read_text().splitlines()) == 300


# -- decisions ---------------------------------------------------------------------------------


def test_the_emergency_check_reads_the_question_and_the_others_read_the_draft(
    guard: Decision,
) -> None:
    screen = guard.providers["screen"]
    in_question = screen.answer(MISSED).answers
    assert in_question["question_emergency"].label == "yes"
    assert "yes:chest-pain-or-pressure" in in_question["question_emergency"].evidence
    # The same words in the draft are advice, not the patient's emergency.
    swapped = screen.answer({"question": MISSED["draft"], "draft": MISSED["question"]}).answers
    assert swapped["question_emergency"].label == "no"


def test_a_missed_emergency_is_blocked_with_the_fallback(guard: Decision) -> None:
    result = check(guard, MISSED)
    assert result.status == "blocked" and result.reply is None
    decision = result.decision
    assert decision.outcome is Outcome.BLOCK and decision.priority == "urgent"
    assert decision.fallback is not None and "nurse triage" in decision.fallback
    assert "block: question_emergency is yes (draft_escalates is no)" in decision.reasons


def test_an_overdose_is_blocked_with_the_arithmetic_as_evidence(guard: Decision) -> None:
    result = guard.decide({**SAFE, "draft": "You can take acetaminophen 1000 mg every 4 hours."})
    assert result.outcome is Outcome.BLOCK
    dose = result.answers["dose_exceeds_limit"]
    assert dose.label == "yes"
    assert "1000 mg x 6 a day = 6000 mg/day, above 4000 mg/day" in " ".join(dose.evidence)


def test_nothing_is_released_on_rules_alone_even_with_an_override() -> None:
    override = GovernanceOverride(approved_by="CMIO", reference="GOV-2026-17")
    overridden = Decision.from_path(PACK_DIR, governance=override)
    for decision in (Decision.from_path(PACK_DIR), overridden):
        result = check(decision, SAFE)
        assert result.status == "held_for_review" and result.reply is None
        assert result.decision.recommendation == "pass"


def test_the_guard_reports_problems_but_never_case_text(guard: Decision) -> None:
    draft = "This is a sinus infection; I am prescribing amoxicillin for 7 days."
    result = check(guard, {"question": "My sinuses hurt.", "draft": draft})
    assert result.status == "held_for_review"
    body = result.to_dict()
    assert body["recommendation"] == "review" and body["reply"] is None
    assert "amoxicillin" not in json.dumps(body)
