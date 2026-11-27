# SPDX-License-Identifier: Apache-2.0
"""The deployment's readiness as a list of checks (`GET /api/system/checks`), for the people who
run it: the go-live checklist of docs/deploying-with-phi.md, evaluated by the server instead of
read by eye from the JSON of `/api/health`.

**One source.** A check is worked out from what signed-in `/api/health` and readiness
(`server/readiness.py`) already say, plus the few facts only an operator is shown: when each
scheduled job last ran, when the audit chain was last verified, when an alert was last
delivered. `Deployment` holds them; `checks` is a pure function of it, so every state of every
check is tested without a server (tests/test_system_checks.py).

**Four states.** `ok`; `attention` (something to do before relying on the deployment); `not_set`
(an optional part that is not configured: the deployment works without it); `not_applicable`
(the question does not arise in this configuration). Each check has a label, one sentence that
says what is so, what to do about it (`action`, None when there is nothing to do), and, where it
reports something that happened, when (`at`, an ISO instant the console shows in the
deployment's time zone).

**Nothing secret.** A check names settings and shows public facts only: a key's id (never a
key), an address people already use, the caller's own address, an origin (never a path, which
may hold a token). The alert webhook's address, any secret and any organization's data never
appear; tests/test_system_routes.py seeds each secret and searches the answer for it.

**A fresh install** (`setup`): no decision was ever made and the bundled demo agent is the only
agent. The console then opens on "Finish setting up", which walks the checks that are not `ok`
and ends at adopting an agent; it goes once every check is `ok` or `not_applicable`, or the
deployment is in use.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from mendari.core.https_only import is_https_or_local

FORMAT: Literal["mendari.system-checks/1"] = "mendari.system-checks/1"
State = Literal["ok", "attention", "not_set", "not_applicable"]
STATES: tuple[State, ...] = ("ok", "attention", "not_set", "not_applicable")
# A daily job is overdue once its last run is older than this: a day, and half a day's grace
# for a schedule that drifts or a machine that was off overnight.
OVERDUE = timedelta(hours=36)
# How long the registry's answer is reused, and how long the first check of an instance waits
# for it (later ones never wait: they get the last answer while a new one is read).
REGISTRY_SECONDS = 300.0
REGISTRY_WAIT_S = 2.0
# The scheduled jobs, in the order they run, with what each does in a few words.
JOBS = (
    ("retention", "deleting kept inputs past their retention"),
    ("checkpoint", "the audit trail's daily checkpoint"),
    ("monitor", "checking each agent for drift"),
)
# The areas the console groups the checks under, in order.
ACCESS = "Sign-in and people"
DATA = "Patient data"
AUDIT = "Database and audit trail"
SCHEDULE = "Scheduled jobs and backups"
ALERTS = "Alerts and limits"
CONNECTIONS = "EHR, registry and agents"
ENGINE = "Engine"


class SystemCheck(BaseModel):
    """One readiness check, as the System view lists it."""

    key: str = Field(description="A stable name for the check, such as `audit_chain`.")
    area: str = Field(description="The heading the console lists it under.")
    label: str = Field(description="What is checked, in a few words.")
    state: State = Field(
        description="`ok`; `attention`: something to do before relying on the deployment; "
        "`not_set`: an optional part that is not configured; `not_applicable`: the question "
        "does not arise in this configuration."
    )
    detail: str = Field(description="What is so, in one sentence written for the person on call.")
    action: str | None = Field(
        default=None, description="What to do about it; null when there is nothing to do."
    )
    at: str | None = Field(
        default=None,
        description="When what the check reports last happened (an instant with its offset); "
        "null when it reports a setting.",
    )


class Recall(BaseModel):
    """A served version its registry recalled: it no longer automates."""

    agent: str = Field(description="The agent's id.")
    version: str
    reason: str = Field(description="`recalled (<severity>)` or `deleted`, as the registry says.")


class Setup(BaseModel):
    """Whether this is a fresh install, and whether its setting-up is finished."""

    fresh: bool = Field(
        description="No decision was ever made here and the bundled demo agent is the only agent."
    )
    finished: bool = Field(description="Every check is `ok` or `not_applicable`.")


class SystemChecks(BaseModel):
    """The deployment's readiness: its checks, in the order to act on them."""

    format: Literal["mendari.system-checks/1"] = FORMAT
    version: str = Field(description="The engine's version.")
    checked_at: str
    summary: dict[str, int] = Field(description="How many checks are in each state.")
    setup: Setup
    recalls: list[Recall]
    checks: list[SystemCheck]


@dataclass(frozen=True)
class Alerts:
    """The alert webhook as the operator's checks see it: whether one is set and signed, and
    the newest alert sent and the newest delivered (`AuditStore.last_alerts`). Never its
    address."""

    webhook: bool = False
    signed: bool = False
    last: Mapping[str, Any] | None = None
    delivered: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class Registry:
    """The agent registry: its origin when one is configured (`MENDARI_REGISTRY_URL`), and
    whether it answered the last time it was asked (`RegistryProbe`): None while unknown."""

    origin: str | None = None
    reachable: bool | None = None
    checked_at: str | None = None


