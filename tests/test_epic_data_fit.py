# SPDX-License-Identifier: Apache-2.0
"""Epic data fit (2026-11-08): what the first SMART launch on Epic's public sandbox showed, the
trial pack abstaining for a test patient on three gaps of Mendari's, each pinned here.

- A note's renditions count once (`fhir.unread_notes`): an Epic Clinical Notes
  DocumentReference is assumed to give each note as an HTML and an RTF Binary (assumption A1),
  and a note none of whose renditions is read counts once, by its text rendition's reason; the
  format of a note that cannot be read is counted by a closed label, never as written.
- Linked reads have a budget per purpose (`linked.MAX_NOTE_READS`, `MAX_FOLLOW_READS`), a bound
  per request (`MAX_REQUEST_READS`, `LINK_SECONDS`) and run inside the decision's deadline, so a
  chart's notes no longer use up what its drugs need. An Epic MedicationRequest is assumed to
  name its drug by `medicationReference` with a `display` (assumption A2).
- What an EHR does not offer may be declared (`MENDARI_EHR_DECLARATIONS`): Epic has no R4
  MedicationStatement API and refused the search (403). Undeclared, the refusal abstains, so
  absence is never read as an answer; declared, the list is read from its other sources, the
  decision records the declaration and never automates. The data-coverage record says why part
  of a record was not read: a prefetch read's HTTP status, the linked reads by budget.

Every record is synthetic, every id and system fictional, and the shapes Epic is assumed to send
are labelled as assumptions; nothing remote is called. The trial pack is read from `packs/`,
unchanged."""

from __future__ import annotations

import functools
import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, load_pack
from mendari.core.decision import DecisionResult
from mendari.core.spec import Outcome
from mendari.core.tenancy import DEFAULT_ORG
from mendari.errors import MendariError
from mendari.providers import deadline as deadline_module
from mendari.sandbox import cds_client
from mendari.server.app import create_app
from mendari.server.smart_routes import decide_for
from mendari.standards import cds_hooks, linked
from mendari.standards.ehr_declarations import (
    ENV,
    EhrDeclaration,
    EhrDeclarations,
    canonical,
    check,
    ehr_declarations,
    excusable,
    parse,
)
from mendari.standards.fhir import FhirAdapter, format_label, unread_attachments, unread_notes
from mendari.standards.fhir_client import FhirClient
from mendari.standards.linked import (
    MAX_FOLLOW_READS,
    MAX_NOTE_READS,
    MAX_REQUEST_READS,
    Linker,
)
from mendari.standards.smart import Registration
from tests.conftest import ROOT
from tests.jwt_signers import PUBLIC_URL
from tests.test_cds_auth import sign_in_on
from tests.test_cds_clients import RSA_EHR, register, token
from tests.test_cds_clients import entry as client_entry
from tests.test_fhir_linked import (
    EHR,
    ORIGIN,
    TOKEN,
    Ehr,
    b64,
    binary,
    bundle,
    decide,
    fhir_json,
    linked_pack,
    linking,
    medication,
    note,
    request_for,
)
from tests.test_smart import FakeEhr, full_launch, signed_site
from tests.test_spec_03 import mini

HTML = "text/html"
RTF = "text/rtf"  # how an Epic note's second rendition is assumed to be typed (A1)


@pytest.fixture
def pack(tmp_path: Path) -> Decision:
    return Decision(load_pack(linked_pack(tmp_path / "packs")))


