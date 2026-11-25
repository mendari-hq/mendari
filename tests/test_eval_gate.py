# SPDX-License-Identifier: Apache-2.0
"""What a CI job asks of `mendari eval` (docs/guides/evaluate-in-ci.md): a result that does not
meet a stated limit, or is worse than an earlier one through the eval gate, is exit 2; and the
result as Markdown for a job summary, with cases named by id and never by their text.

A limit names a figure as a target does (tests/test_operating_point.py): a critical miss is a
critical label's case that is automated, and false automation is a share of the labelled cases."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from mendari import Decision, load_agent
from mendari.cli import EXIT_DEGRADED, main
from mendari.evals import resolve_dataset
from mendari.evals.gate import (
    FALSE_AUTOMATION,
    NO_CRITICAL_LABEL,
    NOT_THE_SAME_CASES,
    Limits,
    baseline_problem,
    check,
    limits,
)
from mendari.evals.harness import Evaluation, evaluation
from mendari.evals.ledger import case_hash
from mendari.evals.render import NAMED_CASES, code, md_row, report_markdown
from mendari.evals.result import critical_cases
from tests.conftest import DEMO_AGENT_DIR

AGENT = "my-clinic/my-agent"
CRITICAL = "critical urgent_symptoms=yes"
# The limits the action states unless its workflow says otherwise (action.yml).
STRICT = ["--min-critical-detection", "90%", "--max-critical-misses", "0"]


def result(
    *,
    accuracy: float | None = 0.9,
    labelled: int = 100,
    false_automation: float | None = 0.02,
    automated: int = 50,
    critical: tuple[int, int, int] | None = (8, 10, 0),
) -> dict[str, Any]:
    """The parts of an evaluation result the gate reads; `critical` is one label's detected,
    cases and automated. `false_automation` is the share of the `automated` cases that are
    wrong, as a result states it; a limit holds those wrong cases to the `labelled` ones."""
    found = []
    if critical is not None:
        detected, cases, auto = critical
        found.append(
            {
                "question": "urgent_symptoms",
                "label": "yes",
                "n": cases,
                "detected": detected,
                "detection_rate": detected / cases if cases else None,
                "auto_missed": auto,
            }
        )
    questions = {}
    if accuracy is not None:
        questions["request"] = {"accuracy": accuracy, "n": labelled}
    return {
        "agent": {"id": AGENT, "version": "0.1.0"},
        "dataset": {"name": "examples", "sha256": "a" * 64, "n": labelled, "limit": None},
        "primary": "request",
        "questions": questions,
        "decisions": {
            "automation": {"false_automation_rate": false_automation, "n": automated},
            "critical": found,
        },
    }


# -- stated limits --------------------------------------------------------------------------------


def test_no_limit_stated_checks_nothing() -> None:
    assert not Limits().stated and Limits(max_critical_misses=0).stated
    assert limits(result(), Limits()) == ([], [])


def test_a_limit_is_met_at_its_value_and_not_met_past_it() -> None:
    every = Limits(
        min_accuracy=0.9,
        max_false_automation=0.01,
        min_critical_detection=0.8,
        max_critical_misses=0,
    )
    rows, failures = limits(result(), every)
    assert failures == []
    assert [(row["metric"], row["limit"], row["measured"], row["met"]) for row in rows] == [
        ("request accuracy", "at least 90%", "90 of 100 [82.4-95.1%]", True),
        (FALSE_AUTOMATION, "at most 1%", "1 of 100 [0.03-5.4%]", True),
        (f"{CRITICAL} detected", "at least 80%", "8 of 10 [44.4-97.5%]", True),
        (f"{CRITICAL} misses", "at most 0", "0 of 10 [0.0-30.8%]", True),
    ]
    worse = result(accuracy=0.89, false_automation=0.04, critical=(7, 10, 1))
    rows, failures = limits(worse, every)
    assert [row["met"] for row in rows] == [False, False, False, False]
    assert failures == [
        "request accuracy: 89 of 100 [81.2-94.4%], and the limit is at least 90%",
        f"{FALSE_AUTOMATION}: 2 of 100 [0.2-7.0%], and the limit is at most 1%",
        f"{CRITICAL} detected: 7 of 10 [34.8-93.3%], and the limit is at least 80%",
        f"{CRITICAL} misses: 1 of 10 [0.3-44.5%], and the limit is at most 0",
    ]


def test_false_automation_is_a_share_of_the_labelled_cases_as_the_report_counts_it() -> None:
    """The limit is on the report's own "False automation" row, which a target is on too
    (`operating_point.Tally.wrong` of `labelled`): never on the share of the automated cases,
    which the baseline's gate compares and names so."""
    assert FALSE_AUTOMATION == "false automation (of labelled cases)"
    one_in_ten_automated = result(false_automation=0.1, automated=10, labelled=100)
    (row,), failures = limits(one_in_ten_automated, Limits(max_false_automation=0.02))
    assert (row["measured"], row["met"], failures) == ("1 of 100 [0.03-5.4%]", True, [])
    # A report that states the proportion is read from it, not worked out.
    stated = {**one_in_ten_automated, "proportions": {"false_automation": {"k": 3, "n": 100}}}
    (row,), _ = limits(stated, Limits(max_false_automation=0.02))
    assert (row["measured"], row["met"]) == ("3 of 100 [0.6-8.5%]", False)


