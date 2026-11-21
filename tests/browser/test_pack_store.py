# SPDX-License-Identifier: Apache-2.0
"""The pack store in the dashboard (LP-20j): copy, edit, test, evaluate, compare and promote in a
second organization, the promoted version answering that organization alone; the author sees no
promote control and why; a refused upload shows kinds and places and no value; a held upload says
only that it is held; too few cases read "insufficient evidence"; without the store the view is
not there.

A real server at `localhost` (tests/browser/access_site.py) in memberships mode with
`MENDARI_PACK_STORE=on`. Run with `uv run pytest -m browser`."""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Browser, Page, expect

from mendari.core.pack import load_pack
from mendari.core.registry import resolve_pack
from mendari.hub import archive
from tests.browser.access_site import Site, memberships, person, running
from tests.browser.test_accessibility import violations
from tests.conftest import INBOX_DIR

pytestmark = pytest.mark.browser

INBOX = "mendari/patient-inbox-router"
CLINIC_A, CLINIC_B = "clinic-a", "clinic-b"
SECRET_PHONE = "617-555-2368"
OWN = {"x-mendari-request": "1"}


@pytest.fixture
def site(browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Site]:
    memberships(monkeypatch)
    monkeypatch.setenv("MENDARI_PACK_STORE", "on")
    with running(browser, tmp_path / "a.db") as here:
        yield here


def store_tab(page: Page, tab: str) -> None:
    page.goto("about:blank")
    page.goto(f"/#store/{tab}")
    expect(page.locator("main h1")).to_have_text("Pack store")
    expect(page.locator(f'.tabs a[data-tab="{tab}"]')).to_have_attribute("aria-current", "page")
    expect(page.locator("#store-body")).not_to_have_attribute("aria-busy", "true")


def copy(page: Page, name: str = "inbox-router") -> str:
    store_tab(page, "packs")
    page.get_by_label("Pack", exact=True).fill(INBOX)
    page.get_by_label("Name of the copy").fill(name)
    page.get_by_role("button", name="Copy", exact=True).click()
    expect(page).to_have_url(re.compile(r"#store/version/sha256"), timeout=20_000)
    expect(page.locator("main h1")).to_contain_text(name)
    return digest_of(page)


def digest_of(page: Page) -> str:
    return page.url.rsplit("/", 1)[-1].replace("%3A", ":")


def run_job(page: Page, *, version: str | None = None, pack: str | None = None) -> None:
    store_tab(page, "results")
    if pack:
        page.get_by_label("Or a pack served here (the current one)").fill(pack)
    else:
        page.get_by_label("Version").select_option(version)
    page.get_by_label("Dataset").select_option("name:test")
    page.get_by_role("button", name="Make the job").click()
    expect(page.locator('[data-table="store-jobs"] tbody tr')).not_to_have_count(0)
    run = page.locator("button[data-advance]").first
    run.click()
    expect(page.locator("button[data-advance]")).to_have_count(0, timeout=120_000)


def compare(page: Page) -> None:
    """The draft's job against the built-in pack's, on the same test set."""
    store_tab(page, "compare")
    options = page.locator("#compare-draft option")
    named = {o.inner_text(): str(o.get_attribute("value")) for o in options.all()}
    current = next(v for t, v in named.items() if t.startswith(INBOX))
    draft = next(v for t, v in named.items() if not t.startswith(INBOX))
    page.get_by_label("Draft", exact=True).select_option(draft)
    page.get_by_label("Current", exact=True).select_option(current)
    page.get_by_role("button", name="Compare").click()


def decide(site: Site, headers: dict[str, str], pack_id: str) -> dict[str, object]:
    with httpx.Client(base_url=site.base, headers={**headers, **OWN}) as client:
        client.post("/api/notice/acknowledge", json={})
        answer = client.post(
            f"/api/packs/{pack_id}/decide", json={"message": {"body": "Refill please"}}
        )
    assert answer.status_code == 200, answer.text
    return dict(answer.json())


