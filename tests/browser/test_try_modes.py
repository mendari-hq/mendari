# SPDX-License-Identifier: Apache-2.0
"""Try it beyond `…/decide`: an AI reply checked (`…/guard`, packs with a `guard` section), FHIR
input (`…/fhir/decide`, packs with a FHIR mapping) and the Da Vinci PAS request built under a
complete prior-authorization request (`…/pas`, packs whose decisions it takes: `pas_ready`). Each
is offered only where the pack takes it, says the server's refusals in words, and passes axe.

The X12 codes below are placeholders of an X12 code's shape, given as a site would give its own:
Mendari ships no X12 code value (tests/test_x12_values.py)."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

from mendari import notices
from mendari.standards import pas
from tests.browser.test_accessibility import violations
from tests.browser.test_dashboard import GUARD, INBOX, PACKS, open_pack, signed_in_as
from tests.test_pas import COVERAGE, INSURER, PATIENT, REQUESTER
from tests.test_prior_auth_pack import _order

pytestmark = pytest.mark.browser

PA = "mendari/prior-auth-preflight"
FHIR_PACKS = [p for p in PACKS if p != GUARD]  # every reference pack but AI Guard maps FHIR
BLOCKED = "a draft that misses an emergency is blocked"
SAFE = "a safe draft passes and waits for one-step approval"
CODES = {"service_type": "Z9", "request_type": "Z1", "certification_type": "Z2"}  # shapes only


def fhir_example(live_server: str) -> dict[str, Any]:
    """The API reference's synthetic FHIR input (a patient's message, for the inbox router)."""
    schema = httpx.get(f"{live_server}/openapi.json").json()
    operation = schema["paths"]["/api/packs/{namespace}/{name}/fhir/decide"]["post"]
    example: dict[str, Any] = operation["requestBody"]["content"]["application/json"]["examples"]
    return dict(example["message"]["value"])


def mode(page: Page, name: str) -> None:
    page.get_by_role("group", name="Edit the input as").get_by_role("button", name=name).click()


@pytest.mark.parametrize("pack_id", PACKS)
def test_each_mode_is_offered_only_where_the_pack_takes_it(page: Page, pack_id: str) -> None:
    open_pack(page, pack_id, "try")
    expect(page.locator("#decide")).to_be_visible()
    check = page.get_by_role("button", name="Check the AI reply")
    expect(check).to_have_count(1 if pack_id == GUARD else 0)
    fhir = page.get_by_role("group", name="Edit the input as").get_by_role("button", name="FHIR")
    expect(fhir).to_have_count(1 if pack_id in FHIR_PACKS else 0)
    if pack_id == INBOX:
        page.get_by_role("button", name="Refill", exact=True).click()
    page.locator("#decide").click()
    expect(page.locator("#result-title")).to_be_focused()
    # The first test of the prior-auth pack is a complete request: only there is PAS offered.
    expect(page.locator("#pas-title")).to_have_count(1 if pack_id == PA else 0)


def test_a_checked_reply_leads_with_what_to_do(page: Page) -> None:
    open_pack(page, GUARD, "try")
    page.select_option("#example", label=BLOCKED)
    page.get_by_role("button", name="Check the AI reply").click()
    title = page.locator("#result-title")
    expect(title).to_be_focused()
    expect(title).to_have_text(re.compile("^Blocked: send nothing"))
    expect(page.locator("#result .callout.danger")).to_contain_text("Instead:")
    page.select_option("#example", label=SAFE)
    page.get_by_role("button", name="Check the AI reply").click()
    expect(title).to_have_text(re.compile("^Held for review: send nothing yet"))
    expect(page.locator("#result")).to_contain_text("A clinician reads the draft")
    # The same result in words as a decision: the answers by name, and the record one link away.
    expect(page.locator("#result h3", has_text="Answers")).to_be_visible()
    expect(page.get_by_role("link", name=re.compile("^Engine detail"))).to_be_visible()


def test_a_released_reply_is_shown_to_send(page: Page) -> None:
    """The reference pack never releases (risk level 2 without an override): the screen is
    checked on the answer a release gives."""
    open_pack(page, GUARD, "try")

    def released(route: Route) -> None:
        answer = route.fetch().json()
        draft = json.loads(route.request.post_data or "{}")["draft"]
        answer.update(status="released", reply=draft)
        route.fulfill(json=answer)

    page.route("**/guard", released)
    draft = page.get_by_label("Draft").input_value()
    page.get_by_role("button", name="Check the AI reply").click()
    expect(page.locator("#result-title")).to_have_text(re.compile("^Released: send the reply"))
    expect(page.locator("#result .message")).to_have_text(draft)


def test_a_refused_check_is_said(page: Page) -> None:
    open_pack(page, GUARD, "try")
    page.route(
        "**/guard",
        lambda route: route.fulfill(
            status=503, json={"error": "ProviderError", "detail": "The safety screen is down."}
        ),
    )
    page.get_by_role("button", name="Check the AI reply").click()
    expect(page.locator("#announce-alert")).to_contain_text("The safety screen is down.")
    expect(page.get_by_role("button", name="Check the AI reply")).to_be_enabled()


def test_fhir_input_decides(page: Page, live_server: str) -> None:
    open_pack(page, INBOX, "try")
    mode(page, "FHIR")
    expect(page.locator("#body")).to_be_hidden()
    page.get_by_label("FHIR R4 JSON").fill(json.dumps(fhir_example(live_server)))
    page.locator("#decide").click()
    expect(page.locator("#result-title")).to_be_focused()
    expect(page.locator("#result")).to_contain_text("The FHIR Task for the work queue")
    page.reload()  # the choice is remembered
    expect(page.get_by_label("FHIR R4 JSON")).to_be_visible()


@pytest.mark.parametrize("pack_id", [PA, "mendari/referral-router"])
def test_fhir_errors_are_said_under_the_input(page: Page, pack_id: str) -> None:
    open_pack(page, pack_id, "try")
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


@pytest.fixture
def no_site_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """No X12 setting from the shell: the live server runs in this process and reads them."""
    for variable in pas.X12_SETTINGS.values():
        monkeypatch.delenv(variable, raising=False)


def complete_request(page: Page) -> None:
    open_pack(page, PA, "try")
    mode(page, "Form")  # whichever was chosen last
    page.select_option("#example", label="a complete lumbar MRI request is ready to submit")
    page.locator("#decide").click()
    expect(page.locator("#pas-title")).to_be_visible()
    for key, resource in {
        "patient": PATIENT,
        "coverage": COVERAGE,
        "insurer": INSURER,
        "requester": REQUESTER,
        "order": _order("mri", "FX101"),
    }.items():
        page.locator(f"#pas-{key}").fill(json.dumps(resource))
    page.get_by_label("Place of service").fill("22")


@pytest.mark.usefixtures("no_site_settings")
def test_the_pas_request_is_built_once_its_codes_are_given(page: Page) -> None:
    complete_request(page)
    page.get_by_role("button", name="Build the request").click()
    error = page.locator("#pas-error")
    expect(error).to_contain_text("MENDARI_PAS_REQUEST_TYPE")  # the server's words, plainly
    expect(page.locator("#pas-codes")).to_have_attribute("open", "")  # the fields that give them
    for key, value in CODES.items():
        page.locator(f"#pas-{key}").fill(value)
    page.get_by_role("button", name="Build the request").click()
    expect(error).to_be_hidden()
    expect(page.locator("#pas-bundle-title")).to_be_focused()
    bundle = json.loads(page.locator("#pas-bundle pre").inner_text())
    claim = bundle["entry"][0]["resource"]
    assert claim["use"] == "preauthorization"
    with page.expect_download() as saved:
        page.get_by_role("button", name="Download").click()
    assert saved.value.suggested_filename.startswith("pas-request-")


@pytest.mark.usefixtures("no_site_settings")
def test_pas_input_errors_are_said_before_anything_is_sent(page: Page) -> None:
    complete_request(page)
    sent: list[str] = []
    page.on("request", lambda r: sent.append(r.url) if r.url.endswith("/pas") else None)
    page.locator("#pas-coverage").fill("{not json")
    page.get_by_role("button", name="Build the request").click()
    expect(page.locator("#pas-error")).to_contain_text("Coverage: not valid JSON")
    expect(page.locator("#pas-coverage")).to_be_focused()
    assert sent == []


def test_a_pas_request_after_fhir_input_takes_its_resources(page: Page, live_server: str) -> None:
    """Only the dashboard's part: the prefill from what the FHIR input held (the decision is a
    real complete request's, answered here so the FHIR input need not make one)."""
    example = httpx.get(f"{live_server}/api/packs/{PA}/examples").json()[0]["input"]
    decided = httpx.post(f"{live_server}/api/packs/{PA}/decide", json=example).json()
    assert decided["recommendation"] == pas.READY
    open_pack(page, PA, "try")
    mode(page, "FHIR")
    page.route(
        "**/fhir/decide",
        lambda route: route.fulfill(json={"decision": decided, "task": {"resourceType": "Task"}}),
    )
    coverage = {**COVERAGE, "payor": [{"reference": f"Organization/{INSURER['id']}"}]}
    bundle = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{"resource": r} for r in (PATIENT, coverage, INSURER, _order("mri", "FX101"))],
    }
    page.get_by_label("FHIR R4 JSON").fill(json.dumps(bundle))
    page.locator("#decide").click()
    expect(page.locator("#pas-title")).to_be_visible()
    assert json.loads(page.locator("#pas-patient").input_value()) == PATIENT
    assert json.loads(page.locator("#pas-insurer").input_value()) == INSURER
    expect(page.locator("#pas-requester")).to_have_value("")  # the EHR's to give


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

    def guard(route: Route) -> None:
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
    page.route("**/guard", guard)
    open_pack(page, GUARD, "try")
    page.get_by_role("button", name="Check the AI reply").click()
    asked = page.get_by_role("dialog", name="Before you start")
    asked.get_by_role("button", name="I understand: synthetic data only").click()
    expect(page.locator("#result-title")).to_be_focused()
    assert state["refused"] == 1


