# SPDX-License-Identifier: Apache-2.0
"""Plain-text rendering of evaluation reports and comparisons for the terminal."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mendari.evals import figures, stats
from mendari.evals.result import MIN_POSITIVES, counted, false_automation, normalize


def pct(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value * 100:.{digits}f}%"


def num(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def shown(p: stats.Proportion | Mapping[str, Any] | None) -> str:
    """A proportion as k of n and its exact 95% interval: "596 of 600 [98.3-99.8%]". The
    interval is worked out from k and n (`stats.interval`), never read from a stored bound, so
    the card, the command line, the dashboard and the Hub print the same text (the dashboard's
    and the Hub's ports are checked against this, tests/fixtures/shown.json)."""
    if p is None:
        return "n/a"
    k, n = (int(p["k"]), int(p["n"])) if isinstance(p, Mapping) else (p.k, p.n)
    if not n:
        return "no cases"
    return f"{k} of {n} [{bounds(k, n)}%]"


def bounds(k: int, n: int) -> str:
    """The interval's bounds as percentages, "98.3-99.8"."""
    return interval_text(k, n, *stats.interval(k, n))


def interval_text(k: int, n: int, low: float, high: float) -> str:
    """k of n's bounds `low` and `high` as percentages. A bound strictly inside (0, 100) never
    reads as either: the lower one is above 0 when k is, and below 100 always; the upper one is
    below 100 when k is below n, and above 0 always."""
    under = f"<{10.0**-BOUND_DIGITS:.{BOUND_DIGITS}f}"
    over = f">{100 - 10.0**-BOUND_DIGITS:.{BOUND_DIGITS}f}"
    low_text, high_text = percent(low), percent(high)
    if float(low_text) == 0.0 and k > 0:
        low_text = under
    elif float(low_text) == 100.0:
        low_text = over
    if float(high_text) == 100.0 and k < n:
        high_text = over
    elif float(high_text) == 0.0:
        high_text = under
    return f"{low_text}-{high_text}"


BOUND_DIGITS = 4


def percent(value: float) -> str:
    """A bound as a percentage, with the digits it needs (up to 4) never to read as 0 or 100
    when it is not (an upper bound of 99.96% is not "100.0")."""
    digits = 1
    while (
        digits < BOUND_DIGITS
        and 0.0 < value < 1.0
        and f"{value * 100:.{digits}f}" in (f"{0:.{digits}f}", f"{100:.{digits}f}")
    ):
        digits += 1
    return f"{value * 100:.{digits}f}"


def evidence(positives: int, *, short: bool = False) -> str:
    """Beside a critical label's figure: too few positives to rank or compare on (LP-10)."""
    if positives >= MIN_POSITIVES:
        return ""
    return (
        "; insufficient evidence"
        if short
        else (f" (insufficient evidence: under {MIN_POSITIVES} positives)")
    )


INTERVALS = "k of n [exact 95% interval, Clopper-Pearson]"


def equal_mass(question: Mapping[str, Any]) -> str:
    """The equal-mass ECE (ten equal-count bins), "not estimable" with why when a bin would
    hold too few cases, or n/a for a report from before it was computed (mendari.eval/1)."""
    stored = (question.get("calibration") or {}).get("equal_mass")
    if not isinstance(stored, Mapping):
        return "n/a"
    if stored.get("ece") is None:
        return f"not estimable ({stored.get('reason', 'too few cases')})"
    return num(stored["ece"])


def report_text(report: dict[str, Any]) -> str:
    primary = report["primary"]
    q = report["questions"].get(primary)
    d = report["decisions"]
    counts = d["outcome_counts"]
    total = sum(counts.values())
    auto = d["automation"]
    providers = "  ".join(f"{slot}={p['variant']}" for slot, p in report["providers"].items())
    lines = [
        f"Pack        {report['pack']['id']} {report['pack']['version']} "
        f"({report['pack']['digest'][:19]}…)",
        f"Providers   {providers}",
        f"Dataset     {report['dataset']['name']} (n={report['dataset']['n']})",
        f"Proportions {INTERVALS}",
        "",
    ]
    if q:
        lines += [
            f"{primary.capitalize()} accuracy".ljust(26)
            + f"{shown(counted(q['accuracy'], q['n']))}   macro-F1 {num(q['macro_f1'])}",
            "Calibration".ljust(26)
            + f"ECE {num(q['calibration']['ece'])}   equal-mass ECE {equal_mass(q)}   "
            + f"Brier {num(q['brier'])}",
        ]
    lines += [
        *[
            f"Outcome {k.replace('HUMAN_', '').lower()}".ljust(26)
            + shown(stats.proportion(v, total))
            for k, v in counts.items()
        ],
        "Automated, correct".ljust(26) + shown(counted(auto["accuracy"], auto["n"])),
        "False automation".ljust(26) + f"{shown(false_automation(report))} of labelled cases",
        "Expected override".ljust(26)
        + f"{shown(counted(d['review']['expected_override_rate'], d['review']['n']))} of reviewed",
    ]
    for item in d["critical"]:
        lines.append(
            f"Critical {item['question']}={item['label']}".ljust(25)
            + f" {shown(stats.proportion(item['detected'], item['n']))} detected, "
            f"{item['auto_missed']} automated{evidence(item['n'])}"
        )
    latency = normalize(report)["run"].get("latency_ms") or {"p50": None, "p95": None}
    cost = report["cost"]
    lines += [
        "Latency".ljust(26) + f"p50 {num(latency['p50'], 2)} ms   p95 {num(latency['p95'], 2)} ms",
        "Cost".ljust(26)
        + (
            "unknown (provider pricing not configured)"
            if cost["per_decision_usd"] is None
            else f"${cost['per_decision_usd']:.5f} per decision"
        ),
    ]
    if report["errors"]:
        lines.append(
            "Provider errors".ljust(26)
            + ", ".join(f"{k}: {v}" for k, v in report["errors"].items())
        )
    return "\n".join(lines)


def comparison_text(comparison: dict[str, Any], max_findings: int = 6) -> str:
    if comparison["status"] == "unknown":
        return f"Comparison unavailable: {comparison['reason']}"
    base, local = comparison["baseline"], comparison["local"]
    worked = figures.comparison(comparison) or {}  # the same figures the dashboard shows
    rows = [
        (
            f"Public benchmark ({base['dataset']['name']}, n={base['dataset']['n']}):",
            worked.get("baseline"),
        ),
        (
            f"Local performance ({local['dataset']['name']}, n={local['dataset']['n']}):",
            worked.get("local"),
        ),
    ]
    width = max(len(label) for label, _ in rows) + 2
    lines = [label.ljust(width) + shown(figure) for label, figure in rows] + [""]
    tolerance = comparison["tolerance"] * 100
    if comparison["status"] == "ok":
        lines.append(f"No degradation beyond tolerance ({tolerance:.1f} pts).")
        return "\n".join(lines)
    heading = (
        "Performance degradation detected"
        if comparison["status"] == "degraded"
        else "Warning: localized degradation"
    )
    lines.append(
        f"{heading} ({comparison['delta'] * 100:+.1f} pts overall; tolerance {tolerance:.1f}):"
    )
    findings = comparison["findings"]
    for finding, figure in list(zip(findings, worked.get("findings", []), strict=True))[
        :max_findings
    ]:
        if finding["kind"] == "critical":
            lines.append(
                f"  - {finding['subject']}: {shown(figure['automated'])} positives"
                f"{evidence(finding['n'], short=True)}"
            )
        elif finding["kind"] == "calibration":
            lines.append(
                f"  - {finding['subject']}: {num(finding['baseline'])} -> {num(finding['local'])}"
            )
        else:
            before = shown(figure["baseline"])
            if figure["overall"]:  # the benchmark has no such subgroup
                before = f"benchmark overall {before}"
            lines.append(f"  - {finding['subject']}: {before} -> {shown(figure['local'])}")
    if len(findings) > max_findings:
        lines.append(f"  … and {len(findings) - max_findings} smaller findings in the JSON report")
    return "\n".join(lines)
