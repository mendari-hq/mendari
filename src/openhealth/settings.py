"""Deployment settings read from the environment, shared by the CLI and the server entrypoints."""

from __future__ import annotations

import os
from collections.abc import Sequence

from openhealth.errors import OpenHealthError

DEFAULT_JEV_DAILY_LIMIT = 500


def parse_providers(values: Sequence[str] | None) -> dict[str, str]:
    """`slot=variant` pairs from flags, falling back to $OPENHEALTH_PROVIDERS (comma-separated)."""
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


def daily_limits() -> dict[str, int]:
    """Per-provider-type daily call budgets. Only remote, metered providers are limited."""
    raw = os.environ.get("OPENHEALTH_JEV_DAILY_LIMIT", str(DEFAULT_JEV_DAILY_LIMIT)).strip()
    if not raw.isdigit():
        raise OpenHealthError("OPENHEALTH_JEV_DAILY_LIMIT must be a non-negative integer")
    return {"jev": int(raw)}
