# SPDX-License-Identifier: Apache-2.0
"""Deployment settings read from the environment, shared by the CLI and the server entrypoints."""

from __future__ import annotations

import os
from collections.abc import Collection, Mapping, Sequence
from typing import Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mendari.errors import MendariError
from mendari.legacy import current_pack_id

DEFAULT_JEV_DAILY_LIMIT = 500
DEFAULT_LLM_DAILY_LIMIT = 500
DEFAULT_TIMEZONE = "America/New_York"
DEFAULT_MAX_BODY_BYTES = 4 * 1024 * 1024
MIN_MAX_BODY_BYTES = 64 * 1024
DEFAULT_RETENTION_DAYS = 30
MAX_RETENTION_DAYS = 36500  # a century: anything longer is a typo, and overflows a date
# An LLM call at its 30 s default with one retry takes about 62 s; Vercel stops a function at
# `maxDuration` (300 s, vercel.json), and the longest deadline leaves 30 s for the audit write.
DEFAULT_DECISION_DEADLINE_S = 120
MIN_DECISION_DEADLINE_S = 10
MAX_DECISION_DEADLINE_S = 270
FhirValidation = Literal["off", "warn", "enforce"]


def display_timezone() -> ZoneInfo:
    """The time zone people see: calendar days, daily counters and displayed times
    ($MENDARI_TIMEZONE, an IANA name; default US Eastern). Stored timestamps stay UTC."""
    name = os.environ.get("MENDARI_TIMEZONE", "").strip() or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise MendariError(f"MENDARI_TIMEZONE {name!r} is not an IANA time zone") from None


def deployment_place() -> tuple[str, str]:
    """Where this server runs and which release it is, for the deployment registry:
    $MENDARI_ENVIRONMENT (else Vercel's environment, else "local") and $MENDARI_RELEASE
    (else the Vercel commit, else the package version)."""
    from mendari import __version__

    environment = (
        os.environ.get("MENDARI_ENVIRONMENT", "").strip()
        or os.environ.get("VERCEL_ENV", "").strip()
        or "local"
    )
    release = (
        os.environ.get("MENDARI_RELEASE", "").strip()
        or os.environ.get("VERCEL_GIT_COMMIT_SHA", "").strip()[:12]
        or f"mendari {__version__}"
    )
    return environment, release


def pas_payer_url() -> str:
    """The payer's FHIR base for Da Vinci PAS submissions ($MENDARI_PAS_PAYER_URL); it must
    also be on MENDARI_FHIR_ALLOWED_SERVERS."""
    from mendari.standards.pas import PasError

    url = os.environ.get("MENDARI_PAS_PAYER_URL", "").strip().rstrip("/")
    if not url:
        raise PasError("set MENDARI_PAS_PAYER_URL to submit prior-authorization requests")
    return url


def pas_codes() -> dict[str, str]:
    """The X12 codes this site sends in Da Vinci PAS requests, by name, from its settings
    (`pas.X12_SETTINGS`: $MENDARI_PAS_REQUEST_TYPE, $MENDARI_PAS_CERTIFICATION_TYPE and the
    others); only those set. Mendari ships no X12 code value: these are the site's."""
    from mendari.standards.pas import X12_CODE, X12_SETTINGS, PasError

    codes = {}
    for name, variable in X12_SETTINGS.items():
        value = os.environ.get(variable, "").strip()
        if not value:
            continue
        if not X12_CODE.fullmatch(value):
            raise PasError(f"{variable} must be an X12 code: 1 to 3 capital letters or digits")
        codes[name] = value
    return codes


