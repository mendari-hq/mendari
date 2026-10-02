"""Content negotiation on FHIR routes (R4 http.html): FHIR R4 JSON only, refused otherwise."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mendari.server.app import create_app

INBOX = "/api/packs/mendari/patient-inbox-router"
BUNDLE = (Path(__file__).parent / "fixtures" / "us-core" / "inbox.json").read_text()


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(db_path=tmp_path / "m.db"), raise_server_exceptions=False)


def post(client: TestClient, content_type: str, **headers: str) -> int:
    response = client.post(
        f"{INBOX}/fhir/decide",
        content=BUNDLE,
        headers={"content-type": content_type, **headers},
    )
    if response.status_code >= 400:
        assert response.headers["content-type"].startswith("application/fhir+json")
        assert response.json()["issue"][0]["code"] in ("not-supported", "structure")
    return response.status_code


@pytest.mark.parametrize(
    ("content_type", "status"),
    [
        ("application/fhir+json", 200),
        ("application/json", 200),
        ("application/fhir+json; charset=utf-8; fhirVersion=4.0", 200),
        ("application/fhir+xml", 415),
        ("application/xml", 415),
        ("application/fhir+json; fhirVersion=5.0", 415),
    ],
)
def test_the_body_must_be_fhir_r4_json(client: TestClient, content_type: str, status: int) -> None:
    assert post(client, content_type) == status


@pytest.mark.parametrize(
    ("accept", "status"),
    [
        ("*/*", 200),
        ("application/fhir+json", 200),
        ("application/fhir+xml, application/json;q=0.5", 200),
        ("application/fhir+xml", 406),
        ("text/html", 406),
        ("application/json;q=0", 406),
        ("application/fhir+json; fhirVersion=3.0", 406),
    ],
)
def test_the_answer_must_be_acceptable_as_json(
    client: TestClient, accept: str, status: int
) -> None:
    assert post(client, "application/fhir+json", accept=accept) == status


@pytest.mark.parametrize(("value", "status"), [("json", 200), ("xml", 406), ("text/xml", 406)])
def test_format_parameter(client: TestClient, value: str, status: int) -> None:
    response = client.post(
        f"{INBOX}/fhir/decide",
        params={"_format": value},
        content=BUNDLE,
        headers={"content-type": "application/fhir+json"},
    )
    assert response.status_code == status


def test_mendari_api_routes_are_not_negotiated(client: TestClient) -> None:
    """Only FHIR routes are bound by FHIR's rules; the JSON API answers as before."""
    body = json.dumps({"message": {"body": "Can I pay my bill online?"}})
    response = client.post(
        f"{INBOX}/decide",
        content=body,
        headers={"content-type": "application/json", "accept": "text/html"},
    )
    assert response.status_code == 200
