# SPDX-License-Identifier: Apache-2.0
"""The audit screens of a signed-in organization (12g-ii, LP-21k): the Access view's Activity tab
lists the organization's administrative events newest first, in words (past its page cap, the
newest, saying so), and exports them as FHIR;
the Audit trail shows the signed daily checkpoint (or a quiet line without a key) and offers its
FHIR export to administrators only; both pass axe in both themes. A real server at `localhost`
in memberships mode (tests/browser/access_site.py); all data synthetic. Run with
`uv run pytest -m browser`."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from playwright.sync_api import Browser, Page, Route, expect

from mendari.core.checkpoint import KEY_ENV
from mendari.server import app as app_module
from tests.browser.access_site import Site, access, lead, memberships, person, row, running
from tests.browser.conftest import TRIAGE
from tests.browser.test_accessibility import violations

pytestmark = pytest.mark.browser

SEED = bytes(range(32))  # the checkpoint's synthetic signing key


@pytest.fixture
def site(browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Site]:
    memberships(monkeypatch)
    monkeypatch.delenv(KEY_ENV, raising=False)
    with running(browser, tmp_path / "a.db") as here:
        yield here


@pytest.fixture
def signing(browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Site]:
    """The same with MENDARI_CHECKPOINT_KEY set: the day's checkpoint can be signed, and each
    read of it reads the store (no minute's reuse)."""
    memberships(monkeypatch)
    monkeypatch.setenv(KEY_ENV, base64.b64encode(SEED).decode())
    monkeypatch.setattr(app_module, "CHECKPOINT_SECONDS", 0.0)
    with running(browser, tmp_path / "a.db") as here:
        yield here


def today() -> str:
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()  # the deployment's zone


def audit(page: Page) -> None:
    page.goto("about:blank")
    page.goto("/#audit")
    expect(page.locator("main h1")).to_have_text("Audit trail")
    expect(page.locator("#checkpoint")).not_to_be_empty()


def keys(site: Site) -> str:
    """Clinic-A's key made and revoked by its administrator, and Clinic-B's own key; the id of
    Clinic-B's."""
    made = site.store.create_api_key(
        name="<b>ehr</b>", role="service", org="clinic-a", created_by="lead@example.org"
    )
    site.store.revoke_api_key(made["id"], "clinic-a", revoked_by="lead@example.org")
    person(site, sub="bea", email="bea@example.org", org="clinic-b", roles=["admin"])
    other = site.store.create_api_key(
        name="other-clinic-key", role="viewer", org="clinic-b", created_by="bea@example.org"
    )
    return str(other["id"])


def test_the_activity_tab_lists_the_organization_s_events_newest_first(site: Site) -> None:
    page = site.window(lead(site))
    other = keys(site)
    access(page, "activity")
    table = page.locator('[data-table="activity"]')
    expect(table.locator("caption")).to_have_text("Administrative events of Clinic-A, newest first")
    first = table.locator("tbody tr").first
    expect(first).to_contain_text("API key revoked")
    expect(first).to_contain_text("API key <b>ehr</b>")  # its name, as text
    expect(first.locator("td").last).to_have_text(re.compile("^(You|lead)$"))
    expect(row(page, "activity", "API key created")).to_have_count(1)
    expect(row(page, "activity", "Member added")).to_have_count(1)
    expect(page.locator("#admin-body b")).to_have_count(0)  # nothing became markup
    expect(page.locator("#admin-body")).not_to_contain_text("other-clinic-key")
    expect(page.locator("#admin-body")).not_to_contain_text(other)
    # The oldest first, by sorting on time.
    table.get_by_role("button", name="Time").click()
    table.get_by_role("button", name="Time").click()
    expect(table.locator("tbody tr").first).not_to_contain_text("API key revoked")

    with page.expect_download() as saved:
        page.get_by_role("button", name="Export as FHIR").click()
    assert saved.value.suggested_filename == f"activity-clinic-a-{today()}.ndjson"
    lines = Path(saved.value.path()).read_text().splitlines()
    exported = [json.loads(line) for line in lines]
    assert {r["resourceType"] for r in exported} == {"AuditEvent"}
    codes = {r["subtype"][0]["code"] for r in exported}
    assert {"key.created", "key.revoked", "membership.created"} <= codes
    assert other not in "\n".join(lines) and "other-clinic-key" not in "\n".join(lines)


def test_past_its_page_cap_the_activity_tab_shows_the_newest_events(site: Site) -> None:
    """More events in the window than the tab's ten pages of 500: it asks newest first, so it
    shows the newest 5,000, and its note and caption say so. The list is answered here (5,000
    events would be slow to make), each page as the route pages newest first."""
    page = site.window(lead(site))
    asked: list[dict[str, list[str]]] = []
    now = datetime.now(UTC)

    def event(n: int) -> dict[str, Any]:  # the n-th newest
        return {
            "subject": f"key:k{n}",
            "event": "key.created",
            "payload": {"id": f"k{n}", "name": f"key-{n}", "org": "clinic-a", "created_by": "x"},
            "created_at": (now - timedelta(minutes=n)).isoformat(timespec="milliseconds"),
            "hash": f"{n:064x}",
        }

    def answer(route: Route) -> None:
        start = 500 * len(asked)
        asked.append(parse_qs(urlsplit(route.request.url).query))
        items = [event(n) for n in range(start, start + 500)]
        route.fulfill(json={"items": items, "next": f"after-{start + 499}"})

    page.route(re.compile(r"/api/audit/administrative\?"), answer)
    access(page, "activity")
    table = page.locator('[data-table="activity"]')
    expect(table.locator("caption")).to_have_text(
        "The newest 5,000 administrative events of Clinic-A of the last 90 days, newest first"
    )
    expect(page.locator("#activity > p.muted")).to_have_text(
        "The newest 5,000 events of these 90 days; the export has every one."
    )
    expect(table.locator("tbody tr")).to_have_count(5000)
    expect(table.locator("tbody tr").first).to_contain_text("API key key-0")
    expect(table.locator("tbody tr").last).to_contain_text("API key key-4999")
    assert len(asked) == 10 and all(query["order"] == ["desc"] for query in asked)
    assert [query.get("after") for query in asked[1:3]] == [["after-499"], ["after-999"]]


def test_an_agent_store_s_events_are_named_in_words(site: Site) -> None:
    """An event of the organization's agent store reads in words in both columns: what happened,
    and what it is about, never the name the chain keeps for either."""
    page = site.window(lead(site))
    now = datetime.now(UTC)
    kinds = [
        ("pack_version.promoted", "pack_version:7", "Agent store version 7"),
        ("store_key.added", "store_key:3", "Agent store author key 3"),
        ("store_dataset.created", "store_dataset:5", "Agent store dataset 5"),
        ("store_job.created", "store_job:9", "Agent store evaluation job 9"),
    ]
    events = [
        {
            "subject": subject,
            "event": kind,
            "payload": {"org": "clinic-a", "created_by": "lead@example.org"},
            "created_at": (now - timedelta(minutes=n)).isoformat(timespec="milliseconds"),
            "hash": f"{n:064x}",
        }
        for n, (kind, subject, _) in enumerate(kinds)
    ]
    page.route(
        re.compile(r"/api/audit/administrative\?"),
        lambda route: route.fulfill(json={"items": events, "next": None}),
    )
    access(page, "activity")
    rows = page.locator('[data-table="activity"] tbody tr')
    expect(rows).to_have_count(len(kinds))
    expect(rows.first.locator("td").nth(1)).to_have_text(
        "Agent store version promoted to be served"
    )
    for n, (_, _, about) in enumerate(kinds):
        expect(rows.nth(n).locator("td").nth(2)).to_have_text(about)


def test_a_viewer_has_neither_the_activity_nor_the_audit_export(site: Site) -> None:
    lead(site)
    viewer = person(site, sub="vi", email="vi@example.org", org="clinic-a", roles=["viewer"])
    page = site.window(viewer)
    asked: list[str] = []
    page.on(
        "request", lambda r: asked.append(r.url) if "/api/audit/administrative" in r.url else None
    )
    audit(page)
    expect(page.locator("#checkpoint")).to_have_text(
        "Signed daily checkpoint: not set up on this deployment."
    )
    expect(page.locator("#export-fhir")).to_have_count(0)
    expect(page.locator('.nav a[data-view="admin"]')).to_be_hidden()
    page.goto("/#admin/activity")  # the address alone opens no Access view for a viewer
    expect(page.locator("main h1")).not_to_have_text("Access")
    expect(page.locator("main h1")).to_be_visible()
    assert asked == []


def test_an_administrator_exports_the_pack_s_trail_as_fhir(site: Site) -> None:
    page = site.window(lead(site))
    page.add_init_script(f"localStorage.setItem('oh.pack', {json.dumps(TRIAGE)})")
    audit(page)
    with page.expect_download() as saved:
        page.get_by_role("button", name="Export as FHIR").click()
    assert saved.value.suggested_filename == (f"audit-mendari-test-triage-{today()}.ndjson")


def test_the_signed_checkpoint_is_shown_and_a_copy_can_be_kept(signing: Site) -> None:
    page = signing.window(lead(signing))
    audit(page)
    expect(page.locator("#checkpoint")).to_have_text("Signed daily checkpoint: none signed yet.")
    signing.store.record_checkpoint(signer=Ed25519PrivateKey.from_private_bytes(SEED))
    published = page.request.get("/api/audit/checkpoint").json()
    audit(page)
    line = page.locator("#checkpoint")
    expect(line).to_contain_text(f"Signed daily checkpoint for {published['statement']['date']}")
    expect(line).to_contain_text(published["key_id"])
    expect(line).to_contain_text(published["statement"]["head"][:12])
    with page.expect_download() as saved:
        line.get_by_role("button", name="Save a copy").click()
    assert saved.value.suggested_filename == f"checkpoint-{published['statement']['date']}.json"
    assert json.loads(Path(saved.value.path()).read_text()) == published


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_activity_tab_and_the_checkpoint_pass_axe_in_both_themes(
    signing: Site, scheme: str
) -> None:
    page = signing.window(lead(signing), bypass_csp=True, color_scheme=scheme)  # axe is injected
    keys(signing)
    access(page, "activity")
    expect(row(page, "activity", "API key revoked")).to_be_visible()
    assert violations(page) == []
    signing.store.record_checkpoint(signer=Ed25519PrivateKey.from_private_bytes(SEED))
    audit(page)
    expect(page.locator("#checkpoint")).to_contain_text("Signed daily checkpoint for")
    assert violations(page) == []
