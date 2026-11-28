# SPDX-License-Identifier: Apache-2.0
"""Proposed actions in the console (spec §13): the review queue shows what confirming a decision
releases, the audit view shows a decision's actions and their events, and the Decision Card lists
what an agent may ask to have done. On a server of its own, serving the actions fixture."""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, Page, expect

from mendari.server.app import create_app
from tests.browser.conftest import TRIAGE, serving
from tests.browser.test_dashboard import open_agent, show
from tests.conftest import ROOT, TRIAGE_DIR, message

pytestmark = pytest.mark.browser

MINI = ROOT / "tests" / "fixtures" / "mini-agents" / "actions-mini"
AGENT = "mendari-test/actions-mini"
VISIT = (
    "I need to move my appointment."  # HUMAN_REVIEW: a read released, a write and a contact held
)
# The same decision, its held write and contact taking enumerated arguments (no free text).
RESCHEDULE = "Please reschedule my visit."
FREE_TEXT = "Its arguments can hold free text: read them before releasing."
NOT_NAMED = "not released: the review did not name it"
REFILL = "Could you refill my lisinopril?"  # AUTO: a draft and a contact released
UNDECLARED = "Try an undeclared thing."  # an action the manifest does not declare: blocked


@pytest.fixture(scope="module")
def actions_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    root = Path(tmp_path_factory.mktemp("actions"))
    shutil.copytree(MINI, root / "agents" / "actions-mini")
    shutil.copytree(TRIAGE_DIR, root / "agents" / "triage")
    app = create_app(db_path=root / "mendari.db", agents_paths=[root / "agents"])
    with serving(app) as port:
        yield f"http://127.0.0.1:{port}"


@pytest.fixture
def console(browser: Browser, actions_server: str) -> Iterator[Page]:
    context = browser.new_context(base_url=actions_server)
    page = context.new_page()
    errors: list[str] = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda error: errors.append(str(error)))
    yield page
    context.close()
    assert errors == []


def decide(base: str, body: str) -> dict[str, Any]:
    answer = httpx.post(f"{base}/api/agents/{AGENT}/decide", json=message(body), timeout=30)
    assert answer.status_code == 200, answer.text
    decided: dict[str, Any] = answer.json()
    return decided


def record(base: str, decision_id: str) -> dict[str, Any]:
    found: dict[str, Any] = httpx.get(f"{base}/api/audit/{decision_id}", timeout=30).json()
    return found


def test_the_queue_shows_what_confirming_releases_and_free_text_only_by_its_tick(
    console: Page, actions_server: str
) -> None:
    decided = decide(actions_server, VISIT)
    open_agent(console, AGENT, "review")
    card = console.locator(f'.review-card[data-id="{decided["id"]}"]')
    expect(card.get_by_role("heading", name="Proposed actions")).to_be_visible()
    rows = card.locator(".actions-list > li")
    expect(rows).to_have_count(3)
    # Each action by name, with its effect and what may be done with it.
    expect(rows.nth(0)).to_contain_text("Look up medications")
    expect(rows.nth(0).locator(".chip")).to_have_text("read")
    expect(rows.nth(0).locator(".badge")).to_have_text("Released")
    expect(rows.nth(1)).to_contain_text("File note")
    expect(rows.nth(1).locator(".chip")).to_have_text("write")
    expect(rows.nth(1).locator(".badge")).to_have_text("Held")
    expect(rows.nth(1)).to_contain_text("waits for this decision's reviewer")
    expect(rows.nth(1)).to_contain_text("Asked to move a visit.")  # its arguments, as inputs are
    expect(rows.nth(2).locator(".chip")).to_have_text("contact")
    # Only a held action can be chosen. Both take free text here, so neither is released unless
    # the reviewer ticks it, and each says why (ADR-0027).
    boxes = card.get_by_role("checkbox", name="Release when I confirm")
    expect(boxes).to_have_count(2)
    expect(rows.nth(0).get_by_role("checkbox")).to_have_count(0)
    for index in range(2):
        expect(boxes.nth(index)).not_to_be_checked()
        expect(rows.nth(index + 1)).to_contain_text(FREE_TEXT)
    boxes.nth(0).check()  # the note for the chart, read; the reply to the patient: not this time
    # A release is final, so confirming waits for Undo, saying what it releases.
    card.get_by_role("button", name=re.compile("^Confirm Appointment")).click()
    bar = card.locator(".undo-bar")
    expect(bar).to_contain_text("Confirm Appointment and release 1 action")
    bar.get_by_role("button", name="Undo").click()
    expect(card.locator(".undo-bar")).to_have_count(0)
    assert record(actions_server, decided["id"])["review_status"] == "pending"  # nothing was sent
    # Confirmed for real: sent when the queue is left, as an override is.
    card.get_by_role("button", name=re.compile("^Confirm Appointment")).click()
    expect(card.locator(".undo-bar")).to_be_visible()
    show(console, "audit")
    found = record(actions_server, decided["id"])
    assert (found["review_status"], found["human_override"]) == ("resolved", False)
    assert [(a["name"], a["disposition"], a["reason"]) for a in found["actions"]] == [
        ("look_up_medications", "released", "with_decision"),
        ("file_note", "released", "confirmed"),
        ("send_reply", "denied", "not_named"),
    ]


