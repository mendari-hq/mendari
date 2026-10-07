# SPDX-License-Identifier: Apache-2.0
"""Da Vinci CRD on the Prior Authorization Preflight's order-sign service (standards/davinci.py).
The HL7 validator checks the same resources against the CRD guide in the CI job
`fhir-conformance` (scripts/davinci_sample.py)."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from mendari.core.decision import Decision
from mendari.core.pack import Pack
from mendari.core.registry import resolve_pack
from mendari.core.spec import cross_reference_problems
from mendari.standards import cds_hooks, davinci
from tests.test_prior_auth_pack import _order, _prefetch

PA = "mendari/prior-auth-preflight"
COMPLETE = "Completed 8 weeks of physical therapy. The knee locks when climbing stairs."


@pytest.fixture(scope="module")
def preflight() -> Decision:
    return Decision.from_hub(PA)


def request(payer: str, note: str, *orders: dict[str, Any]) -> dict[str, Any]:
    return {
        "hook": "order-sign",
        "hookInstance": "d1577c69-dfbe-44ad-ba6d-3e05e953b2ea",
        "context": {
            "userId": "Practitioner/1",
            "patientId": "p1",
            "draftOrders": {"resourceType": "Bundle", "entry": [{"resource": o} for o in orders]},
        },
        "prefetch": _prefetch(payer, note),
    }


def coverage_info(action: dict[str, Any]) -> dict[str, list[Any]]:
    [extension] = [
        e for e in action["resource"]["extension"] if e["url"] == davinci.COVERAGE_INFORMATION
    ]
    parts: dict[str, list[Any]] = {}
    for part in extension["extension"]:
        value = next(v for k, v in part.items() if k.startswith("value"))
        parts.setdefault(part["url"], []).append(value)
    return parts


def test_each_decided_order_comes_back_with_coverage_information(preflight: Decision) -> None:
    lab = _order("lab", "80053")  # a metabolic panel: not the pack's business
    response = cds_hooks.invoke(
        preflight, request("acme", COMPLETE, _order("mri", "72148"), _order("scope", "29881"), lab)
    )
    assert len(response["cards"]) == 2  # the cards are as before
    actions = response["systemActions"]
    assert [a["type"] for a in actions] == ["update", "update"]
    assert [a["resource"]["id"] for a in actions] == ["mri", "scope"]
    parts = coverage_info(actions[0])
    assert parts["coverage"] == [{"reference": "Coverage/c1"}]
    assert parts["covered"] == ["covered"] and parts["pa-needed"] == ["auth-needed"]
    assert "doc-needed" not in parts
    decision_id = response["cards"][0]["extension"]["mendari.decision_id"]
    assert parts["coverage-assertion-id"] == [decision_id]  # traceable to the audit record
    original = _order("mri", "72148")
    updated = {k: v for k, v in actions[0]["resource"].items() if k != "extension"}
    assert updated == original  # the order itself is unchanged


def test_missing_documentation_says_so(preflight: Decision) -> None:
    response = cds_hooks.invoke(
        preflight, request("acme", "Back pain for two weeks.", _order("mri", "72148"))
    )
    parts = coverage_info(response["systemActions"][0])
    assert parts["doc-needed"] == ["clinical"] and parts["pa-needed"] == ["auth-needed"]
    assert "missing from the record" in parts["reason"][0]["text"]


def test_a_service_without_prior_auth_and_an_unknown_payer(preflight: Decision) -> None:
    response = cds_hooks.invoke(
        preflight, request("beacon", "Loud snoring.", _order("psg", "95810"))
    )
    parts = coverage_info(response["systemActions"][0])
    assert parts["covered"] == ["covered"] and parts["pa-needed"] == ["no-auth"]
    unknown = cds_hooks.invoke(preflight, request("zenith", COMPLETE, _order("mri", "72148")))
    assert "systemActions" not in unknown  # not_checked asserts nothing


def test_without_the_patients_coverage_nothing_is_asserted(preflight: Decision) -> None:
    body = request("acme", COMPLETE, _order("mri", "72148"))
    body["prefetch"]["coverage"] = {"resourceType": "Bundle", "type": "searchset", "entry": []}
    response = cds_hooks.invoke(preflight, body)
    assert "systemActions" not in response  # no Coverage to assert anything about


def test_the_coverage_reference_comes_from_the_prefetch() -> None:
    coverage = {"resourceType": "Coverage", "id": "c9"}
    bundle = {"resourceType": "Bundle", "entry": [{"resource": coverage}]}
    assert davinci.coverage_reference({"coverage": coverage}) == {"reference": "Coverage/c9"}
    assert davinci.coverage_reference({"coverage": bundle}) == {"reference": "Coverage/c9"}
    assert davinci.coverage_reference({"coverage": {"resourceType": "Coverage"}}) is None
    assert davinci.coverage_reference({}) is None


def test_a_bad_coverage_mapping_fails_validation() -> None:
    pack: Pack = resolve_pack(PA)
    manifest = copy.deepcopy(pack.manifest)
    hooks = manifest.cds_hooks
    assert hooks is not None
    bad = hooks.model_copy(
        update={
            "hook": "patient-view",
            "prefetch": {"patient": "Patient/{{context.patientId}}"},
            "coverage_information": {
                "approved": hooks.coverage_information["submit_ready"],
            },
        }
    )
    problems = cross_reference_problems(manifest.model_copy(update={"cds_hooks": bad}))
    assert any("unknown labels ['approved']" in p for p in problems)
    assert any("order-select or order-sign" in p for p in problems)
    assert any("needs a `coverage` prefetch" in p for p in problems)


# -- DTR -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("service", "payer", "needed"),
    [
        ("lumbar_mri", "acme", ["diagnosis_supports", "conservative_therapy", "prior_imaging"]),
        ("lumbar_mri", "beacon", ["diagnosis_supports", "conservative_therapy"]),
        (
            "knee_arthroscopy",
            "beacon",
            ["diagnosis_supports", "conservative_therapy", "mechanical_symptoms"],
        ),
        ("sleep_study", "acme", ["diagnosis_supports", "sleep_symptoms"]),
        ("sleep_study", "beacon", []),  # no authorization needed, so no documentation
        ("other", "acme", []),
    ],
)
def test_the_documentation_asked_for_follows_the_decision_table(
    service: str, payer: str, needed: list[str]
) -> None:
    pack = resolve_pack(PA)
    assert davinci.requirements(pack, {"service": service, "payer": payer}) == needed


def test_crd_links_the_questionnaire_only_when_documentation_is_missing(
    preflight: Decision,
) -> None:
    missing = cds_hooks.invoke(preflight, request("acme", "Back pain.", _order("mri", "72148")))
    [link] = coverage_info(missing["systemActions"][0])["questionnaire"]
    assert link == (
        f"https://mendari.co/packs/{PA}/Questionnaire/lumbar_mri-acme|{preflight.pack.version}"
    )
    ready = cds_hooks.invoke(preflight, request("acme", COMPLETE, _order("mri", "72148")))
    assert "questionnaire" not in coverage_info(ready["systemActions"][0])


def package_request(*extra: dict[str, Any]) -> dict[str, Any]:
    coverage = _prefetch("acme", "")["coverage"]["entry"][0]["resource"]
    return {
        "resourceType": "Parameters",
        "parameter": [{"name": "coverage", "resource": coverage}, *extra],
    }


def test_the_questionnaire_package_is_pre_answered_from_the_decision(tmp_path: Any) -> None:
    from fastapi.testclient import TestClient

    from mendari.server.app import create_app

    client = TestClient(create_app(db_path=tmp_path / "a.db"))
    signed = client.post(
        f"/cds-services/{PA.replace('/', '.')}",
        json=request("acme", "Back pain for two weeks.", _order("mri", "72148")),
    ).json()
    order = signed["systemActions"][0]["resource"]
    answer = client.post(
        "/fhir/Questionnaire/$questionnaire-package",
        json=package_request({"name": "order", "resource": order}),
    )
    assert answer.status_code == 200 and answer.headers["content-type"].startswith(
        "application/fhir+json"
    )
    [bundle] = [p["resource"] for p in answer.json()["parameter"] if p["name"] == "packagebundle"]
    form, response = (e["resource"] for e in bundle["entry"])
    assert [i["linkId"] for i in form["item"]] == [
        "diagnosis_supports",
        "conservative_therapy",
        "prior_imaging",
    ]
    assert {i["linkId"]: i["answer"][0]["valueCoding"]["code"] for i in response["item"]} == {
        "diagnosis_supports": "yes",
        "conservative_therapy": "no",  # the gap the clinician documents
        "prior_imaging": "yes",
    }
    assert response["subject"] == {"reference": "Patient/p1"}
    assert response["questionnaire"] == f"{form['url']}|{form['version']}"

    unknown = client.post(
        "/fhir/Questionnaire/$questionnaire-package",
        json=package_request({"name": "context", "valueString": "not-a-decision"}),
    ).json()
    assert [p["name"] for p in unknown["parameter"]] == ["outcome"]
    assert (
        client.post(
            "/fhir/Questionnaire/$questionnaire-package", json={"resourceType": "Parameters"}
        ).status_code
        == 422
    )


def test_a_questionnaire_asked_for_by_canonical_comes_unanswered() -> None:
    pack = resolve_pack(PA)
    url = davinci.questionnaire_url(pack, {"service": "sleep_study", "payer": "acme"})
    answer = davinci.questionnaire_package(
        package_request({"name": "questionnaire", "valueCanonical": url}),
        lambda _: None,
        {PA: pack},
        "2026-09-30T00:00:00+00:00",
    )
    [bundle] = [p["resource"] for p in answer["parameter"]]
    form, response = (e["resource"] for e in bundle["entry"])
    assert [i["linkId"] for i in form["item"]] == ["diagnosis_supports", "sleep_symptoms"]
    assert "item" not in response
    nothing = davinci.questionnaire_package(
        package_request(
            {"name": "questionnaire", "valueCanonical": url.replace("-acme", "-beacon")}
        ),
        lambda _: None,
        {PA: pack},
        "2026-09-30T00:00:00+00:00",
    )
    assert [p["name"] for p in nothing["parameter"]] == ["outcome"]
