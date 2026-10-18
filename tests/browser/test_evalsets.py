# SPDX-License-Identifier: Apache-2.0
"""The hidden sets' screen (LP-11n, `dashboard/evalsets.js`, ADR-0011) in the browser, over a real
memberships-mode server with the evaluation-set keys (tests/browser/access_site.py; throwaway keys
made per test) and sessions planted as `/auth/callback` makes them:

- an administrator lists their organization's sets, opens one, draws its cases, gives them to two
  labellers and a disputed one to a third, and follows the labelling, each labeller named from
  the members list; a refusal is said in the form, and nothing is sent for one the page can tell;
- the stored aggregates: percent agreement as k of n with its interval (no bare percentage
  anywhere), kappa and alpha as numbers, "not estimable" in words, coverage with the
  insufficient-evidence mark; nothing before the key holder measures; a frozen set is followed,
  never drawn or assigned, and one frozen while open loses its forms;
- a draw's button stays busy until the set is read again; the page shows the set its address
  names, by its latest read (a failed or abandoned load, or an older answer, never wins);
- it shows ids, counts, names and aggregates only: no case, input, author, case id or case hash;
- labellers, the other roles, an administrator without the keys and one in claims mode never see
  it or its link, and their pages ask for no set (`pins()`); another organization's set is no set;
- axe-core finds nothing on any tab in both themes; a refusal describes the fields it marks; every
  tab fits a phone (a set name with no break in it too) with every control a 24 px target.

All cases are hand-written synthetic text. Run with `uv run pytest -m browser`."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import APIResponse, Browser, Locator, Page, Route, expect

from mendari import Pack
from mendari.core.evalset_keys import EvalsetKeys
from mendari.core.evalsets import EvalsetStore
from mendari.server.auth import person_id
from tests.browser.access_site import Site, memberships, person, running
from tests.browser.test_access import pins
from tests.browser.test_accessibility import violations
from tests.browser.test_dashboard import BARE_PERCENT, PHONE, UNDER_24
from tests.conftest import encrypt_mode, encrypted_store, evalset_on, signed_in
from tests.test_auth import CLIENT, IDP
from tests.test_evalset_labelling import cases
from tests.test_evalset_store import MARKS

pytestmark = pytest.mark.browser

CLINIC = "clinic-a"
OTHER = "clinic-b"
STRATA = ("zq-stratum-one", "zq-stratum-two")
# Labellers' names hold a letter no hex digit is, so none is found inside a user id.
LABELLERS = ("kim", "lou", "nia")
ROUTINE = {"destination": "nurse", "urgency": "routine"}
PHYSICIAN = {"destination": "physician", "urgency": "routine"}
TABS = ["Progress", "Sample", "Assign", "Adjudicate", "Agreement", "Coverage"]
MEMBERS = "/api/auth/members"
# Every role a membership may hold but an administrator's, alone, and a labeller who also reads.
NOT_CURATORS = [
    ("labeller",),
    ("viewer",),
    ("reviewer",),
    ("service",),
    ("governance",),
    ("pack_author",),
    ("viewer", "labeller"),
]
OVERVIEW = '.nav a[data-view="overview"]'
# What a missing value would print; "undefined in 4 of 50 resamples" is a reason, in words.
NOT_A_NUMBER = re.compile(r"\b(NaN|null|Infinity)\b|\bundefined\b(?! in \d+ of \d+ resamples)")
DASHBOARD = Path(__file__).resolve().parents[2] / "src" / "mendari" / "dashboard"
SECOND = "zq second set"
# A set name is free text: one with no break opportunity must still wrap on a phone.
UNBROKEN = "Triage_urgency_hidden_set_q4_2026_north_clinic_a_second_pass"
# The page fits its viewport: on a mobile browser `innerWidth` widens to the content, so the
# layout viewport's width (`clientWidth`) is the measure.
FITS = "document.documentElement.scrollWidth <= document.documentElement.clientWidth"


@dataclass
class Curating:
    site: Site
    hidden: EvalsetStore
    pack: Pack

    def person(self, sub: str, *roles: str, org: str = CLINIC) -> dict[str, str]:
        """A person of `org` holding `roles`, signed in (their name is `sub`)."""
        return person(self.site, sub=sub, email=f"{sub}@example.org", org=org, roles=list(roles))

    def curator(self, **options: Any) -> Page:
        """Clinic-A's administrator, signed in, in a window of their own."""
        return self.site.window(self.person("lead", "admin"), **options)

    def made(self, count: int = 4, org: str = CLINIC, name: str = MARKS["name"]) -> str:
        """A draft set of `org`'s named `name` with `count` hand-written cases over two strata,
        as the key holder makes one: its id."""
        set_id = str(self.hidden.create_set(org, self.pack, name, created_by="ops")["set"])
        self.hidden.add_cases(set_id, cases(count, strata=STRATA))
        return set_id

    def drawn(self, count: int, subs: tuple[str, ...]) -> str:
        """A set whose `count` cases are all drawn and each given to two of `subs`: its id."""
        set_id = self.made(count)
        self.hidden.sample(set_id, cases=count)
        self.hidden.assign(set_id, [person_id(IDP, sub) for sub in subs])
        return set_id

    def labelled(self, sub: str, *given: dict[str, str]) -> int:
        """Every case waiting for `sub`, labelled at the store, in the order they come: the first
        with `given[0]`, the next with `given[1]`, and the rest with the last. How many."""
        user = person_id(IDP, sub)
        done = 0
        while (shown := self.hidden.next_case(user, org=CLINIC)) is not None:
            labels = given[min(done, len(given) - 1)]
            self.hidden.record_label(shown["assignment"], user, labels, org=CLINIC)
            done += 1
        return done

    def case_marks(self, set_id: str) -> list[str]:
        """The set's case ids and case hashes: none may reach the page."""
        with self.hidden.store._db.session(exclusive=False) as db:
            rows = db.all("SELECT id, case_hash FROM evalset_cases WHERE set_id = ?", (set_id,))
        return [str(row[key]) for row in rows for key in ("id", "case_hash")]


