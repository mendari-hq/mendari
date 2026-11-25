# SPDX-License-Identifier: Apache-2.0
"""What a labelled set covers (`mendari.coverage/1`, LP-11 requirement 8): how many cases, how
many of them are distinct, how many of each label and of each critical label once duplicates are
removed, whether duplicates disagree, and the mix of languages, sources and strata.

Duplicates are counted under the stricter rule, `norm/v1` (`evals.canonical.normalised_input`):
two cases whose inputs differ only in case, spacing, punctuation and symbols, invisible
characters, Unicode form or the spelling of a whole number are one case. A critical label with
fewer than `result.MIN_POSITIVES` distinct positives is `insufficient`, and a warning says so.
The language, source and stratum are read from each case's `attributes`; one that is missing is
counted as `unknown`, never inferred.

The report names labels, counts and attribute values, never a case's input. Duplicate case ids are
listed only when asked (`ids=True`, for a public file a person holds)."""

from __future__ import annotations

from collections import Counter
from typing import Any

from mendari.core.spec import AgentManifest
from mendari.errors import MendariError
from mendari.evals.canonical import NORM_VERSION, canonical_input, normalised_input
from mendari.evals.harness import Dataset, record_problems
from mendari.evals.ledger import ATTRIBUTE_CHARS
from mendari.evals.result import MIN_POSITIVES

FORMAT = "mendari.coverage/1"
MIX = ("language", "source", "stratum")
UNKNOWN = "unknown"


def coverage(
    manifest: AgentManifest, dataset: Dataset, *, agent: str, ids: bool = False
) -> dict[str, Any]:
    """The coverage report of `dataset` for the agent `manifest` describes (named `agent`). A
    dataset whose records the agent cannot read (`record_problems`) is refused, as an evaluation
    refuses it."""
    problems = record_problems(manifest, dataset)
    if problems:
        more = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
        raise MendariError(f"{problems[0]}{more}")
    records = dataset.records
    exact = {canonical_input(record["input"]) for record in records}
    groups: dict[str, list[int]] = {}
    for index, record in enumerate(records):
        groups.setdefault(normalised_input(record["input"]), []).append(index)
    group_of = {index: key for key, members in groups.items() for index in members}

    questions: dict[str, Any] = {}
    for question in manifest.questions:
        cases: Counter[str] = Counter()
        distinct: dict[str, set[str]] = {label: set() for label in manifest.labels(question)}
        for index, record in enumerate(records):
            label = record.get("labels", {}).get(question)
            if label is not None:
                cases[label] += 1
                distinct[label].add(group_of[index])
        questions[question] = {
            "labelled": sum(cases.values()),
            "labels": {
                label: {"cases": cases[label], "distinct": len(keys)}
                for label, keys in distinct.items()
            },
        }

    critical = []
    for item in manifest.evaluation.critical:
        counted = questions[item.question]["labels"][item.label]
        critical.append(
            {
                "question": item.question,
                "label": item.label,
                "cases": counted["cases"],
                "distinct": counted["distinct"],
                "insufficient": counted["distinct"] < MIN_POSITIVES,
            }
        )

    duplicated = [members for members in groups.values() if len(members) > 1]
    conflicting = sum(1 for members in duplicated if _disagree(records, members))
    mix = {name: dict(sorted(_mix(records, name).items())) for name in MIX}
    warnings = [
        f"{item['question']}={item['label']}: {item['distinct']} distinct positives, under "
        f"{MIN_POSITIVES}: too few to measure"
        for item in critical
        if item["insufficient"]
    ]
    if conflicting:
        warnings.append(f"{conflicting} groups of duplicate cases have labels that disagree")
    report: dict[str, Any] = {
        "format": FORMAT,
        "agent": agent,
        "dataset": {"name": dataset.name, "sha256": dataset.sha256},
        "norm": NORM_VERSION,
        "cases": len(records),
        "distinct": {"exact": len(exact), "normalised": len(groups)},
        "duplicate_groups": len(duplicated),
        "conflicting_groups": conflicting,
        "questions": questions,
        "critical": critical,
        "mix": mix,
        "warnings": warnings,
    }
    if ids:
        report["duplicates"] = [[records[index]["id"] for index in group] for group in duplicated]
    return report


def _disagree(records: list[dict[str, Any]], members: list[int]) -> bool:
    """Whether two cases of one duplicate group give one question different labels."""
    seen: dict[str, str] = {}
    for index in members:
        for question, label in records[index].get("labels", {}).items():
            if seen.setdefault(question, label) != label:
                return True
    return False


def _mix(records: list[dict[str, Any]], name: str) -> Counter[str]:
    values: Counter[str] = Counter()
    for record in records:
        value = record.get("attributes", {}).get(name)
        values[UNKNOWN if value is None or value == "" else str(value)[:ATTRIBUTE_CHARS]] += 1
    return values


def coverage_text(report: dict[str, Any]) -> str:
    """The report for a terminal: counts, labels and warnings, never a case's text."""
    distinct = report["distinct"]
    lines = [
        f"Coverage of {report['dataset']['name']} for {report['agent']} "
        f"({report['format']}, duplicates under {report['norm']})",
        f"  {report['cases']} cases, {distinct['normalised']} distinct "
        f"({distinct['exact']} exactly), {report['duplicate_groups']} groups of duplicates, "
        f"{report['conflicting_groups']} of them with labels that disagree",
    ]
    for question, counted in report["questions"].items():
        lines.append(f"  {question}: {counted['labelled']} labelled")
        for label, numbers in counted["labels"].items():
            lines.append(f"    {label}: {numbers['cases']} cases, {numbers['distinct']} distinct")
    if report["critical"]:
        lines.append("  Critical labels, distinct positives:")
        for item in report["critical"]:
            enough = f"insufficient (under {MIN_POSITIVES})" if item["insufficient"] else "enough"
            lines.append(
                f"    {item['question']}={item['label']}: {item['distinct']} of "
                f"{item['cases']} cases: {enough}"
            )
    for name, values in report["mix"].items():
        mixed = ", ".join(f"{value} {count}" for value, count in values.items())
        lines.append(f"  {name}: {mixed}")
    for group in report.get("duplicates", []):
        lines.append(f"  duplicates: {', '.join(str(case) for case in group)}")
    lines.extend(f"warning: {warning}" for warning in report["warnings"])
    return "\n".join(lines)
