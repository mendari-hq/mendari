"""Caps on what multiplies the work of a community pack's patterns (plan B11).

A community pack's patterns run on RE2 (`core/patterns.py`), which never backtracks: a pattern's
time is the text's length times its compiled size, never more. So the work one decision takes
is bounded once every number that multiplies it is: the text (`origin.long_text`, at most
`MAX_COMMUNITY_TEXT_CHARS`), how many patterns run on it, and how often. A community pack is
refused at load, in words saying what to write instead, when

- a provider's `text_fields` (or a question's own) names a field twice: a field named ten times
  is a text ten times as long;
- its `negation_window` is longer than `MAX_NEGATION_WINDOW` characters, or it has more than
  `MAX_NEGATION_CUES` `negation_cues`: every negated match searches that window for the cues;
- its input schema can run more than `MAX_SCHEMA_RUNS` patterns on one value or key
  (`schema_runs`): `$ref`, `allOf` and the other applicators can reach one pattern many times
  (two `$ref`s to a schema holding two `$ref`s... double it at each step), and a reference cycle
  that reaches a pattern runs it more times the deeper the input;
- its rules (each pattern once for each question it answers and each composite member holding
  it, `rules_runs`) and its schema's runs on one value come to more than `MAX_PACK_PATTERNS`;
- the same runs, each weighed by its pattern's size (`re2_syntax.size`: the characters,
  classes and `.`s it holds, each as often as a count repeats it), come to more than
  `MAX_PACK_PATTERN_SIZE` (`pack_size`). RE2 runs a pattern's copies side by side, so its time
  on a character grows with the size: 500 patterns of `.{0,999}a.{0,999}b` (2,001 each), within
  every other cap, took 148 s on 10,000 characters. The shipped packs' patterns (about 1,200 to
  2,600 each) take milliseconds.

The caps do not make a hard bound alone: on 10,000 characters of emoji or Chinese text, where
RE2's automaton runs out of memory and steps through a pattern's copies one by one, one search
can take about 2.5 s, and a crafted pack within every cap took 5.5 s, or 17.5 s with negated
patterns rescanned at each match; typo tolerance's keyword list, the negation check's work on
each negated match and a schema whose keywords fan out (`$ref`s that double, an `enum` on many
items) add more. So a community pack's patterns and schema checks also share one time budget
per decision, in the CPU time of the decision's thread (`patterns.time_budget`,
`COMMUNITY_TIME_BUDGET_S`): checked before each search, at each match, at each word typo
correction looks at, at each schema applied and after each error a schema check finds, it
stops the pack's rules or its schema check, and the decision abstains, for a person (an input
the schema had already found wrong is refused). One decision runs a community pack's
patterns and schema for at most the budget plus one step under way (about 3.5 s of CPU at
worst).

Every other pack is not capped: its patterns run on `re`, and installing it means trusting its
author (SECURITY.md).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import TypeAdapter, ValidationError
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from mendari.core import re2_syntax
from mendari.core.patterns import MAX_PACK_PATTERNS, RULES_FLAGS
from mendari.core.spec import PackManifest

MAX_NEGATION_WINDOW = 200  # characters; the shipped packs use 30 to 40
MAX_NEGATION_CUES = 50  # the shipped packs have 7 to 23
MAX_SCHEMA_RUNS = 50  # patterns run on one value or key of an input
MAX_PACK_PATTERN_SIZE = 3_000  # the shipped packs' come to about 1,200 to 2,600
_INT = TypeAdapter(int)  # as the providers read a number (`"200"` is 200)
Weight = Callable[[str], int]  # what one run of a pattern counts


def _once(pattern: str) -> int:
    return 1


def rules_size(pattern: str) -> int:
    """A rules pattern's size (`re2_syntax.size`), at least 1: every run reads the text."""
    return max(re2_syntax.size(pattern, RULES_FLAGS), 1)


def schema_size(pattern: str) -> int:
    """An input schema pattern's size, at least 1."""
    return max(re2_syntax.size(pattern), 1)


# -- provider configs ----------------------------------------------------------------------------


def rules_problems(relative: str, rules: Any) -> list[str]:
    """Why a community pack's `rules` config (`relative`, as read) multiplies its work: a text
    field named twice, a longer negation window or more negation cues than the caps."""
    if not isinstance(rules, Mapping):
        return []  # the provider says what is wrong with it
    problems = _repeated(relative, "text_fields", rules.get("text_fields"))
    questions = rules.get("questions")
    if isinstance(questions, Mapping):
        for question, spec in questions.items():
            if isinstance(spec, Mapping):
                where = f"questions.{question}.text_fields"
                problems += _repeated(relative, where, spec.get("text_fields"))
    window = _number(rules.get("negation_window"))
    if window is not None and window > MAX_NEGATION_WINDOW:
        problems.append(
            f"{relative}: negation_window is {window}; a community pack's is at most "
            f"{MAX_NEGATION_WINDOW} characters (the shipped packs use 30 to 40)"
        )
    cues = rules.get("negation_cues")
    if isinstance(cues, list) and len(cues) > MAX_NEGATION_CUES:
        problems.append(
            f"{relative}: {len(cues)} negation_cues; a community pack has at most "
            f"{MAX_NEGATION_CUES} (the shipped packs have 7 to 23)"
        )
    return problems


