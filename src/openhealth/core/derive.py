"""Derived questions: one answer computed from several others by a decision table.

    eligibility:
      derive:
        from: [inc_age, inc_diagnosis, exc_pregnancy]
        table:
          - {any: "no", then: not_eligible}
          - {any: unknown, then: needs_review}
          - {else: candidate}

Rows are tried top-down and the first match wins; a row with `in: [...]` only looks at those
members. Instead of applying the table only to each
member's most likely label (which would hide uncertainty), the derived answer carries the exact
probability of every row winning, treating the members' answers as independent. A dynamic
program walks the members once, tracking which rows are satisfied so far:
O(members x labels x 2^rows), and rows are capped at 10.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, MutableMapping
from dataclasses import replace

from openhealth.core.spec import DeriveRow, PackManifest, Question
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


def row_holds(row: DeriveRow, answers: Mapping[str, Answer]) -> bool:
    """Whether a row matches the members' most likely labels."""
    labels = [a.label for member, a in answers.items() if row.applies_to(member)]
    if row.any is not None:
        return row.any in labels
    return row.all is not None and all(label == row.all for label in labels)


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
                    holds
                    if not row.applies_to(member)
                    else (holds or label == row.any)
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
    deciding = next((row for row in rows if row_holds(row, likely)), spec.table[-1])
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


def explain(question: Question, answers: Mapping[str, Answer]) -> list[str]:
    """One reason per table row that the members' most likely labels satisfy, naming the
    members that satisfy it, e.g. "needs_review: egfr_30_or_more, stable_metformin are unknown".
    Every holding row is listed, not only the first, so a reviewer sees all open criteria."""
    spec = question.derive
    if spec is None or any(member not in answers for member in spec.members):
        return []
    reasons = []
    for row in spec.table[:-1]:
        scoped = {m: a for m, a in answers.items() if m in spec.members and row.applies_to(m)}
        if not row_holds(row, scoped):
            continue
        label = row.any if row.any is not None else row.all
        names = [m for m, a in scoped.items() if a.label == label]
        verb = "is" if len(names) == 1 else "are"
        reasons.append(f"{row.result}: {', '.join(names)} {verb} {label}")
    return reasons
