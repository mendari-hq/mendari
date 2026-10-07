# SPDX-License-Identifier: Apache-2.0
"""The local loop cannot pass by accident (LP-15): `mendari validate` reads every dataset a pack
declares (JSON per line, each input against the schema, labels the pack's own); `mendari eval`
refuses a dataset whose labels the pack does not have instead of crashing; `mendari test` on a
pack with no behaviour tests fails; and a typed provider setting that applies to nothing in the
pack is an error wherever it is typed."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from mendari.cli import main
from tests.conftest import INBOX_DIR

SECRET = "Jane Example, born 1950"  # case text: never in a message


@pytest.fixture
def pack(tmp_path: Path) -> Path:
    root = tmp_path / "inbox"
    shutil.copytree(INBOX_DIR, root)
    return root


def add_row(pack: Path, row: str) -> None:
    dataset = pack / "data" / "red_flags_holdout.jsonl"
    dataset.write_text(dataset.read_text() + row + "\n")


VALID = {"message": {"body": SECRET}}


@pytest.mark.parametrize(
    ("row", "problem"),
    [
        ("{not json", "data/red_flags_holdout.jsonl:31: invalid JSON"),
        (
            json.dumps({"id": "r1", "input": {"message": {"body": 5, "x": SECRET}}}),
            "case r1: input",
        ),
        (
            json.dumps({"id": "r2", "input": VALID, "labels": {"urgency": SECRET}}),
            "case r2: labels.urgency is not one of its labels (urgent, routine)",
        ),
        (
            json.dumps({"id": "r3", "input": VALID, "labels": {SECRET: "calm"}}),
            "case r3: labels name a question the pack does not have",
        ),
        (
            json.dumps({"id": "r4", "input": VALID, "labels": ["urgent"]}),
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
    pack: Path, row: str, problem: str, capsys: pytest.CaptureFixture[str]
) -> None:
    add_row(pack, row)
    assert main(["validate", str(pack)]) == 1
    err = capsys.readouterr().err
    lines = err.splitlines()
    assert lines and all(line.startswith("error: ") for line in lines)
    assert any(problem in line for line in lines), err
    assert SECRET not in err and "Traceback" not in err


def test_every_shipped_dataset_validates() -> None:
    for manifest in sorted(Path(INBOX_DIR).parent.glob("*/pack.yaml")):
        assert main(["validate", str(manifest.parent)]) == 0, manifest.parent.name


def test_eval_refuses_labels_the_pack_does_not_have(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An undeclared label crashed the report (KeyError) and a list of labels raised
    AttributeError: now one line, before any decision is made."""
    site = tmp_path / "site.jsonl"
    site.write_text(json.dumps({"id": "s1", "input": VALID, "labels": {"urgency": SECRET}}) + "\n")
    assert main(["eval", "mendari/patient-inbox-router", "--dataset", str(site)]) == 1
    (line,) = capsys.readouterr().err.splitlines()
    # The label's value is the site's own text: never repeated, only what the pack defines.
    assert line == "error: site case s1: labels.urgency is not one of its labels (urgent, routine)"


def test_a_pack_with_no_behaviour_tests_fails_its_test_run(
    pack: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (pack / "tests" / "cases.yaml").write_text("[]\n")
    assert main(["test", str(pack)]) == 1
    assert "no behaviour tests to run: add cases to tests/cases.yaml" in capsys.readouterr().err
    manifest = yaml.safe_load((pack / "pack.yaml").read_text())
    del manifest["tests"]
    (pack / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    assert main(["test", str(pack)]) == 1
    assert "no behaviour tests to run" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["test", "mendari/patient-inbox-router", "--provider", "routr=llm"],
        ["decide", "mendari/patient-inbox-router", "--text", "hi", "--provider", "routr=llm"],
        [
            "eval",
            "mendari/patient-inbox-router",
            "--dataset",
            "red_flags",
            "--provider",
            "routr=llm",
        ],
        [
            "leaderboard",
            "mendari/patient-inbox-router",
            "--run",
            "routr=llm",
            "--dataset",
            "red_flags",
        ],
        ["test", "mendari/patient-inbox-router", "--provider", "mendari/ai-guard:router=llm"],
    ],
    ids=["test", "decide", "eval", "leaderboard", "another-packs-setting"],
)
def test_a_typed_provider_setting_that_applies_to_nothing_is_an_error(
    argv: list[str], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    if argv[0] == "leaderboard":
        argv = [*argv, "--out", str(tmp_path / "leaderboard.json")]
    assert main(argv) == 1
    err = capsys.readouterr().err
    assert "error: mendari/patient-inbox-router: provider setting" in err
    assert "Traceback" not in err
