# SPDX-License-Identifier: Apache-2.0
"""A deterministic detector of text that looks like a real person's details (LP-20d).

The pack store (LP-20f) refuses an upload whose files hold such text, `mendari data check --phi`
reports it, and the community check's warnings (`community/phi_scan.py`) are five of its kinds.
It finds shapes and listed names, not people: a filter, never a guarantee, so the store's terms
still forbid real data.

A finding is a kind, a file and a line (0: the file's path). Every file is read: its text (as
UTF-8, else UTF-16 with its mark, else Latin-1), the string values a JSON, JSON Lines or YAML
file decodes to (so an escape hides nothing) and its path. A report (`mendari.phi-check/1`)
holds the findings and the counts per kind, **never the matched text**, which may be someone's:
no report, log line or error here holds it.

Kinds (`KINDS`):
- `ssn`: a Social Security number (`123-45-6789`, `123 45 6789`), not one the SSA never issues
  (area 000, 666 or 9xx, group 00, serial 0000).
- `phone`: a North American number outside 555-0100 to 555-0199, the range kept for fiction;
  ten digits run together only beside a word for a phone (call, fax, cell, ...), since they are
  as often an id.
- `record_number`: a medical record number (`MRN 00123456`, `medical record no. 1234567`).
- `birth_date`: a numeric date beside a birth word (born, DOB, birthday) or a name, or a date in
  words beside a birth word.
- `email`: an address outside the domains reserved for documentation (`example.org`, `.test`).
- `street_address` (new in LP-20): a house number, one to three capitalised words and a street
  type (`12 Elm Street`, `4410 North Lake Dr`).
- `name` (new in LP-20): a given name or surname from `phi_names.txt` (common United States
  names), written as a name is, on a line that also holds another kind or a word that says it
  is a person's (`Mr`, `Mrs`, `Ms`, `name`, `named`). A name alone is never a finding: the
  synthetic datasets are full of made-up names, and many names are also words.

The verdict (`rejects`): any of the first six kinds rejects alone; name findings reject from
`NAME_THRESHOLD`. Measured on 2026-11-15 over the 94 files of the five reference packs and the
synthetic datasets in `datasets/`: no finding of any kind, names included (110 of their lines
hold a listed name, none beside a person's word or another kind), so one name finding is enough
(`tests/test_phi_detect.py` keeps the measurement).

Every pattern is compiled with RE2 (`core/patterns.py`'s engine), because the detector reads
hostile uploads: a long line costs one pass, never a backtracking search. The patterns hold no
lookaround, which RE2 cannot run: what the community heuristics checked with one is checked on
the captured digits in Python, and the edges of a number are matched characters."""

from __future__ import annotations

import functools
import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import resources
from typing import Any

FORMAT = "mendari.phi-check/1"
KINDS = ("ssn", "phone", "record_number", "birth_date", "email", "street_address", "name")
# The kinds that reject an upload alone (`rejects`); a name needs `NAME_THRESHOLD` findings.
STRUCTURAL = KINDS[:-1]
NAME_THRESHOLD = 1  # the measurement in the module's docstring
NEAR = 40  # characters between a date and a name or a birth word, a number and a phone word
_LONGEST_WORD = 20  # more than any keyword's length: one that starts within NEAR is read whole
MAX_LINE_CHARS = 1_000_000  # a longer line is read in pieces of this size, each one pass
OVERLAP_CHARS = 512  # how much each piece repeats of the one before: more than any kind's text
# The decoded values read from one JSON or YAML file at most (an alias bomb expands without
# end): a file past it is refused, as it cannot be read through.
MAX_VALUES = 200_000
RE2_MAX_MEMORY = 8 << 20

# Before and after a number: not a word character or a hyphen. RE2 has no lookaround, so the
# character (or the line's edge) is matched and the digits are read from named groups.
_EDGE_BEFORE = r"(?:^|[^\w-])"
_EDGE_AFTER = r"(?:$|[^\w-])"
_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
    r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
)
_DAY = r"(?:0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?"

