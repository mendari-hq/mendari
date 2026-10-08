# SPDX-License-Identifier: Apache-2.0
"""HTTP for remote providers: HTTPS only, bounded responses, audit-safe error codes.

Remote error bodies are never surfaced (they can echo inputs or credentials); a failure is
one of `rate_limited`, `credential_unavailable`, `insufficient_credit`,
`provider_unavailable`, `timeout` or `invalid_response`.

Inside a decision, every attempt waits at most what remains of its deadline
(`providers/deadline.py`): its timeout is clamped to it, no retry is waited past it, and a
response read in chunks stops at it (`timeout`).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import httpx

from mendari.errors import ProviderError, ProviderErrorCode
from mendari.providers import deadline

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
    retries: int = 0,
) -> Any:
    """POST `body` and return the parsed JSON response, or raise an audit-safe ProviderError.

    `retries` extra attempts are made after `429 Too Many Requests` or `529 Overloaded`, after
    the server's `Retry-After` when it is short, else a brief exponential backoff. A longer
    `Retry-After` is not waited out: a decision fails safely instead of stalling. Nor is one
    that would end at or after the decision's deadline, whose remaining time also bounds each
    attempt's timeout (`timeout` once it has passed).
    """
    timeout = httpx.Timeout(deadline.clamp(timeout_s))  # past the deadline: nothing is opened
    own = client is None
    session = client or httpx.Client(timeout=timeout, follow_redirects=False)
    try:
        for attempt in range(retries + 1):
            try:
                return _post_once(session, endpoint, body, headers, timeout, max_bytes)
            except _Retryable as retry:
                delay = retry.delay if retry.delay is not None else BACKOFF_S * 2**attempt
                left = deadline.remaining()
                if (
                    attempt == retries
                    or delay > MAX_RETRY_WAIT_S
                    or (left is not None and delay >= left)
                ):
                    raise retry.error from None
                _sleep(delay)
                timeout = httpx.Timeout(deadline.clamp(timeout_s))
        raise AssertionError("unreachable")  # pragma: no cover
    finally:
        if own:
            session.close()


RETRYABLE: dict[int, ProviderErrorCode] = {429: "rate_limited", 529: "provider_unavailable"}
BACKOFF_S = 0.5
MAX_RETRY_WAIT_S = 2.0
_sleep = time.sleep


class _Retryable(Exception):
    def __init__(self, error: ProviderError, delay: float | None):
        self.error, self.delay = error, delay


def _retry_after(headers: httpx.Headers) -> float | None:
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        try:
            return max(0.0, float(headers[name]) * scale)
        except (KeyError, ValueError):
            continue
    return None


def _post_once(
    session: httpx.Client,
    endpoint: str,
    body: Mapping[str, Any],
    headers: Mapping[str, str],
    timeout: httpx.Timeout,
    max_bytes: int,
) -> Any:
    try:
        with session.stream(
            "POST", endpoint, json=body, headers=dict(headers), timeout=timeout
        ) as response:
            status = response.status_code
            if status in RETRYABLE:
                error = ProviderError(RETRYABLE[status], f"HTTP {status}")
                raise _Retryable(error, _retry_after(response.headers))
            if status in (401, 403):
                raise ProviderError("credential_unavailable", f"HTTP {status}")
            if status == 402:  # e.g. the AI Gateway's credit balance is empty
                raise ProviderError("insufficient_credit")
            if not response.is_success:
                raise ProviderError("provider_unavailable", f"HTTP {status}")
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                deadline.refuse_if_past()  # a slow answer stops at the decision's deadline
                size += len(chunk)
                if size > max_bytes:
                    raise ProviderError("invalid_response", "response too large")
                chunks.append(chunk)
    except httpx.TimeoutException:
        raise ProviderError("timeout") from None
    except httpx.HTTPError as error:
        raise ProviderError("provider_unavailable", type(error).__name__) from None
    try:
        return json.loads(b"".join(chunks))
    except ValueError:
        raise ProviderError("invalid_response", "body is not JSON") from None
