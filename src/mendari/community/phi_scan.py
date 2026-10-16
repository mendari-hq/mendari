# SPDX-License-Identifier: Apache-2.0
"""Text in a pack's data and tests that looks like it came from a real person (warnings).

A community pack's data must be synthetic, and anything merged into a public history stays
there. These heuristics find what is easy to spot: a Social Security number, a phone number
outside the fictional 555-01xx range, a medical record number, a date of birth next to a name,
an email address outside the reserved example domains. They find shapes, not people, so each is
a warning for the author and the reviewer to look at, never proof either way. A warning names
the file, the line and the kind, never the text, which may be someone's.

Two shapes are as often something else, so they count only beside a word that says what they
are: ten digits run together (`6175552368`) are a phone number next to a word such as phone,
call or fax, and otherwise as likely a record number or an id; a date written out in words
(`January 2, 1980`, `2 Jan 1980`) is a date of birth next to born, DOB or birthday, and
otherwise an ordinary date. Every pattern is a fixed sequence with bounded repeats, so a long
line costs a pass, never a backtracking search.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SSN = re.compile(r"\b(?!000|666|9\d\d)\d{3}[- ](?!00)\d{2}[- ](?!0000)\d{4}\b")
_PHONE = re.compile(r"(?<![\w-])(?:\+?1[-. ]?)?\(?(\d{3})\)?[-. ]?(\d{3})[-. ](\d{4})(?![\w-])")
# Ten digits with no separator (eleven with the country code): a phone only beside `_PHONE_WORD`.
_BARE_PHONE = re.compile(r"(?<![\w+-])(?:\+?1)?(\d{3})(\d{3})(\d{4})(?![\w-])")
# Each word with its own inflections only ("contacted", "faxed"): not "cells", a lab's unit.
_PHONE_WORD = re.compile(
    r"\b(?:(?:tele)?phones?|tel|cell(?:phones?)?|mobiles?|call(?:s|ed|ing)?|"
    r"fax(?:es|ed|ing)?|contact(?:s|ed|ing)?|pagers?|"
    r"text(?:ed)? me|reach(?:ed)? (?:me|us|him|her|them))\b",
    re.I,
)
_MRN = re.compile(r"\b(?:MRN|medical record(?: number| no\.?)?)\b\W{0,3}[A-Z]{0,3}\d{5,}", re.I)
_DATE = re.compile(r"\b(?:\d{1,2}[/-]\d{1,2}[/-](?:19|20)?\d{2}|(?:19|20)\d{2}-\d{2}-\d{2})\b")
_BIRTH = re.compile(r"\b(?:DOB|D\.O\.B\.?|date of birth|born|birth ?date|birthday)\b", re.I)
# A date in words: "January 2, 1980", "Jan. 2nd 1980", "2 January 1980". A date of birth only
# beside `_BIRTH`: a name beside one is as often an appointment or a letter's date.
_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
    r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
)
_DAY = r"(?:0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?"
_WRITTEN_DATE = re.compile(
    rf"\b(?:{_MONTH} {{1,3}}{_DAY}(?:, {{0,3}}| {{1,3}})|{_DAY} {{1,3}}{_MONTH},? {{1,3}})"
    rf"(?:19|20)\d{{2}}\b",
    re.I,
)
_NAME = re.compile(r"\b(?:Mr|Mrs|Ms|Miss|Dr)\.? [A-Z][a-z]+|\b[A-Z][a-z]+ [A-Z][a-z]{2,}\b")
# Bounded, so a long line without `@` costs a pass, never a quadratic search.
_EMAIL = re.compile(r"[\w.+-]{1,64}@([A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,8})")
# RFC 2606: names reserved for documentation, which no one can register.
_EXAMPLE_DOMAINS = re.compile(
    r"(?:^|\.)(?:example\.(?:org|com|net)|[a-z0-9-]+\.(?:example|test|invalid))$", re.I
)
NEAR = 40  # characters between a date and a name or a birth keyword
# More than any keyword's length: a keyword that starts within NEAR is read to its real end.
_LONGEST_WORD = 20


@dataclass(frozen=True)
class Hit:
    line: int
    kind: str


def scan(text: str) -> list[Hit]:
    """What looks like a real person's details in `text`, by line."""
    hits: list[Hit] = []
    for number, line in enumerate(text.split("\n"), start=1):  # JSONL lines end at "\n" only
        kinds = []
        if _SSN.search(line):
            kinds.append("a Social Security number")
        if any(not _fictional(match) for match in _PHONE.finditer(line)) or _bare_phone(line):
            kinds.append("a phone number (use 555-0100 to 555-0199)")
        if _MRN.search(line):
            kinds.append("a medical record number")
        if _birth_date(line):
            kinds.append("a date of birth next to a name")
        if "@" in line and any(
            not _EXAMPLE_DOMAINS.search(m.group(1)) for m in _EMAIL.finditer(line)
        ):
            kinds.append("an email address (use one at example.org)")
        hits += [Hit(number, kind) for kind in kinds]
    return hits


def _fictional(match: re.Match[str]) -> bool:
    """555-0100 to 555-0199: the range kept for fiction in the North American plan."""
    exchange, line = match.group(2), match.group(3)
    return exchange == "555" and line.startswith("01")


def _near(line: str, match: re.Match[str], word: re.Pattern[str]) -> bool:
    """Whether `word` starts within NEAR characters of `match`. The search runs on the line
    itself, between bounds, not on a slice of it: a slice's edge would read as a word boundary,
    so the "cell" of "cellulitis" or the "born" of "newborn" would count as the word."""
    start, end = max(0, match.start() - NEAR), match.end() + NEAR
    found = word.search(line, start, end + _LONGEST_WORD)
    return found is not None and found.start() < end


def _bare_phone(line: str) -> bool:
    """Ten digits run together, outside the fictional range, next to a word for a phone."""
    return any(
        not _fictional(match) and _near(line, match, _PHONE_WORD)
        for match in _BARE_PHONE.finditer(line)
    )


def _birth_date(line: str) -> bool:
    """A numeric date next to a birth keyword or a name, or one in words next to a keyword."""
    if any(_near(line, d, _BIRTH) or _near(line, d, _NAME) for d in _DATE.finditer(line)):
        return True
    return any(_near(line, date, _BIRTH) for date in _WRITTEN_DATE.finditer(line))
