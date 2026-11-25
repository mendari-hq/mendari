# SPDX-License-Identifier: Apache-2.0
"""The local loop cannot pass by accident (LP-15): `mendari validate` reads every dataset an agent
declares (JSON per line, each input against the schema, labels the agent's own); `mendari eval`
refuses a dataset whose labels the agent does not have instead of crashing; `mendari test` on an
agent with no behaviour tests fails; and a typed provider setting that applies to nothing in the
agent is an error wherever it is typed."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from mendari.cli import main
from tests.conftest import FIXTURE_AGENTS, TRIAGE_DIR

SECRET = "Jane Example, born 1950"  # case text: never in a message


@pytest.fixture
def agent(tmp_path: Path) -> Path:
    root = tmp_path / "triage"
    shutil.copytree(TRIAGE_DIR, root)
    return root


def add_row(agent: Path, row: str) -> None:
    dataset = agent / "data" / "critical.jsonl"  # 40 cases
    dataset.write_text(dataset.read_text() + row + "\n")


VALID = {"message": {"body": SECRET}}


@pytest.mark.parametrize(
    ("row", "problem"),
    [
        ("{not json", "data/critical.jsonl:41: invalid JSON"),
        (
            json.dumps({"id": "r1", "input": {"message": {"body": 5, "x": SECRET}}}),
            "case r1: input",
        ),
        (
            json.dumps({"id": "r2", "input": VALID, "labels": {"urgency": SECRET}}),
            "case r2: labels.urgency is not one of its labels (yes, no)",
        ),
        (
            json.dumps({"id": "r3", "input": VALID, "labels": {SECRET: "calm"}}),
            "case r3: labels name a question the agent does not have",
        ),
        (
            json.dumps({"id": "r4", "input": VALID, "labels": ["yes"]}),
            "case r4: `labels` must be a mapping of question to label",
        ),
        (
            json.dumps({"id": "r5", "input": VALID, "attributes": "es"}),
            "case r5: `attributes` must be a mapping",
        ),
    ],
    ids=[
        "not-json",
        "input-off-schema",
        "undeclared-label",
        "unknown-question",
        "labels-list",
        "attributes",
    ],
)
def test_validate_reads_every_declared_dataset(
    agent: Path, row: str, problem: str, capsys: pytest.CaptureFixture[str]
) -> None:
    add_row(agent, row)
    assert main(["validate", str(agent)]) == 1
    err = capsys.readouterr().err
    lines = err.splitlines()
    assert lines and all(line.startswith("error: ") for line in lines)
    assert any(problem in line for line in lines), err
    assert SECRET not in err and "Traceback" not in err


def test_every_shipped_dataset_validates() -> None:
    for manifest in sorted(FIXTURE_AGENTS.glob("*/agent.yaml")):
        assert main(["validate", str(manifest.parent)]) == 0, manifest.parent.name


def test_eval_refuses_labels_the_agent_does_not_have(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An undeclared label crashed the report (KeyError) and a list of labels raised
    AttributeError: now one line, before any decision is made."""
    site = tmp_path / "site.jsonl"
    site.write_text(json.dumps({"id": "s1", "input": VALID, "labels": {"urgency": SECRET}}) + "\n")
    assert main(["eval", "mendari-test/triage", "--dataset", str(site)]) == 1
    (line,) = capsys.readouterr().err.splitlines()
    # The label's value is the site's own text: never repeated, only what the agent defines.
    assert line == "error: site case s1: labels.urgency is not one of its labels (yes, no)"


def test_an_agent_with_no_behaviour_tests_fails_its_test_run(
    agent: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (agent / "tests" / "cases.yaml").write_text("[]\n")
    assert main(["test", str(agent)]) == 1
    assert "no behaviour tests to run: add cases to tests/cases.yaml" in capsys.readouterr().err
    manifest = yaml.safe_load((agent / "agent.yaml").read_text())
    del manifest["tests"]
    (agent / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    assert main(["test", str(agent)]) == 1
    assert "no behaviour tests to run" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["test", "mendari-test/triage", "--provider", "routr=llm"],
        ["decide", "mendari-test/triage", "--text", "hi", "--provider", "routr=llm"],
        [
            "eval",
            "mendari-test/triage",
            "--dataset",
            "critical",
            "--provider",
            "routr=llm",
        ],
        [
            "leaderboard",
            "mendari-test/triage",
            "--run",
            "routr=llm",
            "--dataset",
            "critical",
        ],
        ["test", "mendari-test/triage", "--provider", "mendari-test/clinical-review:reader=rules"],
    ],
    ids=["test", "decide", "eval", "leaderboard", "another-agents-setting"],
)
def test_a_typed_provider_setting_that_applies_to_nothing_is_an_error(
    argv: list[str], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    if argv[0] == "leaderboard":
        argv = [*argv, "--out", str(tmp_path / "leaderboard.json")]
    assert main(argv) == 1
    err = capsys.readouterr().err
    assert "error: mendari-test/triage: provider setting" in err
    assert "Traceback" not in err
