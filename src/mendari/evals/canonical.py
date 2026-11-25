# SPDX-License-Identifier: Apache-2.0
"""What makes two cases the same case (LP-11): one canonical form of a case's input, used wherever
cases are compared, so the coverage report, the dataset checks (LP-20) and the probe agree on
it.

- `canonical_input`: the strict form, the ledger's canonical JSON (`ledger.canonical`: sorted
  keys, no spaces, ASCII escapes) of a value that holds only JSON (`core.agent.not_json`). Two
  inputs are the same case exactly when their strict forms are equal.
- `normalised_input` (`NORM_VERSION`): the strict form after every string value is folded
  (`fold`: Unicode NFKC, case folded, NFKC again; invisible format characters such as zero-width
  spaces and soft hyphens removed; each run of characters that are not letters, combining marks,
  digits or `_` made one space; trimmed) and every whole number is written as an integer (57.0
  is 57, -0.0 is 0: JSON has one kind of number). Keys, booleans, null and the nesting are kept,
  so two structured inputs that differ in a value other than text never meet. Two inputs that
  differ only in case, spacing, punctuation and symbols (signs and comparison symbols too: "ER+"
  and "ER-" meet), invisible characters or Unicode form (full-width letters, or the "fi"
  ligature against "fi") are the same case under it; a vowel sign or an accent is a letter's.

A change to either form changes what counts as the same case, and what was compared under the old
one stops matching, so their bytes are pinned (tests/test_canonical.py) and the normalised form
names its version."""

from __future__ import annotations

import unicodedata
from typing import Any

from mendari.core.agent import not_json
from mendari.errors import MendariError
from mendari.evals.ledger import canonical

NORM_VERSION = "norm/v1"


def _checked(value: Any) -> Any:
    what = not_json(value)
    if what is not None:
        raise MendariError(f"an input must hold only JSON, not {what}")
    return value


def canonical_input(value: Any) -> str:
    """The strict form of an input: its canonical JSON. A `MendariError` names what in it JSON
    cannot hold (never its text)."""
    return canonical(_checked(value))


def fold(text: str) -> str:
    """One string as `norm/v1` folds it."""
    folded = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", text).casefold())
    kept = "".join(
        ""
        if unicodedata.category(c)
        == "Cf"  # invisible: zero-width spaces, soft hyphens, direction marks
        else c
        if c.isalnum() or c == "_" or unicodedata.category(c).startswith("M")
        else " "
        for c in folded
    )
    return " ".join(kept.split())


def normalised_input(value: Any) -> str:
    """The `norm/v1` form of an input: its strict form once every string value is folded."""
    _checked(value)
    folded: Any = None
    stack: list[tuple[Any, Any, Any]] = [(value, None, None)]  # (item, its parent, its key)
    while stack:  # iterative, as the value may be nested deep
        item, parent, key = stack.pop()
        if isinstance(item, dict):
            copy: Any = {}
            stack.extend((child, copy, name) for name, child in item.items())
        elif isinstance(item, list | tuple):
            copy = [None] * len(item)
            stack.extend((child, copy, index) for index, child in enumerate(item))
        elif isinstance(item, str):
            copy = fold(item)
        elif isinstance(item, float) and item.is_integer():
            copy = int(item)
        else:
            copy = item
        if parent is None:
            folded = copy
        else:
            parent[key] = copy
    return canonical(folded)
