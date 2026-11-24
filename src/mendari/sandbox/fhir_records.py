# SPDX-License-Identifier: Apache-2.0
"""Synthetic portal messages as US Core 6.1.0 FHIR resources, for the sandbox FHIR server.

`message_entries` turns one case of a message agent's dataset into transaction entries: a
Patient and the portal message as an in-progress Communication, tagged as synthetic. The Patient
claims the US Core profile it conforms to, is obviously synthetic ("Synthetic, Case <id>") and
carries the case id as an identifier, which is how a site's labels are joined back to the cases.

`patient`, `entry`, `meta` and `urn` are what a synthetic case of any other shape is built with.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from mendari.sandbox.code_systems import CASE_SYSTEM, CHANNEL_SYSTEM, SANDBOX_TAG

US_CORE = "http://hl7.org/fhir/us/core/StructureDefinition/"
_AGE_FOR_BAND = {"0-17": 8, "18-39": 30, "40-64": 52, "65+": 74}


def message_entries(record: dict[str, Any], now: datetime | None = None) -> list[dict[str, Any]]:
    """One portal message: a Patient and the in-progress Communication, sent `now` (default:
    the current time). The case's `input.message` gives the body, and a subject and a channel
    when it has them; `input.patient`, when the case has one, an age band and a language."""
    now = now or datetime.now(UTC)
    message = record["input"]["message"]
    info = record["input"].get("patient", {})
    birth_year = now.year - _AGE_FOR_BAND.get(info.get("age_band", ""), 45)
    sender = patient(record["id"], gender="unknown", birth_date=f"{birth_year}-06-15")
    language = info.get("preferred_language", "en")
    sender["communication"] = [
        {"language": {"coding": [{"system": "urn:ietf:bcp:47", "code": language}]}}
    ]
    patient_url = urn()
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
    return [entry(sender, patient_url), entry(communication)]


def patient(case_id: str, *, gender: str, birth_date: str | None) -> dict[str, Any]:
    """A synthetic Patient, named for its case and carrying the case id as an identifier."""
    made = {
        "resourceType": "Patient",
        "meta": meta("us-core-patient"),
        "identifier": [{"system": CASE_SYSTEM, "value": case_id}],
        "name": [{"family": "Synthetic", "given": [f"Case {case_id}"]}],
        "gender": gender,
    }
    if birth_date is not None:
        made["birthDate"] = birth_date
    return made


def meta(profile: str) -> dict[str, Any]:
    """A resource's `meta`: the US Core profile it claims, and the sandbox tag."""
    return {"profile": [f"{US_CORE}{profile}"], "tag": [SANDBOX_TAG]}


def entry(resource: dict[str, Any], full_url: str | None = None) -> dict[str, Any]:
    """A transaction entry that creates the resource (a POST of its type)."""
    kind = resource["resourceType"]
    return {
        "fullUrl": full_url or urn(),
        "resource": resource,
        "request": {"method": "POST", "url": kind},
    }


def urn() -> str:
    return f"urn:uuid:{uuid.uuid4()}"
