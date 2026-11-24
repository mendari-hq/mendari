# SPDX-License-Identifier: Apache-2.0
"""Regular expressions a pack's author wrote, compiled when the pack loads, and the engine
that runs them.

A pack's rules patterns (`rules` providers, case ignored) and the `pattern` keywords and
`patternProperties` keys of its input schema are written in Python's `re` syntax. A pattern
that does not compile, or one too large, is the pack's problem: at load each is compiled as it
will run, at most `MAX_PATTERN_CHARS` long, and a pack holds at most `MAX_PACK_PATTERNS`, so a
bad one is a `PackValidationError` naming the pack, the file and the rule, never a traceback in
`mendari validate` or an error answer from `/api/health`.

One engine choice, `compile` (and `search`), runs every one of them. Its keyword `community`
names the B11 community engine, which runs the packs the deployment does not own
(`Pack.bounded`, `core/origin.py`, ADR-0009):

- **A pack the deployment does not own** (a community pack, or one pulled from a registry whose
  digest is not a bundled agent's of this release) has its patterns run on RE2 (`google-re2`),
  which never backtracks: its time grows with the text's length times the pattern's compiled
  size, never exponentially, so no pattern can stall a worker the way `(a+)+$` stalls `re`.
  Each is rewritten for RE2 to mean what `re` means (`core/re2_syntax.py`, which lists the
  differences that remain); one RE2 cannot run (a lookaround, a back reference) or cannot
  compile (too large once compiled) makes the pack invalid, saying what to write instead.
- **Every other pack's** (the bundled agents, a pulled copy of one included, and an operator's
  local packs) run on `re`, exactly as they always have. `re` has no time limit, and checking a
  pattern at load cannot tell how long it will take on an input: a pattern like `(a+)+$`
  backtracks for seconds on a few dozen characters. Installing such a pack means trusting its
  author, as with any plugin (SECURITY.md).

Mendari's own patterns (dosing, CQL headers, the rules provider's scope, word and negation
patterns) are not pack content and stay on `re`. RE2 and the rewriting are imported only when a
pattern of a pack on the community engine is compiled, so a deployment without one never loads
them.

The patterns of a pack on the community engine, and its input schema's checks, share one time
budget per decision (`time_budget`, set by `Decision.decide`), counted in the CPU time of the
decision's own thread (`time.thread_time`): waiting for a model or for another request costs it
nothing, so the same input decides the same way on a busy server. RE2's time is linear and the
caps bound how many patterns run and how large they are (`core/caps.py`), but a crafted pack
within every cap can still take seconds on adversarial text. Each search, each match a search
yields, each step of the rules provider's own loops over a text, each schema applied and each
error a schema check finds (`check_time`) checks the budget, and raises `OutOfTime` once it is
spent: the pack's rules, or its schema check, stop there and the decision abstains, for a person
(an input the schema had already found wrong is refused). A decision runs such a pack's
patterns and schema for at most the budget plus one step already under way: one search can
take about 2.5 s on 10,000 characters of emoji against the largest pattern RE2 compiles, so
about 3.5 s of CPU at worst.
"""

from __future__ import annotations

import functools
import re
import time
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol

from mendari.errors import MendariError

MAX_PATTERN_CHARS = 2_000
MAX_PACK_PATTERNS = 500
RULES_FLAGS = re.IGNORECASE  # as `providers/rules.py` compiles them
COMMUNITY_TIME_BUDGET_S = 1.0  # a community pack's patterns, together, in one decision
_DEADLINE: ContextVar[float | None] = ContextVar("community_pattern_deadline", default=None)


class OutOfTime(MendariError):
    """A community pack's patterns used their time budget for this decision."""


@contextmanager
def time_budget(seconds: float = COMMUNITY_TIME_BUDGET_S) -> Iterator[None]:
    """The CPU time (this thread's) a community pack's patterns and schema checks may take,
    together, inside this block (one decision). Held in a context variable, so concurrent
    decisions keep their own."""
    token = _DEADLINE.set(time.thread_time() + seconds)
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def budget_active() -> bool:
    """Whether a time budget holds here (inside a community pack's decision)."""
    return _DEADLINE.get() is not None


