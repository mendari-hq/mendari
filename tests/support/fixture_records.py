# SPDX-License-Identifier: Apache-2.0
"""The chart-summary test fixture's synthetic patients, and each one's FHIR record.

Synthetic test fixture data. Not clinically reviewed. Not for patient care.

`tests/fixtures/agents/chart-summary` (`mendari-test/chart-summary`, fixture F3 of
wave 6) is a small agent that reads a patient's FHIR record: when the patient should next be
seen (`follow_up`), and whether an anticoagulant is listed (`on_anticoagulant`). This module
makes its data, the same on every run (a fixed seed, fixed phrase lists, no clock read):

- `cases(split)`: the synthetic patients of a dataset (`PLANS`: 120 `test`, 12 `examples`), as
  facts (`Case`). Each case's labels come from its facts by a stated rule (`Case.labels`), never
  from a provider: `soon` when a condition on the synthetic watch list is coded, else `routine`
  for a long-term condition, else `none`; `yes` when an anticoagulant is listed. A test may also
  write a `Case` of its own.
- `Case.record()`: the dataset line, `{id, input, labels}`, whose `input` is exactly what the
  agent's FHIR mapping (`fhir/mapping.yaml`) reads from the case's record on `TODAY`.
- `Case.bundle()`: the case's US Core 6.1.0 resources as a collection Bundle: the Patient (its
  case id as an identifier, `CASE_SYSTEM`), its Conditions (ICD-10-CM), its medications as
  MedicationStatements and MedicationRequests (RxNorm; a request names its drug through a
  Medication it contains, one beside it in the record, or its own concept, with a dosage
  period), its clinic notes as DocumentReferences (plain text or HTML, inline or in a Binary
  beside it), an order on file (a ServiceRequest) and the Practitioner who wrote the requests.
  Every resource has an id and a `fullUrl` under `BASE`, refers to the others by `Type/id`, and
  carries the sandbox's synthetic tag. The datasets' cases keep everything a patient's chart
  needs inside the resources that name the patient (contained drugs, inline notes), as a Bulk
  export joins them; `SHOWCASE` and the vendor-shaped server (`tests/support/chart_ehr.py`)
  have drugs and notes beside them, by reference.
- `site="beside"` puts a fictional site's own code first among a condition's, a drug's and the
  order's codings: the agent still reads the standard one, by its `[system=...]` predicate.
  `site="only"` keeps the site's code alone: the agent reads a condition's (into its code set
  `follow_up_conditions`) and the order's (into its order-sign twin's) through a site code map
  (`site_code_map`), and a drug's not at all.
- `site_code_map` and `ehr_declaration`: a deployment's `MENDARI_SITE_CODES` and
  `MENDARI_EHR_DECLARATIONS` for these records. The site's code systems (`SITE_*`) are fictional
  test URLs declared here, never in the product.

Codes: conditions are ICD-10-CM and drugs RxNorm ingredients. US Core also needs a note's type
(LOINC's progress note), HL7's category codes, and a code for the order: ICD-10-PCS, the public
domain procedure codes CMS publishes, as the sandbox's synthetic orders carry (US Core binds an
order's code to procedure codes, and SNOMED CT and CPT are in no fixture). No SNOMED CT anywhere.

    uv run python -m tests.support.fixture_records          # write the files below
    uv run python -m tests.support.fixture_records --check  # exit 1 if a committed one differs

It writes the agent's datasets (`data/examples.jsonl`, `data/test.jsonl`) and, for
`scripts/fhir_validate.py`, `tests/fixtures/us-core/chart.json` (one record, valid) and
`tests/fixtures/us-core/invalid/chart.json` (resources made invalid on purpose, each breaking
one rule the in-process checks hold); then the agent's `DECISION_CARD.md`, as `mendari card
--write` renders it, since the agent's digest (which its datasets are part of) is on it.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import random
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from functools import cache
from pathlib import Path
from typing import Any, Literal

from mendari.core.agent import load_agent
from mendari.core.card import card_of
from mendari.sandbox.code_systems import CASE_SYSTEM, SANDBOX_TAG
from mendari.sandbox.fhir_records import meta, patient

ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT / "tests" / "fixtures" / "agents" / "chart-summary"
CARD = AGENT / "DECISION_CARD.md"
US_CORE_DIR = ROOT / "tests" / "fixtures" / "us-core"
AGENT_ID = "mendari-test/chart-summary"
SEED = "mendari-test/chart-summary"
# The day the records are built for: a case's age is its age on this day, and the agent reads the
# same age from the birth date when it decides on this day (`clock.today`).
TODAY = date(2026, 10, 17)
# The FHIR server the records are said to come from: each resource's fullUrl is under it, and a
# vendor-shaped server of these records (`tests/support/chart_ehr.py`) answers under it.
BASE = "https://ehr.example.org/fhir"

ICD10CM = "http://hl7.org/fhir/sid/icd-10-cm"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
LOINC = "http://loinc.org"
ICD10PCS = "http://www.cms.gov/Medicare/Coding/ICD10"
UCUM = "http://unitsofmeasure.org"
TERMINOLOGY = "http://terminology.hl7.org/CodeSystem"
US_CORE = "http://hl7.org/fhir/us/core/StructureDefinition/"
# A fictional site's own code systems (test URLs, `.example` being reserved): its formulary, its
# diagnosis codes and its order codes. Never in the product's terminology.
SITE_DRUGS = "https://site.example/fhir/CodeSystem/formulary"
SITE_DIAGNOSES = "https://site.example/fhir/CodeSystem/conditions"
SITE_ORDERS = "https://site.example/fhir/CodeSystem/orders"

SOON, ROUTINE, NONE = "soon", "routine", "none"
# What the agent's mapping writes for a condition of its code set `follow_up_conditions`
# (`fhir/mapping.yaml`'s `map`): one word for each kind, which its canned answers
# (`fixtures/mock.yaml`) look for, as they look for the anticoagulants' RxNorm codes.
FLAG_SOON, FLAG_ROUTINE = "flag-soon", "flag-routine"
CODE_SET = "follow_up_conditions"
ORDER_CODE_SET = "checked_orders"  # the order-sign twin's (`tests/support/chart_fixture.py`)
UNSURE, CANNOT_TELL = "R69", "R68.89"  # codes the canned agent answers unsurely (review, abstain)

# ICD-10-CM code -> (its title, as the record's text; its follow-up: the synthetic watch list
# `soon`, a long-term condition `routine`, None for one that needs no follow-up). Not clinical
# guidance: a rule for test data.
CONDITIONS: dict[str, tuple[str, str | None]] = {
    "I50.9": ("Heart failure, unspecified", SOON),
    "N18.4": ("Chronic kidney disease, stage 4 (severe)", SOON),
    "I26.99": ("Other pulmonary embolism without acute cor pulmonale", SOON),
    "D68.32": ("Hemorrhagic disorder due to extrinsic circulating anticoagulants", SOON),
    "R79.1": ("Abnormal coagulation profile", SOON),
    "E11.9": ("Type 2 diabetes mellitus without complications", ROUTINE),
    "I10": ("Essential (primary) hypertension", ROUTINE),
    "E78.5": ("Hyperlipidemia, unspecified", ROUTINE),
    "I48.91": ("Unspecified atrial fibrillation", ROUTINE),
    "E03.9": ("Hypothyroidism, unspecified", ROUTINE),
    "J45.909": ("Unspecified asthma, uncomplicated", ROUTINE),
    "Z86.718": ("Personal history of other venous thrombosis and embolism", ROUTINE),
    "J06.9": ("Acute upper respiratory infection, unspecified", None),
    "M54.50": ("Low back pain, unspecified", None),
    "L30.9": ("Dermatitis, unspecified", None),
    "R51.9": ("Headache, unspecified", None),
    UNSURE: ("Illness, unspecified", None),
    CANNOT_TELL: ("Other general symptoms and signs", None),
}
WATCH = [code for code, (_, kind) in CONDITIONS.items() if kind == SOON]
LONG_TERM = [code for code, (_, kind) in CONDITIONS.items() if kind == ROUTINE]
OTHER = [
    code
    for code, (_, kind) in CONDITIONS.items()
    if kind is None and code not in (UNSURE, CANNOT_TELL)
]
# RxNorm ingredient code -> (its name, as the chart names the product; an anticoagulant).
MEDICATIONS: dict[str, tuple[str, str, bool]] = {
    "11289": ("warfarin", "warfarin 5 mg tablet", True),
    "1364430": ("apixaban", "apixaban 5 mg tablet", True),
    "1114195": ("rivaroxaban", "rivaroxaban 20 mg tablet", True),
    "1599538": ("edoxaban", "edoxaban 60 mg tablet", True),
    "6809": ("metformin", "metformin 500 mg tablet", False),
    "29046": ("lisinopril", "lisinopril 10 mg tablet", False),
    "83367": ("atorvastatin", "atorvastatin 20 mg tablet", False),
    "10582": ("levothyroxine", "levothyroxine 50 mcg tablet", False),
    "17767": ("amlodipine", "amlodipine 5 mg tablet", False),
    "7646": ("omeprazole", "omeprazole 20 mg capsule", False),
    "435": ("albuterol", "albuterol inhaler", False),
    "1191": ("aspirin", "aspirin 81 mg tablet", False),  # an antiplatelet drug, not one
    "32968": ("clopidogrel", "clopidogrel 75 mg tablet", False),  # likewise
}
ANTICOAGULANTS = [code for code, (_, _, anticoagulant) in MEDICATIONS.items() if anticoagulant]
OTHER_DRUGS = [code for code, (_, _, anticoagulant) in MEDICATIONS.items() if not anticoagulant]
ANTIPLATELETS = ("1191", "32968")
# ICD-10-PCS code -> (its title, as CMS writes it; the order's text in the record).
ORDERS: dict[str, tuple[str, str]] = {
    "0DJD8ZZ": (
        "Inspection of Lower Intestinal Tract, Via Natural or Artificial Opening Endoscopic",
        "Colonoscopy",
    ),
    "0DJ08ZZ": (
        "Inspection of Upper Intestinal Tract, Via Natural or Artificial Opening Endoscopic",
        "Upper endoscopy",
    ),
}
# The fictional site's own codes, each for one of the agent's: its formulary codes for the
# anticoagulants, its diagnosis codes for some conditions, its order codes. A site code map
# translates the diagnosis codes into the agent's code set (`site_code_map`), and the order codes
# into its order-sign twin's (`order_site_codes`); the formulary codes stand for a drug the
# agent reads only by its RxNorm code, so a drug coded only in them is not found.
SITE_DRUG_CODES = {
    "11289": "FRM-2210",
    "1364430": "FRM-2211",
    "1114195": "FRM-2212",
    "1599538": "FRM-2213",
}
SITE_CONDITION_CODES = {
    "I50.9": "DX-4471",
    "N18.4": "DX-4472",
    "D68.32": "DX-4473",
    "E11.9": "DX-1001",
    "I10": "DX-1002",
    "I48.91": "DX-1003",
}
SITE_ORDER_CODES = {"0DJD8ZZ": "ORD-31", "0DJ08ZZ": "ORD-32"}

# A US Core DocumentReference's type and category: LOINC's progress note, a clinical note.
PROGRESS_NOTE = {"coding": [{"system": LOINC, "code": "11506-3", "display": "Progress note"}]}
CLINICAL_NOTE = {
    "system": "http://hl7.org/fhir/us/core/CodeSystem/us-core-documentreference-category",
    "code": "clinical-note",
}
PROBLEM_LIST = {"system": f"{TERMINOLOGY}/condition-category", "code": "problem-list-item"}
ACTIVE = {"system": f"{TERMINOLOGY}/condition-clinical", "code": "active"}
PRESCRIBER = "chart-prescriber"  # the one Practitioner every record's requests name

Form = Literal["plain", "html", "binary"]
Site = Literal["beside", "only"] | None

# The clinic notes' sentences, which the agent's `rules` variant reads (`logic/rules.yaml`).
# Each follow-up has words the rules find, and the watch list also has words they miss, on purpose:
# an evaluation of the rules finds those cases, and none of them is automated.
OPENINGS = (
    "Seen in clinic for a review of the chart.",
    "Reviewed at the clinic today.",
    "Seen today with a family member.",
    "Clinic review of medicines and problems.",
)
SOON_SAID = (
    "Follow-up within a week.",
    "Asked to come back this week to recheck.",
    "Needs a visit within a few days.",
)
SOON_UNSAID = (  # the rules find no follow-up words here
    "Would like to see the patient again before the weekend.",
    "Plan to recheck shortly.",
)
ROUTINE_SAID = (
    "Routine follow-up in three months.",
    "Stable. Follow-up in six months.",
    "Review at the yearly visit.",
)
NONE_SAID = (
    "No follow-up needed.",
    "Return as needed.",
)
TAKES = ("Takes {drug} every day.", "Remains on {drug}.")
NOT_ON = ("Not on warfarin.", "No anticoagulant at present.")


@dataclass(frozen=True)
class Condition:
    """A coded condition on the problem list: its ICD-10-CM code and onset date."""

    code: str
    onset: str


@dataclass(frozen=True)
class Medication:
    """A listed medication: its RxNorm ingredient code, and how the record keeps it. A statement
    is a MedicationStatement (`start`, `end` its effective period). A request is a
    MedicationRequest that names the drug by reference to a Medication it contains
    (`drug="contained"`, `#id`), to one beside it in the record (`"reference"`,
    `Medication/<id>`), or by its own concept (`"concept"`). Its period is its first dosage
    instruction's `boundsPeriod` (`period="bounds"`, written a week before `start`), a
    `boundsDuration` of `days` from when it was written (`"duration"`, written on `start`), or
    only when it was written (`"written"`: its end is unknown).

    The datasets' cases keep the drug inside the request (contained, or its concept): a Bulk
    export joins a patient's resources by their subject, which a Medication has not."""

    code: str
    start: str
    end: str | None = None
    source: Literal["statement", "request"] = "statement"
    drug: Literal["contained", "reference", "concept"] = "contained"
    period: Literal["bounds", "duration", "written"] = "bounds"
    days: int | None = None

    @property
    def stop(self) -> str | None:
        """When the drug stops, as the agent's mapping derives it (`medication_period`)."""
        if self.source == "request" and self.period == "duration":
            assert self.days is not None
            return (date.fromisoformat(self.start) + timedelta(days=self.days)).isoformat()
        if self.source == "request" and self.period == "written":
            return None
        return self.end

    @property
    def status(self) -> str:
        """Its status: a statement that ended is `completed`; anything else is `active`."""
        return "completed" if self.source == "statement" and self.end is not None else "active"