def dosing_problems(relative: str, table: Any) -> list[str]:
    """Why a community pack's `dosing` table multiplies its work: a text field named twice."""
    if not isinstance(table, Mapping):
        return []
    return _repeated(relative, "text_fields", table.get("text_fields"))


def _repeated(relative: str, where: str, fields: Any) -> list[str]:
    if not isinstance(fields, list):
        return []
    counts = Counter(name for name in fields if isinstance(name, str))
    return [
        f"{relative}: {where} names {name!r} {count} times; name each field once (a field named "
        "twice is read twice)"
        for name, count in counts.items()
        if count > 1
    ]


def _number(value: Any) -> int | None:
    try:
        number: int = _INT.validate_python(value)
    except ValidationError:
        return None  # the provider says what is wrong with it
    return number


def rules_runs(manifest: PackManifest, configs: Mapping[str, Any], weight: Weight = _once) -> int:
    """How many rules patterns one decision runs, at most: for each provider slot, the variant
    that runs the most (the deployment may choose any), each rules config (the variant's, or a
    composite member's) counting the patterns of every question the slot answers, as often as
    they are listed (a YAML alias listed twice runs twice). `configs`: the rules files as read.
    With `weight` (`rules_size`), each run counts what `weight` gives its pattern."""
    total = 0
    for slot in manifest.providers.values():
        most = 0
        for variant in slot.variants.values():
            config = {"type": variant.type, **(variant.model_extra or {})}
            members = config.get("members")
            found: list[Mapping[str, Any]] = [config]
            if isinstance(members, Mapping):
                found += [member for member in members.values() if isinstance(member, Mapping)]
            runs = sum(
                _question_patterns(configs.get(c["config"]), question, weight)
                for c in found
                if c.get("type") == "rules" and isinstance(c.get("config"), str)
                for question in slot.answers
            )
            most = max(most, runs)
        total += most
    return total


def _question_patterns(rules: Any, question: str, weight: Weight) -> int:
    questions = rules.get("questions") if isinstance(rules, Mapping) else None
    spec = questions.get(question) if isinstance(questions, Mapping) else None
    labels = spec.get("labels") if isinstance(spec, Mapping) else None
    if not isinstance(labels, Mapping):
        return 0
    return sum(
        weight(item["pattern"])
        for items in labels.values()
        if isinstance(items, list)
        for item in items
        if isinstance(item, Mapping) and isinstance(item.get("pattern"), str)
    )


def pack_size(manifest: PackManifest, configs: Mapping[str, Any], schema: Any) -> int:
    """The size of the patterns one decision runs on each character, at most: the rules' runs
    and the input schema's on one value, each weighed by its pattern's size. Its patterns must
    have compiled (`load_pack` checks them first)."""
    return rules_runs(manifest, configs, rules_size) + schema_runs(schema, schema_size)


# -- the input schema ----------------------------------------------------------------------------


class SchemaCycle(Exception):
    """A reference cycle in an input schema from which a pattern can be reached."""

    def __init__(self, where: str):
        super().__init__(where)
        self.where = where


def schema_problems(schema: Any, rules_runs: int, rules_size: int = 0) -> list[str]:
    """Why a community pack's input schema multiplies its patterns' work (module docstring), with
    `rules_runs` (`rules_runs`) and their size (`rules_runs` with `rules_size`) counted toward
    the pack's totals."""
    try:
        runs = schema_runs(schema)
    except SchemaCycle as cycle:
        return [
            f"input schema: {cycle.where}: a $ref leads back to a schema holding it, and "
            "patterns run there or below it; a community pack's schema may not refer back to "
            "itself where its patterns run"
        ]
    if runs > MAX_SCHEMA_RUNS:
        shown = f"{runs:,}" if runs < 10**9 else "more than 1,000,000,000"
        tripled = (
            "each patternProperties pattern counts three times where additionalProperties is "
            "also set (on each key, then again as additionalProperties searches them and for "
            "its error message), and "
            if _searched_keys(schema)
            else ""
        )
        return [
            f"input schema: its patterns can run {shown} times on one value; a community pack's "
            f"schema runs at most {MAX_SCHEMA_RUNS} on any value or key ({tripled}a $ref, allOf, "
            "anyOf or oneOf that reaches the same pattern more than once multiplies it)"
        ]
    if rules_runs + runs > MAX_PACK_PATTERNS:
        return [
            f"the rules run {rules_runs} patterns on each input (each pattern once for each "
            "question it answers and each composite member holding it) and the input schema up "
            f"to {runs} on one value: {rules_runs + runs} together; a community pack runs at most "
            f"{MAX_PACK_PATTERNS}"
        ]
    size = schema_runs(schema, schema_size)
    if rules_size + size > MAX_PACK_PATTERN_SIZE:
        return [
            f"its patterns' size comes to {rules_size + size:,} on each input: the rules' "
            f"{rules_size:,} (each pattern's once for each question it answers and each "
            f"composite member holding it) and the input schema's {size:,} on one value or key; a "
            f"community pack's comes to at most {MAX_PACK_PATTERN_SIZE:,}, as RE2's time on each "
            "character grows with it. A pattern's size is the characters, classes and .s it "
            "holds, each as often as a count repeats it (.{0,999} is 999, \\w+ is 1): use "
            "smaller counts, or fewer patterns"
        ]
    return []


