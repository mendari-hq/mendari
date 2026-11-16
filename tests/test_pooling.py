# SPDX-License-Identifier: Apache-2.0
"""mendari.evals.pooling (LP-22b) against hand-computed references, scipy and statsmodels
(development dependencies only): the pooled interval, DerSimonian-Laird, the chi-square tail and
the incomplete gamma, the exact test of homogeneity and its seeded permutation estimate."""

from __future__ import annotations

import itertools
import json
import math
from fractions import Fraction
from typing import Any

import numpy as np
import pytest
from scipy import special
from scipy import stats as st
from statsmodels.stats.meta_analysis import combine_effects
from statsmodels.stats.proportion import proportion_confint

from mendari.evals import pooling, stats

EXACT = 1e-12


def test_the_pooled_interval_matches_a_hand_computed_reference() -> None:
    # 45 of 50, 80 of 100 and 18 of 20: 143 of 170 summed. Its exact 95% interval is the
    # Beta(143, 28) quantile at 0.025 and the Beta(144, 27) quantile at 0.975.
    pooled = pooling.pooled_fixed([(45, 50), (80, 100), (18, 20)])
    assert pooled["method"] == "pooled (fixed effect, summed counts)"
    assert (pooled["status"], pooled["sites"], pooled["k"], pooled["n"]) == ("ok", 3, 143, 170)
    assert round(pooled["low"], 4) == 0.7774 and round(pooled["high"], 4) == 0.8926
    low, high = proportion_confint(143, 170, alpha=0.05, method="beta")
    assert abs(pooled["low"] - low) < EXACT and abs(pooled["high"] - high) < EXACT
    assert abs(pooled["low"] - st.beta.ppf(0.025, 143, 28)) < EXACT


def _statsmodels_dl(sites: list[tuple[int, int]]) -> tuple[float, float, float, float]:
    eff, var = [], []
    for k, n in sites:
        hits, misses = (k + 0.5, n - k + 0.5) if k in (0, n) else (k, n - k)
        eff.append(math.log(hits / misses))
        var.append(1 / hits + 1 / misses)
    result = combine_effects(np.array(eff), np.array(var), method_re="dl")
    frame = result.summary_frame()
    # statsmodels leaves tau^2 negative when Q < K - 1; DerSimonian-Laird truncates it at 0,
    # where the random-effects estimate is the fixed-effect one.
    tau2 = max(0.0, float(result.tau2))
    row = frame.loc["random effect" if tau2 > 0 else "fixed effect"]
    return (
        float(special.expit(row["eff"])),
        float(special.expit(row["ci_low"])),
        float(special.expit(row["ci_upp"])),
        tau2,
    )


RANDOM_SITES = [
    [(8, 10), (15, 20), (30, 40)],
    [(40, 50), (30, 60), (90, 100), (12, 30)],
    [(20, 20), (18, 20), (5, 10)],  # a site at k = n: 0.5 added to both its cells
    [(0, 12), (3, 30), (9, 25), (1, 40), (14, 50)],
    [(500, 1000), (480, 1000), (530, 1000)],
]


def test_the_random_effects_estimate_matches_a_hand_computed_reference_and_statsmodels() -> None:
    # By hand, 40 of 50, 30 of 60, 90 of 100 and 12 of 30:
    #   y = log 4, 0, log 9, log(2/3);  v = 1/40 + 1/10, 1/30 + 1/30, 1/90 + 1/10, 1/12 + 1/18
    #   w = 8, 15, 9, 7.2 (sum 39.2); y_F = sum w y / sum w = 0.712909;
    #   Q = sum w (y - y_F)^2 = 40.085383; df = 3;
    #   tau^2 = (Q - 3) / (39.2 - (64 + 225 + 81 + 51.84) / 39.2) = 1.304043;
    #   mu = sum y / (v + tau^2) / sum 1 / (v + tau^2) = 0.792412, se = 0.594545;
    #   estimate expit(mu) = 0.688349, interval expit(mu -/+ 1.959964 se) = (0.407847, 0.876284).
    answer = pooling.random_effects(RANDOM_SITES[1])
    assert answer["method"] == "random effects (DerSimonian-Laird, logit scale)"
    assert answer["status"] == "ok" and answer["sites"] == 4
    assert answer["q"] == pytest.approx(40.085383, abs=2e-6)
    assert answer["tau2"] == pytest.approx(1.304043, abs=2e-6)
    assert answer["estimate"] == pytest.approx(0.688349, abs=2e-6)
    assert (answer["low"], answer["high"]) == pytest.approx((0.407847, 0.876284), abs=2e-6)
    for sites in RANDOM_SITES:
        got = pooling.random_effects(sites)
        estimate, low, high, tau2 = _statsmodels_dl(sites)
        assert got["estimate"] == pytest.approx(estimate, abs=1e-6), sites
        assert (got["low"], got["high"]) == pytest.approx((low, high), abs=1e-6), sites
        assert got["tau2"] == pytest.approx(tau2, abs=1e-6), sites
        assert got["low"] <= got["estimate"] <= got["high"]


