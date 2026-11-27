# SPDX-License-Identifier: Apache-2.0
"""Caps on what multiplies the pattern work of an agent the deployment does not own (plan B11,
stage 2; core/caps.py).

Such an agent (here, one pulled from a registry whose digest is no bundled agent's:
`tests/support/engine.py`) runs on the bounded engine: its patterns on RE2, whose time is linear
in the text, so a decision's work is bounded once every number that multiplies it is: the text
(at most 10,000 characters), the patterns run on it, and what these tests cap. Each cap refuses
the agent at load, in plain words. Agents the deployment owns are not capped: they load and
decide exactly as before.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from mendari import Decision, load_agent
from mendari.core import caps
from mendari.core.origin import long_text
from mendari.core.patterns import MAX_AGENT_PATTERNS
from mendari.errors import AgentValidationError
from tests.conftest import DEMO_AGENT_DIR, FIXTURE_AGENTS, ROOT, TRIAGE_DIR
from tests.support.engine import RULES, SORTER_SCHEMA, pulled, sorter

SCAFFOLD_SCHEMA = ROOT / "src" / "mendari" / "templates" / "base" / "schemas"
TOO_LONG = (
    "input rejected: the input's texts together: longer than 10,000 characters, the most an "
    "agent from a registry reads"
)


def refused(root: Path) -> list[str]:
    """Why the agent at `root`, pulled from a registry, is refused at load."""
    with pytest.raises(AgentValidationError) as error:
        pulled(root)
    return error.value.problems


def doubling(depth: int, leaf: dict[str, Any]) -> dict[str, Any]:
    """The finding's schema: `$defs/t{i}` is allOf two `$ref`s to `t{i+1}`, so the leaf's
    pattern runs 2**depth times on `message`."""
    defs: dict[str, Any] = {
        f"t{i}": {"allOf": [{"$ref": f"#/$defs/t{i + 1}"}, {"$ref": f"#/$defs/t{i + 1}"}]}
        for i in range(depth)
    }
    defs[f"t{depth}"] = leaf
    return {
        "type": "object",
        "properties": {"message": {"$ref": "#/$defs/t0"}},
        "$defs": defs,
    }


# -- (a) text_fields name each field once --------------------------------------------------------


def test_a_bounded_agent_may_not_name_a_text_field_twice(tmp_path: Path) -> None:
    """The finding: `message` listed ten times made a 9,990-character message a 99,999-character
    text, and one question pattern took 22 s on it."""
    root = sorter(tmp_path, rules=lambda r: r.update(text_fields=["message"] * 10))
    assert refused(root) == [
        f"{RULES}: text_fields names 'message' 10 times; name each field once (a field named "
        "twice is read twice)"
    ]


def test_nor_may_a_questions_own_text_fields(tmp_path: Path) -> None:
    def edit(rules: dict[str, Any]) -> None:
        rules["questions"]["topic"]["text_fields"] = ["message", "subject", "message"]

    assert refused(sorter(tmp_path, rules=edit)) == [
        f"{RULES}: questions.topic.text_fields names 'message' 2 times; name each field once (a "
        "field named twice is read twice)"
    ]


# -- (b) the negation check ----------------------------------------------------------------------


@pytest.mark.parametrize("window", [10_000, "10000", 201])
def test_a_bounded_agents_negation_window_is_at_most_200_characters(
    tmp_path: Path, window: Any
) -> None:
    """The finding: a window of 10,000 made every negated match search the text before it, and
    100 negatable patterns took 24 s. A number written as a string is read as one too."""
    root = sorter(tmp_path, rules=lambda r: r.update(negation_cues=["no"], negation_window=window))
    assert refused(root) == [
        f"{RULES}: negation_window is {int(window)}; a registry or store agent's is at most "
        "200 characters (30 to 40 is usual)"
    ]


def test_a_window_of_200_characters_is_allowed(tmp_path: Path) -> None:
    root = sorter(tmp_path, rules=lambda r: r.update(negation_cues=["no"], negation_window=200))
    assert pulled(root).bounded


def test_a_bounded_agent_has_at_most_50_negation_cues(tmp_path: Path) -> None:
    """Each negated match searches its window for every cue: 20,000 cues and 500 negatable
    patterns took 4 s on 10,000 characters."""
    cues = [f"cue{i}" for i in range(51)]
    root = sorter(tmp_path, rules=lambda r: r.update(negation_cues=cues))
    assert refused(root) == [
        f"{RULES}: 51 negation_cues; a registry or store agent has at most 50 (7 to 23 is usual)"
    ]


# -- what the deployment owns is not capped ------------------------------------------------------


def test_an_agent_that_is_not_bounded_is_not_capped(tmp_path: Path) -> None:
    """The same agent as the operator's own (a local folder): the same settings load and decide
    as before."""

    def edit(rules: dict[str, Any]) -> None:
        rules.update(text_fields=["message"] * 10, negation_cues=["no"], negation_window=10_000)

    schema = doubling(6, {"type": "string", "pattern": "."})
    root = sorter(
        tmp_path, rules=edit, schema=lambda s: s.update(schema), agent_id="someone/sorter"
    )
    agent = load_agent(root)
    assert not agent.bounded
    assert Decision(agent).decide({"message": "my bill"}).outcome is not None


def test_the_maintained_agents_load_as_before_and_would_pass_the_caps() -> None:
    """The caps are set above what the agents the deployment owns use: the bundled demo agent's
    and the test fixtures' (the triage fixture's screen window of 30 characters and its 5 cues),
    their schemas' pattern runs."""
    bundled = [p for p in sorted(FIXTURE_AGENTS.iterdir()) if (p / "agent.yaml").is_file()]
    assert bundled
    for root in [*bundled, DEMO_AGENT_DIR]:
        agent = load_agent(root, origin="maintained")
        assert not agent.bounded
        for relative, data in agent.parsed.items():
            assert caps.rules_problems(relative, data) == [], relative
        assert caps.schema_runs(agent.input_schema) <= caps.MAX_SCHEMA_RUNS, root.name
        size = caps.rules_runs(agent.manifest, agent.parsed, caps.rules_size)
        size += caps.schema_runs(agent.input_schema, caps.schema_size)
        assert 0 < size <= caps.MAX_AGENT_PATTERN_SIZE, root.name
    screen = load_agent(TRIAGE_DIR).parsed["logic/screen_rules.yaml"]
    assert (screen["negation_window"], len(screen["negation_cues"])) == (30, 5)