@dataclass(frozen=True)
class Deployment:
    """Everything the checks read. `health` is the signed-in `/api/health` answer and `ready`
    the readiness answer's body, as their routes give them; the rest is what only an operator
    is shown."""

    health: Mapping[str, Any]
    ready: Mapping[str, Any]
    now: datetime
    accounts: Literal["open", "claims", "memberships"] = "open"
    operator_org: str | None = None
    operator_org_name: str | None = None
    # Memberships mode: how many administrators of the operator organization have signed in.
    administrators: int | None = None
    # The first-administrator setting: not set, set and still to be used, or used (the
    # database's mark).
    first_admin: Literal["unset", "waiting", "used"] = "unset"
    public_url: str | None = None
    forwarded: bool = False  # this request came with an X-Forwarded-For header
    scheduled: bool = False  # the scheduled jobs' secret is set, so their routes answer
    runs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    checkpoint: Mapping[str, Any] | None = None
    # The day of the newest checkpoint statement this deployment's key signed: the published one.
    statement_day: str | None = None
    alerts: Alerts = field(default_factory=Alerts)
    registry: Registry = field(default_factory=Registry)
    # Organizations' agent stores are served (`MENDARI_AGENT_STORE=on`, memberships mode): what
    # an agent found in the registry is added to from the console.
    agent_store: bool = True
    # The agents the operator's organization is served, the deployment's own and those of its
    # agent store, and how many of them came from a registry. None: as health counts them (the
    # deployment's own only).
    served: int | None = None
    served_from_registry: int | None = None
    any_decision: bool = False
    demo_only: bool = False
    recalls: Sequence[Mapping[str, str]] = ()
    ehr_checked: Mapping[str, Any] | None = None  # the last check of an EHR's connection


def answer(deployment: Deployment, *, version: str) -> SystemChecks:
    """The route's answer: the checks, how many are in each state, whether this is a fresh
    install whose setting-up is not finished, and the served versions a registry recalled."""
    found = checks(deployment)
    counted = Counter(check.state for check in found)
    finished = all(check.state in ("ok", "not_applicable") for check in found)
    fresh = not deployment.any_decision and deployment.demo_only
    return SystemChecks(
        version=version,
        checked_at=_iso(deployment.now),
        summary={state: counted.get(state, 0) for state in STATES},
        setup=Setup(fresh=fresh, finished=finished),
        recalls=[Recall(**recall) for recall in deployment.recalls],
        checks=found,
    )


def checks(deployment: Deployment) -> list[SystemCheck]:
    """Every check, in the order a person sets a deployment up: who gets in, what is kept, the
    audit trail, the daily jobs, alerts and limits, then what it connects to."""
    return [
        _sign_in(deployment),
        _operators(deployment),
        _public_address(deployment),
        _client_address(deployment),
        _patient_data(deployment),
        _sealing_key(deployment),
        _database(deployment),
        _audit_chain(deployment),
        _checkpoint(deployment),
        _checkpoint_signing(deployment),
        _scheduled_jobs(deployment),
        _backup(deployment),
        _alerts(deployment),
        _limits(deployment),
        _ehr(deployment),
        _registry(deployment),
        _agents(deployment),
        _version(deployment),
    ]


# -- who gets in ----------------------------------------------------------------------------------


def _sign_in(d: Deployment) -> SystemCheck:
    label = "Sign-in"
    if not d.health.get("sign_in"):
        return _check(
            "sign_in",
            ACCESS,
            label,
            "attention",
            "Sign-in is off: anyone who can reach this address can read and change everything.",
            "Set your identity provider's issuer and client id (MENDARI_OIDC_ISSUER, "
            "MENDARI_OIDC_CLIENT_ID) in the deployment's settings and restart. Until then, use "
            "synthetic data only.",
        )
    how = (
        "People and their roles are managed in the Access view."
        if d.accounts == "memberships"
        else "Roles come from the groups your identity provider sends."
    )
    return _check(
        "sign_in",
        ACCESS,
        label,
        "ok",
        f"People sign in through your identity provider, and every request needs a role. {how}",
    )


def _operators(d: Deployment) -> SystemCheck:
    label = "Who runs this deployment"
    if d.accounts == "open":
        return _check(
            "operators",
            ACCESS,
            label,
            "not_applicable",
            "With sign-in off, nobody is told apart.",
        )
    named = (d.operator_org_name or d.operator_org) if d.operator_org else None
    if d.accounts == "claims":
        whose = f" of {named}" if named else ""
        return _check(
            "operators",
            ACCESS,
            label,
            "ok",
            f"Everyone your identity provider gives the administrator role{whose} runs this "
            "deployment: they see these checks and may act on them.",
        )
    used = {
        "used": " The first-administrator setting has done its work and is ignored from now on.",
        "waiting": " The first-administrator setting is still set and was never needed: it can "
        "be removed.",
        "unset": "",
    }[d.first_admin]
    count = d.administrators or 0
    if count < 2:
        return _check(
            "operators",
            ACCESS,
            label,
            "attention",
            f"Only one person administers {named}: if they are away, nobody can add people, "
            f"manage keys or run this deployment.{used}",
            "Invite a second administrator in the Access view.",
        )
    return _check(
        "operators",
        ACCESS,
        label,
        "ok",
        f"The {count} administrators of {named} run this deployment: they see these checks and "
        f"may act on them.{used}",
    )


