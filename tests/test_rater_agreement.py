# SPDX-License-Identifier: Apache-2.0
"""Rater agreement (`mendari.agreement/1`, LP-11 requirement 4, `evals/rater_agreement.py`):
Cohen's kappa and Krippendorff's alpha match hand-computed references and independent
implementations (statsmodels' kappa, a coincidence-matrix alpha in fractions), and the bootstrap
interval matches scipy's on the same index matrix. Undefined coefficients and intervals say so,
and a second run gives the same bytes. Ratings are synthetic, of the triage fixture's
questions."""

from __future__ import annotations

import json
import random
from collections.abc import Sequence
from fractions import Fraction
from typing import Any

import numpy as np
import pytest
from scipy import stats as scipy_stats
from statsmodels.stats.inter_rater import cohens_kappa

from mendari.core.registry import resolve_pack
from mendari.errors import MendariError
from mendari.evals import stats
from mendari.evals.rater_agreement import FORMAT, agreement, alpha, kappa

PACK = "mendari-test/triage"
# What a rater says of a message unless a test says otherwise: the front desk reads it, and it
# holds no same-day symptom and no insult.
CALM = {"destination": "front_desk", "urgency": "no", "abusive": "no"}


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


def coincidence_alpha(units: Sequence[Sequence[str]]) -> Fraction:
    """Krippendorff's nominal alpha by the coincidence matrix, as written in Krippendorff
    (2011), in fractions: the independent reference."""
    pairable = [unit for unit in units if len(unit) >= 2]
    values = sorted({value for unit in pairable for value in unit})
    o = {(c, k): Fraction(0) for c in values for k in values}
    for unit in pairable:
        for i, c in enumerate(unit):
            for j, k in enumerate(unit):
                if i != j:
                    o[(c, k)] += Fraction(1, len(unit) - 1)
    n_c = {c: sum((o[(c, k)] for k in values), Fraction(0)) for c in values}
    n = sum(n_c.values(), Fraction(0))
    observed = sum((o[(c, k)] for c in values for k in values if c != k), Fraction(0))
    products = (n_c[c] * n_c[k] for c in values for k in values if c != k)
    expected = sum(products, Fraction(0)) / (n - 1)
    return 1 - observed / expected


def test_alpha_matches_hand_computed_reference() -> None:
    """Krippendorff (2011), "Computing Krippendorff's alpha-reliability", the nominal example:
    four observers, twelve units, missing values; alpha = 0.743, exactly 113/152."""
    a = ["1", "2", "3", "3", "2", "1", "4", "1", "2", None, None, None]
    b = ["1", "2", "3", "3", "2", "2", "4", "1", "2", "5", None, "3"]
    c = [None, "3", "3", "3", "2", "3", "4", "2", "2", "5", "1", None]
    d = ["1", "2", "3", "3", "2", "4", "4", "1", "2", "5", "1", None]
    units = [
        [value for value in unit if value is not None] for unit in zip(a, b, c, d, strict=True)
    ]
    assert alpha(units) == Fraction(113, 152) == coincidence_alpha(units)
    draw = random.Random(5)
    for _ in range(20):
        generated = [
            [draw.choice("xyz") for _ in range(draw.randint(0, 4))]
            for _ in range(draw.randint(3, 30))
        ]
        if alpha(generated) is not None:
            assert alpha(generated) == coincidence_alpha(generated)


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


def certain(**labels: str) -> dict[str, str]:
    """One rater's labels: `CALM` unless given."""
    return CALM | labels


def report(cases: list[dict[str, dict[str, str]]], **options: Any) -> dict[str, Any]:
    pack = resolve_pack(PACK)
    return agreement(pack.manifest, cases, pack=pack.ref, **options)


def pair_statistic(a: str, b: str) -> Any:
    def statistic(sample: Sequence[dict[str, str]]) -> float | None:
        value = kappa([(row[a], row[b]) for row in sample if a in row and b in row])
        return None if value is None else float(value)

    return statistic


def pool_statistic(sample: Sequence[dict[str, str]]) -> float | None:
    value = alpha([list(row.values()) for row in sample])
    return None if value is None else float(value)


def expected_interval(rows: list[dict[str, str]], statistic: Any, resamples: int) -> dict[str, Any]:
    """The interval the report should hold: the question's cases resampled with the seed."""
    _, low, high, dropped = stats.bootstrap_defined(rows, statistic, resamples, 0)
    if dropped:
        reason = f"undefined in {dropped} of {resamples} resamples"
        return {"status": "not_estimable", "reason": reason, "dropped": dropped}
    assert low is not None and high is not None
    return {"status": "ok", "low": round(low, 6), "high": round(high, 6), "dropped": 0}


