# SPDX-License-Identifier: Apache-2.0
"""An input schema as a graph: each schema object of the document, once, and where the
applicators of Draft 2020-12 lead from it (the same value, its keys, one or every property, one
or every item), through `$ref` and every other applicator.

Two checks read it: the caps of a pack on the community engine (`core/caps.py` `schema_runs`:
how many patterns run on one value) and the expansion bounds of every other pack's schema
(`core/input_schema.py` `expansion_problems`: how many schema objects apply to one value, how
long a chain of `$ref`s runs, which references lead back). Nothing here imports RE2. Every
walk is without recursion, so a deep or wide schema is counted, never a `RecursionError`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from referencing import Registry
from referencing.jsonschema import DRAFT202012

Weight = Callable[[str], int]  # what one run of a pattern counts


def _once(pattern: str) -> int:
    return 1


# The applicators of Draft 2020-12, by where the schemas they hold apply.
_REFERENCES = ("$ref", "$dynamicRef", "$recursiveRef")  # here (no $dynamicAnchor is allowed)
_HERE_ONE = ("not", "if", "then", "else")
_HERE_MANY = ("allOf", "anyOf", "oneOf")
# On one property's value: one of `properties` or `additionalProperties`, with every
# `patternProperties` schema and `unevaluatedProperties`.
# On one item: `prefixItems[i]` or `items`, with `contains` and `unevaluatedItems`.


@dataclass
class _Node:
    """One schema object of the document, and where its applicators lead."""

    where: str
    runs: int  # its own patterns, run on the value here or on each of its keys
    here: list[int] = field(default_factory=list)  # schemas applying to the same value
    keys: list[int] = field(default_factory=list)  # `propertyNames`: applied to each key
    one_property: list[int] = field(default_factory=list)  # `properties`, additionalProperties
    every_property: list[int] = field(default_factory=list)
    one_item: list[int] = field(default_factory=list)  # `prefixItems`, `items`
    every_item: list[int] = field(default_factory=list)
    # Those of `here` reached by a reference (`$ref`, ...), each as often as it is named.
    refs: list[int] = field(default_factory=list)
    # It holds `unevaluatedProperties` or `unevaluatedItems`, whose check validates again what
    # every schema below it on the value applies (jsonschema's `find_evaluated_*_by_schema`).
    unevaluated: bool = False

    def edges(self) -> list[int]:
        return [
            *self.here,
            *self.keys,
            *self.one_property,
            *self.every_property,
            *self.one_item,
            *self.every_item,
        ]


def _graph(schema: Any, weight: Weight = _once) -> list[_Node]:
    """The schema objects reachable from the root through applicators, the root first, each
    with its own patterns' runs (weighed by `weight`); a boolean schema (or anything that is
    not an object) counts nothing and is left out."""
    if not isinstance(schema, dict):
        return []
    resolver = Registry().resolver_with_root(DRAFT202012.create_resource(schema))
    index: dict[int, int] = {}
    nodes: list[_Node] = []
    pending: list[tuple[dict[str, Any], str]] = []

    def add(target: Any, where: str) -> list[int]:
        if not isinstance(target, dict):
            return []
        if id(target) not in index:
            index[id(target)] = len(nodes)
            nodes.append(_Node(where=where, runs=0))
            pending.append((target, where))
        return [index[id(target)]]

    def each(value: Any, where: str) -> list[int]:
        if isinstance(value, list):
            return [i for n, item in enumerate(value) for i in add(item, f"{where}/{n}")]
        return []

    def every(value: Any, where: str) -> list[int]:
        if isinstance(value, dict):
            return [i for key, item in value.items() for i in add(item, f"{where}/{key}")]
        return []

    add(schema, "#")
    while pending:
        node, where = pending.pop()
        record = nodes[index[id(node)]]
        properties = node.get("patternProperties")
        keyed = sum(map(weight, properties)) if isinstance(properties, dict) else 0
        searched = keyed if "additionalProperties" in node else 0
        pattern = node.get("pattern")
        own = weight(pattern) if isinstance(pattern, str) else 0
        record.runs = own + keyed + 2 * searched
        record.unevaluated = "unevaluatedProperties" in node or "unevaluatedItems" in node
        for keyword in _REFERENCES:
            reference = node.get(keyword)
            if isinstance(reference, str):
                target = resolver.lookup(reference).contents
                reached = add(target, reference)
                record.here += reached
                record.refs += reached
        for keyword in _HERE_ONE:
            record.here += add(node.get(keyword), f"{where}/{keyword}")
        for keyword in _HERE_MANY:
            record.here += each(node.get(keyword), f"{where}/{keyword}")
        record.here += every(node.get("dependentSchemas"), f"{where}/dependentSchemas")
        record.keys += add(node.get("propertyNames"), f"{where}/propertyNames")
        record.one_property += every(node.get("properties"), f"{where}/properties")
        record.one_property += add(
            node.get("additionalProperties"), f"{where}/additionalProperties"
        )
        record.every_property += every(properties, f"{where}/patternProperties")
        record.every_property += add(
            node.get("unevaluatedProperties"), f"{where}/unevaluatedProperties"
        )
        record.one_item += each(node.get("prefixItems"), f"{where}/prefixItems")
        record.one_item += add(node.get("items"), f"{where}/items")
        record.every_item += add(node.get("contains"), f"{where}/contains")
        record.every_item += add(node.get("unevaluatedItems"), f"{where}/unevaluatedItems")
    return nodes


def _components(nodes: list[_Node]) -> Iterator[list[int]]:
    """The strongly connected components of the schema graph (Tarjan's, without recursion), each
    after every component it leads to."""
    order = [-1] * len(nodes)
    low = [0] * len(nodes)
    on_stack = [False] * len(nodes)
    stack: list[int] = []
    edges = [node.edges() for node in nodes]
    counter = 0
    work: list[tuple[int, int]] = [(0, 0)]
    while work:
        v, start = work.pop()
        if start == 0:
            order[v] = low[v] = counter
            counter += 1
            stack.append(v)
            on_stack[v] = True
        for n in range(start, len(edges[v])):
            w = edges[v][n]
            if order[w] == -1:
                work += [(v, n + 1), (w, 0)]
                break
            if on_stack[w]:
                low[v] = min(low[v], order[w])
        else:
            if low[v] == order[v]:
                component = []
                while True:
                    w = stack.pop()
                    on_stack[w] = False
                    component.append(w)
                    if w == v:
                        break
                yield component
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
