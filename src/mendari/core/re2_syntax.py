r"""A community pack's patterns, written in Python's `re` syntax, rewritten for RE2.

A pack author writes `re` patterns (the spec, the guide and every maintained pack use them).
A community pack's patterns run on RE2 (`core/patterns.py`), which never backtracks.
`translate` parses a pattern the way `re` does (`re._parser`, the same parse `re.compile`
makes) and writes out an RE2 pattern that matches the same text, spelling out what `re` means
where RE2 would read the same characters differently:

- `\w`, `\d` and `\s` (and `\W`, `\D`, `\S`) are `re`'s Unicode classes, listed from this
  Python's own tables (RE2's are ASCII only); with `(?a)` they are ASCII, as in `re`.
- `\b` and `\B` next to a character the pattern names are `re`'s Unicode word boundaries
  (RE2's are ASCII only): `\bmarché\b` matches "marché", and `\bé+\b` matches "ééé". On
  the text's side of a match RE2 counts only ASCII letters, digits and `_` as word characters,
  so a letter or digit outside ASCII just outside the match does not continue the word:
  `\bpie` also matches in "épie", and `\bma\b` inside "mañana" (`re` matches neither). A
  boundary next to something that may match nothing (`\b\w*`) is RE2's ASCII one.
- `$` is `re`'s: the end of the text, or before a newline that ends it; `\Z` is the end only;
  `a{,3}` is `a{0,3}` (RE2 reads it as text).
- A count above 1,000 (RE2's largest) is split into counts of at most 1,000 one after another,
  which match the same texts. Counts nested inside each other may multiply to at most 1,000:
  RE2 refuses `(?:a{1,50}){1,50}`.
- Case is ignored the same way in both engines but for two letters: with case ignored, `re`
  also matches `i` and `I` to the dotless i, U+0131 (RE2 does not), and with `(?a)` it folds ASCII
  letters only, where RE2 also folds the Kelvin sign and the long s to `k` and `s`.
- A lone surrogate (U+D800 to U+DFFF) is matched by nothing: a text holding one has it
  replaced with U+FFFD before a pattern runs (`core/patterns.py`).

What RE2 cannot run is refused with what to write instead (`Unsupported`): lookahead and
lookbehind, back references, conditional groups, atomic groups and possessive quantifiers.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Iterable, Sequence
from typing import Any

# The parser `re.compile` uses; private, so imported by name (as `community/regex_safety.py`).
_parser: Any = __import__("re._parser", fromlist=["parse"])
_c: Any = __import__("re._constants", fromlist=["MAXREPEAT"])

MAX_COUNT = 1_000  # RE2's largest count in `{m,n}`
_TOP = 0x10FFFF
_SURROGATES = (0xD800, 0xDFFF)
Ranges = tuple[tuple[int, int], ...]  # sorted, disjoint, without surrogates
Item = tuple[Any, Any]  # an (opcode, argument) pair of the parse


class Unsupported(ValueError):
    """A pattern RE2 cannot run; the message says what to write instead."""


def translate(pattern: str, flags: int = 0) -> str:
    """The RE2 pattern for a `re` pattern compiled with `flags` (module docstring). Raises
    `re.error` for a pattern `re` refuses, and `Unsupported` for one RE2 cannot run."""
    tree = _parser.parse(pattern, flags)
    flags = tree.state.flags
    body = _Writer().sequence(list(tree), flags)
    return f"(?i:{body})" if flags & _c.SRE_FLAG_IGNORECASE else body


# -- character sets ------------------------------------------------------------------------------


def _merged(ranges: Iterable[tuple[int, int]]) -> Ranges:
    """Ranges sorted and merged, surrogates removed."""
    out: list[tuple[int, int]] = []
    for low, high in sorted(_unsurrogated(ranges)):
        if out and low <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], high))
        else:
            out.append((low, high))
    return tuple(out)


def _unsurrogated(ranges: Iterable[tuple[int, int]]) -> Iterable[tuple[int, int]]:
    for low, high in ranges:
        if high < _SURROGATES[0] or low > _SURROGATES[1]:
            yield low, high
            continue
        if low < _SURROGATES[0]:
            yield low, _SURROGATES[0] - 1
        if high > _SURROGATES[1]:
            yield _SURROGATES[1] + 1, high


def _complement(ranges: Ranges) -> Ranges:
    out, start = [], 0
    for low, high in ranges:
        if low > start:
            out.append((start, low - 1))
        start = high + 1
    if start <= _TOP:
        out.append((start, _TOP))
    return _merged(out)


def _intersection(a: Ranges, b: Ranges) -> Ranges:
    return _complement(_merged(_complement(a) + _complement(b)))


@functools.cache
def _table(category: str) -> Ranges:
    """Every character `re` matches with the Unicode class `category` (`\\w`, `\\d`, `\\s`),
    found by `re` itself, so the lists are this Python's."""
    every = "".join(map(chr, range(_TOP + 1)))
    return _merged((m.start(), m.end() - 1) for m in re.finditer(category + "+", every))


