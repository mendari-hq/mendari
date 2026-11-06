# SPDX-License-Identifier: Apache-2.0
"""The vendor-shaped synthetic EHR profile (`sandbox/ehr_profile.py`, LP-23e): patients for every
reference pack with a standard CDS hook, in the shapes an EHR's FHIR API returns them (notes
linked to a Binary, several codings, MedicationRequest, the payer by reference), synthetic and
the same on every run, with no SNOMED CT.

Each pack decides on them through its own hook, as the sandbox's signed client calls it
(`cds_client.hook_request`: no prefetch, the FHIR server named), here in this process: a FHIR
server built from the profile (`profile_server`) answers the engine's reads. What each pack
cannot read in these shapes yet is pinned (`ABSTAINS`, `NOT_DECIDED`, `MISSED`), each with why:
a regression fixture, from which no rate is computed or shown. LP-23h, 23i and 23k each take
entries off it."""

from __future__ import annotations

import base64
import copy
import json
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest
from fastapi.testclient import TestClient

from mendari.core import clock
from mendari.core.audit import AuditStore
from mendari.core.reference import reference_packs
from mendari.core.registry import resolve_pack
from mendari.core.site_codes import ENV as SITE_CODES_ENV
from mendari.sandbox import cds_client, ehr_profile
from mendari.sandbox.ehr_profile import (
    BASE,
    PRIOR_AUTH,
    REFERRAL_PACK,
    SITE_DIAGNOSES,
    SITE_DIAGNOSIS_SYSTEM,
    SITE_MRN,
    SITE_ORDER_TYPE_SYSTEM,
    SITE_PAYER_IDS,
    TRIAL,
    ProfilePatient,
)
from mendari.sandbox.fhir_records import CASE_SYSTEM, REFERRAL_SYSTEM, SANDBOX_TAG
from mendari.sandbox.synthetic_trial import ICD, LOCAL
from mendari.server.app import create_app
from mendari.standards import fhir_structure, us_core
from mendari.standards.data_coverage import FORMAT
from mendari.standards.fhir import FhirAdapter
from mendari.standards.record_fhir import ICD10, RXNORM
from tests.test_fhir_client import allow_ehr, fhir_json
from tests.test_site_codes import PACKS as PACKS_DIR
from tests.test_site_codes import SERVICE as PRESCREEN_SERVICE
from tests.test_site_codes import VERSION, mapped, site_pack

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 15)
Handler = Callable[[httpx.Request], httpx.Response]

# -- what each pack cannot read yet (the regression fixture) ---------------------------------------

LINKED_NOTE = (
    "a note is a link to a Binary, which the hook does not fetch (`not_fetched`): LP-23h reads "
    "text/plain and text/html through the allow-listed client, and RTF stays unreadable"
)
FIRST_CODING = (
    "a Condition is read by its first coding, and the site's own diagnosis code comes first in "
    "every other patient: LP-23g's selector by code system, which LP-23k adopts"
)
MEDICATION_REQUEST = (
    "the pack reads MedicationStatement, and the EHR sends MedicationRequest naming a Medication: "
    "LP-23g's MedicationRequest rule, which LP-23k adopts (owner decision 5)"
)
PAYER_REFERENCE = (
    "the payer is read as Coverage.payor's identifier, and the EHR sends a reference to an "
    "Organization holding the site's identifier: LP-23h follows it, LP-23i maps the site's id"
)
REFERRAL_CATEGORY = (
    "a referral is found by its category's first coding, here the site's order type, so no order "
    "is the pack's (no decision, no card): LP-23g's selector by code system, which LP-23k adopts"
)

