# SPDX-License-Identifier: Apache-2.0
"""Check: answer with one variant, check with another, and never automate when they disagree
(ADR-0024; spec §14.4).

    checked:
      type: check
      members:
        agent: {type: agent, ...}         # the first member answers
        second: {type: llm, model: ...}   # the second checks

The first member's answers are the slot's, as it gave them: its labels, its confidence, its
distribution. The second member is asked the same questions about the same input, and only its
labels are read:

- where it gives the first member's answer, nothing changes: agreement raises no confidence
  and lifts no cap;
- where it gives another answer to a question the decision turns on
  (`signals.deciding_questions`), the decision is capped: AUTO becomes HUMAN_REVIEW, the reason
  `<first> and <second> disagree on <question> (<first>: <label>, <second>: <label>)`. On
  another question the disagreement is in the evidence only.

A composite (`providers/composite.py`), so budgets, batching, connections, the deadline, cost
and usage reach both members. A second member that fails is recorded as a composite records a
member's failure (`<member>:<code>` on each question), which the policy never automates over.
A first member that fails fails the slot: the member that checks never answers in its place.

The actions the first member proposes (spec §14.4) are the slot's, as its answers are; the second
member's own are never taken, since it never answers in the first's place. Where the second
member is one that proposes actions too (an `agent` variant), the two are held
against each other (`_proposals`): a proposal of the first that the second did not make, with
the same arguments, is carried `disputed`, so it is held for the decision's reviewer and never
released without one, whatever its effect (`policy.gate_of`); and any difference between the two
caps the decision, the reason `<first> proposed action <name> and <second> did not` (or the
other way round, or `... with different arguments`). A second member that failed checked
nothing, so every proposal of the first is disputed. A second member that cannot propose (an
`llm`, say) checks the answers and not the actions: the first's proposals stand as proposed, and
the gate of each applies as it always does.

The two members should not be the same model behind the same prompt: what is wanted is a
second reader with other mistakes. A member that answers from canned examples proves nothing
by agreeing, and the evidence says so (`Checked.canned`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from pydantic import Field

from mendari.core import actions as proposals
from mendari.errors import ProviderError, ProviderErrorCode
from mendari.providers.base import ProviderOutput
from mendari.providers.composite import CompositeConfig, CompositeProvider
from mendari.providers.signals import (
    Cap,
    Checked,
    Signals,
    deciding_questions,
    proposed_what,
)

CANNED_NOTE = "check: canned answers, so agreement is not evidence"


class CheckConfig(CompositeConfig):
    # Exactly two, in order: the member that answers, then the member that checks.
    members: dict[str, dict[str, Any]] = Field(min_length=2, max_length=2)


def disagree_reason(first: str, second: str, question: str, answered: str, checked: str) -> str:
    """The cap's reason: the two members by name, the question, and what each answered."""
    return f"{first} and {second} disagree on {question} ({first}: {answered}, {second}: {checked})"


class CheckProvider(CompositeProvider):
    type = "check"
    settings_model = CheckConfig

    def answer(self, inputs: Mapping[str, Any]) -> ProviderOutput:
        first, second = self.members
        outputs: dict[str, ProviderOutput] = {first: self.ask(first, inputs)}
        failed: dict[str, ProviderErrorCode] = {}
        try:
            outputs[second] = self.ask(second, inputs)
        except ProviderError as error:
            failed[second] = error.code
        answered, checking = outputs[first], outputs.get(second)
        canned = any(output.canned is not None for output in outputs.values())
        deciding = deciding_questions(self.agent.manifest)
        caps: list[Cap] = []
        disagreed: list[str] = []
        answers = {}
        for key, answer in answered.answers.items():
            if checking is None:
                note = f"check: {second} failed ({failed[second]})"
            elif checking.answers[key].label == answer.label:
                note = CANNED_NOTE if canned else f"check: {second} agrees"
            else:
                other = checking.answers[key].label
                note = f"check: {second} answered {other}"
                if key in deciding:
                    caps.append(
                        Cap("check", disagree_reason(first, second, key, answer.label, other))
                    )
                    disagreed.append(key)
            evidence = (note, *(f"{first}:{item}" for item in answer.evidence))
            answers[key] = self.combined(first, answer, evidence)
        proposed, differed = self._proposals(first, second, answered, checking)
        caps += [Cap("check", reason) for reason in differed]
        found = answered.signals
        if checking is not None:  # its own caps hold too (its calibration's version, say)
            found = found.merge(
                Signals(caps=checking.signals.caps, checked=checking.signals.checked)
            )
        info = self.info
        own = Checked(
            info.slot,
            info.variant,
            "check",
            2,
            1,
            tuple(disagreed),
            canned=canned,
            proposals=len(differed),
        )
        signals = Signals(
            caps=(*found.caps, *caps), checked=(*found.checked, own), reported=found.reported
        )
        return self.output(answers, outputs, failed, signals, proposed)

    def _proposals(
        self, first: str, second: str, answered: ProviderOutput, checking: ProviderOutput | None
    ) -> tuple[tuple[proposals.Proposed, ...], list[str]]:
        """The first member's proposed actions, which are the slot's, and why the two members
        did not agree on them: see the module docstring. A reason for each action's name they
        differed on, the first member's first."""
        mine = proposals.distinct(answered.actions)
        if not self.members[second].proposes:
            return tuple(mine), []  # the second member checks answers, not actions
        manifest = self.agent.manifest
        if checking is None:
            disputed = tuple(replace(proposal, disputed=True) for proposal in mine)
            said = f"{second} failed, so what {first} proposed to do was not checked"
            return disputed, [said] if disputed else []
        theirs = proposals.distinct(checking.actions)
        made = {proposals.key(proposal) for proposal in theirs}
        taken = tuple(
            proposal if proposals.key(proposal) in made else replace(proposal, disputed=True)
            for proposal in mine
        )
        kept = {proposals.key(proposal) for proposal in mine}
        differing = [proposal.name for proposal in taken if proposal.disputed]
        differing += [proposal.name for proposal in theirs if proposals.key(proposal) not in kept]
        by_first = {proposal.name for proposal in mine}
        by_second = {proposal.name for proposal in theirs}
        reasons = []
        for name in dict.fromkeys(differing):
            what = proposed_what(manifest, name)
            if name in by_first and name in by_second:
                reasons.append(f"{first} and {second} proposed {what} with different arguments")
            elif name in by_first:
                reasons.append(f"{first} proposed {what} and {second} did not")
            else:
                reasons.append(f"{second} proposed {what} and {first} did not")
        return taken, list(dict.fromkeys(reasons))
