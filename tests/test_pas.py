# SPDX-License-Identifier: Apache-2.0
"""Da Vinci PAS (standards/pas.py), the sandbox payer and the API. The HL7 validator checks the
request and the payer's answer against PAS 2.2.1 in the CI job `fhir-conformance`.

Mendari ships no X12 code value (LP-14, the owner's decision of 2026-10-07): a site gives them as
settings or with each request. The X12 values below are test configuration, as a site's settings
would give them, and are in no package (NOTICE)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from mendari import settings
from mendari.cli import main
from mendari.errors import MendariError
from mendari.sandbox import payer
from mendari.sandbox.synthetic_prior_auth import procedure_codings
from mendari.server import app as app_module
from mendari.server.app import create_app
from mendari.standards import audit_fhir, pas
from mendari.standards.fhir_client import GuardedHttp
from tests.test_davinci import COMPLETE, PA, request
from tests.test_prior_auth_pack import _order

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
# The X12 codes Mendari filled in before LP-14 (request and certification type, the Coverage's
# code for self, the payer's and the requester's entity codes), now a site's settings, and the
# review action the sandbox payer answered with, now its own setting.
SITE_CODES = {
    "request_type": "HS",
    "certification_type": "I",
    "self_relationship": "18",
    "payer_entity": "PR",
    "requester_entity": "1P",
}
REQUIRED = {key: SITE_CODES[key] for key in ("request_type", "certification_type")}
REVIEW_ACTION = "A1"
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
        "order": _order("mri", "FX101"),
        "patient": PATIENT,
        "coverage": COVERAGE,
        "insurer": INSURER,
        "requester": REQUESTER,
        "service_type": "62",
        "place_of_service": "22",
        "site_codes": SITE_CODES,
        "now": NOW,
    }
    return pas.request_bundle(**(arguments | changes))


@pytest.fixture
def no_site_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """No X12 setting from the shell running the tests."""
    for variable in pas.X12_SETTINGS.values():
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.delenv("MENDARI_SANDBOX_PAYER_REVIEW_ACTION", raising=False)


def site_settings(monkeypatch: pytest.MonkeyPatch, codes: dict[str, str] = SITE_CODES) -> None:
    for name, value in codes.items():
        monkeypatch.setenv(pas.X12_SETTINGS[name], value)


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
    # The payer reads the order's ICD-10-PCS code: the pack's fictional one is billable nowhere.
    assert item["productOrService"]["coding"] == [{"system": pas.ICD10_PCS, "code": "BR39ZZZ"}]
    assert item["category"]["coding"][0] == {"system": f"{pas.X12}/1365", "code": "62"}
    assert claim["diagnosis"][0]["diagnosisCodeableConcept"]["coding"][0]["code"] == "M54.16"
    assert build()["entry"][0]["resource"]["identifier"] == claim["identifier"]  # stable ids


def test_the_service_is_sent_only_in_a_code_system_pas_takes() -> None:
    """PAS binds the requested service (required) to X12 278 Requested Service Type: CPT (a
    site's, under its AMA licence), HCPCS, or ICD-10-PCS (the sandbox's stand-in). The reference
    pack's fictional code alone is refused, never sent."""
    fictional = _order("mri", "FX101")
    fictional["code"]["coding"] = fictional["code"]["coding"][:1]
    with pytest.raises(pas.PasError, match="procedure code in CPT, HCPCS or ICD-10-PCS"):
        build(order=fictional)
    for system in (pas.CPT, pas.HCPCS):
        coded = _order("mri", "FX101")
        coded["code"]["coding"].insert(1, {"system": system, "code": "X0000"})
        item = build(order=coded)["entry"][0]["resource"]["item"][0]
        assert item["productOrService"]["coding"] == [{"system": system, "code": "X0000"}]


def test_the_orders_own_codes_are_used() -> None:
    order = _order("mri", "FX101") | {
        "category": [{"coding": [{"system": f"{pas.X12}/1365", "code": "62"}]}],
        "locationCode": [{"coding": [{"system": pas.POS, "code": "11"}]}],
    }
    item = build(order=order, service_type=None, place_of_service=None)["entry"][0]["resource"]
    assert item["item"][0]["locationCodeableConcept"]["coding"][0]["code"] == "11"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"decision": {"id": "d-1", "recommendation": "not_required"}}, "only a complete"),
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


