"""Audit trail and human-review queue, backed by SQLite (local) or Postgres (hosted).

Every decision is written once as an immutable record. Later facts (a reviewer's final label,
an observed downstream outcome) are *appended* as events, never edited into the record. Events
form a hash chain, so `verify()` detects any row that was altered or removed after the fact.

`AuditStore("var/mendari.db")` uses SQLite; `AuditStore("postgresql://...")` uses Postgres.
Both backends run the same SQL (written with `?` placeholders) and the same hash chain.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import statistics
import time
import uuid
from collections.abc import Callable, Hashable, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from mendari.core import clock, tenancy
from mendari.core.db import Backend as _Backend
from mendari.core.db import Session as _Session
from mendari.core.db import connect
from mendari.core.phi import PhiPolicy
from mendari.core.query import Atom, Page, Query, Within
from mendari.core.spec import Outcome
from mendari.errors import ProviderError, ReviewError

Rating = Literal["up", "down"]

if TYPE_CHECKING:
    from mendari.core.decision import DecisionResult

GENESIS = "0" * 64
# The events that create or change a decision's result. Each makes a new version of the
# decision's FHIR Task and gets a Provenance (standards/task_fhir.py, audit_fhir.py); the others
# (feedback, input reads and purges, PAS submissions) change neither.
RESULT_EVENTS = ("decision.created", "review.resolved", "outcome.recorded")
_REVIEWABLE = (Outcome.HUMAN_REVIEW.value, Outcome.ABSTAIN.value)
# Arbitrary constant key for the Postgres advisory lock that serialises hash-chain appends.
_CHAIN_LOCK = 0x0E4_A0D17
# A system that reports a downstream outcome, named as an identifier ("ehr", "mendari",
# "urn:oid:1.2.3"), never free text: it is chained for good and exported in FHIR.
SYSTEM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,99}$")
# A served pack's labels for one of its questions, by (pack id, question); None when unknown.
QuestionLabels = Callable[[str, str], Sequence[str] | None]

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
    resolved_at     TEXT,
    org             TEXT,
    refused         INTEGER
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
CREATE INDEX IF NOT EXISTS audit_events_decision ON audit_events (decision_id, seq);
CREATE INDEX IF NOT EXISTS decisions_created ON decisions (created_at, id);
CREATE TABLE IF NOT EXISTS eval_reports (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    pack_id     TEXT NOT NULL,
    dataset     TEXT NOT NULL,
    report      TEXT NOT NULL,
    org         TEXT
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
CREATE INDEX IF NOT EXISTS feedback_decision ON feedback (decision_id);
CREATE TABLE IF NOT EXISTS decision_inputs (
    decision_id  TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    ciphertext   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS governance_approvals (
    id           TEXT PRIMARY KEY,
    org          TEXT NOT NULL,
    pack_id      TEXT NOT NULL,
    pack_digest  TEXT NOT NULL,
    risk_level   INTEGER NOT NULL,
    approver     TEXT NOT NULL,
    reference    TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    revoked_at   TEXT,
    revoked_by   TEXT
);
CREATE TABLE IF NOT EXISTS deployments (
    id           TEXT PRIMARY KEY,
    environment  TEXT NOT NULL,
    release      TEXT NOT NULL,
    pack_id      TEXT NOT NULL,
    pack_version TEXT NOT NULL,
    pack_digest  TEXT NOT NULL,
    providers    TEXT NOT NULL,
    started_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS deployments_pack ON deployments (pack_id, started_at);
CREATE TABLE IF NOT EXISTS api_keys (
    id          TEXT PRIMARY KEY,
    key_sha256  TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL,
    org         TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    revoked_at  TEXT,
    scopes      TEXT,
    expires_at  TEXT,
    revoked_by  TEXT
);
CREATE TABLE IF NOT EXISTS capability_versions (
    environment  TEXT NOT NULL,
    version      TEXT NOT NULL,
    since        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS capability_versions_since ON capability_versions (environment, since);
CREATE TABLE IF NOT EXISTS smart_sessions (
    key_sha256  TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    expires_at  INTEGER NOT NULL,
    data        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS smart_sessions_expiry ON smart_sessions (expires_at);
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    org         TEXT NOT NULL,
    role        TEXT NOT NULL,
    scopes      TEXT NOT NULL,
    jwks        TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    revoked_at  TEXT,
    revoked_by  TEXT
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_sha256  TEXT PRIMARY KEY,
    client_id     TEXT NOT NULL,
    scope         TEXT NOT NULL,
    expires_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS oauth_tokens_expiry ON oauth_tokens (expires_at);
CREATE TABLE IF NOT EXISTS rate_limits (
    bucket      TEXT PRIMARY KEY,
    tat         BIGINT NOT NULL,
    expires_at  BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS rate_limits_expiry ON rate_limits (expires_at);
CREATE TABLE IF NOT EXISTS org_usage (
    day         TEXT NOT NULL,
    org         TEXT NOT NULL,
    metric      TEXT NOT NULL,
    count       INTEGER NOT NULL,
    PRIMARY KEY (day, org, metric)
)
"""

# Columns added after their table's first release. New databases get them from _TABLES; older
# ones get them from _migrate(). Additive only: nullable, no default, never renamed or dropped.
_ADDED_COLUMNS = (
    ("feedback", "expected_question", "TEXT"),
    ("feedback", "expected_label", "TEXT"),
    ("feedback", "superseded_by", "TEXT"),
    ("decisions", "org", "TEXT"),  # tenancy (v0.6); NULL is the default organization
    ("eval_reports", "org", "TEXT"),
    # 1 when the input was refused before any provider ran (Phase 3); NULL on older rows, which
    # fall back to "BLOCK without confidence" (_REFUSED, task_fhir.refused).
    ("decisions", "refused", "INTEGER"),
    # Scoped keys that expire (Phase 4, ADR-0004): NULL scopes is a key with its role's
    # permissions; a NULL expiry is set once at startup (_expire_legacy_keys).
    ("api_keys", "scopes", "TEXT"),
    ("api_keys", "expires_at", "TEXT"),
    ("api_keys", "revoked_by", "TEXT"),
)
# API keys (ADR-0004): machines are viewer or service; a key lives 90 days unless asked
# otherwise, 365 at most; keys made before keys expired get 30 days from the first start of
# the release that brought expiry.
MACHINE_ROLES = ("viewer", "service")
KEY_DAYS = 90
KEY_MAX_DAYS = 365
LEGACY_KEY_DAYS = 30
# Expired session-table rows one put deletes (smart_put).
SESSION_PURGE_BATCH = 100

