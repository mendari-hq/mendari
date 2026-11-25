# SPDX-License-Identifier: Apache-2.0
"""Memberships mode in the dashboard (LP-12e-ii, ADR-0008): a person of no organization sees the
access page and nothing else (ask to join, or choose one of theirs); someone in several
organizations switches between them; a window whose access ended, or whose organization was
changed in another window, says so; and every request names the organization the window shows.
The server's answers are those of a memberships-mode deployment (`/api/auth/me` and the
refusals), mocked over the open-mode test server."""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect

from tests.browser.conftest import TRIAGE
from tests.browser.test_accessibility import violations
from tests.browser.test_dashboard import (
    LAYOUT_SHIFT,
    PERMISSIONS,
    PHONE,
    SMART_ITEMS,
    SMART_PATIENT,
    decide_triage,
    launched_as,
    open_agent,
    signed_in_as,
)

pytestmark = pytest.mark.browser

CLINICS = [
    {"org": "default", "name": "The owner's organization", "roles": ["reviewer"]},
    {"org": "clinic-b", "name": "Clinic B", "roles": ["viewer"]},
]


def person(org: str | None, roles: list[str]) -> dict[str, Any]:
    granted = sorted({p for role in roles for p in PERMISSIONS.get(role, [])})
    return {
        "kind": "user",
        "name": "Nurse Kim",
        "roles": roles,
        "permissions": granted,
        "org": org,
        "actor": "kim@clinic.example.org",
    }


def member_of(
    page: Page,
    memberships: list[dict[str, Any]],
    org: str | None,
    roles: list[str],
    *,
    launch: dict[str, Any] | None = None,
) -> list[tuple[str, str, Any]]:
    """`/api/auth/me` as a memberships-mode server answers it. A person of no organization is
    refused everything else as that server would (403 `NoOrganization`), but choosing one and
    asking to join; one with an organization reaches the test server. Returns each API call
    as (method, path, the JSON body sent). With `launch`, the browser also holds that EHR launch."""
    asked: list[tuple[str, str, Any]] = []
    me = {
        "mode": "sign-in",
        "signed_in": True,
        "accounts": "memberships",
        "principal": person(org, roles),
        "memberships": memberships,
    }

    def answer(route: Route) -> None:
        request = route.request
        path = urllib.parse.urlsplit(request.url).path
        body = json.loads(request.post_data) if request.post_data else {}
        asked.append((request.method, path, body or None))
        if path == "/api/auth/me":
            route.fulfill(json=me)
        elif launch is not None and path == "/api/smart/session":
            route.fulfill(json=launch)
        elif launch is not None and path == "/api/smart/patient":
            route.fulfill(json=SMART_PATIENT)
        elif launch is not None and path == "/api/smart/focus":
            route.fulfill(json=SMART_ITEMS)
        elif path == "/api/auth/access-requests":
            route.fulfill(status=202, json={"org": body["org"], "requested": True})
        elif path == "/api/auth/organization":
            route.fulfill(json={"org": body["org"], "name": body["org"], "roles": ["viewer"]})
        elif path == "/auth/logout":
            route.fulfill(status=204, body="")
        elif org is not None or path in ("/api/health", "/api/smart/session"):
            route.fallback()
        else:
            refused = {"error": "NoOrganization", "detail": "you have no organization here"}
            route.fulfill(status=403, json=refused)

    page.route("**/api/**", answer)
    page.route("**/auth/logout", answer)
    return asked


def test_a_person_of_no_organization_sees_the_access_page_and_nothing_else(page: Page) -> None:
    asked = member_of(page, [], None, [])
    page.goto(f"/?agent={TRIAGE}#review")  # whatever the address says
    expect(page.locator("main h1")).to_have_text("You don't have access yet")
    expect(page.locator("main")).to_contain_text("Signed in as kim@clinic.example.org")
    expect(page.locator("#signed-in-name")).to_have_text("Nurse Kim · no organization")
    expect(page.locator(".nav a:visible")).to_have_count(1)  # the API docs only
    expect(page.locator(".agent-picker")).to_be_hidden()
    expect(page.locator(".org-picker")).to_be_hidden()
    paths = {path for _, path, _ in asked}
    assert paths <= {"/api/health", "/api/smart/session", "/api/auth/me"}, paths
    page.goto(f"/?agent={TRIAGE}#access")
    expect(page.locator("main h1")).to_have_text("You don't have access yet")


