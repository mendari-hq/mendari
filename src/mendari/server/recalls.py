# SPDX-License-Identifier: Apache-2.0
"""A running server honours its registries' recalls (LP-18e, ADR-0012).

For each pack it serves that came from a registry, the server reads that registry's list of
recalled versions (`GET /v1/yanks`) every `MENDARI_REGISTRY_RECALL_CHECK_S` seconds (300 unless
set), in a thread of its own, so no decision waits for it. A pack the list recalls (its id,
version and digest) stops automating, and its own BLOCK goes to a person, as before its
organization approved it (`core/decision.py`). So does a pack whose registry has not been read
in `MENDARI_REGISTRY_RECALL_STALE_S` seconds (86400 unless set), one whose registry was never
read since the server started, and one whose verified marker names no registry (pulled before
LP-18d: pull it again): what cannot be checked is treated as recalled, the safe direction.

A registry is read over https only (plain http to this machine alone), with a short timeout,
and its answer is read as data: a recall it lists is honoured whether or not its signature can
be checked here, since a recall only lowers automation.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from mendari.core.pack import Pack
from mendari.errors import MendariError

CHECK_ENV = "MENDARI_REGISTRY_RECALL_CHECK_S"
STALE_ENV = "MENDARI_REGISTRY_RECALL_STALE_S"
DEFAULT_CHECK_S = 300.0
DEFAULT_STALE_S = 86_400.0
TIMEOUT_S = 5.0
MAX_YANKS_BYTES = 5_000_000
SEVERITIES = ("low", "medium", "high", "critical")

Fetch = Callable[[str], list[dict[str, Any]]]


def _seconds(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if not 1 <= value <= 7 * 86_400:
        raise MendariError(f"{name} is a number of seconds from 1 to 604800")
    return value


def fetch_yanks(registry: str) -> list[dict[str, Any]]:
    """A registry's recalled versions (`GET /v1/yanks`), https only (http to this machine)."""
    parts = urlsplit(registry)
    local = (parts.hostname or "") in ("localhost", "127.0.0.1", "::1")
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise MendariError("a registry is read over https")
    with httpx.Client(timeout=TIMEOUT_S, follow_redirects=False) as http:
        response = http.get(f"{registry.rstrip('/')}/v1/yanks")
    if response.status_code != 200 or len(response.content) > MAX_YANKS_BYTES:
        raise MendariError(f"the registry answered HTTP {response.status_code}")
    found = response.json()
    if not isinstance(found, list):
        raise MendariError("the registry's recalls are not a list")
    return [item for item in found if isinstance(item, dict)]


@dataclass
class _Registry:
    checked_at: float | None = None  # the last read that worked (monotonic)
    recalled: dict[tuple[str, str], str] = field(default_factory=dict)  # (pack, version) -> why
    reading: bool = False


class RecallWatch:
    """The recalls of the registries the served packs came from, read in the background."""

    def __init__(
        self,
        packs: Iterable[Pack],
        *,
        fetch: Fetch | None = None,
        clock: Callable[[], float] = time.monotonic,
        check_s: float | None = None,
        stale_s: float | None = None,
        background: bool = True,
    ):
        self.check_s = check_s if check_s is not None else _seconds(CHECK_ENV, DEFAULT_CHECK_S)
        self.stale_s = stale_s if stale_s is not None else _seconds(STALE_ENV, DEFAULT_STALE_S)
        self._fetch, self._clock, self._background = fetch, clock, background
        self._lock = threading.Lock()
        self.registries: dict[str, _Registry] = {
            str(url): _Registry()
            for pack in packs
            if pack.origin == "hub" and isinstance(url := pack.pulled.get("registry"), str)
        }

    def refresh(self, registry: str) -> None:
        """Read one registry's recalls now (the background thread calls this)."""
        state = self.registries[registry]
        try:
            listed = (self._fetch or fetch_yanks)(registry)
        except Exception:  # unreachable, refused, not JSON: what was known stands until stale
            with self._lock:
                state.reading = False
            return
        recalled: dict[tuple[str, str], str] = {}
        for item in listed:
            pack, version = item.get("pack"), item.get("version")
            if not isinstance(pack, str) or not isinstance(version, str):
                continue
            yank = item.get("yank")
            stated = yank.get("severity") if isinstance(yank, Mapping) else None
            severity = stated if isinstance(stated, str) and stated in SEVERITIES else "unknown"
            why = "deleted" if item.get("deleted") is not None else f"recalled ({severity})"
            recalled[(pack, version)] = why
        with self._lock:
            state.recalled, state.checked_at, state.reading = recalled, self._clock(), False

    def _due(self, registry: str) -> None:
        """Start a background read if this registry's is due and none is running (never, for
        a watch made without one: its reads are `refresh`'s)."""
        if not self._background:
            return
        state = self.registries[registry]
        with self._lock:
            now = self._clock()
            if state.reading or (
                state.checked_at is not None and now - state.checked_at < self.check_s
            ):
                return
            state.reading = True
        threading.Thread(target=self.refresh, args=(registry,), daemon=True).start()

    def held(self, pack: Pack) -> str | None:
        """Why this pack may not automate for its registry now, or None."""
        if pack.origin != "hub":
            return None
        registry = pack.pulled.get("registry")
        if not isinstance(registry, str) or registry not in self.registries:
            return "its registry is not recorded beside it: pull it again"
        self._due(registry)
        state = self.registries[registry]
        with self._lock:
            checked, recalled = state.checked_at, dict(state.recalled)
        if checked is None:
            return "its registry has not been checked for recalls yet"
        if self._clock() - checked > self.stale_s:
            return "its registry could not be checked for recalls"
        why = recalled.get((pack.id, pack.version))
        return None if why is None else f"{why} by its registry"

    def status(self) -> dict[str, Any]:
        """For health: each registry's last read's age in seconds and how many it recalls."""
        now = self._clock()
        with self._lock:
            return {
                url: {
                    "checked_s_ago": None
                    if state.checked_at is None
                    else round(now - state.checked_at),
                    "recalled": len(state.recalled),
                }
                for url, state in self.registries.items()
            }
