# SPDX-License-Identifier: Apache-2.0
"""The threat model stays true to the code (docs/security/threat-model.md): every threat has one
id, the rows run in STRIDE order with numbers in order, and every test a row names as "Checked
by" exists. Parallel lanes once added rows with the same id; this catches that and a test that
was renamed or removed. Its "Agent content" part (LP-16g) names every input a pack has, and cites
only rows and tests that exist."""

from __future__ import annotations

import re
from pathlib import Path

from mendari.core.agent import _PROVIDER_FILE_KEYS
from mendari.providers import PROVIDER_CONFIG_MODELS, PROVIDER_TYPES

ROOT = Path(__file__).resolve().parents[1]
THREAT_MODEL = ROOT / "docs" / "security" / "threat-model.md"
ROW = re.compile(r"^\| ([STRIDE])(\d+) \|")
STRIDE = "STRIDE"
AGENT_CONTENT = "## Agent content"
AGENT_CONTENT_PARTS = [
    "Inputs",
    "Trust boundary",
    "What an agent can do",
    "What an agent cannot do",
]
CITED_ROW = re.compile(r"\b[STRIDE]\d+\b")
PROVIDER_TABLE = "| Provider type | Decides | Variant keys |"


def rows() -> list[str]:
    return [line for line in THREAT_MODEL.read_text().splitlines() if ROW.match(line)]


def defined_tests() -> set[str]:
    tests = "\n".join(path.read_text() for path in (ROOT / "tests").rglob("test_*.py"))
    return set(re.findall(r"^\s*def (test_\w+)\(", tests, flags=re.MULTILINE))


def agent_content() -> str:
    """The "Agent content" part: from its heading to the next part's."""
    text = THREAT_MODEL.read_text()
    start = text.index(f"\n{AGENT_CONTENT}\n")
    return text[start : text.index("\n## ", start + 1)]


def test_every_threat_has_one_id_and_the_rows_are_in_order() -> None:
    ids = []
    for line in rows():
        match = ROW.match(line)
        assert match is not None
        ids.append((STRIDE.index(match[1]), int(match[2])))
    assert ids, "no threat rows found"
    assert len(ids) == len(set(ids)), "an id is used twice"
    assert ids == sorted(ids), "rows are not in STRIDE order with their numbers in order"


def test_every_test_a_row_names_exists() -> None:
    named = set()
    for line in rows():
        checked_by = line.rstrip(" |").rsplit(" | ", 1)[-1]
        named |= set(re.findall(r"`(test_\w+)`", checked_by))
        for path in re.findall(r"`(tests/[\w/.]+)`", checked_by):
            assert (ROOT / path).exists(), path
    assert named, "no tests named"
    assert sorted(named - defined_tests()) == []


def test_the_agent_content_part_names_every_agent_input() -> None:
    """The part sits between the STRIDE table and the residual risks, in its four subsections,
    and its Inputs name, as code, every manifest field naming a file a provider reads, the
    schema, the FHIR mapping, the tests, the datasets, the policy, the CDS Hooks prefetch and
    every provider type: a new one fails here until the part says what an agent can set with it."""
    lines = THREAT_MODEL.read_text().splitlines()
    last_row = max(number for number, line in enumerate(lines) if ROW.match(line))
    assert last_row < lines.index(AGENT_CONTENT) < lines.index("## Residual risks and assumptions")
    pieces = re.split(r"^### (.+)$", agent_content(), flags=re.MULTILINE)
    parts = dict(zip(pieces[1::2], pieces[2::2], strict=True))
    assert list(parts) == AGENT_CONTENT_PARTS
    spans = set(re.findall(r"`([^`\n]+)`", parts["Inputs"]))
    named = spans | {word for span in spans for word in span.split(".")}
    inputs = [
        *_PROVIDER_FILE_KEYS,
        "inputs.schema",
        "inputs.fhir",
        "tests",
        "evaluation.datasets",
        "policy",
        "prefetch",
        *PROVIDER_TYPES,
    ]
    assert [name for name in inputs if name not in named] == []


def test_the_provider_table_lists_each_types_closed_config_keys() -> None:
    """The Inputs part's provider table has one row per provider type, and its last column
    names exactly the keys that type's closed config takes beside `type`: a key added to or
    removed from a config fails here until the table says so."""
    part = agent_content()
    table = part[part.index(PROVIDER_TABLE) :].split("\n\n", 1)[0].splitlines()[2:]
    cells = [line.strip("| ").split(" | ") for line in table]
    keys = {kind.strip("`"): set(re.findall(r"`(\w+)`", row[-1])) for kind, *row in cells}
    assert sorted(keys) == sorted(PROVIDER_TYPES)
    for kind, model in PROVIDER_CONFIG_MODELS.items():
        assert keys[kind] == set(model.model_fields) - {"type"}, kind


def test_the_agent_content_part_cites_only_rows_that_exist() -> None:
    """Every row id the part cites is a row of the table, and no line of the part reads as a row
    (`rows` would take it for one): it cites rows in prose, or in a table whose first column is
    not an id."""
    part = agent_content()
    ids = {f"{match[1]}{match[2]}" for match in map(ROW.match, rows()) if match is not None}
    cited = set(CITED_ROW.findall(part))
    assert cited, "the part cites no row"
    assert sorted(cited - ids) == []
    assert [line for line in part.splitlines() if ROW.match(line)] == []


def test_the_agent_content_part_names_tests_that_exist() -> None:
    named = set(re.findall(r"`(test_\w+)`", agent_content()))
    assert named, "the part names no test"
    assert sorted(named - defined_tests()) == []
