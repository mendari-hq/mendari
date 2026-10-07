# SPDX-License-Identifier: Apache-2.0
"""Text in a pack's data and tests that looks like it came from a real person (warnings).

A community pack's data must be synthetic, and anything merged into a public history stays
there. These heuristics find what is easy to spot: a Social Security number, a phone number
outside the fictional 555-01xx range, a medical record number, a date of birth next to a name,
an email address outside the reserved example domains. They find shapes, not people, so each is
a warning for the author and the reviewer to look at, never proof either way. A warning names
the file, the line and the kind, never the text, which may be someone's.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SSN = re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")
_PHONE = re.compile(r"(?<![\w-])(?:\+?1[-. ]?)?\(?(\d{3})\)?[-. ](\d{3})[-. ](\d{4})(?![\w-])")
_MRN = re.compile(r"\b(?:MRN|medical record(?: number| no\.?)?)\b\W{0,3}[A-Z]{0,3}\d{5,}", re.I)
_DATE = re.compile(r"\b(?:\d{1,2}[/-]\d{1,2}[/-](?:19|20)?\d{2}|(?:19|20)\d{2}-\d{2}-\d{2})\b")
_BIRTH = re.compile(r"\b(?:DOB|D\.O\.B\.?|date of birth|born|birth ?date|birthday)\b", re.I)
_NAME = re.compile(r"\b(?:Mr|Mrs|Ms|Miss|Dr)\.? [A-Z][a-z]+|\b[A-Z][a-z]+ [A-Z][a-z]{2,}\b")
# Bounded, so a long line without `@` costs a pass, never a quadratic search.
_EMAIL = re.compile(r"[\w.+-]{1,64}@([A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,8})")
# RFC 2606: names reserved for documentation, which no one can register.
_EXAMPLE_DOMAINS = re.compile(
    r"(?:^|\.)(?:example\.(?:org|com|net)|[a-z0-9-]+\.(?:example|test|invalid))$", re.I
)
NEAR = 40  # characters between a date and a name or a birth keyword


@dataclass(frozen=True)
class Hit:
    line: int
    kind: str


def scan(text: str) -> list[Hit]:
    """What looks like a real person's details in `text`, by line."""
    hits: list[Hit] = []
    for number, line in enumerate(text.splitlines(), start=1):
        kinds = []
        if _SSN.search(line):
            kinds.append("a Social Security number")
        if any(not _fictional(match) for match in _PHONE.finditer(line)):
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


def _birth_date(line: str) -> bool:
    for date in _DATE.finditer(line):
        window = line[max(0, date.start() - NEAR) : date.end() + NEAR]
        if _BIRTH.search(window) or _NAME.search(window):
            return True
    return False
