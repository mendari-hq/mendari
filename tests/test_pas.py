"""Da Vinci PAS (standards/pas.py), the sandbox payer and the API. The HL7 validator checks the
request and the payer's answer against PAS 2.2.1 in the CI job `fhir-conformance`."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from mendari.sandbox import payer
from mendari.server import app as app_module
from mendari.server.app import create_app
from mendari.standards import audit_fhir, pas
from mendari.standards.fhir_client import GuardedHttp
from tests.test_davinci import COMPLETE, PA, request
from tests.test_prior_auth_pack import _order

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
PATIENT = {
    "resourceType": "Patient",
    "id": "p1",
    "identifier": [{"system": "https://mendari.co/fhir/sandbox/mrn", "value": "M-1"}],
    "name": [{"family": "Synthetic", "given": ["Case"]}],
    "gender": "female",
    "birthDate": "1975-01-01",
}
MEMBER = {"system": "http://terminology.hl7.org/CodeSystem/v2-0203", "code": "MB"}
COVERAGE = {
    "resourceType": "Coverage",
    "id": "c1",
    "status": "active",
    "identifier": [
        {"type": {"coding": [MEMBER]}, "system": "https://acme.example", "value": "A-9"}
    ],
    "beneficiary": {"reference": "Patient/p1"},
    "payor": [{"reference": "Organization/acme"}],
}
INSURER = {"resourceType": "Organization", "id": "acme", "name": "Acme (synthetic)"}
REQUESTER = {"resourceType": "Organization", "id": "clinic", "name": "Clinic (synthetic)"}


def build(**changes: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "decision": {"id": "d-1", "recommendation": "submit_ready"},
        "order": _order("mri", "72148"),
        "patient": PATIENT,
        "coverage": COVERAGE,
        "insurer": INSURER,
        "requester": REQUESTER,
        "service_type": "62",
        "place_of_service": "22",
        "now": NOW,
    }
    return pas.request_bundle(**(arguments | changes))


def test_the_request_is_a_preauthorization_claim_and_what_it_references() -> None:
    bundle = build(diagnoses=["M54.16"])
    resources = {e["fullUrl"]: e["resource"] for e in bundle["entry"]}
    claim = bundle["entry"][0]["resource"]
    assert claim["resourceType"] == "Claim" and claim["use"] == "preauthorization"
    assert claim["identifier"][0]["value"] == "d-1"  # the decision this request comes from
    for reference in (claim["patient"], claim["insurer"], claim["provider"]):
        assert reference["reference"] in resources  # everything resolves inside the Bundle
    coverage = resources[claim["insurance"][0]["coverage"]["reference"]]
    assert {"system": f"{pas.X12}/1069", "code": "18"} in coverage["relationship"]["coding"]
    item = claim["item"][0]
    assert item["productOrService"]["coding"][0]["code"] == "72148"
    assert item["category"]["coding"][0] == {"system": f"{pas.X12}/1365", "code": "62"}
    assert claim["diagnosis"][0]["diagnosisCodeableConcept"]["coding"][0]["code"] == "M54.16"
    assert build()["entry"][0]["resource"]["identifier"] == claim["identifier"]  # stable ids


def test_the_orders_own_codes_are_used() -> None:
    order = _order("mri", "72148") | {
        "category": [{"coding": [{"system": f"{pas.X12}/1365", "code": "62"}]}],
        "locationCode": [{"coding": [{"system": pas.POS, "code": "11"}]}],
    }
    item = build(order=order, service_type=None, place_of_service=None)["entry"][0]["resource"]
    assert item["item"][0]["locationCodeableConcept"]["coding"][0]["code"] == "11"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"decision": {"id": "d-1", "recommendation": "needs_attention"}}, "only a complete"),
        ({"patient": {"resourceType": "Patient", "gender": "female"}}, "the patient's name"),
        ({"coverage": COVERAGE | {"identifier": []}}, "member id"),
        ({"service_type": None}, "X12 service type"),
        ({"place_of_service": None}, "place of service"),
        (
            {
                "coverage": COVERAGE
                | {
                    "relationship": {
                        "coding": [
                            {
                                "system": pas.RELATIONSHIP,
                                "code": "child",
                            }
                        ]
                    }
                }
            },
            "dependent",
        ),
    ],
)
def test_nothing_is_filled_in(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(pas.PasError, match=message):
        build(**changes)


def test_the_sandbox_payer_certifies_well_formed_requests() -> None:
    answer = payer.respond(build(), now=NOW)
    result = pas.outcome(answer)
    assert result["outcome"] == "complete" and result["review_actions"] == ["A1"]
    assert result["authorization"].startswith("SANDBOX-")
    refused = payer.respond({"resourceType": "Bundle", "entry": []})
    assert refused["resourceType"] == "OperationOutcome"
    with TestClient(payer.create_payer_app()) as client:
        assert client.post("/Claim/$submit", json={"resourceType": "Bundle"}).status_code == 400


def test_the_api_builds_submits_and_chains_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_PAS_PAYER_URL", "https://payer.example/fhir")
    submitted: list[dict[str, Any]] = []

    def payer_endpoint(http_request: httpx.Request) -> httpx.Response:
        body = json.loads(http_request.content)
        submitted.append(body)
        assert http_request.url.path == "/fhir/Claim/$submit"
        return httpx.Response(
            200, json=payer.respond(body), headers={"content-type": "application/fhir+json"}
        )

    transport = httpx.MockTransport(payer_endpoint)
    monkeypatch.setattr(
        app_module,
        "GuardedHttp",
        lambda **kw: GuardedHttp(transport=transport, allowed={"https://payer.example"}),
    )
    client = TestClient(create_app(db_path=tmp_path / "a.db"))
    signed = client.post(
        f"/cds-services/{PA.replace('/', '.')}",
        json=request("acme", COMPLETE, _order("mri", "72148")),
    ).json()
    decision_id = signed["cards"][0]["extension"]["mendari.decision_id"]
    body = {
        "decision_id": decision_id,
        "patient": PATIENT,
        "coverage": COVERAGE,
        "insurer": INSURER,
        "requester": REQUESTER,
        "order": _order("mri", "72148"),
        "service_type": "62",
        "place_of_service": "22",
    }
    path = f"/api/packs/{PA}/pas"
    built = client.post(path, json=body).json()
    assert built["submitted"] is None and not submitted
    answer = client.post(path, json=body | {"submit": True}).json()
    assert answer["submitted"]["outcome"] == "complete" and len(submitted) == 1

    record = client.get(f"/api/audit/{decision_id}").json()
    [event] = [e for e in record["events"] if e["event"] == "pas.submitted"]
    assert event["payload"]["authorization"] == answer["submitted"]["authorization"]
    audit = [
        r
        for r in audit_fhir.decision_bundle(record)["entry"]
        if r["resource"]["resourceType"] == "AuditEvent"
    ]
    assert any(
        r["resource"]["subtype"][0]["code"] == "pas.submitted" and r["resource"]["action"] == "E"
        for r in audit
    )
    assert client.post(path, json=body | {"decision_id": "nope"}).status_code == 404
