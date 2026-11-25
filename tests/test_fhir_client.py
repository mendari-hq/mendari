# SPDX-License-Identifier: Apache-2.0
"""Outbound FHIR reads under the allow-list (standards/fhir_client.py) and CDS Hooks fetch."""

from __future__ import annotations

import functools
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
from tests.support import chart_fixture
from tests.support import fixture_records as records
from tests.support.chart_ehr import ChartEhr

EHR = "https://ehr.example.org/fhir"
TOKEN = "secret-token-value"
# The chart fixture's patient-view service (`mendari-test/chart-summary`), and a chart it shows a
# card for (a routine follow-up) with a drug and a note.
SERVICE = chart_fixture.SERVICE
CASE = next(
    case
    for case in records.cases("test")
    if case.labels["follow_up"] == "routine" and case.medications and case.notes
)

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


NEXT = f"{EHR}/Observation?page=2"


@pytest.mark.parametrize(
    ("page", "reason"),
    [
        ({"link": "next"}, "not_r4"),
        ({"link": ["next"]}, "not_r4"),
        ({"link": {"relation": "next", "url": NEXT}}, "not_r4"),
        ({"link": [{"relation": "next", "url": 5}]}, "not_r4"),
        ({"link": [{"relation": "next", "url": [NEXT]}]}, "not_r4"),
        ({"link": [{"relation": "next", "url": f"{NEXT}\x01"}]}, "not_r4"),
        ({"link": [{"relation": "next", "url": "https://[ehr.example.org/fhir/x"}]}, "off_server"),
        ({"entry": 5}, "not_r4"),
        ({"entry": {"resource": {"resourceType": "Observation"}}}, "not_r4"),
    ],
)
def test_a_search_page_that_is_not_r4_is_a_fhir_error_never_a_crash(
    page: dict[str, Any], reason: str
) -> None:
    """A search page whose `link` is not a list of objects, whose `next` URL is not printable
    text (or does not parse), or whose `entry` is not a list fails with its reason in every
    mode (LP-23d), where it crashed before with an error a hook did not catch (a `500`)."""
    first = {"resourceType": "Bundle", "type": "searchset", **page}
    with client(lambda r: fhir_json(first)) as fhir:
        for complete in (False, True):
            with pytest.raises(FhirError) as error:
                fhir.read("Observation?patient=p1", complete=complete)
            assert error.value.reason == reason


