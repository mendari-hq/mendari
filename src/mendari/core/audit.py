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
import binascii
import hashlib
import json
import re
import secrets
import statistics
import time
import uuid
from collections import Counter
from collections.abc import Callable, Hashable, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from mendari.core import accounts, actions, clock, tenancy
from mendari.core.db import Backend as _Backend
from mendari.core.db import Baseline, connect
from mendari.core.db import Session as _Session
from mendari.core.phi import NOTE_AAD, PhiPolicy, key_id, token_key
from mendari.core.query import Atom, Page, Query, Within
from mendari.core.rate_buckets import RateBuckets
from mendari.core.session_seal import SealError, seal, unseal
from mendari.core.spec import Outcome
from mendari.errors import (
    AgentNotFoundError,
    AuthError,
    InvitationRefused,
    MendariError,
    NoteError,
    ProviderError,
    ReviewError,
    UnknownCard,
)

Rating = Literal["up", "down"]

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from mendari.core.decision import DecisionResult
    from mendari.providers.base import Metered

GENESIS = "0" * 64
# The events that create or change a decision's result. Each makes a new version of the
# decision's FHIR Task and gets a Provenance (standards/task_fhir.py, audit_fhir.py); the others
# (feedback, input reads and purges) change neither.
RESULT_EVENTS = ("decision.created", "review.resolved", "outcome.recorded")
_REVIEWABLE = (Outcome.HUMAN_REVIEW.value, Outcome.ABSTAIN.value)
# Arbitrary constant key for the Postgres advisory lock that serialises hash-chain appends.
_CHAIN_LOCK = 0x0E4_A0D17
# A system that reports a downstream outcome, named as an identifier ("ehr", "mendari",
# "urn:oid:1.2.3"), never free text: it is chained for good and exported in FHIR.
# A person acknowledged a notice version (mendari.notices; chained under notice:<v>:<id>).
NOTICE_EVENT = "notice.acknowledged"
SYSTEM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,99}$")
# A served agent's labels for one of its questions, by (agent id, question); None when unknown.
QuestionLabels = Callable[[str, str], Sequence[str] | None]
# Notes (Phase 4b, ADR-0005: commit, don't copy). Free text a person writes about a decision
# is kept in `notes`, apart from the chain, which keeps `note: null` and a `note_ref`:
# the note's id and sha256(prefix || salt || text). The 32-byte salt is only in the note's row,
# so the commitment says nothing about the text, and once the row is erased it is noise.
# Why a person erases a note (chained, so a code): it should not have been written, or was wrong.
# Retention erases with its own reason, "retention".
ERASE_REASONS = ("request", "error")
# Retention deletes this many rows per transaction (and chains their events with them), so a
# backlog never holds the chain lock long and each batch done stays done.
PURGE_BATCH = 500
# The chain id a purge's summary is chained under (`retention.purged`): no decision's.
RETENTION_CHAIN = "retention"
# What the usage ledger counts remote calls for (LP-21g): live decisions, and evaluations.
USAGE_PURPOSES = ("decision", "evaluation")
# The chain id the break-glass ending of sessions is chained under (`sessions.ended`, LP-21l).
SESSIONS_CHAIN = "sessions"
# The `settings` row a database is marked with once a store opens it under PHI mode `hash` or
# `encrypt` (the first such mode): from then on no store under `off` keeps input text in it.
PROTECTED = "phi_protected"
# Key rotation (LP-09). Key events are chained under `KEYS_CHAIN` (`phi.rotated`,
# `input_hash.epoch`), no decision's. Every token names its key (`core/phi.py`). `settings`
# rows: the key the last rotation re-sealed under (`ROTATION_TARGET`); each key a rotation moved
# values off (`RETIRED` + its id), which then seals nothing more here; each PHI key a deployment
# ever held (`KNOWN` + its id), so a value naming another was altered; and each input hash key's
# epoch (`HASH_EPOCH` + its id: since when).
KEYS_CHAIN = "keys"
ROTATION_TARGET = "phi_key_target"
RETIRED = "phi_key_retired:"
KNOWN = "phi_key_known:"
HASH_EPOCH = "hash_epoch:"
ROTATE_BATCH = 200  # values a rotation re-seals per batch (one chained event each)
CHECK_FAILURES_SHOWN = 50
# Rows per insert when chaining several events at once (`_append_many`): a page is at most 500.
_APPEND_BATCH = 500
# The organization in the chain (LP-12g, ADR-0008): every event but the deployment's names the
# organization of the row it is about (`_bound`), whichever mode or command writes it, and
# `verify` checks that name against the row.
# The first administrator (`AuditStore.first_administrator`). A database on which the
# first-administrator setting was honoured is marked (`FIRST_ADMIN`, a `settings` row holding the
# membership it made, never removed): from then on the setting gives nothing here, whatever it
# names. The membership it makes is chained as made by `FIRST_ADMIN_BY`, no person's id.
FIRST_ADMIN = "first_admin"
FIRST_ADMIN_BY = "first-administrator setting"
# What the operator's checks read of the work a schedule or an operator does (`system_runs`,
# outside the chain, as `alert_deliveries` is): each job's last run, when, and how it ended.
# `detail` is a fixed code, never a message or a count of anyone's data.
RUN_JOBS = ("retention", "checkpoint", "monitor", "backup", "audit.verify", "ehr.check")
RUN_OUTCOMES = ("ok", "failed")
# What a CDS Hooks card's feedback chains (LP-23f, `record_card_feedback`).
CARD_FEEDBACK = "card.feedback"
# Each kind of event by what it is about. On a decision: the kinds the FHIR export reads
# (standards/audit_fhir.py `_EVENTS`), whose organization is the decision's.
DECISION_KINDS = frozenset(
    {
        "decision.created",
        "review.resolved",
        "outcome.recorded",
        "feedback.recorded",
        "input.read",
        "input.purged",
        "input.erased",
        "note.erased",
        # A clinician's feedback on a CDS Hooks card (LP-23f): codes and ids only.
        CARD_FEEDBACK,
        # An action an agent proposed, or a caller asked about, and a held action's release or
        # denial by the decision's reviewer (spec §13.8, ADR-0023): ids, the declared name, the
        # effect, the disposition, a reason code and a hash of the arguments. Never the
        # arguments, nor a name the manifest does not declare.
        "action.proposed",
        "action.resolved",
    }
)
# On a row of its own, under `<prefix>:<id>`: the table, its id column, and the row's
# organization column. A hold's is NULL when it is the deployment's, a hold set for every
# organization.
SUBJECT_TABLES: dict[str, tuple[str, str, str]] = {
    "key": ("api_keys", "id", "org"),
    "client": ("oauth_clients", "client_id", "org"),
    "governance": ("governance_approvals", "id", "org"),
    "registry_approval": ("registry_approvals", "id", "org"),  # a pulled agent (LP-18d)
    "hold": ("automation_holds", "id", "org"),
    "eval": ("eval_reports", "id", "org"),
    "org": ("organizations", "id", "id"),  # an organization is its own
    "membership": ("memberships", "id", "org"),
    "invitation": ("invitations", "id", "org"),
    "access": ("access_requests", "id", "org"),
    "store_version": ("store_versions", "id", "org"),  # a version in an agent store (LP-20e)
    "store_key": ("store_keys", "id", "org"),  # an author's key for an agent store (LP-20f)
    "store_dataset": ("store_datasets", "id", "org"),  # an agent store's dataset (LP-20g)
    "store_job": ("store_jobs", "id", "org"),  # an agent store's evaluation job (LP-20g)
    # An agent's connection, set in the console for one organization (ADR-0021).
    "agent_connection": ("agent_connections", "id", "org"),
    # A grading round of written output (ADR-0025, `core/written.py`).
    "written": ("written_rounds", "id", "org"),
}
ADMINISTRATIVE_KINDS: dict[str, str] = {  # kind -> its subject's prefix (`SUBJECT_TABLES`)
    "key.created": "key",
    "key.rotated": "key",
    "key.revoked": "key",
    "client.registered": "client",
    "client.revoked": "client",
    "client.keys_replaced": "client",  # a client's JWK Set replaced (LP-12h)
    "governance.approved": "governance",
    "governance.revoked": "governance",
    "registry_agent.approved": "registry_approval",  # a pulled agent's digest (LP-18d)
    "registry_agent.revoked": "registry_approval",
    "automation.held": "hold",
    "automation.lifted": "hold",
    "eval.recorded": "eval",
    "organization.created": "org",
    "membership.created": "membership",
    "membership.roles_changed": "membership",
    "membership.removed": "membership",
    "membership.bound": "membership",
    "invitation.created": "invitation",
    "invitation.accepted": "invitation",
    "invitation.revoked": "invitation",
    "access.requested": "access",
    "access.handled": "access",
    # An agent store's version (LP-20e, `core/agent_store.py`): its digest, the actor's id and the
    # hash of its row as written; each move of its state. Never a file's content.
    "store_version.created": "store_version",
    "store_version.withdrawn": "store_version",
    # Promotion (LP-20h): a version signed off to be served to its organization, by someone who
    # wrote no version of it, on named evaluation reports; and the promotion it replaced.
    "store_version.promoted": "store_version",
    "store_version.retired": "store_version",
    # An author's public key registered for an organization's store, and revoked (LP-20f).
    "store_key.added": "store_key",
    "store_key.revoked": "store_key",
    # A dataset uploaded to an organization's store (LP-20g): ids, its SHA-256 and how many
    # cases, never a case.
    "store_dataset.created": "store_dataset",
    # An evaluation job (LP-20g): what it runs, on which dataset (its SHA-256), who asked.
    "store_job.created": "store_job",
    # The improvement dataset's proposed behaviour tests, accepted or dismissed for a draft
    # (LP-20i): the dataset, the case (a decision's id) and the digest, never an input.
    "store_dataset.proposal_accepted": "store_dataset",
    "store_dataset.proposal_dismissed": "store_dataset",
    # An agent's connection for one organization (ADR-0021, `core/connections.py`): set or
    # changed (the agent, the endpoint's origin and whether it carries a token), and cleared.
    # Never the token, nor the endpoint's path.
    "agent_connection.set": "agent_connection",
    "agent_connection.cleared": "agent_connection",
    # An origin the deployment's allow-list does not name, allowed for one agent in one
    # organization by an operator of the deployment: the agent, the origin and who.
    "agent_connection.origin_allowed": "agent_connection",
    # A grading round of written output (ADR-0025, `core/written.py`): made from a run (the
    # agent's digest, the rubric and its hash, the run's ledger id, how many items); each
    # assignment, each showing of an item to its grader, each grade as a salted commitment; its
    # closing (how many grades, how many texts erased, the report's hash); and texts erased by
    # retention. Ids, hashes and counts: never a text, a context or a grade.
    "written.round_created": "written",
    "written.assigned": "written",
    "written.item_read": "written",
    "written.graded": "written",
    "written.round_closed": "written",
    "written.erased": "written",
}
# The kinds whose payload commits to something a grading round stores: a grade, and a closed
# round's report (`core/written.py` `check_stored`, which `verify` runs).
_WRITTEN_COMMITTED = frozenset({"written.graded", "written.round_closed"})
# An agent store version's state after each of its kinds: `verify` checks a version's row against
# its newest (LP-20e).
STORE_STATES: dict[str, str] = {
    "store_version.created": "draft",
    "store_version.withdrawn": "withdrawn",
    "store_version.promoted": "promoted",
    "store_version.retired": "retired",
}
# A version row's columns that never change once written, in the order their hash reads them.
STORE_VERSION_COLUMNS = (
    "id",
    "org",
    "digest",
    "agent_id",
    "version",
    "files",
    "parent_digest",
    "source",
    "copied_from",
    "validation",
    "checks",
    "created_by",
    "created_at",
)
# A person's, across organizations: the organization they acted for is in the payload, no row.
PERSON_KINDS = frozenset({NOTICE_EVENT})
# The deployment's own, no organization's: never bound.
DEPLOYMENT_KINDS = frozenset(
    {
        "deployment.recorded",
        "input_hash.epoch",
        "phi.rotated",
        "retention.purged",
        "sessions.ended",
        "user.recorded",
    }
)

# The first release's schema, the engine's baseline (`core/db.py`): marked `mendari-engine/1`
# when it is made, refused at open when made by a pre-release Mendari. One chain runs through
# `audit_events` (`_append_many` reads its head over every row), so each event follows a
# different one: no two rows share a `prev_hash`, the first row's GENESIS included. The unique
# index `audit_events_prev_hash` makes the database itself refuse a second event chained after
# the same head (a fork), whatever a future writer gets wrong.
_TABLES = """
CREATE TABLE IF NOT EXISTS decisions (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    agent_id         TEXT NOT NULL,
    agent_version    TEXT NOT NULL,
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
    org             TEXT NOT NULL,
    refused         INTEGER,
    provider_errors INTEGER,
    observed_label  TEXT
);
CREATE INDEX IF NOT EXISTS decisions_queue ON decisions (review_status, priority, created_at);
CREATE INDEX IF NOT EXISTS decisions_agent ON decisions (agent_id, created_at);
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
CREATE UNIQUE INDEX IF NOT EXISTS audit_events_prev_hash ON audit_events (prev_hash);
CREATE INDEX IF NOT EXISTS decisions_created ON decisions (created_at, id);
CREATE TABLE IF NOT EXISTS eval_reports (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    agent_id     TEXT NOT NULL,
    dataset     TEXT NOT NULL,
    report      TEXT NOT NULL,
    org         TEXT NOT NULL,
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
    agent_id           TEXT NOT NULL,
    rating            TEXT NOT NULL,
    reporter          TEXT,
    expected_question TEXT,
    expected_label    TEXT,
    superseded_by     TEXT
);
CREATE INDEX IF NOT EXISTS feedback_agent ON feedback (agent_id, rating, created_at);
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
    agent_id      TEXT NOT NULL,
    agent_digest  TEXT NOT NULL,
    risk_level   INTEGER NOT NULL,
    approver     TEXT NOT NULL,
    reference    TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    revoked_at   TEXT,
    revoked_by   TEXT
);
CREATE TABLE IF NOT EXISTS registry_approvals (
    id           TEXT PRIMARY KEY,
    org          TEXT NOT NULL,
    agent_id      TEXT NOT NULL,
    agent_digest  TEXT NOT NULL,
    registry     TEXT NOT NULL,
    approver     TEXT NOT NULL,
    reference    TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    revoked_at   TEXT,
    revoked_by   TEXT
);
CREATE TABLE IF NOT EXISTS automation_holds (
    id           TEXT PRIMARY KEY,
    org          TEXT,
    agent_id      TEXT,
    agent_digest  TEXT,
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
    agent_id      TEXT NOT NULL,
    agent_version TEXT NOT NULL,
    agent_digest  TEXT NOT NULL,
    providers    TEXT NOT NULL,
    started_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS deployments_agent ON deployments (agent_id, started_at);
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
    replaced_by TEXT,
    preset      TEXT
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
    revoked_by  TEXT,
    keys_version      INTEGER,
    keys_replaced_at  TEXT
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_sha256  TEXT PRIMARY KEY,
    client_id     TEXT NOT NULL,
    scope         TEXT NOT NULL,
    expires_at    INTEGER NOT NULL,
    keys_version  INTEGER
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
CREATE TABLE IF NOT EXISTS principal_usage (
    day         TEXT NOT NULL,
    principal   TEXT NOT NULL,
    count       INTEGER NOT NULL,
    PRIMARY KEY (day, principal)
);
CREATE TABLE IF NOT EXISTS usage_ledger (
    day            TEXT NOT NULL,
    org            TEXT NOT NULL,
    provider_type  TEXT NOT NULL,
    purpose        TEXT NOT NULL,
    calls          INTEGER NOT NULL,
    priced_calls   INTEGER NOT NULL,
    input_tokens   BIGINT NOT NULL,
    output_tokens  BIGINT NOT NULL,
    cost_usd       {real} NOT NULL,
    PRIMARY KEY (day, org, provider_type, purpose)
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
);
CREATE TABLE IF NOT EXISTS alert_deliveries (
    id               TEXT PRIMARY KEY,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    event            TEXT NOT NULL,
    org              TEXT,
    body             TEXT NOT NULL,
    status           TEXT NOT NULL,
    attempts         INTEGER NOT NULL,
    next_attempt_at  TEXT,
    last_status      INTEGER,
    last_error       TEXT
);
CREATE INDEX IF NOT EXISTS alert_deliveries_due ON alert_deliveries (status, next_attempt_at);
CREATE TABLE IF NOT EXISTS audit_checkpoints (
    seq         BIGINT PRIMARY KEY,
    hash        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoint_statements (
    day         TEXT PRIMARY KEY,
    seq         BIGINT NOT NULL,
    statement   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS system_runs (
    job      TEXT PRIMARY KEY,
    ran_at   TEXT NOT NULL,
    outcome  TEXT NOT NULL,
    detail   TEXT
);
CREATE TABLE IF NOT EXISTS store_files (
    sha256  TEXT PRIMARY KEY,
    size    INTEGER NOT NULL,
    data    {blob} NOT NULL
);
CREATE TABLE IF NOT EXISTS store_versions (
    id             TEXT PRIMARY KEY,
    org            TEXT NOT NULL,
    digest         TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    version        TEXT NOT NULL,
    files          TEXT NOT NULL,
    parent_digest  TEXT,
    source         TEXT NOT NULL,
    copied_from    TEXT,
    validation     TEXT NOT NULL,
    checks         TEXT,
    created_by     TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    state          TEXT NOT NULL,
    state_at       TEXT NOT NULL,
    pulled         TEXT,
    UNIQUE (org, digest)
);
CREATE INDEX IF NOT EXISTS store_versions_agent ON store_versions (org, agent_id, created_at);
CREATE TABLE IF NOT EXISTS store_pins (
    org        TEXT NOT NULL,
    registry   TEXT NOT NULL,
    name       TEXT NOT NULL,
    value      TEXT NOT NULL,
    pinned_by  TEXT NOT NULL,
    pinned_at  TEXT NOT NULL,
    PRIMARY KEY (org, registry, name)
);
CREATE TABLE IF NOT EXISTS agent_connections (
    id            TEXT PRIMARY KEY,
    org           TEXT NOT NULL,
    agent_id       TEXT NOT NULL,
    endpoint      TEXT,
    token         TEXT,
    token_set_by  TEXT,
    token_set_at  TEXT,
    changed_by    TEXT NOT NULL,
    changed_at    TEXT NOT NULL,
    allowed_origin TEXT,
    allowed_by    TEXT,
    allowed_at    TEXT,
    UNIQUE (org, agent_id)
);
CREATE TABLE IF NOT EXISTS store_usage (
    org      TEXT NOT NULL,
    day      TEXT NOT NULL,
    uploads  INTEGER NOT NULL,
    bytes    INTEGER NOT NULL,
    jobs     INTEGER NOT NULL DEFAULT 0,
    datasets INTEGER,
    PRIMARY KEY (org, day)
);
CREATE TABLE IF NOT EXISTS store_keys (
    id          TEXT PRIMARY KEY,
    org         TEXT NOT NULL,
    key_id      TEXT NOT NULL,
    public_pem  TEXT NOT NULL,
    added_by    TEXT NOT NULL,
    added_at    TEXT NOT NULL,
    revoked_at  TEXT,
    UNIQUE (org, key_id)
);
CREATE TABLE IF NOT EXISTS store_uploads (
    id          TEXT PRIMARY KEY,
    org         TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    size        INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS store_datasets (
    id          TEXT PRIMARY KEY,
    org         TEXT NOT NULL,
    name        TEXT NOT NULL,
    agent_id     TEXT NOT NULL,
    digest      TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    cases       INTEGER NOT NULL,
    data        TEXT NOT NULL,
    sealed      INTEGER NOT NULL,
    report      TEXT NOT NULL,
    size        INTEGER,
    kind        TEXT NOT NULL DEFAULT 'upload',
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    state       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS store_datasets_org ON store_datasets (org, created_at);
CREATE TABLE IF NOT EXISTS store_jobs (
    id              TEXT PRIMARY KEY,
    org             TEXT NOT NULL,
    target          TEXT NOT NULL,
    dataset         TEXT NOT NULL,
    dataset_sha256  TEXT NOT NULL,
    dataset_cases   INTEGER NOT NULL,
    config          TEXT NOT NULL,
    state           TEXT NOT NULL,
    done            INTEGER NOT NULL,
    total           INTEGER NOT NULL,
    lines           TEXT NOT NULL,
    latencies       TEXT,
    header          TEXT,
    claim           TEXT,
    claimed_until   TEXT,
    report_id       TEXT,
    error           TEXT,
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    finished_at     TEXT
);
CREATE INDEX IF NOT EXISTS store_jobs_org ON store_jobs (org, created_at);
CREATE TABLE IF NOT EXISTS store_promotions (
    id          TEXT PRIMARY KEY,
    org         TEXT NOT NULL,
    agent_id     TEXT NOT NULL,
    digest      TEXT NOT NULL,
    version     TEXT NOT NULL,
    version_id  TEXT NOT NULL,
    authors     TEXT NOT NULL,
    approver    TEXT NOT NULL,
    reports     TEXT NOT NULL,
    reference   TEXT NOT NULL,
    note        TEXT,
    automate    INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    retired_at  TEXT,
    retired_by  TEXT
);
CREATE INDEX IF NOT EXISTS store_promotions_org ON store_promotions (org, agent_id, retired_at);
CREATE TABLE IF NOT EXISTS store_proposals (
    id          TEXT PRIMARY KEY,
    org         TEXT NOT NULL,
    digest      TEXT NOT NULL,
    dataset_id  TEXT NOT NULL,
    case_id     TEXT NOT NULL,
    state       TEXT NOT NULL,
    decided_by  TEXT NOT NULL,
    decided_at  TEXT NOT NULL,
    UNIQUE (org, digest, dataset_id, case_id)
);
CREATE TABLE IF NOT EXISTS store_upload_parts (
    upload_id  TEXT NOT NULL,
    n          INTEGER NOT NULL,
    data       {blob} NOT NULL,
    PRIMARY KEY (upload_id, n)
);
CREATE INDEX IF NOT EXISTS governance_approvals_lookup
    ON governance_approvals (org, agent_id, agent_digest, revoked_at);
CREATE INDEX IF NOT EXISTS registry_approvals_lookup
    ON registry_approvals (org, agent_id, agent_digest, revoked_at);
CREATE INDEX IF NOT EXISTS automation_holds_active
    ON automation_holds (lifted_at, org, agent_id);
CREATE INDEX IF NOT EXISTS audit_events_event_seq
    ON audit_events (event, seq);
CREATE INDEX IF NOT EXISTS oauth_tokens_client
    ON oauth_tokens (client_id);
CREATE INDEX IF NOT EXISTS memberships_user_active
    ON memberships (user_id) WHERE removed_at IS NULL;
CREATE INDEX IF NOT EXISTS memberships_pending_email
    ON memberships (pending_email) WHERE removed_at IS NULL AND user_id IS NULL;
CREATE INDEX IF NOT EXISTS decision_inputs_created
    ON decision_inputs (created_at, decision_id);
CREATE INDEX IF NOT EXISTS notes_created_sealed
    ON notes (created_at, id) WHERE sealed IS NOT NULL;
CREATE INDEX IF NOT EXISTS alert_deliveries_created
    ON alert_deliveries (created_at DESC);
CREATE TABLE IF NOT EXISTS written_rounds (
    id             TEXT PRIMARY KEY,
    org            TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    agent_digest    TEXT NOT NULL,
    rubric         TEXT NOT NULL,
    rubric_sha256  TEXT NOT NULL,
    header         TEXT NOT NULL,
    ledger         TEXT NOT NULL,
    state          TEXT NOT NULL,
    created_by     TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    closed_at      TEXT,
    report         TEXT
);
CREATE TABLE IF NOT EXISTS written_items (
    id          TEXT PRIMARY KEY,
    round_id    TEXT NOT NULL,
    org         TEXT NOT NULL,
    item        TEXT NOT NULL,
    body        TEXT,
    sealed      INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    erased_at   TEXT,
    UNIQUE (round_id, item)
);
CREATE INDEX IF NOT EXISTS written_items_round ON written_items (round_id, id);
CREATE TABLE IF NOT EXISTS written_assignments (
    id             TEXT PRIMARY KEY,
    org            TEXT NOT NULL,
    round_id       TEXT NOT NULL,
    item_id        TEXT NOT NULL,
    user_id        TEXT NOT NULL,
    membership_id  TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    ended_at       TEXT,
    grades         TEXT,
    graded_at      TEXT
);
CREATE INDEX IF NOT EXISTS written_assignments_user
    ON written_assignments (user_id, org, created_at);
CREATE INDEX IF NOT EXISTS written_assignments_round
    ON written_assignments (round_id, item_id)
"""
# Some columns, by table. `decisions`: `org`, the organization it was made for; `refused`, 1
# when the input was refused before any provider ran (a FHIR Task `failed`, `task_fhir`);
# `provider_errors`, how many of its questions have a provider error recorded (`errors`: a
# provider failed, or a composite's member did, so the answer is missing or incomplete; or a
# question they feed), for monitoring's provider error share (LP-21d), outside the chain;
# `observed_label`, what a downstream system saw happen (`record_outcome`), apart from a
# reviewer's `final_label`. `eval_reports.ledger`: the run's mendari.ledger/1 (LP-10).
# `api_keys`: NULL `scopes` is a key with its role's permissions (ADR-0004); `replaced_by` is a
# rotated key's replacement (LP-09), `preset` the integration path it was made for (LP-12i).
# `oauth_clients.keys_version` (NULL is 0) is bumped each time the client's key set is replaced
# (LP-12h), and a token carries the version its assertion was checked against.
# `decision_inputs.details`: what a record leaves out beside its kept input, sealed (each
# answer's evidence; NULL: none). `store_usage.datasets`: the datasets an organization's store
# added that day (NULL: none yet).

