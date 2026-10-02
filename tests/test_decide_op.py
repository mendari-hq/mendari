"""`POST /fhir/$decide` (standards/decide_op.py): Parameters in, a collection Bundle out."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari.core.registry import discover
from mendari.server.app import create_app
from mendari.settings import FhirValidation
from mendari.standards import audit_fhir, decide_op
from mendari.standards.fhir import DECISION_ID_SYSTEM, OUTCOME_SYSTEM
from mendari.standards.terminology import Terminology

FIXTURES = Path(__file__).parent / "fixtures" / "us-core"
INBOX = "mendari/patient-inbox-router"
PACKS = {
    "inbox.json": INBOX,
    "prior-auth.json": "mendari/prior-auth-preflight",
    "referral.json": "mendari/referral-router",
    "trial.json": "mendari/clinical-trial-prescreener",
}
MESSAGE = "Can I get a refill of my blood pressure medicine?"  # the fixture's case text


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data


def parameters(pack: str, data: dict[str, Any], **extra: str) -> dict[str, Any]:
    given = [{"name": "pack", "valueString": pack}, {"name": "data", "resource": data}]
    given += [{"name": name, "valueString": value} for name, value in extra.items()]
    return {"resourceType": "Parameters", "parameter": given}


def app_client(tmp_path: Path, validation: FhirValidation = "warn") -> TestClient:
    app = create_app(db_path=tmp_path / f"{validation}.db", fhir_validation=validation)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return app_client(tmp_path)


def entries(bundle: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["resource"]["resourceType"]: e["resource"] for e in bundle["entry"]}


def references(node: Any) -> list[str]:
    """Every `Reference.reference` in a resource or Bundle."""
    found: list[str] = []
    stack: list[Any] = [node]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            found += [node["reference"]] if isinstance(node.get("reference"), str) else []
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def outcome_of(response: Any, status: int) -> list[dict[str, Any]]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/fhir+json")
    body = response.json()
    assert body["resourceType"] == "OperationOutcome"
    issues: list[dict[str, Any]] = body["issue"]
    return issues


def test_a_decision_as_fhir(client: TestClient) -> None:
    response = client.post("/fhir/$decide", json=parameters(INBOX, load("inbox.json")))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/fhir+json")
    bundle = response.json()
    assert bundle["resourceType"] == "Bundle" and bundle["type"] == "collection"
    kinds = [e["resource"]["resourceType"] for e in bundle["entry"]]
    assert kinds == ["GuidanceResponse", "RequestGroup", "Task", "Parameters", "Provenance"]
    urls = {e["resource"]["resourceType"]: e["fullUrl"] for e in bundle["entry"]}
    found = entries(bundle)
    guidance, group, task = found["GuidanceResponse"], found["RequestGroup"], found["Task"]
    decision_id = guidance["identifier"][0]["value"]
    assert guidance["identifier"] == task["identifier"]
    assert guidance["identifier"][0]["system"] == DECISION_ID_SYSTEM
    assert guidance["status"] == "success"
    assert guidance["result"]["reference"] == urls["RequestGroup"]
    assert guidance["outputParameters"]["reference"] == urls["Parameters"]
    assert group["action"][0]["resource"]["reference"] == urls["Task"]
    assert task["basedOn"] == [{"reference": urls["RequestGroup"]}]
    patient = {"reference": "http://example.org/fhir/Patient/in-p1"}  # the input's fullUrl
    assert guidance["subject"] == group["subject"] == task["for"] == patient
    outcome = next(p for p in found["Parameters"]["parameter"] if p["name"] == "outcome")
    assert outcome["valueCoding"]["system"] == OUTCOME_SYSTEM
    record = client.get(f"/api/audit/{decision_id}").json()
    assert record["outcome"] == outcome["valueCoding"]["code"]  # recorded, like any decision
    assert MESSAGE not in response.text  # the input is never echoed


def test_the_provenance_is_the_audit_trails_own(client: TestClient) -> None:
    bundle = client.post("/fhir/$decide", json=parameters(INBOX, load("inbox.json"))).json()
    found = entries(bundle)
    provenance = found["Provenance"]
    decision_id = found["GuidanceResponse"]["identifier"][0]["value"]
    trail = client.get(f"/api/audit/{decision_id}/fhir").json()
    exported = next(
        e["resource"] for e in trail["entry"] if e["resource"]["resourceType"] == "Provenance"
    )
    targets = [{"reference": e["fullUrl"]} for e in bundle["entry"][:3:2]]  # GuidanceResponse, Task
    assert provenance == {**exported, "target": [*exported["target"], *targets]}


def test_labels_are_coded_as_the_served_codesystems_say(client: TestClient) -> None:
    """Every coding in the answers is in a CodeSystem this server serves, with its display."""
    bundle = client.post("/fhir/$decide", json=parameters(INBOX, load("inbox.json"))).json()
    terms = Terminology.for_packs(discover(None).packs)
    codings = []
    stack: list[Any] = [entries(bundle)["Parameters"]]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            codings += [node["valueCoding"]] if "valueCoding" in node else []
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    assert len(codings) > 5
    for coding in codings:
        generated = terms.by_url(coding["system"])
        assert generated is not None, coding["system"]
        served = client.get(f"/fhir/CodeSystem/{generated['id']}").json()
        concept = next(c for c in served["concept"] if c["code"] == coding["code"])
        assert coding.get("display", concept["display"]) == concept["display"]


def test_a_refused_input_is_a_failure_whose_action_is_the_fallback(client: TestClient) -> None:
    """The pack's schema needs a message body; under BLOCK the proposed work item is the pack's
    fallback, never the blocked action, and the findings say why by path and rule."""
    data = load("inbox.json")
    communication = data["entry"][1]["resource"]
    communication["payload"] = [{"contentString": " "}]
    bundle = client.post("/fhir/$decide", json=parameters(INBOX, data)).json()
    found = entries(bundle)
    guidance, group, task = found["GuidanceResponse"], found["RequestGroup"], found["Task"]
    assert guidance["status"] == "failure"
    outcome = next(p for p in found["Parameters"]["parameter"] if p["name"] == "outcome")
    assert outcome["valueCoding"]["code"] == "BLOCK"
    fallback = next(p for p in found["Parameters"]["parameter"] if p["name"] == "fallback")
    assert group["action"][0]["title"] == task["description"] == fallback["valueString"]
    issues = found["OperationOutcome"]["issue"]
    assert any(i["code"] == "processing" and "body is required" in i["diagnostics"] for i in issues)
    assert guidance["evaluationMessage"] == [{"reference": bundle["entry"][4]["fullUrl"]}]


def test_us_core_findings_under_enforce_refuse_the_input(tmp_path: Path) -> None:
    client = app_client(tmp_path, "enforce")
    data = load("invalid/referral.json")
    response = client.post("/fhir/$decide", json=parameters("mendari/referral-router", data))
    assert response.status_code == 200  # the refusal is a recorded decision, not an HTTP error
    found = entries(response.json())
    assert found["GuidanceResponse"]["status"] == "failure"
    issues = found["OperationOutcome"]["issue"]
    assert issues and all(i["expression"] for i in issues if i["severity"] == "error")


def test_focus_picks_the_resource_to_decide_about(client: TestClient) -> None:
    data = load("inbox.json")
    second = copy.deepcopy(data["entry"][1])
    second["resource"]["id"] = "in-c2"
    second["fullUrl"] = "http://example.org/fhir/Communication/in-c2"
    data["entry"].append(second)
    several = client.post("/fhir/$decide", json=parameters(INBOX, data))
    assert outcome_of(several, 422)[0]["code"] == "processing"
    chosen = client.post(
        "/fhir/$decide", json=parameters(INBOX, data, focus="Communication/in-c2")
    ).json()
    task = entries(chosen)["Task"]
    assert task["focus"] == {"reference": "http://example.org/fhir/Communication/in-c2"}
    missing = client.post("/fhir/$decide", json=parameters(INBOX, data, focus="Communication/x"))
    assert outcome_of(missing, 422)[0]["code"] == "processing"


@pytest.mark.parametrize("fixture", sorted(PACKS))
def test_every_reference_resolves(client: TestClient, fixture: str) -> None:
    """In a Bundle whose entries have urn:uuid full URLs a relative reference has no base (R4
    bundle.html), so a reference to the input is the absolute URL the input gives the resource.
    No other input URL is echoed."""
    data = load(fixture)
    given = {entry["fullUrl"] for entry in data["entry"]}
    bundle = client.post("/fhir/$decide", json=parameters(PACKS[fixture], data)).json()
    inside = {entry["fullUrl"] for entry in bundle["entry"]}
    outside = {reference for reference in references(bundle) if reference not in inside}
    assert outside and outside <= given
    task = entries(bundle)["Task"]
    assert {task["for"]["reference"], task["focus"]["reference"]} == outside
    text = json.dumps(bundle)
    assert [url for url in given - outside if url in text] == []


def without_full_urls(data: dict[str, Any]) -> None:
    for entry in data["entry"]:
        del entry["fullUrl"]


def urn_full_urls(data: dict[str, Any]) -> None:
    for index, entry in enumerate(data["entry"]):
        entry["fullUrl"] = f"urn:uuid:00000000-0000-4000-8000-00000000000{index}"


def another_resources_url(data: dict[str, Any]) -> None:
    data["entry"][0]["fullUrl"] = "http://example.org/fhir/Patient/in-p2"
    data["entry"][1]["fullUrl"] = "http://example.org/fhir/Communication/in-c1/_history/2"


def first_duplicate_has_none(data: dict[str, Any]) -> None:
    data["entry"] = [{"resource": entry["resource"]} for entry in data["entry"]] + data["entry"]


def urn_references(data: dict[str, Any]) -> None:
    """A `urn:uuid` reference resolves only in the input Bundle, so it becomes `Type/id`."""
    urn_full_urls(data)
    data["entry"][1]["resource"]["subject"]["reference"] = data["entry"][0]["fullUrl"]


@pytest.mark.parametrize(
    "change",
    [
        without_full_urls,
        urn_full_urls,
        another_resources_url,
        first_duplicate_has_none,
        urn_references,
    ],
)
def test_without_an_absolute_full_url_references_are_relative(
    client: TestClient, change: Any
) -> None:
    """Then the reference is `Type/id`: relative to the client's own FHIR server, as the
    OperationDefinition says."""
    data = load("inbox.json")
    change(data)
    found = entries(client.post("/fhir/$decide", json=parameters(INBOX, data)).json())
    task = found["Task"]
    patient = {"reference": "Patient/in-p1"}
    assert found["GuidanceResponse"]["subject"] == found["RequestGroup"]["subject"] == patient
    assert task["for"] == patient and task["focus"] == {"reference": "Communication/in-c1"}
    description = decide_op.operation_definition()["description"]
    assert "relative to the client's own FHIR server" in description


def test_an_absolute_reference_in_the_input_stays(client: TestClient) -> None:
    data = load("inbox.json")
    without_full_urls(data)
    patient = "https://ehr.example.com/r4/Patient/in-p1"
    data["entry"][1]["resource"]["subject"]["reference"] = patient
    found = entries(client.post("/fhir/$decide", json=parameters(INBOX, data)).json())
    assert found["Task"]["for"] == found["GuidanceResponse"]["subject"] == {"reference": patient}
    assert found["Task"]["focus"] == {"reference": "Communication/in-c1"}


def test_references_use_the_full_urls_of_nested_search_bundles(client: TestClient) -> None:
    """A search Bundle in `data` is flattened, and its entries' full URLs (any base) count."""
    data = load("inbox.json")
    base = "https://ehr.example.com/r4"
    search = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [
            {**e, "fullUrl": f"{base}/{e['resource']['resourceType']}/{e['resource']['id']}"}
            for e in data["entry"]
        ],
    }
    nested = {"resourceType": "Bundle", "type": "collection", "entry": [{"resource": search}]}
    task = entries(client.post("/fhir/$decide", json=parameters(INBOX, nested)).json())["Task"]
    assert task["for"] == {"reference": f"{base}/Patient/in-p1"}
    assert task["focus"] == {"reference": f"{base}/Communication/in-c1"}


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        (load("inbox.json"), 400, "invalid"),  # a bare Bundle: use the /api route
        ([1, 2], 400, "structure"),  # not a JSON object: refused before $decide reads it
        (parameters("mendari/nope", load("inbox.json")), 404, "not-found"),
        (parameters(INBOX, load("inbox.json"), version="9.9.9"), 404, "not-found"),
        (parameters(INBOX, load("inbox.json"), colour="blue"), 400, "not-supported"),
        (
            {"resourceType": "Parameters", "parameter": [{"name": "pack", "valueString": INBOX}]},
            400,
            "required",
        ),
        (
            {
                "resourceType": "Parameters",
                "parameter": [
                    {"name": "pack", "valueString": INBOX},
                    {"name": "pack", "valueString": INBOX},
                    {"name": "data", "resource": load("inbox.json")},
                ],
            },
            400,
            "invalid",
        ),
        (
            {
                "resourceType": "Parameters",
                "parameter": [
                    {"name": "pack", "valueString": INBOX},
                    {"name": "data", "resource": {"resourceType": "Patient", "id": "p"}},
                ],
            },
            400,
            "invalid",
        ),
        (
            {
                "resourceType": "Parameters",
                "parameter": [
                    {"name": "pack", "valueInteger": 7},
                    {"name": "data", "resource": load("inbox.json")},
                ],
            },
            400,
            "invalid",
        ),
    ],
)
def test_bad_requests_are_operation_outcomes(
    client: TestClient, body: Any, status: int, code: str
) -> None:
    assert outcome_of(client.post("/fhir/$decide", json=body), status)[0]["code"] == code