def _tables() -> list[list[tuple[int, int]]]:
    return [
        [(45, 60), (30, 50)],
        [(80, 100), (70, 100), (95, 120)],
        [(20, 40), (25, 40), (30, 40), (35, 40)],
        [(500, 1000), (480, 1000), (530, 1000)],
        [(12, 30), (15, 30), (18, 30), (10, 30), (20, 30)],
        [(60, 75), (61, 75)],
    ]


def test_chi_square_matches_scipy() -> None:
    for sites in _tables():
        answer = pooling.homogeneity(sites)
        assert answer["method"] == "Pearson chi-square", sites
        table = np.array([[k, n - k] for k, n in sites])
        statistic, p, df, expected = st.chi2_contingency(table, correction=False)
        assert expected.min() >= 5
        assert answer["df"] == df
        assert answer["statistic"] == pytest.approx(statistic, abs=1e-6)
        assert answer["p"] == pytest.approx(p, abs=1e-6)
        assert answer["differ"] is (answer["p"] < 0.05)


def test_the_incomplete_gamma_matches_scipy() -> None:
    worst = 0.0
    for a in (0.5, 1, 1.5, 2, 2.5, 3, 4.5, 7, 10, 15, 24.5, 50, 100, 250):
        for x in (0, 1e-6, 0.01, 0.1, 0.5, 1, 2, 3, 5, 8, 10, 15, 20, 30, 50, 80, 120, 200, 400):
            reference = float(special.gammaincc(a, x))
            got = stats.gamma_q(a, x)
            if reference > 1e-290:
                worst = max(worst, abs(got - reference) / reference)
            else:
                assert got < 1e-280
    assert worst < 1e-12
    for df in range(1, 12):
        for statistic in (0.0, 0.3, 1.0, 3.84, 7.5, 15.0, 40.0):
            assert stats.chi2_sf(statistic, df) == pytest.approx(
                st.chi2.sf(statistic, df), rel=1e-12, abs=1e-300
            )
    for bad in (0, -1, math.inf, math.nan):
        with pytest.raises(ValueError):
            stats.gamma_q(bad, 1.0)
    with pytest.raises(ValueError):
        stats.gamma_q(1.0, -1.0)
    with pytest.raises(ValueError):
        stats.chi2_sf(1.0, 0)


SMALL = [
    [(19, 20), (20, 20), (18, 20)],  # critical-label detection: few misses
    [(9, 10), (3, 10)],
    [(2, 12), (0, 15), (5, 11), (1, 9)],
    [(30, 30), (25, 30)],
    [(1, 3), (2, 2), (0, 4)],
    [(10, 10), (12, 12)],  # one column empty: a single table
]


def test_small_expected_counts_never_use_chi_square() -> None:
    for sites in SMALL:
        answer = pooling.homogeneity(sites)
        assert answer["smallest_expected"] < 5, sites
        assert answer["method"] != "Pearson chi-square", sites
        assert answer["method"] == "exact (every table with the same margins)"
        assert "statistic" not in answer and "df" not in answer


