# SPDX-License-Identifier: Apache-2.0
"""The action other repositories check their agent with (action.yml,
docs/guides/evaluate-in-ci.md). GitHub Actions cannot run here, so two things stand in for a
run: the file is held to the rules the workflows are held to (no secret, each action pinned to
a commit, no expression pasted into a script), and its scripts are run as the runner runs
them, on the bundled demo agent and on a new one, with the install step's outputs given (the
environment the tests run in). CI's `action` job runs the whole of it, install included."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from mendari.cli import _command, build_parser, main
from mendari.core.pack import load_pack
from mendari.evals import resolve_dataset
from mendari.evals.gate import Limits
from tests.conftest import DEMO_AGENT_DIR, ROOT
from tests.test_supply_chain import COMMIT_PIN, USES_LINE, VERSION_COMMENT, WORKFLOWS
from tests.test_vocabulary import offences

ACTION = ROOT / "action.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"
EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
# Each limit the action takes, and the option of `mendari eval` it becomes: a result that does
# not meet one fails the job.
LIMITS = {
    "min-critical-detection": "--min-critical-detection",
    "max-critical-misses": "--max-critical-misses",
    "min-accuracy": "--min-accuracy",
    "max-false-automation": "--max-false-automation",
}
# Each target it takes, likewise: what a recommended automation threshold must keep to. A target
# is advice, and fails nothing.
TARGETS = {
    "target-critical-miss": "--target-critical-miss",
    "target-critical-miss-of": "--target-critical-miss-of",
    "target-false-automation": "--target-false-automation",
    "target-confidence": "--target-confidence",
}


def action() -> dict[str, Any]:
    loaded = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def steps() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = action()["runs"]["steps"]
    return found


# -- the file -------------------------------------------------------------------------------------


def test_it_is_a_composite_action_with_a_default_for_every_input() -> None:
    held = action()
    assert held["runs"]["using"] == "composite"
    assert set(held["inputs"]) == {
        "agent",
        "dataset",
        "provider",
        *LIMITS,
        "baseline",
        *TARGETS,
        "python-version",
        "artifact",
    }
    for name, given in held["inputs"].items():
        assert set(given) == {"description", "default"}, name  # none is required
        assert isinstance(given["default"], str), f"{name}: a default is text"
        assert len(given["description"].strip()) > 30, name
    defaults = {name: given["default"] for name, given in held["inputs"].items()}
    assert defaults["agent"] == "." and defaults["dataset"] == "examples"
    assert defaults["python-version"] == "3.12" and defaults["baseline"] == ""
    # By default no critical case may be automated, and nine in ten must be detected: what
    # Mendari asks of a contributed agent too (community/check.py).
    assert defaults["min-critical-detection"] == "90%" and defaults["max-critical-misses"] == "0"
    assert defaults["min-accuracy"] == defaults["max-false-automation"] == ""
    # No target unless the workflow gives one: a recommendation is asked for, never assumed.
    assert {defaults[name] for name in TARGETS} == {""}
    # The name says which an input is, and so does its description's first words.
    for name, given in held["inputs"].items():
        said = given["description"].strip()
        assert said.startswith("A limit: ") == (name in LIMITS), name
        assert name.startswith("target-") == (name in TARGETS), name
    for name in ("target-critical-miss", "target-false-automation"):
        said = held["inputs"][name]["description"].strip()
        assert said.startswith("A target, which never fails the job: "), name


def options_of(group: str) -> set[str]:
    """The options `mendari eval --help` lists under one heading."""
    command = _command(build_parser(), ["eval"])
    assert command is not None
    (found,) = [held for held in command._action_groups if held.title == group]
    return {option for action in found._group_actions for option in action.option_strings}


def test_each_limit_and_each_target_is_an_option_of_mendari_eval() -> None:
    """A limit is checked by the engine's gate, and a target read by its operating point
    (`mendari eval`), not by the action's script: an input has the name of its option."""
    (check,) = [step for step in steps() if step.get("id") == "check"]
    script = check["run"]
    # The command line's two groups are the action's two kinds of input, and the baseline.
    assert options_of("limits") == {*LIMITS.values(), "--no-worse-than"}
    assert options_of("targets") == set(TARGETS.values())
    for name, option in (LIMITS | TARGETS).items():
        assert option == f"--{name}"
        variable = name.upper().replace("-", "_")
        assert check["env"][variable] == f"${{{{ inputs.{name} }}}}"
        assert f'[ -z "${variable}" ] || evaluate+=({option} "${variable}")' in script
    for option in LIMITS.values():
        assert option.removeprefix("--").replace("-", "_") in Limits.__dataclass_fields__
    assert '[ -z "$BASELINE" ] || evaluate+=(--no-worse-than "$BASELINE")' in script
    for command in ('"$MENDARI" validate "$dir"', '"$MENDARI" test "$dir"', '"$MENDARI" eval '):
        assert command in script
    assert "--markdown" in script and '--out "$out/result.json"' in script


