# SPDX-License-Identifier: Apache-2.0
"""Browser sessions: how long they last, what their cookies are called, and the cookie that ties
a sign-in (or an EHR launch) to the browser that started it. A person's session (`/auth/login`)
and an EHR launch's (`/smart/launch`) follow the same rules.

- **Idle and absolute expiry, both in the row's `expires_at`.** A session ends after
  `MENDARI_SESSION_IDLE_MINUTES` (30, from 5 to 120) without a request, and
  `MENDARI_SESSION_MAX_HOURS` (8, from 1 to 12) after it began whatever happens; an EHR launch's
  also when the EHR's token expires. A request pushes `expires_at` to now + idle, at most once
  every sixth of the idle time (5 minutes of 30), never past the absolute limit (`absolute_at`,
  kept in the row) and never back: one conditional UPDATE, right across any number of instances
  with nothing in memory. So a session ends between 25 and 30 minutes after its last request:
  early, never late. The cookies have no `Max-Age`, so closing the browser ends them too; the
  server decides when a session is over.
- **Rows from before this release** have no `absolute_at`: their own `expires_at` (8 hours after
  sign-in) is their absolute limit, and the idle limit starts at their first request after the
  deploy. Nobody is signed out by it.
- **Cookie names.** `__Host-mendari_session` and `__Host-mendari_smart`: a browser accepts a
  `__Host-` cookie only from this exact host, Secure, with `Path=/` and no `Domain`, so a sibling
  subdomain cannot set or shadow one. Loopback and dotless hosts (`localhost`, `127.0.0.1`, a
  compose service) keep the plain names, which browsers may refuse the prefix on over plain http.
  For one release the plain name is still read on other hosts, for rows from before it only
  (rows without `v`, which end within 8 hours of the deploy); removing that is on the todo list.
- **Sign-in and launch binding** (RFC 6749 §10.12: login CSRF). `/auth/login` and `/smart/launch`
  set a random `__Host-mendari_signin` (or `__Host-mendari_launch`) cookie for ten minutes and
  keep its SHA-256 in the pending row; the callback compares them in constant time. A callback URL
  sent to someone else's browser then fails there, instead of signing them in as the sender.
  A pending row from before this release has no digest and is accepted, once, as before.
  The cookie is host-only, so both start on `MENDARI_PUBLIC_URL`, where the callback comes back:
  started on another name (a `*.vercel.app` alias, a second DNS name), they first send the
  browser there (`to_public_url`).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import Request, Response
from fastapi.responses import RedirectResponse

from mendari.core.audit import AuditStore
from mendari.errors import MendariError

SESSION = "mendari_session"  # a person's, from /auth/callback
SMART = "mendari_smart"  # an EHR launch's, from /smart/callback
SIGNIN = "mendari_signin"  # ties /auth/login to /auth/callback
LAUNCH = "mendari_launch"  # ties /smart/launch to /smart/callback
HOST_PREFIX = "__Host-"
# Rows made under the `__Host-` names carry `v`: the plain name never reaches them.
VERSION = 2
PENDING_SECONDS = 600  # a binding cookie lasts as long as the pending row it binds
# Marks the one redirect to MENDARI_PUBLIC_URL (`to_public_url`), so a proxy that rewrites the
# Host header (the request then never looks like it is for the public name) cannot loop it.
MOVED = "moved"
_DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True)
class Lifetime:
    """How long a session lasts, in seconds: `idle` without a request, `absolute` at most."""

    idle: int = 30 * 60
    absolute: int = 8 * 3600

    @property
    def touch(self) -> int:
        """The least a request moves the expiry by: a sixth of the idle time (5 minutes of 30),
        so an active session costs at most one write per interval."""
        return self.idle // 6

    def start(self, now: int, *, ends_by: int | None = None) -> tuple[int, int]:
        """A new session's `(expires_at, absolute_at)`; `ends_by` caps the absolute limit (an
        EHR token's expiry)."""
        absolute = now + self.absolute if ends_by is None else min(ends_by, now + self.absolute)
        return min(now + self.idle, absolute), absolute

    def extended(self, now: int, expires_at: int, absolute_at: int) -> int | None:
        """The new expiry a request at `now` earns, or None when it would move by less than
        `touch` (or not at all: never back, never past `absolute_at`)."""
        wanted = min(now + self.idle, absolute_at)
        return wanted if wanted - expires_at >= self.touch else None


def lifetime() -> Lifetime:
    """From the environment, bounded: a bad value fails at startup, never at a sign-in."""
    idle = _bounded("MENDARI_SESSION_IDLE_MINUTES", 30, 5, 120)
    absolute = _bounded("MENDARI_SESSION_MAX_HOURS", 8, 1, 12)
    return Lifetime(idle=idle * 60, absolute=absolute * 3600)


