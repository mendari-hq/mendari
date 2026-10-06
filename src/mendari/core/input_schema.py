"""A pack's input schema: self-contained, and checked without reaching outside the pack.

jsonschema resolves a `$ref` it does not find in the document by fetching it: any URL (so the
server calls an address the pack chose, on every input check, with no timeout) or a local file
(`file://`, outside the pack). A nested `$schema`, or a `$dynamicAnchor` reached from a
metaschema, makes it check part of the input against another document than the pack's. So at
load (`check`) an input schema must be self-contained:

- `$ref`, `$dynamicRef` and `$recursiveRef` start with `#` and resolve to a schema in the
  document (an object or a boolean);
- `$schema`, if present, is at the root and names 2020-12; `$id` is only at the root;
- there is no `$dynamicAnchor`;
- there is no `format: "regex"`: jsonschema checks it by compiling the caller's string with
  `re`, which an input can make arbitrarily slow, and no shipped pack uses it.

The whole document is searched, not only keyword positions: a `$ref` can make any object in it
a schema; each object once, by identity. The validator (`validator`) also has a registry that
retrieves nothing, so a reference can never leave the document even if a check were missed.

A community pack's schema patterns run on RE2, like its rules (`core/patterns.py`): its
validator's `pattern`, `patternProperties` and `additionalProperties` (and so `propertyNames`,
which checks keys with the same keywords) search with RE2, and every other pack's validator is
jsonschema's own, unchanged. Its validator also checks the decision's time budget each time it
applies a schema (`patterns.check_time`), so a schema whose keywords apply many times over
(`$ref`s that fan out, a subschema on many items) stops at the budget; it is built without the
root's `$schema` (which may only name 2020-12), so jsonschema never swaps it for its own. And a
community pack's schema uses only keywords whose work stays within the budget, `$ref` only to
its own `$defs` (`community_problems`): jsonschema works some others out on its own, outside any
check (`unevaluatedProperties`, `unevaluatedItems`, `contains`), or compares every pair of items
at once (`uniqueItems`).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from jsonschema import Draft202012Validator, ValidationError, validators
from jsonschema.protocols import Validator
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from mendari.core import patterns

DIALECT = "https://json-schema.org/draft/2020-12/schema"
_REFERENCES = ("$ref", "$dynamicRef", "$recursiveRef")


def problems(schema: Any) -> list[str]:
    """Why a pack's input schema is not self-contained (module docstring); [] if it is."""
    found: list[str] = []
    references: list[tuple[str, str]] = []
    stack: list[tuple[str, Any]] = [("", schema)]
    seen: set[int] = set()  # by identity: a shared node is searched once
    while stack:
        path, node = stack.pop()
        if isinstance(node, dict | list):
            if id(node) in seen:
                continue
            seen.add(id(node))
        if isinstance(node, list):
            stack.extend((f"{path}/{index}", item) for index, item in enumerate(node))
            continue
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            here = f"{path}/{key}" if path else str(key)
            if key == "$dynamicAnchor":
                found.append(f"input schema: {here}: not allowed in a pack input schema")
            elif key in _REFERENCES and isinstance(value, str):
                if value.startswith("#"):
                    references.append((here, value))
                else:
                    found.append(
                        f"input schema: {here}: {value!r} is outside the schema (a pack input "
                        "schema may only refer to its own parts, '#...')"
                    )
            elif key == "$schema" and (path or value not in (DIALECT, f"{DIALECT}#")):
                found.append(f"input schema: {here}: only the root may name a dialect, {DIALECT}")
            elif key == "$id" and path:
                found.append(f"input schema: {here}: only the root may set $id")
            elif key == "format" and value == "regex":
                found.append(
                    f"input schema: {here}: format 'regex' is not allowed in a pack input schema"
                )
            stack.append((here, value))
    if not found and isinstance(schema, dict):
        resolver = Registry().resolver_with_root(DRAFT202012.create_resource(schema))
        for here, reference in references:
            try:
                target = resolver.lookup(reference).contents
            # `referencing` raises more than Unresolvable on an odd pointer (a ValueError for
            # `#/required/abc`, a TypeError through a boolean): every one is the pack's problem.
            except Exception:
                found.append(f"input schema: {here}: {reference!r} is not in the schema")
                continue
            if not isinstance(target, dict | bool):  # `#/required/0`: a string, not a schema
                found.append(f"input schema: {here}: {reference!r} is not a schema")
    return sorted(found)


