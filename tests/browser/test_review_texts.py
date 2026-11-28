# SPDX-License-Identifier: Apache-2.0
"""The review of a decision that holds texts (ADR-0027, points 4 and 5): what a reviewer reads
and does in the queue before such a decision is resolved, and what the queue says afterwards.

- a held text is on the reviewer's screen as text, with its line breaks, never as markup, and a
  long one scrolls inside its box, which the keyboard reaches;
- each text takes a verdict, "Release as written" or "Withhold", with neither chosen at first,
  and the labels that resolve the review wait for every verdict, saying why;
- a held action is released by its tick: one whose arguments can hold free text starts
  unticked, and each text withheld unticks every one;
- the review sends each verdict (`texts`), the ticks that need a name (`release_actions`) and
  the refusals (`deny_actions`), never both for one action;
- afterwards the queue lists what the review did to each text and action, as the server
  answered, with the way to the decision's record (tests/browser/test_record_texts.py);
- a refused review says why, shows nothing as done and keeps the reviewer's choices.

On `tests/browser/held_site.py`'s server. Where it cannot make a state (a text sealed or
erased, PHI mode `hash`, a reader who may not review, a refusal of a well-formed review), the
request is answered or changed with Playwright's routing, and the test says so. Every message
and text is synthetic. Run with `uv run pytest -m browser`."""

from __future__ import annotations

import json
import re
import time
from typing import Any

import pytest
from playwright.sync_api import Browser, Locator, Page, Request, Route, expect

from tests.browser.held_site import (
    ALONE,
    ALONE_TEXT,
    DEMO,
    HOSTILE_KEY,
    IMAGE,
    LETTER,
    LONG,
    LONG_TEXT,
    MARKUP,
    MARKUP_TEXT,
    ODD,
    PARKING,
    PLAIN,
    SCRIPT,
    UNKEPT,
    UNSEEN,
    VAGUE,
    WRITER,
    WRITTEN,
    acted,
    action_of,
    ask,
    card,
    decide,
    drawer,
    elapse,
    held_ids,
    record,
    release_box,
    text_of,
    texts,
    verdict,
    window,
)
from tests.browser.test_dashboard import PHONE, UNDER_24, UNNAMED, open_agent, show
from tests.test_held_texts import ARGUMENT, NOTE, REPLY

pytestmark = pytest.mark.browser

RELEASE, WITHHOLD = "Release as written", "Withhold"
WAITS = "{} a verdict before you can resolve this review."
READY = "Every text has a verdict: you can resolve this review."
FREE_TEXT = "Its arguments can hold free text: read them before releasing."
WITHHELD = "A text is withheld: no held action is released unless you tick it."
OFF = "Will not be released."
ELSEWHERE = "Read the draft where your own system shows it, then give your verdict."
NOT_NAMED = "not released: the review did not name it"
ASKED = "asked for after the decision"
READ = ("look_up_medications", "released", "with_decision")  # released with the decision


def reviews(page: Page, decision_id: str) -> list[dict[str, Any]]:
    """What the page sends to resolve the decision, as it sends it: filled as the test goes."""
    sent: list[dict[str, Any]] = []

    def seen(request: Request) -> None:
        if request.method == "POST" and request.url.endswith(f"/api/review/{decision_id}"):
            sent.append(json.loads(request.post_data or "{}"))

    page.on("request", seen)
    return sent


def reviewed(base: str, decision_id: str) -> dict[str, Any]:
    """The decision's record once its review is recorded. A page that is closing sends the
    review on its way out, so nothing in the page says when it arrived."""
    deadline = time.monotonic() + 10
    while (found := record(base, decision_id))["review_status"] != "resolved":
        assert time.monotonic() < deadline, "the review was not recorded"
        time.sleep(0.05)
    return found


def confirm(held: Locator, label: str) -> Locator:
    return held.get_by_role("button", name=re.compile(f"^Confirm {label}"))


# -- reading: a held text is on the reviewer's screen, as text ------------------------------------


def test_a_held_text_is_shown_as_text_with_its_line_breaks_never_as_markup(
    held_console: Page, held_server: str
) -> None:
    """The text of a decision that awaits review is in its card, under its output's name and
    what the agent's manifest says of it. A `<script>` and an `<img onerror>` in it are
    characters on the screen: nothing runs and no element is made. Its line breaks are kept."""
    decided = decide(held_server, WRITER, MARKUP)
    open_agent(held_console, WRITER, "review")
    held = card(held_console, decided["id"])
    expect(held.get_by_role("heading", name="What the agent wrote")).to_be_visible()
    text = text_of(held, "reply_draft")
    expect(text).to_contain_text("Reply draft")
    expect(text.locator(".badge")).to_have_text("Held")
    expect(text).to_contain_text("A short reply to the patient. Staff read it first.")
    body = text.locator(".text-body")
    assert body.evaluate("(el) => el.textContent") == MARKUP_TEXT
    assert body.inner_text() == MARKUP_TEXT  # as it is laid out: each line on its own
    assert SCRIPT in body.inner_text() and IMAGE in body.inner_text()
    assert body.locator("*").count() == 0  # characters only: no element came of it
    assert held.locator("img, script").count() == 0
    assert held_console.evaluate("window.pwned") is None


def test_what_an_action_would_say_is_shown_as_text_never_as_markup(
    held_console: Page, held_server: str
) -> None:
    """A reviewer reads an action's arguments before naming it, so they are on the card as
    characters: a `<script>` and an `<img onerror>` in a free-text argument, or in a key of an
    action that has no schema, make no element and run nothing."""
    decided = decide(held_server, WRITER, ODD)
    open_agent(held_console, WRITER, "review")
    held = card(held_console, decided["id"])
    said = action_of(held, "Send reply").locator("dl.facts dd")
    assert said.evaluate("(el) => el.textContent") == MARKUP_TEXT
    assert said.locator("*").count() == 0  # characters only
    read = action_of(held, "Look up medications").locator("dl.facts")
    assert read.locator("dt").evaluate("(el) => el.textContent") == HOSTILE_KEY
    assert read.locator("dd").evaluate("(el) => el.textContent") == SCRIPT
    assert held.locator(".actions-list").locator("img, script").count() == 0
    assert held_console.evaluate("window.pwned") is None


