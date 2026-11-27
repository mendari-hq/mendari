# SPDX-License-Identifier: Apache-2.0
"""An agent never stops the server: whatever one agent's content makes loading, checking or
building raise, that agent is listed under /api/health `invalid` and every other agent is served
(core/agent.py, core/registry.py, server/app.py, standards/terminology.py, hub/registry.py)."""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from mendari import load_agent
from mendari.cli import main
from mendari.core import registry as registry_module
from mendari.core.agent import MAX_YAML_CHARS, MAX_YAML_NODES, parse_yaml
from mendari.core.registry import version_key
from mendari.errors import AgentValidationError
from mendari.evals.behaviour_tests import load_cases
from mendari.server.api_docs import MAX_EXAMPLE_BYTES
from mendari.server.app import create_app
from mendari.standards import terminology as terminology_module
from tests.conftest import FIXTURE_AGENTS, TRIAGE_DIR, cpu_time

# The test fixtures' agents, served as a release's bundled agents are.
AGENTS = FIXTURE_AGENTS
TRIAGE_ID = "mendari-test/triage"


def _outsider(root: Path, edit: Callable[[Path], None], source: Path = TRIAGE_DIR) -> Path:
    """An outside copy of a fixture's agent (its own id) under `root`, changed by `edit`."""
    agent = root / "outsider" / source.name
    shutil.copytree(source, agent)
    manifest_path = agent / "agent.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest["id"] = f"outsider/{source.name}"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True))
    edit(agent)
    return agent


def _with_schema_property(value: Any) -> Callable[[Path], None]:
    def edit(agent: Path) -> None:
        path = agent / "schemas" / "input.schema.json"
        schema = json.loads(path.read_text())
        schema["properties"]["zz"] = value
        path.write_text(json.dumps(schema))

    return edit


def _write(relative: str, content: str | bytes) -> Callable[[Path], None]:
    def edit(agent: Path) -> None:
        data = content.encode() if isinstance(content, str) else content
        (agent / relative).write_bytes(data)

    return edit


def _append(relative: str, text: str) -> Callable[[Path], None]:
    def edit(agent: Path) -> None:
        path = agent / relative
        path.write_text(path.read_text() + text)

    return edit


def _set_line(relative: str, line: str) -> Callable[[Path], None]:
    """`line` (a top-level `key: value`) in place of the line that sets that key, or appended
    when none does: a YAML file of an agent may not give a key twice."""
    key = line.split(":", 1)[0]

    def edit(agent: Path) -> None:
        path = agent / relative
        lines = path.read_text().splitlines()
        held = [n for n, text in enumerate(lines) if text.split(":", 1)[0] == key]
        if held:
            lines[held[0]] = line
        else:
            lines.append(line)
        path.write_text("\n".join(lines) + "\n")

    return edit


def _set_key(relative: str, key: str, value: Any) -> Callable[[Path], None]:
    """A YAML file of an agent with its top-level `key` set to `value` (loaded, set and written
    again): a YAML file of an agent may not give a key twice, so an override is never appended."""

    def edit(agent: Path) -> None:
        path = agent / relative
        data = yaml.safe_load(path.read_text())
        data[key] = value
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))

    return edit


def laughs(levels: int, leaf: str = "{a: 1}") -> str:
    """A YAML key whose value is 10**levels leaves, in a few hundred bytes of anchors."""
    lines = [f"x_junk:\n  l0: &l0 [{', '.join([leaf] * 10)}]"]
    for level in range(1, levels + 1):
        lines.append(f"  l{level}: &l{level} [{', '.join([f'*l{level - 1}'] * 10)}]")
    return "\n" + "\n".join(lines) + "\n"


def _shipped() -> list[tuple[str, Path]]:
    """Every agent in the fixtures' folder, by the id its manifest gives (never its folder's
    name), with its folder."""
    return [
        (yaml.safe_load((folder / "agent.yaml").read_text())["id"], folder)
        for folder in sorted(AGENTS.iterdir())
        if (folder / "agent.yaml").is_file()
    ]


