# SPDX-License-Identifier: Apache-2.0
"""Repeated runs: ask the same question N times, and never automate when the runs disagree
(ADR-0024; spec 0.4, `repeat` on an `agent` or `llm` variant, 2 to 5).

    agent:
      type: agent
      repeat: 3

A model that gives two answers to one question is unsure of it, whatever confidence it reports
with each. So, for each of the slot's questions:

- the answer is the majority's (the most frequent label among the runs that answered; a tie
  goes to the earliest run, and is a disagreement either way);
- its confidence, its distribution and its evidence are those of the agreeing run that was
  least confident: the lowest calibrated confidence among the runs that agree;
- when not every one of the N runs gave that answer, the runs disagreed. A run that failed
  (a timeout, a malformed answer, a used-up budget) gave no answer, so it counts as a
  disagreement, never as agreement. On a question the decision turns on
  (`signals.deciding_questions`) a disagreement is a cap: AUTO becomes HUMAN_REVIEW, the reason
  `2 of 3 runs agreed on <question>`. On another question it is in the evidence only.

Agreement changes nothing: it raises no confidence and lifts no cap.

The actions the runs proposed (spec 0.4, an `agent` variant) are held to the same rule
(`_proposals`). The slot proposes each distinct one, a name with its arguments, in the order
first proposed. One that every one of the N runs proposed is carried as it is. One that only
some runs proposed (a run that failed proposed nothing) is carried `disputed`: the decision is
capped, the reason `2 of 3 runs proposed action <name>` (or `the 3 runs proposed action <name>
with different arguments`), and the action is held for the decision's reviewer and never
released without one, whatever its effect (`policy.gate_of`). A reason names a declared action
only, and never an argument.

Every run is the provider's own single call, made in turn: each one is charged to the daily
budget and the organization's quota, counted and priced by the decision's meter, and held to
the decision's one deadline, which the N share. Its tokens and cost are the sum of the runs
that answered; a run that was sent and did not answer leaves the decision's cost unknown, as
any failed call does (`Decision.decide`). If every run fails, the slot fails with the first
run's error.

Canned answers (an agent's `mock_file`) cannot vary, so repeating them would prove nothing:
they are asked once, the evidence says so, and no agreement is claimed (`Checked.canned`).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import replace

from mendari.core import actions as proposals
from mendari.core.spec import AgentManifest
from mendari.errors import ProviderError
from mendari.providers.base import Answer, Provider, ProviderOutput
from mendari.providers.signals import (
    Cap,
    Checked,
    Signals,
    deciding_questions,
    merged,
    proposed_what,
)

MIN_RUNS, MAX_RUNS = 2, 5
CANNED_NOTE = "repeat: asked once, canned answers cannot vary, so no agreement is claimed"


def agreed_reason(agreed: int, runs: int, question: str, failed: list[str]) -> str:
    """The cap's reason: how many of the N runs gave the answer, and how many failed."""
    reason = f"{agreed} of {runs} runs agreed on {question}"
    if failed:
        codes = ", ".join(sorted(set(failed)))
        reason += f" ({len(failed)} failed: {codes})"
    return reason


def _proposals(
    manifest: AgentManifest, outputs: Sequence[ProviderOutput], runs: int
) -> tuple[tuple[proposals.Proposed, ...], list[str]]:
    """The actions the runs proposed, as the slot proposes them, and why they did not agree.
    Each distinct proposal (a name with its arguments) once, in the order first proposed; one
    that fewer than `runs` runs proposed is `disputed` (`outputs` are the runs that answered: a
    run that failed proposed nothing). A reason for each action's name the runs differed on."""
    found: dict[tuple[str, str], proposals.Proposed] = {}
    times: Counter[tuple[str, str]] = Counter()  # the runs that proposed exactly this
    named: Counter[str] = Counter()  # the runs that proposed an action of this name
    for output in outputs:
        own = proposals.distinct(output.actions)
        for proposal in own:
            found.setdefault(proposals.key(proposal), proposal)
            times[proposals.key(proposal)] += 1
        named.update({proposal.name for proposal in own})
    combined = tuple(
        proposal if times[key] == runs else replace(proposal, disputed=True)
        for key, proposal in found.items()
    )
    reasons = []
    for name in dict.fromkeys(proposal.name for proposal in combined if proposal.disputed):
        what = proposed_what(manifest, name)
        if named[name] == runs:
            reasons.append(f"the {runs} runs proposed {what} with different arguments")
        else:
            reasons.append(f"{named[name]} of {runs} runs proposed {what}")
    return combined, list(dict.fromkeys(reasons))


