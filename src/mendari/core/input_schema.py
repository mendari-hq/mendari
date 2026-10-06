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
jsonschema's own, unchanged. jsonschema's `unevaluatedProperties` searches `patternProperties`
with `re` wherever it looks, so a community pack's schema may not hold both
(`community_problems`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
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


def community_problems(schema: Any) -> list[str]:
    """Why a community pack's input schema cannot run as a community pack's must; [] if it can:
    `unevaluatedProperties` with `patternProperties` anywhere in the document (jsonschema would
    run those patterns on `re`, module docstring), or `uniqueItems` (one application compares
    every pair of items, past any time budget)."""
    found: dict[str, list[str]] = {
        "unevaluatedProperties": [],
        "patternProperties": [],
        "uniqueItems": [],
    }
    stack: list[tuple[str, Any]] = [("", schema)]
    seen: set[int] = set()
    while stack:
        path, node = stack.pop()
        if not isinstance(node, dict | list) or id(node) in seen:
            continue
        seen.add(id(node))
        items = node.items() if isinstance(node, dict) else enumerate(node)
        for key, value in items:
            here = f"{path}/{key}" if path else str(key)
            unique_off = key == "uniqueItems" and value is False  # the default: allowed
            if isinstance(node, dict) and key in found and not unique_off:
                found[str(key)].append(here)
            stack.append((here, value))
    unique = [
        f"input schema: {here}: a community pack's schema may not use uniqueItems (it compares "
        "every pair of items at once): accept repeated items, or check them in a rule"
        for here in sorted(found["uniqueItems"])
    ]
    if not (found["unevaluatedProperties"] and found["patternProperties"]):
        return unique
    return unique + [
        f"input schema: {here}: a community pack's schema may not use unevaluatedProperties "
        "with patternProperties (jsonschema would run those patterns on re, not RE2): use "
        "additionalProperties"
        for here in sorted(found["unevaluatedProperties"])
    ]


def validator(
    schema: dict[str, Any], *, formats: bool = True, community: bool = False
) -> Validator:
    """A Draft 2020-12 validator for a pack's input schema (one `problems` passed) whose
    registry retrieves nothing; `formats` checks `format` too. A community pack's runs its
    patterns on RE2 (module docstring)."""
    cls = _Re2Validator if community else Draft202012Validator
    checker = cls.FORMAT_CHECKER if formats else None
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


def _timed(keyword: Callable[..., Iterator[ValidationError] | None]) -> Callable[..., Any]:
    """`keyword`, checking the decision's time budget (`patterns.check_time`) before it runs and
    after each error it yields: a schema that applies its keywords many times over (`$ref`s
    that fan out, an `enum` on many items) stops at the budget like the pack's patterns."""

    def run(*args: Any) -> Iterator[ValidationError]:
        patterns.check_time()
        for error in keyword(*args) or ():
            yield error
            patterns.check_time()

    return run


_Re2Validator = validators.extend(
    Draft202012Validator,
    {
        keyword: _timed(function)
        for keyword, function in {
            **Draft202012Validator.VALIDATORS,
            "pattern": _pattern,
            "patternProperties": _pattern_properties,
            "additionalProperties": _additional_properties,
        }.items()
    },
)
