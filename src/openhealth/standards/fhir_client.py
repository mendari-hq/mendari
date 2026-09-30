"""Outbound FHIR reads, only to servers the deployment allows.

Every call OpenHealth makes to a FHIR server someone else named goes through `FhirClient`:
a CDS Hooks `fhirServer`, a SMART launch's `iss`, a Bulk export URL. That makes it the one place
that guards against server-side request forgery:

- **Allow-list:** the server's origin must be listed in `OPENHEALTH_FHIR_ALLOWED_SERVERS`
  (exact `scheme://host[:port]`; empty by default, so nothing is fetched). Plain `http` is
  accepted only for loopback hosts and single-label hostnames (a compose service such as
  `http://fhir:8080`), and only when listed.
- **Paths stay under the server's base URL**; `next` links must too.
- **No redirects**, a timeout, a response size cap, and JSON only.
- **Errors say what failed, never what the server answered**, and never include the token.
"""

from __future__ import annotations

import ipaddress
import json
import os
import posixpath
from collections.abc import Collection, Mapping
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from openhealth.errors import FhirError, OpenHealthError

MAX_BYTES = 5 * 1024 * 1024
TIMEOUT = 10.0
MAX_PAGES = 10  # a search's `next` links followed at most this many times
_JSON_TYPES = ("application/fhir+json", "application/json")


def allowed_servers() -> frozenset[str]:
    """Origins from $OPENHEALTH_FHIR_ALLOWED_SERVERS (comma-separated), checked at startup."""
    raw = os.environ.get("OPENHEALTH_FHIR_ALLOWED_SERVERS", "")
    origins = set()
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        origin = _origin(item)
        if origin is None or origin != item.rstrip("/"):
            raise OpenHealthError(
                f"OPENHEALTH_FHIR_ALLOWED_SERVERS: {item!r} must be an origin, "
                "like https://ehr.example.org or http://fhir:8080"
            )
        if origin.startswith("http://") and not _local(urlsplit(origin).hostname or ""):
            raise OpenHealthError(
                f"OPENHEALTH_FHIR_ALLOWED_SERVERS: {item!r} must use https "
                "(plain http only for localhost or a single-label compose service)"
            )
        origins.add(origin)
    return frozenset(origins)


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
        allowed = allowed_servers() if allowed is None else frozenset(allowed)
        origin = _origin(base_url)
        if origin is None or origin not in allowed:
            raise FhirError("the FHIR server is not on this deployment's allow-list")
        self.base = base_url.rstrip("/") + "/"
        self._headers = {"accept": "application/fhir+json"}
        if token:
            self._headers["authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            timeout=TIMEOUT, follow_redirects=False, transport=transport, headers=self._headers
        )

    def __enter__(self) -> FhirClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def read(self, relative: str) -> dict[str, Any]:
        """GET `relative` (e.g. "Patient/123" or "Observation?patient=123") under the base URL.
        A searchset's `next` pages are followed and merged into one Bundle."""
        result = self._get(self._resolve(relative))
        if result.get("resourceType") != "Bundle":
            return result
        entries = list(result.get("entry") or [])
        for _ in range(MAX_PAGES):
            links = result.get("link") or []
            following = next((x.get("url") for x in links if x.get("relation") == "next"), None)
            if not following:
                break
            result = self._get(self._resolve(following))
            entries += result.get("entry") or []
        return {"resourceType": "Bundle", "type": "searchset", "entry": entries}

    def _resolve(self, relative: str) -> str:
        """An absolute URL under the base URL, or a FhirError for anything that escapes it."""
        parts = urlsplit(urljoin(self.base, relative))
        base = urlsplit(self.base)
        path = posixpath.normpath(parts.path or "/")
        if (parts.scheme, parts.netloc) != (base.scheme, base.netloc) or not (
            path.rstrip("/") + "/"
        ).startswith(base.path):
            raise FhirError("a FHIR request left the server's base URL")
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))

    def _get(self, url: str) -> dict[str, Any]:
        try:
            with self._client.stream("GET", url) as response:
                if response.is_redirect:
                    raise FhirError(f"the FHIR server redirected (HTTP {response.status_code})")
                if not response.is_success:
                    raise FhirError(f"the FHIR server answered HTTP {response.status_code}")
                kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if kind not in _JSON_TYPES:
                    raise FhirError("the FHIR server did not answer with JSON")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise FhirError(f"the FHIR response is larger than {MAX_BYTES} bytes")
        except httpx.HTTPError as error:
            raise FhirError(f"could not reach the FHIR server ({type(error).__name__})") from None
        try:
            data = json.loads(bytes(body))
        except ValueError:
            raise FhirError("the FHIR server answered with invalid JSON") from None
        if not isinstance(data, Mapping):
            raise FhirError("the FHIR server answered with something other than a resource")
        return dict(data)


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
