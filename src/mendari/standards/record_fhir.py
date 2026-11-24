# SPDX-License-Identifier: Apache-2.0
"""A pack's record input as FHIR R4 resources, for engines that read FHIR (a CQL library).

Record packs (a trial-eligibility agent, for one) take a compact record: the patient's age and sex,
coded conditions, lab results and medications. A CQL library reads the same facts as FHIR, so
this turns the record into a `collection` Bundle:

| Record | FHIR |
|---|---|
| `patient.age`, `patient.sex` | Patient: `birthDate` (`age` years before screening), `gender` |
| `conditions[]` | Condition: code (`condition_system`), clinical status, onset |
| `observations[]` | Observation: LOINC code, `valueQuantity`, effective date |
| `medications[]` | MedicationStatement: RxNorm code and name, status, effective period |

Dates are read the way the `criteria` provider reads them (partial dates mid-period), so both
see the same record. Anything the record leaves out stays out: no value is invented.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from typing import Any

from mendari.providers.criteria import _date, _items
from mendari.sandbox.code_systems import CONDITION_SYSTEM, CONDITIONS

PATIENT_ID = "record-patient"
ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"
# A record's condition code names no system: one shaped like ICD-10-CM is ICD-10-CM, one of the
# synthetic local site's fictional codes is theirs, and any other is read as a site's SNOMED CT.
SNOMED = "http://snomed.info/sct"
LOINC = "http://loinc.org"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
_ICD10_CODE = re.compile(r"^[A-TV-Z][0-9][0-9A-Z](\.[0-9A-Z]{1,4})?\Z")
_CLINICAL_STATUS = {"active", "recurrence", "relapse", "inactive", "remission", "resolved"}
_MEDICATION_STATUS = {
    "active",
    "completed",
    "entered-in-error",
    "intended",
    "stopped",
    "on-hold",
    "unknown",
    "not-taken",
}


def bundle(record: Mapping[str, Any], as_of: date) -> dict[str, Any]:
    """The record as a `collection` Bundle, its Patient first."""
    subject = {"reference": f"Patient/{PATIENT_ID}"}
    resources: list[dict[str, Any]] = [_patient(record.get("patient"), as_of)]
    for i, item in enumerate(_items(record.get("conditions"))):
        resources.append(_condition(f"condition-{i}", item, subject))
    for i, item in enumerate(_items(record.get("observations"))):
        resources.append(_observation(f"observation-{i}", item, subject))
    for i, item in enumerate(_items(record.get("medications"))):
        resources.append(_medication(f"medication-{i}", item, subject))
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [
            {"fullUrl": f"urn:mendari:{r['resourceType']}/{r['id']}", "resource": r}
            for r in resources
        ],
    }


def born(age: int, as_of: date) -> date:
    """A birth date that makes someone exactly `age` on `as_of` (Feb 29 falls back to Feb 28)."""
    try:
        return as_of.replace(year=as_of.year - age)
    except ValueError:
        return as_of.replace(year=as_of.year - age, day=28)


def _patient(patient: Any, as_of: date) -> dict[str, Any]:
    resource: dict[str, Any] = {"resourceType": "Patient", "id": PATIENT_ID}
    if isinstance(patient, Mapping):
        age = patient.get("age")
        if isinstance(age, int) and not isinstance(age, bool) and 0 <= age <= 130:
            resource["birthDate"] = born(age, as_of).isoformat()
        if patient.get("sex") in ("female", "male", "other", "unknown"):
            resource["gender"] = patient["sex"]
    return resource


def _coding(code: Any, *, display: Any = None) -> dict[str, Any]:
    text = str(code)
    system = condition_system(text)
    coding: dict[str, Any] = {"system": system, "code": text}
    if isinstance(display, str) and display:
        coding["display"] = display
    return coding


def condition_system(code: str) -> str:
    """The code system a record's condition code is read as (see `SNOMED`)."""
    if _ICD10_CODE.match(code):
        return ICD10
    return CONDITION_SYSTEM if code in CONDITIONS else SNOMED


def _condition(key: str, item: Mapping[str, Any], subject: dict[str, str]) -> dict[str, Any]:
    resource: dict[str, Any] = {"resourceType": "Condition", "id": key, "subject": subject}
    if isinstance(item.get("code"), str) and item["code"]:
        resource["code"] = {"coding": [_coding(item["code"], display=item.get("text"))]}
    status = item.get("status")
    if status in _CLINICAL_STATUS:
        system = "http://terminology.hl7.org/CodeSystem/condition-clinical"
        resource["clinicalStatus"] = {"coding": [{"system": system, "code": status}]}
    onset = _date(item.get("onset"))
    if onset is not None:
        resource["onsetDateTime"] = onset.isoformat()
    return resource


def _observation(key: str, item: Mapping[str, Any], subject: dict[str, str]) -> dict[str, Any]:
    resource: dict[str, Any] = {
        "resourceType": "Observation",
        "id": key,
        "status": "final",
        "subject": subject,
        "code": {"coding": [{"system": LOINC, "code": str(item.get("code"))}]},
    }
    if isinstance(item.get("name"), str) and item["name"]:
        resource["code"]["text"] = item["name"]
    value = item.get("value")
    if isinstance(value, int | float) and not isinstance(value, bool):
        quantity: dict[str, Any] = {"value": value}
        if isinstance(item.get("unit"), str) and item["unit"]:
            quantity["unit"] = item["unit"]
        resource["valueQuantity"] = quantity
    when = _date(item.get("date"))
    if when is not None:
        resource["effectiveDateTime"] = when.isoformat()
    return resource


def _medication(key: str, item: Mapping[str, Any], subject: dict[str, str]) -> dict[str, Any]:
    status = item.get("status")
    concept: dict[str, Any] = {}
    if isinstance(item.get("code"), str) and item["code"]:
        concept["coding"] = [{"system": RXNORM, "code": item["code"]}]
    if isinstance(item.get("name"), str) and item["name"]:
        concept["text"] = item["name"]
    resource: dict[str, Any] = {
        "resourceType": "MedicationStatement",
        "id": key,
        "status": status if status in _MEDICATION_STATUS else "unknown",
        "subject": subject,
        "medicationCodeableConcept": concept or {"text": "unnamed medication"},
    }
    period = {
        edge: when.isoformat()
        for edge, when in (("start", _date(item.get("start"))), ("end", _date(item.get("end"))))
        if when is not None
    }
    if period:
        resource["effectivePeriod"] = period
    return resource