# The patients whose decision is an ABSTAIN, each with the decision's reasons; and why each reason.
ABSTAINS: dict[str, dict[str, tuple[str, ...]]] = {
    TRIAL: {
        "ehr-trial-1": ("could not read notes: not_fetched",),
        "ehr-trial-2": ("could not read notes: not_fetched",),
        "ehr-trial-3": ("could not read notes: not_fetched",),
    },
    PRIOR_AUTH: {
        "ehr-pa-1": ("could not read notes: not_fetched",),
        "ehr-pa-2": ("could not read notes: not_fetched",),
        "ehr-pa-3": ("could not read notes: not_fetched",),
    },
    REFERRAL_PACK: {},
}
WHY_ABSTAINED = {"could not read notes: not_fetched": LINKED_NOTE}
# The patients the hook made no decision for, and why.
NOT_DECIDED: dict[str, dict[str, str]] = {
    TRIAL: {},
    PRIOR_AUTH: {},
    REFERRAL_PACK: {
        "ehr-ref-1": REFERRAL_CATEGORY,
        "ehr-ref-3": REFERRAL_CATEGORY,
        "ehr-ref-5": REFERRAL_CATEGORY,
    },
}
# Each part of the pack's input it reads less of, or otherwise, from the vendor-shaped patients
# than from the same cases' own US Core records (`fhir_records`), over every patient it decided:
# the input's path (a list's own name when it found fewer items), and why.
MISSED: dict[str, dict[str, str]] = {
    TRIAL: {
        "conditions.code": FIRST_CODING,
        "medications": MEDICATION_REQUEST,
        "notes": LINKED_NOTE,
    },
    PRIOR_AUTH: {
        "conditions.code": FIRST_CODING,
        "coverage.payer": PAYER_REFERENCE,
        "notes": LINKED_NOTE,
    },
    REFERRAL_PACK: {},
}


# -- a FHIR server holding the profile -------------------------------------------------------------

# The reference that names the patient, for the resource types the packs search (R4's `patient`
# search parameter); `subject` for every other one.
_PATIENT_OF = {"Coverage": "beneficiary"}
# The search parameters the packs' prefetch templates use, each a token matched by its code, as
# HAPI matches a token given without a system.
_TOKENS = {"category": "category", "code": "code", "status": "status"}


def profile_server(
    resources: Iterable[Mapping[str, Any]], seen: list[httpx.Request] | None = None
) -> Handler:
    """A FHIR server holding `resources` under `BASE`: a read (`<type>/<id>`), or a search by
    `patient` and the token parameters the packs' prefetch uses (`category`, `code` with codes
    separated by commas, `status`), answered as one searchset page. Anything else is a `400`, so
    a test sees a read it did not expect. Each request is added to `seen`."""
    held = {f"{r['resourceType']}/{r['id']}": r for r in resources}
    prefix = httpx.URL(BASE).path.rstrip("/") + "/"

    def answer(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        path = request.url.path.removeprefix(prefix)
        if request.method != "GET" or not str(request.url).startswith(f"{BASE}/"):
            return fhir_json({"resourceType": "OperationOutcome", "issue": []}, 400)
        if "/" in path:
            found = held.get(path)
            if found is None:
                return fhir_json({"resourceType": "OperationOutcome", "issue": []}, 404)
            return fhir_json(found)
        query = dict(parse_qsl(request.url.query.decode()))
        if "patient" not in query or not set(query) <= {"patient", *_TOKENS}:
            return fhir_json({"resourceType": "OperationOutcome", "issue": []}, 400)
        matches = [
            r
            for r in held.values()
            if r["resourceType"] == path
            and r.get(_PATIENT_OF.get(path, "subject"), {}).get("reference")
            == f"Patient/{query['patient']}"
            and all(_token(r, _TOKENS[name], query[name]) for name in query if name in _TOKENS)
        ]
        entries = [{"fullUrl": f"{BASE}/{path}/{r['id']}", "resource": r} for r in matches]
        bundle = {"resourceType": "Bundle", "type": "searchset", "total": len(entries)}
        return fhir_json({**bundle, "entry": entries})

    return answer


def _token(resource: Mapping[str, Any], element: str, wanted: str) -> bool:
    value = resource.get(element)
    concepts = value if isinstance(value, list) else [value]
    codes = {
        coding.get("code")
        for concept in concepts
        if isinstance(concept, dict)
        for coding in concept.get("coding", [])
    }
    if isinstance(value, str):
        codes.add(value)
    return bool(codes & set(wanted.split(",")))


# -- every hook, called ----------------------------------------------------------------------------


@dataclass
class Called:
    """One hook call for one patient: what it answered, and the records of the decisions it
    made."""

    patient: ProfilePatient
    service: str
    hook: str
    status: int
    answer: Any
    decisions: list[dict[str, Any]]


def call_every_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[Called], list[httpx.Request]]:
    """Every profile patient's hook, called as the sandbox's client calls it, on an engine whose
    FHIR reads go to `profile_server`; and the reads it made."""
    monkeypatch.setattr(clock, "today", lambda: TODAY)
    seen: list[httpx.Request] = []
    allow_ehr(monkeypatch, profile_server(ehr_profile.resources(TODAY), seen))
    db = tmp_path / "profile.db"
    called = []
    with TestClient(create_app(db_path=db)) as http, AuditStore(db) as store:
        listed = http.get("/cds-services").json()["services"]
        hooks = {service["id"]: service["hook"] for service in listed}
        for patient in ehr_profile.patients(TODAY):
            service = patient.pack_id.replace("/", ".")
            before = {d["id"] for d in store.query(limit=1000)}
            body = cds_client.hook_request(hooks[service], patient, BASE)
            answer = http.post(f"/cds-services/{service}", json=body)
            made = [d["id"] for d in store.query(limit=1000) if d["id"] not in before]
            records = []
            for decision_id in made:
                stored = store.get(decision_id)
                assert stored is not None
                records.append(stored["record"])
            called.append(
                Called(patient, service, hooks[service], answer.status_code, answer.json(), records)
            )
    return called, seen


