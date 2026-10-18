# SPDX-License-Identifier: Apache-2.0
"""The Referral Router reference pack and its synthetic data."""

from __future__ import annotations

import json
import shutil
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from mendari import Decision
from mendari.cli import main
from mendari.core.policy import Outcome
from mendari.errors import FhirError
from mendari.sandbox import synthetic_referral
from mendari.sandbox.fhir_records import REFERRAL
from mendari.standards import cds_hooks

ROOT = Path(__file__).parents[1]
PACK_DIR = ROOT / "packs" / "referral-router"
CHECKLIST = ("recent_visit_note", "ecg_on_file", "imaging_on_file", "recent_labs", "marked_urgent")
TODAY = date.today()
# A lab order, as a US Core EHR categorizes one (SNOMED CT: laboratory procedure): never a referral.
LAB = {"system": "http://snomed.info/sct", "code": "108252007"}


@pytest.fixture(scope="module")
def router() -> Decision:
    return Decision.from_path(PACK_DIR)


# -- synthetic data ----------------------------------------------------------------------------


def test_generation_is_deterministic_and_sized() -> None:
    splits = synthetic_referral.public_splits()
    assert {name: len(records) for name, records in splits.items()} == {
        "train": 1500,
        "calibration": 300,
        "test": 600,
    }
    assert splits == synthetic_referral.public_splits()
    assert len(synthetic_referral.local_site()) == 300


def test_ground_truth_follows_the_decision_table() -> None:
    records = synthetic_referral.public_splits()["test"]
    for record in records:
        assert record["labels"]["next_step"] == synthetic_referral.next_step(record["labels"])
    steps = Counter(r["labels"]["next_step"] for r in records)
    assert set(steps) == {"schedule", "return_for_info", "urgent_review", "coordinator_review"}


def test_the_checklist_agrees_with_ground_truth_on_public_data(router: Decision) -> None:
    """Public records use only the document codes the pack lists, so the checklist must be
    exact; a disagreement is a bug in the generator or the checklist."""
    provider = router.providers["checklist"]
    for record in synthetic_referral.public_splits()["calibration"]:
        answers = provider.answer(record["input"]).answers
        for question in CHECKLIST:
            assert answers[question].label == record["labels"][question], (record["id"], question)


def test_the_local_site_differs_the_way_the_card_says() -> None:
    local = synthetic_referral.local_site()
    assert sum(r["attributes"]["style"] == "shorthand" for r in local) > 100
    codes = Counter(d["code"] for r in local for d in r["input"]["documents"])
    assert codes["34117-2"] > codes["11506-3"]  # notes filed as a history and physical
    mix = Counter(r["labels"]["specialty"] for r in local)
    assert mix.most_common(1)[0][0] == "orthopedics"


def test_sandbox_generate_writes_the_referral_data(tmp_path: Path) -> None:
    local = tmp_path / "local.jsonl"
    argv = ["sandbox", "generate", "--kind", "referral", "--pack-dir", str(tmp_path / "pack")]
    assert main([*argv, "--local-out", str(local)]) == 0
    train = (tmp_path / "pack" / "data" / "train.jsonl").read_text().splitlines()
    assert len(train) == 1500 and json.loads(train[0])["id"] == "ref-00000"
    assert len(local.read_text().splitlines()) == 300


# -- decisions ---------------------------------------------------------------------------------

BASE: dict[str, Any] = {
    "as_of": "2026-09-01",
    "patient": {"age": 45, "sex": "female"},
    "referral": {
        "reason": "Acne not improving after 8 months. Please advise.",
        "priority": "routine",
    },
    "documents": [{"code": "11506-3", "date": "2026-08-20"}],
    "reports": [
        {"code": "11524-6", "date": "2026-06-10"},
        {"code": "18748-4", "date": "2026-07-15"},
    ],
    "labs": [{"code": "58410-2", "date": "2026-08-15"}],
}


def test_without_a_model_screen_nothing_is_automated(router: Decision) -> None:
    result = router.decide(BASE)
    assert result.recommendation == "schedule" and result.outcome is Outcome.HUMAN_REVIEW
    assert (
        "automation requires triage_screen = rules_and_jev or rules_and_llm (running: rules)"
        in result.reasons
    )


def test_reasons_list_what_is_missing(router: Decision) -> None:
    reason = "Palpitations for 6 months, ECG normal. Please assess."
    record = {**BASE, "referral": {"reason": reason, "priority": "routine"}, "reports": []}
    result = router.decide(record)
    assert result.recommendation == "return_for_info"
    assert "return_for_info: ecg_on_file is no (specialty is cardiology)" in result.reasons


