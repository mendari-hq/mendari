# SPDX-License-Identifier: Apache-2.0
"""The eval gate for pack changes: no merge that regresses a pack's critical metrics.

A pull request that touches a pack evaluates it on its benchmark dataset and compares the
result with the benchmark published on `main`:

- every critical label (`evaluation.critical`) keeps its detection rate, and a critical case
  that was never automated stays that way;
- the primary question's accuracy, and the false-automation rate, stay within the pack's
  `degradation_tolerance`.

A new pack has nothing to compare with; its metrics are shown and it passes.

The same gate is what `mendari eval --no-worse-than RESULT` runs for anyone's agent, in their own
CI (docs/guides/evaluate-in-ci.md), beside limits they state (`Limits`): what a result must be
whatever it was before. `check` applies both, and `Checked` renders them for a terminal or a job
summary.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import astuple, dataclass
from typing import Any

# A question's key or a label, as a manifest may write one: what a result names, and so what a
# baseline someone hands over may name.
from mendari.core.spec import _KEY as _NAME
from mendari.evals import stats
from mendari.evals.render import evidence, md_row, shown
from mendari.evals.result import counted

EPSILON = 1e-9


def gate(
    report: Mapping[str, Any], main: Mapping[str, Any] | None, tolerance: float
) -> tuple[list[dict[str, Any]], list[str]]:
    """(rows, failures): each metric on main and in this change, and what regressed."""
    rows: list[dict[str, Any]] = []
    failures: list[str] = []
    primary = str(report["primary"])

    def metric(
        name: str,
        new: float | None,
        old: float | None,
        *,
        higher_is_better: bool,
        of: tuple[int | None, int | None] = (None, None),
    ) -> None:
        """`of`: the counts the rates are of, on main and in this change, to show k of n."""
        worse = None
        if new is not None and old is not None:
            worse = (old - new) if higher_is_better else (new - old)
        rows.append(
            {"metric": name, "main": old, "change": new, "worse_by": worse,
             "main_n": of[0], "change_n": of[1]}
        )  # fmt: skip

    new_q = report["questions"].get(primary, {})
    old_q = (main or {}).get("questions", {}).get(primary, {})
    metric(
        f"{primary} accuracy",
        new_q.get("accuracy"),
        old_q.get("accuracy"),
        higher_is_better=True,
        of=(old_q.get("n"), new_q.get("n")),
    )
    if main is not None and rows[-1]["worse_by"] is not None and rows[-1]["worse_by"] > tolerance:
        failures.append(f"{primary} accuracy fell by more than {tolerance:.0%}")
    if main is not None and old_q.get("accuracy") is not None and new_q.get("accuracy") is None:
        failures.append(f"{primary} accuracy can no longer be measured")

    new_auto = report["decisions"]["automation"]
    old_auto = (main or {}).get("decisions", {}).get("automation", {})
    new_far = new_auto.get("false_automation_rate")
    old_far = old_auto.get("false_automation_rate")
    metric(
        "false automation rate (of automated)",
        new_far,
        old_far,
        higher_is_better=False,
        of=(old_auto.get("n"), new_auto.get("n")),
    )
    if main is not None:
        worse_far = rows[-1]["worse_by"]
        if worse_far is None and old_far is None and new_far is not None:
            worse_far = new_far
            rows[-1]["worse_by"] = worse_far
        if worse_far is not None and worse_far > tolerance:
            failures.append(f"the false automation rate rose by more than {tolerance:.0%}")

    old_critical = {
        (c["question"], c["label"]): c
        for c in (main or {}).get("decisions", {}).get("critical", [])
    }
    for item in report["decisions"].get("critical", []):
        key = (item["question"], item["label"])
        before = old_critical.get(key)
        name = f"critical {key[0]}={key[1]} detected"
        metric(
            name,
            item.get("detection_rate"),
            before and before.get("detection_rate"),
            higher_is_better=True,
            of=(before and before.get("n"), item.get("n")),
        )
        rows[-1]["positives"] = item.get("n")
        if before is None:
            continue
        before_rate = before.get("detection_rate")
        now_rate = item.get("detection_rate")
        if before_rate is not None and now_rate is None:
            failures.append(f"{name}: no longer measured")
        elif before_rate is not None and now_rate is not None and now_rate < before_rate - EPSILON:
            failures.append(f"{name}: fewer critical cases found")
        if item.get("auto_missed", 0) > before.get("auto_missed", 0):
            failures.append(f"critical {key[0]}={key[1]}: a critical case was automated")
    # A critical label main measured that this change does not (dropped from
    # `evaluation.critical`, or without a labelled case left) is a regression, not a pass.
    measured = {(c["question"], c["label"]) for c in report["decisions"].get("critical", [])}
    for key, before in old_critical.items():
        if key in measured:
            continue
        name = f"critical {key[0]}={key[1]} detected"
        metric(
            name,
            None,
            before.get("detection_rate"),
            higher_is_better=True,
            of=(before.get("n"), None),
        )
        failures.append(f"{name}: no longer measured")
    return rows, failures


def _table(rows: list[dict[str, Any]], base: str) -> list[str]:
    """The gate's rows as a Markdown table: each metric on `base` and in this change."""

    def cell(value: Any, n: Any) -> str:
        """k of n with its interval; "no cases" when n is 0, "n/a" when the side does not say."""
        return shown(counted(value, n)) if isinstance(n, int) else "n/a"

    def name(row: Mapping[str, Any]) -> str:
        positives = row.get("positives")
        return row["metric"] + (evidence(positives) if isinstance(positives, int) else "")

    return [
        f"| Metric | {base} | this change |",
        "|---|---|---|",
        *(
            f"| {name(r)} | {cell(r['main'], r.get('main_n'))} "
            f"| {cell(r['change'], r.get('change_n'))} |"
            for r in rows
        ),
    ]


