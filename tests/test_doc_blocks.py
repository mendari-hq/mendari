# SPDX-License-Identifier: Apache-2.0
"""Every shell and Python block the docs show (README.md, docs/index.md, docs/guides/api.md) is
either run in CI on a clean runner, the same text as a snippet CI runs, or skipped for a reason a
reviewer accepted (LP-15, `scripts/doc_blocks.py`). A block without its marker fails here, and the
/docs landing shows only snippet steps, what one prints, and its setup."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari.server import snippets
from mendari.server.app import create_app

ROOT = Path(__file__).resolve().parents[1]


def _script() -> Any:
    spec = importlib.util.spec_from_file_location("doc_blocks", ROOT / "scripts" / "doc_blocks.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


doc_blocks = _script()
BLOCKS = doc_blocks.every()


def test_the_blocks_are_found() -> None:
    assert len(BLOCKS) > 20
    assert {block.file for block in BLOCKS} == set(doc_blocks.DOCS)


@pytest.mark.parametrize("block", BLOCKS, ids=lambda block: block.where)
def test_every_shell_and_python_block_has_its_marker(block: Any) -> None:
    assert block.lang in doc_blocks.ALLOWED, (
        f"{block.where}: write ```{block.lang} as one of {sorted(doc_blocks.ALLOWED)}"
    )
    if block.lang not in doc_blocks.CHECKED:
        assert block.kind is None, f"{block.where}: a marker on a {block.lang or 'plain'} block"
        return
    assert block.kind is not None, (
        f"{block.where}: mark this block (`<!-- check: run|snippet|include|skip ... -->` on the "
        "line before it, scripts/doc_blocks.py)"
    )
    if block.kind == "run":
        runner, _, group = block.arg.partition(" ")
        assert runner in doc_blocks.RUNNERS, block.where
        assert bool(group) == (runner == "python"), f"{block.where}: python takes a group, only"
    elif block.kind == "skip":
        reason, colon, why = block.arg.partition(":")
        assert reason in doc_blocks.SKIP_REASONS, (
            f"{block.where}: a skip reason is one of {sorted(doc_blocks.SKIP_REASONS)}"
        )
        assert colon and len(why.strip()) > 20, f"{block.where}: say why it cannot run"
    elif block.kind == "snippet":
        name, _, step_name = block.arg.partition("/")
        stem, _, suffix = name.partition(".")
        assert suffix == "sh" and block.lang != "python", block.where  # the examples are bash
        steps = {step.name: step for step in snippets.load(stem)}
        assert step_name in steps, f"{block.where}: no step {step_name!r} in {name}"
        assert block.text == steps[step_name].text, (
            f"{block.where}: not the same text as {block.arg}"
        )
    else:  # include
        assert block.text == f'--8<-- "{block.arg}"', block.where
        assert (snippets.DIRECTORY / block.arg).is_file(), block.where


def test_every_curl_to_the_server_is_a_snippet_ci_runs() -> None:
    for block in BLOCKS:
        if "curl -sS --fail-with-body" in block.text and '"$MENDARI' in block.text:
            assert block.kind in ("snippet", "include"), block.where


def test_every_snippet_step_is_shown_where_the_docs_promise() -> None:
    """The API guide includes every snippet file whole, so every step is shown there. The README
    links to the guide rather than copying the steps; a step it does copy (a `snippet` block) is
    one that exists, and the marker test above keeps its text the step's."""
    steps = {f"{step.file}.sh/{step.name}" for step in snippets.every()}
    assert {block.arg for block in BLOCKS if block.kind == "snippet"} <= steps
    included = {block.arg for block in BLOCKS if block.kind == "include"}
    assert included == {f"{file}.sh" for file in snippets.FILES}


def test_the_quickstart_calls_no_server() -> None:
    blocks = doc_blocks.marked("quickstart")
    assert blocks and {block.file for block in blocks} == {"README.md", "docs/index.md"}
    for block in blocks + doc_blocks.marked("cli"):
        assert "curl" not in block.text and "mendari serve" not in block.text, block.where


def test_a_block_without_a_marker_is_found() -> None:
    markdown = (
        "Text\n\n```bash\nuv run mendari list\n```\n\n"
        "<!-- check: skip settings: x -->\n```sh\nx\n```\n"
    )
    found = doc_blocks.blocks("x.md", markdown)
    assert [(b.lang, b.kind, b.arg) for b in found] == [
        ("bash", None, ""),
        ("sh", "skip", "settings: x"),
    ]


def test_fences_are_found_however_they_are_written() -> None:
    """In a block quote, indented in an admonition, and a longer fence holding a shorter one."""
    markdown = (
        "> ```bash\n> uv run mendari list\n> ```\n\n"
        "!!! note\n    ```python\n    print(1)\n    ```\n\n"
        "````bash\n```\nnot a close\n````\n"
    )
    found = doc_blocks.blocks("x.md", markdown)
    assert [(b.lang, b.text.strip()) for b in found] == [
        ("bash", "uv run mendari list"),
        ("python", "print(1)"),
        ("bash", "```\nnot a close"),
    ]


def test_no_raw_html_code_block_slips_past() -> None:
    for path in doc_blocks.DOCS:
        text = (ROOT / path).read_text(encoding="utf-8")
        assert "<pre" not in text, f"{path}: write a fenced block, which carries a marker"


def test_ci_runs_every_runner() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    quickstart = ci.split("\n  quickstart:\n", 1)[1].split("\n\n  ", 1)[0]
    for command in (
        "python3 scripts/run_quickstart.py",
        "python3 scripts/doc_blocks.py run cli",
        "uv run python scripts/doc_blocks.py run python",
        "python3 scripts/doc_blocks.py run install",
        "uv run python scripts/check_doc_snippets.py",  # REST, FHIR, CDS Hooks, server-db
    ):
        assert f"run: {command}" in quickstart, command
    assert "enable-cache: false" in quickstart  # a clean runner


def test_the_landing_shows_only_snippet_steps(tmp_path: Path) -> None:
    page = TestClient(create_app(db_path=tmp_path / "a.db")).get("/docs").text
    codes = set(re.findall(r'<code id="code-([^"]+)">', page))
    steps = {f"{step.file}-{step.name}" for step in snippets.every()}
    shown = {
        code for code in codes if not code.endswith(("-prints", "-prints-1")) and code != "setup"
    }
    assert shown == steps
    assert "setup" in codes