def _shipped_agents_all_decide(client: TestClient) -> None:
    """Health answers, counting every fixture's agent, and each one decides its first behaviour
    test's input."""
    health = client.get("/api/health")
    assert health.status_code == 200, health.text
    shipped = _shipped()
    assert shipped and health.json()["agents"] >= len(shipped)
    for agent_id, folder in shipped:
        case = yaml.safe_load((folder / "tests" / "cases.yaml").read_text())[0]
        response = client.post(f"/api/agents/{agent_id}/decide", json=case["input"])
        assert response.status_code == 200, (agent_id, response.text)


DEEP = 5000  # nested deeper than the YAML, JSON and jsonschema readers recurse


@pytest.mark.parametrize(
    ("edit", "problem"),
    [
        (_with_schema_property({"$ref": "#/required/abc"}), "'#/required/abc' is not in the"),
        (
            _with_schema_property({"type": "string", "pattern": "a{99999999999}"}),
            "input schema: properties/zz/pattern: invalid regular expression: the repetition",
        ),
        (_write("schemas/input.schema.json", "{not json"), "not a JSON document (JSONDecodeErr"),
        (
            _write("schemas/input.schema.json", b'{"a": "\xff\xfe"}'),
            "not a JSON document (UnicodeDecodeError)",
        ),
        (
            _write("schemas/input.schema.json", "[" * 200_000 + "]" * 200_000),
            "not a JSON document (RecursionError)",
        ),
        (
            _with_schema_property(json.loads('{"not":' * 900 + "{}" + "}" * 900)),
            "not a schema the validator can read (RecursionError)",
        ),
        (_write("agent.yaml", "a: " + "[" * DEEP + "]" * DEEP), "invalid YAML (RecursionError)"),
        (_write("agent.yaml", b"id: \xff\xfe"), "agent.yaml: invalid YAML (UnicodeDecodeError)"),
        (_append("agent.yaml", laughs(7)), "agent.yaml: expands to more than 1,000,000 nodes"),
        (_append("agent.yaml", "x_loop: &loop [*loop]\n"), "refers to the anchor it is inside"),
    ],
    ids=[
        "ref-index",
        "repeat-count",
        "schema-not-json",
        "schema-not-utf8",
        "schema-too-deep",
        "schema-too-deep-to-check",
        "manifest-too-deep",
        "manifest-not-utf8",
        "manifest-billion-laughs",
        "manifest-alias-loop",
    ],
)
def test_an_agent_whose_content_raises_while_loading_is_listed_and_the_others_decide(
    tmp_path: Path, edit: Callable[[Path], None], problem: str
) -> None:
    """R2, T2, T7: an outside agent whose content made loading raise something other than a
    Mendari error (a ValueError from a `$ref` pointer, a RecursionError from deep nesting), or
    whose YAML aliases expanded exponentially, stopped or stalled the server at startup, every
    Vercel cold start included, so no agent was served."""
    outside = tmp_path / "outside"
    agent = _outsider(outside, edit).resolve()
    started = time.perf_counter()
    client = TestClient(create_app(db_path=tmp_path / "o.db", agents_paths=[outside, AGENTS]))
    assert time.perf_counter() - started < 10
    invalid = client.get("/api/health").json()["invalid"]
    assert list(invalid) == [str(agent)]
    assert problem in invalid[str(agent)] and "Traceback" not in invalid[str(agent)]
    _shipped_agents_all_decide(client)


def test_any_error_loading_one_agent_lists_it_by_type_and_serves_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever an agent's content makes loading raise, the agent is listed under `invalid` by the
    error's type (its text is the agent's, not Mendari's) and every other agent is served."""
    outside = tmp_path / "outside"
    agent = _outsider(outside, lambda agent: None).resolve()
    real = registry_module.load_agent

    def load(root: Path, **options: Any) -> Any:
        if Path(root) == agent:
            raise RuntimeError("text from the agent")
        return real(root, **options)

    monkeypatch.setattr(registry_module, "load_agent", load)
    client = TestClient(create_app(db_path=tmp_path / "r.db", agents_paths=[outside, AGENTS]))
    assert client.get("/api/health").json()["invalid"] == {
        str(agent): "the agent could not be loaded (RuntimeError)"
    }
    _shipped_agents_all_decide(client)


def test_an_unexpected_error_building_terminology_is_named_and_the_agent_still_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = terminology_module.resources

    def resources(agents: Any) -> Any:
        agents = list(agents)
        if any(agent.id == TRIAGE_ID for agent in agents):
            raise KeyError("text from the agent")
        return real(agents)

    monkeypatch.setattr(terminology_module, "resources", resources)
    client = TestClient(create_app(db_path=tmp_path / "t.db", agents_paths=[AGENTS]))
    assert client.get("/api/health").json()["invalid"] == {TRIAGE_ID: "terminology: KeyError"}
    _shipped_agents_all_decide(client)


