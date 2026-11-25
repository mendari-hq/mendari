# SPDX-License-Identifier: Apache-2.0
"""The agent store in the dashboard (LP-20j): copy, edit, test, evaluate, compare and promote in
a second organization, the promoted version answering that organization alone; the author sees no
promote control and why; a refused upload shows kinds and places and no value; too few cases read
"insufficient evidence"; without the store the view is not there. The agent copied is the triage
test fixture.

A real server at `localhost` (tests/browser/access_site.py) in memberships mode with
`MENDARI_AGENT_STORE=on`. Run with `uv run pytest -m browser`."""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Browser, Page, expect

from mendari.core.agent import load_agent
from mendari.core.registry import resolve_agent
from mendari.hub import archive
from tests.browser.access_site import Site, memberships, person, running
from tests.browser.conftest import TRIAGE
from tests.browser.test_accessibility import violations
from tests.conftest import TRIAGE_DIR

pytestmark = pytest.mark.browser

CLINIC_A, CLINIC_B = "clinic-a", "clinic-b"
SECRET_PHONE = "617-555-2368"
OWN = {"x-mendari-request": "1"}


@pytest.fixture
def site(browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Site]:
    memberships(monkeypatch)
    monkeypatch.setenv("MENDARI_AGENT_STORE", "on")
    with running(browser, tmp_path / "a.db") as here:
        yield here


def store_tab(page: Page, tab: str) -> None:
    page.goto("about:blank")
    page.goto(f"/#store/{tab}")
    expect(page.locator("main h1")).to_have_text("Agent store")
    expect(page.locator(f'.tabs a[data-tab="{tab}"]')).to_have_attribute("aria-current", "page")
    expect(page.locator("#store-body")).not_to_have_attribute("aria-busy", "true")


def copy(page: Page, name: str = "triage-copy") -> str:
    store_tab(page, "agents")
    page.get_by_label("Agent to copy", exact=True).fill(TRIAGE)
    page.get_by_label("Name of the copy").fill(name)
    page.get_by_role("button", name="Copy", exact=True).click()
    expect(page).to_have_url(re.compile(r"#store/version/sha256"), timeout=20_000)
    expect(page.locator("main h1")).to_contain_text(name)
    return digest_of(page)


def digest_of(page: Page) -> str:
    return page.url.rsplit("/", 1)[-1].replace("%3A", ":")


def run_job(page: Page, *, version: str | None = None, agent: str | None = None) -> None:
    store_tab(page, "results")
    if agent:
        page.get_by_label("Or an agent served here (the current one)").fill(agent)
    else:
        page.get_by_label("Version").select_option(version)
    page.get_by_label("Dataset").select_option("name:test")
    page.get_by_role("button", name="Make the job").click()
    expect(page.locator('[data-table="store-jobs"] tbody tr')).not_to_have_count(0)
    run = page.locator("button[data-advance]").first
    run.click()
    expect(page.locator("button[data-advance]")).to_have_count(0, timeout=120_000)


def compare(page: Page) -> None:
    """The draft's job against the served agent's, on the same test set."""
    store_tab(page, "compare")
    options = page.locator("#compare-draft option")
    named = {o.inner_text(): str(o.get_attribute("value")) for o in options.all()}
    current = next(v for t, v in named.items() if t.startswith(TRIAGE))
    draft = next(v for t, v in named.items() if not t.startswith(TRIAGE))
    page.get_by_label("Draft", exact=True).select_option(draft)
    page.get_by_label("Current", exact=True).select_option(current)
    page.get_by_role("button", name="Compare").click()


def decide(site: Site, headers: dict[str, str], agent_id: str) -> dict[str, object]:
    with httpx.Client(base_url=site.base, headers={**headers, **OWN}) as client:
        client.post("/api/notice/acknowledge", json={})
        answer = client.post(
            f"/api/agents/{agent_id}/decide", json={"message": {"body": "Refill please"}}
        )
    assert answer.status_code == 200, answer.text
    return dict(answer.json())


def test_copy_edit_test_compare_and_promote_in_a_second_organization(site: Site) -> None:
    """In clinic-b an author copies the triage agent, edits a rule, runs the tests and two jobs,
    reads the comparison; a second person promotes; a decide request from clinic-b is answered by
    the promoted digest, and one from clinic-a by the served agent."""
    author = site.window(
        person(site, sub="aria", email="aria@example.org", org=CLINIC_B, roles=["agent_author"])
    )
    first = copy(author)
    author.get_by_label("File").select_option("logic/router_rules.yaml")
    content = author.get_by_label("Content")
    expect(content).not_to_have_value("")
    content.fill(content.input_value() + "\n# reviewed by the clinic\n")
    author.get_by_role("button", name="Save as a new draft").click()
    expect(author).not_to_have_url(re.compile(re.escape(first.replace(":", "%3A"))), timeout=20_000)
    expect(author.locator("main h1")).to_contain_text("triage-copy")
    draft = digest_of(author)
    author.get_by_role("button", name="Run the tests").click()
    expect(author.locator("#tests-result")).to_contain_text("passed", timeout=20_000)
    run_job(author, version=draft)
    run_job(author, agent=TRIAGE)
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
    expect(approver.locator('[data-table="promotions"]')).to_contain_text(f"{CLINIC_B}/triage-copy")

    b = person(site, sub="rae", email="rae@example.org", org=CLINIC_B, roles=["reviewer"])
    assert decide(site, b, f"{CLINIC_B}/triage-copy")["agent"]["digest"] == draft  # type: ignore[index]
    a = person(site, sub="ana", email="ana@example.org", org=CLINIC_A, roles=["reviewer"])
    assert decide(site, a, TRIAGE)["agent"]["digest"] == resolve_agent(TRIAGE).digest  # type: ignore[index]
    with httpx.Client(base_url=site.base, headers={**a, **OWN}) as client:
        assert client.post(f"/api/agents/{CLINIC_B}/triage-copy/decide", json={}).status_code == 404


