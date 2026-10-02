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


FORM = "application/x-www-form-urlencoded"


@pytest.mark.parametrize(
    ("path", "content_type", "status"),
    [
        ("/fhir/Task/_search", FORM, 200),
        ("/fhir/Task/_search", f"{FORM}; charset=utf-8", 200),
        ("/fhir/AuditEvent/_search", FORM, 200),
        ("/fhir/Task/_search", "application/fhir+json", 415),  # a search's body is a form
        ("/fhir/Task/_search", "multipart/form-data; boundary=x", 415),
        (f"{INBOX}/fhir/decide", FORM, 415),  # a form only where a search reads one
        ("/fhir/$decide", FORM, 415),
    ],
)
def test_a_form_body_is_accepted_only_by_search(
    client: TestClient, path: str, content_type: str, status: int
) -> None:
    response = client.post(path, content="_count=1", headers={"content-type": content_type})
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/fhir+json")


@pytest.mark.parametrize(
    ("url", "body", "headers", "status"),
    [
        ("/fhir/Task/_search", "_count=1", {"accept": "text/html"}, 406),
        ("/fhir/Task/_search?_format=xml", "_count=1", {}, 406),
        ("/fhir/Task/_search", "_count=1&_format=xml", {}, 406),  # in the form, too
        ("/fhir/Task/_search", "_count=1&_format=json&_pretty=true", {}, 200),
        ("/fhir/Task?_format=application/fhir%2Bxml", None, {}, 406),
        ("/fhir/Task?_format=json", None, {"accept": "application/fhir+json"}, 200),
    ],
)
def test_a_search_is_negotiated_like_any_fhir_request(
    client: TestClient, url: str, body: str | None, headers: dict[str, str], status: int
) -> None:
    if body is None:
        response = client.get(url, headers=headers)
    else:
        response = client.post(url, content=body, headers={"content-type": FORM, **headers})
    assert response.status_code == status
    if status == 200:  # presentation parameters are not criteria: nothing was ignored
        assert "entry" not in response.json()
