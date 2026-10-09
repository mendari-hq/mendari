# SPDX-License-Identifier: Apache-2.0
"""The registry's rate limits and security headers (LP-18f), asserted by exact value on every
route."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from mendari.errors import MendariError
from mendari.hub.registry import (
    REGISTRY_CSP,
    RegistryStore,
    create_registry_app,
    registry_limit_settings,
)
from tests.conftest import REGISTRY_ADMIN

EXPECTED = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "content-security-policy": (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
    "cache-control": "no-store",
}


def _example(path: str) -> str:
    return (
        path.replace("{namespace}", "mendari")
        .replace("{name}", "referral-router")
        .replace("{version}", "0.0.1")
    )


def test_every_registry_route_answers_with_the_headers(tmp_path: Path) -> None:
    """On every route, as every method it takes, answered or refused: the exact values, and
    HSTS but on this machine."""
    app = create_registry_app(tmp_path / "r.db", admin_token=REGISTRY_ADMIN)
    with TestClient(app, base_url="https://hub.example.org") as remote:
        local = TestClient(app, base_url="http://localhost")
        seen = 0
        for route in app.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in route.methods:
                answer = remote.request(method, _example(route.path))
                for name, value in EXPECTED.items():
                    assert answer.headers.get(name) == value, (method, route.path, name)
                assert answer.headers["strict-transport-security"] == "max-age=63072000"
                assert (
                    "strict-transport-security"
                    not in local.request(method, _example(route.path)).headers
                )
                seen += 1
    assert seen >= 15
    assert EXPECTED["content-security-policy"] == REGISTRY_CSP


def test_writes_are_limited_per_token_before_their_body_is_read(
    db_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_REGISTRY_RATE_LIMITS", "on,registry_write=2/h:2")
    app = create_registry_app(db_target, admin_token=REGISTRY_ADMIN)
    with TestClient(app) as client:
        admin = {"authorization": f"Bearer {REGISTRY_ADMIN}"}
        for name in ("a", "b"):
            assert (
                client.post("/v1/namespaces", json={"name": name}, headers=admin).status_code == 201
            )
        refused = client.post("/v1/namespaces", json={"name": "c"}, headers=admin)
        assert refused.status_code == 429 and int(refused.headers["retry-after"]) >= 1
        assert refused.headers["x-frame-options"] == "DENY"
        # Another token has its own bucket; a refused write stored and chained nothing.
        other = {"authorization": "Bearer ohr_someone_else"}
        assert client.post("/v1/namespaces", json={"name": "d"}, headers=other).status_code == 401
    with RegistryStore(db_target) as store, store._db.session(exclusive=False) as db:
        names = {row["name"] for row in db.all("SELECT name FROM hub_namespaces")}
    assert "c" not in names and {"a", "b"} <= names


def test_reads_are_limited_per_address_and_log_mode_never_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_REGISTRY_RATE_LIMITS", "on,registry_read=3/min:3")
    with TestClient(create_registry_app(tmp_path / "a.db")) as client:
        codes = [client.get("/v1/health").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    monkeypatch.setenv("MENDARI_REGISTRY_RATE_LIMITS", "log,registry_read=3/min:3")
    with TestClient(create_registry_app(tmp_path / "b.db")) as client:
        assert {client.get("/v1/health").status_code for _ in range(5)} == {200}


@pytest.mark.parametrize("value", ["maybe", "registry_write=often", "decide=10/min"])
def test_a_bad_setting_stops_the_registry(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("MENDARI_REGISTRY_RATE_LIMITS", value)
    with pytest.raises(MendariError, match="MENDARI_REGISTRY_RATE_LIMITS"):
        registry_limit_settings()


def test_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MENDARI_REGISTRY_RATE_LIMITS", raising=False)
    settings: Any = registry_limit_settings()
    assert settings.mode == "on"
    assert {name: str(rate) for name, rate in settings.rates.items()} == {
        "registry_read": "600/min:200",
        "registry_write": "120/h:30",
        "registry_report": "60/h:10",
    }
