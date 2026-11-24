# SPDX-License-Identifier: Apache-2.0
"""AI Guard: release, hold or block an LLM's draft reply."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from mendari import Decision, GovernanceOverride
from mendari.core.spec import PackManifest
from mendari.errors import MendariError
from mendari.guard import check
from mendari.server.app import create_app
from tests.conftest import INBOX_DIR

MINI = Path(__file__).parent / "fixtures" / "mini-agents" / "guard-mini"
SAFE = {"question": "Can I take something for a headache?",
        "draft": "You can take 500 mg of acetaminophen every 6 hours."}  # fmt: skip
OVERDOSE = {**SAFE, "draft": "Take 1000 mg of acetaminophen every 4 hours."}
MISSED = {
    "question": "I have crushing chest pain right now.",
    "draft": "Try to rest and drink water.",
}


def test_blocked_drafts_are_never_sent() -> None:
    result = check(Decision.from_path(MINI), OVERDOSE)
    assert result.status == "blocked" and result.reply is None
    assert result.decision.fallback == "Do not send the draft; route the message to nurse triage."
    missed = check(Decision.from_path(MINI), MISSED)
    assert missed.status == "blocked"
    assert "block: question_emergency is yes (draft_escalates is no)" in missed.decision.reasons


def test_at_risk_level_2_a_passing_draft_waits_for_a_clinician() -> None:
    result = check(Decision.from_path(MINI), SAFE)
    assert result.status == "held_for_review" and result.reply is None
    assert result.decision.recommendation == "pass"


def test_a_governance_override_releases_passing_drafts() -> None:
    override = GovernanceOverride(approved_by="CMIO", reference="GOV-2026-17")
    decision = Decision.from_path(MINI, governance=override)
    result = check(decision, SAFE)
    assert result.status == "released" and result.reply == SAFE["draft"]
    assert check(decision, OVERDOSE).status == "blocked"  # the override never releases a block
    assert result.to_dict()["decision_id"] == result.decision.id


def test_invalid_input_is_blocked_and_packs_without_a_guard_refuse() -> None:
    assert check(Decision.from_path(MINI), {"question": "hi"}).status == "blocked"  # no draft
    with pytest.raises(MendariError, match="no `guard` section"):
        check(Decision.from_path(INBOX_DIR), {"message": {"body": "hi"}})


def test_guard_section_is_validated() -> None:
    raw = yaml.safe_load((MINI / "agent.yaml").read_text())
    raw["guard"]["release"] = ["pass", "maybe"]
    with pytest.raises(
        ValidationError, match=r"guard.release names unknown recommendations \['maybe'\]"
    ):
        PackManifest.model_validate(raw)
    raw["guard"]["release"] = ["pass"]
    raw["spec_version"] = "0.1"
    with pytest.raises(ValidationError, match=r"use spec 0\.2 features"):
        PackManifest.model_validate(raw)


def test_the_guard_endpoint_is_audited(db_target: str) -> None:
    app = create_app(db_path=db_target, packs_paths=[MINI.parent])
    client = TestClient(app)
    url = "/api/agents/mendari-test/guard-mini/guard"
    blocked = client.post(url, json=OVERDOSE).json()
    assert blocked["status"] == "blocked" and blocked["reply"] is None
    assert blocked["fallback"].startswith("Do not send the draft")
    held = client.post(url, json=SAFE).json()
    assert (held["status"], held["recommendation"]) == ("held_for_review", "pass")
    # The whole decision too, as …/decide answers it (the dashboard shows it in words).
    assert held["decision"]["id"] == held["decision_id"] and held["decision"]["answers"]
    assert held["decision"]["outcome"] == held["outcome"]
    audited = {r["id"] for r in client.get("/api/audit?limit=10").json()}
    assert {blocked["decision_id"], held["decision_id"]} <= audited
    no_guard = client.post("/api/agents/mendari-test/trial-mini/guard", json={})
    assert no_guard.status_code == 400
