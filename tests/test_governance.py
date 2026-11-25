# SPDX-License-Identifier: Apache-2.0
"""Governance approvals: a deploying organization lets a level 2-3 pack automate (ADR-0002)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari.core.audit import AuditStore
from mendari.core.registry import resolve_pack
from mendari.errors import ReviewError
from mendari.evals.pack_tests import load_cases
from mendari.server.app import create_app

CLINICAL = "mendari-test/clinical-review"  # risk level 2: the clinical review fixture
TRIAGE = "mendari-test/triage"  # risk level 1
# A mild symptom of two days: care at home, confidently, below the severity rule. At risk level 2
# a person confirms it unless the organization records a governance approval.
CASE = next(
    c
    for c in load_cases(resolve_pack(CLINICAL))
    if c["name"] == "mild pain of two days is care at home, which a person confirms at risk level 2"
)


@pytest.fixture
def app(tmp_path: Path) -> TestClient:
    return TestClient(create_app(db_path=tmp_path / "a.db"))


def decide(app: TestClient) -> dict[str, Any]:
    response = app.post(f"/api/agents/{CLINICAL}/decide", json=CASE["input"])
    assert response.status_code == 200, response.text
    decision: dict[str, Any] = response.json()
    return decision


def test_an_approval_lets_a_level_2_pack_automate_and_is_written_into_each_decision(
    app: TestClient,
) -> None:
    before = decide(app)
    assert (
        before["outcome"] == "HUMAN_REVIEW"
        and "risk level 2 requires human review" in before["reasons"]
    )
    approved = app.post(
        "/api/governance",
        json={
            "pack_id": CLINICAL,
            "reference": "AI committee minutes 2026-09-30, item 4",
            "days": 30,
            "approver": "dr-chair",
        },
    )
    assert approved.status_code == 201, approved.text
    after = decide(app)
    assert after["outcome"] == "AUTO"
    assert after["governance"] == {
        "approved_by": "dr-chair",
        "reference": "AI committee minutes 2026-09-30, item 4",
        "allow_auto": True,
    }
    state = app.get("/api/governance", params={"pack_id": CLINICAL}).json()
    assert state["approvable"] is True and state["active"]["id"] == approved.json()["id"]


def test_revoking_an_approval_stops_automation_at_once(app: TestClient) -> None:
    approval = app.post(
        "/api/governance",
        json={"pack_id": CLINICAL, "reference": "minutes 12", "approver": "dr-chair"},
    ).json()
    assert decide(app)["outcome"] == "AUTO"
    assert app.delete(f"/api/governance/{approval['id']}").status_code == 204
    assert decide(app)["outcome"] == "HUMAN_REVIEW"
    assert app.delete(f"/api/governance/{approval['id']}").status_code == 404


def test_levels_that_need_no_approval_or_can_never_have_one_are_refused(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "a.db")
    with pytest.raises(ReviewError, match="needs no approval"):
        store.approve_automation(
            pack_id=TRIAGE,
            pack_digest="d",
            risk_level=1,
            approver="a",
            reference="r",
            expires_at="2999",
        )
    with pytest.raises(ReviewError, match="is never automated"):
        store.approve_automation(
            pack_id="x/y",
            pack_digest="d",
            risk_level=4,
            approver="a",
            reference="r",
            expires_at="2999",
        )


def test_an_approval_is_for_exactly_one_pack_and_expires(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "a.db")
    store.approve_automation(
        pack_id=CLINICAL,
        pack_digest="sha256:old",
        risk_level=2,
        approver="a",
        reference="r",
        expires_at="2999-01-01T00:00:00+00:00",
    )
    # A changed agent needs a new approval.
    assert store.active_approval(CLINICAL, "sha256:new") is None
    assert store.active_approval(CLINICAL, "sha256:old") is not None
    store.approve_automation(
        pack_id=CLINICAL,
        pack_digest="sha256:e",
        risk_level=2,
        approver="a",
        reference="r",
        expires_at="2000-01-01T00:00:00+00:00",
    )
    assert store.active_approval(CLINICAL, "sha256:e") is None  # expired


def test_approvals_and_revocations_are_in_the_hash_chain(app: TestClient, tmp_path: Path) -> None:
    approval = app.post(
        "/api/governance",
        json={"pack_id": CLINICAL, "reference": "minutes 12", "approver": "dr-chair"},
    ).json()
    app.delete(f"/api/governance/{approval['id']}")
    store = AuditStore(tmp_path / "a.db")
    with store._db.session(exclusive=False) as db:
        events = [
            r["event"]
            for r in db.all(
                "SELECT event FROM audit_events WHERE decision_id = ?",
                (f"governance:{approval['id']}",),
            )
        ]
    assert events == ["governance.approved", "governance.revoked"]
    assert store.verify()["ok"] is True
    assert (
        app.post(
            "/api/governance", json={"pack_id": TRIAGE, "reference": "minutes", "approver": "x"}
        ).status_code
        == 409
    )


# -- the deployment registry -------------------------------------------------------------------


def test_a_deployment_is_recorded_when_it_changes_not_at_every_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_ENVIRONMENT", "pilot")
    monkeypatch.setenv("MENDARI_RELEASE", "r1")
    db = tmp_path / "a.db"
    first = TestClient(create_app(db_path=db)).get("/api/deployments", params={"pack_id": TRIAGE})
    [row] = first.json()
    pack = resolve_pack(TRIAGE)
    assert row["environment"] == "pilot" and row["release"] == "r1"
    assert (row["pack_id"], row["pack_version"], row["pack_digest"]) == (
        TRIAGE,
        pack.version,
        pack.digest,
    )
    assert row["providers"] == {"router": "rules", "screen": "rules"}

    again = TestClient(create_app(db_path=db)).get("/api/deployments", params={"pack_id": TRIAGE})
    assert [r["id"] for r in again.json()] == [row["id"]]  # a restart changes nothing

    monkeypatch.setenv("MENDARI_RELEASE", "r2")
    released = TestClient(create_app(db_path=db))
    rows = released.get("/api/deployments", params={"pack_id": TRIAGE}).json()
    assert [r["release"] for r in rows] == ["r2", "r1"]

    store = AuditStore(db)
    with store._db.session(exclusive=False) as session:
        chained = session.all(
            "SELECT event FROM audit_events WHERE decision_id LIKE 'deployment:%'"
        )
    assert {r["event"] for r in chained} == {"deployment.recorded"}
    assert store.verify()["ok"] is True


def test_another_environment_is_its_own_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "a.db"
    for environment in ("staging", "production"):
        monkeypatch.setenv("MENDARI_ENVIRONMENT", environment)
        TestClient(create_app(db_path=db)).get("/api/deployments", params={"pack_id": TRIAGE})
    rows = AuditStore(db).deployments(TRIAGE)
    assert sorted(r["environment"] for r in rows) == ["production", "staging"]