def _public_address(d: Deployment) -> SystemCheck:
    label = "Public address"
    signed_in = bool(d.health.get("sign_in"))
    if not d.public_url:
        if not signed_in:
            return _check(
                "public_address",
                ACCESS,
                label,
                "not_applicable",
                "No public address is needed while sign-in is off.",
            )
        return _check(
            "public_address",
            ACCESS,
            label,
            "attention",
            "No public address is set, so signing in sends people back to whatever name their "
            "browser happened to use.",
            "Set MENDARI_PUBLIC_URL to the https address people use, and restart.",
        )
    shown = _origin(d.public_url) or "the address set"
    if not is_https_or_local(d.public_url):
        return _check(
            "public_address",
            ACCESS,
            label,
            "attention",
            f"The public address ({shown}) is not https: browsers will not send sign-in cookies "
            "over plain http.",
            "Put a TLS certificate in front of the deployment and set MENDARI_PUBLIC_URL to its "
            "https address.",
        )
    return _check(
        "public_address", ACCESS, label, "ok", f"People reach this deployment at {shown}."
    )


def _client_address(d: Deployment) -> SystemCheck:
    label = "Each person's own address"
    limits = d.health.get("rate_limits") or {}
    seen = str(limits.get("client_ip") or "unknown")
    if not d.health.get("sign_in"):
        return _check(
            "client_address",
            ACCESS,
            label,
            "not_applicable",
            "Addresses are told apart for sign-in limits, which apply once sign-in is on.",
        )
    if limits.get("x_forwarded_for_used"):
        return _check(
            "client_address",
            ACCESS,
            label,
            "ok",
            f"The server sees you connecting from {seen}, as the proxy in front reports it. If "
            "that is this computer's address on your network, sign-in limits and the security "
            "log tell people apart.",
        )
    if d.forwarded:
        return _check(
            "client_address",
            ACCESS,
            label,
            "attention",
            f"The server sees {seen} for you, which is the proxy in front of it and not your "
            "own computer: everyone shares one sign-in limit, and the security log cannot tell "
            "people apart.",
            f"Set FORWARDED_ALLOW_IPS to the address the proxy connects from ({seen}) and restart.",
        )
    return _check(
        "client_address",
        ACCESS,
        label,
        "ok",
        f"The server sees you connecting from {seen}, directly, with no proxy in front.",
    )


# -- what is kept ---------------------------------------------------------------------------------


def _patient_data(d: Deployment) -> SystemCheck:
    label = "Patient data mode"
    mode = d.health.get("phi_mode")
    signed_in = bool(d.health.get("sign_in"))
    keyed = str(d.health.get("input_hash") or "").startswith("hmac-sha256")
    days = d.health.get("retention_days")
    if mode == "off":
        return _check(
            "patient_data",
            DATA,
            label,
            "attention",
            "Patient data mode is off: each case's input is kept as plain text in the audit "
            "trail. That is for synthetic data only.",
            "Before any real data: turn sign-in on and set patient data mode "
            "(MENDARI_PHI_MODE=encrypt with MENDARI_PHI_KEY and MENDARI_INPUT_HASH_KEY), on a "
            "new database.",
        )
    kept = (
        f"Each case's input is kept encrypted, apart from the audit trail, readable only by "
        f"reviewers and administrators, and deleted after {days} days."
        if mode == "encrypt"
        else "No case's input is ever kept, only a keyed fingerprint of it: reviewers cannot "
        "read a case here."
    )
    if not signed_in:
        return _check(
            "patient_data",
            DATA,
            label,
            "attention",
            f"{kept} But sign-in is off, so anyone who can reach this address reads whatever is "
            "kept.",
            "Turn sign-in on before any real data.",
        )
    if not keyed:
        return _check(
            "patient_data",
            DATA,
            label,
            "attention",
            f"{kept} But the fingerprints are not keyed, so a short input could be found by "
            "guessing.",
            "Set MENDARI_INPUT_HASH_KEY (32 random bytes, base64) and restart.",
        )
    return _check("patient_data", DATA, label, "ok", kept)


