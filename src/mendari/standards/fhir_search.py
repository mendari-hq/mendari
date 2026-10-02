"""FHIR search over what Mendari stores: a parsed search (`search.py`) as the store's own query
(`core/query.py`), run, and rendered into the resources of one `searchset` page.

| Type | Searches | Order |
|---|---|---|
| Task | decisions (one Task each, ADR-0003) | newest first: `authoredOn`, then id |
| AuditEvent | every event chained on a decision | chain order |
| Provenance | the events that change a decision's result | chain order |
| CodeSystem | the terminology this deployment serves (in memory) | id |

**Rendered and searched agree.** Each parameter reads what the rendered resource shows: Task
`status` the same three-column predicate (`task_fhir.task_status`), `_lastUpdated` the time of the
last event that made a version, AuditEvent `entity` and Provenance `target` the decision (its
Task's URL under the FHIR base, or its `decision-id` identifier with `:identifier`). A test
compares the store's answer with filtering the rendered resources for random searches.

**Tokens** are matched in the system the element's codes carry: Task `status` in
`http://hl7.org/fhir/task-status`, `priority` in `request-priority`, ids in none (`|id` matches
an id). A code in another system matches nothing; it is not an error. **References** match
`Task/{id}`, a bare `{id}`, or `{base}/Task/{id}` under this server's FHIR base; another type or
another server's base matches nothing.

Tenancy is the store's: every page and count is scoped to the request's organization, and so is
every read (`task`, `audit_event`, `provenance`): another organization's resource is None, as an
unknown one is. Reads never write, whatever the PHI mode (`AuditStore.item`, never `get`).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from mendari.core.audit import RESULT_EVENTS, AuditStore
from mendari.core.query import Atom, OneOf, Page, Query, Within
from mendari.standards import audit_fhir, task_fhir
from mendari.standards.audit_fhir import EVENT_SYSTEM
from mendari.standards.fhir import DECISION_ID_SYSTEM, OUTCOME_SYSTEM, PACK_TAG_SYSTEM
from mendari.standards.search import (
    PRIORITY_SYSTEM,
    TASK_STATUS_SYSTEM,
    Clause,
    DateRange,
    Reference,
    Search,
    Token,
    Uri,
    bounds,
)
from mendari.standards.terminology import Terminology

# Task.status as the store's decision states (`core/query.py`): the same predicate.
STATES = {"failed": "refused", "requested": "pending", "completed": "final"}
_NO_SYSTEM = ""  # ids and versions carry no system: `|x` and `x` both name them


@dataclass(frozen=True)
class Found:
    """One page: its resources, the paging key after it (when there is more), and the total
    (when the search asked for one)."""

    resources: list[dict[str, Any]]
    next_key: dict[str, Any] | None
    total: int | None


def find(
    store: AuditStore, search: Search, *, base: str, terminology: Terminology | None = None
) -> Found:
    """Run a search for the request's organization, rendering under the FHIR `base` (absolute,
    as the resources' references and the Bundle's links use it)."""
    base = base.rstrip("/")
    if search.resource == "Task":
        page = store.search_decisions(task_query(search))
        tasks = [task_fhir.current_task(row, base) for row in page.rows]
        return Found(tasks, _key(page, ("t", "id")), page.total)
    if search.resource in ("AuditEvent", "Provenance"):
        page = store.search_events(event_query(search, base))
        resources = _events(store, search.resource, page.rows, base)
        return Found(resources, _key(page, ("seq",)), page.total)
    if search.resource == "CodeSystem":
        return code_systems(search, terminology or Terminology(()))
    raise ValueError(f"{search.resource} is not searchable")


def task_query(search: Search) -> Query:
    cursor = search.cursor
    return _query(
        search,
        [_task(clause) for clause in search.clauses],
        None if cursor is None else (cursor["t"], cursor["id"]),
    )


def event_query(search: Search, base: str) -> Query:
    """AuditEvent: every chained decision event. Provenance: only those that change the result
    (`RESULT_EVENTS`), the activities a Provenance records."""
    provenance = search.resource == "Provenance"
    conditions = [_event(clause, base, provenance) for clause in search.clauses]
    if provenance:
        conditions.append((OneOf("kind", frozenset(RESULT_EVENTS)),))
    cursor = search.cursor
    return _query(search, conditions, None if cursor is None else (cursor["seq"],))


def _query(
    search: Search, conditions: Sequence[tuple[Atom, ...] | None], after: tuple[Any, ...] | None
) -> Query:
    """None conditions match everything (a value such as `system|`), so they drop out."""
    return Query(
        where=tuple(c for c in conditions if c is not None),
        after=after,
        limit=0 if search.summary_count else search.count,
        count=search.counted,
    )


def _task(clause: Clause) -> tuple[Atom, ...] | None:
    name = clause.name
    if name in ("authored-on", "_lastUpdated"):
        return _dates("created" if name == "authored-on" else "updated", clause)
    system, field, codes_to_values = _TASK_TOKENS[name]
    codes = _codes(clause, system)
    return None if codes is None else (OneOf(field, frozenset(codes_to_values(codes))),)


def _packs(codes: frozenset[str]) -> Iterator[tuple[str, str]]:
    for code in codes:
        pack, at, version = code.rpartition("@")
        if at and pack and version:
            yield pack, version


def _same(codes: frozenset[str]) -> frozenset[str]:
    return codes


def _states(codes: frozenset[str]) -> set[str]:
    return {STATES[code] for code in codes if code in STATES}


# Each Task token parameter: the system its codes are in, the decision field, and the field's
# values for a set of codes.
_TASK_TOKENS: dict[str, tuple[str, str, Callable[[frozenset[str]], Any]]] = {
    "_id": (_NO_SYSTEM, "id", _same),
    "identifier": (DECISION_ID_SYSTEM, "id", _same),
    "status": (TASK_STATUS_SYSTEM, "state", _states),
    "business-status": (OUTCOME_SYSTEM, "outcome", _same),
    "priority": (PRIORITY_SYSTEM, "priority", _same),
    "_tag": (PACK_TAG_SYSTEM, "pack", _packs),
}


def _event(clause: Clause, base: str, provenance: bool) -> tuple[Atom, ...] | None:
    name = clause.name
    if name in ("date", "recorded", "_lastUpdated"):
        return _dates("recorded", clause)
    if name == "subtype":
        codes = _codes(clause, EVENT_SYSTEM)
        return None if codes is None else (OneOf("kind", codes),)
    if name == "_id":
        codes = _codes(clause, _NO_SYSTEM)
        if codes is None:
            return None
        keys = (event_key(code, provenance) for code in codes)
        return (OneOf("event", frozenset(key for key in keys if key is not None)),)
    if clause.modifier == "identifier":  # entity / target
        codes = _codes(clause, DECISION_ID_SYSTEM)
        return None if codes is None else (OneOf("decision", codes),)
    decisions = {_decision(value, base) for value in clause.values}
    return (OneOf("decision", frozenset(d for d in decisions if d is not None)),)


def event_key(resource_id: str, provenance: bool) -> tuple[str, int] | None:
    """`(decision id, seq)` from an AuditEvent id, `{decision id}-{seq}`, or a Provenance id,
    `{decision id}-p{seq}`; None when the id has neither form."""
    decision_id, separator, seq = resource_id.rpartition("-p" if provenance else "-")
    # The seq as Mendari writes it: "07" is not the id of event 7 (a read must return the id
    # it was asked for, so a non-canonical one is a 404, as in vread).
    if not (decision_id and separator and re.fullmatch(r"[1-9][0-9]*", seq)):
        return None
    number = int(seq)
    return (decision_id, number) if number < 2**63 else None


def _decision(value: Any, base: str) -> str | None:
    """The decision a reference value names, if it names a Task on this server."""
    assert isinstance(value, Reference)
    if value.type not in (None, "Task"):
        return None
    if value.base is not None and value.base.rstrip("/") != base:
        return None
    return value.id


def _dates(field: str, clause: Clause) -> tuple[Atom, ...]:
    atoms = []
    for value in clause.values:
        assert isinstance(value, DateRange)
        low, high, outside = bounds(value)
        atoms.append(Within(field, low, high, outside))
    return tuple(atoms)


def _codes(clause: Clause, system: str) -> frozenset[str] | None:
    """The codes a token clause asks for, among codes in `system`; None when one value asks for
    every code of it (`system|`). `code` names a code in any system, `|code` one with no system;
    a code of another system names nothing here."""
    codes = set()
    for value in clause.values:
        assert isinstance(value, Token)
        if value.system is not None and value.system != system:
            continue
        if value.code is None:
            return None
        codes.add(value.code)
    return frozenset(codes)


def _key(page: Page, names: tuple[str, ...]) -> dict[str, Any] | None:
    return None if page.after is None else dict(zip(names, page.after, strict=True))


def _events(
    store: AuditStore, resource: str, rows: list[dict[str, Any]], base: str
) -> list[dict[str, Any]]:
    if resource == "AuditEvent":
        return [audit_fhir.audit_event(row, base=base) for row in rows]
    # A Provenance targets the Task version its event made: number them from each decision's
    # whole trail, fetched for the page's decisions at once.
    trails = store.decision_events(sorted({row["decision_id"] for row in rows}))
    versions = {key: audit_fhir.task_versions(events) for key, events in trails.items()}
    return [
        audit_fhir.provenance(row, base=base, version=versions[row["decision_id"]][row["seq"]])
        for row in rows
    ]


# -- reads ----------------------------------------------------------------------------------


def task(store: AuditStore, resource_id: str, *, base: str) -> list[dict[str, Any]] | None:
    """Every version of a decision's Task, version 1 first, or None (unknown, or another
    organization's): `[-1]` is the read, `[n - 1]` the vread of version n."""
    item = store.item(resource_id)
    return None if item is None else task_fhir.task_versions(item, base)


