# SPDX-License-Identifier: Apache-2.0
"""Calendar days and "today" in the deployment's time zone; stored timestamps stay UTC."""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from mendari import AuditStore, Decision, Pack
from mendari.core import clock
from mendari.errors import MendariError
from mendari.standards.fhir import age_years
from tests.conftest import message


def test_the_default_zone_is_us_eastern(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MENDARI_TIMEZONE", raising=False)
    assert clock.zone().key == "America/New_York"
    monkeypatch.setenv("MENDARI_TIMEZONE", "Europe/Paris")
    assert clock.zone().key == "Europe/Paris"


def test_an_unknown_zone_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENDARI_TIMEZONE", "Eastern")
    with pytest.raises(MendariError, match="MENDARI_TIMEZONE"):
        clock.zone()


@pytest.mark.parametrize(
    ("stored", "eastern_day"),
    [
        ("2026-09-30T00:59:00.000+00:00", "2026-09-29"),  # 8:59 PM EDT
        ("2026-09-30T04:00:00.000+00:00", "2026-09-30"),  # midnight EDT
        ("2026-11-01T03:59:00.000+00:00", "2026-10-31"),  # 11:59 PM EDT, the night DST ends
        ("2026-11-01T05:30:00.000+00:00", "2026-11-01"),  # 1:30 AM EDT, before the switch
        ("2026-12-01T03:00:00.000+00:00", "2026-11-30"),  # 10 PM EST
        ("2026-03-08T04:59:00.000+00:00", "2026-03-07"),  # 11:59 PM EST, the night DST starts
        ("2026-03-08T05:00:00.000+00:00", "2026-03-08"),  # midnight EST
    ],
)
def test_stored_utc_timestamps_fall_on_eastern_days(
    stored: str, eastern_day: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MENDARI_TIMEZONE", raising=False)
    assert clock.local_date(stored) == eastern_day


def test_a_9_pm_decision_counts_on_that_eastern_day(
    triage_agent: Pack, store: AuditStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MENDARI_TIMEZONE", raising=False)
    result = Decision(triage_agent)(message("Can you send a refill for metformin?"))
    at_9_pm = dataclasses.replace(result, created_at="2026-09-30T01:00:00.000+00:00")
    store.record(at_9_pm, message("Can you send a refill for metformin?"))
    assert [day["date"] for day in store.stats()["daily"]] == ["2026-09-29"]
    # The stored timestamp is untouched (UTC), so the hash chain still verifies.
    stored = store.get(at_9_pm.id)
    assert stored is not None
    assert stored["record"]["created_at"] == "2026-09-30T01:00:00.000+00:00"
    assert store.verify()["ok"]
    monkeypatch.setenv("MENDARI_TIMEZONE", "UTC")
    assert [day["date"] for day in store.stats()["daily"]] == ["2026-09-30"]


def test_daily_call_counters_use_the_local_day(
    store: AuditStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mendari.core.clock.today", lambda: date(2026, 9, 29))
    store.consume("llm", daily_limit=None)
    store.consume("llm", daily_limit=None)
    assert store.usage("llm") == 2
    monkeypatch.setattr("mendari.core.clock.today", lambda: date(2026, 9, 30))
    assert store.usage("llm") == 0  # a new local day starts at local midnight


def test_ages_use_the_local_day(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mendari.core.clock.today", lambda: date(2026, 9, 29))
    assert age_years("2008-09-30") == 17
    monkeypatch.setattr("mendari.core.clock.today", lambda: date(2026, 9, 30))
    assert age_years("2008-09-30") == 18
