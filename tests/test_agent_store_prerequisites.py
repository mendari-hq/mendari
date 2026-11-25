# SPDX-License-Identifier: Apache-2.0
"""The agent store's prerequisites (LP-20e), checked by running them at start, not by reading:
LP-12's memberships mode, LP-16's bounded engine and budget, LP-10's statistics and ledger,
LP-11's canonical forms. Each one removed in turn is named."""

from __future__ import annotations

from typing import Any

import pytest

from mendari.core import agent_store, origin, patterns
from mendari.evals import canonical, ledger, stats


def test_the_stores_prerequisites_are_met() -> None:
    assert agent_store.prerequisites("memberships") == []
    # Each by behaviour, on its own.
    assert agent_store._bounded_problems() == []  # LP-16
    assert agent_store._statistics_problems() == []  # LP-10
    assert agent_store._canonical_problems() == []  # LP-11
    assert agent_store.prerequisites("claims") == [
        "memberships mode (LP-12): sign-in is in claims mode"
    ]


def _without_the_budget() -> Any:
    def never(*args: Any, **kwargs: Any) -> None:
        return None

    return never


@pytest.mark.parametrize(
    ("target", "name", "replacement", "named"),
    [
        (origin, "NOT_OWNED", frozenset({"hub"}), "LP-16: an agent from a store"),
        (patterns, "check_time", _without_the_budget(), "LP-16: a bounded agent's rules"),
        (stats, "clopper_pearson", lambda k, n: (0.0, 1.0), "LP-10: the exact interval"),
        (stats, "mcnemar_exact", lambda b, c: 1.0, "LP-10: the exact McNemar"),
        (ledger, "canonical", lambda value: "{}", "LP-10: the ledger's canonical JSON"),
        (canonical, "canonical_input", lambda value: "", "LP-11: the strict canonical form"),
        (canonical, "normalised_input", lambda value: "", "LP-11: the norm/v1 form"),
    ],
)
def test_a_missing_prerequisite_is_named(
    monkeypatch: pytest.MonkeyPatch, target: Any, name: str, replacement: Any, named: str
) -> None:
    monkeypatch.setattr(target, name, replacement)
    missing = agent_store.prerequisites("memberships")
    assert any(problem.startswith(named) for problem in missing), missing