@pytest.mark.parametrize("name", ["request_type", "certification_type", "service_type"])
def test_an_x12_code_pas_requires_and_nothing_gives_is_an_error_naming_its_setting(
    name: str,
) -> None:
    """Never a default: a code the request, the order and the settings all leave out is refused,
    naming exactly what is missing and the setting that supplies it."""
    site = {key: value for key, value in SITE_CODES.items() if key != name}
    with pytest.raises(pas.PasError) as refused:
        build(**{name: None, "site_codes": site})
    message = str(refused.value)
    assert f"the X12 {name.replace('_', ' ')} (" in message and pas.X12_SETTINGS[name] in message
    others = set(pas.X12_SETTINGS.values()) - {pas.X12_SETTINGS[name]}
    assert not any(variable in message for variable in others), message


def test_with_no_x12_setting_the_request_names_every_setting_it_needs() -> None:
    with pytest.raises(pas.PasError) as refused:
        build(service_type=None, site_codes=None)
    message = str(refused.value)
    for name in ("request_type", "certification_type", "service_type"):
        assert pas.X12_SETTINGS[name] in message
    assert "MENDARI_PAS_SELF_RELATIONSHIP" not in message  # optional in PAS: never asked for


# What the sample PAS request (scripts/davinci_sample.py, below with a fixed decision id and
# time) was, byte for byte (`json.dumps(bundle, indent=2)` and a newline), when Mendari filled in
# the X12 codes itself: built at 6546fa0b, before LP-14's X12 change.
SAMPLE_BEFORE_SHA256 = "a6d355e0a32498aa8ebd7f8fc938db58634d7f13c453830829e1316246eb4812"


def sample_request(site_codes: dict[str, str]) -> dict[str, Any]:
    npi = "http://hl7.org/fhir/sid/us-npi"
    codings = procedure_codings("FX101")
    return pas.request_bundle(
        decision={"id": "pa-sample", "recommendation": "submit_ready"},
        order={
            "resourceType": "ServiceRequest",
            "id": "mri",
            "status": "draft",
            "intent": "order",
            "code": {"coding": codings, "text": codings[0]["display"]},
            "subject": {"reference": "Patient/pa-p1"},
            "authoredOn": "2026-09-30",
            "requester": {"reference": "Practitioner/pa-pr1"},
        },
        patient={
            "resourceType": "Patient",
            "identifier": [{"system": "https://mendari.co/fhir/sandbox/mrn", "value": "PA-P1"}],
            "name": [{"family": "Synthetic", "given": ["Case"]}],
            "gender": "female",
            "birthDate": "1975-01-01",
        },
        coverage={
            "resourceType": "Coverage",
            "id": "pa-c1",
            "status": "active",
            "beneficiary": {"reference": "Patient/pa-p1"},
            "payor": [{"identifier": {"value": "acme"}, "display": "Acme"}],
            "identifier": [
                {
                    "type": {"coding": [MEMBER]},
                    "system": "https://acme.example/member",
                    "value": "ACME-778899",
                }
            ],
        },
        insurer={
            "resourceType": "Organization",
            "name": "Acme Health Plan (synthetic)",
            "identifier": [{"system": npi, "value": "1234567893"}],
        },
        requester={
            "resourceType": "Organization",
            "name": "Riverside Clinic (synthetic)",
            "identifier": [{"system": npi, "value": "1245319599"}],
        },
        place_of_service="22",
        diagnoses=["M54.16"],
        site_codes=site_codes,
        now=NOW,
    )


@pytest.mark.usefixtures("no_site_settings")
def test_with_the_codes_mendari_filled_in_as_settings_the_request_is_what_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site_settings(monkeypatch, SITE_CODES | {"service_type": "62"})
    built = json.dumps(sample_request(settings.pas_codes()), indent=2) + "\n"
    assert hashlib.sha256(built.encode()).hexdigest() == SAMPLE_BEFORE_SHA256


def test_the_requests_own_codes_win_over_the_settings_and_the_orders_over_a_setting() -> None:
    """The request, then the order (for the service type), then the site's settings. The
    values here only need an X12 code's shape: which one wins is the point."""
    site = SITE_CODES | {"service_type": "S9"}

    def item(**changes: Any) -> dict[str, Any]:
        claim = build(**({"service_type": None, "site_codes": site} | changes))
        return dict(claim["entry"][0]["resource"]["item"][0])

    def codes(of: dict[str, Any]) -> tuple[str, str, str]:
        request_type, certification = (
            extension["valueCodeableConcept"]["coding"][0]["code"] for extension in of["extension"]
        )
        return request_type, certification, of["category"]["coding"][0]["code"]

    assert codes(item()) == ("HS", "I", "S9")
    order = _order("mri", "FX101") | {
        "category": [{"coding": [{"system": f"{pas.X12}/1365", "code": "O9"}]}]
    }
    assert codes(item(order=order)) == ("HS", "I", "O9")
    assert codes(
        item(order=order, service_type="R9", request_type="R1", certification_type="R")
    ) == ("R1", "R", "R9")