@pytest.fixture
def curating(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inbox_pack: Pack
) -> Iterator[Curating]:
    memberships(monkeypatch)  # with sign-in and PHI mode `encrypt`, as access_site's
    keys = EvalsetKeys(*evalset_on(monkeypatch))
    db = tmp_path / "a.db"
    with running(browser, db) as site, encrypted_store(db) as store:
        yield Curating(site, EvalsetStore(store, keys), inbox_pack)


def opened(page: Page, set_id: str, tab: str = "") -> None:
    """The set's `tab` (its first unless given), freshly loaded."""
    page.goto("about:blank")
    page.goto(f"/#evalsets/{set_id}{f'/{tab}' if tab else ''}")
    expect(page.locator("main h1")).to_have_text(MARKS["name"])
    if tab:
        expect(page.locator(f'.tabs a[data-tab="{tab}"]')).to_have_attribute("aria-current", "page")
    expect(page.locator("#view")).not_to_have_attribute("aria-busy", "true")


def tab(page: Page, name: str) -> None:
    """Another tab of the set shown, read again."""
    page.locator(".tabs a", has_text=name).click()
    expect(page.locator(".tabs a", has_text=name)).to_have_attribute("aria-current", "page")


def figure(page: Page, label: str) -> Locator:
    """The value of the stat labelled exactly `label`."""
    named = page.locator(".stat-label").filter(has_text=re.compile(rf"^{re.escape(label)}$"))
    return page.locator(".stat", has=named).locator(".stat-value")


def choose(page: Page, form: str, *names: str) -> None:
    for name in names:
        page.locator(f"#{form} .pick", has_text=name).get_by_role("checkbox").check()


