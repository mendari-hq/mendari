"""Derived questions: one answer computed from several others by a decision table.

    eligibility:
      derive:
        from: [inc_age, inc_diagnosis, exc_pregnancy]
        table:
          - {any: "no", then: not_eligible}
          - {any: unknown, then: needs_review}
          - {else: candidate}

Rows are tried top-down and the first match wins. Instead of applying the table only to each
member's most likely label (which would hide uncertainty), the derived answer carries the exact
probability of every row winning, treating the members' answers as independent. A dynamic
program walks the members once, tracking which rows are satisfied so far:
O(members x labels x 2^rows), and rows are capped at 10.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, MutableMapping
from dataclasses import replace

from openhealth.core.spec import PackManifest, Question
from openhealth.providers.base import Answer, ProviderInfo

DERIVED = ProviderInfo(slot="derived", variant="decision_table", type="derived")


def complete_answers(
    manifest: PackManifest,
    answers: MutableMapping[str, Answer],
    failures: MutableMapping[str, str],
) -> None:
    """Compute every derived question, then add expected values to score answers."""
    for key, question in manifest.derived.items():
        spec = question.derive
        if spec is None:
            continue
        if any(member not in answers for member in spec.members):
            failures[key] = "derived_input_unavailable"
            continue
        answers[key] = derive_answer(key, question, answers)
    for key, question in manifest.questions.items():
        answer = answers.get(key)
        if answer is not None and question.type == "score" and answer.value is None:
            answers[key] = replace(answer, value=question.expected_score(answer.probabilities))


def derive_answer(key: str, question: Question, answers: Mapping[str, Answer]) -> Answer:
    spec = question.derive
    if spec is None:
        raise ValueError(f"{key} is not a derived question")
    rows = spec.table[:-1]
    fallback = spec.table[-1].result

    # A state records, per row, whether its condition holds so far: `any` rows start False
    # and become True on a match; `all` rows start True and become False on a mismatch.
    start = tuple(row.all is not None for row in rows)
    states: dict[tuple[bool, ...], float] = {start: 1.0}
    for member in spec.members:
        advanced: dict[tuple[bool, ...], float] = defaultdict(float)
        for state, p_state in states.items():
            for label, p_label in answers[member].probabilities.items():
                if p_label <= 0.0:
                    continue
                new = tuple(
                    (holds or label == row.any)
                    if row.any is not None
                    else (holds and label == row.all)
                    for holds, row in zip(state, rows, strict=True)
                )
                advanced[new] += p_state * p_label
        states = advanced

    probabilities = dict.fromkeys(question.criteria, 0.0)
    for state, p_state in states.items():
        result = next(
            (row.result for holds, row in zip(state, rows, strict=True) if holds), fallback
        )
        probabilities[result] += p_state
    total = sum(probabilities.values()) or 1.0
    probabilities = {label: p / total for label, p in probabilities.items()}
    label = max(probabilities, key=lambda item: probabilities[item])

    likely = {member: answers[member] for member in spec.members}
    deciding = next(
        (
            row
            for row in rows
            if (row.any is not None and any(a.label == row.any for a in likely.values()))
            or (row.all is not None and all(a.label == row.all for a in likely.values()))
        ),
        spec.table[-1],
    )
    evidence = (
        f"table: {deciding.describe()}",
        *(f"{member}={a.label} ({a.confidence:.2f})" for member, a in likely.items()),
    )
    return Answer(
        question=key,
        label=label,
        confidence=probabilities[label],
        probabilities=probabilities,
        provider=DERIVED,
        evidence=evidence,
    )