def test_a_character_that_is_not_seen_is_shown_by_its_code_point(
    held_console: Page, held_server: str
) -> None:
    """An action's arguments are kept as the agent sent them. `Take` + a right-to-left override
    + `01` would be drawn "Take 10" and sent as "01": the card shows the override by its code
    point instead, in the argument, in a key and inside a list, so the reviewer reads what
    would be sent. In the record too, under the action and as the note of its event."""
    decided = decide(held_server, WRITER, UNSEEN)
    open_agent(held_console, WRITER, "review")
    held = card(held_console, decided["id"])
    said = action_of(held, "Send reply").locator("dl.facts dd")
    expect(said).to_have_text("Take U+202E01U+202C mgU+200B now")
    marks = said.locator("code.unseen")
    assert marks.all_inner_texts() == ["U+202E", "U+202C", "U+200B"]
    expect(marks.first).to_have_attribute("title", "A character that is not shown: U+202E")
    assert "\u202e" not in said.evaluate("(el) => el.textContent")  # none is left to do its work
    read = action_of(held, "Look up medications").locator("dl.facts")
    expect(read.locator("dt").first).to_have_text("naU+200Bme")
    expect(read.locator("dd").first).to_have_text('["U+2066xU+2069"]')
    # Markup before and after such a character: every part of the text is still characters.
    both = read.locator("dd").last
    expect(both).to_have_text("<i>a</i>U+202E<b>b</b>")
    assert both.locator("i, b").count() == 0 and both.locator("code.unseen").count() == 1
    shown_record = drawer(held_console, decided["id"])
    again = action_of(shown_record, "Send reply").locator("dl.facts dd")
    expect(again).to_have_text("Take U+202E01U+202C mgU+200B now")
    note = shown_record.locator(".timeline .event-note", has_text="Arguments:").last
    expect(note).to_contain_text("Take U+202E01U+202C mgU+200B now")
    assert note.locator("code.unseen").count() == 3


def test_a_long_text_scrolls_inside_its_box_and_the_keyboard_reaches_it(
    held_console: Page, held_server: str
) -> None:
    """A text as long as one may be is all there, in a box that scrolls: the card stays short
    and the page is not pushed sideways, on a phone too. The box is in the tab order, with a
    name and a focus ring, so the keyboard scrolls it."""
    decided = decide(held_server, WRITER, LONG)
    held_console.set_viewport_size(PHONE)
    open_agent(held_console, WRITER, "review")
    held = card(held_console, decided["id"])
    body = text_of(held, "reply_draft").locator(".text-body")
    assert body.inner_text() == LONG_TEXT and len(LONG_TEXT) > 7_500
    box = body.evaluate(
        "(el) => ({ all: el.scrollHeight, seen: el.clientHeight,"
        " scrolls: getComputedStyle(el).overflowY, wide: el.scrollWidth > el.clientWidth })"
    )
    assert box["all"] > box["seen"] and box["scrolls"] == "auto" and not box["wide"]
    assert held_console.evaluate("document.documentElement.scrollWidth") <= PHONE["width"]
    # From the keyboard: the link before it, then Tab.
    held.locator("a.action-link").focus()
    held_console.keyboard.press("Tab")
    expect(body).to_be_focused()
    expect(body).to_have_accessible_name("Reply draft: the text")
    ring = body.evaluate("(el) => getComputedStyle(el).outlineStyle")
    assert ring == "solid"
    held_console.keyboard.press("End")
    expect(body).not_to_have_js_property("scrollTop", 0)


# -- the verdicts: each text takes one, and the labels wait ---------------------------------------


def test_the_label_buttons_wait_for_every_verdict_and_say_why(
    held_console: Page, held_server: str
) -> None:
    """Neither verdict is chosen at first. Until every text has one, the labels that resolve
    the review are unavailable: each says so to a screen reader with the reason, which is on
    the screen in words; asked for anyway (a click, or C), nothing is sent, the reason is said
    at once and focus goes to the first text that waits. Each verdict given is announced."""
    decided = decide(held_server, WRITER, LETTER)
    sent = reviews(held_console, decided["id"])
    open_agent(held_console, WRITER, "review")
    held = card(held_console, decided["id"])
    expect(held.locator('.texts-list input[type="radio"]')).to_have_count(4)
    expect(held.locator(".texts-list input:checked")).to_have_count(0)
    labels = [confirm(held, "Refill"), held.get_by_role("button", name="Appointment", exact=True)]
    gate = held.locator(".review-gate")
    expect(gate).to_have_text(WAITS.format("2 texts need"))
    for label in labels:
        expect(label).to_have_attribute("aria-disabled", "true")
        expect(label).to_have_attribute("aria-describedby", gate.get_attribute("id") or "-")
    first = text_of(held, "chart_note").get_by_role("radio", name=RELEASE)
    labels[0].focus()  # still in the tab order
    expect(labels[0]).to_be_focused()
    held_console.keyboard.press("Enter")
    expect(held_console.locator("#toast")).to_have_text(WAITS.format("2 texts need"))
    expect(held_console.locator("#announce-alert")).to_have_text(WAITS.format("2 texts need"))
    expect(first).to_be_focused()
    labels[1].click(force=True)  # with the pointer: an unavailable button is still pressed
    expect(first).to_be_focused()
    held.focus()
    held_console.keyboard.press("c")  # the queue's key for "confirm the recommendation"
    expect(first).to_be_focused()
    expect(held.locator(".undo-bar")).to_have_count(0)
    verdict(held, "chart_note", RELEASE)
    expect(gate).to_have_text(WAITS.format("1 text needs"))
    expect(held_console.locator("#announce-status")).to_have_text(WAITS.format("1 text needs"))
    expect(labels[0]).to_have_attribute("aria-disabled", "true")
    verdict(held, "reply_draft", RELEASE)
    expect(gate).to_be_hidden()
    expect(held_console.locator("#announce-status")).to_have_text(READY)
    for label in labels:
        expect(label).not_to_have_attribute("aria-disabled", "true")
        expect(label).not_to_have_attribute("aria-describedby", re.compile("."))
        expect(label).to_be_enabled()
    assert sent == [] and record(held_server, decided["id"])["review_status"] == "pending"


