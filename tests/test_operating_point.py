# SPDX-License-Identifier: Apache-2.0
"""An evaluation's operating point (`mendari.evals.operating_point`): the exact bound and the
cases a target needs against scipy; the guarantee itself by simulation; the policy applied again
at another automation threshold; and what a run says when a threshold is supported, when
another is, and when none can be. Every agent here is the bundled demo agent or a copy of it
whose canned answers the test writes."""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import shutil
from bisect import bisect
from collections.abc import Sequence
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from scipy import stats as st

from mendari import Decision, Pack, load_pack
from mendari.cli import main
from mendari.core.card import render_card
from mendari.core.policy import PolicyInputs
from mendari.core.spec import (
    Condition,
    LabelPolicy,
    Outcome,
    PackManifest,
    PolicyRule,
    RiskLevel,
)
from mendari.errors import MendariError
from mendari.evals import operating_point as op
from mendari.evals.harness import Dataset, build_evaluation, evaluation, load_dataset
from mendari.evals.operating_point import Case, Tally, Targets
from mendari.evals.result import result_of
from mendari.providers.base import Answer, ProviderInfo
from mendari.sandbox.demo import run_demo
from tests.conftest import DEMO_AGENT_DIR, ROOT

EXACT = 1e-12
GUIDE = ROOT / "docs" / "guides" / "choosing-a-threshold.md"
URGENT = [("urgent_symptoms", "yes")]  # the demo agent's one critical label


@pytest.fixture(scope="module")
def demo() -> Pack:
    return load_pack(DEMO_AGENT_DIR)


@pytest.fixture(scope="module")
def demo_run(demo: Pack) -> tuple[Any, list[Any]]:
    """The demo agent on its 600-case set: the evaluation, and its decisions."""
    decision = Decision(demo, origin_cap=False)
    dataset = load_dataset(demo.path("data/test.jsonl"), name="test")
    results = [decision.decide(record["input"]) for record in dataset.records]
    return build_evaluation(decision, dataset, results), results


# -- the exact bound, against scipy ---------------------------------------------------------------


