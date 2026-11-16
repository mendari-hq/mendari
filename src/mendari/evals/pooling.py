# SPDX-License-Identifier: Apache-2.0
"""Pooled site evidence (LP-22, ADR-0017): the statistics that put several sites' k of n side by
side, in pure Python, each checked against scipy or statsmodels in tests/test_pooling.py.

Every function takes the sites as whole counts, `[(k, n), ...]`, one pair per site (k of n
cases right, detected or automated), and refuses anything else, as `figures.of` does: a rate or
a fraction is not evidence a registry can check. Each answer is a JSON-ready mapping that names
its method and says `insufficient evidence` where the method needs more sites than it was given.

- `pooled_fixed`: summed k of summed n, with its exact Clopper-Pearson interval. It weighs each
  case once, so a large site counts for more; its name says so.
- `random_effects`: DerSimonian and Laird (1986) on the logit scale, 0.5 added to both cells of
  a site whose k is 0 or n, with its 95% interval; from `RANDOM_EFFECTS_MIN_SITES` sites.
- `homogeneity`: do the sites differ by more than chance? Pearson's chi-square on the sites by
  (hit, miss) table when every expected count is at least 5; otherwise never chi-square, but the
  exact test (every table with the same margins, at most `MAX_TABLES` of them), or past that a
  permutation test of `PERMUTATIONS` resamples whose seed comes from the counts and is written
  in the answer. At `ALPHA`.
- `range_of`: the lowest and highest site rate, each with its k of n.

Derived numbers (an estimate, tau squared, a statistic, a p-value) are rounded to
`stats.DIGITS` places, as stored bounds are, so an answer is the same bytes on every platform;
a figure's `low` and `high` are left as `stats.interval` gives them, as `figures.of` does.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import random
from collections.abc import Sequence
from fractions import Fraction
from statistics import NormalDist
from typing import Any

from mendari.evals import stats

ALPHA = 0.05
LEVEL = stats.LEVEL
MIN_SITES = 2  # a pool, a range or a comparison of one site is no pool
RANDOM_EFFECTS_MIN_SITES = 3  # owner decision 4: DerSimonian-Laird from 3 sites
MIN_EXPECTED = 5  # chi-square only when every expected count is at least this
MAX_TABLES = 1_000_000  # the exact test enumerates at most this many tables
PERMUTATIONS = 10_000
CONTINUITY = 0.5
TIE = 1e-7  # tables this close in probability to the observed one count as ties (scipy's rule)
INSUFFICIENT = "insufficient evidence"

FIXED = "pooled (fixed effect, summed counts)"
RANDOM = "random effects (DerSimonian-Laird, logit scale)"
CHI_SQUARE = "Pearson chi-square"
EXACT = "exact (every table with the same margins)"
PERMUTATION = "permutation (seeded)"

Site = tuple[int, int]


def _sites(sites: Sequence[Site]) -> list[Site]:
    """The sites as whole (k, n) pairs, 0 <= k <= n, n >= 1. Anything else is refused."""
    if isinstance(sites, (str, bytes)) or not isinstance(sites, Sequence):
        raise ValueError("sites must be a sequence of (k, n) pairs")
    checked: list[Site] = []
    for site in sites:
        if not (isinstance(site, (tuple, list)) and len(site) == 2):
            raise ValueError(f"not a (k, n) pair: {site!r}")
        k, n = site
        if any(isinstance(v, bool) or not isinstance(v, int) for v in (k, n)):
            raise ValueError(f"not whole counts: {site!r}")
        if not (n >= 1 and 0 <= k <= n):
            raise ValueError(f"not k of n: {site!r}")
        checked.append((k, n))
    return checked


def _round(value: float) -> float:
    return round(value, stats.DIGITS)


def _figure(k: int, n: int) -> dict[str, Any]:
    low, high = stats.interval(k, n)
    return {"k": k, "n": n, "low": low, "high": high}


def _insufficient(method: str, count: int, needed: int) -> dict[str, Any]:
    return {"method": method, "sites": count, "status": INSUFFICIENT, "needs_sites": needed}


# --------------------------------------------------------------------------------------------
# The pooled figure and the range
# --------------------------------------------------------------------------------------------


def pooled_fixed(sites: Sequence[Site]) -> dict[str, Any]:
    """Summed k of summed n with its exact interval, named for what it is."""
    checked = _sites(sites)
    if len(checked) < MIN_SITES:
        return _insufficient(FIXED, len(checked), MIN_SITES)
    k = sum(site[0] for site in checked)
    n = sum(site[1] for site in checked)
    return {"method": FIXED, "sites": len(checked), "status": "ok", **_figure(k, n)}


def range_of(sites: Sequence[Site]) -> dict[str, Any]:
    """The lowest and the highest site rate, each as its k of n with its interval. Rates are
    compared exactly; of two equal rates the first site given is taken."""
    checked = _sites(sites)
    if len(checked) < MIN_SITES:
        return _insufficient("range", len(checked), MIN_SITES)
    rates = [Fraction(k, n) for k, n in checked]
    low = min(range(len(checked)), key=lambda i: rates[i])
    high = max(range(len(checked)), key=lambda i: (rates[i], -i))
    return {
        "method": "range",
        "sites": len(checked),
        "status": "ok",
        "lowest": _figure(*checked[low]),
        "highest": _figure(*checked[high]),
    }


# --------------------------------------------------------------------------------------------
# Random effects
# --------------------------------------------------------------------------------------------


def _logit(k: int, n: int) -> tuple[float, float]:
    """A site's log odds and its variance, 0.5 added to both cells when either is empty."""
    hits, misses = float(k), float(n - k)
    if k == 0 or k == n:
        hits += CONTINUITY
        misses += CONTINUITY
    return math.log(hits / misses), 1.0 / hits + 1.0 / misses