_PRIORITY_ORDER = (
    "CASE priority WHEN 'stat' THEN 0 WHEN 'asap' THEN 1 WHEN 'urgent' THEN 2 ELSE 3 END"
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _later(delta: timedelta) -> str:
    """A time `delta` from now, formatted as `_now()` is (so the two compare as text)."""
    return (datetime.now(UTC) + delta).isoformat(timespec="milliseconds")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


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
    _expire_legacy_keys(db)


def _expire_legacy_keys(db: _Session) -> None:
    """Keys from before keys expired (expires_at NULL) expire LEGACY_KEY_DAYS after the first
    start that finds them. Idempotent: only rows still NULL change, and every new key has an
    expiry. A key lookup refuses a NULL expiry (one written by an older instance during a
    rollout) until the next start gives it one."""
    db.all(
        "UPDATE api_keys SET expires_at = ? WHERE expires_at IS NULL",
        (_later(timedelta(days=LEGACY_KEY_DAYS)),),
    )


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


# -- store ---------------------------------------------------------------------------------


class AuditStore:
    def __init__(
        self, target: str | Path, *, migrate_url: str | None = None, phi: PhiPolicy | None = None
    ):
        """`target` is a SQLite path (or ":memory:") or a postgres:// / postgresql:// URL.

        `migrate_url` is an optional direct (unpooled) Postgres URL used only for schema setup.
        `phi` is how inputs are kept (`core/phi.py`; default: as the pack says).
        """
        self.phi = phi or PhiPolicy()
        self._db: _Backend = connect(
            str(target),
            schema=_TABLES,
            migrate=_migrate,
            lock_key=_CHAIN_LOCK,
            migrate_url=migrate_url,
        )
        self.path = str(target) if self._db.kind == "sqlite" else None
        # environment -> (the CapabilityStatement version served, since when): capability_since
        self._capability_since: dict[str, tuple[str, str]] = {}

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
        keep = store_input == "full" and self.phi.mode != "hash"
        record["input"] = dict(inputs) if keep and self.phi.mode == "off" else None
        status = "pending" if result.outcome.value in _REVIEWABLE else "not_required"
        with self._db.session(exclusive=True) as db:
            db.all(
                "INSERT INTO decisions (id, created_at, pack_id, pack_version, outcome, "
                "recommendation, priority, confidence, latency_ms, cost_usd, record, "
                "review_status, org, refused) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    tenancy.current() or tenancy.DEFAULT_ORG,
                    int(result.rejected),
                ),
            )
            self._append(db, result.id, "decision.created", {"record_sha256": _sha(record)})
            if keep and self.phi.mode == "encrypt":  # apart from the chained record
                db.all(
                    "INSERT INTO decision_inputs (decision_id, created_at, ciphertext) "
                    "VALUES (?, ?, ?)",
                    (result.id, result.created_at, self.phi.encrypt(result.id, dict(inputs))),
                )

    def resolve_review(
        self,
        decision_id: str,
        *,
        final_label: str,
        reviewer: str,
        note: str | None = None,
        labels: QuestionLabels | None = None,
    ) -> dict[str, Any]:
        """A human confirms or corrects a HUMAN_REVIEW / ABSTAIN decision with one of the labels
        of its primary question (`_check_label`)."""
        if not reviewer.strip():
            raise ReviewError("a reviewer identity is required")
        with self._db.session(exclusive=True) as db:
            scope, scoped = _scope()
            row = db.one(
                f"SELECT * FROM decisions d WHERE id = ? AND {scope}", (decision_id, *scoped)
            )
            if row is None:
                raise ReviewError(f"unknown decision {decision_id}")
            if row["review_status"] != "pending":
                raise ReviewError(f"decision {decision_id} is not awaiting review")
            _check_label(row["pack_id"], json.loads(row["record"]), final_label, labels)
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

    def record_outcome(
        self,
        decision_id: str,
        *,
        final_label: str,
        source: str,
        recorded_by: str,
        labels: QuestionLabels | None = None,
    ) -> None:
        """Downstream ground truth for a decision that was not reviewed (e.g. staff re-routed an
        AUTO message). Feeds live accuracy and drift monitoring.

        `final_label` is one of the labels of the decision's primary question (`_check_label`),
        `source` the reporting system as an identifier (`SYSTEM_ID`), and `recorded_by` who
        recorded it (the signed-in person or API key; "open mode" without sign-in)."""
        if not SYSTEM_ID.fullmatch(source):
            raise ReviewError("the source must be a system identifier, such as 'ehr'")
        if not recorded_by.strip():
            raise ReviewError("who records an outcome is required")
        with self._db.session(exclusive=True) as db:
            scope, scoped = _scope()
            row = db.one(
                f"SELECT review_status, pack_id, record FROM decisions d WHERE id = ? AND {scope}",
                (decision_id, *scoped),
            )
            if row is None:
                raise ReviewError(f"unknown decision {decision_id}")
            if row["review_status"] == "pending":
                raise ReviewError("resolve the pending review instead of recording an outcome")
            _check_label(row["pack_id"], json.loads(row["record"]), final_label, labels)
            db.all("UPDATE decisions SET final_label = ? WHERE id = ?", (final_label, decision_id))
            payload = {"final_label": final_label, "source": source, "recorded_by": recorded_by}
            self._append(db, decision_id, "outcome.recorded", payload)

    def record_event(self, decision_id: str, event: str, payload: Mapping[str, Any]) -> None:
        """A follow-up on a decision (e.g. `pas.submitted`), chained after its other events."""
        with self._db.session(exclusive=True) as db:
            scope, scoped = _scope()
            row = db.one(
                f"SELECT id FROM decisions d WHERE id = ? AND {scope}", (decision_id, *scoped)
            )
            if row is None:
                raise ReviewError(f"unknown decision {decision_id}")
            self._append(db, decision_id, event, dict(payload))

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
            scope, scoped = _scope()
            row = db.one(
                f"SELECT pack_id, record FROM decisions d WHERE id = ? AND {scope}",
                (decision_id, *scoped),
            )
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
                "INSERT INTO eval_reports (id, created_at, pack_id, dataset, report, org) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    report["id"],
                    report["created_at"],
                    report["pack"]["id"],
                    report["dataset"]["name"],
                    _canonical(report),
                    tenancy.current() or tenancy.DEFAULT_ORG,
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

    def consume_org(self, org: str, metric: str, *, daily_limit: int) -> int | None:
        """Count one `metric` (decisions, or a provider type's calls) for `org` today; the count
        it reached, or None when the organization's daily limit was already reached (nothing
        counted). One statement, so instances never count past the limit together."""
        with self._db.session(exclusive=False) as db:
            row = db.one(
                "INSERT INTO org_usage (day, org, metric, count) VALUES (?, ?, ?, 1) "
                "ON CONFLICT (day, org, metric) DO UPDATE SET count = org_usage.count + 1 "
                "WHERE org_usage.count < ? RETURNING count",
                (clock.today().isoformat(), org, metric, daily_limit),
            )
        if row is None or int(row["count"]) > daily_limit:  # a limit of 0 admits nothing
            return None
        return int(row["count"])

    def org_usage(self, org: str) -> dict[str, int]:
        """What `org` has used today, by metric."""
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT metric, count FROM org_usage WHERE day = ? AND org = ?",
                (clock.today().isoformat(), org),
            )
        return {row["metric"]: int(row["count"]) for row in rows}

    # -- rate limits (server/limits.py) -------------------------------------------------------
    # GCRA on one row per bucket: `tat` is when the bucket would be empty again (ms since the
    # epoch). A request is admitted when the bucket, with it, runs at most `tolerance` ahead of
    # now; each admitted request moves `tat` one `interval` on. Not part of the audit chain:
    # no lock but the row's own.

    def rate_take(self, bucket: str, interval: int, tolerance: int, now: int) -> int | None:
        """Admit one request into `bucket` (None), or refuse it without counting it: the
        milliseconds until it would be admitted. One statement, atomic across instances."""
        with self._db.session(exclusive=False) as db:
            admitted = db.one(
                "INSERT INTO rate_limits (bucket, tat, expires_at) VALUES (?, ?, ?) "
                f"ON CONFLICT (bucket) DO UPDATE SET tat = {_FROM} + ?, expires_at = {_FROM} + ? "
                f"WHERE {_FROM} + ? - ? <= ? RETURNING tat",
                (
                    *(bucket, now + interval, now + interval),
                    *(now, now, interval),
                    *(now, now, interval),
                    *(now, now, interval, tolerance, now),
                ),
            )
            if admitted is not None:
                return None
            row = db.one("SELECT tat FROM rate_limits WHERE bucket = ?", (bucket,))
        return _wait(row, interval, tolerance, now)

    def rate_peek(self, bucket: str, interval: int, tolerance: int, now: int) -> int | None:
        """What `rate_take` would answer, without counting anything."""
        with self._db.session(exclusive=False) as db:
            row = db.one("SELECT tat FROM rate_limits WHERE bucket = ?", (bucket,))
        return _wait(row, interval, tolerance, now)

    def rate_purge(self, now: int, batch: int) -> None:
        """Delete up to `batch` buckets that are empty again (equivalent to no row)."""
        with self._db.session(exclusive=False) as db:
            db.all(
                "DELETE FROM rate_limits WHERE bucket IN (SELECT bucket FROM rate_limits "
                "WHERE expires_at <= ? ORDER BY expires_at LIMIT ?)",
                (now, batch),
            )

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

    def get(self, decision_id: str, *, reader: str | None = None) -> dict[str, Any] | None:
        """A decision with its events; under PHI mode `encrypt`, its input only for a `reader`
        (see `reveal_inputs`, which chains the read before the events are fetched)."""
        with self._db.session(exclusive=False) as db:
            item = _decision(db, decision_id)
        if item is None:
            return None
        self.reveal_inputs([item], reader)
        item["events"] = self.decision_events([decision_id])[decision_id]
        return item

    def item(self, decision_id: str) -> dict[str, Any] | None:
        """A decision with its events, shaped like `get()` without a reader, for renderings that
        never show the input (the FHIR Task). Never writes: no `input.read` is chained, whatever
        the PHI mode, and the input is neither decrypted nor marked restricted. Another
        organization's decision is None, as an unknown one is."""
        with self._db.session(exclusive=False) as db:
            item = _decision(db, decision_id)
            if item is not None:
                item["events"] = _events_of(db, [decision_id])[decision_id]
        return item

    def decision_events(self, decision_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Each decision's events in chain order (as `get()` lists them), in one query for a page
        of decisions, so a page of N costs one round trip instead of N. Decisions of another
        organization, and unknown ones, get an empty list."""
        if not decision_ids:
            return {}
        with self._db.session(exclusive=False) as db:
            return _events_of(db, decision_ids)

    def event(self, decision_id: str, seq: int) -> dict[str, Any] | None:
        """One chained event of a decision, with its decision's pack and record (as `events()`
        returns them), or None: unknown, another decision's, or another organization's."""
        scope, scoped = _scope()
        with self._db.session(exclusive=False) as db:
            row = db.one(
                f"SELECT {_JOINED} WHERE e.decision_id = ? AND e.seq = ? AND {scope}",
                (decision_id, seq, *scoped),
            )
        return None if row is None else _joined(row)

    def reveal_inputs(
        self, items: list[dict[str, Any]], reader: str | None, *, id_key: str = "id"
    ) -> list[dict[str, Any]]:
        """PHI mode `encrypt` keeps inputs apart from the records. Each item (a record under
        `record`) whose input was kept gets it back for a `reader`, someone who may see inputs,
        and every such read is written to the hash chain. Anyone else gets `input_restricted`."""
        ids = [str(item[id_key]) for item in items]
        if not ids:
            return items
        marks = ", ".join("?" for _ in ids)
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT decision_id, ciphertext FROM decision_inputs "
                f"WHERE decision_id IN ({marks})",
                tuple(ids),
            )
        sealed = {row["decision_id"]: row["ciphertext"] for row in rows}
        if not sealed:
            return items
        readable = bool(reader) and self.phi.mode == "encrypt"
        opened = {k: self.phi.decrypt(k, v) for k, v in sealed.items()} if readable else {}
        if opened:
            with self._db.session(exclusive=True) as db:
                for decision_id in opened:
                    self._append(db, decision_id, "input.read", {"reader": reader})
        for item in items:
            decision_id = str(item[id_key])
            if decision_id in sealed:
                item["record"]["input"] = opened.get(decision_id)
                item["input_restricted"] = decision_id not in opened
        return items

    def purge_inputs(self, older_than_days: int) -> int:
        """Retention: delete kept (encrypted) inputs older than N days; the chain is untouched."""
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat()
        with self._db.session(exclusive=True) as db:
            doomed = db.all(
                "SELECT decision_id FROM decision_inputs WHERE created_at < ?", (cutoff,)
            )
            db.all("DELETE FROM decision_inputs WHERE created_at < ?", (cutoff,))
            for row in doomed:
                self._append(
                    db, row["decision_id"], "input.purged", {"retention_days": older_than_days}
                )
        return len(doomed)

    def query(
        self,
        *,
        pack_id: str | None = None,
        outcome: str | None = None,
        review_status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        scope, scoped = _scope()
        clauses, params = [scope], list(scoped)
        for column, value in (
            ("pack_id", pack_id),
            ("outcome", outcome),
            ("review_status", review_status),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}"
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
        scope, scoped = _scope()
        clauses: list[str] = ["e.seq > ?", scope]
        params: list[Any] = [after_seq, *scoped]
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
                f"SELECT {_JOINED} WHERE {' AND '.join(clauses)} ORDER BY e.seq LIMIT ?",
                (*params, limit),
            )
        return [_joined(row) for row in rows]

    def search_decisions(self, query: Query) -> Page:
        """Decisions matching a query (`core/query.py`), newest first (by `created_at`, then
        `id`, both descending), each with all its events as `item()` gives them: one query for
        the page and one for its events, however long the page. Keyset paging: `query.after` is
        the `(created_at, id)` of the last decision already returned."""
        conditions, params = _conditions(query.where, _DECISION_SQL)
        scope, scoped = _scope()
        where = " AND ".join([scope, *conditions])
        keyset = "" if query.after is None else " AND (d.created_at, d.id) < (?, ?)"
        with self._db.session(exclusive=False) as db:
            rows = _page(
                db,
                f"SELECT d.* FROM decisions d WHERE {where}{keyset} "
                "ORDER BY d.created_at DESC, d.id DESC LIMIT ?",
                (*scoped, *params, *(query.after or ())),
                query.limit,
            )
            items = [_row(row) for row in rows[: query.limit]]
            trails = _events_of(db, [item["id"] for item in items]) if items else {}
            counted = f"SELECT COUNT(*) AS n FROM decisions d WHERE {where}"
            total = _total(db, counted, (*scoped, *params)) if query.count else None
        for item in items:
            item["events"] = trails[item["id"]]
        more = len(rows) > query.limit
        return Page(items, (items[-1]["created_at"], items[-1]["id"]) if more else None, total)

    def search_events(self, query: Query) -> Page:
        """Chained decision events matching a query (`core/query.py`), in chain order, each
        joined with its decision as `events()` gives them. Keyset paging: `query.after` is the
        `(seq,)` of the last event already returned. Appends are serialised by the chain lock,
        so `seq` order is commit order: a walk neither repeats nor skips an event, even while
        events are added."""
        conditions, params = _conditions(query.where, _EVENT_SQL)
        scope, scoped = _scope()
        where = " AND ".join([scope, *conditions])
        keyset = "" if query.after is None else " AND e.seq > ?"
        with self._db.session(exclusive=False) as db:
            rows = _page(
                db,
                f"SELECT {_JOINED} WHERE {where}{keyset} ORDER BY e.seq LIMIT ?",
                (*scoped, *params, *(query.after or ())),
                query.limit,
            )
            counted = (
                "SELECT COUNT(*) AS n FROM audit_events e JOIN decisions d "
                f"ON d.id = e.decision_id WHERE {where}"
            )
            total = _total(db, counted, (*scoped, *params)) if query.count else None
        events = [_joined(row) for row in rows[: query.limit]]
        more = len(rows) > query.limit
        return Page(events, (events[-1]["seq"],) if more else None, total)

    # -- the session table: sign-ins, SMART launches, sessions, one-time claims ----------------
    # Each row is keyed by sha256(kind || 0x00 || value), the value being the state, session id
    # or claim the caller holds: the table alone cannot be replayed, and a value of one kind can
    # never name a row of another (a kind is a short name without NUL). Every read, take and
    # delete is scoped to its kind. Rows expire; each put purges up to 100 expired ones. Not part
    # of the audit chain, so no operation here takes the chain's lock: a take is one
    # `DELETE ... RETURNING`, a claim one `INSERT ... ON CONFLICT`, each atomic on its own.
    # Rows keyed before the kind was in the digest are never found again and expire.

    def smart_put(self, key: str, kind: str, data: Mapping[str, Any], expires_at: int) -> None:
        row = _session_key(kind, key)
        now = int(time.time())
        with self._db.session(exclusive=False) as db:
            # A bounded purge on the expiry index: a flood of anonymous sign-ins (each leaves a
            # ten-minute pending row) costs every later put at most this many deletes, and
            # puts still outpace the rows they add.
            db.all(
                "DELETE FROM smart_sessions WHERE key_sha256 IN (SELECT key_sha256 FROM "
                "smart_sessions WHERE expires_at <= ? ORDER BY expires_at LIMIT ?)",
                (now, SESSION_PURGE_BATCH),
            )
            db.all(
                "INSERT INTO smart_sessions (key_sha256, kind, expires_at, data) "
                "VALUES (?, ?, ?, ?)",
                (row, kind, expires_at, _canonical(data)),
            )

    def smart_get(self, key: str, kind: str) -> dict[str, Any] | None:
        row = self.smart_read(key, kind)
        return None if row is None else row[0]

    def smart_read(self, key: str, kind: str) -> tuple[dict[str, Any], int] | None:
        """A live row's data and its `expires_at` (a session's idle expiry, server/sessions.py)."""
        row_key = _session_key(kind, key)
        with self._db.session(exclusive=False) as db:
            row = db.one(
                "SELECT data, expires_at FROM smart_sessions WHERE key_sha256 = ? AND kind = ? "
                "AND expires_at > ?",
                (row_key, kind, int(time.time())),
            )
        return None if row is None else (json.loads(row["data"]), int(row["expires_at"]))

    def smart_touch(self, key: str, kind: str, until: int) -> bool:
        """Push a live row's expiry forward to `until`, never back: concurrent touches through
        any number of instances leave the latest, and an ended row stays ended. True if moved."""
        row_key = _session_key(kind, key)
        with self._db.session(exclusive=False) as db:
            moved = db.all(
                "UPDATE smart_sessions SET expires_at = ? WHERE key_sha256 = ? AND kind = ? "
                "AND expires_at > ? AND expires_at < ? RETURNING key_sha256",
                (until, row_key, kind, int(time.time()), until),
            )
        return bool(moved)

    def smart_replace(self, key: str, kind: str, data: Mapping[str, Any], expires_at: int) -> bool:
        """Replace a live row's data and expiry (a session from an earlier release taking up the
        idle limit); an ended row stays ended. True if replaced."""
        row_key = _session_key(kind, key)
        with self._db.session(exclusive=False) as db:
            replaced = db.all(
                "UPDATE smart_sessions SET data = ?, expires_at = ? WHERE key_sha256 = ? "
                "AND kind = ? AND expires_at > ? RETURNING key_sha256",
                (_canonical(data), expires_at, row_key, kind, int(time.time())),
            )
        return bool(replaced)

    def smart_take(self, key: str, kind: str) -> dict[str, Any] | None:
        """Read and delete in one statement: a launch's or a sign-in's state works once."""
        row_key = _session_key(kind, key)
        with self._db.session(exclusive=False) as db:
            row = db.one(
                "DELETE FROM smart_sessions WHERE key_sha256 = ? AND kind = ? "
                "RETURNING data, expires_at",
                (row_key, kind),
            )
        if row is None or row["expires_at"] <= int(time.time()):
            return None
        return json.loads(row["data"])

    def smart_delete(self, key: str, kind: str) -> None:
        """Delete this kind's row for `key` (a logout ends its own kind of session only)."""
        row_key = _session_key(kind, key)
        with self._db.session(exclusive=False) as db:
            db.all("DELETE FROM smart_sessions WHERE key_sha256 = ? AND kind = ?", (row_key, kind))

    def smart_claim(self, kind: str, value: str, expires_at: int) -> bool:
        """Claim `value` once until `expires_at` (e.g. an assertion's `jti`): True for the first
        claim, False while that claim lasts; an expired claim can be made again. One statement,
        so concurrent claims through any number of connections or instances have one winner."""
        row_key = _session_key(kind, value)
        now = int(time.time())
        if expires_at <= now:
            raise ValueError("a claim must expire in the future")
        with self._db.session(exclusive=False) as db:
            won = db.all(
                "INSERT INTO smart_sessions (key_sha256, kind, expires_at, data) "
                "VALUES (?, ?, ?, '{}') "
                "ON CONFLICT (key_sha256) DO UPDATE SET kind = excluded.kind, "
                "expires_at = excluded.expires_at, data = excluded.data "
                "WHERE smart_sessions.expires_at <= ? "
                "RETURNING key_sha256",
                (row_key, kind, expires_at, now),
            )
        return bool(won)

    # -- governance approvals -------------------------------------------------------------------
    # A deploying organization's recorded decision to let a level 2-3 pack automate (ADR-0002):
    # bound to the pack's exact digest (a changed pack needs a new approval), with an expiry, per
    # organization, and chained like decisions ("governance:<id>" in the audit chain).

    def approve_automation(
        self,
        *,
        pack_id: str,
        pack_digest: str,
        risk_level: int,
        approver: str,
        reference: str,
        expires_at: str,
    ) -> dict[str, Any]:
        if risk_level not in (2, 3):
            reason = "is never automated" if risk_level >= 4 else "needs no approval"
            raise ReviewError(f"a risk level {risk_level} pack {reason}")
        if not approver.strip() or not reference.strip():
            raise ReviewError("an approval needs an approver and a reference")
        approval = {
            "id": secrets.token_hex(8),
            "org": tenancy.current() or tenancy.DEFAULT_ORG,
            "pack_id": pack_id,
            "pack_digest": pack_digest,
            "risk_level": risk_level,
            "approver": approver.strip(),
            "reference": reference.strip(),
            "created_at": _now(),
            "expires_at": expires_at,
        }
        with self._db.session(exclusive=True) as db:
            db.all(
                "INSERT INTO governance_approvals (id, org, pack_id, pack_digest, risk_level, "
                "approver, reference, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(approval.values()),
            )
            self._append(db, f"governance:{approval['id']}", "governance.approved", approval)
        return approval

    def revoke_approval(self, approval_id: str, *, revoked_by: str) -> bool:
        org = tenancy.current() or tenancy.DEFAULT_ORG
        with self._db.session(exclusive=True) as db:
            row = db.one(
                "SELECT id FROM governance_approvals "
                "WHERE id = ? AND org = ? AND revoked_at IS NULL",
                (approval_id, org),
            )
            if row is None:
                return False
            db.all(
                "UPDATE governance_approvals SET revoked_at = ?, revoked_by = ? WHERE id = ?",
                (_now(), revoked_by, approval_id),
            )
            payload = {"id": approval_id, "revoked_by": revoked_by}
            self._append(db, f"governance:{approval_id}", "governance.revoked", payload)
        return True

    def approvals(self, pack_id: str | None = None) -> list[dict[str, Any]]:
        org = tenancy.current() or tenancy.DEFAULT_ORG
        packs = ("AND pack_id = ?", (pack_id,)) if pack_id else ("", ())
        with self._db.session(exclusive=False) as db:
            return db.all(
                f"SELECT * FROM governance_approvals WHERE org = ? {packs[0]} "
                "ORDER BY created_at DESC",
                (org, *packs[1]),
            )

    def active_approval(self, pack_id: str, pack_digest: str) -> dict[str, Any] | None:
        """The approval in force for exactly this pack, for this request's organization."""
        org = tenancy.current() or tenancy.DEFAULT_ORG
        with self._db.session(exclusive=False) as db:
            return db.one(
                "SELECT * FROM governance_approvals WHERE org = ? AND pack_id = ? "
                "AND pack_digest = ? AND revoked_at IS NULL AND expires_at > ? "
                "ORDER BY created_at DESC LIMIT 1",
                (org, pack_id, pack_digest, _now()),
            )

    # -- the deployment registry -----------------------------------------------------------------
    # Which pack version runs where, with which providers, since when: one row each time that
    # changes (not each start, since a serverless platform starts the server many times a day),
    # chained like decisions ("deployment:<id>").

    def record_deployment(
        self,
        *,
        environment: str,
        release: str,
        pack_id: str,
        pack_version: str,
        pack_digest: str,
        providers: Mapping[str, str],
    ) -> dict[str, Any] | None:
        """Records that this pack runs here, unless the latest record for the environment already
        says so (same digest, providers and release). Returns the new record, or None."""
        deployment = {
            "id": secrets.token_hex(8),
            "environment": environment,
            "release": release,
            "pack_id": pack_id,
            "pack_version": pack_version,
            "pack_digest": pack_digest,
            "providers": _canonical(dict(providers)),
            "started_at": _now(),
        }
        with self._db.session(exclusive=True) as db:
            last = db.one(
                "SELECT release, pack_digest, providers FROM deployments "
                "WHERE environment = ? AND pack_id = ? ORDER BY started_at DESC LIMIT 1",
                (environment, pack_id),
            )
            unchanged = ("release", "pack_digest", "providers")
            if last is not None and all(last[k] == deployment[k] for k in unchanged):
                return None
            db.all(
                "INSERT INTO deployments (id, environment, release, pack_id, pack_version, "
                "pack_digest, providers, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(deployment.values()),
            )
            self._append(db, f"deployment:{deployment['id']}", "deployment.recorded", deployment)
        return {**deployment, "providers": dict(providers)}

    def capability_since(self, environment: str, version: str) -> str:
        """When this environment started serving this version of its FHIR CapabilityStatement:
        the time recorded when the version first replaced another one here. A statement's
        `date` must change when its version does, and never go back, so a version served again
        after another one gets a new time.

        The statement is public, so anyone can make this run: the answer is kept per process
        and the version is read in a shared session. Only a changed version takes the exclusive
        one (on Postgres, the chain's lock) and reads again inside it, so concurrent first
        calls record one time."""
        known = self._capability_since.get(environment)
        if known is not None and known[0] == version:
            return known[1]
        latest = (
            "SELECT version, since FROM capability_versions WHERE environment = ? "
            "ORDER BY since DESC LIMIT 1"
        )
        with self._db.session(exclusive=False) as db:
            last = db.one(latest, (environment,))
        if last is None or last["version"] != version:
            with self._db.session(exclusive=True) as db:
                last = db.one(latest, (environment,))
                if last is None or last["version"] != version:
                    last = {"version": version, "since": _now()}
                    db.all(
                        "INSERT INTO capability_versions (environment, version, since) "
                        "VALUES (?, ?, ?)",
                        (environment, version, last["since"]),
                    )
        since = str(last["since"])
        self._capability_since[environment] = (version, since)
        return since

    def deployments(self, pack_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Newest first. Deployments belong to the server, so every organization sees them."""
        packs = ("WHERE pack_id = ?", (pack_id,)) if pack_id else ("", ())
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                f"SELECT * FROM deployments {packs[0]} ORDER BY started_at DESC LIMIT ?",
                (*packs[1], limit),
            )
        return [{**row, "providers": json.loads(row["providers"])} for row in rows]

    # -- API keys (server/auth.py) --------------------------------------------------------------
    # Stored as SHA-256 (the secret is in the creation's answer and nowhere else), with a role
    # (viewer or service), optional scopes (server/scopes.py), an expiry and, once revoked, who
    # revoked it. Creating and revoking are chained under "key:<id>", never with the secret or
    # its hash.

    def create_api_key(
        self,
        *,
        name: str,
        role: str,
        org: str,
        created_by: str,
        scopes: str | None = None,
        days: int = KEY_DAYS,
    ) -> dict[str, Any]:
        """A new key for a machine; its secret is in the result and nowhere else. `scopes` (the
        caller has checked them, server/scopes.py) narrow the role; None keeps the role's."""
        if role not in MACHINE_ROLES:
            raise ValueError(f"a key's role is one of {MACHINE_ROLES}")
        if not 1 <= days <= KEY_MAX_DAYS:
            raise ValueError(f"a key lives 1 to {KEY_MAX_DAYS} days")
        secret = "ohk_" + secrets.token_urlsafe(32)
        key = {
            "id": secrets.token_hex(6),
            "name": name,
            "role": role,
            "org": org,
            "scope": scopes,
            "created_by": created_by,
            "created_at": _now(),
            "expires_at": _later(timedelta(days=days)),
        }
        with self._db.session(exclusive=True) as db:
            db.all(
                "INSERT INTO api_keys (id, key_sha256, name, role, org, created_by, created_at, "
                "scopes, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key["id"],
                    _key(secret),
                    name,
                    role,
                    org,
                    created_by,
                    key["created_at"],
                    scopes,
                    key["expires_at"],
                ),
            )
            self._append(db, f"key:{key['id']}", "key.created", key)
        return {**key, "key": secret}

    def api_keys(self, org: str | None = None) -> list[dict[str, Any]]:
        """Every key (of `org`, if given), oldest first; never the secret or its hash."""
        where, params = ("WHERE org = ?", (org,)) if org else ("", ())
        with self._db.session(exclusive=False) as db:
            return db.all(
                "SELECT id, name, role, org, scopes AS scope, created_by, created_at, "
                f"expires_at, revoked_at, revoked_by FROM api_keys {where} ORDER BY created_at",
                params,
            )

    def api_key(self, secret: str) -> dict[str, Any] | None:
        """The key a secret belongs to, unless it was revoked or has expired (one query; a key
        without an expiry is refused too)."""
        with self._db.session(exclusive=False) as db:
            return db.one(
                "SELECT id, name, role, org, scopes AS scope, expires_at FROM api_keys "
                "WHERE key_sha256 = ? AND revoked_at IS NULL AND expires_at > ?",
                (_key(secret), _now()),
            )

    def revoke_api_key(self, key_id: str, org: str | None = None, *, revoked_by: str) -> bool:
        """Revoke a key of `org` (any, if None); False for an unknown or already revoked one."""
        with self._db.session(exclusive=True) as db:
            row = db.one("SELECT org FROM api_keys WHERE id = ? AND revoked_at IS NULL", (key_id,))
            if row is None or (org and row["org"] != org):
                return False
            revoked = {"id": key_id, "revoked_by": revoked_by, "revoked_at": _now()}
            db.all(
                "UPDATE api_keys SET revoked_at = ?, revoked_by = ? WHERE id = ?",
                (revoked["revoked_at"], revoked_by, key_id),
            )
            self._append(db, f"key:{key_id}", "key.revoked", revoked)
        return True

    # -- SMART Backend Services: clients and access tokens (ADR-0004) -------------------------
    # A client is a machine (viewer or service) of one organization, with the scopes it may be
    # granted and a public JWK Set registered by value (server/backend_services.py checks both).
    # Registering and revoking are chained under "client:<id>", with the keys' ids and RFC 7638
    # thumbprints. An access token is stored as SHA-256 with its client, the scope it was granted
    # and its expiry, nothing else: each lookup joins the client, so a change to its role,
    # scopes or organization, or its revocation, applies to its tokens at once.

    def register_client(
        self,
        *,
        name: str,
        role: str,
        org: str,
        created_by: str,
        scopes: str,
        jwks: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """A new client; `scopes` and `jwks` were checked by the caller. Its id is Mendari's."""
        if role not in MACHINE_ROLES:
            raise ValueError(f"a client's role is one of {MACHINE_ROLES}")
        if not scopes.split() or not jwks:
            raise ValueError("a client is registered with scopes and at least one key")
        client = {
            "client_id": secrets.token_hex(12),
            "name": name,
            "role": role,
            "org": org,
            "scope": scopes,
            "keys": _key_summary(jwks),
            "created_by": created_by,
            "created_at": _now(),
        }
        with self._db.session(exclusive=True) as db:
            db.all(
                "INSERT INTO oauth_clients (client_id, name, org, role, scopes, jwks, created_by, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    client["client_id"],
                    name,
                    org,
                    role,
                    scopes,
                    _canonical({"keys": list(jwks)}),
                    created_by,
                    client["created_at"],
                ),
            )
            self._append(db, f"client:{client['client_id']}", "client.registered", client)
        return client

    def clients(self, org: str | None = None) -> list[dict[str, Any]]:
        """Every client (of `org`, if given), oldest first, with its keys' ids and thumbprints."""
        where, params = ("WHERE org = ?", (org,)) if org else ("", ())
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT client_id, name, role, org, scopes AS scope, jwks, created_by, "
                f"created_at, revoked_at, revoked_by FROM oauth_clients {where} "
                "ORDER BY created_at",
                params,
            )
        return [
            {**{k: v for k, v in row.items() if k != "jwks"}, "keys": _key_summary(_jwks(row))}
            for row in rows
        ]

    def client(self, client_id: str) -> dict[str, Any] | None:
        """A client that is not revoked, with its public keys; None otherwise."""
        with self._db.session(exclusive=False) as db:
            row = db.one(
                "SELECT client_id, name, role, org, scopes AS scope, jwks FROM oauth_clients "
                "WHERE client_id = ? AND revoked_at IS NULL",
                (client_id,),
            )
        return None if row is None else {**row, "jwks": _jwks(row)}

    def revoke_client(self, client_id: str, org: str | None = None, *, revoked_by: str) -> bool:
        """Revoke a client of `org` (any, if None) and drop its tokens; False for an unknown or
        already revoked one. Its tokens stop working at once either way (`access_token`)."""
        with self._db.session(exclusive=True) as db:
            row = db.one(
                "SELECT org FROM oauth_clients WHERE client_id = ? AND revoked_at IS NULL",
                (client_id,),
            )
            if row is None or (org and row["org"] != org):
                return False
            revoked = {"client_id": client_id, "revoked_by": revoked_by, "revoked_at": _now()}
            db.all(
                "UPDATE oauth_clients SET revoked_at = ?, revoked_by = ? WHERE client_id = ?",
                (revoked["revoked_at"], revoked_by, client_id),
            )
            db.all("DELETE FROM oauth_tokens WHERE client_id = ?", (client_id,))
            self._append(db, f"client:{client_id}", "client.revoked", revoked)
        return True

    def issue_token(self, client_id: str, scope: str, seconds: int) -> tuple[str, int]:
        """A new access token for a client (already authenticated) and the scope it was granted;
        the secret and its expiry (Unix time). Stored hashed; expired tokens are purged."""
        secret = "mat_" + secrets.token_urlsafe(32)
        now = int(time.time())
        expires_at = now + seconds
        with self._db.session(exclusive=False) as db:
            db.all("DELETE FROM oauth_tokens WHERE expires_at <= ?", (now,))
            db.all(
                "INSERT INTO oauth_tokens (token_sha256, client_id, scope, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (_key(secret), client_id, scope, expires_at),
            )
        return secret, expires_at

    def access_token(self, secret: str) -> dict[str, Any] | None:
        """What an access token stands for, read from its client now (one join): None for an
        unknown or expired token, or one whose client was revoked."""
        with self._db.session(exclusive=False) as db:
            return db.one(
                "SELECT c.client_id, c.name, c.role, c.org, c.scopes AS registered, t.scope, "
                "t.expires_at FROM oauth_tokens t JOIN oauth_clients c "
                "ON c.client_id = t.client_id WHERE t.token_sha256 = ? AND t.expires_at > ? "
                "AND c.revoked_at IS NULL",
                (_key(secret), int(time.time())),
            )

    def iter_events(self, **filters: Any) -> Iterator[dict[str, Any]]:
        """Every event `events()` matches, page by page, without loading the whole chain."""
        after = 0
        while page := self.events(**filters, after_seq=after):
            yield from page
            after = page[-1]["seq"]

    def decision_rows(self, pack_id: str, since: str) -> list[dict[str, Any]]:
        """Each decision of a pack since a time (ISO), for monitoring: when, the outcome, the
        primary confidence, what a person or a downstream system said, and the cost."""
        scope, scoped = _scope()
        with self._db.session(exclusive=False) as db:
            return db.all(
                "SELECT d.id, d.created_at, d.outcome, d.confidence, d.recommendation, "
                "d.final_label, d.human_override, d.cost_usd, "
                "(SELECT COUNT(*) FROM feedback f WHERE f.decision_id = d.id "
                "AND f.superseded_by IS NULL AND f.rating = 'down') AS rated_wrong "
                f"FROM decisions d WHERE {scope} AND d.pack_id = ? AND d.created_at >= ? "
                "ORDER BY d.created_at",
                (*scoped, pack_id, since),
            )

    def pending_reviews(
        self, pack_id: str | None = None, limit: int = 50, *, reader: str | None = None
    ) -> list[dict[str, Any]]:
        """The queue, most urgent first; under PHI mode `encrypt`, with inputs for a `reader`."""
        items = self.query(pack_id=pack_id, review_status="pending", limit=limit)
        return self.reveal_inputs(items, reader)

    def stats(self, pack_id: str | None = None) -> dict[str, Any]:
        scope, scoped = _scope()
        packs = ("AND d.pack_id = ?", (pack_id,)) if pack_id else ("", ())
        params = (*scoped, *packs[1])
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT outcome, recommendation, final_label, human_override, review_status, "
                f"latency_ms, cost_usd, created_at FROM decisions d WHERE {scope} {packs[0]}",
                params,
            )
            # Each person's current rating: a changed rating counts once, as its latest value.
            ratings = db.all(
                "SELECT f.rating, f.expected_label, d.outcome FROM feedback f "
                "JOIN decisions d ON d.id = f.decision_id "
                f"WHERE f.superseded_by IS NULL AND {scope} {packs[0]}",
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
        self,
        *,
        pack_id: str | None = None,
        rating: Rating | None = None,
        limit: int = 100,
        reader: str | None = None,
    ) -> list[dict[str, Any]]:
        """Current feedback (each person's latest rating of a decision), newest first, each with
        the decision it rates and the right answer if the rating gave one; under PHI mode
        `encrypt`, with inputs for a `reader`."""
        scope, scoped = _scope()
        clauses, params = ["f.superseded_by IS NULL", scope], list(scoped)
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
        return self.reveal_inputs([_feedback_item(r) for r in rows], reader, id_key="decision_id")

    def reports(self, pack_id: str | None = None) -> list[dict[str, Any]]:
        scope, scoped = _scope()
        packs = ("AND pack_id = ?", (pack_id,)) if pack_id else ("", ())
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                f"SELECT report FROM eval_reports d WHERE {scope} {packs[0]} "
                "ORDER BY created_at DESC",
                (*scoped, *packs[1]),
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


