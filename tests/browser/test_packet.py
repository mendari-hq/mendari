# SPDX-License-Identifier: Apache-2.0
"""The Committee packet button, where a hospital reads the validation brief (the Overview's
governance lens): one click downloads the agent's packet as one page. And the gates beside it
say "Not enough data" where nothing was measured.

On a server of its own that serves the bundled demo agent."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Browser, Page, expect

from mendari.server.app import create_app
from tests.browser.conftest import serving
from tests.conftest import ROOT

pytestmark = pytest.mark.browser

AGENT = "mendari/portal-message-agent"


@pytest.fixture(scope="module")
def demo_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    db = Path(tmp_path_factory.mktemp("packet")) / "mendari.db"
    with serving(create_app(db_path=db, packs_paths=[ROOT / "agents"])) as port:
        base = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=base, timeout=30) as client:
            body = {"message": {"body": "Please send a refill of my metformin."}}
            assert client.post(f"/api/agents/{AGENT}/decide", json=body).status_code == 200
        yield base


@pytest.fixture
def overview(browser: Browser, demo_server: str) -> Iterator[Page]:
    context = browser.new_context(base_url=demo_server, accept_downloads=True)
    page = context.new_page()
    page.goto("/#overview")
    expect(page.locator('.nav a[data-view="overview"]')).to_have_class(re.compile("active"))
    expect(page.locator("#view")).not_to_have_attribute("aria-busy", "true")
    expect(page.locator("#cc-title")).to_be_visible()
    yield page
    context.close()


def test_the_committee_packet_button_downloads_one_page(overview: Page, tmp_path: Path) -> None:
    button = overview.get_by_role("button", name="Committee packet")
    expect(button).to_be_visible()
    with overview.expect_download() as started:
        button.click()
    download = started.value
    assert download.suggested_filename == "portal-message-agent-committee-packet.html"
    text = Path(download.path()).read_text(encoding="utf-8")
    assert text.startswith("<!doctype html>") and "<script" not in text
    assert "Committee packet" in text and "Portal Message Agent" in text
    assert "Appendix A. Not stated" in text and "Appendix B. Crosswalk" in text
    expect(button).to_be_enabled()  # it comes back from "Building…"
    # Opened from the disk, as a committee member would: the page loads nothing but itself,
    # and shows every section.
    kept = tmp_path / download.suggested_filename
    download.save_as(kept)
    saved = overview.context.new_page()
    asked: list[str] = []
    saved.on("request", lambda request: asked.append(request.url))
    saved.goto(kept.as_uri())
    expect(saved.locator("h1")).to_have_text("Portal Message Agent")
    expect(saved.locator("section h2")).to_have_count(13)  # eleven sections, two appendices
    assert asked == [kept.as_uri()]
    saved.close()


def test_a_gate_with_nothing_to_measure_reads_not_enough_data(overview: Page) -> None:
    """One automated decision that nobody rated, and no label: the safety, agreement and drift
    gates have no data. None reads "Pass", and the brief is not called signed."""
    gates = overview.locator(".gate-list .gate-row")
    expect(gates).to_have_count(5)
    said = {
        row.locator(".gate-label").inner_text(): row.locator(".badge").inner_text()
        for row in gates.all()
    }
    assert said == {
        "Local pilot volume": "Review",
        "Safety & zero automated errors": "Not enough data",
        "Clinician agreement": "Not enough data",
        "Governance & risk gates": "Pass",
        "Calibration & operational drift": "Not enough data",
    }
    section = overview.locator(".command-center")
    expect(section).to_contain_text("computed here and not signed")
    expect(section).not_to_contain_text("signed readiness brief")
    # The adoption path follows the gates: nothing validated yet, so that step is still to do.
    path = overview.get_by_role("region", name="Adoption path")
    expect(path.locator('[data-step="validate"]')).to_have_class(re.compile("todo"))
    expect(path.locator('[data-step="validate"]')).to_contain_text("has a reviewer's label")
    expect(path.locator('[data-step="monitor"]')).to_have_class(re.compile("todo"))