# The `store_` tables (LP-20e, ADR-0016, `core/agent_store.py`) hold each organization's agent
# store: `store_files` each file once by its SHA-256, whichever versions hold it;
# `store_versions` a version's row, written once (`STORE_VERSION_COLUMNS`) but its `state`, each
# write one chained event; and `store_usage` an organization's uploads and bytes a day, outside
# the chain, as `rate_limits` is. LP-20f adds `store_keys` (authors' public keys, by
# organization, chained `store_key.*`) and `store_uploads` with `store_upload_parts` (an
# archive sent in parts, kept until it is joined or a day old, outside the chain). Empty unless
# the store is on. A version pulled from
# a registry in the console (`source` `hub`) keeps where it came from in `pulled` (the registry,
# the namespace, the signing key, the archive's SHA-256 and what was checked), hashed into its
# `store_version.created` event with the rest of its row; `store_pins` keeps the registry keys an
# organization pinned on first use (per registry: a namespace's key ids, the registry's own key
# id), outside the chain, as the command line keeps them in a file (`hub/client.py`).

# `agent_connections` (ADR-0021, `core/connections.py`): the service that answers for an agent
# in one organization, set in the console: one row per organization and agent, kept when the
# connection is cleared (`endpoint` NULL then), since its events name it. `token` is the bearer
# token sealed under the PHI key (`core/phi.py`, kind `connection`), bound to the row's id, its
# organization and its agent; never in an event, an answer or a log line. `allowed_origin`,
# with who and when, is the origin an operator of the deployment allowed for this agent in this
# organization where the deployment's allow-list does not name it (chained
# `agent_connection.origin_allowed`); NULL otherwise, and once the connection is cleared.

# The `written_` tables (ADR-0025, `core/written.py`) hold grading rounds of written output:
# `written_rounds` a round made from a run (`header`: the rubric's text as a grader reads it,
# clear, the manifest's own words; `ledger`: the run's `mendari.written-ledger/1`, hashes and
# the judge's grades, no text; `report`: the `mendari.written/1` report once closed);
# `written_items` each sampled text with its context, in `body`, which follows notes (sealed
# under the PHI key in `encrypt`, kind `written`, bound to the row's id; plain under `off`,
# never in a protected database; refused under `hash`) and is NULL once the round closes or
# retention erased it; `written_assignments` who grades which item, and their grades (`grades`:
# level names with a salt, whose commitment the chain holds).

# How the engine's schema is marked and recognised at every open (`core/db.py`).
ENGINE_SCHEMA = Baseline(
    name="mendari-engine",
    version=1,
    anchor="audit_events",
    table="settings",
    key="name",
    missing="not a Mendari database (no audit chain in it): give the server's --db",
)
# API keys (ADR-0004): machines are viewer or service; a key lives 90 days unless asked
# otherwise, 365 at most.
MACHINE_ROLES = ("viewer", "service")
KEY_DAYS = 90
KEY_MAX_DAYS = 365
# A rotated key keeps working this many hours (24 unless asked, a week at most), then expires.
KEY_OVERLAP_HOURS = 24
KEY_MAX_OVERLAP_HOURS = 168
# A subject's history is its newest events, this many at most (a key's chain holds 2 or 3).
HISTORY_LIMIT = 200
# Expired session-table rows one put deletes (smart_put).
SESSION_PURGE_BATCH = 100
# Session-table kinds sealed under the secret that names the row (core/session_seal.py,
# ADR-0006): an EHR launch's session (its cookie) and its pending launch (its binding cookie,
# never its `state`, which travels in URLs, so `pending` must be given its secret).
# A person's session in memberships mode (`member`, server/auth.py) is sealed too: its organization
# and membership are what it may act for.
SEALED_KINDS = frozenset({"session", "pending", "member"})
# The session-table kinds the break-glass ends (`end_sessions`), by group: people's sessions
# (claims mode's `user`, memberships mode's `member`) with the sign-ins under way
# (`oidc-pending`), and EHR launches' sessions (`session`) with the launches under way
# (`pending`). Never a claim (`jti`, `cds-jti`): ending one would let the assertion or the CDS
# client's token it refused be replayed.
SESSION_GROUPS: dict[str, tuple[str, ...]] = {
    "people": ("member", "oidc-pending", "user"),
    "launches": ("pending", "session"),
}
BREAK_GLASS_REASON = 200  # characters, as a hold's

