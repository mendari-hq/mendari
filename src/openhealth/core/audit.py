"""Audit trail and human-review queue, backed by SQLite (standard library only).

Every decision is written once as an immutable record. Later facts (a reviewer's final label,
an observed downstream outcome) are *appended* as events, never edited into the record. Events
form a hash chain, so `verify()` detects any row that was altered or removed after the fact.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from openhealth.core.spec import Outcome
from openhealth.errors import ReviewError

if TYPE_CHECKING:
    from openhealth.core.decision import DecisionResult

GENESIS = "0" * 64
_REVIEWABLE = (Outcome.HUMAN_REVIEW.value, Outcome.ABSTAIN.value)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    pack_id         TEXT NOT NULL,
    pack_version    TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    recommendation  TEXT,
    priority        TEXT NOT NULL,
    confidence      REAL,
    latency_ms      REAL NOT NULL,
    cost_usd        REAL,
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
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
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
"""

_PRIORITY_ORDER = (
    "CASE priority WHEN 'stat' THEN 0 WHEN 'asap' THEN 1 WHEN 'urgent' THEN 2 ELSE 3 END"
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class AuditStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._memory = (
            sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
            if self.path == ":memory:"
            else None
        )
        self._lock = threading.RLock()  # serialises access to the shared in-memory connection
        with self._connect() as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self._memory is not None:
            with self._lock:
                self._memory.row_factory = sqlite3.Row
                yield self._memory
            return
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as db:
            # IMMEDIATE takes the write lock up front so hash-chain appends are serialised.
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

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
        with self._transaction() as db:
            db.execute(
                "INSERT INTO decisions (id, created_at, pack_id, pack_version, outcome, "
                "recommendation, priority, confidence, latency_ms, cost_usd, record, "
                "review_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        with self._transaction() as db:
            row = db.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
            if row is None:
                raise ReviewError(f"unknown decision {decision_id}")
            if row["review_status"] != "pending":
                raise ReviewError(f"decision {decision_id} is not awaiting review")
            record = json.loads(row["record"])
            labels = record["answers"].get(record["primary"], {}).get("probabilities") or {}
            if labels and final_label not in labels:
                raise ReviewError(f"{final_label!r} is not a valid label ({sorted(labels)})")
            # Overriding means disagreeing with a recommendation; an abstention made none.
            override = (
                None if row["recommendation"] is None else row["recommendation"] != final_label
            )
            resolved_at = _now()
            db.execute(
                "UPDATE decisions SET review_status = 'resolved', final_label = ?, "
                "human_override = ?, reviewer = ?, resolved_at = ? WHERE id = ?",
                (final_label, override, reviewer, resolved_at, decision_id),
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
        with self._transaction() as db:
            row = db.execute(
                "SELECT review_status FROM decisions WHERE id = ?", (decision_id,)
            ).fetchone()
            if row is None:
                raise ReviewError(f"unknown decision {decision_id}")
            if row["review_status"] == "pending":
                raise ReviewError("resolve the pending review instead of recording an outcome")
            db.execute(
                "UPDATE decisions SET final_label = ? WHERE id = ?", (final_label, decision_id)
            )
            self._append(
                db, decision_id, "outcome.recorded", {"final_label": final_label, "source": source}
            )

    def save_report(self, report: Mapping[str, Any]) -> None:
        with self._transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO eval_reports (id, created_at, pack_id, dataset, report) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    report["id"],
                    report["created_at"],
                    report["pack"]["id"],
                    report["dataset"]["name"],
                    _canonical(report),
                ),
            )

    def _append(self, db: sqlite3.Connection, decision_id: str, event: str, payload: Any) -> None:
        last = db.execute("SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1").fetchone()
        prev = last["hash"] if last else GENESIS
        created_at = _now()
        body = _canonical(payload)
        digest = _chain_hash(prev, decision_id, event, body, created_at)
        db.execute(
            "INSERT INTO audit_events (decision_id, event, payload, created_at, prev_hash, hash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (decision_id, event, body, created_at, prev, digest),
        )

    # -- reads ----------------------------------------------------------------------------

    def get(self, decision_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
            if row is None:
                return None
            events = db.execute(
                "SELECT seq, event, payload, created_at, hash FROM audit_events "
                "WHERE decision_id = ? ORDER BY seq",
                (decision_id,),
            ).fetchall()
        item = _row(row)
        item["events"] = [{**dict(e), "payload": json.loads(e["payload"])} for e in events]
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
        with self._connect() as db:
            rows = db.execute(
                f"SELECT * FROM decisions {where} ORDER BY {order} LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [_row(row) for row in rows]

    def pending_reviews(self, pack_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return self.query(pack_id=pack_id, review_status="pending", limit=limit)

    def stats(self, pack_id: str | None = None) -> dict[str, Any]:
        where, params = ("WHERE pack_id = ?", (pack_id,)) if pack_id else ("", ())
        with self._connect() as db:
            rows = db.execute(
                "SELECT outcome, recommendation, final_label, human_override, review_status, "
                f"latency_ms, cost_usd, created_at FROM decisions {where}",
                params,
            ).fetchall()
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
            day = daily.setdefault(row["created_at"][:10], {o.value: 0 for o in Outcome})
            day[row["outcome"]] += 1
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
        }

    def reports(self, pack_id: str | None = None) -> list[dict[str, Any]]:
        where, params = ("WHERE pack_id = ?", (pack_id,)) if pack_id else ("", ())
        with self._connect() as db:
            rows = db.execute(
                f"SELECT report FROM eval_reports {where} ORDER BY created_at DESC", params
            ).fetchall()
        return [json.loads(row["report"]) for row in rows]

    def verify(self) -> dict[str, Any]:
        """Recompute the hash chain and each decision record's digest."""
        with self._connect() as db:
            events = db.execute("SELECT * FROM audit_events ORDER BY seq").fetchall()
            records = {
                row["id"]: json.loads(row["record"])
                for row in db.execute("SELECT id, record FROM decisions").fetchall()
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


def _row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["record"] = json.loads(item["record"])
    if item["human_override"] is not None:
        item["human_override"] = bool(item["human_override"])
    return item


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _chain_hash(prev: str, decision_id: str, event: str, payload: str, created_at: str) -> str:
    return hashlib.sha256(
        "\x1f".join((prev, decision_id, event, payload, created_at)).encode()
    ).hexdigest()


def _percentile(sorted_values: list[float], pct: float) -> float | None:
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1, round(pct / 100 * (len(sorted_values) - 1)))
    return sorted_values[index]
