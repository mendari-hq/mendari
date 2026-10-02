"""HTTP surface: REST decisions, FHIR in/out, CDS Hooks, review queue, audit, evals, dashboard.

v0.1 has no authentication of its own and is meant for synthetic data only. Locally it binds
to localhost; on Vercel it sits behind Vercel Authentication (deployment protection).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
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

from mendari import __version__, guard, settings
from mendari.core import clock, tenancy
from mendari.core.audit import SYSTEM_ID, AuditStore
from mendari.core.card import benchmark, render_card
from mendari.core.decision import CallBudget, Decision
from mendari.core.pack import Pack
from mendari.core.phi import phi_policy
from mendari.core.policy import GovernanceOverride
from mendari.core.registry import discover, version_key
from mendari.errors import (
    AuthError,
    ClientError,
    FhirError,
    FhirStructureError,
    MendariError,
    PackNotFoundError,
    PackValidationError,
    ProviderError,
    ReviewError,
    ScopeError,
    SmartError,
)
from mendari.evals import compare, evaluate, leaderboard, load_dataset, resolve_dataset
from mendari.evals.feedback import feedback_dataset
from mendari.evals.harness import Dataset
from mendari.evals.monitoring import monitor
from mendari.evals.pack_tests import load_cases
from mendari.legacy import current_pack_id
from mendari.providers import jev, llm
from mendari.providers.credentials import key_source
from mendari.server import api_docs
from mendari.server.auth import (
    OPEN,
    Auth,
    Principal,
    add_auth_routes,
    auth_settings,
    challenge,
    credential_in_url_or_form,
    names_a_credential,
    permission_for,
    read_body,
)
from mendari.server.backend_services import (
    DISCOVERY_PATH,
    add_backend_routes,
    backend_settings,
)
from mendari.server.smart_routes import add_smart_routes, error_page
from mendari.settings import FhirValidation, providers_for
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
from mendari.standards.search import SPECS, SearchError, handling, searchset
from mendari.standards.search import parse as parse_search
from mendari.standards.smart import smart_settings
from mendari.standards.terminology import Terminology

DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "dashboard"
INBOX = "mendari/patient-inbox-router"
JsonBody = Annotated[dict[str, Any], Body()]
_DATASET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_STATUS = {
    PackNotFoundError: 404,
    ReviewError: 409,
    FhirStructureError: 400,  # before FhirError: the first match wins
    FhirError: 422,
    PackValidationError: 422,
    PasError: 422,
    ScopeError: 422,
    ClientError: 422,
}
# A provider that cannot answer (e.g. listing Jev's models): not set up here, or the upstream
# service failed; never a client error.
_PROVIDER_STATUS = {
    "not_configured": 503,
    "credential_unavailable": 503,
    "insufficient_credit": 503,
    "budget_exhausted": 429,
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
    approver: str | None = Field(default=None, max_length=200, description="open mode only")


class ReviewRequest(BaseModel):
    final_label: str = Field(min_length=1, max_length=200)
    reviewer: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=2000)


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
    reporter: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=1000)
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


# Evaluations that call a metered, network-bound provider must fit one function invocation.
REMOTE_EVAL_LIMIT = 200
REMOTE_EVAL_WORKERS = 8


def acting_as(request: Request, claimed: str | None) -> str | None:
    """Who a review or rating is recorded for: with sign-in on, the signed-in person or API key,
    whatever the request body says; in open mode, the name the client gives."""
    principal = getattr(request.state, "principal", None)
    return principal.name if principal is not None and principal.kind != "open" else claimed


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
    return str(principal.name)


def create_app(
    *,
    db_path: str | Path = "var/mendari.db",
    migrate_url: str | None = None,
    providers: Mapping[str, str] | None = None,
    datasets_dir: Path | None = None,
    packs_paths: list[Path] | None = None,
    budget_limits: Mapping[str, int | None] | None = None,
    fhir_validation: FhirValidation | None = None,
) -> FastAPI:
    timezone = clock.zone().key  # an invalid MENDARI_TIMEZONE fails here, at startup
    validation = fhir_validation or settings.fhir_validation()  # likewise
    fhir_servers = sorted(allowed_servers())  # likewise
    smart_options = smart_settings()
    sign_in = auth_settings()  # an invalid sign-in setting fails here, at startup
    backend = backend_settings(sign_in)  # likewise: on, it needs sign-in and a public URL
    phi = phi_policy()  # a bad PHI setting (or a missing key) fails here, at startup
    body_limit = settings.max_body_bytes()  # likewise
    store = AuditStore(db_path, migrate_url=migrate_url, phi=phi)
    auth = Auth(sign_in, store, tokens=backend is not None)
    budget = CallBudget(store, budget_limits) if budget_limits else None
    found = discover(packs_paths)
    only = settings.enabled_packs()  # a per-pack image serves just its pack
    missing = only - {pack.id for pack in found.packs}
    if missing:
        raise MendariError(f"MENDARI_PACKS: packs not installed here: {sorted(missing)}")
    installed: dict[str, Pack] = {}  # the newest version of every pack found, served or not
    for pack in found.packs:
        current = installed.get(pack.id)
        if current is None or version_key(pack.version) > version_key(current.version):
            installed[pack.id] = pack
    packs = {pack_id: pack for pack_id, pack in installed.items() if not only or pack_id in only}
    # Every CodeSystem and NamingSystem the served packs need. A pack whose terminology cannot
    # be built is reported in /api/health (`invalid`) and served without it.
    terms, terminology_problems = Terminology.serving(packs.values())
    decisions: dict[str, Decision] = {}
    overrides = dict(providers or {})

    def pack_or_404(pack_id: str) -> Pack:
        pack = packs.get(pack_id)
        if pack is None:
            raise PackNotFoundError(f"no pack {pack_id!r}")
        return pack

    skipped_settings: dict[str, list[str]] = {}

    def decision_for(pack_id: str) -> Decision:
        if pack_id not in decisions:
            pack = pack_or_404(pack_id)
            slots = {slot: set(spec.variants) for slot, spec in pack.manifest.providers.items()}
            chosen, skipped = providers_for(pack.id, slots, overrides)
            if skipped:
                skipped_settings[pack_id] = skipped
            decision = Decision(
                pack,
                providers=chosen,
                store=store,
                budget=budget,
                fhir_validation=validation,
                governance_source=approved_override,
            )
            environment, release = settings.deployment_place()
            store.record_deployment(
                environment=environment,
                release=release,
                pack_id=pack.id,
                pack_version=pack.version,
                pack_digest=pack.digest,
                providers={slot: info.variant for slot, info in decision.provider_info.items()},
            )
            decisions[pack_id] = decision
        return decisions[pack_id]

    def question_labels(pack_id: str, question: str) -> list[str] | None:
        """An installed pack's labels for one of its questions (None: not installed, or no such
        question), for decisions whose record has no answer to check a label against. Installed,
        not only served: a per-pack instance can share a database with other packs' decisions,
        and decisions from before the rename carry the old pack id."""
        pack = installed.get(current_pack_id(pack_id))
        if pack is None or question not in pack.manifest.questions:
            return None
        return pack.manifest.labels(question)

    def approved_override(pack: Pack) -> GovernanceOverride | None:
        """The organization's approval in force for exactly this pack, as the runtime override."""
        approval = store.active_approval(pack.id, pack.digest)
        if approval is None:
            return None
        return GovernanceOverride(approved_by=approval["approver"], reference=approval["reference"])

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

    default_pack = INBOX if INBOX in packs else next(iter(sorted(packs)), None)
    app = FastAPI(
        title="Mendari",
        version=__version__,
        summary="Run, review, audit and evaluate healthcare decisions.",
        description=api_docs.description(
            sign_in=auth.enabled,
            default_pack=default_pack,
            token_url=None if backend is None else backend.token_url,
        ),
        openapi_tags=api_docs.TAGS,
    )

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
                permission_for=permission_for if auth.enabled else None,
                token_url=None if backend is None else backend.token_url,
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
            return error_page(str(error), status)
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
        return JSONResponse({"detail": problems}, 422)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, error: StarletteHTTPException) -> Response:
        if _FHIR_ROUTE.match(request.url.path):
            code = _ISSUE_TYPE.get(error.status_code, "processing")
            issue = {"code": code, "diagnostics": str(error.detail)}
            return operation_outcome(error.status_code, [issue], error.headers)
        return JSONResponse({"detail": error.detail}, error.status_code, error.headers)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, error: Exception) -> Response:
        """A bug: a JSON 500 that says nothing about the input (the traceback goes to the log).
        It is answered outside the middleware, so it sets the security headers itself."""
        if _FHIR_ROUTE.match(request.url.path):
            issue = {"code": "exception", "diagnostics": "internal error"}
            return operation_outcome(500, [issue], SECURITY_HEADERS)
        body = {"error": "InternalError", "detail": "internal error"}
        return JSONResponse(body, 500, headers=SECURITY_HEADERS)

    # -- packs -------------------------------------------------------------------------------

    @app.get(
        "/api/health",
        tags=["Service"],
        summary="Deployment health and settings",
    )
    def health(request: Request) -> dict[str, Any]:
        """Public, for load balancers: `status`, `version` and `sign_in`, and nothing else
        without a credential (no database work either). A principal that may `read` (everyone
        in open mode) also gets the deployment's settings: packs, storage, the FHIR validation
        mode, PHI mode, which provider answers each pack's questions and today's remote calls.
        """
        public = {"status": "ok", "version": __version__, "sign_in": auth.enabled}
        principal = auth.principal(request)  # no credential: no lookup
        if principal is None or not principal.may("read"):
            return public
        return {
            **public,
            "packs": len(packs),
            "invalid": {**found.problems, **terminology_problems},
            "storage": store.backend,
            # Calendar days and displayed times use this zone; stored timestamps are UTC.
            "timezone": timezone,
            "fhir_validation": validation,
            # FHIR servers this deployment may read from (CDS Hooks fetch); empty: none.
            "fhir_allowed_servers": fhir_servers,
            "smart_launch": smart_options is not None,
            "phi_mode": phi.mode,
            "providers": {
                pack_id: {
                    slot: info.variant for slot, info in decision_for(pack_id).provider_info.items()
                }
                for pack_id in sorted(packs)
            },
            # Deployment provider settings that name a slot or variant a pack does not have.
            "provider_settings_skipped": skipped_settings,
            "jev": {
                "credentials": key_source(key_env="JEV_API_KEY", connector_env="JEV_CONNECTOR"),
                "endpoint_configured": bool(os.environ.get("JEV_API_URL", "").strip()),
                "model": os.environ.get("JEV_MODEL", "").strip() or None,
                "calls_today": store.usage("jev"),
                "daily_limit": (budget_limits or {}).get("jev"),
            },
            "llm": {
                **llm.deployment_info(),
                "calls_today": store.usage("llm"),
                "daily_limit": (budget_limits or {}).get("llm"),
            },
        }

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
        """Every Decision Pack this deployment serves: id, version, risk level and the labels of its
        main question."""
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
            "input_schema": pack.input_schema,
            "output_schema": pack.output_schema,
            "benchmark": benchmark(pack),
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
        """Synthetic example inputs: the pack's own behaviour tests."""
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
        `outcome`, not only the status code."""
        return decision_for(f"{namespace}/{name}").decide(payload).to_dict()

    @app.post(
        "/api/packs/{namespace}/{name}/guard",
        tags=["Decisions"],
        summary="Check an LLM's draft reply",
    )
    def guard_draft(namespace: str, name: str, payload: JsonBody) -> dict[str, Any]:
        """Check an LLM's draft reply: `released` (send `reply`), `held_for_review` or
        `blocked` (send nothing; follow `fallback`). Audited like any decision."""
        return guard.check(decision_for(f"{namespace}/{name}"), payload).to_dict()

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
        return {"decision": result.to_dict(), "task": task}

    # -- monitoring ------------------------------------------------------------------------------

    @app.get(
        "/api/monitoring",
        tags=["Evaluation and monitoring"],
        summary="Monitor a pack over time",
    )
    def monitoring(pack_id: str, days: int = 30) -> dict[str, Any]:
        """The pack over time for this organisation: weekly series, drift and alerts."""
        return monitor(store, pack_or_404(pack_id), days=max(7, min(days, 180)))

    # -- governance ------------------------------------------------------------------------------

    @app.get(
        "/api/deployments",
        tags=["Evaluation and monitoring"],
        summary="List deployments",
    )
    def deployments(pack_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Which pack version runs where, with which providers, since when (newest first)."""
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
        principal = getattr(request.state, "principal", None)
        approver = (
            body.approver if principal is None or principal.kind == "open" else principal.name
        )
        expires = datetime.now(UTC) + timedelta(days=body.days)
        return store.approve_automation(
            pack_id=pack.id,
            pack_digest=pack.digest,
            risk_level=int(pack.manifest.risk.level),
            approver=approver or "",
            reference=body.reference,
            expires_at=expires.isoformat(timespec="seconds"),
        )

    @app.delete(
        "/api/governance/{approval_id}",
        status_code=204,
        tags=["Governance"],
        summary="Revoke an approval",
    )
    def revoke(request: Request, approval_id: str) -> Response:
        """Revoke an approval; the pack's automation falls back to its risk-level cap."""
        principal = getattr(request.state, "principal", None)
        name = principal.name if principal is not None else "open mode"
        if not store.revoke_approval(approval_id, revoked_by=name):
            raise HTTPException(404, "no such approval")
        return Response(status_code=204)

    # -- evaluation --------------------------------------------------------------------------

    @app.post(
        "/api/packs/{namespace}/{name}/evals",
        tags=["Evaluation and monitoring"],
        summary="Run an evaluation",
    )
    def run_eval(namespace: str, name: str, request: EvalRequest) -> dict[str, Any]:
        """Evaluate the pack (or another provider configuration) on one of its datasets, and compare
        with the published benchmark. Calls to remote providers are capped at 200 cases."""
        pack_id = f"{namespace}/{name}"
        pack = pack_or_404(pack_id)
        # A store-less Decision: evaluation traffic must not pollute the operational audit log.
        active = decision_for(pack_id)
        chosen = {slot: info.variant for slot, info in active.provider_info.items()}
        decision = Decision(pack, providers={**chosen, **(request.providers or {})}, budget=budget)
        dataset = dataset_for(pack, request.dataset)
        if decision.is_remote:
            dataset = dataset.head(min(request.limit or REMOTE_EVAL_LIMIT, REMOTE_EVAL_LIMIT))
        elif request.limit:
            dataset = dataset.head(request.limit)
        workers = REMOTE_EVAL_WORKERS if decision.is_remote else 1
        report = evaluate(decision, dataset, workers=workers)
        baseline = benchmark(pack)
        if baseline is not None and request.dataset != pack.manifest.evaluation.benchmark_dataset:
            report["comparison"] = compare(
                report, baseline, pack.manifest.evaluation.degradation_tolerance
            )
        store.save_report(report)
        return report

    @app.get(
        "/api/evals",
        tags=["Evaluation and monitoring"],
        summary="List evaluation reports",
    )
    def list_evals(pack_id: str | None = None) -> list[dict[str, Any]]:
        """Saved evaluation reports, newest first."""
        return store.reports(pack_id)

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
        return store.pending_reviews(pack_id, limit=min(limit, 500), reader=input_reader(request))

    @app.post(
        "/api/review/{decision_id}",
        tags=["Review and feedback"],
        summary="Resolve a review",
    )
    def resolve(decision_id: str, request: ReviewRequest, http: Request) -> dict[str, Any]:
        """Record a reviewer's final label for a decision in the queue: one of the labels of the
        decision's question."""
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
        """The audit trail, newest first, with filters and `limit`/`offset` paging."""
        return store.query(
            pack_id=pack_id,
            outcome=outcome,
            review_status=review_status,
            limit=min(limit, 500),
            offset=max(offset, 0),
        )

    @app.get(
        "/api/audit/verify",
        tags=["Audit"],
        summary="Verify the audit chain",
    )
    def verify() -> dict[str, Any]:
        """Recompute the hash chain over every event; `ok: false` means the trail was altered."""
        return store.verify()

    @app.get(
        "/api/audit/export/fhir",
        tags=["FHIR and CDS Hooks"],
        summary="Export the audit trail as FHIR",
    )
    def export_audit_fhir(
        pack_id: str | None = None, since: str | None = None, until: str | None = None
    ) -> StreamingResponse:
        """The audit trail as FHIR NDJSON (AuditEvent and Provenance, ids and codes only),
        streamed in chain order; `since` / `until` bound the event time (ISO 8601, UTC)."""
        lines = (
            json.dumps(resource, separators=(",", ":")) + "\n"
            for resource in audit_fhir.resources(
                store.iter_events(pack_id=pack_id, since=since, until=until)
            )
        )
        return StreamingResponse(lines, media_type="application/fhir+ndjson")

    @app.get(
        "/api/audit/{decision_id}",
        tags=["Audit"],
        summary="Get a decision",
    )
    def audit_record(request: Request, decision_id: str) -> dict[str, Any]:
        """One decision with every event recorded on it."""
        record = store.get(decision_id, reader=input_reader(request))
        if record is None:
            raise HTTPException(404, f"unknown decision {decision_id}")
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
        "/api/audit/{decision_id}/outcome",
        tags=["Review and feedback"],
        summary="Record what really happened",
    )
    def record_outcome(decision_id: str, request: OutcomeRequest, http: Request) -> dict[str, str]:
        """Record the final label from a downstream system (e.g. where the message was really
        handled), for monitoring: one of the labels of the decision's question. `source` names
        that system as an identifier; who recorded it is the caller (the signed-in person or API
        key), whatever the body says."""
        recorded_by = principal_of(http).name
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
        """Thumbs up or down, and on a thumbs-down the right answer."""
        return store.record_feedback(
            decision_id,
            rating=request.rating,
            reporter=acting_as(http, request.reporter),
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
        """Ratings, newest first."""
        return store.feedback(
            pack_id=pack_id, rating=rating, limit=min(limit, 500), reader=input_reader(request)
        )

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
        """Counts by outcome, the review backlog, override rate, latency and cost."""
        return store.stats(pack_id)

    # -- CDS Hooks ---------------------------------------------------------------------------

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
    def cds_invoke(service_id: str, request: JsonBody) -> dict[str, Any]:
        """A CDS Hooks request (context and prefetch) → cards with the decision and its reasons."""
        pack = next((p for p in packs.values() if cds_hooks.service_id(p) == service_id), None)
        if pack is None:
            raise PackNotFoundError(f"no CDS service {service_id!r}")
        return cds_hooks.invoke(decision_for(pack.id), request)

    @app.post(
        "/api/packs/{namespace}/{name}/pas",
        tags=["FHIR and CDS Hooks"],
        summary="Da Vinci PAS request",
    )
    def prior_authorization(
        namespace: str, name: str, body: PriorAuthorizationRequest, http: Request
    ) -> dict[str, Any]:
        """Da Vinci PAS: the request Bundle for a complete request (`submit_ready`), and with
        `submit`, the payer's answer, which is chained on the decision (`pas.submitted`)."""
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
            payer = settings.pas_payer_url()
            with GuardedHttp() as client:
                submitted = pas.submit(bundle, payer, client)
            store.record_event(
                record["id"],
                "pas.submitted",
                {**submitted, "requested_by": acting_as(http, None) or "open mode"},
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

    page = versioned_dashboard()

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

    @app.middleware("http")
    async def _guard(request: Request, call_next: Any) -> Response:
        """Every request passes the sign-in guard (a no-op in open mode). A credential in a URL
        is refused first, in every mode: it would end up in logs and histories. No body is read
        here: the routes that read a form refuse a credential in it, after this check."""
        try:
            if names_a_credential(request.query_params):
                raise credential_in_url_or_form()
            # A credential lookup is a database read: in the thread pool, as sync routes are,
            # so a slow database never stalls the event loop.
            refused = await run_in_threadpool(auth.check, request)
        except AuthError as error:
            return error_response(request.url.path, error.status, error)
        if refused is not None:
            return refused
        principal = getattr(request.state, "principal", None)
        with tenancy.acting_for(principal.org if principal and principal.kind != "open" else None):
            response: Response = await call_next(request)
        return response

    csp = content_security_policy(page)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next: Any) -> Response:
        """Outermost, so refusals from the guard get the headers too. The API docs pages load
        Swagger UI and ReDoc from a CDN, so they keep no CSP."""
        response: Response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        path = request.url.path
        if path.startswith("/api/"):  # data, often an organization's: never stored anywhere
            response.headers["Cache-Control"] = "no-store"
        api = path.startswith(("/api/", "/fhir/")) or path == "/fhir"
        if api and response.status_code in (401, 403):  # RFC 7235 and 6750: say how to sign in
            response.headers.setdefault("WWW-Authenticate", challenge())
        if not request.url.path.startswith(("/docs", "/redoc")):
            response.headers.setdefault("Content-Security-Policy", csp)
            response.headers.setdefault("Cross-Origin-Embedder-Policy", "require-corp")
        return response

    add_auth_routes(app, auth)
    if backend is not None:
        add_backend_routes(app, backend, store)
    add_smart_routes(
        app,
        settings=smart_options,
        store=store,
        pack_or_404=pack_or_404,
        decision_for=decision_for,
        default_pack=default_pack,
    )

    @app.get("/", include_in_schema=False)
    def dashboard() -> HTMLResponse:
        return HTMLResponse(page, headers={"cache-control": "no-cache"})

    # Browsers ask for /favicon.ico on any page without an icon link (e.g. raw JSON responses).
    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(
            DASHBOARD_DIR / "favicon.ico",
            media_type="image/x-icon",
            headers={"cache-control": "public, max-age=86400"},
        )

    app.mount("/static", StaticFiles(directory=DASHBOARD_DIR), name="static")
    return app


def mounted_routes(app: FastAPI) -> list[tuple[str, str]]:
    """(method, path) of every route the app serves."""
    return [
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
    ]


def error_response(path: str, status: int, error: MendariError) -> JSONResponse:
    """`{"error", "detail"}` on Mendari's own API; an OperationOutcome on FHIR routes. A
    sign-in refusal carries its `WWW-Authenticate` challenge."""
    asked = error.challenge if isinstance(error, AuthError) else None
    headers = {"WWW-Authenticate": asked} if asked else None
    if not _FHIR_ROUTE.match(path):
        body = {"error": type(error).__name__, "detail": str(error)}
        return JSONResponse(body, status, headers=headers)
    if isinstance(error, FhirStructureError):
        return operation_outcome(status, [{"code": "structure", **i} for i in error.issues])
    code = _ISSUE_TYPE.get(status, "processing")
    if isinstance(error, PackNotFoundError):
        code = "not-found"
    return operation_outcome(status, [{"code": code, "diagnostics": str(error)}], headers)


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
        return JSONResponse({"detail": self.detail}, 413)


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


def content_security_policy(page: str) -> str:
    """The dashboard needs nothing but its own origin. Its two inline scripts (the theme script
    that runs before first paint, and the import map) are allowed by hash, not 'unsafe-inline';
    style attributes (data-driven bar widths) need 'unsafe-inline' for styles."""
    inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", page, re.S)
    hashes = " ".join(
        f"'sha256-{base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()}'"
        for script in inline
    )
    return "; ".join(
        [
            "default-src 'self'",
            f"script-src 'self' {hashes}".strip(),
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            "connect-src 'self'",
            "font-src 'self'",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        ]
    )


def versioned_dashboard() -> str:
    """index.html with each asset URL stamped by a hash of its content, so a deploy that
    changes a module or the stylesheet can never be served stale from a browser or CDN cache.

    Modules import each other by plain URL ("./ui.js"); the import map sends each one to its
    stamped URL, and a modulepreload link per module fetches the whole graph in parallel."""
    page = (DASHBOARD_DIR / "index.html").read_text(encoding="utf-8")
    stamped = {
        f"/static/{path.name}": f"/static/{path.name}?v={_digest(path)}"
        for path in sorted(DASHBOARD_DIR.glob("*.js"))
    }
    preload = "".join(f'\n  <link rel="modulepreload" href="{url}">' for url in stamped.values())
    import_map = json.dumps({"imports": stamped})
    page = page.replace(
        '<script type="importmap"></script>',
        f'<script type="importmap">{import_map}</script>{preload}',
    )
    page = page.replace('src="/static/main.js"', f'src="{stamped["/static/main.js"]}"')
    style = f"/static/style.css?v={_digest(DASHBOARD_DIR / 'style.css')}"
    return page.replace("/static/style.css", style)


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
        "publisher": m.publisher.model_dump(mode="json"),
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