def test_a_stated_share_is_shown_as_it_was_stated() -> None:
    rows, _ = limits(result(), Limits(min_accuracy=0.925, max_false_automation=0.005))
    assert [row["limit"] for row in rows] == ["at least 92.5%", "at most 0.5%"]


def test_a_share_of_no_case_meets_no_limit_and_a_count_of_none_is_within_any() -> None:
    """No labelled case, no case of the critical label: nothing shows a limit on a share is met,
    a floor or a ceiling. A limit on a count is met by none."""
    empty = result(accuracy=None, false_automation=None, automated=0, critical=(0, 0, 0))
    every = Limits(
        min_accuracy=0.5,
        max_false_automation=0.0,
        min_critical_detection=0.5,
        max_critical_misses=0,
    )
    rows, failures = limits(empty, every)
    assert [(row["measured"], row["met"]) for row in rows] == [
        ("not measured", False),
        ("not measured", False),
        ("no cases", False),
        ("no cases", True),
    ]
    assert failures == [
        "request accuracy: not measured, and the limit is at least 50%",
        f"{FALSE_AUTOMATION}: not measured, and the limit is at most 0%",
        f"{CRITICAL} detected: no cases, and the limit is at least 50%",
    ]
    # Labelled cases and none automated: nothing went wrong, and that is measured.
    quiet = result(false_automation=None, automated=0)
    (row,), failures = limits(quiet, Limits(max_false_automation=0.0))
    assert (row["measured"], row["met"], failures) == ("0 of 100 [0.0-3.6%]", True, [])


def test_limits_on_critical_cases_say_so_when_the_agent_declares_none() -> None:
    checked = check(result(critical=None), Limits(min_critical_detection=0.9))
    assert checked.passed and checked.limits == [] and checked.notes == [NO_CRITICAL_LABEL]
    assert NO_CRITICAL_LABEL in checked.markdown() and NO_CRITICAL_LABEL in checked.text()
    # It passed, and says that it checked nothing: never that a limit was met.
    assert "No stated limit applies and no baseline was given: nothing was checked." in (
        checked.markdown()
    )
    assert "limit is met" not in checked.markdown()
    assert check(result(critical=None), Limits(min_accuracy=0.5)).notes == []


# -- a baseline -----------------------------------------------------------------------------------


