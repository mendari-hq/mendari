"""The provider contract: answer a pack's questions with a probability distribution per question."""

from __future__ import annotations

import hashlib
import math
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar

from openhealth.core.pack import Pack
from openhealth.core.spec import Question


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "confidence": round(self.confidence, 6),
            "probabilities": {k: round(v, 6) for k, v in self.probabilities.items()},
            "provider": asdict(self.provider),
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class ProviderOutput:
    answers: dict[str, Answer]
    input_tokens: int = 0
    output_tokens: int = 0
    # None means "not known" (e.g. a remote provider without configured pricing), not free.
    cost_usd: float | None = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


class Provider(ABC):
    type: ClassVar[str]
    # Remote providers make network calls: they are metered, rate-limited and parallelised.
    remote: ClassVar[bool] = False

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

    def preflight(self) -> None:  # noqa: B027 - optional hook
        """Raise ProviderError if the provider cannot be called at all (e.g. not configured).

        Runs before any call is counted against a budget."""

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
    return hashlib.sha256(pack.path(relative).read_bytes()).hexdigest()[:12]


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


def text_from(inputs: Mapping[str, Any], fields: list[str]) -> str:
    parts = [get_path(inputs, name) for name in fields]
    return "\n".join(str(part) for part in parts if isinstance(part, str) and part.strip())