# A community pack's input schema uses only keywords whose work Mendari bounds
# (`community_problems`): each applies to a value through `descend`, so the time budget is
# checked at every schema applied (`_applicable`), or does a bounded amount of work at once.
# Their values: a schema, a map of names to schemas, a list of schemas, or data.
_SCHEMA = frozenset({"additionalProperties", "items", "propertyNames", "not", "if", "then", "else"})
_SCHEMA_MAPS = frozenset({"properties", "patternProperties", "$defs"})
_SCHEMA_LISTS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_DATA = frozenset(
    {
        "$schema",
        "$id",
        "$ref",
        "$comment",
        "type",
        "required",
        "enum",
        "const",
        "format",
        "pattern",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
        "minProperties",
        "maxProperties",
        "title",
        "description",
        "default",
        "examples",
        "deprecated",
        "readOnly",
        "writeOnly",
    }
)
MAX_REQUIRED = 100  # names one `required` lists: one error each, at once
MAX_ENUM = 500  # values one `enum` lists, each compared with the value at once
_DEFS_REF = "#/$defs/"


def community_problems(schema: Any) -> list[str]:
    """Why a community pack's input schema cannot run as a community pack's must; [] if it can.
    It uses only the keywords above (and `x-...` annotations): jsonschema's others either walk
    the schema on their own, outside the time budget (`unevaluatedProperties`,
    `unevaluatedItems`, `contains`), compare every pair of items at once (`uniqueItems`), or
    reach further than this check can follow (`$dynamicRef`, `dependentSchemas`, ...). A `$ref`
    names a schema of `$defs` (`#/$defs/<name>`), so it never makes data or the root a schema;
    `required` lists at most `MAX_REQUIRED` names and `enum` at most `MAX_ENUM` values, none of
    them an object or a list; `const` is not one either. Walked without recursion, over schema
    positions only."""
    found: list[str] = []
    defs = schema.get("$defs") if isinstance(schema, dict) else None
    names = set(defs) if isinstance(defs, dict) else set()
    stack: list[tuple[str, Any]] = [("", schema)]
    while stack:
        path, node = stack.pop()
        if not isinstance(node, dict):
            continue  # a boolean schema
        for key, value in node.items():
            here = f"{path}/{key}" if path else str(key)
            problem = _keyword_problem(key, value, path, names)
            if problem:
                found.append(f"input schema: {here}: {problem}")
            elif key in _SCHEMA:
                stack.append((here, value))
            elif key in _SCHEMA_MAPS and isinstance(value, dict):
                stack += [(f"{here}/{name}", sub) for name, sub in value.items()]
            elif key in _SCHEMA_LISTS and isinstance(value, list):
                stack += [(f"{here}/{i}", sub) for i, sub in enumerate(value)]
    return sorted(found)


def _keyword_problem(key: str, value: Any, path: str, defs: set[str]) -> str | None:
    """Why a community pack's schema may not hold `key: value` at `path`, or None."""
    if key.startswith("x-"):
        return None  # an annotation of the pack's own
    if key not in _DATA | _SCHEMA | _SCHEMA_MAPS | _SCHEMA_LISTS:
        return (
            f"a community pack's schema may not use {key}: it uses the keywords Mendari can "
            "keep within a decision's time budget (the guide lists them; unevaluatedProperties "
            "is additionalProperties, unevaluatedItems is items, and uniqueItems and contains "
            "are checks for a rule)"
        )
    if key == "$defs" and path:
        return "only the root may hold $defs"
    if key == "$ref" and not (
        isinstance(value, str) and value.startswith(_DEFS_REF) and value[len(_DEFS_REF) :] in defs
    ):
        return f"a $ref names one of the root's $defs ('{_DEFS_REF}<name>'), not {value!r}"
    if key == "required" and isinstance(value, list) and len(value) > MAX_REQUIRED:
        return f"lists {len(value):,} names, at most {MAX_REQUIRED}"
    if key == "enum" and isinstance(value, list):
        if len(value) > MAX_ENUM:
            return f"lists {len(value):,} values, at most {MAX_ENUM}"
        if any(isinstance(item, dict | list) for item in value):
            return "lists an object or a list: its values are texts, numbers, true, false or null"
    if key == "const" and isinstance(value, dict | list):
        return "is an object or a list: a const is a text, a number, true, false or null"
    return None


