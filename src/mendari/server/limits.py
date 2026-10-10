# SPDX-License-Identifier: Apache-2.0
"""Rate limits on the routes that cost something, shared by every instance (ADR-0005).

Two layers: the edge (Vercel's firewall, or a site's proxy) limits per IP on the routes anyone
can reach signed out; this module limits in the database, so every serverless instance and
replica shares one count. It runs in the guard after the sign-in check and before anything that
costs: no body is read, no provider is called and nothing is chained for a refused request.

**What is limited, and by what key** (the class comes from the route's permission and path):

| Class | Routes | Key | Default |
|---|---|---|---|
| `signin` | `GET /auth/login`, `GET /smart/launch`, a person's own (below) | IP | 300/min, 120 |
| `token` | `POST /auth/token`, before its body is read | IP | 60/min |
| `token_failures` | a refused assertion at `/auth/token` | IP and client | 10/10min |
| `decide` | every decision entry point; a CDS draft order is one | principal | 600/min, burst 100 |
| `guard` | `POST …/guard` (an LLM call) | principal | 60/min |
| `evaluate` | `POST …/evals` | principal | 6/h |
| `export` | the audit FHIR export, the feedback export | principal | 10/h |
| `label` | a labeller's routes (LP-11j; each case read is chained) | principal | 60/min, burst 30 |

Reads are not limited here (the edge covers floods, and a read writes nothing permanent), but for
a labeller's, which chain each case they are shown. An EHR launch's decisions count against its
`ehr` principal, like anyone's.

**A daily cap per principal** (`MENDARI_PRINCIPAL_DAILY_LIMIT`, off unless set, sign-in only:
`DailyCap`) counts the decisions each principal asks for, per local day (one per request that
decides, one per draft order of a CDS Hooks order hook), after these limits admit them; past it,
`429` until midnight.
Per-IP keys are used only where no one has signed in yet, and on a person's own three routes in
memberships mode (`POST /api/auth/organization`, `POST /api/auth/access-requests`,
`POST /api/auth/invitations/accept`), which any account that signs in reaches: a hospital sits
behind one address,
so sign-in allows at least what the edge does (120 a minute per address) and a site behind one
egress address raises it (`signin=600/min:240`). A CDS Hooks order hook decides each draft
order (at most 50, or the `decide` burst if smaller): the guard counts the request as one
decision and the route charges the rest in one weighted take before deciding any.

**Mode** (`MENDARI_RATE_LIMITS`): `on` (refuse with `429` and `Retry-After`), `log` (count and
log, never refuse) or `off`. The default is `on` with sign-in and `off` in open mode, where
every caller is one principal. The first refusal of a bucket, until it has room again, is a
`ratelimit.hit` line in the security log (`server/security_log.py`). Overrides follow the
mode, comma-separated: `MENDARI_RATE_LIMITS=on,decide=1200/min:200,evaluate=20/h`
(`count/[n]unit[:burst]`, unit `s`, `min` or `h`). A bad value fails at startup.

**The algorithm** is GCRA, one statement on one row per bucket (`AuditStore.rate_take`): a
bucket admits `burst` requests at once and then one every `period / count`, with no burst at
window edges, constant storage and an exact `Retry-After`. A weighted take (`cost` requests at
once) is admitted whole or not at all, and never more than the burst. Buckets are named by
class and key, never by a secret: `decide|key:<id>`, `decide|ehr:<hash>`, `signin|ip:<address>`.
"""

from __future__ import annotations

import ipaddress
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Literal, cast

from fastapi import Request

from mendari.core import clock
from mendari.core.audit import AuditStore
from mendari.errors import MendariError
from mendari.server import security_log

Mode = Literal["on", "log", "off"]
_UNITS = {"s": 1, "min": 60, "h": 3600}
_RATE = re.compile(r"^(\d+)/(\d*)(s|min|h)(?::(\d+))?$")
# Every this many takes, an instance purges up to PURGE_BATCH empty buckets.
PURGE_EVERY = 100
PURGE_BATCH = 100


