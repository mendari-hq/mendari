"""Composite provider: several independent providers answer the same questions, and the most
alarming answer wins.

Built for safety screens, where the costly error is a miss: a deterministic rules screen that
works offline, plus a stronger model that catches phrasings no keyword list anticipates. For
each question, the member giving the highest probability to the `escalate` label decides.

If a member fails, the others still answer, but the failure is reported with the answer, so the
policy never automates a decision whose safety screen ran incomplete.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from pydantic import Field

from openhealth.errors import ProviderError
from openhealth.providers.base import ProviderOutput
from openhealth.providers.composite import CompositeConfig, CompositeProvider, all_failed


class AnyOfConfig(CompositeConfig):
    escalate: dict[str, str] = Field(min_length=1)  # question -> the label that must not be missed


class AnyOfProvider(CompositeProvider):
    type = "any_of"
    settings_model = AnyOfConfig

    @property
    def escalate(self) -> dict[str, str]:
        return cast(AnyOfConfig, self.settings).escalate

    def check(self) -> list[str]:
        problems = []
        if set(self.escalate) != set(self.questions):
            problems.append(f"{self.info.slot}: escalate must name exactly the slot's questions")
        for question, label in self.escalate.items():
            if question in self.questions and label not in self.questions[question].criteria:
                problems.append(f"{self.info.slot}: {label!r} is not a label of {question!r}")
        return problems

    def answer(self, inputs: Mapping[str, Any]) -> ProviderOutput:
        outputs: dict[str, ProviderOutput] = {}
        failed: dict[str, str] = {}
        for name in self.members:
            try:
                outputs[name] = self.ask(name, inputs)
            except ProviderError as error:
                failed[name] = error.code
        if not outputs:
            raise all_failed(failed)

        answers = {}
        for question, label in self.escalate.items():
            candidates = [(name, output.answers[question]) for name, output in outputs.items()]
            name, chosen = max(candidates, key=lambda item: item[1].probabilities.get(label, 0.0))
            evidence = tuple(
                f"{n}:p({label})={a.probabilities.get(label, 0.0):.2f}" for n, a in candidates
            ) + tuple(f"{n}:{item}" for n, a in candidates for item in a.evidence)
            answers[question] = self.combined(name, chosen, evidence)
        return self.output(answers, outputs, failed)
