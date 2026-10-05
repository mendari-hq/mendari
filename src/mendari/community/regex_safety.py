"""Patterns that may backtrack for seconds (ReDoS), found before a community pack is merged.

Python's `re` has no time limit (`core/patterns.py`), so a pattern like `(a+)+$` can stall the
worker that runs it. Two checks, neither a guarantee:

- **Static** (`static_problems`), on the parse tree `re` itself runs: an unbounded quantifier
  inside another (`(a+)+`, `(\\w+\\s?)*`); alternatives under `*` or `+` that can start on
  the same character (`(a|a)*`, `(.|\\s)+`), so one text matches in many ways; and unbounded
  quantifiers one after another that can match the same characters, with nothing between them
  that only one of them could match and something after them that can fail (`\\d*\\d*x`,
  `a+\\s*a+$`), so the text splits between them in many ways. Possessive quantifiers and atomic
  groups never backtrack and are allowed.
- **Probe** (`probe`): each pattern runs in a separate process on pumped inputs of 10,000
  characters built from its own pieces: every character class and repeated literal it holds,
  and a few defaults (`'a'`, `' '`, `'0'`), each after every literal text that may lead up to
  them (`'ab' + '1' * 9_998 + '!'`). One that takes more than a second on any of them is too
  slow, and the process is killed. A pattern with so many pieces that the inputs would be more
  than `MAX_PROBE_INPUTS` is refused, rather than timed on some of them.
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
    found: list[str] = []
    _walk(
        list(tree),
        _Chars(),
        in_loop=False,
        ignore_case=ignore_case,
        found=found,
        flags=flags | tree.state.flags,
    )
    return sorted(set(found))


_SEQUENTIAL = (
    "repeated characters one after another can match the same characters (`\\d*\\d*`, "
    "`\\w+\\s*\\w+`), so a text splits between them in many ways: merge them into one, or "
    "put something only one of them matches between them"
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


def _one_character(items: Sequence[Any], flags: int) -> frozenset[str] | None:
    """The sample characters a one-character piece matches, as `re` runs it (a negated class
    included), or None for anything else."""
    if len(items) != 1 or _name(items[0][0]) not in _SINGLE:
        return None
    return _matched(repr(items[0]), items[0], flags)


_MATCHED: dict[tuple[str, int], frozenset[str]] = {}


def _matched(key: str, item: Any, flags: int) -> frozenset[str]:
    found = _MATCHED.get((key, flags))
    if found is None:
        state = _parser.State()
        state.flags = flags
        compiled = _compiler.compile(_parser.SubPattern(state, [item]), flags)
        found = frozenset(c for c in _SAMPLE_CHARS if compiled.fullmatch(c))
        if len(_MATCHED) < 10_000:
            _MATCHED[(key, flags)] = found
    return found


def _can_fail(items: Sequence[Any], follow: _Chars, ignore_case: bool) -> bool:
    """Whether anything in `items`, or after them, can fail to match."""
    for item in items:
        _, nullable = _item_first(item, ignore_case)
        if not nullable or _name(item[0]) in _ZERO_WIDTH:
            return True
    return bool(follow)


def _sequential(items: Sequence[Any], follow: _Chars, ignore_case: bool, flags: int) -> bool:
    """Two unbounded repetitions of one character in one sequence that can match the same
    characters (`\\d*\\d*`), with only what the first can also match (or nothing) between them,
    and something after that can fail: a failing text is split between them in every way,
    polynomially many. Anything between that the first cannot match fixes the split."""
    flat = _flat(items)
    for index, item in enumerate(flat):
        first = _one_character(item[1][2], flags) if _unbounded(item) else None
        if not first:
            continue
        for later_index in range(index + 1, len(flat)):
            later = flat[later_index]
            if _unbounded(later):
                again = _one_character(later[1][2], flags)
                if (
                    again is not None
                    and first & again
                    and _can_fail(flat[later_index + 1 :], follow, ignore_case)
                ):
                    return True
            _, nullable = _item_first(later, ignore_case)
            if nullable:
                continue
            single = _one_character([later], flags)
            if single is None or not first & single:
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


def pump_atoms(pattern: str, flags: int = 0) -> tuple[list[str], list[str]]:
    """(atoms, prefixes): the pieces an input is pumped with (a character of every class the
    pattern uses, every literal text inside a repetition, and `DEFAULT_ATOMS`), and every
    literal text that may lead up to them. None is ever left out, so padding a pattern with
    pieces cannot push the slow one out of the probe."""
    import re

    atoms: list[str] = []
    prefixes: list[str] = []
    try:
        tree = _parser.parse(pattern, flags)
    except (re.error, OverflowError, RecursionError, ValueError):
        return list(DEFAULT_ATOMS), []

    def add(target: list[str], text: str) -> None:
        if text and text not in target:
            target.append(text)

    def visit(items: Iterable[Any], repeated: bool) -> None:
        run = ""

        def flush() -> None:
            add(prefixes, run)
            if repeated:
                add(atoms, run)

        for op, av in items:
            name = _name(op)
            if name == "LITERAL":
                run += chr(av)
                continue
            flush()
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
                visit(av[3], repeated)
            elif name == "ATOMIC_GROUP":
                visit(av, repeated)
            elif name == "BRANCH":
                for alternative in av[1]:
                    visit(alternative, repeated)
            elif name in _REPEATS or name == "POSSESSIVE_REPEAT":
                visit(av[2], repeated or av[1] > 1)
            elif name in ("ASSERT", "ASSERT_NOT"):
                visit(av[1], repeated)
        flush()

    visit(list(tree), False)
    for default in DEFAULT_ATOMS:
        add(atoms, default)
    return atoms, prefixes


def probe_inputs(pattern: str, flags: int = 0) -> int:
    """How many inputs the probe runs `pattern` on."""
    atoms, prefixes = pump_atoms(pattern, flags)
    return (1 + len(prefixes)) * len(atoms) * len(TAILS)


# Runs in a separate process: each pattern on its pumped inputs, saying `tick` after each input
# and `done` after each pattern.
_PROBE = r"""
import json, re, sys
size, tails, jobs = json.loads(sys.stdin.read())
print("ready", flush=True)
for pattern, flags, atoms, prefixes in jobs:
    compiled = re.compile(pattern, flags)
    for prefix in [""] + prefixes:
        for atom in atoms:
            body = (atom * (size // len(atom) + 1))[: max(size - len(prefix), 0)]
            for tail in tails:
                compiled.search(prefix + body[: len(body) - len(tail)] + tail)
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
