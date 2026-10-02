"""The runtime: validate input, ask providers, apply policy, emit an auditable result."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator, ValidationError

from mendari.core.derive import complete_answers, explain
from mendari.core.pack import Pack, load_pack
from mendari.core.policy import GovernanceOverride, PolicyResult, apply_policy
from mendari.core.registry import resolve_pack
from mendari.core.spec import Outcome, Priority
from mendari.errors import (
    FhirStructureError,
    HubError,
    PackNotFoundError,
    PackValidationError,
    ProviderError,
)
from mendari.providers import Answer, Provider, ProviderInfo, build_provider
from mendari.providers.batch import batching
from mendari.settings import FhirValidation

if TYPE_CHECKING:
    from mendari.core.audit import AuditStore


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
    # Base R4 and US Core findings for a FHIR input (`standards.us_core.Issue.to_dict()`): paths,
    # no values.
    input_issues: tuple[dict[str, str], ...] = ()

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
            "input_issues": list(self.input_issues),
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
        governance_source: Callable[[Pack], GovernanceOverride | None] | None = None,
        store: AuditStore | None = None,
        budget: CallBudget | None = None,
        fhir_validation: FhirValidation = "warn",
    ):
        """`governance` fixes an override for every decision; `governance_source` looks one up
        for each decision instead (a deployment's recorded approvals, per organization)."""
        overrides = dict(providers or {})
        unknown = set(overrides) - set(pack.manifest.providers)
        if unknown:
            raise PackValidationError(pack.id, [f"unknown provider slots {sorted(unknown)}"])
        self.pack = pack
        self.governance = governance
        self.governance_source = governance_source
        self.store = store
        self.budget = budget
        self.fhir_validation = fhir_validation
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
        """Resolve `namespace/name[@version]` (or a directory) from the local search path, or
        pull it from the registry (MENDARI_REGISTRY_URL, or the one logged into), verified
        and cached, when it isn't there."""
        try:
            return cls(resolve_pack(ref), **kwargs)
        except PackNotFoundError:
            from mendari.hub.client import RegistryClient, default_registry, pull

            try:
                url, token = default_registry()
            except HubError:
                raise PackNotFoundError(
                    f"no pack {ref!r} here, and no registry to pull it from"
                ) from None
            with RegistryClient(url, token) as registry:
                return cls(pull(ref, registry), **kwargs)

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
        """Why the input fails the pack's input schema, at most ten reasons. Each names where
        and which rule of the schema, never the value: jsonschema's own messages quote the
        input, and reasons are stored in the audit trail and copied into the FHIR Task."""
        errors = sorted(self._validator.iter_errors(inputs), key=lambda e: list(map(str, e.path)))
        return [f"{_where(error.path)}: {_schema_rule(error)}" for error in errors[:10]]

    def decide(
        self,
        inputs: Mapping[str, Any],
        *,
        input_issues: Sequence[Mapping[str, str]] = (),
        rejected: Sequence[str] = (),
    ) -> DecisionResult:
        """Decide one input. `rejected` (reasons from a standards check, e.g. US Core under
        `enforce`) blocks the input like a schema failure; `input_issues` are only recorded."""
        started = time.perf_counter()
        manifest = self.pack.manifest
        answers: dict[str, Answer] = {}
        failures: dict[str, str] = {}
        tokens_in = tokens_out = 0
        cost: float | None = 0.0

        governance = self.governance
        if governance is None and self.governance_source is not None:
            governance = self.governance_source(self.pack)
        problems = [*rejected, *self.validate(inputs)]
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
            policy = apply_policy(manifest, answers, failures, governance, variants)
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
            governance=governance.to_dict() if governance else None,
            input_issues=tuple(dict(issue) for issue in input_issues),
        )
        if self.store is not None:
            self.store.record(result, inputs, store_input=manifest.audit.store_input)
        return result

    def decide_fhir(
        self, resources: Mapping[str, Any], *, focus: Mapping[str, Any] | None = None
    ) -> tuple[DecisionResult, dict[str, Any]]:
        """Decide from FHIR R4 resources (a Bundle or {resourceType: resource}); return a Task.
        `focus` picks the resource to decide about, e.g. one of several draft orders.

        Input that is not FHIR R4 JSON (an unknown element or resource type, a wrong JSON type, a
        malformed primitive) raises `FhirStructureError` whatever the mode. A complex element R4
        requires, a code outside its required value set, and US Core's own rules are issues under
        `fhir_validation`: under `warn` they are recorded in the result; under `enforce` an error
        blocks the input before any provider."""
        from mendari.standards import fhir_structure, us_core
        from mendari.standards.fhir import FhirAdapter

        adapter = FhirAdapter.for_pack(self.pack)
        given = resources if focus is None else [resources, focus]
        base = fhir_structure.check(given)
        malformed = fhir_structure.blocking(base)
        if malformed:
            raise FhirStructureError(
                [
                    {"code": i.code, "expression": i.expression, "diagnostics": i.diagnostics}
                    for i in malformed
                ]
            )
        issues = []
        if self.fhir_validation != "off":
            issues = fhir_structure.merge(base, us_core.validate(given))
        indexed = adapter.index(resources, focus=focus)
        rejected = []
        if self.fhir_validation == "enforce":
            rejected = [
                f"not {us_core.standard(issue.profile)}: {issue.resource} {issue.expression}: "
                f"{issue.diagnostics}"
                for issue in us_core.errors(issues)[:10]
            ]
        result = self.decide(
            adapter.to_input(indexed),
            input_issues=[issue.to_dict() for issue in issues],
            rejected=rejected,
        )
        return result, adapter.to_task(result, indexed)


