"""Audit trail and human-review queue, backed by SQLite (local) or Postgres (hosted).

Every decision is written once as an immutable record. Later facts (a reviewer's final label,
an observed downstream outcome) are *appended* as events, never edited into the record. Events
form a hash chain, so `verify()` detects any row that was altered or removed after the fact.

`AuditStore("var/openhealth.db")` uses SQLite; `AuditStore("postgresql://...")` uses Postgres.
Both backends run the same SQL (written with `?` placeholders) and the same hash chain.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics
import threading
import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from openhealth.core import clock
from openhealth.core.spec import Outcome
from openhealth.errors import ProviderError, ReviewError

Rating = Literal["up", "down"]

if TYPE_CHECKING:
    from openhealth.core.decision import DecisionResult

GENESIS = "0" * 64
_REVIEWABLE = (Outcome.HUMAN_REVIEW.value, Outcome.ABSTAIN.value)
# Arbitrary constant key for the Postgres advisory lock that serialises hash-chain appends.
_CHAIN_LOCK = 0x0E4_A0D17

_TABLES = """
CREATE TABLE IF NOT EXISTS decisions (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    pack_id         TEXT NOT NULL,
    pack_version    TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    recommendation  TEXT,
    priority        TEXT NOT NULL,
    confidence      {real},
    latency_ms      {real} NOT NULL,
    cost_usd        {real},
    record          TEXT NOT NULL,
    review_status   TEXT NOT NULL,
    final_label     TEXT,
    human_override  INTEGER,
    reviewer        TEXT,
    resolved_at     TEXT
);
CREATE INDEX IF NOT EXISTS decisions_queue ON decisions (review_status, priority, created_at);
CREATE INDEX IF NOT EXISTS decisions_pack ON decisions (pack_id, created_at);
CREATE TABLE IF NOT EXISTS audit_events (
    seq         {serial},
    decision_id TEXT NOT NULL,
    event       TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    prev_hash   TEXT NOT NULL,
    hash        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eval_reports (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    pack_id     TEXT NOT NULL,
    dataset     TEXT NOT NULL,
    report      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_usage (
    day         TEXT NOT NULL,
    provider    TEXT NOT NULL,
    calls       INTEGER NOT NULL,
    PRIMARY KEY (day, provider)
);
CREATE TABLE IF NOT EXISTS feedback (
    id                TEXT PRIMARY KEY,
    decision_id       TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    pack_id           TEXT NOT NULL,
    rating            TEXT NOT NULL,
    reporter          TEXT,
    note              TEXT,
    expected_question TEXT,
    expected_label    TEXT,
    superseded_by     TEXT
);
CREATE INDEX IF NOT EXISTS feedback_pack ON feedback (pack_id, rating, created_at);
CREATE INDEX IF NOT EXISTS feedback_decision ON feedback (decision_id)
"""

# Columns added after their table's first release. New databases get them from _TABLES; older
# ones get them from _migrate(). Additive only: nullable, no default, never renamed or dropped.
_ADDED_COLUMNS = (
    ("feedback", "expected_question", "TEXT"),
    ("feedback", "expected_label", "TEXT"),
    ("feedback", "superseded_by", "TEXT"),
)

_PRIORITY_ORDER = (
    "CASE priority WHEN 'stat' THEN 0 WHEN 'asap' THEN 1 WHEN 'urgent' THEN 2 ELSE 3 END"
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


# -- backends ------------------------------------------------------------------------------


class _Session(ABC):
    @abstractmethod
    def all(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        """Run a statement and return its rows as dicts (empty for statements without rows)."""

    def one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.all(sql, params)
        return rows[0] if rows else None

    @abstractmethod
    def columns(self, table: str) -> set[str]:
        """The column names a table has now."""


class _Backend(ABC):
    kind: str

    @abstractmethod
    def session(self, *, exclusive: bool) -> Any:
        """Context manager yielding a _Session. Exclusive sessions are transactions serialised
        with every other exclusive session (required for hash-chain appends)."""

    def close(self) -> None:  # noqa: B027 - optional hook
        pass


class _SqliteSession(_Session):
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def all(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(sql, tuple(params)).fetchall()]

    def columns(self, table: str) -> set[str]:
        return {row["name"] for row in self.all(f"PRAGMA table_info({table})")}


class _SqliteBackend(_Backend):
    kind = "sqlite"

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._memory = (
            sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
            if path == ":memory:"
            else None
        )
        self._lock = threading.RLock()  # serialises access to the shared in-memory connection
        with self._connect() as connection:
            connection.executescript(
                _TABLES.format(real="REAL", serial="INTEGER PRIMARY KEY AUTOINCREMENT")
            )
        with self.session(exclusive=True) as db:  # IMMEDIATE: one process migrates at a time
            _migrate(db)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self._memory is not None:
            with self._lock:
                self._memory.row_factory = sqlite3.Row
                yield self._memory
            return
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def session(self, *, exclusive: bool) -> Iterator[_Session]:
        with self._connect() as connection:
            if not exclusive:
                yield _SqliteSession(connection)
                return
            # IMMEDIATE takes the write lock up front so hash-chain appends are serialised.
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield _SqliteSession(connection)
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

    def close(self) -> None:
        if self._memory is not None:
            self._memory.close()


class _PostgresSession(_Session):
    def __init__(self, connection: Any):
        self.connection = connection

    def all(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        cursor = self.connection.execute(sql.replace("?", "%s"), tuple(params))
        return list(cursor.fetchall()) if cursor.description else []

    def columns(self, table: str) -> set[str]:
        rows = self.all(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ?",
            (table,),
        )
        return {row["column_name"] for row in rows}


class _PostgresBackend(_Backend):
    kind = "postgres"

    def __init__(self, url: str, migrate_url: str | None = None):
        import psycopg
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        # prepare_threshold=None: prepared statements break behind transaction-mode poolers
        # (Neon's pooled endpoint, PgBouncer).
        # check_connection: Neon suspends idle compute and the server closes pooled connections,
        # so every connection is verified (one round trip) before use and replaced if dead.
        self._pool = ConnectionPool(
            url,
            min_size=1,
            max_size=4,
            max_idle=60,
            check=ConnectionPool.check_connection,
            kwargs={
                "row_factory": dict_row,
                "prepare_threshold": None,
                "application_name": "openhealth",
            },
            open=True,
        )
        # Schema setup goes over the direct (unpooled) connection when one is given, as Neon
        # recommends for DDL; the advisory lock serialises concurrent cold starts.
        ddl = _TABLES.format(
            real="DOUBLE PRECISION", serial="BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY"
        )
        with (
            psycopg.connect(
                migrate_url or url, prepare_threshold=None, row_factory=dict_row
            ) as connection,
            connection.transaction(),
        ):
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (_CHAIN_LOCK,))
            for statement in ddl.split(";"):
                if statement.strip():
                    connection.execute(statement)
            _migrate(_PostgresSession(connection))

    @contextmanager
    def session(self, *, exclusive: bool) -> Iterator[_Session]:
        with self._pool.connection() as connection, connection.transaction():
            if exclusive:
                connection.execute("SELECT pg_advisory_xact_lock(%s)", (_CHAIN_LOCK,))
            yield _PostgresSession(connection)

    def close(self) -> None:
        self._pool.close()


def _migrate(db: _Session) -> None:
    """Bring a database created by an earlier release up to _TABLES. Runs under the schema lock
    (SQLite: an IMMEDIATE transaction; Postgres: the advisory lock), so cold starts don't race."""
    added = set()
    for table, column, kind in _ADDED_COLUMNS:
        if column not in db.columns(table):
            db.all(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            added.add(column)
    if "superseded_by" in added:
        _supersede_earlier_ratings(db)


def _supersede_earlier_ratings(db: _Session) -> None:
    """Ratings given before ratings could be changed: in each (decision, reporter) group only the
    latest counts, and the earlier ones point to it, as if each had been changed in turn. The
    hash chain is untouched: it already holds every rating."""
    rows = db.all("SELECT id, decision_id, reporter FROM feedback ORDER BY created_at, id")
    latest = {(row["decision_id"], row["reporter"]): row["id"] for row in rows}
    for row in rows:
        current = latest[(row["decision_id"], row["reporter"])]
        if row["id"] != current:
            db.all("UPDATE feedback SET superseded_by = ? WHERE id = ?", (current, row["id"]))


def _backend(target: str, migrate_url: str | None) -> _Backend:
    if target.startswith(("postgres://", "postgresql://")):
        return _PostgresBackend(target, migrate_url)
    return _SqliteBackend(target)


# -- store ---------------------------------------------------------------------------------


class AuditStore:
    def __init__(self, target: str | Path, *, migrate_url: str | None = None):
        """`target` is a SQLite path (or ":memory:") or a postgres:// / postgresql:// URL.

        `migrate_url` is an optional direct (unpooled) Postgres URL used only for schema setup.
        """
        self._db = _backend(str(target), migrate_url)
        self.path = str(target) if self._db.kind == "sqlite" else None

    @property
    def backend(self) -> str:
        return self._db.kind

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> AuditStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- writes ---------------------------------------------------------------------------

    def record(
        self,
        result: DecisionResult,
        inputs: Mapping[str, Any],
        *,
        store_input: Literal["full", "hash"] = "full",
    ) -> None:
        record = result.to_dict()
        record["input"] = dict(inputs) if store_input == "full" else None
        status = "pending" if result.outcome.value in _REVIEWABLE else "not_required"
        with self._db.session(exclusive=True) as db:
            db.all(
                "INSERT INTO decisions (id, created_at, pack_id, pack_version, outcome, "
                "recommendation, priority, confidence, latency_ms, cost_usd, record, "
                "review_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    result.id,
                    result.created_at,
                    result.pack_id,
                    result.pack_version,
                    result.outcome.value,
                    result.recommendation,
                    result.priority.value,
                    result.confidence,
                    result.latency_ms,
                    result.cost_usd,
                    _canonical(record),
                    status,
                ),
            )
            self._append(db, result.id, "decision.created", {"record_sha256": _sha(record)})

    def resolve_review(
        self, decision_id: str, *, final_label: str, reviewer: str, note: str | None = None
    ) -> dict[str, Any]:
        """A human confirms or corrects a HUMAN_REVIEW / ABSTAIN decision."""
        if not reviewer.strip():
            raise ReviewError("a reviewer identity is required")
        with self._db.session(exclusive=True) as db:
            row = db.one("SELECT * FROM decisions WHERE id = ?", (decision_id,))
            if row is None:
                raise ReviewError(f"unknown decision {decision_id}")
            if row["review_status"] != "pending":
                raise ReviewError(f"decision {decision_id} is not awaiting review")
            record = json.loads(row["record"])
            labels = record["answers"].get(record["primary"], {}).get("probabilities") or {}
            if labels and final_label not in labels:
                raise ReviewError(f"{final_label!r} is not a valid label ({sorted(labels)})")
            # Overriding means disagreeing with a recommendation; an abstention made none.
            recommendation = row["recommendation"]
            override = None if recommendation is None else recommendation != final_label
            resolved_at = _now()
            db.all(
                "UPDATE decisions SET review_status = 'resolved', final_label = ?, "
                "human_override = ?, reviewer = ?, resolved_at = ? WHERE id = ?",
                (
                    final_label,
                    None if override is None else int(override),
                    reviewer,
                    resolved_at,
                    decision_id,
                ),
            )
            payload = {
                "final_label": final_label,
                "human_override": override,
                "reviewer": reviewer,
                "note": note,
            }
            self._append(db, decision_id, "review.resolved", payload)
        return {"id": decision_id, "resolved_at": resolved_at, **payload}

    def record_outcome(self, decision_id: str, *, final_label: str, source: str) -> None:
        """Downstream ground truth for a decision that was not reviewed (e.g. staff re-routed an
        AUTO message). Feeds live accuracy and drift monitoring."""
        with self._db.session(exclusive=True) as db:
            row = db.one("SELECT review_status FROM decisions WHERE id = ?", (decision_id,))
            if row is None:
                raise ReviewError(f"unknown decision {decision_id}")
            if row["review_status"] == "pending":
                raise ReviewError("resolve the pending review instead of recording an outcome")
            db.all("UPDATE decisions SET final_label = ? WHERE id = ?", (final_label, decision_id))
            self._append(
                db, decision_id, "outcome.recorded", {"final_label": final_label, "source": source}
            )

    def record_feedback(
        self,
        decision_id: str,
        *,
        rating: Rating,
        reporter: str | None = None,
        note: str | None = None,
        expected: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """A person's thumbs up / thumbs down on any decision (AUTO included), with an optional
        note and, on a thumbs-down, the right answer to one of the decision's questions
        (`{"question": ..., "label": ...}`): a ready-made test case.

        A person can change their rating: the new one replaces their earlier one in counts, lists
        and exports, and both stay in the hash chain. People are told apart by `reporter`; unnamed
        ratings of a decision count as one person, so a mis-click can always be put right."""
        if rating not in ("up", "down"):
            raise ReviewError("rating must be 'up' or 'down'")
        if expected is not None and rating != "down":
            raise ReviewError("only a thumbs-down carries the right answer")
        reporter = (reporter or "").strip() or None
        note = (note or "").strip() or None
        feedback_id = str(uuid.uuid4())
        created_at = _now()
        with self._db.session(exclusive=True) as db:
            row = db.one("SELECT pack_id, record FROM decisions WHERE id = ?", (decision_id,))
            if row is None:
                raise ReviewError(f"unknown decision {decision_id}")
            answer = (
                None if expected is None else _right_answer(json.loads(row["record"]), expected)
            )
            previous = db.one(
                "SELECT id FROM feedback WHERE decision_id = ? AND COALESCE(reporter, '') = ? "
                "AND superseded_by IS NULL",
                (decision_id, reporter or ""),
            )
            db.all(
                "INSERT INTO feedback (id, decision_id, created_at, pack_id, rating, reporter, "
                "note, expected_question, expected_label) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    feedback_id,
                    decision_id,
                    created_at,
                    row["pack_id"],
                    rating,
                    reporter,
                    note,
                    answer and answer["question"],
                    answer and answer["label"],
                ),
            )
            if previous:
                db.all(
                    "UPDATE feedback SET superseded_by = ? WHERE id = ?",
                    (feedback_id, previous["id"]),
                )
            payload = {
                "feedback_id": feedback_id,
                "rating": rating,
                "reporter": reporter,
                "note": note,
                "expected": answer,
                "replaces": previous["id"] if previous else None,
            }
            self._append(db, decision_id, "feedback.recorded", payload)
        return {"id": feedback_id, "decision_id": decision_id, "created_at": created_at, **payload}

    def save_report(self, report: Mapping[str, Any]) -> None:
        with self._db.session(exclusive=False) as db:
            db.all(
                "INSERT INTO eval_reports (id, created_at, pack_id, dataset, report) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    report["id"],
                    report["created_at"],
                    report["pack"]["id"],
                    report["dataset"]["name"],
                    _canonical(report),
                ),
            )

    def consume(self, provider: str, *, daily_limit: int | None) -> None:
        """Count one call to a metered provider; raise `budget_exhausted` past the daily limit
        (None: count without a limit). Days are local calendar days (`core.clock`)."""
        day = clock.today().isoformat()
        cap = ""
        params: list[Any] = [day, provider]
        if daily_limit is not None:
            cap = " WHERE provider_usage.calls < ?"
            params.append(daily_limit)
        with self._db.session(exclusive=False) as db:
            row = db.one(
                "INSERT INTO provider_usage (day, provider, calls) VALUES (?, ?, 1) "
                "ON CONFLICT (day, provider) DO UPDATE SET calls = provider_usage.calls + 1"
                f"{cap} RETURNING calls",
                params,
            )
        if daily_limit is not None and (row is None or row["calls"] > daily_limit):
            raise ProviderError("budget_exhausted", f"daily {provider} limit of {daily_limit}")

    def usage(self, provider: str) -> int:
        day = clock.today().isoformat()
        with self._db.session(exclusive=False) as db:
            row = db.one(
                "SELECT calls FROM provider_usage WHERE day = ? AND provider = ?", (day, provider)
            )
        return int(row["calls"]) if row else 0

    def _append(self, db: _Session, decision_id: str, event: str, payload: Any) -> None:
        last = db.one("SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1")
        prev = last["hash"] if last else GENESIS
        created_at = _now()
        body = _canonical(payload)
        digest = _chain_hash(prev, decision_id, event, body, created_at)
        db.all(
            "INSERT INTO audit_events (decision_id, event, payload, created_at, prev_hash, hash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (decision_id, event, body, created_at, prev, digest),
        )

    # -- reads ----------------------------------------------------------------------------

    def get(self, decision_id: str) -> dict[str, Any] | None:
        with self._db.session(exclusive=False) as db:
            row = db.one("SELECT * FROM decisions WHERE id = ?", (decision_id,))
            if row is None:
                return None
            events = db.all(
                "SELECT seq, event, payload, created_at, prev_hash, hash FROM audit_events "
                "WHERE decision_id = ? ORDER BY seq",
                (decision_id,),
            )
        item = _row(row)
        item["events"] = [{**e, "payload": json.loads(e["payload"])} for e in events]
        return item

    def query(
        self,
        *,
        pack_id: str | None = None,
        outcome: str | None = None,
        review_status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        for column, value in (
            ("pack_id", pack_id),
            ("outcome", outcome),
            ("review_status", review_status),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order = (
            f"{_PRIORITY_ORDER}, created_at" if review_status == "pending" else "created_at DESC"
        )
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                # Each decision's current ratings (each person's latest), for a rating column.
                "SELECT d.*, "
                "(SELECT COUNT(*) FROM feedback f WHERE f.decision_id = d.id "
                "AND f.superseded_by IS NULL AND f.rating = 'up') AS feedback_up, "
                "(SELECT COUNT(*) FROM feedback f WHERE f.decision_id = d.id "
                "AND f.superseded_by IS NULL AND f.rating = 'down') AS feedback_down "
                f"FROM decisions d {where} ORDER BY {order} LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )
        return [_row(row) for row in rows]

    def events(
        self,
        *,
        pack_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        after_seq: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Chained events in order, each with its decision's pack and record, for exports. Pages
        by `after_seq` (the last `seq` returned); `since` / `until` bound `created_at` (ISO)."""
        clauses: list[str] = ["e.seq > ?"]
        params: list[Any] = [after_seq]
        for clause, value in (
            ("d.pack_id = ?", pack_id),
            ("e.created_at >= ?", since),
            ("e.created_at < ?", until),
        ):
            if value is not None:
                clauses.append(clause)
                params.append(value)
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT e.seq, e.decision_id, e.event, e.payload, e.created_at, e.prev_hash, "
                "e.hash, d.pack_id, d.pack_version, d.record FROM audit_events e "
                f"JOIN decisions d ON d.id = e.decision_id WHERE {' AND '.join(clauses)} "
                "ORDER BY e.seq LIMIT ?",
                (*params, limit),
            )
        return [
            {**row, "payload": json.loads(row["payload"]), "record": json.loads(row["record"])}
            for row in rows
        ]

    def iter_events(self, **filters: Any) -> Iterator[dict[str, Any]]:
        """Every event `events()` matches, page by page, without loading the whole chain."""
        after = 0
        while page := self.events(**filters, after_seq=after):
            yield from page
            after = page[-1]["seq"]

    def pending_reviews(self, pack_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return self.query(pack_id=pack_id, review_status="pending", limit=limit)

    def stats(self, pack_id: str | None = None) -> dict[str, Any]:
        where, params = ("WHERE pack_id = ?", (pack_id,)) if pack_id else ("", ())
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT outcome, recommendation, final_label, human_override, review_status, "
                f"latency_ms, cost_usd, created_at FROM decisions {where}",
                params,
            )
            # Each person's current rating: a changed rating counts once, as its latest value.
            fb_pack = "AND f.pack_id = ?" if pack_id else ""
            ratings = db.all(
                "SELECT f.rating, f.expected_label, d.outcome FROM feedback f "
                f"JOIN decisions d ON d.id = f.decision_id WHERE f.superseded_by IS NULL {fb_pack}",
                params,
            )
        total = len(rows)
        outcomes = {outcome.value: 0 for outcome in Outcome}
        for row in rows:
            outcomes[row["outcome"]] += 1
        reviewed = [r for r in rows if r["human_override"] is not None]
        labelled = [r for r in rows if r["final_label"] and r["recommendation"]]
        latencies = sorted(r["latency_ms"] for r in rows)
        costs = [r["cost_usd"] for r in rows]
        daily: dict[str, dict[str, int]] = {}
        for row in rows:
            # Local calendar days: a decision at 9 PM Eastern counts on that Eastern day. Reviews
            # and labels count on the day of the decision they judge, so any run of days gives the
            # override rate and observed accuracy of the decisions made in it.
            local = clock.local_date(row["created_at"])
            day = daily.setdefault(
                local, {**{o.value: 0 for o in Outcome}, **dict.fromkeys(_DAILY_REVIEWS, 0)}
            )
            day[row["outcome"]] += 1
            if row["human_override"] is not None:
                day["reviewed"] += 1
                day["overridden"] += bool(row["human_override"])
            if row["final_label"] and row["recommendation"]:
                day["labelled"] += 1
                day["agreed"] += row["final_label"] == row["recommendation"]
        return {
            "decisions": total,
            "outcomes": outcomes,
            "pending_reviews": sum(r["review_status"] == "pending" for r in rows),
            "reviews_resolved": len(reviewed),
            "human_override_rate": (
                sum(r["human_override"] for r in reviewed) / len(reviewed) if reviewed else None
            ),
            "observed_accuracy": (
                sum(r["final_label"] == r["recommendation"] for r in labelled) / len(labelled)
                if labelled
                else None
            ),
            "labelled": len(labelled),  # decisions with a final human label: accuracy's n
            "latency_ms": {
                "p50": _percentile(latencies, 50),
                "p95": _percentile(latencies, 95),
                "mean": statistics.fmean(latencies) if latencies else None,
            },
            "cost_usd": {
                "total": None if any(c is None for c in costs) else sum(costs),
                "per_decision": (
                    None if not costs or any(c is None for c in costs) else sum(costs) / len(costs)
                ),
            },
            "daily": [{"date": day, **counts} for day, counts in sorted(daily.items())],
            "feedback": {
                "up": sum(r["rating"] == "up" for r in ratings),
                "down": sum(r["rating"] == "down" for r in ratings),
                # Thumbs down on an automated decision: the system was confidently wrong.
                "down_on_auto": sum(
                    r["rating"] == "down" and r["outcome"] == Outcome.AUTO.value for r in ratings
                ),
                # A thumbs-down that says what the answer should have been: a ready-made test.
                "down_with_expected": sum(
                    r["rating"] == "down" and r["expected_label"] is not None for r in ratings
                ),
            },
        }

    def feedback(
        self, *, pack_id: str | None = None, rating: Rating | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Current feedback (each person's latest rating of a decision), newest first, each with
        the decision it rates and the right answer if the rating gave one."""
        clauses, params = ["f.superseded_by IS NULL"], []
        for column, value in (("f.pack_id", pack_id), ("f.rating", rating)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}"
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT f.id, f.decision_id, f.created_at, f.pack_id, f.rating, f.reporter, "
                "f.note, f.expected_question, f.expected_label, d.record FROM feedback f "
                f"JOIN decisions d ON d.id = f.decision_id {where} "
                "ORDER BY f.created_at DESC LIMIT ?",
                (*params, limit),
            )
        return [_feedback_item(row) for row in rows]

    def reports(self, pack_id: str | None = None) -> list[dict[str, Any]]:
        where, params = ("WHERE pack_id = ?", (pack_id,)) if pack_id else ("", ())
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                f"SELECT report FROM eval_reports {where} ORDER BY created_at DESC", params
            )
        return [json.loads(row["report"]) for row in rows]

    def verify(self) -> dict[str, Any]:
        """Recompute the hash chain and each decision record's digest."""
        with self._db.session(exclusive=False) as db:
            events = db.all("SELECT * FROM audit_events ORDER BY seq")
            records = {
                row["id"]: json.loads(row["record"])
                for row in db.all("SELECT id, record FROM decisions")
            }
        prev = GENESIS
        for event in events:
            expected = _chain_hash(
                prev, event["decision_id"], event["event"], event["payload"], event["created_at"]
            )
            if event["prev_hash"] != prev or event["hash"] != expected:
                return {"ok": False, "events": len(events), "broken_at": event["seq"]}
            if event["event"] == "decision.created":
                record = records.get(event["decision_id"])
                if record is None or json.loads(event["payload"])["record_sha256"] != _sha(record):
                    return {"ok": False, "events": len(events), "broken_at": event["seq"]}
            prev = event["hash"]
        return {"ok": True, "events": len(events), "head": prev}


def _row(row: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["record"] = json.loads(item["record"])
    if item["human_override"] is not None:
        item["human_override"] = bool(item["human_override"])
    return item


def _right_answer(record: Mapping[str, Any], expected: Mapping[str, str]) -> dict[str, str]:
    """A thumbs-down's right answer, checked against what the decision answered: one of its
    questions, one of that question's labels, and not the answer it already gave."""
    question, label = expected.get("question", ""), expected.get("label", "")
    answers = record["answers"]
    if question not in answers:
        raise ReviewError(
            f"{question!r} is not a question this decision answered ({sorted(answers)})"
        )
    labels = sorted(answers[question].get("probabilities") or {})
    if label not in labels:
        raise ReviewError(f"{label!r} is not a label of {question} ({labels})")
    if label == answers[question].get("label"):
        raise ReviewError(f"the decision already answered {question} = {label}")
    return {"question": question, "label": label}


def _feedback_item(row: Mapping[str, Any]) -> dict[str, Any]:
    item = {k: v for k, v in row.items() if k not in ("expected_question", "expected_label")}
    item["record"] = json.loads(row["record"])
    item["expected"] = (
        {"question": row["expected_question"], "label": row["expected_label"]}
        if row["expected_label"] is not None
        else None
    )
    return item


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _chain_hash(prev: str, decision_id: str, event: str, payload: str, created_at: str) -> str:
    joined = "\x1f".join((prev, decision_id, event, payload, created_at))
    return hashlib.sha256(joined.encode()).hexdigest()


# Per-day review counts in stats()["daily"]: human_override_rate = overridden / reviewed and
# observed_accuracy = agreed / labelled, over any run of days.
_DAILY_REVIEWS = ("reviewed", "overridden", "labelled", "agreed")


def _percentile(sorted_values: list[float], pct: float) -> float | None:
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1, round(pct / 100 * (len(sorted_values) - 1)))
    return sorted_values[index]
