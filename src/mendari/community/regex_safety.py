"""Patterns that may backtrack for seconds (ReDoS), found before a community pack is merged.

Python's `re` has no time limit (`core/patterns.py`), so a pattern like `(a+)+$` can stall the
worker that runs it. Two checks, neither a guarantee:

- **Static** (`static_problems`), on the parse tree `re` itself runs: an unbounded quantifier
  inside another (`(a+)+`, `(\\w+\\s?)*`); alternatives under `*` or `+` that can start on
  the same character (`(a|a)*`, `(.|\\s)+`), so one text matches in many ways; and unbounded
  quantifiers one after another that can match the same text, with nothing between them that
  only one of them could match and something after them that can fail (`\\d*\\d*x`,
  `a+\\s*a+$`, `(?:\\d\\d)*\\d*x`), so the text splits between them in many ways. Possessive
  quantifiers and atomic groups never backtrack and are allowed. Back references and
  conditional groups are refused: the probe cannot time what they match.
- **Probe** (`probe`): each pattern runs in a separate process, entered at each of its pieces
  that is not a literal text (`entry_points`: a sample of what comes before, kept as literal
  text with its word boundaries, then that piece and everything that can follow it), so a
  slow part is reached directly whatever comes before it. Each entry runs on pumped inputs of
  10,000 characters built from the pattern's own pieces (`pump_atoms`: every character class,
  repeated literal and repeated group it holds, and a few defaults, `'a'`, `' '` and `'0'`),
  ending as they are or with a `'!'` that makes them fail. One that takes more than a second
  on any of them is too slow, and the process is killed. A pattern with so many pieces that
  the inputs would be more than `MAX_PROBE_INPUTS` is refused, rather than timed on some of
  them.
"""

from __future__ import annotations

import json
import math
import queue
import subprocess
import sys
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from functools import cache
from typing import Any, NamedTuple

from mendari.core.origin import MAX_COMMUNITY_TEXT_CHARS
from mendari.core.patterns import problem as compile_problem
from mendari.errors import MendariError

# `re`'s own parser (3.11+), typed loosely: the parse tree is what `re` compiles and runs.
_parser: Any = __import__("re._parser", fromlist=["parse"])
_constants: Any = __import__("re._constants", fromlist=["MAXREPEAT"])
_compiler: Any = __import__("re._compiler", fromlist=["compile"])
MAXREPEAT = _constants.MAXREPEAT

INPUT_CHARS = MAX_COMMUNITY_TEXT_CHARS  # the longest text a community pack reads
TIME_LIMIT_S = 1.0  # on any one input
STARTUP_LIMIT_S = 10.0
MAX_PROBE_INPUTS = 20_000  # per pattern
DEFAULT_ATOMS = ("a", " ", "0")
TAILS = ("", "!")
# One character standing for each category, and for "any character".
_CATEGORY_SAMPLES = {
    "CATEGORY_DIGIT": "0",
    "CATEGORY_NOT_DIGIT": "a",
    "CATEGORY_SPACE": " ",
    "CATEGORY_NOT_SPACE": "a",
    "CATEGORY_WORD": "a",
    "CATEGORY_NOT_WORD": " ",
    "CATEGORY_LINEBREAK": "\n",
    "CATEGORY_NOT_LINEBREAK": "a",
}
_CATEGORY_CLASS = {
    "CATEGORY_DIGIT": r"\d",
    "CATEGORY_NOT_DIGIT": r"\D",
    "CATEGORY_SPACE": r"\s",
    "CATEGORY_NOT_SPACE": r"\S",
    "CATEGORY_WORD": r"\w",
    "CATEGORY_NOT_WORD": r"\W",
    "CATEGORY_LINEBREAK": r"\n",
    "CATEGORY_NOT_LINEBREAK": r"[^\n]",
}
# Characters the category test tries: ASCII and Latin, which is where a pack's text lives.
_SAMPLE_CHARS = [chr(c) for c in range(0x250)]
_ZERO_WIDTH = {"AT", "ASSERT", "ASSERT_NOT"}
_REPEATS = {"MAX_REPEAT", "MIN_REPEAT"}
_NEVER_BACKTRACK = {"POSSESSIVE_REPEAT", "ATOMIC_GROUP"}


