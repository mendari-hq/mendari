# SPDX-License-Identifier: Apache-2.0
"""A hook that always answers (LP-23d): a prefetch key that could not be read, or a note that is
only a link or whose data does not decode, gives a recorded ABSTAIN with a fixed reason, and the
hook still answers `200`, with an `info` card whatever the agent's `show` says. Request faults
stay `4xx`, the structural R4 gate on what the client sent stays a `400`, and refused input still
BLOCKs.

On the chart-summary test fixture (`mendari-test/chart-summary`, a `patient-view` service) and
its `order-sign` twin (`tests/support/chart_fixture.py`), reading a synthetic chart from a
vendor-shaped FHIR server (`tests/support/chart_ehr.py`) whose Condition search is made to fail.
Synthetic data, not clinically reviewed."""

from __future__ import annotations

import base64
import copy
import functools
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from mendari import Agent, AuditStore, Decision
from mendari.cli import main
from mendari.core.agent import load_agent
from mendari.core.spec import Outcome
from mendari.sandbox.fhir_records import message_entries
from mendari.sandbox.fhir_workflow import labelled_cases
from mendari.server import smart_routes
from mendari.server.app import create_app
from mendari.standards import cds_hooks, fhir_client
from mendari.standards.data_coverage import READ_REASONS, source_type
from mendari.standards.fhir_client import FhirClient, GuardedHttp
from tests.conftest import CHART_DIR, DASHBOARD, DEMO_AGENT_DIR, ROOT, TRIAGE_DIR
from tests.support.chart_ehr import ORIGIN, TOKEN, ChartEhr
from tests.support.chart_fixture import (
    CHART_ID,
    ORDERS_SERVICE,
    SERVICE,
    hook,
    order_hook,
    order_sign,
)
from tests.support.fixture_records import BASE, SHOWCASE, Case, Condition, Medication, Note
from tests.test_smart import APP, CLIENT, FakeEhr

Handler = Callable[[httpx.Request], httpx.Response]
SERVER_TEXT = "SERVER-SAYS-PATIENT-chart-unread-1-HAS-SECRETS"  # what a failing server answers
# The read made to fail: the `conditions` key's search, while the EHR answers every other read of
# the chart (the Patient, its medications as statements and requests, its clinic notes).
FAILING = "Condition"
# The showcase chart (`tests/support/fixture_records.py` SHOWCASE), as the US Core fixture holds it.
CHART = json.loads((ROOT / "tests" / "fixtures" / "us-core" / "chart.json").read_text())
# A chart the agent automates when it reads all of it: a long-term condition (a routine follow-up),
# a medication that is no anticoagulant, and a clinic note.
CASE = Case(
    "chart-unread-1",
    age=70,
    sex="female",
    conditions=(Condition("E11.9", "2019-04-01"),),
    medications=(Medication("6809", "2024-01-01"),),
    notes=(Note(("Reviewed at the clinic today.", "Routine follow-up in three months.")),),
)
KEYS = ("patient", "conditions", "medications", "medication_requests", "notes")  # its prefetch


def fhir_json(body: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=body, headers={"content-type": "application/fhir+json"})


