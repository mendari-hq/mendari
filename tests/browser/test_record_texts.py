# SPDX-License-Identifier: Apache-2.0
"""Held texts in a decision's record (ADR-0027, points 4 and 6): what the audit trail's drawer
shows of the texts an agent wrote, before and after their review.

- each text by its output's name with where it stands (held, then released or withheld) and,
  for whoever may read it, the text itself, as text: its line breaks kept, markup never run;
- where there is no text to show, why, in words;
- each action with what became of it: one a review did not name says so in plain words;
- the events say that each text was held and what verdict it got, by name, and never the text.

On `tests/browser/held_site.py`'s server, each decision made and reviewed through its API.
Where it cannot make a state (a text sealed or erased), the record's answer is changed on its
way to the page with Playwright's routing, and the test says so. Every message and text is
synthetic. Run with `uv run pytest -m browser`."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, Route, expect

from tests.browser.held_site import (
    IMAGE,
    LETTER,
    MARKUP,
    MARKUP_TEXT,
    SCRIPT,
    UNKEPT,
    WRITER,
    action_of,
    decide,
    drawer,
    held_ids,
    record,
    review,
    text_of,
)
from tests.browser.test_dashboard import open_agent
from tests.test_held_texts import ARGUMENT, NOTE, REPLY

pytestmark = pytest.mark.browser

NOT_NAMED = "not released: the review did not name it"
POINTER = "Its text is shown above, under “What the agent wrote”."


def test_the_record_of_a_decision_that_waits_shows_its_held_texts(
    held_console: Page, held_server: str
) -> None:
    """Before its review, the decision's record lists each text as held, with its text for
    whoever may read it, and its events say each was held: by name, never the text."""
    decided = decide(held_server, WRITER, LETTER)
    open_agent(held_console, WRITER, "audit")
    shown = drawer(held_console, decided["id"])
    expect(shown.get_by_role("heading", name="What the agent wrote")).to_be_visible()
    for name, mark in (("chart_note", NOTE), ("reply_draft", REPLY)):
        text = text_of(shown, name)
        expect(text.locator(".badge")).to_have_text("Held")
        expect(text.locator(".text-body")).to_contain_text(mark)
    expect(text_of(shown, "reply_draft")).to_contain_text(
        "A short reply to the patient. Staff read it first."  # what its manifest says of it
    )
    expect(shown.get_by_role("radio")).to_have_count(0)  # reviewed in the queue, not here
    kept = shown.locator(".timeline > li", has_text="Text held for review")
    expect(kept).to_have_count(2)
    expect(kept.last).to_contain_text("Reply draft")
    expect(kept.last.locator(".badge")).to_have_text("Held")
    expect(kept.last).to_contain_text(POINTER)
    expect(shown.locator(".timeline")).not_to_contain_text(REPLY)  # shown once, under its name
    kept.last.locator("summary").click()
    expect(kept.last.locator("pre.json")).to_contain_text('"output": "reply_draft"')


def test_the_record_shows_a_text_as_text_with_its_line_breaks_never_as_markup(
    held_console: Page, held_server: str
) -> None:
    """A `<script>` and an `<img onerror>` in what an agent wrote are characters on the screen:
    nothing runs and no element is made of them. The text's line breaks are kept."""
    decided = decide(held_server, WRITER, MARKUP)
    open_agent(held_console, WRITER, "audit")
    shown = drawer(held_console, decided["id"])
    body = text_of(shown, "reply_draft").locator(".text-body")
    assert body.evaluate("(el) => el.textContent") == MARKUP_TEXT
    assert body.inner_text() == MARKUP_TEXT  # as it is laid out: each line on its own
    assert SCRIPT in body.inner_text() and IMAGE in body.inner_text()
    assert body.locator("*").count() == 0  # characters only: no element came of it
    assert shown.locator(".texts-list img, .texts-list script").count() == 0
    assert held_console.evaluate("window.pwned") is None


def test_the_record_of_a_reviewed_decision_says_where_each_text_stands_and_why_no_action_went(
    held_console: Page, held_server: str
) -> None:
    """One text released and one withheld, and no action named: the record shows each text
    with its verdict, and still its text to whoever may read it; each held action is denied,
    and says in plain words that the review did not name it. The events give each verdict and
    who gave it."""
    decided = decide(held_server, WRITER, LETTER)
    review(
        held_server,
        decided["id"],
        "refill",
        texts={"chart_note": "release", "reply_draft": "withhold"},
    )
    open_agent(held_console, WRITER, "audit")
    shown = drawer(held_console, decided["id"])
    note, reply = text_of(shown, "chart_note"), text_of(shown, "reply_draft")
    expect(note.locator(".badge")).to_have_text("Released")
    expect(note.locator(".text-body")).to_have_text(f"{NOTE} Asked for a refill.")
    expect(reply.locator(".badge")).to_have_text("Withheld")
    expect(reply.locator(".text-body")).to_contain_text(REPLY)
    for name in ("Assign queue", "Send reply"):
        expect(action_of(shown, name).locator(".badge")).to_have_text("Denied")
        expect(action_of(shown, name)).to_contain_text(NOT_NAMED)
    expect(action_of(shown, "Send reply")).to_contain_text(f"{ARGUMENT} Received.")
    judged = shown.locator(".timeline > li", has_text="Text released or withheld by review")
    expect(judged).to_have_count(2)
    expect(judged.first).to_contain_text("Chart note")
    expect(judged.first.locator(".badge")).to_have_text("Released")
    expect(judged.first).to_contain_text("rn-1")
    expect(judged.last).to_contain_text("Reply draft")
    expect(judged.last.locator(".badge")).to_have_text("Withheld")
    denied = shown.locator(".timeline > li", has_text="Action released or denied by review")
    expect(denied.last).to_contain_text(f"{NOT_NAMED} · rn-1")
    for mark in (NOTE, REPLY):  # shown once, under its name: the timeline repeats no text
        expect(shown.locator(".timeline")).not_to_contain_text(mark)


