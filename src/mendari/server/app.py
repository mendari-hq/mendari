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
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

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
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from mendari import __version__, guard, settings
from mendari.core import clock, tenancy
from mendari.core.audit import AuditStore
from mendari.core.card import benchmark, render_card
from mendari.core.decision import CallBudget, Decision
from mendari.core.pack import Pack
from mendari.core.phi import phi_policy
from mendari.core.policy import GovernanceOverride
from mendari.core.registry import discover, version_key
from mendari.errors import (
    AuthError,
    FhirError,
    FhirStructureError,
    MendariError,
    PackNotFoundError,
    PackValidationError,
    ProviderError,
    ReviewError,
    SmartError,
)
from mendari.evals import compare, evaluate, leaderboard, load_dataset, resolve_dataset
from mendari.evals.feedback import feedback_dataset
from mendari.evals.harness import Dataset
from mendari.evals.monitoring import monitor
from mendari.evals.pack_tests import load_cases
from mendari.providers import jev, llm
from mendari.providers.credentials import key_source
from mendari.server import api_docs
from mendari.server.auth import Auth, add_auth_routes, auth_settings, permission_for
from mendari.server.smart_routes import add_smart_routes, error_page
from mendari.settings import FhirValidation, providers_for
from mendari.standards import audit_fhir, cds_hooks, davinci, pas
from mendari.standards.fhir_client import GuardedHttp, allowed_servers
from mendari.standards.pas import PasError
from mendari.standards.smart import smart_settings

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
    final_label: str = Field(min_length=1)
    reviewer: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=2000)


class OutcomeRequest(BaseModel):
    final_label: str = Field(min_length=1)
    source: str = Field(min_length=1, max_length=200)


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
    phi = phi_policy()  # a bad PHI setting (or a missing key) fails here, at startup
    store = AuditStore(db_path, migrate_url=migrate_url, phi=phi)
    auth = Auth(sign_in, store)
    budget = CallBudget(store, budget_limits) if budget_limits else None
    found = discover(packs_paths)
    only = settings.enabled_packs()  # a per-pack image serves just its pack
    missing = only - {pack.id for pack in found.packs}
    if missing:
        raise MendariError(f"MENDARI_PACKS: packs not installed here: {sorted(missing)}")
    packs: dict[str, Pack] = {}
    for pack in found.packs:
        if only and pack.id not in only:
            continue
        current = packs.get(pack.id)
        if current is None or version_key(pack.version) > version_key(current.version):
            packs[pack.id] = pack
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
        description=api_docs.description(sign_in=auth.enabled, default_pack=default_pack),
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
    def health() -> dict[str, Any]:
        """Version, packs, storage, the FHIR validation mode, sign-in, PHI mode and which provider
        answers each pack's questions. Public, for load balancers."""
        return {
            "status": "ok",
            "version": __version__,
            "packs": len(packs),
            "invalid": found.problems,
            "storage": store.backend,
            # Calendar days and displayed times use this zone; stored timestamps are UTC.
            "timezone": timezone,
            "fhir_validation": validation,
            # FHIR servers this deployment may read from (CDS Hooks fetch); empty: none.
            "fhir_allowed_servers": fhir_servers,
            "smart_launch": smart_options is not None,
            "sign_in": auth.enabled,
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

        Inputs are checked against US Core 6.1.0 (`fhir_validation` in `/api/health`: `warn`
        records issues in `decision.input_issues`, `enforce` blocks). An element of the wrong JSON
        type is a `400`. An error on this route is an `OperationOutcome`."""
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
        """Record a reviewer's final label for a decision in the queue."""
        return store.resolve_review(
            decision_id,
            final_label=request.final_label,
            reviewer=acting_as(http, request.reviewer) or "",
            note=request.note,
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
    def record_outcome(decision_id: str, request: OutcomeRequest) -> dict[str, str]:
        """Record the final label from a downstream system (e.g. where the message was really
        handled), for monitoring."""
        store.record_outcome(decision_id, final_label=request.final_label, source=request.source)
        return {"id": decision_id, "final_label": request.final_label}

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

    # -- dashboard ---------------------------------------------------------------------------

    page = versioned_dashboard()

    @app.middleware("http")
    async def _guard(request: Request, call_next: Any) -> Response:
        """Every request passes the sign-in guard (a no-op in open mode)."""
        try:
            refused = auth.check(request)
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
        if not request.url.path.startswith(("/docs", "/redoc")):
            response.headers.setdefault("Content-Security-Policy", csp)
            response.headers.setdefault("Cross-Origin-Embedder-Policy", "require-corp")
        return response

    add_auth_routes(app, auth)
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


def error_response(path: str, status: int, error: MendariError) -> JSONResponse:
    """`{"error", "detail"}` on Mendari's own API; an OperationOutcome on FHIR routes."""
    if not _FHIR_ROUTE.match(path):
        return JSONResponse({"error": type(error).__name__, "detail": str(error)}, status)
    if isinstance(error, FhirStructureError):
        return operation_outcome(status, [{"code": "structure", **i} for i in error.issues])
    code = _ISSUE_TYPE.get(status, "processing")
    if isinstance(error, PackNotFoundError):
        code = "not-found"
    return operation_outcome(status, [{"code": code, "diagnostics": str(error)}])


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
