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


def test_an_audit_record_opens_in_a_drawer_from_the_keyboard(page: Page) -> None:
    open_pack(page, INBOX, "audit")
    opener = page.locator("#audit-table button.open-record").first
    decision_id = opener.get_attribute("data-id")
    opener.focus()
    page.keyboard.press("Enter")
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    expect(page).to_have_url(re.compile(f"#audit/{decision_id}$"))
    expect(page.locator("#drawer-title")).to_be_focused()
    expect(dialog).to_contain_text("Decision recorded")  # the hash-chain timeline
    inside = "document.querySelector('[role=dialog]').contains(document.activeElement)"
    for _ in range(12):  # Tab never leaves the dialog
        page.keyboard.press("Tab")
        assert page.evaluate(inside)
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)
    expect(page).to_have_url(re.compile("#audit$"))
    expect(opener).to_be_focused()


def test_a_record_link_opens_its_drawer(page: Page, live_server: str) -> None:
    latest = httpx.get(f"{live_server}/api/audit", params={"pack_id": INBOX, "limit": 1}).json()[0]
    page.add_init_script(f"localStorage.setItem('oh.pack', {json.dumps(INBOX)})")
    page.goto(f"/#audit/{latest['id']}")
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text(latest["id"])
    page.get_by_role("button", name="Close").click()
    expect(dialog).to_have_count(0)


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
        full = f"{at:%b} {at.day}, {at.year}, {hour}:{at:%M} {at:%p} {at:%Z}"
        cell = page.locator("#audit-table button.open-record time").first
        short = f"{at:%b} {at.day}, {hour}:{at:%M} {at:%p}"
        assert cell.inner_text().replace("\u202f", " ") == short
        assert (cell.get_attribute("title") or "").replace("\u202f", " ") == full
        expect(page.locator("#audit-table th").first).to_contain_text("Time (ET)")
        show(page, "overview")
        expect(page.locator("main")).to_contain_text("days in Eastern Time")
    finally:
        context.close()


def test_the_theme_choice_is_kept_across_reloads(page: Page) -> None:
    page.emulate_media(color_scheme="light")
    open_pack(page, INBOX)
    html = page.locator("html")
    expect(html).to_have_attribute("data-theme", "light")
    page.get_by_role("button", name="Dark").click()
    expect(html).to_have_attribute("data-theme", "dark")
    expect(page.get_by_role("button", name="Dark")).to_have_attribute("aria-pressed", "true")
    page.reload()
    expect(html).to_have_attribute("data-theme", "dark")
    expect(page.get_by_role("button", name="Dark")).to_have_attribute("aria-pressed", "true")


@pytest.mark.parametrize(
    ("saved", "system", "theme"), [(None, "dark", "dark"), ("light", "dark", "light")]
)
def test_the_theme_is_set_before_any_app_code_runs(
    page: Page, saved: str | None, system: str, theme: str
) -> None:
    """No flash of the wrong theme: with app.js held back, the head script alone sets it."""
    page.emulate_media(color_scheme=system)  # type: ignore[arg-type]
    if saved:
        page.add_init_script(f"localStorage.setItem('oh.theme', {json.dumps(saved)})")
    page.route("**/static/app.js*", lambda route: None)  # never answered
    page.goto("/", wait_until="commit")
    page.wait_for_function("document.documentElement.hasAttribute('data-theme')")
    assert page.evaluate("document.documentElement.dataset.theme") == theme


def test_system_theme_follows_the_system_setting_live(page: Page) -> None:
    page.emulate_media(color_scheme="light")
    open_pack(page, INBOX)
    expect(page.get_by_role("button", name="System")).to_have_attribute("aria-pressed", "true")
    page.emulate_media(color_scheme="dark")
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")


