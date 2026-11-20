# SPDX-License-Identifier: Apache-2.0
"""Origin checks: a page on another site cannot make a signed-in browser act here.

A browser attaches Mendari's cookies to some requests that another site starts. SameSite=Lax
keeps them off cross-site forms and subresources, but not off top-level navigations (a link to
`/api/review` would make a reviewer's browser read, and under PHI `encrypt` chain, up to 500
inputs in that reviewer's name), and not off a sibling subdomain's requests, which are
same-site. So `_guard` asks, before anything else and before any body is read, where a browser
request comes from:

- With Fetch Metadata (`Sec-Fetch-Site`, every current browser): `same-origin` passes, and
  `none` (the address bar, a bookmark) passes to read. `same-site`, `cross-site` and anything
  else reach only `public` and `page` routes (`permission_for`, so there is no list of routes to
  keep), and only with GET or HEAD: a link to the dashboard, a sign-in callback or the
  capability statement works; a link to an API route that reads or writes for the person does
  not.
- Without it (an older browser), an `Origin` that is `null` or another site's counts as
  cross-site. This site is the request's own host, `MENDARI_PUBLIC_URL` and
  `MENDARI_TRUSTED_ORIGINS` (aliases).
- With neither, an unsafe method passes: a page cannot take `Origin` off an unsafe request, so
  it is not from a browser (curl, ZAP, pilot-check). A read with a Mendari cookie may be an
  older browser (Safari before 16.4) following another site's link, which sends neither: it
  reaches the routes beyond `public` and `page` only with `X-Mendari-Request` (any value; the
  pages send `1`), a header only Mendari's own pages send. Another site cannot add it without a
  CORS preflight, which Mendari never grants for it (it grants one only on `/cds-services`, for
  a CDS client's registered origins, and only `authorization` and `content-type` there:
  `server/cors.py`). So a script that reads with a session cookie sends
  it too, and in such a browser an API route typed into the address bar is refused like a link.

It applies to a request without an `Authorization` header (a bearer credential decides alone,
and another site cannot attach one without a CORS preflight either) that carries a Mendari
cookie or uses an unsafe method (POST, PUT, PATCH, DELETE), and in open mode to every request,
since there none needs a credential. Unsafe methods are checked with no cookie too: the session
cookie is SameSite=Lax, so a browser leaves it off another site's form POST, yet the answer can
still clear it (a form on another site posting to `/auth/logout` would sign the person out).
Machines send no cookie, so curl, EHR servers, CDS clients and the token endpoint's backend
clients are not affected.

One exception (LP-23c): a page on an origin a CDS client registered (`MENDARI_CDS_CLIENTS`,
`origins`) may call `/cds-services` and its children, without a Mendari cookie. CORS lets it
read the answer there (`server/cors.py`), and the guard still asks for its credential with
sign-in on, and that a CDS client's token comes from that client's own origins
(`CdsRegistry.from_its_origin`). Everywhere else such a page is another site.

A refusal is a `403` with the request's id, and one `auth.origin.refused` line in the security
log (server/security_log.py) whose reason is a fixed code, never the header's value.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit

from starlette.requests import Request

from mendari.errors import AuthError, MendariError
from mendari.server import security_log
from mendari.server.auth import CDS_PATH, permission_for

SAFE_METHODS = frozenset({"GET", "HEAD"})
REACHABLE = frozenset({"public", "page"})  # what another site's request may reach, to read
REFUSED = "cross-site request refused"
# Sent by every request Mendari's own pages make (dashboard/ui.js `OWN_REQUEST`): a read with a
# Mendari cookie and no Fetch Metadata or `Origin` needs it to reach more than a page.
OWN_REQUEST = "x-mendari-request"
# The security log's reason for a refusal, by `Sec-Fetch-Site` (anything else: `unknown_site`).
_REASONS = {"cross-site": "cross_site", "same-site": "same_site", "none": "unsafe_navigation"}
_DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True)
class OriginCheck:
    open_mode: bool  # sign-in is off: every request is checked
    trusted: frozenset[str]  # this site's other origins (`trusted_origins`)
    # The origins CDS clients registered (`CdsRegistry.origins`, LP-23c): pages there may call
    # `/cds-services` without a Mendari cookie.
    cds_origins: frozenset[str] = frozenset()
    # The pack registry is mounted here (LP-19d): only then is `/registry/` a public route
    # another site's page may read, as the guard reads it (`Auth.permission`).
    registry: bool = False

    def check(self, request: Request) -> None:
        """Returns when the request may go on; an `AuthError` (403) when another site started
        it and it asks for more than to read a public route or a page. Reads headers only."""
        safe = request.method in SAFE_METHODS
        bearer = "authorization" in request.headers
        cookie = _has_mendari_cookie(request)
        if not self.open_mode and (bearer or (safe and not cookie)):
            return
        if not cookie and self._from_a_cds_origin(request):
            return
        site = request.headers.get("sec-fetch-site", "").strip().lower()
        if site == "same-origin" or (site == "none" and safe):
            return
        sent_origins = request.headers.getlist("origin")
        if len(sent_origins) > 1:
            security_log.event("auth.origin.refused", outcome="refused", reason="foreign_origin")
            raise AuthError(REFUSED, 403)
        origin = sent_origins[0] if sent_origins else None
        if not site:
            if origin is not None and is_this_site(origin, request, self.trusted):
                return
            # Neither header: a script, or an older browser. Only a cookie's read without a
            # credential could be another site's link, and Mendari's own pages say it is not.
            if origin is None and (
                not (safe and cookie and not bearer) or OWN_REQUEST in request.headers
            ):
                return
        # Another site started it (or the address bar sent an unsafe method): a page, to read.
        needed = permission_for("GET", request.url.path, registry=self.registry)
        if safe and needed in REACHABLE:
            return
        if site:
            reason = _REASONS.get(site, "unknown_site")
        elif origin is None:
            reason = "no_fetch_metadata"
        else:
            reason = "null_origin" if origin == "null" else "foreign_origin"
        security_log.event("auth.origin.refused", outcome="refused", reason=reason)
        raise AuthError(REFUSED, 403)

    def _from_a_cds_origin(self, request: Request) -> bool:
        """A call to `/cds-services` (or a child) whose one `Origin` a CDS client registered."""
        if not self.cds_origins or not CDS_PATH.match(request.url.path):
            return False
        sent = request.headers.getlist("origin")
        return len(sent) == 1 and origin_of(sent[0]) in self.cds_origins


def is_this_site(origin: str, request: Request, trusted: frozenset[str]) -> bool:
    """Whether `origin` is this site: the request's own host, or one of `trusted`
    (`trusted_origins`). `Origin: null` (a sandboxed frame, a redirect across sites) is never
    this site."""
    normal = origin_of(origin)
    if normal is None:
        return False
    own = origin_of(f"{urlsplit(normal).scheme}://{request.headers.get('host', '')}")
    return normal == own or normal in trusted


def _has_mendari_cookie(request: Request) -> bool:
    """Either cookie (session or SMART launch), under any prefix (`__Host-` included)."""
    return any("mendari_" in name for name in request.cookies)


def origin_of(value: str) -> str | None:
    """`scheme://host[:port]`, in lower case and without the scheme's default port; None for
    anything else (`null`, no host, another scheme, credentials, a bad port)."""
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return None
    scheme, host = parts.scheme.lower(), parts.hostname
    if scheme not in _DEFAULT_PORTS or not host or parts.username or parts.password:
        return None
    if ":" in host:
        host = f"[{host}]"  # IPv6
    shown = "" if port is None or port == _DEFAULT_PORTS[scheme] else f":{port}"
    return f"{scheme}://{host}{shown}"


def trusted_origins() -> frozenset[str]:
    """The origins that are this site besides the request's own host: `MENDARI_PUBLIC_URL`'s,
    and `MENDARI_TRUSTED_ORIGINS` (comma-separated, for aliases a proxy rewrites). A trusted
    origin that is not exactly an origin fails at startup."""
    trusted = set()
    public = origin_of(os.environ.get("MENDARI_PUBLIC_URL", ""))  # checked where it is used
    if public is not None:
        trusted.add(public)
    for raw in os.environ.get("MENDARI_TRUSTED_ORIGINS", "").split(","):
        value = raw.strip()
        if not value:
            continue
        origin = origin_of(value)
        if origin is None or urlsplit(value).path not in ("", "/") or "?" in value or "#" in value:
            raise MendariError(
                f"MENDARI_TRUSTED_ORIGINS: {value!r} is not an origin (https://host[:port])"
            )
        trusted.add(origin)
    return frozenset(trusted)


PUBLIC_URL_RULE = (
    "MENDARI_PUBLIC_URL must be an https URL without a query or fragment (plain http only for a "
    "loopback or single-label host)"
)


def plain_http_host(host: str) -> bool:
    """Whether a URL on `host` may be plain http: this machine (`localhost`, a loopback address)
    or a single-label name, such as a compose service, which no public DNS name is. The FHIR
    allow-list has the same rule (`fhir_client.allowed_servers`)."""
    if host == "localhost" or (bool(host) and "." not in host and ":" not in host):
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def checked_public_url(public: str) -> str:
    """`public`, `MENDARI_PUBLIC_URL` without its trailing slash, as the audience a CDS client's
    token (`cds_clients`) and a Backend Services assertion (`backend_services`) are addressed to:
    https, or plain http on a `plain_http_host` (the compose sandbox's `http://engine:8000` and
    `http://engine-signin:8000`), with a host and no query or fragment. Both check it here, so a
    value one takes the other takes too; a refusal states the rule, never the value."""
    try:
        parts = urlsplit(public)
        host = parts.hostname or ""
    except ValueError:
        raise MendariError(PUBLIC_URL_RULE) from None
    if (
        not host
        or parts.query
        or parts.fragment
        or not (parts.scheme == "https" or (parts.scheme == "http" and plain_http_host(host)))
    ):
        raise MendariError(PUBLIC_URL_RULE)
    return public