def test_a_review_is_given_from_the_keyboard_alone(held_console: Page, held_server: str) -> None:
    """Tab goes through the card in the order it is read: each text, then its two verdicts,
    then each held action's tick, the note and the labels. Space and the arrows choose, Enter
    confirms, and Undo takes focus and gives it back."""
    decided = decide(held_server, WRITER, LETTER)
    sent = reviews(held_console, decided["id"])
    open_agent(held_console, WRITER, "review")
    held = card(held_console, decided["id"])
    keys = held_console.keyboard
    note, reply = text_of(held, "chart_note"), text_of(held, "reply_draft")
    held.locator("a.action-link").focus()
    keys.press("Tab")
    expect(note.locator(".text-body")).to_be_focused()
    keys.press("Tab")
    expect(note.get_by_role("radio", name=RELEASE)).to_be_focused()
    keys.press("Space")
    expect(note.get_by_role("radio", name=RELEASE)).to_be_checked()
    keys.press("Tab")
    expect(reply.locator(".text-body")).to_be_focused()
    keys.press("Tab")
    expect(reply.get_by_role("radio", name=RELEASE)).to_be_focused()
    keys.press("ArrowRight")  # to the other verdict, which it chooses
    expect(reply.get_by_role("radio", name=WITHHOLD)).to_be_checked()
    keys.press("Tab")
    queue = release_box(held, "Assign queue")
    expect(queue).to_be_focused()
    expect(queue).not_to_be_checked()  # a text is withheld
    keys.press("Space")
    expect(queue).to_be_checked()
    keys.press("Tab")
    expect(release_box(held, "Send reply")).to_be_focused()
    keys.press("Tab")
    expect(held.locator(".review-note summary")).to_be_focused()
    keys.press("Tab")
    expect(confirm(held, "Refill")).to_be_focused()
    assert held_console.evaluate("document.activeElement.matches(':focus-visible')")
    keys.press("Enter")
    undo = held.get_by_role("button", name="Undo")
    expect(undo).to_be_focused()
    expect(held.locator(".undo-bar")).to_contain_text(
        "Confirm Refill and release 1 text and 1 action, withhold 1 text in 5 s"
    )
    keys.press("Enter")
    expect(held.locator(".undo-bar")).to_have_count(0)
    expect(confirm(held, "Refill")).to_be_focused()
    assert sent == []


def test_a_decision_that_holds_nothing_confirms_in_one_tap_as_before(
    held_console: Page, held_server: str
) -> None:
    """A card with no text and no action has no verdict to give and nothing to wait for."""
    decided = decide(held_server, WRITER, PLAIN)
    sent = reviews(held_console, decided["id"])
    open_agent(held_console, WRITER, "review")
    held = card(held_console, decided["id"])
    expect(held.get_by_role("heading", name="What the agent wrote")).to_have_count(0)
    expect(held.locator(".review-gate")).to_have_count(0)
    expect(confirm(held, "Other")).not_to_have_attribute("aria-disabled", "true")
    confirm(held, "Other").click()
    expect(held).to_have_count(0)  # no Undo bar: sent at once
    assert sent == [{"final_label": "other", "reviewer": "sandbox-nurse"}]
    expect(held_console.locator(".resolved-item")).to_have_count(0)  # nothing of it to list


# -- resolving: the verdicts and the ticks are sent, and the queue says what was done -------------


def test_a_review_with_verdicts_resolves_and_the_queue_lists_what_it_did(
    held_console: Page, held_server: str
) -> None:
    """Both texts released, the ticks left as they were: the review sends each verdict and
    names no action, so the action of enumerated arguments is released and the free-text one
    is not. A release is final, so the review waits for Undo, saying what it does. Afterwards
    the queue lists what the review did, each text and each held action by name, as the server
    answered, with the way to the decision's record."""
    decided = decide(held_server, WRITER, LETTER)
    sent = reviews(held_console, decided["id"])
    held_console.clock.install()
    open_agent(held_console, WRITER, "review")
    held = card(held_console, decided["id"])
    verdict(held, "chart_note", RELEASE)
    verdict(held, "reply_draft", RELEASE)
    confirm(held, "Refill").click()
    expect(held.locator(".undo-bar")).to_contain_text(
        "Confirm Refill and release 2 texts and 1 action in 5 s"
    )
    held_console.clock.run_for(5000)
    expect(held).to_have_count(0)
    assert sent == [
        {
            "final_label": "refill",
            "reviewer": "sandbox-nurse",
            "texts": {"chart_note": "release", "reply_draft": "release"},
        }
    ]
    expect(held_console.locator("#toast")).to_have_text(
        "Confirmed: Refill · 2 texts released · 1 action released, 1 action denied"
    )
    # After the review, in the queue: what it did, each text and each held action by name.
    done = held_console.locator(f'.resolved-item[data-id="{decided["id"]}"]')
    expect(held_console.get_by_role("heading", name="What your reviews did")).to_be_visible()
    expect(done).to_contain_text("Confirmed: Refill")
    for name in ("chart_note", "reply_draft"):
        expect(text_of(done, name).locator(".badge")).to_have_text("Released")
    expect(action_of(done, "Assign queue").locator(".badge")).to_have_text("Released")
    expect(action_of(done, "Send reply").locator(".badge")).to_have_text("Denied")
    expect(action_of(done, "Send reply")).to_contain_text(NOT_NAMED)
    for mark in (NOTE, REPLY, ARGUMENT):  # what it did, not what was read
        expect(done).not_to_contain_text(mark)
    found = record(held_server, decided["id"])
    assert (found["review_status"], found["human_override"]) == ("resolved", False)
    assert texts(found) == [("chart_note", "released"), ("reply_draft", "released")]
    assert acted(found) == [
        READ,
        ("assign_queue", "released", "confirmed"),
        ("send_reply", "denied", "not_named"),
    ]
    # The record, from the link the queue gives: each text with where it stands.
    done.get_by_role("link", name=decided["id"][:8]).click()
    shown = held_console.locator(".drawer")
    for name in ("chart_note", "reply_draft"):
        expect(text_of(shown, name).locator(".badge")).to_have_text("Released")


