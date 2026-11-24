# SPDX-License-Identifier: Apache-2.0
"""The Agents view in the console (`dashboard/agents.js`): the agents an organization uses, and
the agent registry to find and add more from.

An author finds an agent in the deployment's registry, reads its listing (what the registry
checked apart from what the publisher claims, its Decision Card as text), and adds a version,
which the server verifies; the view then shows it as added and not in use, with its next step
and who takes it; someone else starts serving it, and it still does not act on its own. What a
registry says is shown as text, whatever it holds. A changed signing key asks before it is
accepted. Reachable by keyboard, without an axe violation, and without scrolling sideways on a
phone.

A real server at `localhost` in memberships mode with the agent store on
(tests/browser/access_site.py) and a real registry beside it (tests/test_hub_client.py). Run
with `uv run pytest -m browser`."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, Dialog, Page, Route, expect

from mendari.core.pack import Pack
from mendari.hub import archive
from tests.browser.access_site import Site, dialog, memberships, person, running
from tests.browser.test_accessibility import violations
from tests.browser.test_dashboard import PHONE
from tests.conftest import DEMO_AGENT_DIR
from tests.test_hub_client import Hub, hub  # noqa: F401 - the live registry fixture
from tests.test_hub_routes import ACME, REFILL, as_published

pytestmark = pytest.mark.browser

CLINIC_A, CLINIC_B = "clinic-a", "clinic-b"
PORTAL = "mendari/portal-message-agent"
OWN = {"x-mendari-request": "1"}
HOSTILE = '<img src=x onerror="window.__ran = 1"><script>window.__ran = 1</script>'


@dataclass
class Adopting:
    """The console's server (memberships mode, the agent store on, the bundled agent served),
    the registry it names, and the agent published there."""

    site: Site
    hub: Hub
    tmp: Path
    token: str
    key: Any
    agent: Pack

    def publish(self, version: str, key: Any = None) -> Pack:
        pack = as_published(self.tmp, ACME, version)
        self.hub.client(self.token).push(pack, key or self.key)
        return pack

    def window(self, sub: str, *roles: str, org: str = CLINIC_A, **options: Any) -> Page:
        """A browser of `sub`'s, a member of `org` with `roles` (axe may be injected in it)."""
        headers = person(self.site, sub=sub, email=f"{sub}@example.org", org=org, roles=list(roles))
        return self.site.window(headers, **{"bypass_csp": True, **options})

    def headers(self, sub: str, *roles: str, org: str = CLINIC_A) -> dict[str, str]:
        found = person(self.site, sub=sub, email=f"{sub}@example.org", org=org, roles=list(roles))
        return {**found, **OWN}


def adopting(
    browser: Browser,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    registry: Hub,
) -> Iterator[Adopting]:
    memberships(monkeypatch)
    monkeypatch.setenv("MENDARI_AGENT_STORE", "on")
    monkeypatch.setenv("MENDARI_REGISTRY_URL", registry.url)
    monkeypatch.setenv("MENDARI_AGENTS_PATH", str(DEMO_AGENT_DIR.parent))
    token, key = registry.namespace("acme")
    agent = as_published(tmp_path, ACME)
    registry.client(token).push(agent, key)
    with running(browser, tmp_path / "a.db") as here:
        yield Adopting(here, registry, tmp_path, token, key, agent)


@pytest.fixture
def world(
    browser: Browser,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hub: Hub,  # noqa: F811
) -> Iterator[Adopting]:
    yield from adopting(browser, tmp_path, monkeypatch, hub)


def agents(page: Page, tab: str = "") -> None:
    """The Agents view's tab (`hub`, or Our agents), freshly loaded."""
    page.goto("about:blank")
    page.goto(f"/#agents{'/' + tab if tab else ''}")
    expect(page.locator("main h1")).to_have_text("Agents")
    current = page.locator(f'.tabs a[data-tab="{tab or "ours"}"]')
    expect(current).to_have_attribute("aria-current", "page")
    expect(page.locator("#agents-body")).not_to_have_attribute("aria-busy", "true")


def listing(page: Page, pack_id: str = ACME) -> None:
    page.goto("about:blank")
    page.goto(f"/#agents/hub/{pack_id}")
    expect(page.locator("#agents-body")).not_to_have_attribute("aria-busy", "true")


