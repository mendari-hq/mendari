# SPDX-License-Identifier: Apache-2.0
"""An agent's connection in the console (`dashboard/connection.js`, ADR-0021): on the agent's
Decision Card, and in a dialog from the Agents view.

An administrator connects the service that answers for an agent, checks it with a synthetic
example, changes it and clears it; the token is typed once and never comes back to the page; a
refused address is explained in the form; a reader sees where it leads and no control; a
deployment that cannot keep a token says so and turns the field off; with the feature switched
off the card says who sets connections. Reachable by keyboard, without an axe violation in
either theme, and without scrolling sideways on a phone.

A real server at `localhost` in memberships mode that serves the bundled agent (it asks a
service of its own), and a real agent service on this machine (tests/test_hub_routes.py). Run
with `uv run pytest -m browser`."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from mendari.providers import reach
from mendari.server.app import create_app
from tests.browser.access_site import Site, dialog, memberships, person, running
from tests.browser.conftest import serving
from tests.browser.test_accessibility import violations
from tests.browser.test_dashboard import PHONE
from tests.conftest import DEMO_AGENT_DIR
from tests.test_agent_connections import Outside
from tests.test_hub_routes import Service, service  # noqa: F401 - the local agent service

pytestmark = pytest.mark.browser

CLINIC_A = "clinic-a"
PORTAL = "mendari/portal-message-agent"
INBOX = "mendari/patient-inbox-router"
TOKEN = "zqTOKEN-browser-9d2e7a41-never-shown"
OWN = {"x-mendari-request": "1"}
AT = f"/api/connections/{PORTAL}"


@pytest.fixture
def site(
    browser: Browser,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    service: Service,  # noqa: F811
) -> Iterator[Site]:
    """The console's server, which may call the local agent service and nothing else."""
    memberships(monkeypatch)
    monkeypatch.setenv("MENDARI_AGENTS_PATH", str(DEMO_AGENT_DIR.parent))
    monkeypatch.setenv("MENDARI_PROVIDER_ALLOWED_ORIGINS", service.origin)
    with running(browser, tmp_path / "a.db") as here:
        yield here


def headers(site: Site, sub: str, *roles: str) -> dict[str, str]:
    found = person(site, sub=sub, email=f"{sub}@example.org", org=CLINIC_A, roles=list(roles))
    return {**found, **OWN}


def window(site: Site, sub: str, *roles: str, **options: Any) -> Page:
    """A browser of `sub`'s, a member of Clinic-A with `roles` (axe may be injected in it), on
    the agent that asks a service."""
    found = person(site, sub=sub, email=f"{sub}@example.org", org=CLINIC_A, roles=list(roles))
    page = site.window(found, **{"bypass_csp": True, **options})
    page.add_init_script(f"localStorage.setItem('oh.pack', '{PORTAL}')")
    return page


def card(page: Page) -> Locator:
    """The Decision Card's Connection section, read."""
    page.goto("about:blank")
    page.goto("/#card")
    section = page.locator("#card-connection")
    expect(section.get_by_role("heading", name="Connection", exact=True)).to_be_visible()
    expect(section.locator("[data-connection], #card-connection-body > p.prose")).to_be_visible()
    return section


def connect(section: Locator, address: str, token: str | None = None) -> None:
    section.get_by_label("Address of the service").fill(address)
    if token is not None:
        section.locator("#connection-token").fill(token)
    section.get_by_role("button", name="Save connection").click()


