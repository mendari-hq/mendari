# SPDX-License-Identifier: Apache-2.0
"""Readiness (`GET /api/health/ready`, LP-21): whether this instance can serve decisions now.

With sign-in on, anonymous `/api/health` answers `ok` without touching the database, so a
liveness probe never restarts a server over an outage it cannot fix; it stays green through
one. Readiness is the deep check that status pages, uptime monitors and a Kubernetes
readinessProbe call:

- **the database**: one read of the chain's table (`AuditStore.ping`), `ok`, or `unreachable`
  whatever failed (a refused connection, a file that is not the database, no answer within
  READY_TIMEOUT_S);
- **the packs**: what start-up found, never rebuilt: `ok`; `degraded` when a pack did not load,
  build or get its terminology (signed-in `/api/health` lists it under `invalid`); `none` when
  no pack is served.

`503` with `status: "unready"` when the database fails or no pack is served; otherwise `200`,
`ready` or `degraded`. The answer holds fixed codes and the version only: no error text (a
driver's message can name a host or a user), no pack ids, no counts.

**Bounded, whoever asks.** An instance checks at most once every READY_SECONDS and keeps the
answer, a failure too. The read runs in a thread of its own, waited for at most
READY_TIMEOUT_S plus a margin: a database that stops answering without closing the connection
(a network black hole) holds that one thread, and no second read starts until it returns.
Callers that arrive while a check runs get the last answer at once (only the very first check
of an instance is waited for), so readiness never holds more than one request thread, and a
flood of it, during an outage or not, can never starve liveness. That is its rate limit: a
per-address `429` would fail a probe that shares its address with other callers (a kubelet
behind SNAT, a load balancer's health checker), so there is none; floods are the edge's, as for
`/api/health`.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from mendari import __version__

# How long an instance reuses an answer, and how long its database read is waited for.
READY_SECONDS = 5.0
READY_TIMEOUT_S = 3.0
_MARGIN_S = 0.5  # beyond the read's own timeout, for the thread to report it

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
    """The cached, bounded check; `ping(timeout_s)` raises when the database does not answer."""

    def __init__(self, ping: Callable[[float], None], packs: Packs):
        self._ping = ping
        self._packs = packs
        self._lock = threading.Lock()  # held by the one caller running a check
        self._last: tuple[float, Answer] | None = None
        self._reading: threading.Thread | None = None  # a read that outlived its wait
        self._failing = False

    def answer(self) -> Answer:
        last = self._last
        if last is not None and time.monotonic() - last[0] < READY_SECONDS:
            return last[1]
        if last is not None:
            if not self._lock.acquire(blocking=False):
                return last[1]  # a check is running: its answer is due within the bound
        elif not self._lock.acquire(timeout=READY_TIMEOUT_S + 2 * _MARGIN_S):
            return self._answer("unreachable")  # the first check outlived its own bound
        try:
            last = self._last
            if last is not None and time.monotonic() - last[0] < READY_SECONDS:
                return last[1]  # checked while this caller waited
            answer = self._answer(self._database())
            self._last = (time.monotonic(), answer)
            return answer
        finally:
            self._lock.release()

    def _database(self) -> str:
        """`ok` or `unreachable`, waiting at most READY_TIMEOUT_S plus a margin."""
        if self._reading is not None and self._reading.is_alive():
            return self._failed("TimeoutError")  # the last read has not returned yet
        failure: list[BaseException] = []

        def read() -> None:
            try:
                self._ping(READY_TIMEOUT_S)
            except Exception as error:  # an outage is the answer, never an error of the route
                failure.append(error)

        thread = threading.Thread(target=read, name="mendari-ready", daemon=True)
        thread.start()
        thread.join(READY_TIMEOUT_S + _MARGIN_S)
        if thread.is_alive():
            self._reading = thread
            return self._failed("TimeoutError")
        if failure:
            return self._failed(type(failure[0]).__name__)
        if self._failing:
            ready_logger.warning("readiness: the database answers again")
        self._failing = False
        return "ok"

    def _failed(self, kind: str) -> str:
        # The type only: a driver's message can name the host, the user or the database.
        ready_logger.warning("readiness: the database did not answer (%s)", kind)
        self._failing = True
        return "unreachable"

    def _answer(self, database: str) -> Answer:
        unready = database != "ok" or self._packs == "none"
        status = "unready" if unready else "degraded" if self._packs == "degraded" else "ready"
        checks = {"database": database, "packs": self._packs}
        body = {"status": status, "version": __version__, "checks": checks}
        return Answer(503 if unready else 200, body)
