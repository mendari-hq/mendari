# SPDX-License-Identifier: Apache-2.0
"""Plain-text rendering of evaluation reports and comparisons for the terminal."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mendari.evals import stats
from mendari.evals.result import MIN_POSITIVES, counted, false_automation, normalize


def pct(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value * 100:.{digits}f}%"


def num(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def shown(p: stats.Proportion | Mapping[str, Any] | None) -> str:
    """A proportion as k of n and its exact 95% interval: "596 of 600 [98.2-99.9%]"."""
    if p is None:
        return "n/a"
    if isinstance(p, Mapping):
        p = stats.Proportion(p["k"], p["n"], p.get("low"), p.get("high"))
    if not p.n or p.low is None or p.high is None:
        return "no cases"
    low = percent(p.low)
    high = percent(p.high)
    # With k above 0 the lower bound is above 0, and with k below n the upper bound is below
    # 100, however close a stored bound (6 places) or 4 printed decimals come to either.
    if p.k > 0 and float(low) == 0.0:
        low = f"<{10.0**-BOUND_DIGITS:.{BOUND_DIGITS}f}"
    if p.k < p.n and float(high) == 100.0:
        high = f">{100 - 10.0**-BOUND_DIGITS:.{BOUND_DIGITS}f}"
    return f"{p.k} of {p.n} [{low}-{high}%]"


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
            + f"ECE {num(q['calibration']['ece'])}   Brier {num(q['brier'])}",
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
    rows = [
        (f"Public benchmark ({base['dataset']['name']}, n={base['dataset']['n']}):", base),
        (f"Local performance ({local['dataset']['name']}, n={local['dataset']['n']}):", local),
    ]
    width = max(len(label) for label, _ in rows) + 2
    lines = [
        label.ljust(width) + shown(counted(side["accuracy"], side.get("n"))) for label, side in rows
    ] + [""]
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
    for finding in findings[:max_findings]:
        if finding["kind"] == "critical":
            automated = shown(stats.proportion(finding["local"], finding["n"]))
            lines.append(
                f"  - {finding['subject']}: {automated} positives"
                f"{evidence(finding['n'], short=True)}"
            )
        elif finding["kind"] == "calibration":
            lines.append(
                f"  - {finding['subject']}: {num(finding['baseline'])} -> {num(finding['local'])}"
            )
        else:
            before = shown(counted(finding["baseline"], finding.get("baseline_n")))
            if finding.get("baseline_scope") == "overall":  # the benchmark has no such subgroup
                before = f"benchmark overall {before}"
            after = shown(counted(finding["local"], finding.get("local_n")))
            lines.append(f"  - {finding['subject']}: {before} -> {after}")
    if len(findings) > max_findings:
        lines.append(f"  … and {len(findings) - max_findings} smaller findings in the JSON report")
    return "\n".join(lines)
