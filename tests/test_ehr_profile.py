# SPDX-License-Identifier: Apache-2.0
"""A vendor-shaped synthetic EHR (LP-23e, `tests/support/chart_ehr.py`): the chart-summary test
fixture's patients as an EHR's FHIR API returns them (notes linked to a Binary, in one rendition
or two; a site's own diagnosis codes beside ICD-10-CM; every medication a MedicationRequest
naming an RxNorm Medication by reference), synthetic and the same on every run, with no SNOMED
CT.

Each agent whose service is on a standard CDS hook decides on them through its own hook, as an
EHR calls it (no prefetch, the FHIR server and its token named), here in this process: the chart
fixture's `patient-view` and its twin's `order-sign` (`tests/support/chart_fixture.py`), the
profile's server (`ChartEhr`) answering the engine's reads. What each cannot read in these shapes
is pinned (`ABSTAINS`, `NOT_DECIDED`, `MISSED`), each with why: a regression fixture, from which
no rate is computed or shown. Synthetic test data, not clinically reviewed."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from mendari.core import clock
from mendari.core.agent import Agent, load_agent
from mendari.core.audit import AuditStore
from mendari.core.site_codes import ENV as SITE_CODES_ENV
from mendari.sandbox import cds_client
from mendari.server.app import create_app
from mendari.standards import fhir_structure, us_core
from mendari.standards.data_coverage import FORMAT
from mendari.standards.fhir import FhirAdapter
from tests.conftest import CHART_DIR
from tests.support.chart_ehr import TOKEN, ChartEhr, Rendition
from tests.support.chart_fixture import CHART_ID, ORDERS_ID, hook, order_hook, order_sign
from tests.support.fixture_records import (
    BASE,
    ICD10CM,
    RXNORM,
    SITE_CONDITION_CODES,
    SITE_DIAGNOSES,
    TODAY,
    Case,
    Condition,
    Medication,
    Note,
    site_code_map,
)

ROOT = Path(__file__).resolve().parents[1]
STANDARD_HOOKS = ("patient-view", "order-sign")

# -- the profile's patients ------------------------------------------------------------------------

# Five synthetic patients, each a shape an EHR sends. Their notes' own form is the rendition the
# EHR keeps them in (`RENDITIONS`), so a note read from its Binary has the case's own words.
PROFILE = (
    # Long-term conditions, one the site codes too; a medication the record ended; a plain note.
    Case(
        "chart-ehr-1",
        age=58,
        sex="male",
        conditions=(Condition("E11.9", "2018-05-02"), Condition("J45.909", "2010-03-03")),
        medications=(
            Medication("6809", "2023-03-01"),
            Medication("29046", "2021-01-10", "2021-07-10"),
        ),
        notes=(Note(("Reviewed at the clinic today.", "Routine follow-up in three months.")),),
    ),
    # The watch list and an anticoagulant, an HTML note: a visit soon.
    Case(
        "chart-ehr-2",
        age=80,
        sex="female",
        conditions=(Condition("I50.9", "2024-02-10"), Condition("I10", "2016-07-07")),
        medications=(Medication("11289", "2025-11-03"),),
        notes=(
            Note(
                (
                    "Seen in clinic for a review of the chart.",
                    "Follow-up within a week.",
                    "Takes warfarin every day.",
                ),
                "html",
            ),
        ),
    ),
    # An anticoagulant ordered, and an RTF note.
    Case(
        "chart-ehr-3",
        age=76,
        sex="male",
        conditions=(Condition("I48.91", "2020-01-15"),),
        medications=(Medication("1364430", "2025-06-01", None, "request", "contained", "bounds"),),
        notes=(
            Note(
                (
                    "Reviewed at the clinic today.",
                    "Review at the yearly visit.",
                    "Remains on apixaban.",
                )
            ),
        ),
    ),
    # Nothing to follow up; two notes, each in HTML with an RTF twin.
    Case(
        "chart-ehr-4",
        age=34,
        sex="female",
        conditions=(Condition("J06.9", "2026-09-20"),),
        notes=(
            Note(("Seen today with a family member.", "No follow-up needed."), "html"),
            Note(("Medicines reviewed with the patient.",), "html", "2026-10-02"),
        ),
    ),
    # The watch list (bleeding on an anticoagulant), a drug ordered for 90 days, no note.
    Case(
        "chart-ehr-5",
        age=71,
        sex="other",
        conditions=(Condition("D68.32", "2026-09-28"), Condition("E78.5", "2017-11-11")),
        medications=(
            Medication("1114195", "2026-08-01", None, "request", "concept", "duration", 90),
        ),
    ),
)
RENDITIONS: dict[str, Rendition] = {
    "chart-ehr-1": "plain",
    "chart-ehr-2": "html",
    "chart-ehr-3": "rtf",
    "chart-ehr-4": "twin",
}


def profile_server(**options: Any) -> ChartEhr:
    """The profile's FHIR server: every patient's record as the EHR keeps it, the bearer token
    a hook names required, and no MedicationStatement (the EHR keeps every medication as a
    MedicationRequest; a statement search finds none)."""
    return ChartEhr.of(
        *PROFILE, today=TODAY, rendition=RENDITIONS, statements="absent", token=TOKEN, **options
    )


# -- what each agent cannot read (the regression fixture) ------------------------------------------

RTF_NOTE = (
    "the note's Binary is RTF, which an agent never reads as text (`unsupported_type`, spec 0.3 "
    "§2.4): the hook reads text/plain and text/html Binaries through the allow-listed client "
    "(LP-23h), and of a note kept in two renditions the readable one"
)
ENDED_AS_ACTIVE = (
    "the EHR keeps every medication as a MedicationRequest, active, its dates in the dosage's "
    "bounds: a medication the case's own record ended (a completed MedicationStatement) reads "
    "as active, with the same dates"
)

# The patients whose decision is an ABSTAIN, each with the decision's reasons; and why each reason.
ABSTAINS: dict[str, dict[str, tuple[str, ...]]] = {
    CHART_ID: {"chart-ehr-3": ("could not read notes: unsupported_type",)},
    ORDERS_ID: {"chart-ehr-3": ("could not read notes: unsupported_type",)},
}
WHY_ABSTAINED = {"could not read notes: unsupported_type": RTF_NOTE}
# The patients the hook made no decision for, and why.
NOT_DECIDED: dict[str, dict[str, str]] = {CHART_ID: {}, ORDERS_ID: {}}
# Each part of the agent's input it reads otherwise from the vendor-shaped patients than from the
# same cases' own US Core records, over every patient it decided: the input's path (a list's own
# name when it found another number of items), and why.
MISSED: dict[str, dict[str, str]] = {
    CHART_ID: {"medications.status": ENDED_AS_ACTIVE, "notes": RTF_NOTE},
    ORDERS_ID: {"medications.status": ENDED_AS_ACTIVE, "notes": RTF_NOTE},
}


# -- every hook, called ----------------------------------------------------------------------------


@dataclass
class Called:
    """One hook call for one patient: the agent called, what it answered, the records of the
    decisions it made, and the requests the engine sent the EHR meanwhile."""

    case: Case
    agent: str
    hook: str
    status: int
    answer: Any
    decisions: list[dict[str, Any]]
    requests: list[httpx.Request]


def served(tmp_path: Path) -> list[Path]:
    """What the engine serves here: the chart fixture and its `order-sign` twin, and nothing
    else."""
    order_sign(tmp_path / "agents")
    return [CHART_DIR, tmp_path / "agents"]


def call_every_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[Called], ChartEhr, dict[str, Agent]]:
    """Every profile patient's hook, of every service on a standard hook the engine lists, called
    as an EHR calls it, on an engine whose FHIR reads go to the profile's server; the server
    (what it was asked is in its `requests`), and the agents by id."""
    monkeypatch.setattr(clock, "today", lambda: TODAY)
    server = profile_server()
    server.install(monkeypatch)
    paths = served(tmp_path)
    agents = {
        agent.id: agent for agent in (load_agent(CHART_DIR), load_agent(paths[1] / "chart-orders"))
    }
    db = tmp_path / "profile.db"
    called = []
    with (
        TestClient(create_app(db_path=db, agents_paths=paths)) as http,
        AuditStore(db) as store,
    ):
        listed = http.get("/cds-services").json()["services"]
        hooks = {s["id"]: s["hook"] for s in listed if s["hook"] in STANDARD_HOOKS}
        for service, kind in hooks.items():
            agent = service.replace(".", "/", 1)
            make = order_hook if kind == "order-sign" else hook
            for case in PROFILE:
                before = {d["id"] for d in store.query(limit=1000)}
                asked = len(server.requests)
                body = make(case, today=TODAY, fhir_server=BASE, token=TOKEN, prefetched=False)
                answer = http.post(f"/cds-services/{service}", json=body)
                made = [d["id"] for d in store.query(limit=1000) if d["id"] not in before]
                records = []
                for decision_id in made:
                    stored = store.get(decision_id)
                    assert stored is not None
                    records.append(stored["record"])
                called.append(
                    Called(
                        case,
                        agent,
                        kind,
                        answer.status_code,
                        answer.json(),
                        records,
                        server.requests[asked:],
                    )
                )
    return called, server, agents


def test_every_reference_agent_with_a_standard_hook_decides_on_the_vendor_shaped_patients_without_a_failed_hook(  # noqa: E501
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every agent the engine serves whose service is on a standard hook (`patient-view`,
    `order-sign`): the chart fixture and its twin, each called for the profile's five patients.
    Called with no prefetch, each hook reads the record from the profile's server (reads only,
    under its base, with the hook's token), answers `200` with CDS Hooks' cards, and records a
    decision with its data-coverage record for each patient, but for the patients pinned in
    `NOT_DECIDED`. A card names a recorded decision."""
    called, server, _ = call_every_hook(tmp_path, monkeypatch)
    assert {c.agent for c in called} == {CHART_ID, ORDERS_ID} == set(NOT_DECIDED)
    assert {c.hook for c in called} == set(STANDARD_HOOKS)
    assert all(sum(c.agent == agent for c in called) == len(PROFILE) for agent in NOT_DECIDED)
    assert server.requests and {r.method for r in server.requests} == {"GET"}
    assert all(str(r.url).startswith(f"{BASE}/") for r in server.requests)
    assert {r.headers.get("authorization") for r in server.requests} == {f"Bearer {TOKEN}"}
    for call in called:
        where = (call.agent, call.case.id)
        assert call.status == 200, (where, call.answer)
        assert cds_client.problems_of(call.answer) == [], where
        expected = 0 if call.case.id in NOT_DECIDED[call.agent] else 1
        assert len(call.decisions) == expected, where
        ids = {record["id"] for record in call.decisions}
        assert {card["uuid"] for card in call.answer["cards"]} <= ids, where
        for record in call.decisions:
            assert record["data_coverage"]["format"] == FORMAT, where
            assert record["agent"]["id"] == call.agent, where


