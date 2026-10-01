"""SMART on FHIR endpoints: the EHR launch, the session, and the patient's work items.

- `GET /smart/launch?iss=&launch=[&pack=]` and `GET /smart/callback` run the launch
  (`standards/smart.py`); the callback sets an opaque, HttpOnly, SameSite=Lax session cookie and
  sends the browser to the dashboard's "This patient" view for the pack.
- `GET /api/smart/session` (`{"active": false}` without one), `POST /api/smart/logout`.
- `GET /api/smart/patient`: the launch's patient, for the banner.
- `GET /api/smart/focus?pack_id=`: the patient's resources the pack decides about (their inbox
  messages, their referrals).
- `POST /api/smart/decide`: decide one of them, with the record fetched through the pack's own
  prefetch templates; the decision is audited like any other.

The access token never leaves the server, and every FHIR read uses the session's token through
the allow-listed client.
"""

from __future__ import annotations

import html
import re
import secrets
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from mendari.core.audit import AuditStore
from mendari.core.decision import Decision
from mendari.core.pack import Pack
from mendari.errors import FhirError, SmartError
from mendari.standards import smart
from mendari.standards.cds_hooks import fill_template
from mendari.standards.fhir import FhirAdapter
from mendari.standards.fhir_client import FhirClient, GuardedHttp
from mendari.standards.smart import SmartSettings

COOKIE = "mendari_smart"
_ID = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")
_OAUTH_ERROR = re.compile(r"^[a-z_]{1,40}$")
MAX_FOCUS = 50


class SmartDecideRequest(BaseModel):
    pack_id: str = Field(min_length=3, max_length=200)
    focus: str = Field(min_length=3, max_length=200, description='e.g. "ServiceRequest/123"')


