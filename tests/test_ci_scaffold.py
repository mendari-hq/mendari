# SPDX-License-Identifier: Apache-2.0
"""The workflow `mendari new` writes into a new agent's folder, and the guide that explains it
(docs/guides/evaluate-in-ci.md). The workflow uses Mendari's action (action.yml) at the tag of
the release installed, is safe on a pull request from a fork, and is no part of the agent: no
digest or archive holds it. The guide's few lines are that workflow, and what it says of the
action's inputs and summary is what the action takes and writes."""

from __future__ import annotations

import importlib.util
import re
import shutil
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from mendari import __version__
from mendari.cli import CI_GUIDE, CI_TEMPLATE, CI_WORKFLOW, main
from mendari.core.pack import load_pack, pack_digest
from mendari.hub import archive
from tests.conftest import ROOT
from tests.test_ci_action import CI, DEMO, action, pytestmark_bash, use
from tests.test_supply_chain import COMMIT_PIN, USES_LINE, VERSION_COMMENT
from tests.test_vocabulary import offences

GUIDE = ROOT / "docs" / "guides" / "evaluate-in-ci.md"
ACTION_AT_THIS_RELEASE = f"mendari-hq/mendari@v{__version__}"
_BLOCK = re.compile(r"^```(\w+)\n(.*?)^```$", re.M | re.S)


def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def scaffolded(tmp_path: Path) -> Path:
    agent = tmp_path / "my-agent"
    assert main(["new", str(agent), "--id", "my-clinic/my-agent"]) == 0
    return agent


def triggers(workflow: dict[Any, Any]) -> Any:
    return workflow.get("on", workflow.get(True))  # YAML 1.1 reads a bare `on` as true


def blocks(language: str) -> list[str]:
    """The guide's fenced blocks in one language, in reading order."""
    found = _BLOCK.findall(GUIDE.read_text(encoding="utf-8"))
    return [text for name, text in found if name == language]


# -- the workflow ---------------------------------------------------------------------------------


def test_new_writes_a_workflow_that_uses_the_action_at_this_releases_tag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    agent = scaffolded(tmp_path)
    written = agent / CI_WORKFLOW
    assert written == agent / ".github" / "workflows" / "mendari.yml"
    said = capsys.readouterr().out.splitlines()[-1]
    assert said == f"On every change: {written} runs these checks on GitHub ({CI_GUIDE})"
    text = written.read_text(encoding="utf-8")
    assert "{{" not in text  # nothing left to fill in, and no expression
    workflow = yaml.safe_load(text)
    (job,) = workflow["jobs"].values()
    checkout, check = job["steps"]
    assert check["uses"] == ACTION_AT_THIS_RELEASE
    # The tag is the one a release of this version is made from (scripts/check_release.py).
    assert _script("check_release").TAG.match(ACTION_AT_THIS_RELEASE.partition("@")[2])
    # What it gives the action: inputs the action takes, the folder the file is in, a dataset
    # the new agent declares, and the action's own limits, written out to be changed.
    inputs = action()["inputs"]
    assert check["with"] == {
        "agent": ".",
        "dataset": "examples",
        "min-critical-detection": "90%",
        "max-critical-misses": "0",
    }
    assert all(check["with"][name] == inputs[name]["default"] for name in check["with"])
    assert check["with"]["dataset"] in load_pack(agent).manifest.evaluation.datasets
    assert checkout["uses"].startswith("actions/checkout@")


def test_the_workflow_says_what_it_does_and_where_it_belongs(tmp_path: Path) -> None:
    text = (scaffolded(tmp_path) / CI_WORKFLOW).read_text(encoding="utf-8")
    # Every comment of the file: its heading, and the ones beside the inputs.
    comment = "\n".join(line.strip() for line in text.splitlines() if line.lstrip().startswith("#"))
    assert comment.startswith("# Mendari checks my-clinic/my-agent on every change")
    for said in (
        "its manifest is validated, its behaviour tests run, and it is evaluated",
        "cases named by id and never by their text",
        "each critical label and the operating point of the automation threshold",
        "the job fails when a check fails, a limit below is not met or the evaluation",
        "none of its cases is automated (a critical miss)",
        "No secret is needed",
        CI_GUIDE,
        "move the file to that repository's .github/workflows/ and name the folder in `agent`",
    ):
        assert said in comment.replace("\n# ", " "), said
    assert offences(text) == []