def test_codes_pas_makes_optional_are_left_out_without_a_setting() -> None:
    """The Coverage's X12 code for self and the organizations' entity codes are sent only when
    set. Without the code for self, PAS's rule `self-beneficiary` asks for the subscriber: the
    patient, whose own coverage it is. An EHR's own organization type is never sent."""
    organization_type = "http://terminology.hl7.org/CodeSystem/organization-type"
    insurer = INSURER | {"type": [{"coding": [{"system": organization_type, "code": "pay"}]}]}
    bundle = build(site_codes=REQUIRED, insurer=insurer)
    assert "x12.org/005010/1069" not in json.dumps(bundle)
    assert "x12.org/005010/98" not in json.dumps(bundle)
    resources = {e["fullUrl"]: e["resource"] for e in bundle["entry"]}
    claim = bundle["entry"][0]["resource"]
    coverage = resources[claim["insurance"][0]["coverage"]["reference"]]
    assert coverage["relationship"] == {"coding": [{"system": pas.RELATIONSHIP, "code": "self"}]}
    assert coverage["subscriber"] == claim["patient"]
    for reference in (claim["insurer"], claim["provider"]):
        assert "type" not in resources[reference["reference"]]
    # Set, they are sent, and the subscriber is left to the code (as before LP-14).
    full = {e["fullUrl"]: e["resource"] for e in build(insurer=insurer)["entry"]}
    coverage = full[claim["insurance"][0]["coverage"]["reference"]]
    assert "subscriber" not in coverage
    assert {"system": f"{pas.X12}/1069", "code": "18"} in coverage["relationship"]["coding"]
    assert full[claim["insurer"]["reference"]]["type"] == [
        {"coding": [{"system": f"{pas.X12}/98", "code": "PR"}]}
    ]


@pytest.mark.usefixtures("no_site_settings")
def test_a_setting_that_is_not_an_x12_code_is_refused_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert settings.pas_codes() == {}
    site_settings(monkeypatch, {"request_type": " HS ", "payer_entity": "PR"})
    assert settings.pas_codes() == {"request_type": "HS", "payer_entity": "PR"}
    for bad in ("hs", "HSXX", "H S", "A-1"):
        monkeypatch.setenv("MENDARI_PAS_CERTIFICATION_TYPE", bad)
        with pytest.raises(pas.PasError, match="MENDARI_PAS_CERTIFICATION_TYPE must be an X12"):
            settings.pas_codes()


def test_the_sandbox_payer_certifies_well_formed_requests() -> None:
    answer = payer.respond(build(), now=NOW, review_action=REVIEW_ACTION)
    result = pas.outcome(answer)
    assert result["outcome"] == "complete" and result["review_actions"] == [REVIEW_ACTION]
    assert result["authorization"].startswith("SANDBOX-")
    refused = payer.respond({"resourceType": "Bundle", "entry": []})
    assert refused["resourceType"] == "OperationOutcome"
    with TestClient(payer.create_payer_app()) as client:
        assert client.post("/Claim/$submit", json={"resourceType": "Bundle"}).status_code == 400


@pytest.mark.usefixtures("no_site_settings")
def test_without_its_setting_the_sandbox_payer_answers_with_no_review_action_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """PAS makes the review action code optional (`extension-reviewAction`: code 0..1), so the
    sandbox payer leaves it out rather than ship an X12 value; the HL7 validator checks that
    answer in CI (`pas-response-no-review-code.json`)."""
    assert settings.sandbox_payer_review_action() is None
    with TestClient(payer.create_payer_app()) as client:
        answer = client.post("/Claim/$submit", json=build(site_codes=REQUIRED)).json()
    result = pas.outcome(answer)
    assert result["outcome"] == "complete" and result["review_actions"] == []
    assert result["authorization"].startswith("SANDBOX-")
    [response] = [
        e["resource"] for e in answer["entry"] if e["resource"]["resourceType"] == "ClaimResponse"
    ]
    [review] = response["item"][0]["adjudication"][0]["extension"]
    assert review["extension"] == [{"url": "number", "valueString": result["authorization"]}]
    assert "x12.org" not in json.dumps(answer)
    # Set, it answers with it.
    monkeypatch.setenv("MENDARI_SANDBOX_PAYER_REVIEW_ACTION", REVIEW_ACTION)
    action = settings.sandbox_payer_review_action()
    with TestClient(payer.create_payer_app(action)) as client:
        answer = client.post("/Claim/$submit", json=build()).json()
    assert pas.outcome(answer)["review_actions"] == [REVIEW_ACTION]
    # A setting that is not an X12 code stops the payer before it listens.
    monkeypatch.setenv("MENDARI_SANDBOX_PAYER_REVIEW_ACTION", "certified")
    with pytest.raises(MendariError, match="MENDARI_SANDBOX_PAYER_REVIEW_ACTION must be"):
        settings.sandbox_payer_review_action()
    capsys.readouterr()
    assert main(["sandbox", "payer", "--port", "8299"]) == 1
    assert "MENDARI_SANDBOX_PAYER_REVIEW_ACTION must be" in capsys.readouterr().err


