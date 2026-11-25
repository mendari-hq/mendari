# SPDX-License-Identifier: Apache-2.0
"""Try it beyond `…/decide`: FHIR input (`…/fhir/decide`, agents with a FHIR mapping). It is
offered only where the agent takes it, says the server's refusals in words, asks for the notice
as `…/decide` does, and passes axe."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect

from mendari import notices
from mendari.server import api_docs
from tests.browser.conftest import CHART, TRIAGE
from tests.browser.test_accessibility import violations
from tests.browser.test_dashboard import AGENTS, open_agent, signed_in_as

pytestmark = pytest.mark.browser

FHIR_AGENTS = [CHART, TRIAGE]  # the served agents with a FHIR mapping (not the clinical review)


def fhir_example() -> dict[str, Any]:
    """The API reference's synthetic FHIR input (a patient's message, as a Communication). The
    reference shows it beside the bundled demo agent, which these tests' server does not serve,
    so it is read from where the reference takes it; the triage fixture maps the same
    Communication."""
    return copy.deepcopy(api_docs.FHIR_EXAMPLE)


def mode(page: Page, name: str) -> None:
    page.get_by_role("group", name="Edit the input as").get_by_role("button", name=name).click()


@pytest.mark.parametrize("agent_id", AGENTS)
def test_each_mode_is_offered_only_where_the_agent_takes_it(page: Page, agent_id: str) -> None:
    open_agent(page, agent_id, "try")
    expect(page.locator("#decide")).to_be_visible()
    fhir = page.get_by_role("group", name="Edit the input as").get_by_role("button", name="FHIR")
    expect(fhir).to_have_count(1 if agent_id in FHIR_AGENTS else 0)
    if agent_id == TRIAGE:
        page.get_by_role("button", name="Refill", exact=True).click()
    page.locator("#decide").click()
    expect(page.locator("#result-title")).to_be_focused()


def test_fhir_input_decides(page: Page) -> None:
    open_agent(page, TRIAGE, "try")
    mode(page, "FHIR")
    expect(page.locator("#body")).to_be_hidden()
    page.get_by_label("FHIR R4 JSON").fill(json.dumps(fhir_example()))
    page.locator("#decide").click()
    expect(page.locator("#result-title")).to_be_focused()
    expect(page.locator("#result")).to_contain_text("The FHIR Task for the work queue")
    page.reload()  # the choice is remembered
    expect(page.get_by_label("FHIR R4 JSON")).to_be_visible()


@pytest.mark.parametrize("agent_id", [CHART])
def test_fhir_errors_are_said_under_the_input(page: Page, agent_id: str) -> None:
    open_agent(page, agent_id, "try")
    mode(page, "FHIR")
    expect(page.locator("#record-form")).to_be_hidden()
    expect(page.locator("#example")).to_be_hidden()
    sent: list[str] = []
    page.on("request", lambda r: sent.append(r.url) if r.url.endswith("/fhir/decide") else None)
    box = page.get_by_label("FHIR R4 JSON")
    box.fill('{"resourceType": ')
    page.locator("#decide").click()
    error = page.locator("#fhir-error")
    expect(error).to_contain_text("Not valid JSON")
    expect(box).to_have_attribute("aria-invalid", "true")
    assert sent == []  # nothing was sent
    box.fill(json.dumps({"resourceType": "NotAResource"}))  # parses, but R4 has no such type
    page.locator("#decide").click()
    expect(error).to_contain_text("not an R4 resource type")  # the OperationOutcome's words
    assert len(sent) == 1
    expect(page.locator("#result")).to_be_empty()
    mode(page, "Form")  # back to the record, as it was
    expect(page.locator("#record-form [data-path]").first).to_be_visible()


def test_the_new_routes_ask_for_the_notice_like_decide(page: Page) -> None:
    """A notice version newer than the one acknowledged: the route refuses, the dashboard asks,
    then sends again (ui.js `api`)."""
    signed_in_as(page, "reviewer")
    state = {"acknowledged": True, "refused": 0}

    def notice(route: Route) -> None:
        if route.request.method == "POST":
            state["acknowledged"] = True
        body = {
            **notices.notice(),
            "acknowledgement": notices.ACKNOWLEDGEMENT,
            "required": True,
            "acknowledged": state["acknowledged"],
        }
        route.fulfill(json=body)

    def fhir_decide(route: Route) -> None:
        if not state["refused"]:
            state["refused"] += 1
            state["acknowledged"] = False
            route.fulfill(
                status=403,
                headers={"Mendari-Notice-Required": notices.VERSION},
                json={"error": "NoticeNotAcknowledged", "detail": "Acknowledge the notice."},
            )
        else:
            route.continue_()

    page.route("**/api/notice**", notice)
    page.route("**/fhir/decide", fhir_decide)
    open_agent(page, TRIAGE, "try")
    mode(page, "FHIR")
    page.get_by_label("FHIR R4 JSON").fill(json.dumps(fhir_example()))
    page.locator("#decide").click()
    asked = page.get_by_role("dialog", name="Before you start")
    asked.get_by_role("button", name="I understand: synthetic data only").click()
    expect(page.locator("#result-title")).to_be_focused()
    assert state["refused"] == 1


@pytest.mark.parametrize("axe_page", ["light", "dark"], indirect=True)
def test_the_new_states_pass_axe(axe_page: Page) -> None:
    page = axe_page
    open_agent(page, TRIAGE, "try")
    mode(page, "FHIR")
    page.get_by_label("FHIR R4 JSON").fill("{")
    page.locator("#decide").click()
    expect(page.locator("#fhir-error")).to_be_visible()
    found = {"fhir error": violations(page)}
    page.get_by_label("FHIR R4 JSON").fill(json.dumps(fhir_example()))
    page.locator("#decide").click()
    expect(page.locator("#result-title")).to_be_focused()
    found["fhir result"] = violations(page)
    assert {state: v for state, v in found.items() if v} == {}
