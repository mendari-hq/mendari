# SPDX-License-Identifier: Apache-2.0
"""CORS for the CDS Hooks services (LP-23c, ADR-0014): a CDS client running in a browser, on an
origin it registered (`MENDARI_CDS_CLIENTS`, `origins`), reads the answers of `/cds-services`
and its children. Nothing else is served across origins.

- **Only `/cds-services` and its children**, and only for the exact origins the registry lists
  (`CdsRegistry.origins`, the union of every client's). With none registered this does nothing.
- **A preflight** (`OPTIONS` with `Origin` and `Access-Control-Request-Method`) from a registered
  origin is answered here, before the guard (it carries no credential): `204`, with
  `Access-Control-Allow-Origin` that origin, `Access-Control-Allow-Methods: GET, POST, OPTIONS`,
  `Access-Control-Allow-Headers: authorization, content-type`, `Access-Control-Max-Age: 600`.
  It is answered in either mode and from any machine: it carries no credential and returns no
  data, and the call that follows meets the guard (in open mode, from this machine only).
- **Every other answer to a registered origin** there, a refusal included, carries
  `Access-Control-Allow-Origin` that origin, so the page can read why it was refused.
- **Never `Access-Control-Allow-Credentials`**: a browser sends no cookie with such a request, and
  could not read the answer of one that did. A CDS client sends its own token instead.
- **Any other origin gets no CORS header**, and goes on to the guard as before: another site's
  preflight and calls are refused there (`server/origin.py`).
- Every answer on these paths carries `Vary: Origin`, so a cache never serves one origin's answer
  to another.

`Cross-Origin-Resource-Policy: same-origin` stays on every answer: it governs `no-cors` loads
(an image, a script), not a CORS read. The guard still decides who may call (`server/auth.py`):
with sign-in on, a credential; a CDS client's token only from that client's own origins; and in
open mode, this machine only.
"""

from __future__ import annotations

from collections.abc import Collection

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mendari.server.auth import CDS_PATH
from mendari.server.origin import origin_of

ALLOW_METHODS = "GET, POST, OPTIONS"
ALLOW_HEADERS = "authorization, content-type"
MAX_AGE = "600"  # seconds a browser may keep a preflight's answer


class Cors:
    """The middleware: outside the guard, inside the security headers (so a preflight's answer
    gets them too) and the request log (so it is logged)."""

    def __init__(self, app: ASGIApp, *, origins: Collection[str]):
        self.app = app
        self.origins = frozenset(origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.origins or not CDS_PATH.match(scope["path"]):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        origin = self.registered(headers)
        preflight = scope["method"] == "OPTIONS" and "access-control-request-method" in headers
        if origin is not None and preflight:
            answered = Response(
                status_code=204,
                headers={
                    "access-control-allow-origin": origin,
                    "access-control-allow-methods": ALLOW_METHODS,
                    "access-control-allow-headers": ALLOW_HEADERS,
                    "access-control-max-age": MAX_AGE,
                    "vary": "Origin",
                },
            )
            await answered(scope, receive, send)
            return

        async def answer(message: Message) -> None:
            if message["type"] == "http.response.start":
                sent = MutableHeaders(scope=message)
                if origin is not None:
                    sent["access-control-allow-origin"] = origin
                sent.add_vary_header("Origin")
            await send(message)

        await self.app(scope, receive, answer)

    def registered(self, headers: Headers) -> str | None:
        """The request's one `Origin`, when a CDS client registered it; None for none, two, or
        any other."""
        sent = headers.getlist("origin")
        origin = origin_of(sent[0]) if len(sent) == 1 else None
        return origin if origin in self.origins else None