def test_copy_edit_test_compare_and_promote_in_a_second_organization(site: Site) -> None:
    """In clinic-b an author copies the inbox pack, edits a rule, runs the tests and two jobs,
    reads the comparison; a second person promotes; a decide request from clinic-b is answered by
    the promoted digest, and one from clinic-a by the built-in pack."""
    author = site.window(
        person(site, sub="aria", email="aria@example.org", org=CLINIC_B, roles=["pack_author"])
    )
    first = copy(author)
    author.get_by_label("File").select_option("logic/routing_rules.yaml")
    content = author.get_by_label("Content")
    expect(content).not_to_have_value("")
    content.fill(content.input_value() + "\n# reviewed by the clinic\n")
    author.get_by_role("button", name="Save as a new draft").click()
    expect(author).not_to_have_url(re.compile(re.escape(first.replace(":", "%3A"))), timeout=20_000)
    expect(author.locator("main h1")).to_contain_text("inbox-router")
    draft = digest_of(author)
    author.get_by_role("button", name="Run the tests").click()
    expect(author.locator("#tests-result")).to_contain_text("passed", timeout=20_000)
    run_job(author, version=draft)
    run_job(author, pack=INBOX)
    compare(author)
    expect(author.locator("#compare-result")).to_contain_text("cases paired")

    approver = site.window(
        person(site, sub="gus", email="gus@example.org", org=CLINIC_B, roles=["governance"])
    )
    store_tab(approver, "promote")
    form = approver.locator(f'form[data-promote="{draft}"]')
    form.get_by_label("Reference (where the sign-off is recorded)").fill("CAB-2026-11")
    form.get_by_role("button", name="Promote…").click()
    dialog = approver.locator("dialog[open]")
    expect(dialog).to_contain_text("clinic-b")
    dialog.get_by_role("button", name="Promote").click()
    expect(approver.locator('[data-table="promotions"]')).to_contain_text(
        f"{CLINIC_B}/inbox-router"
    )

    b = person(site, sub="rae", email="rae@example.org", org=CLINIC_B, roles=["reviewer"])
    assert decide(site, b, f"{CLINIC_B}/inbox-router")["pack"]["digest"] == draft  # type: ignore[index]
    a = person(site, sub="ana", email="ana@example.org", org=CLINIC_A, roles=["reviewer"])
    assert decide(site, a, INBOX)["pack"]["digest"] == resolve_pack(INBOX).digest  # type: ignore[index]
    with httpx.Client(base_url=site.base, headers={**a, **OWN}) as client:
        assert client.post(f"/api/packs/{CLINIC_B}/inbox-router/decide", json={}).status_code == 404


def test_the_author_sees_no_promote_control_and_why(site: Site) -> None:
    both = person(
        site,
        sub="dual",
        email="dual@example.org",
        org=CLINIC_B,
        roles=["governance", "pack_author"],
    )
    page = site.window(both)
    digest = copy(page)
    run_job(page, version=digest)
    store_tab(page, "promote")
    expect(page.locator(f'form[data-promote="{digest}"]')).to_have_count(0)
    expect(page.locator("#store-body")).to_contain_text("someone else signs it off")


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_store_passes_axe(site: Site, scheme: str) -> None:
    both = person(
        site,
        sub="dual",
        email="dual@example.org",
        org=CLINIC_B,
        roles=["governance", "pack_author"],
    )
    page = site.window(both, bypass_csp=True, color_scheme=scheme)  # axe is injected
    found = {}
    digest = copy(page)
    found["a version"] = violations(page)
    page.get_by_role("button", name="Run the tests").click()
    expect(page.locator("#tests-result")).to_contain_text("passed", timeout=20_000)
    found["its tests"] = violations(page)
    run_job(page, version=digest)
    run_job(page, pack=INBOX)
    found["results"] = violations(page)
    compare(page)
    expect(page.locator("#compare-result")).to_contain_text("cases paired")
    found["a comparison"] = violations(page)
    store_tab(page, "promote")
    found["promote"] = violations(page)
    store_tab(page, "packs")
    found["packs"] = violations(page)
    assert found == {view: [] for view in found}


