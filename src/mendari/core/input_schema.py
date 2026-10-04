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
a schema. The validator (`validator`) also has a registry that retrieves nothing, so a reference
can never leave the document even if a check were missed.
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.jsonschema import DRAFT202012

DIALECT = "https://json-schema.org/draft/2020-12/schema"
_REFERENCES = ("$ref", "$dynamicRef", "$recursiveRef")


def problems(schema: Any) -> list[str]:
    """Why a pack's input schema is not self-contained (module docstring); [] if it is."""
    found: list[str] = []
    references: list[tuple[str, str]] = []
    stack: list[tuple[str, Any]] = [("", schema)]
    while stack:
        path, node = stack.pop()
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


def validator(schema: dict[str, Any], *, formats: bool = True) -> Draft202012Validator:
    """A Draft 2020-12 validator for a pack's input schema (one `problems` passed) whose
    registry retrieves nothing; `formats` checks `format` too."""
    checker = Draft202012Validator.FORMAT_CHECKER if formats else None
    return Draft202012Validator(schema, registry=Registry(), format_checker=checker)
