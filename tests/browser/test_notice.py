# SPDX-License-Identifier: Apache-2.0
"""The notice in a real browser (mendari.notices): on every view at every width, outside what a
drawer makes inert, and Try it asks a signed-in person to acknowledge it before the form."""

from __future__ import annotations

import json
from typing import Any

import pytest
from playwright.sync_api import Page, Route, ViewportSize, expect

from mendari import notices
from tests.browser.test_dashboard import INBOX, PHONE, VIEWS, open_pack, signed_in_as

pytestmark = pytest.mark.browser

DESKTOP: ViewportSize = {"width": 1280, "height": 900}


@pytest.mark.parametrize("size", [DESKTOP, PHONE], ids=["desktop", "phone"])
def test_every_view_shows_the_notice(page: Page, size: ViewportSize) -> None:
    page.set_viewport_size(size)
    for view in [*VIEWS, "monitoring"]:
        open_pack(page, INBOX, view)
        notice = page.locator(".preview-notice")
        expect(notice).to_have_text(notices.NOTICE)
        expect(notice).to_be_visible()
    page.goto("/docs")
    expect(page.locator(".preview-notice")).to_have_text(notices.NOTICE)


def test_a_signed_in_person_acknowledges_when_the_dashboard_opens(page: Page) -> None:
    signed_in_as(page, "reviewer")
    state = {"acknowledged": False, "posts": 0}

    def notice(route: Route) -> None:
        if route.request.method == "POST":
            state["posts"] += 1
            state["acknowledged"] = True
        body: dict[str, Any] = {
            **notices.notice(),
            "acknowledgement": notices.ACKNOWLEDGEMENT,
            "required": True,
            "acknowledged": state["acknowledged"],
        }
        route.fulfill(body=json.dumps(body), content_type="application/json")

    page.route("**/api/notice**", notice)
    page.add_init_script(f"localStorage.setItem('oh.pack', {json.dumps(INBOX)})")
    page.goto("/#try")  # the dashboard waits for the answer before it shows a view
    asked = page.get_by_role("dialog", name="Before you start")
    expect(asked).to_contain_text(notices.ACKNOWLEDGEMENT)
    page.keyboard.press("Escape")
    expect(asked).to_be_visible()  # Escape does not skip it
    asked.get_by_role("button", name="I understand: synthetic data only").click()
    expect(asked).to_have_count(0)
    expect(page.get_by_role("button", name="Decide")).to_be_visible()
    assert state["posts"] == 1


def test_an_open_drawer_says_it_too(page: Page) -> None:
    open_pack(page, INBOX, "audit")
    page.locator("table tbody tr").first.click()
    drawer = page.get_by_role("dialog")
    expect(drawer.locator(".drawer-notice")).to_have_text(notices.NOTICE)


def test_the_dialog_says_its_own_errors_and_lets_one_sign_out(page: Page) -> None:
    signed_in_as(page, "reviewer")

    def notice(route: Route) -> None:
        if route.request.method == "POST":
            route.fulfill(status=403, body=json.dumps({"detail": "Refused here."}))
            return
        body = {**notices.notice(), "acknowledgement": notices.ACKNOWLEDGEMENT, "required": True}
        route.fulfill(
            body=json.dumps({**body, "acknowledged": False}), content_type="application/json"
        )

    page.route("**/api/notice**", notice)
    page.add_init_script(f"localStorage.setItem('oh.pack', {json.dumps(INBOX)})")
    page.goto("/#overview")
    asked = page.get_by_role("dialog", name="Before you start")
    asked.get_by_role("button", name="I understand: synthetic data only").click()
    expect(asked.get_by_role("alert")).to_contain_text("Refused here.")
    expect(asked.get_by_role("button", name="Sign out")).to_be_visible()