@dataclass
class _Chars:
    """The characters a piece can start with, over-approximated."""

    ranges: set[tuple[int, int]] = field(default_factory=set)
    categories: set[str] = field(default_factory=set)
    anything: bool = False

    def __bool__(self) -> bool:
        return self.anything or bool(self.ranges) or bool(self.categories)

    def update(self, other: _Chars) -> None:
        self.ranges |= other.ranges
        self.categories |= other.categories
        self.anything = self.anything or other.anything

    def overlaps(self, other: _Chars) -> bool:
        if not self or not other:
            return False
        if self.anything or other.anything:
            return True
        for lo, hi in self.ranges:
            for lo2, hi2 in other.ranges:
                if lo <= hi2 and lo2 <= hi:
                    return True
        pairs = [(a, b) for a in self.categories for b in other.categories]
        if any(_categories_meet(a, b) for a, b in pairs):
            return True
        return any(
            _category_meets_range(category, span)
            for mine, theirs in ((self, other), (other, self))
            for category in mine.categories
            for span in theirs.ranges
        )


@cache
def _category_matcher(category: str) -> Any:
    import re

    return re.compile(_CATEGORY_CLASS.get(category, r"[\s\S]"))


@cache
def _categories_meet(a: str, b: str) -> bool:
    first, second = _category_matcher(a), _category_matcher(b)
    return any(first.fullmatch(c) and second.fullmatch(c) for c in _SAMPLE_CHARS)


def _category_meets_range(category: str, span: tuple[int, int]) -> bool:
    matcher = _category_matcher(category)
    lo, hi = span
    return any(matcher.fullmatch(chr(c)) for c in range(lo, min(hi, lo + 0x250) + 1))


def _name(op: Any) -> str:
    return str(op)


def _literal(code: int, ignore_case: bool) -> _Chars:
    chars = _Chars({(code, code)})
    if ignore_case:
        for variant in {chr(code).lower(), chr(code).upper()}:
            if len(variant) == 1:
                chars.ranges.add((ord(variant), ord(variant)))
    return chars


def _first(items: Sequence[Any], follow: _Chars, ignore_case: bool) -> _Chars:
    """What the sequence `items`, then whatever starts with `follow`, can start with."""
    chars = _Chars()
    for item in items:
        start, nullable = _item_first(item, ignore_case)
        chars.update(start)
        if not nullable:
            return chars
    chars.update(follow)
    return chars


def _item_first(item: Any, ignore_case: bool) -> tuple[_Chars, bool]:
    """(what one piece can start with, whether it can match nothing)."""
    op, av = item
    name = _name(op)
    if name == "LITERAL":
        return _literal(av, ignore_case), False
    if name in ("NOT_LITERAL", "ANY"):
        return _Chars(anything=True), False
    if name == "IN":
        chars = _Chars()
        for member_op, member in av:
            member_name = _name(member_op)
            if member_name == "NEGATE":
                return _Chars(anything=True), False
            if member_name == "LITERAL":
                chars.update(_literal(member, ignore_case))
            elif member_name == "RANGE":
                chars.ranges.add(tuple(member))
                if ignore_case:
                    chars.ranges.update(_case_ranges(member))
            elif member_name == "CATEGORY":
                chars.categories.add(_name(member))
            else:
                chars.anything = True
        return chars, False
    if name == "SUBPATTERN":
        return _sequence_first(av[3], ignore_case)
    if name == "ATOMIC_GROUP":
        return _sequence_first(av, ignore_case)
    if name == "BRANCH":
        chars = _Chars()
        nullable = False
        for alternative in av[1]:
            start, empty = _sequence_first(alternative, ignore_case)
            chars.update(start)
            nullable = nullable or empty
        return chars, nullable
    if name in _REPEATS or name == "POSSESSIVE_REPEAT":
        low, _, body = av
        start, empty = _sequence_first(body, ignore_case)
        return start, low == 0 or empty
    if name in _ZERO_WIDTH:
        return _Chars(), True
    if name == "GROUPREF_EXISTS":
        _, yes, no = av
        chars, empty = _sequence_first(yes, ignore_case)
        if no is not None:
            other, other_empty = _sequence_first(no, ignore_case)
            chars.update(other)
            empty = empty or other_empty
        else:
            empty = True
        return chars, empty
    return _Chars(anything=True), True  # a back reference: anything, or nothing