def _raise(error: type[httpx.TransportError]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error("failed", request=request)

    return handler


def _bundle(*resources: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": r} for r in resources],
        **extra,
    }


OPERATION_OUTCOME = {
    "resourceType": "OperationOutcome",
    "issue": [{"severity": "error", "code": "exception", "diagnostics": SERVER_TEXT}],
}
# A Condition whose structure is not R4's: an onset that is a number.
NOT_R4 = {
    "resourceType": "Condition",
    "id": "c1",
    "subject": {"reference": f"Patient/{CASE.id}"},
    "onsetDateTime": 7,
}
FAULTS: dict[str, Handler] = {
    "network": _raise(httpx.ConnectError),
    "timeout": _raise(httpx.ReadTimeout),
    "http_status": lambda r: fhir_json({"error": SERVER_TEXT}, status=500),
    "unauthorized": lambda r: fhir_json({"error": SERVER_TEXT}, status=403),
    "not_json": lambda r: httpx.Response(
        200, text=f"<html>{SERVER_TEXT}</html>", headers={"content-type": "text/html"}
    ),
    "too_large": lambda r: fhir_json(_bundle(id="x" * 4096)),
    "operation_outcome": lambda r: fhir_json(OPERATION_OUTCOME),
    "not_r4": lambda r: fhir_json(_bundle(NOT_R4)),
    "unsupported_type": lambda r: fhir_json({"resourceType": "Patient", "id": CASE.id}),
    "off_server": lambda r: fhir_json(
        _bundle(link=[{"relation": "next", "url": "https://evil.example/fhir/Condition"}])
    ),
    "cap": lambda r: fhir_json(
        _bundle(link=[{"relation": "next", "url": f"{BASE}/Condition?page=again"}])
    ),
}
FETCHED = sorted(FAULTS)  # the reasons a read from the EHR's server gives
NOTED = ["not_fetched", "undecodable"]  # the reasons a note gives


def test_every_reason_is_shown_here() -> None:
    assert set(FETCHED) | set(NOTED) == READ_REASONS


def ehr_failing(
    monkeypatch: pytest.MonkeyPatch,
    fault: Handler | None,
    kind: str = FAILING,
    *,
    order: bool = False,
) -> tuple[dict[str, Any], list[httpx.Request]]:
    """The chart's hook request as an EHR sends it (no prefetch, its FHIR server and token named):
    the chart fixture's `patient-view`, or with `order` its twin's `order-sign` for the chart's
    order in draft; and every request the EHR was sent. The EHR (`ChartEhr`, its token required)
    answers `kind`'s reads with `fault` and the rest from the chart."""
    server = ChartEhr(CASE.resources(), statements="offered", token=TOKEN)
    seen: list[httpx.Request] = []

    def handler(sent: httpx.Request) -> httpx.Response:
        seen.append(sent)
        if fault is not None and sent.url.path.endswith(f"/{kind}"):
            return fault(sent)
        return server(sent)

    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", ORIGIN)
    patched = functools.partial(FhirClient, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(cds_hooks, "FhirClient", patched)
    monkeypatch.setattr(fhir_client, "MAX_PAGES", 1)  # the `cap` fault pages without end
    monkeypatch.setattr(fhir_client, "MAX_BYTES", 4096)  # the `too_large` fault is over it
    made = order_hook if order else hook
    return made(CASE, prefetched=False, fhir_server=BASE, token=TOKEN), seen


def read_paths(seen: list[httpx.Request]) -> list[str]:
    """What each request read, under the base and without its query, in order."""
    return [str(r.url).removeprefix(BASE).split("?")[0] for r in seen]


@pytest.fixture
def app(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(db_path=tmp_path / "a.db")) as client:
        yield client


@pytest.fixture
def twin(tmp_path: Path) -> Iterator[TestClient]:
    """An engine serving the test fixtures' agents and the chart fixture's `order-sign` twin."""
    order_sign(tmp_path / "agents")
    served = [CHART_DIR.parent, tmp_path / "agents"]
    with TestClient(create_app(db_path=tmp_path / "twin.db", agents_paths=served)) as client:
        yield client


def recorded(app: TestClient, card: dict[str, Any]) -> dict[str, Any]:
    return recorded_by_id(app, card["extension"]["mendari.decision_id"])


def recorded_by_id(app: TestClient, decision_id: str) -> dict[str, Any]:
    found = app.get(f"/api/audit/{decision_id}")
    assert found.status_code == 200, found.text
    return dict(found.json())


def decided(app: TestClient) -> dict[str, Any]:
    """The one decision recorded, for a hook that showed no card for it."""
    [listed] = app.get("/api/audit").json()
    return recorded_by_id(app, listed["id"])


@pytest.mark.parametrize("reason", FETCHED)
def test_a_fetch_failure_gives_abstain_with_a_reason_and_a_recorded_decision(
    monkeypatch: pytest.MonkeyPatch, app: TestClient, reason: str
) -> None:
    request, seen = ehr_failing(monkeypatch, FAULTS[reason])
    response = app.post(SERVICE, json=request)
    assert response.status_code == 200, response.text
    assert "/Condition" in read_paths(seen), "the failing read was made"
    [card] = response.json()["cards"]
    assert card["indicator"] == "info"
    assert card["summary"] == cds_hooks.UNREADABLE_SUMMARY
    assert f"- conditions: {reason}" in card["detail"]
    item = recorded(app, card)
    assert item["outcome"] == "ABSTAIN"
    record = item["record"]
    assert record["reasons"] == [f"could not read conditions: {reason}"]
    assert record["answers"] == {} and record["errors"] == {}  # no provider was asked
    coverage = record["data_coverage"]
    assert coverage["unreadable"] == {reason: 1}
    assert coverage["lists"]["conditions"]["unreadable"] == {reason: 1}
    assert coverage["fields"]["problems"]["unreadable"] == {reason: 1}  # the same key's field
    medications = coverage["lists"]["medications"]  # read: its own count stands
    assert medications["unreadable"] == {}
    assert medications["sources"]["MedicationStatement"]["resources"] == 1


# Search pages whose shape is not R4, as an allowed server may answer them, each with the reason
# the key is unreadable for. Each answered `500` with nothing recorded before.
MALFORMED_PAGES: list[tuple[dict[str, Any], str]] = [
    ({"link": "oops"}, "not_r4"),
    ({"link": ["oops"]}, "not_r4"),
    ({"link": [{"relation": "next", "url": 5}]}, "not_r4"),
    ({"link": [{"relation": "next", "url": f"{BASE}/Condition?page=\x01"}]}, "not_r4"),
    ({"link": [{"relation": "next", "url": "https://[ehr.example.org/fhir/x"}]}, "off_server"),
    ({"entry": 5}, "not_r4"),
    ({"entry": ["x"]}, "not_r4"),
]


@pytest.mark.parametrize(("page", "reason"), MALFORMED_PAGES)
def test_a_search_page_that_is_not_r4_abstains_never_a_500(
    monkeypatch: pytest.MonkeyPatch, app: TestClient, page: dict[str, Any], reason: str
) -> None:
    """An allowed server's search answer whose `link` or `entry` is not R4's shape, or whose
    `next` link is not a URL: the key is unreadable, and the hook answers with a recorded
    ABSTAIN, never a `500` with nothing recorded."""
    answer = {"resourceType": "Bundle", "type": "searchset", **page}
    request, seen = ehr_failing(monkeypatch, lambda r: fhir_json(answer))
    response = app.post(SERVICE, json=request)
    assert response.status_code == 200, response.text
    assert "/Condition" in read_paths(seen), "the read was made"
    [card] = response.json()["cards"]
    assert f"- conditions: {reason}" in card["detail"]
    item = recorded(app, card)
    assert item["outcome"] == "ABSTAIN"
    assert item["record"]["reasons"] == [f"could not read conditions: {reason}"]


def test_a_later_search_page_that_is_not_a_bundle_abstains(
    monkeypatch: pytest.MonkeyPatch, app: TestClient
) -> None:
    """A search whose second page is an OperationOutcome is not read as its first page alone:
    the record would be cut short. The key is unreadable (`operation_outcome`)."""
    first = _bundle(link=[{"relation": "next", "url": f"{BASE}/Condition?page=2"}])

    def answer(sent: httpx.Request) -> httpx.Response:
        return fhir_json(OPERATION_OUTCOME if sent.url.params.get("page") == "2" else first)

    request, seen = ehr_failing(monkeypatch, answer)
    response = app.post(SERVICE, json=request)
    assert response.status_code == 200, response.text
    assert read_paths(seen).count("/Condition") == 2  # both pages were read
    [card] = response.json()["cards"]
    reasons = recorded(app, card)["record"]["reasons"]
    assert reasons == ["could not read conditions: operation_outcome"]
    assert SERVER_TEXT not in response.text


@pytest.mark.parametrize("reason", NOTED)
def test_a_note_that_is_only_a_link_or_does_not_decode_abstains(
    app: TestClient, reason: str
) -> None:
    """Data present but unreadable (LP-23d): an attachment with only a `url`, on a hook that names
    no FHIR server to read it from, is `not_fetched`, and one whose data is base64 but not UTF-8
    text is `undecodable`. Neither reads as no notes: the agent reads its notes through
    `attachment_text`, whose reasons these are."""
    resources = CASE.resources()
    note = next(r for r in resources if r["resourceType"] == "DocumentReference")
    unreadable = base64.b64encode(b"\xfa\xfb\xfc").decode()  # base64, but not UTF-8 text
    linked = {"contentType": "text/plain", "url": "Binary/n1"}
    undecodable = {"contentType": "text/plain", "data": unreadable}
    note["content"] = [{"attachment": linked if reason == "not_fetched" else undecodable}]
    response = app.post(SERVICE, json=patient_view(resources))
    assert response.status_code == 200, response.text
    [card] = response.json()["cards"]
    assert card["indicator"] == "info" and f"- notes: {reason}" in card["detail"]
    item = recorded(app, card)
    assert item["outcome"] == "ABSTAIN"
    assert item["record"]["reasons"] == [f"could not read notes: {reason}"]
    coverage = item["record"]["data_coverage"]
    assert coverage["fields"]["notes"]["unreadable"] == {reason: 1}
    assert coverage["unreadable"] == {reason: 1}


def patient_view(resources: list[dict[str, Any]]) -> dict[str, Any]:
    """The chart fixture's patient-view request for the Patient among `resources`, with every
    prefetch key sent."""
    patient = next(r for r in resources if r["resourceType"] == "Patient")
    return {
        "hook": "patient-view",
        "hookInstance": "4b4c1b2e-0000-4000-8000-000000000002",
        "context": {"patientId": patient["id"], "userId": "Practitioner/1"},
        "prefetch": prefetch_of(load_agent(CHART_DIR), resources),
    }


def prefetch_of(agent: Agent, resources: list[dict[str, Any]]) -> dict[str, Any]:
    """Each of the agent's prefetch keys filled from `resources`: a read with the first resource
    of its type (null when there is none), a search with a searchset of all of them."""
    hooks = agent.manifest.cds_hooks
    assert hooks is not None
    filled: dict[str, Any] = {}
    for key, template in hooks.prefetch.items():
        kind = source_type(template)
        found = [r for r in resources if r["resourceType"] == kind]
        if "?" in template:
            filled[key] = _bundle(*found)
        else:
            filled[key] = found[0] if found else None
    return filled


def test_the_reason_holds_no_url_query_or_server_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """What a failing server answers, the URL and query that were read, and the token are in no
    reason, card, data-coverage record, answer or stored row: only the key and a fixed code."""
    db = tmp_path / "a.db"
    for reason in ("http_status", "not_json", "operation_outcome"):
        request, _ = ehr_failing(monkeypatch, FAULTS[reason])
        with TestClient(create_app(db_path=db)) as app:
            response = app.post(SERVICE, json=request)
            [card] = response.json()["cards"]
            item = recorded(app, card)
        said = json.dumps([card["summary"], card["detail"], item["record"]["reasons"]])
        said += json.dumps(item["record"]["data_coverage"])
        for leak in ("ehr.example.org", "Condition?", "patient=", "category=", "HTTP"):
            assert leak not in said, (reason, leak)
        for leak in (SERVER_TEXT, TOKEN, BASE):
            assert leak not in response.text, (reason, leak)
            assert leak not in json.dumps(item), (reason, leak)
    with AuditStore(db) as store, store._db.session(exclusive=False) as rows:
        kept = json.dumps([rows.all(f"SELECT * FROM {t}") for t in ("decisions", "audit_events")])
    assert SERVER_TEXT not in kept and TOKEN not in kept


def test_a_null_prefetch_value_is_no_data_and_is_not_fetched(
    monkeypatch: pytest.MonkeyPatch, app: TestClient
) -> None:
    """CDS Hooks 2.0: a key whose value is null has no data. It is not fetched (it was, before)
    and does not abstain; a key left out is still fetched. With no condition read, the chart
    needs no follow-up, a decision the agent shows no card for."""
    request, seen = ehr_failing(monkeypatch, None)
    request["prefetch"] = {"conditions": None}
    response = app.post(SERVICE, json=request)
    assert response.status_code == 200, response.text
    assert sorted(read_paths(seen)) == [
        "/DocumentReference",
        "/MedicationRequest",
        "/MedicationStatement",
        f"/Patient/{CASE.id}",
    ]
    assert response.json() == {"cards": []}
    record = decided(app)["record"]
    assert not any(reason.startswith("could not read") for reason in record["reasons"])
    conditions = record["data_coverage"]["lists"]["conditions"]
    assert (conditions["resources"], conditions["items"], conditions["unreadable"]) == (0, 0, {})
    # With every key sent, null ones included, no server is needed: no 412.
    patient = next(r for r in CASE.resources() if r["resourceType"] == "Patient")
    del request["fhirServer"]
    request["prefetch"] = {**dict.fromkeys(KEYS), "patient": patient}
    assert app.post(SERVICE, json=request).status_code == 200


def test_an_operation_outcome_in_prefetch_abstains(
    monkeypatch: pytest.MonkeyPatch, app: TestClient
) -> None:
    """An OperationOutcome the client sends as a key's value is its own read that failed: the
    key is unreadable (and not fetched again), and its text is never repeated."""
    request, seen = ehr_failing(monkeypatch, None)
    request["prefetch"] = {"conditions": OPERATION_OUTCOME}
    response = app.post(SERVICE, json=request)
    assert response.status_code == 200, response.text
    assert "/Condition" not in read_paths(seen)
    [card] = response.json()["cards"]
    record = recorded(app, card)["record"]
    assert record["reasons"] == ["could not read conditions: operation_outcome"]
    assert SERVER_TEXT not in response.text


CONDITION = {
    "resourceType": "Condition",
    "id": "c1",
    "subject": {"reference": f"Patient/{CASE.id}"},
    "code": {"text": "Essential (primary) hypertension"},
}


@pytest.mark.parametrize("value", ["conditions", 7, {"entry": []}, [CONDITION], {"c1": CONDITION}])
def test_a_value_that_is_neither_data_nor_null_abstains(
    monkeypatch: pytest.MonkeyPatch, app: TestClient, value: Any
) -> None:
    """A declared key's value is a resource (or a Bundle), or null for no data. Anything else
    (a string, a number, an object with no `resourceType`, a list) is neither, so the key is
    unreadable (`unsupported_type`) and not fetched: never decided on, and never read as an
    empty record."""
    request, seen = ehr_failing(monkeypatch, None)
    request["prefetch"] = {"conditions": value}
    response = app.post(SERVICE, json=request)
    assert response.status_code == 200, response.text
    assert "/Condition" not in read_paths(seen)
    [card] = response.json()["cards"]
    reasons = recorded(app, card)["record"]["reasons"]
    assert reasons == ["could not read conditions: unsupported_type"]


@pytest.mark.parametrize("value", [{}, [], ""])
def test_an_empty_value_is_left_out_as_before(
    monkeypatch: pytest.MonkeyPatch, app: TestClient, value: Any
) -> None:
    """An empty value is not data and not "no data" (null): it counts as left out, as it did
    before LP-23d. It is fetched from the `fhirServer`, and with none the request is a `412`
    (the one status that moved: it was a `422`), with nothing decided or recorded."""
    request, seen = ehr_failing(monkeypatch, None)
    patient = next(r for r in CASE.resources() if r["resourceType"] == "Patient")
    request["prefetch"] = {**dict.fromkeys(KEYS), "patient": patient, "conditions": value}
    response = app.post(SERVICE, json=request)
    assert response.status_code == 200, response.text
    assert read_paths(seen) == ["/Condition"]
    [card] = response.json()["cards"]  # the condition read: a routine follow-up, shown
    reasons = recorded(app, card)["record"]["reasons"]
    assert not any(reason.startswith("could not read") for reason in reasons)
    del request["fhirServer"]
    response = app.post(SERVICE, json=request)
    assert response.status_code == 412, response.text
    assert "prefetch required for: conditions" in response.text
    assert len(app.get("/api/audit").json()) == 1  # the first request's decision alone


@pytest.mark.parametrize("value", [CASE.id, 7, {"entry": []}])
def test_a_required_resource_sent_as_something_else_is_still_refused(
    app: TestClient, value: Any
) -> None:
    """The chart fixture requires a Patient. A client that sends something else for it (not a
    resource, not null) has sent no Patient: the request is refused (`422`) as before, never
    turned into a recorded ABSTAIN. A Patient sent inside a list is not a resource either: it
    is never decided on, but the record holds it, so the decision abstains."""
    body = patient_view(CASE.resources())
    patient = body["prefetch"]["patient"]
    body["prefetch"]["patient"] = value
    response = app.post(SERVICE, json=body)
    assert response.status_code == 422, response.text
    assert "missing required FHIR resources: Patient" in response.text
    assert app.get("/api/audit").json() == []
    body["prefetch"]["patient"] = [patient]
    response = app.post(SERVICE, json=body)
    assert response.status_code == 200, response.text
    [card] = response.json()["cards"]
    assert card["indicator"] == "info"
    assert recorded(app, card)["record"]["reasons"] == ["could not read patient: unsupported_type"]


def test_no_provider_runs_when_the_record_is_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    decision = Decision.from_hub(CHART_ID)
    asked: list[str] = []
    for slot, provider in decision.providers.items():
        monkeypatch.setattr(provider, "answer", lambda inputs, slot=slot: asked.append(slot))
    result, task = decision.decide_fhir(copy.deepcopy(CHART), unreadable={"conditions": "timeout"})
    assert asked == []
    assert result.outcome is Outcome.ABSTAIN and result.recommendation is None
    assert result.reasons == ("could not read conditions: timeout",)
    assert result.unreadable == (("conditions", "timeout"),)
    assert result.fallback == decision.agent.manifest.policy.fallback
    assert task["description"] == result.fallback  # the Task is the agent's fallback


def test_rejected_input_still_blocks() -> None:
    """Refused input BLOCKs before the unreadable record is considered (never an ABSTAIN in its
    place): the chart's Patient could not be read, so its input lacks the `patient` the agent's
    schema requires, and under `enforce` a US Core error blocks too. The reasons say both."""
    chart = Decision.from_hub(CHART_ID)
    record = copy.deepcopy(CHART)
    others = [e for e in record["entry"] if e["resource"]["resourceType"] != "Patient"]
    result, _ = chart.decide_fhir({**record, "entry": others}, unreadable={"patient": "timeout"})
    assert result.outcome is Outcome.BLOCK
    assert result.reasons[-1] == "could not read patient: timeout"
    assert result.reasons[0].startswith("input rejected: ")
    strict = Decision.from_hub(CHART_ID, fhir_validation="enforce")
    [patient] = [
        e["resource"] for e in record["entry"] if e["resource"]["resourceType"] == "Patient"
    ]
    patient.pop("gender")  # US Core requires it
    blocked, _ = strict.decide_fhir(record, unreadable={"conditions": "timeout"})
    assert blocked.outcome is Outcome.BLOCK
    assert blocked.reasons[0].startswith("input rejected: not US Core")
    assert blocked.reasons[-1] == "could not read conditions: timeout"


def _first_automated(decision: Decision, records: list[dict[str, Any]]) -> dict[str, Any]:
    """The first of `records` (FHIR Bundles) the agent automates, or the first if none is."""
    for record in records:
        result, _ = decision.decide_fhir(copy.deepcopy(record))
        if result.outcome is Outcome.AUTO:
            return record
    return records[0]


def _messages(folder: Path) -> list[dict[str, Any]]:
    """A message agent's first cases as the sandbox seeds them: each a Patient and its
    Communication, as a collection Bundle."""
    cases = labelled_cases(load_agent(folder)).records[:20]
    return [
        {"resourceType": "Bundle", "type": "collection", "entry": message_entries(case)}
        for case in cases
    ]


def _agents(base: Path) -> list[tuple[Decision, dict[str, Any]]]:
    """Each agent here with a FHIR mapping and a CDS hook, and a record it decides (the one it
    automates, where it automates one): the chart fixture and its order-sign twin on a chart, the
    triage fixture and the bundled demo agent on a synthetic message."""
    found = []
    for folder in (CHART_DIR, order_sign(base)):
        decision = Decision(load_agent(folder))
        found.append((decision, _first_automated(decision, [CASE.bundle()])))
    for folder in (TRIAGE_DIR, DEMO_AGENT_DIR):
        decision = Decision(load_agent(folder))
        found.append((decision, _first_automated(decision, _messages(folder))))
    return found


def test_an_unreadable_record_never_raises_automation(tmp_path: Path) -> None:
    """Every agent with a FHIR mapping, every reason, every one of its prefetch keys: a case it
    decides (the one it automates, where it automates one) abstains or is blocked, never
    automated, never sent for review as if the record were whole."""
    agents = _agents(tmp_path)
    automated = [
        d.agent.id for d, record in agents if d.decide_fhir(record)[0].outcome.value == "AUTO"
    ]
    assert CHART_ID in automated  # the chart fixture's case is one it automates
    for decision, bundle in agents:
        hooks = decision.agent.manifest.cds_hooks
        assert hooks is not None and hooks.prefetch
        for key in hooks.prefetch:
            for reason in sorted(READ_REASONS):
                failed = {key: reason}
                result, _ = decision.decide_fhir(copy.deepcopy(bundle), unreadable=failed)
                assert result.outcome in (Outcome.ABSTAIN, Outcome.BLOCK), (decision.agent.id, key)
                assert f"could not read {key}: {reason}" in result.reasons


def test_request_faults_stay_4xx_and_the_r4_gate_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, twin: TestClient
) -> None:
    """The wrong hook, too many orders, a malformed field, a context that cannot fill a
    template, and neither prefetch nor fhirServer are the request's faults: a `4xx`, nothing
    decided or recorded. A structure error in what the client sent stays a `400`; in what
    Mendari read itself it is `not_r4` (above). On the twin's `order-sign`, whose draft orders
    are part of the request."""
    request, _ = ehr_failing(monkeypatch, None, order=True)

    def sent(change: Callable[[dict[str, Any]], None]) -> int:
        body = copy.deepcopy(request)
        change(body)
        return twin.post(ORDERS_SERVICE, json=body).status_code

    def many(body: dict[str, Any]) -> None:
        order = body["context"]["draftOrders"]["entry"][0]["resource"]
        orders = [{"resource": {**order, "id": f"o{n}"}} for n in range(cds_hooks.MAX_ORDERS + 1)]
        body["context"]["draftOrders"]["entry"] = orders

    def no_server(body: dict[str, Any]) -> None:
        del body["fhirServer"]

    def no_patient(body: dict[str, Any]) -> None:
        del body["context"]["patientId"]

    def malformed(value: Any) -> Callable[[dict[str, Any]], None]:
        return lambda body: body.update(prefetch={"conditions": value})

    assert sent(lambda body: body.update(hook="patient-view")) == 422
    assert sent(many) == 400
    assert sent(lambda body: body.update(context=[])) == 422
    assert sent(lambda body: body.update(fhirAuthorization={"access_token": 7})) == 422
    assert sent(no_patient) == 422
    assert sent(no_server) == 412
    # The structural check runs on every value the client sent before any is set aside as an
    # OperationOutcome or as not a resource: each of these was, and stays, a `400`.
    patient = {"resourceType": "Patient", "id": CASE.id, "gender": 7}
    assert sent(lambda body: body.update(prefetch={"patient": patient})) == 400
    assert sent(malformed({"resourceType": "OperationOutcome", "issue": 5})) == 400
    assert sent(malformed([NOT_R4])) == 400
    assert sent(malformed({"c1": NOT_R4})) == 400
    assert sent(malformed({"resourceType": 5})) == 400
    assert sent(malformed(_bundle(NOT_R4))) == 400
    assert twin.get("/api/audit").json() == []


def test_input_refused_by_the_r4_gate_is_refused_before_anything_is_fetched(
    monkeypatch: pytest.MonkeyPatch, twin: TestClient
) -> None:
    """The prefetch the client sent and its draft orders are checked (structure, the context's
    patient) before Mendari fetches the keys left out on the request's behalf: a request refused
    is never a call to its `fhirServer`."""
    request, seen = ehr_failing(monkeypatch, None, order=True)

    def sent(change: Callable[[dict[str, Any]], None]) -> int:
        body = copy.deepcopy(request)
        change(body)
        return twin.post(ORDERS_SERVICE, json=body).status_code

    def bad_order(body: dict[str, Any]) -> None:
        body["context"]["draftOrders"]["entry"][0]["resource"]["status"] = 7

    patient = {"resourceType": "Patient", "id": CASE.id, "gender": 7}
    other = {"resourceType": "Patient", "id": "someone-else"}
    assert sent(lambda body: body.update(prefetch={"patient": patient})) == 400
    assert sent(bad_order) == 400
    assert sent(lambda body: body.update(prefetch={"patient": other})) == 422
    assert seen == []
    assert twin.get("/api/audit").json() == []
    assert sent(lambda body: None) == 200  # the same request, well formed, is fetched for
    assert seen


def test_an_unreadable_abstain_shows_a_card_whatever_show_says(app: TestClient) -> None:
    """The chart fixture shows cards for `soon`, `routine` and a refused input only, so an
    ABSTAIN (no recommendation) would be silent; over unreadable data it shows an `info` card, so
    the clinician knows the record was not checked (owner decision 6)."""
    agent = load_agent(CHART_DIR)
    hooks = agent.manifest.cds_hooks
    assert hooks is not None and hooks.card is not None and hooks.card.show is not None
    assert "none" not in hooks.card.show
    body = patient_view(CASE.resources())
    body["prefetch"]["conditions"] = OPERATION_OUTCOME
    [card] = app.post(SERVICE, json=body).json()["cards"]
    assert card["indicator"] == "info"
    assert card["summary"] == cds_hooks.UNREADABLE_SUMMARY
    assert "- conditions: operation_outcome" in card["detail"]
    assert agent.status_label in card["detail"]  # every card says the agent's status


# -- SMART decide ------------------------------------------------------------------------------


class FailingEhr(FakeEhr):
    """The SMART tests' fake EHR (tests/test_smart.py) holding the showcase chart
    (`tests/fixtures/us-core/chart.json`, its patient the launch's), answering one resource
    type's reads with `status`, or with the JSON body in `bodies`, instead."""

    def __init__(self) -> None:
        super().__init__()
        self.patient = SHOWCASE.id
        self.resources = [entry["resource"] for entry in CHART["entry"]]
        self.failing: dict[str, int] = {}
        self.bodies: dict[str, dict[str, Any]] = {}

    def fhir(self, request: httpx.Request) -> httpx.Response:
        kind = request.url.path.removeprefix("/fhir/").split("/")[0]
        status = self.failing.get(kind)
        if status is not None and request.headers.get("authorization"):
            return httpx.Response(status, json={"error": SERVER_TEXT})
        if kind in self.bodies and request.headers.get("authorization"):
            return fhir_json(self.bodies[kind])
        return super().fhir(request)


def launched_for_the_chart(client: TestClient, ehr: FailingEhr) -> None:
    """The EHR launches Mendari for the chart fixture, and the browser comes back with the
    code."""
    params = {"iss": ehr.iss, "launch": "xyz", "agent": CHART_ID}
    started = client.get("/smart/launch", params=params, follow_redirects=False)
    assert started.status_code == 302, started.text
    location = started.headers["location"]
    code = ehr.authorize(location)
    state = parse_qs(urlsplit(location).query)["state"][0]
    back = client.get("/smart/callback", params={"code": code, "state": state})
    assert back.status_code == 200, back.text
    assert client.get("/api/smart/session").json()["active"] is True


@pytest.fixture
def smart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, FailingEhr]]:
    ehr = FailingEhr()
    monkeypatch.setenv("MENDARI_SMART_CLIENT_ID", CLIENT)
    monkeypatch.setenv("MENDARI_PUBLIC_URL", APP)
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", ORIGIN)
    transport = httpx.MockTransport(ehr)
    monkeypatch.setattr(
        smart_routes, "GuardedHttp", functools.partial(GuardedHttp, transport=transport)
    )
    monkeypatch.setattr(
        smart_routes, "FhirClient", functools.partial(FhirClient, transport=transport)
    )
    with TestClient(
        create_app(db_path=tmp_path / "a.db"), base_url=APP, headers=DASHBOARD
    ) as client:
        launched_for_the_chart(client, ehr)
        yield client, ehr