def test_an_unchanged_result_is_no_worse_than_itself() -> None:
    checked = check(result(), Limits(), result(), 0.03)
    assert checked.passed and checked.compared and checked.regressions == []
    assert [row["metric"] for row in checked.rows] == [
        "request accuracy",
        "false automation rate (of automated)",
        f"{CRITICAL} detected",
    ]
    assert checked.verdict == "passed"
    assert "| Metric | baseline | this change |" in checked.markdown()
    assert "Nothing is worse than the baseline." in checked.markdown()


def test_a_result_worse_than_its_baseline_fails_through_the_eval_gate() -> None:
    worse = result(accuracy=0.8, critical=(7, 10, 1))
    checked = check(worse, Limits(max_critical_misses=0), result(), 0.03)
    assert not checked.passed
    assert checked.regressions == [
        "request accuracy fell by more than 3%",
        f"{CRITICAL} detected: fewer critical cases found",
        "critical urgent_symptoms=yes: a critical case was automated",
    ]
    assert checked.verdict == "failed: 1 limit not met, 3 regressions"
    markdown = checked.markdown()
    assert markdown.startswith("### Gate failed: 1 limit not met, 3 regressions\n")
    assert "- **Regression:** request accuracy fell by more than 3%" in markdown
    assert f"| {CRITICAL} misses | at most 0 | 1 of 10 [0.3-44.5%] | **not met** |" in markdown
    text = checked.text()
    assert "against the baseline (tolerance 3%): REGRESSION" in text
    assert text.endswith("Gate failed: 1 limit not met, 3 regressions")
    # A limit is never "missed": that word is a critical case's, which no person read.
    assert "missed" not in markdown.lower() and "missed" not in text.lower()


def test_within_the_agents_tolerance_is_no_regression() -> None:
    slightly = result(accuracy=0.88)
    assert check(slightly, Limits(), result(), 0.03).passed
    assert not check(slightly, Limits(), result(), 0.01).passed


def test_a_baseline_of_other_cases_is_not_compared_and_that_fails() -> None:
    """Another dataset, another version of it, or its first cases only: rates of different
    cases say nothing about a change, so nothing shows the run is no worse."""
    for other in ({"sha256": "b" * 64}, {"limit": 50}):
        baseline = result()
        baseline["dataset"].update(other)
        checked = check(result(), Limits(), baseline, 0.03)
        assert checked.regressions == [NOT_THE_SAME_CASES] and checked.rows == []
        assert checked.verdict == "failed: 1 regression"


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (lambda b: b["agent"].update(id="other/agent"), "a result of another agent"),
        (lambda b: b.pop("decisions"), "not an evaluation result"),
        (lambda b: b.update(questions=[]), "not an evaluation result"),
        (lambda b: b["questions"]["request"].update(accuracy="high"), "its figures"),
        (lambda b: b["questions"]["request"].update(accuracy=1.5), "its figures"),
        (lambda b: b["questions"]["request"].update(n=-1), "its figures"),
        (lambda b: b["decisions"].update(automation=[]), "its figures"),
        (lambda b: b["decisions"]["automation"].update(n=True), "its figures"),
        (lambda b: b["decisions"].update(critical={}), "its figures"),
        (lambda b: b["decisions"]["critical"][0].update(detection_rate=float("nan")), "its fig"),
        (lambda b: b["decisions"]["critical"][0].update(auto_missed=None), "its figures"),
        # A name a manifest could not hold would be written into the summary as it is.
        (lambda b: b["decisions"]["critical"][0].update(label="yes](https://x)"), "its figures"),
        (lambda b: b["decisions"]["critical"][0].update(question="a|b"), "its figures"),
    ],
)
def test_a_baseline_is_checked_before_it_is_compared(change: Any, problem: str) -> None:
    baseline = result()
    assert baseline_problem(baseline, AGENT, "request") is None
    change(baseline)
    found = baseline_problem(baseline, AGENT, "request")
    assert found is not None and found.startswith(problem)
    assert baseline_problem(["not", "a", "result"], AGENT, "request") is not None


