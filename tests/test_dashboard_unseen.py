# SPDX-License-Identifier: Apache-2.0
"""The characters the console shows by their code point are the ones the engine takes out of
what an agent writes: one set, written twice (`providers/agent.py` `_UNSEEN`, `dashboard/ui.js`
`UNSEEN`). An action's arguments are kept as the agent sent them, so the console is where a
reviewer is shown what the engine would have removed from a text."""

from __future__ import annotations

import re
import sys

from mendari.providers import agent as agent_provider
from tests.conftest import ROOT

UI = ROOT / "src" / "mendari" / "dashboard" / "ui.js"
_TOKEN = re.compile(r"\\u\{([0-9a-fA-F]+)\}|\\u([0-9a-fA-F]{4})|(-)")


def console_set() -> set[int]:
    """The code points of `UNSEEN`'s character class, read from the script."""
    source = UI.read_text(encoding="utf-8")
    found = re.search(r"^const UNSEEN = /\[(.+)\]/gu;$", source, re.M)
    assert found is not None, "ui.js: const UNSEEN = /[…]/gu;"
    listed: set[int] = set()
    previous, ranging, at = None, False, 0
    body = found.group(1)
    while at < len(body):
        token = _TOKEN.match(body, at)
        assert token is not None, body[at : at + 12]  # only escapes and ranges: nothing literal
        at = token.end()
        if token.group(3):
            ranging = True
            continue
        code = int(token.group(1) or token.group(2), 16)
        if ranging:
            assert previous is not None and previous <= code
            listed.update(range(previous, code + 1))
            ranging = False
        else:
            listed.add(code)
        previous = code
    assert not ranging
    return listed


def engine_set() -> set[int]:
    """The code points the engine takes out of a text, but for the lone surrogates, which are
    no characters (`core/text.py` `well_formed` reads each as U+FFFD before)."""
    one = re.compile(f"[{agent_provider._UNSEEN}]")
    return {
        code
        for code in range(sys.maxunicode + 1)
        if not 0xD800 <= code <= 0xDFFF and one.fullmatch(chr(code))
    }


def test_the_console_shows_what_the_engine_takes_out() -> None:
    engine, console = engine_set(), console_set()
    assert console == engine, (sorted(console - engine)[:5], sorted(engine - console)[:5])
    # What stays, in both: the line break and the tab, the joiners a script or an emoji needs,
    # and the two selectors that say how an emoji is drawn.
    for kept in (0x09, 0x0A, 0x200C, 0x200D, 0xFE0E, 0xFE0F, 0x20, 0xA0):
        assert kept not in engine, hex(kept)
    # What goes: every default-ignorable code point but those four, the controls, the line
    # separators, the interlinear annotation marks and the braille blank.
    ignorable = {
        0x00AD, 0x034F, 0x061C, 0x115F, 0x1160, 0x17B4, 0x17B5, *range(0x180B, 0x1810),
        *range(0x200B, 0x2010), *range(0x202A, 0x202F), *range(0x2060, 0x2070), 0x3164,
        *range(0xFE00, 0xFE10), 0xFEFF, 0xFFA0, *range(0xFFF0, 0xFFF9),
        *range(0x1BCA0, 0x1BCA4), *range(0x1D173, 0x1D17B), *range(0xE0000, 0xE1000),
    } - {0x200C, 0x200D, 0xFE0E, 0xFE0F}  # fmt: skip
    controls = {*range(0x00, 0x09), *range(0x0B, 0x20), *range(0x7F, 0xA0)}
    others = {0x2028, 0x2029, 0x2800, 0xFFF9, 0xFFFA, 0xFFFB}
    assert engine == ignorable | controls | others
