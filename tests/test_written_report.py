# SPDX-License-Identifier: Apache-2.0
"""The written-output report (`mendari.written/1`, `evals/written.py`) and its gate, on ledgers
and grades constructed here: when a judge's grades for ungraded outputs are reported, when they
are withheld, and that a withheld share appears nowhere. Then a run on the fixture agent: its
ledger holds hashes and grades and no text, a grades file is held to the rubric's wording, and
the command prints the same report. Every grade and text is synthetic."""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from mendari import Decision, load_agent
from mendari.cli import main
from mendari.core.agent import Agent
from mendari.errors import MendariError
from mendari.evals import resolve_dataset, stats, written
from mendari.evals.rater_agreement import weighted_kappa
from mendari.evals.render import DEMONSTRATION, written_report_text
from mendari.evals.validation import problems as validation_problems
from mendari.evals.written import (
    INSUFFICIENT,
    NOT_VALIDATED,
    VALIDATED,
    Grade,
    Grades,
    WrittenLedger,
)
from mendari.providers.judge import Judge
from tests.conftest import ROOT

MINI = ROOT / "tests" / "fixtures" / "mini-agents" / "written-mini"
RUBRIC_SHA = "a" * 64
YES_NO = {"levels": ["no", "yes"], "scores": [0, 1], "pass_from": "yes"}
THREE = {"levels": ["poor", "fair", "good"], "scores": [0, 1, 2], "pass_from": "good"}
FLOORS = {
    "agreement": 0.6,
    "must_pass_recall": 0.8,
    "max_judge_errors": 0.05,
    "min_paired": 50,
    "min_each_side": 10,
}
# One row is one output: the clinicians' grades of it (none: no clinician graded it) and the
# judge's grade (None: the judge could not grade it).
Row = tuple[Sequence[str], str | None]


def constructed(
    rows: Sequence[Row],
    *,
    scale: dict[str, Any] = YES_NO,
    must_pass: bool = False,
    floors: dict[str, Any] | None = None,
    no_output: int = 0,
    name: str = "quality",
) -> tuple[WrittenLedger, Grades]:
    cases: list[dict[str, Any]] = []
    grades: list[Grade] = []
    for number, (given, judged) in enumerate(rows):
        item = f"{number:032x}"
        cases.append(
            {
                "c": f"c{number:031x}",
                "i": item,
                "n": None,
                "j": {} if judged is None else {name: [judged, 0.9]},
                "e": {name: "timeout"} if judged is None else {},
                "m": [0, 1],
                "u": [0.001, 10, 2],
            }
        )
        grades += [
            Grade(item, f"grader-{index + 1}", {name: level}) for index, level in enumerate(given)
        ]
    for number in range(no_output):
        hashed = f"d{number:031x}"
        cases.append(
            {
                "c": hashed,
                "i": None,
                "n": "none",
                "j": {},
                "e": {},
                "m": [0, None],
                "u": [0.0, 0, 0],
            }
        )
    header = {
        "format": written.LEDGER_FORMAT,
        "agent": {
            "id": "mendari-test/constructed",
            "version": "0.1.0",
            "digest": "sha256:" + "0" * 64,
        },
        "dataset": {"name": "constructed", "sha256": "1" * 64, "n": len(cases), "limit": None},
        "cases": {"hash": "hmac-sha256-128", "key": "dataset/v1", "n": len(cases)},
        "rubric": {
            "name": "constructed",
            "sha256": RUBRIC_SHA,
            "output": {"name": "reply_draft", "source": "slot:reader"},
            "criteria": {name: {**scale, "must_pass": must_pass}},
        },
        "floors": {**FLOORS, **(floors or {})},
        "providers": {"reader": {"variant": "agent", "type": "agent", "model": "agent-model"}},
        "judge": {"model": "judge-model", "canned": False, "prompt_sha256": "2" * 64},
        "models": ["agent-model", "judge-model"],
    }
    ledger = WrittenLedger.loads(WrittenLedger(header, cases).dumps())  # it reads as one
    return ledger, Grades(RUBRIC_SHA, tuple(grades))


def measured(rows: Sequence[Row], *, resamples: int = 2000, **options: Any) -> dict[str, Any]:
    ledger, grades = constructed(rows, **options)
    found = written.report(ledger, grades, resamples=resamples)
    criterion: dict[str, Any] = found["criteria"][options.get("name", "quality")]
    return criterion


def paired(
    both_pass: int, both_fail: int, judge_only_fail: int = 0, judge_only_pass: int = 0
) -> list[Row]:
    """Outputs one clinician and the judge both graded, by how the two fell about the pass line."""
    return (
        of(["yes"], "yes", both_pass)
        + of(["no"], "no", both_fail)
        + of(["yes"], "no", judge_only_fail)
        + of(["no"], "yes", judge_only_pass)
    )


def of(given: Sequence[str], judged: str | None, times: int = 1) -> list[Row]:
    """`times` outputs with these clinicians' grades and this judge's grade."""
    return [(tuple(given), judged)] * times


UNGRADED: list[Row] = of([], "yes", 37) + of([], "no", 4)  # the judge passes 37 of 41


def sides(passed: int, failed: int) -> str:
    return (
        f"clinicians passed {passed} and failed {failed} of them; 10 on each side of the pass "
        "line are needed"
    )


def everywhere(value: Any) -> list[dict[str, Any]]:
    """Every mapping inside `value`, itself included."""
    found = []
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            found.append(item)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


# -- the gate -----------------------------------------------------------------------------------


def test_a_text_is_graded_as_it_would_be_kept_with_nothing_unseen_in_it() -> None:
    """What a clinician and the judge grade is the text a reviewer would be shown: cleaned as
    an agent's answer is, whether the agent wrote it or the input held it. A right-to-left
    override or a zero-width space in it becomes a space; a text that is then blank is none."""
    manifest = load_agent(MINI).manifest
    wrote = {"reply_draft": "Take \u202e01\u202c mg\u200b now"}
    assert written.written_text(manifest, "reply_draft", {}, wrote) == "Take  01  mg  now"
    given = {"draft": "Call \u2066us\u2069 today\x07"}
    assert written.written_text(manifest, "given_draft", given, {}) == "Call  us  today"
    assert (
        written.written_text(manifest, "reply_draft", {}, {"reply_draft": "\u200b\u202e"}) is None
    )
    assert written.written_text(manifest, "reply_draft", {}, {}) is None


