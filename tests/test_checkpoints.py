# SPDX-License-Identifier: Apache-2.0
"""Audit checkpoints (LP-21j): `record_checkpoint` verifies from the last checkpoint and records
the head as the next, outside the chain; `verify(since_checkpoint=True)` reads only what came
after it; a full verification checks every checkpoint, so a chain rewritten from before one, or
cut short of it, is caught. On SQLite and Postgres (`db_target`)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from mendari import AuditStore, Decision, Pack
from mendari.cli import main
from mendari.core.audit import GENESIS, _chain_hash
from tests.conftest import message
from tests.test_chain_orgs import tamper


def decide(store: AuditStore, pack: Pack, count: int) -> list[str]:
    decision = Decision(pack, store=store)
    return [decision(message(f"Refill number {n}, please.")).id for n in range(count)]


def rewrite_from(store: AuditStore, seq: int) -> None:
    """An attacker with the database rewrites the event at `seq` and recomputes every hash
    after it, so the chain holds from the start, and every record's digest with it."""
    with store._db.session(exclusive=True) as db:
        events = db.all("SELECT * FROM audit_events ORDER BY seq")
        prev = GENESIS
        for event in events:
            payload = event["payload"]
            if event["seq"] == seq:
                payload = json.dumps({**json.loads(payload), "forged": True})
            chained = _chain_hash(
                prev, event["decision_id"], event["event"], payload, event["created_at"]
            )
            db.all(
                "UPDATE audit_events SET payload = ?, prev_hash = ?, hash = ? WHERE seq = ?",
                (payload, prev, chained, event["seq"]),
            )
            prev = chained


def test_a_checkpoint_is_the_verified_head_and_later_checks_start_from_it(
    db_target: str, inbox_pack: Pack
) -> None:
    with AuditStore(db_target) as store:
        assert store.record_checkpoint() == {
            "ok": True,
            "events": 0,
            "head": GENESIS,
            "recorded": None,  # an empty chain: nothing to record
        }
        decide(store, inbox_pack, 3)
        first = store.record_checkpoint()
        full = store.verify()
        assert first["ok"] and first["recorded"]["hash"] == full["head"]
        (recorded,) = store.checkpoints()
        assert recorded == first["recorded"]
        again = store.record_checkpoint()  # nothing new: the same head, no second row
        assert again["recorded"] is None and again["events"] == 0
        decide(store, inbox_pack, 2)
        since = store.verify(since_checkpoint=True)
        assert since["ok"] and since["events"] == 2 and since["head"] == store.verify()["head"]
        assert since["checkpoint"] == recorded
        second = store.record_checkpoint()
        assert second["events"] == 2 and [c["seq"] for c in store.checkpoints()] == [
            recorded["seq"],
            second["recorded"]["seq"],
        ]
        latest = store.checkpoints()[-1]  # from now on, every check starts at the newest
        assert latest == second["recorded"]
        since = store.verify(since_checkpoint=True)
        assert since["checkpoint"] == latest and since["events"] == 0
        again = store.record_checkpoint()
        assert again["recorded"] is None and again["events"] == 0
        assert len(store.checkpoints()) == 2
        assert store.verify()["ok"]  # a full verification checks both


def test_a_change_after_the_checkpoint_is_caught_and_one_before_by_a_full_verification(
    db_target: str, inbox_pack: Pack
) -> None:
    with AuditStore(db_target) as store:
        before = decide(store, inbox_pack, 2)
        (checkpoint,) = [store.record_checkpoint()["recorded"]]
        after = decide(store, inbox_pack, 2)
        kept = record_of(store, after[0])
        tamper(store, "UPDATE decisions SET record = ? WHERE id = ?", ('{"x": 1}', after[0]))
        since = store.verify(since_checkpoint=True)
        assert not since["ok"] and since["broken_at"] > checkpoint["seq"]
        assert store.record_checkpoint()["recorded"] is None  # broken: nothing recorded
        assert store.checkpoints() == [checkpoint]
        tamper(store, "UPDATE decisions SET record = ? WHERE id = ?", (kept, after[0]))
        assert store.verify(since_checkpoint=True)["ok"]
        # Before the checkpoint: not read again from it (its limit), read by a full one.
        tamper(store, "UPDATE decisions SET record = ? WHERE id = ?", ('{"x": 1}', before[0]))
        assert store.verify(since_checkpoint=True)["ok"]
        assert not store.verify()["ok"]