def test_a_baseline_without_the_primary_question_or_critical_labels_is_one() -> None:
    assert baseline_problem(result(accuracy=None, critical=None), AGENT, "request") is None


# -- the result as Markdown -----------------------------------------------------------------------


def test_a_name_from_outside_is_shown_as_code_never_rendered() -> None:
    assert code("demo-0002") == "`demo-0002`"
    assert code("a `b` c") == "`a 'b' c`"  # it cannot end the code
    assert code("two\nlines\tand  spaces") == "`two lines and spaces`"
    assert code("x" * 200) == "`" + "x" * 79 + "…`"
    assert code("") == "(empty)" and code(17) == "`17`"
    assert code("<img src=x onerror=alert(1)>") == "`<img src=x onerror=alert(1)>`"
    # A table's cell does not end at a `|` of its own, in code or out of it.
    assert md_row("a|b", code("c|d")) == "| a\\|b | `c\\|d` |"


@pytest.fixture(scope="module")
def demo() -> Evaluation:
    """The bundled demo agent on its 600-case set: its canned agent misses urgent messages on
    purpose, and nine of those are automated."""
    agent = load_agent(DEMO_AGENT_DIR)
    return evaluation(Decision(agent, origin_cap=False), resolve_dataset(agent, "test"))


def test_the_critical_cases_named_are_the_ones_the_result_counts(demo: Evaluation) -> None:
    (counted,) = demo.report["decisions"]["critical"]
    (named,) = critical_cases(demo.ledger)
    assert len(named["automated"]) == counted["auto_missed"] == 9
    assert len(named["undetected"]) == 24
    assert not set(named["automated"]) & set(named["undetected"])
    # Every case of either kind was not detected here (the policy sends a detected one to a
    # person), so together they are the result's missed cases.
    assert set(named["automated"]) | set(named["undetected"]) == set(counted["missed_cases"])


def test_the_markdown_is_the_reports_figures(demo: Evaluation) -> None:
    markdown = report_markdown(demo.report)
    assert markdown.startswith(
        "### Evaluation of `mendari/portal-message-agent` `0.3.0` on `test` (600 cases)\n"
    )
    for row in (
        "| Request accuracy | 549 of 600 [89.0-93.6%] |",
        "| Outcome `AUTO` | 326 of 600 [50.3-58.4%] |",
        "| Outcome `HUMAN_REVIEW` | 274 of 600 [41.6-49.7%] |",
        "| Outcome `ABSTAIN` | 0 of 600 [0.0-0.6%] |",
        "| Outcome `BLOCK` | 0 of 600 [0.0-0.6%] |",
        "| Automated, correct | 317 of 326 [94.8-98.7%] |",
        "| False automation | 9 of 600 [0.7-2.8%] of labelled cases |",
        "| Critical `urgent_symptoms=yes` detected | 81 of 114 [61.8-79.2%] |",
        "| Critical `urgent_symptoms=yes` automated | 9 of 114 [3.7-14.5%] |",
    ):
        assert row in markdown, row
    assert "Providers: `reader=agent`. Answered by `mock-portal-agent`." in markdown
    assert "cases automated" not in markdown  # no ledger given: no case is named


def test_the_markdown_names_cases_by_id_and_at_most_twenty_of_a_kind(demo: Evaluation) -> None:
    agent = load_agent(DEMO_AGENT_DIR)
    dataset = resolve_dataset(agent, "test")
    ids = {case_hash(dataset.case_key, record["id"]): record["id"] for record in dataset.records}
    named = critical_cases(demo.ledger)
    markdown = report_markdown(demo.report, named, ids)
    # Each kind under the name it has: an automated critical case is a critical miss, and one
    # that was not detected and that a person read is not.
    assert (
        "\nCritical misses: `urgent_symptoms=yes` cases automated, so no person read them (9): "
        "`demo-0002`, `demo-0044`, "
    ) in markdown
    (listed,) = [line for line in markdown.splitlines() if "read by a person (24)" in line]
    assert listed.startswith("Critical `urgent_symptoms=yes` cases not detected, and read by a ")
    assert listed.count("`demo-") == NAMED_CASES and listed.endswith(" and 4 more")
    # Without the ids, which only the dataset's holder has: the ledger's own names.
    assert f"`{named[0]['automated'][0]}`" in report_markdown(demo.report, named)


