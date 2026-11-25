# SPDX-License-Identifier: Apache-2.0
"""Alerts to the operator's webhook (`MENDARI_ALERT_WEBHOOK`, LP-21): rendered, signed, sent,
retried and recorded in one place, for the monitor and the quota alert alike.

**What leaves.** Ids, counts and Mendari's own messages: an agent's id, an organization's id, an
alert's kind, severity and message. Never case text, a remote error body, the webhook's URL or
the signing secret.

**Formats** (`MENDARI_ALERT_FORMAT`):
- `mendari` (the default): JSON with `source`, `delivery` (this alert's id), `event`
  (`monitor.alerts`, `monitor.incomplete`, `quota.reached`, `audit.broken`, or `alerts.test`
  from `mendari alerts test`), `organization` and `alerts` (each sender's own list);
- `slack`: a Slack incoming webhook's `{"text": ...}`, a heading and one line per alert.

**The test alert** (`send_test`, `mendari alerts test`): one `alerts.test` with no alerts in
it, rendered, signed and posted as every alert is, tried once and recorded nowhere, so the
operator can check the webhook after setting it.

**Signature** (`MENDARI_ALERT_SECRET`, optional, at least 32 characters): every post carries
`Mendari-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256 of "<t>." + the body>`, beside
`Mendari-Delivery` (its id) and `Mendari-Event`. A receiver recomputes the HMAC over the raw
body, compares it in constant time and refuses an old `t` (docs/deploying-with-phi.md, *Alerts*).

**Retries.** A post is tried up to `attempts` times, RETRY_DELAYS apart: no answer (a network
error, a timeout), `408`, `429` and `5xx` are tried again; any other answer is final, and a
redirect is never followed.

**Records** (`alert_deliveries`, outside the chain). Given a store, each alert is recorded with
its body (ids and counts, as sent), its status (`delivered`; `pending` while another try is due;
`failed` once a final answer came or MAX_ATTEMPTS were made), its attempts, the last HTTP status
and a fixed error code (`http_<status>`, or the error's type). `retry_due` sends the pending
ones again: the scheduled monitor (`GET /api/internal/monitor`, LP-21f) calls it first, and
`mendari monitor`, which records nothing, does not. The URL and the secret are never stored.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from mendari.errors import MendariError

if TYPE_CHECKING:
    from mendari.core.audit import AuditStore

FORMATS = ("mendari", "slack")
MIN_SECRET = 32
# Waits before the second and third tries of one send; the schedule tries a pending one again.
RETRY_DELAYS = (2.0, 8.0)
MAX_ATTEMPTS = 5
RETRY_AFTER = timedelta(minutes=10)
TEST_EVENT = "alerts.test"
TEST_LINE = "a test alert from `mendari alerts test`: nothing to do"
alerts_logger = logging.getLogger("mendari.alerts")
_sleep: Callable[[float], None] = time.sleep  # replaced in tests


@dataclass(frozen=True)
class AlertSettings:
    webhook: str
    format: str = "mendari"
    secret: str | None = None


@dataclass(frozen=True)
class Delivery:
    """What one send came to: the alert's id, the tries made, the last HTTP status (None
    without an answer) and the fixed error code (None: delivered). Never the URL or the body."""

    id: str
    attempts: int
    status: int | None
    error: str | None

    @property
    def delivered(self) -> bool:
        return self.error is None


def alert_settings(webhook: str | None = None) -> AlertSettings | None:
    """The webhook (`webhook`, else $MENDARI_ALERT_WEBHOOK; None without one), its format and
    its signing secret. A format Mendari does not render, or a secret shorter than MIN_SECRET or
    with spaces around it, fails here: a server at startup, a command before any work. A webhook
    without a secret is allowed, with a warning: its receiver cannot tell Mendari's posts."""
    chosen = os.environ.get("MENDARI_ALERT_FORMAT", "").strip().lower() or "mendari"
    if chosen not in FORMATS:
        raise MendariError(f"MENDARI_ALERT_FORMAT must be one of {', '.join(FORMATS)}")
    secret = os.environ.get("MENDARI_ALERT_SECRET", "")
    if secret and (len(secret) < MIN_SECRET or secret != secret.strip()):
        raise MendariError(
            f"MENDARI_ALERT_SECRET must be at least {MIN_SECRET} random characters, with no "
            "spaces around them"
        )
    url = (webhook or os.environ.get("MENDARI_ALERT_WEBHOOK", "")).strip()
    if not url:
        return None
    if not secret:
        alerts_logger.warning(
            "MENDARI_ALERT_WEBHOOK is set without MENDARI_ALERT_SECRET: alerts are not signed, "
            "so their receiver cannot tell them from anyone else's posts"
        )
    return AlertSettings(webhook=url, format=chosen, secret=secret or None)