def _agent_default(change: Callable[[Path], None]) -> Callable[[Path], None]:
    """The router answered by its `agent` variant (canned answers from a mock file), which is
    built when the server starts, then `change`."""

    def edit(agent: Path) -> None:
        manifest = yaml.safe_load((agent / "agent.yaml").read_text())
        assert manifest["providers"]["router"]["variants"]["agent"]["type"] == "agent"
        manifest["providers"]["router"]["use"] = "agent"
        (agent / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
        change(agent)

    return edit


def _agent_extra_key(agent: Path) -> None:
    manifest = yaml.safe_load((agent / "agent.yaml").read_text())
    manifest["providers"]["router"]["variants"]["agent"]["bogus"] = 1
    (agent / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (_agent_extra_key, "Extra inputs are not permitted"),
        (
            lambda agent: (agent / "fixtures" / "router.json").write_bytes(b"\xff\xfe {}"),
            "fixtures/router.json: not a JSON document (UnicodeDecodeError)",
        ),
    ],
    ids=["extra-key", "not-utf8"],
)
def test_an_agent_whose_providers_cannot_be_built_never_fails_health(
    tmp_path: Path, change: Callable[[Path], None], problem: str
) -> None:
    """T9: /api/health built every served agent's providers, and one agent whose default variant
    could not be built made it a 422 or a 500 for every reader. Providers are built at start-up
    now (LP-16d), and health reads that: such an agent is listed under `invalid`, its decisions
    answer 422 naming the problem, and every other agent is reported and decides."""
    outside = tmp_path / "outside"
    _outsider(outside, _agent_default(change))
    client = TestClient(create_app(db_path=tmp_path / "c.db", agents_paths=[outside, AGENTS]))
    health = client.get("/api/health")
    assert health.status_code == 200, health.text
    body = health.json()
    agent_id = "outsider/triage"
    assert list(body["invalid"]) == [agent_id]
    assert problem in body["invalid"][agent_id]
    assert agent_id not in body["providers"]
    assert TRIAGE_ID in body["providers"]
    case = yaml.safe_load((TRIAGE_DIR / "tests" / "cases.yaml").read_text())[0]
    refused = client.post(f"/api/agents/{agent_id}/decide", json=case["input"])
    assert refused.status_code == 422, refused.text
    # As README says: such an agent stays listed, counted and offered to EHRs, answering 422.
    assert client.get(f"/api/agents/{agent_id}").status_code == 422
    assert agent_id in [item["id"] for item in client.get("/api/agents").json()]
    assert body["agents"] == len(_shipped()) + 1
    services = [s["id"] for s in client.get("/cds-services").json()["services"]]
    assert "outsider.triage" in services
    _shipped_agents_all_decide(client)