def sandbox_payer_review_action() -> str | None:
    """The X12 review action code (306) the sandbox's mock payer answers every request with
    ($MENDARI_SANDBOX_PAYER_REVIEW_ACTION); unset: none, and its answers carry no review action
    code, which PAS allows."""
    from mendari.standards.pas import REVIEW_ACTION

    value = os.environ.get("MENDARI_SANDBOX_PAYER_REVIEW_ACTION", "").strip()
    if value and not REVIEW_ACTION.fullmatch(value):
        raise MendariError(
            "MENDARI_SANDBOX_PAYER_REVIEW_ACTION must be an X12 code: 1 to 3 capital letters or "
            "digits"
        )
    return value or None


def fhir_validation() -> FhirValidation:
    """US Core checks on FHIR inputs ($MENDARI_FHIR_VALIDATION): `warn` (default) returns and
    audits the issues without changing the decision; `enforce` blocks a non-conforming input
    before any provider sees it; `off` skips the checks."""
    mode = os.environ.get("MENDARI_FHIR_VALIDATION", "").strip().lower() or "warn"
    if mode not in ("off", "warn", "enforce"):
        raise MendariError("MENDARI_FHIR_VALIDATION must be off, warn or enforce")
    return cast(FhirValidation, mode)


def max_body_bytes() -> int:
    """The largest request body the server takes ($MENDARI_MAX_BODY_BYTES; default 4 MiB, under
    Vercel's 4.5 MB, and room for real FHIR Bundles). At least 64 KiB, so a value meant in
    another unit cannot refuse every request."""
    raw = os.environ.get("MENDARI_MAX_BODY_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_BODY_BYTES
    if not raw.isdigit() or int(raw) < MIN_MAX_BODY_BYTES:
        raise MendariError(
            f"MENDARI_MAX_BODY_BYTES must be a number of bytes, {MIN_MAX_BODY_BYTES} or more"
        )
    return int(raw)


def decision_deadline_s() -> float:
    """How long one decision's remote calls may take together ($MENDARI_DECISION_DEADLINE_S,
    whole seconds, 10 to 270; default 120): a call that would start after it is not made, and
    one under way waits at most what remains, either one the provider error `timeout`
    (`providers/deadline.py`). Local providers always answer. A pack cannot set it."""
    raw = os.environ.get("MENDARI_DECISION_DEADLINE_S", "").strip()
    if not raw:
        return float(DEFAULT_DECISION_DEADLINE_S)
    whole = raw.isascii() and raw.isdigit()  # `isdigit` alone takes "²", which `int` refuses
    if not whole or not MIN_DECISION_DEADLINE_S <= int(raw) <= MAX_DECISION_DEADLINE_S:
        raise MendariError(
            "MENDARI_DECISION_DEADLINE_S must be a whole number of seconds, "
            f"{MIN_DECISION_DEADLINE_S} to {MAX_DECISION_DEADLINE_S}"
        )
    return float(raw)


def retention_days() -> int:
    """How long kept inputs and notes are kept ($MENDARI_RETENTION_DAYS; default 30): what the
    scheduled purge (`/api/internal/retention`) and `mendari phi purge` delete after. At least
    1: deleting everything is asked for on the command line (`--days 0 --yes`), never by a
    setting a typo could make 0."""
    raw = os.environ.get("MENDARI_RETENTION_DAYS", "").strip()
    if not raw:
        return DEFAULT_RETENTION_DAYS
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_RETENTION_DAYS:
        raise MendariError(
            f"MENDARI_RETENTION_DAYS must be a whole number of days, 1 to {MAX_RETENTION_DAYS}"
        )
    return int(raw)


def status_org() -> str | None:
    """The organization the status checks decide in ($MENDARI_STATUS_ORG; unset: none), which
    the monitor's schedule leaves out: its decisions are synthetic (docs/operations/
    status-checks.md). An id no organization could have fails at startup."""
    from mendari.core.accounts import is_org_id

    raw = os.environ.get("MENDARI_STATUS_ORG", "").strip()
    if not raw:
        return None
    if not is_org_id(raw):
        raise MendariError(
            "MENDARI_STATUS_ORG must be an organization's id: lower-case letters, digits and "
            "hyphens"
        )
    return raw


def database_target() -> str:
    """The hosted server's database (`mendari.server.vercel`): $DATABASE_URL, required on
    Vercel; elsewhere `var/mendari.db`."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url
    if os.environ.get("VERCEL"):
        raise MendariError(
            "DATABASE_URL is not set. Connect a Postgres database (e.g. Neon) to this Vercel "
            "project for Production and Preview, then redeploy."
        )
    return "var/mendari.db"


def enabled_packs() -> frozenset[str]:
    """$MENDARI_PACKS: the packs this server serves (comma-separated ids); empty: every pack
    found. The per-pack container images set it to their one pack."""
    raw = os.environ.get("MENDARI_PACKS", "")
    return frozenset(current_pack_id(item.strip()) for item in raw.split(",") if item.strip())


def community_packs_enabled() -> bool:
    """$MENDARI_COMMUNITY_PACKS: `on` serves the packs in `community-packs/` (contributed by
    pull request; they never automate); `off` or unset (the default) serves none of them, even
    where `MENDARI_PACKS_PATH` names that folder."""
    raw = os.environ.get("MENDARI_COMMUNITY_PACKS", "").strip().lower()
    if raw not in ("", "on", "off"):
        raise MendariError("MENDARI_COMMUNITY_PACKS must be on or off")
    return raw == "on"


def parse_providers(values: Sequence[str] | None) -> dict[str, str]:
    """`slot=variant` pairs from flags, falling back to $MENDARI_PROVIDERS (comma-separated).

    A setting can be scoped to one pack: `mendari/clinical-trial-prescreener:notes=jev`.
    """
    items = list(values or [])
    if not items and os.environ.get("MENDARI_PROVIDERS"):
        items = [v for v in os.environ["MENDARI_PROVIDERS"].split(",") if v.strip()]
    overrides = {}
    for item in items:
        slot, sep, variant = item.partition("=")
        if not sep or not slot.strip() or not variant.strip():
            raise MendariError(f"provider override {item!r} must look like slot=variant")
        pack, scoped, name = slot.strip().rpartition(":")
        key = f"{current_pack_id(pack.strip())}:{name.strip()}" if scoped else name.strip()
        overrides[key] = variant.strip()
    return overrides


# With Jev set up, a server runs the reference packs that automate under their own policy (risk
# levels 0-1) as Mendari + Jev: each policy's `automation_requires` asks for a model in its
# screen, and Jev reads there beside the keyword rules, so the rules still answer when Jev
# cannot. The inbox's router and the referral's specialty reader are naive Bayes, asking Jev when
# unsure (`cascade`): when Jev cannot answer, they still suggest a team or a specialty. At risk
# level 2 (the trial prescreener, AI Guard) automation rests on an approval of the
# configuration that runs, so a deployment names those packs' readers itself.
JEV_DEFAULTS: dict[str, dict[str, str]] = {
    "mendari/patient-inbox-router": {"safety_screen": "rules_and_jev", "router": "cascade"},
    "mendari/referral-router": {"triage_screen": "rules_and_jev", "specialty_reader": "cascade"},
    "mendari/prior-auth-preflight": {"notes": "rules_and_jev"},
}


def default_providers() -> dict[str, dict[str, str]]:
    """The variants a server runs, by pack, in each slot its provider settings choose nothing for:
    JEV_DEFAULTS while Jev is set up, else none (each pack's own default). Only servers use them:
    a command that decides or evaluates (a benchmark, a pack's tests) never calls Jev unasked."""
    from mendari.providers import jev

    if not jev.deployment_configured():
        return {}
    return {pack: dict(slots) for pack, slots in JEV_DEFAULTS.items()}


def daily_limits() -> dict[str, int | None]:
    """Per-provider-type daily call budgets for the remote, metered providers. `none` lifts a
    provider's limit; its calls are still counted."""
    limits: dict[str, int | None] = {}
    for provider, variable, default in (
        ("jev", "MENDARI_JEV_DAILY_LIMIT", DEFAULT_JEV_DAILY_LIMIT),
        ("llm", "MENDARI_LLM_DAILY_LIMIT", DEFAULT_LLM_DAILY_LIMIT),
    ):
        raw = os.environ.get(variable, str(default)).strip()
        if raw.lower() == "none":
            limits[provider] = None
        elif raw.isdigit():
            limits[provider] = int(raw)
        else:
            raise MendariError(f"{variable} must be a non-negative integer or `none`")
    return limits


ORG_METRICS = ("decisions", "jev", "llm")


def org_daily_limits() -> dict[str, int]:
    """$MENDARI_ORG_DAILY_LIMITS: what each organization may use a day, e.g.
    `decisions=5000,jev=2000,llm=200` (decisions, and the remote calls they make by provider
    type; evaluations never count). Unset: no per-organization quota. Over a quota a decision
    falls back to human review, as over a provider's daily budget."""
    limits: dict[str, int] = {}
    for item in os.environ.get("MENDARI_ORG_DAILY_LIMITS", "").split(","):
        if not item.strip():
            continue
        metric, sep, value = (part.strip() for part in item.partition("="))
        if not sep or metric not in ORG_METRICS or not value.isdigit() or metric in limits:
            raise MendariError(
                "MENDARI_ORG_DAILY_LIMITS must look like decisions=5000,jev=2000,llm=200 "
                f"(each of {', '.join(ORG_METRICS)} at most once, a non-negative integer)"
            )
        limits[metric] = int(value)
    return limits


def principal_daily_limit() -> int | None:
    """$MENDARI_PRINCIPAL_DAILY_LIMIT: how many decisions each principal (a person, an API key, a
    Backend Services client, an EHR launch) may ask for a local calendar day, beside its
    organization's quota (ADR-0008): one per request that decides, one per draft order of a CDS
    Hooks order hook. Past it the request is refused (`429`, until midnight) before any work.
    Unset: no cap. Sign-in only: open mode has no principals."""
    raw = os.environ.get("MENDARI_PRINCIPAL_DAILY_LIMIT", "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        raise MendariError(
            "MENDARI_PRINCIPAL_DAILY_LIMIT must be a non-negative integer: the decisions each "
            "principal may ask for a day"
        )
    return int(raw)


def applies_to(key: str, pack_id: str, slots: Collection[str], *, community: bool) -> bool:
    """Whether a provider setting (`slot` or `namespace/name:slot`) names a slot of this pack: an
    unscoped one any non-community pack with that slot, a scoped one only its own pack."""
    target, sep, slot = key.rpartition(":")
    if sep:
        return target == pack_id and slot in slots
    return not community and slot in slots


def providers_for(
    pack_id: str,
    slots: Mapping[str, Collection[str]],
    settings: Mapping[str, str],
    *,
    community: bool = False,
) -> tuple[dict[str, str], list[str]]:
    """(the variants to use for this pack, settings that name this pack but cannot apply).

    `slots` maps the pack's slot names to their variants. An unscoped `slot=variant` applies
    only where the pack has that slot *and* variant, so one deployment-wide setting never
    breaks another pack; a pack-scoped setting wins over an unscoped one for the same slot.
    A `community` pack takes pack-scoped settings only: an unscoped one does not name it, so a
    deployment pays for a community pack's remote calls only where it chose them by name.
    """
    chosen: dict[str, str] = {}
    skipped: list[str] = []
    for scoped in (False, True) if not community else (True,):
        for key, variant in settings.items():
            target, sep, slot = key.rpartition(":")
            if bool(sep) != scoped or (scoped and target != pack_id):
                continue
            if slot in slots and variant in slots[slot]:
                chosen[slot] = variant
            elif scoped or slot in slots:
                skipped.append(f"{key}={variant}")
    return chosen, skipped
