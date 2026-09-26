from __future__ import annotations

import json
from typing import Any

import httpx

from openhealth.sandbox import synthetic_inbox
from openhealth.sandbox.fhir_workflow import _process_batch, to_resources

FHIR = "http://fhir.test/fhir"
DECIDE = "http://engine.test/api/packs/openhealth/patient-inbox-router/fhir/decide"


class StaleFhir:
    """A FHIR server whose searches always return the original (cached) results, like HAPI's
    search cache, but whose updates honour If-Match versioning."""

    def __init__(self) -> None:
        self.communication: dict[str, Any] = {
            "resourceType": "Communication",
            "id": "1",
            "meta": {"versionId": "1"},
            "status": "in-progress",
            "payload": [{"contentString": "I need a refill on my metformin"}],
        }
        self.cached_search = json.loads(json.dumps(self.communication))
        self.tasks: list[dict[str, Any]] = []
        self.decisions = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "engine.test":
            self.decisions += 1
            decision = {
                "outcome": "AUTO",
                "recommendation": "medical_assistant",
                "priority": "routine",
            }
            return httpx.Response(
                200, json={"decision": decision, "task": {"resourceType": "Task"}}
            )
        if request.method == "GET":
            return httpx.Response(200, json={"entry": [{"resource": self.cached_search}]})
        if request.method == "PUT":
            if request.headers.get("if-match") != f'W/"{self.communication["meta"]["versionId"]}"':
                return httpx.Response(412)
            body = json.loads(request.content)
            version = str(int(self.communication["meta"]["versionId"]) + 1)
            self.communication = {**body, "meta": {"versionId": version}}
            return httpx.Response(200, json=self.communication)
        self.tasks.append(json.loads(request.content))
        return httpx.Response(201, json={})


def test_worker_processes_each_message_once_despite_stale_searches() -> None:
    fhir = StaleFhir()
    with httpx.Client(transport=httpx.MockTransport(fhir)) as client:
        assert _process_batch(client, FHIR, DECIDE) == 1
        assert _process_batch(client, FHIR, DECIDE) == 0  # stale result is skipped, not redone
    assert fhir.decisions == 1
    assert len(fhir.tasks) == 1
    assert fhir.communication["status"] == "completed"
    assert fhir.tasks[0]["meta"]["tag"][0]["code"] == "synthetic"


def test_seed_resources_are_tagged_and_linked() -> None:
    record = synthetic_inbox.local_site(n=1)[0]
    entries = to_resources(record)
    patient, communication = (entry["resource"] for entry in entries)
    assert patient["meta"]["tag"][0]["code"] == "synthetic"
    assert communication["subject"]["reference"] == entries[0]["fullUrl"]
    assert communication["payload"][0]["contentString"] == record["input"]["message"]["body"]


def test_synthetic_generation_is_deterministic() -> None:
    assert synthetic_inbox.local_site(n=25) == synthetic_inbox.local_site(n=25)
    splits = synthetic_inbox.public_splits()
    assert [len(v) for v in splits.values()] == [1500, 300, 600]
    ids = [r["id"] for split in splits.values() for r in split]
    assert len(ids) == len(set(ids))
