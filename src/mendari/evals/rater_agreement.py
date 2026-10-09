# SPDX-License-Identifier: Apache-2.0
"""How far raters who label the same cases blind agree (`mendari.agreement/1`, LP-11 requirement
4): per question, raw percent agreement, Cohen's kappa for each pair of raters, and Krippendorff's
alpha across the pool, since the pairs vary from case to case. Pure: no input text, no I/O.

`cases` holds one mapping per case, of rater id to that rater's first-pass labels
({question: label}). Raters label the pack's member questions only; a derived question's label for
a rater comes from the pack's decision table over that rater's member labels (`derive_answer`),
when the rater labelled every member. An adjudicator's label is not a first pass and is not here.

- Percent agreement: of the cases two or more raters labelled, those whose labels all agree: k of
  n with its exact interval (Clopper-Pearson), as every share in Mendari is shown.
- Cohen's kappa, for each pair of raters, on the cases both labelled: (n a - s) / (n^2 - s), with a
  the cases they agree on and s the sum over labels of the two raters' counts multiplied; exact
  (Fraction), its n and both raters' label counts beside it.
- Krippendorff's alpha (nominal), on the cases two or more raters labelled, missing ratings
  allowed: 1 - (m - 1) D / (m^2 - sum of m_c^2), with m the ratings in those cases, m_c those of
  label c, and D each case's ordered pairs of disagreeing ratings weighted 1 / (its ratings - 1);
  exact.
- Intervals for kappa and alpha: the percentile bootstrap over 2,000 seeded resamples of the
  question's cases (`stats.resample_indices`: the same index matrix for every coefficient of the
  question). Percent agreement keeps its exact interval.

A coefficient the cases leave undefined (kappa when both raters gave every case one and the same
label, alpha when no case has two ratings or every rating is one label) is null, `not_estimable`,
with its reason. An interval is `not_estimable` when any resample leaves its coefficient undefined,
and says in how many. Values are rounded to 6 places, and the seed and the number of resamples are
in the result, so a second run gives the same bytes."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from fractions import Fraction
from itertools import combinations
from math import lcm
from typing import Any

from mendari.core.derive import derive_answer
from mendari.core.spec import PackManifest
from mendari.errors import MendariError
from mendari.evals import stats
from mendari.providers.base import Answer, ProviderInfo

FORMAT = "mendari.agreement/1"
SEED = 0
RATER = ProviderInfo(slot="rater", variant="first_pass", type="rater")

Ratings = Mapping[str, str]  # one case, one question: rater id -> label


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


def alpha(units: Sequence[Sequence[str]]) -> Fraction | None:
    """Krippendorff's alpha, nominal, over units (one per case: its ratings, any number); a unit
    with fewer than two ratings counts for nothing. Exact; None when no unit has two ratings or
    every rating is one label."""
    pairable = [unit for unit in units if len(unit) >= 2]
    if not pairable:
        return None
    scale = lcm(*(len(unit) - 1 for unit in pairable))
    disagreeing = 0  # the ordered disagreeing pairs, each weighted scale / (ratings - 1)
    counts: Counter[str] = Counter()
    for unit in pairable:
        within = Counter(unit)
        counts.update(within)
        size = len(unit)
        disagreeing += scale // (size - 1) * (size * size - sum(c * c for c in within.values()))
    total = sum(counts.values())
    expected = total * total - sum(c * c for c in counts.values())
    if expected == 0:
        return None
    return 1 - Fraction((total - 1) * disagreeing, scale * expected)


def agreement(
    manifest: PackManifest,
    cases: Sequence[Mapping[str, Mapping[str, str]]],
    *,
    pack: str,
    seed: int = SEED,
    resamples: int = stats.RESAMPLES,
) -> dict[str, Any]:
    """The agreement of the raters of `cases` on each of the pack's questions."""
    labelled = _labelled(manifest, cases)
    questions: dict[str, Any] = {}
    for question in manifest.questions:
        ratings = [
            {rater: labels[question] for rater, labels in case.items() if question in labels}
            for case in labelled
        ]
        questions[question] = _question([rated for rated in ratings if rated], seed, resamples)
    return {
        "format": FORMAT,
        "pack": pack,
        "level": stats.LEVEL,
        "seeds": {"bootstrap": seed, "resamples": resamples},
        "questions": questions,
    }


def _labelled(
    manifest: PackManifest, cases: Sequence[Mapping[str, Mapping[str, str]]]
) -> list[dict[str, dict[str, str]]]:
    """Each case's ratings, checked, with each rater's derived labels added. A problem names the
    case's position and the rater, never a label or a text it was not given by the pack."""
    derived = manifest.derived
    out = []
    for position, case in enumerate(cases):
        rated: dict[str, dict[str, str]] = {}
        for rater, labels in case.items():
            where = f"case {position}, rater {rater}"
            for question, label in labels.items():
                if question not in manifest.questions:
                    raise MendariError(f"{where}: labels a question the pack does not have")
                if question in derived:
                    raise MendariError(
                        f"{where}: labels {question}, a derived question: raters label its members"
                    )
                if label not in manifest.labels(question):
                    raise MendariError(f"{where}: {question}'s label is not one of its labels")
            rated[rater] = with_derived(manifest, labels)
        out.append(rated)
    return out


