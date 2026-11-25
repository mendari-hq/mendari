# SPDX-License-Identifier: Apache-2.0
"""The eval gate for agent changes (evals/gate.py): a seeded regression fails, an improvement or
an unchanged agent passes, and a new agent is shown without a comparison."""

from __future__ import annotations

import copy

from mendari.evals.gate import gate, markdown
from tests.support.engine import benchmark

# `benchmark()`: the triage fixture's evaluation on its `test` set (300 cases, 66 of them
# same-day symptoms), on its default providers.


def test_an_unchanged_agent_passes() -> None:
    rows, failures = gate(benchmark(), benchmark(), 0.03)
    assert failures == [] and {r["metric"] for r in rows} >= {"destination accuracy"}


def test_a_missed_critical_case_fails() -> None:
    worse = copy.deepcopy(benchmark())
    critical = worse["decisions"]["critical"][0]
    critical["detected"] -= 1
    critical["detection_rate"] = critical["detected"] / critical["n"]
    _, failures = gate(worse, benchmark(), 0.03)
    assert failures == ["critical urgency=yes detected: fewer critical cases found"]


def test_an_automated_critical_case_fails() -> None:
    worse = copy.deepcopy(benchmark())
    worse["decisions"]["critical"][0]["auto_missed"] = 1
    _, failures = gate(worse, benchmark(), 0.03)
    assert "a critical case was automated" in failures[0]


def test_accuracy_and_false_automation_within_tolerance() -> None:
    # The benchmark automates nothing (no agent in the screen): give it a rate.
    base = copy.deepcopy(benchmark())
    base["decisions"]["automation"]["false_automation_rate"] = 0.0
    worse = copy.deepcopy(base)
    worse["questions"]["destination"]["accuracy"] -= 0.02  # within 3 points
    assert gate(worse, base, 0.03)[1] == []
    worse["questions"]["destination"]["accuracy"] -= 0.02  # now 4 points
    worse["decisions"]["automation"]["false_automation_rate"] = 0.05
    failures = gate(worse, base, 0.03)[1]
    assert len(failures) == 2


def test_a_new_agent_is_shown_and_passes() -> None:
    rows, failures = gate(benchmark(), None, 0.03)
    assert failures == [] and all(r["main"] is None for r in rows)
    text = markdown("mendari-test/new", rows, failures, new=True)
    assert "nothing on `main` to compare with" in text and "| destination accuracy |" in text


def test_the_table_is_k_of_n_and_says_where_evidence_is_short() -> None:
    rows, failures = gate(benchmark(), benchmark(), 0.03)
    text = markdown("mendari-test/triage", rows, failures, new=False)
    # 66 same-day positives: under the 100 a critical label needs to rank or compare on.
    assert "| critical urgency=yes detected (insufficient evidence: under 100 positives) |" in text
    assert "| 36 of 66 [41.8-66.9%] | 36 of 66 [41.8-66.9%] |" in text
    # The benchmark automates nothing: no cases, never a bare rate or n/a.
    assert "| false automation rate (of automated) | no cases | no cases |" in text
    assert "| destination accuracy | 266 of 300 [84.5-92.0%] | 266 of 300 [84.5-92.0%] |" in text


def test_a_critical_label_main_measured_must_still_be_measured() -> None:
    """The gate compared only the change's critical labels: one dropped from `evaluation.critical`
    (or left without a labelled case) was never compared, and the change passed."""
    worse = copy.deepcopy(benchmark())
    worse["decisions"]["critical"] = []
    rows, failures = gate(worse, benchmark(), 0.03)
    assert failures == ["critical urgency=yes detected: no longer measured"]
    row = next(r for r in rows if r["metric"] == "critical urgency=yes detected")
    assert row["change"] is None and row["main"] is not None


def test_an_accuracy_that_can_no_longer_be_measured_fails() -> None:
    worse = copy.deepcopy(benchmark())
    worse["questions"]["destination"]["accuracy"] = None
    worse["questions"]["destination"]["n"] = 0
    _, failures = gate(worse, benchmark(), 0.03)
    assert failures == ["destination accuracy can no longer be measured"]
