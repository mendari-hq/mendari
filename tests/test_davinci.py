# SPDX-License-Identifier: Apache-2.0
"""Da Vinci CRD on the Prior Authorization Preflight's order-sign service (standards/davinci.py).
The HL7 validator checks the same resources against the CRD guide in the CI job
`fhir-conformance` (scripts/davinci_sample.py)."""

from __future__ import annotations

import copy
import json
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
    lab = _order("lab", "FX904")  # a metabolic panel: not the pack's business
    response = cds_hooks.invoke(
        preflight, request("acme", COMPLETE, _order("mri", "FX101"), _order("scope", "FX202"), lab)
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
    original = _order("mri", "FX101")
    updated = {k: v for k, v in actions[0]["resource"].items() if k != "extension"}
    assert updated == original  # the order itself is unchanged


def test_missing_documentation_says_so(preflight: Decision) -> None:
    response = cds_hooks.invoke(
        preflight, request("acme", "Back pain for two weeks.", _order("mri", "FX101"))
    )
    parts = coverage_info(response["systemActions"][0])
    assert parts["doc-needed"] == ["clinical"] and parts["pa-needed"] == ["auth-needed"]
    assert "missing from the record" in parts["reason"][0]["text"]


def test_a_service_without_prior_auth_and_an_unknown_payer(preflight: Decision) -> None:
    response = cds_hooks.invoke(
        preflight, request("beacon", "Loud snoring.", _order("psg", "FX301"))
    )
    parts = coverage_info(response["systemActions"][0])
    assert parts["covered"] == ["covered"] and parts["pa-needed"] == ["no-auth"]
    unknown = cds_hooks.invoke(preflight, request("zenith", COMPLETE, _order("mri", "FX101")))
    assert "systemActions" not in unknown  # not_checked asserts nothing


def test_without_the_patients_coverage_nothing_is_asserted(preflight: Decision) -> None:
    body = request("acme", COMPLETE, _order("mri", "FX101"))
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


def test_the_coverage_is_the_searchs_first_coverage_whatever_comes_before_it() -> None:
    """A search page may open with an `outcome` entry (an OperationOutcome warning, as
    Mendari's own searches send) or an included resource: only its first entry was looked at,
    so the order had no coverage information at all."""
    coverage = {"resourceType": "Coverage", "id": "c9"}
    warning = {"resourceType": "OperationOutcome", "issue": []}
    bundle = {
        "resourceType": "Bundle",
        "entry": [
            {"resource": warning},
            {"resource": {"resourceType": "Patient"}},
            {"resource": coverage},
        ],
    }
    assert davinci.coverage_reference({"coverage": bundle}) == {"reference": "Coverage/c9"}
    nameless = {"resourceType": "Bundle", "entry": [{"resource": {"resourceType": "Coverage"}}]}
    assert davinci.coverage_reference({"coverage": nameless}) is None
    unnamed = {"resourceType": "Coverage", "id": "../Patient/p1"}  # not a FHIR id
    assert davinci.coverage_reference({"coverage": unnamed}) is None


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
    missing = cds_hooks.invoke(preflight, request("acme", "Back pain.", _order("mri", "FX101")))
    [link] = coverage_info(missing["systemActions"][0])["questionnaire"]
    assert link == (
        f"https://mendari.co/packs/{PA}/Questionnaire/lumbar_mri-acme|{preflight.pack.version}"
    )
    ready = cds_hooks.invoke(preflight, request("acme", COMPLETE, _order("mri", "FX101")))
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
        json=request("acme", "Back pain for two weeks.", _order("mri", "FX101")),
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
    [issue] = outcome(nothing)  # no documentation there, so no such questionnaire (oper-9)
    assert (issue["severity"], issue["code"]) == ("warning", "not-found")
    assert "needs no documentation" in issue["diagnostics"]


# -- DTR errors (2.2.0: oper-6, oper-8, oper-9, spec-130) ------------------------------------------

PACKAGE = "/fhir/Questionnaire/$questionnaire-package"
ELSEWHERE = "https://inferno.health/Questionnaire/does-not-exist"  # another server's


def outcome(answer: dict[str, Any]) -> list[dict[str, Any]]:
    """The issues of a successful answer's `outcome`, which comes after its packages."""
    assert answer["resourceType"] == "Parameters"
    *packages, last = answer["parameter"]
    assert last["name"] == "outcome" and {p["name"] for p in packages} <= {"packagebundle"}
    assert last["resource"]["resourceType"] == "OperationOutcome"
    issues: list[dict[str, Any]] = last["resource"]["issue"]
    return issues


def signed_order(client: Any, payer: str, note: str, order: dict[str, Any]) -> dict[str, Any]:
    """The order as CRD updated it (with the coverage-assertion-id, the decision id)."""
    hook = client.post(f"/cds-services/{PA.replace('/', '.')}", json=request(payer, note, order))
    assert hook.status_code == 200, hook.text
    updated: dict[str, Any] = hook.json()["systemActions"][0]["resource"]
    return updated


def refusal(answer: Any) -> dict[str, Any]:
    """A DTR source data error: a 4xx OperationOutcome, as Inferno's 3.6.02 asks."""
    assert answer.status_code == 422, answer.text
    assert answer.headers["content-type"].startswith("application/fhir+json")
    body: dict[str, Any] = answer.json()
    assert body["resourceType"] == "OperationOutcome"
    [issue] = body["issue"]
    assert (issue["severity"], issue["code"]) == ("error", "not-found")
    return body


@pytest.fixture
def client(tmp_path: Any) -> Any:
    from fastapi.testclient import TestClient

    from mendari.server.app import create_app

    return TestClient(create_app(db_path=tmp_path / "dtr.db"))


def test_an_unknown_questionnaire_next_to_an_order_is_a_warning_beside_its_package(
    client: Any,
) -> None:
    """oper-9 whether or not a package is built: Inferno's 3.6.01 adds a questionnaire that
    cannot be found to a request that worked, and looks for a warning."""
    order = signed_order(client, "acme", "Back pain for two weeks.", _order("mri", "FX101"))
    plain = client.post(PACKAGE, json=package_request({"name": "order", "resource": order}))
    assert [p["name"] for p in plain.json()["parameter"]] == ["packagebundle"]  # as before
    answer = client.post(
        PACKAGE,
        json=package_request(
            {"name": "order", "resource": order},
            {"name": "questionnaire", "valueCanonical": ELSEWHERE},
        ),
    )
    assert answer.status_code == 200, answer.text
    [package] = [p["resource"] for p in answer.json()["parameter"] if p["name"] == "packagebundle"]
    form = package["entry"][0]["resource"]  # the order's questionnaire, as without it
    assert form["url"].endswith("/Questionnaire/lumbar_mri-acme")
    [issue] = outcome(answer.json())
    assert (issue["severity"], issue["code"]) == ("warning", "not-found")
    assert issue["diagnostics"] == f"questionnaire not found: {ELSEWHERE}"


def test_a_questionnaire_not_found_alone_is_a_warning_and_no_package() -> None:
    pack = resolve_pack(PA)
    known = davinci.questionnaire_url(pack, {"service": "sleep_study", "payer": "acme"})
    made_up = f"https://mendari.co/packs/{PA}/Questionnaire/foo-bar"  # not the pack's labels
    no_pack = "https://mendari.co/packs/mendari/nope/Questionnaire/x"
    for canonical in (ELSEWHERE, made_up, no_pack):
        answer = davinci.questionnaire_package(
            package_request({"name": "questionnaire", "valueCanonical": canonical}),
            lambda _: None,
            {PA: pack},
            "2026-10-17T00:00:00+00:00",
        )
        [issue] = outcome(answer)
        assert issue == {
            "severity": "warning",
            "code": "not-found",
            "diagnostics": f"questionnaire not found: {canonical}",
        }
    both = davinci.questionnaire_package(
        package_request(
            {"name": "questionnaire", "valueCanonical": known},
            {"name": "questionnaire", "valueCanonical": made_up},
        ),
        lambda _: None,
        {PA: pack},
        "2026-10-17T00:00:00+00:00",
    )
    assert [p["name"] for p in both["parameter"]] == ["packagebundle", "outcome"]
    assert [i["severity"] for i in outcome(both)] == ["warning"]


def test_a_context_or_an_order_that_names_no_decision_is_a_422(client: Any) -> None:
    """spec-130 (Inferno's 3.6.02): source data that cannot be resolved is a 4xx with an
    OperationOutcome. A 422, not a 404: the operation exists and the request is well formed, its
    content names nothing here, as the route's missing Coverage is a 422 too. The answer never
    repeats the id."""
    unknown = refusal(
        client.post(PACKAGE, json=package_request({"name": "context", "valueString": "nope-42"}))
    )
    assert "nope-42" not in json.dumps(unknown)
    order = signed_order(client, "acme", "Back pain for two weeks.", _order("mri", "FX101"))
    decision_id = davinci.assertion_id(order)
    assert decision_id is not None
    forged = json.loads(json.dumps(order).replace(decision_id, "0" * len(decision_id)))
    unsigned = _order("mri", "FX101")  # no coverage information: never signed through CRD
    for orders in ([forged], [unsigned], [forged, unsigned]):
        given = [{"name": "order", "resource": o} for o in orders]
        assert refusal(client.post(PACKAGE, json=package_request(*given))) == unknown
    with_elsewhere = package_request(
        {"name": "context", "valueString": "nope-42"},
        {"name": "questionnaire", "valueCanonical": ELSEWHERE},
    )
    assert refusal(client.post(PACKAGE, json=with_elsewhere)) == unknown  # still no package


def undecided(source: str, index: int) -> dict[str, Any]:
    """The warning for an order, or the context, that names no decision of the caller's: the
    parameter by its place in the request, and no id."""
    return {
        "severity": "warning",
        "code": "not-found",
        "diagnostics": f"no Mendari decision for this {source}: {davinci.SIGN_FIRST}",
        "expression": [f"Parameters.parameter[{index}]"],
    }


def test_an_order_or_a_context_that_names_no_decision_beside_a_package_is_a_warning(
    client: Any,
) -> None:
    """DTR, after oper-9: an order it cannot understand is a warning, with packages too. The
    warning names the parameter, not the id, so an unknown id and another organization's read
    the same (tests/test_two_organizations.py)."""
    order = signed_order(client, "acme", "Back pain for two weeks.", _order("mri", "FX101"))
    decision_id = davinci.assertion_id(order)
    assert decision_id is not None
    forged = json.loads(json.dumps(order).replace(decision_id, "0" * len(decision_id)))
    unsigned = _order("scope", "FX202")  # no coverage information: never signed through CRD
    for other in (forged, unsigned):
        answer = client.post(
            PACKAGE,
            json=package_request(
                {"name": "order", "resource": order}, {"name": "order", "resource": other}
            ),
        )
        assert answer.status_code == 200, answer.text
        [package] = [
            p["resource"] for p in answer.json()["parameter"] if p["name"] == "packagebundle"
        ]
        assert package["entry"][0]["resource"]["url"].endswith("/Questionnaire/lumbar_mri-acme")
        assert outcome(answer.json()) == [undecided("order", 2)]
    pack = resolve_pack(PA)
    known = davinci.questionnaire_url(pack, {"service": "sleep_study", "payer": "acme"})
    asked = package_request(
        {"name": "context", "valueString": "nope-42"},
        {"name": "questionnaire", "valueCanonical": known},
    )
    answered = client.post(PACKAGE, json=asked)  # the canonical's package, and the warning
    assert answered.status_code == 200, answered.text
    assert [p["name"] for p in answered.json()["parameter"]] == ["packagebundle", "outcome"]
    assert outcome(answered.json()) == [undecided("context", 1)]
    assert "nope-42" not in answered.text


def crd_link(order: dict[str, Any]) -> str:
    """The questionnaire CRD linked on the order it updated (`url|version`)."""
    [link] = coverage_info({"resource": order})["questionnaire"]
    return str(link)


def test_the_packages_are_the_union_of_the_orders_and_the_questionnaires_asked_for(
    client: Any,
) -> None:
    """DTR's OperationDefinition: the union of what each parameter names, each Questionnaire
    version once. The order's own questionnaire, with its version or without, is the order's
    pre-answered package; another comes unanswered beside it."""
    order = signed_order(client, "acme", "Back pain for two weeks.", _order("mri", "FX101"))
    linked = crd_link(order)
    pack = resolve_pack(PA)
    sleep = davinci.questionnaire_url(pack, {"service": "sleep_study", "payer": "acme"})
    canonicals = (linked, linked.partition("|")[0], sleep, f"{sleep}|{pack.version}")
    answer = client.post(
        PACKAGE,
        json=package_request(
            {"name": "order", "resource": order},
            *({"name": "questionnaire", "valueCanonical": c} for c in canonicals),
        ),
    )
    assert answer.status_code == 200, answer.text
    parameters = answer.json()["parameter"]
    assert [p["name"] for p in parameters] == ["packagebundle", "packagebundle"]  # no outcome
    mri, sleep_study = ([e["resource"] for e in p["resource"]["entry"]] for p in parameters)
    assert f"{mri[0]['url']}|{mri[0]['version']}" == linked
    assert {i["linkId"]: i["answer"][0]["valueCoding"]["code"] for i in mri[1]["item"]} == {
        "diagnosis_supports": "yes",
        "conservative_therapy": "no",  # pre-answered from the order's decision
        "prior_imaging": "yes",
    }
    assert sleep_study[0]["url"] == sleep and "item" not in sleep_study[1]


def test_a_questionnaire_version_not_served_is_not_found(client: Any) -> None:
    """oper-9 for a version-specific canonical: only the pack's version is served, so another
    is not found, alone or beside an order's package, and never answered with the current one.
    A canonical without a version asks for the current one."""
    pack = resolve_pack(PA)
    url = davinci.questionnaire_url(pack, {"service": "sleep_study", "payer": "acme"})

    def ask(canonical: str) -> dict[str, Any]:
        return davinci.questionnaire_package(
            package_request({"name": "questionnaire", "valueCanonical": canonical}),
            lambda _: None,
            {PA: pack},
            "2026-10-17T00:00:00+00:00",
        )

    for canonical in (url, f"{url}|{pack.version}"):
        [bundle] = [p["resource"] for p in ask(canonical)["parameter"]]
        assert bundle["entry"][0]["resource"]["version"] == pack.version
    for version in ("9.9.9", "0.0.0-not-a-version"):
        answer = ask(f"{url}|{version}")
        assert [p["name"] for p in answer["parameter"]] == ["outcome"]
        assert outcome(answer) == [
            {
                "severity": "warning",
                "code": "not-found",
                "diagnostics": f"questionnaire not found: {url}|{version}",
            }
        ]
    order = signed_order(client, "acme", "Back pain for two weeks.", _order("mri", "FX101"))
    stale = f"{crd_link(order).partition('|')[0]}|9.9.9"
    answer = client.post(
        PACKAGE,
        json=package_request(
            {"name": "order", "resource": order},
            {"name": "questionnaire", "valueCanonical": stale},
        ),
    )
    assert answer.status_code == 200, answer.text
    assert [p["name"] for p in answer.json()["parameter"]] == ["packagebundle", "outcome"]
    [issue] = outcome(answer.json())
    assert issue["diagnostics"] == f"questionnaire not found: {stale}"


def test_a_decided_order_that_needs_no_documentation_is_a_note(client: Any) -> None:
    """oper-8: the order was decided, but the payer asks for no documentation for it (no
    authorization needed): no package, and an information note that says so, never a 4xx. An
    order beside it that the pack did not decide is a warning."""
    order = signed_order(client, "beacon", "Loud snoring.", _order("psg", "FX301"))
    unsigned = _order("lab", "FX904")
    answer = client.post(
        PACKAGE,
        json=package_request(
            {"name": "order", "resource": order}, {"name": "order", "resource": unsigned}
        ),
    )
    assert answer.status_code == 200, answer.text
    assert outcome(answer.json()) == [
        {
            "severity": "information",
            "code": "informational",
            "diagnostics": "the payer needs no documentation for this order",
        },
        undecided("order", 2),
    ]
    asks_nothing = client.post(PACKAGE, json=package_request())
    assert asks_nothing.status_code == 400  # oper-6: an order, a context or a questionnaire
    assert asks_nothing.json()["issue"][0]["code"] == "required"
