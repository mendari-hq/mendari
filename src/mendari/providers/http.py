# SPDX-License-Identifier: Apache-2.0
"""HTTP for remote providers: HTTPS only, bounded responses, audit-safe error codes.

Remote error bodies are never surfaced (they can echo inputs or credentials); a failure is
one of `rate_limited`, `credential_unavailable`, `insufficient_credit`,
`provider_unavailable`, `timeout` or `invalid_response`.

Inside a decision, every attempt waits at most what remains of its deadline
(`providers/deadline.py`): its timeout is clamped to it, no retry is waited past it, and a
response read in chunks stops at it (`timeout`). Inside a decision or not, an attempt ends at its
own timeout as a whole, never only at each read's.

A deployment may also list the origins its remote providers may be called at
(`MENDARI_PROVIDER_ALLOWED_ORIGINS`, comma-separated, read as `MENDARI_FHIR_ALLOWED_SERVERS` is).
Unset, nothing more is checked than before: every endpoint comes only from the deployment's own
variables (`core/spec.py` `DEPLOYMENT_ENV_KEYS`). Set, a request to any other origin (the AI
Gateway's default, Jev's model list and Vercel Connect included) is refused before a connection
is opened (`invalid_endpoint`), and a server whose `LLM_BASE_URL` or `JEV_API_URL` names one
does not start (`providers.provider_allow_list`).
"""

from __future__ import annotations

import atexit
import email.utils
import json
import math
import os
import re
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from mendari.errors import MendariError, ProviderError, ProviderErrorCode
from mendari.providers import deadline
from mendari.standards.fhir_client import allowed_servers, origin_of

MAX_RESPONSE_BYTES = 65_536
_LOCAL_HOST = re.compile(r"^(localhost|127\.0\.0\.1|\[::1\]|::1)\Z")
ALLOWED_ORIGINS_ENV = "MENDARI_PROVIDER_ALLOWED_ORIGINS"
_ORIGINAL_CLIENT = httpx.Client
_POOL_LOCK = threading.Lock()
_POOLED_CLIENT: httpx.Client | None = None


def _shared_client() -> httpx.Client:
    """Thread-safe pooled HTTP client for production calls (reuses TCP/TLS sessions)."""
    global _POOLED_CLIENT
    with _POOL_LOCK:
        if _POOLED_CLIENT is None or _POOLED_CLIENT.is_closed:
            _POOLED_CLIENT = _ORIGINAL_CLIENT(
                follow_redirects=False,
                limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
            )
        return _POOLED_CLIENT


def close_shared_client() -> None:
    global _POOLED_CLIENT
    with _POOL_LOCK:
        if _POOLED_CLIENT is not None and not _POOLED_CLIENT.is_closed:
            _POOLED_CLIENT.close()
        _POOLED_CLIENT = None


atexit.register(close_shared_client)


def require_secure(endpoint: str, service: str) -> None:
    """https, or plain http to this machine only (local model servers), and, when the
    deployment lists the origins its providers may be called at, one of them
    (`require_allowed`)."""
    require_https(endpoint, service)
    require_allowed(endpoint, service)


def require_https(endpoint: str, service: str) -> None:
    """https, or plain http to this machine only (local model servers)."""
    try:
        parsed = urlparse(endpoint)
        host = parsed.hostname or ""
    except ValueError:
        raise ProviderError("invalid_endpoint", f"{service} endpoint is not a valid URL") from None
    if parsed.username or parsed.password:
        raise ProviderError(
            "invalid_endpoint", f"{service} endpoint must not carry credentials in its address"
        )
    if (parsed.scheme == "https" and host) or (
        parsed.scheme == "http" and _LOCAL_HOST.fullmatch(host)
    ):
        return
    raise ProviderError(
        "invalid_endpoint", f"{service} endpoints must use https (http only for localhost)"
    )


def allowed_origins() -> frozenset[str] | None:
    """The origins remote providers may be called at (`MENDARI_PROVIDER_ALLOWED_ORIGINS`), or
    None when it is unset or blank: no allow-list, as before. A value that is not a list of
    origins is a `MendariError` naming the variable; one that lists none allows none."""
    if not os.environ.get(ALLOWED_ORIGINS_ENV, "").strip():
        return None
    return allowed_servers(ALLOWED_ORIGINS_ENV)


