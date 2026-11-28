# SPDX-License-Identifier: Apache-2.0
"""Spec 0.4: the one spec version Mendari reads, and the manifest's `actions` section
(docs/spec/agent-manifest.md section 13).

A manifest says `spec_version: "0.4"`; any other is refused, saying so. It declares what an agent
may ask to have done, each action with an effect from a closed set and, optionally, a JSON Schema
for its arguments from a closed keyword subset, and it has every other feature the spec
documents (the card keys, the FHIR mapping's)."""

from __future__ import annotations

import copy
import json
import random
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from mendari import load_agent
from mendari.core import input_schema, spec
from mendari.core.agent import manifest_name
from mendari.core.spec import (
    MAX_ACTION_DESCRIPTION,
    MAX_ACTION_SCHEMA_CHARS,
    MAX_ACTIONS,
    SPEC_VERSION,
    ActionSpec,
    AgentManifest,
    Effect,
    action_schema_problems,
)
from mendari.errors import AgentValidationError
from mendari.providers.agent import request_body
from tests.conftest import DEMO_AGENT_DIR, ROOT, fixture_agents

MINI = ROOT / "tests" / "fixtures" / "mini-agents" / "actions-mini"


def raw_manifest(folder: Path = MINI) -> dict[str, Any]:
    name = manifest_name(folder)
    assert name is not None
    raw: dict[str, Any] = yaml.safe_load((folder / name).read_text())
    return raw


def with_action(**action: Any) -> dict[str, Any]:
    """The fixture's manifest with one more action, `extra`, as given."""
    raw = raw_manifest()
    raw["actions"]["extra"] = {"description": "An extra action.", "effect": "read", **action}
    return raw


def with_schema(schema: Any) -> dict[str, Any]:
    return with_action(schema=schema)


OBJECT = {"type": "object", "additionalProperties": False}

# -- versions -------------------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["0.1", "0.2", "0.3", "0.5", "1.0", "0.40", "", " 0.4", 0.4])
def test_spec_0_4_is_the_one_version_read_and_another_is_refused_saying_so(
    version: Any, tmp_path: Path
) -> None:
    """Only `"0.4"`: an older manifest is not read (nothing made before the first release is),
    nor a newer one, nor an unquoted 0.4 (YAML reads a number). The refusal names the version."""
    assert SPEC_VERSION == "0.4" and not hasattr(spec, "SPEC_VERSIONS")
    with pytest.raises(ValidationError, match=r'must be "0\.4"') as refused:
        AgentManifest.model_validate({**raw_manifest(), "spec_version": version})
    assert "the one spec version this engine reads" in str(refused.value)
    root = tmp_path / "agent"
    shutil.copytree(MINI, root)
    text = (root / "agent.yaml").read_text()
    (root / "agent.yaml").write_text(text.replace('spec_version: "0.4"', "spec_version: 0.4"))
    with pytest.raises(AgentValidationError, match=r'spec_version: must be "0\.4"'):
        load_agent(root)


def test_a_0_4_manifest_with_actions_loads_and_keeps_their_order() -> None:
    manifest = AgentManifest.model_validate(raw_manifest())
    assert manifest.spec_version == "0.4"
    assert list(manifest.actions) == [
        "look_up_medications",
        "draft_reply",
        "send_reply",
        "queue_refill",
        "file_note",
        "renew_prescription",
        "send_template",
        "assign_queue",
    ]
    assert {spec.effect for spec in manifest.actions.values()} == set(Effect)
    assert manifest.actions["queue_refill"].allow_auto is False
    assert manifest.actions["send_reply"].allow_auto is True
    assert manifest.actions["look_up_medications"].json_schema is None
    assert load_agent(MINI).manifest == manifest


def test_a_manifest_without_actions_declares_none() -> None:
    """An empty `actions` is what a dumped manifest carries: it counts as left out."""
    raw = raw_manifest()
    for left_out in ({k: v for k, v in raw.items() if k != "actions"}, {**raw, "actions": {}}):
        assert AgentManifest.model_validate(left_out).actions == {}


