"""Dashboard smoke tests in a real browser (Chromium).

Skipped by the default `pytest` run; run them with `uv run pytest -m browser` (first time:
`uv run playwright install chromium`).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from playwright.sync_api import Browser, Page, Route, ViewportSize, expect

pytestmark = pytest.mark.browser

INBOX = "openhealth/patient-inbox-router"
GUARD = "openhealth/ai-guard"
PACKS = [
    GUARD,
    "openhealth/clinical-trial-prescreener",
    INBOX,
    "openhealth/prior-auth-preflight",
    "openhealth/referral-router",
]
VIEWS = ["overview", "review", "audit", "evals", "feedback", "card", "try"]
PHONE: ViewportSize = {"width": 390, "height": 844}

# Form controls and buttons in the view with no accessible name.
UNNAMED = """() => [...document.querySelectorAll(
    'main input, main select, main textarea, main button')]
  .filter((el) => !(
    (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`)) || el.closest('label')
    || el.getAttribute('aria-label') || el.getAttribute('aria-labelledby')
    || (el.tagName === 'BUTTON' && el.textContent.trim())))
  .map((el) => el.outerHTML.slice(0, 90))"""

# WCAG contrast of an element's text colour against its own background colour.
CONTRAST = """(selector) => {
  const rgb = (c) => c.match(/[\\d.]+/g).slice(0, 3).map(Number);
  const lum = ([r, g, b]) => [r, g, b].map((v) => v / 255)
    .map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4))
    .reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i], 0);
  return [...document.querySelectorAll(selector)].map((el) => {
    const style = getComputedStyle(el);
    const [a, b] = [lum(rgb(style.color)), lum(rgb(style.backgroundColor))].sort((x, y) => y - x);
    return [el.textContent.trim(), (a + 0.05) / (b + 0.05)];
  });
}"""


@pytest.fixture
def console_errors(page: Page) -> list[str]:
    errors: list[str] = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda error: errors.append(str(error)))
    return errors


def show(page: Page, view: str) -> None:
    """Go to a view and wait until it has rendered (not just its loading placeholder)."""
    page.goto(f"/#{view}")
    expect(page.locator(f'.nav a[data-view="{view}"]')).to_have_class(re.compile("active"))
    expect(page.locator("main h1")).to_be_visible()
    expect(page.locator("main .empty", has_text="Loading")).to_have_count(0)


def open_pack(page: Page, pack_id: str, view: str = "overview") -> None:
    page.add_init_script(f"localStorage.setItem('oh.pack', {json.dumps(pack_id)})")
    show(page, view)


@pytest.mark.parametrize("pack_id", PACKS)
def test_every_view_renders_without_errors(
    page: Page, console_errors: list[str], pack_id: str
) -> None:
    open_pack(page, pack_id)
    for view in VIEWS:
        show(page, view)
        expect(page.locator("main")).not_to_contain_text("Could not load this view")
    assert console_errors == []


@pytest.mark.parametrize("pack_id", [INBOX, GUARD])
def test_no_view_scrolls_sideways_on_a_phone(page: Page, pack_id: str) -> None:
    page.set_viewport_size(PHONE)
    open_pack(page, pack_id)
    for view in VIEWS:
        show(page, view)
        assert page.evaluate("document.documentElement.scrollWidth") <= PHONE["width"], view


def test_the_sandbox_notice_stays_on_a_phone(page: Page) -> None:
    page.set_viewport_size(PHONE)
    open_pack(page, INBOX)
    expect(page.locator(".tag")).to_be_visible()


def test_an_audit_record_opens_from_the_keyboard(page: Page) -> None:
    open_pack(page, INBOX, "audit")
    page.locator("#audit-table button.open-record").first.focus()
    page.keyboard.press("Enter")
    heading = page.locator("#audit-detail h3").first
    expect(heading).to_contain_text("Decision")
    expect(heading).to_be_focused()


def test_a_new_view_starts_at_the_top_with_its_heading_focused(page: Page) -> None:
    page.set_viewport_size({"width": 1280, "height": 400})
    open_pack(page, INBOX, "audit")
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    assert page.evaluate("window.scrollY") > 0
    page.locator('.nav a[data-view="evals"]').click()
    expect(page.locator("main h1")).to_have_text("Evaluations")
    expect(page.locator("main h1")).to_be_focused()
    assert page.evaluate("window.scrollY") == 0


@pytest.mark.parametrize("pack_id", [INBOX, GUARD])
def test_every_control_has_an_accessible_name(page: Page, pack_id: str) -> None:
    open_pack(page, pack_id)
    for view in ["review", "audit", "evals", "feedback", "try"]:
        show(page, view)
        assert page.evaluate(UNNAMED) == [], view
    page.get_by_role("button", name="Decide").click()
    expect(page.locator("#result .fb-note")).to_be_visible()
    assert page.evaluate(UNNAMED) == []


def test_decide_shows_progress_and_blocks_a_second_submit(page: Page) -> None:
    open_pack(page, INBOX, "try")
    held: list[Route] = []
    page.route("**/decide", lambda route: held.append(route))
    page.get_by_role("button", name="Chest pain").click()
    decide = page.locator("#decide")
    decide.click()
    expect(decide).to_be_disabled()
    expect(decide).to_have_attribute("aria-busy", "true")
    assert len(held) == 1
    held[0].continue_()
    expect(decide).to_be_enabled()
    expect(decide).not_to_have_attribute("aria-busy", "true")
    expect(page.locator("#result")).to_contain_text("nurse")


def test_the_record_example_picker_matches_the_input(page: Page) -> None:
    open_pack(page, GUARD, "try")
    expect(page.locator("#example")).to_have_value("0")
    expect(page.locator("#expected")).to_contain_text("The pack's test expects")


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_buttons_and_badges_meet_aa_contrast(page: Page, scheme: str) -> None:
    page.emulate_media(color_scheme=scheme)  # type: ignore[arg-type]
    open_pack(page, INBOX, "audit")
    pairs = page.evaluate(CONTRAST, "main .badge") + page.evaluate(CONTRAST, "#view button.primary")
    show(page, "evals")
    pairs += page.evaluate(CONTRAST, "#view button.primary")
    assert pairs
    failing = [(text, round(ratio, 2)) for text, ratio in pairs if ratio < 4.5]
    assert failing == [], scheme


def test_times_are_eastern_whatever_the_viewer_clock(browser: Browser, base_url: str) -> None:
    context = browser.new_context(base_url=base_url, timezone_id="Asia/Tokyo")
    page = context.new_page()
    try:
        page.add_init_script(f"localStorage.setItem('oh.pack', {json.dumps(INBOX)})")
        show(page, "audit")
        latest = httpx.get(f"{base_url}/api/audit", params={"pack_id": INBOX, "limit": 1}).json()
        eastern = ZoneInfo("America/New_York")
        at = datetime.fromisoformat(latest[0]["created_at"]).astimezone(eastern)
        hour = at.hour % 12 or 12
        expected = f"{at:%b} {at.day}, {at.year}, {hour}:{at:%M} {at:%p} {at:%Z}"
        shown = page.locator("#audit-table button.open-record").first.inner_text()
        assert shown.replace("\u202f", " ") == expected
        show(page, "overview")
        expect(page.locator("main")).to_contain_text("days in Eastern Time")
    finally:
        context.close()
