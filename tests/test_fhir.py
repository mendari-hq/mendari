# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from mendari import Decision, Pack
from mendari.errors import FhirError
from mendari.standards import cds_hooks
from mendari.standards.fhir import FhirAdapter, age_band, select

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


def test_the_task_says_when_but_leaves_its_version_to_the_server(router: Decision) -> None:
    """`meta.versionId` belongs to whichever server stores the Task; no profile is claimed (none
    exists); the narrative has codes, ids and times, never the message or the reasons."""
    result, task = router.decide_fhir(BUNDLE)
    assert task["meta"]["lastUpdated"] == result.created_at
    assert "versionId" not in task["meta"] and "profile" not in task["meta"]
    assert task["meta"]["tag"][0]["code"] == f"{result.pack_id}@{result.pack_version}"
    div = task["text"]["div"]
    assert task["text"]["status"] == "generated"
    for shown in (result.id, "HUMAN_REVIEW", "stat", "nurse", result.created_at):
        assert shown in div
    assert result.reasons and not any(reason in div for reason in result.reasons)
    for private in ("throat", "medicine", "Help", task["description"]):
        assert private not in div


def test_performer_codes_are_what_the_template_can_fill(router: Decision) -> None:
    adapter = FhirAdapter.for_pack(router.pack)
    labels = router.pack.manifest.labels("destination")
    assert adapter.performer_template == "{recommendation}"
    assert list(adapter.performer_codes()) == [*labels, "manual-triage"]
    assert adapter.performer_codes()["nurse"] == {"recommendation": "nurse"}


def test_missing_required_resource(inbox_pack: Pack) -> None:
    adapter = FhirAdapter.for_pack(inbox_pack)
    with pytest.raises(FhirError, match="Communication"):
        adapter.index({"resourceType": "Bundle", "entry": [{"resource": PATIENT}]})


def test_cds_discovery_and_card(router: Decision) -> None:
    services = cds_hooks.discovery([router.pack])["services"]
    assert services[0]["id"] == "mendari.patient-inbox-router"
    assert services[0]["hook"] == "mendari-message-received"

    response = cds_hooks.invoke(
        router,
        {
            "hook": "mendari-message-received",
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
        cds_hooks.invoke(router, {"hook": "mendari-message-received", "prefetch": {}})
