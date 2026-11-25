# SPDX-License-Identifier: Apache-2.0
"""Record reading in an agent's FHIR mapping (LP-23g): a coding chosen by its code system,
fallback paths, lists of several resource types, a MedicationRequest's start and stop by a stated
rule, one reference hop inside the record, HTML notes read as text, code sets, the bounds on an
agent the deployment does not own, and what each field looks for (`FhirAdapter.looked_for`, for
LP-23d's data-coverage record).

On copies of the chart-summary test fixture (`adapter`, `tests/support/chart_fixture.py`
`chart_copy`), their mapping changed as each test needs, and its order-sign twin for a focus
chosen by a code set. Every case is synthetic; the site systems are fictional."""

from __future__ import annotations

import base64
import json
import socket
from pathlib import Path
from typing import Any

import pytest
import yaml

from mendari import Decision, load_agent
from mendari.core.origin import Origin
from mendari.errors import AgentValidationError
from mendari.evals.behaviour_tests import validate_agent
from mendari.standards import fhir
from mendari.standards.fhir import (
    MAX_CODE_SET_CODES,
    MAX_PATHS,
    NOT_FETCHED,
    UNDECODABLE,
    UNSUPPORTED_TYPE,
    FhirAdapter,
    html_text,
    medication_period,
    read_attachment,
)
from tests.conftest import CHART_DIR, DEMO_AGENT_DIR
from tests.support.chart_fixture import chart_copy, order_sign
from tests.support.fixture_records import (
    ICD10CM,
    ICD10PCS,
    ORDER_CODE_SET,
    ORDERS,
    SITE_DIAGNOSES,
    SITE_ORDERS,
    order_site_codes,
)

ICD = ICD10CM
SITE = SITE_DIAGNOSES  # a fictional site's own condition codes
MED = "https://site.example/fhir/CodeSystem/medications"  # fictional
PAYER = "https://site.example/fhir/NamingSystem/payers"  # fictional
PATIENT = {"resourceType": "Patient", "id": "p1", "birthDate": "1970-01-01"}
# The words the chart agent's mapping writes for a condition of its code set, which its canned
# answers read (`flag`'s map): `flag-soon` for a code on the synthetic watch list (I50.9).
MAPPING = yaml.safe_load((CHART_DIR / "fhir" / "mapping.yaml").read_text(encoding="utf-8"))
FLAGS = MAPPING["lists"]["conditions"]["fields"]["flag"]["map"]


def adapter(tmp_path: Path, changes: dict[str, Any], *, origin: Origin = "local") -> FhirAdapter:
    """The adapter of a copy of the chart-summary agent (`chart_copy`), its mapping changed by
    `changes` and the fictional systems here declared beside its own."""
    root = chart_copy(tmp_path, changes, systems=(MED, PAYER, SITE))
    return FhirAdapter.for_agent(load_agent(root, origin=origin))


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
    decision: the chart agent reads a condition on its watch list (heart failure) behind a
    site's own coding and asks for a visit soon, where a mapping that reads the first coding
    (`[0]`) finds nothing it knows and suggests no follow-up."""
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

    record = [PATIENT, condition("c1", (SITE, "HF"), (ICD, "I50.9"))]
    flagged = {"path": f"code.coding[system={ICD}].code", "map": FLAGS}
    first = {"path": "code.coding[0].code", "map": FLAGS}
    by_its_system = Decision(load_agent(chart_copy(tmp_path / "b", conditions(flag=flagged))))
    by_position = Decision(load_agent(chart_copy(tmp_path / "c", conditions(flag=first))))
    labels = [
        decision.decide_fhir(bundle(*record))[0].answers["follow_up"].label
        for decision in (by_its_system, by_position)
    ]
    assert labels == ["soon", "none"]


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
    its own fields, in the order the mapping gives the types, then the record's. A list of one
    type beside it (the agent's conditions) reads as before."""
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
    assert found["conditions"] == [{"code": "E11.9", "flag": "flag-routine"}]  # the agent's own


BOUNDS = "2026-03-01", "2026-06-30"
WRITTEN = "2026-02-20"  # authoredOn
UCUM = "http://unitsofmeasure.org"


def lasting(value: Any, code: str | None = None, **more: Any) -> dict[str, Any]:
    """A `boundsDuration`: `value` in the UCUM unit `code`, unless `more` says otherwise."""
    return {"value": value, **({"code": code, "system": UCUM} if code else {}), **more}