class Reads:
    """One set's reads (`GET /api/evalsets/<id>`) in a window: while `holding`, each is answered
    by the server at once and its answer kept from the page until `release()`, so a slow read
    answers what the set was when it was asked."""

    def __init__(self, page: Page, set_id: str) -> None:
        self.page = page
        self.holding = False
        self.held: list[tuple[Route, APIResponse]] = []
        page.route(f"**/api/evalsets/{set_id}", self.answer)

    def answer(self, route: Route) -> None:
        if self.holding and route.request.method == "GET":
            self.held.append((route, route.fetch()))
        else:
            route.continue_()

    def taken(self, count: int = 1) -> None:
        """Until `count` reads are held (the handler runs just after the request event)."""
        for _ in range(100):
            if len(self.held) >= count:
                return
            self.page.wait_for_timeout(20)
        raise AssertionError(f"{len(self.held)} of {count} reads held")

    def release(self) -> None:
        """The reads held answered, late, and those that follow passed; time for what a late
        answer could do to the page."""
        self.holding = False
        for route, response in self.held:
            route.fulfill(response=response)
        self.held.clear()
        self.page.wait_for_timeout(300)


def test_a_curator_draws_assigns_adjudicates_and_follows_the_labelling(
    curating: Curating,
) -> None:
    for sub in LABELLERS:
        curating.person(sub, "labeller")
    curating.person("ola", "viewer")  # not a labeller: never offered
    curating.person("sue", "admin", "labeller")  # manages the sets: never offered
    set_id = curating.made(4)
    page = curating.curator()
    seen = pins(page)
    posted: list[str] = []
    page.on("request", lambda r: posted.append(r.url) if r.method == "POST" else None)
    page.goto("/")
    link = page.locator('.nav a[data-view="evalsets"]')
    expect(link).to_have_text("Hidden sets")
    link.click()
    expect(page.locator("main h1")).to_have_text("Hidden sets")
    listed = page.locator('[data-table="evalsets"] tbody tr')
    expect(listed).to_have_count(1)
    for shown in (MARKS["name"], set_id, curating.pack.id, "Draft"):
        expect(listed.first).to_contain_text(shown)
    expect(listed.first.locator("td.num")).to_have_text("4")
    page.get_by_role("link", name=MARKS["name"]).click()
    # The set: its facts, and its tabs with Progress first.
    expect(page.locator("main h1")).to_have_text(MARKS["name"])
    expect(page.locator(".tabs a")).to_have_text(TABS)
    expect(page.locator('.tabs a[data-tab="progress"]')).to_have_attribute("aria-current", "page")
    expect(page.locator(".evalset-facts")).to_contain_text(set_id)
    expect(page.locator(".evalset-facts")).to_contain_text("Evaluation runs")
    expect(figure(page, "Not drawn")).to_have_text("4")
    expect(page.locator("#evalset-body .empty-state h2")).to_have_text("No one is assigned yet")
    # Draw one case from each stratum.
    tab(page, "Sample")
    page.get_by_label("How many").fill("1")
    page.get_by_label("Seed").fill("7")
    page.get_by_role("button", name="Draw cases").click()
    expect(page.locator("#toast")).to_have_text("Drew 2 cases: 2 drawn in all (seed 7).")
    expect(page.locator('.tabs a[data-tab="sample"]')).to_be_focused()
    assert curating.hidden.progress(set_id)["statuses"]["sampled"] == 2
    # Give them to two labellers: the labellers alone are offered, by name.
    tab(page, "Assign")
    expect(page.locator("#assign-form .pick b")).to_have_text(list(LABELLERS))
    choose(page, "assign-form", "kim")
    page.get_by_role("button", name="Assign").click()
    expect(page.locator("#assign-form .field-error")).to_have_text("Choose 2 labellers or more.")
    checkbox = page.locator("#assign-form .pick input").first
    expect(checkbox).to_have_attribute("aria-invalid", "true")
    expect(checkbox).to_have_accessible_description("Choose 2 labellers or more.")
    assert posted == [f"{curating.site.base}/api/evalsets/{set_id}/sample"]
    choose(page, "assign-form", "lou")
    page.get_by_role("button", name="Assign").click()
    expect(page.locator("#toast")).to_have_text("Made 4 assignments.")
    # They label (at the store): one case agreed, one disputed.
    assert curating.labelled("kim", ROUTINE) == 2
    assert curating.labelled("lou", PHYSICIAN, ROUTINE) == 2
    tab(page, "Progress")
    for label, value in (("Not drawn", "2"), ("Agreed", "1"), ("Disputed", "1")):
        expect(figure(page, label)).to_have_text(value)
    first = page.locator('[data-table="evalset-first"] tbody tr')
    expect(first).to_have_count(2)
    expect(first.nth(0).locator("td")).to_have_text(["kim", "2", "2", "0", "0"])
    expect(first.nth(1).locator("td")).to_have_text(["lou", "2", "2", "0", "0"])
    expect(page.locator('[data-table="evalset-third"]')).to_have_count(0)
    # The disputed case to a third, who settles it.
    tab(page, "Adjudicate")
    expect(page.locator("#adjudicate-form")).to_contain_text("1 case is disputed.")
    choose(page, "adjudicate-form", "nia")
    page.get_by_role("button", name="Adjudicate").click()
    expect(page.locator("#toast")).to_have_text("Gave 1 disputed case a third labeller.")
    assert curating.labelled("nia", ROUTINE) == 1
    tab(page, "Progress")
    expect(figure(page, "Resolved")).to_have_text("1")
    expect(figure(page, "Disputed")).to_have_text("0")
    third = page.locator('[data-table="evalset-third"] tbody tr')
    expect(third.locator("td")).to_have_text(["nia", "1", "1", "0", "0"])
    # Ids, counts and names only: no case, input, author, case id or case hash.
    html = page.content()
    for private in (MARKS["input"], MARKS["author"], MARKS["language"], *STRATA):
        assert private not in html, private
    for mark in curating.case_marks(set_id):
        assert mark not in html
    # Every request of the screen named the organization the window shows.
    screen = [(m, p, org) for m, p, org in seen if p.startswith(("/api/evalsets", MEMBERS))]
    assert {org for _, _, org in screen} == {CLINIC}
    writes = {p for m, p, _ in screen if m == "POST"}
    assert writes == {
        f"/api/evalsets/{set_id}/{p}" for p in ("sample", "assignments", "adjudication")
    }


