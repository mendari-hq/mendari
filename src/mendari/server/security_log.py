# SPDX-License-Identifier: Apache-2.0
"""The security log: one JSON line per request and per security event, on stdout (ADR-0005).

Vercel keeps stdout as the function's runtime log, and a self-hosted install sends it to the
site's own log system (a container's stdout is collected by Docker, Kubernetes or journald), so
nothing here needs a table: what must last for years (who read an input, who changed a key, a
client or an approval) is in the hash chain already; this is for incident response.

**Fields come from an allow-list** (`FIELDS`), and anything else is dropped by the formatter:
`ts`, `event`, `outcome`, `reason` (a fixed code), `request_id`, `method`, `route` (the route's
template, `/api/audit/{decision_id}`: never the raw path or its query), `status`, `actor` (a
`Principal.id`: `user:<hash>`, `key:<id>`, `client:<id>`, `ehr:<hash>` (an EHR launch), `open`
or `anonymous`), `org`, `ip`,
`ids` (decision, key, client or approval ids), `count`, `latency_ms`, and for an unhandled
exception its `error` type and `frames` (file, line, function). Never a credential, a token, an
assertion, a query value, a body, a header, an exception's message or case text: none of them is
a field. Values are JSON-escaped, so a newline in a claim cannot start a forged line.

**The request id** is Vercel's `x-vercel-id` on Vercel, else the client's `X-Request-Id` when it
looks like one (`^[A-Za-z0-9._:-]{8,128}$`; anything else could inject into logs), else a new
uuid4. Every response carries it as `X-Request-Id`, and JSON errors on `/api` as `request_id`, so
a person can quote it and the line be found.

**Access lines** (`event: request`): one per `/api`, `/fhir`, `/cds-services`, `/auth` and
`/smart` request, refused ones included, but not `/api/health` and `/api/health/ready`
(probes) or static files.

**Events**: `auth.signin.ok|refused`, `auth.logout`, `auth.credential.refused` (memberships
mode adds the reason `no_organization_record`), `auth.permission.refused` (and the reasons
`no_organization`, `org_changed` and `not_a_member`), `auth.organization.selected` and
`auth.access.requested` (memberships mode: a person choosing one of their organizations, and
asking to join one, refused as `unknown_organization`, `member` or `open_requests`),
`auth.invitation.accepted` (memberships mode, refused as `unverified`, `invalid`,
`other_address` or `member`, with no organization),
`admin.invitation.created|revoked`, `admin.membership.roles_changed|removed` and
`admin.access.granted|refused` (an organization's administrators managing its people),
`auth.origin.refused` (another site's request, server/origin.py),
`auth.token.issued|refused`, `smart.launch.ok|refused` (an EHR launch that starts a session,
with its `ehr:<hash>` and, with sign-in on, its organization; or one that does not:
`state_unknown`, `browser`, `token`, `id_token`, `issuer`, `unregistered`, `user_type` and the
like, server/smart_routes.py), `ratelimit.hit` (once per bucket until it has room again),
`quota.reached` (an organization's quota, with the metric as its reason; or a principal's
daily cap, reason `principal_decisions`, once a day per principal per instance),
`admin.key.created|rotated|revoked` (a rotation names the old key and its
replacement), `auth.key.replaced_used` (a rotated key used during its overlap, once per instance),
`admin.client.registered|keys_replaced|revoked`, `governance.approved|revoked`,
`registry_pack.approved|revoked`, `eval.run`,
`export.audit|audit.administrative|feedback`, `phi.inputs.read` (decision ids and a count) and
`error.unhandled`.

**Library loggers** that would log a URL or a connection at INFO (httpx, psycopg's pool: see
`QUIETED`) keep only warnings: on Vercel the runtime gives the root logger INFO and a handler.

**Unhandled exceptions** are logged as their type and stack frames. Under PHI mode `hash` or
`encrypt` the exception that reaches the server (and its own traceback print) is replaced by
one that names only the type, since a message can quote the input; in `off` mode (synthetic
data) the server prints it as before.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
import traceback
import uuid
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Literal, TextIO

from starlette.requests import Request
from starlette.routing import BaseRoute, Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

LOGGER = "mendari.security"
FIELDS = (
    "ts",
    "event",
    "outcome",
    "reason",
    "request_id",
    "method",
    "route",
    "status",
    "actor",
    "org",
    "ip",
    "ids",
    "count",
    "latency_ms",
    "error",
    "frames",
)
Outcome = Literal["ok", "refused", "limited", "invalid", "error"]
REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
LOGGED_PREFIXES = ("/api/", "/fhir", "/cds-services", "/auth/", "/smart/")
NOT_LOGGED = frozenset({"/api/health", "/api/health/ready"})
MAX_IDS = 500  # the largest page a route answers
MAX_FRAMES = 30
MAX_TEXT = 200  # any one string value: a reason, an id, a route
# Libraries that log a URL or a connection at INFO: httpx every request's URL (a FHIR read's
# Patient id, the alert webhook's secret path), the pool its connections. On Vercel the runtime
# sets the root logger to INFO with its own handler, which would print them all.
QUIETED = ("httpx", "httpcore", "httpx2", "httpcore2", "psycopg.pool")

_request: ContextVar[dict[str, Any] | None] = ContextVar("mendari_request", default=None)
logger = logging.getLogger(LOGGER)


class _Stdout(logging.StreamHandler[TextIO]):
    """stdout as it is when a line is written, not when the handler was made (a test's capture,
    a runtime that swaps it), like logging's own last-resort handler does for stderr."""

    def __init__(self) -> None:
        super().__init__(sys.stdout)

    @property
    def stream(self) -> TextIO:
        return sys.stdout

    @stream.setter
    def stream(self, value: TextIO) -> None:
        pass