# A MedicationRequest's derived (start, end) under the rule in the spec (LP-23 conflict 39, with
# the changes the owner, a physician, chose on 2026-11-07): None is a key left out (unknown); a
# request with no item at all is left out of the list.
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
    # The stop from a `boundsDuration`: the start's day plus its days, rounded up.
    ("72 hours", {"authoredOn": WRITTEN, "duration": lasting(72, "h")}, (WRITTEN, "2026-02-23")),
    (
        "36 hours: 2 days",
        {"authoredOn": WRITTEN, "duration": lasting(36, "h")},
        (WRITTEN, "2026-02-22"),
    ),
    ("10 days", {"authoredOn": WRITTEN, "duration": lasting(10, "d")}, (WRITTEN, "2026-03-02")),
    (
        "half a day: 1",
        {"authoredOn": WRITTEN, "duration": lasting(0.5, "d")},
        (WRITTEN, "2026-02-21"),
    ),
    ("no days", {"authoredOn": WRITTEN, "duration": lasting(0, "d")}, (WRITTEN, WRITTEN)),
    ("2 weeks", {"authoredOn": WRITTEN, "duration": lasting(2, "wk")}, (WRITTEN, "2026-03-06")),
    # 3 months are 91.3125 days (UCUM's `mo`, 30.4375 days), so 92; a year 365.25 days, so 366.
    ("3 months", {"authoredOn": WRITTEN, "duration": lasting(3, "mo")}, (WRITTEN, "2026-05-23")),
    ("1 year", {"authoredOn": WRITTEN, "duration": lasting(1, "a")}, (WRITTEN, "2027-02-21")),
    (
        "from the bounds' start",
        {"bounds": (BOUNDS[0], None), "duration": lasting(10, "d")},
        (BOUNDS[0], "2026-03-11"),
    ),
    ("the bounds' end first", {"bounds": BOUNDS, "duration": lasting(10, "d")}, BOUNDS),
    (
        "a dateTime start: its day",
        {"authoredOn": "2026-02-20T22:30:00-05:00", "duration": lasting(10, "d")},
        ("2026-02-20T22:30:00-05:00", "2026-03-02"),
    ),
    (
        "UCUM's code, no system",
        {"authoredOn": WRITTEN, "duration": {"value": 10, "code": "d"}},
        (WRITTEN, "2026-03-02"),
    ),
    ("no start: no stop", {"duration": lasting(10, "d")}, (None, None)),
    (
        "a month alone: no stop",
        {"authoredOn": "2026-02", "duration": lasting(10, "d")},
        ("2026-02", None),
    ),
    ("another unit", {"authoredOn": WRITTEN, "duration": lasting(600, "min")}, (WRITTEN, None)),
    (
        "the unit's text only",
        {"authoredOn": WRITTEN, "duration": lasting(10, unit="days")},
        (WRITTEN, None),
    ),
    (
        "another system",
        {"authoredOn": WRITTEN, "duration": lasting(10, "d", system="https://site.example/units")},
        (WRITTEN, None),
    ),
    ("less than nothing", {"authoredOn": WRITTEN, "duration": lasting(-5, "d")}, (WRITTEN, None)),
    ("a number as text", {"authoredOn": WRITTEN, "duration": lasting("10", "d")}, (WRITTEN, None)),
    (
        "past the last date",
        {"authoredOn": WRITTEN, "duration": lasting(10**7, "a")},
        (WRITTEN, None),
    ),
    (
        "first dosage's duration only",
        {"authoredOn": WRITTEN, "second_duration": lasting(10, "d")},
        (WRITTEN, None),
    ),
    # Which requests are listed: an order to give the drug, never an order not to.
    *(
        (f"intent {intent}", {"intent": intent, "bounds": BOUNDS}, BOUNDS)
        for intent in ("order", "original-order", "reflex-order", "filler-order", "instance-order")
    ),
    *(
        (f"intent {intent} is left out", {"intent": intent, "bounds": BOUNDS}, None)
        for intent in ("proposal", "plan", "option")
    ),
    ("do not perform is left out", {"doNotPerform": True, "bounds": BOUNDS}, None),
    ("perform", {"doNotPerform": False, "bounds": BOUNDS}, BOUNDS),
]


