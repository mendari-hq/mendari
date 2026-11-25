# SPDX-License-Identifier: Apache-2.0
"""The data-coverage record (`standards/data_coverage.py`, `mendari.data-coverage/1`, LP-23d):
every FHIR decision says, for each mapped field and list, what it looked for (paths, the code
systems the agent declares, the agent's own codes) and what it found, as counts, with what could
not be read by reason code. It never holds a value, an id, text, a patient's code or a system
the agent does not declare; JSON decisions carry none. A spec 0.3 mapping (LP-23g) is counted
against what the adapter says each field looks for. Shown on the chart fixture
(`mendari-test/chart-summary`, a patient's record in) and copies of it with their mapping
changed (`tests/support/chart_fixture.py`)."""

from __future__ import annotations

import base64
import copy
import json
import os
import random
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, load_pack
from mendari.cli import main
from mendari.core.decision import DecisionResult
from mendari.core.phi import PhiPolicy
from mendari.server.smart_routes import decide_for
from mendari.standards import data_coverage, fhir_structure
from mendari.standards.data_coverage import FORMAT, OTHER
from mendari.standards.fhir import FhirAdapter
from mendari.standards.fhir_client import FhirClient
from mendari.standards.linked import Linker
from tests.conftest import CHART_DIR, INPUT_HASH_KEY, ROOT
from tests.support import chart_fixture
from tests.support import fixture_records as records
from tests.support.chart_ehr import ChartEhr
from tests.support.fixture_records import Case, Condition, Medication, Note

FIXTURES = ROOT / "tests" / "fixtures" / "us-core"
CHART = "mendari-test/chart-summary"
ICD = records.ICD10CM
MED = records.RXNORM  # the chart fixture's drugs' system
PAYER = "https://site.example/fhir/NamingSystem/payers"  # fictional: declared by a test's copy
# A site's own code system and code (shaped like an EHR's OID, which names its customer), and
# marks planted in a synthetic record: none may reach a data-coverage record.
SITE_SYSTEM = "urn:oid:1.2.840.114350.1.13.90210.7.2.696580"
SITE_CODE = "SITE-CODE-77123"
MRN = "MRN6627153"
NOTE_MARK = "NOTEMARK-silver-heron"
FILE_MARK = "LNK8842"  # a file name in a note's content type
PATIENT_ICD = "E11.65"  # a patient's ICD-10-CM code, in no code set of the agent's
PATIENT = {"resourceType": "Patient", "id": "p1", "birthDate": "1970-01-01", "gender": "female"}
# A chart the patient-view hook shows a card for (a routine follow-up).
ROUTINE = next(case for case in records.cases("test") if case.labels["follow_up"] == "routine")


def fixture(name: str) -> dict[str, Any]:
    return dict(json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8")))


def bundle(*resources: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{"resource": resource} for resource in resources],
    }


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def decided(record: Any, **kwargs: Any) -> DecisionResult:
    result, _ = Decision.from_hub(CHART).decide_fhir(record, **kwargs)
    return result


def test_each_mapped_field_says_what_was_looked_for_and_found_as_counts() -> None:
    result = decided(fixture("chart"))
    coverage = result.data_coverage
    assert coverage is not None and coverage["format"] == FORMAT
    adapter = FhirAdapter.for_pack(Decision.from_hub(CHART).pack)
    assert list(coverage["fields"]) == sorted(adapter.mapping.fields)
    assert list(coverage["lists"]) == sorted(adapter.mapping.lists)
    assert coverage["focus"] == {
        "codes": [],
        "paths": [],
        "resource": "Patient",
        "resources": 1,
        "unreadable": {},
    }
    assert coverage["fields"]["patient.age"] == {
        "codes": [],
        "codings": {},
        "paths": ["Patient.birthDate"],
        "resources": {"Patient": 1},
        "systems": [],
        "unreadable": {},
        "values": 1,
    }
    assert coverage["fields"]["patient.sex"]["codes"] == ["female", "male", "other", "unknown"]
    conditions = coverage["lists"]["conditions"]
    assert (conditions["resource"], conditions["resources"], conditions["items"]) == (
        "Condition",
        2,
        2,
    )
    code = conditions["fields"]["code"]
    assert code["paths"] == [f"code.coding[system={ICD}].code", "code.coding[0].code"]
    assert (code["values"], code["systems"]) == (2, [ICD])  # the system it selects on
    flag = conditions["fields"]["flag"]
    assert flag["codes"] == sorted(records.WATCH + records.LONG_TERM)  # its code set's
    # A coded path that selects on no system: every system the agent declares (`code_systems`).
    assert conditions["fields"]["status"]["systems"] == [ICD, MED]
    notes = coverage["fields"]["notes"]
    assert notes["paths"] == ["DocumentReference.content.attachment"]  # `attachment_text`
    assert (notes["resources"], notes["values"], notes["unreadable"]) == (
        {"DocumentReference": 2},
        2,
        {},
    )
    assert coverage["unreadable"] == {}


