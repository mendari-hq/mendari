# SPDX-License-Identifier: Apache-2.0
"""Synthetic cases as US Core 6.1.0 FHIR resources, for the sandbox FHIR server.

Each builder turns one local-site dataset record into transaction entries tagged as synthetic
and claiming the US Core profiles they conform to. The CI job `fhir-conformance` checks the
inbox and referral ones with the HL7 validator; the trial and prior-authorization ones (LP-23a)
are checked in process (`us_core.validate`, in tests/test_fhir_decisions.py). Patients are
obviously synthetic ("Synthetic, Case <id>") and carry the case id as an identifier, which is
how a site's labels are joined back to the cases.

The FHIR mappings read no as-of date (they decide as of today), so referral, trial and
prior-authorization cases are moved to today: every date shifts by the gap between the record's
`as_of` and today, and the birth date gives the same age today. The windows the packs check ("a
visit note in the last 180 days", "an HbA1c in the last 90 days") are therefore the same as in
the dataset. The trial and prior-authorization entries are the same on every run for the same
day (their fullUrls come from the case id), and a code is ICD-10-CM, LOINC or a fictional
system's, never SNOMED CT.
"""

from __future__ import annotations

import base64
import itertools
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

from mendari.errors import MendariError
from mendari.sandbox.synthetic_prior_auth import procedure_codings
from mendari.sandbox.synthetic_trial import CONDITION_SYSTEM, CONDITIONS
from mendari.standards.record_fhir import ICD10, condition_system

SANDBOX_TAG = {"system": "https://mendari.co/fhir/sandbox", "code": "synthetic"}
CASE_SYSTEM = "https://mendari.co/fhir/synthetic-case"
CHANNEL_SYSTEM = "https://mendari.co/fhir/CodeSystem/message-channel"
CHANNELS = {"portal": "Patient portal", "sms": "Text message (SMS)", "email": "Email"}
US_CORE = "http://hl7.org/fhir/us/core/StructureDefinition/"
LOINC = "http://loinc.org"
TERMINOLOGY = "http://terminology.hl7.org/CodeSystem"
# What marks a ServiceRequest as a specialty referral (the Referral Router's FHIR focus): a code
# invented for this synthetic example, in a code system named as fictional. Not SNOMED CT or any
# real code system; a site maps its own referral code. (LP-14: it replaced a SNOMED CT code.)
REFERRAL_SYSTEM = "https://mendari.co/fhir/CodeSystem/fictional-referrals"
REFERRALS = {"FR101": "Specialty referral"}
REFERRAL = {"system": REFERRAL_SYSTEM, "code": "FR101", "display": REFERRALS["FR101"]}
_AGE_FOR_BAND = {"0-17": 8, "18-39": 30, "40-64": 52, "65+": 74}
_CLINICAL_NOTE = {
    "system": "http://hl7.org/fhir/us/core/CodeSystem/us-core-documentreference-category",
    "code": "clinical-note",
}
# Report codes in the referral data -> the US Core DiagnosticReport category they belong to.
_REPORT_CATEGORY = {
    "11524-6": {"system": LOINC, "code": "LP29708-2", "display": "Cardiology"},  # ECG
    "18748-4": {"system": LOINC, "code": "LP29684-5", "display": "Radiology"},  # imaging report
}
_LABORATORY = {"system": f"{TERMINOLOGY}/observation-category", "code": "laboratory"}
# The synthetic records name lab panels without results: the value exists but is not known here.
_VALUE_UNKNOWN = {"coding": [{"system": f"{TERMINOLOGY}/data-absent-reason", "code": "unknown"}]}
# A trial record's conditions are its problem list; a prior-authorization record's diagnoses are
# the ones the order is for (US Core's two Condition profiles).
_PROBLEM_LIST = {"system": f"{TERMINOLOGY}/condition-category", "code": "problem-list-item"}
_ENCOUNTER_DIAGNOSIS = {
    "system": f"{TERMINOLOGY}/condition-category",
    "code": "encounter-diagnosis",
}
_CLINICAL_STATUS = f"{TERMINOLOGY}/condition-clinical"
# A clinic note (the trial and prior-authorization records' `notes`): LOINC's progress note.
_PROGRESS_NOTE = {"coding": [{"system": LOINC, "code": "11506-3"}], "text": "Progress note"}
_SELF = {"coding": [{"system": f"{TERMINOLOGY}/subscriber-relationship", "code": "self"}]}
# The namespace of the deterministic fullUrls (`_case_urls`).
_CASE_URLS = uuid.uuid5(uuid.NAMESPACE_URL, "https://mendari.co/fhir/sandbox")


