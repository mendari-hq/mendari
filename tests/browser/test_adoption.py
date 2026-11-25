# SPDX-License-Identifier: Apache-2.0
"""The adoption path at the top of an agent's Overview (dashboard/adoption.js): six steps from
finding an agent to running it on evidence, each with its state, one line of evidence and one
action, the next step marked."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.browser.conftest import TRIAGE
from tests.browser.test_dashboard import open_pack

pytestmark = pytest.mark.browser

STEPS = ["source", "try", "shadow", "validate", "approve", "monitor"]


def test_the_path_shows_six_steps_and_marks_the_next(page: Page) -> None:
    open_pack(page, TRIAGE, "overview")
    path = page.get_by_role("region", name="Adoption path")
    expect(path).to_be_visible()
    steps = path.locator("li.adoption-step")
    expect(steps).to_have_count(len(STEPS))
    assert [steps.nth(i).get_attribute("data-step") for i in range(len(STEPS))] == STEPS
    # The served agent has a source, and the seeded server has made decisions: both done.
    expect(path.locator('[data-step="source"]')).to_have_class("adoption-step done")
    expect(path.locator('[data-step="try"]')).to_have_class("adoption-step done")
    expect(path.locator('[data-step="try"]')).to_contain_text("made so far")
    # No shadow pilot yet: that is the next step, the only one marked, its action the primary.
    current = path.locator('[aria-current="step"]')
    expect(current).to_have_count(1)
    expect(current).to_have_attribute("data-step", "shadow")
    expect(current.locator("a.button.primary")).to_have_text("Start a shadow pilot")
    # A risk-1 agent needs no approval (the governance gate passes); monitoring counts only
    # after validation, which has not happened here.
    expect(path.locator('[data-step="approve"]')).to_have_class("adoption-step done")
    expect(path.locator('[data-step="monitor"]')).to_have_class("adoption-step todo")
    expect(path).to_contain_text("3 of 6 steps done")
    # Every action goes somewhere in the dashboard.
    hrefs = [link.get_attribute("href") for link in path.locator("a.adoption-action").all()]
    assert hrefs == ["#card", "#try", "#cc-title", "#evals", "#card/approval", "#monitoring"]


def test_the_path_fits_a_phone(page: Page) -> None:
    """axe runs on every view, this one included, in test_accessibility.py."""
    page.set_viewport_size({"width": 390, "height": 844})
    open_pack(page, TRIAGE, "overview")
    path = page.get_by_role("region", name="Adoption path")
    expect(path).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth") <= 390


def test_the_shadow_pilot_action_lands_on_the_command_center(page: Page) -> None:
    """The step links to "#cc-title", which is no view: followed as a route, it rendered the
    Overview again from the top, so "Start a shadow pilot" went nowhere. It scrolls to the
    section and focuses its heading."""
    open_pack(page, TRIAGE, "overview")
    page.get_by_role("link", name="Start a shadow pilot").click()
    expect(page.locator("#cc-title")).to_be_focused()
    expect(page.locator("#cc-title")).to_be_in_viewport()
