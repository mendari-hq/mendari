# SPDX-License-Identifier: Apache-2.0
"""HTTP surface: REST decisions, FHIR in/out, CDS Hooks, review queue, audit, evals, dashboard.

v0.1 has no authentication of its own and is meant for synthetic data only. Locally it binds
to localhost; on Vercel it sits behind Vercel Authentication (deployment protection).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import math
import os
import re
import threading
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from html import escape
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, urlsplit

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware import Middleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mendari import __version__, guard, notices, settings
from mendari.alerts import AlertSettings, alert_settings, check_webhook, deliver, retry_due
from mendari.core import accounts, actions, checkpoint, clock, packet, tenancy, validation_brief
from mendari.core.audit import (
    ADMINISTRATIVE_KINDS,
    OFF_DECISION_KINDS,
    SYSTEM_ID,
    AuditStore,
    usage_totals,
)
from mendari.core.card import benchmark, card_of
from mendari.core.checkpoint import checkpoint_key, key_id, public_text
from mendari.core.connections import ConnectionStore
from mendari.core.connections import enabled as connections_enabled
from mendari.core.decision import CallBudget, Decision, QuotaReached
from mendari.core.evalset_keys import evalset_keys, require_evalset_deployment
from mendari.core.evalsets import NO_SET, EvalsetStore, GradingRead, pack_problem
from mendari.core.https_only import is_local_host
from mendari.core.origin import is_local
from mendari.core.pack import Pack
from mendari.core.pack_store import PackStore
from mendari.core.phi import phi_policy, require_protection
from mendari.core.policy import GATE_TEXT, GovernanceOverride, gate_of
from mendari.core.registry import SearchPath, discover, version_key
from mendari.core.site_codes import site_codes
from mendari.core.written import WrittenStore
from mendari.errors import (
    AuthError,
    CdsHooksError,
    ClientError,
    FhirError,
    FhirSourceNotFound,
    FhirStructureError,
    GradingRefused,
    InvitationRefused,
    LabelError,
    MendariError,
    NoAssignment,
    NoEvalset,
    NoOrganization,
    NoteError,
    NoticeNotAcknowledged,
    PackNotFoundError,
    PackValidationError,
    PrefetchRequired,
    ProviderError,
    ReviewError,
    ScopeError,
    SmartError,
    UnknownCard,
)
from mendari.evals import compare, figures, leaderboard, load_dataset, resolve_dataset
from mendari.evals.compare import FROM_REGISTRY, not_compared
from mendari.evals.feedback import feedback_dataset, reviewed_dataset
from mendari.evals.harness import Dataset, evaluation
from mendari.evals.hidden import (
    Declared,
    HiddenResult,
    HiddenScoreboard,
    configuration_of,
    graded,
    holds,
    scoreboard_graded,
    scoreboard_holds,
)
from mendari.evals.monitoring import MAX_DAYS as MONITOR_MAX_DAYS
from mendari.evals.monitoring import MIN_DAYS as MONITOR_MIN_DAYS
from mendari.evals.monitoring import monitor, post_alerts
from mendari.evals.monitoring import organizations as monitored_organizations
from mendari.evals.pack_tests import load_cases
from mendari.providers import jev, llm, provider_allow_list, slots_without_model
from mendari.providers.agent import ENDPOINT_ENV as AGENT_ENDPOINT_ENV
from mendari.providers.agent import AgentProvider, Connection
from mendari.providers.credentials import key_source
from mendari.server import api_docs, landing, security_log, sessions, system
from mendari.server.auth import (
    OPEN,
    Auth,
    Principal,
    add_auth_routes,
    auth_settings,
    challenge,
    credential_in_url_or_form,
    names_a_credential,
    org_of,
    read_body,
    scope_of,
)
from mendari.server.backend_services import (
    DISCOVERY_PATH,
    TOKEN_PATH,
    add_backend_routes,
    backend_settings,
    oauth_error,
    throttled,
)
from mendari.server.cds_clients import CdsOpenMode, cds_clients, cds_sandbox, fhir_server_allowed
from mendari.server.connection_routes import add_connection_routes
from mendari.server.cors import Cors
from mendari.server.hub_routes import add_hub_routes
from mendari.server.limits import DailyCap, Limiter, RateLimited, client_ip, limit_settings
from mendari.server.origin import OriginCheck, trusted_origins
from mendari.server.readiness import Readiness, packs_state
from mendari.server.recalls import RecallWatch
from mendari.server.smart_routes import add_smart_routes, error_page
from mendari.server.system_routes import add_system_routes, scheduled_run
from mendari.settings import FhirValidation, applies_to, providers_for
from mendari.standards import (
    audit_fhir,
    capability,
    cds_hooks,
    davinci,
    decide_op,
    fhir_search,
)
from mendari.standards.ehr_declarations import ehr_declarations
from mendari.standards.fhir_client import allowed_servers
from mendari.standards.search import SPECS, SearchError, encode_cursor, handling, searchset
from mendari.standards.search import parse as parse_search
from mendari.standards.search import stored as stored_instant
from mendari.standards.smart import registration, smart_ehrs, smart_settings
from mendari.standards.terminology import Terminology

DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "dashboard"
VENDOR_DIR = DASHBOARD_DIR / "vendor"  # third-party front-end code (scripts/vendor_docs_ui.py)
DEMO_AGENT = "mendari/portal-message-agent"  # the one agent a release bundles
JsonBody = Annotated[dict[str, Any], Body()]
# The agent a request names by `pack_id`: the parameter keeps its name on the wire, and the API
# description shows it as what it is.
AgentId = Annotated[str, Query(title="Agent id")]
AgentFilter = Annotated[str | None, Query(title="Agent id")]
_DATASET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_STATUS = {
    PackNotFoundError: 404,
    ReviewError: 409,
    NoteError: 422,
    FhirStructureError: 400,  # before FhirError: the first match wins
    FhirSourceNotFound: 422,  # issue type not-found: unknown or another organization's, one answer
    PrefetchRequired: 412,  # CDS Hooks 2.0: neither the prefetch nor a fhirServer to read it from
    FhirError: 422,
    PackValidationError: 422,
    ScopeError: 422,
    ClientError: 422,
    NoAssignment: 404,  # unknown, another's or closed: one answer (LP-11j)
    LabelError: 422,  # names the question, never the value
    NoEvalset: 404,  # unknown or another organization's set: one answer (LP-11k)
    CdsHooksError: 400,
    UnknownCard: 404,  # card feedback: unknown, another's or another caller's, one answer
    RateLimited: 429,
}
# A provider that cannot answer (e.g. listing Jev's models): not set up here, or the upstream
# service failed; never a client error.
_PROVIDER_STATUS = {
    "not_configured": 503,
    "credential_unavailable": 503,
    "insufficient_credit": 503,
    "budget_exhausted": 429,
    "quota_exhausted": 429,
    "invalid_input": 400,
}
_PROVIDER_UPSTREAM = 502
# FHIR routes answer errors with an OperationOutcome: its issue type, by HTTP status.
_FHIR_ROUTE = re.compile(r"^/(fhir/|api/agents/[^/]+/[^/]+/fhir/)")


def fhir_route(path: str) -> bool:
    """Whether `path` is a FHIR route: under `/fhir/`, or an agent's `fhir/decide`. FHIR routes
    take and answer FHIR R4 JSON only, and answer an error with an OperationOutcome."""
    return _FHIR_ROUTE.match(path) is not None


_ISSUE_TYPE = {
    400: "invalid",
    401: "login",
    403: "forbidden",
    404: "not-found",
    405: "not-supported",
    409: "conflict",
    413: "too-costly",
    422: "processing",
    429: "throttled",
    502: "transient",
    503: "not-supported",
}


class RegistryApprovalRequest(BaseModel):
    pack_id: str = Field(min_length=3, max_length=200, title="Agent id")
    # The digest the approver reviewed: the approval names exactly it.
    pack_digest: str = Field(min_length=8, max_length=100, title="Agent digest")
    reference: str = Field(min_length=3, max_length=300, description="e.g. committee minutes")
    approver: str | None = Field(
        default=None,
        max_length=200,
        description="open mode only, an identifier (no spaces, at most 100 characters); with "
        "sign-in on, the approval is recorded for whoever is signed in",
    )


class ApprovalRequest(BaseModel):
    pack_id: str = Field(min_length=3, max_length=200, title="Agent id")
    reference: str = Field(min_length=3, max_length=300, description="e.g. committee minutes")
    days: int = Field(default=180, ge=1, le=365, description="how long the approval lasts")
    approver: str | None = Field(
        default=None,
        max_length=200,
        description="open mode only, an identifier (no spaces, at most 100 characters); with "
        "sign-in on, the approval is recorded for whoever is signed in",
    )


# A review's or a rating's note (core/audit.py, "notes"), as the API reference describes it.
_NOTE = (
    "free text, kept apart from the audit chain (which keeps a commitment to it) and readable "
    "as inputs are; refused with 422 under PHI mode hash, which keeps no free text"
)


class ReviewRequest(BaseModel):
    final_label: str = Field(min_length=1, max_length=200)
    reviewer: str = Field(
        min_length=1,
        max_length=200,
        description="open mode: who reviews, an identifier (no spaces, at most 100 characters); "
        "with sign-in on, the review is recorded for whoever is signed in, whatever this says",
        examples=["sandbox-nurse"],
    )
    note: str | None = Field(
        default=None,
        max_length=2000,
        description=_NOTE,
    )
    deny_actions: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        default=[],
        max_length=actions.MAX_PROPOSED * 4,
        description="ids of this decision's held actions to refuse while confirming its "
        "recommendation: each is denied (`reviewer_denied`), the others are released, and "
        "`human_override` stays what the label makes it. An id that is no held action of this "
        "decision refuses the review (409)",
    )


class ActionRequest(BaseModel):
    """One action to authorize for a decision already recorded: the decision's id, the action's
    name as the agent's manifest declares it, and its arguments."""

    decision_id: str = Field(min_length=1, max_length=100)
    name: str = Field(
        pattern=actions.NAME_PATTERN,
        description="the action, as the agent's manifest declares it",
        examples=["assign_queue"],
    )
    arguments: dict[str, Any] = Field(
        default={},
        description="the arguments the action would be run with: checked against the action's "
        f"schema, at most {actions.MAX_ARGUMENT_CHARS:,} characters as JSON; kept as inputs "
        "are, never in the audit chain, which keeps their hash",
    )


class OutcomeRequest(BaseModel):
    final_label: str = Field(
        min_length=1, max_length=200, description="one of the labels of the decision's question"
    )
    source: str = Field(
        min_length=1,
        max_length=100,
        pattern=SYSTEM_ID.pattern,
        description='the system that observed it, as an identifier (e.g. "ehr", "mendari"), '
        "never free text: it is chained and exported",
        examples=["ehr"],
    )


class ExpectedAnswer(BaseModel):
    question: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=200)


class FeedbackRequest(BaseModel):
    rating: Literal["up", "down"]
    reporter: str | None = Field(
        default=None,
        max_length=200,
        description="open mode: who rates, an identifier (no spaces, at most 100 characters); "
        "with sign-in on, the rating is recorded for whoever is signed in",
    )
    note: str | None = Field(default=None, max_length=1000, description=_NOTE)
    # On a thumbs-down, what the answer to one of the decision's questions should have been.
    expected: ExpectedAnswer | None = None

    @model_validator(mode="after")
    def _right_answer_only_when_wrong(self) -> FeedbackRequest:
        if self.expected is not None and self.rating != "down":
            raise ValueError("only a thumbs-down carries the right answer (expected)")
        return self


class EvalRequest(BaseModel):
    dataset: str = Field(min_length=1, max_length=200)
    limit: int | None = Field(default=None, ge=1)
    # Evaluate another configuration than the deployed one (e.g. for the leaderboard); the
    # remote-call cap and daily budgets still apply.
    providers: dict[str, str] | None = None


def _local_variant(pack: Pack, slot: str, variant: str) -> bool:
    """Whether `variant` of a pack's provider `slot` decides on this machine (`is_local`); a
    slot or variant the pack does not have is left for `Decision` to refuse."""
    spec = pack.manifest.providers.get(slot)
    config = spec.variants.get(variant) if spec is not None else None
    if config is None:
        return True
    return is_local({"type": config.type, **(config.model_extra or {})})


# Evaluations that call a metered, network-bound provider must fit one function invocation.
REMOTE_EVAL_LIMIT = 200
REMOTE_EVAL_WORKERS = 8


# A name the client gives in open mode (reviewer, reporter, approver): it is chained for good, so
# an identifier ("sandbox-nurse", an email), never a sentence.
NAME_ID = re.compile(r"[^\s\x00-\x1f\x7f-\x9f]{1,100}")


def acting_as(request: Request, claimed: str | None, field: str = "reviewer") -> str | None:
    """Who a review, rating or approval is recorded for: with sign-in on, the signed-in person or
    API key as the chain names them (`Principal.actor`), whatever the request body says; in open
    mode, the name the client gives, which must be an identifier: no spaces, at most 100
    characters (`422` otherwise)."""
    principal = getattr(request.state, "principal", None)
    if principal is not None and principal.kind != "open":
        return str(principal.actor)
    if claimed is not None and not NAME_ID.fullmatch(claimed):
        raise HTTPException(
            422, f"{field} must be an identifier: no spaces, at most 100 characters"
        )
    return claimed


def shown_to_reader(item: Mapping[str, Any]) -> bool:
    """Whether `reveal_inputs` showed a reader an item's stored input, what its record holds
    itself, or one of its notes, which it chains as an `input.read` (`input_read`)."""
    return item.get("input_read") is True


# How long a decision's look-up of automation holds is reused on one instance.
HOLD_SECONDS = 5.0
# The promoted store versions' Decisions one process keeps built (LP-20h), oldest dropped first.
STORE_BUILT_MAX = 64
# The connections one process keeps read for HOLD_SECONDS (ADR-0021): past this many, all are
# read again.
CONNECTION_CACHE_MAX = 4096
# The scheduled jobs' bearer secret (CRON_SECRET, as Vercel Cron sends it): its least length.
MIN_CRON_SECRET = 16
# The longest a quota's alert holds the decision that reached the quota: one try.
QUOTA_ALERT_TIMEOUT_S = 5.0
# The scheduled monitor's time (the function's `maxDuration` is 300 s, vercel.json). It sends
# pending alerts again for at most MONITOR_RETRY_SECONDS, then starts no organization after
# MONITOR_SECONDS: the last one started keeps time for its monitoring, its alert (three tries
# of 10 s, 10 s of waits between them) and the alert naming those left, which an alert says
# were not reached. Each day starts at another organization, so none is left every day.
MONITOR_RETRY_SECONDS = 60.0
# How long one instance reuses the newest signed checkpoint it read (`/api/audit/checkpoint`).
CHECKPOINT_SECONDS = 60.0
MONITOR_SECONDS = 180.0
MONITOR_NAMED = 20  # organizations the alert of those not reached names (ids), at most


class EraseRequest(BaseModel):
    reason: Literal["request", "error"]


class HoldRequest(BaseModel):
    pack_id: str | None = Field(default=None, max_length=200, title="Agent id")
    pack_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$", title="Agent digest"
    )
    reason: str = Field(min_length=1, max_length=200)


# A labeller's routes (LP-11j): what they send, and closed answers (`extra="forbid"`), so a field
# the store might one day add to what it returns fails the answer rather than reaching a labeller.
class LabelRequest(BaseModel):
    labels: dict[str, str] = Field(
        description="each question the case asks, with one of its labels (as the case gave them)",
        examples=[{"request": "refill", "urgent_symptoms": "no"}],
    )


class LabellingCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assignment: str = Field(description="what to send your label to")
    questions: dict[str, list[str]] = Field(description="each question, with its labels")
    input: Any = Field(description="the case, as the agent reads it")


class LabelledSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    set: str
    open: int = Field(description="your cases waiting in this set")
    labelled: int = Field(description="your cases labelled in this set")


class LabellingCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sets: list[LabelledSet]
    remaining: int = Field(description="your cases waiting, in all")


class LabelRecorded(BaseModel):
    model_config = ConfigDict(extra="forbid")

    remaining: int = Field(description="your cases still waiting")


# A grader's routes over written output (ADR-0025): closed answers, as a labeller's are, so a
# judge's grade or another grader's added to what the store returns fails the answer rather
# than reaching a grader. A grader sends a `LabelRequest` and is answered a `LabelRecorded`.
class WrittenText(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="the output's name, as the agent's manifest declares it")
    description: str = Field(description="what the text is, in the manifest's words")
    text: str = Field(description="the text to grade, as the agent wrote it")


class CriterionGuidance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instructions: str = Field(description="what the criterion asks")
    display: str | None = Field(description="its name as a person reads it, if it has one")
    levels: dict[str, str] = Field(description="each level, with what it means")


class WrittenCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assignment: str = Field(description="what to send your grades to")
    questions: dict[str, list[str]] = Field(description="each criterion, with its levels")
    guidance: dict[str, CriterionGuidance] = Field(description="each criterion's own words")
    written: WrittenText
    input: Any = Field(description="what the agent was given: the fields the rubric names")


class WrittenRound(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    pack_id: str = Field(title="Agent id")
    pack_digest: str = Field(title="Agent digest")
    rubric: str
    rubric_sha256: str = Field(description="the rubric's wording its grades are bound to")
    state: Literal["open", "closing", "closed"]
    created_at: str
    closed_at: str | None
    items: int = Field(description="the outputs kept to grade")
    graded: int = Field(description="grades given so far")
    open: int = Field(description="assignments still waiting for their grader")
    note: str | None = Field(
        description="what its state needs said: a round left closing, with how to finish it; "
        "or that its stored report does not match the audit trail, and is not shown"
    )
    report: dict[str, Any] | None = Field(
        description="the round's report (`mendari.written/1`), once it is closed and while it "
        "is the one the audit trail committed to; null otherwise"
    )


# An organization's curators' routes over its hidden evaluation sets (LP-11k): what they send,
# and closed answers (`extra="forbid"`) down to the stored aggregates' last field, so a field the
# store might one day add to what it returns (a case, an input, a label given) fails the answer
# rather than reaching anyone. People are named by user id only (`accounts.USER_ID`).
UserId = Annotated[str, Field(pattern=r"^user:[0-9a-f]{16}$")]
SetState = Literal["draft", "frozen", "active", "retired"]
Seed = Annotated[
    int, Field(ge=0, le=2**32 - 1, description="the same set and seed give the same result")
]


class SampleRequest(BaseModel):
    per_stratum: int | None = Field(
        default=None,
        ge=1,
        le=1_000_000,
        description="how many more cases to draw from each stratum (all that are left of a "
        "smaller one); or `cases`, not both",
    )
    cases: int | None = Field(
        default=None,
        ge=1,
        le=1_000_000,
        description="how many more cases to draw in all, shared out among the strata by the "
        "cases left in each; or `per_stratum`, not both",
    )
    seed: Seed = 0


class LabellersRequest(BaseModel):
    labellers: list[Annotated[str, Field(max_length=64)]] = Field(
        min_length=1,
        max_length=500,
        description="the labellers' user ids, as the members list shows them: each with the "
        "labeller role in this organization, and none an administrator",
        examples=[["user:0123456789abcdef", "user:fedcba9876543210"]],
    )
    seed: Seed = 0


class EvalsetListed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    set: str
    name: str
    pack: str = Field(title="Agent")
    state: SetState
    created_at: str
    cases: int = Field(description="the cases the set holds")


class CaseStatuses(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft: int = Field(description="not drawn")
    sampled: int = Field(description="drawn, waiting for its two first labels")
    agreed: int
    disputed: int = Field(description="its two first labels differ: waiting for a third")
    resolved: int = Field(description="settled by the majority, once adjudicated")
    unresolved: int = Field(description="three labels, no majority: keeps the set from freezing")


class RoundCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assigned: int
    labelled: int
    open: int
    withdrawn: int


class LabellerCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first: RoundCounts
    adjudication: RoundCounts


class RunsCount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    used: int = Field(description="grading runs made (frozen and active sets)")
    left: int | None = Field(description="grading runs left; null before the set is frozen")


class LabelCount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cases: int
    distinct: int


class QuestionCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    labelled: int
    labels: dict[str, LabelCount]


class CriticalCount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str
    label: str
    cases: int
    distinct: int
    insufficient: bool


class CoverageMix(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: dict[str, int]
    source: dict[str, int]
    stratum: dict[str, int]


class DistinctCount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exact: int
    normalised: int


class NamedDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="the set's id: no hash of its cases, keyed or not")


class CoverageReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["mendari.coverage/1"]
    pack: str = Field(title="Agent")
    dataset: NamedDataset
    norm: str
    cases: int
    distinct: DistinctCount
    duplicate_groups: int
    conflicting_groups: int
    questions: dict[str, QuestionCoverage]
    critical: list[CriticalCount]
    mix: CoverageMix
    warnings: list[str]


class Share(BaseModel):
    model_config = ConfigDict(extra="forbid")

    k: int
    n: int
    low: float | None
    high: float | None


class Interval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "not_estimable"]
    low: float | None = None
    high: float | None = None
    reason: str | None = None
    dropped: int


class Coefficient(BaseModel):
    model_config = ConfigDict(extra="forbid")

    estimate: float | None
    status: Literal["ok", "not_estimable"]
    reason: str | None = None
    interval: Interval | None = None


# Without the pair's label counts, which the store leaves out for curators: where a pair shares
# few cases, they are what each rater answered (`EvalsetStore.summary`).
class PairKappa(Coefficient):
    raters: list[UserId]
    n: int


class PoolAlpha(Coefficient):
    cases: int
    ratings: int


class QuestionAgreement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cases: int
    percent: Share
    kappa: list[PairKappa]
    alpha: PoolAlpha


class AgreementSeeds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bootstrap: int
    resamples: int


class AgreementReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["mendari.agreement/1"]
    pack: str = Field(title="Agent")
    level: float
    seeds: AgreementSeeds
    questions: dict[str, QuestionAgreement]


class StoredAggregates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["mendari.evalset_aggregates/1"]
    set: str
    measured_at: str
    n: int = Field(description="the drawn cases measured")
    coverage: CoverageReport
    agreement: AgreementReport


class EvalsetDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    set: str
    name: str
    pack: str = Field(title="Agent")
    questions: dict[str, list[str]] = Field(description="each question labellers answer")
    state: SetState
    created_at: str
    cases: int
    statuses: CaseStatuses
    labellers: dict[UserId, LabellerCounts]
    frozen_at: str | None
    commitment: str | None = Field(description="the salted commitment chained at freeze")
    max_runs: int | None
    max_age_days: int | None
    runs: RunsCount
    graded_until: str | None
    retired_at: str | None
    aggregates: StoredAggregates | None = Field(
        description="the set's coverage and rater agreement as last stored (`mendari evalset "
        "agreement` or `coverage`, or at freeze), with when they were measured; null before"
    )


class SetSampled(BaseModel):
    model_config = ConfigDict(extra="forbid")

    set: str
    drawn: int = Field(description="cases drawn now")
    sampled: int = Field(description="cases drawn in all")
    seed: int


class SetAssigned(BaseModel):
    model_config = ConfigDict(extra="forbid")

    set: str
    round: Literal["first", "adjudication"]
    assigned: int = Field(description="assignments made now")
    withdrawn: int = Field(description="open assignments whose membership no longer holds")


# A grading run over a hidden evaluation set (LP-11l): what the organization's grading key sends,
# and what it is answered (`evals/hidden.py` `HiddenResult`, closed, aggregates only).
SlotName = Annotated[str, Field(min_length=1, max_length=64)]
# The scoreboard's arms (`evals/arms.py` `ARMS`), as a grading run declares them (LP-17k).
ArmName = Literal["deterministic", "classifier", "llm", "classifier_and_llm"]


class GradingRequest(BaseModel):
    pack: str = Field(
        min_length=1,
        max_length=200,
        title="Agent",
        description="the id of the agent the set was made for, as this server serves it",
        examples=[DEMO_AGENT],
    )
    variants: dict[SlotName, SlotName] | None = Field(
        default=None,
        max_length=64,
        description="a variant for any of the agent's provider slots (the others run the "
        "deployment's choice, or the agent's default); every slot must run a local variant",
        examples=[{"reader": "rules"}],
    )
    arms: dict[ArmName, Annotated[dict[SlotName, SlotName], Field(max_length=64)]] | None = Field(
        default=None,
        min_length=1,
        max_length=4,
        description="the hidden-set scoreboard (LP-17k): each declared arm with its whole "
        "configuration, a variant for every one of the agent's slots (none is filled in from "
        "the deployment's choice); every one local, and each in the arm it is declared under. "
        "With `declared`, never with `variants`: the answer is `mendari.hidden_scoreboard/1`",
        examples=[{"deterministic": {"reader": "rules"}}],
    )
    declared: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="the SHA-256 of the committed declaration `arms` comes from (the grading "
        "method's section 3), which the answer cites; with `arms` only",
    )

    @model_validator(mode="after")
    def _arms_with_their_declaration(self) -> GradingRequest:
        if (self.arms is None) != (self.declared is None):
            raise ValueError("`arms` and `declared` are sent together, or neither is")
        if self.arms is not None and self.variants is not None:
            raise ValueError("`variants` or `arms`, not both: each arm names its own variants")
        return self


# Each refusal of a grading run (LP-11l), by its fixed reason (`GradingRefused.reason`): its
# status and the one sentence it is answered with, in place of any message (a store's names the
# set, and a run's could name a case), and logged as its reason only.
GRADING_REFUSALS: dict[str, tuple[int, str]] = {
    # Unknown, malformed and another organization's: one answer, as the curators' routes give.
    "no_set": (404, NO_SET),
    "wrong_pack": (422, "a hidden set is graded with the agent it was made for: name it as `pack`"),
    "pack_unavailable": (409, "the set's agent does not run on this server"),
    "unknown_variant": (
        422,
        "`variants`, or an arm in `arms`, names a slot or a variant the agent does not have",
    ),
    "remote_provider": (
        422,
        "a hidden set is graded with local providers only: name a local variant, in `variants` "
        "or in each arm of `arms`, for each slot that runs a remote one here",
    ),
    "wrong_arm": (
        422,
        "each arm in `arms` names a variant for every slot of the agent, a configuration of "
        "that arm",
    ),
    "not_gradable": (409, "only a frozen or active set is graded"),
    "no_runs_left": (409, "the set has had all the grading runs its rotation rule allows"),
    "expired": (409, "the set is older than its rotation rule allows for grading"),
    "set_changed": (409, "the set does not hold what it was frozen with: nothing was read"),
    "set_unavailable": (503, "the set cannot be graded on this server now: nothing was read"),
    "run_failed": (500, "the grading run failed; it counts as one of the set's runs"),
}