def check_webhook(url: str) -> None:
    """Alerts may leave only over https (or http to this machine or a compose service), never
    with credentials in the address, and only to an address the client can parse (a mistyped
    port fails here, never when an alert is due). The URL is never repeated: it holds a token."""
    import re

    from mendari.core.https_only import is_local_host

    try:
        httpx.URL(url)
    except httpx.InvalidURL:
        raise MendariError("the alert webhook is not a valid URL") from None
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host:
        raise MendariError("the alert webhook is not a valid URL")
    # Plaintext http is only for loopback or a compose service DNS label (never decimal IPv4/IPv6).
    compose_service = bool(re.fullmatch(r"[a-z][a-z0-9-]{0,62}", host))
    local = is_local_host(host) or compose_service
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise MendariError("the alert webhook must use https")
    if parts.username or parts.password:
        raise MendariError("the alert webhook must not carry credentials in its address")


def signature(secret: str, body: bytes, at: int) -> str:
    """The `Mendari-Signature` header for `body` sent at `at` (unix seconds)."""
    mac = hmac.new(secret.encode(), f"{at}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={at},v1={mac}"


def render(
    chosen: str,
    *,
    delivery: str,
    event: str,
    org: str | None,
    alerts: Sequence[Any],
    lines: Sequence[str],
) -> bytes:
    """The body of one alert in a format: `alerts` for `mendari`, `lines` for `slack`."""
    if chosen == "slack":
        where = f" for {org}" if org else ""
        text = "\n".join([f"Mendari: {event}{where}", *lines])
        escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return json.dumps({"text": escaped}).encode()
    body = {
        "source": "mendari",
        "delivery": delivery,
        "event": event,
        "organization": org,
        "alerts": list(alerts),
    }
    return json.dumps(body).encode()


def deliver(
    settings: AlertSettings,
    *,
    event: str,
    org: str | None,
    alerts: Sequence[Any],
    lines: Sequence[str],
    attempts: int,
    timeout: float,
    store: AuditStore | None = None,
) -> bool:
    """Send one alert, trying up to `attempts` times; record it when given a store. Whether it
    was delivered. Never raises for a webhook that fails: a failure is logged (its code and the
    alert's id) and, with a store, left `pending` for `retry_due` while tries remain."""
    sent = send(
        settings,
        event=event,
        org=org,
        alerts=alerts,
        lines=lines,
        attempts=attempts,
        timeout=timeout,
        store=store,
    )
    return sent.delivered


def send(
    settings: AlertSettings,
    *,
    event: str,
    org: str | None,
    alerts: Sequence[Any],
    lines: Sequence[str],
    attempts: int,
    timeout: float,
    store: AuditStore | None = None,
) -> Delivery:
    """`deliver`, saying what the send came to rather than only whether it was delivered."""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    delivery = uuid.uuid4().hex
    body = render(
        settings.format, delivery=delivery, event=event, org=org, alerts=alerts, lines=lines
    )
    status, error, tried = None, None, 0
    for tried in range(1, attempts + 1):
        if tried > 1:
            _sleep(RETRY_DELAYS[min(tried - 2, len(RETRY_DELAYS) - 1)])
        status, error = _post(settings, delivery, event, body, timeout)
        if error is None or not _again(status, error):
            break
    if error is not None:
        alerts_logger.warning("an alert to the webhook failed (%s), delivery %s", error, delivery)
    if store is not None:
        now = datetime.now(UTC)
        store.record_alert(
            {
                "id": delivery,
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "event": event,
                "org": org,
                "body": body.decode(),
                "attempts": tried,
                **_outcome(status, error, tried, now),
            }
        )
    return Delivery(id=delivery, attempts=tried, status=status, error=error)


def send_test(
    settings: AlertSettings,
    *,
    timeout: float = 10.0,
    store: AuditStore | None = None,
    line: str = TEST_LINE,
) -> Delivery:
    """One test alert (TEST_EVENT, no alerts in it) in the settings' format, signed with their
    secret, tried once. Recorded nowhere from the command line (`mendari alerts test`). Given a
    store (the System view's test, `POST /api/system/alerts/test`), it is recorded as delivered
    or, when the webhook did not take it, as failed: never pending, so no later run sends a
    test again."""
    sent = send(
        settings,
        event=TEST_EVENT,
        org=None,
        alerts=[],
        lines=[line],
        attempts=1,
        timeout=timeout,
        store=store,
    )
    if store is not None and not sent.delivered:
        store.update_alert(
            sent.id,
            status="failed",
            attempts=sent.attempts,
            updated_at=datetime.now(UTC).isoformat(),
            next_attempt_at=None,
            last_status=sent.status,
            last_error=sent.error,
        )
    return sent


def retry_due(
    store: AuditStore,
    settings: AlertSettings,
    *,
    timeout: float = 10.0,
    limit: int = 50,
    deadline: float | None = None,
) -> dict[str, int]:
    """Send again each `pending` alert whose next try is due, once each, as it was rendered;
    how many were delivered, how many are still to come or failed for good, and how many were
    left for the next run (`deferred`): none is started once `deadline` (a `time.monotonic()`
    value) has passed, and those left stay due."""
    now = datetime.now(UTC)
    counts = {"delivered": 0, "pending": 0, "failed": 0, "deferred": 0}
    for row in store.alerts_due(now.isoformat(), limit):
        if deadline is not None and time.monotonic() >= deadline:
            counts["deferred"] += 1
            continue
        status, error = _post(settings, row["id"], row["event"], row["body"].encode(), timeout)
        tried = int(row["attempts"]) + 1
        outcome = _outcome(status, error, tried, now)
        store.update_alert(row["id"], attempts=tried, updated_at=now.isoformat(), **outcome)
        counts[outcome["status"]] += 1
        if error is not None:
            alerts_logger.warning(
                "an alert to the webhook failed (%s), delivery %s", error, row["id"]
            )
    return counts


def _post(
    settings: AlertSettings, delivery: str, event: str, body: bytes, timeout: float
) -> tuple[int | None, str | None]:
    """One post: its HTTP status (None without an answer) and an error code (None: delivered).
    The code is fixed text, never the answer's body or the URL."""
    headers = {
        "content-type": "application/json",
        "mendari-delivery": delivery,
        "mendari-event": event,
    }
    if settings.secret is not None:
        headers["mendari-signature"] = signature(settings.secret, body, int(time.time()))
    try:
        response = httpx.post(
            settings.webhook, content=body, headers=headers, timeout=timeout, follow_redirects=False
        )
    except (httpx.HTTPError, httpx.InvalidURL) as failure:
        return None, type(failure).__name__
    if 200 <= response.status_code < 300:
        return response.status_code, None
    return response.status_code, f"http_{response.status_code}"


def _again(status: int | None, error: str | None) -> bool:
    """Whether a try is worth another: no answer (but an address the client cannot parse, which
    no try fixes), a timeout, too many, a 5xx."""
    if error == "InvalidURL":
        return False
    return status is None or status in (408, 429) or status >= 500


def _outcome(status: int | None, error: str | None, tried: int, now: datetime) -> dict[str, Any]:
    if error is None:
        state, due = "delivered", None
    elif _again(status, error) and tried < MAX_ATTEMPTS:
        state, due = "pending", (now + RETRY_AFTER).isoformat()
    else:
        state, due = "failed", None
    return {"status": state, "next_attempt_at": due, "last_status": status, "last_error": error}