def _sealing_key(d: Deployment) -> SystemCheck:
    label = "Sealing key"
    mode = d.health.get("phi_mode")
    if mode == "hash":
        return _check(
            "sealing_key",
            DATA,
            label,
            "not_applicable",
            "Nothing is kept in this mode, so there is nothing to seal.",
        )
    keys = d.health.get("phi_keys") or {}
    if mode != "encrypt" or not keys.get("current"):
        return _check(
            "sealing_key",
            DATA,
            label,
            "not_set",
            "No sealing key is set: nothing this deployment keeps is encrypted.",
            "Set patient data mode to encrypt, with its key, before any real data.",
        )
    earlier = len(keys.get("read") or [])
    also = (
        f" {earlier} earlier key{'s are' if earlier != 1 else ' is'} still read while a rotation "
        "finishes."
        if earlier
        else ""
    )
    return _check(
        "sealing_key",
        DATA,
        label,
        "ok",
        f"The key that seals kept inputs and notes is set (key id {keys['current']}).{also} Keep "
        "a copy of it where your organization keeps its secrets: without it, what was sealed "
        "cannot be read.",
    )


# -- the database and the audit trail -------------------------------------------------------------


def _database(d: Deployment) -> SystemCheck:
    label = "Database"
    storage = "Postgres" if d.health.get("storage") == "postgres" else "SQLite"
    state = (d.ready.get("checks") or {}).get("database")
    if state != "ok":
        return _check(
            "database",
            AUDIT,
            label,
            "attention",
            "The database did not answer when it was last asked.",
            "Check that the database is running and that this server can reach it, then reload.",
        )
    if storage == "SQLite" and d.health.get("sign_in"):
        return _check(
            "database",
            AUDIT,
            label,
            "attention",
            "The audit trail is kept in a single SQLite file: fine for trying Mendari out, not "
            "for a deployment people rely on.",
            "Run the deployment on Postgres (the one-machine install does), and back it up.",
        )
    return _check(
        "database",
        AUDIT,
        label,
        "ok",
        f"The database ({storage}) answers, and its tables were brought up to date when this "
        "version started.",
    )


def _audit_chain(d: Deployment) -> SystemCheck:
    label = "Audit trail verified"
    verified = _newest(d.runs.get("audit.verify"), d.runs.get("checkpoint"))
    if verified is None:
        return _check(
            "audit_chain",
            AUDIT,
            label,
            "attention",
            "The audit trail has not been verified on this deployment yet.",
            "Press Verify now. The daily checkpoint job verifies it every day after that.",
        )
    at = str(verified["ran_at"])
    if verified["outcome"] != "ok" and verified.get("detail") == "statement":
        # The daily job verified the chain, then refused the day's stored statement.
        return _check(
            "audit_chain",
            AUDIT,
            label,
            "attention",
            "The audit trail was checked from its last checkpoint and nothing was changed or "
            "removed, but the day's stored checkpoint statement is not one this deployment's key "
            "signed: someone with the database put it there.",
            "Keep the database as it is and follow the incident runbook.",
            at=at,
        )
    if verified["outcome"] != "ok":
        return _check(
            "audit_chain",
            AUDIT,
            label,
            "attention",
            "The audit trail did not verify: an event was changed, removed or added out of "
            "order since the last good check.",
            "Hold all automation below, keep the database as it is, and follow the incident "
            "runbook: restore from a backup taken before the break.",
            at=at,
        )
    whole = verified.get("detail") == "full"
    said = (
        "Every event of the audit trail was checked: nothing was changed or removed."
        if whole
        else "The audit trail was checked from its last checkpoint: nothing was changed or removed."
    )
    if _older(at, d.now):
        return _check(
            "audit_chain",
            AUDIT,
            label,
            "attention",
            f"{said} That was more than a day and a half ago.",
            "Press Verify now, and check that the daily jobs are running.",
            at=at,
        )
    return _check("audit_chain", AUDIT, label, "ok", said, at=at)


def _checkpoint(d: Deployment) -> SystemCheck:
    label = "Daily checkpoint"
    ran = d.runs.get("checkpoint")
    recorded = None if d.checkpoint is None else str(d.checkpoint["created_at"])
    if ran is not None and ran["outcome"] != "ok" and ran.get("detail") == "statement":
        return _check(
            "checkpoint",
            AUDIT,
            label,
            "attention",
            "The daily checkpoint job verified the audit trail and its position is recorded, but "
            "the day's stored statement did not verify (this deployment's key did not sign it), "
            "so it is not published.",
            "Keep the database as it is and follow the incident runbook.",
            at=str(ran["ran_at"]),
        )
    if ran is not None and ran["outcome"] != "ok":
        return _check(
            "checkpoint",
            AUDIT,
            label,
            "attention",
            "The daily checkpoint job could not record a checkpoint: the audit trail did not "
            "verify.",
            "See Audit trail verified, above.",
            at=str(ran["ran_at"]),
        )
    latest = max(
        (at for at in (recorded, None if ran is None else str(ran["ran_at"])) if at), default=None
    )
    if latest is None:
        if d.scheduled:
            return _check(
                "checkpoint",
                AUDIT,
                label,
                "attention",
                "The daily job has not recorded a checkpoint yet.",
                "It runs once a day: check that the scheduled jobs are running.",
            )
        return _check(
            "checkpoint",
            AUDIT,
            label,
            "not_set",
            "No checkpoint is recorded: a checkpoint marks how far the audit trail was verified, "
            "so that a later change to anything before it shows.",
            "Schedule the daily checkpoint (the one-machine install does).",
        )
    if _older(latest, d.now):
        return _check(
            "checkpoint",
            AUDIT,
            label,
            "attention",
            "The last checkpoint is more than a day and a half old.",
            "Check that the scheduled jobs are running.",
            at=latest,
        )
    said = (
        "The audit trail's position was recorded at the last daily check."
        if recorded == latest or ran is None
        else "The daily check ran and found nothing new to record since the last checkpoint."
    )
    return _check("checkpoint", AUDIT, label, "ok", said, at=latest)


