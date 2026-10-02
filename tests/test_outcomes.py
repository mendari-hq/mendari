"""Downstream outcomes (`POST /api/audit/{id}/outcome`, `AuditStore.record_outcome`): the source
is a system identifier, the recorder is whoever called, and the label is one of the decision's
question's labels. Everything recorded is chained for good and exported in FHIR, so nothing a
caller chooses freely may end up there as text."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, Pack
from mendari.errors import ProviderError, ReviewError
from mendari.providers.jev import JevProvider
from mendari.server.app import create_app
from mendari.standards import audit_fhir
from tests.conftest import ROOT, message

ROUTINE = "Can you send a refill for metformin to my pharmacy on Main Street?"  # AUTO
EMERGENCY = "Crushing chest pain and my left arm is numb"  # HUMAN_REVIEW
PINNED = ROOT / "tests" / "fixtures" / "fhir" / "audit-outcome.json"
BASE = "https://mendari.example/fhir"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("MENDARI_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("MENDARI_OIDC_CLIENT_ID", raising=False)
    return TestClient(create_app(db_path=tmp_path / "a.db"))


def decided(client: TestClient, pack: Pack, body: Any) -> str:
    return str(client.post(f"/api/packs/{pack.id}/decide", json=body).json()["id"])


@pytest.mark.parametrize(
    "source",
    [
        "staff re-route",  # free text
        "Jane Doe, RN",
        "ehr\n",
        "-ehr",  # starts with a letter or digit
        "",
        "x" * 101,
        "ehr<script>",
        "café",
    ],
)
def test_the_source_is_a_system_identifier(
    client: TestClient, inbox_pack: Pack, store: AuditStore, source: str
) -> None:
    decision = decided(client, inbox_pack, message(ROUTINE))
    body = {"final_label": "nurse", "source": source}
    refused = client.post(f"/api/audit/{decision}/outcome", json=body)
    assert refused.status_code == 422
    assert source not in refused.text or not source
    result = Decision(inbox_pack, store=store)(message(ROUTINE))
    with pytest.raises(ReviewError, match="system identifier"):
        store.record_outcome(result.id, final_label="nurse", source=source, recorded_by="rn-9")


@pytest.mark.parametrize(
    "source", ["ehr", "mendari", "urn:oid:2.16.840.1", "epic/inbox", "a" * 100]
)
def test_identifiers_are_accepted(client: TestClient, inbox_pack: Pack, source: str) -> None:
    decision = decided(client, inbox_pack, message(ROUTINE))
    recorded = client.post(
        f"/api/audit/{decision}/outcome", json={"final_label": "nurse", "source": source}
    )
    assert recorded.status_code == 200
    assert recorded.json() == {"id": decision, "final_label": "nurse", "recorded_by": "open mode"}


def test_the_label_is_one_of_the_questions_labels(client: TestClient, inbox_pack: Pack) -> None:
    decision = decided(client, inbox_pack, message(ROUTINE))
    url = f"/api/audit/{decision}/outcome"
    wrong = client.post(url, json={"final_label": "Patient seen by Dr X", "source": "ehr"})
    assert wrong.status_code == 409 and "Dr X" not in wrong.text
    long = client.post(url, json={"final_label": "n" * 201, "source": "ehr"})
    assert long.status_code == 422
    review = client.post(
        f"/api/review/{decided(client, inbox_pack, message(EMERGENCY))}",
        json={"final_label": "n" * 201, "reviewer": "rn-1"},
    )
    assert review.status_code == 422
    assert client.post(url, json={"final_label": "front_desk", "source": "ehr"}).status_code == 200


def test_a_refused_input_is_checked_against_the_served_packs_labels(
    client: TestClient, inbox_pack: Pack
) -> None:
    """Refused before any provider ran, a decision has no answer to take labels from."""
    decision = decided(client, inbox_pack, {"message": {}})
    record = client.get(f"/api/audit/{decision}").json()
    assert record["outcome"] == "BLOCK" and record["record"]["answers"] == {}
    url = f"/api/audit/{decision}/outcome"
    assert client.post(url, json={"final_label": "pharmacist", "source": "ehr"}).status_code == 409
    assert client.post(url, json={"final_label": "nurse", "source": "ehr"}).status_code == 200


def test_decisions_of_old_or_unserved_packs_still_take_a_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inbox_pack: Pack
) -> None:
    """A decision with no answer takes its labels from the installed pack, also when its record
    carries the id from before the rename (`openhealth/…`) and when this instance serves another
    pack (a per-pack image sharing the database). Else its review would stay pending for good."""

    def unreachable(*_: object) -> None:
        raise ProviderError("timeout")

    monkeypatch.setenv("JEV_API_URL", "https://jev.test/v1/decide")
    monkeypatch.setenv("JEV_MODEL", "jev-test-model")
    monkeypatch.setenv("JEV_API_KEY", "test-key")
    monkeypatch.delenv("JEV_CONNECTOR", raising=False)
    monkeypatch.setattr(JevProvider, "_send", unreachable)
    db = tmp_path / "a.db"
    deciding = TestClient(create_app(db_path=db, providers={"router": "jev"}))
    abstained = decided(deciding, inbox_pack, message(ROUTINE))
    refused = decided(deciding, inbox_pack, {"message": {}})
    record = deciding.get(f"/api/audit/{abstained}").json()
    assert (record["outcome"], record["review_status"]) == ("ABSTAIN", "pending")
    assert inbox_pack.manifest.policy.primary not in record["record"]["answers"]
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute(
            "UPDATE decisions SET pack_id = ? WHERE id IN (?, ?)",
            ("openhealth/patient-inbox-router", abstained, refused),
        )
    monkeypatch.setenv("MENDARI_PACKS", "mendari/ai-guard")
    client = TestClient(create_app(db_path=db))
    for label, status in (("pharmacist", 409), ("nurse", 200)):
        review = {"final_label": label, "reviewer": "rn-1"}
        assert client.post(f"/api/review/{abstained}", json=review).status_code == status
        outcome = {"final_label": label, "source": "ehr"}
        assert client.post(f"/api/audit/{refused}/outcome", json=outcome).status_code == status


def test_without_any_labels_nothing_is_recorded(store: AuditStore, inbox_pack: Pack) -> None:
    """Fail closed: a refused input's decision, and no pack to ask."""
    result = Decision(inbox_pack, store=store)({"message": {}})
    with pytest.raises(ReviewError, match="labels are unknown"):
        store.record_outcome(result.id, final_label="nurse", source="ehr", recorded_by="rn-9")
    assert [e["event"] for e in store.events()] == ["decision.created"]