def _grid() -> list[tuple[int, int]]:
    cases = [(k, n) for n in range(1, 25) for k in range(n + 1)]
    for n in (50, 114, 299, 300, 600, 1000, 5000):
        cases += [(k, n) for k in sorted({0, 1, 2, 3, 9, n // 20, n // 4, n - 1, n})]
    return cases


@pytest.mark.parametrize("level", [0.9, 0.95, 0.975, 0.99, 0.9999])
def test_the_upper_bound_is_scipys_one_sided_clopper_pearson(level: float) -> None:
    for k, n in _grid():
        ours = op.upper_bound(k, n, level)
        assert abs(ours - (1.0 if k == n else st.beta.ppf(level, k + 1, n - k))) < EXACT, (k, n)
        exact = st.binomtest(k, n, alternative="less").proportion_ci(level, method="exact")
        assert abs(ours - exact.high) < 1e-9, (k, n)  # scipy's own interval, solved to 1e-9


def test_the_upper_bound_is_the_two_sided_intervals_at_half_the_error() -> None:
    """One-sided at 97.5% is the upper end of the 95% interval `mendari eval` prints: with two
    targets, which share the error level, the two say the same of the same k of n."""
    from mendari.evals import stats

    for k, n in [(0, 114), (9, 114), (9, 600), (3, 300)]:
        assert op.upper_bound(k, n, 0.975) == stats.clopper_pearson(k, n, 0.95)[1]


def test_a_bound_within_its_target_is_the_exact_binomial_test() -> None:
    """Certifying is rejecting "the rate is above the target" when seeing k or fewer of n has
    probability at most the error level at the target itself."""
    for level in (0.9, 0.95, 0.975):
        for target in (0.005, 0.01, 0.02, 0.05, 0.2):
            for k, n in _grid():
                p_value = st.binom.cdf(k, n, target)
                if abs(p_value - (1 - level)) < 1e-9:
                    continue  # at the boundary to within rounding: either answer
                assert (op.upper_bound(k, n, level) <= target) == (p_value <= 1 - level), (k, n)


@pytest.mark.parametrize("level", [0.9, 0.95, 0.975])
@pytest.mark.parametrize("target", [0.005, 0.01, 0.02, 0.05])
def test_no_single_test_errs_more_often_than_its_level(level: float, target: float) -> None:
    """The size of each test, exactly: for every n, the most cases that may have the risk and
    still be certified is seen with probability at most the error level when the true rate is
    the target (and less at any higher rate), and one more would not be."""
    for n in (20, 59, 114, 299, 300, 473, 600, 1500):
        most = _most(n, target, level)
        assert st.binom.cdf(most, n, target) <= 1 - level + EXACT, (n, most)
        assert st.binom.cdf(most + 1, n, target) > 1 - level, (n, most)
        # Every count up to it is certified and none past it: the bound grows with the count.
        assert all(op.upper_bound(k, n, level) <= target for k in range(most + 1))
        assert op.upper_bound(most + 1, n, level) > target


def test_the_upper_bound_refuses_what_is_not_k_of_n() -> None:
    for bad in [(1, 0), (-1, 5), (6, 5)]:
        with pytest.raises(ValueError):
            op.upper_bound(*bad, 0.95)
    assert op.upper_bound(1, 5, 0.95) > 0  # cached under the same hash as (True, 5)
    with pytest.raises(TypeError):
        op.upper_bound(True, 5, 0.95)  # a bool is not a count
    with pytest.raises(ValueError):
        op.upper_bound(1, 5, 1.0)


def test_the_known_numbers_of_cases() -> None:
    assert op.cases_needed(0.01, 0.95) == 299  # the "rule of three": 3 / 0.01, less one
    assert op.cases_needed(0.01, 0.975) == 368  # two targets share the error level
    assert op.cases_needed(0.05, 0.95) == 59
    assert op.cases_needed(0.001, 0.95) == 2995
    assert op.cases_needed(0.01, 0.95, k=1) == 473


@pytest.mark.parametrize("level", [0.9, 0.95, 0.975, 0.99])
def test_the_cases_needed_are_the_fewest_by_scipy(level: float) -> None:
    for target in (0.001, 0.005, 0.01, 0.02, 0.05, 0.1, 0.5):
        for k in (0, 1, 2, 5, 10):
            n = op.cases_needed(target, level, k)
            assert n > k
            assert st.beta.ppf(level, k + 1, n - k) <= target, (target, k, n)
            if n - 1 > k:  # one case fewer is not enough
                assert st.beta.ppf(level, k + 1, n - 1 - k) > target, (target, k, n)
            if k == 0:
                assert n == math.ceil(math.log(1 - level) / math.log(1 - target))


def test_the_cases_needed_refuse_what_is_not_a_target() -> None:
    for bad in (0.0, 1.0, -0.1):
        with pytest.raises(ValueError):
            op.cases_needed(bad, 0.95)
    with pytest.raises(ValueError):
        op.cases_needed(0.01, 0.95, k=-1)


def test_the_sequence_is_fixed_before_any_case() -> None:
    """1.00 down to the review threshold in hundredths: it depends on the manifest alone."""
    tested = op.sequence(0.5)
    assert tested[0] == 1.0 and tested[-1] == 0.5 and len(tested) == 51
    assert tested == sorted(tested, reverse=True)
    assert all(abs(t * 100 - round(t * 100)) < 1e-9 for t in tested)
    assert 0.9 in tested and 0.29 in op.sequence(0.29)  # the values a manifest writes
    assert op.sequence(0.505)[-1] == 0.51  # never under the review threshold
    assert op.sequence(1.0) == [1.0] and len(op.sequence(0.0)) == 101


# -- targets as people write them -----------------------------------------------------------------


def test_a_share_is_read_as_a_percentage_or_a_fraction() -> None:
    assert op.share("1%") == 0.01 == op.share("0.01") == op.share(" 1 % ".replace(" ", ""))
    assert op.share("0.5%") == 0.005 and op.share("95%") == 0.95 and op.share("2.5%") == 0.025
    for bad in ("1", "100%", "0", "-1%", "one", "", "%", "nan", "inf"):
        with pytest.raises(ValueError, match="a share is"):
            op.share(bad)
    # A limit on a run's own result is read the same way, and may be either end.
    assert op.share("0", whole=True) == 0.0 == op.share("0%", whole=True)
    assert op.share("1", whole=True) == 1.0 == op.share("100%", whole=True)
    assert op.share("90%", whole=True) == 0.9 == op.share("0.9", whole=True)
    for bad in ("90", "101%", "-1%", "one", "", "%", "nan", "inf"):
        with pytest.raises(ValueError, match="a share is"):
            op.share(bad, whole=True)


def test_targets_are_shares_and_the_error_level_is_split_between_two() -> None:
    assert Targets().given == 0 and Targets().level == 0.95
    assert Targets(critical_miss=0.01).level == 0.95
    assert Targets(false_automation=0.02, confidence=0.9).level == pytest.approx(0.9)
    both = Targets(critical_miss=0.01, false_automation=0.02)
    assert both.given == 2 and both.level == pytest.approx(0.975)
    assert both.to_dict() == {
        "critical_miss": {"at_most": 0.01, "of": "critical"},
        "false_automation": {"at_most": 0.02},
    }
    for bad in (
        {"critical_miss": 0.0},
        {"critical_miss": 1.0},
        {"false_automation": 1e-9},
        {"critical_miss_of": "some"},
        {"confidence": 0.3},
        {"confidence": 1.0},
    ):
        with pytest.raises(ValueError):
            Targets(**bad)


# -- the guarantee, by simulation -----------------------------------------------------------------

SEED = 20261123  # the day this was written: chosen before any run, never changed to pass one
RUNS = 2000
TESTED = op.sequence(0.5)
EARLY, LATE = 5, 20  # places in the sequence, 0.95 and 0.80: from where cases are automated


def _tolerance(rate: float, deviations: float = 3.0) -> float:
    """How far the share of RUNS simulated evaluations may be from the rate it estimates, by
    chance alone: three standard errors of a share of RUNS, which a share at that rate is
    beyond in about three seeds of a thousand. The seed is fixed, so a run passes or fails for
    good; the tolerance says how much of a wrong method it could hide (about 1.5 points)."""
    return deviations * math.sqrt(rate * (1 - rate) / RUNS)


def _counts(rng: random.Random, n: int, *shares: float) -> list[int]:
    """Of n cases drawn one by one, how many are of each kind, the kinds having these shares
    (the rest are of none). Only `random()` is used, whose sequence Python keeps for a seed."""
    cuts = [sum(shares[: i + 1]) for i in range(len(shares))]
    counts = [0] * (len(shares) + 1)
    for _ in range(n):
        counts[bisect(cuts, rng.random())] += 1
    return counts[:-1]


Automated = Sequence[tuple[int, int]]  # from which place of TESTED on, how many cases


def _walk(
    critical: Sequence[tuple[int, Automated]], wrong: Automated, cases: int, targets: Targets
) -> int:
    """How many thresholds of TESTED the walk certifies on one simulated evaluation: each
    critical label has its cases and those automated from a place on; `wrong` are the wrong
    answers automated from a place on, of `cases` labelled cases."""

    def at(place: int) -> Tally:
        labels = tuple(
            (sum(count for start, count in automated if place >= start), n)
            for n, automated in critical
        )
        false = sum(count for start, count in wrong if place >= start)
        return Tally(cases, automated=place, labelled=cases, wrong=false, critical=labels)

    return op.walk(at, len(TESTED), targets)


@cache
def _most(n: int, target: float, level: float) -> int:
    """The most of n cases that may have a risk for it still to be certified (-1: not even
    none)."""
    k = -1
    while k + 1 < n and op.upper_bound(k + 1, n, level) <= target:
        k += 1
    return k


def test_the_guarantee_holds_over_simulated_evaluations() -> None:
    """The true critical-miss rate is 0.4% from 0.95 down and 2.01% from 0.80 down: just above
    the 2% target, the hardest case to tell. A threshold at or under 0.80 is recommended in no
    more than 5% of 2,000 simulated evaluations: the walk has to certify 0.80 first, and that
    one test errs that seldom. The share seen is also the share the exact binomial gives, which
    checks the simulation as much as the method; and holding the share seen itself to the
    target, with no bound, is wrong about half the time."""
    targets = Targets(critical_miss=0.02)
    safe, unsafe = 0.004, 0.0161  # shares of the critical cases automated from 0.95, from 0.80
    sizes = range(560, 640)  # the critical cases an evaluation happens to hold
    expected = sum(st.binom.cdf(_most(n, 0.02, 0.95), n, safe + unsafe) for n in sizes) / len(sizes)
    rng = random.Random(SEED)
    violated = useful = naive = 0
    for _ in range(RUNS):
        n = sizes[int(rng.random() * len(sizes))]
        early, late = _counts(rng, n, safe, unsafe)
        passed = _walk([(n, [(EARLY, early), (LATE, late)])], [], 3 * n, targets)
        violated += passed > LATE  # the lowest certified threshold is 0.80 or under it
        useful += passed > EARLY  # a threshold that automates something is certified
        naive += (early + late) / n <= 0.02
    assert violated / RUNS <= 0.05 + _tolerance(0.05)
    assert abs(violated / RUNS - expected) <= _tolerance(expected), (violated, expected)
    assert 0.03 < expected <= 0.05  # near the level: the scenario does put the guarantee to it
    assert useful / RUNS > 0.95  # and the method is not safe by certifying nothing
    assert naive / RUNS > 0.4  # the simulation tells a rule with no guarantee from one with it


@pytest.mark.parametrize("first", ["critical_miss", "false_automation"])
def test_the_guarantee_holds_with_two_targets(first: str) -> None:
    """Two risks counted on different cases, each going above its target at its own threshold:
    one at 0.80, the other further down. Each is tested at half the error level, so a threshold
    with either above its target is recommended in no more than 5% of evaluations (and in no
    more than 2.5%: the risk that goes above first is tested at 97.5%)."""
    targets = Targets(critical_miss=0.02, false_automation=0.03)
    assert targets.level == pytest.approx(0.975)
    then = 30  # where the other risk goes above its target: 0.70
    critical_from, wrong_from = (LATE, then) if first == "critical_miss" else (then, LATE)
    rng = random.Random(SEED + (first == "critical_miss"))
    violated = useful = 0
    for _ in range(RUNS):
        n = 300 + int(rng.random() * 40)
        (missed,) = _counts(rng, n, 0.0205)  # of the critical cases: above the 2% target
        early, late = _counts(rng, 600, 0.005, 0.026)  # of the labelled: 0.5%, then 3.1%
        passed = _walk(
            [(n, [(critical_from, missed)])], [(EARLY, early), (wrong_from, late)], 600, targets
        )
        violated += passed > LATE
        useful += passed > EARLY
    assert violated / RUNS <= 0.05 + _tolerance(0.05)
    assert violated / RUNS <= 0.025 + _tolerance(0.025)
    assert useful / RUNS > 0.95


def test_the_guarantee_holds_for_every_critical_label_without_a_further_split() -> None:
    """Two critical labels, each tested at the whole error level. The rare one's true rate goes
    above the target at 0.80 and the common one's never does: 0.80 is certified only if the
    rare label's own test errs, so the 5% holds for the two together, at the share the exact
    binomial gives for that one test."""
    targets = Targets(critical_miss=0.02)
    sizes = range(300, 340)
    expected = sum(st.binom.cdf(_most(n, 0.02, 0.95), n, 0.0205) for n in sizes) / len(sizes)
    rng = random.Random(SEED + 2)
    violated = useful = 0
    for _ in range(RUNS):
        rare = sizes[int(rng.random() * len(sizes))]
        (missed,) = _counts(rng, rare, 0.0205)
        (common,) = _counts(rng, 900, 0.002)  # the common label: 0.2%, from 0.95 down
        labels = [(rare, [(LATE, missed)]), (900, [(EARLY, common)])]
        passed = _walk(labels, [], 3000, targets)
        violated += passed > LATE
        useful += passed > EARLY
    assert violated / RUNS <= 0.05 + _tolerance(0.05)
    assert abs(violated / RUNS - expected) <= _tolerance(expected), (violated, expected)
    assert 0.02 < expected <= 0.05 and useful / RUNS > 0.95


def test_the_walk_stops_at_the_first_threshold_it_cannot_certify() -> None:
    """Never past it: a threshold further down that would pass on its own is not reached."""
    targets = Targets(critical_miss=0.05)
    counts = [0, 0, 30, 0, 0]  # the third threshold fails; the ones after it would pass alone

    def at(place: int) -> Tally:
        return Tally(900, 10 * place, 900, 0, ((counts[place], 300),))

    alone = [op.certified(at(place), targets) for place in range(5)]
    assert alone == [True, True, False, True, True]
    assert op.walk(at, 5, targets) == 2
    assert op.walk(at, 2, targets) == 2  # every threshold of a short sequence
    assert op.walk(at, 0, targets) == 0


def test_a_risk_with_no_case_to_count_is_never_certified() -> None:
    nothing = Tally(cases=10, automated=0, labelled=0, wrong=0, critical=((0, 0),))
    assert not op.certified(nothing, Targets(critical_miss=0.5))
    assert not op.certified(nothing, Targets(false_automation=0.5))
    assert op.certified(nothing, Targets())  # nothing is held to a target


# -- the policy, applied again --------------------------------------------------------------------

INFO = ProviderInfo("reader", "agent", "agent", "synthetic")


def answered(request: str, confidence: float, urgent: float = 0.05) -> dict[str, Answer]:
    """The demo agent's two answers: `request` with `confidence`, urgent symptoms at `urgent`."""
    rest = (1.0 - confidence) / 2
    probabilities = {label: rest for label in ("refill", "appointment", "other")}
    probabilities[request] = confidence
    flagged = "yes" if urgent >= 0.5 else "no"
    return {
        "request": Answer("request", request, confidence, probabilities, INFO),
        "urgent_symptoms": Answer(
            "urgent_symptoms",
            flagged,
            max(urgent, 1.0 - urgent),
            {"yes": urgent, "no": 1.0 - urgent},
            INFO,
        ),
    }


def a_case(
    manifest: PackManifest,
    request: str,
    confidence: float,
    *,
    truth: str | None = None,
    critical: bool = False,
    urgent: float = 0.05,
    failures: dict[str, str] | None = None,
) -> Case:
    """A case as a run would hold it: decided by the manifest's policy from these answers."""
    inputs = PolicyInputs(answered(request, confidence, urgent), failures or {}, None, {})
    result = inputs.apply(manifest)
    truth = request if truth is None else truth
    return Case(
        truth, (0,) if critical else (), result.outcome.value, result.recommendation, inputs
    )


def outcomes_at(manifest: PackManifest, cases: Sequence[Case], threshold: float) -> list[str]:
    return [outcome for outcome, _ in op.decided(op.at_threshold(manifest, threshold), cases)]


def test_applying_the_policy_again_at_the_agents_threshold_gives_the_run_back(
    demo: Pack, demo_run: tuple[Any, list[Any]]
) -> None:
    """Case for case, outcome and recommendation, on the demo agent's 600 cases: and so the
    counts the operating point starts from are the ones `mendari eval` prints."""
    run, results = demo_run
    cases = op.cases_of(run.ledger, results)
    recorded = [(result.outcome.value, result.recommendation) for result in results]
    assert op.decided(demo.manifest, cases) == recorded
    assert op.decided(op.at_threshold(demo.manifest, 0.90), cases) == recorded  # its own, set again
    assert all(case.replays for case in cases)
    found = op.tally(cases, recorded, 1)
    report = run.report
    decisions = report["decisions"]
    assert found.cases == 600 and found.automated == decisions["outcome_counts"]["AUTO"] == 326
    (critical,) = decisions["critical"]
    assert found.critical == ((critical["auto_missed"], critical["n"]),) == ((9, 114),)
    wrong = report["proportions"]["false_automation"]
    assert (found.wrong, found.labelled) == (wrong["k"], wrong["n"]) == (9, 600)
    assert found == op.tally_of(report)  # and a report's own counts give the same


def test_the_block_states_the_counts_the_evaluation_prints(
    demo_run: tuple[Any, list[Any]],
) -> None:
    run, _ = demo_run
    report = run.report
    current = report["run"]["operating_point"]["current"]
    assert current["threshold"] == report["policy"]["thresholds"]["auto"] == 0.9
    assert current["automated"] == {"k": 326, "n": 600}
    (label,) = current["critical"]
    assert (label["question"], label["label"]) == URGENT[0]
    assert label["of_critical"] == {"k": 9, "n": 114, "upper": round(st.beta.ppf(0.95, 10, 105), 6)}
    assert label["of_all"] == {"k": 9, "n": 600, "upper": round(st.beta.ppf(0.95, 10, 591), 6)}
    assert current["false_automation"] == label["of_all"]  # the same 9 of 600, as it happens
    assert op.of_report(report) == current  # what a card works out from the result alone


def test_another_threshold_changes_only_what_the_threshold_decides(demo: Pack) -> None:
    """The rule, the label that is never automated and a failed provider decide as they did at
    every threshold; a plain case is automated exactly from its own confidence down."""
    manifest = demo.manifest
    plain = a_case(manifest, "refill", 0.93)
    by_rule = a_case(manifest, "refill", 0.99, urgent=0.9)  # the rule sends it to a nurse
    never = a_case(manifest, "other", 0.99)  # `other` is never automated
    failed = a_case(manifest, "refill", 0.99, failures={"urgent_symptoms": "timeout"})
    unsure = a_case(manifest, "appointment", 0.40)  # under the review threshold: no suggestion
    cases = [plain, by_rule, never, failed, unsure]
    assert [case.outcome for case in cases] == [
        "AUTO",
        "HUMAN_REVIEW",
        "HUMAN_REVIEW",
        "HUMAN_REVIEW",
        "ABSTAIN",
    ]
    for threshold in op.sequence(manifest.policy.thresholds.review):
        expected = "AUTO" if threshold <= 0.93 else "HUMAN_REVIEW"
        assert outcomes_at(manifest, cases, threshold) == [
            expected,
            "HUMAN_REVIEW",
            "HUMAN_REVIEW",
            "HUMAN_REVIEW",
            "ABSTAIN",
        ], threshold
    # The boundary is the policy's own: at least the threshold, as a double compares.
    assert outcomes_at(manifest, [plain], 0.93) == ["AUTO"]
    assert outcomes_at(manifest, [plain], 0.94) == ["HUMAN_REVIEW"]


def test_a_risk_cap_holds_at_every_threshold(demo: Pack) -> None:
    """Policy only lowers automation: at risk level 2 nothing is automated, however low the
    threshold, and the run says there is no threshold to recommend."""
    risky = demo.manifest.model_copy(
        update={"risk": demo.manifest.risk.model_copy(update={"level": RiskLevel(2)})}
    )
    cases = [a_case(risky, "refill", 0.99) for _ in range(400)]
    assert {case.outcome for case in cases} == {"HUMAN_REVIEW"}
    assert set(outcomes_at(risky, cases, 0.5)) == {"HUMAN_REVIEW"}
    found = op.recommend(risky, cases, Targets(false_automation=0.05))
    assert found.status == op.NOTHING_AUTOMATED and found.threshold is None
    block = op.block_of(risky, URGENT, Targets(false_automation=0.05), _current(cases), found)
    assert block["summary"] == (
        "The policy automates no case at any threshold (a rule, a label's limit or a cap sends "
        "each to a person), so there is no threshold to recommend."
    )
    assert block["recommended"] is None and block["change"] is None
    assert block["needs"] == [] and block["current_supported"] is None
    # The same with too few cases to show the target: there is still nothing to hold to it,
    # and the run does not ask for cases that could change nothing.
    few = cases[:20]
    assert not op.certified(_current(few), Targets(false_automation=0.05))
    assert op.recommend(risky, few, Targets(false_automation=0.05)).status == op.NOTHING_AUTOMATED
    # A label that is never automated, alike: every case of this set is `other`.
    others = [a_case(demo.manifest, "other", 0.99) for _ in range(30)]
    found = op.recommend(demo.manifest, others, Targets(false_automation=0.05))
    assert found.status == op.NOTHING_AUTOMATED


def test_a_label_with_a_threshold_of_its_own_keeps_it(demo: Pack) -> None:
    policy = demo.manifest.policy
    labels = {**policy.labels, "refill": LabelPolicy(auto_threshold=0.97)}
    manifest = demo.manifest.model_copy(
        update={"policy": policy.model_copy(update={"labels": labels})}
    )
    refill = a_case(manifest, "refill", 0.95)
    appointment = a_case(manifest, "appointment", 0.95)
    assert (refill.outcome, appointment.outcome) == ("HUMAN_REVIEW", "AUTO")
    for threshold in (1.0, 0.96, 0.95, 0.5):
        outcomes = outcomes_at(manifest, [refill, appointment], threshold)
        assert outcomes == ["HUMAN_REVIEW", "AUTO" if threshold <= 0.95 else "HUMAN_REVIEW"]
    cases = [a_case(manifest, "appointment", 0.95) for _ in range(400)]
    targets = Targets(false_automation=0.05)
    block = op.block_of(
        manifest, URGENT, targets, _current(cases), op.recommend(manifest, cases, targets)
    )
    assert (
        "A label with a threshold of its own (refill) keeps it: only `policy.thresholds.auto` "
        "was varied." in block["lines"]
    )


def _current(cases: Sequence[Case], labels: int = 1) -> Tally:
    return op.tally(cases, [(case.outcome, case.recommendation) for case in cases], labels)


def _block(manifest: PackManifest, cases: Sequence[Case], targets: Targets) -> dict[str, Any]:
    """The block of a run that holds `cases`, as `build` makes it from a ledger."""
    found = op.recommend(manifest, cases, targets) if targets.given else op.Found(op.NO_TARGET)
    return op.block_of(manifest, URGENT, targets, _current(cases), found)


# -- a threshold the cases support ----------------------------------------------------------------


def graded(manifest: PackManifest) -> list[Case]:
    """600 cases whose confidence tells. 200 are critical: the agent flags 190 (a rule sends
    them to a nurse) and misses 10, answering them wrongly with a confidence of 0.85 to 0.92
    (two each at 0.85 and 0.86, one at each hundredth above). 380 plain ones are right, at 0.93
    to 0.99. 20 plain ones are wrong, at 0.70."""
    cases = [a_case(manifest, "refill", 0.99, critical=True, urgent=0.9) for _ in range(190)]
    cases += [
        a_case(manifest, "refill", round(0.85 + (i % 8) / 100, 2), critical=True, truth="other")
        for i in range(10)
    ]
    cases += [a_case(manifest, "appointment", round(0.93 + (i % 7) / 100, 2)) for i in range(380)]
    cases += [a_case(manifest, "refill", 0.70, truth="appointment") for _ in range(20)]
    return cases


def test_a_threshold_is_recommended_with_its_guarantee(demo: Pack) -> None:
    """The agent's 0.90 automates three critical cases it missed. The cases support 0.93, the
    lowest threshold at which none of the 200 is automated: 0 of 200 is at most 1.5%, and the
    1 of 200 at 0.92 is at most 2.4%, above the 2% target."""
    manifest = demo.manifest
    block = _block(manifest, graded(manifest), Targets(critical_miss=0.02))
    assert block["status"] == op.RECOMMENDED and block["current_supported"] is False
    current = block["current"]
    assert current["automated"] == {"k": 383, "n": 600}
    assert current["critical"][0]["of_critical"]["k"] == 3  # at 0.90, 0.91 and 0.92
    recommended = block["recommended"]
    assert recommended["threshold"] == 0.93
    assert recommended["automated"] == {"k": 380, "n": 600}
    assert recommended["critical"][0]["of_critical"] == {
        "k": 0,
        "n": 200,
        "upper": round(1 - 0.05 ** (1 / 200), 6),
    }
    assert recommended["false_automation"]["k"] == 0
    assert block["change"] == {
        "setting": "policy.thresholds.auto",
        "from": 0.9,
        "to": 0.93,
        "line": "auto: 0.93",
    }
    assert block["summary"] == (
        "Recommended automation threshold 0.93 (now 0.90, which these cases do not support): "
        "critical misses at most 2%, 95% confidence, 63.3% automated."
    )
    assert block["guarantee"] == (
        "With 95% confidence, at the automation threshold 0.93 critical misses stay at or below "
        "2% of critical cases, provided the cases the agent will see resemble these, the labels "
        "are right, and the agent, its configuration and its version do not change; after any "
        "of those changes, evaluate again."
    )
    walk = block["walk"]
    assert (walk["from"], walk["to"], walk["step"]) == (1.0, 0.5, 0.01)
    assert walk["certified"] == 8 and walk["stopped_at"] == 0.92  # 1.00 to 0.93, then 0.92
    assert [row["threshold"] for row in walk["tested"]] == op.sequence(0.5)[:9]
    assert [row["certified"] for row in walk["tested"]] == [True] * 8 + [False]
    assert [row["automated"] for row in walk["tested"]] == [
        0,
        54,
        108,
        162,
        216,
        270,
        325,
        380,
        381,
    ]
    assert walk["tested"][-1] == {
        "threshold": 0.92,
        "automated": 381,
        "critical": [1],
        "false_automation": 1,
        "certified": False,
    }
    assert block["lines"] == [
        "At 0.93, 380 of 600 cases would be automated: critical urgent_symptoms=yes automated "
        "0 of 200 critical cases (at most 1.5%), 0 of all 600 cases (at most 0.50%); false "
        "automation 0 of 600 labelled cases (at most 0.50%).",
        "At the agent's automation threshold 0.90, 383 of 600 cases are automated: critical "
        "urgent_symptoms=yes automated 3 of 200 critical cases (at most 3.9%), 3 of all 600 "
        "cases (at most 1.3%); false automation 3 of 600 labelled cases (at most 1.3%).",
        "The walk down from 1.00 stopped at 0.92, which cannot be certified: 1 of 200 critical "
        "cases (urgent_symptoms=yes) is automated (at most 2.4%).",
        f"Guarantee: w{block['guarantee'][1:]}",
        "To adopt it, change one line of the agent's manifest, under `policy.thresholds`: "
        "`auto: 0.93` (now 0.90). Nothing changes it for you.",
        'Each "at most" is the exact one-sided bound (Clopper-Pearson) of its count at 95% '
        "confidence. It is about cases like these, labelled as these are, and about this version "
        "and configuration of the agent.",
    ]
    assert block["needs"] == [] and block["cannot_fix"] == []
    # The numbers are scipy's: none of 200 is within 2%, one of 200 is not.
    assert st.beta.ppf(0.95, 1, 200) <= 0.02 < st.beta.ppf(0.95, 2, 199)


def test_the_strictest_of_the_thresholds_that_automate_as_much_is_the_one(demo: Pack) -> None:
    """Every threshold down to the review threshold is certified for false automation at most
    10% (30 of 600 is at most 6.8%), and from 0.70 down they all automate the same 410 cases:
    0.70 is the one recommended, never 0.50, of which the set says nothing more."""
    manifest = demo.manifest
    cases = graded(manifest)
    targets = Targets(false_automation=0.1)
    found = op.recommend(manifest, cases, targets)
    assert found.passed == 51 and found.stopped_at is None
    assert found.status == op.RECOMMENDED and found.threshold == 0.70
    assert found.supported is True  # 0.90 is certified too: it automates less
    block = _block(manifest, cases, targets)
    assert block["summary"] == (
        "Recommended automation threshold 0.70 (now 0.90, also supported): false automation at "
        "most 10%, 95% confidence, 68.3% automated."
    )
    assert block["recommended"]["false_automation"]["k"] == 30
    assert (
        "Every threshold tested is certified, down to the review threshold 0.50." in block["lines"]
    )
    # A threshold lowered for one risk automates critical cases: said, with their count.
    assert (
        "No target was given for critical misses: at 0.70, 10 of 200 critical cases "
        "(urgent_symptoms=yes) are automated, and no person sees them." in block["lines"]
    )


def test_the_agents_own_threshold_is_supported_when_none_certified_automates_more(
    demo: Pack,
) -> None:
    targets = Targets(critical_miss=0.02)
    manifest = op.at_threshold(demo.manifest, 0.93)
    block = _block(manifest, graded(manifest), targets)
    assert block["status"] == op.SUPPORTED and block["current_supported"] is True
    assert block["change"] is None  # nothing to change
    assert block["recommended"] == block["current"]
    assert block["summary"] == (
        "The agent's automation threshold 0.93 is supported: critical misses at most 2%, 95% "
        "confidence, 63.3% automated."
    )
    assert block["guarantee"].startswith("With 95% confidence, at the automation threshold 0.93 ")
    assert not any("change one line" in line for line in block["lines"])


def test_a_threshold_between_two_tested_ones_is_supported_from_the_lowest_certified_up(
    demo: Pack,
) -> None:
    """A manifest's threshold need not be a hundredth. At or above the lowest certified
    threshold it is supported (the policy automates no more at it); under it, it is not,
    though it automates the same cases of this set: the next tested one down is not certified."""
    targets = Targets(critical_miss=0.02)  # certified down to 0.93 on these cases
    above = op.at_threshold(demo.manifest, 0.935)
    block = _block(above, graded(above), targets)
    assert block["status"] == op.RECOMMENDED and block["current_supported"] is True
    assert block["summary"].startswith(  # 0.93 automates the 55 cases at 0.93 as well
        "Recommended automation threshold 0.93 (now 0.935, also supported): "
    )
    assert block["change"] == {
        "setting": "policy.thresholds.auto",
        "from": 0.935,
        "to": 0.93,
        "line": "auto: 0.93",
    }
    under = op.at_threshold(demo.manifest, 0.925)
    block = _block(under, graded(under), targets)
    assert (
        block["current"]["automated"] == block["recommended"]["automated"] == {"k": 380, "n": 600}
    )
    assert block["status"] == op.RECOMMENDED and block["current_supported"] is False
    assert block["summary"].startswith(
        "Recommended automation threshold 0.93 (now 0.925, which these cases do not support): "
    )
    # Supported and automating as much as any certified threshold: nothing to change.
    wrong = Targets(false_automation=0.1)  # certified all the way down; 0.70 automates the most
    level = op.at_threshold(demo.manifest, 0.695)
    block = _block(level, graded(level), wrong)
    assert block["status"] == op.SUPPORTED and block["recommended"]["threshold"] == 0.695
    assert block["summary"].startswith("The agent's automation threshold 0.695 is supported: ")
    assert op.threshold_text(0.695) == "0.695" and op.threshold_text(0.9) == "0.90"


def test_two_targets_share_the_error_level(demo: Pack) -> None:
    """0 of 200 is at most 1.49% at 95% and at most 1.83% at 97.5%: within a 1.6% target on its
    own, and not once a second target takes half of the error level."""
    manifest = demo.manifest
    cases = graded(manifest)
    assert st.beta.ppf(0.95, 1, 200) <= 0.016 < st.beta.ppf(0.975, 1, 200)
    alone = _block(manifest, cases, Targets(critical_miss=0.016))
    assert alone["status"] == op.RECOMMENDED and alone["level"] == 0.95
    both = _block(manifest, cases, Targets(critical_miss=0.016, false_automation=0.1))
    assert both["status"] == op.NONE_CERTIFIED and both["level"] == pytest.approx(0.975)
    assert both["confidence"] == 0.95
    (need,) = both["needs"]
    assert need["cases"] == op.cases_needed(0.016, 0.975) == 229 and need["n"] == 200
    assert (
        "Showing at most 1.6% with 97.5% confidence needs at least 229 critical cases "
        "(urgent_symptoms=yes) with none automated; this set has 200." in both["lines"]
    )
    assert both["lines"][-1].startswith(
        'Each "at most" is the exact one-sided bound (Clopper-Pearson) of its count at 97.5% '
        "confidence: the two targets share the 5% error level."
    )
    # The bounds a block states are at the level its targets are tested at.
    figure = both["current"]["false_automation"]
    assert figure == {"k": 3, "n": 600, "upper": round(st.beta.ppf(0.975, 4, 597), 6)}
    # With both within reach, the summary and the guarantee name both.
    held = _block(manifest, cases, Targets(critical_miss=0.02, false_automation=0.1))
    assert held["status"] == op.RECOMMENDED and held["recommended"]["threshold"] == 0.93
    assert held["summary"] == (
        "Recommended automation threshold 0.93 (now 0.90, which these cases do not support): "
        "critical misses at most 2%, false automation at most 10%, 95% confidence, 63.3% "
        "automated."
    )
    assert (
        "critical misses stay at or below 2% of critical cases and false automation stays at "
        "or below 10% of labelled cases" in held["guarantee"]
    )


def test_a_target_on_a_share_of_all_cases(demo: Pack) -> None:
    """The same misses held to a share of every case: 6 of 600 at 0.87 is at most 2.0%, and the
    8 of 600 at 0.86 is at most 2.4%. The agent's 0.90 is supported, and 0.87 automates more."""
    manifest = demo.manifest
    block = _block(manifest, graded(manifest), Targets(critical_miss=0.02, critical_miss_of="all"))
    assert block["targets"]["critical_miss"] == {"at_most": 0.02, "of": "all"}
    assert block["status"] == op.RECOMMENDED and block["current_supported"] is True
    assert block["recommended"]["threshold"] == 0.87 and block["walk"]["stopped_at"] == 0.86
    assert block["recommended"]["critical"][0]["of_all"]["k"] == 6
    assert st.beta.ppf(0.95, 7, 594) <= 0.02 < st.beta.ppf(0.95, 9, 592)
    assert block["summary"] == (
        "Recommended automation threshold 0.87 (now 0.90, also supported): critical misses at "
        "most 2% of all cases, 95% confidence, 64.3% automated."
    )
    assert "critical misses stay at or below 2% of all cases" in block["guarantee"]
    assert (
        "The walk down from 1.00 stopped at 0.86, which cannot be certified: 8 of 600 cases are "
        "critical (urgent_symptoms=yes) and automated (at most 2.4%)." in block["lines"]
    )


def test_every_critical_label_is_held_to_the_target_on_its_own(demo: Pack) -> None:
    """Two critical labels: the common one is never automated, and 2 of the rare one's 40 cases
    are. Pooled, 2 of 440 would be within a 2% target (at most 1.5%); the rare label on its own
    is not (0 of 40 is at most 7.3%), and the run says how many of its cases would show it."""
    names = [("urgent_symptoms", "yes"), ("request", "other")]
    manifest = demo.manifest
    common = [a_case(manifest, "refill", 0.99, critical=True, urgent=0.9) for _ in range(400)]
    rare = [
        Case(case.truth, (1,), case.outcome, case.recommendation, case.inputs)
        for case in [a_case(manifest, "refill", 0.95, truth="other") for _ in range(2)]
        + [a_case(manifest, "other", 0.95) for _ in range(38)]
    ]
    plain = [a_case(manifest, "appointment", 0.95) for _ in range(400)]
    cases = [*common, *rare, *plain]
    targets = Targets(critical_miss=0.02)
    assert op.upper_bound(2, 440, 0.95) <= 0.02 < op.upper_bound(0, 40, 0.95)
    found = op.recommend(
        manifest.model_copy(
            update={
                "evaluation": manifest.evaluation.model_copy(
                    update={
                        "critical": [
                            *manifest.evaluation.critical,
                            manifest.evaluation.critical[0].model_copy(
                                update={"question": "request", "label": "other"}
                            ),
                        ]
                    }
                )
            }
        ),
        cases,
        targets,
    )
    assert found.status == op.NONE_CERTIFIED and found.passed == 0
    (failed,) = found.failed
    assert (failed.label, failed.k, failed.n) == (1, 0, 40)  # the rare label, at 1.00
    block = op.block_of(manifest, names, targets, _current(cases, 2), found)
    assert [need["cases"] for need in block["needs"]] == [149]
    assert (
        "Showing at most 2% with 95% confidence needs at least 149 critical cases "
        "(request=other) with none automated; this set has 40." in block["lines"]
    )
    (fix,) = block["cannot_fix"]  # and no threshold fixes it: 2 of its 40 at 0.95
    assert (fix["label"], fix["threshold"], fix["k"], fix["n"]) == ("other", 0.95, 2, 40)
    assert "critical request=other automated 2 of 40 critical cases" in block["lines"][0]


# -- when no threshold can be certified -----------------------------------------------------------


def test_nothing_qualifies_and_the_cases_needed_are_said(
    demo_run: tuple[Any, list[Any]], demo: Pack
) -> None:
    """The demo agent, held to critical misses at most 1%: 114 critical cases cannot show it at
    any threshold (0 of 114 is at most 2.6%), and 299 with none automated would."""
    run, results = demo_run
    block = op.build(demo.manifest, run.ledger, results, Targets(critical_miss=0.01))
    assert block["status"] == op.NONE_CERTIFIED
    assert block["recommended"] is None and block["change"] is None and block["guarantee"] is None
    assert block["current_supported"] is False
    assert block["summary"] == (
        "No automation threshold can be certified for critical misses at most 1% with 95% "
        "confidence."
    )
    assert block["needs"] == [
        {
            "risk": "critical_miss",
            "question": "urgent_symptoms",
            "label": "yes",
            "of": "critical",
            "k": 0,
            "n": 114,
            "at_most": 0.01,
            "cases": 299,
        }
    ]
    assert (
        "Showing at most 1% with 95% confidence needs at least 299 critical cases "
        "(urgent_symptoms=yes) with none automated; this set has 114." in block["lines"]
    )
    # The number is the one scipy gives: 299 is enough, 298 is not, and 114 is far from it.
    assert st.beta.ppf(0.95, 1, 299) <= 0.01 < st.beta.ppf(0.95, 1, 298)
    assert st.beta.ppf(0.95, 1, 114) == pytest.approx(0.0259, abs=1e-4)
    walk = block["walk"]
    assert walk["certified"] == 0 and walk["stopped_at"] == 1.0
    assert walk["tested"] == [
        {
            "threshold": 1.0,
            "automated": 0,
            "critical": [0],
            "false_automation": 0,
            "certified": False,
        }
    ]


def test_a_risk_the_threshold_cannot_fix_is_said_to_be_one(
    demo_run: tuple[Any, list[Any]], demo: Pack
) -> None:
    """The demo agent misses urgent messages at any confidence: at 0.95, the strictest
    threshold that automates anything, 3 of its 114 are automated already."""
    run, results = demo_run
    block = op.build(demo.manifest, run.ledger, results, Targets(critical_miss=0.01))
    (fix,) = block["cannot_fix"]
    assert (fix["threshold"], fix["k"], fix["n"], fix["risk"]) == (0.95, 3, 114, "critical_miss")
    assert block["lines"][2] == (
        "The threshold cannot fix this: at 0.95, the strictest threshold that automates any "
        "case, 3 of 114 critical cases (urgent_symptoms=yes) are automated (2.6%, above the 1% "
        "target), and every lower threshold is above it too. What can: the agent itself, a rule "
        "that sends such cases to a person, or a second signal."
    )
    assert len(block["lines"]) == 5  # the point, the cases needed, this, the repeats, the method
    # False automation is the same story for this agent: 9 of 600 at 0.95 and below.
    wrong = op.build(demo.manifest, run.ledger, results, Targets(false_automation=0.01))
    assert wrong["status"] == op.NONE_CERTIFIED and wrong["needs"] == []
    (fix,) = wrong["cannot_fix"]
    assert (fix["threshold"], fix["k"], fix["n"], fix["risk"]) == (0.95, 9, 600, "false_automation")
    assert "9 of 600 labelled cases are automated with a wrong answer (1.5%" in wrong["lines"][1]
    # With a target the share seen is under, more cases would do: no such sentence.
    lenient = op.build(demo.manifest, run.ledger, results, Targets(critical_miss=0.05))
    assert lenient["status"] == op.NONE_CERTIFIED and lenient["cannot_fix"] == []
    (need,) = lenient["needs"]
    assert (need["k"], need["n"], need["cases"]) == (3, 114, op.cases_needed(0.05, 0.95, 3))
    assert lenient["lines"][1:3] == [
        "The walk down from 1.00 stopped at 0.95, which cannot be certified: 3 of 114 critical "
        "cases (urgent_symptoms=yes) are automated (at most 6.7%).",
        "Showing at most 5% with 95% confidence needs at least 153 critical cases "
        "(urgent_symptoms=yes) with no more than 3 automated; this set has 114.",
    ]


def test_cases_automated_whatever_the_threshold_are_beyond_it(demo: Pack) -> None:
    """A rule that automates on its own, at any confidence: the critical cases it automates are
    automated at 1.00 too, and no threshold changes that."""
    policy = demo.manifest.policy
    rule = PolicyRule(
        name="refills-go-through",
        when=Condition(question="request", labels=["refill"]),
        outcome=Outcome.AUTO,
        reason="Refills are filled at once.",
    )
    manifest = demo.manifest.model_copy(
        update={"policy": policy.model_copy(update={"rules": [*policy.rules, rule]})}
    )
    cases = [a_case(manifest, "refill", 0.6, critical=True, truth="other") for _ in range(12)]
    cases += [a_case(manifest, "appointment", 0.95, critical=True, urgent=0.9) for _ in range(388)]
    cases += [a_case(manifest, "appointment", 0.95) for _ in range(400)]
    assert outcomes_at(manifest, cases[:1], 1.0) == ["AUTO"]  # at the strictest threshold too
    block = _block(manifest, cases, Targets(critical_miss=0.01))
    assert block["status"] == op.NONE_CERTIFIED and block["needs"] == []
    assert block["walk"]["stopped_at"] == 1.0
    (fix,) = block["cannot_fix"]
    assert (fix["threshold"], fix["k"], fix["n"]) == (1.0, 12, 400)
    assert block["lines"][1].startswith(
        "The threshold cannot fix this: at 1.00, the strictest threshold that automates any "
        "case, 12 of 400 critical cases (urgent_symptoms=yes) are automated (3.0%, above the 1% "
        "target)"
    )
    assert len(block["lines"]) == 3  # the point, that sentence and the method: said once


def test_a_run_that_did_not_keep_what_its_policy_decided_from_recommends_nothing(
    demo: Pack,
) -> None:
    """A case automated with nothing to apply the policy to again, or one whose policy gives
    another outcome than the run recorded: no threshold is recommended on such cases."""
    manifest = demo.manifest
    kept = [a_case(manifest, "refill", 0.95) for _ in range(300)]
    bare = Case("refill", (), "AUTO", "refill", None)
    assert not bare.replays and Case(None, (), "BLOCK", None, None).replays  # a refused input does
    targets = Targets(false_automation=0.05)
    assert op.recommend(manifest, [*kept, bare], targets).status == op.NO_REPLAY
    changed = Case("refill", (), "HUMAN_REVIEW", "refill", kept[0].inputs)  # the policy says AUTO
    assert op.recommend(manifest, [*kept, changed], targets).status == op.NO_REPLAY
    block = _block(manifest, [*kept, bare], targets)
    assert block["status"] == op.NO_REPLAY and block["walk"] is None
    assert block["summary"].startswith("No threshold is recommended: this run did not keep")
    # Refused inputs and unread records are decided before any policy: they stay as they were.
    refused = Case(None, (), "BLOCK", None, None)
    assert op.recommend(manifest, [*kept, refused], targets).status == op.SUPPORTED


def test_a_target_on_critical_misses_needs_a_critical_label(demo: Pack) -> None:
    evaluation_ = demo.manifest.evaluation.model_copy(update={"critical": []})
    manifest = demo.manifest.model_copy(update={"evaluation": evaluation_})
    with pytest.raises(MendariError, match="declares no critical label"):
        op.check(manifest, Targets(critical_miss=0.01))
    op.check(manifest, Targets(false_automation=0.01))  # nothing to refuse
    op.check(manifest, None)
    op.check(demo.manifest, Targets(critical_miss=0.01))


# -- no target, and sets too small to say anything ------------------------------------------------


def test_with_no_target_the_bound_at_the_agents_threshold_is_reported(
    demo_run: tuple[Any, list[Any]],
) -> None:
    run, _ = demo_run
    block = run.report["run"]["operating_point"]
    assert block["format"] == "mendari.operating-point/1" and block["status"] == op.NO_TARGET
    assert block["targets"] == {"critical_miss": None, "false_automation": None}
    assert block["confidence"] == block["level"] == 0.95
    assert block["recommended"] is None and block["change"] is None and block["walk"] is None
    assert block["guarantee"] is None and block["current_supported"] is None
    assert block["needs"] == [] and block["cannot_fix"] == []
    assert block["summary"] == (
        "At the agent's automation threshold 0.90, 326 of 600 cases are automated: critical "
        "urgent_symptoms=yes automated 9 of 114 critical cases (at most 13.4%), 9 of all 600 "
        "cases (at most 2.7%); false automation 9 of 600 labelled cases (at most 2.7%)."
    )
    assert block["repeated"] == 64
    assert block["lines"] == [
        "64 of the 600 cases repeat another case's input. Each counts as its own evidence here, "
        "so the bounds are narrower than the distinct cases alone would support.",
        'Each "at most" is the exact one-sided bound (Clopper-Pearson) of its count at 95% '
        "confidence. It is about cases like these, labelled as these are, and about this version "
        "and configuration of the agent.",
    ]
    assert json.loads(json.dumps(block)) == block  # plain JSON, as the report keeps it


def test_cases_that_repeat_an_input_are_counted_and_said(
    demo: Pack, demo_run: tuple[Any, list[Any]]
) -> None:
    """A case whose input is another's is not new evidence. The counts stay the report's (each
    case counts), and the block says how many repeat: for the demo set, the 64 its 536 distinct
    inputs leave (`mendari data coverage` lists them)."""
    run, results = demo_run
    dataset = load_dataset(demo.path("data/test.jsonl"), name="test")
    distinct = {json.dumps(record["input"], sort_keys=True) for record in dataset.records}
    assert len(distinct) == 536
    block = run.report["run"]["operating_point"]
    assert block["repeated"] == 600 - len(distinct) == 64
    assert block["current"]["automated"]["n"] == 600  # none is left out of a count
    # No repeat: no sentence. The first 10 cases are all different.
    few = build_evaluation(Decision(demo, origin_cap=False), dataset.head(10), results[:10])
    none = few.report["run"]["operating_point"]
    assert none["repeated"] == 0 and not any("repeat" in line for line in none["lines"])
    # The same case twice is one repeat, whatever else the set holds.
    twice = load_dataset(demo.path("data/test.jsonl"), name="test")
    records = [dataset.records[0], {**dataset.records[0], "id": "again"}, dataset.records[1]]
    doubled = Dataset(twice.name, twice.path, records, twice.sha256, case_key=twice.case_key)
    again = build_evaluation(
        Decision(demo, origin_cap=False), doubled, [results[0], results[0], results[1]]
    )
    said = again.report["run"]["operating_point"]
    assert said["repeated"] == 1
    assert said["lines"][0].startswith("1 of the 3 cases repeats another case's input.")


def test_each_line_says_what_it_is(demo: Pack, demo_run: tuple[Any, list[Any]]) -> None:
    """A block's `kinds` name its `lines` one for one, so a caller can show some of them: the
    demo shows what to do, without the counts it has printed already."""
    run, results = demo_run
    manifest = demo.manifest
    plain = run.report["run"]["operating_point"]
    assert plain["kinds"] == ["repeated", "method"]
    held = op.build(manifest, run.ledger, results, Targets(critical_miss=0.01))
    assert held["kinds"] == ["point", "needs", "cannot_fix", "repeated", "method"]
    recommended = _block(manifest, graded(manifest), Targets(critical_miss=0.02))
    assert recommended["kinds"] == ["point", "point", "walk", "guarantee", "change", "method"]
    lowered = _block(manifest, graded(manifest), Targets(false_automation=0.1))
    assert lowered["kinds"] == ["point", "point", "walk", "unheld", "guarantee", "change", "method"]
    at_093 = op.at_threshold(manifest, 0.93)
    supported = _block(at_093, graded(at_093), Targets(critical_miss=0.02))
    assert supported["kinds"] == ["point", "walk", "guarantee", "method"]
    # Decided under one threshold and read under another: the run is not that manifest's.
    not_its_own = _block(at_093, graded(manifest), Targets(critical_miss=0.02))
    assert not_its_own["status"] == op.NO_REPLAY and not_its_own["kinds"] == ["point", "method"]
    for block in (plain, held, recommended, lowered, supported, not_its_own):
        assert len(block["kinds"]) == len(block["lines"])
        begun = re.findall(r"(?m)^  \S", op.text(block))  # the summary, then each line
        assert len(begun) == 1 + len(block["lines"])
    brief = op.text(held, kinds=op.WHAT_NEXT)
    assert brief.splitlines()[:2] == [
        "Operating point",
        "  No automation threshold can be certified for critical misses at most 1% with 95% "
        "confidence.",
    ]
    assert "needs at least 299 critical cases" in brief and "cannot fix this" in " ".join(
        brief.split()
    )
    assert "At the agent's automation threshold" not in brief and "repeat" not in brief
    assert op.text(held, "One more line.", kinds=()).splitlines()[1:] == [
        "  No automation threshold can be certified for critical misses at most 1% with 95% "
        "confidence.",
        "  One more line.",
    ]


def test_a_bound_is_rounded_up_and_never_reads_as_within_a_target_it_is_above() -> None:
    assert op._at_most(0.134401) == "13.5%" and op._at_most(0.134) == "13.4%"
    assert op._at_most(0.0262) == "2.7%" and op._at_most(0.026) == "2.6%"
    assert op._at_most(0.0100001) == "1.1%" and op._at_most(0.01) == "1.0%"
    assert op._at_most(0.0099692) == "1.00%" and op._at_most(0.00501) == "0.51%"
    assert op._at_most(0.00015) == "0.015%" and op._at_most(1.0) == "100.0%"
    for k, n in [(0, 299), (9, 114), (3, 300), (0, 5000)]:
        bound = op.upper_bound(k, n, 0.95)
        assert float(op._at_most(bound).rstrip("%")) / 100 >= bound  # never under the bound


def test_an_empty_set_and_tiny_sets_say_what_they_cannot_show(demo: Pack) -> None:
    manifest = demo.manifest
    targets = Targets(critical_miss=0.01, false_automation=0.02)
    empty = _block(manifest, [], targets)
    assert empty["status"] == op.NONE_CERTIFIED
    assert empty["current"]["automated"] == {"k": 0, "n": 0}
    assert empty["current"]["false_automation"] == {"k": 0, "n": 0, "upper": None}
    assert [need["cases"] for need in empty["needs"]] == [368, 183]  # each at 97.5%
    assert empty["lines"][1:3] == [
        "Showing at most 1% with 97.5% confidence needs at least 368 critical cases "
        "(urgent_symptoms=yes) with none automated; this set has none.",
        "Showing at most 2% with 97.5% confidence needs at least 183 labelled cases with none "
        "automated with a wrong answer; this set has none.",
    ]
    assert "critical urgent_symptoms=yes: no such case in this set" in empty["summary"] or (
        "no such case in this set" in empty["lines"][0]
    )
    assert _block(manifest, [], Targets())["status"] == op.NO_TARGET
    for size in (1, 2, 5):
        cases = [a_case(manifest, "refill", 0.95, critical=i == 0) for i in range(size)]
        tiny = _block(manifest, cases, targets)
        assert tiny["status"] == op.NONE_CERTIFIED and tiny["recommended"] is None
        assert tiny["current"]["automated"] == {"k": size, "n": size}
        assert op.text(tiny).startswith("Operating point\n  No automation threshold can be")
    # One case, automated and critical: the bound is 100%, and the share seen is above any target.
    alone = _block(manifest, [a_case(manifest, "refill", 0.95, critical=True)], targets)
    assert alone["current"]["critical"][0]["of_critical"] == {"k": 1, "n": 1, "upper": 1.0}
    assert "(at most 100.0%)" in alone["lines"][0]


def test_cases_with_no_label_are_not_counted_as_wrong(demo: Pack) -> None:
    manifest = demo.manifest
    inputs = PolicyInputs(answered("refill", 0.95), {}, None, {})
    result = inputs.apply(manifest)
    unlabelled = [Case(None, (), result.outcome.value, result.recommendation, inputs)] * 50
    found = _current(unlabelled)
    assert (found.automated, found.labelled, found.wrong) == (50, 0, 0)
    block = _block(manifest, unlabelled, Targets(false_automation=0.05))
    assert block["status"] == op.NONE_CERTIFIED  # nothing labelled: nothing shown
    assert "false automation: no labelled case in this set" in block["lines"][0]
    assert block["lines"][1].endswith("this set has none.")


# -- nothing of a case in what is said ------------------------------------------------------------


def _leaks(said: str, dataset: Dataset, ledger: Any) -> list[str]:
    """What of a case is in `said`: its id, its ledger hash, its text or a sentence of it."""
    found = []
    for record, line in zip(dataset.records, ledger.cases, strict=True):
        body = record["input"]["message"]["body"]
        parts = [record["id"], line["c"], body, *re.split(r"(?<=[.?!]) ", body)]
        found += [part for part in parts if len(part) > 8 and part in said]
    return found


def test_no_case_text_and_no_case_id_is_in_any_output(
    demo: Pack, demo_run: tuple[Any, list[Any]]
) -> None:
    """The block, its text and a card's rows hold counts, thresholds and the manifest's own
    names: nothing of a message, no case id and no case hash."""
    run, results = demo_run
    dataset = load_dataset(demo.path("data/test.jsonl"), name="test")
    assert _leaks(dataset.records[0]["input"]["message"]["body"], dataset, run.ledger)  # it finds
    for targets in (
        Targets(),
        Targets(critical_miss=0.01),
        Targets(critical_miss=0.2, false_automation=0.05),
    ):
        block = op.build(demo.manifest, run.ledger, results, targets)
        said = json.dumps(block) + op.text(block) + repr(op.table(block["current"]))
        assert _leaks(said, dataset, run.ledger) == []
        assert "demo-0" not in said and "insulin" not in said and "chest" not in said


# -- the whole way: a run, its report, the command line, the demo and the card -------------------


def a_copy(tmp_path: Path) -> Path:
    """The demo agent's folder, copied: a test may write in it."""
    root = tmp_path / "portal-message-agent"
    shutil.copytree(DEMO_AGENT_DIR, root)
    return root


Row = tuple[str, float, float, str, bool]  # said, confidence, P(urgent), the request, urgent


def an_agent(tmp_path: Path, rows: Sequence[Row]) -> tuple[Path, Dataset]:
    """A copy of the demo agent that answers each case of a set as `rows` say (through its own
    provider, from its canned answers), and that set."""
    root = a_copy(tmp_path)
    canned, lines = [], []
    for number, (said, confidence, urgent, request, critical) in enumerate(rows):
        token = f"case-{number:05d}-"
        flagged = "yes" if urgent >= 0.5 else "no"
        answers = {
            "request": {"value": said, "confidence": confidence},
            "urgent_symptoms": {
                "value": flagged,
                "probabilities": {"yes": urgent, "no": round(1 - urgent, 6)},
            },
        }
        canned.append({"match": token, "response": {"answers": answers}})
        labels = {"request": request, "urgent_symptoms": "yes" if critical else "no"}
        body = f"A synthetic message, {token}."
        lines.append({"id": f"s{number}", "input": {"message": {"body": body}}, "labels": labels})
    mock = {"model": "synthetic", "agent_version": "0", "cases": canned}
    (root / "fixtures" / "mock_agent.json").write_text(json.dumps(mock), encoding="utf-8")
    data = root / "data" / "synthetic.jsonl"
    data.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return root, load_dataset(data)


def graded_rows() -> list[Row]:
    """`graded`'s 600 cases, as canned answers."""
    rows: list[Row] = [("refill", 0.99, 0.9, "refill", True)] * 190
    rows += [("refill", round(0.85 + (i % 8) / 100, 2), 0.05, "other", True) for i in range(10)]
    rows += [
        ("appointment", round(0.93 + (i % 7) / 100, 2), 0.05, "appointment", False)
        for i in range(380)
    ]
    rows += [("refill", 0.70, 0.05, "appointment", False)] * 20
    return rows


def test_a_run_recommends_what_the_policy_then_does(tmp_path: Path) -> None:
    """End to end, through the agent's provider and the policy: the run on the manifest's
    0.90 recommends 0.93 and says what it would automate; with that one line changed, a second
    run does exactly that, case counts and all, and is told its threshold is supported."""
    root, dataset = an_agent(tmp_path, graded_rows())
    targets = Targets(critical_miss=0.02)
    before = evaluation(Decision(load_pack(root), origin_cap=False), dataset, targets=targets)
    block = before.report["run"]["operating_point"]
    assert block["status"] == op.RECOMMENDED and block["change"]["line"] == "auto: 0.93"
    assert block["current"]["automated"] == {"k": 383, "n": 600}
    said = block["recommended"]
    assert said["automated"] == {"k": 380, "n": 600}
    manifest = root / "agent.yaml"
    text = manifest.read_text(encoding="utf-8")
    assert text.count("    auto: 0.90 ") == 1  # the one line, as the manifest writes it
    manifest.write_text(text.replace("    auto: 0.90 ", f"    {block['change']['line']} "))
    after = evaluation(Decision(load_pack(root), origin_cap=False), dataset, targets=targets)
    report = after.report
    assert report["policy"]["thresholds"]["auto"] == 0.93
    assert report["decisions"]["outcome_counts"]["AUTO"] == said["automated"]["k"]
    (critical,) = report["decisions"]["critical"]
    assert critical["auto_missed"] == said["critical"][0]["of_critical"]["k"] == 0
    wrong = report["proportions"]["false_automation"]
    assert (wrong["k"], wrong["n"]) == (said["false_automation"]["k"], 600)
    now = report["run"]["operating_point"]
    assert now["status"] == op.SUPPORTED and now["current"] == {**said, "threshold": 0.93}
    assert now["walk"] == block["walk"]  # the same walk, whichever threshold the manifest has


def test_a_refused_input_and_a_failed_provider_stay_as_they_were(tmp_path: Path) -> None:
    """A run with a case no policy ran on (its input is refused) and one whose provider failed
    (no canned answer matches it): both replay, and neither is automated at any threshold."""
    rows: list[Row] = [("refill", 0.95, 0.05, "refill", False)] * 200
    root, dataset = an_agent(tmp_path, rows)
    extra = [
        {"id": "refused", "input": {"message": {}}, "labels": {"request": "refill"}},
        {"id": "unanswered", "input": {"message": {"body": "No such case."}}, "labels": {}},
    ]
    with dataset.path.open("a", encoding="utf-8") as lines:
        lines.writelines(json.dumps(line) + "\n" for line in extra)
    dataset = load_dataset(dataset.path)
    decision = Decision(load_pack(root), origin_cap=False)
    results = [decision.decide(record["input"]) for record in dataset.records]
    assert [result.outcome.value for result in results[-2:]] == ["BLOCK", "ABSTAIN"]
    assert results[-2].policy_inputs is None and results[-1].policy_inputs is not None
    run = build_evaluation(decision, dataset, results, targets=Targets(false_automation=0.05))
    cases = op.cases_of(run.ledger, results)
    assert all(case.replays for case in cases)
    for threshold in (1.0, 0.95, 0.5):
        outcomes = outcomes_at(decision.pack.manifest, cases, threshold)
        assert outcomes[-2:] == ["BLOCK", "ABSTAIN"]
        assert outcomes[:200] == ["AUTO" if threshold <= 0.95 else "HUMAN_REVIEW"] * 200
    block = run.report["run"]["operating_point"]
    assert block["status"] == op.SUPPORTED  # 0.90 automates the 200 at 0.95, as 0.95 does
    assert block["recommended"]["automated"] == {"k": 200, "n": 202}
    assert block["recommended"]["false_automation"] == {  # 201 labelled: the refused one is
        "k": 0,
        "n": 201,
        "upper": round(st.beta.ppf(0.95, 1, 201), 6),
    }
    assert block["walk"]["certified"] == 51  # down to the review threshold: nothing else changes


def test_the_block_is_in_the_run_and_the_result_is_as_it_was(
    demo: Pack, demo_run: tuple[Any, list[Any]]
) -> None:
    """The result (`mendari.eval/2`, what `result_sha256` covers) and the ledger are the same
    bytes whatever the targets, and the same as without the block: the operating point is in
    the report's `run`, which no hash and no signed record reads."""
    plain, _ = demo_run
    dataset = load_dataset(demo.path("data/test.jsonl"), name="test")
    decision = Decision(demo, origin_cap=False)
    held = evaluation(decision, dataset, targets=Targets(critical_miss=0.01, confidence=0.9))
    assert held.report["result_sha256"] == plain.report["result_sha256"]
    assert result_of(held.report) == result_of(plain.report)
    assert held.ledger.dumps() == plain.ledger.dumps()
    assert held.report["format"] == plain.report["format"] == "mendari.eval/2"
    for report in (plain.report, held.report):
        assert "operating_point" not in result_of(report) and "operating_point" not in report
        assert report["run"]["operating_point"]["format"] == "mendari.operating-point/1"
    assert plain.report["run"]["operating_point"]["status"] == op.NO_TARGET
    assert held.report["run"]["operating_point"]["status"] == op.NONE_CERTIFIED
    assert held.report["run"]["operating_point"]["confidence"] == 0.9
    # The hash is the result's alone, as it was before a run kept anything else.
    without = {key: value for key, value in held.report.items() if key != "run"}
    digest = hashlib.sha256(json.dumps(result_of(without), sort_keys=True).encode()).hexdigest()
    again = {key: value for key, value in plain.report.items() if key != "run"}
    assert (
        digest == hashlib.sha256(json.dumps(result_of(again), sort_keys=True).encode()).hexdigest()
    )


def test_a_target_the_agent_cannot_be_held_to_is_refused_before_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = a_copy(tmp_path)
    manifest = root / "agent.yaml"
    text = manifest.read_text(encoding="utf-8")
    critical = '  critical:\n    - {question: urgent_symptoms, label: "yes"}\n'
    assert critical in text
    manifest.write_text(text.replace(critical, ""), encoding="utf-8")
    pack = load_pack(root)
    dataset = load_dataset(pack.path("data/test.jsonl"), name="test")
    decision = Decision(pack, origin_cap=False)
    asked: list[Any] = []
    monkeypatch.setattr(decision, "decide", asked.append)  # no case is decided
    with pytest.raises(MendariError, match="declares no critical label"):
        evaluation(decision, dataset, targets=Targets(critical_miss=0.01))
    assert asked == []


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_mendari_eval_prints_the_operating_point(capsys: pytest.CaptureFixture[str]) -> None:
    agent = ("eval", str(DEMO_AGENT_DIR), "--dataset", "test", "--baseline", "none")
    code, out, err = run_cli(capsys, *agent)
    assert code == 0 and err == ""
    report, section = out.split("\n\nOperating point\n")
    assert report.splitlines()[-1].startswith("Cost ")  # after the report, under its own heading
    assert section.startswith(
        "  At the agent's automation threshold 0.90, 326 of 600 cases are automated: critical\n"
        "    urgent_symptoms=yes automated 9 of 114 critical cases (at most 13.4%), 9 of all 600 "
        "cases (at\n    most 2.7%); false automation 9 of 600 labelled cases (at most 2.7%).\n"
    )
    assert " ".join(section.split()).endswith(
        "No target was given. Add --target-critical-miss or --target-false-automation (for "
        "example --target-critical-miss 1%) for the lowest automation threshold these cases "
        "support."
    )
    assert max(map(len, section.splitlines())) <= 100
    # The same counts as the report's own rows, two lines up.
    assert "False automation          9 of 600 [0.7-2.8%] of labelled cases" in report
    assert "Critical urgent_symptoms=yes 81 of 114 [61.8-79.2%] detected, 9 automated" in report


def test_mendari_eval_takes_the_targets_and_the_level(capsys: pytest.CaptureFixture[str]) -> None:
    agent = ("eval", str(DEMO_AGENT_DIR), "--dataset", "test", "--baseline", "none")
    code, out, _ = run_cli(capsys, *agent, "--target-critical-miss", "1%")
    assert code == 0  # advice: the exit code is the evaluation's
    said = " ".join(out.split("\n\nOperating point\n")[1].split())
    assert said.startswith(
        "No automation threshold can be certified for critical misses at most 1% with 95% "
        "confidence."
    )
    assert (
        "Showing at most 1% with 95% confidence needs at least 299 critical cases "
        "(urgent_symptoms=yes) with none automated; this set has 114." in said
    )
    assert "The threshold cannot fix this: at 0.95, the strictest threshold" in said
    assert "No target was given" not in said
    code, out, _ = run_cli(
        capsys,
        *agent,
        "--json",
        *("--target-critical-miss", "0.2", "--target-critical-miss-of", "all"),
        *("--target-false-automation", "5%", "--target-confidence", "90%"),
    )
    block = json.loads(out)["run"]["operating_point"]
    assert code == 0 and block["status"] == op.SUPPORTED
    assert block["targets"] == {
        "critical_miss": {"at_most": 0.2, "of": "all"},
        "false_automation": {"at_most": 0.05},
    }
    assert block["confidence"] == 0.9 and block["level"] == pytest.approx(0.95)
    assert block["guarantee"].startswith("With 90% confidence, at the automation threshold 0.90 ")


@pytest.mark.parametrize(
    ("options", "said"),
    [
        (
            ["--target-critical-miss", "1"],
            "--target-critical-miss: a share is above 0 and below 100%: write 1% or 0.01: '1'",
        ),
        (
            ["--target-false-automation", "two"],
            "--target-false-automation: a share is written 1% or 0.01: 'two'",
        ),
        (
            ["--target-false-automation", "0.00000001"],
            "--target-false-automation: a target is a share",
        ),
        (["--target-confidence", "30%"], "--target-confidence: the confidence level is from 50%"),
        (["--target-confidence", "95"], "--target-confidence: a share is above 0 and below 100%"),
        (["--target-critical-miss-of", "some"], "--target-critical-miss-of: invalid choice"),
    ],
)
def test_an_option_that_is_not_a_share_is_one_line_that_names_it(
    options: list[str], said: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refused by the command line before any work, as a limit's value is: one line that names
    the option, and where its help is."""
    code, out, err = run_cli(capsys, "eval", str(DEMO_AGENT_DIR), "--dataset", "test", *options)
    assert code == 1 and out == ""
    (line,) = err.splitlines()
    assert line.startswith(f"error: mendari eval: argument {said}"), line
    assert line.endswith("(see mendari eval --help)")


def test_what_a_target_is_a_share_of_needs_the_target(capsys: pytest.CaptureFixture[str]) -> None:
    agent = ("eval", str(DEMO_AGENT_DIR), "--dataset", "test")
    code, out, err = run_cli(capsys, *agent, "--target-critical-miss-of", "all")
    assert code == 1 and out == ""
    assert err.strip() == (
        "error: --target-critical-miss-of says what --target-critical-miss is a share of: give "
        "that target too"
    )


def test_the_block_as_markdown_is_its_own_sentences(demo_run: tuple[Any, list[Any]]) -> None:
    """For a CI job's summary: the heading, the summary in bold, each line as a list item. The
    sentences are the ones the terminal prints, and nothing is added."""
    run, _ = demo_run
    block = run.report["run"]["operating_point"]
    markdown = op.markdown(block)
    assert markdown == (
        f"### Operating point\n\n**{block['summary']}**\n\n"
        + "".join(f"- {line}\n" for line in block["lines"])
    )
    printed = " ".join(op.text(block).split())
    assert all(line in printed for line in (block["summary"], *block["lines"]))
    # A block with no line under its summary is its heading and its summary.
    assert op.markdown({"summary": "Nothing to say.", "lines": []}) == (
        "### Operating point\n\n**Nothing to say.**\n"
    )


def test_mendari_eval_writes_the_operating_point_into_the_markdown_summary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`mendari eval --markdown` is what a CI job's summary is made of (action.yml): the
    operating point comes after the figures, with no target and with one, and the exit code is
    the evaluation's either way."""
    agent = ("eval", str(DEMO_AGENT_DIR), "--dataset", "test", "--baseline", "none", "--markdown")
    code, out, err = run_cli(capsys, *agent)
    assert code == 0 and err == ""
    figures, heading, section = out.partition("\n### Operating point\n")
    assert figures.startswith("### Evaluation of `mendari/portal-message-agent`") and heading
    assert section.startswith(
        "\n**At the agent's automation threshold 0.90, 326 of 600 cases are automated: critical "
        "urgent_symptoms=yes automated 9 of 114 critical cases (at most 13.4%), 9 of all 600 "
        "cases (at most 2.7%); false automation 9 of 600 labelled cases (at most 2.7%).**\n\n"
        "- 64 of the 600 cases repeat another case's input."
    )
    # The terminal's hint names options of the command: a summary is a record, and has none.
    assert "No target was given" not in out and "--target" not in out
    code, out, _ = run_cli(capsys, *agent, "--target-critical-miss", "1%")
    assert code == 0  # advice: no target fails a run
    assert (
        "\n### Operating point\n\n**No automation threshold can be certified for critical misses "
        "at most 1% with 95% confidence.**\n\n- At the agent's automation threshold 0.90, "
    ) in out
    assert "\n- The threshold cannot fix this: at 0.95, the strictest threshold" in out
    # Beside limits the run does not meet: the summary holds both, each under its heading, and
    # the exit code is the limits'.
    code, out, _ = run_cli(
        capsys, *agent, "--target-critical-miss", "1%", "--max-critical-misses", "0"
    )
    assert code == 2
    assert out.index("\n### Operating point\n") < out.index("\n### Gate failed: 1 limit not met\n")
    assert (
        "| critical urgent_symptoms=yes misses | at most 0 | 9 of 114 [3.7-14.5%] | **not met** |"
        in out
    )


def test_nothing_edits_the_manifest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A recommendation is advice: the manifest is the same bytes after a run that recommends
    another threshold, and the one line to change is printed."""
    root, dataset = an_agent(tmp_path, graded_rows())
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    out_file = tmp_path / "out" / "report.json"
    code, out, _ = run_cli(
        capsys,
        *("eval", str(root), "--dataset", str(dataset.path), "--baseline", "none"),
        *("--target-critical-miss", "2%", "--out", str(out_file)),
    )
    assert code == 0
    assert "`auto: 0.93` (now 0.90). Nothing changes it for you." in " ".join(out.split())
    assert {path: path.read_bytes() for path in root.rglob("*") if path.is_file()} == before
    written = json.loads(out_file.read_text(encoding="utf-8"))
    assert written["run"]["operating_point"]["change"]["line"] == "auto: 0.93"


def test_the_demos_verdict_is_its_operating_point(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MENDARI_AGENTS_PATH", str(ROOT / "agents"))  # as a release finds it
    run_demo()
    out = capsys.readouterr().out
    section = out[out.index("\nOperating point\n") : out.index("\nVerdict: ")]
    said = " ".join(section.split())
    assert said.startswith(
        "Operating point No automation threshold can be certified for critical misses at most "
        "1% with 95% confidence."
    )
    assert "needs at least 299 critical cases (urgent_symptoms=yes) with none automated" in said
    assert (
        "\nVerdict: not ready. The agent missed 33 urgent messages, and 9 of them would have gone "
        "out with no person reading them. No automation threshold fixes that: fix the agent, or "
        "add a rule or a second signal, and evaluate again before it decides anything alone.\n"
    ) in out


def test_the_demos_verdict_follows_the_status() -> None:
    from mendari.sandbox.demo import _verdict

    guarantee = "With 95% confidence, at the automation threshold 0.93 critical misses stay low."
    change = {"from": 0.9, "to": 0.93}
    held = {"status": op.SUPPORTED, "guarantee": guarantee}
    assert _verdict(held, 0, 0) == (
        "its automation threshold holds on this set. Every urgent message in this set was "
        f"caught. {guarantee}"
    )
    other = {"status": op.RECOMMENDED, "change": change, "current_supported": False}
    assert _verdict(other, 4, 2) == (
        "not ready at its automation threshold 0.90. The agent missed 4 urgent messages, and 2 "
        "of them would have gone out with no person reading them. These cases support 0.93: "
        "change that one line of its manifest, and evaluate again before it decides anything "
        "alone."
    )
    more = {**other, "current_supported": True, "guarantee": guarantee}
    assert _verdict(more, 3, 0).startswith(
        "its automation threshold holds on this set, and these cases support 0.93 too, which "
        "automates more. The agent missed 3 urgent messages, and a person sees each of them. "
    )
    small = {"status": op.NONE_CERTIFIED, "cannot_fix": []}
    assert _verdict(small, 0, 0).startswith(
        "not ready. Every urgent message in this set was caught. No automation threshold can be "
        "certified on this set: fix the agent or add cases, and evaluate again"
    )
    none = {"status": op.NOTHING_AUTOMATED, "summary": "The policy automates no case."}
    assert _verdict(none, 0, 0).endswith("The policy automates no case.")


def with_a_benchmark(root: Path, report: dict[str, Any]) -> Pack:
    """The agent at `root`, with `report` as its published benchmark."""
    manifest = root / "agent.yaml"
    text = manifest.read_text(encoding="utf-8")
    if "\n  benchmark: " not in text:
        declared = "  benchmark_dataset: test\n"
        assert declared in text
        manifest.write_text(
            text.replace(declared, declared + "  benchmark: evals/benchmark.json\n")
        )
    (root / "evals").mkdir(exist_ok=True)
    (root / "evals" / "benchmark.json").write_text(json.dumps(report), encoding="utf-8")
    return load_pack(root)


def test_the_card_shows_the_bound_at_the_benchmarks_threshold(
    tmp_path: Path, demo_run: tuple[Any, list[Any]]
) -> None:
    """Under the benchmark's figures, each risk at the threshold it ran with, as k of n with
    its bound; a benchmark written before a run worked that out leaves the card as it was."""
    run, _ = demo_run
    root = a_copy(tmp_path)
    card = render_card(with_a_benchmark(root, run.report)).splitlines()
    start = card.index("| At this threshold | Cases | Bound |")
    assert card[start - 2] == (
        "Operating point, at the automation threshold **0.90** this benchmark ran with. Each "
        '"at most" is an exact one-sided bound (Clopper-Pearson) at 95% confidence, for cases '
        "like the benchmark's:"
    )
    assert card[start : start + 6] == [
        "| At this threshold | Cases | Bound |",
        "|---|---|---|",
        "| Automated | 326 of 600 | |",
        "| Critical `urgent_symptoms=yes` automated, of its cases | 9 of 114 | at most 13.4% |",
        "| Critical `urgent_symptoms=yes` automated, of all cases | 9 of 600 | at most 2.7% |",
        "| False automation, of labelled cases | 9 of 600 | at most 2.7% |",
    ]
    # The card's rule: a row with a percentage is k of n.
    assert all(" of " in line for line in card[start + 2 : start + 6] if "%" in line)
    earlier = {
        **run.report,
        "run": {k: v for k, v in run.report["run"].items() if k != "operating_point"},
    }
    as_it_was = render_card(with_a_benchmark(root, earlier)).splitlines()
    assert "| At this threshold | Cases | Bound |" not in as_it_was
    assert as_it_was == card[: start - 3] + card[start + 6 :]


def test_the_card_works_its_figures_out_and_reads_none_from_the_runs_block(
    tmp_path: Path, demo_run: tuple[Any, list[Any]]
) -> None:
    """The run's block is not part of the result, and nothing checks it against a fresh run: a
    card shows what it works out from the result's own counts, whatever the block says, and
    never the threshold the block recommends."""
    run, results = demo_run
    block = op.build(
        load_pack(DEMO_AGENT_DIR).manifest, run.ledger, results, Targets(critical_miss=0.2)
    )
    forged = json.loads(json.dumps(block))
    forged["current"]["critical"][0]["of_critical"] = {"k": 0, "n": 114, "upper": 0.001}
    forged["summary"] = "Recommended automation threshold 0.50: nothing is ever missed."
    forged["recommended"]["threshold"] = 0.5
    report = {**run.report, "run": {**run.report["run"], "operating_point": forged}}
    card = render_card(with_a_benchmark(a_copy(tmp_path), report))
    assert (
        "| Critical `urgent_symptoms=yes` automated, of its cases | 9 of 114 | at most 13.4% |"
        in card
    )
    assert (
        "nothing is ever missed" not in card
        and "0.50" not in card.split("Operating point")[1][:600]
    )
    # A block that is not one, and counts that are not counts, show no section and fail no card.
    for odd in ("yes", ["a list"], 3):
        odd_report = {**run.report, "run": {**run.report["run"], "operating_point": odd}}
        assert "At this threshold" not in render_card(
            with_a_benchmark(a_copy(tmp_path / str(odd)), odd_report)
        )
    broken = json.loads(json.dumps(run.report))
    broken["decisions"]["critical"][0]["n"] = 3  # fewer cases than it says were automated
    shown = render_card(with_a_benchmark(a_copy(tmp_path / "broken"), broken))
    assert "At this threshold" not in shown and "## Validation" in shown


# -- the guide ------------------------------------------------------------------------------------


def _table(text: str, heading: str) -> list[list[str]]:
    """The cells of the first table of `text` whose header starts with `heading`, row by row,
    without the header."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"| {heading} |"))
    rows = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows


def test_the_guides_numbers_of_cases_are_the_ones_worked_out() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    targets = ["at most 5%", "at most 2%", "at most 1%", "at most 0.5%", "at most 0.1%"]
    by_level = _table(text, "Target")
    assert [row[0] for row in by_level] == targets
    for row in by_level:  # 90%, 95%, 95% with two targets (each at 97.5%), 99%
        target = op.share(row[0].removeprefix("at most "))
        needed = [op.cases_needed(target, level) for level in (0.9, 0.95, 0.975, 0.99)]
        assert [int(cell) for cell in row[1:]] == needed
    by_count = _table(text[text.index("Every case that does have the risk") :], "Target")
    assert [row[0] for row in by_count] == targets
    for row in by_count:  # at 95%, with none, 1, 2 and 5 cases that have the risk
        target = op.share(row[0].removeprefix("at most "))
        needed = [op.cases_needed(target, 0.95, k) for k in (0, 1, 2, 5)]
        assert [int(cell) for cell in row[1:]] == needed


def test_the_guide_shows_what_the_command_prints(
    demo: Pack, demo_run: tuple[Any, list[Any]]
) -> None:
    """Each example in the guide is the tool's own output, word for word: the demo agent's, and
    the graded cases'."""
    text = GUIDE.read_text(encoding="utf-8")
    run, results = demo_run
    assert op.text(run.report["run"]["operating_point"]) in text
    recommended = _block(demo.manifest, graded(demo.manifest), Targets(critical_miss=0.02))
    assert op.text(recommended) in text
    flat = " ".join(text.split())
    assert recommended["summary"] in flat  # the page's opening example
    held = op.build(demo.manifest, run.ledger, results, Targets(critical_miss=0.01))
    for line in held["lines"][1:3]:  # too few cases, and what no threshold fixes
        assert line in flat, line


def test_the_guide_names_the_options_the_command_takes() -> None:
    from mendari.cli import build_parser

    text = GUIDE.read_text(encoding="utf-8")
    assert set(re.findall(r"`(--[a-z-]+)", text)) == {
        "--target-critical-miss",
        "--target-critical-miss-of",
        "--target-false-automation",
        "--target-confidence",
    }
    parsed = build_parser().parse_args(
        [
            *("eval", "an-agent", "--dataset", "d", "--target-critical-miss", "1%"),
            *("--target-critical-miss-of", "all", "--target-false-automation", "2%"),
            *("--target-confidence", "95%"),
        ]
    )
    assert (parsed.target_critical_miss, parsed.target_critical_miss_of) == (0.01, "all")
    assert (parsed.target_false_automation, parsed.target_confidence) == (0.02, 0.95)
    assert not re.search(r"\bpacks?\b", text, re.IGNORECASE)  # one vocabulary: an agent