def test_every_agent_of_the_repository_loads_again_from_its_own_dump() -> None:
    """A dumped manifest carries the defaults (`actions: {}`, an action's `allow_auto: true`),
    and must load: so `allow_auto: true` is no error on any effect, an `order` included."""
    folders = [*fixture_agents(), DEMO_AGENT_DIR]
    assert MINI in folders
    for folder in folders:
        manifest = load_agent(folder).manifest
        dumped = manifest.model_dump(mode="json", by_alias=True)
        assert AgentManifest.model_validate(dumped) == manifest, folder.name
        assert "actions" in dumped
    order = AgentManifest.model_validate(raw_manifest()).actions["renew_prescription"]
    assert order.effect is Effect.ORDER and order.allow_auto is True


def test_the_bundled_agent_and_the_scaffold_are_as_this_wave_left_them() -> None:
    demo = load_agent(DEMO_AGENT_DIR).manifest
    assert (demo.spec_version, demo.version) == ("0.4", "0.3.0")  # 0.3.0: what it writes
    assert {name: (a.effect.value, a.allow_auto) for name, a in demo.actions.items()} == {
        "assign_queue": ("write", True),
        "send_acknowledgement": ("contact", False),
    }
    # Both take enumerated arguments, so neither can hold free text: ADR-0027 changes nothing of
    # what the demo shows.
    assert not any(action.free_text for action in demo.actions.values())
    # The scaffold is a 0.4 agent, the one version there is, and says what 0.4 offers it where
    # its author reads the version; it shows an action as a comment a person can uncomment.
    template = (ROOT / "src" / "mendari" / "templates" / "base" / "agent.yaml").read_text()
    assert (
        "# Spec 0.4, the one version Mendari reads. It also offers actions (the commented section "
        "at the\n# end of this file), a calibrated confidence (`mendari calibrate`) and a second "
        'signal (`repeat`,\n# or a `check` by a second model).\nspec_version: "0.4"\n'
    ) in template
    commented = [line[2:] for line in template.splitlines() if line.startswith("# ")]
    example = yaml.safe_load("\n".join(commented[commented.index("actions:") :]))
    declared = {
        name: ActionSpec.model_validate(action) for name, action in example["actions"].items()
    }
    assert {name: action.effect.value for name, action in declared.items()} == {
        "assign_queue": "write",
        "send_acknowledgement": "contact",
    }
    # What its comments say of each is true (ADR-0027): the write it calls released with an
    # AUTO decision takes an enumerated argument, and it says what a free string would change;
    # the contact is the author's own `allow_auto: false`.
    assert declared["assign_queue"].allow_auto and not declared["assign_queue"].free_text
    assert not declared["send_acknowledgement"].allow_auto
    assert (
        "#     effect: write  # released with an AUTO decision; otherwise when a reviewer "
        "confirms it\n"
        "#     schema:  # enumerated: with a free string, it is never released without a person\n"
    ) in template


# -- what 0.4 has of what the spec documents of 0.2 and 0.3 ---------------------------------------


def test_a_card_may_word_refused_input_and_a_rules_match() -> None:
    raw = raw_manifest()
    raw["cds_hooks"] = {
        "hook": "patient-view",
        "title": "Actions Mini",
        "description": "A test fixture.",
        "card": {
            "summaries": {"none": "Nothing.", "refused": "The input was refused."},
            "show": ["refill"],
            "also_show": {"abusive-message": {"summary": "A person reads this first."}},
        },
    }
    hooks = AgentManifest.model_validate(raw).cds_hooks
    assert hooks is not None and hooks.card is not None and hooks.card.names_refused()