def _decision(db: _Session, decision_id: str) -> dict[str, Any] | None:
    """One decision row (parsed), if this request's organization may see it."""
    scope, scoped = _scope()
    row = db.one(f"SELECT * FROM decisions d WHERE id = ? AND {scope}", (decision_id, *scoped))
    return None if row is None else _row(row)


def _events_of(db: _Session, decision_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """The events of these decisions, by decision, in chain order; scoped through the decision,
    so another organization's decisions come back empty."""
    scope, scoped = _scope()
    marks = ", ".join("?" for _ in decision_ids)
    rows = db.all(
        "SELECT e.seq, e.decision_id, e.event, e.payload, e.created_at, e.prev_hash, e.hash "
        "FROM audit_events e JOIN decisions d ON d.id = e.decision_id "
        f"WHERE e.decision_id IN ({marks}) AND {scope} ORDER BY e.seq",
        (*decision_ids, *scoped),
    )
    found: dict[str, list[dict[str, Any]]] = {decision_id: [] for decision_id in decision_ids}
    for row in rows:
        event = {k: v for k, v in row.items() if k != "decision_id"}
        found[row["decision_id"]].append({**event, "payload": json.loads(row["payload"])})
    return found


# A chained event joined with its decision's pack and record (`events()`, `event()`).
_JOINED = (
    "e.seq, e.decision_id, e.event, e.payload, e.created_at, e.prev_hash, e.hash, d.pack_id, "
    "d.pack_version, d.record FROM audit_events e JOIN decisions d ON d.id = e.decision_id"
)


def _joined(row: Mapping[str, Any]) -> dict[str, Any]:
    return {**row, "payload": json.loads(row["payload"]), "record": json.loads(row["record"])}


# -- searches (core/query.py) ----------------------------------------------------------------
# A refused input: the stored `refused` flag, or, on rows from before it existed, a BLOCK
# without confidence (no provider ran, so the primary question has none). The same predicate on
# the same columns as `task_fhir.refused`, so that what a FHIR Task renders and what a search on
# its status finds cannot disagree (a test checks both on every kind).
_REFUSED = (
    "(COALESCE(d.refused, CASE WHEN d.outcome = 'BLOCK' AND d.confidence IS NULL "
    "THEN 1 ELSE 0 END) = 1)"
)
_STATES = {
    "refused": _REFUSED,
    "pending": f"(d.review_status = 'pending' AND NOT {_REFUSED})",
    "final": f"(d.review_status <> 'pending' AND NOT {_REFUSED})",
}
# When the last event that changed a decision's result was chained: its Task's lastUpdated.
_UPDATED = (
    "(SELECT v.created_at FROM audit_events v WHERE v.decision_id = d.id AND v.event IN ("
    + ", ".join(f"'{kind}'" for kind in RESULT_EVENTS)
    + ") ORDER BY v.seq DESC LIMIT 1)"
)
# What each field a query may name is in SQL: a column (or expression), a pair of columns, or
# one predicate per value (the states).
_Field = str | tuple[str, str] | Mapping[str, str]
_DECISION_SQL: dict[str, _Field] = {
    "id": "d.id",
    "outcome": "d.outcome",
    "priority": "d.priority",
    "pack": ("d.pack_id", "d.pack_version"),
    "state": _STATES,
    "created": "d.created_at",
    "updated": _UPDATED,
}
_EVENT_SQL: dict[str, _Field] = {
    "event": ("e.decision_id", "e.seq"),
    "decision": "e.decision_id",
    "kind": "e.event",
    "recorded": "e.created_at",
}


def _conditions(
    where: Sequence[Sequence[Atom]], fields: Mapping[str, _Field]
) -> tuple[list[str], list[Any]]:
    """A query's conditions as SQL (ANDed by the caller), with their parameters."""
    clauses: list[str] = []
    params: list[Any] = []
    for condition in where:
        parts = []
        for atom in condition:
            sql, values = _atom(atom, fields[atom.field])
            parts.append(sql)
            params += values
        clauses.append(f"({' OR '.join(parts)})" if parts else "1 = 0")
    return clauses, params


def _atom(atom: Atom, target: _Field) -> tuple[str, list[Any]]:
    if isinstance(atom, Within):
        assert isinstance(target, str), atom.field
        sides = [(f"{target} >= ?", atom.low), (f"{target} < ?", atom.high)]
        present = [(sql, value) for sql, value in sides if value is not None]
        inside = " AND ".join(sql for sql, _ in present) or "1 = 1"
        return (f"NOT ({inside})" if atom.outside else f"({inside})"), [v for _, v in present]
    values = sorted(atom.values, key=repr)  # a stable statement for a given query
    if not values:
        return "1 = 0", []
    if isinstance(target, str):
        return f"{target} IN ({', '.join('?' for _ in values)})", values
    if isinstance(target, tuple):
        pairs = " OR ".join(f"({target[0]} = ? AND {target[1]} = ?)" for _ in values)
        return f"({pairs})", [part for pair in values for part in _pair(pair)]
    return f"({' OR '.join(target[str(value)] for value in values)})", []


def _pair(value: Hashable) -> tuple[Any, Any]:
    if not (isinstance(value, tuple) and len(value) == 2):
        raise ValueError(f"expected a pair, got {value!r}")
    return value[0], value[1]


def _page(db: _Session, sql: str, params: Sequence[Any], limit: int) -> list[dict[str, Any]]:
    """Up to `limit` rows and one more (whether there is a next page), or none for 0."""
    return db.all(sql, (*params, limit + 1)) if limit > 0 else []


def _total(db: _Session, sql: str, params: Sequence[Any]) -> int:
    row = db.one(sql, params)
    return int(row["n"]) if row else 0


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


def _check_label(
    pack_id: str, record: Mapping[str, Any], label: str, labels: QuestionLabels | None
) -> None:
    """A final label must be one of the labels of the decision's primary question: those its
    answer gave probabilities for (the pack version that decided), or else, for a decision with
    no answer (refused before any provider ran, or its provider failed), the installed pack's.
    With neither, no label can be checked, so none is accepted."""
    primary = record.get("primary")
    answer = (record.get("answers") or {}).get(primary) or {}
    known = list(answer.get("probabilities") or ())
    if not known and labels is not None and isinstance(primary, str):
        known = list(labels(pack_id, primary) or ())
    if not known:
        raise ReviewError("this decision's labels are unknown here, so no label can be recorded")
    if label not in known:
        # The label is not echoed: a caller could have put anything there.
        raise ReviewError(f"not one of the labels of this decision's question ({sorted(known)})")


def _scope() -> tuple[str, tuple[str, ...]]:
    """The SQL condition (on the table aliased `d`) that keeps a query to the organization this
    request acts for; nothing in open mode. Rows from before tenancy belong to the default one."""
    org = tenancy.current()
    if org is None:
        return "1 = 1", ()
    return "COALESCE(d.org, ?) = ?", (tenancy.DEFAULT_ORG, org)


def _key(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _jwks(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    keys: list[dict[str, Any]] = json.loads(row["jwks"])["keys"]
    return keys


def _key_summary(jwks: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """A client's keys as chained and listed: id, type and RFC 7638 thumbprint, which name a
    public key without repeating it."""
    return [{"kid": k["kid"], "kty": k["kty"], "thumbprint": _thumbprint(k)} for k in jwks]


def _thumbprint(jwk: Mapping[str, Any]) -> str:
    """RFC 7638: SHA-256 of the key's required members, in order, without whitespace."""
    members = ("e", "kty", "n") if jwk["kty"] == "RSA" else ("crv", "kty", "x", "y")
    body = json.dumps({m: jwk[m] for m in members}, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(body.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


_SESSION_KIND = re.compile(r"[a-z][a-z0-9-]{0,39}")


# A bucket's start for the next request: its `tat`, or now if that has passed (GCRA's max).
_FROM = "(CASE WHEN rate_limits.tat > ? THEN rate_limits.tat ELSE ? END)"


def _wait(row: Mapping[str, Any] | None, interval: int, tolerance: int, now: int) -> int | None:
    """Milliseconds until a bucket at `row` admits one more request; None: it admits now."""
    tat = max(int(row["tat"]), now) if row is not None else now
    wait = tat + interval - tolerance - now
    return wait if wait > 0 else None


def _session_key(kind: str, value: str) -> str:
    """A session-table row's key: sha256(kind || 0x00 || value). Kinds are short lowercase names
    (never a NUL), so two (kind, value) pairs share a preimage only if they are equal."""
    if not _SESSION_KIND.fullmatch(kind):
        raise ValueError(f"not a session kind: {kind!r}")
    return hashlib.sha256(kind.encode() + b"\x00" + value.encode()).hexdigest()


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