@dataclass(frozen=True)
class Note:
    """A clinic note: its sentences, and how the record keeps it: plain text or HTML inline, or
    plain text in a Binary the attachment's url names (beside it in the record). The datasets'
    cases keep their notes inline: a Bulk export joins a patient's resources by their subject,
    which a Binary has not."""

    sentences: tuple[str, ...]
    form: Form = "plain"
    written: str = "2026-10-01"

    @property
    def text(self) -> str:
        """The note as the agent reads it (`attachment_text`): HTML a block a line."""
        return ("\n" if self.form == "html" else " ").join(self.sentences)


@dataclass(frozen=True)
class Case:
    """One synthetic patient and their chart."""

    id: str
    age: int
    sex: str
    conditions: tuple[Condition, ...] = ()
    medications: tuple[Medication, ...] = ()
    notes: tuple[Note, ...] = ()
    order: str = "0DJD8ZZ"
    written: str = "2026-10-01"  # when the order was written
    # A lab result the agent does not read (an INR, `{INR}`), to keep the chart a chart: the
    # showcase record's only.
    inr: float | None = None

    @property
    def labels(self) -> dict[str, str]:
        """The truth, from the facts alone: `follow_up` is `soon` for a condition on the watch
        list, else `routine` for a long-term condition, else `none`; `on_anticoagulant` is `yes`
        when an anticoagulant is listed, in any status."""
        kinds = {CONDITIONS[c.code][1] for c in self.conditions}
        follow_up = SOON if SOON in kinds else ROUTINE if ROUTINE in kinds else NONE
        listed = any(MEDICATIONS[m.code][2] for m in self.medications)
        return {"follow_up": follow_up, "on_anticoagulant": "yes" if listed else "no"}

    def input(self) -> dict[str, Any]:
        """What the agent's FHIR mapping reads from this case's record, on `TODAY`: every list,
        empty ones too; statements before requests, each in the record's order; the problem
        list as text, a condition a line (`join`); the notes joined by a blank line."""
        value: dict[str, Any] = {
            "patient": {"age": self.age, "sex": self.sex},
            "conditions": [_condition_item(c) for c in self.conditions],
            "medications": [
                _medication_item(m)
                for source in ("statement", "request")
                for m in self.medications
                if m.source == source
            ],
        }
        if self.conditions:
            value["problems"] = "\n".join(CONDITIONS[c.code][0] for c in self.conditions)
        if self.notes:
            value["notes"] = "\n\n".join(note.text for note in self.notes)
        return value

    def record(self) -> dict[str, Any]:
        """The case as a line of the agent's datasets."""
        return {"id": self.id, "input": self.input(), "labels": self.labels}

    def entries(self, today: date = TODAY, *, site: Site = None) -> list[dict[str, Any]]:
        """The case's resources as collection Bundle entries, in a fixed order: the Patient, the
        Practitioner, Conditions, MedicationStatements, then each MedicationRequest after the
        Medication it names beside it, notes (a Binary after its DocumentReference), a lab
        result, the order."""
        return [bundle_entry(resource) for resource in self.resources(today, site=site)]

    def bundle(self, today: date = TODAY, *, site: Site = None) -> dict[str, Any]:
        """The case's record as a collection Bundle (`entries`)."""
        return {
            "resourceType": "Bundle",
            "id": self.id,
            "meta": {"tag": [SANDBOX_TAG]},
            "type": "collection",
            "entry": self.entries(today, site=site),
        }

    def resources(self, today: date = TODAY, *, site: Site = None) -> list[dict[str, Any]]:
        """The case's resources (`entries`), each with its id."""
        subject = {"reference": f"Patient/{self.id}"}
        found = [_patient(self, today), _practitioner()]
        found += [_condition(self, n, c, subject, site) for n, c in enumerate(self.conditions, 1)]
        numbered = list(enumerate(self.medications, start=1))
        found += [
            _statement(self, n, m, subject, site) for n, m in numbered if m.source == "statement"
        ]
        for n, m in numbered:
            if m.source != "request":
                continue
            if m.drug == "reference":
                found.append(_medication(self, n, m, site))
            found.append(_request(self, n, m, subject, site))
        for n, note in enumerate(self.notes, start=1):
            found += _note(self, n, note, subject)
        if self.inr is not None:
            found.append(_inr(self, subject))
        found.append(_order(self, subject, site))
        return found


