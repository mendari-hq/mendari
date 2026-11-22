# SPDX-License-Identifier: Apache-2.0
"""Exact intervals, paired tests and seeded bootstrap intervals, in pure Python (LP-10).

Standard library only (math, random, statistics): scipy, numpy and statsmodels are development
dependencies, used by tests/test_stats.py to check every function here (exact methods to 1e-12).
Every function is deterministic; the random ones take a seed, written into what they produce.

- `clopper_pearson(k, n)`: the exact interval for a proportion, from the beta quantile; never a
  normal approximation (`proportion` keeps k, n and the interval together).
- `mcnemar_exact(b, c)`: the two-sided exact test on a paired comparison's discordant cases.
- `bootstrap` and `paired_bootstrap`: percentile intervals over seeded resamples of cases;
  `bootstrap_defined` for a statistic a resample can leave undefined, counting those it drops.
- `holm`: Holm-Bonferroni over a named family of comparisons.
- `equal_mass_ece`: calibration error over equal-count bins, "not estimable" when they are small.
- `gamma_q` and `chi2_sf`: the regularized upper incomplete gamma function and the chi-square
  tail it gives, for the homogeneity test of pooled site evidence (LP-22, `evals.pooling`).

For the scoreboard (LP-17), used by no format yet:

- `stratified_bootstrap`: the percentile interval over seeded resamples within each stratum.
- `weighted_proportion`: a share of weighted cases, with Kish's effective n and the
  Korn-Graubard interval (`WeightedProportion`).
- `brier_reliability`: Murphy's reliability term of the Brier score, over equal-mass bins.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Hashable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from statistics import NormalDist
from typing import Any, TypeVar

T = TypeVar("T")

LEVEL = 0.95
RESAMPLES = 2000
DIGITS = 6  # stored bounds: the last bits of log/exp differ between platforms, these do not

# --------------------------------------------------------------------------------------------
# Regularized incomplete beta I_x(a, b) and its inverse
# --------------------------------------------------------------------------------------------

_EPS = 2.0**-52  # continued fraction stops when a factor is within one ulp of 1
_TINY = 1e-300  # Lentz: replaces an exact zero denominator
_MAX_TERMS = 100_000  # O(sqrt(max(a, b))) are needed; this bound is never reached for n < 1e9
_HALF_LOG_2PI = 0.5 * math.log(2.0 * math.pi)
# B_2k / (2k (2k-1)) for k = 1..7: the Stirling series for log Gamma (A&S 6.1.41)
_STIRLING_SERIES = (
    1.0 / 12.0,
    -1.0 / 360.0,
    1.0 / 1260.0,
    -1.0 / 1680.0,
    1.0 / 1188.0,
    -691.0 / 360360.0,
    1.0 / 156.0,
)


def _stirling_correction(z: float) -> float:
    """mu(z) = lgamma(z) - [(z - 1/2) log z - z + log(2 pi)/2], the Stirling remainder.

    For z >= 10 the asymptotic series (Abramowitz & Stegun 6.1.41; Bernoulli numbers
    B_2..B_14) truncated after z^-13: the first omitted term, 3617/(122400 z^15), is under
    3e-17 at z = 10. Below 10 it is lgamma minus the Stirling main term, both under 25 in
    magnitude, so the absolute error stays near 1e-15.
    """
    if z >= 10.0:
        r = 1.0 / z
        r2 = r * r
        series = 0.0
        for coefficient in reversed(_STIRLING_SERIES):  # Horner in 1/z^2
            series = series * r2 + coefficient
        return series * r
    return math.lgamma(z) - ((z - 0.5) * math.log(z) - z + _HALF_LOG_2PI)


def _rlog1(t: float) -> float:
    """t - log(1 + t), for t > -1 (DiDonato & Morris 1992, rlog1)."""
    return t - math.log1p(t)


def _log_front(a: float, b: float, x: float, y: float) -> float:
    """log( x^a y^b / B(a, b) ), y = 1 - x, without cancellation (DiDonato & Morris 1992,
    Algorithm 708, brcomp). With Stirling for each Gamma, x0 = a/(a+b), y0 = b/(a+b):

        x^a y^b / B(a,b) = sqrt(a b / (2 pi (a+b))) * exp(-(a u + b v) + mu(a+b) - mu(a) - mu(b))

    where lam = a - (a+b) x = (a+b) y - b, u = rlog1(-lam/a), v = rlog1(lam/b). The linear
    terms of a log(x/x0) and b log(y/y0) cancel analytically and are never formed, so the
    rounding of (a+b) x costs about eps * |lam| in the exponent instead of eps * (a+b).
    """
    s = a + b
    lam = a - s * x if x <= y else s * y - b
    return (
        0.5 * math.log(a * b / s)
        - _HALF_LOG_2PI
        - (a * _rlog1(-lam / a) + b * _rlog1(lam / b))
        + _stirling_correction(s)
        - _stirling_correction(a)
        - _stirling_correction(b)
    )


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for I_x(a,b) by the modified Lentz method (Numerical Recipes 6.4,
    betacf; Lentz 1976; Thompson & Barnett 1986). Converges fast for x < (a+1)/(a+b+2)."""
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _TINY:
        d = _TINY
    d = 1.0 / d
    h = d
    for m in range(1, _MAX_TERMS + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < _TINY:
            d = _TINY
        c = 1.0 + aa / c
        if abs(c) < _TINY:
            c = _TINY
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < _TINY:
            d = _TINY
        c = 1.0 + aa / c
        if abs(c) < _TINY:
            c = _TINY
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) <= _EPS:
            return h
    raise ArithmeticError(f"incomplete beta did not converge (a={a}, b={b}, x={x})")


