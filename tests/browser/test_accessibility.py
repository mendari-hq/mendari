# SPDX-License-Identifier: Apache-2.0
"""Accessibility: axe-core (vendored, tests/browser/vendor/axe-core; scripts/vendor_docs_ui.py)
on every dashboard view, an open record drawer, the review of a decision that holds a text and
the developer pages, in both themes, against WCAG 2.0, 2.1 and 2.2 A and AA. axe is injected by
the test (the pages' own CSP never allows it: the context bypasses CSP for that one script), so
nothing in the product loads it. A page is checked once its entrance animations have finished,
as a person reads it.

Swagger UI's own markup on /docs/reference is left out (the page around it is checked): it is
vendored as released, and its known findings (a control inside the operation summary's button,
small targets, the version badges' contrast) are upstream's to fix."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.browser import held_site
from tests.browser.conftest import TRIAGE
from tests.browser.test_dashboard import AGENTS, VIEWS, open_agent, show
from tests.browser.test_registry_approvals import APPROVAL, from_registry, registry_approvals

pytestmark = pytest.mark.browser

VENDOR = Path(__file__).resolve().parent / "vendor" / "axe-core"
AXE = VENDOR / json.loads((VENDOR / "manifest.json").read_text())["version"] / "axe.min.js"
TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]
SCHEMES = ["light", "dark"]

RUN = """async ([tags, exclude]) => {
  const finite = () => document.getAnimations()
    .filter((a) => a.effect && a.effect.getTiming().iterations !== Infinity);
  await Promise.all(finite().map((a) => a.finished.catch(() => null)));
  await new Promise((done) => {
    let last = window.scrollY;
    let steady = 0;
    const step = () => {
      if (window.scrollY === last) {
        if (++steady >= 3) return done();
      } else {
        last = window.scrollY;
        steady = 0;
      }
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  });
  const context = { include: [document], exclude: exclude.map((s) => [s]) };
  const result = await axe.run(context, { runOnly: { type: "tag", values: tags } });
  return result.violations.map((v) => ({
    rule: v.id, impact: v.impact, nodes: v.nodes.slice(0, 3).map((n) => n.target.join(" ")),
  }));
}"""


def violations(page: Page, exclude: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    page.add_script_tag(path=str(AXE))
    found: list[dict[str, Any]] = page.evaluate(RUN, [TAGS, list(exclude)])
    return found


@pytest.mark.parametrize("axe_page", SCHEMES, indirect=True)
@pytest.mark.parametrize("agent_id", AGENTS)
def test_every_view_passes_axe(axe_page: Page, agent_id: str) -> None:
    open_agent(axe_page, agent_id)
    found = {}
    for view in [*VIEWS, "monitoring"]:  # every view the nav shows
        show(axe_page, view)
        found[view] = violations(axe_page)
    assert {view: v for view, v in found.items() if v} == {}


@pytest.mark.parametrize("axe_page", SCHEMES, indirect=True)
def test_an_open_drawer_passes_axe(axe_page: Page) -> None:
    open_agent(axe_page, TRIAGE, "audit")
    axe_page.locator("button.open-record").first.click()
    expect(axe_page.locator(".drawer")).to_be_visible()
    assert violations(axe_page) == []


@pytest.mark.parametrize("scheme", SCHEMES)
def test_the_review_of_a_held_text_passes_axe(
    browser: Browser, held_server: str, scheme: Literal["light", "dark"]
) -> None:
    """A decision that holds two texts and two actions (ADR-0027), on the server that makes
    such decisions: as its card opens (each text with its two verdicts, the labels unavailable
    and saying why), with a text withheld and an action ticked, once resolved (what the review
    did, listed under the queue), and in its record."""
    decided = held_site.decide(held_server, held_site.WRITER, held_site.LETTER)
    context = browser.new_context(base_url=held_server, bypass_csp=True, color_scheme=scheme)
    try:
        page = context.new_page()
        page.clock.install()  # the review waits five seconds for Undo
        open_agent(page, held_site.WRITER, "review")
        card = held_site.card(page, decided["id"])
        expect(card.locator(".review-gate")).to_be_visible()
        assert violations(page) == []
        held_site.verdict(card, "chart_note", "Release as written")
        held_site.verdict(card, "reply_draft", "Withhold")
        held_site.release_box(card, "Send reply").check()
        expect(card.locator(".withheld-note")).to_be_visible()
        assert violations(page) == []
        card.get_by_role("button", name=re.compile("^Confirm Refill")).click()
        expect(card.locator(".undo-bar")).to_be_visible()
        page.clock.run_for(5000)
        expect(page.locator(f'.resolved-item[data-id="{decided["id"]}"]')).to_be_visible()
        assert violations(page) == []
        page.goto(f"/#audit/{decided['id']}")
        expect(page.locator(".drawer .texts-list")).to_be_visible()
        assert violations(page) == []
    finally:
        context.close()


@pytest.mark.parametrize("axe_page", SCHEMES, indirect=True)
def test_an_agent_from_a_registry_passes_axe(axe_page: Page) -> None:
    """The card's registry approval: not approved (facts, recall, form, history), its dialog
    open, and approved."""
    from_registry(axe_page)
    registry_approvals(axe_page, [], recalled=True)
    open_agent(axe_page, TRIAGE, "card")
    assert violations(axe_page) == []
    section = axe_page.locator("#registry-approval")
    section.get_by_label("Reference").fill("minutes 5")
    section.get_by_label("Approver").fill("dr-a")
    section.get_by_role("button", name="Approve this version…").click()
    expect(axe_page.get_by_role("dialog")).to_be_visible()
    assert violations(axe_page) == []
    axe_page.get_by_role("dialog").get_by_role("button", name="Cancel").click()
    registry_approvals(axe_page, [], active=APPROVAL)
    open_agent(axe_page, TRIAGE, "card")
    expect(axe_page.locator("#registry-active")).to_be_visible()
    assert violations(axe_page) == []


@pytest.mark.parametrize("axe_page", SCHEMES, indirect=True)
@pytest.mark.parametrize("path", ["/docs", "/docs/reference"])
def test_the_developer_pages_pass_axe(axe_page: Page, path: str) -> None:
    axe_page.goto(path)
    expect(axe_page.locator("h1").first).to_be_visible()
    swagger = ("#reference",) if path == "/docs/reference" else ()
    assert violations(axe_page, swagger) == []


@pytest.mark.parametrize("axe_page", SCHEMES, indirect=True)
def test_the_command_palette_names_its_active_option(axe_page: Page) -> None:
    """Focus stays in the search box while the arrows move: the box points at the active option
    (a combobox over a listbox), or a screen reader hears nothing as they move."""
    open_agent(axe_page, TRIAGE)
    axe_page.keyboard.press("Control+k")
    box = axe_page.get_by_role("combobox", name="Jump to a view, agent or action")
    expect(box).to_be_focused()
    expect(box).to_have_attribute("aria-expanded", "true")
    options = axe_page.get_by_role("listbox").get_by_role("option")
    expect(options.first).to_have_attribute("aria-selected", "true")
    axe_page.keyboard.press("ArrowDown")
    second = options.nth(1)
    expect(second).to_have_attribute("aria-selected", "true")
    expect(box).to_have_attribute("aria-activedescendant", second.get_attribute("id") or "-")
    expect(axe_page.get_by_role("listbox").get_by_role("option", selected=True)).to_have_count(1)
    assert violations(axe_page) == []
    box.fill("no such thing at all")
    expect(box).not_to_have_attribute("aria-activedescendant", re.compile("."))
    box.fill("audit trail")
    axe_page.keyboard.press("Enter")
    expect(axe_page.locator('.nav a[data-view="audit"]')).to_have_class(re.compile("active"))
    axe_page.keyboard.press("Control+k")
    axe_page.get_by_role("option", name=re.compile("^Evaluations")).click()
    expect(axe_page.locator('.nav a[data-view="evals"]')).to_have_class(re.compile("active"))