def test_every_action_it_uses_is_pinned_to_a_commit_the_workflows_pin() -> None:
    """As the workflows' are (tests/test_supply_chain.py), and to the same commits: one version
    of each action in the repository, which Dependabot moves together (it reads this file)."""
    pinned = {}
    for number, line in enumerate(ACTION.read_text().splitlines(), 1):
        match = USES_LINE.match(line)
        if not match:
            continue
        ref, rest = match.groups()
        assert COMMIT_PIN.match(ref), f"action.yml:{number}: {ref} is not pinned to a commit"
        assert VERSION_COMMENT.match(rest), f"action.yml:{number}: no '# vX.Y.Z' comment"
        pinned[ref.split("@")[0]] = ref
    used = [step["uses"] for step in steps() if "uses" in step]
    assert sorted(pinned.values()) == sorted(used)
    assert set(pinned) == {"astral-sh/setup-uv", "actions/upload-artifact"}
    elsewhere = {
        match.group(1)
        for path in WORKFLOWS
        for line in path.read_text().splitlines()
        if (match := USES_LINE.match(line))
    }
    assert set(pinned.values()) <= elsewhere


def test_it_installs_the_uv_the_workflows_install_and_the_locked_dependencies() -> None:
    (setup,) = [step for step in steps() if step.get("uses", "").startswith("astral-sh/setup-uv@")]
    versions = {
        step["with"]["version"]
        for path in WORKFLOWS
        for job in yaml.safe_load(path.read_text())["jobs"].values()
        for step in job.get("steps", [])
        if step.get("uses", "").startswith("astral-sh/setup-uv@")
    }
    assert {setup["with"]["version"]} == versions
    # No cache of the caller's is read or written, and the caller's Python is not set.
    assert setup["with"] == {"version": setup["with"]["version"], "enable-cache": False}
    (install,) = [step for step in steps() if step.get("id") == "install"]
    script = install["run"]
    assert 'uv venv --quiet --python "$PYTHON_VERSION" "$work/venv"' in script
    assert '--project "$ACTION_PATH" --frozen --no-dev --no-emit-project' in script
    assert '--require-hashes -r "$work/requirements.txt"' in script
    assert '--no-deps "$ACTION_PATH"' in script  # Mendari itself, from this checkout
    assert "pip install mendari" not in script  # never a name from an index
    # This action's checkout, read in the first step, before another action is used.
    first = steps()[0]
    assert first["id"] == "here" and "uses" not in first
    assert first["run"] == 'echo "path=$GITHUB_ACTION_PATH" >> "$GITHUB_OUTPUT"'
    assert install["env"]["ACTION_PATH"] == "${{ steps.here.outputs.path }}"
    assert "GITHUB_ACTION_PATH" not in script


def test_no_expression_is_pasted_into_a_script_and_no_secret_is_read() -> None:
    """An input can hold anything its workflow gives it: it reaches a script through `env`."""
    text = ACTION.read_text()
    assert "secrets." not in text and "github.token" not in text
    assert "pull_request_target" not in text
    for step in steps():
        assert "${{" not in step.get("run", ""), step.get("name")
        if "run" in step:
            assert step["shell"] == "bash", step.get("name")
    # Every expression is an input or an earlier step's output, whole.
    for expression in EXPRESSION.findall(text):
        assert re.fullmatch(r"(inputs\.[a-z-]+|steps\.[a-z]+\.outputs\.[a-z]+)", expression)


def test_it_says_agent() -> None:
    assert offences(ACTION.read_text()) == []


