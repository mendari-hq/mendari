# SPDX-License-Identifier: Apache-2.0
"""Dashboard smoke tests in a real browser (Chromium).

Skipped by the default `pytest` run; run them with `uv run pytest -m browser` (first time:
`uv run playwright install chromium`).
"""

from __future__ import annotations

import json
import re
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from playwright.sync_api import Browser, Locator, Page, Route, ViewportSize, expect

from mendari import Agent, load_agent
from mendari.evals import figures
from mendari.evals.render import shown
from mendari.server import api_docs
from tests.browser.conftest import BENCHMARK, CHART, CLINICAL, TRIAGE
from tests.conftest import ROOT

pytestmark = pytest.mark.browser

# The agents the live server serves (tests/browser/conftest.py), as `/api/agents` lists them, and
# the names they go by.
AGENTS = [CHART, CLINICAL, TRIAGE]
TRIAGE_NAME = "Synthetic Triage (test fixture)"
CLINICAL_NAME = "Synthetic Clinical Review (test fixture)"
CHART_NAME = "Synthetic Chart Summary (test fixture)"
VIEWS = ["overview", "agents", "review", "audit", "evals", "feedback", "card", "try"]
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
    """Go to a view and wait until it has rendered (not just its loading skeleton)."""
    page.goto(f"/#{view}")
    expect(page.locator(f'.nav a[data-view="{view}"]')).to_have_class(re.compile("active"))
    expect(page.locator("main h1")).to_be_visible()
    expect(page.locator("#view")).not_to_have_attribute("aria-busy", "true")


def open_agent(page: Page, agent_id: str, view: str = "overview") -> None:
    page.add_init_script(f"localStorage.setItem('oh.agent', {json.dumps(agent_id)})")
    show(page, view)


def served(folder: Path, agent_id: str) -> Agent:
    """The agent the live server serves as `agent_id`: its published copy in `folder` (the
    `served_agents` fixture), with its benchmark and leaderboard."""
    return load_agent(folder / agent_id.split("/", 1)[1])


# What one call to an agent's service costs a deployment (synthetic, in dollars), and how many
# calls each of the triage fixture's variants makes for a decision: its agent variants answer here
# from canned examples, at no cost, and the cost chart is drawn only where costs differ.
CALL_USD = 0.0004
CALLS = {"agent": 1, "checked": 1, "repeated": 3}


def counted_board(
    agent: Agent, *, positives: int | None = None, priced: bool = False
) -> dict[str, Any]:
    """The agent's published leaderboard as the server serves it (`figures.board`). With
    `positives`, each critical label's counts are scaled to that many positives (synthetic), so
    the rows rank: the fixtures' own sets have too few positives for any row to rank. `priced`:
    each row costs what its calls to a service would (`CALLS`, synthetic)."""
    board = json.loads(agent.attestation("evals/leaderboard.json").read_text())
    for row in board["rows"]:
        if positives is not None:
            row["critical"] = [
                {**c, "n": positives, "detected": round(positives * c["detected"] / c["n"])}
                for c in row["critical"]
                if c["n"]
            ]
        if priced:
            calls = sum(CALLS.get(p["variant"], 0) for p in row["providers"].values())
            row["cost_per_decision_usd"] = calls * CALL_USD
    return figures.board(board, agent.digest, agent.version)


def board_group(board: dict[str, Any], dataset: str) -> int:
    """The index of the board's group (its table) of the rows measured on `dataset`."""
    return next(i for i, group in enumerate(board["groups"]) if group["dataset"] == dataset)


def serve_board(page: Page, agent_id: str, board: dict[str, Any]) -> None:
    """Answer the agent's detail with `board` as its leaderboard."""

    def handle(route: Route) -> None:
        body = route.fetch().json()
        body["leaderboard"] = board
        route.fulfill(json=body)

    page.route(re.compile(rf".*/api/agents/{re.escape(agent_id)}$"), handle)


@pytest.mark.parametrize("agent_id", AGENTS)
def test_every_view_renders_without_errors(
    page: Page, console_errors: list[str], agent_id: str
) -> None:
    open_agent(page, agent_id)
    for view in VIEWS:
        show(page, view)
        expect(page.locator("main")).not_to_contain_text("Could not load this view")
    assert console_errors == []


@pytest.mark.parametrize("agent_id", [TRIAGE, CLINICAL])
def test_no_view_scrolls_sideways_on_a_phone(page: Page, agent_id: str) -> None:
    page.set_viewport_size(PHONE)
    open_agent(page, agent_id)
    for view in VIEWS:
        show(page, view)
        assert page.evaluate("document.documentElement.scrollWidth") <= PHONE["width"], view


def test_the_sandbox_notice_stays_on_a_phone(page: Page) -> None:
    page.set_viewport_size(PHONE)
    open_agent(page, TRIAGE)
    expect(page.locator(".tag")).to_be_visible()


def test_an_audit_record_opens_in_a_drawer_from_the_keyboard(page: Page) -> None:
    open_agent(page, TRIAGE, "audit")
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
    latest = httpx.get(f"{live_server}/api/audit", params={"agent_id": TRIAGE, "limit": 1}).json()[
        0
    ]
    page.add_init_script(f"localStorage.setItem('oh.agent', {json.dumps(TRIAGE)})")
    page.goto(f"/#audit/{latest['id']}")
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text(latest["id"])
    page.get_by_role("button", name="Close").click()
    expect(dialog).to_have_count(0)


@pytest.mark.parametrize("axe_page", ["light", "dark"], indirect=True)
def test_a_fhir_decision_shows_what_it_read_in_its_record(axe_page: Page, live_server: str) -> None:
    """A decision made from FHIR opens, from the link a card gives (`?agent=…#audit/<id>`,
    LP-23f), on a panel of what it read (its data-coverage record): each mapped field and list,
    the resources read, the values found and what could not be read, as counts, never as a
    share; and the drawer passes axe in both themes. A decision on JSON input has no panel."""
    from tests.browser.test_accessibility import violations  # it imports this module

    made = httpx.post(
        f"{live_server}/api/agents/{TRIAGE}/fhir/decide", json=api_docs.FHIR_EXAMPLE, timeout=30
    )
    assert made.status_code == 200, made.text
    decision = made.json()["decision"]["id"]
    axe_page.goto(f"/?agent={TRIAGE}#audit/{decision}")  # as the card writes it
    panel = axe_page.locator(".drawer section.coverage")
    expect(panel).to_be_visible()
    expect(panel.get_by_role("heading", name="What it read")).to_be_visible()
    table = panel.locator("table")
    expect(table).to_contain_text("Focus: Communication")
    expect(table.locator("th")).to_have_text(
        ["Field", "Resources read", "Values found", "Codings", "Could not read"]
    )
    assert axe_page.evaluate(BARE_PERCENT_IN, ".drawer section.coverage") == []
    assert not re.search(r"\d+\s+(?:of|out of|/)\s*\d+", panel.inner_text())  # no k of n
    assert violations(axe_page) == []
    latest = httpx.get(f"{live_server}/api/audit", params={"agent_id": CLINICAL, "limit": 1}).json()
    axe_page.goto(f"/?agent={CLINICAL}#audit/{latest[0]['id']}")
    expect(axe_page.get_by_role("dialog")).to_contain_text(latest[0]["id"])
    expect(axe_page.locator(".drawer section.coverage")).to_have_count(0)


@pytest.mark.parametrize("axe_page", ["light", "dark"], indirect=True)
def test_a_list_of_several_resource_types_shows_a_row_per_source(
    axe_page: Page, live_server: str
) -> None:
    """A list read from several resource types (the chart fixture's medications, from
    MedicationStatement and MedicationRequest) shows a row for the list and one for each source,
    with the resources each read, never `undefined`; the drawer passes axe in both themes
    (2026-11-08)."""
    from tests.browser.test_accessibility import violations  # it imports this module
    from tests.support.fixture_records import SHOWCASE

    record = SHOWCASE.bundle()  # both kinds of medication, a chart's record as an EHR writes it
    made = httpx.post(f"{live_server}/api/agents/{CHART}/fhir/decide", json=record, timeout=30)
    assert made.status_code == 200, made.text
    axe_page.goto(f"/?agent={CHART}#audit/{made.json()['decision']['id']}")
    table = axe_page.locator(".drawer section.coverage table")
    expect(table).to_contain_text("medications ← MedicationStatement")
    expect(table).to_contain_text("medications ← MedicationRequest")
    assert "undefined" not in table.inner_text() and "n/a" not in table.inner_text()
    assert axe_page.evaluate(BARE_PERCENT_IN, ".drawer section.coverage") == []
    assert violations(axe_page) == []


def test_a_shared_link_opens_the_agent_it_was_copied_from(page: Page, live_server: str) -> None:
    """The agent is in the address, so a link beats whatever agent the viewer chose last, a record
    link opens over that agent's list, and moving between views keeps it."""
    latest = httpx.get(
        f"{live_server}/api/audit", params={"agent_id": CLINICAL, "limit": 1}
    ).json()[0]
    page.add_init_script(f"localStorage.setItem('oh.agent', {json.dumps(TRIAGE)})")
    page.goto(f"/?agent={CLINICAL}#audit/{latest['id']}")
    expect(page.get_by_role("dialog")).to_contain_text(latest["id"])
    expect(page.locator("#agent-select")).to_have_value(CLINICAL)
    page.get_by_role("button", name="Close").click()
    page.locator('.nav a[data-view="review"]').click()
    expect(page).to_have_url(re.compile(f"\\?agent={CLINICAL}#review$"))
    expect(page.locator("#agent-select")).to_have_value(CLINICAL)


def test_choosing_an_agent_puts_it_in_the_address(page: Page) -> None:
    open_agent(page, TRIAGE, "overview")
    expect(page).to_have_url(re.compile(f"\\?agent={TRIAGE}#overview$"))
    page.select_option("#agent-select", CLINICAL)
    expect(page).to_have_url(re.compile(f"\\?agent={CLINICAL}#overview$"))
    expect(page.locator("main h1")).to_have_text(CLINICAL_NAME)


def test_an_unknown_agent_in_the_address_falls_back_to_the_last_one(page: Page) -> None:
    page.add_init_script(f"localStorage.setItem('oh.agent', {json.dumps(CLINICAL)})")
    page.goto("/?agent=someone/else#overview")
    expect(page.locator("#agent-select")).to_have_value(CLINICAL)
    expect(page).to_have_url(re.compile(f"\\?agent={CLINICAL}#overview$"))


def test_going_back_across_an_agent_change_shows_that_agent(page: Page) -> None:
    open_agent(page, TRIAGE, "overview")
    page.locator('.nav a[data-view="audit"]').click()
    expect(page.locator("main h1")).to_have_text("Audit trail")
    page.select_option("#agent-select", CLINICAL)
    expect(page).to_have_url(re.compile(f"\\?agent={CLINICAL}#audit$"))
    page.go_back()
    expect(page).to_have_url(re.compile(f"\\?agent={TRIAGE}#overview$"))
    expect(page.locator("#agent-select")).to_have_value(TRIAGE)
    expect(page.locator("main h1")).to_have_text(TRIAGE_NAME)


def test_an_agent_that_answers_late_does_not_replace_the_one_chosen_after_it(page: Page) -> None:
    """Choosing the clinical review, then going back to the triage agent before the review has
    loaded: when the review's answer finally arrives it is dropped, and the triage agent stays."""
    open_agent(page, TRIAGE, "overview")
    page.locator('.nav a[data-view="audit"]').click()
    expect(page.locator("main h1")).to_have_text("Audit trail")
    held: list[Route] = []
    page.route(f"**/api/agents/{CLINICAL}", lambda route: held.append(route))
    with page.expect_request(f"**/api/agents/{CLINICAL}"):
        page.select_option("#agent-select", CLINICAL)
    page.go_back()
    expect(page.locator("main h1")).to_have_text(TRIAGE_NAME)
    for route in held:
        route.continue_()
    page.unroute(f"**/api/agents/{CLINICAL}")
    page.wait_for_timeout(300)  # time for the late answer and any render it could start
    expect(page.locator("main h1")).to_have_text(TRIAGE_NAME)
    expect(page.locator("#agent-select")).to_have_value(TRIAGE)


def test_going_back_from_one_record_to_another_keeps_the_drawer_open(
    page: Page, live_server: str
) -> None:
    """Replacing an open drawer must not run the old drawer's close handler (which returns the
    URL to the list and so closed the new drawer too)."""
    first, second = httpx.get(
        f"{live_server}/api/audit", params={"agent_id": TRIAGE, "limit": 2}
    ).json()
    page.add_init_script(f"localStorage.setItem('oh.agent', {json.dumps(TRIAGE)})")
    page.goto(f"/#audit/{first['id']}")
    expect(page.get_by_role("dialog")).to_contain_text(first["id"])
    page.goto(f"/#audit/{second['id']}")
    expect(page.get_by_role("dialog")).to_contain_text(second["id"])
    page.go_back()
    expect(page).to_have_url(re.compile(f"#audit/{first['id']}$"))
    expect(page.get_by_role("dialog")).to_contain_text(first["id"])


def test_a_new_view_starts_at_the_top_with_its_heading_focused(page: Page) -> None:
    page.set_viewport_size({"width": 1280, "height": 400})
    open_agent(page, TRIAGE, "audit")
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    assert page.evaluate("window.scrollY") > 0
    page.locator('.nav a[data-view="evals"]').click()
    expect(page.locator("main h1")).to_have_text("Evaluations")
    expect(page.locator("main h1")).to_be_focused()
    assert page.evaluate("window.scrollY") == 0


@pytest.mark.parametrize("agent_id", [TRIAGE, CLINICAL])
def test_every_control_has_an_accessible_name(page: Page, agent_id: str) -> None:
    open_agent(page, agent_id)
    for view in ["review", "audit", "evals", "feedback", "try"]:
        show(page, view)
        assert page.evaluate(UNNAMED) == [], view
    page.get_by_role("button", name="Decide").click()
    page.locator("#result").get_by_role("button", name="Wrong").click()  # opens the follow-up
    expect(page.locator("#result .fb-note")).to_be_visible()
    assert page.evaluate(UNNAMED) == []


def test_decide_shows_progress_and_blocks_a_second_submit(page: Page) -> None:
    open_agent(page, TRIAGE, "try")
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
    open_agent(page, CLINICAL, "try")
    expect(page.locator("#example")).to_have_value("0")
    expect(page.locator("#expected")).to_contain_text("The agent's test expects")


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_buttons_and_badges_meet_aa_contrast(page: Page, scheme: str) -> None:
    page.emulate_media(color_scheme=scheme)  # type: ignore[arg-type]
    open_agent(page, TRIAGE, "audit")
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
        page.add_init_script(f"localStorage.setItem('oh.agent', {json.dumps(TRIAGE)})")
        show(page, "audit")
        latest = httpx.get(f"{base_url}/api/audit", params={"agent_id": TRIAGE, "limit": 1}).json()
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
    open_agent(page, TRIAGE)
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
    """No flash of the wrong theme: with every module held back, the head script alone sets it."""
    page.emulate_media(color_scheme=system)  # type: ignore[arg-type]
    if saved:
        page.add_init_script(f"localStorage.setItem('oh.theme', {json.dumps(saved)})")
    page.route("**/static/*.js*", lambda route: None)  # never answered
    page.goto("/", wait_until="commit")
    page.wait_for_function("() => document.documentElement.hasAttribute('data-theme')")
    assert page.evaluate("document.documentElement.dataset.theme") == theme


def test_system_theme_follows_the_system_setting_live(page: Page) -> None:
    page.emulate_media(color_scheme="light")
    open_agent(page, TRIAGE)
    expect(page.get_by_role("button", name="System")).to_have_attribute("aria-pressed", "true")
    page.emulate_media(color_scheme="dark")
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")