def request(id_: str, case: dict[str, Any]) -> dict[str, Any]:
    resource: dict[str, Any] = {
        "resourceType": "MedicationRequest",
        "id": id_,
        "status": case.get("status", "active"),
        "intent": case.get("intent", "order"),
    }
    if "doNotPerform" in case:
        resource["doNotPerform"] = case["doNotPerform"]
    if "authoredOn" in case:
        resource["authoredOn"] = case["authoredOn"]
    repeat: dict[str, Any] = {}
    if "bounds" in case:
        start, end = case["bounds"]
        repeat["boundsPeriod"] = {k: v for k, v in (("start", start), ("end", end)) if v}
    if "duration" in case:  # R4 allows one bounds[x]; a record may hold both, the period read first
        repeat["boundsDuration"] = case["duration"]
    dosages: list[dict[str, Any]] = [
        {"timing": {"repeat": repeat}} if repeat else {"text": "as directed"}
    ]
    if "second_bounds" in case:
        start, end = case["second_bounds"]
        dosages.append({"timing": {"repeat": {"boundsPeriod": {"start": start, "end": end}}}})
    if "second_duration" in case:
        dosages.append({"timing": {"repeat": {"boundsDuration": case["second_duration"]}}})
    resource["dosageInstruction"] = dosages
    if "validity" in case:
        start, end = case["validity"]
        resource["dispenseRequest"] = {"validityPeriod": {"start": start, "end": end}}
    return resource


def test_a_medication_requests_start_and_stop_follow_the_stated_derivation(tmp_path: Path) -> None:
    """`medication_period`: the start is the first dosage instruction's
    `timing.repeat.boundsPeriod.start`, else `authoredOn`; the stop is that `boundsPeriod.end`,
    else the start's day plus that timing's `boundsDuration` in whole days, rounded up (h, d, wk,
    mo, a; a start with its day, a UCUM code), else unknown, never inferred from the status; the
    dispensing window is not read; a request entered in error, a proposal, a plan, an option and
    an order not to give the drug are left out. Each case of the table, alone and all in one
    record."""
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


def test_a_duration_is_whole_days_in_a_ucum_unit_of_time() -> None:
    """`duration_days`: a finite number of at least 0 times its UCUM unit's days, rounded up,
    with exact arithmetic (24 hours are 1 day, not 2); anything else gives none, and so does a
    start that is not a real day (`stop_after`)."""
    assert [fhir.duration_days(lasting(24 * n, "h")) for n in (1, 2, 3)] == [1, 2, 3]
    assert fhir.duration_days(lasting(1, "mo")) == 31  # 30.4375 days
    assert fhir.duration_days(lasting(4, "a")) == 1461  # 365.25 days a year, exactly
    assert fhir.duration_days(lasting(0.1, "d")) == 1 and fhir.duration_days(lasting(2.4, "h")) == 1
    assert fhir.duration_days(lasting(10**5000, "d")) == 10**5000  # no text conversion limit
    assert fhir.stop_after("2026-02-20", lasting(10**5000, "d")) is None
    for value in (float("nan"), float("inf"), True, None, [10]):
        assert fhir.duration_days(lasting(value, "d")) is None, value
    for duration in (None, "10 d", [lasting(10, "d")], {"value": 10, "code": ["d"]}):
        assert fhir.duration_days(duration) is None, duration
    assert fhir.stop_after("2026-02-30", lasting(1, "d")) is None
    assert fhir.stop_after("9999-12-31", lasting(1, "d")) is None
    assert fhir.stop_after(20260220, lasting(1, "d")) is None
    assert fhir.stop_after("2024-02-28", lasting(1, "d")) == "2024-02-29"


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
    with pytest.raises(AgentValidationError, match=r"follow\n.*Input should be a valid string"):
        adapter(tmp_path / "hops", hops)


class Scanned(list[Any]):
    """Resources of a record (of one type, or contained in one resource), counting each time
    something reads them all."""

    def __init__(self, items: list[Any]) -> None:
        super().__init__(items)
        self.scans = 0

    def __iter__(self) -> Any:
        self.scans += 1
        return super().__iter__()