def bundle_entry(resource: dict[str, Any]) -> dict[str, Any]:
    """A collection Bundle's entry: the resource, at its URL under `BASE`."""
    return {"fullUrl": f"{BASE}/{resource['resourceType']}/{resource['id']}", "resource": resource}


def _without_type(resource: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in resource.items() if key != "resourceType"}


def _condition_item(condition: Condition) -> dict[str, Any]:
    text, kind = CONDITIONS[condition.code]
    item: dict[str, Any] = {"code": condition.code}
    if kind is not None:
        item["flag"] = FLAG_SOON if kind == SOON else FLAG_ROUTINE
    item.update({"text": text, "status": "active", "onset": condition.onset})
    return item


def _medication_item(medication: Medication) -> dict[str, Any]:
    _, name, _ = MEDICATIONS[medication.code]
    item: dict[str, Any] = {"code": medication.code, "name": name, "status": medication.status}
    item["start"] = medication.start
    if medication.stop is not None:
        item["end"] = medication.stop
    return item


def born(age: int, today: date) -> str:
    """A birth date that makes the patient `age` on `today` (mid-year, clear of birthdays)."""
    return (today - timedelta(days=round(age * 365.25) + 180)).isoformat()


def tagged(resource: dict[str, Any], profile: str | None = None) -> dict[str, Any]:
    """`resource` with its `meta`: the US Core profile it claims, if any, and the sandbox tag."""
    resource["meta"] = meta(profile) if profile else {"tag": [SANDBOX_TAG]}
    return resource