def test_asking_to_join_sends_the_id_and_says_the_same_whatever_happens(page: Page) -> None:
    asked = member_of(page, [], None, [])
    page.goto("/")
    page.fill("#ask-org", "clinic-a")
    page.get_by_role("button", name="Ask to join").click()
    expect(page.locator("#toast")).to_have_text(
        "If that organization exists, your request is recorded."
    )
    assert ("POST", "/api/auth/access-requests", {"org": "clinic-a"}) in asked
    page.fill("#ask-org", "Clinic A")  # not an id: the browser stops it
    page.get_by_role("button", name="Ask to join").click()
    expect(page.locator("#ask-org:invalid")).to_have_count(1)
    assert len([a for a in asked if a[1] == "/api/auth/access-requests"]) == 1


@pytest.mark.parametrize("held", [CLINICS, CLINICS[1:]], ids=["several", "one-given-since"])
def test_a_person_holding_memberships_chooses_one(page: Page, held: list[dict[str, Any]]) -> None:
    """Several memberships, or one given since signing in: none is chosen yet, so they choose
    (never the no-access page while they hold one)."""
    asked = member_of(page, held, None, [])
    page.goto("/#review")
    expect(page.locator("main h1")).to_have_text("Choose an organization")
    choices = page.locator(".org-choice")
    expect(choices).to_have_count(len(held))
    expect(choices.first).to_contain_text(held[0]["name"])
    choices.last.locator("input").check()
    with page.expect_navigation():  # chosen: the page starts over in that organization
        page.get_by_role("button", name="Continue").click()
    assert ("POST", "/api/auth/organization", {"org": held[-1]["org"]}) in asked


def test_roles_that_grant_nothing_are_said(page: Page) -> None:
    held = [{"org": "clinic-b", "name": "Clinic B", "roles": ["agent_author"]}]
    member_of(page, held, "clinic-b", [])
    page.goto("/")
    expect(page.locator("main h1")).to_have_text("Nothing to do here yet")
    expect(page.locator("main")).to_contain_text("Your roles in Clinic B grant nothing yet")
    expect(page.locator("#signed-in-name")).to_have_text("Nurse Kim · Clinic B")  # no dangling ·
    expect(page.locator("#org-form")).to_be_hidden()  # nowhere else to go


def test_a_role_is_named_never_shown_by_its_key(page: Page) -> None:
    """The list of a person's organizations names each role as the Access view does: whoever
    writes agents is an "Agent author", whatever the role's key is on the wire."""
    held = [
        {**CLINICS[0], "roles": ["agent_author"]},
        {**CLINICS[1], "roles": ["governance", "agent_author"]},
    ]
    member_of(page, held, None, [])
    page.goto("/")
    expect(page.locator("main h1")).to_have_text("Choose an organization")
    choices = page.locator(".org-choice")
    expect(choices.first).to_contain_text("default · Agent author")
    expect(choices.last).to_contain_text("clinic-b · Governance, Agent author")
    expect(page.locator("main")).not_to_contain_text("agent_author")


def test_the_bar_names_the_roles_of_who_is_signed_in(page: Page) -> None:
    held = [{**CLINICS[1], "roles": ["reviewer", "agent_author"]}]
    member_of(page, held, "clinic-b", ["reviewer", "agent_author"])
    open_agent(page, TRIAGE)
    expect(page.locator("#signed-in-name")).to_have_text(
        "Nurse Kim · Clinic B · Reviewer, Agent author"
    )


