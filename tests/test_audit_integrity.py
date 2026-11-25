# SPDX-License-Identifier: Apache-2.0
"""The audit chain's integrity against concurrent writers and edits made past the store: forks,
appends outside the chain's lock, and decision rows changed without their events."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote

import pytest

from mendari import Agent, AuditStore, Decision, DecisionResult
from tests.conftest import message

Decide = Callable[[str], DecisionResult]

# The triage fixture's messages, decided with its screen's canned agent, without which its policy
# automates nothing (`automation_requires`).
SCREENED = {"screen": "agent"}
REVIEW = "hello?"  # abstains: no recommendation, HUMAN_REVIEW queue
EMERGENCY = "Crushing chest pain and my left arm is numb"  # stat HUMAN_REVIEW -> nurse
ROUTINE = "Can you send a refill for metformin to my pharmacy on Main Street?"  # AUTO


def tamper(store: AuditStore, sql: str, params: tuple[Any, ...] = ()) -> None:
    """Edit the database behind the store's back, as someone with the database could."""
    with store._db.session(exclusive=True) as db:
        db.all(sql, params)


@pytest.fixture
def decide(triage_agent: Agent, store: AuditStore) -> Decide:
    decision = Decision(triage_agent, providers=SCREENED, store=store)
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


def test_no_event_s_subject_or_kind_holds_the_chain_s_separator(store: AuditStore) -> None:
    """The chain's hash joins its fields with an unescaped \\x1f: one in a subject or a kind
    would let ("a\\x1fb", "c") and ("a", "b\\x1fc") hash alike. Refused before anything is
    written; the hash of what is chained is unchanged."""
    from mendari.core.audit import _chain_hash

    assert _chain_hash("0" * 64, "a\x1fb", "c", "{}", "t") == _chain_hash(
        "0" * 64, "a", "b\x1fc", "{}", "t"
    )  # why: the join alone does not tell them apart
    for subject, kind in (("deployment:x\x1f", "deployment.recorded"), ("x", "a\x1fb")):
        with (
            pytest.raises(ValueError, match="separator"),
            store._db.session(exclusive=True) as db,
        ):
            store._append(db, subject, kind, {"id": "x"})
    assert store.verify() == {"ok": True, "events": 0, "head": "0" * 64}
    with store._db.session(exclusive=True) as db:
        store._append(db, "deployment:x", "deployment.recorded", {"id": "x"})
    assert store.verify()["ok"] is True


# -- the chain's unique links: no two events follow the same one ------------------------------

LINKS = "audit_events_prev_hash"


def _refused_by_the_database() -> tuple[type[BaseException], ...]:
    import psycopg

    return (sqlite3.IntegrityError, psycopg.IntegrityError)


def _fork(store: AuditStore) -> None:
    """A second event chained after the event the head follows, written past the store."""
    tamper(
        store,
        "INSERT INTO audit_events (decision_id, event, payload, created_at, prev_hash, hash) "
        "SELECT decision_id, event, payload, created_at, prev_hash, ? FROM audit_events "
        "WHERE seq = (SELECT MAX(seq) FROM audit_events)",
        ("f" * 64,),
    )


def _links_indexed(store: AuditStore) -> bool:
    with store._db.session(exclusive=False) as db:
        return LINKS in db.indexes("audit_events")


def test_the_database_refuses_a_second_event_after_the_same_head(
    store: AuditStore, decide: Decide
) -> None:
    """Even a writer that skipped the chain's lock (a future bug) cannot fork the chain: the
    database refuses the row, and the chain stays whole."""
    decide(ROUTINE)
    decide(ROUTINE)
    assert _links_indexed(store)
    with pytest.raises(_refused_by_the_database()):
        _fork(store)
    assert store.verify()["ok"] is True
    # The first event (after GENESIS) is the only one that may follow GENESIS.
    with pytest.raises(_refused_by_the_database()):
        tamper(
            store,
            "INSERT INTO audit_events (decision_id, event, payload, created_at, prev_hash, hash) "
            "VALUES ('x', 'deployment.recorded', '{}', '2026-01-01', ?, ?)",
            ("0" * 64, "e" * 64),
        )


# -- Postgres: a server whose default isolation is not READ COMMITTED -------------------------


def _with_isolation(target: str, level: str) -> str:
    """`target` (a Postgres URL from `db_target`) with every connection's default isolation."""
    option = quote(f" -cdefault_transaction_isolation={level.replace(' ', chr(92) + ' ')}")
    return f"{target}{option}" if "options=" in target else f"{target}?options={option.strip()}"


def test_concurrent_writers_never_fork_whatever_the_servers_isolation(
    db_target: str, triage_agent: Agent
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
        results = [Decision(triage_agent)(message(ROUTINE)) for _ in range(30)]
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


# -- verification reads the chain a page at a time ------------------------------------------


def test_verification_reads_the_chain_in_pages_with_the_same_results(
    store: AuditStore, decide: Decide, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`verify` holds `_VERIFY_PAGE` events at once: whatever the page, every verification
    (full, from a checkpoint, against a signed head, with notes) says exactly the same, on a
    whole chain and on a broken one."""
    from mendari.core import audit as audit_module

    for body in (EMERGENCY, ROUTINE, REVIEW):
        decide(body)
    store.record_checkpoint()
    for body in (EMERGENCY, ROUTINE, REVIEW, ROUTINE):
        decide(body)
    head = store.verify()["head"]

    def every_verification() -> list[dict[str, Any]]:
        return [
            store.verify(),
            store.verify(since_checkpoint=True),
            store.verify(head=head),
            store.verify(head="f" * 64),
            store.verify(notes=True),
        ]

    whole = every_verification()
    assert whole[0]["ok"] is True and whole[0]["events"] == 7
    assert whole[1]["ok"] is True and whole[1]["events"] == 4
    for page in (1, 2, 3, 7):
        monkeypatch.setattr(audit_module, "_VERIFY_PAGE", page)
        assert every_verification() == whole, page
    monkeypatch.undo()
    tamper(store, "UPDATE audit_events SET payload = payload || ' ' WHERE seq = 5")
    broken = every_verification()
    assert broken[0] == {"ok": False, "events": 7, "broken_at": 5}
    assert broken[1] == {"ok": False, "events": 4, "broken_at": 5}
    for page in (1, 2, 3, 7):
        monkeypatch.setattr(audit_module, "_VERIFY_PAGE", page)
        assert every_verification() == broken, page


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
    [("priority", "routine"), ("agent_id", "other-agent"), ("agent_version", "9.9.9")],
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
        "id, created_at, agent_id, agent_version, outcome, recommendation, priority, confidence, "
        "latency_ms, cost_usd, record, review_status, org"
    )
    tamper(
        store,
        f"INSERT INTO decisions ({columns}) SELECT 'forged', created_at, agent_id, agent_version, "
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