def _searched_keys(schema: Any) -> bool:
    """Whether a schema object anywhere sets both `patternProperties` and
    `additionalProperties`, which counts each of those patterns three times."""
    return any(
        isinstance(node, Mapping)
        and isinstance(node.get("patternProperties"), Mapping)
        and bool(node["patternProperties"])
        and "additionalProperties" in node
        for node in _nodes(schema)
    )


def patterns_on_keys(schema: Any) -> bool:
    """Whether an input schema can run patterns on an input's keys: it has `patternProperties`
    (which `additionalProperties` searches too) or `propertyNames` anywhere."""
    return any(
        isinstance(node, Mapping) and ("patternProperties" in node or "propertyNames" in node)
        for node in _nodes(schema)
    )


def _nodes(schema: Any) -> Iterator[Any]:
    """Every object and list in a document, once each, by identity."""
    stack = [schema]
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        if not isinstance(node, dict | list) or id(node) in seen:
            continue
        seen.add(id(node))
        yield node
        stack += node.values() if isinstance(node, dict) else node


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

    def edges(self) -> list[int]:
        return [
            *self.here,
            *self.keys,
            *self.one_property,
            *self.every_property,
            *self.one_item,
            *self.every_item,
        ]


def schema_runs(schema: Any, weight: Weight = _once) -> int:
    """The most patterns an input schema can run on one value or key of an input (with `weight`,
    `schema_size`: the most size, each run counting what `weight` gives its pattern): at each
    place, the patterns of every schema that applies there (`pattern` once; each
    `patternProperties` pattern on each key, again for `additionalProperties`, which searches
    them, and for its error message; a `propertyNames` schema's on each key), through `$ref` and
    every applicator, a schema reached twice counted twice. Counted per schema object, once each
    (memoised), so a schema whose count doubles at each step is counted in steps, not paths.
    Where one value can meet several schemas below one place, the larger is counted; where it
    meets them all (`allOf`, `patternProperties`), their sum. A reference cycle from which a
    pattern can be reached raises `SchemaCycle`: its count would grow with the input's depth.
    The schema has passed `input_schema.problems`: every `$ref` resolves in the document."""
    nodes = _graph(schema, weight)
    if not nodes:
        return 0
    here = [0] * len(nodes)  # on the value (and each key) where the schema applies
    below = [0] * len(nodes)  # on any one value under it
    reaches = [False] * len(nodes)
    for component in _components(nodes):
        cyclic = len(component) > 1 or component[0] in nodes[component[0]].edges()
        if cyclic:
            members = set(component)
            if any(
                nodes[i].runs or any(reaches[j] for j in nodes[i].edges() if j not in members)
                for i in component
            ):
                raise SchemaCycle(nodes[min(component)].where)
            continue  # no pattern below: it counts nothing
        (i,) = component
        node = nodes[i]
        reaches[i] = node.runs > 0 or any(reaches[j] for j in node.edges())
        if not reaches[i]:
            continue

        def total(j: int) -> int:
            return max(here[j], below[j])

        here[i] = node.runs + sum(here[j] for j in node.here) + sum(here[j] for j in node.keys)
        on_property = max((total(j) for j in node.one_property), default=0) + sum(
            total(j) for j in node.every_property
        )
        on_item = max((total(j) for j in node.one_item), default=0) + sum(
            total(j) for j in node.every_item
        )
        below[i] = sum(below[j] for j in node.here) + max(on_property, on_item)
    return max(here[0], below[0])


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
        for keyword in _REFERENCES:
            reference = node.get(keyword)
            if isinstance(reference, str):
                target = resolver.lookup(reference).contents
                record.here += add(target, reference)
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
