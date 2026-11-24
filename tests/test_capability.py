# SPDX-License-Identifier: Apache-2.0
"""`GET /fhir/metadata` (standards/capability.py): the statement says what the routes do."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari.core.registry import SearchPath
from mendari.server.app import create_app, mounted_routes
from mendari.server.backend_services import DISCOVERY_PATH
from mendari.standards import capability, search
from tests.conftest import encrypt_mode, encrypted_store, evalset_on, evalsets_are_on, signed_in


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(db_path=tmp_path / "c.db"), raise_server_exceptions=False)


def statement(client: TestClient) -> dict[str, Any]:
    response = client.get("/fhir/metadata")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/fhir+json")
    body: dict[str, Any] = response.json()
    return body


# Not FHIR interactions: the statement itself, and SMART's configuration (plain JSON, mounted
# only with Backend Services on; the statement names it through `security.service`).
NOT_ADVERTISED = {("GET", "/fhir/metadata"), ("GET", DISCOVERY_PATH)}


@pytest.mark.parametrize("backend_services", [False, True], ids=["off", "on"])
def test_every_fhir_route_is_advertised_and_everything_advertised_is_mounted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend_services: bool
) -> None:
    """Both ways, so the statement can neither claim a missing route nor forget a new one. With
    sign-in on, hidden evaluation sets are on too, so any FHIR route they mount is counted
    (LP-11g, conflict 37)."""
    if backend_services:
        monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
        monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari-org")
        encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
        monkeypatch.setenv("MENDARI_PUBLIC_URL", "https://mendari.test")
        monkeypatch.setenv("MENDARI_BACKEND_SERVICES", "on")
        evalset_on(monkeypatch)
    app = create_app(db_path=tmp_path / "routes.db")
    if backend_services:
        with encrypted_store(tmp_path / "routes.db") as served:
            reader = signed_in(served, role="viewer")
        evalsets_are_on(TestClient(app, base_url="https://mendari.test"), reader)
    mounted = {(m, p) for m, p in mounted_routes(app) if p.startswith("/fhir/")}
    assert (("GET", DISCOVERY_PATH) in mounted) is backend_services
    registry = {(c.method, c.path) for c in capability.CAPABILITIES}
    assert mounted - NOT_ADVERTISED == registry
    statement = TestClient(app).get("/fhir/metadata").json()
    assert ("service" in statement["rest"][0]["security"]) is backend_services


def test_each_advertised_capability_answers(client: TestClient) -> None:
    rest = statement(client)["rest"][0]
    for operation in rest["operation"]:
        response = client.post(f"/fhir/${operation['name']}", json={"resourceType": "Parameters"})
        assert response.status_code not in (404, 405), operation["name"]
    for resource in rest["resource"]:
        for operation in resource.get("operation", []):
            path = f"/fhir/{resource['type']}/${operation['name']}"
            assert client.post(path, json={"resourceType": "Parameters"}).status_code != 404
        codes = {i["code"] for i in resource.get("interaction", [])}
        if "read" in codes:
            # an unknown id is a 404 OperationOutcome from the route, not a missing route
            missing = client.get(f"/fhir/{resource['type']}/no-such-id")
            assert (
                missing.status_code == 404 and missing.json()["resourceType"] == "OperationOutcome"
            )
        if "vread" in codes:
            missing = client.get(f"/fhir/{resource['type']}/no-such-id/_history/1")
            assert missing.status_code == 404
        if "search-type" in codes:
            found = client.get(f"/fhir/{resource['type']}")
            posted = client.post(
                f"/fhir/{resource['type']}/_search",
                content="_count=1",
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
            for answer in (found, posted):
                assert answer.status_code == 200 and answer.json()["type"] == "searchset"


def test_search_parameters_are_the_ones_searched(client: TestClient) -> None:
    """`searchParam` is generated from the table the parser and the store mapping use, with
    the R4 canonical of each parameter, and only types that are searched list any."""
    rest = statement(client)["rest"][0]
    params = {r["type"]: r.get("searchParam") for r in rest["resource"]}
    for name, spec in search.SPECS.items():
        listed = params[name]
        assert listed == search.search_params(spec)
        assert [p["name"] for p in listed] == [p.name for p in spec.params]
        assert all(p["documentation"] and p["definition"].startswith("http") for p in listed)
    assert params["NamingSystem"] is None and params["OperationDefinition"] is None
    assert "Prefer: handling=lenient" in rest["documentation"]  # the deliberate deviation


def test_only_tasks_have_versions_to_read(client: TestClient) -> None:
    resources = {r["type"]: r for r in statement(client)["rest"][0]["resource"]}
    task = resources["Task"]
    assert [i["code"] for i in task["interaction"]] == ["read", "vread", "search-type"]
    assert task["versioning"] == "versioned" and task["readHistory"] is True
    assert "ADR-0003" in task["documentation"] and "newest first" in task["documentation"]
    for immutable in ("AuditEvent", "Provenance"):
        entry = resources[immutable]
        assert [i["code"] for i in entry["interaction"]] == ["read", "search-type"]
        assert entry["readHistory"] is False and "chain order" in entry["documentation"]
    assert "versioning" not in resources["CodeSystem"]


def test_it_claims_nothing_mendari_does_not_do(client: TestClient) -> None:
    body = statement(client)
    assert body["fhirVersion"] == "4.0.1" and body["format"] == ["json"]
    assert body["kind"] == "instance" and "implementation" in body
    assert "implementationGuide" not in body and "instantiates" not in body
    rest = body["rest"][0]
    assert "service" not in rest["security"]  # API keys and OIDC are neither OAuth nor SMART
    served = {r["type"] for r in rest["resource"]}
    assert served == {
        *("CodeSystem", "NamingSystem", "OperationDefinition", "Questionnaire"),
        *("Task", "AuditEvent", "Provenance"),  # decisions and their audit trail, read-only
    }
    interactions = {i["code"] for r in rest["resource"] for i in r.get("interaction", [])}
    assert interactions == {"read", "vread", "search-type"}  # no writes, no history
    assert "not a FHIR data server" in body["description"]
    for resource in rest["resource"]:
        if "interaction" in resource:
            assert resource["conditionalRead"] == "not-supported"  # no 304 on a derived version
        assert "searchInclude" not in resource and "searchRevInclude" not in resource


def test_operations_name_their_definitions(client: TestClient) -> None:
    rest = statement(client)["rest"][0]
    decide = next(o for o in rest["operation"] if o["name"] == "decide")
    assert decide["definition"].startswith(capability.DECIDE_URL + "|")
    assert "mendari/patient-inbox-router@" in decide["documentation"]  # the packs served here
    questionnaire = next(r for r in rest["resource"] if r["type"] == "Questionnaire")
    assert questionnaire["operation"][0]["definition"] == (
        "http://hl7.org/fhir/us/davinci-dtr/OperationDefinition/questionnaire-package|2.2.0"
    )


def test_dtr_is_advertised_only_with_a_pack_that_offers_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_PACKS", "mendari/patient-inbox-router")
    client = TestClient(create_app(db_path=tmp_path / "one.db"))
    rest = statement(client)["rest"][0]
    assert "Questionnaire" not in {r["type"] for r in rest["resource"]}


def test_date_and_etag_are_stable_and_the_version_follows_the_packs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`date` is when the packs and release went live here (stored), not when the server
    started, so a restart changes nothing and clients' cached copies stay valid."""
    first = TestClient(create_app(db_path=tmp_path / "s.db")).get("/fhir/metadata")
    again = TestClient(create_app(db_path=tmp_path / "s.db")).get("/fhir/metadata")
    assert first.json()["date"] == again.json()["date"]
    assert first.headers["etag"] == again.headers["etag"]
    monkeypatch.setenv("MENDARI_PACKS", "mendari/patient-inbox-router")
    fewer = TestClient(create_app(db_path=tmp_path / "s.db")).get("/fhir/metadata").json()
    assert fewer["version"] != first.json()["version"]


