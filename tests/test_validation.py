# SPDX-License-Identifier: Apache-2.0
"""One validation function (LP-20b, `evals/validation.py`): `mendari validate` and the
registry's publish call it, so an agent one takes the other takes, whether the deployment owns
it or pulled it from a registry; each problem has a kind, a file and a place, and never a case's
text."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from mendari.cli import main
from mendari.core.agent import Agent, load_agent
from mendari.errors import AgentValidationError
from mendari.evals import validation
from mendari.evals.behaviour_tests import validate_agent
from mendari.hub import archive
from mendari.hub import registry as registry_module
from mendari.hub.registry import RegistryStore
from tests.conftest import DEMO_AGENT_DIR, FIXTURE_AGENTS, TRIAGE_DIR, Registry, cpu_time
from tests.support.engine import pulled, sorter
from tests.test_bounded_budget import BOUND_S, fan_out

SECRET = "PLANTED-CASE-TEXT-7731"  # in a planted case's input: never in any problem


def _planted_triage(base: Path) -> Agent:
    """The triage fixture at a new version (so a registry checks it as an agent it did not
    ship), with one behaviour test whose `message` is text where the schema wants an object."""
    root = base / "triage"
    shutil.copytree(TRIAGE_DIR, root)
    manifest = (root / "agent.yaml").read_text()
    version = load_agent(TRIAGE_DIR).version
    (root / "agent.yaml").write_text(manifest.replace(f"version: {version}", "version: 9.9.9", 1))
    cases = root / "tests" / "cases.yaml"
    cases.write_text(
        cases.read_text()
        + f"\n- name: planted\n  input: {{message: {SECRET!r}}}\n  expect: {{not_outcome: AUTO}}\n"
    )
    return load_agent(root)


def _case_number(agent: Agent, name: str) -> int:
    cases = yaml.safe_load((agent.root / str(agent.manifest.tests)).read_text())
    return next(number for number, case in enumerate(cases, start=1) if case["name"] == name)


def test_the_command_and_the_registry_call_one_function(
    tmp_path: Path, registry: Registry, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each caller is given an agent with one planted problem (a behaviour test's input the
    schema refuses) and reports the same kind, file and place."""
    agent = _planted_triage(tmp_path)
    place = f"case {_case_number(agent, 'planted')}"
    found = validation.problems(agent)
    assert [(p.kind, p.file, p.place) for p in found] == [("tests", "tests/cases.yaml", place)]
    line = found[0].text
    assert line.startswith(f"tests/cases.yaml {place} (planted): input message: ")

    # The command line: the same line, as `mendari validate` has always printed problems.
    assert main(["validate", str(agent.root)]) == 1
    assert line in capsys.readouterr().err
    with pytest.raises(AgentValidationError) as error:
        validate_agent(agent)  # the old name calls the one function
    assert error.value.problems == [line]

    # The registry: refused, naming it, and nothing stored.
    refused = registry.publish(agent)
    assert refused.status_code == 422, refused.text
    assert line in refused.json()["detail"]
    assert registry.client.get("/v1/search").json() == []

    # An agent pulled from a registry (on the bounded engine), with the same kind of problem.
    root = sorter(tmp_path / "pulled")
    cases = root / "tests" / "cases.yaml"
    cases.write_text(
        cases.read_text() + f"\n- name: planted\n  input: {{message: {{note: {SECRET!r}}}}}\n"
        "  expect: {not_outcome: AUTO}\n"
    )
    bounded = pulled(root)
    [problem] = validation.problems(bounded)
    assert (problem.kind, problem.file) == ("tests", "tests/cases.yaml")
    assert problem.place == f"case {_case_number(bounded, 'planted')}"
    assert SECRET not in problem.text


def test_every_reference_agent_is_valid() -> None:
    """The bundled demo agent, and every agent of the test fixtures' folder (bundled in the
    tests' place)."""
    folders = [d for d in sorted(FIXTURE_AGENTS.iterdir()) if (d / "agent.yaml").is_file()]
    assert folders
    for agent in [load_agent(DEMO_AGENT_DIR), *(load_agent(folder) for folder in folders)]:
        assert validation.problems(agent) == [], agent.id
        validation.validate(agent)


def test_a_publish_the_validator_refuses_stores_nothing_and_names_the_problem(
    tmp_path: Path, registry: Registry, db_target: str
) -> None:
    agent = _planted_triage(tmp_path)
    refused = registry.publish(agent)
    assert refused.status_code == 422
    detail = refused.json()["detail"]
    assert detail.startswith("the agent is not valid (`mendari validate`): tests/cases.yaml case ")
    assert SECRET not in detail
    with RegistryStore(db_target) as store, store._db.session(exclusive=False) as db:
        assert db.one("SELECT COUNT(*) AS n FROM hub_versions")["n"] == 0
        assert db.one("SELECT COUNT(*) AS n FROM hub_archives")["n"] == 0