def repeated(
    provider: Provider, runs: int | None, ask: Callable[[], ProviderOutput]
) -> ProviderOutput:
    """`ask()` once (no `runs`), or `runs` times combined as the module docstring says."""
    if runs is None:
        return ask()
    info = provider.info
    outputs: list[ProviderOutput] = []
    failed: list[str] = []
    first_error: ProviderError | None = None
    for attempt in range(runs):
        try:
            output = ask()
        except ProviderError as error:
            failed.append(error.code)
            first_error = first_error or error
            continue
        if output.canned is not None:
            noted = {
                key: replace(answer, evidence=(CANNED_NOTE, *answer.evidence))
                for key, answer in output.answers.items()
            }
            once = Checked(info.slot, info.variant, "repeat", runs, attempt, canned=True)
            return replace(
                output, answers=noted, signals=output.signals.merge(Signals(checked=(once,)))
            )
        outputs.append(output)
    if not outputs:
        assert first_error is not None
        raise first_error

    deciding = deciding_questions(provider.agent.manifest)
    answers: dict[str, Answer] = {}
    caps: list[Cap] = []
    disagreed: list[str] = []
    reported = {}
    for key in provider.questions:
        given = [output.answers[key].label for output in outputs]
        counts = Counter(given)
        most = max(counts.values())
        label = next(candidate for candidate in given if counts[candidate] == most)
        agreeing = [output for output in outputs if output.answers[key].label == label]
        chosen = min(agreeing, key=lambda output: output.answers[key].confidence)
        answer = chosen.answers[key]
        note = f"repeat: {len(agreeing)} of {runs} runs agreed"
        if len(agreeing) < runs:
            others = [candidate for candidate in given if candidate != label]
            if others:
                note += f" (others: {', '.join(dict.fromkeys(others))})"
            if failed:
                note += f" ({len(failed)} failed)"
            if key in deciding:
                caps.append(Cap("repeat", agreed_reason(len(agreeing), runs, key, failed)))
                disagreed.append(key)
        answers[key] = replace(answer, evidence=(note, *answer.evidence))
        if key in chosen.signals.reported:
            reported[key] = chosen.signals.reported[key]

    proposed, differed = _proposals(provider.agent.manifest, outputs, runs)
    caps += [Cap("repeat", reason) for reason in differed]
    found = merged(output.signals for output in outputs)
    costs = [output.cost_usd for output in outputs]
    checked = Checked(
        info.slot,
        info.variant,
        "repeat",
        runs,
        runs - 1,
        tuple(disagreed),
        proposals=sum(proposal.disputed for proposal in proposed),
    )
    return ProviderOutput(
        answers=answers,
        input_tokens=sum(output.input_tokens for output in outputs),
        output_tokens=sum(output.output_tokens for output in outputs),
        cost_usd=None if any(cost is None for cost in costs) else sum(c or 0.0 for c in costs),
        extra={**outputs[0].extra, "repeat": {"runs": runs, "answered": len(outputs)}},
        failures={key: code for output in outputs for key, code in output.failures.items()},
        actions=proposed,
        # One text, not a blend: the first run's that answered. What is graded is what is kept.
        outputs=dict(outputs[0].outputs),
        signals=Signals(
            caps=(*found.caps, *caps), checked=(*found.checked, checked), reported=reported
        ),
    )