def test_an_agent_whose_tests_do_not_load_never_fails_the_api_reference(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _outsider(outside, _write("tests/cases.yaml", b"- name: \xff\xfe"))
    client = TestClient(create_app(db_path=tmp_path / "a.db", agents_paths=[outside, AGENTS]))
    assert client.get("/openapi.json").status_code == 200


# -- versions -------------------------------------------------------------------------------------


def test_an_over_long_version_is_refused_at_load_and_never_stops_startup(tmp_path: Path) -> None:
    """T10: a version of 5,000 digits loaded, and comparing two versions of one agent at startup
    (`int()` refuses more than 4,300 digits) raised outside every isolation, so the server never
    started."""
    huge = "1" + "0" * 5000 + ".0.0"
    shipped = f"version: {load_agent(TRIAGE_DIR).version}"
    outside = tmp_path / "outside"
    agent = _outsider(
        outside, lambda agent: _replace(agent / "agent.yaml", shipped, f"version: '{huge}'")
    ).resolve()
    with pytest.raises(AgentValidationError, match="at most 64 characters"):
        load_agent(agent)
    client = TestClient(create_app(db_path=tmp_path / "v.db", agents_paths=[outside, AGENTS]))
    assert "at most 64 characters" in client.get("/api/health").json()["invalid"][str(agent)]
    _shipped_agents_all_decide(client)


def _replace(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, (path, old)
    path.write_text(text.replace(old, new, 1))


def test_versions_compare_as_numbers_whatever_their_length() -> None:
    ordered = ["0.9.0", "0.10.0", "1.0.0", "1.0.10", "2.0.0", "1" + "0" * 5000 + ".0.0"]
    assert sorted(reversed(ordered), key=version_key) == ordered
    assert version_key("1.02.0") == version_key("1.2.0")
    assert version_key("1.0.0-rc.1") == version_key("1.0.0")


# -- YAML an agent carries ------------------------------------------------------------------------


def test_a_billion_laughs_in_a_rules_file_is_refused_in_milliseconds(tmp_path: Path) -> None:
    """T7: a rules file of a few KB whose aliases expand to 10**8 patterns took 15 s and 1.25 GB
    to walk. The expanded size is counted on the composed graph, each node once."""
    root = tmp_path / "agent"
    shutil.copytree(TRIAGE_DIR, root)
    _append("logic/screen_rules.yaml", laughs(8, "{pattern: x}"))(root)
    with (
        cpu_time() as spent,
        pytest.raises(
            AgentValidationError, match=r"logic/screen_rules\.yaml: expands to more than"
        ),
    ):
        load_agent(root)
    assert spent() < 0.5


def test_anchors_within_the_cap_still_load(tmp_path: Path) -> None:
    root = tmp_path / "agent"
    shutil.copytree(TRIAGE_DIR, root)
    _append("logic/screen_rules.yaml", laughs(4))(root)  # 10**4 leaves: well within the cap
    rules = load_agent(root).read_yaml("logic/screen_rules.yaml")
    assert len(rules["x_junk"]["l4"]) == 10 and rules["questions"]
    assert MAX_YAML_NODES == 1_000_000


def test_only_yaml_with_an_anchor_is_walked_for_its_expansion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without an anchor there is no alias, so the parsed graph is the file's own tree and
    walking it adds nothing but time to every load (a few milliseconds per start)."""
    from mendari.core import agent as agent_module

    walked: list[object] = []
    real = agent_module._expansion_problem

    def spy(node: Any) -> str | None:
        walked.append(node)
        return real(node)

    monkeypatch.setattr(agent_module, "_expansion_problem", spy)
    plain, anchored = tmp_path / "plain.yaml", tmp_path / "anchored.yaml"
    plain.write_text("a: [1, 2]\nb: {c: 3}\n")
    anchored.write_text("a: &x [1, 2]\nb: *x\n")
    assert agent_module.parse_yaml(plain.read_bytes(), "plain.yaml", "x") == {
        "a": [1, 2],
        "b": {"c": 3},
    }
    assert walked == []
    assert agent_module.parse_yaml(anchored.read_bytes(), "anchored.yaml", "x") == {
        "a": [1, 2],
        "b": [1, 2],
    }
    assert len(walked) == 1


def long_text_laughs(levels: int, length: int) -> str:
    """A YAML key whose value is 10**levels references to one scalar of `length` characters."""
    lines = [f"x_text: &s {'a' * length}", "x_lists:", f"  l1: &l1 [{', '.join(['*s'] * 10)}]"]
    for level in range(2, levels + 1):
        lines.append(f"  l{level}: &l{level} [{', '.join([f'*l{level - 1}'] * 10)}]")
    return "\n" + "\n".join(lines) + "\n"


def test_a_long_scalar_aliased_through_lists_is_refused_by_its_expanded_text(
    tmp_path: Path,
) -> None:
    """The node cap counted a scalar as 1 whatever its length: a 1 MB tests file with a
    1,000,000-character anchor aliased through two levels of ten loaded, and /openapi.json,
    which embeds every agent's first automated test, answered 100 MB (each level more, ten times
    that). Scalar text is counted too now, at most `MAX_YAML_CHARS` once aliases are expanded."""
    path = tmp_path / "cases.yaml"
    path.write_text(long_text_laughs(3, 100_000))  # 300 KB holding 10**8 characters
    with (
        cpu_time() as spent,
        pytest.raises(AgentValidationError, match="expands to more than 32,000,000 characters"),
    ):
        parse_yaml(path.read_bytes(), "tests/cases.yaml", "x")
    assert spent() < 0.5
    within = tmp_path / "within.yaml"
    within.write_text(long_text_laughs(2, 1_000))  # 100,000 characters
    assert len(parse_yaml(within.read_bytes(), "within.yaml", "x")["x_lists"]["l2"]) == 10
    assert MAX_YAML_CHARS == 32_000_000


def test_a_tests_file_that_expands_past_the_cap_never_reaches_the_api_reference(
    tmp_path: Path,
) -> None:
    """The API reference (every agent's) and the agent's examples serialized each reference."""

    def edit(agent: Path) -> None:
        lists = "".join(
            f"  l{n}: &l{n} [{', '.join([f'*{previous}'] * 10)}]\n"
            for n, previous in [(1, "s"), (2, "l1"), (3, "l2")]
        )
        (agent / "tests" / "cases.yaml").write_text(
            f"- name: s\n  x_text: &s {'a' * 100_000}\n  input: {{message: {{body: hi}}}}\n"
            f"- name: lists\n{lists}"
            "- name: boom\n  input: {message: {body: *l3}}\n  expect: {outcome: AUTO}\n"
        )

    outside = tmp_path / "outside"
    agent = _outsider(outside, edit)
    with pytest.raises(AgentValidationError, match=r"tests/cases\.yaml: expands to more than"):
        load_agent(agent).read_yaml("tests/cases.yaml")
    client = TestClient(create_app(db_path=tmp_path / "e.db", agents_paths=[outside, AGENTS]))
    reference = client.get("/openapi.json")
    assert reference.status_code == 200 and len(reference.content) < 5_000_000
    examples = client.get("/api/agents/outsider/triage/examples")
    assert examples.status_code == 422, examples.status_code


def test_control_characters_count_at_their_json_length(tmp_path: Path) -> None:
    """The cap counted characters, but JSON writes a control character as six (`\\u0000`): a
    2 MB tests file aliasing a string of a million `"\\0"` thirty times loaded (30,000,000
    characters) and /openapi.json answered 180 MB. Text is weighed as JSON now."""
    path = tmp_path / "cases.yaml"
    nul = "\\0" * 200_000  # 400 KB of YAML, 200,000 characters, 1,200,000 as JSON
    path.write_text(f'- x_text: &s "{nul}"\n- {{message: {{body: [{", ".join(["*s"] * 30)}]}}}}\n')
    with pytest.raises(AgentValidationError, match="expands to more than 32,000,000 characters"):
        parse_yaml(path.read_bytes(), "tests/cases.yaml", "x")
    path.write_text(f'- x_text: &s "{nul}"\n- {{message: {{body: [{", ".join(["*s"] * 25)}]}}}}\n')
    assert len(parse_yaml(path.read_bytes(), "tests/cases.yaml", "x")[1]["message"]["body"]) == 25


def test_an_example_over_64_kb_as_json_is_left_out_of_the_api_reference(tmp_path: Path) -> None:
    """The reference holds an agent's example several times over, as JSON: 20,000 `"\\0"` (40 KB
    of YAML, no anchor) are 120 KB there each time. Such an example is left out, as one from
    tests that do not load is; the agent's own examples still answer."""

    def edit(agent: Path) -> None:
        cases = agent / "tests" / "cases.yaml"
        nul = "\\0" * 20_000
        first = (
            f'- name: nul\n  input: {{message: {{body: "{nul}"}}}}\n  expect: {{outcome: AUTO}}\n'
        )
        cases.write_text(first + cases.read_text())

    outside = tmp_path / "outside"
    _outsider(outside, edit)
    client = TestClient(create_app(db_path=tmp_path / "x.db", agents_paths=[outside, AGENTS]))
    reference = client.get("/openapi.json")
    assert reference.status_code == 200 and "\\u0000" not in reference.text
    decide = reference.json()["paths"]["/api/agents/{namespace}/{name}/decide"]["post"]
    summaries = [
        e["summary"]
        for e in decide["requestBody"]["content"]["application/json"]["examples"].values()
    ]
    assert len(summaries) == len(_shipped())
    assert not any(s.startswith("outsider/") for s in summaries)
    examples = client.get("/api/agents/outsider/triage/examples")
    assert examples.status_code == 200 and examples.json()[0]["name"] == "nul"
    assert MAX_EXAMPLE_BYTES == 64 * 1024


def test_an_unused_variant_cannot_carry_an_expanding_manifest(tmp_path: Path) -> None:
    """The same in agent.yaml (an unused variant's extra key, dumped by the agent's detail page):
    the agent is refused at load and every other agent is served."""
    outside = tmp_path / "outside"
    agent = _outsider(outside, _append("agent.yaml", long_text_laughs(3, 100_000))).resolve()
    client = TestClient(create_app(db_path=tmp_path / "m.db", agents_paths=[outside, AGENTS]))
    invalid = client.get("/api/health").json()["invalid"]
    assert "agent.yaml: expands to more than 32,000,000 characters" in invalid[str(agent)]
    _shipped_agents_all_decide(client)


@pytest.mark.parametrize(
    ("line", "error"),
    [
        ("x_released: 2024-02-30", "ValueError, line {line}, column 13"),
        ("x_count: !!int 0x", "ValueError, line {line}, column 10"),
        ("x_flag: !!bool maybe", "KeyError, line {line}, column 9"),
        ("x_when: !!timestamp bad", "AttributeError, line {line}, column 9"),
        ("x_ratio: !!float ''", "IndexError, line {line}, column 10"),
        ("x_count: !!int", "IndexError, line {line}, column 10"),
    ],
    ids=[
        "impossible-date",
        "int",
        "bool",
        "timestamp",
        "empty-float",
        "empty-int",
    ],
)
def test_a_yaml_value_python_cannot_build_is_the_agents_problem(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], line: str, error: str
) -> None:
    """SafeConstructor raises a plain ValueError for an impossible date, and a KeyError,
    AttributeError or IndexError for an explicitly tagged scalar it cannot read: `load_agent`
    raised it and `mendari validate` printed a traceback. The problem names the file, the
    error's type and the value's line and column (it had no position), never the value (the
    error's text)."""
    root = tmp_path / "agent"
    shutil.copytree(TRIAGE_DIR, root)
    _append("agent.yaml", f"\n{line}\n")(root)
    error = error.format(line=(root / "agent.yaml").read_text().count("\n"))
    with pytest.raises(AgentValidationError) as raised:
        load_agent(root)
    assert raised.value.problems == [f"agent.yaml: invalid YAML value ({error})"]
    assert main(["validate", str(root)]) == 1
    err = capsys.readouterr().err
    assert "agent.yaml: invalid YAML value" in err and "Traceback" not in err


