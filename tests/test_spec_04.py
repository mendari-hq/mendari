# SPDX-License-Identifier: Apache-2.0
"""Spec 0.4: the manifest's `actions` section (docs/spec/agent-manifest-0.4.md section 2).

A 0.4 manifest declares what an agent may ask to have done, each action with an effect from a
closed set and, optionally, a JSON Schema for its arguments from a closed keyword subset. A 0.1
to 0.3 manifest that declares any is refused, naming spec 0.4; every 0.1 to 0.3 agent still
loads, and a 0.4 agent keeps every feature 0.2 and 0.3 added (the version checks compare, they
do not test equality)."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from mendari import load_pack
from mendari.core import spec
from mendari.core.pack import manifest_name
from mendari.core.spec import (
    MAX_ACTION_DESCRIPTION,
    MAX_ACTION_SCHEMA_CHARS,
    MAX_ACTIONS,
    SPEC_VERSION,
    SPEC_VERSIONS,
    ActionSpec,
    Effect,
    PackManifest,
    action_schema_problems,
    at_least,
)
from mendari.errors import PackValidationError
from mendari.providers.agent import request_body
from mendari.standards.fhir import FhirAdapter
from tests.conftest import DEMO_AGENT_DIR, ROOT, fixture_agents

MINI = ROOT / "tests" / "fixtures" / "mini-agents" / "actions-mini"
PRESCREEN = ROOT / "tests" / "fixtures" / "mini-agents" / "prescreen-mini"


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


def test_the_newest_spec_is_0_4_and_versions_compare_by_their_place() -> None:
    assert SPEC_VERSIONS == ("0.1", "0.2", "0.3", "0.4") and SPEC_VERSION == "0.4"
    for position, version in enumerate(SPEC_VERSIONS):
        for wanted_at, wanted in enumerate(SPEC_VERSIONS):
            assert at_least(version, wanted) is (position >= wanted_at)
    # A version this engine does not read is at least nothing, whichever way it sorts as text.
    for unknown in ("0.5", "1.0", "0.10", "", "0.3 "):
        assert not any(at_least(unknown, wanted) for wanted in SPEC_VERSIONS)


def test_a_0_4_manifest_with_actions_loads_and_keeps_their_order() -> None:
    manifest = PackManifest.model_validate(raw_manifest())
    assert manifest.spec_version == "0.4"
    assert list(manifest.actions) == [
        "look_up_medications",
        "draft_reply",
        "send_reply",
        "queue_refill",
        "file_note",
        "renew_prescription",
    ]
    assert {spec.effect for spec in manifest.actions.values()} == set(Effect)
    assert manifest.actions["queue_refill"].allow_auto is False
    assert manifest.actions["send_reply"].allow_auto is True
    assert manifest.actions["look_up_medications"].json_schema is None
    assert load_pack(MINI).manifest == manifest


@pytest.mark.parametrize("version", ["0.1", "0.2", "0.3"])
def test_an_older_manifest_with_actions_is_refused_naming_spec_0_4(version: str) -> None:
    raw = {**raw_manifest(), "spec_version": version}
    if version == "0.1":  # 0.1 has no boolean question and no provenance: leave only the actions
        raw["questions"].pop("abusive")
        raw["providers"]["reader"]["answers"] = ["request"]
        raw["policy"]["rules"] = []
        raw["card"].pop("provenance")
    with pytest.raises(ValidationError, match=r"\['actions'\] use spec 0.4 features") as refused:
        PackManifest.model_validate(raw)
    assert "set spec_version: '0.4'" in str(refused.value)
    # Without them the same manifest is a valid one of that version: an empty `actions` is what a
    # dumped manifest carries, so it counts as left out.
    for left_out in ({k: v for k, v in raw.items() if k != "actions"}, {**raw, "actions": {}}):
        assert PackManifest.model_validate(left_out).actions == {}


def test_a_0_4_manifest_without_actions_is_a_0_3_manifest() -> None:
    raw = raw_manifest()
    raw.pop("actions")
    assert PackManifest.model_validate(raw).actions == {}
    with pytest.raises(ValidationError, match="spec_version"):
        PackManifest.model_validate({**raw, "spec_version": "0.5"})


def test_every_agent_of_the_repository_loads_again_from_its_own_dump() -> None:
    """A dumped manifest carries the defaults (`actions: {}`, an action's `allow_auto: true`),
    and must load: so `allow_auto: true` is no error on any effect, an `order` included."""
    folders = [*fixture_agents(), DEMO_AGENT_DIR]
    assert MINI in folders
    for folder in folders:
        manifest = load_pack(folder).manifest
        dumped = manifest.model_dump(mode="json", by_alias=True)
        assert PackManifest.model_validate(dumped) == manifest, folder.name
        assert "actions" in dumped
    order = PackManifest.model_validate(raw_manifest()).actions["renew_prescription"]
    assert order.effect is Effect.ORDER and order.allow_auto is True


def test_the_bundled_agent_and_the_scaffold_are_as_this_wave_left_them() -> None:
    demo = load_pack(DEMO_AGENT_DIR).manifest
    assert (demo.spec_version, demo.version) == ("0.4", "0.2.0")
    assert {name: (a.effect.value, a.allow_auto) for name, a in demo.actions.items()} == {
        "assign_queue": ("write", True),
        "send_acknowledgement": ("contact", False),
    }
    # The scaffold stays a 0.2 agent, since it uses nothing newer, and says so where its author
    # sets the version; it shows an action as a comment a person can uncomment.
    template = (ROOT / "src" / "mendari" / "templates" / "agent" / "agent.yaml").read_text()
    assert (
        '# Spec 0.2 is all this agent uses, so every engine that reads 0.2 runs it. Set "0.4" '
        "for what\n# 0.4 adds: actions (the commented section at the end of this file), a "
        "calibrated confidence\n# (`mendari calibrate`) and a second signal (`repeat`, or a "
        '`check` by a second model).\nspec_version: "0.2"\n'
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


# -- a 0.4 agent keeps what 0.2 and 0.3 added ---------------------------------------------------


@pytest.mark.parametrize("feature", ["mapping", "criteria"])
def test_a_0_4_agent_uses_a_0_3_mapping_and_a_0_3_criteria_key(
    tmp_path: Path, feature: str
) -> None:
    """The checks that once tested `== "0.3"` would refuse these in a 0.4 agent."""
    folder = tmp_path / "prescreen"
    shutil.copytree(PRESCREEN, folder)
    manifest = folder / "agent.yaml"
    raw = yaml.safe_load(manifest.read_text())
    assert raw["spec_version"] == "0.2"
    if feature == "mapping":
        mapping_path = folder / raw["inputs"]["fhir"]
        mapping = yaml.safe_load(mapping_path.read_text())
        mapping["fields"]["patient.age"] = {
            "paths": ["Patient.birthDate"],
            "transform": "age_years",
        }
        mapping_path.write_text(yaml.safe_dump(mapping, sort_keys=False))
    else:
        criteria_path = folder / "logic" / "criteria.yaml"
        criteria = yaml.safe_load(criteria_path.read_text())
        criteria["questions"]["t2d_diagnosis"]["checks"].append(
            {"medication": {"names": ["metformin"], "current_within_days": 365, "then": "yes"}}
        )
        criteria_path.write_text(yaml.safe_dump(criteria, sort_keys=False))
    # As 0.2 it is refused; as 0.3 and as 0.4 it loads and maps.
    with pytest.raises(PackValidationError) as refused:
        load_pack(folder)
    assert f"spec 0.3 {feature} features" in "; ".join(refused.value.problems)
    for version in ("0.3", "0.4"):
        manifest.write_text(yaml.safe_dump({**raw, "spec_version": version}, sort_keys=False))
        pack = load_pack(folder)
        assert pack.manifest.spec_version == version
        adapter = FhirAdapter.for_pack(pack)
        if feature == "mapping":
            assert adapter.mapping.fields["patient.age"].paths == ["Patient.birthDate"]


def test_a_0_4_card_uses_the_0_3_card_keys() -> None:
    raw = raw_manifest()
    raw["cds_hooks"] = {
        "hook": "patient-view",
        "title": "Actions Mini",
        "description": "A test fixture.",
        "card": {"summaries": {"none": "Nothing.", "refused": "The input was refused."}},
    }
    assert PackManifest.model_validate(raw).cds_hooks is not None
    with pytest.raises(ValidationError, match=r"spec 0\.3 card features"):
        PackManifest.model_validate({**raw, "spec_version": "0.2", "actions": {}})


def test_no_product_code_asks_for_a_spec_version_by_equality() -> None:
    """A feature of spec X is one of every later version too: each check asks `at_least`.
    The one equality left is on the oldest version (what 0.1 does not have), which no later
    version can break."""
    asked = re.compile(r"spec_version\s*(?:==|!=|\bnot in\b|\bin\b)\s*(.*)")
    found = []
    for path in sorted((ROOT / "src" / "mendari").rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = asked.search(line.split("#")[0])
            if match:
                found.append((str(path.relative_to(ROOT)), number, match.group(1).strip()))
    assert [(name, compared) for name, _, compared in found] == [
        ("src/mendari/core/spec.py", '"0.1":')
    ], found
    assert not hasattr(spec, "SPEC_04")  # the version is asked of `at_least`, by its name


def test_a_0_4_manifest_uses_the_second_signal_keys_and_an_older_one_is_told_to_say_0_4() -> None:
    """`repeat`, `calibration` and the `check` composite are asked for as actions are: by
    `at_least`, so a later version keeps them, and an older manifest is refused naming each."""
    raw = raw_manifest()
    raw["providers"]["reader"]["variants"]["agent"]["repeat"] = 3
    assert PackManifest.model_validate(raw).spec_version == "0.4"
    for version in ("0.2", "0.3"):
        older = {**raw, "spec_version": version, "actions": {}}
        with pytest.raises(ValidationError, match=r"use spec 0\.4 features") as refused:
            PackManifest.model_validate(older)
        assert "providers.reader.variants.agent.repeat" in str(refused.value)
        assert "set spec_version: '0.4'" in str(refused.value)


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
        PackManifest.model_validate(with_action(**action))


@pytest.mark.parametrize(
    "name", ["Send", "send-reply", "1st", "send reply", "", "a" * 65, "send.reply", "_send"]
)
def test_an_actions_name_is_a_key_of_at_most_64_characters(name: str) -> None:
    raw = raw_manifest()
    raw["actions"][name] = {"description": "An action.", "effect": "read"}
    with pytest.raises(ValidationError, match="action name") as refused:
        PackManifest.model_validate(raw)
    assert "a" * 65 not in str(refused.value).split("action name", 1)[1].split("must match")[0]
    raw = raw_manifest()
    raw["actions"]["a" * 64] = {"description": "An action.", "effect": "read"}
    assert "a" * 64 in PackManifest.model_validate(raw).actions


def test_an_agent_declares_at_most_sixteen_actions_of_bounded_size_together() -> None:
    raw = raw_manifest()
    raw["actions"] = {
        f"action_{n}": {"description": "An action.", "effect": "read"} for n in range(MAX_ACTIONS)
    }
    assert len(PackManifest.model_validate(raw).actions) == MAX_ACTIONS
    raw["actions"]["one_more"] = {"description": "An action.", "effect": "read"}
    with pytest.raises(ValidationError, match=f"at most {MAX_ACTIONS} actions"):
        PackManifest.model_validate(raw)
    # Each schema within its own bound, and still too long together as one request sends them.
    values = [f"value-{n:03d}-" + "x" * 180 for n in range(18)]
    big = {**OBJECT, "properties": {"choice": {"type": "string", "enum": values}}}
    assert len(json.dumps(big)) <= MAX_ACTION_SCHEMA_CHARS
    raw["actions"] = {
        f"action_{n}": {"description": "An action.", "effect": "read", "schema": big}
        for n in range(MAX_ACTIONS)
    }
    with pytest.raises(ValidationError, match="characters together"):
        PackManifest.model_validate(raw)


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
    manifest = PackManifest.model_validate(with_schema(ALLOWED))
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
            PackManifest.model_validate(with_schema(schema))


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
        assert PackManifest.model_validate(with_schema(schema)).actions["extra"].json_schema
        return
    assert problems and any(said in problem for problem in problems), problems
    with pytest.raises(ValidationError, match="schema"):
        PackManifest.model_validate(with_schema(schema))


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
        PackManifest.model_validate(with_schema({**OBJECT, "properties": {"a": schema}}))


def test_a_problem_names_where_and_never_a_value() -> None:
    schema = {
        **OBJECT,
        "properties": {"who": {**OBJECT, "properties": {"role": {"type": "SECRET-TYPE"}}}},
    }
    (problem,) = action_schema_problems(schema)
    assert problem.startswith("who.role: ") and "SECRET-TYPE" not in problem
    listed = {**OBJECT, "properties": {"tags": {"type": "array", "items": {"type": "x"}}}}
    assert action_schema_problems(listed)[0].startswith("tags[]: ")


# -- the request ----------------------------------------------------------------------------------


def test_a_request_carries_the_declared_actions_in_order_and_an_older_ones_is_as_it_was() -> None:
    manifest = PackManifest.model_validate(raw_manifest())
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