def test_a_refusal_is_said_in_the_form_and_nothing_is_drawn(curating: Curating) -> None:
    set_id = curating.made(4)
    page = curating.curator()
    posted: list[str] = []
    page.on("request", lambda r: posted.append(r.url) if r.method == "POST" else None)
    opened(page, set_id, "sample")
    page.get_by_role("radio", name=re.compile("^In all")).check()
    page.get_by_role("button", name="Draw cases").click()
    count = page.get_by_label("How many")
    error = page.locator("#sample-form .field-error")
    expect(error).to_have_text("How many is a whole number from 1 to 1000000.")
    expect(count).to_have_attribute("aria-invalid", "true")
    expect(count).to_have_accessible_description("How many is a whole number from 1 to 1000000.")
    expect(count).to_be_focused()
    # The seed refused: the refusal describes it beside its hint, and no longer the count.
    seed = page.get_by_label("Seed")
    count.fill("99")
    seed.fill("-1")
    page.get_by_role("button", name="Draw cases").click()
    expect(error).to_have_text("The seed is a whole number from 0 to 4294967295.")
    expect(seed).to_have_attribute("aria-describedby", "sample-seed-hint sample-form-error")
    assert count.get_attribute("aria-describedby") is None
    assert posted == []
    seed.fill("0")  # more than the set has left: the server's words
    page.get_by_role("button", name="Draw cases").click()
    expect(error).to_contain_text("4 case(s) left to draw, not 99")
    expect(count).not_to_have_attribute("aria-invalid", "true")
    expect(seed).to_have_attribute("aria-describedby", "sample-seed-hint")
    assert len(posted) == 1
    assert curating.hidden.progress(set_id)["statuses"]["draft"] == 4
    # Assigning before anything is drawn: the page says to draw first.
    curating.person("kim", "labeller")
    tab(page, "Assign")
    expect(page.locator("#evalset-body .empty-state h2")).to_have_text("Nothing is drawn yet")


