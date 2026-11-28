# SPDX-License-Identifier: Apache-2.0
"""The runtime: validate input, ask providers, apply policy, emit an auditable result."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, MutableMapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import ValidationError

from mendari.core import actions, input_schema, tenancy
from mendari.core.actions import ActionResult, Asked, Checked, Proposed
from mendari.core.agent import Agent, load_agent
from mendari.core.origin import (
    MAX_BOUNDED_TEXT_CHARS,
    UNAPPROVED,
    bounded_reader,
    long_text,
    shape_problem,
)
from mendari.core.patterns import OutOfTime, budget_active, check_time, search, time_budget
from mendari.core.policy import (
    GovernanceOverride,
    PolicyInputs,
    PolicyResult,
    Review,
    action_stops,
    disposition,
    gate_of,
    limitation,
    risk_permits_auto,
)
from mendari.core.registry import origin_of, resolve_agent
from mendari.core.site_codes import SiteCodeMap, SiteCodeMaps
from mendari.core.spec import AgentManifest, Outcome, Priority
from mendari.core.text import well_formed
from mendari.errors import (
    AgentNotFoundError,
    AgentValidationError,
    FhirStructureError,
    HubError,
    MendariError,
    ProviderError,
)
from mendari.providers import Answer, Provider, ProviderInfo, build_provider
from mendari.providers.agent import AgentProvider, ConnectionSource
from mendari.providers.base import METER, OVER_QUOTA, Meter, Metered
from mendari.providers.batch import batching
from mendari.providers.deadline import deadline_until, decision_deadline, due
from mendari.providers.signals import Signals
from mendari.settings import FhirValidation, decision_deadline_s

if TYPE_CHECKING:
    from mendari.core.audit import AuditStore
    from mendari.standards.ehr_declarations import EhrDeclaration
    from mendari.standards.fhir import FhirMapping
    from mendari.standards.linked import Linker

MAX_SCHEMA_ERRORS = 100  # errors one check collects; ten become reasons
REJECTED = "input rejected: "  # how each reason that refused an input begins (`decide`)


@dataclass(frozen=True)
class DecisionResult:
    id: str
    created_at: str
    agent_id: str
    agent_version: str
    agent_digest: str
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
    # What a FHIR decision looked for in the record and found (`standards/data_coverage.py`,
    # `mendari.data-coverage/1`): counts, reason codes and the agent's own words, never a value.
    # None for a decision on JSON input.
    data_coverage: dict[str, Any] | None = None
    # The site code map a FHIR decision read its record with (`core/site_codes.py`, LP-23i): the
    # version and SHA-256 of its organization's map. None without one, and on JSON input.
    site_codes: dict[str, str] | None = None
    # The declaration of what its EHR does not offer, when a FHIR decision read a list without
    # a source so declared (`standards/ehr_declarations.py`): its version, SHA-256, who attested
    # it and where. None otherwise, and on JSON input.
    ehr_declaration: dict[str, str] | None = None
    # The promotion that let an agent store's version serve here (LP-20h): its id, so the trail
    # leads from a decision to who signed its version off. Absent for every other agent.
    promotion: str | None = None
    # Why the input was refused before any provider ran (`$decide` reports these as failure).
    # In memory only: not in `to_dict`, so the audit record keeps its shape.
    rejected: bool = field(default=False, compare=False)
    # The agent-schema part of those reasons (standards rejections are in `input_issues`).
    schema_problems: tuple[str, ...] = field(default=(), compare=False)
    # How the stored record names each resource an input issue names (`us_core.record_labels`):
    # "Patient (entry 2)" for "Patient/MRN4821937". In memory only, like `rejected`.
    labels: Mapping[str, str] = field(default_factory=dict, compare=False)
    # Its remote calls by provider type (`providers.base.Meter`), for the usage ledger
    # (`AuditStore.record`). In memory only, like `rejected`.
    usage: Mapping[str, Metered] = field(default_factory=dict, compare=False)
    # The parts of a FHIR record that could not be read, as (key, reason) pairs, sorted: a
    # prefetch key or a mapped field, and a `ReadReason`. A decision with any abstains unless
    # its input is refused (`decide`). In memory only, like `rejected`; the reasons and the
    # data-coverage record say the same in the record.
    unreadable: tuple[tuple[str, str], ...] = field(default=(), compare=False)
    # Each list a FHIR decision read without a source the site declared its EHR does not offer,
    # as its reasons begin (`policy.limitation`), whatever the list is called. In memory only,
    # like `rejected`; the reasons and `ehr_declaration` say the same in the record.
    limitations: tuple[str, ...] = field(default=(), compare=False)
    # What the policy decided from (`policy.PolicyInputs`), so it can be applied again under
    # another automation threshold (`evals/operating_point.py`). None when no policy ran: an
    # input refused, or a record that could not be read. In memory only, like `rejected`.
    policy_inputs: PolicyInputs | None = field(default=None, compare=False, repr=False)
    # The actions the agent proposed with its answers, each with what may be done with it (spec
    # 0.4, `core/actions.py`), in the order proposed. None for an agent that declares no action
    # and proposed none: its result is then an answer alone. In the caller's answer (`to_dict`)
    # and never in the stored record (`to_record`): the chain keeps each as an event, without
    # its arguments.
    actions: tuple[ActionResult, ...] | None = field(default=None, compare=False)
    # What its providers found that can only lower its automation (`providers/signals.py`: the
    # agent changed since its confidence was calibrated, a second signal disagreed), with what
    # an evaluation counts from. In memory only, like `rejected`: the reasons say it in the record.
    signals: Signals = field(default_factory=Signals, compare=False, repr=False)
    # What the agent wrote with its answers (spec section 15.2), by the output's name: every
    # text, in memory only, like `rejected`. It is in neither `to_dict` nor `to_record`, so no
    # stored record holds a written text. An evaluation grades these; nothing else hands them
    # on: an answer carries `drafts()`, and says where each text stands (`written`).
    outputs: Mapping[str, str] = field(default_factory=dict, compare=False, repr=False)

    @property
    def abstained_unreadable(self) -> bool:
        """An ABSTAIN because part of the record could not be read (a CDS card always shows
        it)."""
        return self.outcome is Outcome.ABSTAIN and bool(self.unreadable)

    @property
    def confidence(self) -> float | None:
        answer = self.answers.get(self.primary)
        return answer.confidence if answer else None

    @property
    def written(self) -> dict[str, dict[str, str]]:
        """Where each text the agent wrote stands (ADR-0027), by the output's name: `held` when
        the decision awaits a person, who is handed it as a draft (`drafts`); `not_released`
        otherwise: an automated or a blocked decision hands what its agent wrote to nobody.
        Empty for a decision whose agent wrote nothing. Never a text."""
        state = "held" if self.outcome in _REVIEWED else "not_released"
        return {name: {"state": state} for name in sorted(self.outputs)}

    def drafts(self) -> dict[str, str]:
        """The texts an answer may carry (ADR-0027): what the agent wrote, when the decision
        awaits a person; none otherwise. A text is handed only to a person: with an automated
        decision nobody would read it before it was used, and a blocked one is not to be used."""
        return dict(self.outputs) if self.outcome in _REVIEWED else {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "agent": {
                "id": self.agent_id,
                "version": self.agent_version,
                "digest": self.agent_digest,
            },
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
            "data_coverage": self.data_coverage,
            "site_codes": self.site_codes,
            "ehr_declaration": self.ehr_declaration,
            **({"promotion": self.promotion} if self.promotion is not None else {}),
            **(
                {"actions": [action.to_dict() for action in self.actions]}
                if self.actions is not None
                else {}
            ),
            # Where each text stands, only for a decision whose agent wrote one: every other
            # record keeps the shape it had. The texts themselves are in neither.
            **({"written": self.written} if self.outputs else {}),
        }

    def to_record(self) -> dict[str, Any]:
        """`to_dict()` as the audit trail keeps it: the input issues, and the reasons that refused
        an input over them, name a resource by its type and position, never by its logical id
        (which can be a medical record number), since the record's digest is chained for good.
        What the caller gets back (`to_dict`, the OperationOutcome) keeps the id, to find the
        resource. The audit store also leaves out evidence it may not keep (`AuditStore.record`).
        Proposed actions are no part of it: their arguments are the agent's text, and the record's
        digest is chained for good, so each is an event of its own (`action.proposed`) and the
        record's shape does not depend on them."""
        record = self.to_dict()
        record.pop("actions", None)
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
    """Daily call limits per provider type (e.g. {"llm": 500}; None counts calls without a
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
        (`eval_llm`; unset: no limit), never the quotas of its decisions, which an
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
    """A loaded agent bound to concrete providers. Call it with an agent input."""

    def __init__(
        self,
        agent: Agent,
        *,
        providers: Mapping[str, str] | None = None,
        governance: GovernanceOverride | None = None,
        governance_source: Callable[[Agent], GovernanceOverride | None] | None = None,
        hold_source: Callable[[Agent], str | None] | None = None,
        origin_approval: Callable[[Agent], bool] | None = None,
        recall_source: Callable[[Agent], str | None] | None = None,
        origin_cap: bool = True,
        store: AuditStore | None = None,
        budget: CallBudget | None = None,
        fhir_validation: FhirValidation = "warn",
        deadline_s: float | None = None,
        site_codes: SiteCodeMaps | None = None,
        promotion: str | None = None,
        canned_cap: bool = False,
        connection_source: ConnectionSource | None = None,
    ):
        """`governance` fixes an override for every decision; `governance_source` looks one up
        for each decision instead (a deployment's recorded approvals, per organization).
        `site_codes` are the deployment's site code maps (`MENDARI_SITE_CODES`, LP-23i): a FHIR
        decision reads its record with its own organization's, and never automates through one
        of its translations.
        `deadline_s` is how long each decision's remote calls may take together (default: the
        deployment's `MENDARI_DECISION_DEADLINE_S`, `providers/deadline.py`); each decision
        starts its own clock, which is never kept here (one Decision serves every thread).

        An agent from a registry never automates until its organization approves its digest
        (`origin_approval`, the deployment's `registry_approvals`; without one, never), nor while
        the registry recalled it (LP-18d, ADR-0012): AUTO becomes HUMAN_REVIEW, as under a hold.
        Nothing in the agent lifts it. `origin_cap=False` is only for checking the agent's own
        policy (`mendari test`, `mendari eval`), never for serving.

        `canned_cap` (a server with sign-in) never automates on canned answers (an agent's
        `mock_file` read while `MENDARI_AGENT_URL` is unset): AUTO and a rule's BLOCK become
        HUMAN_REVIEW, with the reason (`policy.apply_policy`'s `canned` cap). Off, they decide
        as live answers do.

        `connection_source` (a server's) gives, for an agent's id, the connection the
        organization a decision acts for set for it in the console (ADR-0021): each `agent`
        variant, and each composite's `agent` member, asks it before `MENDARI_AGENT_URL`
        (`AgentProvider.live`). Without one, as for every decision the command line makes, no
        organization's connection is read."""
        overrides = dict(providers or {})
        unknown = set(overrides) - set(agent.manifest.providers)
        if unknown:
            raise AgentValidationError(agent.id, [f"unknown provider slots {sorted(unknown)}"])
        self.agent = agent
        self.governance = governance
        self.governance_source = governance_source
        # Why automation is held for this agent now (an operator's automation hold), or None.
        self.hold_source = hold_source
        self.origin_approval = origin_approval
        # Why its registry recalls this agent now (a server's `RecallWatch`, LP-18e), or None.
        self.recall_source = recall_source
        self.origin_cap = origin_cap
        self.store = store
        self.budget = budget
        self.fhir_validation = fhir_validation
        self.deadline_s = deadline_s if deadline_s is not None else decision_deadline_s()
        self.site_codes = site_codes
        # An agent store's version is served under its promotion (LP-20h), named on each record.
        self.promotion = promotion
        self.canned_cap = canned_cap
        self.providers: dict[str, Provider] = {
            slot: build_provider(agent, slot, overrides.get(slot))
            for slot in agent.manifest.providers
        }
        if budget is not None:
            for provider in self.providers.values():
                provider.use_budget(budget.acquire)
        if connection_source is not None:
            for leaf in self.agent_providers():
                leaf.use_connection(connection_source)
        # What checks the arguments of each declared action that has a schema (spec §13.3).
        self._action_checkers = actions.validators(agent.manifest)
        # An agent the deployment does not own runs on the bounded engine (`Agent.bounded`).
        self._validator = input_schema.validator(agent.input_schema, bounded=agent.bounded)
        # Its input keys count toward its text only where its schema runs patterns on them;
        # otherwise each is held to the length alone (`origin.long_text`).
        self._count_keys = False
        if agent.bounded:  # its caps, imported only for one
            from mendari.core.caps import patterns_on_keys

            self._count_keys = patterns_on_keys(agent.input_schema)

    def _origin_holds(self) -> list[str]:
        """Why an agent from a registry may not automate now: not approved by this organization
        for its digest, or recalled when it was pulled (its verified marker records the recall
        the registry served then)."""
        if not self.origin_cap or self.agent.origin != "hub":
            return []
        held = []
        if self.origin_approval is None or not self.origin_approval(self.agent):
            held.append(UNAPPROVED)
        recall = self.agent.pulled.get("yank")
        if isinstance(recall, Mapping):
            severity = recall.get("severity")
            known = severity if severity in ("low", "medium", "high", "critical") else "unknown"
            held.append(f"recalled by its registry ({known})")
        if self.recall_source is not None:
            now = self.recall_source(self.agent)
            if now:
                held.append(now)
        return held

    def vouched(self) -> bool:
        """Whether the deployment vouches for this agent now: it is no agent from a registry that
        its organization has not approved for its digest, or that is recalled. For one it does
        not vouch for, nothing proposed is released without a person (`policy.gate_of`): its
        declared effects are claims nobody here checked."""
        return not self._origin_holds()

    def automation_stopped(self) -> str | None:
        """Why this agent may not automate now, or None: it is not vouched for, automation is
        on hold, or its risk level needs an approval that is not in force. What an action asked
        for after an AUTO decision is held to (`authorize_action`): a hold or a revoked approval
        stops new releases, though the decision itself was automated."""
        held = self._origin_holds()
        if held:
            return "; ".join(held)
        hold = self.hold_source(self.agent) if self.hold_source is not None else None
        if hold:
            return f"automation is on hold: {hold}"
        governance = self.governance
        if governance is None and self.governance_source is not None:
            governance = self.governance_source(self.agent)
        level = self.agent.manifest.risk.level
        if not risk_permits_auto(level, governance):
            return f"risk level {int(level)} requires human review"
        return None

    def _acted(
        self, checked: Sequence[Checked], policy: PolicyResult, vouched: bool
    ) -> tuple[ActionResult, ...]:
        """Each checked proposal with what may be done with it, under the decision's final
        policy result: a refused one is blocked for what it is; any other by its gate and the
        decision's state (`policy.disposition`). A decision that goes to a person awaits its
        review; one that was automated or blocked has none."""
        hash_of = actions.plain_hash if self.store is None else self.store.phi.arguments_hash
        review: Review = "pending" if policy.outcome in _REVIEWED else "none"
        acted = []
        for item in checked:
            spec = item.spec
            if item.refusal is not None or spec is None:
                found: tuple[actions.Disposition, actions.Reason] = (
                    "blocked",
                    item.refusal or "undeclared",
                )
            else:
                found = disposition(
                    gate_of(
                        spec.effect,
                        allow_auto=spec.allow_auto,
                        vouched=vouched,
                        agreed=not item.proposed.disputed,
                        free_text=spec.free_text,
                    ),
                    outcome=policy.outcome,
                    recommendation=policy.recommendation,
                    review=review,
                )
            acted.append(
                ActionResult(
                    id=actions.new_id(),
                    name=item.proposed.name,
                    effect=None if spec is None else spec.effect,
                    declared=spec is not None,
                    disposition=found[0],
                    reason=found[1],
                    arguments=dict(item.proposed.arguments),
                    arguments_hash=hash_of(item.proposed.arguments),
                    source="answer",
                    slot=item.proposed.slot,
                    problems=item.problems,
                    free_text=spec is not None and spec.free_text,
                )
            )
        return tuple(acted)

    def authorize_action(
        self, decision_id: str, name: str, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Whether one action with these arguments may be run for a decision this agent made
        and its store recorded (spec section 13.8): the action's id, its declared name and
        effect, its disposition and the reason, as the same gate that judged the proposals made
        with the answer gives them, by the decision as it stands now.

        Asking again for an action already proposed on that decision (the same declared name,
        the same arguments) answers how it stands now, with `repeated: true`, and records
        nothing new: so it is the check a caller makes right before it runs a tool, and how a
        caller holding a held action learns that a reviewer released it. A new action is checked
        and gated, and recorded in the audit chain (`action.proposed`, with who asked). Nothing
        is run here: the caller runs an action, and only one that is `released`.

        A decision that is unknown, another organization's or another agent's raises
        `AgentNotFoundError`, one answer for all three. There is no authorization without a
        recorded decision."""
        if self.store is None:
            raise MendariError("an action is authorized for a decision an audit store recorded")
        problem = actions.shape_problem(name, arguments)
        if problem is not None:
            raise MendariError(problem)
        manifest = self.agent.manifest
        checked = actions.check(manifest, self._action_checkers, Proposed(name, dict(arguments)))
        spec = checked.spec
        # Read before the store's own session opens: a hold or an approval is a read of it.
        vouched = self.vouched()
        automating = self.automation_stopped() is None
        digest = self.agent.digest

        def ask(row: Mapping[str, Any], events: Sequence[Mapping[str, Any]]) -> Asked:
            record = row["record"]
            made_by = record.get("agent") if isinstance(record, Mapping) else None
            if not isinstance(made_by, Mapping) or made_by.get("digest") != digest:
                # Another version decided: what it declared is not what this one does.
                return Asked(None, None, "blocked", "agent_changed")
            if spec is None:
                return Asked(None, None, "blocked", "undeclared")
            if checked.refusal is not None:
                return Asked(
                    name, spec.effect.value, "blocked", checked.refusal, free_text=spec.free_text
                )
            refused_before = any(
                event.get("event") == actions.PROPOSED
                and event["payload"].get("reason") in actions.REFUSALS
                for event in events
            )
            found = disposition(
                gate_of(
                    spec.effect,
                    allow_auto=spec.allow_auto,
                    vouched=vouched,
                    free_text=spec.free_text,
                ),
                outcome=Outcome(row["outcome"]),
                recommendation=row["recommendation"],
                review=_review_of(row),
                automating=automating,
                refused_before=refused_before,
            )
            return Asked(name, spec.effect.value, found[0], found[1], free_text=spec.free_text)

        keep = manifest.audit.store_input == "full"
        return self.store.propose_action(
            decision_id,
            agent_id=self.agent.id,
            arguments_hash=self.store.phi.arguments_hash(arguments),
            kept=actions.canonical({"name": name, "arguments": arguments}) if keep else None,
            ask=ask,
        )

    def site_map(self) -> SiteCodeMap | None:
        """The site code map of the organization this decision acts for (`tenancy.current()`;
        the default organization's when it acts for none, as in open mode), or None: always
        None without `site_codes`, as for every decision the command line makes (`mendari
        decide --fhir`; `mendari bulk dataset` reads none either), which never reads
        `MENDARI_SITE_CODES`. Another organization's is never given."""
        if self.site_codes is None:
            return None
        return self.site_codes.for_org(tenancy.current() or tenancy.DEFAULT_ORG)

    @classmethod
    def from_hub(cls, ref: str, **kwargs: Any) -> Decision:
        """Resolve `namespace/name[@version]` (or a directory) from the local search path, or
        pull it from the registry (MENDARI_REGISTRY_URL, or the one logged into), verified
        and cached, when it isn't there."""
        try:
            return cls(resolve_agent(ref), **kwargs)
        except AgentNotFoundError:
            from mendari.hub.client import RegistryClient, default_registry, pull

            try:
                url, token = default_registry()
            except HubError:
                raise AgentNotFoundError(
                    f"no agent {ref!r} here, and no registry to pull it from"
                ) from None
            with RegistryClient(url, token) as registry:
                return cls(pull(ref, registry), **kwargs)

    @classmethod
    def from_path(cls, path: str | Path, **kwargs: Any) -> Decision:
        """The agent at `path`, with the origin of where it is (`core/registry.py` `origin_of`): a
        pulled agent in the registry cache is one, and runs on its engine."""
        return cls(load_agent(path, origin=origin_of(path)), **kwargs)

    def agent_providers(self) -> list[AgentProvider]:
        """The `agent` variants this Decision runs, a composite's members included: the ones a
        connection reaches (ADR-0021)."""
        return [
            leaf
            for provider in self.providers.values()
            for leaf in provider.leaves()
            if isinstance(leaf, AgentProvider)
        ]

    @property
    def is_remote(self) -> bool:
        return any(provider.remote for provider in self.providers.values())

    @property
    def provider_info(self) -> dict[str, ProviderInfo]:
        return {slot: provider.info for slot, provider in self.providers.items()}

    def __call__(self, inputs: Mapping[str, Any]) -> DecisionResult:
        return self.decide(inputs)

    def validate(self, inputs: Any) -> list[str]:
        """Why the input fails the agent's input schema, at most ten reasons, each once (one
        `required` gives an error per missing name, each worded the same). Each names where
        and which rule of the schema, never the value: jsonschema's own messages quote the
        input, and reasons are stored in the audit trail and copied into the FHIR Task. A number
        JSON cannot carry (NaN, an infinity) is refused before the schema runs (`_not_finite`).

        The check of an agent on the bounded engine (`Agent.bounded`: one from a registry or a
        store) holds to its input limits (`origin.shape_problem`) and to a time budget: the
        decision's, or one of its own when called alone. It checks the budget after each error
        and stops at `MAX_SCHEMA_ERRORS`. Inside a decision, a budget spent before any error is
        found raises `OutOfTime` (the decision abstains); alone, it is a reason."""
        bounded = self.agent.bounded
        reader = bounded_reader(self.agent.origin)
        if bounded and (shape := shape_problem(inputs, reader)) is not None:
            return [f"(root): {shape}"]
        if bounded and (too_long := long_text(inputs, count_keys=self._count_keys)):
            return [
                f"{too_long}: longer than {MAX_BOUNDED_TEXT_CHARS:,} characters, the most "
                f"{reader} reads"
            ]
        if not_finite := _not_finite(inputs):
            return [
                f"{place}: must be a finite number, not NaN or an infinity" for place in not_finite
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
                    raise  # the agent's schema, not the input: `decide` abstains
            except (RecursionError, OverflowError):
                if not found:
                    return [
                        "(root): the schema cannot check it: it is nested too deep or holds a "
                        "number too large, or the schema refers to itself without end"
                    ]
            errors = sorted(found, key=lambda e: list(map(str, e.path)))
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
        unreadable: Sequence[tuple[str, str]] = (),
        data_coverage: Mapping[str, Any] | None = None,
        site_codes: Mapping[str, str] | None = None,
        translated: Sequence[str] = (),
        due_at: float | None = None,
        not_offered: Sequence[str] = (),
        ehr_declaration: Mapping[str, str] | None = None,
        shadow: bool = False,
    ) -> DecisionResult:
        """Decide one input. `rejected` (reasons from a standards check, e.g. US Core under
        `enforce`) blocks the input like a schema failure; `input_issues` are only recorded, and
        `labels` says how the stored record names the resources they name (`to_record`).

        `unreadable` names the parts of a FHIR record that could not be read, as (key, reason)
        pairs (`errors.ReadReason`). Input that is refused still BLOCKs; otherwise the decision
        is ABSTAIN, before any provider runs, its reasons `could not read <key>: <reason>`, and
        recorded like any other (LP-23d). `data_coverage` is the FHIR decision's record of what
        it looked for and found (`standards/data_coverage.py`), kept as given, and `site_codes`
        the version and digest of the site code map it read with (LP-23i). `translated` names the
        code sets a site's translation gave the input a value through: then AUTO, and a rule's
        BLOCK, become HUMAN_REVIEW (`policy.apply_policy`'s `site_code` cap). `due_at` is when
        the decision's deadline falls (`providers/deadline.due`), when it started before this
        call (`decide_fhir`'s linked reads); else it starts at the provider loop. `not_offered`
        names each list read without a source the site declared its EHR does not offer
        (`policy.limitation`), and `ehr_declaration` that declaration's stamp: then AUTO, and a
        rule's BLOCK, become HUMAN_REVIEW, the limitation a reason whatever the outcome (an
        ABSTAIN over unread data says it too, and a refused input's BLOCK, without "a person
        decides")."""
        inputs = well_formed(inputs)  # half a character is read as U+FFFD, wherever it is kept
        unread = tuple(sorted(set(unreadable)))
        unread_reasons = tuple(f"could not read {key}: {reason}" for key, reason in unread)
        started = time.perf_counter()
        manifest = self.agent.manifest
        if self.store is not None and manifest.audit.store_input == "full":
            self.store.ready_to_keep()  # before a provider (or a quota) sees the case
        answers: dict[str, Answer] = {}
        failures: dict[str, str] = {}
        tokens_in = tokens_out = 0
        cost: float | None = 0.0
        canned: str | None = None  # why an answer is canned (`ProviderOutput.canned`), if one is
        policy_inputs: PolicyInputs | None = None  # what the policy decides from, when it does
        proposed: list[Proposed] = []  # the actions the agent asks for (spec §13.6), in order
        written: dict[str, str] = {}  # what the agent wrote (spec §15.2), by the output's name
        checked: list[Checked] = []  # each held against the manifest, once the providers answered
        vouched = True
        signals = Signals()  # what its providers found that can only lower automation
        meter = Meter()  # its remote calls, as they are made

        governance = self.governance
        if governance is None and self.governance_source is not None:
            governance = self.governance_source(self.agent)
        over_quota: ProviderError | None = None
        stopped: ProviderError | None = None  # a bounded agent's schema check used the budget
        if self.budget is not None:
            try:
                self.budget.admit_decision()
            except ProviderError as error:
                over_quota = error  # every provider fails with it, so nothing automates
        # The patterns of an agent on the bounded engine (`Agent.bounded`: one from a registry or
        # a store) share one time budget in this decision (`core/patterns.py`): past
        # it its schema check or its rules stop, and the decision abstains, for a person.
        bounded = self.agent.bounded
        with time_budget() if bounded else nullcontext():
            reader = bounded_reader(self.agent.origin)
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
                    f"{too_long}: longer than {MAX_BOUNDED_TEXT_CHARS:,} characters, the most "
                    f"{reader} reads"
                ]
            else:
                try:
                    schema_problems = self.validate(inputs)
                except OutOfTime as error:
                    # The agent's schema used the budget: not the input's fault, so no refusal.
                    # Every slot fails as a provider that cannot answer does, for a person.
                    schema_problems = []
                    stopped = ProviderError("time_budget", f"its schema check stopped: {error}")
            problems = [*rejected, *schema_problems]
            if problems:
                # A refusal is final, so no person decides; a list read without a source the EHR
                # does not offer is still said to be incomplete.
                policy = PolicyResult(
                    outcome=Outcome.BLOCK,
                    recommendation=None,
                    priority=manifest.policy.default_priority,
                    reasons=(
                        *(f"{REJECTED}{problem}" for problem in problems),
                        *unread_reasons,
                        *dict.fromkeys(not_offered),
                    ),
                )
            elif unread:
                # Part of the record could not be read: a person decides, and no provider is
                # asked about a record known to be incomplete (policy only lowers automation).
                policy = PolicyResult(
                    outcome=Outcome.ABSTAIN,
                    recommendation=None,
                    priority=manifest.policy.default_priority,
                    reasons=(
                        *unread_reasons,
                        *(f"{limit}: a person decides" for limit in dict.fromkeys(not_offered)),
                    ),
                )
            else:
                over = OVER_QUOTA.set(over_quota)  # remote calls fail with it; local ones run
                metering = METER.set(meter)
                try:
                    # The deadline's clock starts here, for this decision's remote calls, or
                    # before its linked reads (`decide_fhir`), which then share it: past it they
                    # fail with `timeout`, and local providers still answer.
                    started_clock = (
                        decision_deadline(self.deadline_s)
                        if due_at is None
                        else deadline_until(due_at)
                    )
                    with started_clock, batching(self.providers.values()):
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
                            canned = canned or output.canned
                            proposed.extend(output.actions)
                            written.update(output.outputs)
                            signals = signals.merge(output.signals)
                            tokens_in += output.input_tokens
                            tokens_out += output.output_tokens
                            if cost is not None:
                                cost = None if output.cost_usd is None else cost + output.cost_usd
                finally:
                    METER.reset(metering)
                    OVER_QUOTA.reset(over)
                if any(m.calls > m.priced for m in meter.totals().values()):
                    # A call sent that no answer priced (it failed, or its answer was refused)
                    # may still be billed: the cost is unknown, never zero (as in the ledger).
                    cost = None
                expected_scores(manifest, answers)
                variants = {slot: info.variant for slot, info in self.provider_info.items()}
                hold = self.hold_source(self.agent) if self.hold_source is not None else None
                origin_holds = self._origin_holds()
                shadow_note = "shadow mode: held for clinician comparison" if shadow else None
                hold = "; ".join(filter(None, [*origin_holds, hold, shadow_note])) or None
                if over_quota is not None:
                    hold = "; ".join(
                        filter(None, ["this organization's daily decisions quota is used up", hold])
                    )
                # Each proposal against what the manifest declares (`core/actions.py`): one
                # that could not be released with AUTO lowers the decision, no more.
                checked = [
                    actions.check(manifest, self._action_checkers, proposal)
                    for proposal in actions.distinct(proposed)
                ]
                vouched = not origin_holds
                policy_inputs = PolicyInputs(
                    answers,
                    failures,
                    governance,
                    variants,
                    hold=hold,
                    blocks_to_person="; ".join(origin_holds) or None,
                    site_code=tuple(translated),
                    not_offered=tuple(not_offered),
                    canned=canned if self.canned_cap else None,
                    actions=action_stops(checked, vouched=vouched),
                    caps=signals.reasons(),
                )
                policy = policy_inputs.apply(manifest)

        # What may be done with each proposed action, under the policy's final word. None for
        # an agent that declares none and proposed none.
        acted: tuple[ActionResult, ...] | None = None
        if manifest.actions or checked:
            acted = self._acted(checked, policy, vouched)
        # An input refused for NaN or an infinity (`validate`) is kept as JSON holds it from here:
        # its hash, its record and its sealed copy agree, and any JSON reader reads them.
        inputs = kept_as_json(inputs)
        # Keyed under the deployment's input hash key when it has one (`core/phi.py`).
        hashed = input_hash(inputs) if self.store is None else self.store.phi.input_hash(inputs)
        result = DecisionResult(
            id=str(uuid.uuid4()),
            created_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
            agent_id=self.agent.id,
            agent_version=self.agent.version,
            agent_digest=self.agent.digest,
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
            data_coverage=None if data_coverage is None else dict(data_coverage),
            site_codes=None if site_codes is None else dict(site_codes),
            ehr_declaration=None if ehr_declaration is None else dict(ehr_declaration),
            rejected=bool(problems),
            schema_problems=tuple(schema_problems),
            labels=dict(labels or {}),
            usage=meter.totals(),
            unreadable=unread,
            limitations=tuple(dict.fromkeys(not_offered)),
            promotion=self.promotion,
            policy_inputs=policy_inputs,
            actions=acted,
            signals=signals,
            outputs=written,
        )
        if self.store is not None:
            self.store.record(result, inputs, store_input=manifest.audit.store_input)
        return result

    def decide_fhir(
        self,
        resources: Mapping[str, Any],
        *,
        focus: Mapping[str, Any] | None = None,
        unreadable: Mapping[str, str] | None = None,
        sources: Mapping[str, str] | None = None,
        links: Linker | None = None,
        not_offered: Mapping[str, str] | None = None,
        declaration: EhrDeclaration | None = None,
        statuses: Mapping[str, int] | None = None,
        shadow: bool = False,
    ) -> tuple[DecisionResult, dict[str, Any]]:
        """Decide from FHIR R4 resources (a Bundle or {resourceType: resource}); return a Task.
        `focus` picks the resource to decide about, e.g. one of several draft orders.

        Input that is not FHIR R4 JSON (an unknown element or resource type, a wrong JSON type, a
        malformed primitive) raises `FhirStructureError` whatever the mode. A complex element R4
        requires, a code outside its required value set, and US Core's own rules are issues under
        `fhir_validation`: under `warn` they are recorded in the result; under `enforce` an error
        blocks the input before any provider.

        `unreadable` names the prefetch keys that could not be read, each with its reason
        (`errors.ReadReason`), and `sources` the prefetch templates they were read with (the
        agent's own by default), so a required resource whose read failed is not refused as
        missing. A key that still holds what the client sent (a value that is not a resource)
        excuses nothing: a required resource it does not hold is refused, as before. Those keys,
        and a note whose data does not decode or that is only a link (for an
        `attachment_text` note, also one of a type it cannot read: `unreadable_notes`, and notes
        whose joined text is longer than the agent's input schema lets the field be, left out of
        the input: `fhir.overlong`, `too_large`), make the decision ABSTAIN unless its input is
        refused (`decide`). The result carries its data-coverage record
        (`standards/data_coverage.py`) in every case.

        `links` (a CDS hook's, the SMART launch's decide: LP-23h) reads what the mapping
        reaches by a link the record does not hold, a note's Binary or a followed reference,
        from the EHR's FHIR server, into the record before it is mapped (`standards/linked.py`),
        inside the decision's deadline, which starts before those reads and its providers then
        share (a note field stops reading once its text is longer than the input takes);
        what it reads is checked as the record is (its R4 and profile issues recorded, by type
        alone in the stored record, refused under `enforce`). A link it did not read, and
        without `links` every link the record does not hold (`not_fetched`), makes the decision
        ABSTAIN too (`unreadable_links`).

        With a site code map for the organization it acts for (`site_map`, LP-23i), the record
        is read, and its data coverage measured, with that map's translations, and the result
        records the map's version and digest (`site_codes`); a decision whose input took a value
        through a translation never automates, and its rule's BLOCK goes to a person. So does
        one about a focus that the mapping's `focus_where` code set found only through a
        translation (LP-23k): which resources of the focus type count is read with the map too.

        `not_offered` names the prefetch keys left out because the site declared that its EHR
        does not offer their type (`declaration`, `standards/ehr_declarations.py`; a SMART
        launch's or a hook's, by its FHIR base and organization), each with that type. Each must
        be a key of `sources` holding nothing, of a type the declaration names and the agent may
        read its list without (`ehr_declarations.excusable`): anything else is the caller's
        error (`ValueError`), never the request's. A required resource of that type is not
        refused as missing, nothing abstains on it, the data-coverage record marks the source and
        its list `not_offered`, the result records the declaration (`ehr_declaration`), and the
        decision never automates: AUTO, and a rule's BLOCK, become HUMAN_REVIEW, the limitation
        a reason whatever the outcome. `statuses` is the HTTP status of each `unreadable` key
        whose read failed with one, which the data-coverage record keeps (a number only)."""
        from mendari.standards import data_coverage, fhir_structure, us_core
        from mendari.standards.fhir import TOO_LARGE, FhirAdapter, overlong, text_limits

        resources = well_formed(resources)  # as `decide` reads its input
        focus = well_formed(focus)
        adapter = FhirAdapter.for_agent(self.agent)
        failed = dict(unreadable or {})
        hooks = self.agent.manifest.cds_hooks
        templates = dict(sources if sources is not None else (hooks.prefetch if hooks else {}))
        excused = {
            data_coverage.source_type(templates[key])
            for key in failed
            if key in templates and resources.get(key) is None
        }
        left_out = dict(not_offered or {})
        if left_out:
            _check_not_offered(adapter.mapping, templates, resources, left_out, declaration)
            excused |= set(left_out.values())
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
        site = self.site_map()
        translations = None if site is None else site.sets
        indexed = adapter.index(resources, focus=focus, excused=excused, site_codes=translations)
        due_at = None
        if links is not None:
            # The decision's deadline starts before its linked reads, which its providers share
            # (`providers/deadline.py`); the audit write is outside it.
            due_at = due(self.deadline_s)
            limits = text_limits(adapter.mapping, self.agent.input_schema)
            with deadline_until(due_at):
                read = links.read(adapter.mapping, indexed, limits=limits)
            if read and self.fhir_validation != "off":
                issues += fhir_structure.merge(fhir_structure.check(read), us_core.validate(read))
        rejected = []
        if self.fhir_validation == "enforce":
            rejected = [
                f"not {us_core.standard(issue.profile)}: {issue.resource} {issue.expression}: "
                f"{issue.diagnostics}"
                for issue in us_core.errors(issues)[:10]
            ]
        notes = data_coverage.unreadable_notes(adapter.mapping, indexed)
        unlinked = data_coverage.unreadable_links(adapter.mapping, indexed)
        mapped = adapter.read(indexed, site_codes=translations)
        # Notes longer in all than the input allows are left out and abstain, never refused.
        too_long = overlong(adapter.mapping, mapped.value, self.agent.input_schema)
        unread = [
            *failed.items(),
            *(
                (field, reason)
                for found in (notes, unlinked)
                for field, counts in found.items()
                for reason in counts
            ),
            *((field, TOO_LARGE) for field in too_long),
        ]
        coverage = data_coverage.measure(
            adapter.mapping,
            indexed,
            unreadable=failed,
            systems=[use.url for use in self.agent.manifest.code_systems],
            sources=templates,
            site_codes=translations,
            too_long=too_long,
            links=None if links is None else links.used(indexed),
            not_offered=left_out,
            statuses=statuses,
        )
        limited: list[str] = []
        if left_out and declaration is not None:
            types = set(left_out.values())
            for name, spec in adapter.mapping.lists.items():
                without = [kind for kind in spec.resources or {} if kind in types]
                if without:
                    limited.append(limitation(name, without, declaration.attested_by))
        result = self.decide(
            mapped.value,
            input_issues=[issue.to_dict() for issue in issues],
            rejected=rejected,
            labels=us_core.record_labels(given, issues),
            unreadable=unread,
            data_coverage=coverage,
            site_codes=None if site is None else site.stamp(),
            translated=mapped.translated,
            due_at=due_at,
            not_offered=limited,
            ehr_declaration=None if not limited or declaration is None else declaration.stamp(),
            shadow=shadow,
        )
        return result, adapter.to_task(result, indexed)


def _check_not_offered(
    mapping: FhirMapping,
    templates: Mapping[str, str],
    resources: Mapping[str, Any],
    not_offered: Mapping[str, str],
    declaration: EhrDeclaration | None,
) -> None:
    """`decide_fhir`'s `not_offered`, as its callers must give it (a `ValueError` otherwise: the
    caller's error, never a request's): with its declaration, each a key of the templates that
    holds nothing, of the type its template reads, which the declaration names and the agent may
    read its list without."""
    from mendari.standards.data_coverage import source_type
    from mendari.standards.ehr_declarations import excusable

    if declaration is None:
        raise ValueError("not_offered needs the declaration that says so")
    allowed = excusable(mapping, templates, declaration.not_offered)
    for key, kind in not_offered.items():
        if key not in templates or source_type(templates[key]) != kind:
            raise ValueError(f"not_offered {key!r} is not a prefetch key of type {kind}")
        if resources.get(key) is not None:
            raise ValueError(f"not_offered {key!r} holds a value")
        if kind not in allowed:
            raise ValueError(f"not_offered {key!r}: {kind} may not be left out for this agent")


def expected_scores(manifest: AgentManifest, answers: MutableMapping[str, Answer]) -> None:
    """Each score question's answer with its expected score (`Question.expected_score`), when
    its provider gave none."""
    for key, question in manifest.questions.items():
        answer = answers.get(key)
        if answer is not None and question.type == "score" and answer.value is None:
            answers[key] = replace(answer, value=question.expected_score(answer.probabilities))


_FALLBACK = (Outcome.ABSTAIN, Outcome.BLOCK)
_REVIEWED = (Outcome.HUMAN_REVIEW, Outcome.ABSTAIN)  # the outcomes that await a review


def _review_of(row: Mapping[str, Any]) -> Review:
    """Where a recorded decision's review stands (`policy.Review`), from its row: awaited;
    resolved with the recommendation confirmed or overridden (an abstention's review, which had
    no recommendation, reads as overridden: nothing was confirmed); or none needed."""
    status = row["review_status"]
    if status == "pending":
        return "pending"
    if status == "resolved":
        return "confirmed" if row["human_override"] in (0, False) else "overridden"
    return "none"


_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}\Z")


def _not_finite(inputs: Any) -> list[str]:
    """Where an input holds a number JSON cannot carry, NaN or an infinity: Python's json reads
    them (and so the server does), but no schema keyword bounds them, as every comparison with
    NaN is false, and no provider can send them on. The first ten the input holds, each place
    once (`_where`), sorted as the schema's reasons are. Walked without recursion, each
    container once, with a path built only for a number found."""
    found: list[list[Any]] = []
    links: list[tuple[int, Any]] = []  # each value's container (its index here, -1: none) and key
    stack: list[tuple[Any, int]] = [(inputs, -1)]
    seen: set[int] = set()  # by identity: a container held twice (or in itself) is walked once
    while stack and len(found) < 10:
        value, at = stack.pop()
        if isinstance(value, float) and not math.isfinite(value):
            path: list[Any] = []
            while at >= 0:
                at, key = links[at]
                path.append(key)
            found.append(path[::-1])
        elif isinstance(value, Mapping | list | tuple) and id(value) not in seen:
            seen.add(id(value))
            items = list(value.items() if isinstance(value, Mapping) else enumerate(value))
            for key, item in reversed(items):  # taken off the stack in the input's own order
                links.append((at, key))
                stack.append((item, len(links) - 1))
    shown = sorted(found, key=lambda path: list(map(str, path)))
    return list(dict.fromkeys(map(_where, shown)))


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
    agent's own schema), never from the instance. The patterns of an agent on the bounded engine
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
    if keyword == "unevaluatedProperties":
        return "has a field the schema does not allow"
    if keyword == "additionalProperties":
        schema = error.schema if isinstance(error.schema, Mapping) else {}
        allowed = set(schema.get("properties") or {})
        patterns = list(schema.get("patternProperties") or {})
        extra = [
            str(key)
            for key in present
            if key not in allowed
            and not any(search(p, str(key), bounded=bounded) for p in patterns)
        ]
        named = [key for key in extra if _KEY.match(key)]
        shown = named[:5]
        omitted = len(extra) - len(shown)
        listed = [*shown, *([f"{omitted} more"] if omitted and shown else [])]
        return (
            f"has fields the schema does not allow: {', '.join(listed)}"
            if shown
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


def kept_as_json(inputs: Mapping[str, Any]) -> Mapping[str, Any]:
    """`inputs` with each NaN and infinity as null, the same mapping when it holds none (all but
    an input `Decision.validate` refuses for one). Python's json writes them as bare `NaN` and
    `Infinity` tokens, which no other JSON reader parses: what is hashed, recorded or sealed
    of an input is kept as JSON any reader parses, the reason naming each place."""
    if not _not_finite(inputs):
        return inputs
    return dict(json.loads(json.dumps(inputs, default=str), parse_constant=lambda _: None))


def input_hash(inputs: Any) -> str:
    """The unkeyed `sha256:` of an input, for a decision made without a store (a store keys it
    under its deployment's input hash key, `PhiPolicy.input_hash`)."""
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