def test_the_leaderboard_shows_its_listing_and_marks_the_deployed_row(page: Page) -> None:
    """Every set is public, so the comparison is a regression check, not a ranking (LP-17g): no
    rank column. The rows were measured on this version, with too few positives for any to rank,
    and each says so under its name."""
    open_agent(page, TRIAGE, "evals")
    expect(page).to_have_url(re.compile("#evals$"))
    compare = page.get_by_role("link", name="Provider comparison")
    expect(compare).to_have_attribute("aria-current", "page")
    panel = page.locator("#evals-body .panel").first
    expect(panel).to_contain_text("Public set: regression check, not a ranking.")
    # The order as ranking.grouped writes it: no row ranks, so by configuration name.
    expect(panel).to_contain_text("every other row follows, by configuration name")
    expect(panel).to_contain_text("the agent's own sets are public")
    expect(panel).not_to_contain_text("Every set")
    benchmark_set = page.get_by_role("button", name=re.compile(r"^test n="))
    expect(benchmark_set).to_have_attribute("aria-pressed", "true")  # the benchmark's set first
    table = page.locator("#board table").first  # the per-check table follows it
    assert "Rank" not in table.locator("thead th").all_inner_texts()
    expect(table.locator(".rank, .rank-cell")).to_have_count(0)
    expect(table.locator("tbody .standing").first).to_contain_text("insufficient evidence")
    # Locally the agent runs its default router and screen (the keyword rules).
    deployed = table.locator("tbody tr.deployed")
    expect(deployed).to_have_count(1)
    expect(deployed).to_contain_text("Rules")
    expect(deployed).to_contain_text("Deployed")
    expect(table.locator("caption")).to_contain_text("test · n=300")
    # Constant columns fold into the caption: the critical set was run with one router only.
    page.get_by_role("button", name=re.compile(r"^critical n=")).click()
    note = page.locator("#board .table-note").first
    expect(note).to_contain_text("all rows: router = Rules")
    # And at risk level 2 nothing is automated, in any configuration.
    page.select_option("#agent-select", CLINICAL)
    note = page.locator("#board .table-note").first
    expect(note).to_contain_text(f"Automated {shown(figures.of(0, 120))} in every configuration")


def test_a_template_board_shows_no_rank_and_keeps_its_order_when_sorted(
    page: Page, served_agents: Path
) -> None:
    """A board whose rows rank as data (mendari.rank/1) is listed in the server's order with no
    rank shown, as every set is public (LP-17g); sorting by a column moves the rows (the row
    elements survive it), and the listing order is what the table opens in."""
    board = counted_board(served(served_agents, TRIAGE), positives=120)
    group = board_group(board, "test")
    rows = [r for r in board["rows"] if r["group"] == group]
    assert [r["rank"] for r in rows] == list(range(1, len(rows) + 1))  # all rank, as data
    serve_board(page, TRIAGE, board)
    open_agent(page, TRIAGE, "evals")
    page.get_by_role("button", name=board["groups"][group]["label"]).click()
    table = page.locator("#board table.data").first
    listed = """(table) => [...table.querySelectorAll('tbody tr')].map((tr) =>
      tr.querySelector('.config').title)"""
    assert table.evaluate(listed) == [r["config"] for r in rows]  # the server's order
    assert "Rank" not in table.locator("thead th").all_inner_texts()
    expect(table.locator(".rank, .rank-cell")).to_have_count(0)
    for rank in {str(r["rank"]) for r in rows}:
        expect(table.locator("tbody td", has_text=re.compile(rf"^{rank}$"))).to_have_count(0)
    table.locator("tbody tr").first.evaluate("(tr) => { tr.dataset.kept = 'yes'; }")
    table.get_by_role("button", name="Accuracy").click()
    expect(table.locator("th", has_text="Accuracy")).to_have_attribute("aria-sort", "descending")
    expect(table.get_by_role("button", name="Accuracy")).to_be_focused()
    expect(table.locator("tbody tr[data-kept='yes']")).to_have_count(1)
    assert sorted(table.evaluate(listed)) == sorted(r["config"] for r in rows)  # rows moved
    column = table.locator("th", has_text="Accuracy").evaluate("(th) => th.cellIndex")
    cells = table.locator(f"tbody td:nth-child({column + 1})").all_inner_texts()
    found = [re.match(r"(\d+) of (\d+)", c) for c in cells]
    shares = [int(m.group(1)) / int(m.group(2)) for m in found if m]
    assert len(shares) == len(cells) and shares == sorted(shares, reverse=True)
    table.get_by_role("button", name="Accuracy").click()  # again: ascending, the arrow flips
    expect(table.locator("th", has_text="Accuracy")).to_have_attribute("aria-sort", "ascending")
    expect(table.locator("th", has_text="Accuracy").locator("use")).to_have_attribute(
        "href", "#i-chevron-up"
    )
    expect(table.locator("th[aria-sort='descending']")).to_have_count(0)


def test_the_dashboard_prints_a_figure_as_the_card_does(page: Page) -> None:
    """ui.js's port of render.shown, checked against what Python prints for 2,401 figures and 601
    bounds (tests/fixtures/shown.json), the exact rounding ties among them."""
    open_agent(page, TRIAGE)
    fixture = json.loads((ROOT / "tests" / "fixtures" / "shown.json").read_text())
    wrong = page.evaluate(
        """async (fixture) => {
          const ui = await import(new URL("/static/ui.js", location).href);
          const wrong = [];
          for (const [value, text] of fixture.percent) {
            if (ui.percent(value) !== text) wrong.push(["percent", value, ui.percent(value), text]);
          }
          for (const [k, n, low, high, text] of fixture.shown) {
            const got = ui.shown({ k, n, low, high });
            if (got !== text) wrong.push(["shown", k, n, got, text]);
          }
          if (ui.shown(null) !== fixture.none) wrong.push(["none", ui.shown(null)]);
          return wrong;
        }""",
        fixture,
    )
    assert wrong == []


def monitoring_week(start: str, end: str, agreed: int, labelled: int, overridden: int) -> Any:
    """A week of /api/monitoring, its figures as the server works them out."""
    return {
        "from": start,
        "to": end,
        "decisions": 30,
        "refused": 0,
        "automated_rate": 24 / 30,
        "reviewed": 6,
        "awaiting_review": 1,
        "override_rate": overridden / 6,
        "labelled": labelled,
        "observed_accuracy": agreed / labelled if labelled else None,
        "confidences": 30,
        "cost_per_decision": None,
        "cost_unknown": 30,
        "latency_ms": {"n": 30, "p50": 120.0, "p95": 1460.0, "mean": 300.0},
        "figures": {
            "automated": figures.of(24, 30),
            "overrides": figures.of(overridden, 6),
            "accuracy": figures.of(agreed, labelled),
            "reviewed_accuracy": figures.of(6 - overridden, 6),
            "automated_accuracy": figures.of(0, 0),
            "provider_errors": figures.of(1, 30),
            "abstained": figures.of(2, 30),
        },
    }


MONITORING = {
    "agent": {"id": TRIAGE},
    "days": 30,
    "benchmark": {
        "accuracy": 0.97,
        "n": 600,
        "reviewed_accuracy": 0.995,
        "automated_accuracy": None,
        "tolerance": 0.03,
        "comparable": True,
        "reason": None,
        "figures": {
            "accuracy": figures.counted(0.97, 600),
            "reviewed_accuracy": figures.counted(0.995, 598),
            "automated_accuracy": None,
        },
    },
    "other_versions": {},
    "last_7_days": {
        **monitoring_week("2026-09-23", "2026-09-30", 23, 25, 2),
        "drift_psi": 0.1,
        "automated_rated_wrong": 0,
    },
    "weeks": [
        monitoring_week("2026-09-09", "2026-09-16", 24, 25, 1),
        monitoring_week("2026-09-16", "2026-09-23", 0, 0, 1),
        monitoring_week("2026-09-23", "2026-09-30", 23, 25, 2),
    ],
    "alerts": [],
}


# Text a person reads in the view (and its labels and tooltips), with each figure's interval (or
# one-sided bound, "at most", as the operating point gives its counts) and what is a setting rather
# than a share of cases taken out: no percentage may remain.
BARE_PERCENT = r"""() => {
  const texts = [];
  const walker = document.createTreeWalker(document.querySelector("main"), NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (node.parentElement.closest("svg text.tick, .chart-axis-note, script, style")) continue;
    texts.push(node.textContent);
  }
  for (const el of document.querySelectorAll("main [aria-label], main [title]")) {
    texts.push(el.getAttribute("aria-label") ?? "", el.getAttribute("title") ?? "");
  }
  return texts.join("\n")
    .replace(/\[[^\]]*%\]/g, "[interval]")
    .replace(/\(at most [\d.]+%\)/g, "(bound)")
    .replace(/\d+% confidence/g, "confidence")
    .replace(/The vertical axis starts at [\d.]+%/g, "")
    .match(/[^\n]{0,40}\d\s?%[^\n]{0,20}/g) ?? [];
}"""


# BARE_PERCENT over one element (`selector`) instead of `main`: a drawer's panel, say.
BARE_PERCENT_IN = (
    BARE_PERCENT.replace("() => {", "(selector) => {", 1)
    .replace('document.querySelector("main")', "document.querySelector(selector)", 1)
    .replace(
        'document.querySelectorAll("main [aria-label], main [title]")',
        'document.querySelector(selector).querySelectorAll("[aria-label], [title]")',
        1,
    )
)
assert "main" not in BARE_PERCENT_IN.split("return")[0]


@pytest.mark.parametrize(
    ("agent_id", "view"),
    [
        (TRIAGE, "evals/report"),
        (CLINICAL, "evals/report"),
        (TRIAGE, "evals"),
        (TRIAGE, "overview"),
        (TRIAGE, "monitoring"),
    ],
)
def test_no_share_of_cases_is_a_bare_percentage(
    page: Page, served_agents: Path, agent_id: str, view: str
) -> None:
    """Every share of cases on the evaluation, overview and monitoring views is k of n with its
    interval (LP-10); a percentage is only ever a figure's interval or bound, or an axis."""
    serve_history(page, [{"date": eastern_days_ago(0), "AUTO": 12, "HUMAN_REVIEW": 7, "reviewed": 3,
                          "overridden": 1, "labelled": 4, "agreed": 3}])  # fmt: skip
    priced = counted_board(served(served_agents, TRIAGE), priced=True)
    serve_board(page, TRIAGE, priced)  # with its accuracy figures and cost chart
    page.route("**/api/monitoring?*", lambda route: route.fulfill(json=MONITORING))
    open_agent(page, agent_id, view.split("/")[0])
    if "/" in view:
        page.goto(f"/#{view}")
        expect(page.locator("#evals-body")).to_contain_text("Per label")
    expect(page.locator("#view")).not_to_have_attribute("aria-busy", "true")
    expect(page.locator("main")).not_to_contain_text("Could not load this view")
    expect(page.locator("main .interval").first).to_be_attached()  # figures were shown
    for summary in page.locator("main details > summary").all():
        summary.click()  # subgroups, confusion matrix, chart data and per-check tables too
    assert page.evaluate(BARE_PERCENT) == []
    page.evaluate("document.querySelector('main').insertAdjacentHTML('beforeend', '<p>42.0%</p>')")
    assert page.evaluate(BARE_PERCENT) == ["42.0%"]  # the scan does find one


def test_a_leaderboard_with_no_rows_leaves_the_other_evaluation_views(
    page: Page, served_agents: Path
) -> None:
    agent = served(served_agents, TRIAGE)
    serve_board(page, TRIAGE, figures.board({"rows": []}, agent.digest, agent.version))
    open_agent(page, TRIAGE, "evals")
    expect(page.locator("main")).not_to_contain_text("Could not load this view")
    expect(page.get_by_role("link", name="Provider comparison")).to_have_count(0)
    expect(page.locator("#evals-body")).to_contain_text("Per label")  # the report comes first


def test_a_report_with_nothing_labelled_for_its_question_still_shows(
    page: Page, served_agents: Path
) -> None:
    """A run on cases none of which is labelled for the primary question has no figures for it:
    the report says n/a, never fails, and says what it can of what was automated."""
    report = json.loads(served(served_agents, TRIAGE).attestation(BENCHMARK).read_text())
    del report["questions"][report["primary"]]
    counts, n = report["decisions"]["outcome_counts"], report["dataset"]["n"]
    counts.update(AUTO=50, HUMAN_REVIEW=n - 50 - counts["ABSTAIN"] - counts["BLOCK"])

    def handle(route: Route) -> None:
        body = route.fetch().json()
        body["benchmark"] = {**report, "figures": figures.guarded(lambda: figures.report(report))}
        route.fulfill(json=body)

    page.route(re.compile(rf".*/api/agents/{re.escape(TRIAGE)}$"), handle)
    open_agent(page, TRIAGE, "evals")
    page.goto("/#evals/report")
    body = page.locator("#evals-body")
    expect(body).to_contain_text("Per label")
    expect(page.locator(".stats-primary .stat").first).to_contain_text("n/a")
    automated = page.locator(".stats-secondary .stat", has_text="Automated")
    expect(automated).to_contain_text(shown(figures.of(50, n)))
    expect(automated).to_contain_text("correct: none of them labelled")  # never "nothing automated"


def test_a_model_is_named_whole_unless_a_composite_joins_its_members(page: Page) -> None:
    """An endpoint's model id can hold colons (a Bedrock version, an Ollama tag); only a
    composite's model is "member:model" parts, as leaderboard.config_label reads it."""
    open_agent(page, TRIAGE)
    named = page.evaluate(
        """async () => {
          const manifest = await import(new URL("/static/manifest.js", location).href);
          const name = (variant, type, model) => manifest.providerName({ variant, type, model });
          return [
            name("llm", "llm", "anthropic.claude-sonnet-5-20261001-v1:0"),
            name("llm", "llm", "llama3.1:8b"),
            name("rules_and_llm", "check", "rules:rules@1a2b + llm:vendor/model-2"),
            name("checked", "check", "agent:vendor/model-3 + second:rules@1a2b"),
            name("rules", "rules", "rules@1a2b"),
          ];
        }"""
    )
    assert named == [
        "anthropic.claude-sonnet-5-20261001-v1:0",
        "llama3.1:8b",
        "Rules + model-2",
        "checked (model-3)",
        "Rules",
    ]


def test_the_picker_groups_every_origin_and_leaves_no_agent_out(page: Page) -> None:
    """With agents from several origins, the picker groups them by origin: one from an
    organization's store has its group, and one of an origin the console does not know is in a
    group of its own, never dropped."""
    open_agent(page, TRIAGE)
    grouped = page.evaluate(
        """async () => {
          const manifest = await import(new URL("/static/manifest.js", location).href);
          const agent = (id, origin) => ({ id, name: id, version: "1.0.0", origin });
          const box = document.createElement("select");
          box.innerHTML = manifest.agentOptions([
            agent("a/maintained", "maintained"), agent("a/pulled", "hub"),
            agent("a/stored", "store"), agent("a/local", "local"), agent("a/new", "elsewhere"),
          ]);
          return [...box.querySelectorAll("optgroup")].map((group) =>
            [group.label, [...group.querySelectorAll("option")].map((o) => o.value)]);
        }"""
    )
    assert grouped == [
        ["Maintained", ["a/maintained"]],
        ["From a registry", ["a/pulled"]],
        ["From this organization's store", ["a/stored"]],
        ["Local agents", ["a/local"]],
        ["elsewhere", ["a/new"]],
    ]