_PRIORITY_ORDER = (
    "CASE priority WHEN 'stat' THEN 0 WHEN 'asap' THEN 1 WHEN 'urgent' THEN 2 ELSE 3 END"
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def usage_totals(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The usage ledger's rows (`AuditStore.usage_ledger`) summed by provider type and purpose:
    calls, priced calls, tokens, and the cost, None (unknown) unless every call was priced."""
    totals: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        total = totals.setdefault(
            (row["provider_type"], row["purpose"]),
            {
                "provider_type": row["provider_type"],
                "purpose": row["purpose"],
                "calls": 0,
                "priced_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
            },
        )
        for count in ("calls", "priced_calls", "input_tokens", "output_tokens"):
            total[count] += row[count]
        cost = row["cost_usd"]
        total["cost_usd"] = (
            None if cost is None or total["cost_usd"] is None else (total["cost_usd"] + cost)
        )
    return [totals[key] for key in sorted(totals)]


def _add_usage(db: _Session, org: str, purpose: str, usage: Mapping[str, Metered]) -> None:
    """Add remote calls (`providers.base.Metered` by provider type) to today's usage ledger
    rows of `org` and `purpose`, in the caller's transaction: providers never write."""
    if purpose not in USAGE_PURPOSES:
        raise ValueError(f"a usage purpose is one of {USAGE_PURPOSES}")
    day = clock.today().isoformat()
    for provider_type, metered in sorted(usage.items()):
        if not metered.calls:
            continue
        db.all(
            "INSERT INTO usage_ledger (day, org, provider_type, purpose, calls, priced_calls, "
            "input_tokens, output_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (day, org, provider_type, purpose) DO UPDATE SET "
            "calls = usage_ledger.calls + excluded.calls, "
            "priced_calls = usage_ledger.priced_calls + excluded.priced_calls, "
            "input_tokens = usage_ledger.input_tokens + excluded.input_tokens, "
            "output_tokens = usage_ledger.output_tokens + excluded.output_tokens, "
            "cost_usd = usage_ledger.cost_usd + excluded.cost_usd",
            (
                day,
                org,
                provider_type,
                purpose,
                metered.calls,
                metered.priced,
                metered.input_tokens,
                metered.output_tokens,
                metered.cost_usd,
            ),
        )


def break_glass_reason(reason: str) -> str:
    """Why an operator acted from the command line, on one line (`BREAK_GLASS_REASON` at most);
    a `ValueError` for none or a longer one."""
    reason = " ".join(reason.split())
    if not reason or len(reason) > BREAK_GLASS_REASON:
        raise ValueError(f"give a reason of at most {BREAK_GLASS_REASON} characters")
    return reason


_NO_ROW = object()  # a subject `verify` did not find (a hold's organization may be None)


def _subject_orgs(db: _Session) -> dict[str, Any]:
    """Each administrative row's organization (`SUBJECT_TABLES`), by its chain id."""
    found: dict[str, Any] = {}
    for prefix, (table, column, org) in SUBJECT_TABLES.items():
        for row in db.all(f"SELECT {column} AS id, {org} AS org FROM {table}"):
            found[f"{prefix}:{row['id']}"] = row["org"]
    return found


def _attributed(
    event: Mapping[str, Any], owners: Mapping[str, str], subjects: Mapping[str, Any]
) -> bool:
    """Whether an event names its row's organization, if it names one (`verify`). By kind: on a
    decision, the decision's (`owners`); an administrative kind, its subject's, which must be
    there. The deployment's kinds, a person's (`PERSON_KINDS`) and kinds a later release added
    have no row to check, so a rollback never fails on them."""
    payload = json.loads(event["payload"])
    if not isinstance(payload, dict) or "org" not in payload:
        return True
    kind, chain_id = event["event"], str(event["decision_id"])
    if kind in DECISION_KINDS:
        return owners.get(chain_id, _NO_ROW) == payload["org"]
    prefix = ADMINISTRATIVE_KINDS.get(kind)
    if prefix is None:
        return True
    return chain_id.startswith(f"{prefix}:") and subjects.get(chain_id, _NO_ROW) == payload["org"]


# Every kind on no decision: what the administrative list reads without an organization.
OFF_DECISION_KINDS = (*ADMINISTRATIVE_KINDS, *sorted(PERSON_KINDS), *sorted(DEPLOYMENT_KINDS))


def _administrative(org: str | None, subject: str | None) -> tuple[str, str, list[Any]] | None:
    """What `administrative_events` reads (`FROM`, a `WHERE` condition and its parameters):
    for an organization, the events of each row it owns, one indexed lookup per row; for none,
    every event on no decision. None: `subject` names nothing here."""
    if org is None:
        kinds = ", ".join("?" for _ in OFF_DECISION_KINDS)
        where, params = f"e.event IN ({kinds})", list(OFF_DECISION_KINDS)
        if subject is not None:
            where, params = f"{where} AND e.decision_id = ?", [*params, subject]
        return "audit_events e", where, params
    prefix, _, row_id = (subject or "").partition(":")
    parts, params = [], []
    for name, (table, column, expression) in SUBJECT_TABLES.items():
        if subject is not None and name != prefix:
            continue
        one = f" AND {column} = ?" if subject is not None else ""
        parts.append(
            f"SELECT '{name}:' || {column} AS subject, {expression} AS org FROM {table} "
            f"WHERE {expression} = ?{one}"
        )
        params += [org, row_id] if subject is not None else [org]
    if not parts:
        return None
    union = " UNION ALL ".join(parts)
    return f"({union}) s JOIN audit_events e ON e.decision_id = s.subject", "1 = 1", params


def _mark(db: _Session, name: str, value: str) -> None:
    """Set a `settings` row unless it is set: the first value stays."""
    db.all(
        "INSERT INTO settings (name, value) VALUES (?, ?) ON CONFLICT (name) DO NOTHING",
        (name, value),
    )


def _later(delta: timedelta) -> str:
    """A time `delta` from now, formatted as `_now()` is (so the two compare as text)."""
    return (datetime.now(UTC) + delta).isoformat(timespec="milliseconds")


def _just_after(stamp: str) -> str:
    """The first time after `stamp` (a `_now()` text) that `_now()` can write: 1 ms later."""
    later = datetime.fromisoformat(stamp) + timedelta(milliseconds=1)
    return later.isoformat(timespec="milliseconds")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _migrate(db: _Session) -> None:
    """Bring the schema of an earlier release up to this one's, under the schema lock and after
    the baseline check (`core/db.py`), in the transaction that made or checked it. The first
    release's schema is the baseline (`ENGINE_SCHEMA`): there is nothing to migrate yet."""


# -- store ---------------------------------------------------------------------------------


class AuditStore(RateBuckets):
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
        `phi` is how inputs are kept (`core/phi.py`; default: as the agent says). `marks`
        (default): opening marks the database with this store's PHI mode and keys (`_protected`);
        a read-only tool passes False and only reads the marks. `read_only` opens an existing
        Mendari database and writes nothing to it, not even a mark (`core/db.py`). Every open
        checks the schema's baseline first (`ENGINE_SCHEMA`): a database a pre-release or a
        newer Mendari made is refused before anything is read. Every event names its
        organization, whoever opens the database (`_bound`).
        """
        self.phi = phi or PhiPolicy()
        self.read_only = read_only
        self._db: _Backend = connect(
            str(target),
            schema=_TABLES,
            migrate=_migrate,
            lock_key=_CHAIN_LOCK,
            baseline=ENGINE_SCHEMA,
            migrate_url=migrate_url,
            read_only=read_only,
        )
        self.path = str(target) if self._db.kind == "sqlite" else None
        if read_only:
            marks = False
        # environment -> (the CapabilityStatement version served, since when): capability_since
        self._capability_since: dict[str, tuple[str, str]] = {}
        self._hash_epoch: str | None = None  # the hash key id this process last recorded under
        self.protected = self._protected(marks)

    def _protected_on(self, db: _Session, marks: bool = True) -> str | None:
        if marks and self.phi.mode != "off":
            _mark(db, PROTECTED, self.phi.mode)
        if marks and self.phi.mode == "encrypt":
            for known in sorted(self.phi.ring_ids):
                _mark(db, KNOWN + known, _now())
        found = db.one("SELECT value FROM settings WHERE name = ?", (PROTECTED,))
        return None if found is None else str(found["value"])

    def _protected(self, marks: bool = True) -> str | None:
        """The PHI mode that first protected this database (`hash` or `encrypt`), marking it now
        (with `marks`) if this store's mode is one; None while only mode `off` has opened it. A
        store under `encrypt` also marks each key it holds as known."""
        with self._db.session(exclusive=False) as db:
            return self._protected_on(db, marks=marks)

    def chain_input_reads(self, org: str, decision_ids: Sequence[str], *, reader: str) -> int:
        """One `input.read` by `reader` for each of `org`'s decisions whose input a run of a
        sealed copy opened: a version decided over the improvement dataset (LP-20i), whose cases
        are those decisions' inputs. What the version decides of each is a function of its input
        that the version's author chose, so the run is a read of every input, chained as one
        (I2) with the payload a read of the decision's own input has. A decision of another
        organization, or no longer kept, is skipped. Returns how many were chained."""
        ids = list(dict.fromkeys(str(decision_id) for decision_id in decision_ids))
        if not ids:
            return 0
        marks = ", ".join("?" for _ in ids)
        with self._db.session(exclusive=True) as db:
            kept = {
                row["id"]
                for row in db.all(
                    f"SELECT id FROM decisions WHERE org = ? AND id IN ({marks})", [org, *ids]
                )
            }
            entries = [
                (decision_id, self._bound({"reader": reader}, org))
                for decision_id in ids
                if decision_id in kept
            ]
            self._append_many(db, "input.read", entries)
        return len(entries)

    def _bound(self, payload: Mapping[str, Any], org: str | None) -> dict[str, Any]:
        """A payload as chained: with the organization of the row it is about (`org`, which the
        caller read in the same transaction). Every kind but the deployment's names it
        (`_append_many` refuses one that does not)."""
        return {**payload, "org": org}

    def ready_to_keep(self) -> None:
        """Whether this store may keep what a decision brings: not under `off` in a protected
        database (`refuse_plaintext`), nor under `encrypt` with a key a rotation retired
        (`_refuse_retired`). The server checks at startup; a decision before any provider runs;
        each write again, in its own transaction."""
        self.refuse_plaintext()
        if self.phi.mode == "encrypt":
            with self._db.session(exclusive=False) as db:
                self._refuse_retired(db)

    def _refuse_retired(self, db: _Session) -> None:
        """Under `encrypt`, nothing is sealed with a key a rotation moved values off: a deployment
        still on the previous key, once the rotation began, refuses instead of sealing what the
        rotation would leave behind."""
        if self.phi.mode != "encrypt":
            return
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

    def _refuse_plaintext(self, db: _Session) -> None:
        if self.phi.mode != "off":
            return
        if self.protected is None:
            self.protected = self._protected_on(db)
        if self.protected:
            raise MendariError(
                f"This database is protected (first kept under PHI mode {self.protected}): run "
                "with the deployment's PHI mode, hash or encrypt, and its keys. A protected "
                "database never takes plaintext again."
            )

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

    def ping(self, timeout_s: float) -> None:
        """One read of the chain's table, bounded by `timeout_s` (the readiness route, LP-21):
        it proves this store's schema answers, where `SELECT 1` would pass on an empty file or
        another database. Raises whatever the database raised; creates and writes nothing."""
        self._db.ping("SELECT 1 FROM audit_events LIMIT 1", timeout_s)

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
        quote it: a lab value and its date), stay in the record only under PHI mode `off` with an
        agent that keeps inputs; under `encrypt` with such an agent both are sealed apart
        (`decision_inputs`), so retention can delete them; otherwise neither is kept. The
        caller's own answer (`/decide`) is unchanged. With sign-in on (and, in open mode, for a
        CDS client's verified token), the chained event also names who asked (`requested_by`,
        the principal's chain name, `tenancy.requester`). The actions the agent proposed are
        chained after it, one `action.proposed` each, their arguments kept as the input is: as
        the event's note, sealed under `encrypt`, and not at all where the input is not kept."""
        from mendari.core.decision import kept_as_json  # here: decision's imports load this module

        inputs = kept_as_json(inputs)  # as a decision keeps a refused NaN: null, so JSON
        record = result.to_record()
        keep = store_input == "full" and self.phi.mode != "hash"
        in_record = keep and self.phi.mode == "off"
        if in_record:
            self.refuse_plaintext()
        record["input"] = dict(inputs) if in_record else None
        evidence = {} if in_record else _take_evidence(record)
        status = "pending" if result.outcome.value in _REVIEWABLE else "not_required"
        org = tenancy.current() or tenancy.DEFAULT_ORG  # the row's, and its events' when bound
        with self._db.session(exclusive=True) as db:
            if in_record:
                self._refuse_plaintext(db)
            if keep and self.phi.mode == "encrypt":
                self._refuse_retired(db)
            epoch = self._mark_hash_epoch(db) if self.phi.hash_key else None
            db.all(
                "INSERT INTO decisions (id, created_at, agent_id, agent_version, outcome, "
                "recommendation, priority, confidence, latency_ms, cost_usd, record, "
                "review_status, org, refused, provider_errors) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    result.id,
                    result.created_at,
                    result.agent_id,
                    result.agent_version,
                    result.outcome.value,
                    result.recommendation,
                    result.priority.value,
                    result.confidence,
                    result.latency_ms,
                    result.cost_usd,
                    _canonical(record),
                    status,
                    org,
                    int(result.rejected),
                    len(result.errors),
                ),
            )
            created: dict[str, Any] = {"record_sha256": _sha(record)}
            requester = tenancy.requester()
            # Who asked: with sign-in on, and in open mode for a CDS client's verified token
            # (`cds <id>`, LP-23b); never anyone else in open mode.
            if requester is not None:
                created["requested_by"] = requester
            self._append(db, result.id, "decision.created", self._bound(created, org))
            # Each action proposed with the answer (spec §13.9), in the same transaction: what the
            # chain keeps of it (`ActionResult.chained`), and its arguments apart, as a note,
            # exactly when the decision's input is kept.
            proposed = [
                (
                    result.id,
                    self._bound(
                        {
                            **action.chained(),
                            "note_ref": self._keep_note(
                                db, result.id, "action.proposed", action.kept()
                            )
                            if keep
                            else None,
                        },
                        org,
                    ),
                )
                for action in result.actions or ()
            ]
            self._append_many(db, "action.proposed", proposed)
            _add_usage(db, org, "decision", result.usage)
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
        deny_actions: Sequence[str] = (),
    ) -> dict[str, Any]:
        """A human confirms or corrects a HUMAN_REVIEW / ABSTAIN decision with one of the labels
        of its primary question (`_check_label`). A note is kept apart from the chain (`notes`);
        the answer repeats it to whoever wrote it.

        The decision's held actions (spec §13.8) are resolved with it, in the same transaction,
        one `action.resolved` each: released when the reviewer's label is the recommendation,
        denied when it is another. `deny_actions` names held actions (by id) the reviewer
        refuses while confirming the label, so refusing an action never takes a false override:
        each is denied (`reviewer_denied`) and `human_override` says what it always said. An id
        that is no held action of this decision refuses the whole review, before anything is
        written. The answer has `actions` only when some were held."""
        if not reviewer.strip():
            raise ReviewError("a reviewer identity is required")
        note = self._may_keep(note)
        denied = set(deny_actions)
        with self._db.session(exclusive=True) as db:
            scope, scoped = _scope()
            row = db.one(
                f"SELECT * FROM decisions d WHERE id = ? AND {scope}", (decision_id, *scoped)
            )
            if row is None:
                raise ReviewError(f"unknown decision {decision_id}")
            if row["review_status"] != "pending":
                raise ReviewError(f"decision {decision_id} is not awaiting review")
            _check_label(row["agent_id"], json.loads(row["record"]), final_label, labels)
            # Overriding means disagreeing with a recommendation; an abstention made none.
            recommendation = row["recommendation"]
            override = None if recommendation is None else recommendation != final_label
            held = actions.held(actions.state(_events_of(db, [decision_id])[decision_id]))
            if denied - {action["id"] for action in held}:
                raise ReviewError("deny_actions names an action this decision does not hold")
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
            chained = self._bound(payload, row["org"])
            self._append(db, decision_id, "review.resolved", chained)
            # Each held action is resolved here and nowhere else, so exactly once: a held action
            # exists only on a decision that awaits review, and this is the only way out of it.
            resolved = [
                {
                    "action_id": action["id"],
                    "name": action["name"],
                    "effect": action["effect"],
                    **_resolution(override, action["id"] in denied),
                    "reviewer": reviewer,
                }
                for action in held
            ]
            self._append_many(
                db,
                "action.resolved",
                [(decision_id, self._bound(item, row["org"])) for item in resolved],
            )
        answer = {"id": decision_id, "resolved_at": resolved_at, **payload, "note": note}
        if resolved:
            answer["actions"] = [
                {
                    "id": item["action_id"],
                    **{key: item[key] for key in ("name", "effect", "disposition", "reason")},
                }
                for item in resolved
            ]
        return answer

    def propose_action(
        self,
        decision_id: str,
        *,
        agent_id: str,
        arguments_hash: str,
        kept: str | None,
        ask: Callable[[Mapping[str, Any], Sequence[Mapping[str, Any]]], actions.Asked],
    ) -> dict[str, Any]:
        """Record that a caller asked whether one action may be run for a decision of agent
        `agent_id` (`Decision.authorize_action`, spec section 13.8), and answer how the action
        stands. In one exclusive session: `ask` judges it from the decision's row (its record
        parsed) and its events; an action already proposed on the decision (the same chained
        name, the same arguments' hash; for an action with no chained name, also the same
        reason) is answered as it stands now with `repeated: True`, and nothing is chained; a new
        one is chained as `action.proposed` (`source: request`, with who asked), its name and
        arguments (`kept`) apart as the event's note, as the decision's input is kept (never
        under PHI mode `hash`; `kept` is None for an agent that keeps only hashes).

        A decision that is unknown, another organization's or another agent's is
        `AgentNotFoundError`, one answer for all three."""
        with self._db.session(exclusive=True) as db:
            item = _decision(db, decision_id)
            if item is None or item["agent_id"] != agent_id:
                raise AgentNotFoundError(f"no decision {decision_id} of {agent_id}")
            events = _events_of(db, [decision_id])[decision_id]
            asked = ask(item, events)
            for action in actions.state(events):
                same = action["name"] == asked.name and action["arguments_hash"] == arguments_hash
                if same and (asked.name is not None or action["reason"] == asked.reason):
                    return {**_asked(decision_id, action), "repeated": True}
            epoch = self._mark_hash_epoch(db) if self.phi.hash_key else None
            payload: dict[str, Any] = {
                "action_id": actions.new_id(),
                "name": asked.name,
                "effect": asked.effect,
                "disposition": asked.disposition,
                "reason": asked.reason,
                "arguments_hash": arguments_hash,
                "source": "request",
                "slot": None,
            }
            if asked.free_text:  # as `ActionResult.chained`: only then
                payload["free_text"] = True
            requester = tenancy.requester()
            if requester is not None:
                payload["requested_by"] = requester
            keep = kept is not None and self.phi.mode != "hash"
            payload["note_ref"] = (
                self._keep_note(db, decision_id, "action.proposed", kept) if keep else None
            )
            self._append(
                db,
                decision_id,
                "action.proposed",
                self._bound(payload, item["org"]),
            )
        if epoch is not None:  # committed: this process need not look again
            self._hash_epoch = epoch
        shown = {**payload, "id": payload["action_id"]}
        return {**_asked(decision_id, shown), "repeated": False}

    def record_outcome(
        self,
        decision_id: str,
        *,
        final_label: str,
        source: str,
        recorded_by: str,
        labels: QuestionLabels | None = None,
    ) -> None:
        """What really happened downstream (e.g. staff re-routed an AUTO message, or where a
        reviewed message was finally handled), kept as the decision's `observed_label`: a
        reviewer's `final_label`, `human_override`, `reviewer` and `resolved_at` never change
        after the review. A later outcome replaces an earlier one; the chain keeps every one.
        Observed accuracy (`stats`, monitoring) measures the recommendation against the outcome
        when there is one, else against the reviewer's label. A decision still awaiting review
        is resolved first.

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
                f"SELECT review_status, agent_id, record, org FROM decisions d "
                f"WHERE id = ? AND {scope}",
                (decision_id, *scoped),
            )
            if row is None:
                raise ReviewError(f"unknown decision {decision_id}")
            if row["review_status"] == "pending":
                raise ReviewError("resolve the pending review instead of recording an outcome")
            _check_label(row["agent_id"], json.loads(row["record"]), final_label, labels)
            db.all(
                "UPDATE decisions SET observed_label = ? WHERE id = ?", (final_label, decision_id)
            )
            payload = {"final_label": final_label, "source": source, "recorded_by": recorded_by}
            chained = self._bound(payload, row["org"])
            self._append(db, decision_id, "outcome.recorded", chained)

    def record_card_feedback(
        self, agent_id: str, service: str, items: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """What clinicians did with CDS Hooks cards (`POST /cds-services/{id}/feedback`, LP-23f),
        one `card.feedback` event per item, all or none, in one transaction. An item is its card
        (the card's uuid is its decision's id) and what the feedback says, as codes and ids:
        `outcome`, `accepted_suggestions`, `override_reason` (`{system, code}` or None) and
        `outcome_timestamp`. Each chained event also names the `service` and who sent the
        feedback (`reporter`, `tenancy.requester`, None in open mode), never a comment.

        Every card must be a decision of `agent_id` in the organization this request acts for
        and, when the request names who asks (`tenancy.requester`: sign-in on, or a CDS client's
        verified token), one that same caller asked for (its chained `requested_by`). Otherwise
        `UnknownCard`, one refusal whichever it is, naming the cards as sent, and nothing is
        chained. The events, as chained."""
        cards = [str(item["card"]) for item in items]
        if not cards:
            return []
        unique_cards = list(dict.fromkeys(cards))
        requester = tenancy.requester()
        with self._db.session(exclusive=True) as db:
            scope, scoped = _scope()
            marks = ", ".join("?" for _ in unique_cards)
            rows = {
                row["id"]: row
                for row in db.all(
                    f"SELECT id, org, agent_id FROM decisions d WHERE id IN ({marks}) AND {scope}",
                    (*unique_cards, *scoped),
                )
                if row["agent_id"] == agent_id
            }
            if requester is not None and rows:
                asked = {
                    row["decision_id"]: json.loads(row["payload"]).get("requested_by")
                    for row in db.all(
                        "SELECT decision_id, payload FROM audit_events "
                        f"WHERE event = 'decision.created' AND decision_id IN ({marks})",
                        tuple(unique_cards),
                    )
                }
                rows = {id_: row for id_, row in rows.items() if asked.get(id_) == requester}
            unknown = list(dict.fromkeys(card for card in cards if card not in rows))
            if unknown:
                raise UnknownCard(f"no card of this service: {', '.join(unknown)}")
            entries = []
            for item in items:
                card = str(item["card"])
                payload = {
                    "service": service,
                    "outcome": item["outcome"],
                    "accepted_suggestions": list(item.get("accepted_suggestions") or []),
                    "override_reason": item.get("override_reason"),
                    "outcome_timestamp": item["outcome_timestamp"],
                    "reporter": requester,
                }
                entries.append((card, self._bound(payload, rows[card]["org"])))
            return self._append_many(db, "card.feedback", entries)

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
                f"SELECT agent_id, record, org FROM decisions d WHERE id = ? AND {scope}",
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
                "INSERT INTO feedback (id, decision_id, created_at, agent_id, rating, reporter, "
                "expected_question, expected_label) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    feedback_id,
                    decision_id,
                    created_at,
                    row["agent_id"],
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
            chained = self._bound(payload, row["org"])
            self._append(db, decision_id, "feedback.recorded", chained)
        return {
            "id": feedback_id,
            "decision_id": decision_id,
            "created_at": created_at,
            **payload,
            "note": note,
        }

    def save_report(
        self,
        report: Mapping[str, Any],
        *,
        ledger: bytes | None = None,
        usage: Mapping[str, Metered] | None = None,
    ) -> None:
        """Keep an evaluation report, and the ledger its result is computed from, for the
        organization; chained as `eval.recorded` with ids and hashes only (which report, result
        and ledger, of which agent digest and dataset). The run's remote calls (`usage`) go to
        the usage ledger in the same transaction, as the organization's evaluations."""
        run = report["run"]
        org = tenancy.current() or tenancy.DEFAULT_ORG
        recorded = {
            "report": run["id"],
            "result_sha256": report.get("result_sha256"),
            "ledger": (report.get("ledger") or {}).get("id"),
            "agent_id": report["agent"]["id"],
            "agent_digest": report["agent"].get("digest"),
            "dataset_sha256": report["dataset"].get("sha256"),
            "org": org,
        }
        with self._db.session(exclusive=True) as db:
            self._insert_report(db, report, run, recorded, org, ledger, usage)

    def _insert_report(
        self,
        db: _Session,
        report: Mapping[str, Any],
        run: Mapping[str, Any],
        recorded: Mapping[str, Any],
        org: str,
        ledger: bytes | None,
        usage: Mapping[str, Metered] | None,
    ) -> None:
        """`save_report`'s writes, in the caller's transaction (an agent store's job writes its
        report with its own state, LP-20g)."""
        db.all(
            "INSERT INTO eval_reports (id, created_at, agent_id, dataset, report, org, ledger) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run["id"],
                run["created_at"],
                report["agent"]["id"],
                report["dataset"]["name"],
                _canonical(report),
                org,
                None if ledger is None else ledger.decode("ascii"),
            ),
        )
        self._append(db, f"eval:{run['id']}", "eval.recorded", recorded)
        _add_usage(db, org, "evaluation", usage or {})

    def report_record(self, report: Mapping[str, Any], org: str) -> dict[str, Any]:
        """What `eval.recorded` chains of a report, as `save_report` builds it."""
        run = report["run"]
        return {
            "report": run["id"],
            "result_sha256": report.get("result_sha256"),
            "ledger": (report.get("ledger") or {}).get("id"),
            "agent_id": report["agent"]["id"],
            "agent_digest": report["agent"].get("digest"),
            "dataset_sha256": report["dataset"].get("sha256"),
            "org": org,
        }

    def usage_ledger(
        self, org: str | None = None, *, since: str | None = None, until: str | None = None
    ) -> list[dict[str, Any]]:
        """The usage ledger (LP-21g): remote calls by day, organization, provider type and
        purpose (`USAGE_PURPOSES`), of `org` if given (None: every organization), from `since`
        to `until` (local calendar days, ISO dates, both included). Each row has its calls, the
        calls that were priced, their tokens, and `cost_usd`: the cost, or None (unknown)
        unless every call was priced, never a partial sum."""
        where, params = ["1 = 1"], []
        for clause, value in (("org = ?", org), ("day >= ?", since), ("day <= ?", until)):
            if value is not None:
                where.append(clause)
                params.append(value)
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT day, org, provider_type, purpose, calls, priced_calls, input_tokens, "
                f"output_tokens, cost_usd FROM usage_ledger WHERE {' AND '.join(where)} "
                "ORDER BY day, org, provider_type, purpose",
                tuple(params),
            )
        return [
            {
                **{key: value for key, value in row.items() if key != "cost_usd"},
                "cost_usd": row["cost_usd"] if row["priced_calls"] == row["calls"] else None,
            }
            for row in rows
        ]

    def consume(self, provider: str, *, daily_limit: int | None) -> None:
        """Count one call to a metered provider; raise `budget_exhausted` past the daily limit
        (None: count without a limit). Days are local calendar days (`core.clock`)."""
        if daily_limit is not None and daily_limit < 1:  # admits nothing, and writes nothing
            raise ProviderError("budget_exhausted", f"daily {provider} limit of {daily_limit}")
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
        if daily_limit < 1:  # admits nothing, and writes nothing (the INSERT would count one)
            return None
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

    # -- alert deliveries (mendari.alerts): outside the chain, ids and counts only -------------

    def record_alert(self, delivery: Mapping[str, Any]) -> None:
        """Record one alert sent to the webhook, as `mendari.alerts.deliver` left it."""
        columns = (
            "id", "created_at", "updated_at", "event", "org", "body", "status", "attempts",
            "next_attempt_at", "last_status", "last_error",
        )  # fmt: skip
        with self._db.session(exclusive=False) as db:
            db.all(
                f"INSERT INTO alert_deliveries ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                tuple(delivery[column] for column in columns),
            )

    def alerts_due(self, now: str, limit: int) -> list[dict[str, Any]]:
        """The pending alerts whose next try is due at `now` (ISO), oldest first."""
        with self._db.session(exclusive=False) as db:
            return db.all(
                "SELECT id, event, body, attempts FROM alert_deliveries "
                "WHERE status = 'pending' AND next_attempt_at <= ? "
                "ORDER BY next_attempt_at LIMIT ?",
                (now, limit),
            )

    def update_alert(
        self,
        delivery: str,
        *,
        status: str,
        attempts: int,
        updated_at: str,
        next_attempt_at: str | None,
        last_status: int | None,
        last_error: str | None,
    ) -> None:
        with self._db.session(exclusive=False) as db:
            db.all(
                "UPDATE alert_deliveries SET status = ?, attempts = ?, updated_at = ?, "
                "next_attempt_at = ?, last_status = ?, last_error = ? WHERE id = ?",
                (status, attempts, updated_at, next_attempt_at, last_status, last_error, delivery),
            )

    def alert_deliveries(self, limit: int = 50) -> list[dict[str, Any]]:
        """The newest alerts sent, for the operator (and the tests)."""
        with self._db.session(exclusive=False) as db:
            return db.all(
                "SELECT * FROM alert_deliveries ORDER BY created_at DESC LIMIT ?", (limit,)
            )

    def last_alerts(self) -> dict[str, dict[str, Any] | None]:
        """For the operator's checks: the newest alert sent (`last`) and the newest one the
        webhook took (`delivered`), each as its event, status, when it was last tried, the
        webhook's HTTP status and the fixed error code. Never a body, a URL or a secret. None
        for none."""
        columns = "event, status, updated_at, last_status, last_error"
        with self._db.session(exclusive=False) as db:
            last = db.one(
                f"SELECT {columns} FROM alert_deliveries ORDER BY updated_at DESC LIMIT 1"
            )
            delivered = db.one(
                f"SELECT {columns} FROM alert_deliveries WHERE status = 'delivered' "
                "ORDER BY updated_at DESC LIMIT 1"
            )
        return {"last": last, "delivered": delivered}

    # -- the operator's checks (`server/system.py`): outside the chain, times and codes only ----

    def record_run(self, job: str, outcome: str, detail: str | None = None) -> dict[str, Any]:
        """Record that `job` (one of `RUN_JOBS`) just ran and how it ended (`RUN_OUTCOMES`),
        over its last run: the operator's checks show each job's last run, never a history
        (what must last is in the chain: a purge's summary, a checkpoint). `detail` is a fixed
        code (a verification's reason, a job's step), at most 64 characters."""
        if job not in RUN_JOBS:
            raise ValueError(f"no job named {job!r} (one of {', '.join(RUN_JOBS)})")
        if outcome not in RUN_OUTCOMES:
            raise ValueError(f"a run ends {' or '.join(RUN_OUTCOMES)}")
        if detail is not None and not re.fullmatch(r"[a-z0-9_.:-]{1,64}", detail):
            raise ValueError("a run's detail is a code: lower-case letters, digits, _ . : -")
        run = {"job": job, "ran_at": _now(), "outcome": outcome, "detail": detail}
        with self._db.session(exclusive=False) as db:
            db.all(
                "INSERT INTO system_runs (job, ran_at, outcome, detail) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (job) DO UPDATE SET ran_at = excluded.ran_at, "
                "outcome = excluded.outcome, detail = excluded.detail",
                tuple(run.values()),
            )
        return run

    def runs(self) -> dict[str, dict[str, Any]]:
        """Each job's last recorded run, by job (`record_run`)."""
        with self._db.session(exclusive=False) as db:
            rows = db.all("SELECT job, ran_at, outcome, detail FROM system_runs")
        return {str(row["job"]): dict(row) for row in rows}

    def any_decision(self) -> bool:
        """Whether this deployment has ever made a decision, for any organization: what tells a
        fresh install from one in use (the operator's checks). One indexed read, no count."""
        with self._db.session(exclusive=False) as db:
            return db.one("SELECT id FROM decisions LIMIT 1") is not None

    def last_checkpoint(self) -> dict[str, Any] | None:
        """The newest recorded checkpoint (`record_checkpoint`): its event's place, its hash and
        when it was recorded; None without one."""
        with self._db.session(exclusive=False) as db:
            return db.one(
                "SELECT seq, hash, created_at FROM audit_checkpoints ORDER BY seq DESC LIMIT 1"
            )

    def consume_principal(self, principal: str, *, daily_limit: int, cost: int = 1) -> int | None:
        """Count `cost` decisions for `principal` (its opaque id, `Principal.id`) today, all or
        none: the count it reached, or None when they would pass its daily cap (nothing is
        counted, nor written). One statement, as `consume_org`: instances never count past the
        cap together. Not part of the audit chain."""
        if cost < 1:
            raise ValueError("cost must be at least 1")
        if cost > daily_limit:  # a cap of 0 admits nothing, and writes nothing
            return None
        with self._db.session(exclusive=False) as db:
            row = db.one(
                "INSERT INTO principal_usage (day, principal, count) VALUES (?, ?, ?) "
                "ON CONFLICT (day, principal) DO UPDATE SET count = principal_usage.count + ? "
                "WHERE principal_usage.count + ? <= ? RETURNING count",
                (clock.today().isoformat(), principal, cost, cost, cost, daily_limit),
            )
        return None if row is None else int(row["count"])

    def principal_usage(self, principal: str) -> int:
        """The decisions `principal` asked for today."""
        with self._db.session(exclusive=False) as db:
            row = db.one(
                "SELECT count FROM principal_usage WHERE day = ? AND principal = ?",
                (clock.today().isoformat(), principal),
            )
        return int(row["count"]) if row else 0

    def org_usage(self, org: str) -> dict[str, int]:
        """What `org` has used today, by metric."""
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT metric, count FROM org_usage WHERE day = ? AND org = ?",
                (clock.today().isoformat(), org),
            )
        return {row["metric"]: int(row["count"]) for row in rows}

    # -- rate limits: `RateBuckets` (`core/rate_buckets.py`) ---------------------------------

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
        insert's order, as an identity column does. The events, as `_events_of` gives them.

        Every kind but the deployment's names its organization (`_bound`): a writer that
        forgets is refused here, before anything is chained, so a new kind cannot leave it out.
        So is a session that is not exclusive (`db.Session.exclusive`): only the chain's lock
        keeps the head read here the head the insert follows."""
        if not entries:
            return []
        # A separator in either would let two different events hash alike (`_chain_hash`).
        if _SEPARATOR in event or any(_SEPARATOR in decision_id for decision_id, _ in entries):
            raise ValueError("an event's kind or subject never holds the chain's separator")
        if not db.exclusive:
            # Outside the chain's lock two writers read the same head and fork the chain.
            raise RuntimeError("the audit chain is appended to in an exclusive session only")
        if event not in DEPLOYMENT_KINDS:
            for _, payload in entries:
                if not (isinstance(payload, Mapping) and "org" in payload):
                    raise ValueError(f"a {event} event names its organization: chain it bound")
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
        self._refuse_plaintext(db)  # under `off` a note is kept as it is (on the open session)
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
        exports still hold it (docs/deploying-with-phi.md). Another organization's note is
        refused as an unknown one is (through its decision, as `erase_input`)."""
        if reason not in ERASE_REASONS:
            raise NoteError(f"the reason is one of {ERASE_REASONS}")
        if not SYSTEM_ID.fullmatch(erased_by):
            raise NoteError("who erases is an identifier, such as an email address")
        scope, scoped = _scope()
        with self._db.session(exclusive=True) as db:
            row = db.one(
                "SELECT n.decision_id, n.sealed, d.org FROM notes n "
                f"LEFT JOIN decisions d ON d.id = n.decision_id WHERE n.id = ? AND {scope}",
                (note_id, *scoped),
            )
            if row is None or row["sealed"] is None:
                raise NoteError(f"no note {note_id} to erase")
            erased_at = _now()
            db.all(
                "UPDATE notes SET sealed = NULL, erased_at = ?, erased_by = ? WHERE id = ?",
                (erased_at, erased_by, note_id),
            )
            payload = {"note_id": note_id, "reason": reason, "erased_by": erased_by}
            chained = self._bound(payload, row["org"])
            self._append(db, row["decision_id"], "note.erased", chained)
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
                    "SELECT n.id, n.decision_id, d.org FROM notes n "
                    "LEFT JOIN decisions d ON d.id = n.decision_id "
                    "WHERE n.created_at < ? AND n.sealed IS NOT NULL "
                    "ORDER BY n.created_at, n.id LIMIT ?",
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
                    (
                        row["decision_id"],
                        self._bound({"note_id": row["id"], **retention}, row["org"]),
                    )
                    for row in doomed
                ]
                self._append_many(db, "note.erased", erased)
            total += len(doomed)

    # -- reads ----------------------------------------------------------------------------

    def get(self, decision_id: str, *, reader: str | None = None) -> dict[str, Any] | None:
        """A decision with its events; under PHI mode `encrypt`, its input only for a `reader`,
        and its events' notes as `reveal_inputs` shows them (the read it chains is the last
        event). A decision with proposed actions (spec §13.7) has `actions`: each as it stands
        (`actions.state`), its arguments as its event's note is shown."""
        with self._db.session(exclusive=False) as db:
            item = _decision(db, decision_id)
            if item is not None:
                item["events"] = _events_of(db, [decision_id])[decision_id]
        if item is None:
            return None
        self.reveal_inputs([item], reader)
        _with_actions(item)
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
        """One chained event of a decision, with its decision's agent and record (as `events()`
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
        item gets `note` (the text, or None) and `note_status` ("open", "sealed" or "erased").
        A decision whose input or notes a `reader` opened gets one `input.read` (naming the
        notes), all chained with one append and added to the item's `events`; each item of such
        a decision gets `input_read: true`. Outside mode `off`, what a record holds itself
        (`_held_input`) is shown as a kept input is."""
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
        # A record that holds its input itself (kept under mode `off`, with its answers'
        # evidence): outside `off`, only a reader sees it (and the read is chained), as with a
        # sealed input.
        held = {
            str(item[id_key])
            for item in items
            if gated and str(item[id_key]) not in opened and _held_input(item)
        }
        if reader:
            for decision_id in held:
                reads.setdefault(decision_id, [])
        chained: list[dict[str, Any]] = []
        if reads:
            marks = ", ".join("?" for _ in reads)
            with self._db.session(exclusive=True) as db:
                orgs = {
                    row["id"]: row["org"]
                    for row in db.all(
                        f"SELECT id, org FROM decisions WHERE id IN ({marks})", list(reads)
                    )
                }
                entries = [
                    (
                        decision_id,
                        self._bound(
                            {"reader": reader, **({"notes": named} if named else {})},
                            orgs.get(decision_id),
                        ),
                    )
                    for decision_id, named in reads.items()
                ]
                chained = self._append_many(db, "input.read", entries)
        for item in items:
            decision_id = str(item[id_key])
            if decision_id in reads:  # shown to a reader, and chained (`input.read`)
                item["input_read"] = True
            if decision_id in opened:
                item["record"]["input"] = opened[decision_id]
                item["input_restricted"] = False
                _give_evidence(item["record"], details.get(decision_id, {}).get("evidence", {}))
            elif decision_id in sealed or decision_id in held:
                # A sealed input not opened stays sealed; what the record holds itself is shown
                # to a reader (the read chained above) and to no one else.
                item["input_restricted"] = decision_id in sealed or not reader
                if decision_id in held and not reader:
                    _withhold(item["record"])
            for event in item.get("events") or ():
                _show_note(event, views)
            if own_notes:
                _show_own_note(item, views)
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
        untouched, so `audit verify` still passes. What the chained record holds itself (an
        input kept under PHI mode `off`, with its answers' evidence) nothing erases without
        breaking the chain: `still_in_chain` says so, and retiring that database removes it
        (docs/deploying-with-phi.md). Backups, the database's history and earlier exports still
        hold what was erased."""
        if reason not in ERASE_REASONS:
            raise NoteError(f"the reason is one of {ERASE_REASONS}")
        if not erased_by.strip() or len(erased_by) > 200 or not erased_by.isprintable():
            raise NoteError("who erases is a name or identifier of at most 200 characters")
        with self._db.session(exclusive=True) as db:
            item = _decision(db, decision_id)
            if item is None:
                raise NoteError(f"no decision {decision_id}")
            in_chain = _held_input(item)
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
                        "record holds its input and evidence, which only retiring the database "
                        "removes (docs/deploying-with-phi.md)"
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
            org = item["org"]
            self._append_many(
                db,
                "note.erased",
                [(decision_id, self._bound({"note_id": note, **who}, org)) for note in notes],
            )
            if kept is not None:
                self._append(db, decision_id, "input.erased", self._bound(who, org))
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
        written = self.purge_written(older_than_days)
        summary = {
            "retention_days": older_than_days,
            "inputs": inputs,
            "notes": notes,
            # Written outputs kept to grade (`purge_written`), counted only where there were any.
            **({"written": written} if written else {}),
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

    def purge_written(self, older_than_days: int) -> int:
        """Retention, with inputs and notes (`purge`): erase the written outputs a grading round
        still keeps (`written_items.body`: a text and its context) that are older than N days
        (0: all of them), a round at a time, each round's erasure chained (`written.erased`: the
        round, how many, never an item's text). Closing a round erases its texts at once
        (`core/written.py`); this is the backstop for a round left open. An item whose text is
        gone is no longer shown to its grader."""
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat()
        total = 0
        while True:
            with self._db.session(exclusive=True) as db:
                doomed = db.all(
                    "SELECT id, round_id, org FROM written_items WHERE created_at < ? "
                    "AND body IS NOT NULL ORDER BY round_id, id LIMIT ?",
                    (cutoff, PURGE_BATCH),
                )
                if not doomed:
                    return total
                marks = ", ".join("?" for _ in doomed)
                db.all(
                    f"UPDATE written_items SET body = NULL, erased_at = ? WHERE id IN ({marks})",
                    (_now(), *(row["id"] for row in doomed)),
                )
                by_round: dict[str, tuple[str, int]] = {}
                for row in doomed:
                    org, count = by_round.get(str(row["round_id"]), (str(row["org"]), 0))
                    by_round[str(row["round_id"])] = (org, count + 1)
                erased = [
                    (
                        f"written:{round_id}",
                        self._bound(
                            {
                                "round": round_id,
                                "erased": count,
                                "reason": "retention",
                                "retention_days": older_than_days,
                            },
                            org,
                        ),
                    )
                    for round_id, (org, count) in sorted(by_round.items())
                ]
                self._append_many(db, "written.erased", erased)
            total += len(doomed)

    def purge_inputs(self, older_than_days: int) -> int:
        """Retention: delete kept (encrypted) inputs older than N days (0: all of them), in
        batches of `PURGE_BATCH`, each chained with its `input.purged` events; the chain is
        untouched. Notes have their own purge (`purge_notes`); `purge` runs both."""
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat()
        purged = {"retention_days": older_than_days}
        total = 0
        while True:
            with self._db.session(exclusive=True) as db:
                rows = db.all(
                    "SELECT i.decision_id, d.org FROM decision_inputs i "
                    "LEFT JOIN decisions d ON d.id = i.decision_id WHERE i.created_at < ? "
                    "ORDER BY i.created_at, i.decision_id LIMIT ?",
                    (cutoff, PURGE_BATCH),
                )
                doomed = [row["decision_id"] for row in rows]
                if not doomed:
                    return total
                marks = ", ".join("?" for _ in doomed)
                db.all(f"DELETE FROM decision_inputs WHERE decision_id IN ({marks})", doomed)
                self._append_many(
                    db,
                    "input.purged",
                    [
                        (
                            row["decision_id"],
                            self._bound(purged, row["org"]),
                        )
                        for row in rows
                    ],
                )
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
        self._rotate_datasets(prefix, batch, summary)
        self._rotate_connections(prefix, batch, summary)
        self._rotate_written(prefix, batch, summary)
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

    def _rotate_datasets(self, prefix: str, batch: int, summary: dict[str, Any]) -> None:
        """An agent store's datasets sealed under another key (LP-20g), re-sealed as notes are."""
        after = ""
        while True:
            with self._db.session(exclusive=False) as db:
                rows = db.all(
                    "SELECT id, data FROM store_datasets WHERE id > ? AND sealed = 1 "
                    "AND substr(data, 1, 20) <> ? ORDER BY id LIMIT ?",
                    (after, prefix, batch),
                )
            if not rows:
                return
            after = rows[-1]["id"]
            changes = []
            for row in rows:
                try:
                    text, opened = self.phi.open("dataset", row["data"], row["id"])
                except MendariError:
                    summary["unopenable"].append(
                        {"kind": "dataset", "id": row["id"], "key": token_key(row["data"])}
                    )
                    continue
                changes.append((row, self.phi.seal("dataset", text, row["id"]), opened))
            if not changes:
                continue
            moved: Counter[str] = Counter()
            datasets = 0
            with self._db.session(exclusive=True) as db:
                for row, sealed, opened in changes:
                    if db.all(
                        "UPDATE store_datasets SET data = ? WHERE id = ? AND data = ? RETURNING id",
                        (sealed, row["id"], row["data"]),
                    ):
                        datasets += 1
                        moved[opened] += 1
                if datasets:
                    self._chain_rotation(db, moved, summary["to"], datasets=datasets)
            summary["datasets"] = summary.get("datasets", 0) + datasets
            summary["from"].update(moved)
            summary["batches"] += bool(datasets)

    def _rotate_connections(self, prefix: str, batch: int, summary: dict[str, Any]) -> None:
        """Agents' connection tokens sealed under another key (ADR-0021), re-sealed as notes
        are: each bound to its row, its organization and its agent, as it was."""
        after = ""
        while True:
            with self._db.session(exclusive=False) as db:
                rows = db.all(
                    "SELECT id, org, agent_id, token FROM agent_connections WHERE id > ? "
                    "AND token IS NOT NULL AND substr(token, 1, 20) <> ? ORDER BY id LIMIT ?",
                    (after, prefix, batch),
                )
            if not rows:
                return
            after = rows[-1]["id"]
            changes = []
            for row in rows:
                ids = (str(row["id"]), str(row["org"]), str(row["agent_id"]))
                try:
                    token, opened = self.phi.open("connection", row["token"], *ids)
                except MendariError:
                    summary["unopenable"].append(
                        {"kind": "connection", "id": row["id"], "key": token_key(row["token"])}
                    )
                    continue
                changes.append((row, self.phi.seal("connection", token, *ids), opened))
            if not changes:
                continue
            moved: Counter[str] = Counter()
            connections = 0
            with self._db.session(exclusive=True) as db:
                for row, sealed, opened in changes:
                    if db.all(
                        "UPDATE agent_connections SET token = ? WHERE id = ? AND token = ? "
                        "RETURNING id",
                        (sealed, row["id"], row["token"]),
                    ):  # neither replaced nor cleared since it was read
                        connections += 1
                        moved[opened] += 1
                if connections:
                    self._chain_rotation(db, moved, summary["to"], connections=connections)
            summary["connections"] = summary.get("connections", 0) + connections
            summary["from"].update(moved)
            summary["batches"] += bool(connections)

    def _rotate_written(self, prefix: str, batch: int, summary: dict[str, Any]) -> None:
        """Written outputs a grading round keeps, sealed under another key (ADR-0025), re-sealed
        as notes are: each bound to its item row's id, as it was."""
        after = ""
        while True:
            with self._db.session(exclusive=False) as db:
                rows = db.all(
                    "SELECT id, body FROM written_items WHERE id > ? AND sealed = 1 "
                    "AND body IS NOT NULL AND substr(body, 1, 20) <> ? ORDER BY id LIMIT ?",
                    (after, prefix, batch),
                )
            if not rows:
                return
            after = rows[-1]["id"]
            changes = []
            for row in rows:
                try:
                    body, opened = self.phi.open("written", row["body"], row["id"])
                except MendariError:
                    summary["unopenable"].append(
                        {"kind": "written", "id": row["id"], "key": token_key(row["body"])}
                    )
                    continue
                changes.append((row, self.phi.seal("written", body, row["id"]), opened))
            if not changes:
                continue
            moved: Counter[str] = Counter()
            written = 0
            with self._db.session(exclusive=True) as db:
                for row, sealed, opened in changes:
                    if db.all(
                        "UPDATE written_items SET body = ? WHERE id = ? AND body = ? RETURNING id",
                        (sealed, row["id"], row["body"]),
                    ):  # neither erased nor re-sealed since it was read
                        written += 1
                        moved[opened] += 1
                if written:
                    self._chain_rotation(db, moved, summary["to"], written=written)
            summary["written"] = summary.get("written", 0) + written
            summary["from"].update(moved)
            summary["batches"] += bool(written)

    def _chain_rotation(
        self,
        db: _Session,
        moved: Counter[str],
        to: str,
        *,
        inputs: int = 0,
        details: int = 0,
        notes: int = 0,
        datasets: int = 0,
        connections: int = 0,
        written: int = 0,
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
            **({"datasets": datasets} if datasets else {}),
            **({"connections": connections} if connections else {}),
            **({"written": written} if written else {}),
        }
        self._append(db, KEYS_CHAIN, "phi.rotated", payload)

    def phi_check(self, *, batch: int = 500) -> dict[str, Any]:
        """Reads only (`mendari phi rotate --check`, on a store opened with `marks=False`): whether
        every sealed value opens with the current key alone, as dropping the read keys
        requires. Counts sealed values by the key each names (`invalid`: a value that names
        none), with the newest under each other key; opens every kept input, its details and
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
                try:
                    salt = base64.b64decode(str(body.get("salt", "")), validate=True)
                except (binascii.Error, ValueError):
                    failures.append({"kind": "note", "id": row["id"], "key": "commitment"})
                    continue
                if (
                    row["id"] in refs
                    and _commitment(salt, str(body.get("text"))) != refs[row["id"]]
                ):
                    failures.append({"kind": "note", "id": row["id"], "key": "commitment"})
        # An agent store's sealed datasets (LP-20g), counted only where there are any.
        with self._db.session(exclusive=False) as db:
            sealed_sets = db.all("SELECT id, created_at, data FROM store_datasets WHERE sealed = 1")
        for row in sealed_sets:
            by_key.setdefault("datasets", Counter())
            named = seen("datasets", row["data"], row["created_at"])
            try:
                alone.open("dataset", row["data"], row["id"])
            except MendariError:
                failures.append({"kind": "dataset", "id": row["id"], "key": named})
        # Agents' connection tokens (ADR-0021), counted only where there are any.
        with self._db.session(exclusive=False) as db:
            sealed_tokens = db.all(
                "SELECT id, org, agent_id, token, token_set_at FROM agent_connections "
                "WHERE token IS NOT NULL"
            )
        for row in sealed_tokens:
            by_key.setdefault("connections", Counter())
            named = seen("connections", row["token"], row["token_set_at"] or "")
            try:
                alone.open(
                    "connection",
                    row["token"],
                    str(row["id"]),
                    str(row["org"]),
                    str(row["agent_id"]),
                )
            except MendariError:
                failures.append({"kind": "connection", "id": row["id"], "key": named})
        # Written outputs a grading round keeps (ADR-0025), counted only where there are any.
        with self._db.session(exclusive=False) as db:
            sealed_texts = db.all(
                "SELECT id, created_at, body FROM written_items "
                "WHERE sealed = 1 AND body IS NOT NULL"
            )
        for row in sealed_texts:
            by_key.setdefault("written", Counter())
            named = seen("written", row["body"], row["created_at"])
            try:
                alone.open("written", row["body"], row["id"])
            except MendariError:
                failures.append({"kind": "written", "id": row["id"], "key": named})
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
        agent_id: str | None = None,
        outcome: str | None = None,
        review_status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        scope, scoped = _scope()
        clauses, params = [scope], list(scoped)
        for column, value in (
            ("agent_id", agent_id),
            ("outcome", outcome),
            ("review_status", review_status),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}"
        order = (
            f"{_PRIORITY_ORDER}, created_at, id"
            if review_status == "pending"
            else "created_at DESC, id DESC"
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
        agent_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        after_seq: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Chained events in order, each with its decision's agent and record, for exports. Pages
        by `after_seq` (the last `seq` returned); `since` / `until` bound `created_at` (ISO)."""
        scope, scoped = _scope()
        clauses: list[str] = ["e.seq > ?", scope]
        params: list[Any] = [after_seq, *scoped]
        for clause, value in (
            ("d.agent_id = ?", agent_id),
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
    # delete is scoped to its kind. Rows expire; each put and each claim purges up to 100 expired
    # ones. Not part of the audit chain, so no operation here takes the chain's lock: a take is
    # one `DELETE ... RETURNING`, a claim one `INSERT ... ON CONFLICT`, each atomic on its own.
    # Rows of `SEALED_KINDS` are sealed under `secret` (by default the row's own value): a row
    # that does not open (another row's data, a changed byte, a row that is not sealed) is
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
        """Replace a live row's data and expiry (a person's session that now acts for another
        organization); an ended row stays ended. True if replaced."""
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

    def end_sessions(self, group: str, *, ended_by: str, reason: str) -> dict[str, Any]:
        """The break-glass (`mendari access end-sessions`): delete every row of a group's kinds
        (`SESSION_GROUPS`, or `all` of them), so the next request with one of their cookies, on
        any instance, finds none and is signed out (nothing is cached in memory). Chained in the
        same transaction as `sessions.ended` (the kinds, how many live ones ended, who and why)
        under `SESSIONS_CHAIN`. Claims (`jti`, `cds-jti`) are never touched. A member's and a
        launch's rows are sealed under their cookie, so whose they are cannot be read here: a
        whole kind."""
        if group != "all" and group not in SESSION_GROUPS:
            raise ValueError(f"end the sessions of one of {sorted(SESSION_GROUPS)} or all")
        why = break_glass_reason(reason)
        chosen = SESSION_GROUPS.values() if group == "all" else [SESSION_GROUPS[group]]
        kinds = sorted({kind for kinds in chosen for kind in kinds})
        marks = ", ".join("?" for _ in kinds)
        with self._db.session(exclusive=True) as db:
            gone = db.all(
                f"DELETE FROM smart_sessions WHERE kind IN ({marks}) RETURNING expires_at",
                tuple(kinds),
            )
            now = int(time.time())
            ended = {
                "kinds": kinds,
                "ended": sum(1 for row in gone if int(row["expires_at"]) > now),
                "ended_by": ended_by,
                "ended_at": _now(),
                "reason": why,
            }
            self._append(db, SESSIONS_CHAIN, "sessions.ended", ended)
        return ended

    def smart_claim(self, kind: str, value: str, expires_at: int) -> bool:
        """Claim `value` once until `expires_at` (e.g. an assertion's `jti`, or a CDS client's
        token's): True for the first claim, False while that claim lasts; an expired claim can be
        made again. One statement, so concurrent claims through any number of connections or
        instances have one winner. Each claim first deletes up to `SESSION_PURGE_BATCH` expired
        rows, as each put does: a CDS client's every call leaves a claim, which would otherwise
        stay in the table for good."""
        row_key = _session_key(kind, value)
        now = int(time.time())
        if expires_at <= now:
            raise ValueError("a claim must expire in the future")
        with self._db.session(exclusive=False) as db:
            db.all(
                "DELETE FROM smart_sessions WHERE key_sha256 IN (SELECT key_sha256 FROM "
                "smart_sessions WHERE expires_at <= ? ORDER BY expires_at LIMIT ?)",
                (now, SESSION_PURGE_BATCH),
            )
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
    # A deploying organization's recorded decision to let a level 2-3 agent automate (ADR-0002):
    # bound to the agent's exact digest (a changed agent needs a new approval), with an expiry, per
    # organization, and chained like decisions ("governance:<id>" in the audit chain).

    def approve_automation(
        self,
        *,
        agent_id: str,
        agent_digest: str,
        risk_level: int,
        approver: str,
        reference: str,
        expires_at: str,
    ) -> dict[str, Any]:
        if risk_level not in (2, 3):
            reason = "is never automated" if risk_level >= 4 else "needs no approval"
            raise ReviewError(f"a risk level {risk_level} agent {reason}")
        if not approver.strip() or not reference.strip():
            raise ReviewError("an approval needs an approver and a reference")
        approval = {
            "id": secrets.token_hex(8),
            "org": tenancy.current() or tenancy.DEFAULT_ORG,
            "agent_id": agent_id,
            "agent_digest": agent_digest,
            "risk_level": risk_level,
            "approver": approver.strip(),
            "reference": reference.strip(),
            "created_at": _now(),
            "expires_at": expires_at,
        }
        with self._db.session(exclusive=True) as db:
            db.all(
                "INSERT INTO governance_approvals (id, org, agent_id, agent_digest, risk_level, "
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
        agent_id: str | None,
        agent_digest: str | None,
        reason: str,
        set_by: str,
    ) -> dict[str, Any]:
        """Stop automation (AUTO becomes HUMAN_REVIEW) for every decision a hold covers: an
        organization (None: every one), an agent (None: every agent) and a digest (None: every
        version). Chained as `automation.held`; it lasts until lifted."""
        reason = " ".join(reason.split())
        if not reason or len(reason) > 200:
            raise ReviewError("a hold needs a reason of at most 200 characters")
        if agent_digest is not None and agent_id is None:
            raise ReviewError("a hold on a digest names its agent")
        hold = {
            "id": secrets.token_hex(8),
            "org": org,
            "agent_id": agent_id,
            "agent_digest": agent_digest,
            "reason": reason,
            "set_by": set_by,
            "set_at": _now(),
        }
        with self._db.session(exclusive=True) as db:
            db.all(
                "INSERT INTO automation_holds (id, org, agent_id, agent_digest, reason, set_by, "
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
                f"SELECT id, org FROM automation_holds WHERE id = ? AND lifted_at IS NULL{scope}",
                (hold_id,) if org is None else (hold_id, org),
            )
            if row is None:
                return False
            lifted = {"id": hold_id, "lifted_by": lifted_by, "lifted_at": _now()}
            db.all(
                "UPDATE automation_holds SET lifted_by = ?, lifted_at = ? WHERE id = ?",
                (lifted["lifted_by"], lifted["lifted_at"], hold_id),
            )
            chained = self._bound(lifted, row["org"])  # NULL: every one's
            self._append(db, f"hold:{hold_id}", "automation.lifted", chained)
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

    def hold_for(self, agent_id: str, agent_digest: str, org: str) -> dict[str, Any] | None:
        """The first active hold that covers a decision of this agent, digest and organization."""
        with self._db.session(exclusive=False) as db:
            row = db.one(
                "SELECT * FROM automation_holds WHERE lifted_at IS NULL "
                "AND (org IS NULL OR org = ?) AND (agent_id IS NULL OR agent_id = ?) "
                "AND (agent_digest IS NULL OR agent_digest = ?) ORDER BY set_at, id LIMIT 1",
                (org, agent_id, agent_digest),
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
            payload = self._bound({"id": approval_id, "revoked_by": revoked_by}, org)
            self._append(db, f"governance:{approval_id}", "governance.revoked", payload)
        return True

    def approvals(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        org = tenancy.current() or tenancy.DEFAULT_ORG
        agents = ("AND agent_id = ?", (agent_id,)) if agent_id else ("", ())
        with self._db.session(exclusive=False) as db:
            return db.all(
                f"SELECT * FROM governance_approvals WHERE org = ? {agents[0]} "
                "ORDER BY created_at DESC",
                (org, *agents[1]),
            )

    # -- agents from a registry (LP-18d) ---------------------------------------------------------
    # A pulled agent never automates until its organization approves its exact digest here, at
    # any risk level (`core/decision.py`); a risk level 2 or 3 agent needs its governance approval
    # too. Each approval and revocation is chained under `registry_approval:<id>`.

    def approve_registry_agent(
        self, *, agent_id: str, agent_digest: str, registry: str, approver: str, reference: str
    ) -> dict[str, Any]:
        if not approver.strip() or not reference.strip():
            raise ReviewError("an approval needs an approver and a reference")
        approval = {
            "id": secrets.token_hex(8),
            "org": tenancy.current() or tenancy.DEFAULT_ORG,
            "agent_id": agent_id,
            "agent_digest": agent_digest,
            "registry": registry,
            "approver": approver.strip(),
            "reference": reference.strip(),
            "created_at": _now(),
        }
        with self._db.session(exclusive=True) as db:
            if db.one(
                "SELECT id FROM registry_approvals WHERE org = ? AND agent_id = ? "
                "AND agent_digest = ? AND revoked_at IS NULL",
                (approval["org"], agent_id, agent_digest),
            ):
                # One live approval a digest, so revoking it stops automation at once.
                raise ReviewError("this digest is approved already: revoke that approval first")
            db.all(
                "INSERT INTO registry_approvals (id, org, agent_id, agent_digest, registry, "
                "approver, reference, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(approval.values()),
            )
            self._append(
                db, f"registry_approval:{approval['id']}", "registry_agent.approved", approval
            )
        return approval

    def revoke_registry_approval(self, approval_id: str, *, revoked_by: str) -> bool:
        """Revoke this organization's approval of a pulled agent: it stops automating at once."""
        org = tenancy.current() or tenancy.DEFAULT_ORG
        with self._db.session(exclusive=True) as db:
            row = db.one(
                "SELECT id FROM registry_approvals WHERE id = ? AND org = ? AND revoked_at IS NULL",
                (approval_id, org),
            )
            if row is None:
                return False
            db.all(
                "UPDATE registry_approvals SET revoked_at = ?, revoked_by = ? WHERE id = ?",
                (_now(), revoked_by, approval_id),
            )
            payload = self._bound({"id": approval_id, "revoked_by": revoked_by}, org)
            self._append(db, f"registry_approval:{approval_id}", "registry_agent.revoked", payload)
        return True

    def registry_approvals(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        org = tenancy.current() or tenancy.DEFAULT_ORG
        agents = ("AND agent_id = ?", (agent_id,)) if agent_id else ("", ())
        with self._db.session(exclusive=False) as db:
            return db.all(
                f"SELECT * FROM registry_approvals WHERE org = ? {agents[0]} "
                "ORDER BY created_at DESC",
                (org, *agents[1]),
            )

    def registry_approval(
        self, agent_id: str, agent_digest: str, in_org: str | None = None
    ) -> dict[str, Any] | None:
        """The approval in force for exactly this pulled agent in one organization (`in_org`;
        None: this request's, never every one's), or None."""
        org = in_org or tenancy.current() or tenancy.DEFAULT_ORG
        with self._db.session(exclusive=False) as db:
            return db.one(
                "SELECT * FROM registry_approvals WHERE org = ? AND agent_id = ? "
                "AND agent_digest = ? AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 1",
                (org, agent_id, agent_digest),
            )

    def active_approval(self, agent_id: str, agent_digest: str) -> dict[str, Any] | None:
        """The approval in force for exactly this agent, for this request's organization."""
        org = tenancy.current() or tenancy.DEFAULT_ORG
        with self._db.session(exclusive=False) as db:
            return db.one(
                "SELECT * FROM governance_approvals WHERE org = ? AND agent_id = ? "
                "AND agent_digest = ? AND revoked_at IS NULL AND expires_at > ? "
                "ORDER BY created_at DESC LIMIT 1",
                (org, agent_id, agent_digest, _now()),
            )

    # -- the deployment registry -----------------------------------------------------------------
    # Which agent version runs where, with which providers, since when: one row each time that
    # changes (not each start, since a serverless platform starts the server many times a day),
    # chained like decisions ("deployment:<id>").

    def record_deployment(
        self,
        *,
        environment: str,
        release: str,
        agent_id: str,
        agent_version: str,
        agent_digest: str,
        providers: Mapping[str, str],
    ) -> dict[str, Any] | None:
        """Records that this agent runs here, unless the latest record for the environment already
        says so (same digest, providers and release). Returns the new record, or None."""
        deployment = {
            "id": secrets.token_hex(8),
            "environment": environment,
            "release": release,
            "agent_id": agent_id,
            "agent_version": agent_version,
            "agent_digest": agent_digest,
            "providers": _canonical(dict(providers)),
            "started_at": _now(),
        }
        with self._db.session(exclusive=True) as db:
            last = db.one(
                "SELECT release, agent_digest, providers FROM deployments "
                "WHERE environment = ? AND agent_id = ? ORDER BY started_at DESC LIMIT 1",
                (environment, agent_id),
            )
            unchanged = ("release", "agent_digest", "providers")
            if last is not None and all(last[k] == deployment[k] for k in unchanged):
                return None
            db.all(
                "INSERT INTO deployments (id, environment, release, agent_id, agent_version, "
                "agent_digest, providers, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(deployment.values()),
            )
            self._append(db, f"deployment:{deployment['id']}", "deployment.recorded", deployment)
        return {**deployment, "providers": dict(providers)}

    def capability_since(self, environment: str, version: str) -> str:
        """When this environment started serving this version of its FHIR CapabilityStatement:
        the time recorded when the version first replaced another one here. A statement's
        `date` must change when its version does, and never go back, so a version served again
        after another one gets a new time. That time is later than the one it replaces even when
        `_now()` is not (the same millisecond, or an instance whose clock is behind).

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
                    now = _now() if last is None else max(_now(), _just_after(last["since"]))
                    last = {"version": version, "since": now}
                    db.all(
                        "INSERT INTO capability_versions (environment, version, since) "
                        "VALUES (?, ?, ?)",
                        (environment, version, last["since"]),
                    )
        since = str(last["since"])
        self._capability_since[environment] = (version, since)
        return since

    def deployments(
        self, agent_id: str | None = None, limit: int = 50, *, digest: str | None = None
    ) -> list[dict[str, Any]]:
        """Newest first, of one agent and one build of it if given. Deployments belong to the
        server, so every organization sees them."""
        wheres = [
            (column, value)
            for column, value in (("agent_id", agent_id), ("agent_digest", digest))
            if value
        ]
        where = ("WHERE " + " AND ".join(f"{column} = ?" for column, _ in wheres)) if wheres else ""
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                f"SELECT * FROM deployments {where} ORDER BY started_at DESC LIMIT ?",
                (*(value for _, value in wheres), limit),
            )
        return [{**row, "providers": json.loads(row["providers"])} for row in rows]

    def configuration_started(
        self, agent_id: str, digest: str, providers: Mapping[str, str], limit: int = 500
    ) -> str | None:
        """When this build of an agent started running on these providers (slot -> variant), by
        the deployment records: the oldest of the newest unbroken run of records with this
        digest and these providers, newest first across environments; None without one. A time
        only, for monitoring (which compares decisions made since): never a record."""
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT agent_digest, providers, started_at FROM deployments WHERE agent_id = ? "
                "ORDER BY started_at DESC LIMIT ?",
                (agent_id, limit),
            )
        started = None
        for row in rows:
            if row["agent_digest"] == digest and json.loads(row["providers"]) == dict(providers):
                started = str(row["started_at"])
            elif started is not None:
                break  # the run before this configuration
        return started

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
        preset: str | None = None,
    ) -> dict[str, Any]:
        """A new key for a machine; its secret is in the result and nowhere else. `scopes` (the
        caller has checked them, server/scopes.py) narrow the role; None keeps the role's.
        `preset` labels the integration path it was made for (server/presets.py, checked by the
        caller); a label only: never read when the key is used."""
        with self._db.session(exclusive=True) as db:
            return self._insert_key(
                db,
                name=name,
                role=role,
                org=org,
                created_by=created_by,
                scopes=scopes,
                days=days,
                preset=preset,
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
        preset: str | None = None,
    ) -> dict[str, Any]:
        """Store a new key (hashed) and chain `key.created` (with `replaces` for a rotation's,
        `preset` for one made from a preset); the key with its secret, which is nowhere else."""
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
            **({"preset": preset} if preset else {}),
        }
        db.all(
            "INSERT INTO api_keys (id, key_sha256, name, role, org, created_by, created_at, "
            "scopes, expires_at, preset) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                preset,
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
                "SELECT id, name, role, org, scopes, expires_at, revoked_at, replaced_by, preset "
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
                preset=row["preset"],  # its label, and its stored scope: never the table's now
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
            chained = self._bound(rotated, row["org"])
            self._append(db, f"key:{key_id}", "key.rotated", chained)
        return {**new, "replaces": {"id": key_id, "expires_at": until}}

    def api_keys(self, org: str | None = None) -> list[dict[str, Any]]:
        """Every key (of `org`, if given), oldest first, with its `status` now (`_key_status`);
        never the secret or its hash."""
        # None is every organization (open mode, the command line); "" is an organization.
        where, params = ("WHERE org = ?", (org,)) if org is not None else ("", ())
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT id, name, role, org, created_by, created_at, revoked_at, scopes AS scope, "
                f"expires_at, revoked_by, replaced_by, preset FROM api_keys {where} "
                "ORDER BY created_at",
                params,
            )
        now = _now()
        return [{**row, "status": _key_status(row, now)} for row in rows]

    def api_key(self, secret: str) -> dict[str, Any] | None:
        """The key a secret belongs to, unless it was revoked or has expired (one query; a key
        without an expiry is refused too)."""
        with self._db.session(exclusive=False) as db:
            return db.one(
                "SELECT id, name, role, org, scopes AS scope, expires_at, replaced_by "
                "FROM api_keys WHERE key_sha256 = ? AND revoked_at IS NULL AND expires_at > ?",
                (_key(secret), _now()),
            )

    def revoke_api_key(
        self, key_id: str, org: str | None = None, *, revoked_by: str, reason: str | None = None
    ) -> bool:
        """Revoke a key of `org` (any, if None); False for an unknown or already revoked one.
        The break-glass (`mendari access revoke`) chains why too (`reason`)."""
        why = None if reason is None else break_glass_reason(reason)
        with self._db.session(exclusive=True) as db:
            row = db.one("SELECT org FROM api_keys WHERE id = ? AND revoked_at IS NULL", (key_id,))
            if row is None or (org is not None and row["org"] != org):
                return False
            revoked = {"id": key_id, "revoked_by": revoked_by, "revoked_at": _now()}
            if why is not None:
                revoked["reason"] = why
            db.all(
                "UPDATE api_keys SET revoked_at = ?, revoked_by = ? WHERE id = ?",
                (revoked["revoked_at"], revoked_by, key_id),
            )
            chained = self._bound(revoked, row["org"])
            self._append(db, f"key:{key_id}", "key.revoked", chained)
        return True

    # -- SMART Backend Services: clients and access tokens (ADR-0004) -------------------------
    # A client is a machine (viewer or service) of one organization, with the scopes it may be
    # granted and a public JWK Set registered by value (server/backend_services.py checks both).
    # Registering, replacing its key set (LP-12h) and revoking are chained under "client:<id>",
    # with the keys' ids and RFC 7638 thumbprints. An access token is stored as SHA-256 with its
    # client, the scope it was granted, its expiry and the version of the client's key set its
    # assertion was checked against, nothing else: each lookup joins the client, so a change to
    # its role, scopes or organization, its revocation, or a replaced key set (`keys_version`),
    # applies to its tokens at once.

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
        """Every client (of `org`, if given), oldest first, with its keys' ids and thumbprints
        and when its key set was last replaced (`keys_replaced_at`, None if never)."""
        # None is every organization (open mode, the command line); "" is an organization.
        where, params = ("WHERE org = ?", (org,)) if org is not None else ("", ())
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT client_id, name, role, org, scopes AS scope, jwks, created_by, "
                f"created_at, revoked_at, revoked_by, keys_replaced_at FROM oauth_clients {where} "
                "ORDER BY created_at",
                params,
            )
        return [
            {**{k: v for k, v in row.items() if k != "jwks"}, "keys": _key_summary(_jwks(row))}
            for row in rows
        ]

    def client(self, client_id: str) -> dict[str, Any] | None:
        """A client that is not revoked, with its public keys and their `keys_version`, read
        together (one row): a token issued after checking an assertion against these keys
        carries that version (`issue_token`). None otherwise."""
        with self._db.session(exclusive=False) as db:
            row = db.one(
                "SELECT client_id, name, role, org, scopes AS scope, jwks, "
                "COALESCE(keys_version, 0) AS keys_version FROM oauth_clients "
                "WHERE client_id = ? AND revoked_at IS NULL",
                (client_id,),
            )
        return None if row is None else {**row, "jwks": _jwks(row)}

    def revoke_client(
        self,
        client_id: str,
        org: str | None = None,
        *,
        revoked_by: str,
        reason: str | None = None,
    ) -> bool:
        """Revoke a client of `org` (any, if None) and drop its tokens; False for an unknown or
        already revoked one. Its tokens stop working at once either way (`access_token`). The
        break-glass (`mendari access revoke`) chains why too (`reason`)."""
        why = None if reason is None else break_glass_reason(reason)
        with self._db.session(exclusive=True) as db:
            row = db.one(
                "SELECT org FROM oauth_clients WHERE client_id = ? AND revoked_at IS NULL",
                (client_id,),
            )
            if row is None or (org is not None and row["org"] != org):
                return False
            revoked = {"client_id": client_id, "revoked_by": revoked_by, "revoked_at": _now()}
            if why is not None:
                revoked["reason"] = why
            db.all(
                "UPDATE oauth_clients SET revoked_at = ?, revoked_by = ? WHERE client_id = ?",
                (revoked["revoked_at"], revoked_by, client_id),
            )
            db.all("DELETE FROM oauth_tokens WHERE client_id = ?", (client_id,))
            chained = self._bound(revoked, row["org"])
            self._append(db, f"client:{client_id}", "client.revoked", chained)
        return True

    def replace_client_keys(
        self,
        client_id: str,
        org: str | None = None,
        *,
        replaced_by: str,
        jwks: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Replace the public keys of a client of `org` (any, if None) whole, keeping its id,
        role and scopes; `jwks` was checked by the caller (`check_jwks`). Its tokens are dropped
        in the same transaction, and a token issued meanwhile against the old keys is refused
        (`keys_version`, compared by `access_token`). No overlap: a switch without a gap is two
        replacements (old and new keys, then the new alone). Chained `client.keys_replaced`
        (ids, types and thumbprints, never a key's material), with the organization.

        Refused: an unknown client or another organization's (404); a revoked one (409)."""
        if not jwks:
            raise ValueError("a client keeps at least one key")
        now = _now()
        with self._db.session(exclusive=True) as db:
            row = db.one(
                "SELECT org, revoked_at, jwks FROM oauth_clients WHERE client_id = ?",
                (client_id,),
            )
            if row is None or (org is not None and row["org"] != org):
                raise AuthError("no such client", 404)
            if row["revoked_at"] is not None:
                raise AuthError("this client is revoked: register a new one", 409)
            db.all(
                "UPDATE oauth_clients SET jwks = ?, "
                "keys_version = COALESCE(keys_version, 0) + 1, keys_replaced_at = ? "
                "WHERE client_id = ?",
                (_canonical({"keys": list(jwks)}), now, client_id),
            )
            dropped = db.all(
                "DELETE FROM oauth_tokens WHERE client_id = ? RETURNING token_sha256",
                (client_id,),
            )
            replaced = {
                "client_id": client_id,
                "org": row["org"],  # always, in both modes (conflict 3)
                "keys_before": _key_summary(_jwks(row)),
                "keys": _key_summary(jwks),
                "replaced_by": replaced_by,
                "replaced_at": now,
                "tokens_dropped": len(dropped),
            }
            self._append(db, f"client:{client_id}", "client.keys_replaced", replaced)
        return replaced

    def issue_token(
        self, client_id: str, scope: str, seconds: int, keys_version: int = 0
    ) -> tuple[str, int]:
        """A new access token for a client (already authenticated) and the scope it was granted;
        the secret and its expiry (Unix time). Stored hashed, with `keys_version`: the version
        of the client's key set its assertion was checked against (`client`), so a token issued
        while that set was being replaced is refused (`access_token`). Expired tokens are
        purged."""
        secret = "mat_" + secrets.token_urlsafe(32)
        now = int(time.time())
        expires_at = now + seconds
        with self._db.session(exclusive=False) as db:
            db.all("DELETE FROM oauth_tokens WHERE expires_at <= ?", (now,))
            db.all(
                "INSERT INTO oauth_tokens (token_sha256, client_id, scope, expires_at, "
                "keys_version) VALUES (?, ?, ?, ?, ?)",
                (_key(secret), client_id, scope, expires_at, keys_version),
            )
        return secret, expires_at

    def access_token(self, secret: str) -> dict[str, Any] | None:
        """What an access token stands for, read from its client now (one join): None for an
        unknown or expired token, one whose client was revoked, or one issued against a key set
        the client's has replaced since."""
        with self._db.session(exclusive=False) as db:
            return db.one(
                "SELECT c.client_id, c.name, c.role, c.org, c.scopes AS registered, t.scope, "
                "t.expires_at FROM oauth_tokens t JOIN oauth_clients c "
                "ON c.client_id = t.client_id WHERE t.token_sha256 = ? AND t.expires_at > ? "
                "AND c.revoked_at IS NULL "
                "AND COALESCE(t.keys_version, 0) = COALESCE(c.keys_version, 0)",
                (_key(secret), int(time.time())),
            )

    def subject_history(self, kind: str, subject_id: str, org: str | None) -> dict[str, Any] | None:
        """`<kind>:<subject_id>`'s chained events, oldest first (the newest `HISTORY_LIMIT`,
        `truncated` if there were more), if its row is `org`'s (any, if None: open mode, the
        command line); None for an unknown row or another organization's. Each event is
        attributed through the row it names (ADR-0008), and one whose payload names another
        organization than the row's is left out (fail closed). Never `seq` or hashes."""
        table, id_column, org_expression = SUBJECT_TABLES[kind]
        with self._db.session(exclusive=False) as db:
            row = db.one(
                f"SELECT {org_expression} AS org FROM {table} WHERE {id_column} = ?",
                (subject_id,),
            )
            if row is None:
                return None
            # A hold's NULL is the deployment's (every organization's), no one organization's.
            owner = row["org"]
            if org is not None and owner != org:
                return None
            rows = db.all(
                "SELECT event, payload, created_at FROM audit_events WHERE decision_id = ? "
                "ORDER BY seq DESC LIMIT ?",
                (f"{kind}:{subject_id}", HISTORY_LIMIT + 1),
            )
        events = []
        for found in reversed(rows[:HISTORY_LIMIT]):
            payload = json.loads(found["payload"])
            if "org" in payload and payload["org"] != owner:
                continue  # never shown here; LP-12g's verify reports such an event
            events.append({"event": found["event"], "at": found["created_at"], "payload": payload})
        return {
            "subject": f"{kind}:{subject_id}",
            "org": owner,
            "events": events,
            "truncated": len(rows) > HISTORY_LIMIT,
        }

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
                self._insert_user(
                    db,
                    user_id=user_id,
                    issuer=issuer,
                    subject=subject,
                    email=address,
                    name=name,
                    signed_in=now,
                )
            else:
                db.all(
                    "UPDATE users SET email = ?, name = ?, last_sign_in_at = ? WHERE id = ?",
                    (address, name, now, user_id),
                )
            user = db.one("SELECT * FROM users WHERE id = ?", (user_id,))
        return dict(user) if user is not None else {}

    def _insert_user(
        self,
        db: _Session,
        *,
        user_id: str,
        issuer: str,
        subject: str,
        email: str | None,
        name: str | None,
        signed_in: str | None,
    ) -> None:
        """A person's first record (`user.recorded`), in the caller's exclusive transaction."""
        try:
            address = accounts.normalise_email(email) if email else None
        except ValueError:
            address = None
        db.all(
            "INSERT INTO users (id, issuer, subject, email, name, created_at, last_sign_in_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, issuer, subject, address, name, _now(), signed_in),
        )
        self._append(db, user_id, "user.recorded", {"user": user_id})

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
        """Every organization's record."""
        with self._db.session(exclusive=False) as db:
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
            rows = db.all(
                f"SELECT * FROM memberships WHERE {' AND '.join(where)} ORDER BY created_at, id",
                params,
            )
        return [_membership(r) for r in rows]

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

    def members(self, org: str) -> list[dict[str, Any]]:
        """`org`'s memberships in force, oldest first, each with its person's name and verified
        address (or, pending, the address it waits for: `pending`), for its administrators."""
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT m.id, m.user_id, m.roles, m.created_by, m.created_at, u.name, "
                "COALESCE(u.email, m.pending_email) AS email FROM memberships m "
                "LEFT JOIN users u ON u.id = m.user_id "
                "WHERE m.org = ? AND m.removed_at IS NULL ORDER BY m.created_at, m.id",
                (org,),
            )
        return [
            {
                "id": r["id"],
                "user_id": r["user_id"],
                "name": r["name"],
                "email": r["email"],
                "pending": r["user_id"] is None,
                "roles": json.loads(r["roles"]),
                "created_by": r["created_by"],
                "created_at": r["created_at"],
            }
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
            raise accounts.MembershipRefused(f"already a member of {org!r}", "member")
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
        if user_id is not None:
            self._settle_requests(db, user_id=user_id, org=org, membership_id=str(membership["id"]))
        return membership

    def _settle_requests(self, db: _Session, *, user_id: str, org: str, membership_id: str) -> None:
        """`user_id`'s open request to join `org`, closed in the caller's transaction now that
        they hold a membership of it some other way (`access.handled`, outcome `joined`, handled
        by that membership): no administrator is left to grant it into a conflict, and it no
        longer counts against the person's open requests."""
        settled = db.all(
            "UPDATE access_requests SET handled_at = ?, handled_by = ?, outcome = ? "
            "WHERE user_id = ? AND org = ? AND handled_at IS NULL RETURNING id",
            (_now(), f"membership:{membership_id}", "joined", user_id, org),
        )
        for row in settled:
            self._append(
                db,
                f"access:{row['id']}",
                "access.handled",
                {"id": row["id"], "org": org, "outcome": "joined",
                 "handled_by": f"membership:{membership_id}"},
            )  # fmt: skip

    def set_membership_roles(
        self, membership_id: str, roles: Sequence[str], *, org: str, changed_by: str
    ) -> dict[str, Any]:
        """Change a membership of `org`'s roles (`membership.roles_changed`); never leaving
        `org` without an administrator. `accounts.MembershipRefused` (a `ValueError`) for one not
        in force (`no_membership`) or the last administrator's (`last_admin`)."""
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
        """End a membership of `org` (`membership.removed`); never its last administrator's
        (`accounts.MembershipRefused`, `last_admin`). False for one not in force. The open
        invitations of `org` made out to that person's address (recorded, or the pending
        membership's) are revoked with it
        (`invitation.revoked`, by `removed_by`), so no link left open earlier (one they could not
        accept while a member) lets them back in; the invitations, keys and clients they made
        stay as they were."""
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
            if row["user_id"] is None:
                address = row["pending_email"]
            else:
                person = db.one("SELECT email FROM users WHERE id = ?", (row["user_id"],))
                address = None if person is None else person["email"]
            if address:
                self._revoke_invitations_to(db, address=address, org=org, by=removed_by)
        return True

    def _revoke_invitations_to(self, db: _Session, *, address: str, org: str, by: str) -> None:
        """Revoke `org`'s open invitations made out to `address` (a person's recorded verified
        one, or a pending membership's), in the caller's transaction (`invitation.revoked` each,
        as `revoke_invitation` chains it)."""
        revoked = db.all(
            "UPDATE invitations SET revoked_at = ?, revoked_by = ? WHERE org = ? AND email = ? "
            "AND accepted_at IS NULL AND revoked_at IS NULL RETURNING id, created_at",
            (_now(), by, org, address),
        )
        for invitation in sorted(revoked, key=lambda r: (r["created_at"], r["id"])):
            self._append(
                db,
                f"invitation:{invitation['id']}",
                "invitation.revoked",
                {"id": invitation["id"], "org": org, "revoked_by": by},
            )

    def _membership_row(self, db: _Session, membership_id: str, org: str) -> Any:
        row = db.one(
            "SELECT * FROM memberships WHERE id = ? AND org = ? AND removed_at IS NULL",
            (membership_id, org),
        )
        if row is None:
            raise accounts.MembershipRefused("no such membership", "no_membership")
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
            raise accounts.MembershipRefused(
                f"{org!r} would be left without an administrator", "last_admin"
            )

    def bind_pending(self, user_id: str, email: str) -> list[dict[str, Any]]:
        """The pending memberships for `email` (verified by the identity provider at this
        sign-in) made `user_id`'s (`membership.bound`, and their open request to join that
        organization, if any, closed: `access.handled`); one where they are already a member
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
                self._settle_requests(db, user_id=user_id, org=row["org"], membership_id=row["id"])
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
        """`org`'s invitations, newest first, each with its `status` (`open`, `accepted`,
        `revoked` or `expired`); never their tokens or their digests."""
        now = _now()
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT id, org, email, roles, created_by, created_at, expires_at, accepted_at, "
                "accepted_by, revoked_at, revoked_by FROM invitations WHERE org = ? "
                "ORDER BY created_at DESC, id",
                (org,),
            )
        return [
            {**dict(r), "roles": json.loads(r["roles"]), "status": _invitation_status(r, now)}
            for r in rows
        ]

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
        self,
        token: str,
        *,
        user_id: str,
        verified_email: str | None,
        asker: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Accept an invitation as the signed-in `user_id`, whose identity provider verified
        `verified_email`: once, before it expires, and only for the address it was made for
        (`invitation.accepted`, then `membership.created`). `InvitationRefused`, with its
        reason: no verified address (`unverified`); unknown, used, revoked and expired are one
        answer (`invalid`); another address (`other_address`) and someone already a member of
        its organization (`member`) leave it open, and nothing is written. `asker` (`issuer`,
        `subject`, `email`, `name`: who signed in) records the person, after every check and in
        the same transaction, when they have no record yet (`user.recorded`), as asking to join
        does."""
        if asker is not None and accounts.person_id(asker["issuer"], asker["subject"]) != user_id:
            raise ValueError("the asker is not the person accepting")
        if not verified_email:
            raise InvitationRefused(
                "sign in with the e-mail address the invitation is for, verified", 403, "unverified"
            )
        address = accounts.normalise_email(verified_email)
        with self._db.session(exclusive=True) as db:
            now = _now()  # under the lock: expiry as of this transaction
            row = db.one(
                "SELECT * FROM invitations WHERE token_sha256 = ? AND accepted_at IS NULL "
                "AND revoked_at IS NULL AND expires_at > ?",
                (_key(token), now),
            )
            if row is None:
                raise InvitationRefused(
                    "this invitation is not valid: ask for a new one", 404, "invalid"
                )
            if row["email"] != address:
                raise InvitationRefused(
                    "this invitation is for another e-mail address", 403, "other_address"
                )
            if db.one(
                "SELECT id FROM memberships WHERE org = ? AND user_id = ? AND removed_at IS NULL",
                (row["org"], user_id),
            ):
                raise InvitationRefused(
                    "you are already a member of this organization: ask its administrators to "
                    "change your roles",
                    409,
                    "member",
                )
            if (
                asker is not None
                and db.one("SELECT id FROM users WHERE id = ?", (user_id,)) is None
            ):
                self._insert_user(
                    db,
                    user_id=user_id,
                    issuer=str(asker["issuer"]),
                    subject=str(asker["subject"]),
                    email=asker.get("email"),
                    name=asker.get("name"),
                    signed_in=None,  # a sign-in keeps this current, not an invitation
                )
            taken = db.all(
                "UPDATE invitations SET accepted_at = ?, accepted_by = ? WHERE id = ? "
                "AND accepted_at IS NULL AND revoked_at IS NULL AND expires_at > ? RETURNING id",
                (now, user_id, row["id"], now),
            )
            if not taken:  # accepted or revoked since it was read: never twice, on any backend
                raise InvitationRefused(
                    "this invitation is not valid: ask for a new one", 404, "invalid"
                )
            self._append(
                db,
                f"invitation:{row['id']}",
                "invitation.accepted",
                {"id": row["id"], "org": row["org"], "user": user_id},
            )
            return self._insert_membership(
                db,
                org=row["org"],
                roles=json.loads(row["roles"]),
                created_by=f"invitation:{row['id']}",
                user_id=user_id,
            )

    def request_access(
        self, *, user_id: str, org: str, asker: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """A person asking to join `org` (`access.requested`); their open request again if they
        asked before. `accounts.AccessRefused` (a `ValueError`) for an organization with no
        record, one they are a member of, or a request past `accounts.OPEN_REQUESTS_MAX` open
        ones. What is refused is refused before the chain's lock is taken: asking costs
        everyone else nothing. `asker` (`issuer`, `subject`, `email`, `name`: who signed in)
        records the person, in the same transaction as the request, when they have no record
        yet (`user.recorded`); a record they have is left as it is (signing in keeps it
        current), so a request never stands without its person."""
        accounts.check_org_id(org)
        if asker is not None and accounts.person_id(asker["issuer"], asker["subject"]) != user_id:
            raise ValueError("the asker is not the person asking")
        with self._db.session(exclusive=False) as db:
            found = self._open_request(db, user_id, org)
        if found is not None:
            return found
        with self._db.session(exclusive=True) as db:
            found = self._open_request(db, user_id, org)  # again, under the lock
            if found is not None:
                return found
            if (
                asker is not None
                and db.one("SELECT id FROM users WHERE id = ?", (user_id,)) is None
            ):
                self._insert_user(
                    db,
                    user_id=user_id,
                    issuer=str(asker["issuer"]),
                    subject=str(asker["subject"]),
                    email=asker.get("email"),
                    name=asker.get("name"),
                    signed_in=None,  # a sign-in keeps this current, not a request
                )
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
            rows = db.all(
                f"SELECT * FROM access_requests WHERE handled_at IS NULL {where} "
                "ORDER BY created_at, id",
                params,
            )
        return [dict(r) for r in rows]

    def open_access_requests(self, org: str) -> list[dict[str, Any]]:
        """`org`'s open requests to join, oldest first, each with the name and verified address
        of the person who asked, for its administrators."""
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT r.id, r.user_id, r.created_at, u.name, u.email FROM access_requests r "
                "LEFT JOIN users u ON u.id = r.user_id "
                "WHERE r.org = ? AND r.handled_at IS NULL ORDER BY r.created_at, r.id",
                (org,),
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
        (`access.handled`); None for one not open. A request closes by itself when its person
        joins another way (`_settle_requests`); granting one whose person is a member anyway (a
        request left open before that) is refused (`accounts.MembershipRefused`, `member`), and
        the request stays open."""
        if outcome not in ("granted", "refused"):
            raise ValueError("an access request is granted or refused")
        with self._db.session(exclusive=True) as db:
            row = db.one(
                "SELECT * FROM access_requests WHERE id = ? AND org = ? AND handled_at IS NULL",
                (request_id, org),
            )
            if row is None:
                return None
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
            membership = None
            if outcome == "granted":  # after: it is no longer open for the membership to close
                membership = self._insert_membership(
                    db, org=org, roles=roles, created_by=handled_by, user_id=row["user_id"]
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

    def first_admin(self) -> str | None:
        """The membership the first-administrator setting made on this database (`FIRST_ADMIN`),
        or None while it has made none. Once it has, the setting is ignored here."""
        with self._db.session(exclusive=False) as db:
            row = db.one("SELECT value FROM settings WHERE name = ?", (FIRST_ADMIN,))
        return None if row is None else str(row["value"])

    def first_administrator(
        self, *, issuer: str, subject: str, email: str, name: str | None, org: str, org_name: str
    ) -> dict[str, Any] | None:
        """Make the person signing in now (`issuer` and `subject`; `email` the address their
        identity provider verified at this sign-in) the first administrator of `org`, as the
        first-administrator setting asks (`server/auth.py`, which checked that `email` is the
        setting's): `org`'s record when it has none (`organization.created`), theirs when they
        have none (`user.recorded`), and an administrator's membership (`membership.created`,
        or `membership.roles_changed` on one they hold already), each chained as made by
        `FIRST_ADMIN_BY`. The database is marked in the same transaction (`FIRST_ADMIN`).

        Once, and for an organization nobody administers yet: None, with nothing written, when
        this database is marked already or `org` has an administrator who signed in. An
        organization never loses its last one (`_keep_an_admin`), so the setting can never give
        a second administrator, even on a database restored from before its mark."""
        user_id = accounts.person_id(issuer, subject)
        address = accounts.normalise_email(email)
        accounts.check_org_id(org)
        with self._db.session(exclusive=True) as db:
            if db.one("SELECT value FROM settings WHERE name = ?", (FIRST_ADMIN,)) is not None:
                return None
            held = db.all(
                "SELECT id, user_id, roles FROM memberships WHERE org = ? AND removed_at IS NULL "
                "AND user_id IS NOT NULL",
                (org,),
            )
            if any(accounts.ADMIN in json.loads(row["roles"]) for row in held):
                return None
            new_org = db.one("SELECT id FROM organizations WHERE id = ?", (org,)) is None
            if new_org:
                self._insert_organization(db, org=org, name=org_name, created_by=FIRST_ADMIN_BY)
            if db.one("SELECT id FROM users WHERE id = ?", (user_id,)) is None:
                self._insert_user(
                    db,
                    user_id=user_id,
                    issuer=issuer,
                    subject=subject,
                    email=address,
                    name=name,
                    signed_in=_now(),
                )
            theirs = next((row for row in held if row["user_id"] == user_id), None)
            if theirs is None:
                made = self._insert_membership(
                    db, org=org, roles=[accounts.ADMIN], created_by=FIRST_ADMIN_BY, user_id=user_id
                )
                membership_id = str(made["id"])
            else:  # a member already, of another role: an administrator from now on
                membership_id = str(theirs["id"])
                previous = json.loads(theirs["roles"])
                chosen = accounts.check_roles([*previous, accounts.ADMIN])
                db.all(
                    "UPDATE memberships SET roles = ? WHERE id = ?",
                    (json.dumps(chosen), membership_id),
                )
                self._append(
                    db,
                    f"membership:{membership_id}",
                    "membership.roles_changed",
                    {"id": membership_id, "org": org, "roles": chosen, "previous": previous,
                     "changed_by": FIRST_ADMIN_BY},
                )  # fmt: skip
            _mark(db, FIRST_ADMIN, membership_id)
        return {"org": org, "membership": membership_id, "organization_created": new_org}

    def organizations_in_use(self) -> list[dict[str, Any]]:
        """The preflight before memberships mode (`mendari access check`): each organization
        that keys, clients, approvals, holds, decisions or evaluation reports name, with how
        many of each, whether it has a record, and whether its id can have one (`recordable`).
        Counts and ids only."""
        tables = (  # (table, the live rows)
            ("api_keys", "revoked_at IS NULL"),
            ("oauth_clients", "revoked_at IS NULL"),
            ("governance_approvals", "revoked_at IS NULL"),
            ("registry_approvals", "revoked_at IS NULL"),
            ("automation_holds", "lifted_at IS NULL"),
            ("decisions", "1 = 1"),
            ("eval_reports", "1 = 1"),
        )
        found: dict[str, dict[str, Any]] = {}
        with self._db.session(exclusive=False) as db:
            recorded = {r["id"] for r in db.all("SELECT id FROM organizations")}
            for table, live in tables:
                rows = db.all(f"SELECT org, COUNT(*) AS n FROM {table} WHERE {live} GROUP BY org")
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

    # -- an organization's administrative events (LP-12g, ADR-0008) ----------------------------
    # The events of the rows an organization owns (`SUBJECT_TABLES`: its keys, clients,
    # approvals, holds, evaluation reports, record, memberships, invitations and requests to
    # join), attributed by the row, never by a range of chain ids. Without an organization (the
    # operator's command line), every event on no decision, the deployment's own included.

    def administrative_events(
        self,
        org: str | None,
        *,
        after: tuple[str, str] | None = None,
        kinds: Sequence[str] = (),
        subject: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
        newest_first: bool = False,
    ) -> Page:
        """One page of administrative events in chain order, each `{subject, event, payload,
        created_at, hash}`: never `seq` or `prev_hash`, as an event's place in the deployment's
        one chain is not the organization's. `after` is the `(subject, hash)` of the last item
        of the page before; one that is not an event of this list is a `ValueError`, the same
        whatever is wrong with it, so a cursor cannot ask about another organization's events.
        `kinds` and `subject` (a chain id: `key:<id>`) narrow the list; `since` / `until` bound
        `created_at` (the stored format). An event whose payload names another organization
        than its row's is left out (`verify` reports it). `newest_first` reverses the chain
        order: each page then holds the events before the cursor's in the chain, so a window's
        newest events come first however many it has (the dashboard's Activity tab)."""
        if limit < 1:
            return Page()
        with self._db.session(exclusive=False) as db:
            listed = _administrative(org, subject)
            if listed is None:  # no such subject here
                if after is not None:
                    raise ValueError("not a cursor of this list")
                return Page()
            source, where, params = listed
            if after is not None:
                found = db.one(
                    f"SELECT e.seq FROM {source} WHERE {where} AND e.decision_id = ? "
                    "AND e.hash = ?",
                    (*params, *after),
                )
                if found is None:
                    raise ValueError("not a cursor of this list")
            # Past the cursor in the list's order: later in the chain, or earlier when newest
            # first (which starts at the chain's end).
            start = found["seq"] if after is not None else (None if newest_first else 0)
            clauses, values = [where], [*params]
            if start is not None:
                clauses.append("e.seq < ?" if newest_first else "e.seq > ?")
                values.append(start)
            if kinds:
                clauses.append(f"e.event IN ({', '.join('?' for _ in kinds)})")
                values += kinds
            for clause, value in (("e.created_at >= ?", since), ("e.created_at < ?", until)):
                if value is not None:
                    clauses.append(clause)
                    values.append(value)
            rows = db.all(
                "SELECT e.decision_id, e.event, e.payload, e.created_at, e.hash, "
                f"{'s.org' if org is not None else 'NULL'} AS org FROM {source} "
                f"WHERE {' AND '.join(clauses)} ORDER BY e.seq{' DESC' if newest_first else ''} "
                "LIMIT ?",
                (*values, limit + 1),
            )
        page = rows[:limit]
        items = []
        for row in page:
            payload = json.loads(row["payload"])
            named = isinstance(payload, dict) and "org" in payload
            if org is not None and named and payload["org"] != row["org"]:
                continue  # fail closed: not its row's organization's
            items.append(
                {
                    "subject": row["decision_id"],
                    "event": row["event"],
                    "payload": payload,
                    "created_at": row["created_at"],
                    "hash": row["hash"],
                }
            )
        more = len(rows) > limit
        return Page(
            rows=items,
            after=(str(page[-1]["decision_id"]), str(page[-1]["hash"])) if more else None,
        )

    def iter_administrative_events(
        self, org: str | None, **filters: Any
    ) -> Iterator[dict[str, Any]]:
        """Every event `administrative_events` lists, page by page (an export)."""
        after: tuple[str, str] | None = None
        while True:
            page = self.administrative_events(org, after=after, limit=500, **filters)
            yield from page.rows
            if page.after is None:
                return
            after = page.after

    def decision_rows(
        self, agent_id: str, since: str, version: str | None = None
    ) -> list[dict[str, Any]]:
        """Each decision of an agent since a time (ISO), of one version if given, for monitoring:
        when, the outcome, the primary confidence, whether its input was refused, whether it
        awaits review, what a person or a downstream system said, and the cost."""
        scope, scoped = _scope()
        versions = ("AND d.agent_version = ? ", (version,)) if version is not None else ("", ())
        with self._db.session(exclusive=False) as db:
            return db.all(
                "SELECT d.id, d.created_at, d.outcome, d.confidence, d.recommendation, "
                "d.final_label, d.observed_label, d.human_override, d.cost_usd, "
                f"{_REFUSED} AS refused, "
                "d.review_status, d.latency_ms, d.provider_errors, "
                "(SELECT COUNT(*) FROM feedback f WHERE f.decision_id = d.id "
                "AND f.superseded_by IS NULL AND f.rating = 'down') AS rated_wrong "
                f"FROM decisions d WHERE {scope} AND d.agent_id = ? AND d.created_at >= ? "
                f"{versions[0]}ORDER BY d.created_at",
                (*scoped, agent_id, since, *versions[1]),
            )

    def organizations_since(self, since: str) -> list[str]:
        """Every organization that made a decision since a time (ISO): the monitor's schedule
        runs once for each (LP-21f). Deployment-wide: an operator's read, never an
        organization's route's."""
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT DISTINCT org FROM decisions WHERE created_at >= ? ORDER BY 1", (since,)
            )
        return [str(row["org"]) for row in rows]

    def decision_versions(self, agent_id: str, since: str) -> dict[str, int]:
        """How many decisions each version of an agent made since a time (ISO), for monitoring,
        which compares one version's with its benchmark and counts the others apart."""
        scope, scoped = _scope()
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT d.agent_version, COUNT(*) AS n FROM decisions d "
                f"WHERE {scope} AND d.agent_id = ? AND d.created_at >= ? GROUP BY d.agent_version",
                (*scoped, agent_id, since),
            )
        return {str(row["agent_version"]): int(row["n"]) for row in rows}

    def pending_by_agent(self) -> list[dict[str, Any]]:
        """Pending review counts grouped by agent for the current organization (aggregate counts
        only, never inputs or PHI), ordered with stat items first."""
        scope, scoped = _scope()
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT d.agent_id, "
                "COUNT(*) AS pending, "
                "SUM(CASE WHEN d.priority = 'stat' THEN 1 ELSE 0 END) AS stat, "
                "SUM(CASE WHEN d.priority = 'asap' THEN 1 ELSE 0 END) AS asap, "
                "SUM(CASE WHEN d.priority = 'urgent' THEN 1 ELSE 0 END) AS urgent "
                "FROM decisions d "
                f"WHERE {scope} AND d.review_status = 'pending' "
                "GROUP BY d.agent_id "
                "ORDER BY stat DESC, asap DESC, urgent DESC, pending DESC, d.agent_id ASC",
                scoped,
            )
        return [
            {
                "agent_id": str(row["agent_id"]),
                "pending": int(row["pending"] or 0),
                "stat": int(row["stat"] or 0),
                "asap": int(row["asap"] or 0),
                "urgent": int(row["urgent"] or 0),
            }
            for row in rows
        ]

    def validation_summary(self, agent_id: str, *, days: int = 30) -> dict[str, Any]:
        """Aggregate clinical and operational summary for `agent_id` over the last `days` calendar
        days (zero PHI: reads only counts, outcomes, label codes, latencies, and policy reasons)."""
        scope, scoped = _scope()
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="milliseconds")
        params = (*scoped, agent_id, cutoff)
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT outcome, priority, recommendation, final_label, observed_label, "
                "human_override, review_status, latency_ms, cost_usd, record "
                "FROM decisions d "
                f"WHERE {scope} AND d.agent_id = ? AND d.created_at >= ?",
                params,
            )
            ratings = db.all(
                "SELECT f.rating, d.outcome FROM feedback f "
                "JOIN decisions d ON d.id = f.decision_id "
                f"WHERE f.superseded_by IS NULL AND {scope} AND d.agent_id = ? "
                "AND d.created_at >= ?",
                params,
            )
        outcomes = {outcome.value: 0 for outcome in Outcome}
        shadow_decisions = 0
        would_automate = 0
        stat_cases = 0
        reviewed = 0
        overrides = 0
        labelled = 0
        agreed = 0
        auto_labelled = 0
        clusters: dict[tuple[str, str], int] = {}
        latencies: list[float] = []
        costs: list[float | None] = []
        for row in rows:
            out = str(row["outcome"])
            if out in outcomes:
                outcomes[out] += 1
            if row["priority"] == "stat":
                stat_cases += 1
            latencies.append(float(row["latency_ms"]))
            costs.append(row["cost_usd"])
            reasons: list[str] = []
            try:
                parsed = json.loads(row["record"])
                raw_reasons = parsed.get("reasons") if isinstance(parsed, dict) else None
                if isinstance(raw_reasons, list):
                    reasons = [str(r) for r in raw_reasons]
            except (TypeError, ValueError):
                reasons = []
            held_shadow = any("shadow mode:" in r or "shadow pilot:" in r for r in reasons)
            if held_shadow:
                shadow_decisions += 1
            if out == Outcome.AUTO.value or any("automation is on hold:" in r for r in reasons):
                would_automate += 1
            if row["human_override"] is not None:
                reviewed += 1
                if row["human_override"]:
                    overrides += 1
                    rec = str(row["recommendation"])
                    chosen = str(row["final_label"] or "")
                    if chosen:
                        clusters[(rec, chosen)] = clusters.get((rec, chosen), 0) + 1
            truth = truth_label(row)
            if truth and row["recommendation"]:
                labelled += 1
                if truth == row["recommendation"]:
                    agreed += 1
            if truth and out == Outcome.AUTO.value:
                auto_labelled += 1  # an automated decision whose outcome someone recorded
        latencies.sort()
        down_on_auto = sum(
            r["rating"] == "down" and r["outcome"] == Outcome.AUTO.value for r in ratings
        )
        # Whether anyone looked at an automated decision at all: its current ratings, up or down.
        auto_rated = sum(r["outcome"] == Outcome.AUTO.value for r in ratings)
        top_clusters = [
            {
                "recommended": rec,
                "chosen": chosen,
                "count": count,
                "share": round(count / overrides, 4) if overrides else 0.0,
            }
            for (rec, chosen), count in sorted(
                clusters.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1])
            )[:10]
        ]
        return {
            "window_days": days,
            "total": len(rows),
            "outcomes": outcomes,
            "shadow_decisions": shadow_decisions,
            "would_automate": would_automate,
            "stat_cases": stat_cases,
            "pending_reviews": sum(r["review_status"] == "pending" for r in rows),
            "reviewed": reviewed,
            "overrides": overrides,
            "labelled": labelled,
            "agreed": agreed,
            "down_on_auto": down_on_auto,
            "auto_rated": auto_rated,
            "auto_labelled": auto_labelled,
            "p95_latency_ms": _percentile(latencies, 95),
            "cost_per_decision_usd": (
                None
                if not costs or any(c is None for c in costs)
                else sum(c for c in costs if c is not None) / len(costs)
            ),
            "override_clusters": top_clusters,
        }

    def pending_reviews(
        self, agent_id: str | None = None, limit: int = 50, *, reader: str | None = None
    ) -> list[dict[str, Any]]:
        """The queue, most urgent first; under PHI mode `encrypt`, with inputs for a `reader`.
        A decision with proposed actions (spec §13.7) has `actions`, each as it stands, so a
        reviewer sees what confirming the decision releases; their arguments are shown as
        inputs are (to a `reader`, the read chained)."""
        items = self.query(agent_id=agent_id, review_status="pending", limit=limit)
        if items:
            with self._db.session(exclusive=False) as db:
                found = _events_of(
                    db, [item["id"] for item in items], kinds=(actions.PROPOSED, actions.RESOLVED)
                )
            for item in items:
                if found[item["id"]]:  # only a decision with actions: every other is as it was
                    item["events"] = found[item["id"]]
        self.reveal_inputs(items, reader)
        for item in items:
            if "events" in item:
                _with_actions(item)
                del item["events"]
        return items

    def stats(self, agent_id: str | None = None) -> dict[str, Any]:
        scope, scoped = _scope()
        agents = ("AND d.agent_id = ?", (agent_id,)) if agent_id else ("", ())
        params = (*scoped, *agents[1])
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT outcome, recommendation, final_label, observed_label, human_override, "
                f"review_status, latency_ms, cost_usd, created_at FROM decisions d "
                f"WHERE {scope} {agents[0]}",
                params,
            )
            # Each person's current rating: a changed rating counts once, as its latest value.
            ratings = db.all(
                "SELECT f.rating, f.expected_label, d.outcome FROM feedback f "
                "JOIN decisions d ON d.id = f.decision_id "
                f"WHERE f.superseded_by IS NULL AND {scope} {agents[0]}",
                params,
            )
        total = len(rows)
        outcomes = {outcome.value: 0 for outcome in Outcome}
        for row in rows:
            outcomes[row["outcome"]] += 1
        reviewed = [r for r in rows if r["human_override"] is not None]
        labelled = [r for r in rows if truth_label(r) and r["recommendation"]]
        latencies = sorted(r["latency_ms"] for r in rows)
        costs = [r["cost_usd"] for r in rows]
        tz = clock.zone()
        daily: dict[str, dict[str, int]] = {}
        for row in rows:
            # Local calendar days: a decision at 9 PM Eastern counts on that Eastern day. Reviews
            # and labels count on the day of the decision they judge, so any run of days gives the
            # override rate and observed accuracy of the decisions made in it.
            local = clock.local_date(row["created_at"], tz)
            day = daily.setdefault(
                local, {**{o.value: 0 for o in Outcome}, **dict.fromkeys(_DAILY_REVIEWS, 0)}
            )
            day[row["outcome"]] += 1
            if row["human_override"] is not None:
                day["reviewed"] += 1
                day["overridden"] += bool(row["human_override"])
            if truth_label(row) and row["recommendation"]:
                day["labelled"] += 1
                day["agreed"] += truth_label(row) == row["recommendation"]
        return {
            "decisions": total,
            "outcomes": outcomes,
            "pending_reviews": sum(r["review_status"] == "pending" for r in rows),
            "reviews_resolved": len(reviewed),
            "human_override_rate": (
                sum(r["human_override"] for r in reviewed) / len(reviewed) if reviewed else None
            ),
            "observed_accuracy": (
                sum(truth_label(r) == r["recommendation"] for r in labelled) / len(labelled)
                if labelled
                else None
            ),
            "labelled": len(labelled),  # decisions with an outcome or a reviewer's label: its n
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
        agent_id: str | None = None,
        rating: Rating | None = None,
        limit: int = 100,
        reader: str | None = None,
    ) -> list[dict[str, Any]]:
        """Current feedback (each person's latest rating of a decision), newest first, each with
        the decision it rates and the right answer if the rating gave one; under PHI mode
        `encrypt`, with inputs for a `reader`. Notes as `reveal_inputs` shows them."""
        scope, scoped = _scope()
        clauses, params = ["f.superseded_by IS NULL", scope], list(scoped)
        for column, value in (("f.agent_id", agent_id), ("f.rating", rating)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}"
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT f.id, f.decision_id, f.created_at, f.agent_id, f.rating, f.reporter, "
                "f.expected_question, f.expected_label, d.record FROM feedback f "
                f"JOIN decisions d ON d.id = f.decision_id {where} "
                "ORDER BY f.created_at DESC LIMIT ?",
                (*params, limit),
            )
        items = [_feedback_item(r) for r in rows]
        return self.reveal_inputs(items, reader, id_key="decision_id", own_notes=True)

    def labelled(
        self, *, agent_id: str, limit: int = 1000, reader: str | None = None
    ) -> list[dict[str, Any]]:
        """An agent's decisions that someone has labelled, newest first: a reviewer's
        `final_label` (a resolved review), a downstream outcome's `observed_label`, or both
        (`truth_label` says which one counts). Each as `query` gives a decision; under PHI mode
        `encrypt`, with inputs for a `reader`, each such read chained (`reveal_inputs`)."""
        scope, scoped = _scope()
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                f"SELECT d.* FROM decisions d WHERE {scope} AND d.agent_id = ? "
                "AND (d.final_label IS NOT NULL OR d.observed_label IS NOT NULL) "
                "ORDER BY d.created_at DESC, d.id LIMIT ?",
                (*scoped, agent_id, limit),
            )
        items = [_row(row) for row in rows]
        # A database binds only so many values in one statement: inputs are opened a part at a
        # time (each part's reads chained together).
        for start in range(0, len(items), _REVEALED_AT_ONCE):
            self.reveal_inputs(items[start : start + _REVEALED_AT_ONCE], reader)
        return items

    def reports(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        scope, scoped = _scope()
        agents = ("AND agent_id = ?", (agent_id,)) if agent_id else ("", ())
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                f"SELECT report FROM eval_reports d WHERE {scope} {agents[0]} "
                "ORDER BY created_at DESC",
                (*scoped, *agents[1]),
            )
        return [json.loads(row["report"]) for row in rows]

    def verify(
        self,
        *,
        notes: bool = False,
        since_checkpoint: bool = False,
        head: str | None = None,
    ) -> dict[str, Any]:
        """Recompute the hash chain and each decision record's digest, and check that an event
        naming an organization names its row's (`_attributed`): a row moved to another
        organization, or gone, breaks verification at the first such event, with
        `reason: "organization"`. Each recorded checkpoint (`audit_checkpoints`, LP-21j) must
        still name its event: a chain rewritten from before one, or cut short of it, breaks
        verification there, with `reason: "checkpoint"`. Each decision row's unchanging columns
        must be its chained record's, and a full verification also requires every decision row
        to have its `decision.created` and every decision taken off the review queue to have a
        chained review or outcome (`_row_as_chained`, `_unsettled`): `reason: "decision"`. With
        `notes` (`mendari audit verify
        --notes`), also each kept note against the commitment its event chained: a note
        changed, moved to another decision, or missing without a chained `note.erased` breaks
        verification at that event (`_check_notes`).

        With `since_checkpoint`, only what came after the last checkpoint: its event must still
        be the one it recorded, and each later event is checked as above (`events` counts
        those; `checkpoint` says when it was recorded). What came before is not read again: a
        full verification covers it. Without a checkpoint it is a full one.

        With `head` (a signed checkpoint's, LP-21k), a full verification also requires an event
        with that hash: a chain rewritten from before the statement, or cut short of it, has
        none, and fails with `reason: "statement"`.

        A full verification also checks the grading rounds of written output (ADR-0025):
        each stored grade against the commitment its `written.graded` chained, and each closed
        round's stored report against the hash its `written.round_closed` chained; a grade or
        a report changed in the database, or one no event accounts for, breaks it with
        `reason: "written"` (`written`: what was checked, where there is any).

        Everything is read in one snapshot of the database (`db.Backend.snapshot`): what is
        written while it runs, an event and its rows alike, is not seen, and is the next
        verification's to check. Nothing is marked or written."""
        if since_checkpoint and (notes or head is not None):
            raise ValueError("notes and a signed head are checked by a full verification only")
        return self._verify(notes=notes, since_checkpoint=since_checkpoint, head=head)[0]

    def record_checkpoint(self, *, signer: Ed25519PrivateKey | None = None) -> dict[str, Any]:
        """Verify from the last checkpoint (from the start without one) and, when the chain
        holds, record its head as the next checkpoint (LP-21j): a row outside the chain, which
        later verifications start from, and every full one checks. The verification, with the
        checkpoint recorded (`recorded`: None when the chain is broken or has nothing new).

        With `signer` (`MENDARI_CHECKPOINT_KEY`, LP-21k), the day's statement too, once a local
        day: the head verified, signed (`core/checkpoint.py`), kept to publish (`signed`: the
        day's statement, the one signed earlier that day if any; None when the chain is broken
        or empty)."""
        status, last = self._verify(notes=False, since_checkpoint=True)
        previous = status.get("checkpoint")
        recorded = None
        if status["ok"] and last is not None and (previous is None or last != _at(previous)):
            recorded = {"seq": last["seq"], "hash": last["hash"], "created_at": _now()}
            with self._db.session(exclusive=False) as db:
                db.all(
                    "INSERT INTO audit_checkpoints (seq, hash, created_at) VALUES (?, ?, ?) "
                    "ON CONFLICT (seq) DO NOTHING",
                    (recorded["seq"], recorded["hash"], recorded["created_at"]),
                )
        if signer is None:
            return {**status, "recorded": recorded}
        signed: dict[str, Any] | None = None
        refused: str | None = None
        if status["ok"] and last is not None:
            from mendari.core import checkpoint

            statement = checkpoint.sign(last["hash"], clock.today(), signer)
            with self._db.session(exclusive=True) as db:
                db.all(
                    "INSERT INTO checkpoint_statements (day, seq, statement) VALUES (?, ?, ?) "
                    "ON CONFLICT (day) DO NOTHING",
                    (statement["date"], last["seq"], _canonical(statement)),
                )
                row = db.one(
                    "SELECT statement FROM checkpoint_statements WHERE day = ?",
                    (statement["date"],),
                )
                stored = None if row is None else json.loads(row["statement"])
                if isinstance(stored, dict) and stored.get("key_id") != statement["key_id"]:
                    # Signed earlier today with another key (the key was rotated since): this
                    # key's statement for the day replaces it.
                    db.all(
                        "UPDATE checkpoint_statements SET seq = ?, statement = ? WHERE day = ?",
                        (last["seq"], _canonical(statement), statement["date"]),
                    )
                    stored = statement
            # What the day's row holds must be this key's statement for today: anything else
            # was put there by someone with the database, and is never published as signed.
            refused = checkpoint.problem(stored, signer.public_key())
            if refused is None and isinstance(stored, dict) and stored["date"] != statement["date"]:
                refused = "it is another day's statement"
            signed = dict(stored) if refused is None and isinstance(stored, dict) else None
        checked = {**status, "recorded": recorded, "signed": signed}
        return checked if refused is None else {**checked, "statement_refused": refused}

    def statements(self, *, until: str, limit: int = 7) -> list[dict[str, Any]]:
        """The newest signed checkpoint statements (LP-21k) of the days up to `until` (an ISO
        date), newest first, each with the day its row is filed under: for the published one,
        which the caller checks (`/api/audit/checkpoint`)."""
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT day, statement FROM checkpoint_statements WHERE day <= ? "
                "ORDER BY day DESC LIMIT ?",
                (until, limit),
            )
        return [{"day": row["day"], "statement": json.loads(row["statement"])} for row in rows]

    def checkpoints(self) -> list[dict[str, Any]]:
        """Every recorded checkpoint, oldest first."""
        with self._db.session(exclusive=False) as db:
            return _checkpoints(db)

    def _verify(
        self,
        *,
        notes: bool,
        since_checkpoint: bool,
        head: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """`verify`, and the last event checked (`seq`, `hash`; None for none)."""
        # Every read is of one snapshot: an event and the rows it is about are committed
        # together, so the rows read are those the events read account for, whatever is written
        # while the verification runs (the next one checks that).
        with self._db.snapshot() as db:
            recorded = _checkpoints(db)
            start = recorded[-1] if since_checkpoint and recorded else None
            # The events checked are those up to the head (`top`), read a page at a time
            # (`_chain_pages`), so a long chain is never held whole.
            found_top = db.one("SELECT MAX(seq) AS top FROM audit_events")
            top = None if found_top is None or found_top["top"] is None else int(found_top["top"])
            first = None if start is None else int(start["seq"])
            counted = _chain_count(db, first, top)
            # `events` counts what is checked: from a checkpoint, not its own event.
            total = counted - 1 if start is not None and counted else counted
            columns = f"{', '.join(_DECISION_CHAINED)}, review_status, record, org"
            # From a checkpoint, only the decisions the events read name (page by page, below).
            decided = db.all(f"SELECT {columns} FROM decisions") if start is None else []
            records = {row["id"]: json.loads(row["record"]) for row in decided}
            rows_of = {row["id"]: row for row in decided}
            owners = {row["id"]: row["org"] for row in decided}
            subjects = _subject_orgs(db)
            kept = db.all("SELECT id, decision_id, event, sealed FROM notes") if notes else []
            # An agent store's versions (LP-20e): each against its chained hash and newest state.
            stored = {str(row["id"]): row for row in db.all("SELECT * FROM store_versions")}
            # Grading rounds of written output (ADR-0025): each stored grade and each closed
            # round's stored report, against what the chain committed to (`_check_written`).
            graded_rounds = self._written_rows(db) if start is None else None
            of_written: list[dict[str, Any]] = []
            states: dict[str, str] = {}
            prev = GENESIS
            expected = {row["seq"]: row["hash"] for row in recorded} if start is None else {}
            # Each decision's events that make or settle it (`_DECISION_STATES`), and the `seq`
            # of its `decision.created`, for the rows' own check after the chain's.
            settled: dict[str, set[str]] = {}
            created_at_seq: dict[str, int] = {}
            # Kept for the checks after the chain's, and only what they read: the events with a
            # note and the erasures (`_check_notes`).
            noted: list[dict[str, Any]] = []
            seen_head = False
            last: dict[str, Any] | None = None
            for page in _chain_pages(db, first, top):
                if start is not None and last is None:
                    opening = page[0]
                    if opening["seq"] != start["seq"] or opening["hash"] != start["hash"]:
                        cut = {"ok": False, "events": 0, "broken_at": start["seq"]}
                        return {**cut, "reason": "checkpoint"}, None
                    prev, page = start["hash"], page[1:]
                    last = {"seq": start["seq"], "hash": start["hash"]}
                if start is not None:
                    named = sorted({str(event["decision_id"]) for event in page} - rows_of.keys())
                    for at in range(0, len(named), _VERIFY_BATCH):
                        batch = named[at : at + _VERIFY_BATCH]
                        marks = ", ".join("?" for _ in batch)
                        for row in db.all(
                            f"SELECT {columns} FROM decisions WHERE id IN ({marks})", tuple(batch)
                        ):
                            records[row["id"]] = json.loads(row["record"])
                            rows_of[row["id"]] = row
                            owners[row["id"]] = row["org"]
                for event in page:
                    breaks = _chain_break(
                        event, prev, expected, records, rows_of, stored, states, owners, subjects
                    )
                    if breaks is not None:
                        return {"ok": False, "events": total, **breaks}, None
                    kind = str(event["event"])
                    if kind in _DECISION_STATES:
                        settled.setdefault(str(event["decision_id"]), set()).add(kind)
                        if kind == "decision.created":
                            created_at_seq.setdefault(str(event["decision_id"]), int(event["seq"]))
                    if notes and (kind == "note.erased" or '"note_ref":' in event["payload"]):
                        noted.append(event)
                    if graded_rounds is not None and kind in _WRITTEN_COMMITTED:
                        of_written.append(event)
                    seen_head = seen_head or event["hash"] == head
                    prev = event["hash"]
                    last = {"seq": event["seq"], "hash": event["hash"]}
            if start is not None and last is None:  # the checkpoint's event is gone
                cut = {"ok": False, "events": 0, "broken_at": start["seq"]}
                return {**cut, "reason": "checkpoint"}, None
        # Every store version's state is its newest event's, and none is unchained.
        if start is None and any(states.get(i) != row["state"] for i, row in stored.items()):
            return {"ok": False, "events": total, "reason": "store"}, None
        if expected:  # a checkpoint's event is gone: the chain was cut short of it
            cut = {"ok": False, "events": total, "broken_at": min(expected)}
            return {**cut, "reason": "checkpoint"}, None
        if head is not None and not seen_head:
            return {"ok": False, "events": total, "reason": "statement"}, None
        # The rows themselves, once the chain holds: a chain cut short reads as cut first.
        if start is None:
            found = _unsettled(rows_of, settled, created_at_seq)
            if found is not None:
                return {"ok": False, "events": total, **found, "reason": "decision"}, None
        since = {} if start is None else {"checkpoint": start}
        checked: dict[str, Any] = {}
        if graded_rounds is not None and (of_written or any(graded_rounds[:2])):
            from mendari.core.written import check_stored  # imports this module

            broken, counts = check_stored(of_written, *graded_rounds)
            if broken is not None:
                failed = {"ok": False, "events": total, "reason": "written", "written": counts}
                return ({**failed, "broken_at": broken} if broken else failed), None
            checked["written"] = counts
        if notes:
            broken, counts, unopenable = self._check_notes(noted, {row["id"]: row for row in kept})
            if broken is not None:
                failed = {"ok": False, "events": total, "broken_at": broken}
                return {**failed, "notes": counts}, None
            if unopenable:  # the chain holds; these notes cannot be checked with this ring
                status = {"ok": False, "events": total, "notes": counts}
                return {**status, "unopenable": unopenable}, None
            checked["notes"] = counts
        return {"ok": True, "events": total, "head": prev, **since, **checked}, last

    def _written_rows(
        self, db: _Session
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, str]]:
        """The grading rounds' rows a full verification checks (`core/written.py`
        `check_stored`): each round's state and stored report, each assignment's grader and
        grades, and each item row's item id."""
        rounds = {
            str(row["id"]): row for row in db.all("SELECT id, state, report FROM written_rounds")
        }
        assignments = {
            str(row["id"]): row
            for row in db.all(
                "SELECT id, round_id, item_id, user_id, grades FROM written_assignments"
            )
        }
        items = {
            str(row["id"]): str(row["item"]) for row in db.all("SELECT id, item FROM written_items")
        }
        return rounds, assignments, items

    def _check_notes(
        self, events: list[dict[str, Any]], kept: Mapping[str, Mapping[str, Any]]
    ) -> tuple[int | None, dict[str, int], dict[str, int]]:
        """The `seq` of the first event whose note does not match its commitment (or None); how
        many notes were checked, found erased, or left unchecked (sealed, and this deployment not
        in mode `encrypt`); and, by the key each names, the notes no key of this deployment's
        ring opens. A note that fails under a key the ring has, by the id it names, was altered:
        that breaks verification."""
        counts = {"checked": 0, "erased": 0, "unchecked": 0}
        unopenable: Counter[str] = Counter()
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
                # Unopenable only for want of a key this database held; anything else, a forged
                # key id included, was altered.
                named = token_key(str(row["sealed"]))
                if named not in missing:
                    return event["seq"], counts, dict(unopenable)
                unopenable[named] += 1
                continue
            if body is None:
                counts["unchecked"] += 1
                continue
            try:
                salt = base64.b64decode(str(body.get("salt", "")), validate=True)
            except (binascii.Error, ValueError):
                return event["seq"], counts, dict(unopenable)
            if _commitment(salt, str(body.get("text"))) != ref.get("sha256"):
                return event["seq"], counts, dict(unopenable)
            counts["checked"] += 1
        return None, counts, dict(unopenable)


