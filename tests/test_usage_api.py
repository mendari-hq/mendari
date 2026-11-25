# SPDX-License-Identifier: Apache-2.0
"""What an organization spent (LP-21i): `GET /api/usage` gives its administrators their own
usage ledger, by day, provider type and purpose, with totals, a cost only where every call was
priced; the operator reads every organization's from the command line (`mendari usage`)."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore
from mendari.cli import main
from mendari.core import clock
from mendari.core.audit import usage_totals
from mendari.server.app import create_app
from tests.conftest import encrypt_mode, signed_in
from tests.test_hosted_data import sign_in_env

# The day the tests run on, pinned (`pinned_day`): a run that crosses local midnight still
# agrees with the server about which days are the last 30.
DAY = date(2026, 10, 8)


def seed(store: AuditStore, *rows: tuple[str, str, str, str, int, int, float]) -> None:
    """Ledger rows as decisions and evaluations would leave them: (day, org, provider type,
    purpose, calls, priced calls, cost of the priced ones), 100 tokens in and 10 out a call."""
    with store._db.session(exclusive=True) as db:
        for day, org, provider, purpose, calls, priced, cost in rows:
            db.all(
                "INSERT INTO usage_ledger (day, org, provider_type, purpose, calls, "
                "priced_calls, input_tokens, output_tokens, cost_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (day, org, provider, purpose, calls, priced, 100 * calls, 10 * calls, cost),
            )


@pytest.fixture(autouse=True)
def pinned_day(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clock, "today", lambda: DAY)


def days_ago(n: int) -> str:
    return (clock.today() - timedelta(days=n)).isoformat()


def seeded(store: AuditStore) -> None:
    seed(
        store,
        (days_ago(0), "clinic-a", "agent", "decision", 10, 10, 0.004),
        (days_ago(1), "clinic-a", "agent", "decision", 5, 5, 0.002),
        (days_ago(1), "clinic-a", "llm", "evaluation", 4, 3, 0.03),  # one call unpriced
        (days_ago(40), "clinic-a", "agent", "decision", 99, 99, 9.0),  # outside 30 days
        (days_ago(0), "clinic-b", "agent", "decision", 7, 7, 0.003),
    )


def test_an_administrator_sees_their_organization_s_usage_and_its_totals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    encrypt_mode(monkeypatch)
    sign_in_env(monkeypatch)
    client = TestClient(create_app(db_path=tmp_path / "a.db"), base_url="https://mendari.test")
    store = AuditStore(tmp_path / "a.db")
    seeded(store)
    admin = signed_in(store, role="admin", org="clinic-a")
    answer = client.get("/api/usage", headers=admin)
    assert answer.status_code == 200, answer.text
    usage = answer.json()
    assert (usage["since"], usage["until"]) == (days_ago(29), days_ago(0))
    assert {row["org"] for row in usage["rows"]} == {"clinic-a"}  # never clinic-b's
    assert len(usage["rows"]) == 3  # not the day 40 days ago
    by = {(t["provider_type"], t["purpose"]): t for t in usage["totals"]}
    assert by[("agent", "decision")]["calls"] == 15
    assert by[("agent", "decision")]["cost_usd"] == pytest.approx(0.006)
    unpriced = by[("llm", "evaluation")]
    assert (unpriced["calls"], unpriced["priced_calls"], unpriced["cost_usd"]) == (4, 3, None)
    assert len(client.get("/api/usage", params={"days": 1}, headers=admin).json()["rows"]) == 1
    for days in (0, 367):
        assert client.get("/api/usage", params={"days": days}, headers=admin).status_code == 422
    for role in ("viewer", "reviewer", "governance"):
        refused = client.get("/api/usage", headers=signed_in(store, role=role, org="clinic-a"))
        assert refused.status_code == 403, role


def test_open_mode_shows_every_organization_each_row_naming_its_own(tmp_path: Path) -> None:
    client = TestClient(create_app(db_path=tmp_path / "a.db"))
    seeded(AuditStore(tmp_path / "a.db"))
    rows = client.get("/api/usage").json()["rows"]
    assert {row["org"] for row in rows} == {"clinic-a", "clinic-b"}


def test_totals_show_a_cost_only_where_every_call_was_priced() -> None:
    row = {"provider_type": "agent", "purpose": "decision", "calls": 2, "priced_calls": 2,
           "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.5}  # fmt: skip
    assert usage_totals([row, row])[0]["cost_usd"] == 1.0
    unknown = {**row, "priced_calls": 1, "cost_usd": None}
    for rows in ([row, unknown], [unknown, row]):
        (total,) = usage_totals(rows)
        assert (total["calls"], total["priced_calls"], total["cost_usd"]) == (4, 3, None)


def test_the_operator_reads_every_organization_s_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "a.db"
    with AuditStore(db) as store:
        seeded(store)

    def usage(*argv: str) -> dict[str, Any]:
        assert main(["usage", "--db", str(db), *argv]) == 0
        ledger: dict[str, Any] = json.loads(capsys.readouterr().out)["ledger"]
        return ledger

    every = usage()
    assert (every["since"], every["until"]) == (days_ago(29), days_ago(0))
    assert len(every["rows"]) == 4  # not the day 40 days ago
    assert {row["org"] for row in every["rows"]} == {"clinic-a", "clinic-b"}
    by = {(t["provider_type"], t["purpose"]): t for t in every["totals"]}
    assert by[("agent", "decision")]["calls"] == 22
    assert by[("llm", "evaluation")]["cost_usd"] is None
    assert {row["org"] for row in usage("--org", "clinic-b")["rows"]} == {"clinic-b"}
    assert len(usage("--days", "1")["rows"]) == 2
    assert len(usage("--days", "60")["rows"]) == 5
    for days in ("0", "367"):
        assert main(["usage", "--db", str(db), "--days", days]) == 1
        assert "--days is 1 to 366" in capsys.readouterr().err