def markdown(pack_id: str, rows: list[dict[str, Any]], failures: list[str], new: bool) -> str:
    lines = [f"### {pack_id}", ""]
    if new:
        lines += ["A new agent: nothing on `main` to compare with.", ""]
    lines += _table(rows, "main")
    lines += ["", *(f"- **Regression:** {f}" for f in failures)]
    if not failures:
        lines.append("No regression in the critical metrics.")
    return "\n".join(lines) + "\n"


# -- stated limits, and one evaluation against all that was asked of it ---------------------------


@dataclass(frozen=True)
class Limits:
    """What an evaluation's result must be, whatever it was before (`mendari eval --min-...
    --max-...`). A share is of 1; None: not stated."""

    min_accuracy: float | None = None  # the primary question's
    max_false_automation: float | None = None  # wrong recommendations, of the automated cases
    min_critical_detection: float | None = None  # of each critical label's cases
    max_critical_automated: int | None = None  # cases of each critical label

    @property
    def stated(self) -> bool:
        return any(value is not None for value in astuple(self))


def _percent(share: float) -> str:
    """A stated share as a percentage, as it was stated: 0.9 is 90%, 0.925 is 92.5%."""
    return f"{round(share * 100, 6):g}%"


def limits(report: Mapping[str, Any], stated: Limits) -> tuple[list[dict[str, Any]], list[str]]:
    """(rows, failures): each stated limit beside what the report measured, and the ones it
    misses. A floor under a figure the report does not measure (no labelled case, no case of a
    critical label) is missed, since nothing shows it is met; a ceiling over what never
    happened (nothing automated) is met."""
    rows: list[dict[str, Any]] = []
    failures: list[str] = []

    def row(name: str, measured: str, words: str, met: bool) -> None:
        rows.append({"metric": name, "limit": words, "measured": measured, "met": met})
        if not met:
            failures.append(f"{name}: {measured}, and the limit is {words}")

    def share(name: str, value: Any, n: Any, limit: float, *, floor: bool) -> None:
        measured = "not measured" if value is None and n != 0 else shown(counted(value, n))
        if value is None:
            met = not floor
        else:
            met = value >= limit - EPSILON if floor else value <= limit + EPSILON
        row(name, measured, f"at {'least' if floor else 'most'} {_percent(limit)}", met)

    primary = str(report["primary"])
    if stated.min_accuracy is not None:
        question = report["questions"].get(primary, {})
        share(
            f"{primary} accuracy",
            question.get("accuracy"),
            question.get("n"),
            stated.min_accuracy,
            floor=True,
        )
    if stated.max_false_automation is not None:
        automation = report["decisions"]["automation"]
        share(
            "false automation rate (of automated)",
            automation.get("false_automation_rate"),
            automation.get("n"),
            stated.max_false_automation,
            floor=False,
        )
    for item in report["decisions"].get("critical", []):
        name = f"critical {item['question']}={item['label']}"
        if stated.min_critical_detection is not None:
            share(
                f"{name} detected",
                item.get("detection_rate"),
                item.get("n"),
                stated.min_critical_detection,
                floor=True,
            )
        if stated.max_critical_automated is not None:
            automated, positives = int(item.get("auto_missed", 0)), int(item.get("n", 0))
            row(
                f"{name} automated",
                shown(stats.proportion(automated, positives)),
                f"at most {stated.max_critical_automated}",
                automated <= stated.max_critical_automated,
            )
    return rows, failures


