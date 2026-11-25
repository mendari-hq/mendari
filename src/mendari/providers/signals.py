# SPDX-License-Identifier: Apache-2.0
"""What a provider reports beside its answers that can only lower a decision's automation
(ADR-0024).

Three things do, and each is found while a slot answers:

- **the agent changed since its confidence was calibrated** (`providers/agent_calibration.py`):
  the service reports a version other than the one its calibration was fitted for;
- **repeated runs disagreed** (`providers/repeat.py`): the same question, asked N times, did not
  get the same answer every time, or a run failed;
- **a second model disagreed** (`providers/check.py`): the variant that checks did not give the
  answer of the variant that answers.

Each is a `Cap`: a reason. The policy turns AUTO into HUMAN_REVIEW for any of them and changes
no other outcome (`policy.lowered`); nothing in a manifest switches one off, and none can raise
automation, because nothing here is read anywhere that could. A decision's reasons give them in
the order of `KINDS`: the version cap first (how far the confidence can be believed), then
repeated runs, then a second model.

A disagreement caps only on a question the decision turns on (`deciding_questions`): the primary
question, a critical one (`evaluation.critical`) and one a policy rule reads. On another question
it is written in the answer's evidence and nothing else.

A second signal that did not agree on a proposed action (spec §13.7: only some of the repeated
runs proposed it, or the member that checks proposed another) caps too, whatever the action:
the reason names the action when the manifest declares it, and the proposal is carried
`disputed`, which no decision releases without a person (`policy.gate_of`).

`Signals` also keeps, in memory only, what an evaluation reports from: each second signal's
count of calls and of disagreements (`Checked`), and each calibrated answer's distribution as
the provider reported it (`reported`), so the run can state its calibration before and after
(`evals/confidence.py`). None of it is stored, hashed or signed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from mendari.core.spec import AgentManifest

CapKind = Literal["calibration", "repeat", "check"]
SecondSignal = Literal["repeat", "check"]
# The order a decision's reasons give the caps in: the version cap, then the second signal's.
KINDS: tuple[CapKind, ...] = ("calibration", "repeat", "check")
# The caps that are a second signal's (the other is the calibration's version cap).
SECOND_SIGNALS: tuple[CapKind, ...] = ("repeat", "check")


@dataclass(frozen=True)
class Cap:
    """Why a decision may not be automated, as its reasons will say it."""

    kind: CapKind
    reason: str


@dataclass(frozen=True)
class Checked:
    """One second signal on one slot of one decision: how many calls it meant to make for the
    decision (`runs`: N repeated runs, or 2 for a check), how many beyond the first it made
    (`extra_calls`), the deciding questions it disagreed on, how many proposed actions it did
    not agree on (`proposals`), and whether the answers were canned, in which case nothing was
    checked and no agreement is claimed."""

    slot: str
    variant: str
    kind: SecondSignal
    runs: int
    extra_calls: int = 0
    disagreed: tuple[str, ...] = ()
    canned: bool = False
    proposals: int = 0

    @property
    def differed(self) -> bool:
        """Whether it disagreed on anything that caps: a deciding question, or a proposal."""
        return bool(self.disagreed or self.proposals)


@dataclass(frozen=True)
class Signals:
    caps: tuple[Cap, ...] = ()
    checked: tuple[Checked, ...] = ()
    # Question -> the distribution the provider reported, for an answer a calibration rescaled.
    reported: Mapping[str, Mapping[str, float]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.caps or self.checked or self.reported)

    def merge(self, other: Signals) -> Signals:
        """Both, each cap once, in the order found."""
        if not other:
            return self
        if not self:
            return other
        return Signals(
            caps=tuple(dict.fromkeys((*self.caps, *other.caps))),
            checked=(*self.checked, *other.checked),
            reported={**self.reported, **other.reported},
        )

    def reasons(self, kinds: Iterable[CapKind] | None = None) -> tuple[str, ...]:
        """The caps' reasons, each once: all of them, or those of `kinds`. In the order of
        `KINDS` (the version cap, repeated runs, a second model), and within a kind in the
        order found."""
        wanted = frozenset(KINDS if kinds is None else kinds)
        return tuple(
            dict.fromkeys(
                cap.reason
                for kind in KINDS
                if kind in wanted
                for cap in self.caps
                if cap.kind == kind
            )
        )


def merged(found: Iterable[Signals]) -> Signals:
    out = Signals()
    for signals in found:
        out = out.merge(signals)
    return out


def proposed_what(manifest: AgentManifest, name: str) -> str:
    """A proposed action as a cap's reason names it: by its name only when the manifest declares
    it. Any other name is the agent's text, and no reason ever holds an argument."""
    declared = name in manifest.actions
    return f"action {name!r}" if declared else "an action the manifest does not declare"


def deciding_questions(manifest: AgentManifest) -> frozenset[str]:
    """The questions a decision turns on: the primary question, each critical question
    (`evaluation.critical`) and each question a policy rule reads. A disagreement on one of them
    caps automation; on another it does not."""
    named = {manifest.policy.primary}
    named |= {critical.question for critical in manifest.evaluation.critical}
    named |= {rule.when.question for rule in manifest.policy.rules}
    return frozenset(named)
