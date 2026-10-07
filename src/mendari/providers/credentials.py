# SPDX-License-Identifier: Apache-2.0
"""Credentials for remote providers.

A provider key is resolved in this order:

1. An explicit environment variable, e.g. `JEV_API_KEY` (local development, tests).
2. A Vercel Connect connector named by another variable, e.g. `JEV_CONNECTOR=jev/my-project`.
   The deployment's Vercel OIDC token is exchanged for the provider key at Vercel's documented
   endpoint `POST https://api.vercel.com/v1/connect/token/{connector}` with subject `app`.
   Keys are cached in memory until shortly before they expire.

Secrets are never logged and never appear in error messages or audit records.

Which environment variables supply a provider's endpoint, model and key is fixed in code and
belongs to the deployment. A pack's provider config may not name them: a pack that could would
choose which process secret is sent, and to which URL (`core/spec.py`, `DEPLOYMENT_ENV_KEYS`).
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from urllib.parse import quote

import httpx

from mendari.errors import ProviderError

CONNECT_TOKEN_URL = "https://api.vercel.com/v1/connect/token/{connector}"
REFRESH_MARGIN_S = 60
DEFAULT_TTL_S = 300


def vercel_oidc_token() -> str:
    """The deployment's OIDC token (on Vercel), or the one `vercel env pull` wrote locally."""
    from vercel.oidc import VercelOidcTokenError, get_vercel_oidc_token_sync

    try:
        token = get_vercel_oidc_token_sync()
    except VercelOidcTokenError:
        token = ""
    if not token:
        raise ProviderError(
            "credential_unavailable",
            "no Vercel OIDC token: deploy on Vercel, or run `vercel env pull` for local use",
        )
    return token


def _deployment_oidc_token() -> str:
    return vercel_oidc_token()  # looked up when called, so a test suite can refuse it


class ConnectorToken:
    def __init__(
        self,
        connector: str,
        *,
        client: httpx.Client | None = None,
        oidc: Callable[[], str] = _deployment_oidc_token,
        clock: Callable[[], float] = time.time,
    ):
        self.connector = connector
        self._client = client
        self._oidc = oidc
        self._clock = clock
        self._lock = threading.Lock()
        self._token = ""
        self._expires_at = 0.0

    def get(self) -> str:
        with self._lock:
            if not self._token or self._clock() >= self._expires_at - REFRESH_MARGIN_S:
                self._token, self._expires_at = self._fetch()
            return self._token

    def _fetch(self) -> tuple[str, float]:
        url = CONNECT_TOKEN_URL.format(connector=quote(self.connector, safe=""))
        headers = {"authorization": f"Bearer {self._oidc()}"}
        client = self._client or httpx.Client(timeout=10, follow_redirects=False)
        try:
            response = client.post(url, json={"subject": {"type": "app"}}, headers=headers)
        except httpx.HTTPError as error:
            raise ProviderError(
                "credential_unavailable", f"Vercel Connect unreachable ({type(error).__name__})"
            ) from None
        finally:
            if self._client is None:
                client.close()
        if not response.is_success:
            raise ProviderError(
                "credential_unavailable",
                f"Vercel Connect refused {self.connector!r} (HTTP {response.status_code})",
            )
        try:
            body = response.json()
        except ValueError:
            body = None
        token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise ProviderError("credential_unavailable", "Vercel Connect returned no token")
        return token, self._expiry(body.get("expiresAt"))

    def _expiry(self, expires_at: object) -> float:
        if isinstance(expires_at, int | float) and not isinstance(expires_at, bool):
            # Vercel reports epoch milliseconds; accept seconds too.
            return expires_at / 1000 if expires_at > 1e11 else float(expires_at)
        return self._clock() + DEFAULT_TTL_S


_connectors: dict[str, ConnectorToken] = {}
_connectors_lock = threading.Lock()


def connector_token(connector: str) -> ConnectorToken:
    with _connectors_lock:
        if connector not in _connectors:
            _connectors[connector] = ConnectorToken(connector)
        return _connectors[connector]


def resolve_key(*, key_env: str, connector_env: str) -> str:
    key = os.environ.get(key_env, "").strip()
    if key:
        return key
    connector = os.environ.get(connector_env, "").strip()
    if connector:
        return connector_token(connector).get()
    raise ProviderError("credential_unavailable", f"set {key_env} or {connector_env}")


def key_source(*, key_env: str, connector_env: str) -> str | None:
    """Where a key would come from (for health checks). Never returns the key itself."""
    if os.environ.get(key_env, "").strip():
        return f"env:{key_env}"
    connector = os.environ.get(connector_env, "").strip()
    return f"vercel-connect:{connector}" if connector else None