def test_the_record_shows_an_action_released_by_name(held_console: Page, held_server: str) -> None:
    """A free-text action the review named is released, as the reviewer confirmed it."""
    decided = decide(held_server, WRITER, LETTER)
    ids = held_ids(record(held_server, decided["id"]))
    review(
        held_server,
        decided["id"],
        "refill",
        texts={"chart_note": "release", "reply_draft": "release"},
        release_actions=[ids["send_reply"]],
    )
    open_agent(held_console, WRITER, "audit")
    shown = drawer(held_console, decided["id"])
    for name in ("Assign queue", "Send reply"):
        expect(action_of(shown, name).locator(".badge")).to_have_text("Released")
        expect(action_of(shown, name)).to_contain_text("the reviewer confirmed the decision")


def test_the_record_says_why_a_text_was_not_kept(held_console: Page, held_server: str) -> None:
    """An agent that keeps only its input's hash keeps no text: the record says where each
    stands, and why there is none to show."""
    decided = decide(held_server, UNKEPT, LETTER)
    review(
        held_server,
        decided["id"],
        "refill",
        texts={"chart_note": "release", "reply_draft": "withhold"},
    )
    open_agent(held_console, UNKEPT, "audit")
    shown = drawer(held_console, decided["id"])
    kept = text_of(shown, "reply_draft")
    expect(kept.locator(".badge")).to_have_text("Withheld")
    expect(kept.locator(".text-body")).to_have_count(0)
    expect(kept.locator(".text-missing")).to_have_text(
        "Not kept here: this agent keeps only a hash of its input (audit.store_input)."
    )
    expect(shown).not_to_contain_text(REPLY)


@pytest.mark.parametrize(
    ("status", "phi_mode", "why"),
    [
        ("sealed", "off", "Kept sealed under a key this deployment does not hold."),
        (
            "sealed",
            "encrypt",
            "Kept sealed: reviewers and administrators can read it, and each read is logged.",
        ),
        ("erased", "off", "Erased, with what this decision kept of its input."),
        ("missing", "off", "Missing: verify the chain with --notes."),
        ("not_kept", "hash", "Not kept here: PHI mode hash keeps no free text."),
    ],
)
def test_the_record_says_why_a_text_cannot_be_shown(
    held_console: Page, held_server: str, status: str, phi_mode: str, why: str
) -> None:
    """A text sealed for this reader, sealed under a key the deployment no longer holds, erased
    or missing, and one PHI mode `hash` never kept. The server here makes none of these: the
    record's answer and the deployment's PHI mode are changed on their way to the page
    (Playwright routing). The words say which, and the text's event says the same of it."""
    decided = decide(held_server, WRITER, LETTER)

    def unshown(route: Route) -> None:
        found = route.fetch().json()
        for text in found["outputs"]:
            text.pop("text", None)
            text["text_status"] = status
        for event in found["events"]:
            if event["event"] == "output.held":
                event["note"] = None
                event["note_status"] = None if status == "not_kept" else status
        route.fulfill(json=found)

    def mode(route: Route) -> None:
        route.fulfill(json={**route.fetch().json(), "phi_mode": phi_mode})

    held_console.route(f"**/api/audit/{decided['id']}", unshown)
    held_console.route("**/api/health", mode)
    open_agent(held_console, WRITER, "audit")
    shown = drawer(held_console, decided["id"])
    for name in ("chart_note", "reply_draft"):
        text = text_of(shown, name)
        expect(text.locator(".text-missing")).to_have_text(why)
        expect(text.locator(".text-body")).to_have_count(0)
    expect(shown.locator(".texts-list")).not_to_contain_text(REPLY)
    kept = shown.locator(".timeline > li", has_text="Text held for review")
    expect(kept).to_have_count(2)
    said = {
        "sealed": "Text (only reviewers and admins can read it)",
        "erased": "Text (erased)",
        "missing": "Text (missing: verify the chain with --notes)",
    }
    if status in said:
        expect(kept.first).to_contain_text(said[status])
    expect(kept.first).not_to_contain_text(POINTER)