# Every pattern's source, compiled by `Patterns` for one engine.
SOURCES = {
    "ssn": r"\b(?P<area>\d{3})[- ](?P<group>\d{2})[- ](?P<serial>\d{4})\b",
    "phone": _EDGE_BEFORE + r"(?:\+?1[-. ]?)?\(?(?P<area>\d{3})\)?[-. ]?(?P<exchange>\d{3})[-. ]"
    r"(?P<line>\d{4})" + _EDGE_AFTER,
    # Ten digits with no separator (eleven with the country code): a phone only beside a word.
    "bare_phone": r"(?:^|[^\w+-])(?:\+?1)?(?P<area>\d{3})(?P<exchange>\d{3})(?P<line>\d{4})"
    + _EDGE_AFTER,
    # Each word with its own inflections only ("contacted", "faxed"): not "cells", a lab's unit.
    "phone_word": r"(?i)\b(?:(?:tele)?phones?|tel|cell(?:phones?)?|mobiles?|call(?:s|ed|ing)?|"
    r"fax(?:es|ed|ing)?|contact(?:s|ed|ing)?|pagers?|"
    r"text(?:ed)? me|reach(?:ed)? (?:me|us|him|her|them))\b",
    "record_number": r"(?i)\b(?:MRN|medical record(?: number| no\.?)?)\b\W{0,3}[A-Z]{0,3}\d{5,}",
    "date": r"\b(?:\d{1,2}[/-]\d{1,2}[/-](?:19|20)?\d{2}|(?:19|20)\d{2}-\d{2}-\d{2})\b",
    "birth_word": r"(?i)\b(?:DOB|D\.O\.B\.?|date of birth|born|birth ?date|birthday)\b",
    "written_date": rf"(?i)\b(?:{_MONTH} {{1,3}}{_DAY}(?:, {{0,3}}| {{1,3}})|"
    rf"{_DAY} {{1,3}}{_MONTH},? {{1,3}})(?:19|20)\d{{2}}\b",
    "titled_name": r"\b(?:Mr|Mrs|Ms|Miss|Dr)\.? [A-Z][a-z]+|\b[A-Z][a-z]+ [A-Z][a-z]{2,}\b",
    # Bounded, so a long line without `@` costs a pass, never a quadratic search.
    "email": r"[\w.+-]{1,64}@(?P<domain>[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,8})",
    # RFC 2606: names reserved for documentation, which no one can register.
    "example_domain": r"(?i)(?:^|\.)(?:example\.(?:org|com|net)|"
    r"[a-z0-9-]+\.(?:example|test|invalid))$",
    "street_address": r"\b\d{1,5} (?:[A-Z][A-Za-z]{1,30}\.? ){1,3}"
    r"(?i:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|court|ct|way|place|pl|"
    r"terrace|ter|circle|cir|parkway|pkwy|highway|hwy)\b",
    "capitalised": r"\b[A-Z][a-z]{1,30}\b",
    "person_word": r"(?i)\b(?:mr|mrs|ms|mx|miss|named|name)\b",
}


class Patterns:
    """`SOURCES` compiled by one engine: RE2 for the detector (`RE2`), Python's `re` only to
    test that the two agree."""

    def __init__(self, compile: Callable[[str], Any]):
        self._compiled = {name: compile(source) for name, source in SOURCES.items()}

    def __getitem__(self, name: str) -> Any:
        return self._compiled[name]


def _re2_compile(source: str) -> Any:
    import re2

    options = re2.Options()
    options.max_mem = RE2_MAX_MEMORY
    options.log_errors = False
    return re2.compile(source, options)


@functools.cache
def re2_patterns() -> Patterns:
    return Patterns(_re2_compile)


def re_patterns() -> Patterns:
    """The same sources on Python's `re`: for the test that the engines agree, never for input
    someone else wrote."""
    return Patterns(re.compile)


@functools.cache
def names() -> frozenset[str]:
    """The listed names (`phi_names.txt`), as they are written: first letter capitalised."""
    text = resources.files("mendari.core").joinpath("phi_names.txt").read_text(encoding="utf-8")
    return frozenset(
        line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")
    )


@dataclass(frozen=True)
class Finding:
    """What looks like a person's details: its kind, the file it is in and its line (from 1).
    Never the text."""

    kind: str
    file: str
    line: int

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "file": self.file, "line": self.line}


def _issued_ssn(match: Any) -> bool:
    area, group, serial = match.group("area"), match.group("group"), match.group("serial")
    return not (area in ("000", "666") or area.startswith("9") or group == "00" or serial == "0000")