def _patient(case: Case, today: date) -> dict[str, Any]:
    made = patient(case.id, gender=case.sex, birth_date=born(case.age, today))
    return {"resourceType": "Patient", "id": case.id, **_without_type(made)}


def _practitioner() -> dict[str, Any]:
    """The one prescriber, a US Core Practitioner: US Core asks for an identifier, so it has one
    in the sandbox's own system (never a number that could be a real NPI)."""
    return tagged(
        {
            "resourceType": "Practitioner",
            "id": PRESCRIBER,
            "identifier": [{"system": CASE_SYSTEM, "value": PRESCRIBER}],
            "name": [{"family": "Prescriber", "given": ["Synthetic"]}],
        },
        "us-core-practitioner",
    )


def _coding(system: str, code: str, display: str | None = None) -> dict[str, str]:
    coding = {"system": system, "code": code}
    if display is not None:
        coding["display"] = display
    return coding


def _codings(
    standard: dict[str, str], site_code: str | None, site_system: str, site: Site
) -> list[dict[str, str]]:
    """A concept's codings: the standard one; with `site`, the site's own code first beside it
    (`"beside"`) or in its place (`"only"`), when the site has a code for it."""
    if site is None or site_code is None:
        return [standard]
    own = _coding(site_system, site_code)
    return [own] if site == "only" else [own, standard]


def _condition(
    case: Case, n: int, condition: Condition, subject: dict[str, str], site: Site
) -> dict[str, Any]:
    text, _ = CONDITIONS[condition.code]
    codings = _codings(
        _coding(ICD10CM, condition.code, text),
        SITE_CONDITION_CODES.get(condition.code),
        SITE_DIAGNOSES,
        site,
    )
    return tagged(
        {
            "resourceType": "Condition",
            "id": f"{case.id}-condition-{n}",
            "clinicalStatus": {"coding": [ACTIVE]},
            "category": [{"coding": [PROBLEM_LIST]}],
            "code": {"coding": codings, "text": text},
            "subject": subject,
            "onsetDateTime": condition.onset,
        },
        "us-core-condition-problems-health-concerns",
    )


