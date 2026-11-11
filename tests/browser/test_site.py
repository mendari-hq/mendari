# SPDX-License-Identifier: Apache-2.0
"""The public page (web/) in a real browser: its scores table on a phone (LP-17g). The page is
served from the repository's web/ directory on a routed origin; nothing is deployed, and every
request to any other origin is refused."""

from __future__ import annotations

import mimetypes
import re
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


def scores(page: Page, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 800})
    page.goto(f"{ORIGIN}/")
    page.locator("#scores table").wait_for()


@pytest.mark.parametrize("width", [320, 390])
def test_the_page_never_scrolls_sideways_on_a_phone(site: Page, width: int) -> None:
    """WCAG 1.4.10 (reflow): at 320 CSS px, and at a common phone's 390, the page is no wider
    than the screen. The score table scrolls in its own region, which clips everything it holds,
    the visually hidden header text ("Automated") included."""
    scores(site, width)
    assert site.evaluate("document.documentElement.scrollWidth") == width
    site.evaluate("window.scrollTo(100, 0)")
    assert site.evaluate("window.scrollX") == 0


def test_each_scores_row_header_stays_in_view_as_its_figures_scroll(site: Page) -> None:
    """On a phone the figures do not all fit, so they scroll sideways in their region; each row's
    header (its arm and configuration) stays at the region's left edge, and the last column, a
    critical label's figure, comes fully into view beside it."""
    scores(site, 390)
    region = site.get_by_role("region", name=re.compile("^Scores for the"))
    assert region.evaluate("(el) => el.scrollWidth > el.clientWidth")
    header = region.locator("tbody th").first
    assert header.evaluate("(th) => getComputedStyle(th).position") == "sticky"
    region.evaluate("(el) => { el.scrollLeft = el.scrollWidth; }")
    area, held = region.bounding_box(), header.bounding_box()
    last = region.locator("thead th").last.bounding_box()
    assert area is not None and held is not None and last is not None
    assert abs(held["x"] - area["x"]) < 1  # held at the left edge
    assert last["x"] >= held["x"] + held["width"] - 1  # not under the held header
    assert last["x"] + last["width"] <= area["x"] + area["width"] + 1  # in view


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
