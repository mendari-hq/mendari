"""SMART on FHIR endpoints: the EHR launch, the session, and the patient's work items.

- `GET /smart/launch?iss=&launch=[&pack=]` and `GET /smart/callback` run the launch
  (`standards/smart.py`), tied to the browser that started it (and so begun on
  `MENDARI_PUBLIC_URL`, where the callback lands); the callback sets an opaque,
  HttpOnly, SameSite=Lax `__Host-` session cookie (ending the launch the browser held until
  then: one browser, one launch) and sends the browser to the dashboard's "This patient" view
  for the pack, pinned to this launch (`#patient/<launch_id>`: a window whose launch another
  launch in the same browser replaced says so, and never shows the other patient). The session
  ends after 30 minutes idle, 8 hours, or when the EHR's token expires, whichever is first
  (`server/sessions.py`).
- `GET /api/smart/session` (`{"active": false}` without one), `POST /api/smart/logout`: public.
  The session names the launch (`launch_id`, not a secret) and the packs it may use (id, name
  and origin), so a browser that only launched opens the dashboard on the patient view alone.
- `GET /api/smart/patient`: the launch's patient, for the banner.
- `GET /api/smart/focus?pack_id=`: the patient's resources the pack decides about (their inbox
  messages, their referrals).
- `POST /api/smart/decide`: decide one of them, with the record fetched through the pack's own
  prefetch templates; the decision is audited like any other.
- These three take the launch a dashboard window is pinned to (`X-Mendari-Launch`) and answer
  `409` when the browser's launch is another one now, before reading anything from the EHR.

**Who launched** (ADR-0006). With sign-in on, a launch is a principal of kind `ehr`
(`server/auth.py`): the guard resolves it from the launch cookie (`Auth.launch`, which these
routes share), so its decisions are in its EHR's registered organization and limited per
principal, patient and focus need `launch` and decide `launch_decide`. Before any request
leaves, a launch from an EHR that is not registered (`MENDARI_SMART_EHRS`) is refused
(`unregistered`, also in open mode once any EHR is registered), and with sign-in on so is one
whose scopes do not ask for `openid fhirUser` (`configuration`); at the callback, an id_token
without a subject (`id_token`) and a user who is not a Practitioner or PractitionerRole
(`user_type`). The sealed session keeps who launched: the id_token's issuer and subject, and
the chain's name for them (a verified email, else `<issuer>|<subject>`), and whether those
sign-in on checks ran (`identified`): a launch made while sign-in was off is no principal once
it is on. Open mode with no EHR registered launches as before.

The access token never leaves the server, and every FHIR read uses the session's token through
the allow-listed client. The pending launch is sealed under the launch's binding cookie and the
session under its own cookie (`core/session_seal.py`, ADR-0006): the database alone opens
neither, a row that does not open ends the launch, and so does an EHR that answers `401` to the
session's token. The security log has `smart.launch.ok` for each launch that starts a
session and `smart.launch.refused` for each one that does not, with a fixed reason: never a
value from the request or the EHR.
"""

from __future__ import annotations

import html
import re
import secrets
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from mendari.core.audit import AuditStore
from mendari.core.decision import Decision
from mendari.core.pack import Pack
from mendari.core.session_seal import SealError
from mendari.errors import FhirError, PackNotFoundError, SmartError
from mendari.server import security_log, sessions
from mendari.server.auth import Auth, chain_name, ehr_id
from mendari.standards import smart
from mendari.standards.cds_hooks import fill_template
from mendari.standards.fhir import FhirAdapter
from mendari.standards.fhir_client import FhirClient, GuardedHttp
from mendari.standards.smart import SmartSettings

