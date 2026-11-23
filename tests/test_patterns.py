# SPDX-License-Identifier: Apache-2.0
"""A pack's own patterns are compiled with `re` when it loads (core/patterns.py)."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from mendari import Decision, load_pack
from mendari.cli import main
from mendari.core import patterns
from mendari.errors import PackValidationError
from mendari.server.app import create_app
from tests.conftest import INBOX_DIR, cpu_time, message

RED_FLAGS = "logic/red_flags.yaml"


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    """A copy of the inbox pack to edit."""
    root = tmp_path / "pack"
    shutil.copytree(INBOX_DIR, root)
    return root


def with_pattern(root: Path, pattern: str, *, name: str = "author") -> Path:
    """The copied inbox pack with one more `urgent` pattern in its safety screen."""
    path = root / RED_FLAGS
    rules = yaml.safe_load(path.read_text())
    urgent = rules["questions"]["urgency"]["labels"]["urgent"]
    urgent.append({"name": name, "pattern": pattern, "weight": 1})
    path.write_text(yaml.safe_dump(rules, sort_keys=False, allow_unicode=True))
    return root


def urgent_count(root: Path) -> int:
    rules = yaml.safe_load((root / RED_FLAGS).read_text())
    return len(rules["questions"]["urgency"]["labels"]["urgent"])


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
def test_a_rules_pattern_that_is_not_a_pack_pattern_names_the_pack_file_and_rule(
    inbox: Path, pattern: str, detail: str
) -> None:
    root = with_pattern(inbox, pattern)
    index = urgent_count(root) - 1
    with pytest.raises(PackValidationError) as raised:
        load_pack(root)
    assert raised.value.pack == "mendari/patient-inbox-router"
    assert raised.value.problems == [
        f"{RED_FLAGS}: questions.urgency.labels.urgent[{index}] (author): {detail}"
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
def test_a_schema_pattern_that_is_not_a_pack_pattern_fails_at_load(
    inbox: Path, pattern: str, detail: str
) -> None:
    # `a{99999999999}` raised OverflowError from jsonschema's own schema check: a traceback in
    # `mendari validate`, and once a server that did not start.
    root = with_schema_pattern(inbox, pattern)
    with pytest.raises(PackValidationError) as raised:
        load_pack(root)
    (problem,) = raised.value.problems
    assert problem.startswith(f"input schema: properties/message/properties/body/pattern: {detail}")


def test_a_pack_holds_at_most_500_patterns_counted_before_any_is_compiled(
    inbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = inbox / RED_FLAGS
    rules = yaml.safe_load(path.read_text())
    urgent = rules["questions"]["urgency"]["labels"]["urgent"]
    urgent += [{"name": f"p{i}", "pattern": f"word{i}", "weight": 1} for i in range(500)]
    path.write_text(yaml.safe_dump(rules, sort_keys=False))
    compiled: list[str] = []
    monkeypatch.setattr(patterns, "problem", lambda text, flags=0: compiled.append(text))
    with pytest.raises(PackValidationError, match="at most 500 patterns"):
        load_pack(inbox)
    assert compiled == []


def test_rules_patterns_compile_as_the_provider_runs_them(inbox: Path) -> None:
    # Case ignored, as the rules provider compiles them: `(?-i:...)` and inline flags behave the
    # same at load and in a decision.
    root = with_pattern(inbox, r"(?-i:ZQSTAT)\b")
    decision = Decision(load_pack(root), providers={"safety_screen": "rules"})
    assert "urgent:author" in decision.decide(message("please ZQSTAT")).answers["urgency"].evidence
    assert "urgent:author" not in decision.decide(message("zqstat")).answers["urgency"].evidence


def test_a_pattern_used_by_a_non_default_variant_or_a_member_is_checked_too(
    inbox: Path,
) -> None:
    manifest = yaml.safe_load((inbox / "pack.yaml").read_text())
    (inbox / "logic" / "extra.yaml").write_text(
        (inbox / RED_FLAGS).read_text().replace("weight: 1", "weight: 1", 1)
    )
    rules = yaml.safe_load((inbox / "logic" / "extra.yaml").read_text())
    rules["questions"]["urgency"]["labels"]["urgent"][0]["pattern"] = "(bad"
    (inbox / "logic" / "extra.yaml").write_text(yaml.safe_dump(rules, sort_keys=False))
    screen = manifest["providers"]["safety_screen"]["variants"]
    screen["extra"] = {
        "type": "any_of",
        "members": {
            "a": {"type": "rules", "config": RED_FLAGS},
            "b": {"type": "rules", "config": "logic/extra.yaml"},
        },
    }
    (inbox / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    with pytest.raises(PackValidationError) as raised:
        load_pack(inbox)
    (problem,) = raised.value.problems
    assert problem.startswith("logic/extra.yaml: questions.urgency.labels.urgent[0]")


def test_validate_reports_an_invalid_pattern_without_a_traceback(
    inbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = with_pattern(inbox, "a{99999999999}")
    assert main(["validate", str(root)]) == 1
    err = capsys.readouterr().err
    assert "the repetition number is too large" in err and "Traceback" not in err


def test_an_invalid_pattern_lists_the_pack_and_never_fails_health(
    inbox: Path, tmp_path: Path
) -> None:
    root = with_pattern(inbox, "(unclosed")
    manifest = (root / "pack.yaml").read_text()
    (root / "pack.yaml").write_text(
        manifest.replace("id: mendari/patient-inbox-router", "id: outsider/inbox", 1)
    )
    client = TestClient(
        create_app(db_path=tmp_path / "p.db", packs_paths=[root.parent, INBOX_DIR.parent])
    )
    health = client.get("/api/health")
    assert health.status_code == 200
    (problem,) = health.json()["invalid"].values()
    assert "missing ), unterminated subpattern" in problem
    assert health.json()["packs"] == 5


def test_the_shipped_patterns_load_and_compile_cheaply() -> None:
    with cpu_time() as spent:
        for root in sorted(INBOX_DIR.parent.iterdir()):
            if (root / "pack.yaml").is_file():
                re.purge()
                load_pack(root)
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


def test_pattern_properties_keys_are_pack_patterns_counted_and_checked(inbox: Path) -> None:
    """jsonschema runs every `patternProperties` key with `re.search` on each validation, but
    only `pattern` values were counted and length-checked: 1,000 keys of 3,000 characters
    loaded, and thrashing `re`'s cache cost every decision over a second."""
    shipped = load_pack(inbox)
    existing = len(list(patterns.schema_patterns(shipped.input_schema))) + sum(
        len(list(patterns.rules_patterns(data))) for data in shipped.parsed.values()
    )
    with pytest.raises(PackValidationError, match="501 patterns; an agent holds at most 500"):
        load_pack(with_pattern_properties(inbox, [f"^k{i}$" for i in range(501 - existing)]))
    long = "^z" + "a" * 2000
    with pytest.raises(PackValidationError) as raised:
        load_pack(with_pattern_properties(inbox, ["^ok$", long]))
    assert raised.value.problems == [
        f"input schema: properties/probe/patternProperties/{long[:40]}...: "
        "longer than 2000 characters"
    ]
    with pytest.raises(PackValidationError) as raised:
        load_pack(with_pattern_properties(inbox, ["(bad"]))
    (problem,) = raised.value.problems
    assert problem.startswith("input schema: properties/probe/patternProperties/(bad: invalid")
    assert load_pack(with_pattern_properties(inbox, ["^x-[a-z]+$"])).id


def test_schema_patterns_lists_pattern_properties_keys_where_they_are() -> None:
    schema = {"patternProperties": {"^a$": {"pattern": "b"}}, "properties": {"pattern": {}}}
    assert list(patterns.schema_patterns(schema)) == [
        ("patternProperties/^a$", "^a$"),
        ("patternProperties/^a$/pattern", "b"),
    ]
