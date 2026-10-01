"""The Bulk exit check on the sandbox: export -> dataset -> evaluation, compared with evaluating
the same cases from the dataset file.

The sandbox seeds the first cases of the pack's local-site example dataset (moved to today), so an
export of the sandbox, labelled from that same file, must evaluate exactly like those cases read
straight from it: the same decisions, the same metrics per question, the same misclassified cases.
This is the roadmap's v0.4 exit: "Bulk export -> mendari eval works end to end".
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from mendari.core.decision import Decision
from mendari.errors import MendariError
from mendari.evals.harness import evaluate, load_dataset
from mendari.sandbox import example_datasets_dir
from mendari.sandbox.fhir_records import CASE_SYSTEM
from mendari.standards import bulk
from mendari.standards.fhir import FhirAdapter
from mendari.standards.fhir_client import GuardedHttp

_EXAMPLES = {"mendari/referral-router": "referral-local-site-example.jsonl"}
_TYPES = {
    "mendari/referral-router": [
        "Patient",
        "ServiceRequest",
        "DocumentReference",
        "DiagnosticReport",
        "Observation",
    ],
}
COMPARED = ("decisions", "questions", "misclassified")


def run(fhir: str, pack_id: str) -> dict[str, Any]:
    if pack_id not in _EXAMPLES:
        raise MendariError(f"the bulk check runs for {sorted(_EXAMPLES)}, not {pack_id}")
    datasets = example_datasets_dir()
    if datasets is None:
        raise MendariError("the example datasets are not installed")
    labels_path = datasets / _EXAMPLES[pack_id]
    decision = Decision.from_hub(pack_id)
    with tempfile.TemporaryDirectory() as scratch:
        folder = Path(scratch)
        with GuardedHttp() as http:
            manifest = bulk.export(fhir, folder / "export", http=http, types=_TYPES[pack_id])
        resources = list(bulk.read_ndjson(folder / "export"))
        labels = bulk.load_labels(labels_path)
        adapter = FhirAdapter.for_pack(decision.pack)
        records, skipped = bulk.dataset(adapter, resources, labels, key_system=CASE_SYSTEM)
        if not records:
            raise MendariError(f"the export holds no labelled {pack_id} cases ({skipped})")
        exported = {r["id"] for r in records}
        direct = [r for r in load_dataset(labels_path).records if r["id"] in exported]
        write_jsonl(folder / "bulk.jsonl", records)
        write_jsonl(folder / "direct.jsonl", direct)
        from_bulk = evaluate(decision, load_dataset(folder / "bulk.jsonl"))
        from_file = evaluate(decision, load_dataset(folder / "direct.jsonl"))
    differences = [key for key in COMPARED if from_bulk[key] != from_file[key]]
    return {
        "files": {o["type"]: o["count"] for o in manifest.get("output", [])},
        "cases": len(records),
        "left_out": skipped,
        "accuracy": {q: v["accuracy"] for q, v in from_bulk["questions"].items()},
        "identical": not differences,
        "differences": differences,
    }


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
