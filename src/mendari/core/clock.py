"""Dates as people see them, in the deployment's time zone (see `settings.display_timezone`).

Stored timestamps stay UTC ISO-8601: the audit hash chain covers them, and APIs and FHIR expect
unambiguous instants. Only calendar days ("today", "which day was this decision") and displayed
times use the local zone, so a decision at 9 PM Eastern counts on that Eastern day.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from mendari.settings import display_timezone


def zone() -> ZoneInfo:
    return display_timezone()


def today() -> date:
    return datetime.now(zone()).date()


def local_date(timestamp: str) -> str:
    """The local calendar day (YYYY-MM-DD) of a stored ISO-8601 timestamp with an offset."""
    return datetime.fromisoformat(timestamp).astimezone(zone()).date().isoformat()


def seconds_until_tomorrow() -> int:
    """Whole seconds until the next local midnight, when daily budgets and quotas start over
    (at least 1: a `Retry-After`)."""
    now = datetime.now(zone())
    midnight = datetime.combine(now.date() + timedelta(days=1), time(), tzinfo=zone())
    # In UTC: the same zone on both sides would subtract wall-clock times, wrong on DST days.
    return max(1, math.ceil((midnight.astimezone(UTC) - now.astimezone(UTC)).total_seconds()))
