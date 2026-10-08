# SPDX-License-Identifier: Apache-2.0
"""Readiness (`GET /api/health/ready`, LP-21): whether this instance can serve decisions now.

`/api/health` answers anyone `ok` without touching the database, so a liveness probe never
restarts a server over an outage it cannot fix, and it stays green through one. Readiness is the
deep check that status pages, uptime monitors and a Kubernetes readinessProbe call:

- **the database**: one bounded read of the chain's table (`AuditStore.ping`): `ok`, or
  `unreachable` whatever failed (a timeout, a refused connection, a file that is not the
  database);
- **the packs**: what start-up found, never rebuilt: `ok`; `degraded` when a pack did not load,
  build or get its terminology (signed-in `/api/health` lists it under `invalid`); `none` when
  no pack is served.

`503` with `status: "unready"` when the database fails or no pack is served; otherwise `200`,
`ready` or `degraded`. The answer holds fixed codes and the version only: no error text (a
driver's message can name a host or a user), no pack ids, no counts.

An instance runs the check at most once every READY_SECONDS and reuses the answer, a failure
included, so callers never multiply database work; callers that arrive during a check wait for
it. Requests are limited per address on each instance, in memory (`Limiter.hit_here`), so the
limit needs no database either.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from mendari import __version__

# How long an instance reuses an answer, and about the longest its database check waits.
READY_SECONDS = 5.0
READY_TIMEOUT_S = 3.0

Packs = Literal["ok", "degraded", "none"]
ready_logger = logging.getLogger("mendari.ready")


def packs_state(*, built: int, problems: int) -> Packs:
    """What readiness says of the packs, from start-up: `built` packs serve, `problems` are
    what signed-in health lists under `invalid` (a pack that did not load, build or get its
    terminology)."""
    if built == 0:
        return "none"
    return "degraded" if problems else "ok"


@dataclass(frozen=True)
class Answer:
    status_code: int
    body: dict[str, Any]


class Readiness:
    """The cached check; `ping(timeout_s)` raises when the database does not answer."""

    def __init__(self, ping: Callable[[float], None], packs: Packs):
        self._ping = ping
        self._packs = packs
        self._lock = threading.Lock()
        self._last: tuple[float, Answer] | None = None
        self._failing = False

    def answer(self) -> Answer:
        fresh = self._fresh()
        if fresh is not None:
            return fresh
        with self._lock:  # one check at a time; whoever waited for it reads its answer
            fresh = self._fresh()
            if fresh is None:
                fresh = self._check()
                self._last = (time.monotonic(), fresh)
            return fresh

    def _fresh(self) -> Answer | None:
        last = self._last
        if last is not None and time.monotonic() - last[0] < READY_SECONDS:
            return last[1]
        return None

    def _check(self) -> Answer:
        try:
            self._ping(READY_TIMEOUT_S)
        except Exception as error:  # an outage is the answer, never an error of this route
            # The type only: a driver's message can name the host, the user or the database.
            ready_logger.warning(
                "readiness: the database did not answer (%s)", type(error).__name__
            )
            self._failing = True
            database = "unreachable"
        else:
            if self._failing:
                ready_logger.warning("readiness: the database answers again")
            self._failing = False
            database = "ok"
        unready = database != "ok" or self._packs == "none"
        status = "unready" if unready else "degraded" if self._packs == "degraded" else "ready"
        checks = {"database": database, "packs": self._packs}
        return Answer(
            503 if unready else 200, {"status": status, "version": __version__, "checks": checks}
        )
