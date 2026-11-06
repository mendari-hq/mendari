# SPDX-License-Identifier: Apache-2.0
"""Outbound FHIR reads, only to servers the deployment allows.

Every call Mendari makes to a FHIR server someone else named goes through `FhirClient`:
a CDS Hooks `fhirServer`, a SMART launch's `iss`, a Bulk export URL. That makes it the one place
that guards against server-side request forgery:

- **Allow-list:** the server's origin must be listed in `MENDARI_FHIR_ALLOWED_SERVERS`
  (exact `scheme://host[:port]`; empty by default, so nothing is fetched). Plain `http` is
  accepted only for loopback hosts and single-label hostnames (a compose service such as
  `http://fhir:8080`), and only when listed.
- **Paths stay under the server's base URL**; `next` links must too.
- **No redirects**, a timeout, a response size cap, and JSON only.
- **Errors say what failed, never what the server answered**, and never include the token.
  Each carries its fixed reason (`FhirError.reason`, `errors.ReadReason`), which a decision
  that could not read part of its record abstains with (LP-23d).
"""

from __future__ import annotations

import ipaddress
import json
import os
import posixpath
import re
from collections.abc import Callable, Collection, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from mendari.errors import FhirError, MendariError, ReadReason

MAX_BYTES = 5 * 1024 * 1024
TIMEOUT = 10.0
MAX_PAGES = 10  # a search's `next` links followed at most this many times
_JSON_TYPES = ("application/fhir+json", "application/json")


def allowed_servers(variable: str = "MENDARI_FHIR_ALLOWED_SERVERS") -> frozenset[str]:
    """Origins from $MENDARI_FHIR_ALLOWED_SERVERS (comma-separated), checked at startup. Other
    allow-lists of the same form (`variable`) are read the same way."""
    raw = os.environ.get(variable, "")
    origins = set()
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        origin = origin_of(item)
        if origin is None or origin != item.rstrip("/"):
            raise MendariError(
                f"{variable}: {item!r} must be an origin, "
                "like https://ehr.example.org or http://fhir:8080"
            )
        if origin.startswith("http://") and not _local(urlsplit(origin).hostname or ""):
            raise MendariError(
                f"{variable}: {item!r} must use https "
                "(plain http only for localhost or a single-label compose service)"
            )
        origins.add(origin)
    return frozenset(origins)


def check_allowed(url: str, allowed: Collection[str] | None = None) -> None:
    """A FhirError (`off_server`) unless `url`'s origin is on the allow-list (`allowed`, else
    $MENDARI_FHIR_ALLOWED_SERVERS). Nothing is sent."""
    origin = origin_of(url)
    if origin is None or origin not in (allowed_servers() if allowed is None else allowed):
        raise FhirError(
            "the FHIR server is not on this deployment's allow-list", reason="off_server"
        )