# -- numbers, constructor errors and test inputs that are not JSON ------------------------------

LONG = "a number of more than 4,300 characters"


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("9" * 5000, LONG),
        ("0x" + "f" * 5000, LONG),
        ("0" + "7" * 5000, LONG),
        ("0b" + "1" * 5000, LONG),
        ("!!int " + "9" * 5000, LONG),
        ("1" * 5000 + ".5", LONG),
        (":".join(["1"] * 240_000), LONG),  # 480 KB: PyYAML took 23.6 s to build it
        ("1:2:3:4:5", "a number of more than 4 ':' parts (base 60)"),
        ("1:2:3:4:5.5", "a number of more than 4 ':' parts (base 60)"),
        ("0x" + "f" * 3998, "an integer of more than 4,300 digits"),
    ],
    ids=[
        "decimal",
        "hex",
        "octal",
        "binary",
        "tagged",
        "float",
        "sexagesimal-480kb",
        "five-parts",
        "five-parts-float",
        "hex-over-4300-digits",
    ],
)
def test_a_number_is_bounded_before_it_is_built_in_every_base(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], value: str, reason: str
) -> None:
    """Python refuses only decimal integers of more than 4,300 digits: a hexadecimal, octal,
    binary or base-60 one was built (a base-60 one in quadratic time), and a long hexadecimal
    integer in a test input made `mendari validate` print a traceback."""
    root = tmp_path / "agent"
    shutil.copytree(TRIAGE_DIR, root)
    _append("agent.yaml", f"\nx_count: {value}\n")(root)
    lines = (root / "agent.yaml").read_text().count("\n")
    started = time.perf_counter()
    with pytest.raises(AgentValidationError) as raised:
        load_agent(root)
    assert time.perf_counter() - started < 2
    assert raised.value.problems == [f"agent.yaml: {reason} (line {lines}, column 10)"]
    assert main(["validate", str(root)]) == 1
    err = capsys.readouterr().err
    assert reason in err and "Traceback" not in err


