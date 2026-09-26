"""Pure metric functions. Inputs are plain lists so they are trivial to test and reuse."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any


def safe_div(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def classification(
    truths: Sequence[str], predictions: Sequence[str | None], labels: Sequence[str]
) -> dict[str, Any]:
    """Accuracy, per-label precision/recall/F1, macro-F1 and a confusion matrix.

    A missing prediction (provider failure) counts as wrong.
    """
    n = len(truths)
    correct = sum(t == p for t, p in zip(truths, predictions, strict=True))
    confusion = {t: dict.fromkeys([*labels, "none"], 0) for t in labels}
    for truth, prediction in zip(truths, predictions, strict=True):
        confusion[truth][prediction or "none"] += 1
    per_label = {}
    for label in labels:
        tp = confusion[label][label]
        predicted = sum(row[label] for row in confusion.values())
        support = sum(confusion[label].values())
        precision = safe_div(tp, predicted)
        recall = safe_div(tp, support)
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision is not None and recall is not None and precision + recall
            else (0.0 if support else None)
        )
        per_label[label] = {"precision": precision, "recall": recall, "f1": f1, "support": support}
    f1s = [m["f1"] for m in per_label.values() if m["support"] and m["f1"] is not None]
    return {
        "n": n,
        "accuracy": safe_div(correct, n),
        "macro_f1": safe_div(sum(f1s), len(f1s)),
        "per_label": per_label,
        "confusion": confusion,
    }


def calibration(
    confidences: Sequence[float], correct: Sequence[bool], bins: int = 10
) -> dict[str, Any]:
    """Expected calibration error over equal-width confidence bins, plus the reliability table."""
    table = []
    ece = 0.0
    n = len(confidences)
    for index in range(bins):
        low, high = index / bins, (index + 1) / bins
        members = [
            i
            for i, c in enumerate(confidences)
            if low <= c < high or (index == bins - 1 and c == 1.0)
        ]
        if not members:
            continue
        mean_conf = sum(confidences[i] for i in members) / len(members)
        accuracy = sum(correct[i] for i in members) / len(members)
        ece += len(members) / n * abs(accuracy - mean_conf)
        table.append(
            {
                "low": low,
                "high": high,
                "n": len(members),
                "confidence": mean_conf,
                "accuracy": accuracy,
            }
        )
    return {"ece": ece if n else None, "bins": table}


def brier(probabilities: Sequence[Mapping[str, float]], truths: Sequence[str]) -> float | None:
    """Multi-class Brier score: mean squared distance to the one-hot truth (0 best, 2 worst)."""
    if not probabilities:
        return None
    total = 0.0
    for dist, truth in zip(probabilities, truths, strict=True):
        total += sum((p - (label == truth)) ** 2 for label, p in dist.items())
    return total / len(probabilities)


def percentiles(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "p50": None, "p95": None, "max": None}
    ordered = sorted(values)

    def pick(pct: float) -> float:
        return ordered[min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))]

    return {
        "mean": sum(ordered) / len(ordered),
        "p50": pick(50),
        "p95": pick(95),
        "max": ordered[-1],
    }


def threshold_sweep(
    confidences: Sequence[float],
    correct: Sequence[bool],
    thresholds: Sequence[float] = (0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 0.97, 0.99),
) -> list[dict[str, float | None]]:
    """Coverage vs accuracy if everything at or above each threshold were automated."""
    n = len(confidences)
    rows = []
    for threshold in thresholds:
        chosen = [ok for c, ok in zip(confidences, correct, strict=True) if c >= threshold]
        rows.append(
            {
                "threshold": threshold,
                "coverage": safe_div(len(chosen), n),
                "accuracy": safe_div(sum(chosen), len(chosen)),
                "errors": len(chosen) - sum(chosen),
            }
        )
    return rows


def rates(counts: Counter[str], keys: Sequence[str]) -> dict[str, float | None]:
    total = sum(counts.values())
    return {key: safe_div(counts.get(key, 0), total) for key in keys}
