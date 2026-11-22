# SPDX-License-Identifier: Apache-2.0
"""The API examples have one source, `src/mendari/dashboard/snippets/*.sh` (`server/snippets.py`):
the API guide includes them whole, their bodies are the packs' synthetic examples, and
`scripts/check_doc_snippets.py` (a CI step) runs them against a local server, in open mode and
with a service key. These tests keep the copies equal and the scripts checkable; the CI step
proves the calls work. The README's one call, a decision by the bundled demo agent, is made here
on a test server."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari.core.pack import Pack
from mendari.core.registry import discover
from mendari.evals.pack_tests import load_cases
from mendari.server import api_docs, snippets
from mendari.server.app import create_app
from mendari.server.snippets import Step
from mendari.standards import cds_hooks

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
FHIR_ENTRIES = api_docs.FHIR_EXAMPLE["entry"]


def test_every_file_has_named_steps() -> None:
    for file in snippets.FILES:
        steps = snippets.load(file)
        assert steps, f"{file}.sh has no '# step:'"
        names = [step.name for step in steps]
        assert len(names) == len(set(names)), f"{file}.sh repeats a step name: {names}"
        assert all(step.text for step in steps), f"{file}.sh has an empty step"
    assert {path.stem for path in snippets.DIRECTORY.glob("*.sh")} == set(snippets.FILES)
    assert {path.stem for path in snippets.DIRECTORY.glob("*.py")} == set(snippets.PYTHON)
    for file in snippets.PYTHON:
        steps = snippets.load(file, language="python")
        assert steps and all(step.text and step.language == "python" for step in steps), file


def test_a_malformed_step_header_is_refused(tmp_path: Path) -> None:
    (tmp_path / "x.sh").write_text('# step: Packs - list\ncurl -sS "$MENDARI/api/packs"\n')
    with pytest.raises(ValueError, match="write a step as"):
        snippets.load("x", tmp_path)


def test_every_readme_curl_block_is_a_snippet_step_or_the_demo_agents_decision(
    tmp_path: Path,
) -> None:
    """GitHub can't include a file, so a step the README copies must be the step exactly. Its
    quickstart's own call asks the bundled demo agent for a decision on a local server: that one
    is posted here as written, and answers what the README says it does."""
    texts = {step.text for step in STEPS}
    blocks = _curl_blocks(README)
    own = [block for block in blocks if block not in texts]
    assert len(own) == 1, "a README curl block is neither a snippet step nor the quickstart's call"
    (call,) = own
    found = re.fullmatch(
        r"curl -sS -X POST http://127\.0\.0\.1:8000(/api/packs/mendari/portal-message-agent/decide)"
        r" \\\n  -H 'content-type: application/json' \\\n  -d '(\{.+\})'",
        call,
    )
    assert found is not None, call
    # The tests' search path is their fixtures (conftest); the README's is the bundled agent.
    app = create_app(db_path=tmp_path / "readme.db", packs_paths=[ROOT / "agents"])
    client = TestClient(app)
    response = client.post(found.group(1), json=json.loads(found.group(2)))
    assert response.status_code == 200, response.text
    decision = response.json()
    assert (decision["outcome"], decision["priority"]) == ("HUMAN_REVIEW", "urgent")
    assert '`"outcome": "HUMAN_REVIEW"`, `"priority": "urgent"`' in README
    assert client.get(f"/api/audit/{decision['id']}").status_code == 200
    assert [item["id"] for item in client.get("/api/review").json()] == [decision["id"]]


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
    """What a release serves: the bundled demo agent (the tests' own search path is fixtures)."""
    return {pack.id: pack for pack in discover([ROOT / "agents"]).packs}


def test_every_body_is_a_synthetic_example_the_project_already_ships() -> None:
    """A decide body is one of the demo agent's behaviour-test inputs, and the FHIR one is the API
    reference's example: synthetic, and the same as what "Try it out" offers. The CDS hook's
    prefetch is that example's patient and message, for the agent it is the example of. Every
    pack a body names is one a release serves."""
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
        hooked = re.fullmatch(r"/cds-services/([^/]+)", path)
        if hooked is not None:
            (pack,) = [p for p in packs.values() if cds_hooks.service_id(p) == hooked.group(1)]
            assert pack.id == api_docs.FHIR_EXAMPLE_PACK and pack.manifest.cds_hooks is not None
            example = {e["resource"]["resourceType"]: e["resource"] for e in FHIR_ENTRIES}
            patient, message = example["Patient"], example["Communication"]
            assert body["hook"] == pack.manifest.cds_hooks.hook
            assert body["prefetch"] == {"patient": patient, "communication": message}
            assert body["context"] == {"patientId": patient["id"], "communicationId": message["id"]}
            continue
        pack_id = re.fullmatch(r"/api/packs/([^/]+/[^/]+)/decide", path)
        assert pack_id is not None, f"{step.file}.sh {step.name}: a body for {path}"
        inputs = [case["input"] for case in load_cases(packs[pack_id.group(1)])]
        assert body in inputs, f"{step.file}.sh {step.name}: not one of the pack's examples"
    assert bodies == 3  # the demo agent's decision, the FHIR one and the CDS hook's


def test_the_key_pass_skips_only_what_a_service_key_may_not_do() -> None:
    """The second CI pass runs with a `service` key; a step it may not run is chosen by the route
    rules, not by hand: today only the whole-trail export, which needs an administrator."""
    check = _script("check_doc_snippets")
    skipped = {(step.file, step.name) for step in STEPS if not check.runnable_with_key(step)}
    assert skipped == {("fhir", "export")}


def test_ci_runs_the_snippets() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "run: uv run python scripts/check_doc_snippets.py" in ci


def test_the_api_guide_includes_every_snippet_file_as_it_is() -> None:
    """docs/guides/api.md includes the scripts themselves (pymdownx.snippets); `check_paths`
    makes `mkdocs build --strict` fail on one that is gone."""
    guide = (ROOT / "docs" / "guides" / "api.md").read_text(encoding="utf-8")
    included = re.findall(r'^--8<-- "([^"]+)"$', guide, re.M)
    expected = [
        name
        for file in snippets.FILES
        for name in (f"{file}.sh", *([f"{file}.py"] if file in snippets.PYTHON else []))
    ]
    assert included == expected
    mkdocs = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    assert "base_path: [src/mendari/dashboard/snippets]" in mkdocs
    assert "check_paths: true" in mkdocs and "guides/api.md" in mkdocs


def test_step_titles_say_agent_not_pack() -> None:
    """A step's title is shown on /docs beside its copy button: user-facing words say "agent"
    (the routes still say `packs`, which a step's commands keep)."""
    for step in [*snippets.every(), *snippets.python_steps()]:
        assert not re.search(r"\bpacks?\b", step.title, re.I), f"{step.file} {step.name}: {step.title}"
