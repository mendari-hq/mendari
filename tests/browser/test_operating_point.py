# SPDX-License-Identifier: Apache-2.0
"""An evaluation report's operating point in the console, in a real browser: the bundled demo
agent, served alone, with the reports `mendari eval --db` leaves in a deployment's history."""

from __future__ import annotations

import copy
import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, Page, expect

from mendari import AuditStore, Decision, load_pack
from mendari.evals.harness import evaluation, load_dataset
from mendari.evals.operating_point import Targets
from mendari.server.app import create_app
from tests.browser.conftest import serving
from tests.conftest import DEMO_AGENT_DIR, ROOT

pytestmark = pytest.mark.browser

MARKUP = '<b id="injected">not markup</b>'
# The reports in the history, oldest first (`_reports`).
NO_TARGET, HELD, RECOMMENDS, EARLIER = range(4)


def _reports() -> list[dict[str, Any]]:
    """Four reports of the demo agent on its test set, oldest first: with no target; held to
    critical misses at most 1%, which no threshold meets; one whose block recommends a
    threshold, with markup where a sentence should be (the block is written here: only how one
    is shown is checked on it); and one as a run wrote it before it worked a block out."""
    pack = load_pack(DEMO_AGENT_DIR)
    dataset = load_dataset(pack.path("data/test.jsonl"), name="test")
    decision = Decision(pack, origin_cap=False)
    plain = evaluation(decision, dataset).report
    held = evaluation(decision, dataset, targets=Targets(critical_miss=0.01)).report
    recommends = copy.deepcopy(held)
    block = recommends["run"]["operating_point"]
    block["status"] = "recommended"
    block["summary"] = "Recommended automation threshold 0.93 (now 0.90)."
    block["lines"] = [
        "To adopt it, change one line of the agent's manifest, under `policy.thresholds`: "
        "`auto: 0.93` (now 0.90). Nothing changes it for you.",
        MARKUP,
    ]
    earlier = copy.deepcopy(plain)
    del earlier["run"]["operating_point"]
    reports = [plain, held, recommends, earlier]
    for age, report in enumerate(reports):  # each its own run, a second after the last
        report["run"]["id"] = str(uuid.uuid4())
        report["run"]["created_at"] = f"2026-11-23T10:00:0{age}+00:00"
    return reports


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    db = Path(tmp_path_factory.mktemp("operating-point")) / "mendari.db"
    with AuditStore(db) as store:
        for report in _reports():
            store.save_report(report)
    with serving(create_app(db_path=db, packs_paths=[ROOT / "agents"])) as port:
        yield f"http://127.0.0.1:{port}"


@pytest.fixture
def console(browser: Browser, served: str) -> Iterator[Page]:
    context = browser.new_context(base_url=served)
    yield context.new_page()
    context.close()


def _history(page: Page, report: int) -> None:
    """The evaluation history, showing one of the four reports (the list is newest first, and
    a run made from the console in another test is newer than all four)."""
    page.goto("/#evals/history")
    options = page.locator("#history option")
    expect(options.nth(3)).to_be_attached()
    page.locator("#history").select_option(index=options.count() - 1 - report)
    expect(page.locator("#history-report .stats-primary")).to_be_visible()


def test_an_evaluation_report_shows_its_operating_point(console: Page) -> None:
    errors: list[str] = []
    console.on("pageerror", lambda error: errors.append(str(error)))
    _history(console, NO_TARGET)
    panel = console.locator("#history-report [data-operating-point]")
    expect(panel).to_have_count(1)
    expect(panel).to_have_attribute("data-operating-point", "no_target")
    expect(panel.get_by_role("heading", level=2)).to_have_text("Operating point")
    expect(panel.locator("b")).to_have_text(
        "At the agent's automation threshold 0.90, 326 of 600 cases are automated: critical "
        "urgent_symptoms=yes automated 9 of 114 critical cases (at most 13.4%), 9 of all 600 "
        "cases (at most 2.7%); false automation 9 of 600 labelled cases (at most 2.7%)."
    )
    expect(panel.locator(".finding")).to_have_count(2)  # the repeats, and how a bound is taken
    # The first panel of the report: above the threshold sweep, where a threshold is looked for.
    first = console.locator("#history-report .panel").first
    expect(first).to_have_attribute("data-operating-point", "no_target")
    assert errors == []


def test_an_evaluation_held_to_a_target_says_what_no_threshold_meets(console: Page) -> None:
    _history(console, HELD)
    panel = console.locator("#history-report [data-operating-point='none_certified']")
    expect(panel.locator("b")).to_have_text(
        "No automation threshold can be certified for critical misses at most 1% with 95% "
        "confidence."
    )
    said = panel.locator(".finding")
    expect(said).to_have_count(5)
    expect(said.nth(1)).to_have_text(
        "Showing at most 1% with 95% confidence needs at least 299 critical cases "
        "(urgent_symptoms=yes) with none automated; this set has 114."
    )
    expect(said.nth(2)).to_contain_text("The threshold cannot fix this: at 0.95, the strictest")


def test_an_evaluation_reports_sentences_are_text_and_what_they_quote_is_code(
    console: Page,
) -> None:
    _history(console, RECOMMENDS)
    panel = console.locator("#history-report [data-operating-point='recommended']")
    expect(panel.locator("code")).to_have_text(["policy.thresholds", "auto: 0.93"])
    expect(panel.locator(".finding").nth(1)).to_have_text(MARKUP)  # shown, never rendered
    expect(console.locator("#injected")).to_have_count(0)


def test_an_evaluation_report_from_before_shows_no_operating_point(console: Page) -> None:
    _history(console, EARLIER)
    expect(console.locator("#history-report h2").first).to_be_visible()
    expect(console.locator("#history-report [data-operating-point]")).to_have_count(0)
    expect(console.locator("#history-report")).not_to_contain_text("Operating point")


def test_an_evaluation_run_from_the_console_shows_its_operating_point(console: Page) -> None:
    """A run from the console is given no target: it shows the bound at the agent's threshold.
    (An agent is asked for over the network when it is live, so the console confirms first, and
    the server runs the first 200 cases.)"""
    console.goto("/#evals/run")
    console.locator("#dataset").select_option("test")
    console.get_by_role("button", name="Run evaluation").click()
    console.get_by_role("dialog").get_by_role(
        "button", name=re.compile(r"^Run \d+ records$")
    ).click()
    panel = console.locator("#run-report [data-operating-point='no_target']")
    expect(panel).to_be_visible(timeout=60_000)
    expect(panel.locator("b")).to_have_text(
        re.compile(r"^At the agent's automation threshold 0\.90, \d+ of 200 cases are automated: ")
    )
