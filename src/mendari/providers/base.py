# SPDX-License-Identifier: Apache-2.0
"""The provider contract: answer a pack's questions with a probability distribution per question."""

from __future__ import annotations

import hashlib
import math
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Hashable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError

from mendari.core.actions import Proposed
from mendari.core.pack import Pack
from mendari.core.spec import Question
from mendari.core.text import plain
from mendari.errors import PackValidationError, ProviderError
from mendari.providers.signals import Signals

T = TypeVar("T")


class FileConfig(BaseModel):
    """A `rules`, `criteria` or `dosing` variant: its type and the file of the agent its config
    is in, and nothing else (`Provider.config_problems`)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: str
    config: str


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
    # Why the answers are canned rather than a live call's (an agent's `mock_file`, read while
    # `MENDARI_AGENT_URL` is unset), or None. A server with sign-in never automates on them.
    canned: str | None = None
    # The actions an agent proposed with its answers (spec 0.4): asked for, never run. Only an
    # `agent` variant of a 0.4 agent proposes any.
    actions: tuple[Proposed, ...] = ()
    # What can only lower the decision's automation (`providers/signals.py`): the agent changed
    # since its confidence was calibrated, or a second signal disagreed.
    signals: Signals = field(default_factory=Signals)


# A decision over its organization's daily quota (`CallBudget.admit_decision`): its remote
# calls fail with it before anything is fetched or called, so local providers still answer and
# composites keep their local members; the policy then holds the decision.
OVER_QUOTA: ContextVar[ProviderError | None] = ContextVar("over_quota", default=None)


def refuse_if_over_quota() -> None:
    """Raise the decision's quota error (remote providers call this first)."""
    refused = OVER_QUOTA.get()
    if refused is not None:
        raise refused


# The most one call may report (the usage ledger, LP-21g): above, a figure is not believed. A
# provider's own parser bounds what it accepts (Jev's MAX_TOKENS, the LLM's); these keep a
# figure that got through from ever failing a decision's record.
MAX_CALL_TOKENS = 10_000_000
MAX_CALL_COST_USD = 1_000.0


@dataclass
class Metered:
    """One provider type's remote calls: how many were sent (`sending`), how many answered with
    a price (`priced`), the tokens the answered ones reported, and the cost of the priced ones.
    A call that failed, whose answer was refused, or whose provider has no price, is counted but
    not priced, and adds no tokens: its cost is unknown, never zero (the usage ledger,
    LP-21g)."""

    calls: int = 0
    priced: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, other: Metered) -> None:
        self.calls += other.calls
        self.priced += other.priced
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cost_usd += other.cost_usd


