# SPDX-License-Identifier: Apache-2.0
"""The structural R4 gate: is this FHIR R4 JSON at all, before any profile is checked?

The rules are HL7's R4 JSON Schema (`fhir_r4_structure.json`, generated and patched by
`scripts/fhir_r4_schema.py`; the patches are listed in its `source`). Every element must be one
the resource or data type defines, of the right JSON type, and every primitive must have its
format; complex elements R4 requires must be present, and codes with a required binding come
from their value set. This is a structural check, not validation: invariants, profiles,
cardinality of primitives and terminology are left to US Core (`us_core.py`) and to the HL7
validator in CI.

The walk uses an explicit stack, never recursion: valid R4 nests extensions deeper than Python's
recursion limit. It supports exactly the keywords the generated file uses (`KEYWORDS`). Three
FHIR JSON rules differ from plain JSON Schema: a null in a primitive's array is padding (for an
item whose only content is in the `_x` sibling), while any other null is invalid; `true` is never
a number; and an integer has no decimal point.

Expressions are FHIRPath from the resource they are in (`Communication.subject`, not
`Bundle.entry[1].resource.subject`), and diagnostics come from a fixed vocabulary that never
quotes a value. An unknown key is the client's text, so it is named only when it looks like an
element name, and an id labels its resource only when it has the id format.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, NamedTuple

from mendari.standards.us_core import R4_BASE, Issue, IssueCode

DATA = Path(__file__).with_name("fhir_r4_structure.json")
MAX_ISSUES = 100
# Not FHIR JSON: a 400 in every validation mode. `required` and `code-invalid` are findings under
# `fhir_validation`, like a profile's.
BLOCKING: frozenset[IssueCode] = frozenset({"structure", "value"})
KEYWORDS = frozenset(
    {
        "$ref",
        "type",
        "pattern",
        "minimum",
        "maximum",
        "enum",
        "const",
        "items",
        "properties",
        "additionalProperties",
        "required",
        "resourceChoice",
    }
)
_REF = "#/definitions/"
_ELEMENT_NAME = re.compile(r"_?[A-Za-z][A-Za-z0-9]{0,63}")
_ID = re.compile(r"[A-Za-z0-9\-.]{1,64}")
_EXPECTED = {
    "string": "a string",
    "object": "an object",
    "array": "an array",
    "boolean": "a boolean",
    "number": "a number",
    "integer": "a number",
}


@dataclass(frozen=True)
class Rules:
    definitions: dict[str, Any]
    resource_types: frozenset[str]
    primitives: tuple[str, ...]  # longest name first: "dateTime" before "time"


@cache
def rules() -> Rules:
    data: dict[str, Any] = json.loads(DATA.read_text(encoding="utf-8"))
    definitions = data["definitions"]
    primitives = [name for name, schema in definitions.items() if "properties" not in schema]
    primitives.remove("ResourceList")
    return Rules(
        definitions,
        frozenset(data["resourceTypes"]),
        tuple(sorted(primitives, key=lambda name: (-len(name), name))),
    )


_B64_REDOS = r"^(\s*([0-9a-zA-Z\+/=]){4}\s*)+$"
_B64_LINEAR = r"^\s*(?:[0-9a-zA-Z+/=]{4}\s*)+\Z"


@cache
def _pattern(text: str) -> re.Pattern[str]:
    if text == _B64_REDOS:
        return re.compile(_B64_LINEAR)
    # Anchor trailing `$` with `\Z` so a trailing newline never satisfies an R4 primitive pattern.
    if text.endswith("$") and not text.endswith(r"\$"):
        text = text[:-1] + r"\Z"
    return re.compile(text)


class _Resource(NamedTuple):
    kind: str  # the resource type, or "Resource" outside any resource
    label: str  # "Patient/p1", "Patient" when the id is missing or malformed, or "resource"


class _Frame(NamedTuple):
    value: Any  # for an unknown element, its key
    schema: Mapping[str, Any] | None  # None: the top level (a resource, a list or a prefetch map)
    path: str  # FHIRPath from the resource
    element: str  # the element's name, for diagnostics (for an unknown one, its parent's type)
    resource: _Resource


_TOP = _Resource("Resource", "resource")
_RESOURCE: Mapping[str, Any] = {"$ref": _REF + "ResourceList"}
_UNKNOWN: Mapping[str, Any] = {}  # by identity: a key the parent's type does not define


def check(document: Any) -> list[Issue]:
    """Structural issues in every resource of a resource, a Bundle, a list or a CDS Hooks
    prefetch map, including Bundle entries, contained resources and `Parameters` resources. At
    most `MAX_ISSUES`, then one issue that counts the rest."""
    walk = _Walk()
    stack = [_Frame(document, None, "", "resource", _TOP)]
    while stack:
        frame = stack.pop()
        schema = frame.schema
        if schema is None:
            children = _top(frame.value)
        elif schema is _UNKNOWN:
            children = walk.unknown(frame)
        else:
            children = walk.visit(frame, schema)
        stack.extend(reversed(children))  # in document order
    return walk.issues()


def blocking(issues: list[Issue]) -> list[Issue]:
    return [issue for issue in issues if issue.code in BLOCKING]


def merge(base: list[Issue], profiled: list[Issue]) -> list[Issue]:
    """Profile issues, then the base R4 issues no profile issue repeats: a profile constrains its
    base, so the same finding on the same element is reported once, by the profile."""
    said = {(issue.resource, issue.expression, issue.code) for issue in profiled}
    return profiled + [i for i in base if (i.resource, i.expression, i.code) not in said]


def _top(value: Any) -> list[_Frame]:
    """A resource is checked; a list or a map without `resourceType` (CDS Hooks prefetch) is
    opened; anything else holds no resource (a prefetch key the EHR could not fill is null)."""
    if isinstance(value, Mapping) and "resourceType" in value:
        return [_Frame(value, _RESOURCE, "", "resource", _TOP)]
    if isinstance(value, Mapping):
        value = list(value.values())
    if not isinstance(value, list):
        return []
    return [_Frame(item, None, "", "resource", _TOP) for item in value]


@dataclass
class _Walk:
    kept: list[Issue] = field(default_factory=list)
    seen: set[Issue] = field(default_factory=set)  # the same resource twice in a prefetch map
    omitted: int = 0
    anchor: Issue | None = None  # the first omitted issue, a blocking one if any

    def issues(self) -> list[Issue]:
        if self.anchor is None:
            return list(self.kept)
        more = f"and {self.omitted} more issue" + ("s" if self.omitted > 1 else "")
        summary = Issue(
            "error",
            self.anchor.code,
            self.anchor.expression,
            more,
            self.anchor.resource,
            self.anchor.profile,
        )
        return [*self.kept, summary]

    def add(self, code: IssueCode, expression: str, diagnostics: str, at: _Resource) -> None:
        issue = Issue("error", code, expression, diagnostics, at.label, R4_BASE + at.kind)
        if issue in self.seen:
            return
        if len(self.kept) < MAX_ISSUES:
            self.kept.append(issue)
            self.seen.add(issue)
            return
        self.omitted += 1
        first_blocking = self.anchor is not None and self.anchor.code in BLOCKING
        if self.anchor is None or (code in BLOCKING and not first_blocking):
            self.anchor = issue

    def visit(self, frame: _Frame, given: Mapping[str, Any]) -> list[_Frame]:
        """Check one value against its schema; return its children to check."""
        value, path, element, resource = frame.value, frame.path, frame.element, frame.resource
        schema, name = _resolve(given)
        if "resourceChoice" in schema:
            return self.resource(frame)
        if value is None:
            self.add("structure", path, f"{element} must be {_expected(schema)}", resource)
            return []
        kind = schema.get("type")
        if kind is not None and not _is(kind, value):
            if kind == "integer" and _is("number", value):
                self.add("value", path, _format(element, name, schema), resource)
            else:
                self.add("structure", path, f"{element} must be {_EXPECTED[kind]}", resource)
            return []
        if "const" in schema and value != schema["const"]:
            self.add("structure", path, f"{element} is not an R4 resource type", resource)
        elif "enum" in schema and value not in schema["enum"]:
            if isinstance(value, str):
                diagnostics = f"{element} is not a code from its required value set"
                self.add("code-invalid", path, diagnostics, resource)
            else:
                self.add("structure", path, f"{element} must be a string", resource)
        if _malformed(schema, value):
            self.add("value", path, _format(element, name, schema), resource)
        if "items" in schema and isinstance(value, list):
            padded = _padded(schema["items"])
            return [
                _Frame(item, schema["items"], f"{path}[{index}]", element, resource)
                for index, item in enumerate(value)
                if item is not None or not padded
            ]
        if "properties" in schema and isinstance(value, Mapping):
            return self.elements(value, schema, name, frame)
        return []

    def resource(self, frame: _Frame) -> list[_Frame]:
        """A resource where the schema allows any (a Bundle entry, `contained`, a parameter):
        checked against its own type, with FHIRPath restarting at it."""
        value, enclosing = frame.value, frame.resource
        where = frame.path or "resourceType"
        if not isinstance(value, Mapping):
            self.add("structure", where, f"{frame.element} must be an object", enclosing)
            return []
        kind = value.get("resourceType")
        if "resourceType" not in value:
            self.add("structure", where, "resourceType is missing", enclosing)
        elif not isinstance(kind, str):
            self.add("structure", where, "resourceType must be a string", enclosing)
        elif kind not in rules().resource_types:
            self.add("structure", where, "resourceType is not an R4 resource type", enclosing)
        else:
            resource_id = value.get("id")
            named = isinstance(resource_id, str) and _ID.fullmatch(resource_id)
            label = f"{kind}/{resource_id}" if named else kind
            return [_Frame(value, {"$ref": _REF + kind}, kind, kind, _Resource(kind, label))]
        return []

    def elements(
        self, value: Mapping[str, Any], schema: Mapping[str, Any], name: str | None, frame: _Frame
    ) -> list[_Frame]:
        path, resource = frame.path, frame.resource
        properties: Mapping[str, Any] = schema["properties"]
        for required in schema.get("required", ()):
            if required not in value:
                self.add("required", f"{path}.{required}", f"{required} is required", resource)
        children = []
        for key, item in value.items():
            if key in properties:
                children.append(_Frame(item, properties[key], f"{path}.{key}", key, resource))
            elif schema.get("additionalProperties") is False:
                children.append(_Frame(key, _UNKNOWN, path, str(name), resource))
        return children

    def unknown(self, frame: _Frame) -> list[_Frame]:
        key = frame.value
        named = isinstance(key, str) and _ELEMENT_NAME.fullmatch(key)
        where = f"{frame.path}.{key}" if named else frame.path
        diagnostics = f"{key if named else 'an element'} is not an element of {frame.element} in R4"
        self.add("structure", where, diagnostics, frame.resource)
        return []


def _resolve(schema: Mapping[str, Any]) -> tuple[Mapping[str, Any], str | None]:
    """The schema a `$ref` chain ends at, and the last definition named on the way."""
    definitions = rules().definitions
    name = None
    while "$ref" in schema:
        name = schema["$ref"].removeprefix(_REF)
        schema = definitions[name]
    return schema, name


def _padded(items: Mapping[str, Any]) -> bool:
    """Arrays of a primitive and of its `_x` sibling (of `Element`, which nothing else is) may
    hold nulls, padding so that the two line up: a value with no extension, an extension with no
    value. Arrays of other objects may not."""
    resolved, name = _resolve(items)
    if name == "Element":
        return True
    return resolved.get("type") != "object" and "resourceChoice" not in resolved


def _is(kind: str, value: Any) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "object":
        return isinstance(value, Mapping)
    if kind == "array":
        return isinstance(value, list)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":  # FHIR integers have no decimal point, so 1.0 is not one
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, int | float) and not isinstance(value, bool)  # number


def _malformed(schema: Mapping[str, Any], value: Any) -> bool:
    if "pattern" in schema and isinstance(value, str):
        return not _pattern(schema["pattern"]).search(value)
    if _is("number", value):
        return bool(value < schema.get("minimum", value) or value > schema.get("maximum", value))
    return False


def _expected(schema: Mapping[str, Any]) -> str:
    """What a null stands in for: codes (an enum) and the narrative's xhtml are strings."""
    return _EXPECTED[schema.get("type", "string")]


def _format(element: str, name: str | None, schema: Mapping[str, Any]) -> str:
    """The primitive's name: the definition referred to, or for a primitive spelled out inline
    (the `value[x]` of an extension, say), the type that ends the element's name."""
    primitives = rules().primitives
    if name not in primitives:
        suffixes = (p for p in primitives if element.endswith(p[0].upper() + p[1:]))
        name = next(suffixes, schema.get("type", "string"))
    return f"{element} does not have the {name} format"
