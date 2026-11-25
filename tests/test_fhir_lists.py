# SPDX-License-Identifier: Apache-2.0
"""A patient's FHIR record read into an agent's lists, decided into a Task and shown as
patient-view cards: the chart fixture (`mendari-test/chart-summary`), whose mapping reads the
Patient, the Conditions, a medication list from MedicationStatements and MedicationRequests, and
the clinic notes; and what a mapping or a manifest that cannot be read does."""

from __future__ import annotations

import copy
import shutil
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from mendari import Decision, load_agent
from mendari.core.spec import AgentManifest
from mendari.errors import AgentValidationError, FhirError
from mendari.standards import cds_hooks
from mendari.standards.fhir import FhirAdapter, age_years
from tests.conftest import CHART_DIR, TRIAGE_DIR
from tests.support import chart_fixture
from tests.support.fixture_records import Case, Condition, Medication, Note
from tests.test_fhir import COMMUNICATION, PATIENT

TODAY = date.today()
RESOLVED = {
    "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
    "code": "resolved",
}
# A synthetic chart: two long-term conditions of the agent's code set and one it does not list
# (resolved, below), metformin as a statement and warfarin as a request with its dosage's period,
# a plain and an HTML note, and a lab result the agent does not read.
CASE = Case(
    id="pat-9",
    age=54,
    sex="female",
    conditions=(
        Condition("E11.9", "2019-04-01"),
        Condition("I10", "2020-02-11"),
        Condition("J06.9", "2026-09-01"),
    ),
    medications=(
        Medication("6809", "2021-02-01"),
        Medication("11289", "2025-11-03", "2026-11-02", "request", "contained", "bounds"),
    ),
    notes=(
        Note(("Seen for diabetes follow-up.", "Routine follow-up in three months."), "plain"),
        Note(("Medicines reviewed with the patient.",), "html"),
    ),
    inr=3.4,
)


def _record() -> list[dict[str, Any]]:
    resources = CASE.resources(TODAY)
    conditions = [r for r in resources if r["resourceType"] == "Condition"]
    conditions[2]["clinicalStatus"] = {"coding": [RESOLVED]}
    return resources


RECORD = _record()
PATIENT_9 = RECORD[0]
BUNDLE = {
    "resourceType": "Bundle",
    "type": "collection",
    "entry": [{"resource": r} for r in RECORD],
}


def searchset(resources: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(resources),
        "entry": [{"resource": r} for r in resources],
    }


def chart() -> Decision:
    """The chart fixture on its default providers (its canned agent)."""
    return Decision(load_agent(CHART_DIR))


def test_a_patient_record_maps_to_lists() -> None:
    adapter = FhirAdapter.for_agent(load_agent(CHART_DIR))
    inputs = adapter.to_input(adapter.index(BUNDLE))
    assert inputs["patient"] == {"age": 54, "sex": "female"}
    assert [c["code"] for c in inputs["conditions"]] == ["E11.9", "I10", "J06.9"]
    assert inputs["conditions"][0]["flag"] == "flag-routine"  # of the agent's code set
    assert inputs["conditions"][2] == {
        "code": "J06.9",
        "text": "Acute upper respiratory infection, unspecified",
        "status": "resolved",
        "onset": "2026-09-01",
    }
    assert inputs["medications"] == [  # statements first, then requests
        {
            "code": "6809",
            "name": "metformin 500 mg tablet",
            "status": "active",
            "start": "2021-02-01",
        },
        {
            "code": "11289",
            "name": "warfarin 5 mg tablet",
            "status": "active",
            "start": "2025-11-03",  # the request's dosage bounds
            "end": "2026-11-02",
        },
    ]
    assert inputs["notes"] == (
        "Seen for diabetes follow-up. Routine follow-up in three months.\n\n"
        "Medicines reviewed with the patient."
    )
    # The lab result is mapped to nothing.
    assert set(inputs) == {"patient", "problems", "notes", "conditions", "medications"}


def test_a_fhir_record_is_decided_and_becomes_a_task() -> None:
    result, task = chart().decide_fhir(BUNDLE)
    assert (result.recommendation, result.answers["on_anticoagulant"].label) == ("routine", "yes")
    assert result.outcome.value == "AUTO"  # risk level 1, a sure answer
    by_rules, _ = Decision(load_agent(CHART_DIR), providers={"reader": "rules"}).decide_fhir(BUNDLE)
    assert "routine:routine-follow-up" in by_rules.answers["follow_up"].evidence  # the notes
    assert task["focus"] == {"reference": "Patient/pat-9"}
    assert task["for"] == {"reference": "Patient/pat-9"}
    assert task["performerType"][0]["coding"][0]["code"] == "care-team"
    assert task["description"] == (
        "Synthetic chart summary: follow-up routine; anticoagulant listed: yes"
    )


