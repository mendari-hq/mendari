# SPDX-License-Identifier: Apache-2.0
"""mendari.evals.stats against scipy and statsmodels (development dependencies only): the exact
methods to 1e-12, the bootstrap by feeding both the same index matrix, and the known values the
launch plan's statistics rest on (LP-10)."""

from __future__ import annotations

import math
import random
from collections.abc import Hashable, Sequence
from fractions import Fraction
from typing import Any

import numpy as np
import pytest
from scipy import stats as st
from statsmodels.stats.contingency_tables import mcnemar
from statsmodels.stats.multitest import multipletests
from statsmodels.stats.proportion import proportion_confint

from mendari.evals import metrics, stats

EXACT = 1e-12


def test_the_known_values() -> None:
    assert round(stats.clopper_pearson(20, 20)[0], 4) == 0.8316
    low, high = stats.clopper_pearson(95, 100)
    assert (round(low, 4), round(high, 4)) == (0.8872, 0.9836)
    assert round(stats.clopper_pearson(100, 100)[0], 4) == 0.9638
    assert stats.clopper_pearson(0, 10)[0] == 0.0 and stats.clopper_pearson(10, 10)[1] == 1.0


def _grid() -> list[tuple[int, int]]:
    cases = [(k, n) for n in range(1, 31) for k in range(n + 1)]
    for n in (50, 100, 300, 600, 1000, 5000):
        ks = {0, 1, 2, 3, n // 4, n // 2, n - 3, n - 2, n - 1, n, *range(0, n + 1, max(1, n // 15))}
        cases += [(k, n) for k in sorted(ks)]
    # Two inputs that broke a first inversion (a step at a bracket's end; an overflowing
    # correction taken for convergence).
    return [*cases, (3, 300), (6, 250), (2, 2500)]


# Where I reaches the floor of its precision before a step can: the nearest iterate is kept.
EXTREME = [(2, 27), (797, 799), (2, 180), (269, 271)]


@pytest.mark.parametrize("level", [0.9, 0.95, 0.99, 0.999])
def test_clopper_pearson_matches_scipy_and_statsmodels(level: float) -> None:
    alpha = 1 - level
    for k, n in _grid():  # each case asserted: a NaN fails, as a running max() would not
        low, high = stats.clopper_pearson(k, n, level)
        model_low, model_high = proportion_confint(k, n, alpha=alpha, method="beta")
        scipy_low = 0.0 if k == 0 else st.beta.ppf(alpha / 2, k, n - k + 1)
        scipy_high = 1.0 if k == n else st.beta.ppf(1 - alpha / 2, k + 1, n - k)
        assert abs(low - model_low) < EXACT and abs(high - model_high) < EXACT, (k, n, level)
        assert abs(low - scipy_low) < EXACT and abs(high - scipy_high) < EXACT, (k, n, level)


@pytest.mark.parametrize(("k", "n"), EXTREME)
def test_clopper_pearson_at_extreme_levels(k: int, n: int) -> None:
    level = 0.999999
    low, high = stats.clopper_pearson(k, n, level)
    assert abs(low - st.beta.ppf((1 - level) / 2, k, n - k + 1)) < EXACT
    assert abs(high - st.beta.ppf(1 - (1 - level) / 2, k + 1, n - k)) < EXACT


def test_the_beta_quantile_takes_shapes_of_at_least_one() -> None:
    for a, b in [(0.5, 3), (2, 0.4)]:
        with pytest.raises(ValueError, match="at least 1"):
            stats.beta_ppf(a, b, 0.2)


def test_the_beta_quantile_matches_scipy() -> None:
    rng = random.Random(7)
    for _ in range(500):
        a, b = rng.uniform(1, 400), rng.uniform(1, 400)
        q = rng.choice([rng.uniform(1e-6, 1 - 1e-6), 0.025, 0.975, 0.5])
        assert abs(stats.beta_ppf(a, b, q) - st.beta.ppf(q, a, b)) < EXACT


def test_clopper_pearson_refuses_what_is_not_a_proportion() -> None:
    for bad in [(1, 0), (-1, 5), (6, 5)]:
        with pytest.raises(ValueError):
            stats.clopper_pearson(*bad)
    with pytest.raises(TypeError):
        stats.clopper_pearson(True, 5)  # a bool is not a count
    with pytest.raises(ValueError):
        stats.clopper_pearson(1, 5, level=1.0)


def test_a_proportion_keeps_k_n_and_its_interval() -> None:
    assert stats.proportion(3, 10).to_dict() == {"k": 3, "n": 10, "low": 0.06674, "high": 0.652453}
    assert stats.proportion(0, 0).to_dict() == {"k": 0, "n": 0, "low": None, "high": None}
    assert stats.proportion(0, 0).rate is None and stats.proportion(1, 4).rate == 0.25


def test_mcnemar_matches_statsmodels_and_exact_arithmetic() -> None:
    for b in range(41):
        for c in range(41):
            ours = stats.mcnemar_exact(b, c)
            assert abs(ours - mcnemar([[0, b], [c, 0]], exact=True).pvalue) < EXACT
            if b + c:
                assert abs(ours - st.binomtest(min(b, c), b + c, 0.5).pvalue) < EXACT
                tail = sum(
                    Fraction(math.comb(b + c, j), 2 ** (b + c)) for j in range(min(b, c) + 1)
                )
                assert ours == float(min(Fraction(1), 2 * tail))  # correctly rounded
    assert stats.mcnemar_exact(0, 0) == 1.0
    with pytest.raises(ValueError):
        stats.mcnemar_exact(-1, 2)


HOLM_EDGES = [
    [0.05],  # one comparison, on its threshold
    [0.025, 0.05],  # each on its step's threshold
    [0.0125, 0.0125, 0.0125, 0.0125],  # ties on the first threshold
    [0.0, 1.0, 0.01, 0.05, 0.05],
    [0.04, 0.01, 0.03, 0.02],
]


def _holm_families(count: int) -> list[list[float]]:
    rng = random.Random(11)
    pool = [0.0, 1.0, 0.05, 0.025, 0.0125, 0.01]  # on the thresholds, and ties
    families = []
    for _ in range(count):
        m = rng.randint(1, 12)
        families.append(
            [rng.choice(pool) if rng.random() < 0.3 else rng.random() ** 3 for _ in range(m)]
        )
    return families


def _holm_agrees(p: list[float]) -> None:
    reject, adjusted = stats.holm(p, alpha=0.05)
    model_reject, model_adjusted, _, _ = multipletests(p, alpha=0.05, method="holm")
    assert reject == list(model_reject)
    assert adjusted == list(model_adjusted)


def test_holm_matches_statsmodels() -> None:  # statsmodels takes about 20 ms a family
    for p in [*HOLM_EDGES, *_holm_families(80)]:
        _holm_agrees(p)
    assert stats.holm([]) == ([], [])


@pytest.mark.slow
def test_holm_matches_statsmodels_over_many_families() -> None:
    for p in _holm_families(2000):
        _holm_agrees(p)


def test_a_holm_family_is_named() -> None:
    family = stats.holm_family({"accuracy": 0.01, "detection": 0.04, "automation": 0.2})
    assert family["accuracy"] == {"p": 0.01, "adjusted": 0.03, "reject": True}
    assert family["detection"]["reject"] is False


def test_percentile_is_numpys_linear() -> None:
    rng = random.Random(3)
    for _ in range(2000):
        values = sorted(rng.random() for _ in range(rng.randint(1, 60)))
        q = rng.random()
        assert stats.percentile(values, q) == np.quantile(values, q, method="linear")


class FixedIndices(np.random.Generator):
    """A generator that hands scipy.stats.bootstrap our index matrix (its one draw)."""

    def __init__(self, matrix: np.ndarray[Any, Any]) -> None:
        super().__init__(np.random.PCG64(0))
        self.matrix = matrix

    def integers(self, low: Any, high: Any = None, size: Any = None, **_: Any) -> Any:
        assert low == 0 and high == self.matrix.shape[1] and tuple(size) == self.matrix.shape
        return self.matrix


def _matrix(n: int, resamples: int, seed: int) -> np.ndarray[Any, Any]:
    return np.array(list(stats.resample_indices(n, resamples, seed)))


@pytest.mark.parametrize(("n", "seed"), [(5, 0), (37, 1), (200, 2026)])
def test_bootstrap_matches_numpy_and_scipy_on_the_same_resamples(n: int, seed: int) -> None:
    rng = random.Random(seed)
    values = [rng.random() for _ in range(n)]
    estimate, low, high = stats.bootstrap(values, resamples=500, seed=seed)
    matrix = _matrix(n, 500, seed)
    means = np.asarray(values)[matrix].mean(axis=1)
    assert estimate == pytest.approx(np.mean(values), abs=1e-15)
    assert abs(low - np.quantile(means, 0.025)) < 1e-15
    assert abs(high - np.quantile(means, 0.975)) < 1e-15
    interval = st.bootstrap(
        (np.asarray(values),), np.mean, n_resamples=500, method="percentile",
        confidence_level=0.95, rng=FixedIndices(matrix),
    ).confidence_interval  # fmt: skip
    assert abs(low - interval.low) < 1e-14 and abs(high - interval.high) < 1e-14


def test_paired_bootstrap_matches_numpy_and_scipy_on_the_same_resamples() -> None:
    rng = random.Random(5)
    a = [rng.random() for _ in range(120)]
    b = [x - 0.05 + rng.random() * 0.1 for x in a]
    estimate, low, high, p = stats.paired_bootstrap(a, b, resamples=800, seed=9)
    matrix = _matrix(120, 800, 9)
    diffs = np.asarray(a)[matrix].mean(axis=1) - np.asarray(b)[matrix].mean(axis=1)
    assert abs(low - np.quantile(diffs, 0.025)) < 1e-15
    assert abs(high - np.quantile(diffs, 0.975)) < 1e-15
    below, above = int((diffs <= 0).sum()), int((diffs >= 0).sum())
    assert p == min(1.0, 2 * min(1 + below, 1 + above) / 801)
    interval = st.bootstrap(
        (np.asarray(a), np.asarray(b)),
        lambda x, y, axis=-1: x.mean(axis=axis) - y.mean(axis=axis),
        n_resamples=800, method="percentile", paired=True, vectorized=True,
        confidence_level=0.95, rng=FixedIndices(matrix),
    ).confidence_interval  # fmt: skip
    assert abs(low - interval.low) < 1e-14 and abs(high - interval.high) < 1e-14
    assert estimate == pytest.approx(np.mean(a) - np.mean(b), abs=1e-15)


def test_the_same_seed_gives_the_same_interval() -> None:
    rng = random.Random(2)
    values = [rng.random() for _ in range(60)]
    assert stats.bootstrap(values, seed=4) == stats.bootstrap(values, seed=4)
    assert stats.bootstrap(values, seed=4) != stats.bootstrap(values, seed=5)


def _equal_mass_reference(confidences: list[float], correct: list[bool], bins: int) -> float:
    """An independent equal-mass ECE in numpy: tie groups averaged, then numpy.array_split."""
    conf = np.asarray(confidences, dtype=float)
    ok = np.asarray(correct, dtype=float)
    order = np.argsort(conf, kind="stable")
    conf, ok = conf[order], ok[order]
    _, inverse = np.unique(conf, return_inverse=True)
    smoothed = (np.bincount(inverse, weights=ok) / np.bincount(inverse))[inverse]
    total = 0.0
    for c_bin, a_bin in zip(
        np.array_split(conf, bins), np.array_split(smoothed, bins), strict=True
    ):
        total += len(c_bin) * abs(a_bin.mean() - c_bin.mean())
    return total / len(conf)


def test_equal_mass_ece_matches_an_independent_version_and_ignores_order() -> None:
    rng = random.Random(13)
    for _ in range(200):
        n = rng.randint(300, 900)
        confidences = [round(rng.choice([0.5, 0.9, rng.random()]), 2) for _ in range(n)]
        correct = [rng.random() < c for c in confidences]
        result = stats.equal_mass_ece(confidences, correct)
        assert result["status"] == "ok"
        ece = result["ece"]
        assert isinstance(ece, float)
        assert abs(ece - _equal_mass_reference(confidences, correct, 10)) < 1e-12
        pairs = list(zip(confidences, correct, strict=True))
        rng.shuffle(pairs)
        shuffled = stats.equal_mass_ece([c for c, _ in pairs], [ok for _, ok in pairs])
        assert shuffled["ece"] == ece


def test_equal_mass_ece_is_not_estimable_on_small_bins() -> None:
    result = stats.equal_mass_ece([0.9] * 299, [True] * 299)
    assert result["ece"] is None and result["status"] == "not_estimable"
    assert "need 300" in str(result["reason"])


# --------------------------------------------------------------------------------------------
# For the scoreboard (LP-17b): the stratified bootstrap, weighted shares, Brier reliability
# --------------------------------------------------------------------------------------------


def _labelled(n: int, labels: int, seed: int) -> tuple[list[str], list[str], list[str]]:
    """Label names, truths in strata of uneven size, and predictions right about 2 times in 3."""
    rng = random.Random(seed)
    names = [f"label{j}" for j in range(labels)]
    truths = [rng.choice(names[: 1 + rng.randrange(labels)]) for _ in range(n)]
    predictions = [t if rng.random() < 0.65 else rng.choice(names) for t in truths]
    return names, truths, predictions


def _macro_f1(pairs: Sequence[tuple[str, str]], names: list[str]) -> float:
    found = metrics.classification([t for t, _ in pairs], [p for _, p in pairs], names)
    assert isinstance(found["macro_f1"], float)
    return found["macro_f1"]


def _macro_f1_rows(
    truths: list[str], predictions: list[str], names: list[str], matrix: np.ndarray[Any, Any]
) -> np.ndarray[Any, Any]:
    """Macro-F1 of each resample (row) in numpy: per label present, 2 tp / (predicted + support),
    which is 2PR / (P + R), and 0 when nothing of it is found."""
    truth, predicted = np.asarray(truths)[matrix], np.asarray(predictions)[matrix]
    f1s = []
    for name in names:
        support = (truth == name).sum(axis=1)
        if support.any():
            hits = ((truth == name) & (predicted == name)).sum(axis=1)
            f1s.append(2 * hits / ((predicted == name).sum(axis=1) + support))
    return np.asarray(np.mean(f1s, axis=0))


@pytest.mark.parametrize(("n", "labels", "seed"), [(60, 3, 0), (200, 5, 1), (333, 2, 2026)])
def test_stratified_bootstrap_matches_numpy_on_the_same_resamples(
    n: int, labels: int, seed: int
) -> None:
    names, truths, predictions = _labelled(n, labels, seed)
    # The index matrix, built again in numpy from the same draws of random.Random(seed).
    rng = random.Random(seed)
    draws = np.array([rng.random() for _ in range(500 * n)]).reshape(500, n)
    strata = np.asarray(truths)
    expected = np.empty((500, n), dtype=int)
    for i in range(n):
        pool = np.flatnonzero(strata == strata[i])
        expected[:, i] = pool[(draws[:, i] * len(pool)).astype(int)]
    matrix = np.array(list(stats.stratified_resample_indices(truths, 500, seed)))
    assert (matrix == expected).all()
    # Macro-F1 resampled within each true label, as the scoreboard resamples it.
    pairs = list(zip(truths, predictions, strict=True))
    estimate, low, high = stats.stratified_bootstrap(
        pairs, truths, lambda sample: _macro_f1(sample, names), resamples=500, seed=seed
    )
    whole = _macro_f1_rows(truths, predictions, names, np.arange(n)[np.newaxis, :])[0]
    f1s = _macro_f1_rows(truths, predictions, names, matrix)
    assert abs(estimate - whole) < EXACT
    assert abs(low - np.quantile(f1s, 0.025)) < EXACT
    assert abs(high - np.quantile(f1s, 0.975)) < EXACT
    # A mean, to the ulp, as the plain bootstrap's test checks it.
    values = [rng.random() for _ in range(n)]
    _, low, high = stats.stratified_bootstrap(values, truths, stats.mean, resamples=500, seed=seed)
    means = np.asarray(values)[matrix].mean(axis=1)
    assert abs(low - np.quantile(means, 0.025)) < 1e-15
    assert abs(high - np.quantile(means, 0.975)) < 1e-15


def _share_of_b(sample: Sequence[Hashable]) -> float:
    return sum(1 for stratum in sample if stratum == "b") / len(sample)


def test_stratified_bootstrap_keeps_each_stratums_size() -> None:
    strata: list[Hashable] = ["a"] * 12 + ["b"] * 30 + [None] * 3 + [("x", 1)]
    random.Random(8).shuffle(strata)
    drawn: dict[Hashable, set[int]] = {}
    for row in stats.stratified_resample_indices(strata, 300, 6):
        assert [strata[j] for j in row] == strata  # each place keeps its case's stratum
        for j in row:
            drawn.setdefault(strata[j], set()).add(j)
    for stratum in set(strata):  # and every case of a stratum is drawn in some resample
        assert drawn[stratum] == {i for i, s in enumerate(strata) if s == stratum}
    # So a figure of the strata alone never varies: its interval is its estimate.
    assert stats.stratified_bootstrap(strata, strata, _share_of_b, resamples=200, seed=1) == (
        30 / 46,
        30 / 46,
        30 / 46,
    )


def test_stratified_bootstrap_with_one_stratum_is_the_plain_bootstrap() -> None:
    rng = random.Random(4)
    for n, seed in [(1, 0), (25, 3), (150, 2026)]:
        values = [rng.random() for _ in range(n)]
        one = ["all"] * n
        rows = list(stats.stratified_resample_indices(one, 300, seed))
        assert rows == list(stats.resample_indices(n, 300, seed))
        assert stats.stratified_bootstrap(
            values, one, stats.mean, resamples=300, seed=seed
        ) == stats.bootstrap(values, resamples=300, seed=seed)


def _bounds(found: stats.WeightedProportion) -> tuple[float, float, float, float]:
    assert found.estimate is not None and found.effective_n is not None
    assert found.low is not None and found.high is not None
    return found.estimate, found.effective_n, found.low, found.high


def test_weighted_proportion_matches_scipy_beta_at_the_effective_counts() -> None:
    # By hand: weights 1, 1, 2 and 4, hits on the first and third. The total weight is 8, the
    # hits' 3 and the squares' 22: estimate 3/8, effective n 64/22, x = 24/22 hits and 40/22
    # misses. Both counts are 1 or more, so both bounds are Korn-Graubard's at those counts.
    found = stats.weighted_proportion([True, False, True, False], [1, 1, 2, 4.0])
    estimate, effective_n, low, high = _bounds(found)
    assert estimate == 3 / 8 and effective_n == pytest.approx(64 / 22, rel=1e-15)
    assert abs(low - st.beta.ppf(0.025, 24 / 22, 40 / 22 + 1)) < EXACT
    assert abs(high - st.beta.ppf(0.975, 24 / 22 + 1, 40 / 22)) < EXACT
    assert found.to_dict() == {
        "n": 4, "estimate": 0.375, "effective_n": 2.909091, "low": 0.012474, "high": 0.926301,
    }  # fmt: skip
    rng = random.Random(17)
    for _ in range(300):
        n = rng.randint(1, 400)
        # Weights as a stratified draw makes them (N_h / n_h), and arbitrary ones.
        weights = [rng.choice([rng.uniform(0.5, 40.0), 600 / rng.randint(1, 60)]) for _ in range(n)]
        share = rng.choice([0.0, 1.0, rng.random(), rng.random()])  # no hits and all hits too
        hits = [rng.random() < share for _ in range(n)]
        level = rng.choice([0.9, 0.95, 0.99])
        estimate, effective_n, low, high = _bounds(stats.weighted_proportion(hits, weights, level))
        w, y = np.asarray(weights), np.asarray(hits, dtype=float)
        model_n = float(w.sum() ** 2 / (w * w).sum())
        x = float((w * y).sum() / w.sum() * model_n)
        assert estimate == pytest.approx((w * y).sum() / w.sum(), rel=1e-13)
        assert effective_n == pytest.approx(model_n, rel=1e-13)
        # Korn-Graubard at the real counts, a bound clamped where its count is under 1.
        alpha, misses = 1 - level, model_n - x
        model_low = 0.0
        if round(x, 9) >= 1:
            model_low = st.beta.ppf(alpha / 2, x, misses + 1)
        model_high = 1.0
        if round(misses, 9) >= 1:
            model_high = st.beta.ppf(1 - alpha / 2, x + 1, misses)
        assert abs(low - model_low) < EXACT and abs(high - model_high) < EXACT, (n, level)
        assert low <= estimate <= high


def test_weighted_proportion_is_korn_graubard_at_no_hits_and_at_all_hits() -> None:
    # No false automation is the usual figure, so this upper bound is the one most read. At no
    # hits Korn-Graubard's upper bound is Beta(1, n*)'s quantile, 1 - (alpha/2)^(1/n*), and at
    # all hits the lower bound is its mirror, (alpha/2)^(1/n*). Neither rounds the counts to
    # whole numbers, which would widen it (0.032198 for 0.027287 in the first set).
    for weights, model_n in [
        ([1.0] * 250 + [7.0] * 50, 600**2 / 2700),  # squares: 250 + 50 * 49
        ([1.0] * 30 + [3.0], 33**2 / 39),
        ([1, 1, 2, 4.0], 64 / 22),
    ]:
        n = len(weights)
        for level in (0.9, 0.95, 0.99):
            tail = (1 - level) / 2
            estimate, effective_n, low, high = _bounds(
                stats.weighted_proportion([False] * n, weights, level)
            )
            assert (estimate, low) == (0.0, 0.0)
            assert effective_n == pytest.approx(model_n, rel=1e-15)
            assert high == pytest.approx(1 - tail ** (1 / model_n), rel=1e-12)
            assert abs(high - st.beta.ppf(1 - tail, 1, model_n)) < EXACT
            estimate, _, low, high = _bounds(stats.weighted_proportion([True] * n, weights, level))
            assert (estimate, high) == (1.0, 1.0)
            assert low == pytest.approx(tail ** (1 / model_n), rel=1e-12)
            assert abs(low - st.beta.ppf(tail, model_n, 1)) < EXACT
    assert stats.weighted_proportion([False] * 300, [1.0] * 250 + [7.0] * 50).to_dict() == {
        "n": 300, "estimate": 0.0, "effective_n": 133.333333, "low": 0.0, "high": 0.027287,
    }  # fmt: skip
    assert stats.weighted_proportion([True] * 31, [1.0] * 30 + [3.0]).to_dict() == {
        "n": 31, "estimate": 1.0, "effective_n": 27.923077, "low": 0.876246, "high": 1.0,
    }  # fmt: skip


def test_weighted_proportion_clamps_a_bound_only_where_its_count_is_under_one() -> None:
    # Weights 1 and 3: effective n 8/5. A hit on the light case is 2/5 of a hit, where
    # beta_ppf takes no shape, so the lower bound is 0, wider than Korn-Graubard's (scipy takes
    # shapes under 1); its 6/5 misses are 1 or more, so the upper bound is Korn-Graubard's.
    _, _, low, high = _bounds(stats.weighted_proportion([True, False], [1, 3.0]))
    assert low == 0.0 < st.beta.ppf(0.025, 2 / 5, 6 / 5 + 1)
    assert abs(high - st.beta.ppf(0.975, 2 / 5 + 1, 6 / 5)) < EXACT
    # The mirror: a hit on the heavy case leaves 2/5 of a miss, so the upper bound is 1.
    _, _, low, high = _bounds(stats.weighted_proportion([False, True], [1, 3.0]))
    assert abs(low - st.beta.ppf(0.025, 6 / 5, 2 / 5 + 1)) < EXACT
    assert high == 1.0 > st.beta.ppf(0.975, 6 / 5 + 1, 2 / 5)


def test_weighted_proportion_with_unit_weights_is_clopper_pearson() -> None:
    for k, n in _grid():
        hits = [True] * k + [False] * (n - k)
        for level in (0.95, 0.99):
            estimate, effective_n, low, high = _bounds(
                stats.weighted_proportion(hits, [1.0] * n, level)
            )
            assert estimate == k / n and effective_n == n
            assert (low, high) == stats.clopper_pearson(k, n, level), (k, n, level)
            model_low, model_high = proportion_confint(k, n, alpha=1 - level, method="beta")
            assert abs(low - model_low) < EXACT and abs(high - model_high) < EXACT, (k, n, level)
    # One weight for every case, whatever it is, gives the same share: the sums are exact.
    for weight in (2, 0.1, 600 / 7, 1e-3):
        found = stats.weighted_proportion([True] * 37 + [False] * 563, [weight] * 600)
        assert _bounds(found) == (37 / 600, 600.0, *stats.clopper_pearson(37, 600))
    assert stats.weighted_proportion([], []).to_dict() == {
        "n": 0, "estimate": None, "effective_n": None, "low": None, "high": None,
    }  # fmt: skip


def _reliability_reference(
    distributions: list[dict[str, float]], truths: list[str], labels: list[str], bins: int
) -> float:
    """An independent reliability term in numpy: tie groups of top-label confidence averaged
    (forecasts and outcomes), then numpy.array_split."""
    forecast = np.array([[d[label] for label in labels] for d in distributions])
    outcome = np.array([[float(label == t) for label in labels] for t in truths])
    confidence = forecast.max(axis=1)
    order = np.argsort(confidence, kind="stable")
    forecast, outcome, confidence = forecast[order], outcome[order], confidence[order]
    _, inverse, counts = np.unique(confidence, return_inverse=True, return_counts=True)

    def tie_means(matrix: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
        sums = np.zeros((len(counts), matrix.shape[1]))
        np.add.at(sums, inverse, matrix)
        return np.asarray((sums / counts[:, np.newaxis])[inverse])

    total = 0.0
    for f_bin, o_bin in zip(
        np.array_split(tie_means(forecast), bins),
        np.array_split(tie_means(outcome), bins),
        strict=True,
    ):
        total += len(f_bin) * float(((f_bin.mean(axis=0) - o_bin.mean(axis=0)) ** 2).sum())
    return total / len(forecast)


def _forecasts(rng: random.Random, labels: list[str], n: int) -> list[dict[str, float]]:
    """Half from a coarse grid (tied top-label confidences over different forecasts), half
    continuous."""
    distributions = []
    for _ in range(n):
        if rng.random() < 0.5:
            raw = [float(rng.choice([1, 2, 3, 6])) for _ in labels]
        else:
            raw = [rng.random() + 1e-3 for _ in labels]
        total = math.fsum(raw)
        distributions.append({label: p / total for label, p in zip(labels, raw, strict=True)})
    return distributions


def test_brier_reliability_matches_an_independent_numpy_implementation() -> None:
    rng = random.Random(19)
    for _ in range(60):
        labels = ["a", "b", "c", "d"][: rng.randint(2, 4)]
        n = rng.randint(300, 700)
        distributions = _forecasts(rng, labels, n)
        truths = [
            rng.choices(labels, weights=[d[label] for label in labels])[0]
            if rng.random() < 0.7
            else rng.choice(labels)
            for d in distributions
        ]
        result = stats.brier_reliability(distributions, truths, labels)
        assert result["status"] == "ok"
        value = result["reliability"]
        assert isinstance(value, float)
        assert abs(value - _reliability_reference(distributions, truths, labels, 10)) < EXACT
        pairs = list(zip(distributions, truths, strict=True))
        rng.shuffle(pairs)
        shuffled = stats.brier_reliability([d for d, _ in pairs], [t for _, t in pairs], labels)
        assert shuffled["reliability"] == value  # the same for any order
    # Where each bin holds one forecast, Murphy's decomposition is exact:
    # Brier = reliability - resolution + uncertainty.
    labels = ["a", "b", "c"]
    distributions, truths = [], []
    for j in range(10):
        top = 0.4 + 0.05 * j
        forecast = {label: (1 - top) / 2 for label in labels} | {labels[j % 3]: top}
        for _ in range(30):
            distributions.append(forecast)
            truths.append(rng.choices(labels, weights=[forecast[label] for label in labels])[0])
    reliability = stats.brier_reliability(distributions, truths, labels)["reliability"]
    assert isinstance(reliability, float)
    outcome = np.array([[float(label == t) for label in labels] for t in truths])
    bins = outcome.reshape(10, 30, 3).mean(axis=1)  # the forecasts come grouped, as bins are
    base_rate = outcome.mean(axis=0)
    resolution = float(((bins - base_rate) ** 2).sum(axis=1).mean())
    uncertainty = float((base_rate * (1 - base_rate)).sum())
    brier = metrics.brier(distributions, truths)
    assert brier is not None
    assert abs(brier - (reliability - resolution + uncertainty)) < EXACT


def test_brier_reliability_is_not_estimable_under_30_per_bin() -> None:
    labels = ["yes", "no"]
    distributions = [{"yes": 0.8, "no": 0.2}] * 300
    truths = ["yes"] * 240 + ["no"] * 60
    small = stats.brier_reliability(distributions[:299], truths[:299], labels)
    assert small["reliability"] is None and small["status"] == "not_estimable"
    assert "need 300" in str(small["reason"])
    assert stats.brier_reliability(distributions, truths, labels)["reliability"] == 0.0
    five = stats.brier_reliability(distributions[:150], truths[:150], labels, bins=5)
    assert five["status"] == "ok"
    strict = stats.brier_reliability(distributions, truths, labels, bins=5, min_bin_size=61)
    assert strict["status"] == "not_estimable" and "need 305" in str(strict["reason"])
    # A forecast of 0.8 met half the time is 0.3 off on each label: 0.09 + 0.09.
    half = stats.brier_reliability(distributions, ["yes", "no"] * 150, labels)["reliability"]
    assert half == pytest.approx(0.18, abs=1e-15)


def test_the_scoreboards_statistics_refuse_what_they_cannot_compute() -> None:
    with pytest.raises(ValueError, match="no records"):
        stats.stratified_bootstrap([], [], stats.mean)
    with pytest.raises(ValueError, match="differ in length"):
        stats.stratified_bootstrap([0.1, 0.2], ["a"], stats.mean)
    with pytest.raises(TypeError, match="seed"):
        next(stats.stratified_resample_indices(["a"], 1, True))
    for weights in ([0.0], [-1.0], [math.inf], [math.nan]):
        with pytest.raises(ValueError, match="finite and positive"):
            stats.weighted_proportion([True], weights)
    with pytest.raises(ValueError, match="differ in length"):
        stats.weighted_proportion([True, False], [1.0])
    with pytest.raises(ValueError, match="level"):
        stats.weighted_proportion([True], [1.0], level=1.0)
    with pytest.raises(TypeError, match="hit"):
        stats.weighted_proportion([1], [1.0])  # type: ignore[list-item]  # a count is not a hit
    with pytest.raises(TypeError, match="weight"):
        stats.weighted_proportion([True], [True])  # a bool is not a weight
    one = [{"a": 0.6, "b": 0.4}]
    for distributions, truths, labels, reason in [
        (one, ["c"], ["a", "b"], "one of the labels"),
        ([{"a": 1.0}], ["a"], ["a", "b"], "every label"),
        (one, ["a", "b"], ["a", "b"], "differ in length"),
        (one, ["a"], ["a", "a"], "distinct"),
        ([{"a": math.nan, "b": 0.4}], ["a"], ["a", "b"], "finite"),
    ]:
        with pytest.raises(ValueError, match=reason):
            stats.brier_reliability(distributions, truths, labels)
    with pytest.raises(ValueError, match="at least 1"):
        stats.brier_reliability(one, ["a"], ["a", "b"], bins=0)


@pytest.mark.slow
def test_clopper_pearson_over_a_wide_grid() -> None:
    for n in range(1, 601):
        for k in range(n + 1):
            low, high = stats.clopper_pearson(k, n)
            model_low, model_high = proportion_confint(k, n, alpha=0.05, method="beta")
            assert abs(low - model_low) < EXACT and abs(high - model_high) < EXACT, (k, n)


@pytest.mark.parametrize(
    "call",
    [
        lambda: stats.bootstrap([1.0, 2.0], resamples=0),
        lambda: stats.bootstrap_defined([1.0, 2.0], stats.mean, resamples=0),
        lambda: stats.paired_bootstrap([1.0], [2.0], resamples=0),
        lambda: stats.stratified_bootstrap([1.0, 2.0], ["a", "b"], stats.mean, resamples=0),
        lambda: stats.bootstrap([1.0, 2.0], level=1.5),
        lambda: stats.paired_bootstrap([1.0], [2.0], level=0.0),
    ],
)
def test_a_bootstrap_without_resamples_or_a_level_in_0_1_is_refused(call: Any) -> None:
    """Before, no resample raised an IndexError deep in `percentile`, and a level of 1.5 gave
    'bounds' with the low above the high."""
    with pytest.raises(ValueError, match=r"resamples must be|level must be"):
        call()


def test_equal_mass_ece_refuses_no_bins_and_a_confidence_that_is_not_a_number() -> None:
    """Before, bins=0 raised ZeroDivisionError, and a NaN confidence made an empty tie group
    (NaN != NaN): another ZeroDivisionError, with the cases around it in no order."""
    with pytest.raises(ValueError, match="bins and min_bin_size"):
        stats.equal_mass_ece([0.5] * 10, [True] * 10, bins=0)
    with pytest.raises(ValueError, match="bins and min_bin_size"):
        stats.equal_mass_ece([], [], min_bin_size=0)
    with pytest.raises(ValueError, match="finite"):
        stats.equal_mass_ece([math.nan] + [0.5] * 99, [True] * 100, bins=2, min_bin_size=1)