def submit_ready_decision(client: TestClient) -> str:
    signed = client.post(
        f"/cds-services/{PA.replace('/', '.')}",
        json=request("acme", COMPLETE, _order("mri", "FX101")),
    ).json()
    decision_id: str = signed["cards"][0]["extension"]["mendari.decision_id"]
    return decision_id


@pytest.mark.usefixtures("no_site_settings")
def test_the_api_takes_x12_codes_from_the_request_else_the_settings_and_never_fills_one_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TestClient(create_app(db_path=tmp_path / "x.db"))
    body = {
        "decision_id": submit_ready_decision(client),
        "patient": PATIENT,
        "coverage": COVERAGE,
        "insurer": INSURER,
        "requester": REQUESTER,
        "order": _order("mri", "FX101"),
        "place_of_service": "22",
    }
    path = f"/api/packs/{PA}/pas"
    refused = client.post(path, json=body)
    assert refused.status_code == 422
    for name in ("request_type", "certification_type", "service_type"):
        assert pas.X12_SETTINGS[name] in refused.text

    def item(sent: dict[str, Any]) -> tuple[list[str], str]:
        answer = client.post(path, json=sent)
        assert answer.status_code == 200, answer.text
        line = answer.json()["bundle"]["entry"][0]["resource"]["item"][0]
        kinds = [e["valueCodeableConcept"]["coding"][0]["code"] for e in line["extension"]]
        return kinds, line["category"]["coding"][0]["code"]

    site_settings(monkeypatch, SITE_CODES | {"service_type": "S9"})
    assert item(body) == (["HS", "I"], "S9")
    own = {"request_type": "R1", "certification_type": "R", "service_type": "R9"}
    assert item(body | own) == (["R1", "R"], "R9")
    # The request's codes are an X12 code's shape, or refused before anything is built.
    assert client.post(path, json=body | {"request_type": "hs"}).status_code == 422
    monkeypatch.setenv("MENDARI_PAS_PAYER_ENTITY", "payer")
    refused = client.post(path, json=body)
    assert refused.status_code == 422 and "MENDARI_PAS_PAYER_ENTITY" in refused.text


def test_a_pack_says_which_decisions_make_a_pas_request(tmp_path: Path) -> None:
    """The dashboard offers to build the request only where the route can build one."""
    client = TestClient(create_app(db_path=tmp_path / "x.db"))
    assert client.get(f"/api/packs/{PA}").json()["pas_ready"] == pas.READY
    assert client.get("/api/packs/mendari/patient-inbox-router").json()["pas_ready"] is None


@pytest.mark.usefixtures("no_site_settings")
def test_pas_build_takes_flags_else_the_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "c.db"
    decision_id = submit_ready_decision(TestClient(create_app(db_path=db)))
    files = {}
    for name, resource in {
        "patient": PATIENT,
        "coverage": COVERAGE,
        "insurer": INSURER,
        "requester": REQUESTER,
        "order": _order("mri", "FX101"),
    }.items():
        files[name] = tmp_path / f"{name}.json"
        files[name].write_text(json.dumps(resource))
    out = tmp_path / "pas.json"
    command = [
        *("pas", "build", "--db", str(db), "--decision", decision_id, "--out", str(out)),
        *(part for name, path in files.items() for part in (f"--{name}", str(path))),
        *("--place-of-service", "22", "--service-type", "62"),
    ]
    capsys.readouterr()
    assert main(command) == 1
    error = capsys.readouterr().err
    assert "MENDARI_PAS_REQUEST_TYPE" in error and "MENDARI_PAS_CERTIFICATION_TYPE" in error
    assert not out.exists()
    site_settings(monkeypatch)
    assert main(command) == 0
    line = json.loads(out.read_text())["entry"][0]["resource"]["item"][0]
    assert [e["valueCodeableConcept"]["coding"][0]["code"] for e in line["extension"]] == [
        "HS",
        "I",
    ]
    assert main([*command, "--request-type", "R1", "--certification-type", "R"]) == 0
    line = json.loads(out.read_text())["entry"][0]["resource"]["item"][0]
    assert [e["valueCodeableConcept"]["coding"][0]["code"] for e in line["extension"]] == [
        "R1",
        "R",
    ]
    capsys.readouterr()
    assert main([*command, "--request-type", "hs"]) == 1
    assert "--request-type" in capsys.readouterr().err


