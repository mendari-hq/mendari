# SPDX-License-Identifier: Apache-2.0
"""Approvals of agents pulled from a registry on the Decision Card (LP-18d, ADR-0012 §6): where
the agent came from, the approval in force and the earlier ones, approving through a dialog that
says what it allows and what it never lifts, revoking, and both offered only to people with
`approve`. The agent is served as the shared server serves it, said to come from a registry, and
/api/registry-approvals is answered here, so the shared server's approvals never change."""

from __future__ import annotations

import json
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect

from tests.browser.conftest import CLINICAL, TRIAGE
from tests.browser.test_dashboard import open_pack, signed_in_as

pytestmark = pytest.mark.browser

DIGEST = "sha256:" + "ab12" * 16
PULLED = {
    "format": "mendari.origin/1",
    "registry": "https://hub.example.org",
    "namespace": "mendari",
    "publisher_type": "community",
    "key_id": "key-7f3a",
    "archive_sha256": "c" * 64,
    "digest": DIGEST,
    "pulled_at": "2026-10-08T14:00:00+00:00",
}
APPROVAL = {
    "id": "0a1b2c3d4e5f6071",
    "org": "default",
    "pack_id": TRIAGE,
    "pack_digest": DIGEST,
    "registry": "https://hub.example.org",
    "approver": "dr-a",
    "reference": "AI committee minutes, item 4",
    "created_at": "2026-10-09T15:00:00+00:00",
    "revoked_at": None,
    "revoked_by": None,
}
EARLIER = {
    **APPROVAL,
    "id": "9f8e7d6c5b4a3921",
    "pack_digest": "sha256:" + "0" * 64,
    "reference": "minutes 3",
    "created_at": "2026-09-01T15:00:00+00:00",
    "revoked_at": "2026-09-20T15:00:00+00:00",
    "revoked_by": "dr-b",
}


def from_registry(page: Page, pack_id: str = TRIAGE) -> None:
    """The agent's detail as the server gives it, but from a registry, serving `DIGEST`."""

    def answer(route: Route) -> None:
        detail = route.fetch().json()
        detail.update(origin="hub", origin_label="From a registry", digest=DIGEST)
        route.fulfill(json=detail)

    page.route(f"**/api/agents/{pack_id}", answer)


def registry_approvals(
    page: Page,
    sent: list[tuple[str, str, Any]],
    *,
    active: dict[str, Any] | None = None,
    recalled: bool = False,
    post: tuple[int, dict[str, Any]] = (201, APPROVAL),
    delete: int = 204,
    pack_id: str = TRIAGE,
) -> None:
    listed = [row for row in (active, EARLIER) if row]

    def answer(route: Route) -> None:
        request = route.request
        body = json.loads(request.post_data) if request.post_data else None
        sent.append((request.method, request.url, body))
        if request.method == "GET":
            route.fulfill(
                json={
                    "pack_id": pack_id,
                    "digest": DIGEST,
                    "origin": "hub",
                    "pulled": PULLED,
                    "recalled": recalled,
                    "active": active,
                    "approvals": listed,
                }
            )
        elif request.method == "POST":
            route.fulfill(status=post[0], json=post[1])
        elif delete == 204:
            route.fulfill(status=204)
        else:
            route.fulfill(status=delete, json={"detail": "no such approval"})

    page.route("**/api/registry-approvals**", answer)


def test_an_unapproved_pack_says_so_and_is_approved_through_the_dialog(page: Page) -> None:
    sent: list[tuple[str, str, Any]] = []
    from_registry(page)
    registry_approvals(page, sent)
    open_pack(page, TRIAGE, "card")
    assert any(m == "GET" and "pack_id=mendari-test%2Ftriage" in u for m, u, _ in sent)
    expect(page.locator("#card-facts")).to_contain_text(
        "Not without an approval: this agent came from a registry"
    )
    section = page.locator("#registry-approval")
    expect(section).to_contain_text("https://hub.example.org")
    expect(section).to_contain_text("key-7f3a")
    expect(section).to_contain_text(DIGEST)
    expect(section).to_contain_text("Not approved here.")
    history = section.locator("tbody tr")
    expect(history).to_have_count(1)
    expect(history.first).to_contain_text("Revoked by dr-b")
    # Risk level 1: no approval for its risk level, and nothing claims none is needed at all.
    expect(page.locator("#card-approval")).to_contain_text("besides the registry approval above")
    expect(page.locator("#card-approval")).not_to_contain_text("No approval is needed")

    section.get_by_label("Reference").fill("AI committee minutes, item 4")
    section.get_by_label("Approver").fill("dr-a")
    section.get_by_role("button", name="Approve this version…").click()
    dialog = page.get_by_role("dialog", name="Approve this agent from a registry?")
    expect(dialog).to_contain_text(DIGEST)
    expect(dialog).to_contain_text("from https://hub.example.org")
    expect(dialog).to_contain_text("It does not lift a recall by its registry, an automation hold")
    assert not any(m == "POST" for m, _, _ in sent)  # nothing is sent before the dialog's answer
    dialog.get_by_role("button", name="Approve", exact=True).click()
    expect(page.locator("#toast")).to_have_text("Approved for this version")
    posted = [body for m, _, body in sent if m == "POST"]
    assert posted == [
        {
            "pack_id": TRIAGE,
            "pack_digest": DIGEST,
            "reference": "AI committee minutes, item 4",
            "approver": "dr-a",
        }
    ]