def test_no_product_code_compares_a_spec_version() -> None:
    """There is one: nothing asks which version a manifest is, by equality or by order."""
    asked = re.compile(r"spec_version\s*(?:==|!=|<|>|\bnot in\b|\bin\b)")
    found = [
        (str(path.relative_to(ROOT)), number)
        for path in sorted((ROOT / "src" / "mendari").rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if asked.search(line.split("#")[0])
    ]
    assert found == []


def test_a_manifest_uses_the_second_signal_keys() -> None:
    """`repeat`, `calibration` and the `check` composite are a variant's like any key."""
    raw = raw_manifest()
    raw["providers"]["reader"]["variants"]["agent"]["repeat"] = 3
    assert AgentManifest.model_validate(raw).spec_version == "0.4"


# -- the section's own rules ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "said"),
    [
        ({"effect": "delete"}, "effect"),
        ({"effect": "WRITE"}, "effect"),
        ({"effect": None}, "effect"),
        ({"description": ""}, "description"),
        ({"description": "   "}, "a description a person can read"),
        ({"description": "x" * (MAX_ACTION_DESCRIPTION + 1)}, "description"),
        ({"gate": "decision"}, "gate"),  # no key names a gate: the runtime's
        ({"release": "always"}, "release"),
        ({"allow_auto": "yes please"}, "allow_auto"),
        ({"schema": "schemas/arguments.json"}, "schema"),  # inline only
    ],
)
def test_an_action_that_is_not_of_the_sections_shape_is_refused(
    action: dict[str, Any], said: str
) -> None:
    with pytest.raises(ValidationError, match=said):
        AgentManifest.model_validate(with_action(**action))


@pytest.mark.parametrize(
    "name", ["Send", "send-reply", "1st", "send reply", "", "a" * 65, "send.reply", "_send"]
)
def test_an_actions_name_is_a_key_of_at_most_64_characters(name: str) -> None:
    raw = raw_manifest()
    raw["actions"][name] = {"description": "An action.", "effect": "read"}
    with pytest.raises(ValidationError, match="action name") as refused:
        AgentManifest.model_validate(raw)
    assert "a" * 65 not in str(refused.value).split("action name", 1)[1].split("must match")[0]
    raw = raw_manifest()
    raw["actions"]["a" * 64] = {"description": "An action.", "effect": "read"}
    assert "a" * 64 in AgentManifest.model_validate(raw).actions


def test_an_agent_declares_at_most_sixteen_actions_of_bounded_size_together() -> None:
    raw = raw_manifest()
    raw["actions"] = {
        f"action_{n}": {"description": "An action.", "effect": "read"} for n in range(MAX_ACTIONS)
    }
    assert len(AgentManifest.model_validate(raw).actions) == MAX_ACTIONS
    raw["actions"]["one_more"] = {"description": "An action.", "effect": "read"}
    with pytest.raises(ValidationError, match=f"at most {MAX_ACTIONS} actions"):
        AgentManifest.model_validate(raw)
    # Each schema within its own bound, and still too long together as one request sends them.
    values = [f"value-{n:03d}-" + "x" * 180 for n in range(18)]
    big = {**OBJECT, "properties": {"choice": {"type": "string", "enum": values}}}
    assert len(json.dumps(big)) <= MAX_ACTION_SCHEMA_CHARS
    raw["actions"] = {
        f"action_{n}": {"description": "An action.", "effect": "read", "schema": big}
        for n in range(MAX_ACTIONS)
    }
    with pytest.raises(ValidationError, match="characters together"):
        AgentManifest.model_validate(raw)


# -- the arguments schema: a closed subset --------------------------------------------------------

ALLOWED: dict[str, Any] = {
    **OBJECT,
    "title": "Arguments",
    "description": "What the action takes.",
    "required": ["message"],
    "properties": {
        "message": {"type": "string", "minLength": 1, "maxLength": 2000},
        "days": {"type": "integer", "minimum": 1, "maximum": 90},
        "dose": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 10.5},
        "urgent": {"type": "boolean"},
        "route": {"type": "string", "enum": ["oral", "topical"]},
        "kind": {"type": "string", "const": "refill"},
        "tags": {"type": "array", "minItems": 0, "maxItems": 5, "items": {"type": "string"}},
        "who": {**OBJECT, "properties": {"role": {"type": "string"}}},
    },
}


