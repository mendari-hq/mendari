"""Sandbox workflow against a FHIR R4 server (HAPI FHIR in docker compose).

`seed` loads synthetic Patients and Communications (status `in-progress`) from the reference
pack's test split. `run_worker` polls for in-progress Communications, asks the engine for a
decision, writes the resulting Task back to the FHIR server and completes the Communication.
Only resources carrying the sandbox tag are ever touched.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from openhealth.core.registry import resolve_pack
from openhealth.errors import OpenHealthError
from openhealth.evals.harness import resolve_dataset

SANDBOX_TAG = {"system": "https://openhealth.dev/fhir/sandbox", "code": "synthetic"}
CHANNEL_SYSTEM = "https://openhealth.dev/fhir/CodeSystem/message-channel"
_TAG_QUERY = f"{SANDBOX_TAG['system']}|{SANDBOX_TAG['code']}"
_AGE_FOR_BAND = {"0-17": 8, "18-39": 30, "40-64": 52, "65+": 74}
CASE_SYSTEM = "https://openhealth.dev/fhir/synthetic-case"
_FHIR_JSON = {"content-type": "application/fhir+json", "accept": "application/fhir+json"}


def wait_for(fhir: str, seconds: int) -> None:
    deadline = time.monotonic() + seconds
    while True:
        try:
            if httpx.get(f"{fhir}/metadata", timeout=10, headers=_FHIR_JSON).is_success:
                return
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            raise OpenHealthError(f"FHIR server {fhir} not ready after {seconds}s")
        time.sleep(3)


def to_resources(record: dict[str, Any]) -> list[dict[str, Any]]:
    """One synthetic case as a Patient + Communication pair of transaction entries."""
    message = record["input"]["message"]
    patient_info = record["input"].get("patient", {})
    patient_url = f"urn:uuid:{uuid.uuid4()}"
    channel = message.get("channel", "portal")
    birth_year = datetime.now(UTC).year - _AGE_FOR_BAND.get(patient_info.get("age_band", ""), 45)
    patient = {
        "resourceType": "Patient",
        "meta": {"tag": [SANDBOX_TAG]},
        "identifier": [
            {"system": "https://openhealth.dev/fhir/synthetic-case", "value": record["id"]}
        ],
        "birthDate": f"{birth_year}-06-15",
        "communication": [
            {
                "language": {
                    "coding": [
                        {
                            "system": "urn:ietf:bcp:47",
                            "code": patient_info.get("preferred_language", "en"),
                        }
                    ]
                }
            }
        ],
    }
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
    return [
        {
            "fullUrl": patient_url,
            "resource": patient,
            "request": {"method": "POST", "url": "Patient"},
        },
        {
            "fullUrl": f"urn:uuid:{uuid.uuid4()}",
            "resource": communication,
            "request": {"method": "POST", "url": "Communication"},
        },
    ]


def seed(fhir: str, *, count: int, wait_seconds: int = 180) -> int:
    fhir = fhir.rstrip("/")
    wait_for(fhir, wait_seconds)
    existing = (
        httpx.get(
            f"{fhir}/Communication",
            params={"_tag": _TAG_QUERY, "_summary": "count"},
            headers=_FHIR_JSON,
            timeout=30,
        )
        .json()
        .get("total", 0)
    )
    if existing:
        print(f"sandbox already seeded ({existing} synthetic Communications); skipping")
        return 0
    pack = resolve_pack("openhealth/patient-inbox-router")
    records = resolve_dataset(pack, "test").records
    step = max(1, len(records) // count)
    chosen = records[::step][:count]
    bundle = {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [entry for record in chosen for entry in to_resources(record)],
    }
    response = httpx.post(fhir, json=bundle, headers=_FHIR_JSON, timeout=120)
    if not response.is_success:
        raise OpenHealthError(f"seeding failed: HTTP {response.status_code} {response.text[:300]}")
    return len(chosen)


def run_worker(fhir: str, engine: str, *, pack_id: str, interval: float, once: bool) -> None:
    fhir, engine = fhir.rstrip("/"), engine.rstrip("/")
    namespace, name = pack_id.split("/")
    with httpx.Client(timeout=30) as client:
        while True:
            processed = _process_batch(
                client, fhir, f"{engine}/api/packs/{namespace}/{name}/fhir/decide"
            )
            if once:
                return
            if not processed:
                time.sleep(interval)


def _process_batch(client: httpx.Client, fhir: str, decide_url: str) -> int:
    search = client.get(
        f"{fhir}/Communication",
        params={
            "status": "in-progress",
            "_tag": _TAG_QUERY,
            "_include": "Communication:subject",
            "_count": "25",
        },
        headers=_FHIR_JSON,
    )
    search.raise_for_status()
    entries = [entry["resource"] for entry in search.json().get("entry", [])]
    patients = {f"Patient/{r['id']}": r for r in entries if r["resourceType"] == "Patient"}
    communications = [r for r in entries if r["resourceType"] == "Communication"]
    for communication in communications:
        patient = patients.get(communication.get("subject", {}).get("reference", ""))
        bundle = {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": [{"resource": communication}] + ([{"resource": patient}] if patient else []),
        }
        response = client.post(decide_url, json=bundle)
        response.raise_for_status()
        decision, task = response.json()["decision"], response.json()["task"]
        task.setdefault("meta", {}).setdefault("tag", []).append(SANDBOX_TAG)
        client.post(f"{fhir}/Task", json=task, headers=_FHIR_JSON).raise_for_status()
        communication["status"] = "completed"
        client.put(
            f"{fhir}/Communication/{communication['id']}", json=communication, headers=_FHIR_JSON
        ).raise_for_status()
        target = decision["recommendation"] or "manual triage"
        print(
            f"Communication/{communication['id']}: {decision['outcome']:<12} -> {target:<17} "
            f"priority {decision['priority']}",
            flush=True,
        )
    return len(communications)