_ASCII_WORD: Ranges = ((0x30, 0x39), (0x41, 0x5A), (0x5F, 0x5F), (0x61, 0x7A))
_ASCII = {
    "DIGIT": ((0x30, 0x39),),
    "SPACE": ((0x09, 0x0D), (0x20, 0x20)),
    "WORD": _ASCII_WORD,
}
_UNICODE = {"DIGIT": r"\d", "SPACE": r"\s", "WORD": r"\w"}


def _category(name: str, ascii_only: bool) -> Ranges:
    """The characters of a `re` category (CATEGORY_WORD, CATEGORY_NOT_DIGIT, ...)."""
    kind = name.removeprefix("CATEGORY_")
    negated = kind.startswith("NOT_")
    kind = kind.removeprefix("NOT_")
    ranges = _ASCII[kind] if ascii_only else _table(_UNICODE[kind])
    return _complement(ranges) if negated else ranges


@functools.cache
def _non_ascii_word() -> Ranges:
    """Word characters RE2's ASCII `\\b` does not count as word characters."""
    return _intersection(_table(r"\w"), _complement(_ASCII_WORD))


def _char(code: int) -> str:
    char = chr(code)
    return char if char.isascii() and (char.isalnum() or char == "_") else f"\\x{{{code:x}}}"


def _class(ranges: Ranges, negated: bool = False) -> str:
    if not ranges:  # nothing, or everything
        return "(?s:.)" if negated else "[^\\x{0}-\\x{10ffff}]"
    body = "".join(_char(lo) if lo == hi else f"{_char(lo)}-{_char(hi)}" for lo, hi in ranges)
    return f"[{'^' if negated else ''}{body}]"


# -- writing -------------------------------------------------------------------------------------

_FLIP = {"b": "B", "B": "b"}
_REFUSED = {
    "ASSERT": "a lookahead or lookbehind ((?=...), (?<=...))",
    "ASSERT_NOT": "a negative lookahead or lookbehind ((?!...), (?<!...))",
    "GROUPREF": "a back reference (\\1, (?P=name))",
    "GROUPREF_EXISTS": "a conditional group ((?(1)...|...))",
    "ATOMIC_GROUP": "an atomic group ((?>...))",
    "POSSESSIVE_REPEAT": "a possessive quantifier (*+, ++, ?+, {m,n}+)",
}
_INSTEAD = {
    "ASSERT": "match the text itself instead",
    "ASSERT_NOT": "match what may come there instead (a class such as [^x]), or write a second "
    "rule",
    "GROUPREF": "write the group's pattern again instead",
    "GROUPREF_EXISTS": "write the alternatives with | instead",
    "ATOMIC_GROUP": "write (?:...) instead: RE2 never backtracks, so it is not needed",
    "POSSESSIVE_REPEAT": "drop the last +: RE2 never backtracks, so it is not needed",
}
_SINGLE = ("LITERAL", "NOT_LITERAL", "ANY", "IN")
_REPEATS = ("MAX_REPEAT", "MIN_REPEAT")


