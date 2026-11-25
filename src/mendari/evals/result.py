# SPDX-License-Identifier: Apache-2.0
"""An evaluation report's result (`mendari.eval/1`, LP-10), computed only from its ledger and
seeds, so two runs on the same cases give byte-identical results and anyone holding the ledger can
recompute them. What changes from run to run (its id, when it ran, how long decisions took) is in
the report's separate `run` block, outside the result; so is the run's operating point
(mendari.evals.operating_point), which depends on the run's options and on what each decision
kept in memory, never on the ledger alone.

The result holds every figure and, with them, each proportion as k of n with its exact
Clopper-Pearson interval (`proportions`, and `proportion` beside each question's accuracy and each
critical label's detection), a seeded bootstrap interval for each Brier score, an equal-mass
calibration error beside the equal-width one ("not estimable" on small bins), the answering models,
and the ledger it came from. Cases are named by their ledger hash, never their id.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from mendari.evals import metrics, stats
from mendari.evals.ledger import Ledger, canonical

FORMAT = "mendari.eval/1"
SEED = 0
# A critical label's evidence floor: fewer positives than this, and its figure says "insufficient
# evidence" and ranks nothing (LP-10).
MIN_POSITIVES = 100
MAX_N = 10**9  # the size of anything a figure is of; a larger one is not a dataset
_SLACK = 1e-6  # how far rate x n may be from a whole k, from a rate's float rounding
OUTCOMES = ["AUTO", "HUMAN_REVIEW", "ABSTAIN", "BLOCK"]
# Keys of a report that are not its result: what differs between two runs on the same cases.
# Not the result: the run, a comparison with a benchmark, the hash, and the figures a server
# serves beside a report (mendari.evals.figures, worked out on every read, never stored).
RUN_KEYS = ("run", "comparison", "result_sha256", "figures")
TRUTH, PREDICTED, CONFIDENCE, PROBABILITIES, MODEL, ERROR = range(6)


def result_sha256(result: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical(result).encode()).hexdigest()


def result_of(report: Mapping[str, Any]) -> dict[str, Any]:
    """A `mendari.eval/1` report's result block: the report without its run."""
    return {key: value for key, value in report.items() if key not in RUN_KEYS}


def _proportion(k: int, n: int) -> dict[str, Any]:
    return stats.proportion(k, n).to_dict()


def _interval(values: Sequence[float], seed: int, resamples: int) -> dict[str, Any] | None:
    if not values:
        return None
    _, low, high = stats.bootstrap(values, resamples=resamples, seed=seed)
    return {"low": round(low, stats.DIGITS), "high": round(high, stats.DIGITS)}