def test_a_schema_of_the_subset_is_taken_as_written() -> None:
    assert action_schema_problems(ALLOWED) == []
    manifest = AgentManifest.model_validate(with_schema(ALLOWED))
    assert manifest.actions["extra"].json_schema == ALLOWED
    # It is sent to the agent as written, with the action's name, description and effect.
    sent = manifest.actions["extra"].request("extra")
    assert sent == {
        "name": "extra",
        "description": "An extra action.",
        "effect": "read",
        "schema": ALLOWED,
    }
    assert set(ALLOWED) | {key for p in ALLOWED["properties"].values() for key in p} == set(
        spec.ACTION_SCHEMA_KEYWORDS
    )


@pytest.mark.parametrize(
    "keyword",
    [
        "pattern", "format", "$ref", "$defs", "anyOf", "allOf", "oneOf", "not", "if",
        "patternProperties", "unevaluatedProperties", "dependentRequired", "$schema", "default",
        "prefixItems", "contains", "multipleOf", "propertyNames", "uniqueItems", "examples",
    ],
)  # fmt: skip
def test_any_other_keyword_makes_the_agent_invalid_named(keyword: str) -> None:
    for schema in (
        {**OBJECT, keyword: {}},
        {**OBJECT, "properties": {"message": {"type": "string", keyword: "x"}}},
    ):
        problems = action_schema_problems(schema)
        assert problems and keyword in problems[0], problems
        with pytest.raises(ValidationError, match="keywords an arguments schema may not use"):
            AgentManifest.model_validate(with_schema(schema))


def nested(depth: int) -> dict[str, Any]:
    schema: dict[str, Any] = {**OBJECT, "properties": {"leaf": {"type": "string"}}}
    for _ in range(depth - 1):
        schema = {**OBJECT, "properties": {"inner": schema}}
    return schema


@pytest.mark.parametrize(
    ("schema", "said"),
    [
        ([], "it must be a mapping"),
        ({"type": "string"}, None),  # a root that is no object: see below
        ({"type": "object"}, "additionalProperties: false"),
        ({"type": "object", "additionalProperties": True}, "additionalProperties: false"),
        ({**OBJECT, "properties": {"who": {"type": "object"}}}, "additionalProperties: false"),
        ({**OBJECT, "type": ["object", "null"]}, "`type` is one of"),
        ({**OBJECT, "type": "null"}, "`type` is one of"),
        ({"additionalProperties": False}, "`type` is one of"),
        ({**OBJECT, "properties": {"a b": {"type": "string"}}}, "a property's name matches"),
        ({**OBJECT, "properties": {"x" * 65: {"type": "string"}}}, "a property's name matches"),
        ({**OBJECT, "properties": {"a": "string"}}, "a schema is a mapping"),
        ({**OBJECT, "properties": []}, "`properties` is a mapping"),
        ({**OBJECT, "required": ["missing"]}, "`required` names only the object's properties"),
        ({**OBJECT, "required": "message"}, "`required` names only"),
        ({**OBJECT, "properties": {"a": {"type": "string", "enum": []}}}, "`enum` lists 1 to"),
        (
            {**OBJECT, "properties": {"a": {"type": "string", "enum": [{"x": 1}]}}},
            "an `enum` value is no object or list",
        ),
        (
            {**OBJECT, "properties": {"a": {"type": "string", "enum": ["y" * 201]}}},
            "at most 200 characters",
        ),
        (
            {**OBJECT, "properties": {"a": {"type": "string", "enum": list(range(101))}}},
            "`enum` lists 1 to 100",
        ),
        ({**OBJECT, "properties": {"a": {"type": "string", "const": [1]}}}, "`const` is no object"),
        ({**OBJECT, "properties": {"a": {"type": "string", "minLength": -1}}}, "whole number"),
        ({**OBJECT, "properties": {"a": {"type": "string", "maxLength": "9"}}}, "whole number"),
        ({**OBJECT, "properties": {"a": {"type": "integer", "minimum": "1"}}}, "is a number"),
        ({**OBJECT, "properties": {"a": {"type": "integer", "maximum": True}}}, "is a number"),
        ({**OBJECT, "properties": {"a": {"type": "string", "items": {}}}}, "only an array"),
        ({**OBJECT, "properties": {"a": {"type": "string", "properties": {}}}}, "only an object"),
        ({**OBJECT, "title": 5}, "`title` is a text"),
        (
            {**OBJECT, "properties": {f"p{n}": {"type": "string"} for n in range(33)}},
            "a mapping of at most 32",
        ),
        (nested(5), "objects nest at most 4 deep"),
        ({**OBJECT, "description": "x" * MAX_ACTION_SCHEMA_CHARS}, "characters as JSON"),
        ({**OBJECT, "properties": {"a": {"type": "number", "minimum": float("nan")}}}, "JSON"),
    ],
)
def test_each_bound_of_the_arguments_schema(schema: Any, said: str | None) -> None:
    problems = action_schema_problems(schema)
    if said is None:  # a root of another type is a schema of the subset, and takes no object
        assert problems == []
        assert AgentManifest.model_validate(with_schema(schema)).actions["extra"].json_schema
        return
    assert problems and any(said in problem for problem in problems), problems
    with pytest.raises(ValidationError, match="schema"):
        AgentManifest.model_validate(with_schema(schema))