def _drug(medication: Medication, site: Site) -> dict[str, Any]:
    ingredient, name, _ = MEDICATIONS[medication.code]
    codings = _codings(
        _coding(RXNORM, medication.code, ingredient),
        SITE_DRUG_CODES.get(medication.code),
        SITE_DRUGS,
        site,
    )
    return {"coding": codings, "text": name}


def _statement(
    case: Case, n: int, medication: Medication, subject: dict[str, str], site: Site
) -> dict[str, Any]:
    period = {"start": medication.start}
    if medication.end is not None:
        period["end"] = medication.end
    return tagged(
        {
            "resourceType": "MedicationStatement",
            "id": f"{case.id}-statement-{n}",
            "status": medication.status,
            "medicationCodeableConcept": _drug(medication, site),
            "subject": subject,
            "effectivePeriod": period,
        }
    )


def _medication(case: Case, n: int, medication: Medication, site: Site) -> dict[str, Any]:
    return tagged(
        {"resourceType": "Medication", "id": f"{case.id}-drug-{n}", "code": _drug(medication, site)}
    )


def _request(
    case: Case, n: int, medication: Medication, subject: dict[str, str], site: Site
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "resourceType": "MedicationRequest",
        "id": f"{case.id}-request-{n}",
        "status": "active",
        "intent": "order",
    }
    name = MEDICATIONS[medication.code][1]
    if medication.drug == "contained":
        drug = {"resourceType": "Medication", "id": "drug", "code": _drug(medication, site)}
        request["contained"] = [drug]
        request["medicationReference"] = {"reference": "#drug", "display": name}
    elif medication.drug == "reference":
        reference = {"reference": f"Medication/{case.id}-drug-{n}", "display": name}
        request["medicationReference"] = reference
    else:
        request["medicationCodeableConcept"] = _drug(medication, site)
    start = date.fromisoformat(medication.start)
    request["subject"] = subject
    if medication.period == "bounds":
        request["authoredOn"] = (start - timedelta(days=7)).isoformat()
        bounds = {"start": medication.start}
        if medication.end is not None:
            bounds["end"] = medication.end
        request["dosageInstruction"] = [{"timing": {"repeat": {"boundsPeriod": bounds}}}]
    elif medication.period == "duration":
        request["authoredOn"] = medication.start
        duration = {"value": medication.days, "unit": "days", "system": UCUM, "code": "d"}
        request["dosageInstruction"] = [{"timing": {"repeat": {"boundsDuration": duration}}}]
    else:
        request["authoredOn"] = medication.start
    request["requester"] = {"reference": f"Practitioner/{PRESCRIBER}"}
    return tagged(request, "us-core-medicationrequest")


def _base64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _note(case: Case, n: int, note: Note, subject: dict[str, str]) -> list[dict[str, Any]]:
    """A note's DocumentReference, and the Binary its url names when it is kept in one."""
    reference = f"{case.id}-note-{n}"
    if note.form == "html":
        markup = "".join(f"<p>{html.escape(sentence)}</p>" for sentence in note.sentences)
        attachment = {"contentType": "text/html", "data": _base64(f"<div>{markup}</div>")}
    elif note.form == "binary":
        attachment = {"contentType": "text/plain", "url": f"Binary/{reference}-text"}
    else:
        attachment = {"contentType": "text/plain", "data": _base64(note.text)}
    found = [
        tagged(
            {
                "resourceType": "DocumentReference",
                "id": reference,
                "status": "current",
                "type": PROGRESS_NOTE,
                "category": [{"coding": [CLINICAL_NOTE]}],
                "subject": subject,
                "date": f"{note.written}T12:00:00Z",
                "content": [{"attachment": attachment}],
            },
            "us-core-documentreference",
        )
    ]
    if note.form == "binary":
        binary = {
            "resourceType": "Binary",
            "id": f"{reference}-text",
            "contentType": "text/plain",
            "data": _base64(note.text),
        }
        found.append(tagged(binary))
    return found


def _inr(case: Case, subject: dict[str, str]) -> dict[str, Any]:
    """An INR result (LOINC 6301-6, in UCUM's `{INR}`), which the agent does not read."""
    laboratory = {"system": f"{TERMINOLOGY}/observation-category", "code": "laboratory"}
    return tagged(
        {
            "resourceType": "Observation",
            "id": f"{case.id}-inr",
            "status": "final",
            "category": [{"coding": [laboratory]}],
            "code": {"coding": [_coding(LOINC, "6301-6")], "text": "INR"},
            "subject": subject,
            "effectiveDateTime": case.written,
            "valueQuantity": {"value": case.inr, "unit": "INR", "system": UCUM, "code": "{INR}"},
        },
        "us-core-observation-lab",
    )


def _order(case: Case, subject: dict[str, str], site: Site) -> dict[str, Any]:
    title, text = ORDERS[case.order]
    codings = _codings(
        _coding(ICD10PCS, case.order, title), SITE_ORDER_CODES.get(case.order), SITE_ORDERS, site
    )
    return tagged(
        {
            "resourceType": "ServiceRequest",
            "id": f"{case.id}-order",
            "status": "active",
            "intent": "order",
            "code": {"coding": codings, "text": text},
            "subject": subject,
            "authoredOn": case.written,
            "requester": {"reference": f"Practitioner/{PRESCRIBER}"},
        },
        "us-core-servicerequest",
    )


# -- the synthetic patients ------------------------------------------------------------------

