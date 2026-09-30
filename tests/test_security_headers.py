"""Security headers on every response (server/app.py), and a CSP that allows exactly the
dashboard's own inline scripts."""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openhealth.server.app import SECURITY_HEADERS, create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(db_path=tmp_path / "a.db"))


def test_every_response_carries_the_headers(client: TestClient) -> None:
    for path in ("/", "/api/health", "/api/packs", "/api/audit/nope"):
        response = client.get(path)
        for name, value in SECURITY_HEADERS.items():
            assert response.headers[name] == value, (path, name)
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_the_csp_allows_the_dashboards_inline_scripts_and_nothing_else(client: TestClient) -> None:
    response = client.get("/")
    policy = response.headers["content-security-policy"]
    script_src = next(p for p in policy.split("; ") if p.startswith("script-src"))
    inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", response.text, re.S)
    assert len(inline) == 2  # the theme script and the import map
    for script in inline:
        digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        assert f"'sha256-{digest}'" in script_src
    assert "'unsafe-inline'" not in script_src and "'unsafe-eval'" not in script_src
    assert "default-src 'self'" in policy and "object-src 'none'" in policy


def test_refusals_carry_the_headers_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENHEALTH_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("OPENHEALTH_OIDC_CLIENT_ID", "openhealth")
    refused = TestClient(create_app(db_path=tmp_path / "a.db")).get("/api/audit")
    assert refused.status_code == 401
    assert refused.headers["x-frame-options"] == "DENY"


def test_the_api_docs_keep_their_cdn(client: TestClient) -> None:
    docs = client.get("/docs")
    assert docs.status_code == 200 and "content-security-policy" not in docs.headers
    assert docs.headers["x-content-type-options"] == "nosniff"