def _decision(db: _Session, decision_id: str) -> dict[str, Any] | None:
    """One decision row (parsed), if this request's organization may see it."""
    scope, scoped = _scope()
    row = db.one(f"SELECT * FROM decisions d WHERE id = ? AND {scope}", (decision_id, *scoped))
    return None if row is None else _row(row)


def _events_of(
    db: _Session, decision_ids: list[str], *, kinds: Sequence[str] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """The events of these decisions, by decision, in chain order; scoped through the decision,
    so another organization's decisions come back empty. With `kinds`, only events of those."""
    scope, scoped = _scope()
    marks = ", ".join("?" for _ in decision_ids)
    only = f" AND e.event IN ({', '.join('?' for _ in kinds)})" if kinds else ""
    rows = db.all(
        "SELECT e.seq, e.decision_id, e.event, e.payload, e.created_at, e.prev_hash, e.hash "
        "FROM audit_events e JOIN decisions d ON d.id = e.decision_id "
        f"WHERE e.decision_id IN ({marks}){only} AND {scope} ORDER BY e.seq",
        (*decision_ids, *(kinds or ()), *scoped),
    )
    found: dict[str, list[dict[str, Any]]] = {decision_id: [] for decision_id in decision_ids}
    for row in rows:
        event = {k: v for k, v in row.items() if k != "decision_id"}
        found[row["decision_id"]].append({**event, "payload": json.loads(row["payload"])})
    return found


def _with_actions(item: dict[str, Any]) -> None:
    """Give a decision its `actions` (`actions.state` of its events, after `reveal_inputs`
    showed their notes), when it has any: a decision without is as it always was."""
    found = actions.state(item.get("events") or ())
    if found:
        item["actions"] = found


def _resolution(override: bool | None, denied: bool) -> dict[str, str]:
    """How a review resolves one held action: released when the reviewer confirmed the
    recommendation and did not refuse the action, denied otherwise. A review with no
    recommendation to confirm (`override` None) releases nothing."""
    if override is False and not denied:
        return {"disposition": "released", "reason": "confirmed"}
    if override is False:
        return {"disposition": "denied", "reason": "reviewer_denied"}
    return {"disposition": "denied", "reason": "overridden"}


def _asked(decision_id: str, action: Mapping[str, Any]) -> dict[str, Any]:
    """An action as a caller that asks about it is answered: ids, the declared name, the effect,
    the disposition, the reason and the arguments' hash. Never the arguments."""
    return {
        "id": action["id"],
        "decision_id": decision_id,
        **{
            key: action.get(key)
            for key in ("name", "effect", "disposition", "reason", "arguments_hash")
        },
    }


# A chained event joined with its decision's agent and record (`events()`, `event()`).
_JOINED = (
    "e.seq, e.decision_id, e.event, e.payload, e.created_at, e.prev_hash, e.hash, d.agent_id, "
    "d.agent_version, d.record FROM audit_events e JOIN decisions d ON d.id = e.decision_id"
)


def _joined(row: Mapping[str, Any]) -> dict[str, Any]:
    return {**row, "payload": json.loads(row["payload"]), "record": json.loads(row["record"])}


# -- searches (core/query.py) ----------------------------------------------------------------
# A refused input: the stored `refused` flag. The same predicate on the same column as
# `task_fhir.task_status`, so that what a FHIR Task renders and what a search on its status finds
# cannot disagree (a test checks both on every kind).
_REFUSED = "(d.refused = 1)"
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
    "agent": ("d.agent_id", "d.agent_version"),
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


_REVEALED_AT_ONCE = 500  # the decisions `labelled` opens the inputs of in one statement


def truth_label(row: Mapping[str, Any]) -> str | None:
    """What a decision's recommendation is measured against: the downstream outcome
    (`observed_label`, what really happened) when one was recorded, else the reviewer's
    `final_label`; None when neither is known."""
    return row.get("observed_label") or row.get("final_label") or None


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
    agent_id: str, record: Mapping[str, Any], label: str, labels: QuestionLabels | None
) -> None:
    """A final label must be one of the labels of the decision's primary question: those its
    answer gave probabilities for (the agent version that decided), or else, for a decision with
    no answer (refused before any provider ran, or its provider failed), the installed agent's.
    With neither, no label can be checked, so none is accepted."""
    primary = record.get("primary")
    answer = (record.get("answers") or {}).get(primary) or {}
    known = list(answer.get("probabilities") or ())
    if not known and labels is not None and isinstance(primary, str):
        known = list(labels(agent_id, primary) or ())
    if not known:
        raise ReviewError("this decision's labels are unknown here, so no label can be recorded")
    if label not in known:
        # The label is not echoed: a caller could have put anything there.
        raise ReviewError(f"not one of the labels of this decision's question ({sorted(known)})")


