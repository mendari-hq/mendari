# SPDX-License-Identifier: Apache-2.0
"""Sandbox workflow against a FHIR R4 server (HAPI FHIR in docker compose).

`seed` loads synthetic cases as US Core resources (`fhir_records`): inbox Patients and
Communications (status `in-progress`), referral Patients, ServiceRequests and their records, or
the trial and prior-authorization packs' Patients and records. The sandbox FHIR server plays the
part of the "local site", so cases come from the local-site example datasets. It also loads the
vendor-shaped EHR profile (`ehr_profile`, LP-23e) under the profile's own ids, which the sandbox
CDS client (`cds_client`) names. `run_worker` claims each in-progress Communication, asks the
engine for a decision, and writes the resulting Task back. Only resources carrying the sandbox
tag are ever touched.

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

from mendari.core.registry import resolve_pack
from mendari.errors import MendariError
from mendari.evals.harness import load_dataset, resolve_dataset
from mendari.sandbox import ehr_profile, example_datasets_dir
from mendari.sandbox.fhir_records import (
    CASE_SYSTEM,
    SANDBOX_TAG,
    inbox_entries,
    prior_auth_entries,
    referral_entries,
    trial_entries,
)

INBOX = "mendari/patient-inbox-router"
REFERRAL = "mendari/referral-router"
TRIAL = "mendari/clinical-trial-prescreener"
PRIOR_AUTH = "mendari/prior-auth-preflight"
EHR_PROFILE = "ehr-profile"  # the vendor-shaped EHR profile (ehr_profile.py), not a pack
SEEDS = (INBOX, REFERRAL, TRIAL, PRIOR_AUTH, EHR_PROFILE)
# What `seed` counts of each: the resource a case or patient is about.
_FOCUS = {
    INBOX: "Communication",
    REFERRAL: "ServiceRequest",
    TRIAL: "Patient",
    PRIOR_AUTH: "ServiceRequest",
    EHR_PROFILE: "Patient",
}
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
            raise MendariError(f"FHIR server {fhir} not ready after {seconds}s")
        time.sleep(3)


def to_resources(record: dict[str, Any]) -> list[dict[str, Any]]:
    """One inbox case as a Patient + Communication pair of transaction entries."""
    return inbox_entries(record)


def seed_bundle(pack_id: str, count: int) -> dict[str, Any]:
    """The transaction `seed` posts: `count` local-site cases of the pack as FHIR resources, or
    (`EHR_PROFILE`) the whole vendor-shaped profile, which PUTs each resource under its id."""
    if pack_id == INBOX:
        entries = [e for record in _sample(count) for e in inbox_entries(record)]
    elif pack_id == REFERRAL:
        cases = _local(REFERRAL, "referral")[:count]
        entries = [e for record in cases for e in referral_entries(record)]
    elif pack_id == TRIAL:
        entries = [e for record in _local(TRIAL, "trial")[:count] for e in trial_entries(record)]
    elif pack_id == PRIOR_AUTH:
        cases = _local(PRIOR_AUTH, "prior-auth")[:count]
        entries = [e for record in cases for e in prior_auth_entries(record)]
    elif pack_id == EHR_PROFILE:
        return ehr_profile.bundle()
    else:
        raise MendariError(f"the sandbox can seed {', '.join(SEEDS)}, not {pack_id}")
    return {"resourceType": "Bundle", "type": "transaction", "entry": entries}


def seed(fhir: str, *, count: int, wait_seconds: int = 180, pack_id: str = INBOX) -> int:
    """Load `count` synthetic cases of the pack into the FHIR server, once (a server that already
    holds them is left alone), or the EHR profile, whose PUTs replace what an earlier seed of it
    made. Returns how many of the cases' focus resources (or the profile's Patients) it sent."""
    fhir = fhir.rstrip("/")
    wait_for(fhir, wait_seconds)
    bundle = seed_bundle(pack_id, count)
    focus = _FOCUS[pack_id]
    if pack_id != EHR_PROFILE and _seeded(fhir, pack_id, bundle):
        print(f"sandbox already holds {pack_id}'s synthetic cases; skipping")
        return 0
    response = httpx.post(fhir, json=bundle, headers=_FHIR_JSON, timeout=120)
    if not response.is_success:
        raise MendariError(f"seeding failed: HTTP {response.status_code} {response.text[:300]}")
    return sum(e["resource"]["resourceType"] == focus for e in bundle["entry"])


def _seeded(fhir: str, pack_id: str, bundle: dict[str, Any]) -> int:
    """Whether the server already holds the pack's cases (how many of what it counts): the
    inbox's tagged Communications, as before. The referral router's and the prior-authorization
    pack's focus is a ServiceRequest and the trial's a Patient, kinds each shares with another
    pack's cases, so for those it counts the Patient of the bundle's first case, by its case id."""
    params = {"_tag": _TAG_QUERY, "_summary": "count"}
    kind = _FOCUS[pack_id]
    if pack_id != INBOX:
        patients = [
            e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Patient"
        ]
        if not patients:  # `--count 0`: nothing to send, and nothing to look for
            return 0
        case = next(i["value"] for i in patients[0]["identifier"] if i["system"] == CASE_SYSTEM)
        params, kind = {"identifier": f"{CASE_SYSTEM}|{case}", "_summary": "count"}, "Patient"
    found = httpx.get(
        f"{fhir}/{kind}",
        params=params,
        headers=_NO_CACHE,  # a cached "0" from before an earlier seed would seed twice
        timeout=30,
    )
    return int(found.json().get("total", 0))


def run_worker(fhir: str, engine: str, *, pack_id: str, interval: float, once: bool) -> None:
    fhir, engine = fhir.rstrip("/"), engine.rstrip("/")
    namespace, name = pack_id.split("/")
    with httpx.Client(timeout=30) as client:
        while True:
            processed = _process_batch(
                client, fhir, f"{engine}/api/agents/{namespace}/{name}/fhir/decide"
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