def test_a_follow_is_one_lookup_a_reference_whatever_the_record_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bounds bound the work too: a `follow` is resolved once for its field, however many
    paths the field tries, and each reference is a lookup: `Type/id` and `fullUrl` in the
    indexed record, never a scan of each resource of its type, and `#id` among a resource's
    contained ones, read once for the resource, not once a reference. So an agent the
    deployment does not own maps a record in time that grows with the record, not with its
    square. A plain mapping (not `index`'s) still resolves, the first of an id winning."""
    paths = [f"Medication.code.coding[{n}].code" for n in range(MAX_PATHS)]
    source = {"fields": {"code": {"follow": "medicationReference", "paths": paths}}}
    payer = {
        "follow": "Coverage.payor",
        "paths": [f"Organization.identifier[{n}].value" for n in range(MAX_PATHS)],
    }
    changes = {
        "lists": {"medications": {"resources": {"MedicationRequest": source}}},
        "fields": {"payer": payer},
    }
    mapped = adapter(tmp_path, changes, origin="hub")
    count = 50

    def ordered(number: int, reference: str) -> dict[str, Any]:
        return {
            "resourceType": "MedicationRequest",
            "id": f"r{number}",
            "status": "active",
            "intent": "order",
            "medicationReference": {"reference": reference},
        }

    medications = [
        {"resourceType": "Medication", "id": f"m{n}", "code": {"coding": [{"code": f"M{n}"}]}}
        for n in range(count)
    ]
    misses = [ordered(n, "Medication/none") for n in range(count)]  # every path finds nothing
    hits = [ordered(count + n, f"Medication/m{n}") for n in range(count)]
    coverage = {  # payors contained in it, none with an identifier: every path finds nothing
        "resourceType": "Coverage",
        "id": "cov",
        "status": "active",
        "payor": [{"reference": f"#o{n}"} for n in range(count)],
        "contained": [{"resourceType": "Organization", "id": f"o{n}"} for n in range(count)],
    }
    indexed = mapped.index(bundle(PATIENT, *misses, *hits, *medications, coverage))
    indexed["Medication"] = by_type = Scanned(indexed["Medication"])
    held = indexed["Coverage"][0]
    held["contained"] = contained = Scanned(held["contained"])
    resolved: list[Any] = []
    resolve = fhir.resolve

    def counted(reference: Any, container: Any, record: Any) -> Any:
        resolved.append(reference)
        return resolve(reference, container, record)

    monkeypatch.setattr(fhir, "resolve", counted)
    found = mapped.to_input(indexed)
    assert found["medications"] == [{"code": f"M{n}"} for n in range(count)]
    assert "payer" not in found
    assert len(resolved) == 3 * count  # once a reference, not once a path
    assert (by_type.scans, contained.scans) == (0, 1)

    plain = {"Medication": [{"resourceType": "Medication", "id": "m1", "n": 1}, {"id": "m1"}]}
    assert resolve({"reference": "Medication/m1"}, None, plain) == plain["Medication"][0]
    one = {"Medication": {"resourceType": "Medication", "id": "m2"}}
    assert resolve({"reference": "Medication/m2/_history/3"}, None, one) == one["Medication"]
    assert resolve({"reference": "Medication/m3"}, None, one) is None
    twice = {"contained": [{"resourceType": "Organization", "id": "o1", "n": 1}, {"id": "o1"}]}
    twice["contained"].append({"resourceType": "Organization", "id": "o1", "n": 3})
    assert resolve({"reference": "#o1"}, twice, {}) == twice["contained"][0]
    assert resolve({"reference": "#o2"}, twice, {}) is None


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
    "<html><head><style>p {color: red}</style><script>var note = 'warfarin';</script></head>"
    "<body><h1>Visit</h1><p>Seen for diabetes &amp; follow-up.</p><ul><li>No   new complaints</li>"
    "</ul><script type='text/javascript'>alert('warfarin')</script></body></html>"
)


def test_an_html_note_is_read_as_text_and_another_type_is_unreadable(tmp_path: Path) -> None:
    """`attachment_text`: text/plain as it is, text/html as text with its scripts and styles
    dropped, inline or from a Binary in the record. Any other content type (RTF, PDF, none) is
    `unsupported_type`, data that does not decode `undecodable`, a url to a Binary the record
    does not hold `not_fetched`; none of them adds text. Through a decision (the chart agent's
    rules, which read the notes), a drug named in a script is not read and the same drug in a
    paragraph is."""
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
        note("n2", "application/rtf", "{\\rtf1 Takes warfarin.}"),
        note("n3", "text/plain", "Plain note."),
        note("n4", "text/html", url="Binary/b1"),
        note("n5", "text/html", url="Binary/missing"),
        binary,
    )
    assert found["notes"] == "\n\n".join([html_text(HTML), "Plain note.", "From a Binary."])
    rtf_only = read(mapped, note("n2", "application/rtf", "{\\rtf1 Takes warfarin.}"))
    assert "notes" not in rtf_only

    decision = Decision(load_agent(chart_copy(tmp_path / "b", use="rules")))
    labels = []
    for markup in (HTML, "<p>Takes warfarin every day.</p>"):
        result, _ = decision.decide_fhir(bundle(PATIENT, note("n1", "text/html", markup)))
        labels.append(result.answers["on_anticoagulant"].label)
    assert labels == ["no", "yes"]


