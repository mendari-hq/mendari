"""Composite provider: several independent providers answer the same questions, and the most
alarming answer wins.

Built for safety screens, where the costly error is a miss: a deterministic rules screen that
works offline, plus a stronger model that catches phrasings no keyword list anticipates. For
each question, the member giving the highest probability to the `escalate` label decides.

If a member fails, the others still answer, but the failure is reported with the answer, so the
policy never automates a decision whose safety screen ran incomplete.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from openhealth.errors import PackValidationError, ProviderError
from openhealth.providers.base import Answer, Provider, ProviderInfo, ProviderOutput


class AnyOfConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: str
    escalate: dict[str, str] = Field(min_length=1)  # question -> the label that must not be missed
    members: dict[str, dict[str, Any]] = Field(min_length=2)


class AnyOfProvider(Provider):
    type = "any_of"

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        from openhealth.providers import PROVIDER_TYPES  # the registry imports this module

        try:
            settings = AnyOfConfig.model_validate(self.config)
        except ValidationError as error:
            raise PackValidationError(self.pack.id, [f"{self.info.slot}: {error}"]) from None
        problems = []
        if set(settings.escalate) != set(self.questions):
            problems.append(f"{self.info.slot}: escalate must name exactly the slot's questions")
        for question, label in settings.escalate.items():
            if question in self.questions and label not in self.questions[question].criteria:
                problems.append(f"{self.info.slot}: {label!r} is not a label of {question!r}")
        for name, member in settings.members.items():
            if member.get("type") not in PROVIDER_TYPES or member.get("type") == self.type:
                problems.append(
                    f"{self.info.slot}.{name}: unknown member type {member.get('type')!r}"
                )
        if problems:
            raise PackValidationError(self.pack.id, problems)

        self.escalate = settings.escalate
        self.members: dict[str, Provider] = {}
        for name, member in settings.members.items():
            model = member.get("model") if isinstance(member.get("model"), str) else None
            info = ProviderInfo(
                slot=self.info.slot,
                variant=f"{self.info.variant}.{name}",
                type=member["type"],
                model=model,
            )
            self.members[name] = PROVIDER_TYPES[member["type"]](
                self.pack, info, self.questions, member
            )
        self.remote = any(member.remote for member in self.members.values())
        self.info = replace(
            self.info,
            model=" + ".join(f"{n}:{m.info.model or m.type}" for n, m in self.members.items()),
        )

    def use_budget(self, acquire: Callable[[str], None]) -> None:
        super().use_budget(acquire)
        for member in self.members.values():
            member.use_budget(acquire)

    def answer(self, inputs: Mapping[str, Any]) -> ProviderOutput:
        outputs: dict[str, ProviderOutput] = {}
        failed: dict[str, str] = {}
        for name, member in self.members.items():
            try:
                member.preflight()
                outputs[name] = member.answer(inputs)
            except ProviderError as error:
                failed[name] = error.code
        if not outputs:
            raise ProviderError(next(iter(failed.values())), "every member failed")

        answers = {}
        for question, label in self.escalate.items():
            candidates = [(name, output.answers[question]) for name, output in outputs.items()]
            name, chosen = max(candidates, key=lambda item: item[1].probabilities.get(label, 0.0))
            evidence = tuple(
                f"{n}:p({label})={a.probabilities.get(label, 0.0):.2f}" for n, a in candidates
            ) + tuple(f"{n}:{item}" for n, a in candidates for item in a.evidence)
            answers[question] = Answer(
                question=question,
                label=chosen.label,
                confidence=chosen.confidence,
                probabilities=chosen.probabilities,
                provider=replace(self.info, model=f"{name}:{chosen.provider.model}"),
                evidence=evidence,
            )

        costs = [output.cost_usd for output in outputs.values()]
        return ProviderOutput(
            answers=answers,
            input_tokens=sum(output.input_tokens for output in outputs.values()),
            output_tokens=sum(output.output_tokens for output in outputs.values()),
            cost_usd=None if any(cost is None for cost in costs) else sum(c or 0.0 for c in costs),
            failures={
                question: ", ".join(f"{name}:{code}" for name, code in failed.items())
                for question in self.questions
            }
            if failed
            else {},
        )