def inbox_entries(record: dict[str, Any], now: datetime | None = None) -> list[dict[str, Any]]:
    """One inbox case: a Patient and the in-progress Communication (the portal message), sent
    `now` (default: the current time)."""
    now = now or datetime.now(UTC)
    message = record["input"]["message"]
    info = record["input"].get("patient", {})
    birth_year = now.year - _AGE_FOR_BAND.get(info.get("age_band", ""), 45)
    patient = _patient(record["id"], gender="unknown", birth_date=f"{birth_year}-06-15")
    language = info.get("preferred_language", "en")
    patient["communication"] = [
        {"language": {"coding": [{"system": "urn:ietf:bcp:47", "code": language}]}}
    ]
    patient_url = _urn()
    channel = message.get("channel", "portal")
    communication: dict[str, Any] = {
        "resourceType": "Communication",
        "meta": {"tag": [SANDBOX_TAG]},
        "status": "in-progress",
        "category": [{"text": "patient-message"}],
        "medium": [{"coding": [{"system": CHANNEL_SYSTEM, "code": channel}]}],
        "subject": {"reference": patient_url},
        "sender": {"reference": patient_url},
        "sent": now.isoformat(timespec="seconds"),
        "payload": [{"contentString": message["body"]}],
    }
    if message.get("subject"):
        communication["topic"] = {"text": message["subject"]}
    return [_entry(patient, patient_url), _entry(communication)]


def referral_entries(record: dict[str, Any], today: date | None = None) -> list[dict[str, Any]]:
    """One referral case: the Patient, the referral ServiceRequest, and the record the pack
    checks (visit notes as DocumentReferences, ECG and imaging DiagnosticReports, labs)."""
    today = today or datetime.now(UTC).date()
    data = record["input"]
    shift = today - date.fromisoformat(data["as_of"])
    moved = lambda day: (date.fromisoformat(day) + shift).isoformat()  # noqa: E731
    info = data.get("patient", {})
    born = _born(info.get("age"), today)
    patient = _patient(record["id"], gender=info.get("sex", "unknown"), birth_date=born)
    patient_url = _urn()
    subject = {"reference": patient_url}
    referral = data["referral"]
    request = {
        "resourceType": "ServiceRequest",
        "meta": _meta("us-core-servicerequest"),
        "status": "active",
        "intent": "order",
        "category": [{"coding": [REFERRAL]}],
        "priority": referral.get("priority", "routine"),
        "code": {"coding": [REFERRAL]},
        "subject": subject,
        "authoredOn": today.isoformat(),
        "reasonCode": [{"text": referral["reason"]}],
    }
    entries = [_entry(patient, patient_url), _entry(request)]
    entries += [_entry(_document(d, subject, moved)) for d in data.get("documents", [])]
    entries += [_entry(_report(r, subject, moved)) for r in data.get("reports", [])]
    entries += [_entry(_lab(lab, subject, moved)) for lab in data.get("labs", [])]
    return entries


def trial_entries(record: dict[str, Any], today: date | None = None) -> list[dict[str, Any]]:
    """One prescreening case (the Clinical Trial Prescreener's): the Patient, their Conditions
    (the problem list), lab Observations, MedicationStatements and the clinic note as a
    DocumentReference, moved to today. An age the record leaves out stays out."""
    today = today or datetime.now(UTC).date()
    data = record["input"]
    moved = _mover(data["as_of"], today)
    urls = _case_urls(record["id"])
    info = data.get("patient", {})
    age = info.get("age")
    born = None if age is None else _born(age, today)
    patient = _patient(record["id"], gender=info.get("sex", "unknown"), birth_date=born)
    patient_url = next(urls)
    subject = {"reference": patient_url}
    resources = [
        _condition(item, subject, moved, problem_list=True) for item in data.get("conditions", [])
    ]
    resources += [_observation(item, subject, moved) for item in data.get("observations", [])]
    resources += [_medication(item, subject, moved) for item in data.get("medications", [])]
    if data.get("notes"):
        resources.append(_note(data["notes"], subject, today))
    return [_entry(patient, patient_url), *(_entry(r, next(urls)) for r in resources)]