def test_an_unchanged_statement_is_a_304(client: TestClient) -> None:
    etag = client.get("/fhir/metadata").headers["etag"]
    assert client.get("/fhir/metadata", headers={"if-none-match": etag}).status_code == 304
    assert client.get("/fhir/metadata", headers={"if-none-match": 'W/"old"'}).status_code == 200


def test_security_says_what_this_deployment_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    assert "sign-in is off" in statement(client)["rest"][0]["security"]["description"]
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    monkeypatch.setenv("MENDARI_PUBLIC_URL", "https://mendari.example.org")
    signed = TestClient(create_app(db_path=tmp_path / "in.db"))
    body = statement(signed)  # public: no sign-in needed to discover the server
    assert "Bearer ohk_" in body["rest"][0]["security"]["description"]
    assert body["implementation"]["url"] == "https://mendari.example.org/fhir"


def test_with_sign_in_on_the_statement_names_no_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It needs no credential, so with sign-in on it does not say which packs run here (that is
    for signed-in callers, as /api/agents and /api/health are): two servers with other packs but
    the same operations give the same statement, apart from when its version went live here."""
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    every = statement(TestClient(create_app(db_path=tmp_path / "a.db")))
    monkeypatch.setenv("MENDARI_PACKS", "mendari/patient-inbox-router,mendari/prior-auth-preflight")
    fewer = statement(TestClient(create_app(db_path=tmp_path / "b.db")))
    assert {**every, "date": ""} == {**fewer, "date": ""}
    shown = str(every)
    assert "mendari/" not in shown and "@0." not in shown
    decide = next(o for o in every["rest"][0]["operation"] if o["name"] == "decide")
    for said in (decide["documentation"], every["text"]["div"]):
        assert "for signed-in callers, the agents GET /api/agents lists whose" in said
    definition = TestClient(create_app(db_path=tmp_path / "c.db")).get(
        "/fhir/OperationDefinition/decide"
    )
    assert "with sign-in on, it is for signed-in callers" in definition.json()["comment"]


def test_the_date_moves_with_the_version_and_never_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4: `date` changes when `version` does. Serving an earlier pack set again is a new change
    here, so it gets a new date, not the old one."""
    seen = []
    for packs in ("", "mendari/patient-inbox-router", ""):
        monkeypatch.setenv("MENDARI_PACKS", packs)
        body = statement(TestClient(create_app(db_path=tmp_path / "v.db")))
        seen.append((body["version"], body["date"]))
    (first, d1), (second, d2), (third, d3) = seen
    assert first != second and third == first  # the version is the content's
    assert d1 < d2 < d3


