# SPDX-License-Identifier: Apache-2.0
"""An agent's own patterns are compiled with `re` when it loads (core/patterns.py)."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from mendari import Decision, load_agent
from mendari.cli import main
from mendari.core import patterns
from mendari.errors import AgentValidationError
from mendari.server.app import create_app
from tests.conftest import FIXTURE_AGENTS, TRIAGE_DIR, cpu_time, message

# The triage fixture's screen: same-day symptoms, under the label "yes".
RED_FLAGS = "logic/screen_rules.yaml"


@pytest.fixture
def triage_copy(tmp_path: Path) -> Path:
    """A copy of the triage fixture to edit."""
    root = tmp_path / "agent"
    shutil.copytree(TRIAGE_DIR, root)
    return root


def with_pattern(root: Path, pattern: str, *, name: str = "author") -> Path:
    """The copied triage fixture with one more same-day symptom pattern in its screen."""
    path = root / RED_FLAGS
    rules = yaml.safe_load(path.read_text())
    urgent = rules["questions"]["urgency"]["labels"]["yes"]
    urgent.append({"name": name, "pattern": pattern, "weight": 1})
    path.write_text(yaml.safe_dump(rules, sort_keys=False, allow_unicode=True))
    return root


def urgent_count(root: Path) -> int:
    rules = yaml.safe_load((root / RED_FLAGS).read_text())
    return len(rules["questions"]["urgency"]["labels"]["yes"])


def with_schema_pattern(root: Path, pattern: str) -> Path:
    path = root / "schemas" / "input.schema.json"
    schema = json.loads(path.read_text())
    schema["properties"]["message"]["properties"]["body"]["pattern"] = pattern
    path.write_text(json.dumps(schema))
    return root


@pytest.mark.parametrize(
    ("pattern", "detail"),
    [
        (
            "(unclosed",
            "invalid regular expression: missing ), unterminated subpattern at position 0",
        ),
        ("a{99999999999}", "invalid regular expression: the repetition number is too large"),
        ("x" * 2001, "longer than 2000 characters"),
    ],
    ids=["re-error", "overflow", "too-long"],
)
def test_a_rules_pattern_that_is_not_an_agent_pattern_names_the_agent_file_and_rule(
    triage_copy: Path, pattern: str, detail: str
) -> None:
    root = with_pattern(triage_copy, pattern)
    index = urgent_count(root) - 1
    with pytest.raises(AgentValidationError) as raised:
        load_agent(root)
    assert raised.value.agent == "mendari-test/triage"
    assert raised.value.problems == [
        f"{RED_FLAGS}: questions.urgency.labels.yes[{index}] (author): {detail}"
    ]


@pytest.mark.parametrize(
    ("pattern", "detail"),
    [
        ("[unclosed", "invalid regular expression: unterminated character set"),
        ("a{99999999999}", "invalid regular expression: the repetition number is too large"),
        ("(" * 1000 + ")" * 1000, "invalid regular expression: "),
    ],
    ids=["re-error", "overflow", "too-deep"],
)
def test_a_schema_pattern_that_is_not_an_agent_pattern_fails_at_load(
    triage_copy: Path, pattern: str, detail: str
) -> None:
    # `a{99999999999}` raised OverflowError from jsonschema's own schema check: a traceback in
    # `mendari validate`, and once a server that did not start.
    root = with_schema_pattern(triage_copy, pattern)
    with pytest.raises(AgentValidationError) as raised:
        load_agent(root)
    (problem,) = raised.value.problems
    assert problem.startswith(f"input schema: properties/message/properties/body/pattern: {detail}")


def test_an_agent_holds_at_most_500_patterns_counted_before_any_is_compiled(
    triage_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = triage_copy / RED_FLAGS
    rules = yaml.safe_load(path.read_text())
    urgent = rules["questions"]["urgency"]["labels"]["yes"]
    urgent += [{"name": f"p{i}", "pattern": f"word{i}", "weight": 1} for i in range(500)]
    path.write_text(yaml.safe_dump(rules, sort_keys=False))
    compiled: list[str] = []
    monkeypatch.setattr(patterns, "problem", lambda text, flags=0: compiled.append(text))
    with pytest.raises(AgentValidationError, match="at most 500 patterns"):
        load_agent(triage_copy)
    assert compiled == []


def test_rules_patterns_compile_as_the_provider_runs_them(triage_copy: Path) -> None:
    # Case ignored, as the rules provider compiles them: `(?-i:...)` and inline flags behave the
    # same at load and in a decision.
    root = with_pattern(triage_copy, r"(?-i:ZQSTAT)\b")
    decision = Decision(load_agent(root), providers={"screen": "rules"})
    assert "yes:author" in decision.decide(message("please ZQSTAT")).answers["urgency"].evidence
    assert "yes:author" not in decision.decide(message("zqstat")).answers["urgency"].evidence


def test_a_pattern_used_by_a_non_default_variant_or_a_member_is_checked_too(
    triage_copy: Path,
) -> None:
    manifest = yaml.safe_load((triage_copy / "agent.yaml").read_text())
    (triage_copy / "logic" / "extra.yaml").write_text(
        (triage_copy / RED_FLAGS).read_text().replace("weight: 1", "weight: 1", 1)
    )
    rules = yaml.safe_load((triage_copy / "logic" / "extra.yaml").read_text())
    rules["questions"]["urgency"]["labels"]["yes"][0]["pattern"] = "(bad"
    (triage_copy / "logic" / "extra.yaml").write_text(yaml.safe_dump(rules, sort_keys=False))
    screen = manifest["providers"]["screen"]["variants"]
    screen["extra"] = {
        "type": "check",
        "members": {
            "a": {"type": "rules", "config": RED_FLAGS},
            "b": {"type": "rules", "config": "logic/extra.yaml"},
        },
    }
    (triage_copy / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    with pytest.raises(AgentValidationError) as raised:
        load_agent(triage_copy)
    (problem,) = raised.value.problems
    assert problem.startswith("logic/extra.yaml: questions.urgency.labels.yes[0]")


def test_validate_reports_an_invalid_pattern_without_a_traceback(
    triage_copy: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = with_pattern(triage_copy, "a{99999999999}")
    assert main(["validate", str(root)]) == 1
    err = capsys.readouterr().err
    assert "the repetition number is too large" in err and "Traceback" not in err


def test_an_invalid_pattern_lists_the_agent_and_never_fails_health(
    triage_copy: Path, tmp_path: Path
) -> None:
    root = with_pattern(triage_copy, "(unclosed")
    manifest = (root / "agent.yaml").read_text()
    (root / "agent.yaml").write_text(
        manifest.replace("id: mendari-test/triage", "id: outsider/triage", 1)
    )
    client = TestClient(
        create_app(db_path=tmp_path / "p.db", agents_paths=[root.parent, FIXTURE_AGENTS])
    )
    health = client.get("/api/health")
    assert health.status_code == 200
    (problem,) = health.json()["invalid"].values()
    assert "missing ), unterminated subpattern" in problem
    served = [d for d in FIXTURE_AGENTS.iterdir() if (d / "agent.yaml").is_file()]
    assert health.json()["agents"] == len(served)  # every fixture's agent, not the outsider


def test_the_shipped_patterns_load_and_compile_cheaply() -> None:
    with cpu_time() as spent:
        for root in sorted(FIXTURE_AGENTS.iterdir()):
            if (root / "agent.yaml").is_file():
                re.purge()
                load_agent(root)
    assert spent() < 1


def test_patterns_in_an_aliased_rules_file_are_found_once_per_node() -> None:
    shared: dict[str, Any] = {"name": "n", "pattern": "x", "weight": 1}
    rules = {"questions": {"q": {"labels": {"a": [shared, shared], "b": [shared]}}}}
    assert [where for where, _ in patterns.rules_patterns(rules)] == ["questions.q.labels.a[0] (n)"]
    node: dict[str, Any] = {"pattern": "x"}
    schema = {"a": node, "b": [node, node]}
    assert len(list(patterns.schema_patterns(schema))) == 1


def with_pattern_properties(root: Path, keys: list[str]) -> Path:
    path = root / "schemas" / "input.schema.json"
    schema = json.loads(path.read_text())
    schema["properties"]["probe"] = {
        "type": "object",
        "patternProperties": {key: {} for key in keys},
    }
    path.write_text(json.dumps(schema))
    return root


def test_pattern_properties_keys_are_agent_patterns_counted_and_checked(triage_copy: Path) -> None:
    """jsonschema runs every `patternProperties` key with `re.search` on each validation, but
    only `pattern` values were counted and length-checked: 1,000 keys of 3,000 characters
    loaded, and thrashing `re`'s cache cost every decision over a second."""
    shipped = load_agent(triage_copy)
    existing = len(list(patterns.schema_patterns(shipped.input_schema))) + sum(
        len(list(patterns.rules_patterns(data))) for data in shipped.parsed.values()
    )
    with pytest.raises(AgentValidationError, match="501 patterns; an agent holds at most 500"):
        load_agent(with_pattern_properties(triage_copy, [f"^k{i}$" for i in range(501 - existing)]))
    long = "^z" + "a" * 2000
    with pytest.raises(AgentValidationError) as raised:
        load_agent(with_pattern_properties(triage_copy, ["^ok$", long]))
    assert raised.value.problems == [
        f"input schema: properties/probe/patternProperties/{long[:40]}...: "
        "longer than 2000 characters"
    ]
    with pytest.raises(AgentValidationError) as raised:
        load_agent(with_pattern_properties(triage_copy, ["(bad"]))
    (problem,) = raised.value.problems
    assert problem.startswith("input schema: properties/probe/patternProperties/(bad: invalid")
    assert load_agent(with_pattern_properties(triage_copy, ["^x-[a-z]+$"])).id


def test_schema_patterns_lists_pattern_properties_keys_where_they_are() -> None:
    schema = {"patternProperties": {"^a$": {"pattern": "b"}}, "properties": {"pattern": {}}}
    assert list(patterns.schema_patterns(schema)) == [
        ("patternProperties/^a$", "^a$"),
        ("patternProperties/^a$/pattern", "b"),
    ]
