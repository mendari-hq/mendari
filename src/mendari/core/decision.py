# SPDX-License-Identifier: Apache-2.0
"""The runtime: validate input, ask providers, apply policy, emit an auditable result."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import ValidationError

from mendari.core import input_schema, tenancy
from mendari.core.derive import complete_answers, explain
from mendari.core.origin import (
    MAX_COMMUNITY_TEXT_CHARS,
    bounded_reader,
    long_text,
    shape_problem,
)
from mendari.core.pack import Pack, load_pack
from mendari.core.patterns import OutOfTime, budget_active, check_time, search, time_budget
from mendari.core.policy import GovernanceOverride, PolicyResult, apply_policy
from mendari.core.registry import origin_of, resolve_pack
from mendari.core.spec import Outcome, Priority
from mendari.errors import (
    FhirStructureError,
    HubError,
    PackNotFoundError,
    PackValidationError,
    ProviderError,
)
from mendari.providers import Answer, Provider, ProviderInfo, build_provider
from mendari.providers.base import OVER_QUOTA
from mendari.providers.batch import batching
from mendari.providers.deadline import decision_deadline
from mendari.settings import FhirValidation, decision_deadline_s

if TYPE_CHECKING:
    from mendari.core.audit import AuditStore

MAX_SCHEMA_ERRORS = 100  # errors one check collects; ten become reasons


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
    # Why the input was refused before any provider ran (`$decide` reports these as failure).
    # In memory only: not in `to_dict`, so the audit record keeps its shape.
    rejected: bool = field(default=False, compare=False)
    # The pack-schema part of those reasons (standards rejections are in `input_issues`).
    schema_problems: tuple[str, ...] = field(default=(), compare=False)
    # How the stored record names each resource an input issue names (`us_core.record_labels`):
    # "Patient (entry 2)" for "Patient/MRN4821937". In memory only, like `rejected`.
    labels: Mapping[str, str] = field(default_factory=dict, compare=False)

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

    def to_record(self) -> dict[str, Any]:
        """`to_dict()` as the audit trail keeps it: the input issues, and the reasons that refused
        an input over them, name a resource by its type and position, never by its logical id
        (which can be a medical record number), since the record's digest is chained for good.
        What the caller gets back (`to_dict`, the OperationOutcome) keeps the id, to find the
        resource. The audit store also leaves out evidence it may not keep (`AuditStore.record`)."""
        record = self.to_dict()
        labels = {
            issue["resource"]: self.labels.get(issue["resource"]) or _type_only(issue["resource"])
            for issue in self.input_issues
        }
        record["input_issues"] = [
            {**issue, "resource": labels[issue["resource"]]} for issue in self.input_issues
        ]
        record["reasons"] = [_relabel(reason, labels) for reason in self.reasons]
        return record


# Called the first time an organization reaches one of its daily quotas (org, metric, limit).
QuotaReached = Callable[[str, str, int], None]


class CallBudget:
    """Daily call limits per provider type (e.g. {"jev": 500}; None counts calls without a
    limit), counted in the audit store, and per-organization daily quotas (`org_limits`:
    `decisions`, the remote calls they make by provider type, e.g. {"decisions": 5000}, and
    the remote calls of its evaluations, `eval_<type>`, which `for_evaluation` counts).

    A provider over budget fails like any other provider (`budget_exhausted`). A decision over
    its organization's quota fails its remote calls (`quota_exhausted`, `providers/base.py`
    `OVER_QUOTA`): local providers still answer, and the policy holds the decision for a
    person. `on_quota_reached` hears of each quota once a day per organization: when the call
    that reaches it is counted.
    """

    def __init__(
        self,
        store: AuditStore,
        limits: Mapping[str, int | None],
        *,
        org_limits: Mapping[str, int] | None = None,
        on_quota_reached: QuotaReached | None = None,
        evaluation: bool = False,
    ):
        self.store = store
        self.limits = dict(limits)
        self.org_limits = dict(org_limits or {})
        self.on_quota_reached = on_quota_reached
        self.evaluation = evaluation  # its calls count against `eval_<type>`, never a decision's

    def acquire(self, provider_type: str) -> None:
        refused = OVER_QUOTA.get()
        if refused is not None:
            raise refused
        metric = f"eval_{provider_type}" if self.evaluation else provider_type
        if metric in self.org_limits:
            self._charge_org(metric)
        if provider_type in self.limits:
            self.store.consume(provider_type, daily_limit=self.limits[provider_type])

    def admit_decision(self) -> None:
        """Count one decision against its organization's quota (`quota_exhausted` over it).
        An evaluation's cases are not the organization's decisions: never counted."""
        if "decisions" in self.org_limits and not self.evaluation:
            self._charge_org("decisions")

    def for_evaluation(self) -> CallBudget:
        """The budget an evaluation spends (LP-21h): its organization's evaluation quotas
        (`eval_jev`, `eval_llm`; unset: no limit), never the quotas of its decisions, which an
        evaluation never uses up; and the deployment's daily budgets, which live decisions
        share: once those run out, every organization's live decisions go to people
        (`budget_exhausted`) until midnight, so keep the evaluation quotas below them."""
        return CallBudget(
            self.store,
            self.limits,
            org_limits=self.org_limits,
            on_quota_reached=self.on_quota_reached,
            evaluation=True,
        )

    def _charge_org(self, metric: str) -> None:
        org = tenancy.current() or tenancy.DEFAULT_ORG
        limit = self.org_limits[metric]
        count = self.store.consume_org(org, metric, daily_limit=limit)
        if count is None:
            raise ProviderError("quota_exhausted", f"this organization's daily {metric} quota")
        if count == limit and self.on_quota_reached is not None:
            self.on_quota_reached(org, metric, limit)


class Decision:
    """A loaded Decision Pack bound to concrete providers. Call it with a pack input."""

    def __init__(
        self,
        pack: Pack,
        *,
        providers: Mapping[str, str] | None = None,
        governance: GovernanceOverride | None = None,
        governance_source: Callable[[Pack], GovernanceOverride | None] | None = None,
        hold_source: Callable[[Pack], str | None] | None = None,
        store: AuditStore | None = None,
        budget: CallBudget | None = None,
        fhir_validation: FhirValidation = "warn",
        deadline_s: float | None = None,
    ):
        """`governance` fixes an override for every decision; `governance_source` looks one up
        for each decision instead (a deployment's recorded approvals, per organization).
        `deadline_s` is how long each decision's remote calls may take together (default: the
        deployment's `MENDARI_DECISION_DEADLINE_S`, `providers/deadline.py`); each decision
        starts its own clock, which is never kept here (one Decision serves every thread)."""
        overrides = dict(providers or {})
        unknown = set(overrides) - set(pack.manifest.providers)
        if unknown:
            raise PackValidationError(pack.id, [f"unknown provider slots {sorted(unknown)}"])
        self.pack = pack
        self.governance = governance
        self.governance_source = governance_source
        # Why automation is held for this pack now (an operator's automation hold), or None.
        self.hold_source = hold_source
        self.store = store
        self.budget = budget
        self.fhir_validation = fhir_validation
        self.deadline_s = deadline_s if deadline_s is not None else decision_deadline_s()
        self.providers: dict[str, Provider] = {
            slot: build_provider(pack, slot, overrides.get(slot))
            for slot in pack.manifest.providers
        }
        if budget is not None:
            for provider in self.providers.values():
                provider.use_budget(budget.acquire)
        # A pack the deployment does not own runs on the community engine (`Pack.bounded`).
        self._validator = input_schema.validator(pack.input_schema, community=pack.bounded)
        # Its input keys count toward its text only where its schema runs patterns on them;
        # otherwise each is held to the length alone (`origin.long_text`).
        self._count_keys = False
        if pack.bounded:  # its caps, imported only for one
            from mendari.core.caps import patterns_on_keys

            self._count_keys = patterns_on_keys(pack.input_schema)

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
        """The pack at `path`, with the origin of where it is (`core/registry.py` `origin_of`): a
        pulled pack in the registry cache is one, and runs on its engine."""
        return cls(load_pack(path, origin=origin_of(path)), **kwargs)

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
        input, and reasons are stored in the audit trail and copied into the FHIR Task.

        The check of a pack on the community engine (`Pack.bounded`: a community pack, or one
        from a registry) holds to its input limits (`origin.shape_problem`) and to a time
        budget: the decision's, or one of its own when called alone (`mendari omop`). It checks
        the budget after each error and stops at `MAX_SCHEMA_ERRORS`. Inside a decision, a
        budget spent before any error is found raises `OutOfTime` (the decision abstains);
        alone, it is a reason."""
        bounded = self.pack.bounded
        reader = bounded_reader(self.pack.id, self.pack.origin)
        if bounded and (shape := shape_problem(inputs, reader)) is not None:
            return [f"(root): {shape}"]
        if bounded and (too_long := long_text(inputs, count_keys=self._count_keys)):
            return [
                f"{too_long}: longer than {MAX_COMMUNITY_TEXT_CHARS:,} characters, the most "
                f"{reader} reads"
            ]
        alone = bounded and not budget_active()
        with time_budget() if alone else nullcontext():
            found: list[Any] = []
            try:
                for error in self._validator.iter_errors(inputs):
                    found.append(error)
                    if bounded:
                        check_time()
                        if len(found) >= MAX_SCHEMA_ERRORS:
                            break
            except OutOfTime as stopped:
                if not found:
                    if alone:
                        return [f"(root): checking it against the schema stopped: {stopped}"]
                    raise  # the pack's schema, not the input: `decide` abstains
            except (RecursionError, OverflowError):
                if not found:
                    return [
                        "(root): the schema cannot check it: it is nested too deep or holds a "
                        "number too large, or the schema refers to itself without end"
                    ]
            errors = sorted(found, key=lambda e: list(map(str, e.path)))
            if not bounded:  # as it always was
                return [f"{_where(error.path)}: {_schema_rule(error)}" for error in errors[:10]]
            reasons: list[str] = []
            for error in errors:
                try:
                    rule = _schema_rule(error, bounded)
                except OutOfTime:  # wording it would search the schema's patterns again
                    rule = "does not meet the schema"
                reason = f"{_where(error.path)}: {rule}"
                if reason not in reasons:  # one `required` yields an error per missing name
                    reasons.append(reason)
                if len(reasons) == 10:
                    break
            return reasons

    def decide(
        self,
        inputs: Mapping[str, Any],
        *,
        input_issues: Sequence[Mapping[str, str]] = (),
        rejected: Sequence[str] = (),
        labels: Mapping[str, str] | None = None,
    ) -> DecisionResult:
        """Decide one input. `rejected` (reasons from a standards check, e.g. US Core under
        `enforce`) blocks the input like a schema failure; `input_issues` are only recorded, and
        `labels` says how the stored record names the resources they name (`to_record`)."""
        started = time.perf_counter()
        manifest = self.pack.manifest
        if self.store is not None and manifest.audit.store_input == "full":
            self.store.ready_to_keep()  # before a provider (or a quota) sees the case
        answers: dict[str, Answer] = {}
        failures: dict[str, str] = {}
        tokens_in = tokens_out = 0
        cost: float | None = 0.0

        governance = self.governance
        if governance is None and self.governance_source is not None:
            governance = self.governance_source(self.pack)
        over_quota: ProviderError | None = None
        stopped: ProviderError | None = None  # a bounded pack's schema check used the budget
        if self.budget is not None:
            try:
                self.budget.admit_decision()
            except ProviderError as error:
                over_quota = error  # every provider fails with it, so nothing automates
        # The patterns of a pack on the community engine (`Pack.bounded`: a community pack, or
        # one from a registry) share one time budget in this decision (`core/patterns.py`): past
        # it its schema check or its rules stop, and the decision abstains, for a person.
        bounded = self.pack.bounded
        with time_budget() if bounded else nullcontext():
            reader = bounded_reader(self.pack.id, self.pack.origin)
            shape = shape_problem(inputs, reader) if bounded else None
            too_long = (
                long_text(inputs, count_keys=self._count_keys)
                if bounded and shape is None
                else None
            )
            if shape is not None:
                schema_problems = [f"(root): {shape}"]  # before the schema runs on every value
            elif too_long is not None:
                # Before the schema's patterns run too: its patterns run on texts this long, no
                # longer (`core/origin.py`; its caps are measured on it).
                schema_problems = [
                    f"{too_long}: longer than {MAX_COMMUNITY_TEXT_CHARS:,} characters, the most "
                    f"{reader} reads"
                ]
            else:
                try:
                    schema_problems = self.validate(inputs)
                except OutOfTime as error:
                    # The pack's schema used the budget: not the input's fault, so no refusal.
                    # Every slot fails as a provider that cannot answer does, for a person.
                    schema_problems = []
                    stopped = ProviderError("time_budget", f"its schema check stopped: {error}")
            problems = [*rejected, *schema_problems]
            if problems:
                policy = PolicyResult(
                    outcome=Outcome.BLOCK,
                    recommendation=None,
                    priority=manifest.policy.default_priority,
                    reasons=tuple(f"input rejected: {problem}" for problem in problems),
                )
            else:
                over = OVER_QUOTA.set(over_quota)  # remote calls fail with it; local ones run
                try:
                    # The deadline's clock starts here, for this decision's remote calls only:
                    # past it they fail with `timeout`, and local providers still answer.
                    with decision_deadline(self.deadline_s), batching(self.providers.values()):
                        for slot, provider in self.providers.items():
                            try:
                                if stopped is not None:
                                    raise stopped
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
                finally:
                    OVER_QUOTA.reset(over)
                complete_answers(manifest, answers, failures)
                variants = {slot: info.variant for slot, info in self.provider_info.items()}
                hold = self.hold_source(self.pack) if self.hold_source is not None else None
                if over_quota is not None:
                    hold = "; ".join(
                        filter(None, ["this organization's daily decisions quota is used up", hold])
                    )
                policy = apply_policy(
                    manifest,
                    answers,
                    failures,
                    governance,
                    variants,
                    community=self.pack.community,
                    hold=hold,
                )
                primary = manifest.questions[manifest.policy.primary]
                if primary.derive is not None:
                    policy = replace(policy, reasons=(*policy.reasons, *explain(primary, answers)))

        # Keyed under the deployment's input hash key when it has one (`core/phi.py`).
        hashed = input_hash(inputs) if self.store is None else self.store.phi.input_hash(inputs)
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
            input_hash=hashed,
            governance=governance.to_dict() if governance else None,
            input_issues=tuple(dict(issue) for issue in input_issues),
            rejected=bool(problems),
            schema_problems=tuple(schema_problems),
            labels=dict(labels or {}),
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
            labels=us_core.record_labels(given, issues),
        )
        return result, adapter.to_task(result, indexed)