def _checkpoint_signing(d: Deployment) -> SystemCheck:
    label = "Signed checkpoint"
    key = d.health.get("checkpoint_key")
    if not key:
        return _check(
            "checkpoint_signing",
            AUDIT,
            label,
            "not_set",
            "Checkpoints are not signed, so a copy kept outside the database cannot later prove "
            "that the audit trail was not rewritten.",
            "Set MENDARI_CHECKPOINT_KEY (the one-machine install generates it) and restart.",
        )
    if d.statement_day is None:
        return _check(
            "checkpoint_signing",
            AUDIT,
            label,
            "attention",
            f"A signing key is set (key id {key}), but no checkpoint has been signed with it yet.",
            "The daily checkpoint job signs one: check that the scheduled jobs are running.",
        )
    return _check(
        "checkpoint_signing",
        AUDIT,
        label,
        "ok",
        f"The checkpoint of {d.statement_day} is signed (key id {key}). Save a copy from the "
        "Audit trail view now and then, outside this machine: it is what proves later that the "
        "trail was not rewritten.",
    )


# -- the daily jobs -------------------------------------------------------------------------------


def _job_runs(d: Deployment) -> dict[str, Mapping[str, Any]]:
    """Each scheduled job's last run, for those that left one: as its route recorded it, else as
    the database shows it (a purge's chained summary, a recorded checkpoint), for a schedule
    that runs the commands instead of the routes. The monitor leaves nothing behind but its
    route's record."""
    found = {name: run for name, _ in JOBS if (run := d.runs.get(name)) is not None}
    purged = d.health.get("last_purge")
    if "retention" not in found and purged:
        found["retention"] = {"ran_at": purged["at"], "outcome": "ok"}
    if "checkpoint" not in found and d.checkpoint is not None:
        found["checkpoint"] = {"ran_at": d.checkpoint["created_at"], "outcome": "ok"}
    return found


def _scheduled_jobs(d: Deployment) -> SystemCheck:
    label = "Daily jobs"
    ran = _job_runs(d)
    words = dict(JOBS)
    failed = [name for name, run in ran.items() if run["outcome"] != "ok"]
    # Run from the command line, the monitor records nothing: without the scheduled routes its
    # absence says nothing, so it is not counted as missing.
    expected = [name for name, _ in JOBS if d.scheduled or name != "monitor"]
    missing = [name for name in expected if name not in ran]
    late = [
        name
        for name, run in ran.items()
        if run["outcome"] == "ok" and _older(str(run["ran_at"]), d.now)
    ]
    oldest = min((str(run["ran_at"]) for run in ran.values()), default=None)
    if failed:
        return _check(
            "scheduled_jobs",
            SCHEDULE,
            label,
            "attention",
            f"The last run failed for: {_listed(words[name] for name in failed)}.",
            "Read the scheduled jobs' log for why. If it is the checkpoint, see Audit trail "
            "verified, above.",
            at=max(str(ran[name]["ran_at"]) for name in failed),
        )
    if not ran and not d.scheduled:
        return _check(
            "scheduled_jobs",
            SCHEDULE,
            label,
            "not_set",
            "Nothing runs on a schedule here: old inputs are not deleted, no checkpoint is "
            "recorded and no agent is checked for drift.",
            "Schedule the three daily jobs. The one-machine install does it; elsewhere, call "
            "the scheduled routes every day with the jobs' secret (CRON_SECRET).",
        )
    if not ran:
        return _check(
            "scheduled_jobs",
            SCHEDULE,
            label,
            "attention",
            "The daily jobs are set up, and none has run yet. They run a few minutes after the "
            "deployment first starts, then once a day.",
            "If this still shows in an hour, check that the scheduled jobs are running, and "
            "read their log.",
        )
    if missing or late:
        said = []
        if late:
            said.append(f"Not run in the last day and a half: {_listed(words[n] for n in late)}.")
        if missing:
            said.append(f"No run on record yet: {_listed(words[n] for n in missing)}.")
        return _check(
            "scheduled_jobs",
            SCHEDULE,
            label,
            "attention",
            " ".join(said),
            "Check that the scheduled jobs are running, and read their log.",
            at=oldest,
        )
    drift = (
        " and every agent was checked for drift"
        if "monitor" in ran
        else "; the drift check leaves no record when it is run from the command line"
    )
    return _check(
        "scheduled_jobs",
        SCHEDULE,
        label,
        "ok",
        "In the last day, kept inputs past their retention were deleted, the audit trail's "
        f"checkpoint was recorded{drift}.",
        at=oldest,
    )