def test_the_stored_aggregates_are_counts_and_coefficients_in_words(curating: Curating) -> None:
    """Before the key holder measures, nothing; then percent agreement as k of n with its interval
    (never a bare percentage), kappa and alpha as numbers or "not estimable" in words (never a
    blank, NaN or null), and coverage with the insufficient-evidence mark."""
    curating.person("kim", "labeller")
    curating.person("lou", "labeller")
    set_id = curating.drawn(4, ("kim", "lou"))
    nurse, physician = ROUTINE, PHYSICIAN
    assert curating.labelled("kim", nurse, physician, nurse, physician) == 4
    assert curating.labelled("lou", nurse, physician, physician, physician) == 4
    page = curating.curator()
    for name in ("agreement", "coverage"):
        opened(page, set_id, name)
        expect(page.locator("#evalset-body .empty-state h2")).to_have_text("Not measured yet")
    curating.hidden.measure(set_id, resamples=50)
    opened(page, set_id, "agreement")
    body = page.locator("#evalset-body")
    expect(body).to_contain_text("over the 4 drawn cases")
    urgency = page.locator(".evalset-question", has=page.locator("h2", has_text="Urgency"))
    # Every first label of urgency is routine: agreement is total, the coefficients undefined.
    expect(urgency.locator(".stat-value").first).to_contain_text("4 of 4")
    expect(urgency.locator(".stat-value .interval")).to_have_count(1)
    expect(urgency.locator(".stat-value").nth(1)).to_have_text("not estimable")
    pair = urgency.locator("tbody tr td")
    expect(pair).to_have_text(
        ["kim and lou", "4", "not estimable", "both raters gave every case one and the same label"]
    )
    destination = page.locator(".evalset-question", has=page.locator("h2", has_text="Destination"))
    expect(destination.locator(".stat-value").first).to_contain_text(re.compile(r"^[0-4] of 4"))
    kappa = destination.locator("tbody tr td").nth(2)
    expect(kappa).to_have_text(re.compile(r"^-?\d\.\d{3}$"))  # both gave labels of two kinds
    text = body.inner_text()
    assert NOT_A_NUMBER.search(text) is None, text
    assert page.evaluate(BARE_PERCENT) == []
    opened(page, set_id, "coverage")
    critical = page.locator('[data-table="evalset-critical"] tbody tr')
    expect(critical.locator("td").nth(1)).to_have_text("urgent")
    expect(critical.locator(".evidence-mark")).to_have_text("insufficient evidence")
    mix = page.locator('[data-table="evalset-mix"] tbody')
    for value in (*STRATA, MARKS["language"]):  # a curator's own aggregates name them (conflict 55)
        expect(mix).to_contain_text(value)
    expect(figure(page, "Cases")).to_have_text("4")
    assert NOT_A_NUMBER.search(page.locator("#evalset-body").inner_text()) is None
    assert page.evaluate(BARE_PERCENT) == []
    html = page.content()
    for private in (MARKS["input"], MARKS["author"], *curating.case_marks(set_id)):
        assert private not in html


def test_a_frozen_set_is_followed_but_no_longer_drawn_or_assigned(curating: Curating) -> None:
    curating.person("kim", "labeller")
    curating.person("lou", "labeller")
    set_id = curating.drawn(2, ("kim", "lou"))
    for sub in ("kim", "lou"):
        assert curating.labelled(sub, ROUTINE) == 2
    curating.hidden.freeze(set_id, max_runs=3, max_age_days=30, resamples=20)
    page = curating.curator()
    page.goto("/#evalsets")
    expect(page.locator('[data-table="evalsets"] tbody tr')).to_contain_text("Frozen")
    page.goto("about:blank")
    page.goto(f"/#evalsets/{set_id}/sample")  # no such tab now: the first one
    expect(page.locator(".tabs a")).to_have_text(["Progress", "Agreement", "Coverage"])
    expect(page.locator('.tabs a[data-tab="progress"]')).to_have_attribute("aria-current", "page")
    facts = page.locator(".evalset-facts")
    expect(facts).to_contain_text("Frozen")
    expect(facts).to_contain_text("Commitment")
    expect(facts).to_contain_text("0 used, 3 left of 3, until")
    expect(facts).to_contain_text("(30 days from freezing)")
    tab(page, "Agreement")
    expect(page.locator(".evalset-question")).to_have_count(2)