@dataclass(frozen=True)
class Rate:
    """`count` requests per `seconds`, `burst` of them at once (GCRA)."""

    count: int
    seconds: int
    burst: int

    @property
    def interval_ms(self) -> int:
        """The time one request uses up: a bucket refills one request per interval."""
        return max(1, round(self.seconds * 1000 / self.count))

    @property
    def tolerance_ms(self) -> int:
        """How far ahead of now a bucket may run: `burst` requests' worth."""
        return self.interval_ms * self.burst

    def __str__(self) -> str:
        """In the setting's own syntax (`600/min:100`), so health shows what to set."""
        unit, size = next((u, n) for u, n in _UNITS_LARGEST_FIRST if self.seconds % n == 0)
        times = self.seconds // size
        burst = f":{self.burst}" if self.burst != self.count else ""
        return f"{self.count}/{times if times != 1 else ''}{unit}{burst}"


_UNITS_LARGEST_FIRST = (("h", 3600), ("min", 60), ("s", 1))
DEFAULT_RATES: dict[str, Rate] = {
    "signin": Rate(300, 60, 120),
    "token": Rate(60, 60, 60),
    "token_failures": Rate(10, 600, 10),
    "decide": Rate(600, 60, 100),
    "guard": Rate(60, 60, 60),
    "evaluate": Rate(6, 3600, 6),
    "export": Rate(10, 3600, 10),
    "label": Rate(60, 60, 30),
}

_GUARD = re.compile(r"^/api/packs/[^/]+/[^/]+/guard$")
_EXPORTS = re.compile(
    r"^/api/(audit/(administrative/)?export/fhir|packs/[^/]+/[^/]+/feedback/export)$"
)


@dataclass(frozen=True)
class LimitSettings:
    mode: Mode
    rates: dict[str, Rate]


def limit_settings(*, sign_in: bool) -> LimitSettings:
    """From `MENDARI_RATE_LIMITS`; see the module docstring. Raises at startup on a bad value."""
    raw = os.environ.get("MENDARI_RATE_LIMITS", "").strip()
    mode: str = ""
    rates = dict(DEFAULT_RATES)
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        name, sep, value = item.partition("=")
        if not sep:
            if mode or item.lower() not in ("on", "log", "off"):
                raise MendariError(
                    "MENDARI_RATE_LIMITS: give one mode (on, log or off), then overrides "
                    "such as decide=1200/min:200"
                )
            mode = item.lower()
            continue
        name = name.strip()
        if name not in DEFAULT_RATES:
            raise MendariError(
                f"MENDARI_RATE_LIMITS: no limit named {name!r} (one of {', '.join(DEFAULT_RATES)})"
            )
        rates[name] = parse_rate(value.strip(), name)
    if not mode:
        mode = "on" if sign_in else "off"
    return LimitSettings(mode=cast(Mode, mode), rates=rates)


def parse_rate(value: str, name: str) -> Rate:
    """`count/[n]unit[:burst]`, e.g. `600/min:100`, `10/10min`, `6/h`; the burst is the count
    unless given."""
    match = _RATE.match(value)
    if match is None:
        raise MendariError(
            f"MENDARI_RATE_LIMITS: {name}={value!r} must look like 600/min, 10/10min or 600/min:100"
        )
    count, times, unit, burst = match.groups()
    seconds = int(times or 1) * _UNITS[unit]
    rate = Rate(int(count), seconds, int(burst) if burst else int(count))
    if rate.count < 1 or rate.burst < 1 or seconds < 1:
        raise MendariError(f"MENDARI_RATE_LIMITS: {name}={value!r} must allow at least one request")
    return rate


def client_ip(request: Request) -> str:
    """The caller's address. On Vercel, the first `X-Forwarded-For` value, which Vercel sets
    itself (a client's own value is overwritten); elsewhere the connection's address, which
    uvicorn replaces with `X-Forwarded-For` only from the proxies it trusts
    (`mendari serve --forwarded-allow-ips`, `FORWARDED_ALLOW_IPS`). Anything that is not an IP
    address counts as one shared `unknown`, so a header can never name a bucket."""
    address = ""
    if os.environ.get("VERCEL"):
        address = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if not address and request.client is not None:
        address = request.client.host
    try:
        return str(ipaddress.ip_address(address))
    except ValueError:
        return "unknown"


def client_ip_source() -> str:
    """Where `client_ip` reads the address, for `/api/health`: a proxy left unconfigured puts
    every person behind it in one sign-in bucket, and this makes that visible."""
    if os.environ.get("VERCEL"):
        return "x-forwarded-for (set by Vercel)"
    trusted = os.environ.get("FORWARDED_ALLOW_IPS")
    if trusted is None:
        return "the connection (x-forwarded-for trusted from 127.0.0.1 only)"
    if not trusted.strip():
        return "the connection (x-forwarded-for not trusted)"
    return f"x-forwarded-for from {trusted.strip()}, else the connection"


