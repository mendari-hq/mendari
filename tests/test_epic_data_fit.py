# SPDX-License-Identifier: Apache-2.0
"""Epic data fit (2026-11-08): what the first SMART launch on Epic's public sandbox showed, a
chart-reading agent abstaining for a test patient on three gaps of Mendari's, each pinned here on
the chart-summary test fixture (`mendari-test/chart-summary`), a copy of it reading links
(`tests/test_fhir_linked.py` `linked_agent`), and an Epic-shaped server of its records
(`tests/support/chart_ehr.py` `ChartEhr`).

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
are labelled as assumptions; nothing remote is called. The fixture is read from its folder,
unchanged."""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from dataclasses import replace
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
from tests.conftest import CHART_DIR, CLINICAL_DIR, FIXTURE_AGENTS, TRIAGE_DIR
from tests.jwt_signers import PUBLIC_URL
from tests.support.chart_ehr import ChartEhr, Rendition, Statements, vendor_shaped
from tests.support.chart_fixture import CHART_ID, SERVICE, chart_copy
from tests.support.chart_fixture import hook as chart_hook
from tests.support.fixture_records import (
    BASE,
    OTHER_DRUGS,
    RXNORM,
    Case,
    Condition,
    Medication,
    Note,
    ehr_declaration,
)
from tests.test_cds_auth import sign_in_on
from tests.test_cds_clients import RSA_EHR, register, token
from tests.test_cds_clients import entry as client_entry
from tests.test_fhir_linked import (
    EHR,
    ORIGIN,
    Ehr,
    b64,
    binary,
    bundle,
    decide,
    fhir_json,
    linked_agent,
    linking,
    medication,
    note,
    request_for,
)
from tests.test_smart import FakeEhr, full_launch, signed_site

HTML = "text/html"
RTF = "text/rtf"  # how an Epic note's second rendition is assumed to be typed (A1)


@pytest.fixture
def agent(tmp_path: Path) -> Decision:
    return Decision(load_pack(linked_agent(tmp_path / "agents")))


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
    agent: Decision,
) -> None:
    """A note none of whose renditions is read counts once, by its text rendition's reason: an
    HTML twin past the budget is `cap` alone (it was `cap` and `unsupported_type` for its RTF
    twin, the 2026-11-08 run's reasons), an HTML twin whose read fails is that failure alone. A
    note with no text rendition (RTF only) still abstains, `unsupported_type`, its format
    counted; an empty HTML beside an RTF reads no text, so that note counts, by the RTF. Outside
    renditions (a Communication's payloads) each attachment still counts."""
    documents, htmls = twins(MAX_NOTE_READS + 5)
    ehr = Ehr(*htmls)
    result = decide(agent, *documents, ehr=ehr)
    assert result.reasons == ("could not read notes: cap",)
    notes = coverage_of(result)["fields"]["notes"]
    assert notes["unreadable"] == {"cap": 5} and "unread_formats" not in notes
    assert len(ehr.reads()) == MAX_NOTE_READS  # never an RTF: its type says it is not text

    document, html = twin(1)
    ehr = Ehr(html)
    ehr.faults["Binary/h1"] = lambda r: fhir_json({"issue": []}, status=500)
    result = decide(agent, document, ehr=ehr)
    assert result.reasons == ("could not read notes: http_status",)
    assert coverage_of(result)["fields"]["notes"]["unreadable"] == {"http_status": 1}

    readable, html = twin(2)
    rtf_only = note("n3", linking("Binary/r3", RTF))
    result = decide(agent, readable, rtf_only, ehr=Ehr(html))
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
    result = decide(agent, empty, ehr=Ehr())
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
    agent: Decision,
) -> None:
    """The 2026-11-08 run's cause: notes, read first, used every link a decision had (20), and
    the drugs behind the MedicationRequests were `cap`. With a budget per purpose, 25 twin notes
    and 30 orders of 30 drugs all read: no reason, each Binary and Medication once."""
    documents, htmls = twins(25)
    requests, medications = drugs(30)
    ehr = Ehr(*htmls, *medications)
    result = decide(agent, *documents, *requests, ehr=ehr)
    assert result.unreadable == () and result.answers
    assert coverage_of(result)["links"] == {
        "budget": {"follows": MAX_FOLLOW_READS, "notes": MAX_NOTE_READS},
        "read": {"follows": 30, "notes": 25},
    }
    assert len(ehr.reads()) == 55 == len(set(ehr.reads()))