def test_codings_are_counted_by_declared_system_and_others_only_as_a_number() -> None:
    """A concept's codings are counted by the systems the agent declares; a site's own system,
    and a coding without one, count only as `other`, so a site sees the agent looked at a coding
    from a system it does not know, never which."""
    case = Case(
        id="cov-1",
        age=56,
        sex="female",
        conditions=(Condition("E11.9", "2019-04-01"),),
        notes=(Note(("Seen for diabetes follow-up.",)),),
    )
    resources = case.resources(records.TODAY)
    condition = next(r for r in resources if r["resourceType"] == "Condition")
    condition["code"]["coding"] = [
        {"system": SITE_SYSTEM, "code": SITE_CODE},
        {"system": ICD, "code": PATIENT_ICD},
        {"code": "no-system"},
    ]
    coverage = decided(bundle(*resources)).data_coverage
    assert coverage is not None
    code = coverage["lists"]["conditions"]["fields"]["code"]
    assert code["codings"] == {ICD: 1, OTHER: 2}
    assert ICD in code["systems"] and SITE_SYSTEM not in code["systems"]
    shown = json.dumps(coverage)
    for leak in (SITE_SYSTEM, "114350", SITE_CODE, PATIENT_ICD, "no-system"):
        assert leak not in shown, leak


def test_a_missing_list_and_an_empty_list_are_told_apart(tmp_path: Path) -> None:
    """Three lists with no items, each told apart: one whose read failed (unreadable, with its
    reason), one read and empty (the EHR has none), and one whose resources hold nothing the
    mapping reads (resources counted, no item). On a copy of the chart fixture with two lists
    more."""
    lists = {
        "labs": {"resource": "Observation", "fields": {"code": {"path": "code.coding[0].code"}}},
        "documents": {
            "resource": "DocumentReference",
            "fields": {"type": {"path": "type.coding[0].code"}},
        },
    }

    def allow(schema: dict[str, Any]) -> None:
        schema["properties"].update(labs={"type": "array"}, documents={"type": "array"})

    agent = load_pack(chart_fixture.chart_copy(tmp_path, {"lists": lists}, schema=allow))
    bare = {"resourceType": "DocumentReference", "id": "d9", "status": "current", "content": []}
    record = bundle(PATIENT, bare)
    result, _ = Decision(agent).decide_fhir(record, unreadable={"conditions": "timeout"})
    assert result.data_coverage is not None
    found = result.data_coverage["lists"]
    shown = {
        name: (found[name]["resources"], found[name]["items"], found[name]["unreadable"])
        for name in ("conditions", "labs", "documents")
    }
    assert shown == {
        "conditions": (0, 0, {"timeout": 1}),  # could not be read
        "labs": (0, 0, {}),  # read, and there are none
        "documents": (1, 0, {}),  # read, and nothing in it the mapping reads
    }


def test_the_record_is_the_same_bytes_every_run() -> None:
    """Canonical: sorted keys and lists, so the same record gives the same bytes on every run
    and in whatever order its resources came."""
    record = fixture("chart")
    order = record["entry"]
    first = decided(record, unreadable={"notes": "timeout"}).data_coverage
    shuffled = copy.deepcopy(record)
    random.Random(7).shuffle(shuffled["entry"])
    assert shuffled["entry"] != order
    again = decided(shuffled, unreadable={"notes": "timeout"}).data_coverage
    assert json.dumps(first) == json.dumps(again)
    assert json.dumps(first) == json.dumps(first, sort_keys=True)