SMART_DECIDE = {"agent_id": CHART_ID, "focus": f"Patient/{SHOWCASE.id}"}


def test_smart_decide_abstains_on_a_failed_prefetch_read(
    smart: tuple[TestClient, FailingEhr],
) -> None:
    """The launch's decide reads each prefetch key on its own, as a hook does: one that fails
    abstains with its reason. The focus resource failing is an error, as before (there is
    nothing to decide about), and a `401` still ends the launch."""
    app, ehr = smart
    ehr.failing = {"Condition": 500}
    response = app.post("/api/smart/decide", json=SMART_DECIDE)
    assert response.status_code == 200, response.text
    decision = response.json()["decision"]
    assert decision["outcome"] == "ABSTAIN"
    assert decision["reasons"] == ["could not read conditions: http_status"]
    assert decision["data_coverage"]["lists"]["conditions"]["unreadable"] == {"http_status": 1}
    assert SERVER_TEXT not in response.text
    assert app.get(f"/api/audit/{decision['id']}").status_code == 200
    ehr.failing = {"Patient": 500}
    assert app.post("/api/smart/decide", json=SMART_DECIDE).status_code == 422  # as before
    ehr.failing = {"Condition": 401}
    assert app.post("/api/smart/decide", json=SMART_DECIDE).status_code == 401
    assert app.get("/api/smart/session").json() == {"active": False}  # the launch ended