def test_the_job_fails_last_after_the_summary_and_the_artifact() -> None:
    """The check step records what it found and never fails; the upload comes next; only the
    last step fails the job. So a failing agent still has its summary and its result."""
    named = [step.get("id") or step.get("uses", "").split("@")[0] or "last" for step in steps()]
    assert named == [
        "here",
        "astral-sh/setup-uv",
        "install",
        "check",
        "actions/upload-artifact",
        "last",
    ]
    check, upload, last = steps()[3:]
    # The runner starts a step's bash with -e: the check step turns it off, and never exits.
    assert "set +e -uo pipefail\n" in check["run"] and "exit " not in check["run"]
    assert upload["if"] == "inputs.artifact != ''"
    assert upload["with"] == {
        "name": "${{ inputs.artifact }}",
        "path": "${{ steps.check.outputs.directory }}",
        "if-no-files-found": "ignore",
    }
    assert last["run"].rstrip().endswith('exit "$failed"')
    assert set(last["env"]) == {"VALIDATION", "TESTS", "EVALUATION"}
    outputs = action()["outputs"]
    assert set(outputs) == {"validation", "tests", "evaluation", "result", "summary", "mendari"}


# -- its scripts, run as the runner runs them -----------------------------------------------------


class Run:
    """One use of the action: its outputs, its summary, and whether its last step failed."""

    def __init__(self, outputs: dict[str, str], summary: str, log: str, failed: bool):
        self.outputs, self.summary, self.log, self.failed = outputs, summary, log, failed


def use(tmp_path: Path, **inputs: str) -> Run:
    """The action's `run` steps after the install, each as GitHub runs a composite's bash step
    (`bash --noprofile --norc -e -o pipefail`), with `inputs` over the defaults (an input given
    as empty is empty, as on GitHub) and the install step's outputs given: the `mendari` of the
    environment the tests run in, and a work folder."""
    held = action()
    given = {name: spec["default"] for name, spec in held["inputs"].items()} | {
        name.replace("_", "-"): value for name, value in inputs.items()
    }
    assert set(given) == set(held["inputs"]), "an input the action does not take"
    tmp_path.mkdir(parents=True, exist_ok=True)
    work = tmp_path / "work"
    work.mkdir()
    mendari = Path(sys.executable).parent / "mendari"
    known = {"install": {"mendari": str(mendari), "work": str(work)}}
    summary_file = tmp_path / "summary.md"
    summary_file.touch()
    log = ""
    failed = False

    def value(expression: str) -> str:
        kind, _, rest = expression.partition(".")
        if kind == "inputs":
            return given[rest]
        step, _, output = rest.partition(".outputs.")
        return known.get(step, {}).get(output, "")

    for number, step in enumerate(held["runs"]["steps"]):
        if "run" not in step or step.get("id") == "install" or failed:
            continue
        script = tmp_path / f"step-{number}.sh"
        script.write_text(step["run"], encoding="utf-8")
        output_file = tmp_path / f"output-{number}"
        output_file.touch()
        # A runner's environment, with none of the settings of this machine's Mendari.
        env = {
            **{name: os.environ[name] for name in ("PATH", "HOME", "TMPDIR") if name in os.environ},
            "MENDARI_AGENTS_PATH": "",
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_ACTION_PATH": str(ROOT),
            "GITHUB_OUTPUT": str(output_file),
            "GITHUB_STEP_SUMMARY": str(summary_file),
            **{
                name: EXPRESSION.sub(lambda match: value(match.group(1)), text)
                for name, text in step.get("env", {}).items()
            },
        }
        done = subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", str(script)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        log += done.stdout + done.stderr
        failed = done.returncode != 0
        lines = output_file.read_text(encoding="utf-8").splitlines()
        known[step.get("id", "last")] = dict(line.split("=", 1) for line in lines)
    return Run(known["check"], summary_file.read_text(encoding="utf-8"), log, failed)


pytestmark_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

DEMO = {"agent": "agents/portal-message-agent", "dataset": "test"}
# Limits the demo agent meets: its canned agent detects 81 of 114 urgent messages (71%), and
# nine of the others are automated (nine critical misses). And a target none of its thresholds
# keeps to, which must fail nothing. CI's `action` job states the same.
WITHIN = {
    "min_critical_detection": "70%",
    "max_critical_misses": "9",
    "min_accuracy": "90%",
    "max_false_automation": "5%",
    "target_critical_miss": "1%",
}


