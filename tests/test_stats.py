# SPDX-License-Identifier: Apache-2.0
"""mendari.evals.stats against scipy and statsmodels (development dependencies only): the exact
methods to 1e-12, the bootstrap by feeding both the same index matrix, and the known values the
launch plan's statistics rest on (LP-10)."""

from __future__ import annotations

import math
import random
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


def test_a_proportion_refuses_what_is_not_k_of_n() -> None:
    # 3 hits of no cases used to come back as a blank interval, as if nothing had run.
    for bad in [(3, 0), (-1, 0), (-1, 5), (6, 5), (0, -1)]:
        with pytest.raises(ValueError, match="0 <= k <= n"):
            stats.proportion(*bad)


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
# Brier reliability (the calibration report's, `evals/confidence.py`)
# --------------------------------------------------------------------------------------------


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


def test_brier_reliability_refuses_what_it_cannot_compute() -> None:
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