def require_allowed(url: str, service: str) -> None:
    """Nothing when the deployment lists no origins, or lists `url`'s; `invalid_endpoint`
    otherwise, before any credential is read or connection opened. A malformed list, which
    stops a server at start-up, refuses every call (a command reads it only when it calls)."""
    try:
        allowed = allowed_origins()
    except MendariError:
        raise ProviderError("invalid_endpoint", f"{ALLOWED_ORIGINS_ENV} is malformed") from None
    if allowed is not None and origin_of(url) not in allowed:
        problem = f"{service} endpoint is not on this deployment's provider allow-list"
        raise ProviderError("invalid_endpoint", problem)


def require_listed(variable: str, allowed: frozenset[str]) -> None:
    """At start-up: a `MendariError` naming `variable`, an endpoint variable of the deployment,
    when it is set and its origin is not in `allowed`. Its value is never shown."""
    value = os.environ.get(variable, "").strip()
    if value and origin_of(value) not in allowed:
        raise MendariError(
            f"{variable} is not on {ALLOWED_ORIGINS_ENV}: list its origin there, or unset it"
        )


def is_local(endpoint: str) -> bool:
    try:
        return bool(_LOCAL_HOST.fullmatch(urlparse(endpoint).hostname or ""))
    except ValueError:
        return False


def post_json(
    client: httpx.Client | None,
    endpoint: str,
    body: Mapping[str, Any],
    *,
    headers: Mapping[str, str],
    timeout_s: float,
    max_bytes: int = MAX_RESPONSE_BYTES,
    retries: int = 0,
    allowed: bool = False,
) -> Any:
    """POST `body` and return the parsed JSON response, or raise an audit-safe ProviderError.

    `retries` extra attempts are made after `429 Too Many Requests` or `529 Overloaded`, after
    the server's `Retry-After` when it is short, else a brief exponential backoff. A longer
    `Retry-After` is not waited out: a decision fails safely instead of stalling. Nor is one
    that would end at or after the decision's deadline, whose remaining time also bounds each
    attempt's timeout (`timeout` once it has passed). Off the deployment's provider allow-list,
    when it has one, nothing is opened (`invalid_endpoint`; each caller checks before its key).
    `allowed` is the one exception, an agent's connection whose origin an operator allowed for
    it (`providers/agent.py` `_secure`, which read the list and gives the client that opens
    public addresses only): the list is not asked again here.
    """
    if not allowed:
        require_allowed(endpoint, "Provider")
    timeout = httpx.Timeout(deadline.clamp(timeout_s))  # past the deadline: nothing is opened
    use_pool = client is None and httpx.Client is _ORIGINAL_CLIENT
    own = client is None and not use_pool
    session = (
        client
        if client is not None
        else (
            _shared_client() if use_pool else httpx.Client(timeout=timeout, follow_redirects=False)
        )
    )
    try:
        for attempt in range(retries + 1):
            try:
                return _post_once(session, endpoint, body, headers, timeout_s, max_bytes)
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
        raw = headers.get(name)
        if raw is None:
            continue
        try:
            value = float(raw) * scale
        except ValueError:
            if name == "retry-after":
                try:
                    parsed_dt = email.utils.parsedate_to_datetime(raw)
                except (TypeError, ValueError):
                    continue
                if parsed_dt.tzinfo is None:
                    parsed_dt = parsed_dt.replace(tzinfo=UTC)
                value = (parsed_dt - datetime.now(UTC)).total_seconds()
            else:
                continue
        if math.isfinite(value):
            return max(0.0, value)
    return None


def _post_once(
    session: httpx.Client,
    endpoint: str,
    body: Mapping[str, Any],
    headers: Mapping[str, str],
    timeout_s: float,
    max_bytes: int,
) -> Any:
    """One attempt, which ends at its timeout (or what remains of the deadline) as a whole:
    httpx's timeout bounds each read, so an answer arriving a little inside each wait would
    otherwise hold the caller as long as the server likes."""
    allowed = deadline.clamp(timeout_s)
    timeout = httpx.Timeout(allowed)
    stop_at = time.monotonic() + allowed
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
                if time.monotonic() > stop_at:  # and at its own timeout
                    raise ProviderError("timeout", "the answer took longer than its timeout")
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
    except (ValueError, RecursionError):
        raise ProviderError("invalid_response", "body is not JSON") from None