def test_every_fhir_decision_carries_its_data_coverage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CDS Hooks (the chart fixture's patient-view), `fhir/decide`, `$decide`, the SMART
    launch's decide, and `mendari decide --fhir`: each decision's answer and its stored record
    carry the record."""
    app = TestClient(create_app_for(tmp_path))
    ids = []
    response = app.post(chart_fixture.SERVICE, json=chart_fixture.hook(ROUTINE)).json()
    ids.append(response["cards"][0]["extension"]["mendari.decision_id"])
    answered = app.post(f"/api/agents/{CHART}/fhir/decide", json=fixture("chart")).json()
    assert answered["decision"]["data_coverage"]["format"] == FORMAT
    ids.append(answered["decision"]["id"])
    operation = app.post(
        "/fhir/$decide",
        json={
            "resourceType": "Parameters",
            "parameter": [
                {"name": "pack", "valueString": CHART},
                {"name": "data", "resource": fixture("chart")},
            ],
        },
    )
    assert operation.status_code == 200, operation.text
    guidance = operation.json()["entry"][0]["resource"]
    ids.append(guidance["identifier"][0]["value"])
    for decision_id in ids:
        record = app.get(f"/api/audit/{decision_id}").json()["record"]
        assert record["data_coverage"]["format"] == FORMAT, decision_id
    # The SMART launch's decide, against a vendor-shaped EHR of the chart.
    ehr = ChartEhr.of(ROUTINE, statements="offered")
    smart, _ = decide_for(
        Decision.from_hub(CHART), ehr.client(), ROUTINE.id, f"Patient/{ROUTINE.id}"
    )
    assert smart.data_coverage is not None and smart.data_coverage["format"] == FORMAT
    case = tmp_path / "chart.json"
    case.write_text(json.dumps(fixture("chart")), encoding="utf-8")
    capsys.readouterr()
    assert main(["decide", CHART, "--fhir", "--input", str(case)]) == 0
    assert json.loads(capsys.readouterr().out)["decision"]["data_coverage"]["format"] == FORMAT


def create_app_for(tmp_path: Path) -> Any:
    from mendari.server.app import create_app

    return create_app(db_path=tmp_path / "a.db")


def test_a_json_decision_carries_none(tmp_path: Path) -> None:
    app = TestClient(create_app_for(tmp_path))
    body = {"message": {"body": "Can you send a refill for metformin to my pharmacy?"}}
    decision = app.post("/api/agents/mendari-test/triage/decide", json=body).json()
    assert "data_coverage" in decision and decision["data_coverage"] is None
    record = app.get(f"/api/audit/{decision['id']}").json()["record"]
    assert record["data_coverage"] is None


def refusing(request: httpx.Request) -> httpx.Response:
    """An EHR that refuses every read, its answer naming the patient."""
    body = {"error": f"no access to {MRN}"}
    return httpx.Response(403, json=body, headers={"content-type": "application/json"})


def test_no_note_text_patient_code_or_id_reaches_the_record_or_the_chain(tmp_path: Path) -> None:
    """Marks planted in a synthetic record (a patient id shaped like a record number, a note's
    text, the patient's own codes and a site's system) are in no data-coverage record, under
    PHI mode `encrypt`: not the caller's, not the stored record, not the chain. A link-only note's
    `url` is not either."""
    store = AuditStore(
        tmp_path / "a.db",
        phi=PhiPolicy(mode="encrypt", key=os.urandom(32), hash_key=INPUT_HASH_KEY),
    )
    case = Case(
        id="cov-2",
        age=56,
        sex="female",
        conditions=(Condition("E11.9", "2019-04-01"),),
        medications=(Medication("6809", "2021-02-01"),),
        notes=(Note((f"Seen for diabetes follow-up. {NOTE_MARK}.",)),),
    )
    resources = case.resources(records.TODAY)
    for resource in resources:
        if resource["resourceType"] == "Patient":
            resource["id"] = MRN
        elif "subject" in resource:
            resource["subject"] = {"reference": f"Patient/{MRN}"}
        if resource["resourceType"] == "Condition":
            resource["code"]["coding"] = [
                {"system": ICD, "code": PATIENT_ICD},
                {"system": SITE_SYSTEM, "code": SITE_CODE},
            ]
    linked = copy.deepcopy(next(r for r in resources if r["resourceType"] == "DocumentReference"))
    linked["id"] = "linked-note"
    linked["content"] = [{"attachment": {"contentType": "text/plain", "url": f"Binary/{MRN}-note"}}]
    # An RTF note whose content type names a file (its parameters are never kept: the format is
    # counted by a closed label, 2026-11-08).
    rtf = copy.deepcopy(linked)
    rtf["id"] = "rtf-note"
    rtf["content"] = [
        {
            "attachment": {
                "contentType": f"text/rtf; name={FILE_MARK}.rtf",
                "data": b64("{\\rtf1 x}"),
            }
        }
    ]
    decision = Decision.from_hub(CHART, store=store)
    # A prefetch key the EHR refused (403) and a linked read it refused, its answer naming the
    # patient: only the key, the reason and the status, a number, are kept (2026-11-08).
    ehr = FhirClient(
        records.BASE,
        allowed={"https://ehr.example.org"},
        transport=httpx.MockTransport(refusing),
    )
    refused, _ = decision.decide_fhir(
        bundle(*resources, linked),
        unreadable={"medications": "unauthorized"},
        statuses={"medications": 403},
        links=Linker.of(ehr),
    )
    assert refused.data_coverage is not None
    assert refused.data_coverage["prefetch"]["medications"]["status"] == 403
    assert refused.data_coverage["links"]["statuses"] == {"403": 1}
    results = [
        decision.decide_fhir(bundle(*resources))[0],
        decision.decide_fhir(bundle(*resources, linked))[0],
        decision.decide_fhir(bundle(*resources, rtf))[0],
        refused,
    ]
    assert results[1].reasons == ("could not read notes: not_fetched",)
    assert results[2].reasons == ("could not read notes: unsupported_type",)
    assert results[2].data_coverage is not None
    assert results[2].data_coverage["fields"]["notes"]["unread_formats"] == {"rtf": 1}
    marks = (
        MRN,
        NOTE_MARK,
        PATIENT_ICD,
        SITE_CODE,
        SITE_SYSTEM,
        "114350",
        "linked-note",
        FILE_MARK,
    )
    for result in results:
        assert result.data_coverage is not None
        shown = json.dumps(result.data_coverage) + json.dumps(result.reasons)
        assert not [mark for mark in marks if mark in shown]
        item = store.get(result.id)
        assert item is not None
        assert item["record"]["data_coverage"] == result.data_coverage
    with store._db.session(exclusive=False) as rows:
        kept = json.dumps(
            [rows.all(f"SELECT * FROM {table}") for table in ("decisions", "audit_events")],
            default=str,
        )
    assert not [mark for mark in marks if mark in kept]
    assert store.verify()["ok"] is True
    store.close()


def test_the_unreadable_notes_are_counted_by_reason(tmp_path: Path) -> None:
    """`unreadable_notes` counts each attachment of a `base64_text` note (spec 0.2's transform,
    on its data) once, by reason, and leaves out a note that reads, an empty one and an
    attachment with neither data nor a link. (The chart fixture's own notes are
    `attachment_text`, whose reasons `test_an_attachment_text_note_...` below counts.)"""
    notes = {"path": "DocumentReference.content.attachment.data", "transform": "base64_text"}
    pack = load_pack(chart_fixture.chart_copy(tmp_path, {"fields": {"notes": notes}}))
    adapter = FhirAdapter.for_pack(pack)
    note = {
        "resourceType": "DocumentReference",
        "content": [
            {"attachment": {"data": "U2VlbiB0b2RheS4="}},  # "Seen today."
            {"attachment": {"data": ""}},  # empty, and it decodes
            {"attachment": {"data": "+vv8"}},  # base64, not UTF-8
            {"attachment": {"url": "Binary/1"}},
            {"attachment": {"url": "Binary/2", "contentType": "application/pdf"}},
            {"attachment": {"contentType": "text/plain"}},  # nothing at all
        ],
    }
    indexed = adapter.index({"patient": {"resourceType": "Patient", "id": "p"}, "notes": note})
    assert data_coverage.unreadable_notes(adapter.mapping, indexed) == {
        "notes": {"not_fetched": 2, "undecodable": 1}
    }


# A Patient whose name and address arrays are padded, as R4 JSON allows a primitive array to be
# so that it lines up with its `_x` sibling: each null is a slot whose extension has no value.
PADDING = {
    "extension": [{"url": "https://site.example/fhir/StructureDefinition/x", "valueCode": "a"}]
}


def padded(given: list[Any]) -> dict[str, Any]:
    return {
        **PATIENT,
        "name": [{"given": given, "_given": [None, PADDING]}],
        "address": [{"line": ["1 Main St", None], "_line": [None, PADDING]}],
    }


def test_a_primitive_array_s_null_padding_is_not_a_value(tmp_path: Path) -> None:
    """A null in a primitive array is R4 JSON's padding for its `_x` sibling, which the
    structure check passes: it is no value, and a `base64_text` null is no undecodable note, so a
    record counts what LP-23d counted and its decision does not ABSTAIN on it."""
    lists = {"addresses": {"resource": "Patient", "fields": {"line": {"path": "address[0].line"}}}}
    fields = {"name": {"path": "Patient.name[0].given", "transform": "join"}}
    changes = {"fields": fields, "lists": lists}
    pack = load_pack(chart_fixture.chart_copy(tmp_path, changes, folder="join"))
    adapter = FhirAdapter.for_pack(pack)
    record = bundle(padded(["Ann", None]))
    assert fhir_structure.blocking(fhir_structure.check(record)) == []  # R4 allows the nulls
    coverage = data_coverage.measure(adapter.mapping, adapter.index(record), unreadable={})
    assert coverage["fields"]["name"]["values"] == 1
    assert coverage["lists"]["addresses"]["fields"]["line"]["values"] == 1

    notes = {"notes": {"path": "Patient.name[0].given", "transform": "base64_text"}}
    copied = chart_fixture.chart_copy(tmp_path, {"fields": notes}, folder="notes")
    decision = Decision(load_pack(copied))
    adapter = FhirAdapter.for_pack(decision.pack)
    record = bundle(padded([b64("Ann"), None]))
    assert data_coverage.unreadable_notes(adapter.mapping, adapter.index(record)) == {}
    result, _ = decision.decide_fhir(record)
    assert result.outcome.value != "ABSTAIN" and result.unreadable == ()
    assert result.data_coverage is not None
    assert result.data_coverage["fields"]["notes"]["values"] == 1
    assert result.data_coverage["fields"]["notes"]["unreadable"] == {}


MARK = "ID7f3"  # in every id and text of the 0.3 record below: none may reach its coverage
OUT_OF_SET = "J06.9"  # a patient's ICD-10-CM code the test's code set does not list


def test_a_0_3_mapping_is_counted_against_what_each_field_looks_for(tmp_path: Path) -> None:
    """A spec 0.3 mapping (LP-23g) is counted against `FhirAdapter.looked_for`: a field's paths
    in the order it tries them, the systems it selects on and its code set's codes; what the
    paths it read found (in order, until one gave the field its value); a `follow`'s references
    and how many it reached, and (LP-23h) each link among them the record does not hold, as
    `not_fetched` with no linked reads; a list of several types by type (`sources`, a
    `medication_period`'s keys among a MedicationRequest's fields); and each `attachment_text`
    note that could not be read, by its reason, while one with nothing in it counts for nothing.
    Never a value, an id, text, a patient's code or a site's own system. On a copy of the chart
    fixture whose mapping holds these fields and lists, a payer's system declared beside its
    own."""
    changes = {
        "code_sets": {"t2d": {"system": ICD, "codes": ["E11.9", "E11.65"]}},
        "fields": {
            "notes": {
                "path": "DocumentReference.content.attachment",
                "transform": "attachment_text",
            },
            "payer": {
                "follow": "Coverage.payor",
                "path": f"Organization.identifier[system={PAYER}].value",
            },
        },
        "lists": {
            "conditions": {
                "resource": "Condition",
                "fields": {"code": {"path": "code.coding", "code_set": "t2d"}},
            },
            "medications": {
                "resources": {
                    "MedicationStatement": {
                        "fields": {
                            "code": {"path": f"medicationCodeableConcept.coding[system={MED}].code"}
                        }
                    },
                    "MedicationRequest": {
                        "fields": {
                            "code": {
                                "follow": "medicationReference",
                                "paths": [
                                    f"Medication.code.coding[system={MED}].code",
                                    "Medication.code.text",
                                ],
                            }
                        },
                        "medication_period": {"start": "start", "end": "end"},
                    },
                }
            },
        },
    }
    mapped = FhirAdapter.for_pack(
        load_pack(chart_fixture.chart_copy(tmp_path, changes, systems=(PAYER,)))
    )

    def coded(*codings: tuple[str, str]) -> dict[str, Any]:
        return {"coding": [{"system": system, "code": code} for system, code in codings]}

    def note(number: int, **attachment: Any) -> dict[str, Any]:
        return {
            "resourceType": "DocumentReference",
            "id": f"{MARK}-n{number}",
            "status": "current",
            "content": [{"attachment": attachment}],
        }

    def ordered(number: int, reference: str, **more: Any) -> dict[str, Any]:
        return {
            "resourceType": "MedicationRequest",
            "id": f"{MARK}-r{number}",
            "status": more.pop("status", "active"),
            "intent": "order",
            "medicationReference": {"reference": reference},
            **more,
        }

    record = bundle(
        PATIENT,
        {
            "resourceType": "Condition",
            "id": f"{MARK}-c1",
            "code": coded((SITE_SYSTEM, SITE_CODE), (ICD, "E11.9")),
        },
        {"resourceType": "Condition", "id": f"{MARK}-c2", "code": coded((ICD, OUT_OF_SET))},
        {
            "resourceType": "MedicationStatement",
            "id": f"{MARK}-s1",
            "status": "active",
            "medicationCodeableConcept": coded((SITE_SYSTEM, SITE_CODE), (MED, f"{MARK}-M1")),
        },
        ordered(1, f"Medication/{MARK}-m1", authoredOn="2026-09-01"),
        ordered(2, "Medication/none"),  # reaches nothing in the record
        ordered(3, f"Medication/{MARK}-m1", status="entered-in-error"),  # no item
        {
            "resourceType": "Medication",
            "id": f"{MARK}-m1",
            "code": {"coding": [{"system": SITE_SYSTEM, "code": SITE_CODE}], "text": MARK},
        },
        {
            "resourceType": "Coverage",
            "id": f"{MARK}-cov",
            "status": "active",
            "payor": [{"reference": f"#{MARK}-o1"}],
            "contained": [
                {
                    "resourceType": "Organization",
                    "id": f"{MARK}-o1",
                    "identifier": [
                        {"system": PAYER, "value": f"{MARK}-payer"},
                        {"system": SITE_SYSTEM, "value": SITE_CODE},
                    ],
                }
            ],
        },
        note(1, contentType="text/plain", data=b64(f"{NOTE_MARK} {MARK}")),
        note(2, contentType="application/rtf", data=b64("{\\rtf1 note}")),
        note(3, contentType="text/plain", data="not base64!"),
        note(4, contentType="text/html", url=f"Binary/{MARK}-missing"),
        note(5, contentType="text/html", url=f"Binary/{MARK}-b1"),
        note(6, contentType="application/pdf"),  # nothing in it: not counted
        {
            "resourceType": "Binary",
            "id": f"{MARK}-b1",
            "contentType": "text/html",
            "data": b64(f"<p>{NOTE_MARK}</p>"),
        },
    )
    indexed = mapped.index(record)
    assert mapped.to_input(indexed)["medications"] == [
        {"code": f"{MARK}-M1"},
        {"code": MARK, "start": "2026-09-01"},
    ]
    assert data_coverage.unreadable_notes(mapped.mapping, indexed) == {
        "notes": {"not_fetched": 1, "undecodable": 1, "unsupported_type": 1}
    }
    # `Medication/none` is a link the record does not hold (LP-23h); the payer is contained.
    assert data_coverage.unreadable_links(mapped.mapping, indexed) == {
        "medications.code": {"not_fetched": 1}
    }
    systems = [use.url for use in mapped.pack.manifest.code_systems]
    sources = {"meds": "Medication?patient={{context.patientId}}"}
    coverage = data_coverage.measure(
        mapped.mapping, indexed, unreadable={"meds": "timeout"}, systems=systems, sources=sources
    )
    fields, lists = coverage["fields"], coverage["lists"]
    assert fields["payer"] == {
        "codes": [],
        "codings": {},
        "follow": {"path": "Coverage.payor", "reached": 1, "references": 1},
        "paths": [f"Organization.identifier[system={PAYER}].value"],
        "resources": {"Coverage": 1, "Organization": 0},  # the payer is contained
        "systems": [PAYER],
        "unreadable": {},
        "values": 1,
    }
    assert fields["notes"]["resources"] == {"DocumentReference": 6}
    assert fields["notes"]["values"] == 6
    assert fields["notes"]["unreadable"] == {
        "not_fetched": 1,
        "undecodable": 1,
        "unsupported_type": 1,
    }
    assert lists["conditions"] == {
        "fields": {
            "code": {
                "codes": ["E11.65", "E11.9"],  # the code set's
                "codings": {ICD: 2, OTHER: 1},
                "paths": ["code.coding"],
                "systems": [ICD],  # the code set's system, not every declared one
                "values": 3,
            }
        },
        "items": 1,  # the second Condition's code is not in the set
        "resource": "Condition",
        "resources": 2,
        "unreadable": {},
    }
    medications = lists["medications"]
    assert sorted(medications) == ["items", "sources", "unreadable"]
    assert medications["items"] == 2
    assert medications["unreadable"] == {"not_fetched": 1, "timeout": 1}
    statements = medications["sources"]["MedicationStatement"]
    assert (statements["resources"], statements["items"], statements["unreadable"]) == (1, 1, {})
    assert statements["fields"]["code"]["codings"] == {MED: 1, OTHER: 1}
    requests = medications["sources"]["MedicationRequest"]
    assert (requests["resource"], requests["resources"], requests["items"]) == (
        "MedicationRequest",
        3,
        1,  # one reaches nothing, one is entered in error
    )
    # Its follow reads Medication, whose read failed, and one reference reaches nothing.
    assert requests["unreadable"] == {"not_fetched": 1, "timeout": 1}
    assert requests["fields"]["code"] == {
        "codes": [],
        "codings": {OTHER: 2},  # the Medication's own coding, read by two requests
        "follow": {"path": "medicationReference", "reached": 2, "references": 3},
        "paths": [f"Medication.code.coding[system={MED}].code", "Medication.code.text"],
        "systems": [MED],
        "values": 2,
        # The reference that reached nothing, by the field that follows it (the failed
        # Medication read is the source's, by its type, above).
        "unreadable": {"not_fetched": 1},
    }
    bounds = "dosageInstruction[0].timing.repeat.boundsPeriod"
    assert requests["fields"]["start"]["paths"] == [f"{bounds}.start", "authoredOn"]
    assert (requests["fields"]["start"]["values"], requests["fields"]["end"]["values"]) == (1, 0)
    assert coverage["unreadable"] == {
        "not_fetched": 2,
        "timeout": 1,
        "undecodable": 1,
        "unsupported_type": 1,
    }
    shown = json.dumps(coverage)
    assert shown == json.dumps(coverage, sort_keys=True)
    for leak in (MARK, NOTE_MARK, SITE_SYSTEM, "114350", SITE_CODE, OUT_OF_SET, "none"):
        assert leak not in shown, leak
    again = data_coverage.measure(
        mapped.mapping, indexed, unreadable={"meds": "timeout"}, systems=systems, sources=sources
    )
    assert json.dumps(again) == shown


def test_an_attachment_text_note_that_cannot_be_read_abstains_with_its_reason() -> None:
    """As a `base64_text` note that cannot be read does (LP-23d), an `attachment_text` one
    (spec 0.3, the chart fixture's) makes the decision ABSTAIN before any provider, with its
    reason; a note that reads, or an attachment with nothing in it, does not."""
    decision = Decision(load_pack(CHART_DIR))

    def decided_on(**attachment: Any) -> DecisionResult:
        note = {
            "resourceType": "DocumentReference",
            "id": "n1",
            "status": "current",
            "content": [{"attachment": attachment}],
        }
        result, _ = decision.decide_fhir(bundle(PATIENT, note))
        return result

    for attachment, reason in (
        (
            {"contentType": "application/rtf", "data": b64("{\\rtf1 Pancreatitis.}")},
            "unsupported_type",
        ),
        ({"contentType": "text/plain", "data": "not base64!"}, "undecodable"),
        ({"contentType": "text/html", "url": "Binary/elsewhere"}, "not_fetched"),
    ):
        result = decided_on(**attachment)
        assert result.outcome.value == "ABSTAIN", attachment
        assert result.reasons == (f"could not read notes: {reason}",)
        assert result.answers == {}  # no provider ran
        assert result.data_coverage is not None
        assert result.data_coverage["fields"]["notes"]["unreadable"] == {reason: 1}
    for attachment in (
        {"contentType": "text/html", "data": b64("<p>Seen for diabetes follow-up.</p>")},
        {"contentType": "application/pdf"},  # nothing in it to read
    ):
        result = decided_on(**attachment)
        assert result.unreadable == (), attachment
        assert result.answers, attachment  # the providers ran