def test_the_bounds_themselves_are_allowed() -> None:
    assert action_schema_problems(nested(4)) == []
    wide = {**OBJECT, "properties": {f"p{n}": {"type": "string"} for n in range(32)}}
    assert action_schema_problems(wide) == []
    hundred = {**OBJECT, "properties": {"a": {"type": "integer", "enum": list(range(100))}}}
    assert action_schema_problems(hundred) == []


def test_a_schema_too_deep_to_walk_by_recursion_is_a_problem_and_never_a_crash() -> None:
    schema: dict[str, Any] = {"type": "string"}
    for _ in range(5000):
        schema = {"type": "array", "items": schema}
    problems = action_schema_problems({**OBJECT, "properties": {"a": schema}})
    assert problems  # too long as JSON, or not what JSON carries: named, not raised
    with pytest.raises(ValidationError):
        AgentManifest.model_validate(with_schema({**OBJECT, "properties": {"a": schema}}))


def test_a_problem_names_where_and_never_a_value() -> None:
    schema = {
        **OBJECT,
        "properties": {"who": {**OBJECT, "properties": {"role": {"type": "SECRET-TYPE"}}}},
    }
    (problem,) = action_schema_problems(schema)
    assert problem.startswith("who.role: ") and "SECRET-TYPE" not in problem
    listed = {**OBJECT, "properties": {"tags": {"type": "array", "items": {"type": "x"}}}}
    assert action_schema_problems(listed)[0].startswith("tags[]: ")


def _taking(properties: dict[str, Any]) -> dict[str, Any]:
    return {**OBJECT, "properties": properties}


def _listed(count: int) -> dict[str, Any]:
    """A string limited to `count` values."""
    return {"type": "string", "enum": [f"v{n}" for n in range(count)]}


LETTER = {"type": "string", "enum": list("abcdefghijklmnopqrstuvwxyz")}
DIGIT = {"type": "integer", "enum": list(range(10))}


