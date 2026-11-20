# SPDX-License-Identifier: Apache-2.0
"""Derived questions: one answer computed from several others by a decision table.

    eligibility:
      derive:
        from: [inc_age, inc_diagnosis, exc_pregnancy]
        table:
          - {any: "no", then: not_eligible}
          - {any: unknown, then: needs_review}
          - {else: candidate}

Rows are tried top-down and the first match wins. `any` / `all` take a label or a list of
labels; a row with `in: [...]` only looks at those members, and a row with
`when: {member: [labels], ...}` only counts while each named member's answer is one of its
labels. Instead of applying the table only to each member's most likely label (which would
hide uncertainty), the derived answer carries the exact probability of every row winning,
treating the members' answers as independent. A dynamic program walks the members once,
tracking which rows are satisfied so far and whether each row's `when` is still open:
O(members x labels x states), at most 4^rows states, and rows are capped at 10.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, MutableMapping
from dataclasses import replace

from mendari.core.patterns import check_time
from mendari.core.spec import DeriveRow, PackManifest, Question
from mendari.errors import DERIVED_INPUT_UNAVAILABLE
from mendari.providers.base import Answer, ProviderInfo

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
            failures[key] = DERIVED_INPUT_UNAVAILABLE
            continue
        answers[key] = derive_answer(key, question, answers)
    for key, question in manifest.questions.items():
        answer = answers.get(key)
        if answer is not None and question.type == "score" and answer.value is None:
            answers[key] = replace(answer, value=question.expected_score(answer.probabilities))


def row_holds(row: DeriveRow, answers: Mapping[str, Answer]) -> bool:
    """Whether a row matches the members' most likely labels (`answers`: every member's)."""
    for gate_member, open_labels in row.gates.items():
        gate_answer = answers.get(gate_member)
        if gate_answer is None or gate_answer.label not in open_labels:
            return False
    labels = [a.label for member, a in answers.items() if row.applies_to(member)]
    if row.any is not None:
        return any(label in row.labels for label in labels)
    return all(label in row.labels for label in labels)


def derive_answer(key: str, question: Question, answers: Mapping[str, Answer]) -> Answer:
    spec = question.derive
    if spec is None:
        raise ValueError(f"{key} is not a derived question")
    rows = spec.table[:-1]
    fallback = spec.table[-1].result

    # A state is a 2n-bit integer: bit i records whether row i's condition holds so far (`any` rows
    # start 0 and become 1 on a match; `all` rows start 1 and become 0 on a mismatch), and bit
    # n + i records whether row i's `when` gate is still open (starts 1; each gating member, when
    # seen, must match). Precomputing bit masks per (member, label) makes transitions O(1) bitwise
    # ops while keeping the exact probability distribution identical.
    n = len(rows)
    matches = [row.labels for row in rows]
    gates = [row.gates for row in rows]
    full_mask = (1 << (2 * n)) - 1
    start = ((1 << n) - 1) << n  # all gates open
    for i, row in enumerate(rows):
        if row.all is not None:
            start |= 1 << i
    states: dict[int, float] = {start: 1.0}
    for member in spec.members:
        check_time()
        transitions: list[tuple[int, int, float]] = []
        for label, p_label in answers[member].probabilities.items():
            if p_label <= 0.0:
                continue
            set_bits = 0
            keep_bits = full_mask
            for i, (row, match, gate) in enumerate(zip(rows, matches, gates, strict=True)):
                if row.applies_to(member):
                    if row.any is not None:
                        if label in match:
                            set_bits |= 1 << i
                    elif label not in match:
                        keep_bits &= ~(1 << i)
                if member in gate and label not in gate[member]:
                    keep_bits &= ~(1 << (n + i))
            transitions.append((set_bits, keep_bits, p_label))
        advanced: dict[int, float] = defaultdict(float)
        for steps, (state, p_state) in enumerate(states.items(), 1):
            if steps & 4095 == 0:
                check_time()
            for set_bits, keep_bits, p_label in transitions:
                advanced[(state | set_bits) & keep_bits] += p_state * p_label
        states = advanced

    probabilities = dict.fromkeys(question.criteria, 0.0)
    for state, p_state in states.items():
        both = state & (state >> n)
        if both:
            # Lowest set bit is the first matching row.
            first_row = (both & -both).bit_length() - 1
            result = rows[first_row].result
        else:
            result = fallback
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
    members = {m: answers[m] for m in spec.members}
    reasons = []
    for row in spec.table[:-1]:
        if not row_holds(row, members):
            continue
        matched = {
            m: a.label for m, a in members.items() if row.applies_to(m) and a.label in row.labels
        }
        if len(set(matched.values())) == 1:  # "a, b are no"
            verb = "is" if len(matched) == 1 else "are"
            found = f"{', '.join(matched)} {verb} {next(iter(matched.values()))}"
        else:  # "a is no, b is unknown"
            found = ", ".join(f"{m} is {label}" for m, label in matched.items())
        gate = ", ".join(f"{m} is {members[m].label}" for m in row.gates)
        reasons.append(f"{row.result}: {found}{f' ({gate})' if gate else ''}")
    return reasons
