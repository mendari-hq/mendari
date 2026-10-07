# SPDX-License-Identifier: Apache-2.0
"""Plain-text rendering of evaluation reports and comparisons for the terminal."""

from __future__ import annotations

from typing import Any

from mendari.evals.result import normalize


def pct(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value * 100:.{digits}f}%"


def num(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def report_text(report: dict[str, Any]) -> str:
    primary = report["primary"]
    q = report["questions"].get(primary)
    d = report["decisions"]
    rates = d["outcome_rates"]
    auto = d["automation"]
    wrong_auto = round((auto["false_automation_rate"] or 0) * auto["n"])
    providers = "  ".join(f"{slot}={p['variant']}" for slot, p in report["providers"].items())
    lines = [
        f"Pack        {report['pack']['id']} {report['pack']['version']} "
        f"({report['pack']['digest'][:19]}…)",
        f"Providers   {providers}",
        f"Dataset     {report['dataset']['name']} (n={report['dataset']['n']})",
        "",
    ]
    if q:
        lines += [
            f"{primary.capitalize()} accuracy".ljust(26)
            + f"{pct(q['accuracy'])}   macro-F1 {num(q['macro_f1'])}",
            "Calibration".ljust(26)
            + f"ECE {num(q['calibration']['ece'])}   Brier {num(q['brier'])}",
        ]
    lines += [
        "Outcomes".ljust(26)
        + "  ".join(f"{k.replace('HUMAN_', '')} {pct(v)}" for k, v in rates.items()),
        "Automation".ljust(26) + f"accuracy {pct(auto['accuracy'])}   false automation rate "
        f"{pct(auto['false_automation_rate'])} ({wrong_auto} of {auto['n']})",
        "Expected override rate".ljust(26)
        + f"{pct(d['review']['expected_override_rate'])} of reviewed",
    ]
    for item in d["critical"]:
        lines.append(
            f"Critical {item['question']}={item['label']}".ljust(25)
            + f" {item['detected']}/{item['n']} detected, {item['auto_missed']} automated"
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
    lines = [label.ljust(width) + pct(side["accuracy"]) for label, side in rows] + [""]
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
            lines.append(f"  - {finding['subject']}: {finding['local']} of {finding['n']}")
        elif finding["kind"] == "calibration":
            lines.append(
                f"  - {finding['subject']}: {num(finding['baseline'])} -> {num(finding['local'])}"
            )
        else:
            lines.append(
                f"  - {finding['subject']}: {pct(finding['baseline'])} -> {pct(finding['local'])} "
                f"(n={finding['n']})"
            )
    if len(findings) > max_findings:
        lines.append(f"  … and {len(findings) - max_findings} smaller findings in the JSON report")
    return "\n".join(lines)
