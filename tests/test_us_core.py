# SPDX-License-Identifier: Apache-2.0
"""US Core 6.1.0 checks on FHIR inputs (standards/us_core.py) and how decisions use them."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari.core.decision import Decision
from mendari.core.spec import Outcome
from mendari.errors import MendariError
from mendari.server.app import create_app
from mendari.settings import fhir_validation
from mendari.standards import us_core
from tests.support import triage_records

FIXTURES = Path(__file__).parent / "fixtures" / "us-core"
CHART = "mendari-test/chart-summary"  # a patient's record in (tests/support/fixture_records.py)
PATIENT = "Patient/chart-us-core-1"  # the record's focus, in chart.json


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data


def resource(bundle: dict[str, Any], kind: str) -> dict[str, Any]:
    found: dict[str, Any] = next(
        e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == kind
    )
    return found


def found(resources: Any) -> set[tuple[str, str]]:
    return {(issue.expression, issue.code) for issue in us_core.validate(resources)}


# -- the generated profile data ----------------------------------------------------------------


def test_the_profiles_come_from_the_official_packages() -> None:
    data = json.loads(us_core.DATA.read_text(encoding="utf-8"))
    assert data["source"]["us_core"]["package"] == "hl7.fhir.us.core#6.1.0"
    assert data["source"]["r4"]["package"] == "hl7.fhir.r4.core#4.0.1"
    patient = us_core.profiles()["us-core-patient"]
    assert {"identifier", "name", "gender"} <= set(patient["required"])
    assert [code for _, code in patient["codes"]["gender"]] == [
        "male",
        "female",
        "other",
        "unknown",
    ]


def test_every_invariant_in_the_profiles_is_implemented() -> None:
    """A regenerated file with a new us-core-* constraint must not be silently unchecked."""
    for name, profile in us_core.profiles().items():
        for invariant in profile["invariants"]:
            assert (profile["type"], invariant["key"]) in us_core._INVARIANTS, (name, invariant)


# -- conforming resources ----------------------------------------------------------------------


@pytest.mark.parametrize("name", ["chart.json", "triage.json"])
def test_conforming_bundles_have_no_issues(name: str) -> None:
    """The same files pass the HL7 validator in CI (scripts/fhir_validate.py)."""
    assert us_core.validate(load(name)) == []


def test_the_triage_fixture_is_what_its_generator_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`triage.json` is the triage fixture's message as `tests/support/triage_records.py` writes
    it, the same on every run; a committed file that differs fails its `--check`."""
    assert triage_records.differing() == []
    assert triage_records.main(["--check"]) == 0
    assert load("triage.json") == triage_records.us_core_triage() == triage_records.us_core_triage()
    changed = tmp_path / "triage.json"
    changed.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(triage_records, "written", lambda: {changed: "[]\n"})
    monkeypatch.setattr(triage_records, "ROOT", tmp_path)
    capsys.readouterr()
    assert triage_records.main(["--check"]) == 1
    assert "triage.json is not what tests/support/triage_records.py writes" in (
        capsys.readouterr().err
    )


def test_a_prefetch_object_is_checked_like_a_bundle() -> None:
    bundle = load("chart.json")
    patient = resource(bundle, "Patient")
    broken = {**patient, "gender": "F"}
    prefetch = {
        "patient": broken,
        "again": {"resourceType": "Bundle", "entry": [{"resource": broken}]},
    }
    issues = us_core.validate(prefetch)
    assert [(i.resource, i.expression) for i in issues] == [(PATIENT, "Patient.gender")]


# -- each kind of rule -------------------------------------------------------------------------


def test_required_elements_and_their_children() -> None:
    patient = resource(load("chart.json"), "Patient")
    assert found({k: v for k, v in patient.items() if k != "name"}) == {
        ("Patient.name", "required")
    }
    no_system = {**patient, "identifier": [{"value": "x"}]}
    assert found(no_system) == {("Patient.identifier[0].system", "required")}


def test_required_bindings_on_codes_and_codeable_concepts() -> None:
    patient = resource(load("chart.json"), "Patient")
    assert found({**patient, "gender": "F"}) == {("Patient.gender", "code-invalid")}
    condition = resource(load("chart.json"), "Condition")
    wrong = {"coding": [{"system": f"{us_core.TERMINOLOGY}/condition-clinical", "code": "gone"}]}
    assert found({**condition, "clinicalStatus": wrong}) == {
        ("Condition.clinicalStatus", "code-invalid")
    }
    text_only = {"text": "active"}
    assert found({**condition, "clinicalStatus": text_only}) == {
        ("Condition.clinicalStatus", "code-invalid")
    }


def test_required_category_slices_pick_and_check_the_profile() -> None:
    lab = resource(load("chart.json"), "Observation")
    lab.pop("meta", None)
    assert us_core.profile_for(lab) == "us-core-observation-lab"
    vital = {**lab, "category": [{"coding": [{"system": "x", "code": "vital-signs"}]}]}
    assert us_core.profile_for(vital) == "base-observation"  # not US Core lab: base R4 only
    condition = resource(load("chart.json"), "Condition")
    condition.pop("meta", None)
    wrong = {**condition, "category": [{"coding": [{"system": "x", "code": "y"}]}]}
    assert found(wrong) == {("Condition.category", "required")}