def _scope() -> tuple[str, tuple[str, ...]]:
    """The SQL condition (on the table aliased `d`) that keeps a query to the organization this
    request acts for; nothing in open mode."""
    org = tenancy.current()
    if org is None:
        return "1 = 1", ()
    return "d.org = ?", (org,)


def _membership(row: Any) -> dict[str, Any]:
    """A membership row as a dict, its roles a list."""
    data = dict(row)
    data["roles"] = json.loads(data["roles"])
    return data


def _key_status(row: Mapping[str, Any], now: str) -> str:
    """An API key's status at `now` (a `_now()` text), as `api_key` refuses it: revoked;
    expired (a key without an expiry is refused too); replaced (rotated, in its overlap); else
    active."""
    if row["revoked_at"] is not None:
        return "revoked"
    if not row["expires_at"] or row["expires_at"] <= now:
        return "expired"
    return "replaced" if row["replaced_by"] else "active"


def _invitation_status(row: Any, now: str) -> str:
    """An invitation's status at `now` (a `_now()` text): accepted, revoked, expired or open."""
    if row["accepted_at"] is not None:
        return "accepted"
    if row["revoked_at"] is not None:
        return "revoked"
    return "expired" if row["expires_at"] <= now else "open"


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
def _session_key(kind: str, value: str) -> str:
    """A session-table row's key: sha256(kind || 0x00 || value). Kinds are short lowercase names
    (never a NUL), so two (kind, value) pairs share a preimage only if they are equal. A value
    is any string: one decoded from JSON (a token's `jti`) may hold a lone surrogate, which
    `surrogatepass` encodes as no valid string encodes, so the bytes of every other value, and
    so every key made before, are unchanged."""
    if not _SESSION_KIND.fullmatch(kind):
        raise ValueError(f"not a session kind: {kind!r}")
    raw = value.encode("utf-8", "surrogatepass")
    return hashlib.sha256(kind.encode() + b"\x00" + raw).hexdigest()


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