def add(page: Page, pack_id: str = ACME) -> None:
    """Add the listed version from its page, through the dialog."""
    listing(page, pack_id)
    page.get_by_role("button", name=re.compile(r"^Add version")).click()
    dialog(page).get_by_role("button", name="Add", exact=True).click()


def waiting(page: Page) -> Any:
    return page.locator("article[data-waiting]")


def decide(world: Adopting, org: str, pack_id: str = ACME) -> httpx.Response:
    with httpx.Client(base_url=world.site.base) as client:
        return client.post(
            f"/api/agents/{pack_id}/decide",
            json=REFILL,
            headers=world.headers(f"reviewer-{org}", "reviewer", org=org),
        )


def test_an_agent_is_found_added_evaluated_and_served_by_someone_else(world: Adopting) -> None:
    """The whole path in the console. An author searches the Hub, reads the listing and adds
    the version; Our agents shows it added and not in use, with the next step; the author
    evaluates it and is told someone else starts serving it; a governance member does; it is
    then in use, and the view says why a person still decides every case."""
    author = world.window("aria", "pack_author", "viewer")
    agents(author, "hub")
    card = author.locator(f'article[data-hub="{ACME}"]')
    expect(card.get_by_role("heading")).to_have_text("Portal Message Agent")
    expect(card).to_contain_text("community publisher")
    expect(card).to_contain_text("Risk 0 (declared)")
    expect(card).to_contain_text("Synthetic example. Not clinically reviewed.")
    author.get_by_label("Search the agent registry").fill("zq-nothing-like-it")
    author.get_by_role("button", name="Search").click()
    expect(author.locator("#hub-results")).to_contain_text("No agent matches")
    author.get_by_label("Search the agent registry").fill("portal")
    author.keyboard.press("Enter")
    author.locator(f'article[data-hub="{ACME}"] h3 a').click()

    expect(author).to_have_url(re.compile(r"#agents/hub/acme/portal-agent$"))
    expect(author.locator("main h1")).to_have_text("Portal Message Agent")
    expect(author.locator("main h1")).to_be_focused()
    checked = author.locator("#hub-checked")
    expect(checked.get_by_role("heading")).to_have_text("Checked by the registry")
    expect(checked).to_contain_text("Verified when it was published")
    expect(checked).to_contain_text(world.agent.digest)
    expect(checked).to_contain_text("Not reviewed by the registry.")
    claimed = author.locator("#hub-claimed")
    expect(claimed.get_by_role("heading")).to_have_text("Claimed by the publisher")
    expect(claimed).to_contain_text("Mendari (community, self-declared)")
    expect(claimed).to_contain_text("0, administrative")
    expect(author.locator("#hub-evaluations")).to_contain_text("published no evaluation")
    expect(author.locator("#hub-card")).to_contain_text("# Decision Card: Portal Message Agent")
    assert violations(author) == []

    author.get_by_role("button", name=f"Add version {world.agent.version}…").click()
    asked = dialog(author)
    expect(asked).to_contain_text(f"{ACME}@{world.agent.version}")
    expect(asked).to_contain_text("not in use")
    assert violations(author) == []
    asked.get_by_role("button", name="Add", exact=True).click()
    result = author.locator("#hub-add-result")
    expect(result).to_contain_text("Added and verified.", timeout=30_000)
    expect(author.get_by_role("button", name=re.compile(r"^Add version"))).to_have_count(0)
    result.get_by_role("link", name="See it under Our agents.").click()

    expect(author.locator("main h1")).to_have_text("Agents")
    added = waiting(author)
    expect(added).to_have_count(1)
    expect(added).to_contain_text(f"{ACME} {world.agent.version}")
    expect(added).to_contain_text("Not in use yet")
    expect(added).to_contain_text(f"Added from {world.hub.url}")
    expect(added.locator(".adoption-step.done")).to_have_count(1)
    expect(added.locator('.adoption-step[aria-current="step"]')).to_contain_text("Evaluate it here")
    assert violations(author) == []
    assert decide(world, CLINIC_A).status_code == 404  # added: it answers nothing

    added.get_by_role("button", name="Evaluate…").click()
    evaluating = dialog(author)
    expect(evaluating.get_by_label("Dataset")).to_have_value("examples")
    evaluating.get_by_role("button", name="Evaluate", exact=True).click()
    expect(added.locator(".adoption-step.done")).to_have_count(2, timeout=120_000)
    current = added.locator('.adoption-step[aria-current="step"]')
    expect(current).to_contain_text("Start serving it")
    expect(current).to_contain_text("Governance or an administrator.")  # not the author
    expect(added.get_by_role("button", name="Start serving…")).to_have_count(0)

    approver = world.window("gus", "governance")
    agents(approver)
    theirs = waiting(approver)
    theirs.get_by_role("button", name="Start serving…").click()
    serving = dialog(approver)
    expect(serving).to_contain_text("Clinic-A")
    expect(serving).to_contain_text("a person decides every case until your organization approves")
    serving.get_by_label("Reference (where this decision is recorded)").fill("AI committee, item 2")
    assert violations(approver) == []
    serving.get_by_role("button", name="Start serving", exact=True).click()
    in_use = approver.locator(f'article[data-agent="{ACME}"]')
    expect(in_use).to_be_visible(timeout=30_000)
    expect(in_use.locator("[data-standing]")).to_have_text("A person decides every case")
    expect(in_use).to_contain_text("Your organization has not approved this exact version yet.")
    expect(in_use).to_contain_text("It is not connected to its service")
    expect(in_use).to_contain_text("AI committee, item 2")
    expect(waiting(approver)).to_have_count(0)
    assert violations(approver) == []

    decided = decide(world, CLINIC_A)
    assert decided.status_code == 200 and decided.json()["outcome"] == "HUMAN_REVIEW"
    assert decide(world, CLINIC_B).status_code == 404  # another organization: nothing
    # The agent picker now has it, and Open shows it in the other views.
    in_use.get_by_role("button", name="Open").click()
    expect(approver).to_have_url(re.compile(r"#overview$"))
    expect(approver.locator("#pack-select")).to_have_value(ACME)