@pytest.mark.parametrize(
    ("schema", "free"),
    [
        (None, True),  # no schema: the arguments are whatever the agent sends
        (_taking({"message": {"type": "string", "maxLength": 2000}}), True),
        (_taking({"code": {"type": "string", "maxLength": 8}}), True),  # a dose fits in 8
        (_taking({"who": _taking({"note": {"type": "string"}})}), True),  # however deep
        ({"type": "string"}, True),  # a root that is a string
        # A list puts a text together out of its items, however each item is limited.
        (_taking({"tags": {"type": "array", "items": {"type": "string"}}}), True),
        (_taking({"anything": {"type": "array"}}), True),
        (_taking({"rows": {"type": "array", "items": _taking({"n": {"type": "string"}})}}), True),
        (_taking({"picked": {"type": "array", "items": {"type": "integer"}}}), True),
        (_taking({"spelled": {"type": "array", "items": LETTER, "maxItems": 200}}), True),
        (_taking({"routes": {"type": "array", "items": _listed(1), "maxItems": 1}}), True),
        # A number nothing lists can be any dose; an integer between two bounds is a range the
        # manifest states.
        (_taking({"days": {"type": "integer"}}), True),
        (_taking({"dose": {"type": "number"}}), True),
        (_taking({"dose": {"type": "number", "minimum": 0, "maximum": 10}}), True),
        (_taking({"days": {"type": "integer", "minimum": 1}}), True),
        (_taking({"days": {"type": "integer", "maximum": 90}}), True),
        (_taking({"days": {"type": "integer", "minimum": 1, "maximum": 90}}), False),
        (_taking({"n": {"type": "integer", "exclusiveMinimum": 0, "exclusiveMaximum": 4}}), False),
        (_taking({"dose": {"type": "number", "enum": [0.5, 1, 2.5]}}), False),
        (
            {
                **_taking({"n": {"type": "integer", "minimum": 1, "maximum": 1_000_000}}),
                "required": ["n"],
            },
            False,  # a million values, and no more
        ),
        (
            {
                **_taking({"n": {"type": "integer", "minimum": 0, "maximum": 1_000_000}}),
                "required": ["n"],
            },
            True,
        ),
        # A menu: every value is one the manifest lists.
        (_taking({}), False),  # no arguments at all
        (_taking({"queue": {"type": "string", "enum": ["refill", "front_desk"]}}), False),
        (_taking({"kind": {"type": "string", "const": "refill"}}), False),
        (_taking({"urgent": {"type": "boolean"}}), False),
        (
            {
                **_taking({"a": _listed(100), "b": _listed(100), "c": _listed(100)}),
                "required": ["a", "b", "c"],
            },
            False,
        ),
        # Past a million arguments in all, the properties spell a text a character at a time.
        (_taking({f"c{n}": LETTER for n in range(22)}), True),
        (_taking({f"d{n}": DIGIT for n in range(32)}), True),
        (
            {
                **_taking({f"c{n}": LETTER for n in range(5)}),
                "required": [f"c{n}" for n in range(5)],
            },
            True,
        ),
        (
            {
                **_taking(
                    {"a": _listed(100), "b": _listed(100), "c": _listed(100), "d": _listed(2)}
                ),
                "required": ["a", "b", "c", "d"],
            },
            True,
        ),
        (
            _taking({"inner": _taking({"a": _listed(100), "b": _listed(100)}), "c": _listed(100)}),
            True,  # 101 * 101 + 1, times 101: a property left out counts once more
        ),
    ],
)
def test_arguments_hold_free_text_unless_they_are_a_menu(
    schema: dict[str, Any] | None, free: bool
) -> None:
    """ADR-0027: an action's arguments can hold free text unless they are a menu, a fixed set of
    values the manifest lists. Every string is an `enum` or a `const` (no pattern narrows one,
    and a short one still holds a dose); every number is listed, or an integer between two
    bounds; there is no list; and the schema admits at most a million arguments in all, so that
    no text is spelled out of enumerated pieces, a list of letters or a property a character.
    Decided from the manifest alone."""
    declared: dict[str, Any] = {"description": "Does something.", "effect": "write"}
    if schema is not None:
        assert action_schema_problems(schema) == []  # each is a schema a manifest may declare
        declared["schema"] = schema
    action = ActionSpec.model_validate(declared)
    assert action.free_text is free
    assert spec.holds_free_text(schema) is free