def test_an_action_of_enumerated_arguments_is_released_by_default_and_a_reviewer_denies_one(
    console: Page, actions_server: str
) -> None:
    """No free text in either held action: each starts ticked, as it always did, and the one
    the reviewer unticks is refused by name while the label is confirmed."""
    decided = decide(actions_server, RESCHEDULE)
    open_agent(console, AGENT, "review")
    card = console.locator(f'.review-card[data-id="{decided["id"]}"]')
    boxes = card.get_by_role("checkbox", name="Release when I confirm")
    expect(boxes).to_have_count(2)
    for index in range(2):
        expect(boxes.nth(index)).to_be_checked()
    expect(card).not_to_contain_text(FREE_TEXT)
    boxes.nth(1).uncheck()  # the standard reply to the patient: not this time
    card.get_by_role("button", name=re.compile("^Confirm Appointment")).click()
    expect(card.locator(".undo-bar")).to_contain_text("Confirm Appointment and release 1 action")
    show(console, "audit")
    found = record(actions_server, decided["id"])
    assert (found["review_status"], found["human_override"]) == ("resolved", False)
    assert [(a["name"], a["disposition"], a["reason"]) for a in found["actions"]] == [
        ("look_up_medications", "released", "with_decision"),
        ("assign_queue", "released", "confirmed"),
        ("send_template", "denied", "reviewer_denied"),
    ]


def test_another_label_denies_the_held_actions(console: Page, actions_server: str) -> None:
    decided = decide(actions_server, VISIT)
    open_agent(console, AGENT, "review")
    card = console.locator(f'.review-card[data-id="{decided["id"]}"]')
    card.get_by_role("button", name="Refill", exact=True).click()
    expect(card.locator(".undo-bar")).to_contain_text("Override to Refill")
    show(console, "audit")  # leaving sends it
    found = record(actions_server, decided["id"])
    assert found["human_override"] is True
    assert [a["disposition"] for a in found["actions"]] == ["released", "denied", "denied"]


def test_a_decision_with_nothing_held_confirms_in_one_tap_as_before(
    console: Page, actions_server: str
) -> None:
    triage = httpx.post(
        f"{actions_server}/api/agents/{TRIAGE}/decide",
        json={"message": {"body": "Crushing chest pain and my left arm is numb"}},
        timeout=30,
    ).json()
    open_agent(console, TRIAGE, "review")
    card = console.locator(f'.review-card[data-id="{triage["id"]}"]')
    expect(card.locator(".actions-list")).to_have_count(0)  # an older agent's card is as it was
    expect(card.get_by_role("heading", name="Proposed actions")).to_have_count(0)
    card.get_by_role("button", name=re.compile("^Confirm Nurse")).click()
    expect(card).to_have_count(0)  # no Undo bar: sent at once


def test_the_audit_view_shows_a_decisions_actions_and_their_events(
    console: Page, actions_server: str
) -> None:
    decided = decide(actions_server, UNDECLARED)
    open_agent(console, AGENT, "audit")
    console.goto(f"/#audit/{decided['id']}")
    drawer = console.locator(".drawer")
    expect(drawer.get_by_role("heading", name="Actions", exact=True)).to_be_visible()
    rows = drawer.locator(".actions-list > li")
    expect(rows).to_have_count(2)
    expect(rows.nth(0)).to_contain_text("Draft reply")
    expect(rows.nth(0).locator(".badge")).to_have_text("Released")
    # An undeclared action has no name the chain keeps; who may read the decision sees what the
    # agent called it, with its arguments.
    expect(rows.nth(1)).to_contain_text("An action the agent did not declare")
    expect(rows.nth(1).locator(".chip").first).to_have_text("undeclared")
    expect(rows.nth(1).locator(".badge")).to_have_text("Blocked")
    expect(rows.nth(1)).to_contain_text("the agent's manifest declares no such action")
    expect(rows.nth(1)).to_contain_text("delete_chart_sentinel")
    expect(rows.nth(0).get_by_role("checkbox")).to_have_count(0)  # nothing to choose here
    # The events: each proposal in the chain, its arguments apart from it.
    events = drawer.locator(".timeline > li")
    expect(events).to_have_count(3)
    expect(events.nth(1)).to_contain_text("Action proposed")
    expect(events.nth(1)).to_contain_text("Arguments:")
    expect(events.nth(2)).to_contain_text("An action the agent did not declare")
    events.nth(2).locator("summary").click()
    payload = events.nth(2).locator("pre.json")
    expect(payload).to_contain_text('"reason": "undeclared"')
    expect(payload).to_contain_text('"name": null')
    expect(payload).not_to_contain_text("delete_chart_sentinel")  # never in the chain
    expect(payload).not_to_contain_text("SENTINEL-ARGUMENT-TEXT")