def test_the_markdown_renders_nothing_a_dataset_or_a_model_says() -> None:
    """A dataset's name and ids, and the model an agent says answered, are someone else's
    text: each is shown as code, on its line, and no cell of a table ends early."""
    agent = load_agent(DEMO_AGENT_DIR)
    run = evaluation(Decision(agent, origin_cap=False), resolve_dataset(agent, "examples"))
    report = copy.deepcopy(run.report)
    hostile = "![x](https://evil.example/x.png) | <script>`\nnext"
    report["dataset"]["name"] = hostile
    report["models"] = {"request": {hostile: 12}}
    report["errors"] = {hostile: 3}
    report["decisions"]["critical"][0]["auto_missed"] = 1
    markdown = report_markdown(report, [{"automated": ["h1"], "undetected": []}], {"h1": hostile})
    shown = "`![x](https://evil.example/x.png) \\| <script>' next`"
    unescaped = shown.replace("\\|", "|")
    assert f" on {unescaped} (12 cases)" in markdown  # the heading: not a table
    assert f"Answered by {unescaped}." in markdown
    assert f"| Provider errors | {shown}: 3 |" in markdown
    assert markdown.rstrip().endswith(f"(1): {unescaped}")
    assert "\nnext" not in markdown


# -- the command ----------------------------------------------------------------------------------


def scaffold(tmp_path: Path) -> Path:
    target = tmp_path / "my-agent"
    assert main(["new", str(target), "--id", AGENT]) == 0
    return target


def test_eval_holds_a_result_to_its_limits_and_a_miss_is_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert EXIT_DEGRADED == 2  # what a CI job tells from an error (1)
    agent = scaffold(tmp_path)
    capsys.readouterr()
    command = ["eval", str(agent), "--dataset", "examples", "--baseline", "none"]
    assert main([*command, *STRICT]) == 0  # a new agent meets what a CI job asks by default
    out = capsys.readouterr().out
    assert f"  {CRITICAL} detected  3 of 3 [29.2-100.0%], limit at least 90%: met" in out
    assert f"  {CRITICAL} misses    0 of 3 [0.0-70.8%], limit at most 0: met" in out
    assert out.rstrip().endswith("Gate passed")
    assert main(command) == 0  # no gate asked for: none printed
    assert "Gate" not in capsys.readouterr().out
    # The demo agent's first hundred cases: urgent messages it misses are automated.
    demo = ["eval", str(DEMO_AGENT_DIR), "--dataset", "test", "--limit", "100"]
    assert main([*demo, "--baseline", "none", "--max-critical-misses", "0"]) == EXIT_DEGRADED
    out = capsys.readouterr().out
    assert "limit at most 0: NOT MET" in out
    assert out.rstrip().endswith("Gate failed: 1 limit not met")


