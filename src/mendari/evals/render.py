# SPDX-License-Identifier: Apache-2.0
"""Plain-text rendering of evaluation reports and comparisons for the terminal, and the
scoreboard's tables in Markdown (`scoreboard_markdown`)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from mendari.evals import figures, runs, scoreboard, stats
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


# --------------------------------------------------------------------------------------------
# The scoreboard (LP-17d)
# --------------------------------------------------------------------------------------------

BOOTSTRAP = "value [95% bootstrap interval, 2,000 resamples, seed 0]"


def scoreboard_markdown(board: Mapping[str, Any]) -> str:
    """A board (`scoreboard.board`) in Markdown: one section per pack and set, each with its
    standing ("public set: regression check, not a ranking"), the insufficient-evidence banner
    where it applies, each arm's status, a measured arm's configurations that have no entry, and
    a table with a column per entry: its marks, its
    scope, then its figures. A proportion is k of n with its exact 95% interval (`shown`), a
    mean is its value with its bootstrap interval, the grade's bound is "lower bound X at Y%,
    simultaneous across L labels", and ECE is a description with no interval. An entry that is
    not published (`scoreboard.published`: its disclosure pending, or no longer the file's)
    shows its scope and none of its figures. Nothing is ordered by a figure, and nothing names a
    case."""
    lines = [
        f"Grading method {board['method']['version']} (sha256 `{board['method']['sha256']}`). "
        f"Proportions are {INTERVALS}; means are {BOOTSTRAP}.",
    ]
    for table in board["tables"]:
        lines += ["", *_board_table(table)]
    return "\n".join(lines) + "\n"


def _board_table(table: Mapping[str, Any]) -> list[str]:
    lines = [
        f"## {table['pack']}: `{table['set']}`",
        "",
        f"{_sentence(table['ranking']['reason'])} {_sentence(table['evaluation'])}",
    ]
    if table["banner"] is not None:
        lines += [
            "",
            "> **Insufficient evidence.** A critical label here has fewer distinct positives than "
            "the method's minimum, so no figure on this set can show that its recall floor is met:",
        ]
        lines += [
            f"> - `{short['configuration']}`: {short['question']}={short['label']}, "
            f"{short['positives']} distinct positives (needs {short['needed']})"
            for short in table["banner"]["short"]
        ]
    arms = "; ".join(f"{arm['name']}: {arm['status']}" for arm in table["arms"])
    lines += ["", f"Arms: {arms}."]
    if table.get("not_measured"):
        listed = "; ".join(
            f"{item['name']}: `{item['configuration']}`" for item in table["not_measured"]
        )
        lines += ["", f"Not measured yet, in an arm measured here: {listed}."]
    lines.append("")
    shown_entries = table["entries"]
    heads = [f"{item['arm_name']}: `{item['configuration']}`" for item in shown_entries]
    lines += ["| | " + " | ".join(heads) + " |", "|---|" + "---|" * len(heads)]
    for label, cells in _rows(shown_entries):
        lines.append(f"| {label} | " + " | ".join(_cell(cell) for cell in cells) + " |")
    for item in shown_entries:
        if item["published"]:
            lines += _subgroup_table(item)
    return lines


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:] + "."


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _rows(items: Sequence[Mapping[str, Any]]) -> list[tuple[str, list[str]]]:
    """The table's rows: (label, one cell per entry), from each entry's column (`_column`)."""
    pairs: list[tuple[str, str]] = []
    for item in items:
        for critical in item["entry"]["figures"]["critical"]:
            if (critical["question"], critical["label"]) not in pairs:
                pairs.append((critical["question"], critical["label"]))
    columns = [_column(item, pairs) for item in items]
    return [
        (label, [column[i][1] for column in columns]) for i, (label, _) in enumerate(columns[0])
    ]


def _column(item: Mapping[str, Any], pairs: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    """One entry's cells, each with its row's label: its marks and scope, then its figures, or
    "not published: disclosure pending" in place of each figure. A critical label the entry
    does not have (another digest's) reads n/a."""
    scope = item["entry"]["scope"]
    cells = [
        ("Marks", "; ".join(item["marks"])),
        ("Pack", f"{scope['pack']['version']}, `{scope['pack']['digest']}`"),
        ("Set", _set_text(scope["set"])),
        ("Method", f"{scope['method']['version']}, sha256 `{scope['method']['sha256'][:12]}…`"),
        ("Measured on", scope["measured_on"]),
        ("Models", _models_text(scope["models"]["by_type"])),
        # An unpublished entry's statement is pending, or no longer the disclosures file's: the
        # statement it holds is never shown as if it were current.
        (
            "Disclosure",
            _disclosure_text(scope["disclosure"])
            if item["published"]
            else scoreboard.DISCLOSURE_PENDING,
        ),
        ("Status", scope["status"]),
    ]
    found = item["entry"]["figures"]
    critical = {(c["question"], c["label"]): c for c in found["critical"]}
    shown_figures: list[tuple[str, str]] = []
    for question, label in pairs:
        name = f"{question}={label}"
        held = critical.get((question, label))
        shown_figures += [
            (f"Detected {name}", "n/a" if held is None else _detected_text(held)),
            (f"Grade's bound {name}", "n/a" if held is None else _bound_text(held["bound"])),
            (f"Critical automated {name}", "n/a" if held is None else shown(held["automated"])),
        ]
    shown_figures += [
        ("False automation", f"{shown(found['false_automation'])} of labelled cases"),
        ("AUTO error rate", _auto_error_text(found["auto_error_rate"])),
        ("Automation", shown(found["automation"])),
        ("Accuracy", shown(found["accuracy"])),
        ("Macro-F1", _mean_text(found["macro_f1"], 3)),
        ("Brier", _mean_text(found["brier"], 3)),
        ("Brier reliability", _reliability_text(found["brier_reliability"])),
        ("ECE (equal-mass)", _ece_text(found["ece"])),
        ("Cost per 1,000 decisions", _cost_text(found["cost_per_1000"])),
        ("Latency p50", _latency_text(found["latency_ms"], "p50")),
        ("Latency p95", _latency_text(found["latency_ms"], "p95")),
        ("Weighted figures", _weighted_text(found["weighted"])),
        *_replicate_rows(item["entry"]),
    ]
    if not item["published"]:
        shown_figures = [(label, scoreboard.DISCLOSURE_PENDING) for label, _ in shown_figures]
    return cells + shown_figures


def _replicate_rows(entry: Mapping[str, Any]) -> list[tuple[str, str]]:
    """An entry's replicates (LP-17h): their count and headline, the case flip rate (k of n with
    its interval) and the spread of each figure that differs across them (its minimum to
    maximum, a description with no interval). A local configuration is deterministic: one
    replicate, no flips."""
    held = entry["figures"].get("replicates")
    if held is None:
        one = "1 (local: deterministic)" if entry["configuration"]["local"] else "1"
        return [
            ("Replicates", one),
            ("Case flip rate", "none: one replicate"),
            ("Spread across replicates", "none: one replicate"),
        ]
    differing = []
    for name, spread in held["spread"].items():
        if spread.get("status") == runs.NOT_ESTIMABLE or spread["min"] == spread["max"]:
            continue
        differing.append(f"{name} {spread['min']:.4f} to {spread['max']:.4f}")
    same = "every other figure the same in each" if differing else "every figure the same in each"
    return [
        ("Replicates", f"{held['count']}, replicate {held['headline']} the headline"),
        ("Case flip rate", f"{shown(held['flip_rate'])} of cases, between any two replicates"),
        ("Spread across replicates", "; ".join([*differing, same])),
    ]


def _auto_error_text(found: Mapping[str, Any]) -> str:
    return f"{shown(found)} of AUTO cases" if found["n"] else "no AUTO cases"


def _set_text(found: Mapping[str, Any]) -> str:
    return (
        f"{found['kind']}, sha256 `{found['sha256'][:12]}…`, {found['n']} cases, "
        f"{found['distinct_inputs']} distinct inputs ({found['norm']})"
    )


def _models_text(by_type: Mapping[str, Sequence[str]]) -> str:
    if not by_type:
        return "none reported"
    return "; ".join(f"{kind}: {', '.join(models)}" for kind, models in by_type.items())


def _disclosure_text(found: Mapping[str, Any]) -> str:
    if found["status"] != "stated":
        return str(found["status"])
    return "; ".join(f"{kind}: {text}" for kind, text in found["statements"].items())


def _detected_text(found: Mapping[str, Any]) -> str:
    distinct = found["distinct"]
    evidence = found["evidence"]
    text = (
        f"{shown(found['detected'])}; {distinct['positives']} distinct positives, "
        f"{distinct['detected']} detected"
    )
    if evidence["status"] != scoreboard.SUFFICIENT:
        text += f"; {evidence['status']} (needs {evidence['needed']})"
    return text


def _level(level: float) -> str:
    """A level as a percentage, with up to two decimals: 95, 97.5, 98.75, 99.17."""
    return f"{level * 100:.2f}".rstrip("0").rstrip(".")


def _bound_text(found: Mapping[str, Any]) -> str:
    """The grade's bound: "lower bound X at Y%, simultaneous across L labels", over the distinct
    groups it is of."""
    k, n, labels = int(found["k"]), int(found["n"]), int(found["labels"])
    if not n:
        return "no distinct positives"
    low, high = stats.clopper_pearson(k, n, float(found["level"]))
    low_text, _ = interval_text(k, n, low, high).split("-", 1)
    across = f"{labels} label{'' if labels == 1 else 's'}"
    return (
        f"lower bound {low_text}% at {_level(float(found['level']))}%, simultaneous across "
        f"{across} ({k} of {n} distinct groups detected)"
    )


def _bounds(low: float | None, high: float | None, digits: int) -> str:
    if low is None or high is None:
        return "no interval"
    return f"[{low:.{digits}f}-{high:.{digits}f}]"


def _mean_text(found: Mapping[str, Any] | None, digits: int) -> str:
    if found is None:
        return "n/a"
    return f"{found['estimate']:.{digits}f} {_bounds(found['low'], found['high'], digits)}"


def _reliability_text(found: Mapping[str, Any] | None) -> str:
    if found is None:
        return "n/a"
    if found["status"] != "ok":
        return f"not estimable ({found['reason']})"
    return _mean_text(found, 4)


def _ece_text(found: Mapping[str, Any] | None) -> str:
    if found is None:
        return "n/a"
    if found["status"] != "ok":
        return f"not estimable ({found['reason']})"
    return f"{found['value']:.3f}, a description: no interval"


def _cost_text(found: Mapping[str, Any]) -> str:
    if found["status"] == "none":
        return str(found["text"])
    if found["status"] == scoreboard.UNKNOWN:
        return f"unknown ({found['reason']})"
    return f"{found['currency']} {_mean_text(found, 4)}"


def _latency_text(found: Mapping[str, Any], name: str) -> str:
    return f"{_mean_text(found[name], 2)} ms, {found['note']}"


def _weighted_text(found: Mapping[str, Any]) -> str:
    if found["status"] == scoreboard.AS_SAMPLED:
        return scoreboard.AS_SAMPLED
    parts = []
    for name, title in (
        ("accuracy", "accuracy"),
        ("false_automation", "false automation"),
        ("automation", "automation"),
        ("auto_error_rate", "AUTO error rate"),
    ):
        share = found[name]
        if share["estimate"] is None:
            parts.append(f"{title} no cases")
            continue
        parts.append(
            f"{title} {share['estimate']:.3f} {_bounds(share['low'], share['high'], 3)} "
            f"(effective n {share['effective_n']:.1f} of {share['n']})"
        )
    return "; ".join(parts)


def _subgroup_table(item: Mapping[str, Any]) -> list[str]:
    groups = item["entry"]["figures"]["subgroups"]
    if not groups:
        return []
    lines = [
        "",
        f"Subgroups of {item['arm_name']}: `{item['configuration']}` (a figure over fewer than "
        f'{scoreboard.method.SUBGROUP_MIN} cases reads "{scoreboard.TOO_FEW}"):',
        "",
        "| Subgroup | Cases | Accuracy | Automation | AUTO error rate |",
        "|---|---|---|---|---|",
    ]
    for attribute, values in groups.items():
        for value, cell in values.items():
            if cell.get("status") == scoreboard.TOO_FEW:
                figures_text = [scoreboard.TOO_FEW] * 3
            else:
                figures_text = [
                    _counted_text(cell[name])
                    for name in ("accuracy", "automation", "auto_error_rate")
                ]
            lines.append(
                f"| {_cell(f'{attribute}={value}')} | {cell['n']} | "
                + " | ".join(figures_text)
                + " |"
            )
    return lines


def _counted_text(found: Mapping[str, Any]) -> str:
    if found.get("status") == scoreboard.TOO_FEW:
        return f"{scoreboard.TOO_FEW} ({found['n']} cases)"
    return shown(found)
