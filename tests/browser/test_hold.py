# SPDX-License-Identifier: Apache-2.0
"""Automation holds in the dashboard: the overview says when one covers the agent, and the
Decision Card lists them, holds in one click and lifts through a dialog that asks first (the API
is answered here, so the shared server's decisions never change)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect

from tests.browser.conftest import CLINICAL, TRIAGE
from tests.browser.test_dashboard import open_agent

pytestmark = pytest.mark.browser

HOLD = {
    "id": "abc123",
    "org": "default",
    "agent_id": TRIAGE,
    "agent_digest": None,
    "reason": "a missed emergency, checking",
    "set_by": "ops@clinic.example.org",
    "set_at": "2026-10-06T12:00:00Z",
}


def holds(page: Page, listed: list[dict[str, Any]], sent: list[tuple[str, str]]) -> None:
    def answer(route: Route) -> None:
        request = route.request
        sent.append((request.method, request.url))
        if request.method == "GET":
            route.fulfill(body=json.dumps(listed), content_type="application/json")
        elif request.method == "POST":
            route.fulfill(status=201, body=json.dumps(HOLD), content_type="application/json")
        else:
            route.fulfill(status=204)

    page.route("**/api/holds**", answer)


def test_the_overview_says_a_hold_covers_the_agent(page: Page) -> None:
    holds(page, [HOLD], [])
    open_agent(page, TRIAGE)
    expect(page.locator(".hold-notice")).to_contain_text("Automation is on hold.")
    holds(page, [{**HOLD, "agent_id": CLINICAL}], [])
    open_agent(page, TRIAGE)
    expect(page.locator(".hold-notice")).to_have_count(0)


def lifts(sent: list[tuple[str, str]]) -> bool:
    return any(m == "DELETE" and u.endswith("/api/holds/abc123") for m, u in sent)


def test_the_card_holds_and_lifts(page: Page) -> None:
    sent: list[tuple[str, str]] = []
    holds(page, [HOLD], sent)
    open_agent(page, TRIAGE, "card")
    section = page.locator("#card-approval")
    expect(section).to_contain_text("a missed emergency, checking")
    # Lifting raises automation again: it asks first, and Cancel keeps the hold.
    section.get_by_role("button", name="Lift").click()
    dialog = page.get_by_role("dialog", name="Lift this automation hold?")
    expect(dialog).to_contain_text("decides on its own again")
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_have_count(0)
    assert not lifts(sent)
    expect(section).to_contain_text("a missed emergency, checking")
    # Confirmed, it is lifted.
    section.get_by_role("button", name="Lift").click()
    dialog.get_by_role("button", name="Lift", exact=True).click()
    expect(page.locator("#toast")).to_have_text("Hold lifted")
    # Exactly one: the confirmed lift. By now that one is answered, so a lift Cancel sent before
    # it would be recorded too (the check right after Cancel can run before one arrives).
    assert [m for m, _ in sent if m == "DELETE"] == ["DELETE"] and lifts(sent)
    page.get_by_label("Reason").fill("checking a new version")
    section.get_by_role("button", name="Hold automation for this agent").click()
    expect(page.locator("#toast")).to_have_text("Automation held: a person reviews every decision")
    assert any(m == "POST" for m, _ in sent)