def test_a_set_frozen_while_open_loses_its_forms(curating: Curating) -> None:
    """The key holder freezes a draft while its Sample tab is open: the draw is refused in the
    form, and the next tab shown reads the set again and drops the forms' tabs."""
    curating.person("kim", "labeller")
    curating.person("lou", "labeller")
    set_id = curating.drawn(2, ("kim", "lou"))
    for sub in ("kim", "lou"):
        assert curating.labelled(sub, ROUTINE) == 2
    page = curating.curator()
    opened(page, set_id, "sample")
    expect(page.locator(".tabs a")).to_have_text(TABS)
    curating.hidden.freeze(set_id, max_runs=3, max_age_days=30, resamples=20)
    page.get_by_label("How many").fill("1")
    page.get_by_role("button", name="Draw cases").click()
    expect(page.locator("#sample-form .field-error")).to_contain_text("is frozen: only a draft")
    page.locator(".tabs a", has_text="Assign").click()  # no such tab now: the first one
    expect(page.locator(".tabs a")).to_have_text(["Progress", "Agreement", "Coverage"])
    progress = page.locator('.tabs a[data-tab="progress"]')
    expect(progress).to_have_attribute("aria-current", "page")
    expect(progress).to_be_focused()  # focus stays on the tab bar
    expect(page.locator("#evalset-body")).to_have_attribute("data-tab", "progress")
    expect(page.locator(".evalset-facts")).to_contain_text("Frozen")
    expect(page.locator("#evalset-body form")).to_have_count(0)


def test_a_draw_keeps_its_button_busy_until_the_set_is_read_again(curating: Curating) -> None:
    """A draw is chained and never undone: until the form shows the new counts its button stays
    busy, so a second press (or Enter) draws no second batch."""
    set_id = curating.made(4)
    page = curating.curator()
    posted: list[str] = []
    page.on("request", lambda r: posted.append(r.url) if r.method == "POST" else None)
    opened(page, set_id, "sample")
    reads = Reads(page, set_id)
    reads.holding = True
    count = page.get_by_label("How many")
    count.fill("1")
    page.get_by_label("Seed").fill("7")
    button = page.locator('#sample-form button[type="submit"]')
    button.click()
    expect(page.locator("#toast")).to_have_text("Drew 2 cases: 2 drawn in all (seed 7).")
    reads.taken()
    expect(button).to_be_disabled()
    expect(page.locator("#sample-form")).to_contain_text("0 cases of 4 drawn so far")
    count.press("Enter")  # a form whose submit button is disabled is not submitted
    reads.release()
    expect(page.locator("#sample-form")).to_contain_text("2 cases of 4 drawn so far")
    expect(page.locator('#sample-form button[type="submit"]')).to_be_enabled()
    assert posted == [f"{curating.site.base}/api/evalsets/{set_id}/sample"]
    assert curating.hidden.progress(set_id)["statuses"]["sampled"] == 2


def test_an_older_read_of_the_set_never_replaces_a_newer_one(curating: Curating) -> None:
    """Agreement's read is slow; the cases are drawn meanwhile and Progress is shown from a newer
    read. The older answer, arriving last, is dropped."""
    set_id = curating.made(4)
    page = curating.curator()
    opened(page, set_id, "progress")
    expect(figure(page, "Not drawn")).to_have_text("4")
    reads = Reads(page, set_id)
    reads.holding = True
    page.locator(".tabs a", has_text="Agreement").click()
    reads.taken()
    reads.holding = False
    curating.hidden.sample(set_id, cases=2)
    page.locator(".tabs a", has_text="Progress").click()
    expect(figure(page, "Not drawn")).to_have_text("2")
    reads.release()  # the older read: 4 not drawn
    expect(figure(page, "Not drawn")).to_have_text("2")
    expect(page.locator('.tabs a[data-tab="progress"]')).to_have_attribute("aria-current", "page")


