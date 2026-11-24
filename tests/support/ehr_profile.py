# SPDX-License-Identifier: Apache-2.0
"""A vendor-shaped synthetic EHR profile (LP-23e): patients for the three fixture agents with a
standard CDS hook (the Clinical Trial Prescreener's `patient-view`, the Prior Authorization
Preflight's and the Referral Router's `order-sign`), as FHIR R4 in the shapes an EHR's FHIR API
returns. The shapes follow Epic's and Oracle Health's public FHIR documentation; no vendor's
data is in it, and every patient is synthetic.

Each patient is one case of an agent's local-site example (`tests/fixtures/datasets/`, the same
cases every time, `PACKS`), built as US Core resources by `fhir_records` for the day asked (so
its dates sit where the case's do), then reshaped:

- **Notes are links.** A DocumentReference's attachment holds only `url`, `Binary/<id>`, and the
  Binary holds the text: as text/plain, text/html or text/rtf, one kind a case in turn. RTF is
  there to be unreadable: an agent is never to read it as text. Two cases of the trial and
  prior-authorization agents have no note.
- **Several codings.** A Condition carries the site's own diagnosis code (`SITE_DIAGNOSES`, a
  fictional system) and ICD-10-CM; a fictional condition code of the trial's local site becomes
  its ICD-10-CM code. A referral's category carries the site's order type (`SITE_ORDER_TYPES`)
  and the fictional referral code. The site's coding comes first in every other case and second
  in the rest: R4 gives the order of codings no meaning, so an EHR may send either.
- **MedicationRequest.** A medication is a MedicationRequest (US Core profiles it, and not
  MedicationStatement) naming a Medication by reference, coded in RxNorm (`RXNORM_CODES`), its
  start and stop a dosage `boundsPeriod`, ordered by the profile's one Practitioner. Its
  `authoredOn`, the day it was ordered, is a week before it starts (`ORDERED_BEFORE`), as an
  order is written before it is taken: a mapping that read the start from `authoredOn` instead
  of `boundsPeriod.start` (LP-23g's rule) would read other dates than the case's.
- **The payer by reference.** `Coverage.payor` is a reference to an Organization that holds the
  site's identifier for the payer (`PAYERS`), not the agent's payer label, and `subscriberId` is
  the site's member id for the patient (`M-` and its record number), not the case's id.
- **The profile's own ids.** Every resource has an id of the profile (`ehr-<agent>-<n>`), so the
  transaction (`bundle`) PUTs them under those ids, a FHIR server holds them there, and a CDS
  client names them. Patients carry the site's medical record number, and no resource carries
  a synthetic case id or the sandbox tag, as an EHR's would not: the sandbox's tagged
  queries (the worker, the SMART launch) and the Bulk check's labelled cases never see them.
- No SNOMED CT anywhere: the codes are ICD-10-CM, LOINC, RxNorm, ICD-10-PCS (the payer codes
  the prior-authorization orders carry) and fictional systems named as such.

What each agent cannot read in these shapes yet is pinned in tests/test_ehr_profile.py; LP-23k
took entries off it when the agents moved to spec 0.3 (an RTF note and the site's own payer id
are left).

It is test support, outside the product: the product's sandbox runs on the bundled demo agent,
which reads a portal message. `tests/support/sandbox.py` seeds the profile and calls the
agents' hooks on it (CI's `sandbox-e2e`).
"""

from __future__ import annotations

import base64
import copy
import html
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import cache
from typing import Any

from mendari.errors import MendariError
from mendari.sandbox import example_datasets_dir
from mendari.sandbox.code_systems import (
    REFERRAL,
    SITE_DIAGNOSES,
    SITE_DIAGNOSIS_SYSTEM,
    SITE_ORDER_TYPE_SYSTEM,
    SITE_ORDER_TYPES,
)
from mendari.sandbox.fhir_records import US_CORE
from mendari.sandbox.synthetic_trial import CONDITION_SYSTEM, ICD, LOCAL
from mendari.standards.record_fhir import ICD10, RXNORM
from tests.support.fhir_records import prior_auth_entries, referral_entries, trial_entries