def test_the_api_builds_submits_and_chains_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_PAS_PAYER_URL", "https://payer.example/fhir")
    site_settings(monkeypatch)
    submitted: list[dict[str, Any]] = []

    def payer_endpoint(http_request: httpx.Request) -> httpx.Response:
        body = json.loads(http_request.content)
        submitted.append(body)
        assert http_request.url.path == "/fhir/Claim/$submit"
        return httpx.Response(
            200,
            json=payer.respond(body, review_action=REVIEW_ACTION),
            headers={"content-type": "application/fhir+json"},
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
        json=request("acme", COMPLETE, _order("mri", "FX101")),
    ).json()
    decision_id = signed["cards"][0]["extension"]["mendari.decision_id"]
    body = {
        "decision_id": decision_id,
        "patient": PATIENT,
        "coverage": COVERAGE,
        "insurer": INSURER,
        "requester": REQUESTER,
        "order": _order("mri", "FX101"),
        "service_type": "62",
        "place_of_service": "22",
    }
    path = f"/api/packs/{PA}/pas"
    built = client.post(path, json=body).json()
    assert built["submitted"] is None and not submitted
    # The decision went to a person (no model in the notes screen): it is sent once one confirms.
    refused = client.post(path, json=body | {"submit": True})
    assert refused.status_code == 422 and "reviewer confirms" in refused.text and not submitted
    review = {"final_label": "submit_ready", "reviewer": "pa-nurse"}
    assert client.post(f"/api/review/{decision_id}", json=review).status_code == 200
    answer = client.post(path, json=body | {"submit": True}).json()
    assert answer["submitted"]["outcome"] == "complete" and len(submitted) == 1

    record = client.get(f"/api/audit/{decision_id}").json()
    [event] = [e for e in record["events"] if e["event"] == "pas.submitted"]
    assert event["payload"]["review_actions"] == [REVIEW_ACTION]
    # The payer's authorization number is a note, apart from the chain (PHI mode off: readable).
    authorization = answer["submitted"]["authorization"]
    assert "authorization" not in event["payload"] and event["payload"]["outcome"] == "complete"
    assert json.loads(event["note"])["authorization"] == authorization
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