@pytest.mark.parametrize(("page", "reason"), MALFORMED_PAGES)
def test_smart_decide_abstains_on_a_search_page_that_is_not_r4(
    smart: tuple[TestClient, FailingEhr], page: dict[str, Any], reason: str
) -> None:
    """The launch's decide reads its record as a hook does: a search page that is not R4's
    shape makes the key unreadable, never a `500`."""
    app, ehr = smart
    ehr.bodies = {"Condition": {"resourceType": "Bundle", "type": "searchset", **page}}
    response = app.post("/api/smart/decide", json=SMART_DECIDE)
    assert response.status_code == 200, response.text
    decision = response.json()["decision"]
    assert decision["outcome"] == "ABSTAIN"
    assert decision["reasons"] == [f"could not read conditions: {reason}"]


@pytest.mark.parametrize("unread", ["timeout", "not_fetched"])
def test_audit_verify_passes_after_an_abstain(
    db_target: str, capsys: pytest.CaptureFixture[str], unread: str
) -> None:
    """`mendari audit verify` over a trail holding an unreadable ABSTAIN, on both databases."""
    store = AuditStore(db_target)
    chart = Decision.from_hub(CHART_ID, store=store)
    if unread == "not_fetched":
        resources = CASE.resources()
        note = next(r for r in resources if r["resourceType"] == "DocumentReference")
        note["content"] = [{"attachment": {"url": "Binary/n1"}}]
        result, _ = chart.decide_fhir(_bundle(*resources))
    else:
        result, _ = chart.decide_fhir(copy.deepcopy(CHART), unreadable={"conditions": "timeout"})
    assert result.outcome is Outcome.ABSTAIN
    store.close()
    capsys.readouterr()
    assert main(["audit", "verify", "--db", db_target]) == 0
    assert '"ok": true' in capsys.readouterr().out


def test_a_prefetch_patient_other_than_the_hooks_patient_is_refused(app: TestClient) -> None:
    """A decision is never made on one patient's record for another: a Patient in the prefetch,
    read or in a search, that is not `context.patientId` refuses the request, and nothing is
    decided or recorded."""
    body = patient_view(CASE.resources())
    assert app.post(SERVICE, json=body).status_code == 200
    for key, value in body["prefetch"].items():
        if isinstance(value, dict) and value.get("resourceType") == "Patient":
            other = copy.deepcopy(body)
            other["prefetch"][key]["id"] = "someone-else"
            refused = app.post(SERVICE, json=other)
            assert refused.status_code == 422
            assert "context.patientId" in refused.text and "someone-else" not in refused.text
            break
    else:
        raise AssertionError("the chart's request holds no Patient")
    search = copy.deepcopy(body)
    search["prefetch"]["extra"] = _bundle({"resourceType": "Patient", "id": "p2"})
    assert app.post(SERVICE, json=search).status_code == 422
    assert len(app.get("/api/audit").json()) == 1  # the first request's decision alone
