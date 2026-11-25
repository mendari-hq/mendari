# SPDX-License-Identifier: Apache-2.0
"""Malformed input and errors: no input crashes a FHIR route, FHIR routes answer errors with an
OperationOutcome, and no error echoes what the client sent."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari.core.decision import Decision
from mendari.errors import FhirError, FhirStructureError
from mendari.server.app import create_app
from mendari.standards import fhir_structure, us_core
from mendari.standards.fhir import FhirAdapter
from tests.conftest import without_request_id

FIXTURES = Path(__file__).parent / "fixtures" / "us-core"
AGENTS = {
    "triage.json": "mendari-test/triage",  # a message (tests/support/triage_records.py)
    "chart.json": "mendari-test/chart-summary",  # a patient's record (fixture_records.py)
}
TRIAGE = "/api/agents/mendari-test/triage"
SECRET = "Synthetic secret: chest pain since noon"  # must never come back in an error
WRONG_TYPES: list[Any] = ["x", 7, {"x": 1}, ["x"]]


def element_paths(node: Any, prefix: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    if isinstance(node, dict):
        items: Any = node.items()
    elif isinstance(node, list):
        items = enumerate(node)
    else:
        return
    for key, value in items:
        if key != "resourceType":
            yield (*prefix, key)
        yield from element_paths(value, (*prefix, key))


def replaced(document: Any, path: tuple[Any, ...], value: Any) -> Any:
    changed = copy.deepcopy(document)
    node = changed
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return changed


@pytest.mark.parametrize("fixture", sorted(AGENTS))
def test_no_mistyped_element_crashes_the_fhir_layer(fixture: str) -> None:
    """Every element of a US Core fixture, replaced by each wrong JSON type: the structural gate
    rejects it, or the US Core checks, the mapping and the Task either work or raise a FhirError
    (an HTTP 4xx), never anything else."""
    seed = json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))
    decision = Decision.from_hub(AGENTS[fixture])
    adapter = FhirAdapter.for_agent(decision.agent)
    result, _ = decision.decide_fhir(seed)
    for path in element_paths(seed):
        for wrong in WRONG_TYPES:
            mistyped = replaced(seed, path, wrong)
            if fhir_structure.blocking(fhir_structure.check(mistyped)):
                continue
            try:
                us_core.validate(mistyped)
                indexed = adapter.index(mistyped)
                adapter.to_input(indexed)
                adapter.to_task(result, indexed)
            except FhirError:
                pass


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(db_path=tmp_path / "e.db"), raise_server_exceptions=False)


def message_bundle() -> dict[str, Any]:
    """The triage fixture's message: its sender, then the Communication."""
    bundle: dict[str, Any] = json.loads((FIXTURES / "triage.json").read_text(encoding="utf-8"))
    return bundle


def communication(bundle: dict[str, Any]) -> dict[str, Any]:
    found: dict[str, Any] = bundle["entry"][1]["resource"]
    assert found["resourceType"] == "Communication"
    return found


def outcome_of(response: Any, status: int) -> list[dict[str, Any]]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/fhir+json")
    body = response.json()
    assert body["resourceType"] == "OperationOutcome"
    assert all(issue["severity"] == "error" for issue in body["issue"])
    issues: list[dict[str, Any]] = body["issue"]
    return issues


@pytest.mark.parametrize(
    ("element", "value", "expression"),
    [
        ("meta", "x", "Communication.meta"),
        ("subject", "Patient/tr-p1", "Communication.subject"),
        ("subject", {"reference": 7}, "Communication.subject.reference"),
        ("id", 7, "Communication.id"),
    ],
)
def test_an_element_of_the_wrong_json_type_is_a_400(
    client: TestClient, element: str, value: Any, expression: str
) -> None:
    """These crashed with a plain-text 500 before (meta and subject in the audit)."""
    bundle = message_bundle()
    communication(bundle)[element] = value
    issues = outcome_of(client.post(f"{TRIAGE}/fhir/decide", json=bundle), 400)
    assert [(i["code"], i["expression"]) for i in issues] == [("structure", [expression])]


def test_malformed_bundles_and_codings_are_400s(client: TestClient) -> None:
    entry_not_array = {"resourceType": "Bundle", "type": "collection", "entry": {"x": 1}}
    issues = outcome_of(client.post(f"{TRIAGE}/fhir/decide", json=entry_not_array), 400)
    assert issues[0]["expression"] == ["Bundle.entry"]
    resource_not_object = {"resourceType": "Bundle", "entry": [{"resource": "Communication"}]}
    issues = outcome_of(client.post(f"{TRIAGE}/fhir/decide", json=resource_not_object), 400)
    assert issues[0]["expression"] == ["Bundle.entry[0].resource"]
    bundle = message_bundle()
    communication(bundle)["category"] = [{"coding": {"code": "x"}}]
    issues = outcome_of(client.post(f"{TRIAGE}/fhir/decide", json=bundle), 400)
    assert issues[0]["expression"] == ["Communication.category[0].coding"]


def test_structure_is_checked_even_with_validation_off(tmp_path: Path) -> None:
    """`off` skips the US Core checks, not the check that the input is FHIR JSON at all."""
    app = create_app(db_path=tmp_path / "off.db", fhir_validation="off")
    client = TestClient(app, raise_server_exceptions=False)
    bundle = message_bundle()
    communication(bundle)["meta"] = "x"
    outcome_of(client.post(f"{TRIAGE}/fhir/decide", json=bundle), 400)
    communication(bundle)["meta"] = {}
    communication(bundle)["status"] = "not-a-status"  # a US Core finding: not checked when off
    response = client.post(f"{TRIAGE}/fhir/decide", json=bundle)
    assert response.status_code == 200 and response.json()["decision"]["input_issues"] == []