def test_numbers_within_the_bounds_still_load(tmp_path: Path) -> None:
    path = tmp_path / "n.yaml"
    path.write_text(
        "a: 190:20:30\nb: 0x_ff\nc: 1:2:3:4.5\nd: 1_000\ne: -017\nf: 0b101\n"
        f"g: {'9' * 4300}\nh: 6.02e+23\n"
    )
    loaded = parse_yaml(path.read_bytes(), "n.yaml", "x")
    assert loaded["a"] == 685230 and loaded["b"] == 255 and loaded["d"] == 1000
    assert loaded["c"] == pytest.approx(((1 * 60 + 2) * 60 + 3) * 60 + 4.5)
    assert (loaded["e"], loaded["f"], loaded["g"], loaded["h"]) == (
        -15,
        5,
        int("9" * 4300),
        6.02e23,
    )


def test_a_constructor_error_names_its_type_and_place_never_the_value(tmp_path: Path) -> None:
    """A `ConstructorError` (a YAMLError) was reported with its text, which quotes the value and
    a snippet of the file; it is now named by type, line and column, like any other value the
    constructor cannot build."""
    root = tmp_path / "agent"
    shutil.copytree(TRIAGE_DIR, root)
    _append("agent.yaml", "\nx_secret: !canary-tag canary-value\n")(root)
    lines = (root / "agent.yaml").read_text().count("\n")
    with pytest.raises(AgentValidationError) as raised:
        load_agent(root)
    assert raised.value.problems == [
        f"agent.yaml: invalid YAML value (ConstructorError, line {lines}, column 11)"
    ]
    # A file that does not parse keeps the parser's message: what it expected, and where.
    _append("agent.yaml", "x_broken: [1, 2\n")(root)
    with pytest.raises(AgentValidationError, match=r"agent\.yaml: invalid YAML: while parsing"):
        load_agent(root)