def test_the_leaderboard_remembers_the_dataset(page: Page) -> None:
    open_agent(page, TRIAGE, "evals")
    page.get_by_role("button", name=re.compile(r"^critical n=")).click()
    expect(page.locator("#board caption")).to_contain_text("critical · n=")
    page.reload()
    critical_set = page.get_by_role("button", name=re.compile(r"^critical n="))
    expect(critical_set).to_have_attribute("aria-pressed", "true")


def test_evaluation_views_have_their_own_urls(page: Page) -> None:
    open_agent(page, TRIAGE, "evals")
    page.get_by_role("link", name="Run on a dataset").click()
    expect(page).to_have_url(re.compile("#evals/run$"))
    expect(page.get_by_label("Dataset")).to_be_visible()
    page.get_by_role("link", name="Benchmark report").click()
    expect(page.locator("#evals-body")).to_contain_text("Per label")
    page.goto("/#evals/history")
    expect(page.get_by_role("link", name="History")).to_have_attribute("aria-current", "page")


def test_subgroups_are_grouped_lowest_first(page: Page) -> None:
    open_agent(page, TRIAGE, "evals")
    page.goto("/#evals/report")
    groups = page.locator("details.group")
    expect(groups.first).to_be_visible()
    groups.first.locator("summary").click()
    accuracies = groups.first.locator("tbody td:nth-child(3)").all_inner_texts()
    pairs = [re.match(r"(\d+) of (\d+) \[", a) for a in accuracies]
    values = [int(m.group(1)) / int(m.group(2)) for m in pairs if m]
    assert len(values) == len(accuracies) and values == sorted(values)


def test_the_leaderboard_keeps_the_configuration_in_view_on_a_phone(page: Page) -> None:
    page.set_viewport_size(PHONE)
    open_agent(page, TRIAGE, "evals")
    container = page.locator("#board .table-scroll").first
    assert container.evaluate("el => el.scrollWidth > el.clientWidth")  # it scrolls sideways...
    for selector in ["thead th:first-child", "tbody tr:first-child td.config-cell"]:
        cell = container.locator(selector)
        assert cell.evaluate("el => getComputedStyle(el).position") == "sticky", selector
    container.evaluate("el => el.scrollBy(400, 0)")
    box = container.locator("tbody tr:first-child td.config-cell").bounding_box()
    edge = container.bounding_box()
    assert box is not None and edge is not None
    assert abs(box["x"] - edge["x"]) < 2  # ...but the configuration stays at its left edge


# -- charts ---------------------------------------------------------------------------------------

DAILY = 'figure.chart[data-chart="daily"]'

# Text inside a chart that assistive technology would read outside the marks' own labels.
DECORATION_EXPOSED = """(figure) => [...figure.querySelectorAll('svg.chart-svg[role=group] text')]
  .filter((t) => !t.closest('[aria-hidden="true"]') && !t.closest('.mark'))
  .map((t) => t.textContent)"""


def serve_daily(page: Page, days: list[tuple[str, int]]) -> None:
    """Answer /api/stats with the given history: (Eastern date, decisions that day)."""
    serve_history(page, [{"date": day, "AUTO": n} for day, n in days])


def serve_history(page: Page, days: list[dict[str, Any]]) -> None:
    """Answer /api/stats with the given per-day entries (missing counts are 0)."""
    counts = ["AUTO", "HUMAN_REVIEW", "ABSTAIN", "BLOCK", "reviewed", "overridden", "labelled"]

    def handle(route: Route) -> None:
        body = route.fetch().json()
        body["daily"] = [{**dict.fromkeys([*counts, "agreed"], 0), **day} for day in days]
        # The periods, as the server works them out from those days (figures.periods).
        today = datetime.now(ZoneInfo("America/New_York")).date()
        body["periods"] = figures.periods(body["daily"], today)
        route.fulfill(json=body)

    page.route("**/api/stats*", handle)


def eastern_days_ago(days: int) -> str:
    return (datetime.now(ZoneInfo("America/New_York")).date() - timedelta(days=days)).isoformat()


def test_the_daily_chart_starts_at_the_first_decision(page: Page) -> None:
    """A young deployment's chart is not a run of empty days: it starts at the first decision, and
    the days in between with nothing to show are real zeros."""
    serve_daily(page, [(eastern_days_ago(8), 5), (eastern_days_ago(1), 2)])
    open_agent(page, TRIAGE)
    marks = page.locator(f"{DAILY} .mark")
    expect(marks).to_have_count(9)  # eight days ago .. today; nothing before the first decision
    for mark, total in zip(marks.all(), [5, 0, 0, 0, 0, 0, 0, 2, 0], strict=True):
        expect(mark).to_have_attribute("aria-label", re.compile(f"Total {total}$"))
    expect(page.locator(DAILY)).to_contain_text("Since the first decision")
    expect(page.locator(f"{DAILY} svg.chart-svg")).to_have_attribute(
        "aria-label", re.compile(r"^Decisions per day, .* peak 5 on ")
    )


def test_the_daily_chart_shows_the_last_30_days_and_the_table_shows_all(page: Page) -> None:
    serve_daily(page, [(eastern_days_ago(back), 1) for back in range(44, -1, -1)])
    open_agent(page, TRIAGE)
    expect(page.locator(f"{DAILY} .mark")).to_have_count(30)
    expect(page.locator(DAILY)).to_contain_text("The last 30 days")
    expect(page.locator(f"{DAILY} details.chart-data tbody tr")).to_have_count(45)


def test_chart_marks_are_reached_and_read_with_the_keyboard(page: Page) -> None:
    open_agent(page, TRIAGE, "evals")
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
    expect(tip).to_contain_text("Would be automated")
    page.keyboard.press("End")
    expect(marks.nth(8)).to_be_focused()
    expect(marks.nth(8)).to_have_attribute("tabindex", "0")
    expect(marks.first).to_have_attribute("tabindex", "-1")
    page.keyboard.press("Escape")
    expect(tip).to_be_hidden()
    marks.nth(2).hover()
    expect(tip).to_contain_text("Automate at ≥ 0.70")
    # The label a screen reader gets carries the same exact values.
    expect(marks.nth(2)).to_have_attribute(
        "aria-label",
        re.compile(r"Would be automated \d+ of \d+ \[.*%\], Of those, correct \d+ of \d+ \["),
    )


def test_every_chart_has_a_summary_and_its_data_table(page: Page) -> None:
    serve_daily(page, [(eastern_days_ago(9), 3), (eastern_days_ago(0), 4)])
    open_agent(page, TRIAGE)
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


# The chart fixture has the longest question names (e.g. in its report's per-question stats).
@pytest.mark.parametrize("agent_id", AGENTS)
def test_charts_fit_a_phone_and_keep_their_text_readable(page: Page, agent_id: str) -> None:
    page.set_viewport_size(PHONE)
    open_agent(page, agent_id)
    for target in ["#overview", "#evals", "#evals/report", "#try"]:
        page.goto(f"/{target}")
        expect(page.locator("main h1")).to_be_visible()
        expect(page.locator("#view")).not_to_have_attribute("aria-busy", "true")
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
    serve_daily(page, [(eastern_days_ago(9), 3), (eastern_days_ago(0), 4)])
    open_agent(page, TRIAGE)
    column = page.locator(f"{DAILY} .seg.f-AUTO").first
    light = column.evaluate("el => getComputedStyle(el).fill")
    page.get_by_role("button", name="Dark").click()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    assert column.evaluate("el => getComputedStyle(el).fill") != light


def chart_name(cell: str) -> str:
    """A configuration as the cost chart names it, from its cell in the leaderboard's table: the
    slots that differ between rows, a line each there ("Router: Rules"), joined, without the
    row's standing or the "Deployed" mark."""
    slots = [line.removesuffix(" Deployed") for line in cell.split("\n") if ": " in line]
    return ", ".join(slots)


def test_the_cost_chart_rings_the_deployed_configuration(page: Page, served_agents: Path) -> None:
    serve_board(page, TRIAGE, counted_board(served(served_agents, TRIAGE), priced=True))
    open_agent(page, TRIAGE, "evals")
    chart = page.locator('figure.chart[data-chart^="cost"]')
    configs = page.locator("#board table.data").first.locator("tbody .config").all_inner_texts()
    expect(chart.locator(".mark").first).to_be_visible()
    names = " | ".join(chart.locator(".mark").evaluate_all("els => els.map((e) => e.ariaLabel)"))
    # Every leaderboard row is on the chart (rows at the same point share one named mark); under
    # each name, its standing (too few positives here to rank).
    assert all(c.endswith("\ninsufficient evidence") for c in configs)
    assert all(chart_name(c) in names for c in configs)
    expect(chart.locator("svg .ring")).to_have_count(1)  # locally the keyword rules are deployed
    expect(chart.locator("svg text", has_text="$0").first).to_be_visible()  # rules cost nothing
    expect(chart.locator(".mark").first).to_have_attribute(
        "aria-label",
        re.compile(r"Accuracy \d+ of \d+ \[.*Urgency: yes caught \d+ of \d+ \["),
    )


def test_the_cost_chart_names_the_deployed_and_the_unbeaten_never_the_first(
    page: Page, served_agents: Path
) -> None:
    """The cost chart names beside its point the deployed configuration and every one no other
    beats on both cost and accuracy (LP-17g): the first in the listing order is not named for
    that alone. Here the first is made the dearest and no more accurate than a cheaper row, and
    the deployed keyword rules are beaten too: the first is named in its tooltip only, the
    deployed one beside its point."""
    board = counted_board(served(served_agents, TRIAGE), positives=120, priced=True)
    group = board_group(board, "test")
    rows = [r for r in board["rows"] if r["group"] == group]
    first = next(r for r in rows if r["rank"] == 1)
    cheaper = next(r for r in rows if r["rank"] == 3)
    deployed = next(r for r in rows if {p["variant"] for p in r["providers"].values()} == {"rules"})
    beats_deployed = next(
        r
        for r in rows
        if (r["providers"]["router"]["variant"], r["providers"]["screen"]["variant"])
        == ("rules", "repeated")
    )
    assert first["cost_per_decision_usd"] > cheaper["cost_per_decision_usd"]
    assert len({id(r) for r in (first, cheaper, deployed, beats_deployed)}) == 4
    dearest = 10 * max(r["cost_per_decision_usd"] for r in rows)
    first.update(
        cost_per_decision_usd=dearest,
        figures={**first["figures"], "accuracy": cheaper["figures"]["accuracy"]},
    )
    deployed.update(
        cost_per_decision_usd=2 * beats_deployed["cost_per_decision_usd"],
        figures={**deployed["figures"], "accuracy": beats_deployed["figures"]["accuracy"]},
    )
    serve_board(page, TRIAGE, board)
    open_agent(page, TRIAGE, "evals")
    page.get_by_role("button", name=board["groups"][group]["label"]).click()
    table = page.locator("#board table.data").first

    def plain(config: str) -> str:  # the configuration in the words the chart uses
        return chart_name(table.locator(f'tbody .config[title="{config}"]').inner_text())

    chart = page.locator(f'figure.chart[data-chart="cost-{group}"]')
    expect(chart.locator(".mark").first).to_be_visible()
    labels = chart.locator("svg text.direct-label").evaluate_all(
        "els => els.map((e) => e.textContent)"
    )
    marks = chart.locator(".mark").evaluate_all("els => els.map((e) => e.ariaLabel)")
    assert plain(first["config"]) not in labels  # the first in the order, beaten: not named
    assert any(m.startswith(f"{plain(first['config'])}: ") for m in marks)  # but in its tooltip
    assert plain(deployed["config"]) in labels  # the deployed one, beaten or not
    assert plain(cheaper["config"]) in labels  # unbeaten: no row is cheaper and as accurate
    expect(chart.locator("svg .ring")).to_have_count(1)


# Messages of the triage fixture's: an abusive one (blocked by its rule `abusive-message`), and a
# refill.
ABUSIVE = "You people are useless. Can you send a refill of my metformin to the pharmacy?"
REFILL = "Hi, I need a refill on my lisinopril. I have about three pills left."


def engine_detail(page: Page, body: str) -> None:
    """Decide the message `body` on the triage fixture in Try it, then open its record, where
    the engine's charts are."""
    open_agent(page, TRIAGE, "try")
    page.locator("#body").fill(body)
    page.get_by_role("button", name="Decide").click()
    page.get_by_role("link", name=re.compile("^Engine detail")).click()
    expect(page.locator(".drawer")).to_be_visible()


def test_probability_bars_are_coloured_by_what_the_label_does(page: Page) -> None:
    abusive = '.drawer figure.chart[data-chart$="-abusive"] .chosen rect'
    engine_detail(page, ABUSIVE)
    expect(page.locator(f"{abusive}.f-block")).to_have_count(1)  # a block is danger, not green
    expect(page.locator('.drawer figure.chart[data-chart$="-abusive"] svg')).to_have_attribute(
        "aria-label",
        re.compile(
            r"^Is the message abusive towards the people who read it\? yes \d.*Chosen: yes\.$"
        ),
    )
    engine_detail(page, REFILL)
    expect(page.locator(f"{abusive}.f-c1")).to_have_count(1)
    expect(page.locator(f"{abusive}.f-block")).to_have_count(0)


def test_the_confusion_matrix_shades_only_the_mistakes(page: Page) -> None:
    open_agent(page, TRIAGE, "evals")
    page.goto("/#evals/report")
    page.get_by_text("Confusion matrix").click()
    table = page.locator("table.data", has_text="True label")
    expect(table.locator("thead th").first).to_contain_text("True label")
    diagonal = table.locator("tbody tr").first.locator("td").nth(1)
    expect(diagonal.locator(".cell-warn, .cell-bad")).to_have_count(0)  # right answers stay plain
    assert table.locator(".cell-warn, .cell-bad").count() > 0  # front_desk -> billing, and so on
    expect(table.locator("thead th").last).to_have_text("No answer")  # not a label called "none"


def test_the_threshold_sweep_does_not_claim_automation_that_never_happens(page: Page) -> None:
    """At risk level 2 the clinical review automates nothing; the sweep is a what-if and says
    so."""
    open_agent(page, CLINICAL, "evals")
    page.goto("/#evals/report")
    sweep = page.locator('figure.chart[data-chart^="sweep"]')
    expect(sweep).to_contain_text("Automation threshold 0.90")
    expect(sweep).to_contain_text("Nothing was automated in this run")
    expect(sweep).not_to_contain_text("automates at")

    def automating(route: Route) -> None:  # the triage benchmark with an agent in the screen
        body = route.fetch().json()
        body["benchmark"]["decisions"]["outcome_rates"]["AUTO"] = 0.91
        route.fulfill(json=body)

    page.route(f"**/api/agents/{TRIAGE}", automating)
    page.select_option("#agent-select", TRIAGE)
    expect(page.locator("#evals-body")).to_contain_text("Destination accuracy")
    expect(page.locator('figure.chart[data-chart^="sweep"]')).not_to_contain_text("Nothing was")


# -- screens (Phase 4) ----------------------------------------------------------------------------


def decide_triage(base: str, body: str) -> dict[str, Any]:
    response = httpx.post(
        f"{base}/api/agents/{TRIAGE}/decide", json={"message": {"body": body}}, timeout=30
    )
    return dict(response.json())


def test_the_overview_leads_with_what_people_act_on(page: Page) -> None:
    open_agent(page, TRIAGE)
    deployed = page.locator(".agent-header .deployed")
    expect(deployed).to_contain_text("Screen: Rules")  # words first...
    expect(deployed).to_contain_text("Router: Rules")
    expect(page.locator(".agent-header .mono")).to_contain_text("router=rules")  # codes below
    primary = page.locator(".stats-primary .stat")
    expect(primary).to_have_count(4)
    waiting = primary.nth(1)
    expect(waiting).to_contain_text("Waiting for review")
    expect(waiting).to_have_class(re.compile("tone-warning"))  # the seeded chest pain waits
    waiting.get_by_role("link", name=re.compile("review queue")).click()
    expect(page).to_have_url(re.compile("#review$"))