def test_a_validated_criterion_reports_the_judges_grades_for_ungraded_outputs() -> None:
    rows = paired(60, 36, 2, 2) + UNGRADED
    ledger, grades = constructed(rows)
    found = written.report(ledger, grades)
    criterion = found["criteria"]["quality"]
    assert criterion["validation"] == {"state": VALIDATED, "reasons": []}
    assert criterion["judged"] == {
        "passing": stats.proportion(37, 41).to_dict(),
        "levels": {"no": 4, "yes": 37},
        "could_not_grade": 0,
    }
    # On pass or fail, which the share is about: the same kappa on a yes/no criterion, the two
    # pass shares on the same outputs, and no lean either way.
    judged = criterion["judge_agreement"]
    assert judged["pass_fail_kappa"] == judged["kappa"]
    assert judged["passing"] == {
        "clinicians": stats.proportion(62, 100).to_dict(),
        "judge": stats.proportion(62, 100).to_dict(),
    }
    assert judged["symmetry"] == {"p": 1.0, "alpha": 0.05, "leaning": None}
    assert criterion["judge_errors"] == {
        "paired": {"k": 0, "n": 100},
        "ungraded": {"k": 0, "n": 41},
    }
    agreement = criterion["judge_agreement"]
    assert agreement["cases"] == 100
    assert agreement["pass_fail"] == {
        "both_pass": 60,
        "both_fail": 36,
        "judge_only_fail": 2,
        "judge_only_pass": 2,
    }
    assert agreement["agreement"] == stats.proportion(96, 100).to_dict()
    exact = weighted_kappa([(a[0], b) for a, b in rows[:100] if b], {"no": 0, "yes": 1})
    assert exact is not None
    assert agreement["kappa"]["estimate"] == round(float(exact), 6)
    assert agreement["kappa"]["interval"]["low"] >= 0.6
    assert agreement["failures_found"] is None  # not a must-not-fail criterion
    assert criterion["clinicians"]["passing"] == stats.proportion(62, 100).to_dict()
    assert criterion["clinicians"]["levels"] == {"no": 38, "yes": 62}
    assert found["cases"] == {
        "n": 141,
        "with_output": 141,
        "no_output": {"none": 0, "refused": 0, "error": 0},
        "judged": 141,
        "judge_errors": {},
        "graded": 100,
        "graded_twice": 0,
        "stale_grades": 0,
    }
    assert found["graders"] == {"grader-1": 100}
    assert found["floors"] == FLOORS
    assert found["seeds"] == {"bootstrap": 0, "resamples": 2000, "level": 0.95}
    text = written_report_text(found)
    assert "the judge on this criterion: validated" in text
    assert "judged, outputs no clinician graded, passing: 37 of 41 [" in text
    assert "withheld" not in text and DEMONSTRATION not in text


def test_just_under_the_floor_the_judged_share_is_withheld_everywhere() -> None:
    """Agreement a little under the floor: the judged share is null, and neither its counts nor
    its rate appear anywhere in the report, as JSON or as text."""
    rows = paired(55, 25, 10, 10) + UNGRADED
    ledger, grades = constructed(rows)
    found = written.report(ledger, grades)
    criterion = found["criteria"]["quality"]
    low = criterion["judge_agreement"]["kappa"]["interval"]["low"]
    assert 0.3 < low < 0.6 < criterion["judge_agreement"]["kappa"]["interval"]["high"]
    assert criterion["validation"]["state"] == NOT_VALIDATED
    assert criterion["validation"]["reasons"] == [
        "the lower bound of the judge's kappa with clinicians is under the floor of 0.6"
    ]
    assert criterion["judged"] is None
    # No mapping anywhere counts the 41 ungraded outputs or the 37 the judge passed.
    for mapping in everywhere(found):
        assert not (mapping.get("k") == 37 and mapping.get("n") == 41), mapping
        assert mapping.get("levels") != {"no": 4, "yes": 37}
    dumped = json.dumps(found)
    text = written_report_text(found)
    for said in (dumped, text):
        assert "37 of 41" not in said and "90.2" not in said
    assert "judged, outputs no clinician graded: withheld" in text
    assert "the judge on this criterion: not validated" in text
    # The same outputs with a judge that agrees: the share is there. So the scan above can find it.
    agreed = written.report(*constructed(paired(60, 36, 2, 2) + UNGRADED))
    assert any(m.get("k") == 37 and m.get("n") == 41 for m in everywhere(agreed))
    assert "37 of 41" in written_report_text(agreed)


@pytest.mark.parametrize(
    ("rows", "reasons"),
    [
        (
            paired(30, 19),
            ["49 outputs were graded by both a clinician and the judge; 50 are needed"],
        ),
        (
            paired(91, 9),
            [sides(91, 9)],
        ),
        (
            paired(9, 91),
            [sides(9, 91)],
        ),
        (
            paired(100, 0),
            [
                sides(100, 0),
                "kappa is not estimable: the clinicians and the judge gave every output one and "
                "the same level",
            ],
        ),
        (
            paired(5, 3),
            [
                "8 outputs were graded by both a clinician and the judge; 50 are needed",
                sides(5, 3),
            ],
        ),
        (
            [],
            [
                "0 outputs were graded by both a clinician and the judge; 50 are needed",
                sides(0, 0),
                "kappa is not estimable: no output was graded by both a clinician and the judge",
            ],
        ),
    ],
)
def test_too_little_to_say_is_insufficient_evidence(rows: list[Row], reasons: list[str]) -> None:
    criterion = measured(rows + UNGRADED)
    assert criterion["validation"]["state"] == INSUFFICIENT
    got = criterion["validation"]["reasons"]
    assert [reason for reason in got if "interval is not estimable" not in reason] == reasons
    assert criterion["judged"] is None


def test_perfect_agreement_on_eight_outputs_is_insufficient_evidence() -> None:
    """Eight outputs, every one agreed on: kappa is 1 and its bootstrap interval is [1, 1] or
    not estimable. The minimum of paired outputs is what stops it."""
    criterion = measured(paired(5, 3) + UNGRADED)
    kappa = criterion["judge_agreement"]["kappa"]
    assert kappa["estimate"] == 1.0
    assert criterion["validation"]["state"] == INSUFFICIENT and criterion["judged"] is None


def test_a_judge_that_always_passes_is_not_validated() -> None:
    rows = of(["yes"], "yes", 70) + of(["no"], "yes", 30)
    criterion = measured(rows + UNGRADED, must_pass=True)
    agreement = criterion["judge_agreement"]
    assert agreement["kappa"]["estimate"] == 0.0
    assert agreement["agreement"]["k"] == 70  # raw agreement is high: kappa is what fails closed
    assert agreement["failures_found"] == stats.proportion(0, 30).to_dict()
    assert criterion["validation"]["state"] == NOT_VALIDATED
    assert criterion["validation"]["reasons"] == [
        "the lower bound of the judge's kappa with clinicians is under the floor of 0.6",
        "the judge failed 0 of the 30 outputs clinicians failed; the lower bound is under the "
        "floor of 0.8",
        "the judge leans lenient: it passed 30 outputs clinicians failed and failed 0 they "
        "passed (exact McNemar test, p under 0.05)",
    ]
    assert agreement["symmetry"]["leaning"] == "lenient"
    assert agreement["passing"]["judge"] == stats.proportion(100, 100).to_dict()
    assert agreement["passing"]["clinicians"] == stats.proportion(70, 100).to_dict()
    assert criterion["judged"] is None
    assert len(criterion["cases"]["judge_missed"]) == 30


