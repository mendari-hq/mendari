r"""A community pack's patterns, written in Python's `re` syntax, rewritten for RE2.

A pack author writes `re` patterns (the spec, the guide and every maintained pack use them).
A community pack's patterns run on RE2 (`core/patterns.py`), which never backtracks.
`translate` parses a pattern the way `re` does (`re._parser`, the same parse `re.compile`
makes) and writes out an RE2 pattern that matches the same text, spelling out what `re` means
where RE2 would read the same characters differently:

- `\w`, `\d` and `\s` (and `\W`, `\D`, `\S`) are `re`'s Unicode classes, listed from this
  Python's own tables (RE2's are ASCII only); with `(?a)` they are ASCII, as in `re`.
- With case ignored, every character, class and `.` is written out as the characters `re`
  itself matches there (found by asking `re`, for every character case can relate to another),
  and RE2 folds no case: `ı`, `İ`, `I` and `i` match each other as in `re`, and `\W` never
  matches `ι` (RE2 would fold the class's U+0345 into it). With `(?a)`, as `re`, ASCII only.
- `\b` and `\B` next to a character the pattern names are written with RE2's own `\b` and
  `\B`, which see only ASCII letters, digits and `_` as word characters *in the text*: right
  wherever the character just outside the match is in ASCII. A letter or digit outside ASCII
  just outside the match does not continue the word: `\bpie` also matches in "épie", `\bma\b`
  inside "mañana", and next to letters outside ASCII the pattern names, `\bрак\b` inside
  "практика" (`re` matches none of them). `boundary_problem` says when a pattern has such a
  boundary; `community-check` warns on it. A boundary next to something that may match nothing
  (`\b\w*`) is RE2's ASCII one.
- `$` is `re`'s: the end of the text, or before a newline that ends it; `\Z` is the end only;
  `a{,3}` is `a{0,3}` (RE2 reads it as text).
- A count above 1,000 (RE2's largest) is split into counts of at most 1,000 one after another,
  which match the same texts. Counts nested inside each other may multiply to at most 1,000:
  RE2 refuses `(?:a{1,50}){1,50}`.
- A lone surrogate (U+D800 to U+DFFF) is matched by nothing: a text holding one has it
  replaced with U+FFFD before a pattern runs (`core/patterns.py`).

What RE2 cannot run is refused with what to write instead (`Unsupported`): lookahead and
lookbehind, back references, conditional groups, atomic groups and possessive quantifiers.

`size` is how much of RE2's work a pattern can make on each character of a text
(`core/caps.py` caps a community pack's total).
"""  # noqa: RUF002 (the letters the differences are about)

from __future__ import annotations

import functools
import re
import warnings
from collections.abc import Iterable, Sequence
from typing import Any

# The parser `re.compile` uses; private, so imported by name.
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
    return _Writer().sequence(list(tree), tree.state.flags)


def size(pattern: str, flags: int = 0) -> int:
    """How much of RE2's work `pattern` can make on each character of a text, at most: the
    characters, classes and `.`s it holds, each as often as the counts around it repeat it
    (`a{1,50}` is 50, `a{5,}` is 5, `\\w+` and `x*` are 1), alternatives added together.
    RE2 runs a pattern's copies of these side by side, so its time on a text grows with this
    number times the text's length (measured in `core/caps.py`). Raises `re.error` for a
    pattern `re` refuses."""
    return _size(list(_parsed(pattern, flags)))


def _size(items: Sequence[Item]) -> int:
    total = 0
    for op, av in items:
        name = str(op)
        if name in _SINGLE:
            total += 1
        elif name == "SUBPATTERN":
            total += _size(list(av[3]))
        elif name == "BRANCH":
            total += sum(_size(list(a)) for a in av[1])
        elif name in _REPEATS:
            low, high, body = av
            copies = low if high == _c.MAXREPEAT else high
            total += max(copies, 1) * _size(list(body))
    return total  # what RE2 cannot run counts nothing: such a pattern is refused anyway


