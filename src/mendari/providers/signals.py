# SPDX-License-Identifier: Apache-2.0
"""What a provider reports beside its answers that can only lower a decision's automation
(ADR-0022).

Three things do, and each is found while a slot answers:

- **the agent changed since its confidence was calibrated** (`providers/agent_calibration.py`):
  the service reports a version other than the one its calibration was fitted for;
- **repeated runs disagreed** (`providers/repeat.py`): the same question, asked N times, did not
  get the same answer every time, or a run failed;
- **a second model disagreed** (`providers/check.py`): the variant that checks did not give the
  answer of the variant that answers.

Each is a `Cap`: a reason. The policy turns AUTO into HUMAN_REVIEW for any of them and changes
no other outcome (`policy.lowered`); nothing in a manifest switches one off, and none can raise
automation, because nothing here is read anywhere that could.

A disagreement caps only on a question the decision turns on (`deciding_questions`): the primary
question, a critical one (`evaluation.critical`), one a policy rule reads, and the questions any
of those is derived from. On another question it is written in the answer's evidence and
nothing else.

`Signals` also keeps, in memory only, what an evaluation reports from: each second signal's
count of calls and of disagreements (`Checked`), and each calibrated answer's distribution as
the provider reported it (`reported`), so the run can state its calibration before and after
(`evals/confidence.py`). None of it is stored, hashed or signed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from mendari.core.spec import PackManifest

CapKind = Literal["calibration", "repeat", "check"]
SecondSignal = Literal["repeat", "check"]
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
    (`extra_calls`), the deciding questions it disagreed on, and whether the answers were
    canned, in which case nothing was checked and no agreement is claimed."""

    slot: str
    variant: str
    kind: SecondSignal
    runs: int
    extra_calls: int = 0
    disagreed: tuple[str, ...] = ()
    canned: bool = False


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
        """The caps' reasons, each once, in the order found: all of them, or those of `kinds`."""
        wanted = None if kinds is None else frozenset(kinds)
        return tuple(
            dict.fromkeys(cap.reason for cap in self.caps if wanted is None or cap.kind in wanted)
        )


def merged(found: Iterable[Signals]) -> Signals:
    out = Signals()
    for signals in found:
        out = out.merge(signals)
    return out


def deciding_questions(manifest: PackManifest) -> frozenset[str]:
    """The questions a decision turns on: the primary question, each critical question
    (`evaluation.critical`), each question a policy rule reads, and the questions any of those
    is derived from. A disagreement on one of them caps automation; on another it does not."""
    named = {manifest.policy.primary}
    named |= {critical.question for critical in manifest.evaluation.critical}
    named |= {rule.when.question for rule in manifest.policy.rules}
    for key in list(named):
        question = manifest.questions.get(key)
        if question is not None and question.derive is not None:
            named |= set(question.derive.members)
    return frozenset(named)