def test_an_attachments_base64_may_hold_line_breaks_and_spaces() -> None:
    """R4's base64Binary allows whitespace between its characters, and an EHR may wrap `data`
    every 76 characters as MIME does: `attachment_text` reads such data, inline or in a Binary.
    A character outside base64, a no-break space among them, is still `undecodable`."""
    text = "Seen for diabetes follow-up. No new complaints. " * 6
    wrapped = base64.encodebytes(text.encode()).decode()  # a line break every 76 characters
    assert wrapped.strip().count("\n") > 1
    spaced = " \t" + "\r\n ".join(wrapped.split("\n"))
    for data in (wrapped, spaced):
        assert read_attachment({"contentType": "text/plain", "data": data}) == (text.strip(), None)
    html = base64.encodebytes(f"<p>{text}</p>".encode()).decode()
    assert read_attachment({"contentType": "text/html", "data": html}) == (text.strip(), None)
    binary = {"resourceType": "Binary", "id": "b1", "contentType": "text/plain", "data": wrapped}
    linked = {"contentType": "text/plain", "url": "Binary/b1"}
    assert read_attachment(linked, record={"Binary": [binary]}) == (text.strip(), None)
    encoded = b64("A note.")
    for stray in ("\u00a0", "\u2028", "\x0b"):  # no-break space, line separator, vertical tab
        unreadable = {"contentType": "text/plain", "data": encoded[:4] + stray + encoded[4:]}
        assert read_attachment(unreadable) == (None, UNDECODABLE), repr(stray)


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


