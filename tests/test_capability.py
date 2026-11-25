# SPDX-License-Identifier: Apache-2.0
"""`GET /fhir/metadata` (standards/capability.py): the statement says what the routes do."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari.core.registry import SearchPath, resolve_pack
from mendari.server.app import create_app, mounted_routes
from mendari.server.backend_services import DISCOVERY_PATH
from mendari.standards import capability, search
from tests.conftest import TRIAGE_DIR, encrypt_mode

TRIAGE = "mendari-test/triage"  # a message in: it takes FHIR
CHART = "mendari-test/chart-summary"  # a patient's record in: it takes FHIR too
CLINICAL = "mendari-test/clinical-review"  # a note as JSON only: no FHIR mapping


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
    """Both ways, so the statement can neither claim a missing route nor forget a new one: in
    open mode, and with sign-in and SMART Backend Services on."""
    if backend_services:
        monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
        monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari-org")
        encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
        monkeypatch.setenv("MENDARI_PUBLIC_URL", "https://mendari.test")
        monkeypatch.setenv("MENDARI_BACKEND_SERVICES", "on")
    app = create_app(db_path=tmp_path / "routes.db")
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


def test_it_claims_nothing_mendari_does_not_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the test fixtures' agents (whatever other agents sit beside them)."""
    monkeypatch.setenv("MENDARI_AGENTS", ",".join((TRIAGE, CHART, CLINICAL)))
    body = statement(TestClient(create_app(db_path=tmp_path / "fixtures.db")))
    assert body["fhirVersion"] == "4.0.1" and body["format"] == ["json"]
    assert body["kind"] == "instance" and "implementation" in body
    assert "implementationGuide" not in body and "instantiates" not in body
    rest = body["rest"][0]
    assert "service" not in rest["security"]  # API keys and OIDC are neither OAuth nor SMART
    served = {r["type"] for r in rest["resource"]}
    assert served == {
        *("CodeSystem", "NamingSystem", "OperationDefinition"),
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
    assert f"{TRIAGE}@" in decide["documentation"]  # the agents served here


def test_date_and_etag_are_stable_and_the_version_follows_the_packs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`date` is when the packs and release went live here (stored), not when the server
    started, so a restart changes nothing and clients' cached copies stay valid."""
    first = TestClient(create_app(db_path=tmp_path / "s.db")).get("/fhir/metadata")
    again = TestClient(create_app(db_path=tmp_path / "s.db")).get("/fhir/metadata")
    assert first.json()["date"] == again.json()["date"]
    assert first.headers["etag"] == again.headers["etag"]
    monkeypatch.setenv("MENDARI_AGENTS", TRIAGE)
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
    served = [resolve_pack(agent_id) for agent_id in (TRIAGE, CHART, CLINICAL)]
    monkeypatch.setenv("MENDARI_AGENTS", ",".join(agent.id for agent in served))
    every = statement(TestClient(create_app(db_path=tmp_path / "a.db")))
    monkeypatch.setenv("MENDARI_AGENTS", CHART)
    fewer = statement(TestClient(create_app(db_path=tmp_path / "b.db")))
    assert {**every, "date": ""} == {**fewer, "date": ""}
    shown = str(every)
    for agent in served:  # neither an agent's id nor its namespace, nor `<id>@<version>`
        namespace = agent.id.split("/")[0]
        assert agent.id not in shown and f"{namespace}/" not in shown, agent.id
        assert f"@{agent.version}" not in shown, agent.id
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
    for packs in ("", TRIAGE, ""):
        monkeypatch.setenv("MENDARI_AGENTS", packs)
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
    assert f"{TRIAGE}@" in decide["documentation"] and f"{CHART}@" in decide["documentation"]
    assert CLINICAL not in decide["documentation"]  # it takes no FHIR
    monkeypatch.setenv("MENDARI_AGENTS", CLINICAL)
    rest = statement(TestClient(create_app(db_path=tmp_path / "g.db")))["rest"][0]
    assert "operation" not in rest  # no agent here takes FHIR, so no $decide is advertised


def test_a_pack_whose_terminology_cannot_be_built_is_reported_not_fatal(tmp_path: Path) -> None:
    """One pack's broken FHIR mapping must not stop the server (it did not before Phase 2)."""
    broken = tmp_path / "pack"
    shutil.copytree(TRIAGE_DIR, broken)
    (broken / "fhir" / "mapping.yaml").write_text("fhir_version: R5\n")
    # A copy of a bundled agent, standing in for it, as a deployment's own agents are served.
    app = create_app(db_path=tmp_path / "b.db", packs_paths=[SearchPath(broken, "maintained")])
    client = TestClient(app)
    invalid = client.get("/api/health").json()["invalid"]
    assert any("terminology" in problem for problem in invalid.values()), invalid
    assert client.get("/fhir/metadata").status_code == 200