def standard_hooked_packs() -> set[str]:
    """Every reference pack whose CDS Hooks service is on a standard hook."""
    found = set()
    for pack_id in reference_packs():
        hooks = resolve_pack(pack_id).manifest.cds_hooks
        if hooks is not None and hooks.hook in cds_client.STANDARD_HOOKS:
            found.add(pack_id)
    return found


def test_every_reference_pack_with_a_standard_hook_decides_on_the_vendor_shaped_patients_without_a_failed_hook(  # noqa: E501
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The profile has five patients for each reference pack whose service is on a standard hook
    (`patient-view`, `order-sign`). Called with no prefetch, each hook reads the record from the
    profile's server (reads only, under its base), answers `200` with CDS Hooks' cards, and
    records a decision with its data-coverage record for each patient, but for the patients
    pinned in `NOT_DECIDED`. A card names a recorded decision."""
    profile_packs = [spec.pack_id for spec in ehr_profile.PACKS]
    assert set(profile_packs) == standard_hooked_packs() == {TRIAL, PRIOR_AUTH, REFERRAL_PACK}
    called, seen = call_every_hook(tmp_path, monkeypatch)
    assert sorted({c.patient.pack_id for c in called}) == sorted(profile_packs)
    assert all(sum(c.patient.pack_id == pack for c in called) == 5 for pack in profile_packs)
    assert seen and {r.method for r in seen} == {"GET"}
    assert all(str(r.url).startswith(f"{BASE}/") for r in seen)
    for call in called:
        where = call.patient.id
        assert call.status == 200, (where, call.answer)
        assert cds_client.problems_of(call.answer) == [], where
        expected = 0 if where in NOT_DECIDED[call.patient.pack_id] else 1
        assert len(call.decisions) == expected, where
        ids = {record["id"] for record in call.decisions}
        assert {card["uuid"] for card in call.answer["cards"]} <= ids, where
        for record in call.decisions:
            assert record["data_coverage"]["format"] == FORMAT, where
            assert record["pack"]["id"] == call.patient.pack_id, where


def test_what_each_pack_cannot_read_yet_is_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per pack, the patients it abstains on (with the decision's reasons), those it makes no
    decision for, and each part of its input it reads otherwise than from the same cases' own US
    Core records, each with why. A step that teaches a pack to read a shape takes its entries off
    (LP-23h, 23i, 23k); a shape that stops being read fails here."""
    called, _ = call_every_hook(tmp_path, monkeypatch)
    abstains: dict[str, dict[str, tuple[str, ...]]] = {
        spec.pack_id: {} for spec in ehr_profile.PACKS
    }
    not_decided: dict[str, set[str]] = {spec.pack_id: set() for spec in ehr_profile.PACKS}
    missed: dict[str, set[str]] = {spec.pack_id: set() for spec in ehr_profile.PACKS}
    for call in called:
        pack = call.patient.pack_id
        if not call.decisions:
            not_decided[pack].add(call.patient.id)
            continue
        (record,) = call.decisions
        if record["outcome"] == "ABSTAIN":
            abstains[pack][call.patient.id] = tuple(record["reasons"])
        missed[pack] |= differences(own_record_input(call.patient), record["input"])
    assert abstains == ABSTAINS
    assert not_decided == {pack: set(pinned) for pack, pinned in NOT_DECIDED.items()}
    assert missed == {pack: set(pinned) for pack, pinned in MISSED.items()}
    reasons = {
        reason for pinned in ABSTAINS.values() for kept in pinned.values() for reason in kept
    }
    assert reasons <= set(WHY_ABSTAINED)  # every reason says why
    assert all(
        why for pinned in (*NOT_DECIDED.values(), *MISSED.values()) for why in pinned.values()
    )


def test_the_profiles_site_diagnosis_codes_are_read_through_a_site_code_map_and_never_automated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trial patients' Conditions through `MENDARI_SITE_CODES` (LP-23i), as the sandbox's
    client calls the hook: no shipped pack declares a code set yet, so the prescreen fixture at
    spec 0.3 reads them, its `t2d` set over ICD-10-CM, and the map translates the profile's
    fictional site diagnosis code for type 2 diabetes into `E11.9`. Sent as the profile has them
    (the site's coding and ICD-10-CM), the pack's own code wins and every decision is as without
    a map, the map's version and digest recorded. Sent as a site that codes with its own system
    only (the ICD-10-CM coding left out), the diagnosis is read through the map, and what would
    automate goes to a person instead (`site_code`). A patient with a linked note still abstains
    on it, map or not."""
    site_pack(tmp_path)
    site_code, _ = SITE_DIAGNOSES["E11.9"]
    monkeypatch.setenv(
        SITE_CODES_ENV,
        json.dumps(
            mapped({"t2d": [{"system": SITE_DIAGNOSIS_SYSTEM, "code": site_code, "as": "E11.9"}]})
        ),
    )
    monkeypatch.setattr(clock, "today", lambda: TODAY)
    trial = [p for p in ehr_profile.patients(TODAY) if p.pack_id == TRIAL]
    as_sent = ehr_profile.resources(TODAY)
    site_only = copy.deepcopy(as_sent)
    for resource in site_only:
        if resource["resourceType"] == "Condition":
            codings = resource["code"]["coding"]
            resource["code"]["coding"] = [c for c in codings if c["system"] != ICD10]
    note = ("could not read notes: not_fetched",)
    translated = "site code translated (t2d): a person decides"
    decided = {
        name: _prescreened(tmp_path / name, monkeypatch, resources, trial)
        for name, resources in (("as-sent", as_sent), ("site-only", site_only))
    }
    stamps = {
        record["site_codes"]["version"] for found in decided.values() for record in found.values()
    }
    assert stamps == {VERSION}  # every decision names the map it read with
    pinned = {
        name: {
            patient: (record["outcome"], record["recommendation"], translated in record["reasons"])
            for patient, record in found.items()
        }
        for name, found in decided.items()
    }
    assert pinned == {
        "as-sent": {
            "ehr-trial-1": ("ABSTAIN", None, False),
            "ehr-trial-2": ("ABSTAIN", None, False),
            "ehr-trial-3": ("ABSTAIN", None, False),
            "ehr-trial-4": ("AUTO", "not_eligible", False),  # HbA1c out of range
            "ehr-trial-5": ("AUTO", "candidate", False),
        },
        "site-only": {
            "ehr-trial-1": ("ABSTAIN", None, False),
            "ehr-trial-2": ("ABSTAIN", None, False),
            "ehr-trial-3": ("ABSTAIN", None, False),
            "ehr-trial-4": ("HUMAN_REVIEW", "not_eligible", True),
            "ehr-trial-5": ("HUMAN_REVIEW", "candidate", True),
        },
    }
    for found in decided.values():
        for patient in ("ehr-trial-1", "ehr-trial-2", "ehr-trial-3"):
            assert tuple(found[patient]["reasons"]) == note, patient
    # Without the map: the profile as sent is decided the same, and the site's own code alone
    # is not read as the pack's (the diagnosis unknown), so nothing is translated.
    monkeypatch.delenv(SITE_CODES_ENV)
    unmapped = {
        name: _prescreened(tmp_path / f"{name}-unmapped", monkeypatch, resources, trial)
        for name, resources in (("as-sent", as_sent), ("site-only", site_only))
    }
    assert all(r["site_codes"] is None for found in unmapped.values() for r in found.values())
    same = lambda r: (r["outcome"], r["recommendation"], r["reasons"])  # noqa: E731
    assert {p: same(r) for p, r in unmapped["as-sent"].items()} == {
        p: same(r) for p, r in decided["as-sent"].items()
    }
    alone = {p: (r["outcome"], r["recommendation"]) for p, r in unmapped["site-only"].items()}
    assert (alone["ehr-trial-4"], alone["ehr-trial-5"]) == (
        ("AUTO", "not_eligible"),  # the HbA1c alone rules it out
        (
            "AUTO",
            "needs_review",
        ),  # the diagnosis unknown: a chart review, which this pack automates
    )


def _prescreened(
    folder: Path,
    monkeypatch: pytest.MonkeyPatch,
    resources: list[dict[str, Any]],
    patients: list[ProfilePatient],
) -> dict[str, dict[str, Any]]:
    """Each patient's decision record by the prescreen fixture's hook, called with no prefetch
    on an engine whose FHIR reads go to a server holding `resources`."""
    allow_ehr(monkeypatch, profile_server(resources))
    folder.mkdir()
    db = folder / "prescreen.db"
    packs = [PACKS_DIR, folder.parent / "packs"]
    found: dict[str, dict[str, Any]] = {}
    with TestClient(create_app(db_path=db, packs_paths=packs)) as http, AuditStore(db) as store:
        for patient in patients:
            before = {d["id"] for d in store.query(limit=1000)}
            body = cds_client.hook_request("patient-view", patient, BASE)
            answer = http.post(PRESCREEN_SERVICE, json=body)
            assert answer.status_code == 200, (patient.id, answer.text)
            (made,) = [d["id"] for d in store.query(limit=1000) if d["id"] not in before]
            stored = store.get(made)
            assert stored is not None
            found[patient.id] = stored["record"]
    return found


def own_record_input(patient: ProfilePatient) -> dict[str, Any]:
    """What the pack reads from the patient's case as its own US Core record (`fhir_records`,
    which the pack reads as its JSON record says), with the profile's left-out notes left out too,
    and a trial local site's fictional condition code as the ICD-10-CM code the profile gives it."""
    entries = ehr_profile.own_entries(patient, TODAY)
    bundle = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{"fullUrl": e["fullUrl"], "resource": e["resource"]} for e in entries],
    }
    adapter = FhirAdapter.for_pack(resolve_pack(patient.pack_id))
    read: dict[str, Any] = adapter.to_input(adapter.index(bundle))
    for condition in read.get("conditions", []):
        condition["code"] = _as_icd10(condition["code"])
    return read