# Each split's cases by (follow-up, anticoagulant listed): how many of each.
PLANS: dict[str, dict[tuple[str, str], int]] = {
    "test": {
        (SOON, "yes"): 14,
        (SOON, "no"): 16,
        (ROUTINE, "yes"): 20,
        (ROUTINE, "no"): 34,
        (NONE, "yes"): 6,
        (NONE, "no"): 30,
    },
    "examples": {
        (SOON, "yes"): 2,
        (SOON, "no"): 1,
        (ROUTINE, "yes"): 2,
        (ROUTINE, "no"): 3,
        (NONE, "yes"): 1,
        (NONE, "no"): 3,
    },
}
# Of the cases with no anticoagulant, how many also carry a code the canned agent is unsure of
# (`UNSURE`: a person reviews) or cannot tell from (`CANNOT_TELL`: it abstains), by follow-up.
UNSURE_CASES = {"test": {ROUTINE: 6, NONE: 3}, "examples": {ROUTINE: 1, NONE: 0}}
CANNOT_TELL_CASES = {"test": {NONE: 5}, "examples": {NONE: 1}}
# The watch-list codes a chart that lists an anticoagulant is most often coded with.
BLEEDING = ("D68.32", "R79.1", "I26.99")


def cases(split: str) -> tuple[Case, ...]:
    """The cases of one of the agent's datasets (`test`, `examples`), the same on every run."""
    return _cases(split)


@cache
def _cases(split: str) -> tuple[Case, ...]:
    rng = random.Random(f"{SEED}/{split}")
    plan = [kind for kind, count in PLANS[split].items() for _ in range(count)]
    rng.shuffle(plan)
    special: dict[int, str] = {}
    for code, counts in ((UNSURE, UNSURE_CASES[split]), (CANNOT_TELL, CANNOT_TELL_CASES[split])):
        for follow_up, count in counts.items():
            free = [
                n
                for n, (kind, anticoagulant) in enumerate(plan)
                if kind == follow_up and anticoagulant == "no" and n not in special
            ]
            for n in rng.sample(free, count):
                special[n] = code
    return tuple(
        _case(f"chart-{split}-{n + 1:03d}", follow_up, anticoagulant == "yes", special.get(n), rng)
        for n, (follow_up, anticoagulant) in enumerate(plan)
    )


def _day(rng: random.Random, first: date, last: date) -> date:
    return first + timedelta(days=rng.randrange((last - first).days + 1))


def _case(
    case_id: str, follow_up: str, anticoagulant: bool, special: str | None, rng: random.Random
) -> Case:
    codes: list[str] = []
    if follow_up == SOON:
        codes.append(rng.choice(BLEEDING if anticoagulant and rng.random() < 0.6 else WATCH))
        codes += rng.sample(LONG_TERM, rng.randint(0, 2))
    elif follow_up == ROUTINE:
        codes += rng.sample(LONG_TERM, rng.randint(1, 3))
    # Every chart codes at least one condition: one with nothing to follow up codes another.
    extra = 1 if follow_up == NONE and special is None else rng.choice((0, 0, 1))
    codes += rng.sample(OTHER, extra)
    if special is not None:
        codes.append(special)
    rng.shuffle(codes)
    conditions = tuple(
        Condition(code, _day(rng, date(2012, 1, 1), date(2026, 8, 31)).isoformat())
        for code in codes
    )
    drugs = rng.sample(OTHER_DRUGS, rng.randint(0, 3))
    if not anticoagulant and not set(drugs) & set(ANTIPLATELETS) and rng.random() < 0.3:
        drugs.append(rng.choice(ANTIPLATELETS))
    if anticoagulant:
        drugs.insert(rng.randint(0, len(drugs)), rng.choice(ANTICOAGULANTS))
    medications = tuple(_listed(code, rng) for code in drugs)
    notes = _notes(follow_up, drugs, rng)
    return Case(
        id=case_id,
        age=rng.randint(25, 89),
        sex=rng.choice(("female", "male", "female", "male", "other")),
        conditions=conditions,
        medications=medications,
        notes=notes,
        order=rng.choice(sorted(ORDERS)),
        written=_day(rng, date(2026, 9, 1), date(2026, 10, 10)).isoformat(),
    )


def _listed(code: str, rng: random.Random) -> Medication:
    start = _day(rng, date(2023, 1, 1), date(2026, 9, 15))
    if rng.random() < 0.55:
        ended = not MEDICATIONS[code][2] and rng.random() < 0.2
        end = (start + timedelta(days=rng.randint(30, 180))).isoformat() if ended else None
        return Medication(code, start.isoformat(), end)
    drug: Literal["contained", "concept"] = "contained" if rng.random() < 0.65 else "concept"
    pick = rng.random()
    if pick < 0.4:
        end = (start + timedelta(days=rng.choice((90, 180, 365)))).isoformat()
        return Medication(code, start.isoformat(), end, "request", drug, "bounds")
    if pick < 0.75:
        days = rng.choice((30, 90, 180))
        return Medication(code, start.isoformat(), None, "request", drug, "duration", days)
    return Medication(code, start.isoformat(), None, "request", drug, "written")


