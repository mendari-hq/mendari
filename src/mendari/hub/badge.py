# SPDX-License-Identifier: Apache-2.0
"""The evaluation badge: one line a publisher puts in a README, as an SVG the registry serves.

It says the state of an agent's published evaluation (`hub/evaluation_page.py` works it out,
from signed records that verify and from nothing else) and links to the page that shows it:

    Mendari | evaluated, 600 synthetic cases, 0 critical automated
    Mendari | evaluated, 2 configurations, 600 cases, 0 to 9 critical automated
    Mendari | no evaluation published
    Mendari | evaluation not verified
    Mendari | recalled
    Mendari | agent not found

**Words and whole numbers only.** `message` builds the text from this module's own words and
counts the registry worked out. No name a publisher chose is in it (an agent's id is in the
address, and only there), and no rate: a count is said with what it counts.

**An image that can do nothing.** The SVG holds two shapes and two texts. No script, no
style (`style` attributes and `<style>` are what a strict CSP refuses, so every colour and size
is a presentation attribute), no link, no reference to anything inside or outside the file,
and no font it loads. Every text is escaped as XML, whatever it holds (`svg`), and the widths
are worked out here (`width`), so nothing a text holds decides the markup around it.

The route (`hub/registry.py`) serves it as `image/svg+xml` with the registry's own headers
(`nosniff`, the CSP that loads nothing), for a few minutes of caching, to any site's page.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from xml.sax.saxutils import escape, quoteattr

LABEL = "Mendari"
# A badge's states (`evaluation_page.state` gives the first four).
EVALUATED, NONE, NOT_VERIFIED, RECALLED, NOT_FOUND = (
    "evaluated",
    "none",
    "not_verified",
    "recalled",
    "not_found",
)
# What the right half is filled with: the Agent Hub's light-theme accent, warning and danger
# (`hub/app/globals.css`) and a slate, each at least 4.5:1 under white text
# (`tests/test_evaluation_page.py` checks). Never green: nothing here is a pass, only what was
# measured.
TONES = {"info": "#1d4ed8", "warn": "#8a4f00", "bad": "#b3261e", "plain": "#64748b"}
LABEL_FILL = "#334155"  # `--color-ink-2`: it shows on a white page and on a dark one
TEXT_FILL = "#ffffff"
HEIGHT = 20
PAD = 60  # tenths of a pixel either side of a text
RADIUS = 3
FONT = "Verdana,Geneva,DejaVu Sans,sans-serif"
MAX_TEXT = 120  # characters: a longer text is cut, never a wider badge than a line holds

# Verdana's advance widths at 11px, in tenths of a pixel, for the printable ASCII from the space
# (32) to the tilde (126): read once from the font's own tables. `textLength` holds each text to
# the width worked out from these, so a viewer without Verdana draws the same badge.
_ASCII = (
    *(39, 43, 50, 90, 70, 118, 80, 30, 50, 50, 70, 90, 40, 50, 40, 50),  # space to /
    *(70, 70, 70, 70, 70, 70, 70, 70, 70, 70),  # 0 to 9
    *(50, 50, 90, 90, 90, 60, 110),  # : to @
    *(75, 75, 77, 85, 70, 63, 85, 83, 46, 50, 76, 61, 93),  # A to M
    *(82, 87, 66, 87, 76, 75, 68, 81, 75, 109, 75, 68, 75),  # N to Z
    *(50, 50, 50, 90, 70, 70),  # [ to `
    *(66, 69, 57, 69, 66, 39, 69, 70, 30, 38, 65, 30, 107),  # a to m
    *(70, 67, 69, 69, 47, 57, 43, 70, 65, 90, 65, 65, 58),  # n to z
    *(70, 50, 70, 90),  # { to ~
)
_OTHER = 110  # any other character: as wide as the widest letter, so a text never overflows


def width(text: str) -> int:
    """How wide `text` is drawn, in tenths of a pixel."""
    return sum(_ASCII[ord(c) - 32] if 32 <= ord(c) <= 126 else _OTHER for c in text)


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _s(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def message(state: str, totals: Mapping[str, Any] | None = None) -> tuple[str, str]:
    """A badge's words and its tone (a key of `TONES`), from its state and, when it is
    `evaluated`, the totals of the evaluations shown (`evaluation_page.totals`): how many
    configurations and datasets, the cases of the one dataset, whether they are synthetic,
    and the critical cases a configuration automated: the fewest and the most where the
    configurations differ ("0 to 9"), so a weak configuration is neither hidden behind a good
    one nor shown alone. Anything that is not a whole count is left unsaid; an unknown state
    is said as no evaluation."""
    if state == NOT_FOUND:
        return "agent not found", "plain"
    if state == RECALLED:
        return "recalled", "bad"
    if state == NOT_VERIFIED:
        return "evaluation not verified", "bad"
    if state != EVALUATED or totals is None:
        return "no evaluation published", "plain"
    said = ["evaluated"]
    configurations = _count(totals.get("configurations"))
    if configurations is not None and configurations > 1:
        said.append(_s(configurations, "configuration"))
    datasets, cases = _count(totals.get("datasets")), _count(totals.get("cases"))
    if datasets is not None and datasets > 1:
        said.append(_s(datasets, "dataset"))
    elif cases is not None:
        kind = "synthetic case" if totals.get("synthetic") is True else "case"
        said.append(_s(cases, kind))
    most, least = _count(totals.get("most_automated")), _count(totals.get("least_automated"))
    if most is None:  # the agent names no critical label: nothing to say of one
        return ", ".join(said), "info"
    # Where the configurations differ, the fewest and the most: never the best one alone.
    automated = f"{least} to {most}" if least is not None and least < most else str(most)
    said.append(f"{automated} critical automated")
    return ", ".join(said), "warn" if most else "info"


def _shown(text: str) -> str:
    """A text as the badge draws it: its printable characters (a control character has no
    place in XML, escaped or not), at most `MAX_TEXT` of them, and never nothing."""
    return "".join(c for c in text if c.isprintable())[:MAX_TEXT] or "unknown"


def svg(text: str, tone: str = "plain", *, label: str = LABEL) -> str:
    """The badge: `label` on the left, `text` on the right, filled with `tone`'s colour. Both
    are escaped as XML wherever they are written, a text of more than `MAX_TEXT` characters is
    cut, and a tone that is not one of `TONES` is the plain one."""
    label, text = _shown(label), _shown(text)
    fill = TONES.get(tone, TONES["plain"])
    left, right = width(label) + 2 * PAD, width(text) + 2 * PAD
    whole = (left + right + 9) // 10  # whole pixels, rounded up
    split = left / 10
    said = quoteattr(f"{label}: {text}")
    r, h = RADIUS, HEIGHT
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{whole}" height="{h}" '
        f'viewBox="0 0 {whole} {h}" role="img" aria-label={said}>'
        f"<title>{escape(label)}: {escape(text)}</title>"
        f'<rect width="{whole}" height="{h}" rx="{r}" fill="{fill}"/>'
        f'<path fill="{LABEL_FILL}" d="M{r} 0H{split:g}V{h}H{r}A{r} {r} 0 0 1 0 {h - r}V{r}'
        f'A{r} {r} 0 0 1 {r} 0Z"/>'
        f'<g fill="{TEXT_FILL}" text-anchor="middle" font-family={quoteattr(FONT)} '
        'font-size="110" text-rendering="geometricPrecision">'
        f'<text x="{left // 2}" y="140" transform="scale(.1)" textLength="{left - 2 * PAD}">'
        f"{escape(label)}</text>"
        f'<text x="{left + right // 2}" y="140" transform="scale(.1)" '
        f'textLength="{right - 2 * PAD}">{escape(text)}</text>'
        "</g></svg>"
    )
