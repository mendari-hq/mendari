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


def test_try_it_asks_for_the_acknowledgement_before_the_form(page: Page) -> None:
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
    open_pack(page, INBOX, "try")
    asked = page.get_by_role("region", name="Before you try a pack")
    expect(asked).to_contain_text(notices.ACKNOWLEDGEMENT)
    expect(page.get_by_role("button", name="Decide")).to_have_count(0)
    page.get_by_role("button", name="I understand: synthetic data only").click()
    expect(page.get_by_role("button", name="Decide")).to_be_visible()
    assert state["posts"] == 1