_FALLBACK = (Outcome.ABSTAIN, Outcome.BLOCK)


_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")


def _where(path: Iterable[Any]) -> str:
    """A path in the input. A key the schema does not name is the client's text, so only keys
    shaped like a field name are shown."""
    parts = [str(p) if isinstance(p, int) or _KEY.match(str(p)) else "*" for p in path]
    return "/".join(parts) or "(root)"


def _short(name: str, limit: int = 40) -> str:
    """A field name as a reason shows it: at most `limit` characters."""
    return name if len(name) <= limit else name[: limit - 1] + "…"


def _schema_rule(error: ValidationError, bounded: bool = False) -> str:
    """What the schema requires, in words built from the schema alone (`validator_value` is the
    pack's own schema), never from the instance. The patterns of a pack on the community engine
    (`bounded`) run on RE2 (`core/patterns.py`)."""
    keyword, expected = error.validator, error.validator_value
    present = error.instance if isinstance(error.instance, Mapping) else {}
    if keyword == "required":
        names = expected if isinstance(expected, list) else []
        missing = [str(name) for name in names if name not in present]
        if not bounded:  # as it always was
            return f"{', '.join(missing) or 'a field'} is required"
        more = f" and {len(missing) - 5} more" if len(missing) > 5 else ""
        return f"{', '.join(map(_short, missing[:5])) or 'a field'}{more} is required"
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
            if key not in allowed
            and not any(search(p, str(key), community=bounded) for p in patterns)
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


def _type_only(label: str) -> str:
    """A resource label without its id: "Patient/p1" -> "Patient"."""
    return label.partition("/")[0]


def _relabel(reason: str, labels: Mapping[str, str]) -> str:
    """A refusal reason ("input rejected: not US Core 6.1.0: Patient/p1 Patient.gender: ...")
    with each resource named as the record names it. A label is followed by a space and a
    FHIRPath, and a FHIR id has no space, so the match is exact."""
    for label, kept in labels.items():
        reason = reason.replace(f": {label} ", f": {kept} ")
    return reason


def input_hash(inputs: Any) -> str:
    """The unkeyed `sha256:` of an input, for a decision made without a store (a store keys it
    under its deployment's input hash key, `PhiPolicy.input_hash`)."""
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