# -- (c) an input schema's pattern runs on one value ----------------------------------------------


def test_the_doubling_schema_from_the_finding_is_refused(tmp_path: Path) -> None:
    """Six steps of allOf over two `$ref`s ran one pattern 64 times on `message` (16.5 s); the
    old count saw one pattern."""
    leaf = {"type": "string", "maxLength": 10_000, "not": {"pattern": "[^.!?]+\\?"}}
    schema = doubling(6, leaf)
    assert caps.schema_runs(schema) == 64
    root = sorter(tmp_path, schema=lambda s: s.update(schema))
    assert refused(root) == [
        "input schema: its patterns can run 64 times on one value; a registry or store "
        "agent's schema runs at most 50 on any value or key (a $ref, allOf, anyOf or oneOf that "
        "reaches the same pattern more than once multiplies it)"
    ]


def test_the_cap_message_names_patternproperties_with_additionalproperties(
    tmp_path: Path,
) -> None:
    """20 key patterns where additionalProperties is set run 60 times on a key; the message
    said only $ref, allOf, anyOf or oneOf, which the schema does not use."""

    def schema(s: dict[str, Any]) -> None:
        s["properties"]["codes"] = {
            "type": "object",
            "patternProperties": {f"^x_{i}$": {} for i in range(20)},
            "additionalProperties": False,
        }

    (problem,) = refused(sorter(tmp_path, schema=schema))
    assert problem == (
        "input schema: its patterns can run 60 times on one value; a registry or store "
        "agent's schema runs at most 50 on any value or key (each patternProperties pattern "
        "counts three times "
        "where additionalProperties is also set (on each key, then again as additionalProperties "
        "searches them and for its error message), and a $ref, allOf, anyOf or oneOf that "
        "reaches the same pattern more than once multiplies it)"
    )
    pages = sorted((ROOT / "docs" / "spec").glob("agent-manifest*.md"))
    spec = " ".join(" ".join(page.read_text().split()) for page in pages)
    assert "each `patternProperties` pattern counts three times where" in spec


def test_a_deep_doubling_schema_is_counted_without_walking_every_path() -> None:
    assert caps.schema_runs(doubling(200, {"pattern": "x"})) == 2**200


