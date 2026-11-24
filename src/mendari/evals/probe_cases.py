# SPDX-License-Identifier: Apache-2.0
"""The cases `mendari probe` changes, and where their text is.

**Which cases.** A labelled dataset (one of the agent's, or a JSON Lines file), or the agent's
own behaviour tests (`source`). A case is what the agent reads of it: two records with the same
text in the fields the agent reads are one case (`distinct_cases`), probed once. A small probe
takes cases with a critical label and cases without in turn, in an order the seed gives
(`in_turn`), so it reaches both the answers a miss is costly on and the cases a wrong answer
would automate.

**Which text.** The fields the running providers say they read (`read_fields`: an agent's, a
model's or Jev's `state_fields`; the `text_fields` of rules, a trained classifier or a dosing
table), or, when one of them is sent the whole input, every string in it. Of those, a string the
input schema holds to a list of values, a format or a pattern (`enum`, `const`, `format`,
`pattern`) is a code, a date or an id, not text, and is never changed (`text_places`). Fields
the caller names take the place of all that.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from mendari.core import site_cases
from mendari.core.decision import Decision
from mendari.core.pack import Pack
from mendari.core.spec import PackManifest
from mendari.errors import MendariError
from mendari.evals.canonical import canonical_input
from mendari.evals.harness import record_problems, resolve_dataset
from mendari.evals.pack_tests import load_cases
from mendari.evals.probe_transforms import Stream
from mendari.providers import Provider
from mendari.providers.agent import AgentProvider
from mendari.providers.base import select_fields
from mendari.providers.dosing import DosingProvider
from mendari.providers.jev import JevProvider
from mendari.providers.llm import LlmProvider
from mendari.providers.naive_bayes import NaiveBayesProvider
from mendari.providers.rules import RulesProvider

Place = tuple[str | int, ...]  # where a text is in an input: ("message", "body")


@dataclass(frozen=True)
class Record:
    """A case as its source gives it: its id there, its input, the answers known to be right
    (any of the questions, or none), and whether it is a site's own (`core/site_cases.py`)."""

    id: str
    input: Mapping[str, Any]
    labels: Mapping[str, str] = field(default_factory=dict)
    site: bool = False


@dataclass(frozen=True)
class Source:
    """Where the cases come from: a labelled dataset, or the agent's own behaviour tests."""

    kind: Literal["dataset", "tests"]
    name: str
    records: tuple[Record, ...]


@dataclass(frozen=True)
class Case:
    """An original case the probe decides: a record, the hash of what the agent reads of it
    (its identity: the same text given twice is one case), and where its text is."""

    id: str
    source: str
    input: Mapping[str, Any]
    labels: Mapping[str, str]
    key: str
    places: tuple[Place, ...]
    site: bool = False


def source(pack: Pack, dataset: str | None = None, *, tests: bool = False) -> Source:
    """The cases to probe: the dataset named (one of the agent's, or a JSON Lines file), or the
    agent's own behaviour tests (`tests`). With neither: its benchmark dataset, else the first
    dataset it declares, else its tests. A dataset the agent cannot evaluate is refused as an
    evaluation refuses it. The tests the probe wrote itself are never its cases."""
    if tests and dataset is not None:
        raise MendariError("--from-tests and --dataset: choose one")
    declared = pack.manifest.evaluation
    if dataset is None and not tests:
        dataset = declared.benchmark_dataset or next(iter(declared.datasets), None)
    if dataset is not None and not tests:
        found = resolve_dataset(pack, dataset)
        problems = record_problems(pack.manifest, found)
        if problems:
            more = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
            raise MendariError(f"{problems[0]}{more}")
        return Source(
            "dataset",
            found.name,
            tuple(
                Record(
                    shown_id(record["id"]),
                    record["input"],
                    dict(record.get("labels") or {}),
                    bool(site_cases.carried([record])),
                )
                for record in found.records
            ),
        )
    cases = load_cases(pack)
    if not cases:
        raise MendariError(
            f"{pack.ref} has no case to probe: name a labelled dataset with --dataset, or give "
            "the agent behaviour tests"
        )
    return Source(
        "tests",
        str(pack.manifest.tests),
        tuple(
            Record(str(number), case["input"], _test_labels(pack.manifest, case["expect"]))
            for number, case in enumerate(cases, start=1)
        ),
    )


