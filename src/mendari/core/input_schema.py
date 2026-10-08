# SPDX-License-Identifier: Apache-2.0
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

The schema patterns of a pack on the community engine (a pack the deployment does not own: a
community pack, or one from a registry that is not a reference pack of this release,
`Pack.bounded`) run on RE2, like its rules (`core/patterns.py`): its validator's `pattern`,
`patternProperties` and `additionalProperties` (and so `propertyNames`, which checks keys with
the same keywords) search with RE2, and every other pack's validator is jsonschema's own,
unchanged. Its validator also checks the decision's time budget each time it applies a schema
(`patterns.check_time`), so a schema whose keywords apply many times over (`$ref`s that fan out,
a subschema on many items) stops at the budget; it is built without the root's `$schema` (which
may only name 2020-12), so jsonschema never swaps it for its own. And such a pack's schema uses
only keywords whose work stays within the budget, `$ref` only to its own `$defs`
(`community_problems`): jsonschema works some others out on its own, outside any check
(`unevaluatedProperties`, `unevaluatedItems`, `contains`), or compares every pair of items at
once (`uniqueItems`).

Every other pack's schema (a pack the deployment owns: a reference pack, an operator's local
one) has no budget, so its shape is bounded at load instead (`expansion_problems`): it nests at
most `MAX_SCHEMA_NESTING` deep, a chain of `$ref`s runs at most `MAX_REF_CHAIN` long, and one
value of an input is checked against at most `MAX_SCHEMA_APPLICATIONS` schema objects (a `$ref`,
`allOf`, `anyOf` or `oneOf` that reaches one schema twice doubles it, and twenty-eight such steps
made one check apply hundreds of millions). A schema may refer back to itself only as a recursive
list or tree does: once, through `properties` or `items`, so one value meets it once.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any

from jsonschema import Draft202012Validator, ValidationError, _utils, validators
from jsonschema.protocols import Validator
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from mendari.core import patterns
from mendari.core.schema_graph import _components, _graph, _Node

DIALECT = "https://json-schema.org/draft/2020-12/schema"
_REFERENCES = ("$ref", "$dynamicRef", "$recursiveRef")
# The bounds on the input schema of a pack the deployment owns (`expansion_problems`). The shipped
# packs' schemas nest at most 6 deep, chain at most 1 `$ref`, and apply at most 2 schema objects
# to one value (`tests/test_input_schema.py`).
MAX_SCHEMA_NESTING = 32  # objects and lists inside each other, in the document
MAX_REF_CHAIN = 16  # `$ref`s one after another on any path through the schema
MAX_SCHEMA_APPLICATIONS = 1_000  # schema objects applied to one value of an input
_COUNTED = 10**9  # counts stop here: past it, a message says "more than 1,000,000,000"


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
            elif key == "$id" and not isinstance(value, str):
                found.append(f"input schema: {here}: $id is a text")
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


def expansion_problems(schema: Any) -> list[str]:
    """Why the input schema of a pack the deployment owns would multiply the work of checking an
    input, or could not be checked (module docstring); [] if it is within the bounds. The schema
    has passed `problems` and jsonschema's own check. Counted without recursion, over the graph
    of its schema objects (`core/schema_graph.py`), each once: `schema_runs`' count in
    `core/caps.py`, with every schema object counting one where it applies."""
    found = []
    depth = _nesting(schema)
    if depth > MAX_SCHEMA_NESTING:
        found.append(f"input schema: nested {depth} levels deep (at most {MAX_SCHEMA_NESTING})")
    nodes = _graph(schema)
    if not nodes:
        return found
    counted = _expansion(nodes)
    if isinstance(counted, str):
        return [*found, counted]
    applications, chain, start = counted
    if chain > MAX_REF_CHAIN:
        found.append(f"input schema: {start}: a chain of {chain} $refs (at most {MAX_REF_CHAIN})")
    if applications > MAX_SCHEMA_APPLICATIONS:
        shown = f"{applications:,}" if applications <= _COUNTED else "more than 1,000,000,000"
        found.append(
            f"input schema: one value can be checked against {shown} schemas (at most "
            f"{MAX_SCHEMA_APPLICATIONS:,}): a $ref, allOf, anyOf or oneOf that reaches the same "
            "schema more than once multiplies it"
        )
    return found


