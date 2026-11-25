# SPDX-License-Identifier: Apache-2.0
"""Composite providers: several member providers answer the same slot, combined by a rule.
The one composite is `check` (`providers/check.py`): its first member answers, its second
checks, and a disagreement holds the decision for a person.

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

from mendari.core.actions import Proposed
from mendari.core.pack import Pack
from mendari.core.spec import Question
from mendari.errors import PackValidationError
from mendari.providers.base import Answer, Provider, ProviderInfo, ProviderOutput, prefixed
from mendari.providers.signals import Signals, merged


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

        # Its own settings here; each member's are checked as it is built, below.
        settings, problems = self._settings(self.info.slot, self.config, self.questions)
        if settings is None or problems:
            raise PackValidationError(self.pack.id, problems)
        self.settings = settings

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

    @classmethod
    def config_problems(
        cls,
        pack: Pack,
        where: str,
        config: Mapping[str, Any],
        questions: Mapping[str, Question],
        *,
        bounded: bool,
    ) -> list[str]:
        """Its own settings, then each member's (`where.members.<name>`), as each member's
        provider checks them."""
        from mendari.providers import PROVIDER_TYPES  # the registry imports this module

        settings, problems = cls._settings(where, config, questions)
        if settings is None or problems:
            return problems
        for name, member in settings.members.items():
            at = f"{where}.members.{name}"
            found = PROVIDER_TYPES[str(member["type"])].config_problems(
                pack, at, member, questions, bounded=bounded
            )
            problems += prefixed(at, found)
        return problems

    @classmethod
    def _settings(
        cls, where: str, config: Mapping[str, Any], questions: Mapping[str, Question]
    ) -> tuple[CompositeConfig | None, list[str]]:
        """The composite's own settings (its closed config, `settings_model`), checked (`check`),
        with a known type that is not a composite for each member; or None and why not."""
        from mendari.providers import PROVIDER_TYPES  # the registry imports this module

        try:
            settings = cls.settings_model.model_validate(config)
        except ValidationError as error:
            return None, [f"{where}: {error}"]
        problems = cls.check(where, settings, questions)
        for name, member in settings.members.items():
            member_type = PROVIDER_TYPES.get(str(member.get("type")))
            if member_type is None or issubclass(member_type, CompositeProvider):
                problems.append(f"{where}.{name}: unknown member type {member.get('type')!r}")
        return settings, problems

    @classmethod
    def check(
        cls, where: str, settings: CompositeConfig, questions: Mapping[str, Question]
    ) -> list[str]:
        """Type-specific validation of `settings` (`where`: the variant, or the slot, a problem
        names); returns problems."""
        return []

    def use_budget(self, acquire: Callable[[str], None]) -> None:
        super().use_budget(acquire)
        for member in self.members.values():
            member.use_budget(acquire)

    def leaves(self) -> Iterator[Provider]:
        for member in self.members.values():
            yield from member.leaves()

    def eager_leaves(self) -> Iterator[Provider]:
        for member in self.members.values():
            yield from member.eager_leaves()

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
        signals: Signals | None = None,
        actions: tuple[Proposed, ...] | None = None,
    ) -> ProviderOutput:
        """`signals`: the composite's own (a check's); else every member's that answered, so a
        member's cap holds whichever member's answer is used. `actions`: the proposals the
        composite takes (a check's: its first member's); else none is taken."""
        costs = [output.cost_usd for output in outputs.values()]
        if actions is None:
            # A member's proposals are not taken (spec 0.4 section 4.2): each is carried
            # refused, so it is blocked and the decision goes to a person.
            actions = tuple(
                replace(action, refused="composite_member")
                for output in outputs.values()
                for action in output.actions
            )
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
            canned=next((o.canned for o in outputs.values() if o.canned is not None), None),
            actions=actions,
            signals=merged(o.signals for o in outputs.values()) if signals is None else signals,
        )
