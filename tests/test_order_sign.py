# SPDX-License-Identifier: Apache-2.0
"""CDS `order-sign`: the orders come in `context.draftOrders`; each order of the agent's own codes
gets a decision. On the chart-summary fixture's order-sign twin (`mendari-test/chart-orders`,
`tests/support/chart_fixture.py` `order_sign`): its focus is a ServiceRequest whose code is one
of its code set `checked_orders` (ICD-10-PCS), decided with the patient's chart beside it.
Synthetic test data, not clinically reviewed."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from mendari import Decision
from mendari.errors import FhirError
from mendari.standards import cds_hooks
from mendari.standards.fhir import FhirAdapter
from tests.support.chart_fixture import draft, order_hook, order_sign
from tests.support.fixture_records import ICD10PCS, LOINC, ORDERS, SHOWCASE

PATIENT = f"Patient/{SHOWCASE.id}"
ON_FILE = f"{SHOWCASE.id}-order"  # the showcase record's order: a colonoscopy (0DJD8ZZ)
MEDICATION = {"resourceType": "MedicationRequest", "id": "m1", "status": "draft"}


@pytest.fixture(scope="module")
def twin(tmp_path_factory: pytest.TempPathFactory) -> Decision:
    return Decision.from_path(order_sign(tmp_path_factory.mktemp("orders")))


def checked(id_: str, code: str) -> dict[str, Any]:
    """A draft order the twin checks: one of its ICD-10-PCS codes."""
    title, text = ORDERS[code]
    return {
        "resourceType": "ServiceRequest",
        "id": id_,
        "status": "draft",
        "intent": "order",
        "code": {"coding": [{"system": ICD10PCS, "code": code, "display": title}], "text": text},
        "subject": {"reference": PATIENT},
    }


def lab(id_: str) -> dict[str, Any]:
    """A draft lab order (an INR, coded in LOINC): no order the twin checks."""
    return {
        "resourceType": "ServiceRequest",
        "id": id_,
        "status": "draft",
        "intent": "order",
        "code": {"coding": [{"system": LOINC, "code": "6301-6"}], "text": "INR"},
        "subject": {"reference": PATIENT},
    }


def test_one_card_per_referral_and_other_orders_are_ignored(twin: Decision) -> None:
    """The order of the agent's codes gets a card; a lab order and a MedicationRequest signed
    with it get none."""
    request = order_hook(SHOWCASE, draft(SHOWCASE), lab("lab-1"), MEDICATION)
    cards = cds_hooks.invoke(twin, request)["cards"]
    assert [c["summary"] for c in cards] == [
        # A display name ("Yes"); the Task keeps labels.
        "Synthetic chart summary: see the patient soon; anticoagulant listed: Yes"
    ]
    task = cards[0]["suggestions"][0]["actions"][0]["resource"]
    assert task["focus"] == {"reference": f"ServiceRequest/{ON_FILE}"}
    assert task["for"] == {"reference": PATIENT}
    assert task["performerType"][0]["coding"][0]["code"] == "care-team"
    described = "Synthetic chart summary: follow-up soon; anticoagulant listed: yes"
    assert task["description"] == described
    assert any("rule possible-follow-up-soon" in note["text"] for note in task["note"])


def test_two_referrals_give_two_cards(twin: Decision) -> None:
    """Two orders of the agent's codes give two cards, one decision each, on the same chart:
    the same summary, each Task about its own order."""
    request = order_hook(SHOWCASE, checked("o1", "0DJD8ZZ"), checked("o2", "0DJ08ZZ"))
    cards = cds_hooks.invoke(twin, request)["cards"]
    summary = "Synthetic chart summary: see the patient soon; anticoagulant listed: Yes"
    assert [c["summary"] for c in cards] == [summary, summary]
    focused = [c["suggestions"][0]["actions"][0]["resource"]["focus"] for c in cards]
    assert focused == [{"reference": "ServiceRequest/o1"}, {"reference": "ServiceRequest/o2"}]
    assert len({c["uuid"] for c in cards}) == 2


def test_urgent_marking_and_no_referral(twin: Decision) -> None:
    """No order of the agent's codes, or no order at all: no card."""
    assert cds_hooks.invoke(twin, order_hook(SHOWCASE, lab("lab-1"), MEDICATION)) == {"cards": []}
    unsigned = order_hook(SHOWCASE)
    unsigned["context"]["draftOrders"]["entry"] = []
    assert cds_hooks.invoke(twin, unsigned) == {"cards": []}


def test_fhir_decide_picks_the_referral_among_other_orders(twin: Decision) -> None:
    """`$decide` on a record picks the one order of the agent's codes beside a lab order; two
    such orders are refused, as the focus must be one."""
    record = SHOWCASE.bundle()
    record["entry"].append({"resource": lab("lab-1")})
    result, task = twin.decide_fhir(record)
    assert result.recommendation == "soon"
    assert task["focus"] == {"reference": f"ServiceRequest/{ON_FILE}"}
    two = copy.deepcopy(record)
    two["entry"].append({"resource": checked("o2", "0DJ08ZZ")})
    with pytest.raises(FhirError, match="exactly one ServiceRequest"):
        twin.decide_fhir(two)
    adapter = FhirAdapter.for_pack(twin.pack)
    assert [o["id"] for o in adapter.focus_resources(two)] == [ON_FILE, "o2"]
    assert adapter.focus_resources(None) == []
