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
- With neither, the request is not from a browser (curl, ZAP, pilot-check) and passes: a page
  cannot take `Origin` off an unsafe request.

It applies to a request that carries a Mendari cookie and no `Authorization` header (a bearer
credential decides alone, and another site cannot attach one without a CORS preflight, which
Mendari never grants), and in open mode to every request, since there none needs a credential.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit

from starlette.requests import Request

from mendari.errors import AuthError, MendariError
from mendari.server.auth import permission_for

SAFE_METHODS = frozenset({"GET", "HEAD"})
REACHABLE = frozenset({"public", "page"})  # what another site's request may reach, to read
REFUSED = "cross-site request refused"
_DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True)
class OriginCheck:
    open_mode: bool  # sign-in is off: every request is checked
    trusted: frozenset[str]  # this site's other origins (`trusted_origins`)

    def check(self, request: Request) -> None:
        """Returns when the request may go on; an `AuthError` (403) when another site started
        it and it asks for more than to read a public route or a page. Reads headers only."""
        if not self.open_mode and (
            "authorization" in request.headers or not _has_mendari_cookie(request)
        ):
            return
        site = request.headers.get("sec-fetch-site", "").strip().lower()
        safe = request.method in SAFE_METHODS
        if site == "same-origin" or (site == "none" and safe):
            return
        if not site:
            origin = request.headers.get("origin")
            if origin is None or self._is_this_site(origin, request):
                return
        # Another site started it (or the address bar sent an unsafe method): a page, to read.
        if safe and permission_for("GET", request.url.path) in REACHABLE:
            return
        raise AuthError(REFUSED, 403)

    def _is_this_site(self, origin: str, request: Request) -> bool:
        """`Origin: null` (a sandboxed frame, a redirect across sites) is never this site."""
        normal = origin_of(origin)
        if normal is None:
            return False
        own = origin_of(f"{urlsplit(normal).scheme}://{request.headers.get('host', '')}")
        return normal == own or normal in self.trusted


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


def is_local_host(host: str | None) -> bool:
    """This machine: `localhost`, a `.localhost` name or a loopback address. Such a host gets
    no HSTS, which would bind every other local server on it to https."""
    name = (host or "").strip("[]").lower()
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        return ip_address(name).is_loopback
    except ValueError:
        return False