COOKIE = sessions.SMART  # `__Host-mendari_smart` except on local hosts (server/sessions.py)
PIN_HEADER = "x-mendari-launch"  # the launch a dashboard window is pinned to (its `launch_id`)
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
    packs: Mapping[str, Pack],
    pack_or_404: Callable[[str], Pack],
    decision_for: Callable[[str], Decision],
    default_pack: str | None,
    auth: Auth,
    lifetime: sessions.Lifetime | None = None,
) -> None:
    lifetime = lifetime or sessions.Lifetime()
    ehrs = auth.ehrs or {}
    identify = auth.enabled  # with sign-in on, a launch must say who the clinician is
    gated = auth.enabled or bool(ehrs)  # only registered EHRs may launch

    def configured() -> SmartSettings:
        if settings is None:
            raise SmartError(
                "SMART launch is not configured here (MENDARI_SMART_CLIENT_ID)",
                404,
                reason="not_configured",
            )
        return settings

    def public_url(request: Request) -> str:
        return configured().public_url or str(request.base_url).rstrip("/")

    def session_of(request: Request) -> dict[str, Any]:
        """The launch the guard resolved (with sign-in on it is this request's `ehr`
        principal); in open mode, resolved here the same way. A dashboard window sends the
        launch it is pinned to (`X-Mendari-Launch`): when a later launch in this browser
        replaced that one, `409`, before anything is read from the EHR, so the window never
        shows the other launch's patient. A request without the header is not checked."""
        launch = auth.launch(request) if settings is not None else None
        if launch is None:
            raise SmartError("no SMART session: launch the app from the EHR again", 401)
        pinned = request.headers.get(PIN_HEADER)
        if pinned is not None and pinned != launch.session.get("launch_id"):
            raise SmartError(
                "this window's launch was replaced by a later one in this browser: "
                "open Mendari again from the EHR",
                409,
            )
        return launch.session

    def registered(iss: str) -> smart.Registration | None:
        """The EHR's registration; refused (`unregistered`) when only registered EHRs may
        launch and this one is not."""
        found = smart.registration(ehrs, iss)
        if gated and found is None:
            raise SmartError(
                "this EHR is not registered here (MENDARI_SMART_EHRS): ask the administrator",
                403,
                reason="unregistered",
            )
        return found

    @contextmanager
    def ehr(request: Request, session: Mapping[str, Any]) -> Iterator[FhirClient]:
        """The EHR's FHIR server, read with the session's token. When the EHR answers `401`
        the token is over there (expired early, revoked, the user signed out of the EHR): the
        session ends here too, and the launch must start again."""
        try:
            with FhirClient(session["iss"], token=session["access_token"]) as fhir:
                yield fhir
        except FhirError as error:
            if error.status != 401:
                raise
            sid, _ = sessions.read(request, COOKIE)
            if sid:
                store.smart_delete(sid, "session")
            raise SmartError(
                "the EHR ended this launch: open Mendari again from the EHR", 401
            ) from None

    @app.get("/smart/launch", include_in_schema=False)
    def smart_launch(request: Request, iss: str, launch: str, pack: str | None = None) -> Response:
        with refusals_logged(unreachable="issuer"):
            options = configured()
            registered(iss)  # before anything leaves for the EHR
            # Launched on another name: begin again on the public URL, where the callback lands.
            params = {"iss": iss, "launch": launch} | ({"pack": pack} if pack else {})
            moved = sessions.to_public_url(request, options.public_url, "/smart/launch", params)
            if moved is not None:
                return moved
            pack_id = pack or default_pack
            if pack_id is None:
                raise SmartError("no pack to launch", 404, reason="pack")
            pack_or_404(pack_id)
            redirect_uri = f"{public_url(request)}/smart/callback"
            with GuardedHttp() as http:
                url, state, pending = smart.begin(
                    iss,
                    launch,
                    redirect_uri=redirect_uri,
                    settings=options,
                    http=http,
                    identify=identify,
                )
        pending["pack"] = pack_id
        response = RedirectResponse(url, status_code=302)
        # Only this browser may finish the launch: the row is sealed under its binding cookie.
        _, binding = sessions.bind(response, request, sessions.LAUNCH)
        expires = int(time.time()) + smart.PENDING_SECONDS
        store.smart_put(state, "pending", pending, expires, secret=binding)
        return response

    @app.get("/smart/callback", include_in_schema=False)
    def smart_callback(
        request: Request, state: str = "", code: str = "", error: str = ""
    ) -> Response:
        with refusals_logged(unreachable="id_token"):  # the issuer's keys, for the id_token
            options = configured()
            pending = None
            unopened = False
            if state:
                # Taken (once) whoever brings it back; it opens only with the binding cookie
                # of the browser that started the launch.
                binding = sessions.binding(request, sessions.LAUNCH) or ""
                try:
                    pending = store.smart_take(state, "pending", secret=binding)
                except SealError:
                    unopened = True
            if error:
                shown = error if _OAUTH_ERROR.match(error) else "error"
                raise SmartError(
                    f"the EHR's authorization server did not authorize the app ({shown})",
                    reason="ehr_error",
                )
            if unopened:
                # Someone else's launch (or one in flight at the upgrade that sealed these rows):
                # its patient context must not land in this browser.
                raise SmartError(
                    "this launch was started in another browser: start it again", reason="browser"
                )
            if pending is None:
                raise SmartError(
                    "this launch is unknown, used or expired: start it again from the EHR",
                    reason="state_unknown",
                )
            if not code:
                raise SmartError("the authorization server sent no code", reason="no_code")
            registration = registered(pending["iss"])  # unregistered since the launch began
            with GuardedHttp() as http:
                session = smart.complete(
                    pending, code, settings=options, http=http, identify=identify
                )
        session["pack"] = pending["pack"]
        # The launch's name in the window it opens (`#patient/<launch_id>`): not a secret, it
        # tells that window whether the browser's launch is still this one.
        session["launch_id"] = secrets.token_hex(8)
        # Who launched, sealed with the rest: the id_token's issuer and subject, and the name
        # the chain gives them. The address and its verification are not kept.
        identity = session.pop("identity", None)
        actor = None
        if identity is not None:
            session["identity"] = {"iss": identity["iss"], "sub": identity["sub"]}
            session["actor"] = chain_name(identity["iss"], identity)
            actor = ehr_id(identity["iss"], identity["sub"])
        if identify:
            # The checks sign-in on makes (the subject, a Practitioner, `openid fhirUser`) ran:
            # only such a launch is a principal with sign-in on (`Auth._resolve`), so one made
            # while sign-in was off is not, once it is on.
            session["identified"] = True
        # `expires_at` stays the EHR token's expiry (shown in /api/smart/session); the session
        # ends by then, or earlier when idle or at the absolute limit.
        row, expires_at = sessions.begin(
            session, int(time.time()), lifetime, ends_by=session["expires_at"]
        )
        # One browser, one launch: the launch this browser held until now ends here, so a copy
        # of its cookie reaches nothing and signing out leaves no launch behind.
        replaced, _ = sessions.read(request, COOKIE)
        if replaced:
            store.smart_delete(replaced, "session")
        sid = secrets.token_urlsafe(32)
        store.smart_put(sid, "session", row, expires_at)
        org = registration.org if registration is not None and auth.enabled else None
        security_log.event("smart.launch.ok", actor=actor, org=org)
        pack = quote(pending["pack"], safe="/")
        opened = f"{public_url(request)}/?pack={pack}#patient/{session['launch_id']}"
        response = RedirectResponse(opened, status_code=302)
        sessions.set_cookie(response, request, COOKIE, sid)
        sessions.clear_cookie(response, request, sessions.LAUNCH)  # used: one launch, once
        return response

    @app.get(
        "/api/smart/session",
        tags=["SMART on FHIR"],
        summary="The SMART launch session",
    )
    def smart_session(request: Request) -> dict[str, Any]:
        """The launch's context, or `{"active": false}` (every dashboard load asks, so no launch
        is an answer, not an error): the EHR, patient and encounter, who launched (`fhir_user`),
        the scopes, when the EHR's token expires, the pack launched, the launch's id
        (`launch_id`: the dashboard window it opened carries it, to tell a replaced launch), and
        the packs it may use (`packs`: id, name and origin)."""
        launch = auth.launch(request) if settings is not None else None
        if launch is None:
            return {"active": False}
        session = launch.session
        shown = ("iss", "patient", "encounter", "fhir_user", "scope", "expires_at", "pack")
        context = {key: session.get(key) for key in shown}
        listed = [
            {"id": pack.id, "name": pack.manifest.name, "origin": pack.shown_origin}
            for pack in packs.values()
        ]
        return {
            "active": True,
            **context,
            "launch_id": session.get("launch_id"),
            "packs": sorted(listed, key=lambda pack: pack["id"]),
        }

    @app.post(
        "/api/smart/logout",
        tags=["SMART on FHIR"],
        summary="End the SMART session",
    )
    def smart_logout(request: Request) -> Response:
        """Forget the EHR launch (its tokens are deleted)."""
        sid, _ = sessions.read(request, COOKIE)
        if sid:
            store.smart_delete(sid, "session")
        response = Response(status_code=204)
        sessions.clear_cookie(response, request, COOKIE)
        return response

    @app.get(
        "/api/smart/patient",
        tags=["SMART on FHIR"],
        summary="The launch's patient",
    )
    def smart_patient(request: Request) -> dict[str, Any]:
        """The patient in context, read from the EHR's FHIR server: id, name, gender and birth
        date.

        Needs an EHR launch (`/smart/launch`, the `__Host-mendari_smart` cookie; with sign-in on,
        from a registered EHR); without one, `401`, and `401` when the EHR refuses the launch's
        token (the launch has ended: open Mendari again from the EHR). `409` when
        `X-Mendari-Launch` names another launch than the browser's (a later one replaced it)."""
        session = session_of(request)
        with ehr(request, session) as fhir:
            return patient_summary(fhir.read(f"Patient/{session['patient']}"))

    @app.get(
        "/api/smart/focus",
        tags=["SMART on FHIR"],
        summary="Items to decide about",
    )
    def smart_focus(request: Request, pack_id: str) -> list[dict[str, Any]]:
        """The patient's resources the pack decides about (e.g. referrals), from the EHR.

        Needs an EHR launch (`/smart/launch`, the `__Host-mendari_smart` cookie; with sign-in on,
        from a registered EHR); without one, `401`, and `401` when the EHR refuses the launch's
        token (the launch has ended: open Mendari again from the EHR). `409` when
        `X-Mendari-Launch` names another launch than the browser's (a later one replaced it)."""
        session = session_of(request)
        adapter = FhirAdapter.for_pack(pack_or_404(pack_id))
        with ehr(request, session) as fhir:
            return focus_items(fhir, adapter, session["patient"])

    @app.post(
        "/api/smart/decide",
        tags=["SMART on FHIR"],
        summary="Decide for the launch's patient",
    )
    def smart_decide(request: Request, body: SmartDecideRequest) -> dict[str, Any]:
        """Read the record from the EHR, decide, and return the decision and its FHIR Task.

        Needs an EHR launch (`/smart/launch`, the `__Host-mendari_smart` cookie; with sign-in on,
        from an EHR registered to decide); without one, `401`, `403` from an EHR registered to
        view only, and `401` when the EHR refuses the launch's token (the launch has ended: open
        Mendari again from the EHR). `409` when `X-Mendari-Launch` names another launch than the
        browser's (a later one replaced it): nothing is read or decided. With sign-in on the
        decision is the EHR's registered organization's."""
        session = session_of(request)
        decision = decision_for(body.pack_id)
        with ehr(request, session) as fhir:
            result, task = decide_for(decision, fhir, session["patient"], body.focus)
        return {"decision": result.to_dict(), "task": task}


@contextmanager
def refusals_logged(*, unreachable: str) -> Iterator[None]:
    """One `smart.launch.refused` line for a launch step that fails: the `SmartError`'s fixed
    reason, `pack` for an unknown pack, and `unreachable` when the allow-listed client refuses
    or cannot reach a server (the launch's issuer, or the keys that verify its id_token)."""
    try:
        yield
    except SmartError as error:
        _refused(error.reason)
        raise
    except PackNotFoundError:
        _refused("pack")
        raise
    except FhirError:
        _refused(unreachable)
        raise


def _refused(reason: str) -> None:
    security_log.event("smart.launch.refused", outcome="refused", reason=reason)


def error_page(message: str, status: int) -> HTMLResponse:
    """Launch errors land in the EHR's browser, so they get a page rather than JSON, with the
    request's id for the person to quote (server/security_log.py)."""
    rid = security_log.request_id()
    reference = f"<p>Reference: <code>{html.escape(rid)}</code></p>" if rid else ""
    body = (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        "<title>Mendari: launch failed</title>"
        "<body style='font:16px system-ui;max-width:36rem;margin:3rem auto;padding:0 1rem'>"
        f"<h1>The launch did not complete</h1><p>{html.escape(message)}</p>{reference}"
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