def validator(
    schema: dict[str, Any], *, formats: bool = True, community: bool = False
) -> Validator:
    """A Draft 2020-12 validator for a pack's input schema (one `problems` passed) whose
    registry retrieves nothing; `formats` checks `format` too. A community pack's runs its
    patterns on RE2 (module docstring)."""
    cls = _Re2Validator if community else Draft202012Validator
    checker = cls.FORMAT_CHECKER if formats else None
    if community and isinstance(schema, dict):  # never swapped for jsonschema's own (above)
        schema = {key: value for key, value in schema.items() if key != "$schema"}
    return cls(schema, registry=Registry(), format_checker=checker)


# -- a community pack's keywords: jsonschema's own, searching with RE2 ---------------------------


def _search(pattern: str, text: str) -> bool:
    return patterns.search(pattern, text, community=True)


def _pattern(
    validator: Any, pattern: str, instance: Any, schema: Mapping[str, Any]
) -> Iterator[ValidationError]:
    if validator.is_type(instance, "string") and not _search(pattern, instance):
        yield ValidationError(f"{instance!r} does not match {pattern!r}")


def _pattern_properties(
    validator: Any, properties: Mapping[str, Any], instance: Any, schema: Mapping[str, Any]
) -> Iterator[ValidationError]:
    if not validator.is_type(instance, "object"):
        return
    for pattern, subschema in properties.items():
        for key, value in instance.items():
            if _search(pattern, key):
                yield from validator.descend(value, subschema, path=key, schema_path=pattern)


def _additional_properties(
    validator: Any, additional: Any, instance: Any, schema: Mapping[str, Any]
) -> Iterator[ValidationError]:
    if not validator.is_type(instance, "object"):
        return
    named = schema.get("properties", {})
    keyed = list(schema.get("patternProperties", {}))
    extras = {
        key
        for key in instance
        if key not in named and not any(_search(pattern, key) for pattern in keyed)
    }
    if validator.is_type(additional, "object"):
        for extra in extras:
            yield from validator.descend(instance[extra], additional, path=extra)
    elif not additional and extras:
        if keyed:
            verb = "does" if len(extras) == 1 else "do"
            joined = ", ".join(repr(each) for each in sorted(extras))
            listed = ", ".join(repr(each) for each in sorted(keyed))
            yield ValidationError(f"{joined} {verb} not match any of the regexes: {listed}")
        else:
            unexpected = sorted(extras, key=str)
            verb = "was" if len(unexpected) == 1 else "were"
            listed = ", ".join(repr(extra) for extra in unexpected)
            yield ValidationError(
                f"Additional properties are not allowed ({listed} {verb} unexpected)"
            )


def _applicable(schema: Any) -> Any:
    """The keywords jsonschema applies for `schema`, once the decision's time budget is checked:
    it asks each time it applies a schema, to an instance or a part of one."""
    patterns.check_time()
    return schema.items()


_Re2Validator = validators.create(
    meta_schema=Draft202012Validator.META_SCHEMA,
    validators={
        **Draft202012Validator.VALIDATORS,
        "pattern": _pattern,
        "patternProperties": _pattern_properties,
        "additionalProperties": _additional_properties,
    },
    type_checker=Draft202012Validator.TYPE_CHECKER,
    format_checker=Draft202012Validator.FORMAT_CHECKER,
    applicable_validators=_applicable,
)
