"""Dates as people see them, in the deployment's time zone (see `settings.display_timezone`).

Stored timestamps stay UTC ISO-8601: the audit hash chain covers them, and APIs and FHIR expect
unambiguous instants. Only calendar days ("today", "which day was this decision") and displayed
times use the local zone, so a decision at 9 PM Eastern counts on that Eastern day.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from mendari.settings import display_timezone


def zone() -> ZoneInfo:
    return display_timezone()


def today() -> date:
    return datetime.now(zone()).date()


def local_date(timestamp: str) -> str:
    """The local calendar day (YYYY-MM-DD) of a stored ISO-8601 timestamp with an offset."""
    return datetime.fromisoformat(timestamp).astimezone(zone()).date().isoformat()