def test_a_followed_field_falls_back_to_a_path_of_its_own_type(tmp_path: Path) -> None:
    """After a `follow`, a path that starts with the field's own type is read where the field
    is (LP-23k): a top-level field's `follow` type (`Coverage` for `Coverage.payor[0]`), a list
    field's resource type. So the payer is the Organization's identifier when the reference
    reaches one, else the payor's own identifier; and a MedicationRequest's drug is its
    Medication's code, else its own `medicationCodeableConcept`'s. A link to a type the paths
    read through the reference is still a link (it abstains, `unreadable_links`), whatever the
    fallback reads; the field's own type is never one (`followed_types`)."""
    from mendari.standards import data_coverage

    payer = {
        "follow": "Coverage.payor[0]",
        "paths": [
            f"Organization.identifier[system={PAYER}].value",
            "Coverage.payor[0].identifier.value",
        ],
    }
    code = {
        "follow": "medicationReference",
        "paths": [
            f"Medication.code.coding[system={MED}].code",
            f"MedicationRequest.medicationCodeableConcept.coding[system={MED}].code",
        ],
    }
    source = {"fields": {"id": {"path": "id"}, "code": code}}
    changes = {
        "lists": {"medications": {"resources": {"MedicationRequest": source}}},
        "fields": {"payer": payer},
    }
    mapped = adapter(tmp_path, changes)
    payer_field = mapped.mapping.fields["payer"]
    code_field = mapped.mapping.lists["medications"].sources()["MedicationRequest"].fields["code"]
    assert fhir.own_type(payer_field, None) == "Coverage"
    assert fhir.followed_types(payer_field, None) == {"Organization"}
    assert fhir.own_type(code_field, "MedicationRequest") == "MedicationRequest"
    assert fhir.followed_types(code_field, "MedicationRequest") == {"Medication"}
    assert fhir.own_type(mapped.mapping.fields["patient.age"], None) is None  # no `follow`

    def coverage(**payor: Any) -> dict[str, Any]:
        return {"resourceType": "Coverage", "id": "cov", "status": "active", "payor": [payor]}

    organization = {
        "resourceType": "Organization",
        "id": "o1",
        "identifier": [{"system": PAYER, "value": "PAYER-A"}],
    }
    site_only = {
        "resourceType": "Organization",
        "id": "o2",
        "identifier": [{"system": SITE, "value": "S-77"}],
    }
    cases: list[tuple[str, list[dict[str, Any]], str | None]] = [
        (
            "the reference reaches it",
            [coverage(reference="Organization/o1"), organization],
            "PAYER-A",
        ),
        ("an identifier only", [coverage(identifier={"value": "acme"})], "acme"),
        (
            "reached, but not by the system: the payor's own",
            [coverage(reference="Organization/o2", identifier={"value": "acme"}), site_only],
            "acme",
        ),
        ("reached, neither", [coverage(reference="Organization/o2"), site_only], None),
        ("the patient pays: not a link", [coverage(reference="Patient/p1")], None),
    ]
    for name, record, want in cases:
        assert read(mapped, *record).get("payer") == want, name
        indexed = mapped.index(bundle(PATIENT, *record))
        assert data_coverage.unreadable_links(mapped.mapping, indexed) == {}, name
    linked = mapped.index(
        bundle(PATIENT, coverage(reference="Organization/missing", identifier={"value": "acme"}))
    )
    assert mapped.to_input(linked)["payer"] == "acme"  # the fallback reads
    assert data_coverage.unreadable_links(mapped.mapping, linked) == {"payer": {"not_fetched": 1}}

    medication = {
        "resourceType": "Medication",
        "id": "m1",
        "code": {"coding": [{"system": MED, "code": "M1"}]},
    }

    def ordered(id_: str, **drug: Any) -> dict[str, Any]:
        return {"resourceType": "MedicationRequest", "id": id_, "status": "active", **drug}

    concept = {"coding": [{"system": SITE, "code": "x"}, {"system": MED, "code": "M2"}]}
    found = read(
        mapped,
        ordered("r1", medicationReference={"reference": "Medication/m1"}),
        ordered("r2", medicationCodeableConcept=concept),
        ordered("r3", medicationReference={"reference": "#gone"}),
        medication,
    )
    assert found["medications"] == [
        {"id": "r1", "code": "M1"},
        {"id": "r2", "code": "M2"},
        {"id": "r3"},
    ]
    looked = mapped.looked_for()["lists"]["medications"]["MedicationRequest"]["code"]
    assert looked["follow"] == "MedicationRequest.medicationReference"
    assert looked["paths"] == code["paths"]


def test_focus_where_reads_a_code_set_and_a_sites_translation_finds_its_focus(
    tmp_path: Path,
) -> None:
    """`focus_where: {path, code_set}` (LP-23k), on the chart agent's order-sign twin, whose
    focus is a ServiceRequest coded in its set `checked_orders` (ICD-10-PCS): a resource of the
    focus type counts when the codings at its path give one of the set's own codes, wherever the
    coding sits; another code of the set's system, or the set's code in another system, does
    not. A site's translation into the set makes its own code count too (`is_focus`,
    `focus_resources`, `index`), and a record whose focus counted only through it says so
    (`read`'s `translated`), so its decision is never AUTO; one with the agent's own code is read
    as without the map. Its data-coverage record names the set's codes. `in` and `code_set` are
    one or the other, and an unknown code set is refused at load."""
    from mendari.standards import data_coverage

    mapped = FhirAdapter.for_agent(load_agent(order_sign(tmp_path)))
    colonoscopy = "0DJD8ZZ"

    def order(id_: str, *codings: tuple[str, str]) -> dict[str, Any]:
        coding = [{"system": system, "code": code} for system, code in codings]
        return {
            "resourceType": "ServiceRequest",
            "id": id_,
            "status": "active",
            "intent": "order",
            "code": {"coding": coding, "text": "An order"},
        }

    own = order("own", (SITE_ORDERS, "ORD-31"), (ICD10PCS, colonoscopy))
    other = order("other", (ICD10PCS, "0DJ07ZZ"))  # a code of the set's system it does not list
    elsewhere = order("elsewhere", (SITE_ORDERS, colonoscopy))
    site = order("site", (SITE_ORDERS, "ORD-31"))
    translations = {ORDER_CODE_SET: order_site_codes()}
    everything = [own, other, elsewhere, site]
    assert [mapped.is_focus(r) for r in everything] == [True, False, False, False]
    assert [mapped.is_focus(r, site_codes=translations) for r in everything] == [
        True,
        False,
        False,
        True,
    ]
    assert [r["id"] for r in mapped.focus_resources(everything)] == ["own"]
    assert [r["id"] for r in mapped.focus_resources(everything, site_codes=translations)] == [
        "own",
        "site",
    ]
    with pytest.raises(fhir.FhirError, match="missing required FHIR resources: ServiceRequest"):
        mapped.index(bundle(PATIENT, site))
    indexed = mapped.index(bundle(PATIENT, site), site_codes=translations)
    assert mapped.read(indexed, site_codes=translations).translated == (ORDER_CODE_SET,)
    for_own = mapped.index(bundle(PATIENT, own), site_codes=translations)
    assert mapped.read(for_own, site_codes=translations).translated == ()
    assert mapped.read(mapped.index(bundle(PATIENT, own))).translated == ()
    coverage = data_coverage.measure(mapped.mapping, for_own, unreadable={})
    assert coverage["focus"]["codes"] == sorted(ORDERS)
    assert coverage["focus"]["paths"] == ["ServiceRequest.code.coding"]

    both = {"focus_where": {"in": [colonoscopy]}}  # beside the twin's `code_set`
    with pytest.raises(AgentValidationError, match="either `in` or `code_set`, not both"):
        FhirAdapter.for_agent(load_agent(order_sign(tmp_path / "both", both)))
    unknown = {"focus_where": {"code_set": "nothing"}}
    with pytest.raises(AgentValidationError, match="focus_where names 'nothing'"):
        FhirAdapter.for_agent(load_agent(order_sign(tmp_path / "unknown", unknown)))


