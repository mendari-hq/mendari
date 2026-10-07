# SPDX-License-Identifier: Apache-2.0
"""Vote: a weighted average of the members' probability distributions (soft voting).

    ensemble:
      type: vote
      weights: {nb: 1, jev: 2}   # optional; equal weights by default
      members:
        nb: {type: naive_bayes, ...}
        jev: {type: jev, ...}

Averaging distributions from providers with different error patterns is usually better
calibrated than any one of them. A member that fails is left out of the average (the weights
of the others are renormalised) and recorded as a failure, so the decision is not automated.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any, cast

from mendari.errors import ProviderError, ProviderErrorCode
from mendari.providers.base import Answer, ProviderInfo, ProviderOutput
from mendari.providers.composite import CompositeConfig, CompositeProvider, all_failed


class VoteConfig(CompositeConfig):
    weights: dict[str, float] | None = None


class VoteProvider(CompositeProvider):
    type = "vote"
    settings_model = VoteConfig

    def check(self) -> list[str]:
        settings = cast(VoteConfig, self.settings)
        weights = settings.weights
        if weights is None:
            return []
        problems = []
        if set(weights) != set(settings.members):
            problems.append(f"{self.info.slot}: weights must name exactly the members")
        if any(not w > 0 for w in weights.values()):
            problems.append(f"{self.info.slot}: weights must be positive")
        return problems

    def answer(self, inputs: Mapping[str, Any]) -> ProviderOutput:
        weights = cast(VoteConfig, self.settings).weights or {}
        outputs: dict[str, ProviderOutput] = {}
        failed: dict[str, ProviderErrorCode] = {}
        for name in self.members:
            try:
                outputs[name] = self.ask(name, inputs)
            except ProviderError as error:
                failed[name] = error.code
        if not outputs:
            raise all_failed(failed)

        total = sum(weights.get(name, 1.0) for name in outputs)
        answers = {}
        for question, spec in self.questions.items():
            probabilities = {
                label: sum(
                    weights.get(name, 1.0) * output.answers[question].probabilities.get(label, 0.0)
                    for name, output in outputs.items()
                )
                / total
                for label in spec.labels
            }
            label = max(probabilities, key=lambda item: probabilities[item])
            evidence = tuple(
                f"{name}:{output.answers[question].label} "
                f"({output.answers[question].confidence:.2f}, w={weights.get(name, 1.0):g})"
                for name, output in outputs.items()
            )
            answers[question] = Answer(
                question=question,
                label=label,
                confidence=probabilities[label],
                probabilities=probabilities,
                provider=self._answered_by(question, outputs),
                evidence=evidence,
            )
        return self.output(answers, outputs, failed)

    def _answered_by(self, question: str, outputs: Mapping[str, ProviderOutput]) -> ProviderInfo:
        """This slot, naming the members that answered `question`, each with the model its
        answer records (the one a remote member reported); a member that failed is in the
        failures, not here."""
        parts = []
        for name, output in outputs.items():
            model = output.answers[question].provider.model or self.members[name].type
            parts.append(f"{name}:{model}")
        return replace(self.info, model=self.joiner.join(parts))