@pytest.mark.parametrize("axe_page", ["light", "dark"], indirect=True)
@pytest.mark.usefixtures("no_site_settings")
def test_the_new_states_pass_axe(axe_page: Page, live_server: str) -> None:
    page = axe_page
    open_pack(page, GUARD, "try")
    page.select_option("#example", label=BLOCKED)
    page.get_by_role("button", name="Check the AI reply").click()
    expect(page.locator("#result-title")).to_be_focused()
    found = {"guard": violations(page)}
    open_pack(page, INBOX, "try")
    mode(page, "FHIR")
    page.get_by_label("FHIR R4 JSON").fill("{")
    page.locator("#decide").click()
    expect(page.locator("#fhir-error")).to_be_visible()
    found["fhir error"] = violations(page)
    page.get_by_label("FHIR R4 JSON").fill(json.dumps(fhir_example(live_server)))
    page.locator("#decide").click()
    expect(page.locator("#result-title")).to_be_focused()
    found["fhir result"] = violations(page)
    complete_request(page)
    page.get_by_role("button", name="Build the request").click()
    expect(page.locator("#pas-error")).to_be_visible()
    found["pas error"] = violations(page)
    for key, value in CODES.items():
        page.locator(f"#pas-{key}").fill(value)
    page.get_by_role("button", name="Build the request").click()
    expect(page.locator("#pas-bundle-title")).to_be_focused()
    found["pas bundle"] = violations(page)
    assert {state: v for state, v in found.items() if v} == {}