def _as_icd10(code: str) -> str:
    """A trial local site's fictional condition code as the ICD-10-CM code it stands for (the
    first of its condition's, as the profile turns it); any other code as it is."""
    events = [event for event, codes in LOCAL.items() if code in codes]
    return ICD[events[0]][0] if events else code


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
    """The shapes the module says, built the same on every run: notes as links to a Binary in
    each of text/plain, text/html and text/rtf; Conditions coded by the site and in ICD-10-CM,
    in both orders; MedicationRequests naming an RxNorm Medication, with a dosage `boundsPeriod`
    and an `authoredOn` before its start (so a mapping's start rule shows in what it reads); the
    payer by reference to an Organization holding the site's identifier, and the site's member
    id; referrals under the site's order type, in both orders. Every id is the profile's, every
    reference resolves in it, no resource carries the sandbox tag or a synthetic case id (in any
    value, a note's text included), and it conforms to US Core."""
    assert ehr_profile.bundle(TODAY) == ehr_profile.bundle(TODAY)
    held = ehr_profile.by_reference(TODAY)
    resources = list(held.values())
    assert len(held) == len(resources) == len(ehr_profile.resources(TODAY))
    assert all(r["id"].startswith("ehr-") for r in resources)
    cases = {case for spec in ehr_profile.PACKS for case in spec.cases}
    values = [v for r in resources for v in _strings(r)]
    values += [_text(b) for b in resources if b["resourceType"] == "Binary"]
    assert not [v for v in values if any(case in v for case in cases)]
    for resource in resources:
        for reference in _references(resource):
            assert reference in held, (resource["id"], reference)
        assert SANDBOX_TAG not in resource.get("meta", {}).get("tag", [])
        identifiers = {i.get("system") for i in resource.get("identifier", [])}
        assert CASE_SYSTEM not in identifiers
    of = lambda kind: [r for r in resources if r["resourceType"] == kind]  # noqa: E731
    assert not of("MedicationStatement")
    for patient in ehr_profile.patients(TODAY):
        assert patient.patient["identifier"] == [{"system": SITE_MRN, "value": patient.id.upper()}]
        own = f"Patient/{patient.id}"
        assert all(
            ref == own for r in patient.resources for ref in _references(r) if "Patient/" in ref
        )
    kinds = set()
    for document in of("DocumentReference"):
        (content,) = document["content"]
        attachment = content["attachment"]
        assert set(attachment) == {"contentType", "url"}, document["id"]  # a link, no data
        binary = held[attachment["url"]]
        assert binary["resourceType"] == "Binary"
        assert binary["contentType"] == attachment["contentType"]
        kinds.add(binary["contentType"])
    assert kinds == set(ehr_profile.NOTE_KINDS) == {"text/plain", "text/html", "text/rtf"}
    rtf = [b for b in of("Binary") if b["contentType"] == "text/rtf"]
    assert rtf and all(_text(b).startswith("{\\rtf1") for b in rtf)
    orders = set()
    for condition in of("Condition"):
        systems = tuple(c["system"] for c in condition["code"]["coding"])
        assert sorted(systems) == sorted((SITE_DIAGNOSIS_SYSTEM, ICD10)), condition["id"]
        orders.add(systems)
    assert len(orders) == 2  # the site's coding first and second
    requests = of("MedicationRequest")
    assert requests and len(requests) == len(of("Medication"))
    for request in requests:
        medication = held[request["medicationReference"]["reference"]]
        assert [c["system"] for c in medication["code"]["coding"]] == [RXNORM]
        assert "medicationCodeableConcept" not in request
        start = request["dosageInstruction"][0]["timing"]["repeat"]["boundsPeriod"]["start"]
        assert request["authoredOn"] < start, request["id"]  # ordered before it starts
    coverages = of("Coverage")
    assert len(coverages) == 5
    for coverage in coverages:
        (payor,) = coverage["payor"]
        assert set(payor) == {"reference", "display"}
        payer = held[payor["reference"]]
        assert [i["system"] for i in payer["identifier"]] == [SITE_PAYER_IDS]
        member = coverage["beneficiary"]["reference"].removeprefix("Patient/").upper()
        assert coverage["subscriberId"] == f"M-{member}"
    referrals = [r for r in of("ServiceRequest") if r.get("category")]
    assert len(referrals) == 5
    orders = set()
    for referral in referrals:
        (category,) = referral["category"]
        systems = tuple(c["system"] for c in category["coding"])
        assert sorted(systems) == sorted((SITE_ORDER_TYPE_SYSTEM, REFERRAL_SYSTEM))
        orders.add(systems)
    assert len(orders) == 2
    assert us_core.validate(resources) == []
    assert fhir_structure.check(ehr_profile.bundle(TODAY)) == []


