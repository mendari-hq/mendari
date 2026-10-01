"""Composite providers: several member providers answer the same slot, combined by a rule.

- `any_of`: the most alarming answer wins (safety screens).
- `cascade`: members in order; the first confident answer is accepted (cost control).
- `vote`: a weighted average of the members' distributions (ensembles).

Members are ordinary provider variants declared inline under `members`; they cannot be
composites themselves. Budgets and batching reach every member. A member that fails is
recorded as a failure of the slot's questions (`<member>:<code>`), so the policy never
automates a decision that one of its providers could not complete.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mendari.errors import PackValidationError, ProviderError
from mendari.providers.base import Answer, Provider, ProviderInfo, ProviderOutput


class CompositeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: str
    members: dict[str, dict[str, Any]] = Field(min_length=2)


class CompositeProvider(Provider):
    settings_model: ClassVar[type[CompositeConfig]] = CompositeConfig
    joiner: ClassVar[str] = " + "  # how the members' models are listed in provenance

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        from mendari.providers import PROVIDER_TYPES  # the registry imports this module

        try:
            self.settings = self.settings_model.model_validate(self.config)
        except ValidationError as error:
            raise PackValidationError(self.pack.id, [f"{self.info.slot}: {error}"]) from None
        problems = self.check()
        for name, member in self.settings.members.items():
            member_type = PROVIDER_TYPES.get(str(member.get("type")))
            if member_type is None or issubclass(member_type, CompositeProvider):
                problems.append(
                    f"{self.info.slot}.{name}: unknown member type {member.get('type')!r}"
                )
        if problems:
            raise PackValidationError(self.pack.id, problems)

        self.members: dict[str, Provider] = {}
        for name, member in self.settings.members.items():
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
            model=self.joiner.join(
                f"{n}:{m.info.model or m.type}" for n, m in self.members.items()
            ),
        )

    def check(self) -> list[str]:
        """Type-specific validation of `self.settings`; returns problems."""
        return []

    def use_budget(self, acquire: Callable[[str], None]) -> None:
        super().use_budget(acquire)
        for member in self.members.values():
            member.use_budget(acquire)

    def leaves(self) -> Iterator[Provider]:
        for member in self.members.values():
            yield from member.leaves()

    def ask(self, name: str, inputs: Mapping[str, Any]) -> ProviderOutput:
        member = self.members[name]
        member.preflight()
        return member.answer(inputs)

    def combined(self, member: str, answer: Answer, evidence: tuple[str, ...]) -> Answer:
        """An answer attributed to this slot, recording which member it came from."""
        return replace(
            answer,
            provider=replace(self.info, model=f"{member}:{answer.provider.model}"),
            evidence=evidence,
        )

    def output(
        self,
        answers: dict[str, Answer],
        outputs: Mapping[str, ProviderOutput],
        failed: Mapping[str, str],
    ) -> ProviderOutput:
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


def all_failed(failed: Mapping[str, str]) -> ProviderError:
    return ProviderError(next(iter(failed.values())), "every member failed")