def twin(n: int, text: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Note `n` as Epic is assumed to send it (A1): one DocumentReference whose two contents
    are an HTML and an RTF Binary of the same note, both links; and the HTML Binary."""
    shown = text if text is not None else f"<p>Visit {n}: seen for diabetes follow-up.</p>"
    document = note(f"n{n}", linking(f"Binary/h{n}", HTML), linking(f"Binary/r{n}", RTF))
    return document, binary(f"h{n}", shown, HTML)


def twins(count: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pairs = [twin(n) for n in range(count)]
    return [document for document, _ in pairs], [html for _, html in pairs]


def drugs(count: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """`count` MedicationRequests, each naming its own Medication by reference and display, as
    Epic is assumed to (A2), and those Medications."""
    requests = []
    for n in range(count):
        request = request_for(f"r{n}", f"Medication/m{n}")
        request["medicationReference"]["display"] = f"drug {n} 10 mg tablet"
        requests.append(request)
    return requests, [medication(f"m{n}", f"RX-{n}") for n in range(count)]


def coverage_of(result: Any) -> dict[str, Any]:
    assert result.data_coverage is not None
    return dict(result.data_coverage)


# -- notes: renditions count once ---------------------------------------------------------------


def test_a_note_none_of_whose_renditions_is_read_counts_once_by_its_text_rendition(
    pack: Decision,
) -> None:
    """A note none of whose renditions is read counts once, by its text rendition's reason: an
    HTML twin past the budget is `cap` alone (it was `cap` and `unsupported_type` for its RTF
    twin, the 2026-11-08 run's reasons), an HTML twin whose read fails is that failure alone. A
    note with no text rendition (RTF only) still abstains, `unsupported_type`, its format
    counted; an empty HTML beside an RTF reads no text, so that note counts, by the RTF. Outside
    renditions (a Communication's payloads) each attachment still counts."""
    documents, htmls = twins(MAX_NOTE_READS + 5)
    ehr = Ehr(*htmls)
    result = decide(pack, *documents, ehr=ehr)
    assert result.reasons == ("could not read notes: cap",)
    notes = coverage_of(result)["fields"]["notes"]
    assert notes["unreadable"] == {"cap": 5} and "unread_formats" not in notes
    assert len(ehr.reads()) == MAX_NOTE_READS  # never an RTF: its type says it is not text

    document, html = twin(1)
    ehr = Ehr(html)
    ehr.faults["Binary/h1"] = lambda r: fhir_json({"issue": []}, status=500)
    result = decide(pack, document, ehr=ehr)
    assert result.reasons == ("could not read notes: http_status",)
    assert coverage_of(result)["fields"]["notes"]["unreadable"] == {"http_status": 1}

    readable, html = twin(2)
    rtf_only = note("n3", linking("Binary/r3", RTF))
    result = decide(pack, readable, rtf_only, ehr=Ehr(html))
    assert result.reasons == ("could not read notes: unsupported_type",)
    assert result.answers == {}  # abstained before any provider
    notes = coverage_of(result)["fields"]["notes"]
    assert notes["unreadable"] == {"unsupported_type": 1}
    assert notes["unread_formats"] == {"rtf": 1}

    empty = note(
        "n4",
        {"contentType": HTML, "data": b64("<p></p>")},
        {"contentType": RTF, "data": b64("{\\rtf1 Seen.}")},
    )
    result = decide(pack, empty, ehr=Ehr())
    assert result.reasons == ("could not read notes: unsupported_type",)
    assert coverage_of(result)["fields"]["notes"]["unread_formats"] == {"rtf": 1}

    holder = object()
    pairs = [
        (holder, {"contentType": "application/pdf", "data": b64("A")}),
        (holder, {"contentType": RTF, "data": b64("B")}),
    ]
    assert unread_attachments("Communication.payload.contentAttachment", pairs, {}) == [
        "unsupported_type",
        "unsupported_type",
    ]
    assert unread_notes("DocumentReference.content.attachment", pairs, {}) == [
        ("unsupported_type", "pdf")
    ]


def test_an_unread_note_s_format_is_a_closed_label_never_the_type_as_written() -> None:
    """The format of a note read as `unsupported_type` is one of six labels; a content type's
    parameters (which can carry a file name) never reach the record. A text attachment whose
    Binary holds another type is labelled by the Binary's type."""
    assert [
        format_label(kind)
        for kind in (
            "text/rtf; name=LNK8842.rtf",
            "application/rtf",
            "application/pdf",
            "application/hl7-cda+xml",
            "text/xml",
            "image/png",
            None,
            "application/msword",
        )
    ] == ["rtf", "rtf", "pdf", "xml", "xml", "image", "none", "other"]
    record = {"Binary": [binary("b1", "{\\rtf1 x}", "application/rtf")]}
    container = object()
    said_text = {"contentType": "text/plain", "url": "Binary/b1"}
    assert unread_notes(
        "DocumentReference.content.attachment", [(container, said_text)], record
    ) == [("unsupported_type", "rtf")]


# -- link budgets, by purpose and per request ---------------------------------------------------


def test_more_than_twenty_medication_orders_read_their_drugs_beside_many_notes(
    pack: Decision,
) -> None:
    """The 2026-11-08 run's cause: notes, read first, used every link a decision had (20), and
    the drugs behind the MedicationRequests were `cap`. With a budget per purpose, 25 twin notes
    and 30 orders of 30 drugs all read: no reason, each Binary and Medication once."""
    documents, htmls = twins(25)
    requests, medications = drugs(30)
    ehr = Ehr(*htmls, *medications)
    result = decide(pack, *documents, *requests, ehr=ehr)
    assert result.unreadable == () and result.answers
    assert coverage_of(result)["links"] == {
        "budget": {"follows": MAX_FOLLOW_READS, "notes": MAX_NOTE_READS},
        "read": {"follows": 30, "notes": 25},
    }
    assert len(ehr.reads()) == 55 == len(set(ehr.reads()))


def test_linked_reads_are_capped_per_purpose_per_decision_and_per_request(
    pack: Decision,
) -> None:
    """Past its purpose's budget a link is `cap` and is not read, and the other purpose's
    budget is untouched: 60 notes read 50 beside 5 drugs read, 60 drugs read 50. One request's
    decisions together send at most `MAX_REQUEST_READS` reads: past them a link is `cap`,
    nothing is sent, and the decision abstains."""
    documents, htmls = twins(60)
    requests, medications = drugs(5)
    ehr = Ehr(*htmls, *medications)
    result = decide(pack, *documents, *requests, ehr=ehr)
    assert result.reasons == ("could not read notes: cap",)
    coverage = coverage_of(result)
    assert coverage["fields"]["notes"]["unreadable"] == {"cap": 10}
    assert coverage["links"]["read"] == {"follows": 5, "notes": 50}
    source = coverage["lists"]["medications"]["sources"]["MedicationRequest"]
    assert source["unreadable"] == {} and "unreadable" not in source["fields"]["code"]

    requests, medications = drugs(60)
    ehr = Ehr(*medications)
    result = decide(pack, *requests, ehr=ehr)
    assert result.reasons == ("could not read medications.code: cap",)
    coverage = coverage_of(result)
    source = coverage["lists"]["medications"]["sources"]["MedicationRequest"]
    assert source["fields"]["code"]["unreadable"] == {"cap": 10}
    assert coverage["links"]["read"] == {"follows": 50, "notes": 0}
    assert len(ehr.reads()) == 50

    documents, htmls = twins(MAX_NOTE_READS)
    requests, medications = drugs(MAX_FOLLOW_READS)
    late_note, late_html = twin(99)
    ehr = Ehr(*htmls, *medications, late_html)
    links = Linker.of(ehr.client())
    first, _ = pack.decide_fhir(bundle(*documents, *requests), links=links)
    assert first.unreadable == () and len(ehr.reads()) == MAX_REQUEST_READS
    second, _ = pack.decide_fhir(bundle(late_note), links=links)
    assert second.reasons == ("could not read notes: cap",)
    assert len(ehr.reads()) == MAX_REQUEST_READS  # nothing more was sent
    assert coverage_of(second)["fields"]["notes"]["unreadable"] == {"cap": 1}
    assert coverage_of(second)["links"]["read"] == {"follows": 0, "notes": 0}


def test_a_linked_read_s_status_is_recorded_as_a_number(pack: Decision) -> None:
    """A linked read that fails with an HTTP status counts its status, as a number, in the
    decision's `links` section (each read once a request, counted for each decision it fails);
    no other read adds one, and a decision with no linked read has no `links` section."""
    documents, htmls = twins(3)
    requests, medications = drugs(2)
    ehr = Ehr(*htmls, *medications)
    ehr.faults["Binary/h0"] = lambda r: fhir_json({}, status=403)
    ehr.faults["Binary/h1"] = lambda r: fhir_json({}, status=404)
    ehr.faults["Medication/m0"] = lambda r: fhir_json({}, status=403)
    result = decide(pack, *documents, *requests, ehr=ehr)
    assert result.reasons == (
        "could not read medications.code: unauthorized",
        "could not read notes: http_status",
        "could not read notes: unauthorized",
    )
    links = coverage_of(result)["links"]
    assert links["statuses"] == {"403": 2, "404": 1}
    assert links["read"] == {"follows": 2, "notes": 3}
    inline = note("n9", {"contentType": "text/plain", "data": b64("Seen.")})
    assert "links" not in coverage_of(decide(pack, inline, ehr=Ehr()))
    assert "links" not in coverage_of(decide(pack, inline))


def test_one_drug_ordered_many_times_is_read_once(pack: Decision) -> None:
    """Forty orders of one drug (one Medication id, as Epic is assumed to reuse it: A4) read
    that Medication once, and count once against the decision's follow budget."""
    requests = [request_for(f"r{n}", "Medication/m1") for n in range(40)]
    ehr = Ehr(medication("m1", "RX-1"))
    result = decide(pack, *requests, ehr=ehr)
    assert result.unreadable == () and ehr.reads() == ["Medication/m1"]
    assert coverage_of(result)["links"]["read"] == {"follows": 1, "notes": 0}


def test_a_note_field_stops_reading_once_its_text_is_longer_than_the_input_takes() -> None:
    """Once a note field's text, as the field joins it, is longer than the pack's input takes
    (the trial pack's 20,000 characters), the decision abstains `too_large` whatever the other
    notes hold: they are not read, each is `too_large`, and no other reason is added. A chart
    whose notes fit reads them all."""
    trial = Decision.from_hub("mendari/clinical-trial-prescreener")
    long = "x" * 6_000
    pairs = [twin(n, f"<p>{long}</p>") for n in range(10)]
    ehr = Ehr(*(html for _, html in pairs))
    links = Linker.of(ehr.client())
    record = bundle(*(document for document, _ in pairs))
    result, _ = trial.decide_fhir(record, links=links)
    assert result.reasons == ("could not read notes: too_large",)
    assert len(ehr.reads()) == 4  # 4 x 6,000 characters (and the blank lines) pass 20,000
    notes = coverage_of(result)["fields"]["notes"]
    assert notes["unreadable"] == {"too_large": 6 + 1}  # the 6 not read, and the field
    assert coverage_of(result)["links"]["read"] == {"follows": 0, "notes": 4}

    short = [twin(n, "<p>Seen.</p>") for n in range(10)]
    ehr = Ehr(*(html for _, html in short))
    result, _ = trial.decide_fhir(
        bundle(*(document for document, _ in short)), links=Linker.of(ehr.client())
    )
    assert "notes" not in {key for key, _ in result.unreadable}
    assert len(ehr.reads()) == 10


# -- clocks: the decision's deadline and the request's seconds ---------------------------------


class Clock:
    """A monotonic clock for the linked reads and the decision deadline, moved on by the fake
    EHR: each read takes `step` seconds."""

    def __init__(self, step: float):
        self.now = 1_000.0
        self.step = step

    def __call__(self) -> float:
        return self.now


class SlowEhr(Ehr):
    """`Ehr`, each read taking `clock.step` seconds of the fake clock."""

    clock: Clock | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.clock is not None:
            self.clock.now += self.clock.step
        return super().__call__(request)


def test_linked_reads_stop_at_the_deadline_and_the_request_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linked reads run inside the decision's deadline, which starts before them: a read waits
    at most what remains of it, an answer still arriving when it passes stops (`timeout`), and
    past it a link is `timeout` and nothing is sent; the decision abstains. A read the clock
    stopped is not kept: the request's next decision has its own deadline and reads it. The
    providers get what the reads left of the same deadline. The request's seconds in reads
    (`LINK_SECONDS`), added up read by read, stop its reads the same way."""
    decision = Decision(load_pack(linked_pack(tmp_path / "packs")), deadline_s=10)
    clock = Clock(step=4)
    monkeypatch.setattr(linked, "_now", clock)
    monkeypatch.setattr(deadline_module, "time", SimpleNamespace(monotonic=clock))
    documents = [note(f"n{n}", linking(f"Binary/b{n}")) for n in range(4)]
    ehr = SlowEhr(*(binary(f"b{n}", f"Note {n}.") for n in range(4)))
    ehr.clock = clock
    links = Linker.of(ehr.client())
    first, _ = decision.decide_fhir(bundle(*documents), links=links)
    # b0 ends at 4 s, b1 at 8 s, b2 is stopped at 12 s (past 10), b3 is never sent.
    assert first.reasons == ("could not read notes: timeout",)
    assert ehr.reads() == ["Binary/b0", "Binary/b1", "Binary/b2"]

    seen: list[float | None] = []
    provider = decision.providers["notes"]
    answer = provider.answer

    def timed(inputs: Any) -> Any:
        seen.append(deadline_module.remaining())
        return answer(inputs)

    monkeypatch.setattr(provider, "answer", timed)
    second, _ = decision.decide_fhir(bundle(*documents), links=links)
    # b0 and b1 were kept; b2 (stopped by the clock) and b3 are read in this decision's own
    # deadline (from 12 s to 22 s), at 16 s and 20 s, and the providers get the 2 s left.
    assert second.unreadable == () and second.answers
    assert ehr.reads()[3:] == ["Binary/b2", "Binary/b3"]
    assert seen == [pytest.approx(2.0)]

    monkeypatch.setattr(linked, "LINK_SECONDS", 10.0)
    roomy = Decision(decision.pack, deadline_s=120)
    ehr = SlowEhr(*(binary(f"b{n}", f"Note {n}.") for n in range(4)))
    ehr.clock = clock
    result, _ = roomy.decide_fhir(bundle(*documents), links=Linker.of(ehr.client()))
    # 4 s + 4 s spent; b2 may wait 2 s and is stopped at 12 s of reading; b3 is never sent.
    assert result.reasons == ("could not read notes: timeout",)
    assert ehr.reads() == ["Binary/b0", "Binary/b1", "Binary/b2"]
    assert coverage_of(result)["links"]["read"] == {"follows": 0, "notes": 3}


# -- what the EHR does not offer, declared ------------------------------------------------------

TRIAL = "mendari/clinical-trial-prescreener"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
ICD = "http://hl7.org/fhir/sid/icd-10-cm"
LOINC = "http://loinc.org"
ATTESTED = "A. Fictional, CMIO"  # who attested the declaration (fictional)
DECLARED = {
    EHR: {
        "org": "default",
        "version": "2026-11-08",
        "attested_by": ATTESTED,
        "reference": "CHG-0001 (fictional)",
        "not_offered": ["MedicationStatement"],
    }
}
LIMITED = (
    "medications may be incomplete: read without MedicationStatement, which this EHR does not "
    f"offer (declared, {ATTESTED})"
)
LIMITATION = f"{LIMITED}: a person decides"


def declarations(entries: dict[str, Any] = DECLARED, *, signed_in: bool = False) -> EhrDeclarations:
    return parse(json.dumps(entries), signed_in=signed_in)


def declaration(entries: dict[str, Any] = DECLARED) -> EhrDeclaration:
    found = declarations(entries).for_base(EHR, DEFAULT_ORG)
    assert found is not None
    return found


def ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


def order(id_: str, drug: str, name: str, days: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """A MedicationRequest written `days` ago naming its drug as Epic is assumed to (A2), and the
    Medication it names."""
    request = request_for(id_, f"Medication/{drug}")
    request["medicationReference"]["display"] = name
    request["authoredOn"] = ago(days)
    return request, {"resourceType": "Medication", "id": drug, "code": {"text": name}}


def epic_chart(*, notes: int = 0, orders: int = 0, insulin: bool = False) -> Ehr:
    """A synthetic adult with type 2 diabetes, an HbA1c and eGFR in range, on metformin for 200
    days (an active order with no end is current only when written in the last 12 months, B21), as
    an EHR with no R4 MedicationStatement serves her: its MedicationStatement search
    answers 403 (as Epic's did on 2026-11-08), her drugs are MedicationRequests (A2), her notes
    HTML and RTF twins (A1). `orders` more drugs, and `insulin` an insulin order of 30 days ago."""
    patient = {
        "resourceType": "Patient",
        "id": "p1",
        "birthDate": ago(58 * 366),
        "gender": "female",
    }
    condition = {
        "resourceType": "Condition",
        "id": "c1",
        "subject": {"reference": "Patient/p1"},
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "code": {"coding": [{"system": ICD, "code": "E11.9"}]},
    }

    def lab(id_: str, code: str, value: float, unit: str, days: int) -> dict[str, Any]:
        return {
            "resourceType": "Observation",
            "id": id_,
            "status": "final",
            "subject": {"reference": "Patient/p1"},
            "code": {"coding": [{"system": LOINC, "code": code}]},
            "valueQuantity": {"value": value, "unit": unit},
            "effectiveDateTime": ago(days),
        }

    drugs = [order("mr-met", "med-met", "metformin 500 mg tablet", 200)]
    drugs += [order(f"mr{n}", f"med{n}", f"drug {n} 10 mg tablet", 200) for n in range(orders)]
    if insulin:
        drugs.append(order("mr-ins", "med-ins", "insulin glargine 100 unit/mL pen", 30))
    pairs = [twin(n, "<p>Seen for diabetes follow-up.</p>") for n in range(notes)]
    ehr = Ehr(
        patient,
        *(drug for _, drug in drugs),
        *(html for _, html in pairs),
        record=(
            condition,
            lab("o1", "4548-4", 64, "mmol/mol", 20),
            lab("o2", "33914-3", 58, "mL/min/1.73m2", 100),
            *(request for request, _ in drugs),
            *(document for document, _ in pairs),
        ),
    )
    ehr.faults["MedicationStatement"] = lambda r: fhir_json(
        {"resourceType": "OperationOutcome", "issue": [{"severity": "error", "code": "forbidden"}]},
        status=403,
    )
    return ehr


def searched(ehr: Ehr) -> list[str]:
    """The searches the EHR was sent, by resource type."""
    paths = [r.url.path.removeprefix("/fhir/") for r in ehr.requests]
    return [path for path in paths if "/" not in path]


def hook(prefetch: dict[str, Any] | None = None) -> dict[str, Any]:
    """A patient-view request from the EHR, with its server and token; `prefetch` as sent (None:
    none, so every key is fetched)."""
    request: dict[str, Any] = {
        "hook": "patient-view",
        "hookInstance": "2b0c5d1e-7a4f-4b8e-9c3d-6f1a2e3b4c5d",
        "fhirServer": EHR,
        "fhirAuthorization": {
            "access_token": TOKEN,
            "token_type": "Bearer",
            "scope": "patient/*.rs",
        },
        "context": {"userId": "Practitioner/1", "patientId": "p1"},
    }
    if prefetch is not None:
        request["prefetch"] = prefetch
    return request


def invoked(
    monkeypatch: pytest.MonkeyPatch,
    decision: Decision,
    ehr: Ehr,
    request: dict[str, Any],
    declared: EhrDeclaration | None,
) -> DecisionResult:
    """The one decision a hook request made, with `ehr` as the allow-listed server behind its
    `fhirServer` (for the prefetch it fetches and the links it reads)."""
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", ORIGIN)
    patched = functools.partial(FhirClient, transport=httpx.MockTransport(ehr))
    monkeypatch.setattr(cds_hooks, "FhirClient", patched)
    made: list[DecisionResult] = []
    original = decision.decide_fhir

    def kept(*args: Any, **kwargs: Any) -> tuple[DecisionResult, dict[str, Any]]:
        found = original(*args, **kwargs)
        made.append(found[0])
        return found

    monkeypatch.setattr(decision, "decide_fhir", kept)
    cds_hooks.invoke(decision, request, declared=declared)
    [result] = made
    return result


def sent_from(ehr: Ehr, **keys: Any) -> dict[str, Any]:
    """The trial's whole prefetch from `ehr`'s record, as an EHR sends it, `keys` replacing
    what it would send."""

    def search(kind: str) -> dict[str, Any]:
        found = [r for r in ehr.record if r["resourceType"] == kind]
        return {
            "resourceType": "Bundle",
            "type": "searchset",
            "entry": [{"resource": r} for r in found],
        }

    prefetch = {
        "patient": ehr.resources["Patient/p1"],
        "conditions": search("Condition"),
        "observations": search("Observation"),
        "medications": search("MedicationStatement"),
        "medication_requests": search("MedicationRequest"),
        "notes": search("DocumentReference"),
    }
    return {**prefetch, **keys}


def test_an_undeclared_medication_statement_refusal_never_reads_as_off_insulin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a declaration, the EHR refusing MedicationStatement makes the decision ABSTAIN
    before any provider, through the SMART launch's decide and through a hook that fetches its
    prefetch: no answer exists, so "no insulin listed" never becomes "off insulin"."""
    trial = Decision.from_hub(TRIAL)
    ehr = epic_chart()
    result, _ = decide_for(trial, ehr.client(), "p1", "Patient/p1")
    assert result.outcome is Outcome.ABSTAIN and result.recommendation is None
    assert result.reasons == ("could not read medications: unauthorized",)
    assert result.answers == {} and result.ehr_declaration is None
    assert "MedicationStatement" in searched(ehr)
    refused = {"resource": "MedicationStatement", "reason": "unauthorized", "status": 403}
    assert coverage_of(result)["prefetch"] == {"medications": refused}

    hooked = invoked(monkeypatch, Decision.from_hub(TRIAL), epic_chart(), hook(), None)
    assert hooked.reasons == ("could not read medications: unauthorized",)
    assert hooked.answers == {} and hooked.ehr_declaration is None
    assert coverage_of(hooked)["prefetch"] == {"medications": refused}


def test_a_declared_not_offered_type_is_not_requested_and_the_decision_records_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the site's declaration for this base and organization, MedicationStatement is never
    asked for: the list is read from the MedicationRequests, the source and the list are marked
    not offered (its 0 is not "read and empty"), the decision records the declaration and goes to
    a person with the limitation as a reason. There, and only there, "no insulin among the
    orders" reads as "off insulin", on the site's word; an insulin order still reads."""
    trial = Decision.from_hub(TRIAL)
    declared = declaration()
    ehr = epic_chart()
    result, _ = decide_for(trial, ehr.client(), "p1", "Patient/p1", declared=declared)
    assert "MedicationStatement" not in searched(ehr) and "MedicationRequest" in searched(ehr)
    assert result.unreadable == () and result.outcome is Outcome.HUMAN_REVIEW
    assert LIMITATION in result.reasons
    assert (
        result.ehr_declaration
        == declared.stamp()
        == {
            "version": "2026-11-08",
            "sha256": declared.sha256,
            "attested_by": ATTESTED,
            "reference": "CHG-0001 (fictional)",
        }
    )
    assert coverage_of(result)["prefetch"] == {
        "medications": {"resource": "MedicationStatement", "not_offered": True}
    }
    medications = coverage_of(result)["lists"]["medications"]
    assert medications["not_offered"] == ["MedicationStatement"]
    statements = medications["sources"]["MedicationStatement"]
    assert (statements["not_offered"], statements["resources"], statements["items"]) == (True, 0, 0)
    assert "not_offered" not in medications["sources"]["MedicationRequest"]
    assert result.answers["no_insulin"].label == "yes"
    assert result.answers["stable_metformin"].label == "yes"
    assert result.to_record()["ehr_declaration"] == declared.stamp()

    ehr = epic_chart(insulin=True)
    result, _ = decide_for(trial, ehr.client(), "p1", "Patient/p1", declared=declared)
    assert result.answers["no_insulin"].label == "no"
    assert LIMITATION in result.reasons

    hooked = invoked(monkeypatch, Decision.from_hub(TRIAL), epic_chart(), hook(), declared)
    assert hooked.unreadable == () and LIMITATION in hooked.reasons
    assert hooked.ehr_declaration == declared.stamp()


def test_a_declaration_never_applies_to_another_base_or_organization() -> None:
    """A declaration is given for its own base (however it is spelled) and organization only: a
    launch from another EHR, or for another organization, reads every source, and the refusal
    abstains."""
    found = declarations()
    assert found.for_base(f"{EHR}/", DEFAULT_ORG) is not None  # normalized
    assert found.for_base("HTTPS://EHR.example.org/fhir", DEFAULT_ORG) is not None
    assert found.for_base("https://other.example.org/fhir", DEFAULT_ORG) is None
    assert found.for_base(f"{EHR}/other", DEFAULT_ORG) is None
    assert found.for_base(EHR, "acme") is None
    assert (
        found.for_base(None, DEFAULT_ORG) is None
        and found.for_base("http://[", DEFAULT_ORG) is None
    )
    signed = declarations({EHR: {**DECLARED[EHR], "org": "acme"}}, signed_in=True)
    assert signed.for_base(EHR, DEFAULT_ORG) is None and signed.for_base(EHR, "acme") is not None
    ehr = epic_chart()
    other = found.for_base("https://other.example.org/fhir", DEFAULT_ORG)
    result, _ = decide_for(
        Decision.from_hub(TRIAL), ehr.client(), "p1", "Patient/p1", declared=other
    )
    assert result.reasons == ("could not read medications: unauthorized",)


TWO_SOURCES = {
    "resources": {
        "MedicationStatement": {"fields": {"name": {"path": "medicationCodeableConcept.text"}}},
        "MedicationRequest": {"fields": {"name": {"path": "medicationCodeableConcept.text"}}},
    }
}


def two_source_pack(
    base: Path,
    *,
    changes: dict[str, Any] | None = None,
    rules: list[dict[str, Any]] | None = None,
    statements: str = "MedicationStatement?patient={{context.patientId}}",
) -> Path:
    """The prescreen fixture at spec 0.3 and risk level 1, automating at 0.80 (its criteria give
    a candidate 0.83), its medications read from MedicationStatement and MedicationRequest, both
    prefetched (MedicationStatement by `statements`); `changes` to its mapping, `rules` its
    policy's."""
    root = mini(
        base / "packs", {"lists": {"medications": TWO_SOURCES}, **(changes or {})}, spec="0.3"
    )
    manifest = yaml.safe_load((root / "agent.yaml").read_text())
    manifest["risk"]["level"] = 1
    manifest["policy"]["thresholds"]["auto"] = 0.8
    if rules is not None:
        manifest["policy"]["rules"] = rules
    manifest["cds_hooks"]["prefetch"].update(
        {
            "statements": statements,
            "requests": "MedicationRequest?patient={{context.patientId}}",
        }
    )
    (root / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    schema_path = root / "schemas" / "input.schema.json"
    schema = json.loads(schema_path.read_text())
    schema["properties"]["medications"] = {"type": "array", "items": {"type": "object"}}
    schema_path.write_text(json.dumps(schema))
    return root


def mini_chart(*, refused: bool) -> Ehr:
    """The fixture's patient with type 2 diabetes and an HbA1c in range today; its EHR refuses
    MedicationStatement (`refused`) or has none."""
    condition = {
        "resourceType": "Condition",
        "id": "c1",
        "subject": {"reference": "Patient/p1"},
        "code": {"coding": [{"system": ICD, "code": "E11.9"}]},
    }
    hba1c = {
        "resourceType": "Observation",
        "id": "o1",
        "status": "final",
        "subject": {"reference": "Patient/p1"},
        "code": {"coding": [{"system": LOINC, "code": "4548-4"}]},
        "valueQuantity": {"value": 8.0, "unit": "%"},
        "effectiveDateTime": date.today().isoformat(),
    }
    ehr = Ehr(record=(condition, hba1c))
    if refused:
        ehr.faults["MedicationStatement"] = lambda r: fhir_json({}, status=403)
    return ehr


def test_a_declaration_never_automates(tmp_path: Path) -> None:
    """A pack that automates this chart when every source reads goes to a person when a declared
    source was left out, the limitation a reason; its rule's BLOCK does too. Without the
    declaration, the refused source abstains."""
    pack = Decision(load_pack(two_source_pack(tmp_path / "auto")))
    declared = declaration()
    whole, _ = decide_for(pack, mini_chart(refused=False).client(), "p1", "Patient/p1")
    assert whole.outcome is Outcome.AUTO and whole.ehr_declaration is None
    result, _ = decide_for(
        pack, mini_chart(refused=True).client(), "p1", "Patient/p1", declared=declared
    )
    assert result.outcome is Outcome.HUMAN_REVIEW
    assert result.recommendation == whole.recommendation
    assert LIMITATION in result.reasons
    refused, _ = decide_for(pack, mini_chart(refused=True).client(), "p1", "Patient/p1")
    assert refused.reasons == ("could not read statements: unauthorized",)

    rule = {
        "name": "fixture_block",
        "when": {"question": "t2d_diagnosis", "labels": ["yes"]},
        "outcome": "BLOCK",
        "reason": "a fixture's blocking rule",
    }
    blocking = Decision(load_pack(two_source_pack(tmp_path / "block", rules=[rule])))
    whole, _ = decide_for(blocking, mini_chart(refused=False).client(), "p1", "Patient/p1")
    assert whole.outcome is Outcome.BLOCK
    result, _ = decide_for(
        blocking, mini_chart(refused=True).client(), "p1", "Patient/p1", declared=declared
    )
    assert result.outcome is Outcome.HUMAN_REVIEW
    assert f"{LIMITATION} instead of its rule's BLOCK" in result.reasons


def test_a_declared_key_a_launch_cannot_fill_is_not_offered(tmp_path: Path) -> None:
    """A declared key whose template needs context the SMART launch does not have (an encounter)
    is left out as not offered, never a `500`: nothing of its type is asked for, and the decision
    records the declaration and goes to a person, the limitation a reason."""
    template = "MedicationStatement?patient={{context.patientId}}&encounter={{context.encounterId}}"
    pack = Decision(load_pack(two_source_pack(tmp_path, statements=template)))
    declared = declaration()
    ehr = mini_chart(refused=True)
    result, _ = decide_for(pack, ehr.client(), "p1", "Patient/p1", declared=declared)
    assert result.outcome is Outcome.HUMAN_REVIEW and LIMITATION in result.reasons
    assert result.unreadable == () and result.ehr_declaration == declared.stamp()
    assert "MedicationStatement" not in searched(ehr)
    assert coverage_of(result)["prefetch"] == {
        "statements": {"resource": "MedicationStatement", "not_offered": True}
    }


def test_a_refused_input_under_a_declaration_still_names_the_limitation() -> None:
    """Under `enforce`, an input refused for its US Core errors (the synthetic chart has several:
    a patient with no name, orders with no `requester`) is a final BLOCK: the limitation is still
    one of its reasons, without "a person decides", beside the declaration it records."""
    strict = Decision.from_hub(TRIAL, fhir_validation="enforce")
    declared = declaration()
    result, _ = decide_for(strict, epic_chart().client(), "p1", "Patient/p1", declared=declared)
    assert result.outcome is Outcome.BLOCK and result.answers == {}
    assert result.reasons[0].startswith("input rejected: not US Core")
    assert result.reasons[-1] == LIMITED and LIMITATION not in result.reasons
    assert result.ehr_declaration == declared.stamp()


LINKED = cds_hooks.CardLinks(public_url=PUBLIC_URL)


def bullets(text: str) -> list[str]:
    """A card detail's (or a part of one's) `- ` lines."""
    return [line for line in text.splitlines() if line.startswith("- ")]


def test_a_card_keeps_the_limitation_whatever_the_list_is_called(tmp_path: Path) -> None:
    """A list's name is the pack's own, spaces and all: under a declaration, the card still keeps
    the line that says the list may be incomplete in its detail when the engine's other reasons
    are behind the audit link, as it does for `medications`."""
    name = "current meds"
    root = two_source_pack(tmp_path, changes={"lists": {name: TWO_SOURCES}})
    schema_path = root / "schemas" / "input.schema.json"
    schema = json.loads(schema_path.read_text())
    schema["properties"][name] = schema["properties"].pop("medications")
    schema_path.write_text(json.dumps(schema))
    pack = Decision(load_pack(root))
    result, task = decide_for(
        pack, mini_chart(refused=True).client(), "p1", "Patient/p1", declared=declaration()
    )
    limited = LIMITATION.replace("medications", name, 1)
    assert result.outcome is Outcome.HUMAN_REVIEW and limited in result.reasons
    assert bullets(cds_hooks.card(pack.pack, result, task, LINKED)["detail"]) == [f"- {limited}"]


def test_a_refused_input_s_card_says_the_limitation_apart_from_why(tmp_path: Path) -> None:
    """A card keyed `refused` (spec 0.3 §5) lists why the input was refused, whatever the links;
    under a declaration its limitation is on the card too, but apart (`ALSO_NOTED`), since it is
    not why: the refusal is the US Core errors."""
    root = two_source_pack(tmp_path)
    manifest = yaml.safe_load((root / "agent.yaml").read_text())
    card = manifest["cds_hooks"]["card"]
    card["summaries"]["refused"] = "Prescreen not checked: its data was refused as invalid"
    card["indicators"]["refused"] = "warning"
    card["show"].append("refused")
    (root / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    strict = Decision(load_pack(root), fhir_validation="enforce")
    result, task = decide_for(
        strict, mini_chart(refused=True).client(), "p1", "Patient/p1", declared=declaration()
    )
    assert result.outcome is Outcome.BLOCK and result.rejected and result.reasons[-1] == LIMITED
    for links in (cds_hooks.NO_LINKS, LINKED):
        detail = cds_hooks.card(strict.pack, result, task, links)["detail"]
        why, also = detail.split(f"\n{cds_hooks.ALSO_NOTED}\n")
        assert cds_hooks.REFUSED_DETAIL in why
        assert bullets(why) and all(line.startswith("- not US Core") for line in bullets(why))
        assert bullets(also) == [f"- {LIMITED}"]


def test_an_unreadable_abstain_s_card_says_the_limitation_apart() -> None:
    """An ABSTAIN because part of the record could not be read always shows its card, which
    lists what was not read; under a declaration it also says the list may be incomplete, apart
    (`ALSO_NOTED`), whatever the links, and the sandbox client reads only the first list as what
    was not read (`cds_client.unread_of`)."""
    trial = Decision.from_hub(TRIAL)
    ehr = epic_chart()
    ehr.faults["Observation"] = lambda r: fhir_json({}, status=403)
    result, task = decide_for(trial, ehr.client(), "p1", "Patient/p1", declared=declaration())
    assert result.abstained_unreadable and LIMITATION in result.reasons
    for links in (cds_hooks.NO_LINKS, LINKED):
        card = cds_hooks.card(trial.pack, result, task, links)
        unread, also = card["detail"].split(f"\n{cds_hooks.ALSO_NOTED}\n")
        assert bullets(unread) == [f"- {key}: {reason}" for key, reason in result.unreadable]
        assert bullets(also) == [f"- {LIMITATION}"]
        assert cds_client.unread_of(card) == set(result.unreadable)


def test_a_declared_type_a_pack_reads_elsewhere_is_read_as_without_the_declaration(
    tmp_path: Path,
) -> None:
    """A declared type a pack also reads elsewhere (a single-source list, a top-level field), or
    whose list's every source is declared, is read as without the declaration, and its refusal
    abstains: a declaration can never leave a list or a field with no source."""
    declared = declaration()
    single = {
        "lists": {
            "medications": {
                "resource": "MedicationStatement",
                "fields": {"name": {"path": "medicationCodeableConcept.text"}},
            }
        }
    }
    field = {"fields": {"last_status": {"path": "MedicationStatement.status"}}}
    for name, changes in (("single", single), ("field", field)):
        root = two_source_pack(tmp_path / name, changes=changes)
        if name == "field":
            schema_path = root / "schemas" / "input.schema.json"
            schema = json.loads(schema_path.read_text())
            schema["properties"]["last_status"] = {"type": "string"}
            schema_path.write_text(json.dumps(schema))
        pack = Decision(load_pack(root))
        mapping = FhirAdapter.for_pack(pack.pack).mapping
        prefetch = pack.pack.manifest.cds_hooks.prefetch if pack.pack.manifest.cds_hooks else {}
        assert excusable(mapping, prefetch, {"MedicationStatement"}) == frozenset(), name
        result, _ = decide_for(
            pack, mini_chart(refused=True).client(), "p1", "Patient/p1", declared=declared
        )
        assert result.reasons == ("could not read statements: unauthorized",), name

    both = declaration(
        {EHR: {**DECLARED[EHR], "not_offered": ["MedicationRequest", "MedicationStatement"]}}
    )
    trial = Decision.from_hub(TRIAL)
    hooks = trial.pack.manifest.cds_hooks
    assert hooks is not None
    mapping = FhirAdapter.for_pack(trial.pack).mapping
    assert excusable(mapping, hooks.prefetch, both.not_offered) == frozenset()
    assert both.skipped(mapping, hooks.prefetch, hooks.prefetch) == {}
    ehr = epic_chart()
    result, _ = decide_for(trial, ehr.client(), "p1", "Patient/p1", declared=both)
    assert result.reasons == ("could not read medications: unauthorized",)
    assert "MedicationStatement" in searched(ehr)
    with pytest.raises(ValueError, match="needs the declaration"):
        trial.decide_fhir(bundle(), not_offered={"medications": "MedicationStatement"})
    with pytest.raises(ValueError, match="may not be left out"):
        trial.decide_fhir(
            bundle(), not_offered={"notes": "DocumentReference"}, declaration=declared
        )


def test_a_cds_client_may_still_send_a_declared_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under a declaration, what a client sends for a declared key is data when it holds entries,
    used as sent, and an OperationOutcome is still `operation_outcome`; sent null, empty (`{}`)
    or as a search with no entries, it says nothing more than the declaration does: the key is
    not offered, recorded, never fetched, never automated (no `412` either)."""
    declared = declaration()
    ehr = epic_chart()
    statement = {
        "resourceType": "MedicationStatement",
        "id": "ms1",
        "status": "active",
        "subject": {"reference": "Patient/p1"},
        "medicationCodeableConcept": {"text": "insulin glargine 100 unit/mL pen"},
        "effectivePeriod": {"start": ago(60)},
    }
    given = {"resourceType": "Bundle", "type": "searchset", "entry": [{"resource": statement}]}
    result = invoked(
        monkeypatch,
        Decision.from_hub(TRIAL),
        ehr,
        hook(sent_from(ehr, medications=given)),
        declared,
    )
    assert result.unreadable == () and result.ehr_declaration is None
    assert result.answers["no_insulin"].label == "no"
    assert LIMITATION not in result.reasons

    outcome = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "forbidden"}],
    }
    result = invoked(
        monkeypatch,
        Decision.from_hub(TRIAL),
        ehr,
        hook(sent_from(ehr, medications=outcome)),
        declared,
    )
    assert result.reasons == ("could not read medications: operation_outcome",)

    empty = {"resourceType": "Bundle", "type": "searchset"}
    for name, value in (("null", None), ("empty", {}), ("no entries", empty)):
        ehr = epic_chart()
        result = invoked(
            monkeypatch,
            Decision.from_hub(TRIAL),
            ehr,
            hook(sent_from(ehr, medications=value)),
            declared,
        )
        assert result.unreadable == (), name
        assert result.ehr_declaration == declared.stamp(), name
        assert LIMITATION in result.reasons, name
        assert coverage_of(result)["lists"]["medications"]["not_offered"] == ["MedicationStatement"]
        assert searched(ehr) == [], name  # nothing fetched: the rest was sent
    # Without the declaration, null is "no data" and an empty search is read and empty, as before.
    result = invoked(
        monkeypatch, Decision.from_hub(TRIAL), ehr, hook(sent_from(ehr, medications=None)), None
    )
    assert result.ehr_declaration is None and LIMITATION not in result.reasons


# -- the setting, checked at startup -------------------------------------------------------------

REFERENCE_PACKS = ROOT / "tests" / "fixtures" / "reference-agents"
REGISTERED = {EHR: Registration(base=EHR, org=DEFAULT_ORG, level="decide")}


def entry(**changes: Any) -> dict[str, Any]:
    return {**DECLARED[EHR], **changes}


def installed() -> list[Any]:
    return [load_pack(path) for path in sorted(REFERENCE_PACKS.iterdir()) if path.is_dir()]


def cds_listing(base: str, org: str) -> Any:
    """A CDS client registry with one client listing `base`, for `org` (what `check` reads)."""
    return SimpleNamespace(clients=[SimpleNamespace(fhir_servers=(base,), org=org)])


SHAPES: dict[str, tuple[str, str]] = {
    "not JSON": ("{", "is not JSON"),
    "empty": ("{}", "must be a JSON object of 1 to 50 FHIR bases"),
    "too many": (
        json.dumps({f"https://ehr{n}.example.org/fhir": entry() for n in range(51)}),
        "must be a JSON object of 1 to 50 FHIR bases",
    ),
    "a key twice": (f'{{"{EHR}": {json.dumps(entry())}, "{EHR}": {{}}}}', "gives a key twice"),
    "a base that is not one": (json.dumps({"ftp://ehr.example.org": entry()}), "base 1 is not"),
    "a base twice": (json.dumps({EHR: entry(), f"{EHR}/": entry()}), "is given twice"),
    "a key missing": (
        json.dumps({EHR: {k: v for k, v in entry().items() if k != "attested_by"}}),
        "must be an object of org, version, attested_by, reference, not_offered",
    ),
    "a key more": (json.dumps({EHR: entry(extra=1)}), "must be an object of"),
    "org": (json.dumps({EHR: entry(org="has space")}), "org must be 1 to 100"),
    "org signed out": (json.dumps({EHR: entry(org="acme")}), "with sign-in off"),
    "version": (json.dumps({EHR: entry(version="")}), "version must be 1 to 64"),
    "attested_by": (json.dumps({EHR: entry(attested_by=" A. Fictional")}), "attested_by must be"),
    "reference": (json.dumps({EHR: entry(reference="")}), "reference must be"),
    "no types": (json.dumps({EHR: entry(not_offered=[])}), "not_offered must be a list"),
    "not a type": (
        json.dumps({EHR: entry(not_offered=["Medicine"])}),
        "not_offered 1 is not an R4",
    ),
    "a type twice": (
        json.dumps({EHR: entry(not_offered=["MedicationStatement", "MedicationStatement"])}),
        "names a type twice",
    ),
}


@pytest.mark.parametrize("case", sorted(SHAPES))
def test_the_declaration_setting_is_checked_at_startup(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each thing wrong in the setting's shape stops the server at startup, named by the setting
    and the base (by position when it is not one), never by who attested it."""
    raw, problem = SHAPES[case]
    with pytest.raises(MendariError) as refused:
        parse(raw, signed_in=False)
    message = str(refused.value)
    assert message.startswith(ENV) and problem in message, message
    assert ATTESTED not in message
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", ORIGIN)
    monkeypatch.setenv("MENDARI_SMART_EHRS", f"{EHR}={DEFAULT_ORG}:decide")
    monkeypatch.setenv(ENV, raw)
    with pytest.raises(MendariError, match=ENV):
        create_app(db_path=tmp_path / "a.db", packs_paths=[REFERENCE_PACKS])


def test_each_declaration_is_checked_against_the_deployment() -> None:
    """A declared base must be on the allow-list and registered (for launches, or in a CDS
    client's `fhir_servers`), for the organization those registrations name when sign-in is on;
    each declared type must be one a pack found here may read a list without. The same entry
    written in another order has the same digest; a changed one another."""
    packs = installed()

    def refusal(raw: dict[str, Any], **kwargs: Any) -> str:
        arguments: dict[str, Any] = {
            "signed_in": False,
            "ehrs": REGISTERED,
            "cds": None,
            "allowed": {ORIGIN},
            **kwargs,
        }
        found = parse(json.dumps(raw), signed_in=arguments["signed_in"])
        with pytest.raises(MendariError) as refused:
            check(found, packs, **arguments)
        return str(refused.value)

    assert "not on MENDARI_FHIR_ALLOWED_SERVERS" in refusal(DECLARED, allowed=set())
    assert "registered neither" in refusal(DECLARED, ehrs={})
    other = {EHR: Registration(base=EHR, org="other", level="decide")}
    signed = {EHR: entry(org="acme")}
    assert "MENDARI_SMART_EHRS registers it to" in refusal(signed, signed_in=True, ehrs=other)
    assert "every CDS client" in refusal(
        signed, signed_in=True, ehrs={}, cds=cds_listing(EHR, "other")
    )
    for kind in ("DocumentReference", "Patient", "Medication", "Condition"):
        message = refusal({EHR: entry(not_offered=[kind])})
        assert f"not_offered {kind}: no agent found here" in message, kind
    both = {EHR: entry(not_offered=["MedicationRequest", "MedicationStatement"])}
    assert "not_offered MedicationRequest: no agent found here" in refusal(both)

    # Registered either way, for its organization: checked.
    check(declarations(), packs, signed_in=False, ehrs=REGISTERED, cds=None, allowed={ORIGIN})
    found = parse(json.dumps(signed), signed_in=True)
    check(found, packs, signed_in=True, ehrs={}, cds=cds_listing(EHR, "acme"), allowed={ORIGIN})

    same = declaration({EHR: dict(reversed(list(entry().items())))})
    assert same.sha256 == declaration().sha256
    changed = declaration({EHR: entry(version="2026-11-09")})
    assert changed.sha256 != declaration().sha256
    assert json.loads(canonical(declaration())) == {
        "format": "mendari.ehr-declarations/1",
        "base": EHR,
        "org": DEFAULT_ORG,
        "version": "2026-11-08",
        "attested_by": ATTESTED,
        "reference": "CHG-0001 (fictional)",
        "not_offered": ["MedicationStatement"],
    }


def test_the_setting_is_inline_json_or_a_file_and_off_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset, there is no declaration and every source is read. Inline JSON and a file give the
    same declarations; a file that cannot be read, is too large or is not UTF-8 stops the
    server."""
    packs = installed()
    kwargs: dict[str, Any] = {"signed_in": False, "ehrs": REGISTERED, "cds": None}
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", ORIGIN)
    assert ehr_declarations(packs, **kwargs) is None
    monkeypatch.setenv(ENV, json.dumps(DECLARED))
    inline = ehr_declarations(packs, **kwargs)
    path = tmp_path / "declared.json"
    path.write_text(json.dumps(DECLARED))
    monkeypatch.setenv(ENV, str(path))
    assert ehr_declarations(packs, **kwargs) == inline is not None
    for name, content, problem in (
        ("missing.json", None, "neither inline JSON"),
        ("large.json", b" " * (1024 * 1024 + 1), "larger than"),
        ("latin.json", '{"é": 1}'.encode("latin-1"), "not UTF-8"),
    ):
        target = tmp_path / name
        if content is not None:
            target.write_bytes(content)
        monkeypatch.setenv(ENV, str(target))
        with pytest.raises(MendariError, match=problem):
            ehr_declarations(packs, **kwargs)


def test_health_shows_the_organizations_declarations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/api/health` shows what the caller's organization's EHRs do not offer, by base: each
    declaration's version, digest, who attested it, its reference and the types; null when
    nothing is declared."""
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", ORIGIN)
    monkeypatch.setenv("MENDARI_SMART_EHRS", f"{EHR}={DEFAULT_ORG}:decide")
    with TestClient(create_app(db_path=tmp_path / "a.db", packs_paths=[REFERENCE_PACKS])) as app:
        assert app.get("/api/health").json()["ehr_declarations"] is None
    monkeypatch.setenv(ENV, json.dumps(DECLARED))
    with TestClient(create_app(db_path=tmp_path / "b.db", packs_paths=[REFERENCE_PACKS])) as app:
        shown = app.get("/api/health").json()["ehr_declarations"]
    assert shown == {EHR: {**declaration().stamp(), "not_offered": ["MedicationStatement"]}}


# -- through the app: a hook and a launch read the declaration for their base and organization ----

EHR2 = "https://ehr2.example.org/fhir"  # another base the same CDS client registered, undeclared
NOT_OFFERED = {"resource": "MedicationStatement", "not_offered": True}


def test_a_hook_reads_the_declaration_for_its_fhir_server_and_organization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the CDS service route, sign-in on: a call from a client of organization `acme`
    whose `fhirServer` is declared for `acme` leaves MedicationStatement out, and its audited
    record carries the declaration; its card, whose other reasons are behind the audit link
    (`MENDARI_PUBLIC_URL`), still says the list may be incomplete. The same client's call for
    another base it registered reads every source, and the refusal abstains. A declaration for
    another organization than every client listing its base stops the server at startup."""
    sign_in_on(monkeypatch)
    client = client_entry("acme", org="acme", fhir_servers=[EHR, EHR2])
    register(monkeypatch, client, allowed=f"{ORIGIN},https://ehr2.example.org")
    monkeypatch.setenv(ENV, json.dumps({EHR: entry(org="acme")}))
    chart = epic_chart()
    patched = functools.partial(FhirClient, transport=httpx.MockTransport(chart))
    monkeypatch.setattr(cds_hooks, "FhirClient", patched)
    path = f"/cds-services/{cds_hooks.service_id(Decision.from_hub(TRIAL).pack)}"
    declared = declarations({EHR: entry(org="acme")}, signed_in=True).for_base(EHR, "acme")
    assert declared is not None
    db = tmp_path / "a.db"

    def called(server: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """The audited record of the one decision a call for `server` made, and its card."""
        chart.requests.clear()
        with TestClient(create_app(db_path=db), base_url=PUBLIC_URL) as app:
            response = app.post(
                path,
                json={**hook(), "fhirServer": server},
                headers={"authorization": f"Bearer {token(RSA_EHR, path)}"},
            )
        assert response.status_code == 200, response.text
        [card] = response.json()["cards"]
        with AuditStore(db) as store:
            found = store.get(card["uuid"])
        assert found is not None
        return dict(found["record"]), card

    record, card = called(EHR)
    assert record["ehr_declaration"] == declared.stamp()
    assert record["outcome"] == "HUMAN_REVIEW" and LIMITATION in record["reasons"]
    assert record["data_coverage"]["prefetch"] == {"medications": NOT_OFFERED}
    assert "MedicationStatement" not in searched(chart)
    assert card["summary"] == "Possible GLYCEMIA-2 candidate: confirm eligibility"
    assert [line for line in card["detail"].splitlines() if line.startswith("- ")] == [
        f"- {LIMITATION}"
    ]
    record, _ = called(EHR2)
    assert record["ehr_declaration"] is None
    assert record["reasons"] == ["could not read medications: unauthorized"]
    assert "MedicationStatement" in searched(chart)

    for clients, org in (
        ([client], "other"),
        ([client, client_entry("other", org="other", iss="https://other.example.org")], "acme"),
    ):
        register(monkeypatch, *clients, allowed=f"{ORIGIN},https://ehr2.example.org")
        monkeypatch.setenv(ENV, json.dumps({EHR: entry(org=org)}))
        with pytest.raises(MendariError, match="every CDS client"):
            create_app(db_path=tmp_path / "refused.db")


class EpicLauncher(FakeEhr):
    """The SMART tests' EHR (`test_smart.FakeEhr`, at `EHR`), launching for `p1`, its FHIR reads
    answered by `chart` once the token is one it issued."""

    def __init__(self, chart: Ehr) -> None:
        super().__init__(iss=EHR, root=ORIGIN)
        self.chart = chart
        self.patient = "p1"

    def fhir(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization", "").removeprefix("Bearer ") not in self.tokens:
            return httpx.Response(401, json={"resourceType": "OperationOutcome"})
        return self.chart(request)


def test_a_launch_reads_the_declaration_for_its_issuer_and_organization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through a SMART launch, sign-in on, from a base registered to `clinic-a` and declared for
    it: `/api/smart/decide` leaves MedicationStatement out and the decision carries the
    declaration. A declaration for another organization than the launch's registration stops the
    server at startup."""
    chart = epic_chart()
    launcher = EpicLauncher(chart)
    monkeypatch.setenv(ENV, json.dumps({EHR: entry(org="clinic-a")}))
    app = signed_site(tmp_path / "a.db", monkeypatch, f"{EHR}=clinic-a:decide", launcher)
    assert full_launch(app, launcher).status_code == 302
    decided = app.post("/api/smart/decide", json={"pack_id": TRIAL, "focus": "Patient/p1"})
    assert decided.status_code == 200, decided.text
    decision = decided.json()["decision"]
    declared = declarations({EHR: entry(org="clinic-a")}, signed_in=True).for_base(EHR, "clinic-a")
    assert declared is not None and decision["ehr_declaration"] == declared.stamp()
    assert decision["outcome"] == "HUMAN_REVIEW" and LIMITATION in decision["reasons"]
    assert decision["data_coverage"]["prefetch"] == {"medications": NOT_OFFERED}
    assert "MedicationStatement" not in searched(chart)

    monkeypatch.setenv(ENV, json.dumps({EHR: entry(org="clinic-b")}))
    with pytest.raises(MendariError, match="MENDARI_SMART_EHRS registers it to"):
        signed_site(tmp_path / "refused.db", monkeypatch, f"{EHR}=clinic-a:decide", launcher)


# -- records from before, and an Epic-shaped chart, end to end -----------------------------------


def test_old_records_still_verify(db_target: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A record chained before the declarations has no `ehr_declaration`; one after has it (null
    without a declaration left out, and on JSON input; the stamp with one). The chain verifies
    across all of them, on both databases."""
    store = AuditStore(db_target)
    trial = Decision.from_hub(TRIAL, store=store)
    inbox = Decision.from_hub("mendari/patient-inbox-router", store=store)
    original = DecisionResult.to_record

    def before(self: DecisionResult) -> dict[str, Any]:
        record = original(self)
        del record["ehr_declaration"]  # the shape from before
        return record

    monkeypatch.setattr(DecisionResult, "to_record", before)
    old, _ = decide_for(trial, epic_chart().client(), "p1", "Patient/p1")
    monkeypatch.setattr(DecisionResult, "to_record", original)
    new, _ = decide_for(trial, epic_chart().client(), "p1", "Patient/p1")
    declared, _ = decide_for(
        trial, epic_chart().client(), "p1", "Patient/p1", declared=declaration()
    )
    plain = inbox.decide({"message": {"body": "Please call me about my bill."}})
    shapes = {}
    for result in (old, new, declared, plain):
        item = store.get(result.id)
        assert item is not None
        shapes[result.id] = item["record"].get("ehr_declaration", "absent")
    assert shapes[old.id] == "absent"
    assert shapes[new.id] is None and shapes[plain.id] is None
    assert shapes[declared.id] == declaration().stamp()
    assert store.verify()["ok"] is True
    store.close()


def test_an_epic_shaped_chart_decides_with_the_declaration() -> None:
    """The 2026-11-08 run's three gaps together, on a synthetic chart shaped as Epic is assumed to
    send it (A1, A2): 22 notes as HTML and RTF twins, 26 orders of 26 drugs, no MedicationStatement
    API. Without the declaration, the one reason left is MedicationStatement's refusal; with it,
    the chart is decided, nothing unread, and a person reviews it (risk level 2)."""
    trial = Decision.from_hub(TRIAL)
    ehr = epic_chart(notes=22, orders=25)
    result, _ = decide_for(trial, ehr.client(), "p1", "Patient/p1")
    assert result.reasons == ("could not read medications: unauthorized",)

    ehr = epic_chart(notes=22, orders=25)
    result, _ = decide_for(trial, ehr.client(), "p1", "Patient/p1", declared=declaration())
    assert result.unreadable == () and result.outcome is Outcome.HUMAN_REVIEW
    assert result.recommendation is not None and LIMITATION in result.reasons
    coverage = coverage_of(result)
    assert coverage["links"]["read"] == {"follows": 26, "notes": 22}
    assert coverage["fields"]["notes"]["unreadable"] == {}
    assert coverage["lists"]["medications"]["items"] == 26