def test_an_administrator_connects_checks_changes_and_clears_an_agents_service(
    site: Site,
    service: Service,  # noqa: F811
) -> None:
    admin = window(site, "lead", "admin")
    section = card(admin)
    expect(section).to_contain_text("Not connected.")
    expect(section).to_contain_text("a person decides every case")
    expect(section).to_contain_text(f"This deployment may call: {service.origin}")
    token = section.locator("#connection-token")
    expect(token).to_have_attribute("type", "password")
    expect(token).to_have_attribute("autocomplete", "new-password")
    assert violations(admin) == []

    connect(section, service.url, TOKEN)
    expect(section).to_contain_text(f"Connected to {service.url}.")
    expect(section).to_contain_text(
        "by lead@example.org. It is kept sealed and is never shown again."
    )
    expect(section.locator("#connection-token")).to_have_value("")  # typed once, never back
    expect(section.get_by_label("Address of the service")).to_have_value(service.url)
    assert TOKEN not in admin.content()
    assert violations(admin) == []

    section.get_by_role("button", name="Check connection").click()
    result = section.locator("[data-connection-result]")
    expect(result).to_contain_text("The connection works.", timeout=30_000)
    expect(result).to_contain_text("a refill request goes to the refill queue")
    expect(result).to_contain_text("model local-test-agent")
    assert service.authorizations() == [f"Bearer {TOKEN}"]
    assert violations(admin) == []

    # The service turns the token away: said in the page's words, never the service's.
    service.status = 401
    section.get_by_role("button", name="Check connection").click()
    expect(result).to_contain_text("The connection does not work yet.", timeout=30_000)
    expect(result).to_contain_text("the token is missing, wrong or expired")
    expect(result).not_to_contain_text(Service.SAID)
    service.status = 200

    # Saved again with the token left empty: the one that is set stays.
    connect(section, service.url)
    expect(section).to_contain_text("It is kept sealed and is never shown again.")
    section.get_by_role("button", name="Check connection").click()
    expect(result).to_contain_text("The connection works.", timeout=30_000)
    assert service.authorizations()[-1] == f"Bearer {TOKEN}"

    section.get_by_role("button", name="Clear…").click()
    asked = dialog(admin)
    expect(asked).to_contain_text("The address and the token are removed.")
    asked.get_by_role("button", name="Clear", exact=True).click()
    expect(section).to_contain_text("Not connected.")
    expect(section.get_by_role("button", name="Clear…")).to_have_count(0)
    with httpx.Client(base_url=site.base, headers=headers(site, "lead", "admin")) as client:
        assert client.get(AT).json()["connection"] is None
        listed = client.get("/api/audit/administrative").json()["items"]
    # Saved once (the second save changed nothing), then cleared.
    assert [item["event"] for item in listed if item["event"].startswith("agent_connection")] == [
        "agent_connection.set",
        "agent_connection.cleared",
    ]
    assert TOKEN not in str(listed)


def test_a_refused_address_is_explained_in_the_form_and_nothing_is_saved(site: Site) -> None:
    admin = window(site, "lead", "admin")
    section = card(admin)
    error = section.locator("form .error")
    section.get_by_role("button", name="Save connection").click()
    expect(error).to_have_text("Type the address of the service, starting with https://")
    expect(section.get_by_label("Address of the service")).to_be_focused()
    for address, why in (
        ("http://example.org/decide", "an address starts with https://"),
        # Off the list: an administrator who does not operate the deployment is told who can
        # allow the address.
        (
            "https://example.org/decide",
            "is not on the list of addresses this deployment may call. An administrator of "
            "the organization that operates this deployment can allow it for this agent",
        ),
        ("https://someone:pw@example.org/decide", "no user or password"),
        ("https://example.org/decide?key=zq", "no query"),
    ):
        connect(section, address, TOKEN)
        expect(error).to_be_visible()
        expect(error).to_contain_text(why)
        expect(error).not_to_contain_text(TOKEN)
    # They are offered no box to allow it themselves.
    expect(section.locator("[data-connection-allow]")).to_have_count(0)
    expect(section).to_contain_text(
        "Another address is allowed by an administrator of the organization that operates"
    )
    assert violations(admin) == []
    expect(section).to_contain_text("Not connected.")
    with httpx.Client(base_url=site.base, headers=headers(site, "lead", "admin")) as client:
        assert client.get(AT).json()["connection"] is None


