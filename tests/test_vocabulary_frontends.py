# SPDX-License-Identifier: Apache-2.0
"""One vocabulary in the three front ends (tasks/todo.md, "Plan: the engineering to A+"): the web
console (`src/mendari/dashboard`), the Agent Hub (`hub/`) and the site (`web/`) say agent, never
"pack" or "Decision Pack", wherever a person reads.

**The rule** is `tests/test_vocabulary.py`'s (`offences`), written there and nowhere else. It is
applied here to what each front end shows (`SURFACES`):

- **A page** (an `.html` file of the console or the site): its text, and the attributes a person
  reads (`READ`: a tooltip, the name a screen reader says, a field's hint, an image's words, a
  `<meta>`'s content). Markup is not read: a class, an id, an address. A script written in the
  page is read as a script.
- **A script** (the console's `.js`, the Hub's `.ts` and `.tsx`): the prose in its string and
  template literals, and JSX text. A literal that holds whitespace is prose; one that holds none
  is an identifier (a key, a path, a selector), unless it is the old word with a capital, a
  one-word label (`"Packs"`). A literal that holds markup (the console builds its screens from
  template literals) is read as a page is: its text and those attributes. Comments are not read.
- **An API example** (the console's `snippets/`), which the developer page shows as written.

**A wire name stays** (`pack_id`, the `pack` parameter of `$decide`: the plan's wave 6 renames
them). The rule lets one through when it is written as code, which a page does with `<code>`.

**What says the old word on purpose** is listed with its reason in `ALLOWED`. An entry that
is no longer said fails here, so the list never outlives its reasons.

The addresses the console writes and reads (`?agent=`, `#store/agents`) are held by the browser
tests, which see the address bar."""

from __future__ import annotations

import html
import json
import os
import re
from bisect import bisect_right
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

import pytest

from tests.conftest import ROOT
from tests.test_vocabulary import offences

CONSOLE = ROOT / "src" / "mendari" / "dashboard"
HUB = ROOT / "hub"
SITE = ROOT / "web"
# Folders that are not a front end's own words: others' code, and what a build or a test run
# writes.
NOT_OURS = frozenset({"vendor", "node_modules", ".next", "playwright-report", "test-results"})
PAGES = (".html",)
SCRIPTS = (".js", ".mjs", ".jsx", ".ts", ".tsx")
EXAMPLES = (".sh",)


def files(root: Path, *suffixes: str) -> list[Path]:
    """The files under `root` with one of `suffixes`, outside `NOT_OURS`."""
    found: list[Path] = []
    for folder, folders, names in os.walk(root):
        folders[:] = sorted(name for name in folders if name not in NOT_OURS)
        found += [Path(folder) / name for name in names if Path(name).suffix in suffixes]
    return sorted(found)


def prose(text: str) -> bool:
    """Whether a literal is prose: it holds whitespace (an identifier, a key, a path or a
    selector holds none), or it is the old word with a capital, a one-word label (`"Packs"`)."""
    return bool(re.search(r"\s", text)) or any(word[:1].isupper() for word in offences(text))


# -- a page ---------------------------------------------------------------------------------------

# What a person reads of an element besides its text.
READ = (
    "title",  # a tooltip
    "aria-label",  # the name a screen reader says
    "aria-description",
    "aria-roledescription",
    "aria-valuetext",
    "aria-placeholder",
    "placeholder",  # a field's hint
    "alt",  # an image's words
    "content",  # a <meta>'s: the description, a link preview's title
    "label",  # a group of options' name
)
_BUTTONS = ("button", "submit", "reset")  # an <input> of these types shows its `value`
# Elements inside a sentence: its text runs through them.
_INLINE = frozenset(
    {"a", "abbr", "b", "bdi", "bdo", "cite", "code", "data", "del", "dfn", "em", "i", "ins"}
    | {"kbd", "mark", "q", "s", "samp", "small", "span", "strong", "sub", "sup", "time", "u"}
    | {"var", "wbr"}
)
_CODE = frozenset({"code", "kbd", "samp"})  # written as code: the rule lets a wire name through
_JAVASCRIPT = ("", "module", "text/javascript", "application/javascript")
_JSON = ("application/json", "application/ld+json")


