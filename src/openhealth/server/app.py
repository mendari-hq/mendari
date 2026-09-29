"""HTTP surface: REST decisions, FHIR in/out, CDS Hooks, review queue, audit, evals, dashboard.

v0.1 has no authentication of its own and is meant for synthetic data only. Locally it binds
to localhost; on Vercel it sits behind Vercel Authentication (deployment protection).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from openhealth import __version__
from openhealth.core.audit import AuditStore
from openhealth.core.card import benchmark, render_card
from openhealth.core.decision import CallBudget, Decision
from openhealth.core.pack import Pack
from openhealth.core.registry import discover, version_key
from openhealth.errors import (
    FhirError,
    OpenHealthError,
    PackNotFoundError,
    PackValidationError,
    ReviewError,
)
from openhealth.evals import compare, evaluate, leaderboard, load_dataset, resolve_dataset
from openhealth.evals.feedback import feedback_dataset
from openhealth.evals.harness import Dataset
from openhealth.providers import jev, llm
from openhealth.providers.credentials import key_source
from openhealth.settings import providers_for
from openhealth.standards import cds_hooks

DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "dashboard"
JsonBody = Annotated[dict[str, Any], Body()]
_DATASET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_STATUS = {
    PackNotFoundError: 404,
    ReviewError: 409,
    FhirError: 422,
    PackValidationError: 422,
}


class ReviewRequest(BaseModel):
    final_label: str = Field(min_length=1)
    reviewer: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=2000)


class OutcomeRequest(BaseModel):
    final_label: str = Field(min_length=1)
    source: str = Field(min_length=1, max_length=200)


class FeedbackRequest(BaseModel):
    rating: Literal["up", "down"]
    reporter: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=1000)


class EvalRequest(BaseModel):
    dataset: str = Field(min_length=1, max_length=200)
    limit: int | None = Field(default=None, ge=1)
    # Evaluate another configuration than the deployed one (e.g. for the leaderboard); the
    # remote-call cap and daily budgets still apply.
    providers: dict[str, str] | None = None


# Evaluations that call a metered, network-bound provider must fit one function invocation.
REMOTE_EVAL_LIMIT = 200
REMOTE_EVAL_WORKERS = 8


def create_app(
    *,
    db_path: str | Path = "var/openhealth.db",
    migrate_url: str | None = None,
    providers: Mapping[str, str] | None = None,
    datasets_dir: Path | None = None,
    packs_paths: list[Path] | None = None,
    budget_limits: Mapping[str, int] | None = None,
) -> FastAPI:
    store = AuditStore(db_path, migrate_url=migrate_url)
    budget = CallBudget(store, budget_limits) if budget_limits else None
    found = discover(packs_paths)
    packs: dict[str, Pack] = {}
    for pack in found.packs:
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
            decisions[pack_id] = Decision(pack, providers=chosen, store=store, budget=budget)
        return decisions[pack_id]

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

    app = FastAPI(
        title="OpenHealth Hub",
        version=__version__,
        description="Decision Packs: run, review, audit and evaluate healthcare decisions.",
    )

    @app.exception_handler(OpenHealthError)
    async def _error(_: Request, error: OpenHealthError) -> JSONResponse:
        status = next((code for kind, code in _STATUS.items() if isinstance(error, kind)), 400)
        return JSONResponse({"error": type(error).__name__, "detail": str(error)}, status)

    # -- packs -------------------------------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "packs": len(packs),
            "invalid": found.problems,
            "storage": store.backend,
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

    @app.get("/api/providers/jev/models")
    def jev_models() -> dict[str, Any]:
        return {"models": jev.list_models()}

    @app.get("/api/packs")
    def list_packs() -> list[dict[str, Any]]:
        return [_summary(pack) for pack in sorted(packs.values(), key=lambda p: p.id)]

    @app.get("/api/packs/{namespace}/{name}")
    def pack_detail(namespace: str, name: str) -> dict[str, Any]:
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

    @app.post("/api/packs/{namespace}/{name}/decide")
    def decide(namespace: str, name: str, payload: JsonBody) -> dict[str, Any]:
        return decision_for(f"{namespace}/{name}").decide(payload).to_dict()

    @app.post("/api/packs/{namespace}/{name}/fhir/decide")
    def decide_fhir(namespace: str, name: str, payload: JsonBody) -> dict[str, Any]:
        result, task = decision_for(f"{namespace}/{name}").decide_fhir(payload)
        return {"decision": result.to_dict(), "task": task}

    # -- evaluation --------------------------------------------------------------------------

    @app.post("/api/packs/{namespace}/{name}/evals")
    def run_eval(namespace: str, name: str, request: EvalRequest) -> dict[str, Any]:
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

    @app.get("/api/evals")
    def list_evals(pack_id: str | None = None) -> list[dict[str, Any]]:
        return store.reports(pack_id)

    # -- review & audit ----------------------------------------------------------------------

    @app.get("/api/review")
    def review_queue(pack_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return store.pending_reviews(pack_id, limit=min(limit, 500))

    @app.post("/api/review/{decision_id}")
    def resolve(decision_id: str, request: ReviewRequest) -> dict[str, Any]:
        return store.resolve_review(
            decision_id,
            final_label=request.final_label,
            reviewer=request.reviewer,
            note=request.note,
        )

    @app.get("/api/audit")
    def audit_log(
        pack_id: str | None = None,
        outcome: str | None = None,
        review_status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return store.query(
            pack_id=pack_id,
            outcome=outcome,
            review_status=review_status,
            limit=min(limit, 500),
            offset=max(offset, 0),
        )

    @app.get("/api/audit/verify")
    def verify() -> dict[str, Any]:
        return store.verify()

    @app.get("/api/audit/{decision_id}")
    def audit_record(decision_id: str) -> dict[str, Any]:
        record = store.get(decision_id)
        if record is None:
            raise HTTPException(404, f"unknown decision {decision_id}")
        return record

    @app.post("/api/audit/{decision_id}/outcome")
    def record_outcome(decision_id: str, request: OutcomeRequest) -> dict[str, str]:
        store.record_outcome(decision_id, final_label=request.final_label, source=request.source)
        return {"id": decision_id, "final_label": request.final_label}

    @app.post("/api/audit/{decision_id}/feedback")
    def give_feedback(decision_id: str, request: FeedbackRequest) -> dict[str, Any]:
        return store.record_feedback(
            decision_id, rating=request.rating, reporter=request.reporter, note=request.note
        )

    @app.get("/api/feedback")
    def list_feedback(
        pack_id: str | None = None, rating: Literal["up", "down"] | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        return store.feedback(pack_id=pack_id, rating=rating, limit=min(limit, 500))

    @app.get("/api/packs/{namespace}/{name}/feedback/export")
    def export_feedback(
        namespace: str, name: str, rating: Literal["up", "down"] = "down"
    ) -> Response:
        pack = pack_or_404(f"{namespace}/{name}")
        records, skipped = feedback_dataset(store, pack_id=pack.id, rating=rating)
        body = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
        return Response(
            body,
            media_type="application/x-ndjson",
            headers={
                "content-disposition": f'attachment; filename="{name}-feedback-{rating}.jsonl"',
                # Decisions stored with audit.store_input=hash have no text to export.
                "x-openhealth-skipped": str(skipped),
            },
        )

    @app.get("/api/stats")
    def stats(pack_id: str | None = None) -> dict[str, Any]:
        return store.stats(pack_id)

    # -- CDS Hooks ---------------------------------------------------------------------------

    @app.get("/cds-services")
    def cds_discovery() -> dict[str, Any]:
        return cds_hooks.discovery(packs.values())

    @app.post("/cds-services/{service_id}")
    def cds_invoke(service_id: str, request: JsonBody) -> dict[str, Any]:
        pack = next((p for p in packs.values() if cds_hooks.service_id(p) == service_id), None)
        if pack is None:
            raise PackNotFoundError(f"no CDS service {service_id!r}")
        return cds_hooks.invoke(decision_for(pack.id), request)

    # -- dashboard ---------------------------------------------------------------------------

    page = versioned_dashboard()

    @app.get("/", include_in_schema=False)
    def dashboard() -> HTMLResponse:
        return HTMLResponse(page, headers={"cache-control": "no-cache"})

    app.mount("/static", StaticFiles(directory=DASHBOARD_DIR), name="static")
    return app


def versioned_dashboard() -> str:
    """index.html with each asset URL stamped by a hash of its content, so a deploy that
    changes app.js or style.css can never be served stale from a browser or CDN cache."""
    page = (DASHBOARD_DIR / "index.html").read_text(encoding="utf-8")
    for asset in ("app.js", "style.css"):
        digest = hashlib.sha256((DASHBOARD_DIR / asset).read_bytes()).hexdigest()[:12]
        page = page.replace(f"/static/{asset}", f"/static/{asset}?v={digest}")
    return page


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
