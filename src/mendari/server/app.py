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
import os
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from html import escape
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, urlsplit

import httpx
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
from pydantic import BaseModel, Field, model_validator
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mendari import __version__, guard, notices, settings
from mendari.core import clock, tenancy
from mendari.core.audit import ADMINISTRATIVE_KINDS, OFF_DECISION_KINDS, SYSTEM_ID, AuditStore
from mendari.core.card import benchmark, render_card
from mendari.core.decision import CallBudget, Decision, QuotaReached
from mendari.core.origin import is_local
from mendari.core.pack import Pack
from mendari.core.phi import phi_policy, require_protection
from mendari.core.policy import GovernanceOverride
from mendari.core.registry import SearchPath, discover, version_key
from mendari.errors import (
    AuthError,
    CdsHooksError,
    ClientError,
    FhirError,
    FhirStructureError,
    InvitationRefused,
    MendariError,
    NoteError,
    NoticeNotAcknowledged,
    PackNotFoundError,
    PackValidationError,
    ProviderError,
    ReviewError,
    ScopeError,
    SmartError,
)
from mendari.evals import compare, figures, leaderboard, load_dataset, resolve_dataset
from mendari.evals.feedback import feedback_dataset
from mendari.evals.harness import Dataset, evaluation
from mendari.evals.monitoring import MAX_DAYS as MONITOR_MAX_DAYS
from mendari.evals.monitoring import MIN_DAYS as MONITOR_MIN_DAYS
from mendari.evals.monitoring import check_webhook, monitor
from mendari.evals.pack_tests import load_cases
from mendari.legacy import current_pack_id
from mendari.providers import jev, llm, provider_allow_list, slots_without_model
from mendari.providers.credentials import key_source
from mendari.server import api_docs, landing, security_log, sessions
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
from mendari.server.limits import DailyCap, Limiter, RateLimited, client_ip, limit_settings
from mendari.server.origin import OriginCheck, is_local_host, trusted_origins
from mendari.server.readiness import Readiness, packs_state
from mendari.server.smart_routes import add_smart_routes, error_page
from mendari.settings import FhirValidation, applies_to, providers_for
from mendari.standards import (
    audit_fhir,
    capability,
    cds_hooks,
    davinci,
    decide_op,
    fhir_search,
    pas,
)
from mendari.standards.fhir_client import GuardedHttp, allowed_servers
from mendari.standards.pas import PasError
from mendari.standards.search import SPECS, SearchError, encode_cursor, handling, searchset
from mendari.standards.search import parse as parse_search
from mendari.standards.search import stored as stored_instant
from mendari.standards.smart import smart_ehrs, smart_settings
from mendari.standards.terminology import Terminology

DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "dashboard"
VENDOR_DIR = DASHBOARD_DIR / "vendor"  # third-party front-end code (scripts/vendor_docs_ui.py)
INBOX = "mendari/patient-inbox-router"
JsonBody = Annotated[dict[str, Any], Body()]
_DATASET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_STATUS = {
    PackNotFoundError: 404,
    ReviewError: 409,
    NoteError: 422,
    FhirStructureError: 400,  # before FhirError: the first match wins
    FhirError: 422,
    PackValidationError: 422,
    PasError: 422,
    ScopeError: 422,
    ClientError: 422,
    CdsHooksError: 400,
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
_FHIR_ROUTE = re.compile(r"^/(fhir/|api/packs/[^/]+/[^/]+/fhir/)")
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


class ApprovalRequest(BaseModel):
    pack_id: str = Field(min_length=3, max_length=200)
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


class PriorAuthorizationRequest(BaseModel):
    """A PAS request for a decided order: the EHR's resources, and the X12 codes the payer needs
    (unless the order carries them). `submit` sends it to $MENDARI_PAS_PAYER_URL."""

    decision_id: str = Field(min_length=1, max_length=100)
    patient: dict[str, Any]
    coverage: dict[str, Any]
    insurer: dict[str, Any]
    requester: dict[str, Any]
    order: dict[str, Any]
    service_type: str | None = Field(default=None, max_length=5, description="X12 1365")
    place_of_service: str | None = Field(default=None, max_length=5, description="CMS POS code")
    diagnoses: list[str] = Field(default=[], max_length=12, description="ICD-10-CM codes")
    submit: bool = False


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
# The scheduled purge's bearer secret (CRON_SECRET, as Vercel Cron sends it): its least length.
MIN_CRON_SECRET = 16


class EraseRequest(BaseModel):
    reason: Literal["request", "error"]


class HoldRequest(BaseModel):
    pack_id: str | None = Field(default=None, max_length=200)
    pack_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=200)


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
    phi = phi_policy()  # a bad PHI setting (or a missing key) fails here, at startup
    launch = smart_options is not None  # a launch fetches the patient's record
    # and plaintext where it must not be: never with sign-in, never on the hosted site
    require_protection(phi, sign_in=sign_in is not None, launch=launch)
    retention_days = settings.retention_days()  # likewise
    cron_secret = os.environ.get("CRON_SECRET", "")  # the scheduled purge's credential
    if cron_secret and (len(cron_secret) < MIN_CRON_SECRET or cron_secret != cron_secret.strip()):
        # An optional job never stops the server: the purge stays off, and health says so.
        phi_logger.warning(
            "CRON_SECRET is not used, so the scheduled purge is off: it needs at least %d random "
            "characters, with no spaces around them (Vercel Cron sends it as it is stored)",
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
    webhook = os.environ.get("MENDARI_ALERT_WEBHOOK", "").strip()
    if org_limits and webhook:
        check_webhook(webhook)  # likewise: a quota's alert must be able to leave
    # Memberships mode marks the database: from then on each event names its organization,
    # whichever mode or command opens it (LP-12g, ADR-0008).
    bind_org = sign_in is not None and sign_in.accounts == "memberships"
    store = AuditStore(db_path, migrate_url=migrate_url, phi=phi, bind_org=bind_org)
    store.ready_to_keep()  # never `off` on a protected database, nor a retired PHI key
    acknowledgement = notices.mode()  # read once; a bad value fails at startup
    acknowledged: set[str] = set()  # chain ids found acknowledged: an acknowledgement stays
    auth = Auth(
        sign_in,
        store,
        tokens=backend is not None,
        lifetime=session_lifetime,
        ehrs=ehrs if smart_options is not None else None,
    )
    origins = OriginCheck(open_mode=not auth.enabled, trusted=trusted_origins())  # likewise
    limiter = Limiter(store, rate_limits)
    # The daily cap per principal (MENDARI_PRINCIPAL_DAILY_LIMIT): sign-in only.
    cap = DailyCap(store, principal_limit) if principal_limit is not None and auth.enabled else None
    if principal_limit is not None and cap is None:
        quota_logger.warning("MENDARI_PRINCIPAL_DAILY_LIMIT is ignored in open mode: no principals")
    budget = (
        CallBudget(
            store,
            budget_limits or {},
            org_limits=org_limits,
            on_quota_reached=quota_alert(webhook or None),
        )
        if budget_limits or org_limits
        else None
    )
    found = discover(packs_paths)
    only = settings.enabled_packs()  # a per-pack image serves just its pack
    missing = only - {pack.id for pack in found.packs}
    if missing:
        raise MendariError(f"MENDARI_PACKS: packs not installed here: {sorted(missing)}")
    # The newest version of every pack found, served or not. `discover` refuses any second
    # claim to an id a community pack takes part in, so a community pack never competes here.
    installed: dict[str, Pack] = {}
    for pack in found.packs:
        current = installed.get(pack.id)
        if current is None or version_key(pack.version) > version_key(current.version):
            installed[pack.id] = pack
    packs = {pack_id: pack for pack_id, pack in installed.items() if not only or pack_id in only}
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

    def pack_or_404(pack_id: str) -> Pack:
        pack = packs.get(pack_id)
        if pack is None:
            raise PackNotFoundError(f"no pack {pack_id!r}")
        return pack

    skipped_settings: dict[str, list[str]] = {}
    fallbacks = {pack_id: dict(slots) for pack_id, slots in (default_providers or {}).items()}
    # Settings that apply to no served pack (an unknown slot, a pack not served): reported in
    # /api/health and the startup log, never a failed start (instances share one setting).
    unused_settings = sorted(
        f"{key}={variant}"
        for key, variant in overrides.items()
        if not any(
            applies_to(key, pack.id, pack.manifest.providers, community=pack.community)
            for pack in packs.values()
        )
    )
    if unused_settings:
        providers_logger.warning(
            "provider settings that apply to no served pack: %s", ", ".join(unused_settings)
        )
    # ...and those no served pack takes at all (above, or a variant no pack with the slot has):
    # what `mendari serve --provider` refuses when typed.
    taken: set[str] = set()
    for pack in packs.values():
        pack_slots = {slot: set(spec.variants) for slot, spec in pack.manifest.providers.items()}
        chosen = providers_for(pack.id, pack_slots, overrides, community=pack.community)[0]
        for key, variant in overrides.items():
            slot = key.rpartition(":")[2]
            if chosen.get(slot) == variant and applies_to(
                key, pack.id, pack_slots, community=pack.community
            ):
                taken.add(key)
    not_taken = sorted(f"{key}={variant}" for key, variant in overrides.items() if key not in taken)

    def variants_for(pack: Pack) -> tuple[dict[str, str], list[str]]:
        """The variants the deployment chose for this pack's slots (the rest run the pack's
        default), and the settings that name the pack but cannot apply. A community pack takes
        only settings that name it, never the defaults (it spends only where the owner said)."""
        slots = {slot: set(spec.variants) for slot, spec in pack.manifest.providers.items()}
        chosen, skipped = providers_for(pack.id, slots, overrides, community=pack.community)
        if pack.community:
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
            community=pack.community,
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
                deadline_s=deadline_s,
            )
        except MendariError:
            raise
        except Exception as error:  # the pack's content: named by type, never a 500
            problem = f"its providers could not be built ({type(error).__name__})"
            raise PackValidationError(pack.id, [problem]) from None

    def decision_for(pack_id: str) -> Decision:
        """A served pack's Decision, built at start-up. Its first use records the deployment, once
        per process (concurrent first uses: `record_deployment` keeps one row), with no lock
        /api/health could wait on. A pack that could not be built raises why on every use."""
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
        not only served: a per-pack instance can share a database with other packs' decisions,
        and decisions from before the rename carry the old pack id."""
        pack = installed.get(current_pack_id(pack_id))
        if pack is None or question not in pack.manifest.questions:
            return None
        return pack.manifest.labels(question)

    # Automation holds (`AuditStore.hold_automation`), read per decision and kept a few seconds:
    # a hold set on another instance takes effect within HOLD_SECONDS, one set here at once.
    hold_cache: dict[tuple[str, str, str], tuple[float, str | None]] = {}
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

    default_pack = INBOX if INBOX in packs else next(iter(sorted(packs)), None)
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
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]

    @app.exception_handler(MendariError)
    async def _error(request: Request, error: MendariError) -> Response:
        status = next((code for kind, code in _STATUS.items() if isinstance(error, kind)), 400)
        if isinstance(error, SmartError | AuthError):
            status = error.status
        if isinstance(error, ProviderError):
            status = _PROVIDER_STATUS.get(error.code, _PROVIDER_UPSTREAM)
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
        if _FHIR_ROUTE.match(request.url.path):
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
        if _FHIR_ROUTE.match(request.url.path):
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
        headers = {**SECURITY_HEADERS, **({"X-Request-Id": rid} if rid else {})}
        if _FHIR_ROUTE.match(request.url.path):
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

    @app.get(
        "/api/health",
        tags=["Service"],
        summary="Deployment health and settings",
    )
    def health(request: Request) -> dict[str, Any]:
        """Public, for load balancers: `status`, `version` and `sign_in`, and nothing else
        without a credential (no database work either). A principal that may `read` (everyone
        in open mode) also gets the deployment's settings: packs (and how many come from
        where), storage, the FHIR validation mode, the FHIR and provider allow-lists, PHI mode,
        the input hash's kind and key id, which provider answers each pack's questions, today's
        remote calls (in memberships mode the operator's, `mendari usage`, as the last purge's
        counts are), the rate limits (where the caller's address is read, and the address seen
        on this request), the session policy, the organization's daily quotas with what it used
        today, and whether SMART Backend Services is on (`backend_services`: its token endpoint,
        or null).

        The packs' state (which provider each runs, the settings skipped, a pack that could not
        be built, the problems an owned pack loads with, `pack_warnings`) is
        computed at start-up: health builds no pack and records no deployment (a signed-in
        caller's session may still be refreshed, as on any route).
        """
        public = {"status": "ok", "version": __version__, "sign_in": auth.enabled}
        principal = auth.principal(request)  # no credential: no lookup
        if principal is None or principal.org is None or not principal.may("read"):
            return public  # a person of no organization (memberships mode) too
        return {
            **public,
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
                if principal.kind == "open" or ehr.org == principal.org
            ],
            "phi_mode": phi.mode,
            # Under `encrypt`, the ids of the PHI keys (never a key): the one that seals, and
            # those it only opens with (MENDARI_PHI_READ_KEYS, during a rotation).
            **({"phi_keys": phi.key_ids} if phi.mode == "encrypt" else {}),
            # Kept inputs and notes are deleted after this many days (MENDARI_RETENTION_DAYS),
            # by `mendari phi purge` or, with CRON_SECRET set, the scheduled purge.
            "retention_days": retention_days,
            "retention_scheduled": bool(cron_secret),
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
                # The reference packs run Mendari + Jev where no setting chose (JEV_DEFAULTS).
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
            "holds": len(store.holds(principal.org)),
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
                    principal.org if principal.kind != "open" else tenancy.DEFAULT_ORG
                ),
            },
        }

    @app.get(
        "/api/health/ready",
        tags=["Service"],
        summary="Readiness: the database answers and the packs loaded",
        responses={503: {"description": "Not ready: the database did not answer, or no pack"}},
    )
    def ready() -> Response:
        """Public, for status pages, uptime checks and readiness probes (`server/readiness.py`),
        with `GET` or `HEAD`: `status` (`ready`, `degraded` or `unready`), `version` and
        `checks`: `database` (`ok` or `unreachable`) and `packs` (`ok`; `degraded` while a pack
        did not load or build, which signed-in `/api/health` lists under `invalid`; `none` when
        no pack is served). `503` when unready. Fixed codes only: no error text, no pack ids,
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
        "/api/packs",
        tags=["Packs"],
        summary="List packs",
    )
    def list_packs() -> list[dict[str, Any]]:
        """Every Decision Pack this deployment serves: id, version, risk level, the labels of its
        main question, and where it comes from (`origin`: `maintained`, `community`, `hub` or
        `local`, with `origin_label` to show; a community pack is not clinically reviewed and
        never automates)."""
        return [_summary(pack) for pack in sorted(packs.values(), key=lambda p: p.id)]

    @app.get(
        "/api/packs/{namespace}/{name}",
        tags=["Packs"],
        summary="Get a pack",
    )
    def pack_detail(namespace: str, name: str) -> dict[str, Any]:
        """A pack's manifest, input and output JSON Schemas, active providers, benchmark,
        leaderboard and Decision Card (Markdown)."""
        pack_id = f"{namespace}/{name}"
        pack = pack_or_404(pack_id)
        decision = decision_for(pack_id)
        return {
            **_summary(pack),
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
            "benchmark": None
            if (published := benchmark(pack)) is None
            else _with_figures(published),
            "leaderboard": leaderboard.load(pack),
            "card_markdown": render_card(pack),
            "datasets": [*pack.manifest.evaluation.datasets, *local_datasets(pack)],
        }

    @app.get(
        "/api/packs/{namespace}/{name}/datasets",
        tags=["Packs"],
        summary="List a pack's datasets",
    )
    def pack_datasets(namespace: str, name: str) -> list[dict[str, Any]]:
        """The datasets an evaluation can run on, with their size (e.g. to say what a run will
        cost in remote calls before it starts)."""
        pack = pack_or_404(f"{namespace}/{name}")
        names = [*pack.manifest.evaluation.datasets, *local_datasets(pack)]
        return [{"name": n, "n": len(dataset_for(pack, n).records)} for n in names]

    @app.get(
        "/api/packs/{namespace}/{name}/examples",
        tags=["Packs"],
        summary="Example inputs",
    )
    def pack_examples(namespace: str, name: str) -> list[dict[str, Any]]:
        """Synthetic example inputs: the pack's own behaviour tests. Tests that do not load, or
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

    @app.get("/api/notice", tags=["Packs"], summary="The notice, and whether you acknowledged it")
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
        tags=["Packs"],
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

    @app.post(
        "/api/packs/{namespace}/{name}/decide",
        tags=["Decisions"],
        summary="Make a decision",
    )
    def decide(namespace: str, name: str, payload: JsonBody) -> dict[str, Any]:
        """Run the pack on one input (its `input_schema`; see `GET …/examples` for synthetic ones).

        Every decision is recorded in the audit trail and ends as `AUTO`, `HUMAN_REVIEW`,
        `ABSTAIN` or `BLOCK`. **An input that fails the pack's schema is not an HTTP error:** it
        is a recorded decision with `outcome: BLOCK` and the reason in `reasons`, so check
        `outcome`, not only the status code.

        The answer is complete, with each answer's `evidence`; the audit record keeps it only as
        the PHI mode allows (`GET /api/audit/{id}`). `input_hash` is `hmac-sha256:<key id>:<hex>`
        where the deployment has an input hash key (always with PHI), else `sha256:<hex>`."""
        return {**decision_for(f"{namespace}/{name}").decide(payload).to_dict(), **NOTICE}

    @app.post(
        "/api/packs/{namespace}/{name}/guard",
        tags=["Decisions"],
        summary="Check an LLM's draft reply",
    )
    def guard_draft(namespace: str, name: str, payload: JsonBody) -> dict[str, Any]:
        """Check an LLM's draft reply: `released` (send `reply`), `held_for_review` or
        `blocked` (send nothing; follow `fallback`). Audited like any decision."""
        return {**guard.check(decision_for(f"{namespace}/{name}"), payload).to_dict(), **NOTICE}

    @app.post(
        "/api/packs/{namespace}/{name}/fhir/decide",
        tags=["FHIR and CDS Hooks"],
        summary="Make a decision from FHIR",
    )
    def decide_fhir(namespace: str, name: str, payload: JsonBody) -> dict[str, Any]:
        """Decide from FHIR R4 resources: a Bundle, one resource, or a CDS Hooks prefetch object.
        The pack's `fhir/mapping.yaml` maps them onto its input. Returns the decision and a FHIR
        `Task` for the work queue.

        Inputs first pass a structural R4 check in every mode: an unknown element or resource
        type, an element of the wrong JSON type, or a primitive without its format is a `400`
        OperationOutcome (issue code `structure` or `value`). A complex element R4 requires, a
        code outside an R4 required value set, and US Core 6.1.0's rules are checked under
        `fhir_validation` (`/api/health`): `warn` records them in `decision.input_issues`,
        `enforce` blocks. An error on this route is an `OperationOutcome`. For FHIR out as well,
        use `POST /fhir/$decide`."""
        result, task = decision_for(f"{namespace}/{name}").decide_fhir(payload)
        return {"decision": result.to_dict(), "task": task, **NOTICE}

    # -- monitoring ------------------------------------------------------------------------------

    @app.get(
        "/api/monitoring",
        tags=["Evaluation and monitoring"],
        summary="Monitor a pack over time",
    )
    def monitoring(pack_id: str, days: int = 30) -> dict[str, Any]:
        """The served version of the pack over time for this organisation: weekly series, drift
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
    def deployments(pack_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Which pack version runs where, with which providers, since when (newest first). In
        memberships mode the deployment's records are the operator's: `404` (ADR-0008)."""
        if auth.memberships:
            raise HTTPException(404, "the deployment's records are its operator's")
        if pack_id is not None:
            decision_for(pack_or_404(pack_id).id)  # this server's own row is recorded first
        return store.deployments(pack_id, limit=min(limit, 200))

    @app.get(
        "/api/governance",
        tags=["Governance"],
        summary="List automation approvals",
    )
    def governance(pack_id: str | None = None) -> dict[str, Any]:
        """Approvals for this organization, and what a pack's risk level allows."""
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
        """Record an organization's approval for a risk level 2 or 3 pack to automate (ADR-0002): a
        reference (e.g. committee minutes) and how long it lasts. It applies to exactly this
        pack version (its digest)."""
        pack = pack_or_404(body.pack_id)
        approver = acting_as(request, body.approver, "approver")
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
        """Revoke an approval; the pack's automation falls back to its risk-level cap."""
        if not store.revoke_approval(approval_id, revoked_by=principal_of(request).actor):
            raise HTTPException(404, "no such approval")
        log_event(request, "governance.revoked", ids=[approval_id])
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
        """Stop automation for your organization: every pack, one pack (`pack_id`), or one
        version of it (`pack_digest`). Decisions still run and are recorded; any that would be
        `AUTO` goes to a person instead, with the same recommendation and priority. Chained as
        `automation.held`; it lasts until lifted (`DELETE /api/holds/{id}`)."""
        if body.pack_id is not None:
            served = pack_or_404(body.pack_id)
            if body.pack_digest is not None and body.pack_digest != served.digest:
                raise HTTPException(422, "pack_digest is not the digest this deployment serves")
        elif body.pack_digest is not None:
            raise HTTPException(422, "a hold on a digest names its pack")
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
        "/api/packs/{namespace}/{name}/evals",
        tags=["Evaluation and monitoring"],
        summary="Run an evaluation",
    )
    def run_eval(namespace: str, name: str, request: EvalRequest, http: Request) -> dict[str, Any]:
        """Evaluate the pack (or another provider configuration) on one of its datasets, and compare
        with the published benchmark. Calls to remote providers are capped at 200 cases."""
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
                    f"{pack.id} is a community pack: it runs a remote variant only where the "
                    f"deployment names it in its settings ({pack.id}:slot=variant), not "
                    f"{', '.join(refused)}"
                )
        # Provider calls count against the daily budgets, never an organization's quotas.
        decision = Decision(
            pack,
            providers={**chosen, **asked},
            budget=None if budget is None else budget.for_evaluation(),
            deadline_s=deadline_s,  # each of its decisions has the deployment's deadline
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
            report["comparison"] = compare(
                report, baseline, pack.manifest.evaluation.degradation_tolerance
            )
        store.save_report(report, ledger=run.ledger.dumps())
        return _with_figures(report)

    @app.get(
        "/api/evals",
        tags=["Evaluation and monitoring"],
        summary="List evaluation reports",
    )
    def list_evals(pack_id: str | None = None) -> list[dict[str, Any]]:
        """Saved evaluation reports, newest first, each with its figures (`figures`)."""
        return [_with_figures(report) for report in store.reports(pack_id)]

    # -- review & audit ----------------------------------------------------------------------

    @app.get(
        "/api/review",
        tags=["Review and feedback"],
        summary="The review queue",
    )
    def review_queue(
        request: Request, pack_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Decisions waiting for a human (`HUMAN_REVIEW`, `ABSTAIN`), most urgent first."""
        queue = store.pending_reviews(pack_id, limit=min(limit, 500), reader=input_reader(request))
        logged_reads(request, queue)
        return queue

    @app.post(
        "/api/review/{decision_id}",
        tags=["Review and feedback"],
        summary="Resolve a review",
    )
    def resolve(decision_id: str, request: ReviewRequest, http: Request) -> dict[str, Any]:
        """Record a reviewer's final label for a decision in the queue: one of the labels of the
        decision's question. A note is kept apart from the audit chain; the answer repeats it."""
        return store.resolve_review(
            decision_id,
            final_label=request.final_label,
            reviewer=acting_as(http, request.reviewer) or "",
            note=request.note,
            labels=question_labels,
        )

    @app.get(
        "/api/audit",
        tags=["Audit"],
        summary="List decisions",
    )
    def audit_log(
        pack_id: str | None = None,
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
            limit=min(limit, 500),
            offset=max(offset, 0),
        )
        return store.withhold_inputs(listed)

    @app.get(
        "/api/audit/verify",
        tags=["Audit"],
        summary="Verify the audit chain",
    )
    def verify() -> dict[str, Any]:
        """Recompute the hash chain over every event; `ok: false` means the trail was altered.
        In memberships mode the whole chain is the operator's, verified from the command line
        (`mendari audit verify`): `404` here (ADR-0008)."""
        if auth.memberships:
            raise HTTPException(404, OPERATORS_CHAIN)
        return store.verify()

    @app.get(
        "/api/audit/export/fhir",
        tags=["FHIR and CDS Hooks"],
        summary="Export the audit trail as FHIR",
    )
    def export_audit_fhir(
        request: Request,
        pack_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> StreamingResponse:
        """The audit trail as FHIR NDJSON (AuditEvent and Provenance, ids and codes only),
        streamed in chain order; `since` / `until` bound the event time (ISO 8601, UTC)."""
        log_event(request, "export.audit")  # its filters are query values: not logged
        lines = (
            json.dumps(resource, separators=(",", ":")) + "\n"
            for resource in audit_fhir.resources(
                store.iter_events(pack_id=pack_id, since=since, until=until)
            )
        )
        return StreamingResponse(lines, media_type="application/fhir+ndjson")

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
    ) -> dict[str, Any]:
        """The chained events of what your organization owns, oldest first: its keys, clients,
        automation approvals and holds, evaluation reports, its record, memberships,
        invitations and requests to join (ADR-0008). Each is `{subject, event, payload,
        created_at, hash}`: the event's chain id, its kind, what was chained, when, and its
        hash, which `mendari audit verify` checks. Never its place in the deployment's chain,
        nor another organization's events, nor the deployment's own (the operator's).

        `kind` (repeatable) and `subject` (a chain id, `key:<id>`) narrow it; `since` and
        `until` are instants with their offset; `limit` is 1 to 500 (100). `next`, when there is
        more, is the `after` of the next page."""
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
        pack that keeps only the hash) its answers' `evidence` is empty; under `encrypt` the
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
        record = store.get(decision_id)
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

    @app.get("/api/internal/retention", include_in_schema=False)
    def scheduled_retention(request: Request) -> dict[str, Any]:
        """The scheduled purge (Vercel Cron, `vercel.json`): deletes kept inputs and erases
        notes older than MENDARI_RETENTION_DAYS in batches, as `mendari phi purge` does, and
        chains a summary (`retention.purged`). Only with `Authorization: Bearer $CRON_SECRET`;
        without CRON_SECRET set it is not found."""
        if not cron_secret:
            raise HTTPException(404, "Not Found")
        given = request.headers.get("authorization", "")
        if not hmac.compare_digest(given.encode(), f"Bearer {cron_secret}".encode()):
            security_log.event("retention.refused", outcome="refused", reason="secret")
            raise HTTPException(401, "this route is for the scheduled purge")
        summary = store.purge(retention_days, source="schedule")
        security_log.event("retention.purged", count=summary["inputs"] + summary["notes"])
        return summary

    @app.post(
        "/api/audit/{decision_id}/outcome",
        tags=["Review and feedback"],
        summary="Record what really happened",
    )
    def record_outcome(decision_id: str, request: OutcomeRequest, http: Request) -> dict[str, str]:
        """Record the final label from a downstream system (e.g. where the message was really
        handled), for monitoring: one of the labels of the decision's question. `source` names
        that system as an identifier; who recorded it is the caller (the signed-in person or API
        key), whatever the body says."""
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
        pack_id: str | None = None,
        rating: Literal["up", "down"] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Ratings, newest first; each note as `GET /api/audit/{decision_id}` shows notes."""
        ratings = store.feedback(
            pack_id=pack_id, rating=rating, limit=min(limit, 500), reader=input_reader(request)
        )
        logged_reads(request, ratings, id_key="decision_id")
        return ratings

    @app.get(
        "/api/packs/{namespace}/{name}/feedback/export",
        tags=["Review and feedback"],
        summary="Export feedback as a dataset",
    )
    def export_feedback(
        request: Request, namespace: str, name: str, rating: Literal["up", "down"] = "down"
    ) -> Response:
        """Rated decisions as JSONL for evaluation; `x-mendari-skipped` counts the ones stored
        without their input."""
        pack = pack_or_404(f"{namespace}/{name}")
        records, skipped = feedback_dataset(
            store, pack_id=pack.id, rating=rating, reader=input_reader(request)
        )
        log_event(request, "export.feedback", ids=[pack.id], count=len(records))
        if phi.mode != "off" and input_reader(request) is not None and records:
            shown = [str(record["id"]).removeprefix("feedback-") for record in records]
            log_event(request, "phi.inputs.read", ids=shown, count=len(shown))
        body = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
        return Response(
            body,
            media_type="application/x-ndjson",
            headers={
                "content-disposition": f'attachment; filename="{name}-feedback-{rating}.jsonl"',
                # Decisions stored with audit.store_input=hash have no text to export.
                "x-mendari-skipped": str(skipped),
            },
        )

    @app.get(
        "/api/stats",
        tags=["Audit"],
        summary="Decision statistics",
    )
    def stats(pack_id: str | None = None) -> dict[str, Any]:
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
        """The CDS Hooks services this deployment offers (one per pack that declares a hook)."""
        return cds_hooks.discovery(packs.values())

    @app.post(
        "/cds-services/{service_id}",
        tags=["FHIR and CDS Hooks"],
        summary="Call a CDS Hooks service",
    )
    def cds_invoke(service_id: str, request: JsonBody, http: Request) -> dict[str, Any]:
        """A CDS Hooks request (context and prefetch) → cards with the decision and its reasons.
        An order hook decides each draft order the pack is about, at most 50 a request: each
        counts as one decision against the rate limit and the principal's daily cap."""
        pack = next((p for p in packs.values() if cds_hooks.service_id(p) == service_id), None)
        if pack is None:
            raise PackNotFoundError(f"no CDS service {service_id!r}")

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

        return cds_hooks.invoke(decision_for(pack.id), request, most=most_orders, charge=charge)

    @app.post(
        "/api/packs/{namespace}/{name}/pas",
        tags=["FHIR and CDS Hooks"],
        summary="Da Vinci PAS request",
    )
    def prior_authorization(
        namespace: str, name: str, body: PriorAuthorizationRequest, http: Request
    ) -> dict[str, Any]:
        """Da Vinci PAS: the request Bundle for a complete request (`submit_ready`), and with
        `submit` (a decision that was automated, or that a reviewer resolved as `submit_ready`),
        the payer's answer, which is chained on the decision (`pas.submitted`): its
        outcome and review actions; the authorization number and disposition are kept as a
        note (PHI mode `hash` keeps neither)."""
        pack = pack_or_404(f"{namespace}/{name}")
        record = store.get(body.decision_id)
        if record is None or record["pack_id"] != pack.id:
            raise PackNotFoundError(f"no decision {body.decision_id} of {pack.id}")
        bundle = pas.request_bundle(
            decision={"id": record["id"], "recommendation": record["recommendation"]},
            order=body.order,
            patient=body.patient,
            coverage=body.coverage,
            insurer=body.insurer,
            requester=body.requester,
            service_type=body.service_type,
            place_of_service=body.place_of_service,
            diagnoses=body.diagnoses,
        )
        submitted = None
        if body.submit:
            # The reviewer's own label, as the chain recorded it: an outcome reported later can
            # change `final_label`, never what the reviewer resolved.
            confirmed = any(
                event["event"] == "review.resolved"
                and event["payload"].get("final_label") == "submit_ready"
                for event in record.get("events") or ()
            )
            if record["outcome"] != "AUTO" and not confirmed:
                raise PasError(
                    "this decision went to a person: it is sent to the payer once a reviewer "
                    "confirms it as submit_ready (POST /api/review/{id})"
                )
            payer = settings.pas_payer_url()
            with GuardedHttp() as client:
                submitted = pas.submit(bundle, payer, client)
            codes = {key: submitted[key] for key in ("outcome", "review_actions")}
            store.record_event(
                record["id"],
                "pas.submitted",
                {**codes, "requested_by": acting_as(http, None) or "open mode"},
                note=None if phi.mode == "hash" else pas.payer_note(submitted),
            )
        return {"bundle": bundle, "submitted": submitted}

    @app.post(
        "/fhir/Questionnaire/$questionnaire-package",
        tags=["FHIR and CDS Hooks"],
        summary="Da Vinci DTR $questionnaire-package",
    )
    def questionnaire_package(payload: JsonBody) -> JSONResponse:
        """Da Vinci DTR: the documentation the payer asks for, for orders a pack decided (found by
        CRD's assertion id), pre-answered from the decision (`standards/davinci.py`)."""

        def lookup(decision_id: str) -> tuple[Pack, dict[str, str]] | None:
            record = store.get(decision_id)
            pack = packs.get(record["pack_id"]) if record is not None else None
            if record is None or pack is None:
                return None
            answers = record["record"].get("answers") or {}
            return pack, {key: str(answer.get("label")) for key, answer in answers.items()}

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
        `status`. Then a RequestGroup and the Task (under BLOCK and ABSTAIN, the pack's fallback),
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
        """One of the CodeSystems Mendari's FHIR uses (decision outcomes, audit events, packs,
        work queues, each pack's question labels), generated from the code and the served
        packs. Its canonical `url` is on mendari.co; each deployment serves its own copy."""
        return conformance(terms.get("CodeSystem", resource_id))

    @app.get(
        "/fhir/NamingSystem/{resource_id}",
        tags=["FHIR and CDS Hooks"],
        summary="Read a Mendari NamingSystem",
    )
    def fhir_naming_system(resource_id: str) -> JSONResponse:
        """One of the identifier systems Mendari's FHIR uses (decision ids, input hashes, pack
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
    app.add_middleware(BodyCap, limit=body_limit)

    @app.middleware("http")
    async def _fhir_media_types(request: Request, call_next: Any) -> Response:
        """FHIR routes speak FHIR R4 JSON only. Inside the guard, so sign-in answers first. The
        SMART configuration is plain JSON whatever the client accepts (SMART App Launch 2.2)."""
        if _FHIR_ROUTE.match(request.url.path) and request.url.path != DISCOVERY_PATH:
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
        # Who asked, as the chain names them, rides along with the organization (open mode: none).
        # A person of no organization (memberships mode) acts for nobody: any organization data
        # a route reaches through tenancy is refused, never read for all of them or `default`.
        who = principal if principal is not None and principal.kind != "open" else None
        acting = (
            tenancy.acting_for_nobody(who.actor)
            if who is not None and who.org is None
            else tenancy.acting_for(who.org if who else None, who.actor if who else None)
        )
        with acting:
            response: Response = await call_next(request)
        return response

    csp = content_security_policy(page)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next: Any) -> Response:
        """Outside the guard, so refusals from the guard get the headers too. Every response gets
        a CSP and COEP: a page sets its own policy (computed from its HTML), anything else gets
        the dashboard's. Any host but this machine also gets HSTS."""
        response: Response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        path = request.url.path
        if path.startswith("/api/"):  # data, often an organization's: never stored anywhere
            response.headers["Cache-Control"] = "no-store"
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
    )

    add_auth_routes(app, auth)
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

    # The quickstart's decision is automated only where the inbox runs what its policy requires.
    quickstart = packs.get(landing.QUICKSTART_PACK)
    running = {} if quickstart is None else variants_for(quickstart)[0]
    quickstart_automates = quickstart is not None and all(
        running.get(slot, quickstart.manifest.providers[slot].use) in allowed
        for slot, allowed in quickstart.manifest.policy.automation_requires.items()
    )
    landing_page = landing.page(
        versioned_page("docs.html"),
        version=__version__,
        # With sign-in on, what runs here is not public: the page shows both answers.
        quickstart_automates=None if auth.enabled else quickstart_automates,
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
    if not _FHIR_ROUTE.match(path):
        said: dict[str, Any] = {"error": type(error).__name__, "detail": str(error)}
        if isinstance(error, InvitationRefused):  # which refusal, for the /invite page
            said["reason"] = error.reason
        return JSONResponse(identified(said), status, headers=headers)
    if isinstance(error, FhirStructureError):
        return operation_outcome(status, [{"code": "structure", **i} for i in error.issues])
    code = _ISSUE_TYPE.get(status, "processing")
    if isinstance(error, PackNotFoundError):
        code = "not-found"
    return operation_outcome(status, [{"code": code, "diagnostics": str(error)}], headers)


quota_logger = logging.getLogger("mendari.limits")
phi_logger = logging.getLogger("mendari.phi")
providers_logger = logging.getLogger("mendari.providers")


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


def operators_hold(hold: Mapping[str, Any]) -> dict[str, Any]:
    """A hold of the deployment's (set from the command line, for every organization) as an
    organization sees it in memberships mode: what it covers and since when; the operator's
    reason and name are the operator's (ADR-0008)."""
    return {key: hold[key] for key in ("id", "org", "pack_id", "pack_digest", "set_at")}


def quota_alert(webhook: str | None) -> QuotaReached:
    """What happens the first time an organization reaches a daily quota (once a day, whichever
    instance counts that call): a `quota.reached` line in the security log (the limit as its
    `count`) and, with `MENDARI_ALERT_WEBHOOK`, one post
    with the organization, the metric and the limit (no case data). Never fails the decision:
    a webhook that is down loses the alert, not the request."""

    def alert(org: str, metric: str, limit: int) -> None:
        security_log.event("quota.reached", outcome="limited", reason=metric, org=org, count=limit)
        if webhook is None:
            return
        body = {
            "source": "mendari",
            "organization": org,
            "alerts": [
                {
                    "severity": "warning",
                    "kind": "quota_reached",
                    "message": f"daily {metric} quota of {limit} reached; until midnight, "
                    "decisions fall back to human review",
                }
            ],
        }
        try:
            httpx.post(webhook, json=body, timeout=5, follow_redirects=False).raise_for_status()
        except httpx.HTTPError as failure:
            quota_logger.warning("the quota alert webhook failed (%s)", type(failure).__name__)

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


class BodyCap:
    """No request body over `limit` bytes (MENDARI_MAX_BODY_BYTES), whatever the route. It reads
    nothing itself: a declared length over the limit is refused before anything is read, and
    the bytes a route reads, chunked or not, are counted as they arrive and stop with a `413`
    once they pass it. It runs inside the guard, so sign-in answers first and no body is read
    before the sign-in check."""

    def __init__(self, app: ASGIApp, limit: int) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > self.limit:
            await self.too_large(scope["path"])(scope, receive, send)
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
        if _FHIR_ROUTE.match(path):
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
