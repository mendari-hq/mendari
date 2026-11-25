# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from mendari import Agent, Decision
from mendari.errors import FhirError
from mendari.standards import cds_hooks
from mendari.standards.fhir import FhirAdapter, age_band, select

# A synthetic message the triage fixture (`mendari-test/triage`) reads as a possible same-day
# emergency, as a Communication from its sender, a Patient the fixture does not read.
COMMUNICATION: dict[str, Any] = {
    "resourceType": "Communication",
    "id": "msg-1",
    "status": "in-progress",
    "subject": {"reference": "Patient/pat-1"},
    "topic": {"text": "Help"},
    "medium": [{"coding": [{"system": "x", "code": "sms"}]}],
    "sent": "2026-09-25T10:00:00Z",
    "payload": [
        {"contentString": "I have had chest pain since last night"},
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
        "I have had chest pain since last night",
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


def test_bundle_maps_onto_agent_input(triage_agent: Agent) -> None:
    """The message's topic, its text joined a part a line, and how it came, as the fixture's
    channel code; its sender is not read."""
    adapter = FhirAdapter.for_agent(triage_agent)
    inputs = adapter.to_input(adapter.index(BUNDLE))
    assert inputs == {
        "message": {
            "subject": "Help",
            "body": "I have had chest pain since last night\nafter the new medicine.",
            "channel": "sms",
        }
    }


def test_decision_becomes_a_fhir_task(triage: Decision) -> None:
    result, task = triage.decide_fhir(BUNDLE)
    assert result.recommendation == "nurse" and result.priority.value == "stat"
    assert task["resourceType"] == "Task"
    assert task["status"] == "requested"  # HUMAN_REVIEW: a person must accept it
    assert task["priority"] == "stat"
    assert task["focus"] == {"reference": "Communication/msg-1"}
    assert task["for"] == {"reference": "Patient/pat-1"}
    assert task["performerType"][0]["coding"][0]["code"] == "nurse"
    assert task["identifier"][0]["value"] == result.id


def test_the_task_says_when_but_leaves_its_version_to_the_server(triage: Decision) -> None:
    """`meta.versionId` belongs to whichever server stores the Task; no profile is claimed (none
    exists); the narrative has codes, ids and times, never the message or the reasons."""
    result, task = triage.decide_fhir(BUNDLE)
    assert task["meta"]["lastUpdated"] == result.created_at
    assert "versionId" not in task["meta"] and "profile" not in task["meta"]
    assert task["meta"]["tag"][0]["code"] == f"{result.agent_id}@{result.agent_version}"
    div = task["text"]["div"]
    assert task["text"]["status"] == "generated"
    for shown in (result.id, "HUMAN_REVIEW", "stat", "nurse", result.created_at):
        assert shown in div
    assert result.reasons and not any(reason in div for reason in result.reasons)
    for private in ("chest pain", "medicine", "Help", task["description"]):
        assert private not in div


def test_performer_codes_are_what_the_template_can_fill(triage: Decision) -> None:
    adapter = FhirAdapter.for_agent(triage.agent)
    labels = triage.agent.manifest.labels("destination")
    assert adapter.performer_template == "{recommendation}"
    assert list(adapter.performer_codes()) == [*labels, "manual-triage"]
    assert adapter.performer_codes()["nurse"] == {"recommendation": "nurse"}


def test_missing_required_resource(triage_agent: Agent) -> None:
    adapter = FhirAdapter.for_agent(triage_agent)
    with pytest.raises(FhirError, match="Communication"):
        adapter.index({"resourceType": "Bundle", "entry": [{"resource": PATIENT}]})


def test_cds_discovery_and_card(triage: Decision) -> None:
    services = cds_hooks.discovery([triage.agent])["services"]
    assert services[0]["id"] == "mendari-test.triage"
    assert services[0]["hook"] == "mendari-message-received"

    response = cds_hooks.invoke(
        triage,
        {
            "hook": "mendari-message-received",
            "hookInstance": "d1577c69-dfbe-44ad-ba6d-3e05e953b2ea",
            "context": {"communicationId": "msg-1", "patientId": "pat-1"},
            "prefetch": {"communication": COMMUNICATION, "patient": PATIENT},
        },
    )
    card = response["cards"][0]
    assert card["indicator"] == "critical"
    assert len(card["summary"]) < 140  # CDS Hooks 2.0: under 140 characters
    assert card["suggestions"][0]["actions"][0]["resource"]["resourceType"] == "Task"


def test_cds_requires_prefetch(triage: Decision) -> None:
    with pytest.raises(FhirError, match="prefetch required"):
        cds_hooks.invoke(triage, {"hook": "mendari-message-received", "prefetch": {}})


@pytest.mark.parametrize("data", ["héllo", "not base64!", "QUJD", "", None, 12])
def test_a_base64_attachment_that_is_not_base64_maps_to_nothing(data: Any) -> None:
    """Non-ASCII text made `b64decode` raise a plain ValueError (not binascii.Error), which escaped
    the adapter as a 500; every bad value is now None, and good base64 still decodes."""
    from mendari.standards.fhir import _decode

    assert _decode(data) == ("ABC" if data == "QUJD" else None)
