# SPDX-License-Identifier: Apache-2.0
"""A real Mendari server whose decisions hold texts for a person (ADR-0027), for the browser
tests of their review (tests/browser/test_review_texts.py) and of its accessibility
(tests/browser/test_accessibility.py). Open mode under no PHI mode, so whoever reviews reads
what is kept. It serves four agents:

- `WRITER`: the copy of the actions fixture that also writes (`tests.test_held_texts.writing`:
  two texts, a `read` released with the decision, a `write` of enumerated arguments and a
  free-text `contact`), with four more canned answers: a text that holds markup, one of the
  longest a text may be, a decision with a text and nothing else, one whose actions hold
  markup in their arguments, and one whose arguments hold characters that are not seen;
- `UNKEPT`: the same agent keeping only its input's hash (`audit.store_input: hash`), so
  none of its texts is kept;
- `WRITTEN`: the written fixture, which writes a reply and proposes no action;
- `DEMO`: the bundled demo agent, whose acknowledgement rides on a decision that goes to a
  person anyway, with two held actions of enumerated arguments.

`WRITER`, `UNKEPT` and `WRITTEN` are on an automation hold: every decision of theirs waits for
a person with its recommendation, whether or not a text alone would send it to one. Beside the
server: a browser window on it, a decision and a review made through its API, and where a
decision's texts and actions are on the screen. Every message and text here is synthetic."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import yaml
from playwright.sync_api import Browser, Locator, Page

from mendari.core.spec import MAX_OUTPUT_CHARS
from mendari.server.app import create_app
from tests.browser.conftest import serving
from tests.conftest import DEMO_AGENT_DIR, ROOT, message
from tests.test_held_texts import writing

WRITER = "mendari-test/actions-mini"  # the copy that writes keeps the fixture's id
UNKEPT = "mendari-test/keeps-hashes"
WRITTEN = "mendari-test/written-mini"
DEMO = "mendari/portal-message-agent"
HELD = (WRITER, UNKEPT, WRITTEN)  # on an automation hold
WRITTEN_MINI = ROOT / "tests" / "fixtures" / "mini-agents" / "written-mini"

# What the canned agent answers each message with (the first three are `writing`'s).
LETTER = "Please write the letter about my refill."  # two texts, a held action of each kind
VAGUE = "Something vague, I am not sure."  # no recommendation, and a text
PLAIN = "Hello there."  # no text and no action
MARKUP = "Please check the markup in what you write."  # a text that holds markup
LONG = "Please write me the longest text."  # a text of the most characters one may have
ALONE = "Just the one text, please."  # one text, and no action
ODD = "Please ask to do something odd."  # actions whose arguments, and a key of them, hold markup
UNSEEN = "Please send what I cannot see."  # an argument that holds characters that are not seen
PARKING = "Where do I park on Tuesday?"  # the demo agent: the front desk, an acknowledgement

SCRIPT = "<script>window.pwned = 1</script>"
IMAGE = '<img src="x" onerror="window.pwned = 1">'
MARKUP_TEXT = f"Dear patient,\n{SCRIPT}\n{IMAGE}\n\nYour refill is with the pharmacy."
PARAGRAPH = " ".join(["The pharmacy reads each request in the order it came."] * 4)
# As long as a text may be, less than a paragraph: one paragraph to a line.
LONG_TEXT = "\n".join([PARAGRAPH] * (MAX_OUTPUT_CHARS // (len(PARAGRAPH) + 1)))
ALONE_TEXT = "Thank you. The pharmacy will reply within two business days."
# Drawn "Take 10 mg now" by a screen that lets the override work, and sent as "01".
REVERSED = "Take \u202e01\u202c mg\u200b now"
# Markup on both sides of a character that is not seen: each part is still only characters.
BOTH = "<i>a</i>\u202e<b>b</b>"
# An action with no schema takes any JSON object: a key of it is the agent's text too.
HOSTILE_KEY = '<img src="y" onerror="window.pwned = 2">'


def _answer(outputs: dict[str, str]) -> dict[str, Any]:
    return {
        "answers": {
            "request": {"value": "refill", "confidence": 0.95},
            "abusive": {"value": "no", "confidence": 0.97},
        },
        "outputs": outputs,
    }


CASES = [
    {
        "match": "cannot see",
        "response": {
            **_answer({"reply_draft": ALONE_TEXT}),
            "actions": [
                {
                    "name": "look_up_medications",
                    "arguments": {"na\u200bme": ["\u2066x\u2069"], "note": BOTH},
                },
                {"name": "send_reply", "arguments": {"message": REVERSED}},
            ],
        },
    },
    {
        "match": "something odd",
        "response": {
            **_answer({"reply_draft": ALONE_TEXT}),
            "actions": [
                {"name": "look_up_medications", "arguments": {HOSTILE_KEY: SCRIPT}},
                {"name": "send_reply", "arguments": {"message": MARKUP_TEXT}},
            ],
        },
    },
    {"match": "markup", "response": _answer({"reply_draft": MARKUP_TEXT})},
    {"match": "longest text", "response": _answer({"reply_draft": LONG_TEXT})},
    {"match": "one text", "response": _answer({"reply_draft": ALONE_TEXT})},
]


def agents(root: Path) -> Path:
    """The folder of the agents the server serves, made under `root`."""
    folder = root / "agents"
    writer = writing(folder)
    manifest = yaml.safe_load((writer / "agent.yaml").read_text(encoding="utf-8"))
    manifest["outputs"]["reply_draft"]["max_chars"] = MAX_OUTPUT_CHARS
    (writer / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    canned = writer / "fixtures" / "mock_agent.json"
    fixture = json.loads(canned.read_text(encoding="utf-8"))
    fixture["cases"][:0] = CASES
    canned.write_text(json.dumps(fixture), encoding="utf-8")
    unkept = writing(folder, store_input="hash")
    manifest = yaml.safe_load((unkept / "agent.yaml").read_text(encoding="utf-8"))
    manifest["id"] = UNKEPT
    manifest["name"] = "Keeps Hashes (test fixture)"  # told apart in the agent picker
    (unkept / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    shutil.copytree(WRITTEN_MINI, folder / "written-mini")
    shutil.copytree(DEMO_AGENT_DIR, folder / "portal-message-agent")
    return folder


@contextmanager
def running(root: Path) -> Iterator[str]:
    """The server's address while the block runs, its three fixture agents on hold."""
    app = create_app(db_path=root / "mendari.db", agents_paths=[agents(root)])
    with serving(app) as port:
        base = f"http://127.0.0.1:{port}"
        for agent in HELD:
            held = httpx.post(f"{base}/api/holds", json={"agent_id": agent, "reason": "a drill"})
            assert held.status_code == 201, held.text
        yield base