class _JsonLines(logging.Formatter):
    """A record's `fields` as one JSON object on one line: allow-listed keys only."""

    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "fields", {})
        line = {key: fields[key] for key in FIELDS if fields.get(key) is not None}
        return json.dumps(line, ensure_ascii=True, separators=(",", ":"), default=str)


def configure() -> None:
    """Give the security logger its own stdout handler, once: INFO and up, and not passed on to
    the root logger, whose handlers (if a host configures any) would print it again, in their
    own format. Idempotent: `create_app` calls it for every app. The `QUIETED` libraries keep
    only their warnings and errors, whatever level the host gives the root logger."""
    for name in QUIETED:
        logging.getLogger(name).setLevel(logging.WARNING)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.disabled = False  # a dictConfig that disables existing loggers must not mute this
    if not any(isinstance(handler, _Stdout) for handler in logger.handlers):
        handler = _Stdout()
        handler.setFormatter(_JsonLines())
        logger.addHandler(handler)


configure()


def event(
    name: str,
    *,
    outcome: Outcome = "ok",
    reason: str | None = None,
    actor: str | None = None,
    org: str | None = None,
    ids: Iterable[str] | None = None,
    count: int | None = None,
    status: int | None = None,
    route: str | None = None,
    latency_ms: int | None = None,
    error: str | None = None,
    frames: list[str] | None = None,
) -> None:
    """Write one line; the current request's id, method, route and address are added. The
    parameters are the allow-list: there is no way to pass free text."""
    fields: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        **(_request.get() or {}),
        "event": name,
        "outcome": outcome,
        "reason": _text(reason),
        "actor": _text(actor),
        "org": _text(org),
        "ids": None if ids is None else [_text(str(i)) for i in list(ids)[:MAX_IDS]],
        "count": count,
        "status": status,
        "latency_ms": latency_ms,
        "error": _text(error),
        "frames": frames,
    }
    if route is not None:
        fields["route"] = _text(route)
    level = logging.WARNING if outcome == "error" else logging.INFO
    logger.log(level, name, extra={"fields": fields})


def request_id() -> str | None:
    """The current request's id (None outside a request)."""
    current = _request.get()
    return None if current is None else current.get("request_id")


def _text(value: str | None) -> str | None:
    return None if value is None else value[:MAX_TEXT]


def outcome_of(status: int) -> Outcome:
    if status < 400:
        return "ok"
    if status in (401, 403):
        return "refused"
    if status == 429:
        return "limited"
    if status >= 500:
        return "error"
    return "invalid"