def test_a_reviewed_decisions_events_say_what_was_released(
    console: Page, actions_server: str
) -> None:
    decided = decide(actions_server, VISIT)
    held = {a["name"]: a["id"] for a in record(actions_server, decided["id"])["actions"]}
    resolved = httpx.post(
        f"{actions_server}/api/review/{decided['id']}",
        # Free text is released by name (ADR-0027): the note is named, the reply is not.
        json={
            "final_label": "appointment",
            "reviewer": "rn-1",
            "release_actions": [held["file_note"]],
        },
        timeout=30,
    )
    assert resolved.status_code == 200
    open_agent(console, AGENT, "audit")
    console.goto(f"/#audit/{decided['id']}")
    drawer = console.locator(".drawer")
    released = drawer.locator(".timeline > li", has_text="Action released or denied by review")
    expect(released).to_have_count(2)
    expect(released.first).to_contain_text("File note")
    expect(released.first.locator(".badge")).to_have_text("Released")
    expect(released.first).to_contain_text("the reviewer confirmed the decision · rn-1")
    expect(released.last).to_contain_text("Send reply")
    expect(released.last.locator(".badge")).to_have_text("Denied")
    expect(released.last).to_contain_text(f"{NOT_NAMED} · rn-1")
    rows = drawer.locator(".actions-list > li")
    expect(rows.nth(1).locator(".badge")).to_have_text("Released")
    expect(rows.nth(2).locator(".badge")).to_have_text("Denied")
    expect(rows.nth(2)).to_contain_text(NOT_NAMED)


def test_the_card_lists_the_declared_actions_and_how_each_is_gated(console: Page) -> None:
    open_agent(console, AGENT, "card")
    expect(console.locator('.toc a[href="#card/actions"]')).to_have_text("Actions")
    section = console.locator("#card-actions")
    expect(section).to_contain_text("Mendari authorizes each action and runs none")
    rows = section.locator("tbody tr")
    expect(rows).to_have_count(8)
    expect(rows.nth(0)).to_contain_text("look_up_medications")
    expect(rows.nth(0)).to_contain_text("With any decision that is not blocked.")
    expect(rows.nth(0)).to_contain_text("not checked")
    # A contact whose argument is a free string: never without a person, why, and that a
    # reviewer has to name it (ADR-0027).
    expect(rows.nth(2).locator(".chip")).to_have_text("contact")
    expect(rows.nth(2)).to_contain_text(
        "Never without a person: when a reviewer confirms the decision and names this action, "
        "since its arguments can hold free text."
    )
    expect(rows.nth(3)).to_contain_text("Never without a person")  # tightened by the manifest
    expect(rows.nth(3)).to_contain_text("medication")
    expect(rows.nth(5).locator(".chip")).to_have_text("order")
    # The same effect with an enumerated argument is released with an automated decision.
    expect(rows.nth(6)).to_contain_text("send_template")
    expect(rows.nth(6).locator(".chip")).to_have_text("contact")
    expect(rows.nth(6)).to_contain_text("Only with an automated decision")
    # An agent that declares none has no such section, and no link to one.
    open_agent(console, TRIAGE, "card")
    expect(console.locator("#card-actions")).to_have_count(0)
    expect(console.locator('.toc a[href="#card/actions"]')).to_have_count(0)


def test_the_views_with_actions_have_no_unnamed_control_and_fit_a_phone(
    console: Page, actions_server: str
) -> None:
    decide(actions_server, VISIT)
    console.set_viewport_size({"width": 390, "height": 844})
    open_agent(console, AGENT, "review")
    card = console.locator(".review-card").first
    expect(card.locator(".actions-list")).to_be_visible()
    unnamed = console.evaluate(
        """() => [...document.querySelectorAll('main input, main button')]
          .filter((el) => !(el.closest('label') || el.getAttribute('aria-label')
            || (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`))
            || (el.tagName === 'BUTTON' && el.textContent.trim())))
          .map((el) => el.outerHTML.slice(0, 90))"""
    )
    assert unnamed == []
    wide = console.evaluate("() => document.documentElement.scrollWidth > window.innerWidth + 1")
    assert not wide  # nothing pushes the page sideways
