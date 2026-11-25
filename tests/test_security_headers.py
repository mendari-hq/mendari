# SPDX-License-Identifier: Apache-2.0
"""Security headers on every response (server/app.py), and a CSP on every page that allows
exactly that page's own inline scripts and nothing from another origin."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari.server.app import SECURITY_HEADERS, VENDOR_DIR, create_app
from tests.conftest import encrypt_mode

VENDORED = ("swagger-ui-bundle.js", "swagger-ui.css")  # what the reference page loads


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(db_path=tmp_path / "a.db"))


def test_every_response_carries_the_headers(client: TestClient) -> None:
    for path in ("/", "/api/health", "/api/agents", "/api/audit/nope"):
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
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    refused = TestClient(create_app(db_path=tmp_path / "a.db")).get("/api/audit")
    assert refused.status_code == 401
    assert refused.headers["x-frame-options"] == "DENY"


# An attribute that loads or links something: it must stay on this origin (a path, not a URL).
_LINKED = re.compile(r"""\b(?:src|href)=(["'])(.*?)\1""", re.I)
_OFF_ORIGIN = re.compile(r"^\s*(?:[a-z][a-z0-9+.-]*:|//|\\\\)", re.I)


def _html_responses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Every kind of HTML Mendari answers with: the dashboard, the developer landing, the API
    reference (also reached from ReDoc's old address), the error page of a failed sign-in or
    SMART launch, and, in memberships mode, the invitation page."""
    client = TestClient(create_app(db_path=tmp_path / "a.db"))
    pages = {path: client.get(path) for path in ("/", "/docs/reference", "/docs", "/redoc")}
    pages["/smart/callback"] = client.get("/smart/callback")
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    signed = TestClient(create_app(db_path=tmp_path / "b.db"))
    pages["/auth/callback"] = signed.get("/auth/callback")
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    members = TestClient(create_app(db_path=tmp_path / "c.db"))
    pages["/invite"] = members.get("/invite")
    return pages


def test_every_html_response_has_a_csp_and_loads_nothing_off_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The API reference used to be FastAPI's: Swagger UI and ReDoc from a CDN, unpinned, with
    no CSP. Now no page is exempt, and none names another origin."""
    pages = _html_responses(tmp_path, monkeypatch)
    for path, response in pages.items():
        assert response.headers["content-type"].startswith("text/html"), path
        policy = response.headers["content-security-policy"]
        assert response.headers["cross-origin-embedder-policy"] == "require-corp", path
        directives = dict(d.split(" ", 1) for d in policy.split("; "))
        assert directives["default-src"] == "'self'" and directives["object-src"] == "'none'"
        assert directives["frame-ancestors"] == "'none'" and directives["connect-src"] == "'self'"
        assert "unsafe" not in directives["script-src"], path
        inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", response.text, re.S)
        for script in inline:  # each inline script by its hash, and nothing else inline
            digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
            assert f"'sha256-{digest}'" in directives["script-src"], path
        linked = [m[2] for m in _LINKED.finditer(response.text)]
        assert linked and not [url for url in linked if _OFF_ORIGIN.match(url)], path
    assert "launch failed" in pages["/smart/callback"].text  # the error page, both times
    assert "launch failed" in pages["/auth/callback"].text


def test_the_api_reference_is_served_from_vendored_files(client: TestClient) -> None:
    """Swagger UI at the version vendored (scripts/vendor_docs_ui.py), with a policy stricter
    than the dashboard's: no inline script and no inline style at all."""
    moved = client.get("/redoc", follow_redirects=False)  # FastAPI's ReDoc page was here
    assert (moved.status_code, moved.headers["location"]) == (308, "/docs/reference")
    assert moved.headers["content-security-policy"]  # the headers on redirects too
    page = client.get("/docs/reference")
    assert page.headers["cache-control"] == "no-cache"
    policy = page.headers["content-security-policy"]
    assert "script-src 'self';" in policy and "style-src 'self';" in policy
    assert "unsafe" not in policy
    version = json.loads((VENDOR_DIR / "swagger-ui" / "manifest.json").read_text())["version"]
    assets = re.findall(r'(?:src|href)="(/static/[^"]+)"', page.text)
    vendored = [f"/static/vendor/swagger-ui/{version}/{name}" for name in VENDORED]
    assert sorted(a for a in assets if "/vendor/" in a) == sorted(vendored)
    init = next(a for a in assets if a.startswith("/static/docs/reference.js?v="))
    script = client.get(init)
    assert "javascript" in script.headers["content-type"] and "validatorUrl: null" in script.text
    assert "cache-control" not in script.headers  # stamped by content: revalidated, not pinned
    for url in vendored:
        served = client.get(url)
        assert served.status_code == 200, url
        assert served.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert served.headers["cross-origin-resource-policy"] == "same-origin"
    assert client.get("/docs/oauth2-redirect").status_code == 404  # FastAPI's, gone with it
    unversioned = client.get("/static/vendor/swagger-ui/manifest.json")
    assert "immutable" not in unversioned.headers.get("cache-control", "")
