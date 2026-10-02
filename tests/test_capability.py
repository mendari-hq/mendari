"""`GET /fhir/metadata` (standards/capability.py): the statement says what the routes do."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari.server.app import create_app, mounted_routes
from mendari.standards import capability


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(db_path=tmp_path / "c.db"), raise_server_exceptions=False)


def statement(client: TestClient) -> dict[str, Any]:
    response = client.get("/fhir/metadata")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/fhir+json")
    body: dict[str, Any] = response.json()
    return body


def test_every_fhir_route_is_advertised_and_everything_advertised_is_mounted(
    client: TestClient,
) -> None:
    """Both ways, so the statement can neither claim a missing route nor forget a new one."""
    fhir_routes = {
        (method, path)
        for method, path in mounted_routes(client.app)  # type: ignore[arg-type]
        if path.startswith("/fhir/") and path != "/fhir/metadata"
    }
    registry = {(c.method, c.path) for c in capability.CAPABILITIES}
    assert fhir_routes == registry


def test_each_advertised_capability_answers(client: TestClient) -> None:
    rest = statement(client)["rest"][0]
    for operation in rest["operation"]:
        response = client.post(f"/fhir/${operation['name']}", json={"resourceType": "Parameters"})
        assert response.status_code not in (404, 405), operation["name"]
    for resource in rest["resource"]:
        for operation in resource.get("operation", []):
            path = f"/fhir/{resource['type']}/${operation['name']}"
            assert client.post(path, json={"resourceType": "Parameters"}).status_code != 404
        if any(i["code"] == "read" for i in resource.get("interaction", [])):
            # an unknown id is a 404 OperationOutcome from the route, not a missing route
            missing = client.get(f"/fhir/{resource['type']}/no-such-id")
            assert (
                missing.status_code == 404 and missing.json()["resourceType"] == "OperationOutcome"
            )


def test_it_claims_nothing_mendari_does_not_do(client: TestClient) -> None:
    body = statement(client)
    assert body["fhirVersion"] == "4.0.1" and body["format"] == ["json"]
    assert body["kind"] == "instance" and "implementation" in body
    assert "implementationGuide" not in body and "instantiates" not in body
    rest = body["rest"][0]
    assert "service" not in rest["security"]  # API keys and OIDC are neither OAuth nor SMART
    served = {r["type"] for r in rest["resource"]}
    assert served == {"CodeSystem", "NamingSystem", "OperationDefinition", "Questionnaire"}
    assert all(i["code"] == "read" for r in rest["resource"] for i in r.get("interaction", []))
    assert "not a FHIR data server" in body["description"]


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
    monkeypatch.setenv("MENDARI_PUBLIC_URL", "https://mendari.example.org")
    signed = TestClient(create_app(db_path=tmp_path / "in.db"))
    body = statement(signed)  # public: no sign-in needed to discover the server
    assert "Bearer ohk_" in body["rest"][0]["security"]["description"]
    assert body["implementation"]["url"] == "https://mendari.example.org/fhir"