def _nesting(schema: Any) -> int:
    """How deep objects and lists sit inside each other in the document (an object alone is 1)."""
    deepest = 0
    stack: list[tuple[Any, int]] = [(schema, 1)]
    seen: set[int] = set()  # by identity: a shared node is walked once
    while stack:
        node, depth = stack.pop()
        if not isinstance(node, dict | list) or id(node) in seen:
            continue
        seen.add(id(node))
        deepest = max(deepest, depth)
        children = node.values() if isinstance(node, dict) else node
        stack += [(child, depth + 1) for child in children]
    return deepest


def _descending(node: _Node) -> list[int]:
    """Where a schema object's applicators lead below its value: to a property's or an item's."""
    return [*node.one_property, *node.every_property, *node.one_item, *node.every_item]


def _expansion(nodes: list[_Node]) -> tuple[int, int, str] | str:
    """The most schema objects applied to one value, the longest chain of `$ref`s and where it
    starts; or the problem with a reference that leads back. Each strongly connected component
    after every one it leads to (`_components`), as `schema_runs` counts patterns: at each
    place, every schema applied there (`here`), and on any one value below it (`below`); a
    schema reached twice counts twice.

    A component that leads back to itself is allowed only as a recursive list or tree is: each
    of its schemas leads back into it exactly once, and the loop passes through a property or
    an item. Then each of them applies at most once to one value (one turn of the loop ends a
    level down), and on a value below, besides the loop's own schemas, each schema it leads out
    to counts once for each level its schemas reach down (`reach`). A loop that leads to another
    loop is refused: one value would meet the inner one once for each level of the outer."""
    count = len(nodes)
    here, below, chain = [0] * count, [0] * count, [0] * count
    reach: list[int | None] = [0] * count  # how many levels down it applies; None: no end
    looped = [False] * count

    def total(j: int) -> int:
        return max(here[j], below[j])

    def deepest(node: _Node, outside: set[int]) -> int | None:
        """How many levels down the schemas `node` leads to (but those in `outside`) apply: the
        deepest of theirs, one more below a property or an item; None if one has no end."""
        levels = 0
        for down, targets in ((0, [*node.here, *node.keys]), (1, _descending(node))):
            for j in targets:
                if j in outside:
                    continue
                level = reach[j]
                if level is None:
                    return None
                levels = max(levels, level + down)
        return levels

    for component in _components(nodes):
        first = nodes[component[0]]
        if len(component) == 1 and component[0] not in first.edges():
            (i,) = component
            node = nodes[i]
            here[i] = min(1 + sum(here[j] for j in [*node.here, *node.keys]), _COUNTED + 1)
            on_property = max((total(j) for j in node.one_property), default=0) + sum(
                total(j) for j in node.every_property
            )
            on_item = max((total(j) for j in node.one_item), default=0) + sum(
                total(j) for j in node.every_item
            )
            below[i] = min(
                sum(below[j] for j in node.here) + max(on_property, on_item), _COUNTED + 1
            )
            reach[i] = deepest(node, set())
            chain[i] = max(
                [chain[j] for j in node.edges()] + [1 + chain[j] for j in node.refs], default=0
            )
            continue
        members = set(component)
        where = nodes[min(component)].where
        inward = [[j for j in nodes[i].edges() if j in members] for i in component]
        down = any(j in members for i in component for j in _descending(nodes[i]))
        if any(len(edges) != 1 for edges in inward) or not down:
            return (
                f"input schema: {where}: a $ref leads back to a schema holding it on the same "
                "value, or more than once; a schema may refer back to itself only once, through "
                "properties or items"
            )
        outward = [j for i in component for j in nodes[i].edges() if j not in members]
        if any(reach[j] is None for j in outward):
            return (
                f"input schema: {where}: a schema that refers back to itself leads to another "
                "that does: the deeper the input, the more schemas one value is checked against"
            )
        on_value = len(component)
        under = 0
        for i in component:
            node = nodes[i]
            on_value += sum(here[j] for j in [*node.here, *node.keys] if j not in members)
            on_property = max(
                (total(j) for j in node.one_property if j not in members), default=0
            ) + sum(total(j) for j in node.every_property if j not in members)
            on_item = max((total(j) for j in node.one_item if j not in members), default=0) + sum(
                total(j) for j in node.every_item if j not in members
            )
            levels = deepest(node, members)
            same = sum(below[j] for j in node.here if j not in members)
            under += (levels or 0) * (same + max(on_property, on_item))
        refs_inside = sum(1 for i in component for j in nodes[i].refs if j in members)
        leaving = max(
            [chain[j] for j in outward]
            + [1 + chain[j] for i in component for j in nodes[i].refs if j not in members],
            default=0,
        )
        for i in component:
            here[i] = min(on_value, _COUNTED + 1)
            below[i] = min(on_value + under, _COUNTED + 1)
            reach[i] = None
            chain[i] = refs_inside + leaving
            looped[i] = True
    return max(here[0], below[0]), chain[0], _chain_start(nodes, chain, looped)


