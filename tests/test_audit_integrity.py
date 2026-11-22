# SPDX-License-Identifier: Apache-2.0
"""The audit chain's integrity against concurrent writers and edits made past the store: forks,
appends outside the chain's lock, and decision rows changed without their events."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote

import pytest

from mendari import AuditStore, Decision, DecisionResult, Pack
from tests.conftest import message

Decide = Callable[[str], DecisionResult]

REVIEW = "hello?"  # abstains: no recommendation, HUMAN_REVIEW queue
EMERGENCY = "Crushing chest pain and my left arm is numb"  # stat HUMAN_REVIEW -> nurse
ROUTINE = "Can you send a refill for metformin to my pharmacy on Main Street?"  # AUTO


def tamper(store: AuditStore, sql: str, params: tuple[Any, ...] = ()) -> None:
    """Edit the database behind the store's back, as someone with the database could."""
    with store._db.session(exclusive=True) as db:
        db.all(sql, params)


@pytest.fixture
def decide(inbox_pack: Pack, store: AuditStore, screened: dict[str, str]) -> Decide:
    decision = Decision(inbox_pack, providers=screened, store=store)
    return lambda body: decision(message(body))


# -- appends outside the chain's lock --------------------------------------------------------


def test_the_chain_is_never_appended_to_outside_an_exclusive_session(store: AuditStore) -> None:
    """Two writers outside the lock read the same head and fork the chain: refused before
    anything is written, whichever kind of event."""
    with (
        pytest.raises(RuntimeError, match="exclusive session"),
        store._db.session(exclusive=False) as db,
    ):
        store._append(db, "deployment:x", "deployment.recorded", {"id": "x"})
    assert store.verify() == {"ok": True, "events": 0, "head": "0" * 64}


# -- Postgres: a server whose default isolation is not READ COMMITTED -------------------------


def _with_isolation(target: str, level: str) -> str:
    """`target` (a Postgres URL from `db_target`) with every connection's default isolation."""
    option = quote(f" -cdefault_transaction_isolation={level.replace(' ', chr(92) + ' ')}")
    return f"{target}{option}" if "options=" in target else f"{target}?options={option.strip()}"


