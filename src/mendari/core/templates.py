# SPDX-License-Identifier: Apache-2.0
"""Wording templates in packs (FHIR Task text, CDS card summaries).

A template may use `{recommendation}`, `{priority}`, and `{<question>}` for any question of the
pack, which is replaced by that question's label, e.g. "Referral to {specialty} is missing
information". A question without an answer (its provider failed) reads as "unknown".

A CDS Hooks card is read by a person (`fill_display`, LP-23f): there each label reads as its
display name (`Question.name_of`: the pack's `names`, else a readable form of the label), and no
recommendation reads as "manual triage". A Task's text and its performer code (`fill`) keep the
labels as they are.

A placeholder is a bare name (`template_problems`): no conversion, format spec, attribute or
index, which would let a template pad a label to any length or reach into what fills it. A pack
the deployment does not own is refused for one; an owned pack is warned, and `fill` renders its
templates as it always did.
"""

from __future__ import annotations

import itertools
import math
import string
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mendari.core.decision import DecisionResult
    from mendari.core.spec import PackManifest

BUILT_IN = ("recommendation", "priority")
UNKNOWN = "unknown"  # what a placeholder reads when its question has no answer
NO_RECOMMENDATION = "manual triage"  # what a card says `{recommendation}` is without one
SHOWN_CHARS = 40  # how much of a placeholder a problem shows


def unknown_placeholders(template: str, questions: Iterable[str]) -> list[str]:
    """Placeholders a template uses that are neither built in nor a question of the pack."""
    allowed = {*BUILT_IN, *questions}
    try:
        names = {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}
    except ValueError:
        return [template]  # unbalanced braces
    return sorted(name for name in names if name not in allowed)


def template_problems(template: str, questions: Iterable[str]) -> list[str]:
    """Why a wording template is not bare, each problem worded to follow where the template is
    (`cds_hooks.card.summaries.<key> uses ...`); none when it is. Bare: every placeholder is a
    built-in or a question (`unknown_placeholders`) and only its name, with no conversion
    (`{priority!r}`), format spec (`{priority:>9}`), attribute (`{priority.x}`) or index
    (`{priority[0]}`)."""
    questions = list(questions)
    unknown = unknown_placeholders(template, questions)
    try:
        fields = [
            (name, conversion, spec)
            for _, name, spec, conversion in string.Formatter().parse(template)
            if name is not None
        ]
    except ValueError:
        return [f"uses unknown placeholders {unknown}"]  # unbalanced braces
    reaching = {name for name, _, _ in fields if "." in name or "[" in name}
    problems = []
    if named := [name for name in unknown if name not in reaching]:
        problems.append(f"uses unknown placeholders {named}")
    shaped = {
        _shown(name, conversion, spec)
        for name, conversion, spec in fields
        if conversion is not None or spec or name in reaching
    }
    problems += [
        f"uses {shown}: a placeholder is a bare name, with no conversion, format spec, "
        "attribute or index"
        for shown in sorted(shaped)
    ]
    return problems


def _shown(name: str, conversion: str | None, spec: str | None) -> str:
    """A placeholder as written, at most `SHOWN_CHARS` of it."""
    text = name + (f"!{conversion}" if conversion else "") + (f":{spec}" if spec else "")
    text = "{" + text + "}"
    return text if len(text) <= SHOWN_CHARS else text[: SHOWN_CHARS - 4] + "...}"


def fill(template: str, result: DecisionResult, *, recommendation: str) -> str:
    values = {question: answer.label for question, answer in result.answers.items()}
    values.update(recommendation=recommendation, priority=result.priority.value)
    return template.format_map(_Unknown(values))


def fill_display(template: str, result: DecisionResult, manifest: PackManifest) -> str:
    """A card's words: as `fill`, with each answer and the recommendation as a person reads it
    (`shown`), and `NO_RECOMMENDATION` when there is none."""
    values = {
        question: shown(manifest, question, answer.label)
        for question, answer in result.answers.items()
    }
    primary = manifest.policy.primary
    recommendation = result.recommendation
    values.update(
        recommendation=NO_RECOMMENDATION
        if recommendation is None
        else shown(manifest, primary, recommendation),
        priority=result.priority.value,
    )
    return template.format_map(_Unknown(values))


def shown(manifest: PackManifest, question: str, label: str) -> str:
    """One of a question's labels as a person reads it (`Question.name_of`); a question the pack
    does not have keeps the label."""
    found = manifest.questions.get(question)
    return label if found is None else found.name_of(label)


def display_choices(manifest: PackManifest) -> dict[str, list[str]]:
    """Every text each placeholder of a card summary can read (`fill_display`): the primary
    question's display names or `NO_RECOMMENDATION`, each priority, and each question's display
    names or `UNKNOWN`."""
    primary = manifest.questions[manifest.policy.primary]
    from mendari.core.spec import Priority  # spec imports this module

    return {
        "recommendation": [
            *(primary.name_of(label) for label in primary.labels),
            NO_RECOMMENDATION,
        ],
        "priority": [priority.value for priority in Priority],
        **{
            key: [*(question.name_of(label) for label in question.labels), UNKNOWN]
            for key, question in manifest.questions.items()
        },
    }


def longest(template: str, choices: Mapping[str, Sequence[str]]) -> str:
    """The longest text `template` can give (`expansions`) when each placeholder takes one of its
    `choices`: each takes its longest choice, since a text's length is its words' plus each
    placeholder's value, so one expansion is enough, however many a template has."""
    picked = {name: [max(values, key=len)] for name, values in choices.items()}
    ((text, _),) = expansions(template, picked)
    return text


def expansion_count(template: str, choices: Mapping[str, Sequence[str]]) -> int:
    """How many texts `expansions` builds for `template` (the product of its placeholders'
    choices, each placeholder counted once), without building any: so a caller can refuse a
    template whose expansions would not fit in memory. A placeholder without choices counts
    one: `expansions` refuses it."""
    names = dict.fromkeys(name for _, name, _, _ in string.Formatter().parse(template) if name)
    return math.prod(len(choices.get(name, ("",))) for name in names)


def expansions(
    template: str, choices: Mapping[str, Sequence[str]]
) -> list[tuple[str, dict[str, str]]]:
    """Every text `fill` can give for a template, each with the placeholder values that give it,
    when each placeholder takes each of its `choices` (e.g. a question's labels and `unknown`).
    A template without placeholders has one expansion: itself."""
    names = list(
        dict.fromkeys(name for _, name, _, _ in string.Formatter().parse(template) if name)
    )
    found = []
    for picked in itertools.product(*(choices[name] for name in names)):
        values = dict(zip(names, picked, strict=True))
        found.append((template.format_map(_Unknown(values)), values))
    return found


class _Unknown(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return UNKNOWN
