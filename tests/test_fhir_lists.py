from __future__ import annotations

import base64
import copy
import shutil
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from mendari import Decision, load_pack
from mendari.core.spec import PackManifest
from mendari.errors import FhirError, PackValidationError
from mendari.standards import cds_hooks
from mendari.standards.fhir import FhirAdapter, age_years
from tests.conftest import INBOX_DIR
from tests.test_fhir import COMMUNICATION, PATIENT

MINI = Path(__file__).parent / "fixtures" / "packs" / "prescreen-mini"
TODAY = date.today()


def ago(days: int) -> str:
    return (TODAY - timedelta(days=days)).isoformat()


def note(text: str, id_: str) -> dict[str, Any]:
    data = base64.b64encode(text.encode()).decode()
    return {
        "resourceType": "DocumentReference",
        "id": id_,
        "content": [{"attachment": {"contentType": "text/plain", "data": data}}],
    }


def condition(id_: str, code: str, text: str, status: str = "active") -> dict[str, Any]:
    return {
        "resourceType": "Condition",
        "id": id_,
        "code": {"coding": [{"code": code}], "text": text},
        "clinicalStatus": {"coding": [{"code": status}]},
    }


def observation(id_: str, code: str, value: float, unit: str, days: int) -> dict[str, Any]:
    return {
        "resourceType": "Observation",
        "id": id_,
        "code": {"coding": [{"code": code}]},
        "valueQuantity": {"value": value, "unit": unit},
        "effectiveDateTime": ago(days),
    }


PATIENT_9 = {"resourceType": "Patient", "id": "pat-9", "birthDate": f"{TODAY.year - 54}-01-01"}
RECORD = [
    PATIENT_9,
    condition("c1", "E11.9", "Type 2 diabetes mellitus"),
    condition("c2", "I10", "Hypertension"),
    condition("c3", "E78.5", "Hyperlipidaemia", status="resolved"),
    observation("o1", "4548-4", 64, "mmol/mol", 20),  # = 8.0 %
    observation("o2", "4548-4", 12.5, "%", 400),  # out of range, but outside the window
    observation("o3", "33914-3", 72, "mL/min/1.73m2", 30),
    observation("o4", "8480-6", 132, "mm[Hg]", 30),
    {"resourceType": "MedicationStatement", "id": "m1", "status": "active"},
    {"resourceType": "MedicationStatement", "id": "m2", "status": "stopped"},
    note("Seen for diabetes follow-up. No history of pancreatitis.", "n1"),
    note("Family history: mother had diabetes.", "n2"),
]
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


def test_a_patient_record_maps_to_lists() -> None:
    adapter = FhirAdapter.for_pack(Decision.from_path(MINI).pack)
    inputs = adapter.to_input(adapter.index(BUNDLE))
    assert inputs["patient"] == {"age": 54}
    assert [c["code"] for c in inputs["conditions"]] == ["E11.9", "I10", "E78.5"]
    assert inputs["conditions"][2] == {
        "code": "E78.5",
        "text": "Hyperlipidaemia",
        "status": "resolved",
    }
    assert len(inputs["observations"]) == 4
    assert inputs["observations"][0] == {
        "code": "4548-4",
        "value": 64,
        "unit": "mmol/mol",
        "date": ago(20),
    }
    assert inputs["notes"] == (
        "Seen for diabetes follow-up. No history of pancreatitis.\n\n"
        "Family history: mother had diabetes."
    )
    assert "medications" not in inputs  # not mapped by this pack


def test_a_fhir_record_is_decided_and_becomes_a_task() -> None:
    decision = Decision.from_path(MINI)
    result, task = decision.decide_fhir(BUNDLE)
    assert result.recommendation == "candidate"
    assert result.answers["hba1c_in_range"].evidence[0].startswith("4548-4 64 mmol/mol (= 8.01 %)")
    assert result.outcome.value == "HUMAN_REVIEW"  # risk level 2
    assert task["focus"] == {"reference": "Patient/pat-9"}
    assert task["for"] == {"reference": "Patient/pat-9"}
    assert task["performerType"][0]["coding"][0]["code"] == "research-coordinator"
    assert (
        task["description"] == "Prescreen result: candidate. Review eligibility for the toy study."
    )