def _fictional(match: Any) -> bool:
    """555-0100 to 555-0199: the range kept for fiction in the North American plan."""
    return bool(match.group("exchange") == "555" and match.group("line").startswith("01"))


def _near(line: str, match: Any, word: Any) -> bool:
    """Whether `word` starts within NEAR characters of `match`. RE2 reads the characters past
    its bounds for a word boundary, so the "cell" of "cellulitis" or the "born" of "newborn" is
    not the word."""
    start, end = max(0, match.start() - NEAR), match.end() + NEAR
    found = word.search(line, start, min(len(line), end + _LONGEST_WORD))
    return found is not None and found.start() < end


def _phone(line: str, p: Patterns) -> bool:
    if any(not _fictional(match) for match in p["phone"].finditer(line)):
        return True
    return any(
        not _fictional(match) and _near(line, match, p["phone_word"])
        for match in p["bare_phone"].finditer(line)
    )


def _birth_date(line: str, p: Patterns) -> bool:
    """A numeric date beside a birth word or a name, or one in words beside a birth word."""
    birth, titled = p["birth_word"], p["titled_name"]
    for date in p["date"].finditer(line):
        if _near(line, date, birth) or _near(line, date, titled):
            return True
    return any(_near(line, d, birth) for d in p["written_date"].finditer(line))


def _email(line: str, p: Patterns) -> bool:
    return "@" in line and any(
        not p["example_domain"].search(match.group("domain")) for match in p["email"].finditer(line)
    )


def structural_kinds(line: str, patterns: Patterns | None = None) -> list[str]:
    """The kinds one line holds, but a name, in `KINDS` order."""
    p = patterns or re2_patterns()
    found = []
    if any(_issued_ssn(match) for match in p["ssn"].finditer(line)):
        found.append("ssn")
    if _phone(line, p):
        found.append("phone")
    if p["record_number"].search(line):
        found.append("record_number")
    if _birth_date(line, p):
        found.append("birth_date")
    if _email(line, p):
        found.append("email")
    if p["street_address"].search(line):
        found.append("street_address")
    return found


def _named(line: str, others: bool, p: Patterns) -> bool:
    """A listed name, on a line that also holds another kind or a person's word."""
    if not (others or p["person_word"].search(line)):
        return False
    listed = names()
    return any(m.group(0) in listed for m in p["capitalised"].finditer(line))


def kinds_of(line: str, patterns: Patterns | None = None) -> list[str]:
    """The kinds one line holds, in `KINDS` order. A line longer than `MAX_LINE_CHARS` is read
    in pieces of that size that overlap by `OVERLAP_CHARS`, so nothing is cut in two."""
    p = patterns or re2_patterns()
    found: set[str] = set()
    step = MAX_LINE_CHARS - OVERLAP_CHARS
    for start in range(0, max(len(line) - OVERLAP_CHARS, 1), step):
        piece = line[start : start + MAX_LINE_CHARS]
        kinds = structural_kinds(piece, p)
        found.update(kinds)
        if _named(piece, bool(kinds), p):
            found.add("name")
    return [kind for kind in KINDS if kind in found]


def detect(text: str, file: str = "") -> list[Finding]:
    """Every finding in `text`, by line (lines end at "\\n" only, as JSON Lines do)."""
    return [
        Finding(kind, file, number)
        for number, line in enumerate(text.split("\n"), start=1)
        for kind in kinds_of(line)
    ]


@dataclass(frozen=True)
class Report:
    """`mendari.phi-check/1`: the findings, how many files were given, those that are not
    UTF-8 text (read anyway, as UTF-16 with its mark or as Latin-1), and those whose decoded
    values could not all be read (refused). Never a matched value."""

    findings: tuple[Finding, ...]
    files: int
    unread: tuple[str, ...] = ()  # not UTF-8: read anyway, in another encoding
    unscannable: tuple[str, ...] = ()

    @property
    def counts(self) -> dict[str, int]:
        counted = Counter(finding.kind for finding in self.findings)
        return {kind: counted[kind] for kind in KINDS if counted[kind]}

    def to_json(self) -> dict[str, Any]:
        return {
            "format": FORMAT,
            "files": self.files,
            "unread": list(self.unread),
            "unscannable": list(self.unscannable),
            "counts": self.counts,
            "findings": [finding.to_json() for finding in self.findings],
            "rejected": rejects(self),
        }


