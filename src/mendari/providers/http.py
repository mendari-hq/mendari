# SPDX-License-Identifier: Apache-2.0
"""HTTP for remote providers: HTTPS only, bounded responses, audit-safe error codes.

Remote error bodies are never surfaced (they can echo inputs or credentials); a failure is
one of `rate_limited`, `credential_unavailable`, `insufficient_credit`,
`provider_unavailable`, `timeout` or `invalid_response`, and a request JSON cannot carry is
`invalid_input`, sent nowhere.

Inside a decision, every attempt waits at most what remains of its deadline
(`providers/deadline.py`): its timeout is clamped to it, and no retry is waited past it
(`timeout`). Inside a decision or not, an attempt ends at its own timeout as a whole, never only
at each step's: httpx bounds each step (connecting, the TLS handshake, each write and each read)
by a timeout of its own, so on the clients Mendari calls with each step waits at most what
remains of the attempt (`Budgeted`), and on any client a response read in chunks stops at its
end. Not bounded, as before: looking a name up, and a name of the deployment's with several
addresses, which is tried at each with what remained when connecting began.

A deployment may also list the origins its remote providers may be called at
(`MENDARI_PROVIDER_ALLOWED_ORIGINS`, comma-separated, read as `MENDARI_FHIR_ALLOWED_SERVERS` is).
Unset, nothing more is checked than before: every endpoint comes only from the deployment's own
variables (`core/spec.py` `DEPLOYMENT_ENV_KEYS`). Set, a request to any other origin (the AI
Gateway's default included) is refused before a connection is opened (`invalid_endpoint`), and a
server whose `LLM_BASE_URL` or `MENDARI_AGENT_URL` names one does not start
(`providers.provider_allow_list`).
"""

from __future__ import annotations

import atexit
import email.utils
import json
import math
import os
import re
import ssl
import threading
import time
from collections.abc import Iterable, Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpcore
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
# When the attempt under way ends (`time.monotonic`), for the steps of its connection to wait at
# most until (`Budgeted`); None outside one. Per thread: one attempt per connection at a time.
_ATTEMPT_ENDS: ContextVar[float | None] = ContextVar("attempt_ends", default=None)


def _within(timeout: float | None, late: type[Exception]) -> float | None:
    """`timeout`, or what remains of the attempt under way when that is less; `late` (httpcore's
    timeout of the step) once nothing remains, so that no step starts after the attempt ends."""
    ends = _ATTEMPT_ENDS.get()
    if ends is None:
        return timeout
    left = ends - time.monotonic()
    if left <= 0:
        raise late("the attempt's time is up")
    return left if timeout is None else min(timeout, left)


class Budgeted(httpcore.SyncBackend):
    """httpcore's network backend, each step of a connection held to the attempt under way
    (`_post_once`): connecting, the TLS handshake, each write and each read wait at most what
    remains of it, and none starts after it. httpx's timeout bounds each step on its own, so a
    server that answers a little inside each wait (its headers, then its body) could otherwise
    hold an attempt for as many timeouts as it takes steps. The clients Mendari calls with
    connect through it: the shared one (`_shared_client`) and `reach.public_client`."""

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        stream = super().connect_tcp(
            host,
            port,
            timeout=_within(timeout, httpcore.ConnectTimeout),
            local_address=local_address,
            socket_options=socket_options,
        )
        return _BudgetedStream(stream)


class _BudgetedStream(httpcore.NetworkStream):
    """A connection's stream, each step held to the attempt under way (`Budgeted`)."""

    def __init__(self, stream: httpcore.NetworkStream):
        self._stream = stream

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._stream.read(max_bytes, _within(timeout, httpcore.ReadTimeout))

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._stream.write(buffer, _within(timeout, httpcore.WriteTimeout))

    def close(self) -> None:
        self._stream.close()

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        timeout = _within(timeout, httpcore.ConnectTimeout)
        return _BudgetedStream(self._stream.start_tls(ssl_context, server_hostname, timeout))

    def get_extra_info(self, info: str) -> Any:
        return self._stream.get_extra_info(info)


def _shared_client() -> httpx.Client:
    """Thread-safe pooled HTTP client for production calls (reuses TCP/TLS sessions). It reads
    a proxy from the environment as httpx does, and every pool it connects through, its own and
    a proxy's, holds each attempt to its time (`Budgeted`): httpx takes no network backend, so
    each pool is given one after it is built (`reach` builds its own transport's pool)."""
    global _POOLED_CLIENT
    with _POOL_LOCK:
        if _POOLED_CLIENT is None or _POOLED_CLIENT.is_closed:
            _POOLED_CLIENT = _ORIGINAL_CLIENT(
                follow_redirects=False,
                limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
            )
            for transport in (_POOLED_CLIENT._transport, *_POOLED_CLIENT._mounts.values()):
                pool = getattr(transport, "_pool", None)  # None: a pattern with no proxy
                if pool is not None:
                    pool._network_backend = Budgeted()
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
    public addresses only): the list is not asked again here. A `body` JSON cannot carry (NaN
    or an infinity, a string that is not UTF-8) is not sent either (`invalid_input`).
    """
    if not allowed:
        require_allowed(endpoint, "Provider")
    content = _encoded(body)
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
                return _post_once(session, endpoint, content, headers, timeout_s, max_bytes)
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


def _encoded(body: Mapping[str, Any]) -> bytes:
    """`body` as the JSON a request carries, encoded as httpx encodes `json=`, or
    `invalid_input` when it holds what JSON cannot carry (NaN or an infinity, a string that is
    not UTF-8 text, values nested too deep), which httpx would refuse with a raw error. Never
    the value in the message: it can be anything the input held."""
    try:
        text = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        return text.encode("utf-8")
    except (ValueError, RecursionError):  # UnicodeEncodeError is a ValueError
        raise ProviderError(
            "invalid_input", "the request holds a value JSON cannot carry"
        ) from None


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
                except (TypeError, ValueError, OverflowError):  # no date: as if it were absent
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
    content: bytes,
    headers: Mapping[str, str],
    timeout_s: float,
    max_bytes: int,
) -> Any:
    """One attempt, which ends at its timeout (or what remains of the deadline) as a whole:
    httpx's timeout bounds each step on its own, so an answer arriving a little inside each wait
    would otherwise hold the caller as long as the server likes. Each step of a connection the
    attempt makes through `Budgeted` waits at most until its end, and each chunk of the answer
    is checked against it, on any client."""
    allowed = deadline.clamp(timeout_s)
    timeout = httpx.Timeout(allowed)
    stop_at = time.monotonic() + allowed
    attempt = _ATTEMPT_ENDS.set(stop_at)
    try:
        with session.stream(
            "POST",
            endpoint,
            content=content,
            headers={**headers, "content-type": "application/json"},
            timeout=timeout,
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
    finally:
        _ATTEMPT_ENDS.reset(attempt)
    try:
        return json.loads(b"".join(chunks))
    except (ValueError, RecursionError):
        raise ProviderError("invalid_response", "body is not JSON") from None