def _ibeta(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b) for a, b > 0 and 0 <= x <= 1."""
    if not (a > 0.0 and b > 0.0):
        raise ValueError("a and b must be positive")
    if not 0.0 <= x <= 1.0:
        raise ValueError("x must be in [0, 1]")
    if x == 0.0:
        return 0.0
    if x == 1.0:
        return 1.0
    y = 1.0 - x
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(_log_front(a, b, x, y)) * _betacf(a, b, x) / a
    return 1.0 - math.exp(_log_front(b, a, y, x)) * _betacf(b, a, y) / b


def _initial_guess(a: float, b: float, p: float) -> float:
    """Abramowitz & Stegun 26.5.22 (a, b >= 1), the normal quantile from the standard library
    (statistics.NormalDist.inv_cdf, Wichura's AS 241)."""
    z = -NormalDist().inv_cdf(p)  # A&S y_p: Q(y_p) = p, positive for p < 1/2
    lam = (z * z - 3.0) / 6.0
    h = 2.0 / (1.0 / (2.0 * a - 1.0) + 1.0 / (2.0 * b - 1.0))
    w = z * math.sqrt(h + lam) / h - (1.0 / (2.0 * b - 1.0) - 1.0 / (2.0 * a - 1.0)) * (
        lam + 5.0 / 6.0 - 2.0 / (3.0 * h)
    )
    return a / (a + b * math.exp(2.0 * w))  # I_x(a, b) ~ p


def _beta_quantile_lower(a: float, b: float, p: float) -> float:
    """x with I_x(a, b) = p, for 0 < p <= 1/2. Halley's method from the A&S 26.5.22 guess,
    kept inside a bracket [lo, hi] that every evaluation shrinks; a step that is not finite or
    leaves the bracket is replaced by bisection (the safeguard of Numerical Recipes 9.4,
    rtsafe). Ends when a step is at most four ulps or the bracket is two adjacent doubles; if
    I has reached the floor of its precision first (far tails, levels of 0.999999 and over), the
    iterate nearest p."""
    if b == 1.0:  # I_x(a, 1) = x^a: closed form
        return math.exp(math.log(p) / a)
    if a == 1.0:  # I_x(1, b) = 1 - (1-x)^b
        return -math.expm1(math.log1p(-p) / b)
    lo, hi = 0.0, 1.0
    x = _initial_guess(a, b, p)
    if not 0.0 < x < 1.0:
        x = a / (a + b)
    best_x, best_f = x, math.inf
    for _ in range(100):
        f = _ibeta(a, b, x) - p
        if f == 0.0:
            return x
        if abs(f) < best_f:
            best_x, best_f = x, abs(f)
        if f < 0.0:
            lo = x
        else:
            hi = x
        y = 1.0 - x
        dens = math.exp(_log_front(a, b, x, y)) / (x * y)  # x^(a-1) y^(b-1) / B(a, b)
        new = math.nan
        if dens > 0.0:
            u = f / dens
            # Halley: f''/f' = (a-1)/x - (b-1)/y, its correction capped at a factor of 2.
            # Far in a tail dens underflows and u * f''/f' overflows: then new stays NaN.
            g = u * ((a - 1.0) / x - (b - 1.0) / y)
            if math.isfinite(g):
                new = x - u / (1.0 - 0.5 * min(1.0, g))
                if abs(new - x) <= 4.0 * math.ulp(x):
                    return new
        if not lo < new < hi:  # not finite, or outside the bracket: bisect
            new = lo + (hi - lo) / 2.0
            if not lo < new < hi:  # lo and hi are adjacent doubles
                return hi
        x = new
    return best_x


def beta_ppf(a: float, b: float, q: float) -> float:
    """Quantile of Beta(a, b): x with I_x(a, b) = q. Solves in the lower tail of whichever of
    Beta(a,b), Beta(b,a) puts q below one half, so I is evaluated where it is small and its
    relative accuracy carries over to x (1 - x' is exact to half an ulp of the result). For
    a, b >= 1 (the starting guess's domain, and all Clopper-Pearson needs)."""
    if not 0.0 < q < 1.0:
        raise ValueError("q must be in (0, 1)")
    if not (a >= 1.0 and b >= 1.0):
        raise ValueError("a and b must be at least 1")
    if q <= 0.5:
        return _beta_quantile_lower(a, b, q)
    return 1.0 - _beta_quantile_lower(b, a, 1.0 - q)


def clopper_pearson(k: int, n: int, level: float = 0.95) -> tuple[float, float]:
    """Exact (Clopper & Pearson 1934) two-sided interval for a binomial proportion k of n.

    lower = Beta(k, n-k+1) quantile at alpha/2 (0 when k = 0);
    upper = Beta(k+1, n-k) quantile at 1-alpha/2 (1 when k = n).
    """
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (k, n)):
        raise TypeError("k and n must be integers")
    if n < 1 or not 0 <= k <= n:
        raise ValueError("need n >= 1 and 0 <= k <= n")
    if not 0.0 < level < 1.0:
        raise ValueError("level must be in (0, 1)")
    tail = (1.0 - level) / 2.0
    lower = 0.0 if k == 0 else beta_ppf(k, n - k + 1, tail)
    upper = 1.0 if k == n else beta_ppf(k + 1, n - k, 1.0 - tail)
    return lower, upper


