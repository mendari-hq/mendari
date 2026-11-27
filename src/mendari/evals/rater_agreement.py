# SPDX-License-Identifier: Apache-2.0
"""How far two raters who grade the same items blind agree, for written output's report
(`evals/written.py`, ADR-0025). Pure: no input text, no I/O.

- Cohen's kappa of two raters on the items both graded: (n a - s) / (n^2 - s), with a the items
  they agree on and s the sum over levels of the two raters' counts multiplied; exact (Fraction).
- With linear weights on the levels' declared scores (`weighted_kappa`, Cicchetti-Allison): on
  two levels it is the plain kappa.
- Its interval (`measured`): the percentile bootstrap over seeded resamples of the items
  (`stats.resample_indices`).

A coefficient the items leave undefined (both raters gave every item one and the same level) is
null, `not_estimable`, with its reason. An interval is `not_estimable` when any resample leaves
its coefficient undefined, and says in how many. Values are rounded to 6 places."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from fractions import Fraction
from typing import Any

from mendari.evals import stats


def kappa(pairs: Sequence[tuple[str, str]]) -> Fraction | None:
    """Cohen's kappa of two raters' labels on the same cases, exact; None when both gave every
    case one and the same label (the chance agreement is then 1)."""
    n = len(pairs)
    if n == 0:
        return None
    agree = sum(1 for first, second in pairs if first == second)
    firsts = Counter(first for first, _ in pairs)
    seconds = Counter(second for _, second in pairs)
    chance = sum(count * seconds[label] for label, count in firsts.items())
    if n * n == chance:
        return None
    return Fraction(n * agree - chance, n * n - chance)


def weighted_kappa(
    pairs: Sequence[tuple[str, str]], scores: Mapping[str, float]
) -> Fraction | None:
    """Cohen's kappa with linear weights on the levels' declared `scores` (Cicchetti-Allison), of
    two raters' levels on the same cases, exact:

        1 - n * sum_i |s(a_i) - s(b_i)|  /  sum_l sum_m |s(l) - s(m)| * r(l) * c(m)

    with r and c the first and the second rater's counts of each level. On two levels it is
    `kappa`. None for no pairs, or when the denominator is zero: both raters gave every case one
    and the same level. A level `scores` does not hold is a KeyError: the caller checks levels."""
    n = len(pairs)
    if n == 0:
        return None
    exact = {level: Fraction(score) for level, score in scores.items()}
    cells = Counter(pairs)
    observed = sum(
        (count * abs(exact[first] - exact[second]) for (first, second), count in cells.items()),
        Fraction(0),
    )
    firsts = Counter(first for first, _ in pairs)
    seconds = Counter(second for _, second in pairs)
    expected = sum(
        (
            abs(exact[first] - exact[second]) * rows * columns
            for first, rows in firsts.items()
            for second, columns in seconds.items()
        ),
        Fraction(0),
    )
    if expected == 0:
        return None
    return 1 - n * observed / expected


def measured(
    estimate: Fraction | None,
    statistic: Any,
    ratings: Sequence[Any],
    seed: int,
    resamples: int,
    reason: str,
) -> tuple[dict[str, Any], float | None]:
    """A coefficient as a report stores it: its estimate and its percentile-bootstrap interval
    over `ratings` (the cases), each to six places; `not_estimable` with its reason when the
    cases leave it undefined, and an interval `not_estimable` when any resample does. With it,
    its interval's lower bound as the bootstrap gave it, before rounding (None when the
    coefficient or its interval is not estimable): a floor is held against this, never against
    the stored six places, so a bound just under a floor never rounds up to it."""
    if estimate is None:
        return {"estimate": None, "status": "not_estimable", "reason": reason}, None
    _, low, high, dropped = stats.bootstrap_defined(ratings, statistic, resamples, seed)
    interval: dict[str, Any]
    if dropped or low is None or high is None:
        interval = {
            "status": "not_estimable",
            "reason": f"undefined in {dropped} of {resamples} resamples",
            "dropped": dropped,
        }
    else:
        interval = {
            "status": "ok",
            "low": round(low, stats.DIGITS),
            "high": round(high, stats.DIGITS),
            "dropped": 0,
        }
    stored = {
        "estimate": round(float(estimate), stats.DIGITS),
        "status": "ok",
        "interval": interval,
    }
    return stored, (low if interval["status"] == "ok" else None)