def _notes(follow_up: str, drugs: list[str], rng: random.Random) -> tuple[Note, ...]:
    """A case's clinic notes: usually one, sometimes two, rarely none. The first says what
    follow-up is planned (in words the rules find, or for some on the watch list words they
    miss, or not at all) and, for most charts that list an anticoagulant, which one."""
    count = rng.choices((1, 2, 0), weights=(85, 10, 5))[0]
    if count == 0:
        return ()
    sentences = [rng.choice(OPENINGS)]
    said = rng.random()
    if follow_up == SOON:
        sentences.append(rng.choice(SOON_SAID if said < 0.8 else SOON_UNSAID))
    elif follow_up == ROUTINE and said < 0.85:
        sentences.append(rng.choice(ROUTINE_SAID))
    elif follow_up == NONE and said < 0.85:
        sentences.append(rng.choice(NONE_SAID))
    anticoagulant = [code for code in drugs if MEDICATIONS[code][2]]
    antiplatelet = [code for code in drugs if code in ANTIPLATELETS]
    if anticoagulant and rng.random() < 0.75:
        drug = MEDICATIONS[anticoagulant[0]][0]
        sentences.append(rng.choice(TAKES).format(drug=drug))
    elif not anticoagulant and antiplatelet and rng.random() < 0.7:
        sentences.append(rng.choice(TAKES).format(drug=MEDICATIONS[antiplatelet[0]][0]))
    elif not anticoagulant and rng.random() < 0.3:
        sentences.append(rng.choice(NOT_ON))
    forms: tuple[Form, ...] = ("plain", "plain", "html")
    written = _day(rng, date(2026, 9, 1), date(2026, 10, 15))
    first = Note(tuple(sentences), rng.choice(forms), written.isoformat())
    if count == 1:
        return (first,)
    later = written + timedelta(days=rng.randint(1, 2))
    second = Note(("Medicines reviewed with the patient.",), rng.choice(forms), later.isoformat())
    return (first, second)


# -- the US Core fixtures ----------------------------------------------------------------------

# The record `tests/fixtures/us-core/chart.json` holds, written by hand to have one of each
# thing the agent reads: two coded conditions (one on the watch list), a MedicationStatement,
# MedicationRequests naming their drug by reference to a Medication beside them (with a dosage
# period), to one they contain, and by their own concept (with a duration), a plain and an HTML
# note, and the order; and a lab result the agent does not read.
SHOWCASE = Case(
    id="chart-us-core-1",
    age=67,
    sex="female",
    conditions=(Condition("I48.91", "2019-04-01"), Condition("D68.32", "2026-09-28")),
    medications=(
        Medication("6809", "2021-02-01"),
        Medication("11289", "2025-11-03", "2026-11-02", "request", "reference", "bounds"),
        Medication("83367", "2024-05-20", None, "request", "contained", "written"),
        Medication("29046", "2026-08-01", None, "request", "concept", "duration", 90),
    ),
    notes=(
        Note(
            (
                "Reviewed at the clinic today.",
                "Follow-up within a week.",
                "Takes warfarin every day.",
            ),
            "plain",
            "2026-10-01",
        ),
        Note(("Medicines reviewed with the patient.",), "html", "2026-10-02"),
    ),
    order="0DJD8ZZ",
    written="2026-10-01",
    inr=3.4,
)
SHOWCASE_BUNDLE = "chart"
INVALID_BUNDLE = "chart-invalid"


def us_core_chart() -> dict[str, Any]:
    """`tests/fixtures/us-core/chart.json`: the showcase record, valid US Core 6.1.0."""
    found = SHOWCASE.bundle()
    found["id"] = SHOWCASE_BUNDLE
    return found


def us_core_invalid_chart() -> dict[str, Any]:
    """`tests/fixtures/us-core/invalid/chart.json`: a Patient, then resources a chart holds, each
    made invalid on purpose: a Condition with no category (US Core's slice), a MedicationStatement
    whose status is no code of its value set, a MedicationRequest ordered by no one (us-core-21),
    a note with neither data nor a url (us-core-6), an order whose status is no code, a lab
    result dated by its year alone in a unit that is not UCUM (us-core-1 and -3), a final report
    with no effective time (us-core-10), a Coverage with no member id, neither a member
    identifier nor a subscriber id (us-core-15), and a completed Procedure with no performed time
    (us-core-7). The HL7 validator flags each too (`scripts/fhir_validate.py --invalid`). No
    SNOMED CT: the order and the procedure are coded in ICD-10-PCS. The Patient, the record's
    focus, is valid, so a decision on the record is refused for these findings alone."""
    pid = "chart-invalid-1"
    subject = {"reference": f"Patient/{pid}"}
    person = patient(pid, gender="other", birth_date=None)
    laboratory = {"system": f"{TERMINOLOGY}/observation-category", "code": "laboratory"}
    oddly = {"value": 3.4, "unit": "ratio", "system": "http://example.org/units", "code": "r"}
    myself = {"coding": [{"system": f"{TERMINOLOGY}/subscriber-relationship", "code": "self"}]}
    resources: list[dict[str, Any]] = [
        {"resourceType": "Patient", "id": pid, **_without_type(person)},
        tagged(
            {
                "resourceType": "Condition",
                "id": f"{pid}-condition",
                "clinicalStatus": {"coding": [ACTIVE]},
                "code": {"coding": [_coding(ICD10CM, "I48.91")]},
                "subject": subject,
            },
            "us-core-condition-problems-health-concerns",
        ),
        tagged(
            {
                "resourceType": "MedicationStatement",
                "id": f"{pid}-statement",
                "status": "taking",
                "medicationCodeableConcept": {"coding": [_coding(RXNORM, "6809")]},
                "subject": subject,
            }
        ),
        tagged(
            {
                "resourceType": "MedicationRequest",
                "id": f"{pid}-request",
                "status": "active",
                "intent": "order",
                "medicationCodeableConcept": {"coding": [_coding(RXNORM, "11289")]},
                "subject": subject,
            },
            "us-core-medicationrequest",
        ),
        tagged(
            {
                "resourceType": "DocumentReference",
                "id": f"{pid}-note",
                "status": "current",
                "type": PROGRESS_NOTE,
                "category": [{"coding": [CLINICAL_NOTE]}],
                "subject": subject,
                "content": [{"attachment": {"contentType": "text/plain"}}],
            },
            "us-core-documentreference",
        ),
        tagged(
            {
                "resourceType": "ServiceRequest",
                "id": f"{pid}-order",
                "status": "on-hold-ish",
                "intent": "order",
                "code": {"coding": [_coding(ICD10PCS, "0DJD8ZZ")]},
                "subject": subject,
            },
            "us-core-servicerequest",
        ),
        tagged(
            {
                "resourceType": "Observation",
                "id": f"{pid}-lab",
                "status": "final",
                "category": [{"coding": [laboratory]}],
                "code": {"coding": [_coding(LOINC, "6301-6")]},
                "subject": subject,
                "effectiveDateTime": "2026",
                "valueQuantity": oddly,
            },
            "us-core-observation-lab",
        ),
        tagged(
            {
                "resourceType": "DiagnosticReport",
                "id": f"{pid}-report",
                "status": "final",
                "category": [{"coding": [_coding(LOINC, "LP29708-2")]}],
                "code": {"coding": [_coding(LOINC, "11524-6")]},
                "subject": subject,
            },
            "us-core-diagnosticreport-note",
        ),
        tagged(
            {
                "resourceType": "Coverage",
                "id": f"{pid}-coverage",
                "status": "active",
                "beneficiary": subject,
                "relationship": myself,
                "payor": [subject],
            },
            "us-core-coverage",
        ),
        tagged(
            {
                "resourceType": "Procedure",
                "id": f"{pid}-procedure",
                "status": "completed",
                "code": {"coding": [_coding(ICD10PCS, "0DJ08ZZ")]},
                "subject": subject,
            },
            "us-core-procedure",
        ),
    ]
    return {
        "resourceType": "Bundle",
        "id": INVALID_BUNDLE,
        "meta": {"tag": [SANDBOX_TAG]},
        "type": "collection",
        "entry": [bundle_entry(resource) for resource in resources],
    }