def test_linked_reads_are_capped_per_purpose_per_decision_and_per_request(
    agent: Decision,
) -> None:
    """Past its purpose's budget a link is `cap` and is not read, and the other purpose's
    budget is untouched: 60 notes read 50 beside 5 drugs read, 60 drugs read 50. One request's
    decisions together send at most `MAX_REQUEST_READS` reads: past them a link is `cap`,
    nothing is sent, and the decision abstains."""
    documents, htmls = twins(60)
    requests, medications = drugs(5)
    ehr = Ehr(*htmls, *medications)
    result = decide(agent, *documents, *requests, ehr=ehr)
    assert result.reasons == ("could not read notes: cap",)
    coverage = coverage_of(result)
    assert coverage["fields"]["notes"]["unreadable"] == {"cap": 10}
    assert coverage["links"]["read"] == {"follows": 5, "notes": 50}
    source = coverage["lists"]["medications"]["sources"]["MedicationRequest"]
    assert source["unreadable"] == {} and "unreadable" not in source["fields"]["code"]

    requests, medications = drugs(60)
    ehr = Ehr(*medications)
    result = decide(agent, *requests, ehr=ehr)
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
    first, _ = agent.decide_fhir(bundle(*documents, *requests), links=links)
    assert first.unreadable == () and len(ehr.reads()) == MAX_REQUEST_READS
    second, _ = agent.decide_fhir(bundle(late_note), links=links)
    assert second.reasons == ("could not read notes: cap",)
    assert len(ehr.reads()) == MAX_REQUEST_READS  # nothing more was sent
    assert coverage_of(second)["fields"]["notes"]["unreadable"] == {"cap": 1}
    assert coverage_of(second)["links"]["read"] == {"follows": 0, "notes": 0}


def test_a_linked_read_s_status_is_recorded_as_a_number(agent: Decision) -> None:
    """A linked read that fails with an HTTP status counts its status, as a number, in the
    decision's `links` section (each read once a request, counted for each decision it fails);
    no other read adds one, and a decision with no linked read has no `links` section."""
    documents, htmls = twins(3)
    requests, medications = drugs(2)
    ehr = Ehr(*htmls, *medications)
    ehr.faults["Binary/h0"] = lambda r: fhir_json({}, status=403)
    ehr.faults["Binary/h1"] = lambda r: fhir_json({}, status=404)
    ehr.faults["Medication/m0"] = lambda r: fhir_json({}, status=403)
    result = decide(agent, *documents, *requests, ehr=ehr)
    assert result.reasons == (
        "could not read medications.code: unauthorized",
        "could not read notes: http_status",
        "could not read notes: unauthorized",
    )
    links = coverage_of(result)["links"]
    assert links["statuses"] == {"403": 2, "404": 1}
    assert links["read"] == {"follows": 2, "notes": 3}
    inline = note("n9", {"contentType": "text/plain", "data": b64("Seen.")})
    assert "links" not in coverage_of(decide(agent, inline, ehr=Ehr()))
    assert "links" not in coverage_of(decide(agent, inline))


def test_one_drug_ordered_many_times_is_read_once(agent: Decision) -> None:
    """Forty orders of one drug (one Medication id, as Epic is assumed to reuse it: A4) read
    that Medication once, and count once against the decision's follow budget."""
    requests = [request_for(f"r{n}", "Medication/m1") for n in range(40)]
    ehr = Ehr(medication("m1", "RX-1"))
    result = decide(agent, *requests, ehr=ehr)
    assert result.unreadable == () and ehr.reads() == ["Medication/m1"]
    assert coverage_of(result)["links"]["read"] == {"follows": 1, "notes": 0}


