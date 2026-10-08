# SPDX-License-Identifier: Apache-2.0
"""Requests other sites start, in a real browser (Chromium's own Fetch Metadata and SameSite
rules): run with `uv run pytest -m browser`."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser

from mendari.core.audit import AuditStore
from mendari.server.app import create_app
from tests.browser.conftest import serving
from tests.conftest import encrypt_mode, security_lines, signed_in

pytestmark = pytest.mark.browser

ATTACKER = "http://attacker.test/"  # another site, answered by the test itself


@pytest.fixture
def signed_in_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, str, str]]:
    """Mendari with sign-in on, at `localhost` (a secure context, so its Secure cookies stay),
    and a reviewer's session: the base URL, the cookie's name and its value."""
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")  # never reached
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    db = tmp_path / "a.db"
    app = create_app(db_path=db)
    with AuditStore(db) as store:
        name, _, value = signed_in(store, role="reviewer", host="localhost")["cookie"].partition(
            "="
        )
    with serving(app) as port:
        yield f"http://localhost:{port}", name, value


def test_a_form_on_another_site_cannot_sign_you_out(
    browser: Browser, signed_in_site: tuple[str, str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """The browser leaves the SameSite=Lax session cookie off the cross-site POST; the answer
    must still not clear it."""
    base, name, value = signed_in_site
    context = browser.new_context()
    context.add_cookies(
        [
            {
                "name": name,
                "value": value,
                "url": base,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ]
    )
    page = context.new_page()
    form = f'<form id=f method=post action="{base}/auth/logout"></form><script>f.submit()</script>'
    page.route(ATTACKER, lambda route: route.fulfill(content_type="text/html", body=form))
    with page.expect_response(f"{base}/auth/logout") as answer:
        page.goto(ATTACKER)
    assert answer.value.status == 403
    refusals = [e for e in security_lines(capsys.readouterr().out) if e["event"] != "request"]
    assert [(e["event"], e["reason"]) for e in refusals] == [("auth.origin.refused", "cross_site")]
    assert [c["name"] for c in context.cookies(base)] == [name]
    page.goto(f"{base}/api/auth/me")
    assert '"signed_in":true' in page.inner_text("body")
    context.close()