def test_a_reader_sees_where_it_leads_and_no_control(
    site: Site,
    service: Service,  # noqa: F811
) -> None:
    with httpx.Client(base_url=site.base, headers=headers(site, "lead", "admin")) as client:
        made = client.put(AT, json={"endpoint": service.url, "token": TOKEN})
        assert made.status_code == 200, made.text
    viewer = window(site, "vera", "viewer")
    section = card(viewer)
    expect(section).to_contain_text(f"Connected to {service.origin}.")
    expect(section).not_to_contain_text("/decide")  # where it leads, never the path
    expect(section).to_contain_text("An administrator of your organization sets or changes it.")
    expect(section.locator("form, button")).to_have_count(0)
    assert TOKEN not in viewer.content()
    assert violations(viewer) == []
    # The Agents view says the same of it.
    viewer.goto("/#agents")
    served = viewer.locator(f'article[data-agent="{PORTAL}"]')
    expect(served).to_contain_text("Connected")
    expect(served).to_contain_text(f"to {service.origin}, set")
    expect(served).to_contain_text("by lead@example.org")
    # An agent that decides on this server has nothing to connect, and says so.
    viewer.evaluate(f"localStorage.setItem('oh.pack', '{INBOX}')")
    viewer.add_init_script(f"localStorage.setItem('oh.pack', '{INBOX}')")
    local = card(viewer)
    expect(local).to_contain_text("This agent decides on this server")
    expect(local.locator("form, button")).to_have_count(0)


def test_the_connection_opens_from_the_agents_view_and_works_from_the_keyboard(
    site: Site,
    service: Service,  # noqa: F811
) -> None:
    admin = window(site, "lead", "admin")
    admin.goto("/#agents")
    opener = admin.locator(f'article[data-agent="{PORTAL}"]').get_by_role(
        "button", name="Connection…"
    )
    opener.focus()
    admin.keyboard.press("Enter")
    box = dialog(admin)
    expect(box.get_by_role("heading", name="Connection of Portal Message Agent")).to_be_visible()
    assert violations(admin) == []
    address = box.get_by_label("Address of the service")
    address.focus()
    admin.keyboard.type(service.url)
    admin.keyboard.press("Tab")
    expect(box.locator("#connection-token")).to_be_focused()
    admin.keyboard.type(TOKEN)
    admin.keyboard.press("Enter")  # the form's own submit
    expect(box).to_contain_text(f"Connected to {service.url}.")
    check = box.get_by_role("button", name="Check connection")
    check.focus()
    admin.keyboard.press("Enter")
    expect(box.locator("[data-connection-result]")).to_contain_text(
        "The connection works.", timeout=30_000
    )
    # Tab never reaches the page behind the dialog (it may pass through the browser's own bar).
    behind = """() => { const at = document.activeElement;
      return at !== document.body && !document.querySelector('dialog[open]').contains(at); }"""
    for _ in range(14):
        admin.keyboard.press("Tab")
        assert not admin.evaluate(behind)
    admin.keyboard.press("Escape")
    expect(box).to_have_count(0)
    # The list is read again, and the keyboard is back on the control it came from.
    served = admin.locator(f'article[data-agent="{PORTAL}"]')
    expect(served).to_contain_text(f"to {service.origin}, set")
    expect(served.get_by_role("button", name="Connection…")).to_be_focused()
    expect(served.locator("[data-standing]")).to_have_text("Acts on its own within its policy")


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_connection_fits_a_phone_in_both_themes(
    site: Site,
    service: Service,  # noqa: F811
    scheme: Literal["light", "dark"],
) -> None:
    admin = window(site, "lead", "admin", viewport=PHONE, color_scheme=scheme)
    section = card(admin)
    connect(section, service.url, TOKEN)
    expect(section).to_contain_text(f"Connected to {service.url}.")
    section.get_by_role("button", name="Check connection").click()
    expect(section.locator("[data-connection-result]")).to_contain_text(
        "The connection works.", timeout=30_000
    )
    assert admin.evaluate("document.documentElement.scrollWidth") <= PHONE["width"]
    # The card's own tables scroll sideways at this width and are not this section's.
    assert violations(admin, exclude=(".table-wrap",)) == []
    admin.goto("/#agents")
    admin.locator(f'article[data-agent="{PORTAL}"]').get_by_role(
        "button", name="Connection…"
    ).click()
    box = dialog(admin)
    expect(box).to_contain_text(f"Connected to {service.url}.")
    width = admin.evaluate("document.querySelector('dialog[open]').getBoundingClientRect().right")
    assert width <= PHONE["width"]
    assert violations(admin) == []
    box.get_by_role("button", name="Close").click()
    expect(box).to_have_count(0)