def _share(value: Any) -> bool:
    """None, or a share of 1: what a result's rate may be."""
    if value is None:
        return True
    number = isinstance(value, int | float) and not isinstance(value, bool)
    return number and math.isfinite(value) and 0.0 <= value <= 1.0


def _count(value: Any) -> bool:
    """None, or a whole count: what a result's number of cases may be."""
    return value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0)


def baseline_problem(baseline: Any, agent_id: str, primary: str) -> str | None:
    """Why `baseline` cannot be the result a run of the agent `agent_id` (primary question
    `primary`) is held against, or None. A baseline is a file someone hands over: everything
    `gate` reads from it, and each name of it that is then shown, is checked here first, so a
    malformed one says so instead of failing the evaluation or writing its own text into a
    summary."""
    not_a_result = "not an evaluation result (`mendari eval --out` writes one)"
    if not isinstance(baseline, Mapping):
        return not_a_result
    pack, dataset = baseline.get("pack"), baseline.get("dataset")
    questions, decisions = baseline.get("questions"), baseline.get("decisions")
    if not isinstance(pack, Mapping) or not isinstance(dataset, Mapping):
        return not_a_result
    if not isinstance(questions, Mapping) or not isinstance(decisions, Mapping):
        return not_a_result
    if pack.get("id") != agent_id:
        return f"a result of another agent, not of {agent_id}"
    malformed = "its figures are not an evaluation result's"
    question = questions.get(primary, {})
    automation = decisions.get("automation", {})
    critical = decisions.get("critical", [])
    if not isinstance(question, Mapping) or not isinstance(automation, Mapping):
        return malformed
    if not _share(question.get("accuracy")) or not _count(question.get("n")):
        return malformed
    if not _share(automation.get("false_automation_rate")) or not _count(automation.get("n")):
        return malformed
    if not isinstance(critical, list):
        return malformed
    for item in critical:
        if not isinstance(item, Mapping):
            return malformed
        names = (item.get("question"), item.get("label"))
        if not all(isinstance(name, str) and _NAME.match(name) for name in names):
            return malformed
        if not _share(item.get("detection_rate")) or not _count(item.get("n")):
            return malformed
        if item.get("auto_missed", 0) is None or not _count(item.get("auto_missed", 0)):
            return malformed
    return None


def _cases(report: Mapping[str, Any]) -> tuple[Any, Any]:
    """Which cases a result is of: the dataset file, and how many of its first cases."""
    dataset = report.get("dataset") or {}
    return dataset.get("sha256"), dataset.get("limit")


NOT_THE_SAME_CASES = (
    "the baseline was not measured on these cases (another dataset, another version of it, or "
    "another part): not compared. Measure the baseline again on this dataset"
)
NO_CRITICAL_LABEL = (
    "The agent declares no critical label (`evaluation.critical`), so the limits on critical "
    "cases check nothing."
)