def test_any_change_to_the_statement_is_a_new_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    open_mode = statement(client)["version"]
    monkeypatch.setenv("MENDARI_PUBLIC_URL", "https://mendari.example.org")
    with_url = statement(TestClient(create_app(db_path=tmp_path / "u.db")))["version"]
    assert with_url != open_mode  # not only the packs: every part of the statement counts


def test_decide_lists_only_packs_that_take_fhir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    decide = next(o for o in statement(client)["rest"][0]["operation"] if o["name"] == "decide")
    assert "mendari/referral-router@" in decide["documentation"]
    assert "mendari/ai-guard" not in decide["documentation"]  # the guard takes no FHIR
    monkeypatch.setenv("MENDARI_PACKS", "mendari/ai-guard")
    rest = statement(TestClient(create_app(db_path=tmp_path / "g.db")))["rest"][0]
    assert "operation" not in rest  # no pack here takes FHIR, so no $decide is advertised


def test_a_pack_whose_terminology_cannot_be_built_is_reported_not_fatal(tmp_path: Path) -> None:
    """One pack's broken FHIR mapping must not stop the server (it did not before Phase 2)."""
    broken = tmp_path / "pack"
    shutil.copytree(
        Path(__file__).parent / "fixtures" / "reference-agents" / "patient-inbox-router", broken
    )
    (broken / "fhir" / "mapping.yaml").write_text("fhir_version: R5\n")
    # A copy of a maintained pack, standing in for one: `mendari/` is reserved to them.
    app = create_app(db_path=tmp_path / "b.db", packs_paths=[SearchPath(broken, "maintained")])
    client = TestClient(app)
    invalid = client.get("/api/health").json()["invalid"]
    assert any("terminology" in problem for problem in invalid.values()), invalid
    assert client.get("/fhir/metadata").status_code == 200
