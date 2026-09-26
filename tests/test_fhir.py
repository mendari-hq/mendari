from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from openhealth import Decision, Pack
from openhealth.errors import FhirError
from openhealth.standards import cds_hooks
from openhealth.standards.fhir import FhirAdapter, age_band, select

COMMUNICATION: dict[str, Any] = {
    "resourceType": "Communication",
    "id": "msg-1",
    "status": "in-progress",
    "subject": {"reference": "Patient/pat-1"},
    "topic": {"text": "Help"},
    "medium": [{"coding": [{"system": "x", "code": "sms"}]}],
    "sent": "2026-09-25T10:00:00Z",
    "payload": [
        {"contentString": "My throat feels like it's closing"},
        {"contentString": "after the new medicine."},
    ],
}
PATIENT: dict[str, Any] = {
    "resourceType": "Patient",
    "id": "pat-1",
    "birthDate": "1950-03-02",
    "communication": [{"language": {"coding": [{"code": "es"}]}}],
}
BUNDLE: dict[str, Any] = {
    "resourceType": "Bundle",
    "type": "collection",
    "entry": [{"resource": COMMUNICATION}, {"resource": PATIENT}],
}


def test_select_follows_fhirpath_list_semantics() -> None:
    indexed = {"Communication": COMMUNICATION}
    assert select(indexed, "Communication.payload.contentString") == [
        "My throat feels like it's closing",
        "after the new medicine.",
    ]
    assert select(indexed, "Communication.payload[1].contentString") == ["after the new medicine."]
    assert select(indexed, "Communication.medium[0].coding[0].code") == ["sms"]
    assert select(indexed, "Patient.birthDate") == []


@pytest.mark.parametrize(
    ("born", "band"),
    [
        ("2015-01-01", "0-17"),
        ("1990-06", "18-39"),
        ("1970", "40-64"),
        ("1950-03-02", "65+"),
        ("2030-01-01", None),
        ("garbage", None),
    ],
)
def test_age_band(born: str, band: str | None) -> None:
    assert age_band(born, today=date(2026, 9, 25)) == band


def test_bundle_maps_onto_pack_input(inbox_pack: Pack) -> None:
    adapter = FhirAdapter.for_pack(inbox_pack)
    inputs = adapter.to_input(adapter.index(BUNDLE))
    assert inputs["message"] == {
        "subject": "Help",
        "body": "My throat feels like it's closing\nafter the new medicine.",
        "channel": "sms",
        "sent_at": "2026-09-25T10:00:00Z",
    }
    assert inputs["patient"] == {"age_band": "65+", "preferred_language": "es"}


def test_decision_becomes_a_fhir_task(router: Decision) -> None:
    result, task = router.decide_fhir(BUNDLE)
    assert result.recommendation == "nurse" and result.priority.value == "stat"
    assert task["resourceType"] == "Task"
    assert task["status"] == "requested"  # HUMAN_REVIEW: a person must accept it
    assert task["priority"] == "stat"
    assert task["focus"] == {"reference": "Communication/msg-1"}
    assert task["for"] == {"reference": "Patient/pat-1"}
    assert task["performerType"][0]["coding"][0]["code"] == "nurse"
    assert task["identifier"][0]["value"] == result.id


def test_missing_required_resource(inbox_pack: Pack) -> None:
    adapter = FhirAdapter.for_pack(inbox_pack)
    with pytest.raises(FhirError, match="Communication"):
        adapter.index({"resourceType": "Bundle", "entry": [{"resource": PATIENT}]})


def test_cds_discovery_and_card(router: Decision) -> None:
    services = cds_hooks.discovery([router.pack])["services"]
    assert services[0]["id"] == "openhealth.patient-inbox-router"
    assert services[0]["hook"] == "openhealth-message-received"

    response = cds_hooks.invoke(
        router,
        {
            "hook": "openhealth-message-received",
            "hookInstance": "d1577c69-dfbe-44ad-ba6d-3e05e953b2ea",
            "context": {"communicationId": "msg-1", "patientId": "pat-1"},
            "prefetch": {"communication": COMMUNICATION, "patient": PATIENT},
        },
    )
    card = response["cards"][0]
    assert card["indicator"] == "critical"
    assert len(card["summary"]) <= 140
    assert card["suggestions"][0]["actions"][0]["resource"]["resourceType"] == "Task"


def test_cds_requires_prefetch(router: Decision) -> None:
    with pytest.raises(FhirError, match="prefetch required"):
        cds_hooks.invoke(router, {"hook": "openhealth-message-received", "prefetch": {}})