def store_version_sha256(row: Mapping[str, Any]) -> str:
    """The hash an agent store version's `store_version.created` event chains of its row: the
    columns that never change (`STORE_VERSION_COLUMNS`, and `pulled` where a version has it), so
    a row changed past the store fails `verify` (LP-20e)."""
    hashed = {column: row[column] for column in STORE_VERSION_COLUMNS}
    if row.get("pulled") is not None:
        # A version pulled from a registry: where it came from is part of what never changes.
        # Every other version's hash is of those columns alone.
        hashed["pulled"] = row["pulled"]
    return _sha(hashed)


def _commitment(salt: bytes, text: str) -> str:
    """A note's commitment, chained in its event's `note_ref`: hiding (a 32-byte salt kept only
    with the note) and binding (SHA-256)."""
    return hashlib.sha256(NOTE_AAD + salt + text.encode()).hexdigest()


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
    """Whether an item's record holds its input itself: kept under PHI mode `off`, with its
    answers' evidence, which can quote it (`record`)."""
    record = item.get("record")
    return isinstance(record, dict) and record.get("input") is not None


def _withhold(record: dict[str, Any]) -> None:
    """A record without its input and its answers' evidence, which can quote it."""
    record["input"] = None
    for answer in (record.get("answers") or {}).values():
        if isinstance(answer, dict) and answer.get("evidence"):
            answer["evidence"] = []