@pytest.mark.parametrize(
    ("value", "what"),
    [
        ('"\\ud800"', "{file}: a string with a lone surrogate, which is not UTF-8 text ({at})"),
        ("!!binary aGVsbG8=", "{file}: a !!binary value, which JSON cannot hold ({at})"),
        ("2024-01-01", "{file} case {case}: not JSON, it holds a value of type date"),
        ("2024-01-01T10:00:00Z", "{file} case {case}: not JSON, it holds a value of type datetime"),
        (".nan", "{file} case {case}: not JSON, it holds a number that is not finite"),
        ("!!set {a}", "{file}: a !!set value, which JSON cannot hold ({at})"),
    ],
    ids=["surrogate", "binary", "date", "datetime", "nan", "set"],
)
def test_a_test_input_that_is_not_json_is_a_validation_problem(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], value: str, what: str
) -> None:
    """A lone surrogate or `!!binary` in a test input made `GET .../examples` answer 500 (the
    response could not be encoded), and a date was served as text though no input can hold
    one. Such an input is the agent's validation problem, named by type (and, for what the YAML
    loader refuses in any agent file, by place), never a 500 or a traceback."""
    outside = tmp_path / "outside"
    count = len(yaml.safe_load((TRIAGE_DIR / "tests" / "cases.yaml").read_text()))
    case = (
        f"\n- name: odd\n  input: {{message: {{body: hello}}, x_odd: {value}}}\n"
        "  expect: {outcome: AUTO}\n"
    )
    agent = _outsider(outside, _append("tests/cases.yaml", case))
    line = (agent / "tests" / "cases.yaml").read_text().count("\n") - 1
    problem = what.format(
        file="tests/cases.yaml", case=f"{count + 1} (odd)", at=f"line {line}, column 42"
    )
    with pytest.raises(AgentValidationError) as raised:
        load_cases(load_agent(agent))
    assert raised.value.problems == [problem]
    assert main(["validate", str(agent)]) == 1
    err = capsys.readouterr().err
    assert problem in err and "Traceback" not in err
    client = TestClient(create_app(db_path=tmp_path / "e.db", agents_paths=[outside, AGENTS]))
    examples = client.get("/api/agents/outsider/triage/examples")
    assert examples.status_code == 422
    assert problem in examples.text
    assert client.get("/openapi.json").status_code == 200
    assert client.get(f"/api/agents/{TRIAGE_ID}/examples").status_code == 200