def test_the_scaffolds_and_the_fixtures_schemas_are_allowed() -> None:
    scaffold = json.loads((SCAFFOLD_SCHEMA / "input.schema.json").read_text())
    for schema in (scaffold, SORTER_SCHEMA):
        assert caps.schema_runs(schema) == 0


@pytest.mark.parametrize(
    ("schema", "runs"),
    [
        ({"pattern": "a"}, 1),
        (
            {
                "pattern": "a",
                "allOf": [{"pattern": "b"}, {"$ref": "#/$defs/c"}],
                "$defs": {"c": {"pattern": "c"}},
            },
            3,
        ),
        ({"anyOf": [{"pattern": "a"}, {"pattern": "b"}], "oneOf": [{"pattern": "c"}]}, 3),
        (
            {
                "not": {"pattern": "a"},
                "if": {"pattern": "b"},
                "then": {"pattern": "c"},
                "else": {"pattern": "d"},
            },
            4,
        ),
        ({"dependentSchemas": {"x": {"pattern": "a"}, "y": {"pattern": "b"}}}, 2),
        # Each key: every patternProperties pattern, and again for additionalProperties (which
        # searches them for keys no pattern names) and its error message.
        ({"patternProperties": {"a": {}, "b": {}}}, 2),
        ({"patternProperties": {"a": {}, "b": {}}, "additionalProperties": False}, 6),
        ({"propertyNames": {"pattern": "a", "allOf": [{"pattern": "b"}]}}, 2),
        # A value matches one of `properties` (or additionalProperties), and every pattern of
        # patternProperties: one property with a pattern each is one run on any value.
        ({"properties": {f"p{i}": {"pattern": "a"} for i in range(60)}}, 1),
        (
            {
                "properties": {"a": {"pattern": "a"}},
                "additionalProperties": {"allOf": [{"pattern": "b"}, {"pattern": "c"}]},
            },
            2,
        ),
        ({"patternProperties": {"x": {"pattern": "a"}, "y": {"pattern": "b"}}}, 2),
        (
            {
                "prefixItems": [{"pattern": "a"}],
                "items": {"pattern": "b"},
                "contains": {"pattern": "c"},
            },
            2,
        ),
        # Applicators at one place and below it add up where they meet.
        (
            {
                "allOf": [
                    {"properties": {"m": {"pattern": "a"}}},
                    {"properties": {"m": {"pattern": "b"}}},
                ]
            },
            2,
        ),
        ({"items": {"items": {"allOf": [{"pattern": "a"}, {"pattern": "b"}]}}}, 2),
        (True, 0),
        ({"properties": {"a": False}}, 0),
    ],
)
def test_pattern_runs_are_counted_through_every_applicator(schema: Any, runs: int) -> None:
    assert caps.schema_runs(schema) == runs


@pytest.mark.parametrize(
    "schema",
    [
        # A tree whose nodes have a pattern: twice the nodes each step down (the finding's
        # doubling, over the input's depth instead of the schema's).
        {
            "$defs": {
                "n": {"allOf": [{"$ref": "#/$defs/m"}, {"$ref": "#/$defs/m"}]},
                "m": {"properties": {"child": {"$ref": "#/$defs/n"}, "name": {"pattern": "a"}}},
            },
            "$ref": "#/$defs/n",
        },
        # A cycle with the pattern on it.
        {"properties": {"child": {"$ref": "#"}}, "pattern": "a"},
        # A cycle in place (jsonschema recurses without end).
        {"$defs": {"a": {"allOf": [{"$ref": "#/$defs/a"}], "pattern": "x"}}, "$ref": "#/$defs/a"},
    ],
)
def test_a_reference_cycle_that_reaches_a_pattern_is_refused(schema: Any) -> None:
    with pytest.raises(caps.SchemaCycle):
        caps.schema_runs(schema)
    (problem,) = caps.schema_problems(schema, rules_runs=0)
    assert problem.startswith("input schema: ") and problem.endswith(
        ": a $ref leads back to a schema holding it, and patterns run there or below it; a "
        "registry or store agent's schema may not refer back to itself where its patterns run"
    )


def test_a_reference_cycle_without_patterns_is_allowed() -> None:
    tree = {
        "$defs": {"n": {"properties": {"children": {"items": {"$ref": "#/$defs/n"}}}}},
        "$ref": "#/$defs/n",
        "properties": {"name": {"pattern": "a"}},
    }
    assert caps.schema_runs(tree) == 1