def test_only_administrators_are_offered_the_chain_verification(page: Page) -> None:
    """Verifying the whole chain needs `admin` on the server; a viewer's dashboard neither asks
    for it nor offers it, and still renders."""
    viewer = {"kind": "user", "name": "Viewer", "roles": ["viewer"], "permissions": ["read"]}
    me = {"mode": "sign-in", "signed_in": True, "principal": {**viewer, "org": "default"}}
    page.route("**/api/auth/me", lambda route: route.fulfill(json=me))
    asked: list[str] = []
    page.on("request", lambda r: asked.append(r.url) if "/api/audit/verify" in r.url else None)
    open_agent(page, TRIAGE)
    expect(page.locator(".stats-primary .stat")).to_have_count(4)
    expect(page.locator(".stats-secondary")).not_to_contain_text("Audit chain")
    show(page, "audit")
    expect(page.locator("#audit-table")).to_be_visible()
    expect(page.locator("#verify")).to_have_count(0)
    expect(page.locator("#export-fhir")).to_have_count(0)  # the export is administrators' too
    assert asked == []


def test_the_audit_trail_exports_as_fhir_and_says_the_checkpoint_is_not_set_up(
    page: Page,
) -> None:
    """Open mode, no MENDARI_CHECKPOINT_KEY: the checkpoint is one quiet line, and the agent's
    trail downloads as FHIR NDJSON named for the agent and the deployment's day."""
    open_agent(page, TRIAGE, "audit")
    expect(page.locator("#checkpoint")).to_have_text(
        "Signed daily checkpoint: not set up on this deployment."
    )
    with page.expect_download() as saved:
        page.get_by_role("button", name="Export as FHIR").click()
    day = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    assert saved.value.suggested_filename == f"audit-mendari-test-triage-{day}.ndjson"
    exported = [json.loads(line) for line in saved.value.path().read_text().splitlines()]
    assert {"AuditEvent", "Provenance"} >= {r["resourceType"] for r in exported}
    assert any(r["resourceType"] == "AuditEvent" for r in exported)


def test_an_agent_without_decisions_says_what_to_do(page: Page) -> None:
    def no_decisions(route: Route) -> None:
        route.fulfill(json={**route.fetch().json(), "decisions": 0})

    page.route("**/api/stats*", no_decisions)
    open_agent(page, TRIAGE)
    expect(page.locator(".stats-primary")).to_have_count(0)  # no wall of zeros
    empty = page.locator(".empty-state")
    expect(empty).to_contain_text("No decisions yet")
    expect(empty.get_by_role("link", name="Try it")).to_have_attribute("href", "#try")


def test_stat_reviews_come_first_and_focus_moves_to_the_next_card(
    page: Page, live_server: str
) -> None:
    for _ in range(2):
        decide_triage(live_server, "Crushing chest pain and my left arm is numb")
    open_agent(page, TRIAGE, "review")
    expect(page.locator(".review-group h2").first).to_contain_text("Stat")
    cards = page.locator(".review-card")
    first, second = cards.nth(0), cards.nth(1)
    expect(first.locator(".stat-banner")).to_contain_text("Possible same-day symptoms")
    first_id, second_id = first.get_attribute("data-id"), second.get_attribute("data-id")
    confirm = first.get_by_role("button", name=re.compile("^Confirm Nurse"))
    confirm.focus()
    page.keyboard.press("Enter")  # a keyboard reviewer confirms...
    expect(page.locator(f'.review-card[data-id="{first_id}"]')).to_have_count(0)
    expect(page.locator(f'.review-card[data-id="{second_id}"]')).to_be_focused()  # carries on


def test_the_result_leads_with_the_outcome_and_takes_focus(page: Page) -> None:
    open_agent(page, TRIAGE, "try")
    page.locator("#body").fill(ABUSIVE)
    page.get_by_role("button", name="Decide").click()
    title = page.locator("#result-title")
    expect(title).to_have_text(re.compile(r"^Blocked"))
    expect(title).to_be_focused()
    expect(page.locator("#result .callout.danger")).to_contain_text(
        "The front desk reads the message."
    )
    # Answers read as the agent's names; each code is one click away ("Show codes").
    answer = page.locator("#result .answers dt", has_text="Abusive")
    code = answer.locator(".code-ref code")
    expect(code).to_have_text("abusive")
    expect(code).to_be_hidden()
    toggle = page.get_by_role("button", name="Show codes")
    toggle.click()
    expect(code).to_be_visible()
    expect(toggle).to_have_attribute("aria-pressed", "true")  # a toggle keeps its name
    expect(answer).to_contain_text("Abusive abusive")


def test_a_wrong_rating_takes_the_right_answer_and_can_be_changed(
    page: Page, live_server: str
) -> None:
    decision = decide_triage(live_server, REFILL)
    page.add_init_script("localStorage.setItem('oh.reviewer', 'browser-rater')")
    open_agent(page, TRIAGE, "audit")
    page.locator(f'#audit-table button.open-record[data-id="{decision["id"]}"]').click()
    dialog = page.get_by_role("dialog")
    wrong, right = (dialog.get_by_role("button", name=n, exact=True) for n in ("Wrong", "Right"))
    wrong.click()
    expect(wrong).to_have_attribute("aria-pressed", "true")
    choice = dialog.locator(".fb-labels:not([hidden]) button").first
    label = choice.inner_text()
    assert label != decision["recommendation"]  # the answer it gave is not offered
    choice.click()
    dialog.get_by_role("button", name="Save").click()
    status = dialog.locator(".fb-status")
    expect(status).to_have_text(f"Recorded as wrong: destination should be {label}.")
    row = page.locator(f'#audit-table tr[data-id="{decision["id"]}"]')
    expect(row.locator(".rated.down")).to_be_visible()  # the table behind shows it at once

    page.keyboard.press("Escape")
    page.goto(f"/#audit/{decision['id']}")  # reopened: this person's rating is remembered
    expect(wrong).to_have_attribute("aria-pressed", "true")
    expect(status).to_have_text(f"Recorded as wrong: destination should be {label}.")
    right.click()  # changed their mind
    expect(right).to_have_attribute("aria-pressed", "true")
    expect(wrong).to_have_attribute("aria-pressed", "false")
    expect(row.locator(".rated.up")).to_be_visible()
    expect(row.locator(".rated.down")).to_have_count(0)
    current = httpx.get(f"{live_server}/api/feedback", params={"agent_id": TRIAGE}).json()
    mine = [f for f in current if f["decision_id"] == decision["id"]]
    assert [(f["reporter"], f["rating"]) for f in mine] == [("browser-rater", "up")]


def test_the_feedback_page_leads_with_the_cases_to_act_on(page: Page, live_server: str) -> None:
    decision = decide_triage(live_server, REFILL)
    labels = httpx.get(f"{live_server}/api/agents/{TRIAGE}").json()["labels"]
    other = next(label for label in labels if label != decision["recommendation"])
    expected = {"question": "destination", "label": other}
    httpx.post(
        f"{live_server}/api/audit/{decision['id']}/feedback",
        json={"rating": "down", "reporter": "page-test", "expected": expected},
    )
    open_agent(page, TRIAGE, "feedback")
    ready = page.locator(".stats-primary .stat", has_text="Ready to become tests")
    expect(ready.locator(".stat-value")).not_to_have_text("0")
    card = page.locator(".feedback-card", has=page.locator(f'a[href="#audit/{decision["id"]}"]'))
    expect(card).to_contain_text(f"destination should be {other}")
    card.get_by_role("link", name=re.compile("Open the decision")).click()
    expect(page.get_by_role("dialog")).to_contain_text(decision["id"])


def test_a_note_shows_to_whoever_may_read_it_and_is_sealed_for_others(
    page: Page, live_server: str
) -> None:
    """Notes are kept apart from the audit chain and follow inputs: the drawer and the feedback
    page show a note to whoever may read it (PHI mode off here) and only say "sealed" to anyone
    else, never the text, in the event or in its payload."""
    decision = decide_triage(live_server, REFILL)
    note = "Wanted a 90-day supply"
    httpx.post(
        f"{live_server}/api/audit/{decision['id']}/feedback",
        json={"rating": "down", "reporter": "note-test", "note": note},
    )
    page.add_init_script(f"localStorage.setItem('oh.agent', {json.dumps(TRIAGE)})")
    page.goto(f"/#audit/{decision['id']}")
    rated = page.get_by_role("dialog").locator(".timeline li", has_text="Feedback recorded")
    expect(rated.locator(".event-note")).to_have_text(f"Note: {note}")
    show(page, "feedback")
    card = page.locator(".feedback-card", has=page.locator(f'a[href="#audit/{decision["id"]}"]'))
    expect(card).to_contain_text(f"Note: {note}")

    def sealed(route: Route) -> None:  # what a viewer gets under PHI mode encrypt
        body = route.fetch().json()
        items = body["events"] if isinstance(body, dict) else body
        for item in items:
            if item.get("note_status"):
                item.update(note_status="sealed", note=None)
        route.fulfill(json=body)

    page.route(f"**/api/audit/{decision['id']}", sealed)
    page.route("**/api/feedback?*", sealed)
    show(page, "feedback")
    expect(card).to_contain_text("Note: only reviewers and admins can read it.")
    expect(card).not_to_contain_text(note)
    page.goto(f"/#audit/{decision['id']}")
    dialog = page.get_by_role("dialog")
    expect(rated.locator(".event-note")).to_have_text(
        "Note (only reviewers and admins can read it)"
    )
    expect(dialog).not_to_contain_text(note)


def test_phi_mode_hash_offers_no_note_field(page: Page) -> None:
    """PHI mode hash keeps no free text (the server refuses a note), so there is none to fill."""

    def hashing(route: Route) -> None:
        route.fulfill(json={**route.fetch().json(), "phi_mode": "hash"})

    page.route("**/api/health", hashing)
    open_agent(page, TRIAGE, "try")
    page.get_by_role("button", name="Decide").click()
    page.locator("#result").get_by_role("button", name="Wrong").click()
    expect(page.locator("#result .fb-save")).to_be_visible()
    expect(page.locator("#result .fb-note")).to_have_count(0)


def test_the_decision_card_is_a_document_with_linked_sections(page: Page) -> None:
    open_agent(page, TRIAGE, "card")
    page.get_by_role("navigation", name="On this page").get_by_role("link", name="Policy").click()
    expect(page).to_have_url(re.compile("#card/policy$"))
    heading = page.get_by_role("heading", name="Policy", exact=True)
    expect(heading).to_be_focused()
    expect(page.locator("#card-policy")).to_contain_text(
        "When the chance that urgency is yes is at least 0.35."
    )
    page.reload()  # a link to a section lands there
    expect(heading).to_be_focused()
    expect(page.locator("#card-schemas details")).to_have_count(2)  # schemas folded away


@pytest.mark.parametrize(
    ("agent_id", "automates"),
    [
        (TRIAGE, "Not with the deployed providers: automation needs screen"),
        (CLINICAL, "Not without an approval: at risk level 2 a person reviews every decision"),
        (CHART, "At confidence 0.90 or more"),
    ],
)
def test_the_card_says_whether_this_deployment_automates(
    page: Page, agent_id: str, automates: str
) -> None:
    """The thresholds alone would say "automates at 0.90" for agents that never automate here;
    one whose canned answers automate in open mode says so."""
    open_agent(page, agent_id, "card")
    expect(page.locator("dl.facts")).to_contain_text(automates)


def test_with_a_model_in_the_screen_the_card_says_the_inbox_automates(page: Page) -> None:
    def screened(route: Route) -> None:
        body = route.fetch().json()
        agent = {"variant": "agent", "type": "agent", "model": "triage-screen"}
        body["active_providers"]["screen"] = agent
        route.fulfill(json=body)

    page.route(f"**/api/agents/{TRIAGE}", screened)
    open_agent(page, TRIAGE, "card")
    expect(page.locator("dl.facts")).to_contain_text(
        "At confidence 0.90 or more (some labels need more, some are never automated; see Policy)."
    )


def test_the_overview_names_the_slots_running_without_a_model(page: Page) -> None:
    open_agent(page, TRIAGE)
    note = page.get_by_role("note").filter(has_text="Running without a model")
    expect(note).to_contain_text("Running without a model: Router, Screen.")
    open_agent(page, CLINICAL)
    note = page.get_by_role("note").filter(has_text="Running without a model")
    expect(note).to_contain_text("Running without a model: Reader.")


def test_the_digest_copies(page: Page) -> None:
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    open_agent(page, TRIAGE, "card")
    page.get_by_role("button", name="Copy the digest").click()
    expect(page.locator("#toast")).to_have_text("Digest copied")
    assert page.evaluate("navigator.clipboard.readText()").startswith("sha256:")


def test_a_digest_that_cannot_be_copied_says_what_to_select(page: Page) -> None:
    """The card names what to select by hand; copyText dropped that message for a generic one."""
    page.add_init_script(
        "navigator.clipboard.writeText = () => Promise.reject(new Error('denied'))"
    )
    open_agent(page, TRIAGE, "card")
    page.get_by_role("button", name="Copy the digest").click()
    expect(page.locator("#toast")).to_have_text(
        "Copying isn't available here: select the digest and copy it"
    )


def test_an_evaluation_that_calls_remote_models_asks_first(page: Page) -> None:
    def remote(route: Route) -> None:
        body = route.fetch().json()
        llm = {"variant": "llm", "type": "llm", "model": "vendor/model-2"}
        body["active_providers"]["router"] = llm
        route.fulfill(json=body)

    runs: list[str] = []
    page.on("request", lambda r: runs.append(r.url) if r.url.endswith("/evals") else None)
    page.route(f"**/api/agents/{TRIAGE}", remote)
    open_agent(page, TRIAGE, "evals")
    page.goto("/#evals/run")
    run = page.get_by_role("button", name="Run evaluation")
    run.click()
    dialog = page.get_by_role("dialog", name="Run this evaluation?")
    expect(dialog).to_contain_text(re.compile(r"all \d+ records of local-site-example"))
    expect(dialog).to_contain_text("Router: model-2")
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_have_count(0)
    expect(run).to_be_focused()
    assert runs == []  # nothing ran


def test_an_evaluation_that_runs_locally_needs_no_confirmation(page: Page) -> None:
    open_agent(page, TRIAGE, "evals")
    page.goto("/#evals/run")
    expect(page.locator("#evals-body")).to_contain_text("runs locally: no remote calls")
    page.get_by_role("button", name="Run evaluation").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    expect(page.locator("#run-report .stats-primary")).to_be_visible(timeout=60_000)


# -- QA pass (Phase 5) ----------------------------------------------------------------------------

# Every visible heading in the view, in order; one h1, and no level skipped on the way down.
HEADING_PROBLEMS = """() => {
  const shown = (el) => el.getClientRects().length > 0;
  const hs = [...document.querySelectorAll('main :is(h1, h2, h3, h4)')].filter(shown);
  const problems = [];
  if (hs.filter((h) => h.tagName === 'H1').length !== 1) problems.push('h1 count');
  let previous = 1;
  for (const h of hs) {
    const level = Number(h.tagName[1]);
    const text = h.textContent.trim().slice(0, 30);
    if (level > previous + 1) problems.push(`h${previous} -> h${level}: ${text}`);
    previous = level;
  }
  const tables = [...document.querySelectorAll('main table')].filter(shown);
  const bare = tables.filter((t) => !t.querySelector('th'));
  if (bare.length) problems.push(`${bare.length} tables without headers`);
  return problems;
}"""