def test_concurrent_writers_never_fork_whatever_the_servers_isolation(
    db_target: str, inbox_pack: Pack
) -> None:
    """A Postgres role or database may default to REPEATABLE READ. An exclusive session's
    snapshot would then be taken as it waits for the chain's lock, so it would read the head as
    it was before the writer it waited for, and chain after the same event: a fork. Every
    connection the store opens is READ COMMITTED instead, so many writers through several
    stores (instances) leave one chain."""
    if not db_target.startswith("postgresql://"):
        pytest.skip("Postgres-only behaviour")
    target = _with_isolation(db_target, "repeatable read")
    stores = [AuditStore(target) for _ in range(3)]
    try:
        with stores[0]._db.session(exclusive=False) as db:
            level = db.one("SELECT current_setting('default_transaction_isolation') AS level")
        assert level is not None and level["level"] == "repeatable read"  # the option holds
        results = [Decision(inbox_pack)(message(ROUTINE)) for _ in range(30)]
        failures: list[BaseException] = []

        def write(store: AuditStore, mine: list[DecisionResult]) -> None:
            try:
                for result in mine:
                    store.record(result, message(ROUTINE))
            except BaseException as error:  # reported below, never lost in the thread
                failures.append(error)

        threads = [
            threading.Thread(target=write, args=(store, results[n::3]))
            for n, store in enumerate(stores)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert failures == []
        verified = stores[0].verify()
        assert verified["ok"] is True, verified
        assert verified["events"] == 30
    finally:
        for store in stores:
            store.close()


# -- decision rows changed past the store -----------------------------------------------------


def test_a_decision_made_to_look_automated_breaks_verification(
    store: AuditStore, decide: Decide
) -> None:
    """The queue, lists and statistics read a decision's columns, not its chained record: an
    escalated decision whose outcome column says AUTO and whose review is no longer pending
    drops off the queue unseen unless `verify` compares the columns with the record."""
    escalated = decide(EMERGENCY)
    decide(ROUTINE)
    assert store.verify()["ok"] is True
    tamper(
        store,
        "UPDATE decisions SET outcome = 'AUTO', review_status = 'not_required' WHERE id = ?",
        (escalated.id,),
    )
    assert store.pending_reviews() == []  # what the change did
    assert store.verify() == {"ok": False, "events": 2, "broken_at": 1, "reason": "decision"}
    assert store.verify(since_checkpoint=True)["reason"] == "decision"


@pytest.mark.parametrize(
    ("column", "value"),
    [("priority", "routine"), ("pack_id", "other-pack"), ("pack_version", "9.9.9")],
)
def test_each_unchanging_column_is_checked_against_the_record(
    store: AuditStore, decide: Decide, column: str, value: str
) -> None:
    result = decide(EMERGENCY)
    tamper(store, f"UPDATE decisions SET {column} = ? WHERE id = ?", (value, result.id))
    assert store.verify()["reason"] == "decision"


def test_a_review_taken_off_the_queue_without_its_event_breaks_verification(
    store: AuditStore, decide: Decide
) -> None:
    """Only the review status changed (the outcome column still says HUMAN_REVIEW): the case
    leaves the queue, and no review was ever chained."""
    decide(ROUTINE)
    escalated = decide(EMERGENCY)
    tamper(
        store,
        "UPDATE decisions SET review_status = 'resolved', final_label = 'nurse' WHERE id = ?",
        (escalated.id,),
    )
    assert store.pending_reviews() == []
    assert store.verify() == {"ok": False, "events": 2, "broken_at": 2, "reason": "decision"}


def test_reviews_and_outcomes_chained_keep_verification_whole(
    store: AuditStore, decide: Decide
) -> None:
    for body in (EMERGENCY, REVIEW):
        store.resolve_review(decide(body).id, final_label="nurse", reviewer="rn-1")
    automated = decide(ROUTINE)
    store.record_outcome(automated.id, final_label="nurse", source="ehr", recorded_by="rn-1")
    assert store.verify()["ok"] is True


def test_a_decision_added_past_the_store_breaks_verification(
    store: AuditStore, decide: Decide
) -> None:
    """A decision row with no `decision.created`: statistics would count a decision the chain
    never recorded."""
    decide(ROUTINE)
    columns = (
        "id, created_at, pack_id, pack_version, outcome, recommendation, priority, confidence, "
        "latency_ms, cost_usd, record, review_status, org"
    )
    tamper(
        store,
        f"INSERT INTO decisions ({columns}) SELECT 'forged', created_at, pack_id, pack_version, "
        "outcome, recommendation, priority, confidence, latency_ms, cost_usd, record, "
        "review_status, org FROM decisions",
    )
    assert store.stats()["decisions"] == 2
    assert store.verify() == {"ok": False, "events": 1, "unchained": 1, "reason": "decision"}


def test_a_decision_chained_after_the_events_were_read_is_no_break(
    store: AuditStore, decide: Decide, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`verify` reads the events, then the rows: a decision made, and another resolved, in
    between have rows but none of the events read, and must not read as unchained or dropped."""
    decide(ROUTINE)
    pending = decide(EMERGENCY)
    opened = store._db.session
    written: list[str] = []

    @contextmanager
    def session(*, exclusive: bool) -> Iterator[Any]:
        with opened(exclusive=exclusive) as db:
            if exclusive:
                yield db
                return
            read = db.all

            def all_(sql: str, params: Any = ()) -> list[dict[str, Any]]:
                if sql.startswith("SELECT id, created_at") and not written:
                    written.append("once")  # after the events, before the rows
                    decide(EMERGENCY)
                    store.resolve_review(pending.id, final_label="nurse", reviewer="rn-1")
                return read(sql, params)

            monkeypatch.setattr(db, "all", all_)
            yield db

    monkeypatch.setattr(store._db, "session", session)
    verified = store.verify()
    assert written == ["once"]
    assert verified["ok"] is True, verified
    assert verified["events"] == 2  # those read; the two written meanwhile come after them
    monkeypatch.undo()
    assert store.verify()["events"] == 4