_FALLBACK = (Outcome.ABSTAIN, Outcome.BLOCK)


_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")


def _where(path: Iterable[Any]) -> str:
    """A path in the input. A key the schema does not name is the client's text, so only keys
    shaped like a field name are shown."""
    parts = [str(p) if isinstance(p, int) or _KEY.match(str(p)) else "*" for p in path]
    return "/".join(parts) or "(root)"


def _schema_rule(error: ValidationError) -> str:
    """What the schema requires, in words built from the schema alone (`validator_value` is the
    pack's own schema), never from the instance."""
    keyword, expected = error.validator, error.validator_value
    present = error.instance if isinstance(error.instance, Mapping) else {}
    if keyword == "required":
        names = expected if isinstance(expected, list) else []
        missing = [str(name) for name in names if name not in present]
        return f"{', '.join(missing) or 'a field'} is required"
    if keyword == "type":
        kinds = expected if isinstance(expected, list) else [expected]
        return f"must be of type {' or '.join(map(str, kinds))}"
    if keyword in ("enum", "const"):
        return "is not one of the values the schema allows"
    if keyword in ("minLength", "maxLength", "minItems", "maxItems"):
        bound = "at least" if keyword.startswith("min") else "at most"
        unit = "characters" if keyword.endswith("Length") else "items"
        return f"must have {bound} {expected} {unit}"
    if keyword in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        relation = {
            "minimum": "at least",
            "maximum": "at most",
            "exclusiveMinimum": "more than",
            "exclusiveMaximum": "less than",
        }[str(keyword)]
        return f"must be {relation} {expected}"
    if keyword == "pattern":
        return "does not match the pattern the schema requires"
    if keyword == "format":
        return f"is not a valid {expected}"
    if keyword in ("additionalProperties", "unevaluatedProperties"):
        schema = error.schema if isinstance(error.schema, Mapping) else {}
        allowed = set(schema.get("properties") or {})
        patterns = list(schema.get("patternProperties") or {})
        extra = [
            str(key)
            for key in present
            if key not in allowed and not any(re.search(p, str(key)) for p in patterns)
        ]
        named = [key for key in extra if _KEY.match(key)]
        unnamed = len(extra) - len(named)
        listed = [*named, *([f"{unnamed} more"] if unnamed and named else [])]
        return (
            f"has fields the schema does not allow: {', '.join(listed)}"
            if named
            else ("has a field the schema does not allow")
        )
    return f"does not satisfy the schema's {keyword} rule"


def input_hash(inputs: Any) -> str:
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
