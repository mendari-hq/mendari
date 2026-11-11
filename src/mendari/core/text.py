# SPDX-License-Identifier: Apache-2.0
"""The plain form of a text: what the providers that decide on this machine read.

A pack's patterns and a trained model's words are written as a keyboard types them: a straight
apostrophe, one space between words. The text they are matched against is written by phones,
web forms, word processors and language models, which write the same words otherwise: a curly
apostrophe (U+2019, a phone's default), a no-break space, a zero-width space carried in pasted
text, an accent as a separate character, an en dash in a range ("every 4 to 6 hours" written
4, en dash, 6). Matched as written, each makes a pattern miss what it was written to find. On the
reference packs' own hand-written emergencies, the curly apostrophe alone took the stat priority
off 3 of the 55 the keyword rules flagged ("it won't stop bleeding").

So every provider that reads free text here reads its plain form, `plain` (`rules`, `dosing`
and `naive_bayes` through `providers.base.text_from`, and `criteria` a medication's name), and
text that differs only in such typography decides the same. The plain form is the same words in
a keyboard's characters:

1. invisible format characters are left out (Unicode category Cf: zero-width spaces and joiners,
   the byte order mark, the soft hyphen, direction marks);
2. a letter and its accent written as two characters become the one character (Unicode NFC),
   unless composing would make the text longer (a few characters of Hebrew, Indic scripts and
   musical notation compose to more), where the text stays as it is;
3. apostrophes and quotation marks become `'` and `"`, dashes and the minus sign `-`, and the
   ellipsis character a full stop: a sentence ends there as at three full stops;
4. every line end becomes a newline, and every other run of white space one space.

A newline stays a newline: a pack's patterns stop at one, as at the end of a sentence, so text
wrapped mid-phrase is still read as two lines. Nothing else changes: no case folding (patterns
ignore case themselves), no compatibility folding (the "fi" ligature, full-width letters and the
micro sign stay as they are), no spelling (`rules` may tolerate typos, a pack's choice).

The plain form is never longer than the text, so the length a pack on the community engine's
patterns run on (`core/origin.py`) holds for it too, and it is its own plain form. A decision's
record, its input hash, and what a remote model is sent keep the text as it was received.
"""

from __future__ import annotations

import re
import unicodedata

_APOSTROPHES = (
    "\N{LEFT SINGLE QUOTATION MARK}\N{RIGHT SINGLE QUOTATION MARK}"
    "\N{SINGLE LOW-9 QUOTATION MARK}\N{SINGLE HIGH-REVERSED-9 QUOTATION MARK}"
    "\N{PRIME}\N{MODIFIER LETTER APOSTROPHE}\N{ACUTE ACCENT}\N{GRAVE ACCENT}"
)
_QUOTATION_MARKS = (
    "\N{LEFT DOUBLE QUOTATION MARK}\N{RIGHT DOUBLE QUOTATION MARK}"
    "\N{DOUBLE LOW-9 QUOTATION MARK}\N{DOUBLE HIGH-REVERSED-9 QUOTATION MARK}"
    "\N{DOUBLE PRIME}"
)
_DASHES = (
    "\N{HYPHEN}\N{NON-BREAKING HYPHEN}\N{FIGURE DASH}\N{EN DASH}\N{EM DASH}"
    "\N{HORIZONTAL BAR}\N{MINUS SIGN}"
)
# A carriage return (alone, or before a newline), a vertical tab, a form feed, the next-line
# control, and the line and paragraph separators.
_LINE_ENDS = "\r\x0b\x0c\x85\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}"
_KEYBOARD = str.maketrans(
    {
        **dict.fromkeys(_APOSTROPHES, "'"),
        **dict.fromkeys(_QUOTATION_MARKS, '"'),
        **dict.fromkeys(_DASHES, "-"),
        **dict.fromkeys(_LINE_ENDS, "\n"),
        "\N{HORIZONTAL ELLIPSIS}": ".",
    }
)
_SPACES = re.compile(r"[^\S\n]+")  # a run of white space that holds no newline


def plain(text: str) -> str:
    """`text` in its plain form (the module's docstring): never longer than `text`, and its own
    plain form."""
    if not text.isascii():  # ASCII holds no format character and nothing to compose
        text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
        composed = unicodedata.normalize("NFC", text)
        if len(composed) <= len(text):
            text = composed
    return _SPACES.sub(" ", text.replace("\r\n", "\n").translate(_KEYBOARD))