def test_what_is_no_schema_a_manifest_may_declare_is_free_text_and_never_a_crash() -> None:
    """`holds_free_text` reads schemas a manifest's load accepted. Given anything else it says
    free text, the strict answer: an object open to properties nothing names, a bound that is
    no number, a schema that is no mapping."""
    open_object = {"type": "object", "properties": {"queue": _listed(2)}}
    assert action_schema_problems(open_object) != []
    for odd in (
        open_object,
        {**open_object, "additionalProperties": True},
        _taking({"n": {"type": "integer", "minimum": True, "maximum": 4}}),
        _taking({"n": {"type": "integer", "minimum": float("-inf"), "maximum": 4}}),
        _taking({"n": {"type": "integer", "minimum": 0, "maximum": float("nan")}}),
        _taking({"n": {"type": "string", "enum": "abc"}}),
        _taking({"n": []}),
        {"type": "object", "additionalProperties": False, "properties": []},
        {"type": "null"},
    ):
        assert spec.holds_free_text(odd) is True, odd
    # Bounds that leave no integer at all admit nothing: no text either.
    empty = _taking({"n": {"type": "integer", "minimum": 5, "maximum": 4}})
    assert action_schema_problems(empty) == [] and spec.holds_free_text(empty) is False


def _random_menu(rng: random.Random, depth: int = 1) -> dict[str, Any]:
    """An object schema whose every value is listed: enums, a const, booleans, bounded
    integers, and objects of the same, to the depth a schema may nest."""

    def leaf() -> dict[str, Any]:
        kind = rng.choice(("texts", "const", "boolean", "range", "numbers"))
        if kind == "texts":
            return {"type": "string", "enum": [f"v{n}" for n in range(rng.randint(1, 4))]}
        if kind == "const":
            return {"type": "string", "const": "only"}
        if kind == "boolean":
            return {"type": "boolean"}
        if kind == "range":
            low = rng.randint(-3, 3)
            return {"type": "integer", "minimum": low, "maximum": low + rng.randint(0, 3)}
        return {"type": "number", "enum": [0.5, 1, 2.5][: rng.randint(1, 3)]}

    properties = {
        f"p{n}": _random_menu(rng, depth + 1) if depth < 3 and rng.random() < 0.25 else leaf()
        for n in range(rng.randint(0, 4))
    }
    required = [key for key in properties if rng.random() < 0.6]
    return {**_taking(properties), "required": required}


def _one_of(rng: random.Random, schema: dict[str, Any]) -> Any:
    """One value `schema` lists (`_random_menu`'s kinds)."""
    if schema["type"] == "object":
        return {
            key: _one_of(rng, child)
            for key, child in schema["properties"].items()
            if key in schema["required"] or rng.random() < 0.5
        }
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return rng.choice(schema["enum"])
    if schema["type"] == "boolean":
        return rng.random() < 0.5
    return rng.randint(schema["minimum"], schema["maximum"])


def test_a_menu_takes_no_value_its_manifest_does_not_list() -> None:
    """Held against the validator that checks an action's arguments (`actions.validators`):
    for random schemas the manifest's load accepts and `holds_free_text` judges a menu, an
    argument made of listed values is accepted, and the same argument is refused once any one
    value in it is a text, a fraction or a list nothing lists, or a property nothing names is
    added anywhere. So what such an action is released with is a value the manifest lists."""
    rng = random.Random(27)
    unlisted: tuple[Any, ...] = ("ZQ free text", 1_000_000.25, ["ZQ", "free"], {"zq": 1}, None)
    for _ in range(300):
        schema = _random_menu(rng)
        assert action_schema_problems(schema) == []
        assert spec.holds_free_text(schema) is False
        checker = input_schema.validator(schema, formats=False)
        argument = _one_of(rng, schema)
        assert list(checker.iter_errors(argument)) == [], (schema, argument)
        # Every place a value sits in the argument: the path to it from the root.
        places: list[tuple[str, ...]] = []
        stack: list[tuple[tuple[str, ...], Any]] = [((), argument)]
        while stack:
            path, value = stack.pop()
            if isinstance(value, dict):
                places.append((*path, "zq_unnamed"))  # a property nothing names, here
                stack.extend(((*path, key), inner) for key, inner in value.items())
            else:
                places.append(path)
        for path in places:
            for other in unlisted:
                changed = copy.deepcopy(argument)
                at = changed
                for key in path[:-1]:
                    at = at[key]
                at[path[-1]] = other
                assert list(checker.iter_errors(changed)), (schema, path, other)