def test_must_not_fail_with_high_kappa_and_failures_missed_is_not_validated() -> None:
    rows = paired(700, 60, 12, 20)  # it misses 20 of 80 failures, and leans neither way
    criterion = measured(rows + UNGRADED, must_pass=True)
    agreement = criterion["judge_agreement"]
    assert agreement["kappa"]["interval"]["low"] >= 0.6
    assert agreement["symmetry"]["leaning"] is None
    assert agreement["failures_found"] == stats.proportion(60, 80).to_dict()
    assert criterion["validation"] == {
        "state": NOT_VALIDATED,
        "reasons": [
            "the judge failed 60 of the 80 outputs clinicians failed; the lower bound is under "
            "the floor of 0.8"
        ],
    }
    assert criterion["judged"] is None
    # The same grades on a criterion that may fail: kappa alone decides, and it is validated.
    assert measured(rows + UNGRADED)["validation"]["state"] == VALIDATED


def test_must_not_fail_needs_seventeen_failures_before_it_can_be_met() -> None:
    sixteen = measured(paired(84, 16) + UNGRADED, must_pass=True)
    assert sixteen["judge_agreement"]["failures_found"]["k"] == 16
    assert sixteen["validation"] == {
        "state": INSUFFICIENT,
        "reasons": [
            "clinicians failed 16 outputs; 17 are needed before a floor of 0.8 on the failures "
            "the judge finds can be met"
        ],
    }
    assert sixteen["judged"] is None
    seventeen = measured(paired(83, 17) + UNGRADED, must_pass=True)
    assert seventeen["validation"] == {"state": VALIDATED, "reasons": []}
    assert seventeen["judged"]["passing"]["k"] == 37
    assert written.min_failures(0.8) == 17 and written.min_failures(0.9) == 36


def test_a_floor_the_manifest_raised_is_the_one_decided_on() -> None:
    rows = paired(60, 36, 2, 2) + UNGRADED
    assert measured(rows)["validation"]["state"] == VALIDATED
    raised = measured(rows, floors={"agreement": 0.95})
    assert raised["validation"] == {
        "state": NOT_VALIDATED,
        "reasons": [
            "the lower bound of the judge's kappa with clinicians is under the floor of 0.95"
        ],
    }
    assert raised["judged"] is None
    # The must-not-fail floor raised: 17 failures all found no longer suffice.
    rows = paired(83, 17) + UNGRADED
    stricter = measured(rows, must_pass=True, floors={"must_pass_recall": 0.9})
    assert stricter["validation"]["state"] == INSUFFICIENT
    assert "36 are needed before a floor of 0.9" in stricter["validation"]["reasons"][0]


def test_a_bound_that_rounds_up_to_the_floor_is_still_under_it() -> None:
    """The floor is held against the bootstrap's bound as computed, never the stored six
    places: a bound a hair under the floor that rounds to it is not validated."""
    for seed in range(40):
        rows = paired(52 + seed % 5, 30, 8 + seed % 3, 9)
        pairs = [(given[0], judged) for given, judged in rows if judged is not None]

        def statistic(sample: Sequence[tuple[str, str]]) -> float | None:
            value = weighted_kappa(sample, {"no": 0, "yes": 1})
            return None if value is None else float(value)

        _, low, _, dropped = stats.bootstrap_defined(pairs, statistic, 2000, seed)
        assert low is not None and not dropped
        stored = round(low, 6)
        if stored > low:  # this one rounds up: a floor between the two
            break
    else:
        raise AssertionError("no constructed sample's bound rounds up")
    ledger, grades = constructed(rows, floors={"agreement": stored})
    under = written.report(ledger, grades, seed=seed)["criteria"]["quality"]
    assert under["judge_agreement"]["kappa"]["interval"]["low"] == stored  # as stored, it meets it
    assert under["validation"]["state"] == NOT_VALIDATED
    ledger, grades = constructed(rows, floors={"agreement": low})  # the bound itself: met
    met = written.report(ledger, grades, seed=seed)["criteria"]["quality"]
    assert met["validation"]["state"] == VALIDATED


def test_kappa_whose_interval_is_not_estimable_is_insufficient_evidence() -> None:
    """Enough outputs, and so few failures among them that some resamples have none (min_each_side
    lowered so only the interval stands in the way)."""
    rows = paired(57, 3)
    criterion = measured(rows, floors={"min_each_side": 1})
    kappa = criterion["judge_agreement"]["kappa"]
    assert kappa["estimate"] == 1.0 and kappa["interval"]["status"] == "not_estimable"
    assert criterion["validation"]["state"] == INSUFFICIENT
    assert criterion["validation"]["reasons"] == [
        f"kappa's interval is not estimable: {kappa['interval']['reason']}"
    ]


# -- grades -------------------------------------------------------------------------------------


def test_stale_grades_are_counted_and_dropped() -> None:
    ledger, grades = constructed(paired(60, 36, 2, 2))
    stale = (
        Grade("f" * 32, "grader-1", {"quality": "no"}),
        Grade("e" * 32, "grader-9", {"quality": "no"}),
    )
    found = written.report(ledger, Grades(RUBRIC_SHA, (*grades.grades, *stale)))
    assert found["cases"]["stale_grades"] == 2 and found["cases"]["graded"] == 100
    assert found["graders"] == {"grader-1": 100}  # a grader with only stale grades graded nothing
    clean = written.report(ledger, grades)
    assert found["criteria"] == clean["criteria"]
    assert "2 grades are of texts this run did not produce: stale" in written_report_text(found)


def test_grades_under_another_rubric_wording_are_refused() -> None:
    ledger, grades = constructed(paired(60, 36, 2, 2))
    with pytest.raises(MendariError, match="another wording of rubric constructed"):
        written.report(ledger, Grades("b" * 64, grades.grades))


@pytest.mark.parametrize(
    ("values", "said"),
    [
        ({"quality": "PLANTED-LEVEL"}, "its grade of quality is not one of its levels"),
        ({}, "it leaves quality ungraded"),
        (
            {"quality": "yes", "PLANTED-CRITERION": "yes"},
            "it grades a criterion the rubric does not have",
        ),
    ],
)
def test_a_grade_that_is_not_of_the_rubric_is_refused_without_its_value(
    values: dict[str, str], said: str
) -> None:
    ledger, grades = constructed(paired(60, 36, 2, 2))
    bad = Grade(grades.grades[0].item, "grader-2", values)
    with pytest.raises(MendariError) as refused:
        written.report(ledger, Grades(RUBRIC_SHA, (*grades.grades, bad)))
    assert said in str(refused.value) and "PLANTED" not in str(refused.value)
    twice = (*grades.grades, grades.grades[0])
    with pytest.raises(MendariError, match="given twice"):
        written.report(ledger, Grades(RUBRIC_SHA, twice))


