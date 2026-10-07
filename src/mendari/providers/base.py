# SPDX-License-Identifier: Apache-2.0
"""The provider contract: answer a pack's questions with a probability distribution per question."""

from __future__ import annotations

import hashlib
import math
from abc import ABC, abstractmethod
from collections.abc import Callable, Hashable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar

from mendari.core.pack import Pack
from mendari.core.spec import Question
from mendari.errors import ProviderError


@dataclass(frozen=True)
class ProviderInfo:
    slot: str
    variant: str
    type: str
    model: str | None = None


@dataclass(frozen=True)
class Answer:
    question: str
    label: str
    confidence: float
    probabilities: dict[str, float]
    provider: ProviderInfo
    evidence: tuple[str, ...] = ()
    # Score questions: the expected score (probability-weighted average of the levels).
    value: float | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "label": self.label,
            "confidence": round(self.confidence, 6),
            "probabilities": {k: round(v, 6) for k, v in self.probabilities.items()},
            "provider": asdict(self.provider),
            "evidence": list(self.evidence),
        }
        if self.value is not None:
            data["value"] = round(self.value, 6)
        return data


@dataclass(frozen=True)
class ProviderOutput:
    answers: dict[str, Answer]
    input_tokens: int = 0
    output_tokens: int = 0
    # None means "not known" (e.g. a remote provider without configured pricing), not free.
    cost_usd: float | None = 0.0
    extra: dict[str, Any] = field(default_factory=dict)
    # Questions answered despite a partial failure (e.g. one member of a composite screen was
    # down). The policy treats them like failures: no AUTO when a check ran incomplete.
    failures: dict[str, str] = field(default_factory=dict)


# A decision over its organization's daily quota (`CallBudget.admit_decision`): its remote
# calls fail with it before anything is fetched or called, so local providers still answer and
# composites keep their local members; the policy then holds the decision.
OVER_QUOTA: ContextVar[ProviderError | None] = ContextVar("over_quota", default=None)


def refuse_if_over_quota() -> None:
    """Raise the decision's quota error (remote providers call this first)."""
    refused = OVER_QUOTA.get()
    if refused is not None:
        raise refused


class Provider(ABC):
    type: ClassVar[str]
    # Remote providers make network calls: they are metered, rate-limited and parallelised.
    remote: bool = False

    def __init__(
        self,
        pack: Pack,
        info: ProviderInfo,
        questions: Mapping[str, Question],
        config: Mapping[str, Any],
    ):
        self.pack = pack
        self.info = info
        self.questions = dict(questions)
        self.config = dict(config)
        self._budget: Callable[[str], None] | None = None

    def use_budget(self, acquire: Callable[[str], None]) -> None:
        """Metered providers call `charge()` before each billable call; the runtime wires the
        daily budget in here."""
        self._budget = acquire

    def charge(self) -> None:
        if self._budget is not None:
            self._budget(self.info.type)

    def preflight(self) -> None:  # noqa: B027 - optional hook
        """Raise ProviderError if the provider cannot be called at all (e.g. not configured).

        Runs before any call is counted against a budget."""

    # Batching (see `providers.batch`): providers of one type with equal keys may share a call.
    max_batch_questions: ClassVar[int] = 0

    def leaves(self) -> Iterator[Provider]:
        """The providers that make calls: this one, or a composite's members."""
        yield self

    def batch_key(self) -> Hashable | None:
        """A key shared by providers that can be asked in one call, or None (never batched)."""
        return None

    def answer_batch(
        self, providers: list[Provider], inputs: Mapping[str, Any]
    ) -> list[ProviderOutput | ProviderError]:
        """Answer for `providers` (this one first) in one call: one output each, or an error in
        place of the output of a provider whose own answers were unusable."""
        raise NotImplementedError(f"{self.type} providers do not batch")

    @abstractmethod
    def answer(self, inputs: Mapping[str, Any]) -> ProviderOutput:
        """Answer every question in `self.questions`, or raise ProviderError."""

    def make_answer(
        self, question: str, probabilities: dict[str, float], evidence: tuple[str, ...] = ()
    ) -> Answer:
        label = max(probabilities, key=lambda key: probabilities[key])
        return Answer(
            question=question,
            label=label,
            confidence=probabilities[label],
            probabilities=probabilities,
            provider=self.info,
            evidence=evidence,
        )


def fingerprint(pack: Pack, relative: str) -> str:
    """Short content hash of a pack file, recorded as the provider's model version."""
    return hashlib.sha256(pack.read_bytes(relative)).hexdigest()[:12]


def softmax(scores: Mapping[str, float], temperature: float = 1.0) -> dict[str, float]:
    if temperature <= 0:
        raise ValueError("temperature must be positive")
    top = max(scores.values())
    exps = {label: math.exp((score - top) / temperature) for label, score in scores.items()}
    total = sum(exps.values())
    return {label: value / total for label, value in exps.items()}


def get_path(data: Any, dotted: str) -> Any:
    """Read `a.b.c` from nested mappings; missing keys yield None."""
    current = data
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def select_fields(inputs: Mapping[str, Any], fields: list[str]) -> dict[str, Any]:
    """Only the named (dotted) fields of `inputs`, nested as in the input: the minimum
    necessary that a remote provider receives."""
    state: dict[str, Any] = {}
    for dotted in fields:
        value = get_path(inputs, dotted)
        if value is None:
            continue
        *parents, leaf = dotted.split(".")
        node = state
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value
    return state


def batch_state(inputs: Mapping[str, Any], selections: list[list[str] | None]) -> dict[str, Any]:
    """What one (possibly batched) remote call sends: the union of the providers'
    `state_fields`, or the whole input if any of them sends everything."""
    if any(fields is None for fields in selections):
        return dict(inputs)
    return select_fields(
        inputs, list(dict.fromkeys(f for fields in selections for f in fields or []))
    )


def text_from(inputs: Mapping[str, Any], fields: list[str]) -> str:
    parts = [get_path(inputs, name) for name in fields]
    return "\n".join(str(part) for part in parts if isinstance(part, str) and part.strip())
