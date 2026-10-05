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
