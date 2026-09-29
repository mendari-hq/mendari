"""AI Guard: check an LLM's draft reply before it reaches a patient.

A pack with a `guard` section (spec 0.2) names the input field holding the draft and the
recommendations that release it. `check` decides as usual (audited like any decision) and
turns the decision into what the calling application needs:

- `released`: AUTO with a release recommendation; `reply` is the draft, to send as is;
- `held_for_review`: HUMAN_REVIEW or ABSTAIN; a clinician sees the draft and the reasons;
- `blocked`: BLOCK; the draft must not be sent, and `fallback` says what to do instead.

At risk level 2 or above, AUTO (and so `released`) needs a recorded governance override.

    result = guard.check(decision, {"question": question, "draft": draft})
    if result.status == "released":
        send(result.reply)
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from openhealth.core.decision import Decision, DecisionResult
from openhealth.core.spec import Outcome
from openhealth.errors import OpenHealthError
from openhealth.providers.base import get_path

Status = Literal["released", "held_for_review", "blocked"]


@dataclass(frozen=True)
class GuardResult:
    status: Status
    reply: str | None
    decision: DecisionResult

    def to_dict(self) -> dict[str, Any]:
        d = self.decision
        return {
            "status": self.status,
            "reply": self.reply,
            "outcome": d.outcome.value,
            "recommendation": d.recommendation,
            "reasons": list(d.reasons),
            "fallback": d.fallback,
            "decision_id": d.id,
        }


def check(decision: Decision, inputs: Mapping[str, Any]) -> GuardResult:
    config = decision.pack.manifest.guard
    if config is None:
        raise OpenHealthError(f"pack {decision.pack.id} has no `guard` section")
    result = decision.decide(inputs)
    if result.outcome is Outcome.BLOCK:
        return GuardResult("blocked", None, result)
    draft = get_path(inputs, config.draft)
    releasable = result.outcome is Outcome.AUTO and result.recommendation in config.release
    if releasable and isinstance(draft, str):
        return GuardResult("released", draft, result)
    return GuardResult("held_for_review", None, result)