def test_the_report_holds_percent_kappa_per_pair_and_alpha() -> None:
    cases = []
    for i in range(30):
        unsafe = "yes" if i % 3 == 0 else "no"
        flipped = "no" if unsafe == "yes" else "yes"
        other = flipped if i % 10 == 0 else unsafe  # the second rater disagrees on three cases
        raters = ("r1", "r2") if i % 2 else ("r1", "r3")
        cases.append({raters[0]: certain(urgency=unsafe), raters[1]: certain(urgency=other)})
    rows = [{rater: labels["urgency"] for rater, labels in case.items()} for case in cases]
    result = report(cases, resamples=200)
    assert result["format"] == FORMAT and result["level"] == 0.95
    assert result["seeds"] == {"bootstrap": 0, "resamples": 200}
    unsafe = result["questions"]["urgency"]
    assert unsafe["cases"] == 30
    assert unsafe["percent"] == stats.proportion(27, 30).to_dict()
    pairs = {tuple(pair["raters"]): pair for pair in unsafe["kappa"]}
    assert set(pairs) == {("r1", "r2"), ("r1", "r3")}
    for (a, b), pair in pairs.items():
        shared = [(case[a]["urgency"], case[b]["urgency"]) for case in cases if b in case]
        assert pair["n"] == len(shared) == 15
        assert pair["estimate"] == round(float(kappa(shared) or 0), 6)
        assert pair["labels"][a] == dict(sorted({"yes": 5, "no": 10}.items()))
        assert pair["interval"] == expected_interval(rows, pair_statistic(a, b), 200)
    units = [[case[r]["urgency"] for r in case] for case in cases]
    assert unsafe["alpha"]["estimate"] == round(float(alpha(units) or 0), 6)
    assert (unsafe["alpha"]["cases"], unsafe["alpha"]["ratings"]) == (30, 60)
    assert unsafe["alpha"]["interval"] == expected_interval(rows, pool_statistic, 200)
    statuses = {pair["interval"]["status"] for pair in unsafe["kappa"]}
    assert statuses == {"ok", "not_estimable"}  # both shapes are checked above
    for coefficient in [*unsafe["kappa"], unsafe["alpha"]]:
        interval = coefficient["interval"]
        if interval["status"] == "ok":
            assert interval["low"] <= coefficient["estimate"] <= interval["high"]


def test_an_undefined_coefficient_and_interval_say_so() -> None:
    same = [{"r1": certain(), "r2": certain()} for _ in range(10)]  # every label `no`
    result = report(same, resamples=50)
    out_of_scope = result["questions"]["abusive"]
    (pair,) = out_of_scope["kappa"]
    assert pair["estimate"] is None and pair["status"] == "not_estimable"
    assert pair["reason"] == "both raters gave every case one and the same label"
    assert "interval" not in pair
    assert out_of_scope["alpha"]["status"] == "not_estimable"
    assert out_of_scope["alpha"]["reason"] == "every rating is one and the same label"
    assert out_of_scope["percent"]["k"] == out_of_scope["percent"]["n"] == 10

    # Two `yes` cases in twelve (one agreed, one not): the estimate is defined, but resamples
    # that draw neither leave both raters on `no` throughout, so kappa is undefined there; the
    # interval is not estimable and says in how many.
    rare = [
        *same,
        {"r1": certain(abusive="yes"), "r2": certain(abusive="yes")},
        {"r1": certain(abusive="yes"), "r2": certain()},
    ]
    pair = report(rare, resamples=200)["questions"]["abusive"]["kappa"][0]
    assert pair["status"] == "ok" and pair["estimate"] is not None
    interval = pair["interval"]
    assert interval["status"] == "not_estimable" and 0 < interval["dropped"] < 200
    rows = stats.resample_indices(len(rare), 200, 0)
    assert interval["dropped"] == sum(1 for row in rows if 10 not in row and 11 not in row)
    assert interval["reason"] == f"undefined in {interval['dropped']} of 200 resamples"


def test_one_rater_per_case_has_no_agreement_to_measure() -> None:
    result = report([{"r1": certain()}, {"r2": certain(urgency="yes")}], resamples=20)
    unsafe = result["questions"]["urgency"]
    assert unsafe["percent"] == {"k": 0, "n": 0, "low": None, "high": None}
    assert unsafe["kappa"] == []
    assert unsafe["alpha"]["status"] == "not_estimable"
    assert unsafe["alpha"]["reason"] == "no case has two ratings"


@pytest.mark.parametrize(
    ("labels", "message"),
    [
        ({"no_such_question": "yes"}, "labels a question the agent does not have"),
        ({"urgency": "certainly"}, "urgency's label is not one of its labels"),
    ],
)
def test_ratings_the_pack_cannot_read_are_refused_by_position(
    labels: dict[str, str], message: str
) -> None:
    with pytest.raises(MendariError, match=f"case 1, rater r2: {message}") as refused:
        report([{"r1": certain()}, {"r2": labels}])
    assert "certainly" not in str(refused.value) and "no_such_question" not in str(refused.value)


def test_a_second_run_gives_the_same_bytes_and_the_seed_matters() -> None:
    draw = random.Random(9)
    teams = ("front_desk", "nurse", "pharmacy")
    cases = [{r: certain(destination=draw.choice(teams)) for r in ("r1", "r2")} for _ in range(40)]
    once = json.dumps(report(cases, resamples=300), sort_keys=True)
    assert once == json.dumps(report(cases, resamples=300), sort_keys=True)
    other = report(cases, resamples=300, seed=1)["questions"]["destination"]["kappa"][0]
    first = json.loads(once)["questions"]["destination"]["kappa"][0]
    assert other["estimate"] == first["estimate"] and other["interval"] != first["interval"]
