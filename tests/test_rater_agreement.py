# SPDX-License-Identifier: Apache-2.0
"""Rater agreement (`evals/rater_agreement.py`, written output's): Cohen's kappa matches a
hand-computed reference and an independent implementation (statsmodels'), and the bootstrap
interval matches scipy's on the same index matrix. Ratings are synthetic."""

from __future__ import annotations

import random
from collections.abc import Sequence
from fractions import Fraction
from typing import Any

import numpy as np
import pytest
from scipy import stats as scipy_stats
from statsmodels.stats.inter_rater import cohens_kappa

from mendari.evals import stats
from mendari.evals.rater_agreement import kappa


def table(first: Sequence[str], second: Sequence[str]) -> np.ndarray:
    labels = sorted(set(first) | set(second))
    return np.array(
        [
            [sum(1 for a, b in zip(first, second, strict=True) if (a, b) == (x, y)) for y in labels]
            for x in labels
        ]
    )


def test_kappa_matches_hand_computed_reference() -> None:
    # 50 cases: yes/yes 20, yes/no 5, no/yes 10, no/no 15. Agreement 35/50 = 0.7; chance
    # 25/50 * 30/50 + 25/50 * 20/50 = 0.5; kappa (0.7 - 0.5) / (1 - 0.5) = 2/5.
    pairs = [("yes", "yes")] * 20 + [("yes", "no")] * 5 + [("no", "yes")] * 10
    pairs += [("no", "no")] * 15
    assert kappa(pairs) == Fraction(2, 5)
    assert kappa([("a", "b"), ("b", "a")]) == Fraction(-1)  # perfect disagreement
    assert kappa([("a", "a"), ("b", "b")]) == Fraction(1)
    labels = ("yes", "no", "unknown")
    draw = random.Random(11)
    for size in (5, 17, 60, 241):
        first = [draw.choice(labels) for _ in range(size)]
        second = [label if draw.random() < 0.6 else draw.choice(labels) for label in first]
        exact = kappa(list(zip(first, second, strict=True)))
        assert exact is not None
        assert float(exact) == pytest.approx(cohens_kappa(table(first, second)).kappa, abs=1e-12)


class FixedIndices(np.random.Generator):
    """A generator whose one draw of integers is the rows Mendari draws: scipy's bootstrap then
    resamples exactly as `stats.resample_indices` does."""

    def __init__(self, rows: list[list[int]]) -> None:
        super().__init__(np.random.PCG64(0))
        self.rows = np.array(rows)

    def integers(self, low: Any, high: Any = None, size: Any = None, *_: Any, **__: Any) -> Any:
        assert tuple(size) == self.rows.shape
        return self.rows


def test_bootstrap_interval_matches_reference() -> None:
    draw = random.Random(3)
    labels = ("yes", "no")
    first = [draw.choice(labels) for _ in range(80)]
    second = [label if draw.random() < 0.75 else draw.choice(labels) for label in first]
    pairs = list(zip(first, second, strict=True))

    def statistic(sample: Sequence[tuple[str, str]]) -> float | None:
        value = kappa(list(sample))
        return None if value is None else float(value)

    estimate, low, high, dropped = stats.bootstrap_defined(pairs, statistic, 2000, 0)
    assert dropped == 0 and estimate == float(kappa(pairs) or 0)
    rows = list(stats.resample_indices(len(pairs), 2000, 0))

    def by_index(index: np.ndarray) -> float:
        return float(statistic([pairs[int(i)] for i in index]) or 0.0)

    reference = scipy_stats.bootstrap(
        (np.arange(len(pairs)),),
        by_index,
        n_resamples=2000,
        method="percentile",
        confidence_level=0.95,
        vectorized=False,
        rng=FixedIndices(rows),
    )
    assert low == pytest.approx(reference.confidence_interval.low, abs=1e-12)
    assert high == pytest.approx(reference.confidence_interval.high, abs=1e-12)