def test_a_member_who_only_labels_is_told_so_and_the_page_asks_for_nothing_else(
    page: Page,
) -> None:
    """A labeller and nothing else holds `label` only: no agents, no notice, no review queue.
    Where no grader's route is served (the open-mode test server here), they are told what they
    do, and the page reads nothing but who they are, health and their counts (which say whether
    the routes are served; tests/browser/test_written.py where they are)."""
    held = [{"org": "clinic-b", "name": "Clinic B", "roles": ["labeller"]}]
    seen = pins(page)
    member_of(page, held, "clinic-b", ["labeller"])
    page.goto("/")
    expect(page.locator("main h1")).to_have_text("Nothing to do here yet")
    expect(page.locator("main")).to_contain_text("You grade what agents write")
    for section in ("overview", "review", "try", "written"):
        expect(page.locator(f'.nav a[data-view="{section}"]')).to_be_hidden()
    asked = {"/api/health", "/api/written/labelling"}
    assert {path for _, path, _ in seen} <= asked


def test_roles_that_grant_nothing_still_offer_the_persons_other_organizations(
    page: Page,
) -> None:
    held = [{**CLINICS[0], "roles": ["agent_author"]}, CLINICS[1]]
    asked = member_of(page, held, "default", [])
    page.goto("/")
    expect(page.locator("main h1")).to_have_text("Nothing to do here yet")
    expect(page.locator("main")).to_contain_text("Your other organizations are in the top bar.")
    expect(page.locator("#org-select")).to_have_value("default")
    page.locator("#org-select").select_option("clinic-b")
    with page.expect_navigation():
        page.get_by_role("button", name="Switch").click()
    assert ("POST", "/api/auth/organization", {"org": "clinic-b"}) in asked


def test_someone_in_several_organizations_switches_between_them(page: Page) -> None:
    asked = member_of(page, CLINICS, "default", ["reviewer"])
    open_agent(page, TRIAGE)
    expect(page.locator("#signed-in-name")).to_have_text(
        "Nurse Kim · Reviewer"
    )  # the list names it
    picker = page.locator("#org-select")
    expect(picker).to_be_visible()
    expect(picker.locator("option")).to_have_text(["The owner's organization", "Clinic B"])
    expect(picker).to_have_value("default")
    picker.select_option("clinic-b")  # choosing in the list alone changes nothing (WCAG 3.2.2)
    assert ("POST", "/api/auth/organization", {"org": "clinic-b"}) not in asked
    with page.expect_navigation():
        page.get_by_role("button", name="Switch").click()
    assert ("POST", "/api/auth/organization", {"org": "clinic-b"}) in asked


def test_a_refused_switch_says_why_and_keeps_the_organization(page: Page) -> None:
    member_of(page, CLINICS, "default", ["reviewer"])
    refused = {
        "error": "AuthError",
        "detail": "you hold no membership of that organization in force",
    }
    page.route("**/api/auth/organization", lambda route: route.fulfill(status=403, json=refused))
    open_agent(page, TRIAGE)
    page.locator("#org-select").select_option("clinic-b")
    page.get_by_role("button", name="Switch").click()
    expect(page.locator("#toast")).to_have_text(refused["detail"])
    expect(page.locator("#org-select")).to_have_value("default")
    expect(page.locator("#signed-in-name")).to_have_text("Nurse Kim · Reviewer")


def test_switching_sends_a_held_override_first_in_the_organization_it_was_made_in(
    page: Page, live_server: str
) -> None:
    decision = decide_triage(live_server, "Crushing chest pain and sweating, before a switch")
    order: list[tuple[str, str | None]] = []

    def posted(request: Any) -> None:
        if request.method == "POST":
            order.append(
                (urllib.parse.urlsplit(request.url).path, request.headers.get("x-mendari-org"))
            )

    page.on("request", posted)
    member_of(page, CLINICS, "default", ["reviewer"])
    open_agent(page, TRIAGE, "review")
    card = page.locator(f'.review-card[data-id="{decision["id"]}"]')
    card.get_by_role("button", name="Billing", exact=True).click()  # held, in its undo window
    page.locator("#org-select").select_option("clinic-b")
    with page.expect_navigation():
        page.get_by_role("button", name="Switch").click()
    assert order[:2] == [
        (f"/api/review/{decision['id']}", "default"),
        ("/api/auth/organization", "default"),  # its own route ignores the organization named
    ], order