def test_cancelling_the_dialog_sends_nothing(page: Page) -> None:
    sent: list[tuple[str, str, Any]] = []
    from_registry(page)
    registry_approvals(page, sent)
    open_pack(page, TRIAGE, "card")
    section = page.locator("#registry-approval")
    section.get_by_label("Reference").fill("minutes 5")
    section.get_by_label("Approver").fill("dr-a")
    section.get_by_role("button", name="Approve this version…").click()
    page.get_by_role("dialog").get_by_role("button", name="Cancel").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert not any(m == "POST" for m, _, _ in sent)


def test_a_refused_approval_is_said_in_the_dialog(page: Page) -> None:
    sent: list[tuple[str, str, Any]] = []
    from_registry(page)
    refused = (409, {"detail": "this digest is approved already: revoke that approval first"})
    registry_approvals(page, sent, post=refused)
    open_pack(page, TRIAGE, "card")
    section = page.locator("#registry-approval")
    section.get_by_label("Reference").fill("minutes 5")
    section.get_by_label("Approver").fill("dr-a")
    section.get_by_role("button", name="Approve this version…").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_role("button", name="Approve", exact=True).click()
    expect(dialog.get_by_role("alert")).to_have_text(
        "this digest is approved already: revoke that approval first"
    )
    expect(dialog).to_be_visible()  # left open, to cancel or try again


def test_an_approval_in_force_is_shown_and_revoked(page: Page) -> None:
    sent: list[tuple[str, str, Any]] = []
    from_registry(page)
    registry_approvals(page, sent, active=APPROVAL)
    open_pack(page, TRIAGE, "card")
    active = page.locator("#registry-active")
    expect(active).to_contain_text("Approved for automation by dr-a (AI committee minutes, item 4)")
    expect(active).to_contain_text("ab12ab12ab12")
    expect(page.locator("#registry-form")).to_have_count(0)  # one approval a digest
    expect(page.locator("#registry-approval tbody tr").first).to_contain_text("In force")
    expect(page.locator("#card-facts")).not_to_contain_text("Not without an approval")
    active.get_by_role("button", name="Revoke").click()
    expect(page.locator("#toast")).to_have_text("Approval revoked: a person reviews again")
    assert any(
        m == "DELETE" and u.endswith(f"/api/registry-approvals/{APPROVAL['id']}")
        for m, u, _ in sent
    )


def test_a_refused_revocation_is_said(page: Page) -> None:
    from_registry(page)
    registry_approvals(page, [], active=APPROVAL, delete=404)
    open_pack(page, TRIAGE, "card")
    page.locator("#registry-active").get_by_role("button", name="Revoke").click()
    expect(page.locator("#toast")).to_have_text("no such approval")


def test_a_recall_is_said_and_holds_even_an_approved_pack(page: Page) -> None:
    from_registry(page)
    registry_approvals(page, [], active=APPROVAL, recalled=True)
    open_pack(page, TRIAGE, "card")
    expect(page.locator("#registry-approval .callout.danger")).to_contain_text(
        "Recalled by its registry"
    )
    expect(page.locator("#card-facts")).to_contain_text("Not while its registry's recall stands")


def test_only_people_who_may_approve_are_offered_approving_and_revoking(page: Page) -> None:
    signed_in_as(page, "reviewer")
    from_registry(page)
    registry_approvals(page, [], active=APPROVAL)
    open_pack(page, TRIAGE, "card")
    expect(page.locator("#registry-active")).to_contain_text("Approved for automation by dr-a")
    expect(page.locator("#registry-active button")).to_have_count(0)
    registry_approvals(page, [])
    open_pack(page, TRIAGE, "card")
    expect(page.locator("#registry-approval")).to_contain_text("Not approved here.")
    expect(page.locator("#registry-form")).to_have_count(0)
    expect(page.locator("#registry-approval tbody tr")).to_have_count(1)  # still listed


def test_a_signed_in_approver_is_recorded_by_the_server_not_typed(page: Page) -> None:
    sent: list[tuple[str, str, Any]] = []
    signed_in_as(page, "admin")
    from_registry(page)
    registry_approvals(page, sent)
    open_pack(page, TRIAGE, "card")
    section = page.locator("#registry-approval")
    expect(section.get_by_label("Approver")).to_have_count(0)
    section.get_by_label("Reference").fill("minutes 6")
    section.get_by_role("button", name="Approve this version…").click()
    page.get_by_role("dialog").get_by_role("button", name="Approve", exact=True).click()
    expect(page.locator("#toast")).to_have_text("Approved for this version")
    assert [body["approver"] for m, _, body in sent if m == "POST"] == [None]


def test_a_risk_level_2_pack_shows_both_approvals(page: Page) -> None:
    from_registry(page, CLINICAL)
    registry_approvals(page, [], pack_id=CLINICAL)
    open_pack(page, CLINICAL, "card")
    section = page.locator("#card-approval")
    expect(section.get_by_role("heading", name="Agent from a registry")).to_be_visible()
    expect(section.get_by_role("heading", name="Approval for risk level 2")).to_be_visible()


def test_a_pack_not_from_a_registry_never_asks(page: Page) -> None:
    asked: list[str] = []
    page.on("request", lambda r: asked.append(r.url) if "registry-approvals" in r.url else None)
    open_pack(page, TRIAGE, "card")
    expect(page.locator("#card-approval")).to_contain_text("No approval is needed.")
    expect(page.locator("#registry-approval")).to_have_count(0)
    assert asked == []