def _chain_start(nodes: list[_Node], chain: list[int], looped: list[bool]) -> str:
    """Where the longest chain of `$ref`s from the root takes its first `$ref` (or enters a loop
    of references): the schema object whose `$ref` begins it."""
    v = 0
    while chain[v] and not looped[v]:
        node = nodes[v]
        if any(1 + chain[j] == chain[v] for j in node.refs):
            break
        v = next(j for j in node.edges() if chain[j] == chain[v])
    return nodes[v].where


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
MAX_SCHEMA_TEXT = 200  # characters of a `required` name or an `enum` or `const` text
_DEFS_REF = "#/$defs/"
# A `$defs` name a `$ref` can name exactly: a JSON pointer splits on `/`, escapes with `~` and
# is read as a URL (`%2F` is `/`), so any of those would let `#/$defs/<name>` lead elsewhere.
_DEF_NAME = re.compile(r"[A-Za-z0-9_.-]+")
# What to write instead of a keyword a community pack's schema may not use.
_INSTEAD = {
    "unevaluatedProperties": "use additionalProperties",
    "unevaluatedItems": "use items",
    "uniqueItems": "accept repeated items, or check them in a rule",
    "contains": "check it in a rule",
    "minContains": "check it in a rule",
    "maxContains": "check it in a rule",
    "dependentRequired": "use if/then",
    "dependentSchemas": "use if/then",
    "dependencies": "use if/then",
    "$anchor": "use $defs and '#/$defs/<name>'",
    "$dynamicRef": "use $ref to one of the root's $defs",
    "$recursiveRef": "use $ref to one of the root's $defs",
    "definitions": "use $defs",
    "additionalItems": "use items",
    "example": "use examples",
    "nullable": 'use a type list with "null" in it',
}
# Why: jsonschema works these out on its own, outside the time budget, or compares at once.
_UNBOUNDED = frozenset(
    {
        "unevaluatedProperties",
        "unevaluatedItems",
        "uniqueItems",
        "contains",
        "minContains",
        "maxContains",
        "dependentRequired",
        "dependentSchemas",
        "dependencies",
    }
)