def _signed(site: Site, tmp_path: Path, extra_case: dict[str, object]) -> bytes:
    """A signed archive of the inbox pack in clinic-b's namespace with one more test case, and
    clinic-b's author key registered by its administrator."""
    private, public = archive.generate_key()
    admin = person(site, sub="lead-b", email="lead-b@example.org", org=CLINIC_B, roles=["admin"])
    with httpx.Client(base_url=site.base, headers={**admin, **OWN}) as client:
        assert (
            client.post("/api/store/keys", json={"public_pem": public.decode()}).status_code == 201
        )
    root = tmp_path / "pack"
    shutil.copytree(INBOX_DIR, root)
    manifest = yaml.safe_load((root / "pack.yaml").read_text())
    manifest["id"] = f"{CLINIC_B}/uploaded"
    (root / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    cases = root / "tests" / "cases.yaml"
    case = {"name": "planted", "input": extra_case, "expect": {"not_outcome": "BLOCK"}}
    cases.write_text(cases.read_text() + "\n" + yaml.safe_dump([case], sort_keys=False))
    return archive.build(load_pack(root), archive.load_private_key(private))


def _upload(page: Page, tmp_path: Path, data: bytes) -> None:
    store_tab(page, "packs")
    path = tmp_path / "upload.ohpack"
    path.write_bytes(data)
    page.get_by_label("Archive").set_input_files(str(path))
    page.get_by_role("button", name="Upload", exact=True).click()
    expect(page.locator("#upload-result")).not_to_be_empty(timeout=20_000)


def test_a_refused_upload_shows_kinds_and_places_and_no_value(site: Site, tmp_path: Path) -> None:
    data = _signed(site, tmp_path, {"message": {"body": f"call me at {SECRET_PHONE}"}})
    page = site.window(
        person(site, sub="aria", email="aria@example.org", org=CLINIC_B, roles=["pack_author"])
    )
    _upload(page, tmp_path, data)
    result = page.locator("#upload-result")
    expect(result).to_contain_text("Refused")
    expect(result).to_contain_text("phone")
    expect(result).to_contain_text("tests/cases.yaml")
    assert SECRET_PHONE not in page.content()


def test_a_held_upload_says_only_that_it_is_held(
    site: Site, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without the hidden sets' keys this deployment holds no hidden case: the check is "not
    # applicable" and nothing is held. A held version, as the store writes one, says only that.
    data = _signed(site, tmp_path, {"message": {"body": "a plain synthetic message"}})
    page = site.window(
        person(site, sub="aria", email="aria@example.org", org=CLINIC_B, roles=["pack_author"])
    )
    _upload(page, tmp_path, data)
    expect(page.locator("#upload-result")).to_contain_text("A draft")
    with site.store._db.session(exclusive=True) as db:  # held, as the hidden-set check holds one
        db.all(
            "UPDATE store_versions SET state = 'held' WHERE pack_id = ?", (f"{CLINIC_B}/uploaded",)
        )
    store_tab(page, "packs")
    row = page.locator(f'[data-table="store-{CLINIC_B}/uploaded"] tbody tr')
    expect(row).to_contain_text("Held for the operator's review")
    expect(row).not_to_contain_text("hidden")


def test_a_comparison_on_too_few_cases_says_insufficient_evidence(site: Site) -> None:
    page = site.window(
        person(site, sub="aria", email="aria@example.org", org=CLINIC_B, roles=["pack_author"])
    )
    digest = copy(page)
    run_job(page, version=digest)
    run_job(page, pack=INBOX)
    compare(page)
    expect(page.locator("#compare-result")).to_contain_text("Insufficient evidence")


def test_without_the_store_the_view_is_not_there(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memberships(monkeypatch)
    monkeypatch.delenv("MENDARI_PACK_STORE", raising=False)
    with running(browser, tmp_path / "b.db") as here:
        page = here.window(
            person(here, sub="lea", email="lea@example.org", org=CLINIC_A, roles=["admin"])
        )
        page.goto("/#store")
        expect(page.locator('.nav a[data-view="store"]')).to_be_hidden()
        expect(page.locator("main h1")).not_to_have_text("Pack store")
        author = here.window(
            person(here, sub="pia", email="pia@example.org", org=CLINIC_A, roles=["pack_author"])
        )
        author.goto("/#store")
        expect(author.locator("main h1")).to_have_text("Nothing to do here yet")