def forwarded_for_used(request: Request) -> bool:
    """Whether this request's address (`client_ip`) came from `X-Forwarded-For`: the header holds
    it, so Vercel or a trusted proxy put it there. For `/api/health`, where a go-live check
    compares the address with the browser's own: a proxy left untrusted shows its own address
    and `false`. (A caller that sends its own address in the header reads `true`, rightly
    about the address.)"""
    seen = client_ip(request)
    for value in ",".join(request.headers.getlist("x-forwarded-for")).split(","):
        try:
            if str(ipaddress.ip_address(value.strip())) == seen:
                return True
        except ValueError:
            continue
    return False


class RateLimited(MendariError):
    """A request over its class's limit: `retry_after` whole seconds (at least 1)."""

    def __init__(self, limit: str, retry_after: int):
        self.limit = limit
        self.retry_after = retry_after
        super().__init__(f"too many requests ({limit}); retry in {retry_after} s")


class DailyCapReached(RateLimited):
    """A principal's decisions reached its daily cap (`DailyCap`): refused until local
    midnight."""

    def __init__(self, limit: int, retry_after: int):
        self.limit = "principal_daily"
        self.retry_after = retry_after
        MendariError.__init__(
            self,
            f"this credential's daily limit of {limit} decisions is reached; it starts over at "
            f"midnight (in {retry_after} s)",
        )


# The entry points that make decisions: each request one, but a CDS Hooks order hook one per
# draft order (counted in its route, `DailyCap.charge`). Not what only needs the permission
# (acknowledging the notice, a PAS submission or a DTR package, built from decisions made).
_DECIDES = re.compile(
    r"^/(api/packs/[^/]+/[^/]+/(decide|guard|fhir/decide)|fhir/\$decide|api/smart/decide)$"
)


class DailyCap:
    """A cap per principal on the decisions it asks for, per local calendar day
    (`MENDARI_PRINCIPAL_DAILY_LIMIT`, ADR-0008), beside its organization's quota: one leaked key,
    or one runaway integration, cannot spend its organization's day. Counted in the store, so
    every instance shares it, after the rate limits admit the request (one they refuse never
    uses the day) and before any provider call or chain write: in the guard for each request
    that decides (`counts`), and in the route for a CDS Hooks request, all its draft orders at
    once (`charge`). Each counts whatever it answers. Refusals are `429` with `Retry-After`
    (until midnight), logged once a day per principal per instance (`quota.reached`, reason
    `principal_decisions`)."""

    def __init__(self, store: AuditStore, limit: int):
        self.store = store
        self.limit = limit
        self._logged: tuple[str, set[str]] = ("", set())  # (day, principals logged that day)
        self._lock = threading.Lock()

    @staticmethod
    def counts(method: str, path: str) -> bool:
        """Whether the guard counts this request (one decision); a CDS Hooks request is counted
        in its route, which knows how many it makes."""
        return method == "POST" and _DECIDES.match(path) is not None

    def check(self, principal_id: str, org: str | None, cost: int = 1) -> DailyCapReached | None:
        """Count `cost` decisions, all or none; the refusal if they would pass the cap."""
        if cost <= 0:
            return None
        consumed = self.store.consume_principal(principal_id, daily_limit=self.limit, cost=cost)
        if consumed is not None:
            return None
        day = clock.today().isoformat()
        with self._lock:
            if self._logged[0] != day:
                self._logged = (day, set())
            first = principal_id not in self._logged[1]
            self._logged[1].add(principal_id)
        if first:
            security_log.event(
                "quota.reached",
                outcome="limited",
                reason="principal_decisions",
                actor=principal_id,
                org=org,
                count=self.limit,
            )
        return DailyCapReached(self.limit, clock.seconds_until_tomorrow())


