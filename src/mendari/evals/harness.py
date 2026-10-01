"""Run a Decision over a labelled dataset and produce a standard evaluation report.

Dataset format (JSON Lines), one case per line:

    {"id": "...", "input": {...pack input...},
     "labels": {"<question>": "<label>", ...},        # ground truth, any subset of questions
     "attributes": {"language": "es", "site": "..."}} # optional, for subgroup analysis

Reports never contain case inputs, only ids, so a report on local data can be shared
without sharing the data.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mendari.core.decision import Decision, DecisionResult
from mendari.core.pack import Pack
from mendari.core.spec import Outcome
from mendari.errors import MendariError
from mendari.evals import metrics
from mendari.parallel import parallel_map

REPORT_FORMAT = "mendari.eval/1"
_OUTCOMES = [outcome.value for outcome in Outcome]


@dataclass(frozen=True)
class Dataset:
    name: str
    path: Path
    records: list[dict[str, Any]]
    sha256: str  # of the whole file, even when only the first `limit` cases are used
    limit: int | None = None

    def head(self, limit: int) -> Dataset:
        """The first `limit` cases (e.g. to fit a metered provider's time or cost budget)."""
        if limit >= len(self.records):
            return self
        return replace(self, records=self.records[:limit], limit=limit)


def load_dataset(path: str | Path, name: str | None = None) -> Dataset:
    path = Path(path)
    raw = path.read_bytes()
    records = []
    for number, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise MendariError(f"{path}:{number}: invalid JSON ({error.msg})") from None
        if not isinstance(record, dict) or not isinstance(record.get("input"), dict):
            raise MendariError(f"{path}:{number}: each case needs an 'input' object")
        record.setdefault("id", f"{path.stem}-{number}")
        records.append(record)
    if not records:
        raise MendariError(f"{path}: dataset is empty")
    return Dataset(
        name=name or path.stem,
        path=path,
        records=records,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def resolve_dataset(pack: Pack, ref: str) -> Dataset:
    """A dataset named in the pack's `evaluation.datasets`, or a path to a JSONL file."""
    named = pack.manifest.evaluation.datasets.get(ref)
    if named is not None:
        return load_dataset(pack.path(named), name=ref)
    if Path(ref).is_file():
        return load_dataset(ref)
    known = ", ".join(pack.manifest.evaluation.datasets) or "none"
    raise MendariError(f"dataset {ref!r} is neither a file nor a pack dataset ({known})")


def evaluate(decision: Decision, dataset: Dataset, *, workers: int = 1) -> dict[str, Any]:
    """Run every case. `workers > 1` overlaps network-bound providers; order is preserved."""
    inputs = [record["input"] for record in dataset.records]
    results = parallel_map(decision.decide, inputs, workers)
    return build_report(decision, dataset, results)


def build_report(
    decision: Decision, dataset: Dataset, results: list[DecisionResult]
) -> dict[str, Any]:
    pack = decision.pack
    manifest = pack.manifest
    primary = manifest.policy.primary
    records = dataset.records

    questions = {}
    for question in manifest.questions:
        labelled = [
            (r, res)
            for r, res in zip(records, results, strict=True)
            if question in r.get("labels", {})
        ]
        if not labelled:
            continue
        truths = [r["labels"][question] for r, _ in labelled]
        answers = [res.answers.get(question) for _, res in labelled]
        predictions = [a.label if a else None for a in answers]
        scored = [(a, t) for a, t in zip(answers, truths, strict=True) if a is not None]
        summary = metrics.classification(truths, predictions, manifest.labels(question))
        summary["calibration"] = metrics.calibration(
            [a.confidence for a, _ in scored], [a.label == t for a, t in scored]
        )
        summary["brier"] = metrics.brier(
            [a.probabilities for a, _ in scored], [t for _, t in scored]
        )
        questions[question] = summary

    truth_of = [r.get("labels", {}).get(primary) for r in records]
    outcomes = Counter(res.outcome.value for res in results)
    auto = [
        (res, t)
        for res, t in zip(results, truth_of, strict=True)
        if res.outcome is Outcome.AUTO and t is not None
    ]
    auto_wrong = [res for res, t in auto if res.recommendation != t]
    reviewed = [
        (res, t)
        for res, t in zip(results, truth_of, strict=True)
        if res.outcome is Outcome.HUMAN_REVIEW and t is not None
    ]
    recommended = [
        (res, t)
        for res, t in zip(results, truth_of, strict=True)
        if res.recommendation is not None and t is not None
    ]

    critical = []
    for item in manifest.evaluation.critical:
        cases = [
            (r, res)
            for r, res in zip(records, results, strict=True)
            if r.get("labels", {}).get(item.question) == item.label
        ]
        detected = sum(
            1
            for _, res in cases
            if (a := res.answers.get(item.question)) is not None and a.label == item.label
        )
        critical.append(
            {
                "question": item.question,
                "label": item.label,
                "n": len(cases),
                "detected": detected,
                "detection_rate": metrics.safe_div(detected, len(cases)),
                "auto_missed": sum(res.outcome is Outcome.AUTO for _, res in cases),
                "missed_ids": [
                    r["id"]
                    for r, res in cases
                    if (a := res.answers.get(item.question)) is None or a.label != item.label
                ],
            }
        )

    primary_answers = [
        (res.answers.get(primary), t)
        for res, t in zip(results, truth_of, strict=True)
        if t is not None
    ]
    confident = [(a.confidence, a.label == t) for a, t in primary_answers if a is not None]
    costs = [res.cost_usd for res in results]
    cost_known = all(c is not None for c in costs)

    return {
        "format": REPORT_FORMAT,
        "id": str(uuid.uuid4()),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "pack": {"id": pack.id, "version": pack.version, "digest": pack.digest},
        "providers": {
            slot: {"variant": info.variant, "type": info.type, "model": info.model}
            for slot, info in decision.provider_info.items()
        },
        "policy": {
            "risk_level": int(manifest.risk.level),
            "thresholds": manifest.policy.thresholds.model_dump(),
            "governance_override": decision.governance.to_dict() if decision.governance else None,
        },
        "dataset": {
            "name": dataset.name,
            "sha256": dataset.sha256,
            "n": len(records),
            "limit": dataset.limit,
        },
        "primary": primary,
        "questions": questions,
        "decisions": {
            "outcome_counts": {key: outcomes.get(key, 0) for key in _OUTCOMES},
            "outcome_rates": metrics.rates(outcomes, _OUTCOMES),
            "abstention_rate": metrics.safe_div(outcomes.get("ABSTAIN", 0), len(results)),
            "automation": {
                "n": len(auto),
                "accuracy": metrics.safe_div(len(auto) - len(auto_wrong), len(auto)),
                "false_automation_rate": metrics.safe_div(len(auto_wrong), len(auto)),
                "false_automation_share": metrics.safe_div(len(auto_wrong), len(results)),
            },
            "review": {
                "n": len(reviewed),
                # A reviewer who knows the truth would change every wrong recommendation.
                "expected_override_rate": metrics.safe_div(
                    sum(res.recommendation != t for res, t in reviewed), len(reviewed)
                ),
            },
            "recommendation_accuracy": metrics.safe_div(
                sum(res.recommendation == t for res, t in recommended), len(recommended)
            ),
            "critical": critical,
        },
        "threshold_sweep": metrics.threshold_sweep(
            [c for c, _ in confident], [ok for _, ok in confident]
        ),
        "latency_ms": metrics.percentiles([res.latency_ms for res in results]),
        "cost": {
            "total_usd": sum(c for c in costs if c is not None) if cost_known else None,
            "per_decision_usd": (
                sum(c for c in costs if c is not None) / len(costs) if cost_known else None
            ),
            "input_tokens": sum(res.input_tokens for res in results),
            "output_tokens": sum(res.output_tokens for res in results),
        },
        "errors": dict(Counter(code for res in results for code in res.errors.values())),
        "subgroups": _subgroups(pack, records, results, primary),
        "misclassified": [
            {
                "id": r["id"],
                "truth": t,
                "predicted": a.label if a else None,
                "confidence": round(a.confidence, 4) if a else None,
                "outcome": res.outcome.value,
            }
            for r, res, t in zip(records, results, truth_of, strict=True)
            if t is not None and ((a := res.answers.get(primary)) is None or a.label != t)
        ][:50],
    }


def _subgroups(
    pack: Pack, records: list[dict[str, Any]], results: list[DecisionResult], primary: str
) -> dict[str, dict[str, dict[str, Any]]]:
    groups: dict[str, dict[str, dict[str, Any]]] = {}
    for attribute in pack.manifest.evaluation.subgroups:
        buckets: dict[str, list[tuple[DecisionResult, str | None]]] = {}
        for record, result in zip(records, results, strict=True):
            value = str(record.get("attributes", {}).get(attribute, "unknown"))
            buckets.setdefault(value, []).append((result, record.get("labels", {}).get(primary)))
        groups[attribute] = {}
        for value, members in sorted(buckets.items()):
            labelled = [(res, t) for res, t in members if t is not None]
            answered = [(res.answers.get(primary), t) for res, t in labelled]
            auto = [(res, t) for res, t in labelled if res.outcome is Outcome.AUTO]
            groups[attribute][value] = {
                "n": len(members),
                "accuracy": metrics.safe_div(
                    sum(a is not None and a.label == t for a, t in answered), len(labelled)
                ),
                "auto_rate": metrics.safe_div(
                    sum(res.outcome is Outcome.AUTO for res, _ in members), len(members)
                ),
                "abstention_rate": metrics.safe_div(
                    sum(res.outcome is Outcome.ABSTAIN for res, _ in members), len(members)
                ),
                "false_automation_rate": metrics.safe_div(
                    sum(res.recommendation != t for res, t in auto), len(auto)
                ),
            }
    return groups
