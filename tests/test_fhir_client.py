# SPDX-License-Identifier: Apache-2.0
"""Outbound FHIR reads under the allow-list (standards/fhir_client.py) and CDS Hooks fetch."""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from mendari.errors import FhirError, MendariError
from mendari.server.app import create_app
from mendari.standards import cds_hooks, fhir_client
from mendari.standards.fhir_client import FhirClient, allowed_servers

EHR = "https://ehr.example.org/fhir"
TOKEN = "secret-token-value"
FIXTURES = Path(__file__).parent / "fixtures" / "us-core"

Handler = Callable[[httpx.Request], httpx.Response]


def fhir_json(body: Any, status: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(
        status, json=body, headers={"content-type": "application/fhir+json", **headers}
    )


def client(handler: Handler, base: str = EHR, **kwargs: Any) -> FhirClient:
    return FhirClient(
        base, allowed={"https://ehr.example.org"}, transport=httpx.MockTransport(handler), **kwargs
    )


# -- the allow-list ----------------------------------------------------------------------------


def test_the_allow_list_takes_origins_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "MENDARI_FHIR_ALLOWED_SERVERS",
        "https://ehr.example.org, http://fhir:8080,http://localhost:8080",
    )
    assert allowed_servers() == {
        "https://ehr.example.org",
        "http://fhir:8080",
        "http://localhost:8080",
    }
    monkeypatch.delenv("MENDARI_FHIR_ALLOWED_SERVERS")
    assert allowed_servers() == frozenset()  # default: nothing is fetched


@pytest.mark.parametrize(
    "value",
    [
        "https://ehr.example.org/fhir",
        "ehr.example.org",
        "http://ehr.example.org",
        "https://u:p@ehr.example.org",
    ],
)
def test_the_allow_list_rejects_anything_but_a_safe_origin(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", value)
    with pytest.raises(MendariError, match="MENDARI_FHIR_ALLOWED_SERVERS"):
        allowed_servers()


def test_a_server_off_the_list_is_never_called() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return fhir_json({})

    transport = httpx.MockTransport(handler)
    with pytest.raises(FhirError, match="allow-list"):
        FhirClient(
            "https://other.example.org/fhir",
            allowed={"https://ehr.example.org"},
            transport=transport,
        )
    assert calls == []


# -- reads -------------------------------------------------------------------------------------


def test_a_read_sends_the_token_and_returns_the_resource() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return fhir_json({"resourceType": "Patient", "id": "p1"})

    with client(handler, token=TOKEN) as fhir:
        assert fhir.read("Patient/p1") == {"resourceType": "Patient", "id": "p1"}
    assert str(seen[0].url) == f"{EHR}/Patient/p1"
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"


def test_a_search_follows_next_links_on_the_same_server() -> None:
    first = {
        "resourceType": "Bundle",
        "entry": [{"resource": {"id": "1"}}],
        "link": [{"relation": "next", "url": f"{EHR}/Observation?page=2"}],
    }
    second = {"resourceType": "Bundle", "entry": [{"resource": {"id": "2"}}]}
    pages = {f"{EHR}/Observation?patient=p1": first, f"{EHR}/Observation?page=2": second}
    with client(lambda r: fhir_json(pages[str(r.url)])) as fhir:
        bundle = fhir.read("Observation?patient=p1")
    assert [e["resource"]["id"] for e in bundle["entry"]] == ["1", "2"]


def test_paging_stops_after_the_page_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fhir_client, "MAX_PAGES", 2)
    again = [{"relation": "next", "url": f"{EHR}/Observation?again"}]
    endless = {"resourceType": "Bundle", "entry": [{"resource": {}}], "link": again}
    with client(lambda r: fhir_json(endless)) as fhir:
        assert len(fhir.read("Observation")["entry"]) == 3


@pytest.mark.parametrize(
    "target",
    [
        "../admin",
        "/other/Patient",
        "https://evil.example/fhir/Patient",
        "https://ehr.example.org/fhirx/Patient",
    ],
)
def test_requests_cannot_leave_the_base_url(target: str) -> None:
    with client(lambda r: fhir_json({})) as fhir, pytest.raises(FhirError, match="left the server"):
        fhir.read(target)


def test_a_next_link_to_another_server_is_refused() -> None:
    page = {
        "resourceType": "Bundle",
        "link": [{"relation": "next", "url": "https://evil.example/fhir/x"}],
    }
    with (
        client(lambda r: fhir_json(page)) as fhir,
        pytest.raises(FhirError, match="left the server"),
    ):
        fhir.read("Observation")


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(302, headers={"location": "https://evil.example/"}), "redirected"),
        (httpx.Response(401, text=f"bad token {TOKEN}"), "HTTP 401"),
        (
            httpx.Response(200, text="<html>", headers={"content-type": "text/html"}),
            "not answer with JSON",
        ),
        (
            httpx.Response(200, text="{", headers={"content-type": "application/json"}),
            "invalid JSON",
        ),
        (
            httpx.Response(200, text="[1]", headers={"content-type": "application/json"}),
            "other than a JSON object",
        ),
    ],
)
def test_failures_fail_closed_without_echoing_the_server(
    response: httpx.Response, message: str
) -> None:
    with (
        client(lambda r: response, token=TOKEN) as fhir,
        pytest.raises(FhirError, match=message) as error,
    ):
        fhir.read("Patient/p1")
    assert TOKEN not in str(error.value)


def test_a_response_over_the_size_cap_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fhir_client, "MAX_BYTES", 100)
    big = {"resourceType": "Patient", "text": "x" * 200}
    with client(lambda r: fhir_json(big)) as fhir, pytest.raises(FhirError, match="larger than"):
        fhir.read("Patient/p1")


