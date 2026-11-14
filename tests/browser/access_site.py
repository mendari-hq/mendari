# SPDX-License-Identifier: Apache-2.0
"""A real Mendari server for the Access view's browser tests (tests/browser/test_invitations.py,
tests/browser/test_credentials.py): served at `localhost` (a secure context, so its Secure
cookies stay), with sessions planted as `/auth/callback` makes them (`tests.conftest.member`),
and the helpers both files use to reach the view. Every address is at example.org; all data is
synthetic."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, BrowserContext, Locator, Page, expect

from mendari.core.audit import AuditStore
from mendari.server.app import create_app
from tests.browser.conftest import serving
from tests.conftest import encrypt_mode, member
from tests.test_auth import CLIENT, IDP


@dataclass
class Site:
    """Mendari in memberships mode at `localhost` (a secure context: its Secure cookies stay), its
    store, and the browser windows a test opens (closed with it)."""

    base: str
    store: AuditStore
    browser: Browser
    contexts: list[BrowserContext] = field(default_factory=list)

    def window(self, headers: dict[str, str] | None = None, **options: Any) -> Page:
        """A new browser (context) holding the session in `headers`, if any."""
        context = self.browser.new_context(base_url=self.base, **options)
        self.contexts.append(context)
        if headers:
            name, _, value = headers["cookie"].partition("=")
            context.add_cookies(
                [
                    {
                        "name": name,
                        "value": value,
                        "url": self.base,
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "Lax",
                    }
                ]
            )
        return context.new_page()


@contextmanager
def running(browser: Browser, db: Path) -> Iterator[Site]:
    """The server, with the environment the caller set (memberships mode and sign-in; the pack
    registry too, with `MENDARI_REGISTRY=on`, its store closed with the server)."""
    app = create_app(db_path=db)
    try:
        with AuditStore(db) as store, serving(app) as port:
            site = Site(f"http://localhost:{port}", store, browser)
            try:
                yield site
            finally:
                for context in site.contexts:
                    context.close()
    finally:
        registry = getattr(app.state, "registry_store", None)
        if registry is not None:
            registry.close()


def memberships(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)  # reached only by the real sign-in's test
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", CLIENT)
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off


def lead(site: Site) -> dict[str, str]:
    """Clinic-A's administrator (Clinic-A's record too, made by the first call)."""
    return member(
        site.store, org="clinic-a", roles=["admin"], sub="lead", email="lead@example.org",
        host="localhost",
    )  # fmt: skip


def person(
    site: Site,
    sub: str = "ada",
    email: str | None = "ada@example.org",
    org: str | None = None,
    roles: list[str] | None = None,
) -> dict[str, str]:
    """Someone signed in (of no organization unless `org`), their address verified unless None."""
    return member(site.store, org=org, roles=roles, sub=sub, email=email, host="localhost")


def access(page: Page, tab: str) -> None:
    """The Access view's `tab`, freshly loaded."""
    page.goto("about:blank")
    page.goto(f"/#admin/{tab}")
    expect(page.locator("main h1")).to_have_text("Access")
    expect(page.locator(f'.tabs a[data-tab="{tab}"]')).to_have_attribute("aria-current", "page")


def row(page: Page, table: str, text: str) -> Locator:
    return page.locator(f'[data-table="{table}"] tbody tr', has_text=text)


def dialog(page: Page) -> Locator:
    return page.locator("dialog[open]")