def community_problems(schema: Any) -> list[str]:
    """Why the input schema of a pack on the community engine (`Pack.bounded`: a community pack,
    or one from a registry) cannot run as such a pack's must; [] if it can.
    It uses only the keywords above (and `x-...` annotations): jsonschema's others either walk
    the schema on their own, outside the time budget (`unevaluatedProperties`,
    `unevaluatedItems`, `contains`), compare every pair of items at once (`uniqueItems`), or
    reach further than this check can follow (`$dynamicRef`, `dependentSchemas`, ...). A `$ref`
    names a schema of `$defs` (`#/$defs/<name>`), so it never makes data or the root a schema;
    `required` lists at most `MAX_REQUIRED` names and `enum` at most `MAX_ENUM` values, none of
    them an object or a list; `const` is not one either; those texts are at most
    `MAX_SCHEMA_TEXT` characters, and a `$defs` name is letters, digits, `_`, `.` and `-`, so
    `#/$defs/<name>` names exactly one. Walked without recursion, over schema positions only."""
    found: list[str] = []
    defs = schema.get("$defs") if isinstance(schema, dict) else None
    names = set(defs) if isinstance(defs, dict) else set()
    found += [
        f"input schema: $defs/{name}: a $defs name is letters, digits, '_', '.' or '-' (a $ref "
        "to it then names it exactly)"
        for name in sorted(names, key=str)
        if not _DEF_NAME.fullmatch(str(name))
    ]
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
    """Why the schema of a pack on the community engine may not hold `key: value` at `path`, or
    None."""
    if key.startswith("x-"):
        return None  # an annotation of the pack's own
    if key not in _DATA | _SCHEMA | _SCHEMA_MAPS | _SCHEMA_LISTS:
        instead = _INSTEAD.get(key, "an annotation of your own is x-<name>")
        why = (
            "a keyword whose work Mendari cannot keep within a decision's time budget"
            if key in _UNBOUNDED
            else "not one of the keywords the guide lists"
        )
        return f"a community or registry pack's schema may not use {key}, {why}: {instead}"
    if key == "$defs" and path:
        return "only the root may hold $defs"
    if key == "$ref" and not (
        isinstance(value, str) and value.startswith(_DEFS_REF) and value[len(_DEFS_REF) :] in defs
    ):
        return f"a $ref names one of the root's $defs ('{_DEFS_REF}<name>'), not {value!r}"
    if key == "required" and isinstance(value, list):
        if len(value) > MAX_REQUIRED:
            return f"lists {len(value):,} names, at most {MAX_REQUIRED}: split it with allOf"
        if any(len(str(name)) > MAX_SCHEMA_TEXT for name in value):
            return f"names a field longer than {MAX_SCHEMA_TEXT} characters"
    if key == "enum" and isinstance(value, list):
        if len(value) > MAX_ENUM:
            return f"lists {len(value):,} values, at most {MAX_ENUM}: split it with anyOf"
        if any(isinstance(item, dict | list) for item in value):
            return "lists an object or a list: its values are texts, numbers, true, false or null"
        if any(isinstance(item, str) and len(item) > MAX_SCHEMA_TEXT for item in value):
            return f"lists a text longer than {MAX_SCHEMA_TEXT} characters"
    if key == "const":
        if isinstance(value, dict | list):
            return "is an object or a list: a const is a text, a number, true, false or null"
        if isinstance(value, str) and len(value) > MAX_SCHEMA_TEXT:
            return f"is a text longer than {MAX_SCHEMA_TEXT} characters"
    return None


def validator(
    schema: dict[str, Any], *, formats: bool = True, community: bool = False
) -> Validator:
    """A Draft 2020-12 validator for a pack's input schema (one `problems` passed) whose
    registry retrieves nothing; `formats` checks `format` too. With `community` (a pack on the
    community engine, `Pack.bounded`), it runs its patterns on RE2 (module docstring)."""
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


def _enum(
    validator: Any, enums: Any, instance: Any, schema: Mapping[str, Any]
) -> Iterator[ValidationError]:
    """jsonschema's `enum`, with a short message: its own writes out the instance and every
    allowed value for each failing value, which inside `anyOf` on many items held hundreds of
    megabytes (reasons are worded from the keyword alone, `decision._schema_rule`)."""
    if all(not _utils.equal(each, instance) for each in enums):
        yield ValidationError("is not one of the values the schema allows")


def _const(
    validator: Any, const: Any, instance: Any, schema: Mapping[str, Any]
) -> Iterator[ValidationError]:
    if not _utils.equal(instance, const):
        yield ValidationError("is not the value the schema requires")


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
        "enum": _enum,
        "const": _const,
    },
    type_checker=Draft202012Validator.TYPE_CHECKER,
    format_checker=Draft202012Validator.FORMAT_CHECKER,
    applicable_validators=_applicable,
)