def test_the_author_sees_no_promote_control_and_why(site: Site) -> None:
    both = person(
        site,
        sub="dual",
        email="dual@example.org",
        org=CLINIC_B,
        roles=["governance", "agent_author"],
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
        roles=["governance", "agent_author"],
    )
    page = site.window(both, bypass_csp=True, color_scheme=scheme)  # axe is injected
    found = {}
    digest = copy(page)
    found["a version"] = violations(page)
    page.get_by_role("button", name="Run the tests").click()
    expect(page.locator("#tests-result")).to_contain_text("passed", timeout=20_000)
    found["its tests"] = violations(page)
    run_job(page, version=digest)
    run_job(page, agent=TRIAGE)
    found["results"] = violations(page)
    compare(page)
    expect(page.locator("#compare-result")).to_contain_text("cases paired")
    found["a comparison"] = violations(page)
    store_tab(page, "promote")
    found["promote"] = violations(page)
    store_tab(page, "agents")
    found["agents"] = violations(page)
    assert found == {view: [] for view in found}


def _signed(site: Site, tmp_path: Path, extra_case: dict[str, object]) -> bytes:
    """A signed archive of the triage agent in clinic-b's namespace with one more test case, and
    clinic-b's author key registered by its administrator."""
    private, public = archive.generate_key()
    admin = person(site, sub="lead-b", email="lead-b@example.org", org=CLINIC_B, roles=["admin"])
    with httpx.Client(base_url=site.base, headers={**admin, **OWN}) as client:
        assert (
            client.post("/api/store/keys", json={"public_pem": public.decode()}).status_code == 201
        )
    root = tmp_path / "agent"
    shutil.copytree(TRIAGE_DIR, root)
    manifest = yaml.safe_load((root / "agent.yaml").read_text())
    manifest["id"] = f"{CLINIC_B}/uploaded"
    (root / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    cases = root / "tests" / "cases.yaml"
    case = {"name": "planted", "input": extra_case, "expect": {"not_outcome": "BLOCK"}}
    cases.write_text(cases.read_text() + "\n" + yaml.safe_dump([case], sort_keys=False))
    return archive.build(load_agent(root), archive.load_private_key(private))


def _upload(page: Page, tmp_path: Path, data: bytes) -> None:
    store_tab(page, "agents")
    path = tmp_path / f"upload{archive.EXTENSION}"  # named as `mendari archive build` names one
    path.write_bytes(data)
    page.get_by_label("Archive").set_input_files(str(path))
    page.get_by_role("button", name="Upload", exact=True).click()
    expect(page.locator("#upload-result")).not_to_be_empty(timeout=20_000)


def test_a_refused_upload_shows_kinds_and_places_and_no_value(site: Site, tmp_path: Path) -> None:
    data = _signed(site, tmp_path, {"message": {"body": f"call me at {SECRET_PHONE}"}})
    page = site.window(
        person(site, sub="aria", email="aria@example.org", org=CLINIC_B, roles=["agent_author"])
    )
    _upload(page, tmp_path, data)
    result = page.locator("#upload-result")
    expect(result).to_contain_text("Refused")
    expect(result).to_contain_text("phone")
    expect(result).to_contain_text("tests/cases.yaml")
    assert SECRET_PHONE not in page.content()


def test_the_agents_tab_names_how_an_archive_is_built(site: Site) -> None:
    """The Agents tab (`#store/agents`) names the command that builds an archive, and offers an
    archive by the extension it is written with."""
    page = site.window(
        person(site, sub="aria", email="aria@example.org", org=CLINIC_B, roles=["agent_author"])
    )
    page.goto("about:blank")
    page.goto("/#store/agents")
    expect(page.locator("main h1")).to_have_text("Agent store")
    expect(page.locator('.tabs a[data-tab="agents"]')).to_have_attribute("aria-current", "page")
    expect(page.get_by_role("heading", name="Copy an agent")).to_be_visible()
    expect(page.locator("#upload-form")).to_contain_text("Built with mendari archive build")
    # The file chooser offers an archive by the extension it is written with
    # (`hub/archive.py` `EXTENSION`).
    offered = f"{archive.EXTENSION},application/gzip"
    expect(page.locator("#upload-file")).to_have_attribute("accept", offered)


def test_a_comparison_on_too_few_cases_says_insufficient_evidence(site: Site) -> None:
    page = site.window(
        person(site, sub="aria", email="aria@example.org", org=CLINIC_B, roles=["agent_author"])
    )
    digest = copy(page)
    run_job(page, version=digest)
    run_job(page, agent=TRIAGE)
    compare(page)
    expect(page.locator("#compare-result")).to_contain_text("Insufficient evidence")


def test_without_the_store_the_view_is_not_there(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memberships(monkeypatch)
    monkeypatch.delenv("MENDARI_AGENT_STORE", raising=False)
    with running(browser, tmp_path / "b.db") as here:
        page = here.window(
            person(here, sub="lea", email="lea@example.org", org=CLINIC_A, roles=["admin"])
        )
        page.goto("/#store")
        expect(page.locator('.nav a[data-view="store"]')).to_be_hidden()
        expect(page.locator("main h1")).not_to_have_text("Agent store")
        author = here.window(
            person(here, sub="pia", email="pia@example.org", org=CLINIC_A, roles=["agent_author"])
        )
        author.goto("/#store")
        expect(author.locator("main h1")).to_have_text("Nothing to do here yet")