def test_prefetch_search_bundles_are_flattened_and_deduplicated() -> None:
    adapter = FhirAdapter.for_agent(load_agent(CHART_DIR))
    prefetch = chart_fixture.prefetch(CASE, TODAY)
    prefetch["notes"]["entry"].insert(0, {"resource": PATIENT_9})
    indexed = adapter.index(prefetch)
    assert len(indexed["Patient"]) == 1  # the same Patient twice: kept once
    assert len(indexed["Condition"]) == 3 and len(indexed["DocumentReference"]) == 2
    other = copy.deepcopy(PATIENT_9) | {"id": "pat-10"}
    with pytest.raises(FhirError, match="exactly one Patient"):
        adapter.index({"patient": PATIENT_9, "other": other})


def cds_request(prefetch: dict[str, Any]) -> dict[str, Any]:
    return {
        "hook": "patient-view",
        "hookInstance": "4b4c1b2e-0000-4000-8000-000000000001",
        "context": {"patientId": "pat-9", "userId": "Practitioner/1"},
        "prefetch": prefetch,
    }


def test_patient_view_cards_use_the_agents_wording() -> None:
    decision = chart()
    prefetch = chart_fixture.prefetch(CASE, TODAY)
    card = cds_hooks.invoke(decision, cds_request(prefetch))["cards"][0]
    assert (
        card["summary"] == "Synthetic chart summary: routine follow-up; anticoagulant listed: Yes"
    )
    assert card["indicator"] == "info"
    assert card["suggestions"][0]["label"] == "Create a follow-up task"
    assert card["suggestions"][0]["actions"][0]["resource"]["resourceType"] == "Task"

    quiet = copy.deepcopy(prefetch)  # nothing to follow up, no anticoagulant
    unlisted = [r for r in RECORD if r["resourceType"] == "Condition"][2]
    quiet["conditions"] = searchset([unlisted])
    quiet["medication_requests"] = searchset([])
    assert cds_hooks.invoke(decision, cds_request(quiet)) == {"cards": []}  # silent


def test_the_inbox_card_wording_is_unchanged(triage: Decision) -> None:
    """A team's card names it as a person reads it, its display name (LP-23f), while the Task's
    performer code stays the label; the suggestion's label is the agent's own template's
    (LP-23j). On the triage fixture: an appointment, for the front desk."""
    appointment = {
        **COMMUNICATION,
        "payload": [{"contentString": "Can I move my appointment to Friday?"}],
    }
    request = {
        "hook": "mendari-message-received",
        "prefetch": {"communication": appointment, "patient": PATIENT},
    }
    card = cds_hooks.invoke(triage, request)["cards"][0]
    assert card["summary"] == "Front desk: a message to read (routine)"
    assert card["suggestions"][0]["label"] == "Create a routing task"
    task = card["suggestions"][0]["actions"][0]["resource"]
    assert task["performerType"][0]["coding"][0]["code"] == "front_desk"


def test_card_keys_must_be_recommendations() -> None:
    raw = yaml.safe_load((CHART_DIR / "agent.yaml").read_text())
    raw["cds_hooks"]["card"]["show"] = ["soon", "maybe"]
    with pytest.raises(ValidationError, match="unknown recommendations"):
        AgentManifest.model_validate(raw)


def test_age_years_and_bad_attachments() -> None:
    assert age_years("1970-06-01", today=date(2026, 5, 31)) == 55
    assert age_years("1970-06-01", today=date(2026, 6, 1)) == 56
    assert age_years("2030-01-01", today=date(2026, 1, 1)) is None
    adapter = FhirAdapter.for_agent(load_agent(CHART_DIR))
    broken = {"resourceType": "DocumentReference", "content": [{"attachment": {"data": "%%%"}}]}
    inputs = adapter.to_input(adapter.index({"p": PATIENT_9, "d": broken}))
    assert "notes" not in inputs


def test_validate_parses_the_fhir_mapping(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from mendari.cli import main

    root = tmp_path / "agent"
    shutil.copytree(CHART_DIR, root)
    (root / "fhir" / "mapping.yaml").write_text("fields: {code: {path: code.coding[0].code}}\n")
    assert main(["validate", str(root)]) != 0
    assert "fhir/mapping.yaml: invalid YAML" in capsys.readouterr().err


def test_invalid_manifest_or_policy_yaml_is_a_validation_error(tmp_path: Path) -> None:
    """The manifest, which holds the policy too."""
    root = tmp_path / "agent"
    shutil.copytree(TRIAGE_DIR, root)
    (root / "agent.yaml").write_text("summary: a: b\n")
    with pytest.raises(AgentValidationError, match=r"agent\.yaml: invalid YAML"):
        load_agent(root)