def test_the_workflow_is_safe_on_a_pull_request_from_a_fork(tmp_path: Path) -> None:
    """As Mendari's own workflows are (tests/test_workflow_safety.py, test_supply_chain.py):
    no trigger that runs a fork's change with secrets, a read-only token, no secret, no tag
    push, and each action pinned: Mendari's to this release's tag, the checkout to the commit
    Mendari's own workflows pin, leaving no credentials behind."""
    text = (scaffolded(tmp_path) / CI_WORKFLOW).read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    assert triggers(workflow) == {"pull_request": None, "push": {"branches": ["main"]}}
    assert workflow["permissions"] == {"contents": "read"}
    (job,) = workflow["jobs"].values()
    assert "permissions" not in job and "environment" not in job
    assert "secrets." not in text and "pull_request_target" not in text
    assert all("run" not in step for step in job["steps"])  # nothing of its own to run
    used = [match for line in text.splitlines() if (match := USES_LINE.match(line))]
    assert [match.group(1) for match in used] == [step["uses"] for step in job["steps"]]
    (checkout,) = [match for match in used if match.group(1).startswith("actions/checkout@")]
    assert COMMIT_PIN.match(checkout.group(1)) and VERSION_COMMENT.match(checkout.group(2))
    assert checkout.group(0).strip() in {line.strip() for line in CI.read_text().splitlines()}, (
        "the checkout in src/mendari/templates/ci/mendari.yml is not the commit the workflows "
        "pin: Dependabot does not read the template, so move it with them by hand"
    )
    assert job["steps"][0]["with"] == {"persist-credentials": False}


def test_the_workflow_is_no_part_of_the_agent(tmp_path: Path) -> None:
    """`mendari validate` passes with it, and the agent's digest and its signed archive are
    what they are without it: a hidden folder is not the agent's."""
    agent = scaffolded(tmp_path)
    assert main(["validate", str(agent)]) == 0
    with_it = pack_digest(agent)
    assert not [path for path in archive.pack_files(agent) if ".github" in path.parts]
    shutil.rmtree(agent / ".github")
    assert pack_digest(agent) == with_it
    assert main(["validate", str(agent)]) == 0


def test_a_community_agent_gets_no_workflow(tmp_path: Path) -> None:
    """It lives in Mendari's repository, whose own workflow checks it."""
    agent = tmp_path / "community-agents" / "sorter"
    assert main(["new", str(agent), "--community"]) == 0
    assert not (agent / ".github").exists()


def test_the_template_is_in_no_folder_a_release_refuses() -> None:
    """A wheel, an sdist and an image hold no `.github/` at any depth
    (scripts/check_excluded.py): the template is kept outside one, and written into one."""
    relative = CI_TEMPLATE.relative_to(ROOT).as_posix()
    assert relative == "src/mendari/templates/ci/mendari.yml" and CI_TEMPLATE.is_file()
    assert _script("check_excluded").excluded([relative]) == []
    assert "{{version}}" in CI_TEMPLATE.read_text() and "{{id}}" in CI_TEMPLATE.read_text()


@pytestmark_bash
def test_the_action_passes_a_new_agent_with_what_its_workflow_gives(tmp_path: Path) -> None:
    agent = scaffolded(tmp_path)
    workflow = yaml.safe_load((agent / CI_WORKFLOW).read_text(encoding="utf-8"))
    given = workflow["jobs"]["agent"]["steps"][1]["with"]
    inputs = {name.replace("-", "_"): value for name, value in given.items()}
    run = use(tmp_path / "use", **(inputs | {"agent": str(agent)}))  # `.`, from the workspace
    assert not run.failed, run.log
    assert run.summary.startswith("## Mendari agent check: passed\n")
    assert "### Gate passed" in run.summary


# -- the guide ------------------------------------------------------------------------------------


def test_the_guide_is_where_the_command_and_the_workflow_say() -> None:
    prefix = "https://github.com/mendari-hq/mendari/blob/main/"
    assert CI_GUIDE.startswith(prefix) and ROOT / CI_GUIDE.removeprefix(prefix) == GUIDE
    assert GUIDE.is_file() and offences(GUIDE.read_text(encoding="utf-8")) == []


def test_the_guides_few_lines_are_the_workflow_new_writes(tmp_path: Path) -> None:
    written = yaml.safe_load((scaffolded(tmp_path) / CI_WORKFLOW).read_text(encoding="utf-8"))
    assert yaml.safe_load(blocks("yaml")[0]) == written