def _backup(d: Deployment) -> SystemCheck:
    label = "Backups"
    run = d.runs.get("backup")
    if run is None:
        return _check(
            "backup",
            SCHEDULE,
            label,
            "not_set",
            "No backup has been reported to this server, so it cannot say whether the database "
            "is backed up.",
            "The one-machine install backs up every night and reports here. Elsewhere, back the "
            "database up yourself and test a restore.",
        )
    at = str(run["ran_at"])
    if run["outcome"] != "ok":
        return _check(
            "backup",
            SCHEDULE,
            label,
            "attention",
            "The last backup failed.",
            "Read the scheduled jobs' log, free disk space if it is short, and run a backup now.",
            at=at,
        )
    if _older(at, d.now):
        return _check(
            "backup",
            SCHEDULE,
            label,
            "attention",
            "The last backup is more than a day and a half old.",
            "Check that the scheduled jobs are running, and run a backup now.",
            at=at,
        )
    return _check(
        "backup",
        SCHEDULE,
        label,
        "ok",
        "The database was backed up and the backup file was read back. Copy the backups off "
        "this machine: one on the same disk does not survive the disk.",
        at=at,
    )


# -- alerts and limits ----------------------------------------------------------------------------


def _alerts(d: Deployment) -> SystemCheck:
    label = "Alerts"
    alerts = d.alerts
    if not alerts.webhook:
        return _check(
            "alerts",
            ALERTS,
            label,
            "not_set",
            "No alert destination is set: a broken audit trail, a drifting agent or a used-up "
            "quota shows here and in the logs, and nobody is told.",
            "Set MENDARI_ALERT_WEBHOOK to the webhook of your on-call channel, restart, then "
            "send a test alert.",
        )
    if not alerts.signed:
        return _check(
            "alerts",
            ALERTS,
            label,
            "attention",
            "Alerts are sent unsigned, so whoever receives them cannot tell them from anyone "
            "else's posts.",
            "Set MENDARI_ALERT_SECRET (32 random characters or more) and restart.",
        )
    last, delivered = alerts.last, alerts.delivered
    if last is not None and last["status"] != "delivered":
        why = _alert_failure(last)
        return _check(
            "alerts",
            ALERTS,
            label,
            "attention",
            f"The last alert was not delivered ({why}).",
            "Check the webhook's address and that it accepts posts from this server, then send "
            "a test alert.",
            at=str(last["updated_at"]),
        )
    if delivered is None:
        return _check(
            "alerts",
            ALERTS,
            label,
            "attention",
            "An alert destination is set, but nothing has been delivered to it yet.",
            "Send a test alert, and check that it arrives.",
        )
    return _check(
        "alerts",
        ALERTS,
        label,
        "ok",
        "Alerts go to your webhook, signed, and the last one was delivered.",
        at=str(delivered["updated_at"]),
    )


def _alert_failure(last: Mapping[str, Any]) -> str:
    """Why an alert was not delivered, from its fixed code: the webhook's HTTP status, or that
    it gave no answer. Never an error's own words."""
    status = last.get("last_status")
    if isinstance(status, int):
        return f"the webhook answered HTTP {status}"
    return "the webhook did not answer"


def _limits(d: Deployment) -> SystemCheck:
    label = "Limits"
    limits = d.health.get("rate_limits") or {}
    mode = limits.get("mode")
    if not d.health.get("sign_in"):
        return _check(
            "limits",
            ALERTS,
            label,
            "not_applicable",
            "Limits count per caller, and with sign-in off every caller is the same one.",
        )
    if mode != "on":
        counted = "only counted, not enforced" if mode == "log" else "off"
        return _check(
            "limits",
            ALERTS,
            label,
            "attention",
            f"Rate limits are {counted}: one caller can take every decision this deployment "
            "can make.",
            "Set MENDARI_RATE_LIMITS=on (the default with sign-in) and restart.",
        )
    rates = limits.get("limits") or {}
    quotas = (d.health.get("org_quotas") or {}).get("daily_limits") or {}
    daily = (
        f" Each organization may make {quotas['decisions']} decisions a day."
        if "decisions" in quotas
        else " No daily quota per organization is set, which is fine for one organization."
    )
    return _check(
        "limits",
        ALERTS,
        label,
        "ok",
        f"Costly calls are limited: decisions {rates.get('decide', 'limited')} per caller, "
        f"signing in {rates.get('signin', 'limited')} per address.{daily}",
    )


# -- what it connects to --------------------------------------------------------------------------