def test_the_rules_and_the_schema_run_at_most_max_agent_patterns_together(tmp_path: Path) -> None:
    """A rules pattern runs once for each question it answers and each composite member that
    holds it: 26 questions sharing 20 patterns (a YAML alias) run 520."""
    shared = [{"name": f"p{i}", "pattern": f"word{i}", "weight": 1} for i in range(20)]

    def rules(r: dict[str, Any]) -> None:
        r["questions"] = {
            f"q{i}": {"default": "other", "labels": {"billing": shared}} for i in range(26)
        }

    def manifest(m: dict[str, Any]) -> None:
        criteria = m["questions"]["topic"]["criteria"]
        m["questions"] = {
            f"q{i}": {"instructions": "What is it about?", "criteria": criteria} for i in range(26)
        }
        m["providers"]["reader"]["answers"] = [f"q{i}" for i in range(26)]
        m["policy"]["primary"] = "q0"

    root = sorter(tmp_path, rules=rules, manifest=manifest)
    problems = refused(root)
    assert problems == [
        "the rules run 520 patterns on each input (each pattern once for each question it "
        "answers and each composite member holding it) and the input schema up to 0 on one "
        f"value: 520 together; a registry or store agent runs at most {MAX_AGENT_PATTERNS}"
    ]


def test_a_composite_counts_each_rules_member(tmp_path: Path) -> None:
    def manifest(m: dict[str, Any]) -> None:
        member = {"type": "rules", "config": RULES}
        m["providers"]["reader"]["variants"]["checked"] = {
            "type": "check",
            "members": {f"m{i}": member for i in range(2)},
        }

    agent = pulled(sorter(tmp_path, manifest=manifest))
    # The slot's busiest variant, the check: each member runs the fixture's two patterns.
    assert caps.rules_runs(agent.manifest, agent.parsed) == 2 * 2


# -- (e) the size of the patterns run on one input ------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "size"),
    [
        ("bill|invoice", 11),
        (r"\bpain\b", 4),
        (r"\w+", 1),
        (r"x*", 1),
        (r"a{5,}", 5),
        (r"a{1,50}", 50),
        (r"(?:ab|cd){1,10}", 40),
        (r".{0,999}a.{0,999}b0", 2_001),
        (r"(?:.{1,40}){1,25}x", 1_001),
        ("^$", 1),  # every run reads the text: at least 1
    ],
)
def test_a_patterns_size_is_its_characters_repeated_as_its_counts_say(
    pattern: str, size: int
) -> None:
    assert caps.rules_size(pattern) == size


def test_patterns_whose_sizes_come_to_more_than_3000_are_refused(tmp_path: Path) -> None:
    """The finding: 500 patterns `.{0,999}a.{0,999}b<i>`, within every other cap, took 148 s on
    10,000 characters (0.3 s each). Two already come to more than the cap."""
    gaps = [
        {"name": f"g{i}", "pattern": f".{{0,999}}a.{{0,999}}b{i}", "weight": 1} for i in range(2)
    ]
    root = sorter(tmp_path, rules=lambda r: r["questions"]["topic"]["labels"].update(billing=gaps))
    assert refused(root) == [
        "its patterns' size comes to 4,029 on each input: the rules' 4,029 (each pattern's once "
        "for each question it answers and each composite member holding it) and the input "
        "schema's 0 on one value or key; a registry or store agent's comes to at most 3,000, "
        "as RE2's time on each character grows with it. A pattern's size is the characters, "
        "classes and .s it holds, each as often as a count repeats it (.{0,999} is 999, \\w+ "
        "is 1): use smaller counts, or fewer patterns"
    ]
    one = sorter(
        tmp_path / "one", rules=lambda r: r["questions"]["topic"]["labels"].update(billing=gaps[:1])
    )
    agent = pulled(one)
    size = caps.rules_runs(agent.manifest, agent.parsed, caps.rules_size)
    size += caps.schema_runs(agent.input_schema, caps.schema_size)
    assert size == 2_001 + 27  # and the fixture's scheduling pattern
    root = sorter(
        tmp_path / "local",
        rules=lambda r: r["questions"]["topic"]["labels"].update(billing=gaps),
        agent_id="someone/sorter",
    )
    assert not load_agent(root).bounded  # the deployment's own: not capped


