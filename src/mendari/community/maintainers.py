# SPDX-License-Identifier: Apache-2.0
"""`community-agents/MAINTAINERS.yaml`: who maintains each community agent, and the agents
removed.

    agents:
      - id: community/visit-sorter
        maintainers: [octocat]
      # Removed: the entry stays, as a tombstone, and the id is never used again.
      - id: community/old-sorter
        maintainers: [octocat]
        removed: "2026-10-04"
        reason: unmaintained

Approvals, CDS service ids and audit records key on an agent's id, so a removed id never
comes back: a new agent under it would inherit them. The list's key was `packs` until 2026-11;
a file that still has it is refused, saying the key it has now.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from mendari.core.pack import parse_yaml
from mendari.errors import MendariError

FILE = "MAINTAINERS.yaml"
KEY = "agents"  # the list's key
FORMER_KEY = "packs"  # the name it had before: refused, saying so
# A GitHub user name: letters, digits and single dashes, at most 39 characters.
HANDLE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
_KEYS = {"id", "maintainers", "removed", "reason"}


@dataclass(frozen=True)
class Entry:
    id: str
    maintainers: tuple[str, ...]
    removed: str | None = None
    reason: str | None = None

    @property
    def tombstone(self) -> bool:
        return self.removed is not None


def parse(data: bytes, where: str = FILE) -> dict[str, Entry]:
    """The entries of a MAINTAINERS.yaml, by agent id. A file that is not in the format above is
    a `MendariError` saying what to fix."""
    raw = parse_yaml(data, FILE, where)
    if raw is None:
        return {}
    if isinstance(raw, dict) and KEY not in raw and FORMER_KEY in raw:
        raise MendariError(
            f"{where}: the list of entries is `{KEY}` (it was `{FORMER_KEY}`): rename the key"
        )
    if not isinstance(raw, dict) or not isinstance(raw.get(KEY), list):
        raise MendariError(f"{where}: expected `{KEY}`, a list of entries")
    entries: dict[str, Entry] = {}
    for number, item in enumerate(raw[KEY], start=1):
        entry = _entry(item, f"{where}: entry {number}")
        if entry.id in entries:
            raise MendariError(f"{where}: {entry.id} is listed twice")
        entries[entry.id] = entry
    return entries


def _entry(item: Any, where: str) -> Entry:
    if not isinstance(item, dict):
        raise MendariError(f"{where}: expected a mapping with `id` and `maintainers`")
    unknown = sorted(set(item) - _KEYS)
    if unknown:
        raise MendariError(f"{where}: unknown keys {unknown} (allowed: {sorted(_KEYS)})")
    pack_id = item.get("id")
    if not isinstance(pack_id, str) or not pack_id:
        raise MendariError(f"{where}: `id` must be the agent's id, e.g. community/<name>")
    handles = item.get("maintainers")
    if (
        not isinstance(handles, list)
        or not handles
        or not all(isinstance(h, str) and HANDLE.match(h) for h in handles)
    ):
        raise MendariError(
            f"{where} ({pack_id}): `maintainers` must list one or more GitHub user names"
        )
    removed = item.get("removed")
    reason = item.get("reason")
    if removed is not None and not str(removed).strip():
        raise MendariError(f"{where} ({pack_id}): `removed` is the date the agent was removed")
    if reason is not None and not isinstance(reason, str):
        raise MendariError(f"{where} ({pack_id}): `reason` must be text")
    return Entry(
        id=pack_id,
        maintainers=tuple(handles),
        removed=None if removed is None else str(removed),
        reason=reason,
    )