TRIAL = "mendari/clinical-trial-prescreener"
PRIOR_AUTH = "mendari/prior-auth-preflight"
REFERRAL_PACK = "mendari/referral-router"
# The fictional EHR the profile is shaped as: its resources' fullUrls are under this base.
BASE = "https://ehr.example.org/fhir"
# The site's own identifier systems, fictional. Its two code systems, and each ICD-10-CM code's
# site diagnosis code, are the sandbox's (`mendari.sandbox.code_systems`), published to the HL7
# validator with the sandbox's others (`terminology.sandbox_resources`).
SITE_MRN = "https://mendari.co/fhir/sid/fictional-site-mrn"
SITE_PAYER_IDS = "https://mendari.co/fhir/sid/fictional-site-payers"
SITE_PROVIDER_IDS = "https://mendari.co/fhir/sid/fictional-site-providers"
REFERRAL_ORDER_TYPE = {
    "system": SITE_ORDER_TYPE_SYSTEM,
    "code": "OT20",
    "display": SITE_ORDER_TYPES["OT20"],
}
# The trial cases' medications (their names, as the cases write them) -> RxNorm ingredient code
# and name (NLM's public-domain RxNorm; NOTICE).
RXNORM_CODES = {
    "metformin 1000 mg tablet": ("6809", "metformin"),
    "Glucophage 500 mg tablet": ("6809", "metformin"),
    "insulin glargine 100 units/mL": ("274783", "insulin glargine"),
}
# The prior-authorization cases' payers (the agent's labels) -> the site's identifier for each
# and the Organization's name.
PAYERS = {
    "acme": ("SP100", "Acme Health (synthetic payer)"),
    "beacon": ("SP200", "Beacon Health (synthetic payer)"),
}
NOTE_KINDS = ("text/plain", "text/html", "text/rtf")
PRESCRIBER_ID = "ehr-prescriber"
ORDERED_BEFORE = timedelta(days=7)  # a MedicationRequest's authoredOn, before its start
# A case's resource type -> the one the EHR sends in its place.
_AS_SENT = {"MedicationStatement": "MedicationRequest"}


@dataclass(frozen=True)
class ProfilePack:
    """One pack's patients: the local-site cases they are made from, in order, and how a case
    is built as US Core resources."""

    pack_id: str
    short: str  # in its patients' ids: `ehr-<short>-<n>`
    dataset: str
    cases: tuple[str, ...]
    build: Callable[[dict[str, Any], date], list[dict[str, Any]]]
    notes_left_out: tuple[int, ...] = ()  # the positions of the cases that get no note


PACKS = (
    ProfilePack(
        TRIAL,
        "trial",
        "trial-local-site-example.jsonl",
        (
            "trial-local-0000",
            "trial-local-0001",
            "trial-local-0002",
            "trial-local-0003",
            "trial-local-0006",
        ),
        lambda record, today: trial_entries(record, today=today),
        notes_left_out=(3, 4),
    ),
    ProfilePack(
        PRIOR_AUTH,
        "pa",
        "prior-auth-local-site-example.jsonl",
        ("pa-local-0002", "pa-local-0007", "pa-local-0012", "pa-local-0031", "pa-local-0047"),
        lambda record, today: prior_auth_entries(record, today=today),
        notes_left_out=(3, 4),
    ),
    ProfilePack(
        REFERRAL_PACK,
        "ref",
        "referral-local-site-example.jsonl",
        (
            "ref-local-00000",
            "ref-local-00011",
            "ref-local-00001",
            "ref-local-00006",
            "ref-local-00002",
        ),
        lambda record, today: referral_entries(record, today=today),
    ),
)


