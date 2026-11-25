# SPDX-License-Identifier: Apache-2.0
"""Plain-text rendering of evaluation reports and comparisons for the terminal, and a report in
Markdown for a CI job summary (`report_markdown`)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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
    hold too few cases."""
    stored = question["calibration"]["equal_mass"]
    if stored.get("ece") is None:
        return f"not estimable ({stored.get('reason', 'too few cases')})"
    return num(stored["ece"])


def confidence_rows(report: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Each model variant of a run and whether its confidence is calibrated, in one clause
    (`evals/confidence.py`: "not calibrated", or what it was calibrated on); none for a run
    without a model variant or without the block."""
    run = report.get("run")
    block = run.get("calibration") if isinstance(run, Mapping) else None
    summary = block.get("summary") if isinstance(block, Mapping) else None
    if not isinstance(summary, list):
        return []
    return [
        (str(item["variant"]), str(item["words"]))
        for item in summary
        if isinstance(item, Mapping) and "variant" in item and "words" in item
    ]


def report_text(report: dict[str, Any]) -> str:
    primary = report["primary"]
    q = report["questions"].get(primary)
    d = report["decisions"]
    counts = d["outcome_counts"]
    total = sum(counts.values())
    auto = d["automation"]
    providers = "  ".join(f"{slot}={p['variant']}" for slot, p in report["providers"].items())
    lines = [
        f"Agent       {report['agent']['id']} {report['agent']['version']} "
        f"({report['agent']['digest'][:19]}…)",
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
        ("Confidence" if index == 0 else "").ljust(26) + f"{variant}: {words}"
        for index, (variant, words) in enumerate(confidence_rows(report))
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


CODE_CHARS = 80  # the most of a name from outside that a Markdown summary shows
NAMED_CASES = 20  # the most case ids a Markdown summary lists for one kind of critical case


def code(value: Any, limit: int = CODE_CHARS) -> str:
    """A name that comes from outside the manifest (a dataset, a model, a case's id) as inline
    code in Markdown: shown as it is, never rendered. It stays on one line and under `limit`
    characters, and holds no backtick that would end the code."""
    text = " ".join(str(value).split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    text = text.replace("`", "'")
    return f"`{text}`" if text else "(empty)"


def md_row(*cells: str) -> str:
    """A Markdown table's row: no cell ends early at a `|` of its own or breaks the line."""
    return "| " + " | ".join(_cell(cell) for cell in cells) + " |"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def report_markdown(
    report: Mapping[str, Any],
    critical: Sequence[Mapping[str, Sequence[str]]] = (),
    ids: Mapping[str, Any] | None = None,
) -> str:
    """An evaluation report as Markdown, for a CI job summary: the figures `report_text` gives
    first (the primary question's accuracy, the mix of outcomes, automation, each critical
    label), each k of n with its interval. With `critical` (`result.critical_cases`, from the
    run's ledger), the critical cases that went wrong are named: by the id `ids` gives for a
    ledger hash (known only where the dataset is), else by the hash, and never by their text.
    A name the manifest does not hold to a pattern (the dataset's, a model's, an id) is shown
    as code, never rendered."""
    agent, primary = report["agent"], report["primary"]
    question = report["questions"].get(primary)
    decisions = report["decisions"]
    counts = decisions["outcome_counts"]
    total = sum(counts.values())
    automation = decisions["automation"]
    providers = ", ".join(
        code(f"{slot}={provider['variant']}") for slot, provider in report["providers"].items()
    )
    models = sorted({model for by_model in report["models"].values() for model in by_model})
    answered = f" Answered by {', '.join(code(model) for model in models)}." if models else ""
    rows = []
    if question:
        accuracy = shown(counted(question["accuracy"], question["n"]))
        rows.append((f"{primary.capitalize()} accuracy", accuracy))
    rows += [
        (f"Outcome {code(outcome)}", shown(stats.proportion(count, total)))
        for outcome, count in counts.items()
    ]
    rows += [
        ("Automated, correct", shown(counted(automation["accuracy"], automation["n"]))),
        ("False automation", f"{shown(false_automation(report))} of labelled cases"),
    ]
    names = [code(f"{item['question']}={item['label']}") for item in decisions["critical"]]
    for name, item in zip(names, decisions["critical"], strict=True):
        detected = shown(stats.proportion(item["detected"], item["n"])) + evidence(item["n"])
        rows.append((f"Critical {name} detected", detected))
        automated = shown(stats.proportion(item["auto_missed"], item["n"]))
        rows.append((f"Critical {name} automated", automated))
    if report["errors"]:
        errors = ", ".join(f"{code(error)}: {count}" for error, count in report["errors"].items())
        rows.append(("Provider errors", errors))
    rows += [
        (f"Confidence of {code(variant)}", code(words, 200))
        for variant, words in confidence_rows(report)
    ]
    lines = [
        f"### Evaluation of {code(agent['id'])} {code(agent['version'])} on "
        f"{code(report['dataset']['name'])} ({total} cases)",
        "",
        f"Providers: {providers}.{answered} Proportions are {INTERVALS}.",
        "",
        md_row("Figure", "Result"),
        "|---|---|",
        *(md_row(*row) for row in rows),
    ]
    # The two ways a critical case goes wrong, each under the name it has everywhere: a critical
    # miss is a case that was automated (the row "automated" above, and what a limit or a target
    # on critical misses counts); a case that was not detected and that a person still read is
    # no miss, and is said in those words.
    kinds = (
        ("automated", "Critical misses: {name} cases automated, so no person read them"),
        ("undetected", "Critical {name} cases not detected, and read by a person"),
    )
    for name, cases in zip(names, critical, strict=False):
        for kind, words in kinds:
            found = cases.get(kind, ())
            if found:
                named = [code((ids or {}).get(case, case)) for case in found[:NAMED_CASES]]
                more = len(found) - len(named)
                listed = ", ".join(named) + (f" and {more} more" if more else "")
                lines += ["", f"{words.format(name=name)} ({len(found)}): {listed}"]
    return "\n".join(lines) + "\n"


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


# -- written output (`mendari.written/1`, evals/written.py) ---------------------------------------

# What a report made from canned grades says wherever it is shown.
DEMONSTRATION = "demonstration only: canned grades, nothing may be concluded from them"
# And one whose grades file says its graders are simulated.
SIMULATED = (
    "the graders are SIMULATED, not clinicians: where this report says clinicians, read "
    "simulated graders, and conclude nothing from it"
)
_LISTED = 8  # the cases a terminal names under each heading


def written_report_text(report: Mapping[str, Any], ids: Mapping[str, Any] | None = None) -> str:
    """A written-output report for a terminal: each criterion's clinician figures, the two
    agreements, the gate's state in words with its reasons, and the judged share only where the
    criterion is validated (otherwise "withheld"). Every share is k of n with its exact
    interval. Counts, coefficients and case hashes only; with `ids` (hash to case id, for
    whoever holds the dataset) the cases are named by id, as `mendari compare` names them."""
    agent, rubric, cases = report["agent"], report["rubric"], report["cases"]
    judge, floors, seeds = report["judge"], report["floors"], report["seeds"]
    missing = cases["no_output"]
    lines = [
        f"Written output: rubric {rubric['name']} of {agent['id']}@{agent['version']} "
        f"({report['format']})",
        f"  output {rubric['output']['name']} ({rubric['output']['source']}), dataset "
        f"{report['dataset']['name']}",
        f"  judge: {judge['model'] or 'no model named'}"
        + (f" ({DEMONSTRATION})" if judge["canned"] else "")
        + (
            "; the same model as the agent it grades, and models favour their own text"
            if judge["same_model_as_agent"]
            else ""
        ),
        f"  {cases['n']} cases: {cases['with_output']} with an output (none written "
        f"{missing['none']}, input refused {missing['refused']}, slot failed {missing['error']}); "
        f"{cases['judged']} judged; {cases['graded']} graded by clinicians, "
        f"{cases['graded_twice']} of them by two or more",
    ]
    if cases["judge_errors"]:
        failed = ", ".join(f"{code} {count}" for code, count in cases["judge_errors"].items())
        lines.append(f"  the judge could not grade: {failed} (never counted as a pass)")
    if cases["stale_grades"]:
        lines.append(
            f"  {cases['stale_grades']} grades are of texts this run did not produce: stale, "
            "counted and not used"
        )
    if report["graders"]:
        graders = ", ".join(f"{grader} {count}" for grader, count in report["graders"].items())
        lines.append(f"  graders: {graders}")
    if report.get("simulated_graders"):
        lines.append(f"  {SIMULATED}")
    lines.append(
        f"  floors: kappa's lower bound at least {floors['agreement']}; on a must-not-fail "
        f"criterion, the failures the judge finds at least {floors['must_pass_recall']} by their "
        f"lower bound; {floors['min_paired']} outputs graded by both, {floors['min_each_side']} "
        "on each side of the pass line; and the judge may fail to grade at most "
        f"{floors['max_judge_errors']} of the outputs"
    )
    lines.append(
        f"  {round(seeds['level'] * 100)}% intervals; bootstrap seed {seeds['bootstrap']}, "
        f"{seeds['resamples']} resamples"
    )
    for key, measured in report["criteria"].items():
        clinicians, between = measured["clinicians"], measured["clinician_agreement"]
        judged, validation = measured["judge_agreement"], measured["validation"]
        kind = "must not fail; " if measured["must_pass"] else ""
        lines.append("")
        lines.append(f"  {key} ({kind}passes from {measured['scale']['pass_from']})")
        lines.append(
            f"    clinicians, passing: {shown(clinicians['passing'])}; "
            f"{clinicians['disputed']} disputed (the less favourable grade stands)"
        )
        lines.append(
            f"    clinician with clinician, {between['cases']} outputs: agree "
            f"{shown(between['agreement'])}; kappa {_kappa_text(between['kappa'])}"
        )
        lines.append(
            f"    judge with clinicians, {judged['cases']} outputs: agree "
            f"{shown(judged['agreement'])}; kappa {_kappa_text(judged['kappa'])}"
        )
        # What a pass share is about: the same outputs as pass or fail.
        symmetry, sides = judged["symmetry"], judged["pass_fail"]
        leaning = f"; the judge leans {symmetry['leaning']}" if symmetry["leaning"] else ""
        lines.append(
            f"    on pass or fail: kappa {_kappa_text(judged['pass_fail_kappa'])}; clinicians "
            f"passed {shown(judged['passing']['clinicians'])}, the judge "
            f"{shown(judged['passing']['judge'])}; the judge alone passed "
            f"{sides['judge_only_pass']} and alone failed {sides['judge_only_fail']} (exact "
            f"McNemar p {symmetry['p']:.4f}){leaning}"
        )
        if judged["failures_found"] is not None:
            lines.append(
                f"    failures the judge found, of those clinicians found: "
                f"{shown(judged['failures_found'])}"
            )
        could_not = measured["judge_errors"]
        if could_not["paired"]["k"] or could_not["ungraded"]["k"]:
            lines.append(
                f"    the judge could not grade {could_not['paired']['k']} of the "
                f"{could_not['paired']['n']} outputs clinicians graded, and "
                f"{could_not['ungraded']['k']} of the {could_not['ungraded']['n']} others "
                "(never a pass, and on a must-not-fail criterion never a failure found)"
            )
        lines.append(f"    the judge on this criterion: {validation['state']}")
        lines.extend(f"      - {reason}" for reason in validation["reasons"])
        if measured["judged"] is None:
            lines.append("    judged, outputs no clinician graded: withheld")
        else:
            lines.append(
                "    judged, outputs no clinician graded, passing: "
                f"{shown(measured['judged']['passing'])}; it could not grade "
                f"{measured['judged']['could_not_grade']} more (its interval covers sampling "
                "only; the agreement above says how far to trust it)"
            )
        for heading, name in (
            ("failed by clinicians", "failed_by_clinicians"),
            ("failed by clinicians, passed by the judge", "judge_missed"),
            ("disputed", "disputed"),
        ):
            listed = measured["cases"][name]
            if listed:
                named = [str((ids or {}).get(case, case)) for case in listed[:_LISTED]]
                more = f" (and {len(listed) - _LISTED} more)" if len(listed) > _LISTED else ""
                lines.append(f"    {heading}: {', '.join(named)}{more}")
    cost = report["cost"]["judge_usd"]
    lines.append("")
    lines.append(
        "  judge cost: "
        + ("not priced" if cost is None else f"${cost:.4f}")
        + f" ({report['cost']['input_tokens']} input tokens, "
        f"{report['cost']['output_tokens']} output tokens)"
    )
    return "\n".join(lines)


def _kappa_text(measured: Mapping[str, Any]) -> str:
    if measured.get("estimate") is None:
        return f"not estimable ({measured.get('reason')})"
    interval = measured["interval"]
    if interval["status"] != "ok":
        return f"{measured['estimate']:.3f}, interval not estimable ({interval['reason']})"
    return f"{measured['estimate']:.3f} ({interval['low']:.3f} to {interval['high']:.3f})"