def test_an_action_asked_for_after_the_decision_is_released_by_its_tick_and_never_unseen(
    held_console: Page, held_server: str
) -> None:
    """A caller asks for one more action once the decision is made. Where the card shows it,
    the card says so and its tick names it in the review: ticked, it is released. One asked
    for after the card was read is not on the screen: confirming releases nothing the reviewer
    was not shown, and the list under the queue says the review did not name it."""
    shown, unseen = decide(held_server, WRITER, PLAIN), decide(held_server, WRITER, PLAIN)
    first = ask(held_server, WRITER, shown["id"], "assign_queue", queue="front_desk")
    assert (first["disposition"], first["reason"]) == ("held", "awaiting_review")
    sent, blind = reviews(held_console, shown["id"]), reviews(held_console, unseen["id"])
    held_console.clock.install()
    open_agent(held_console, WRITER, "review")
    held = card(held_console, shown["id"])
    expect(release_box(held, "Assign queue")).to_be_checked()  # enumerated: ticked as any other
    expect(action_of(held, "Assign queue")).to_contain_text(ASKED)
    confirm(held, "Other").click()
    expect(held.locator(".undo-bar")).to_contain_text("Confirm Other and release 1 action in 5 s")
    elapse(held_console, shown["id"])
    assert [(s.get("release_actions"), s.get("deny_actions")) for s in sent] == [
        ([first["id"]], None)
    ]
    assert acted(record(held_server, shown["id"])) == [("assign_queue", "released", "confirmed")]
    # The other decision's card is on the screen before anything is asked for it.
    show(held_console, "review")
    other = card(held_console, unseen["id"])
    expect(other.get_by_role("checkbox")).to_have_count(0)
    ask(held_server, WRITER, unseen["id"], "assign_queue", queue="front_desk")
    confirm(other, "Other").click()  # one tap: the card holds nothing to release
    done = held_console.locator(f'.resolved-item[data-id="{unseen["id"]}"]')
    expect(action_of(done, "Assign queue")).to_contain_text(NOT_NAMED)
    assert blind == [{"final_label": "other", "reviewer": "sandbox-nurse"}]
    assert acted(record(held_server, unseen["id"])) == [("assign_queue", "denied", "not_named")]


def test_a_free_text_action_is_released_only_when_ticked(
    held_console: Page, held_server: str
) -> None:
    """A held action whose arguments can hold free text starts unticked, says why beside its
    tick and that it will not be released; one of enumerated arguments starts ticked, as it
    was. Ticked, the free-text action is named in the review, and released."""
    decided = decide(held_server, WRITER, LETTER)
    ids = held_ids(record(held_server, decided["id"]))
    sent = reviews(held_console, decided["id"])
    held_console.clock.install()
    open_agent(held_console, WRITER, "review")
    held = card(held_console, decided["id"])
    queue, reply = release_box(held, "Assign queue"), release_box(held, "Send reply")
    expect(queue).to_be_checked()
    expect(reply).not_to_be_checked()
    why = action_of(held, "Send reply").locator(".release-why")
    expect(why).to_have_text(FREE_TEXT)
    expect(reply).to_have_accessible_description(f"{FREE_TEXT} {OFF}")
    expect(action_of(held, "Send reply").locator(".release-off")).to_have_text(OFF)
    expect(action_of(held, "Assign queue").locator(".release-why")).to_have_count(0)
    expect(action_of(held, "Assign queue").locator(".release-off")).to_be_hidden()
    # What it would send is on the screen to read: its arguments, as text.
    expect(action_of(held, "Send reply")).to_contain_text(f"{ARGUMENT} Received.")
    # A read released with the decision is not the reviewer's to release.
    expect(action_of(held, "Look up medications").get_by_role("checkbox")).to_have_count(0)
    reply.check()
    expect(action_of(held, "Send reply").locator(".release-off")).to_be_hidden()
    expect(reply).to_have_accessible_description(FREE_TEXT)
    verdict(held, "chart_note", RELEASE)
    verdict(held, "reply_draft", RELEASE)
    confirm(held, "Refill").click()
    expect(held.locator(".undo-bar")).to_contain_text(
        "Confirm Refill and release 2 texts and 2 actions in 5 s"
    )
    elapse(held_console, decided["id"])
    assert [(s.get("release_actions"), s.get("deny_actions")) for s in sent] == [
        ([ids["send_reply"]], None)
    ]
    assert acted(record(held_server, decided["id"])) == [
        READ,
        ("assign_queue", "released", "confirmed"),
        ("send_reply", "released", "confirmed"),
    ]


def test_withholding_a_text_unticks_every_action_and_only_a_named_one_is_released(
    held_console: Page, held_server: str
) -> None:
    """The demo agent's acknowledgement, with its two held actions of enumerated arguments:
    both start ticked. Withholding the text unticks both and says why, each then showing that
    it will not be released; the one the reviewer ticks again is named in the review and
    released, the other is not released, and the text is withheld."""
    decided = decide(held_server, DEMO, PARKING)
    ids = held_ids(record(held_server, decided["id"]))
    sent = reviews(held_console, decided["id"])
    held_console.clock.install()
    open_agent(held_console, DEMO, "review")
    held = card(held_console, decided["id"])
    queue = release_box(held, "Assign queue")
    send = release_box(held, "Send acknowledgement")
    note = held.locator(".withheld-note")
    expect(queue).to_be_checked()
    expect(send).to_be_checked()
    expect(note).to_be_hidden()
    expect(held.locator(".release-why")).to_have_count(0)  # neither holds free text
    verdict(held, "acknowledgement", WITHHOLD)
    expect(queue).not_to_be_checked()
    expect(send).not_to_be_checked()
    expect(note).to_have_text(WITHHELD)
    expect(held_console.locator("#announce-status")).to_have_text(f"{READY} {WITHHELD}")
    for name in ("Assign queue", "Send acknowledgement"):
        expect(action_of(held, name).locator(".release-off")).to_have_text(OFF)
    expect(send).to_have_accessible_description(f"{WITHHELD} {OFF}")
    queue.check()
    expect(action_of(held, "Assign queue").locator(".release-off")).to_be_hidden()
    confirm(held, "Other").click()
    expect(held.locator(".undo-bar")).to_contain_text(
        "Confirm Other and release 1 action, withhold 1 text in 5 s"
    )
    elapse(held_console, decided["id"])
    assert sent == [
        {
            "final_label": "other",
            "reviewer": "sandbox-nurse",
            "texts": {"acknowledgement": "withhold"},
            "release_actions": [ids["assign_queue"]],
        }
    ]
    found = record(held_server, decided["id"])
    assert texts(found) == [("acknowledgement", "withheld")]
    assert acted(found) == [
        ("assign_queue", "released", "confirmed"),
        ("send_acknowledgement", "denied", "not_named"),
    ]


