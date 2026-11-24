# SPDX-License-Identifier: Apache-2.0
"""A running server honours its registries' recalls (LP-18e, ADR-0012).

For each pack it serves that came from a registry, the server reads that registry's list of
recalled versions (`GET /v1/yanks`) in a thread of its own, so no decision waits for it. A
decision for one of the registry's packs starts the read once the last one is older than
`MENDARI_REGISTRY_RECALL_CHECK_S` seconds (300 unless set); that decision, and any made while
the read runs, use the list read before, and nothing reads while no decision is made. A pack the
list recalls (its id, version and digest) stops automating, and its own BLOCK goes to a person,
as before its organization approved it (`core/decision.py`). So does a pack whose registry has
not been read in `MENDARI_REGISTRY_RECALL_STALE_S` seconds (86400 unless set), one whose
registry was never read since the server started, and one whose verified marker names no
registry (pulled before LP-18d: pull it again): what cannot be checked is treated as recalled,
the safe direction.

The same read asks whether the registry publishes a signing key (`GET /v1/registry-key`, 200 or
404). Once it does, a pack whose marker does not record `accepted: true` (pulled while the
registry counter-signed nothing, or before LP-19i) stops automating as a recalled one does, until
`mendari pull` fetches it again with the registry's counter-signature (LP-19i, ADR-0015); the
copy in the cache is never removed. A registry with no key changes nothing, and a read that fails
keeps what was known, as for recalls.

A registry is read over https only (plain http to this machine alone), with a short timeout,
and its answer is read as data: a recall it lists is honoured whether or not its signature can
be checked here, since a recall only lowers automation.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from mendari.core.https_only import is_https_or_local
from mendari.core.pack import Pack
from mendari.errors import MendariError

CHECK_ENV = "MENDARI_REGISTRY_RECALL_CHECK_S"
STALE_ENV = "MENDARI_REGISTRY_RECALL_STALE_S"
DEFAULT_CHECK_S = 300.0
DEFAULT_STALE_S = 86_400.0
TIMEOUT_S = 5.0  # each connect and read
DEADLINE_S = 20.0  # the whole answer
# A read started this long ago is taken as lost (a thread that never returned): another starts.
ABANDONED_S = 60.0
MAX_YANKS_BYTES = 5_000_000
SEVERITIES = ("low", "medium", "high", "critical")

Fetch = Callable[[str], list[dict[str, Any]]]
FetchKey = Callable[[str], bool]
UNSIGNED = (
    "pulled before its registry counter-signed the versions it lists: pull it again so the "
    "registry's counter-signature is checked"
)


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


log = logging.getLogger("mendari.recalls")


def fetch_yanks(registry: str, *, deadline_s: float = DEADLINE_S) -> list[dict[str, Any]]:
    """A registry's recalled versions (`GET /v1/yanks`), https only (http to this machine): the
    whole read within `deadline_s` and at most `MAX_YANKS_BYTES`, however the registry sends it."""
    if not is_https_or_local(registry):
        raise MendariError("a registry is read over https")
    started = time.monotonic()
    body = bytearray()
    with (
        httpx.Client(timeout=TIMEOUT_S, follow_redirects=False) as http,
        http.stream("GET", f"{registry.rstrip('/')}/v1/yanks") as response,
    ):
        if response.status_code != 200:
            raise MendariError(f"the registry answered HTTP {response.status_code}")
        declared = response.headers.get("content-length", "")
        if declared.isascii() and declared.isdigit() and int(declared) > MAX_YANKS_BYTES:
            raise MendariError("the registry's recalls are too large")
        for chunk in response.iter_bytes():
            body += chunk
            if len(body) > MAX_YANKS_BYTES:
                raise MendariError("the registry's recalls are too large")
            if time.monotonic() - started > deadline_s:
                raise MendariError("the registry's recalls took too long")
    found = json.loads(bytes(body))
    if not isinstance(found, list):
        raise MendariError("the registry's recalls are not a list")
    return [item for item in found if isinstance(item, dict)]


def fetch_signs(registry: str) -> bool:
    """Whether a registry publishes its signing key (`GET /v1/registry-key`: 200 yes, 404 no),
    https only (http to this machine). The key itself is not read: `mendari pull` checks it."""
    if not is_https_or_local(registry):
        raise MendariError("a registry is read over https")
    with (
        httpx.Client(timeout=TIMEOUT_S, follow_redirects=False) as http,
        http.stream("GET", f"{registry.rstrip('/')}/v1/registry-key") as response,
    ):
        if response.status_code in (200, 404):
            return response.status_code == 200
        raise MendariError(f"the registry answered HTTP {response.status_code}")


@dataclass
class _Registry:
    checked_at: float | None = None  # the last read that worked (monotonic)
    recalled: dict[tuple[str, str], str] = field(default_factory=dict)  # (pack, version) -> why
    reading_since: float | None = None  # a read in flight, since
    failed_at: float | None = None  # the last failed read, for backoff on outage
    failing: str | None = None  # the last read's error type, while reads fail
    signs: bool = False  # it published a signing key at the last read that worked


class RecallWatch:
    """The recalls of the registries the served packs came from, read in the background."""

    def __init__(
        self,
        packs: Iterable[Pack],
        *,
        fetch: Fetch | None = None,
        fetch_key: FetchKey | None = None,
        clock: Callable[[], float] = time.monotonic,
        check_s: float | None = None,
        stale_s: float | None = None,
        background: bool = True,
    ):
        self.check_s = check_s if check_s is not None else _seconds(CHECK_ENV, DEFAULT_CHECK_S)
        self.stale_s = stale_s if stale_s is not None else _seconds(STALE_ENV, DEFAULT_STALE_S)
        if self.stale_s < 2 * self.check_s:
            raise MendariError(
                f"{STALE_ENV} is at least twice {CHECK_ENV}: otherwise every agent from a registry "
                "is held between two reads"
            )
        self._fetch, self._fetch_key = fetch, fetch_key
        self._clock, self._background = clock, background
        self._lock = threading.Lock()
        self.registries: dict[str, _Registry] = {
            str(url): _Registry()
            for pack in packs
            if pack.origin == "hub" and isinstance(url := pack.pulled.get("registry"), str)
        }

    def watch(self, registry: str) -> None:
        """Watch a registry this server did not know when it started: one a version an
        organization added from the console came from (`core/pack_store.py`), read from then on
        as the others are. What it recalls is unknown until its first read: until then every
        agent from it is held."""
        with self._lock:
            self.registries.setdefault(registry, _Registry())

    def refresh(self, registry: str) -> None:
        """Read one registry's recalls, and whether it has a signing key, now (the background
        thread calls this)."""
        state = self.registries[registry]
        try:
            listed = (self._fetch or fetch_yanks)(registry)
            signs = (self._fetch_key or fetch_signs)(registry)
        except Exception as error:  # unreachable, refused, not JSON: what was known stands
            kind = type(error).__name__
            with self._lock:
                state.reading_since = None
                state.failed_at = self._clock()
                changed, state.failing = state.failing != kind, kind
            if changed:  # once a change of state, never the registry's own words
                log.warning("a registry's recalls could not be read (%s)", kind)
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
            state.recalled, state.checked_at, state.signs = recalled, self._clock(), signs
            state.reading_since, state.failed_at, state.failing = None, None, None

    def _due(self, registry: str) -> None:
        """Start a background read if this registry's is due and none is running (never, for
        a watch made without one: its reads are `refresh`'s)."""
        if not self._background:
            return
        state = self.registries[registry]
        with self._lock:
            now = self._clock()
            reading = state.reading_since is not None and now - state.reading_since < ABANDONED_S
            cooldown = min(self.check_s, 30.0)
            cooling = state.failed_at is not None and now - state.failed_at < cooldown
            fresh = state.checked_at is not None and now - state.checked_at < self.check_s
            if reading or cooling or fresh:
                return
            state.reading_since = now
        try:
            threading.Thread(target=self.refresh, args=(registry,), daemon=True).start()
        except RuntimeError:  # no thread to be had: tried again on a later decision
            with self._lock:
                state.reading_since = None

    def held(self, pack: Pack) -> str | None:
        """Why this pack may not automate for its registry now, or None."""
        if pack.origin != "hub":
            return None
        registry = pack.pulled.get("registry")
        if not isinstance(registry, str):
            return "its registry is not recorded beside it: pull it again"
        if registry not in self.registries:
            self.watch(registry)  # an organization added it since this server started
        self._due(registry)
        state = self.registries[registry]
        with self._lock:
            checked, recalled, signs = state.checked_at, dict(state.recalled), state.signs
        if checked is None:
            return "its registry has not been checked for recalls yet"
        if self._clock() - checked > self.stale_s:
            return "its registry could not be checked for recalls"
        why = recalled.get((pack.id, pack.version))
        if why is not None:
            return f"{why} by its registry"
        if signs and pack.pulled.get("accepted") is not True:
            return UNSIGNED
        return None

    def recalled(self, pack: Pack) -> str | None:
        """Why its registry recalled this served version (`recalled (<severity>)` or `deleted`),
        as the last read listed it, or None: for the operator's banner (`server/system.py`),
        which names a recall and not the other reasons an agent from a registry is held (a
        registry not read yet, a missing counter-signature). Starts a read when one is due, as a
        decision does."""
        if pack.origin != "hub":
            return None
        registry = pack.pulled.get("registry")
        if not isinstance(registry, str) or registry not in self.registries:
            return None
        self._due(registry)
        with self._lock:
            return self.registries[registry].recalled.get((pack.id, pack.version))

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
                    "failing": state.failing,
                    "signs": state.signs,
                }
                for url, state in self.registries.items()
            }
