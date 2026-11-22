# SPDX-License-Identifier: Apache-2.0
"""A hook that always answers (LP-23d): a prefetch key that could not be read, or a note that is
only a link or whose data does not decode, gives a recorded ABSTAIN with a fixed reason, and the
hook still answers `200`, with an `info` card whatever the pack's `show` says. Request faults stay
`4xx`, the structural R4 gate on what the client sent stays a `400`, and refused input still
BLOCKs. For CRD, an order that abstained over unreadable data gets `indeterminate` coverage for a
`technical` reason when its Coverage was read."""

from __future__ import annotations

import base64
import copy
import functools
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, Pack
from mendari.cli import main
from mendari.core.pack import load_pack
from mendari.core.spec import Outcome
from mendari.errors import FhirError
from mendari.server import smart_routes
from mendari.server.app import create_app
from mendari.standards import cds_hooks, davinci, fhir_client
from mendari.standards.data_coverage import READ_REASONS, source_type
from mendari.standards.fhir_client import FhirClient, GuardedHttp
from tests.conftest import DASHBOARD, ROOT
from tests.test_fhir_client import EHR, TOKEN, Handler, fhir_json, referral_ehr
from tests.test_prior_auth_pack import _order, _prefetch
from tests.test_smart import APP, CLIENT, REFERRAL, FakeEhr, full_launch
from tests.test_trial_pack import _record as trial_record

SERVICE = "/cds-services/mendari.referral-router"
TRIAL = "/cds-services/mendari.clinical-trial-prescreener"
PA = "mendari/prior-auth-preflight"
SERVER_TEXT = "SERVER-SAYS-PATIENT-ref-p1-HAS-SECRETS"  # what a failing server answers
# How the EHR answers the referral pack's `labs` search (Observation) for each reason a read can
# fail with, while it answers every other read from the fixture.
LABS = "Observation"


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


FAULTS: dict[str, Handler] = {
    "network": _raise(httpx.ConnectError),
    "timeout": _raise(httpx.ReadTimeout),
    "http_status": lambda r: fhir_json({"error": SERVER_TEXT}, status=500),
    "unauthorized": lambda r: fhir_json({"error": SERVER_TEXT}, status=403),
    "not_json": lambda r: httpx.Response(
        200, text=f"<html>{SERVER_TEXT}</html>", headers={"content-type": "text/html"}
    ),
    "too_large": lambda r: fhir_json(_bundle(id="x" * 4096)),
    "operation_outcome": lambda r: fhir_json(
        {
            "resourceType": "OperationOutcome",
            "issue": [{"severity": "error", "code": "exception", "diagnostics": SERVER_TEXT}],
        }
    ),
    "not_r4": lambda r: fhir_json(
        _bundle({"resourceType": "Observation", "id": "o1", "status": 7, "code": {}})
    ),
    "unsupported_type": lambda r: fhir_json({"resourceType": "Patient", "id": "ref-p1"}),
    "off_server": lambda r: fhir_json(
        _bundle(link=[{"relation": "next", "url": "https://evil.example/fhir/Observation"}])
    ),
    "cap": lambda r: fhir_json(
        _bundle(link=[{"relation": "next", "url": f"{EHR}/Observation?page=again"}])
    ),
}
FETCHED = sorted(FAULTS)  # the reasons a read from the EHR's server gives
NOTED = ["not_fetched", "undecodable"]  # the reasons a note gives


def test_every_reason_is_shown_here() -> None:
    assert set(FETCHED) | set(NOTED) == READ_REASONS