class Meter:
    """A decision's remote calls by provider type, as its providers make them (`sending`, then
    `metered` once the call answers). Safe across threads; providers never write anywhere:
    the decision's record takes it (`AuditStore.record`)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._types: dict[str, Metered] = {}

    def call(self, provider_type: str) -> None:
        with self._lock:
            self._types.setdefault(provider_type, Metered()).calls += 1

    def answered(
        self, provider_type: str, input_tokens: int, output_tokens: int, cost_usd: float | None
    ) -> None:
        if not all(0 <= tokens <= MAX_CALL_TOKENS for tokens in (input_tokens, output_tokens)):
            input_tokens = output_tokens = 0  # not believed: reported as none
        if cost_usd is not None and not (
            math.isfinite(cost_usd) and 0 <= cost_usd <= MAX_CALL_COST_USD
        ):
            cost_usd = None  # not believed: unpriced
        with self._lock:
            metered = self._types.setdefault(provider_type, Metered())
            metered.input_tokens += input_tokens
            metered.output_tokens += output_tokens
            if cost_usd is not None:
                metered.priced += 1
                metered.cost_usd += cost_usd

    def totals(self) -> dict[str, Metered]:
        with self._lock:
            return {kind: Metered(**asdict(m)) for kind, m in sorted(self._types.items())}


# The decision being made's meter (`Decision.__call__`): None outside one, where calls (a
# calibration's, say) are counted nowhere.
METER: ContextVar[Meter | None] = ContextVar("meter", default=None)


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

    @classmethod
    def config_problems(
        cls,
        pack: Pack,
        where: str,
        config: Mapping[str, Any],
        questions: Mapping[str, Question],
        *,
        bounded: bool,
        trained: bool,
    ) -> list[str]:
        """Why `config` (a variant's or a composite member's, with its `type`) cannot answer
        `questions` in `pack`, or nothing: every check that reads neither the environment nor
        the network, each problem naming `where` (the variant, or the member) or the pack file
        it is about. Its closed config model refuses any key it does not read. `bounded`: the
        pack is one the deployment does not own (`Pack.bounded`), held to more. `trained`: the
        trained files (`model_file`, `calibration`) are read too; `mendari train` loads a pack
        before it has them. `load_pack` runs it on every variant (`providers.config_problems`:
        a pack the deployment does not own is refused, an owned one warned) and so does
        `mendari validate`; each constructor raises on the checks a variant was refused for
        before (an owned pack's unread key only warns, so it still serves)."""
        return []

    def use_budget(self, acquire: Callable[[str], None]) -> None:
        """Metered providers call `charge()` before each billable call; the runtime wires the
        daily budget in here."""
        self._budget = acquire

    def charge(self) -> None:
        """Before each billable call, once it can be made: the daily budget, which may refuse
        it."""
        if self._budget is not None:
            self._budget(self.info.type)

    def sending(self) -> None:
        """Right before a charged call is sent: the decision's meter counts it. A call refused
        before (a budget, a quota, the deadline, a request too large) is never counted."""
        meter = METER.get()
        if meter is not None:
            meter.call(self.info.type)

    def metered(self, input_tokens: int, output_tokens: int, cost_usd: float | None) -> None:
        """Once a charged call answers: its tokens and its cost (None: not priced), whichever
        providers of a batch it answered for."""
        meter = METER.get()
        if meter is not None:
            meter.answered(self.info.type, input_tokens, output_tokens, cost_usd)

    def preflight(self) -> None:  # noqa: B027 - optional hook
        """Raise ProviderError if the provider cannot be called at all (e.g. not configured).

        Runs before any call is counted against a budget."""

    # Batching (see `providers.batch`): providers of one type with equal keys may share a call.
    max_batch_questions: ClassVar[int] = 0

    def leaves(self) -> Iterator[Provider]:
        """The providers that make calls: this one, or a composite's members."""
        yield self

    def eager_leaves(self) -> Iterator[Provider]:
        """Leaves guaranteed to run on every decision (used to group eager batch calls)."""
        yield from self.leaves()

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


def closed_problems(model: type[BaseModel], where: str, config: Mapping[str, Any]) -> list[str]:
    """Why `config` is not one its closed config `model` accepts (a key it does not read, a value
    out of range), or nothing."""
    try:
        model.model_validate(config)
    except ValidationError as error:
        return [f"{where}: {error}"]
    return []


def read_problems(read: Callable[[], Any]) -> tuple[Any, list[str]]:
    """(what `read` returns, []), or (None, the problems) when the pack file it reads is missing,
    unreadable, changed since load or malformed (`Pack.read_*` raise `PackValidationError`)."""
    try:
        return read(), []
    except PackValidationError as error:
        return None, list(error.problems)


# What one `providers.config_problems` call has already worked out about a pack file, by key: a
# pack's composites often name the same trained model, which is then read and checked once.
# None outside such a call, so nothing outlives it (a file edited later is read again, and
# refused).
_CHECKED: ContextVar[dict[Hashable, Any] | None] = ContextVar("checked", default=None)


@contextmanager
def checking_once() -> Iterator[None]:
    """Within this block, `once` computes each key's value a single time."""
    token = _CHECKED.set({})
    try:
        yield
    finally:
        _CHECKED.reset(token)


def once(key: Hashable, compute: Callable[[], T]) -> T:
    """`compute()`, computed once per key inside `checking_once`, every time outside it."""
    checked = _CHECKED.get()
    if checked is None:
        return compute()
    if key not in checked:
        checked[key] = compute()
    value: T = checked[key]
    return value


def state_fields_problem(kind: str) -> str:
    """Why a model variant of a pack the deployment does not own is refused without
    `state_fields`: it would send the whole input."""
    return (
        f"a community or registry agent's {kind} variant must name its state_fields: the input "
        "fields it sends"
    )


def prefixed(where: str, problems: list[str]) -> list[str]:
    """`problems`, each naming `where` (the variant or member they are about) first: one about a
    pack file names the file only."""
    return [
        problem if problem.startswith((f"{where}: ", f"{where}.")) else f"{where}: {problem}"
        for problem in problems
    ]


def fingerprint(pack: Pack, relative: str) -> str:
    """Short content hash of a pack file, recorded as the provider's model version."""
    return hashlib.sha256(pack.read_bytes(relative)).hexdigest()[:12]


def softmax(scores: Mapping[str, float], temperature: float = 1.0) -> dict[str, float]:
    if not math.isfinite(temperature) or temperature <= 0:
        raise ValueError("temperature must be positive and finite")
    if not scores:
        raise ValueError("scores must not be empty")
    if not all(math.isfinite(score) for score in scores.values()):
        raise ValueError("scores must be finite")
    top = max(scores.values())
    exps = {label: math.exp((score - top) / temperature) for label, score in scores.items()}
    total = sum(exps.values())
    return {label: value / total for label, value in exps.items()}


def scored(scores: Mapping[str, float], temperature: float) -> dict[str, float]:
    """The softmax of a local model's scores, or ProviderError when they are not finite: finite
    weights can still add up past a float (two of 1e308), and the decision then fails closed
    for that slot instead of raising out of it."""
    if not all(math.isfinite(score) for score in scores.values()):
        raise ProviderError("invalid_response", "the model's scores are not finite numbers")
    return softmax(scores, temperature)


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
    """The text of the named (dotted) fields, one to a line, each in its plain form
    (`core/text.py`): what a provider that decides on this machine reads. A field that is not
    text, or holds nothing to read, is left out."""
    parts = [plain(part) for name in fields if isinstance(part := get_path(inputs, name), str)]
    return "\n".join(part for part in parts if part.strip())