def test_what_each_agent_cannot_read_yet_is_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per agent, the patients it abstains on (with the decision's reasons), those it makes no
    decision for, and each part of its input it reads otherwise than from the same cases' own US
    Core records, each with why. A step that teaches an agent to read a shape takes its entries
    off; a shape that stops being read fails here."""
    called, _, agents = call_every_hook(tmp_path, monkeypatch)
    abstains: dict[str, dict[str, tuple[str, ...]]] = {agent: {} for agent in NOT_DECIDED}
    not_decided: dict[str, set[str]] = {agent: set() for agent in NOT_DECIDED}
    missed: dict[str, set[str]] = {agent: set() for agent in NOT_DECIDED}
    for call in called:
        if not call.decisions:
            not_decided[call.agent].add(call.case.id)
            continue
        (record,) = call.decisions
        if record["outcome"] == "ABSTAIN":
            abstains[call.agent][call.case.id] = tuple(record["reasons"])
        own = own_record_input(agents[call.agent], call.case)
        missed[call.agent] |= differences(own, record["input"])
    assert abstains == ABSTAINS
    assert not_decided == {agent: set(pinned) for agent, pinned in NOT_DECIDED.items()}
    assert missed == {agent: set(pinned) for agent, pinned in MISSED.items()}
    reasons = {
        reason for pinned in ABSTAINS.values() for kept in pinned.values() for reason in kept
    }
    assert reasons <= set(WHY_ABSTAINED)  # every reason says why
    assert all(
        why for pinned in (*NOT_DECIDED.values(), *MISSED.values()) for why in pinned.values()
    )


def test_the_shipped_agents_read_the_profiles_linked_notes_through_the_allow_listed_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agents read a note through spec 0.3's `attachment_text`, and their hooks read what a
    note links to (LP-23h): each text/plain and text/html note's Binary is read once for a
    decision, from the profile's FHIR server under its base, with a GET, and the decision's notes
    are the case's own words. An RTF note's Binary is never read (its content type is checked
    first), and that decision abstains on it; of a note in HTML with an RTF twin, the HTML is
    read and the RTF never. A patient with no note reads none."""
    called, server, _ = call_every_hook(tmp_path, monkeypatch)
    held = server.held
    kinds: set[str] = set()
    read_notes = 0
    for call in called:
        where = (call.agent, call.case.id)
        reads = [
            str(r.url).removeprefix(f"{BASE}/") for r in call.requests if "/Binary/" in str(r.url)
        ]
        assert len(reads) == len(set(reads)), where
        kinds |= {held[read]["contentType"] for read in reads}
        (record,) = call.decisions
        documents = [
            resource
            for resource in held.values()
            if resource["resourceType"] == "DocumentReference"
            and resource["subject"]["reference"] == f"Patient/{call.case.id}"
        ]
        if not documents:
            assert "notes" not in record["input"], where
            assert reads == [], where
            continue
        links = [content["attachment"]["url"] for d in documents for content in d["content"]]
        readable = [link for link in links if held[link]["contentType"] != "text/rtf"]
        assert sorted(reads) == sorted(readable), where
        if not readable:
            assert record["outcome"] == "ABSTAIN", where
            continue
        assert record["input"]["notes"] == call.case.input()["notes"], where
        read_notes += 1
    assert kinds == {"text/plain", "text/html"}
    assert read_notes == 3 * 2  # three patients' notes, by each of the two agents


def site_only(server: ChartEhr) -> list[dict[str, Any]]:
    """The profile's resources as a site that codes a diagnosis with its own system only sends
    them: each Condition the site has a code for without its ICD-10-CM coding."""
    resources = json.loads(json.dumps(list(server.held.values())))
    for resource in resources:
        if resource["resourceType"] != "Condition":
            continue
        codings = resource["code"]["coding"]
        if any(coding["system"] == SITE_DIAGNOSES for coding in codings):
            resource["code"]["coding"] = [c for c in codings if c["system"] != ICD10CM]
    return resources


def summarized(
    folder: Path, monkeypatch: pytest.MonkeyPatch, server: ChartEhr
) -> dict[str, dict[str, Any]]:
    """Each patient's decision record by the chart fixture's hook, called with no prefetch on an
    engine serving the chart fixture alone, whose FHIR reads go to `server`."""
    server.install(monkeypatch)
    folder.mkdir()
    db = folder / "chart.db"
    found: dict[str, dict[str, Any]] = {}
    with (
        TestClient(create_app(db_path=db, agents_paths=[CHART_DIR])) as http,
        AuditStore(db) as store,
    ):
        for case in PROFILE:
            before = {d["id"] for d in store.query(limit=1000)}
            body = hook(case, today=TODAY, fhir_server=BASE, token=TOKEN, prefetched=False)
            answer = http.post("/cds-services/mendari-test.chart-summary", json=body)
            assert answer.status_code == 200, (case.id, answer.text)
            (made,) = [d["id"] for d in store.query(limit=1000) if d["id"] not in before]
            stored = store.get(made)
            assert stored is not None
            found[case.id] = stored["record"]
    return found


def test_the_profiles_site_diagnosis_codes_are_read_through_a_site_code_map_and_never_automated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The patients' Conditions through `MENDARI_SITE_CODES` (LP-23i), as an EHR calls the chart
    fixture's hook: the agent reads a condition's follow-up through its code set
    `follow_up_conditions`, into which the site's map (`site_code_map`) translates the site's
    fictional diagnosis codes. Sent as the profile has them (the site's coding first, then
    ICD-10-CM), the agent's own code wins and every decision is as without a map, the map's
    version and digest recorded. Sent as a site that codes with its own system only (the
    ICD-10-CM coding left out where the site has a code), the conditions are read through the
    map, and what would automate goes to a person instead (`site_code`). The patient with an RTF
    note still abstains on it, map or not. Without the map, the site's own codes alone are not
    read as the agent's: a chart on the watch list reads as one with less to follow up, and is
    automated."""
    monkeypatch.setattr(clock, "today", lambda: TODAY)
    mapped = site_code_map()
    monkeypatch.setenv(SITE_CODES_ENV, json.dumps(mapped))
    as_sent = profile_server()
    own_only = ChartEhr(site_only(as_sent), statements="absent", token=TOKEN)
    note = ("could not read notes: unsupported_type",)
    translated = "site code translated (follow_up_conditions): a person decides"
    decided = {
        name: summarized(tmp_path / name, monkeypatch, server)
        for name, server in (("as-sent", as_sent), ("site-only", own_only))
    }
    stamps = {
        record["site_codes"]["version"] for found in decided.values() for record in found.values()
    }
    assert stamps == {mapped["default"]["version"]}  # every decision names the map it read with
    pinned = {
        name: {
            patient: (record["outcome"], record["recommendation"], translated in record["reasons"])
            for patient, record in found.items()
        }
        for name, found in decided.items()
    }
    assert pinned == {
        "as-sent": {
            "chart-ehr-1": ("AUTO", "routine", False),
            "chart-ehr-2": ("HUMAN_REVIEW", "soon", False),
            "chart-ehr-3": ("ABSTAIN", None, False),
            "chart-ehr-4": ("AUTO", "none", False),
            "chart-ehr-5": ("HUMAN_REVIEW", "soon", False),
        },
        "site-only": {
            "chart-ehr-1": ("HUMAN_REVIEW", "routine", True),
            "chart-ehr-2": ("HUMAN_REVIEW", "soon", True),
            "chart-ehr-3": ("ABSTAIN", None, False),
            "chart-ehr-4": ("AUTO", "none", False),
            "chart-ehr-5": ("HUMAN_REVIEW", "soon", True),
        },
    }
    for found in decided.values():
        assert tuple(found["chart-ehr-3"]["reasons"]) == note
    # Without the map: the profile as sent is decided the same, and the site's own codes alone
    # are not read as the agent's, so nothing is translated.
    monkeypatch.delenv(SITE_CODES_ENV)
    unmapped = {
        name: summarized(tmp_path / f"{name}-unmapped", monkeypatch, server)
        for name, server in (("as-sent", as_sent), ("site-only", own_only))
    }
    assert all(r["site_codes"] is None for found in unmapped.values() for r in found.values())
    same = lambda r: (r["outcome"], r["recommendation"], r["reasons"])  # noqa: E731
    assert {p: same(r) for p, r in unmapped["as-sent"].items()} == {
        p: same(r) for p, r in decided["as-sent"].items()
    }
    alone = {p: (r["outcome"], r["recommendation"]) for p, r in unmapped["site-only"].items()}
    assert (alone["chart-ehr-2"], alone["chart-ehr-5"]) == (
        ("AUTO", "none"),  # heart failure and hypertension unread: nothing to follow up
        ("AUTO", "routine"),  # the bleeding unread: hyperlipidemia's routine follow-up alone
    )


def own_record_input(agent: Agent, case: Case) -> dict[str, Any]:
    """What the agent reads from the case as its own US Core record (`Case.bundle`): the same
    patient as the profile's, as the case's own resources hold it."""
    adapter = FhirAdapter.for_agent(agent)
    read: dict[str, Any] = adapter.to_input(adapter.index(case.bundle(TODAY)))
    return read


def differences(own: Any, read: Any, path: str = "") -> set[str]:
    """The paths at which `read` (the vendor-shaped patient's input) holds other values than
    `own` (the same case's own record's): a list's path when it holds another number of items,
    else each field of its items whose values differ, in any order."""
    if isinstance(own, dict) or isinstance(read, dict):
        own, read = own or {}, read or {}
        return {
            found
            for key in own.keys() | read.keys()
            for found in differences(own.get(key), read.get(key), f"{path}.{key}".lstrip("."))
        }
    if isinstance(own, list) or isinstance(read, list):
        own, read = own or [], read or []
        if len(own) != len(read):
            return {path}
        fields = {key for item in [*own, *read] if isinstance(item, dict) for key in item}
        if not fields:
            return set() if sorted(map(repr, own)) == sorted(map(repr, read)) else {path}
        return {
            f"{path}.{key}"
            for key in fields
            if sorted(repr(item.get(key)) for item in own)
            != sorted(repr(item.get(key)) for item in read)
        }
    return set() if own == read else {path}


# -- the profile itself ----------------------------------------------------------------------------


def test_the_profile_has_linked_notes_several_codings_and_medication_requests() -> None:
    """The shapes the server's module says, built the same on every run: notes as links to a
    Binary in each of text/plain, text/html and text/rtf, and a note kept in HTML with an RTF
    twin; a Condition the site has a code for coded by the site first, then in ICD-10-CM, and
    any other in ICD-10-CM alone; every medication a MedicationRequest naming an RxNorm
    Medication beside it, its period a dosage `boundsPeriod` with an `authoredOn` before its
    start, or a `boundsDuration` from when it was written. No MedicationStatement. Every
    reference resolves in it, every resource belongs to one of the patients (or is the one
    Practitioner their requests name), and it conforms to US Core."""
    assert profile_server().bundle() == profile_server().bundle()
    held = profile_server().held
    resources = list(held.values())
    patients = {f"Patient/{case.id}" for case in PROFILE}
    for resource in resources:
        for reference in _references(resource):
            assert reference in held, (resource["id"], reference)
        owners = {ref for ref in _references(resource) if ref.startswith("Patient/")}
        assert owners <= patients, resource["id"]
    of = lambda kind: [r for r in resources if r["resourceType"] == kind]  # noqa: E731
    assert not of("MedicationStatement")
    kinds, twins = set(), 0
    for document in of("DocumentReference"):
        renditions = []
        for content in document["content"]:
            attachment = content["attachment"]
            assert set(attachment) == {"contentType", "url"}, document["id"]  # a link, no data
            binary = held[attachment["url"]]
            assert binary["resourceType"] == "Binary"
            assert binary["contentType"] == attachment["contentType"]
            renditions.append(binary["contentType"])
        kinds |= set(renditions)
        twins += renditions == ["text/html", "text/rtf"]
    assert kinds == {"text/plain", "text/html", "text/rtf"} and twins == 2
    rtf = [b for b in of("Binary") if b["contentType"] == "text/rtf"]
    assert rtf and all(_text(b).startswith("{\\rtf1") for b in rtf)
    for condition in of("Condition"):
        systems = [coding["system"] for coding in condition["code"]["coding"]]
        icd = next(c["code"] for c in condition["code"]["coding"] if c["system"] == ICD10CM)
        site = icd in SITE_CONDITION_CODES
        assert systems == ([SITE_DIAGNOSES, ICD10CM] if site else [ICD10CM]), condition["id"]
    requests = of("MedicationRequest")
    assert requests and len(requests) == len(of("Medication"))
    for request in requests:
        medication = held[request["medicationReference"]["reference"]]
        assert RXNORM in [c["system"] for c in medication["code"]["coding"]]
        assert "medicationCodeableConcept" not in request
        (dosage,) = request["dosageInstruction"]
        repeat = dosage["timing"]["repeat"]
        if "boundsPeriod" in repeat:
            assert request["authoredOn"] < repeat["boundsPeriod"]["start"], request["id"]
        else:
            assert set(repeat) == {"boundsDuration"}, request["id"]
    assert us_core.validate(resources) == []
    assert fhir_structure.check(profile_server().bundle()) == []


def test_the_profile_carries_no_snomed_ct(tmp_path: Path) -> None:
    """Through scripts/check_snomed.py, as one of Mendari's own files (where any number written as
    a SNOMED CT concept id counts): the profile as its server holds it, and the modules that build
    it. The same check finds a SNOMED CT coding put in."""
    folder = tmp_path / "mendari" / "sandbox"
    folder.mkdir(parents=True)
    text = json.dumps(profile_server().bundle(), indent=2)
    (folder / "ehr-profile.json").write_text(text, encoding="utf-8")
    for module in ("chart_ehr.py", "fixture_records.py"):
        source = ROOT / "tests" / "support" / module
        (folder / module).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    script = str(ROOT / "scripts" / "check_snomed.py")
    clean = subprocess.run([sys.executable, script, str(tmp_path)], capture_output=True, text=True)
    assert clean.returncode == 0, clean.stdout
    assert "no SNOMED CT content" in clean.stdout
    coded = text.replace(SITE_DIAGNOSES, "http://snomed.info/sct", 1)
    (folder / "ehr-profile.json").write_text(coded, encoding="utf-8")
    caught = subprocess.run([sys.executable, script, str(tmp_path)], capture_output=True, text=True)
    assert caught.returncode == 1
    assert "mendari/sandbox/ehr-profile.json: names SNOMED CT" in caught.stdout


def _references(value: Any) -> list[str]:
    """Every `reference` in a resource, at any depth."""
    if isinstance(value, dict):
        found = [value["reference"]] if isinstance(value.get("reference"), str) else []
        return found + [r for item in value.values() for r in _references(item)]
    if isinstance(value, list):
        return [r for item in value for r in _references(item)]
    return []


def _text(binary: dict[str, Any]) -> str:
    return base64.b64decode(binary["data"]).decode("utf-8")