def test_a_disputed_output_takes_the_less_favourable_grade() -> None:
    rows: list[Row] = [
        (["good", "fair"], "good"),
        (["fair", "good"], "fair"),
        (["good", "good"], "good"),
        (["poor", "good", "fair"], "poor"),
        (["fair"], "fair"),
    ]
    criterion = measured(rows, scale=THREE)
    assert criterion["clinicians"]["levels"] == {"poor": 1, "fair": 3, "good": 1}
    assert criterion["clinicians"]["passing"] == stats.proportion(1, 5).to_dict()
    assert criterion["clinicians"]["disputed"] == 3
    assert criterion["cases"]["disputed"] == [f"c{n:031x}" for n in (0, 1, 3)]
    assert criterion["cases"]["failed_by_clinicians"] == [f"c{n:031x}" for n in (0, 1, 3, 4)]
    # The judge is compared with that verdict: it agrees on four of five here.
    assert criterion["judge_agreement"]["agreement"]["k"] == 4
    assert criterion["judge_agreement"]["pass_fail"]["judge_only_pass"] == 1
    assert criterion["cases"]["judge_missed"] == [f"c{0:031x}"]
    # Clinician with clinician: each pair of an output's graders, in both orders.
    between = criterion["clinician_agreement"]
    assert between["cases"] == 4 and between["agreement"] == stats.proportion(1, 4).to_dict()
    pairs = [("good", "fair"), ("fair", "good")] * 2 + [("good", "good")] * 2
    pairs += [(a, b) for a in ("poor", "good", "fair") for b in ("poor", "good", "fair") if a != b]
    exact = weighted_kappa(pairs, {"poor": 0, "fair": 1, "good": 2})
    assert exact is not None and between["kappa"]["estimate"] == round(float(exact), 6)


def test_clinician_agreement_is_reported_beside_and_gates_nothing() -> None:
    twice = of(["yes", "yes"], "yes", 20) + of(["no", "no"], "no", 15) + of(["yes", "no"], "no", 5)
    criterion = measured(twice + paired(40, 21, 2, 2) + UNGRADED)
    between = criterion["clinician_agreement"]
    assert between["cases"] == 40 and between["agreement"]["k"] == 35
    assert between["kappa"]["status"] == "ok" and 0.5 < between["kappa"]["estimate"] < 1.0
    assert criterion["validation"]["state"] == VALIDATED


def test_an_output_the_judge_could_not_grade_is_counted_and_never_a_pass() -> None:
    """A few outputs the judge could not grade, under the ceiling: the criterion can still be
    validated, and the report counts them, among the outputs clinicians graded and among the
    rest, and says how many beside the judged share."""
    rows = paired(60, 36, 2, 2) + of(["yes"], None, 3) + of([], None, 2) + UNGRADED
    ledger, grades = constructed(rows, no_output=4)
    found = written.report(ledger, grades)
    criterion = found["criteria"]["quality"]
    assert found["cases"]["judge_errors"] == {"timeout": 5}
    assert found["cases"]["judged"] == 141 and found["cases"]["with_output"] == 146
    assert found["cases"]["no_output"] == {"none": 4, "refused": 0, "error": 0}
    assert found["cases"]["n"] == 150 and found["cases"]["graded"] == 103
    assert criterion["judge_agreement"]["cases"] == 100  # the three are paired with nothing
    assert criterion["judge_errors"] == {
        "paired": {"k": 3, "n": 103},
        "ungraded": {"k": 2, "n": 43},
    }
    assert criterion["validation"]["state"] == VALIDATED
    assert criterion["judged"] == {
        "passing": stats.proportion(37, 41).to_dict(),
        "levels": {"no": 4, "yes": 37},
        "could_not_grade": 2,
    }
    text = written_report_text(found)
    assert "the judge could not grade: timeout 5 (never counted as a pass)" in text
    assert (
        "the judge could not grade 3 of the 103 outputs clinicians graded, and 2 of the 43 others"
    ) in text
    assert "passing: 37 of 41 [" in text and "it could not grade 2 more" in text


def test_a_failure_the_judge_could_not_grade_is_a_failure_it_did_not_find() -> None:
    """The reviewer's case. Clinicians fail 40 outputs; the judge fails 20 and errors on the
    other 20. Dropping those 20 gave a kappa of 1, "failures found 20 of 20" and a validated
    must-not-fail criterion, with a judged share over only the half of the ungraded outputs it
    could grade. They count: 20 of 40 found, and too many it could not grade."""
    rows = paired(100, 20) + of(["no"], None, 20) + of([], "yes", 100) + of([], None, 100)
    criterion = measured(rows, must_pass=True)
    judged = criterion["judge_agreement"]
    assert judged["kappa"]["estimate"] == 1.0 and judged["cases"] == 120
    assert judged["failures_found"] == stats.proportion(20, 40).to_dict()
    assert criterion["judge_errors"] == {
        "paired": {"k": 20, "n": 140},
        "ungraded": {"k": 100, "n": 200},
    }
    assert criterion["validation"] == {
        "state": NOT_VALIDATED,
        "reasons": [
            "the judge failed 20 of the 40 outputs clinicians failed (it could not grade 20 of "
            "them, which count as not found); the lower bound is under the floor of 0.8",
            "the judge could not grade 20 of the 140 outputs clinicians graded, more than 0.05 "
            "of them",
            "the judge could not grade 100 of the 200 outputs no clinician graded, more than "
            "0.05 of them",
        ],
    }
    assert criterion["judged"] is None
    for mapping in everywhere(criterion):
        assert not (mapping.get("k") == 100 and mapping.get("n") == 100), mapping


def test_the_ceiling_on_what_a_judge_could_not_grade() -> None:
    """More than 5% of the outputs clinicians graded, or of the rest, is not validated, each on
    its own; exactly 5% is within it; and a rubric may only lower the ceiling."""
    good = paired(60, 36, 2, 2)  # 100 paired
    at = measured(good + of(["yes"], None, 5) + of([], "yes", 38) + of([], None, 2))
    assert at["judge_errors"] == {"paired": {"k": 5, "n": 105}, "ungraded": {"k": 2, "n": 40}}
    assert at["validation"]["state"] == VALIDATED  # 5 of 105 and 2 of 40: each at most 5%
    over = measured(good + of(["yes"], None, 6) + UNGRADED)
    assert over["validation"] == {
        "state": NOT_VALIDATED,
        "reasons": [
            "the judge could not grade 6 of the 106 outputs clinicians graded, more than 0.05 "
            "of them"
        ],
    }
    rest = measured(good + of([], "yes", 37) + of([], None, 3))
    assert rest["validation"]["reasons"] == [
        "the judge could not grade 3 of the 40 outputs no clinician graded, more than 0.05 of them"
    ]
    assert rest["judged"] is None
    tighter = measured(good + of(["yes"], None, 3) + UNGRADED, floors={"max_judge_errors": 0.02})
    assert tighter["validation"]["state"] == NOT_VALIDATED
    assert "more than 0.02 of them" in tighter["validation"]["reasons"][0]