def test_whoever_added_it_is_told_someone_else_starts_serving_it(world: Adopting) -> None:
    """A person who is both author and governance adds and evaluates: no control starts
    serving it for them."""
    both = world.window("dana", "pack_author", "governance")
    add(both)
    expect(both.locator("#hub-add-result")).to_contain_text("Added and verified.", timeout=30_000)
    agents(both)
    added = waiting(both)
    added.get_by_role("button", name="Evaluate…").click()
    dialog(both).get_by_role("button", name="Evaluate", exact=True).click()
    current = added.locator('.adoption-step[aria-current="step"]')
    expect(current).to_contain_text("Someone other than you: you added it.", timeout=120_000)
    expect(added.get_by_role("button", name="Start serving…")).to_have_count(0)
    # The Hub says what the organization has of it; nothing is offered twice.
    listing(both)
    expect(both.locator("#hub-add")).to_contain_text("it is not in use yet")
    expect(both.get_by_role("button", name=re.compile(r"^Add version"))).to_have_count(0)
    # Withdrawn, it leaves the list.
    agents(both)
    waiting(both).get_by_role("button", name="Withdraw…").click()
    dialog(both).get_by_role("button", name="Withdraw", exact=True).click()
    expect(waiting(both)).to_have_count(0)
    expect(both.locator("#agents-body")).to_contain_text("Nothing is waiting.")


def test_a_reader_reads_the_hub_and_is_told_who_may_add(world: Adopting) -> None:
    viewer = world.window("vera", "viewer")
    agents(viewer, "hub")
    expect(viewer.locator("#hub-cannot-add")).to_contain_text("needs the Agent author role")
    listing(viewer)
    expect(viewer.locator("#hub-add")).to_contain_text("needs the Agent author role")
    expect(viewer.get_by_role("button", name=re.compile(r"^Add version"))).to_have_count(0)
    agents(viewer)
    served = viewer.locator(f'article[data-agent="{PORTAL}"]')
    expect(served.locator("[data-standing]")).to_have_text("A person decides every case")
    expect(served).to_contain_text("Not connected")
    expect(viewer.locator("#agents-waiting + p")).to_contain_text("Shown to agent authors")
    assert violations(viewer) == []