def test_each_text_withheld_unticks_every_action_and_nothing_is_ticked_again_for_the_reviewer(
    held_console: Page, held_server: str
) -> None:
    """Every tick comes off each time a text is withheld, the reviewer's own too: a tick made
    while another text could still be released does not outlive that text's withholding. A
    verdict changed back to "Release as written" ticks nothing: what is unticked stays so, on
    the screen and in the review. The action the label alone would release is then refused by
    name (`deny_actions`), the free-text one is left out, and neither is in both."""
    decided = decide(held_server, WRITER, LETTER)
    ids = held_ids(record(held_server, decided["id"]))
    sent = reviews(held_console, decided["id"])
    held_console.clock.install()
    open_agent(held_console, WRITER, "review")
    held = card(held_console, decided["id"])
    queue, reply = release_box(held, "Assign queue"), release_box(held, "Send reply")
    reply.check()  # ticked by the reviewer, before a text is withheld
    verdict(held, "chart_note", WITHHOLD)
    expect(queue).not_to_be_checked()
    expect(reply).not_to_be_checked()  # that tick too: the action may send the withheld text
    queue.check()  # ticked again, while the reply could still be released
    verdict(held, "reply_draft", WITHHOLD)
    expect(queue).not_to_be_checked()  # and off again: the other text is withheld now
    expect(held_console.locator("#announce-status")).to_have_text(f"{READY} {WITHHELD}")
    expect(held.locator(".withheld-note")).to_be_visible()
    verdict(held, "chart_note", RELEASE)
    expect(held.locator(".withheld-note")).to_be_visible()  # the reply is still withheld
    verdict(held, "reply_draft", RELEASE)
    expect(held.locator(".withheld-note")).to_be_hidden()
    expect(queue).not_to_be_checked()
    expect(reply).not_to_be_checked()
    expect(action_of(held, "Assign queue").locator(".release-off")).to_have_text(OFF)
    confirm(held, "Refill").click()
    expect(held.locator(".undo-bar")).to_contain_text("Confirm Refill and release 2 texts in 5 s")
    elapse(held_console, decided["id"])
    assert [(s.get("release_actions"), s.get("deny_actions")) for s in sent] == [
        (None, [ids["assign_queue"]])
    ]
    assert acted(record(held_server, decided["id"])) == [
        READ,
        ("assign_queue", "denied", "reviewer_denied"),
        ("send_reply", "denied", "not_named"),
    ]


def test_another_label_takes_each_verdict_and_releases_no_action(
    held_console: Page, held_server: str
) -> None:
    """An override needs the verdicts too, says what becomes of the texts while it waits for
    Undo, and names no action: each is denied, whatever is ticked."""
    decided = decide(held_server, WRITER, LETTER)
    sent = reviews(held_console, decided["id"])
    held_console.clock.install()
    open_agent(held_console, WRITER, "review")
    held = card(held_console, decided["id"])
    release_box(held, "Send reply").check()
    verdict(held, "chart_note", WITHHOLD)
    verdict(held, "reply_draft", RELEASE)
    release_box(held, "Send reply").check()  # ticked again after the withholding
    held.get_by_role("button", name="Appointment", exact=True).click()
    expect(held.locator(".undo-bar")).to_contain_text(
        "Override to Appointment and release 1 text, withhold 1 text in 5 s"
    )
    elapse(held_console, decided["id"])
    assert sent == [
        {
            "final_label": "appointment",
            "reviewer": "sandbox-nurse",
            "texts": {"chart_note": "withhold", "reply_draft": "release"},
        }
    ]
    found = record(held_server, decided["id"])
    assert found["human_override"] is True
    assert texts(found) == [("chart_note", "withheld"), ("reply_draft", "released")]
    assert acted(found) == [
        READ,
        ("assign_queue", "denied", "overridden"),
        ("send_reply", "denied", "overridden"),
    ]


def test_an_abstention_with_a_text_takes_its_verdict_with_the_label(
    held_console: Page, held_server: str
) -> None:
    """No recommendation to confirm: every label waits for the text's verdict, and the label
    the reviewer chooses is sent with it."""
    decided = decide(held_server, WRITER, VAGUE)
    assert decided["outcome"] == "ABSTAIN"
    sent = reviews(held_console, decided["id"])
    held_console.clock.install()
    open_agent(held_console, WRITER, "review")
    held = card(held_console, decided["id"])
    expect(held.get_by_role("heading", name="No recommendation")).to_be_visible()
    expect(text_of(held, "reply_draft").locator(".text-body")).to_contain_text(REPLY)
    other = held.get_by_role("button", name="Other", exact=True)
    expect(other).to_have_attribute("aria-disabled", "true")
    expect(held.locator(".review-gate")).to_have_text(WAITS.format("1 text needs"))
    verdict(held, "reply_draft", RELEASE)
    other.click()
    expect(held.locator(".undo-bar")).to_contain_text("Label Other and release 1 text in 5 s")
    elapse(held_console, decided["id"])
    assert sent == [
        {"final_label": "other", "reviewer": "sandbox-nurse", "texts": {"reply_draft": "release"}}
    ]
    found = record(held_server, decided["id"])
    assert (found["final_label"], texts(found)) == ("other", [("reply_draft", "released")])


