"""Write the FHIR resources Mendari produces from FHIR input, for the HL7 validator (CI job
`fhir-conformance`), which checks them against Mendari's terminology (`--ig`):

- `task-<pack>.json`: the Task each pack with a FHIR mapping makes from its US Core fixture
  (`tests/fixtures/us-core/`), decided with the pack's local providers;
- `record-bundle.json`: a synthetic trial record as the Bundle a CQL engine reads
  (`standards/record_fhir.py`).

    uv run python scripts/fhir_output_sample.py var/fhir-samples
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from mendari import AuditStore, Decision
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


def write(path: Path, resource: dict[str, Any]) -> None:
    path.write_text(json.dumps(resource, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "var/fhir-samples"))
