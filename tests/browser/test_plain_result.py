# SPDX-License-Identifier: Apache-2.0
"""The result in words (LP-05b): Try it's result shows the agent's names, never its codes; each
code is one click away ("Show codes"), since an agent's names are its author's claim; the engine's
detail (probabilities, providers, thresholds, the reasons as the engine gave them) is in the
decision record's drawer. Checked on every served agent, and with axe in both themes."""

from __future__ import annotations

import re

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.browser.test_accessibility import violations
from tests.browser.conftest import CHART, TRIAGE
from tests.browser.test_dashboard import PACKS, open_pack

pytestmark = pytest.mark.browser


def decide(page: Page, pack_id: str) -> None:
    """Try it on an agent: its first behaviour test, or for a message agent the Refill sample."""
    open_pack(page, pack_id, "try")
    if page.locator("#example").count():
        page.locator("#decide").click()
    else:
        page.get_by_role("button", name="Refill", exact=True).click()
        page.locator("#decide").click()
    expect(page.locator("#result-title")).to_be_focused()


def codes(live_server: str, pack_id: str) -> set[str]:
    """The agent's codes that never pass for words: its question keys, and every label written
    with an underscore (`front_desk`, `see_today`)."""
    manifest = httpx.get(f"{live_server}/api/agents/{pack_id}").json()["manifest"]
    found = set(manifest["questions"])
    for question in manifest["questions"].values():
        found |= {label for label in question.get("labels") or [] if "_" in label}
    return found


@pytest.mark.parametrize("pack_id", PACKS)
def test_the_result_shows_names_and_no_raw_code(page: Page, live_server: str, pack_id: str) -> None:
    decide(page, pack_id)
    result = page.locator("#result .result")
    shown = result.inner_text()  # what a person sees: hidden codes are not in it
    leaked = sorted(
        code for code in codes(live_server, pack_id) if re.search(rf"\b{code}\b", shown)
    )
    assert leaked == []
    assert "threshold" not in shown and " ms" not in shown  # the engine's detail is in the record
    page.get_by_role("button", name="Show codes").click()
    assert any(code in result.inner_text() for code in codes(live_server, pack_id))


def test_the_engine_detail_is_in_the_record(page: Page) -> None:
    decide(page, TRIAGE)
    main_view = page.locator("#result .result").inner_text()
    assert "auto threshold" not in main_view
    page.get_by_role("link", name=re.compile("^Engine detail")).click()
    drawer = page.locator(".drawer")
    expect(drawer).to_contain_text("auto threshold")  # the reasons as the engine gave them
    expect(drawer.locator("figure.chart")).not_to_have_count(0)  # each answer's probabilities
    expect(drawer.locator("#drawer-title")).not_to_contain_text("_")  # named, not coded


def test_a_rule_reason_reads_in_the_authors_words(page: Page) -> None:
    open_pack(page, TRIAGE, "try")
    page.get_by_role("button", name="Chest pain", exact=True).click()
    page.locator("#decide").click()
    why = page.locator("#result .reasons")
    expect(why).to_contain_text("Possible same-day symptoms. A nurse reads the message now.")
    expect(why).not_to_contain_text("rule ")  # the rule's name is the engine's


@pytest.mark.parametrize("axe_page", ["light", "dark"], indirect=True)
def test_the_result_passes_axe(axe_page: Page) -> None:
    decide(axe_page, TRIAGE)
    assert violations(axe_page) == []
    axe_page.get_by_role("button", name="Show codes").click()
    assert violations(axe_page) == []


def test_a_refused_input_says_what_is_wrong(page: Page) -> None:
    """A refusal names the field and the rule (schema paths, not an agent's codes), so a person
    can fix the input where it is, in Try it."""
    open_pack(page, CHART, "try")
    page.locator('[data-mode="json"]').click()
    record = page.locator("#record")
    record.fill(record.input_value().replace("{", '{"my_edit": 1, ', 1))
    page.locator("#decide").click()
    expect(page.locator("#result-title")).to_have_text(re.compile("^Blocked"))
    expect(page.locator("#result .reasons")).to_contain_text(
        "The input was refused: (root): has fields the schema does not allow: my_edit"
    )
    expect(page.get_by_role("heading", name="Answers")).to_have_count(0)  # none: nothing was asked


def test_a_provider_failure_keeps_its_safety_caveat_and_names_the_question(page: Page) -> None:
    """A provider that failed leaves a question unanswered: the result names it, keeps the
    engine's caveat that safety checks may be incomplete, and lists it as unanswered."""
    failed = {
        "id": "00000000-0000-4000-8000-000000000001",
        "outcome": "HUMAN_REVIEW",
        "recommendation": "pharmacy",
        "priority": "routine",
        "confidence": 0.97,
        "primary": "destination",
        "rule": None,
        "fallback": None,
        "answers": {"destination": {"label": "pharmacy", "confidence": 0.97, "evidence": []}},
        "errors": {"urgency": "provider_unavailable"},
        "reasons": ["provider failure (provider_unavailable); safety checks may be incomplete"],
    }
    page.route(
        f"**/api/agents/{TRIAGE}/decide",
        lambda route: (
            route.fulfill(json=failed) if route.request.method == "POST" else route.continue_()
        ),
    )
    open_pack(page, TRIAGE, "try")
    page.get_by_role("button", name="Refill", exact=True).click()
    page.locator("#decide").click()
    expect(page.locator("#result .reasons")).to_have_text(
        "No answer for Same-day symptoms; safety checks may be incomplete, so a person checks."
    )
    unanswered = page.locator("#result .answers div", has_text="Same-day symptoms")
    expect(unanswered).to_contain_text("no answer")
