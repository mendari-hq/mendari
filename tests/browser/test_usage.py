# SPDX-License-Identifier: Apache-2.0
"""The Access view's Usage tab (LP-21i): an organization's administrator sees its remote calls
and their cost over the last 30 days, totals then by day, "unknown" where a call had no price,
and nothing of another organization's; it passes axe in both themes. A real server at
`localhost` in memberships mode (tests/browser/access_site.py); all data synthetic. Run with
`uv run pytest -m browser`."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, expect

from tests.browser.access_site import Site, access, lead, memberships, row, running
from tests.browser.test_accessibility import violations
from tests.test_usage_api import days_ago, seed

pytestmark = pytest.mark.browser


@pytest.fixture
def site(browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Site]:
    memberships(monkeypatch)
    with running(browser, tmp_path / "a.db") as here:
        yield here


def test_the_usage_tab_shows_the_organization_s_calls_and_an_unknown_cost(site: Site) -> None:
    seed(
        site.store,
        (days_ago(0), "clinic-a", "jev", "decision", 10, 10, 0.004),
        (days_ago(1), "clinic-a", "llm", "evaluation", 4, 3, 0.03),  # one call unpriced
        (days_ago(0), "clinic-b", "jev", "decision", 777, 777, 0.3),  # never shown here
    )
    page = site.window(lead(site))
    access(page, "usage")
    jev = row(page, "usage-totals", "Jev")
    expect(jev).to_contain_text("Decisions")
    expect(jev.locator("td").nth(2)).to_have_text("10")
    expect(jev.locator("td").last).to_have_text("$0.004")
    model = row(page, "usage-totals", "Language model")
    expect(model.locator("td").last).to_have_text("unknown")
    expect(page.locator('[data-table="usage-days"] tbody tr')).to_have_count(2)
    expect(page.locator('[data-table="usage-days"] tbody tr').first).to_contain_text(days_ago(0))
    expect(page.locator("#admin-body")).not_to_contain_text("777")


def test_an_organization_without_remote_calls_says_so(site: Site) -> None:
    page = site.window(lead(site))
    access(page, "usage")
    expect(page.locator("#admin-body")).to_contain_text("No remote calls")


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_usage_tab_passes_axe_in_both_themes(site: Site, scheme: str) -> None:
    seed(site.store, (days_ago(0), "clinic-a", "llm", "decision", 4, 3, 0.03))
    page = site.window(lead(site), bypass_csp=True, color_scheme=scheme)  # the test injects axe
    access(page, "usage")
    expect(row(page, "usage-totals", "Language model")).to_be_visible()
    assert violations(page) == []