def shown_id(value: Any) -> str:
    """A case's id as a report shows it: the text it is, or its JSON."""
    return value if isinstance(value, str) else canonical_input(value)


def _test_labels(manifest: PackManifest, expect: Mapping[str, Any]) -> dict[str, str]:
    """The answers a behaviour test says are right: its `answers`, and its `recommendation` as
    the primary question's."""
    labels = {
        str(question): str(label) for question, label in (expect.get("answers") or {}).items()
    }
    recommended = expect.get("recommendation")
    if isinstance(recommended, str):
        labels.setdefault(manifest.policy.primary, recommended)
    return labels


def critical(manifest: PackManifest) -> list[tuple[str, str]]:
    """The (question, label) pairs the manifest calls critical."""
    return [(found.question, found.label) for found in manifest.evaluation.critical]


def is_critical(labels: Mapping[str, str], manifest: PackManifest) -> bool:
    return any(labels.get(question) == label for question, label in critical(manifest))


def distinct_cases(
    decision: Decision, cases: Source, fields: Sequence[str] | None
) -> tuple[dict[str, Case], dict[str, Case], int]:
    """The source's cases, one for each text the agent reads: by key (the first record with a
    text stands for the others), by the id of every record a case stands for, and how many
    records the agent refuses as written (there is nothing to compare a variant of one with).
    `fields`: the text fields the caller named, or None for those the agent reads."""
    read = fields if fields is not None else read_fields(decision)
    schema = decision.pack.input_schema
    by_key: dict[str, Case] = {}
    by_id: dict[str, Case] = {}
    refused = 0
    for record in cases.records:
        if decision.validate(record.input):
            refused += 1
            continue
        state = record.input if read is None else select_fields(record.input, list(read))
        key = canonical_input(state)
        places = text_places(record.input, schema, read, named=fields is not None)
        if key not in by_key and places:
            by_key[key] = Case(
                record.id, cases.name, record.input, record.labels, key, tuple(places), record.site
            )
        if key in by_key:
            by_id.setdefault(record.id, by_key[key])
    if not by_key:
        where = ", ".join(read) if read is not None else "its input"
        raise MendariError(
            f"no case has text in {where}: name the input's text fields with --field"
        )
    return by_key, by_id, refused


def in_turn(cases: Sequence[Case], manifest: PackManifest, seed: int) -> list[Case]:
    """Cases in the order a small probe takes them: an order the seed gives (whatever the
    source's), those with a critical label and those without in turn."""
    ordered = sorted(cases, key=lambda case: Stream(seed, "cases", case.key).below(1 << 62))
    flagged = [case for case in ordered if is_critical(case.labels, manifest)]
    others = [case for case in ordered if not is_critical(case.labels, manifest)]
    taken: list[Case] = []
    for number in range(max(len(flagged), len(others))):
        taken += flagged[number : number + 1] + others[number : number + 1]
    return taken


# -- where the text is -----------------------------------------------------------------------------


def leaves(decision: Decision) -> Iterator[Provider]:
    """The providers that answer in this configuration: each slot's, or a composite's members."""
    for provider in decision.providers.values():
        yield from provider.leaves()


def read_fields(decision: Decision) -> tuple[str, ...] | None:
    """The input fields the running providers say they read, or None when one of them is sent
    the whole input, or none names a field."""
    named: list[str] = []
    for leaf in leaves(decision):
        if isinstance(leaf, AgentProvider | LlmProvider | JevProvider):
            if leaf.settings.state_fields is None:
                return None
            named += leaf.settings.state_fields
        elif isinstance(leaf, RulesProvider):
            named += leaf.rules.text_fields
            for rules in leaf.rules.questions.values():
                named += rules.text_fields or []
        elif isinstance(leaf, NaiveBayesProvider):
            named += leaf.text_fields
        elif isinstance(leaf, DosingProvider):
            named += leaf.table.text_fields
    return tuple(dict.fromkeys(named)) or None


