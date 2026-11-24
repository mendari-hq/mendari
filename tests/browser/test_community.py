# SPDX-License-Identifier: Apache-2.0
"""Community packs in a real browser (plan B3): the picker groups packs by where they come from
and opens on a maintained one, and every view that shows a community pack says "Community · not
clinically reviewed", readably in both themes. Run with `uv run pytest -m browser`."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, expect

from mendari.cli import main
from mendari.core.registry import SearchPath
from mendari.server.app import create_app
from tests.browser.conftest import serving
from tests.browser.test_dashboard import CONTRAST, PHONE, show
from tests.conftest import INBOX_DIR, ROOT

pytestmark = pytest.mark.browser

LABEL = "Community · not clinically reviewed"
SORTER = "community/visit-sorter"
SCAFFOLD = "community/refill-router"  # what `mendari new --community` writes
FIRST_MAINTAINED = "mendari/ai-guard"  # by id; every community/ id sorts before it


@pytest.fixture(scope="module")
def community_site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """The shipped packs and two community packs (the test fixture, and the scaffold as the
    guide makes it), as a deployment with MENDARI_COMMUNITY_AGENTS=on serves them."""
    tmp = Path(tmp_path_factory.mktemp("community"))
    base = tmp / "community-agents"
    fixture = ROOT / "tests" / "fixtures" / "community-agents" / "visit-sorter"
    shutil.copytree(fixture, base / "visit-sorter")
    scaffold = ["new", str(base / "refill-router"), "--community", "--publisher", "Test Clinic"]
    assert main(scaffold) == 0
    app = create_app(
        db_path=tmp / "c.db", packs_paths=[INBOX_DIR.parent, SearchPath(base, "community")]
    )
    with serving(app) as port:
        yield f"http://127.0.0.1:{port}"


@pytest.fixture
def site(browser: Browser, community_site: str) -> Iterator[Page]:
    context = browser.new_context(base_url=community_site)
    yield context.new_page()
    context.close()


def _open(page: Page, pack_id: str, view: str) -> None:
    page.add_init_script(f"localStorage.setItem('oh.pack', {json.dumps(pack_id)})")
    show(page, view)


def test_the_picker_groups_packs_by_origin_and_opens_on_a_maintained_one(site: Page) -> None:
    show(site, "overview")
    select = site.locator("#pack-select")
    expect(select).to_have_value(FIRST_MAINTAINED)
    groups = select.locator("optgroup")
    expect(groups).to_have_count(2)
    assert [groups.nth(i).get_attribute("label") for i in range(2)] == ["Maintained", LABEL]
    options = groups.nth(1).locator("option")
    expect(options).to_have_count(2)
    assert [options.nth(i).get_attribute("value") for i in range(2)] == [SCAFFOLD, SORTER]
    expect(options.nth(1)).to_have_text("Visit Sorter (test fixture) 0.1.0 (community)")
    expect(site.locator(".pack-header .badge.origin")).to_have_text("Maintained by Mendari")
    expect(site.locator(".badge.origin-community")).to_have_count(0)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_a_community_pack_is_labelled_wherever_it_shows_and_readably(
    site: Page, scheme: str
) -> None:
    site.emulate_media(color_scheme=scheme)  # type: ignore[arg-type]
    _open(site, SORTER, "overview")
    expect(site.locator(".pack-header .badge.origin-community")).to_have_text(LABEL)
    pairs = site.evaluate(CONTRAST, ".badge.origin-community")

    show(site, "card")
    expect(site.locator("main .community-notice")).to_contain_text(LABEL)
    expect(site.locator("#card-facts .badge.origin-community")).to_have_text(LABEL)
    expect(site.locator("#card-facts")).to_contain_text("Never: a community agent recommends")
    expect(site.locator("#card-facts")).to_contain_text("(community, self-declared)")
    expect(site.locator("#card-approval")).to_contain_text("A community agent is never automated")
    pairs += site.evaluate(CONTRAST, "main .community-notice, main .badge.origin-community")

    show(site, "try")
    expect(site.locator("main .community-notice")).to_contain_text(LABEL)
    pairs += site.evaluate(CONTRAST, "main .community-notice")

    _open(site, FIRST_MAINTAINED, "card")
    expect(site.locator("#card-facts .badge.origin")).to_have_text("Maintained by Mendari")
    expect(site.locator("main .community-notice")).to_have_count(0)
    pairs += site.evaluate(CONTRAST, "main .badge.origin")

    assert len(pairs) >= 5
    failing = [(text[:40], round(ratio, 2)) for text, ratio in pairs if ratio < 4.5]
    assert failing == [], scheme


def test_the_docs_landing_labels_the_community_pack(site: Page) -> None:
    site.goto("/docs")
    item = site.locator(".docs-cards li", has=site.locator(f'a[href="/?agent={SORTER}#card"]'))
    expect(item.locator(".badge.origin-community")).to_have_text(LABEL)
    expect(site.locator(".docs-cards .badge.origin-community")).to_have_count(2)


def test_the_labels_fit_a_phone(site: Page) -> None:
    site.set_viewport_size(PHONE)
    _open(site, SORTER, "overview")
    for view in ("overview", "card", "try"):
        show(site, view)
        shown = site.locator("main .badge.origin-community, main .community-notice")
        expect(shown.first).to_be_visible()
        assert site.evaluate("document.documentElement.scrollWidth") <= PHONE["width"], view


def test_the_review_queue_audit_and_monitoring_label_a_community_pack(site: Page) -> None:
    """Where a person confirms a recommendation, the queue says the pack is not clinically
    reviewed: before, only the overview, card and Try it did."""
    response = site.request.post(f"/api/agents/{SORTER}/decide", data={"message": "My bill?"})
    assert response.ok, response.text()
    _open(site, SORTER, "review")
    expect(site.locator("main .community-notice")).to_contain_text(LABEL)
    card = site.locator(".review-card").first
    expect(card.locator(".badge.origin-community")).to_have_text(LABEL)
    for view in ("audit", "monitoring"):
        show(site, view)
        expect(site.locator("main .community-notice")).to_contain_text(LABEL)
    _open(site, FIRST_MAINTAINED, "review")
    expect(site.locator("main .community-notice, main .badge.origin-community")).to_have_count(0)


def _launched(page: Page) -> None:
    """An EHR launch, as the server would answer it (the launch itself: tests/browser/
    test_launch.py): the patient view's requests, answered here."""
    session = {"active": True, "launch_id": "abc123", "patient": "p1", "packs": []}
    patient = {"name": "Test Synthetic", "gender": "female", "birthDate": "1980-01-01"}
    item = {"ref": "Communication/c1", "title": "A message", "text": "A question about my bill."}
    decision = {"id": "d1", "outcome": "HUMAN_REVIEW", "recommendation": "billing", "reasons": []}
    page.route("**/api/smart/session", lambda route: route.fulfill(json=session))
    page.route("**/api/smart/patient", lambda route: route.fulfill(json=patient))
    page.route("**/api/smart/focus?*", lambda route: route.fulfill(json=[item]))
    page.route("**/api/smart/decide", lambda route: route.fulfill(json={"decision": decision}))