@contextmanager
def window(browser: Browser, base: str, errors: list[str] | None = None) -> Iterator[Page]:
    """A browser window on the server, closed with the block. With `errors`: what its console
    and its pages reported. A request a test still answers when the block ends (the next view
    asks for the queue's count) is dropped, not left to fail in the next test."""
    page = browser.new_context(base_url=base).new_page()
    if errors is not None:
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        yield page
    finally:
        page.unroute_all(behavior="ignoreErrors")
        page.context.close()


def decide(base: str, agent: str, body: str, **extra: Any) -> dict[str, Any]:
    """A decision of `agent` on a message, as its caller gets it back."""
    answer = httpx.post(f"{base}/api/agents/{agent}/decide", json=message(body, **extra))
    assert answer.status_code == 200, answer.text
    decided: dict[str, Any] = answer.json()
    return decided


def ask(base: str, agent: str, decision_id: str, name: str, **arguments: Any) -> dict[str, Any]:
    """Ask, as a caller does once a decision is made, whether one more action may be run for
    it: how the action stands."""
    body = {"decision_id": decision_id, "name": name, "arguments": arguments}
    answer = httpx.post(f"{base}/api/agents/{agent}/actions", json=body, timeout=30)
    assert answer.status_code == 200, answer.text
    asked: dict[str, Any] = answer.json()
    return asked


def record(base: str, decision_id: str) -> dict[str, Any]:
    """The decision as the audit trail shows it."""
    found: dict[str, Any] = httpx.get(f"{base}/api/audit/{decision_id}", timeout=30).json()
    return found


def held_ids(found: dict[str, Any]) -> dict[str, str]:
    """The id of each of a decision's actions, by its name."""
    return {action["name"]: action["id"] for action in found["actions"]}


def review(base: str, decision_id: str, label: str, **more: Any) -> dict[str, Any]:
    """Resolve a decision's review through the API, as a caller does: the answer."""
    body = {"final_label": label, "reviewer": "rn-1", **more}
    answer = httpx.post(f"{base}/api/review/{decision_id}", json=body, timeout=30)
    assert answer.status_code == 200, answer.text
    resolved: dict[str, Any] = answer.json()
    return resolved


def elapse(page: Page, decision_id: str) -> None:
    """Let the five seconds of a review that waits for Undo pass, and wait for the server's
    answer to it. The page's clock is the test's (`page.clock.install()`, before the queue is
    opened): nothing else sends a review that gives a verdict or releases an action, least of
    all leaving the page."""
    with page.expect_response(
        lambda r: r.request.method == "POST" and r.url.endswith(f"/api/review/{decision_id}")
    ):
        page.clock.run_for(5000)


def texts(found: dict[str, Any]) -> list[tuple[str, str]]:
    """Each text of a decision and where it stands."""
    return [(text["name"], text["state"]) for text in found.get("outputs", [])]


def acted(found: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Each action of a decision, what may be done with it and why."""
    return [(a["name"], a["disposition"], a["reason"]) for a in found.get("actions", [])]


def card(page: Page, decision_id: str) -> Locator:
    """A decision's card in the review queue."""
    return page.locator(f'.review-card[data-id="{decision_id}"]')


def drawer(page: Page, decision_id: str) -> Locator:
    """A decision's record, opened by its address over the audit trail."""
    page.goto(f"/#audit/{decision_id}")
    return page.locator(".drawer")


def text_of(scope: Locator, name: str) -> Locator:
    """One text of a decision, in a review card, a resolved review or a record."""
    return scope.locator(f'.texts-list > li[data-output="{name}"]')


def verdict(scope: Locator, name: str, choice: str) -> None:
    """Give the text `name` its verdict: "Release as written" or "Withhold"."""
    text_of(scope, name).get_by_role("radio", name=choice, exact=True).check()


def action_of(scope: Locator, name: str) -> Locator:
    """One action of a decision, by the name it is shown under ("Send reply")."""
    return scope.locator(".actions-list > li", has_text=name)


def release_box(scope: Locator, name: str) -> Locator:
    return action_of(scope, name).get_by_role("checkbox", name="Release when I confirm")
