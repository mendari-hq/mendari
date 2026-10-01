"""The eval gate for pack changes (evals/gate.py): a seeded regression fails, an improvement or
an unchanged pack passes, and a new pack is shown without a comparison."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from mendari.evals.gate import gate, markdown

BENCHMARK = Path(__file__).resolve().parents[1] / "packs/patient-inbox-router/evals/benchmark.json"


def benchmark() -> dict[str, Any]:
    report: dict[str, Any] = json.loads(BENCHMARK.read_text())
    return report


def test_an_unchanged_pack_passes() -> None:
    rows, failures = gate(benchmark(), benchmark(), 0.03)
    assert failures == [] and {r["metric"] for r in rows} >= {"destination accuracy"}


def test_a_missed_critical_case_fails() -> None:
    worse = copy.deepcopy(benchmark())
    critical = worse["decisions"]["critical"][0]
    critical["detected"] -= 1
    critical["detection_rate"] = critical["detected"] / critical["n"]
    _, failures = gate(worse, benchmark(), 0.03)
    assert failures == ["critical urgency=urgent detected: fewer critical cases found"]


def test_an_automated_critical_case_fails() -> None:
    worse = copy.deepcopy(benchmark())
    worse["decisions"]["critical"][0]["auto_missed"] = 1
    _, failures = gate(worse, benchmark(), 0.03)
    assert "a critical case was automated" in failures[0]


def test_accuracy_and_false_automation_within_tolerance() -> None:
    worse = copy.deepcopy(benchmark())
    worse["questions"]["destination"]["accuracy"] -= 0.02  # within 3 points
    assert gate(worse, benchmark(), 0.03)[1] == []
    worse["questions"]["destination"]["accuracy"] -= 0.02  # now 4 points
    worse["decisions"]["automation"]["false_automation_rate"] += 0.05
    failures = gate(worse, benchmark(), 0.03)[1]
    assert len(failures) == 2


def test_a_new_pack_is_shown_and_passes() -> None:
    rows, failures = gate(benchmark(), None, 0.03)
    assert failures == [] and all(r["main"] is None for r in rows)
    text = markdown("mendari/new", rows, failures, new=True)
    assert "nothing on `main` to compare with" in text and "| destination accuracy |" in text