def test_an_outcome_reported_later_never_stands_in_for_the_reviewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A service key may report an outcome (`final_label`) after a review; PAS goes by the label
    the reviewer resolved, as the chain recorded it."""
    monkeypatch.setenv("MENDARI_PAS_PAYER_URL", "https://payer.example/fhir")
    site_settings(monkeypatch)
    client = TestClient(create_app(db_path=tmp_path / "o.db"))
    signed = client.post(
        f"/cds-services/{PA.replace('/', '.')}",
        json=request("acme", COMPLETE, _order("mri", "FX101")),
    ).json()
    decision_id = signed["cards"][0]["extension"]["mendari.decision_id"]
    review = {"final_label": "needs_attention", "reviewer": "pa-nurse"}
    assert client.post(f"/api/review/{decision_id}", json=review).status_code == 200
    outcome = {"final_label": "submit_ready", "source": "ehr"}
    assert client.post(f"/api/audit/{decision_id}/outcome", json=outcome).status_code == 200
    body = {
        "decision_id": decision_id,
        "patient": PATIENT,
        "coverage": COVERAGE,
        "insurer": INSURER,
        "requester": REQUESTER,
        "order": _order("mri", "FX101"),
        "service_type": "62",
        "place_of_service": "22",
        "submit": True,
    }
    refused = client.post(f"/api/packs/{PA}/pas", json=body)
    assert refused.status_code == 422 and "reviewer confirms" in refused.text


# -- A request that needs attention carries the completed DTR QuestionnaireResponse -------------
# The owner's decision of 2026-11-07. PAS 2.2.1's slice Claim.supportingInfo:AdditionalInformation
# references it, and the request Bundle holds it as an entry of its own. Its report type (X12 755)
# is a site's setting: `REPORT_TYPE` has an X12 code's shape only.
QUESTIONNAIRE = (
    "https://mendari.co/packs/mendari/prior-auth-preflight/Questionnaire/lumbar_mri-acme"
)
REPORT_TYPE = "R5"
GAP = "Back pain for two weeks."  # conservative therapy undocumented: needs_attention
COMPLETED_RESPONSE = {
    "resourceType": "QuestionnaireResponse",
    "id": "lumbar-mri-acme-response",
    "meta": {"versionId": "3"},
    "questionnaire": f"{QUESTIONNAIRE}|0.3.0",
    "status": "completed",
    "subject": {"reference": "Patient/p1"},
    "authored": "2026-09-30T11:00:00+00:00",
    "item": [
        {
            "linkId": "conservative_therapy",
            "text": "Conservative therapy documented",
            "answer": [{"valueCoding": {"system": "https://example.org/cs", "code": "yes"}}],
        }
    ],
}


def attention(**changes: Any) -> dict[str, Any]:
    """A request that needs attention, with the completed response for its questionnaire."""
    arguments: dict[str, Any] = {
        "decision": {"id": "d-1", "recommendation": pas.ATTENTION},
        "questionnaire_response": COMPLETED_RESPONSE,
        "questionnaire": QUESTIONNAIRE,
        "site_codes": SITE_CODES | {"report_type": REPORT_TYPE},
    }
    return build(**(arguments | changes))


def attached(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """The resources Claim.supportingInfo references, each found in the Bundle."""
    resources = {e["fullUrl"]: e["resource"] for e in bundle["entry"]}
    claim = bundle["entry"][0]["resource"]
    return [
        resources[info["valueReference"]["reference"]] for info in claim.get("supportingInfo", [])
    ]


def test_a_request_that_needs_attention_carries_the_completed_questionnaire_response() -> None:
    bundle = attention()
    claim = bundle["entry"][0]["resource"]
    [info] = claim["supportingInfo"]
    assert info["sequence"] == 1
    assert info["category"] == {
        "coding": [{"system": pas.PAS_CODES, "code": "additionalInformation"}]
    }
    [document] = info["extension"]
    assert document["url"] == pas.DOCUMENT_INFORMATION
    assert document["extension"] == [
        {
            "url": "reportTypeCode",
            "valueCodeableConcept": {"coding": [{"system": f"{pas.X12}/755", "code": REPORT_TYPE}]},
        }
    ]
    [response] = attached(bundle)
    # An entry of the Bundle (the last), never contained in the Claim; referenced by its fullUrl.
    assert bundle["entry"][-1]["resource"] is response and "contained" not in claim
    assert [e["resource"]["resourceType"] for e in bundle["entry"]] == [
        "Claim", "Patient", "Coverage", "Organization", "Organization", "ServiceRequest",
        "QuestionnaireResponse",
    ]  # fmt: skip
    # As the EHR gave it, but for its id and meta: profiled as DTR's, about the request's patient.
    assert response["meta"] == {"profile": [pas.PROFILE["QuestionnaireResponse"]]}
    assert "id" not in response and response["subject"] == claim["patient"]
    for key in ("questionnaire", "status", "authored", "item"):
        assert response[key] == COMPLETED_RESPONSE[key]
    assert attention()["entry"][-1]["fullUrl"] == bundle["entry"][-1]["fullUrl"]  # stable ids


def test_without_a_questionnaire_response_nothing_is_attached() -> None:
    """A request that needs attention and gives no response is built as a complete one is: no
    supportingInfo, no report type asked for."""
    bundle = attention(questionnaire_response=None, site_codes=SITE_CODES)
    assert "supportingInfo" not in bundle["entry"][0]["resource"]
    assert attached(bundle) == []
    complete = build(decision={"id": "d-1", "recommendation": pas.READY})
    assert bundle == complete


def test_a_complete_request_is_sent_as_it_was_with_a_response_or_without() -> None:
    """Other outcomes are unchanged: a complete request attaches nothing, and the others are
    refused as before, with a response or without."""
    complete = {"id": "d-1", "recommendation": pas.READY}
    assert attention(decision=complete) == build(decision=complete)
    for other in ("not_required", "not_checked"):
        for response in (COMPLETED_RESPONSE, None):
            with pytest.raises(pas.PasError, match="only a complete request"):
                attention(
                    decision={"id": "d-1", "recommendation": other}, questionnaire_response=response
                )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"questionnaire_response": {**COMPLETED_RESPONSE, "resourceType": "Questionnaire"}},
            "must be a FHIR QuestionnaireResponse",
        ),
        (
            {"questionnaire_response": {**COMPLETED_RESPONSE, "status": "in-progress"}},
            "once a clinician has completed it",
        ),
        ({"questionnaire_response": {**COMPLETED_RESPONSE, "status": None}}, "completed it"),
        ({"questionnaire_response": {**COMPLETED_RESPONSE, "status": ["completed"]}}, "completed"),
        (
            {"questionnaire_response": {**COMPLETED_RESPONSE, "questionnaire": [QUESTIONNAIRE]}},
            "answers another questionnaire",
        ),
        (
            {
                "questionnaire_response": {
                    **COMPLETED_RESPONSE,
                    "questionnaire": f"{QUESTIONNAIRE.replace('acme', 'beacon')}|0.3.0",
                }
            },
            f"answers another questionnaire: this decision's is {QUESTIONNAIRE}",
        ),
        (
            {
                "questionnaire_response": {
                    k: v for k, v in COMPLETED_RESPONSE.items() if k != "questionnaire"
                }
            },
            "answers another questionnaire",
        ),
        ({"questionnaire": None}, "no DTR questionnaire"),
    ],
)
def test_only_a_completed_response_to_the_decisions_questionnaire_is_attached(
    changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(pas.PasError, match=message):
        attention(**changes)


def test_the_report_type_comes_from_the_request_else_the_setting_and_is_never_filled_in() -> None:
    def report_type(bundle: dict[str, Any]) -> str:
        [info] = bundle["entry"][0]["resource"]["supportingInfo"]
        return str(
            info["extension"][0]["extension"][0]["valueCodeableConcept"]["coding"][0]["code"]
        )

    assert report_type(attention()) == REPORT_TYPE
    assert report_type(attention(report_type="Q7")) == "Q7"
    with pytest.raises(pas.PasError) as refused:
        attention(site_codes=SITE_CODES)
    message = str(refused.value)
    assert "the X12 report type (755: from the request, or set MENDARI_PAS_REPORT_TYPE)" in message
    assert "MENDARI_PAS_REQUEST_TYPE" not in message


def test_the_report_type_is_a_site_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENDARI_PAS_REPORT_TYPE", f" {REPORT_TYPE} ")
    assert settings.pas_codes()["report_type"] == REPORT_TYPE
    monkeypatch.setenv("MENDARI_PAS_REPORT_TYPE", "report")
    with pytest.raises(pas.PasError, match="MENDARI_PAS_REPORT_TYPE must be an X12"):
        settings.pas_codes()


def completed_response(client: TestClient, order: dict[str, Any]) -> dict[str, Any]:
    """The DTR package's pre-answered response for an order CRD updated, as a clinician completes
    it: the gap documented, and `completed`."""
    coverage = {"resourceType": "Coverage", "id": "c1", "beneficiary": {"reference": "Patient/p1"}}
    package = client.post(
        "/fhir/Questionnaire/$questionnaire-package",
        json={
            "resourceType": "Parameters",
            "parameter": [
                {"name": "coverage", "resource": coverage},
                {"name": "order", "resource": order},
            ],
        },
    ).json()
    [bundle] = [p["resource"] for p in package["parameter"] if p["name"] == "packagebundle"]
    response: dict[str, Any] = bundle["entry"][1]["resource"]
    assert response["status"] == "in-progress"  # pre-answered: not yet completed
    for item in response["item"]:
        item["answer"][0]["valueCoding"]["code"] = "yes"
    return response | {"status": "completed"}


@pytest.mark.usefixtures("no_site_settings")
def test_the_api_attaches_the_completed_response_and_sends_it_once_a_reviewer_confirms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_PAS_PAYER_URL", "https://payer.example/fhir")
    site_settings(monkeypatch, SITE_CODES | {"report_type": REPORT_TYPE})
    submitted: list[dict[str, Any]] = []

    def payer_endpoint(http_request: httpx.Request) -> httpx.Response:
        body = json.loads(http_request.content)
        submitted.append(body)
        return httpx.Response(
            200,
            json=payer.respond(body, review_action=REVIEW_ACTION),
            headers={"content-type": "application/fhir+json"},
        )

    transport = httpx.MockTransport(payer_endpoint)
    monkeypatch.setattr(
        app_module,
        "GuardedHttp",
        lambda **kw: GuardedHttp(transport=transport, allowed={"https://payer.example"}),
    )
    client = TestClient(create_app(db_path=tmp_path / "n.db"))
    signed = client.post(
        f"/cds-services/{PA.replace('/', '.')}", json=request("acme", GAP, _order("mri", "FX101"))
    ).json()
    order = signed["systemActions"][0]["resource"]
    decision_id = signed["cards"][0]["extension"]["mendari.decision_id"]
    assert client.get(f"/api/audit/{decision_id}").json()["recommendation"] == pas.ATTENTION
    response = completed_response(client, order)
    body = {
        "decision_id": decision_id,
        "patient": PATIENT,
        "coverage": COVERAGE,
        "insurer": INSURER,
        "requester": REQUESTER,
        "order": _order("mri", "FX101"),
        "service_type": "62",
        "place_of_service": "22",
    }
    path = f"/api/packs/{PA}/pas"

    built = client.post(path, json=body | {"questionnaire_response": response})
    assert built.status_code == 200, built.text
    [sent] = attached(built.json()["bundle"])
    assert sent["questionnaire"] == response["questionnaire"] and sent["item"] == response["item"]
    # Without a response, nothing is attached.
    bare = client.post(path, json=body).json()["bundle"]
    assert attached(bare) == [] and "supportingInfo" not in bare["entry"][0]["resource"]
    # Only a completed response, to this decision's questionnaire, with an X12 code's shape.
    unfinished = client.post(
        path, json=body | {"questionnaire_response": response | {"status": "in-progress"}}
    )
    assert unfinished.status_code == 422 and "completed it" in unfinished.text
    other = response | {
        "questionnaire": response["questionnaire"].replace("lumbar_mri", "knee_arthroscopy")
    }
    refused = client.post(path, json=body | {"questionnaire_response": other})
    assert refused.status_code == 422 and "answers another questionnaire" in refused.text
    malformed = body | {"questionnaire_response": response, "report_type": "report"}
    assert client.post(path, json=malformed).status_code == 422

    # Sent to the payer once a reviewer confirms the request (needs_attention is never automated).
    attach = body | {"questionnaire_response": response, "submit": True}
    unconfirmed = client.post(path, json=attach)
    assert unconfirmed.status_code == 422 and "reviewer confirms" in unconfirmed.text
    assert not submitted
    review = {"final_label": "submit_ready", "reviewer": "pa-nurse"}
    assert client.post(f"/api/review/{decision_id}", json=review).status_code == 200
    answer = client.post(path, json=attach)
    assert answer.status_code == 200, answer.text
    assert answer.json()["submitted"]["outcome"] == "complete"
    [request_sent] = submitted
    [sent] = attached(request_sent)
    assert sent["resourceType"] == "QuestionnaireResponse" and sent["item"] == response["item"]


@pytest.mark.usefixtures("no_site_settings")
def test_pas_build_attaches_a_completed_response_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "c.db"
    client = TestClient(create_app(db_path=db))
    signed = client.post(
        f"/cds-services/{PA.replace('/', '.')}", json=request("acme", GAP, _order("mri", "FX101"))
    ).json()
    decision_id = signed["cards"][0]["extension"]["mendari.decision_id"]
    files = {}
    for name, resource in {
        "patient": PATIENT,
        "coverage": COVERAGE,
        "insurer": INSURER,
        "requester": REQUESTER,
        "order": _order("mri", "FX101"),
        "questionnaire-response": completed_response(
            client, signed["systemActions"][0]["resource"]
        ),
    }.items():
        files[name] = tmp_path / f"{name}.json"
        files[name].write_text(json.dumps(resource))
    out = tmp_path / "pas.json"
    command = [
        *("pas", "build", "--db", str(db), "--decision", decision_id, "--out", str(out)),
        *(part for name, path in files.items() for part in (f"--{name}", str(path))),
        *("--place-of-service", "22", "--service-type", "62"),
    ]
    site_settings(monkeypatch)
    capsys.readouterr()
    assert main(command) == 1
    assert "MENDARI_PAS_REPORT_TYPE" in capsys.readouterr().err and not out.exists()
    assert main([*command, "--report-type", REPORT_TYPE]) == 0
    [response] = attached(json.loads(out.read_text()))
    assert response["status"] == "completed"
    capsys.readouterr()
    assert main([*command, "--report-type", "r5"]) == 1
    assert "--report-type" in capsys.readouterr().err
