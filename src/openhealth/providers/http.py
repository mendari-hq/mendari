"""HTTP for remote providers: HTTPS only, bounded responses, audit-safe error codes.

Remote error bodies are never surfaced (they can echo inputs or credentials); a failure is
one of `rate_limited`, `credential_unavailable`, `provider_unavailable`, `timeout` or
`invalid_response`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import httpx

from openhealth.errors import ProviderError

MAX_RESPONSE_BYTES = 65_536
_LOCAL_HOST = re.compile(r"^(localhost|127\.0\.0\.1|\[::1\]|::1)$")


def require_secure(endpoint: str, service: str) -> None:
    """https, or plain http to this machine only (local model servers)."""
    parsed = urlparse(endpoint)
    if parsed.scheme == "https" or (
        parsed.scheme == "http" and _LOCAL_HOST.match(parsed.hostname or "")
    ):
        return
    raise ProviderError(
        "invalid_endpoint", f"{service} endpoints must use https (http only for localhost)"
    )


def is_local(endpoint: str) -> bool:
    return bool(_LOCAL_HOST.match(urlparse(endpoint).hostname or ""))


def post_json(
    client: httpx.Client | None,
    endpoint: str,
    body: Mapping[str, Any],
    *,
    headers: Mapping[str, str],
    timeout_s: float,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> Any:
    """POST `body` and return the parsed JSON response, or raise an audit-safe ProviderError."""
    timeout = httpx.Timeout(timeout_s)
    own = client is None
    session = client or httpx.Client(timeout=timeout, follow_redirects=False)
    try:
        with session.stream(
            "POST", endpoint, json=body, headers=dict(headers), timeout=timeout
        ) as response:
            if response.status_code == 429:
                raise ProviderError("rate_limited")
            if response.status_code in (401, 402, 403):
                raise ProviderError("credential_unavailable", f"HTTP {response.status_code}")
            if not response.is_success:
                raise ProviderError("provider_unavailable", f"HTTP {response.status_code}")
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise ProviderError("invalid_response", "response too large")
                chunks.append(chunk)
    except httpx.TimeoutException:
        raise ProviderError("timeout") from None
    except httpx.HTTPError as error:
        raise ProviderError("provider_unavailable", type(error).__name__) from None
    finally:
        if own:
            session.close()
    try:
        return json.loads(b"".join(chunks))
    except ValueError:
        raise ProviderError("invalid_response", "body is not JSON") from None