def _sequence_first(items: Sequence[Any], ignore_case: bool) -> tuple[_Chars, bool]:
    chars = _Chars()
    for item in items:
        start, nullable = _item_first(item, ignore_case)
        chars.update(start)
        if not nullable:
            return chars, False
    return chars, True


def _case_ranges(span: Sequence[int]) -> set[tuple[int, int]]:
    """The other case of each ASCII letter in a range (`[a-c]` under IGNORECASE: A-C too)."""
    lo, hi = span
    found: set[tuple[int, int]] = set()
    for code in range(lo, min(hi, 0x7F) + 1):
        for variant in {chr(code).lower(), chr(code).upper()}:
            found.add((ord(variant), ord(variant)))
    return found


def static_problems(pattern: str, flags: int = 0) -> list[str]:
    """Why `pattern` (compiled with `flags`) may backtrack catastrophically, in words, or []."""
    import re

    try:
        tree = _parser.parse(pattern, flags)
    except (re.error, OverflowError, RecursionError, ValueError):
        return []  # not a pattern at all: `core/patterns.py` refuses it at load
    ignore_case = bool((flags | tree.state.flags) & re.IGNORECASE)
    found: list[str] = [_REFERENCES] if _references(list(tree)) else []
    _walk(
        list(tree),
        _Chars(),
        in_loop=False,
        ignore_case=ignore_case,
        found=found,
        flags=flags | tree.state.flags,
    )
    return sorted(set(found))


_REFERENCES = (
    "a back reference (`\\1`, `(?P=name)`) or a conditional group (`(?(1)...)`): a community "
    "pack's patterns cannot use them, since the probe cannot time what they match; match the "
    "text itself instead"
)


def _children(item: Any) -> list[Sequence[Any]]:
    """The sequences inside one piece."""
    op, av = item
    name = _name(op)
    if name == "SUBPATTERN":
        return [av[3]]
    if name == "BRANCH":
        return list(av[1])
    if name in _REPEATS or name == "POSSESSIVE_REPEAT":
        return [av[2]]
    if name == "ATOMIC_GROUP":
        return [av]
    if name in ("ASSERT", "ASSERT_NOT"):
        return [av[1]]
    if name == "GROUPREF_EXISTS":
        return [av[1]] + ([av[2]] if av[2] is not None else [])
    return []


def _references(items: Sequence[Any]) -> bool:
    """Whether a back reference or a conditional group is anywhere in `items`."""
    stack = [items]
    while stack:
        for item in stack.pop():
            if _name(item[0]) in ("GROUPREF", "GROUPREF_EXISTS"):
                return True
            stack += _children(item)
    return False


_SEQUENTIAL = (
    "repeated pieces one after another can match the same characters (`\\d*\\d*`, "
    "`\\w+\\s*\\w+`, `(?:ab)*(?:ac)*`), so a text splits between them in many ways: merge them "
    "into one, or put something only one of them matches between them"
)


def _flat(items: Sequence[Any]) -> list[Any]:
    """A sequence with its groups opened (`(a*)(a*)` is `a* a*`): what follows what."""
    flat: list[Any] = []
    for item in items:
        op, av = item
        if _name(op) == "SUBPATTERN":
            flat += _flat(av[3])
        else:
            flat.append(item)
    return flat


def _unbounded(item: Any) -> bool:
    op, av = item
    return _name(op) in _REPEATS and av[1] == MAXREPEAT


_SINGLE = {"LITERAL", "NOT_LITERAL", "ANY", "IN"}  # pieces that match one character


def _single(items: Sequence[Any]) -> Any | None:
    """The one piece of `items` when it matches one character, or None."""
    if len(items) != 1 or _name(items[0][0]) not in _SINGLE:
        return None
    return items[0]


