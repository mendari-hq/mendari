# SPDX-License-Identifier: Apache-2.0
"""The public site's legal pages (web/terms.html, privacy.html, acceptable-use.html): drafts for the
owner and counsel. Each exists, says it is a draft pending legal review with the day it was last
updated, is linked from every page's footer, and keeps the site's rules: the notice, no scripts, no
inline styles, nothing loaded from another origin, every link inside the site leading somewhere.
Each fact still to be supplied is a marked placeholder, and the note for the owner and counsel
lists every one."""

from __future__ import annotations

import datetime
import itertools
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from mendari import notices

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
# Each page's file name, with its heading.
LEGAL = {
    "terms": "Terms of use",
    "privacy": "Privacy notice",
    "acceptable-use": "Acceptable use policy",
}
PAGES = ["index", *LEGAL]
DRAFT = "Draft, pending legal review. Not yet in effect."
QUESTIONS = ROOT / "notes" / "legal-pages-open-questions.md"
VOID = {"meta", "link", "br", "hr", "img", "input", "path", "rect"}


class Page(HTMLParser):
    """What a check needs of one page: its tags with their attributes, each element's text by
    class, its ids, and the text of the page."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source = source
        self.tags: list[tuple[str, dict[str, str]]] = []
        self.ids: set[str] = set()
        self.by_class: dict[str, list[str]] = {}
        self.by_tag: dict[str, list[str]] = {}
        self.text: list[str] = []
        self._open: list[tuple[str, list[str], list[str]]] = []
        self.feed(source)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = {name: value or "" for name, value in attrs}
        self.tags.append((tag, found))
        if "id" in found:
            self.ids.add(found["id"])
        if tag not in VOID:
            self._open.append((tag, found.get("class", "").split(), []))

    def handle_endtag(self, tag: str) -> None:
        while self._open:
            name, classes, parts = self._open.pop()
            text = " ".join("".join(parts).split())
            self.by_tag.setdefault(name, []).append(text)
            for each in classes:
                self.by_class.setdefault(each, []).append(text)
            if name == tag:
                break

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        for _, _, parts in self._open:
            parts.append(data)

    def attrs(self, tag: str) -> list[dict[str, str]]:
        return [found for name, found in self.tags if name == tag]

    def within(self, tag: str, classes: str) -> list[dict[str, str]]:
        """The links inside the first `tag` of class `classes`, in order."""
        found = re.search(rf'<{tag} class="{classes}"[^>]*>(.*?)</{tag}>', self.source, re.S)
        assert found, f"no <{tag} class={classes!r}>"
        return Page(found.group(1)).attrs("a")


def page(name: str) -> Page:
    return Page((WEB / f"{name}.html").read_text(encoding="utf-8"))


def target(href: str) -> tuple[Path, str]:
    """The file of web/ a root path leads to, as the host serves it (`cleanUrls`), and the
    fragment."""
    path, _, fragment = href.partition("#")
    name = path.strip("/") or "index"
    file = WEB / name
    return (file if file.suffix else file.with_suffix(".html")), fragment


@pytest.mark.parametrize("name", LEGAL)
def test_each_page_is_a_draft_with_the_day_it_was_last_updated(name: str) -> None:
    found = page(name)
    assert found.by_class.get("draft"), "the draft marker is missing"
    marker = found.by_class["draft"][0]
    assert marker.startswith(DRAFT)
    assert "not legal advice" in marker
    # The marker comes before the page's first section, so nobody reads a term before it.
    source = found.source
    assert source.index('class="draft"') < source.index("<h2")
    assert found.by_tag["h1"] == [LEGAL[name]]
    assert found.by_tag["title"] == [f"{LEGAL[name]} (draft) · Mendari"]
    (updated,) = [each for each in found.attrs("time") if "datetime" in each]
    day = datetime.date.fromisoformat(updated["datetime"])
    assert datetime.date(2026, 11, 10) <= day <= datetime.date.today()
    assert "Last updated" in " ".join(found.text)


@pytest.mark.parametrize("name", LEGAL)
def test_each_page_keeps_the_sites_rules(name: str) -> None:
    found = page(name)
    source = found.source
    assert source.startswith("<!doctype html>\n<!-- SPDX-License-Identifier: Apache-2.0 -->\n")
    assert found.attrs("html") == [{"lang": "en"}]
    # The notice every surface carries, word for word.
    assert found.by_class["notice"] == [notices.NOTICE]
    # No scripts and no inline styles: the site's CSP (web/vercel.json) allows neither.
    assert not found.attrs("script") and not found.attrs("style")
    assert not [tag for tag, attrs in found.tags if "style" in attrs or "onclick" in attrs]
    # Nothing is loaded from anywhere but the site.
    loaded = [each["href"] for each in found.attrs("link") if each.get("rel") != "canonical"]
    loaded += [each["src"] for tag, each in found.tags if "src" in each]
    assert loaded and all(re.fullmatch(r"/[\w./-]+", each) for each in loaded), loaded
    canonical = [each["href"] for each in found.attrs("link") if each.get("rel") == "canonical"]
    assert canonical == [f"https://mendari.co/{name}"]
    # A draft is not for search engines yet.
    robots = [each["content"] for each in found.attrs("meta") if each.get("name") == "robots"]
    assert robots == ["noindex"]
    # One h1, a skip link to the main content, and headings that only step down by one.
    assert "main" in found.ids and {"href": "#main", "class": "skip"} in found.attrs("a")
    levels = [int(tag[1]) for tag, _ in found.tags if re.fullmatch(r"h[1-6]", tag)]
    assert levels[0] == 1 and levels.count(1) == 1
    assert all(later - earlier <= 1 for earlier, later in itertools.pairwise(levels))


@pytest.mark.parametrize("name", PAGES)
def test_every_footer_links_the_three_pages(name: str) -> None:
    links = page(name).within("nav", "foot-links")
    assert [each["href"] for each in links] == [f"/{legal}" for legal in LEGAL]


@pytest.mark.parametrize("name", PAGES)
def test_every_link_inside_the_site_leads_somewhere(name: str) -> None:
    found = page(name)
    for link in found.attrs("a"):
        href = link["href"]
        if href.startswith("#"):
            assert href[1:] in found.ids, href
        elif href.startswith("/"):
            file, fragment = target(href)
            assert file.is_file(), href
            if fragment:
                assert fragment in page(file.stem).ids, href
        else:
            # Outside the site: https only, and opened without handing over this page.
            assert href.startswith("https://"), href
            assert link.get("rel") == "noopener noreferrer", href


def test_the_pages_point_at_each_other_and_at_the_projects_own_texts() -> None:
    terms, privacy, use = (" ".join(page(name).source.split()) for name in LEGAL)
    assert 'href="/privacy"' in terms and 'href="/acceptable-use"' in terms
    assert 'href="/terms"' in privacy and 'href="/terms"' in use
    # The licence, not these terms, governs the code; the security policy is the reporting route.
    assert "https://www.apache.org/licenses/LICENSE-2.0" in terms
    assert "https://github.com/mendari-hq/mendari/security/advisories/new" in use
    assert "https://developercertificate.org" in terms
    for text in (terms, privacy, use):
        assert "synthetic" in text and "real patient" in text


def placeholders() -> set[str]:
    found: set[str] = set()
    for name in LEGAL:
        found.update(page(name).by_class.get("placeholder", []))
    return found


def test_every_placeholder_is_marked_and_listed_for_the_owner_and_counsel() -> None:
    marked = placeholders()
    assert marked, "the drafts name facts only the owner or counsel can supply"
    assert all(re.fullmatch(r"\[[a-z][a-z ,'-]+\]", each) for each in marked), marked
    # A bracketed gap anywhere on a page is a marked placeholder, never bare text.
    for name in LEGAL:
        found = page(name)
        bare = set(re.findall(r"\[[a-z][a-z ,'-]+\]", " ".join(found.text)))
        assert bare <= set(found.by_class.get("placeholder", [])), name
    note = QUESTIONS.read_text(encoding="utf-8")
    assert not [each for each in sorted(marked) if f"`{each}`" not in note]
    listed = set(re.findall(r"`(\[[a-z][a-z ,'-]+\])`", note))
    assert listed == marked, "the note lists a placeholder no page has"
    assert "legal-pages-open-questions.md" in (ROOT / "notes" / "README.md").read_text()


def luminance(colour: str) -> float:
    parts = [int(colour[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        part / 12.92 if part <= 0.04045 else ((part + 0.055) / 1.055) ** 2.4 for part in parts
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def test_the_pages_text_colours_meet_wcag_aa_in_both_themes() -> None:
    css = (WEB / "style.css").read_text(encoding="utf-8")
    light = css[css.index(":root {") : css.index("@media (prefers-color-scheme: dark)")]
    dark = css[css.index("@media (prefers-color-scheme: dark)") : css.index("*,\n*::before")]
    # Text on its background, as the legal pages use them: prose, links, the footer, the draft
    # marker and a placeholder.
    pairs = [
        ("ink", "paper"),
        ("ink-2", "paper"),
        ("muted", "paper"),
        ("accent", "paper"),
        ("review-text", "review-soft"),
    ]
    for theme in (light, dark):
        tokens = dict(re.findall(r"--([\w-]+): (#[0-9a-f]{6});", theme))
        for text, ground in pairs:
            high, low = sorted((luminance(tokens[text]), luminance(tokens[ground])), reverse=True)
            assert (high + 0.05) / (low + 0.05) >= 4.5, (text, ground)
    # The pages' own rules name colours by token only.
    rules = css[css.index("/* Legal pages") : css.index("/* Footer */")]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", rules)
