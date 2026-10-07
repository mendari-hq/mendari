# SPDX-License-Identifier: Apache-2.0
"""Searches of the audit store, in terms of what the store holds and nothing of FHIR.

`standards/fhir_search.py` turns a FHIR search into a `Query`; `AuditStore.search_decisions`
and `search_events` run it, scoped to the request's organization on every page and count. A
query is an AND of conditions, each an OR of atoms over named fields:

- `OneOf(field, values)`: the field is one of the values (none: nothing matches);
- `Within(field, low, high)`: `low <= field < high` on a stored timestamp (a None side is open),
  or outside that range.

Decision fields: `id`, `outcome`, `priority`, `pack` (`(pack_id, pack_version)` pairs), `state`,
`created` and `updated`. A decision's `state` is `refused` (a BLOCK with no primary confidence:
the input was refused before any provider ran), else `pending` (waiting for review), else
`final`. `updated` is when the last event that changed its result was chained
(`RESULT_EVENTS`).

Event fields: `event` (`(decision_id, seq)` pairs), `decision`, `kind` and `recorded`.
"""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class OneOf:
    field: str
    values: frozenset[Hashable]


@dataclass(frozen=True)
class Within:
    field: str
    low: str | None = None  # inclusive
    high: str | None = None  # exclusive
    outside: bool = False


Atom = OneOf | Within


@dataclass(frozen=True)
class Query:
    """`where`: conditions ANDed, each a tuple of atoms ORed (an empty one matches nothing).
    `after`: the paging key of the last row already returned. `count`: also count every match
    (not only this page's)."""

    where: tuple[tuple[Atom, ...], ...] = ()
    after: tuple[Any, ...] | None = None
    limit: int = 50
    count: bool = False


@dataclass(frozen=True)
class Page:
    """One page of rows; `after` is the key to continue from, when there is more."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    after: tuple[Any, ...] | None = None
    total: int | None = None
