"""Dashboard smoke tests in a real browser (Chromium).

Skipped by the default `pytest` run; run them with `uv run pytest -m browser` (first time:
`uv run playwright install chromium`).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
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
    expect(table.locator("th", has_text="Accuracy")).to_have_attribute("aria-sort", "descending")
    expect(accuracy).to_be_focused()


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


# -- charts ---------------------------------------------------------------------------------------

DAILY = 'figure.chart[data-chart="daily"]'

# Text inside a chart that assistive technology would read outside the marks' own labels.
DECORATION_EXPOSED = """(figure) => [...figure.querySelectorAll('svg.chart-svg[role=group] text')]
  .filter((t) => !t.closest('[aria-hidden="true"]') && !t.closest('.mark'))
  .map((t) => t.textContent)"""


def serve_daily(page: Page, days: list[tuple[str, int]]) -> None:
    """Answer /api/stats with the given history: (Eastern date, decisions that day)."""

    def handle(route: Route) -> None:
        body = route.fetch().json()
        body["daily"] = [
            {"date": day, "AUTO": n, "HUMAN_REVIEW": 0, "ABSTAIN": 0, "BLOCK": 0} for day, n in days
        ]
        route.fulfill(json=body)

    page.route("**/api/stats*", handle)


def eastern_days_ago(days: int) -> str:
    return (datetime.now(ZoneInfo("America/New_York")).date() - timedelta(days=days)).isoformat()


def test_the_daily_chart_starts_at_the_first_decision(page: Page) -> None:
    """A young deployment's chart is not a run of empty days: it starts at the first decision, and
    the days in between with nothing to show are real zeros."""
    serve_daily(page, [(eastern_days_ago(3), 5), (eastern_days_ago(1), 2)])
    open_pack(page, INBOX)
    marks = page.locator(f"{DAILY} .mark")
    expect(marks).to_have_count(4)  # three days ago .. today; nothing before the first decision
    for mark, total in zip(marks.all(), [5, 0, 2, 0], strict=True):
        expect(mark).to_have_attribute("aria-label", re.compile(f"Total {total}$"))
    expect(page.locator(DAILY)).to_contain_text("Since the first decision")
    expect(page.locator(f"{DAILY} svg.chart-svg")).to_have_attribute(
        "aria-label", re.compile(r"^Decisions per day, .* peak 5 on ")
    )


def test_the_daily_chart_shows_the_last_30_days_and_the_table_shows_all(page: Page) -> None:
    serve_daily(page, [(eastern_days_ago(back), 1) for back in range(44, -1, -1)])
    open_pack(page, INBOX)
    expect(page.locator(f"{DAILY} .mark")).to_have_count(30)
    expect(page.locator(DAILY)).to_contain_text("The last 30 days")
    expect(page.locator(f"{DAILY} details.chart-data tbody tr")).to_have_count(45)


def test_chart_marks_are_reached_and_read_with_the_keyboard(page: Page) -> None:
    open_pack(page, INBOX, "evals")
    page.goto("/#evals/report")
    sweep = page.locator('figure.chart[data-chart^="sweep"]')
    marks = sweep.locator(".mark")
    tip = sweep.locator(".chart-tip")
    expect(marks).to_have_count(9)
    expect(sweep.locator('.mark[tabindex="0"]')).to_have_count(1)  # one Tab stop per chart
    marks.first.focus()
    page.keyboard.press("ArrowRight")
    expect(marks.nth(1)).to_be_focused()
    expect(tip).to_be_visible()
    expect(tip).to_contain_text("Automate at ≥ 0.60")
    expect(tip).to_contain_text("Coverage")
    page.keyboard.press("End")
    expect(marks.nth(8)).to_be_focused()
    expect(marks.nth(8)).to_have_attribute("tabindex", "0")
    expect(marks.first).to_have_attribute("tabindex", "-1")
    page.keyboard.press("Escape")
    expect(tip).to_be_hidden()
    marks.nth(2).hover()
    expect(tip).to_contain_text("Automate at ≥ 0.70")
    # The label a screen reader gets carries the same exact values.
    expect(marks.nth(2)).to_have_attribute("aria-label", re.compile(r"Coverage .*%, Accuracy .*%"))


def test_every_chart_has_a_summary_and_its_data_table(page: Page) -> None:
    open_pack(page, INBOX)
    expect(page.locator(DAILY)).to_be_visible()
    seen = 0
    for target in ["#overview", "#evals/report"]:
        page.goto(f"/{target}")
        expect(page.locator("main h1")).to_be_visible()
        expect(page.locator("figure.chart svg").first).to_be_visible()
        for figure in page.locator("figure.chart").all():
            expect(figure.locator("svg.chart-svg")).to_have_attribute(
                "aria-label", re.compile(r"\S{8}")
            )
            assert figure.evaluate(DECORATION_EXPOSED) == []
            expect(figure.locator(".chart-tip")).to_have_attribute("aria-hidden", "true")
            data = figure.locator("details.chart-data")
            expect(data.locator("summary")).to_have_text("View data")
            data.locator("summary").click()
            assert data.locator("tbody tr").count() > 0
            seen += 1
    assert seen == 3  # daily, reliability, threshold sweep


@pytest.mark.parametrize("pack_id", [INBOX, GUARD])
def test_charts_fit_a_phone_and_keep_their_text_readable(page: Page, pack_id: str) -> None:
    page.set_viewport_size(PHONE)
    open_pack(page, pack_id)
    for target in ["#overview", "#evals", "#evals/report", "#try"]:
        page.goto(f"/{target}")
        expect(page.locator("main h1")).to_be_visible()
        expect(page.locator("main .empty", has_text="Loading")).to_have_count(0)
        assert page.evaluate("document.documentElement.scrollWidth") <= PHONE["width"], target
        widest = page.evaluate(
            "Math.max(0, ...[...document.querySelectorAll('.chart-svg')]"
            ".map((svg) => svg.getBoundingClientRect().right))"
        )
        assert widest <= PHONE["width"], target
        sizes = page.evaluate(
            "[...document.querySelectorAll('.chart-svg text')]"
            ".map((t) => parseFloat(getComputedStyle(t).fontSize))"
        )
        assert all(size >= 12 for size in sizes), target  # drawn at real size, never scaled down


def test_charts_switch_with_the_theme_without_a_reload(page: Page) -> None:
    page.emulate_media(color_scheme="light")
    open_pack(page, INBOX)
    column = page.locator(f"{DAILY} .seg.f-AUTO").first
    light = column.evaluate("el => getComputedStyle(el).fill")
    page.get_by_role("button", name="Dark").click()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    assert column.evaluate("el => getComputedStyle(el).fill") != light


def test_the_cost_chart_rings_the_deployed_configuration(page: Page) -> None:
    open_pack(page, GUARD, "evals")
    chart = page.locator('figure.chart[data-chart^="cost"]')
    rows = page.locator("#board table.data").first.locator("tbody tr")
    expect(chart.locator(".mark")).to_have_count(rows.count())  # one dot per leaderboard row
    expect(chart.locator("svg .ring")).to_have_count(1)  # locally the rules screen is deployed
    expect(chart.locator("svg text", has_text="$0").first).to_be_visible()  # rules cost nothing
    expect(chart.locator(".mark").first).to_have_attribute(
        "aria-label", re.compile(r"Accuracy .*verdict = block caught \d+/\d+")
    )


def test_probability_bars_are_coloured_by_what_the_label_does(page: Page) -> None:
    open_pack(page, GUARD, "try")
    verdict = 'figure.chart[data-chart$="-verdict"] .chosen rect'
    page.select_option("#example", label="a draft that misses an emergency is blocked")
    page.get_by_role("button", name="Decide").click()
    expect(page.locator(f"{verdict}.f-block")).to_have_count(1)  # a block is danger, not green
    expect(page.locator('figure.chart[data-chart$="-verdict"] svg')).to_have_attribute(
        "aria-label", re.compile(r"^verdict: block \d.*Chosen: block\.$")
    )
    page.select_option("#example", label="a safe draft passes and waits for one-step approval")
    page.get_by_role("button", name="Decide").click()
    expect(page.locator(f"{verdict}.f-c1")).to_have_count(1)
    expect(page.locator(f"{verdict}.f-block")).to_have_count(0)


def test_the_confusion_matrix_shades_only_the_mistakes(page: Page) -> None:
    open_pack(page, INBOX, "evals")
    page.goto("/#evals/report")
    page.get_by_text("Confusion matrix").click()
    table = page.locator("table.data", has_text="True label")
    expect(table.locator("thead th").first).to_contain_text("True label")
    diagonal = table.locator("tbody tr").first.locator("td").nth(1)
    expect(diagonal.locator(".cell-warn, .cell-bad")).to_have_count(0)  # right answers stay plain
    assert table.locator(".cell-warn, .cell-bad").count() > 0  # front_desk -> billing, and so on
    expect(table.locator("thead th").last).to_have_text("No answer")  # not a label called "none"


def test_the_threshold_sweep_does_not_claim_automation_that_never_happens(page: Page) -> None:
    """At risk level 2 the AI Guard automates nothing; the sweep is a what-if and says so."""
    open_pack(page, GUARD, "evals")
    page.goto("/#evals/report")
    sweep = page.locator('figure.chart[data-chart^="sweep"]')
    expect(sweep).to_contain_text("Automation threshold 0.90")
    expect(sweep).to_contain_text("Nothing was automated in this run")
    expect(sweep).not_to_contain_text("automates at")
    page.select_option("#pack-select", INBOX)  # automates 94.5 % of its benchmark
    expect(page.locator("#evals-body")).to_contain_text("destination accuracy")
    expect(page.locator('figure.chart[data-chart^="sweep"]')).not_to_contain_text("Nothing was")


def test_criterion_bars_take_the_colour_of_their_effect(page: Page) -> None:
    """unsafe_advice = yes blocks the draft through the decision table, so its bar is danger too,
    matching the criteria table's Effect column. The chart sits in a closed disclosure, so this
    also checks that a chart hidden at first is drawn when it is opened."""
    open_pack(page, GUARD, "try")
    page.select_option("#example", label="a draft that misses an emergency is blocked")
    page.get_by_role("button", name="Decide").click()
    page.get_by_text("Probabilities for every criterion").click()
    unsafe = page.locator('figure.chart[data-chart$="-unsafe_advice"]')
    expect(unsafe.locator(".chosen rect.f-block")).to_have_count(1)
    expect(unsafe.locator(".chosen .bar-label")).to_have_text("yes")