def result_from_ledger(
    ledger: Ledger, *, seed: int = SEED, resamples: int = stats.RESAMPLES
) -> dict[str, Any]:
    header, cases = ledger.header, ledger.cases
    primary = header["primary"]
    questions: dict[str, Any] = {}
    for question, labels in header["questions"].items():
        entries = [c["q"][question] for c in cases if question in c["q"]]
        labelled = [e for e in entries if e[TRUTH] is not None]
        if not labelled:
            continue
        truths = [e[TRUTH] for e in labelled]
        predictions = [e[PREDICTED] for e in labelled]
        scored = [e for e in labelled if e[PREDICTED] is not None]
        confidences = [e[CONFIDENCE] for e in scored]
        correct = [e[PREDICTED] == e[TRUTH] for e in scored]
        summary = metrics.classification(truths, predictions, labels)
        summary["proportion"] = _proportion(
            sum(t == p for t, p in zip(truths, predictions, strict=True)), len(labelled)
        )
        summary["calibration"] = metrics.calibration(confidences, correct)
        summary["calibration"]["equal_mass"] = stats.equal_mass_ece(confidences, correct)
        distributions = [dict(zip(labels, e[PROBABILITIES], strict=True)) for e in scored]
        summary["brier"] = metrics.brier(distributions, [e[TRUTH] for e in scored])
        per_case = [
            metrics.brier([d], [e[TRUTH]]) or 0.0
            for d, e in zip(distributions, scored, strict=True)
        ]
        summary["brier_interval"] = _interval(per_case, seed, resamples)
        questions[question] = summary

    def truth(case: Mapping[str, Any], q: str = primary) -> str | None:
        entry = case["q"].get(q)
        return None if entry is None else entry[TRUTH]

    def predicted(case: Mapping[str, Any], q: str) -> str | None:
        entry = case["q"].get(q)
        return None if entry is None else entry[PREDICTED]

    n = len(cases)
    outcomes = Counter(c["o"] for c in cases)
    labelled_cases = [c for c in cases if truth(c) is not None]
    auto = [c for c in labelled_cases if c["o"] == "AUTO"]
    auto_wrong = [c for c in auto if c["r"] != truth(c)]
    reviewed = [c for c in labelled_cases if c["o"] == "HUMAN_REVIEW"]
    recommended = [c for c in labelled_cases if c["r"] is not None]
    recommended_right = sum(c["r"] == truth(c) for c in recommended)

    critical = []
    critical_proportions = {}
    for index, (question, label) in enumerate(header["critical"]):
        positives = [c for c in cases if index in c["k"]]
        detected = sum(predicted(c, question) == label for c in positives)
        critical.append(
            {
                "question": question,
                "label": label,
                "n": len(positives),
                "detected": detected,
                "detection_rate": metrics.safe_div(detected, len(positives)),
                "proportion": _proportion(detected, len(positives)),
                "auto_missed": sum(c["o"] == "AUTO" for c in positives),
                "missed_cases": [c["c"] for c in positives if predicted(c, question) != label],
            }
        )
        critical_proportions[f"{question}={label}"] = critical[-1]["proportion"]

    primary_scored = [c["q"][primary] for c in labelled_cases if predicted(c, primary) is not None]
    confident = [(e[CONFIDENCE], e[PREDICTED] == e[TRUTH]) for e in primary_scored]
    costs = [c["u"][0] for c in cases]
    cost_known = all(cost is not None for cost in costs)
    models: dict[str, dict[str, int]] = {}
    for case in cases:
        for question, entry in case["q"].items():
            if entry[MODEL] is not None:
                model = header["models"][entry[MODEL]]
                models.setdefault(question, {}).setdefault(model, 0)
                models[question][model] += 1

    primary_accuracy = questions.get(primary, {}).get("proportion")
    return {
        "format": FORMAT,
        "agent": header["agent"],
        "providers": header["providers"],
        "policy": {
            "risk_level": header["policy"]["risk_level"],
            "thresholds": header["policy"]["thresholds"],
            "governance_override": header["policy"]["governance"],
        },
        "dataset": header["dataset"],
        "ledger": {"id": ledger.id, "cases": n},
        "seeds": {"bootstrap": seed, "resamples": resamples, "level": stats.LEVEL},
        "primary": primary,
        "questions": questions,
        "proportions": {
            "accuracy": primary_accuracy,
            "automation": _proportion(outcomes.get("AUTO", 0), n),
            "false_automation": _proportion(len(auto_wrong), len(labelled_cases)),
            "automated_correct": _proportion(len(auto) - len(auto_wrong), len(auto)),
            "abstention": _proportion(outcomes.get("ABSTAIN", 0), n),
            "recommendation": _proportion(recommended_right, len(recommended)),
            "critical": critical_proportions,
        },
        "decisions": {
            "outcome_counts": {key: outcomes.get(key, 0) for key in OUTCOMES},
            "outcome_rates": metrics.rates(outcomes, OUTCOMES),
            "abstention_rate": metrics.safe_div(outcomes.get("ABSTAIN", 0), n),
            "automation": {
                "n": len(auto),
                "accuracy": metrics.safe_div(len(auto) - len(auto_wrong), len(auto)),
                "false_automation_rate": metrics.safe_div(len(auto_wrong), len(auto)),
                "false_automation_share": metrics.safe_div(len(auto_wrong), n),
            },
            "review": {
                "n": len(reviewed),
                # A reviewer who knows the truth would change every wrong recommendation.
                "expected_override_rate": metrics.safe_div(
                    sum(c["r"] != truth(c) for c in reviewed), len(reviewed)
                ),
            },
            "recommendation_accuracy": metrics.safe_div(recommended_right, len(recommended)),
            "critical": critical,
        },
        "threshold_sweep": metrics.threshold_sweep(
            [conf for conf, _ in confident], [ok for _, ok in confident]
        ),
        "cost": {
            "total_usd": sum(c for c in costs if c is not None) if cost_known else None,
            "per_decision_usd": (
                sum(c for c in costs if c is not None) / n if cost_known and n else None
            ),
            "input_tokens": sum(c["u"][1] for c in cases),
            "output_tokens": sum(c["u"][2] for c in cases),
        },
        "models": models,
        "errors": dict(
            Counter(e[ERROR] for c in cases for e in c["q"].values() if e[ERROR] is not None)
        ),
        "subgroups": _subgroups(header, cases, primary),
        "misclassified": [
            {
                "case": c["c"],
                "truth": truth(c),
                "predicted": predicted(c, primary),
                "confidence": None
                if predicted(c, primary) is None
                else round(c["q"][primary][CONFIDENCE], 4),
                "outcome": c["o"],
            }
            for c in labelled_cases
            if predicted(c, primary) != truth(c)
        ][:50],
    }