def test_what_a_registry_says_is_shown_as_text(world: Adopting) -> None:
    """A registry (or a publisher) that answers markup in every field: nothing of it runs or
    becomes an element; it is read as the text it is."""
    author = world.window("aria", "pack_author", "viewer")
    alerts: list[str] = []

    def dismissed(shown: Dialog) -> None:
        alerts.append(shown.message)
        shown.dismiss()

    author.on("dialog", dismissed)

    def hostile(value: Any) -> Any:
        if isinstance(value, str):
            return HOSTILE
        if isinstance(value, list):
            return [hostile(item) for item in value] or [HOSTILE]
        if isinstance(value, dict):
            return {key: hostile(item) if key != "id" else item for key, item in value.items()}
        return value

    def answer(route: Route) -> None:
        body = route.fetch().json()
        if "agents" in body:
            body["agents"] = [
                {**hostile(a), "version": HOSTILE, "installed": None} for a in body["agents"]
            ]
        else:
            body["agent"] = {**hostile(body["agent"]), "recall": None}
            body["recalled"] = [
                {"version": HOSTILE, "severity": HOSTILE, "reason": HOSTILE, "deleted": False}
            ]
            body["agent"]["evaluations"] = {
                "published": 1,
                "listed": [{"dataset": HOSTILE, "cases": 3, "models": [HOSTILE], "rerun": HOSTILE}],
            }
        route.fulfill(json=body)

    author.route(re.compile(r".*/api/hub/(search|agents/).*"), answer)
    agents(author, "hub")
    card = author.locator(f'article[data-hub="{ACME}"]')
    expect(card.get_by_role("heading")).to_have_text(HOSTILE)
    listing(author)
    expect(author.locator("main h1")).to_have_text(HOSTILE)
    expect(author.locator("#hub-card")).to_have_text(HOSTILE)
    expect(author.locator("#hub-recalled")).to_contain_text(HOSTILE)
    expect(author.locator("#hub-evaluations")).to_contain_text(HOSTILE)
    assert author.locator("main img, main script").count() == 0
    assert author.evaluate("window.__ran") is None and alerts == []


def test_a_changed_signing_key_asks_before_it_is_accepted(world: Adopting) -> None:
    """The publisher signs a later version with a key the organization has not seen: the first
    dialog adds nothing and a second names both keys; cancelled, nothing is added; accepted,
    the version is."""
    author = world.window("aria", "pack_author", "viewer")
    add(author)
    expect(author.locator("#hub-add-result")).to_contain_text("Added and verified.", timeout=30_000)
    private, public = archive.generate_key()
    world.hub.client(world.token).add_key("acme", public)
    rotated = archive.load_private_key(private)
    world.publish("2.0.0", rotated)
    old, new = (archive.key_id(k.public_key()) for k in (world.key, rotated))

    add(author)
    asked = dialog(author)
    expect(asked).to_contain_text("Accept this publisher's new key and add?", timeout=30_000)
    expect(asked).to_contain_text(new)
    expect(asked).to_contain_text(old)
    expect(asked).to_contain_text("Check the new key with the publisher")
    assert violations(author) == []
    asked.get_by_role("button", name="Cancel").click()
    expect(author.get_by_role("button", name="Add version 2.0.0…")).to_be_visible()
    with httpx.Client(base_url=world.site.base) as client:
        kept = client.get(
            "/api/store/agents", headers=world.headers("aria", "pack_author", "viewer")
        ).json()
    assert [v["version"] for v in kept[0]["versions"]] == [world.agent.version]

    author.get_by_role("button", name="Add version 2.0.0…").click()
    dialog(author).get_by_role("button", name="Add", exact=True).click()
    dialog(author).get_by_role("button", name="Accept the key and add").click()
    expect(author.locator("#hub-add-result")).to_contain_text("Added and verified.", timeout=30_000)