@pytest.mark.parametrize(
    ("second", "reason"),
    [
        ({"resourceType": "OperationOutcome", "issue": []}, "operation_outcome"),
        ({"resourceType": "Patient", "id": "p1"}, "not_r4"),
        ({"resourceType": "Bundle", "entry": 5}, "not_r4"),
    ],
)
def test_a_following_page_that_is_not_a_bundle_is_not_a_complete_record(
    second: dict[str, Any], reason: str
) -> None:
    """A decision's record is read whole (`complete`): a later page that is not a Bundle fails
    with its reason, never read as the first pages alone. Other reads keep their first pages,
    as before, unless the page's shape is not R4."""
    first = {
        "resourceType": "Bundle",
        "entry": [{"resource": {"id": "1"}}],
        "link": [{"relation": "next", "url": NEXT}],
    }
    pages = {f"{EHR}/Observation?patient=p1": first, NEXT: second}
    with client(lambda r: fhir_json(pages[str(r.url)])) as fhir:
        with pytest.raises(FhirError) as error:
            fhir.read("Observation?patient=p1", complete=True)
        assert error.value.reason == reason
        if second["resourceType"] != "Bundle":
            bundle = fhir.read("Observation?patient=p1")
            assert [e["resource"]["id"] for e in bundle["entry"]] == ["1"]


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
        (  # nested too deep to read: a FhirError, never a RecursionError
            httpx.Response(200, text="[" * 100_000, headers={"content-type": "application/json"}),
            "invalid JSON",
        ),
        (
            httpx.Response(400, text="[" * 100_000, headers={"content-type": "application/json"}),
            "HTTP 400",
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


def patient_view() -> tuple[dict[str, Any], ChartEhr]:
    """A patient-view request for `CASE` with no prefetch, for the engine to read the chart from
    `EHR` with `TOKEN`; and that EHR, a vendor-shaped server of the chart (its drug requested by
    reference, its note a link to a Binary) that offers MedicationStatement, so it answers each
    of the agent's prefetch queries. Nothing calls it until it is installed (`allow_ehr`)."""
    ehr = ChartEhr.of(CASE, statements="offered", token=TOKEN)
    return chart_fixture.hook(CASE, prefetched=False, fhir_server=EHR, token=TOKEN), ehr


def allow_ehr(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> None:
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", "https://ehr.example.org")
    patched = functools.partial(FhirClient, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(cds_hooks, "FhirClient", patched)


def test_cds_hooks_fetches_missing_prefetch_from_an_allowed_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each prefetch query, then what the record links to and does not hold (its note's Binary,
    LP-23h), each with the hook's token."""
    request, ehr = patient_view()
    allow_ehr(monkeypatch, ehr)
    app = TestClient(create_app(db_path=tmp_path / "a.db"))
    assert app.get("/api/health").json()["fhir_allowed_servers"] == ["https://ehr.example.org"]
    response = app.post(SERVICE, json=request)
    assert response.status_code == 200, response.text
    assert len(response.json()["cards"]) == 1
    urls = sorted(str(r.url).removeprefix(EHR) for r in ehr.requests)
    patient = CASE.id
    binaries = [f"/Binary/{patient}-note-{n}-1" for n in range(1, len(CASE.notes) + 1)]
    assert urls == [
        *binaries,  # each note's one rendition, read as the record is
        f"/Condition?patient={patient}",
        f"/DocumentReference?patient={patient}&category=clinical-note",
        f"/MedicationRequest?patient={patient}&_include=MedicationRequest:medication",
        f"/MedicationStatement?patient={patient}",
        f"/Patient/{patient}",
    ]
    assert {r.headers["authorization"] for r in ehr.requests} == {f"Bearer {TOKEN}"}


def test_prefetch_that_was_sent_is_not_fetched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    request, ehr = patient_view()
    allow_ehr(monkeypatch, ehr)
    request["prefetch"] = {"patient": chart_fixture.prefetch(CASE)["patient"]}
    TestClient(create_app(db_path=tmp_path / "a.db")).post(SERVICE, json=request)
    assert all("/Patient/" not in str(r.url) for r in ehr.requests)
    assert len(ehr.searches()) == 4  # each of the other prefetch keys


def test_a_server_off_the_list_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    request, ehr = patient_view()
    monkeypatch.delenv("MENDARI_FHIR_ALLOWED_SERVERS", raising=False)
    monkeypatch.setattr(
        cds_hooks,
        "FhirClient",
        functools.partial(FhirClient, transport=httpx.MockTransport(ehr)),
    )
    response = TestClient(create_app(db_path=tmp_path / "a.db")).post(SERVICE, json=request)
    assert response.status_code == 422
    assert "allow-list" in response.text
    assert TOKEN not in response.text
    assert ehr.requests == []


def test_without_a_fhir_server_prefetch_is_still_required(tmp_path: Path) -> None:
    """Neither the prefetch nor a server to read it from: a `412`, as CDS Hooks 2.0 says
    (LP-23d; it was a `422`), and nothing is decided or recorded."""
    request, _ = patient_view()
    del request["fhirServer"]
    app = TestClient(create_app(db_path=tmp_path / "a.db"))
    response = app.post(SERVICE, json=request)
    assert response.status_code == 412
    assert response.json()["error"] == "PrefetchRequired"
    keys = "patient, conditions, medications, medication_requests, notes"
    assert f"prefetch required for: {keys}" in response.text
    assert app.get("/api/audit").json() == []


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
    request, ehr = patient_view()
    allow_ehr(monkeypatch, ehr)
    request["fhirAuthorization"]["token_type"] = "Basic"
    with pytest.raises(FhirError, match="Bearer"):
        cds_hooks.fetch({"patient": "Patient/{{context.patientId}}"}, ["patient"], request)


def test_a_port_that_is_not_a_number_is_refused_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`urlsplit(url).port` raises ValueError for `https://host:abc`; the allow-list check (every
    server-supplied link, token or JWKS endpoint, CDS `fhirServer`) answered 500 for such a URL."""
    http = fhir_client.GuardedHttp(allowed={"https://ehr.test"})
    with pytest.raises(FhirError, match="allow-list"):
        http.check("https://ehr.test:abc/fhir/Patient/1")
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", "https://ehr.test:abc")
    with pytest.raises(MendariError, match="must be an origin"):
        allowed_servers()


@pytest.mark.parametrize(
    ("field", "value", "said"),
    [
        ("fhirAuthorization", "a-token", "fhirAuthorization must be an object"),
        ("fhirAuthorization", {"access_token": 7}, "access_token must be a string"),
        # Sent as a header: a line break or a space was read as "network" (the server
        # "could not be reached"), and the decision abstained, for the client's own fault.
        ("fhirAuthorization", {"access_token": "tok\r\nx-a: b"}, "not a valid OAuth token"),
        ("fhirAuthorization", {"access_token": "two words"}, "not a valid OAuth token"),
        ("context", [], "context must be an object"),
        ("prefetch", "x", "prefetch must be an object"),
    ],
)
def test_a_malformed_cds_request_is_a_client_error(
    tmp_path: Path, field: str, value: Any, said: str
) -> None:
    """The route takes any JSON object; a nested field of the wrong shape raised AttributeError
    or ValueError inside the service (a 500, after the rate limit was charged)."""
    request, ehr = patient_view()
    request[field] = value
    response = TestClient(create_app(db_path=tmp_path / "a.db")).post(SERVICE, json=request)
    assert response.status_code == 422, response.text
    assert said in response.text
    assert ehr.requests == []


def test_a_context_id_with_a_trailing_line_break_does_not_fill_a_template() -> None:
    """`$` matches before a final newline: `pat-1\\n` passed as a FHIR id and reached the
    query as `pat-1%0A`."""
    template = "Patient/{{context.patientId}}"
    assert cds_hooks.fill_template(template, {"patientId": "pat-1"}) == "Patient/pat-1"
    with pytest.raises(FhirError, match=r"context\.patientId"):
        cds_hooks.fill_template(template, {"patientId": "pat-1\n"})


def test_a_value_that_is_not_text_is_off_the_allow_list() -> None:
    """A configuration may name a number or a list where a URL belongs: refused, never a crash
    (`.strip()` raised AttributeError)."""
    http = fhir_client.GuardedHttp(allowed={"https://ehr.test"})
    for value in (123, None, ["https://ehr.test"]):
        assert fhir_client.origin_of(value) is None  # type: ignore[arg-type]
        with pytest.raises(FhirError, match="allow-list"):
            http.check(value)  # type: ignore[arg-type]
