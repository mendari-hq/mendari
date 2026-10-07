# SPDX-License-Identifier: Apache-2.0
"""Cascade: members are tried in order, and the first confident answer is accepted.

Built for cost: a cheap local provider answers most cases, and an expensive or remote one is
called only when the cheap one is unsure, e.g. naive Bayes first, then Jev when naive Bayes is
below 0.95 on any of the slot's questions:

    cascade:
      type: cascade
      accept_at: 0.95
      members:
        nb: {type: naive_bayes, model_file: models/destination-nb.json, text_fields: [...]}
        jev: {type: jev, state_fields: [...]}

The last member's answer is always accepted. A member that fails passes the case to the next
one and is recorded as a failure (so the decision is not automated). If the last member fails
too, the most confident answer obtained so far is used; if every member fails, the slot fails.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, cast

from pydantic import Field

from mendari.errors import ProviderError
from mendari.providers.base import ProviderOutput
from mendari.providers.composite import CompositeConfig, CompositeProvider, all_failed


class CascadeConfig(CompositeConfig):
    accept_at: float = Field(gt=0.0, le=1.0)


class CascadeProvider(CompositeProvider):
    type = "cascade"
    settings_model = CascadeConfig
    joiner = " → "

    def answer(self, inputs: Mapping[str, Any]) -> ProviderOutput:
        accept_at = cast(CascadeConfig, self.settings).accept_at
        outputs: dict[str, ProviderOutput] = {}
        failed: dict[str, str] = {}
        trail: list[str] = []
        names = list(self.members)
        for position, name in enumerate(names):
            try:
                output = self.ask(name, inputs)
            except ProviderError as error:
                failed[name] = error.code
                trail.append(f"{name} failed ({error.code})")
                continue
            outputs[name] = output
            lowest = min(output.answers[q].confidence for q in self.questions)
            if lowest >= accept_at or position == len(names) - 1:
                return self._accept(name, outputs, failed, [*trail, f"{name} accepted"])
            # Floored, so a confidence just below the threshold never displays as reaching it.
            trail.append(f"{name} {math.floor(lowest * 1000) / 1000:.3f} < {accept_at:.3f}")
        if not outputs:
            raise all_failed(failed)
        best = max(
            outputs, key=lambda n: min(outputs[n].answers[q].confidence for q in self.questions)
        )
        return self._accept(best, outputs, failed, [*trail, f"{best} used (last member failed)"])

    def _accept(
        self,
        name: str,
        outputs: Mapping[str, ProviderOutput],
        failed: Mapping[str, str],
        trail: list[str],
    ) -> ProviderOutput:
        path = "cascade: " + "; ".join(trail)
        answers = {
            question: self.combined(name, answer, (path, *(f"{name}:{e}" for e in answer.evidence)))
            for question, answer in outputs[name].answers.items()
        }
        return self.output(answers, outputs, failed)
