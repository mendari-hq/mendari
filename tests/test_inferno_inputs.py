# SPDX-License-Identifier: Apache-2.0
"""The Inferno DTR preset (scripts/inferno_dtr_inputs.py), built against an engine in this
process as conformance.yml builds it against the compose engine: valid Parameters, one request
per `$questionnaire-package` input kind and each kind alone (Inferno's 3.1.2.03 checks that),
the source data no engine can resolve, and synthetic ids only. No Inferno and no Docker here."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari.server.app import create_app
from mendari.standards import davinci, fhir_structure

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "inferno_dtr_inputs.py"


def _script() -> Any:
    spec = importlib.util.spec_from_file_location("inferno_dtr_inputs", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


inputs = _script()
FHIR_BASE = "http://engine:8000/fhir"
# The ids the synthetic builders give (scripts/davinci_sample.py): the patient, the practitioner,
# the Coverage and the orders. Any other id in the preset is one the engine made (a decision id).
SYNTHETIC = {"pa-p1", "pa-pr1", "pa-c1", "mri", "mri2"}
ENGINE_IDS = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@pytest.fixture(scope="module")
def engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    # As sandbox/inferno/docker-compose.yml's engine: open mode under the sandbox flag, since
    # Inferno calls it from another machine (LP-23c). Built once for the module, before the
    # suite's own per-test flag is set.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MENDARI_CDS_SANDBOX", "1")
        app = create_app(db_path=tmp_path_factory.mktemp("inferno") / "e.db")
        with TestClient(app) as client:
            yield client


@pytest.fixture(scope="module")
def preset(engine: TestClient) -> dict[str, Any]:
    built: dict[str, Any] = inputs.build(engine, FHIR_BASE)
    return built


def values(preset: dict[str, Any]) -> dict[str, Any]:
    return {given["name"]: given["value"] for given in preset["inputs"]}


def requests(preset: dict[str, Any]) -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = json.loads(
        values(preset)["questionnaire_package_request_parameters"]
    )
    return loaded


def kinds(body: dict[str, Any]) -> set[str]:
    return {p["name"] for p in body["parameter"]} - {"coverage"}


def test_the_preset_is_for_the_suite_and_the_engine_inferno_reaches(
    preset: dict[str, Any],
) -> None:
    assert preset["test_suite_id"] == "dtr_payer_server_v220"
    given = values(preset)
    assert set(given) == {
        "url",
        "backend_services_smart_auth_info",
        "questionnaire_package_request_parameters",
        "source_data_error_request",
        "questionnaire_canonical",
        "invalid_questionnaire_response",
    }
    assert given["url"] == FHIR_BASE
    # A static token the open engine never reads: no Mendari credential (`ohk_…`), no key.
    assert given["backend_services_smart_auth_info"] == {
        "auth_type": "backend_services",
        "access_token": inputs.STATIC_TOKEN,
    }
    assert not inputs.STATIC_TOKEN.startswith("ohk_")


def test_the_bodies_are_valid_parameters_one_per_input_kind(
    engine: TestClient, preset: dict[str, Any]
) -> None:
    bodies = requests(preset)
    assert [kinds(body) for body in bodies] == [
        {"order"},
        {"context"},
        {"questionnaire"},
        {"questionnaire"},
    ]
    canonicals = [bodies[2]["parameter"][1]["valueCanonical"], bodies[3]["parameter"][1]]
    assert "|" in canonicals[0]  # the canonical CRD linked, with its version
    assert "|" not in canonicals[1]["valueCanonical"]  # the blank canonical case
    for body in [*bodies, json.loads(values(preset)["source_data_error_request"])]:
        assert body["resourceType"] == "Parameters"
        assert fhir_structure.check(body) == []
        coverage = body["parameter"][0]
        assert coverage["name"] == "coverage" and coverage["resource"]["resourceType"] == "Coverage"
        # CRD's profile-coverage: the member id and the relationship (HL7's code, not X12's).
        assert coverage["resource"]["identifier"][0]["value"] == inputs.MEMBER_ID
        assert coverage["resource"]["relationship"]["coding"][0]["code"] == "self"
    # Each request gets one package, as the script checked before writing the preset.
    for body in bodies:
        answer = engine.post(inputs.PACKAGE, json=body).json()
        assert [p["name"] for p in answer["parameter"]] == ["packagebundle"]


def test_the_order_and_its_context_name_one_decision_and_the_canonical_crd_linked(
    preset: dict[str, Any],
) -> None:
    order, context, canonical, _ = requests(preset)
    signed = order["parameter"][1]["resource"]
    decision_id = davinci.assertion_id(signed)
    assert decision_id is not None
    assert context["parameter"][1] == {"name": "context", "valueString": decision_id}
    linked = inputs.coverage_information(signed)["questionnaire"]
    assert canonical["parameter"][1]["valueCanonical"] == linked
    assert values(preset)["questionnaire_canonical"] == linked


def test_the_source_data_error_names_no_decision_and_is_refused(
    engine: TestClient, preset: dict[str, Any]
) -> None:
    body = json.loads(values(preset)["source_data_error_request"])
    assert kinds(body) == {"context"}
    assert body["parameter"][1]["valueString"] == inputs.UNKNOWN_CONTEXT
    refused = engine.post(inputs.PACKAGE, json=body)
    assert refused.status_code == 422 and refused.json()["resourceType"] == "OperationOutcome"


def test_the_invalid_response_is_completed_with_its_required_questions_unanswered(
    preset: dict[str, Any],
) -> None:
    response = json.loads(values(preset)["invalid_questionnaire_response"])
    assert fhir_structure.check(response) == []
    [form] = response["contained"]
    assert response["questionnaire"] == f"#{form['id']}" and response["status"] == "completed"
    assert any(item["required"] for item in form["item"]) and "item" not in response


def _ids(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        if isinstance(value.get("id"), str):
            yield value["id"]
        if isinstance(value.get("reference"), str):
            yield value["reference"].split("/")[-1]
        for item in value.values():
            yield from _ids(item)
    elif isinstance(value, list):
        for item in value:
            yield from _ids(item)


def test_every_id_is_synthetic_or_the_engines(preset: dict[str, Any]) -> None:
    every = [
        *requests(preset),
        json.loads(values(preset)["source_data_error_request"]),
        json.loads(values(preset)["invalid_questionnaire_response"]),
    ]
    found = set(_ids(every))
    made_here = {i for i in found if ENGINE_IDS.fullmatch(i)}
    response_ids = {"synthetic-invalid-response", "form"}
    assert {"pa-p1", "pa-c1", "mri2"} <= found - made_here - response_ids <= SYNTHETIC
    # The canonicals are Mendari's own names for its questionnaires: no other server's.
    canonicals = [
        p["valueCanonical"] for b in every[:5] for p in b["parameter"] if "valueCanonical" in p
    ]
    assert canonicals and all(c.startswith(f"{davinci.PACKS}/") for c in canonicals)


def test_an_engine_that_does_not_answer_as_the_suite_needs_writes_no_preset(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the prior-authorization pack there is no order-sign service: the script stops
    and writes nothing, rather than a preset that would test nothing."""
    monkeypatch.setenv("MENDARI_PACKS", "mendari/referral-router")
    refused = pytest.raises(inputs.InputsError, match="order-sign needs-attention: HTTP 404")
    with TestClient(create_app(db_path=tmp_path / "other.db")) as other, refused:
        inputs.build(other, FHIR_BASE)
    out = tmp_path / "preset.json"
    assert inputs.main(["--engine", "http://127.0.0.1:9", "--out", str(out)]) == 1
    assert not out.exists() and "inferno inputs:" in capsys.readouterr().err