def test_meta_profile_wins_over_the_type_default() -> None:
    condition = resource(load("chart.json"), "Condition")
    url = us_core.profiles()["us-core-condition-encounter-diagnosis"]["url"]
    claimed = {**condition, "meta": {"profile": [f"{url}|6.1.0"]}}
    assert us_core.profile_for(claimed) == "us-core-condition-encounter-diagnosis"
    assert found(claimed) == {("Condition.category", "required")}  # not an encounter diagnosis


@pytest.mark.parametrize(
    ("key", "expression"),
    [
        ("us-core-1", "Observation.effective"),
        ("us-core-3", "Observation.value"),
        ("us-core-10", "DiagnosticReport.effective"),
        ("us-core-15", "Coverage.identifier"),
        ("us-core-7", "Procedure.performed"),
        ("us-core-21", "MedicationRequest.requester"),
        ("us-core-6", "DocumentReference.content[0].attachment"),
    ],
)
def test_invariants(key: str, expression: str) -> None:
    """Each of these resources of invalid/chart.json breaks its invariant (the HL7 validator
    agrees): one invariant a resource, but for the lab result's two."""
    issues = us_core.validate(load("invalid/chart.json"))
    assert any(
        i.code == "invariant" and i.diagnostics.startswith(key) and i.expression == expression
        for i in issues
    )


def test_observation_value_or_absent_reason() -> None:
    lab = resource(load("chart.json"), "Observation")
    unvalued = {k: v for k, v in lab.items() if k != "valueQuantity"}
    assert found(unvalued) == {("Observation", "invariant")}
    unknown = {"system": f"{us_core.TERMINOLOGY}/data-absent-reason", "code": "unknown"}
    assert found({**unvalued, "dataAbsentReason": {"coding": [unknown]}}) == set()
    assert found({**unvalued, "hasMember": [{"reference": "Observation/x"}]}) == set()


def test_issues_carry_paths_never_values() -> None:
    patient = resource(load("chart.json"), "Patient")
    secret = {**patient, "gender": "SECRET-VALUE", "name": [{"text": "SECRET-NAME"}]}
    outcome = json.dumps(us_core.operation_outcome(us_core.validate(secret)))
    assert "SECRET" not in outcome
    assert json.loads(outcome)["issue"][0]["expression"]


def test_an_empty_operation_outcome_says_ok() -> None:
    outcome = us_core.operation_outcome([])
    assert outcome["issue"] == [
        {"severity": "information", "code": "informational", "diagnostics": "OK"}
    ]


# -- decisions ---------------------------------------------------------------------------------


def chart_bundle(*, conforming: bool) -> dict[str, Any]:
    bundle = copy.deepcopy(load("chart.json"))
    if not conforming:
        resource(bundle, "Patient").pop("name")
    return bundle


def test_warn_records_the_issues_and_never_changes_the_decision() -> None:
    bundle = chart_bundle(conforming=False)
    off, _ = Decision.from_hub(CHART, fhir_validation="off").decide_fhir(bundle)
    warn, _ = Decision.from_hub(CHART, fhir_validation="warn").decide_fhir(bundle)
    assert off.input_issues == ()
    assert [i["expression"] for i in warn.input_issues] == ["Patient.name"]
    assert (warn.outcome, warn.recommendation, warn.reasons) == (
        off.outcome,
        off.recommendation,
        off.reasons,
    )
    assert warn.to_dict()["input_issues"][0]["profile"].endswith("us-core-patient")


def test_enforce_blocks_before_any_provider_answers() -> None:
    result, task = Decision.from_hub(CHART, fhir_validation="enforce").decide_fhir(
        chart_bundle(conforming=False)
    )
    assert result.outcome is Outcome.BLOCK
    assert result.answers == {}
    assert result.reasons == (
        f"input rejected: not US Core 6.1.0: {PATIENT} Patient.name: name is required "
        "(US Core Patient Profile)",
    )
    assert task["status"]  # a Task is still written, so the work item is not lost


def test_enforce_lets_a_conforming_input_through() -> None:
    result, _ = Decision.from_hub(CHART, fhir_validation="enforce").decide_fhir(
        chart_bundle(conforming=True)
    )
    assert result.outcome is not Outcome.BLOCK
    assert result.answers  # the providers ran
    assert result.input_issues == ()


def test_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MENDARI_FHIR_VALIDATION", raising=False)
    assert fhir_validation() == "warn"
    monkeypatch.setenv("MENDARI_FHIR_VALIDATION", " Enforce ")
    assert fhir_validation() == "enforce"
    monkeypatch.setenv("MENDARI_FHIR_VALIDATION", "strict")
    with pytest.raises(MendariError, match="off, warn or enforce"):
        fhir_validation()


def test_the_server_reports_the_mode_and_returns_the_issues(tmp_path: Path) -> None:
    client = TestClient(create_app(db_path=tmp_path / "a.db", fhir_validation="warn"))
    assert client.get("/api/health").json()["fhir_validation"] == "warn"
    response = client.post(f"/api/agents/{CHART}/fhir/decide", json=chart_bundle(conforming=False))
    assert response.status_code == 200
    issues = response.json()["decision"]["input_issues"]
    assert [(i["resource"], i["expression"], i["severity"]) for i in issues] == [
        (PATIENT, "Patient.name", "error")
    ]
    record = client.get(f"/api/audit/{response.json()['decision']['id']}").json()
    # audited with the decision, the resource named by its type and position, not its id
    assert record["record"]["input_issues"] == [{**issues[0], "resource": "Patient (entry 1)"}]