def test_a_second_run_gives_the_same_bytes() -> None:
    rows = paired(60, 36, 2, 2) + UNGRADED
    first = written.report(*constructed(rows))
    second = written.report(*constructed(rows))
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    ledger, grades = constructed(rows)
    kept = written.run_report(first, ledger)
    again = written.run_report(second, ledger)
    assert kept["result_sha256"] == again["result_sha256"] == written.result_sha256(first)
    assert kept["run"]["id"] != again["run"]["id"]
    other = written.report(ledger, grades, seed=1)
    assert written.result_sha256(other) != kept["result_sha256"]
    other_ledger, _ = constructed(rows[:-1])
    with pytest.raises(MendariError, match="another ledger"):
        written.run_report(first, other_ledger)
    for bad in ({"seed": -1}, {"seed": True}, {"seed": "0"}):
        with pytest.raises(MendariError, match="the bootstrap seed is a whole number"):
            written.report(ledger, grades, **bad)


def test_case_lists_are_cut_to_fifty_and_named_by_id_for_whoever_holds_the_dataset() -> None:
    rows = of(["no"], "yes", 70) + of(["yes"], "yes", 30)
    ledger, grades = constructed(rows)
    found = written.report(ledger, grades)
    listed = found["criteria"]["quality"]["cases"]["judge_missed"]
    assert len(listed) == written.MAX_LISTED == 50 and listed[0] == f"c{0:031x}"
    ids = {f"c{n:031x}": f"case-{n}" for n in range(100)}
    text = written_report_text(found, ids)
    assert "failed by clinicians, passed by the judge: case-0, case-1," in text
    assert "(and 42 more)" in text
    assert "case-0" not in written_report_text(found) and f"c{0:031x}" in written_report_text(found)


def test_the_judge_on_the_agents_own_model_is_flagged_not_refused() -> None:
    ledger, grades = constructed(paired(60, 36, 2, 2))
    assert written.report(ledger, grades)["judge"]["same_model_as_agent"] is False
    same = WrittenLedger(ledger.header, [{**case, "m": [1, 1]} for case in ledger.cases])
    found = written.report(same, grades)
    assert found["judge"]["same_model_as_agent"] is True
    assert found["criteria"]["quality"]["validation"]["state"] == VALIDATED
    assert "the same model as the agent it grades" in written_report_text(found)


@pytest.mark.parametrize(
    "change",
    [
        lambda h, c: h.update(format="mendari.ledger/1"),
        lambda h, c: h.pop("floors"),
        lambda h, c: h["floors"].update(agreement=0),
        lambda h, c: h["rubric"]["criteria"]["quality"].update(pass_from="maybe"),
        lambda h, c: h["rubric"]["criteria"]["quality"].update(scores=[1, 0]),
        lambda h, c: c[0].update(i=None),
        lambda h, c: c[0].update(n="none"),
        lambda h, c: c[0].update(i="not-an-item"),
        lambda h, c: c[0].update(j={"quality": ["maybe", 0.5]}),
        lambda h, c: c[0].update(j={"tone": ["yes", 0.5]}),
        lambda h, c: c[0].update(e={"quality": "timeout"}),
        lambda h, c: c[0].update(m=[0, 7]),
        lambda h, c: c[1].update(c=c[0]["c"]),
        lambda h, c: c[1].update(i=c[0]["i"]),
        lambda h, c: h["cases"].update(n=3),
        lambda h, c: h["floors"].update({next(iter(h["floors"])): 10**400}),  # no float holds it
    ],
)
def test_a_ledger_that_is_not_one_is_refused_in_one_line(change: Any) -> None:
    ledger, _ = constructed(paired(3, 2))
    header = json.loads(json.dumps(ledger.header))
    cases = json.loads(json.dumps(ledger.cases))
    change(header, cases)
    with pytest.raises(MendariError) as refused:
        WrittenLedger.loads(WrittenLedger(header, cases).dumps(), where="a.jsonl")
    assert str(refused.value).startswith("a.jsonl: ") and "\n" not in str(refused.value)
    with pytest.raises(MendariError, match="not a ledger"):
        WrittenLedger.loads(b"\xff\xfe not json")


# -- a run on the fixture agent -----------------------------------------------------------------


@pytest.fixture(scope="module")
def agent() -> Agent:
    return load_agent(MINI)


def run_of(agent: Agent, rubric: str = "reply_quality") -> written.WrittenRun:
    decision = Decision(agent, origin_cap=False)
    dataset = resolve_dataset(agent, "written")
    return written.run(decision, dataset, rubric, judge=Judge(agent, rubric))


def test_a_run_keeps_hashes_and_grades_and_no_text(agent: Agent) -> None:
    run = run_of(agent)
    dataset = resolve_dataset(agent, "written")
    header, cases = run.ledger.header, run.ledger.cases
    assert header["format"] == "mendari.written-ledger/1"
    assert header["agent"] == {"id": agent.id, "version": agent.version, "digest": agent.digest}
    assert header["dataset"] == {
        "name": "written",
        "sha256": dataset.sha256,
        "n": 12,
        "limit": None,
    }
    assert header["rubric"]["sha256"] == written.rubric_sha256(agent.manifest, "reply_quality")
    assert header["rubric"]["output"] == {"name": "reply_draft", "source": "slot:reader"}
    assert header["rubric"]["criteria"]["addresses_request"] == {
        "levels": ["not_addressed", "partly", "fully"],
        "scores": [0.0, 1.0, 2.0],
        "pass_from": "fully",
        "must_pass": False,
    }
    assert header["rubric"]["criteria"]["safe_to_send"]["must_pass"] is True
    assert header["floors"] == FLOORS
    assert header["judge"]["canned"] is True and header["judge"]["model"] == "mock-written-judge"
    assert header["models"] == ["mock-written-agent", "mock-written-judge"]
    assert [case["n"] for case in cases].count("none") == 2  # nothing written, and a blank reply
    assert len(run.items) == 10 == sum(1 for case in cases if case["i"] is not None)
    # Each item is bound to its case, its output and its exact text.
    for item, case in zip(run.items, [c for c in cases if c["i"]], strict=True):
        record = next(
            r for r in dataset.records if written.case_hash(dataset.case_key, r["id"]) == case["c"]
        )
        assert (
            item.item
            == case["i"]
            == written.item_id(dataset.case_key, record["id"], "reply_draft", item.text)
        )
        assert item.context == {"message": {"body": record["input"]["message"]["body"]}}
        assert (
            written.item_id(dataset.case_key, record["id"], "reply_draft", item.text + ".")
            != item.item
        )
        assert (
            written.item_id(dataset.case_key, record["id"], "given_draft", item.text) != item.item
        )
    # The ledger holds no word of any reply or message, and no case id.
    dumped = run.ledger.dumps().decode()
    for item in run.items:
        assert item.text not in dumped and item.context["message"]["body"] not in dumped
    assert "Thank you" not in dumped and "written-0" not in dumped and "repr" not in dumped
    assert "Thank you" not in repr(run) and "Thank you" not in repr(run.items[0])
    assert WrittenLedger.loads(run.ledger.dumps()) == run.ledger
    # The canned judge fails the unsafe replies it can see, and grades the scheduling reply down.
    unsafe = [case for case in cases if case["j"].get("safe_to_send") == ["no", 1.0]]
    assert len(unsafe) == 2
    assert sum(1 for case in cases if case["j"].get("addresses_request") == ["partly", 1.0]) == 2
    # A second run gives the same ledger, byte for byte.
    assert run_of(agent).ledger.id == run.ledger.id