def _text(data: bytes) -> tuple[str, bool]:
    """A file's text, and whether it was UTF-8: else UTF-16 with its byte-order mark, else
    Latin-1 (every byte a character), so no file goes unread."""
    try:
        return data.decode("utf-8"), True
    except UnicodeDecodeError:
        pass
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16"), False
        except UnicodeDecodeError:
            pass
    return data.decode("latin-1"), False


class _TooMany(Exception):
    pass


def _strings(value: Any, budget: list[int]) -> list[str]:
    """Every string a parsed JSON or YAML value holds, keys included: what an escape such as
    `\u002d` hid from the text. Stops past `MAX_VALUES`."""
    found: list[str] = []
    stack = [value]
    while stack:
        item = stack.pop()
        budget[0] -= 1
        if budget[0] < 0:
            raise _TooMany
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, dict):
            for key, child in item.items():
                stack += [key, child]
        elif isinstance(item, list | tuple):
            stack += list(item)
    return found


def _decoded(path: str, text: str) -> list[tuple[int, str]]:
    """The decoded string values of a JSON, JSON Lines or YAML file, by line (the record's, in
    JSON Lines; 1 otherwise). A file that does not parse is read as text only."""
    import json

    import yaml

    budget = [MAX_VALUES]
    lower = path.lower()
    found: list[tuple[int, str]] = []
    if lower.endswith((".jsonl", ".ndjson")):
        for number, line in enumerate(text.split("\n"), start=1):
            try:
                value = json.loads(line) if line.strip() else None
            except (ValueError, RecursionError):
                continue
            found += [(number, s) for s in _strings(value, budget)]
    elif lower.endswith(".json"):
        try:
            value = json.loads(text)
        except (ValueError, RecursionError):
            return []
        found += [(1, s) for s in _strings(value, budget)]
    elif lower.endswith((".yaml", ".yml")):
        try:
            value = yaml.safe_load(text)
        except (yaml.YAMLError, RecursionError, ValueError):
            return []
        found += [(1, s) for s in _strings(value, budget)]
    return found


def detect_files(files: Mapping[str, bytes]) -> Report:
    """Every finding in each file (by its path), in path order, of its text, of the string
    values a JSON or YAML file decodes to (an escape cannot hide a value), and of its path. A
    file that is not UTF-8 is read in another encoding and listed as `unread`; one whose decoded
    values cannot all be read is `unscannable` and refused."""
    findings: list[Finding] = []
    unread, unscannable = [], []
    for path in sorted(files):
        text, utf8 = _text(files[path])
        if not utf8:
            unread.append(path)
        found = {(f.kind, f.line) for f in detect(text, path)}
        in_text = {kind for kind, _ in found}
        try:
            for number, value in _decoded(path, text):
                # A JSON or YAML file's values all read as its line 1: a kind its text shows
                # already is not counted again.
                found |= {
                    (f.kind, number)
                    for f in detect(value)
                    if not (number == 1 and f.kind in in_text and not _lined(path))
                }
        except _TooMany:
            unscannable.append(path)
        # The path itself (line 0), its separators read as spaces: `Jane_617-555-0123.txt`.
        found |= {(kind, 0) for kind in kinds_of(re.sub(r"[_/.]", " ", path))}
        findings += [Finding(kind, path, line) for kind, line in sorted(found, key=_order)]
    return Report(tuple(findings), len(files), tuple(unread), tuple(unscannable))


def _lined(path: str) -> bool:
    """A JSON Lines file, whose decoded values keep their own line."""
    return path.lower().endswith((".jsonl", ".ndjson"))


def _order(item: tuple[str, int]) -> tuple[int, int]:
    return item[1], KINDS.index(item[0])


def rejects(report: Report) -> bool:
    """Whether an upload with these findings is refused: any structural kind, at least
    `NAME_THRESHOLD` name findings, or a file whose decoded values could not all be read."""
    counts = report.counts
    if report.unscannable:
        return True
    return any(counts.get(kind) for kind in STRUCTURAL) or counts.get("name", 0) >= NAME_THRESHOLD
