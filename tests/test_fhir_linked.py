# SPDX-License-Identifier: Apache-2.0
"""Linked record data (LP-23h): what a spec 0.3 mapping reaches by a link the record does not
hold, a note's Binary, the payer behind a Coverage and the drug behind a MedicationRequest, read
from the EHR's FHIR server (`standards/linked.py`). Each read goes through the allow-listed client,
stays under the request's `fhirServer`, reads a Binary as FHIR JSON only, is capped per decision
and in size, and is made once a request; a link not read is its field's reason and the decision
abstains, and `fhir/decide` and `$decide` never fetch. Every record is synthetic, every system
fictional, and the keys and tokens are made here."""

from __future__ import annotations

import base64
import copy
import functools
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, load_pack
from mendari.core import site_codes
from mendari.core.decision import DecisionResult
from mendari.core.phi import PhiPolicy
from mendari.core.registry import SearchPath
from mendari.core.spec import Outcome
from mendari.errors import FhirError
from mendari.server.app import create_app
from mendari.server.smart_routes import decide_for
from mendari.settings import FhirValidation
from mendari.standards import cds_hooks, linked
from mendari.standards.fhir import FhirAdapter, renditions, unread_attachments
from mendari.standards.fhir_client import FhirClient
from mendari.standards.linked import MAX_NOTE_READS, Linker, reads_links
from tests.conftest import INPUT_HASH_KEY, ROOT
from tests.test_spec_03 import mini

EHR = "https://ehr.example.org/fhir"
ORIGIN = "https://ehr.example.org"
TOKEN = "linked-test-token"  # made for these tests; no server outside them takes it
MED = "https://site.example/fhir/CodeSystem/medications"  # fictional
PAYER = "https://site.example/fhir/NamingSystem/payers"  # fictional
SITE_FORMULARY = "https://site.example/fhir/CodeSystem/formulary"  # a site's own drug codes
PACK = "mendari-test/prescreen-mini"
SERVICE = "/cds-services/mendari-test.prescreen-mini"
PATIENT = {"resourceType": "Patient", "id": "p1", "birthDate": "1970-01-01"}
NOTE_MARK = "NOTEMARK-amber-wren"  # in linked text: never in a reason, a record or the chain
LINK_MARK = "LNK8842"  # in linked ids, urls, codes and identifiers: likewise
SERVER_TEXT = "SERVER-SAYS-p1-HAS-SECRETS"  # what a failing server answers

NOTES = {"path": "DocumentReference.content.attachment", "transform": "attachment_text"}
PAYER_FIELD = {
    "follow": "Coverage.payor[0]",
    "path": f"Organization.identifier[system={PAYER}].value",
}
MEDICATIONS: dict[str, Any] = {
    "resources": {
        "MedicationRequest": {
            "fields": {
                "code": {
                    "follow": "medicationReference",
                    "path": f"Medication.code.coding[system={MED}].code",
                }
            }
        }
    }
}
PREFETCH = {
    "coverage": "Coverage?patient={{context.patientId}}",
    "medications": "MedicationRequest?patient={{context.patientId}}",
}

Handler = Callable[[httpx.Request], httpx.Response]