def boundary_problem(pattern: str, flags: int = 0) -> bool:
    """Whether `pattern` has a `\\b` or `\\B` next to something that may match a letter or digit
    outside ASCII (module docstring): RE2 then decides it as if a letter or digit outside ASCII
    just outside the match did not continue the word, so `\\bрак\\b` matches inside "практика".
    Raises `re.error` for a pattern `re` refuses."""  # noqa: RUF002
    tree = _parsed(pattern, flags)
    return _boundary_near_wide(list(tree), tree.state.flags)


def _parsed(pattern: str, flags: int) -> Any:
    """`re`'s parse of `pattern`, its warnings (a FutureWarning on `[[`) left to `re.compile`."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return _parser.parse(pattern, flags)


def _boundary_near_wide(items: Sequence[Item], flags: int) -> bool:
    for index, (op, av) in enumerate(items):
        name = str(op)
        if _boundary(op, av) and not flags & _c.SRE_FLAG_ASCII:
            before = items[index - 1] if index else None
            after = items[index + 1] if index + 1 < len(items) else None
            if (before is not None and _movable(before) and _wide(before, "end", flags)) or (
                after is not None and _movable(after) and _wide(after, "start", flags)
            ):
                return True
        elif name == "SUBPATTERN":
            _, added, removed, inner = av
            if _boundary_near_wide(list(inner), (flags | added) & ~removed):
                return True
        elif name == "BRANCH":
            if any(_boundary_near_wide(list(a), flags) for a in av[1]):
                return True
        elif name in _REPEATS and _boundary_near_wide(list(av[2]), flags):
            return True
    return False


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


@functools.cache
def _cased() -> tuple[str, Ranges]:
    """Every character case can relate to another in `re` (a lower, upper, title or folded
    form differs, those forms themselves, and `re`'s own extra cases such as the dotless i and
    U+0345), as a string and as ranges: elsewhere ignoring case changes nothing."""
    extra: Any = __import__("re._casefix", fromlist=["_EXTRA_CASES"])
    found: set[int] = set()
    for code in range(_TOP + 1):
        if _SURROGATES[0] <= code <= _SURROGATES[1]:
            continue
        char = chr(code)
        forms = (char.lower(), char.upper(), char.title(), char.casefold())
        if any(form != char for form in forms):
            found.add(code)
            found.update(ord(form) for form in forms if len(form) == 1)
    for code, others in extra._EXTRA_CASES.items():
        found.add(code)
        found.update(others)
    codes = sorted(found)
    return "".join(map(chr, codes)), _merged((code, code) for code in codes)


@functools.lru_cache(maxsize=4_096)
def _matched_cased(source: str, flags: int) -> Ranges:
    """The characters of `_cased` that the one-character `re` pattern `source` matches."""
    text, _ = _cased()
    return _merged(
        (ord(m.group()), ord(m.group())) for m in re.compile(source, flags).finditer(text)
    )


_CATEGORY_SOURCE = {
    "CATEGORY_DIGIT": r"\d",
    "CATEGORY_NOT_DIGIT": r"\D",
    "CATEGORY_SPACE": r"\s",
    "CATEGORY_NOT_SPACE": r"\S",
    "CATEGORY_WORD": r"\w",
    "CATEGORY_NOT_WORD": r"\W",
}


def _source(item: Item) -> str:
    """A one-character `re` pattern that parses to `item` (a LITERAL, NOT_LITERAL or IN)."""
    op, av = item
    name = str(op)
    if name == "LITERAL":
        return _escaped(av)
    if name == "NOT_LITERAL":
        return f"[^{_escaped(av)}]"
    parts = []
    for kind, value in av:
        kind = str(kind)
        if kind == "NEGATE":
            parts.append("^")
        elif kind == "LITERAL":
            parts.append(_escaped(value))
        elif kind == "RANGE":
            parts.append(f"{_escaped(value[0])}-{_escaped(value[1])}")
        elif str(value) in _CATEGORY_SOURCE:
            parts.append(_CATEGORY_SOURCE[str(value)])
        else:
            raise Unsupported(f"uses {kind} {value} in a class, which RE2 does not have")
    return f"[{''.join(parts)}]"


def _escaped(code: int) -> str:
    return f"\\U{code:08x}"


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
    "ASSERT_NOT": "say what must come there instead, or write a second rule for what must not",
    "GROUPREF": "spell out each repeated text instead, e.g. (?:aa|bb) for ([ab])\\1",
    "GROUPREF_EXISTS": "write the alternatives with | instead",
    "ATOMIC_GROUP": "write (?:...) instead: RE2 never backtracks, so it is not needed",
    "POSSESSIVE_REPEAT": "drop the last +: RE2 never backtracks, so it is not needed",
}
_NOT_AFTER = (
    'for "not after a word" (no pain), mark the rule negatable: true and list the word in '
    "negation_cues; otherwise write a second rule for what must not come before"
)
_GUIDE = 'see "Patterns run on RE2" in docs/guides/contributing-a-pack.md'
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
            instead = _INSTEAD[name]
            if name == "ASSERT_NOT" and av[0] < 0:  # a negative lookbehind
                instead = _NOT_AFTER
            raise Unsupported(
                f"uses {_REFUSED[name]}, which RE2 (the engine a community pack's patterns "
                f"run on) does not have: {instead} ({_GUIDE})"
            )
        if name in _SINGLE:
            return self._single(item, flags, lead, trail)
        if name == "AT":
            return _at(av, flags)
        if name == "SUBPATTERN":
            _, added, removed, inner = av
            inside = (flags | added) & ~removed
            return f"(?:{self.sequence(list(inner), inside, lead, trail)})"
        if name == "BRANCH":
            return "(?:" + "|".join(self.sequence(list(a), flags, lead, trail) for a in av[1]) + ")"
        if name in _REPEATS:
            return self._repeat(av, flags, lead, trail, lazy=name == "MIN_REPEAT")
        raise Unsupported(f"uses {name}, which RE2 does not have")

    def _single(self, item: Item, flags: int, lead: str, trail: str) -> str:
        negated, chars = _exact(item, flags)
        if not lead and not trail:
            if not negated and len(chars) == 1 and chars[0][0] == chars[0][1]:
                return _char(chars[0][0])
            return _class(chars, negated)
        wide = _non_ascii_word()
        if negated:  # each part's complement: what RE2 negates
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


def _exact(item: Item, flags: int) -> tuple[bool, Ranges]:
    """(negated, characters) of a single-character item as `re` matches it, case and all: it
    matches every character in the ranges, or with `negated`, every one outside them. With
    case ignored, the characters case relates (`_cased`) are the ones `re` matches, asked of
    `re` itself; RE2 then folds no case (module docstring)."""
    if str(item[0]) == "ANY" or not flags & _c.SRE_FLAG_IGNORECASE:
        return _chars(item, flags)
    return _folded(_source(item), flags & (re.IGNORECASE | re.ASCII))


@functools.lru_cache(maxsize=4_096)
def _folded(source: str, flags: int) -> tuple[bool, Ranges]:
    """`_exact` for the one-character `re` pattern `source`, with case ignored."""
    negated, chars = _chars(next(iter(_parser.parse(source, flags))), flags)
    _, cased = _cased()
    kept = _intersection(chars, _uncased())  # case changes nothing for these
    matched = _matched_cased(source, flags)
    if negated:  # the characters not matched: as written, and the cased ones `re` refuses
        return True, _merged(kept + _intersection(cased, _complement(matched)))
    return False, _merged(kept + matched)


@functools.cache
def _uncased() -> Ranges:
    return _complement(_cased()[1])


def _chars(item: Item, flags: int) -> tuple[bool, Ranges]:
    """(negated, characters) of a single-character item, case aside: it matches every
    character in the ranges, or with `negated`, every one outside them."""
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
        negated, chars = _exact(item, flags)
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
