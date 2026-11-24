# SPDX-License-Identifier: Apache-2.0
"""The System routes (`/api/system/...`): the deployment's readiness and its operator's actions,
which needed a terminal before.

- `GET /api/system/checks`: readiness as a list of checks (`server/system.py`).
- `POST /api/system/audit/verify`: verify the whole audit chain now. When and how it ended is
  recorded outside the chain (`system_runs`), so the checks say when it was last verified; a
  chain that does not verify raises the same alert as the scheduled checkpoint's.
- `POST /api/system/alerts/test`: one test alert to the alert webhook, as `mendari alerts test`
  sends it, recorded with the alerts (`alert_deliveries`) as delivered or failed, never left to
  be sent again.
- `POST /api/system/ehr-check`: what a launch from one of the organization's registered EHRs
  would meet here, as `mendari smart check` reports it. Only the EHR's own servers are called,
  and only one registered to the caller's organization is checked.

**Who.** `operate` (`server/auth.py`): an administrator, signed in as a person, of the
organization that operates the deployment; never a key, a token or another organization's
administrator. In open mode, everyone, as everything. Holding all automation and erasing a
decision's kept input, which the System view offers beside these, are the organization's own
routes (`POST /api/holds`, `POST /api/audit/{id}/erase`), under their own permissions.

**Scope.** The checks are the deployment's, but what they show of an organization (its EHRs,
its CDS clients, its holds) is the caller's organization's only, as `/api/health` shows it. The
EHR check refuses a base that is not registered to the caller's organization as it refuses an
unknown one.

**Limits.** The three actions count against `label` (`server/limits.py`): per person, at a
person's pace. Reading the checks is not limited.

**Recorded.** Each action writes a line to the security log (`system.audit.verified`,
`system.alert.tested`, `system.ehr.checked`, with a fixed reason and no address): what must last
is already in the chain (a hold, an erasure), and these three change nothing an organization
owns.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from mendari import __version__
from mendari.alerts import AlertSettings, check_webhook, send_test
from mendari.core.audit import AuditStore
from mendari.errors import MendariError
from mendari.server import security_log, smart_check, system
from mendari.server.auth import Auth, Principal
from mendari.standards.smart import Registration, normalize_base

SYSTEM_TEST_LINE = "a test alert from the System view: nothing to do"
MAX_DETAIL = 500  # a finding's detail may quote the EHR's own configuration: bounded


class Verified(BaseModel):
    """What verifying the whole audit chain came to."""

    ok: bool = Field(description="False: an event was changed, removed or added out of order.")
    events: int = Field(description="How many events were checked.")
    verified_at: str
    reason: str | None = Field(
        default=None,
        description="When it failed, which check did: `chain` (an event's hash), `checkpoint`, "
        "`organization`, `decision`, `store`.",
    )
    broken_at: int | None = Field(
        default=None, description="The place in the chain of the first event that fails."
    )


class TestAlert(BaseModel):
    """What one test alert came to."""

    __test__ = False  # a model, not a test class

    delivered: bool
    status: int | None = Field(
        default=None, description="The webhook's HTTP status; null without an answer."
    )
    error: str | None = Field(
        default=None, description="A fixed code when it was not delivered; never an address."
    )
    delivery: str = Field(description="The alert's id, as the receiver saw it.")


class EhrCheckRequest(BaseModel):
    base: str = Field(
        min_length=1,
        max_length=2000,
        description="The FHIR base of an EHR registered to your organization to launch Mendari.",
    )


class EhrFinding(BaseModel):
    status: Literal["ok", "warn", "unknown", "fail"]
    check: str
    detail: str


class EhrCheck(BaseModel):
    """What a launch from this EHR would meet here."""

    base: str
    ok: bool = Field(description="False when a finding is `fail`: launches would be refused.")
    findings: list[EhrFinding]


def add_system_routes(
    app: FastAPI,
    auth: Auth,
    store: AuditStore,
    *,
    deployment: Callable[[Request, Principal], system.Deployment],
    alerts: AlertSettings | None,
    ehrs: Mapping[str, Registration],
    chain_broken: Callable[[bool, str], None],
) -> None:
    """Mount the System routes. `deployment` gathers what the checks read for a request and
    its principal (`create_app`, which holds it); `alerts` is the alert webhook's settings
    (None: no webhook); `ehrs` the EHRs registered to launch, by FHIR base; `chain_broken` what
    the scheduled checkpoint does when the chain does not verify (the security log's line and
    the alert), called with whether the chain itself held and the fixed reason."""

    def operator(request: Request) -> Principal:
        return auth.acting(request)

    def logged(request: Request, event: str, *, ok: bool, reason: str | None = None) -> None:
        principal = operator(request)
        security_log.event(
            event,
            outcome="ok" if ok else "error",
            reason=reason,
            actor=principal.id,
            org=principal.org,
        )

    @app.get(
        "/api/system/checks",
        tags=["System"],
        summary="The deployment's readiness, as checks",
    )
    def system_checks(request: Request) -> system.SystemChecks:
        """Whether this deployment is set up to be relied on, as a list of checks in the order
        to act on them: sign-in, who runs it, the public address, patient data mode and its
        sealing key, the database, the audit chain and its checkpoints, the scheduled jobs and
        backups, alerts, limits, the EHR connection, the agent registry, the agents and the
        engine's version. Each has a `state` (`ok`, `attention`, `not_set` or
        `not_applicable`), one sentence on what is so, what to do about it, and when what it
        reports last happened.

        `setup.fresh` says no decision was ever made here and only the bundled demo agent is
        served; the console then opens on "Finish setting up" until `setup.finished`.
        `recalls` lists the served versions a registry recalled: they no longer automate.

        Worked out from what signed-in `/api/health` and `/api/health/ready` say, never from
        a second reading of the settings. Nothing secret is in it: a key is named by its id,
        the alert webhook only as set or not, a registry by its origin. What it shows of an
        organization (its EHRs, its CDS clients) is your organization's only."""
        principal = operator(request)
        return system.answer(deployment(request, principal), version=__version__)

    @app.post(
        "/api/system/audit/verify",
        tags=["System"],
        summary="Verify the whole audit chain now",
    )
    def verify_chain(request: Request) -> Verified:
        """Recompute the hash chain over every event and check each decision's record against
        it, as `mendari audit verify` does. `ok: false` means the trail was altered: the
        answer says which check failed and where, the alert webhook is told (`audit.broken`),
        and the checks show it until a verification passes again. When it ran and how it
        ended is kept outside the chain; nothing is chained, and no checkpoint is recorded
        (the daily job records those)."""
        status = store.verify()
        ok = bool(status["ok"])
        reason = None if ok else str(status.get("reason") or "chain")
        run = store.record_run("audit.verify", "ok" if ok else "failed", "full" if ok else reason)
        logged(request, "system.audit.verified", ok=ok, reason=reason)
        if reason is not None:
            chain_broken(False, reason)
        broken_at = status.get("broken_at")
        return Verified(
            ok=ok,
            events=int(status.get("events") or 0),
            verified_at=str(run["ran_at"]),
            reason=reason,
            broken_at=broken_at if isinstance(broken_at, int) else None,
        )

    @app.post(
        "/api/system/alerts/test",
        tags=["System"],
        summary="Send a test alert",
        responses={409: {"description": "No alert webhook is set here"}},
    )
    def test_alert(request: Request) -> TestAlert:
        """One test alert (`alerts.test`, with no alerts in it: a receiver treats it as nothing
        to do) to the alert webhook, in its format and signed with its secret as every alert
        is. Tried once. It is recorded with the alerts as delivered or failed, so the checks
        say when an alert last arrived, and it is never sent again by itself. The answer names
        the webhook's HTTP status or a fixed error code, never its address. `409` when no
        webhook is set."""
        if alerts is None:
            raise HTTPException(409, "no alert webhook is set here (MENDARI_ALERT_WEBHOOK)")
        try:
            check_webhook(alerts.webhook)
        except MendariError as error:  # its own words name no address
            logged(request, "system.alert.tested", ok=False, reason="webhook_unusable")
            raise HTTPException(409, str(error)) from None
        sent = send_test(alerts, store=store, line=SYSTEM_TEST_LINE)
        logged(request, "system.alert.tested", ok=sent.delivered, reason=sent.error)
        return TestAlert(
            delivered=sent.delivered, status=sent.status, error=sent.error, delivery=sent.id
        )

    @app.post(
        "/api/system/ehr-check",
        tags=["System"],
        summary="Check an EHR's connection",
        responses={404: {"description": "No EHR with this FHIR base is registered to you"}},
    )
    def ehr_check(request: Request, body: EhrCheckRequest) -> EhrCheck:
        """What a launch from an EHR registered to your organization would meet here, with this
        deployment's settings as they are: its registration and the allow-list, the SMART
        client, the EHR's own configuration, the scopes it publishes, PKCE and its signing
        keys. Each finding is `ok`, `warn` (works, but check it), `unknown` (the EHR does not
        say: ask it) or `fail` (launches will be refused). Only the EHR's own servers are
        called, for their public configuration and keys: no launch, no token, no patient's
        record. A base that is not registered to your organization is a `404`, as an unknown
        one is."""
        principal = operator(request)
        key = normalize_base(body.base)
        registered = None if key is None else ehrs.get(key)
        if registered is None or (principal.kind != "open" and registered.org != principal.org):
            raise HTTPException(404, "no EHR with this FHIR base is registered to you")
        findings = [
            EhrFinding.model_validate(
                {"status": found.status, "check": found.check, "detail": found.detail[:MAX_DETAIL]}
            )
            for found in smart_check.check(registered.base)
        ]
        ok = not any(found.status == "fail" for found in findings)
        store.record_run("ehr.check", "ok" if ok else "failed")
        logged(request, "system.ehr.checked", ok=ok, reason=None if ok else "fail")
        return EhrCheck(base=registered.base, ok=ok, findings=findings)


def scheduled_run(store: AuditStore, job: str, *, ok: bool, detail: str | None = None) -> None:
    """Record a scheduled job's run for the checks (`AuditStore.record_run`). A job never fails
    because its run could not be recorded: the checks then show it as not run, which is what a
    person should look into."""
    try:
        store.record_run(job, "ok" if ok else "failed", detail)
    except Exception as error:
        security_log.event("system.run.unrecorded", outcome="error", reason=type(error).__name__)
