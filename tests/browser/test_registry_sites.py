# SPDX-License-Identifier: Apache-2.0
"""Evaluating sites in the dashboard (LP-22i, ADR-0017): an administrator's Sites tab in the
Access view (register a site, add the key file `mendari site init` wrote, revoke a key with
why and whether it is compromised) and the sites waiting on the registry's review screen,
verified or refused with a note. A real server at `localhost` (tests/browser/access_site.py);
synthetic sites, every key made for the test. Run with `uv run pytest -m browser`."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, Page, expect

from mendari.core.registry import resolve_pack
from mendari.hub import site_signing
from mendari.hub.site_signing import Site as SiteFiles
from mendari.hub.site_signing import init_site
from tests.browser.access_site import Site, access, dialog, lead, memberships, row, running
from tests.browser.test_accessibility import violations
from tests.browser.test_registry_accounts import reviewer
from tests.test_registry_review import REVIEW_ORG, review_org

pytestmark = pytest.mark.browser


@pytest.fixture
def site(browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Site]:
    """Memberships mode with the registry served here and reviewed by `registry-review`."""
    memberships(monkeypatch)
    monkeypatch.setenv("MENDARI_REGISTRY", "on")
    monkeypatch.setenv("MENDARI_REGISTRY_REVIEW_ORG", REVIEW_ORG)
    db = tmp_path / "a.db"
    review_org(str(db))
    with running(browser, db) as here:
        yield here


SITE_NAME = "Clinic A Hospital (synthetic)"


def register(page: Page, name: str = SITE_NAME) -> str:
    access(page, "sites")
    page.get_by_label("Name", exact=True).fill(name)
    page.get_by_role("button", name="Register", exact=True).click()
    expect(row(page, "sites", name)).to_contain_text("Waiting for the registry's review")
    return row(page, "sites", name).locator("code").first.inner_text()


def add_key(page: Page, site_id: str, where: Path, name: str = SITE_NAME) -> SiteFiles:
    """The key file `mendari site init` writes, pasted into the dialog; the site's own files."""
    made = init_site(site_id, directory=where)
    row(page, "sites", name).get_by_role("button", name=f"Add key to {name}").click()
    dialog(page).get_by_label("Key file").fill(json.dumps(made))
    dialog(page).get_by_role("button", name="Add key").click()
    expect(dialog(page)).to_have_count(0)
    expect(row(page, "sites", name)).to_contain_text(made["key_id"])
    return SiteFiles(where)


def verify(site: Site, name: str = SITE_NAME) -> None:
    review = site.window(reviewer(site))
    review.goto("/#registry-review")
    item = review.locator("article.registry-site", has_text=name)
    expect(item).to_contain_text("clinic-a")
    item.get_by_role("button", name=f"Verify {name}").click()
    dialog(review).get_by_label(re.compile("^How you checked it")).fill("Called their CMIO back.")
    dialog(review).get_by_role("button", name="Verify", exact=True).click()
    expect(review.locator("#site-queue .empty-state")).to_contain_text("No sites waiting")


def test_from_a_registered_site_to_a_verified_one(site: Site, tmp_path: Path) -> None:
    admin = site.window(lead(site))
    site_id = register(admin)
    add_key(admin, site_id, tmp_path / "site")
    assert httpx.get(f"{site.base}/registry/v1/sites/{site_id}").status_code == 404  # pending
    verify(site)
    access(admin, "sites")
    expect(row(admin, "sites", SITE_NAME)).to_contain_text("Verified")
    shown = httpx.get(f"{site.base}/registry/v1/sites/{site_id}").json()
    assert (shown["name"], shown["status"]) == (SITE_NAME, "verified")


def test_a_revoked_key_says_so_and_gets_no_ticket(site: Site, tmp_path: Path) -> None:
    admin = site.window(lead(site))
    site_id = register(admin)
    files = add_key(admin, site_id, tmp_path / "site")
    verify(site)
    access(admin, "sites")
    the_row = row(admin, "sites", SITE_NAME)
    the_row.get_by_role("button", name=re.compile(f"^Revoke key {files.key_id}")).click()
    dialog(admin).get_by_label(re.compile("^Why")).fill("The laptop was lost.")
    dialog(admin).get_by_label(re.compile("may have been copied")).check()
    dialog(admin).get_by_role("button", name="Revoke", exact=True).click()
    expect(dialog(admin)).to_have_count(0)
    expect(the_row).to_contain_text("revoked")
    expect(the_row).to_contain_text("as compromised")
    referral = resolve_pack("mendari/referral-router")
    request = files.sign(
        site_signing.TICKET_REQUEST,
        {
            "pack": {"id": referral.id, "version": referral.version, "digest": referral.digest},
            "configuration": {"triage_screen": "rules"},
            "remote_consent": False,
            "commitment": files.commitment(b"cases"),
            "n": 300,
        },
    )
    answer = httpx.post(f"{site.base}/registry/v1/tickets", json={"record": request})
    assert answer.status_code == 403, answer.text  # a revoked key is no key


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_sites_tab_and_its_dialogs_pass_axe(site: Site, tmp_path: Path, scheme: str) -> None:
    look: dict[str, Any] = {"bypass_csp": True, "color_scheme": scheme}  # the test injects axe
    admin = site.window(lead(site), **look)
    found = {}
    access(admin, "sites")
    found["no sites"] = violations(admin)
    site_id = register(admin)
    row(admin, "sites", SITE_NAME).get_by_role("button", name=f"Add key to {SITE_NAME}").click()
    found["add key dialog"] = violations(admin)
    dialog(admin).get_by_role("button", name="Cancel").click()
    expect(dialog(admin)).to_have_count(0)
    add_key(admin, site_id, tmp_path / "site")
    found["sites"] = violations(admin)
    row(admin, "sites", SITE_NAME).get_by_role("button", name=re.compile("^Revoke key")).click()
    found["revoke dialog"] = violations(admin)
    assert {screen: v for screen, v in found.items() if v} == {}
