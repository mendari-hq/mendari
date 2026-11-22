# SPDX-License-Identifier: Apache-2.0
"""The public page (web/) in a real browser: it reflows on a phone (LP-17g). The page is
served from the repository's web/ directory on a routed origin; nothing is deployed, and every
request to any other origin is refused."""

from __future__ import annotations

import mimetypes
from collections.abc import Iterator

import pytest
from playwright.sync_api import Browser, Page, Route

from tests.conftest import ROOT

pytestmark = pytest.mark.browser

WEB = (ROOT / "web").resolve()
ORIGIN = "https://mendari.test"


def _serve(route: Route) -> None:
    """A file of web/ by its path, as a static host serves it; anything else is refused."""
    url = route.request.url
    if not url.startswith(f"{ORIGIN}/"):
        route.abort()
        return
    path = url.removeprefix(ORIGIN).split("?")[0].split("#")[0].lstrip("/")
    file = (WEB / (path or "index.html")).resolve()
    if WEB not in file.parents or not file.is_file():
        route.fulfill(status=404, body="")
        return
    kind = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
    route.fulfill(status=200, body=file.read_bytes(), content_type=kind)


@pytest.fixture
def site(browser: Browser) -> Iterator[Page]:
    context = browser.new_context()
    context.route("**/*", _serve)
    page = context.new_page()
    yield page
    context.close()


def home(page: Page, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 800})
    page.goto(f"{ORIGIN}/")
    page.locator("main").wait_for()


@pytest.mark.parametrize("width", [320, 390])
def test_the_page_never_scrolls_sideways_on_a_phone(site: Page, width: int) -> None:
    """WCAG 1.4.10 (reflow): at 320 CSS px, and at a common phone's 390, the page is no wider
    than the screen."""
    home(site, width)
    assert site.evaluate("document.documentElement.scrollWidth") == width
    site.evaluate("window.scrollTo(100, 0)")
    assert site.evaluate("window.scrollX") == 0


@pytest.mark.parametrize("name", ["terms", "privacy", "acceptable-use"])
@pytest.mark.parametrize("width", [320, 390])
def test_a_legal_page_never_scrolls_sideways_on_a_phone(site: Page, name: str, width: int) -> None:
    """The draft legal pages reflow as the home page does, their longest placeholder included,
    and each shows its draft marker before its first section."""
    site.set_viewport_size({"width": width, "height": 800})
    site.goto(f"{ORIGIN}/{name}.html")
    marker = site.locator("main .draft")
    marker.wait_for()
    assert site.evaluate("document.documentElement.scrollWidth") == width
    first_section = site.locator("main h2").first
    assert marker.bounding_box()["y"] < first_section.bounding_box()["y"]  # type: ignore[index]
