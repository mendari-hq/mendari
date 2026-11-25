# SPDX-License-Identifier: Apache-2.0
"""The dashboard's colour tokens meet WCAG AA in both themes, and components use only tokens:
colours, spacing and layers.

Reads the token blocks from style.css (`:root` for light, `:root[data-theme="dark"]` for dark), so a
colour change that breaks contrast fails here before anyone sees it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS = (Path(__file__).parents[1] / "src" / "mendari" / "dashboard" / "style.css").read_text()
TEXT, UI = 4.5, 3.0  # WCAG AA: normal text; UI components, focus rings and chart marks


def _block(selector: str) -> tuple[int, int]:
    start = CSS.index(selector + " {")
    return start, CSS.index("\n}", start)


def _tokens(selector: str) -> dict[str, str]:
    start, end = _block(selector)
    return dict(re.findall(r"--([\w-]+):\s*([^;]+);", CSS[start:end]))


LIGHT = _tokens(":root")
THEMES = {"light": LIGHT, "dark": {**LIGHT, **_tokens(':root[data-theme="dark"]')}}

PAIRS = [
    # (foreground, background, minimum): where the pair appears
    ("color-text", "color-bg", TEXT),
    ("color-text", "color-surface", TEXT),
    ("color-text", "color-surface-sunken", TEXT),  # messages, chips, table hover
    ("color-text-muted", "color-bg", TEXT),
    ("color-text-muted", "color-surface", TEXT),
    ("color-text-muted", "color-surface-sunken", TEXT),
    ("color-accent", "color-bg", TEXT),  # links
    ("color-accent", "color-surface", TEXT),
    ("color-accent", "color-accent-soft", TEXT),  # active nav item, pressed toggle
    ("color-on-accent", "color-accent", TEXT),  # primary buttons
    ("outcome-auto", "outcome-auto-soft", TEXT),  # badges
    ("outcome-review", "outcome-review-soft", TEXT),
    ("outcome-abstain", "outcome-abstain-soft", TEXT),
    ("outcome-block", "outcome-block-soft", TEXT),
    ("color-on-solid", "color-danger-solid", TEXT),  # the stat chip
    *[("color-on-outcome", f"outcome-{o}", TEXT) for o in ("auto", "review", "abstain", "block")],
    ("color-warning", "color-warning-soft", TEXT),  # sandbox tag, review count
    ("color-text", "outcome-review-soft", TEXT),  # a cell that needs attention
    ("color-text", "color-warning-soft", TEXT),  # an override waiting to be sent (Undo)
    ("color-accent", "color-warning-soft", TEXT),  # "Sign in again." when a session ended
    ("color-text", "outcome-block-soft", TEXT),  # confusion matrix: a mistake over a tenth of a row
    ("color-text-muted", "outcome-review-soft", TEXT),
    ("color-bg", "color-text", TEXT),  # toast
    # Phase 4: numbers that need attention, rating counts, result headers and their links.
    ("color-warning", "color-surface", TEXT),
    ("color-danger", "color-surface", TEXT),
    ("outcome-auto", "color-surface", TEXT),
    ("outcome-block", "color-surface", TEXT),
    *[("color-text", f"outcome-{o}-soft", TEXT) for o in ("auto", "abstain")],
    *[("color-text-muted", f"outcome-{o}-soft", TEXT) for o in ("block", "abstain")],
    *[("color-accent", f"outcome-{o}-soft", TEXT) for o in ("auto", "review", "abstain")],
    ("color-border-strong", "color-bg", UI),  # form fields
    ("color-border-strong", "color-surface", UI),
    ("color-border-strong", "color-surface-sunken", UI),
    ("color-accent", "color-surface-sunken", UI),  # focus ring on sunken areas
    *[(f"chart-{i}", "color-surface", UI) for i in range(1, 7)],
    *[(f"outcome-{o}", "color-surface-sunken", UI) for o in ("auto", "review", "abstain", "block")],
    # Chart marks sit on the surface: outcome columns, danger diamonds, the deployed ring.
    *[(f"outcome-{o}", "color-surface", UI) for o in ("auto", "review", "abstain", "block")],
    ("color-danger", "color-surface", UI),
    ("color-accent", "color-surface", UI),
    ("color-text-muted", "color-surface", UI),  # chart axes, reference lines and gridline labels
    # Phase 5, the developer landing (/docs): examples and their bars are text and muted text on
    # the sunken surface, step numbers accent on accent-soft, the diagram's boxes text and muted
    # text on the surface with border-strong edges and muted arrows on the page, its outcomes and
    # chips the outcome pairs: all above. New: a copy button that has copied turns success.
    ("color-success", "color-surface", TEXT),
    ("color-text-muted", "color-bg", UI),  # the diagram's arrows
    # Community packs: where a pack comes from, as a badge on the surface and the page, and the
    # notice that a community pack is not clinically reviewed (text on the soft colour).
    ("origin-community", "origin-community-soft", TEXT),
    ("origin-other", "origin-other-soft", TEXT),
    ("color-text", "origin-community-soft", TEXT),
]


def _hex(tokens: dict[str, str], name: str) -> str:
    value = tokens[name].strip()
    while value.startswith("var(--"):
        value = tokens[value[6:-1]].strip()
    assert re.fullmatch(r"#[0-9a-fA-F]{6}", value), f"--{name} is {value!r}, expected #rrggbb"
    return value


def _contrast(foreground: str, background: str) -> float:
    def luminance(color: str) -> float:
        channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    high, low = sorted([luminance(foreground), luminance(background)], reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_every_colour_pair_meets_wcag_aa(theme: str) -> None:
    tokens = THEMES[theme]
    failing = [
        f"--{fg} on --{bg}: {ratio:.2f}:1 (needs {minimum}:1)"
        for fg, bg, minimum in PAIRS
        if (ratio := _contrast(_hex(tokens, fg), _hex(tokens, bg))) < minimum
    ]
    assert failing == []


def _components() -> str:
    """The stylesheet without its token blocks."""
    outside = CSS
    for selector in (':root[data-theme="dark"]', ":root"):
        start, end = _block(selector)
        outside = outside[:start] + outside[end:]
    return outside


def test_components_use_tokens_not_hex_colours() -> None:
    assert re.findall(r"#[0-9a-fA-F]{3,8}\b", _components()) == []


def test_components_use_the_spacing_and_layer_scales() -> None:
    """Spacing comes from --space-* (1 px hairlines aside) and stacking from --z-*."""
    declarations = re.findall(r"(?<![\w-])([a-z-]+)\s*:\s*([^;{}]+);", _components())
    spacing = r"(padding|margin|gap|row-gap|column-gap|inset|top|right|bottom|left)(-[a-z]+)*"
    raw = [
        f"{prop}: {value}"
        for prop, value in declarations
        if re.fullmatch(spacing, prop) and re.search(r"(?<![\w.-])(?!1px)\d+(\.\d+)?px", value)
    ]
    layers = [v for prop, v in declarations if prop == "z-index" and not v.startswith("var(--z-")]
    assert raw == [] and layers == []