class GuardedHttp:
    """HTTP to allow-listed origins only: no redirects, a timeout, and JSON answers under the size
    cap. `FhirClient` reads through it; SMART uses it for discovery, token and JWKS requests."""

    def __init__(
        self,
        *,
        allowed: Collection[str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.allowed = allowed_servers() if allowed is None else frozenset(allowed)
        self._client = httpx.Client(timeout=TIMEOUT, follow_redirects=False, transport=transport)

    def __enter__(self) -> GuardedHttp:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def check(self, url: str) -> None:
        check_allowed(url, self.allowed)

    def get_json(
        self,
        url: str,
        headers: Mapping[str, str] | None = None,
        *,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        """A JSON answer to a GET. `max_bytes` caps this answer below `MAX_BYTES` (a linked read's,
        LP-23h): past it, the answer stops arriving and is a `FhirError` (`too_large`)."""
        extra = {} if max_bytes is None else {"max_bytes": max_bytes}
        return self._send("GET", url, headers=headers, **extra)

    def post_form(
        self, url: str, data: Mapping[str, str], headers: Mapping[str, str] | None = None
    ) -> dict[str, Any]:
        return self._send("POST", url, headers=headers, data=data)

    def send_json(
        self,
        method: str,
        url: str,
        body: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
        *,
        timeout: float | None = None,
        stop: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """A FHIR JSON request (a CQL `$evaluate`, a Library upload); its JSON answer. A
        `timeout` (seconds, each step) replaces the client's `TIMEOUT` for this request: what
        remains of a decision's deadline (`providers/deadline.py`). `stop`, asked before each
        part of the answer is read, ends an answer still arriving (`FhirError`): the deadline's
        `past`, so an engine sending a little at a time cannot outlast it."""
        sent = {"content-type": "application/fhir+json", "accept": "application/fhir+json"}
        extra: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
        if stop is not None:
            extra["stop"] = stop
        return self._send(
            method, url, headers={**sent, **(headers or {})}, json=dict(body), **extra
        )

    def raw(
        self, method: str, url: str, headers: Mapping[str, str] | None = None, **kwargs: Any
    ) -> tuple[int, httpx.Headers, bytes]:
        """Status, headers and body (capped), for protocols that answer 202 or put their result
        in a header (Bulk export). Redirects are refused, as everywhere."""
        self.check(url)
        try:
            with self._client.stream(method, url, headers=headers, **kwargs) as response:
                if response.is_redirect:
                    raise _redirected(response.status_code)
                body = bytearray()
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise _too_large()
                return response.status_code, response.headers, bytes(body)
        except httpx.HTTPError as error:
            raise _unreachable(error) from None

    def download(
        self, url: str, path: Path, *, headers: Mapping[str, str] | None, max_bytes: int
    ) -> int:
        """Stream a file (Bulk NDJSON) to `path`, up to `max_bytes`; return its size."""
        self.check(url)
        size = 0
        try:
            with self._client.stream("GET", url, headers=headers) as response:
                if response.is_redirect or not response.is_success:
                    status = response.status_code
                    raise FhirError(
                        f"the download answered HTTP {status}", reason=_status_reason(status)
                    )
                with path.open("wb") as out:
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise FhirError(
                                f"the download is larger than {max_bytes} bytes",
                                reason="too_large",
                            )
                        out.write(chunk)
        except httpx.HTTPError as error:
            path.unlink(missing_ok=True)
            raise FhirError(
                f"the download failed ({type(error).__name__})", reason=_network_reason(error)
            ) from None
        except FhirError:
            path.unlink(missing_ok=True)
            raise
        return size

    def _send(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        self.check(url)
        stop: Callable[[], bool] | None = kwargs.pop("stop", None)
        cap = min(MAX_BYTES, kwargs.pop("max_bytes", MAX_BYTES))
        try:
            with self._client.stream(method, url, **kwargs) as response:
                if response.is_redirect:
                    raise _redirected(response.status_code)
                body = bytearray()
                for chunk in response.iter_bytes():
                    if stop is not None and stop():
                        raise FhirError("the answer did not arrive in time", reason="timeout")
                    body += chunk
                    if len(body) > cap:
                        raise _too_large(cap)
                if not response.is_success:
                    status = response.status_code
                    raise FhirError(
                        f"the server answered HTTP {status}{_oauth(bytes(body))}",
                        status=status,
                        reason=_status_reason(status),
                    )
                kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if kind not in _JSON_TYPES:
                    raise FhirError("the server did not answer with JSON", reason="not_json")
        except httpx.HTTPError as error:
            raise _unreachable(error) from None
        try:
            data = json.loads(bytes(body))
        except (ValueError, RecursionError):  # not JSON, over 4,300 digits, or nested too deep
            raise FhirError("the server answered with invalid JSON", reason="not_json") from None
        if not isinstance(data, Mapping):
            raise FhirError(
                "the server answered with something other than a JSON object", reason="not_json"
            )
        return dict(data)


class FhirClient:
    """Reads from one FHIR server (its base URL), with an optional bearer token."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        allowed: Collection[str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self._http = GuardedHttp(allowed=allowed, transport=transport)
        self._http.check(base_url)
        self.base = base_url.rstrip("/") + "/"
        self._headers = {"accept": "application/fhir+json"}
        if token:
            self._headers["authorization"] = f"Bearer {token}"

    def __enter__(self) -> FhirClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def read(self, relative: str, *, complete: bool = False) -> dict[str, Any]:
        """GET `relative` (e.g. "Patient/123" or "Observation?patient=123") under the base URL.
        A searchset's `next` pages are followed and merged into one Bundle, at most `MAX_PAGES`
        of them after the first. With `complete` (a decision's record, LP-23d), a search with
        more pages than that is a `FhirError` (`cap`) rather than its first pages, and so is a
        page that is not a Bundle (`operation_outcome`, `not_r4`): a record cut short is not
        read. A page whose `entry` or `link` is not R4's shape, or whose `next` link is not a
        URL, is a `FhirError` (`not_r4`) in every mode, never a crash."""
        result = self._http.get_json(self.url_of(relative), self._headers)
        if result.get("resourceType") != "Bundle":
            return result
        entries = _entries(result)
        for _ in range(MAX_PAGES):
            following = _next(result)
            if not following:
                break
            result = self._http.get_json(self.url_of(following), self._headers)
            if complete and result.get("resourceType") != "Bundle":
                kind: ReadReason = (
                    "operation_outcome"
                    if result.get("resourceType") == "OperationOutcome"
                    else "not_r4"
                )
                raise FhirError("a page of the search is not a Bundle", reason=kind)
            entries += _entries(result)
        else:
            if complete and _next(result):
                raise FhirError(f"the search has more than {MAX_PAGES + 1} pages", reason="cap")
        return {"resourceType": "Bundle", "type": "searchset", "entry": entries}

    def read_resource(self, relative: str, *, max_bytes: int | None = None) -> dict[str, Any]:
        """GET one resource (`Type/id`, or an absolute URL under the base URL): one request, no
        page followed, its answer at most `max_bytes` (`MAX_BYTES` unless set lower). What a
        linked read uses (LP-23h, `standards/linked.py`)."""
        return self._http.get_json(self.url_of(relative), self._headers, max_bytes=max_bytes)

    def url_of(self, relative: str) -> str:
        """`relative` (a path, or an absolute URL) as an absolute URL under the base URL, or a
        FhirError (`off_server`) for anything that escapes it (a URL that does not parse cannot
        be shown to stay under it). Nothing is sent."""
        try:
            parts = urlsplit(urljoin(self.base, relative))
        except ValueError:  # e.g. a malformed IPv6 host
            raise FhirError("a FHIR request is not a URL", reason="off_server") from None
        base = urlsplit(self.base)
        path = posixpath.normpath(parts.path or "/")
        if (parts.scheme, parts.netloc) != (base.scheme, base.netloc) or not (
            path.rstrip("/") + "/"
        ).startswith(base.path):
            raise FhirError("a FHIR request left the server's base URL", reason="off_server")
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def _entries(bundle: Mapping[str, Any]) -> list[Any]:
    """A Bundle page's entries (none when `entry` is absent or empty); any other `entry` that is
    not a list is not R4."""
    entries = bundle.get("entry") or []
    if not isinstance(entries, list):
        raise FhirError("a page of the search has an entry that is not a list", reason="not_r4")
    return list(entries)


def _next(bundle: Mapping[str, Any]) -> str | None:
    """A Bundle page's `next` link, if it has one (none when `link` or its URL is absent or
    empty). A `link` that is not a list of objects, or a `next` URL that is not printable text,
    is not R4, and could not be sent."""
    links = bundle.get("link") or []
    if not isinstance(links, list) or not all(isinstance(x, Mapping) for x in links):
        raise FhirError("a page of the search has a link that is not R4", reason="not_r4")
    url = next((x.get("url") for x in links if x.get("relation") == "next"), None)
    if not url:
        return None
    if not isinstance(url, str) or not url.isprintable():
        raise FhirError("a page of the search has a next link that is not a URL", reason="not_r4")
    return url


def _status_reason(status: int) -> ReadReason:
    return "unauthorized" if status in (401, 403) else "http_status"


def _network_reason(error: httpx.HTTPError) -> ReadReason:
    return "timeout" if isinstance(error, httpx.TimeoutException) else "network"


def _redirected(status: int) -> FhirError:
    return FhirError(f"the server redirected (HTTP {status})", reason="http_status")


def _too_large(cap: int | None = None) -> FhirError:
    size = MAX_BYTES if cap is None else cap
    return FhirError(f"the response is larger than {size} bytes", reason="too_large")


def _unreachable(error: httpx.HTTPError) -> FhirError:
    reason = _network_reason(error)
    return FhirError(f"could not reach the server ({type(error).__name__})", reason=reason)


def _oauth(body: bytes) -> str:
    """An OAuth error code from an error response (" (invalid_grant)"), and nothing else of it."""
    try:
        code = json.loads(body).get("error")
    except (ValueError, AttributeError, RecursionError):
        return ""
    return f" ({code})" if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,40}", code) else ""


def origin_of(url: str) -> str | None:
    """`scheme://host[:port]` as the allow-list compares origins (the host in lower case, the
    port as written), or None for anything else, a port that is not a number included."""
    try:
        return _origin(url)
    except ValueError:
        return None


def _origin(url: str) -> str | None:
    parts = urlsplit(url.strip())
    if parts.scheme not in ("https", "http") or not parts.hostname:
        return None
    if parts.username or parts.password:
        return None
    port = f":{parts.port}" if parts.port else ""
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{parts.scheme}://{host.lower()}{port}"


def _local(host: str) -> bool:
    if host == "localhost" or ("." not in host and ":" not in host):
        return True  # localhost, or a single-label name such as a compose service
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