def prior_auth_entries(record: dict[str, Any], today: date | None = None) -> list[dict[str, Any]]:
    """One prior-authorization case (the Prior Authorization Preflight's): the Patient, the order
    as a ServiceRequest, the Coverage naming the payer, the diagnoses, the prior procedures and
    the clinic note as a DocumentReference, moved to today. An order or procedure carries its
    fictional code first (the pack reads `coding[0]`), then the ICD-10-PCS code a payer reads,
    where it has one (`procedure_codings`)."""
    today = today or datetime.now(UTC).date()
    data = record["input"]
    moved = _mover(data["as_of"], today)
    urls = _case_urls(record["id"])
    info = data.get("patient", {})
    age = info.get("age")
    born = None if age is None else _born(age, today)
    patient = _patient(record["id"], gender=info.get("sex", "unknown"), birth_date=born)
    patient_url = next(urls)
    subject = {"reference": patient_url}
    order = data["order"]
    request = {
        "resourceType": "ServiceRequest",
        "meta": _meta("us-core-servicerequest"),
        "status": "active",
        "intent": "order",
        "code": {"coding": procedure_codings(order["code"]), "text": order["name"]},
        "subject": subject,
        "authoredOn": today.isoformat(),
    }
    coverage = {
        "resourceType": "Coverage",
        "meta": _meta("us-core-coverage"),
        "status": "active",
        "subscriberId": record["id"],
        "beneficiary": subject,
        "relationship": _SELF,
        # The synthetic payers are named by their identifier, as the pack's mapping reads them.
        "payor": [{"identifier": {"value": data["coverage"]["payer"]}}],
    }
    resources = [request, coverage]
    resources += [
        _condition(item, subject, moved, problem_list=False) for item in data.get("conditions", [])
    ]
    resources += [_procedure(item, subject, moved) for item in data.get("procedures", [])]
    if data.get("notes"):
        resources.append(_note(data["notes"], subject, today))
    return [_entry(patient, patient_url), *(_entry(r, next(urls)) for r in resources)]


Moved = Callable[[str], str]


def _mover(as_of: str, today: date) -> Moved:
    """A record's dates moved by the gap between its `as_of` and `today`."""
    shift = today - date.fromisoformat(as_of)
    return lambda day: (date.fromisoformat(day) + shift).isoformat()


def _condition(
    item: dict[str, Any], subject: dict[str, str], moved: Moved, *, problem_list: bool
) -> dict[str, Any]:
    code = str(item["code"])
    system = condition_system(code)
    if system not in (ICD10, CONDITION_SYSTEM):  # a code of any other system is read as SNOMED CT
        raise MendariError(
            f"condition code {code!r} is neither ICD-10-CM nor a fictional condition code"
        )
    coding = {"system": system, "code": code}
    if system == CONDITION_SYSTEM:
        coding["display"] = CONDITIONS[code]
    concept: dict[str, Any] = {"coding": [coding]}
    if item.get("text"):
        concept["text"] = item["text"]
    category, profile = (
        (_PROBLEM_LIST, "us-core-condition-problems-health-concerns")
        if problem_list
        else (_ENCOUNTER_DIAGNOSIS, "us-core-condition-encounter-diagnosis")
    )
    resource: dict[str, Any] = {
        "resourceType": "Condition",
        "meta": _meta(profile),
        "category": [{"coding": [category]}],
        "code": concept,
        "subject": subject,
    }
    if item.get("status"):
        resource["clinicalStatus"] = {
            "coding": [{"system": _CLINICAL_STATUS, "code": item["status"]}]
        }
    if item.get("onset"):
        resource["onsetDateTime"] = moved(item["onset"])
    return resource


def _observation(item: dict[str, Any], subject: dict[str, str], moved: Moved) -> dict[str, Any]:
    resource: dict[str, Any] = {
        "resourceType": "Observation",
        "meta": _meta("us-core-observation-lab"),
        "status": "final",
        "category": [{"coding": [_LABORATORY]}],
        "code": {"coding": [{"system": LOINC, "code": item["code"]}], "text": item["name"]},
        "subject": subject,
        "effectiveDateTime": moved(item["date"]),
    }
    if item.get("value") is None:
        resource["dataAbsentReason"] = _VALUE_UNKNOWN
    else:
        quantity = {"value": item["value"]}
        if item.get("unit"):
            quantity["unit"] = item["unit"]
        resource["valueQuantity"] = quantity
    return resource


def _medication(item: dict[str, Any], subject: dict[str, str], moved: Moved) -> dict[str, Any]:
    """A medication the record lists, as a MedicationStatement (US Core profiles none)."""
    period = {edge: moved(item[edge]) for edge in ("start", "end") if item.get(edge)}
    resource: dict[str, Any] = {
        "resourceType": "MedicationStatement",
        "meta": {"tag": [SANDBOX_TAG]},
        "status": item["status"],
        "medicationCodeableConcept": {"text": item["name"]},
        "subject": subject,
    }
    if period:
        resource["effectivePeriod"] = period
    return resource