def test_a_deployment_that_cannot_keep_a_token_says_so_and_turns_the_field_off(
    browser: Browser, tmp_path: Path
) -> None:
    """Open mode on synthetic data, PHI mode off: no key to seal a token under. The field is
    off and says why; an address alone is kept."""
    app = create_app(db_path=tmp_path / "open.db", packs_paths=[DEMO_AGENT_DIR.parent])
    with serving(app) as port:
        context = browser.new_context(base_url=f"http://127.0.0.1:{port}", bypass_csp=True)
        try:
            page = context.new_page()
            section = card(page)
            expect(section.locator("#connection-token")).to_be_disabled()
            expect(section).to_contain_text("This deployment cannot keep a token")
            expect(section).to_contain_text("The address must be on the public internet.")
            assert violations(page) == []
            # An address by its number: nothing is looked up, and nothing is called here.
            connect(section, "https://1.1.1.1/decide")
            expect(section).to_contain_text("Connected to https://1.1.1.1/decide.")
            expect(section).to_contain_text("None: calls are sent without one.")
        finally:
            context.close()


def test_with_connections_switched_off_the_card_says_who_sets_them(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memberships(monkeypatch)
    monkeypatch.setenv("MENDARI_AGENTS_PATH", str(DEMO_AGENT_DIR.parent))
    monkeypatch.setenv("MENDARI_AGENT_CONNECTIONS", "off")
    with running(browser, tmp_path / "off.db") as here:
        admin = window(here, "lead", "admin")
        section = card(admin)
        expect(section).to_contain_text("Not connected.")
        expect(section).to_contain_text("Connections are set by whoever runs this deployment")
        expect(section).to_contain_text("setting them here is switched off.")
        expect(section.locator("form")).to_have_count(0)
        assert violations(admin) == []


ELSEWHERE = "https://agents.example.org/v1/decide"


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_an_operator_allows_an_address_the_deployments_list_does_not_name(
    browser: Browser,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    service: Service,  # noqa: F811
    scheme: Literal["light", "dark"],
) -> None:
    """A one-machine install's state: the organization operates the deployment, and the list
    of addresses it may call does not name the agent's service. Its administrator is offered a
    box to allow the address for this agent, must tick it, and the page then says who allowed
    it. Nothing leaves this machine: the name is looked up here, and the public-only client is
    replaced."""
    outside = Outside()
    monkeypatch.setattr(reach, "resolve", lambda host, port: ["93.184.216.34"])
    monkeypatch.setattr(reach, "public_client", lambda: outside.client)
    memberships(monkeypatch)
    monkeypatch.setenv("MENDARI_AGENTS_PATH", str(DEMO_AGENT_DIR.parent))
    monkeypatch.setenv("MENDARI_OPERATOR_ORG", CLINIC_A)
    monkeypatch.setenv("MENDARI_PROVIDER_ALLOWED_ORIGINS", service.origin)
    with running(browser, tmp_path / "operated.db") as here:
        admin = window(here, "lead", "admin", color_scheme=scheme)
        section = card(admin)
        expect(section).to_contain_text(
            "As one of this deployment's operators you can allow another address on the public "
            "internet for this agent when you save."
        )
        box = section.locator("[data-connection-allow]")
        expect(box).to_be_hidden()
        address = section.get_by_label("Address of the service")
        # A listed address needs nobody's word.
        address.fill(service.url)
        expect(box).to_be_hidden()
        address.fill(ELSEWHERE)
        expect(box).to_be_visible()
        expect(box).to_contain_text(
            "https://agents.example.org is not on the list of addresses this deployment may call."
        )
        expect(box).to_contain_text("recorded in the audit trail with your name")
        assert violations(admin) == []
        # Not ticked: nothing is sent, and the form says what to do.
        section.get_by_role("button", name="Save connection").click()
        error = section.locator("form .error")
        expect(error).to_contain_text("Tick the box to allow it for this agent")
        allow = section.get_by_label("Allow this address for this agent")
        expect(allow).to_be_focused()
        with httpx.Client(base_url=here.base, headers=headers(here, "lead", "admin")) as client:
            assert client.get(AT).json()["connection"] is None
        admin.keyboard.press("Space")
        expect(allow).to_be_checked()
        section.locator("#connection-token").fill(TOKEN)
        section.get_by_role("button", name="Save connection").click()
        expect(section).to_contain_text(f"Connected to {ELSEWHERE}.")
        expect(section).to_contain_text("Address allowed")
        expect(section).to_contain_text("by lead@example.org, for this agent only.")
        # The address that was allowed is offered no box again; another one is.
        expect(section.locator("[data-connection-allow]")).to_be_hidden()
        section.get_by_label("Address of the service").fill("https://other.example.org/decide")
        expect(section.locator("[data-connection-allow]")).to_be_visible()
        section.get_by_label("Address of the service").fill(ELSEWHERE)
        assert violations(admin) == []
        section.get_by_role("button", name="Check connection").click()
        expect(section.locator("[data-connection-result]")).to_contain_text(
            "The connection works.", timeout=30_000
        )
        assert [str(request.url) for request in outside.seen] == [ELSEWHERE]
        assert outside.authorizations() == [f"Bearer {TOKEN}"]
        # The audit trail's Activity names it in words.
        with httpx.Client(base_url=here.base, headers=headers(here, "lead", "admin")) as client:
            listed = client.get("/api/audit/administrative").json()["items"]
        assert [
            item["event"] for item in listed if item["event"].startswith("agent_connection")
        ] == ["agent_connection.origin_allowed", "agent_connection.set"]
        assert TOKEN not in str(listed)


def test_where_no_operator_is_named_the_panel_says_nobody_can_allow_an_address(
    browser: Browser,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    service: Service,  # noqa: F811
) -> None:
    """A deployment with a list of addresses and no operator organization named: an
    administrator is offered no box, the panel says what whoever runs it must set, and an
    address off the list is refused by the list."""
    memberships(monkeypatch)
    monkeypatch.setenv("MENDARI_AGENTS_PATH", str(DEMO_AGENT_DIR.parent))
    monkeypatch.delenv("MENDARI_OPERATOR_ORG", raising=False)
    monkeypatch.setenv("MENDARI_PROVIDER_ALLOWED_ORIGINS", service.origin)
    with running(browser, tmp_path / "unnamed.db") as here:
        admin = window(here, "lead", "admin")
        section = card(admin)
        expect(section).to_contain_text(
            "Nobody can allow another address from here: this deployment names no organization "
            "as its operator. Whoever runs it names one (MENDARI_OPERATOR_ORG), or lists the "
            "address (MENDARI_PROVIDER_ALLOWED_ORIGINS)."
        )
        expect(section.locator("[data-connection-allow]")).to_have_count(0)
        section.get_by_label("Address of the service").fill(ELSEWHERE)
        section.get_by_role("button", name="Save connection").click()
        expect(section.locator("form .error")).to_contain_text("MENDARI_PROVIDER_ALLOWED_ORIGINS")
        with httpx.Client(base_url=here.base, headers=headers(here, "lead", "admin")) as client:
            assert client.get(AT).json()["connection"] is None
        assert violations(admin) == []
