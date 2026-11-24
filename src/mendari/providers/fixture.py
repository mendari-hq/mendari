# SPDX-License-Identifier: Apache-2.0
"""Matching a canned fixture's cases against an input: what an `agent` variant's `mock_file`
(`providers/agent.py`) and a rubric's canned judge (`providers/judge.py`) share.

A case's `match` is a text (found anywhere in the input's strings, whatever their case), a list
of texts (any of them), or a mapping of `contains_any`, `contains_all`, `exclude_any` (lists of
texts) and `input` (a mapping every key of which the input holds with that value)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def match_problem(spec: Any) -> str | None:
    """Why `spec` is no `match`, or None."""
    if isinstance(spec, str):
        return None if spec.strip() else "must not be empty"
    if isinstance(spec, list):
        if not spec or not all(isinstance(s, str) and s.strip() for s in spec):
            return "must be a non-empty list of strings"
        return None
    if isinstance(spec, dict):
        allowed = {"contains_any", "contains_all", "exclude_any", "input"}
        extra = sorted(set(spec) - allowed)
        if extra:
            return f"unexpected keys {extra}"
        if not spec:
            return "must not be empty"
        for key in ("contains_any", "contains_all", "exclude_any"):
            if key in spec:
                val = spec[key]
                if not isinstance(val, list) or not all(isinstance(s, str) and s for s in val):
                    return f"{key} must be a list of non-empty strings"
        if "input" in spec and not isinstance(spec["input"], dict):
            return "input must be a mapping"
        return None
    return "must be a string, list of strings, or mapping"


def flatten(value: Any) -> str:
    """Every string of `value`, in order, one a line, lowercased: what a `match` is searched in."""
    parts: list[str] = []
    stack: list[Any] = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, Mapping):
            stack.extend(reversed(list(item.values())))
        elif isinstance(item, list | tuple):
            stack.extend(reversed(item))
    return "\n".join(parts).lower()


def subset_match(expected: Mapping[str, Any], actual: Any) -> bool:
    """Whether `actual` holds every key of `expected` with that value (mappings compared the
    same way, a boolean never equal to a number)."""
    if not isinstance(actual, Mapping):
        return False
    for key, wanted in expected.items():
        if key not in actual:
            return False
        if isinstance(wanted, Mapping):
            if not subset_match(wanted, actual[key]):
                return False
        elif isinstance(wanted, bool) or isinstance(actual[key], bool):
            if type(actual[key]) is not type(wanted) or actual[key] != wanted:
                return False
        elif actual[key] != wanted:
            return False
    return True


def matches(spec: Any, text: str, state: Mapping[str, Any]) -> bool:
    """Whether a case's `match` (one `match_problem` passed) fits: `text` is `flatten(state)`."""
    if isinstance(spec, str):
        return spec.lower() in text
    if isinstance(spec, list):
        return any(needle.lower() in text for needle in spec)
    assert isinstance(spec, dict)
    if "contains_any" in spec and not any(s.lower() in text for s in spec["contains_any"]):
        return False
    if "contains_all" in spec and not all(s.lower() in text for s in spec["contains_all"]):
        return False
    if "exclude_any" in spec and any(s.lower() in text for s in spec["exclude_any"]):
        return False
    return not ("input" in spec and not subset_match(spec["input"], state))
