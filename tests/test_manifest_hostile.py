# SPDX-License-Identifier: Apache-2.0
"""Hostile or malformed manifests are refused as the agent's problem, named so its author can act,
never with a raw error or a stall (`core/agent.py`, `core/spec.py`, `core/patterns.py`)."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from mendari import load_agent
from mendari.core import patterns, re2_syntax
from mendari.errors import AgentValidationError
from tests.conftest import TRIAGE_DIR

SCHEMA_LINE = "schema: schemas/input.schema.json"


def _copy(tmp_path: Path) -> Path:
    root = tmp_path / "agent"
    shutil.copytree(TRIAGE_DIR, root)
    return root


def _replace(root: Path, old: str, new: str) -> None:
    manifest = root / "agent.yaml"
    text = manifest.read_text()
    assert old in text
    manifest.write_text(text.replace(old, new, 1))


def _edit_manifest(root: Path, edit: Callable[[dict[str, Any]], None]) -> None:
    manifest = root / "agent.yaml"
    raw = yaml.safe_load(manifest.read_text())
    edit(raw)
    manifest.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))


def _refused(root: Path) -> list[str]:
    with pytest.raises(AgentValidationError) as caught:
        load_agent(root)
    return caught.value.problems


# -- paths a file cannot have -----------------------------------------------------------------


def test_a_manifest_path_holding_a_nul_is_refused_naming_its_field(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    _replace(root, SCHEMA_LINE, 'schema: "schemas/in\\0put.schema.json"')
    assert _refused(root) == [
        "inputs.schema: path 'schemas/in\\x00put.schema.json' is not a path a file can have "
        "(ValueError)"
    ]


def test_a_manifest_path_too_long_for_the_filesystem_is_refused_shortened(
    tmp_path: Path,
) -> None:
    root = _copy(tmp_path)
    _replace(root, SCHEMA_LINE, f"schema: {'a' * 5_000}.json")
    (problem,) = _refused(root)
    assert problem.startswith(f"inputs.schema: path '{'a' * 80}'... (5,005 characters) cannot")
    assert len(problem) < 300  # the path is cut, never written out whole


# -- question shapes -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "field"),
    [
        ({"type": "boolean", "instructions": "Is it?", "criteria": ["yes"]}, "criteria"),
        ({"type": "boolean", "instructions": "Is it?", "criteria": 5}, "criteria"),
        ({"type": "score", "instructions": "How bad?", "levels": ["mild"]}, "levels"),
    ],
)
def test_a_question_whose_criteria_or_levels_are_not_a_mapping_is_named(
    tmp_path: Path, question: dict[str, Any], field: str
) -> None:
    root = _copy(tmp_path)

    def edit(raw: dict[str, Any]) -> None:
        raw["questions"]["extra"] = question
        raw["providers"]["router"]["answers"].append("extra")

    _edit_manifest(root, edit)
    problems = _refused(root)
    assert any(
        f"questions.extra.{field}: Input should be a valid dictionary" in p for p in problems
    )


# -- a pattern RE2 would be handed as gigabytes ------------------------------------------------


@pytest.mark.parametrize(
    "pattern", ["a{4294967294}", "(?:(?:(?:a{99999}){99999}){99999}){99999}", "a{1000001}"]
)
def test_a_bounded_pattern_past_re2s_size_is_refused_before_it_is_written_out(
    monkeypatch: pytest.MonkeyPatch, pattern: str
) -> None:
    """A count over RE2's 1,000 is written out as counts one after another, so these few
    characters would be written out as gigabytes (or, for the first, 2 s and 60 MB each)."""

    def never(*args: object) -> str:
        raise AssertionError("written out for RE2")

    monkeypatch.setattr(re2_syntax, "translate", never)
    patterns._re2.cache_clear()
    try:
        problem = patterns.problem(pattern, bounded=True)
    finally:
        patterns._re2.cache_clear()
    assert problem is not None and problem.startswith("too large for RE2")


def test_a_bounded_pattern_within_re2s_size_still_compiles() -> None:
    assert patterns.problem("a{500000}", bounded=True) is None
    assert re2_syntax.size("a{500000}") <= patterns.MAX_RE2_SIZE