def ehr_failing(
    monkeypatch: pytest.MonkeyPatch, fault: Handler | None, kind: str = LABS
) -> tuple[dict[str, Any], list[httpx.Request]]:
    """The referral order-sign request (no prefetch) and an EHR that answers `kind`'s reads with
    `fault` and the rest from the fixture."""
    request, _, answer, seen = referral_ehr()

    def handler(sent: httpx.Request) -> httpx.Response:
        if fault is not None and sent.url.path.endswith(f"/{kind}"):
            seen.append(sent)
            return fault(sent)
        return answer(sent)

    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", "https://ehr.example.org")
    patched = functools.partial(FhirClient, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(cds_hooks, "FhirClient", patched)
    monkeypatch.setattr(fhir_client, "MAX_PAGES", 1)  # the `cap` fault pages without end
    monkeypatch.setattr(fhir_client, "MAX_BYTES", 4096)  # the `too_large` fault is over it
    return request, seen


@pytest.fixture
def app(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(db_path=tmp_path / "a.db")) as client:
        yield client


def recorded(app: TestClient, card: dict[str, Any]) -> dict[str, Any]:
    decision_id = card["extension"]["mendari.decision_id"]
    found = app.get(f"/api/audit/{decision_id}")
    assert found.status_code == 200, found.text
    return dict(found.json())


@pytest.mark.parametrize("reason", FETCHED)
def test_a_fetch_failure_gives_abstain_with_a_reason_and_a_recorded_decision(
    monkeypatch: pytest.MonkeyPatch, app: TestClient, reason: str
) -> None:
    request, seen = ehr_failing(monkeypatch, FAULTS[reason])
    response = app.post(SERVICE, json=request)
    assert response.status_code == 200, response.text
    assert seen, "the failing read was made"
    [card] = response.json()["cards"]
    assert card["indicator"] == "info"
    assert card["summary"] == cds_hooks.UNREADABLE_SUMMARY
    assert f"- labs: {reason}" in card["detail"]
    item = recorded(app, card)
    assert item["outcome"] == "ABSTAIN"
    record = item["record"]
    assert record["reasons"] == [f"could not read labs: {reason}"]
    assert record["answers"] == {} and record["errors"] == {}  # no provider was asked
    coverage = record["data_coverage"]
    assert coverage["unreadable"] == {reason: 1}
    assert coverage["lists"]["labs"]["unreadable"] == {reason: 1}
    assert coverage["lists"]["documents"]["unreadable"] == {}  # read: its own count stands
    assert coverage["lists"]["documents"]["resources"] == 1


# Search pages whose shape is not R4, as an allowed server may answer them, each with the reason
# the key is unreadable for. Each answered `500` with nothing recorded before.
MALFORMED_PAGES: list[tuple[dict[str, Any], str]] = [
    ({"link": "oops"}, "not_r4"),
    ({"link": ["oops"]}, "not_r4"),
    ({"link": [{"relation": "next", "url": 5}]}, "not_r4"),
    ({"link": [{"relation": "next", "url": f"{EHR}/Observation?page=\x01"}]}, "not_r4"),
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
    assert seen, "the read was made"
    [card] = response.json()["cards"]
    assert f"- labs: {reason}" in card["detail"]
    item = recorded(app, card)
    assert item["outcome"] == "ABSTAIN"
    assert item["record"]["reasons"] == [f"could not read labs: {reason}"]


def test_a_later_search_page_that_is_not_a_bundle_abstains(
    monkeypatch: pytest.MonkeyPatch, app: TestClient
) -> None:
    """A search whose second page is an OperationOutcome is not read as its first page alone:
    the record would be cut short. The key is unreadable (`operation_outcome`)."""
    first = _bundle(link=[{"relation": "next", "url": f"{EHR}/Observation?page=2"}])
    outcome = FAULTS["operation_outcome"](httpx.Request("GET", EHR)).json()

    def answer(sent: httpx.Request) -> httpx.Response:
        return fhir_json(outcome if sent.url.params.get("page") == "2" else first)

    request, seen = ehr_failing(monkeypatch, answer)
    response = app.post(SERVICE, json=request)
    assert response.status_code == 200, response.text
    assert sum(r.url.path.endswith("/Observation") for r in seen) == 2  # both pages were read
    [card] = response.json()["cards"]
    assert recorded(app, card)["record"]["reasons"] == ["could not read labs: operation_outcome"]
    assert SERVER_TEXT not in response.text


@pytest.mark.parametrize("reason", NOTED)
def test_a_note_that_is_only_a_link_or_does_not_decode_abstains(
    app: TestClient, reason: str
) -> None:
    """Data present but unreadable (LP-23d): an attachment with only a `url`, on a hook that names
    no FHIR server to read it from, is `not_fetched`, and one whose data is base64 but not UTF-8
    text is `undecodable`. Before, both read as no notes, so the trial pack decided as if the
    record held none. (The trial pack reads its notes through spec 0.3's `attachment_text` since
    LP-23k, whose reasons these are too.)"""
    resources = trial_record("Seen for diabetes follow-up.")
    note = next(r for r in resources if r["resourceType"] == "DocumentReference")
    unreadable = base64.b64encode(b"\xfa\xfb\xfc").decode()  # base64, but not UTF-8 text
    linked = {"contentType": "text/plain", "url": "Binary/n1"}
    undecodable = {"contentType": "text/plain", "data": unreadable}
    note["content"] = [{"attachment": linked if reason == "not_fetched" else undecodable}]
    response = app.post(TRIAL, json=patient_view(resources))
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
    """A trial patient-view request with every prefetch key sent."""
    pack = load_pack(ROOT / "tests" / "fixtures" / "reference-packs" / "clinical-trial-prescreener")
    return {
        "hook": "patient-view",
        "hookInstance": "4b4c1b2e-0000-4000-8000-000000000002",
        "context": {"patientId": "p1", "userId": "Practitioner/1"},
        "prefetch": prefetch_of(pack, resources),
    }


def prefetch_of(pack: Pack, resources: list[dict[str, Any]]) -> dict[str, Any]:
    """Each of the pack's prefetch keys filled from `resources`: a read with the first resource
    of its type (null when there is none), a search with a searchset of all of them."""
    hooks = pack.manifest.cds_hooks
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
        for leak in ("ehr.example.org", "Observation?", "patient=", "category=", "HTTP"):
            assert leak not in said, (reason, leak)
        for leak in (SERVER_TEXT, TOKEN, "https://ehr.example.org/fhir"):
            assert leak not in response.text, (reason, leak)
            assert leak not in json.dumps(item), (reason, leak)
    with AuditStore(db) as store, store._db.session(exclusive=False) as rows:
        kept = json.dumps([rows.all(f"SELECT * FROM {t}") for t in ("decisions", "audit_events")])
    assert SERVER_TEXT not in kept and TOKEN not in kept


def test_a_null_prefetch_value_is_no_data_and_is_not_fetched(
    monkeypatch: pytest.MonkeyPatch, app: TestClient
) -> None:
    """CDS Hooks 2.0: a key whose value is null has no data. It is not fetched (it was, before)
    and does not abstain; a key left out is still fetched."""
    request, seen = ehr_failing(monkeypatch, None)
    request["prefetch"] = {"labs": None}
    response = app.post(SERVICE, json=request)
    assert response.status_code == 200, response.text
    paths = sorted(str(r.url).removeprefix(EHR).split("?")[0] for r in seen)
    assert paths == ["/DiagnosticReport", "/DocumentReference", "/Patient/ref-p1"]
    [card] = response.json()["cards"]
    record = recorded(app, card)["record"]
    assert not any(reason.startswith("could not read") for reason in record["reasons"])
    labs = record["data_coverage"]["lists"]["labs"]
    assert (labs["resources"], labs["items"], labs["unreadable"]) == (0, 0, {})
    # With every key sent, null ones included, no server is needed: no 412.
    del request["fhirServer"]
    request["prefetch"] = dict.fromkeys(["patient", "documents", "reports", "labs"])
    assert app.post(SERVICE, json=request).status_code == 200


def test_an_operation_outcome_in_prefetch_abstains(
    monkeypatch: pytest.MonkeyPatch, app: TestClient
) -> None:
    """An OperationOutcome the client sends as a key's value is its own read that failed: the
    key is unreadable (and not fetched again), and its text is never repeated."""
    request, seen = ehr_failing(monkeypatch, None)
    request["prefetch"] = {"labs": FAULTS["operation_outcome"](httpx.Request("GET", EHR)).json()}
    response = app.post(SERVICE, json=request)
    assert response.status_code == 200, response.text
    assert all("/Observation" not in str(r.url) for r in seen)
    [card] = response.json()["cards"]
    record = recorded(app, card)["record"]
    assert record["reasons"] == ["could not read labs: operation_outcome"]
    assert SERVER_TEXT not in response.text


LAB = {"resourceType": "Observation", "id": "o1", "status": "final", "code": {"text": "HbA1c"}}


@pytest.mark.parametrize("value", ["labs", 7, {"entry": []}, [LAB], {"o1": LAB}])
def test_a_value_that_is_neither_data_nor_null_abstains(
    monkeypatch: pytest.MonkeyPatch, app: TestClient, value: Any
) -> None:
    """A declared key's value is a resource (or a Bundle), or null for no data. Anything else
    (a string, a number, an object with no `resourceType`, a list) is neither, so the key is
    unreadable (`unsupported_type`) and not fetched: never decided on, and never read as an
    empty record."""
    request, seen = ehr_failing(monkeypatch, None)
    request["prefetch"] = {"labs": value}
    response = app.post(SERVICE, json=request)
    assert response.status_code == 200, response.text
    assert all("/Observation" not in str(r.url) for r in seen)
    [card] = response.json()["cards"]
    assert recorded(app, card)["record"]["reasons"] == ["could not read labs: unsupported_type"]


@pytest.mark.parametrize("value", [{}, [], ""])
def test_an_empty_value_is_left_out_as_before(
    monkeypatch: pytest.MonkeyPatch, app: TestClient, value: Any
) -> None:
    """An empty value is not data and not "no data" (null): it counts as left out, as it did
    before LP-23d. It is fetched from the `fhirServer`, and with none the request is a `412`
    (the one status that moved: it was a `422`), with nothing decided or recorded."""
    request, seen = ehr_failing(monkeypatch, None)
    request["prefetch"] = {"patient": None, "documents": None, "reports": None, "labs": value}
    response = app.post(SERVICE, json=request)
    assert response.status_code == 200, response.text
    assert [str(r.url).removeprefix(EHR).split("?")[0] for r in seen] == ["/Observation"]
    [card] = response.json()["cards"]
    reasons = recorded(app, card)["record"]["reasons"]
    assert not any(reason.startswith("could not read") for reason in reasons)
    del request["fhirServer"]
    response = app.post(SERVICE, json=request)
    assert response.status_code == 412, response.text
    assert "prefetch required for: labs" in response.text
    assert len(app.get("/api/audit").json()) == 1  # the first request's decision alone


@pytest.mark.parametrize("value", ["p1", 7, {"entry": []}])
def test_a_required_resource_sent_as_something_else_is_still_refused(
    app: TestClient, value: Any
) -> None:
    """The trial pack requires a Patient. A client that sends something else for it (not a
    resource, not null) has sent no Patient: the request is refused (`422`) as before, never
    turned into a recorded ABSTAIN. A Patient sent inside a list is not a resource either: it
    is never decided on, but the record holds it, so the decision abstains."""
    body = patient_view(trial_record("Seen for diabetes follow-up."))
    patient = body["prefetch"]["patient"]
    body["prefetch"]["patient"] = value
    response = app.post(TRIAL, json=body)
    assert response.status_code == 422, response.text
    assert "missing required FHIR resources: Patient" in response.text
    assert app.get("/api/audit").json() == []
    body["prefetch"]["patient"] = [patient]
    response = app.post(TRIAL, json=body)
    assert response.status_code == 200, response.text
    [card] = response.json()["cards"]
    assert card["indicator"] == "info"
    assert recorded(app, card)["record"]["reasons"] == ["could not read patient: unsupported_type"]


def test_no_provider_runs_when_the_record_is_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    decision = Decision.from_hub("mendari/referral-router")
    asked: list[str] = []
    for slot, provider in decision.providers.items():
        monkeypatch.setattr(provider, "answer", lambda inputs, slot=slot: asked.append(slot))
    bundle = json.loads((ROOT / "tests/fixtures/us-core/referral.json").read_text())
    result, task = decision.decide_fhir(bundle, unreadable={"labs": "timeout"})
    assert asked == []
    assert result.outcome is Outcome.ABSTAIN and result.recommendation is None
    assert result.reasons == ("could not read labs: timeout",)
    assert result.unreadable == (("labs", "timeout"),)
    assert result.fallback == decision.pack.manifest.policy.fallback
    assert task["description"] == result.fallback  # the Task is the pack's fallback


def test_rejected_input_still_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refused input BLOCKs before the unreadable record is considered (never an ABSTAIN in its
    place): the inbox's message could not be read, so its input lacks the required message, and
    under `enforce` a US Core error blocks too. The reasons say both."""
    inbox = Decision.from_hub("mendari/patient-inbox-router")
    bundle = json.loads((ROOT / "tests/fixtures/us-core/inbox.json").read_text())
    [patient] = [
        e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Patient"
    ]
    result, _ = inbox.decide_fhir({"patient": patient}, unreadable={"communication": "timeout"})
    assert result.outcome is Outcome.BLOCK
    assert result.reasons[-1] == "could not read communication: timeout"
    assert result.reasons[0].startswith("input rejected: ")
    strict = Decision.from_hub("mendari/referral-router", fhir_validation="enforce")
    referral = json.loads((ROOT / "tests/fixtures/us-core/referral.json").read_text())
    referral["entry"][0]["resource"].pop("gender")  # US Core requires it
    blocked, _ = strict.decide_fhir(referral, unreadable={"labs": "timeout"})
    assert blocked.outcome is Outcome.BLOCK
    assert blocked.reasons[0].startswith("input rejected: not US Core")
    assert blocked.reasons[-1] == "could not read labs: timeout"


def _cases() -> list[tuple[str, dict[str, Any]]]:
    """One synthetic FHIR case per pack with a FHIR mapping (the 23a golden's builders), the
    one it automates if it automates any."""
    script = _golden_script()
    found = []
    with script.on_the_day():
        for pack_id, (directory, dataset, build) in script.CASES.items():
            decision = Decision.from_path(
                ROOT / "tests" / "fixtures" / "reference-packs" / directory
            )
            records = script.load_dataset(
                ROOT / "tests" / "fixtures" / "datasets" / dataset
            ).records
            chosen = None
            for record in records:
                bundle = {"resourceType": "Bundle", "type": "collection", "entry": build(record)}
                try:
                    result, _ = decision.decide_fhir(bundle)
                except FhirError:  # an order the pack has no requirements for
                    continue
                chosen = chosen or bundle
                if result.outcome is Outcome.AUTO:
                    chosen = bundle
                    break
            assert chosen is not None
            found.append((pack_id, chosen))
    return found


@functools.cache
def _golden_script() -> Any:
    import importlib.util
    import sys

    path = ROOT / "scripts" / "fhir_decisions_golden.py"
    spec = importlib.util.spec_from_file_location("fhir_decisions_golden_unreadable", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_an_unreadable_record_never_raises_automation() -> None:
    """Every pack with a FHIR mapping, every reason, every one of its prefetch keys: a case it
    decides (the one it automates, where it automates one) abstains or is blocked, never
    automated, never sent for review as if the record were whole."""
    script = _golden_script()
    with script.on_the_day():
        for pack_id, bundle in _cases():
            decision = Decision.from_hub(pack_id)
            hooks = decision.pack.manifest.cds_hooks
            assert hooks is not None and hooks.prefetch
            for key in hooks.prefetch:
                for reason in sorted(READ_REASONS):
                    failed = {key: reason}
                    result, _ = decision.decide_fhir(copy.deepcopy(bundle), unreadable=failed)
                    assert result.outcome in (Outcome.ABSTAIN, Outcome.BLOCK), (pack_id, key)
                    assert f"could not read {key}: {reason}" in result.reasons


def test_request_faults_stay_4xx_and_the_r4_gate_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, app: TestClient
) -> None:
    """The wrong hook, too many orders, a malformed field, a context that cannot fill a
    template, and neither prefetch nor fhirServer are the request's faults: a `4xx`, nothing
    decided or recorded. A structure error in what the client sent stays a `400`; in what
    Mendari read itself it is `not_r4` (above)."""
    request, _ = ehr_failing(monkeypatch, None)

    def sent(change: Callable[[dict[str, Any]], None]) -> int:
        body = copy.deepcopy(request)
        change(body)
        return app.post(SERVICE, json=body).status_code

    def many(body: dict[str, Any]) -> None:
        order = body["context"]["draftOrders"]["entry"][0]["resource"]
        orders = [{"resource": {**order, "id": f"o{n}"}} for n in range(cds_hooks.MAX_ORDERS + 1)]
        body["context"]["draftOrders"]["entry"] = orders

    def no_server(body: dict[str, Any]) -> None:
        del body["fhirServer"]

    def no_patient(body: dict[str, Any]) -> None:
        del body["context"]["patientId"]

    def malformed(value: Any) -> Callable[[dict[str, Any]], None]:
        return lambda body: body.update(prefetch={"labs": value})

    bad = {"resourceType": "Observation", "id": "o1", "status": 7, "code": {}}
    assert sent(lambda body: body.update(hook="patient-view")) == 422
    assert sent(many) == 400
    assert sent(lambda body: body.update(context=[])) == 422
    assert sent(lambda body: body.update(fhirAuthorization={"access_token": 7})) == 422
    assert sent(no_patient) == 422
    assert sent(no_server) == 412
    # The structural check runs on every value the client sent before any is set aside as an
    # OperationOutcome or as not a resource: each of these was, and stays, a `400`.
    patient = {"resourceType": "Patient", "id": "ref-p1", "gender": 7}
    assert sent(lambda body: body.update(prefetch={"patient": patient})) == 400
    assert sent(malformed({"resourceType": "OperationOutcome", "issue": 5})) == 400
    assert sent(malformed([bad])) == 400
    assert sent(malformed({"o1": bad})) == 400
    assert sent(malformed({"resourceType": 5})) == 400
    assert sent(malformed(_bundle(bad))) == 400
    assert app.get("/api/audit").json() == []


def test_an_unreadable_abstain_shows_a_card_whatever_show_says(app: TestClient) -> None:
    """The trial pack shows cards for `candidate` and `needs_review` only, so an ABSTAIN (no
    recommendation) would be silent; over unreadable data it shows an `info` card, so the
    clinician knows the record was not checked (owner decision 6)."""
    pack = load_pack(ROOT / "tests" / "fixtures" / "reference-packs" / "clinical-trial-prescreener")
    hooks = pack.manifest.cds_hooks
    assert hooks is not None and hooks.card is not None and hooks.card.show is not None
    assert "none" not in hooks.card.show
    body = patient_view(trial_record("Seen for diabetes follow-up."))
    body["prefetch"]["conditions"] = FAULTS["operation_outcome"](httpx.Request("GET", EHR)).json()
    [card] = app.post(TRIAL, json=body).json()["cards"]
    assert card["indicator"] == "info"
    assert card["summary"] == cds_hooks.UNREADABLE_SUMMARY
    assert "- conditions: operation_outcome" in card["detail"]
    assert pack.status_label in card["detail"]  # every card says the pack's status


def _crd(prefetch: dict[str, Any]) -> dict[str, Any]:
    return cds_hooks.invoke(
        Decision.from_hub(PA),
        {
            "hook": "order-sign",
            "hookInstance": "d1577c69-dfbe-44ad-ba6d-3e05e953b2ea",
            "context": {
                "userId": "Practitioner/1",
                "patientId": "p1",
                "draftOrders": _bundle(_order("mri", "FX101")),
            },
            "prefetch": prefetch,
        },
    )


def test_a_failed_read_gives_indeterminate_technical_coverage_when_coverage_was_read() -> None:
    """CRD 2.2.1: coverage that could not be determined for technical reasons is `covered:
    indeterminate` with reason `technical` and its text (crd-ci-q9; Inferno's Technical Issues
    group). Only when the Coverage itself was read: without it there is no Coverage to name, so
    no system action, and the card says the Coverage could not be read."""
    outcome = FAULTS["operation_outcome"](httpx.Request("GET", EHR)).json()
    prefetch = _prefetch("acme", "Completed 8 weeks of physical therapy.")
    response = _crd({**prefetch, "conditions": outcome})
    [action] = response["systemActions"]
    [extension] = action["resource"]["extension"]
    parts = {part["url"]: part for part in extension["extension"]}
    assert list(parts) == ["coverage", "covered", "reason", "date", "coverage-assertion-id"]
    assert parts["covered"]["valueCode"] == "indeterminate"
    reason = parts["reason"]["valueCodeableConcept"]
    assert reason["coding"] == [{"system": f"{davinci.CRD}/CodeSystem/temp", "code": "technical"}]
    assert reason["text"] == davinci.TECHNICAL_TEXT
    assert parts["coverage"]["valueReference"] == {"reference": "Coverage/c1"}
    [card] = response["cards"]
    assert parts["coverage-assertion-id"]["valueString"] == card["extension"]["mendari.decision_id"]
    assert card["indicator"] == "info"
    without = _crd({**prefetch, "coverage": outcome})
    assert "systemActions" not in without
    assert "- coverage: operation_outcome" in without["cards"][0]["detail"]


# -- SMART decide ------------------------------------------------------------------------------


class FailingEhr(FakeEhr):
    """The fake EHR, answering one resource type's reads with `status`, or with the JSON body
    in `bodies`, instead."""

    def __init__(self) -> None:
        super().__init__()
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


@pytest.fixture
def smart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Any]]:
    ehr = FailingEhr()
    monkeypatch.setenv("MENDARI_SMART_CLIENT_ID", CLIENT)
    monkeypatch.setenv("MENDARI_PUBLIC_URL", APP)
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", "https://ehr.example.org")
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
        full_launch(client, ehr)
        yield client, ehr


def test_smart_decide_abstains_on_a_failed_prefetch_read(smart: tuple[TestClient, Any]) -> None:
    """The launch's decide reads each prefetch key on its own, as a hook does: one that fails
    abstains with its reason. The focus resource failing is an error, as before (there is
    nothing to decide about), and a `401` still ends the launch."""
    app, ehr = smart
    body = {"pack_id": REFERRAL, "focus": "ServiceRequest/ref-sr1"}
    ehr.failing = {"Observation": 500}
    response = app.post("/api/smart/decide", json=body)
    assert response.status_code == 200, response.text
    decision = response.json()["decision"]
    assert decision["outcome"] == "ABSTAIN"
    assert decision["reasons"] == ["could not read labs: http_status"]
    assert decision["data_coverage"]["lists"]["labs"]["unreadable"] == {"http_status": 1}
    assert SERVER_TEXT not in response.text
    assert app.get(f"/api/audit/{decision['id']}").status_code == 200
    ehr.failing = {"ServiceRequest": 500}
    assert app.post("/api/smart/decide", json=body).status_code == 422  # as before
    ehr.failing = {"Observation": 401}
    assert app.post("/api/smart/decide", json=body).status_code == 401
    assert app.get("/api/smart/session").json() == {"active": False}  # the launch ended


@pytest.mark.parametrize(("page", "reason"), MALFORMED_PAGES)
def test_smart_decide_abstains_on_a_search_page_that_is_not_r4(
    smart: tuple[TestClient, Any], page: dict[str, Any], reason: str
) -> None:
    """The launch's decide reads its record as a hook does: a search page that is not R4's
    shape makes the key unreadable, never a `500`."""
    app, ehr = smart
    ehr.bodies = {"Observation": {"resourceType": "Bundle", "type": "searchset", **page}}
    body = {"pack_id": REFERRAL, "focus": "ServiceRequest/ref-sr1"}
    response = app.post("/api/smart/decide", json=body)
    assert response.status_code == 200, response.text
    decision = response.json()["decision"]
    assert decision["outcome"] == "ABSTAIN"
    assert decision["reasons"] == [f"could not read labs: {reason}"]


@pytest.mark.parametrize("unread", ["timeout", "not_fetched"])
def test_audit_verify_passes_after_an_abstain(
    db_target: str, capsys: pytest.CaptureFixture[str], unread: str
) -> None:
    """`mendari audit verify` over a trail holding an unreadable ABSTAIN, on both databases."""
    store = AuditStore(db_target)
    bundle = json.loads((ROOT / "tests/fixtures/us-core/referral.json").read_text())
    if unread == "not_fetched":
        trial = Decision.from_hub("mendari/clinical-trial-prescreener", store=store)
        resources = trial_record("Seen for diabetes follow-up.")
        note = next(r for r in resources if r["resourceType"] == "DocumentReference")
        note["content"] = [{"attachment": {"url": "Binary/n1"}}]
        result, _ = trial.decide_fhir(_bundle(*resources))
    else:
        referral = Decision.from_hub("mendari/referral-router", store=store)
        result, _ = referral.decide_fhir(bundle, unreadable={"labs": "timeout"})
    assert result.outcome is Outcome.ABSTAIN
    store.close()
    capsys.readouterr()
    assert main(["audit", "verify", "--db", db_target]) == 0
    assert '"ok": true' in capsys.readouterr().out


def test_a_prefetch_patient_other_than_the_hooks_patient_is_refused(app: TestClient) -> None:
    """A decision is never made on one patient's record for another: a Patient in the prefetch,
    read or in a search, that is not `context.patientId` refuses the request, and nothing is
    decided or recorded."""
    body = patient_view(trial_record("Seen for diabetes follow-up."))
    assert app.post(TRIAL, json=body).status_code == 200
    for key, value in body["prefetch"].items():
        if isinstance(value, dict) and value.get("resourceType") == "Patient":
            other = copy.deepcopy(body)
            other["prefetch"][key]["id"] = "someone-else"
            refused = app.post(TRIAL, json=other)
            assert refused.status_code == 422
            assert "context.patientId" in refused.text and "someone-else" not in refused.text
            break
    else:
        raise AssertionError("the trial request holds no Patient")
    search = copy.deepcopy(body)
    search["prefetch"]["extra"] = _bundle({"resourceType": "Patient", "id": "p2"})
    assert app.post(TRIAL, json=search).status_code == 422