class Limiter:
    """Counts requests per class and key in the store; see the module docstring."""

    def __init__(self, store: AuditStore, settings: LimitSettings):
        self.store = store
        self.mode = settings.mode
        self.rates = settings.rates
        self._takes = 0
        self._logged: dict[str, int] = {}  # bucket -> until when a refusal is not logged again
        self._lock = threading.Lock()

    @property
    def on(self) -> bool:
        return self.mode != "off"

    def describe(self, request: Request) -> dict[str, object]:
        """For `/api/health` (signed in): the mode, every class's rate, where the address is
        read, and the address seen on this request, with whether `X-Forwarded-For` gave it."""
        return {
            "mode": self.mode,
            "client_ip_source": client_ip_source(),
            "client_ip": client_ip(request),
            "x_forwarded_for_used": forwarded_for_used(request),
            "limits": {name: str(rate) for name, rate in self.rates.items()},
        }

    def classify(self, method: str, path: str, permission: str) -> str | None:
        """The class a request counts against, or None (not limited here)."""
        if method == "GET" and path in ("/auth/login", "/smart/launch"):
            return "signin"
        if method == "POST" and permission == "person":
            # Memberships mode: choosing an organization, asking to join, accepting an
            # invitation; open to any account that signs in, so counted per address as signing
            # in is.
            return "signin"
        if method == "POST" and path == "/auth/token":
            return "token"
        if method == "POST" and path == "/api/smart/decide":
            return "decide"
        if permission == "decide":
            return "guard" if _GUARD.match(path) else "decide"
        if permission == "evaluate":
            return "evaluate"
        if method == "GET" and _EXPORTS.match(path):
            return "export"
        if permission == "label":  # a labeller's routes: a person's pace, each case read chained
            return "label"
        return None

    def check(self, request: Request, permission: str, principal_id: str) -> RateLimited | None:
        """Count this request against its class; the refusal to answer with, if it is over
        (never in `log` mode). `principal_id` is the guard's principal (`open` in open mode)."""
        if self.mode == "off":
            return None
        kind = self.classify(request.method, request.url.path, permission)
        if kind is None:
            return None
        # Per address before anyone is known; else per principal, an EHR launch's (`ehr:<hash>`)
        # included.
        key = f"ip:{client_ip(request)}" if kind in ("signin", "token") else principal_id
        return self.hit(kind, key)

    def hit(self, kind: str, key: str, cost: int = 1) -> RateLimited | None:
        """Count `cost` requests in bucket `kind|key`, all or none; the refusal if they are over
        the limit (a CDS Hooks order hook charges its draft orders after the first this way)."""
        rate = self.rates[kind]
        bucket = f"{kind}|{key}"
        now = _now_ms()
        wait_ms = self.store.rate_take(bucket, rate.interval_ms, rate.tolerance_ms, now, cost)
        self._maybe_purge(now)
        return None if wait_ms is None else self._refused(kind, bucket, rate, wait_ms, now)

    def blocked(self, kind: str, key: str) -> RateLimited | None:
        """Whether bucket `kind|key` is over its limit now, without counting (failure counts,
        which `hit` charges only when a request fails)."""
        if self.mode == "off":
            return None
        rate = self.rates[kind]
        bucket = f"{kind}|{key}"
        now = _now_ms()
        wait_ms = self.store.rate_peek(bucket, rate.interval_ms, rate.tolerance_ms, now)
        return None if wait_ms is None else self._refused(kind, bucket, rate, wait_ms, now)

    def fail(self, kind: str, key: str) -> None:
        """Charge one failure to `kind|key` (the refusal, if any, comes on the next request)."""
        if self.mode != "off":
            self.hit(kind, key)

    def _refused(
        self, kind: str, bucket: str, rate: Rate, wait_ms: int, now: int
    ) -> RateLimited | None:
        retry_after = max(1, math.ceil(wait_ms / 1000))
        with self._lock:
            first = self._logged.get(bucket, 0) <= now  # once per bucket until it has room again
            if first:
                if len(self._logged) > 10_000:
                    self._logged.clear()
                self._logged[bucket] = now + wait_ms
        if first:
            # The bucket's opaque id, without the address (the line's `ip` has it): `key:<id>`,
            # `user:<hash>`, `client:<id>` (failed assertions), `ehr:<hash>`; none for sign-in.
            key = bucket.partition("|")[2]
            actor = next((part for part in key.split("|") if not part.startswith("ip:")), None)
            security_log.event(
                "ratelimit.hit",
                outcome="limited" if self.mode == "on" else "ok",
                reason=kind if self.mode == "on" else f"{kind}:log_mode",
                actor=actor,
            )
        return RateLimited(f"{kind}: {rate}", retry_after) if self.mode == "on" else None

    def _maybe_purge(self, now: int) -> None:
        with self._lock:
            self._takes += 1
            due = self._takes % PURGE_EVERY == 0
        if due:
            self.store.rate_purge(now, PURGE_BATCH)


def _now_ms() -> int:
    return int(time.time() * 1000)