def test_the_profile_carries_no_snomed_ct(tmp_path: Path) -> None:
    """Through scripts/check_snomed.py, as one of Mendari's own files (where any number written as
    a SNOMED CT concept id counts): the profile as `mendari sandbox seed --pack ehr-profile`
    posts it, and the module that builds it. The same check finds a SNOMED CT coding put in."""
    folder = tmp_path / "mendari" / "sandbox"
    folder.mkdir(parents=True)
    text = json.dumps(ehr_profile.bundle(TODAY), indent=2)
    (folder / "ehr-profile.json").write_text(text, encoding="utf-8")
    module = ROOT / "src" / "mendari" / "sandbox" / "ehr_profile.py"
    (folder / "ehr_profile.py").write_text(module.read_text(encoding="utf-8"), encoding="utf-8")
    script = str(ROOT / "scripts" / "check_snomed.py")
    clean = subprocess.run([sys.executable, script, str(tmp_path)], capture_output=True, text=True)
    assert clean.returncode == 0, clean.stdout
    assert "no SNOMED CT content" in clean.stdout
    coded = text.replace(SITE_DIAGNOSIS_SYSTEM, "http://snomed.info/sct", 1)
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


def _strings(value: Any) -> list[str]:
    """Every string in a resource, at any depth."""
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings(item)]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    return [value] if isinstance(value, str) else []


def _text(binary: Mapping[str, Any]) -> str:
    return base64.b64decode(binary["data"]).decode("utf-8")
