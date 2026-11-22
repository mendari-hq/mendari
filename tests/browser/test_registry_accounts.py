# SPDX-License-Identifier: Apache-2.0
"""The pack registry under the engine in the dashboard (LP-19f, ADR-0015): an administrator's
Namespaces and Publisher tokens tabs in the Access view (claim, add a key, mint a token shown
once, revoke), and the registry's review screen for a reviewer of its review organization (the
claims waiting, what the registry checked beside what the publisher claims, the rendered card as
text, approve and refuse with a note).

A real server at `localhost` (tests/browser/access_site.py) in memberships mode with
`MENDARI_REGISTRY=on` and a review organization, sessions planted as `/auth/callback` makes
them; packs are published over HTTP with the token the page showed. Every address is at
example.org, and every key and token is made for the test. Run with `uv run pytest -m browser`."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, Page, expect

from mendari.hub import archive
from tests.browser.access_site import Site, access, dialog, lead, memberships, person, row, running
from tests.browser.test_accessibility import violations
from tests.test_hub_client import SHIPPED, copy_pack
from tests.test_registry_review import REVIEW_ORG, review_org

pytestmark = pytest.mark.browser

TOKEN = re.compile(r"ohr_[A-Za-z0-9_-]{43}")
VERSION = f"/registry/v1/packs/acme/inbox/{SHIPPED}"


@pytest.fixture
def site(browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Site]:
    """Memberships mode with the registry served here and reviewed by `registry-review`."""
    memberships(monkeypatch)
    monkeypatch.setenv("MENDARI_REGISTRY", "on")
    monkeypatch.setenv("MENDARI_REGISTRY_REVIEW_ORG", REVIEW_ORG)
    db = tmp_path / "a.db"
    review_org(str(db))  # its record before the server starts, as the operator makes it
    with running(browser, db) as here:
        yield here


def reviewer(site: Site, sub: str = "rita", roles: list[str] | None = None) -> dict[str, str]:
    held = roles or ["registry_reviewer"]
    return person(site, sub=sub, email=f"{sub}@example.org", org=REVIEW_ORG, roles=held)


def claim(page: Page, name: str = "acme") -> None:
    access(page, "namespaces")
    page.get_by_label("Name", exact=True).fill(name)
    page.get_by_role("button", name="Claim", exact=True).click()
    expect(row(page, "namespaces", name)).to_contain_text("Waiting for the registry's review")


def add_key(page: Page, name: str = "acme") -> Any:
    """A publisher key added to `name` through its dialog: the private key, to sign with."""
    private, public = archive.generate_key()
    row(page, "namespaces", name).get_by_role("button", name=f"Add key to {name}").click()
    dialog(page).get_by_label("Public key").fill(public.decode())
    dialog(page).get_by_role("button", name="Add key").click()
    expect(dialog(page)).to_have_count(0)
    expect(row(page, "namespaces", name)).not_to_contain_text("None yet")
    return archive.load_private_key(private)


def mint(page: Page, name: str = "ci", namespace: str = "acme") -> str:
    """A token minted on the tab, read from the one-time dialog, which is then closed."""
    access(page, "publisher-tokens")
    page.get_by_label("Name", exact=True).fill(name)
    page.get_by_label("Namespace").select_option(namespace)
    page.get_by_role("button", name="Mint token").click()
    secret = dialog(page).get_by_label("Token")
    expect(secret).to_have_value(TOKEN)
    token = secret.input_value()
    dialog(page).get_by_role("button", name="Done").click()
    expect(dialog(page)).to_have_count(0)
    return token


def publish(site: Site, tmp_path: Path, key: Any, token: str) -> httpx.Response:
    pack = copy_pack(tmp_path, "acme/inbox")
    return httpx.put(
        f"{site.base}{VERSION}",
        content=archive.build(pack, key),
        headers={"authorization": f"Bearer {token}"},
    )


def search(site: Site) -> list[str]:
    found: list[dict[str, Any]] = httpx.get(f"{site.base}/registry/v1/search").json()
    return [p["id"] for p in found]


def test_from_a_claim_to_an_approved_pack(site: Site, tmp_path: Path) -> None:
    admin = site.window(lead(site))
    claim(admin)
    key = add_key(admin)
    token = mint(admin)
    published = publish(site, tmp_path, key, token)
    assert published.status_code == 201, published.text
    assert search(site) == []
    assert httpx.get(f"{site.base}{VERSION}").status_code == 404

    review = site.window(reviewer(site))
    review.goto("/#registry-review")
    expect(review.locator("main h1")).to_have_text("Registry review")
    item = review.locator("article.registry-claim", has_text="acme")
    expect(item).to_contain_text("clinic-a")
    expect(item).to_contain_text("What the registry checked")
    expect(item).to_contain_text("What the publisher claims")
    expect(item.locator("dd", has_text="verified")).to_have_count(1)
    item.get_by_text("The card the registry rendered").click()
    card = item.locator("pre.registry-card")
    expect(card).not_to_have_text("Loading the card…")
    expect(card).to_contain_text("Synthetic example")  # the line the registry derives
    assert card.evaluate("pre => pre.children.length") == 0  # text, never markup

    item.get_by_role("button", name="Approve acme").click()
    dialog(review).get_by_label(re.compile("^Note")).fill("Read the card and the checks.")
    dialog(review).get_by_role("button", name="Approve", exact=True).click()
    expect(review.locator("#registry-queue .empty-state")).to_contain_text("No claims waiting")
    assert search(site) == ["acme/inbox"]
    assert httpx.get(f"{site.base}{VERSION}/archive").status_code == 200

    access(admin, "namespaces")
    expect(row(admin, "namespaces", "acme")).to_contain_text("Listed")


def test_a_token_is_shown_once_and_never_again(site: Site) -> None:
    admin = site.window(lead(site))
    claim(admin)
    token = mint(admin)
    tokens = row(admin, "publisher-tokens", "ci")
    expect(tokens).to_contain_text("Active")
    expect(tokens).to_contain_text("Never")  # not used yet
    for page_html in (admin.content(), admin.evaluate("JSON.stringify(localStorage)")):
        assert token not in page_html
    access(admin, "publisher-tokens")
    assert token not in admin.content()
    assert TOKEN.search(admin.content()) is None


def test_a_revoked_token_says_so_and_no_longer_publishes(site: Site, tmp_path: Path) -> None:
    admin = site.window(lead(site))
    claim(admin)
    key = add_key(admin)
    token = mint(admin)
    row(admin, "publisher-tokens", "ci").get_by_role("button", name="Revoke ci").click()
    expect(dialog(admin)).to_contain_text("can't be undone")
    dialog(admin).get_by_role("button", name="Revoke", exact=True).click()
    expect(row(admin, "publisher-tokens", "ci")).to_contain_text("Revoked")
    expect(row(admin, "publisher-tokens", "ci").get_by_role("button")).to_have_count(0)
    refused = publish(site, tmp_path, key, token)
    assert refused.status_code in (401, 403), refused.text


def test_a_reserved_name_is_refused_in_words(site: Site) -> None:
    admin = site.window(lead(site))
    access(admin, "namespaces")
    for name in ("mendari", "openhealth-labs"):
        admin.get_by_label("Name", exact=True).fill(name)
        admin.get_by_role("button", name="Claim", exact=True).click()
        expect(admin.locator("#claim-error")).to_have_text(
            f"“{name}” is reserved for Mendari's own agents. Choose another name."
        )
    expect(admin.locator('[data-table="namespaces"]')).to_have_count(0)
    # Nothing to mint for until a namespace is claimed: the tab says where to claim one.
    access(admin, "publisher-tokens")
    expect(admin.locator(".empty-state")).to_contain_text("No namespace to publish to")


def test_only_reviewers_see_the_queue_and_only_administrators_the_tabs(site: Site) -> None:
    review_link = 'nav a[data-view="registry-review"]'
    # An administrator elsewhere: the tabs, no review screen (an address naming it shows the
    # overview).
    admin = site.window(lead(site))
    access(admin, "namespaces")
    expect(admin.locator(".tabs a[data-tab]")).to_contain_text(["Namespaces", "Publisher tokens"])
    expect(admin.locator(review_link)).to_be_hidden()
    admin.goto("/#registry-review")
    expect(admin.locator("main h1")).not_to_have_text("Registry review")
    # A reviewer and nothing else: the review screen alone, whatever the address says.
    rita = site.window(reviewer(site))
    rita.goto("/#admin/namespaces")
    expect(rita.locator("main h1")).to_have_text("Registry review")
    expect(rita.locator("nav a[data-view]:visible")).to_have_text(["Registry review"])
    # A labeller too, where labelling is not served here: still the review screen.
    lea = site.window(reviewer(site, sub="lea", roles=["labeller", "registry_reviewer"]))
    lea.goto("/#overview")
    expect(lea.locator("main h1")).to_have_text("Registry review")
    expect(lea.locator("nav a[data-view]:visible")).to_have_text(["Registry review"])
    # The review organization's administrator is not a reviewer until given the role; given it,
    # they see both.
    boss = site.window(reviewer(site, sub="boss", roles=["admin"]))
    boss.goto("/#overview")
    expect(boss.locator("main h1")).to_be_visible()
    expect(boss.locator(review_link)).to_be_hidden()
    both = site.window(reviewer(site, sub="both", roles=["admin", "registry_reviewer"]))
    both.goto("/#registry-review")
    expect(both.locator("main h1")).to_have_text("Registry review")
    expect(both.locator(review_link)).to_be_visible()
    access(both, "publisher-tokens")
    # A viewer: neither.
    vic = person(site, sub="vic", email="vic@example.org", org="clinic-a", roles=["viewer"])
    viewer = site.window(vic)
    viewer.goto("/#overview")
    expect(viewer.locator("main h1")).to_be_visible()
    expect(viewer.locator('nav a[data-view="admin"]')).to_be_hidden()
    expect(viewer.locator(review_link)).to_be_hidden()


def test_a_reviewer_never_decides_their_own_claim(site: Site, tmp_path: Path) -> None:
    both = site.window(reviewer(site, sub="both", roles=["admin", "registry_reviewer"]))
    claim(both, "review-tools")
    both.goto("/#registry-review")
    item = both.locator("article.registry-claim", has_text="review-tools")
    expect(item).to_contain_text("You claimed this namespace: another reviewer decides it.")
    expect(item.get_by_role("button")).to_have_count(0)
    expect(item).to_contain_text("Nothing published yet")
    # Another reviewer may refuse it before anything is published (approval needs a version).
    rita = site.window(reviewer(site))
    rita.goto("/#registry-review")
    theirs = rita.locator("article.registry-claim", has_text="review-tools")
    expect(theirs.get_by_role("button", name="Approve review-tools")).to_have_count(0)
    theirs.get_by_role("button", name="Refuse review-tools").click()
    dialog(rita).get_by_label(re.compile("^Note")).fill("Not a pack publisher.")
    dialog(rita).get_by_role("button", name="Refuse", exact=True).click()
    expect(rita.locator("#registry-queue .empty-state")).to_contain_text("No claims waiting")
    access(both, "namespaces")
    refused = row(both, "namespaces", "review-tools")
    expect(refused).to_contain_text("Refused: Not a pack publisher.")
    expect(refused.get_by_role("button")).to_have_count(0)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_registry_screens_pass_axe(site: Site, tmp_path: Path, scheme: str) -> None:
    look: dict[str, Any] = {"bypass_csp": True, "color_scheme": scheme}  # the test injects axe
    admin = site.window(lead(site), **look)
    found = {}
    access(admin, "namespaces")
    found["no namespaces"] = violations(admin)
    access(admin, "publisher-tokens")
    found["nothing to mint for"] = violations(admin)
    claim(admin)
    key = add_key(admin)
    token = mint(admin)
    access(admin, "namespaces")
    found["namespaces"] = violations(admin)
    access(admin, "publisher-tokens")
    found["tokens"] = violations(admin)
    assert publish(site, tmp_path, key, token).status_code == 201
    review = site.window(reviewer(site), **look)
    review.goto("/#registry-review")
    item = review.locator("article.registry-claim", has_text="acme")
    item.get_by_text("The card the registry rendered").click()
    expect(item.locator("pre.registry-card")).to_contain_text("Synthetic example")
    found["queue"] = violations(review)
    item.get_by_role("button", name="Approve acme").click()
    found["approve dialog"] = violations(review)
    dialog(review).get_by_role("button", name="Approve", exact=True).click()
    expect(review.locator("#registry-queue .empty-state")).to_contain_text("No claims waiting")
    found["empty queue"] = violations(review)
    assert {screen: v for screen, v in found.items() if v} == {}


@pytest.fixture
def without_registry(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Site]:
    memberships(monkeypatch)
    with running(browser, tmp_path / "a.db") as here:
        yield here


def test_without_the_registry_the_tabs_are_not_there(without_registry: Site) -> None:
    admin = without_registry.window(lead(without_registry))
    admin.goto("/#admin/namespaces")
    expect(admin.locator("main h1")).to_have_text("Access")
    tabs = admin.locator(".tabs a[data-tab]")
    expect(tabs.first).to_be_visible()
    assert not {"Namespaces", "Publisher tokens"} & set(tabs.all_inner_texts())
    expect(admin.locator('.tabs a[aria-current="page"]')).to_have_attribute("data-tab", "members")
    expect(admin.locator('nav a[data-view="registry-review"]')).to_be_hidden()
