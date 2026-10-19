# SPDX-License-Identifier: Apache-2.0
"""The Inferno checker (scripts/inferno_check.py, the verdict of conformance.yml's `dtr-payer`):
a run passes only when every test gives the result its expected results list, so a new failure,
a known gap that now passes, a test the run skipped over and a test nobody listed all fail. The
expected results themselves name a reason for every result that is not a pass, and the kit,
suite and groups the workflow runs. No Inferno and no Docker here: the runs are fixtures."""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "inferno_check.py"
EXPECTED = ROOT / "sandbox" / "inferno" / "dtr_payer_server_v220_expected.json"
WORKFLOW = ROOT / ".github" / "workflows" / "conformance.yml"
DOCKERFILE = ROOT / "sandbox" / "inferno" / "Dockerfile"


def _script() -> Any:
    spec = importlib.util.spec_from_file_location("inferno_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _script()
SUITE = "dtr_payer_server_v220"
FOUND = f"{SUITE}-dtr_payer_server_v220_questionnaire_operations-dtr_payer_v220_error_handling-"
GOLDEN: dict[str, Any] = {
    "kit": "davinci-dtr-test-kit",
    "version": "0.18.0",
    "commit": "1b76dd3f565433a3157808da2d6d0c08a797b2f0",
    "suite": SUITE,
    "groups": ["1", "3", "4"],
    "tests": [
        {
            "id": f"{FOUND}dtr_server_v220_payer_questionnaire_not_found",
            "short_id": "3.6.01",
            "title": "Questionnaire package response warns of an unknown Questionnaire",
            "result": "pass",
            "requirements": ["hl7.fhir.us.davinci-dtr_2.2.0@oper-9"],
        },
        {
            "id": f"{FOUND}dtr_v220_payer_invalid_questionnaire_response",
            "short_id": "3.6.03",
            "title": "Invalid QuestionnaireResponses return 400 with an OperationOutcome",
            "result": "fail",
            "requirements": ["hl7.fhir.us.davinci-dtr_2.2.0@spec-147"],
            "reason": "Mendari serves no $next-question",
        },
        {
            "id": f"{SUITE}-dtr_payer_v220_next_question_support-next_question_validation",
            "short_id": "3.3.01",
            "title": "$next-question responses are valid",
            "result": "omit",
            "requirements": [],
            "reason": "no adaptive form, so no $next-question request is made",
        },
    ],
}
NOT_FOUND, INVALID, NEXT = (entry["id"] for entry in GOLDEN["tests"])


def output(results: dict[str, str], *, log: bool = True) -> str:
    """What `inferno execute --outputter json` prints: log lines, then the results (the
    suite's and a group's included), pretty-printed."""
    entries: list[dict[str, Any]] = [
        {"id": "r0", "test_suite_id": SUITE, "result": "fail"},
        {"id": "r1", "test_group_id": f"{SUITE}-dtr_payer_v220_error_handling", "result": "fail"},
    ]
    entries += [
        {"id": f"r{n}", "test_id": test, "result": result, "messages": [{"type": "info"}]}
        for n, (test, result) in enumerate(results.items(), start=2)
    ]
    lines = "W, [2026-10-18T19:23:54.323096 #1]  WARN -- : Unknown input `url` for a group\n"
    return (lines if log else "") + json.dumps(entries, indent=2) + "\n"


def run(tmp_path: Path, results: dict[str, str], golden: dict[str, Any] = GOLDEN) -> int:
    raw = tmp_path / "results.json"
    raw.write_text(output(results))
    expected = tmp_path / "expected.json"
    expected.write_text(json.dumps(golden))
    return int(check.main([str(raw), "--expected", str(expected)]))


AS_EXPECTED = {NOT_FOUND: "pass", INVALID: "fail", NEXT: "omit"}


def test_a_run_that_gives_every_expected_result_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(tmp_path, AS_EXPECTED) == 0
    printed = capsys.readouterr().out
    assert "3.6.01     pass" in printed and "3.6.03     fail" in printed
    assert "every test gave its expected result" in printed
    assert "%" not in printed  # each test's outcome, never a pass rate


def test_a_new_failure_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(tmp_path, {**AS_EXPECTED, NOT_FOUND: "fail"}) == 1
    assert "3.6.01: fail, expected pass (a new failure)" in capsys.readouterr().err
    assert run(tmp_path, {**AS_EXPECTED, NOT_FOUND: "error"}) == 1  # Inferno's own error too


def test_an_unexpected_pass_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A known gap that closes is recorded in the expected results, by the change that closed
    it: the list only shrinks by a decision."""
    assert run(tmp_path, {**AS_EXPECTED, INVALID: "pass"}) == 1
    err = capsys.readouterr().err
    assert "3.6.03: passes, expected fail (Mendari serves no $next-question)" in err
    assert run(tmp_path, {**AS_EXPECTED, NEXT: "pass"}) == 1
    assert run(tmp_path, {**AS_EXPECTED, NEXT: "skip"}) == 1  # any other change, too


def test_an_unknown_test_id_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    unknown = f"{SUITE}-dtr_payer_v220_error_handling-a_test_added_by_a_new_kit"
    assert run(tmp_path, {**AS_EXPECTED, unknown: "pass"}) == 1
    assert f"{unknown}: pass, but the expected results do not list this test" in (
        capsys.readouterr().err
    )


def test_a_test_the_run_did_not_execute_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(tmp_path, {NOT_FOUND: "pass", INVALID: "fail"}) == 1
    assert "3.3.01: expected omit, not run" in capsys.readouterr().err


def test_a_run_that_stopped_before_any_test_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = tmp_path / "results.json"
    expected = tmp_path / "expected.json"
    expected.write_text(json.dumps(GOLDEN))
    stopped = 'W, [2026] WARN -- : Unknown input\n"Missing the following required inputs: x"\n'
    for text, said in (
        (stopped, "Inferno stopped before any test ran: Missing the following required inputs"),
        ("", "no Inferno results found"),
        ("[\n]\n", "the run has no test results"),
    ):
        raw.write_text(text)
        assert check.main([str(raw), "--expected", str(expected)]) == 1
        assert said in capsys.readouterr().err


def test_the_results_are_read_after_the_log_lines() -> None:
    assert check.run_results(output(AS_EXPECTED)) == AS_EXPECTED
    assert check.run_results(output(AS_EXPECTED, log=False)) == AS_EXPECTED


@pytest.mark.parametrize(
    ("change", "said"),
    [
        (lambda e: e.pop("reason"), "a fail needs its reason"),
        (lambda e: e.update(reason="  "), "a fail needs its reason"),
        (lambda e: e.update(requirements=[]), "a fail needs the requirement ids"),
        (lambda e: e.update(requirements="spec-147"), "requirements must be a list"),
        (lambda e: e.update(result="error"), "is not one of pass, fail, skip, omit"),
    ],
)
def test_every_result_but_a_pass_says_why(change: Any, said: str) -> None:
    golden = copy.deepcopy(GOLDEN)
    change(golden["tests"][1])
    with pytest.raises(check.CheckError, match=re.escape(said)):
        check.expected_results(golden)


def test_the_expected_results_name_their_kit_and_each_test_once() -> None:
    golden = copy.deepcopy(GOLDEN)
    golden["tests"].append(golden["tests"][0])
    with pytest.raises(check.CheckError, match="listed twice"):
        check.expected_results(golden)
    with pytest.raises(check.CheckError, match="lack commit"):
        check.expected_results({**GOLDEN, "commit": ""})


def test_writing_keeps_each_reason_whose_result_did_not_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = tmp_path / "results.json"
    raw.write_text(output({**AS_EXPECTED, NEXT: "skip"}))
    expected = tmp_path / "expected.json"
    expected.write_text(json.dumps(GOLDEN))
    # The changed result has no reason yet: written, and named as needing one.
    assert check.main([str(raw), "--expected", str(expected), "--write"]) == 1
    assert "a skip needs its reason" in capsys.readouterr().err
    written = {entry["id"]: entry for entry in json.loads(expected.read_text())["tests"]}
    assert written[INVALID]["reason"] == "Mendari serves no $next-question"
    assert written[NEXT]["reason"] == "" and written[NEXT]["result"] == "skip"
    assert "reason" not in written[NOT_FOUND]
    assert [e["short_id"] for e in written.values()] == ["3.3.01", "3.6.01", "3.6.03"]


# -- the expected results this repository runs against ---------------------------------------


def _expected() -> dict[str, Any]:
    loaded = json.loads(EXPECTED.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_the_dtr_expected_results_are_valid_and_name_the_pinned_kit() -> None:
    golden = _expected()
    entries = check.expected_results(golden)
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert f"ARG KIT_COMMIT={golden['commit']}\n" in dockerfile
    assert golden["suite"] == SUITE and golden["groups"] == ["1", "3", "4"]
    assert golden["version"] == "0.18.0"
    # Every test of groups 1, 3 and 4 is listed, with its short id and the kit's requirements.
    shorts = [entry["short_id"] for entry in entries.values()]
    assert len(shorts) == len(set(shorts)) and {s.split(".")[0] for s in shorts} == {"1", "3", "4"}
    for entry in entries.values():
        assert all(r.startswith("hl7.fhir.us.davinci-dtr_2.2.0@") for r in entry["requirements"])
        assert entry["id"].startswith(f"{SUITE}-")


def test_the_dtr_error_handling_dv_a_fixed_passes() -> None:
    """DV-a's fixes, as Inferno sees them: the unknown questionnaire's warning (oper-9) and the
    4xx for source data that names no decision (spec-130)."""
    by_short = {entry["short_id"]: entry for entry in _expected()["tests"]}
    assert by_short["3.6.01"]["result"] == "pass"
    assert by_short["3.6.02"]["result"] == "pass"


def test_the_workflow_runs_what_the_expected_results_list() -> None:
    golden = _expected()
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    runs = "\n".join(step.get("run", "") for step in workflow["jobs"]["dtr-payer"]["steps"])
    groups = " ".join(golden["groups"])
    assert f"--suite {golden['suite']} --groups {groups}" in runs
    assert f"list_tests.rb {golden['suite']} {groups}" in runs
    assert "--expected sandbox/inferno/dtr_payer_server_v220_expected.json" in runs