def test_an_agent_that_only_writes_is_reviewed_with_its_verdict_and_undo_still_cancels(
    held_console: Page, held_server: str
) -> None:
    """The written fixture proposes no action: its card has the text and no list of actions.
    Releasing a text is final, so confirming waits for Undo; undone, nothing was sent and the
    verdict is still there to change."""
    decided = decide(held_server, WRITTEN, "Could you refill my lisinopril?")
    sent = reviews(held_console, decided["id"])
    held_console.clock.install()
    open_agent(held_console, WRITTEN, "review")
    held = card(held_console, decided["id"])
    expect(held.get_by_role("heading", name="Proposed actions")).to_have_count(0)
    expect(text_of(held, "reply_draft").locator(".text-body")).to_contain_text(
        "The pharmacy team will review your refill request"
    )
    verdict(held, "reply_draft", RELEASE)
    confirm(held, "Refill").click()
    bar = held.locator(".undo-bar")
    expect(bar).to_contain_text("Confirm Refill and release 1 text in 5 s")
    # What the bar says is what is sent: the verdict cannot change under it.
    expect(text_of(held, "reply_draft").get_by_role("radio", name=WITHHOLD)).to_be_disabled()
    bar.get_by_role("button", name="Undo").click()
    expect(bar).to_have_count(0)
    assert sent == [] and record(held_server, decided["id"])["review_status"] == "pending"
    verdict(held, "reply_draft", WITHHOLD)  # changed their mind
    confirm(held, "Refill").click()
    expect(bar).to_contain_text("Confirm Refill and withhold 1 text in 5 s")
    elapse(held_console, decided["id"])
    assert [s["texts"] for s in sent] == [{"reply_draft": "withhold"}]
    assert texts(record(held_server, decided["id"])) == [("reply_draft", "withheld")]


def test_leaving_or_closing_the_page_sends_no_review_that_lets_anything_out(
    held_console: Page, held_server: str
) -> None:
    """A review that gives a verdict or releases an action cannot be taken back, so only its
    five seconds passing sends it. Leaving the queue while it waits drops it and says so: the
    decision still waits, with no verdict given. A page that goes away (closed, or gone to
    another address) sends nothing either: whoever chose by mistake and left released nothing."""
    decided = decide(held_server, WRITER, LETTER)
    sent = reviews(held_console, decided["id"])
    held_console.clock.install()
    open_agent(held_console, WRITER, "review")
    held = card(held_console, decided["id"])
    verdict(held, "chart_note", WITHHOLD)
    verdict(held, "reply_draft", RELEASE)
    release_box(held, "Assign queue").check()
    confirm(held, "Refill").click()
    expect(held.locator(".undo-bar")).to_contain_text(
        "Confirm Refill and release 1 text and 1 action, withhold 1 text in 5 s"
    )
    # Leaving by the section list: dropped, and said.
    audit = held_console.locator('.nav a[data-view="audit"]')
    audit.click()
    expect(audit).to_have_class(re.compile("active"))
    said = "1 review not sent: you left before the 5 seconds were up. Its decision still waits"
    expect(held_console.locator("#toast")).to_have_text(f"{said} in the queue.")
    expect(held_console.locator("#toast")).to_have_class(re.compile("error"))
    expect(held_console.locator("#announce-alert")).to_have_text(f"{said} in the queue.")
    held_console.clock.run_for(10_000)  # long past its five seconds
    assert sent == []
    waiting = record(held_server, decided["id"])
    assert waiting["review_status"] == "pending"
    assert texts(waiting) == [("chart_note", "held"), ("reply_draft", "held")]
    # Back in the queue its card is as any other: no verdict is given, nothing was sent.
    show(held_console, "review")
    held = card(held_console, decided["id"])
    expect(held.get_by_role("radio", checked=True)).to_have_count(0)
    expect(held.locator(".undo-bar")).to_have_count(0)
    # The page itself goes away while a review waits, as when its tab is closed.
    verdict(held, "chart_note", RELEASE)
    verdict(held, "reply_draft", RELEASE)
    confirm(held, "Refill").click()
    expect(held.locator(".undo-bar")).to_contain_text("release 2 texts and 1 action in 5 s")
    held_console.reload()  # the page goes, and comes back new
    expect(card(held_console, decided["id"])).to_be_visible()
    assert sent == []
    waiting = record(held_server, decided["id"])
    assert waiting["review_status"] == "pending"
    assert acted(waiting) == [
        READ,
        ("assign_queue", "held", "awaiting_review"),
        ("send_reply", "held", "awaiting_review"),
    ]


def test_a_page_that_is_hidden_sends_no_review_that_lets_anything_out(
    held_console: Page, held_server: str
) -> None:
    """Another tab, a locked screen: the reviewer is no longer looking, and has no Undo to
    press. A review that gives a verdict or releases an action is dropped when its page is
    hidden inside its five seconds, and the page says so for when they come back."""
    decided = decide(held_server, WRITER, ALONE)
    sent = reviews(held_console, decided["id"])
    held_console.clock.install()
    open_agent(held_console, WRITER, "review")
    held = card(held_console, decided["id"])
    verdict(held, "reply_draft", RELEASE)
    confirm(held, "Refill").click()
    expect(held.locator(".undo-bar")).to_contain_text("Confirm Refill and release 1 text in 5 s")
    held_console.evaluate(
        """() => {
          Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
          document.dispatchEvent(new Event("visibilitychange"));
        }"""
    )
    expect(held.locator(".undo-bar")).to_have_count(0)
    expect(held_console.locator("#toast")).to_have_text(
        "1 review not sent: this page was hidden before the 5 seconds were up. "
        "Its decision still waits in the queue."
    )
    held_console.clock.run_for(10_000)
    assert sent == []
    assert record(held_server, decided["id"])["review_status"] == "pending"
    # The card is as it was before the label was chosen: the verdict is still there to send.
    expect(text_of(held, "reply_draft").get_by_role("radio", name=RELEASE)).to_be_checked()
    expect(confirm(held, "Refill")).to_be_enabled()


