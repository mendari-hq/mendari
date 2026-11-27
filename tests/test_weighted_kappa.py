# SPDX-License-Identifier: Apache-2.0
"""Cohen's kappa with linear weights on declared scores (`rater_agreement.weighted_kappa`), the
statistic written output is gated on (`evals/written.py`): it matches statsmodels' on yes/no, on
ordinal scales and on uneven scores, a hand-computed fraction, and plain kappa on two levels; its
bootstrap interval matches scipy's on the same index matrix; and the smallest number of failures
a must-not-fail floor needs matches scipy's beta quantile. Ratings are synthetic."""

from __future__ import annotations

import random
from collections.abc import Sequence
from fractions import Fraction
from typing import Any

import numpy as np
import pytest
from scipy import stats as scipy_stats
from statsmodels.stats.inter_rater import cohens_kappa
from statsmodels.stats.proportion import proportion_confint

from mendari.evals import stats, written
from mendari.evals.rater_agreement import kappa, measured, weighted_kappa

SCALES: dict[str, dict[str, float]] = {
    "yes_no": {"no": 0, "yes": 1},
    "three": {"not_addressed": 0, "partly": 1, "fully": 2},
    "five": {"a": 0, "b": 1, "c": 2, "d": 3, "e": 4},
    "uneven": {"poor": 0, "fair": 1, "good": 4, "excellent": 10},
    "halves": {"low": 0.5, "mid": 1.25, "high": 3.0},
}


def table(pairs: Sequence[tuple[str, str]], levels: Sequence[str]) -> np.ndarray:
    return np.array(
        [[sum(1 for pair in pairs if pair == (row, column)) for column in levels] for row in levels]
    )


Pairs = list[tuple[str, str]]


def drawn(scale: dict[str, float], size: int, seed: int, keep: float = 0.6) -> Pairs:
    draw = random.Random(seed)
    levels = list(scale)
    first = [draw.choice(levels) for _ in range(size)]
    second = [level if draw.random() < keep else draw.choice(levels) for level in first]
    return list(zip(first, second, strict=True))


@pytest.mark.parametrize("name", sorted(SCALES))
@pytest.mark.parametrize("size", [5, 17, 60, 241, 400])
def test_matches_statsmodels(name: str, size: int) -> None:
    scale = SCALES[name]
    levels = list(scale)
    scores = np.array([scale[level] for level in levels], dtype=float)
    for seed in range(4):
        pairs = drawn(scale, size, seed * 31 + size)
        exact = weighted_kappa(pairs, scale)
        counts = table(pairs, levels)
        if exact is None:  # both raters constant and equal: nothing to compare
            assert len({pair for pair in pairs}) == 1
            continue
        reference = cohens_kappa(counts, weights=scores, wt="linear").kappa
        assert float(exact) == pytest.approx(reference, abs=1e-12)


def test_hand_computed_fraction() -> None:
    # Three levels scored 0, 1, 2. Twenty cases:
    #   (0,0) 6, (0,1) 2, (1,1) 4, (1,2) 2, (2,2) 5, (2,0) 1.
    # Observed distance: 2*1 + 2*1 + 1*2 = 6, times n = 120.
    # Rows r = {0: 8, 1: 6, 2: 6}; columns c = {0: 7, 1: 6, 2: 7}.
    # Expected: sum |l - m| r(l) c(m)
    #   = 8*(6*1 + 7*2) + 6*(7*1 + 7*1) + 6*(7*2 + 6*1) = 160 + 84 + 120 = 364.
    # kappa = 1 - 120/364 = 61/91.
    pairs = (
        [("a", "a")] * 6
        + [("a", "b")] * 2
        + [("b", "b")] * 4
        + [("b", "c")] * 2
        + [("c", "c")] * 5
        + [("c", "a")] * 1
    )
    assert weighted_kappa(pairs, {"a": 0, "b": 1, "c": 2}) == Fraction(61, 91)


def test_is_plain_kappa_on_yes_no() -> None:
    for seed in range(20):
        pairs = drawn(SCALES["yes_no"], 5 + seed * 13, seed)
        assert weighted_kappa(pairs, SCALES["yes_no"]) == kappa(pairs)
    # Whatever the two scores are, as long as they differ.
    pairs = drawn(SCALES["yes_no"], 90, 5)
    assert weighted_kappa(pairs, {"no": -3, "yes": 7.5}) == kappa(pairs)


