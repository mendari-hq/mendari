"""Regular expressions a pack's author wrote, compiled when the pack loads.

A pack's rules patterns (`rules` providers, case ignored) and the `pattern` keywords of its input
schema run on Python's `re`, as they always have. A pattern that does not compile, or one too
large, is the pack's problem: at load each is compiled with `re` as it will run, at most
`MAX_PATTERN_CHARS` long, and a pack holds at most `MAX_PACK_PATTERNS`, so a bad one is a
`PackValidationError` naming the pack, the file and the rule, never a traceback in `mendari
validate` or an error answer from `/api/health`.

`re` has no time limit, and checking a pattern at load cannot tell how long it will take on an
input: a pattern like `(a+)+$` backtracks for seconds on a few dozen characters. So a pack's
patterns can slow or stall the worker that runs them. Installing a pack means trusting its
author, as with any plugin; packs from others will run in an isolated process with memory and
time limits once uploads or a public registry exist (SECURITY.md).

Mendari's own patterns (dosing, CQL headers, the rules provider's scope and word patterns) are
not pack content and are not checked here.
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Iterator
from typing import Any

MAX_PATTERN_CHARS = 2_000
MAX_PACK_PATTERNS = 500
RULES_FLAGS = re.IGNORECASE  # as `providers/rules.py` compiles them


def problem(text: str, flags: int = 0) -> str | None:
    """Why `text` may not be a pack pattern compiled with `flags`, or None."""
    if len(text) > MAX_PATTERN_CHARS:
        return f"longer than {MAX_PATTERN_CHARS} characters"
    with warnings.catch_warnings():  # a FutureWarning is not a reason to refuse a pattern
        warnings.simplefilter("ignore")
        try:
            re.compile(text, flags)
        except (re.error, OverflowError, RecursionError, ValueError) as error:
            return f"invalid regular expression: {error}"
    return None


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
    """(where, pattern) for every string `pattern` in an input schema. The whole document is
    searched, as a `$ref` can make any object in it a schema; each object once, by identity."""
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
            stack.append((here, value))
    yield from sorted(found)