def _expit(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    e = math.exp(value)
    return e / (1.0 + e)


def random_effects(sites: Sequence[Site], level: float = LEVEL) -> dict[str, Any]:
    """DerSimonian and Laird (1986) on the logit scale.

    Each site i gives y_i = log(k / (n - k)) and v_i = 1/k + 1/(n - k) (0.5 added to k and to
    n - k when either is 0). With fixed weights w_i = 1/v_i, Cochran's
    Q = sum w_i (y_i - y_F)^2 about the fixed estimate y_F; the between-site variance
    tau^2 = max(0, (Q - (K - 1)) / (sum w - sum w^2 / sum w)); random weights
    w*_i = 1/(v_i + tau^2); the estimate mu = sum w* y / sum w* with standard error
    1/sqrt(sum w*), its interval mu -/+ z se (z the normal quantile at the level), each end
    turned back into a proportion. I^2 = max(0, (Q - (K - 1)) / Q).
    """
    checked = _sites(sites)
    if not 0.0 < level < 1.0:
        raise ValueError("level must be in (0, 1)")
    if len(checked) < RANDOM_EFFECTS_MIN_SITES:
        return _insufficient(RANDOM, len(checked), RANDOM_EFFECTS_MIN_SITES)
    pairs = [_logit(k, n) for k, n in checked]
    y = [p[0] for p in pairs]
    v = [p[1] for p in pairs]
    w = [1.0 / vi for vi in v]
    sum_w = math.fsum(w)
    fixed = math.fsum(wi * yi for wi, yi in zip(w, y, strict=True)) / sum_w
    q = math.fsum(wi * (yi - fixed) ** 2 for wi, yi in zip(w, y, strict=True))
    df = len(checked) - 1
    scale = sum_w - math.fsum(wi * wi for wi in w) / sum_w
    tau2 = max(0.0, (q - df) / scale) if scale > 0 else 0.0
    w_star = [1.0 / (vi + tau2) for vi in v]
    sum_star = math.fsum(w_star)
    mu = math.fsum(wi * yi for wi, yi in zip(w_star, y, strict=True)) / sum_star
    se = 1.0 / math.sqrt(sum_star)
    z = NormalDist().inv_cdf(0.5 + level / 2.0)
    i2 = max(0.0, (q - df) / q) if q > 0 else 0.0
    return {
        "method": RANDOM,
        "sites": len(checked),
        "status": "ok",
        "estimate": _round(_expit(mu)),
        "low": _round(_expit(mu - z * se)),
        "high": _round(_expit(mu + z * se)),
        "level": level,
        "tau2": _round(tau2),
        "q": _round(q),
        "i2": _round(i2),
        "continuity": CONTINUITY,
    }


# --------------------------------------------------------------------------------------------
# Homogeneity
# --------------------------------------------------------------------------------------------


def _log_choose(n: int, k: int) -> float:
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def _table_count(sizes: Sequence[int], total: int) -> int:
    """How many tables have these row sizes and this many hits: ways to write `total` as
    k_1 + ... + k_K with 0 <= k_i <= n_i. Stops counting past MAX_TABLES."""
    ways = [1] + [0] * total
    for size in sizes:
        prefix = [0] * (total + 2)
        for s in range(total + 1):
            prefix[s + 1] = prefix[s] + ways[s]
        ways = [
            min(prefix[s + 1] - prefix[max(0, s - size)], MAX_TABLES + 1) for s in range(total + 1)
        ]
    return ways[total]


def _seed(sites: Sequence[Site]) -> int:
    """The permutation test's seed: the first 8 bytes of the SHA-256 of the counts' canonical
    JSON, so the same counts always draw the same resamples and anyone can redraw them."""
    body = json.dumps([list(site) for site in sites], separators=(",", ":"))
    return int.from_bytes(hashlib.sha256(body.encode()).digest()[:8], "big")


def _exact_p(sizes: Sequence[int], total: int, observed: float) -> float:
    """The exact test (Freeman and Halton 1951; Fisher's for two sites): the probability, under
    the hypergeometric distribution with these margins, of every table no more likely than the
    observed one. A table's probability is prod C(n_i, k_i) / C(N, total)."""
    logs = [[_log_choose(size, k) for k in range(size + 1)] for size in sizes]
    log_all = _log_choose(sum(sizes), total)
    bound = observed + math.log1p(TIE)
    # The largest number of hits the sites after i can take, so no branch is a dead end.
    room = [0] * (len(sizes) + 1)
    for i in range(len(sizes) - 1, -1, -1):
        room[i] = room[i + 1] + sizes[i]
    terms: list[float] = []

    def walk(i: int, left: int, partial: float) -> None:
        if i == len(sizes) - 1:
            value = partial + logs[i][left]
            if value <= bound:
                terms.append(math.exp(value - log_all))
            return
        row = logs[i]
        for k in range(max(0, left - room[i + 1]), min(sizes[i], left) + 1):
            walk(i + 1, left - k, partial + row[k])

    walk(0, total, 0.0)
    return min(1.0, math.fsum(terms))


def _permutation_p(
    sizes: Sequence[int], total: int, observed: float, seed: int, resamples: int
) -> float:
    """The exact test's p-value estimated from seeded resamples: each resample deals the
    smaller column's cases among all N at random (Floyd's sampling, from random.Random(seed)
    .random() only), counts them per site, and is as extreme as the observed table when its
    probability is no larger. p = (1 + as extreme) / (1 + resamples)."""
    n_total = sum(sizes)
    smaller = min(total, n_total - total)
    flip = smaller != total  # dealt the misses: a site's hits are its size minus them
    edges = []
    running = 0
    for size in sizes:
        running += size
        edges.append(running)
    logs = [[_log_choose(size, k) for k in range(size + 1)] for size in sizes]
    bound = observed + math.log1p(TIE)
    draw = random.Random(seed).random
    extreme = 0
    for _ in range(resamples):
        chosen: set[int] = set()
        for j in range(n_total - smaller, n_total):  # Floyd (1987): `smaller` distinct cases
            t = int(draw() * (j + 1))
            chosen.add(j if t in chosen else t)
        dealt = [0] * len(sizes)
        for case in chosen:
            dealt[bisect.bisect_right(edges, case)] += 1
        value = math.fsum(
            logs[i][(sizes[i] - dealt[i]) if flip else dealt[i]] for i in range(len(sizes))
        )
        if value <= bound:
            extreme += 1
    return (1 + extreme) / (1 + resamples)


def homogeneity(
    sites: Sequence[Site], alpha: float = ALPHA, permutations: int = PERMUTATIONS
) -> dict[str, Any]:
    """Do the sites' rates differ by more than chance? The sites by (hit, miss) table, its
    expected counts n_i * column / N.

    - Every expected count 5 or more: Pearson's X^2 = sum (O - E)^2 / E on K - 1 degrees of
      freedom, its p-value `stats.chi2_sf`.
    - Otherwise chi-square is never used: the exact test when there are at most MAX_TABLES
      tables with the same margins, else the seeded permutation test, its seed in the answer.

    `differ` is p < alpha. A column with no cases in it (every site all hits, or all misses)
    has one table only: p is 1.
    """
    checked = _sites(sites)
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    if isinstance(permutations, bool) or not isinstance(permutations, int) or permutations < 1:
        raise ValueError("permutations must be a positive integer")
    if len(checked) < MIN_SITES:
        return _insufficient("homogeneity", len(checked), MIN_SITES)
    sizes = [n for _, n in checked]
    hits = [k for k, _ in checked]
    n_total = sum(sizes)
    total = sum(hits)
    columns = (total, n_total - total)
    expected = [size * column / n_total for size in sizes for column in columns]
    smallest = min(expected)
    answer: dict[str, Any] = {
        "sites": len(checked),
        "status": "ok",
        "alpha": alpha,
        "smallest_expected": _round(smallest),
    }
    if smallest >= MIN_EXPECTED:
        statistic = math.fsum(
            (observed - size * column / n_total) ** 2 / (size * column / n_total)
            for size, k in zip(sizes, hits, strict=True)
            for observed, column in ((k, columns[0]), (size - k, columns[1]))
        )
        df = len(checked) - 1
        p = stats.chi2_sf(statistic, df)
        answer.update(method=CHI_SQUARE, statistic=_round(statistic), df=df)
    else:
        observed = math.fsum(_log_choose(size, k) for size, k in zip(sizes, hits, strict=True))
        tables = _table_count(sizes, total)
        if tables <= MAX_TABLES:
            p = _exact_p(sizes, total, observed)
            answer.update(method=EXACT, tables=tables)
        else:
            seed = _seed(checked)
            p = _permutation_p(sizes, total, observed, seed, permutations)
            answer.update(method=PERMUTATION, seed=seed, resamples=permutations)
    answer["p"] = _round(p)
    answer["differ"] = answer["p"] < alpha
    return answer
