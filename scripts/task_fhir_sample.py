# SPDX-License-Identifier: Apache-2.0
"""Write the decisions Mendari serves as FHIR Tasks (`standards/task_fhir.py`), and their audit
trail as served under `/fhir`, for the HL7 validator (CI job `fhir-conformance`), which checks
them against Mendari's terminology (`--ig`).

A throwaway store gets synthetic decisions from the bundled demo agent, read by its keyword
rules (`sample_agent`), which automate a routine message and abstain on one that asks for two
things:

- `task-reviewed-v1.json` … `-v3.json`: a HUMAN_REVIEW decision, overridden in review (version
  2), rated (no version), then given a downstream outcome (version 3);
- `task-auto.json`, `task-abstain-reviewed.json`, `task-refused.json`: an automated decision, an
  abstention a person labelled, and a refused input;
- `audit-served.json`: those decisions' AuditEvents and Provenances with the absolute references
  to their Tasks that the `/fhir` routes add, as a `collection` Bundle.
- `searchset-*.json`: search pages over them (`standards/search.py`, `fhir_search.py`): Tasks
  with a `next` link and the warning entry for an ignored parameter, a count only, and a
  decision's AuditEvents and Provenances.

    uv run python scripts/task_fhir_sample.py var/fhir-samples
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sample_agent import EMERGENCY, NO_BODY, REFILL, TWO_REQUESTS, demo_decision

from mendari import AuditStore
from mendari.standards import audit_fhir, fhir_search, task_fhir
from mendari.standards import search as fhir_searching
from mendari.standards.fhir import DECISION_ID_SYSTEM

BASE = "https://mendari.example/fhir"  # a reserved name: the samples point nowhere real


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        store = AuditStore(Path(scratch) / "audit.db")
        decide = demo_decision(store)
        reviewed = decide(EMERGENCY)
        store.resolve_review(reviewed.id, final_label="appointment", reviewer="rn-1")
        store.record_feedback(reviewed.id, rating="up")
        store.record_outcome(
            reviewed.id, final_label="other", source="staff-reroute", recorded_by="rn-9"
        )
        automated = decide(REFILL)
        abstained = decide(TWO_REQUESTS)
        store.resolve_review(abstained.id, final_label="refill", reviewer="fd-1")
        refused = decide(NO_BODY)
        made = (reviewed, automated, abstained, refused)
        # Each file is the kind of decision its name says.
        kinds = [decision.outcome.value for decision in made]
        assert kinds == ["HUMAN_REVIEW", "AUTO", "ABSTAIN", "BLOCK"], kinds
        items = [store.item(r.id) for r in made]
        searches = {
            "task-page": (
                "Task",
                [
                    ("status", "completed,requested"),
                    ("stauts", "x"),
                    ("_count", "2"),
                    ("_total", "accurate"),
                ],
            ),
            "task-count": ("Task", [("_summary", "count")]),
            "auditevent": ("AuditEvent", [("entity", f"Task/{reviewed.id}")]),
            "provenance": (
                "Provenance",
                [("target:identifier", f"{DECISION_ID_SYSTEM}|{reviewed.id}"), ("_count", "2")],
            ),
        }
        for name, (resource, query) in searches.items():
            write(out / f"searchset-{name}.json", searchset(store, resource, query))
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


def searchset(store: AuditStore, resource: str, query: list[tuple[str, str]]) -> dict[str, Any]:
    """The first page of a search, as the `/fhir` search routes answer it."""
    search = fhir_searching.parse(fhir_searching.SPECS[resource], query)
    found = fhir_search.find(store, search, base=BASE)
    return fhir_searching.searchset(
        base=BASE,
        search=search,
        resources=found.resources,
        total=found.total,
        next_key=found.next_key,
        timestamp=datetime.now(UTC).isoformat(timespec="milliseconds"),
    )


def write(path: Path, resource: dict[str, Any]) -> None:
    path.write_text(json.dumps(resource, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "var/fhir-samples"))
