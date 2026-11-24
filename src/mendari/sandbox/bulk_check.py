# SPDX-License-Identifier: Apache-2.0
"""The Bulk exit check on the sandbox: export -> dataset -> evaluation, compared with evaluating
the same cases from the dataset file.

The sandbox is seeded from an agent's labelled cases (`mendari sandbox seed`), each Patient
carrying its case id. So an export of the sandbox, labelled from that same dataset, must
evaluate exactly like those cases read straight from it: the same decisions, the same metrics
per question, the same misclassified cases. The export asks for the resource types the agent's
FHIR mapping reads. This is the roadmap's v0.4 exit: "Bulk export -> mendari eval works end to
end".
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from mendari.core.decision import Decision
from mendari.core.pack import Pack
from mendari.errors import MendariError
from mendari.evals.harness import evaluate, load_dataset
from mendari.evals.ledger import case_hash
from mendari.sandbox.code_systems import CASE_SYSTEM
from mendari.sandbox.fhir_workflow import labelled_cases
from mendari.standards import bulk
from mendari.standards.fhir import FhirAdapter
from mendari.standards.fhir_client import GuardedHttp

COMPARED = ("decisions", "questions", "misclassified")


def export_types(adapter: FhirAdapter) -> list[str]:
    """The resource types an export for the agent asks for: the Patient, what the agent's FHIR
    mapping focuses on, and every type its fields and lists read."""
    mapping = adapter.mapping
    listed = [kind for spec in mapping.lists.values() for kind in spec.sources()]
    return list(dict.fromkeys(["Patient", mapping.focus, *mapping.resources, *listed]))


def run(fhir: str, agent: Pack, dataset: str | None = None) -> dict[str, Any]:
    """Export the sandbox, build the agent's dataset from it with the labels of `dataset` (one
    of the agent's datasets or a JSONL file; default: its benchmark dataset, which `mendari
    sandbox seed` loads), and compare its evaluation with the same cases read from the file."""
    if agent.manifest.inputs.fhir is None:
        raise MendariError(
            f"{agent.id} has no FHIR mapping (`inputs.fhir`): a Bulk export becomes its cases "
            "through one"
        )
    labels_path = labelled_cases(agent, dataset).path
    decision = Decision(agent)
    adapter = FhirAdapter.for_pack(agent)
    with tempfile.TemporaryDirectory() as scratch:
        folder = Path(scratch)
        with GuardedHttp() as http:
            manifest = bulk.export(fhir, folder / "export", http=http, types=export_types(adapter))
        resources = list(bulk.read_ndjson(folder / "export"))
        labels = bulk.load_labels(labels_path)
        records, skipped = bulk.dataset(adapter, resources, labels, key_system=CASE_SYSTEM)
        if not records:
            raise MendariError(f"the export holds no labelled {agent.id} cases ({skipped})")
        exported = {r["id"] for r in records}
        direct = [r for r in load_dataset(labels_path).records if r["id"] in exported]
        # In the file's order, whatever order the server exported them in: a metric summed
        # over the cases (and its resampled interval) is the same only in the same order.
        position = {r["id"]: n for n, r in enumerate(direct)}
        records.sort(key=lambda r: position[r["id"]])
        write_jsonl(folder / "bulk.jsonl", records)
        write_jsonl(folder / "direct.jsonl", direct)
        exported_set = load_dataset(folder / "bulk.jsonl")
        direct_set = load_dataset(folder / "direct.jsonl")
        # Each file keys its own case hashes: name the cases by their ids to compare them.
        from_bulk = by_id(evaluate(decision, exported_set), exported_set)
        from_file = by_id(evaluate(decision, direct_set), direct_set)
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


def by_id(report: dict[str, Any], dataset: Any) -> dict[str, Any]:
    """The report with each case hash replaced by the case's id (local only: a check)."""
    ids = {case_hash(dataset.case_key, record["id"]): record["id"] for record in dataset.records}
    named: dict[str, Any] = json.loads(json.dumps(report))
    for item in named["misclassified"]:
        item["case"] = ids[item["case"]]
    for item in named["decisions"]["critical"]:
        item["missed_cases"] = sorted(ids[case] for case in item["missed_cases"])
    return named