def record_of(store: AuditStore, decision_id: str) -> str:
    with store._db.session(exclusive=False) as db:
        row = db.one("SELECT record FROM decisions WHERE id = ?", (decision_id,))
    assert row is not None
    return str(row["record"])


def test_a_chain_rewritten_from_before_a_checkpoint_or_cut_short_of_it_is_caught(
    db_target: str, inbox_pack: Pack
) -> None:
    with AuditStore(db_target) as store:
        decide(store, inbox_pack, 3)
        (checkpoint,) = [store.record_checkpoint()["recorded"]]
        decide(store, inbox_pack, 1)
        rewrite_from(store, 1)  # every hash recomputed: the chain alone holds
        full = store.verify()
        assert (full["ok"], full["reason"], full["broken_at"]) == (
            False,
            "checkpoint",
            checkpoint["seq"],
        )
        since = store.verify(since_checkpoint=True)
        assert (since["ok"], since["reason"]) == (False, "checkpoint")
    with AuditStore(db_target) as store:
        tamper(store, "DELETE FROM audit_events WHERE seq >= ?", (checkpoint["seq"],))
        cut = store.verify()
        assert (cut["ok"], cut["reason"], cut["broken_at"]) == (
            False,
            "checkpoint",
            checkpoint["seq"],
        )


def test_a_database_from_before_checkpoints_is_verified_whole(
    tmp_path: Path, inbox_pack: Pack
) -> None:
    db = tmp_path / "old.db"
    with AuditStore(db) as store:
        decide(store, inbox_pack, 2)
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute("DROP TABLE audit_checkpoints")
        connection.execute("PRAGMA journal_mode=DELETE")
    with AuditStore(db, read_only=True) as old:
        assert old.verify(since_checkpoint=True) == old.verify()
        assert old.checkpoints() == []


def test_the_commands_record_a_checkpoint_and_verify_from_it(
    tmp_path: Path, inbox_pack: Pack, capsys: pytest.CaptureFixture[str]
) -> None:
    db = str(tmp_path / "a.db")
    with AuditStore(db) as store:
        decide(store, inbox_pack, 2)

    def run(*argv: str) -> tuple[int, Any]:
        code = main(list(argv))
        out = capsys.readouterr().out
        return code, json.loads(out) if out.strip() else None

    code, recorded = run("audit", "checkpoint", "--db", db)
    assert code == 0 and recorded["recorded"]["seq"] >= 1
    with AuditStore(db) as store:
        decide(store, inbox_pack, 1)
    code, since = run("audit", "verify", "--db", db, "--since-checkpoint")
    assert code == 0 and since["events"] == 1
    assert main(["audit", "verify", "--db", db, "--notes", "--since-checkpoint"]) == 1
    assert "not allowed with argument" in capsys.readouterr().err
    with AuditStore(db) as store:  # after the checkpoint: the next one is refused
        last = "(SELECT MAX(seq) FROM audit_events)"
        tamper(store, f"UPDATE audit_events SET payload = '{{}}' WHERE seq = {last}")
    code, broken = run("audit", "checkpoint", "--db", db)
    assert code == 1 and broken["recorded"] is None
    code, full = run("audit", "verify", "--db", db)
    assert code == 1 and not full["ok"]
    missing = str(tmp_path / "missing.db")
    assert main(["audit", "checkpoint", "--db", missing]) == 1 and not Path(missing).exists()