class _Writer:
    """Writes parse items as RE2 text. A Unicode word boundary next to an item travels into
    it as `lead` (before it) or `trail` (after it), down to the single characters at its edge,
    where it becomes RE2's ASCII `\\b` or `\\B`, whichever is right for that character."""

    def sequence(self, items: Sequence[Item], flags: int, lead: str = "", trail: str = "") -> str:
        leads, trails = [""] * len(items), [""] * len(items)
        kept = [True] * len(items)
        for index, (op, av) in enumerate(items):
            boundary = _boundary(op, av)
            if boundary and not flags & _c.SRE_FLAG_ASCII:
                side = self._side(items, index, flags)
                if side == "next":
                    leads[index + 1], kept[index] = boundary, False
                elif side == "previous":
                    trails[index - 1], kept[index] = boundary, False
        if items and lead and _movable(items[0]):
            leads[0], lead = lead, ""
        if items and trail and _movable(items[-1]):
            trails[-1], trail = trail, ""
        out = "".join(
            self.item(item, flags, leads[i], trails[i]) for i, item in enumerate(items) if kept[i]
        )
        return _plain(lead) + out + _plain(trail)

    def _side(self, items: Sequence[Item], index: int, flags: int) -> str:
        """Which neighbour a boundary at `index` travels into: the one whose other side names
        no letter or digit outside ASCII (so the result is exact), else the next one; "" when
        neither may hold one (RE2's own `\\b` is exact) or neither can take it."""
        before = items[index - 1] if index else None
        after = items[index + 1] if index + 1 < len(items) else None
        wide_before = before is None or _wide(before, "end", flags)
        wide_after = after is None or _wide(after, "start", flags)
        can_after = after is not None and _movable(after)
        can_before = before is not None and _movable(before)
        if not wide_before and not wide_after:
            return ""
        if not wide_before and can_after:
            return "next"
        if not wide_after and can_before:
            return "previous"
        return "next" if can_after else "previous" if can_before else ""

    def item(self, item: Item, flags: int, lead: str = "", trail: str = "") -> str:
        op, av = item
        name = str(op)
        if name in _REFUSED:
            raise Unsupported(
                f"uses {_REFUSED[name]}, which RE2 (the engine a community pack's patterns "
                f"run on) does not have: {_INSTEAD[name]}"
            )
        if name in _SINGLE:
            return self._single(item, flags, lead, trail)
        if name == "AT":
            return _at(av, flags)
        if name == "SUBPATTERN":
            _, added, removed, inner = av
            inside = (flags | added) & ~removed
            text = self.sequence(list(inner), inside, lead, trail)
            case = _c.SRE_FLAG_IGNORECASE
            if inside & case and not flags & case:
                return f"(?i:{text})"
            if flags & case and not inside & case:
                return f"(?-i:{text})"
            return f"(?:{text})"
        if name == "BRANCH":
            return "(?:" + "|".join(self.sequence(list(a), flags, lead, trail) for a in av[1]) + ")"
        if name in _REPEATS:
            return self._repeat(av, flags, lead, trail, lazy=name == "MIN_REPEAT")
        raise Unsupported(f"uses {name}, which RE2 does not have")

    def _single(self, item: Item, flags: int, lead: str, trail: str) -> str:
        negated, chars = _chars(item, flags)
        if not lead and not trail:
            if str(item[0]) == "LITERAL":
                return _char(item[1]) if chars else _class(())
            return _class(chars, negated)
        wide = _non_ascii_word()
        if negated:  # what RE2 negates after folding case: the part's complement
            parts = [
                (_merged(chars + wide), True, False),
                (_merged(chars + _complement(wide)), True, True),
            ]
        else:
            parts = [
                (_intersection(chars, _complement(wide)), False, False),
                (_intersection(chars, wide), False, True),
            ]
        written = [
            _bounded(_class(ranges, negate), lead, trail, flip)
            for ranges, negate, flip in parts
            if (_complement(ranges) if negate else ranges)
        ]
        if not written:
            return _class(())
        return written[0] if len(written) == 1 else "(?:" + "|".join(written) + ")"

    def _repeat(self, av: Any, flags: int, lead: str, trail: str, *, lazy: bool) -> str:
        low, high, body = av[0], av[1], list(av[2])
        unbounded = high == _c.MAXREPEAT

        def less(count: int) -> int:
            return high if unbounded else high - count

        if low == 0 or not (lead or trail):
            return (
                _plain(lead) + _counted(self.sequence(body, flags), low, high, lazy) + _plain(trail)
            )
        if lead and trail and low == 1 and high == 1:
            return self.sequence(body, flags, lead, trail)
        if lead and trail and low == 1:  # one copy holding both, or a first and a last
            both = self.sequence(body, flags, lead, trail)
            rest = _counted(self.sequence(body, flags), 0, less(2), lazy)
            ends = self.sequence(body, flags, lead) + rest + self.sequence(body, flags, "", trail)
            return f"(?:{both}|{ends})" if lazy else f"(?:{ends}|{both})"
        first = self.sequence(body, flags, lead) if lead else ""
        last = self.sequence(body, flags, "", trail) if trail else ""
        peeled = (1 if lead else 0) + (1 if trail else 0)
        middle = _counted(self.sequence(body, flags), low - peeled, less(peeled), lazy)
        return first + middle + last


def _boundary(op: Any, av: Any) -> str:
    if str(op) != "AT":
        return ""
    return {"AT_BOUNDARY": "b", "AT_NON_BOUNDARY": "B"}.get(str(av), "")