def _ehr(d: Deployment) -> SystemCheck:
    label = "EHR connection"
    launch = bool(d.health.get("smart_launch"))
    registered = len(d.health.get("smart_ehrs") or [])
    clients = len(d.health.get("cds_clients") or [])
    if not launch and not registered and not clients:
        return _check(
            "ehr",
            CONNECTIONS,
            label,
            "not_set",
            "No EHR is connected: nothing launches Mendari from the EHR or calls it with the "
            "EHR's own tokens.",
            "When you are ready, register your EHR as the guide Connecting an EHR describes, "
            "and restart.",
        )
    if launch and not registered and d.health.get("sign_in"):
        return _check(
            "ehr",
            CONNECTIONS,
            label,
            "attention",
            "The launch from an EHR is on, but no EHR is registered for your organization: "
            "every launch is refused.",
            "Register the EHR's FHIR base (MENDARI_SMART_EHRS) and restart.",
        )
    counted = _listed(
        part
        for part in (
            f"{registered} EHR{'s' if registered != 1 else ''} registered to launch"
            if registered
            else "",
            f"{clients} calling with {'their' if clients != 1 else 'its'} own signed tokens"
            if clients
            else "",
        )
        if part
    )
    if not registered:
        return _check("ehr", CONNECTIONS, label, "ok", f"{counted[:1].upper()}{counted[1:]}.")
    checked = d.ehr_checked
    if checked is None:
        return _check(
            "ehr",
            CONNECTIONS,
            label,
            "attention",
            f"{counted[:1].upper()}{counted[1:]}. The connection has not been checked from here "
            "yet.",
            "Press Check the EHR connection.",
        )
    at = str(checked["ran_at"])
    if checked["outcome"] != "ok":
        return _check(
            "ehr",
            CONNECTIONS,
            label,
            "attention",
            f"{counted[:1].upper()}{counted[1:]}. The last check found something that would "
            "refuse its launches.",
            "Press Check the EHR connection and fix what it marks as failing.",
            at=at,
        )
    return _check(
        "ehr",
        CONNECTIONS,
        label,
        "ok",
        f"{counted[:1].upper()}{counted[1:]}. The last check found nothing that would refuse a "
        "launch.",
        at=at,
    )


def _registry(d: Deployment) -> SystemCheck:
    label = "Agent registry"
    registry = d.registry
    recalls = d.health.get("registry_recalls") or {}
    failing = [url for url, state in recalls.items() if state.get("failing")]
    if registry.origin is None and not recalls:
        return _check(
            "registry",
            CONNECTIONS,
            label,
            "not_set",
            "No agent registry is set, so no agent can be found and adopted from here.",
            "Set MENDARI_REGISTRY_URL to the registry's https address in the deployment's "
            "settings and restart. On a one-machine install, run ./mendari-site install again "
            "and give the address when it asks: nothing else changes.",
        )
    if failing:
        return _check(
            "registry",
            CONNECTIONS,
            label,
            "attention",
            "A registry that agents here came from could not be read for recalls: once that "
            "lasts a day, those agents stop automating.",
            "Check that this server can reach the registry over https.",
        )
    if registry.origin is not None and registry.reachable is not True:
        return _check(
            "registry",
            CONNECTIONS,
            label,
            "attention",
            f"The registry {registry.origin} did not answer when it was last asked.",
            "Check that this server can reach it over https, then reload.",
            at=registry.checked_at,
        )
    if registry.origin is not None and not d.agent_store:
        return _check(
            "registry",
            CONNECTIONS,
            label,
            "attention",
            f"The registry {registry.origin} answers, so agents can be found in the Agents "
            "view, but none can be added: your organization's agent store is off.",
            "Set MENDARI_AGENT_STORE=on in the deployment's settings and restart. It needs "
            "people managed in the console (MENDARI_ACCOUNTS=memberships).",
            at=registry.checked_at,
        )
    if registry.origin is not None:
        return _check(
            "registry",
            CONNECTIONS,
            label,
            "ok",
            f"The registry {registry.origin} answers: agents can be found and added in the "
            "Agents view.",
            at=registry.checked_at,
        )
    return _check(
        "registry",
        CONNECTIONS,
        label,
        "ok",
        "The registry that agents here came from is read for recalls.",
    )


def _agents(d: Deployment) -> SystemCheck:
    label = "Agents"
    invalid = sorted(d.health.get("invalid") or {})
    served = int(d.health.get("agents") or 0) if d.served is None else d.served
    if invalid:
        return _check(
            "agents",
            CONNECTIONS,
            label,
            "attention",
            f"{len(invalid)} agent{'s' if len(invalid) != 1 else ''} did not load: "
            f"{_listed(invalid)}.",
            "The server's log says why. Fix or remove the agent, then restart.",
        )
    if d.recalls:
        count = len(d.recalls)
        return _check(
            "agents",
            CONNECTIONS,
            label,
            "attention",
            f"{count} served version{'s were' if count != 1 else ' was'} recalled by "
            f"{'their' if count != 1 else 'its'} registry and no longer "
            f"automate{'' if count != 1 else 's'}.",
            "Adopt the fixed version from the Agents view.",
        )
    if d.demo_only:
        return _check(
            "agents",
            CONNECTIONS,
            label,
            "not_set",
            "Only the bundled demo agent is served. It answers from canned examples: it shows "
            "how Mendari works and decides nothing real.",
            "Adopt an agent from the Agents view.",
        )
    origins = d.health.get("agents_by_origin") or {}
    pulled = (
        int(origins.get("hub") or 0) if d.served_from_registry is None else d.served_from_registry
    )
    from_registry = f", {pulled} of them from a registry" if pulled else ""
    return _check(
        "agents",
        CONNECTIONS,
        label,
        "ok",
        f"{served} agent{'s are' if served != 1 else ' is'} served{from_registry}.",
    )


