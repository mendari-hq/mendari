# SPDX-License-Identifier: Apache-2.0
"""Grades and band ranking (LP-17j, `evals/grade.py` and `evals/bands.py`): a grade is a set of
attestations, each met, not met or insufficient evidence, never a letter or a score; ranking
orders arms on a band tuple (a total preorder, equal tuples sharing a competition rank), lists
an arm that fails a safety attestation or lacks its evidence unranked, keeps ECE and latency out
of the order, and sets rank intervals from a stated Holm family that never reorders the ranks.
Both are given only on frozen hidden sets: on a public set the ranking is off and the grade is
null.

A public synthetic set stands in for a frozen hidden set here: the referral router's `test`
cases with a critical positive (113 distinct urgent referrals and 124 needing urgent review,
every one detected by the deterministic arm) and the next 100 others, 224 cases, with its
entries' set kind changed to `hidden`, as LP-17k's grading read will make them. The other arms
are the deterministic run's ledger with its configuration changed and some answers edited, so
every figure that differs is known by construction."""

from __future__ import annotations

import copy
import dataclasses
import itertools
import math
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from scipy.stats import beta
from statsmodels.stats.contingency_tables import mcnemar
from statsmodels.stats.multitest import multipletests

from mendari.errors import MendariError
from mendari.evals import arms, bands, grade, method, scoreboard
from mendari.evals.ledger import Ledger, canonical
from mendari.evals.render import scoreboard_markdown
from mendari.evals.result import CONFIDENCE, PREDICTED, PROBABILITIES, TRUTH
from tests.test_scoreboard import (
    INBOX,
    REFERRAL,
    Built,
    build,
    edited,
    records_of,
    run_for,
    written,
)

