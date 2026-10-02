"""Write the decisions Mendari serves as FHIR Tasks (`standards/task_fhir.py`), and their audit
trail as served under `/fhir`, for the HL7 validator (CI job `fhir-conformance`), which checks
them against Mendari's terminology (`--ig`).

A throwaway store gets synthetic decisions from the inbox pack, each decided with its local
providers:

- `task-reviewed-v1.json` … `-v3.json`: a HUMAN_REVIEW decision, overridden in review (version
  2), rated (no version), then given a downstream outcome (version 3);
- `task-auto.json`, `task-abstain-reviewed.json`, `task-refused.json`: an automated decision, an
  abstention a person labelled, and a refused input;
- `audit-served.json`: those decisions' AuditEvents and Provenances with the absolute references
  to their Tasks that the `/fhir` routes add, as a `collection` Bundle.

    uv run python scripts/task_fhir_sample.py var/fhir-samples
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from mendari import AuditStore, Decision
from mendari.standards import audit_fhir, task_fhir

BASE = "https://mendari.example/fhir"  # a reserved name: the samples point nowhere real


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        store = AuditStore(Path(scratch) / "audit.db")
        decide = Decision.from_hub("mendari/patient-inbox-router", store=store)
        reviewed = decide({"message": {"body": "Crushing chest pain and my left arm is numb"}})
        store.resolve_review(reviewed.id, final_label="physician", reviewer="rn-1")
        store.record_feedback(reviewed.id, rating="up")
        store.record_outcome(reviewed.id, final_label="nurse", source="staff re-route")
        automated = decide({"message": {"body": "Can you send a refill for metformin?"}})
        abstained = decide({"message": {"body": "hello?"}})
        store.resolve_review(abstained.id, final_label="front_desk", reviewer="fd-1")
        refused = decide({"message": {}})
        items = [store.item(r.id) for r in (reviewed, automated, abstained, refused)]
        store.close()
    trails: list[dict[str, Any]] = []
    for name, item in zip(("reviewed", "auto", "abstain-reviewed", "refused"), items, strict=True):
        assert item is not None
        tasks = task_fhir.task_versions(item, BASE)
        if name == "reviewed":
            for task in tasks:
                write(out / f"task-reviewed-v{task['meta']['versionId']}.json", task)
        else:
            write(out / f"task-{name}.json", tasks[-1])
        versions = audit_fhir.task_versions(item["events"])
        trails += audit_fhir.resources(audit_fhir.joined(item), base=BASE, versions=versions)
    bundle = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{"fullUrl": audit_fhir.full_url(r), "resource": r} for r in trails],
    }
    write(out / "audit-served.json", bundle)
    print(f"Tasks and {len(trails)} served audit resources written to {out}")


def write(path: Path, resource: dict[str, Any]) -> None:
    path.write_text(json.dumps(resource, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "var/fhir-samples"))