def test_the_page_shows_the_set_its_address_names(curating: Curating) -> None:
    """Another set fails to load, or is left with Back while it loads: the set the address names
    is the one shown (and the one its forms would write to), never the other or its error."""
    first = curating.made(2)
    second = curating.made(2, name=SECOND)
    page = curating.curator()
    opened(page, first, "coverage")
    to_second = f"location.hash = '#evalsets/{second}'"
    page.route(f"**/api/evalsets/{second}", lambda route: route.fulfill(status=500, json={}))
    page.evaluate(to_second)
    expect(page.locator("main h1")).to_have_text("Could not load this view")
    page.go_back()
    expect(page.locator("main h1")).to_have_text(MARKS["name"])
    expect(page.locator('.tabs a[data-tab="coverage"]')).to_have_attribute("aria-current", "page")
    page.unroute(f"**/api/evalsets/{second}")
    reads = Reads(page, second)
    reads.holding = True
    page.evaluate(to_second)
    reads.taken()
    page.go_back()  # while the second set loads
    reads.release()
    expect(page).to_have_url(re.compile(f"#evalsets/{re.escape(first)}/coverage$"))
    expect(page.locator("main h1")).to_have_text(MARKS["name"])
    expect(page.locator('.tabs a[data-tab="coverage"]')).to_have_attribute("aria-current", "page")
    assert SECOND not in page.locator("main").inner_text()


def test_another_organizations_set_is_no_set(curating: Curating) -> None:
    curating.person("lead-b", "admin", org=OTHER)
    theirs = curating.made(2, org=OTHER)
    page = curating.curator()
    seen = pins(page)
    page.goto(f"/#evalsets/{theirs}")
    expect(page.locator("main .empty-state h2")).to_have_text("No such set")
    page.get_by_role("link", name="All hidden sets").click()
    expect(page.locator("main .empty-state h2")).to_have_text("No hidden sets yet")
    assert MARKS["name"] not in page.content()
    assert {org for _, path, org in seen if path.startswith("/api/evalsets")} == {CLINIC}
    # "." asks the browser for the list of sets (`/api/evalsets/.` is `/api/evalsets/`): no set.
    curating.made(2)
    for address in (".", "%2E"):
        page.goto("about:blank")
        page.goto(f"/#evalsets/{address}")
        expect(page.locator("main .empty-state h2")).to_have_text("No such set")


def test_labellers_and_the_other_roles_never_see_the_screen(curating: Curating) -> None:
    set_id = curating.made(2)
    for roles in NOT_CURATORS:
        page = curating.site.window(curating.person("-".join(roles), *roles))
        seen = pins(page)
        page.goto(f"/#evalsets/{set_id}/sample")
        if roles == ("labeller",):  # the labelling screen alone
            expect(page.locator("main h1")).to_have_text("Labelling")
        elif roles == ("pack_author",):  # a reserved role grants nothing: the access page
            expect(page.locator("main h1")).to_have_text("Nothing to do here yet")
        else:  # not a view they may use: the overview
            expect(page.locator(OVERVIEW)).to_have_attribute("aria-current", "page")
        expect(page.locator("#view")).not_to_have_attribute("aria-busy", "true")
        page.wait_for_load_state("networkidle")
        expect(page.locator('.nav a[data-view="evalsets"]')).to_be_hidden()
        assert not [path for _, path, _ in seen if path.startswith("/api/evalsets")], roles
        assert MARKS["name"] not in page.content(), roles


