from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from openhealth.errors import ProviderError
from openhealth.providers.credentials import ConnectorToken, key_source, resolve_key

SECRET = "jev-secret-value"
OIDC = "oidc-identity-token"


class FakeConnect:
    """Vercel Connect's token endpoint."""

    def __init__(self, status: int = 200, body: Any = None):
        self.status = status
        self.body = body if body is not None else {"token": SECRET, "expiresAt": 4_102_444_800_000}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body)


def connector(fake: FakeConnect, clock: list[float]) -> ConnectorToken:
    return ConnectorToken(
        "jev/openhealth-hub",
        client=httpx.Client(transport=httpx.MockTransport(fake)),
        oidc=lambda: OIDC,
        clock=lambda: clock[0],
    )


def test_exchanges_the_deployment_identity_for_the_provider_key() -> None:
    fake = FakeConnect()
    assert connector(fake, [0.0]).get() == SECRET
    (request,) = fake.requests
    assert request.method == "POST"
    assert str(request.url) == "https://api.vercel.com/v1/connect/token/jev%2Fopenhealth-hub"
    assert request.headers["authorization"] == f"Bearer {OIDC}"
    assert json.loads(request.content) == {"subject": {"type": "app"}}


def test_key_is_cached_until_shortly_before_expiry() -> None:
    now = 1_790_000_000.0  # epoch seconds; Vercel reports expiresAt in epoch milliseconds
    fake = FakeConnect(body={"token": SECRET, "expiresAt": int((now + 1000) * 1000)})
    clock = [now]
    token = connector(fake, clock)
    token.get()
    clock[0] = now + 900
    token.get()
    assert len(fake.requests) == 1
    clock[0] = now + 941  # inside the 60 s refresh margin
    token.get()
    assert len(fake.requests) == 2


@pytest.mark.parametrize(
    "fake",
    [FakeConnect(status=403, body={"error": SECRET}), FakeConnect(body={"nope": True})],
)
def test_failures_are_credential_errors_without_secrets(fake: FakeConnect) -> None:
    with pytest.raises(ProviderError) as error:
        connector(fake, [0.0]).get()
    assert error.value.code == "credential_unavailable"
    assert SECRET not in str(error.value) and OIDC not in str(error.value)


def test_missing_oidc_token_is_a_credential_error() -> None:
    def no_oidc() -> str:
        raise ProviderError("credential_unavailable", "no Vercel OIDC token")

    token = ConnectorToken("jev/openhealth-hub", oidc=no_oidc)
    with pytest.raises(ProviderError, match="credential_unavailable"):
        token.get()


def test_explicit_key_wins_and_sources_never_expose_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JEV_API_KEY", SECRET)
    monkeypatch.setenv("JEV_CONNECTOR", "jev/openhealth-hub")
    assert resolve_key(key_env="JEV_API_KEY", connector_env="JEV_CONNECTOR") == SECRET
    assert key_source(key_env="JEV_API_KEY", connector_env="JEV_CONNECTOR") == "env:JEV_API_KEY"
    monkeypatch.delenv("JEV_API_KEY")
    assert (
        key_source(key_env="JEV_API_KEY", connector_env="JEV_CONNECTOR")
        == "vercel-connect:jev/openhealth-hub"
    )
    monkeypatch.delenv("JEV_CONNECTOR")
    assert key_source(key_env="JEV_API_KEY", connector_env="JEV_CONNECTOR") is None
    with pytest.raises(ProviderError, match="set JEV_API_KEY or JEV_CONNECTOR"):
        resolve_key(key_env="JEV_API_KEY", connector_env="JEV_CONNECTOR")