@pytestmark_bash
def test_a_use_within_its_limits_passes_and_writes_the_summary_and_the_result(
    tmp_path: Path,
) -> None:
    run = use(tmp_path / "within", **DEMO, **WITHIN)
    assert not run.failed, run.log
    assert run.outputs["validation"] == run.outputs["tests"] == "passed"
    assert run.outputs["evaluation"] == "passed"
    summary = run.summary
    assert summary.startswith("## Mendari agent check: passed\n")
    assert "Validation: **passed**. Behaviour tests: **passed**. Evaluation: **passed**." in summary
    assert "    ok  mendari/portal-message-agent@" in summary
    assert "    3 passed, 0 failed" in summary and "PASS " not in summary
    assert "### Evaluation of `mendari/portal-message-agent`" in summary
    assert "| Outcome `AUTO` | 326 of 600 [50.3-58.4%] |" in summary
    assert "| Critical `urgent_symptoms=yes` detected | 81 of 114 [61.8-79.2%] |" in summary
    assert "### Gate passed" in summary
    assert "| false automation (of labelled cases) | at most 5% | 9 of 600 [0.7-2.8%] | met |" in (
        summary
    )
    assert "| critical urgent_symptoms=yes misses | at most 9 | 9 of 114 [3.7-14.5%] | met |" in (
        summary
    )
    # The operating point, between the figures and the gate: the target is one no threshold of
    # this agent keeps to, the summary says so, and the job passes all the same.
    figures, _, rest = summary.partition("\n### Operating point\n")
    point, _, gate = rest.partition("\n### Gate passed\n")
    assert "| Figure | Result |" in figures and gate.strip()
    assert point.startswith(
        "\n**No automation threshold can be certified for critical misses at most 1% with 95% "
        "confidence.**\n\n- At the agent's automation threshold 0.90, 326 of 600 cases are "
        "automated: critical urgent_symptoms=yes automated 9 of 114 critical cases (at most "
        "13.4%), "
    )
    assert "\n- The threshold cannot fix this: at 0.95, the strictest threshold" in point
    assert Path(run.outputs["summary"]).read_text() == summary
    # What the artifact holds: the result, its ledger and the summary, and nothing else.
    directory = Path(run.outputs["directory"])
    assert Path(run.outputs["result"]) == directory / "result.json"
    held = sorted(path.relative_to(directory).parts[0] for path in directory.rglob("*"))
    assert sorted(set(held)) == ["ledgers", "result.json", "summary.md"]
    # Cases are named by id, and nothing written or printed holds a case's text.
    assert (
        "Critical misses: `urgent_symptoms=yes` cases automated, so no person read them (9): "
        "`demo-0002`" in summary
    )
    dataset = resolve_dataset(load_pack(DEMO_AGENT_DIR), "test")
    written = summary + run.log + (directory / "result.json").read_text()
    for record in dataset.records:
        assert record["input"]["message"]["body"] not in written, record["id"]

    # The same result as the baseline, and no limit stated: no worse than itself.
    again = use(
        tmp_path / "against",
        **DEMO,
        baseline=run.outputs["result"],
        min_critical_detection="",
        max_critical_misses="",
    )
    assert not again.failed, again.log
    assert again.outputs["evaluation"] == "passed"
    assert "| Metric | baseline | this change |" in again.summary
    assert "Nothing is worse than the baseline." in again.summary
    # With no target, the operating point is the bound at the agent's own threshold.
    assert (
        "\n### Operating point\n\n**At the agent's automation threshold 0.90, 326 of 600 cases "
        "are automated: " in again.summary
    )
    assert "No automation threshold can be certified" not in again.summary


@pytestmark_bash
def test_a_use_with_the_default_limits_fails_the_demo_agent_for_the_limits_alone(
    tmp_path: Path,
) -> None:
    """The demo agent misses urgent messages on purpose, and the limits the action states by
    default catch it. CI's `action` job expects this failure, and checks it is this one."""
    run = use(tmp_path, **DEMO)
    assert run.failed
    assert run.outputs["validation"] == run.outputs["tests"] == "passed"
    assert run.outputs["evaluation"] == "failed"
    assert run.summary.startswith("## Mendari agent check: failed\n")
    assert "### Gate failed: 2 limits not met" in run.summary
    assert "::error title=Mendari::The evaluation does not meet a stated limit" in run.log
    assert run.log.count("::error") == 1
    assert Path(run.outputs["result"]).is_file()  # the result is there to upload all the same