def _bounded(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    if not raw.isdigit() or not low <= int(raw) <= high:
        raise MendariError(f"{name} must be a whole number from {low} to {high}, not {raw!r}")
    return int(raw)


# -- cookie names ---------------------------------------------------------------------------


def local(host: str | None) -> bool:
    """Loopback or dotless (a compose service, say): the plain cookie names are kept there."""
    host = (host or "").lower().rstrip(".")
    loopback = host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost")
    return loopback or "." not in host


def cookie_name(base: str, host: str | None) -> str:
    """The name of cookie `base` on `host`: `__Host-` unless the host is local."""
    return base if local(host) else HOST_PREFIX + base


def read(request: Request, base: str) -> tuple[str | None, bool]:
    """Cookie `base`'s value and whether it came under the old plain name on a host that now uses
    `__Host-` (accepted for rows from before this release only)."""
    if local(request.url.hostname):
        return request.cookies.get(base) or None, False
    value = request.cookies.get(HOST_PREFIX + base)
    if value:
        return value, False
    legacy = request.cookies.get(base) or None
    return legacy, legacy is not None


def set_cookie(
    response: Response, request: Request, base: str, value: str, *, max_age: int | None = None
) -> None:
    """HttpOnly, Secure, SameSite=Lax, `Path=/`, no `Domain` (what `__Host-` requires). Sessions
    have no `Max-Age`: the browser keeps them until it closes, the server decides when they end."""
    response.set_cookie(
        cookie_name(base, request.url.hostname),
        value,
        max_age=max_age,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def clear_cookie(response: Response, request: Request, base: str) -> None:
    """Delete cookie `base` under its name here, and under the old plain name too."""
    for name in dict.fromkeys((cookie_name(base, request.url.hostname), base)):
        response.delete_cookie(name, path="/", secure=True, httponly=True, samesite="lax")


# -- sessions -------------------------------------------------------------------------------


def begin(
    data: Mapping[str, Any], now: int, lifetime: Lifetime, *, ends_by: int | None = None
) -> tuple[dict[str, Any], int]:
    """A new session row's data (marked `v`, with its `absolute_at`) and its first expiry."""
    expires_at, absolute_at = lifetime.start(now, ends_by=ends_by)
    return {**data, "v": VERSION, "absolute_at": absolute_at}, expires_at


def resume(
    store: AuditStore, request: Request, base: str, kind: str, lifetime: Lifetime
) -> tuple[dict[str, Any] | None, bool]:
    """The session this request's cookie names, its idle expiry pushed forward when due, and
    whether a cookie was sent at all. None for no row, an ended one, or a row made under the
    `__Host-` name reached through the plain one."""
    value, legacy = read(request, base)
    if value is None:
        return None, False
    row = store.smart_read(value, kind)
    if row is None:
        return None, True
    data, expires_at = row
    if legacy and data.get("v"):
        return None, True
    now = int(time.time())
    absolute_at = data.get("absolute_at")
    if isinstance(absolute_at, int) and now >= absolute_at:
        # The row's clear `expires_at` never passes it; a sealed row's `absolute_at` cannot be
        # changed, so this holds even against a database writer who moves `expires_at`.
        return None, True
    if not isinstance(absolute_at, int):
        # From before this release: its expiry is its absolute limit, and the idle limit starts
        # with this request (the one time a session's expiry moves back).
        store.smart_replace(
            value, kind, {**data, "absolute_at": expires_at}, min(now + lifetime.idle, expires_at)
        )
        return data, True
    until = lifetime.extended(now, expires_at, absolute_at)
    if until is not None:
        store.smart_touch(value, kind, until)
    return data, True


# -- binding a sign-in or a launch to the browser ------------------------------------------------


def bind(response: Response, request: Request, base: str) -> tuple[str, str]:
    """Set a fresh binding cookie (`SIGNIN` or `LAUNCH`) on `response`; return its digest (which
    a pending sign-in keeps) and its value (which a pending launch is sealed under)."""
    value = secrets.token_urlsafe(32)
    set_cookie(response, request, base, value, max_age=PENDING_SECONDS)
    return _digest(value), value


def binding(request: Request, base: str) -> str | None:
    """The binding cookie `base` this browser brought back, under its name on this host."""
    return request.cookies.get(cookie_name(base, request.url.hostname)) or None


def bound(request: Request, base: str, pending: Mapping[str, Any]) -> bool:
    """Whether this browser started the sign-in `pending` records. A pending row from before
    this release keeps no digest: accepted (it is taken once, and ends in ten minutes). (A
    pending launch needs no digest: it is sealed under the cookie, and opens only with it.)"""
    expected = pending.get("browser")
    if expected is None:
        return True
    value = binding(request, base)
    if not value or not isinstance(expected, str):
        return False
    return hmac.compare_digest(_digest(value), expected)


def to_public_url(
    request: Request, public_url: str | None, path: str, params: Mapping[str, str]
) -> Response | None:
    """A redirect to `path` with `params` on `public_url` when this request reached another
    origin (scheme, host, port), so the host-only binding cookie is set where the callback comes
    back; None when it is there already, when no public URL is set, or once the browser was sent
    there (`MOVED`)."""
    if public_url is None or request.query_params.get(MOVED):
        return None
    public = urlsplit(public_url)
    here = request.url
    if _origin(public.scheme, public.hostname, public.port) == _origin(
        here.scheme, here.hostname, here.port
    ):
        return None
    query = urlencode({**params, MOVED: "1"})
    return RedirectResponse(f"{public_url.rstrip('/')}{path}?{query}", status_code=302)


def _origin(scheme: str, host: str | None, port: int | None) -> tuple[str, str, int | None]:
    scheme = scheme.lower()
    return scheme, (host or "").lower(), port or _DEFAULT_PORTS.get(scheme)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