def test_both_orders_of_each_pair() -> None:
    """Two clinicians are neither first: each pair counts in both orders, which is the kappa of
    the table added to its transpose."""
    for name in ("three", "uneven"):
        scale = SCALES[name]
        levels = list(scale)
        scores = np.array([scale[level] for level in levels], dtype=float)
        pairs = drawn(scale, 120, 9)
        both = pairs + [(second, first) for first, second in pairs]
        counts = table(pairs, levels)
        reference = cohens_kappa(counts + counts.T, weights=scores, wt="linear").kappa
        exact = weighted_kappa(both, scale)
        assert exact is not None
        assert float(exact) == pytest.approx(reference, abs=1e-12)


def test_undefined_and_zero() -> None:
    scale = SCALES["three"]
    assert weighted_kappa([], scale) is None
    assert weighted_kappa([("fully", "fully")] * 12, scale) is None  # constant and equal
    # A judge that always passes: no better than chance, exactly 0, whatever the clinicians said.
    clinicians = ["yes"] * 90 + ["no"] * 10
    assert weighted_kappa([(given, "yes") for given in clinicians], SCALES["yes_no"]) == 0
    graded = ["fully"] * 50 + ["partly"] * 30 + ["not_addressed"] * 20
    assert weighted_kappa([(given, "fully") for given in graded], scale) == 0
    assert weighted_kappa([("no", "yes"), ("yes", "no")], SCALES["yes_no"]) == -1
    with pytest.raises(KeyError):
        weighted_kappa([("yes", "maybe")], SCALES["yes_no"])


class FixedIndices(np.random.Generator):
    """A generator whose one draw of integers is the rows Mendari draws: scipy's bootstrap then
    resamples exactly as `stats.resample_indices` does (as tests/test_rater_agreement.py)."""

    def __init__(self, rows: list[list[int]]) -> None:
        super().__init__(np.random.PCG64(0))
        self.rows = np.array(rows)

    def integers(self, low: Any, high: Any = None, size: Any = None, *_: Any, **__: Any) -> Any:
        assert tuple(size) == self.rows.shape
        return self.rows


def test_bootstrap_interval_matches_scipy() -> None:
    scale = SCALES["three"]
    pairs = drawn(scale, 90, 4, keep=0.7)

    def statistic(sample: Sequence[tuple[str, str]]) -> float | None:
        value = weighted_kappa(list(sample), scale)
        return None if value is None else float(value)

    estimate, low, high, dropped = stats.bootstrap_defined(pairs, statistic, 2000, 0)
    assert dropped == 0 and estimate == float(weighted_kappa(pairs, scale) or 0)
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
    stored, unrounded = measured(
        weighted_kappa(pairs, scale), statistic, pairs, 0, 2000, "constant"
    )
    assert stored["interval"] == {
        "status": "ok",
        "low": round(low or 0, 6),
        "high": round(high or 0, 6),
        "dropped": 0,
    }
    assert unrounded == low


def test_min_failures_is_the_smallest_perfect_sample() -> None:
    assert written.min_failures(written.MUST_PASS_RECALL_FLOOR) == 17
    for floor in (0.5, 0.6, 0.75, 0.8, 0.9, 0.95, 0.99):
        needed = written.min_failures(floor)
        # The n-of-n exact lower bound is the beta quantile at alpha / 2 with a = n, b = 1.
        assert scipy_stats.beta.ppf(0.025, needed, 1) >= floor
        assert needed == 1 or scipy_stats.beta.ppf(0.025, needed - 1, 1) < floor
    for bad in (0.0, 1.0, -0.1, 1.5):
        with pytest.raises(ValueError):
            written.min_failures(bad)
    with pytest.raises(TypeError):
        written.min_failures(True)


def test_failures_found_interval_matches_statsmodels() -> None:
    for found, failed in ((16, 16), (17, 17), (15, 20), (0, 12), (31, 34)):
        low, high = proportion_confint(found, failed, alpha=0.05, method="beta")
        measured = stats.proportion(found, failed)
        assert measured.low == pytest.approx(low if found else 0.0, abs=1e-9)
        assert measured.high == pytest.approx(high if found < failed else 1.0, abs=1e-9)
    assert (stats.proportion(16, 16).low or 0) < written.MUST_PASS_RECALL_FLOOR
    assert (stats.proportion(17, 17).low or 0) >= written.MUST_PASS_RECALL_FLOOR


def test_the_floors_are_the_stated_ones() -> None:
    assert written.AGREEMENT_FLOOR == 0.60
    assert written.MUST_PASS_RECALL_FLOOR == 0.80
    assert written.MIN_PAIRED == 50
    assert written.MIN_EACH_SIDE == 10