def test_leaving_still_sends_a_held_label_that_lets_nothing_out(
    held_console: Page, held_server: str
) -> None:
    """Another label than the recommendation, on a decision that holds no text: it denies
    every held action and releases nothing, and leaving the queue sends it as it always did."""
    decided = decide(held_server, WRITER, PLAIN)
    ask(held_server, WRITER, decided["id"], "assign_queue", queue="front_desk")
    sent = reviews(held_console, decided["id"])
    open_agent(held_console, WRITER, "review")
    held = card(held_console, decided["id"])
    held.get_by_role("button", name="Refill", exact=True).click()
    expect(held.locator(".undo-bar")).to_contain_text("Override to Refill in 5 s")
    audit = held_console.locator('.nav a[data-view="audit"]')
    with held_console.expect_response(
        lambda r: r.request.method == "POST" and r.url.endswith(f"/api/review/{decided['id']}")
    ):
        audit.click()
    assert sent == [{"final_label": "refill", "reviewer": "sandbox-nurse"}]
    assert acted(record(held_server, decided["id"])) == [("assign_queue", "denied", "overridden")]


# -- a text that is not on the screen: why, and the verdict is still owed -------------------------


def test_a_text_that_is_not_kept_says_why_and_still_takes_its_verdict(
    held_console: Page, held_server: str
) -> None:
    """An agent that keeps only its input's hash keeps no text: the card says so in words, and
    where the reviewer reads the draft instead. Each text still takes its verdict, the labels
    still wait for it, and the review resolves."""
    decided = decide(held_server, UNKEPT, LETTER)
    assert decided["outputs"]["reply_draft"]  # the caller has the draft: Mendari kept none
    held_console.clock.install()
    open_agent(held_console, UNKEPT, "review")
    held = card(held_console, decided["id"])
    for name in ("chart_note", "reply_draft"):
        text = text_of(held, name)
        expect(text.locator(".text-body")).to_have_count(0)
        expect(text.locator(".text-missing")).to_have_text(
            "Not kept here: this agent keeps only a hash of its input (audit.store_input). "
            + ELSEWHERE
        )
    expect(held).not_to_contain_text(REPLY)
    expect(confirm(held, "Refill")).to_have_attribute("aria-disabled", "true")
    verdict(held, "chart_note", RELEASE)
    verdict(held, "reply_draft", WITHHOLD)
    expect(confirm(held, "Refill")).not_to_have_attribute("aria-disabled", "true")
    confirm(held, "Refill").click()
    elapse(held_console, decided["id"])
    found = record(held_server, decided["id"])
    assert texts(found) == [("chart_note", "released"), ("reply_draft", "withheld")]


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
def test_a_text_that_cannot_be_shown_says_why_in_words(
    held_console: Page, held_server: str, status: str, phi_mode: str, why: str
) -> None:
    """A text sealed for this reader, sealed under a key the deployment no longer holds, erased
    or missing, and one PHI mode `hash` never kept. The server here makes none of these: the
    queue's answer and the deployment's PHI mode are changed on their way to the page
    (Playwright routing). The words say which, the verdict is still asked for, and the real
    server takes it."""
    decided = decide(held_server, WRITER, ALONE)

    def unshown(route: Route) -> None:
        queue = route.fetch().json()
        for item in queue:
            for text in item.get("outputs", []):
                text.pop("text", None)
                text["text_status"] = status
        route.fulfill(json=queue)

    def mode(route: Route) -> None:
        route.fulfill(json={**route.fetch().json(), "phi_mode": phi_mode})

    held_console.route("**/api/review?*", unshown)
    held_console.route("**/api/health", mode)
    held_console.clock.install()
    open_agent(held_console, WRITER, "review")
    held = card(held_console, decided["id"])
    text = text_of(held, "reply_draft")
    expect(text.locator(".text-missing")).to_have_text(f"{why} {ELSEWHERE}")
    expect(text.locator(".text-body")).to_have_count(0)
    expect(held).not_to_contain_text(ALONE_TEXT)
    verdict(held, "reply_draft", RELEASE)
    confirm(held, "Refill").click()
    elapse(held_console, decided["id"])
    assert texts(record(held_server, decided["id"])) == [("reply_draft", "released")]


def test_a_text_named_as_a_built_in_still_waits_for_its_verdict(
    held_console: Page, held_server: str
) -> None:
    """An output may be named `constructor`: a manifest's key is any lowercase word. Its card
    waits for the verdict as for any text, and sends it. The queue's answer and the review are
    renamed on their way (Playwright routing): no agent of this server has such an output."""
    decided = decide(held_server, WRITER, ALONE)

    def renamed(route: Route) -> None:
        queue = route.fetch().json()
        for item in queue:
            for text in item.get("outputs", []):
                text["name"] = "constructor"
        route.fulfill(json=queue)

    def sent_back(route: Route) -> None:
        body = route.request.post_data_json
        assert isinstance(body, dict)
        asked.append(body)
        route.continue_(post_data={**body, "texts": {"reply_draft": body["texts"]["constructor"]}})

    asked: list[dict[str, Any]] = []
    held_console.route("**/api/review?*", renamed)
    held_console.route(f"**/api/review/{decided['id']}", sent_back)
    held_console.clock.install()
    open_agent(held_console, WRITER, "review")
    held = card(held_console, decided["id"])
    expect(held.locator(".review-gate")).to_have_text(WAITS.format("1 text needs"))
    expect(confirm(held, "Refill")).to_have_attribute("aria-disabled", "true")
    confirm(held, "Refill").click(force=True)
    expect(held.locator(".undo-bar")).to_have_count(0)  # nothing is sent without the verdict
    verdict(held, "constructor", WITHHOLD)
    expect(confirm(held, "Refill")).not_to_have_attribute("aria-disabled", "true")
    confirm(held, "Refill").click()
    elapse(held_console, decided["id"])
    assert [body["texts"] for body in asked] == [{"constructor": "withhold"}]
    assert texts(record(held_server, decided["id"])) == [("reply_draft", "withheld")]


