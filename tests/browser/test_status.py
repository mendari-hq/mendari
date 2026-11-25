# SPDX-License-Identifier: Apache-2.0
"""The status page (/status, LP-21b): it shows this deployment's readiness, the outage when the
database does not answer, and nothing answering at all; axe in both themes, and a phone's width."""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect

from tests.browser.test_accessibility import SCHEMES, violations
from tests.browser.test_dashboard import PHONE

pytestmark = pytest.mark.browser

READY = "**/api/health/ready"
OUTAGE = {
    "status": "unready",
    "version": "0.1.0",
    "checks": {"database": "unreachable", "agents": "ok"},
}


def answer(status: int, body: dict[str, Any]) -> Any:
    def fulfil(route: Route) -> None:
        route.fulfill(status=status, json=body)

    return fulfil


def test_the_status_page_shows_a_working_deployment(page: Page) -> None:
    page.goto("/status")
    expect(page.locator("[data-status]")).to_have_attribute("data-status", "ready")
    expect(page.locator("#status-body")).to_contain_text("Working.")
    expect(page.locator('[data-check="database"]')).to_have_text("Answering")
    expect(page.locator('[data-check="agents"]')).to_have_text("Loaded")


def test_the_status_view_shows_a_database_outage(page: Page) -> None:
    page.route(READY, answer(503, OUTAGE))
    page.goto("/status")
    expect(page.locator("[data-status]")).to_have_attribute("data-status", "unready")
    expect(page.locator(".callout.danger")).to_contain_text("Not working.")
    expect(page.locator('[data-check="database"]')).to_have_text("Not answering")
    expect(page.locator('[data-check="agents"]')).to_have_text("Loaded")


def test_the_status_view_says_when_nothing_answers(page: Page) -> None:
    page.route(READY, lambda route: route.abort())
    page.goto("/status")
    expect(page.locator(".callout.danger")).to_contain_text("Not answering.")
    expect(page.locator('[data-check="database"]')).to_have_text("Unknown")


def test_the_status_view_checks_again(page: Page) -> None:
    calls: list[str] = []

    def count(route: Route) -> None:
        calls.append(route.request.url)
        if len(calls) > 1:  # the database goes after the first check
            route.fulfill(status=503, json=OUTAGE)
        else:
            route.continue_()

    page.clock.install()
    page.route(READY, count)
    page.goto("/status")
    expect(page.locator("[data-status]")).to_have_attribute("data-status", "ready")
    page.clock.run_for(31_000)
    expect(page.locator("[data-status]")).to_have_attribute("data-status", "unready")
    assert len(calls) >= 2


@pytest.mark.parametrize("axe_page", SCHEMES, indirect=True)
@pytest.mark.parametrize("state", ["ready", "unready"])
def test_the_status_page_passes_axe(axe_page: Page, state: str) -> None:
    if state == "unready":
        axe_page.route(READY, answer(503, OUTAGE))
    axe_page.goto("/status")
    expect(axe_page.locator("[data-status]")).to_have_attribute("data-status", state)
    assert violations(axe_page) == []


def test_the_status_page_fits_a_phone(page: Page) -> None:
    page.set_viewport_size(PHONE)
    page.route(READY, answer(503, OUTAGE))
    page.goto("/status")
    expect(page.locator("[data-status]")).to_have_attribute("data-status", "unready")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