def test_a_valid_publish_is_stored_as_before(
    tmp_path: Path, registry: Registry, db_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The metadata stored for a valid agent is what the registry stored before it validated:
    `describe` of the same archive, unchanged."""
    monkeypatch.setenv("MENDARI_REGISTRY_REGRESSION", "off")  # its result is the run's own
    root = tmp_path / "triage"
    shutil.copytree(TRIAGE_DIR, root)
    manifest = (root / "agent.yaml").read_text()
    version = load_agent(TRIAGE_DIR).version
    (root / "agent.yaml").write_text(manifest.replace(f"version: {version}", "version: 9.9.9", 1))
    agent = load_agent(root)
    token, key = registry.fixture_publisher()  # the test fixtures' namespace's
    data = archive.build(agent, key)
    published = registry.client.put(
        f"/v1/agents/{agent.id}/9.9.9",
        content=data,
        headers={"authorization": f"Bearer {token}"},
    )
    assert published.status_code == 201, published.text
    public = key.public_key()
    verified = archive.verify(data, {archive.key_id(public): public})
    with tempfile.TemporaryDirectory() as scratch:
        extracted = archive.extract(verified, Path(scratch) / "agent", origin="hub")
        expected = json.loads(json.dumps(registry_module.describe(extracted, verified)))
    with RegistryStore(db_target) as store, store._db.session(exclusive=False) as db:
        row = db.one("SELECT metadata FROM hub_versions")
    assert row is not None and json.loads(row["metadata"]) == expected


def test_a_not_owned_agent_is_validated_inside_the_budget(tmp_path: Path) -> None:
    """An agent from a registry whose schema fans one pattern out a million times: each dataset
    record and each behaviour test is checked within the CPU budget a decision gives it, and the
    stop is a problem, not a hang."""

    def noted(schema: dict[str, Any]) -> None:
        schema["properties"]["note"] = {"type": "string", "$ref": "#/$defs/t0"}
        schema["$defs"] = fan_out(22)

    def declared(manifest: dict[str, Any]) -> None:
        manifest.setdefault("evaluation", {})["datasets"] = {"examples": "data/examples.jsonl"}

    root = sorter(tmp_path, schema=noted, manifest=declared)
    (root / "data").mkdir()
    record = {"id": "c1", "input": {"message": "my bill", "note": "x"}, "labels": {}}
    (root / "data" / "examples.jsonl").write_text(json.dumps(record) + "\n")
    cases = root / "tests" / "cases.yaml"
    cases.write_text(
        cases.read_text() + '- name: noted\n  input: {message: "my bill", note: x}\n'
        "  expect: {recommendation: billing}\n"
    )
    hub = pulled(root)
    with cpu_time() as spent:
        found = validation.problems(hub)
    assert spent() < 2 * BOUND_S  # one budget for the test, one for the record
    kinds = {(p.kind, p.place) for p in found if "stopped" in p.text}
    assert ("dataset", "case c1") in kinds
    assert any(kind == "tests" for kind, _ in kinds)


def test_a_problem_names_a_file_and_a_place_and_never_case_text(tmp_path: Path) -> None:
    """A dataset record and a behaviour test whose inputs hold planted text in a place the
    schema refuses, and a record whose label is not the agent's: each problem names the file and
    the case, and no problem holds the text."""

    def declared(manifest: dict[str, Any]) -> None:
        manifest.setdefault("evaluation", {})["datasets"] = {"examples": "data/examples.jsonl"}

    root = sorter(tmp_path, manifest=declared)
    (root / "data").mkdir()
    records = [
        {"id": "c1", "input": {"message": {"text": SECRET}}, "labels": {"topic": "billing"}},
        {"id": "c2", "input": {"message": "a bill"}, "labels": {"topic": SECRET}},
    ]
    (root / "data" / "examples.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records)
    )
    cases = root / "tests" / "cases.yaml"
    cases.write_text(
        cases.read_text() + f"\n- name: planted\n  input: {{message: [{SECRET!r}]}}\n"
        "  expect: {not_outcome: AUTO}\n"
    )
    found = validation.problems(pulled(root))
    assert {(p.kind, p.file, p.place) for p in found} == {
        ("tests", "tests/cases.yaml", f"case {_case_number(load_agent(root), 'planted')}"),
        ("dataset", "data/examples.jsonl", "case c1"),
        ("dataset", "data/examples.jsonl", "case c2"),
    }
    for problem in found:
        assert SECRET not in json.dumps(problem.to_json())
        assert problem.text.startswith(problem.file)