def _procedure(item: dict[str, Any], subject: dict[str, str], moved: Moved) -> dict[str, Any]:
    return {
        "resourceType": "Procedure",
        "meta": _meta("us-core-procedure"),
        "status": "completed",
        "code": {"coding": procedure_codings(item["code"]), "text": item["name"]},
        "subject": subject,
        "performedDateTime": moved(item["date"]),
    }


def _note(text: str, subject: dict[str, str], today: date) -> dict[str, Any]:
    return {
        "resourceType": "DocumentReference",
        "meta": _meta("us-core-documentreference"),
        "status": "current",
        "type": _PROGRESS_NOTE,
        "category": [{"coding": [_CLINICAL_NOTE]}],
        "subject": subject,
        "date": f"{today.isoformat()}T12:00:00Z",
        "content": [{"attachment": {"contentType": "text/plain", "data": _base64(text)}}],
    }


def _document(document: dict[str, Any], subject: dict[str, str], moved: Moved) -> dict[str, Any]:
    text = f"Synthetic {document['name'].lower()} (no clinical content)."
    return {
        "resourceType": "DocumentReference",
        "meta": _meta("us-core-documentreference"),
        "status": "current",
        "type": _loinc(document),
        "category": [{"coding": [_CLINICAL_NOTE]}],
        "subject": subject,
        "date": f"{moved(document['date'])}T12:00:00Z",
        "content": [{"attachment": {"contentType": "text/plain", "data": _base64(text)}}],
    }


def _report(report: dict[str, Any], subject: dict[str, str], moved: Moved) -> dict[str, Any]:
    category = _REPORT_CATEGORY.get(report["code"])
    return {
        "resourceType": "DiagnosticReport",
        "meta": _meta("us-core-diagnosticreport-note"),
        "status": "final",
        "category": [{"coding": [category]}] if category else [{"text": "report"}],
        "code": _loinc(report),
        "subject": subject,
        "effectiveDateTime": moved(report["date"]),
        "issued": f"{moved(report['date'])}T12:00:00Z",
    }


def _lab(lab: dict[str, Any], subject: dict[str, str], moved: Moved) -> dict[str, Any]:
    return {
        "resourceType": "Observation",
        "meta": _meta("us-core-observation-lab"),
        "status": "final",
        "category": [{"coding": [_LABORATORY]}],
        "code": _loinc(lab),
        "subject": subject,
        "effectiveDateTime": moved(lab["date"]),
        "dataAbsentReason": _VALUE_UNKNOWN,
    }


def _loinc(item: dict[str, Any]) -> dict[str, Any]:
    return {"coding": [{"system": LOINC, "code": item["code"]}], "text": item["name"]}


def _patient(case_id: str, *, gender: str, birth_date: str | None) -> dict[str, Any]:
    patient = {
        "resourceType": "Patient",
        "meta": _meta("us-core-patient"),
        "identifier": [{"system": CASE_SYSTEM, "value": case_id}],
        "name": [{"family": "Synthetic", "given": [f"Case {case_id}"]}],
        "gender": gender,
    }
    if birth_date is not None:
        patient["birthDate"] = birth_date
    return patient


def _born(age: int | None, today: date) -> str:
    """A birth date that makes the patient `age` today (mid-year, clear of birthdays)."""
    if age is None:
        return f"{today.year - 45}-06-15"
    born = today - timedelta(days=round(age * 365.25) + 180)
    return born.isoformat()


def _meta(profile: str) -> dict[str, Any]:
    return {"profile": [f"{US_CORE}{profile}"], "tag": [SANDBOX_TAG]}


def _entry(resource: dict[str, Any], full_url: str | None = None) -> dict[str, Any]:
    kind = resource["resourceType"]
    return {
        "fullUrl": full_url or _urn(),
        "resource": resource,
        "request": {"method": "POST", "url": kind},
    }


def _urn() -> str:
    return f"urn:uuid:{uuid.uuid4()}"


def _case_urls(case_id: str) -> Iterator[str]:
    """One case's fullUrls, in order: the n-th is the same urn on every run."""
    for n in itertools.count():
        yield f"urn:uuid:{uuid.uuid5(_CASE_URLS, f'{case_id}/{n}')}"


def _base64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()
