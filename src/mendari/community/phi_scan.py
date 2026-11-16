# SPDX-License-Identifier: Apache-2.0
"""Text in a pack's data and tests that looks like it came from a real person (warnings).

A community pack's data must be synthetic, and anything merged into a public history stays
there. These heuristics find what is easy to spot: a Social Security number, a phone number
outside the fictional 555-01xx range, a medical record number, a date of birth next to a name,
an email address outside the reserved example domains. They find shapes, not people, so each is
a warning for the author and the reviewer to look at, never proof either way. A warning names
the file, the line and the kind, never the text, which may be someone's.

They are five of the PHI detector's kinds (`core/phi_detect.py`, LP-20d, on RE2), worded as
the community check has always worded them; its street addresses and listed names are the pack
store's and not warnings here. Two shapes are as often something else, so they count only
beside a word that says what they are: ten digits run together (`6175552368`) are a phone number
next to a word such as phone, call or fax, and otherwise as likely a record number or an id; a
date written out in words (`January 2, 1980`, `2 Jan 1980`) is a date of birth next to born, DOB
or birthday, and otherwise an ordinary date. A long line costs a pass, never a backtracking
search.
"""

from __future__ import annotations

from dataclasses import dataclass

from mendari.core import phi_detect

# The detector's kinds this check warns of, in its words.
WARNINGS = {
    "ssn": "a Social Security number",
    "phone": "a phone number (use 555-0100 to 555-0199)",
    "record_number": "a medical record number",
    "birth_date": "a date of birth next to a name",
    "email": "an email address (use one at example.org)",
}


@dataclass(frozen=True)
class Hit:
    line: int
    kind: str


def scan(text: str) -> list[Hit]:
    """What looks like a real person's details in `text`, by line."""
    hits: list[Hit] = []
    for number, line in enumerate(text.split("\n"), start=1):  # JSONL lines end at "\n" only
        for kind in phi_detect.structural_kinds(line):
            if kind in WARNINGS:
                hits.append(Hit(number, WARNINGS[kind]))
    return hits