def test_keyboard_users_can_skip_to_the_content(page: Page) -> None:
    open_agent(page, TRIAGE, "feedback")
    page.evaluate("document.activeElement.blur()")
    page.goto(f"/?agent={TRIAGE}#feedback")  # the same document: tabbing starts from the top
    page.keyboard.press("Tab")
    skip = page.get_by_role("button", name="Skip to content")
    expect(skip).to_be_focused()
    assert (skip.bounding_box() or {"y": -1})["y"] >= 0  # shown while focused
    page.keyboard.press("Enter")
    expect(page.locator("main h1")).to_be_focused()


@pytest.mark.parametrize("agent_id", [TRIAGE, CLINICAL])
def test_headings_are_in_order_and_tables_have_headers(page: Page, agent_id: str) -> None:
    """What a screen reader navigates by: one h1 per view, no skipped heading level, and every
    table says what its columns are."""
    open_agent(page, agent_id)
    for view in VIEWS:
        show(page, view)
        assert page.evaluate(HEADING_PROBLEMS) == [], view
    page.goto("/#evals/report")
    expect(page.locator("#evals-body .stats-primary")).to_be_visible()
    assert page.evaluate(HEADING_PROBLEMS) == [], "evals/report"
    expect(page.locator('.nav a[aria-current="page"]')).to_have_text("Evaluations")


def test_after_a_blocked_result_tab_reaches_its_controls_with_a_ring(page: Page) -> None:
    """The solid red header holds the headline only; the first Tab after it reaches the result's
    own controls (the codes, then the record), each with a visible focus ring."""
    open_agent(page, TRIAGE, "try")
    page.locator("#body").fill(ABUSIVE)
    page.get_by_role("button", name="Decide").click()
    expect(page.locator("#result-title")).to_be_focused()
    expect(page.locator("#result .result-head :is(a, button)")).to_have_count(0)
    page.keyboard.press("Tab")
    codes = page.get_by_role("button", name="Show codes")
    expect(codes).to_be_focused()
    assert codes.evaluate("el => getComputedStyle(el).outlineStyle") != "none"


def test_a_view_that_fails_to_load_says_so_and_can_retry(page: Page) -> None:
    page.route(
        "**/api/stats*",
        lambda route: route.fulfill(status=503, json={"detail": "database is waking up"}),
    )
    open_agent(page, TRIAGE, "review")
    page.goto("/#overview")
    failure = page.get_by_role("alert").filter(has_text="Could not load this view")
    expect(failure).to_contain_text("database is waking up")
    page.unroute("**/api/stats*")
    page.get_by_role("button", name="Try again").click()
    expect(page.locator(".stats-primary")).to_be_visible()


def test_errors_are_announced_at_once(page: Page) -> None:
    open_agent(page, TRIAGE, "review")
    page.fill("#reviewer", "")
    page.locator(".review-card button.primary").first.click()
    expect(page.locator("#announce-alert")).to_have_text("Enter a reviewer name first")
    expect(page.locator("#toast")).to_have_class(re.compile("error"))
    expect(page.locator("#reviewer")).to_be_focused()


def test_the_phone_section_list_shows_where_you_are(page: Page) -> None:
    page.set_viewport_size(PHONE)
    open_agent(page, TRIAGE, "card")  # near the end of the list
    nav = page.locator(".nav")
    active = nav.locator("a.active")
    expect(active).to_have_text("Decision Card")
    inside = nav.evaluate(
        """(nav) => { const a = nav.querySelector('a.active').getBoundingClientRect();
           const n = nav.getBoundingClientRect();
           return a.left >= n.left && a.right <= n.right; }"""
    )
    assert inside  # scrolled into the strip, not off to the right
    expect(nav).to_have_class(re.compile("scrolled"))  # an edge shadow: more to the left


# -- Try it forms for record agents ---------------------------------------------------------------

RECORD_AGENTS = [CHART, CLINICAL]  # the served agents whose input is a record, not a message


def declared(schema: dict[str, Any], value: Any) -> bool:
    """Whether every key of every object in `value` is one its schema takes (a closed object
    takes its properties only): what a form generated from the schema can hold."""
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        if schema.get("additionalProperties") is False and not set(value) <= set(properties):
            return False
        return all(declared(properties.get(key) or {}, item) for key, item in value.items())
    if isinstance(value, list):
        return all(declared(schema.get("items") or {}, item) for item in value)
    return True


def view_button(page: Page, name: str) -> Locator:
    return page.get_by_role("group", name="Edit the input as").get_by_role("button", name=name)


@pytest.mark.parametrize("agent_id", RECORD_AGENTS)
def test_the_form_carries_every_example_unchanged(
    page: Page, live_server: str, agent_id: str
) -> None:
    """Each behaviour-test example goes into the form generated from the agent's input schema and
    comes out as the same record: the form never adds, drops or retypes a value. (An example of a
    field the schema refuses has no field in the form to go into: none of those.)"""
    examples = httpx.get(f"{live_server}/api/agents/{agent_id}/examples").json()
    schema = httpx.get(f"{live_server}/api/agents/{agent_id}").json()["input_schema"]
    held = [index for index, example in enumerate(examples) if declared(schema, example["input"])]
    assert len(held) > 1
    open_agent(page, agent_id, "try")
    expect(page.locator("#record-form [data-path]").first).to_be_visible()  # the form by default
    for index in held:
        example = examples[index]
        page.select_option("#example", str(index))
        view_button(page, "JSON").click()
        assert json.loads(page.input_value("#record")) == example["input"], example["name"]
        view_button(page, "Form").click()


def test_editing_a_form_field_changes_the_decision(page: Page) -> None:
    open_agent(page, CLINICAL, "try")
    page.select_option(
        "#example",
        label="mild pain of two days is care at home, which a person confirms at risk level 2",
    )
    page.get_by_role("button", name="Decide").click()
    expect(page.locator("#result-title")).to_have_text("Sent to review")
    page.get_by_label("Text").fill("Patient reports severe head pain since this morning.")
    page.get_by_role("button", name="Decide").click()
    expect(page.locator("#result-title")).to_have_text("Sent to review · urgent")


def test_rows_are_added_and_removed_from_the_keyboard(page: Page) -> None:
    open_agent(page, CHART, "try")
    rows = page.locator('#record-form [data-array*="conditions"] tbody tr')
    before = rows.count()
    assert before > 0
    page.get_by_role("button", name="Add to conditions").focus()
    page.keyboard.press("Enter")
    new = before + 1
    expect(page.get_by_label(f"Conditions {new}: Code")).to_be_focused()  # typing can start
    page.keyboard.type("I10")
    page.keyboard.press("Tab")
    page.keyboard.type("flag-routine")
    page.keyboard.press("Tab")
    page.keyboard.type("Essential (primary) hypertension")
    view_button(page, "JSON").click()
    added = json.loads(page.input_value("#record"))["conditions"][-1]
    assert added == {
        "code": "I10",
        "flag": "flag-routine",
        "text": "Essential (primary) hypertension",
    }
    view_button(page, "Form").click()
    page.get_by_role("button", name=f"Remove conditions {new}").click()
    expect(rows).to_have_count(before)
    expect(page.get_by_label(f"Conditions {before}: Code")).to_be_focused()


def test_invalid_json_stays_in_the_json_view(page: Page) -> None:
    open_agent(page, CLINICAL, "try")
    view_button(page, "JSON").click()
    page.fill("#record", '{"note": ')
    view_button(page, "Form").click()
    expect(page.locator("#announce-alert")).to_contain_text("Not valid JSON")
    expect(page.locator("#record")).to_be_visible()
    expect(view_button(page, "JSON")).to_have_attribute("aria-pressed", "true")


def test_a_required_field_is_asked_for_before_deciding(page: Page) -> None:
    open_agent(page, CLINICAL, "try")
    decided: list[str] = []
    page.on("request", lambda r: decided.append(r.url) if r.url.endswith("/decide") else None)
    page.get_by_label("Text").fill("")
    page.get_by_role("button", name="Decide").click()
    expect(page.locator("#record-form textarea:invalid")).to_have_count(1)
    assert decided == []  # nothing was sent


