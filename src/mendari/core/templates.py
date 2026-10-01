"""Wording templates in packs (FHIR Task text, CDS card summaries).

A template may use `{recommendation}`, `{priority}`, and `{<question>}` for any question of the
pack, which is replaced by that question's label, e.g. "Referral to {specialty} is missing
information". A question without an answer (its provider failed) reads as "unknown".
"""

from __future__ import annotations

import string
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mendari.core.decision import DecisionResult

BUILT_IN = ("recommendation", "priority")


def unknown_placeholders(template: str, questions: Iterable[str]) -> list[str]:
    """Placeholders a template uses that are neither built in nor a question of the pack."""
    allowed = {*BUILT_IN, *questions}
    try:
        names = {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}
    except ValueError:
        return [template]  # unbalanced braces
    return sorted(name for name in names if name not in allowed)


def fill(template: str, result: DecisionResult, *, recommendation: str) -> str:
    values = {question: answer.label for question, answer in result.answers.items()}
    values.update(recommendation=recommendation, priority=result.priority.value)
    return template.format_map(_Unknown(values))


class _Unknown(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "unknown"