def test_three_full_lists_of_options_are_a_menu_and_a_property_left_out_counts() -> None:
    """The bound is a million arguments: three required properties of a hundred values each
    are exactly that many, and a menu. Not required, each may also be left out, which is one
    more value: 101 x 101 x 101 is past the bound."""
    full = _taking({"a": _listed(100), "b": _listed(100), "c": _listed(100)})
    assert spec.holds_free_text({**full, "required": ["a", "b", "c"]}) is False
    assert spec.holds_free_text({**full, "required": ["a", "b"]}) is True
    assert spec.holds_free_text(full) is True
    assert spec.MAX_ACTION_MENU == 1_000_000


def test_the_fixture_declares_two_actions_whose_arguments_hold_no_free_text() -> None:
    """The actions fixture, as the gate's tests read it: the spec's five and the filed note each
    take a string that is not enumerated, or declare no schema; `send_template` (a `contact`)
    and `assign_queue` (a `write`) take enumerated arguments only, so they are the ones an AUTO
    decision still releases (ADR-0027)."""
    manifest = AgentManifest.model_validate(raw_manifest())
    assert {name: action.free_text for name, action in manifest.actions.items()} == {
        "look_up_medications": True,  # no schema
        "draft_reply": True,
        "send_reply": True,
        "queue_refill": True,
        "file_note": True,
        "renew_prescription": True,  # no schema
        "send_template": False,
        "assign_queue": False,
    }
    assert manifest.actions["send_template"].effect is Effect.CONTACT
    assert manifest.actions["assign_queue"].effect is Effect.WRITE
    assert (
        manifest.actions["send_template"].allow_auto and manifest.actions["assign_queue"].allow_auto
    )


def test_free_text_is_found_without_recursion_in_a_schema_as_deep_as_one_may_be() -> None:
    assert spec.holds_free_text(nested(4)) is True  # its leaf is a string, four objects down
    deep: dict[str, Any] = {"type": "string", "enum": ["a", "b"]}
    for _ in range(3):
        deep = _taking({"a": deep})
    assert action_schema_problems(deep) == [] and spec.holds_free_text(deep) is False


# -- the request ----------------------------------------------------------------------------------


def test_a_request_carries_the_declared_actions_in_order_and_an_older_ones_is_as_it_was() -> None:
    manifest = AgentManifest.model_validate(raw_manifest())
    questions = {"request": manifest.questions["request"]}
    inputs = {"message": {"body": "A refill, please."}}
    plain = request_body(manifest.id, "reader", questions, inputs)
    assert "actions" not in plain
    # An agent that declares none: byte for byte what it was.
    assert request_body(manifest.id, "reader", questions, inputs, actions=None) == plain
    assert request_body(manifest.id, "reader", questions, inputs, actions={}) == plain
    sent = request_body(manifest.id, "reader", questions, inputs, actions=manifest.actions)
    assert {key: value for key, value in sent.items() if key != "actions"} == plain
    assert [action["name"] for action in sent["actions"]] == list(manifest.actions)
    read, draft = sent["actions"][0], sent["actions"][1]
    assert read == {
        "name": "look_up_medications",
        "description": "Read the patient's active medication list.",
        "effect": "read",
    }  # no `schema` key without one, and never `allow_auto`: the gate is not the agent's to read
    assert draft["schema"]["required"] == ["message"] and "allow_auto" not in draft
    assert json.loads(json.dumps(sent)) == sent
