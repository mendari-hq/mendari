# SPDX-License-Identifier: Apache-2.0
"""Sandbox workflow against a FHIR R4 server (HAPI FHIR in docker compose).

`seed` loads a message agent's synthetic cases as US Core resources (`fhir_records`): Patients
and Communications (status `in-progress`). The sandbox FHIR server plays the part of the "local
site", and the agent is the bundled demo agent unless another is named. `run_worker` claims each
in-progress Communication, asks the engine for a decision, and writes the resulting Task back.
Only resources carrying the sandbox tag are ever touched.

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

from mendari.core.pack import Pack
from mendari.core.registry import resolve_pack
from mendari.errors import MendariError
from mendari.evals.harness import Dataset, resolve_dataset
from mendari.sandbox import DEMO_AGENT
from mendari.sandbox.code_systems import CASE_SYSTEM, SANDBOX_TAG
from mendari.sandbox.fhir_records import message_entries

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


def labelled_cases(agent: Pack, dataset: str | None = None) -> Dataset:
    """The agent's labelled cases the sandbox is seeded from, and its checks compare with: the
    dataset named (one of the agent's own, or a JSONL file), by default the agent's benchmark
    dataset."""
    ref = dataset or agent.manifest.evaluation.benchmark_dataset
    if ref is None:
        raise MendariError(f"{agent.id} names no benchmark dataset: give its cases with --dataset")
    return resolve_dataset(agent, ref)


def seed_bundle(agent: Pack, count: int, dataset: str | None = None) -> dict[str, Any]:
    """The transaction `seed` posts: `count` of the agent's cases, each a portal message, as
    FHIR resources. A reproducible mix that always includes some cases with a critical label
    (an urgent message, for the demo agent), so the sandbox's queue shows both."""
    chosen = _sample(agent, labelled_cases(agent, dataset).records, count)
    if not all(_is_message(record) for record in chosen):
        raise MendariError(
            f"the sandbox seeds portal messages, and {agent.id}'s cases are not: each needs an "
            "input with a `message` (its `body`, and a `subject` if it has one)"
        )
    entries = [e for record in chosen for e in message_entries(record)]
    return {"resourceType": "Bundle", "type": "transaction", "entry": entries}


def _is_message(record: dict[str, Any]) -> bool:
    given = record.get("input")
    message = given.get("message") if isinstance(given, dict) else None
    return isinstance(message, dict) and isinstance(message.get("body"), str)


def seed(
    fhir: str,
    *,
    count: int,
    wait_seconds: int = 180,
    agent: str = DEMO_AGENT,
    dataset: str | None = None,
) -> int:
    """Load `count` synthetic messages of the agent into the FHIR server, once (a server that
    already holds them is left alone). Returns how many Communications it sent."""
    pack = resolve_pack(agent)
    bundle = seed_bundle(pack, count, dataset)
    if not post_once(fhir, bundle, wait_seconds=wait_seconds, named=pack.id):
        return 0
    return sum(e["resource"]["resourceType"] == "Communication" for e in bundle["entry"])


def post_once(fhir: str, bundle: dict[str, Any], *, wait_seconds: int, named: str) -> bool:
    """Post a transaction of synthetic cases, unless the server already holds its first case:
    the first Patient of the bundle, found by its case id with a fresh search. Returns whether
    it was sent. A bundle with no case is not sent."""
    fhir = fhir.rstrip("/")
    wait_for(fhir, wait_seconds)
    patients = [
        e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Patient"
    ]
    if not patients:  # `--count 0`: nothing to send, and nothing to look for
        return False
    case = next(i["value"] for i in patients[0]["identifier"] if i["system"] == CASE_SYSTEM)
    found = httpx.get(
        f"{fhir}/Patient",
        params={"identifier": f"{CASE_SYSTEM}|{case}", "_summary": "count"},
        headers=_NO_CACHE,  # a cached "0" from before an earlier seed would seed twice
        timeout=30,
    )
    if int(found.json().get("total", 0)):
        print(f"sandbox already holds {named}'s synthetic cases; skipping")
        return False
    post(fhir, bundle)
    return True


def post(fhir: str, bundle: dict[str, Any]) -> None:
    """Post a transaction to the FHIR server, or say why it was refused."""
    response = httpx.post(fhir.rstrip("/"), json=bundle, headers=_FHIR_JSON, timeout=120)
    if not response.is_success:
        raise MendariError(f"seeding failed: HTTP {response.status_code} {response.text[:300]}")


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


def _sample(agent: Pack, records: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    """A reproducible mix of the cases that always includes some with a critical label: at
    least one, and one in ten of `count` when the cases hold that many."""
    critical = {(c.question, c.label) for c in agent.manifest.evaluation.critical}

    def flagged(record: dict[str, Any]) -> bool:
        labels = record.get("labels") or {}
        return any(labels.get(question) == label for question, label in critical)

    urgent = [r for r in records if flagged(r)][: max(1, count // 10)][:count]
    routine = [r for r in records if not flagged(r)][: count - len(urgent)]
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