def check_time() -> None:
    """Raise `OutOfTime` when the current decision's budget is spent (no budget, no limit)."""
    deadline = _DEADLINE.get()
    if deadline is not None and time.thread_time() > deadline:
        raise OutOfTime(
            f"its patterns and schema checks took more than {COMMUNITY_TIME_BUDGET_S:g} s"
        )


# RE2's memory for one pattern (its default): its compiled program and the cache of the
# automaton that runs it. Bounds both, and so the compiled size a pattern may reach: a larger
# one does not compile. `re`'s Unicode `\w` is a large class once compiled (about 1,300
# instructions a copy), so `(?:\w{1,20}\s?){1,20}` needs most of it, and `\w{0,1000}` is too large.
RE2_MAX_MEMORY = 8 << 20
_SURROGATE = re.compile("[\ud800-\udfff]")


class Match(Protocol):
    def start(self) -> int: ...


class Matcher(Protocol):
    """A compiled pattern, of either engine: what the runtime calls on one."""

    def search(self, text: str, /) -> Match | None: ...

    def finditer(self, text: str, /) -> Iterator[Match]: ...


class _Re2Pattern:
    """A pattern compiled for RE2, searched like an `re` pattern. Text is UTF-8 to RE2, so a
    lone surrogate (which a JSON string can hold) is read as U+FFFD, the same length."""

    __slots__ = ("_regexp",)

    def __init__(self, regexp: Any):
        self._regexp = regexp

    def search(self, text: str, /) -> Match | None:
        check_time()
        match: Match | None = self._regexp.search(_encodable(text))
        return match

    def finditer(self, text: str, /) -> Iterator[Match]:
        check_time()
        for match in self._regexp.finditer(_encodable(text)):
            yield match
            check_time()


def _encodable(text: str) -> str:
    if text.isascii():
        return text
    try:
        text.encode()
    except UnicodeEncodeError:
        return _SURROGATE.sub("\ufffd", text)
    return text


def _options() -> Any:
    import re2

    options = re2.Options()
    options.max_mem = RE2_MAX_MEMORY
    options.never_capture = True  # only where a match starts is read
    options.log_errors = False  # a refused pattern is the pack's problem, not a log line
    return options


# The most copies of characters, classes and `.`s (`re2_syntax.size`) a pattern may make for RE2.
# Past it, a pattern never fits `RE2_MAX_MEMORY` (`a{1000000}` does not), and it is refused
# before it is written out for RE2: a count over RE2's 1,000 is spelt out as counts one after
# another, so `(?:(?:a{99999}){99999}){99999}`, a few characters, would be written out as
# gigabytes.
MAX_RE2_SIZE = 1_000_000


@functools.lru_cache(maxsize=4_096)
def _re2(text: str, flags: int) -> _Re2Pattern:
    """`text` compiled for RE2, once: raises `re.error`, `Unsupported` or `re2.error`."""
    import re2

    from mendari.core.re2_syntax import Unsupported, size, translate

    if size(text, flags) > MAX_RE2_SIZE:
        raise Unsupported(_too_large())
    return _Re2Pattern(re2.compile(translate(text, flags), _options()))


def compile(text: str, flags: int = 0, *, community: bool = False) -> Matcher:
    """A pack's pattern compiled for the engine its pack's patterns run on (module docstring):
    `community` for a pack on the community engine (`Pack.bounded`); for any other pack, this is
    `re.compile(text, flags)`."""
    if community:
        return _re2(text, flags)
    return re.compile(text, flags)


def search(pattern: str, text: str, *, community: bool = False) -> bool:
    """Whether a pack's pattern (an input schema's, so no flags) matches anywhere in `text`."""
    if community:
        return _re2(pattern, 0).search(text) is not None
    return re.search(pattern, text) is not None


