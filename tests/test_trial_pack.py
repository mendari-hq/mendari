# SPDX-License-Identifier: Apache-2.0
"""The Clinical Trial Prescreener reference pack and its synthetic data."""

from __future__ import annotations

import base64
import json
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from mendari import Decision
from mendari.cli import main
from mendari.core.policy import Outcome
from mendari.sandbox import synthetic_trial
from mendari.standards import cds_hooks

ROOT = Path(__file__).parents[1]
PACK_DIR = ROOT / "packs" / "clinical-trial-prescreener"
TODAY = date.today()


@pytest.fixture(scope="module")
def trial() -> Decision:
    return Decision.from_path(PACK_DIR)


# -- synthetic data ----------------------------------------------------------------------------


def test_generation_is_deterministic_and_sized() -> None:
    splits = synthetic_trial.public_splits()
    assert {name: len(records) for name, records in splits.items()} == {
        "calibration": 200,
        "test": 300,
    }
    assert splits == synthetic_trial.public_splits()
    assert len(synthetic_trial.local_site()) == 150


def test_ground_truth_follows_the_studys_table() -> None:
    for record in synthetic_trial.public_splits()["test"]:
        labels = record["labels"]
        assert labels["eligibility"] == synthetic_trial.eligibility(labels)
    outcomes = Counter(r["labels"]["eligibility"] for r in synthetic_trial.public_splits()["test"])
    assert set(outcomes) == {"candidate", "not_eligible", "needs_review"}


def test_the_criteria_agree_with_ground_truth_on_public_data(trial: Decision) -> None:
    """Public records use only the codes and units the pack lists, so the structured criteria
    must be exact; any disagreement is a bug in the generator or the criteria."""
    provider = trial.providers["structured"]
    for record in synthetic_trial.public_splits()["calibration"]:
        answers = provider.answer(record["input"]).answers
        for question in synthetic_trial.STRUCTURED:
            assert answers[question].label == record["labels"][question], (record["id"], question)


def test_notes_never_contradict_their_own_labels() -> None:
    records = [*synthetic_trial.public_splits()["test"], *synthetic_trial.local_site()]
    for record in records:
        notes, labels = record["input"]["notes"], record["labels"]
        if labels["note_pancreatitis"] == "yes":
            assert "No history of pancreatitis" not in notes
            assert "Sin antecedentes de pancreatitis" not in notes
        if labels["note_pregnancy"] == "yes":
            assert "Pregnancy test negative" not in notes
            assert record["attributes"]["sex"] == "female"


def test_the_local_site_differs_the_way_the_card_says() -> None:
    local = synthetic_trial.local_site()
    text = json.dumps([r["input"] for r in local], ensure_ascii=False)
    for marker in ("mmol/mol", "FC101", "17856-6", "Glucophage"):
        assert marker in text
    spanish = sum(r["attributes"]["language"] == "es" for r in local) / len(local)
    assert 0.2 < spanish < 0.4


def test_sandbox_generate_writes_the_trial_data(tmp_path: Path) -> None:
    local = tmp_path / "local.jsonl"
    argv = ["sandbox", "generate", "--kind", "trial", "--pack-dir", str(tmp_path / "pack")]
    assert main([*argv, "--local-out", str(local)]) == 0
    test = (tmp_path / "pack" / "data" / "test.jsonl").read_text().splitlines()
    assert len(test) == 300 and json.loads(test[0])["id"] == "trial-0200"
    assert len(local.read_text().splitlines()) == 150


# -- decisions ---------------------------------------------------------------------------------


def test_every_decision_is_reviewed_at_risk_level_2(trial: Decision) -> None:
    for record in synthetic_trial.public_splits()["test"][:60]:
        assert trial.decide(record["input"]).outcome is not Outcome.AUTO


def test_reasons_name_the_deciding_criteria(trial: Decision) -> None:
    record = {
        "as_of": "2026-09-01",
        "patient": {"age": 54},
        "conditions": [{"code": "E11.9"}],
        "observations": [{"code": "4548-4", "value": 11.2, "unit": "%", "date": "2026-08-01"}],
        "medications": [{"name": "metformin", "status": "active", "start": "2025-01-01"}],
        "notes": "History of pancreatitis in 2019.",
    }
    result = trial.decide(record)
    assert result.recommendation == "not_eligible"
    assert "not_eligible: hba1c_in_range is no" in result.reasons
    assert "not_eligible: note_pancreatitis is yes" in result.reasons
    assert any("egfr_30_or_more" in reason for reason in result.reasons)  # no eGFR: unknown


def _note(text: str) -> dict[str, Any]:
    data = base64.b64encode(text.encode()).decode()
    return {
        "resourceType": "DocumentReference",
        "id": "n1",
        "content": [{"attachment": {"contentType": "text/plain", "data": data}}],
    }


def _ago(days: int) -> str:
    return (TODAY - timedelta(days=days)).isoformat()


