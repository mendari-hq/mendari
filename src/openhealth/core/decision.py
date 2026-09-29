"""The runtime: validate input, ask providers, apply policy, emit an auditable result."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from openhealth.core.derive import complete_answers, explain
from openhealth.core.pack import Pack, load_pack
from openhealth.core.policy import GovernanceOverride, PolicyResult, apply_policy
from openhealth.core.registry import resolve_pack
from openhealth.core.spec import Outcome, Priority
from openhealth.errors import PackValidationError, ProviderError
from openhealth.providers import Answer, Provider, ProviderInfo, build_provider
from openhealth.providers.batch import batching

if TYPE_CHECKING:
    from openhealth.core.audit import AuditStore


@dataclass(frozen=True)
class DecisionResult:
    id: str
    created_at: str
    pack_id: str
    pack_version: str
    pack_digest: str
    primary: str
    outcome: Outcome
    recommendation: str | None
    priority: Priority
    answers: dict[str, Answer]
    reasons: tuple[str, ...]
    fallback: str | None
    errors: dict[str, str]
    rule: str | None
    latency_ms: float
    cost_usd: float | None
    input_tokens: int
    output_tokens: int
    input_hash: str
    governance: dict[str, Any] | None = None

    @property
    def confidence(self) -> float | None:
        answer = self.answers.get(self.primary)
        return answer.confidence if answer else None

    @property
    def needs_human(self) -> bool:
        return self.outcome in (Outcome.HUMAN_REVIEW, Outcome.ABSTAIN)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "pack": {"id": self.pack_id, "version": self.pack_version, "digest": self.pack_digest},
            "outcome": self.outcome.value,
            "recommendation": self.recommendation,
            "priority": self.priority.value,
            "confidence": None if self.confidence is None else round(self.confidence, 6),
            "primary": self.primary,
            "answers": {key: answer.to_dict() for key, answer in self.answers.items()},
            "reasons": list(self.reasons),
            "rule": self.rule,
            "fallback": self.fallback,
            "errors": dict(self.errors),
            "latency_ms": round(self.latency_ms, 3),
            "cost_usd": self.cost_usd,
            "usage": {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens},
            "input_hash": self.input_hash,
            "governance": self.governance,
        }


class CallBudget:
    """Daily call limits per provider type (e.g. {"jev": 500}; None counts calls without a
    limit), counted in the audit store.

    A provider over budget fails like any other provider (`budget_exhausted`), so the policy
    falls back safely instead of automating.
    """

    def __init__(self, store: AuditStore, limits: Mapping[str, int | None]):
        self.store = store
        self.limits = dict(limits)

    def acquire(self, provider_type: str) -> None:
        if provider_type in self.limits:
            self.store.consume(provider_type, daily_limit=self.limits[provider_type])


class Decision:
    """A loaded Decision Pack bound to concrete providers. Call it with a pack input."""

    def __init__(
        self,
        pack: Pack,
        *,
        providers: Mapping[str, str] | None = None,
        governance: GovernanceOverride | None = None,
        store: AuditStore | None = None,
        budget: CallBudget | None = None,
    ):
        overrides = dict(providers or {})
        unknown = set(overrides) - set(pack.manifest.providers)
        if unknown:
            raise PackValidationError(pack.id, [f"unknown provider slots {sorted(unknown)}"])
        self.pack = pack
        self.governance = governance
        self.store = store
        self.budget = budget
        self.providers: dict[str, Provider] = {
            slot: build_provider(pack, slot, overrides.get(slot))
            for slot in pack.manifest.providers
        }
        if budget is not None:
            for provider in self.providers.values():
                provider.use_budget(budget.acquire)
        self._validator = Draft202012Validator(
            pack.input_schema, format_checker=Draft202012Validator.FORMAT_CHECKER
        )

    @classmethod
    def from_hub(cls, ref: str, **kwargs: Any) -> Decision:
        """Resolve `namespace/name[@version]` (or a directory) from the local hub search path."""
        return cls(resolve_pack(ref), **kwargs)

    @classmethod
    def from_path(cls, path: str | Path, **kwargs: Any) -> Decision:
        return cls(load_pack(path), **kwargs)

    @property
    def is_remote(self) -> bool:
        return any(provider.remote for provider in self.providers.values())

    @property
    def provider_info(self) -> dict[str, ProviderInfo]:
        return {slot: provider.info for slot, provider in self.providers.items()}

    def __call__(self, inputs: Mapping[str, Any]) -> DecisionResult:
        return self.decide(inputs)

    def validate(self, inputs: Any) -> list[str]:
        errors = sorted(self._validator.iter_errors(inputs), key=lambda e: list(e.path))
        return [f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message}" for e in errors[:10]]

    def decide(self, inputs: Mapping[str, Any]) -> DecisionResult:
        started = time.perf_counter()
        manifest = self.pack.manifest
        answers: dict[str, Answer] = {}
        failures: dict[str, str] = {}
        tokens_in = tokens_out = 0
        cost: float | None = 0.0

        problems = self.validate(inputs)
        if problems:
            policy = PolicyResult(
                outcome=Outcome.BLOCK,
                recommendation=None,
                priority=manifest.policy.default_priority,
                reasons=tuple(f"input rejected: {problem}" for problem in problems),
            )
        else:
            with batching(self.providers.values()):
                for slot, provider in self.providers.items():
                    try:
                        provider.preflight()
                        output = provider.answer(inputs)
                    except ProviderError as error:
                        failed = dict.fromkeys(manifest.providers[slot].answers, error.code)
                        failures.update(failed)
                        continue
                    answers.update(output.answers)
                    failures.update(output.failures)
                    tokens_in += output.input_tokens
                    tokens_out += output.output_tokens
                    if cost is not None:
                        cost = None if output.cost_usd is None else cost + output.cost_usd
            complete_answers(manifest, answers, failures)
            variants = {slot: info.variant for slot, info in self.provider_info.items()}
            policy = apply_policy(manifest, answers, failures, self.governance, variants)
            primary = manifest.questions[manifest.policy.primary]
            if primary.derive is not None:
                policy = replace(policy, reasons=(*policy.reasons, *explain(primary, answers)))

        result = DecisionResult(
            id=str(uuid.uuid4()),
            created_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
            pack_id=self.pack.id,
            pack_version=self.pack.version,
            pack_digest=self.pack.digest,
            primary=manifest.policy.primary,
            outcome=policy.outcome,
            recommendation=policy.recommendation,
            priority=policy.priority,
            answers=answers,
            reasons=policy.reasons,
            fallback=manifest.policy.fallback if policy.outcome in _FALLBACK else None,
            errors=failures,
            rule=policy.rule,
            latency_ms=(time.perf_counter() - started) * 1000,
            cost_usd=cost,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            input_hash=input_hash(inputs),
            governance=self.governance.to_dict() if self.governance else None,
        )
        if self.store is not None:
            self.store.record(result, inputs, store_input=manifest.audit.store_input)
        return result

    def decide_fhir(
        self, resources: Mapping[str, Any], *, focus: Mapping[str, Any] | None = None
    ) -> tuple[DecisionResult, dict[str, Any]]:
        """Decide from FHIR R4 resources (a Bundle or {resourceType: resource}); return a Task.
        `focus` picks the resource to decide about, e.g. one of several draft orders."""
        from openhealth.standards.fhir import FhirAdapter

        adapter = FhirAdapter.for_pack(self.pack)
        indexed = adapter.index(resources, focus=focus)
        result = self.decide(adapter.to_input(indexed))
        return result, adapter.to_task(result, indexed)


_FALLBACK = (Outcome.ABSTAIN, Outcome.BLOCK)


def input_hash(inputs: Any) -> str:
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
