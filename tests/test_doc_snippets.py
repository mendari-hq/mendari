"""The API examples have one source, `src/mendari/dashboard/snippets/*.sh` (`server/snippets.py`):
the README shows their steps verbatim, their bodies are the packs' synthetic examples, and
`scripts/check_doc_snippets.py` (a CI step) runs them against a local server, in open mode and
with a service key. These tests keep the copies equal and the scripts checkable; the CI step
proves the calls work."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest

from mendari.core.pack import Pack
from mendari.core.registry import discover
from mendari.evals.pack_tests import load_cases
from mendari.server import api_docs, snippets
from mendari.server.snippets import Step

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")


def _script(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bash_blocks(markdown: str) -> list[str]:
    return [block.strip() for block in re.findall(r"```bash\n(.*?)```", markdown, re.S)]


def _curl_blocks(markdown: str) -> list[str]:
    """Blocks that call the API with curl (not `vercel curl`, which goes through a deployment)."""
    return [
        block
        for block in _bash_blocks(markdown)
        if any(re.match(r"\s*(\w+=\$\()?curl\s", line) for line in block.splitlines())
    ]


STEPS = snippets.every()


def test_every_file_has_named_steps() -> None:
    for file in snippets.FILES:
        steps = snippets.load(file)
        assert steps, f"{file}.sh has no '# step:'"
        names = [step.name for step in steps]
        assert len(names) == len(set(names)), f"{file}.sh repeats a step name: {names}"
        assert all(step.text for step in steps), f"{file}.sh has an empty step"
    assert {path.stem for path in snippets.DIRECTORY.glob("*.sh")} == set(snippets.FILES)


def test_a_malformed_step_header_is_refused(tmp_path: Path) -> None:
    (tmp_path / "x.sh").write_text('# step: Packs - list\ncurl -sS "$MENDARI/api/packs"\n')
    with pytest.raises(ValueError, match="write a step as"):
        snippets.load("x", tmp_path)


def test_every_readme_curl_block_is_a_snippet_step() -> None:
    """GitHub can't include a file, so the README keeps copies; each must be a step exactly."""
    texts = {step.text for step in STEPS}
    blocks = _curl_blocks(README)
    assert len(blocks) >= 8  # the REST quickstart, sign-in, and FHIR in and out
    for block in blocks:
        assert block in texts, f"a README curl block is not a snippet step:\n{block}"


def test_the_curl_block_finder_sees_old_style_and_skips_vercel_curl() -> None:
    markdown = (
        "```bash\ncurl -s localhost:8000/fhir/metadata\n```\n"
        "```bash\nvercel curl /api/health --deployment <url>\n```\n"
        '```bash\nbundle=$(curl -sS "$MENDARI/x")\n```\n'
    )
    assert _curl_blocks(markdown) == [
        "curl -s localhost:8000/fhir/metadata",
        'bundle=$(curl -sS "$MENDARI/x")',
    ]


def test_the_readme_quickstart_ci_runs_calls_no_server() -> None:
    """The `quickstart` CI job runs every bash block of "## Quickstart" before "### Python": the
    REST quickstart must sit after it, since it needs `mendari serve` running."""
    blocks = _script("run_quickstart").quickstart_blocks(README)
    assert blocks
    for block in blocks:
        assert "curl" not in block and "mendari serve" not in block, block
    assert "### REST API" in README.split("\n### Python", 1)[1]


def test_every_call_fails_on_a_refusal_and_names_the_server_by_variable() -> None:
    """`--fail-with-body` makes a 4xx or 5xx fail the CI run (with the answer shown), and
    `"$MENDARI/…"` lets the same text run against the CI server and a reader's."""
    for step in STEPS:
        commands = re.split(r"(?<![\w-])curl(?=\s)", step.text)[1:]
        assert commands, f"{step.file}.sh {step.name} makes no call"
        for command in commands:
            assert "--fail-with-body" in command, f"{step.file}.sh {step.name}: {command[:60]}"
            assert "localhost" not in command and "127.0.0.1" not in command
        assert len(step.calls()) == len(commands)


def test_calls_are_read_from_the_text() -> None:
    step = Step(
        "fhir",
        "x",
        "x",
        "id=$(curl -sS -X POST \"$MENDARI/fhir/\\$decide\" -d '{}')\n"
        'curl -sS "$MENDARI/fhir/Task/$id/_history/1?a=b" | jq .\n'
        'curl -sS "$MENDARI/api/audit/${id}/fhir"',
    )
    assert step.calls() == [
        ("POST", "/fhir/$decide"),
        ("GET", "/fhir/Task/x/_history/1"),
        ("GET", "/api/audit/x/fhir"),
    ]
    with pytest.raises(ValueError, match="without"):
        Step("q", "y", "y", "curl -sS http://localhost:8000/api/packs").calls()


def _body(step: Step) -> Any:
    found = re.search(r"-d '(.*?)'", step.text, re.S)
    return None if found is None else json.loads(found.group(1))


def _packs() -> dict[str, Pack]:
    return {pack.id: pack for pack in discover().packs}


def test_every_body_is_a_synthetic_example_the_project_already_ships() -> None:
    """A decide body is one of the pack's behaviour-test inputs, and the FHIR one is the API
    reference's example: synthetic, and the same as what "Try it out" offers."""
    packs = _packs()
    bodies = 0
    for step in STEPS:
        body = _body(step)
        if body is None:
            continue
        bodies += 1
        ((method, path),) = step.calls()
        assert method == "POST"
        if path == "/fhir/$decide":
            assert body == {
                "resourceType": "Parameters",
                "parameter": [
                    {"name": "pack", "valueString": api_docs.FHIR_EXAMPLE_PACK},
                    {"name": "data", "resource": api_docs.FHIR_EXAMPLE},
                ],
            }
            continue
        pack_id = re.fullmatch(r"/api/packs/([^/]+/[^/]+)/decide", path)
        assert pack_id is not None, f"{step.file}.sh {step.name}: a body for {path}"
        inputs = [case["input"] for case in load_cases(packs[pack_id.group(1)])]
        assert body in inputs, f"{step.file}.sh {step.name}: not one of the pack's examples"
    assert bodies == 2


def test_the_key_pass_skips_only_what_a_service_key_may_not_do() -> None:
    """The second CI pass runs with a `service` key; a step it may not run is chosen by the route
    rules, not by hand: today only the whole-trail export, which needs an administrator."""
    check = _script("check_doc_snippets")
    skipped = {(step.file, step.name) for step in STEPS if not check.runnable_with_key(step)}
    assert skipped == {("fhir", "export")}


def test_ci_runs_the_snippets() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "run: uv run python scripts/check_doc_snippets.py" in ci