@dataclass(frozen=True)
class ProfilePatient:
    """One patient of the profile: the pack it is for, its id, the case it was made from, and
    its resources (the Patient first, each resource after those it refers to)."""

    pack_id: str
    id: str
    case: str
    resources: tuple[dict[str, Any], ...]

    @property
    def patient(self) -> dict[str, Any]:
        return self.resources[0]

    def orders(self) -> list[dict[str, Any]]:
        """Its orders (ServiceRequests): what an order hook's `draftOrders` hold."""
        return [r for r in self.resources if r["resourceType"] == "ServiceRequest"]


def patients(today: date | None = None, pack_id: str | None = None) -> list[ProfilePatient]:
    """The profile's patients for `today` (default: today, UTC), of one pack or of all three,
    in `PACKS` order."""
    today = today or datetime.now(UTC).date()
    chosen = [spec for spec in PACKS if pack_id is None or spec.pack_id == pack_id]
    if not chosen:
        raise MendariError(f"the EHR profile has patients for {[p.pack_id for p in PACKS]}")
    return [patient for spec in chosen for patient in _pack_patients(spec, today)]


def shared() -> list[dict[str, Any]]:
    """The resources the patients' resources refer to and no patient owns: the Practitioner who
    orders the medications and the payers' Organizations."""
    prescriber = {
        "resourceType": "Practitioner",
        "id": PRESCRIBER_ID,
        "meta": {"profile": [f"{US_CORE}us-core-practitioner"]},
        "identifier": [{"system": SITE_PROVIDER_IDS, "value": "P1001"}],
        "name": [{"family": "Synthetic", "given": ["Prescriber"]}],
    }
    payers = [
        {
            "resourceType": "Organization",
            "id": _payer_id(label),
            "identifier": [{"system": SITE_PAYER_IDS, "value": site_id}],
            "active": True,
            "name": name,
        }
        for label, (site_id, name) in PAYERS.items()
    ]
    return [prescriber, *payers]


def own_entries(patient: ProfilePatient, today: date | None = None) -> list[dict[str, Any]]:
    """The patient's case as its own US Core entries (`fhir_records`, as a pack reads its JSON
    record), before the reshaping, without the note the profile leaves out: what the patient is
    made from, for comparing what a pack reads from each."""
    today = today or datetime.now(UTC).date()
    spec = next(s for s in PACKS if s.pack_id == patient.pack_id)
    entries = spec.build(_records(spec.dataset)[patient.case], today)
    if spec.cases.index(patient.case) in spec.notes_left_out:
        entries = [e for e in entries if e["resource"]["resourceType"] != "DocumentReference"]
    return entries


def resources(today: date | None = None) -> list[dict[str, Any]]:
    """Every resource of the profile: the shared ones, then each patient's."""
    return [*shared(), *(r for p in patients(today) for r in p.resources)]


def bundle(today: date | None = None) -> dict[str, Any]:
    """The profile as one transaction that PUTs each resource under its own id (so a server
    holds it there, and seeding again replaces it), with fullUrls under `BASE`."""
    return {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [
            {
                "fullUrl": f"{BASE}/{r['resourceType']}/{r['id']}",
                "resource": r,
                "request": {"method": "PUT", "url": f"{r['resourceType']}/{r['id']}"},
            }
            for r in resources(today)
        ],
    }


# -- building a pack's patients ------------------------------------------------------------------


def _pack_patients(spec: ProfilePack, today: date) -> list[ProfilePatient]:
    records = _records(spec.dataset)
    made = []
    for position, case in enumerate(spec.cases):
        record = records.get(case)
        if record is None:
            raise MendariError(f"{spec.dataset} has no case {case} for the EHR profile")
        patient_id = f"ehr-{spec.short}-{position + 1}"
        entries = spec.build(record, today)
        shaped = _Shaper(patient_id, position, spec).reshape(entries)
        made.append(ProfilePatient(spec.pack_id, patient_id, case, tuple(shaped)))
    return made