def _scipy_exact(sites: list[tuple[int, int]]) -> float:
    """The reference: every table with the observed margins, its probability from scipy's
    multivariate hypergeometric distribution, summed where no larger than the observed one's."""
    sizes = [n for _, n in sites]
    total = sum(k for k, _ in sites)
    distribution = st.multivariate_hypergeom(m=sizes, n=total)
    observed = distribution.pmf([k for k, _ in sites])
    p = 0.0
    for ks in itertools.product(*(range(n + 1) for n in sizes)):
        if sum(ks) == total:
            prob = distribution.pmf(list(ks))
            if prob <= observed * (1 + 1e-7):
                p += prob
    return min(1.0, p)


def test_the_exact_test_matches_a_scipy_reference() -> None:
    for sites in SMALL:
        answer = pooling.homogeneity(sites)
        assert answer["p"] == pytest.approx(_scipy_exact(sites), abs=2e-6), sites
    # Two sites: Fisher's exact test.
    _, fisher = st.fisher_exact([[9, 1], [3, 7]])
    assert pooling.homogeneity([(9, 10), (3, 10)])["p"] == pytest.approx(fisher, abs=2e-6)
    assert pooling.homogeneity([(10, 10), (12, 12)])["p"] == 1.0


def test_the_permutation_test_is_seeded_and_writes_its_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sites = [(19, 20), (16, 20), (20, 20), (17, 20)]
    exact = pooling.homogeneity(sites)
    assert exact["method"] == "exact (every table with the same margins)"
    monkeypatch.setattr(pooling, "MAX_TABLES", 10)
    first = pooling.homogeneity(sites)
    second = pooling.homogeneity(sites)
    assert first["method"] == "permutation (seeded)"
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["seed"] == pooling._seed(sites) and first["resamples"] == 10_000
    # 10,000 resamples estimate the exact p-value to within a few standard errors.
    assert abs(first["p"] - exact["p"]) < 4 * math.sqrt(exact["p"] * (1 - exact["p"]) / 10_000)
    other = pooling.homogeneity([(18, 20), (16, 20), (20, 20), (17, 20)])
    assert other["seed"] != first["seed"]
    # The misses are dealt when they are the smaller column; hits when they are.
    flipped = pooling.homogeneity([(n - k, n) for k, n in sites])
    assert flipped["method"] == "permutation (seeded)"
    assert abs(flipped["p"] - exact["p"]) < 0.02


def test_one_site_or_two_reads_insufficient_evidence_where_the_method_says() -> None:
    one = [(18, 20)]
    two = [(18, 20), (15, 20)]
    for answer in (
        pooling.pooled_fixed(one),
        pooling.range_of(one),
        pooling.homogeneity(one),
        pooling.random_effects(one),
        pooling.random_effects(two),
        pooling.pooled_fixed([]),
    ):
        assert answer["status"] == "insufficient evidence"
        assert "k" not in answer and "estimate" not in answer and "p" not in answer
    assert pooling.random_effects(two)["needs_sites"] == 3
    assert pooling.pooled_fixed(two)["status"] == "ok"
    assert pooling.homogeneity(two)["status"] == "ok"
    span = pooling.range_of([(18, 20), (15, 20), (9, 10)])
    assert (span["lowest"]["k"], span["lowest"]["n"]) == (15, 20)
    assert (span["highest"]["k"], span["highest"]["n"]) == (18, 20)  # 9/10 ties; first kept
    assert pooling.random_effects([(18, 20), (15, 20), (9, 10)])["status"] == "ok"


BAD: list[Any] = [
    [(0.9, 10), (8, 10)],
    [(9, 10.0), (8, 10)],
    [(Fraction(9, 10), 1), (8, 10)],
    [(True, 1), (8, 10)],
    [(11, 10), (8, 10)],
    [(-1, 10), (8, 10)],
    [(0, 0), (8, 10)],
    [("9", 10), (8, 10)],
    [(np.int64(9), 10), (8, 10)],
    [(9, 10, 1), (8, 10)],
    [0.9, 0.8],
    "9/10",
    {"k": 9, "n": 10},
]


@pytest.mark.parametrize("sites", BAD)
def test_rates_and_fractions_are_refused(sites: Any) -> None:
    for method in (
        pooling.pooled_fixed,
        pooling.random_effects,
        pooling.homogeneity,
        pooling.range_of,
    ):
        with pytest.raises(ValueError):
            method(sites)
