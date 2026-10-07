# SPDX-License-Identifier: Apache-2.0
"""Run a Decision over a labelled dataset and produce a standard evaluation report.

Dataset format (JSON Lines), one case per line:

    {"id": "...", "input": {...pack input...},
     "labels": {"<question>": "<label>", ...},        # ground truth, any subset of questions
     "attributes": {"language": "es", "site": "..."}} # optional, for subgroup analysis

Reports never contain case inputs or ids: a case is named by its ledger hash
(mendari.evals.ledger), so a report on local data can be shared without sharing the data. A
report's result is computed from its ledger (mendari.evals.result); `evaluation` keeps both.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mendari import __version__
from mendari.core.decision import Decision, DecisionResult
from mendari.core.pack import Pack, not_json
from mendari.core.spec import PackManifest
from mendari.errors import MendariError
from mendari.evals import ledger as ledgers
from mendari.evals import metrics
from mendari.evals.ledger import Ledger
from mendari.evals.result import FORMAT, result_from_ledger, result_sha256
from mendari.parallel import parallel_map

REPORT_FORMAT = FORMAT


@dataclass(frozen=True)
class Dataset:
    name: str
    path: Path
    records: list[dict[str, Any]]
    sha256: str  # of the whole file, even when only the first `limit` cases are used
    limit: int | None = None
    # What its cases' ledger hashes are keyed with: from the file's bytes (mendari.evals.ledger).
    case_key: bytes = field(default=b"", repr=False, compare=False)

    def head(self, limit: int) -> Dataset:
        """The first `limit` cases (e.g. to fit a metered provider's time or cost budget)."""
        if limit >= len(self.records):
            return self
        return replace(self, records=self.records[:limit], limit=limit)


def load_dataset(
    path: str | Path, name: str | None = None, *, raw: bytes | None = None, label: str | None = None
) -> Dataset:
    """A JSONL dataset: the file at `path`, or `raw` (a pack's dataset, as its digest covers
    it). A line that is not UTF-8 or not JSON (an integer over Python's 4,300 digits, nested too
    deep), or holds what a JSON report cannot (a lone surrogate, NaN), is a `MendariError`
    naming the file (`label`, or `path`) and the line, never the line's text."""
    path = Path(path)
    where = label or str(path)
    if raw is None:
        raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        number = raw.count(b"\n", 0, error.start) + 1
        raise MendariError(f"{where}:{number}: not UTF-8 text") from None
    records = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise MendariError(f"{where}:{number}: invalid JSON ({error.msg})") from None
        except (ValueError, RecursionError) as error:  # over 4,300 digits, nested too deep
            raise MendariError(f"{where}:{number}: invalid JSON ({type(error).__name__})") from None
        if not isinstance(record, dict) or not isinstance(record.get("input"), dict):
            raise MendariError(f"{where}:{number}: each case needs an 'input' object")
        what = not_json(record)
        if what is not None:
            raise MendariError(f"{where}:{number}: not JSON, it holds {what}")
        record.setdefault("id", f"{path.stem}-{number}")
        records.append(record)
    if not records:
        raise MendariError(f"{where}: dataset is empty")
    return Dataset(
        name=name or path.stem,
        path=path,
        records=records,
        sha256=hashlib.sha256(raw).hexdigest(),
        case_key=ledgers.case_key(raw),
    )


def record_problems(manifest: PackManifest, dataset: Dataset) -> list[str]:
    """What in a dataset's records the pack cannot evaluate, one line per record named by its id
    (never its text): a repeated id (its ledger hash would be too), `labels` that are not a
    mapping of the pack's questions to their labels, or `attributes` that are not a mapping.
    Other keys are kept as they are (`mendari feedback export` adds what the system decided and
    the ratings behind its labels)."""
    problems = []
    seen: set[str] = set()
    for record in dataset.records:
        where = f"{dataset.name} case {record['id']}"
        key = ledgers.canonical(record["id"])  # what its ledger hash is made from
        if key in seen:
            problems.append(f"{where}: another case has the same id")
            continue
        seen.add(key)
        labels = record.get("labels", {})
        if not isinstance(labels, dict):
            problems.append(f"{where}: `labels` must be a mapping of question to label")
        elif not isinstance(record.get("attributes", {}), dict):
            problems.append(f"{where}: `attributes` must be a mapping")
        else:
            for question, label in labels.items():
                # Only what the pack defines is named: a site's label can hold case text.
                if question not in manifest.questions:
                    problems.append(
                        f"{where}: labels name a question the pack does not have (its "
                        f"questions: {', '.join(manifest.questions)})"
                    )
                    break
                if label not in manifest.labels(question):
                    problems.append(
                        f"{where}: labels.{question} is not one of its labels "
                        f"({', '.join(manifest.labels(question))})"
                    )
                    break
    return problems


def resolve_dataset(pack: Pack, ref: str) -> Dataset:
    """A dataset named in the pack's `evaluation.datasets`, or a path to a JSONL file."""
    named = pack.manifest.evaluation.datasets.get(ref)
    if named is not None:
        return load_dataset(pack.path(named), name=ref, raw=pack.read_bytes(named), label=named)
    if Path(ref).is_file():
        return load_dataset(ref)
    known = ", ".join(pack.manifest.evaluation.datasets) or "none"
    raise MendariError(f"dataset {ref!r} is neither a file nor a pack dataset ({known})")


def evaluate(decision: Decision, dataset: Dataset, *, workers: int = 1) -> dict[str, Any]:
    """Run every case. `workers > 1` overlaps network-bound providers; order is preserved. A
    dataset whose records the pack cannot evaluate (`record_problems`) is refused first."""
    return evaluation(decision, dataset, workers=workers).report


@dataclass(frozen=True)
class Evaluation:
    """A run's report and the ledger its result is computed from."""

    report: dict[str, Any]
    ledger: Ledger


def evaluation(decision: Decision, dataset: Dataset, *, workers: int = 1) -> Evaluation:
    """Run every case (as `evaluate`), keeping the ledger to store beside the report."""
    problems = record_problems(decision.pack.manifest, dataset)
    if problems:
        more = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
        raise MendariError(f"{problems[0]}{more}")
    inputs = [record["input"] for record in dataset.records]
    results = parallel_map(decision.decide, inputs, workers)
    return build_evaluation(decision, dataset, results)


def build_evaluation(
    decision: Decision, dataset: Dataset, results: list[DecisionResult]
) -> Evaluation:
    ledger = ledgers.build(decision, dataset, results)
    found = result_from_ledger(ledger)
    run = {
        "id": str(uuid.uuid4()),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "latency_ms": metrics.percentiles([res.latency_ms for res in results]),
        "engine": __version__,
    }
    if decision.governance is not None:  # who approved it: local, never in the result
        run["governance_override"] = decision.governance.to_dict()
    report = {**found, "run": run, "result_sha256": result_sha256(found)}
    return Evaluation(report, ledger)


def build_report(
    decision: Decision, dataset: Dataset, results: list[DecisionResult]
) -> dict[str, Any]:
    return build_evaluation(decision, dataset, results).report
