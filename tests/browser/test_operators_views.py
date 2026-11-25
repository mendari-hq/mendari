# SPDX-License-Identifier: Apache-2.0
"""Memberships mode leaves the deployment's views to its operator (LP-12g, ADR-0008, owner
decision 5): an organization's administrator is not offered the whole chain's verification,
the Decision Card says the deployment's records are the operator's, and a hold set for every
organization shows what it covers, not the operator's reason. On a real memberships server."""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Browser, expect

from tests.browser.access_site import lead, memberships, running
from tests.browser.conftest import TRIAGE
from tests.browser.test_dashboard import open_agent

pytestmark = pytest.mark.browser


def test_an_organizations_administrator_sees_the_deployments_views_as_the_operators(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memberships(monkeypatch)
    with running(browser, tmp_path / "mendari.db") as site:
        headers = lead(site)
        site.store.hold_automation(
            org=None, agent_id=TRIAGE, agent_digest=None, reason="operator drill", set_by="ops"
        )
        page = site.window(headers)
        asked: list[str] = []
        page.on("request", lambda request: asked.append(request.url))
        open_agent(page, TRIAGE)
        expect(page.locator(".hold-notice")).to_contain_text("Automation is on hold.")
        open_agent(page, TRIAGE, "audit")
        expect(page.locator("#audit-table")).to_be_visible()
        expect(page.locator("#verify")).to_have_count(0)
        open_agent(page, TRIAGE, "card")
        expect(page.locator("#card-deployments")).to_contain_text(
            "Kept by the deployment's operator"
        )
        held = page.locator("#card-approval .callout.danger")
        expect(held).to_contain_text("set by the deployment's operator")
        expect(held).not_to_contain_text("operator drill")
        expect(held.get_by_role("button", name="Lift")).to_have_count(0)
        assert not [url for url in asked if "/api/audit/verify" in url or "/api/deployments" in url]