def test_fhir_route_errors_are_operation_outcomes(client: TestClient) -> None:
    missing = client.post(f"{TRIAGE}/fhir/decide", json={"resourceType": "Patient", "id": "p"})
    assert outcome_of(missing, 422)[0]["code"] == "processing"
    unknown_agent = client.post("/api/agents/mendari/nope/fhir/decide", json=message_bundle())
    assert outcome_of(unknown_agent, 404)[0]["code"] == "not-found"
    no_route = client.get("/fhir/Patient/p1")  # Mendari serves no clinical resources
    assert outcome_of(no_route, 404)[0]["code"] == "not-found"


def test_a_body_that_is_not_a_json_object_is_refused_without_an_echo(client: TestClient) -> None:
    as_text = client.post(
        f"{TRIAGE}/fhir/decide", content=SECRET, headers={"content-type": "text/plain"}
    )
    assert outcome_of(as_text, 415)[0]["code"] == "not-supported" and SECRET not in as_text.text
    as_array = client.post(f"{TRIAGE}/fhir/decide", json=[SECRET])
    assert outcome_of(as_array, 400)[0]["code"] == "structure" and SECRET not in as_array.text


def test_validation_errors_do_not_echo_the_request(client: TestClient) -> None:
    """FastAPI's 422 carries the offending `input`; ours keeps only where and what."""
    response = client.post(
        "/api/review/some-id", json={"final_label": SECRET, "reviewer": "", "note": 7}
    )
    assert response.status_code == 422
    assert SECRET not in response.text
    problems = response.json()["detail"]
    assert problems and all(set(problem) == {"type", "loc", "msg"} for problem in problems)


def test_mendari_api_errors_keep_their_shape(client: TestClient) -> None:
    response = client.post("/api/agents/mendari/nope/decide", json={})
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/json"
    assert response.json()["error"] == "AgentNotFoundError"


def test_an_unexpected_error_is_a_json_500_that_says_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: Any, **__: Any) -> None:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(us_core, "validate", broken)
    fhir = client.post(f"{TRIAGE}/fhir/decide", json=message_bundle())
    assert outcome_of(fhir, 500)[0]["code"] == "exception" and SECRET not in fhir.text
    assert fhir.headers["x-content-type-options"] == "nosniff"
    monkeypatch.setattr(Decision, "decide", broken)
    api = client.post(f"{TRIAGE}/decide", json={"message": {"body": "hi"}})
    assert api.status_code == 500 and without_request_id(api.json(), api) == {
        "error": "InternalError",
        "detail": "internal error",
    }


def test_structure_errors_carry_paths_not_values() -> None:
    bundle = message_bundle()
    communication(bundle)["subject"] = SECRET
    with pytest.raises(FhirStructureError) as raised:
        Decision.from_hub("mendari-test/triage").decide_fhir(bundle)
    assert SECRET not in str(raised.value) and raised.value.issues


def test_a_null_padded_profile_is_valid_fhir(client: TestClient) -> None:
    """FHIR JSON pads a repeating primitive with null where only its extension is present."""
    bundle = message_bundle()
    patient = bundle["entry"][0]["resource"]
    absent = {"url": us_core.DATA_ABSENT, "valueCode": "unknown"}
    patient["meta"]["profile"] = [*patient["meta"]["profile"], None]
    patient["meta"]["_profile"] = [None, {"extension": [absent]}]
    assert fhir_structure.check(bundle) == []
    assert client.post(f"{TRIAGE}/fhir/decide", json=bundle).status_code == 200


DEPTH = 1500  # extension levels: two JSON containers each, past Python's recursion limit


def test_the_checks_walk_deeply_nested_fhir_without_recursion() -> None:
    """Extensions can nest deeper than Python's recursion limit (valid R4); the structural gate
    must not recurse. Before, about 500 levels was a RecursionError, a 500 over HTTP."""
    nested: dict[str, Any] = {"url": "https://mendari.co/fhir/test/leaf", "valueString": "x"}
    for depth in range(DEPTH):
        nested = {"url": f"https://mendari.co/fhir/test/{depth}", "extension": [nested]}
    resource = {**communication(message_bundle()), "extension": [nested]}
    assert fhir_structure.check(resource) == []
    nested["extension"][0]["valueCoding"] = {"code": "x"}  # a Coding, not `coding`: fine
    resource["category"] = [{"coding": "x"}]
    assert [i.expression for i in fhir_structure.check(resource)] == [
        "Communication.category[0].coding"
    ]


def test_deeply_nested_fhir_over_http_is_never_a_500(client: TestClient) -> None:
    """The JSON is built as text: before 3.12, Python's JSON encoder and parser share the
    recursion limit. Then the parser refuses the body (a 400); from 3.12 it is decided."""
    leaf = '{"url":"https://mendari.co/fhir/test/leaf","valueString":"x"}'
    nested = leaf
    for depth in range(DEPTH):
        nested = f'{{"url":"https://mendari.co/fhir/test/{depth}","extension":[{nested}]}}'
    bundle = message_bundle()
    communication(bundle)["extension"] = ["PLACEHOLDER"]
    body = json.dumps(bundle).replace('"PLACEHOLDER"', nested)
    response = client.post(
        f"{TRIAGE}/fhir/decide", content=body, headers={"content-type": "application/fhir+json"}
    )
    if response.status_code == 400:
        assert outcome_of(response, 400)[0]["code"] == "invalid"
    else:
        assert response.status_code == 200, response.text
