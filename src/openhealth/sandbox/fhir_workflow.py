"""Sandbox workflow against a FHIR R4 server (HAPI FHIR in docker compose).

`seed` loads synthetic cases as US Core resources (`fhir_records`): inbox Patients and
Communications (status `in-progress`), or referral Patients, ServiceRequests and their records.
The sandbox FHIR server plays the part of the "local site", so cases come from the local-site
example datasets. `run_worker` claims each in-progress Communication, asks the engine for a
decision, and writes the resulting Task back. Only resources carrying the sandbox tag are ever
touched.

Each Communication is claimed with a version-conditional update (`If-Match`) before the engine
sees it, so a message is processed at most once even if searches return stale results (HAPI
caches search results) or several workers run at the same time.
"""

from __future__ import annotations

import contextlib
import random
import time
from typing import Any

import httpx

from openhealth.core.registry import resolve_pack
from openhealth.errors import OpenHealthError
from openhealth.evals.harness import load_dataset, resolve_dataset
from openhealth.sandbox import example_datasets_dir
from openhealth.sandbox.fhir_records import SANDBOX_TAG, inbox_entries, referral_entries

INBOX = "openhealth/patient-inbox-router"
REFERRAL = "openhealth/referral-router"
_TAG_QUERY = f"{SANDBOX_TAG['system']}|{SANDBOX_TAG['code']}"
_FHIR_JSON = {"content-type": "application/fhir+json", "accept": "application/fhir+json"}
# HAPI reuses cached search results for a while; a work queue must always see fresh state.
_NO_CACHE = {**_FHIR_JSON, "cache-control": "no-cache"}


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
    """One inbox case as a Patient + Communication pair of transaction entries."""
    return inbox_entries(record)


def seed_bundle(pack_id: str, count: int) -> dict[str, Any]:
    """The transaction `seed` posts: `count` local-site cases of the pack as FHIR resources."""
    if pack_id == INBOX:
        entries = [e for record in _sample(count) for e in inbox_entries(record)]
    elif pack_id == REFERRAL:
        cases = _local(REFERRAL, "referral")[:count]
        entries = [e for record in cases for e in referral_entries(record)]
    else:
        raise OpenHealthError(f"the sandbox can seed {INBOX} or {REFERRAL}, not {pack_id}")
    return {"resourceType": "Bundle", "type": "transaction", "entry": entries}


def seed(fhir: str, *, count: int, wait_seconds: int = 180, pack_id: str = INBOX) -> int:
    """Load `count` synthetic cases of the pack into the FHIR server, once (a server that already
    holds the pack's tagged focus resources is left alone)."""
    fhir = fhir.rstrip("/")
    wait_for(fhir, wait_seconds)
    focus = "Communication" if pack_id == INBOX else "ServiceRequest"
    existing = (
        httpx.get(
            f"{fhir}/{focus}",
            params={"_tag": _TAG_QUERY, "_summary": "count"},
            headers=_NO_CACHE,  # a cached "0" from before an earlier seed would seed twice
            timeout=30,
        )
        .json()
        .get("total", 0)
    )
    if existing:
        print(f"sandbox already seeded ({existing} synthetic {focus}s); skipping")
        return 0
    bundle = seed_bundle(pack_id, count)
    response = httpx.post(fhir, json=bundle, headers=_FHIR_JSON, timeout=120)
    if not response.is_success:
        raise OpenHealthError(f"seeding failed: HTTP {response.status_code} {response.text[:300]}")
    return sum(e["resource"]["resourceType"] == focus for e in bundle["entry"])


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


def _local(pack_id: str, prefix: str) -> list[dict[str, Any]]:
    """The pack's local-site example cases (the sandbox plays the local site)."""
    datasets = example_datasets_dir()
    name = "local-site-example.jsonl" if prefix == "inbox" else f"{prefix}-local-site-example.jsonl"
    local = datasets / name if datasets else None
    if local is not None and local.is_file():
        return load_dataset(local).records
    return resolve_dataset(resolve_pack(pack_id), "test").records


def _sample(count: int) -> list[dict[str, Any]]:
    """A reproducible mix of local-site messages that always includes some emergencies."""
    records = _local(INBOX, "inbox")
    urgent = [r for r in records if r["labels"]["urgency"] == "urgent"][: max(1, count // 10)]
    routine = [r for r in records if r["labels"]["urgency"] != "urgent"][: count - len(urgent)]
    chosen = urgent + routine
    random.Random(0).shuffle(chosen)
    return chosen


def _process_batch(client: httpx.Client, fhir: str, decide_url: str) -> int:
    search = client.get(
        f"{fhir}/Communication",
        params={
            "status": "in-progress",
            "_tag": _TAG_QUERY,
            "_include": "Communication:subject",
            "_count": "25",
        },
        headers=_NO_CACHE,
    )
    search.raise_for_status()
    entries = [entry["resource"] for entry in search.json().get("entry", [])]
    patients = {f"Patient/{r['id']}": r for r in entries if r["resourceType"] == "Patient"}
    communications = [r for r in entries if r["resourceType"] == "Communication"]
    processed = 0
    for communication in communications:
        claimed = _claim(client, fhir, communication)
        if claimed is None:
            continue  # already taken by another worker, or a stale search result
        patient = patients.get(communication.get("subject", {}).get("reference", ""))
        bundle = {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": [{"resource": communication}] + ([{"resource": patient}] if patient else []),
        }
        try:
            response = client.post(decide_url, json=bundle)
            response.raise_for_status()
            decision, task = response.json()["decision"], response.json()["task"]
            task.setdefault("meta", {}).setdefault("tag", []).append(SANDBOX_TAG)
            client.post(f"{fhir}/Task", json=task, headers=_FHIR_JSON).raise_for_status()
        except httpx.HTTPError:
            _release(client, fhir, claimed)
            raise
        processed += 1
        target = decision["recommendation"] or "manual triage"
        print(
            f"Communication/{communication['id']}: {decision['outcome']:<12} -> {target:<17} "
            f"priority {decision['priority']}",
            flush=True,
        )
    return processed


def _claim(client: httpx.Client, fhir: str, communication: dict[str, Any]) -> dict[str, Any] | None:
    """Mark the Communication completed iff nobody changed it since we read it."""
    version = communication.get("meta", {}).get("versionId")
    updated = {**communication, "status": "completed"}
    response = client.put(
        f"{fhir}/Communication/{communication['id']}",
        json=updated,
        headers={**_FHIR_JSON, "if-match": f'W/"{version}"'},
    )
    if response.status_code in (409, 412):
        return None
    response.raise_for_status()
    claimed: dict[str, Any] = response.json()
    return claimed


def _release(client: httpx.Client, fhir: str, claimed: dict[str, Any]) -> None:
    """Put a claimed message back in the queue after a failure. The caller re-raises the
    original error, so a failed release must not mask it."""
    with contextlib.suppress(httpx.HTTPError):
        client.put(
            f"{fhir}/Communication/{claimed['id']}",
            json={**claimed, "status": "in-progress"},
            headers=_FHIR_JSON,
        )
