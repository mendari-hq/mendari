# SPDX-License-Identifier: Apache-2.0
"""Exact intervals, paired tests and seeded bootstrap intervals, in pure Python (LP-10).

Standard library only (math, random, statistics): scipy, numpy and statsmodels are development
dependencies, used by tests/test_stats.py to check every function here (exact methods to 1e-12).
Every function is deterministic; the random ones take a seed, written into what they produce.

- `clopper_pearson(k, n)`: the exact interval for a proportion, from the beta quantile; never a
  normal approximation (`proportion` keeps k, n and the interval together).
- `mcnemar_exact(b, c)`: the two-sided exact test on a paired comparison's discordant cases.
- `bootstrap` and `paired_bootstrap`: percentile intervals over seeded resamples of cases.
- `holm`: Holm-Bonferroni over a named family of comparisons.
- `equal_mass_ece`: calibration error over equal-count bins, "not estimable" when they are small.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from statistics import NormalDist
from typing import Any

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
    rtsafe). Ends when a step is at most four ulps or the bracket is two adjacent doubles."""
    if b == 1.0:  # I_x(a, 1) = x^a: closed form
        return math.exp(math.log(p) / a)
    if a == 1.0:  # I_x(1, b) = 1 - (1-x)^b
        return -math.expm1(math.log1p(-p) / b)
    lo, hi = 0.0, 1.0
    x = _initial_guess(a, b, p)
    if not 0.0 < x < 1.0:
        x = a / (a + b)
    for _ in range(100):
        f = _ibeta(a, b, x) - p
        if f == 0.0:
            return x
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
    raise ArithmeticError(f"beta quantile did not converge (a={a}, b={b}, p={p})")


def beta_ppf(a: float, b: float, q: float) -> float:
    """Quantile of Beta(a, b): x with I_x(a, b) = q. Solves in the lower tail of whichever of
    Beta(a,b), Beta(b,a) puts q below one half, so I is evaluated where it is small and its
    relative accuracy carries over to x (1 - x' is exact to half an ulp of the result)."""
    if not 0.0 < q < 1.0:
        raise ValueError("q must be in (0, 1)")
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
    estimate = statistic(values)
    stats = sorted(
        statistic([values[i] for i in row]) for row in resample_indices(n, resamples, seed)
    )
    tail = (1.0 - level) / 2.0
    return estimate, percentile(stats, tail), percentile(stats, 1.0 - tail)


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
    estimate = statistic(a) - statistic(b)
    diffs = sorted(
        statistic([a[i] for i in row]) - statistic([b[i] for i in row])
        for row in resample_indices(n, resamples, seed)
    )
    tail = (1.0 - level) / 2.0
    at_or_below = sum(1 for d in diffs if d <= 0.0)
    at_or_above = sum(1 for d in diffs if d >= 0.0)
    p_value = min(1.0, 2 * min(1 + at_or_below, 1 + at_or_above) / (resamples + 1))
    return estimate, percentile(diffs, tail), percentile(diffs, 1.0 - tail), p_value


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


def proportion(k: int, n: int, level: float = LEVEL) -> Proportion:
    if n == 0:
        return Proportion(k, n, None, None, level)
    low, high = clopper_pearson(k, n, level)
    return Proportion(k, n, low, high, level)


def holm_family(p_values: Mapping[str, float], alpha: float = 0.05) -> dict[str, dict[str, Any]]:
    """Holm over a family named in the output: each comparison's p-value, adjusted p-value and
    whether it is rejected at `alpha`."""
    names = list(p_values)
    reject, adjusted = holm([p_values[name] for name in names], alpha)
    return {
        name: {"p": p_values[name], "adjusted": adjusted[i], "reject": reject[i]}
        for i, name in enumerate(names)
    }
