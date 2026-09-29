"""Deployment settings read from the environment, shared by the CLI and the server entrypoints."""

from __future__ import annotations

import os
from collections.abc import Collection, Mapping, Sequence

from openhealth.errors import OpenHealthError

DEFAULT_JEV_DAILY_LIMIT = 500
DEFAULT_LLM_DAILY_LIMIT = 500


def parse_providers(values: Sequence[str] | None) -> dict[str, str]:
    """`slot=variant` pairs from flags, falling back to $OPENHEALTH_PROVIDERS (comma-separated).

    A setting can be scoped to one pack: `openhealth/clinical-trial-prescreener:notes=jev`.
    """
    items = list(values or [])
    if not items and os.environ.get("OPENHEALTH_PROVIDERS"):
        items = [v for v in os.environ["OPENHEALTH_PROVIDERS"].split(",") if v.strip()]
    overrides = {}
    for item in items:
        slot, sep, variant = item.partition("=")
        if not sep or not slot.strip() or not variant.strip():
            raise OpenHealthError(f"provider override {item!r} must look like slot=variant")
        overrides[slot.strip()] = variant.strip()
    return overrides


def daily_limits() -> dict[str, int | None]:
    """Per-provider-type daily call budgets for the remote, metered providers. `none` lifts a
    provider's limit; its calls are still counted."""
    limits: dict[str, int | None] = {}
    for provider, variable, default in (
        ("jev", "OPENHEALTH_JEV_DAILY_LIMIT", DEFAULT_JEV_DAILY_LIMIT),
        ("llm", "OPENHEALTH_LLM_DAILY_LIMIT", DEFAULT_LLM_DAILY_LIMIT),
    ):
        raw = os.environ.get(variable, str(default)).strip()
        if raw.lower() == "none":
            limits[provider] = None
        elif raw.isdigit():
            limits[provider] = int(raw)
        else:
            raise OpenHealthError(f"{variable} must be a non-negative integer or `none`")
    return limits


def providers_for(
    pack_id: str, slots: Mapping[str, Collection[str]], settings: Mapping[str, str]
) -> tuple[dict[str, str], list[str]]:
    """(the variants to use for this pack, settings that name this pack but cannot apply).

    `slots` maps the pack's slot names to their variants. An unscoped `slot=variant` applies
    only where the pack has that slot *and* variant, so one deployment-wide setting never
    breaks another pack; a pack-scoped setting wins over an unscoped one for the same slot.
    """
    chosen: dict[str, str] = {}
    skipped: list[str] = []
    for scoped in (False, True):
        for key, variant in settings.items():
            target, sep, slot = key.rpartition(":")
            if bool(sep) != scoped or (scoped and target != pack_id):
                continue
            if slot in slots and variant in slots[slot]:
                chosen[slot] = variant
            elif scoped or slot in slots:
                skipped.append(f"{key}={variant}")
    return chosen, skipped