# --------------------------------------------------------------------------------------------
# Regularized incomplete gamma and the chi-square tail (LP-22)
# --------------------------------------------------------------------------------------------


def _log_gamma_front(a: float, x: float) -> float:
    """log( x^a e^-x / Gamma(a) ), the factor both expansions of the incomplete gamma share."""
    return a * math.log(x) - x - math.lgamma(a)


def gamma_q(a: float, x: float) -> float:
    """The regularized upper incomplete gamma function Q(a, x) = Gamma(a, x) / Gamma(a), for
    a > 0 and x >= 0 (scipy.special.gammaincc).

    Below x = a + 1 the series for P(a, x) (Numerical Recipes 6.2, gser), and Q = 1 - P; from
    there the continued fraction for Q by the modified Lentz method (gcf), which keeps a small
    Q's relative accuracy. Each stops when a term is within one ulp of the sum.
    """
    if not (a > 0.0 and math.isfinite(a)):
        raise ValueError("a must be positive and finite")
    if not (x >= 0.0 and math.isfinite(x)):
        raise ValueError("x must be non-negative and finite")
    if x == 0.0:
        return 1.0
    if x < a + 1.0:
        term = total = 1.0 / a
        ap = a
        for _ in range(_MAX_TERMS):
            ap += 1.0
            term *= x / ap
            total += term
            if abs(term) <= abs(total) * _EPS:
                return 1.0 - total * math.exp(_log_gamma_front(a, x))
        raise ArithmeticError(f"incomplete gamma series did not converge (a={a}, x={x})")
    b = x + 1.0 - a
    c = 1.0 / _TINY
    d = 1.0 / b
    h = d
    for i in range(1, _MAX_TERMS + 1):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < _TINY:
            d = _TINY
        c = b + an / c
        if abs(c) < _TINY:
            c = _TINY
        d = 1.0 / d
        step = d * c
        h *= step
        if abs(step - 1.0) <= _EPS:
            return math.exp(_log_gamma_front(a, x)) * h
    raise ArithmeticError(f"incomplete gamma fraction did not converge (a={a}, x={x})")


