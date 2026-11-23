# SPDX-License-Identifier: Apache-2.0
"""The inputs of Inferno's scripted suites (scripts/inferno_scripted_inputs.py, LP-23l): what the
CRD, SMART App Launch and Bulk Data suites are given about the engines, on synthetic data only.
The engines' URLs and client ids are the compose project's, CRD's requests are what an EHR
sends (no prefetch, draft orders naming their clinician, the FHIR authorization the kit
checks), and the Mock EHR holds every resource the requests name. No Inferno here."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from mendari.sandbox import ehr_profile

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "inferno_scripted_inputs.py"
COMPOSE = ROOT / "sandbox" / "inferno" / "scripted" / "docker-compose.yml"
TODAY = date(2026, 11, 6)


def _script() -> Any:
    spec = importlib.util.spec_from_file_location("inferno_scripted_inputs", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


inputs = _script()


def _values(preset: dict[str, Any]) -> dict[str, str]:
    assert all(isinstance(item["value"], str) for item in preset["inputs"])  # as Inferno reads
    return {item["name"]: item["value"] for item in preset["inputs"]}


def _environment(service: str) -> dict[str, str]:
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    return {k: str(v) for k, v in services[service]["environment"].items()}


def test_crd_calls_the_signed_in_engine_at_its_public_url_with_authentication() -> None:
    values = _values(inputs.crd(TODAY))
    assert values["base_url"] == _environment("engine-signin")["MENDARI_PUBLIC_URL"]
    assert (values["authentication_required"], values["encryption_method"]) == ("yes", "ES384")
    assert "jwks_kid" not in values  # the kit's first ES384 key
    assert values["manual_continuation"] == "no"  # Inferno's own job resumes each hook test
    # Only the prior-authorization pack's service is CRD's; the others are left out, split as
    # the kit splits the list.
    assert values["crd_discovery_service_ignore_list"].split(", ") == [
        "mendari.clinical-trial-prescreener",
        "mendari.referral-router",
        "mendari.patient-inbox-router",
    ]
    for name in ("any_hook", "order_sign", "technical_issues", "no_member_found"):
        assert values[f"{name}_service_ids"] == "mendari.prior-auth-preflight"


def _bodies(values: dict[str, str]) -> list[dict[str, Any]]:
    bodies = [json.loads(values["any_hook_request_body"])]
    bodies += json.loads(values["order_sign_request_bodies"])
    bodies += [json.loads(values[f"{case}_request_body"]) for case in inputs.COVERAGE_CASES]
    return [*bodies, json.loads(values["technical_issues_request_body"])]


def test_each_crd_request_is_an_ehrs_order_sign_with_the_authorization_the_kit_checks() -> None:
    values = _values(inputs.crd(TODAY))
    signed = json.loads(values["order_sign_request_bodies"])
    assert [b["context"]["patientId"] for b in signed] == [
        p.id for p in ehr_profile.patients(TODAY, ehr_profile.PRIOR_AUTH)
    ]
    technical = json.loads(values["technical_issues_request_body"])
    for body in _bodies(values):
        assert body["hook"] == "order-sign"
        if body != technical:
            assert "prefetch" not in body  # the engine reads the record from the Mock EHR
        authorization = body["fhirAuthorization"]
        assert authorization["token_type"] == "Bearer" and authorization["expires_in"] == 300
        assert authorization["patient"] == body["context"]["patientId"]
        assert {"access_token", "scope", "subject"} <= set(authorization)
        assert body["context"]["userId"] == f"Practitioner/{ehr_profile.PRESCRIBER_ID}"
        drafts = body["context"]["draftOrders"]["entry"]
        assert drafts
        for entry in drafts:
            order = entry["resource"]
            assert order["status"] == "draft" and order["requester"]["reference"]
            assert entry["fullUrl"].endswith(f"/ServiceRequest/{order['id']}")


def test_the_technical_issues_call_prefetches_only_the_patient_and_its_coverage() -> None:
    """As the kit's own Technical Issues example: the Patient and the active Coverage come in the
    request, under the pack's prefetch keys, so the reads Inferno fails (a 500 to the token it
    sends) are the rest of the record, and the answer can name the Coverage it was about."""
    values = _values(inputs.crd(TODAY))
    body = json.loads(values["technical_issues_request_body"])
    patient = body["context"]["patientId"]
    assert set(body["prefetch"]) == {"patient", "coverage"}
    assert body["prefetch"]["patient"]["id"] == patient
    (entry,) = body["prefetch"]["coverage"]["entry"]
    coverage = entry["resource"]
    assert coverage["status"] == "active"
    assert coverage["beneficiary"]["reference"] == f"Patient/{patient}"
    asked = inputs.resolve_pack(ehr_profile.PRIOR_AUTH).manifest.cds_hooks.prefetch
    assert set(body["prefetch"]) <= set(asked)  # only keys the service asks for


def test_the_mock_ehr_holds_every_resource_the_requests_name_as_the_hook_sends_it() -> None:
    """The kit checks a coverage answer against the order it came from: in the request, or for
    group 4 in the Mock EHR, so the Mock EHR's orders are the drafts the hooks send."""
    values = _values(inputs.crd(TODAY))
    bundle = json.loads(values["mock_ehr_bundle"])
    held = {f"{e['resource']['resourceType']}/{e['resource']['id']}": e for e in bundle["entry"]}
    assert all(
        e["fullUrl"] == f"{ehr_profile.BASE}/{ref}" for ref, e in held.items()
    )  # RESTful, as the kit resolves references
    for body in _bodies(values):
        context = body["context"]
        assert f"Patient/{context['patientId']}" in held and context["userId"] in held
        for entry in context["draftOrders"]["entry"]:
            order = entry["resource"]
            assert held[f"ServiceRequest/{order['id']}"]["resource"] == order
    text = json.dumps(bundle)
    assert "synthetic-case" not in text and "Synthetic" in text  # the profile's, no case id