def pins(page: Page) -> list[tuple[str, str, str | None]]:
    """Every API request the page makes after it knows who is signed in: (method, path, the
    organization it names)."""
    seen: list[tuple[str, str, str | None]] = []
    known: list[bool] = []

    def record(request: Any) -> None:
        path = urllib.parse.urlsplit(request.url).path
        if path == "/api/auth/me":
            known.append(True)
        elif known and path.startswith("/api/"):
            seen.append((request.method, path, request.headers.get("x-mendari-org")))

    page.on("request", record)
    return seen


def decide_and_download(page: Page) -> None:
    """A decision (a POST), then the wrong cases' download, as the person would."""
    page.goto(f"/?agent={TRIAGE}#try")
    page.get_by_role("button", name="Chest pain").click()
    page.locator("#decide").click()
    expect(page.locator("#result")).to_contain_text("nurse")
    page.goto(f"/?agent={TRIAGE}#feedback")
    with page.expect_download() as saved:
        page.locator("#feedback-export").click()
    assert saved.value.suggested_filename == "triage-feedback-down.jsonl"


def test_every_request_names_the_organization_the_window_shows(page: Page) -> None:
    """Reads, writes and the download a link starts alike (it is fetched to carry the header)."""
    seen = pins(page)
    member_of(page, CLINICS, "default", ["reviewer"])
    open_agent(page, TRIAGE)
    decide_and_download(page)
    assert {"GET", "POST"} <= {method for method, _, _ in seen}
    assert f"/api/agents/{TRIAGE}/feedback/export" in {path for _, path, _ in seen}
    assert {org for _, _, org in seen} == {"default"}, seen


def test_claims_mode_sends_no_organization(page: Page) -> None:
    seen = pins(page)
    signed_in_as(page, "reviewer")
    open_agent(page, TRIAGE)
    expect(page.locator("#signed-in-name")).to_have_text("Nurse Kim · Reviewer")
    expect(page.locator(".org-picker")).to_be_hidden()
    decide_and_download(page)
    assert {"GET", "POST"} <= {method for method, _, _ in seen}
    assert {org for _, _, org in seen} == {None}


def test_a_download_for_an_organization_changed_since_is_refused_and_said(page: Page) -> None:
    member_of(page, CLINICS, "default", ["reviewer"])
    open_agent(page, TRIAGE)
    moved = {"error": "OrganizationChanged", "detail": "this window shows another organization"}
    page.route("**/feedback/export*", lambda route: route.fulfill(status=409, json=moved))
    page.goto(f"/?agent={TRIAGE}#feedback")
    page.locator("#feedback-export").click()
    expect(page.locator("#org-changed")).to_be_visible()


def test_who_is_signed_in_must_be_known_to_show_anything(page: Page) -> None:
    page.route("**/api/auth/me", lambda route: route.fulfill(status=503, json={"detail": "down"}))
    page.goto(f"/?agent={TRIAGE}#overview")
    expect(page.locator("main")).to_contain_text("Could not reach the Mendari API")
    expect(page.locator("#agent-select option")).to_have_count(0)


def test_a_launch_alongside_no_chosen_organization_offers_both(page: Page) -> None:
    """An EHR launch in a browser whose person holds memberships but has chosen none: the
    launch's patient, and choosing an organization."""
    member_of(page, CLINICS, None, [], launch=launched_as("L1"))
    page.goto("/#patient/L1")
    expect(page.locator(".patient-name")).to_have_text("Case 1 Synthetic")
    expect(page.locator(".nav a:visible")).to_have_text(["This patient", "Choose an organization"])
    page.locator('.nav a[data-view="access"]').click()
    expect(page.locator("main h1")).to_have_text("Choose an organization")
    page.goto("/#overview")  # nothing else
    expect(page.locator("main h1")).to_have_text("This patient")