@dataclass(frozen=True)
class Checked:
    """One evaluation against all that was asked of it: the stated limits (`limits`) and, when a
    baseline was given, the gate against it."""

    limits: list[dict[str, Any]]
    missed: list[str]  # the limits it misses
    compared: bool  # a baseline was given
    tolerance: float  # the agent's, which the gate allowed
    rows: list[dict[str, Any]]  # the gate's; none when the baseline is of other cases
    regressions: list[str]
    notes: list[str]

    @property
    def passed(self) -> bool:
        return not self.missed and not self.regressions

    @property
    def verdict(self) -> str:
        """`passed`, or `failed` with what failed it: `failed: 2 limits missed, 1 regression`."""
        if self.passed:
            return "passed"
        found = []
        if self.missed:
            found.append(f"{len(self.missed)} limit{'s' if len(self.missed) > 1 else ''} missed")
        if self.regressions:
            count = len(self.regressions)
            found.append(f"{count} regression{'s' if count > 1 else ''}")
        return "failed: " + ", ".join(found)

    def text(self) -> str:
        """For a terminal, after the report."""
        lines = ["Gate"]
        width = max((len(row["metric"]) for row in self.limits), default=0)
        lines += [
            f"  {row['metric']:<{width}}  {row['measured']}, limit {row['limit']}: "
            f"{'met' if row['met'] else 'MISSED'}"
            for row in self.limits
        ]
        if self.compared:
            lines.append(
                f"  against the baseline (tolerance {_percent(self.tolerance)}): "
                + ("REGRESSION" if self.regressions else "no regression")
            )
            lines += [f"    {regression}" for regression in self.regressions]
        lines += [f"  {note}" for note in self.notes]
        lines.append(f"Gate {self.verdict}")
        return "\n".join(lines)

    def markdown(self) -> str:
        """For a CI job summary, after `render.report_markdown`."""
        lines = [f"### Gate {self.verdict}", ""]
        if self.limits:
            lines += [md_row("Limit", "Stated", "Measured", "Verdict"), "|---|---|---|---|"]
            lines += [
                md_row(
                    row["metric"],
                    row["limit"],
                    row["measured"],
                    "met" if row["met"] else "**missed**",
                )
                for row in self.limits
            ]
            lines.append("")
        if self.compared:
            lines += [
                "Compared with the baseline, within the agent's tolerance of "
                f"{_percent(self.tolerance)}:",
                "",
            ]
            if self.rows:
                lines += [*_table(self.rows, "baseline"), ""]
        lines += [f"- **Regression:** {regression}" for regression in self.regressions]
        if self.passed:
            lines.append(_PASSED[bool(self.limits), self.compared])
        if self.notes:
            lines += ["", *self.notes]
        return "\n".join(lines) + "\n"


# What a passed gate says, by what it held the result to: (limits stated, a baseline given).
_PASSED = {
    (True, True): "Every stated limit is met, and nothing is worse than the baseline.",
    (True, False): "Every stated limit is met.",
    (False, True): "Nothing is worse than the baseline.",
    (False, False): "No limit was stated and no baseline given: nothing was checked.",
}


def check(
    report: Mapping[str, Any],
    stated: Limits,
    baseline: Mapping[str, Any] | None = None,
    tolerance: float = 0.03,
) -> Checked:
    """`report` against the limits `stated` and, with a `baseline` (one `baseline_problem`
    passes), against it through `gate`, within `tolerance` (the agent's
    `evaluation.degradation_tolerance`). A baseline of other cases is not compared, and that is
    a regression: nothing shows the run is no worse."""
    limit_rows, missed = limits(report, stated)
    notes = []
    on_critical = (stated.min_critical_detection, stated.max_critical_automated) != (None, None)
    if on_critical and not report["decisions"].get("critical"):
        notes.append(NO_CRITICAL_LABEL)
    rows: list[dict[str, Any]] = []
    regressions: list[str] = []
    if baseline is not None:
        if _cases(baseline) != _cases(report):
            regressions = [NOT_THE_SAME_CASES]
        else:
            rows, regressions = gate(report, baseline, tolerance)
    return Checked(
        limits=limit_rows,
        missed=missed,
        compared=baseline is not None,
        tolerance=tolerance,
        rows=rows,
        regressions=regressions,
        notes=notes,
    )