def test_the_input_view_is_remembered(page: Page) -> None:
    open_agent(page, CLINICAL, "try")
    view_button(page, "JSON").click()
    page.reload()
    expect(view_button(page, "JSON")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#record")).to_be_visible()
    expect(page.locator("#record-form")).to_be_hidden()


# -- design grade, Phase A (notes/design-grade.md) -------------------------------------------------

LAYOUT_SHIFT = """window.__cls = 0; new PerformanceObserver((list) => {
  for (const e of list.getEntries()) if (!e.hadRecentInput) window.__cls += e.value;
}).observe({ type: 'layout-shift', buffered: true });"""


def test_nothing_shifts_while_a_tablet_page_loads(browser: Browser, base_url: str) -> None:
    """The agent picker used to fill in after first paint, wrap the top bar at 768 px and shift the
    whole page (a layout shift of 0.50)."""
    context = browser.new_context(base_url=base_url, viewport={"width": 768, "height": 900})
    page = context.new_page()
    try:
        page.add_init_script(LAYOUT_SHIFT)
        page.goto("/#overview")
        expect(page.locator(".stats-primary")).to_be_visible()
        page.wait_for_timeout(300)
        assert page.evaluate("window.__cls") < 0.1
    finally:
        context.close()


def test_content_uses_a_wide_screen(page: Page) -> None:
    page.set_viewport_size({"width": 1920, "height": 1000})
    open_agent(page, TRIAGE)
    box = page.locator("main").bounding_box()
    assert box is not None and box["width"] == 1440
    left_gap, right_gap = box["x"] - 200, 1920 - (box["x"] + box["width"])  # beside the 200 px nav
    assert abs(left_gap - right_gap) <= 2  # centred, not stopped at the left


def test_a_zoomed_rate_axis_says_where_it_starts(page: Page, served_agents: Path) -> None:
    """A sweep whose rates all sit within 15 points near the top zooms in and says where its
    axis starts: the triage benchmark, served with such a sweep (synthetic: fewer and fewer of
    its 300 cases automated, nearly all correct). The clinical review's spans most of the scale,
    and its axis starts at 0."""
    report = json.loads(served(served_agents, TRIAGE).attestation(BENCHMARK).read_text())
    for row, chosen, errors in zip(
        report["threshold_sweep"],
        [285, 284, 283, 282, 281, 280, 278, 276, 274],
        [5, 4, 4, 3, 3, 2, 2, 1, 1],
        strict=True,
    ):
        scored = row["scored"]
        row.update(
            chosen=chosen, errors=errors, coverage=chosen / scored, accuracy=1 - errors / chosen
        )

    def zoomed(route: Route) -> None:
        body = route.fetch().json()
        body["benchmark"] = {**report, "figures": figures.guarded(lambda: figures.report(report))}
        route.fulfill(json=body)

    page.route(re.compile(rf".*/api/agents/{re.escape(TRIAGE)}$"), zoomed)
    open_agent(page, TRIAGE, "evals")
    page.goto("/#evals/report")
    sweep = page.locator('figure.chart[data-chart^="sweep"]')
    expect(sweep.locator(".chart-axis-note")).to_have_text("The vertical axis starts at 90%.")
    expect(sweep.locator("path.axis-break")).to_have_count(1)
    page.select_option("#agent-select", CLINICAL)  # it automates nothing: the axis starts at 0
    expect(page.locator('figure.chart[data-chart^="sweep"] .chart-axis-note')).to_be_hidden()


# Visible controls in the view shorter than Apple's 44 pt minimum.
SMALL_TARGETS = """() => {
  const selector = 'main button, main a.button, main select, main summary, '
    + 'main input:not([type=checkbox])';
  return [...document.querySelectorAll(selector)]
    .filter((el) => el.getClientRects().length && el.getBoundingClientRect().height < 44)
    .map((el) => el.outerHTML.slice(0, 60));
}"""


def test_touch_screens_get_44_px_targets(browser: Browser, base_url: str) -> None:
    context = browser.new_context(base_url=base_url, has_touch=True, is_mobile=True, viewport=PHONE)
    page = context.new_page()
    try:
        page.add_init_script(f"localStorage.setItem('oh.agent', {json.dumps(TRIAGE)})")
        for view in ["audit", "evals/report", "review", "try"]:
            page.goto(f"/#{view}")
            expect(page.locator("main h1")).to_be_visible()
            expect(page.locator("#view")).not_to_have_attribute("aria-busy", "true")
            assert page.evaluate(SMALL_TARGETS) == [], view
    finally:
        context.close()


def test_configurations_are_named_once(page: Page, served_agents: Path) -> None:
    """A remote part of a configuration is named by its model: "Rules + model-2", not "Rules +
    LLM" beside a "model-2" chip. The fixtures' configurations run here, so one row is given the
    router of a deployment that calls a model."""
    board = counted_board(served(served_agents, TRIAGE))
    row = next(r for r in board["rows"] if r["group"] == board_group(board, "test"))
    model = "rules:rules@1a2b + llm:vendor/model-2"
    row["providers"]["router"] = {"variant": "rules_and_llm", "type": "check", "model": model}
    serve_board(page, TRIAGE, board)
    open_agent(page, TRIAGE, "evals")
    configs = page.locator("#board table").first.locator("tbody .config").all_inner_texts()
    assert any("Rules + model-2" in c for c in configs)
    assert not any("model-2 model-2" in " ".join(c.split()) for c in configs)
    assert not any("LLM" in c for c in configs)


def test_a_record_drawer_is_titled_in_words(page: Page, live_server: str) -> None:
    decision = httpx.post(
        f"{live_server}/api/agents/{TRIAGE}/decide",
        json={"message": {"body": "Crushing chest pain and my left arm is numb"}},
    ).json()
    page.add_init_script(f"localStorage.setItem('oh.agent', {json.dumps(TRIAGE)})")
    page.goto(f"/#audit/{decision['id']}")
    expect(page.locator("#drawer-title")).to_have_text("Human review · Nurse")


def test_another_agents_record_is_not_replayed_into_the_chosen_one(
    page: Page, live_server: str
) -> None:
    """A decision opened by its id alone (the palette's "Open decision", a bare `#audit/<id>`)
    can be another agent's than the one chosen. Its record says whose it is, shows its labels as
    codes, not in the chosen agent's words, and offers no replay: that would load its input into
    the chosen agent's Try it, where Decide records a decision for the wrong agent. Shown with
    its own agent, it is in that agent's words and replays there."""
    decision = httpx.post(
        f"{live_server}/api/agents/{CLINICAL}/decide",
        json={"note": {"text": "Patient reports mild throat pain for two days."}},
        timeout=30,
    ).json()
    assert decision["recommendation"] == "self_care"  # "Care at home" in its own agent's words
    page.add_init_script(f"localStorage.setItem('oh.agent', {json.dumps(TRIAGE)})")
    page.goto(f"/#audit/{decision['id']}")
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text(decision["id"])
    expect(page.locator("#agent-select")).to_have_value(TRIAGE)
    expect(dialog.get_by_role("button", name="Replay in Try it")).to_have_count(0)
    expect(page.locator("#drawer-title")).to_have_text("Human review · self_care")
    whose = dialog.locator(".record-agent")
    expect(whose).to_contain_text(CLINICAL_NAME)
    whose.get_by_role("button", name=f"Show it with {CLINICAL_NAME}").click()
    expect(page).to_have_url(re.compile(f"\\?agent={CLINICAL}#audit/{decision['id']}$"))
    expect(page.locator("#agent-select")).to_have_value(CLINICAL)
    expect(page.locator("#drawer-title")).to_have_text("Human review · Care at home")
    expect(dialog.locator(".record-agent")).to_have_count(0)
    expect(dialog.get_by_role("button", name="Replay in Try it")).to_be_visible()


def test_another_agents_record_borrows_none_of_the_chosen_agents_words(
    page: Page, live_server: str
) -> None:
    """Two agents can ask the same question (two versions of one agent, a fork): a decision of
    the other one is still shown in its own codes, never in the chosen agent's names for its
    labels and questions. The record is answered here as the triage fixture's, with the clinical
    review's questions."""
    decision = httpx.post(
        f"{live_server}/api/agents/{CLINICAL}/decide",
        json={"note": {"text": "Patient reports mild throat pain for two days."}},
        timeout=30,
    ).json()

    def as_triage(route: Route) -> None:
        found = route.fetch().json()
        found["agent_id"] = TRIAGE
        found["record"]["agent"]["id"] = TRIAGE
        route.fulfill(json=found)

    page.route(f"**/api/audit/{decision['id']}", as_triage)
    page.add_init_script(f"localStorage.setItem('oh.agent', {json.dumps(CLINICAL)})")
    page.goto(f"/#audit/{decision['id']}")
    dialog = page.get_by_role("dialog")
    expect(dialog.locator(".record-agent")).to_contain_text(TRIAGE_NAME)
    expect(page.locator("#drawer-title")).to_have_text("Human review · self_care")
    expect(dialog.locator("h4").first).to_contain_text("Disposition")
    expect(dialog).not_to_contain_text("How soon should the patient")  # the chosen agent's words
    expect(dialog.get_by_role("button", name="Replay in Try it")).to_have_count(0)


# -- Phase B2: tablet layout, phone top bar, loading, run progress, override undo -----------------

TOP_BAR_HEIGHT = "document.querySelector('.topbar').getBoundingClientRect().height"
NAV_DIRECTION = "getComputedStyle(document.querySelector('.nav')).flexDirection"
STAT_ROWS = """() => { const rows = {};
  for (const s of document.querySelectorAll('.stats-primary > .stat')) {
    const top = Math.round(s.getBoundingClientRect().top); rows[top] = (rows[top] || 0) + 1; }
  return Object.values(rows); }"""


@pytest.mark.parametrize("width", [768, 1024])
def test_tablets_get_the_full_width_with_sections_across_the_top(page: Page, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    open_agent(page, TRIAGE)
    assert page.evaluate(NAV_DIRECTION) == "row"
    assert page.evaluate("document.querySelector('main').getBoundingClientRect().width") == width
    assert page.evaluate(TOP_BAR_HEIGHT) <= 56  # one row
    assert page.evaluate(STAT_ROWS) == [2, 2]  # four stats: two rows of two, not three and one
    page.set_viewport_size({"width": 1280, "height": 900})
    assert page.evaluate(NAV_DIRECTION) == "column"
    assert page.evaluate(STAT_ROWS) == [4]


def test_phones_get_a_56_px_top_bar_with_settings_in_a_sheet(page: Page) -> None:
    page.set_viewport_size(PHONE)
    open_agent(page, TRIAGE)
    assert page.evaluate(TOP_BAR_HEIGHT) <= 56
    settings = page.get_by_role("button", name="Settings")
    expect(page.locator("#agent-select")).to_be_hidden()
    settings.click()
    expect(settings).to_have_attribute("aria-expanded", "true")
    expect(page.get_by_text("Agent", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Dark")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#agent-select")).to_be_hidden()
    expect(settings).to_be_focused()
    settings.click()
    page.select_option("#agent-select", CLINICAL)
    expect(settings).to_have_attribute("aria-expanded", "false")  # choosing an agent closes it
    expect(page.locator("main h1")).to_contain_text(CLINICAL_NAME)


def test_a_view_keeps_its_shape_while_it_loads(page: Page) -> None:
    open_agent(page, TRIAGE, "review")
    held: list[Route] = []
    page.route("**/api/stats*", lambda route: held.append(route))
    with page.expect_request("**/api/stats*"):  # held from here on: the view waits for it
        page.goto("/#overview")
    view = page.locator("#view")
    expect(view).to_have_attribute("aria-busy", "true")
    expect(view.locator(".skeleton .sk-stat")).to_have_count(4)
    for _ in range(50):  # the route handler has taken the request (it runs just after the event)
        if held:
            break
        page.wait_for_timeout(20)
    for route in held:  # release what was held
        route.continue_()
    page.unroute("**/api/stats*")
    expect(view).not_to_have_attribute("aria-busy", "true")
    expect(view.locator(".skeleton")).to_have_count(0)
    expect(page.locator(".stats-primary")).to_be_visible()


def test_an_evaluation_run_shows_it_is_alive(page: Page) -> None:
    open_agent(page, TRIAGE, "evals")
    page.goto("/#evals/run")
    held: list[Route] = []
    page.route(
        "**/evals",
        lambda route: held.append(route) if route.request.method == "POST" else route.continue_(),
    )
    page.get_by_role("button", name="Run evaluation").click()
    progress = page.locator(".run-progress")
    expect(progress).to_contain_text(re.compile(r"Running \d+ records of \S+"))
    expect(progress.locator(".elapsed")).to_have_text(re.compile(r"^[1-9]\d* s$"))  # it counts
    expect(page.locator("#dataset")).to_be_disabled()
    for route in held:  # release the run
        route.continue_()
    page.unroute("**/evals")
    expect(page.locator("#run-report .stats-primary")).to_be_visible(timeout=60_000)
    expect(progress).to_have_count(0)
    expect(page.locator("#dataset")).to_be_enabled()


def test_an_override_waits_five_seconds_and_can_be_undone(page: Page, live_server: str) -> None:
    decision = decide_triage(live_server, "Crushing chest pain again, and I feel faint")
    sent: list[dict[str, Any]] = []
    page.on(
        "request",
        lambda r: (
            sent.append(json.loads(r.post_data or "{}"))
            if r.method == "POST" and decision["id"] in r.url
            else None
        ),
    )
    page.clock.install()
    open_agent(page, TRIAGE, "review")
    card = page.locator(f'.review-card[data-id="{decision["id"]}"]')
    pharmacy = card.get_by_role("button", name="Pharmacy", exact=True)
    pharmacy.click()
    undo = card.get_by_role("button", name="Undo")
    expect(undo).to_be_focused()
    expect(card.locator(".undo-bar")).to_contain_text("Override to Pharmacy in 5 s")
    page.clock.run_for(3000)
    expect(card.locator(".undo-seconds")).to_have_text("2")
    undo.click()
    expect(card.locator(".undo-bar")).to_have_count(0)
    expect(pharmacy).to_be_focused()
    page.clock.run_for(10_000)
    assert sent == []  # undone in time: nothing reached the audit chain
    pharmacy.click()
    page.clock.run_for(5000)
    expect(card).to_have_count(0)
    assert [s["final_label"] for s in sent] == ["pharmacy"]


def test_g_then_c_goes_to_the_card_without_confirming_a_review(
    page: Page, live_server: str
) -> None:
    """The chord "G C" goes to the Decision Card; "C" alone confirms the focused review. The
    queue's own handler saw the chord's C too, and confirmed a review on the way out."""
    decision = decide_triage(live_server, "Crushing chest pain, sweating, cannot breathe well")
    resolved: list[str] = []
    page.on(
        "request",
        lambda r: (
            resolved.append(r.url) if r.method == "POST" and "/api/review/" in r.url else None
        ),
    )
    open_agent(page, TRIAGE, "review")
    card = page.locator(f'.review-card[data-id="{decision["id"]}"]')
    card.focus()
    page.keyboard.press("g")
    page.keyboard.press("c")
    expect(page.locator('.nav a[data-view="card"]')).to_have_class(re.compile("active"))
    page.wait_for_timeout(500)
    assert resolved == []
    pending = httpx.get(
        f"{live_server}/api/review", params={"agent_id": TRIAGE, "limit": 500}
    ).json()
    assert decision["id"] in [item["id"] for item in pending]


def test_leaving_the_queue_sends_a_held_override(page: Page, live_server: str) -> None:
    decision = decide_triage(live_server, "Crushing chest pain once more, sweating")
    open_agent(page, TRIAGE, "review")
    card = page.locator(f'.review-card[data-id="{decision["id"]}"]')
    card.get_by_role("button", name="Billing", exact=True).click()
    with page.expect_request(lambda r: r.method == "POST" and decision["id"] in r.url) as request:
        page.locator('.nav a[data-view="audit"]').click()
    assert json.loads(request.value.post_data or "{}")["final_label"] == "billing"


# -- Phase B3: chart finish and Overview trends ---------------------------------------------------


def test_a_short_history_is_a_line_per_day_not_a_chart(page: Page) -> None:
    serve_history(
        page,
        [
            {"date": eastern_days_ago(3), "AUTO": 2, "HUMAN_REVIEW": 3},
            {"date": eastern_days_ago(1), "BLOCK": 1},
        ],
    )
    open_agent(page, TRIAGE)
    expect(page.locator(DAILY)).to_have_count(0)
    days = page.locator(".day-summary li")
    expect(days).to_have_count(2)
    expect(days.first).to_contain_text("1 decision")  # newest first
    expect(days.first).to_contain_text("1 blocked")
    expect(days.nth(1)).to_contain_text("5 decisions")
    expect(days.nth(1)).to_contain_text("2 automated · 3 human review")
    expect(page.locator(".day-summary + .chart-note")).to_contain_text("7 days of history")


def test_the_period_sets_the_figures_and_compares_with_the_one_before(page: Page) -> None:
    serve_history(
        page,
        [
            {"date": eastern_days_ago(8), "AUTO": 1, "HUMAN_REVIEW": 3, "reviewed": 2},
            {
                "date": eastern_days_ago(0),
                "AUTO": 3,
                "HUMAN_REVIEW": 1,
                "reviewed": 1,
                "overridden": 1,
                "labelled": 2,
                "agreed": 1,
            },
        ],
    )
    open_agent(page, TRIAGE)
    stats = page.locator(".stats-primary .stat")
    automated, overrides = stats.nth(0), stats.nth(2)
    expect(page.get_by_role("button", name="30 days")).to_have_attribute("aria-pressed", "true")
    expect(automated.locator(".stat-value")).to_contain_text(shown(figures.of(4, 8)))
    expect(automated.locator("svg.sparkline")).to_have_count(1)
    page.get_by_role("button", name="7 days").click()
    expect(automated.locator(".stat-value")).to_contain_text(shown(figures.of(3, 4)))
    expect(automated).to_contain_text("of the 4 decisions, last 7 days")
    # The week before is its own k of n, never a difference of two shares.
    expect(automated.locator(".stat-change")).to_have_text(
        f"previous 7 days: {shown(figures.of(1, 4))}"
    )
    expect(overrides.locator(".stat-value")).to_contain_text(shown(figures.of(1, 1)))
    expect(overrides.locator(".stat-change")).to_have_text(
        f"previous 7 days: {shown(figures.of(0, 2))}"
    )
    expect(stats.nth(3).locator(".stat-change")).to_have_count(0)  # nothing labelled before
    decisions = page.locator(".stats-secondary .stat").first
    expect(decisions).to_contain_text("4")
    expect(decisions.locator(".stat-change")).to_have_text("previous 7 days: 4")
    page.get_by_role("button", name="All").click()
    expect(automated).to_contain_text("of the 8 decisions, all time")
    expect(page.locator(".stat-change")).to_have_count(0)  # nothing before "all"
    page.reload()
    expect(page.get_by_role("button", name="All")).to_have_attribute("aria-pressed", "true")


def test_the_outcome_mix_names_segments_that_have_room(page: Page) -> None:
    serve_history(
        page, [{"date": eastern_days_ago(0), "AUTO": 12, "HUMAN_REVIEW": 7, "ABSTAIN": 1}]
    )
    open_agent(page, TRIAGE)
    segment = page.locator(".stack > span")
    expect(segment.nth(0)).to_have_class(re.compile("labelled"))
    expect(segment.nth(0)).to_contain_text("Automated 12")
    expect(segment.nth(0)).to_have_attribute("title", f"Automated: {shown(figures.of(12, 20))}")
    expect(segment.nth(2)).not_to_have_class(re.compile("labelled"))  # 5%: no room
    legend = page.locator(".stack-legend")
    expect(legend.locator('[data-outcome="AUTO"]')).to_have_class(re.compile("sr-only"))
    expect(legend.locator('[data-outcome="ABSTAIN"]')).to_be_visible()
    expect(legend.locator('[data-outcome="ABSTAIN"]')).to_have_text(
        f"Abstained {shown(figures.of(1, 20))}"
    )
    expect(legend.locator('[data-outcome="BLOCK"]')).to_be_visible()  # 0 blocked is worth saying
    page.set_viewport_size(PHONE)
    expect(segment.nth(1)).to_contain_text("Human review")
    expect(legend.locator('[data-outcome="HUMAN_REVIEW"]')).to_be_visible()  # no room on a phone


def test_configurations_at_the_same_point_share_one_labelled_mark(
    page: Page, served_agents: Path
) -> None:
    """On the triage fixture's critical set the four screens, all with the keyword router, are
    as accurate; the `agent` screen and the `checked` one (its service checked by the rules) each
    call the service once, so they cost the same too: one mark for the two, naming both."""
    serve_board(page, TRIAGE, counted_board(served(served_agents, TRIAGE), priced=True))
    open_agent(page, TRIAGE, "evals")
    page.get_by_role("button", name=re.compile(r"^critical n=")).click()
    chart = page.locator('figure.chart[data-chart^="cost"]').last
    expect(chart.locator(".mark")).to_have_count(3)  # four configurations, two in one place
    expect(chart.locator(".mark[aria-label*=' and ']")).to_have_count(1)
    expect(chart.locator(".direct-label", has_text="Rules")).not_to_have_count(0)  # deployed


def test_line_charts_name_their_lines_where_there_is_room(page: Page) -> None:
    open_agent(page, TRIAGE, "evals")
    page.goto("/#evals/report")
    sweep = page.locator('figure.chart[data-chart^="sweep"]')
    expect(sweep).to_have_class(re.compile("direct"))
    expect(sweep.locator(".direct-label")).to_have_text(["Of those, correct", "Would be automated"])
    expect(sweep.locator(".chart-legend")).to_be_hidden()
    page.set_viewport_size(PHONE)
    expect(sweep).not_to_have_class(re.compile("direct"))
    expect(sweep.locator(".chart-legend")).to_be_visible()


# -- Phase C: motion, empty states, identity ------------------------------------------------------

ANIMATION = "(el) => getComputedStyle(el).animationName"


def test_the_drawer_slides_in_and_out(page: Page) -> None:
    open_agent(page, TRIAGE, "audit")
    page.locator("#audit-table button.open-record").first.click()
    drawer = page.locator(".drawer")
    assert drawer.evaluate(ANIMATION) == "drawer-in"
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0)  # at once: no longer a dialog...
    expect(page.locator(".drawer-root.closing")).to_have_count(1)  # ...while it slides out
    expect(page.locator(".drawer-root")).to_have_count(0)
    expect(page.locator("#audit-table button.open-record").first).to_be_focused()


def test_a_view_fades_in_where_its_skeleton_was(page: Page) -> None:
    page.add_init_script(
        "window.fades = 0; document.addEventListener('animationstart', (e) => {"
        " if (e.target.id === 'view' && e.animationName === 'fade-in') window.fades++; }, true)"
    )
    open_agent(page, TRIAGE, "overview")
    view = page.locator("#view")
    expect(view).to_have_class(re.compile("view-enter"))
    assert view.evaluate(ANIMATION) == "fade-in"
    page.wait_for_function("() => window.fades === 1")
    page.locator('.nav a[data-view="audit"]').click()  # every navigation fades in again
    expect(page.locator("main h1")).to_have_text("Audit trail")
    page.wait_for_function("() => window.fades === 2")


def test_nothing_moves_under_reduced_motion(page: Page) -> None:
    page.emulate_media(reduced_motion="reduce")
    open_agent(page, TRIAGE, "audit")
    assert page.locator("#view").evaluate(ANIMATION) == "none"  # the view appears at once
    page.locator("#audit-table button.open-record").first.click()
    assert page.locator(".drawer").evaluate(ANIMATION) == "none"
    page.keyboard.press("Escape")
    assert page.locator(".drawer-root").count() == 0  # removed at once, no slide


def test_empty_views_say_what_goes_there_and_offer_one_action(page: Page) -> None:
    page.route("**/api/review?*", lambda route: route.fulfill(json=[]))
    page.route("**/api/feedback?*", lambda route: route.fulfill(json=[]))
    open_agent(page, TRIAGE, "review")
    for view, title, action in [
        ("review", "Nothing waiting for review", "Open the audit trail"),
        ("feedback", "Nothing rated yet", "Try a decision"),
    ]:
        show(page, view)
        empty = page.locator(".empty-state")
        expect(empty.locator(".empty-icon svg")).to_be_visible()
        expect(empty.get_by_role("heading")).to_have_text(title)
        expect(empty.get_by_role("link")).to_have_text(action)  # exactly one


def test_the_brand_is_a_mark_and_stat_labels_read_as_labels(page: Page) -> None:
    open_agent(page, TRIAGE)
    expect(page.locator(".brand svg.logo")).to_be_visible()
    expect(page.locator(".brand")).not_to_contain_text("◆")
    label = page.locator(".stats-primary .stat-label").first
    assert label.evaluate("(el) => getComputedStyle(el).textTransform") == "uppercase"
    expect(label).to_have_text("Automated")  # the words themselves are unchanged


# Controls smaller than WCAG 2.5.8's 24 x 24 px (links inside a sentence are exempt; there are none
# left outside sentences).
UNDER_24 = """() => [...document.querySelectorAll('main a[href], main button, main input, '
    + 'main select, main textarea, main summary, .nav a, .topbar button, .topbar select, '
    + 'main [tabindex="0"]')]
  .filter((el) => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden')
  .filter((el) => { const r = el.getBoundingClientRect(); return r.width < 24 || r.height < 24; })
  .filter((el) => !(el.tagName === 'A' && el.closest('p, li')))
  .map((el) => el.outerHTML.slice(0, 80))"""


@pytest.mark.parametrize("agent_id", [TRIAGE, CLINICAL])
def test_every_control_is_at_least_24_px(page: Page, agent_id: str) -> None:
    page.set_viewport_size(PHONE)
    open_agent(page, agent_id)
    for view in [*VIEWS, "evals/report", "evals/run"]:
        page.goto(f"/#{view}")
        expect(page.locator("main h1")).to_be_visible()
        expect(page.locator("#view")).not_to_have_attribute("aria-busy", "true")
        page.wait_for_load_state("networkidle")
        assert page.evaluate(UNDER_24) == [], view


# -- SMART on FHIR: this patient ------------------------------------------------------------------

# A launch of the chart fixture (`mendari-test/chart-summary`, whose focus is the Patient): the
# patient's record is its one work item, as the server lists it (server/smart_routes.py).
SMART_SESSION = {
    "active": True,
    "iss": "https://ehr.example.org/fhir",
    "patient": "chart-us-core-1",
    "encounter": "enc-1",
    "fhir_user": "Practitioner/pr-1",
    "scope": "launch openid fhirUser patient/*.rs",
    "expires_at": 4102444800,
    "agent": CHART,
}
SMART_PATIENT = {
    "id": "chart-us-core-1",
    "name": "Case 1 Synthetic",
    "gender": "female",
    "birthDate": "1959-06-15",
}
SMART_ITEMS = [
    {
        "ref": "Patient/chart-us-core-1",
        "status": None,
        "title": "This patient's record",
        "text": "",
        "date": None,
    }
]


def smart_launched(page: Page, decided: list[Any]) -> None:
    """A browser that came back from an EHR launch (the SMART API answers from fixtures)."""
    page.route("**/api/smart/session", lambda route: route.fulfill(json=SMART_SESSION))
    page.route("**/api/smart/patient", lambda route: route.fulfill(json=SMART_PATIENT))
    page.route("**/api/smart/focus?*", lambda route: route.fulfill(json=SMART_ITEMS))

    def decide(route: Any) -> None:
        decided.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(
            json={
                "decision": {
                    "id": "d-1",
                    "outcome": "HUMAN_REVIEW",
                    "recommendation": "soon",
                },
                "task": {},
            }
        )

    page.route("**/api/smart/decide", decide)
    page.route("**/api/smart/logout", lambda route: route.fulfill(status=204, body=""))


def test_without_a_launch_the_patient_view_stays_out_of_the_way(page: Page) -> None:
    open_agent(page, TRIAGE)
    expect(page.locator('.nav a[data-view="patient"]')).to_be_hidden()
    page.goto(f"/?agent={TRIAGE}#patient")
    expect(page.locator(".empty-state h2")).to_have_text("No EHR launch")


def test_an_ehr_launch_shows_the_patient_and_decides_their_work_items(page: Page) -> None:
    decided: list[Any] = []
    smart_launched(page, decided)
    page.goto(f"/?agent={CHART}#patient")
    expect(page.locator('.nav a[data-view="patient"]')).to_be_visible()
    expect(page.locator("main h1")).to_have_text("This patient")
    expect(page.locator(".patient-name")).to_have_text("Case 1 Synthetic")
    expect(page.locator(".patient-banner")).to_contain_text("female")
    item = page.locator(".work-item")
    expect(item).to_have_count(1)
    expect(item).to_contain_text("This patient's record")
    expect(item.locator(".badge")).to_have_count(0)  # no priority to show
    item.get_by_role("button", name="Decide").click()
    expect(item.locator(".work-result")).to_contain_text("soon")
    expect(item.get_by_role("link", name=re.compile("Open the decision"))).to_have_attribute(
        "href", "#audit/d-1"
    )
    assert decided == [{"agent_id": CHART, "focus": "Patient/chart-us-core-1"}]


def test_ending_the_ehr_session_hides_the_patient_view(page: Page) -> None:
    smart_launched(page, [])
    page.goto(f"/?agent={CHART}#patient")
    page.get_by_role("button", name="End session").click()
    expect(page).to_have_url(re.compile("#overview$"))
    expect(page.locator('.nav a[data-view="patient"]')).to_be_hidden()


def test_the_patient_view_fits_a_phone(page: Page) -> None:
    page.set_viewport_size(PHONE)
    smart_launched(page, [])
    page.goto(f"/?agent={CHART}#patient")
    expect(page.locator(".work-item")).to_have_count(1)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


# -- SMART on FHIR: a browser that only launched (sign-in on, no one signed in) --------------------

LAUNCH_AGENTS = [{"id": CHART, "name": CHART_NAME}, {"id": TRIAGE, "name": TRIAGE_NAME}]
DECIDED = {
    "decision": {
        "id": "d-1",
        "outcome": "HUMAN_REVIEW",
        "recommendation": "soon",
        "reasons": ["A condition on the chart may call for a visit soon."],
    },
    "task": {},
}


def launch_only(page: Page, session: dict[str, Any]) -> list[str]:
    """A browser the EHR launched on a deployment with sign-in on, where no one signed in: the
    launch's routes answer (from `session`, which a test may change), `/api/auth/me` says no one
    is signed in, and every other API call is refused as the server would refuse it. Returns
    the API paths the page asked for."""
    asked: list[str] = []

    def answer(route: Route) -> None:
        path = urllib.parse.urlsplit(route.request.url).path
        asked.append(path)
        if path == "/api/smart/session":
            route.fulfill(json=session)
        elif path == "/api/smart/patient":
            route.fulfill(json=SMART_PATIENT)
        elif path == "/api/smart/focus":
            route.fulfill(json=SMART_ITEMS)
        elif path == "/api/smart/decide":
            route.fulfill(json=DECIDED)
        elif path == "/api/smart/logout":
            route.fulfill(status=204, body="")
        elif path == "/api/auth/me":
            route.fulfill(json={"mode": "sign-in", "signed_in": False})
        elif path == "/api/health":
            route.fulfill(json={"status": "ok", "version": "test", "sign_in": True})
        else:
            route.fulfill(status=401, json={"detail": "sign in first, or send an API key"})

    page.route("**/api/**", answer)
    return asked


def launched_as(launch_id: str) -> dict[str, Any]:
    return {**SMART_SESSION, "launch_id": launch_id, "agents": LAUNCH_AGENTS}


def test_a_launch_alone_opens_the_patient_view_only(page: Page) -> None:
    """The EHR opened this window (`#patient/<launch id>`) and no one is signed in: the patient
    view is the only section, the agents are the launch's, the decision is shown in place with
    no link into the audit trail, and nothing outside the launch's routes is asked for."""
    asked = launch_only(page, launched_as("L1"))
    page.goto(f"/?agent={CHART}#patient/L1")
    expect(page.locator(".patient-name")).to_have_text("Case 1 Synthetic")
    expect(page.locator(".nav a:visible")).to_have_count(1)
    expect(page.locator(".nav a:visible")).to_have_text("This patient")
    names = [agent["name"] for agent in LAUNCH_AGENTS]
    expect(page.locator("#agent-select option")).to_have_text(names)
    expect(page.locator("#signed-in")).to_be_hidden()
    item = page.locator(".work-item")
    item.get_by_role("button", name="Decide").click()
    result = item.locator(".work-result")
    expect(result).to_contain_text("soon")
    expect(result.locator(".badge")).to_have_text("Human review")
    expect(result).to_contain_text("A condition on the chart may call for a visit soon")
    expect(result).to_contain_text("Recorded in your organization's audit trail as d-1.")
    expect(result.get_by_role("link")).to_have_count(0)
    page.goto(f"/?agent={CHART}#overview")  # no other section, whatever the address says
    expect(page.locator("main h1")).to_have_text("This patient")
    expect(page.locator(".patient-name")).to_have_text("Case 1 Synthetic")
    outside = {path for path in asked if not path.startswith("/api/smart/")}
    assert outside <= {"/api/health", "/api/auth/me"}, outside


def test_a_signed_in_person_keeps_the_link_to_the_decision(page: Page) -> None:
    signed_in_as(page, "reviewer")
    page.route("**/api/smart/session", lambda route: route.fulfill(json=launched_as("L1")))
    page.route("**/api/smart/patient", lambda route: route.fulfill(json=SMART_PATIENT))
    page.route("**/api/smart/focus?*", lambda route: route.fulfill(json=SMART_ITEMS))
    page.route("**/api/smart/decide", lambda route: route.fulfill(json=DECIDED))
    page.goto(f"/?agent={CHART}#patient/L1")
    expect(page.locator('.nav a[data-view="overview"]')).to_be_visible()
    item = page.locator(".work-item")
    item.get_by_role("button", name="Decide").click()
    expect(item.locator(".work-result")).to_contain_text("may call for a visit soon")
    expect(item.get_by_role("link", name=re.compile("Open the decision"))).to_have_attribute(
        "href", "#audit/d-1"
    )
    expect(item.locator(".decided-id")).to_have_count(0)


@pytest.mark.parametrize("signed_in", [False, True])
def test_an_ended_launch_says_to_open_mendari_again_from_the_ehr(
    page: Page, signed_in: bool
) -> None:
    """The EHR ended the launch (its token, or the launch's idle or absolute limit): the launch's
    routes answer 401. That is said as the launch's end, never as a Mendari session's (signing in
    would not bring the launch back), for a launch alone and for someone signed in too."""
    ended = {"detail": "the EHR ended this launch: open Mendari again from the EHR"}
    if signed_in:
        signed_in_as(page, "reviewer")
        page.route("**/api/smart/session", lambda route: route.fulfill(json=launched_as("L1")))
    else:
        launch_only(page, launched_as("L1"))
    page.route("**/api/smart/patient", lambda route: route.fulfill(status=401, json=ended))
    page.goto(f"/?agent={CHART}#patient/L1")
    banner = page.locator("#launch-ended")
    expect(banner).to_be_visible()
    expect(banner).to_have_text("This EHR launch ended. Open Mendari again from the EHR.")
    expect(page.locator(".empty-state h2")).to_have_text("This EHR launch ended")
    expect(page.locator("#session-ended")).to_be_hidden()
    expect(page.locator("#announce-alert")).to_have_text(
        "This EHR launch ended. Open Mendari again from the EHR."
    )
    expect(page.locator(".patient-name")).to_have_count(0)


def test_a_window_whose_launch_was_replaced_says_so(page: Page) -> None:
    """One browser, one launch at a time: a window pins the launch it shows, and once another
    launch replaced it (another chart, maybe another patient), the window says so, decides
    nothing and never shows the other launch's patient."""
    session = launched_as("L1")
    asked = launch_only(page, session)
    page.goto(f"/?agent={CHART}#patient")  # opened without a pin: it takes the one it shows
    expect(page.locator(".patient-name")).to_have_text("Case 1 Synthetic")
    expect(page).to_have_url(re.compile("#patient/L1$"))
    expect(page.locator('.nav a[data-view="patient"]')).to_have_attribute("href", "#patient/L1")
    session["launch_id"] = "L2"  # the EHR launched again in this browser
    page.locator(".work-item").get_by_role("button", name="Decide").click()
    replaced = page.locator(".empty-state h2")
    expect(replaced).to_have_text("This window's launch was replaced")
    assert "/api/smart/decide" not in asked
    asked.clear()
    page.reload()
    expect(replaced).to_have_text("This window's launch was replaced")
    expect(page.locator(".empty-state")).to_contain_text("Open Mendari again from the EHR")
    expect(page.locator(".patient-name")).to_have_count(0)
    assert "/api/smart/patient" not in asked and "/api/smart/focus" not in asked
    # An address that no longer names the launch keeps the window's own pin.
    page.evaluate("location.hash = '#overview'")
    expect(replaced).to_have_text("This window's launch was replaced")
    page.evaluate("location.hash = '#patient'")
    expect(replaced).to_have_text("This window's launch was replaced")
    expect(page.locator(".patient-name")).to_have_count(0)
    assert "/api/smart/patient" not in asked and "/api/smart/focus" not in asked


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_launch_alone_fits_a_phone_in_both_themes(page: Page, scheme: str) -> None:
    page.emulate_media(color_scheme=scheme)  # type: ignore[arg-type]
    page.set_viewport_size(PHONE)
    launch_only(page, launched_as("L1"))
    page.route(
        "**/api/smart/patient",
        lambda route: route.fulfill(status=401, json={"detail": "ended"}),
        times=1,
    )
    page.goto(f"/?agent={CHART}#patient/L1")
    expect(page.locator("#launch-ended")).to_be_visible()
    pairs = page.evaluate(CONTRAST, "#launch-ended")
    page.reload()
    expect(page.locator("html")).to_have_attribute("data-theme", scheme)
    item = page.locator(".work-item")
    item.get_by_role("button", name="Decide").click()
    expect(item.locator(".decided-id")).to_be_visible()
    pairs += page.evaluate(CONTRAST, "main .badge") + page.evaluate(CONTRAST, "main button.primary")
    failing = [(text, round(ratio, 2)) for text, ratio in pairs if ratio < 4.5]
    assert pairs and failing == [], scheme
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


# -- organization sign-in ----------------------------------------------------------------------


PERMISSIONS = {  # server/auth.py
    "viewer": ["read"],
    "reviewer": ["decide", "outcome", "read", "read_input", "review"],
    "admin": [
        "admin",
        "approve",
        "decide",
        "evaluate",
        "keys",
        "outcome",
        "read",
        "read_input",
        "review",
    ],
    "labeller": ["label"],  # LP-11: memberships mode only
}


def signed_in_as(page: Page, *roles: str) -> None:
    granted = sorted({p for role in roles for p in PERMISSIONS[role]})
    principal = {
        "kind": "user",
        "name": "Nurse Kim",
        "roles": list(roles),
        "permissions": granted,
        "org": "default",
        "actor": "kim@clinic.example.org",
    }
    page.route(
        "**/api/auth/me",
        lambda route: route.fulfill(
            json={"mode": "sign-in", "signed_in": True, "principal": principal}
        ),
    )


def test_open_mode_shows_no_sign_in(page: Page) -> None:
    open_agent(page, TRIAGE)
    expect(page.locator("#signed-in")).to_be_hidden()


def test_the_bar_says_who_is_signed_in(page: Page) -> None:
    signed_in_as(page, "reviewer")
    open_agent(page, TRIAGE)
    expect(page.locator("#signed-in-name")).to_have_text("Nurse Kim · Reviewer")
    expect(page.get_by_role("button", name="Sign out")).to_be_visible()


def test_only_administrators_see_the_evaluation_run(page: Page) -> None:
    signed_in_as(page, "reviewer")
    open_agent(page, TRIAGE, "evals")
    page.goto(f"/?agent={TRIAGE}#evals/run")
    expect(page.locator("#evals-body")).to_contain_text(
        "Running an evaluation is for administrators"
    )
    expect(page.locator("#run-eval")).to_have_count(0)


def test_a_viewer_reads_but_is_not_offered_what_the_server_would_refuse(
    page: Page, live_server: str
) -> None:
    decide_triage(live_server, "Crushing chest pain and my left arm is numb")  # waits for review
    signed_in_as(page, "viewer")
    page.goto(f"/?agent={TRIAGE}#try")
    expect(page.get_by_role("heading", name="Trying the agent is for reviewers")).to_be_visible()
    page.goto(f"/?agent={TRIAGE}#review")
    card = page.locator(".review-card").first
    expect(card).to_contain_text("Resolving is for reviewers")
    expect(page.locator(".review-card .labels")).to_have_count(0)


def test_who_is_signed_in_closes_the_phone_settings_sheet(page: Page) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    signed_in_as(page, "reviewer")
    open_agent(page, TRIAGE)
    page.get_by_role("button", name="Settings").click()
    sheet, signed_in = page.locator("#topbar-actions"), page.locator("#signed-in")
    expect(signed_in).to_be_visible()
    sheet_box, row = sheet.bounding_box(), signed_in.bounding_box()
    assert sheet_box and row
    assert row["width"] > sheet_box["width"] - 40  # a row of its own, not squeezed into a column
    assert row["y"] > page.locator("#agent-select").bounding_box()["y"]  # type: ignore[index]


def test_a_signed_in_reviewer_reviews_under_their_own_name(page: Page) -> None:
    signed_in_as(page, "reviewer")
    page.goto(f"/?agent={TRIAGE}#review")
    expect(page.locator("#reviewer")).to_have_value("kim@clinic.example.org")  # as chained
    expect(page.locator("#reviewer")).to_have_attribute("readonly", "")


def test_a_session_that_ended_says_so_and_offers_the_way_back(page: Page) -> None:
    """The server ended the session (30 minutes idle, or its 8 hours): the next call is a 401,
    and the person is told so, on screen until they act, with a sign-in that comes back here."""
    signed_in_as(page, "reviewer")
    open_agent(page, TRIAGE)
    banner = page.locator("#session-ended")
    expect(banner).to_be_hidden()
    ended = {"detail": "sign in first, or send an API key"}
    page.route("**/api/review?*", lambda route: route.fulfill(status=401, json=ended))
    page.goto(f"/?agent={TRIAGE}#review")
    expect(banner).to_be_visible()
    expect(banner).to_have_text("Your session ended. Sign in again.")
    link = banner.get_by_role("link", name="Sign in again.")
    href = link.get_attribute("href") or ""
    assert href.startswith("/auth/login?next=") and urllib.parse.unquote(href).endswith("#review")
    expect(page.locator("#announce-alert")).to_have_text("Your session ended. Sign in again.")


def test_open_mode_never_says_a_session_ended(page: Page) -> None:
    """Without sign-in a 401 cannot be a session's end: the error is shown as it comes."""
    open_agent(page, TRIAGE)
    refused = {"detail": "refused here"}
    page.route("**/api/review?*", lambda route: route.fulfill(status=401, json=refused))
    page.goto(f"/?agent={TRIAGE}#review")
    expect(page.locator(".error-state")).to_contain_text("refused here")
    expect(page.locator("#session-ended")).to_be_hidden()


def test_a_level_2_agent_can_be_approved_to_automate_from_its_card(page: Page) -> None:
    open_agent(page, CLINICAL, "card")
    page.goto(f"/?agent={CLINICAL}#card/approval")
    section = page.locator("#card-approval")
    expect(section).to_contain_text(
        "a person reviews every decision unless your organization approves"
    )
    page.fill("#approval-reference", "AI committee minutes, item 4")
    page.fill("#approval-approver", "dr-chair")
    page.get_by_role("button", name="Approve automation").click()
    expect(section).to_contain_text(
        "Automation approved by dr-chair (AI committee minutes, item 4)"
    )
    expect(page.locator("dl.facts")).to_contain_text("Approved by dr-chair: At confidence")
    section.get_by_role("button", name="Revoke").click()
    expect(section.get_by_role("button", name="Approve automation")).to_be_visible()


def test_the_card_lists_where_the_agent_runs(page: Page) -> None:
    open_agent(page, TRIAGE, "card")
    page.goto(f"/?agent={TRIAGE}#card/deployments")
    rows = page.locator("#card-deployments tbody tr")
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("local")
    expect(rows.first).to_contain_text("router rules")


def test_a_level_1_agent_needs_no_approval(page: Page) -> None:
    open_agent(page, TRIAGE, "card")
    page.goto(f"/?agent={TRIAGE}#card/approval")
    expect(page.locator("#card-approval")).to_contain_text("No approval is needed")


def test_monitoring_shows_the_last_week_against_the_benchmark(page: Page) -> None:
    open_agent(page, TRIAGE, "monitoring")
    expect(page.locator("main h1")).to_have_text("Monitoring")
    expect(page.locator(".stats-primary .stat")).to_have_count(4)
    expect(page.locator(".stat").first).to_contain_text("benchmark")
    expect(page.locator("#monitoring-days")).to_have_value("30")
    expect(page.locator('[data-table="monitoring-weeks"] tbody tr')).to_have_count(5)
    expect(page.locator("main")).to_contain_text("The trend appears once decisions in two weeks")


def test_monitoring_draws_the_weekly_trend(page: Page) -> None:
    def with_weeks(route: Route) -> None:
        report = route.fetch().json()
        report["weeks"] = MONITORING["weeks"]
        route.fulfill(json=report)

    page.route("**/api/monitoring?*", with_weeks)
    open_agent(page, TRIAGE, "monitoring")
    chart = page.locator('figure[data-chart="monitoring-trend"]')
    expect(chart.locator("svg")).to_be_visible()
    expect(chart).to_contain_text("Sep 30")
    rows = page.locator('[data-table="monitoring-weeks"] tbody tr')
    expect(rows.first).to_contain_text("Sep 9 \u2013 Sep 16")  # an en dash
    expect(rows.first).to_contain_text(shown(figures.of(24, 25)))
    expect(rows.nth(1)).to_contain_text("no cases")  # a week with nothing labelled


def monitoring_report(**changes: Any) -> dict[str, Any]:
    """A /api/monitoring answer whose reviewed and whole-set figures differ, so the view shows
    which it compares."""
    report: dict[str, Any] = {
        "agent": {"id": TRIAGE},
        "days": 30,
        "benchmark": {
            "accuracy": 0.97,
            "n": 600,
            "reviewed_accuracy": 0.995,
            "automated_accuracy": None,
            "tolerance": 0.03,
            "comparable": True,
            "reason": None,
            "figures": {
                "accuracy": figures.counted(0.97, 600),
                "reviewed_accuracy": figures.counted(0.995, 598),
                "automated_accuracy": None,
            },
        },
        "other_versions": {"0.4.0": 3},
        "configuration_since": None,
        "last_7_days": {
            "decisions": 40,
            "refused": 0,
            "automated_rate": 0.9,
            "reviewed": 30,
            "awaiting_review": 0,
            "override_rate": 10 / 30,
            "labelled": 30,
            "observed_accuracy": 0.8,
            "confidences": 40,
            "cost_per_decision": 0.0,
            "cost_unknown": 0,
            "latency_ms": {"n": 40, "p50": 110.0, "p95": 900.0, "mean": 200.0},
            "drift_psi": 0.31,
            "automated_rated_wrong": 2,
            "figures": {
                "automated": figures.of(36, 40),
                "overrides": figures.of(10, 30),
                "accuracy": figures.of(24, 30),
                "reviewed_accuracy": figures.of(20, 30),
                "automated_accuracy": figures.of(0, 0),
                "provider_errors": figures.of(0, 40),
                "abstained": figures.of(0, 40),
            },
        },
        "weeks": [],
        "alerts": [],
    }
    for key, value in changes.items():
        report[key] = value
    return report


def test_monitoring_alerts_lead_the_page(page: Page) -> None:
    report = monitoring_report(
        alerts=[
            {
                "kind": "accuracy",
                "stratum": "reviewed_accuracy",
                "severity": "high",
                "message": f"reviewed decisions: accuracy {shown(figures.of(20, 30))} "
                "over the last 7 days",
            },
            {
                "kind": "drift",
                "severity": "medium",
                "message": "PSI 0.31 against the benchmark's confidences",
            },
        ]
    )
    page.route("**/api/monitoring?*", lambda route: route.fulfill(json=report))
    open_agent(page, TRIAGE, "monitoring")
    expect(page.locator(".callout.danger")).to_contain_text(
        f"Accuracy below the benchmark: reviewed decisions: accuracy {shown(figures.of(20, 30))}"
    )
    expect(page.locator("main")).to_contain_text("against its benchmark")
    expect(page.locator("main")).to_contain_text("3 decisions of other versions of the agent are")
    reviewed = page.locator(".stats-primary .stat").first
    expect(reviewed).to_contain_text(shown(figures.of(20, 30)))  # the stratum, not the whole set
    expect(reviewed).to_contain_text(f"benchmark {shown(figures.counted(0.995, 598))}")
    expect(page.locator(".stats-primary")).not_to_contain_text(shown(figures.counted(0.97, 600)))
    expect(page.locator(".callout.warning")).to_contain_text("Confidence drift: PSI 0.31")
    expect(page.locator(".stat.tone-danger")).to_have_count(2)
    expect(
        page.get_by_role("heading", name="No decisions of this version in the last 30 days")
    ).to_be_visible()


def test_monitoring_says_an_agent_from_a_registry_is_not_compared(page: Page) -> None:
    """LP-18h: the monitor takes no benchmark for a pulled agent (`from_registry`)."""
    bench = {
        **monitoring_report()["benchmark"],
        "accuracy": None,
        "n": None,
        "reviewed_accuracy": None,
        "comparable": False,
        "reason": "from_registry",
        "figures": {"accuracy": None, "reviewed_accuracy": None, "automated_accuracy": None},
    }
    report = monitoring_report(benchmark=bench)
    page.route("**/api/monitoring?*", lambda route: route.fulfill(json=report))
    open_agent(page, TRIAGE, "monitoring")
    main = page.locator("main")
    expect(main).to_contain_text(
        "Not compared with the benchmark: the agent is from a registry, and the benchmark it "
        "carries is its publisher's upload, never a reference. Only the checks that need no "
        "benchmark run"
    )
    expect(page.locator(".stats-primary .stat").first).not_to_contain_text("benchmark")


def test_monitoring_says_why_the_benchmark_is_not_compared(page: Page) -> None:
    week = {
        **monitoring_week("2026-09-23", "2026-09-30", 23, 25, 2),
        "awaiting_review": 3,
        "refused": 2,
        "cost_unknown": 1,
        "cost_per_decision": None,
    }
    bench = {**monitoring_report()["benchmark"], "comparable": False, "reason": "other_providers"}
    quick = {**week, "latency_ms": {"n": 30, "p50": 1.1, "p95": 1.4, "mean": 1.2}}
    report = monitoring_report(benchmark=bench, other_versions={"0.4.0": 1}, weeks=[week, quick])
    page.route("**/api/monitoring?*", lambda route: route.fulfill(json=report))
    open_agent(page, TRIAGE, "monitoring")
    main = page.locator("main")
    expect(main).to_contain_text(
        "Not compared with the benchmark: the benchmark ran other providers or models than this "
        "deployment. Only the checks that need no benchmark run"
    )
    expect(main).to_contain_text("1 decision of another version of the agent is not counted.")
    expect(main).not_to_contain_text("against its benchmark")
    expect(page.locator(".stats-primary .stat").first).not_to_contain_text("benchmark")
    expect(page.locator(".stats-primary")).to_contain_text("needs a comparable benchmark")
    table = page.locator('[data-table="monitoring-weeks"]')
    expect(table.locator("thead")).to_contain_text("Awaiting review")
    expect(table.locator("thead")).to_contain_text("Refused inputs")
    row = table.locator("tbody tr").first
    expect(row).to_contain_text("unknown")
    expect(row.locator("td").nth(5)).to_have_text("3")
    expect(row.locator("td").nth(6)).to_have_text("2")
    expect(table.locator("thead")).to_contain_text("Provider errors")
    expect(row.locator("td").nth(7)).to_contain_text(shown(figures.of(1, 30)))
    expect(row.locator("td").nth(8)).to_contain_text(shown(figures.of(2, 30)))
    expect(row.locator("td").nth(9)).to_have_text("1.5 s")
    expect(table.locator("tbody tr").nth(1).locator("td").nth(9)).to_have_text("1.4 ms")


def test_an_automated_stratum_alert_marks_its_own_stat(page: Page) -> None:
    bench = monitoring_report()["benchmark"]
    bench = {
        **bench,
        "automated_accuracy": 0.95,
        "figures": {**bench["figures"], "automated_accuracy": figures.counted(0.95, 100)},
    }
    week = monitoring_report()["last_7_days"]
    week = {
        **week,
        "figures": {
            **week["figures"],
            "reviewed_accuracy": figures.of(30, 30),
            "automated_accuracy": figures.of(0, 25),
        },
    }
    alert = {
        "kind": "accuracy",
        "stratum": "automated_accuracy",
        "severity": "high",
        "message": "automated decisions with an outcome recorded: accuracy 0 of 25",
    }
    report = monitoring_report(benchmark=bench, last_7_days=week, alerts=[alert])
    page.route("**/api/monitoring?*", lambda route: route.fulfill(json=report))
    open_agent(page, TRIAGE, "monitoring")
    stats = page.locator(".stats-primary .stat")
    expect(stats).to_have_count(5)
    expect(stats.first).not_to_have_class(re.compile(r"tone-danger"))
    automated = stats.nth(1)
    expect(automated).to_contain_text("Automated accuracy, 7 days")
    expect(automated).to_contain_text(f"benchmark {shown(figures.counted(0.95, 100))}")
    expect(automated).to_have_class(re.compile(r"tone-danger"))


def test_an_input_kept_encrypted_says_who_can_read_it(page: Page, live_server: str) -> None:
    decided = decide_triage(live_server, "Crushing chest pain and my left arm is numb")

    def restricted(route: Route) -> None:
        record = route.fetch().json()
        record["record"]["input"] = None
        record["input_restricted"] = True
        route.fulfill(json=record)

    page.route(f"**/api/audit/{decided['id']}", restricted)
    open_agent(page, TRIAGE, "audit")
    page.goto(f"/?agent={TRIAGE}#audit/{decided['id']}")
    expect(page.locator(".drawer")).to_contain_text("Input kept encrypted (PHI mode)")
