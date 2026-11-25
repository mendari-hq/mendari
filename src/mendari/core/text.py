# SPDX-License-Identifier: Apache-2.0
"""The plain form of a text: what the providers that decide on this machine read.

An agent's patterns are written as a keyboard types them: a straight apostrophe, one space between
words. The text they are matched against is written by phones, web forms, word processors and
language models, which write the same words otherwise: a curly apostrophe (U+2019, a phone's
default), a no-break space, a zero-width space carried in pasted text, an accent as a separate
character, an en dash in a range ("every 4 to 6 hours" written 4, en dash, 6). Matched as written,
each makes a pattern miss what it was written to find. On the test fixtures' own hand-written
emergencies, the curly apostrophe alone took the stat priority off 3 of the 55 the keyword rules
flagged ("it won't stop bleeding").

So every provider that reads free text here reads its plain form, `plain` (`rules` through
`providers.base.text_from`), and text that differs only in such typography decides the same. The
plain form is the same words in a keyboard's characters:

1. a zero-width space becomes a space, as it separates words, and the other invisible format
   characters are left out (Unicode category Cf: zero-width joiners, the byte order mark, the
   soft hyphen, direction marks), as they sit inside words;
2. a letter and its accent written as two characters become the one character (Unicode NFC),
   unless composing would make the text longer (a few characters of Hebrew, Indic scripts and
   musical notation compose to more), where the text stays as it is;
3. apostrophes and quotation marks become `'` and `"`, dashes and the minus sign `-`, and the
   ellipsis character a full stop, where a sentence ends as at three full stops; between two
   digits it becomes a hyphen instead, where a full stop would read as a decimal point;
4. a carriage return before a newline is left out, and every run of white space that holds no
   newline becomes one space.

A newline stays a newline: a pack's patterns stop at one, as at the end of a sentence, so text
wrapped mid-phrase is still read as two lines. Every other character that ends a line somewhere
(a lone carriage return, a vertical tab, a form feed, the line and paragraph separators) is
white space, as a pattern has always read it. Nothing else changes: no case folding (patterns
ignore case themselves), no compatibility folding (the "fi" ligature, full-width letters and the
micro sign stay as they are), no spelling (`rules` may tolerate typos, an agent's choice).

Three things a pattern matched in the text as received and does not match in its plain form,
each as the same words typed on a keyboard already decide:

- a format character other than the zero-width space as the only thing between two words: the
  words join, as they are shown;
- a negation cue that many spaces kept outside a rule's `negation_window`: the run is one space
  now, so the cue is as near as it reads;
- a pattern or a negation reaching across an ellipsis character: it ends there, as at "...".

The plain form is never longer than the text, so the length the patterns of a bounded agent
run on (`core/origin.py`) holds for it too, and it is its own plain form. A decision's
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
_ZERO_WIDTH_SPACE = "\N{ZERO WIDTH SPACE}"
_ELLIPSIS = "\N{HORIZONTAL ELLIPSIS}"
_KEYBOARD = str.maketrans(
    {
        **dict.fromkeys(_APOSTROPHES, "'"),
        **dict.fromkeys(_QUOTATION_MARKS, '"'),
        **dict.fromkeys(_DASHES, "-"),
        _ELLIPSIS: ".",
    }
)
_BETWEEN_DIGITS = re.compile(rf"(?<=\d){_ELLIPSIS}(?=\d)")  # as in a range of doses
_SPACES = re.compile(r"[^\S\n]+")  # a run of white space that holds no newline


def plain(text: str) -> str:
    """`text` in its plain form (the module's docstring): never longer than `text`, and its own
    plain form."""
    if not text.isascii():  # ASCII holds no format character, ellipsis or accent to compose
        text = "".join(
            " " if ch == _ZERO_WIDTH_SPACE else ch
            for ch in text
            if ch == _ZERO_WIDTH_SPACE or unicodedata.category(ch) != "Cf"
        )
        composed = unicodedata.normalize("NFC", text)
        if len(composed) <= len(text):
            text = composed
        text = _BETWEEN_DIGITS.sub("-", text)
    return _SPACES.sub(" ", text.replace("\r\n", "\n").translate(_KEYBOARD))