def test_a_network_error_says_what_failed() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with client(fail) as fhir, pytest.raises(FhirError, match="could not reach the server"):
        fhir.read("Patient/p1")


# -- CDS Hooks fetch ---------------------------------------------------------------------------


def referral_ehr() -> tuple[dict[str, Any], dict[str, Any], Handler, list[httpx.Request]]:
    """The referral fixture split into an order-sign request (the referral as a draft order, no
    prefetch) and a FHIR server that answers the pack's prefetch queries from the fixture."""
    bundle = json.loads((FIXTURES / "referral.json").read_text())
    resources = [e["resource"] for e in bundle["entry"]]
    order = next(r for r in resources if r["resourceType"] == "ServiceRequest")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        kind = (
            request.url.path.rsplit("/", 2)[-1] if "Patient/" not in str(request.url) else "Patient"
        )
        if kind == "Patient":
            return fhir_json(next(r for r in resources if r["resourceType"] == "Patient"))
        matches = [r for r in resources if r["resourceType"] == kind]
        return fhir_json(
            {
                "resourceType": "Bundle",
                "type": "searchset",
                "entry": [{"resource": r} for r in matches],
            }
        )

    request = {
        "hook": "order-sign",
        "hookInstance": "d1577c69-dfbe-44ad-ba6d-3e05e953b2ea",
        "fhirServer": EHR,
        "fhirAuthorization": {
            "access_token": TOKEN,
            "token_type": "Bearer",
            "scope": "patient/*.rs",
        },
        "context": {
            "userId": "Practitioner/1",
            "patientId": "ref-p1",
            "draftOrders": {"resourceType": "Bundle", "entry": [{"resource": order}]},
        },
    }
    return request, order, handler, seen


def allow_ehr(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> None:
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", "https://ehr.example.org")
    patched = functools.partial(FhirClient, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(cds_hooks, "FhirClient", patched)


def test_cds_hooks_fetches_missing_prefetch_from_an_allowed_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _, handler, seen = referral_ehr()
    allow_ehr(monkeypatch, handler)
    app = TestClient(create_app(db_path=tmp_path / "a.db"))
    assert app.get("/api/health").json()["fhir_allowed_servers"] == ["https://ehr.example.org"]
    response = app.post("/cds-services/mendari.referral-router", json=request)
    assert response.status_code == 200, response.text
    assert len(response.json()["cards"]) == 1
    urls = sorted(str(r.url).removeprefix(EHR) for r in seen)
    assert urls == [
        "/DiagnosticReport?patient=ref-p1",
        "/DocumentReference?patient=ref-p1&category=clinical-note",
        "/Observation?patient=ref-p1&category=laboratory",
        "/Patient/ref-p1",
    ]
    assert {r.headers["authorization"] for r in seen} == {f"Bearer {TOKEN}"}


def test_prefetch_that_was_sent_is_not_fetched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    request, _, handler, seen = referral_ehr()
    allow_ehr(monkeypatch, handler)
    patient = json.loads((FIXTURES / "referral.json").read_text())["entry"][0]["resource"]
    request["prefetch"] = {"patient": patient}
    TestClient(create_app(db_path=tmp_path / "a.db")).post(
        "/cds-services/mendari.referral-router", json=request
    )
    assert all("/Patient/" not in str(r.url) for r in seen)
    assert len(seen) == 3


def test_a_server_off_the_list_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    request, _, handler, seen = referral_ehr()
    monkeypatch.delenv("MENDARI_FHIR_ALLOWED_SERVERS", raising=False)
    monkeypatch.setattr(
        cds_hooks,
        "FhirClient",
        functools.partial(FhirClient, transport=httpx.MockTransport(handler)),
    )
    response = TestClient(create_app(db_path=tmp_path / "a.db")).post(
        "/cds-services/mendari.referral-router", json=request
    )
    assert response.status_code == 422
    assert "allow-list" in response.text
    assert TOKEN not in response.text
    assert seen == []


def test_without_a_fhir_server_prefetch_is_still_required(tmp_path: Path) -> None:
    request, _, _, _ = referral_ehr()
    del request["fhirServer"]
    response = TestClient(create_app(db_path=tmp_path / "a.db")).post(
        "/cds-services/mendari.referral-router", json=request
    )
    assert response.status_code == 422
    assert "prefetch required for: patient, documents, reports, labs" in response.text


@pytest.mark.parametrize("patient_id", ["p1&_include=*", "../Admin", "", None])
def test_only_a_fhir_id_can_fill_a_template(patient_id: Any) -> None:
    with pytest.raises(FhirError, match=r"context\.patientId"):
        cds_hooks.fill_template(
            "Observation?patient={{context.patientId}}", {"patientId": patient_id}
        )


def test_a_template_fills_every_token() -> None:
    assert (
        cds_hooks.fill_template("Patient/{{ context.patientId }}", {"patientId": "a-1.2"})
        == "Patient/a-1.2"
    )
    with pytest.raises(FhirError, match="cannot fill"):
        cds_hooks.fill_template("Practitioner/{{userPractitionerId}}", {})


def test_a_non_bearer_authorization_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    request, _, handler, _ = referral_ehr()
    allow_ehr(monkeypatch, handler)
    request["fhirAuthorization"]["token_type"] = "Basic"
    with pytest.raises(FhirError, match="Bearer"):
        cds_hooks.fetch({"patient": "Patient/{{context.patientId}}"}, ["patient"], request)
