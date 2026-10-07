# SPDX-License-Identifier: Apache-2.0
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
from collections import Counter
from collections.abc import Callable, Hashable, Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from mendari.core import accounts, clock, tenancy
from mendari.core.db import Backend as _Backend
from mendari.core.db import Session as _Session
from mendari.core.db import connect
from mendari.core.phi import LEGACY, NOTE_AAD, PhiPolicy, key_id, token_key
from mendari.core.query import Atom, Page, Query, Within
from mendari.core.session_seal import SealError, seal, unseal
from mendari.core.spec import Outcome
from mendari.errors import AuthError, MendariError, NoteError, ProviderError, ReviewError

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
# A person acknowledged a notice version (mendari.notices; chained under notice:<v>:<id>).
NOTICE_EVENT = "notice.acknowledged"
SYSTEM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,99}$")
# A served pack's labels for one of its questions, by (pack id, question); None when unknown.
QuestionLabels = Callable[[str, str], Sequence[str] | None]
# Notes (Phase 4b, ADR-0005: commit, don't copy). Free text a person or a payer writes about a
# decision is kept in `notes`, apart from the chain, which keeps `note: null` and a `note_ref`:
# the note's id and sha256(prefix || salt || text). The 32-byte salt is only in the note's row,
# so the commitment says nothing about the text, and once the row is erased it is noise.
# Free text chained as is before then (by event) is never rewritten, only hidden when read.
LEGACY_TEXT = {
    "review.resolved": ("note",),
    "feedback.recorded": ("note",),
    "pas.submitted": ("authorization", "disposition"),
}
# Why a person erases a note (chained, so a code): it should not have been written, or was wrong.
# Retention erases with its own reason, "retention".
ERASE_REASONS = ("request", "error")
# Retention deletes this many rows per transaction (and chains their events with them), so a
# backlog never holds the chain lock long and each batch done stays done.
PURGE_BATCH = 500
# The chain id a purge's summary is chained under (`retention.purged`): no decision's.
RETENTION_CHAIN = "retention"
# The `settings` row a database is marked with once a store opens it under PHI mode `hash` or
# `encrypt` (the first such mode): from then on no store under `off` keeps input text in it.
PROTECTED = "phi_protected"
# Key rotation (LP-09). Key events are chained under `KEYS_CHAIN` (`phi.rotated`,
# `input_hash.epoch`), no decision's. `settings` rows: from the first rotation on, new tokens name
# their key (`PHI_TOKENS_V2`); the key the last rotation re-sealed under (`ROTATION_TARGET`);
# each key a rotation moved values off (`RETIRED` + its id), which then seals nothing more here;
# each PHI key a deployment ever held (`KNOWN` + its id), so a value naming another was altered;
# and each input hash key's epoch (`HASH_EPOCH` + its id: since when).
KEYS_CHAIN = "keys"
PHI_TOKENS_V2 = "phi_tokens_v2"
ROTATION_TARGET = "phi_key_target"
RETIRED = "phi_key_retired:"
KNOWN = "phi_key_known:"
HASH_EPOCH = "hash_epoch:"
ROTATE_BATCH = 200  # values a rotation re-seals per batch (one chained event each)
CHECK_FAILURES_SHOWN = 50
# Rows per insert when chaining several events at once (`_append_many`): a page is at most 500.
_APPEND_BATCH = 500

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
    org         TEXT,
    ledger      TEXT
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
    ciphertext   TEXT NOT NULL,
    details      TEXT
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
CREATE TABLE IF NOT EXISTS automation_holds (
    id           TEXT PRIMARY KEY,
    org          TEXT,
    pack_id      TEXT,
    pack_digest  TEXT,
    reason       TEXT NOT NULL,
    set_by       TEXT NOT NULL,
    set_at       TEXT NOT NULL,
    lifted_by    TEXT,
    lifted_at    TEXT
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
    revoked_by  TEXT,
    replaced_by TEXT
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
);
CREATE TABLE IF NOT EXISTS notes (
    id          TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    event       TEXT NOT NULL,
    org         TEXT,
    created_at  TEXT NOT NULL,
    sealed      TEXT,
    erased_at   TEXT,
    erased_by   TEXT
);
CREATE INDEX IF NOT EXISTS notes_decision ON notes (decision_id);
CREATE TABLE IF NOT EXISTS users (
    id               TEXT PRIMARY KEY,
    issuer           TEXT NOT NULL,
    subject          TEXT NOT NULL,
    email            TEXT,
    name             TEXT,
    created_at       TEXT NOT NULL,
    last_sign_in_at  TEXT,
    UNIQUE (issuer, subject)
);
CREATE TABLE IF NOT EXISTS organizations (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memberships (
    id             TEXT PRIMARY KEY,
    org            TEXT NOT NULL,
    user_id        TEXT,
    pending_email  TEXT,
    roles          TEXT NOT NULL,
    created_by     TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    removed_at     TEXT,
    removed_by     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS memberships_member ON memberships (org, user_id)
    WHERE removed_at IS NULL AND user_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS memberships_pending ON memberships (org, pending_email)
    WHERE removed_at IS NULL AND user_id IS NULL;
CREATE TABLE IF NOT EXISTS invitations (
    id            TEXT PRIMARY KEY,
    token_sha256  TEXT NOT NULL UNIQUE,
    org           TEXT NOT NULL,
    email         TEXT NOT NULL,
    roles         TEXT NOT NULL,
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    accepted_at   TEXT,
    accepted_by   TEXT,
    revoked_at    TEXT,
    revoked_by    TEXT
);
CREATE TABLE IF NOT EXISTS access_requests (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    org         TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    handled_at  TEXT,
    handled_by  TEXT,
    outcome     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS access_requests_open ON access_requests (user_id, org)
    WHERE handled_at IS NULL;
CREATE TABLE IF NOT EXISTS settings (
    name  TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

# The tables a read-only store reads (`_read_only_check`); the first two must be there.
_READ_TABLES = ("audit_events", "decisions", "feedback", "decision_inputs", "notes", "settings")

# Columns added after their table's first release. New databases get them from _TABLES; older
# ones get them from _migrate(). Additive only: nullable, no default, never renamed or dropped.
_ADDED_COLUMNS = (
    ("feedback", "expected_question", "TEXT"),
    ("feedback", "expected_label", "TEXT"),
    ("feedback", "superseded_by", "TEXT"),
    ("decisions", "org", "TEXT"),  # tenancy (v0.6); NULL is the default organization
    ("eval_reports", "org", "TEXT"),
    ("eval_reports", "ledger", "TEXT"),  # the run's mendari.ledger/1 (LP-10); NULL before it
    # 1 when the input was refused before any provider ran (Phase 3); NULL on older rows, which
    # fall back to "BLOCK without confidence" (_REFUSED, task_fhir.refused).
    ("decisions", "refused", "INTEGER"),
    # Scoped keys that expire (Phase 4, ADR-0004): NULL scopes is a key with its role's
    # permissions; a NULL expiry is set once at startup (_expire_legacy_keys).
    ("api_keys", "scopes", "TEXT"),
    ("api_keys", "expires_at", "TEXT"),
    ("api_keys", "revoked_by", "TEXT"),
    ("api_keys", "replaced_by", "TEXT"),  # a rotated key's replacement (LP-09)
    # What a record leaves out beside its kept input, sealed (Phase 4b): each answer's evidence.
    # NULL: none, or a row from before.
    ("decision_inputs", "details", "TEXT"),
)
# API keys (ADR-0004): machines are viewer or service; a key lives 90 days unless asked
# otherwise, 365 at most; keys made before keys expired get 30 days from the first start of
# the release that brought expiry.
MACHINE_ROLES = ("viewer", "service")
KEY_DAYS = 90
KEY_MAX_DAYS = 365
# A rotated key keeps working this many hours (24 unless asked, a week at most), then expires.
KEY_OVERLAP_HOURS = 24
KEY_MAX_OVERLAP_HOURS = 168
LEGACY_KEY_DAYS = 30
# Expired session-table rows one put deletes (smart_put).
SESSION_PURGE_BATCH = 100
# Session-table kinds sealed under the secret that names the row (core/session_seal.py,
# ADR-0006): an EHR launch's session (its cookie) and its pending launch (its binding cookie,
# never its `state`, which travels in URLs, so `pending` must be given its secret).
# A person's session in memberships mode (`member`, server/auth.py) is sealed too: its organization
# and membership are what it may act for.
SEALED_KINDS = frozenset({"session", "pending", "member"})

_PRIORITY_ORDER = (
    "CASE priority WHEN 'stat' THEN 0 WHEN 'asap' THEN 1 WHEN 'urgent' THEN 2 ELSE 3 END"
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _mark(db: _Session, name: str, value: str) -> None:
    """Set a `settings` row unless it is set: the first value stays."""
    db.all(
        "INSERT INTO settings (name, value) VALUES (?, ?) ON CONFLICT (name) DO NOTHING",
        (name, value),
    )


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
        self,
        target: str | Path,
        *,
        migrate_url: str | None = None,
        phi: PhiPolicy | None = None,
        marks: bool = True,
        read_only: bool = False,
    ):
        """`target` is a SQLite path (or ":memory:") or a postgres:// / postgresql:// URL.

        `migrate_url` is an optional direct (unpooled) Postgres URL used only for schema setup.
        `phi` is how inputs are kept (`core/phi.py`; default: as the pack says). `marks`
        (default): opening marks the database with this store's PHI mode and keys (`_protected`);
        a read-only tool passes False and only reads the marks. `read_only` opens an existing
        Mendari database and writes nothing to it, not even a mark or a migration
        (`core/db.py`); a database written by an older Mendari is read as it is
        (`unmigrated`, `require_current`).
        """
        self.phi = phi or PhiPolicy()
        self.read_only = read_only
        self._db: _Backend = connect(
            str(target),
            schema=_TABLES,
            migrate=_migrate,
            lock_key=_CHAIN_LOCK,
            migrate_url=migrate_url,
            read_only=read_only,
        )
        self.path = str(target) if self._db.kind == "sqlite" else None
        # Read-only: what an older database lacks that a migration would add (none otherwise).
        self.unmigrated: list[str] = []
        if read_only:
            marks = False
            self.unmigrated = self._read_only_check()
        # environment -> (the CapabilityStatement version served, since when): capability_since
        self._capability_since: dict[str, tuple[str, str]] = {}
        self._hash_epoch: str | None = None  # the hash key id this process last recorded under
        self.protected = self._protected(marks)

    def _read_only_check(self) -> list[str]:
        """A read-only store's target is a Mendari database: its chain and decisions tables are
        there. Returns the tables and columns a migration would add that it lacks."""
        with self._db.session(exclusive=False) as db:
            present = {table: db.columns(table) for table in _READ_TABLES}
            if not present["audit_events"] or not present["decisions"]:
                self._db.close()
                raise MendariError(
                    "not a Mendari database (no audit chain in it): give the server's --db"
                )
            lacking = [table for table, columns in present.items() if not columns]
            lacking += [
                f"{table}.{column}"
                for table, column, _ in _ADDED_COLUMNS
                if present.get(table) and column not in present[table]
            ]
            return lacking

    def require_current(self, command: str) -> None:
        """Refuses, in one line, a read-only command that needs what an older database lacks:
        opening it read-only never migrates it (`mendari serve` on it once does)."""
        if self.unmigrated:
            raise MendariError(
                f"{command}: this database was written by an older Mendari and lacks "
                f"{', '.join(self.unmigrated[:3])}; start the server on it once (it migrates), "
                "then run this again (`mendari audit verify` reads it as it is)"
            )

    def _protected(self, marks: bool = True) -> str | None:
        """The PHI mode that first protected this database (`hash` or `encrypt`), marking it now
        (with `marks`) if this store's mode is one; None while only mode `off` has opened it. A
        store under `encrypt` also marks each key it holds as known, and, with read keys (a
        rotation under way), that tokens name their key from now on; every store that finds that
        mark seals so."""
        if "settings" in self.unmigrated:
            return None  # an older database, read as it is: nothing marked it
        with self._db.session(exclusive=False) as db:
            if marks and self.phi.mode != "off":
                _mark(db, PROTECTED, self.phi.mode)
            if marks and self.phi.mode == "encrypt":
                for known in sorted(self.phi.ring_ids):
                    _mark(db, KNOWN + known, _now())
                if self.phi.read_keys:
                    _mark(db, PHI_TOKENS_V2, "1")
            found = {
                row["name"]: str(row["value"])
                for row in db.all(
                    "SELECT name, value FROM settings WHERE name IN (?, ?)",
                    (PROTECTED, PHI_TOKENS_V2),
                )
            }
        if PHI_TOKENS_V2 in found and not self.phi.tag_tokens:
            self.phi = replace(self.phi, tag_tokens=True)
        return found.get(PROTECTED)

    def ready_to_keep(self) -> None:
        """Whether this store may keep what a decision brings: not under `off` in a protected
        database (`refuse_plaintext`), nor under `encrypt` with a key a rotation retired
        (`_refuse_retired`). The server checks at startup; a decision before any provider
        runs; each write again, in its own transaction."""
        self.refuse_plaintext()
        if self.phi.mode == "encrypt":
            with self._db.session(exclusive=False) as db:
                self._refuse_retired(db)

    def _refuse_retired(self, db: _Session) -> None:
        """Under `encrypt`, nothing is sealed with a key a rotation moved values off: a deployment
        still on the previous key, once the rotation began, refuses instead of sealing what the
        rotation would leave behind."""
        current = key_id(self.phi.key)
        if db.one("SELECT name FROM settings WHERE name = ?", (RETIRED + current,)):
            raise MendariError(
                f"This deployment's PHI key {current} was retired by a key rotation: deploy the "
                "current MENDARI_PHI_KEY (the retired one belongs in MENDARI_PHI_READ_KEYS)."
            )

    def _mark_hash_epoch(self, db: _Session) -> str | None:
        """A new input hash key starts an epoch: the first record hashed under a key id this
        database has not seen chains `input_hash.epoch` (the key id), once per key, whichever
        deployments share the database. Old hashes stay as they were, under the key id each
        names; hashes of two epochs never match, and nothing here tries. The key id, for the
        caller to remember once its transaction commits (None: already remembered)."""
        current = key_id(self.phi.hash_key)
        if self._hash_epoch == current:
            return None
        added = db.all(
            "INSERT INTO settings (name, value) VALUES (?, ?) ON CONFLICT (name) DO NOTHING "
            "RETURNING name",
            (HASH_EPOCH + current, _now()),
        )
        if added:
            self._append(db, KEYS_CHAIN, "input_hash.epoch", {"key_id": current})
        return current

    def refuse_plaintext(self) -> None:
        """Mode `off` keeps no input text (an input in its record, a note) in a database a
        protected mode kept: the server refuses to start so, and a decision or a write here
        refuses before it begins. Until the mark is set, each check reads it again (another
        process may set it)."""
        if self.phi.mode != "off":
            return
        if self.protected is None:
            self.protected = self._protected()
        if self.protected:
            raise MendariError(
                f"This database is protected (first kept under PHI mode {self.protected}): run "
                "with the deployment's PHI mode, hash or encrypt, and its keys. A protected "
                "database never takes plaintext again."
            )

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
        """Keep a decision and chain its digest. The input, and each answer's evidence (which can
        quote it: a lab value and its date), stay in the record only under PHI mode `off` with a
        pack that keeps inputs; under `encrypt` with such a pack both are sealed apart
        (`decision_inputs`), so retention can delete them; otherwise neither is kept. The
        caller's own answer (`/decide`) is unchanged. With sign-in on, the chained event also
        names who asked (`requested_by`, the principal's chain name, `tenancy.requester`)."""
        record = result.to_record()
        keep = store_input == "full" and self.phi.mode != "hash"
        in_record = keep and self.phi.mode == "off"
        if in_record:
            self.refuse_plaintext()
        record["input"] = dict(inputs) if in_record else None
        evidence = {} if in_record else _take_evidence(record)
        status = "pending" if result.outcome.value in _REVIEWABLE else "not_required"
        with self._db.session(exclusive=True) as db:
            if keep and self.phi.mode == "encrypt":
                self._refuse_retired(db)
            epoch = self._mark_hash_epoch(db) if self.phi.hash_key else None
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
            created: dict[str, Any] = {"record_sha256": _sha(record)}
            requester = tenancy.requester()
            if requester is not None:  # sign-in on: who asked; never in open mode
                created["requested_by"] = requester
            self._append(db, result.id, "decision.created", created)
            if keep and self.phi.mode == "encrypt":  # apart from the chained record
                details = {"evidence": evidence}
                db.all(
                    "INSERT INTO decision_inputs (decision_id, created_at, ciphertext, details) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        result.id,
                        result.created_at,
                        self.phi.encrypt(result.id, dict(inputs)),
                        self.phi.seal_details(result.id, details) if evidence else None,
                    ),
                )
        if epoch is not None:  # committed: this process need not look again
            self._hash_epoch = epoch

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
        of its primary question (`_check_label`). A note is kept apart from the chain (`notes`);
        the answer repeats it to whoever wrote it."""
        if not reviewer.strip():
            raise ReviewError("a reviewer identity is required")
        note = self._may_keep(note)
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
                "note": None,
                "note_ref": self._keep_note(db, decision_id, "review.resolved", note),
            }
            self._append(db, decision_id, "review.resolved", payload)
        return {"id": decision_id, "resolved_at": resolved_at, **payload, "note": note}

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

    def record_event(
        self,
        decision_id: str,
        event: str,
        payload: Mapping[str, Any],
        *,
        note: str | None = None,
    ) -> None:
        """A follow-up on a decision (e.g. `pas.submitted`), chained after its other events, with
        its free text (the payer's words) kept as a note (`note_ref`)."""
        note = self._may_keep(note)
        with self._db.session(exclusive=True) as db:
            scope, scoped = _scope()
            row = db.one(
                f"SELECT id FROM decisions d WHERE id = ? AND {scope}", (decision_id, *scoped)
            )
            if row is None:
                raise ReviewError(f"unknown decision {decision_id}")
            chained = {**payload, "note_ref": self._keep_note(db, decision_id, event, note)}
            self._append(db, decision_id, event, chained)

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
        ratings of a decision count as one person, so a mis-click can always be put right. The
        note is kept apart from the chain (`notes`, under the rating's id)."""
        if rating not in ("up", "down"):
            raise ReviewError("rating must be 'up' or 'down'")
        if expected is not None and rating != "down":
            raise ReviewError("only a thumbs-down carries the right answer")
        reporter = (reporter or "").strip() or None
        note = self._may_keep(note)
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
            # The `note` column is only read now: rows from before notes left the chain.
            db.all(
                "INSERT INTO feedback (id, decision_id, created_at, pack_id, rating, reporter, "
                "expected_question, expected_label) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    feedback_id,
                    decision_id,
                    created_at,
                    row["pack_id"],
                    rating,
                    reporter,
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
                "note": None,
                "note_ref": self._keep_note(
                    db, decision_id, "feedback.recorded", note, note_id=feedback_id
                ),
                "expected": answer,
                "replaces": previous["id"] if previous else None,
            }
            self._append(db, decision_id, "feedback.recorded", payload)
        return {
            "id": feedback_id,
            "decision_id": decision_id,
            "created_at": created_at,
            **payload,
            "note": note,
        }

    def save_report(self, report: Mapping[str, Any], *, ledger: bytes | None = None) -> None:
        """Keep an evaluation report, and the ledger its result is computed from, for the
        organization; chained as `eval.recorded` with ids and hashes only (which report, result
        and ledger, of which pack digest and dataset)."""
        from mendari.evals.result import normalize

        run = normalize(report)["run"]
        org = tenancy.current() or tenancy.DEFAULT_ORG
        recorded = {
            "report": run["id"],
            "result_sha256": report.get("result_sha256"),
            "ledger": (report.get("ledger") or {}).get("id"),
            "pack_id": report["pack"]["id"],
            "pack_digest": report["pack"].get("digest"),
            "dataset_sha256": report["dataset"].get("sha256"),
            "org": org,
        }
        with self._db.session(exclusive=True) as db:
            db.all(
                "INSERT INTO eval_reports (id, created_at, pack_id, dataset, report, org, ledger) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run["id"],
                    run["created_at"],
                    report["pack"]["id"],
                    report["dataset"]["name"],
                    _canonical(report),
                    org,
                    None if ledger is None else ledger.decode("ascii"),
                ),
            )
            self._append(db, f"eval:{run['id']}", "eval.recorded", recorded)

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

    def rate_take(
        self, bucket: str, interval: int, tolerance: int, now: int, cost: int = 1
    ) -> int | None:
        """Admit `cost` requests into `bucket` at once (None), or refuse them all without
        counting any: the milliseconds until they would be admitted. One statement, atomic
        across instances. More than the burst (`cost * interval > tolerance`) never fits, so
        it is refused without touching the bucket."""
        step = interval * cost
        with self._db.session(exclusive=False) as db:
            if step > tolerance:
                row = db.one("SELECT tat FROM rate_limits WHERE bucket = ?", (bucket,))
                return _wait(row, step, tolerance, now)
            admitted = db.one(
                "INSERT INTO rate_limits (bucket, tat, expires_at) VALUES (?, ?, ?) "
                f"ON CONFLICT (bucket) DO UPDATE SET tat = {_FROM} + ?, expires_at = {_FROM} + ? "
                f"WHERE {_FROM} + ? - ? <= ? RETURNING tat",
                (
                    *(bucket, now + step, now + step),
                    *(now, now, step),
                    *(now, now, step),
                    *(now, now, step, tolerance, now),
                ),
            )
            if admitted is not None:
                return None
            row = db.one("SELECT tat FROM rate_limits WHERE bucket = ?", (bucket,))
        return _wait(row, step, tolerance, now)

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
        self._append_many(db, event, [(decision_id, payload)])

    def _append_many(
        self, db: _Session, event: str, entries: Sequence[tuple[str, Any]]
    ) -> list[dict[str, Any]]:
        """Chain one kind of event on several decisions (`(decision_id, payload)`), in order:
        the chain N `_append`s would write, with one read of its head and one insert (per
        `_APPEND_BATCH` rows), so 2 round trips under the lock instead of 2N. Each row's
        `prev_hash` is the row before's `hash`, so `verify()` holds only if `seq` follows the
        insert's order, as an identity column does. The events, as `_events_of` gives them."""
        if not entries:
            return []
        last = db.one("SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1")
        prev = last["hash"] if last else GENESIS
        rows = []
        for decision_id, payload in entries:
            created_at = _now()
            body = _canonical(payload)
            digest = _chain_hash(prev, decision_id, event, body, created_at)
            rows.append((decision_id, event, body, created_at, prev, digest))
            prev = digest
        chained = []
        for start in range(0, len(rows), _APPEND_BATCH):
            batch = rows[start : start + _APPEND_BATCH]
            marks = ", ".join("(?, ?, ?, ?, ?, ?)" for _ in batch)
            written = db.all(
                "INSERT INTO audit_events (decision_id, event, payload, created_at, prev_hash, "
                f"hash) VALUES {marks} RETURNING seq",
                [value for row in batch for value in row],
            )
            seqs = sorted(int(row["seq"]) for row in written)
            for (decision_id, kind, body, created_at, before, digest), seq in zip(
                batch, seqs, strict=True
            ):
                chained.append(
                    {
                        "seq": seq,
                        "decision_id": decision_id,
                        "event": kind,
                        "payload": json.loads(body),
                        "created_at": created_at,
                        "prev_hash": before,
                        "hash": digest,
                    }
                )
        return chained

    # -- notes ---------------------------------------------------------------------------------
    # Notes follow inputs. Who may read one: under PHI mode `off`, whoever may read the decision
    # (as its input is in the record); otherwise only a `reader` (a signed-in role that may read
    # inputs), and each such read is chained as the decision's `input.read`. A note is kept
    # sealed (AES-GCM under the PHI key, `phi.seal_note`) under `encrypt`, as plain JSON under
    # `off` (a row's `sealed` is JSON when it starts with "{", which base64 never does), and is
    # refused under `hash`, which promises to keep no free text. Erasing a note (a person's
    # request, or retention) empties its row and chains `note.erased`.

    def _may_keep(self, note: str | None) -> str | None:
        """The note to keep (None for none or blank); refused before anything is written under
        PHI mode `hash`, so a review or rating with a note is refused whole, never kept without
        it."""
        note = (note or "").strip() or None
        if note is not None and self.phi.mode == "hash":
            raise NoteError("PHI mode hash keeps no free text: send it without a note")
        return note

    def _keep_note(
        self,
        db: _Session,
        decision_id: str,
        event: str,
        text: str | None,
        *,
        note_id: str | None = None,
    ) -> dict[str, str] | None:
        """Keep a note in the session that chains its event; its `note_ref`, or None."""
        if text is None:
            return None
        self.refuse_plaintext()  # under `off` a note is kept as it is
        if self.phi.mode == "encrypt":
            self._refuse_retired(db)
        note_id = note_id or str(uuid.uuid4())
        salt = secrets.token_bytes(32)
        body = {"salt": base64.b64encode(salt).decode(), "text": text}
        sealed = (
            self.phi.seal_note(note_id, decision_id, body)
            if self.phi.mode == "encrypt"
            else _canonical(body)
        )
        db.all(
            "INSERT INTO notes (id, decision_id, event, org, created_at, sealed) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (note_id, decision_id, event, tenancy.current() or tenancy.DEFAULT_ORG, _now(), sealed),
        )
        return {"id": note_id, "sha256": _commitment(salt, text)}

    def _note_body(self, row: Mapping[str, Any]) -> dict[str, Any] | None:
        """A kept note's `{salt, text}`; None when sealed under a key this deployment does not
        have (it is no longer in mode `encrypt`)."""
        sealed = str(row["sealed"])
        if sealed.startswith("{"):
            body: dict[str, Any] = json.loads(sealed)
            return body
        if self.phi.mode != "encrypt":
            return None
        opened: dict[str, Any] = self.phi.open_note(row["id"], row["decision_id"], sealed)
        return opened

    def _note_view(self, row: Mapping[str, Any], reader: str | None) -> tuple[str, str | None]:
        """What one may see of a kept note: ("open", text), ("sealed", None) or ("erased",
        None)."""
        if row["sealed"] is None:
            return "erased", None
        if self.phi.mode != "off" and not reader:
            return "sealed", None
        body = self._note_body(row)
        return ("sealed", None) if body is None else ("open", str(body["text"]))

    def erase_note(self, note_id: str, *, erased_by: str, reason: str) -> dict[str, Any]:
        """Erase a note (`mendari notes erase`): its row keeps its id, decision and when, and
        loses its text and salt, so the commitment chained on its event is noise from now on;
        `note.erased` is chained on its decision. Backups, the database's history and earlier
        exports still hold it (docs/deploying-with-phi.md)."""
        if reason not in ERASE_REASONS:
            raise NoteError(f"the reason is one of {ERASE_REASONS}")
        if not SYSTEM_ID.fullmatch(erased_by):
            raise NoteError("who erases is an identifier, such as an email address")
        with self._db.session(exclusive=True) as db:
            row = db.one("SELECT decision_id, sealed FROM notes WHERE id = ?", (note_id,))
            if row is None or row["sealed"] is None:
                raise NoteError(f"no note {note_id} to erase")
            erased_at = _now()
            db.all(
                "UPDATE notes SET sealed = NULL, erased_at = ?, erased_by = ? WHERE id = ?",
                (erased_at, erased_by, note_id),
            )
            payload = {"note_id": note_id, "reason": reason, "erased_by": erased_by}
            self._append(db, row["decision_id"], "note.erased", payload)
        return {**payload, "decision_id": row["decision_id"], "erased_at": erased_at}

    def purge_notes(self, older_than_days: int) -> int:
        """Retention, with inputs (`purge`): erase notes older than N days, in batches of
        `PURGE_BATCH`, each chained with its `note.erased` events."""
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat()
        retention = {"reason": "retention", "retention_days": older_than_days}
        total = 0
        while True:
            with self._db.session(exclusive=True) as db:
                doomed = db.all(
                    "SELECT id, decision_id FROM notes WHERE created_at < ? AND sealed IS NOT NULL "
                    "ORDER BY created_at, id LIMIT ?",
                    (cutoff, PURGE_BATCH),
                )
                if not doomed:
                    return total
                marks = ", ".join("?" for _ in doomed)
                db.all(
                    "UPDATE notes SET sealed = NULL, erased_at = ?, erased_by = 'retention' "
                    f"WHERE id IN ({marks})",
                    (_now(), *(row["id"] for row in doomed)),
                )
                erased = [
                    (row["decision_id"], {"note_id": row["id"], **retention}) for row in doomed
                ]
                self._append_many(db, "note.erased", erased)
            total += len(doomed)

    # -- reads ----------------------------------------------------------------------------

    def get(self, decision_id: str, *, reader: str | None = None) -> dict[str, Any] | None:
        """A decision with its events; under PHI mode `encrypt`, its input only for a `reader`,
        and its events' notes as `reveal_inputs` shows them (the read it chains is the last
        event)."""
        with self._db.session(exclusive=False) as db:
            item = _decision(db, decision_id)
            if item is not None:
                item["events"] = _events_of(db, [decision_id])[decision_id]
        if item is None:
            return None
        self.reveal_inputs([item], reader)
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
        self,
        items: list[dict[str, Any]],
        reader: str | None,
        *,
        id_key: str = "id",
        own_notes: bool = False,
    ) -> list[dict[str, Any]]:
        """PHI mode `encrypt` keeps inputs apart from the records. Each item (a record under
        `record`) whose input was kept gets it back for a `reader`, someone who may see inputs,
        with its answers' evidence (sealed beside it), and every such read is written to the hash
        chain. Anyone else gets `input_restricted`.

        Notes follow inputs (see "notes"): those of the item's `events` (`get`) and, with
        `own_notes`, the item's own (a rating's note has the rating's id). Each such event or
        item gets `note` (the text, or None) and `note_status` ("open", "sealed" or "erased");
        an event chained with its note in the payload, before notes left the chain, shows it only
        to a `reader` and otherwise has it blanked. A decision whose input or notes a `reader`
        opened gets one `input.read` (naming the notes), all chained with one append and added
        to the item's `events`; each item of such a decision gets `input_read: true`. Outside
        mode `off`, what a record holds itself (`_held_input`) is shown as a kept input is."""
        ids = list(dict.fromkeys(str(item[id_key]) for item in items))
        if not ids:
            return items
        refs = [
            ref["id"]
            for item in items
            for event in item.get("events") or ()
            if isinstance(ref := event["payload"].get("note_ref"), Mapping)
        ]
        own = [str(item["id"]) for item in items] if own_notes else []
        note_ids = list(dict.fromkeys([*refs, *own]))
        marks = ", ".join("?" for _ in ids)
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT decision_id, ciphertext, details FROM decision_inputs "
                f"WHERE decision_id IN ({marks})",
                tuple(ids),
            )
            notes = (
                db.all(
                    "SELECT id, decision_id, sealed FROM notes "
                    f"WHERE id IN ({', '.join('?' for _ in note_ids)})",
                    tuple(note_ids),
                )
                if note_ids
                else []
            )
        sealed = {row["decision_id"]: row["ciphertext"] for row in rows}
        readable = bool(reader) and self.phi.mode == "encrypt"
        opened = {k: self.phi.decrypt(k, v) for k, v in sealed.items()} if readable else {}
        details = {
            row["decision_id"]: self.phi.open_details(row["decision_id"], row["details"])
            for row in rows
            if row["decision_id"] in opened and row["details"]
        }
        # Who read what: inputs, then notes; outside mode off a note opens for a reader only.
        reads: dict[str, list[str]] = {decision_id: [] for decision_id in opened}
        gated = self.phi.mode != "off"
        views: dict[str, tuple[str, str | None]] = {}
        kept = {row["id"]: row for row in notes}
        for row in (kept[note_id] for note_id in note_ids if note_id in kept):  # chain order
            views[row["id"]] = view = self._note_view(row, reader)
            if gated and view[0] == "open":
                reads.setdefault(row["decision_id"], []).append(row["id"])
        # A record that holds input values itself (its input, kept under mode `off`, or its
        # answers' evidence, on any record written before 2026-10-02): outside `off`, only a
        # reader sees them (and the read is chained), as with a sealed input.
        legacy = {
            str(item[id_key])
            for item in items
            if gated and str(item[id_key]) not in opened and _held_input(item)
        }
        if gated and reader:
            for item in items:
                if _legacy_text(item, own_notes) or str(item[id_key]) in legacy:
                    reads.setdefault(str(item[id_key]), [])
        chained: list[dict[str, Any]] = []
        if reads:
            entries = [
                (decision_id, {"reader": reader, **({"notes": named} if named else {})})
                for decision_id, named in reads.items()
            ]
            with self._db.session(exclusive=True) as db:
                chained = self._append_many(db, "input.read", entries)
        for item in items:
            decision_id = str(item[id_key])
            if decision_id in reads:  # shown to a reader, and chained (`input.read`)
                item["input_read"] = True
            if decision_id in opened:
                item["record"]["input"] = opened[decision_id]
                item["input_restricted"] = False
                _give_evidence(item["record"], details.get(decision_id, {}).get("evidence", {}))
            elif decision_id in sealed or decision_id in legacy:
                # A sealed input not opened stays sealed; what the record holds itself is shown
                # to a reader (the read chained above) and to no one else.
                item["input_restricted"] = decision_id in sealed or not reader
                if decision_id in legacy and not reader:
                    _withhold(item["record"])
            for event in item.get("events") or ():
                _show_note(event, views, reader)
            if own_notes:
                _show_own_note(item, views, reader)
            if "events" in item:
                item["events"] += [
                    {k: v for k, v in event.items() if k != "decision_id"}
                    for event in chained
                    if event["decision_id"] == decision_id
                ]
        return items

    def withhold_inputs(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Lists show no input. Outside mode `off`, a record that holds input values itself (its
        input, or its answers' evidence) has them withheld: a reader opens one decision
        (`get`), and that read is chained."""
        if self.phi.mode == "off":
            return items
        for item in items:
            if _held_input(item):
                _withhold(item["record"])
                item["input_restricted"] = True
        return items

    def erase_input(self, decision_id: str, *, erased_by: str, reason: str) -> dict[str, Any]:
        """Erase what one decision keeps apart from the chain, on request (`mendari phi erase`):
        its sealed input and evidence are deleted (`input.erased`) and its notes erased (one
        `note.erased` each), all chained with who and why. Its record and the chain are
        untouched, so `audit verify` still passes. What the chain itself holds (an input kept
        under PHI mode `off`, evidence on a record written before 2026-10-02, a note chained
        before notes left the chain) nothing erases without breaking it: `still_in_chain` says
        so, and retiring that database removes it (docs/deploying-with-phi.md). Backups, the
        database's history and earlier exports still hold what was erased."""
        if reason not in ERASE_REASONS:
            raise NoteError(f"the reason is one of {ERASE_REASONS}")
        if not erased_by.strip() or len(erased_by) > 200 or not erased_by.isprintable():
            raise NoteError("who erases is a name or identifier of at most 200 characters")
        with self._db.session(exclusive=True) as db:
            item = _decision(db, decision_id)
            if item is None:
                raise NoteError(f"no decision {decision_id}")
            events = _events_of(db, [decision_id])[decision_id]
            in_chain = _held_input(item) or _legacy_text({"events": events}, own_notes=False)
            kept = db.one(
                "SELECT decision_id FROM decision_inputs WHERE decision_id = ?", (decision_id,)
            )
            notes = [
                row["id"]
                for row in db.all(
                    "SELECT id FROM notes WHERE decision_id = ? AND sealed IS NOT NULL "
                    "ORDER BY created_at, id",
                    (decision_id,),
                )
            ]
            if kept is None and not notes:
                if in_chain:
                    raise NoteError(
                        f"decision {decision_id} keeps nothing apart from the chain: its chained "
                        "record or events hold its input, evidence or note text, which only "
                        "retiring the database removes (docs/deploying-with-phi.md)"
                    )
                raise NoteError(f"decision {decision_id} keeps no input or note to erase")
            erased_at = _now()
            db.all("DELETE FROM decision_inputs WHERE decision_id = ?", (decision_id,))
            db.all(
                "UPDATE notes SET sealed = NULL, erased_at = ?, erased_by = ? "
                "WHERE decision_id = ? AND sealed IS NOT NULL",
                (erased_at, erased_by, decision_id),
            )
            who = {"reason": reason, "erased_by": erased_by}
            self._append_many(
                db, "note.erased", [(decision_id, {"note_id": note, **who}) for note in notes]
            )
            if kept is not None:
                self._append(db, decision_id, "input.erased", who)
        return {
            "decision_id": decision_id,
            "input": kept is not None,
            "notes": notes,
            **who,
            "erased_at": erased_at,
            "still_in_chain": in_chain,
        }

    def purge(
        self, older_than_days: int, *, source: Literal["schedule", "command"]
    ) -> dict[str, Any]:
        """Retention (`mendari phi purge`, the scheduled purge): delete kept inputs and erase
        notes older than N days (0: all of them), each deletion chained, then chain a summary
        (`retention.purged`, under `RETENTION_CHAIN`), which `last_purge` reads back."""
        inputs, notes = self.purge_inputs(older_than_days), self.purge_notes(older_than_days)
        summary = {
            "retention_days": older_than_days,
            "inputs": inputs,
            "notes": notes,
            "source": source,
        }
        with self._db.session(exclusive=True) as db:
            self._append(db, RETENTION_CHAIN, "retention.purged", summary)
        return summary

    def last_purge(self) -> dict[str, Any] | None:
        """The last purge's summary (`purge`), with when it ran; None if none ever did."""
        with self._db.session(exclusive=False) as db:
            row = db.one(
                "SELECT created_at, payload FROM audit_events WHERE decision_id = ? "
                "ORDER BY seq DESC LIMIT 1",
                (RETENTION_CHAIN,),
            )
        return None if row is None else {"at": row["created_at"], **json.loads(row["payload"])}

    def purge_inputs(self, older_than_days: int) -> int:
        """Retention: delete kept (encrypted) inputs older than N days (0: all of them), in
        batches of `PURGE_BATCH`, each chained with its `input.purged` events; the chain is
        untouched. Notes have their own purge (`purge_notes`); `purge` runs both."""
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat()
        purged = {"retention_days": older_than_days}
        total = 0
        while True:
            with self._db.session(exclusive=True) as db:
                doomed = [
                    row["decision_id"]
                    for row in db.all(
                        "SELECT decision_id FROM decision_inputs WHERE created_at < ? "
                        "ORDER BY created_at, decision_id LIMIT ?",
                        (cutoff, PURGE_BATCH),
                    )
                ]
                if not doomed:
                    return total
                marks = ", ".join("?" for _ in doomed)
                db.all(f"DELETE FROM decision_inputs WHERE decision_id IN ({marks})", doomed)
                self._append_many(db, "input.purged", [(d, purged) for d in doomed])
            total += len(doomed)

    # -- key rotation (LP-09) -------------------------------------------------------------
    # The PHI key: `rotate_phi` re-seals under the current key what is sealed under another
    # (kept inputs, their details, notes); `phi_check` proves, read-only, that everything opens
    # with the current key alone before the read keys are dropped. Chained records are never
    # touched. The input hash key needs no rotation: each hash names its key (`_mark_hash_epoch`).

    def rotate_phi(self, *, to: str, batch: int = ROTATE_BATCH) -> dict[str, Any]:
        """Re-seal under the current PHI key every kept input, its details and every note sealed
        under another key (`mendari phi rotate`). `to` is the current key's id, as the operator
        read it off `/api/health` (`phi_keys.current`): a run whose key is another refuses, and so
        does one to a key an earlier rotation retired. Values are read in pages by
        id, opened with the ring and re-sealed outside the chain lock, then written in a short
        exclusive session that changes a value only if it is still what was read
        (compare-and-set) and chains one `phi.rotated` with what it changed; the keys values moved
        off are marked retired in that session, so nothing seals with them here again. An
        interrupted run is run again: what is done stays done. A value no key opens is left as
        it is and listed in `unopenable` (a missing read key, or a value to erase)."""
        if self.phi.mode != "encrypt":
            raise MendariError("a PHI key rotation runs under MENDARI_PHI_MODE=encrypt")
        if batch < 1:
            raise MendariError("a batch is 1 value or more")
        current = key_id(self.phi.key)
        if to != current:
            raise MendariError(
                f"this run's MENDARI_PHI_KEY is key {current}, not {to}: run with the key the "
                "deployment seals with (its id is phi_keys.current in /api/health)"
            )
        if not self.protected:
            raise MendariError("this database was never kept under PHI mode hash or encrypt")
        with self._db.session(exclusive=True) as db:
            if db.one("SELECT name FROM settings WHERE name = ?", (RETIRED + to,)):
                raise MendariError(
                    f"key {to} was retired by an earlier rotation: make a new key and rotate to "
                    "it (docs/deploying-with-phi.md, Rotating the keys)"
                )
            db.all(
                "INSERT INTO settings (name, value) VALUES (?, ?) "
                "ON CONFLICT (name) DO UPDATE SET value = excluded.value",
                (ROTATION_TARGET, to),
            )
            _mark(db, PHI_TOKENS_V2, "1")
        self.phi = replace(self.phi, tag_tokens=True)
        summary: dict[str, Any] = {
            "to": to,
            "inputs": 0,
            "details": 0,
            "notes": 0,
            "batches": 0,
            "from": Counter(),
            "unopenable": [],
        }
        prefix = f"v2:{to}:"
        self._rotate_inputs(prefix, batch, summary)
        self._rotate_notes(prefix, batch, summary)
        return {**summary, "from": dict(summary["from"])}

    def _rotate_inputs(self, prefix: str, batch: int, summary: dict[str, Any]) -> None:
        after = ""
        while True:
            with self._db.session(exclusive=False) as db:
                rows = db.all(
                    "SELECT decision_id, ciphertext, details FROM decision_inputs "
                    "WHERE decision_id > ? AND (substr(ciphertext, 1, 20) <> ? "
                    "OR (details IS NOT NULL AND substr(details, 1, 20) <> ?)) "
                    "ORDER BY decision_id LIMIT ?",
                    (after, prefix, prefix, batch),
                )
            if not rows:
                return
            after = rows[-1]["decision_id"]
            changes = []
            for row in rows:
                decision_id = row["decision_id"]
                try:
                    value, opened = self.phi.open("input", row["ciphertext"], decision_id)
                    froms = [opened]
                    details = None
                    if row["details"] is not None:
                        evidence, by = self.phi.open("details", row["details"], decision_id)
                        details = self.phi.seal("details", evidence, decision_id)
                        froms.append(by)
                except MendariError:
                    summary["unopenable"].append(
                        {"kind": "input", "id": decision_id, "key": token_key(row["ciphertext"])}
                    )
                    continue
                cipher = self.phi.seal("input", value, decision_id)
                changes.append((row, cipher, details, froms))
            if changes:
                self._apply_inputs(changes, summary)

    def _apply_inputs(self, changes: list[Any], summary: dict[str, Any]) -> None:
        moved: Counter[str] = Counter()
        inputs = details = 0
        with self._db.session(exclusive=True) as db:
            for row, cipher, sealed_details, froms in changes:
                same = "details IS NULL" if row["details"] is None else "details = ?"
                old = () if row["details"] is None else (row["details"],)
                done = db.all(
                    "UPDATE decision_inputs SET ciphertext = ?, details = ? "
                    f"WHERE decision_id = ? AND ciphertext = ? AND {same} RETURNING decision_id",
                    (cipher, sealed_details, row["decision_id"], row["ciphertext"], *old),
                )
                if done:  # still what was read: neither erased, purged nor re-sealed since
                    inputs += 1
                    details += sealed_details is not None
                    moved.update(froms)
            if inputs:
                self._chain_rotation(db, moved, summary["to"], inputs=inputs, details=details)
        summary["inputs"] += inputs
        summary["details"] += details
        summary["from"].update(moved)
        summary["batches"] += bool(inputs)

    def _rotate_notes(self, prefix: str, batch: int, summary: dict[str, Any]) -> None:
        after = ""
        while True:
            with self._db.session(exclusive=False) as db:
                rows = db.all(
                    "SELECT id, decision_id, sealed FROM notes WHERE id > ? AND sealed IS NOT NULL "
                    "AND substr(sealed, 1, 1) <> '{' AND substr(sealed, 1, 20) <> ? "
                    "ORDER BY id LIMIT ?",
                    (after, prefix, batch),
                )
            if not rows:
                return
            after = rows[-1]["id"]
            changes = []
            for row in rows:
                try:
                    body, opened = self.phi.open(
                        "note", row["sealed"], row["id"], row["decision_id"]
                    )
                except MendariError:
                    summary["unopenable"].append(
                        {"kind": "note", "id": row["id"], "key": token_key(row["sealed"])}
                    )
                    continue
                sealed = self.phi.seal("note", body, row["id"], row["decision_id"])
                changes.append((row, sealed, opened))
            if not changes:
                continue
            moved: Counter[str] = Counter()
            notes = 0
            with self._db.session(exclusive=True) as db:
                for row, sealed, opened in changes:
                    done = db.all(
                        "UPDATE notes SET sealed = ? WHERE id = ? AND sealed = ? RETURNING id",
                        (sealed, row["id"], row["sealed"]),
                    )
                    if done:  # neither erased nor re-sealed since it was read
                        notes += 1
                        moved[opened] += 1
                if notes:
                    self._chain_rotation(db, moved, summary["to"], notes=notes)
            summary["notes"] += notes
            summary["from"].update(moved)
            summary["batches"] += bool(notes)

    def _chain_rotation(
        self,
        db: _Session,
        moved: Counter[str],
        to: str,
        *,
        inputs: int = 0,
        details: int = 0,
        notes: int = 0,
    ) -> None:
        """One batch's chained event (`phi.rotated`: key ids and counts, never a key), and the
        keys it moved values off marked retired."""
        for retired in moved:
            if retired != to:
                _mark(db, RETIRED + retired, _now())
        payload = {
            "to": to,
            "from": dict(sorted(moved.items())),
            "inputs": inputs,
            "details": details,
            "notes": notes,
        }
        self._append(db, KEYS_CHAIN, "phi.rotated", payload)

    def phi_check(self, *, batch: int = 500) -> dict[str, Any]:
        """Reads only (`mendari phi rotate --check`, on a store opened with `marks=False`): whether
        every sealed value opens with the current key alone, as dropping the read keys
        requires. Counts sealed values by the key each names (`legacy`: sealed before key
        ids), with the newest under each other key; opens every kept input, its details and
        every note with the current key only; recomputes the input hash of each record
        hashed under the current hash key, and checks each note against the commitment its
        event chained. Lists the input hash epochs (key id, first and last decision, how
        many). `ready` only when everything opened and matched."""
        alone = self.phi.current_only()
        current = key_id(self.phi.key) if self.phi.key else None
        hash_now = key_id(self.phi.hash_key) if self.phi.hash_key else None
        by_key: dict[str, Counter[str]] = {"inputs": Counter(), "notes": Counter()}
        newest: dict[str, str] = {}
        failures: list[dict[str, str]] = []

        def seen(kind: str, token: str, created_at: str) -> str:
            named = token_key(token)
            by_key[kind][named] += 1
            if named != current:
                newest[named] = max(newest.get(named, ""), str(created_at))
            return named

        after = ""
        while True:
            with self._db.session(exclusive=False) as db:
                rows = db.all(
                    "SELECT i.decision_id, i.created_at, i.ciphertext, i.details, d.record "
                    "FROM decision_inputs i LEFT JOIN decisions d ON d.id = i.decision_id "
                    "WHERE i.decision_id > ? ORDER BY i.decision_id LIMIT ?",
                    (after, batch),
                )
            if not rows:
                break
            after = rows[-1]["decision_id"]
            for row in rows:
                decision_id = row["decision_id"]
                named = seen("inputs", row["ciphertext"], row["created_at"])
                try:
                    value, _ = alone.open("input", row["ciphertext"], decision_id)
                    if row["details"] is not None:
                        seen("inputs", row["details"], row["created_at"])
                        alone.open("details", row["details"], decision_id)
                except MendariError:
                    failures.append({"kind": "input", "id": decision_id, "key": named})
                    continue
                hashed = str(json.loads(row["record"] or "{}").get("input_hash") or "")
                under_current = bool(hash_now) and hashed.startswith(f"hmac-sha256:{hash_now}:")
                if under_current and hashed != self.phi.input_hash(value):
                    failures.append({"kind": "input", "id": decision_id, "key": "input_hash"})
        with self._db.session(exclusive=False) as db:
            refs = {
                str(ref["id"]): str(ref.get("sha256"))
                for row in db.all(
                    "SELECT payload FROM audit_events WHERE payload LIKE ?", ('%"note_ref"%',)
                )
                if isinstance(ref := json.loads(row["payload"]).get("note_ref"), Mapping)
            }
        after = ""
        while True:
            with self._db.session(exclusive=False) as db:
                rows = db.all(
                    "SELECT id, decision_id, created_at, sealed FROM notes WHERE id > ? "
                    "AND sealed IS NOT NULL AND substr(sealed, 1, 1) <> '{' ORDER BY id LIMIT ?",
                    (after, batch),
                )
            if not rows:
                break
            after = rows[-1]["id"]
            for row in rows:
                named = seen("notes", row["sealed"], row["created_at"])
                try:
                    body, _ = alone.open("note", row["sealed"], row["id"], row["decision_id"])
                except MendariError:
                    failures.append({"kind": "note", "id": row["id"], "key": named})
                    continue
                salt = base64.b64decode(str(body.get("salt", "")))
                if (
                    row["id"] in refs
                    and _commitment(salt, str(body.get("text"))) != refs[row["id"]]
                ):
                    failures.append({"kind": "note", "id": row["id"], "key": "commitment"})
        with self._db.session(exclusive=False) as db:
            retired = {
                str(row["name"])[len(RETIRED) :]
                for row in db.all("SELECT name FROM settings WHERE name LIKE ?", (RETIRED + "%",))
            }
        read = sorted(self.phi.ring_ids - {current})
        return {
            "current": current,
            "read": read,
            # A read key no rotation moved values off is not retired: a deployment that still
            # seals with it (built before the swap) would go on doing so; delete those first.
            "read_not_retired": [key for key in read if key not in retired],
            "current_retired": current in retired,
            "by_key": {kind: dict(sorted(counts.items())) for kind, counts in by_key.items()},
            "newest_not_current": dict(sorted(newest.items())),
            "failures": len(failures),
            "failed": failures[:CHECK_FAILURES_SHOWN],
            "hash_epochs": self._hash_epochs(batch),
            "ready": not failures and current not in retired,
        }

    def _hash_epochs(self, batch: int) -> dict[str, dict[str, Any]]:
        """Each input hash key id the records name (`sha256`: unkeyed), with how many decisions
        and the first and last one's time."""
        epochs: dict[str, dict[str, Any]] = {}
        after = ""
        while True:
            with self._db.session(exclusive=False) as db:
                rows = db.all(
                    "SELECT id, created_at, record FROM decisions WHERE id > ? ORDER BY id LIMIT ?",
                    (after, batch),
                )
            if not rows:
                return epochs
            after = rows[-1]["id"]
            for row in rows:
                hashed = str(json.loads(row["record"]).get("input_hash") or "")
                named = hashed.split(":")[1] if hashed.startswith("hmac-sha256:") else "sha256"
                epoch = epochs.setdefault(named, {"decisions": 0, "first": None, "last": None})
                epoch["decisions"] += 1
                when = str(row["created_at"])
                epoch["first"] = min(filter(None, (epoch["first"], when)))
                epoch["last"] = max(filter(None, (epoch["last"], when)))

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
    # Rows of `SEALED_KINDS` are sealed under `secret` (by default the row's own value): a row
    # that does not open (another row's data, a changed byte, plaintext from before sealing) is
    # deleted and read as absent, and a take of one raises `SealError` (the row is gone too).

    def smart_put(
        self,
        key: str,
        kind: str,
        data: Mapping[str, Any],
        expires_at: int,
        *,
        secret: str | None = None,
    ) -> None:
        row = _session_key(kind, key)
        stored = _sealed(row, kind, key, data, secret)
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
                (row, kind, expires_at, _canonical(stored)),
            )

    def smart_get(self, key: str, kind: str, *, secret: str | None = None) -> dict[str, Any] | None:
        row = self.smart_read(key, kind, secret=secret)
        return None if row is None else row[0]

    def smart_read(
        self, key: str, kind: str, *, secret: str | None = None
    ) -> tuple[dict[str, Any], int] | None:
        """A live row's data and its `expires_at` (a session's idle expiry, server/sessions.py).
        A sealed row that does not open is deleted: None, as for no row."""
        row_key = _session_key(kind, key)
        opener = _opener(kind, key, secret)
        with self._db.session(exclusive=False) as db:
            row = db.one(
                "SELECT data, expires_at FROM smart_sessions WHERE key_sha256 = ? AND kind = ? "
                "AND expires_at > ?",
                (row_key, kind, int(time.time())),
            )
            if row is None:
                return None
            try:
                return _opened(row_key, kind, row["data"], opener), int(row["expires_at"])
            except SealError:
                db.all(
                    "DELETE FROM smart_sessions WHERE key_sha256 = ? AND kind = ?",
                    (row_key, kind),
                )
                return None

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

    def smart_replace(
        self,
        key: str,
        kind: str,
        data: Mapping[str, Any],
        expires_at: int,
        *,
        secret: str | None = None,
    ) -> bool:
        """Replace a live row's data and expiry (a session from an earlier release taking up the
        idle limit); an ended row stays ended. True if replaced."""
        row_key = _session_key(kind, key)
        stored = _sealed(row_key, kind, key, data, secret)
        with self._db.session(exclusive=False) as db:
            replaced = db.all(
                "UPDATE smart_sessions SET data = ?, expires_at = ? WHERE key_sha256 = ? "
                "AND kind = ? AND expires_at > ? RETURNING key_sha256",
                (_canonical(stored), expires_at, row_key, kind, int(time.time())),
            )
        return bool(replaced)

    def smart_take(
        self, key: str, kind: str, *, secret: str | None = None
    ) -> dict[str, Any] | None:
        """Read and delete in one statement: a launch's or a sign-in's state works once. None
        for no live row; `SealError` for a sealed row that does not open under `secret` (a
        launch's state brought back by another browser), which is deleted all the same."""
        row_key = _session_key(kind, key)
        opener = _opener(kind, key, secret)
        with self._db.session(exclusive=False) as db:
            row = db.one(
                "DELETE FROM smart_sessions WHERE key_sha256 = ? AND kind = ? "
                "RETURNING data, expires_at",
                (row_key, kind),
            )
        if row is None or row["expires_at"] <= int(time.time()):
            return None
        return _opened(row_key, kind, row["data"], opener)

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

    def acknowledge_notice(self, chain_id: str, *, version: str, actor: str) -> bool:
        """A person's acknowledgement of a notice version (`mendari.notices`), chained once under
        `chain_id` (`notice:<version>:<principal id>`). False: already acknowledged."""
        if self.notice_acknowledged(chain_id):  # no chain lock for an answer already given
            return False
        with self._db.session(exclusive=True) as db:
            if db.one(
                "SELECT seq FROM audit_events WHERE decision_id = ? AND event = ?",
                (chain_id, NOTICE_EVENT),
            ):
                return False
            payload = {"version": version, "actor": actor, "org": tenancy.current()}
            self._append(db, chain_id, "notice.acknowledged", payload)
        return True

    def notice_acknowledged(self, chain_id: str) -> bool:
        with self._db.session(exclusive=False) as db:
            return (
                db.one(
                    "SELECT seq FROM audit_events WHERE decision_id = ? AND event = ?",
                    (chain_id, NOTICE_EVENT),
                )
                is not None
            )

    def hold_automation(
        self,
        *,
        org: str | None,
        pack_id: str | None,
        pack_digest: str | None,
        reason: str,
        set_by: str,
    ) -> dict[str, Any]:
        """Stop automation (AUTO becomes HUMAN_REVIEW) for every decision a hold covers: an
        organization (None: every one), a pack (None: every pack) and a digest (None: every
        version). Chained as `automation.held`; it lasts until lifted."""
        reason = " ".join(reason.split())
        if not reason or len(reason) > 200:
            raise ReviewError("a hold needs a reason of at most 200 characters")
        if pack_digest is not None and pack_id is None:
            raise ReviewError("a hold on a digest names its pack")
        hold = {
            "id": secrets.token_hex(8),
            "org": org,
            "pack_id": pack_id,
            "pack_digest": pack_digest,
            "reason": reason,
            "set_by": set_by,
            "set_at": _now(),
        }
        with self._db.session(exclusive=True) as db:
            db.all(
                "INSERT INTO automation_holds (id, org, pack_id, pack_digest, reason, set_by, "
                "set_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                tuple(hold.values()),
            )
            self._append(db, f"hold:{hold['id']}", "automation.held", hold)
        return hold

    def lift_hold(self, hold_id: str, *, lifted_by: str, org: str | None) -> bool:
        """Lift an active hold; `org` limits it to that organization's own holds (None: any,
        the command line's). False: none such."""
        scope = "" if org is None else " AND org = ?"
        with self._db.session(exclusive=True) as db:
            row = db.one(
                f"SELECT id FROM automation_holds WHERE id = ? AND lifted_at IS NULL{scope}",
                (hold_id,) if org is None else (hold_id, org),
            )
            if row is None:
                return False
            lifted = {"id": hold_id, "lifted_by": lifted_by, "lifted_at": _now()}
            db.all(
                "UPDATE automation_holds SET lifted_by = ?, lifted_at = ? WHERE id = ?",
                (lifted["lifted_by"], lifted["lifted_at"], hold_id),
            )
            self._append(db, f"hold:{hold_id}", "automation.lifted", lifted)
        return True

    def holds(self, org: str | None = None, *, everyone: bool = False) -> list[dict[str, Any]]:
        """Active holds: `org`'s and the deployment's (org NULL), or every one (`everyone`)."""
        query = "SELECT * FROM automation_holds WHERE lifted_at IS NULL"
        params: tuple[Any, ...] = ()
        if not everyone:
            query += " AND (org IS NULL OR org = ?)"
            params = (org,)
        with self._db.session(exclusive=False) as db:
            return [dict(row) for row in db.all(query + " ORDER BY set_at, id", params)]

    def hold_for(self, pack_id: str, pack_digest: str, org: str) -> dict[str, Any] | None:
        """The first active hold that covers a decision of this pack, digest and organization."""
        with self._db.session(exclusive=False) as db:
            row = db.one(
                "SELECT * FROM automation_holds WHERE lifted_at IS NULL "
                "AND (org IS NULL OR org = ?) AND (pack_id IS NULL OR pack_id = ?) "
                "AND (pack_digest IS NULL OR pack_digest = ?) ORDER BY set_at, id LIMIT 1",
                (org, pack_id, pack_digest),
            )
        return None if row is None else dict(row)

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
        with self._db.session(exclusive=True) as db:
            return self._insert_key(
                db, name=name, role=role, org=org, created_by=created_by, scopes=scopes, days=days
            )

    def _insert_key(
        self,
        db: _Session,
        *,
        name: str,
        role: str,
        org: str,
        created_by: str,
        scopes: str | None,
        days: int,
        replaces: str | None = None,
    ) -> dict[str, Any]:
        """Store a new key (hashed) and chain `key.created` (with `replaces` for a rotation's);
        the key with its secret, which is nowhere else."""
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
            **({"replaces": replaces} if replaces else {}),
        }
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

    def rotate_api_key(
        self,
        key_id: str,
        org: str | None = None,
        *,
        rotated_by: str,
        overlap_hours: int = KEY_OVERLAP_HOURS,
        days: int = KEY_DAYS,
        check_scope: Callable[[str | None, str], str | None] = lambda scope, role: scope,
    ) -> dict[str, Any]:
        """Replace a key of `org` (any, if None) with a new one of the same name, role,
        organization and scope (`check_scope` re-checks the scope for the role, as creating a
        key does; it raises to refuse). The old key keeps working for `overlap_hours` (0 to 168,
        never past its own expiry nor longer than the new key lives), then is refused as an
        expired key is. Chained: the new key's `key.created` (with `replaces`), then
        `key.rotated` on the old one. The new key with its secret, and what it replaces.

        Refused: an unknown key or another organization's (404); a revoked or expired one, one
        of a role no key holds now, or one already replaced (409), unless its replacement was
        revoked (the answer carrying it may have been lost)."""
        if not 0 <= overlap_hours <= KEY_MAX_OVERLAP_HOURS:
            raise ValueError(f"the overlap is 0 to {KEY_MAX_OVERLAP_HOURS} hours")
        if not 1 <= days <= KEY_MAX_DAYS:
            raise ValueError(f"a key lives 1 to {KEY_MAX_DAYS} days")
        if overlap_hours > days * 24:
            raise ValueError("the overlap is longer than the new key lives")
        now = _now()
        with self._db.session(exclusive=True) as db:
            row = db.one(
                "SELECT id, name, role, org, scopes, expires_at, revoked_at, replaced_by "
                "FROM api_keys WHERE id = ?",
                (key_id,),
            )
            if row is None or (org is not None and row["org"] != org):
                raise AuthError("no such key", 404)
            if row["revoked_at"] is not None:
                raise AuthError("this key is revoked: create a new one", 409)
            if not row["expires_at"] or row["expires_at"] <= now:
                raise AuthError("this key has expired: create a new one", 409)
            if row["role"] not in MACHINE_ROLES:
                raise AuthError(
                    f"this key's role, {row['role']}, is not one a key holds now: create a new one",
                    409,
                )
            if row["replaced_by"]:
                replacement = db.one(
                    "SELECT revoked_at FROM api_keys WHERE id = ?", (row["replaced_by"],)
                )
                if replacement is None or replacement["revoked_at"] is None:
                    raise AuthError(f"this key was already replaced by {row['replaced_by']}", 409)
            scope = check_scope(row["scopes"], row["role"])
            new = self._insert_key(
                db,
                name=row["name"],
                role=row["role"],
                org=row["org"],
                created_by=rotated_by,
                scopes=scope,
                days=days,
                replaces=key_id,
            )
            until = min(str(row["expires_at"]), _later(timedelta(hours=overlap_hours)))
            db.all(
                "UPDATE api_keys SET expires_at = ?, replaced_by = ? WHERE id = ?",
                (until, new["id"], key_id),
            )
            rotated = {
                "id": key_id,
                "replaced_by": new["id"],
                "rotated_by": rotated_by,
                "expires_at": until,
                "expires_at_before": row["expires_at"],
                "overlap_hours": overlap_hours,
            }
            self._append(db, f"key:{key_id}", "key.rotated", rotated)
        return {**new, "replaces": {"id": key_id, "expires_at": until}}

    def api_keys(self, org: str | None = None) -> list[dict[str, Any]]:
        """Every key (of `org`, if given), oldest first; never the secret or its hash."""
        # None is every organization (open mode, the command line); "" is an organization.
        where, params = ("WHERE org = ?", (org,)) if org is not None else ("", ())
        with self._db.session(exclusive=False) as db:
            return db.all(
                "SELECT id, name, role, org, scopes AS scope, created_by, created_at, "
                "expires_at, revoked_at, revoked_by, replaced_by "
                f"FROM api_keys {where} ORDER BY created_at",
                params,
            )

    def api_key(self, secret: str) -> dict[str, Any] | None:
        """The key a secret belongs to, unless it was revoked or has expired (one query; a key
        without an expiry is refused too)."""
        with self._db.session(exclusive=False) as db:
            return db.one(
                "SELECT id, name, role, org, scopes AS scope, expires_at, replaced_by "
                "FROM api_keys WHERE key_sha256 = ? AND revoked_at IS NULL AND expires_at > ?",
                (_key(secret), _now()),
            )

    def revoke_api_key(self, key_id: str, org: str | None = None, *, revoked_by: str) -> bool:
        """Revoke a key of `org` (any, if None); False for an unknown or already revoked one."""
        with self._db.session(exclusive=True) as db:
            row = db.one("SELECT org FROM api_keys WHERE id = ? AND revoked_at IS NULL", (key_id,))
            if row is None or (org is not None and row["org"] != org):
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
        # None is every organization (open mode, the command line); "" is an organization.
        where, params = ("WHERE org = ?", (org,)) if org is not None else ("", ())
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
            if row is None or (org is not None and row["org"] != org):
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

    # -- people, organizations and memberships (LP-12, ADR-0008) --------------------------------
    # Memberships mode reads these; claims mode never does. Each write chains an event of ids and
    # digests only (an e-mail address as `accounts.email_digest`, an organization's name as a
    # digest). The command line (`mendari access`) and, from LP-12e on, the server write them.

    def record_user(
        self, *, issuer: str, subject: str, email: str | None, name: str | None
    ) -> dict[str, Any]:
        """The person signing in, recorded the first time (`user.recorded`) and kept current:
        `email` only when the identity provider verified it. Only a first record takes the
        chain's lock; keeping one current does not."""
        user_id = accounts.person_id(issuer, subject)
        try:  # one no invitation can name (not ASCII) is kept as none: they still sign in
            address = accounts.normalise_email(email) if email else None
        except ValueError:
            address = None
        now = _now()
        with self._db.session(exclusive=False) as db:
            kept = db.all(
                "UPDATE users SET email = ?, name = ?, last_sign_in_at = ? WHERE id = ? "
                "RETURNING *",
                (address, name, now, user_id),
            )
        if kept:
            return dict(kept[0])
        with self._db.session(exclusive=True) as db:
            row = db.one("SELECT id FROM users WHERE id = ?", (user_id,))
            if row is None:
                db.all(
                    "INSERT INTO users (id, issuer, subject, email, name, created_at, "
                    "last_sign_in_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (user_id, issuer, subject, address, name, now, now),
                )
                self._append(db, user_id, "user.recorded", {"user": user_id})
            else:
                db.all(
                    "UPDATE users SET email = ?, name = ?, last_sign_in_at = ? WHERE id = ?",
                    (address, name, now, user_id),
                )
            user = db.one("SELECT * FROM users WHERE id = ?", (user_id,))
        return dict(user) if user is not None else {}

    def user(self, user_id: str) -> dict[str, Any] | None:
        with self._db.session(exclusive=False) as db:
            row = db.one("SELECT * FROM users WHERE id = ?", (user_id,))
        return None if row is None else dict(row)

    def create_organization(self, *, org: str, name: str, created_by: str) -> dict[str, Any]:
        """A new organization's record (`organization.created`). `default` is the owner's,
        created by `bootstrap` only."""
        accounts.check_org_id(org)
        if org == accounts.RESERVED_ORG:
            raise ValueError(
                f"{org!r} is reserved for the owner's data: `mendari access bootstrap`"
            )
        with self._db.session(exclusive=True) as db:
            return self._insert_organization(db, org=org, name=name, created_by=created_by)

    def _insert_organization(
        self, db: _Session, *, org: str, name: str, created_by: str
    ) -> dict[str, Any]:
        name = name.strip()
        if not name or len(name) > 200:
            raise ValueError("an organization's name is 1 to 200 characters")
        if db.one("SELECT id FROM organizations WHERE id = ?", (org,)) is not None:
            raise ValueError(f"organization {org!r} already exists")
        created = {"id": org, "name": name, "created_by": created_by, "created_at": _now()}
        db.all(
            "INSERT INTO organizations (id, name, created_by, created_at) VALUES (?, ?, ?, ?)",
            (org, name, created_by, created["created_at"]),
        )
        self._append(
            db,
            f"org:{org}",
            "organization.created",
            {
                "id": org,
                "org": org,
                "name_digest": accounts.name_digest(name),
                "created_by": created_by,
            },
        )
        return created

    def organizations(self) -> list[dict[str, Any]]:
        """Every organization's record; none in a database from before them (read-only)."""
        with self._db.session(exclusive=False) as db:
            if not db.columns("organizations"):
                return []
            return [dict(r) for r in db.all("SELECT * FROM organizations ORDER BY id")]

    def organization(self, org: str) -> dict[str, Any] | None:
        with self._db.session(exclusive=False) as db:
            row = db.one("SELECT * FROM organizations WHERE id = ?", (org,))
        return None if row is None else dict(row)

    def memberships(
        self, *, org: str | None = None, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        """The memberships in force (of `org`, or `user_id`'s), oldest first, roles as lists."""
        where, params = ["removed_at IS NULL"], []
        if org is not None:
            where.append("org = ?")
            params.append(org)
        if user_id is not None:
            where.append("user_id = ?")
            params.append(user_id)
        with self._db.session(exclusive=False) as db:
            if not db.columns("memberships"):  # a database from before them, read as it is
                return []
            rows = db.all(
                f"SELECT * FROM memberships WHERE {' AND '.join(where)} ORDER BY created_at, id",
                params,
            )
        return [_membership(r) for r in rows]

    def active_membership(self, user_id: str, org: str) -> dict[str, Any] | None:
        found = self.memberships(org=org, user_id=user_id)
        return found[0] if found else None

    def membership_in_force(self, user_id: str, org: str) -> dict[str, Any] | None:
        """`user_id`'s membership of `org` in force, as it is now (one indexed read, nothing
        cached): what a memberships-mode request acts with (server/auth.py)."""
        with self._db.session(exclusive=False) as db:
            row = db.one(
                "SELECT * FROM memberships WHERE org = ? AND user_id = ? AND removed_at IS NULL",
                (org, user_id),
            )
        return None if row is None else _membership(row)

    def member_of(self, user_id: str) -> list[dict[str, Any]]:
        """The organizations `user_id` holds a membership of in force, each with its name and
        the roles held: theirs only, never anyone else's or every organization's."""
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT m.org, o.name, m.roles FROM memberships m "
                "LEFT JOIN organizations o ON o.id = m.org "
                "WHERE m.user_id = ? AND m.removed_at IS NULL ORDER BY m.org",
                (user_id,),
            )
        return [
            {"org": r["org"], "name": r["name"] or r["org"], "roles": json.loads(r["roles"])}
            for r in rows
        ]

    def pending_for(self, email: str) -> bool:
        """Whether a membership waits for whoever signs in with `email` verified."""
        address = accounts.normalise_email(email)
        with self._db.session(exclusive=False) as db:
            return (
                db.one(
                    "SELECT id FROM memberships WHERE pending_email = ? AND user_id IS NULL "
                    "AND removed_at IS NULL",
                    (address,),
                )
                is not None
            )

    def add_membership(
        self,
        *,
        org: str,
        roles: Sequence[str],
        created_by: str,
        user_id: str | None = None,
        email: str | None = None,
    ) -> dict[str, Any]:
        """A membership of `org` for a recorded person (`user_id`) or, pending until they sign
        in with it verified, for an e-mail address (`membership.created`)."""
        with self._db.session(exclusive=True) as db:
            return self._insert_membership(
                db, org=org, roles=roles, created_by=created_by, user_id=user_id, email=email
            )

    def _insert_membership(
        self,
        db: _Session,
        *,
        org: str,
        roles: Sequence[str],
        created_by: str,
        user_id: str | None = None,
        email: str | None = None,
    ) -> dict[str, Any]:
        if (user_id is None) == (email is None):
            raise ValueError("a membership is a person's or a pending e-mail address's")
        chosen = accounts.check_roles(roles)
        if db.one("SELECT id FROM organizations WHERE id = ?", (org,)) is None:
            raise ValueError(f"no organization {org!r}")
        address = None if email is None else accounts.normalise_email(email)
        if user_id is not None:
            if db.one("SELECT id FROM users WHERE id = ?", (user_id,)) is None:
                raise ValueError("no such person")
            taken = db.one(
                "SELECT id FROM memberships WHERE org = ? AND user_id = ? AND removed_at IS NULL",
                (org, user_id),
            )
        else:
            taken = db.one(
                "SELECT id FROM memberships WHERE org = ? AND pending_email = ? "
                "AND user_id IS NULL AND removed_at IS NULL",
                (org, address),
            )
        if taken is not None:
            raise ValueError(f"already a member of {org!r}")
        membership = {
            "id": secrets.token_hex(8),
            "org": org,
            "user_id": user_id,
            "pending_email": address,
            "roles": chosen,
            "created_by": created_by,
            "created_at": _now(),
        }
        db.all(
            "INSERT INTO memberships (id, org, user_id, pending_email, roles, created_by, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                membership["id"],
                org,
                user_id,
                address,
                json.dumps(chosen),
                created_by,
                membership["created_at"],
            ),
        )
        who = (
            {"user": user_id}
            if user_id
            else {"email_digest": accounts.email_digest(self.phi, address or "")}
        )
        self._append(
            db,
            f"membership:{membership['id']}",
            "membership.created",
            {"id": membership["id"], "org": org, **who, "roles": chosen, "created_by": created_by},
        )
        return membership

    def set_membership_roles(
        self, membership_id: str, roles: Sequence[str], *, org: str, changed_by: str
    ) -> dict[str, Any]:
        """Change a membership of `org`'s roles (`membership.roles_changed`); never leaving
        `org` without an administrator."""
        chosen = accounts.check_roles(roles)
        with self._db.session(exclusive=True) as db:
            row = self._membership_row(db, membership_id, org)
            previous = json.loads(row["roles"])
            if accounts.ADMIN in previous and accounts.ADMIN not in chosen:
                self._keep_an_admin(db, org, row)
            db.all(
                "UPDATE memberships SET roles = ? WHERE id = ?", (json.dumps(chosen), membership_id)
            )
            self._append(
                db,
                f"membership:{membership_id}",
                "membership.roles_changed",
                {"id": membership_id, "org": org, "roles": chosen, "previous": previous,
                 "changed_by": changed_by},
            )  # fmt: skip
            changed = db.one("SELECT * FROM memberships WHERE id = ?", (membership_id,))
        return _membership(changed)

    def remove_membership(self, membership_id: str, *, org: str, removed_by: str) -> bool:
        """End a membership of `org` (`membership.removed`); never its last administrator's.
        False for one not in force."""
        with self._db.session(exclusive=True) as db:
            try:
                row = self._membership_row(db, membership_id, org)
            except ValueError:
                return False
            if accounts.ADMIN in json.loads(row["roles"]):
                self._keep_an_admin(db, org, row)
            now = _now()
            db.all(
                "UPDATE memberships SET removed_at = ?, removed_by = ? WHERE id = ?",
                (now, removed_by, membership_id),
            )
            self._append(
                db,
                f"membership:{membership_id}",
                "membership.removed",
                {"id": membership_id, "org": org, "removed_by": removed_by},
            )
        return True

    def _membership_row(self, db: _Session, membership_id: str, org: str) -> Any:
        row = db.one(
            "SELECT * FROM memberships WHERE id = ? AND org = ? AND removed_at IS NULL",
            (membership_id, org),
        )
        if row is None:
            raise ValueError("no such membership")
        return row

    def _keep_an_admin(self, db: _Session, org: str, leaving: Any) -> None:
        """Refuse what would leave `org` without an administrator who can act: one bound to a
        person who has signed in. A pending membership (`user_id` NULL) counts for nothing,
        either way: removing or demoting one never lowers how many can act."""
        if leaving["user_id"] is None:
            return
        others = db.all(
            "SELECT roles FROM memberships WHERE org = ? AND id <> ? AND removed_at IS NULL "
            "AND user_id IS NOT NULL",
            (org, leaving["id"]),
        )
        if not any(accounts.ADMIN in json.loads(r["roles"]) for r in others):
            raise ValueError(f"{org!r} would be left without an administrator")

    def bind_pending(self, user_id: str, email: str) -> list[dict[str, Any]]:
        """The pending memberships for `email` (verified by the identity provider at this
        sign-in) made `user_id`'s (`membership.bound`); one where they are already a member
        stays pending."""
        address = accounts.normalise_email(email)
        bound: list[dict[str, Any]] = []
        if not self.pending_for(address):  # most sign-ins: no lock taken
            return bound
        with self._db.session(exclusive=True) as db:
            pending = db.all(
                "SELECT * FROM memberships WHERE pending_email = ? AND user_id IS NULL "
                "AND removed_at IS NULL ORDER BY created_at, id",
                (address,),
            )
            for row in pending:
                if db.one(
                    "SELECT id FROM memberships WHERE org = ? AND user_id = ? "
                    "AND removed_at IS NULL",
                    (row["org"], user_id),
                ):
                    continue
                db.all(
                    "UPDATE memberships SET user_id = ?, pending_email = NULL WHERE id = ?",
                    (user_id, row["id"]),
                )
                self._append(
                    db,
                    f"membership:{row['id']}",
                    "membership.bound",
                    {"id": row["id"], "org": row["org"], "user": user_id},
                )
                bound.append(
                    _membership(db.one("SELECT * FROM memberships WHERE id = ?", (row["id"],)))
                )
        return bound

    def create_invitation(
        self,
        *,
        org: str,
        email: str,
        roles: Sequence[str],
        created_by: str,
        days: int = accounts.INVITE_DAYS,
    ) -> dict[str, Any]:
        """A single-use invitation to `org` for whoever signs in with `email` verified, valid
        `days` (`invitation.created`). Its token is in the result and nowhere else: Mendari
        sends no e-mail, the inviting admin passes the link on."""
        chosen = accounts.check_roles(roles)
        address = accounts.normalise_email(email)
        if not 1 <= days <= accounts.INVITE_MAX_DAYS:
            raise ValueError(f"an invitation lasts 1 to {accounts.INVITE_MAX_DAYS} days")
        token = accounts.TOKEN_PREFIX + secrets.token_urlsafe(32)
        invitation = {
            "id": secrets.token_hex(8),
            "org": org,
            "email": address,
            "roles": chosen,
            "created_by": created_by,
            "created_at": _now(),
            "expires_at": _later(timedelta(days=days)),
        }
        with self._db.session(exclusive=True) as db:
            if db.one("SELECT id FROM organizations WHERE id = ?", (org,)) is None:
                raise ValueError(f"no organization {org!r}")
            db.all(
                "INSERT INTO invitations (id, token_sha256, org, email, roles, created_by, "
                "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    invitation["id"],
                    _key(token),
                    org,
                    address,
                    json.dumps(chosen),
                    created_by,
                    invitation["created_at"],
                    invitation["expires_at"],
                ),
            )
            self._append(
                db,
                f"invitation:{invitation['id']}",
                "invitation.created",
                {"id": invitation["id"], "org": org, "roles": chosen,
                 "email_digest": accounts.email_digest(self.phi, address),
                 "created_by": created_by, "expires_at": invitation["expires_at"]},
            )  # fmt: skip
        return {**invitation, "token": token}

    def invitations(self, org: str) -> list[dict[str, Any]]:
        """`org`'s invitations, newest first; never their tokens."""
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT id, org, email, roles, created_by, created_at, expires_at, accepted_at, "
                "accepted_by, revoked_at, revoked_by FROM invitations WHERE org = ? "
                "ORDER BY created_at DESC, id",
                (org,),
            )
        return [{**dict(r), "roles": json.loads(r["roles"])} for r in rows]

    def revoke_invitation(self, invitation_id: str, *, org: str, revoked_by: str) -> bool:
        """Revoke an open invitation of `org` (`invitation.revoked`); False otherwise."""
        with self._db.session(exclusive=True) as db:
            row = db.one(
                "SELECT id FROM invitations WHERE id = ? AND org = ? AND accepted_at IS NULL "
                "AND revoked_at IS NULL",
                (invitation_id, org),
            )
            if row is None:
                return False
            db.all(
                "UPDATE invitations SET revoked_at = ?, revoked_by = ? WHERE id = ?",
                (_now(), revoked_by, invitation_id),
            )
            self._append(
                db,
                f"invitation:{invitation_id}",
                "invitation.revoked",
                {"id": invitation_id, "org": org, "revoked_by": revoked_by},
            )
        return True

    def accept_invitation(
        self, token: str, *, user_id: str, verified_email: str | None
    ) -> dict[str, Any]:
        """Accept an invitation as the signed-in `user_id`, whose identity provider verified
        `verified_email`: once, before it expires, and only for the address it was made for
        (`invitation.accepted`, then `membership.created` unless already a member). Unknown,
        used, revoked and expired are one answer; another address keeps it open."""
        if not verified_email:
            raise AuthError("sign in with the e-mail address the invitation is for, verified", 403)
        address = accounts.normalise_email(verified_email)
        now = _now()
        with self._db.session(exclusive=True) as db:
            row = db.one(
                "SELECT * FROM invitations WHERE token_sha256 = ? AND accepted_at IS NULL "
                "AND revoked_at IS NULL AND expires_at > ?",
                (_key(token), now),
            )
            if row is None:
                raise AuthError("this invitation is not valid: ask for a new one", 404)
            if row["email"] != address:
                raise AuthError("this invitation is for another e-mail address", 403)
            db.all(
                "UPDATE invitations SET accepted_at = ?, accepted_by = ? WHERE id = ?",
                (now, user_id, row["id"]),
            )
            self._append(
                db,
                f"invitation:{row['id']}",
                "invitation.accepted",
                {"id": row["id"], "org": row["org"], "user": user_id},
            )
            member = db.one(
                "SELECT * FROM memberships WHERE org = ? AND user_id = ? AND removed_at IS NULL",
                (row["org"], user_id),
            )
            if member is not None:
                return _membership(member)
            return self._insert_membership(
                db,
                org=row["org"],
                roles=json.loads(row["roles"]),
                created_by=f"invitation:{row['id']}",
                user_id=user_id,
            )

    def request_access(self, *, user_id: str, org: str) -> dict[str, Any]:
        """A person asking to join `org` (`access.requested`); their open request again if they
        asked before. `accounts.AccessRefused` (a `ValueError`) for an organization with no
        record, one they are a member of, or a request past `accounts.OPEN_REQUESTS_MAX` open
        ones. What is refused is
        refused before the chain's lock is taken: asking costs everyone else nothing."""
        accounts.check_org_id(org)
        with self._db.session(exclusive=False) as db:
            found = self._open_request(db, user_id, org)
        if found is not None:
            return found
        with self._db.session(exclusive=True) as db:
            found = self._open_request(db, user_id, org)  # again, under the lock
            if found is not None:
                return found
            request = {
                "id": secrets.token_hex(8),
                "user_id": user_id,
                "org": org,
                "created_at": _now(),
            }
            db.all(
                "INSERT INTO access_requests (id, user_id, org, created_at) VALUES (?, ?, ?, ?)",
                (request["id"], user_id, org, request["created_at"]),
            )
            self._append(
                db,
                f"access:{request['id']}",
                "access.requested",
                {"id": request["id"], "user": user_id, "org": org},
            )
        return request

    def _open_request(self, db: _Session, user_id: str, org: str) -> dict[str, Any] | None:
        """`user_id`'s open request to `org`, if any; a `ValueError` when one may not be made."""
        open_one = db.one(
            "SELECT * FROM access_requests WHERE user_id = ? AND org = ? AND handled_at IS NULL",
            (user_id, org),
        )
        if open_one is not None:
            return dict(open_one)
        if db.one("SELECT id FROM organizations WHERE id = ?", (org,)) is None:
            raise accounts.AccessRefused(f"no organization {org!r}", "unknown_organization")
        if db.one(
            "SELECT id FROM memberships WHERE org = ? AND user_id = ? AND removed_at IS NULL",
            (org, user_id),
        ):
            raise accounts.AccessRefused(f"already a member of {org!r}", "member")
        counted = db.one(
            "SELECT COUNT(*) AS n FROM access_requests WHERE user_id = ? AND handled_at IS NULL",
            (user_id,),
        )
        if counted is not None and int(counted["n"]) >= accounts.OPEN_REQUESTS_MAX:
            raise accounts.AccessRefused(
                f"at most {accounts.OPEN_REQUESTS_MAX} open requests at once", "open_requests"
            )
        return None

    def access_requests(self, org: str | None = None) -> list[dict[str, Any]]:
        """The open requests (to `org`, if given), oldest first."""
        where, params = ("AND org = ?", (org,)) if org is not None else ("", ())
        with self._db.session(exclusive=False) as db:
            if not db.columns("access_requests"):  # a database from before them
                return []
            rows = db.all(
                f"SELECT * FROM access_requests WHERE handled_at IS NULL {where} "
                "ORDER BY created_at, id",
                params,
            )
        return [dict(r) for r in rows]

    def handle_access_request(
        self,
        request_id: str,
        *,
        org: str,
        outcome: Literal["granted", "refused"],
        handled_by: str,
        roles: Sequence[str] = ("viewer",),
    ) -> dict[str, Any] | None:
        """Grant (a membership with `roles`) or refuse an open request to `org`
        (`access.handled`); None for one not open."""
        if outcome not in ("granted", "refused"):
            raise ValueError("an access request is granted or refused")
        with self._db.session(exclusive=True) as db:
            row = db.one(
                "SELECT * FROM access_requests WHERE id = ? AND org = ? AND handled_at IS NULL",
                (request_id, org),
            )
            if row is None:
                return None
            membership = None
            if outcome == "granted":
                membership = self._insert_membership(
                    db, org=org, roles=roles, created_by=handled_by, user_id=row["user_id"]
                )
            db.all(
                "UPDATE access_requests SET handled_at = ?, handled_by = ?, outcome = ? "
                "WHERE id = ?",
                (_now(), handled_by, outcome, request_id),
            )
            self._append(
                db,
                f"access:{request_id}",
                "access.handled",
                {"id": request_id, "org": org, "outcome": outcome, "handled_by": handled_by},
            )
        return {"id": request_id, "outcome": outcome, "membership": membership}

    def bootstrap(self, *, email: str, created_by: str) -> dict[str, Any]:
        """The owner in their existing data: `default`'s record if it has none, and an
        administrator's membership of it pending for `email`, made the owner's at their next
        sign-in with that address verified (`bind_pending`). It never picks a stored person by
        their address (an address stored from an earlier sign-in proves nothing now).
        Idempotent: nothing again while that pending membership, or an administrator's of
        `default` held by a person who signed in with that address, is in force. Adds records
        and rewrites none."""
        address = accounts.normalise_email(email)
        done: dict[str, Any] = {"organization": None, "membership": None}
        with self._db.session(exclusive=True) as db:
            org = accounts.RESERVED_ORG
            if db.one("SELECT id FROM organizations WHERE id = ?", (org,)) is None:
                done["organization"] = self._insert_organization(
                    db, org=org, name="The owner's organization", created_by=created_by
                )
            pending = db.one(
                "SELECT id FROM memberships WHERE org = ? AND pending_email = ? "
                "AND user_id IS NULL AND removed_at IS NULL",
                (org, address),
            )
            bound = db.all(
                "SELECT m.roles FROM memberships m JOIN users u ON u.id = m.user_id "
                "WHERE m.org = ? AND u.email = ? AND m.removed_at IS NULL",
                (org, address),
            )
            if pending is None and not any(accounts.ADMIN in json.loads(r["roles"]) for r in bound):
                done["membership"] = self._insert_membership(
                    db, org=org, roles=[accounts.ADMIN], created_by=created_by, email=address
                )
        return done

    def organizations_in_use(self) -> list[dict[str, Any]]:
        """The preflight before memberships mode (`mendari access check`): each organization
        that keys, clients, approvals, holds, decisions or evaluation reports name (a NULL is
        `default`), with how many of each, whether it has a record, and whether its id can
        have one (`recordable`). Counts and ids only."""
        default = f"COALESCE(org, '{tenancy.DEFAULT_ORG}')"
        tables = (  # (table, its rows' organization, the live rows)
            ("api_keys", "org", "revoked_at IS NULL"),
            ("oauth_clients", "org", "revoked_at IS NULL"),
            ("governance_approvals", "org", "revoked_at IS NULL"),
            ("automation_holds", "org", "lifted_at IS NULL"),
            ("decisions", default, "1 = 1"),
            ("eval_reports", default, "1 = 1"),
        )
        found: dict[str, dict[str, Any]] = {}
        with self._db.session(exclusive=False) as db:
            recorded = (
                {r["id"] for r in db.all("SELECT id FROM organizations")}
                if db.columns("organizations")  # none before LP-12, read as it is
                else set()
            )
            for table, column, live in tables:
                present = db.columns(table)
                if not present:  # an older database, read as it is
                    continue
                if "org" in present:
                    rows = db.all(
                        f"SELECT {column} AS org, COUNT(*) AS n FROM {table} WHERE {live} "
                        f"GROUP BY {column}"
                    )
                else:  # before its `org` column (v0.6), every row was `default`'s
                    rows = [
                        {"org": tenancy.DEFAULT_ORG, "n": r["n"]}
                        for r in db.all(f"SELECT COUNT(*) AS n FROM {table} WHERE {live}")
                        if int(r["n"])
                    ]
                for row in rows:
                    if row["org"] is None:  # a deployment-wide hold
                        continue
                    entry = found.setdefault(str(row["org"]), {"org": str(row["org"])})
                    entry[table] = int(row["n"])
        return [
            {**found[org], "recorded": org in recorded, "recordable": accounts.is_org_id(org)}
            for org in sorted(found)
        ]

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
        `encrypt`, with inputs for a `reader`. Notes as `reveal_inputs` shows them."""
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
        items = [_feedback_item(r) for r in rows]
        return self.reveal_inputs(items, reader, id_key="decision_id", own_notes=True)

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

    def verify(self, *, notes: bool = False) -> dict[str, Any]:
        """Recompute the hash chain and each decision record's digest. With `notes` (`mendari
        audit verify --notes`), also each kept note against the commitment its event chained:
        a note changed, moved to another decision, or missing without a chained `note.erased`
        breaks verification at that event (`_check_notes`)."""
        with self._db.session(exclusive=False) as db:
            events = db.all("SELECT * FROM audit_events ORDER BY seq")
            records = {
                row["id"]: json.loads(row["record"])
                for row in db.all("SELECT id, record FROM decisions")
            }
            # An older database read as it is may have no notes table: nothing kept to check.
            has_notes = notes and "notes" not in self.unmigrated
            kept = db.all("SELECT id, decision_id, event, sealed FROM notes") if has_notes else []
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
        if not notes:
            return {"ok": True, "events": len(events), "head": prev}
        broken, counts, unopenable = self._check_notes(events, {row["id"]: row for row in kept})
        if broken is not None:
            return {"ok": False, "events": len(events), "broken_at": broken, "notes": counts}
        if unopenable:  # the chain holds; these notes cannot be checked with this ring
            return {"ok": False, "events": len(events), "notes": counts, "unopenable": unopenable}
        return {"ok": True, "events": len(events), "head": prev, "notes": counts}

    def _check_notes(
        self, events: list[dict[str, Any]], kept: Mapping[str, Mapping[str, Any]]
    ) -> tuple[int | None, dict[str, int], dict[str, int]]:
        """The `seq` of the first event whose note does not match its commitment (or None); how
        many notes were checked, found erased, or left unchecked (sealed, and this deployment not
        in mode `encrypt`); and, by the key each names (`legacy`: sealed before key ids), the
        notes no key of this deployment's ring opens. A note that fails under a key the ring has,
        by the id it names, was altered: that breaks verification."""
        counts = {"checked": 0, "erased": 0, "unchecked": 0}
        unopenable: Counter[str] = Counter()
        known: set[str] = set()
        if "settings" not in self.unmigrated:  # an older database, read as it is, has none
            with self._db.session(exclusive=False) as db:
                known = {
                    str(row["name"])[len(KNOWN) :]
                    for row in db.all("SELECT name FROM settings WHERE name LIKE ?", (KNOWN + "%",))
                }
        missing = known - self.phi.ring_ids  # keys this database held that this ring lacks
        payloads = [(event, json.loads(event["payload"])) for event in events]
        erased = {p.get("note_id") for e, p in payloads if e["event"] == "note.erased"}
        for event, payload in payloads:
            ref = payload.get("note_ref") if isinstance(payload, dict) else None
            if not isinstance(ref, Mapping):
                continue
            row = kept.get(str(ref.get("id")))
            if row is None or (row["decision_id"], row["event"]) != (
                event["decision_id"],
                event["event"],
            ):
                return event["seq"], counts, dict(unopenable)
            if row["sealed"] is None:
                if ref["id"] not in erased:
                    return event["seq"], counts, dict(unopenable)
                counts["erased"] += 1
                continue
            try:
                body = self._note_body(row)
            except MendariError:
                # Unopenable only for want of a key this database held (a legacy token, if any
                # is missing); anything else, a forged key id included, was altered.
                named = token_key(str(row["sealed"]))
                if not (named in missing or (named == LEGACY and missing)):
                    return event["seq"], counts, dict(unopenable)
                unopenable[named] += 1
                continue
            if body is None:
                counts["unchecked"] += 1
                continue
            salt = base64.b64decode(str(body.get("salt", "")))
            if _commitment(salt, str(body.get("text"))) != ref.get("sha256"):
                return event["seq"], counts, dict(unopenable)
            counts["checked"] += 1
        return None, counts, dict(unopenable)


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


def _membership(row: Any) -> dict[str, Any]:
    """A membership row as a dict, its roles a list."""
    data = dict(row)
    data["roles"] = json.loads(data["roles"])
    return data


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
    """Milliseconds until a bucket at `row` admits `interval` more (one request's worth, or
    a weighted take's); None: it admits now."""
    tat = max(int(row["tat"]), now) if row is not None else now
    wait = tat + interval - tolerance - now
    return wait if wait > 0 else None


def _session_key(kind: str, value: str) -> str:
    """A session-table row's key: sha256(kind || 0x00 || value). Kinds are short lowercase names
    (never a NUL), so two (kind, value) pairs share a preimage only if they are equal."""
    if not _SESSION_KIND.fullmatch(kind):
        raise ValueError(f"not a session kind: {kind!r}")
    return hashlib.sha256(kind.encode() + b"\x00" + value.encode()).hexdigest()


def _opener(kind: str, key: str, secret: str | None) -> str | None:
    """The secret a row of `kind` is sealed under (None: not a sealed kind)."""
    if kind not in SEALED_KINDS:
        return None
    if secret is None and kind == "pending":
        raise ValueError("a pending launch is sealed under its binding cookie: pass its secret")
    return key if secret is None else secret


def _sealed(
    row_key: str, kind: str, key: str, data: Mapping[str, Any], secret: str | None
) -> Mapping[str, Any]:
    """What a row stores: `data`, sealed when its kind is."""
    opener = _opener(kind, key, secret)
    if opener is None:
        return data
    return seal(data, secret=opener, kind=kind, row_key=row_key)


def _opened(row_key: str, kind: str, stored: str, opener: str | None) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(stored)
    if opener is None:
        return data
    return unseal(data, secret=opener, kind=kind, row_key=row_key)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _commitment(salt: bytes, text: str) -> str:
    """A note's commitment, chained in its event's `note_ref`: hiding (a 32-byte salt kept only
    with the note) and binding (SHA-256)."""
    return hashlib.sha256(NOTE_AAD + salt + text.encode()).hexdigest()


def _legacy_fields(event: Mapping[str, Any]) -> dict[str, Any]:
    """The free text an event chained itself before notes left the chain (`LEGACY_TEXT`)."""
    payload = event["payload"]
    if not isinstance(payload, Mapping) or "note_ref" in payload:
        return {}
    fields = LEGACY_TEXT.get(event["event"], ())
    return {key: payload[key] for key in fields if payload.get(key) is not None}


def _take_evidence(record: dict[str, Any]) -> dict[str, list[str]]:
    """Empty each answer's evidence in a record about to be stored; what there was, by question
    (only questions that had some)."""
    taken: dict[str, list[str]] = {}
    for question, answer in record["answers"].items():
        if answer.get("evidence"):
            taken[question] = answer["evidence"]
        answer["evidence"] = []
    return taken


def _give_evidence(record: dict[str, Any], evidence: Mapping[str, list[str]]) -> None:
    """Put sealed evidence back into the record it was taken from, for a reader."""
    for question, given in evidence.items():
        if question in record["answers"]:
            record["answers"][question]["evidence"] = given


def _held_input(item: Mapping[str, Any]) -> bool:
    """Whether an item's record holds input values itself: its input (kept under PHI mode
    `off`), or, in every mode before 2026-10-02, its answers' evidence or a resource's logical
    id (which can be a medical record number) in its input issues and reasons."""
    record = item.get("record")
    if not isinstance(record, dict):
        return False
    answers = record.get("answers") or {}
    return (
        record.get("input") is not None
        or any(isinstance(answer, dict) and answer.get("evidence") for answer in answers.values())
        or bool(_resource_ids(record))
    )


def _resource_ids(record: Mapping[str, Any]) -> dict[str, str]:
    """The resources an older record names by logical id in its input issues ("Patient/p1"),
    each with what it is shown as instead ("Patient"). Records since 2026-10-02 name one by type
    and position ("Patient (entry 2)"), never with a "/"."""
    return {
        str(issue["resource"]): str(issue["resource"]).partition("/")[0]
        for issue in record.get("input_issues") or ()
        if isinstance(issue, dict) and "/" in str(issue.get("resource", ""))
    }


def _withhold(record: dict[str, Any]) -> None:
    """A record without its input, its answers' evidence, which can quote it, and the logical
    ids an older record names resources by (as `DecisionResult.to_record` names them now)."""
    record["input"] = None
    for answer in (record.get("answers") or {}).values():
        if isinstance(answer, dict) and answer.get("evidence"):
            answer["evidence"] = []
    ids = _resource_ids(record)
    if not ids:
        return
    record["input_issues"] = [
        {**issue, "resource": ids.get(str(issue.get("resource")), issue.get("resource"))}
        if isinstance(issue, dict)
        else issue
        for issue in record["input_issues"]
    ]
    reasons = [str(reason) for reason in record.get("reasons") or ()]
    for label, kept in ids.items():  # a label is followed by a space and a FHIRPath
        reasons = [reason.replace(f": {label} ", f": {kept} ") for reason in reasons]
    record["reasons"] = reasons


def _legacy_text(item: Mapping[str, Any], own_notes: bool) -> bool:
    """Whether an item carries free text from before notes left the chain: on one of its events,
    or (a rating) in its own row's `note` column."""
    if own_notes and item.get("note") is not None:
        return True
    return any(_legacy_fields(event) for event in item.get("events") or ())


def _show_note(
    event: dict[str, Any], views: Mapping[str, tuple[str, str | None]], reader: str | None
) -> None:
    """An event's note as `reveal_inputs` shows it (`note`, `note_status`), if it has one."""
    ref = event["payload"].get("note_ref")
    if isinstance(ref, Mapping):
        # A missing row (never deleted, only emptied) is for `verify --notes` to report.
        event["note_status"], event["note"] = views.get(str(ref.get("id")), ("missing", None))
        return
    legacy = _legacy_fields(event)
    if not legacy:
        return
    if reader:
        text = legacy.get("note") if set(legacy) == {"note"} else _canonical(legacy)
        event["note_status"], event["note"] = "open", text
        return
    event["payload"] = {**event["payload"], **dict.fromkeys(legacy)}
    event["note_status"], event["note"] = "sealed", None


def _show_own_note(
    item: dict[str, Any], views: Mapping[str, tuple[str, str | None]], reader: str | None
) -> None:
    """A rating's note: its row's `note` column before notes left the chain, else its note."""
    if item.get("note") is not None:
        item["note_status"] = "open" if reader else "sealed"
        item["note"] = item["note"] if reader else None
        return
    item["note_status"], item["note"] = views.get(str(item["id"]), (None, None))


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