def test_the_leaderboard_ranks_safety_first_and_marks_the_deployed_row(page: Page) -> None:
    open_pack(page, GUARD, "evals")
    expect(page).to_have_url(re.compile("#evals$"))
    compare = page.get_by_role("link", name="Provider comparison")
    expect(compare).to_have_attribute("aria-current", "page")
    held_out = page.get_by_role("button", name=re.compile("unsafe_drafts_holdout"))
    expect(held_out).to_have_attribute("aria-pressed", "true")  # held-out set first
    table = page.locator("#board table").first  # the per-check table follows it
    ranks = table.locator("tbody .rank").all_inner_texts()
    assert ranks == [str(i) for i in range(1, len(ranks) + 1)]
    # Locally the pack runs its default screen (the keyword rules), the weakest on held-out blocks.
    deployed = table.locator("tbody tr.deployed")
    expect(deployed).to_have_count(1)
    expect(deployed).to_contain_text("Rules")
    expect(deployed).to_contain_text("Deployed")
    expect(table.locator("tbody tr").last).to_have_class(re.compile("deployed"))
    # Constant columns fold into the caption (risk level 2: nothing is automated).
    note = page.locator("#board .table-note")
    expect(note).to_contain_text("Automated 0.0% in every configuration")
    expect(note).to_contain_text("all rows: dosing = Dosing")
    expect(table.locator("caption")).to_contain_text("unsafe_drafts_holdout · n=24")
    # Sort by accuracy: the header says so, and the best accuracy comes first.
    accuracy = table.get_by_role("button", name="Accuracy")
    accuracy.click()
    header = page.locator("#board th", has=page.get_by_role("button", name="Accuracy"))
    expect(header).to_have_attribute("aria-sort", "descending")
    expect(page.locator("#board th button.sort", has_text="Accuracy")).to_be_focused()


def test_the_leaderboard_remembers_the_dataset(page: Page) -> None:
    open_pack(page, GUARD, "evals")
    page.get_by_role("button", name=re.compile(r"^test n=")).click()
    expect(page.locator("#board caption")).to_contain_text("test · n=")
    page.reload()
    test_set = page.get_by_role("button", name=re.compile(r"^test n="))
    expect(test_set).to_have_attribute("aria-pressed", "true")


def test_evaluation_views_have_their_own_urls(page: Page) -> None:
    open_pack(page, GUARD, "evals")
    page.get_by_role("link", name="Run on a dataset").click()
    expect(page).to_have_url(re.compile("#evals/run$"))
    expect(page.get_by_label("Dataset")).to_be_visible()
    page.get_by_role("link", name="Benchmark report").click()
    expect(page.locator("#evals-body")).to_contain_text("Per label")
    page.goto("/#evals/history")
    expect(page.get_by_role("link", name="History")).to_have_attribute("aria-current", "page")


def test_subgroups_are_grouped_lowest_first(page: Page) -> None:
    open_pack(page, INBOX, "evals")
    page.goto("/#evals/report")
    groups = page.locator("details.group")
    expect(groups.first).to_be_visible()
    groups.first.locator("summary").click()
    accuracies = groups.first.locator("tbody td:nth-child(3)").all_inner_texts()
    values = [float(a.split("%")[0]) for a in accuracies if "%" in a]
    assert values == sorted(values)


def test_the_leaderboard_keeps_rank_and_configuration_in_view_on_a_phone(page: Page) -> None:
    page.set_viewport_size(PHONE)
    open_pack(page, GUARD, "evals")
    container = page.locator("#board .table-scroll").first
    assert container.evaluate("el => el.scrollWidth > el.clientWidth")  # it scrolls sideways...
    for selector in ["tbody tr:first-child td.rank-cell", "tbody tr:first-child td.config-cell"]:
        cell = container.locator(selector)
        assert cell.evaluate("el => getComputedStyle(el).position") == "sticky", selector
    container.evaluate("el => el.scrollBy(400, 0)")
    box = container.locator("tbody tr:first-child td.config-cell").bounding_box()
    assert box is not None and box["x"] < 120  # ...but rank and configuration stay in view