def test_every_use_in_the_guide_is_this_release_and_a_pin_the_workflows_have() -> None:
    lines = [line for block in blocks("yaml") for line in block.splitlines()]
    used = [match for line in lines if (match := USES_LINE.match(line))]
    ours = [match.group(1) for match in used if match.group(1).startswith("mendari-hq/")]
    assert len(ours) >= 5 and set(ours) == {ACTION_AT_THIS_RELEASE}, (
        "docs/guides/evaluate-in-ci.md names another release than the version in pyproject.toml"
    )
    others = {match.group(0).strip() for match in used} - {f"- uses: {ACTION_AT_THIS_RELEASE}"}
    assert others and others <= {line.strip() for line in CI.read_text().splitlines()}, (
        "docs/guides/evaluate-in-ci.md pins an action to another commit than the workflows do: "
        "Dependabot does not read the guide, so move it with them by hand"
    )


def test_the_guide_names_every_input_with_its_default_and_every_output() -> None:
    guide = GUIDE.read_text(encoding="utf-8")
    held = action()
    for name, given in held["inputs"].items():
        default = f"`{given['default']}`" if given["default"] else ""
        assert f"| `{name}` | {default}" in guide, name
    for name in held["outputs"]:
        assert f"`{name}`" in guide, name
    # A step's inputs in the guide are inputs the action takes.
    for block in blocks("yaml"):
        for step in _steps(yaml.safe_load(block)):
            if step.get("uses") == ACTION_AT_THIS_RELEASE:
                assert set(step["with"]) <= set(held["inputs"]), step["with"]


def _steps(loaded: Any) -> list[dict[str, Any]]:
    """The steps of a workflow, or of a part of one a block shows (a job's steps, a list)."""
    if isinstance(loaded, list):
        return loaded
    if "jobs" in loaded:
        return [step for job in loaded["jobs"].values() for step in job["steps"]]
    return list(loaded.get("steps", []))


@pytestmark_bash
def test_the_summary_the_guide_shows_is_the_one_the_action_writes(tmp_path: Path) -> None:
    """The demo agent, held to the action's own limits: each heading and each figure the guide
    shows is in the summary the action writes for it."""
    summary = use(tmp_path, **DEMO).summary
    (shown,) = [block for block in blocks("text") if "Mendari agent check" in block]
    headings = [line for line in shown.splitlines() if line.startswith("#")]
    assert headings == [
        "## Mendari agent check: failed",
        "### Evaluation of mendari/portal-message-agent 0.1.2 on test (600 cases)",
        "### Operating point",
        "### Gate failed: 2 limits not met",
    ]
    plain = summary.replace("`", "").replace("**", "")  # as the page renders it
    # The operating point, whole: the guide shows the sentences the action writes, wrapped.
    point = " ".join(shown.split("### Operating point")[1].split("### Gate failed")[0].split())
    written = " ".join(plain.split("### Operating point")[1].split("### Gate failed")[0].split())
    assert point == written
    for heading in headings:
        assert f"\n{heading}\n" in f"\n{plain}", heading
    figures = re.findall(r"\d+ of \d+ \[[^\]]+\]", shown)
    assert len(figures) > 10
    for figure in figures:
        assert figure in summary, figure
    for line in ("Validation: passed. Behaviour tests: passed. Evaluation: failed.",):
        assert line in plain
    assert "(9): demo-0002, demo-0044, " in plain and "(24): demo-0052, " in plain
    for line in shown.splitlines():  # each kind of critical case, under the name it has
        if line.startswith("Critical "):
            assert line.split(": demo-")[0] in plain, line
    assert "| critical urgent_symptoms=yes misses" in shown and "| not met |" in shown


@pytestmark_bash
def test_the_guides_target_is_one_the_action_takes_and_fails_nothing(tmp_path: Path) -> None:
    """The guide's target step, on the demo agent with no limit: the summary says which
    threshold the cases support, or why none, and the job passes."""
    (step,) = [
        step
        for block in blocks("yaml")
        for step in _steps(yaml.safe_load(block))
        if any(name.startswith("target-") for name in step.get("with", {}))
    ]
    targets = {
        name.replace("-", "_"): value
        for name, value in step["with"].items()
        if name.startswith("target-")
    }
    assert targets == {"target_critical_miss": "1%", "target_false_automation": "2%"}
    run = use(tmp_path, **DEMO, **targets, min_critical_detection="", max_critical_misses="")
    assert not run.failed, run.log
    assert run.outputs["evaluation"] == "passed"
    assert (
        "**No automation threshold can be certified for critical misses at most 1% and false "
        "automation at most 2% with 95% confidence.**" in run.summary
    )
    assert "### Gate" not in run.summary  # no limit, no baseline: nothing to hold it to