def test_eval_on_the_demo_agent_fails_the_limits_and_names_the_cases_by_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The demo agent misses urgent messages on purpose: the limits a CI job states by default
    fail it, the summary says which cases by id, and nothing printed holds a case's text."""
    out = tmp_path / "result.json"
    command = ["eval", str(DEMO_AGENT_DIR), "--dataset", "test", "--baseline", "none"]
    assert main([*command, "--markdown", "--out", str(out), *STRICT]) == EXIT_DEGRADED
    printed = capsys.readouterr()
    markdown = printed.out
    assert "### Gate failed: 2 limits not met" in markdown
    assert "automated, so no person read them (9): `demo-0002`, " in markdown
    assert "Report written" not in markdown and printed.err == ""
    dataset = resolve_dataset(load_agent(DEMO_AGENT_DIR), "test")
    for record in dataset.records:
        assert record["input"]["message"]["body"] not in markdown, record["id"]
    # The result file is what it is without a gate: the verdict is in the summary and the exit.
    report = json.loads(out.read_text())
    assert "gate" not in report and "comparison" not in report
    assert (out.parent / "ledgers").is_dir()
    # Against itself, with limits it meets: no regression, exit 0; and `--out` may be the
    # baseline's own file, read before it is written.
    relaxed = ["--min-critical-detection", "0.7", "--max-critical-misses", "9"]
    again = [*command, "--no-worse-than", str(out), "--out", str(out), *relaxed]
    assert main([*again, "--markdown"]) == 0
    markdown = capsys.readouterr().out
    assert "### Gate passed" in markdown
    assert "Every stated limit is met, and nothing is worse than the baseline." in markdown
    assert json.loads(out.read_text())["result_sha256"] == report["result_sha256"]


def test_eval_with_json_keeps_the_verdict_out_of_the_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    agent = scaffold(tmp_path)
    capsys.readouterr()
    command = ["eval", str(agent), "--dataset", "examples", "--baseline", "none", "--json"]
    assert main([*command, "--max-false-automation", "0%", "--min-accuracy", "0.5"]) == 0
    printed = capsys.readouterr()
    assert json.loads(printed.out)["format"] == "mendari.eval/1"
    assert printed.err.rstrip().endswith("Gate passed")


def test_eval_refuses_what_cannot_be_a_limit_or_a_baseline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    agent = scaffold(tmp_path)
    command = ["eval", str(agent), "--dataset", "examples"]
    other = tmp_path / "other.json"
    other.write_text(json.dumps({**result(), "agent": {"id": "other/agent"}}))
    not_json = tmp_path / "notes.txt"
    not_json.write_text("not a result")
    capsys.readouterr()
    for extra, said in (
        (
            ["--min-accuracy", "90"],
            "argument --min-accuracy: a share is from 0 to 100%: write 90% or 0.9: '90'",
        ),
        (["--max-false-automation", "nan"], "argument --max-false-automation: a share is from"),
        (["--max-false-automation", "two"], "argument --max-false-automation: a share is written"),
        (["--min-critical-detection", "-0.1"], "argument --min-critical-detection: a share is"),
        (["--max-critical-misses", "1.5"], "argument --max-critical-misses: not a whole"),
        # A target's former names are no option: a limit is never taken for one.
        (["--max-critical-miss", "1%"], "argument --max-critical-misses: not a whole number"),
        (["--confidence-level", "95%"], "unrecognized arguments: --confidence-level 95%"),
        (["--json", "--markdown"], "choose --json or --markdown, not both"),
        (["--no-worse-than", str(other)], f"{other}: a result of another agent, not of {AGENT}"),
        (["--no-worse-than", str(not_json)], f"--no-worse-than {not_json}: not valid JSON"),
    ):
        assert main([*command, *extra]) == 1, extra
        (line,) = capsys.readouterr().err.splitlines()
        assert said in line, line


def test_a_baseline_is_refused_before_a_shared_run_asks_for_its_ticket(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ticket with no report stays counted against a site for good: a run that cannot go on
    takes none."""
    agent = scaffold(tmp_path)
    other = tmp_path / "other.json"
    other.write_text(json.dumps({**result(), "agent": {"id": "other/agent"}}))
    asked = []
    monkeypatch.setattr("mendari.cli._ticket_for", lambda *args: asked.append(args))
    monkeypatch.setattr("mendari.cli._site_configuration", lambda *args, **kw: asked.append(args))
    capsys.readouterr()
    command = ["eval", str(agent), "--dataset", "examples", "--share"]
    assert main([*command, "--no-worse-than", str(other)]) == 1
    (line,) = capsys.readouterr().err.splitlines()
    assert "a result of another agent" in line and asked == []
