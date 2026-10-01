"""CDS `order-sign`: the orders come in `context.draftOrders`; each referral gets a decision."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from mendari import Decision
from mendari.errors import FhirError
from mendari.standards import cds_hooks
from mendari.standards.fhir import FhirAdapter

MINI = Path(__file__).parent / "fixtures" / "packs" / "referral-mini"
REFERRAL = {"system": "http://snomed.info/sct", "code": "3457005"}
LAB = {"system": "http://snomed.info/sct", "code": "108252007"}


@pytest.fixture(scope="module")
def router() -> Decision:
    return Decision.from_path(MINI)


def order(
    id_: str, reason: str, category: dict[str, str] = REFERRAL, **extra: Any
) -> dict[str, Any]:
    return {
        "resourceType": "ServiceRequest",
        "id": id_,
        "status": "draft",
        "intent": "order",
        "category": [{"coding": [category]}],
        "reasonCode": [{"text": reason}],
        "subject": {"reference": "Patient/p1"},
        **extra,
    }


def ecg(days: int) -> dict[str, Any]:
    when = (date.today() - timedelta(days=days)).isoformat()
    return {
        "resourceType": "DocumentReference",
        "id": f"ecg-{days}",
        "date": when,
        "type": {"coding": [{"system": "http://loinc.org", "code": "11524-6"}]},
    }


def request(
    *orders: dict[str, Any], documents: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "hook": "order-sign",
        "hookInstance": "d1577c69-dfbe-44ad-ba6d-3e05e953b2ea",
        "context": {
            "userId": "Practitioner/1",
            "patientId": "p1",
            "draftOrders": {
                "resourceType": "Bundle",
                "type": "collection",
                "entry": [{"resource": o} for o in orders],
            },
        },
        "prefetch": {
            "patient": {"resourceType": "Patient", "id": "p1"},
            "documents": {
                "resourceType": "Bundle",
                "type": "searchset",
                "entry": [{"resource": d} for d in documents or [ecg(400)]],
            },
        },
    }


MEDICATION = {"resourceType": "MedicationRequest", "id": "m1", "status": "draft"}


def test_one_card_per_referral_and_other_orders_are_ignored(router: Decision) -> None:
    lab = order("lab-1", "Annual labs", LAB)
    cards = cds_hooks.invoke(router, request(order("r1", "Exertional chest pain"), lab, MEDICATION))
    assert [c["summary"] for c in cards["cards"]] == [
        "Referral to cardiology is missing information"
    ]
    task = cards["cards"][0]["suggestions"][0]["actions"][0]["resource"]
    assert task["focus"] == {"reference": "ServiceRequest/r1"}
    assert task["for"] == {"reference": "Patient/p1"}
    assert task["performerType"][0]["coding"][0]["code"] == "cardiology"
    assert task["description"] == "Referral return_for_info (cardiology)"
    assert any("ecg_on_file is no (specialty is cardiology)" in n["text"] for n in task["note"])


def test_two_referrals_give_two_cards(router: Decision) -> None:
    both = request(
        order("r1", "Exertional chest pain"), order("r2", "Changing mole"), documents=[ecg(30)]
    )
    summaries = [c["summary"] for c in cds_hooks.invoke(router, both)["cards"]]
    assert summaries == [
        "Referral to cardiology: ready to schedule",
        "Referral to dermatology: ready to schedule",
    ]


def test_urgent_marking_and_no_referral(router: Decision) -> None:
    urgent = request(order("r1", "Changing mole", priority="urgent"))
    card = cds_hooks.invoke(router, urgent)["cards"][0]
    assert card["summary"] == "Urgent dermatology referral: review today"
    assert cds_hooks.invoke(router, request(order("lab-1", "Labs", LAB), MEDICATION)) == {
        "cards": []
    }
    assert cds_hooks.invoke(router, request()) == {"cards": []}


def test_fhir_decide_picks_the_referral_among_other_orders(router: Decision) -> None:
    bundle = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [
            {"resource": order("r1", "Changing mole")},
            {"resource": order("lab-1", "Labs", LAB)},
        ],
    }
    result, task = router.decide_fhir(bundle)
    assert result.recommendation == "schedule" and task["focus"] == {
        "reference": "ServiceRequest/r1"
    }
    two = {
        "resourceType": "Bundle",
        "entry": [{"resource": order("r1", "a")}, {"resource": order("r2", "b")}],
    }
    with pytest.raises(FhirError, match="exactly one ServiceRequest"):
        router.decide_fhir(two)
    adapter = FhirAdapter.for_pack(router.pack)
    assert [o["id"] for o in adapter.focus_resources(two)] == ["r1", "r2"]
    assert adapter.focus_resources(None) == []