def shown(place: Place) -> str:
    """`message.body`, `notes[0].text`."""
    said = ""
    for part in place:
        said += f"[{part}]" if isinstance(part, int) else (f".{part}" if said else part)
    return said


def text_at(value: Any, place: Place) -> str:
    for part in place:
        value = value[part]
    return str(value)


def with_text(value: Any, place: Place, text: str) -> None:
    """Put `text` at `place` in `value` (a copy of an input: it is changed in place)."""
    for part in place[:-1]:
        value = value[part]
    value[place[-1]] = text


# What makes a string a code, a date or an id, and not text: never changed.
_NOT_TEXT = ("enum", "const", "format", "pattern")


def text_places(
    value: Any, schema: Any, fields: Sequence[str] | None, *, named: bool
) -> list[Place]:
    """Where the text of an input is: each string in it (through mappings and lists) that holds
    something, is at or under one of `fields` when there are any, and, unless the fields were
    `named` by the caller, is one the schema leaves free (`_NOT_TEXT`)."""
    found: list[Place] = []
    stack: list[tuple[Any, Place, list[Any]]] = [(value, (), [schema])]
    while stack:
        node, place, schemas = stack.pop()
        if isinstance(node, str):
            read = fields is None or _under(place, fields)
            if node.strip() and read and (named or _free(schemas, schema)):
                found.append(place)
        elif isinstance(node, Mapping):
            children: list[tuple[str | int, Any]] = list(node.items())
            stack.extend(
                (child, (*place, key), _below(schemas, key, schema))
                for key, child in reversed(children)
            )
        elif isinstance(node, list):
            stack.extend(
                (child, (*place, index), _below(schemas, index, schema))
                for index, child in reversed(list(enumerate(node)))
            )
    return found


def _under(place: Place, fields: Sequence[str]) -> bool:
    """Whether a place is one of `fields` (dotted names) or inside one."""
    names = [part for part in place if isinstance(part, str)]
    return any(names[: len(parts)] == parts for parts in (name.split(".") for name in fields))


def _free(schemas: Sequence[Any], root: Any) -> bool:
    applied = (node for schema in schemas for node in _applied(schema, root))
    return not any(key in node for node in applied for key in _NOT_TEXT)


def _applied(schema: Any, root: Any) -> Iterator[Mapping[str, Any]]:
    """A schema and every schema it applies to the same value: what its `$ref` names, and each
    branch of its `allOf`, `anyOf`, `oneOf`, `then` and `else`."""
    stack, seen = [schema], set()
    while stack:
        node = stack.pop()
        if not isinstance(node, Mapping) or id(node) in seen:
            continue
        seen.add(id(node))
        yield node
        reference = node.get("$ref")
        if isinstance(reference, str) and reference.startswith("#"):
            stack.append(_pointed(root, reference[1:]))
        for key in ("allOf", "anyOf", "oneOf"):
            branches = node.get(key)
            stack.extend(branches if isinstance(branches, list) else [])
        stack.extend(node.get(key) for key in ("then", "else"))


def _pointed(root: Any, pointer: str) -> Any:
    """What a JSON pointer inside the schema names, or None."""
    node = root
    for part in pointer.split("/")[1:]:
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return node


def _below(schemas: Sequence[Any], part: str | int, root: Any) -> list[Any]:
    """The schemas of a value's property or item, from the schemas of the value."""
    found: list[Any] = []
    for schema in schemas:
        for node in _applied(schema, root):
            if isinstance(part, str):
                properties = node.get("properties")
                if isinstance(properties, Mapping) and part in properties:
                    found.append(properties[part])
                else:
                    found.append(node.get("additionalProperties"))
            else:
                found.append(node.get("items"))
                listed = node.get("prefixItems")
                if isinstance(listed, list) and part < len(listed):
                    found.append(listed[part])
    return found