def test_prefetch_search_bundles_are_flattened_and_deduplicated() -> None:
    adapter = FhirAdapter.for_pack(Decision.from_path(MINI).pack)
    prefetch = {
        "patient": PATIENT_9,
        "conditions": searchset([r for r in RECORD if r["resourceType"] == "Condition"]),
        "observations": searchset([r for r in RECORD if r["resourceType"] == "Observation"]),
        "notes": searchset(
            [PATIENT_9, *[r for r in RECORD if r["resourceType"] == "DocumentReference"]]
        ),
    }
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


def test_patient_view_cards_use_the_packs_wording() -> None:
    decision = Decision.from_path(MINI)
    prefetch = {
        "patient": PATIENT_9,
        "conditions": searchset([r for r in RECORD if r["resourceType"] == "Condition"]),
        "observations": searchset([r for r in RECORD if r["resourceType"] == "Observation"]),
        "notes": searchset([r for r in RECORD if r["resourceType"] == "DocumentReference"]),
    }
    card = cds_hooks.invoke(decision, cds_request(prefetch))["cards"][0]
    assert card["summary"] == "Possible candidate for the toy study: review eligibility"
    assert card["indicator"] == "info"
    assert card["suggestions"][0]["label"] == "Create a prescreening review task"
    assert card["suggestions"][0]["actions"][0]["resource"]["resourceType"] == "Task"

    excluded = copy.deepcopy(prefetch)
    excluded["notes"] = searchset([note("History of pancreatitis in 2019.", "n3")])
    assert cds_hooks.invoke(decision, cds_request(excluded)) == {"cards": []}  # silent


def test_the_inbox_card_wording_is_unchanged(router: Decision) -> None:
    request = {
        "hook": "mendari-message-received",
        "prefetch": {"communication": COMMUNICATION, "patient": PATIENT},
    }
    card = cds_hooks.invoke(router, request)["cards"][0]
    assert card["summary"] == "Review: route to nurse (stat)"
    assert card["suggestions"][0]["label"] == "Create routing task for nurse"


def test_card_keys_must_be_recommendations() -> None:
    raw = yaml.safe_load((MINI / "pack.yaml").read_text())
    raw["cds_hooks"]["card"]["show"] = ["candidate", "maybe"]
    with pytest.raises(ValidationError, match="unknown recommendations"):
        PackManifest.model_validate(raw)


def test_age_years_and_bad_attachments() -> None:
    assert age_years("1970-06-01", today=date(2026, 5, 31)) == 55
    assert age_years("1970-06-01", today=date(2026, 6, 1)) == 56
    assert age_years("2030-01-01", today=date(2026, 1, 1)) is None
    adapter = FhirAdapter.for_pack(Decision.from_path(MINI).pack)
    broken = {"resourceType": "DocumentReference", "content": [{"attachment": {"data": "%%%"}}]}
    inputs = adapter.to_input(adapter.index({"p": PATIENT_9, "d": broken}))
    assert "notes" not in inputs


def test_validate_parses_the_fhir_mapping(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from mendari.cli import main

    root = tmp_path / "pack"
    shutil.copytree(MINI, root)
    (root / "fhir" / "mapping.yaml").write_text("fields: {code: {path: code.coding[0].code}}\n")
    assert main(["validate", str(root)]) != 0
    assert "fhir/mapping.yaml: invalid YAML" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("name", "reported"), [("pack.yaml", "pack.yaml"), ("policy.yaml", "policy.yaml")]
)
def test_invalid_manifest_or_policy_yaml_is_a_validation_error(
    tmp_path: Path, name: str, reported: str
) -> None:
    root = tmp_path / "pack"
    shutil.copytree(INBOX_DIR, root)
    (root / name).write_text("summary: a: b\n")
    with pytest.raises(PackValidationError, match=f"{reported}: invalid YAML"):
        load_pack(root)