DETERMINISTIC = {
    "specialty_reader": "naive_bayes",
    "triage_screen": "rules",
    "checklist": "criteria",
}
CLASSIFIER = {"specialty_reader": "jev", "triage_screen": "rules", "checklist": "criteria"}
LLM = {"specialty_reader": "llm", "triage_screen": "rules", "checklist": "criteria"}
BOTH = {"specialty_reader": "jev", "triage_screen": "rules_and_llm", "checklist": "criteria"}
INBOX_CLASSIFIER = {"safety_screen": "rules", "router": "jev"}
WORSE = 20  # the classifier's wrong next steps
GENERATED = settings(
    derandomize=True,
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

# The figures past detection of an entry that only its critical labels set apart.
REST = {
    "false_automation": {"k": 0, "n": 600},
    "accuracy": {"k": 590, "n": 600},
    "brier": {"high": 0.04},
    "cost_per_1000": {"status": "none"},
}

Arm = tuple[dict[str, Any], Ledger]
Edit = Callable[[list[dict[str, Any]]], None]


def hidden(entry: dict[str, Any]) -> dict[str, Any]:
    """`entry` as a frozen hidden set's would be (LP-17k): the same figures, of a hidden set."""
    found = copy.deepcopy(entry)
    found["scope"]["set"]["kind"] = "hidden"
    return found


def arm(base: Built, providers: dict[str, str], edit: Edit | None = None) -> Arm:
    """The entry (of a hidden set) and ledger of `providers` on `base`'s cases: `base`'s ledger
    with its configuration changed and `edit` applied to its case lines."""
    (configuration,) = [c for c in arms.configurations(base.pack) if c.providers == providers]

    def header(found: dict[str, Any]) -> None:
        for slot, variant in providers.items():
            found["providers"][slot]["variant"] = variant

    ledger = edited(base.ledger, edit, header=header)
    found = base.entry(
        ledger=ledger,
        run=run_for(ledger, base.run),
        arm=configuration.arm,
        configuration=configuration,
    )
    return hidden(found), ledger


def worse_accuracy(cases: list[dict[str, Any]]) -> None:
    """20 cases whose next step is to schedule, answered "coordinator_review" for sure: accuracy
    falls by 20 and Brier rises, and no critical positive changes."""
    changed = 0
    for case in cases:
        answered = case["q"]["next_step"]
        right = answered[TRUTH] == answered[PREDICTED] == "schedule"
        if changed < WORSE and right and not case["k"]:
            answered[PREDICTED] = "coordinator_review"
            answered[CONFIDENCE] = 1.0
            answered[PROBABILITIES] = [0.0, 1.0, 0.0, 0.0]  # urgent_review, coordinator_review, ...
            changed += 1
    assert changed == WORSE


def first_urgent(cases: list[dict[str, Any]]) -> dict[str, Any]:
    return next(case for case in cases if 0 in case["k"])


def missed_urgent(cases: list[dict[str, Any]]) -> None:
    """One `urgency=urgent` positive answered routine: 112 of 113 distinct groups detected."""
    first_urgent(cases)["q"]["urgency"][PREDICTED] = "routine"


def automated_urgent(cases: list[dict[str, Any]]) -> None:
    """One `urgency=urgent` positive decided AUTO, its recommendation right."""
    first_urgent(cases)["o"] = "AUTO"


def wrong_auto(cases: list[dict[str, Any]]) -> None:
    """Three routine cases decided AUTO with a wrong recommendation: false automation 3 of 224."""
    chosen = [c for c in cases if not c["k"] and c["q"]["next_step"][TRUTH] == "schedule"][:3]
    for case in chosen:
        case["o"], case["r"] = "AUTO", "return_for_info"


def configured(providers: dict[str, str]) -> str:
    return ",".join(f"{slot}={providers[slot]}" for slot in sorted(providers))


@pytest.fixture(scope="module")
def referral(tmp_path_factory: pytest.TempPathFactory) -> Built:
    """The deterministic arm's run on the 224 cases."""
    records = records_of(REFERRAL, "test")

    def positive(record: dict[str, Any]) -> bool:
        labels = record["labels"]
        return labels.get("urgency") == "urgent" or labels.get("next_step") == "urgent_review"

    others = {r["id"] for r in [r for r in records if not positive(r)][:100]}
    kept = [r for r in records if positive(r) or r["id"] in others]
    assert len(kept) == 224
    folder: Path = tmp_path_factory.mktemp("referral")
    return build(REFERRAL, written(folder, "referral_hidden", kept), DETERMINISTIC)


@pytest.fixture(scope="module")
def graded(referral: Built) -> dict[str, Arm]:
    """The arms the tests rank, by name."""
    return {
        "deterministic": (hidden(referral.entry()), referral.ledger),
        "both": arm(referral, BOTH),  # the deterministic arm's answers: an equal tuple
        "classifier": arm(referral, CLASSIFIER, worse_accuracy),
        "llm missed": arm(referral, LLM, missed_urgent),
        "llm automated": arm(referral, LLM, automated_urgent),
        "llm wrong auto": arm(referral, LLM, wrong_auto),
    }


def rank(referral: Built, chosen: list[Arm]) -> dict[str, Any]:
    return bands.ranking(
        [entry for entry, _ in chosen],
        ledgers=[ledger for _, ledger in chosen],
        records=referral.dataset.records,
    )


FOUR = ("deterministic", "classifier", "both", "llm missed")


@pytest.fixture(scope="module")
def ranked(referral: Built, graded: dict[str, Arm]) -> dict[str, Any]:
    """Four arms: two with equal tuples, one with a worse accuracy band, one that missed an
    urgent referral."""
    return rank(referral, [graded[name] for name in FOUR])


@pytest.fixture(scope="module")
def holdout() -> Built:
    """The inbox's red-flag holdout: 20 urgent positives in 30 cases, run locally."""
    return build(INBOX, "red_flags_holdout")


# --------------------------------------------------------------------------------------------
# The order
# --------------------------------------------------------------------------------------------

LEVELS = {labels: method.simultaneous_level(labels) for labels in range(1, 7)}


@st.composite
def generated_entry(draw: st.DrawFn) -> dict[str, Any]:
    """An entry's figures as far as the band tuple reads them, each of any size and value."""

    def counted() -> dict[str, int]:
        n = draw(st.integers(0, 700))
        return {"k": draw(st.integers(0, n)), "n": n}

    labels = draw(st.integers(1, 6))
    critical = []
    for index in range(labels):
        found = counted()
        critical.append(
            {
                "question": f"q{index}",
                "label": "positive",
                "distinct": {"positives": found["n"], "detected": found["k"]},
                "bound": {"level": LEVELS[labels]},
            }
        )
    brier = draw(st.one_of(st.none(), st.floats(0, 2).map(lambda v: {"high": round(v, 6)})))
    cost = draw(
        st.one_of(
            st.just({"status": "none"}),
            st.just({"status": "unknown", "reason": "a provider call failed"}),
            st.floats(0, 300).map(lambda v: {"status": "priced", "estimate": round(v, 6)}),
        )
    )
    return {
        "figures": {
            "critical": critical,
            "false_automation": counted(),
            "accuracy": counted(),
            "brier": brier,
            "cost_per_1000": cost,
        },
    }


@GENERATED
@given(st.lists(generated_entry(), min_size=3, max_size=6), st.randoms(use_true_random=False))
def test_the_band_order_is_a_total_preorder_on_generated_arms(
    entries: list[dict[str, Any]], shuffle: Any
) -> None:
    keys = [bands.key_of(entry) for entry in entries]
    assert keys == [bands.key_of(copy.deepcopy(entry)) for entry in entries]  # a function
    ordered = [bands.order_key(key) for key in keys]
    count = range(len(entries))

    def at_least_as_good(i: int, j: int) -> bool:
        return ordered[i] <= ordered[j]

    for a, b, c in itertools.product(count, repeat=3):
        assert at_least_as_good(a, a)  # reflexive
        assert at_least_as_good(a, b) or at_least_as_good(b, a)  # total
        if at_least_as_good(a, b) and at_least_as_good(b, c):
            assert at_least_as_good(a, c)  # transitive
    # Competition ranks follow the preorder: shared exactly by equal tuples, smaller exactly for
    # a strictly better tuple, and the same whatever order the arms come in.
    ranks = bands.competition_ranks(keys)
    for i, j in itertools.product(count, repeat=2):
        strictly = at_least_as_good(i, j) and not at_least_as_good(j, i)
        assert (ranks[i] == ranks[j]) == (keys[i] == keys[j])
        assert (ranks[i] < ranks[j]) == strictly
    order = list(count)
    shuffle.shuffle(order)
    assert bands.competition_ranks([keys[i] for i in order]) == [ranks[i] for i in order]
    # A better figure never makes a worse band, on any criterion.
    for entry, key in zip(entries, keys, strict=True):
        better = copy.deepcopy(entry)
        figures = better["figures"]
        for item in figures["critical"]:
            distinct = item["distinct"]
            distinct["detected"] = min(distinct["positives"], distinct["detected"] + 1)
        figures["false_automation"]["k"] = max(0, figures["false_automation"]["k"] - 1)
        figures["accuracy"]["k"] = min(figures["accuracy"]["n"], figures["accuracy"]["k"] + 1)
        for mine, theirs in zip(
            bands.order_key(bands.key_of(better)), bands.order_key(key), strict=True
        ):
            assert mine <= theirs


def test_a_band_is_the_first_edge_reached_read_as_the_decimal_written() -> None:
    recall, fa = method.RECALL_BANDS, method.FALSE_AUTOMATION_BANDS
    found = [bands.edge_band(v, recall, higher_is_better=True) for v in (1.0, 0.99, 0.97)]
    assert found == [0, 0, 2]
    assert bands.edge_band(0.969999, recall, higher_is_better=True) == 3
    assert bands.edge_band(0.95, recall, higher_is_better=True) == 3
    assert bands.edge_band(0.8999, recall, higher_is_better=True) == len(recall)
    assert bands.edge_band(0.0, fa, higher_is_better=False) == 0
    assert bands.edge_band(0.02, fa, higher_is_better=False) == 2
    assert bands.edge_band(0.020001, fa, higher_is_better=False) == 3
    assert bands.edge_band(0.2, fa, higher_is_better=False) == len(fa)
    # 0.95 as a binary float is just under 19/20, and 0.95 / 0.05 is 18.999999999999996 in
    # floating point: the band is still 0.95's, as written.
    assert 0.95 / 0.05 < 19
    assert bands.step_band(0.95, 0.05, higher_is_better=True) == 1
    assert bands.step_band(0.949999, 0.05, higher_is_better=True) == 2
    assert bands.step_band(1.0, 0.05, higher_is_better=True) == 0
    assert bands.step_band(0.05, 0.05, higher_is_better=False) == 1
    assert bands.step_band(0.050001, 0.05, higher_is_better=False) == 2
    assert bands.step_band(0.0, 0.05, higher_is_better=False) == 0

    # An unknown cost is in the last band, with a cost over every edge.
    def cost(found: dict[str, Any]) -> dict[str, Any]:
        figures = {**REST, "critical": [], "cost_per_1000": found}
        return bands.bands_of({"figures": figures})["cost_per_1000"]

    over = cost({"status": "priced", "estimate": 250.0})
    unknown = cost({"status": "unknown", "reason": "a provider call failed"})
    assert over["position"] == unknown["position"] == len(method.COST_BANDS_USD_PER_1000)
    assert (over["band"], unknown["band"]) == ("above 100 USD", "unknown: the last band")
    assert cost({"status": "priced", "estimate": 1.0})["band"] == "at or below 1 USD"


def test_equal_band_tuples_share_a_rank(ranked: dict[str, Any], graded: dict[str, Arm]) -> None:
    assert ranked["status"] == "on" and ranked["graded"] == 4 and ranked["draft"]
    listing = [(item["rank"], item["arm"], item["configuration"]) for item in ranked["ranked"]]
    assert listing == [
        (1, "classifier_and_llm", configured(BOTH)),  # by configuration: jev, then naive_bayes
        (1, "deterministic", configured(DETERMINISTIC)),
        (3, "classifier", configured(CLASSIFIER)),  # a competition rank: 1, 1, 3
    ]
    both, deterministic, classifier = ranked["ranked"]
    assert both["key"] == deterministic["key"]
    assert both["bands"] == deterministic["bands"]
    found = deterministic["bands"]
    # The worst label is urgency, 113 of 113 distinct groups at 97.5%, below next_step's 124 of
    # 124; false automation is 0 of the 224 labelled cases; each bound as scipy has it.
    assert found["detection"] == {
        "question": "urgency",
        "label": "urgent",
        "value": round(beta.ppf(0.0125, 113, 1), 6),
        "position": 3,
        "band": "at or above 0.95",
    }
    assert found["false_automation"] == {
        "value": round(beta.ppf(0.975, 1, 224), 6),
        "position": 2,
        "band": "at or below 0.02",
    }
    figures = graded["deterministic"][0]["figures"]
    k, n = figures["accuracy"]["k"], figures["accuracy"]["n"]
    assert found["accuracy"]["value"] == round(beta.ppf(0.025, k, n - k + 1), 6)
    assert found["accuracy"]["band"] == "at or above 0.95"
    assert found["brier"]["value"] == figures["brier"]["high"]
    assert found["brier"]["band"] == "at or below 0.05"
    assert found["cost_per_1000"] == {"value": 0.0, "position": 0, "band": "at or below 0 USD"}
    assert both["key"] == [3, 2, 1, 1, 0]
    # The classifier's 20 wrong next steps: the same detection and false automation, then a
    # worse accuracy band, the first criterion where the tuples differ (Brier is worse too).
    assert classifier["key"] == [3, 2, 3, 6, 0]
    assert classifier["bands"]["accuracy"]["band"] == "at or above 0.85"
    assert bands.competition_ranks([(0,), (1,), (1,), (2,)]) == [1, 2, 2, 4]


def test_an_arm_failing_a_safety_floor_or_short_of_evidence_is_listed_unranked(
    referral: Built, graded: dict[str, Arm], ranked: dict[str, Any], holdout: Built
) -> None:
    assert ranked["unranked"] == [
        {
            "arm": "llm",
            "configuration": configured(LLM),
            "reasons": [
                {
                    "attestation": "recall_floor",
                    "question": "urgency",
                    "label": "urgent",
                    "state": "not met",  # 112 of 113: lower bound 0.9449 at 97.5%
                }
            ],
        }
    ]
    automated = rank(referral, [graded["deterministic"], graded["llm automated"]])
    assert automated["unranked"] == [
        {
            "arm": "llm",
            "configuration": configured(LLM),
            "reasons": [{"attestation": "no_critical_automated", "state": "not met"}],
        }
    ]
    assert [item["arm"] for item in automated["ranked"]] == ["deterministic"]
    assert automated["ranked"][0]["interval"] == [1, 1]
    assert automated["family"]["size"] == 0  # one ranked arm: no pair to test
    # False automation is a criterion, not a safety attestation: three wrong AUTO decisions
    # break its ceiling and rank the arm lower, never unrank it.
    wrong = rank(referral, [graded["deterministic"], graded["llm wrong auto"]])
    assert wrong["unranked"] == []
    assert [(item["arm"], item["key"][1]) for item in wrong["ranked"]] == [
        ("deterministic", 2),
        ("llm", 3),
    ]
    assert grade.attestations(graded["llm wrong auto"][0])[-1]["state"] == "not met"
    # Short of evidence: 20 distinct urgent messages in the inbox's holdout. Each arm is
    # unranked, whatever it detected.
    classifier, classifier_ledger = arm(holdout, INBOX_CLASSIFIER)
    short = bands.ranking(
        [hidden(holdout.entry()), classifier],
        ledgers=[holdout.ledger, classifier_ledger],
        records=holdout.dataset.records,
    )
    assert short["ranked"] == [] and short["family"]["size"] == 0
    assert [item["arm"] for item in short["unranked"]] == ["deterministic", "classifier"]
    for item in short["unranked"]:
        assert item["reasons"] == [
            {"attestation": "no_critical_automated", "state": "insufficient evidence"},
            {
                "attestation": "recall_floor",
                "question": "urgency",
                "label": "urgent",
                "state": "insufficient evidence",
            },
        ]


def test_ece_and_latency_never_enter_the_order(
    referral: Built, graded: dict[str, Arm], ranked: dict[str, Any]
) -> None:
    changed = []
    for number, name in enumerate(FOUR):
        entry, ledger = graded[name]
        found = copy.deepcopy(entry)
        figures = found["figures"]
        figures["ece"] = {"status": "ok", "value": 0.9 if number % 2 else 0.0}
        for percentile in ("p50", "p95"):
            figures["latency_ms"][percentile] = {"estimate": 10.0**number, "low": 1, "high": 1e6}
        figures["subgroups"] = {}
        changed.append((found, ledger))
    assert canonical(rank(referral, changed)) == canonical(ranked)
    for item in ranked["ranked"]:
        assert list(item["bands"]) == list(bands.CRITERIA)
    text = canonical(ranked)
    assert '"ece"' not in text and "latency" not in text and "subgroup" not in text


# --------------------------------------------------------------------------------------------
# The Holm family and the rank intervals
# --------------------------------------------------------------------------------------------


def test_the_holm_family_is_every_pair_by_every_criterion_and_label_and_matches_statsmodels(
    ranked: dict[str, Any],
) -> None:
    family = ranked["family"]
    order = [item["arm"] for item in ranked["ranked"]]
    criteria = [
        ("detection", "urgency", "urgent"),
        ("detection", "next_step", "urgent_review"),
        ("false_automation", None, None),
        ("accuracy", None, None),
        ("brier", None, None),
    ]
    expected = [
        (list(pair), criterion, question, label)
        for pair in itertools.combinations(order, 2)
        for criterion, question, label in criteria
    ]
    found = [
        (test["arms"], test["criterion"], test.get("question"), test.get("label"))
        for test in family["tests"]
    ]
    assert found == expected
    assert family["size"] == len(expected) == 3 * (2 + 3)  # 3 pairs; L = 2 labels, and three
    assert family["of"] == bands.FAMILY and family["alpha"] == method.ALPHA
    assert family["seeds"] == {"bootstrap": 0, "resamples": 2000}
    assert len({test["name"] for test in family["tests"]}) == family["size"]
    # Each McNemar test against statsmodels' exact test, on its discordant pairs.
    for test in family["tests"]:
        if test["criterion"] != "brier":
            only_a, only_b = test["discordant"]
            reference = mcnemar([[0, only_a], [only_b, 0]], exact=True).pvalue
            assert math.isclose(test["p"], reference, abs_tol=1e-12)
    against = {
        (test["arms"][1], test["criterion"], test.get("label")): test
        for test in family["tests"]
        if test["arms"][0] == "classifier_and_llm"
    }
    accuracy = against[("classifier", "accuracy", None)]
    assert accuracy["n"] == 224 and accuracy["discordant"] == [WORSE, 0]
    assert accuracy["favours"] == "classifier_and_llm" and accuracy["reject"]
    urgent = against[("classifier", "detection", "urgent")]
    assert urgent["n"] == 113 and urgent["discordant"] == [0, 0] and urgent["p"] == 1.0
    brier = against[("classifier", "brier", None)]
    assert brier["n"] == 224 and brier["difference"] < 0
    assert brier["favours"] == "classifier_and_llm"
    alike = [t for t in family["tests"] if t["arms"] == ["classifier_and_llm", "deterministic"]]
    assert all(test["p"] == 1.0 and test["favours"] is None for test in alike)
    # Holm over the whole family, against statsmodels.
    p = [test["p"] for test in family["tests"]]
    reject, adjusted, _, _ = multipletests(p, alpha=method.ALPHA, method="holm")
    assert [test["reject"] for test in family["tests"]] == list(reject)
    for test, reference in zip(family["tests"], adjusted, strict=True):
        assert math.isclose(test["adjusted"], reference, abs_tol=1e-12)


def critical_only(arm: str, urgent: tuple[int, int], review: tuple[int, int]) -> dict[str, Any]:
    """An entry's two critical labels as far as the detection band reads them."""
    level = method.simultaneous_level(2)
    critical = [
        {
            "question": question,
            "label": label,
            "distinct": {"detected": k, "positives": n},
            "bound": {"level": level},
        }
        for (question, label), (k, n) in (
            (("urgency", "urgent"), urgent),
            (("next_step", "urgent_review"), review),
        )
    ]
    return {"arm": arm, "figures": {"critical": critical}}


def detection_tests(urgent: bool, review: bool, favours: str) -> list[dict[str, Any]]:
    return [
        {
            "arms": ["deterministic", "classifier"],
            "criterion": "detection",
            "question": question,
            "label": label,
            "reject": rejected,
            "favours": favours,
        }
        for question, label, rejected in (
            ("urgency", "urgent", urgent),
            ("next_step", "urgent_review", review),
        )
    ]


def test_rank_intervals_match_hand_computed_references(ranked: dict[str, Any]) -> None:
    # By hand: with (j, i) meaning j is significantly ahead of i, arm i's interval runs from
    # 1 + the arms ahead of it to n - the arms behind it.
    assert bands.rank_intervals(4, {(0, 1), (0, 2), (0, 3), (1, 3)}) == [
        (1, 1),
        (2, 3),
        (2, 4),
        (3, 4),
    ]
    assert bands.rank_intervals(3, set()) == [(1, 3), (1, 3), (1, 3)]
    assert bands.rank_intervals(1, set()) == [(1, 1)]
    # On the hidden set: the two equal arms are never apart, and both are significantly ahead
    # of the classifier on accuracy (20 discordant cases, all theirs), the first criterion where
    # the tuples differ.
    assert [(item["rank"], item["interval"]) for item in ranked["ranked"]] == [
        (1, [1, 2]),
        (1, [1, 2]),
        (3, [3, 3]),
    ]
    # Apart first on detection, the test is the one on the label that sets the lower arm's band.
    # A: both labels 500 of 500 (bound 0.9913, band 0.99). B: urgent 300 of 300 (0.9855, band
    # 0.98) and its next step 500 of 500: B's band is set by urgency.
    entries = [
        critical_only("deterministic", (500, 500), (500, 500)),
        critical_only("classifier", (300, 300), (500, 500)),
    ]
    assert [bands.key_of({"figures": {**e["figures"], **REST}})[0] for e in entries] == [0, 1]
    keys: list[bands.Key] = [(0, 1, 1, 1, 0), (1, 1, 1, 1, 0)]
    assert bands.worst_label(entries[1]) == 0
    ahead = bands.significantly_ahead
    assert ahead(entries, keys, detection_tests(True, False, "deterministic")) == {(0, 1)}
    assert ahead(entries, keys, detection_tests(False, True, "deterministic")) == set()
    # A test that rejects for the arm behind in the order never puts it ahead.
    assert ahead(entries, keys, detection_tests(True, True, "classifier")) == set()
    # Apart first on cost: never significantly apart, as cost has no test.
    assert ahead(entries, [(0, 1, 1, 1, 0), (0, 1, 1, 1, 3)], []) == set()


def test_tests_never_reorder_ranks(
    referral: Built, graded: dict[str, Arm], ranked: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = bands.stats.holm_family

    def decided(rejected: bool) -> Callable[..., dict[str, dict[str, Any]]]:
        def holm_family(p_values: dict[str, float], alpha: float) -> dict[str, dict[str, Any]]:
            found = real(p_values, alpha)
            return {name: {**test, "reject": rejected} for name, test in found.items()}

        return holm_family

    shown = {}
    for rejected in (True, False):
        monkeypatch.setattr(bands.stats, "holm_family", decided(rejected))
        shown[rejected] = rank(referral, [graded[name] for name in FOUR])
    for found in shown.values():
        listing = [(item["rank"], item["configuration"], item["key"]) for item in found["ranked"]]
        assert listing == [
            (item["rank"], item["configuration"], item["key"]) for item in ranked["ranked"]
        ]
        assert found["unranked"] == ranked["unranked"]
        for item in found["ranked"]:
            low, high = item["interval"]
            assert low <= item["rank"] <= high
    assert [item["interval"] for item in shown[False]["ranked"]] == [[1, 3], [1, 3], [1, 3]]
    assert [item["interval"] for item in shown[True]["ranked"]] == [[1, 2], [1, 2], [3, 3]]


# --------------------------------------------------------------------------------------------
# Attestations
# --------------------------------------------------------------------------------------------


def made_entry(labels: list[tuple[int, int, int]], false_automation: tuple[int, int]) -> dict:
    """A hidden set's entry as far as a grade reads it: per critical label its distinct
    positives, how many are detected and its positives decided AUTO; and false automation, k
    of n."""
    count = len(labels)
    level = method.simultaneous_level(count) if count else method.LEVEL
    critical = [
        {
            "question": f"q{index}",
            "label": "positive",
            "automated": {"k": automated, "n": positives},
            "distinct": {"positives": positives, "detected": detected},
            "bound": {"k": detected, "n": positives, "level": level, "labels": count},
        }
        for index, (positives, detected, automated) in enumerate(labels)
    ]
    k, n = false_automation
    in_effect = {"version": method.METHOD, "sha256": method.METHOD_SHA256, "in_effect": False}
    return {
        "scope": {"set": {"kind": "hidden"}, "method": in_effect},
        "figures": {"critical": critical, "false_automation": {"k": k, "n": n}},
    }


def states(entry: dict[str, Any]) -> list[str]:
    return [item["state"] for item in grade.attestations(entry)]


def test_attestations_are_met_not_met_or_insufficient_never_a_letter_or_score(
    graded: dict[str, Arm],
) -> None:
    deterministic = grade.grade(graded["deterministic"][0])
    assert deterministic is not None
    assert set(deterministic) == {"method", "attestations"}  # no letter, score or composite
    assert deterministic["method"] == {
        "version": "mendari.grading/1.0",
        "sha256": method.METHOD_SHA256,
        "in_effect": False,
    }
    found = deterministic["attestations"]
    assert [(item["attestation"], item.get("label"), item["state"]) for item in found] == [
        ("no_critical_automated", None, "met"),
        ("recall_floor", "urgent", "met"),
        ("recall_floor", "urgent_review", "met"),
        ("false_automation_ceiling", None, "met"),
    ]
    assert all(item["draft"] for item in found)  # the method is not in effect
    # Each figure a grade is decided on is the entry's own, k of n with its interval.
    figures = graded["deterministic"][0]["figures"]
    urgent = found[1]
    assert urgent["bound"] == figures["critical"][0]["bound"]
    bound = urgent["bound"]
    assert (bound["k"], bound["n"], bound["level"], bound["labels"]) == (113, 113, 0.975, 2)
    assert bound["low"] == round(beta.ppf(0.0125, 113, 1), 6)
    assert (urgent["floor"], urgent["needed"]) == (0.95, 100)
    assert found[0]["labels"][0]["automated"] == figures["critical"][0]["automated"]
    ceiling = found[3]
    assert ceiling["false_automation"] == figures["false_automation"]
    fa = ceiling["false_automation"]
    assert (fa["k"], fa["n"], ceiling["needed"]) == (0, 224, 183)
    assert fa["high"] == round(beta.ppf(0.975, 1, 224), 6) <= 0.02
    assert states(graded["llm missed"][0]) == ["met", "not met", "met", "met"]
    assert states(graded["llm automated"][0]) == ["not met", "met", "met", "met"]
    for entry, _ in graded.values():
        for item in grade.attestations(entry):
            assert item["state"] in grade.STATES
            assert not {"grade", "score", "letter", "overall", "composite"} & set(item)
    # Under another method's hash, an entry is never graded under this one's floors.
    other = copy.deepcopy(graded["deterministic"][0])
    other["scope"]["method"]["sha256"] = "0" * 64
    with pytest.raises(MendariError, match="made under another method"):
        grade.attestations(other)
    wrong_level = copy.deepcopy(graded["deterministic"][0])
    wrong_level["figures"]["critical"][0]["bound"]["level"] = 0.95
    with pytest.raises(MendariError, match="not the method's"):
        grade.attestations(wrong_level)
    with pytest.raises(MendariError, match="no critical label"):
        grade.attestations(made_entry([], (0, 200)))


@pytest.mark.parametrize("labels", [1, 2, 4, 6])
def test_an_attestation_is_insufficient_when_a_perfect_result_could_not_meet_it(
    labels: int,
) -> None:
    needed = method.min_positives(method.RECALL_FLOOR, labels)
    alpha = 1 - method.simultaneous_level(labels)
    # On the minimum a perfect result reaches the floor (scipy agrees); on one fewer it is
    # insufficient evidence: it could not reach it, or there are fewer than 100.
    assert beta.ppf(alpha / 2, needed, 1) >= method.RECALL_FLOOR
    if needed > method.MIN_POSITIVES:
        assert beta.ppf(alpha / 2, needed - 1, 1) < method.RECALL_FLOOR
    else:
        assert needed == method.MIN_POSITIVES
    perfect = [(needed, needed, 0)] * labels
    short = made_entry([(needed - 1, needed - 1, 0)] * labels, (0, 183))
    assert states(short)[1:-1] == ["insufficient evidence"] * labels
    assert states(made_entry(perfect, (0, 183))) == ["met"] * (labels + 2)
    # With the evidence, one miss on the minimum falls under the floor: not met.
    missed = made_entry([(needed, needed - 1, 0), *perfect[1:]], (0, 183))
    assert states(missed)[1] == "not met"
    # No critical case automated: under 100 distinct positives it cannot be met, unless a
    # positive was decided AUTO, which no further case could undo.
    assert states(made_entry([(99, 99, 0)] * labels, (0, 183)))[0] == "insufficient evidence"
    assert states(made_entry([(99, 99, 1)] * labels, (0, 183)))[0] == "not met"
    # The false-automation ceiling: no wrong AUTO in 182 labelled cases cannot reach 0.02 at 95%
    # (scipy agrees), in 183 it can; one wrong AUTO in 183 does not.
    assert method.min_labelled(method.FALSE_AUTOMATION_CEILING) == 183
    assert beta.ppf(0.975, 1, 182) > 0.02 >= beta.ppf(0.975, 1, 183)
    assert states(made_entry(perfect, (0, 182)))[-1] == "insufficient evidence"
    assert states(made_entry(perfect, (0, 183)))[-1] == "met"
    assert states(made_entry(perfect, (1, 183)))[-1] == "not met"
    # Insufficient evidence names the minimum beside the counts it was decided on.
    floor = next(
        item for item in grade.attestations(short) if item["attestation"] == "recall_floor"
    )
    assert floor["needed"] == needed
    assert (floor["bound"]["k"], floor["bound"]["n"]) == (needed - 1, needed - 1)


def test_twenty_emergency_positives_are_insufficient_evidence_for_a_grade(holdout: Built) -> None:
    found = grade.attestations(hidden(holdout.entry()))
    assert [(item["attestation"], item["state"]) for item in found] == [
        ("no_critical_automated", "insufficient evidence"),  # 20 distinct, of 100
        ("recall_floor", "insufficient evidence"),
        ("false_automation_ceiling", "insufficient evidence"),
    ]
    assert found[1]["bound"]["n"] == 20 and found[1]["needed"] == 100
    # 20 of its 30 cases are labelled for the primary question: false automation needs 183.
    assert (found[2]["false_automation"]["n"], found[2]["needed"]) == (20, 183)


# --------------------------------------------------------------------------------------------
# Reproducible, and never on a public set
# --------------------------------------------------------------------------------------------


def test_a_rerun_from_the_same_ledgers_and_seeds_is_byte_identical(
    referral: Built, graded: dict[str, Arm], ranked: dict[str, Any]
) -> None:
    again = build(REFERRAL, referral.dataset, DETERMINISTIC)
    assert again.ledger.dumps() == referral.ledger.dumps()  # a local configuration re-runs alike
    again = dataclasses.replace(again, run=referral.run)  # latency is an input, never re-measured
    rebuilt = [(hidden(again.entry()), again.ledger), arm(again, CLASSIFIER, worse_accuracy)]
    for (entry, ledger), name in zip(rebuilt, ("deterministic", "classifier"), strict=True):
        assert canonical(entry) == canonical(graded[name][0])
        assert ledger.dumps() == graded[name][1].dumps()
    first = rank(referral, [graded["deterministic"], graded["classifier"]])
    assert canonical(rank(referral, rebuilt)).encode() == canonical(first).encode()
    # The order the arms are given in changes nothing: the listing is the tuple's, then the
    # configuration's.
    backwards = rank(referral, [graded[name] for name in reversed(FOUR)])
    assert canonical(backwards) == canonical(ranked)


def test_nothing_is_ranked_or_graded_on_a_public_set(
    referral: Built, graded: dict[str, Arm]
) -> None:
    public = referral.entry()
    assert public["scope"]["set"]["kind"] == "public"
    assert public["ranking"] == {
        "status": "off",
        "reason": "public set: regression check, not a ranking",
    }
    assert public["grade"] is None and grade.grade(public) is None
    with pytest.raises(MendariError, match="a public set is never graded"):
        grade.attestations(public)
    # With its ledgers and records at hand, still off.
    both, both_ledger = graded["both"]
    both = copy.deepcopy(both)
    both["scope"]["set"]["kind"] = "public"
    found = bands.ranking(
        [public, both], ledgers=[referral.ledger, both_ledger], records=referral.dataset.records
    )
    assert found == bands.off(grade.PUBLIC_SET) == public["ranking"]
    with pytest.raises(MendariError, match="never ranked with a hidden set's"):
        bands.ranking([public, graded["both"][0]])
    # The board: ranking off, every entry's grade null, and no rank or grade in its text.
    shown = scoreboard.board([public, both], {REFERRAL: scoreboard.Current.of(referral.pack)})
    (table,) = shown["tables"]
    assert table["ranking"] == public["ranking"]
    assert [item["grade"] for item in table["entries"]] == [None, None]
    text = scoreboard_markdown(shown)
    assert "Public set: regression check, not a ranking." in text
    # The grade's bound is a figure of every entry; no rank, attestation or state is shown.
    assert re.search(r"\bRank\b|\branked\b|\brank\b|attestation|\bmet\b", text) is None


def test_a_hidden_set_is_ranked_only_from_its_paired_ledgers(
    referral: Built, graded: dict[str, Arm], holdout: Built
) -> None:
    deterministic, ledger = graded["deterministic"]
    both, both_ledger = graded["both"]
    # One arm graded: off, whatever else is at hand.
    assert bands.ranking([deterministic]) == bands.off("one arm graded")
    # A board holds no ledger: a hidden table of one arm shows its grade and "one arm graded",
    # and one of two is refused, never ranked on what cannot be paired.
    (table,) = scoreboard.board([deterministic], {})["tables"]
    assert table["ranking"] == {"status": "off", "reason": "one arm graded"}
    assert [item["state"] for item in table["entries"][0]["grade"]["attestations"]] == ["met"] * 4
    with pytest.raises(MendariError, match="ranked from their ledgers"):
        scoreboard.board([deterministic, both], {})
    records = referral.dataset.records
    with pytest.raises(MendariError, match="is not the one the entry"):
        bands.ranking([deterministic, both], ledgers=[both_ledger, ledger], records=records)
    twice = copy.deepcopy(both)
    twice["arm"] = "deterministic"
    with pytest.raises(MendariError, match="one configuration per arm"):
        bands.ranking([deterministic, twice], ledgers=[ledger, both_ledger], records=records)

    def reordered(cases: list[dict[str, Any]]) -> None:
        cases[0], cases[1] = cases[1], cases[0]

    swapped_ledger = edited(both_ledger, reordered)
    swapped = copy.deepcopy(both)
    swapped["inputs"]["ledger"] = swapped_ledger.id
    with pytest.raises(MendariError, match="do not pair"):
        bands.ranking([deterministic, swapped], ledgers=[ledger, swapped_ledger], records=records)
    with pytest.raises(MendariError, match="records are not the cases"):
        bands.ranking(
            [deterministic, both],
            ledgers=[ledger, both_ledger],
            records=[records[0]] * len(records),
        )
    with pytest.raises(MendariError, match="one pack, set and method"):
        bands.ranking([deterministic, hidden(holdout.entry())])
