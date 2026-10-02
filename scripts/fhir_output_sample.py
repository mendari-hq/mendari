"""Write the FHIR resources Mendari produces from FHIR input, for the HL7 validator (CI job
`fhir-conformance`), which checks them against Mendari's terminology (`--ig`):

- `task-<pack>.json`: the Task each pack with a FHIR mapping makes from its US Core fixture
  (`tests/fixtures/us-core/`), decided with the pack's local providers;
- `record-bundle.json`: a synthetic trial record as the Bundle a CQL engine reads
  (`standards/record_fhir.py`);
- what the FHIR routes serve, from a local server: `capability.json` (`/fhir/metadata`),
  `operation-definition-decide.json`, and `decide-<pack>.json` (`$decide` on each fixture), plus
  `decide-referral-refused.json` (the invalid referral fixture under `enforce`: a refused input,
  with its findings).

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

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "us-core"
PACKS = {
    "inbox": "mendari/patient-inbox-router",
    "prior-auth": "mendari/prior-auth-preflight",
    "referral": "mendari/referral-router",
    "trial": "mendari/clinical-trial-prescreener",
}
RECORDS = ROOT / "packs" / "clinical-trial-prescreener" / "data" / "test.jsonl"


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
            write(out / "capability.json", client.get("/fhir/metadata").json())
            definition = client.get("/fhir/OperationDefinition/decide").json()
            write(out / "operation-definition-decide.json", definition)
            for name, pack_id in PACKS.items():
                write(
                    out / f"decide-{name}.json", decide(client, pack_id, FIXTURES / f"{name}.json")
                )
    print(f"CapabilityStatement, OperationDefinition and $decide Bundles written to {out}")


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