@pytestmark_bash
def test_a_new_agent_passes_as_scaffolded_and_with_another_provider(tmp_path: Path) -> None:
    agent = tmp_path / "my-agent"
    assert main(["new", str(agent), "--id", "my-clinic/my-agent"]) == 0
    run = use(tmp_path / "default", agent=str(agent))
    assert not run.failed, run.log
    assert "### Evaluation of `my-clinic/my-agent` `0.1.0` on `examples` (12 cases)" in run.summary
    assert "Providers: `reader=agent`." in run.summary
    rules = use(tmp_path / "rules", agent=str(agent), provider="reader=rules\n", artifact="")
    assert not rules.failed, rules.log
    assert "Providers: `reader=rules`." in rules.summary


@pytestmark_bash
def test_what_cannot_be_checked_fails_and_says_why(tmp_path: Path) -> None:
    missing = use(tmp_path / "missing", agent="no/such/folder")
    assert missing.failed and missing.outputs["validation"] == "failed"
    assert missing.outputs["tests"] == missing.outputs["evaluation"] == "skipped"
    assert "the `agent` input names no directory" in missing.summary
    assert "result" not in missing.outputs
    assert "::error title=Mendari::The agent is not valid" in missing.log

    unknown = use(tmp_path / "unknown", **(DEMO | {"dataset": "no-such-set"}))
    assert unknown.failed and unknown.outputs["evaluation"] == "error"
    assert unknown.outputs["tests"] == "passed"
    assert "    error: dataset 'no-such-set' is neither a file nor an agent dataset" in (
        unknown.summary
    )
    assert "::error title=Mendari::The evaluation could not run" in unknown.log

    # An input is data: a word of it is never run, expanded or taken for an option's value.
    marker = tmp_path / "ran"
    hostile = use(tmp_path / "hostile", **DEMO, provider=f"$(touch {marker}) `touch {marker}` *")
    assert hostile.failed and not marker.exists()
    assert hostile.outputs["tests"] == "failed" and hostile.outputs["evaluation"] == "error"


# -- CI's own run of it ---------------------------------------------------------------------------


def test_ci_runs_the_action_as_another_repository_would_and_expects_one_failure() -> None:
    job = yaml.safe_load(CI.read_text())["jobs"]["action"]
    assert "if" not in job and "permissions" not in job  # read-only, as the workflow is
    uses = {step["id"]: step for step in job["steps"] if step.get("uses") == "./"}
    assert list(uses) == ["within", "against", "strict"]
    assert all(step["with"]["agent"] == "agents/portal-message-agent" for step in uses.values())
    assert all(step["with"]["dataset"] == "test" for step in uses.values())
    names = [step["with"]["artifact"] for step in uses.values()]
    assert len(set(names)) == 3  # one artifact name is taken once in a run
    # The limits the demo agent meets, as the test above states them.
    within = {name.replace("-", "_"): value for name, value in uses["within"]["with"].items()}
    assert {name: within[name] for name in WITHIN} == WITHIN
    assert "continue-on-error" not in uses["within"] and "continue-on-error" not in uses["against"]
    assert uses["against"]["with"]["baseline"] == "${{ steps.within.outputs.result }}"
    assert uses["against"]["with"]["min-critical-detection"] == ""
    assert uses["against"]["with"]["max-critical-misses"] == ""
    # The third use states no limit of its own, so the action's defaults apply: it must fail.
    assert set(uses["strict"]["with"]) == {"agent", "dataset", "artifact"}
    assert uses["strict"]["continue-on-error"] is True
    (expected,) = [step for step in job["steps"] if "steps.strict.outcome" in str(step.get("env"))]
    assert expected["env"] == {
        "OUTCOME": "${{ steps.strict.outcome }}",
        "VALIDATION": "${{ steps.strict.outputs.validation }}",
        "TESTS": "${{ steps.strict.outputs.tests }}",
        "EVALUATION": "${{ steps.strict.outputs.evaluation }}",
    }
    for check in (
        'test "$OUTCOME" = failure',
        'test "$VALIDATION" = passed',
        'test "$TESTS" = passed',
        'test "$EVALUATION" = failed',
    ):
        assert check in expected["run"], check
    assert "if" not in expected and "continue-on-error" not in expected
    assert job["steps"].index(expected) > job["steps"].index(uses["strict"])
