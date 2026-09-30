"""Synthetic cases as US Core 6.1.0 FHIR resources, for the sandbox FHIR server.

Each builder turns one local-site dataset record into transaction entries tagged as synthetic
and claiming the US Core profiles they conform to (the CI job `fhir-conformance` checks them
with the HL7 validator). Patients are obviously synthetic ("Synthetic, Case <id>") and carry
the case id as an identifier, which is how a site's labels are joined back to the cases.

The FHIR mappings read no as-of date (they decide as of today), so referral cases are moved to
today: every date shifts by the gap between the record's `as_of` and today, and the birth date
gives the same age today. The windows the pack checks ("a visit note in the last 180 days") are
therefore the same as in the dataset.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

SANDBOX_TAG = {"system": "https://openhealth.dev/fhir/sandbox", "code": "synthetic"}
CASE_SYSTEM = "https://openhealth.dev/fhir/synthetic-case"
CHANNEL_SYSTEM = "https://openhealth.dev/fhir/CodeSystem/message-channel"
US_CORE = "http://hl7.org/fhir/us/core/StructureDefinition/"
SNOMED = "http://snomed.info/sct"
LOINC = "http://loinc.org"
TERMINOLOGY = "http://terminology.hl7.org/CodeSystem"
REFERRAL = {"system": SNOMED, "code": "3457005", "display": "Patient referral"}
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


def inbox_entries(record: dict[str, Any]) -> list[dict[str, Any]]:
    """One inbox case: a Patient and the in-progress Communication (the portal message)."""
    message = record["input"]["message"]
    info = record["input"].get("patient", {})
    birth_year = datetime.now(UTC).year - _AGE_FOR_BAND.get(info.get("age_band", ""), 45)
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
        "sent": datetime.now(UTC).isoformat(timespec="seconds"),
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


Moved = Callable[[str], str]


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


def _patient(case_id: str, *, gender: str, birth_date: str) -> dict[str, Any]:
    return {
        "resourceType": "Patient",
        "meta": _meta("us-core-patient"),
        "identifier": [{"system": CASE_SYSTEM, "value": case_id}],
        "name": [{"family": "Synthetic", "given": [f"Case {case_id}"]}],
        "gender": gender,
        "birthDate": birth_date,
    }


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


def _base64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()