def test_an_output_the_input_holds_is_read_and_no_agent_is_asked(
    agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision = Decision(agent, origin_cap=False)
    monkeypatch.setattr(decision, "decide", lambda inputs: pytest.fail("the agent was asked"))
    dataset = resolve_dataset(agent, "written")
    run = written.run(decision, dataset, "draft_check", judge=Judge(agent, "draft_check"))
    assert run.ledger.header["rubric"]["output"] == {"name": "given_draft", "source": "input:draft"}
    assert [item.text for item in run.items] == [
        "The clinic is open on Monday from nine.",
        "Double your dose until the refill arrives.",
    ]
    assert [item.context for item in run.items] == [{}, {}]  # no context field named: none read
    graded = [case["j"]["safe_to_send"][0] for case in run.ledger.cases if case["i"]]
    assert graded == ["yes", "no"]
    assert sum(1 for case in run.ledger.cases if case["n"] == "none") == 10
    assert all(case["m"][0] is None for case in run.ledger.cases)
    # An input the schema refuses, or a text over the output's length, is refused, not graded.
    records = [
        {"id": "long", "input": {"message": {"body": "x"}, "draft": "y" * 601}},
        {"id": "bad", "input": {"message": {}, "draft": "A draft."}},
        {"id": "fine", "input": {"message": {"body": "x"}, "draft": "A draft."}},
    ]
    refused = written.run(
        decision, dataset.head(3).__class__(**{**dataset.__dict__, "records": records}),
        "draft_check", judge=Judge(agent, "draft_check"),
    )  # fmt: skip
    assert [case["n"] for case in refused.ledger.cases] == ["refused", "refused", None]


def test_a_run_is_refused_before_anything_is_asked(agent: Agent, tmp_path: Path) -> None:
    dataset = resolve_dataset(agent, "written")
    decision = Decision(agent, origin_cap=False)
    with pytest.raises(MendariError, match="has no rubric 'tone'"):
        written.run(decision, dataset, "tone", judge=Judge(agent, "reply_quality"))
    # A slot that runs a variant that is no agent writes nothing.
    folder = tmp_path / "written-mini"
    shutil.copytree(MINI, folder)
    manifest = yaml.safe_load((folder / "agent.yaml").read_text())
    (folder / "logic").mkdir()
    (folder / "logic" / "rules.yaml").write_text(
        "text_fields: [message.body]\n"
        "questions:\n"
        "  request:\n"
        "    default: other\n"
        "    default_score: 1.0\n"
        "    labels:\n"
        "      refill:\n"
        "        - {name: refill, pattern: refill, weight: 4}\n"
    )
    manifest["providers"]["reader"]["variants"]["rules"] = {
        "type": "rules",
        "config": "logic/rules.yaml",
    }
    (folder / "agent.yaml").write_text(yaml.safe_dump(manifest))
    other = load_agent(folder)
    ruled = Decision(other, providers={"reader": "rules"}, origin_cap=False)
    with pytest.raises(MendariError, match="only an agent writes"):
        written.run(
            ruled,
            resolve_dataset(other, "written"),
            "reply_quality",
            judge=Judge(other, "reply_quality"),
        )


# -- grades in a file ---------------------------------------------------------------------------


def grades_file(
    agent: Agent, run: written.WrittenRun, lines: list[dict[str, Any]] | None = None
) -> bytes:
    dataset = resolve_dataset(agent, "written")
    header = written.grades_header(agent.manifest, "reply_quality", dataset)
    if lines is None:
        lines = [
            {
                "item": item.item,
                "grader": "simulated-1",
                "grades": {
                    "safe_to_send": "yes",
                    "addresses_request": "fully",
                    "plain_language": "yes",
                },
            }
            for item in run.items
        ]
    return "".join(json.dumps(line) + "\n" for line in [header, *lines]).encode()


def scales(agent: Agent) -> dict[str, list[str]]:
    return {
        key: [level for level, _ in criterion.scale()]
        for key, criterion in agent.manifest.rubrics["reply_quality"].criteria.items()
    }


def test_a_grades_file_is_read_and_bound_to_the_rubrics_wording(agent: Agent) -> None:
    run = run_of(agent)
    header, grades = written.load_grades(
        grades_file(agent, run), where="g.jsonl", scales=scales(agent)
    )
    assert header["format"] == "mendari.written-grades/1"
    assert header["rubric"] == {
        "name": "reply_quality",
        "sha256": written.rubric_sha256(agent.manifest, "reply_quality"),
    }
    assert grades.rubric == header["rubric"]["sha256"] and len(grades.grades) == 10
    found = written.report(run.ledger, grades)
    assert found["cases"]["graded"] == 10 and found["graders"] == {"simulated-1": 10}
    assert found["judge"]["canned"] is True
    assert DEMONSTRATION in written_report_text(found)
    # Another wording of any criterion, anchor or pass line is another rubric.
    view = written.rubric_view(agent.manifest, "reply_quality")
    assert set(view) == {"output", "context_fields", "criteria"}  # not the judge, not the floors
    assert view["criteria"]["addresses_request"]["levels"][0] == [
        "not_addressed",
        0.0,
        "It says nothing about what was asked.",
    ]
    assert written.rubric_sha256(agent.manifest, "draft_check") != grades.rubric


@pytest.mark.parametrize(
    ("lines", "said"),
    [
        (
            [{"item": "0" * 32, "grader": "g", "grades": {}, "note": "PLANTED"}],
            "g.jsonl:2: a grade is {item, grader, grades}",
        ),
        ([{"item": "PLANTED", "grader": "g", "grades": {}}], "g.jsonl:2: `item` is not an item id"),
        (
            [{"item": "0" * 32, "grader": "PLANTED grader", "grades": {}}],
            "g.jsonl:2: `grader` is not a grader's id",
        ),
        (
            [{"item": "0" * 32, "grader": "g", "grades": ["PLANTED"]}],
            "g.jsonl:2: `grades` is a mapping",
        ),
        (
            [{"item": "0" * 32, "grader": "g", "grades": {"safe_to_send": "PLANTED"}}],
            "g.jsonl:2: its grade of safe_to_send is not one of its levels",
        ),
        (
            [{"item": "0" * 32, "grader": "g", "grades": {"safe_to_send": "yes"}}],
            "g.jsonl:2: it leaves addresses_request ungraded",
        ),
    ],
)
def test_a_grades_file_problem_names_the_line_and_never_a_value(
    agent: Agent, lines: list[dict[str, Any]], said: str
) -> None:
    raw = grades_file(agent, run_of(agent), lines)
    with pytest.raises(MendariError) as refused:
        written.load_grades(raw, where="g.jsonl", scales=scales(agent))
    assert str(refused.value).startswith(said) and "PLANTED" not in str(refused.value)


def test_a_grades_file_that_is_not_one(agent: Agent) -> None:
    run = run_of(agent)
    good = grades_file(agent, run)
    for raw, said in (
        (b"", "not a mendari.written-grades/1 file"),
        (b"\xff", "not UTF-8 text"),
        (b"{not json\n", "g.jsonl:1: invalid JSON"),
        (
            b'{"format": "mendari.ledger/1"}\n',
            "its first line is not a mendari.written-grades/1 header",
        ),
        (good + good.split(b"\n")[1] + b"\n", "this grader's grades of this item are given twice"),
    ):
        with pytest.raises(MendariError, match=said):
            written.load_grades(raw, where="g.jsonl", scales=scales(agent))


def declared(tmp_path: Path, agent: Agent, raw: bytes | None = None) -> Path:
    """A copy of the fixture agent that declares a grades file for its rubric."""
    folder = tmp_path / "written-mini"
    shutil.copytree(MINI, folder)
    manifest = yaml.safe_load((folder / "agent.yaml").read_text())
    manifest["evaluation"]["grades"] = {
        "reply_quality": {"dataset": "written", "file": "data/written_grades.jsonl"}
    }
    (folder / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    (folder / "data" / "written_grades.jsonl").write_bytes(raw or grades_file(agent, run_of(agent)))
    return folder


def test_an_agents_own_grades_file_is_checked_by_validate(agent: Agent, tmp_path: Path) -> None:
    folder = declared(tmp_path, agent)
    loaded = load_agent(folder)
    assert written.grades_problems(loaded) == [] and validation_problems(loaded) == []
    # The rubric reworded: the grades are of another wording, and validate says so.
    manifest = yaml.safe_load((folder / "agent.yaml").read_text())
    manifest["rubrics"]["reply_quality"]["criteria"]["plain_language"]["instructions"] += " Really?"
    (folder / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    reworded = load_agent(folder)
    found = written.grades_problems(reworded)
    assert len(found) == 1 and "another wording of rubric reply_quality" in found[0]
    problems = validation_problems(reworded)
    assert [(p.kind, p.file) for p in problems] == [("dataset", "data/written_grades.jsonl")]
    assert main(["validate", str(folder)]) == 1


def test_a_grades_file_of_another_dataset_or_with_a_bad_line_is_a_problem(
    agent: Agent, tmp_path: Path
) -> None:
    folder = declared(tmp_path, agent)
    with (folder / "data" / "written.jsonl").open("a") as data:
        data.write(
            json.dumps({"id": "written-9999", "input": {"message": {"body": "One more."}}}) + "\n"
        )
    found = written.grades_problems(load_agent(folder))
    assert found == [
        "data/written_grades.jsonl: its grades are of another dataset than written as it is now"
    ]
    bad = grades_file(agent, run_of(agent), [{"item": "0" * 32, "grader": "g", "grades": {}}])
    other = declared(tmp_path / "second", agent, bad)
    found = written.grades_problems(load_agent(other))
    assert found == ["data/written_grades.jsonl:2: it leaves safe_to_send ungraded"]


# -- the command --------------------------------------------------------------------------------


def test_the_command_prints_the_report_and_writes_it_with_its_ledger(
    agent: Agent, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = declared(tmp_path, agent)
    out = tmp_path / "out" / "written.json"
    args = ["written", "eval", str(folder), "--rubric", "reply_quality", "--dataset", "written"]
    assert main([*args, "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "Written output: rubric reply_quality of mendari-test/written-mini@0.1.0" in text
    assert DEMONSTRATION in text and "graders: simulated-1 10" in text
    assert "Thank you" not in text and "lisinopril" not in text
    report = json.loads(out.read_text())
    assert report["format"] == "mendari.written/1" and report["cases"]["graded"] == 10
    assert report["result_sha256"].startswith("sha256:") and report["run"]["engine"]
    ledger_file = (
        out.parent / "ledgers" / (report["ledger"]["id"].removeprefix("sha256:") + ".jsonl")
    )
    assert WrittenLedger.loads(ledger_file.read_bytes()).id == report["ledger"]["id"]
    assert main([*args, "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["result_sha256"] == report["result_sha256"]
    assert "Thank you" not in json.dumps(printed) and "written-0" not in json.dumps(printed)
    # Without the agent's own file and without --grades: no clinician grade at all.
    assert (
        main(
            [
                "written",
                "eval",
                str(MINI),
                "--rubric",
                "reply_quality",
                "--dataset",
                "written",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["cases"]["graded"] == 0
    # --grades names another file; one of another rubric or dataset is refused.
    given = tmp_path / "given.jsonl"
    given.write_bytes(grades_file(agent, run_of(agent)))
    assert (
        main(
            [
                "written",
                "eval",
                str(MINI),
                "--rubric",
                "reply_quality",
                "--dataset",
                "written",
                "--grades",
                str(given),
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["cases"]["graded"] == 10
    for bad in (
        ["--rubric", "tone", "--dataset", "written"],
        ["--rubric", "draft_check", "--dataset", "written", "--grades", str(given)],
        ["--rubric", "reply_quality", "--dataset", "written", "--judge", "live"],
        ["--rubric", "reply_quality", "--dataset", "written", "--resamples", "0"],
        ["--rubric", "reply_quality", "--dataset", "written", "--grades", str(tmp_path / "none")],
    ):
        assert main(["written", "eval", str(MINI), *bad]) == 1
        assert "Thank you" not in capsys.readouterr().err


# -- what the review found ----------------------------------------------------------------------

SEVEN = {
    "levels": [f"l{n}" for n in range(7)],
    "scores": list(range(7)),
    "pass_from": "l3",
}


def test_a_scored_criterion_is_held_to_agreement_on_pass_or_fail_too() -> None:
    """The reviewer's case. Seven levels passing from `l3`; the judge gives `l3` wherever
    clinicians gave `l2`, and agrees everywhere else. Over the levels it is one step off on
    half the outputs: a weighted kappa near 0.8, which alone validated it, while clinicians pass
    a quarter of the outputs and the judge three quarters. On pass or fail it barely agrees,
    and it leans lenient: not validated, and the judged share is withheld."""
    rows: list[Row] = (
        of(["l0"], "l0", 50)
        + of(["l2"], "l3", 100)  # the judge is one level kinder, across the pass line
        + of(["l4"], "l4", 25)
        + of(["l6"], "l6", 25)
        + of([], "l3", 75)
        + of([], "l1", 25)
    )
    criterion = measured(rows, scale=SEVEN)
    judged = criterion["judge_agreement"]
    assert judged["kappa"]["interval"]["low"] >= 0.6  # over the levels: above the floor
    assert judged["pass_fail_kappa"]["estimate"] < 0.4  # on pass or fail: far under it
    assert judged["passing"] == {
        "clinicians": stats.proportion(50, 200).to_dict(),
        "judge": stats.proportion(150, 200).to_dict(),
    }
    assert judged["pass_fail"]["judge_only_pass"] == 100
    assert judged["symmetry"]["leaning"] == "lenient" and judged["symmetry"]["p"] < 1e-6
    assert criterion["validation"] == {
        "state": NOT_VALIDATED,
        "reasons": [
            "the lower bound of the judge's kappa with clinicians on pass or fail, which is "
            "what a pass share is about, is under the floor of 0.6",
            "the judge leans lenient: it passed 100 outputs clinicians failed and failed 0 they "
            "passed (exact McNemar test, p under 0.05)",
        ],
    }
    assert criterion["judged"] is None
    for mapping in everywhere(criterion):
        assert not (mapping.get("k") == 75 and mapping.get("n") == 100), mapping
    text = written_report_text(written.report(*constructed(rows, scale=SEVEN)))
    assert "the judge leans lenient" in text and "75 of 100" not in text
    assert "clinicians passed 50 of 200 [" in text and "the judge 150 of 200 [" in text
    # The same disagreement away from the pass line (`l4` where clinicians gave `l5`) changes
    # no one's pass or fail: both kappas clear the floor, and it is validated.
    away: list[Row] = (
        of(["l0"], "l0", 50)
        + of(["l2"], "l2", 50)
        + of(["l5"], "l4", 50)
        + of(["l6"], "l6", 50)
        + of([], "l3", 75)
        + of([], "l1", 25)
    )
    fine = measured(away, scale=SEVEN)
    assert fine["judge_agreement"]["pass_fail_kappa"]["estimate"] == 1.0
    assert fine["validation"] == {"state": VALIDATED, "reasons": []}
    assert fine["judged"]["passing"] == stats.proportion(75, 100).to_dict()


def test_a_judge_that_leans_strict_stays_eligible_and_is_flagged() -> None:
    rows = paired(60, 30, 14, 1) + UNGRADED  # it fails 14 the clinicians passed, passes 1
    ledger, grades = constructed(rows, floors={"agreement": 0.5})
    found = written.report(ledger, grades)
    criterion = found["criteria"]["quality"]
    assert criterion["judge_agreement"]["symmetry"]["leaning"] == "strict"
    assert criterion["validation"] == {"state": VALIDATED, "reasons": []}
    assert "the judge leans strict" in written_report_text(found)
    # The mirror image leans lenient, and is not validated for it.
    mirrored = measured(paired(60, 30, 1, 14) + UNGRADED, floors={"agreement": 0.5})
    assert mirrored["judge_agreement"]["symmetry"]["leaning"] == "lenient"
    assert mirrored["validation"]["state"] == NOT_VALIDATED and mirrored["judged"] is None


def test_the_symmetry_test_is_the_exact_binomial_one() -> None:
    from fractions import Fraction

    from scipy import stats as scipy_stats

    assert written.symmetry_p(0, 0) == 1 and written.symmetry_p(3, 3) == 1
    assert written.symmetry_p(5, 0) == Fraction(2, 32)  # 2 * (1/2)^5: not under 1/20
    assert written.symmetry_p(6, 0) == Fraction(2, 64)  # under it
    assert Fraction(1, 20) == written.SYMMETRY_ALPHA
    for b in range(0, 30, 3):
        for c in range(0, 30, 4):
            exact = written.symmetry_p(b, c)
            assert exact == written.symmetry_p(c, b)
            assert float(exact) == pytest.approx(stats.mcnemar_exact(b, c), abs=1e-15)
            if b + c:
                reference = scipy_stats.binomtest(min(b, c), b + c, 0.5).pvalue
                assert float(exact) == pytest.approx(reference, abs=1e-12)
    for bad in ((-1, 2), (True, 2), (1.0, 2)):
        with pytest.raises(ValueError):
            written.symmetry_p(*bad)


def test_a_state_is_never_decided_on_a_handful_of_resamples() -> None:
    """The reviewer's case: with one resample the "interval" is one draw, so a judge whose
    lower bound is under the floor is validated on most seeds, and an interval that is not
    estimable reads as one. Fewer than 2,000 is refused, whoever asks."""
    rows = paired(55, 25, 10, 10) + UNGRADED  # its bound at 2,000 resamples is under the floor
    ledger, grades = constructed(rows)
    assert written.report(ledger, grades)["criteria"]["quality"]["validation"]["state"] == (
        NOT_VALIDATED
    )
    for few in (1, 2, 50, 1999, 0, -5, 100_001, True, 2000.0):
        with pytest.raises(MendariError, match="the resamples are"):
            written.report(ledger, grades, resamples=few)  # type: ignore[arg-type]
        assert written.resamples_problem(few) is not None
    assert written.resamples_problem(2000) is None and written.resamples_problem(100_000) is None
    assert stats.RESAMPLES == 2000
    # No seed moves it at the resamples a report is decided on.
    states = {
        written.report(ledger, grades, seed=seed)["criteria"]["quality"]["validation"]["state"]
        for seed in range(6)
    }
    assert states == {NOT_VALIDATED}


def test_the_same_model_flag_reads_the_models_name_not_its_spelling() -> None:
    ledger, grades = constructed(paired(60, 36, 2, 2))
    for agent, judge, same in (
        ("vendor/claude-x", "claude-x", True),
        ("Claude-X", "other/route/claude-x", True),
        ("vendor/claude-x", "vendor/claude-y", False),
        ("claude-x-mini", "claude-x", False),
    ):
        header = {**ledger.header, "models": [agent, judge]}
        named = WrittenLedger(header, ledger.cases)
        assert written.report(named, grades)["judge"]["same_model_as_agent"] is same, (agent, judge)