def test_an_outside_agents_new_keys_are_bounded_at_load(tmp_path: Path) -> None:
    """An agent the deployment does not own tries at most 4 paths a field and has at most 200
    codes a code set, refused at load past either; an owned agent loads past them and `mendari
    validate` refuses it. A `follow` is one hop for every agent (it takes one path)."""
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
    root = chart_copy(tmp_path / "beyond", beyond)
    problems = [
        "fhir/mapping.yaml: lists.conditions.fields.code.paths tries 5 paths, more than 4",
        "fhir/mapping.yaml: code_sets.big has 201 codes, more than 200",
    ]
    with pytest.raises(AgentValidationError) as refused:
        load_agent(root, origin="hub")
    assert refused.value.problems == problems
    owned = load_agent(root)
    assert len(FhirAdapter.for_agent(owned).mapping.code_sets["big"].codes) == 201
    with pytest.raises(AgentValidationError) as refused:
        validate_agent(owned)
    assert refused.value.problems == problems


def test_a_mappings_paths_and_systems_are_checked_at_load(tmp_path: Path) -> None:
    """An agent's paths are of the subset, each `[system=…]` and code set names a system its
    `code_systems` declares, a list's types are resource types and `medication_period` is a
    MedicationRequest's: for every agent, wherever it comes from."""
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
    root = chart_copy(tmp_path, changes)
    subset = "dotted element names, each with at most one [n], [*] or [system=<uri>])"
    origins: tuple[Origin, ...] = ("local", "hub")
    for origin in origins:
        with pytest.raises(AgentValidationError) as refused:
            FhirAdapter.for_agent(load_agent(root, origin=origin))
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
    it selects on and the agent's own codes it looks for (a code set's, or a map's keys), from
    the mapping alone. The same on every call, JSON, and for a mapping with no lists (the
    bundled demo agent's)."""
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
    duration = "MedicationRequest.dosageInstruction[0].timing.repeat.boundsDuration"
    assert mapped.looked_for() == {
        "fields": {
            "patient.age": looked(["Patient.birthDate"]),
            "patient.sex": looked(["Patient.gender"], codes=("female", "male", "other", "unknown")),
            "problems": looked(["Condition.code.text"]),
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
                    "end": looked([f"{bounds}.end", duration]),  # a duration from the start
                },
            },
        },
    }
    assert json.dumps(mapped.looked_for()) == json.dumps(mapped.looked_for())

    demo = FhirAdapter.for_agent(load_agent(DEMO_AGENT_DIR)).looked_for()
    assert demo == {
        "fields": {
            "message.subject": looked(["Communication.topic.text"]),
            "message.body": looked(["Communication.payload.contentString"]),
        },
        "lists": {},
    }
