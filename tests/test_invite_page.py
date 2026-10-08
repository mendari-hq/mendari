# SPDX-License-Identifier: Apache-2.0
"""The invitation page (`/invite`, LP-12f-ii): served in memberships mode only, public and
static, never a redirect, with a policy of its own; the token in its link's fragment is taken
out of the address by its first script, before anything else loads, and never reaches the
server's logs. The dashboard's roles are the server's. The page in a real browser:
tests/browser/test_invitations.py."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mendari.core import accounts
from mendari.core.audit import AuditStore
from mendari.server.app import DASHBOARD_DIR, create_app
from mendari.server.auth import permission_for
from tests.conftest import ROOT, encrypt_mode, member, security_lines

HTTPS = "https://mendari.test"
TOKEN = re.compile(r"ohi_[A-Za-z0-9_-]{43}")
INLINE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


def signing_in(monkeypatch: pytest.MonkeyPatch, *, accounts_mode: str | None) -> None:
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari-org")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    if accounts_mode is not None:
        monkeypatch.setenv("MENDARI_ACCOUNTS", accounts_mode)


@pytest.fixture
def members(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    signing_in(monkeypatch, accounts_mode="memberships")
    with TestClient(create_app(db_path=tmp_path / "a.db"), base_url=HTTPS) as client:
        yield client


def test_the_invite_page_is_public_static_and_never_redirects(
    members: TestClient, tmp_path: Path
) -> None:
    """Never `page`: a signed-out browser sent to sign in would carry the link's fragment through
    every redirect after it, to the identity provider (RFC 9110 §10.2.2)."""
    assert permission_for("GET", "/invite") == "public"
    with AuditStore(tmp_path / "a.db") as store:
        signed = member(store, org=None, sub="ada", email="ada@example.org")
    for headers in ({}, {"cookie": "__Host-mendari_session=stale"}, signed):
        for path in ("/invite", "/invite?from=mail"):
            page = members.get(path, headers=headers, follow_redirects=False)
            assert page.status_code == 200, (path, headers)
            assert page.headers["content-type"].startswith("text/html")
            assert "location" not in page.headers and "set-cookie" not in page.headers
    assert members.get("/invite").text == members.get("/invite?x=1").text  # static


def test_the_invite_page_has_its_own_policy(members: TestClient) -> None:
    page = members.get("/invite")
    assert page.headers["cache-control"] == "no-store"
    directives = dict(d.split(" ", 1) for d in page.headers["content-security-policy"].split("; "))
    scripts = INLINE.findall(page.text)
    assert len(scripts) == 3  # the token's capture, the theme and the import map
    hashes = [
        f"'sha256-{base64.b64encode(hashlib.sha256(s.encode()).digest()).decode()}'"
        for s in scripts
    ]
    assert directives["script-src"] == " ".join(["'self'", *hashes])
    assert "unsafe" not in page.headers["content-security-policy"]
    assert directives["style-src"] == "'self'" and ' style="' not in page.text
    assert directives["frame-ancestors"] == "'none'" and directives["default-src"] == "'self'"
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert '<meta name="referrer" content="no-referrer">' in page.text


def test_the_fragment_leaves_the_address_before_anything_loads(members: TestClient) -> None:
    """The first script on the page takes the token: before the stylesheet, the icons, the
    import map or any module, and into this tab's sessionStorage only."""
    text = members.get("/invite").text
    head = text[: text.index("</head>")]
    capture = head.index("<script>")
    loads = [m.start() for m in re.finditer(r"<link |<script type=|<script src=", text)]
    assert loads and capture < min(loads)
    first = INLINE.findall(text)[0]
    assert 'history.replaceState(null, "", location.pathname)' in first
    assert first.index("history.replaceState(") < first.index("sessionStorage.setItem(")
    assert "localStorage" not in first and "cookie" not in first


def test_the_invite_page_loads_only_its_own_modules(members: TestClient) -> None:
    text = members.get("/invite").text
    found = re.search(r'<script type="importmap">(.*?)</script>', text)
    assert found
    stamped = json.loads(found[1])["imports"]
    assert sorted(stamped) == ["/static/invite.js", "/static/theme.js", "/static/ui.js"]
    assert '<script type="module" src="/static/invite.js?v=' in text


@pytest.mark.parametrize("mode", ["open", "claims"])
def test_the_invite_page_is_served_only_in_memberships_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    if mode == "claims":
        signing_in(monkeypatch, accounts_mode=None)
    with TestClient(create_app(db_path=tmp_path / "a.db"), base_url=HTTPS) as client:
        missing = client.get("/invite", follow_redirects=False)
        assert missing.status_code == 404 and "location" not in missing.headers
        assert "/invite" not in client.get("/docs").text  # the landing lists what is mounted


def test_the_invite_page_is_never_logged_and_holds_no_token(
    members: TestClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with AuditStore(tmp_path / "a.db") as store:
        lead = member(store, org="clinic-a", roles=["admin"], sub="lead", email="lead@example.org")
    made = members.post(
        "/api/auth/invitations",
        json={"email": "ada@example.org", "roles": ["viewer"]},
        headers=lead,
    )
    assert made.status_code == 201 and TOKEN.search(made.json()["link"])
    capsys.readouterr()
    for headers in ({}, lead):
        assert members.get("/invite", headers=headers).status_code == 200
        assert not TOKEN.search(members.get("/invite", headers=headers).text)
    lines = security_lines(capsys.readouterr().out)
    assert [line for line in lines if "/invite" in json.dumps(line)] == []


def test_the_developer_landing_lists_the_invite_page_as_public(members: TestClient) -> None:
    text = members.get("/docs").text
    public = re.search(r'<tr data-permission="public">(.*?)</tr>', text, re.S)
    assert public and "<code>GET /invite</code>" in public[1]


def test_the_machine_roles_and_docs_site_in_ui_js_are_the_servers() -> None:
    """The roles the Access view offers a key or a client (LP-12h) are the server's machine
    roles, and preset documentation links point at the docs site mkdocs builds."""
    from mendari.core.audit import MACHINE_ROLES

    ui = (DASHBOARD_DIR / "ui.js").read_text(encoding="utf-8")
    chosen = re.search(r"MACHINE_ROLES = MEMBERSHIP_ROLES\.filter\(\(r\) => \[([^\]]*)\]", ui)
    assert chosen is not None
    assert re.findall(r'"([a-z_]+)"', chosen[1]) == list(MACHINE_ROLES)
    site = re.search(r'^export const DOCS_SITE = "([^"]+)";$', ui, re.M)
    mkdocs = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    site_url = re.search(r"^site_url: (\S+)$", mkdocs, re.M)
    assert site is not None and site_url is not None and site[1] == site_url[1]


def test_the_dashboard_role_list_is_the_servers() -> None:
    ui = (DASHBOARD_DIR / "ui.js").read_text(encoding="utf-8")
    assert re.findall(r'\{ key: "([a-z_]+)"', ui) == list(accounts.MEMBERSHIP_ROLES)
    for path in [*DASHBOARD_DIR.glob("*.js"), *DASHBOARD_DIR.glob("*.html")]:
        assert not re.search(r"\bgrading\b", path.read_text(encoding="utf-8")), path.name