def with_derived(manifest: PackManifest, labels: Mapping[str, str]) -> dict[str, str]:
    """`labels` (a rater's, or a case's settled labels, of the pack's member questions) with the
    label of each derived question whose members it all labels, from the pack's decision
    table (`derive_answer`)."""
    own = dict(labels)
    for key, table in manifest.derived.items():
        spec = table.derive
        if spec is not None and all(member in labels for member in spec.members):
            answers = {member: _certain(member, labels[member]) for member in spec.members}
            own[key] = derive_answer(key, table, answers).label
    return own


def _certain(question: str, label: str) -> Answer:
    return Answer(
        question=question,
        label=label,
        confidence=1.0,
        probabilities={label: 1.0},
        provider=RATER,
    )


def _question(ratings: list[Ratings], seed: int, resamples: int) -> dict[str, Any]:
    several = [rated for rated in ratings if len(rated) >= 2]
    agreeing = sum(1 for rated in several if len(set(rated.values())) == 1)
    raters = sorted({rater for rated in ratings for rater in rated})
    pairs = []
    for first, second in combinations(raters, 2):
        shared = [
            (rated[first], rated[second]) for rated in ratings if first in rated and second in rated
        ]
        if not shared:
            continue

        def pair_kappa(sample: Sequence[Ratings], a: str = first, b: str = second) -> float | None:
            value = kappa([(rated[a], rated[b]) for rated in sample if a in rated and b in rated])
            return None if value is None else float(value)

        pairs.append(
            {
                "raters": [first, second],
                "n": len(shared),
                "labels": {
                    first: dict(sorted(Counter(label for label, _ in shared).items())),
                    second: dict(sorted(Counter(label for _, label in shared).items())),
                },
                **_coefficient(
                    kappa(shared),
                    pair_kappa,
                    ratings,
                    seed,
                    resamples,
                    "both raters gave every case one and the same label",
                ),
            }
        )

    def pool_alpha(sample: Sequence[Ratings]) -> float | None:
        value = alpha([list(rated.values()) for rated in sample])
        return None if value is None else float(value)

    estimate = alpha([list(rated.values()) for rated in ratings])
    reason = "no case has two ratings" if not several else "every rating is one and the same label"
    return {
        "cases": len(ratings),
        "percent": stats.proportion(agreeing, len(several)).to_dict(),
        "kappa": pairs,
        "alpha": {
            "cases": len(several),
            "ratings": sum(len(rated) for rated in several),
            **_coefficient(estimate, pool_alpha, ratings, seed, resamples, reason),
        },
    }


def _coefficient(
    estimate: Fraction | None,
    statistic: Any,
    ratings: list[Ratings],
    seed: int,
    resamples: int,
    reason: str,
) -> dict[str, Any]:
    if estimate is None:
        return {"estimate": None, "status": "not_estimable", "reason": reason}
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
    return {"estimate": round(float(estimate), stats.DIGITS), "status": "ok", "interval": interval}


def agreement_text(report: Mapping[str, Any]) -> str:
    """The report for a terminal: each question's percent agreement (k of n, with its exact
    interval), each pair's kappa and the pool's alpha, raters as the report names them; counts
    and coefficients only, never a label."""
    seeds = report["seeds"]
    lines = [
        f"Rater agreement for {report['pack']} ({report['format']}, "
        f"{round(report['level'] * 100)}% intervals, bootstrap seed {seeds['bootstrap']}, "
        f"{seeds['resamples']} resamples)"
    ]
    for question, measured in report["questions"].items():
        percent = measured["percent"]
        lines.append(f"  {question}: {measured['cases']} cases")
        lines.append(
            f"    percent agreement: {percent['k']} of {percent['n']}"
            + _between(percent.get("low"), percent.get("high"))
        )
        for pair in measured["kappa"]:
            first, second = pair["raters"]
            lines.append(
                f"    kappa {first} and {second} (n {pair['n']}): {_coefficient_text(pair)}"
            )
        alpha = measured["alpha"]
        lines.append(
            f"    alpha ({alpha['cases']} cases, {alpha['ratings']} ratings): "
            f"{_coefficient_text(alpha)}"
        )
    return "\n".join(lines)


def _between(low: float | None, high: float | None) -> str:
    return "" if low is None or high is None else f" ({low} to {high})"


def _coefficient_text(measured: Mapping[str, Any]) -> str:
    if measured.get("estimate") is None:
        return f"not estimable: {measured.get('reason')}"
    interval = measured["interval"]
    if interval["status"] != "ok":
        return f"{measured['estimate']}, interval not estimable: {interval['reason']}"
    return f"{measured['estimate']}" + _between(interval["low"], interval["high"])