def _show_note(event: dict[str, Any], views: Mapping[str, tuple[str, str | None]]) -> None:
    """An event's note as `reveal_inputs` shows it (`note`, `note_status`), if it has one."""
    ref = event["payload"].get("note_ref")
    if isinstance(ref, Mapping):
        # A missing row (never deleted, only emptied) is for `verify --notes` to report.
        event["note_status"], event["note"] = views.get(str(ref.get("id")), ("missing", None))


def _show_own_note(item: dict[str, Any], views: Mapping[str, tuple[str, str | None]]) -> None:
    """A rating's note (kept under the rating's id), as `reveal_inputs` shows it."""
    item["note_status"], item["note"] = views.get(str(item["id"]), (None, None))


# Events a verification reads per query (`_chain_pages`): what it holds of the chain at once.
_VERIFY_PAGE = 2000


def _chain_bound(first: int | None, top: int) -> tuple[str, tuple[int, ...]]:
    """The events from `first` (None: the start) up to `top`, as a condition on `seq`."""
    if first is None:
        return "seq <= ?", (top,)
    return "seq >= ? AND seq <= ?", (first, top)


def _chain_count(db: _Session, first: int | None, top: int | None) -> int:
    """How many events `_chain_pages` gives."""
    if top is None:
        return 0
    bound, values = _chain_bound(first, top)
    row = db.one(f"SELECT COUNT(*) AS n FROM audit_events WHERE {bound}", values)
    return 0 if row is None else int(row["n"])