@cache
def _records(dataset: str) -> dict[str, dict[str, Any]]:
    # Here, not at the top: `standards/terminology.py` imports this module's site codes, and the
    # evaluation harness imports the decision runtime.
    from mendari.evals.harness import load_dataset

    folder = example_datasets_dir()
    if folder is None or not (folder / dataset).is_file():
        raise MendariError(f"the EHR profile is made from {dataset}, which is not installed")
    return {record["id"]: record for record in load_dataset(folder / dataset).records}


class _Shaper:
    """One case's US Core entries reshaped as the EHR sends them (the module's docstring)."""

    def __init__(self, patient_id: str, position: int, spec: ProfilePack):
        self.patient_id = patient_id
        self.site_first = position % 2 == 0
        self.note_kind = NOTE_KINDS[position % len(NOTE_KINDS)]
        self.notes = position not in spec.notes_left_out
        self.counts: Counter[str] = Counter()

    def reshape(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        named = {
            entry["fullUrl"]: self._name(entry["resource"]["resourceType"]) for entry in entries
        }
        shaped: list[dict[str, Any]] = []
        for entry in entries:
            given = _refer_by_id(copy.deepcopy(entry["resource"]), named)
            kind, resource_id = named[entry["fullUrl"]].split("/")
            meta = {k: v for k, v in given.pop("meta", {}).items() if k != "tag"}
            given.pop("resourceType")
            resource = {"resourceType": kind, "id": resource_id, **({"meta": meta} if meta else {})}
            shaped += self._vendor(resource | given)
        return shaped

    def _name(self, kind: str) -> str:
        """The `<type>/<id>` a resource of the case gets: the Patient the patient's id, every
        other resource the next of its type (a MedicationStatement becomes a MedicationRequest)."""
        if kind == "Patient":
            return f"Patient/{self.patient_id}"
        sent = _AS_SENT.get(kind, kind)
        return f"{sent}/{self._id(sent)}"

    def _id(self, kind: str) -> str:
        self.counts[kind] += 1
        return f"{self.patient_id}-{kind.lower()}-{self.counts[kind]}"

    def _vendor(self, resource: dict[str, Any]) -> list[dict[str, Any]]:
        """The resource as the EHR holds it, with the resources it now refers to before it."""
        kind = resource["resourceType"]
        if kind == "Patient":
            resource["identifier"] = [{"system": SITE_MRN, "value": self.patient_id.upper()}]
            resource["name"] = [{"family": "Synthetic", "given": [self.patient_id]}]
            return [resource]
        if kind == "Condition":
            resource["code"] = self._diagnosis(resource["code"])
            return [resource]
        if kind == "MedicationRequest":  # a MedicationStatement of the case
            return self._medication_request(resource)
        if kind == "DocumentReference":
            return self._linked_note(resource) if self.notes else []
        if kind == "Coverage":
            label = resource["payor"][0]["identifier"]["value"]
            if label not in PAYERS:
                raise MendariError(f"the EHR profile has no Organization for payer {label!r}")
            resource["payor"] = [
                {"reference": f"Organization/{_payer_id(label)}", "display": PAYERS[label][1]}
            ]
            resource["subscriberId"] = f"M-{self.patient_id.upper()}"  # not the case's id
            return [resource]
        if kind == "ServiceRequest" and resource.get("category"):  # a referral
            pair = [REFERRAL_ORDER_TYPE, REFERRAL]
            resource["category"] = [{"coding": pair if self.site_first else pair[::-1]}]
            return [resource]
        return [resource]

    def _diagnosis(self, concept: dict[str, Any]) -> dict[str, Any]:
        (coding,) = concept["coding"]
        code = coding["code"]
        icd = code if coding["system"] == ICD10 else _icd_of_fictional(coding)
        if icd not in SITE_DIAGNOSES:
            raise MendariError(f"the EHR profile has no site diagnosis code for {icd}")
        site_code, shown = SITE_DIAGNOSES[icd]
        site = {"system": SITE_DIAGNOSIS_SYSTEM, "code": site_code, "display": shown}
        standard = {"system": ICD10, "code": icd}
        shaped: dict[str, Any] = {
            "coding": [site, standard] if self.site_first else [standard, site]
        }
        if concept.get("text"):
            shaped["text"] = concept["text"]
        return shaped

    def _medication_request(self, statement: dict[str, Any]) -> list[dict[str, Any]]:
        name = statement["medicationCodeableConcept"]["text"]
        if name not in RXNORM_CODES:
            raise MendariError(f"the EHR profile has no RxNorm code for {name!r}")
        code, shown = RXNORM_CODES[name]
        medication_id = self._id("Medication")
        medication = {
            "resourceType": "Medication",
            "id": medication_id,
            "meta": {"profile": [f"{US_CORE}us-core-medication"]},
            "code": {"coding": [{"system": RXNORM, "code": code, "display": shown}], "text": name},
        }
        period = statement.get("effectivePeriod") or {}
        request: dict[str, Any] = {
            "resourceType": "MedicationRequest",
            "id": statement["id"],
            "meta": {"profile": [f"{US_CORE}us-core-medicationrequest"]},
            "status": statement["status"],  # active, completed and stopped are its codes too
            "intent": "order",
            "medicationReference": {"reference": f"Medication/{medication_id}", "display": name},
            "subject": statement["subject"],
            "requester": {"reference": f"Practitioner/{PRESCRIBER_ID}"},
        }
        if period.get("start"):
            ordered = date.fromisoformat(period["start"]) - ORDERED_BEFORE
            request["authoredOn"] = ordered.isoformat()
        if period:
            request["dosageInstruction"] = [{"timing": {"repeat": {"boundsPeriod": period}}}]
        return [medication, request]

    def _linked_note(self, document: dict[str, Any]) -> list[dict[str, Any]]:
        (content,) = document["content"]
        text = base64.b64decode(content["attachment"]["data"]).decode("utf-8")
        binary_id = self._id("Binary")
        binary = {
            "resourceType": "Binary",
            "id": binary_id,
            "contentType": self.note_kind,
            "data": base64.b64encode(_rendered(text, self.note_kind).encode()).decode(),
        }
        document["content"] = [
            {"attachment": {"contentType": self.note_kind, "url": f"Binary/{binary_id}"}}
        ]
        return [binary, document]


def _refer_by_id(value: Any, named: dict[str, str]) -> Any:
    """`value` with every reference to one of the case's fullUrls made the `<type>/<id>` that
    resource now has."""
    if isinstance(value, dict):
        reference = value.get("reference")
        if isinstance(reference, str) and reference in named:
            return {**value, "reference": named[reference]}
        return {key: _refer_by_id(item, named) for key, item in value.items()}
    if isinstance(value, list):
        return [_refer_by_id(item, named) for item in value]
    return value


def _icd_of_fictional(coding: dict[str, Any]) -> str:
    """The ICD-10-CM code a trial local site's fictional condition code stands for: the first of
    its condition's (`synthetic_trial.ICD`)."""
    if coding["system"] != CONDITION_SYSTEM:
        raise MendariError(f"the EHR profile reads no condition coded in {coding['system']}")
    (event,) = [event for event, codes in LOCAL.items() if coding["code"] in codes]
    return ICD[event][0]


def _payer_id(label: str) -> str:
    return f"ehr-payer-{label}"


def _rendered(text: str, kind: str) -> str:
    """A note's text as the Binary holds it."""
    if kind == "text/html":
        return f"<!DOCTYPE html><html><body><p>{html.escape(text)}</p></body></html>"
    if kind == "text/rtf":
        escaped = text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
        escaped = "".join(c if ord(c) < 128 else f"\\u{ord(c)}?" for c in escaped)
        return "{\\rtf1\\ansi\\deff0 {\\fonttbl {\\f0 Arial;}}\\f0 " + escaped + "\\par}"
    return text


def by_reference(today: date | None = None) -> dict[str, dict[str, Any]]:
    """Every resource of the profile by its `<type>/<id>`, as a FHIR server holds it."""
    return {f"{r['resourceType']}/{r['id']}": r for r in resources(today)}