def test_malformed_data_is_a_400_and_never_echoed(client: TestClient) -> None:
    data = load("inbox.json")
    data["entry"][1]["resource"]["meta"] = "Synthetic secret"
    response = client.post("/fhir/$decide", json=parameters(INBOX, data))
    assert outcome_of(response, 400)[0]["code"] == "structure"
    assert "Synthetic secret" not in response.text


def test_the_operation_definition_is_served(client: TestClient) -> None:
    response = client.get("/fhir/OperationDefinition/decide")
    assert response.status_code == 200
    definition = response.json()
    assert definition == decide_op.operation_definition()
    assert definition["system"] is True and definition["instance"] is False
    names = {(p["name"], p["use"]) for p in definition["parameter"]}
    assert names == {
        ("pack", "in"),
        ("version", "in"),
        ("data", "in"),
        ("focus", "in"),
        ("return", "out"),
    }
    assert client.get("/fhir/OperationDefinition/apply").status_code == 404


def test_audit_fhir_provenance_targets_the_decision() -> None:
    """The Provenance $decide extends is the audit trail's: its first target is the decision."""
    event = {
        "event": "decision.created",
        "decision_id": "d1",
        "seq": 1,
        "created_at": "2026-10-01T12:00:00+00:00",
        "payload": {},
        "pack_id": INBOX,
        "pack_version": "0.3.0",
        "record": {"input_hash": "sha256:00"},
    }
    provenance = audit_fhir.provenance(event)
    assert provenance["target"][0]["identifier"] == {"system": DECISION_ID_SYSTEM, "value": "d1"}


@pytest.mark.parametrize(
    ("failure", "issue"),
    [
        ("budget_exhausted", "throttled"),
        ("timeout", "timeout"),
        ("invalid_response", "exception"),
        ("jev:budget_exhausted, rules:invalid_response", "throttled"),  # any_of, vote
        ("llm:timeout", "timeout"),
    ],
)
def test_provider_failures_keep_their_kind(failure: str, issue: str) -> None:
    assert decide_op._provider_issue(failure) == issue


def test_a_pack_without_fhir_input_is_a_422(client: TestClient) -> None:
    response = client.post("/fhir/$decide", json=parameters("mendari/ai-guard", load("inbox.json")))
    assert outcome_of(response, 422)[0]["code"] == "processing"


def test_the_definition_promises_only_what_happens() -> None:
    description = decide_op.operation_definition()["description"]
    assert "503" not in description and "never echoed" not in description
