"""The OMOP exit check: a dataset written into an OMOP CDM database and read back as pack inputs
must evaluate exactly like the dataset itself (the same decisions, metrics per question and
misclassified cases). This is the roadmap's v0.7 check for "OMOP CDM -> pack inputs".
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from mendari.core.decision import Decision
from mendari.core.registry import resolve_pack
from mendari.evals.harness import evaluate, load_dataset
from mendari.sandbox.bulk_check import COMPARED, write_jsonl
from mendari.standards import omop


def run(dataset: Path, pack_id: str) -> dict[str, Any]:
    decision = Decision(resolve_pack(pack_id))
    source = load_dataset(dataset)
    with tempfile.TemporaryDirectory() as scratch:
        folder = Path(scratch)
        db = omop.open_database(str(folder / "omop.db"), create=True)
        written = omop.write(db, [(r["id"], r["input"]) for r in source.records])
        labels = {r["id"]: r.get("labels", {}) for r in source.records}
        records = [
            {"id": person, "input": record, "labels": labels[person]}
            for person, record in omop.read(db)
        ]
        db.close()
        write_jsonl(folder / "omop.jsonl", records)
        from_omop = evaluate(decision, load_dataset(folder / "omop.jsonl"))
    from_file = evaluate(decision, source)
    differences = [key for key in COMPARED if from_omop[key] != from_file[key]]
    return {
        "people": written,
        "cases": len(records),
        "accuracy": {q: v["accuracy"] for q, v in from_omop["questions"].items()},
        "identical": not differences,
        "differences": differences,
    }