# -- a site's configuration ----------------------------------------------------------------------


def site_code_map(
    org: str = "default", version: str = "chart-site-1", *, orders: bool = False
) -> dict[str, Any]:
    """`MENDARI_SITE_CODES` for these records' fictional site, for organization `org`: its
    diagnosis codes into the agent's `follow_up_conditions`, and with `orders` its order codes
    into the order-sign twin's `checked_orders` (`order_site_codes`), which a server refuses at
    startup unless it serves the twin."""
    sets: dict[str, Any] = {CODE_SET: condition_site_codes()}
    if orders:
        sets[ORDER_CODE_SET] = order_site_codes()
    return {org: {"version": version, "sets": sets}}


def condition_site_codes() -> list[dict[str, str]]:
    """The site's diagnosis codes as translations into `follow_up_conditions`."""
    return [
        {"system": SITE_DIAGNOSES, "code": site, "as": code}
        for code, site in SITE_CONDITION_CODES.items()
    ]


def order_site_codes() -> list[dict[str, str]]:
    """The site's order codes as translations into the order-sign twin's `checked_orders`."""
    return [
        {"system": SITE_ORDERS, "code": site, "as": code} for code, site in SITE_ORDER_CODES.items()
    ]


def ehr_declaration(
    base: str = BASE,
    org: str = "default",
    *,
    not_offered: tuple[str, ...] = ("MedicationStatement",),
    version: str = "chart-ehr-1",
) -> dict[str, Any]:
    """`MENDARI_EHR_DECLARATIONS` for the EHR at `base`: it does not offer `not_offered` (by
    default MedicationStatement, as Epic has no R4 MedicationStatement API), attested by a
    fictional person, for organization `org`. The agent reads its medications from
    MedicationStatement and MedicationRequest, so it may read the list without the first."""
    return {
        base: {
            "org": org,
            "version": version,
            "attested_by": "A. Fictional (synthetic test)",
            "reference": "TEST-CHART-EHR-1",
            "not_offered": list(not_offered),
        }
    }


# -- the files this module writes ----------------------------------------------------------------


def _jsonl(records: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(record, ensure_ascii=True) + "\n" for record in records)


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=True) + "\n"


def written() -> dict[Path, str]:
    """Every file this module writes from its cases, by path, with its content."""
    return {
        AGENT / "data" / "examples.jsonl": _jsonl([case.record() for case in cases("examples")]),
        AGENT / "data" / "test.jsonl": _jsonl([case.record() for case in cases("test")]),
        US_CORE_DIR / "chart.json": _json(us_core_chart()),
        US_CORE_DIR / "invalid" / "chart.json": _json(us_core_invalid_chart()),
    }


def decision_card() -> str:
    """The agent's `DECISION_CARD.md` as `mendari card --write` writes it where the agent is
    served (as a maintained agent, the tests' bundled folder), from the agent's files as they
    are on disk: so after its datasets, which its digest covers."""
    return card_of(load_agent(AGENT, origin="maintained"))


def differing() -> list[Path]:
    """The files whose committed content is not what this module writes: the written ones, and
    the Decision Card when they are current."""
    stale = [
        path
        for path, text in written().items()
        if not path.is_file() or path.read_text(encoding="utf-8") != text
    ]
    if not stale and (not CARD.is_file() or CARD.read_text(encoding="utf-8") != decision_card()):
        stale.append(CARD)
    return stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if a committed file is not what it writes"
    )
    arguments = parser.parse_args(argv)
    if arguments.check:
        stale = differing()
        for path in stale:
            print(
                f"{path.relative_to(ROOT)} is not what tests/support/fixture_records.py writes: "
                "run `uv run python -m tests.support.fixture_records`",
                file=sys.stderr,
            )
        return 1 if stale else 0
    for path, text in written().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    CARD.write_text(decision_card(), encoding="utf-8")  # last: the agent's digest is on it
    return 0


if __name__ == "__main__":
    sys.exit(main())