def test_the_patient_view_labels_a_community_pack(site: Page) -> None:
    """The SMART view, where a clinician decides from the EHR, says so too."""
    _launched(site)
    site.add_init_script(f"localStorage.setItem('oh.pack', {json.dumps(SORTER)})")
    site.goto("/#patient/abc123")
    expect(site.locator(".patient-banner")).to_be_visible()
    expect(site.locator("main .community-notice")).to_contain_text(LABEL)
    site.locator("button[data-decide]").click()
    expect(site.locator(".decided-line .badge.origin-community")).to_have_text(LABEL)


def test_the_patient_view_of_a_maintained_pack_has_no_community_label(site: Page) -> None:
    _launched(site)
    site.add_init_script(f"localStorage.setItem('oh.pack', {json.dumps(FIRST_MAINTAINED)})")
    site.goto("/#patient/abc123")
    expect(site.locator(".patient-banner")).to_be_visible()
    site.locator("button[data-decide]").click()
    expect(site.locator(".decided-line")).to_be_visible()
    expect(site.locator("main .community-notice, main .badge.origin-community")).to_have_count(0)


def test_try_it_decides_on_a_pack_made_with_the_community_scaffold(site: Page) -> None:
    """The scaffold's schema takes a message body and subject only: before, Try it also sent a
    channel and a patient, and every decision was blocked ("input rejected")."""
    _open(site, SCAFFOLD, "try")
    expect(site.locator("#channel, #language, #age")).to_have_count(0)
    site.get_by_role("button", name="Chest pain").click()
    site.locator("#decide").click()
    expect(site.locator("#result-title")).to_contain_text("Sent to review")
    head = site.locator(".result .result-head")
    # A recommendation, by name (as shown: the code beside it is hidden): the input was read.
    expect(head).to_contain_text("Request:", use_inner_text=True)
    expect(head).not_to_contain_text("Blocked")
    expect(site.locator(".result")).not_to_contain_text("The input was refused")


def test_try_it_still_sends_the_inbox_routers_channel_and_patient(site: Page) -> None:
    _open(site, "mendari/patient-inbox-router", "try")
    for field in ("#subject", "#channel", "#language", "#age"):
        expect(site.locator(field)).to_have_count(1)
    with site.expect_request("**/decide") as sent:
        site.get_by_role("button", name="Bill (es)").click()
        site.locator("#decide").click()
    body = json.loads(sent.value.post_data or "{}")
    assert body["message"]["channel"] == "sms" and body["patient"] == {"preferred_language": "es"}
