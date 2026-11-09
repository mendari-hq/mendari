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

Every record is synthetic, every id and system fictional, and the shapes Epic is assumed to send
are labelled as assumptions; nothing remote is called. The trial pack is read from `packs/`,
unchanged."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from mendari import Decision, load_pack
from mendari.providers import deadline as deadline_module
from mendari.standards import linked
from mendari.standards.fhir import format_label, unread_attachments, unread_notes
from mendari.standards.linked import (
    MAX_FOLLOW_READS,
    MAX_NOTE_READS,
    MAX_REQUEST_READS,
    Linker,
)
from tests.test_fhir_linked import (
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