# -- FHIR and CDS Hooks ------------------------------------------------------------------------


def _ago(days: int) -> str:
    return (TODAY - timedelta(days=days)).isoformat()


def _order(
    id_: str, reason: str, category: dict[str, str] = REFERRAL, **extra: Any
) -> dict[str, Any]:
    return {
        "resourceType": "ServiceRequest",
        "id": id_,
        "status": "draft",
        "intent": "order",
        "priority": "routine",
        "category": [{"coding": [category]}],
        "reasonCode": [{"text": reason}],
        "subject": {"reference": "Patient/p1"},
        **extra,
    }


def _record() -> dict[str, dict[str, Any]]:
    def bundle(*resources: dict[str, Any]) -> dict[str, Any]:
        return {
            "resourceType": "Bundle",
            "type": "searchset",
            "entry": [{"resource": r} for r in resources],
        }

    def coded(kind: str, id_: str, code: str, date_field: str, days: int) -> dict[str, Any]:
        document = kind == "DocumentReference"
        return {
            "resourceType": kind,
            "id": id_,
            "type" if document else "code": {"coding": [{"code": code}]},
            date_field: f"{_ago(days)}T09:00:00Z" if document else _ago(days),  # date: an instant
        }

    patient = {"resourceType": "Patient", "id": "p1", "gender": "male"}
    patient["birthDate"] = f"{TODAY.year - 61}-03-01"
    return {
        "patient": patient,
        "documents": bundle(coded("DocumentReference", "d1", "11506-3", "date", 20)),
        "reports": bundle(coded("DiagnosticReport", "r1", "18748-4", "effectiveDateTime", 60)),
        "labs": bundle(coded("Observation", "o1", "58410-2", "effectiveDateTime", 10)),
    }


def test_a_fhir_referral_becomes_a_task_for_the_specialty(router: Decision) -> None:
    prefetch = _record()
    order = _order("sr1", "Palpitations for 6 months, ECG normal. Please assess.")
    result, task = router.decide_fhir(prefetch, focus=order)
    assert result.answers["specialty"].label == "cardiology"
    assert result.recommendation == "return_for_info"  # no ECG on file
    assert task["performerType"][0]["coding"][0]["code"] == "cardiology"
    assert task["description"] == "Referral to cardiology: return_for_info"
    assert task["focus"] == {"reference": "ServiceRequest/sr1"}


def test_a_referral_under_a_site_code_is_decided_once_the_site_lists_it(
    router: Decision, tmp_path: Path
) -> None:
    """The pack matches only the fictional referral code of its synthetic examples, so an EHR's
    own referral code (a real EHR's is often SNOMED CT) decides nothing until the site lists it
    in the FHIR mapping (owner-accepted, LP-14; a site code map is LP-23)."""
    site = {"system": "https://ehr.example/referral-type", "code": "REF"}
    order = _order("sr1", "Palpitations for 6 months, ECG normal. Please assess.", category=site)
    with pytest.raises(FhirError, match="missing required FHIR resources: ServiceRequest"):
        router.decide_fhir(_record(), focus=order)
    copy = tmp_path / "referral-router"
    shutil.copytree(PACK_DIR, copy)
    mapping = copy / "fhir" / "mapping.yaml"
    mapping.write_text(mapping.read_text().replace('in: ["FR101"]', 'in: ["FR101", "REF"]'))
    result, _ = Decision.from_path(copy).decide_fhir(_record(), focus=order)
    assert result.answers["specialty"].label == "cardiology"


def test_order_sign_gives_one_card_per_referral(router: Decision) -> None:
    orders = [
        _order("sr1", "New chest tightness on exertion for 3 weeks, eases with rest. Please see."),
        _order("lab1", "Annual bloods", category=LAB),
        {"resourceType": "MedicationRequest", "id": "m1", "status": "draft"},
    ]
    request = {
        "hook": "order-sign",
        "hookInstance": "d1577c69-dfbe-44ad-ba6d-3e05e953b2ea",
        "context": {
            "userId": "Practitioner/1",
            "patientId": "p1",
            "draftOrders": {"resourceType": "Bundle", "entry": [{"resource": o} for o in orders]},
        },
        "prefetch": _record(),
    }
    cards = cds_hooks.invoke(router, request)["cards"]
    assert [c["summary"] for c in cards] == ["Possibly urgent Cardiology referral: review today"]
    assert cards[0]["indicator"] == "critical"