def test_the_schema_patterns_size_counts_on_one_value(tmp_path: Path) -> None:
    def schema(s: dict[str, Any]) -> None:
        s["properties"]["message"]["not"] = {"pattern": "a{1,999}"}
        s["properties"]["codes"] = {
            "type": "object",
            "patternProperties": {"^x{1,999}$": {}},  # 999, three times on each key
            "additionalProperties": False,
        }

    (problem,) = refused(sorter(tmp_path, schema=schema))
    assert problem.startswith(
        "its patterns' size comes to 3,051 on each input: the rules' 54 (each pattern's once "
        "for each question it answers and each composite member holding it) and the input "
        "schema's 2,997 on one value or key;"
    )


# -- (d) keys count with the texts only where patterns run on them -------------------------------


def observation(i: int) -> dict[str, Any]:
    """A FHIR Observation as an input might carry it (synthetic)."""
    return {
        "resourceType": "Observation",
        "id": f"obs-{i:04d}",
        "status": "final",
        "code": {
            "coding": [{"system": "http://loinc.org", "code": "8867-4", "display": "Heart rate"}]
        },
        "subject": {"reference": "Patient/example"},
        "effectiveDateTime": "2026-10-01T09:00:00Z",
        "valueQuantity": {
            "value": 72,
            "unit": "/min",
            "system": "http://unitsofmeasure.org",
            "code": "/min",
        },
    }


def test_keys_are_counted_only_when_the_schema_runs_patterns_on_them() -> None:
    """The finding: 38 Observations were refused, their keys counted as text the patterns would
    read; their strings alone first pass 10,000 characters at 75."""
    inputs = {"observations": [observation(i) for i in range(38)]}
    assert long_text(inputs) == "the input's texts together"
    assert long_text(inputs, count_keys=False) is None
    assert long_text({"observations": [observation(i) for i in range(75)]}, count_keys=False)
    # Not counted, a key is still held to the length on its own.
    assert long_text({"k" * 10_001: 1}, count_keys=False) == "(root)"


@pytest.mark.parametrize(
    ("schema", "keys"),
    [
        ({"type": "object"}, False),
        ({"properties": {"a": {"pattern": "x"}}}, False),
        ({"patternProperties": {"x": {}}}, True),
        ({"properties": {"a": {"propertyNames": {"maxLength": 3}}}}, True),
        ({"$defs": {"a": {"patternProperties": {"x": {}}}}}, True),
    ],
)
def test_whether_a_schema_runs_patterns_on_keys(schema: Any, keys: bool) -> None:
    assert caps.patterns_on_keys(schema) is keys


def test_a_bounded_agent_reads_long_keys_its_schema_runs_no_pattern_on(tmp_path: Path) -> None:
    """The fixture's schema runs no pattern on keys: an input whose texts and keys together are
    longer than 10,000 characters, each within it, is checked against the schema (which refuses
    the extra field), not refused for its length."""
    root = sorter(tmp_path)
    keys = {"k" * 9_000 + str(i): 1 for i in range(3)}
    result = Decision(pulled(root)).decide({"message": "hi", "extra": keys})
    assert result.reasons != (TOO_LONG,)
    assert result.schema_problems and "characters" not in result.schema_problems[0]


def test_a_bounded_agent_counts_keys_its_schema_runs_patterns_on(tmp_path: Path) -> None:
    def schema(s: dict[str, Any]) -> None:
        s["properties"]["extra"] = {"type": "object", "patternProperties": {"^k": {}}}

    root = sorter(tmp_path, schema=schema)
    keys = {"k" * 9_000 + str(i): 1 for i in range(3)}
    result = Decision(pulled(root)).decide({"message": "hi", "extra": keys})
    assert result.reasons == (TOO_LONG,)


def test_the_rules_provider_refuses_the_same_for_an_agent_built_by_hand(tmp_path: Path) -> None:
    """An `Agent` not loaded by `load_agent` (a test's, a tool's) gets the caps when its rules
    provider is built."""
    agent = pulled(sorter(tmp_path))
    rules = {**agent.parsed[RULES], "text_fields": ["message", "message"]}
    with pytest.raises(AgentValidationError) as error:
        Decision(replace(agent, parsed={RULES: rules}))
    assert error.value.problems == [
        f"{RULES}: text_fields names 'message' 2 times; name each field once (a field named "
        "twice is read twice)"
    ]
