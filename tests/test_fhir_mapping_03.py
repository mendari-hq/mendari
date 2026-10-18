# SPDX-License-Identifier: Apache-2.0
"""FHIR mapping syntax 0.3 (LP-23g): a coding chosen by its code system, fallback paths, lists
of several resource types, a MedicationRequest's start and stop by a stated rule, one reference
hop inside the record, HTML notes read as text, code sets, the bounds on a pack the deployment
does not own, and what each field looks for (`FhirAdapter.looked_for`, for LP-23d's
data-coverage record). Every case is synthetic; the site systems are fictional."""

from __future__ import annotations

import base64
import copy
import json
import socket
from pathlib import Path
from typing import Any

import pytest
import yaml

from mendari import Decision, load_pack
from mendari.core.decision import input_hash
from mendari.core.origin import Origin
from mendari.errors import PackValidationError
from mendari.evals.harness import load_dataset
from mendari.evals.pack_tests import validate_pack
from mendari.standards import fhir
from mendari.standards.fhir import (
    MAX_CODE_SET_CODES,
    MAX_PATHS,
    NOT_FETCHED,
    UNDECODABLE,
    UNSUPPORTED_TYPE,
    FhirAdapter,
    FhirMapping,
    html_text,
    mapping_problems,
    medication_period,
    read_attachment,
)
from tests.conftest import ROOT
from tests.test_fhir_decisions import script
from tests.test_spec_03 import ICD, PATIENT, SITE, mini

MED = "https://site.example/fhir/CodeSystem/medications"  # fictional
PAYER = "https://site.example/fhir/NamingSystem/payers"  # fictional


def adapter(tmp_path: Path, changes: dict[str, Any], *, origin: Origin = "local") -> FhirAdapter:
    """The adapter of the prescreen fixture at spec 0.3, its mapping changed by `changes`."""
    root = mini(tmp_path, changes, spec="0.3", systems=(MED, PAYER))
    return FhirAdapter.for_pack(load_pack(root, origin=origin))


def bundle(*resources: dict[str, Any], urls: dict[str, str] | None = None) -> dict[str, Any]:
    """A collection Bundle; `urls` gives an entry, by its resource id, a fullUrl."""
    entries = []
    for resource in resources:
        entry: dict[str, Any] = {"resource": resource}
        if urls and resource.get("id") in urls:
            entry["fullUrl"] = urls[resource["id"]]
        entries.append(entry)
    return {"resourceType": "Bundle", "type": "collection", "entry": entries}


