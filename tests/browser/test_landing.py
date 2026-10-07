# SPDX-License-Identifier: Apache-2.0
"""The developer landing (/docs) in a real browser: both themes under its strict CSP with nothing
from another origin, copy buttons that copy and say so, the keyboard path through it, its
headings, landmarks and names, and a phone's width.

Skipped by the default `pytest` run; run with `uv run pytest -m browser`.
"""

from __future__ import annotations

from itertools import pairwise

import pytest
from playwright.sync_api import BrowserContext, Page, expect

from mendari.server import snippets
from tests.browser import test_dashboard as dashboard
from tests.browser import test_docs as reference

pytestmark = pytest.mark.browser

# The page's own colours, from style.css's tokens (--color-bg, --color-text).
BACKGROUND = {"light": "rgb(245, 246, 248)", "dark": "rgb(14, 19, 26)"}
TEXT = {"light": "rgb(23, 32, 44)", "dark": "rgb(230, 235, 242)"}
STEP = {step.name: step for step in snippets.load("quickstart")}


@pytest.fixture
def watched(page: Page, live_server: str) -> dict[str, list[str]]:
    """Every request, console error and CSP violation of the page, from before its scripts run."""
    seen: dict[str, list[str]] = {"requests": [], "errors": []}
    page.on("request", lambda request: seen["requests"].append(request.url))
    page.on("console", lambda msg: seen["errors"].append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda error: seen["errors"].append(str(error)))
    page.add_init_script(reference.WATCH_CSP)
    return seen


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_landing_renders_in_both_themes_with_nothing_from_elsewhere(
    page: Page, live_server: str, watched: dict[str, list[str]], scheme: str
) -> None:
    page.emulate_media(color_scheme=scheme)  # type: ignore[arg-type]
    page.goto("/docs")
    expect(page.locator("html")).to_have_attribute("data-theme", scheme)  # "System", the default
    expect(page.get_by_role("heading", level=1)).to_have_text("Mendari for developers")
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == BACKGROUND[scheme]
    name = page.locator(".docs-diagram text.name").first
    assert name.evaluate("(el) => getComputedStyle(el).fill") == TEXT[scheme]
    # docs.js ran: the examples name this server, and the copy buttons are offered.
    expect(page.locator("[data-origin]")).to_have_text(live_server)
    expect(page.locator("button[data-copy]").first).to_be_visible()
    chips = page.evaluate(dashboard.CONTRAST, ".docs-outcomes .badge, .docs-step-number")
    assert len(chips) > 4 and [c for c in chips if c[1] < 4.5] == []
    assert page.evaluate("window.__violations") == []
    assert [url for url in watched["requests"] if not url.startswith(f"{live_server}/")] == []
    assert watched["errors"] == []


def test_the_theme_toggle_switches_and_is_kept(page: Page) -> None:
    page.emulate_media(color_scheme="light")
    page.goto("/docs")
    dark = page.get_by_role("button", name="Dark")
    dark.click()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    expect(dark).to_have_attribute("aria-pressed", "true")
    page.reload()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    page.goto("/")  # the dashboard shares the choice
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")


def test_a_copy_button_copies_its_example_and_says_so(
    page: Page, context: BrowserContext, live_server: str
) -> None:
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=live_server)
    page.goto("/docs")
    decide = page.get_by_role("button", name=f"Copy: {STEP['decide'].title}")
    decide.click()
    expect(decide).to_contain_text("Copied")
    expect(page.locator("#announce-status")).to_have_text("Copied to the clipboard")
    assert page.evaluate("navigator.clipboard.readText()") == STEP["decide"].text
    page.get_by_role("button", name="Copy: name the server and your key").click()
    copied = page.evaluate("navigator.clipboard.readText()")
    assert copied.startswith(f"export MENDARI={live_server} ")  # this server, ready to paste
    expect(decide).to_contain_text("Copy", timeout=5000)  # and back again


def test_the_keyboard_skips_to_the_content_and_reaches_every_copy_button_in_order(
    page: Page,
) -> None:
    page.goto("/docs")
    page.keyboard.press("Tab")
    skip = page.get_by_role("link", name="Skip to content")
    expect(skip).to_be_focused()
    expect(skip).to_be_in_viewport()
    page.keyboard.press("Enter")
    expect(page.locator("main")).to_be_focused()
    copies = page.locator("button[data-copy]")
    buttons = copies.evaluate_all("(all) => all.map((button) => button.dataset.copy)")
    reached: list[str] = []
    for _ in range(400):
        page.keyboard.press("Tab")
        focused = page.evaluate(
            "[document.activeElement.dataset.copy || null, "
            "getComputedStyle(document.activeElement).outlineStyle]"
        )
        if focused[0]:
            assert focused[1] != "none", focused[0]  # a visible focus ring
            reached.append(focused[0])
        if reached == buttons:
            break
    assert reached == buttons
    # Each example scrolls sideways on its own, so it takes focus too.
    expect(page.locator("pre[tabindex='0']")).to_have_count(len(buttons) + 1)  # + what prints


def test_headings_landmarks_and_names(page: Page) -> None:
    page.goto("/docs")
    levels = page.locator("h1, h2, h3, h4").evaluate_all("(all) => all.map((h) => +h.tagName[1])")
    assert levels[0] == 1 and levels.count(1) == 1
    assert all(b - a <= 1 for a, b in pairwise(levels)), levels
    for role, name in [("banner", None), ("main", None), ("contentinfo", None)]:
        expect(page.get_by_role(role, name=name)).to_have_count(1)  # type: ignore[arg-type]
    expect(page.get_by_role("navigation", name="On this page")).to_be_visible()
    expect(page.get_by_role("img", name="How a decision is made")).to_be_visible()
    expect(page.locator("ol.docs-flow li")).to_have_count(6)  # the picture, in words
    unnamed = page.evaluate(
        """() => [...document.querySelectorAll('a, button')].filter((el) =>
          !(el.getAttribute('aria-label') || el.textContent.trim())).map((el) => el.outerHTML)"""
    )
    assert unnamed == []


def test_a_phone_never_scrolls_sideways(page: Page) -> None:
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto("/docs")
    width = page.evaluate("document.documentElement.scrollWidth")
    assert width <= 375
    # A long example scrolls inside its own box instead.
    code = page.locator("#code-quickstart-decide").locator("xpath=..")
    assert code.evaluate("(pre) => pre.scrollWidth > pre.clientWidth")
    expect(page.get_by_role("button", name="Dark")).to_be_visible()