@pytest.mark.parametrize(
    ("case", "coverage"),
    [
        ("no_member_found", lambda c: c is not None and "subscriberId" not in c),
        ("coverage_not_found", lambda c: c is None),
        ("no_active_coverage", lambda c: c is not None and c["status"] == "cancelled"),
    ],
)
def test_each_specific_coverage_case_has_a_patient_of_its_own(case: str, coverage: Any) -> None:
    values = _values(inputs.crd(TODAY))
    body = json.loads(values[f"{case}_request_body"])
    patient = body["context"]["patientId"]
    assert patient.endswith(f"-{inputs.COVERAGE_CASES[case]}")
    bundle = json.loads(values["mock_ehr_bundle"])
    found = [
        e["resource"]
        for e in bundle["entry"]
        if e["resource"]["resourceType"] == "Coverage"
        and e["resource"]["beneficiary"]["reference"] == f"Patient/{patient}"
    ]
    assert coverage(found[0] if found else None)


def test_smart_registers_the_open_engines_client_launch_url_and_redirect() -> None:
    values = _values(inputs.smart(TODAY))
    engine = _environment("engine")
    public = engine["MENDARI_PUBLIC_URL"]
    assert values["client_id"] == engine["MENDARI_SMART_CLIENT_ID"]
    # Inferno adds iss and launch to the launch URL and nothing else: the engine serves one pack.
    assert values["smart_launch_urls"] == f"{public}/smart/launch"
    assert "," not in engine["MENDARI_AGENTS"]
    assert values["smart_redirect_uris"] == f"{public}/smart/callback"
    patient = json.loads(values["launch_context"])["patient"]
    served = json.loads(values["fhir_read_resources_bundle"])["entry"]
    assert [e["resource"]["id"] for e in served] == [patient] and patient.startswith("ehr-")
    # No `openid fhirUser`: the kit's SMART configuration has no jwks_uri for its id_token.
    assert set(engine["MENDARI_SMART_SCOPES"].split()) == {"launch", "patient/*.rs"}


def test_bulk_asks_for_a_patient_level_export_and_leaves_the_keys_to_the_driver() -> None:
    values = _values(inputs.bulk())
    assert values == {
        "client_id": inputs.BULK_CLIENT_ID,
        "export_type": "Patient",
        "group_id": "1a",  # required by the suite, read only for a Group export
    }
    assert "smart_jwk_set" not in values


def test_the_cli_writes_each_preset(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for suite, suite_id in (
        ("crd", "crd_server_v221"),
        ("smart", "smart_client_stu2_2"),
        ("bulk", "bulk_data_v200_client"),
    ):
        out = tmp_path / f"{suite}.json"
        assert inputs.main([suite, "--out", str(out)]) == 0
        assert json.loads(out.read_text())["test_suite_id"] == suite_id
        assert f"{suite_id} inputs written" in capsys.readouterr().out
