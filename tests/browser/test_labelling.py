# SPDX-License-Identifier: Apache-2.0
"""The labelling screen (LP-11m, `dashboard/labelling.js`, ADR-0011) in the browser, over a real
memberships-mode server with the evaluation-set keys (tests/browser/access_site.py; throwaway
keys made per test) and sessions planted as `/auth/callback` makes them:

- a labeller and nothing else sees the labelling screen alone (no packs, no other section): their
  counts set by set, then one case at a time, its input shown as text (never markup) and the
  set's questions as labelled radio groups; a label saved brings the next case, and the last one
  the empty state;
- blind: nothing of the case but its input reaches the page, nor anyone else's label;
- a question left unanswered, or a `422` naming a question, is said beside that question, and
  nothing is recorded; a `404` (the case is no longer theirs) moves on to their next case;
- someone holding `label` beside other roles gets the section in their list; without the keys
  (no labelling route) a labeller sees the access page that says so;
- axe-core finds nothing on the screen, in both themes, and it fits a phone.

All cases are hand-written synthetic text. Run with `uv run pytest -m browser`."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, Locator, Page, Route, expect

from mendari import Pack
from mendari.core.evalset_keys import EvalsetKeys
from mendari.core.evalsets import EvalsetStore
from mendari.server.auth import person_id
from tests.browser.access_site import Site, memberships, person, running
from tests.browser.test_access import pins
from tests.browser.test_accessibility import violations
from tests.browser.test_dashboard import PHONE, UNDER_24
from tests.conftest import encrypted_store, evalset_on, hand_written
from tests.test_auth import IDP
from tests.test_evalset_store import MARKS, a_set

pytestmark = pytest.mark.browser

CLINIC = "clinic-a"
MARKUP = '<img src=x onerror="window.pwned=1">'
FIRST = f"{MARKUP} my knee aches after the long walk, since Tuesday"
SECOND = f"{MARKUP} could I move my appointment to next week? Thanks"
DASHBOARD = Path(__file__).resolve().parents[2] / "src" / "mendari" / "dashboard"


@dataclass
class Labelling:
    site: Site
    hidden: EvalsetStore
    pack: Pack

    def labeller(self, sub: str, *roles: str) -> dict[str, str]:
        """A person of Clinic-A holding `roles` (`labeller` unless given), signed in."""
        return person(
            self.site, sub=sub, email=f"{sub}@example.org", org=CLINIC,
            roles=list(roles or ("labeller",)),
        )  # fmt: skip

    def drawn(self, bodies: list[str], subs: list[str]) -> str:
        """A draft set of Clinic-A's holding a hand-written case per body, each drawn and given
        to two of `subs`: its id."""
        set_id = a_set(self.hidden, self.pack, CLINIC)
        written = [
            {**hand_written(body, author=MARKS["author"], stratum=MARKS["stratum"])}
            for body in bodies
        ]
        self.hidden.add_cases(set_id, written)
        self.hidden.sample(set_id, per_stratum=len(bodies))
        self.hidden.assign(set_id, [person_id(IDP, sub) for sub in subs])
        return set_id

    def counts(self, sub: str) -> dict[str, Any]:
        return self.hidden.labelling(person_id(IDP, sub), org=CLINIC)


@pytest.fixture
def labelling(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inbox_pack: Pack
) -> Iterator[Labelling]:
    memberships(monkeypatch)  # with sign-in and PHI mode `encrypt`, as access_site's
    keys = EvalsetKeys(*evalset_on(monkeypatch))
    db = tmp_path / "a.db"
    with running(browser, db) as site, encrypted_store(db) as store:
        yield Labelling(site, EvalsetStore(store, keys), inbox_pack)


def opened(page: Page) -> None:
    page.goto("/")
    expect(page.locator("main h1")).to_have_text("Labelling")


def case(page: Page) -> Locator:
    return page.locator("#label-case")


def question(page: Page, name: str) -> Locator:
    return page.locator(".label-question", has=page.locator("legend", has_text=name))


def answer(page: Page, destination: str = "physician", urgency: str = "routine") -> None:
    question(page, "Destination").get_by_role("radio", name=destination, exact=True).check()
    question(page, "Urgency").get_by_role("radio", name=urgency, exact=True).check()


def test_a_labeller_labels_case_after_case_blind_until_nothing_waits(
    labelling: Labelling,
) -> None:
    ada, _ = labelling.labeller("ada"), labelling.labeller("bo")
    set_id = labelling.drawn([FIRST, SECOND], ["ada", "bo"])
    page = labelling.site.window(ada)
    seen = pins(page)
    opened(page)
    # The screen alone: no pack, no other section (the API docs stay).
    expect(page.locator(".pack-picker")).to_be_hidden()
    expect(page.locator(".nav a[data-view]:visible")).to_have_text(["Labelling"])
    expect(page.locator('.nav a[data-view="labelling"]')).to_have_attribute("aria-current", "page")
    sets = page.locator('[data-table="label-sets"] tbody tr')
    expect(sets).to_have_count(1)
    expect(sets.first).to_contain_text(set_id)
    expect(sets.first.locator("td")).to_have_text([set_id, "2", "0"])
    # The case as its author wrote it, as text: the markup is shown, never run.
    expect(case(page)).to_contain_text("2 cases wait for you, this one included.")
    shown = case(page).locator(".case-input")
    expect(shown).to_contain_text(MARKUP)  # either case first (the order is by case id)
    first = shown.inner_text()
    later = SECOND if FIRST in first else FIRST
    expect(page.locator("#label-form input:checked")).to_have_count(0)  # nothing chosen for them
    expect(case(page).locator(".case-input img")).to_have_count(0)
    assert page.evaluate("window.pwned") is None
    # Blind: nothing of the case but its input, nor anyone's label.
    text = page.locator("body").inner_text()
    for private in (MARKS["author"], MARKS["stratum"], MARKS["name"], "confidence", "intended"):
        assert private not in text
    # Each question a labelled radio group, operable from the keyboard.
    destination = question(page, "Destination").get_by_role("radio")
    expect(destination.first).to_be_visible()
    destination.first.focus()
    page.keyboard.press("ArrowDown")
    expect(destination.nth(1)).to_be_checked()
    question(page, "Urgency").get_by_role("radio", name="routine", exact=True).check()
    page.get_by_role("button", name="Save label").click()
    expect(page.locator("#toast")).to_have_text("Label saved. 1 case waits for you.")
    expect(shown).to_contain_text(later)
    expect(sets.first.locator("td")).to_have_text([set_id, "1", "1"])
    expect(case(page).locator("h2")).to_be_focused()
    answer(page)
    page.get_by_role("button", name="Save label").click()
    expect(case(page).locator(".empty-state h2")).to_have_text("Nothing waits for you")
    expect(sets.first.locator("td")).to_have_text([set_id, "0", "2"])
    counted = labelling.counts("ada")
    assert counted["remaining"] == 0
    assert [(s["open"], s["labelled"]) for s in counted["sets"]] == [(0, 2)]
    # Bo's cases are untouched by Ada's labels, and Bo is shown the same as Ada was: the case,
    # nothing chosen, their own counts.
    assert labelling.counts("bo")["remaining"] == 2
    bo = labelling.site.window(labelling.labeller("bo"))
    opened(bo)
    expect(bo.locator('[data-table="label-sets"] tbody tr td')).to_have_text([set_id, "2", "0"])
    expect(case(bo).locator(".case-input")).to_contain_text(MARKUP)
    assert case(bo).locator(".case-input").inner_text().strip() in {FIRST, SECOND}
    expect(bo.locator("#label-form input:checked")).to_have_count(0)
    # Ada's page asked for nothing but who she is, health and her own labelling, each for her
    # organization; and kept nothing in the browser.
    paths = {path for _, path, _ in seen}
    assert {p for p in paths if not p.startswith("/api/labelling")} <= {
        "/api/health",
        "/api/auth/me",
    }
    assert {org for _, path, org in seen if path.startswith("/api/labelling")} == {CLINIC}
    assert page.evaluate("localStorage.length + sessionStorage.length") == 0
    # A case assigned since: checking again brings it.
    labelling.drawn([SECOND], ["ada", "bo"])
    page.get_by_role("button", name="Check again").click()
    expect(shown).to_contain_text(SECOND)
    expect(sets).to_have_count(2)


def test_an_unanswered_question_is_said_beside_it_and_nothing_is_sent(
    labelling: Labelling,
) -> None:
    ada, _ = labelling.labeller("ada"), labelling.labeller("bo")
    labelling.drawn([SECOND], ["ada", "bo"])
    page = labelling.site.window(ada)
    posted: list[str] = []
    page.on("request", lambda r: posted.append(r.url) if r.method == "POST" else None)
    opened(page)
    question(page, "Destination").get_by_role("radio", name="physician", exact=True).check()
    page.get_by_role("button", name="Save label").click()
    urgency = question(page, "Urgency")
    expect(urgency.locator(".field-error")).to_have_text("Choose a label for this question.")
    expect(urgency.get_by_role("radio").first).to_have_attribute("aria-invalid", "true")
    expect(urgency.get_by_role("radio").first).to_be_focused()
    expect(question(page, "Destination").locator(".field-error")).to_be_hidden()
    assert posted == []
    urgency.get_by_role("radio", name="routine", exact=True).check()
    page.get_by_role("button", name="Save label").click()
    expect(case(page).locator(".empty-state h2")).to_have_text("Nothing waits for you")


def test_a_422_is_shown_beside_the_question_it_names(labelling: Labelling) -> None:
    """The server refuses a label not among a question's labels, naming the question (never the
    value): the page says so beside it, keeps the case, and nothing is recorded."""
    ada, _ = labelling.labeller("ada"), labelling.labeller("bo")
    labelling.drawn([SECOND], ["ada", "bo"])
    page = labelling.site.window(ada)

    def tampered(route: Route) -> None:
        sent = json.loads(route.request.post_data or "{}")
        sent["labels"]["urgency"] = "not-a-label"
        route.continue_(post_data=json.dumps(sent))

    page.route("**/api/labelling/*-*", tampered)  # an assignment's id, not /next
    opened(page)
    answer(page)
    with page.expect_response(lambda r: r.request.method == "POST") as answered:
        page.get_by_role("button", name="Save label").click()
    assert answered.value.status == 422
    urgency = question(page, "Urgency")
    expect(urgency.locator(".field-error")).to_have_text("Choose one of this question's labels.")
    expect(urgency.get_by_role("radio").first).to_have_attribute("aria-invalid", "true")
    expect(question(page, "Destination").locator(".field-error")).to_be_hidden()
    expect(case(page).locator(".case-input")).to_contain_text(SECOND)
    assert labelling.counts("ada")["remaining"] == 1


def test_a_case_no_longer_theirs_moves_on_to_the_next(labelling: Labelling) -> None:
    """A `404` (an assignment unknown, someone else's or no longer open: one answer) is not an
    error to fix: the page says so and shows the labeller's next case."""
    ada, _ = labelling.labeller("ada"), labelling.labeller("bo")
    labelling.drawn([SECOND], ["ada", "bo"])
    page = labelling.site.window(ada)
    gone = f"{labelling.site.base}/api/labelling/{uuid.uuid4()}"

    def elsewhere(route: Route) -> None:
        if route.request.method == "POST":
            route.continue_(url=gone)
        else:
            route.fallback()

    page.route("**/api/labelling/*-*", elsewhere)
    opened(page)
    answer(page)
    page.get_by_role("button", name="Save label").click()
    expect(page.locator("#toast")).to_have_text(
        "That case is no longer yours to label. Here is your next one."
    )
    expect(case(page).locator(".case-input")).to_contain_text(SECOND)  # still open: still next
    expect(case(page).locator("h2")).to_be_focused()
    assert labelling.counts("ada")["remaining"] == 1


def test_someone_who_also_labels_finds_it_in_their_sections(labelling: Labelling) -> None:
    labelling.labeller("ada")
    labelling.labeller("bo")
    labelling.drawn([SECOND], ["ada", "bo"])
    page = labelling.site.window(labelling.labeller("cy", "viewer", "labeller"))
    page.goto("/#overview")
    link = page.locator('.nav a[data-view="labelling"]')
    expect(link).to_be_visible()
    expect(page.locator('.nav a[data-view="overview"]')).to_be_visible()
    link.click()
    expect(page.locator("main h1")).to_have_text("Labelling")
    expect(page.locator("#label-case h2")).to_have_text("Nothing waits for you")  # not cy's


def test_without_the_keys_a_labeller_is_told_there_is_nothing_to_label(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memberships(monkeypatch)  # no evaluation-set keys: no labelling route
    with running(browser, tmp_path / "a.db") as site:
        page = site.window(
            person(site, sub="ada", email="ada@example.org", org=CLINIC, roles=["labeller"])
        )
        page.goto("/")
        expect(page.locator("main h1")).to_have_text("Nothing to do here yet")
        expect(page.locator("main")).to_contain_text("Labelling is not switched on here yet")
        expect(page.locator(".nav a[data-view]:visible")).to_have_count(0)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_labelling_screen_passes_axe(labelling: Labelling, scheme: str) -> None:
    ada, _ = labelling.labeller("ada"), labelling.labeller("bo")
    labelling.drawn([FIRST], ["ada", "bo"])
    page = labelling.site.window(ada, bypass_csp=True, color_scheme=scheme)  # axe is injected
    found = {}
    opened(page)
    found["a case"] = violations(page)
    page.get_by_role("button", name="Save label").click()
    expect(question(page, "Urgency").locator(".field-error")).to_be_visible()
    found["unanswered"] = violations(page)
    answer(page)
    page.get_by_role("button", name="Save label").click()
    expect(case(page).locator(".empty-state")).to_be_visible()
    found["nothing waits"] = violations(page)
    assert {where: v for where, v in found.items() if v} == {}


def test_the_labelling_screen_fits_a_phone(labelling: Labelling) -> None:
    ada, _ = labelling.labeller("ada"), labelling.labeller("bo")
    labelling.drawn([FIRST], ["ada", "bo"])
    page = labelling.site.window(ada, viewport=PHONE, has_touch=True, is_mobile=True)
    opened(page)
    expect(question(page, "Urgency")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page.evaluate(UNDER_24) == []


def test_the_screen_imports_only_the_router_the_helpers_and_the_record() -> None:
    """What the labelling screen can reach is what it imports: nothing of packs, decisions,
    tables or charts."""
    lines = (DASHBOARD / "labelling.js").read_text().splitlines()
    assert [line for line in lines if line.startswith("import")] == [
        'import { recordHtml } from "./record.js";',
        'import { views } from "./router.js";',
        "import { announce, api, emptyState, esc, focusHeading, slotText, state, toast, "
        'toastError, whileBusy } from "./ui.js";',
    ]