def test_a_reader_who_may_not_review_reads_the_text_and_gives_no_verdict(
    held_console: Page, held_server: str
) -> None:
    """A viewer's queue (their sign-in answered by Playwright routing: this server has none)
    shows the held text and its actions, and nothing to choose: no verdict, no tick, no label."""
    decided = decide(held_server, WRITER, LETTER)
    viewer = {"kind": "user", "name": "Viewer", "roles": ["viewer"], "permissions": ["read"]}
    me = {"mode": "sign-in", "signed_in": True, "principal": {**viewer, "org": "default"}}
    held_console.route("**/api/auth/me", lambda route: route.fulfill(json=me))
    open_agent(held_console, WRITER, "review")
    held = card(held_console, decided["id"])
    expect(text_of(held, "reply_draft").locator(".text-body")).to_contain_text(REPLY)
    expect(text_of(held, "reply_draft").locator(".badge")).to_have_text("Held")
    expect(held.get_by_role("radio")).to_have_count(0)
    expect(held.get_by_role("checkbox")).to_have_count(0)
    expect(held.locator(".review-gate")).to_have_count(0)
    expect(held).to_contain_text("Resolving is for reviewers; your role can read the queue.")


# -- a refused review ----------------------------------------------------------------------------


def refused_review(
    page: Page, held_server: str, decided: dict[str, Any], ids: dict[str, str]
) -> None:
    """The three refusals of a review of `decided`, then the review, in `page`."""
    page.clock.install()
    open_agent(page, WRITER, "review")
    held = card(page, decided["id"])
    verdict(held, "chart_note", RELEASE)
    verdict(held, "reply_draft", WITHHOLD)
    release_box(held, "Send reply").check()
    url = f"**/api/review/{decided['id']}"
    error = held.locator(".review-error")

    def as_left() -> None:
        expect(held).to_be_visible()
        expect(held.locator(".undo-bar")).to_have_count(0)
        expect(page.locator("#toast")).not_to_contain_text("Confirmed")
        expect(page.locator(".resolved-item")).to_have_count(0)
        assert record(held_server, decided["id"])["review_status"] == "pending"
        expect(text_of(held, "chart_note").get_by_role("radio", name=RELEASE)).to_be_checked()
        expect(text_of(held, "reply_draft").get_by_role("radio", name=WITHHOLD)).to_be_checked()
        expect(release_box(held, "Send reply")).to_be_checked()
        expect(release_box(held, "Assign queue")).not_to_be_checked()
        expect(held.locator(".withheld-note")).to_be_visible()
        expect(confirm(held, "Refill")).to_be_enabled()
        expect(release_box(held, "Send reply")).to_be_enabled()

    def send() -> None:
        confirm(held, "Refill").click()
        expect(held.locator(".undo-bar")).to_be_visible()
        page.clock.run_for(5000)

    def without_verdicts(route: Route) -> None:
        body = json.loads(route.request.post_data or "{}")
        del body["texts"]
        route.continue_(post_data=json.dumps(body))

    page.route(url, without_verdicts)
    send()
    expect(error).to_have_attribute("role", "alert")
    expect(error).to_contain_text("Not recorded:")
    expect(error).to_contain_text("holds written output 'chart_note', 'reply_draft'")
    as_left()
    page.unroute(url)
    refusal = "texts gives each held text a verdict by its output's name: release or withhold"
    detail = [{"type": "value_error", "loc": ["body", "texts"], "msg": f"Value error, {refusal}"}]
    page.route(url, lambda route: route.fulfill(status=422, json={"detail": detail}))
    send()
    expect(error).to_have_text(f"Not recorded: {refusal}")
    as_left()
    page.unroute(url)
    page.route(url, lambda route: route.abort())
    send()
    expect(error).to_have_text(
        "Not recorded: the server could not be reached. Your choices are kept: try again."
    )
    as_left()
    page.unroute(url)
    sent = reviews(page, decided["id"])
    send()
    expect(held).to_have_count(0)
    expect(page.locator(f'.resolved-item[data-id="{decided["id"]}"]')).to_be_visible()
    assert sent == [
        {
            "final_label": "refill",
            "reviewer": "sandbox-nurse",
            "texts": {"chart_note": "release", "reply_draft": "withhold"},
            "release_actions": [ids["send_reply"]],
        }
    ]
    found = record(held_server, decided["id"])
    assert texts(found) == [("chart_note", "released"), ("reply_draft", "withheld")]
    assert acted(found) == [
        READ,
        ("assign_queue", "denied", "not_named"),
        ("send_reply", "released", "confirmed"),
    ]


def test_a_refused_review_says_why_shows_nothing_as_done_and_keeps_the_choices(
    browser: Browser, held_server: str
) -> None:
    """Three refusals, then the review itself. The server's own 409, which names the texts
    (the review reaches it with its verdicts taken out on the way: Playwright routing); a 422
    and a failed connection (answered and cut by routing). Each time the card says why, in
    place and at once; it is not folded away, no toast says "Confirmed", nothing is listed as
    resolved, the decision still waits, and every verdict and tick is as the reviewer left it.
    Sent again untouched, the review resolves as chosen."""
    decided = decide(held_server, WRITER, LETTER)
    ids = held_ids(record(held_server, decided["id"]))
    # Its own window: a refused request is a console error of the browser's.
    with window(browser, held_server) as page:
        refused_review(page, held_server, decided, ids)


# -- controls: named, large enough, and the page stays in its width ------------------------------


def test_the_review_of_a_held_text_has_no_unnamed_or_small_control_and_fits_a_phone(
    held_console: Page, held_server: str
) -> None:
    decide(held_server, WRITER, LETTER)
    decide(held_server, DEMO, PARKING)
    held_console.set_viewport_size(PHONE)
    for agent in (WRITER, DEMO):
        open_agent(held_console, agent, "review")
        expect(held_console.locator(".texts-list").first).to_be_visible()
        assert held_console.evaluate(UNNAMED) == [], agent
        assert held_console.evaluate(UNDER_24) == [], agent
        assert held_console.evaluate("document.documentElement.scrollWidth") <= PHONE["width"], (
            agent
        )