def critical_cases(ledger: Ledger) -> list[dict[str, list[str]]]:
    """For each critical label, in the order of a result's `decisions.critical`: the ledger
    hashes of its cases that were `automated` (critical misses: no person read them, whatever
    the agent answered), and of those `undetected` that a person still read. A result counts
    them (`auto_missed`; `missed_cases`, which holds both kinds that were not detected); this
    says which, for whoever holds the dataset."""
    found = []
    for index, (question, label) in enumerate(ledger.header["critical"]):
        positives = [case for case in ledger.cases if index in case["k"]]
        automated = [case["c"] for case in positives if case["o"] == "AUTO"]
        undetected = [
            case["c"]
            for case in positives
            if case["o"] != "AUTO" and (case["q"].get(question) or [None, None])[PREDICTED] != label
        ]
        found.append({"automated": automated, "undetected": undetected})
    return found


def _subgroups(
    header: Mapping[str, Any], cases: Sequence[Mapping[str, Any]], primary: str
) -> dict[str, dict[str, dict[str, Any]]]:
    groups: dict[str, dict[str, dict[str, Any]]] = {}
    for attribute in header["subgroups"]:
        buckets: dict[str, list[Mapping[str, Any]]] = {}
        for case in cases:
            buckets.setdefault(case["a"].get(attribute, "unknown"), []).append(case)
        groups[attribute] = {}
        for value, members in sorted(buckets.items()):
            entries = [c["q"].get(primary) for c in members]
            labelled = [
                (c, e)
                for c, e in zip(members, entries, strict=True)
                if e is not None and e[TRUTH] is not None
            ]
            auto = [(c, e) for c, e in labelled if c["o"] == "AUTO"]
            groups[attribute][value] = {
                "n": len(members),
                "labelled": len(labelled),  # what accuracy is of
                "automated": len(auto),  # what the false automation rate is of
                "accuracy": metrics.safe_div(
                    sum(e[PREDICTED] == e[TRUTH] for _, e in labelled), len(labelled)
                ),
                "auto_rate": metrics.safe_div(sum(c["o"] == "AUTO" for c in members), len(members)),
                "abstention_rate": metrics.safe_div(
                    sum(c["o"] == "ABSTAIN" for c in members), len(members)
                ),
                "false_automation_rate": metrics.safe_div(
                    sum(c["r"] != e[TRUTH] for c, e in auto), len(auto)
                ),
            }
    return groups


def counted(rate: Any, n: Any) -> stats.Proportion | None:
    """k of n, with its interval, for a rate a report states over n cases (k = rate x n: exact
    for any n a report holds). None when that is not known: n missing or not a whole count (up
    to MAX_N), the rate missing over some cases, or not a share of those n cases (outside
    [0, 1], or rate x n not a whole number). A report can be someone else's file (an agent's
    benchmark, a registry upload): what it says is checked before it is multiplied."""
    if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= MAX_N:
        return None
    if rate is None:
        return stats.proportion(0, 0) if n == 0 else None
    if isinstance(rate, bool) or not isinstance(rate, int | float) or not math.isfinite(rate):
        return None
    if not 0.0 <= rate <= 1.0:
        return None
    k = round(rate * n)
    if abs(rate * n - k) > _SLACK:
        return None
    return stats.proportion(k, n)


def false_automation(report: Mapping[str, Any]) -> stats.Proportion | None:
    """Automated with a wrong recommendation, over every case labelled for the primary question:
    the count the report stores (`proportions`), else worked out from its rate."""
    stored = (report.get("proportions") or {}).get("false_automation")
    if stored is not None:
        return stats.proportion(stored["k"], stored["n"])
    automation = report["decisions"]["automation"]
    wrong = counted(automation["false_automation_rate"], automation["n"])
    labelled = report["questions"].get(report["primary"], {}).get("n")
    return None if wrong is None or labelled is None else stats.proportion(wrong.k, labelled)


def sweep(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The threshold sweep with the counts each row is of (`scored` cases, `chosen` at or above
    the threshold), as the report stores them; for a row that does not, `scored` is the primary
    question's calibration bins, which hold exactly the cases the sweep is over, and `chosen`
    follows from the coverage."""
    question = report["questions"].get(report["primary"]) or {}
    bins = (question.get("calibration") or {}).get("bins")
    scored = sum(b["n"] for b in bins) if bins is not None else None
    rows = []
    for row in report.get("threshold_sweep", []):
        if "chosen" not in row:
            coverage = row.get("coverage")
            if scored == 0:  # nothing scored: nothing at or above any threshold
                chosen: int | None = 0
            else:
                chosen = None if scored is None or coverage is None else round(coverage * scored)
            row = {**row, "scored": scored, "chosen": chosen}
        rows.append(dict(row))
    return rows


def subgroup_labelled(report: Mapping[str, Any], members: Mapping[str, Any]) -> int | None:
    """How many of a subgroup's members are labelled for the primary question, which its
    accuracy is over, as the report stores it (`labelled`); else known only when every case is
    labelled."""
    if "labelled" in members:
        return int(members["labelled"])
    question = report["questions"].get(report["primary"]) or {}
    every = question.get("n") is not None and question.get("n") == report["dataset"].get("n")
    return int(members["n"]) if every else None