def _movable(item: Item) -> bool:
    """An item a boundary can travel into: one that matches text."""
    return str(item[0]) != "AT"


def _plain(boundary: str) -> str:
    return f"\\{boundary}" if boundary else ""


def _bounded(text: str, lead: str, trail: str, flip: bool) -> str:
    """A single character with boundaries around it: flipped for a letter or digit outside
    ASCII, which RE2's `\\b` counts as a non-word character."""
    before = _FLIP[lead] if flip and lead else lead
    after = _FLIP[trail] if flip and trail else trail
    return _plain(before) + text + _plain(after)


def _chars(item: Item, flags: int) -> tuple[bool, Ranges]:
    """(negated, characters) of a single-character item: it matches every character in the
    ranges, or with `negated`, every one outside them."""
    op, av = item
    name = str(op)
    if name == "LITERAL":
        return False, _merged([(av, av)])
    if name == "NOT_LITERAL":
        return True, _merged([(av, av)])
    if name == "ANY":
        return True, () if flags & _c.SRE_FLAG_DOTALL else ((0x0A, 0x0A),)
    negated, found = False, []
    for kind, value in av:
        kind = str(kind)
        if kind == "NEGATE":
            negated = True
        elif kind == "LITERAL":
            found.append((value, value))
        elif kind == "RANGE":
            found.append(value)
        elif kind == "CATEGORY":
            found += _category(str(value), bool(flags & _c.SRE_FLAG_ASCII))
        else:
            raise Unsupported(f"uses {kind} in a class, which RE2 does not have")
    return negated, _merged(found)


def _wide(item: Item, edge: str, flags: int) -> bool:
    """Whether the character at an edge ("start" or "end") of what `item` matches may be a
    letter or digit outside ASCII, or is not known."""
    op, av = item
    name = str(op)
    if name in _SINGLE:
        negated, chars = _chars(item, flags)
        wide = _non_ascii_word()
        if negated:
            return _intersection(wide, _complement(chars)) != ()
        return _intersection(wide, chars) != ()
    if name == "SUBPATTERN":
        _, added, removed, inner = av
        return _wide_sequence(list(inner), edge, (flags | added) & ~removed)
    if name == "BRANCH":
        return any(_wide_sequence(list(a), edge, flags) for a in av[1])
    if name in _REPEATS and av[0] >= 1:
        return _wide_sequence(list(av[2]), edge, flags)
    return True


def _wide_sequence(items: list[Item], edge: str, flags: int) -> bool:
    matching = [item for item in items if str(item[0]) != "AT"]
    if not matching:
        return True
    return _wide(matching[0] if edge == "start" else matching[-1], edge, flags)


def _at(av: Any, flags: int) -> str:
    where = str(av)
    multiline = flags & _c.SRE_FLAG_MULTILINE
    if where == "AT_BEGINNING":
        return "(?m:^)" if multiline else "\\A"
    if where == "AT_BEGINNING_STRING":
        return "\\A"
    if where == "AT_END":
        return "(?m:$)" if multiline else "(?:\\n?\\z)"
    if where == "AT_END_STRING":
        return "\\z"
    if where in ("AT_BOUNDARY", "AT_NON_BOUNDARY"):
        return _plain(_boundary("AT", av))
    raise Unsupported(f"uses {where}, which RE2 does not have")


def _counted(body: str, low: int, high: int, lazy: bool) -> str:
    """`body` repeated from `low` to `high` times (`high` MAXREPEAT: unbounded), as counts of
    at most `MAX_COUNT` one after another."""
    lazy_mark = "?" if lazy else ""
    unbounded = high == _c.MAXREPEAT
    if body == "":
        return ""
    if unbounded and low <= MAX_COUNT:
        return f"(?:{body}){{{low},}}{lazy_mark}"
    if not unbounded and high <= MAX_COUNT:
        if low == high == 0:
            return ""
        exact = f"{{{low}}}" if low == high else f"{{{low},{high}}}"
        return f"(?:{body}){exact}{lazy_mark}"
    parts = []
    required = low
    while required > 0:
        count = min(required, MAX_COUNT)
        parts.append(f"(?:{body}){{{count}}}")
        required -= count
    if unbounded:
        parts.append(f"(?:{body})*{lazy_mark}")
    else:
        optional = high - low
        while optional > 0:
            count = min(optional, MAX_COUNT)
            parts.append(f"(?:{body}){{0,{count}}}{lazy_mark}")
            optional -= count
    return "".join(parts)