def new_request_id(scope: Scope, *, on_vercel: bool) -> str:
    """Vercel's id on Vercel (it matches `proxy.vercelId` in a log drain), else a well-formed
    client `X-Request-Id`, else a new uuid4."""
    headers = {key.decode("latin-1"): value.decode("latin-1") for key, value in scope["headers"]}
    vercel = headers.get("x-vercel-id", "") if on_vercel else ""
    candidates = [vercel, headers.get("x-request-id", "")]
    return next((c for c in candidates if REQUEST_ID.fullmatch(c)), str(uuid.uuid4()))


def route_template(routes: Iterable[BaseRoute], scope: Scope) -> str | None:
    """The template of the route a request is for (its path with `{parameters}`), matched the
    way the router matches, so a request the guard refused has one too; None when no route
    matches (a 404's raw path is not logged). As in the router, the first full match wins, else
    the first route with the path but another method (a 405)."""
    partial = None
    for route in routes:
        match, _ = route.matches(scope)
        path = getattr(route, "path", None)
        if match is Match.FULL:
            return path if isinstance(path, str) else None
        if match is Match.PARTIAL and partial is None and isinstance(path, str):
            partial = path
    return partial


def frames_of(error: BaseException) -> list[str]:
    """Where an exception was raised: `file:line function` for each frame, innermost last, the
    file relative to its package; no source line, no locals."""
    summary = traceback.extract_tb(error.__traceback__)[-MAX_FRAMES:]
    return [f"{_short(frame.filename)}:{frame.lineno} {frame.name}" for frame in summary]


def _short(filename: str) -> str:
    for marker in ("/site-packages/", "/src/"):
        if marker in filename:
            return filename.rsplit(marker, 1)[1]
    return filename.rsplit("/", 1)[-1]


class InternalError(Exception):
    """What reaches the server in place of an unhandled exception under PHI mode `hash` or
    `encrypt`: the original's type, without its message (which can quote the input)."""


class RequestLog:
    """Outermost of Mendari's middleware: gives each request its id, returns it in
    `X-Request-Id`, writes the access line, and logs an unhandled exception without its
    message. `ip_of` reads the caller's address (server/limits.py), `routes` are the app's."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        routes: Callable[[], Iterable[BaseRoute]],
        ip_of: Callable[[Request], str],
        open_mode: bool,
        phi_mode: str,
        on_vercel: bool,
    ) -> None:
        self.app = app
        self.routes = routes
        self.ip_of = ip_of
        self.open_mode = open_mode
        self.quiet_errors = phi_mode in ("hash", "encrypt")
        self.on_vercel = on_vercel

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        rid = new_request_id(scope, on_vercel=self.on_vercel)
        # The state is shared with the request objects downstream, and with the exception
        # handler that answers a 500 outside this middleware (`_unexpected`).
        scope.setdefault("state", {})["request_id"] = rid
        path: str = scope["path"]
        logged = path.startswith(LOGGED_PREFIXES) and path not in NOT_LOGGED
        context = {
            "request_id": rid,
            "method": scope["method"],
            "route": route_template(self.routes(), scope),
            "ip": self.ip_of(Request(scope)),
        }
        token = _request.set(context)
        status = 500

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = [*message.get("headers", []), (b"x-request-id", rid.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception as error:
            status = 500
            event(
                "error.unhandled",
                outcome="error",
                error=type(error).__name__,
                frames=frames_of(error),
                status=500,
            )
            if self.quiet_errors:
                raise InternalError(type(error).__name__) from None
            raise
        finally:
            if logged:
                actor, org = self.actor(scope)
                event(
                    "request",
                    outcome=outcome_of(status),
                    status=status,
                    actor=actor,
                    org=org,
                    latency_ms=round((time.perf_counter() - started) * 1000),
                )
            _request.reset(token)

    def actor(self, scope: Scope) -> tuple[str, str | None]:
        """Who the guard let through (`request.state.principal`), `open` in open mode, else
        `anonymous`: a public route, or a refused request."""
        principal = scope.get("state", {}).get("principal")
        if principal is not None:
            return str(principal.id), None if principal.org is None else str(principal.org)
        return ("open", None) if self.open_mode else ("anonymous", None)