@pytest.mark.parametrize(
    "held", [[], [{**CLINICS[1], "roles": ["agent_author"]}]], ids=["none", "nothing"]
)
def test_a_launch_alongside_no_access_shows_the_launch(
    page: Page, held: list[dict[str, Any]]
) -> None:
    """No organization, or roles that grant nothing: the launch's own view, alone."""
    org = held[0]["org"] if held else None
    seen = pins(page)
    member_of(page, held, org, [], launch=launched_as("L1"))
    page.goto("/#patient/L1")
    expect(page.locator(".patient-name")).to_have_text("Case 1 Synthetic")
    expect(page.locator(".nav a:visible")).to_have_text(["This patient"])
    outside = {path for _, path, _ in seen if not path.startswith("/api/smart/")}
    assert outside <= {"/api/health"}, outside


@pytest.mark.parametrize("width", [1024, 1280, 1440])
def test_nothing_shifts_while_a_member_of_several_loads(page: Page, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    page.add_init_script(LAYOUT_SHIFT)
    member_of(page, CLINICS, "default", ["reviewer"])
    page.goto(f"/?agent={TRIAGE}#overview")
    expect(page.locator(".stats-primary")).to_be_visible()
    page.wait_for_timeout(300)
    assert page.evaluate("window.__cls") < 0.1


def test_access_that_ended_mid_session_says_so(page: Page) -> None:
    member_of(page, CLINICS, "default", ["reviewer"])
    open_agent(page, TRIAGE)
    banner = page.locator("#access-ended")
    expect(banner).to_be_hidden()
    ended = {"error": "NoOrganization", "detail": "you have no organization here"}
    page.route("**/api/review?*", lambda route: route.fulfill(status=403, json=ended))
    page.goto(f"/?agent={TRIAGE}#review")
    expect(banner).to_be_visible()
    expect(banner).to_contain_text("Your access to this organization ended.")
    expect(page.locator("#announce-alert")).to_have_text("Your access to this organization ended.")


def test_a_window_on_an_organization_changed_since_says_so(page: Page) -> None:
    member_of(page, CLINICS, "default", ["reviewer"])
    open_agent(page, TRIAGE)
    moved = {"error": "OrganizationChanged", "detail": "this window shows another organization"}
    page.route("**/api/review?*", lambda route: route.fulfill(status=409, json=moved))
    page.goto(f"/?agent={TRIAGE}#review")
    banner = page.locator("#org-changed")
    expect(banner).to_be_visible()
    expect(banner.get_by_role("button", name="Reload it")).to_be_visible()


def test_use_another_account_signs_out_and_asks_which(page: Page) -> None:
    asked = member_of(page, [], None, [])
    page.route("**/auth/login*", lambda route: route.fulfill(status=200, body="signing in"))
    page.goto("/")
    page.get_by_role("button", name="Use another account").click()
    page.wait_for_url("**/auth/login?account=choose")
    assert ("POST", "/auth/logout", None) in asked


@pytest.mark.parametrize("axe_page", ["light", "dark"], indirect=True)
@pytest.mark.parametrize("held", [[], CLINICS], ids=["none", "choose"])
def test_the_access_page_passes_axe(axe_page: Page, held: list[dict[str, Any]]) -> None:
    member_of(axe_page, held, None, [])
    axe_page.goto("/")
    expect(axe_page.locator("main h1")).to_be_visible()
    assert violations(axe_page) == []


def test_the_access_page_fits_a_phone(page: Page) -> None:
    page.set_viewport_size(PHONE)
    member_of(page, [], None, [])
    page.goto("/")
    expect(page.locator("main h1")).to_have_text("You don't have access yet")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_the_switcher_is_in_the_phone_settings_sheet(page: Page) -> None:
    page.set_viewport_size(PHONE)
    member_of(page, CLINICS, "default", ["reviewer"])
    open_agent(page, TRIAGE)
    page.locator("#settings-button").click()
    expect(page.locator("#org-select")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