class Page(HTMLParser):
    """A page, or a piece of one, as a person meets it. `said` holds `(line, text)` for each run
    of text (a sentence with the inline elements it runs through, a `<code>`'s text in backticks)
    and each attribute of `READ`; `scripts` holds `(line, type, source)` for each script written
    in it."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.said: list[tuple[int, str]] = []
        self.scripts: list[tuple[int, str, str]] = []
        self._run: list[str] = []
        self._run_line = 1
        self._raw: tuple[str, str, int] | None = None  # in a <script> or a <style>
        self._raw_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        line = self.getpos()[0]
        given = {name: value or "" for name, value in attrs}
        shown = [given.get(name, "") for name in READ]
        if tag == "input" and given.get("type") in _BUTTONS:
            shown.append(given.get("value", ""))
        self.said += [(line, " ".join(value.split())) for value in shown if value.strip()]
        if tag in ("script", "style"):
            self._end_run()
            self._raw, self._raw_text = (tag, given.get("type", "").strip().lower(), line), []
        elif tag in _CODE:
            self._add("`", line)
        elif tag == "br":
            self._add(" ", line)
        elif tag not in _INLINE:
            self._end_run()

    def handle_endtag(self, tag: str) -> None:
        if self._raw is not None:
            if tag == self._raw[0]:
                if tag == "script":
                    self.scripts.append((self._raw[2], self._raw[1], "".join(self._raw_text)))
                self._raw = None
        elif tag in _CODE:
            self._add("`", self.getpos()[0])
        elif tag not in _INLINE:
            self._end_run()

    def handle_data(self, data: str) -> None:
        if self._raw is not None:
            self._raw_text.append(data)
        else:
            self._add(data, self.getpos()[0])

    def close(self) -> None:
        super().close()
        self._end_run()

    def _add(self, text: str, line: int) -> None:
        if not "".join(self._run).strip():  # its first word: the run starts on that word's line
            self._run_line = line + _lines_before_text(text)
        self._run.append(text)

    def _end_run(self) -> None:
        text = " ".join("".join(self._run).split())
        if text.strip("` "):
            self.said.append((self._run_line, text))
        self._run = []


def _lines_before_text(text: str) -> int:
    """The line breaks in the whitespace `text` starts with."""
    return text[: len(text) - len(text.lstrip())].count("\n")


def read_page(source: str) -> Page:
    page = Page()
    page.feed(source)
    page.close()
    return page


def page_texts(source: str) -> Iterator[tuple[int, str]]:
    """`(line, text)` of everything a person reads in a page: its text and the attributes of
    `READ`, the prose of the scripts written in it, and the strings of the data it embeds."""
    page = read_page(source)
    found = list(page.said)
    for line, kind, script in page.scripts:
        if kind in _JAVASCRIPT:
            found += [(line + at - 1, text) for at, text in script_texts(script)]
        elif kind in _JSON and script.strip():
            found += [(line, text) for text in _strings(json.loads(script)) if prose(text)]
    return iter(sorted(found, key=lambda said: said[0]))  # in the page's order


def _strings(data: object) -> Iterator[str]:
    """Every string of a JSON document, its keys left out."""
    if isinstance(data, str):
        yield data
    elif isinstance(data, dict):
        for value in data.values():
            yield from _strings(value)
    elif isinstance(data, list):
        for value in data:
            yield from _strings(value)


# -- a script -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Literal:
    """A string of a script's source: where it starts, its text, and what it is: a "string", a
    "template" (with `{}` where it interpolates), JSX "text", or the name of the JSX attribute
    it is the value of."""

    line: int
    kind: str
    text: str


_WORD = re.compile(r"[A-Za-z_$][\w$]*|\d[\w.]*")
_JSX_NAME = re.compile(r"[A-Za-z_$][\w$.:-]*")
_JSX_OPENS = re.compile(r"<[A-Za-z_$>]")
_JSX_TEXT_ENDS = re.compile(r"[<{]")
_FLAGS = re.compile(r"[A-Za-z]*")
_CODE_POINT = re.compile(r"\\u\{([0-9A-Fa-f]{1,6})\}|\\u([0-9A-Fa-f]{4})|\\x([0-9A-Fa-f]{2})")
_TAG = re.compile(r"</?[A-Za-z]")  # where a literal holds markup
# After one of these words comes a value, as after an operator: a `/` there opens a regular
# expression, and a `<` in a `.tsx` file opens an element.
_BEFORE_A_VALUE = frozenset(
    {"return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw", "case"}
    | {"do", "else", "yield", "await", "default"}
)
_CLOSES = {"(": ")", "[": "]", "{": "}"}
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0", "\n": ""}
# JSX attributes whose value is CSS classes: names, however many a literal holds.
_CLASSES = ("className", "class")


class Unreadable(Exception):
    """A script the reader lost its place in: it says where, and nothing of the file is taken
    as read."""


class Script:
    """A reader of JavaScript and TypeScript source, enough of one to find every string: it
    tells a comment, a regular expression and a division apart, follows a template through its
    `${...}`, and in a `.tsx` file (`jsx`) reads elements, their attributes and their text. It
    stops (`Unreadable`) on a bracket, a string or an element that does not close, so a file it
    misreads fails loudly instead of hiding its words."""

    def __init__(self, source: str, *, jsx: bool = False) -> None:
        self.source = source
        self.jsx = jsx
        self.at = 0
        self.before = ""  # the token before: what a `/` or a `<` after it is
        self.found: list[Literal] = []
        self._lines = [found.end() for found in re.finditer("\n", source)]

    def line(self, at: int | None = None) -> int:
        return bisect_right(self._lines, self.at if at is None else at) + 1

    def lost(self, why: str, at: int | None = None) -> Unreadable:
        where = self.at if at is None else at
        return Unreadable(f"line {self.line(where)}: {why}: {self.source[where : where + 60]!r}")

    def literals(self) -> list[Literal]:
        self.code()
        return sorted(self.found, key=lambda literal: literal.line)

    def a_value_comes_next(self) -> bool:
        before = self.before
        if not before or before in _BEFORE_A_VALUE:
            return True
        if before == "a value" or before[0].isalnum() or before[0] in "_$":
            return False  # after a value: a division, a comparison, a type's parameters
        return before not in (")", "]")

    def code(self, closer: str = "") -> None:
        """Code, up to the `closer` that ends it (left unread) or to the end of the source."""
        source, opened = self.source, []
        while self.at < len(source):
            char = source[self.at]
            if char.isspace():
                self.at += 1
            elif source.startswith("//", self.at):
                end = source.find("\n", self.at)
                self.at = len(source) if end < 0 else end
            elif source.startswith("/*", self.at):
                end = source.find("*/", self.at + 2)
                if end < 0:
                    raise self.lost("a comment does not close")
                self.at = end + 2
            elif char in "\"'":
                self.string(char)
            elif char == "`":
                self.template()
            elif char == "/" and self.a_value_comes_next():
                self.regular_expression()
            elif (
                char == "<"
                and self.jsx
                and self.a_value_comes_next()
                and _JSX_OPENS.match(source, self.at)
            ):
                self.element()
            elif char in _CLOSES:
                opened.append(_CLOSES[char])
                self.at += 1
                self.before = char
            elif char in ")]}":
                if not opened:
                    if char != closer:
                        raise self.lost(f"a {char} closes nothing")
                    return
                if opened.pop() != char:
                    raise self.lost(f"a {char} closes another bracket")
                self.at += 1
                self.before = char
            elif word := _WORD.match(source, self.at):
                # A name after a dot is a property, whatever word it is (`module.default`).
                self.before = "a value" if self.before == "." else word.group()
                self.at = word.end()
            elif source.startswith(("++", "--"), self.at):
                # After a value it ends one (`i++ / 2`); before one, it is an operator like any.
                self.before = char if self.a_value_comes_next() else "a value"
                self.at += 2
            else:
                self.at += 1
                self.before = char
        if closer or opened:
            raise self.lost("the source ends inside a bracket")

    def string(self, quote: str) -> None:
        source, start = self.source, self.at
        at, parts = start + 1, []
        while True:
            if at >= len(source) or source[at] == "\n":
                raise self.lost("a string does not close", start)
            if source[at] == quote:
                break
            text, at = self._character(at)
            parts.append(text)
        self.found.append(Literal(self.line(start), "string", "".join(parts)))
        self.at, self.before = at + 1, "a value"

    def template(self) -> None:
        source, start = self.source, self.at
        self.at, parts = start + 1, []
        while True:
            if self.at >= len(source):
                raise self.lost("a template does not close", start)
            if source[self.at] == "`":
                break
            if source.startswith("${", self.at):
                self.at += 1
                self._braces()
                parts.append("{}")
            else:
                text, self.at = self._character(self.at)
                parts.append(text)
        self.found.append(Literal(self.line(start), "template", "".join(parts)))
        self.at, self.before = self.at + 1, "a value"

    def _character(self, at: int) -> tuple[str, int]:
        """The character at `at`, an escape sequence as what it stands for, and where the next
        one starts."""
        source = self.source
        if source[at] != "\\":
            return source[at], at + 1
        coded = _CODE_POINT.match(source, at)
        if coded and (point := int(next(filter(None, coded.groups())), 16)) < 0x110000:
            return chr(point), coded.end()
        kind = source[at + 1 : at + 2]
        return _ESCAPES.get(kind, kind), at + 2

    def regular_expression(self) -> None:
        source, start = self.source, self.at
        at, in_class = start + 1, False
        while True:
            if at >= len(source) or source[at] == "\n":
                raise self.lost("a regular expression does not close", start)
            char = source[at]
            if char == "\\":
                at += 2
                continue
            if char == "/" and not in_class:
                break
            in_class = char == "[" or (in_class and char != "]")
            at += 1
        flags = _FLAGS.match(source, at + 1)
        assert flags is not None
        self.at, self.before = flags.end(), "a value"

    def element(self) -> None:
        """A JSX element, from its `<` to the end of its closing tag: each attribute's value,
        and its children (text, `{...}`, elements)."""
        source, start = self.source, self.at
        self.at += 1
        named = _JSX_NAME.match(source, self.at)
        name = named.group() if named else ""  # a fragment has none
        self.at = named.end() if named else self.at
        while True:  # its attributes
            self._space()
            if self.at >= len(source):
                raise self.lost(f"<{name}> does not close", start)
            if source.startswith("/>", self.at):
                self.at, self.before = self.at + 2, "a value"
                return
            if source[self.at] == ">":
                self.at += 1
                break
            if source[self.at] == "{":  # a spread
                self._braces()
                continue
            attribute = _JSX_NAME.match(source, self.at)
            if not attribute:
                raise self.lost(f"not an attribute of <{name}>")
            self.at = attribute.end()
            self._space()
            if source[self.at : self.at + 1] != "=":
                continue
            self.at += 1
            self._space()
            if source[self.at : self.at + 1] == "{":
                self._braces()
            elif source[self.at : self.at + 1] in ('"', "'"):
                end = source.find(source[self.at], self.at + 1)
                if end < 0:
                    raise self.lost(f"{attribute.group()} does not close")
                value = html.unescape(source[self.at + 1 : end])
                self.found.append(Literal(self.line(), attribute.group(), value))
                self.at = end + 1
            else:
                raise self.lost(f"not a value of {attribute.group()}")
        while True:  # its children
            if self.at >= len(source):
                raise self.lost(f"<{name}> does not close", start)
            if source.startswith("</", self.at):
                end = source.find(">", self.at)
                if end < 0 or source[self.at + 2 : end].strip() != name:
                    raise self.lost(f"this closes <{name}>")
                self.at, self.before = end + 1, "a value"
                return
            if source[self.at] == "<":
                self.element()
            elif source[self.at] == "{":
                self._braces()
            else:
                ends = _JSX_TEXT_ENDS.search(source, self.at)
                stop = ends.start() if ends else len(source)
                raw = source[self.at : stop]
                text = " ".join(html.unescape(raw).split())
                if text:  # on the line of its first word
                    first = self.at + len(raw) - len(raw.lstrip())
                    self.found.append(Literal(self.line(first), "text", text))
                self.at = stop

    def _braces(self) -> None:
        """The code between a `{` and its `}`."""
        self.at += 1
        self.before = "{"
        self.code("}")
        self.at += 1

    def _space(self) -> None:
        """Past whitespace and comments (between an element's attributes)."""
        source = self.source
        while self.at < len(source):
            if source[self.at].isspace():
                self.at += 1
            elif source.startswith("//", self.at):
                end = source.find("\n", self.at)
                self.at = len(source) if end < 0 else end
            elif source.startswith("/*", self.at):
                end = source.find("*/", self.at + 2)
                if end < 0:
                    raise self.lost("a comment does not close")
                self.at = end + 2
            else:
                return


def script_texts(source: str, *, jsx: bool = False) -> Iterator[tuple[int, str]]:
    """`(line, text)` of everything a person reads that a script holds: JSX text, the prose
    among its literals and its JSX attributes' values, and, of a literal that holds markup,
    that markup's text and its attributes of `READ`. The line is the literal's first."""
    for literal in Script(source, jsx=jsx).literals():
        said = " ".join(literal.text.split())
        if literal.kind == "text":
            yield literal.line, said
        elif literal.kind in ("string", "template") and _TAG.search(literal.text):
            yield from ((literal.line, text) for _, text in read_page(literal.text).said)
        elif literal.kind not in _CLASSES and said and prose(literal.text):
            yield literal.line, said


# -- the surfaces ---------------------------------------------------------------------------------


def _page(path: Path) -> Iterator[tuple[int, str]]:
    return page_texts(path.read_text(encoding="utf-8"))


def _script(path: Path) -> Iterator[tuple[int, str]]:
    try:
        yield from script_texts(
            path.read_text(encoding="utf-8"), jsx=path.suffix in (".tsx", ".jsx")
        )
    except Unreadable as lost:
        raise AssertionError(f"{path.relative_to(ROOT)} is not read: {lost}") from lost


def _example(path: Path) -> Iterator[tuple[int, str]]:
    """An API example is shown as written: each of its lines."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return ((number, line.strip()) for number, line in enumerate(lines, 1) if line.strip())


@dataclass(frozen=True)
class Surface:
    """What a front end shows: its files, and how one is read (`(line, text)` of what a person
    reads in it). So that reading little or nothing never passes, it also names the least it
    reads (`at_least` texts) and the product's own terms it is known to say (`says`)."""

    name: str
    files: Callable[[], list[Path]]
    read: Callable[[Path], Iterator[tuple[int, str]]]
    at_least: int
    says: tuple[str, ...] = ()

    def texts(self) -> Iterator[tuple[str, int, str]]:
        """`(file, line, text)` of everything it shows."""
        for path in self.files():
            file = path.relative_to(ROOT).as_posix()
            yield from ((file, line, text) for line, text in self.read(path))


SURFACES = [
    Surface(
        "the console's pages",
        lambda: files(CONSOLE, *PAGES),
        _page,
        at_least=100,
        says=("Agent store", "Decision Card"),
    ),
    Surface(
        "the console's scripts",
        lambda: files(CONSOLE, *SCRIPTS),
        _script,
        at_least=2000,
        says=("Agent store", "Agent author", "Decision Card"),
    ),
    Surface(
        "the console's API examples",
        lambda: files(CONSOLE / "snippets", *EXAMPLES),
        _example,
        at_least=80,
    ),
    Surface(
        "the site's pages",
        lambda: files(SITE, *PAGES),
        _page,
        at_least=300,
        says=("Agent Hub", "Decision Card"),
    ),
    Surface(
        "the Hub's scripts",
        lambda: files(HUB, *SCRIPTS),
        _script,
        at_least=350,
        says=("Agent Hub", "Decision Card"),
    ),
]

# Texts that say the old word on purpose: (file, the whole text) -> why.
ALLOWED: dict[tuple[str, str], str] = {
    (
        "src/mendari/dashboard/snippets/fhir.sh",
        '{"name": "pack", "valueString": "mendari/portal-message-agent"},',
    ): "the `$decide` operation's parameter is named `pack` on the wire, and a FHIR Parameters "
    "resource gives a parameter's name as a value",
}


@pytest.mark.parametrize("surface", SURFACES, ids=lambda surface: surface.name)
def test_a_front_end_says_agent(surface: Surface) -> None:
    texts = list(surface.texts())
    found = [
        f"{file}:{line}: {', '.join(sorted(set(offences(text))))!r} in {text[:160]!r}"
        for file, line, text in texts
        if offences(text) and (file, text) not in ALLOWED
    ]
    assert not found, f"{len(found)} in {surface.name}:\n" + "\n".join(found)
    assert len(texts) >= surface.at_least, f"{surface.name}: only {len(texts)} texts are read"
    everything = "\n".join(text for _, _, text in texts)
    missing = [term for term in surface.says if term not in everything]
    assert not missing, f"{surface.name}: not read, or no longer said: {missing}"


def test_every_allowed_text_is_still_said_and_still_needs_allowing() -> None:
    said = {(file, text) for surface in SURFACES for file, _, text in surface.texts()}
    for (file, text), reason in ALLOWED.items():
        assert reason.strip(), (file, text)
        assert (file, text) in said, f"{file} no longer says {text[:80]!r}: take it off the list"
        assert offences(text), f"{file}: {text[:80]!r} breaks no rule: take it off the list"


def test_every_page_script_and_example_of_a_front_end_is_read() -> None:
    """A file the surfaces leave out would say what it likes: every page, script and API example
    under the three front ends belongs to one of them."""
    read = [path for surface in SURFACES for path in surface.files()]
    assert len(read) == len(set(read))  # none twice
    there = {
        path for root in (CONSOLE, HUB, SITE) for path in files(root, *PAGES, *SCRIPTS, *EXAMPLES)
    }
    left_out = sorted(path.relative_to(ROOT).as_posix() for path in there - set(read))
    assert not left_out, f"read by no surface: {left_out}"
    counted = {surface.name: len(surface.files()) for surface in SURFACES}
    assert counted["the console's pages"] >= 5 and counted["the console's scripts"] >= 37
    assert counted["the site's pages"] >= 4 and counted["the Hub's scripts"] >= 20
    assert counted["the console's API examples"] >= 4


# -- the Hub's addresses --------------------------------------------------------------------------


def test_the_hub_s_addresses_say_agents() -> None:
    """A folder of `hub/app` is a part of an address, which people see and type (Next.js routes
    by folder; `[name]` is a parameter). None says the old word."""
    app = HUB / "app"
    routed = [path.relative_to(app) for path in files(app, *SCRIPTS)]
    parts = {part for path in routed for part in path.parts[:-1] if not part.startswith("[")}
    assert {"agents", "publishers"} <= parts
    assert not {part for part in parts if offences(part)}


# -- the readers, on what they must tell apart ----------------------------------------------------


def test_the_rule_for_prose() -> None:
    for text in ("a pack", "No agent yet", "Pack", "Packs", "Decision Pack", "PACK"):
        assert prose(text), text
    for text in ("pack", "packs", "pack_id", "./pack.js", "#pack-select", "/v1/packs/{}/{}"):
        assert not prose(text), text


def test_a_page_is_read_as_a_person_reads_it() -> None:
    page = """<!doctype html>
<html lang="en">
<head>
  <title>Packs · Mendari</title>
  <meta name="description" content="Every pack, in one place.">
  <style>.pack-hero { color: red; }</style>
  <script>var saved = "oh.pack"; document.title = "A pack's page";</script>
  <script type="application/ld+json">{"name": "pack", "about": "Decision Packs for all"}</script>
</head>
<!-- A comment about a pack. -->
<body class="pack-page">
  <a class="pack-link" href="/packs/x" id="pack" data-pack="x" title="Open the pack">Open</a>
  <p>The <code>pack</code> parameter names an <b>agent</b>, not
     a pack.</p>
  <img src="pack.png" alt="A pack's card">
  <input name="pack" placeholder="Search packs" aria-label="Find a pack">
  <input type="submit" value="Add a pack"><input type="hidden" value="a pack">
  <select><optgroup label="Local packs"><option value="pack">Inbox</option></optgroup></select>
</body>
</html>"""
    assert list(page_texts(page)) == [
        (4, "Packs · Mendari"),
        (5, "Every pack, in one place."),
        (7, "A pack's page"),  # a script's prose; `"oh.pack"` is a key
        (8, "Decision Packs for all"),  # data the page embeds
        (12, "Open the pack"),
        (12, "Open"),
        (13, "The `pack` parameter names an agent, not a pack."),
        (15, "A pack's card"),
        (16, "Find a pack"),
        (16, "Search packs"),
        (17, "Add a pack"),
        (18, "Local packs"),
        (18, "Inbox"),
    ]
    # A wire name written as code passes; the word in the sentence does not.
    assert offences("The `pack` parameter names an agent, not a pack.") == ["pack"]
    assert not offences("The `pack` parameter names an agent.")


def test_a_script_s_strings_are_found_whatever_is_around_them() -> None:
    source = r"""// A comment about a pack: it's not read, nor is "this".
/* Nor this pack, with a ` and a ' in it. */
import { packOptions } from "./pack.js";
const KEY = "pack";
const plain = 'It\'s a pack';
const joined = "one%u0020pack" + 'two\x20packs' + "three%u{20}packs";
const half = total / packs / 2, quoted = text.replace(/["'`\/]/g, "a pack's").split(/[/"]/);
const rate = hits++ / total, share = stats.default / 2, cell = "<b>pack</b>";
const html = `<a class="pack-link" href="#store/packs" title="Open ${name}'s pack">
  ${items.map((item) => `<li>${item.pack ? `The pack ${item.pack}` : "no pack"}</li>`).join("")}
</a>`;
const last = saved ? `#pack-${id}` : "Packs";
""".replace("%u", "\\u")  # written so: this file holds no escape of its own there
    assert Script(source).literals() == [
        Literal(3, "string", "./pack.js"),
        Literal(4, "string", "pack"),
        Literal(5, "string", "It's a pack"),
        Literal(6, "string", "one pack"),
        Literal(6, "string", "two packs"),
        Literal(6, "string", "three packs"),
        Literal(7, "string", "a pack's"),
        Literal(8, "string", "<b>pack</b>"),
        Literal(
            9,
            "template",
            '<a class="pack-link" href="#store/packs" title="Open {}\'s pack">\n  {}\n</a>',
        ),
        Literal(10, "template", "The pack {}"),
        Literal(10, "string", "no pack"),
        Literal(10, "template", "<li>{}</li>"),
        Literal(10, "string", ""),
        Literal(12, "template", "#pack-{}"),
        Literal(12, "string", "Packs"),
    ]
    assert list(script_texts(source)) == [
        (5, "It's a pack"),
        (6, "one pack"),
        (6, "two packs"),
        (6, "three packs"),
        (7, "a pack's"),
        (8, "pack"),  # markup's text, though the literal holds no whitespace
        (9, "Open {}'s pack"),  # the template's markup: its tooltip and its text, not its class
        (9, "{}"),
        (10, "The pack {}"),
        (10, "no pack"),
        (10, "{}"),
        (12, "Packs"),  # one word with a capital: a label
    ]


def test_a_tsx_file_s_text_and_attributes_are_found() -> None:
    source = """import type { PackSummary } from "@/lib/registry";
type Kind = "a pack" | "agent";
export function Card({ item, wide }: { item: PackSummary; wide: boolean }) {
  const [shown, setShown] = useState<"idle" | "a pack">("idle");
  const few = item.versions.length < 3 && !wide;
  return (
    <>
      {/* A comment about a pack. */}
      <li className="pack-card pack-wide" title="A pack&apos;s card" aria-label={`Pack ${item.id}`}>
        <Link href={`/packs/${item.id}`} {...rest}>Open the pack</Link>
        {few ? <b>Few of this pack&apos;s versions</b> : "Every pack version"}
        <Fact label="Pack digest" value={item.digest} />
        Packs: {item.count} of <code>mendari pack</code>
      </li>
    </>
  );
}
"""
    assert list(script_texts(source, jsx=True)) == [
        (2, "a pack"),
        (4, "a pack"),
        (9, "A pack's card"),
        (9, "Pack {}"),
        (10, "Open the pack"),
        (11, "Few of this pack's versions"),
        (11, "Every pack version"),
        (12, "Pack digest"),
        (13, "Packs:"),
        (13, "of"),
        (13, "mendari pack"),
    ]
    with pytest.raises(Unreadable, match="line 2: <p> does not close"):
        Script("const a = 1;\nconst b = <p>never closed;\n", jsx=True).literals()
    with pytest.raises(Unreadable, match="line 1: a string does not close"):
        Script("const a = 'never closed;\n").literals()
    with pytest.raises(Unreadable, match="line 3: the source ends inside a bracket"):
        Script("function f() {\n  return 1;\n").literals()