def grading_refused(reason: str) -> GradingRefused:
    """A grading run's refusal as answered: its fixed reason and sentence (`GRADING_REFUSALS`)."""
    return GradingRefused(reason, GRADING_REFUSALS[reason][1])


# What every JSON decide answer carries (mendari.notices), and the header a refusal for want of
# an acknowledgement carries instead of a sign-in challenge.
NOTICE = {"notice": notices.notice()}
NOTICE_HEADER = "Mendari-Notice-Required"


def principal_of(request: Request) -> Principal:
    """Who the guard let through (`open mode` when sign-in is off)."""
    principal: Principal = getattr(request.state, "principal", None) or OPEN
    return principal


def input_reader(request: Request) -> str | None:
    """Who may see inputs kept encrypted (PHI mode `encrypt`): a signed-in person or key whose
    role may read them. Without sign-in there is no one to log the read against, so no one."""
    principal = getattr(request.state, "principal", None)
    if principal is None or principal.kind == "open" or not principal.may("read_input"):
        return None
    return str(principal.actor)


def _again(error: MendariError) -> MendariError:
    """A new exception of `error`'s type, with its message and attributes. Raising the one
    stored error on every request would add each request's frames (its input among their
    locals) to the one traceback kept with it, for as long as the server runs."""
    fresh = type(error).__new__(type(error), *error.args)
    fresh.__dict__.update(vars(error))
    return fresh


def _with_figures(report: dict[str, Any]) -> dict[str, Any]:
    """A report as served: with its figures (`mendari.evals.figures`), worked out on every read
    and never stored; null figures for a stored report they cannot be read from."""
    return {**report, "figures": figures.guarded(lambda: figures.report(report))}


