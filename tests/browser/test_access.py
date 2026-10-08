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

from tests.browser.test_accessibility import violations
from tests.browser.test_dashboard import INBOX, PERMISSIONS, PHONE, open_pack, signed_in_as

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
    page: Page, memberships: list[dict[str, Any]], org: str | None, roles: list[str]
) -> list[tuple[str, str, Any]]:
    """`/api/auth/me` as a memberships-mode server answers it. A person of no organization is
    refused everything else as that server would (403 `NoOrganization`), but choosing one and
    asking to join; one with an organization reaches the test server. Returns each API call
    as (method, path, the JSON body sent)."""
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
    page.goto(f"/?pack={INBOX}#review")  # whatever the address says
    expect(page.locator("main h1")).to_have_text("You don't have access yet")
    expect(page.locator("main")).to_contain_text("Signed in as kim@clinic.example.org")
    expect(page.locator("#signed-in-name")).to_have_text("Nurse Kim · no organization")
    expect(page.locator(".nav a:visible")).to_have_count(1)  # the API docs only
    expect(page.locator(".pack-picker")).to_be_hidden()
    expect(page.locator(".org-picker")).to_be_hidden()
    paths = {path for _, path, _ in asked}
    assert paths <= {"/api/health", "/api/smart/session", "/api/auth/me"}, paths
    page.goto(f"/?pack={INBOX}#access")
    expect(page.locator("main h1")).to_have_text("You don't have access yet")


def test_asking_to_join_sends_the_id_and_says_the_same_whatever_happens(page: Page) -> None:
    asked = member_of(page, [], None, [])
    page.goto("/")
    page.fill("#ask-org", "clinic-a")
    page.get_by_role("button", name="Ask to join").click()
    expect(page.locator("#toast")).to_have_text(
        "If that organization exists, its administrators will see your request."
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
    member_of(
        page, [{"org": "clinic-b", "name": "Clinic B", "roles": ["labeller"]}], "clinic-b", []
    )
    page.goto("/")
    expect(page.locator("main h1")).to_have_text("Nothing to do here yet")
    expect(page.locator("main")).to_contain_text("Your roles in Clinic B grant nothing yet")


def test_someone_in_several_organizations_switches_between_them(page: Page) -> None:
    asked = member_of(page, CLINICS, "default", ["reviewer"])
    open_pack(page, INBOX)
    expect(page.locator("#signed-in-name")).to_have_text(
        "Nurse Kim · The owner's organization · reviewer"
    )
    picker = page.locator("#org-select")
    expect(picker).to_be_visible()
    expect(picker.locator("option")).to_have_text(["The owner's organization", "Clinic B"])
    expect(picker).to_have_value("default")
    with page.expect_navigation():
        picker.select_option("clinic-b")
    assert ("POST", "/api/auth/organization", {"org": "clinic-b"}) in asked


def test_every_request_names_the_organization_the_window_shows(page: Page) -> None:
    pinned: list[str | None] = []
    page.on(
        "request",
        lambda request: (
            pinned.append(request.headers.get("x-mendari-org"))
            if "/api/packs" in request.url
            else None
        ),
    )
    member_of(page, CLINICS, "default", ["reviewer"])
    open_pack(page, INBOX)
    assert pinned and set(pinned) == {"default"}


def test_claims_mode_sends_no_organization(page: Page) -> None:
    sent: list[dict[str, str]] = []
    page.on(
        "request", lambda request: sent.append(request.headers) if "/api/" in request.url else None
    )
    signed_in_as(page, "reviewer")
    open_pack(page, INBOX)
    expect(page.locator("#signed-in-name")).to_have_text("Nurse Kim · reviewer")
    expect(page.locator(".org-picker")).to_be_hidden()
    assert sent and all("x-mendari-org" not in headers for headers in sent)


def test_access_that_ended_mid_session_says_so(page: Page) -> None:
    member_of(page, CLINICS, "default", ["reviewer"])
    open_pack(page, INBOX)
    banner = page.locator("#access-ended")
    expect(banner).to_be_hidden()
    ended = {"error": "NoOrganization", "detail": "you have no organization here"}
    page.route("**/api/review?*", lambda route: route.fulfill(status=403, json=ended))
    page.goto(f"/?pack={INBOX}#review")
    expect(banner).to_be_visible()
    expect(banner).to_contain_text("Your access to this organization ended.")
    expect(page.locator("#announce-alert")).to_have_text("Your access to this organization ended.")


def test_a_window_on_an_organization_changed_since_says_so(page: Page) -> None:
    member_of(page, CLINICS, "default", ["reviewer"])
    open_pack(page, INBOX)
    moved = {"error": "OrganizationChanged", "detail": "this window shows another organization"}
    page.route("**/api/review?*", lambda route: route.fulfill(status=409, json=moved))
    page.goto(f"/?pack={INBOX}#review")
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
    open_pack(page, INBOX)
    page.locator("#settings-button").click()
    expect(page.locator("#org-select")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