def test_a_recalled_version_cannot_be_added_and_a_refusal_says_why(world: Adopting) -> None:
    author = world.window("aria", "pack_author", "viewer")
    listing(author)
    world.hub.client(world.token).yank(
        "acme", "portal-agent", world.agent.version, reason="misroutes refills",
        severity="high", key=world.key,
    )  # fmt: skip
    # Recalled after the page was read: the server refuses, and the page says why.
    author.get_by_role("button", name=re.compile(r"^Add version")).click()
    dialog(author).get_by_role("button", name="Add", exact=True).click()
    refusal = dialog(author).locator(".error")
    expect(refusal).to_contain_text("was recalled by its registry (severity: high)")
    dialog(author).get_by_role("button", name="Cancel").click()
    listing(author)
    expect(author.locator("#agents-body")).to_contain_text(
        "Every version of this agent is recalled."
    )
    expect(author.locator("#hub-recalled")).to_contain_text("recalled (high). misroutes refills")
    expect(author.get_by_role("button", name=re.compile(r"^Add version"))).to_have_count(0)
    agents(author, "hub")
    expect(author.locator("#hub-results")).to_contain_text("The registry lists no agent yet")
    agents(author)
    expect(waiting(author)).to_have_count(0)


def test_the_view_works_from_the_keyboard_and_on_a_phone(world: Adopting) -> None:
    author = world.window("aria", "pack_author", "viewer", viewport=PHONE)
    agents(author, "hub")
    assert author.evaluate("document.documentElement.scrollWidth") <= PHONE["width"]
    search = author.get_by_label("Search the agent registry")
    search.focus()
    author.keyboard.type("portal")
    author.keyboard.press("Enter")
    link = author.locator(f'article[data-hub="{ACME}"] h3 a')
    expect(link).to_be_visible()
    link.focus()
    author.keyboard.press("Enter")
    expect(author.locator("main h1")).to_be_focused()
    assert author.evaluate("document.documentElement.scrollWidth") <= PHONE["width"]
    button = author.get_by_role("button", name=re.compile(r"^Add version"))
    button.focus()
    author.keyboard.press("Enter")
    asked = dialog(author)
    expect(asked).to_be_visible()
    author.keyboard.press("Escape")  # a dialog closes from the keyboard, and adds nothing
    expect(asked).to_have_count(0)
    expect(button).to_be_focused()
    button.click()
    dialog(author).get_by_role("button", name="Add", exact=True).click()
    expect(author.locator("#hub-add-result")).to_contain_text("Added and verified.", timeout=30_000)
    agents(author)
    expect(waiting(author)).to_have_count(1)
    assert author.evaluate("document.documentElement.scrollWidth") <= PHONE["width"]
    assert violations(author) == []


def test_without_a_registry_the_hub_says_so_and_our_agents_lists_what_is_served(
    axe_page: Page,
) -> None:
    """The plain open-mode dashboard (no registry, no agent store): the view lists the served
    agents, and the Hub tab says what the deployment lacks, in words for a person."""
    page = axe_page
    page.goto("/#agents")
    expect(page.locator("main h1")).to_have_text("Agents")
    expect(page.locator("#agents-body")).not_to_have_attribute("aria-busy", "true")
    expect(page.locator("article[data-agent]")).not_to_have_count(0)
    expect(page.locator("#agents-waiting")).to_have_count(0)  # no agent store here
    page.get_by_role("link", name="Find agents", exact=True).click()
    expect(page.locator("#agents-body")).to_contain_text("No agent registry is set")
    expect(page.locator("#agents-body")).to_contain_text("MENDARI_REGISTRY_URL")
    assert violations(page) == []


def test_a_registry_that_does_not_answer_is_said_in_plain_words(world: Adopting) -> None:
    author = world.window("aria", "pack_author", "viewer")
    author.route(
        re.compile(r".*/api/hub/search.*"),
        lambda route: route.fulfill(
            status=502,
            json={"detail": "the registry did not answer: check that this server can reach it"},
        ),
    )
    agents(author, "hub")
    failed = author.locator("#hub-results [role=alert]")
    expect(failed).to_contain_text("The registry could not be read")
    expect(failed).to_contain_text("the registry did not answer")
    author.unroute(re.compile(r".*/api/hub/search.*"))
    failed.get_by_role("button", name="Try again").click()
    expect(author.locator(f'article[data-hub="{ACME}"]')).to_be_visible()