def _record(notes: str = "Seen for diabetes follow-up.") -> list[dict[str, Any]]:
    def coded(resource: str, id_: str, code: str, **extra: Any) -> dict[str, Any]:
        return {"resourceType": resource, "id": id_, "code": {"coding": [{"code": code}]}, **extra}

    def lab(id_: str, code: str, value: float, unit: str, days: int) -> dict[str, Any]:
        quantity = {"valueQuantity": {"value": value, "unit": unit}}
        return coded("Observation", id_, code, effectiveDateTime=_ago(days), **quantity)

    def medication(id_: str, text: str, status: str, **period: str) -> dict[str, Any]:
        return {
            "resourceType": "MedicationStatement",
            "id": id_,
            "status": status,
            "medicationCodeableConcept": {"text": text},
            "effectivePeriod": period,
        }

    status = {"clinicalStatus": {"coding": [{"code": "active"}]}}
    return [
        {"resourceType": "Patient", "id": "p1", "birthDate": f"{TODAY.year - 58}-01-01"},
        coded("Condition", "c1", "E11.9", **status),
        coded("Condition", "c2", "I10", **status),
        coded("Condition", "c3", "I21.4", onsetDateTime=_ago(900)),  # remote MI
        lab("o1", "4548-4", 64, "mmol/mol", 20),  # 8.0 %
        lab("o2", "4548-4", 12.5, "%", 300),  # older than 90 days: ignored
        lab("o3", "33914-3", 58, "mL/min/1.73m2", 100),
        lab("o4", "8480-6", 132, "mm[Hg]", 20),
        medication("m1", "metformin 1000 mg tablet", "active", start=_ago(500)),
        medication("m2", "insulin glargine", "completed", start=_ago(900), end=_ago(400)),
        _note(notes),
    ]


def test_a_fhir_record_is_prescreened(trial: Decision) -> None:
    entries = [{"resource": r} for r in _record()]
    bundle = {"resourceType": "Bundle", "type": "collection", "entry": entries}
    result, task = trial.decide_fhir(bundle)
    assert result.recommendation == "candidate" and result.outcome is Outcome.HUMAN_REVIEW
    assert {q: result.answers[q].label for q in synthetic_trial.STRUCTURED} == dict.fromkeys(
        synthetic_trial.STRUCTURED, "yes"
    )
    assert result.answers["no_insulin"].label == "yes"  # insulin ended 400 days ago
    assert task["for"] == {"reference": "Patient/p1"}
    assert task["description"].startswith("GLYCEMIA-2 prescreen: candidate.")


def test_through_fhir_a_condition_is_read_by_its_first_coding_only(trial: Decision) -> None:
    """The FHIR mapping reads a Condition's first coding (`code.coding[0].code`). One whose first
    coding the pack does not list (a SNOMED CT coding, say) is not found even when an ICD-10-CM
    coding follows it: type 2 diabetes is `unknown` and a heart attack 40 days ago is missed. The
    known failure modes say so; a site code map (LP-23) is to change it, and this test with it."""
    site = "https://site.example/fhir/CodeSystem/conditions"
    icd = "http://hl7.org/fhir/sid/icd-10-cm"

    def decide(*, icd_first: bool) -> dict[str, str]:
        record = _record()
        for resource, local, code in ((record[1], "T2D", "E11.9"), (record[3], "MI", "I21.4")):
            codings = [{"system": site, "code": local}, {"system": icd, "code": code}]
            resource["code"] = {"coding": codings[::-1] if icd_first else codings}
        record[3]["onsetDateTime"] = _ago(40)
        bundle = {"resourceType": "Bundle", "type": "collection"}
        result, _ = trial.decide_fhir({**bundle, "entry": [{"resource": r} for r in record]})
        assert result.outcome is Outcome.HUMAN_REVIEW
        return {q: result.answers[q].label for q in ("t2d_diagnosis", "no_recent_cv_event")}

    assert decide(icd_first=True) == {"t2d_diagnosis": "yes", "no_recent_cv_event": "no"}
    assert decide(icd_first=False) == {"t2d_diagnosis": "unknown", "no_recent_cv_event": "yes"}


def test_patient_view_card(trial: Decision) -> None:
    def searchset(resources: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "resourceType": "Bundle",
            "type": "searchset",
            "entry": [{"resource": r} for r in resources],
        }

    def request(notes: str) -> dict[str, Any]:
        record = _record(notes)
        by_type: dict[str, list[dict[str, Any]]] = {}
        for resource in record[1:]:
            by_type.setdefault(resource["resourceType"], []).append(resource)
        return {
            "hook": "patient-view",
            "hookInstance": "4b4c1b2e-0000-4000-8000-000000000002",
            "context": {"patientId": "p1", "userId": "Practitioner/1"},
            "prefetch": {
                "patient": record[0],
                "conditions": searchset(by_type["Condition"]),
                "observations": searchset(by_type["Observation"]),
                "medications": searchset(by_type["MedicationStatement"]),
                "notes": searchset(by_type["DocumentReference"]),
            },
        }

    cards = cds_hooks.invoke(trial, request("Seen for diabetes follow-up."))["cards"]
    assert [c["summary"] for c in cards] == ["Possible GLYCEMIA-2 candidate: confirm eligibility"]
    assert cards[0]["suggestions"][0]["label"] == "Create a prescreening review task"
    excluded = request("Currently receiving chemotherapy for colon cancer.")
    assert cds_hooks.invoke(trial, excluded) == {"cards": []}  # clearly not eligible: silent
