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

from mendari.core.registry import SearchPath
from mendari.server.app import create_app
from tests.browser.conftest import serving
from tests.browser.test_dashboard import CONTRAST, PHONE, show
from tests.conftest import INBOX_DIR, ROOT

pytestmark = pytest.mark.browser

LABEL = "Community · not clinically reviewed"
SORTER = "community/visit-sorter"
FIRST_MAINTAINED = "mendari/ai-guard"  # by id; every community/ id sorts before it


@pytest.fixture(scope="module")
def community_site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """The shipped packs and one community pack, as a deployment with
    MENDARI_COMMUNITY_PACKS=on serves them."""
    tmp = Path(tmp_path_factory.mktemp("community"))
    base = tmp / "community-packs"
    fixture = ROOT / "tests" / "fixtures" / "community-packs" / "visit-sorter"
    shutil.copytree(fixture, base / "visit-sorter")
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
    expect(groups.nth(1).locator("option")).to_have_count(1)
    assert groups.nth(1).locator("option").get_attribute("value") == SORTER
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
    expect(site.locator("#card-facts")).to_contain_text("Never: a community pack recommends")
    expect(site.locator("#card-facts")).to_contain_text("(community, self-declared)")
    expect(site.locator("#card-approval")).to_contain_text("A community pack is never automated")
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
    item = site.locator(".docs-cards li", has=site.locator(f'a[href="/?pack={SORTER}#card"]'))
    expect(item.locator(".badge.origin-community")).to_have_text(LABEL)
    expect(site.locator(".docs-cards .badge.origin-community")).to_have_count(1)


def test_the_labels_fit_a_phone(site: Page) -> None:
    site.set_viewport_size(PHONE)
    _open(site, SORTER, "overview")
    for view in ("overview", "card", "try"):
        show(site, view)
        shown = site.locator("main .badge.origin-community, main .community-notice")
        expect(shown.first).to_be_visible()
        assert site.evaluate("document.documentElement.scrollWidth") <= PHONE["width"], view
