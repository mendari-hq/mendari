"""Patterns that may backtrack for seconds (ReDoS), found before a community pack is merged.

Python's `re` has no time limit (`core/patterns.py`), so a pattern like `(a+)+$` can stall the
worker that runs it. Two checks, neither a guarantee:

- **Static** (`static_problems`), on the parse tree `re` itself runs: an unbounded quantifier
  inside another (`(a+)+`, `(\\w+\\s?)*`), and alternatives under `*` or `+` that can start on
  the same character (`(a|a)*`, `(.|\\s)+`), so one text matches in many ways. Possessive
  quantifiers and atomic groups never backtrack and are allowed.
- **Probe** (`probe`): each pattern runs in a separate process on pumped inputs of 10,000
  characters built from its own pieces (`'a' * 10_000 + '!'`); one that takes more than a
  second on them is too slow, and the process is killed.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import cache
from typing import Any

from mendari.core.patterns import problem as compile_problem
from mendari.errors import MendariError

# `re`'s own parser (3.11+), typed loosely: the parse tree is what `re` compiles and runs.
_parser: Any = __import__("re._parser", fromlist=["parse"])
_constants: Any = __import__("re._constants", fromlist=["MAXREPEAT"])
MAXREPEAT = _constants.MAXREPEAT

INPUT_CHARS = 10_000
TIME_LIMIT_S = 1.0
STARTUP_LIMIT_S = 10.0
MAX_ATOMS = 8
MAX_PREFIXES = 4
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
    found: list[str] = []
    _walk(list(tree), _Chars(), in_loop=False, ignore_case=ignore_case, found=found)
    return sorted(set(found))


def _walk(
    items: Sequence[Any], follow: _Chars, *, in_loop: bool, ignore_case: bool, found: list[str]
) -> None:
    for index, (op, av) in enumerate(items):
        name = _name(op)
        rest = _first(items[index + 1 :], follow, ignore_case)
        if name in _NEVER_BACKTRACK:
            continue
        if name == "SUBPATTERN":
            _walk(av[3], rest, in_loop=in_loop, ignore_case=ignore_case, found=found)
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
                _walk(alternative, rest, in_loop=in_loop, ignore_case=ignore_case, found=found)
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
            _walk(body, again, in_loop=in_loop or unbounded, ignore_case=ignore_case, found=found)
        elif name in ("ASSERT", "ASSERT_NOT"):
            _walk(av[1], _Chars(), in_loop=in_loop, ignore_case=ignore_case, found=found)
        elif name == "GROUPREF_EXISTS":
            _, yes, no = av
            for branch in (yes, no):
                if branch is not None:
                    _walk(branch, rest, in_loop=in_loop, ignore_case=ignore_case, found=found)


# -- the probe -----------------------------------------------------------------------------------


def pump_atoms(pattern: str, flags: int = 0) -> tuple[list[str], list[str]]:
    """(atoms, prefixes): the pieces of `pattern` an input is pumped with, and the literal
    texts it may need before them."""
    import re

    atoms: list[str] = []
    prefixes: list[str] = []
    try:
        tree = _parser.parse(pattern, flags)
    except (re.error, OverflowError, RecursionError, ValueError):
        return ["a"], []

    def add(target: list[str], text: str) -> None:
        if text and text not in target:
            target.append(text)

    def visit(items: Iterable[Any]) -> None:
        run = ""
        for op, av in items:
            name = _name(op)
            if name == "LITERAL":
                run += chr(av)
                continue
            add(prefixes, run)
            add(atoms, run)
            run = ""
            if name == "IN":
                for member_op, member in av:
                    member_name = _name(member_op)
                    if member_name == "LITERAL":
                        add(atoms, chr(member))
                    elif member_name == "RANGE":
                        add(atoms, chr(member[0]))
                    elif member_name == "CATEGORY":
                        add(atoms, _CATEGORY_SAMPLES.get(_name(member), "a"))
            elif name in ("ANY", "NOT_LITERAL"):
                add(atoms, "a")
            elif name == "SUBPATTERN":
                visit(av[3])
            elif name == "ATOMIC_GROUP":
                visit(av)
            elif name == "BRANCH":
                for alternative in av[1]:
                    visit(alternative)
            elif name in _REPEATS or name == "POSSESSIVE_REPEAT":
                visit(av[2])
            elif name in ("ASSERT", "ASSERT_NOT"):
                visit(av[1])
        add(prefixes, run)
        add(atoms, run)

    visit(list(tree))
    for default in ("a", " ", "0"):
        add(atoms, default)
    return atoms[:MAX_ATOMS], prefixes[:MAX_PREFIXES]


# Runs in a separate process: each pattern on its pumped inputs, saying `done` after each.
_PROBE = r"""
import json, re, sys
size, tails, jobs = json.loads(sys.stdin.read())
print("ready", flush=True)
for pattern, flags, atoms, prefixes in jobs:
    compiled = re.compile(pattern, flags)
    for prefix in [""] + prefixes:
        for atom in atoms:
            body = (atom * (size // len(atom) + 1))[: size - len(prefix)]
            for tail in tails:
                compiled.search(prefix + body[: len(body) - len(tail)] + tail)
    print("done", flush=True)
"""


@dataclass(frozen=True)
class Job:
    where: str
    pattern: str
    flags: int


def probe(jobs: Sequence[Job], *, limit_s: float = TIME_LIMIT_S) -> list[Job]:
    """The patterns that take more than `limit_s` on their pumped inputs, each run in a separate
    process that is killed when it does. Patterns that do not compile are skipped (the pack's
    load refuses them)."""
    pending = [job for job in jobs if compile_problem(job.pattern, job.flags) is None]
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
        [[job.pattern, int(job.flags), *pump_atoms(job.pattern, job.flags)] for job in jobs],
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
            if lines.get(timeout=limit_s) != "done":
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