def create_app(
    *,
    db_path: str | Path = "var/mendari.db",
    migrate_url: str | None = None,
    providers: Mapping[str, str] | None = None,
    default_providers: Mapping[str, Mapping[str, str]] | None = None,
    datasets_dir: Path | None = None,
    packs_paths: Sequence[Path | SearchPath] | None = None,
    budget_limits: Mapping[str, int | None] | None = None,
    fhir_validation: FhirValidation | None = None,
) -> FastAPI:
    """`providers` are the deployment's provider settings (`settings.parse_providers`);
    `default_providers` (pack id -> slot -> variant, `settings.default_providers`) fill each
    slot those settings choose nothing for, before the pack's own default."""
    timezone = clock.zone().key  # an invalid MENDARI_TIMEZONE fails here, at startup
    validation = fhir_validation or settings.fhir_validation()  # likewise
    fhir_servers = sorted(allowed_servers())  # likewise
    # The origins remote providers may be called at (None: no list): a malformed list, or an
    # LLM or Jev endpoint the deployment set that it leaves out, fails here, at startup.
    provider_origins = provider_allow_list()
    smart_options = smart_settings()
    # The EHRs whose launches are principals: only a malformed value, or a base off the FHIR
    # allow-list, fails here; every other SMART check refuses a launch, never the server.
    ehrs = smart_ehrs()
    sign_in = auth_settings()  # an invalid sign-in setting fails here, at startup
    backend = backend_settings(sign_in)  # likewise: on, it needs sign-in and a public URL
    trusted = trusted_origins()  # this site's other origins: a malformed one fails here
    # The CDS clients whose own signed tokens are principals (LP-23b): unset, none; anything
    # wrong in the list (or a missing public URL) fails here, at startup, naming the entry.
    cds = cds_clients(signed_in=sign_in is not None, ehrs=ehrs, trusted=trusted)
    # Open mode answers CDS Hooks on this machine only, or from anywhere under the sandbox flag
    # (LP-23c), which fails here on Vercel or with sign-in on.
    cds_open = CdsOpenMode(sandbox=cds_sandbox(signed_in=sign_in is not None))
    phi = phi_policy()  # a bad PHI setting (or a missing key) fails here, at startup
    launch = smart_options is not None  # a launch fetches the patient's record
    # and plaintext where it must not be: never with sign-in, never on the hosted site
    require_protection(phi, sign_in=sign_in is not None, launch=launch)
    # Hidden evaluation sets (LP-11): off unless both keys are set; malformed, half-set or
    # reused keys stop the server here, as does a set without sign-in or off Production.
    evalsets = evalset_keys()
    if evalsets is not None:
        require_evalset_deployment(sign_in=sign_in is not None)
    retention_days = settings.retention_days()  # likewise
    status_org = settings.status_org()  # likewise
    # The pack registry under this engine (LP-19d): off unless MENDARI_REGISTRY=on.
    registry_on = settings.registry_mounted()  # likewise
    # Organizations' pack stores (LP-20f, ADR-0016): off unless MENDARI_AGENT_STORE=on.
    store_on = settings.pack_store_on()  # likewise
    # Its reviewers' organization (LP-19e): unset, nobody reviews through the web. Checked once
    # the store is open, below.
    review_org = settings.registry_review_org()  # likewise
    # The registry the console finds agents in and adds them from (`server/hub_routes.py`):
    # unset, none; an address that is not https (or this machine's) fails here.
    hub_registry = settings.registry_url()  # likewise
    # Whether organizations set their agents' connections in the console (ADR-0021): on unless
    # MENDARI_AGENT_CONNECTIONS=off.
    connections_on = connections_enabled()  # likewise
    # The organization whose administrators operate the deployment (`MENDARI_OPERATOR_ORG`), and
    # a fresh install's first administrator (`MENDARI_BOOTSTRAP_ADMIN`): a malformed one fails
    # here, and so does a first administrator where no membership could be given.
    operator_org = settings.operator_org()  # likewise
    first_admin = settings.bootstrap_admin()  # likewise
    first_admin_org_name = settings.bootstrap_org_name()  # likewise
    if first_admin is not None and (sign_in is None or sign_in.accounts != "memberships"):
        raise MendariError(
            "MENDARI_BOOTSTRAP_ADMIN names a first administrator, which needs memberships mode "
            "(MENDARI_ACCOUNTS=memberships, with sign-in on): in claims mode your identity "
            "provider's roles say who administers. Turn memberships mode on, or take the "
            "setting out."
        )
    registry_app: FastAPI | None = None  # mounted with the routes, below
    registry_headers: Mapping[str, str] | None = None  # the mounted registry's own
    cron_secret = os.environ.get("CRON_SECRET", "")  # the scheduled jobs' credential
    if cron_secret and (len(cron_secret) < MIN_CRON_SECRET or cron_secret != cron_secret.strip()):
        # An optional job never stops the server: the jobs stay off, and health says so.
        phi_logger.warning(
            "CRON_SECRET is not used, so the scheduled purge, monitor and checkpoint are off: it "
            "needs at least %d random characters, with no spaces around them (Vercel Cron sends "
            "it as it is stored)",
            MIN_CRON_SECRET,
        )
        cron_secret = ""
    body_limit = settings.max_body_bytes()  # likewise
    deadline_s = settings.decision_deadline_s()  # likewise
    rate_limits = limit_settings(sign_in=sign_in is not None)  # likewise
    org_limits = settings.org_daily_limits()  # likewise
    principal_limit = settings.principal_daily_limit()  # likewise
    session_lifetime = sessions.lifetime()  # likewise
    security_log.configure()
    if launch and phi.mode == "off":  # sign-in is off: with it on, `off` never starts
        phi_logger.warning(
            "the EHR launch is on and PHI mode is off: inputs are kept in plaintext, "
            "so use synthetic data only (docs/deploying-with-phi.md)"
        )
    alerts = alert_settings()  # likewise: its format and signing secret
    checkpoint_signer = checkpoint_key()  # likewise: a malformed MENDARI_CHECKPOINT_KEY stops it
    if org_limits and alerts is not None:
        check_webhook(alerts.webhook)  # likewise: a quota's alert must be able to leave
    memberships = sign_in is not None and sign_in.accounts == "memberships"
    store = AuditStore(db_path, migrate_url=migrate_url, phi=phi)
    store.ready_to_keep()  # never `off` on a protected database, nor a retired PHI key
    if store_on:
        # The store needs memberships mode, and what it rests on, checked by running it: a
        # missing one stops the server here, before any route is mounted (LP-20f).
        from mendari.core.pack_store import prerequisites

        mode = "open" if sign_in is None else sign_in.accounts
        unmet = prerequisites(mode)
        if unmet:
            raise MendariError(
                "MENDARI_AGENT_STORE is on, but the agent store cannot open: " + "; ".join(unmet)
            )
    if evalsets is not None:  # nor a stray, retired or PHI key as an evaluation-set key (LP-11e)
        store.evalset_ready(evalsets)
    if review_org is not None:
        # An optional feature never stops the server (as CRON_SECRET above): reviewing stays
        # off, and the log says why.
        why_not = (
            "MENDARI_REGISTRY is not on"
            if not registry_on
            else "reviewers are memberships (MENDARI_ACCOUNTS=memberships with sign-in on)"
            if not memberships
            else f"no organization {review_org!r} has a record (mendari access org create)"
            if store.organization(review_org) is None
            else None
        )
        if why_not is not None:
            phi_logger.warning(
                "MENDARI_REGISTRY_REVIEW_ORG is not used, so nobody reviews the agent registry "
                "through the web (the operator's `mendari registry` commands still do): %s",
                why_not,
            )
            review_org = None
        else:
            # Said at every start: a membership that held the role before this organization
            # was named (the operator's command gives any role anywhere) reviews from now.
            holding = sum(
                accounts.REGISTRY_REVIEWER in member["roles"]
                for member in store.memberships(org=review_org)
            )
            phi_logger.info(
                "the agent registry's reviewers are the %d member(s) of %r holding %s",
                holding,
                review_org,
                accounts.REGISTRY_REVIEWER,
            )
    acknowledgement = notices.mode()  # read once; a bad value fails at startup
    acknowledged: set[str] = set()  # chain ids found acknowledged: an acknowledgement stays
    # Each organization's connections for its agents (ADR-0021, `core/connections.py`), read
    # for each decision and kept a few seconds on this instance, as holds are: one set or
    # cleared here takes effect at once, one set on another instance within HOLD_SECONDS.
    connection_store = ConnectionStore(store)
    connection_cache: dict[tuple[str, str], tuple[float, Connection | None]] = {}
    connection_generation = [0]

    def forget_connections() -> None:
        connection_generation[0] += 1
        connection_cache.clear()

    def connection_for(pack_id: str) -> Connection | None:
        """The connection the organization this call acts for set for an agent, or None: none
        set, or connections are switched off here (then the deployment's own endpoint is all
        there is). One whose token does not open raises, each time it is asked: the agent's
        provider fails the call, and never falls through to another service."""
        if not connections_on:
            return None
        org = tenancy.current() or tenancy.DEFAULT_ORG
        key = (org, pack_id)
        now = time.monotonic()
        cached = connection_cache.get(key)
        if cached is not None and now - cached[0] < HOLD_SECONDS:
            return cached[1]
        generation = connection_generation[0]
        found = connection_store.resolve(org, pack_id)
        if generation == connection_generation[0]:  # none set or cleared here meanwhile
            if len(connection_cache) >= CONNECTION_CACHE_MAX:
                connection_cache.clear()
            connection_cache[key] = (now, found)
        return found

    auth = Auth(
        sign_in,
        store,
        tokens=backend is not None,
        lifetime=session_lifetime,
        ehrs=ehrs if smart_options is not None else None,
        cds=cds,
        cds_open=cds_open,
        registry=registry_on,
        review_org=review_org,
        pack_store=store_on,
        operator_org=operator_org,
        first_admin=first_admin,
        # Its record's name when the setting makes it: as given, else the public address's host.
        first_admin_org_name=first_admin_org_name
        or (urlsplit(sign_in.public_url).hostname if sign_in and sign_in.public_url else None),
    )
    if first_admin is not None:
        if store.first_admin() is not None:
            phi_logger.info(
                "MENDARI_BOOTSTRAP_ADMIN is ignored: this database's first administrator was made "
                "at their first sign-in, once. The setting can be taken out."
            )
        elif operator_org is None:
            phi_logger.warning(
                "MENDARI_BOOTSTRAP_ADMIN is set without MENDARI_OPERATOR_ORG: the first "
                "administrator will administer `default`, and nobody will see the deployment's "
                "checks in the console"
            )
    cds_origins = frozenset() if cds is None else cds.origins  # what CORS serves (LP-23c)
    origins = OriginCheck(
        open_mode=not auth.enabled, trusted=trusted, cds_origins=cds_origins, registry=registry_on
    )
    limiter = Limiter(store, rate_limits)
    # The daily cap per principal (MENDARI_PRINCIPAL_DAILY_LIMIT): sign-in only.
    cap = DailyCap(store, principal_limit) if principal_limit is not None and auth.enabled else None
    if principal_limit is not None and cap is None:
        quota_logger.warning("MENDARI_PRINCIPAL_DAILY_LIMIT is ignored in open mode: no principals")
    for provider_type, limit in (budget_limits or {}).items():
        # Evaluations share the deployment's budget with live decisions (LP-21h): one
        # organization's evaluation quota as large spends every organization's day.
        quota = org_limits.get(f"eval_{provider_type}")
        if limit is not None and quota is not None and quota >= limit:
            quota_logger.warning(
                "eval_%s=%d in MENDARI_ORG_DAILY_LIMITS is not below the deployment's daily %s "
                "budget (%d): one organization's evaluations can use it all, and live decisions "
                "then go to people until midnight",
                provider_type,
                quota,
                provider_type,
                limit,
            )
    budget = (
        CallBudget(
            store,
            budget_limits or {},
            org_limits=org_limits,
            on_quota_reached=quota_alert(alerts, store),
        )
        if budget_limits or org_limits
        else None
    )
    found = discover(packs_paths)
    only = settings.enabled_packs()  # a per-pack image serves just its pack
    missing = only - {pack.id for pack in found.packs}
    if missing:
        raise MendariError(f"MENDARI_AGENTS: agents not installed here: {sorted(missing)}")
    # The newest version of every pack found, served or not. `discover` refuses any second
    # claim to an id a community pack takes part in, so a community pack never competes here.
    installed: dict[str, Pack] = {}
    for pack in found.packs:
        current = installed.get(pack.id)
        if current is None or version_key(pack.version) > version_key(current.version):
            installed[pack.id] = pack
    packs = {pack_id: pack for pack_id, pack in installed.items() if not only or pack_id in only}
    # The site code maps, per organization (LP-23i): unset, no code is translated. Anything wrong
    # in them, or a translation into a code no pack found here declares (served or not, so one
    # setting serves every per-pack image), fails here, at startup, naming the organization.
    site_maps = site_codes(installed.values(), signed_in=sign_in is not None)
    # What each registered EHR does not offer, declared per FHIR base (MENDARI_EHR_DECLARATIONS):
    # unset, every source is read and a refused one abstains. A base off the allow-list, not
    # registered for its organization, or a type no pack found here may read its list without,
    # fails here, at startup, naming the base.
    declarations = ehr_declarations(
        installed.values(), signed_in=sign_in is not None, ehrs=ehrs, cds=cds
    )
    # Every CodeSystem and NamingSystem the served packs need. A pack whose terminology cannot
    # be built is reported in /api/health (`invalid`) and served without it.
    terms, terminology_problems = Terminology.serving(packs.values())
    # Every served pack is built once, at start-up (below), never in a request (LP-16d): `built`
    # holds its Decision, or `unbuildable` why it could not be built, which each use raises again
    # and nothing builds again. Its deployment is recorded on its first use (`recorded`).
    built: dict[str, Decision] = {}
    unbuildable: dict[str, MendariError] = {}
    recorded: set[str] = set()
    overrides = dict(providers or {})

    # An organization's promoted pack store versions (LP-20h): read through `promoted_source`,
    # which the store sets when it is on, per organization and kept `HOLD_SECONDS` (a promotion
    # or retirement here clears it at once). Never another organization's.
    promoted_source: list[Callable[[str], dict[str, tuple[Pack, str, bool]]]] = []
    promoted_cache: dict[str, tuple[float, int, dict[str, tuple[Pack, str, bool]]]] = {}
    promoted_generation = [0]

    def forget_promoted() -> None:
        promoted_generation[0] += 1
        promoted_cache.clear()

    def promoted_here() -> dict[str, tuple[Pack, str, bool]]:
        """The caller's organization's promoted versions, pack id -> (pack, promotion id,
        whether it may automate); none
        without the store, without an organization, or for an id this deployment serves from
        disk (which a store version never replaces)."""
        if not promoted_source:
            return {}
        try:
            org = tenancy.current()
        except NoOrganization:
            return {}
        if org is None:
            return {}
        now = time.monotonic()
        cached = promoted_cache.get(org)
        if cached is not None and now - cached[0] < HOLD_SECONDS:
            return cached[2]
        generation = promoted_generation[0]
        found = {
            pack_id: served
            for pack_id, served in promoted_source[0](org).items()
            if pack_id not in packs
        }
        if generation == promoted_generation[0]:
            promoted_cache[org] = (now, generation, found)
        return found

    def served_here() -> dict[str, Pack]:
        """The packs this request may use: those read from disk at start-up, for everyone, and
        its organization's promoted store versions (LP-20h)."""
        promoted = promoted_here()
        if not promoted:
            return packs
        return {**{pack_id: served[0] for pack_id, served in promoted.items()}, **packs}

    def pack_or_404(pack_id: str) -> Pack:
        pack = packs.get(pack_id)
        if pack is None:
            served = promoted_here().get(pack_id)
            pack = None if served is None else served[0]
        if pack is None:
            raise PackNotFoundError(f"no agent {pack_id!r}")
        return pack

    skipped_settings: dict[str, list[str]] = {}
    fallbacks = {pack_id: dict(slots) for pack_id, slots in (default_providers or {}).items()}
    # Settings that apply to no served pack (an unknown slot, a pack not served): reported in
    # /api/health and the startup log, never a failed start (instances share one setting).
    unused_settings = sorted(
        f"{key}={variant}"
        for key, variant in overrides.items()
        if not any(
            applies_to(key, pack.id, pack.manifest.providers, community=pack.named_settings_only)
            for pack in packs.values()
        )
    )
    if unused_settings:
        providers_logger.warning(
            "provider settings that apply to no served agent: %s", ", ".join(unused_settings)
        )
    # ...and those no served pack takes at all (above, or a variant no pack with the slot has):
    # what `mendari serve --provider` refuses when typed.
    taken: set[str] = set()
    for pack in packs.values():
        pack_slots = {slot: set(spec.variants) for slot, spec in pack.manifest.providers.items()}
        chosen = providers_for(pack.id, pack_slots, overrides, community=pack.named_settings_only)[
            0
        ]
        for key, variant in overrides.items():
            slot = key.rpartition(":")[2]
            if chosen.get(slot) == variant and applies_to(
                key, pack.id, pack_slots, community=pack.named_settings_only
            ):
                taken.add(key)
    not_taken = sorted(f"{key}={variant}" for key, variant in overrides.items() if key not in taken)

    def variants_for(pack: Pack) -> tuple[dict[str, str], list[str]]:
        """The variants the deployment chose for this pack's slots (the rest run the pack's
        default), and the settings that name the pack but cannot apply. A community pack takes
        only settings that name it, never the defaults (it spends only where the owner said), and
        so does a pack from a store (`Pack.named_settings_only`)."""
        slots = {slot: set(spec.variants) for slot, spec in pack.manifest.providers.items()}
        only_named = pack.named_settings_only
        chosen, skipped = providers_for(pack.id, slots, overrides, community=only_named)
        if only_named:
            return chosen, skipped
        for slot, variant in fallbacks.get(pack.id, {}).items():
            if slot not in chosen and variant in slots.get(slot, ()):
                chosen[slot] = variant
        return chosen, skipped

    # Say once, at startup, which slots the Jev defaults turn on: ids and variant names only.
    filled = []
    for pack in packs.values():
        chosen = variants_for(pack)[0]
        named = providers_for(
            pack.id,
            {s: set(v.variants) for s, v in pack.manifest.providers.items()},
            overrides,
            community=pack.named_settings_only,
        )[0]
        filled += [f"{pack.id} {slot}={chosen[slot]}" for slot in sorted(set(chosen) - set(named))]
    if filled:
        level = logging.INFO if phi.mode == "off" else logging.WARNING
        providers_logger.log(level, "Jev is set up; its defaults run %s", ", ".join(sorted(filled)))

    def build(pack: Pack) -> Decision:
        """The pack bound to the providers the deployment chose for it (at start-up only)."""
        chosen, skipped = variants_for(pack)
        if skipped:
            skipped_settings[pack.id] = skipped
        try:
            return Decision(
                pack,
                providers=chosen,
                store=store,
                budget=budget,
                fhir_validation=validation,
                governance_source=approved_override,
                hold_source=held,
                origin_approval=registry_approved,
                recall_source=recall_watch.held,
                deadline_s=deadline_s,
                site_codes=site_maps,
                canned_cap=sign_in is not None,  # canned answers never automate with sign-in
                connection_source=connection_for,
            )
        except MendariError:
            raise
        except Exception as error:  # the pack's content: named by type, never a 500
            problem = f"its providers could not be built ({type(error).__name__})"
            raise PackValidationError(pack.id, [problem]) from None

    # A promoted store version's Decision (LP-20h): built on its first use in this process,
    # under a lock of its own (a request that arrives while it is built waits for that build and
    # never starts a second), kept by digest and promotion; health never waits on it.
    store_built: dict[tuple[str, str], Decision] = {}
    store_building: dict[tuple[str, str], threading.Lock] = {}
    store_lock = threading.Lock()

    def store_decision(pack: Pack, promotion: str, automate: bool) -> Decision:
        """A promoted version's Decision. One promoted to recommend only (`automate` false)
        never automates, as under a hold (policy only lowers automation). A version that was
        pulled from a registry (origin `hub`) is held to what every agent from a registry is:
        it never automates until its organization approves its digest, nor while its registry
        recalls it (`Decision._origin_holds`)."""
        key = (pack.digest, promotion)

        def held_or_recommending(served: Pack) -> str | None:
            recommending = None if automate else f"promoted to recommend only ({promotion})"
            return "; ".join(filter(None, [held(served), recommending])) or None

        found = store_built.get(key)
        if found is not None:
            return found
        with store_lock:
            building = store_building.setdefault(key, threading.Lock())
        try:
            with building:
                found = store_built.get(key)
                if found is None:
                    chosen, _ = variants_for(pack)
                    try:
                        found = Decision(
                            pack,
                            providers=chosen,
                            store=store,
                            budget=budget,
                            fhir_validation=validation,
                            governance_source=approved_override,
                            hold_source=held_or_recommending,
                            origin_approval=registry_approved,
                            recall_source=recall_watch.held,
                            deadline_s=deadline_s,
                            site_codes=site_maps,
                            promotion=promotion,
                            canned_cap=sign_in is not None,
                            connection_source=connection_for,
                        )
                    except MendariError:
                        raise
                    except Exception as error:  # the pack's content: named by type, never a 500
                        problem = f"its providers could not be built ({type(error).__name__})"
                        raise PackValidationError(pack.id, [problem]) from None
                    with store_lock:
                        if len(store_built) >= STORE_BUILT_MAX:
                            store_built.pop(next(iter(store_built)))
                        store_built[key] = found
        finally:
            with store_lock:
                store_building.pop(key, None)
        return found

    def decision_for(pack_id: str) -> Decision:
        """A served pack's Decision, built at start-up. Its first use records the deployment, once
        per process (concurrent first uses: `record_deployment` keeps one row), with no lock
        /api/health could wait on. A pack that could not be built raises why on every use. An
        organization's promoted store version is built on its first use (LP-20h)."""
        if pack_id not in packs:
            served = promoted_here().get(pack_id)
            if served is not None:
                return store_decision(*served)
        pack = pack_or_404(pack_id)
        if pack.id in unbuildable:
            raise _again(unbuildable[pack.id])
        decision = built[pack.id]
        if pack.id not in recorded:
            environment, release = settings.deployment_place()
            store.record_deployment(
                environment=environment,
                release=release,
                pack_id=pack.id,
                pack_version=pack.version,
                pack_digest=pack.digest,
                providers={slot: info.variant for slot, info in decision.provider_info.items()},
            )
            recorded.add(pack.id)
        return decision

    def question_labels(pack_id: str, question: str) -> list[str] | None:
        """An installed pack's labels for one of its questions (None: not installed, or no such
        question), for decisions whose record has no answer to check a label against. Installed,
        not only served: a per-pack instance can share a database with other packs' decisions."""
        pack = installed.get(pack_id)
        if pack is None or question not in pack.manifest.questions:
            return None
        return pack.manifest.labels(question)

    # Automation holds (`AuditStore.hold_automation`), read per decision and kept a few seconds:
    # a hold set on another instance takes effect within HOLD_SECONDS, one set here at once.
    hold_cache: dict[tuple[str, str, str], tuple[float, str | None]] = {}
    # The recalls of the registries the served packs came from, read in the background (LP-18e).
    recall_watch = RecallWatch(packs.values())
    hold_generation = [0]  # bumped by every hold set or lifted here: older look-ups are not kept

    def forget_holds() -> None:
        hold_generation[0] += 1
        hold_cache.clear()

    def held(pack: Pack) -> str | None:
        """Why automation is held for this pack in the caller's organization now, or None."""
        org = tenancy.current() or tenancy.DEFAULT_ORG
        key = (org, pack.id, pack.digest)
        now = time.monotonic()
        cached = hold_cache.get(key)
        if cached is not None and now - cached[0] < HOLD_SECONDS:
            return cached[1]
        generation = hold_generation[0]
        hold = store.hold_for(pack.id, pack.digest, org)
        reason = None if hold is None else f"hold {hold['id']}"
        if generation == hold_generation[0]:  # no hold set or lifted here while it was read
            hold_cache[key] = (now, reason)
        return reason

    def registry_approved(pack: Pack) -> bool:
        """Whether the caller's organization approved exactly this pulled pack (LP-18d)."""
        return store.registry_approval(pack.id, pack.digest) is not None

    def approved_override(pack: Pack) -> GovernanceOverride | None:
        """The organization's approval in force for exactly this pack, as the runtime override."""
        approval = store.active_approval(pack.id, pack.digest)
        if approval is None:
            return None
        return GovernanceOverride(approved_by=approval["approver"], reference=approval["reference"])

    # Start-up builds every served pack, so /api/health reads what it found and builds nothing.
    # A pack whose providers cannot be built is listed there with the packs that did not load;
    # it never stops the start, nor turns health into an error for every other pack.
    for pack_id in sorted(packs):
        try:
            built[pack_id] = build(packs[pack_id])
        except MendariError as error:
            unbuildable[pack_id] = error
    health_providers = {
        pack_id: {slot: info.variant for slot, info in decision.provider_info.items()}
        for pack_id, decision in built.items()
    }
    # The model each slot runs (an LLM's or Jev's from its setting): monitoring compares a
    # benchmark only with the models it measured.
    running_models = {
        pack_id: {slot: info.model for slot, info in decision.provider_info.items()}
        for pack_id, decision in built.items()
    }
    health_without_model = {
        pack_id: slots
        for pack_id, running in health_providers.items()
        if (slots := slots_without_model(packs[pack_id], running))
    }
    health_unbuilt = {pack_id: str(error) for pack_id, error in unbuildable.items()}
    # Every pack that did not load, get its terminology or build: signed-in health lists them,
    # and readiness says `degraded` while there is one (`none` when no pack is built).
    health_invalid = {**found.problems, **terminology_problems, **health_unbuilt}
    readiness = Readiness(store.ping, packs_state(built=len(built), problems=len(health_invalid)))
    # An owned served pack whose provider configs, prefetch or wording templates `mendari validate`
    # refuses (`Pack.warnings`): it loads and serves what builds, and says so once here and in
    # /api/health.
    health_pack_warnings = {
        pack_id: list(packs[pack_id].warnings)
        for pack_id in sorted(packs)
        if packs[pack_id].warnings
    }
    for pack_id, warnings in health_pack_warnings.items():
        providers_logger.warning(
            "%s loads with problems (provider configs, prefetch or wording templates) that "
            "mendari validate refuses: %s",
            packs[pack_id].ref,
            "; ".join(warnings),
        )

    def local_datasets(pack: Pack) -> dict[str, Path]:
        if datasets_dir is None or not datasets_dir.is_dir():
            return {}
        return {
            path.stem: path
            for path in sorted(datasets_dir.glob("*.jsonl"))
            if _labels_questions_of(path, pack)
        }

    def dataset_for(pack: Pack, name: str) -> Dataset:
        if name in pack.manifest.evaluation.datasets:
            return resolve_dataset(pack, name)
        path = local_datasets(pack).get(name)
        if not _DATASET_NAME.match(name) or path is None:
            raise PackNotFoundError(f"unknown dataset {name!r}")
        return load_dataset(path)

    def logged_reads(request: Request, items: list[dict[str, Any]], id_key: str = "id") -> None:
        """Under PHI mode `hash` or `encrypt`, a security log line for the decisions whose kept
        input or notes a reader was shown (their ids and how many), as the chain has an
        `input.read` for each (`AuditStore.reveal_inputs`)."""
        if phi.mode == "off" or input_reader(request) is None:
            return
        # One per decision, as the chain has one `input.read` (a feedback list has an item per
        # rating).
        shown = list(dict.fromkeys(str(item[id_key]) for item in items if shown_to_reader(item)))
        if shown:
            log_event(request, "phi.inputs.read", ids=shown, count=len(shown))

    default_pack = DEMO_AGENT if DEMO_AGENT in packs else next(iter(sorted(packs)), None)
    app = FastAPI(
        title="Mendari",
        version=__version__,
        summary="Run, review, audit and evaluate healthcare decisions.",
        description=api_docs.description(
            sign_in=auth.enabled,
            default_pack=default_pack,
            token_url=None if backend is None else backend.token_url,
            rate_limits=(
                {name: str(rate) for name, rate in rate_limits.rates.items()}
                if limiter.mode == "on"
                else None
            ),
        ),
        openapi_tags=api_docs.TAGS,
        # FastAPI's own pages load Swagger UI and ReDoc from a CDN, unpinned, into this origin.
        # Mendari serves its reference from vendored files instead (/docs/reference).
        docs_url=None,
        redoc_url=None,
        swagger_ui_oauth2_redirect_url=None,
    )
    app.state.provider_settings_not_taken = not_taken  # `mendari serve --provider`
    app.state.recall_watch = recall_watch  # read by tests and the readiness of recalls

    def limit_class(method: str, path: str) -> str | None:
        return limiter.classify(method, path, auth.permission(method, path))

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            generated = get_openapi(
                title=app.title,
                version=app.version,
                summary=app.summary,
                description=app.description,
                routes=app.routes,
                tags=app.openapi_tags,
            )
            app.openapi_schema = api_docs.document(
                generated,
                packs=packs,
                default_pack=default_pack,
                permission_for=auth.permission if auth.enabled else None,
                token_url=None if backend is None else backend.token_url,
                limit_for=limit_class if limiter.mode == "on" else None,
                session_cookie=session_cookie_name(),
                launch_cookie=session_cookie_name(sessions.SMART),
            )
            if registry_app is not None:  # mounted below; its routes are described here
                api_docs.mounted(
                    app.openapi_schema,
                    registry_app.openapi(),
                    prefix=REGISTRY_PREFIX.rstrip("/"),
                )
            app.openapi_schema = api_docs.titled(app.openapi_schema)
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]

    @app.exception_handler(MendariError)
    async def _error(request: Request, error: MendariError) -> Response:
        status = next((code for kind, code in _STATUS.items() if isinstance(error, kind)), 400)
        if isinstance(error, SmartError | AuthError):
            status = error.status
        if isinstance(error, ProviderError):
            status = _PROVIDER_STATUS.get(error.code, _PROVIDER_UPSTREAM)
        if isinstance(error, GradingRefused):
            status = GRADING_REFUSALS.get(error.reason, (status, ""))[0]
        if request.url.path.startswith(("/smart/", "/auth/")):
            page = error_page(str(error), status)
            page.headers.update(retry_headers(error))
            return page
        return error_response(request.url.path, status, error)

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(request: Request, error: RequestValidationError) -> Response:
        """FastAPI's 422, without the `input` and `ctx` it would echo: a request body can carry
        case text, and errors end up in logs and browser consoles."""
        problems = [
            {"type": e.get("type"), "loc": list(e.get("loc", ())), "msg": e.get("msg")}
            for e in error.errors()
        ]
        if fhir_route(request.url.path):
            issues = [
                {
                    "code": "structure",
                    "expression": ".".join(map(str, p["loc"][1:])) or "body",
                    "diagnostics": str(p["msg"]),
                }
                for p in problems
            ]
            return operation_outcome(400, issues)
        return JSONResponse(identified({"detail": problems}), 422)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, error: StarletteHTTPException) -> Response:
        if fhir_route(request.url.path):
            code = _ISSUE_TYPE.get(error.status_code, "processing")
            issue = {"code": code, "diagnostics": str(error.detail)}
            return operation_outcome(error.status_code, [issue], error.headers)
        body = identified({"detail": error.detail})
        return JSONResponse(body, error.status_code, error.headers)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, error: Exception) -> Response:
        """A bug: a JSON 500 that says nothing about the input (the security log has its type
        and frames). It is answered outside the middleware, so it sets the security headers and
        the request id itself."""
        rid = getattr(request.state, "request_id", None)
        headers = {
            **SECURITY_HEADERS,
            "Content-Security-Policy": csp,
            "Cross-Origin-Embedder-Policy": "require-corp",
            "Cache-Control": "no-store",
            **(
                {"Strict-Transport-Security": HSTS}
                if not is_local_host(request.url.hostname)
                else {}
            ),
            **({"X-Request-Id": rid} if rid else {}),
        }
        if fhir_route(request.url.path):
            issue = {"code": "exception", "diagnostics": "internal error"}
            return operation_outcome(500, [issue], headers)
        body = {"error": "InternalError", "detail": "internal error"}
        return JSONResponse({**body, **({"request_id": rid} if rid else {})}, 500, headers=headers)

    # -- packs -------------------------------------------------------------------------------

    def deployment_total(name: str, total: Callable[[], Any]) -> dict[str, Any]:
        """A total over every organization, in health: the operator's in memberships mode."""
        return {} if auth.memberships else {name: total()}

    def purged(last: dict[str, Any] | None) -> dict[str, Any] | None:
        """The last purge as health shows it: in memberships mode when and by what, not how
        many inputs and notes, which are every organization's."""
        if last is None or not auth.memberships:
            return last
        return {key: value for key, value in last.items() if key not in ("inputs", "notes")}

    def runs_deployment(request: Request) -> bool:
        """Whether the caller sees the deployment's own views (the whole chain's verification,
        the deployment records), beyond what each route's permission asks. In open mode, and
        in claims mode without an operator organization, everyone the route lets in, as before.
        Otherwise only who operates the deployment: in memberships mode an administrator,
        signed in as a person, of the organization `MENDARI_OPERATOR_ORG` names (`operate`;
        nobody, without the setting), and in claims mode with the setting, that organization's
        callers."""
        principal = auth.acting(request)
        if principal.kind == "open":
            return True
        if auth.memberships:
            return principal.may("operate")
        return auth.operates(principal.org)

    def health_details(request: Request, principal: Principal, org: str) -> dict[str, Any]:
        """What `/api/health` adds for a principal that may read: the deployment's settings
        as start-up read them, and its organization's own figures. The operator's checks
        (`/api/system/checks`, `server/system.py`) are worked out from this same answer, so
        the two never disagree. `org` is the organization the caller acts for, which its
        callers checked it has."""
        return {
            # The pack registry is served here (MENDARI_REGISTRY=on, LP-19d): only then.
            **({"registry": True} if registry_on else {}),
            # The organization's pack store (LP-20f): how many of its versions wait for the
            # operator's review, for its administrators; only with the store on.
            **(
                {"store": {"held": app.state.pack_store.held_count(org)}}
                if store_on and principal.may("admin")
                else {}
            ),
            "packs": len(packs),
            # How many of them come from where: maintained, community, hub, local.
            "packs_by_origin": dict(Counter(pack.shown_origin for pack in packs.values())),
            "invalid": health_invalid,
            # Served packs that load with problems in their provider configs, prefetch or wording
            # templates (`mendari validate` refuses them; a pack the deployment does not own is
            # refused, under `invalid`).
            "pack_warnings": health_pack_warnings,
            "storage": store.backend,
            # Calendar days and displayed times use this zone; stored timestamps are UTC.
            "timezone": timezone,
            "fhir_validation": validation,
            # FHIR servers this deployment may read from (CDS Hooks fetch); empty: none.
            "fhir_allowed_servers": fhir_servers,
            # The origins remote providers (Jev, LLMs, Vercel Connect) may be called at
            # (MENDARI_PROVIDER_ALLOWED_ORIGINS), read at start-up; null: no list is set.
            "provider_allowed_origins": provider_origins,
            # Each registry a served pack came from: its last recall check's age, and how many
            # versions it recalls (LP-18e).
            "registry_recalls": recall_watch.status(),
            "smart_launch": smart_options is not None,
            # SMART Backend Services: its token endpoint when it is on (public in discovery
            # too), None when off. The dashboard's Clients tab is shown only when it is on.
            "backend_services": (
                {"token_endpoint": backend.token_url} if backend is not None else None
            ),
            # The EHRs registered to launch (MENDARI_SMART_EHRS), the caller's organization's
            # only (every one in open mode): base, organization and level.
            "smart_ehrs": [
                {"base": ehr.base, "org": ehr.org, "level": ehr.level}
                for ehr in sorted(ehrs.values(), key=lambda ehr: ehr.base)
                if principal.kind == "open" or ehr.org == org
            ],
            # The CDS clients whose own tokens are verified here (MENDARI_CDS_CLIENTS), by id,
            # the caller's organization's only (every one in open mode); None: none registered.
            "cds_clients": (
                None if cds is None else cds.ids(None if principal.kind == "open" else org)
            ),
            # The caller's organization's site code map (MENDARI_SITE_CODES, LP-23i): the version
            # and SHA-256 its FHIR decisions record; None: no code is translated for it.
            "site_codes": (
                None
                if site_maps is None or (found_map := site_maps.for_org(org)) is None
                else found_map.stamp()
            ),
            # What the caller's organization's EHRs do not offer (MENDARI_EHR_DECLARATIONS), by
            # FHIR base: each declaration's version, SHA-256, who attested it and where, and the
            # types; None: nothing is declared here.
            "ehr_declarations": (None if declarations is None else declarations.for_org(org)),
            "phi_mode": phi.mode,
            # Under `encrypt`, the ids of the PHI keys (never a key): the one that seals, and
            # those it only opens with (MENDARI_PHI_READ_KEYS, during a rotation).
            **({"phi_keys": phi.key_ids} if phi.mode == "encrypt" else {}),
            # With hidden evaluation sets on (LP-11), the ids of their keys (never a key): the
            # one that seals, those it only opens with, and the index key's.
            **({"evalset_keys": evalsets.key_ids} if evalsets is not None else {}),
            # Kept inputs and notes are deleted after this many days (MENDARI_RETENTION_DAYS),
            # by `mendari phi purge` or, with CRON_SECRET set, the scheduled purge.
            "retention_days": retention_days,
            "retention_scheduled": bool(cron_secret),
            # The id of the key that signs the daily checkpoint (MENDARI_CHECKPOINT_KEY, LP-21k;
            # never the key), or None when it is not set: the dashboard then asks for none.
            "checkpoint_key": (
                key_id(checkpoint_signer.public_key()) if checkpoint_signer is not None else None
            ),
            # When, how many, and by what (None: never); in memberships mode not how many, a
            # total of every organization's (the operator's, ADR-0008).
            "last_purge": purged(store.last_purge()),
            # How new records' input hashes are made: `hmac-sha256:<key id>` under
            # MENDARI_INPUT_HASH_KEY (required in PHI modes hash and encrypt), else `sha256`.
            "input_hash": phi.hash_kind,
            "providers": health_providers,
            # Slots running without a model although their pack offers one (the offline floor).
            "without_model": health_without_model,
            # Deployment provider settings that name a slot or variant a pack does not have.
            "provider_settings_skipped": skipped_settings,
            # ...and those that apply to no served pack at all (a typo, a pack not served).
            "provider_settings_unused": unused_settings,
            "jev": {
                # The bundled agents run Mendari + Jev where no setting chose (JEV_DEFAULTS).
                "defaults": bool(fallbacks),
                "credentials": key_source(key_env=jev.API_KEY_ENV, connector_env=jev.CONNECTOR_ENV),
                "endpoint_configured": bool(os.environ.get(jev.ENDPOINT_ENV, "").strip()),
                "model": os.environ.get(jev.MODEL_ENV, "").strip() or None,
                **deployment_total("calls_today", lambda: store.usage("jev")),
                "daily_limit": (budget_limits or {}).get("jev"),
            },
            "llm": {
                **llm.deployment_info(),
                **deployment_total("calls_today", lambda: store.usage("llm")),
                "daily_limit": (budget_limits or {}).get("llm"),
            },
            "rate_limits": limiter.describe(request),
            # Automation holds in force for the caller's organization (and the deployment's).
            "holds": len(store.holds(org)),
            # The notice people acknowledge before taking data in (mendari.notices).
            "notice": {"version": notices.VERSION, "acknowledgement": acknowledgement},
            # Browser sessions: ended after this long without a request, and at most after.
            "sessions": {
                "idle_minutes": session_lifetime.idle // 60,
                "max_hours": session_lifetime.absolute // 3600,
            },
            # The caller's own daily cap (MENDARI_PRINCIPAL_DAILY_LIMIT), with what it used today.
            **(
                {
                    "principal_quota": {
                        "daily_limit": cap.limit,
                        "used_today": store.principal_usage(principal.id),
                    }
                }
                if cap is not None and principal.kind != "open"
                else {}
            ),
            # The caller's organization only: another's usage is its own business.
            "org_quotas": {
                "daily_limits": org_limits,
                "used_today": store.org_usage(
                    org if principal.kind != "open" else tenancy.DEFAULT_ORG
                ),
            },
        }

    @app.get(
        "/api/health",
        tags=["Service"],
        summary="Deployment health and settings",
    )
    def health(request: Request) -> dict[str, Any]:
        """Public, for load balancers: `status`, `version` and `sign_in`, and nothing else
        without a credential (no database work either). A principal that may `read` (everyone
        in open mode) also gets the deployment's settings: agents (and how many come from
        where), storage, the FHIR validation mode, the FHIR and provider allow-lists, PHI mode,
        the input hash's kind and key id, which provider answers each agent's questions, today's
        remote calls (in memberships mode the operator's, `mendari usage`, as the last purge's
        counts are), the rate limits (where the caller's address is read, and the address seen
        on this request), the session policy, the organization's daily quotas with what it used
        today, whether SMART Backend Services is on (`backend_services`: its token endpoint,
        or null), the registered CDS clients of the caller's organization (`cds_clients`: their
        ids, or null when none is registered), the site code map of the caller's organization
        (`site_codes`: its version and SHA-256, or null), what its EHRs do not offer
        (`ehr_declarations`: by FHIR base, each declaration's version, SHA-256, who attested it,
        its reference and the types, or null when nothing is declared), and the id of the key
        that signs the daily checkpoint (`checkpoint_key`, or null).

        The agents' state (which provider each runs, the settings skipped, an agent that could not
        be built, the problems an owned agent loads with, `pack_warnings`) is
        computed at start-up: health builds no agent and records no deployment (a signed-in
        caller's session may still be refreshed, as on any route).
        """
        public = {"status": "ok", "version": __version__, "sign_in": auth.enabled}
        principal = auth.principal(request)  # no credential: no lookup
        if principal is None or principal.org is None or not principal.may("read"):
            return public  # a person of no organization (memberships mode) too
        if principal.grant is not None:
            return public  # a scoped key or token reaches only the routes its scope names
        return {**public, **health_details(request, principal, principal.org)}

    @app.get(
        "/api/health/ready",
        tags=["Service"],
        summary="Readiness: the database answers and the agents loaded",
        responses={503: {"description": "Not ready: the database did not answer, or no agent"}},
    )
    def ready() -> Response:
        """Public, for status pages, uptime checks and readiness probes (`server/readiness.py`),
        with `GET` or `HEAD`: `status` (`ready`, `degraded` or `unready`), `version` and
        `checks`: `database` (`ok` or `unreachable`) and `packs` (`ok`; `degraded` while an agent
        did not load or build, which signed-in `/api/health` lists under `invalid`; `none` when
        no agent is served). `503` when unready. Fixed codes only: no error text, no agent ids,
        no counts.

        An instance reads the database at most once every 5 s, whoever asks, and waits for the
        read about 3.5 s at most; a caller that comes while a check runs gets the last answer,
        so readiness holds one request thread at most, through any outage. Liveness stays
        `/api/health`, which with sign-in on does no database work for an anonymous caller.
        """
        answer = readiness.answer()
        return JSONResponse(answer.body, answer.status_code)

    # `HEAD`, as uptime checks often send it: the same answer without its body (one operation in
    # the reference, so the two methods never share an operation id).
    app.add_api_route("/api/health/ready", ready, methods=["HEAD"], include_in_schema=False)

    @app.get(
        "/api/providers/jev/models",
        tags=["Service"],
        summary="Jev models available",
    )
    def jev_models() -> dict[str, Any]:
        """The models this deployment's Jev account can use. `503` when Jev is not set up here."""
        return {"models": jev.list_models()}

    @app.get(
        "/api/agents",
        tags=["Agents"],
        summary="List agents",
        name="list_agents",
    )
    def list_packs() -> list[dict[str, Any]]:
        """Every agent this deployment serves: id, version, risk level, the labels of its
        main question, and where it comes from (`origin`: `maintained`, `community`, `hub` or
        `local`, with `origin_label` to show; a community agent is not clinically reviewed and
        never automates)."""
        return [_summary(pack) for pack in sorted(served_here().values(), key=lambda p: p.id)]

    @app.get(
        "/api/agents/{namespace}/{name}",
        tags=["Agents"],
        summary="Get an agent",
        name="agent_detail",
    )
    def pack_detail(namespace: str, name: str) -> dict[str, Any]:
        """An agent's manifest, input and output JSON Schemas, active providers, benchmark,
        leaderboard and Decision Card (Markdown)."""
        pack_id = f"{namespace}/{name}"
        pack = pack_or_404(pack_id)
        decision = decision_for(pack_id)
        summary = _summary(pack)
        return {
            **summary,
            "manifest": pack.manifest.model_dump(mode="json", by_alias=True),
            "active_providers": {
                slot: {"variant": i.variant, "type": i.type, "model": i.model}
                for slot, i in decision.provider_info.items()
            },
            "without_model": slots_without_model(
                pack, {slot: i.variant for slot, i in decision.provider_info.items()}
            ),
            "input_schema": pack.input_schema,
            "output_schema": pack.output_schema,
            # A pack from a registry: what it carries about itself (its benchmark and
            # leaderboard) is its publisher's, never shown, as the registry shows none (ADR-0012).
            "benchmark": None
            if pack.origin == "hub" or (published := benchmark(pack)) is None
            else _with_figures(published),
            "leaderboard": None if pack.origin == "hub" else leaderboard.load(pack),
            "card_markdown": card_of(pack),
            "datasets": [*pack.manifest.evaluation.datasets, *local_datasets(pack)],
            # What the agent may ask to have done (spec 0.4), each with how it is gated for
            # this organization now. Absent for an agent that declares none.
            **declared_actions(pack, vouched=decision.vouched()),
        }

    @app.get(
        "/api/agents/{namespace}/{name}/datasets",
        tags=["Agents"],
        summary="List an agent's datasets",
        name="agent_datasets",
    )
    def pack_datasets(namespace: str, name: str) -> list[dict[str, Any]]:
        """The datasets an evaluation can run on, with their size (e.g. to say what a run will
        cost in remote calls before it starts)."""
        pack = pack_or_404(f"{namespace}/{name}")
        names = [*pack.manifest.evaluation.datasets, *local_datasets(pack)]
        return [{"name": n, "n": len(dataset_for(pack, n).records)} for n in names]

    @app.get(
        "/api/agents/{namespace}/{name}/examples",
        tags=["Agents"],
        summary="Example inputs",
        name="agent_examples",
    )
    def pack_examples(namespace: str, name: str) -> list[dict[str, Any]]:
        """Synthetic example inputs: the agent's own behaviour tests. Tests that do not load, or
        hold a value JSON cannot (a date, binary, a lone surrogate), answer `422` naming why."""
        pack = pack_or_404(f"{namespace}/{name}")
        return [
            {
                "name": case.get("name", "unnamed case"),
                "input": case["input"],
                "expect": case.get("expect", {}),
            }
            for case in load_cases(pack)
            if isinstance(case, dict) and isinstance(case.get("input"), dict)
        ]

    @app.get("/api/notice", tags=["Agents"], summary="The notice, and whether you acknowledged it")
    def notice(request: Request) -> dict[str, Any]:
        """What Mendari is (`text`), shown on its pages and in every JSON decide answer, and what
        a signed-in person who may decide or review acknowledges before taking data in
        (`acknowledgement`). `required`: whether this caller must acknowledge `version` first;
        `acknowledged`: whether they have."""
        principal = principal_of(request)
        asked = (
            acknowledgement == "required"
            and principal.kind == "user"
            and any(principal.may(permission) for permission in notices.GATED)
        )
        return {
            **notices.notice(),
            "acknowledgement": notices.ACKNOWLEDGEMENT,
            "required": asked,
            "acknowledged": asked and store.notice_acknowledged(notices.chain_id(principal.id)),
        }

    @app.post(
        "/api/notice/acknowledge",
        tags=["Agents"],
        summary="Acknowledge the notice",
    )
    def acknowledge_notice(request: Request) -> dict[str, Any]:
        """Records, in the audit trail and once per notice version, that the signed-in person
        acknowledged `acknowledgement` (`GET /api/notice`). Without sign-in there is no one to
        record, and nothing is asked."""
        principal = principal_of(request)
        if principal.kind != "user":
            return {**notices.notice(), "acknowledged": False, "required": False}
        key = notices.chain_id(principal.id)
        if key not in acknowledged and store.acknowledge_notice(
            key, version=notices.VERSION, actor=principal.actor
        ):
            log_event(request, "notice.acknowledged", reason=f"version {notices.VERSION}")
        acknowledged.add(key)
        return {**notices.notice(), "acknowledged": True, "required": acknowledgement == "required"}

    def _is_shadow(request: Request, shadow: bool = False) -> bool:
        if shadow:
            return True
        header = (request.headers.get("x-mendari-shadow") or "").strip().lower()
        return header in {"1", "true", "yes", "on"}

    @app.post(
        "/api/agents/{namespace}/{name}/decide",
        tags=["Decisions"],
        summary="Make a decision",
    )
    def decide(
        request: Request,
        namespace: str,
        name: str,
        payload: JsonBody,
        shadow: bool = False,
    ) -> dict[str, Any]:
        """Run the agent on one input (its `input_schema`; see `GET …/examples` for synthetic ones).

        Every decision is recorded in the audit trail and ends as `AUTO`, `HUMAN_REVIEW`,
        `ABSTAIN` or `BLOCK`. **An input that fails the agent's schema is not an HTTP error:** it
        is a recorded decision with `outcome: BLOCK` and the reason in `reasons`, so check
        `outcome`, not only the status code.

        Pass `?shadow=true` or header `X-Mendari-Shadow: 1` to evaluate in shadow mode: automation
        is held for clinician comparison while recording what the agent would have automated.

        The answer is complete, with each answer's `evidence`; the audit record keeps it only as
        the PHI mode allows (`GET /api/audit/{id}`). `input_hash` is `hmac-sha256:<key id>:<hex>`
        where the deployment has an input hash key (always with PHI), else `sha256:<hex>`.

        An agent whose manifest declares written outputs (spec 0.4) answers with `outputs` too:
        what it wrote, by name. They are returned here and are not kept with the decision."""
        decided = decision_for(f"{namespace}/{name}").decide(
            payload, shadow=_is_shadow(request, shadow)
        )
        written = {"outputs": dict(decided.outputs)} if decided.outputs else {}
        return {**decided.to_dict(), **written, **NOTICE}

    @app.post(
        "/api/agents/{namespace}/{name}/guard",
        tags=["Decisions"],
        summary="Check an LLM's draft reply",
    )
    def guard_draft(namespace: str, name: str, payload: JsonBody) -> dict[str, Any]:
        """Check an LLM's draft reply: `released` (send `reply`), `held_for_review` or
        `blocked` (send nothing; follow `fallback`). Audited like any decision; `decision` is the
        whole decision, as `…/decide` answers it."""
        result = guard.check(decision_for(f"{namespace}/{name}"), payload)
        return {**result.to_dict(), "decision": result.decision.to_dict(), **NOTICE}

    @app.post(
        "/api/agents/{namespace}/{name}/fhir/decide",
        tags=["FHIR and CDS Hooks"],
        summary="Make a decision from FHIR",
    )
    def decide_fhir(
        request: Request,
        namespace: str,
        name: str,
        payload: JsonBody,
        shadow: bool = False,
    ) -> dict[str, Any]:
        """Decide from FHIR R4 resources: a Bundle, one resource, or a CDS Hooks prefetch object.
        The agent's `fhir/mapping.yaml` maps them onto its input. Returns the decision and a FHIR
        `Task` for the work queue.

        Inputs first pass a structural R4 check in every mode: an unknown element or resource
        type, an element of the wrong JSON type, or a primitive without its format is a `400`
        OperationOutcome (issue code `structure` or `value`). A complex element R4 requires, a
        code outside an R4 required value set, and US Core 6.1.0's rules are checked under
        `fhir_validation` (`/api/health`): `warn` records them in `decision.input_issues`,
        `enforce` blocks. An error on this route is an `OperationOutcome`. For FHIR out as well,
        use `POST /fhir/$decide`.

        A note whose data does not decode, or that is only a link (`url`), cannot be read: the
        decision is ABSTAIN with the reason (`undecodable`, `not_fetched`). This route never reads
        a link, so a spec 0.3 reference the record does not hold is `not_fetched` too.
        `decision.data_coverage` says what the mapping looked for and found, as counts and the
        agent's own codes."""
        result, task = decision_for(f"{namespace}/{name}").decide_fhir(
            payload, shadow=_is_shadow(request, shadow)
        )
        return {"decision": result.to_dict(), "task": task, **NOTICE}

    @app.post(
        "/api/agents/{namespace}/{name}/actions",
        tags=["Decisions"],
        summary="Authorize an action",
    )
    def authorize_action(namespace: str, name: str, body: ActionRequest) -> dict[str, Any]:
        """Ask whether one action the agent declares may be run, with these arguments, for a
        decision already recorded. Mendari authorizes and records; it runs nothing: run the
        action yourself, and only when `disposition` is `released`.

        `disposition` is `released`, `held` (it waits for the decision's reviewer: ask again
        later), `blocked` or `denied` (a reviewer refused it), with a `reason` code. **A blocked
        action is not an HTTP error**, as a `BLOCK` decision is not: check `disposition`, not
        only the status code. The gate is the one that judged the actions the agent proposed
        with its answer: an effect that reads or drafts is released with any decision that is
        not `BLOCK`; one that writes or contacts a patient only with an `AUTO` decision, or
        once the decision's reviewer confirms it; an order never without a person. An action
        the agent's manifest does not declare, or whose arguments do not match its schema, is
        blocked.

        **Asking again is safe.** The same action with the same arguments for the same
        decision answers how it stands now, with `repeated: true`, and records nothing new: so
        this is the check to make right before running a tool, and how a caller holding a held
        action learns that a reviewer released it. A new action is recorded in the audit trail
        (`action.proposed`) with who asked; the trail keeps the arguments' hash, never the
        arguments, which are kept as the decision's input is.

        A decision that is unknown, another organization's or another agent's is a `404`."""
        pack_id = f"{namespace}/{name}"
        pack_or_404(pack_id)
        problem = actions.shape_problem(body.name, body.arguments)
        if problem is not None:
            raise HTTPException(422, problem)
        found = decision_for(pack_id).authorize_action(body.decision_id, body.name, body.arguments)
        return {**found, **NOTICE}

    @app.get(
        "/api/agents/{namespace}/{name}/validation-brief",
        tags=["Evaluation and monitoring"],
        summary="Executive validation & readiness brief",
        name="agent_validation_brief",
    )
    def pack_validation_brief(
        request: Request,
        namespace: str,
        name: str,
        days: int = 30,
        minutes_per_case: float = validation_brief.DEFAULT_MINUTES_PER_CASE,
        format: Literal["json", "markdown"] = "json",
    ) -> Any:
        """Aggregate clinical and operational validation brief for an agent (zero PHI): readiness
        gates, exact Clopper-Pearson confidence intervals, shadow-pilot status, reclaimable
        clinician hours, and clustered human-override patterns for closed-loop improvement.

        Computed from your organization's audit trail when asked; it is not signed (`signed` is
        `false`, and `about` says what it is). A gate's `status` is `pass`, `warn`, `fail` or
        `no_data`: a gate with nothing to measure (no decision automated, as in every shadow
        pilot; too few decisions for the monitor to compare) says so and never passes."""
        pack = pack_or_404(f"{namespace}/{name}")
        days = max(1, min(days, MONITOR_MAX_DAYS))
        minutes_per_case = max(0.5, min(minutes_per_case, 60.0))
        org = org_of(auth.acting(request))
        held = store.holds(org)
        if auth.memberships:
            held = [h if h["org"] is not None else operators_hold(h) for h in held]
        gov = store.active_approval(pack.id, pack.digest)
        reg = (
            {
                "active": store.registry_approval(pack.id, pack.digest),
            }
            if pack.origin == "hub"
            else None
        )
        mon_days = max(MONITOR_MIN_DAYS, min(days, MONITOR_MAX_DAYS))
        mon = monitor(
            store,
            pack,
            days=mon_days,
            providers=health_providers.get(pack.id),
            models=running_models.get(pack.id),
        )
        brief = validation_brief.build(
            store,
            pack,
            days=days,
            minutes_per_case=minutes_per_case,
            holds=held,
            governance_active=gov,
            registry_approval=reg,
            monitoring=mon,
        )
        if format == "markdown":
            return Response(
                validation_brief.render_markdown(brief),
                media_type="text/markdown; charset=utf-8",
                headers={
                    "content-disposition": f'attachment; filename="{name}-validation-brief.md"'
                },
            )
        return brief

    @app.get(
        "/api/agents/{namespace}/{name}/committee-packet",
        tags=["Governance"],
        summary="Committee packet",
        name="agent_committee_packet",
    )
    def committee_packet(
        request: Request,
        namespace: str,
        name: str,
        days: int = packet.DEFAULT_DAYS,
        format: Literal["json", "html"] = "json",
    ) -> Any:
        """Everything an AI governance committee reads about one agent, at the version served
        here, as your organization sees it (`mendari.committee-packet/1`): what it is and who
        made it; its intended use and what it must not be used for; its risk level and what the
        runtime enforces; how a person stays in the loop; the publisher's evidence; your
        organization's own decisions, shadow pilot, overrides and the validation brief's gates;
        known failure modes; monitoring; change control; data handling; and who approved what.
        Then two appendices: `not_stated`, every item a framework asks for that neither the
        publisher nor your organization has stated (never filled with a guess), and
        `crosswalk`, where each section relates to the CHAI Applied Model Card, the HTI-1
        source attributes, the NIST AI Risk Management Framework and the Joint Commission and
        CHAI guidance, each entry with its source, an address and the day it was checked.

        Counts, ids, label codes, names of configurations and dates only: never case text or
        patient data, and no person's name but an approver's. It is a reading aid prepared by
        the software: not signed, and not a certification, an attestation of compliance or
        legal advice. `days` is held to 14 to 180. `format=html` answers one self-contained
        page to print, as a download; `sha256` is the hash of the JSON packet without it."""
        pack = pack_or_404(f"{namespace}/{name}")
        days = max(packet.MIN_DAYS, min(days, packet.MAX_DAYS))
        org = org_of(auth.acting(request))
        held = store.holds(org)
        if auth.memberships:  # an operator's hold: what it covers, never its reason (ADR-0008)
            held = [h if h["org"] is not None else operators_hold(h) for h in held]
        deployment = packet.Deployment(
            phi_mode=phi.mode,
            input_hash=phi.hash_kind,
            retention_days=retention_days,
            scheduled=bool(cron_secret),
            sign_in=auth.enabled,
            accounts=None if sign_in is None else sign_in.accounts,
            session_idle_minutes=session_lifetime.idle // 60,
            session_max_hours=session_lifetime.absolute // 3600,
            checkpoint_key=(
                key_id(checkpoint_signer.public_key()) if checkpoint_signer is not None else None
            ),
            alert_destination=alerts is not None,
            alerts_signed=alerts is not None and bool(alerts.secret),
            provider_allowed_origins=provider_origins,
            agent_endpoint=bool(os.environ.get(AGENT_ENDPOINT_ENV, "").strip()),
            providers=health_providers.get(pack.id),
            models=running_models.get(pack.id),
        )
        built = packet.build(
            store,
            pack,
            deployment=deployment,
            days=days,
            organization=org if auth.enabled else None,
            holds=held,
            # Which version ran where is the deployment operator's in memberships mode.
            records=None if auth.memberships else store.deployments(pack.id, limit=20),
            recall=recall_watch.held(pack),
        )
        if format == "html":
            return Response(
                packet.render_html(built),
                media_type="text/html; charset=utf-8",
                headers={
                    "content-disposition": f'attachment; filename="{name}-committee-packet.html"'
                },
            )
        return built

    # -- monitoring ------------------------------------------------------------------------------

    @app.get(
        "/api/monitoring",
        tags=["Evaluation and monitoring"],
        summary="Monitor an agent over time",
    )
    def monitoring(pack_id: AgentId, days: int = 30) -> dict[str, Any]:
        """The served version of the agent over time for this organisation: weekly series, drift
        and alerts, against its benchmark only when that measured this build with the providers
        and models that run here, under the same governance (`benchmark.comparable`;
        `benchmark.reason` otherwise). `days` is held to 14 to 180, in whole weeks."""
        pack = pack_or_404(pack_id)
        days = max(MONITOR_MIN_DAYS, min(days, MONITOR_MAX_DAYS))
        report = monitor(
            store,
            pack,
            days=days,
            providers=health_providers.get(pack.id),
            models=running_models.get(pack.id),
        )
        if auth.memberships:  # when the deployment changed is the operator's (ADR-0008)
            report["configuration_since"] = None
        return report

    # -- governance ------------------------------------------------------------------------------

    @app.get(
        "/api/deployments",
        tags=["Evaluation and monitoring"],
        summary="List deployments",
    )
    def deployments(
        request: Request, pack_id: AgentFilter = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Which agent version runs where, with which providers, since when (newest first). In
        memberships mode the deployment's records are the operator's (ADR-0008): they answer to
        an administrator of the organization that operates the deployment
        (`MENDARI_OPERATOR_ORG`), and are a `404` for everyone else."""
        if not runs_deployment(request):
            raise HTTPException(404, "the deployment's records are its operator's")
        if pack_id is not None:
            decision_for(pack_or_404(pack_id).id)  # this server's own row is recorded first
        return store.deployments(pack_id, limit=min(max(limit, 0), 200))

    def refuse_own_version(request: Request, pack: Pack) -> None:
        """Separation of duties reaches the automation approvals too (LP-20h review): whoever
        wrote a version of an organization's store, or added it from a registry, or made a
        version it was made from, does not approve it for automation; someone else does."""
        principal = principal_of(request)
        if principal.kind == "open" or pack.id in packs or pack.id not in promoted_here():
            return  # not a version of this organization's store
        if principal.id in app.state.pack_store.authors(org_of(principal), pack.digest):
            raise HTTPException(
                403,
                "the author of a store version, or whoever added it from a registry, does not "
                "approve it: someone else does",
            )

    @app.get(
        "/api/governance",
        tags=["Governance"],
        summary="List automation approvals",
    )
    def governance(pack_id: AgentFilter = None) -> dict[str, Any]:
        """Approvals for this organization, and what an agent's risk level allows."""
        found = store.approvals(pack_id)
        if pack_id is None:
            return {"approvals": found}
        pack = pack_or_404(pack_id)
        level = int(pack.manifest.risk.level)
        active = store.active_approval(pack.id, pack.digest)
        return {
            "pack_id": pack.id,
            "risk_level": level,
            "digest": pack.digest,
            "approvable": level in (2, 3),
            "active": active,
            "approvals": found,
        }

    @app.post(
        "/api/governance",
        status_code=201,
        tags=["Governance"],
        summary="Approve automation",
    )
    def approve(request: Request, body: ApprovalRequest) -> dict[str, Any]:
        """Record an organization's approval for a risk level 2 or 3 agent to automate (ADR-0002): a
        reference (e.g. committee minutes) and how long it lasts. It applies to exactly this
        agent version (its digest)."""
        pack = pack_or_404(body.pack_id)
        approver = acting_as(request, body.approver, "approver")
        refuse_own_version(request, pack)
        expires = datetime.now(UTC) + timedelta(days=body.days)
        approval = store.approve_automation(
            pack_id=pack.id,
            pack_digest=pack.digest,
            risk_level=int(pack.manifest.risk.level),
            approver=approver or "",
            reference=body.reference,
            expires_at=expires.isoformat(timespec="seconds"),
        )
        log_event(request, "governance.approved", ids=[str(approval["id"])])
        return approval

    @app.delete(
        "/api/governance/{approval_id}",
        status_code=204,
        tags=["Governance"],
        summary="Revoke an approval",
    )
    def revoke(request: Request, approval_id: str) -> Response:
        """Revoke an approval; the agent's automation falls back to its risk-level cap."""
        if not store.revoke_approval(approval_id, revoked_by=principal_of(request).actor):
            raise HTTPException(404, "no such approval")
        log_event(request, "governance.revoked", ids=[approval_id])
        return Response(status_code=204)

    @app.get(
        "/api/registry-approvals",
        tags=["Governance"],
        summary="List approvals of agents from a registry",
    )
    def registry_approvals(pack_id: AgentFilter = None) -> dict[str, Any]:
        """An agent pulled from a registry never automates until your organization approves its
        exact digest, at every risk level (a risk level 2 or 3 agent needs its governance
        approval too). Your organization's approvals, and for `pack_id` where it came from."""
        found = store.registry_approvals(pack_id)
        if pack_id is None:
            return {"approvals": found}
        pack = pack_or_404(pack_id)
        return {
            "pack_id": pack.id,
            "digest": pack.digest,
            "origin": pack.origin,
            "pulled": {k: v for k, v in pack.pulled.items() if k != "yank"},
            "recalled": pack.pulled.get("yank") is not None,
            "active": store.registry_approval(pack.id, pack.digest),
            "approvals": found,
        }

    @app.post(
        "/api/registry-approvals",
        status_code=201,
        tags=["Governance"],
        summary="Approve an agent from a registry",
        name="approve_registry_agent",
    )
    def approve_registry_pack(request: Request, body: RegistryApprovalRequest) -> dict[str, Any]:
        """Record that your organization approves exactly this pulled agent (its digest) to
        automate under its policy: chained `registry_pack.approved`. It applies to the digest
        served now, a reference (e.g. the review's minutes) names why, and it lasts until
        revoked. It never lifts a recall, a hold or a risk level's cap."""
        pack = pack_or_404(body.pack_id)
        if pack.origin != "hub":
            raise HTTPException(422, "only an agent pulled from a registry needs this approval")
        if body.pack_digest != pack.digest:
            raise HTTPException(422, "pack_digest is not the digest this deployment serves")
        approver = acting_as(request, body.approver, "approver")
        refuse_own_version(request, pack)
        approval = store.approve_registry_pack(
            pack_id=pack.id,
            pack_digest=pack.digest,
            registry=str(pack.pulled.get("registry") or "unknown"),
            approver=approver or "",
            reference=body.reference,
        )
        log_event(request, "registry_pack.approved", ids=[str(approval["id"])])
        return approval

    @app.delete(
        "/api/registry-approvals/{approval_id}",
        status_code=204,
        tags=["Governance"],
        summary="Revoke an approval of an agent from a registry",
        name="revoke_registry_agent",
    )
    def revoke_registry_pack(request: Request, approval_id: str) -> Response:
        """Revoke it: the pulled agent stops automating at once."""
        if not store.revoke_registry_approval(approval_id, revoked_by=principal_of(request).actor):
            raise HTTPException(404, "no such approval")
        log_event(request, "registry_pack.revoked", ids=[approval_id])
        return Response(status_code=204)

    @app.get("/api/holds", tags=["Governance"], summary="Automation holds in force")
    def list_holds(request: Request) -> list[dict[str, Any]]:
        """The automation holds in force for your organization: its own and the deployment's
        (set from the command line). While one covers a decision, AUTO becomes HUMAN_REVIEW.
        In memberships mode a hold of the deployment's shows what it covers and since when, not
        the operator's reason or name (ADR-0008)."""
        held = store.holds(org_of(auth.acting(request)))
        if not auth.memberships:
            return held
        return [hold if hold["org"] is not None else operators_hold(hold) for hold in held]

    @app.post(
        "/api/holds",
        status_code=201,
        tags=["Governance"],
        summary="Hold automation",
    )
    def hold_automation(request: Request, body: HoldRequest) -> dict[str, Any]:
        """Stop automation for your organization: every agent, one agent (`pack_id`), or one
        version of it (`pack_digest`). Decisions still run and are recorded; any that would be
        `AUTO` goes to a person instead, with the same recommendation and priority. Chained as
        `automation.held`; it lasts until lifted (`DELETE /api/holds/{id}`)."""
        if body.pack_id is not None:
            served = pack_or_404(body.pack_id)
            if body.pack_digest is not None and body.pack_digest != served.digest:
                raise HTTPException(422, "pack_digest is not the digest this deployment serves")
        elif body.pack_digest is not None:
            raise HTTPException(422, "a hold on a digest names its agent")
        principal = auth.acting(request)
        try:
            hold = store.hold_automation(
                org=org_of(principal),
                pack_id=body.pack_id,
                pack_digest=body.pack_digest,
                reason=body.reason,
                set_by=principal.actor,
            )
        except ReviewError as error:
            raise HTTPException(422, str(error)) from None
        forget_holds()
        log_event(request, "automation.held", ids=[hold["id"]])
        return hold

    @app.delete(
        "/api/holds/{hold_id}",
        status_code=204,
        tags=["Governance"],
        summary="Lift an automation hold",
    )
    def lift_hold(request: Request, hold_id: str) -> Response:
        """Lift your organization's hold. A deployment's hold (set from the command line, for
        every organization) is lifted there too: `mendari automation lift`."""
        principal = auth.acting(request)
        if not store.lift_hold(hold_id, lifted_by=principal.actor, org=org_of(principal)):
            raise HTTPException(404, "no such hold in force for your organization")
        forget_holds()
        log_event(request, "automation.lifted", ids=[hold_id])
        return Response(status_code=204)

    # -- evaluation --------------------------------------------------------------------------

    @app.post(
        "/api/agents/{namespace}/{name}/evals",
        tags=["Evaluation and monitoring"],
        summary="Run an evaluation",
    )
    def run_eval(namespace: str, name: str, request: EvalRequest, http: Request) -> dict[str, Any]:
        """Evaluate the agent (or another provider configuration) on one of its datasets, and
        compare with the published benchmark (an agent from a registry: `comparison` says it is
        not compared with the benchmark it carries). Calls to remote providers are capped at 200
        cases."""
        pack_id = f"{namespace}/{name}"
        pack = pack_or_404(pack_id)
        # A store-less Decision: evaluation traffic must not pollute the operational audit log.
        active = decision_for(pack_id)
        chosen = {slot: info.variant for slot, info in active.provider_info.items()}
        asked = request.providers or {}
        if pack.community:
            # A community pack runs a remote variant only where the deployment named the pack
            # for it (`community/<name>:slot=variant`), which `chosen` already holds.
            refused = sorted(
                f"{slot}={variant}"
                for slot, variant in asked.items()
                if variant != chosen.get(slot) and not _local_variant(pack, slot, variant)
            )
            if refused:
                raise MendariError(
                    f"{pack.id} is a community agent: it runs a remote variant only where the "
                    f"deployment names it in its settings ({pack.id}:slot=variant), not "
                    f"{', '.join(refused)}"
                )
        # Provider calls count against the daily budgets and the organization's evaluation
        # quotas (`eval_jev`, `eval_llm`), never its decisions' quotas.
        decision = Decision(
            pack,
            providers={**chosen, **asked},
            budget=None if budget is None else budget.for_evaluation(),
            deadline_s=deadline_s,  # each of its decisions has the deployment's deadline
            # An evaluation measures the pack's own policy, as `mendari eval` does: a pack from
            # a registry is capped only where it is served (LP-18d).
            origin_cap=False,
            connection_source=connection_for,  # the service that answers here (ADR-0021)
        )
        dataset = dataset_for(pack, request.dataset)
        if decision.is_remote:
            dataset = dataset.head(min(request.limit or REMOTE_EVAL_LIMIT, REMOTE_EVAL_LIMIT))
        elif request.limit:
            dataset = dataset.head(request.limit)
        workers = REMOTE_EVAL_WORKERS if decision.is_remote else 1
        log_event(http, "eval.run", ids=[pack.id], count=len(dataset.records))
        run = evaluation(decision, dataset, workers=workers)
        report = run.report
        baseline = benchmark(pack)
        if baseline is not None and request.dataset != pack.manifest.evaluation.benchmark_dataset:
            # A pack from a registry carries its publisher's benchmark, never a reference here
            # (ADR-0012): the answer says so instead of comparing with it.
            report["comparison"] = (
                not_compared(FROM_REGISTRY)
                if pack.origin == "hub"
                else compare(report, baseline, pack.manifest.evaluation.degradation_tolerance)
            )
        store.save_report(report, ledger=run.ledger.dumps(), usage=run.usage)
        return _with_figures(report)

    @app.get(
        "/api/evals",
        tags=["Evaluation and monitoring"],
        summary="List evaluation reports",
    )
    def list_evals(pack_id: AgentFilter = None) -> list[dict[str, Any]]:
        """Saved evaluation reports, newest first, each with its figures (`figures`)."""
        return [_with_figures(report) for report in store.reports(pack_id)]

    # -- review & audit ----------------------------------------------------------------------

    @app.get(
        "/api/review/summary",
        tags=["Review and feedback"],
        summary="Pending review counts by agent",
    )
    def review_summary() -> list[dict[str, Any]]:
        """Pending and urgent review counts across every agent in this organization (aggregate
        counts only, no inputs or PHI)."""
        return store.pending_by_pack()

    @app.get(
        "/api/review",
        tags=["Review and feedback"],
        summary="The review queue",
    )
    def review_queue(
        request: Request, pack_id: AgentFilter = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Decisions waiting for a human (`HUMAN_REVIEW`, `ABSTAIN`), most urgent first."""
        limit = min(max(limit, 0), 500)  # never negative: SQLite reads LIMIT -1 as no limit
        queue = store.pending_reviews(pack_id, limit=limit, reader=input_reader(request))
        logged_reads(request, queue)
        return queue

    @app.post(
        "/api/review/{decision_id}",
        tags=["Review and feedback"],
        summary="Resolve a review",
    )
    def resolve(decision_id: str, request: ReviewRequest, http: Request) -> dict[str, Any]:
        """Record a reviewer's final label for a decision in the queue: one of the labels of the
        decision's question. A note is kept apart from the audit chain; the answer repeats it.

        The decision's held actions are resolved with it: **confirming the recommendation
        releases them, overriding it denies them**, and `deny_actions` refuses some of them
        while confirming the label. The answer lists each under `actions`, when there were
        any. A release is final."""
        return store.resolve_review(
            decision_id,
            final_label=request.final_label,
            reviewer=acting_as(http, request.reviewer) or "",
            note=request.note,
            labels=question_labels,
            deny_actions=request.deny_actions,
        )

    @app.get(
        "/api/audit",
        tags=["Audit"],
        summary="List decisions",
    )
    def audit_log(
        pack_id: AgentFilter = None,
        outcome: str | None = None,
        review_status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """The audit trail, newest first, with filters and `limit`/`offset` paging. Outside PHI
        mode `off`, no input: a reader opens one decision (`/api/audit/{id}`), chained."""
        listed = store.query(
            pack_id=pack_id,
            outcome=outcome,
            review_status=review_status,
            limit=min(max(limit, 0), 500),
            offset=min(max(offset, 0), MAX_OFFSET),
        )
        return store.withhold_inputs(listed)

    # Before `/api/audit/{decision_id}`, which would take its name for a decision's id.

    @app.get(
        "/api/audit/checkpoint",
        tags=["Audit"],
        summary="The signed daily checkpoint",
        responses={404: {"description": "No key is set here, or nothing is signed yet"}},
    )
    def signed_checkpoint() -> dict[str, Any]:
        """The newest signed daily checkpoint (LP-21k): the audit chain's head on a day, signed
        with this deployment's Ed25519 key, and that key's public half. Keep a copy of this
        answer anywhere: `mendari audit verify --against <copy> --public-key <key>` later shows
        whether the chain was rewritten from before it. Take the public key from where the operator
        publishes it, not only from here. Public: a hash and a day, never a position or a
        count. Only a statement this key signed for the day it is filed under, up to today, is
        published. Not found until the operator sets the key and a statement is signed with it."""
        if checkpoint_signer is None:
            raise HTTPException(404, "no checkpoint is signed here: no key is set")
        public = checkpoint_signer.public_key()
        now = time.monotonic()
        with store_lock:
            stale = now - published["at"] > CHECKPOINT_SECONDS
            statement = published["statement"]
        if stale:
            rows = store.statements(until=clock.today().isoformat())
            found = next(
                (
                    row["statement"]
                    for row in rows
                    if checkpoint.problem(row["statement"], public) is None
                    and row["statement"]["date"] == row["day"]
                ),
                None,
            )
            with store_lock:
                published.update(at=now, statement=found)
                statement = found
        if statement is None:
            raise HTTPException(404, "no checkpoint is signed with this key yet")
        return {
            "statement": statement,
            "public_key": public_text(public),
            "key_id": key_id(public),
        }

    @app.get(
        "/api/audit/verify",
        tags=["Audit"],
        summary="Verify the audit chain",
    )
    def verify(request: Request) -> dict[str, Any]:
        """Recompute the hash chain over every event; `ok: false` means the trail was altered.
        In memberships mode the whole chain is the operator's (ADR-0008): it answers to an
        administrator of the organization that operates the deployment
        (`MENDARI_OPERATOR_ORG`), and is a `404` for everyone else, who verify it from the
        command line (`mendari audit verify`). `POST /api/system/audit/verify` does the same
        and keeps when it was done."""
        if not runs_deployment(request):
            raise HTTPException(404, OPERATORS_CHAIN)
        return store.verify()

    @app.get(
        "/api/audit/export/fhir",
        tags=["FHIR and CDS Hooks"],
        summary="Export the audit trail as FHIR",
    )
    def export_audit_fhir(
        request: Request,
        pack_id: AgentFilter = None,
        since: str | None = None,
        until: str | None = None,
    ) -> StreamingResponse:
        """The audit trail as FHIR NDJSON (AuditEvent and Provenance, ids and codes only),
        streamed in chain order; `since` / `until` bound the event time: a UTC day (`2026-09-01`)
        or an instant with its offset, else a `400`."""
        since, until = export_bound("since", since), export_bound("until", until)
        log_event(request, "export.audit")  # its filters are query values: not logged
        org = scope_of(auth.acting(request))  # resolved before the stream starts

        def stream() -> Iterator[str]:
            after = 0
            while True:
                # Re-enter tenancy per page rather than across `yield`: Starlette iterates a
                # sync generator in a threadpool where each step runs in its own Context copy.
                with tenancy.acting_for(org):
                    page = store.events(pack_id=pack_id, since=since, until=until, after_seq=after)
                if not page:
                    break
                for resource in audit_fhir.resources(page):
                    yield json.dumps(resource, separators=(",", ":")) + "\n"
                after = page[-1]["seq"]

        return StreamingResponse(stream(), media_type="application/fhir+ndjson")

    @app.get(
        "/api/usage",
        tags=["Evaluation and monitoring"],
        summary="Your organization's remote calls and their cost",
    )
    def usage(request: Request, days: Annotated[int, Query(ge=1, le=366)] = 30) -> dict[str, Any]:
        """The usage ledger (LP-21g) of your organization over the last `days` local days, today
        included (1 to 366; 30): its remote model calls (Jev, an LLM) by day, provider type and
        purpose (`decision` or `evaluation`), with how many were priced, their tokens and their
        cost, `null` (unknown) unless every call was priced, never a partial sum; and `totals`
        over those days by provider type and purpose, the same way. In open mode, every
        organization's rows, each naming its own. Administrators only."""
        org = scope_of(auth.acting(request))
        until = clock.today()
        since = until - timedelta(days=days - 1)
        rows = store.usage_ledger(org, since=since.isoformat(), until=until.isoformat())
        return {
            "since": since.isoformat(),
            "until": until.isoformat(),
            "rows": rows,
            "totals": usage_totals(rows),
        }

    @app.get(
        "/api/audit/administrative",
        tags=["Audit"],
        summary="Your organization's administrative events",
    )
    def administrative_events(
        request: Request,
        kind: Annotated[list[str] | None, Query()] = None,
        subject: str | None = None,
        since: str | None = None,
        until: str | None = None,
        after: str | None = None,
        limit: int = 100,
        order: Literal["asc", "desc"] = "asc",
    ) -> dict[str, Any]:
        """The chained events of what your organization owns, oldest first: its keys, clients,
        automation approvals and holds, evaluation reports, its record, memberships,
        invitations and requests to join (ADR-0008). Each is `{subject, event, payload,
        created_at, hash}`: the event's chain id, its kind, what was chained, when, and its
        hash, which `mendari audit verify` checks. Never its place in the deployment's chain,
        nor another organization's events, nor the deployment's own (the operator's).

        `kind` (repeatable) and `subject` (a chain id, `key:<id>`) narrow it; `since` and
        `until` are instants with their offset; `limit` is 1 to 500 (100). `order=desc` lists
        newest first. `next`, when there is more, is the `after` of the next page, in the same
        order."""
        org = scope_of(auth.acting(request))
        try:
            page = store.administrative_events(
                org,
                after=administrative_cursor(after),
                kinds=administrative_kinds(kind, org),
                subject=chain_subject(subject),
                since=instant("since", since),
                until=instant("until", until),
                limit=min(max(limit, 1), 500),
                newest_first=order == "desc",
            )
        except ValueError as error:  # a cursor of no event of this list, whatever the reason
            raise HTTPException(400, str(error)) from None
        following = None
        if page.after is not None:
            following = encode_cursor(dict(zip(("subject", "hash"), page.after, strict=True)))
        return {"items": page.rows, "next": following}

    @app.get(
        "/api/audit/administrative/export/fhir",
        tags=["FHIR and CDS Hooks"],
        summary="Export your organization's administrative events as FHIR",
    )
    def export_administrative_fhir(
        request: Request,
        kind: Annotated[list[str] | None, Query()] = None,
        since: str | None = None,
        until: str | None = None,
    ) -> StreamingResponse:
        """Your organization's administrative events as FHIR NDJSON AuditEvents, ids and codes
        only (never a key's or client's name, a hold's reason or an approval's reference),
        streamed in chain order; `kind`, `since` and `until` as for the list."""
        org = scope_of(auth.acting(request))  # resolved before the stream starts
        kinds = administrative_kinds(kind, org)
        bounds = {"since": instant("since", since), "until": instant("until", until)}
        log_event(request, "export.audit.administrative")  # its filters are query values
        lines = (
            json.dumps(resource, separators=(",", ":")) + "\n"
            for resource in audit_fhir.administrative_resources(
                store.iter_administrative_events(org, kinds=kinds, **bounds)
            )
        )
        return StreamingResponse(lines, media_type="application/fhir+ndjson")

    @app.get(
        "/api/audit/{decision_id}",
        tags=["Audit"],
        summary="Get a decision",
    )
    def audit_record(request: Request, decision_id: str) -> dict[str, Any]:
        """One decision with every event recorded on it. An event with a note has `note_status`
        (`open`, `sealed` or `erased`) and `note`, its text when open: notes are read as inputs
        are, so under PHI mode `encrypt` only by reviewers and admins, and each read is chained
        (`input.read`).

        The record keeps no value of the input outside PHI mode `off`: under `hash` (or for a
        agent that keeps only the hash) its answers' `evidence` is empty; under `encrypt` the
        evidence is sealed with the input and comes back with it. An input issue names its
        resource by type and position (`Patient (entry 2)`), never by its id."""
        record = store.get(decision_id, reader=input_reader(request))
        if record is None:
            raise HTTPException(404, f"unknown decision {decision_id}")
        logged_reads(request, [record])
        return record

    @app.get(
        "/api/audit/{decision_id}/fhir",
        tags=["FHIR and CDS Hooks"],
        summary="A decision's audit trail as FHIR",
    )
    def audit_record_fhir(decision_id: str) -> JSONResponse:
        """One decision's audit trail as a FHIR `collection` Bundle."""
        record = store.item(decision_id)
        if record is None:
            raise HTTPException(404, f"unknown decision {decision_id}")
        bundle = audit_fhir.decision_bundle(record)
        return JSONResponse(bundle, media_type="application/fhir+json")

    @app.post(
        "/api/audit/{decision_id}/erase",
        tags=["Audit"],
        summary="Erase a decision's kept input",
    )
    def erase_input(request: Request, decision_id: str, body: EraseRequest) -> dict[str, Any]:
        """On request (`reason`: `request`, or `error` for one recorded by mistake), delete what
        a decision keeps apart from the chain: its encrypted input and evidence, and its notes.
        The record and the chain stay, so `/api/audit/verify` still passes; each erasure is
        chained (`input.erased`, `note.erased`). Backups, the database's history and earlier
        exports still hold it. A decision recorded under PHI mode `off` holds its input in its
        chained record, and an older record its evidence: nothing erases those without breaking
        the chain, so `still_in_chain` says they remain (and a `422` when nothing else is
        kept)."""
        if store.item(decision_id) is None:
            raise HTTPException(404, f"unknown decision {decision_id}")
        principal = principal_of(request)
        erased = store.erase_input(decision_id, erased_by=principal.actor, reason=body.reason)
        log_event(request, "input.erased", ids=[decision_id], reason=body.reason)
        return erased

    def require_cron(request: Request, job: str) -> None:
        """A scheduled job's route answers only `Authorization: Bearer $CRON_SECRET`, before any
        database work: without CRON_SECRET set it is not found; a wrong secret (a session or a
        key included) is `401`, logged as `<job>.refused`."""
        if not cron_secret:
            raise HTTPException(404, "Not Found")
        given = request.headers.get("authorization", "")
        if not hmac.compare_digest(given.encode(), f"Bearer {cron_secret}".encode()):
            security_log.event(f"{job}.refused", outcome="refused", reason="secret")
            raise HTTPException(401, "this route is for a scheduled job")

    def chain_broken(held: bool, reason: str) -> None:
        """The audit chain does not verify (`held` False), or it holds and the day's stored
        checkpoint statement is not one this deployment's key signed (`held` True): the
        security log's line (`audit.chain.broken`, with the fixed `reason`) and one alert to
        MENDARI_ALERT_WEBHOOK (`audit.broken`, recorded), the operator's, with no position or
        count. From the scheduled checkpoint, and from an operator's own verification
        (`POST /api/system/audit/verify`)."""
        security_log.event("audit.chain.broken", outcome="error", reason=reason)
        webhook = alerts
        if webhook is not None:
            try:
                check_webhook(webhook.webhook)
            except MendariError:
                monitor_logger.warning("the alert webhook is not usable: no alert is posted")
                webhook = None
        if webhook is None:
            return
        message = (
            "the day's stored checkpoint statement is not one this deployment's key signed for "
            "today: someone with the database changed it; follow the incident runbook"
            if held
            else "the audit chain does not verify from its last checkpoint: run "
            "`mendari audit verify` and follow the incident runbook"
        )
        deliver(
            webhook,
            event="audit.broken",
            org=None,
            alerts=[{"severity": "critical", "kind": "audit_broken", "message": message}],
            lines=[f"[critical] audit_broken: {message}"],
            attempts=3,
            timeout=10,
            store=store,
        )

    # The newest signed statement, reused on one instance for CHECKPOINT_SECONDS (the route that
    # publishes it is public); the scheduled checkpoint clears it when it signs.
    published: dict[str, Any] = {"at": -math.inf, "statement": None}

    @app.get("/api/internal/checkpoint", include_in_schema=False)
    def scheduled_checkpoint(request: Request) -> dict[str, Any]:
        """The scheduled checkpoint (LP-21j, LP-21k): verifies the chain from its last
        checkpoint and, when it holds, records the head as the next one and, with
        MENDARI_CHECKPOINT_KEY, signs the day's statement (published at
        `/api/audit/checkpoint`). A chain that does not verify records and signs nothing: it is
        logged (`audit.chain.broken`) and alerted to MENDARI_ALERT_WEBHOOK (`audit.broken`,
        recorded), the operator's, with no position or count. So is a day's stored statement
        that is not this key's for today (someone with the database put it there), which is
        never published as signed. The answer says only whether it held and what was recorded
        and signed. `Authorization: Bearer $CRON_SECRET` only, as the purge."""
        require_cron(request, "checkpoint")
        status = store.record_checkpoint(signer=checkpoint_signer)
        refused = status.get("statement_refused")
        if status["ok"]:
            published["at"] = -math.inf  # read anew on this instance
        if status["ok"] and refused is None:
            security_log.event("audit.checkpoint", outcome="ok")
            scheduled_run(store, "checkpoint", ok=True, detail="since_checkpoint")
        else:
            reason = "statement" if status["ok"] else str(status.get("reason", "chain"))
            chain_broken(bool(status["ok"]), reason)
            scheduled_run(store, "checkpoint", ok=False, detail=reason)
        return {
            "ok": status["ok"] and refused is None,
            "recorded": status["recorded"] is not None,
            "signed": status.get("signed") is not None,
        }

    @app.get("/api/internal/retention", include_in_schema=False)
    def scheduled_retention(request: Request) -> dict[str, Any]:
        """The scheduled purge (Vercel Cron, `vercel.json`): deletes kept inputs and erases
        notes older than MENDARI_RETENTION_DAYS in batches, as `mendari phi purge` does, and
        chains a summary (`retention.purged`). Only with `Authorization: Bearer $CRON_SECRET`;
        without CRON_SECRET set it is not found."""
        require_cron(request, "retention")
        try:
            summary = store.purge(retention_days, source="schedule")
        except Exception:
            scheduled_run(store, "retention", ok=False, detail="error")
            raise
        security_log.event("retention.purged", count=summary["inputs"] + summary["notes"])
        scheduled_run(store, "retention", ok=True)
        return summary

    @app.post("/api/internal/backup", include_in_schema=False)
    def backup_reported(request: Request, outcome: Literal["ok", "failed"]) -> dict[str, Any]:
        """A backup of the database, reported by the schedule that made it (the one-install
        template's nightly dump, `deploy/site`): when it ran and whether the backup file could
        be read back, kept for the operator's checks (`system_runs`, outside the chain). The
        engine makes no backup itself and learns nothing else about one. `Authorization:
        Bearer $CRON_SECRET` only, as the purge; `outcome` is `ok` or `failed`."""
        require_cron(request, "backup")
        scheduled_run(store, "backup", ok=outcome == "ok")
        security_log.event("backup.reported", outcome="ok" if outcome == "ok" else "error")
        return {"recorded": True}

    @app.get("/api/internal/monitor", include_in_schema=False)
    def scheduled_monitor(request: Request) -> dict[str, Any]:
        """The scheduled monitor (LP-21f): first the alerts still pending sent again
        (`retry_due`, for MONITOR_RETRY_SECONDS at most), then each organization with a
        decision in the last 30 days, on its own, but the status organization
        (`MENDARI_STATUS_ORG`): every served agent's report (`evals/monitoring.py`), and one
        alert for the organization when any report alerts, recorded. The organizations are
        taken in order from a different one each day, and none is started once
        MONITOR_SECONDS have passed (the function's limit is 300 s): the ones left are counted,
        and an alert names them. The answer holds counts only. `Authorization: Bearer
        $CRON_SECRET` only, as the purge."""
        require_cron(request, "monitor")
        try:
            answered = monitor_each_organization()
        except Exception:
            scheduled_run(store, "monitor", ok=False, detail="error")
            raise
        scheduled_run(store, "monitor", ok=True)
        return answered

    def monitor_each_organization() -> dict[str, Any]:
        """What the scheduled monitor does (`scheduled_monitor`), and its answer."""
        started = time.monotonic()
        webhook = alerts
        if webhook is not None:
            try:
                check_webhook(webhook.webhook)
            except MendariError:
                monitor_logger.warning("the alert webhook is not usable: no alert is posted")
                webhook = None
        counts = {"organizations": 0, "alerting": 0, "delivered": 0, "not_delivered": 0}
        retried = (
            retry_due(store, webhook, deadline=started + MONITOR_RETRY_SECONDS)
            if webhook is not None
            else {"delivered": 0, "pending": 0, "failed": 0, "deferred": 0}
        )
        left: list[str] = []
        orgs = monitored_organizations(store, skip=status_org)
        if orgs:  # today's first: tomorrow's run starts at the next one
            first = clock.today().toordinal() % len(orgs)
            orgs = orgs[first:] + orgs[:first]
        for org in orgs:
            if time.monotonic() - started > MONITOR_SECONDS:
                left.append(org)
                continue
            counts["organizations"] += 1
            with tenancy.acting_for(org):
                reports = [
                    monitor(
                        store,
                        pack,
                        providers=health_providers.get(pack.id),
                        models=running_models.get(pack.id),
                    )
                    for pack in packs.values()
                ]
            if not any(report["alerts"] for report in reports):
                continue
            counts["alerting"] += 1
            if webhook is None:
                continue
            posted = post_alerts(webhook, org, reports, attempts=3, store=store)
            counts["delivered" if posted else "not_delivered"] += 1
        if left and webhook is not None:
            named = ", ".join(left[:MONITOR_NAMED])
            more = f" and {len(left) - MONITOR_NAMED} more" if len(left) > MONITOR_NAMED else ""
            message = (
                f"{len(left)} organization(s) not monitored: the schedule ran out of time "
                f"after {counts['organizations']} ({named}{more})"
            )
            incomplete = {"severity": "warning", "kind": "monitor_incomplete", "message": message}
            deliver(
                webhook,
                event="monitor.incomplete",
                org=None,
                alerts=[incomplete],
                lines=[f"[warning] monitor_incomplete: {message}"],
                attempts=1,
                timeout=10,
                store=store,
            )
        security_log.event("monitor.ran", count=counts["organizations"])
        return {**counts, "not_reached": len(left), "retried": retried}

    @app.post(
        "/api/audit/{decision_id}/outcome",
        tags=["Review and feedback"],
        summary="Record what really happened",
    )
    def record_outcome(decision_id: str, request: OutcomeRequest, http: Request) -> dict[str, str]:
        """Record what really happened, from a downstream system (e.g. where the message was
        really handled), for monitoring: one of the labels of the decision's question, kept as
        its `observed_label` beside a reviewer's `final_label`, which it never changes (a
        decision still awaiting review is resolved first, `422`). `source` names that system as
        an identifier; who recorded it is the caller (the signed-in person or API key),
        whatever the body says."""
        recorded_by = principal_of(http).actor
        store.record_outcome(
            decision_id,
            final_label=request.final_label,
            source=request.source,
            recorded_by=recorded_by,
            labels=question_labels,
        )
        return {"id": decision_id, "final_label": request.final_label, "recorded_by": recorded_by}

    @app.post(
        "/api/audit/{decision_id}/feedback",
        tags=["Review and feedback"],
        summary="Rate a decision",
    )
    def give_feedback(decision_id: str, request: FeedbackRequest, http: Request) -> dict[str, Any]:
        """Thumbs up or down, and on a thumbs-down the right answer, with an optional note (kept
        apart from the audit chain)."""
        return store.record_feedback(
            decision_id,
            rating=request.rating,
            reporter=acting_as(http, request.reporter, "reporter"),
            note=request.note,
            expected=request.expected.model_dump() if request.expected else None,
        )

    @app.get(
        "/api/feedback",
        tags=["Review and feedback"],
        summary="List feedback",
    )
    def list_feedback(
        request: Request,
        pack_id: AgentFilter = None,
        rating: Literal["up", "down"] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Ratings, newest first; each note as `GET /api/audit/{decision_id}` shows notes."""
        ratings = store.feedback(
            pack_id=pack_id,
            rating=rating,
            limit=min(max(limit, 0), 500),
            reader=input_reader(request),
        )
        logged_reads(request, ratings, id_key="decision_id")
        return ratings

    @app.get(
        "/api/agents/{namespace}/{name}/feedback/export",
        tags=["Review and feedback"],
        summary="Export feedback as a dataset",
    )
    def export_feedback(
        request: Request,
        namespace: str,
        name: str,
        source: Literal["ratings", "reviews", "overrides"] = "ratings",
        rating: Literal["up", "down"] | None = None,
    ) -> Response:
        """What people said about an agent's decisions, as JSONL for evaluation, the newest 1,000:
        rated decisions (`source=ratings`, thumbs `rating`, down unless given), decisions a
        reviewer resolved or an outcome was recorded for (`source=reviews`), or only cases where
        a clinician overrode the agent (`source=overrides`). `x-mendari-skipped` counts the ones
        stored without their input. Each record says it was exported (`attributes.exported_by`):
        a site's cases, which no agent archive holds."""
        pack = pack_or_404(f"{namespace}/{name}")
        if source in ("reviews", "overrides"):
            if rating is not None:
                raise HTTPException(422, f"rating filters ratings: leave it out with {source}")
            records, skipped = reviewed_dataset(
                store,
                pack_id=pack.id,
                reader=input_reader(request),
                only_overrides=(source == "overrides"),
            )
            stem = "overrides" if source == "overrides" else "reviewed"
            filename = f"{name}-{stem}.jsonl"
        else:
            rating = rating or "down"
            records, skipped = feedback_dataset(
                store, pack_id=pack.id, rating=rating, reader=input_reader(request)
            )
            filename = f"{name}-feedback-{rating}.jsonl"
        log_event(request, "export.feedback", ids=[pack.id], count=len(records))
        if phi.mode != "off" and input_reader(request) is not None and records:
            shown = [str(record["attributes"]["decision_id"]) for record in records]
            log_event(request, "phi.inputs.read", ids=shown, count=len(shown))
        body = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
        return Response(
            body,
            media_type="application/x-ndjson",
            headers={
                "content-disposition": f'attachment; filename="{filename}"',
                # Decisions stored with audit.store_input=hash have no text to export.
                "x-mendari-skipped": str(skipped),
            },
        )

    @app.get(
        "/api/stats",
        tags=["Audit"],
        summary="Decision statistics",
    )
    def stats(pack_id: AgentFilter = None) -> dict[str, Any]:
        """Counts by outcome, the review backlog, override rate, latency and cost; and the
        overview's periods (`periods`: the last 7 and 30 days and all time, each with the one
        before), their shares of cases as figures."""
        counts = store.stats(pack_id)
        return {**counts, "periods": figures.periods(counts["daily"], clock.today())}

    # -- CDS Hooks ---------------------------------------------------------------------------

    # A request with more orders than the decide limit's burst could never be admitted: with
    # limits on, the burst is also the most one request may decide.
    most_orders = cds_hooks.MAX_ORDERS
    if limiter.mode == "on":
        most_orders = min(most_orders, limiter.rates["decide"].burst)

    @app.get(
        "/cds-services",
        tags=["FHIR and CDS Hooks"],
        summary="CDS Hooks discovery",
    )
    def cds_discovery() -> dict[str, Any]:
        """The CDS Hooks services this deployment offers (one per agent that declares a hook).
        With sign-in on, a key or token scoped to deciding reads it, and so does a registered CDS
        client's own signed token (`MENDARI_CDS_CLIENTS`), whose `aud` is this URL."""
        return cds_hooks.discovery(served_here().values())

    @app.post(
        "/cds-services/{service_id}",
        tags=["FHIR and CDS Hooks"],
        summary="Call a CDS Hooks service",
    )
    def cds_invoke(service_id: str, request: JsonBody, http: Request) -> dict[str, Any]:
        """A CDS Hooks request (context and prefetch) → cards with the decision and its reasons.
        An order hook decides each draft order the agent is about, at most 50 a request: each
        counts as one decision against the rate limit and the principal's daily cap. A
        registered CDS client calls with its own signed token (RS384 or ES384, `aud` this
        service's URL, a `jti` used once); the `fhirServer` it sends must be one it registered,
        else `403` before anything is fetched or decided.

        A prefetch key left out, or sent empty, is read from `fhirServer` (an allow-listed one
        only); with no `fhirServer` the request is a `412`. A null value is "no data" and is not
        read. A structure error in what was sent is a `400`. When a key cannot be read (a failed
        read, an OperationOutcome, a value that is not a resource, a resource or search page
        that is not R4), the hook still answers `200`: the decision is ABSTAIN with the key and a
        fixed reason, it is recorded with its `data_coverage` record, and its card says so. A spec
        0.3 agent also reads, from that `fhirServer` only and under its base, what a link the
        record does not hold points to (a note's Binary, a followed reference): at most 50 notes
        and 50 followed references a decision and 100 reads a request, inside the decision's
        deadline, each read once a request; a link it does not read abstains the same way. A
        prefetch key of a type the site declared that `fhirServer` does not offer
        (`MENDARI_EHR_DECLARATIONS`) is not read for an agent that may read its list without it:
        the decision records the declaration, and never automates."""
        pack = next(
            (
                p
                for p in served_here().values()
                if p.manifest.cds_hooks is not None and cds_hooks.service_id(p) == service_id
            ),
            None,
        )
        if pack is None:
            raise PackNotFoundError(f"no CDS service {service_id!r}")
        caller = principal_of(http)
        if caller.kind == "cds":
            # A CDS client's token (LP-23b): its `fhirServer` must be one it registered, before
            # anything is charged, fetched or decided.
            registered = None if cds is None else cds.client(caller.id.removeprefix("cds:"))
            if registered is None or not fhir_server_allowed(registered, request.get("fhirServer")):
                security_log.event(
                    "auth.permission.refused",
                    outcome="refused",
                    reason="cds_fhir_server_not_registered",
                    actor=caller.id,
                    org=caller.org,
                )
                raise AuthError("the fhirServer is not one registered for this CDS client", 403)

        def charge(decisions: int) -> None:
            """The guard counted the request as one decision against the rate limit; the rest
            are counted here, all or none, before any is made (a refusal decides nothing). Then,
            once the rate limit admits them, all of them against the principal's daily cap."""
            principal = principal_of(http)
            if decisions > 1 and limiter.on:
                over = limiter.hit("decide", principal.id, cost=decisions - 1)
                if over is not None:
                    raise over
            if cap is not None and principal.kind != "open":
                capped = cap.check(principal.id, principal.org, cost=decisions)
                if capped is not None:
                    raise capped

        links = card_links(caller, request.get("fhirServer"))
        # What the request's FHIR server does not offer, as the site declared it for the
        # organization this call acts for (MENDARI_EHR_DECLARATIONS).
        org = tenancy.current() or tenancy.DEFAULT_ORG
        declared = (
            None if declarations is None else declarations.for_base(request.get("fhirServer"), org)
        )
        return cds_hooks.invoke(
            decision_for(pack.id),
            request,
            most=most_orders,
            charge=charge,
            links=links,
            declared=declared,
        )

    def card_links(caller: Principal, fhir_server: object) -> cds_hooks.CardLinks:
        """Where this request's cards may link (LP-23f): the audit view under
        `MENDARI_PUBLIC_URL`, read on each request as the FHIR base is, and the pack's SMART
        launch only when SMART is on and the request's `fhirServer` is registered for launches
        (`MENDARI_SMART_EHRS`) to the caller's own organization."""
        public = os.environ.get("MENDARI_PUBLIC_URL", "").strip().rstrip("/") or None
        if public is None or urlsplit(public).scheme not in ("http", "https"):
            return cds_hooks.CardLinks()
        found = (
            registration(ehrs, fhir_server)
            if smart_options is not None and isinstance(fhir_server, str)
            else None
        )
        org = caller.org if caller.org is not None else tenancy.DEFAULT_ORG
        return cds_hooks.CardLinks(public_url=public, launch=found is not None and found.org == org)

    @app.post(
        "/cds-services/{service_id}/feedback",
        tags=["FHIR and CDS Hooks"],
        summary="Feedback on a CDS Hooks service's cards",
    )
    def cds_feedback(service_id: str, body: cds_hooks.CardFeedback) -> dict[str, Any]:
        """CDS Hooks 2.0 feedback: what the clinician did with each card (1 to 50), accepted with
        the suggestion taken or overridden, with one of the reasons the card offered or none,
        and when. Each is chained on the card's decision as `card.feedback`, codes and ids only:
        `userComment` is never kept. All or nothing: a suggestion or reason the card did not
        offer is a `422`; a card that is not a decision of this service's agent in your
        organization, or, with sign-in on (and for a CDS client's token), not one you asked
        for, is one `404` for them all, and nothing is recorded. Not counted against the daily
        cap: it decides nothing."""
        pack = next(
            (
                p
                for p in served_here().values()
                if p.manifest.cds_hooks is not None and cds_hooks.service_id(p) == service_id
            ),
            None,
        )
        if pack is None:  # a pack with no hook makes no card
            raise PackNotFoundError(f"no CDS service {service_id!r}")
        items = [item.chained() for item in body.feedback]
        recorded = store.record_card_feedback(pack.id, service_id, items)
        return {"recorded": len(recorded)}

    @app.post(
        "/fhir/Questionnaire/$questionnaire-package",
        tags=["FHIR and CDS Hooks"],
        summary="Da Vinci DTR $questionnaire-package",
    )
    def questionnaire_package(payload: JsonBody) -> JSONResponse:
        """Da Vinci DTR: the documentation the payer asks for, for orders an agent decided (found by
        CRD's assertion id), pre-answered from the decision, and the questionnaires asked for,
        each version once (`standards/davinci.py`). A questionnaire not found here, or an order
        or a `context` that names no decision of yours, is a warning in `outcome`; orders or a
        `context` that name no decision, with nothing to return, are a `422` OperationOutcome
        (`not-found`)."""

        def lookup(decision_id: str) -> tuple[Pack, dict[str, str]] | None:
            record = store.get(decision_id)
            pack = packs.get(record["pack_id"]) if record is not None else None
            if record is None or pack is None:
                return None
            return pack, davinci.record_answers(record)

        authored = datetime.now(UTC).isoformat(timespec="seconds")
        result = davinci.questionnaire_package(payload, lookup, packs, authored)
        return JSONResponse(result, media_type="application/fhir+json")

    # -- FHIR: the $decide operation and conformance resources --------------------------------

    @app.post(
        "/fhir/$decide",
        tags=["FHIR and CDS Hooks"],
        summary="$decide: a decision as FHIR",
    )
    def fhir_decide(payload: JsonBody) -> JSONResponse:
        """Mendari's FHIR operation (system level, POST only): `Parameters` in (`pack`, `version`,
        `data` as a Bundle, `focus`), a `collection` Bundle out. Its first entry, a
        `GuidanceResponse`, is the decision; act on `outputParameters.outcome`, never on
        `status`. Then a RequestGroup and the Task (under BLOCK and ABSTAIN, the agent's fallback),
        the answers as Parameters, an OperationOutcome when there are findings, and the audit
        trail's Provenance. The definition: `/fhir/OperationDefinition/decide`. Errors are
        OperationOutcomes."""
        request = decide_op.parse(payload)
        pack = pack_or_404(request.pack)
        if request.version is not None and request.version != pack.version:
            raise PackNotFoundError(
                f"{pack.id} is served here at version {pack.version}, not {request.version}"
            )
        decision = decision_for(pack.id)
        focus = decide_op.focus_in(request.data, request.focus)
        result, task = decision.decide_fhir(request.data, focus=focus)
        record = store.get(result.id)
        if record is None:  # every decision this server makes is recorded
            raise RuntimeError(f"decision {result.id} is not in the audit trail")
        created = next(e for e in record["events"] if e["event"] == "decision.created")
        event = {**created, **{k: record[k] for k in ("pack_id", "pack_version", "record")}}
        provenance = audit_fhir.provenance({**event, "decision_id": result.id})
        bundle = decide_op.response(result, task, pack, provenance, request.data)
        return JSONResponse(bundle, media_type="application/fhir+json")

    @app.get(
        "/fhir/metadata",
        tags=["FHIR and CDS Hooks"],
        summary="CapabilityStatement",
    )
    def fhir_metadata(request: Request) -> Response:
        """What this deployment's FHIR routes do, built from the routes that are mounted. Mendari
        is a decision engine, not a FHIR data server: it serves no clinical resources. `version`
        changes with any change to the statement and `date` is when that version went live
        here; the ETag follows the content."""
        place = settings.deployment_place()
        statement = capability.capability_statement(
            mounted_routes(app),
            packs,
            sign_in=auth.enabled,
            backend_services=backend is not None,
            since=lambda version: store.capability_since(place[0], version),
            public_url=os.environ.get("MENDARI_PUBLIC_URL", "").strip() or None,
            place=place,
        )
        body = json.dumps(statement, separators=(",", ":"))
        etag = f'W/"{hashlib.sha256(body.encode()).hexdigest()[:16]}"'
        headers = {"etag": etag, "cache-control": "no-cache"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(body, media_type="application/fhir+json", headers=headers)

    @app.get(
        "/fhir/CodeSystem/{resource_id}",
        tags=["FHIR and CDS Hooks"],
        summary="Read a Mendari CodeSystem",
    )
    def fhir_code_system(resource_id: str) -> JSONResponse:
        """One of the CodeSystems Mendari's FHIR uses (decision outcomes, audit events, agents,
        work queues, each agent's question labels), generated from the code and the served
        agents. Its canonical `url` is on mendari.co; each deployment serves its own copy."""
        return conformance(terms.get("CodeSystem", resource_id))

    @app.get(
        "/fhir/NamingSystem/{resource_id}",
        tags=["FHIR and CDS Hooks"],
        summary="Read a Mendari NamingSystem",
    )
    def fhir_naming_system(resource_id: str) -> JSONResponse:
        """One of the identifier systems Mendari's FHIR uses (decision ids, input hashes, agent
        versions)."""
        return conformance(terms.get("NamingSystem", resource_id))

    @app.get(
        "/fhir/OperationDefinition/{resource_id}",
        tags=["FHIR and CDS Hooks"],
        summary="Read the $decide OperationDefinition",
    )
    def fhir_operation_definition(resource_id: str) -> JSONResponse:
        """The definition of `$decide` (id `decide`). Da Vinci's `$questionnaire-package` is
        defined by the DTR implementation guide, not here."""
        return conformance(decide_op.operation_definition() if resource_id == "decide" else None)

    def conformance(resource: Mapping[str, Any] | None) -> JSONResponse:
        if resource is None:
            raise HTTPException(404, "no such resource here")
        return JSONResponse(dict(resource), media_type="application/fhir+json")

    # -- FHIR: decisions and their audit trail, read and searched ------------------------------
    # Read-only and scoped to the caller's organization in SQL (another organization's resource
    # is a 404, as an unknown one is); no read writes to the audit trail, whatever the PHI mode.

    @app.get(
        "/fhir/Task/{resource_id}",
        tags=["FHIR and CDS Hooks"],
        summary="Read a decision as a Task",
    )
    def fhir_task(request: Request, resource_id: str) -> Response:
        """A Mendari decision as a FHIR Task (ADR-0003): the decision itself, not the work item it
        routes. `requested` while it waits for a reviewer in Mendari, `completed` once its answer
        is final, `failed` when the input was refused; `businessStatus` is the outcome. The
        latest version; the ETag is `W/"{versionId}"`. No conditional read: an `If-None-Match`
        always gets the resource."""
        versions = fhir_search.task(store, resource_id, base=fhir_base(request))
        if versions is None:
            raise HTTPException(404, "no such Task here")
        return served(versions[-1])

    @app.get(
        "/fhir/Task/{resource_id}/_history/{version_id}",
        tags=["FHIR and CDS Hooks"],
        summary="Read a version of a decision's Task",
    )
    def fhir_task_version(request: Request, resource_id: str, version_id: str) -> Response:
        """One version of a decision's Task (vread). Its versions are the events that changed
        the decision: 1 when it was made, then each review resolved and outcome recorded, in
        chain order; each version's Provenance is in `relevantHistory`."""
        versions = fhir_search.task(store, resource_id, base=fhir_base(request))
        number = int(version_id) if re.fullmatch(r"[1-9]\d{0,8}", version_id) else 0
        if versions is None or not 1 <= number <= len(versions):
            raise HTTPException(404, "no such Task version here")
        return served(versions[number - 1])

    @app.get(
        "/fhir/AuditEvent/{resource_id}",
        tags=["FHIR and CDS Hooks"],
        summary="Read an audit event",
    )
    def fhir_audit_event(request: Request, resource_id: str) -> Response:
        """One event of a decision's hash-chained audit trail (id `{decision id}-{chain
        position}`), with its hashes; `entity` refers to the decision's Task."""
        resource = fhir_search.audit_event(store, resource_id, base=fhir_base(request))
        if resource is None:
            raise HTTPException(404, "no such AuditEvent here")
        return served(resource)

    @app.get(
        "/fhir/Provenance/{resource_id}",
        tags=["FHIR and CDS Hooks"],
        summary="Read a decision's provenance",
    )
    def fhir_provenance(request: Request, resource_id: str) -> Response:
        """Who made a version of a decision's Task, and how (id `{decision id}-p{chain
        position}`): the engine created it, a reviewer verified or changed it, a downstream
        system recorded what really happened. `target` is the Task version it made."""
        resource = fhir_search.provenance(store, resource_id, base=fhir_base(request))
        if resource is None:
            raise HTTPException(404, "no such Provenance here")
        return served(resource)

    def search_page(request: Request, resource: str, items: list[tuple[str, str]]) -> Response:
        """One `searchset` page: the URL's parameters, then a POST body's (R4 http.html)."""
        search = parse_search(
            SPECS[resource], items, handling(", ".join(request.headers.getlist("prefer")) or None)
        )
        base = fhir_base(request)
        found = fhir_search.find(store, search, base=base, terminology=terms)
        bundle = searchset(
            base=base,
            search=search,
            resources=found.resources,
            total=found.total,
            next_key=found.next_key,
            timestamp=datetime.now(UTC).isoformat(timespec="milliseconds"),
        )
        return JSONResponse(bundle, media_type="application/fhir+json", headers=PRIVATE)

    def search_get(resource: str) -> Callable[[Request], Response]:
        def endpoint(request: Request) -> Response:
            return search_page(request, resource, request.query_params.multi_items())

        return endpoint

    def search_post(resource: str) -> Callable[[Request], Awaitable[Response]]:
        """Async only to read the form body; the search runs in the thread pool, as every sync
        route does, so the event loop never waits on the database."""

        async def endpoint(request: Request) -> Response:
            body = await read_body(request, MAX_SEARCH_FORM_BYTES)
            if body is None:
                kib = MAX_SEARCH_FORM_BYTES // 1024
                issue = {"code": "too-costly", "diagnostics": f"a search form is {kib} KiB at most"}
                return operation_outcome(413, [issue])
            form = form_items(body)
            if names_a_credential(name for name, _ in form):
                raise credential_in_url_or_form()
            items = [*request.query_params.multi_items(), *form]
            if any(name == "_format" and not _offered(value) for name, value in items):
                return _refuse(406, "only JSON is offered: _format must be json")
            return await run_in_threadpool(search_page, request, resource, items)

        return endpoint

    for resource, (what, order) in SEARCHES.items():
        app.add_api_route(
            f"/fhir/{resource}",
            search_get(resource),
            methods=["GET"],
            tags=["FHIR and CDS Hooks"],
            summary=f"Search {what}",
            description=_search_description(resource, what, order),
            openapi_extra={"parameters": _search_parameters(resource)},
        )
        app.add_api_route(
            f"/fhir/{resource}/_search",
            search_post(resource),
            methods=["POST"],
            tags=["FHIR and CDS Hooks"],
            summary=f"Search {what} (POST)",
            description=f"`GET /fhir/{resource}` with its parameters in a form body "
            "(`application/x-www-form-urlencoded`), so they stay out of URLs and logs. "
            "Parameters in the URL count too, before the body's. The form is 256 KiB at most "
            "(a `413` otherwise).",
            openapi_extra={"requestBody": _search_form(resource)},
        )

    # -- dashboard ---------------------------------------------------------------------------

    page = versioned_page("index.html")
    # The API reference: Swagger UI from vendored files, which needs no inline style.
    reference = versioned_page("docs/reference.html")
    reference_csp = content_security_policy(reference, inline_styles=False)

    # Innermost, so the route reads through it directly: Starlette's middleware would wrap the
    # 413 it raises mid-body in an exception group, which FastAPI answers with a 400.
    app.add_middleware(
        BodyCap, limit=body_limit, counted_within=(REGISTRY_PREFIX,) if registry_on else ()
    )

    @app.middleware("http")
    async def _fhir_media_types(request: Request, call_next: Any) -> Response:
        """FHIR routes speak FHIR R4 JSON only. Inside the guard, so sign-in answers first. The
        SMART configuration is plain JSON whatever the client accepts (SMART App Launch 2.2)."""
        if fhir_route(request.url.path) and request.url.path != DISCOVERY_PATH:
            refused = fhir_media_refusal(request)
            if refused is not None:
                return refused
        response: Response = await call_next(request)
        return response

    def notice_refusal(request: Request) -> Response | None:
        """A signed-in person who may decide or review takes data in only once they have
        acknowledged the current notice (`mendari.notices`; the dashboard asks when it opens).
        Keys, clients, EHR launches and open mode are never asked."""
        principal = principal_of(request)
        if acknowledgement == "off" or principal.kind != "user":
            return None
        route = (request.method, request.url.path)
        permission = auth.permission(*route)
        minting = request.method == "POST" and permission == notices.MINTING_PERMISSION
        if permission not in notices.GATED and not minting:
            return None
        if request.url.path == "/api/notice/acknowledge":  # what the refusal asks for
            return None
        key = notices.chain_id(principal.id)
        if key in acknowledged or store.notice_acknowledged(key):
            acknowledged.add(key)
            return None
        log_event(request, "notice.refused", outcome="refused", reason=f"version {notices.VERSION}")
        error = NoticeNotAcknowledged(
            "acknowledge the notice first (POST /api/notice/acknowledge): "
            + notices.ACKNOWLEDGEMENT
        )
        refused = error_response(request.url.path, 403, error)
        refused.headers[NOTICE_HEADER] = notices.VERSION  # not a sign-in challenge
        return refused

    def admit(request: Request) -> Response | None:
        """The sign-in check, then the rate limit and the principal's daily cap
        (server/limits.py), all before the route: a refused request reads no body, calls no
        provider and writes nothing to the chain."""
        refused = auth.check(request) or notice_refusal(request)
        if refused is not None or not (limiter.on or cap):
            return refused
        permission = auth.permission(request.method, request.url.path)
        principal = principal_of(request)
        over = limiter.check(request, permission, principal.id) if limiter.on else None
        decides = DailyCap.counts(request.method, request.url.path)
        if over is None and cap is not None and principal.kind != "open" and decides:
            over = cap.check(principal.id, principal.org)
        return None if over is None else limited_response(request.url.path, over)

    @app.middleware("http")
    async def _guard(request: Request, call_next: Any) -> Response:
        """Every request passes the sign-in guard (a no-op in open mode) and the rate limits.
        First, a request another site started reaches only a public route or a page, to read
        (`server/origin.py`). Then a credential in a URL is refused, in every mode: it would end
        up in logs and histories. No body is read here: the routes that read a form refuse a
        credential in it, after this check."""
        try:
            origins.check(request)
            if names_a_credential(request.query_params):
                raise credential_in_url_or_form()
            # A credential lookup and a limit are database work: in the thread pool, as sync
            # routes are, so a slow database never stalls the event loop.
            refused = await run_in_threadpool(admit, request)
        except AuthError as error:
            return error_response(request.url.path, error.status, error)
        if refused is not None:
            return refused
        principal = getattr(request.state, "principal", None)
        # Who asked, as the chain names them, rides along with the organization (open mode: none,
        # but for a CDS client's verified token, which is `default`'s).
        # A person of no organization (memberships mode) acts for nobody: any organization data
        # a route reaches through tenancy is refused, never read for all of them or `default`.
        who = principal if principal is not None and principal.kind != "open" else None
        acting = (
            tenancy.acting_for_nobody(who.actor)
            if who is not None and (who.org is None or tenancy.is_blank(who.org))
            else tenancy.acting_for(who.org if who else None, who.actor if who else None)
        )
        with acting:
            response: Response = await call_next(request)
        return response

    # Outside the guard, so a registered origin's preflight is answered before it and its
    # refusals carry CORS too; inside the security headers and the request log (LP-23c).
    app.add_middleware(Cors, origins=cds_origins)

    csp = content_security_policy(page)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next: Any) -> Response:
        """Outside the guard, so refusals from the guard get the headers too. Every response gets
        a CSP and COEP: a page sets its own policy (computed from its HTML), anything else gets
        the dashboard's. Any host but this machine also gets HSTS."""
        response: Response = await call_next(request)
        path = request.url.path
        if registry_headers is not None and path.startswith(REGISTRY_PREFIX):
            # What the engine answers for the mounted registry before the registry is reached
            # (the origin check, the body limit, a 500): the registry's own headers too, so
            # every answer under it carries the policy that loads nothing (LP-19d).
            for name, value in registry_headers.items():
                response.headers.setdefault(name, value)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if path.startswith("/api/"):  # data, often an organization's: never stored anywhere
            response.headers["Cache-Control"] = "no-store"
        elif path.startswith(("/auth/", "/smart/", "/cds-services/")) or path in (
            "/fhir/$decide",
            "/fhir/Questionnaire/$questionnaire-package",
        ):
            response.headers.setdefault("Cache-Control", "no-store")
        api = path.startswith(("/api/", "/fhir/")) or path == "/fhir"
        if api and response.status_code in (401, 403) and NOTICE_HEADER not in response.headers:
            response.headers.setdefault("WWW-Authenticate", challenge())  # RFC 7235 and 6750
        response.headers.setdefault("Content-Security-Policy", csp)
        response.headers.setdefault("Cross-Origin-Embedder-Policy", "require-corp")
        if not is_local_host(request.url.hostname):  # browsers ignore it over plain http
            response.headers.setdefault("Strict-Transport-Security", HSTS)
        return response

    # Outermost: every request gets its id first and its access line last, refusals included
    # (server/security_log.py). The routes are read per request: some are added below.
    app.add_middleware(
        security_log.RequestLog,
        routes=lambda: app.router.routes,
        ip_of=client_ip,
        open_mode=not auth.enabled,
        phi_mode=phi.mode,
        on_vercel=bool(os.environ.get("VERCEL")),
        also_logged=(REGISTRY_PREFIX,) if registry_on else (),
    )

    add_auth_routes(app, auth)
    if registry_on:
        # Imported here only: `mendari.hub.registry` imports this module.
        from mendari.hub.registry import REGISTRY_HEADERS, RegistryStore, create_registry_app
        from mendari.server.registry_routes import (
            add_registry_review_routes,
            add_registry_routes,
        )

        # One store, on the engine's database, for the mounted registry and the people's
        # routes. The mounted registry is public to the guard and checks its own publisher
        # tokens; it holds no shared secret (LP-19g).
        registry_store = RegistryStore(db_path, migrate_url=migrate_url)
        registry_app = create_registry_app(store=registry_store)
        # The engine's body limit, innermost in the registry too (as above, and for the same
        # reason): the registry's own routes read through it directly. A declared length over
        # the limit never gets this far.
        registry_app.user_middleware.append(Middleware(BodyCap, limit=body_limit))
        registry_headers = REGISTRY_HEADERS
        app.mount(REGISTRY_PREFIX.rstrip("/"), registry_app, name="registry")
        app.state.registry_store = registry_store  # closed by whoever closes the app (tests)
        if auth.enabled:  # the people's routes need someone signed in
            add_registry_routes(app, auth, registry_store)
            if review_org is not None:  # memberships mode, and the organization has a record
                add_registry_review_routes(app, auth, registry_store, review_org)

    if store_on:
        # An organization's pack store (LP-20f): its authors' routes. The hidden-set check
        # reads every organization's hidden sets with the index key, when it is set; a
        # namespace an organization holds by an approved registry claim is its too.
        from mendari.core.evalsets import EvalsetStore as _Evalsets
        from mendari.server.store_routes import add_store_routes

        def claimed_namespace(org: str, namespace: str) -> bool:
            if not registry_on:
                return False
            claims = app.state.registry_store.claims(org=org, namespace=namespace)
            return any(claim["status"] == "approved" for claim in claims)

        from mendari.evals.store_jobs import JobStore

        pack_store = PackStore(store)
        app.state.pack_store = pack_store

        def job_decision(pack: Pack, variants: Mapping[str, str]) -> Decision:
            """A store job's Decision: the deployment's choice for the pack, then the job's,
            each decision under the deadline and the evaluation quotas (LP-20g)."""
            return Decision(
                pack,
                providers={**variants_for(pack)[0], **variants},
                budget=None if budget is None else budget.for_evaluation(),
                deadline_s=deadline_s,
                origin_cap=False,  # an evaluation measures the pack's own policy
                connection_source=connection_for,  # the service that answers here (ADR-0021)
            )

        store_jobs = JobStore(pack_store, served=lambda: packs, decision_for=job_decision)

        def promoted_versions(org: str) -> dict[str, tuple[Pack, str, bool]]:
            """`org`'s promoted versions, each loaded on its own: one that no longer loads (an
            engine upgrade, a file gone) is left out and logged, never the request's failure."""
            found: dict[str, tuple[Pack, str, bool]] = {}
            for pack_id, (digest, promotion, automate) in pack_store.promoted(org).items():
                try:
                    found[pack_id] = (pack_store.load(org, digest), promotion, automate)
                except MendariError as error:
                    providers_logger.warning(
                        "promoted store version %s of %s is not served: %s",
                        digest,
                        org,
                        type(error).__name__,
                    )
            return found

        promoted_source.append(promoted_versions)
        add_store_routes(
            app,
            auth,
            pack_store,
            served=lambda: packs,
            hidden=None if evalsets is None else _Evalsets(store, evalsets),
            claimed=claimed_namespace,
            deadline_s=deadline_s,
            jobs=store_jobs,
            limits=lambda: org_limits,
            on_promotion=forget_promoted,
        )
    # Finding an agent in the deployment's registry and adding it to an organization's store
    # (`server/hub_routes.py`): `GET /api/hub` always; the rest where a registry is named.
    kept_store: PackStore | None = getattr(app.state, "pack_store", None)
    add_hub_routes(
        app,
        auth,
        registry=hub_registry,
        served=lambda: packs,
        served_here=served_here,
        store=kept_store,
        hidden=None if evalsets is None else EvalsetStore(store, evalsets),
    )

    def draft_decision(pack: Pack) -> Decision:
        """A Decision for a store version that is not served, to read where its `agent`
        variants' answers would come from and to check its connection: no store, no budget,
        its own policy."""
        return Decision(
            pack,
            providers=variants_for(pack)[0],
            deadline_s=deadline_s,
            origin_cap=False,
            connection_source=connection_for,
        )

    def agent_providers(org: str, pack_id: str) -> tuple[Pack, list[AgentProvider]] | None:
        """An agent `org` is served (from the deployment's files, or a version of its store it
        promoted) or holds in its store as a draft, with the `agent` variants it runs; None for
        any other, another organization's included. An agent that could not be built has none."""
        if pack_id in packs:
            decided = built.get(pack_id)
            return packs[pack_id], [] if decided is None else decided.agent_providers()
        served = promoted_here().get(pack_id)
        try:
            if served is not None:
                return served[0], store_decision(*served).agent_providers()
            if kept_store is not None:
                for row in kept_store.versions(org, pack_id):
                    if row["state"] in ("draft", "retired"):
                        pack = kept_store.load(org, row["digest"])
                        return pack, draft_decision(pack).agent_providers()
        except MendariError:
            return None  # a version that no longer loads or builds: not one to connect
        return None

    add_connection_routes(
        app,
        auth,
        connection_store,
        enabled=connections_on,
        agents=lambda org: sorted(served_here()),
        providers=agent_providers,
        changed=forget_connections,
    )
    if backend is not None:
        add_backend_routes(app, backend, store, limiter if limiter.on else None)
    add_smart_routes(
        app,
        settings=smart_options,
        store=store,
        packs=packs,
        pack_or_404=pack_or_404,
        decision_for=decision_for,
        default_pack=default_pack,
        auth=auth,
        lifetime=session_lifetime,
        site_codes=site_maps,
        declarations=declarations,
    )

    # The System routes (`server/system_routes.py`): the deployment's readiness as checks, and
    # its operator's actions. Mounted in every mode: `operate` says who reaches them.
    registry_probe = system.RegistryProbe(os.environ.get("MENDARI_REGISTRY_URL"))
    first_admin_used = [False]  # once the database is marked, it stays marked: read until then

    def system_deployment(request: Request, principal: Principal) -> system.Deployment:
        """What the checks read (`server/system.py`), for this request: signed-in health's own
        answer and readiness's, then what only an operator is shown (each job's last run, the
        last checkpoint, the last alerts, who administers the operator organization)."""
        named, administrators = None, None
        if auth.memberships and operator_org is not None:
            record = store.organization(operator_org)
            named = None if record is None else str(record["name"])
            administrators = sum(
                accounts.ADMIN in member["roles"] and member["user_id"] is not None
                for member in store.memberships(org=operator_org)
            )
        if first_admin is not None and not first_admin_used[0]:
            first_admin_used[0] = store.first_admin() is not None
        signed_day = None
        if checkpoint_signer is not None:
            statement = store.latest_statement()
            own = key_id(checkpoint_signer.public_key())
            if statement is not None and statement.get("key_id") == own:
                signed_day = str(statement.get("date"))
        runs = store.runs()
        sent = store.last_alerts()
        public = {"status": "ok", "version": __version__, "sign_in": auth.enabled}
        # What this organization is served: the deployment's agents and its own store's.
        served = served_here()
        return system.Deployment(
            health={**public, **health_details(request, principal, org_of(principal))},
            ready=readiness.answer().body,
            now=datetime.now(UTC),
            accounts="open"
            if not auth.enabled
            else "memberships"
            if auth.memberships
            else "claims",
            operator_org=operator_org,
            operator_org_name=named,
            administrators=administrators,
            first_admin=(
                "unset" if first_admin is None else "used" if first_admin_used[0] else "waiting"
            ),
            public_url=(sign_in.public_url if sign_in is not None else None)
            or os.environ.get("MENDARI_PUBLIC_URL", "").strip()
            or None,
            forwarded=bool(request.headers.get("x-forwarded-for")),
            scheduled=bool(cron_secret),
            runs=runs,
            checkpoint=store.last_checkpoint(),
            statement_day=signed_day,
            alerts=system.Alerts(
                webhook=alerts is not None,
                signed=alerts is not None and alerts.secret is not None,
                last=sent["last"],
                delivered=sent["delivered"],
            ),
            registry=registry_probe.state(),
            agent_store=kept_store is not None,
            served=len(served),
            served_from_registry=sum(pack.shown_origin == "hub" for pack in served.values()),
            any_decision=store.any_decision(),
            demo_only=set(served) == {DEMO_AGENT},
            recalls=[
                {"agent": pack.id, "version": pack.version, "reason": why}
                for pack in served.values()
                if (why := recall_watch.recalled(pack)) is not None
            ],
            ehr_checked=runs.get("ehr.check"),
        )

    add_system_routes(
        app,
        auth,
        store,
        deployment=system_deployment,
        alerts=alerts,
        ehrs=ehrs,
        chain_broken=chain_broken,
    )

    @app.get("/", include_in_schema=False)
    def dashboard() -> HTMLResponse:
        return HTMLResponse(page, headers={"cache-control": "no-cache"})

    @app.get("/docs/reference", include_in_schema=False)
    def api_reference() -> HTMLResponse:
        headers = {"cache-control": "no-cache", "content-security-policy": reference_csp}
        return HTMLResponse(reference, headers=headers)

    # The invitation page, memberships mode only (open and claims mode: 404): public and static,
    # never a redirect (auth.py `_RULES`). Its own policy allows its own inline scripts (the one
    # that takes the token out of the address before anything loads, the theme, the import map)
    # and no inline style; never stored, as it holds a token for a moment.
    if auth.memberships:
        invite_page = versioned_page("invite.html")
        invite_csp = content_security_policy(invite_page, inline_styles=False)

        @app.get("/invite", include_in_schema=False)
        def invitation_page() -> HTMLResponse:
            headers = {"cache-control": "no-store", "content-security-policy": invite_csp}
            return HTMLResponse(invite_page, headers=headers)

    def grading_decision(header: Mapping[str, Any], asked: GradingRequest) -> Decision:
        """The Decision a grading run over a hidden set with `header` runs (LP-11l): the pack the
        set was made for, as served here (`asked.pack` names it, and it still asks the set's
        questions with their labels and critical labels), each slot on the variant `asked`
        names, else the deployment's choice, else the pack's default, every one of them local
        (conflict 52, owner decision 4 open); with no store (nothing is recorded), no budget (no
        remote call), no governance override and the pack's own policy, as an evaluation
        measures it. Refused before any case is read (`grading_refused`): `wrong_pack`,
        `pack_unavailable` (not served here, it does not build, or what it names does not fit
        `mendari.hidden_result/1`, so every run's answer would fail: `holds`), `unknown_variant`
        and `remote_provider`. Its own Decision, whatever the deployment's for the pack built."""
        if asked.pack != header.get("pack"):
            raise grading_refused("wrong_pack")
        pack = packs.get(asked.pack)
        if pack is None:
            raise grading_refused("pack_unavailable")
        if pack_problem(pack, header) is not None:
            raise grading_refused("wrong_pack")
        slots = pack.manifest.providers
        named = dict(asked.variants or {})
        if any(slot not in slots or named[slot] not in slots[slot].variants for slot in named):
            raise grading_refused("unknown_variant")
        chosen = {**variants_for(pack)[0], **named}
        running = {slot: chosen.get(slot, spec.use) for slot, spec in slots.items()}
        if not all(_local_variant(pack, slot, variant) for slot, variant in running.items()):
            raise grading_refused("remote_provider")
        try:
            decision = Decision(pack, providers=running, deadline_s=deadline_s, origin_cap=False)
        except Exception:  # the pack's content: it does not run here, whatever it says
            raise grading_refused("pack_unavailable") from None
        if decision.is_remote:  # what the variants say, checked again on what was built
            raise grading_refused("remote_provider")
        if not holds(decision):
            raise grading_refused("pack_unavailable")
        return decision

    # A labeller's routes over hidden evaluation sets (LP-11j), and its organization's curators'
    # (LP-11k): memberships mode (so sign-in, which the keys need anyway) with both evaluation-set
    # keys, as the invitation page is memberships mode's; the hosted site (memberships mode since
    # 2026-11-07, no keys yet) mounts none of them. The grading route (LP-11l) is mounted with the
    # keys in both account modes: only the organization's grading key reaches it.
    if evalsets is not None:
        hidden = EvalsetStore(store, evalsets)
        if auth.memberships:
            add_labelling_routes(app, auth, hidden)
            add_evalset_routes(app, auth, hidden)
        add_grading_routes(app, auth, hidden, grading_decision)

    # Written output (ADR-0025): a grader's routes and the rounds' reports. Memberships mode
    # (graders are members holding `labeller`) under PHI mode `encrypt` (a round's texts are
    # sealed under the PHI key; `hash` keeps none, and sign-in never runs `off`).
    if auth.memberships and phi.mode == "encrypt":
        add_written_routes(app, auth, WrittenStore(store))

    # The status page (LP-21b): public and static, the same for everyone; it reads readiness
    # (`/api/health/ready`) in the browser, so serving it does no database work. Its own policy
    # allows its inline scripts (the theme, the import map) and no inline style.
    status_page = versioned_page("status.html")
    status_csp = content_security_policy(status_page, inline_styles=False)

    @app.get("/status", include_in_schema=False)
    def status_view() -> HTMLResponse:
        headers = {"cache-control": "no-cache", "content-security-policy": status_csp}
        return HTMLResponse(status_page, headers=headers)

    # Bookmarks and links to FastAPI's ReDoc page, which is gone.
    @app.get("/redoc", include_in_schema=False)
    def to_api_reference() -> RedirectResponse:
        return RedirectResponse("/docs/reference", status_code=308)

    # The developer landing: public and static (server/landing.py). It lists every route, so it
    # is built once all are mounted, from settings only.
    @app.get("/docs", include_in_schema=False)
    def developer_landing() -> HTMLResponse:
        headers = {"cache-control": "no-cache", "content-security-policy": landing_csp}
        return HTMLResponse(landing_page, headers=headers)

    # Browsers ask for /favicon.ico on any page without an icon link (e.g. raw JSON responses).
    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(
            DASHBOARD_DIR / "favicon.ico",
            media_type="image/x-icon",
            headers={"cache-control": "public, max-age=86400"},
        )

    landing_page = landing.page(
        versioned_page("docs.html"),
        version=__version__,
        sign_in=auth.enabled,
        smart_launch=smart_options is not None,
        token_url=None if backend is None else backend.token_url,
        routes=mounted_routes(app),
        permission_for=auth.permission,
        cards=None
        if auth.enabled
        else {
            pack_id: landing.CardLink(p.manifest.name, p.community) for pack_id, p in packs.items()
        },
    )
    # No style attribute anywhere, so no inline style; its inline scripts (the theme script and
    # the import map) by hash.
    landing_csp = content_security_policy(landing_page, inline_styles=False)

    app.mount("/static", _Static(directory=DASHBOARD_DIR), name="static")
    return app


def add_labelling_routes(app: FastAPI, auth: Auth, hidden: EvalsetStore) -> None:
    """A labeller's routes over hidden evaluation sets (LP-11j, ADR-0011): their own counts,
    their next case and their label. Mounted only in memberships mode with both evaluation-set
    keys (`create_app`). The guard lets through only a person whose membership holds the
    `labeller` role (`label`: never a key, a token, an EHR launch, an administrator or open
    mode), and each route acts for that person and the organization they act for, never one a
    body, a query or a path names; the store refuses anyone who also manages the organization's
    sets. Not gated by the notice (conflict 20). The security log names set ids only (conflict
    50); the access line names the route's template, never an assignment's id."""

    def labeller(request: Request) -> tuple[str, str]:
        """The person the guard let through (their user id), and the organization they act
        for."""
        principal = auth.acting(request)
        if principal.kind != "user":  # the guard lets people only through: fail closed
            raise AuthError("only a person who signed in may do this", 403)
        return principal.id, org_of(principal)

    @app.get("/api/labelling", tags=["Labelling"], summary="Your labelling, set by set")
    def labelling(request: Request) -> LabellingCounts:
        """Your cases in each hidden evaluation set you label for this organization: how many
        wait (`open`) and how many you labelled, by the set's id; and how many wait in all
        (`remaining`). Counts only: no case is named or opened, and nothing is recorded."""
        user, org = labeller(request)
        return LabellingCounts.model_validate(hidden.labelling(user, org=org))

    @app.get(
        "/api/labelling/next",
        tags=["Labelling"],
        summary="Your next case to label",
        response_model=LabellingCase,
        responses={204: {"description": "Nothing waits for you"}},
    )
    def next_case(request: Request) -> Response | LabellingCase:
        """Your oldest open case, in a set still being labelled: its input, and the set's
        questions with their labels; nothing else (no attribute of the case, no intended label,
        no one's label, no model's output). Each time it is shown, the audit chain records that
        you read it (`evalset.case_read`) before it is sent. `204` when nothing waits for you.
        Answer it with `POST /api/labelling/{assignment}`."""
        user, org = labeller(request)
        shown = hidden.next_case(user, org=org)
        if shown is None:
            return Response(status_code=204)
        set_id = hidden.assignment_set(str(shown["assignment"]), user, org=org)
        log_event(request, "evalset.case.read", ids=[set_id] if set_id else None)
        return LabellingCase.model_validate(shown)

    @app.post("/api/labelling/{assignment_id}", tags=["Labelling"], summary="Label your case")
    def label(request: Request, assignment_id: str, body: LabelRequest) -> LabelRecorded:
        """Your answer to the case `GET /api/labelling/next` gave you (its `assignment`): every
        question of the set's, each with one of its labels. One answer per case, kept sealed and
        never changed; the audit chain records a salted commitment to it (`evalset.labelled`),
        never the label. The answer is how many of your cases still wait, never how yours
        compares with anyone's. An assignment that is unknown, someone else's, another
        organization's or no longer open (answered, withdrawn, or in a set no longer being
        labelled) is one answer: `404`. A label that does not answer each question with one of
        its labels is a `422` naming the question, never the value."""
        user, org = labeller(request)
        try:
            recorded = hidden.record_label(assignment_id, user, body.labels, org=org)
        except NoAssignment:
            log_event(request, "evalset.labelled", outcome="refused", reason="no_assignment")
            raise
        set_id = hidden.assignment_set(assignment_id, user, org=org)
        log_event(request, "evalset.labelled", ids=[set_id] if set_id else None)
        return LabelRecorded.model_validate(recorded)


def add_written_routes(app: FastAPI, auth: Auth, rounds: WrittenStore) -> None:
    """A grader's routes over written output, and the rounds' reports (ADR-0025). Mounted only
    in memberships mode under PHI mode `encrypt` (`create_app`). The guard lets through to a
    grader's routes only a person whose membership holds the `labeller` role (`label`: never a
    key, a token, an EHR launch, an administrator or open mode), and each route acts for that
    person and the organization they act for, never one a body, a query or a path names; the
    store refuses anyone who also manages the organization's sets. Not gated by the notice, as
    a labeller's are not. The security log names round ids only; the access line names the
    route's template, never an assignment's id."""

    def grader(request: Request) -> tuple[str, str]:
        """The person the guard let through (their user id), and the organization they act
        for."""
        principal = auth.acting(request)
        if principal.kind != "user":  # the guard lets people only through: fail closed
            raise AuthError("only a person who signed in may do this", 403)
        return principal.id, org_of(principal)

    @app.get(
        "/api/written/labelling", tags=["Written output"], summary="Your grading, round by round"
    )
    def written_labelling(request: Request) -> LabellingCounts:
        """Your items in each grading round of written output for this organization: how many
        wait (`open`) and how many you graded (`labelled`), by the round's id (`set`); and how
        many wait in all (`remaining`). Counts only: no text is opened, and nothing is
        recorded."""
        user, org = grader(request)
        return LabellingCounts.model_validate(rounds.labelling(user, org=org))

    @app.get(
        "/api/written/labelling/next",
        tags=["Written output"],
        summary="Your next text to grade",
        response_model=WrittenCase,
        responses={204: {"description": "Nothing waits for you"}},
    )
    def written_next(request: Request) -> Response | WrittenCase:
        """Your oldest open item, in a round still being graded: the text an agent wrote
        (`written`), what it was given (`input`: the fields the rubric names, nothing else of
        the case), and the rubric: each criterion's levels (`questions`) and its own words
        (`guidance`). Nothing else: no judge's grade, no one else's grade, none of the agent's
        answers. Each time it is shown, the audit chain records that you read it
        (`written.item_read`) before it is sent. `204` when nothing waits for you. Answer it
        with `POST /api/written/labelling/{assignment}`."""
        user, org = grader(request)
        shown = rounds.next_item(user, org=org)
        if shown is None:
            return Response(status_code=204)
        round_id = rounds.assignment_round(str(shown["assignment"]), user, org=org)
        log_event(request, "written.item.read", ids=[round_id] if round_id else None)
        return WrittenCase.model_validate(shown)

    @app.post(
        "/api/written/labelling/{assignment_id}",
        tags=["Written output"],
        summary="Grade your text",
    )
    def written_grade(request: Request, assignment_id: str, body: LabelRequest) -> LabelRecorded:
        """Your grades of the text `GET /api/written/labelling/next` gave you (its
        `assignment`), as `labels`: every criterion of the rubric, each with one of its levels.
        One answer per item, never changed; the audit chain records a salted commitment to it
        (`written.graded`), never a grade. The answer is how many of your items still wait,
        never how yours compares with anyone's or with a judge's. An assignment that is unknown,
        someone else's, another organization's or no longer open (graded, withdrawn, or in a
        round that closed) is one answer: `404`. Grades that do not answer each criterion with
        one of its levels are a `422` naming the criterion, never the value."""
        user, org = grader(request)
        try:
            recorded = rounds.record_grades(assignment_id, user, body.labels, org=org)
        except NoAssignment:
            log_event(request, "written.graded", outcome="refused", reason="no_assignment")
            raise
        round_id = rounds.assignment_round(assignment_id, user, org=org)
        log_event(request, "written.graded", ids=[round_id] if round_id else None)
        return LabelRecorded.model_validate(recorded)

    @app.get(
        "/api/written/rounds",
        tags=["Written output"],
        summary="Your organization's grading rounds, and each closed one's report",
    )
    def written_rounds(
        request: Request,
        pack_id: Annotated[
            str | None, Query(max_length=200, title="Agent id", description="only this agent's")
        ] = None,
    ) -> list[WrittenRound]:
        """The grading rounds of written output of the organization you act for, oldest
        first: each one's agent, rubric and state, how many items it holds, how many grades
        were given and how many assignments still wait; and, once it is closed, its report
        (`mendari.written/1`): per criterion, the judge's agreement with clinicians beside
        clinician with clinician, the state (validated, not validated, insufficient evidence),
        and the judge's grades for ungraded outputs only where validated. While a round is
        open there is no report: nothing compares anyone before it closes. A stored report is
        served only while its hash is the one the round's closing recorded in the audit trail;
        otherwise `report` is null and `note` says so. Counts, coefficients, hashes and
        graders' user ids: never a text or a single grade."""
        org = org_of(auth.acting(request))
        return [
            WrittenRound.model_validate(
                {key: value for key, value in found.items() if key not in ("org", "created_by")}
            )
            for found in rounds.rounds(org, pack_id)
        ]


def add_evalset_routes(app: FastAPI, auth: Auth, hidden: EvalsetStore) -> None:
    """An organization's curators' routes over its hidden evaluation sets (LP-11k, ADR-0011):
    its sets listed, one read, and its cases drawn, assigned and adjudicated. Mounted only in
    memberships mode with both evaluation-set keys (`create_app`). The guard lets through only a
    person holding `curate` (an administrator of the organization they act for: never a key, a
    token, an EHR launch, a labeller or open mode), and each route acts for that organization,
    never one a body, a query or a path names: another organization's set answers as an unknown
    one does (`NoEvalset`, one `404`). Answers are ids, counts, user ids, the commitment and the
    stored aggregates (without each rater pair's label counts): never an input, a label given,
    an attribute or a case id. No route computes statistics, freezes or retires (conflicts 43
    and 49): those are the key holder's commands. Not gated by the notice: they take no data in
    and mint no credential (`notices.GATED`, `MINTING_PERMISSION`). Like an administrator's
    other writes, they have no rate-limit class. The security log names set ids only (conflict
    50); the access line names the route's template."""

    def curators_org(request: Request) -> str:
        """The organization the person the guard let through acts for."""
        principal = auth.acting(request)
        if principal.kind != "user":  # the guard lets people only through: fail closed
            raise AuthError("only a person who signed in may do this", 403)
        return org_of(principal)

    def logged_write(
        request: Request,
        event: str,
        set_id: str,
        write: Callable[[], dict[str, Any]],
        counted: str,
    ) -> dict[str, Any]:
        """`write`, logged as `event` with the set's id and how many it made (its `counted`);
        a set that is unknown or another organization's logged as refused (`no_set`), with no
        id."""
        try:
            done = write()
        except NoEvalset:
            log_event(request, event, outcome="refused", reason="no_set")
            raise
        log_event(request, event, ids=[set_id], count=int(done[counted]))
        return done

    @app.get("/api/evalsets", tags=["Evaluation sets"], summary="Your organization's hidden sets")
    def list_evalsets(request: Request) -> list[EvalsetListed]:
        """The hidden evaluation sets of the organization you act for, oldest first: each one's
        id, name, agent, state, when it was made and how many cases it holds. Nothing is
        recorded."""
        org = curators_org(request)
        return [EvalsetListed.model_validate(item) for item in hidden.named_sets(org=org)]

    @app.get(
        "/api/evalsets/{set_id}",
        tags=["Evaluation sets"],
        summary="One hidden set: its state, labelling and stored aggregates",
        response_model=EvalsetDetail,
        response_model_exclude_unset=True,
    )
    def evalset_detail(request: Request, set_id: str) -> EvalsetDetail:
        """One of your organization's sets: its name, agent and questions with their labels; its
        state, commitment (once frozen) and grading runs used and left; its cases counted by
        status and each labeller's assignments by user id; and its coverage and rater agreement
        as last stored (by the key holder's `mendari evalset agreement` or `coverage`, or at
        freeze), with when they were measured, or null before. Counts and coefficients by label,
        strata and languages (each rater pair's kappa without the pair's label counts): never a
        case, an input, a label given or an attribute. Nothing is computed or recorded. An
        unknown set and another organization's are one `404`."""
        org = curators_org(request)
        return EvalsetDetail.model_validate(hidden.summary(set_id, org=org))

    @app.post(
        "/api/evalsets/{set_id}/sample",
        tags=["Evaluation sets"],
        summary="Draw cases of a draft set to label",
    )
    def sample_evalset(request: Request, set_id: str, body: SampleRequest) -> SetSampled:
        """Draws more cases of a draft set to label, seeded, within each stratum: `per_stratum`
        from each (all that are left of a smaller one) or `cases` in all, shared out by the
        strata's sizes. Each drawn case's weight is sealed with it, and the audit chain records
        how many were drawn, the seed and a salted commitment to each drawn case's weight,
        naming every case drawn so far by its keyed hash (`evalset.sampled`), never a stratum or
        a weight. The answer: how many were drawn now and in all, and the seed. An unknown set
        and another organization's are one `404`; a set no longer a draft changes no more."""
        org = curators_org(request)
        drawn = logged_write(
            request,
            "evalset.sampled",
            set_id,
            lambda: hidden.sample(
                set_id, per_stratum=body.per_stratum, cases=body.cases, seed=body.seed, org=org
            ),
            "drawn",
        )
        return SetSampled.model_validate(drawn)

    @app.post(
        "/api/evalsets/{set_id}/assignments",
        tags=["Evaluation sets"],
        summary="Give each drawn case two blind labellers",
    )
    def assign_evalset(request: Request, set_id: str, body: LabellersRequest) -> SetAssigned:
        """Gives each drawn case of a draft set two different labellers from those named (their
        user ids), balanced across labellers and pairs, deterministically for a seed; an open
        assignment whose membership no longer holds is withdrawn and its case given to another.
        Each must hold the labeller role in this organization, and no one who manages its sets
        (an administrator, you included) is ever assigned. Cases are chosen by status, so a set
        whose rows are not those its audit chain recorded (a label, a status or a weight changed
        behind the store's back) is refused (a `400` naming the set and no case) and nothing is
        assigned. The audit chain records each assignment (`evalset.assigned`). The answer: how
        many were made and withdrawn. An unknown set and another organization's are one
        `404`."""
        org = curators_org(request)
        made = logged_write(
            request,
            "evalset.assigned",
            set_id,
            lambda: hidden.assign(set_id, body.labellers, seed=body.seed, org=org),
            "assigned",
        )
        return SetAssigned.model_validate(_counted(made))

    @app.post(
        "/api/evalsets/{set_id}/adjudication",
        tags=["Evaluation sets"],
        summary="Give each disputed case a third, blind labeller",
    )
    def adjudicate_evalset(request: Request, set_id: str, body: LabellersRequest) -> SetAssigned:
        """Gives each disputed case of a draft set (its two first labels differ) a third labeller
        from those named who is neither of its first two, balanced, with the same refusals as
        assigning; the third labels from the input alone, and the majority on each question
        settles the case. The audit chain records each assignment (`evalset.assigned`). The
        answer: how many were made and withdrawn. An unknown set and another organization's are
        one `404`."""
        org = curators_org(request)
        made = logged_write(
            request,
            "evalset.adjudication.assigned",
            set_id,
            lambda: hidden.adjudicate(set_id, body.labellers, seed=body.seed, org=org),
            "assigned",
        )
        return SetAssigned.model_validate(_counted(made))


def add_grading_routes(
    app: FastAPI,
    auth: Auth,
    hidden: EvalsetStore,
    grading: Callable[[Mapping[str, Any], GradingRequest], Decision],
) -> None:
    """The grading route over hidden evaluation sets (LP-11l, ADR-0011): one run of a pack over
    one of the organization's frozen or active sets, answered with aggregates only
    (`mendari.hidden_result/1`, `evals/hidden.py`). Mounted with both evaluation-set keys (so
    with sign-in) in both account modes (`create_app`). The guard lets through only a grading
    key (`grade`: never a person, an administrator, another key, a token, an EHR launch or open
    mode), and the route acts for its organization: another organization's set answers as an
    unknown one does (`no_set`, one `404`). Before any case is read the set's header is checked
    against the pack asked for and every provider slot is checked to be local (`grading`); then
    the store's grading read (`EvalsetStore.grade_read`) checks the set and its rotation rule,
    recomputes its content against its commitment, chains the read (`evalset.graded`, and
    `evalset.activated` on a frozen set's first) and hands the cases, in memory, to the run,
    whose answer is the closed result. A request that declares its arms (`arms`, with
    `declared`) is the hidden-set scoreboard (LP-17k): each declared configuration is checked as
    `variants` is, and to be in the arm it is declared under (`declared_arms`), before any case
    is read; then one grading read hands the cases to every arm, decided in turn on the same
    records, and the answer is `mendari.hidden_scoreboard/1`. Nothing is stored: no decision,
    report, ledger or `eval_reports` row. A refusal or a failure is answered with its fixed
    reason and sentence (`GRADING_REFUSALS`), never a store's or a run's message; a set that no
    longer holds what it was frozen with is a `409` `set_changed`, and the operator's log names
    the set only. Not
    gated by the notice (a key is never asked); its rate-limit class is `evaluate`. The security
    log names set ids only (conflict 50); the access line names the route's template."""

    def grader_of(request: Request) -> Principal:
        """The grading key the guard let through."""
        principal = auth.acting(request)
        if not principal.is_grading_key:  # the guard lets the grading key only through
            raise AuthError("only the deployment's grading key may do this", 403)
        return principal

    def declared_arms(header: Mapping[str, Any], body: GradingRequest) -> list[Declared]:
        """Each arm `body.arms` declares, as sent (`scoreboard_graded` decides them in the
        scoreboard's order), with the configuration it runs and its Decision: each
        configuration resolved and refused as `variants` is (`grading`: `wrong_pack`,
        `pack_unavailable`, `unknown_variant`, `remote_provider`, so a declared arm that calls a
        remote provider is refused here), then `wrong_arm` when the configuration it runs is not
        the one sent, slot for slot (an arm that leaves a slot out would run the deployment's
        choice for it, so one cited declaration would grade other configurations on other
        deployments), or not of the arm it is declared under, and `pack_unavailable` when no
        grade of the pack could be answered (`scoreboard_holds`: it has no critical label to
        attest). Before any case is read."""
        assert body.arms is not None
        found = []
        for arm, variants in body.arms.items():
            asked = body.model_copy(update={"variants": variants, "arms": None, "declared": None})
            decision = grading(header, asked)
            configuration = configuration_of(decision)
            sent = (arm, variants)  # the whole configuration, as declared
            if configuration is None or (configuration.arm, configuration.providers) != sent:
                raise grading_refused("wrong_arm")
            found.append(Declared(arm, configuration, decision))
        if not scoreboard_holds(found[0].decision.pack):
            raise grading_refused("pack_unavailable")
        return found

    @app.post(
        "/api/evalsets/{set_id}/runs",
        tags=["Grading"],
        summary="Grade an agent against a hidden set: aggregates only",
        response_model=HiddenResult | HiddenScoreboard,
        responses={
            404: {"description": "No set of your organization has that id"},
            409: {"description": "The set is not graded now (`reason` says why)"},
            422: {"description": "The agent or a variant asked for is not the set's to run"},
        },
    )
    def grade_evalset(
        request: Request, set_id: str, body: GradingRequest
    ) -> HiddenResult | HiddenScoreboard:
        """One grading run of the agent a hidden evaluation set of your organization was made
        for, over the set's cases, with local providers only, answered with aggregates only:
        each question's metrics, the outcome counts and rates, each critical label's counts, the
        threshold sweep and error counts, with the set's id and commitment, how many cases were
        graded, this run's number and the runs left (`mendari.hidden_result/1`). Never a case,
        its id or hash, a dataset's name or hash, a missed or misclassified case or a subgroup.
        The set is frozen or active (a frozen set becomes active on its first run), its rotation
        rule allows another run, and its cases still give the commitment chained when it froze;
        each run is chained (`evalset.graded`) before the cases are read, and counts even if it
        fails. `pack` is the set's agent id, and `variants` may choose a local variant for any of
        its slots. With `arms` and `declared` instead, the run is the hidden-set scoreboard
        (`mendari.hidden_scoreboard/1`, LP-17k): each arm declared with its configuration, every
        one local, decided in turn on the same cases in one run; each arm's figures (every
        proportion as k of n with its interval, every mean with its bootstrap interval), its
        attestations (met, not met or insufficient evidence, draft while the grading method is
        not in effect) and the ranking (off with one arm graded), with the scope and the
        declaration's SHA-256; never cost, latency, a subgroup or a case. Nothing is stored. A
        refusal names its `reason` only: `no_set` (`404`: an unknown set and another
        organization's are one answer), `wrong_pack`, `unknown_variant`, `remote_provider` and
        `wrong_arm` (`422`), `pack_unavailable`, `not_gradable`, `no_runs_left`, `expired` and
        `set_changed` (`409`), `set_unavailable` (`503`) and `run_failed` (`500`)."""
        principal = grader_of(request)
        told: list[GradingRead] = []
        declared: list[Declared] = []
        measured_on = clock.today()  # the day a scoreboard's answer is dated (US Eastern)

        def run(dataset: Dataset) -> HiddenResult | HiddenScoreboard:
            try:
                if declared:
                    assert body.declared is not None
                    return scoreboard_graded(
                        declared,
                        dataset,
                        told[0],
                        declaration=body.declared,
                        measured_on=measured_on,
                    )
                return graded(decision, dataset, told[0])
            except Exception as error:  # after the read: it counts, and says nothing of a case
                grading_logger.warning(
                    "a grading run of evaluation set %s failed (%s): it counts as a run",
                    set_id,
                    type(error).__name__,
                )
                raise grading_refused("run_failed") from None

        try:
            header = hidden.grading_header(set_id, org=org_of(principal))
            if body.arms is None:
                decision = grading(header, body)
            else:
                declared += declared_arms(header, body)
                decision = declared[0].decision
            answer = hidden.grade_read(
                set_id, principal.id, decision.pack.digest, run, receipt=told.append
            )
        except NoEvalset:
            log_event(request, "evalset.graded", outcome="refused", reason="no_set")
            raise grading_refused("no_set") from None
        except GradingRefused as refused:
            reason = refused.reason if refused.reason in GRADING_REFUSALS else "set_unavailable"
        except MendariError:  # the store's keys or database not ready for this set: say so only
            reason = "set_unavailable"
        else:
            log_event(request, "evalset.graded", ids=[set_id], count=answer.n)
            return answer
        if reason == "no_set":
            log_event(request, "evalset.graded", outcome="refused", reason=reason)
        else:
            log_event(request, "evalset.graded", ids=[set_id], outcome="refused", reason=reason)
        if reason == "set_changed":
            grading_logger.warning(
                "evaluation set %s does not hold what it was frozen with: run `mendari audit "
                "verify --evalsets`",
                set_id,
            )
        elif reason == "set_unavailable":
            grading_logger.warning(
                "evaluation set %s cannot be graded here: run `mendari evalset rotate --check`",
                set_id,
            )
        raise grading_refused(reason) from None


def _counted(made: Mapping[str, Any]) -> dict[str, Any]:
    """What an assignment run answers a curator: counts, never the assignments it made (each a
    case's hash, its id and its labeller, which the chain keeps)."""
    return {key: made[key] for key in ("set", "round", "assigned", "withdrawn")}


def mounted_routes(app: FastAPI) -> list[tuple[str, str]]:
    """(method, path) of every route the app serves."""
    return [
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
    ]


def identified(body: dict[str, Any]) -> dict[str, Any]:
    """A JSON error body with the request's id (`X-Request-Id`), for a person to quote."""
    rid = security_log.request_id()
    return body if rid is None else {**body, "request_id": rid}


def error_response(path: str, status: int, error: MendariError) -> JSONResponse:
    """`{"error", "detail"}` on Mendari's own API (and an invitation refusal's `reason`); an
    OperationOutcome on FHIR routes. A
    sign-in refusal carries its `WWW-Authenticate` challenge, a `429` its `Retry-After`."""
    asked = error.challenge if isinstance(error, AuthError) else None
    headers = {"WWW-Authenticate": asked} if asked else {}
    headers.update(retry_headers(error))
    if not fhir_route(path):
        said: dict[str, Any] = {"error": type(error).__name__, "detail": str(error)}
        if isinstance(error, InvitationRefused | GradingRefused):  # which refusal: a fixed code
            said["reason"] = error.reason
        return JSONResponse(identified(said), status, headers=headers)
    if isinstance(error, FhirStructureError):
        return operation_outcome(status, [{"code": "structure", **i} for i in error.issues])
    code = _ISSUE_TYPE.get(status, "processing")
    if isinstance(error, PackNotFoundError | FhirSourceNotFound):
        code = "not-found"
    return operation_outcome(status, [{"code": code, "diagnostics": str(error)}], headers)


quota_logger = logging.getLogger("mendari.limits")
phi_logger = logging.getLogger("mendari.phi")
monitor_logger = logging.getLogger("mendari.monitor")
providers_logger = logging.getLogger("mendari.providers")
grading_logger = logging.getLogger("mendari.evalsets")  # the operator's: a set's id only


def log_event(request: Request, name: str, **fields: Any) -> None:
    """A security log line for something the request's principal did (server/security_log.py)."""
    principal = principal_of(request)
    security_log.event(name, actor=principal.id, org=principal.org, **fields)


def retry_headers(error: MendariError) -> dict[str, str]:
    """`Retry-After` (and `no-store`, so no cache keeps the refusal) for a rate limit, and for a
    daily budget or quota, which start over at local midnight."""
    if isinstance(error, RateLimited):
        seconds = error.retry_after
    elif isinstance(error, ProviderError) and error.code in ("budget_exhausted", "quota_exhausted"):
        seconds = clock.seconds_until_tomorrow()
    else:
        return {}
    return {"Retry-After": str(seconds), "Cache-Control": "no-store"}


def limited_response(path: str, error: RateLimited) -> Response:
    """A `429` in the shape the route speaks: OAuth's at the token endpoint, a page for a
    browser signing in or launched from an EHR, an OperationOutcome on FHIR routes, else JSON.
    Always with `Retry-After` and `no-store`."""
    if path == TOKEN_PATH:
        return oauth_error(throttled(error.retry_after))
    if path in ("/auth/login", "/smart/launch"):
        page = error_page(
            f"Too many sign-ins from your network: try again in {error.retry_after} s.", 429
        )
        page.headers.update(retry_headers(error))
        return page
    return error_response(path, 429, error)


# What a memberships-mode server answers for the whole chain's verification (ADR-0008).
OPERATORS_CHAIN = (
    "the whole audit chain is the operator's: `mendari audit verify` on the server's database; "
    "your organization's own events are at /api/audit/administrative"
)
_HASH = re.compile(r"[0-9a-f]{64}")
# The furthest a list's `offset` reaches: past any trail, and within a 64-bit integer, which a
# larger value overflowed in the database driver (a 500).
MAX_OFFSET = 2**31


def administrative_cursor(text: str | None) -> tuple[str, str] | None:
    """An `after` of the administrative list (`{subject, hash}`, as `encode_cursor` wrote it),
    or a `400`, the same for anything else, as the store answers one not of the list."""
    if text is None:
        return None
    key = None
    if len(text) <= 1000:  # one of this list's is a few hundred characters
        try:
            key = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
        except (ValueError, RecursionError):  # binascii and Unicode errors are ValueErrors
            key = None
    if not (
        isinstance(key, dict)
        and set(key) == {"subject", "hash"}
        and isinstance(key["subject"], str)
        and 0 < len(key["subject"]) <= 200
        and isinstance(key["hash"], str)
        and _HASH.fullmatch(key["hash"])
    ):
        raise HTTPException(400, "not a cursor of this list")
    return key["subject"], key["hash"]


def administrative_kinds(asked: list[str] | None, org: str | None) -> list[str]:
    """The kinds asked for (`kind`), each one the list holds: an organization's
    (`ADMINISTRATIVE_KINDS`), or, in open mode, every kind on no decision; else a `400`."""
    known = set(ADMINISTRATIVE_KINDS) if org is not None else set(OFF_DECISION_KINDS)
    unknown = sorted(set(asked or ()) - known)
    if unknown:
        raise HTTPException(400, f"not an administrative event kind: {', '.join(unknown)}")
    return list(dict.fromkeys(asked or ()))


def chain_subject(subject: str | None) -> str | None:
    """A `subject` query value: a chain id of at most 200 printable characters, else a `400`."""
    if subject is not None and not (0 < len(subject) <= 200 and subject.isprintable()):
        raise HTTPException(400, "subject is a chain id, such as key:<id>")
    return subject


def instant(name: str, value: str | None) -> str | None:
    """A `since` / `until` query value as stored (UTC, to the millisecond), or a `400`: an
    instant with its offset, never a bare date or a local time."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            return stored_instant(parsed)
    except (ValueError, OverflowError):  # not an instant, or past datetime's range in UTC
        pass
    raise HTTPException(400, f"{name} is an instant with its offset: 2026-10-01T00:00:00Z")


_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def export_bound(name: str, value: str | None) -> str | None:
    """An audit export's `since` / `until`: a UTC day (`2026-09-01`), or an instant with its
    offset, as stored (`instant`); else a `400`. The store compares them as text with its UTC
    times, so an unchecked value with another offset cut the window hours off, and one that
    is no time at all exported nothing, both silently."""
    if value is None:
        return None
    if _DAY.fullmatch(value):
        try:
            date.fromisoformat(value)
        except ValueError:
            pass
        else:
            return value
    try:
        return instant(name, value)
    except HTTPException:
        raise HTTPException(
            400, f"{name} is a UTC day (2026-10-01) or an instant with its offset"
        ) from None


def operators_hold(hold: Mapping[str, Any]) -> dict[str, Any]:
    """A hold of the deployment's (set from the command line, for every organization) as an
    organization sees it in memberships mode: what it covers and since when; the operator's
    reason and name are the operator's (ADR-0008)."""
    return {key: hold[key] for key in ("id", "org", "pack_id", "pack_digest", "set_at")}


def quota_alert(alerts: AlertSettings | None, store: AuditStore) -> QuotaReached:
    """What happens the first time an organization reaches a daily quota (once a day, whichever
    instance counts that call): a `quota.reached` line in the security log (the limit as its
    `count`) and, with `MENDARI_ALERT_WEBHOOK`, one alert (`mendari.alerts`) with the
    organization, the metric and the limit (no case data), recorded. One try on the decision's
    path (its timeout QUOTA_ALERT_TIMEOUT_S); one that fails stays `pending` for `retry_due`
    (the monitor's schedule, LP-21f). Never fails the decision: a webhook that is down delays
    the alert, not the request."""

    def alert(org: str, metric: str, limit: int) -> None:
        security_log.event("quota.reached", outcome="limited", reason=metric, org=org, count=limit)
        if alerts is None:
            return
        then = (
            "evaluations make no more of these calls"
            if metric.startswith("eval_")
            else "decisions fall back to human review"
        )
        message = f"daily {metric} quota of {limit} reached; until midnight, {then}"
        deliver(
            alerts,
            event="quota.reached",
            org=org,
            alerts=[{"severity": "warning", "kind": "quota_reached", "message": message}],
            lines=[f"[warning] quota_reached: {message}"],
            attempts=1,
            timeout=QUOTA_ALERT_TIMEOUT_S,
            store=store,
        )

    return alert


_JSON_FORMATS = {"json", "application/json", "application/fhir+json", "text/json"}
_JSON_ACCEPTED = {"*/*", "application/*", "application/json", "application/fhir+json"}
_R4_VERSIONS = {"4.0", "4.0.1"}


def fhir_media_refusal(request: Request) -> JSONResponse | None:
    """R4 content negotiation (http.html): only JSON is offered, so `_format` or an Accept
    header that excludes it is a `406`, a request body in another format a `415`, and a
    `fhirVersion` media type parameter other than R4 is refused the same way. A search's body
    (`POST [type]/_search`) is a form, and only there is a form accepted; a `_format` in that
    form is checked by the route, which reads the body."""
    requested = request.query_params.get("_format")
    if requested is not None and not _offered(requested):
        return _refuse(406, "only JSON is offered: _format must be json")
    accept = request.headers.get("accept")
    if accept and not any(_takes_json(media) for media in accept.split(",")):
        return _refuse(406, "only application/fhir+json (FHIR R4) is offered")
    sent = request.headers.get("content-type")
    if sent and request.method in ("POST", "PUT", "PATCH"):
        media, params = _media_type(sent)
        if request.url.path.endswith("/_search"):
            if media != _FORM:
                return _refuse(415, f"send the search parameters as a form ({_FORM})")
        elif media not in ("application/json", "application/fhir+json"):
            return _refuse(415, "send application/fhir+json: other formats are not accepted")
        if params.get("fhirversion", "4.0") not in _R4_VERSIONS:
            return _refuse(415, "this server speaks FHIR R4 (fhirVersion=4.0)")
    return None


def _offered(format_parameter: str) -> bool:
    """Whether a `_format` value asks for JSON, the only format offered."""
    return format_parameter.split(";")[0].strip().lower() in _JSON_FORMATS


MAX_SEARCH_FORM_BYTES = 256 * 1024
REGISTRY_PREFIX = "/registry/"  # where the pack registry is mounted (LP-19d; auth.py's rule)


class BodyCap:
    """No request body over `limit` bytes (MENDARI_MAX_BODY_BYTES), whatever the route. It reads
    nothing itself: a declared length over the limit is refused before anything is read, and
    the bytes a route reads, chunked or not, are counted as they arrive and stop with a `413`
    once they pass it. It runs inside the guard, so sign-in answers first and no body is read
    before the sign-in check."""

    def __init__(self, app: ASGIApp, limit: int, counted_within: tuple[str, ...] = ()) -> None:
        self.app = app
        self.limit = limit
        # Paths an app mounted there counts itself, with a `BodyCap` of its own inside its
        # middleware (the registry, LP-19d): raised from here mid-body, the 413 would pass
        # through that app's middleware as an exception group, and be answered as a 500.
        self.counted_within = counted_within

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isascii() and declared.isdigit() and int(declared) > self.limit:
            await self.too_large(scope["path"])(scope, receive, send)
            return
        if self.counted_within and scope["path"].startswith(self.counted_within):
            await self.app(scope, receive, send)
            return
        received = 0

        async def counted() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:  # a route's handler answers it (`_http_error`)
                    raise StarletteHTTPException(413, self.detail)
            return message

        await self.app(scope, counted, send)

    @property
    def detail(self) -> str:
        size = f"{self.limit // 2**20} MiB" if self.limit % 2**20 == 0 else f"{self.limit} bytes"
        return f"a request body is {size} at most"

    def too_large(self, path: str) -> JSONResponse:
        """The `413` a route gives (`_http_error`): an OperationOutcome on FHIR routes."""
        if fhir_route(path):
            return operation_outcome(413, [{"code": "too-costly", "diagnostics": self.detail}])
        return JSONResponse(identified({"detail": self.detail}), 413)


def form_items(body: bytes) -> list[tuple[str, str]]:
    """A search's form body as (name, value) pairs, in order and with empty values kept (the
    parser decides what an empty value means)."""
    try:
        return parse_qsl(body.decode("utf-8"), keep_blank_values=True, max_num_fields=1000)
    except UnicodeDecodeError:
        problem = "the form body is not UTF-8"
    except ValueError:
        problem = "the form body has more than 1000 parameters"
    raise SearchError([{"code": "invalid", "diagnostics": problem}])


def session_cookie_name(base: str = sessions.SESSION) -> str:
    """The session (or launch) cookie's name for the API reference, which is one document for
    every host: the plain name only when `MENDARI_PUBLIC_URL` is a local host
    (server/sessions.py)."""
    host = urlsplit(os.environ.get("MENDARI_PUBLIC_URL", "").strip()).hostname
    return sessions.cookie_name(base, host or "public.invalid")


def fhir_base(request: Request) -> str:
    """The absolute FHIR base that served resources refer to and Bundle links use: under
    `MENDARI_PUBLIC_URL` when it is set (behind a proxy, the request's own host may be an
    internal one), else under the request's."""
    public = os.environ.get("MENDARI_PUBLIC_URL", "").strip().rstrip("/")
    return f"{public or str(request.base_url).rstrip('/')}/fhir"


def served(resource: Mapping[str, Any]) -> JSONResponse:
    """A read's answer (R4 http.html): the ETag is the version (`W/"n"`), Last-Modified its
    time. Nothing answers `If-None-Match` with a 304: a version is derived on read, and a
    release may render it differently, so a client's copy is never confirmed as current."""
    meta = resource["meta"]
    modified = datetime.fromisoformat(meta["lastUpdated"]).astimezone(UTC)
    headers = {
        **PRIVATE,
        "etag": f'W/"{meta["versionId"]}"',
        "last-modified": format_datetime(modified, usegmt=True),
    }
    return JSONResponse(dict(resource), media_type="application/fhir+json", headers=headers)


# Tenant-scoped FHIR answers: a shared cache (a CDN, a proxy) must never keep one, and a browser
# must ask again before reusing its own copy.
PRIVATE = {"cache-control": "private, no-cache"}
_FORM = "application/x-www-form-urlencoded"
# What each searchable type searches, and in which order (standards/fhir_search.py).
SEARCHES = {
    "Task": ("decisions as Tasks", "newest first (authoredOn, then id)"),
    "AuditEvent": ("the audit trail", "in chain order, oldest first: the feed to sync from"),
    "Provenance": ("decision provenance", "in chain order, oldest first"),
    "CodeSystem": ("Mendari's CodeSystems", "by id"),
}


def _search_description(resource: str, what: str, order: str) -> str:
    spec = SPECS[resource]
    names = ", ".join(
        f"`{p.name}`" + "".join(f" (`:{m}`)" for m in sorted(p.modifiers)) for p in spec.params
    )
    scope = (
        "The same for every organization."
        if resource == "CodeSystem"
        else "Only your organization's."
    )
    return (
        f"A `searchset` Bundle of {what}, {order}. {scope} Parameters: {names} (definitions in "
        "`/fhir/metadata`). `_count` is 50 by default and at most 500; follow the `next` link "
        "for more. `_total=accurate` adds `total`; `_summary=count` (or `_count=0`) gives only "
        "it. A parameter R4 does not define for the type is ignored, with a warning entry; a "
        "standard one Mendari does not support (`patient`, `subject`, `_sort`, ...) is a `400` "
        "unless `Prefer: handling=lenient`, and `Prefer: handling=strict` refuses unknown ones "
        "too. Dates are UTC: a date alone is that UTC day, a time needs a zone, and `ap` is not "
        "supported."
    )


def _search_parameters(resource: str) -> list[dict[str, Any]]:
    """The type's search parameters as OpenAPI query parameters, for "Try it out"."""
    return [
        {
            "name": name,
            "in": "query",
            "required": False,
            "schema": {"type": "string"},
            "description": documentation,
        }
        for name, documentation in _search_fields(resource)
    ]


def _search_form(resource: str) -> dict[str, Any]:
    properties = {
        name: {"type": "string", "description": documentation}
        for name, documentation in _search_fields(resource)
    }
    schema = {"type": "object", "properties": properties}
    return {"required": False, "content": {_FORM: {"schema": schema}}}


def _search_fields(resource: str) -> list[tuple[str, str]]:
    fields = [
        (f"{p.name}:{modifier}" if modifier else p.name, p.documentation)
        for p in SPECS[resource].params
        for modifier in ("", *sorted(p.modifiers))
    ]
    return [
        *fields,
        ("_count", "Entries per page: 50 by default, at most 500; 0 for the total only."),
        ("_total", "accurate adds Bundle.total (none and estimate give none)."),
        ("_summary", "count: the total and no entries."),
        ("cursor", "Where a page starts: copy it from a next link, never build it."),
    ]


def _takes_json(media_range: str) -> bool:
    media, params = _media_type(media_range)
    try:
        weight = float(params.get("q", "1"))
    except ValueError:
        weight = 1.0
    return (
        media in _JSON_ACCEPTED and weight > 0 and params.get("fhirversion", "4.0") in _R4_VERSIONS
    )


def _media_type(value: str) -> tuple[str, dict[str, str]]:
    media, *rest = value.split(";")
    params = {}
    for part in rest:
        name, _, setting = part.partition("=")
        params[name.strip().lower()] = setting.strip().strip('"')
    return media.strip().lower(), params


def _refuse(status: int, diagnostics: str) -> JSONResponse:
    return operation_outcome(status, [{"code": "not-supported", "diagnostics": diagnostics}])


def operation_outcome(
    status: int, issues: list[dict[str, str]], headers: Mapping[str, str] | None = None
) -> JSONResponse:
    """A FHIR R4 OperationOutcome of `error` issues ({code, diagnostics, expression?})."""
    body = {
        "resourceType": "OperationOutcome",
        "issue": [
            {
                "severity": "error",
                "code": issue["code"],
                "diagnostics": issue["diagnostics"],
                **({"expression": [issue["expression"]]} if issue.get("expression") else {}),
            }
            for issue in issues
        ],
    }
    return JSONResponse(
        body, status, headers=dict(headers or {}), media_type="application/fhir+json"
    )


# Every response: no MIME sniffing, no referrer, no framing, no powerful browser features, and
# cross-origin isolation (no other site shares a browsing context or embeds a response).
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


# Two years, this host only: a custom domain's siblings (the Hub, the website) decide their own.
HSTS = "max-age=63072000"


def content_security_policy(page: str, *, inline_styles: bool = True) -> str:
    """A page's policy: nothing but its own origin. Its inline scripts are allowed by hash, not
    'unsafe-inline' (the dashboard has two: the theme script that runs before first paint, and
    the import map). The dashboard's style attributes (data-driven bar widths) need
    'unsafe-inline' for styles; a page without any passes `inline_styles=False`."""
    inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", page, re.S)
    hashes = " ".join(
        f"'sha256-{base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()}'"
        for script in inline
    )
    styles = "'self' 'unsafe-inline'" if inline_styles else "'self'"
    return "; ".join(
        [
            "default-src 'self'",
            f"script-src 'self' {hashes}".strip(),
            f"style-src {styles}",
            "img-src 'self' data:",
            "connect-src 'self'",
            "font-src 'self'",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        ]
    )


_IMPORT_MAP = '<script type="importmap"></script>'
_NOTICE_MARKER = "<!-- mendari:notice -->"  # where a page shows the notice (mendari.notices)
_ASSET = re.compile(r'\b(src|href)="(/static/[^"?#]+\.(?:js|css))"')
_VERSIONED_FILE = re.compile(r"^[^/]+/\d+\.\d+\.\d+/[^/]+$")  # <package>/<version>/<file>
_ENTRY_MODULE = re.compile(r'<script type="module" src="/static/([^"/?#]+\.js)"')
_IMPORT = re.compile(r'^import (?:[^"\n]* from )?"\./([^"/]+\.js)";$', re.M)


def versioned_page(name: str) -> str:
    """A page under dashboard/ with each asset URL versioned, so a deploy that changes a file
    can never be served stale from a browser or CDN cache: Mendari's own modules and stylesheets
    are stamped with a hash of their content (`?v=`), and a vendored file
    (`/static/vendor/<package>/<file>`) is sent to the path of its package's version.

    Dashboard modules import each other by plain URL ("./ui.js"): a page with an empty import
    map gets one that sends each module its page's scripts reach to its stamped URL, and a
    modulepreload link per module fetches that graph in parallel (the dashboard's whole graph
    from main.js; the /docs landing only docs.js, theme.js and ui.js)."""
    page = (DASHBOARD_DIR / name).read_text(encoding="utf-8")
    page = page.replace(
        _NOTICE_MARKER, f'<p class="preview-notice" role="note">{escape(notices.NOTICE)}</p>'
    )
    if _IMPORT_MAP in page:
        stamped = {
            f"/static/{module}": f"/static/{module}?v={_digest(DASHBOARD_DIR / module)}"
            for module in _module_graph(_ENTRY_MODULE.findall(page))
        }
        preload = "".join(
            f'\n  <link rel="modulepreload" href="{url}">' for url in stamped.values()
        )
        import_map = json.dumps({"imports": stamped})
        page = page.replace(_IMPORT_MAP, f'<script type="importmap">{import_map}</script>{preload}')
    vendored = _vendored()

    def versioned(match: re.Match[str]) -> str:
        attribute, url = match[1], match[2]
        if url.startswith("/static/vendor/"):
            if url not in vendored:
                raise RuntimeError(f"{name}: {url} is in no vendored package's manifest.json")
            return f'{attribute}="{vendored[url]}"'
        return f'{attribute}="{url}?v={_digest(DASHBOARD_DIR / url.removeprefix("/static/"))}"'

    return _ASSET.sub(versioned, page)


def _module_graph(entries: list[str]) -> list[str]:
    """Every dashboard module the entry modules import, directly or not, themselves included
    (static `import … from "./x.js"` lines: the modules use no other kind)."""
    reached: set[str] = set()
    waiting = list(entries)
    while waiting:
        module = waiting.pop()
        if module not in reached:
            reached.add(module)
            waiting.extend(_IMPORT.findall((DASHBOARD_DIR / module).read_text(encoding="utf-8")))
    return sorted(reached)


def _vendored() -> dict[str, str]:
    """`/static/vendor/<package>/<file>` -> the file's URL under its package's version, from
    each package's manifest.json (scripts/vendor_docs_ui.py writes it, and checks the files)."""
    urls: dict[str, str] = {}
    for path in sorted(VENDOR_DIR.glob("*/manifest.json")):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        package = f"/static/vendor/{path.parent.name}"
        urls.update(
            {f"{package}/{f}": f"{package}/{manifest['version']}/{f}" for f in manifest["files"]}
        )
    return urls


class _Static(StaticFiles):
    """The dashboard's files. A vendored file's path names its package's version, so its content
    never changes there: browsers keep it a year without asking again."""

    def file_response(
        self, full_path: os.PathLike[str] | str, *args: Any, **kwargs: Any
    ) -> Response:
        response = super().file_response(full_path, *args, **kwargs)
        path = Path(full_path)
        if path.is_relative_to(VENDOR_DIR) and _VERSIONED_FILE.match(
            path.relative_to(VENDOR_DIR).as_posix()
        ):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def declared_actions(pack: Pack, *, vouched: bool) -> dict[str, Any]:
    """An agent's declared actions as its detail shows them (`actions`): each with its effect,
    its gate (`policy.gate_of`, for an agent the organization does or does not vouch for) and
    the gate in words, and the names of its arguments (None: not checked). Nothing for an agent
    that declares none, so its detail is what it was."""
    declared = pack.manifest.actions
    if not declared:
        return {}
    listed = []
    for name, spec in declared.items():
        gate = gate_of(spec.effect, allow_auto=spec.allow_auto, vouched=vouched)
        schema = spec.json_schema
        listed.append(
            {
                "name": name,
                "description": spec.description,
                "effect": spec.effect.value,
                "allow_auto": spec.allow_auto,
                "gate": gate.value,
                "released": GATE_TEXT[gate],
                "arguments": None if schema is None else list(schema.get("properties") or {}),
            }
        )
    return {"actions": listed}


def _summary(pack: Pack) -> dict[str, Any]:
    m = pack.manifest
    return {
        "id": m.id,
        "version": m.version,
        "name": m.name,
        "summary": m.summary,
        "digest": pack.digest,
        # `publisher.type` is what the manifest says of itself; `origin` is where this
        # deployment found the pack (`core/origin.py`), and `origin_label` what to show for it.
        "publisher": m.publisher.model_dump(mode="json"),
        "origin": pack.shown_origin,
        "origin_label": pack.origin_label,
        # Derived from `card.provenance` (`core/status.py`), never written by the pack.
        "status_label": pack.status_label,
        "license": m.license,
        "risk": {"level": int(m.risk.level), "category": m.risk.category},
        "primary": m.policy.primary,
        "labels": m.labels(m.policy.primary),
        "cds_service": cds_hooks.service_id(pack) if m.cds_hooks else None,
    }


def _labels_questions_of(path: Path, pack: Pack) -> bool:
    """A local dataset is offered to the pack whose questions its first record labels."""
    try:
        with path.open(encoding="utf-8") as handle:
            first = json.loads(handle.readline() or "null")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    labels = first.get("labels") if isinstance(first, dict) else None
    return isinstance(labels, dict) and bool(labels) and set(labels) <= set(pack.manifest.questions)