def test_a_note_field_stops_reading_once_its_text_is_longer_than_the_input_takes() -> None:
    """Once a note field's text, as the field joins it, is longer than the agent's input takes
    (the chart agent's 20,000 characters), the decision abstains `too_large` whatever the other
    notes hold: they are not read, each is `too_large`, and no other reason is added. A chart
    whose notes fit reads them all."""
    chart = Decision(load_pack(CHART_DIR))
    long = "x" * 6_000
    pairs = [twin(n, f"<p>{long}</p>") for n in range(10)]
    ehr = Ehr(*(html for _, html in pairs))
    links = Linker.of(ehr.client())
    record = bundle(*(document for document, _ in pairs))
    result, _ = chart.decide_fhir(record, links=links)
    assert result.reasons == ("could not read notes: too_large",)
    assert len(ehr.reads()) == 4  # 4 x 6,000 characters (and the blank lines) pass 20,000
    notes = coverage_of(result)["fields"]["notes"]
    assert notes["unreadable"] == {"too_large": 6 + 1}  # the 6 not read, and the field
    assert coverage_of(result)["links"]["read"] == {"follows": 0, "notes": 4}

    short = [twin(n, "<p>Seen.</p>") for n in range(10)]
    ehr = Ehr(*(html for _, html in short))
    result, _ = chart.decide_fhir(
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
    decision = Decision(load_pack(linked_agent(tmp_path / "agents")), deadline_s=10)
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
    provider = decision.providers["reader"]
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

# The site's declaration for the chart's EHR (`fixture_records.ehr_declaration`): it does not
# offer MedicationStatement, as Epic has no R4 MedicationStatement API.
DECLARED = ehr_declaration()
ATTESTED = DECLARED[EHR]["attested_by"]  # who attested it (fictional)
REFERENCE = DECLARED[EHR]["reference"]
VERSION = DECLARED[EHR]["version"]
LIMITED = (
    "medications may be incomplete: read without MedicationStatement, which this EHR does not "
    f"offer (declared, {ATTESTED})"
)
LIMITATION = f"{LIMITED}: a person decides"
# A synthetic adult with type 2 diabetes on metformin: a routine follow-up, which the agent
# automates when every source of her record reads. As Epic serves her (`ChartEhr`): her
# conditions coded the site's way beside ICD-10-CM, her drugs MedicationRequests naming a
# Medication (A2), her notes HTML and RTF twins (A1), and no MedicationStatement API.
ROUTINE = Case(
    id="chart-epic-1",
    age=58,
    sex="female",
    conditions=(Condition("E11.9", "2019-03-01"),),
    medications=(Medication("6809", "2026-03-01"),),
    notes=(Note(("Reviewed at the clinic today.", "Routine follow-up in three months.")),),
)
# The same patient with warfarin ordered as well.
ANTICOAGULATED = replace(
    ROUTINE, medications=(*ROUTINE.medications, Medication("11289", "2026-09-01"))
)
PATIENT_ID = ROUTINE.id
FOCUS = f"Patient/{PATIENT_ID}"
STATEMENTS = "MedicationStatement?patient={{context.patientId}}"


def declarations(entries: dict[str, Any] = DECLARED, *, signed_in: bool = False) -> EhrDeclarations:
    return parse(json.dumps(entries), signed_in=signed_in)


def declaration(entries: dict[str, Any] = DECLARED) -> EhrDeclaration:
    found = declarations(entries).for_base(EHR, DEFAULT_ORG)
    assert found is not None
    return found


def epic_chart(
    case: Case = ROUTINE,
    *,
    statements: Statements = "refused",
    rendition: Rendition = "twin",
    faults: dict[str, int] | None = None,
    base: str = BASE,
) -> ChartEhr:
    """The case's record as an EHR with no R4 MedicationStatement serves it: its
    MedicationStatement search answers 403 (as Epic's did on 2026-11-08; `statements`), its
    notes HTML and RTF twins (`rendition`), under `base`."""
    resources = vendor_shaped(case, rendition=rendition)
    return ChartEhr(resources, base=base, statements=statements, faults=faults)


def unconformant_chart() -> ChartEhr:
    """`epic_chart()` with US Core errors: a Patient with no name, orders with no requester."""
    resources = vendor_shaped(ROUTINE, rendition="twin")
    for resource in resources:
        if resource["resourceType"] == "Patient":
            resource.pop("name")
        if resource["resourceType"] == "MedicationRequest":
            resource.pop("requester")
    return ChartEhr(resources, statements="refused")


def searched(ehr: ChartEhr) -> list[str]:
    """The searches the EHR was sent, by resource type."""
    return [search.partition("?")[0] for search in ehr.searches()]


def hook(server: str = EHR) -> dict[str, Any]:
    """A patient-view request from the EHR for the routine patient, with its server and token,
    and no prefetch: every key is fetched."""
    return chart_hook(ROUTINE, prefetched=False, fhir_server=server, token="epic-test-token")


def sent(**keys: Any) -> dict[str, Any]:
    """The routine patient's patient-view request with the whole prefetch, as an EHR sends it,
    `keys` replacing what it would send."""
    request = chart_hook(ROUTINE, fhir_server=EHR, token="epic-test-token")
    request["prefetch"].update(keys)
    return request


def invoked(
    monkeypatch: pytest.MonkeyPatch,
    decision: Decision,
    ehr: ChartEhr,
    request: dict[str, Any],
    declared: EhrDeclaration | None,
) -> DecisionResult:
    """The one decision a hook request made, with `ehr` as the allow-listed server behind its
    `fhirServer` (for the prefetch it fetches and the links it reads)."""
    ehr.install(monkeypatch)
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


def chart_agent(
    base: Path,
    *,
    rules: list[dict[str, Any]] | None = None,
    prefetch: dict[str, str] | None = None,
    changes: dict[str, Any] | None = None,
    use: str | None = None,
    schema: Callable[[dict[str, Any]], None] | None = None,
) -> Path:
    """A copy of the chart-summary agent (risk level 1: it automates a routine follow-up): its
    policy's rules and `rules`, its prefetch templates with `prefetch`, its mapping changed by
    `changes`, on its `use` variant, its input schema edited by `schema`."""

    def manifest(data: dict[str, Any]) -> None:
        data["policy"]["rules"].extend(rules or [])
        data["cds_hooks"]["prefetch"].update(prefetch or {})

    return chart_copy(base, changes, manifest=manifest, use=use, schema=schema)


def chart() -> Decision:
    return Decision(load_pack(CHART_DIR))


def test_an_undeclared_medication_statement_refusal_never_reads_as_off_insulin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a declaration, the EHR refusing MedicationStatement makes the decision ABSTAIN
    before any provider, through the SMART launch's decide and through a hook that fetches its
    prefetch: no answer exists, so "no anticoagulant listed" never becomes "not on one"."""
    ehr = epic_chart()
    result, _ = decide_for(chart(), ehr.client(), PATIENT_ID, FOCUS)
    assert result.outcome is Outcome.ABSTAIN and result.recommendation is None
    assert result.reasons == ("could not read medications: unauthorized",)
    assert result.answers == {} and result.ehr_declaration is None
    assert "MedicationStatement" in searched(ehr)
    refused = {"resource": "MedicationStatement", "reason": "unauthorized", "status": 403}
    assert coverage_of(result)["prefetch"] == {"medications": refused}

    hooked = invoked(monkeypatch, chart(), epic_chart(), hook(), None)
    assert hooked.reasons == ("could not read medications: unauthorized",)
    assert hooked.answers == {} and hooked.ehr_declaration is None
    assert coverage_of(hooked)["prefetch"] == {"medications": refused}


def test_a_declared_not_offered_type_is_not_requested_and_the_decision_records_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the site's declaration for this base and organization, MedicationStatement is never
    asked for: the list is read from the MedicationRequests, the source and the list are marked
    not offered (its 0 is not "read and empty"), the decision records the declaration and goes to
    a person with the limitation as a reason. There, and only there, "no anticoagulant among the
    orders" reads as "not on one", on the site's word; an anticoagulant order still reads."""
    declared = declaration()
    ehr = epic_chart()
    result, _ = decide_for(chart(), ehr.client(), PATIENT_ID, FOCUS, declared=declared)
    assert "MedicationStatement" not in searched(ehr) and "MedicationRequest" in searched(ehr)
    assert result.unreadable == () and result.outcome is Outcome.HUMAN_REVIEW
    assert LIMITATION in result.reasons
    assert (
        result.ehr_declaration
        == declared.stamp()
        == {
            "version": VERSION,
            "sha256": declared.sha256,
            "attested_by": ATTESTED,
            "reference": REFERENCE,
        }
    )
    assert coverage_of(result)["prefetch"] == {
        "medications": {"resource": "MedicationStatement", "not_offered": True}
    }
    medications = coverage_of(result)["lists"]["medications"]
    assert medications["not_offered"] == ["MedicationStatement"]
    statements = medications["sources"]["MedicationStatement"]
    assert (statements["not_offered"], statements["resources"], statements["items"]) == (True, 0, 0)
    requests = medications["sources"]["MedicationRequest"]
    assert "not_offered" not in requests and requests["items"] == 1  # the metformin order
    assert result.answers["on_anticoagulant"].label == "no"
    assert result.answers["follow_up"].label == "routine"
    assert result.to_record()["ehr_declaration"] == declared.stamp()

    ehr = epic_chart(ANTICOAGULATED)
    result, _ = decide_for(chart(), ehr.client(), PATIENT_ID, FOCUS, declared=declared)
    assert result.answers["on_anticoagulant"].label == "yes"
    assert LIMITATION in result.reasons

    hooked = invoked(monkeypatch, chart(), epic_chart(), hook(), declared)
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
    signed = declarations(ehr_declaration(org="acme"), signed_in=True)
    assert signed.for_base(EHR, DEFAULT_ORG) is None and signed.for_base(EHR, "acme") is not None
    ehr = epic_chart()
    other = found.for_base("https://other.example.org/fhir", DEFAULT_ORG)
    result, _ = decide_for(chart(), ehr.client(), PATIENT_ID, FOCUS, declared=other)
    assert result.reasons == ("could not read medications: unauthorized",)


def test_a_declaration_never_automates(tmp_path: Path) -> None:
    """An agent that automates this chart when every source reads goes to a person when a
    declared source was left out, the limitation a reason; its rule's BLOCK does too. Without
    the declaration, the refused source abstains."""
    declared = declaration()
    whole, _ = decide_for(chart(), epic_chart(statements="absent").client(), PATIENT_ID, FOCUS)
    assert whole.outcome is Outcome.AUTO and whole.ehr_declaration is None
    result, _ = decide_for(chart(), epic_chart().client(), PATIENT_ID, FOCUS, declared=declared)
    assert result.outcome is Outcome.HUMAN_REVIEW
    assert result.recommendation == whole.recommendation == "routine"
    assert LIMITATION in result.reasons
    refused, _ = decide_for(chart(), epic_chart().client(), PATIENT_ID, FOCUS)
    assert refused.reasons == ("could not read medications: unauthorized",)

    rule = {
        "name": "fixture-block",
        "when": {"question": "follow_up", "labels": ["routine"]},
        "outcome": "BLOCK",
        "reason": "a fixture's blocking rule",
    }
    blocking = Decision(load_pack(chart_agent(tmp_path / "block", rules=[rule])))
    absent = epic_chart(statements="absent")
    whole, _ = decide_for(blocking, absent.client(), PATIENT_ID, FOCUS)
    assert whole.outcome is Outcome.BLOCK
    result, _ = decide_for(blocking, epic_chart().client(), PATIENT_ID, FOCUS, declared=declared)
    assert result.outcome is Outcome.HUMAN_REVIEW
    assert f"{LIMITATION} instead of its rule's BLOCK" in result.reasons


def test_a_declared_key_a_launch_cannot_fill_is_not_offered(tmp_path: Path) -> None:
    """A declared key whose template needs context the SMART launch does not have (an encounter)
    is left out as not offered, never a `500`: nothing of its type is asked for, and the decision
    records the declaration and goes to a person, the limitation a reason."""
    template = f"{STATEMENTS}&encounter={{{{context.encounterId}}}}"
    agent = Decision(load_pack(chart_agent(tmp_path, prefetch={"medications": template})))
    declared = declaration()
    ehr = epic_chart()
    result, _ = decide_for(agent, ehr.client(), PATIENT_ID, FOCUS, declared=declared)
    assert result.outcome is Outcome.HUMAN_REVIEW and LIMITATION in result.reasons
    assert result.unreadable == () and result.ehr_declaration == declared.stamp()
    assert "MedicationStatement" not in searched(ehr)
    assert coverage_of(result)["prefetch"] == {
        "medications": {"resource": "MedicationStatement", "not_offered": True}
    }


def test_a_refused_input_under_a_declaration_still_names_the_limitation() -> None:
    """Under `enforce`, an input refused for its US Core errors (a patient with no name, orders
    with no `requester`) is a final BLOCK: the limitation is still one of its reasons, without
    "a person decides", beside the declaration it records."""
    strict = Decision(load_pack(CHART_DIR), fhir_validation="enforce")
    declared = declaration()
    ehr = unconformant_chart()
    result, _ = decide_for(strict, ehr.client(), PATIENT_ID, FOCUS, declared=declared)
    assert result.outcome is Outcome.BLOCK and result.answers == {}
    assert result.reasons[0].startswith("input rejected: not US Core")
    assert result.reasons[-1] == LIMITED and LIMITATION not in result.reasons
    assert result.ehr_declaration == declared.stamp()


LINKED = cds_hooks.CardLinks(public_url=PUBLIC_URL)


def bullets(text: str) -> list[str]:
    """A card detail's (or a part of one's) `- ` lines."""
    return [line for line in text.splitlines() if line.startswith("- ")]


def test_a_card_keeps_the_limitation_whatever_the_list_is_called(tmp_path: Path) -> None:
    """A list's name is the agent's own, spaces and all: under a declaration, the card still
    keeps the line that says the list may be incomplete in its detail when the engine's other
    reasons are behind the audit link, as it does for `medications`."""
    name = "current meds"
    own = yaml.safe_load((CHART_DIR / "fhir" / "mapping.yaml").read_text(encoding="utf-8"))

    def renamed(document: dict[str, Any]) -> None:
        properties = document["properties"]
        properties[name] = properties.pop("medications")

    changes = {"lists": {"medications": None, name: own["lists"]["medications"]}}
    agent = Decision(load_pack(chart_agent(tmp_path, changes=changes, use="rules", schema=renamed)))
    result, task = decide_for(
        agent, epic_chart().client(), PATIENT_ID, FOCUS, declared=declaration()
    )
    limited = LIMITATION.replace("medications", name, 1)
    assert result.outcome is Outcome.HUMAN_REVIEW and limited in result.reasons
    assert bullets(cds_hooks.card(agent.pack, result, task, LINKED)["detail"]) == [f"- {limited}"]


def test_a_refused_input_s_card_says_the_limitation_apart_from_why() -> None:
    """A card keyed `refused` (the chart agent's) lists why the input was refused, whatever the
    links; under a declaration its limitation is on the card too, but apart (`ALSO_NOTED`),
    since it is not why: the refusal is the US Core errors."""
    strict = Decision(load_pack(CHART_DIR), fhir_validation="enforce")
    result, task = decide_for(
        strict, unconformant_chart().client(), PATIENT_ID, FOCUS, declared=declaration()
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
    agent = chart()
    ehr = epic_chart(faults={"Condition": 403})
    result, task = decide_for(agent, ehr.client(), PATIENT_ID, FOCUS, declared=declaration())
    assert result.abstained_unreadable and LIMITATION in result.reasons
    for links in (cds_hooks.NO_LINKS, LINKED):
        card = cds_hooks.card(agent.pack, result, task, links)
        unread, also = card["detail"].split(f"\n{cds_hooks.ALSO_NOTED}\n")
        assert bullets(unread) == [f"- {key}: {reason}" for key, reason in result.unreadable]
        assert bullets(also) == [f"- {LIMITATION}"]
        assert cds_client.unread_of(card) == set(result.unreadable)


def test_a_declared_type_a_pack_reads_elsewhere_is_read_as_without_the_declaration(
    tmp_path: Path,
) -> None:
    """A declared type an agent also reads elsewhere (a single-source list, a top-level field),
    or whose list's every source is declared, is read as without the declaration, and its
    refusal abstains: a declaration can never leave a list or a field with no source."""
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

    def status(document: dict[str, Any]) -> None:
        document["properties"]["last_status"] = {"type": "string", "maxLength": 64}

    for name, changes, schema in (("single", single, None), ("field", field, status)):
        agent = Decision(load_pack(chart_agent(tmp_path / name, changes=changes, schema=schema)))
        mapping = FhirAdapter.for_pack(agent.pack).mapping
        hooks = agent.pack.manifest.cds_hooks
        assert hooks is not None
        assert excusable(mapping, hooks.prefetch, {"MedicationStatement"}) == frozenset(), name
        result, _ = decide_for(agent, epic_chart().client(), PATIENT_ID, FOCUS, declared=declared)
        assert result.reasons == ("could not read medications: unauthorized",), name

    both = declaration(
        {EHR: {**DECLARED[EHR], "not_offered": ["MedicationRequest", "MedicationStatement"]}}
    )
    agent = chart()
    hooks = agent.pack.manifest.cds_hooks
    assert hooks is not None
    mapping = FhirAdapter.for_pack(agent.pack).mapping
    assert excusable(mapping, hooks.prefetch, both.not_offered) == frozenset()
    assert both.skipped(mapping, hooks.prefetch, hooks.prefetch) == {}
    ehr = epic_chart()
    result, _ = decide_for(agent, ehr.client(), PATIENT_ID, FOCUS, declared=both)
    assert result.reasons == ("could not read medications: unauthorized",)
    assert "MedicationStatement" in searched(ehr)
    with pytest.raises(ValueError, match="needs the declaration"):
        agent.decide_fhir(bundle(), not_offered={"medications": "MedicationStatement"})
    with pytest.raises(ValueError, match="may not be left out"):
        agent.decide_fhir(
            bundle(), not_offered={"notes": "DocumentReference"}, declaration=declared
        )


def test_a_cds_client_may_still_send_a_declared_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under a declaration, what a client sends for a declared key is data when it holds entries,
    used as sent, and an OperationOutcome is still `operation_outcome`; sent null, empty (`{}`)
    or as a search with no entries, it says nothing more than the declaration does: the key is
    not offered, recorded, never fetched, never automated (no `412` either)."""
    declared = declaration()
    statement = {
        "resourceType": "MedicationStatement",
        "id": "ms1",
        "status": "active",
        "subject": {"reference": FOCUS},
        "medicationCodeableConcept": {
            "coding": [{"system": RXNORM, "code": "11289"}],
            "text": "warfarin 5 mg tablet",
        },
        "effectivePeriod": {"start": "2026-09-01"},
    }
    given = {"resourceType": "Bundle", "type": "searchset", "entry": [{"resource": statement}]}
    result = invoked(monkeypatch, chart(), epic_chart(), sent(medications=given), declared)
    assert result.unreadable == () and result.ehr_declaration is None
    assert result.answers["on_anticoagulant"].label == "yes"
    assert LIMITATION not in result.reasons

    outcome = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "forbidden"}],
    }
    result = invoked(monkeypatch, chart(), epic_chart(), sent(medications=outcome), declared)
    assert result.reasons == ("could not read medications: operation_outcome",)

    empty = {"resourceType": "Bundle", "type": "searchset"}
    for name, value in (("null", None), ("empty", {}), ("no entries", empty)):
        ehr = epic_chart()
        result = invoked(monkeypatch, chart(), ehr, sent(medications=value), declared)
        assert result.unreadable == (), name
        assert result.ehr_declaration == declared.stamp(), name
        assert LIMITATION in result.reasons, name
        assert coverage_of(result)["lists"]["medications"]["not_offered"] == ["MedicationStatement"]
        assert ehr.requests == [], name  # nothing fetched: the rest was sent
    # Without the declaration, null is "no data" and an empty search is read and empty, as before.
    result = invoked(monkeypatch, chart(), epic_chart(), sent(medications=None), None)
    assert result.ehr_declaration is None and LIMITATION not in result.reasons


# -- the setting, checked at startup -------------------------------------------------------------

REGISTERED = {EHR: Registration(base=EHR, org=DEFAULT_ORG, level="decide")}


def entry(**changes: Any) -> dict[str, Any]:
    return {**DECLARED[EHR], **changes}


def installed() -> list[Any]:
    """The test fixtures' agents a deployment of them finds (the chart agent reads its
    medications from MedicationStatement and MedicationRequest)."""
    return [load_pack(folder) for folder in (CHART_DIR, TRIAGE_DIR, CLINICAL_DIR)]


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
        create_app(db_path=tmp_path / "a.db", packs_paths=[FIXTURE_AGENTS])


def test_each_declaration_is_checked_against_the_deployment() -> None:
    """A declared base must be on the allow-list and registered (for launches, or in a CDS
    client's `fhir_servers`), for the organization those registrations name when sign-in is on;
    each declared type must be one an agent found here may read a list without. The same entry
    written in another order has the same digest; a changed one another."""
    agents = installed()

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
            check(found, agents, **arguments)
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
    check(declarations(), agents, signed_in=False, ehrs=REGISTERED, cds=None, allowed={ORIGIN})
    found = parse(json.dumps(signed), signed_in=True)
    check(found, agents, signed_in=True, ehrs={}, cds=cds_listing(EHR, "acme"), allowed={ORIGIN})

    same = declaration({EHR: dict(reversed(list(entry().items())))})
    assert same.sha256 == declaration().sha256
    changed = declaration({EHR: entry(version="chart-ehr-2")})
    assert changed.sha256 != declaration().sha256
    assert json.loads(canonical(declaration())) == {
        "format": "mendari.ehr-declarations/1",
        "base": EHR,
        "org": DEFAULT_ORG,
        "version": VERSION,
        "attested_by": ATTESTED,
        "reference": REFERENCE,
        "not_offered": ["MedicationStatement"],
    }


def test_the_setting_is_inline_json_or_a_file_and_off_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset, there is no declaration and every source is read. Inline JSON and a file give the
    same declarations; a file that cannot be read, is too large or is not UTF-8 stops the
    server."""
    agents = installed()
    kwargs: dict[str, Any] = {"signed_in": False, "ehrs": REGISTERED, "cds": None}
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", ORIGIN)
    assert ehr_declarations(agents, **kwargs) is None
    monkeypatch.setenv(ENV, json.dumps(DECLARED))
    inline = ehr_declarations(agents, **kwargs)
    path = tmp_path / "declared.json"
    path.write_text(json.dumps(DECLARED))
    monkeypatch.setenv(ENV, str(path))
    assert ehr_declarations(agents, **kwargs) == inline is not None
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
            ehr_declarations(agents, **kwargs)


def test_health_shows_the_organizations_declarations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/api/health` shows what the caller's organization's EHRs do not offer, by base: each
    declaration's version, digest, who attested it, its reference and the types; null when
    nothing is declared."""
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", ORIGIN)
    monkeypatch.setenv("MENDARI_SMART_EHRS", f"{EHR}={DEFAULT_ORG}:decide")
    with TestClient(create_app(db_path=tmp_path / "a.db", packs_paths=[FIXTURE_AGENTS])) as app:
        assert app.get("/api/health").json()["ehr_declarations"] is None
    monkeypatch.setenv(ENV, json.dumps(DECLARED))
    with TestClient(create_app(db_path=tmp_path / "b.db", packs_paths=[FIXTURE_AGENTS])) as app:
        shown = app.get("/api/health").json()["ehr_declarations"]
    assert shown == {EHR: {**declaration().stamp(), "not_offered": ["MedicationStatement"]}}


# -- through the app: a hook and a launch read the declaration for their base and organization ----

EHR2 = "https://ehr2.example.org/fhir"  # another base the same CDS client registered, undeclared
NOT_OFFERED = {"resource": "MedicationStatement", "not_offered": True}


def answering(*ehrs: ChartEhr) -> httpx.MockTransport:
    """One transport for several EHRs, each answering under its own base."""

    def handler(request: httpx.Request) -> httpx.Response:
        for ehr in ehrs:
            if str(request.url).startswith(f"{ehr.base}/"):
                return ehr(request)
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)


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
    first, second = epic_chart(), epic_chart(base=EHR2)
    patched = functools.partial(FhirClient, transport=answering(first, second))
    monkeypatch.setattr(cds_hooks, "FhirClient", patched)
    declared = declarations({EHR: entry(org="acme")}, signed_in=True).for_base(EHR, "acme")
    assert declared is not None
    db = tmp_path / "a.db"

    def called(server: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """The audited record of the one decision a call for `server` made, and its card."""
        with TestClient(create_app(db_path=db), base_url=PUBLIC_URL) as app:
            response = app.post(
                SERVICE,
                json=hook(server),
                headers={"authorization": f"Bearer {token(RSA_EHR, SERVICE)}"},
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
    assert "MedicationStatement" not in searched(first)
    assert card["summary"] == "Synthetic chart summary: routine follow-up; anticoagulant listed: No"
    assert bullets(card["detail"]) == [f"- {LIMITATION}"]
    record, _ = called(EHR2)
    assert record["ehr_declaration"] is None
    assert record["reasons"] == ["could not read medications: unauthorized"]
    assert "MedicationStatement" in searched(second)

    for clients, org in (
        ([client], "other"),
        ([client, client_entry("other", org="other", iss="https://other.example.org")], "acme"),
    ):
        register(monkeypatch, *clients, allowed=f"{ORIGIN},https://ehr2.example.org")
        monkeypatch.setenv(ENV, json.dumps({EHR: entry(org=org)}))
        with pytest.raises(MendariError, match="every CDS client"):
            create_app(db_path=tmp_path / "refused.db")


class EpicLauncher(FakeEhr):
    """The SMART tests' EHR (`test_smart.FakeEhr`, at `EHR`), launching for the routine patient,
    its FHIR reads answered by `chart` once the token is one it issued."""

    def __init__(self, chart: ChartEhr) -> None:
        super().__init__(iss=EHR, root=ORIGIN)
        self.chart = chart
        self.patient = PATIENT_ID

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
    ehr = epic_chart()
    launcher = EpicLauncher(ehr)
    monkeypatch.setenv(ENV, json.dumps({EHR: entry(org="clinic-a")}))
    app = signed_site(tmp_path / "a.db", monkeypatch, f"{EHR}=clinic-a:decide", launcher)
    assert full_launch(app, launcher).status_code == 302
    decided = app.post("/api/smart/decide", json={"pack_id": CHART_ID, "focus": FOCUS})
    assert decided.status_code == 200, decided.text
    decision = decided.json()["decision"]
    declared = declarations({EHR: entry(org="clinic-a")}, signed_in=True).for_base(EHR, "clinic-a")
    assert declared is not None and decision["ehr_declaration"] == declared.stamp()
    assert decision["outcome"] == "HUMAN_REVIEW" and LIMITATION in decision["reasons"]
    assert decision["data_coverage"]["prefetch"] == {"medications": NOT_OFFERED}
    assert "MedicationStatement" not in searched(ehr)

    monkeypatch.setenv(ENV, json.dumps({EHR: entry(org="clinic-b")}))
    with pytest.raises(MendariError, match="MENDARI_SMART_EHRS registers it to"):
        signed_site(tmp_path / "refused.db", monkeypatch, f"{EHR}=clinic-a:decide", launcher)


# -- an Epic-shaped chart, end to end ------------------------------------------------------------

# A synthetic chart shaped as Epic is assumed to send it (A1, A2): 22 notes as HTML and RTF twins,
# 26 orders of 26 drugs (each its own Medication), no MedicationStatement API.
CROWDED = Case(
    id="chart-epic-2",
    age=58,
    sex="female",
    conditions=(Condition("E11.9", "2019-03-01"),),
    medications=tuple(
        Medication(OTHER_DRUGS[n % len(OTHER_DRUGS)], f"2026-{1 + n % 9:02d}-{1 + n:02d}")
        for n in range(26)
    ),
    notes=tuple(
        Note((f"Visit {n}: reviewed at the clinic today.",), written=f"2026-09-{1 + n:02d}")
        for n in range(22)
    ),
)


def test_an_epic_shaped_chart_decides_with_the_declaration(tmp_path: Path) -> None:
    """The 2026-11-08 run's three gaps together, on a synthetic chart shaped as Epic is assumed to
    send it (A1, A2): 22 notes as HTML and RTF twins, 26 orders of 26 drugs, each read by its
    reference (the agent's copy asks for no `_include`), no MedicationStatement API. Without the
    declaration, the one reason left is MedicationStatement's refusal; with it, the chart is
    decided, nothing unread, and a person reviews it."""
    requests = {"medication_requests": "MedicationRequest?patient={{context.patientId}}"}
    agent = Decision(load_pack(chart_agent(tmp_path, prefetch=requests)))
    focus = f"Patient/{CROWDED.id}"
    ehr = epic_chart(CROWDED)
    result, _ = decide_for(agent, ehr.client(), CROWDED.id, focus)
    assert result.reasons == ("could not read medications: unauthorized",)

    ehr = epic_chart(CROWDED)
    result, _ = decide_for(agent, ehr.client(), CROWDED.id, focus, declared=declaration())
    assert result.unreadable == () and result.outcome is Outcome.HUMAN_REVIEW
    assert result.recommendation is not None and LIMITATION in result.reasons
    coverage = coverage_of(result)
    assert coverage["links"]["read"] == {"follows": 26, "notes": 22}
    assert coverage["fields"]["notes"]["unreadable"] == {}
    assert coverage["lists"]["medications"]["items"] == 26