def add_smart_routes(
    app: FastAPI,
    *,
    settings: SmartSettings | None,
    store: AuditStore,
    pack_or_404: Callable[[str], Pack],
    decision_for: Callable[[str], Decision],
    default_pack: str | None,
) -> None:
    def configured() -> SmartSettings:
        if settings is None:
            raise SmartError("SMART launch is not configured here (MENDARI_SMART_CLIENT_ID)", 404)
        return settings

    def public_url(request: Request) -> str:
        return configured().public_url or str(request.base_url).rstrip("/")

    def session_of(request: Request) -> dict[str, Any]:
        sid = request.cookies.get(COOKIE)
        session = store.smart_get(sid, "session") if sid else None
        if session is None:
            raise SmartError("no SMART session: launch the app from the EHR again", 401)
        return session

    @app.get("/smart/launch", include_in_schema=False)
    def smart_launch(request: Request, iss: str, launch: str, pack: str | None = None) -> Response:
        options = configured()
        pack_id = pack or default_pack
        if pack_id is None:
            raise SmartError("no pack to launch", 404)
        pack_or_404(pack_id)
        redirect_uri = f"{public_url(request)}/smart/callback"
        with GuardedHttp() as http:
            url, pending = smart.begin(
                iss, launch, redirect_uri=redirect_uri, settings=options, http=http
            )
        pending["pack"] = pack_id
        expires = int(time.time()) + smart.PENDING_SECONDS
        store.smart_put(pending["state"], "pending", pending, expires)
        return RedirectResponse(url, status_code=302)

    @app.get("/smart/callback", include_in_schema=False)
    def smart_callback(
        request: Request, state: str = "", code: str = "", error: str = ""
    ) -> Response:
        options = configured()
        pending = store.smart_take(state, "pending") if state else None
        if error:
            reason = error if _OAUTH_ERROR.match(error) else "error"
            raise SmartError(f"the EHR's authorization server did not authorize the app ({reason})")
        if pending is None:
            raise SmartError("this launch is unknown, used or expired: start it again from the EHR")
        if not code:
            raise SmartError("the authorization server sent no code")
        with GuardedHttp() as http:
            session = smart.complete(pending, code, settings=options, http=http)
        session["pack"] = pending["pack"]
        sid = secrets.token_urlsafe(32)
        store.smart_put(sid, "session", session, session["expires_at"])
        pack = quote(pending["pack"], safe="/")
        response = RedirectResponse(f"{public_url(request)}/?pack={pack}#patient", status_code=302)
        response.set_cookie(
            COOKIE,
            sid,
            max_age=max(0, session["expires_at"] - int(time.time())),
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        return response

    @app.get("/api/smart/session")
    def smart_session(request: Request) -> dict[str, Any]:
        """The launch's context, or `{"active": false}` (every dashboard load asks, so no launch
        is an answer, not an error)."""
        sid = request.cookies.get(COOKIE)
        session = store.smart_get(sid, "session") if sid and settings is not None else None
        if session is None:
            return {"active": False}
        shown = ("iss", "patient", "encounter", "fhir_user", "scope", "expires_at", "pack")
        return {"active": True, **{key: session.get(key) for key in shown}}

    @app.post("/api/smart/logout")
    def smart_logout(request: Request) -> Response:
        sid = request.cookies.get(COOKIE)
        if sid:
            store.smart_delete(sid)
        response = Response(status_code=204)
        response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")
        return response

    @app.get("/api/smart/patient")
    def smart_patient(request: Request) -> dict[str, Any]:
        session = session_of(request)
        with FhirClient(session["iss"], token=session["access_token"]) as fhir:
            return patient_summary(fhir.read(f"Patient/{session['patient']}"))

    @app.get("/api/smart/focus")
    def smart_focus(request: Request, pack_id: str) -> list[dict[str, Any]]:
        session = session_of(request)
        adapter = FhirAdapter.for_pack(pack_or_404(pack_id))
        with FhirClient(session["iss"], token=session["access_token"]) as fhir:
            return focus_items(fhir, adapter, session["patient"])

    @app.post("/api/smart/decide")
    def smart_decide(request: Request, body: SmartDecideRequest) -> dict[str, Any]:
        session = session_of(request)
        decision = decision_for(body.pack_id)
        with FhirClient(session["iss"], token=session["access_token"]) as fhir:
            result, task = decide_for(decision, fhir, session["patient"], body.focus)
        return {"decision": result.to_dict(), "task": task}


def error_page(message: str, status: int) -> HTMLResponse:
    """Launch errors land in the EHR's browser, so they get a page rather than JSON."""
    body = (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        "<title>Mendari: launch failed</title>"
        "<body style='font:16px system-ui;max-width:36rem;margin:3rem auto;padding:0 1rem'>"
        f"<h1>The launch did not complete</h1><p>{html.escape(message)}</p>"
        "<p><a href='/'>Open the dashboard</a></p>"
    )
    return HTMLResponse(body, status_code=status)


# -- the patient's context ----------------------------------------------------------------------


def patient_summary(patient: Mapping[str, Any]) -> dict[str, Any]:
    names = patient.get("name") or [{}]
    name = names[0] if isinstance(names, list) and names else {}
    display = name.get("text") or " ".join([*name.get("given", []), name.get("family", "")]).strip()
    return {
        "id": patient.get("id"),
        "name": display or None,
        "gender": patient.get("gender"),
        "birthDate": patient.get("birthDate"),
    }


def focus_items(fhir: FhirClient, adapter: FhirAdapter, patient_id: str) -> list[dict[str, Any]]:
    """The patient's resources of the pack's focus type (only those the pack decides about)."""
    kind = adapter.mapping.focus
    if kind == "Patient":
        return [_summary(fhir.read(f"Patient/{patient_id}"))]
    bundle = fhir.read(f"{kind}?patient={quote(patient_id, safe='')}&_count={MAX_FOCUS}")
    resources = [e.get("resource") or {} for e in bundle.get("entry") or []]
    found = [r for r in resources if r.get("resourceType") == kind and adapter.is_focus(r)]
    return [_summary(resource) for resource in found[:MAX_FOCUS]]


def decide_for(
    decision: Decision, fhir: FhirClient, patient_id: str, focus_ref: str
) -> tuple[Any, dict[str, Any]]:
    """Decide one of the patient's focus resources, with the record the pack's CDS prefetch
    templates describe, read with the session's token."""
    adapter = FhirAdapter.for_pack(decision.pack)
    kind, _, resource_id = focus_ref.partition("/")
    if kind != adapter.mapping.focus or not _ID.match(resource_id):
        raise FhirError(f"this pack decides about a {adapter.mapping.focus}, not {focus_ref!r}")
    focus = fhir.read(f"{kind}/{resource_id}")
    about = focus.get("id") if kind == "Patient" else _subject_id(focus)
    if about != patient_id:
        raise SmartError("that resource is not about the launch's patient", 403)
    context = {"patientId": patient_id, f"{kind[0].lower()}{kind[1:]}Id": resource_id}
    hooks = decision.pack.manifest.cds_hooks
    templates = dict(hooks.prefetch) if hooks else {"patient": "Patient/{{context.patientId}}"}
    prefetch = {}
    for key, template in templates.items():
        try:
            query = fill_template(template, context)
        except FhirError:
            continue  # a template for context this launch doesn't have
        prefetch[key] = fhir.read(query)
    return decision.decide_fhir(prefetch, focus=focus)


def _subject_id(resource: Mapping[str, Any]) -> str | None:
    reference = str((resource.get("subject") or {}).get("reference") or "")
    return reference.rsplit("Patient/", 1)[-1] if "Patient/" in reference else None


def _summary(resource: Mapping[str, Any]) -> dict[str, Any]:
    kind = resource.get("resourceType")
    item: dict[str, Any] = {"ref": f"{kind}/{resource.get('id')}", "status": resource.get("status")}
    if kind == "Communication":
        payload = next((p.get("contentString") for p in resource.get("payload") or []), None)
        item |= {
            "title": (resource.get("topic") or {}).get("text") or "Message",
            "text": (payload or "")[:280],
            "date": resource.get("sent"),
        }
    elif kind == "ServiceRequest":
        code = resource.get("code") or {}
        reasons = [r.get("text") for r in resource.get("reasonCode") or [] if r.get("text")]
        item |= {
            "title": code.get("text")
            or next((c.get("display") for c in code.get("coding", [])), None),
            "text": " ".join(reasons)[:280],
            "date": resource.get("authoredOn"),
            "priority": resource.get("priority"),
        }
    elif kind == "Patient":
        item |= {"title": "This patient's record", "text": "", "date": None}
    else:
        item |= {"title": kind, "text": "", "date": (resource.get("meta") or {}).get("lastUpdated")}
    return item