def audit_event(store: AuditStore, resource_id: str, *, base: str) -> dict[str, Any] | None:
    key = event_key(resource_id, provenance=False)
    event = None if key is None else store.event(*key)
    return None if event is None else audit_fhir.audit_event(event, base=base)


def provenance(store: AuditStore, resource_id: str, *, base: str) -> dict[str, Any] | None:
    """Only the events that change a decision's result have a Provenance; it targets the Task
    version its event made, numbered from the decision's whole trail."""
    key = event_key(resource_id, provenance=True)
    if key is None:
        return None
    decision_id, seq = key
    event = store.event(decision_id, seq)
    if event is None or event["event"] not in RESULT_EVENTS:
        return None
    versions = audit_fhir.task_versions(store.decision_events([decision_id])[decision_id])
    return audit_fhir.provenance(event, base=base, version=versions[seq])


# -- CodeSystem, in memory -------------------------------------------------------------------


def code_systems(search: Search, terminology: Terminology) -> Found:
    """The served CodeSystems that match, by id. Terminology is the same for every
    organization (it describes the packs, never a decision)."""
    systems = sorted(
        (r for r in terminology.resources if r["resourceType"] == "CodeSystem"),
        key=lambda r: str(r["id"]),
    )
    matched = [r for r in systems if all(_code_system(r, c) for c in search.clauses)]
    after = None if search.cursor is None else search.cursor["id"]
    rest = [r for r in matched if after is None or r["id"] > after]
    page = [] if search.summary_count else rest[: search.count]
    more = bool(page) and len(rest) > len(page)
    return Found(
        page,
        {"id": page[-1]["id"]} if more else None,
        len(matched) if search.counted else None,
    )


def _code_system(resource: Mapping[str, Any], clause: Clause) -> bool:
    if clause.name == "url":
        return any(_uri(resource, value) for value in clause.values)
    element = {"_id": "id", "version": "version"}[clause.name]
    codes = _codes(clause, _NO_SYSTEM)
    return codes is None or resource.get(element) in codes


def _uri(resource: Mapping[str, Any], value: Any) -> bool:
    assert isinstance(value, Uri)
    return resource["url"] == value.url and value.version in (None, resource["version"])