def problem(text: str, flags: int = 0, *, community: bool = False) -> str | None:
    """Why `text` may not be a pack pattern compiled with `flags`, or None. A pattern of a pack
    on the community engine (`community`, `Pack.bounded`) must also run on RE2."""
    if len(text) > MAX_PATTERN_CHARS:
        return f"longer than {MAX_PATTERN_CHARS} characters"
    with warnings.catch_warnings():  # a FutureWarning is not a reason to refuse a pattern
        warnings.simplefilter("ignore")
        try:
            re.compile(text, flags)
        except (re.error, OverflowError, RecursionError, ValueError) as error:
            return f"invalid regular expression: {error}"
        if not community:
            return None
        import re2

        from mendari.core.re2_syntax import Unsupported

        try:
            _re2(text, flags)
        except Unsupported as error:
            return str(error)
        except re2.error as error:
            return _re2_problem(str(error))
        except (re.error, OverflowError, RecursionError, ValueError) as error:
            return f"invalid regular expression: {error}"
    return None


# What a load-time message says RE2 is: it names no origin, as the same check refuses a pattern of
# a community pack and of one from a registry.
_RE2_ENGINE = "the engine community agents and agents from a registry run on"


def _re2_problem(message: str) -> str:
    """What RE2 refused, in words a pack author can act on."""
    if "repetition" in message:
        return (
            f"repeats nested inside each other multiply past 1,000, the most RE2 ({_RE2_ENGINE}) "
            "allows (e.g. (?:a{1,50}){1,50}): use smaller counts"
        )
    if "too large" in message:
        return _too_large()
    return f"RE2 ({_RE2_ENGINE}) cannot compile it"


def _too_large() -> str:
    return (
        f"too large for RE2 ({_RE2_ENGINE}) once compiled: use smaller counts and classes, or "
        "split it into several rules"
    )


def rules_patterns(rules: Any) -> Iterator[tuple[str, str]]:
    """(where, pattern) for every pattern of a `rules` config, in the shape the provider reads
    (`questions.<q>.labels.<label>[i].pattern`). Each object is visited once, by identity, so a
    rule or a list of rules shared through YAML aliases is one rule, found where it first is.
    Anything else in the file is the provider's to refuse when it is built."""
    questions = rules.get("questions") if isinstance(rules, dict) else None
    if not isinstance(questions, dict):
        return
    seen: set[int] = set()
    for question, spec in questions.items():
        labels = spec.get("labels") if isinstance(spec, dict) else None
        if not isinstance(labels, dict) or id(labels) in seen:
            continue
        seen.add(id(labels))
        for label, items in labels.items():
            if not isinstance(items, list) or id(items) in seen:
                continue
            seen.add(id(items))
            for index, item in enumerate(items):
                if not isinstance(item, dict) or id(item) in seen:
                    continue
                seen.add(id(item))
                if isinstance(item.get("pattern"), str):
                    name = item.get("name")
                    where = f"questions.{question}.labels.{label}[{index}]"
                    if isinstance(name, str):
                        where += f" ({name})"
                    yield where, item["pattern"]


def schema_patterns(schema: Any) -> Iterator[tuple[str, str]]:
    """(where, pattern) for every string `pattern` and every key of a `patternProperties` object
    in an input schema (jsonschema runs both with `re.search`). The whole document is searched,
    as a `$ref` can make any object in it a schema; each object once, by identity."""
    stack: list[tuple[str, Any]] = [("", schema)]
    seen: set[int] = set()
    found = []
    while stack:
        path, node = stack.pop()
        if not isinstance(node, dict | list) or id(node) in seen:
            continue
        seen.add(id(node))
        items = node.items() if isinstance(node, dict) else enumerate(node)
        for key, value in items:
            here = f"{path}/{key}" if path else str(key)
            if key == "pattern" and isinstance(node, dict) and isinstance(value, str):
                found.append((here, value))
            if key == "patternProperties" and isinstance(node, dict) and isinstance(value, dict):
                found += [(f"{here}/{_shortened(text)}", text) for text in value]
            stack.append((here, value))
    yield from sorted(found)


def _shortened(text: str, limit: int = 40) -> str:
    """A pattern named in a location, cut to `limit` characters."""
    return text if len(text) <= limit else text[:limit] + "..."