def linked_pack(tmp_path: Path, medications: dict[str, Any] = MEDICATIONS) -> Path:
    """The prescreen fixture at spec 0.3, reading its notes as `attachment_text`, the payer by
    its Coverage's reference and each MedicationRequest's drug by its `medicationReference`
    (`medications`, its list); its hook prefetches the Coverage and the MedicationRequests too,
    and shows every card."""
    root = mini(
        tmp_path,
        {
            "fields": {"notes": NOTES, "patient.payer": PAYER_FIELD},
            "lists": {"medications": medications},
        },
        spec="0.3",
        systems=(MED, PAYER),
    )
    manifest = yaml.safe_load((root / "pack.yaml").read_text())
    manifest["cds_hooks"]["prefetch"].update(PREFETCH)
    del manifest["cds_hooks"]["card"]["show"]
    (root / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    schema_path = root / "schemas" / "input.schema.json"
    schema = json.loads(schema_path.read_text())
    schema["properties"]["medications"] = {"type": "array", "items": {"type": "object"}}
    schema_path.write_text(json.dumps(schema))
    return root


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def binary(id_: str, text: str, kind: str = "text/plain") -> dict[str, Any]:
    return {"resourceType": "Binary", "id": id_, "contentType": kind, "data": b64(text)}


def note(id_: str, *attachments: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceType": "DocumentReference",
        "id": id_,
        "status": "current",
        "content": [{"attachment": attachment} for attachment in attachments],
    }


def linking(url: str, kind: str = "text/plain") -> dict[str, Any]:
    """An attachment that is only a link."""
    return {"contentType": kind, "url": url}


def coverage(*payors: str, contained: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    found: dict[str, Any] = {
        "resourceType": "Coverage",
        "id": "cov1",
        "status": "active",
        "beneficiary": {"reference": "Patient/p1"},
        "payor": [{"reference": payor} for payor in payors],
    }
    if contained:
        found["contained"] = contained
    return found


def organization(id_: str, payer: str) -> dict[str, Any]:
    return {
        "resourceType": "Organization",
        "id": id_,
        "active": True,
        "name": "A fictional payer",
        "identifier": [{"system": PAYER, "value": payer}],
    }


def medication(id_: str, code: str) -> dict[str, Any]:
    return {
        "resourceType": "Medication",
        "id": id_,
        "code": {"coding": [{"system": MED, "code": code}]},
    }


def request_for(id_: str, reference: str) -> dict[str, Any]:
    return {
        "resourceType": "MedicationRequest",
        "id": id_,
        "status": "active",
        "intent": "order",
        "subject": {"reference": "Patient/p1"},
        "medicationReference": {"reference": reference},
    }


def bundle(*resources: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{"resource": resource} for resource in (PATIENT, *resources)],
    }


def searchset(*resources: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": resource} for resource in resources],
    }


def fhir_json(body: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=body, headers={"content-type": "application/fhir+json"})


class Ehr:
    """A FHIR server at `EHR`: a read by `Type/id` answers that resource (else a `404`), a
    search the record's resources of its type; `faults` answers a path its own way. Every
    request is kept."""

    def __init__(self, *resources: dict[str, Any], record: tuple[dict[str, Any], ...] = ()):
        self.resources = {f"{r['resourceType']}/{r['id']}": r for r in resources}
        self.record = [PATIENT, *record]
        self.faults: dict[str, Handler] = {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/fhir/")
        if path in self.faults:
            return self.faults[path](request)
        if "/" not in path:  # a search
            return fhir_json(searchset(*(r for r in self.record if r["resourceType"] == path)))
        found = self.resources.get(path) or {"Patient/p1": PATIENT}.get(path)
        if found is None:
            outcome = {
                "resourceType": "OperationOutcome",
                "issue": [{"severity": "error", "code": "not-found", "diagnostics": SERVER_TEXT}],
            }
            return fhir_json(outcome, status=404)
        return fhir_json(found)

    def reads(self) -> list[str]:
        """The reads by id, as paths under the base, in order."""
        paths = [r.url.path.removeprefix("/fhir/") for r in self.requests]
        return [path for path in paths if "/" in path]

    def client(self, base: str = EHR) -> FhirClient:
        return FhirClient(base, token=TOKEN, allowed={ORIGIN}, transport=httpx.MockTransport(self))


@pytest.fixture
def pack(tmp_path: Path) -> Decision:
    return Decision(load_pack(linked_pack(tmp_path / "packs")))


def decide(
    decision: Decision, *resources: dict[str, Any], ehr: Ehr | None = None
) -> DecisionResult:
    links = None if ehr is None else Linker.of(ehr.client())
    result, _ = decision.decide_fhir(bundle(*resources), links=links)
    return result


def mapped(decision: Decision, ehr: Ehr | None, *resources: dict[str, Any]) -> dict[str, Any]:
    """The pack's input from the record, after the linked reads when there is an EHR."""
    adapter = FhirAdapter.for_pack(decision.pack)
    indexed = adapter.index(bundle(*resources))
    if ehr is not None:
        Linker.of(ehr.client()).read(adapter.mapping, indexed)
    return adapter.to_input(indexed)


def serve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ehr: Ehr) -> TestClient:
    """The app with the linked pack, and `ehr` as the allow-listed EHR behind every
    `fhirServer`."""
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", ORIGIN)
    patched = functools.partial(FhirClient, transport=httpx.MockTransport(ehr))
    monkeypatch.setattr(cds_hooks, "FhirClient", patched)
    linked_pack(tmp_path / "packs")
    packs = [SearchPath(tmp_path / "packs", "local")]
    return TestClient(create_app(db_path=tmp_path / "a.db", packs_paths=packs))


def hook(*notes: dict[str, Any], server: str | None = EHR, **record: Any) -> dict[str, Any]:
    """A patient-view request with the whole prefetch, so only linked data is read."""
    request: dict[str, Any] = {
        "hook": "patient-view",
        "hookInstance": "6b1d7c35-49f2-4c3e-9d1e-3a4c1f0b2d11",
        "context": {"userId": "Practitioner/1", "patientId": "p1"},
        "prefetch": {
            "patient": PATIENT,
            "conditions": searchset(),
            "observations": searchset(),
            "notes": searchset(*notes),
            "coverage": searchset(*record.get("coverage", ())),
            "medications": searchset(*record.get("medications", ())),
        },
    }
    if server is not None:
        request["fhirServer"] = server
        request["fhirAuthorization"] = {
            "access_token": TOKEN,
            "token_type": "Bearer",
            "scope": "patient/*.rs",
        }
    return request


def audited(app: TestClient, response: Any) -> dict[str, Any]:
    """The audited record of the one card a hook answered with."""
    assert response.status_code == 200, response.text
    [card] = response.json()["cards"]
    found = app.get(f"/api/audit/{card['extension']['mendari.decision_id']}")
    assert found.status_code == 200, found.text
    return dict(found.json()["record"])


def test_a_linked_note_is_read_through_the_allow_listed_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CDS hook's note that is only a link to a Binary, relative or an absolute URL under the
    `fhirServer`, is read from that server: one GET each, with the request's token and `Accept:
    application/fhir+json`, its text reaching the decision (the note screen finds
    pancreatitis), and the note counted as read. A Binary the server answers with its raw bytes
    instead of FHIR JSON is not read (`not_json`)."""
    ehr = Ehr(
        binary("b1", "History of pancreatitis in 2019."),
        binary("b2", "<p>Seen for diabetes follow-up.</p>", "text/html"),
    )
    app = serve(tmp_path, monkeypatch, ehr)
    request = hook(
        note("n1", linking("Binary/b1")),
        note("n2", linking(f"{EHR}/Binary/b2", "text/html")),
    )
    record = audited(app, app.post(SERVICE, json=request))
    assert record["outcome"] != "ABSTAIN" and record["reasons"] != []
    assert record["answers"]["note_pancreatitis"]["label"] == "yes"
    notes = record["data_coverage"]["fields"]["notes"]
    assert (notes["values"], notes["unreadable"]) == (2, {})
    assert ehr.reads() == ["Binary/b1", "Binary/b2"]
    assert {r.method for r in ehr.requests} == {"GET"}
    assert {r.headers["accept"] for r in ehr.requests} == {"application/fhir+json"}
    assert {r.headers["authorization"] for r in ehr.requests} == {f"Bearer {TOKEN}"}
    assert {str(r.url.copy_with(path="/")) for r in ehr.requests} == {f"{ORIGIN}/"}

    ehr.requests.clear()
    ehr.faults["Binary/b1"] = lambda r: httpx.Response(
        200, content=b"History of pancreatitis.", headers={"content-type": "text/plain"}
    )
    record = audited(app, app.post(SERVICE, json=hook(note("n1", linking("Binary/b1")))))
    assert record["outcome"] == "ABSTAIN"
    assert record["reasons"] == ["could not read notes: not_json"]
    assert record["data_coverage"]["fields"]["notes"]["unreadable"] == {"not_json": 1}


@pytest.mark.parametrize(
    "url",
    [
        "https://elsewhere.example/fhir/Binary/b1",  # another server
        "https://ehr.example.org/other/Binary/b1",  # the EHR's origin, outside its base
        f"{EHR}/../admin/Binary/b1",  # out of the base by `..`
        "http://ehr.example.org/fhir/Binary/b1",  # another scheme
        "HTTPS://elsewhere.example/fhir/Binary/b1",  # another server, its scheme in capitals
    ],
)
def test_an_attachment_off_the_fhir_server_is_never_fetched_and_counts_unreadable(
    pack: Decision, url: str
) -> None:
    """A link anywhere but under the `fhirServer`'s base is `off_server`: nothing is sent for
    it, the note counts it as unreadable and the decision abstains, the url in no reason."""
    ehr = Ehr(binary("b1", "Seen today."))
    result = decide(pack, note("n1", linking(url)), ehr=ehr)
    assert ehr.requests == []
    assert result.outcome.value == "ABSTAIN"
    assert result.reasons == ("could not read notes: off_server",)
    assert result.answers == {}  # no provider ran
    assert result.data_coverage is not None
    assert result.data_coverage["fields"]["notes"]["unreadable"] == {"off_server": 1}
    assert url not in json.dumps(result.to_record())


def test_a_fhir_server_off_the_allow_list_reads_no_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `fhirServer` not on the allow-list is configuration, not the patient's data (conflict
    14): for a pack that reads links, the hook is refused (`422`) before anything is decided,
    with nothing fetched or recorded, whether its record holds every link or not, as it is when
    a prefetch key is left out. With the server listed, the same request decides."""
    ehr = Ehr(binary("b1", "Seen today."))
    app = serve(tmp_path, monkeypatch, ehr)
    inline = {"contentType": "text/plain", "data": b64("Seen today.")}
    other = "https://other.example.org/fhir"
    for request in (
        hook(note("n1", linking("Binary/b1")), server=other),  # a link to read
        hook(note("n1", inline), server=other),  # every link in the record
    ):
        response = app.post(SERVICE, json=request)
        assert response.status_code == 422, response.text
        assert "allow-list" in response.text
        missing = copy.deepcopy(request)
        del missing["prefetch"]["notes"]  # the prefetch's own read gives the same answer
        assert app.post(SERVICE, json=missing).status_code == 422
    assert ehr.requests == [] and app.get("/api/audit").json() == []
    record = audited(app, app.post(SERVICE, json=hook(note("n1", linking("Binary/b1")))))
    assert record["outcome"] != "ABSTAIN" and ehr.reads() == ["Binary/b1"]


def test_linked_reads_are_capped_per_decision_and_in_size(
    pack: Decision, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At most `MAX_NOTE_READS` note links a decision: past them, a link is `cap` and is not
    read (the budgets by purpose and per request: `tests/test_epic_data_fit.py`). Each read is
    at most `MAX_BYTES` once decoded (a Binary's content, measured exactly; another resource's
    JSON as it arrives), else `too_large`; the decision abstains on either."""
    many = MAX_NOTE_READS + 5
    ehr = Ehr(*(binary(f"b{n}", f"Note {n}.") for n in range(many)))
    notes = [note(f"n{n}", linking(f"Binary/b{n}")) for n in range(many)]
    result = decide(pack, *notes, ehr=ehr)
    assert MAX_NOTE_READS == 50 and len(ehr.reads()) == MAX_NOTE_READS
    assert result.reasons == ("could not read notes: cap",)
    assert result.data_coverage is not None
    assert result.data_coverage["fields"]["notes"]["unreadable"] == {"cap": many - MAX_NOTE_READS}

    ehr = Ehr(*(binary(f"b{n}", f"Note {n}.") for n in range(MAX_NOTE_READS)))
    result = decide(pack, *notes[:MAX_NOTE_READS], ehr=ehr)
    assert result.unreadable == () and len(ehr.reads()) == MAX_NOTE_READS  # the cap itself reads

    monkeypatch.setattr(linked, "MAX_BYTES", 200)
    ehr = Ehr(
        binary("full", "x" * 200),  # exactly the cap, decoded
        binary("over", "x" * 201),  # one byte past it
        binary("wire", "x" * 300),  # its answer is past twice the cap
        medication("m1", "y" * 200),  # its JSON is past the cap
    )
    assert mapped(pack, ehr, note("n1", linking("Binary/full")))["notes"] == "x" * 200
    for name in ("over", "wire"):
        result = decide(pack, note("n1", linking(f"Binary/{name}")), ehr=ehr)
        assert result.reasons == ("could not read notes: too_large",), name
    result = decide(pack, request_for("r1", "Medication/m1"), ehr=ehr)
    assert result.reasons == ("could not read medications.code: too_large",)


def test_html_is_read_as_text_and_rtf_abstains(pack: Decision) -> None:
    """A linked text/html Binary is read as text, its scripts dropped. An RTF note abstains
    (`unsupported_type`), and is not fetched: its content type says so first. A link said to be
    text whose Binary is RTF is fetched, and abstains. Renditions of one document (a
    DocumentReference's `content`) are one note: once one reads, the others (an RTF beside the
    HTML, a second text) are neither read nor counted, and only a note with no rendition read
    abstains."""
    html = "<script>alert('pancreatitis')</script><p>Seen for diabetes follow-up.</p>"
    ehr = Ehr(
        binary("h1", html, "text/html"),
        binary("p1", "Seen today.", "text/plain"),
        binary("r1", "{\\rtf1 Pancreatitis.}", "application/rtf"),
    )
    found = mapped(pack, ehr, note("n1", linking("Binary/h1", "text/html")))
    assert found["notes"] == "Seen for diabetes follow-up."
    assert ehr.reads() == ["Binary/h1"]

    ehr.requests.clear()
    result = decide(pack, note("n1", linking("Binary/r1", "application/rtf")), ehr=ehr)
    assert result.reasons == ("could not read notes: unsupported_type",)
    assert ehr.reads() == []
    result = decide(pack, note("n1", linking("Binary/r1")), ehr=ehr)  # said to be text
    assert result.reasons == ("could not read notes: unsupported_type",)
    assert ehr.reads() == ["Binary/r1"]

    for first, second, read in (
        (linking("Binary/h1", "text/html"), linking("Binary/r1", "application/rtf"), "h1"),
        (linking("Binary/r1", "application/rtf"), linking("Binary/h1", "text/html"), "h1"),
        (linking("Binary/p1"), linking("Binary/h1", "text/html"), "p1"),
        (linking("https://elsewhere.example/fhir/Binary/x"), linking("Binary/p1"), "p1"),
    ):
        ehr.requests.clear()
        result = decide(pack, note("n1", first, second), ehr=ehr)
        assert result.unreadable == (), (first, second)
        assert result.answers, (first, second)  # decided: the providers ran
        assert ehr.reads() == [f"Binary/{read}"], (first, second)
    result = decide(pack, note("n1", linking("Binary/r1", "application/rtf"), {}), ehr=ehr)
    assert result.reasons == ("could not read notes: unsupported_type",)

    # Elsewhere, two attachments are two contents: one that cannot be read still counts.
    pairs = [(object(), {"contentType": "text/plain", "data": b64("A.")})]
    pairs.append((pairs[0][0], {"contentType": "application/pdf", "data": b64("B")}))
    assert renditions("DocumentReference.content.attachment")
    assert renditions("DiagnosticReport.presentedForm")
    assert not renditions("Communication.payload.contentAttachment")
    assert unread_attachments("DocumentReference.content.attachment", pairs, {}) == []
    assert unread_attachments("Communication.payload.contentAttachment", pairs, {}) == [
        "unsupported_type"
    ]


def test_a_note_url_to_another_type_or_an_empty_binary_is_unsupported_type(pack: Decision) -> None:
    """A note's url that names another type than a Binary is `unsupported_type`, never read,
    whether it is relative or an absolute URL under the base, and so is one that names a
    resource of another type inside the record; with no server to read from, one the record
    does not hold is `not_fetched`. A Binary with no content (R4's `data` is 0..1), read or in
    the record, is `unsupported_type` too: it was read, so it is not `not_fetched`."""
    empty = {"resourceType": "Binary", "id": "b0", "contentType": "text/plain"}
    ehr = Ehr(empty, note("x", linking("Binary/b0")))
    for url in (
        "Patient/p9",
        f"{EHR}/Patient/p9",
        "DocumentReference/x",
        f"{EHR}/DocumentReference/x",
    ):
        ehr.requests.clear()
        result = decide(pack, note("n1", linking(url)), ehr=ehr)
        assert result.reasons == ("could not read notes: unsupported_type",), url
        assert ehr.requests == [], url
        unread = decide(pack, note("n1", linking(url)))
        assert unread.reasons == ("could not read notes: not_fetched",), url
    inside = decide(pack, note("n1", linking("Patient/p1")))  # the record's own Patient
    assert inside.reasons == ("could not read notes: unsupported_type",)

    ehr.requests.clear()
    result = decide(pack, note("n1", linking("Binary/b0")), ehr=ehr)
    assert result.reasons == ("could not read notes: unsupported_type",)
    assert ehr.reads() == ["Binary/b0"]
    held = decide(pack, note("n1", linking("Binary/b0")), empty)
    assert held.reasons == ("could not read notes: unsupported_type",)
    assert held.data_coverage is not None
    assert held.data_coverage["fields"]["notes"]["unreadable"] == {"unsupported_type": 1}


@pytest.mark.parametrize("mode", ["warn", "enforce"])
def test_a_linked_resource_is_checked_as_the_record_is(
    pack: Decision, mode: FhirValidation
) -> None:
    """What a linked read reads is checked as the record is (`fhir_validation`): a payer whose
    identifier's `use` is outside its required value set (an R4 issue that does not block) is
    recorded under `warn`, and under `enforce` refuses the input before any provider, where the
    record alone decides. The stored record names it by its type alone, never by the id its link
    names."""
    patient = {
        **PATIENT,
        "identifier": [{"system": "https://site.example/fhir/mrn", "value": "M1"}],
        "name": [{"family": "Test"}],
        "gender": "unknown",
    }
    held = {
        **coverage(f"Organization/{LINK_MARK}-o1"),
        "relationship": {
            "coding": [
                {
                    "system": "http://terminology.hl7.org/CodeSystem/subscriber-relationship",
                    "code": "self",
                }
            ]
        },
        "subscriberId": "S1",
    }
    record = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{"resource": patient}, {"resource": held}],
    }
    strict = Decision(pack.pack, fhir_validation=mode)
    payer = organization(f"{LINK_MARK}-o1", "ACME-77")
    clean, _ = strict.decide_fhir(record, links=Linker.of(Ehr(payer).client()))
    assert clean.input_issues == () and clean.answers  # the record and its payer decide

    bad = copy.deepcopy(payer)
    bad["identifier"][0]["use"] = "not-a-use"
    ehr = Ehr(bad)
    result, _ = strict.decide_fhir(record, links=Linker.of(ehr.client()))
    assert ehr.reads() == [f"Organization/{LINK_MARK}-o1"]
    stored = result.to_record()
    expression = "Organization.identifier[0].use"
    issues = [(i["resource"], i["code"], i["expression"]) for i in stored["input_issues"]]
    assert issues == [("Organization", "code-invalid", expression)]
    assert LINK_MARK not in json.dumps(stored)
    if mode == "enforce":
        assert result.rejected and result.answers == {}
        assert stored["reasons"] == [
            f"input rejected: not R4 Organization: Organization {expression}: "
            "use is not a code from its required value set"
        ]
    else:
        assert not result.rejected and result.answers


def test_the_payer_is_read_by_its_coverage_reference(pack: Decision) -> None:
    """`Coverage.payor[0]` reaches its Organization: contained, in the record, or by one read
    under the server (relative, or an absolute URL under the base, its scheme in any case, which
    finds an Organization the record holds without a read). A payor that is not of the type the
    field reads (the patient paying) is not read and is not unreadable; a later payor is not
    read; a failed read abstains with its reason, and a payor on another server, however its
    scheme is spelled, is `off_server` (`not_fetched` with no server), never read. An
    Organization the record holds is not among the resources the linker read for the decision,
    so it is not checked twice."""
    ehr = Ehr(organization("o1", "ACME-77"), organization("o2", "OTHER-1"))
    payer = organization("o1", "ACME-77")
    for record, reads in (
        ((coverage("Organization/o1", "Organization/o2"),), ["Organization/o1"]),
        ((coverage(f"{EHR}/Organization/o1"),), ["Organization/o1"]),
        ((coverage("HTTPS://ehr.example.org/fhir/Organization/o1"),), ["Organization/o1"]),
        ((coverage("#o1", contained=[payer]),), []),
        ((coverage("Organization/o1"), payer), []),
        ((coverage(f"{EHR}/Organization/o1"), payer), []),
    ):
        ehr.requests.clear()
        assert mapped(pack, ehr, *record)["patient"]["payer"] == "ACME-77", record
        assert ehr.reads() == reads, record
        adapter = FhirAdapter.for_pack(pack.pack)
        read = Linker.of(ehr.client()).read(adapter.mapping, adapter.index(bundle(*record)))
        assert [f"{r['resourceType']}/{r['id']}" for r in read] == reads, record

    ehr.requests.clear()
    result = decide(pack, coverage("Patient/p1"), ehr=ehr)
    assert result.unreadable == () and ehr.reads() == []
    assert "payer" not in mapped(pack, ehr, coverage("Patient/p1")).get("patient", {})

    result = decide(pack, coverage("Organization/missing"), ehr=ehr)
    assert result.reasons == ("could not read patient.payer: http_status",)
    assert SERVER_TEXT not in json.dumps(result.to_record())
    assert result.data_coverage is not None
    payer_record = result.data_coverage["fields"]["patient.payer"]
    assert payer_record["follow"] == {"path": "Coverage.payor[0]", "reached": 0, "references": 1}
    assert payer_record["unreadable"] == {"http_status": 1}

    for spelled in ("https", "HTTPS", "Https"):
        elsewhere = coverage(f"{spelled}://elsewhere.example/fhir/Organization/o1")
        ehr.requests.clear()
        result = decide(pack, elsewhere, ehr=ehr)
        assert result.reasons == ("could not read patient.payer: off_server",), spelled
        assert ehr.requests == [], spelled
        assert decide(pack, elsewhere).reasons == ("could not read patient.payer: not_fetched",)


def test_a_medication_reference_is_read_once(pack: Decision) -> None:
    """Each Medication is read once a request, however many MedicationRequests name it and
    however they spell it (relative, or an absolute URL under the base), across the decisions of
    one request (the draft orders of one order hook share one linker)."""
    ehr = Ehr(medication("m1", "RX-1"), medication("m2", "RX-2"))
    requests = (
        request_for("r1", "Medication/m1"),
        request_for("r2", "Medication/m1"),
        request_for("r3", f"{EHR}/Medication/m1"),
        request_for("r4", "Medication/m2"),
    )
    links = Linker.of(ehr.client())
    results = [pack.decide_fhir(bundle(*requests), links=links)[0] for _ in range(2)]
    assert ehr.reads() == ["Medication/m1", "Medication/m2"]
    assert [result.unreadable for result in results] == [(), ()]
    medications = [item["code"] for item in mapped(pack, ehr, *requests)["medications"]]
    assert medications == ["RX-1", "RX-1", "RX-1", "RX-2"]
    assert results[0].data_coverage is not None
    source = results[0].data_coverage["lists"]["medications"]["sources"]["MedicationRequest"]
    assert source["fields"]["code"]["follow"] == {
        "path": "medicationReference",
        "reached": 4,
        "references": 4,
    }


def test_a_linked_resource_is_read_through_the_organizations_site_code_map(tmp_path: Path) -> None:
    """LP-23h with LP-23i: what a linked read fetched is read with the organization's site code
    map, as the rest of the record is. A drug the EHR codes only in the site's own formulary,
    behind a `medicationReference` the record does not hold, is read from the EHR and then,
    through the map, as the pack's code: the decision records the map, and since its input took
    a value through a translation it goes to a person (`site_code`). The same drug coded the
    pack's own way, and the site's code without a map, are decided by the pack alone, and
    automate."""
    medications = copy.deepcopy(MEDICATIONS)
    field = medications["resources"]["MedicationRequest"]["fields"]["code"]
    field.update(path="Medication.code.coding", code_set="drugs")
    root = linked_pack(tmp_path / "packs", medications)
    mapping_path = root / "fhir" / "mapping.yaml"
    mapping = yaml.safe_load(mapping_path.read_text())
    mapping["code_sets"] = {"drugs": {"system": MED, "codes": ["RX-1"]}}
    mapping_path.write_text(yaml.safe_dump(mapping, sort_keys=False))
    manifest = yaml.safe_load((root / "pack.yaml").read_text())
    manifest["risk"]["level"] = 1  # may automate
    manifest["policy"]["thresholds"]["auto"] = 0.8  # this record's eligibility gives 0.85
    (root / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    pack = load_pack(root)
    translation = {"system": SITE_FORMULARY, "code": "F-77", "as": "RX-1"}
    given = {"default": {"version": "2026-11-06.1", "sets": {"drugs": [translation]}}}
    maps = site_codes.parse(json.dumps(given), signed_in=False)
    site_codes.check(maps, [pack])
    site_drug = {
        "resourceType": "Medication",
        "id": "m1",
        "code": {"coding": [{"system": SITE_FORMULARY, "code": "F-77"}]},
    }
    order = request_for("r1", "Medication/m1")

    ehr = Ehr(site_drug)
    through, _ = Decision(pack, site_codes=maps).decide_fhir(
        bundle(order), links=Linker.of(ehr.client())
    )
    assert ehr.reads() == ["Medication/m1"]
    assert through.unreadable == ()
    assert through.site_codes == maps.maps["default"].stamp()
    assert through.outcome is Outcome.HUMAN_REVIEW
    assert "site code translated (drugs): a person decides" in through.reasons

    ehr = Ehr(medication("m1", "RX-1"))
    own, _ = Decision(pack, site_codes=maps).decide_fhir(
        bundle(order), links=Linker.of(ehr.client())
    )
    ehr = Ehr(site_drug)
    alone, _ = Decision(pack).decide_fhir(bundle(order), links=Linker.of(ehr.client()))
    assert own.outcome is alone.outcome is Outcome.AUTO
    assert own.site_codes == through.site_codes and alone.site_codes is None
    assert not any("site code" in reason for reason in (*own.reasons, *alone.reasons))
    assert through.recommendation == own.recommendation == alone.recommendation


def test_a_request_its_list_leaves_out_is_not_read_for_its_links(tmp_path: Path) -> None:
    """A MedicationRequest entered in error, which a source with `medication_period` leaves
    out of its list (conflict 39), is not an item: the drug it names is not read, uses none of
    the decision's reads, and is neither counted unreadable nor a reason to abstain, with a
    linker or without one. A request the list holds is read, and counted, as before."""
    medications = copy.deepcopy(MEDICATIONS)
    period = {"start": "start", "end": "end"}
    medications["resources"]["MedicationRequest"]["medication_period"] = period
    decision = Decision(load_pack(linked_pack(tmp_path / "packs", medications)))
    ehr = Ehr(medication("m1", "RX-1"))
    gone = {**request_for("r3", "Medication/gone"), "status": "entered-in-error"}
    record = (request_for("r1", "Medication/m1"), gone)
    assert mapped(decision, ehr, *record)["medications"] == [{"code": "RX-1"}]
    assert ehr.reads() == ["Medication/m1"]

    ehr.requests.clear()
    result = decide(decision, *record, ehr=ehr)
    assert ehr.reads() == ["Medication/m1"]
    assert result.unreadable == () and result.answers  # decided: the providers ran
    assert result.data_coverage is not None
    medicated = result.data_coverage["lists"]["medications"]
    assert medicated["unreadable"] == {}
    source = medicated["sources"]["MedicationRequest"]
    assert (source["resources"], source["items"], source["unreadable"]) == (2, 1, {})

    result = decide(decision, *record)  # no linker: only the listed request's link counts
    assert result.reasons == ("could not read medications.code: not_fetched",)
    assert result.data_coverage is not None
    assert result.data_coverage["lists"]["medications"]["unreadable"] == {"not_fetched": 1}
    ehr.requests.clear()
    assert decide(decision, gone, ehr=ehr).unreadable == () and ehr.requests == []


def test_without_a_server_a_link_is_not_fetched_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no server to read from, a link the record does not hold is `not_fetched`, and the
    decision abstains with each field's reason: a hook with no `fhirServer` (its prefetch whole),
    `fhir/decide` and `$decide`, which never fetch, even with the EHR on the allow-list."""
    ehr = Ehr(binary("b1", "Seen today."), organization("o1", "ACME-77"), medication("m1", "RX-1"))
    app = serve(tmp_path, monkeypatch, ehr)
    record = (
        note("n1", linking("Binary/b1")),
        coverage("Organization/o1"),
        request_for("r1", "Medication/m1"),
    )
    expected = [
        "could not read medications.code: not_fetched",
        "could not read notes: not_fetched",
        "could not read patient.payer: not_fetched",
    ]
    request = hook(record[0], server=None, coverage=[record[1]], medications=[record[2]])
    decided = audited(app, app.post(SERVICE, json=request))
    assert decided["reasons"] == expected
    answered = app.post(f"/api/packs/{PACK}/fhir/decide", json=bundle(*record))
    assert answered.status_code == 200, answered.text
    assert answered.json()["decision"]["reasons"] == expected
    operation = app.post(
        "/fhir/$decide",
        json={
            "resourceType": "Parameters",
            "parameter": [
                {"name": "pack", "valueString": PACK},
                {"name": "data", "resource": bundle(*record)},
            ],
        },
    )
    assert operation.status_code == 200, operation.text
    decision_id = operation.json()["entry"][0]["resource"]["identifier"][0]["value"]
    stored = app.get(f"/api/audit/{decision_id}").json()["record"]
    assert stored["reasons"] == expected
    assert stored["data_coverage"]["unreadable"] == {"not_fetched": 3}
    assert ehr.requests == []


NO_ID = {"resourceType": "Binary", "contentType": "text/plain", "data": b64("Seen today.")}


@pytest.mark.parametrize(
    ("reason", "fault"),
    [
        ("network", None),
        ("http_status", lambda r: fhir_json({"error": SERVER_TEXT}, status=500)),
        ("unauthorized", lambda r: fhir_json({"error": SERVER_TEXT}, status=403)),
        ("operation_outcome", lambda r: fhir_json({"resourceType": "OperationOutcome"})),
        ("unsupported_type", lambda r: fhir_json({"resourceType": "Patient", "id": "b1"})),
        ("unsupported_type", lambda r: fhir_json(binary("b2", "Another note."))),  # not b1
        ("unsupported_type", lambda r: fhir_json(NO_ID)),
        ("not_r4", lambda r: fhir_json({"resourceType": "Binary", "id": "b1", "data": 7})),
    ],
)
def test_a_linked_read_that_fails_abstains_with_its_reason(
    pack: Decision, reason: str, fault: Handler | None
) -> None:
    """A linked read that fails is its note's reason, from the closed set, never what the
    server answered: the decision abstains before any provider, recorded like any other."""
    ehr = Ehr(binary("b1", "Seen today."))

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("failed", request=request)

    ehr.faults["Binary/b1"] = fault or broken
    result = decide(pack, note("n1", linking("Binary/b1")), ehr=ehr)
    assert result.reasons == (f"could not read notes: {reason}",)
    assert result.answers == {} and result.outcome.value == "ABSTAIN"
    assert SERVER_TEXT not in json.dumps(result.to_record())


def test_smart_decide_reads_links_with_the_session_token_and_a_401_ends_the_launch(
    pack: Decision,
) -> None:
    """The SMART launch's decide reads its links through the session's client, as a hook does;
    a `401` on one ends the launch (it is raised, for `ehr` to end the session)."""
    ehr = Ehr(binary("b1", "History of pancreatitis."), record=(note("n1", linking("Binary/b1")),))
    result, _ = decide_for(pack, ehr.client(), "p1", "Patient/p1")
    assert result.unreadable == () and result.answers["note_pancreatitis"].label == "yes"
    assert ehr.reads()[-1:] == ["Binary/b1"]  # after the focus and the prefetch
    assert {r.headers["authorization"] for r in ehr.requests} == {f"Bearer {TOKEN}"}
    ehr.faults["Binary/b1"] = lambda r: fhir_json({}, status=401)
    with pytest.raises(FhirError) as ended:
        decide_for(pack, ehr.client(), "p1", "Patient/p1")
    assert ended.value.status == 401


def test_no_note_text_reaches_the_coverage_record_reasons_or_the_chain(
    tmp_path: Path, pack: Decision
) -> None:
    """Marks planted in linked data (a Binary's text, a payer's identifier, a drug's code, the
    links' ids and urls, one off the server) are in no reason, no data-coverage record and no
    row of the trail, under PHI mode `encrypt`."""
    store = AuditStore(
        tmp_path / "a.db",
        phi=PhiPolicy(mode="encrypt", key=os.urandom(32), hash_key=INPUT_HASH_KEY),
    )
    decision = Decision(pack.pack, store=store)
    ehr = Ehr(
        binary(f"{LINK_MARK}-b1", f"Seen today. {NOTE_MARK}."),
        organization(f"{LINK_MARK}-o1", f"{LINK_MARK}-payer"),
        medication(f"{LINK_MARK}-m1", f"{LINK_MARK}-rx"),
    )
    record = (
        note("n1", linking(f"Binary/{LINK_MARK}-b1")),
        note("n2", linking(f"https://elsewhere.example/{LINK_MARK}/Binary/b2")),
        coverage(f"Organization/{LINK_MARK}-o1"),
        request_for("r1", f"{EHR}/Medication/{LINK_MARK}-m1"),
        request_for("r2", f"Medication/{LINK_MARK}-gone"),
    )
    results = [
        decide(decision, *record, ehr=ehr),
        decide(decision, *record),  # no server: every link not fetched
    ]
    assert results[0].reasons == (
        "could not read medications.code: http_status",
        "could not read notes: off_server",
    )
    marks = (LINK_MARK, NOTE_MARK, "elsewhere.example")
    for result in results:
        shown = json.dumps(result.data_coverage) + json.dumps(result.reasons)
        assert not [mark for mark in marks if mark in shown]
        item = store.get(result.id)
        assert item is not None and item["record"]["data_coverage"] == result.data_coverage
    with store._db.session(exclusive=False) as rows:
        kept = json.dumps(
            [rows.all(f"SELECT * FROM {table}") for table in ("decisions", "audit_events")],
            default=str,
        )
    assert not [mark for mark in marks if mark in kept]
    assert store.verify()["ok"] is True
    store.close()


def test_a_0_1_or_0_2_pack_reads_no_link(tmp_path: Path) -> None:
    """A pack before spec 0.3 cannot reach anything by a link: a hook with a `fhirServer` makes no
    linker for a shipped one (the inbox's), and a 0.2 note that is only a link is `not_fetched` as
    before (LP-23d). Of the reference packs LP-23k moved to spec 0.3, the trial and
    prior-authorization packs read links (notes through `attachment_text`, a followed payer or
    drug); the referral router follows none."""
    request = {"fhirServer": EHR}
    linking = set()
    for root in sorted((ROOT / "tests" / "fixtures" / "reference-packs").glob("*/pack.yaml")):
        found = load_pack(root.parent)
        if found.manifest.inputs.fhir is None or found.manifest.cds_hooks is None:
            continue
        reaches = reads_links(FhirAdapter.for_pack(found).mapping)
        if found.manifest.spec_version == "0.3":
            if reaches:
                linking.add(found.id)
            continue
        assert not reaches, found.id
        assert cds_hooks.linker(Decision(found), request) is None, found.id
    assert linking == {"mendari/clinical-trial-prescreener", "mendari/prior-auth-preflight"}
    as_before = Decision(load_pack(mini(tmp_path, {})))
    result = decide(as_before, note("n1", {"url": "Binary/b1"}), ehr=Ehr(binary("b1", "Seen.")))
    assert result.reasons == ("could not read notes: not_fetched",)


def test_the_record_holds_what_was_read_by_its_link_only(pack: Decision) -> None:
    """A resource read for a link is reached by that link: it is not one of the record's
    resources of its type, so no list or unfollowed path reads it, and the data-coverage record
    counts it among what a reference reached, not among the record's Organizations. The same
    record read twice in one request gives the same bytes, with one read."""
    ehr = Ehr(organization("o1", "ACME-77"))
    links = Linker.of(ehr.client())
    first = pack.decide_fhir(bundle(coverage("Organization/o1")), links=links)[0]
    again = pack.decide_fhir(bundle(coverage("Organization/o1")), links=links)[0]
    assert first.data_coverage is not None
    payer = first.data_coverage["fields"]["patient.payer"]
    assert payer["resources"] == {"Coverage": 1, "Organization": 0}
    assert payer["follow"]["reached"] == 1 and payer["values"] == 1
    assert json.dumps(first.data_coverage) == json.dumps(again.data_coverage)
    assert ehr.reads() == ["Organization/o1"]
    adapter = FhirAdapter.for_pack(pack.pack)
    indexed = adapter.index(bundle(coverage("Organization/o1")))
    Linker.of(ehr.client()).read(adapter.mapping, indexed)
    assert "Organization" not in indexed and list(indexed.linked) == ["Organization/o1"]


def test_a_reference_with_a_trailing_line_break_is_neither_a_link_nor_a_resolved_reference() -> (
    None
):
    """`$` matches before a final newline: `Binary/b1\\n` was a link to read and resolved to
    `Binary/b1` inside the record, though it is not a FHIR reference."""
    from mendari.standards.fhir import link_target, resolve

    record = {"Binary": [{"resourceType": "Binary", "id": "b1"}]}  # a record by type
    assert link_target("Binary/b1") == ("Binary", "b1")
    assert link_target(f"{EHR}/Binary/b1/_history/2") == ("Binary", "b1")
    assert link_target("Binary/b1\n") is None
    assert link_target(f"{EHR}/Binary/b1\n") is None
    assert resolve({"reference": "Binary/b1"}, None, record) is not None
    assert resolve({"reference": "Binary/b1\n"}, None, record) is None
