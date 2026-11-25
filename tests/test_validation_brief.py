# SPDX-License-Identifier: Apache-2.0
"""Tests for shadow mode, validation brief, cross-pack queue summary, and override export."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from mendari import notices
from mendari.server.app import create_app

# The triage fixture, and another agent beside it (the clinical review fixture).
INBOX_URL = "/api/agents/mendari-test/triage"
INBOX_ID = "mendari-test/triage"
REFERRAL_URL = "/api/agents/mendari-test/clinical-review"
REFERRAL_ID = "mendari-test/clinical-review"

pytestmark = pytest.mark.core


@pytest.fixture
def client(db_target: str) -> TestClient:
    return TestClient(create_app(db_path=db_target))


def test_shadow_mode_header_and_query_hold_for_comparison(client: TestClient) -> None:
    """`X-Mendari-Shadow: 1` and `?shadow=true` demote automated decisions into shadow review."""
    routine = {"message": {"body": "Please refill my lisinopril 10mg prescription at CVS."}}

    via_header = client.post(
        f"{INBOX_URL}/decide",
        json=routine,
        headers={"X-Mendari-Shadow": "1"},
    )
    assert via_header.status_code == 200
    body_h = via_header.json()
    assert body_h["outcome"] == "HUMAN_REVIEW"
    assert any("shadow mode" in r.lower() for r in body_h["reasons"])

    via_query = client.post(f"{INBOX_URL}/decide?shadow=true", json=routine)
    assert via_query.status_code == 200
    body_q = via_query.json()
    assert body_q["outcome"] == "HUMAN_REVIEW"
    assert any("shadow mode" in r.lower() for r in body_q["reasons"])


def test_validation_brief_json_markdown_gates_shadow_pilot_and_overrides(
    client: TestClient,
) -> None:
    """Validation brief computes readiness gates, ROI, shadow pilot state, and override clusters."""
    hold = client.post(
        "/api/holds",
        json={
            "pack_id": INBOX_ID,
            "reason": "shadow pilot: 7-day validation against clinicians",
            "actor": "cmio-1",
            "duration_hours": 168,
        },
    )
    assert hold.status_code == 201

    stat_dec = client.post(
        f"{INBOX_URL}/decide",
        json={"message": {"body": "Crushing chest pain and shortness of breath"}},
    ).json()
    routine_dec = client.post(
        f"{INBOX_URL}/decide",
        json={"message": {"body": "Can I move my Tuesday clinic appointment to Thursday?"}},
        headers={"X-Mendari-Shadow": "1"},
    ).json()
    assert stat_dec["priority"] == "stat"
    assert routine_dec["outcome"] == "HUMAN_REVIEW"

    # Confirm the stat decision, override the routine decision to a different label.
    stat_label = stat_dec["recommendation"]
    res1 = client.post(
        f"/api/review/{stat_dec['id']}",
        json={"final_label": stat_label, "reviewer": "rn-1"},
    )
    assert res1.status_code == 200 and res1.json()["human_override"] is False

    routine_label = routine_dec["recommendation"]
    override_label = "billing" if routine_label != "billing" else "front_desk"
    res2 = client.post(
        f"/api/review/{routine_dec['id']}",
        json={"final_label": override_label, "reviewer": "rn-1"},
    )
    assert res2.status_code == 200 and res2.json()["human_override"] is True

    # JSON validation brief.
    resp = client.get(f"{INBOX_URL}/validation-brief?days=30&minutes_per_case=4")
    assert resp.status_code == 200
    brief = resp.json()
    assert brief["format"] == "mendari.validation-brief/2"
    assert brief["signed"] is False and "not signed" in brief["about"]  # it says what it is
    assert brief["pack"]["id"] == INBOX_ID
    assert brief["shadow_pilot"]["active"] is True
    assert brief["shadow_pilot"]["hold_id"] == hold.json()["id"]
    assert brief["volume"]["total"] == 2
    assert brief["volume"]["reviewed"] == 2
    assert brief["volume"]["overrides"] == 1
    assert brief["volume"]["shadow_decisions"] >= 1
    assert len(brief["gates"]) == 5
    assert {g["key"] for g in brief["gates"]} == {
        "volume",
        "safety",
        "agreement",
        "governance",
        "drift",
    }
    assert brief["roi"]["minutes_per_case"] == 4.0
    assert len(brief["override_clusters"]) == 1
    cluster = brief["override_clusters"][0]
    assert cluster["recommended"] == routine_label
    assert cluster["chosen"] == override_label
    assert cluster["count"] == 1

    # Markdown committee packet export.
    md_resp = client.get(f"{INBOX_URL}/validation-brief?days=30&format=markdown")
    assert md_resp.status_code == 200
    assert md_resp.headers["content-type"].startswith("text/markdown")
    md = md_resp.text
    assert notices.NOTICE in md
    assert brief["about"] in md and "ALL GATES PASSED" not in md
    assert "Clinical AI Validation & Readiness Brief" in md
    assert "Readiness Gates" in md
    assert f"`{routine_label}`" in md
    assert f"`{override_label}`" in md


def test_review_summary_reports_cross_pack_pending_and_stat_counts(client: TestClient) -> None:
    """`/api/review/summary` aggregates pending and stat counts across packs without PHI."""
    client.post(
        f"{INBOX_URL}/decide",
        json={"message": {"body": "Severe chest pain radiating to left arm"}},
    )
    ref_example = client.get(f"{REFERRAL_URL}/examples").json()[0]["input"]
    ref_res = client.post(
        f"{REFERRAL_URL}/decide",
        json=ref_example,
        headers={"X-Mendari-Shadow": "1"},
    )
    assert ref_res.status_code == 200

    resp = client.get("/api/review/summary")
    assert resp.status_code == 200
    by_pack = {row["pack_id"]: row for row in resp.json()}
    assert by_pack[INBOX_ID]["pending"] >= 1
    assert by_pack[INBOX_ID]["stat"] >= 1
    assert by_pack[REFERRAL_ID]["pending"] >= 1


def test_feedback_export_overrides_source_excludes_confirmed_reviews(client: TestClient) -> None:
    """`source=overrides` exports only cases where clinicians disagreed with the recommendation."""
    d1 = client.post(
        f"{INBOX_URL}/decide",
        json={"message": {"body": "Crushing chest pain right now"}},
    ).json()
    d2 = client.post(
        f"{INBOX_URL}/decide",
        json={"message": {"body": "Question about the charges on my bill from last visit"}},
        headers={"X-Mendari-Shadow": "1"},
    ).json()

    # Confirm d1, override d2.
    r1 = client.post(
        f"/api/review/{d1['id']}",
        json={"final_label": d1["recommendation"], "reviewer": "rn-1"},
    )
    assert r1.status_code == 200
    other = "billing" if d2["recommendation"] != "billing" else "front_desk"
    r2 = client.post(
        f"/api/review/{d2['id']}",
        json={"final_label": other, "reviewer": "rn-1"},
    )
    assert r2.status_code == 200 and r2.json()["human_override"] is True

    all_reviews = client.get(f"{INBOX_URL}/feedback/export?source=reviews")
    assert all_reviews.status_code == 200
    all_rows = [json.loads(line) for line in all_reviews.text.splitlines() if line.strip()]
    assert len(all_rows) == 2

    only_overrides = client.get(f"{INBOX_URL}/feedback/export?source=overrides")
    assert only_overrides.status_code == 200
    override_rows = [json.loads(line) for line in only_overrides.text.splitlines() if line.strip()]
    assert len(override_rows) == 1
    assert override_rows[0]["review"]["label"] == other
    assert override_rows[0]["review"]["overridden"] is True