def _chain_pages(
    db: _Session, first: int | None, top: int | None
) -> Iterator[list[dict[str, Any]]]:
    """The events from `first` (None: the start) up to `top` (None: there are none), in chain
    order, `_VERIFY_PAGE` at a time (keyset on `seq`)."""
    if top is None:
        return
    bound, values = _chain_bound(first, top)
    while True:
        page = db.all(
            f"SELECT * FROM audit_events WHERE {bound} ORDER BY seq LIMIT ?",
            (*values, _VERIFY_PAGE),
        )
        if page:
            yield page
        if len(page) < _VERIFY_PAGE:
            return
        bound, values = "seq > ? AND seq <= ?", (int(page[-1]["seq"]), top)


def _chain_break(
    event: Mapping[str, Any],
    prev: str,
    expected: dict[Any, Any],
    records: Mapping[str, Any],
    rows_of: Mapping[str, Any],
    stored: Mapping[str, Any],
    states: dict[str, str],
    owners: Mapping[str, str],
    subjects: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Where `event`, following `prev`, breaks the chain (`broken_at`, with a `reason` but for
    its hash's own), or None. `expected` loses its checkpoint and `states` takes its state."""
    chained = _chain_hash(
        prev, event["decision_id"], event["event"], event["payload"], event["created_at"]
    )
    if event["prev_hash"] != prev or event["hash"] != chained:
        return {"broken_at": event["seq"]}
    if expected.pop(event["seq"], event["hash"]) != event["hash"]:
        return {"broken_at": event["seq"], "reason": "checkpoint"}
    if event["event"] == "decision.created":
        record = records.get(event["decision_id"])
        if record is None or json.loads(event["payload"])["record_sha256"] != _sha(record):
            return {"broken_at": event["seq"]}
        if not _row_as_chained(rows_of[event["decision_id"]], record):
            return {"broken_at": event["seq"], "reason": "decision"}
    if event["event"] in STORE_STATES:
        version_id = str(event["decision_id"]).partition(":")[2]
        row = stored.get(version_id)
        if event["event"] == "store_version.created" and (
            row is None
            or json.loads(event["payload"]).get("version_sha256") != store_version_sha256(row)
        ):
            return {"broken_at": event["seq"], "reason": "store"}
        states[version_id] = STORE_STATES[event["event"]]
    if '"org":' in event["payload"] and not _attributed(event, owners, subjects):
        return {"broken_at": event["seq"], "reason": "organization"}
    return None


# Decisions a verification from a checkpoint reads per query (`_verify`).
_VERIFY_BATCH = 500
# A decision row's columns that never change once written, each as its chained record holds it
# (`_row_as_chained`): what lists, the review queue and statistics read instead of the record.
_DECISION_CHAINED = (
    "id", "created_at", "agent_id", "agent_version", "outcome", "recommendation", "priority",
)  # fmt: skip
# The events that make a decision and settle its review (`_unsettled`): only a reviewer's, as an
# outcome waits for a pending review (`record_outcome`).
_DECISION_STATES = ("decision.created", "review.resolved")


def _row_as_chained(row: Mapping[str, Any], record: Any) -> bool:
    """Whether a decision row's unchanging columns (`_DECISION_CHAINED`) are its chained
    record's: a row whose outcome, priority or agent was changed past the store (an escalated
    decision made to look automated, say) breaks `verify` at its `decision.created`."""
    if not isinstance(record, Mapping):
        return False
    found = record.get("agent")
    agent: Mapping[str, Any] = found if isinstance(found, Mapping) else {}
    chained = {
        "id": record.get("id"),
        "created_at": record.get("created_at"),
        "agent_id": agent.get("id"),
        "agent_version": agent.get("version"),
        "outcome": record.get("outcome"),
        "recommendation": record.get("recommendation"),
        "priority": record.get("priority"),
    }
    return all(row[column] == chained[column] for column in _DECISION_CHAINED)


def _unsettled(
    rows: Mapping[str, Mapping[str, Any]],
    settled: Mapping[str, set[str]],
    created: Mapping[str, int],
) -> dict[str, Any] | None:
    """What a full `verify` finds wrong with the decision rows themselves (None: nothing): a
    decision that awaits review (HUMAN_REVIEW or ABSTAIN) taken off the queue with no chained
    review (`broken_at` its `decision.created`, the earliest such), or rows that no
    `decision.created` chained (`unchained`, how many: a decision added past the store). Each
    row's events are those read (`settled`), of the same snapshot as the rows, so a decision
    made or resolved beside the verification is not read at all."""
    unchained = 0
    dropped: list[int] = []
    for decision_id, row in rows.items():
        events = settled.get(decision_id, set())
        if "decision.created" not in events:
            unchained += 1
            continue
        if (
            row["outcome"] in _REVIEWABLE
            and row["review_status"] != "pending"
            and "review.resolved" not in events
            and decision_id in created
        ):
            dropped.append(created[decision_id])
    if dropped:
        return {"broken_at": min(dropped)}
    return {"unchained": unchained} if unchained else None


def _at(checkpoint: Mapping[str, Any]) -> dict[str, Any]:
    """A checkpoint's place in the chain: its event's `seq` and `hash`."""
    return {"seq": checkpoint["seq"], "hash": checkpoint["hash"]}


def _checkpoints(db: _Session) -> list[dict[str, Any]]:
    """The recorded checkpoints, oldest first."""
    return db.all("SELECT seq, hash, created_at FROM audit_checkpoints ORDER BY seq")


# The fields are joined with US (\x1f), unescaped, so the join is unambiguous only while no
# field holds one: `prev` is 64 hex digits, `payload` canonical JSON (which escapes every control
# character), `created_at` an ISO time, and `decision_id` and `event` are refused with one by
# `_append_many` (`_SEPARATOR`). Every event already chained hashes as before: never change this.
_SEPARATOR = "\x1f"


def _chain_hash(prev: str, decision_id: str, event: str, payload: str, created_at: str) -> str:
    joined = _SEPARATOR.join((prev, decision_id, event, payload, created_at))
    return hashlib.sha256(joined.encode()).hexdigest()


# Per-day review counts in stats()["daily"]: human_override_rate = overridden / reviewed and
# observed_accuracy = agreed / labelled, over any run of days.
_DAILY_REVIEWS = ("reviewed", "overridden", "labelled", "agreed")


def _percentile(sorted_values: list[float], pct: float) -> float | None:
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1, round(pct / 100 * (len(sorted_values) - 1)))
    return sorted_values[index]