def read(mapped: FhirAdapter, *resources: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    urls = kwargs.pop("urls", None)
    return mapped.to_input(mapped.index(bundle(PATIENT, *resources, urls=urls)), **kwargs)


def condition(id_: str, *codings: tuple[str, str]) -> dict[str, Any]:
    coding = [{"system": system, "code": code} for system, code in codings]
    return {"resourceType": "Condition", "id": id_, "code": {"coding": coding}}


def conditions(**fields: Any) -> dict[str, Any]:
    return {"lists": {"conditions": {"resource": "Condition", "fields": fields}}}


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def test_a_coding_is_chosen_by_its_system_whatever_its_position(tmp_path: Path) -> None:
    """`code.coding[system=<ICD-10-CM>].code` finds the ICD-10-CM coding first or second, and
    nothing when there is none; `code.coding[0].code` reads whichever is first. Through a
    decision: the prescreen reads type 2 diabetes behind a site's own coding, where the 0.2
    mapping (`[0]`) reads it as `unknown`, as `tests/test_trial_pack.py` pins for the trial."""
    by_system = {"code": {"path": f"code.coding[system={ICD}].code"}}
    mapped = adapter(tmp_path / "a", conditions(**by_system, first={"path": "code.coding[0].code"}))
    record = [
        condition("c1", (SITE, "T2D"), (ICD, "E11.9")),
        condition("c2", (ICD, "I21.4"), (SITE, "MI")),
        condition("c3", (SITE, "HTN")),
    ]
    assert read(mapped, *record)["conditions"] == [
        {"code": "E11.9", "first": "T2D"},
        {"code": "I21.4", "first": "I21.4"},
        {"first": "HTN"},
    ]

    record = [PATIENT, condition("c1", (SITE, "T2D"), (ICD, "E11.9"))]
    adopted = Decision(load_pack(mini(tmp_path / "b", conditions(**by_system), spec="0.3")))
    as_today = Decision(load_pack(mini(tmp_path / "c", {})))
    labels = [
        decision.decide_fhir(bundle(*record))[0].answers["t2d_diagnosis"].label
        for decision in (adopted, as_today)
    ]
    assert labels == ["yes", "unknown"]


def test_a_fallback_path_is_read_only_when_the_first_finds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`paths` are tried in order and the first that gives a value wins; a later path is not
    read at all once one has. A value its `map` does not know is nothing found."""
    sex = {"paths": ["Patient.gender", "Patient.extension.valueCode"], "map": {"male": "m"}}
    code = {"paths": [f"code.coding[system={ICD}].code", f"code.coding[system={SITE}].code"]}
    mapped = adapter(tmp_path, {**conditions(code=code), "fields": {"patient.sex": sex}})
    walked: list[str] = []
    walk = fhir._walk

    def spy(indexed: Any, path: str) -> Any:
        walked.append(path)
        return walk(indexed, path)

    monkeypatch.setattr(fhir, "_walk", spy)
    found = read(mapped, condition("c1", (SITE, "T2D"), (ICD, "E11.9")))
    assert found["conditions"] == [{"code": "E11.9"}]
    assert walked.count(f"Condition.code.coding[system={SITE}].code") == 0
    assert walked.count("Patient.extension.valueCode") == 1  # a Patient without gender

    walked.clear()
    found = read(mapped, condition("c2", (SITE, "T2D")), condition("c3"))
    assert found["conditions"] == [{"code": "T2D"}]
    assert walked.count(f"Condition.code.coding[system={SITE}].code") == 2

    for patient, want in (
        ({"gender": "male"}, "m"),
        ({"gender": "unknown", "extension": [{"url": "x", "valueCode": "male"}]}, "m"),
        ({"extension": [{"url": "x", "valueCode": "male"}]}, "m"),
        ({"gender": "female"}, None),
    ):
        record = mapped.index(bundle({**PATIENT, **patient}))
        assert mapped.to_input(record).get("patient", {}).get("sex") == want, patient


def test_one_list_reads_two_resource_types_by_their_own_fields(tmp_path: Path) -> None:
    """`resources` puts MedicationStatements and MedicationRequests in one list, each read by
    its own fields, in the order the mapping gives the types, then the record's. A list in the
    0.2 form beside it reads as before."""
    code = {"path": f"medicationCodeableConcept.coding[system={MED}].code"}
    medications = {
        "resources": {
            "MedicationStatement": {
                "fields": {
                    "code": code,
                    "status": {"path": "status"},
                    "start": {"path": "effectivePeriod.start"},
                }
            },
            "MedicationRequest": {
                "fields": {
                    "code": code,
                    "status": {"path": "status"},
                    "start": {"path": "authoredOn"},
                }
            },
        }
    }
    mapped = adapter(tmp_path, {"lists": {"medications": medications}})

    def drug(kind: str, id_: str, med: str, status: str, **dates: Any) -> dict[str, Any]:
        concept = {"coding": [{"system": SITE, "code": "x"}, {"system": MED, "code": med}]}
        return {
            "resourceType": kind,
            "id": id_,
            "status": status,
            "medicationCodeableConcept": concept,
            **dates,
        }

    found = read(
        mapped,
        drug("MedicationRequest", "r1", "M1", "active", authoredOn="2026-09-01"),
        drug(
            "MedicationStatement", "s1", "M2", "completed", effectivePeriod={"start": "2026-01-02"}
        ),
        drug("MedicationRequest", "r2", "M3", "stopped", authoredOn="2026-05-03"),
        condition("c1", (ICD, "E11.9")),
    )
    assert found["medications"] == [
        {"code": "M2", "status": "completed", "start": "2026-01-02"},
        {"code": "M1", "status": "active", "start": "2026-09-01"},
        {"code": "M3", "status": "stopped", "start": "2026-05-03"},
    ]
    assert found["conditions"] == [{"code": "E11.9"}]  # the fixture's 0.2-form list


BOUNDS = "2026-03-01", "2026-06-30"
WRITTEN = "2026-02-20"  # authoredOn
# A MedicationRequest's derived (start, end) under the rule in the spec (LP-23 conflict 39):
# None is a key left out (unknown); a request with no item at all is left out of the list.
PERIODS: list[tuple[str, dict[str, Any], tuple[str | None, str | None] | None]] = [
    ("bounds give both", {"bounds": BOUNDS, "authoredOn": WRITTEN}, BOUNDS),
    ("no bounds: written, stop unknown", {"authoredOn": WRITTEN}, (WRITTEN, None)),
    ("a start only", {"bounds": (BOUNDS[0], None), "authoredOn": WRITTEN}, (BOUNDS[0], None)),
    ("an end only", {"bounds": (None, BOUNDS[1]), "authoredOn": WRITTEN}, (WRITTEN, BOUNDS[1])),
    ("completed: no stop", {"status": "completed", "authoredOn": WRITTEN}, (WRITTEN, None)),
    ("stopped: no stop", {"status": "stopped", "authoredOn": WRITTEN}, (WRITTEN, None)),
    ("no dispensing window", {"authoredOn": WRITTEN, "validity": BOUNDS}, (WRITTEN, None)),
    ("first dosage only", {"second_bounds": BOUNDS, "authoredOn": WRITTEN}, (WRITTEN, None)),
    ("nothing dated", {}, (None, None)),
    ("entered in error is left out", {"status": "entered-in-error", "bounds": BOUNDS}, None),
]


def request(id_: str, case: dict[str, Any]) -> dict[str, Any]:
    resource: dict[str, Any] = {
        "resourceType": "MedicationRequest",
        "id": id_,
        "status": case.get("status", "active"),
        "intent": "order",
    }
    if "authoredOn" in case:
        resource["authoredOn"] = case["authoredOn"]
    dosages: list[dict[str, Any]] = [{"text": "as directed"}]
    if "bounds" in case:
        start, end = case["bounds"]
        period = {key: value for key, value in (("start", start), ("end", end)) if value}
        dosages = [{"timing": {"repeat": {"boundsPeriod": period}}}]
    if "second_bounds" in case:
        start, end = case["second_bounds"]
        dosages.append({"timing": {"repeat": {"boundsPeriod": {"start": start, "end": end}}}})
    resource["dosageInstruction"] = dosages
    if "validity" in case:
        start, end = case["validity"]
        resource["dispenseRequest"] = {"validityPeriod": {"start": start, "end": end}}
    return resource


def test_a_medication_requests_start_and_stop_follow_the_stated_derivation(tmp_path: Path) -> None:
    """`medication_period`: the start is the first dosage instruction's
    `timing.repeat.boundsPeriod.start`, else `authoredOn`; the stop is that `boundsPeriod.end`,
    else unknown, never inferred from the status; the dispensing window is not read; a request
    entered in error is left out. Each case of the table, alone and all in one record."""
    source = {"fields": {"id": {"path": "id"}}, "medication_period": {"start": "from", "end": "to"}}
    mapped = adapter(
        tmp_path, {"lists": {"medications": {"resources": {"MedicationRequest": source}}}}
    )
    wanted = []
    for number, (name, case, period) in enumerate(PERIODS):
        resource = request(f"r{number}", case)
        if period is None:
            assert read(mapped, resource)["medications"] == [], name
            assert medication_period(resource) == BOUNDS  # the rule; the list leaves it out
            continue
        item = {"id": f"r{number}"}
        item |= {key: value for key, value in zip(("from", "to"), period, strict=True) if value}
        assert read(mapped, resource)["medications"] == [item], name
        assert medication_period(resource) == period, name
        wanted.append(item)
    resources = [request(f"r{number}", case) for number, (_, case, _) in enumerate(PERIODS)]
    assert read(mapped, *resources)["medications"] == wanted


def test_a_reference_is_followed_inside_the_record_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`follow` resolves one reference inside the record: a contained resource (`#id`) of the
    resource holding the reference, a Bundle entry by its fullUrl, or `Type/id`. An absolute URL
    no entry has, another resource's contained one, a missing id or a resource of another type
    give nothing, and nothing is fetched. One hop: `follow` is one path."""

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the adapter opened a connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    code = {"follow": "medicationReference", "path": f"Medication.code.coding[system={MED}].code"}
    source = {"fields": {"id": {"path": "id"}, "code": code}}
    payer = {"follow": "Coverage.payor", "path": f"Organization.identifier[system={PAYER}].value"}
    changes = {
        "lists": {"medications": {"resources": {"MedicationRequest": source}}},
        "fields": {"payer": payer},
    }
    mapped = adapter(tmp_path, changes)

    def medication(id_: str, med: str) -> dict[str, Any]:
        return {
            "resourceType": "Medication",
            "id": id_,
            "code": {"coding": [{"system": MED, "code": med}]},
        }

    def ordered(id_: str, reference: str, **more: Any) -> dict[str, Any]:
        return {
            "resourceType": "MedicationRequest",
            "id": id_,
            "status": "active",
            "intent": "order",
            "medicationReference": {"reference": reference},
            **more,
        }

    uuid = "urn:uuid:7f4b5a2e-0d0a-4c1e-9e57-3b1c2f0a9d11"
    found = read(
        mapped,
        ordered("r1", "#med1", contained=[medication("med1", "M1")]),
        ordered("r2", "Medication/m2"),
        ordered("r3", uuid),
        ordered("r4", "https://elsewhere.example/fhir/Medication/m2"),
        ordered("r5", "#med1"),
        ordered("r6", "Medication/missing"),
        ordered("r7", "Patient/p1"),
        medication("m2", "M2"),
        medication("m3", "M3"),
        {
            "resourceType": "Coverage",
            "id": "cov",
            "status": "active",
            "payor": [{"reference": "Organization/o1"}],
        },
        {
            "resourceType": "Organization",
            "id": "o1",
            "identifier": [
                {"system": "https://other.example", "value": "x"},
                {"system": PAYER, "value": "PAYER-A"},
            ],
        },
        urls={"m3": uuid},
    )
    assert found["medications"] == [
        {"id": "r1", "code": "M1"},
        {"id": "r2", "code": "M2"},
        {"id": "r3", "code": "M3"},
        {"id": "r4"},
        {"id": "r5"},
        {"id": "r6"},
        {"id": "r7"},
    ]
    assert found["payer"] == "PAYER-A"

    hops = {"fields": {"payer": {**payer, "follow": ["Coverage.payor", "Organization.partOf"]}}}
    with pytest.raises(PackValidationError, match=r"follow\n.*Input should be a valid string"):
        adapter(tmp_path / "hops", hops)


def note(
    id_: str, content_type: str | None, text: str | None = None, **attachment: Any
) -> dict[str, Any]:
    if content_type is not None:
        attachment["contentType"] = content_type
    if text is not None:
        attachment["data"] = b64(text)
    return {
        "resourceType": "DocumentReference",
        "id": id_,
        "status": "current",
        "content": [{"attachment": attachment}],
    }


HTML = (
    "<html><head><style>p {color: red}</style><script>var note = 'pancreatitis';</script></head>"
    "<body><h1>Visit</h1><p>Seen for diabetes &amp; follow-up.</p><ul><li>No   new complaints</li>"
    "</ul><script type='text/javascript'>alert('pancreatitis')</script></body></html>"
)


def test_an_html_note_is_read_as_text_and_another_type_is_unreadable(tmp_path: Path) -> None:
    """`attachment_text`: text/plain as it is, text/html as text with its scripts and styles
    dropped, inline or from a Binary in the record. Any other content type (RTF, PDF, none) is
    `unsupported_type`, data that does not decode `undecodable`, a url to a Binary the record
    does not hold `not_fetched`; none of them adds text. Through a decision, a word in a script
    is not read and the same word in a paragraph is."""
    assert html_text(HTML) == "Visit\nSeen for diabetes & follow-up.\nNo new complaints"
    plain = {"contentType": "text/plain; charset=utf-8", "data": b64("  Plain note.\n")}
    assert read_attachment(plain) == ("Plain note.", None)
    assert read_attachment({"contentType": "TEXT/HTML", "data": b64(HTML)})[0] == html_text(HTML)
    for attachment, reason in (
        ({"contentType": "application/rtf", "data": b64("{\\rtf1 note}")}, UNSUPPORTED_TYPE),
        ({"contentType": "application/pdf", "url": "Binary/b1"}, UNSUPPORTED_TYPE),
        ({"data": b64("no content type")}, UNSUPPORTED_TYPE),
        ({"contentType": "text/plain", "data": "not base64!"}, UNDECODABLE),
        ({"contentType": "text/plain", "data": b64("x")[:-1] + "é"}, UNDECODABLE),
        (
            {"contentType": "text/plain", "data": base64.b64encode(b"\xff\xfe").decode()},
            UNDECODABLE,
        ),
        ({"contentType": "text/html", "url": "Binary/elsewhere"}, NOT_FETCHED),
        ({"contentType": "text/html", "url": "https://elsewhere.example/Binary/b1"}, NOT_FETCHED),
        ("a string, not an attachment", UNSUPPORTED_TYPE),
    ):
        assert read_attachment(attachment) == (None, reason), attachment
    assert read_attachment({"contentType": "text/plain"}) == (None, None)  # nothing there

    notes = {"path": "DocumentReference.content.attachment", "transform": "attachment_text"}
    mapped = adapter(tmp_path / "a", {"fields": {"notes": notes}})
    binary = {
        "resourceType": "Binary",
        "id": "b1",
        "contentType": "text/html",
        "data": b64("<p>From a Binary.</p>"),
    }
    found = read(
        mapped,
        note("n1", "text/html", HTML),
        note("n2", "application/rtf", "{\\rtf1 Pancreatitis.}"),
        note("n3", "text/plain", "Plain note."),
        note("n4", "text/html", url="Binary/b1"),
        note("n5", "text/html", url="Binary/missing"),
        binary,
    )
    assert found["notes"] == "\n\n".join([html_text(HTML), "Plain note.", "From a Binary."])
    rtf_only = read(mapped, note("n2", "application/rtf", "{\\rtf1 Pancreatitis.}"))
    assert "notes" not in rtf_only

    decision = Decision(load_pack(mini(tmp_path / "b", {"fields": {"notes": notes}}, spec="0.3")))
    labels = []
    for markup in (HTML, "<p>History of pancreatitis in 2019.</p>"):
        result, _ = decision.decide_fhir(bundle(PATIENT, note("n1", "text/html", markup)))
        labels.append(result.answers["note_pancreatitis"].label)
    assert labels == ["no", "yes"]


def test_a_code_set_selector_matches_only_its_declared_codes(tmp_path: Path) -> None:
    """`code_set` gives one of the set's own codes: a coding of the set's system whose code the
    set lists (in a CodeableConcept or a list of Codings), and nothing for another code of that
    system or the same code in another system. A site's translations (LP-23i; nothing passes
    them yet) count only into a code the set lists, only for their own set, and a coding of the
    set's own system wins over a translated one wherever it is. A code or a translation that is
    not text matches nothing."""
    sets = {
        "t2d": {"system": ICD, "codes": ["E11.9", "E11.65"]},
        "mi": {"system": ICD, "codes": ["I21.4"]},
    }
    fields = {
        "id": {"path": "id"},
        "code": {"path": "code.coding", "code_set": "t2d"},
        "whole": {"path": "code", "code_set": "t2d"},  # the CodeableConcept itself
        "heart": {"path": "code.coding", "code_set": "mi"},
    }
    mapped = adapter(tmp_path, {"code_sets": sets, **conditions(**fields)})
    record = [
        condition("c1", (SITE, "T2D"), (ICD, "E11.65")),
        condition("c2", (ICD, "E10.9")),
        condition("c3", (SITE, "E11.9")),
        condition("c4", (SITE, "T2D")),
        condition("c5", (SITE, "T2D-B"), (ICD, "E11.9")),
        condition("c6", (SITE, "MI")),
        condition("c7", (SITE, "T2D-X")),
        {"resourceType": "Condition", "id": "c8", "code": {"coding": [{"system": ICD, "code": 5}]}},
    ]
    t2d = {"code": "E11.65", "whole": "E11.65"}
    assert read(mapped, *record)["conditions"] == [
        {"id": "c1", **t2d},
        {"id": "c2"},
        {"id": "c3"},
        {"id": "c4"},
        {"id": "c5", "code": "E11.9", "whole": "E11.9"},
        {"id": "c6"},
        {"id": "c7"},
        {"id": "c8"},
    ]
    translations = {
        "t2d": [
            {"system": SITE, "code": "T2D", "as": "E11.9"},
            {"system": SITE, "code": "T2D-B", "as": "E11.65"},
            {"system": SITE, "code": "T2D-X", "as": "E10.9"},  # not a code of the set: ignored
            {"system": SITE, "code": ["T2D-X"], "as": ["E11.9"]},  # not text: ignored
        ],
        "mi": [{"system": SITE, "code": "MI", "as": "I21.4"}],
    }
    assert read(mapped, *record, site_codes=translations)["conditions"] == [
        {"id": "c1", **t2d},
        {"id": "c2"},
        {"id": "c3"},
        {"id": "c4", "code": "E11.9", "whole": "E11.9"},
        {"id": "c5", "code": "E11.9", "whole": "E11.9"},
        {"id": "c6", "heart": "I21.4"},
        {"id": "c7"},
        {"id": "c8"},
    ]


def test_an_outside_packs_new_keys_are_bounded_at_load(tmp_path: Path) -> None:
    """A pack the deployment does not own tries at most 4 paths a field and has at most 200
    codes a code set, refused at load past either; an owned pack loads past them and `mendari
    validate` refuses it. A `follow` is one hop for every pack (it takes one path)."""
    paths = [f"code.coding[{n}].code" for n in range(MAX_PATHS + 1)]
    codes = [f"FX{n:03d}" for n in range(MAX_CODE_SET_CODES + 1)]
    within = {
        "code_sets": {"big": {"system": ICD, "codes": codes[:-1]}},
        **conditions(code={"paths": paths[:-1]}, big={"path": "code.coding", "code_set": "big"}),
    }
    assert adapter(tmp_path / "within", within, origin="hub").mapping.code_sets["big"].codes
    beyond = {
        "code_sets": {"big": {"system": ICD, "codes": codes}},
        **conditions(code={"paths": paths}, big={"path": "code.coding", "code_set": "big"}),
    }
    root = mini(tmp_path / "beyond", beyond, spec="0.3")
    problems = [
        "fhir/mapping.yaml: lists.conditions.fields.code.paths tries 5 paths, more than 4",
        "fhir/mapping.yaml: code_sets.big has 201 codes, more than 200",
    ]
    with pytest.raises(PackValidationError) as refused:
        load_pack(root, origin="hub")
    assert refused.value.problems == problems
    owned = load_pack(root)
    assert len(FhirAdapter.for_pack(owned).mapping.code_sets["big"].codes) == 201
    with pytest.raises(PackValidationError) as refused:
        validate_pack(owned)
    assert refused.value.problems == problems


def test_a_0_3_mappings_paths_and_systems_are_checked_at_load(tmp_path: Path) -> None:
    """A 0.3 pack's paths are of the subset, each `[system=…]` and code set names a system its
    `code_systems` declares, a list's types are resource types and `medication_period` is a
    MedicationRequest's: for every pack, wherever it comes from."""
    other = "https://undeclared.example/codes"
    changes = {
        "code_sets": {"loose": {"system": other, "codes": ["A1"]}},
        "focus_where": {"path": "identifier[system=" + other + "].value", "in": ["x"]},
        "fields": {
            "patient.age": {"path": "birthDate", "transform": "age_years"},
            "notes": {"follow": "subject", "path": "Patient.name[first].text"},
        },
        "lists": {
            "conditions": {"resources": {"condition": {"fields": {"code": {"path": "code..x"}}}}},
            "observations": {
                "resources": {"Observation": {"medication_period": {"start": "a", "end": "b"}}}
            },
        },
    }
    root = mini(tmp_path, changes, spec="0.3")
    subset = "dotted element names, each with at most one [n], [*] or [system=<uri>])"
    origins: tuple[Origin, ...] = ("local", "hub")
    for origin in origins:
        with pytest.raises(PackValidationError) as refused:
            FhirAdapter.for_pack(load_pack(root, origin=origin))
        assert [p.removeprefix("fhir/mapping.yaml: ") for p in refused.value.problems] == [
            f"focus_where.path 'identifier[system={other}].value' selects '{other}', which "
            "code_systems lacks",
            f"fields.patient.age.path 'birthDate' is not a path of the mapping's subset (a "
            f"resource type, then {subset}",
            "fields.notes.follow 'subject' is not a path of the mapping's subset (a resource "
            f"type, then {subset}",
            "fields.notes.path 'Patient.name[first].text' is not a path of the mapping's subset "
            f"(a resource type, then {subset}",
            f"lists.conditions.resources.condition.fields.code.path 'code..x' is not a path of "
            f"the mapping's subset ({subset}",
            f"code_sets.loose is of '{other}', which code_systems lacks",
            "lists.conditions.resources.condition: 'condition' is not a resource type",
            "lists.observations.resources.Observation.medication_period is a MedicationRequest's "
            "only",
        ]


def test_looked_for_says_what_each_field_and_list_looks_for(tmp_path: Path) -> None:
    """`looked_for` is what LP-23d's data-coverage record counts against: per field and list
    item key, the full paths in the order tried, the Reference followed, the declared systems
    it selects on and the pack's own codes it looks for, from the mapping alone. The same on
    every call, JSON, and for a 0.2 pack too (the inbox: its map's keys)."""
    changes = {
        "code_sets": {"t2d": {"system": ICD, "codes": ["E11.9", "E11.65"]}},
        "fields": {
            "patient.age": {"paths": ["Patient.birthDate"], "transform": "age_years"},
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
    mapped = adapter(tmp_path, changes)

    def looked(
        paths: list[str],
        *,
        follow: str | None = None,
        systems: tuple[str, ...] = (),
        codes: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        return {"follow": follow, "paths": paths, "systems": list(systems), "codes": list(codes)}

    bounds = "MedicationRequest.dosageInstruction[0].timing.repeat.boundsPeriod"
    assert mapped.looked_for() == {
        "fields": {
            "patient.age": looked(["Patient.birthDate"]),
            "notes": looked(["DocumentReference.content.attachment"]),
            "payer": looked(
                [f"Organization.identifier[system={PAYER}].value"],
                follow="Coverage.payor",
                systems=(PAYER,),
            ),
        },
        "lists": {
            "conditions": {
                "Condition": {
                    "code": looked(
                        ["Condition.code.coding"], systems=(ICD,), codes=("E11.65", "E11.9")
                    )
                },
            },
            "observations": {
                "Observation": {
                    "code": looked(["Observation.code.coding[0].code"]),
                    "value": looked(["Observation.valueQuantity.value"]),
                    "unit": looked(["Observation.valueQuantity.unit"]),
                    "date": looked(["Observation.effectiveDateTime"]),
                }
            },
            "medications": {
                "MedicationStatement": {
                    "code": looked(
                        [
                            f"MedicationStatement.medicationCodeableConcept.coding[system={MED}].code"
                        ],
                        systems=(MED,),
                    )
                },
                "MedicationRequest": {
                    "code": looked(
                        [f"Medication.code.coding[system={MED}].code", "Medication.code.text"],
                        follow="MedicationRequest.medicationReference",
                        systems=(MED,),
                    ),
                    "start": looked([f"{bounds}.start", "MedicationRequest.authoredOn"]),
                    "end": looked([f"{bounds}.end"]),
                },
            },
        },
    }
    assert json.dumps(mapped.looked_for()) == json.dumps(mapped.looked_for())

    inbox = FhirAdapter.for_pack(load_pack(ROOT / "packs" / "patient-inbox-router")).looked_for()
    assert inbox["lists"] == {}
    assert inbox["fields"]["message.channel"] == looked(
        ["Communication.medium[0].coding[0].code"], codes=("email", "portal", "sms")
    )
    assert inbox["fields"]["message.body"] == looked(["Communication.payload.contentString"])


def in_0_3_keys(raw: dict[str, Any]) -> dict[str, Any]:
    """A 0.2 mapping written with 0.3's keys: every `path` as `paths: [path]`, every list as
    `resources: {<type>: {fields}}`."""
    mapping = copy.deepcopy(raw)
    for mapped in mapping.get("fields", {}).values():
        mapped["paths"] = [mapped.pop("path")]
    for spec in mapping.get("lists", {}).values():
        for mapped in spec["fields"].values():
            mapped["paths"] = [mapped.pop("path")]
        spec["resources"] = {spec.pop("resource"): {"fields": spec.pop("fields")}}
    return mapping


def test_a_0_2_mapping_written_with_0_3_keys_reads_every_golden_case_alike() -> None:
    """Each reference pack's mapping, rewritten with `paths` and `resources` (a valid 0.3
    mapping, bounds included), reads every case of the 23a golden into the input the 0.2
    mapping reads, byte for byte: the input hash the golden pins."""
    pinned = json.loads(script.GOLDEN.read_text(encoding="utf-8"))["packs"]
    with script.on_the_day():
        for pack_id, (directory, dataset, build) in script.CASES.items():
            pack = load_pack(ROOT / "packs" / directory)
            raw = yaml.safe_load((pack.root / "fhir" / "mapping.yaml").read_text())
            mapping = FhirMapping.model_validate(in_0_3_keys(raw))
            manifest = pack.manifest.model_copy(update={"spec_version": "0.3"})
            assert mapping_problems(mapping, manifest, held=True) == [], pack_id
            rewritten = FhirAdapter(pack, mapping)
            want = {case["id"]: case.get("input_hash") for case in pinned[pack_id]["cases"]}
            read = 0
            for record in load_dataset(ROOT / "datasets" / dataset).records:
                if want[record["id"]] is None:
                    continue  # refused through FHIR (an order with no requirement set)
                entries = {"resourceType": "Bundle", "type": "collection", "entry": build(record)}
                got = rewritten.to_input(rewritten.index(entries))
                assert input_hash(got) == want[record["id"]], (pack_id, record["id"])
                read += 1
            assert read >= 100, pack_id