def _version(d: Deployment) -> SystemCheck:
    version = d.ready.get("version") or d.health.get("version")
    return _check(
        "version",
        ENGINE,
        "Engine version",
        "ok",
        f"This deployment runs Mendari {version}. Calendar days and displayed times use "
        f"{d.health.get('timezone', 'the default time zone')}.",
    )


# -- helpers --------------------------------------------------------------------------------------


def _check(
    key: str,
    area: str,
    label: str,
    state: State,
    detail: str,
    action: str | None = None,
    *,
    at: str | None = None,
) -> SystemCheck:
    return SystemCheck(
        key=key, area=area, label=label, state=state, detail=detail, action=action, at=at
    )


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _older(at: str, now: datetime, than: timedelta = OVERDUE) -> bool:
    """Whether the instant `at` (as the store writes them) is more than `than` before `now`. One
    that cannot be read is old: a time nobody can check is no evidence of a recent run."""
    try:
        then = datetime.fromisoformat(at)
    except ValueError:
        return True
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return now - then > than


def _newest(*runs: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """The most recent of some recorded runs; None for none."""
    found = [run for run in runs if run is not None]
    return max(found, key=lambda run: str(run["ran_at"]), default=None)


def _listed(items: Any) -> str:
    """`a`, `a and b`, `a, b and c`."""
    words = [str(item) for item in items]
    if len(words) < 2:
        return "".join(words)
    return f"{', '.join(words[:-1])} and {words[-1]}"


def _origin(url: str) -> str | None:
    """`scheme://host[:port]` of an address, never its path, query or credentials (a webhook's
    path holds its token); None for what is not an http(s) address."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname if ":" not in parts.hostname else f"[{parts.hostname}]"
    return f"{parts.scheme}://{host}{f':{port}' if port else ''}"


origin = _origin  # for the routes: the registry's origin as the checks show it


class RegistryProbe:
    """Whether the agent registry answers, asked in a thread of its own at most once every
    `REGISTRY_SECONDS`, so reading the checks never waits on another server: the first caller of
    an instance waits `REGISTRY_WAIT_S` at most for the first answer, and every later one gets
    the last answer at once while a new one is read. https only (http to this machine), as every
    read of a registry (`server/recalls.py`); what the registry says is never repeated."""

    def __init__(
        self,
        url: str | None,
        *,
        ask: Callable[[str], object] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.url = (url or "").strip().rstrip("/") or None
        self.origin = None if self.url is None else _origin(self.url)
        self._ask = ask
        self._clock = clock
        self._lock = threading.Lock()
        self._asked: float | None = None  # when the read in flight started
        self._answered: float | None = None
        self._reachable: bool | None = None
        self._at: str | None = None
        self._done = threading.Event()

    def _read(self) -> None:
        assert self.url is not None
        try:
            if self._ask is not None:
                self._ask(self.url)
            else:
                from mendari.server.recalls import fetch_signs

                fetch_signs(self.url)
            reachable = True
        except Exception:  # unreachable, refused, not https: one answer, never its words
            reachable = False
        with self._lock:
            self._reachable, self._answered, self._asked = reachable, self._clock(), None
            self._at = _iso(datetime.now(UTC))
        self._done.set()

    def state(self) -> Registry:
        """The registry as the checks show it, starting a read when one is due."""
        if self.url is None or self.origin is None:
            return Registry()
        if not is_https_or_local(self.url):
            return Registry(origin=self.origin, reachable=False)  # never asked over plain http
        waits = False
        with self._lock:
            now = self._clock()
            fresh = self._answered is not None and now - self._answered < REGISTRY_SECONDS
            reading = self._asked is not None and now - self._asked < 60.0
            if not fresh and not reading:
                # Only the caller that starts an instance's first read waits for it.
                waits = self._answered is None
                self._asked = now
                self._done.clear()
                try:
                    threading.Thread(
                        target=self._read, name="mendari-registry", daemon=True
                    ).start()
                except RuntimeError:  # no thread to be had: asked again next time
                    self._asked, waits = None, False
        if waits:
            self._done.wait(REGISTRY_WAIT_S)
        with self._lock:
            return Registry(origin=self.origin, reachable=self._reachable, checked_at=self._at)
