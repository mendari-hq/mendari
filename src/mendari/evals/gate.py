# SPDX-License-Identifier: Apache-2.0
"""The eval gate for pack changes: no merge that regresses a pack's critical metrics.

A pull request that touches a pack evaluates it on its benchmark dataset and compares the
result with the benchmark published on `main`:

- every critical label (`evaluation.critical`) keeps its detection rate, and a critical case
  that was never automated stays that way;
- the primary question's accuracy, and the false-automation rate, stay within the pack's
  `degradation_tolerance`.

A new pack has nothing to compare with; its metrics are shown and it passes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mendari.evals.render import shown
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

    new_auto = report["decisions"]["automation"]
    old_auto = (main or {}).get("decisions", {}).get("automation", {})
    metric(
        "false automation rate (of automated)",
        new_auto.get("false_automation_rate"),
        old_auto.get("false_automation_rate"),
        higher_is_better=False,
        of=(old_auto.get("n"), new_auto.get("n")),
    )
    if main is not None and rows[-1]["worse_by"] is not None and rows[-1]["worse_by"] > tolerance:
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
        if before is None:
            continue
        if (item.get("detection_rate") or 0) < (before.get("detection_rate") or 0) - EPSILON:
            failures.append(f"{name}: fewer critical cases found")
        if item.get("auto_missed", 0) > before.get("auto_missed", 0):
            failures.append(f"critical {key[0]}={key[1]}: a critical case was automated")
    return rows, failures


def markdown(pack_id: str, rows: list[dict[str, Any]], failures: list[str], new: bool) -> str:
    def cell(value: Any, n: Any) -> str:
        if value is None:
            return "n/a"
        return shown(counted(value, n)) if isinstance(n, int) else f"{value:.1%}"

    lines = [f"### {pack_id}", ""]
    if new:
        lines += ["A new pack: nothing on `main` to compare with.", ""]
    lines += ["| Metric | main | this change |", "|---|---|---|"]
    lines += [
        f"| {r['metric']} | {cell(r['main'], r.get('main_n'))} "
        f"| {cell(r['change'], r.get('change_n'))} |"
        for r in rows
    ]
    lines += ["", *(f"- **Regression:** {f}" for f in failures)]
    if not failures:
        lines.append("No regression in the critical metrics.")
    return "\n".join(lines) + "\n"
