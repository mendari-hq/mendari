# SPDX-License-Identifier: Apache-2.0
"""Write the FHIR resources Mendari produces from FHIR input, for the HL7 validator (CI job
`fhir-conformance`), which checks them against Mendari's terminology (`--ig`):

- `task-<pack>.json`: the Task each pack with a FHIR mapping makes from its US Core fixture
  (`tests/fixtures/us-core/`), decided with the pack's local providers;
- `record-bundle.json`: a synthetic trial record as the Bundle a CQL engine reads
  (`standards/record_fhir.py`);
- what the FHIR routes serve, from a local server: `davinci/capability.json`
  (`/fhir/metadata`, which cites DTR's `$questionnaire-package`, so CI checks it with the Da Vinci
  guides: the US Core run loads none), `operation-definition-decide.json`, and
  `decide-<pack>.json` (`$decide` on each fixture), plus `decide-referral-refused.json` (the
  invalid referral fixture under `enforce`: a refused input, with its findings);
- `served-*.json`: what the read and search routes answer for synthetic decisions made through
  the API (`served_reads_and_searches`): a Task read and vread (a decision reviewed, rated and
  given an outcome: versions 1-3), Task searchsets (a page with a `next` link, a lenient search
  with its warning entry, a `_summary=count` Bundle), an AuditEvent and a Provenance read and
  searchset, and a CodeSystem searchset. `tests/test_fhir_routes.py` runs the same requests
  through the in-process structural gate.

    uv run python scripts/fhir_output_sample.py var/fhir-samples
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from mendari import AuditStore, Decision
from mendari.server.app import create_app
from mendari.standards import record_fhir
from mendari.standards.fhir import DECISION_ID_SYSTEM

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "us-core"
PACKS = {
    "inbox": "mendari/patient-inbox-router",
    "prior-auth": "mendari/prior-auth-preflight",
    "referral": "mendari/referral-router",
    "trial": "mendari/clinical-trial-prescreener",
}
RECORDS = ROOT / "packs" / "clinical-trial-prescreener" / "data" / "test.jsonl"
SERVER = "https://mendari.example"  # a reserved name: the samples' links point nowhere real
FORM = {"content-type": "application/x-www-form-urlencoded"}
# The CapabilityStatement cites DTR 2.2.0's $questionnaire-package, so it goes with the Da Vinci
# samples (scripts/davinci_sample.py), which CI validates with DTR 2.2.0 loaded.
CAPABILITY = Path("davinci") / "capability.json"


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        store = AuditStore(Path(scratch) / "audit.db")
        for name, pack_id in PACKS.items():
            resources = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
            result, task = Decision.from_hub(pack_id, store=store).decide_fhir(resources)
            write(out / f"task-{name}.json", task)
            print(f"{pack_id}: {result.outcome.value}, Task written to {out}/task-{name}.json")
        store.close()
    record = json.loads(RECORDS.read_text(encoding="utf-8").splitlines()[0])["input"]
    bundle = record_fhir.bundle(record, date.fromisoformat(record["as_of"]))
    write(out / "record-bundle.json", bundle)
    print(f"{len(bundle['entry'])} resources written to {out}/record-bundle.json")
    served(out)


def served(out: Path) -> None:
    with tempfile.TemporaryDirectory() as scratch:
        for mode in ("warn", "enforce"):
            app = create_app(db_path=Path(scratch) / f"{mode}.db", fhir_validation=mode)
            client = TestClient(app, raise_server_exceptions=True)
            if mode == "enforce":
                invalid = FIXTURES / "invalid" / "referral.json"
                refused = decide(client, PACKS["referral"], invalid)
                write(out / "decide-referral-refused.json", refused)
                continue
            (out / CAPABILITY).parent.mkdir(parents=True, exist_ok=True)
            write(out / CAPABILITY, client.get("/fhir/metadata").json())
            definition = client.get("/fhir/OperationDefinition/decide").json()
            write(out / "operation-definition-decide.json", definition)
            for name, pack_id in PACKS.items():
                write(
                    out / f"decide-{name}.json", decide(client, pack_id, FIXTURES / f"{name}.json")
                )
    print(f"CapabilityStatement, OperationDefinition and $decide Bundles written to {out}")
    with tempfile.TemporaryDirectory() as scratch:
        app = create_app(db_path=Path(scratch) / "search.db")
        client = TestClient(app, base_url=SERVER, raise_server_exceptions=True)
        answers = served_reads_and_searches(client)
    for name, resource in answers.items():
        write(out / f"{name}.json", resource)
    print(f"{len(answers)} read and search answers written to {out}/served-*.json")


def served_reads_and_searches(client: TestClient) -> dict[str, Any]:
    """What the `/fhir` read and search routes answer, by sample name, for synthetic decisions
    made through the API: one reviewed (overridden), rated and given an outcome (Task versions
    1-3), a routine message (automated only while a model screens its urgency; the local server
    here has none, so it waits for review) and an abstention still waiting for review."""
    inbox = PACKS["inbox"]

    def decided(body: str) -> str:
        made = call(client.post(f"/api/packs/{inbox}/decide", json={"message": {"body": body}}))
        return str(made["id"])

    reviewed = decided("Crushing chest pain and my left arm is numb")
    call(
        client.post(
            f"/api/review/{reviewed}", json={"final_label": "physician", "reviewer": "rn-1"}
        )
    )
    call(client.post(f"/api/audit/{reviewed}/feedback", json={"rating": "up"}))
    outcome = {"final_label": "nurse", "source": "staff-reroute"}
    call(client.post(f"/api/audit/{reviewed}/outcome", json=outcome))
    decided("Can you send a refill for metformin?")
    decided("hello?")
    seq = {e["event"]: e["seq"] for e in call(client.get(f"/api/audit/{reviewed}"))["events"]}
    lenient = {"prefer": "handling=lenient"}
    target = f"target:identifier={DECISION_ID_SYSTEM}|{reviewed}&_count=2"
    return {
        "served-task": call(client.get(f"/fhir/Task/{reviewed}")),
        "served-task-v2": call(client.get(f"/fhir/Task/{reviewed}/_history/2")),
        "served-searchset-task": call(client.get("/fhir/Task?_count=1&_total=accurate")),
        "served-searchset-lenient": call(
            client.get("/fhir/Task?status=requested&patient=Patient/p1&stauts=x", headers=lenient)
        ),
        "served-searchset-count": call(
            client.post("/fhir/Task/_search", content="_summary=count", headers=FORM)
        ),
        "served-auditevent": call(
            client.get(f"/fhir/AuditEvent/{reviewed}-{seq['review.resolved']}")
        ),
        "served-searchset-auditevent": call(client.get(f"/fhir/AuditEvent?entity=Task/{reviewed}")),
        "served-provenance": call(
            client.get(f"/fhir/Provenance/{reviewed}-p{seq['outcome.recorded']}")
        ),
        "served-searchset-provenance": call(
            client.post("/fhir/Provenance/_search", content=target, headers=FORM)
        ),
        "served-searchset-codesystem": call(client.get("/fhir/CodeSystem?_count=3")),
    }


def call(response: Any) -> Any:
    response.raise_for_status()
    return response.json()


def decide(client: TestClient, pack_id: str, fixture: Path) -> Any:
    data = json.loads(fixture.read_text(encoding="utf-8"))
    parameters = {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "pack", "valueString": pack_id},
            {"name": "data", "resource": data},
        ],
    }
    response = client.post("/fhir/$decide", json=parameters)
    response.raise_for_status()
    return response.json()


def write(path: Path, resource: dict[str, Any]) -> None:
    path.write_text(json.dumps(resource, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "var/fhir-samples"))