def test_the_recorder_is_required(store: AuditStore, inbox_pack: Pack) -> None:
    result = Decision(inbox_pack, store=store)(message(ROUTINE))
    with pytest.raises(ReviewError, match="who records"):
        store.record_outcome(result.id, final_label="nurse", source="ehr", recorded_by=" ")


def test_open_mode_records_the_outcome_as_open_mode(client: TestClient, inbox_pack: Pack) -> None:
    decision = decided(client, inbox_pack, message(ROUTINE))
    body = {"final_label": "nurse", "source": "ehr", "recorded_by": "Dr Somebody Else"}
    assert client.post(f"/api/audit/{decision}/outcome", json=body).status_code == 200
    [event] = [e for e in client.get(f"/api/audit/{decision}").json()["events"][1:]]
    assert event["payload"] == {"final_label": "nurse", "source": "ehr", "recorded_by": "open mode"}


# -- the FHIR renderings (pinned: any change is a changelog item) --------------------------------


def outcome_event(payload: dict[str, Any]) -> dict[str, Any]:
    """A fixed `outcome.recorded` event on a fixed decision, joined as `AuditStore.events` does."""
    return {
        "seq": 12,
        "decision_id": "2b3f6c1e-8d4a-4f0b-9c71-5e2d8a6b4c90",
        "event": "outcome.recorded",
        "payload": payload,
        "created_at": "2026-10-01T15:00:00.000+00:00",
        "prev_hash": f"{11:064x}",
        "hash": f"{12:064x}",
        "pack_id": "mendari/patient-inbox-router",
        "pack_version": "0.3.0",
        "record": {},
    }


def rendered(payload: dict[str, Any]) -> dict[str, Any]:
    event = outcome_event(payload)
    return {
        "AuditEvent": audit_fhir.audit_event(event, base=BASE),
        "Provenance": audit_fhir.provenance(event, base=BASE, version=3),
    }


def test_an_outcomes_rendering_is_pinned() -> None:
    """Since Phase 4 the agent is whoever recorded the outcome; the reporting system is the
    AuditEvent's `source.site`. An event recorded before has no recorder ("not recorded"), and
    its source, free text then, is shown only when it is an identifier."""
    pinned = json.loads(PINNED.read_text(encoding="utf-8"))
    assert {
        "recorded": rendered(
            {"final_label": "front_desk", "source": "ehr", "recorded_by": "key ehr integration"}
        ),
        "before-phase-4": rendered(
            {"final_label": "front_desk", "source": "Jane Doe re-routed it"}
        ),
        "before-phase-4-identifier": rendered({"final_label": "front_desk", "source": "ehr"}),
    } == pinned


def test_the_recorder_is_the_agent_and_the_source_is_not(
    store: AuditStore, inbox_pack: Pack
) -> None:
    result = Decision(inbox_pack, store=store)(message(ROUTINE))
    store.record_outcome(result.id, final_label="nurse", source="ehr", recorded_by="rn-9")
    resources = list(audit_fhir.resources(store.iter_events()))
    event = next(r for r in resources if r["resourceType"] == "AuditEvent" and r["action"] == "U")
    assert event["agent"] == [{"who": {"display": "rn-9"}, "requestor": True}]
    assert event["source"]["site"] == "ehr"
    provenance = [r for r in resources if r["resourceType"] == "Provenance"][-1]
    assert provenance["agent"][0]["who"] == {"display": "rn-9"}
    assert "ehr" not in json.dumps(provenance["agent"])