def chi2_sf(statistic: float, df: int) -> float:
    """P(X >= statistic) for X ~ chi-square with df degrees of freedom: Q(df/2, statistic/2)
    (scipy.stats.chi2.sf)."""
    if isinstance(df, bool) or not isinstance(df, int) or df < 1:
        raise ValueError("df must be a positive integer")
    if not (statistic >= 0.0 and math.isfinite(statistic)):
        raise ValueError("the statistic must be non-negative and finite")
    return gamma_q(df / 2.0, statistic / 2.0)


# --------------------------------------------------------------------------------------------
# Exact McNemar
# --------------------------------------------------------------------------------------------


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value on the discordant pairs (b: only A right, c: only B
    right): p = min(1, 2 P(X <= min(b, c))), X ~ Bin(b + c, 1/2). Exact integer arithmetic;
    the one rounding is the final int/int division, which CPython rounds correctly. b + c = 0
    (no discordant pair) gives 1.0."""
    for v in (b, c):
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError("b and c must be non-negative integers")
    n = b + c
    m = min(b, c)
    term = 1  # C(n, 0)
    total = 1
    for j in range(m):
        term = term * (n - j) // (j + 1)  # C(n, j+1), exact
        total += term
    numerator = 2 * total
    denominator = 1 << n
    if numerator >= denominator:
        return 1.0
    return numerator / denominator


# --------------------------------------------------------------------------------------------
# Holm
# --------------------------------------------------------------------------------------------


def holm(p_values: Sequence[float], alpha: float = 0.05) -> tuple[list[bool], list[float]]:
    """Holm (1979) step-down: sorted ascending p_(1..m), reject while p_(i) <= alpha/(m-i+1).
    Adjusted p_(i) = min(1, max_{j<=i} (m-j+1) p_(j)). Returned in input order."""
    m = len(p_values)
    for p in p_values:
        if not (isinstance(p, (int, float)) and 0.0 <= p <= 1.0):
            raise ValueError("p-values must be numbers in [0, 1]")
    order = sorted(range(m), key=lambda i: p_values[i])  # stable: ties keep input order
    reject = [False] * m
    adjusted = [0.0] * m
    running = 0.0
    stepping = True
    for rank, i in enumerate(order):
        p = float(p_values[i])
        factor = m - rank
        if stepping and p > alpha / factor:
            stepping = False
        reject[i] = stepping
        running = max(running, p * factor)
        adjusted[i] = min(1.0, running)
    return reject, adjusted


# --------------------------------------------------------------------------------------------
# Seeded bootstrap
# --------------------------------------------------------------------------------------------


def resample_indices(n: int, resamples: int, seed: int) -> Iterator[list[int]]:
    """The bootstrap's index matrix, one row (resample) at a time: row r, column i is
    floor(U * n) for the (r*n + i)-th draw U of random.Random(seed).random(). Only random()
    is used: it is the one part of the module whose sequence Python guarantees across versions
    for a given seed (docs, random: "Notes on Reproducibility")."""
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("seed must be an integer")
    draw = random.Random(seed).random
    for _ in range(resamples):
        yield [int(draw() * n) for _ in range(n)]


def percentile(ordered: Sequence[float], q: float) -> float:
    """Hyndman & Fan (1996) definition 7: h = (N-1) q, linear between the order statistics
    either side; numpy.percentile(method='linear') and scipy.stats.quantile's default. The
    lerp is written as numpy writes it (from the nearer end) so the two agree to the ulp."""
    size = len(ordered)
    h = (size - 1) * q
    lo = math.floor(h)
    if lo >= size - 1:
        return float(ordered[-1])
    t = h - lo
    a, b = ordered[lo], ordered[lo + 1]
    diff = b - a
    return b - diff * (1.0 - t) if t >= 0.5 else a + diff * t


def mean(values: Sequence[float]) -> float:
    """Correctly rounded sum (fsum) divided once: independent of order and platform."""
    return math.fsum(values) / len(values)


def _tail(resamples: int, level: float) -> float:
    """A percentile interval's tail, (1 - level) / 2, once its resamples are a positive whole
    number and its level is in (0, 1): with no resample there is no percentile (an IndexError
    deep in `percentile`), and a level outside (0, 1) gives bounds that are not an interval."""
    if isinstance(resamples, bool) or not isinstance(resamples, int) or resamples < 1:
        raise ValueError("resamples must be a positive integer")
    if not 0.0 < level < 1.0:
        raise ValueError("level must be in (0, 1)")
    return (1.0 - level) / 2.0


def bootstrap(
    values: Sequence[float],
    statistic: Callable[[Sequence[float]], float] = mean,
    resamples: int = 2000,
    seed: int = 0,
    level: float = 0.95,
) -> tuple[float, float, float]:
    """(estimate, low, high): percentile interval of statistic over seeded resamples."""
    n = len(values)
    if n == 0:
        raise ValueError("no values")
    tail = _tail(resamples, level)
    estimate = statistic(values)
    stats = sorted(
        statistic([values[i] for i in row]) for row in resample_indices(n, resamples, seed)
    )
    return estimate, percentile(stats, tail), percentile(stats, 1.0 - tail)


def bootstrap_defined(
    records: Sequence[T],
    statistic: Callable[[Sequence[T]], float | None],
    resamples: int = 2000,
    seed: int = 0,
    level: float = 0.95,
) -> tuple[float | None, float | None, float | None, int]:
    """(estimate, low, high, dropped): `bootstrap` for a statistic that a sample can leave
    undefined (None, as a kappa when both raters used one label throughout). The estimate is
    the statistic on every record; the percentile interval is over the resamples where it is
    defined, and `dropped` counts the others, always reported so a reader can say the interval
    is not estimable. With no resample defined, the bounds are None."""
    n = len(records)
    if n == 0:
        raise ValueError("no records")
    tail = _tail(resamples, level)
    estimate = statistic(records)
    values: list[float] = []
    dropped = 0
    for row in resample_indices(n, resamples, seed):
        value = statistic([records[i] for i in row])
        if value is None:
            dropped += 1
        else:
            values.append(value)
    if not values:
        return estimate, None, None, dropped
    values.sort()
    return estimate, percentile(values, tail), percentile(values, 1.0 - tail), dropped


def paired_bootstrap(
    a: Sequence[float],
    b: Sequence[float],
    statistic: Callable[[Sequence[float]], float] = mean,
    resamples: int = 2000,
    seed: int = 0,
    level: float = 0.95,
) -> tuple[float, float, float, float]:
    """(estimate, low, high, p_value) of statistic(a) - statistic(b), resampling cases: one
    index row per resample, applied to both arms. p_value is the two-sided bootstrap p-value
    for "no difference", with the +1 correction that keeps it above zero (Davison & Hinkley
    1997, eq. 4.11): min(1, 2 min((1 + #{d* <= 0}) / (R + 1), (1 + #{d* >= 0}) / (R + 1)));
    it is what a Holm family of mixed McNemar and bootstrap comparisons adjusts."""
    n = len(a)
    if n == 0 or len(b) != n:
        raise ValueError("a and b must be the same non-zero length (paired cases)")
    tail = _tail(resamples, level)
    estimate = statistic(a) - statistic(b)
    diffs = sorted(
        statistic([a[i] for i in row]) - statistic([b[i] for i in row])
        for row in resample_indices(n, resamples, seed)
    )
    at_or_below = sum(1 for d in diffs if d <= 0.0)
    at_or_above = sum(1 for d in diffs if d >= 0.0)
    p_value = min(1.0, 2 * min(1 + at_or_below, 1 + at_or_above) / (resamples + 1))
    return estimate, percentile(diffs, tail), percentile(diffs, 1.0 - tail), p_value


def stratified_resample_indices(
    strata: Sequence[Hashable], resamples: int, seed: int
) -> Iterator[list[int]]:
    """The stratified bootstrap's index matrix, one row (resample) at a time: row r, column i
    is pool[floor(U * m)], where pool holds the m cases of case i's stratum in input order and U
    is the (r*n + i)-th draw of random.Random(seed).random(), as in `resample_indices`. Each
    column keeps its case's stratum, so every stratum keeps its size and its places; with one
    stratum the rows are `resample_indices`'s. Strata are told apart by equality."""
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("seed must be an integer")
    members: dict[Hashable, list[int]] = {}
    for i, stratum in enumerate(strata):
        members.setdefault(stratum, []).append(i)
    pools = [members[stratum] for stratum in strata]
    draw = random.Random(seed).random
    for _ in range(resamples):
        yield [pool[int(draw() * len(pool))] for pool in pools]


def stratified_bootstrap(
    records: Sequence[T],
    strata: Sequence[Hashable],
    statistic: Callable[[Sequence[T]], float],
    resamples: int = 2000,
    seed: int = 0,
    level: float = 0.95,
) -> tuple[float, float, float]:
    """(estimate, low, high): `bootstrap` with each case resampled within its stratum
    (`stratified_resample_indices`; strata[i] is records[i]'s). A statistic that needs every
    stratum, as macro-F1 needs each true label's support, stays defined on every resample. With
    one stratum it is `bootstrap`."""
    n = len(records)
    if n == 0:
        raise ValueError("no records")
    if len(strata) != n:
        raise ValueError("records and strata differ in length")
    tail = _tail(resamples, level)
    estimate = statistic(records)
    values = sorted(
        statistic([records[i] for i in row])
        for row in stratified_resample_indices(strata, resamples, seed)
    )
    return estimate, percentile(values, tail), percentile(values, 1.0 - tail)


# --------------------------------------------------------------------------------------------
# Equal-mass ECE
# --------------------------------------------------------------------------------------------


def equal_mass_ece(
    confidences: Sequence[float], correct: Sequence[bool], bins: int = 10, min_bin_size: int = 30
) -> dict[str, object]:
    """Expected calibration error over equal-mass (equal-count) confidence bins.

    Cases are sorted by confidence and cut into `bins` consecutive groups whose sizes differ by
    at most one (the first n mod bins groups take one more: numpy.array_split). Tied
    confidences are exchangeable, so each case's correctness is replaced by the mean
    correctness of all cases sharing its confidence: the result is the same for any order of
    the input and needs no tie-break key. Not estimable when the smallest bin, n // bins, is
    under min_bin_size. Sums are math.fsum, so the value is independent of order and platform.
    """
    n = len(confidences)
    if len(correct) != n:
        raise ValueError("confidences and correct differ in length")
    if bins < 1 or min_bin_size < 1:
        raise ValueError("bins and min_bin_size must be at least 1")
    # A NaN never equals itself: its tie group would be empty (a division by zero), and sorted
    # leaves the cases around it in no order at all.
    if not all(math.isfinite(c) for c in confidences):
        raise ValueError("confidences must be finite")
    base = {"method": "equal-mass", "bins_requested": bins, "min_bin_size": min_bin_size, "n": n}
    if n // bins < min_bin_size:
        return {
            **base,
            "ece": None,
            "status": "not_estimable",
            "reason": f"{n} scored cases; {bins} equal-mass bins of at least {min_bin_size} "
            f"need {bins * min_bin_size}",
            "bins": [],
        }
    pairs = sorted(zip(confidences, correct, strict=True), key=lambda p: p[0])
    conf = [float(c) for c, _ in pairs]
    smoothed: list[float] = []
    i = 0
    while i < n:  # replace correctness by its tie-group mean
        j = i
        while j < n and conf[j] == conf[i]:
            j += 1
        group_mean = sum(1 for _, ok in pairs[i:j] if ok) / (j - i)
        smoothed.extend([group_mean] * (j - i))
        i = j
    size, extra = divmod(n, bins)
    table = []
    terms = []
    start = 0
    for index in range(bins):
        stop = start + size + (1 if index < extra else 0)
        members = range(start, stop)
        m = stop - start
        mean_conf = math.fsum(conf[k] for k in members) / m
        accuracy = math.fsum(smoothed[k] for k in members) / m
        terms.append(m * abs(accuracy - mean_conf))
        table.append(
            {
                "low": conf[start],
                "high": conf[stop - 1],
                "n": m,
                "confidence": mean_conf,
                "accuracy": accuracy,
            }
        )
        start = stop
    return {**base, "ece": math.fsum(terms) / n, "status": "ok", "bins": table}


def brier_reliability(
    distributions: Sequence[Mapping[str, float]],
    truths: Sequence[str],
    labels: Sequence[str],
    bins: int = 10,
    min_bin_size: int = 30,
) -> dict[str, object]:
    """Murphy's (1973) reliability term of the multi-class Brier score (`metrics.brier`'s scale,
    0 best), over equal-mass bins of top-label confidence.

    A case's forecast is its distribution over `labels`, its outcome the one-hot vector of its
    truth, and its top-label confidence its largest probability. Cases are cut into bins by that
    confidence as `equal_mass_ece` cuts them, and tied confidences are exchangeable here too:
    each case's forecast and outcome are replaced by their means over every case sharing its
    confidence, so the result is the same for any order of the input. The term is the sum over
    bins of (bin size / n) times the squared distance between the bin's mean forecast and its
    outcome frequencies. Not estimable, as `equal_mass_ece` is, when the smallest bin, n // bins,
    is under min_bin_size. Sums are math.fsum, so the value is independent of order and platform.
    """
    n = len(distributions)
    if len(truths) != n:
        raise ValueError("distributions and truths differ in length")
    names = list(labels)
    if not names or len(set(names)) != len(names):
        raise ValueError("labels must be one or more distinct names")
    if bins < 1 or min_bin_size < 1:
        raise ValueError("bins and min_bin_size must be at least 1")
    rows: list[tuple[float, list[float], list[float]]] = []
    for distribution, truth in zip(distributions, truths, strict=True):
        if set(distribution) != set(names):
            raise ValueError("each distribution must give every label's probability, and no other")
        if truth not in distribution:
            raise ValueError("each truth must be one of the labels")
        forecast = [float(distribution[label]) for label in names]
        if not all(math.isfinite(p) for p in forecast):
            raise ValueError("probabilities must be finite")
        rows.append((max(forecast), forecast, [float(label == truth) for label in names]))
    base = {"method": "equal-mass", "bins_requested": bins, "min_bin_size": min_bin_size, "n": n}
    if n // bins < min_bin_size:
        return {
            **base,
            "reliability": None,
            "status": "not_estimable",
            "reason": f"{n} scored cases; {bins} equal-mass bins of at least {min_bin_size} "
            f"need {bins * min_bin_size}",
        }
    rows.sort(key=lambda row: row[0])
    width = len(names)
    smoothed: list[tuple[list[float], list[float]]] = []  # each case's tie-group means
    i = 0
    while i < n:
        j = i
        while j < n and rows[j][0] == rows[i][0]:
            j += 1
        group = rows[i:j]
        forecast = [math.fsum(row[1][c] for row in group) / (j - i) for c in range(width)]
        outcome = [math.fsum(row[2][c] for row in group) / (j - i) for c in range(width)]
        smoothed.extend([(forecast, outcome)] * (j - i))
        i = j
    size, extra = divmod(n, bins)
    terms = []
    start = 0
    for index in range(bins):
        stop = start + size + (1 if index < extra else 0)
        members = smoothed[start:stop]
        m = stop - start
        gaps = [
            math.fsum(f[c] for f, _ in members) / m - math.fsum(o[c] for _, o in members) / m
            for c in range(width)
        ]
        terms.append(m * math.fsum(gap * gap for gap in gaps))
        start = stop
    return {**base, "reliability": math.fsum(terms) / n, "status": "ok"}


@dataclass(frozen=True)
class Proportion:
    """k of n with its exact Clopper-Pearson interval (None when n is 0: no cases)."""

    k: int
    n: int
    low: float | None
    high: float | None
    level: float = LEVEL

    @property
    def rate(self) -> float | None:
        return self.k / self.n if self.n else None

    def to_dict(self) -> dict[str, Any]:
        return {"k": self.k, "n": self.n, "low": _stored(self.low), "high": _stored(self.high)}


def _stored(value: float | None) -> float | None:
    return None if value is None else round(value, DIGITS)


@lru_cache(maxsize=65536)
def interval(k: int, n: int) -> tuple[float, float]:
    """The exact 95% interval of k of n (n >= 1), unrounded: what every displayed figure is
    printed from, never a stored (6-place) bound, so a figure reads the same wherever it is
    shown. Memoised: a page shows many figures of the same few sizes."""
    return clopper_pearson(k, n, LEVEL)


def proportion(k: int, n: int, level: float = LEVEL) -> Proportion:
    if n == 0:
        return Proportion(k, n, None, None, level)
    low, high = clopper_pearson(k, n, level)
    return Proportion(k, n, low, high, level)


@dataclass(frozen=True)
class WeightedProportion:
    """A share of weighted cases (`weighted_proportion`): the Hájek estimate, Kish's effective n
    and the Korn-Graubard interval, all None when there are no cases. It is shown beside the
    unweighted k of n, never in its place."""

    n: int
    estimate: float | None
    effective_n: float | None
    low: float | None
    high: float | None
    level: float = LEVEL

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "estimate": _stored(self.estimate),
            "effective_n": _stored(self.effective_n),
            "low": _stored(self.low),
            "high": _stored(self.high),
        }


def weighted_proportion(
    hits: Sequence[bool], weights: Sequence[float], level: float = LEVEL
) -> WeightedProportion:
    """The share of hits among cases drawn with unequal probabilities, each case weighted (a
    hidden set's sampling weight, N_h / n_h).

    - estimate: Hájek's ratio, sum(w * hit) / sum(w);
    - effective_n: Kish's, sum(w)^2 / sum(w^2);
    - low, high: Korn & Graubard's (1998) interval, Clopper-Pearson at the effective counts,
      x = estimate * effective_n hits and effective_n - x misses, with Kish's n as the effective
      sample size (no degrees-of-freedom factor): low is the Beta(x, misses + 1) quantile at
      alpha/2 and high the Beta(x + 1, misses) quantile at 1 - alpha/2, at the real counts.
      `beta_ppf` takes shapes of 1 or more only, so where a count is under 1 its bound is
      clamped: low is 0 when x < 1, and high is 1 when misses < 1. A clamped bound is wider than
      Korn-Graubard's, never narrower, and every other bound is Korn-Graubard's, at no hits and
      at all hits too.

    The sums are exact (fractions of the weights' binary values) and each figure and shape is
    rounded once, so unit weights, or one weight for every case, give clopper_pearson(k, n)
    exactly.
    """
    n = len(hits)
    if len(weights) != n:
        raise ValueError("hits and weights differ in length")
    if not 0.0 < level < 1.0:
        raise ValueError("level must be in (0, 1)")
    if not all(isinstance(hit, bool) for hit in hits):
        raise TypeError("each hit must be a bool")
    exact: list[Fraction] = []
    for weight in weights:
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise TypeError("each weight must be a number")
        if not (math.isfinite(weight) and weight > 0):
            raise ValueError("each weight must be finite and positive")
        exact.append(Fraction(weight))
    if n == 0:
        return WeightedProportion(0, None, None, None, None, level)
    total = sum(exact, Fraction(0))
    hit_total = sum((w for w, hit in zip(exact, hits, strict=True) if hit), Fraction(0))
    squares = sum((w * w for w in exact), Fraction(0))
    effective_n = total * total / squares
    x = total * hit_total / squares  # estimate * effective_n, exactly
    misses = effective_n - x
    tail = (1.0 - level) / 2.0
    low = beta_ppf(float(x), float(misses + 1), tail) if x >= 1 else 0.0
    high = beta_ppf(float(x + 1), float(misses), 1.0 - tail) if misses >= 1 else 1.0
    return WeightedProportion(n, float(hit_total / total), float(effective_n), low, high, level)


def holm_family(p_values: Mapping[str, float], alpha: float = 0.05) -> dict[str, dict[str, Any]]:
    """Holm over a family named in the output: each comparison's p-value, adjusted p-value and
    whether it is rejected at `alpha`."""
    names = list(p_values)
    reject, adjusted = holm([p_values[name] for name in names], alpha)
    return {
        name: {"p": p_values[name], "adjusted": adjusted[i], "reject": reject[i]}
        for i, name in enumerate(names)
    }