def _own_characters(item: Any) -> str:
    """The characters a one-character piece names (its literals, the ends and middle of its
    ranges) in both cases, beyond the sample characters: a class outside them (Cyrillic letters) is
    still tried on characters of its own."""
    op, av = item
    name = _name(op)
    codes: list[int] = []
    if name in ("LITERAL", "NOT_LITERAL"):
        codes = [av]
    elif name == "IN":
        for member_op, member in av:
            member_name = _name(member_op)
            if member_name == "LITERAL":
                codes.append(member)
            elif member_name == "RANGE":
                codes += [member[0], (member[0] + member[1]) // 2, member[1]]
    found: set[str] = set()
    for c in map(chr, codes):
        found.update(v for v in (c, c.lower(), c.upper()) if len(v) == 1 and ord(v) >= 0x250)
    return "".join(sorted(found))


_MATCHED: dict[tuple[str, int, str], frozenset[str]] = {}


def _matched(item: Any, flags: int, extra: str = "") -> frozenset[str]:
    """The characters tried (the sample characters and `extra`) that the one-character piece
    `item` matches, as `re` runs it (a negated class included)."""
    key = (repr(item), flags, extra)
    found = _MATCHED.get(key)
    if found is None:
        state = _parser.State()
        state.flags = flags
        compiled = _compiler.compile(_parser.SubPattern(state, [item]), flags)
        found = frozenset(c for c in [*_SAMPLE_CHARS, *extra] if compiled.fullmatch(c))
        if len(_MATCHED) < 10_000:
            _MATCHED[key] = found
    return found


def _meet(first: Any, second: Any, flags: int) -> bool:
    """Whether two one-character pieces match a character in common, tried on the sample
    characters and on each one's own."""
    extra = _own_characters(first) + _own_characters(second)
    return bool(_matched(first, flags, extra) & _matched(second, flags, extra))


_MAX_PERIOD = 1_000


def _repeats_meet(
    first: Sequence[Any], second: Sequence[Any], flags: int, ignore_case: bool
) -> bool:
    """Whether the bodies of two repetitions can match a text in common. Bodies of one-character
    pieces (`\\d`, `[a-z]\\.`) are compared piece by piece over a common period, exactly
    (`(?:\\d\\d)*` meets `\\d*`; `(?:[a-z]\\.)+` does not meet `[a-z]+`); any other body by
    the characters it can start with, which over-approximates."""
    a, b = _flat(first), _flat(second)
    if a and b and all(_single([piece]) for piece in [*a, *b]):
        period = math.lcm(len(a), len(b))
        if period <= _MAX_PERIOD:
            return all(_meet(a[i % len(a)], b[i % len(b)], flags) for i in range(period))
    return _sequence_first(first, ignore_case)[0].overlaps(_sequence_first(second, ignore_case)[0])


def _can_fail(items: Sequence[Any], follow: _Chars, ignore_case: bool) -> bool:
    """Whether anything in `items`, or after them, can fail to match."""
    for item in items:
        _, nullable = _item_first(item, ignore_case)
        if not nullable or _name(item[0]) in _ZERO_WIDTH:
            return True
    return bool(follow)


def _sequential(items: Sequence[Any], follow: _Chars, ignore_case: bool, flags: int) -> bool:
    """Two unbounded repetitions in one sequence that can match the same text (`\\d*\\d*`,
    `(?:\\d\\d)*(?:\\d\\d)*`, `_repeats_meet`), with only what the first can also match (or
    nothing) between them, and something after that can fail: a failing text is split between
    them in every way, polynomially many. Anything between that the first cannot match fixes
    the split."""
    flat = _flat(items)
    for index, item in enumerate(flat):
        if not _unbounded(item):
            continue
        body = item[1][2]
        first = _single(_flat(body))
        if first is not None and not _matched(first, flags, _own_characters(first)):
            continue  # matches none of the characters tried
        for later_index in range(index + 1, len(flat)):
            later = flat[later_index]
            if (
                _unbounded(later)
                and _repeats_meet(body, later[1][2], flags, ignore_case)
                and _can_fail(flat[later_index + 1 :], follow, ignore_case)
            ):
                return True
            _, nullable = _item_first(later, ignore_case)
            if nullable:
                continue
            if first is None or _single([later]) is None or not _meet(first, later, flags):
                break  # something the first cannot match (or not one character): fixed
    return False


def _walk(
    items: Sequence[Any],
    follow: _Chars,
    *,
    in_loop: bool,
    ignore_case: bool,
    found: list[str],
    flags: int = 0,
) -> None:
    if _sequential(items, follow, ignore_case, flags):
        found.append(_SEQUENTIAL)
    for index, (op, av) in enumerate(items):
        name = _name(op)
        rest = _first(items[index + 1 :], follow, ignore_case)
        if name in _NEVER_BACKTRACK:
            continue
        if name == "SUBPATTERN":
            _walk(av[3], rest, in_loop=in_loop, ignore_case=ignore_case, found=found, flags=flags)
        elif name == "BRANCH":
            alternatives = av[1]
            if in_loop:
                starts = [_first(alternative, rest, ignore_case) for alternative in alternatives]
                if any(
                    starts[i].overlaps(starts[j])
                    for i in range(len(starts))
                    for j in range(i + 1, len(starts))
                ):
                    found.append(
                        "alternatives inside a repeated group (`*` or `+`) can start with the "
                        "same character, so one text can match in many ways: make them start "
                        "differently, or drop the repetition"
                    )
            for alternative in alternatives:
                _walk(
                    alternative,
                    rest,
                    in_loop=in_loop,
                    ignore_case=ignore_case,
                    found=found,
                    flags=flags,
                )
        elif name in _REPEATS:
            _, high, body = av
            unbounded = high == MAXREPEAT
            if unbounded and in_loop:
                found.append(
                    "an unbounded quantifier (`*`, `+` or `{n,}`) inside another one, such as "
                    "`(a+)+`: write the repetition once, e.g. `a+` or `(a b?)+` with a bounded part"
                )
            again = _first(body, rest, ignore_case)
            again.update(rest)
            _walk(
                body,
                again,
                in_loop=in_loop or unbounded,
                ignore_case=ignore_case,
                found=found,
                flags=flags,
            )
        elif name in ("ASSERT", "ASSERT_NOT"):
            _walk(
                av[1], _Chars(), in_loop=in_loop, ignore_case=ignore_case, found=found, flags=flags
            )
        elif name == "GROUPREF_EXISTS":
            _, yes, no = av
            for branch in (yes, no):
                if branch is not None:
                    _walk(
                        branch,
                        rest,
                        in_loop=in_loop,
                        ignore_case=ignore_case,
                        found=found,
                        flags=flags,
                    )


# -- the probe -----------------------------------------------------------------------------------


_SAMPLE_PREFERRED = "a0 "
_SAMPLE_BUDGET = 100  # characters: the longest sample of a repeated group pumped as one piece


def _sample_one(item: Any, flags: int) -> str:
    """One character the one-character piece `item` matches (a letter, digit or space when it
    can), or "" when none of the characters tried does."""
    matched = _matched(item, flags, _own_characters(item))
    preferred = [c for c in _SAMPLE_PREFERRED if c in matched]
    return preferred[0] if preferred else min(matched, default="")


def _sample(items: Iterable[Any], flags: int, budget: int = _SAMPLE_BUDGET) -> str:
    """A short text the sequence `items` matches, as far as one can be built without running
    it (the fewest repetitions each, the first alternative, nothing for an assertion), at most
    `budget` characters: one repetition of a group the probe pumps (`0a` for `(?:0[a-z])*`)."""
    text = ""
    for item in items:
        if len(text) >= budget:
            break
        op, av = item
        name = _name(op)
        if name in _SINGLE:
            text += _sample_one(item, flags)
        elif name == "SUBPATTERN":
            text += _sample(av[3], flags, budget - len(text))
        elif name == "ATOMIC_GROUP":
            text += _sample(av, flags, budget - len(text))
        elif name == "BRANCH":
            text += _sample(av[1][0], flags, budget - len(text))
        elif name in _REPEATS or name == "POSSESSIVE_REPEAT":
            part = _sample(av[2], flags, budget - len(text))
            if part:
                text += part * min(av[0], (budget - len(text)) // len(part) + 1)
    return text[:budget]


def pump_atoms(pattern: str, flags: int = 0) -> list[str]:
    """The pieces an input is pumped with: a character of every class the pattern uses, every
    literal text and one repetition of every group inside a repetition, and `DEFAULT_ATOMS`.
    None is ever left out, so padding a pattern with pieces cannot push the slow one out of the
    probe."""
    import re

    atoms: list[str] = []
    try:
        tree = _parser.parse(pattern, flags)
    except (re.error, OverflowError, RecursionError, ValueError):
        return list(DEFAULT_ATOMS)
    flags |= tree.state.flags

    def add(text: str) -> None:
        if text and text not in atoms:
            atoms.append(text)

    def visit(items: Sequence[Any], repeated: bool) -> None:
        run = ""
        for op, av in items:
            name = _name(op)
            if name == "LITERAL":
                run += chr(av)
                continue
            if repeated:
                add(run)
            run = ""
            if name == "IN":
                for member_op, member in av:
                    member_name = _name(member_op)
                    if member_name == "LITERAL":
                        add(chr(member))
                    elif member_name == "RANGE":
                        add(chr(member[0]))
                    elif member_name == "CATEGORY":
                        add(_CATEGORY_SAMPLES.get(_name(member), "a"))
            elif name in ("ANY", "NOT_LITERAL"):
                add("a")
            elif (name in _REPEATS or name == "POSSESSIVE_REPEAT") and av[1] > 1:
                add(_sample(av[2], flags))  # a body mixing pieces, `0[a-z]`, as a whole
            for child in _children((op, av)):
                visit(child, repeated or (name in _REPEATS | {"POSSESSIVE_REPEAT"} and av[1] > 1))
        if repeated:
            add(run)

    visit(list(tree), False)
    for default in DEFAULT_ATOMS:
        add(default)
    return atoms


# What a group's flags can be written as, scoped: (?i:...), (?-i:...).
_SCOPED_FLAGS = (
    ("a", "SRE_FLAG_ASCII"),
    ("i", "SRE_FLAG_IGNORECASE"),
    ("m", "SRE_FLAG_MULTILINE"),
    ("s", "SRE_FLAG_DOTALL"),
    ("u", "SRE_FLAG_UNICODE"),
)
_AT = {
    "AT_BEGINNING": "^",
    "AT_BEGINNING_STRING": r"\A",
    "AT_BOUNDARY": r"\b",
    "AT_NON_BOUNDARY": r"\B",
    "AT_END": "$",
    "AT_END_STRING": r"\Z",
}
_VERBOSE = _constants.SRE_FLAG_VERBOSE


def _char(code: int) -> str:
    """One character, written so that `re` reads it as itself, in a class or out of one."""
    c = chr(code)
    if c.isascii() and c.isalnum():
        return c
    if 0x20 <= code < 0x7F:
        return "\\" + c
    if code < 0x100:
        return f"\\x{code:02x}"
    return f"\\u{code:04x}" if code < 0x10000 else f"\\U{code:08x}"


def _flag_letters(mask: int) -> str:
    return "".join(letter for letter, bit in _SCOPED_FLAGS if mask & getattr(_constants, bit))


def _unparse(items: Iterable[Any]) -> str:
    """A pattern `re` parses to `items` again (groups made non-capturing), for a process that
    compiles it without the `VERBOSE` flag. Back references and conditional groups have no
    place here: a community pack's patterns cannot use them (`static_problems`)."""
    out: list[str] = []
    for op, av in items:
        name = _name(op)
        if name == "LITERAL":
            out.append(_char(av))
        elif name == "NOT_LITERAL":
            out.append(f"[^{_char(av)}]")
        elif name == "ANY":
            out.append(".")
        elif name == "IN":
            out.append(_unparse_class(av))
        elif name == "AT":
            out.append(_AT[_name(av)])
        elif name == "BRANCH":
            out.append("(?:" + "|".join(_unparse(alternative) for alternative in av[1]) + ")")
        elif name == "SUBPATTERN":
            _, added, removed, body = av
            scope = _flag_letters(added) + ("-" + _flag_letters(removed) if removed else "")
            out.append(f"(?{scope.rstrip('-')}:{_unparse(body)})")
        elif name in _REPEATS or name == "POSSESSIVE_REPEAT":
            low, high, body = av
            count = f"{{{low},}}" if high == MAXREPEAT else f"{{{low},{high}}}"
            mode = {"MIN_REPEAT": "?", "POSSESSIVE_REPEAT": "+"}.get(name, "")
            out.append(f"(?:{_unparse(body)}){count}{mode}")
        elif name == "ATOMIC_GROUP":
            out.append(f"(?>{_unparse(av)})")
        elif name in ("ASSERT", "ASSERT_NOT"):
            direction, body = av
            kind = ("<" if direction < 0 else "") + ("=" if name == "ASSERT" else "!")
            out.append(f"(?{kind}{_unparse(body)})")
        else:
            raise ValueError(f"cannot write {name} back as a pattern")
    return "".join(out)


_CLASS_CATEGORY = {
    "CATEGORY_DIGIT": r"\d",
    "CATEGORY_NOT_DIGIT": r"\D",
    "CATEGORY_SPACE": r"\s",
    "CATEGORY_NOT_SPACE": r"\S",
    "CATEGORY_WORD": r"\w",
    "CATEGORY_NOT_WORD": r"\W",
}


def _unparse_class(members: Iterable[Any]) -> str:
    out = ""
    for op, av in members:
        name = _name(op)
        if name == "NEGATE":
            out += "^"
        elif name == "LITERAL":
            out += _char(av)
        elif name == "RANGE":
            out += f"{_char(av[0])}-{_char(av[1])}"
        elif name == "CATEGORY":
            out += _CLASS_CATEGORY[_name(av)]
        else:
            raise ValueError(f"cannot write {name} back in a class")
    return f"[{out}]"


Close = Callable[[list[Any]], list[Any]]


class Entry(NamedTuple):
    """One way into a pattern: `lead` (a sample of what comes before), then the piece the
    pattern is entered at and everything that can follow it."""

    pattern: str
    flags: int
    lead: str


def entry_points(pattern: str, flags: int = 0) -> list[Entry]:
    """The pattern, and the pattern entered at each piece that is not a literal text: a
    sample of what comes before it, as literal text, then that piece and everything that can
    follow it (the rest of its group, further repetitions, what comes after), in the flags that
    hold there. The probe times each on its lead and pumped text, so a slow part is reached
    directly, whatever comes before it (a class, a lookaround, another alternative), and only
    where it could start. A pattern with a back reference or a conditional group is entered
    only at its start (`static_problems` refuses it)."""
    import re

    entries = [Entry(pattern, flags, "")]
    try:
        tree = _parser.parse(pattern, flags)
    except (re.error, OverflowError, RecursionError, ValueError):
        return entries
    if _references(list(tree)):
        return entries
    state = tree.state
    sample_flags = flags | state.flags
    entry_flags = sample_flags & ~_VERBOSE

    def group(op: Any, added: int, removed: int, items: list[Any]) -> Any:
        return (op, (None, added, removed, _parser.SubPattern(state, items)))

    def add(lead: Lead, items: list[Any]) -> None:
        entry = Entry(_unparse([*lead.items, *items]), entry_flags, lead.text)
        if entry not in entries:
            entries.append(entry)

    def visit(items: Sequence[Any], close: Close, scope: Close, lead: Lead) -> None:
        items = list(items)
        for index, (op, av) in enumerate(items):
            name = _name(op)
            rest = items[index + 1 :]
            if name != "LITERAL":
                add(lead, close(items[index:]))

            def then(seq: list[Any], rest: list[Any] = rest) -> list[Any]:
                return close([*seq, *rest])

            if name == "SUBPATTERN":
                _, added, removed, body = av
                if added or removed:

                    def inside(
                        seq: list[Any],
                        op: Any = op,
                        a: int = added,
                        r: int = removed,
                        rest: list[Any] = rest,
                    ) -> list[Any]:
                        return close([group(op, a, r, seq), *rest])

                    def scoped(
                        seq: list[Any], op: Any = op, a: int = added, r: int = removed
                    ) -> list[Any]:
                        return scope([group(op, a, r, seq)])

                    visit(body, inside, scoped, lead)
                else:
                    visit(body, then, scope, lead)
            elif name == "BRANCH":
                for alternative in av[1]:
                    visit(alternative, then, scope, lead)
            elif name in _REPEATS or name == "POSSESSIVE_REPEAT":
                low, high, body = av
                more = high if high == MAXREPEAT else high - 1
                again = [(op, (max(low - 1, 0), more, body))] if more else []

                def repeated(
                    seq: list[Any], again: list[Any] = again, rest: list[Any] = rest
                ) -> list[Any]:
                    return close([*seq, *again, *rest])

                visit(body, repeated, scope, lead)
            elif name == "ATOMIC_GROUP":

                def atomic(seq: list[Any], op: Any = op, rest: list[Any] = rest) -> list[Any]:
                    return close([(op, _parser.SubPattern(state, seq)), *rest])

                visit(av, atomic, scope, lead)
            elif name in ("ASSERT", "ASSERT_NOT") and av[0] > 0:
                visit(av[1], scope, scope, lead)  # a lookahead's own body, then nothing
            # (a lookbehind has a fixed width: nothing in it repeats)
            lead = lead.then((op, av), sample_flags)

    visit(list(tree), list, list, Lead("", ()))
    return entries


class Lead(NamedTuple):
    """What an entry point is led up to by: `text`, a sample of the pieces before it, and
    `items`, the same as a pattern: the sample as literal text, with the word boundaries,
    anchors and negative lookarounds kept, since they fix where a slow part can start (a
    positive lookaround is dropped: the sample may not satisfy it, and it only narrows)."""

    text: str
    items: tuple[Any, ...]

    def then(self, item: Any, flags: int) -> Lead:
        name = _name(item[0])
        if name in ("AT", "ASSERT_NOT"):
            return Lead(self.text, (*self.items, item))
        if name == "ASSERT" or len(self.text) >= INPUT_CHARS:
            return self
        sample = _sample([item], flags, INPUT_CHARS - len(self.text))
        literal = tuple((_constants.LITERAL, ord(c)) for c in sample)
        return Lead(self.text + sample, (*self.items, *literal))


def probe_inputs(pattern: str, flags: int = 0) -> int:
    """How many inputs the probe runs `pattern` on."""
    return len(entry_points(pattern, flags)) * len(pump_atoms(pattern, flags)) * len(TAILS)


# Runs in a separate process: each pattern, entered at each of its pieces, on its lead and pumped
# inputs, saying `tick` after each input and `done` after each pattern.
_PROBE = r"""
import json, re, sys
size, tails, jobs = json.loads(sys.stdin.read())
print("ready", flush=True)
for entries, atoms in jobs:
    for pattern, flags, lead in entries:
        compiled = re.compile(pattern, flags)
        for atom in atoms:
            body = (atom * (size // len(atom) + 1))[: max(size - len(lead), 0)]
            for tail in tails:
                compiled.search(lead + body[: len(body) - len(tail)] + tail)
                print("tick", flush=True)
    print("done", flush=True)
"""


@dataclass(frozen=True)
class Job:
    where: str
    pattern: str
    flags: int


def probe(jobs: Sequence[Job], *, limit_s: float = TIME_LIMIT_S) -> list[Job]:
    """The patterns that take more than `limit_s` on any one of their pumped inputs, each run
    in a separate process that is killed when it does. Patterns that do not compile are skipped
    (the pack's load refuses them), and so are those with more than `MAX_PROBE_INPUTS` inputs
    (`probe_inputs`), which the caller refuses."""
    pending = [
        job
        for job in jobs
        if compile_problem(job.pattern, job.flags) is None
        and probe_inputs(job.pattern, job.flags) <= MAX_PROBE_INPUTS
    ]
    slow: list[Job] = []
    while pending:
        finished = _run(pending, limit_s)
        if finished == len(pending):
            break
        slow.append(pending[finished])
        pending = pending[finished + 1 :]
    return slow


def _run(jobs: Sequence[Job], limit_s: float) -> int:
    """How many of `jobs` one process finished, in order, each within `limit_s`."""
    payload = [
        INPUT_CHARS,
        list(TAILS),
        [
            [entry_points(job.pattern, job.flags), pump_atoms(job.pattern, job.flags)]
            for job in jobs
        ],
    ]
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", _PROBE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    lines: queue.Queue[str] = queue.Queue()

    def read() -> None:
        assert process.stdout is not None
        for raw in process.stdout:
            lines.put(raw.decode().strip())
        lines.put("")  # the process ended

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    assert process.stdin is not None
    process.stdin.write(json.dumps(payload).encode())
    process.stdin.close()
    finished = 0
    try:
        try:
            started = lines.get(timeout=STARTUP_LIMIT_S) == "ready"
        except queue.Empty:
            started = False
        if not started:
            raise MendariError("the pattern probe could not start a Python process")
        while finished < len(jobs):
            line = lines.get(timeout=limit_s)
            if line == "tick":
                continue
            if line != "done":
                break  # the process died: count the pattern it was on as too slow
            finished += 1
    except queue.Empty:
        pass
    finally:
        process.kill()
        process.wait()
        reader.join(timeout=STARTUP_LIMIT_S)
        assert process.stdout is not None
        process.stdout.close()
    return finished