@pytest.mark.parametrize("mode", ["memberships without the keys", "claims mode with the keys"])
def test_an_administrator_has_no_such_screen_where_its_routes_are_not_served(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """The curators' routes are mounted only in memberships mode with the keys: elsewhere an
    administrator sees neither the screen nor its link, and nothing asks for a set."""
    if mode.startswith("memberships"):
        memberships(monkeypatch)
    else:
        monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
        monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", CLIENT)
        encrypt_mode(monkeypatch)
        evalset_on(monkeypatch)
    with running(browser, tmp_path / "a.db") as site:
        headers = (
            person(site, sub="lead", email="lead@example.org", org=CLINIC, roles=["admin"])
            if mode.startswith("memberships")
            else signed_in(site.store, role="admin", host="localhost")
        )
        page = site.window(headers)
        seen = pins(page)
        page.goto("/#evalsets")
        expect(page.locator(OVERVIEW)).to_have_attribute("aria-current", "page")
        expect(page.locator('.nav a[data-view="admin"]')).to_be_visible()  # an administrator
        expect(page.locator('.nav a[data-view="evalsets"]')).to_be_hidden()
        page.wait_for_load_state("networkidle")
        assert not [path for _, path, _ in seen if path.startswith("/api/evalsets")]


def every_tab(curating: Curating) -> tuple[str, list[str]]:
    """A set drawn, labelled (one case disputed) and measured, for the checks over every tab:
    its id and the tabs."""
    for sub in LABELLERS:
        curating.person(sub, "labeller")
    set_id = curating.made(4)
    curating.hidden.sample(set_id, cases=2)
    curating.hidden.assign(set_id, [person_id(IDP, sub) for sub in LABELLERS[:2]])
    curating.labelled("kim", ROUTINE)
    curating.labelled("lou", PHYSICIAN, ROUTINE)
    curating.hidden.measure(set_id, resamples=20)
    return set_id, [name.lower() for name in TABS]


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_screen_passes_axe(curating: Curating, scheme: str) -> None:
    set_id, tabs = every_tab(curating)
    page = curating.curator(bypass_csp=True, color_scheme=scheme)  # axe is injected
    found = {}
    page.goto("/#evalsets")
    expect(page.locator('[data-table="evalsets"]')).to_be_visible()
    found["the list"] = violations(page)
    for name in tabs:
        opened(page, set_id, name)
        found[name] = violations(page)
    opened(page, set_id, "adjudicate")
    page.get_by_role("button", name="Adjudicate").click()  # none chosen: said in the form
    expect(page.locator("#adjudicate-form .field-error")).to_be_visible()
    found["a refusal"] = violations(page)
    assert {where: v for where, v in found.items() if v} == {}


def test_every_tab_fits_a_phone(curating: Curating) -> None:
    set_id, tabs = every_tab(curating)
    page = curating.curator(viewport=PHONE, has_touch=True, is_mobile=True)
    page.goto("/#evalsets")
    expect(page.locator('[data-table="evalsets"]')).to_be_visible()
    assert page.evaluate(FITS) and page.evaluate(UNDER_24) == []
    for name in tabs:
        opened(page, set_id, name)
        page.wait_for_load_state("networkidle")
        assert page.evaluate(FITS), name
        assert page.evaluate(UNDER_24) == [], name
        assert page.evaluate(BARE_PERCENT) == [], name
    unbroken = curating.made(2, name=UNBROKEN)
    page.goto(f"/#evalsets/{unbroken}")
    expect(page.locator("main h1")).to_have_text(UNBROKEN)
    assert page.evaluate(FITS)


def test_the_screen_imports_only_the_router_the_tables_and_the_helpers() -> None:
    """What the screen can reach is what it imports: nothing of packs, decisions or charts, and not
    the Access view (its tab pattern is copied). One line each, so the import map stamps them."""
    lines = (DASHBOARD / "evalsets.js").read_text().splitlines()
    assert [line for line in lines if line.startswith("import")] == [
        'import { render, views, wire } from "./router.js";',
        'import { tableHtml } from "./table.js";',
        "import { $, api, emptyState, esc, evidenceMark, figureHtml, may, num, signedIn, slotText, "
        'stat, state, time, toast, toastError, whileBusy } from "./ui.js";',
    ]
