# SPDX-License-Identifier: Apache-2.0
"""The registry: namespaces, publisher keys, immutable signed pack versions, and search.

- **Namespaces** are owned by publishers. Each has a publisher type (community, verified,
  institutional) that registry admins set after verification; a pack's own `publisher.type` is
  only what it claims. Each namespace registers Ed25519 public keys.
- **Publishing** (`PUT /v1/packs/{ns}/{name}/{version}`, with a namespace token) accepts an
  archive only if:
  - its signature verifies with one of the namespace's keys;
  - its manifest names this pack and version;
  - it extracts to a pack that loads with the signed digest, on the engine its pullers run
    (`origin="hub"`: RE2, the caps and the schema keyword list unless it is a reference pack of
    the registry's own release, ADR-0009);
  - that version does not exist yet.

  Versions are immutable.
- **What a version shows** (ADR-0012): only what the registry checked or rendered itself,
  in two groups. `checked`: the signing key, the archive's SHA-256, the pack digest, the engine
  it was checked on, the card (rendered here from the files the digest covers, never the
  uploaded one) and the review status. `claimed`: what the publisher says (its name and type,
  the risk level it declares, the licence and summary). An uploaded card, benchmark or
  leaderboard is never shown: it stays in the signed archive, unread.
- **Reads** (no token): packs, versions, archives, namespaces and their keys, and search, of
  every listed namespace (see Claims).
- **The chain.** Every write appends a hash-chained event in the same transaction
  (`hub_events`: namespaces, claims, publisher types, tokens, keys, publishes, site reports,
  reviews), and `RegistryStore.verify` (`mendari registry verify`) checks the chain and that
  every row it covers is named by its event. A registry created before the chain records what
  it held then (`registry.chain_started`). The chain is unkeyed, so with a signing key the
  registry signs its head for a day (`hub/checkpoint.py`, `RegistryStore.checkpoint`, `mendari
  registry checkpoint`): kept outside the database, the statement shows a chain rewritten from
  before it (`verify(head=…)`). Nothing of it is stored here.
- **Reviews.** A registry reviewer records the risk level a version's review found
  (`POST …/review`, the admin token until reviewer accounts exist); until then the version's
  risk level is shown as declared by the publisher.
- **Recalls** (`hub/yank.py`). A version is yanked by a signed `mendari.yank/1` record (the
  publisher's, signed with a namespace key, or the registry's, signed with its own key): it
  leaves the latest version, search and the namespace's list, and is still served by exact
  version with the record. An administrator may delete a version, with a reason: its archive's
  bytes are removed (no version still needing them), its row and a tombstone are kept, and it
  is never served or published again. Both are chained.
- **Limits and headers** (LP-18f). Every request counts against a rate in the registry's own
  database (`Limiter`, `MENDARI_REGISTRY_RATE_LIMITS`: `on` unless set, `log` or `off`, then
  overrides such as `registry_write=240/h:40`): reads per address, writes per token (or
  address), site reports per token. A refused one is `429` with `Retry-After`, before its body
  is read. Every answer carries the security headers, a CSP that loads nothing
  (`default-src 'none'; frame-ancestors 'none'`), `Cache-Control: no-store`, and HSTS but on
  this machine.
- **Claims** (LP-19c). An organization claims a namespace (`RegistryStore.claim`): the claim is
  its link to the namespace, and it is `pending` until a reviewer approves or refuses it, once
  there is a published version to look at. Until it is approved the publisher works as usual
  and nothing of the namespace is served to anyone else: every read takes a `Viewer`, and to the
  public a pending or refused namespace answers exactly as one that does not exist. A namespace
  with no claim (one from before claims) stays listed.
- **Counter-signature** (LP-19i, `hub/acceptance.py`). With a signing key
  (`MENDARI_REGISTRY_SIGNING_KEY`) the registry signs a `mendari.registry-acceptance/1` record
  for each version when it becomes listed: at publish in a listed namespace, or at approval for
  the versions published while the claim was pending. Never for a refused namespace, and never
  without the key. It is served as `checked.accepted` (null: none), chained `pack.accepted`,
  and `RegistryStore.countersign` (`mendari registry countersign`) signs the listed versions
  that have none. A version written straight into the database has none, and a client that
  trusts the registry's key refuses it.
- **Tokens** are random, shown once, and stored as SHA-256. The admin token comes from
  `MENDARI_REGISTRY_ADMIN_TOKEN`; without it the admin routes are closed.

Storage is the shared database layer (`core/db.py`): SQLite locally, Postgres deployed. Archives
are small (a pack is a few MB), so they live in the database.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import unicodedata
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TypeVar, cast

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError, field_validator
from pydantic_core import from_json, to_json
from starlette.concurrency import run_in_threadpool

from mendari import __version__
from mendari.core import clock
from mendari.core.audit import GENESIS, _canonical, _chain_hash
from mendari.core.db import Backend, Session, connect
from mendari.core.https_only import is_local_host
from mendari.core.pack import Pack
from mendari.core.rate_buckets import SCHEMA as RATE_SCHEMA
from mendari.core.rate_buckets import RateBuckets
from mendari.core.registry import version_key
from mendari.errors import HubError, MendariError
from mendari.evals import figures
from mendari.evals.share import (
    JOINERS,
    SiteReport,
    composite_members,
    failed_members,
    summary,
    where,
)
from mendari.hub import acceptance, archive, regression, yank
from mendari.hub import checkpoint as chain_checkpoint
from mendari.server.app import HSTS, SECURITY_HEADERS
from mendari.server.limits import Limiter, LimitSettings, Rate, client_ip, parse_rate

PublisherType = Literal["community", "verified", "institutional"]
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_VERSION = re.compile(r"^(?=.{1,64}$)\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
# Stored with each version published since the registry renders its own card (ADR-0012). A
# version stored without it was published before: its uploaded card, benchmark and leaderboard
# are never served.
VERSION_FORMAT = "mendari.registry-version/2"
UPLOADED = ("benchmark", "leaderboard", "card_markdown")
# The card the registry renders, at most (a shipped one is about 30 KB): it is stored with the
# version and sent with every read of the pack.
MAX_CARD_BYTES = 1_000_000
_LOCK = 0x0E4_4B0B  # the registry's Postgres advisory lock (the audit store has its own)

SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS hub_namespaces (
    name            TEXT PRIMARY KEY,
    publisher_type  TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hub_keys (
    key_id      TEXT PRIMARY KEY,
    namespace   TEXT NOT NULL,
    public_pem  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hub_tokens (
    token_sha256  TEXT PRIMARY KEY,
    namespace     TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hub_versions (
    namespace       TEXT NOT NULL,
    name            TEXT NOT NULL,
    version         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    archive_sha256  TEXT NOT NULL,
    metadata        TEXT NOT NULL,
    PRIMARY KEY (namespace, name, version)
);
CREATE TABLE IF NOT EXISTS hub_archives (
    archive_sha256  TEXT PRIMARY KEY,
    data            {blob} NOT NULL
);
CREATE TABLE IF NOT EXISTS hub_reports (
    id          TEXT PRIMARY KEY,
    namespace   TEXT NOT NULL,
    name        TEXT NOT NULL,
    version     TEXT NOT NULL,
    site        TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    report      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hub_events (
    seq         {serial},
    subject     TEXT NOT NULL,
    event       TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    prev_hash   TEXT NOT NULL,
    hash        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hub_reviews (
    id          TEXT PRIMARY KEY,
    namespace   TEXT NOT NULL,
    name        TEXT NOT NULL,
    version     TEXT NOT NULL,
    digest      TEXT NOT NULL,
    risk_level  INTEGER NOT NULL,
    reviewer    TEXT NOT NULL,
    note        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    ordinal     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS hub_yanks (
    namespace   TEXT NOT NULL,
    name        TEXT NOT NULL,
    version     TEXT NOT NULL,
    record      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (namespace, name, version)
);
CREATE TABLE IF NOT EXISTS hub_deletions (
    namespace   TEXT NOT NULL,
    name        TEXT NOT NULL,
    version     TEXT NOT NULL,
    reason      TEXT NOT NULL,
    deleted_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (namespace, name, version)
);
CREATE TABLE IF NOT EXISTS hub_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hub_publisher_tokens (
    id            TEXT PRIMARY KEY,
    token_sha256  TEXT NOT NULL UNIQUE,
    namespace     TEXT NOT NULL,
    org           TEXT NOT NULL,
    owner         TEXT NOT NULL,
    name          TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    revoked_at    TEXT,
    revoked_by    TEXT
);
CREATE TABLE IF NOT EXISTS hub_token_use (
    token_id       TEXT PRIMARY KEY,
    last_used_day  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hub_claims (
    namespace   TEXT PRIMARY KEY,
    org         TEXT NOT NULL,
    claimed_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    status      TEXT NOT NULL,
    decided_by  TEXT,
    decided_at  TEXT,
    note        TEXT
);
CREATE TABLE IF NOT EXISTS hub_acceptances (
    namespace   TEXT NOT NULL,
    name        TEXT NOT NULL,
    version     TEXT NOT NULL,
    record      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (namespace, name, version)
);
"""
    + RATE_SCHEMA
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def token_id(token: str) -> str:
    """How the chain names a token: the start of its SHA-256, never the token."""
    return _sha(token)[:16]


# An owned publisher token (LP-19b): how long one may live, and how long its labels may be.
MAX_TOKEN_DAYS = 365
_TOKEN_ID = re.compile(r"^[0-9a-f]{16}$")
_TOKEN_FIELDS = (
    "id, namespace, org, owner, name, created_at, expires_at, revoked_at, revoked_by"  # no hash
)


def _label(value: str, what: str, most: int) -> str:
    """One printable line of at most `most` characters (a token's name, owner or organization,
    and why an operator acted), or a `HubError` naming `what`."""
    if any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp", "Co", "Cs") for c in value):
        raise HubError(f"{what} is one printable line")
    value = " ".join(value.split())
    if not value or len(value) > most:
        raise HubError(f"{what} is 1 to {most} characters")
    return value


# A namespace claim (LP-19c). These names are the operator's alone to claim (`mendari registry
# claim`): each, and any name that starts with one and a dash.
RESERVED_NAMESPACES = ("mendari", "openhealth")
# A claim only holds a name until its first publish is reviewed: an organization holds a few.
MAX_PENDING_CLAIMS = 3
ClaimStatus = Literal["pending", "approved", "refused"]


def reserved_namespace(name: str) -> bool:
    return any(name == held or name.startswith(f"{held}-") for held in RESERVED_NAMESPACES)


@dataclass(frozen=True)
class Viewer:
    """Who a read is for. A namespace is *listed* when its claim is approved or it has none (one
    from before claims); one that is not is read only by its own token (`namespace`) and by a
    reviewer. To everyone else it answers as a namespace that does not exist."""

    namespace: str | None = None
    reviewer: bool = False

    def reads(self, namespace: str, unlisted: set[str]) -> bool:
        return self.reviewer or namespace == self.namespace or namespace not in unlisted


PUBLIC = Viewer()
REVIEWER = Viewer(reviewer=True)


# The rows the chain covers, by kind: the table, and how a row is named in the chain.
ROWS: dict[str, tuple[str, Callable[[Mapping[str, Any]], str]]] = {
    "namespaces": ("hub_namespaces", lambda row: str(row["name"])),
    "keys": ("hub_keys", lambda row: str(row["key_id"])),
    "tokens": ("hub_tokens", lambda row: str(row["token_sha256"])[:16]),
    "publisher_tokens": ("hub_publisher_tokens", lambda row: str(row["id"])),
    "claims": ("hub_claims", lambda row: str(row["namespace"])),
    "versions": (
        "hub_versions",
        lambda row: _pack_subject(row["namespace"], row["name"], row["version"]),
    ),
    "reports": ("hub_reports", lambda row: str(row["id"])),
    "reviews": ("hub_reviews", lambda row: str(row["id"])),
    "yanks": (
        "hub_yanks",
        lambda row: _pack_subject(row["namespace"], row["name"], row["version"]),
    ),
    "deletions": (
        "hub_deletions",
        lambda row: _pack_subject(row["namespace"], row["name"], row["version"]),
    ),
    "acceptances": (
        "hub_acceptances",
        lambda row: _pack_subject(row["namespace"], row["name"], row["version"]),
    ),
}


def _row_sha(row: Mapping[str, Any]) -> str:
    """A row as the chain holds it: the SHA-256 of its columns, canonical JSON."""
    return hashlib.sha256(_canonical(dict(row)).encode()).hexdigest()


def record_event(
    db: Session,
    event: str,
    subject: str,
    payload: Mapping[str, Any],
    *,
    row: tuple[str, str, tuple[Any, ...]] | None = None,
) -> None:
    """Append one chained event, in the caller's exclusive session (so with the write it
    records, or not at all). Hashed as the audit chain is (`core/audit.py`). `row` is the row the
    write left (its kind, and the clause and parameters that select it): the event holds its
    hash, so `verify` finds any later change to it, its removal, or a row with no event."""
    payload = dict(payload)
    if row is not None:
        kind, where, params = row
        table, name = ROWS[kind]
        written = db.one(f"SELECT * FROM {table} WHERE {where}", params)
        assert written is not None, (kind, where)
        payload["row"] = {"kind": kind, "key": name(written), "sha256": _row_sha(written)}
    last = db.one("SELECT hash FROM hub_events ORDER BY seq DESC LIMIT 1")
    prev = last["hash"] if last else GENESIS
    body, created = _canonical(payload), _now_ms()
    db.all(
        "INSERT INTO hub_events (subject, event, payload, created_at, prev_hash, hash) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (subject, event, body, created, prev, _chain_hash(prev, subject, event, body, created)),
    )


def _now_ms() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _pack_subject(namespace: str, name: str, version: str) -> str:
    return f"{namespace}/{name}@{version}"


_NOT_COUNTERSIGNED = (
    # The versions the clause selects that are neither deleted nor counter-signed yet.
    "SELECT v.namespace, v.name, v.version, v.archive_sha256, v.metadata FROM hub_versions v "
    "LEFT JOIN hub_deletions d ON d.namespace = v.namespace AND d.name = v.name "
    "AND d.version = v.version "
    "LEFT JOIN hub_acceptances a ON a.namespace = v.namespace AND a.name = v.name "
    "AND a.version = v.version "
    "WHERE d.version IS NULL AND a.version IS NULL"
)


_NOT_THE_CHECKPOINTS = (
    "the chain does not hold the checkpoint's head: it was rewritten or cut short from before "
    "the checkpoint, or the checkpoint is another registry's"
)


def _signing_key() -> Ed25519PrivateKey | None:
    """The registry's signing key, or None when unset. One that is set and is not a key is the
    operator's to fix: a `503` that says what the setting must be, never the value."""
    try:
        return yank.registry_key()
    except MendariError as error:
        raise HubError(f"the registry cannot counter-sign: {error}", 503) from None


def _not_genuine(db: Session, version: Mapping[str, Any]) -> str | None:
    """Why a stored version is not one the registry could have accepted (None: it is), checked
    again from what is stored before it is counter-signed after the fact: its archive is there
    and hashes to its name, is signed by a key of its namespace, and its signed manifest names
    this pack, version, digest and that key. A row written straight into the database with no
    archive a namespace key signed fails here."""
    stored = json.loads(version["metadata"])
    digest, publisher_key = stored.get("digest"), stored.get("key_id")
    if not (
        isinstance(digest, str) and digest and isinstance(publisher_key, str) and publisher_key
    ):
        return "it was stored without a digest or a publisher key"
    namespace, name, number = _key(version)
    held = db.one(
        "SELECT data FROM hub_archives WHERE archive_sha256 = ?", (version["archive_sha256"],)
    )
    if held is None:
        return "its archive is missing"
    data = bytes(held["data"])
    if hashlib.sha256(data).hexdigest() != version["archive_sha256"]:
        return "its archive is not the bytes its name hashes"
    listed = db.all("SELECT key_id, public_pem FROM hub_keys WHERE namespace = ?", (namespace,))
    try:
        keys = {
            str(k["key_id"]): archive.load_public_key(str(k["public_pem"]).encode()) for k in listed
        }
        if any(archive.key_id(key) != key_id for key_id, key in keys.items()):
            return "a key of its namespace is not the key its id names"
        verified = archive.verify(data, keys)
    except HubError as error:
        return str(error)
    manifest = verified.manifest
    if (manifest.get("id"), manifest.get("version")) != (f"{namespace}/{name}", number):
        return "its archive is another pack or version"
    if manifest.get("digest") != digest or verified.key_id != publisher_key:
        return "its archive is another build, or signed by another key, than it records"
    return None


def _countersign(
    db: Session, key: Ed25519PrivateKey, version: Mapping[str, Any], by: str, *, verified: bool
) -> bool:
    """Counter-sign one version as it becomes listed (`hub/acceptance.py`), in the caller's
    exclusive session: the record, and its chained `pack.accepted`. `verified`: the caller
    verified its archive itself (this publish, or `_not_genuine` just now); otherwise it is
    checked again from what is stored. False, and nothing written, for one that fails."""
    if not verified and _not_genuine(db, version) is not None:
        return False
    stored = json.loads(version["metadata"])
    digest, publisher_key = stored["digest"], stored["key_id"]
    namespace, name, number = _key(version)
    record = acceptance.sign(
        pack=f"{namespace}/{name}",
        version=number,
        digest=digest,
        archive_sha256=str(version["archive_sha256"]),
        publisher_key_id=publisher_key,
        key=key,
    )
    db.all(
        "INSERT INTO hub_acceptances (namespace, name, version, record, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (namespace, name, number, json.dumps(record, sort_keys=True), _now_ms()),
    )
    record_event(
        db,
        "pack.accepted",
        _pack_subject(namespace, name, number),
        {f: record[f] for f in ("pack", "version", "digest", "archive_sha256", "key_id")}
        | {"by": by},
        row=("acceptances", "namespace = ? AND name = ? AND version = ?", _key(version)),
    )
    return True


def stated_head(statement: Any, public: Ed25519PublicKey | None = None) -> str:
    """The head a registry checkpoint names (`hub/checkpoint.py`), once its signature holds with
    `public` (None: this registry's own key). A `HubError` says why it is not one."""
    if public is None:
        own = _signing_key()
        if own is None:
            raise HubError(
                f"a checkpoint is checked with the registry's key: set {yank.KEY_ENV}, or give "
                "its public key"
            )
        public = own.public_key()
    why = chain_checkpoint.problem(statement, public)
    if why is not None:
        raise HubError(f"the checkpoint is refused: {why}")
    return str(statement["head"])


class RegistryStore(RateBuckets):
    def __init__(
        self, target: str | Path, *, migrate_url: str | None = None, read_only: bool = False
    ):
        self._db: Backend = connect(
            str(target),
            schema=SCHEMA,
            migrate=_migrate,
            lock_key=_LOCK,
            migrate_url=migrate_url,
            read_only=read_only,
        )

    @property
    def backend(self) -> str:
        return self._db.kind

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> RegistryStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- namespaces, keys, tokens -------------------------------------------------------------

    def create_namespace(
        self, name: str, publisher_type: str, display_name: str, *, by: str = "admin"
    ) -> dict[str, Any]:
        if not _NAME.match(name):
            raise HubError("a namespace is lowercase letters, digits and dashes")
        with self._db.session(exclusive=True) as db:
            if db.one("SELECT name FROM hub_namespaces WHERE name = ?", (name,)):
                raise HubError(f"namespace {name!r} exists", 409)
            db.all(
                "INSERT INTO hub_namespaces (name, publisher_type, display_name, created_at) "
                "VALUES (?, ?, ?, ?)",
                (name, publisher_type, display_name or name, _now()),
            )
            record_event(
                db,
                "namespace.created",
                f"namespace:{name}",
                {"namespace": name, "publisher_type": publisher_type, "by": by},
                row=("namespaces", "name = ?", (name,)),
            )
        return self.namespace(name, REVIEWER)

    def set_publisher_type(
        self, name: str, publisher_type: str, *, by: str = "admin"
    ) -> dict[str, Any]:
        self.namespace(name, REVIEWER)
        with self._db.session(exclusive=True) as db:
            db.all(
                "UPDATE hub_namespaces SET publisher_type = ? WHERE name = ?",
                (publisher_type, name),
            )
            record_event(
                db,
                "namespace.publisher_type_set",
                f"namespace:{name}",
                {"namespace": name, "publisher_type": publisher_type, "by": by},
                row=("namespaces", "name = ?", (name,)),
            )
        return self.namespace(name, REVIEWER)

    def namespace(self, name: str, viewer: Viewer = PUBLIC) -> dict[str, Any]:
        """A namespace and its keys, with whether it is `listed`. Its `claim` is shown to its own
        token and to a reviewer only (None to the public, and for a namespace with none)."""
        with self._db.session(exclusive=False) as db:
            row = db.one("SELECT * FROM hub_namespaces WHERE name = ?", (name,))
            unlisted = self._unlisted(db)
            if row is None or not viewer.reads(name, unlisted):
                raise HubError(f"no namespace {name!r}", 404)
            keys = db.all(
                "SELECT key_id, public_pem, created_at FROM hub_keys WHERE namespace = ? "
                "ORDER BY created_at",
                (name,),
            )
            claim = None
            if (viewer.reviewer or viewer.namespace == name) and self._has_claims(db):
                claim = db.one("SELECT * FROM hub_claims WHERE namespace = ?", (name,))
        return {**row, "keys": keys, "listed": name not in unlisted, "claim": claim}

    # -- claims -------------------------------------------------------------------------------

    def _has_claims(self, db: Session) -> bool:
        """Whether the claims table is there: always, but in a database from before claims that
        is read as it is (`read_only`)."""
        return not self._db.read_only or bool(db.columns("hub_claims"))

    def _unlisted(self, db: Session) -> set[str]:
        """The namespaces that are not listed: each with a claim that is not approved. The one
        rule every read applies (`Viewer.reads`)."""
        if not self._has_claims(db):
            return set()
        rows = db.all("SELECT namespace FROM hub_claims WHERE status <> 'approved'")
        return {str(row["namespace"]) for row in rows}

    def claim(
        self,
        name: str,
        *,
        org: str,
        by: str,
        display_name: str = "",
        reserved: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Claim a namespace for an organization (LP-19c): the namespace is made, `community`,
        with its claim `pending`, in one transaction. Refused: a reserved name (but to the
        operator, `reserved`), a name that exists (409), and an organization that already holds
        `MAX_PENDING_CLAIMS` pending claims (409). Chained `namespace.created` then
        `namespace.claimed`, each with the organization (and why, when an operator claimed)."""
        if not _NAME.match(name):
            raise HubError("a namespace is lowercase letters, digits and dashes")
        if reserved_namespace(name) and not reserved:
            raise HubError(f"the namespace {name!r} is reserved")
        org, by = _label(org, "the organization", 200), _label(by, "the claimant", 200)
        shown = _label(display_name, "the display name", 200) if display_name.strip() else name
        payload: dict[str, Any] = {"namespace": name, "org": org, "by": by}
        if reason is not None:
            payload["reason"] = _label(reason, "the reason", 200)
        with self._db.session(exclusive=True) as db:
            if db.one("SELECT name FROM hub_namespaces WHERE name = ?", (name,)):
                raise HubError(f"namespace {name!r} exists", 409)
            held = db.one(
                "SELECT COUNT(*) AS n FROM hub_claims WHERE org = ? AND status = 'pending'", (org,)
            )
            if held is not None and int(held["n"]) >= MAX_PENDING_CLAIMS:
                raise HubError(
                    f"an organization holds {MAX_PENDING_CLAIMS} pending claims at most: "
                    "publish to one and have it reviewed first",
                    409,
                )
            created = _now()
            db.all(
                "INSERT INTO hub_namespaces (name, publisher_type, display_name, created_at) "
                "VALUES (?, 'community', ?, ?)",
                (name, shown, created),
            )
            db.all(
                "INSERT INTO hub_claims (namespace, org, claimed_by, created_at, status) "
                "VALUES (?, ?, ?, ?, 'pending')",
                (name, org, by, created),
            )
            record_event(
                db,
                "namespace.created",
                f"namespace:{name}",
                {"namespace": name, "publisher_type": "community", "org": org, "by": by},
                row=("namespaces", "name = ?", (name,)),
            )
            record_event(
                db,
                "namespace.claimed",
                f"namespace:{name}",
                payload,
                row=("claims", "namespace = ?", (name,)),
            )
        (claimed,) = self.claims(namespace=name)
        return claimed

    def decide_claim(self, name: str, *, approve: bool, by: str, note: str = "") -> dict[str, Any]:
        """Approve or refuse a pending claim, once. Approval needs a published version that is
        not deleted (there is nothing to review otherwise); from then the namespace is listed,
        with every version it has and will have, and each one it has is counter-signed (when
        the registry has a signing key). A refused namespace stays taken and never lists.
        Chained `namespace.approved` or `namespace.refused` with the organization."""
        by = _label(by, "who decides", 200)
        note = _label(note, "the note", 300) if note.strip() else ""
        own = _signing_key() if approve else None
        with self._db.session(exclusive=True) as db:
            claim = db.one("SELECT * FROM hub_claims WHERE namespace = ?", (name,))
            if claim is None:
                raise HubError(f"no claim on namespace {name!r}", 404)
            if claim["status"] != "pending":
                raise HubError(f"the claim on {name!r} is already {claim['status']}", 409)
            if approve and not db.one(
                "SELECT v.version FROM hub_versions v LEFT JOIN hub_deletions d ON "
                "d.namespace = v.namespace AND d.name = v.name AND d.version = v.version "
                "WHERE v.namespace = ? AND d.version IS NULL LIMIT 1",
                (name,),
            ):
                raise HubError(
                    f"namespace {name!r} has no published version to review: a claim is "
                    "approved with its first publish",
                    409,
                )
            status = "approved" if approve else "refused"
            db.all(
                "UPDATE hub_claims SET status = ?, decided_by = ?, decided_at = ?, note = ? "
                "WHERE namespace = ?",
                (status, by, _now(), note, name),
            )
            record_event(
                db,
                f"namespace.{status}",
                f"namespace:{name}",
                {"namespace": name, "org": claim["org"], "by": by, "note": note},
                row=("claims", "namespace = ?", (name,)),
            )
            if own is not None:  # listed from here: what was published while it was pending
                waiting = db.all(f"{_NOT_COUNTERSIGNED} AND v.namespace = ?", (name,))
                for version in sorted(
                    waiting, key=lambda v: (v["name"], version_key(v["version"]))
                ):
                    # Checked again from what is stored: one that is not genuine stays without
                    # a record, and every client of this registry refuses it.
                    _countersign(db, own, version, by, verified=False)
        (decided,) = self.claims(namespace=name)
        return decided

    def claims(
        self,
        *,
        org: str | None = None,
        status: str | None = None,
        namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        """The claims, oldest first: of one organization, in one status, or all (the operator's
        and a reviewer's view)."""
        clauses, params = [], []
        for column, value in (("org", org), ("status", status), ("namespace", namespace)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._db.session(exclusive=False) as db:
            if not self._has_claims(db):
                return []
            return db.all(
                f"SELECT * FROM hub_claims{where} ORDER BY created_at, namespace", tuple(params)
            )

    def add_key(self, namespace: str, public_pem: str, *, by: str = "admin") -> str:
        key = archive.load_public_key(public_pem.encode())
        key_id = archive.key_id(key)
        self.namespace(namespace, REVIEWER)
        with self._db.session(exclusive=True) as db:
            owner = db.one("SELECT namespace FROM hub_keys WHERE key_id = ?", (key_id,))
            if owner and owner["namespace"] != namespace:
                raise HubError("that key belongs to another namespace", 409)
            if owner is None:
                db.all(
                    "INSERT INTO hub_keys (key_id, namespace, public_pem, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (key_id, namespace, archive.public_pem(key).decode(), _now()),
                )
                record_event(
                    db,
                    "key.added",
                    f"namespace:{namespace}",
                    {"namespace": namespace, "key_id": key_id, "by": by},
                    row=("keys", "key_id = ?", (key_id,)),
                )
        return key_id

    def issue_token(self, namespace: str, *, by: str = "admin") -> str:
        self.namespace(namespace, REVIEWER)
        token = "ohr_" + secrets.token_urlsafe(32)
        with self._db.session(exclusive=True) as db:
            db.all(
                "INSERT INTO hub_tokens (token_sha256, namespace, created_at) VALUES (?, ?, ?)",
                (_sha(token), namespace, _now()),
            )
            record_event(
                db,
                "token.issued",
                f"namespace:{namespace}",
                {"namespace": namespace, "token_id": token_id(token), "by": by},
                row=("tokens", "token_sha256 = ?", (_sha(token),)),
            )
        return token

    def token_namespace(self, token: str) -> str | None:
        """The namespace of a token from before owned tokens (`issue_token`): no owner, no
        expiry, never revoked."""
        with self._db.session(exclusive=False) as db:
            row = db.one("SELECT namespace FROM hub_tokens WHERE token_sha256 = ?", (_sha(token),))
        return None if row is None else str(row["namespace"])

    def mint_token(
        self,
        namespace: str,
        *,
        org: str,
        owner: str,
        name: str,
        days: int,
        by: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """A publisher token for `namespace` that belongs to `owner` in `org`, lives `days`
        (1 to `MAX_TOKEN_DAYS`) and can be revoked (LP-19b). The answer holds the token, the one
        time it is shown: the store keeps its SHA-256. Chained `token.issued` with the
        organization, the owner and the expiry (and why, when an operator minted it)."""
        self.namespace(namespace, REVIEWER)
        if isinstance(days, bool) or not 1 <= days <= MAX_TOKEN_DAYS:
            raise HubError(f"a token lives 1 to {MAX_TOKEN_DAYS} days")
        org, owner = _label(org, "the organization", 200), _label(owner, "the owner", 200)
        name = _label(name, "the token's name", 80)
        payload: dict[str, Any] = {}
        if reason is not None:
            payload["reason"] = _label(reason, "the reason", 200)
        token = "ohr_" + secrets.token_urlsafe(32)
        created = _now()
        expires = (datetime.fromisoformat(created) + timedelta(days=days)).isoformat(
            timespec="seconds"
        )
        with self._db.session(exclusive=True) as db:
            db.all(
                "INSERT INTO hub_publisher_tokens (id, token_sha256, namespace, org, owner, "
                "name, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (token_id(token), _sha(token), namespace, org, owner, name, created, expires),
            )
            record_event(
                db,
                "token.issued",
                f"namespace:{namespace}",
                {
                    "namespace": namespace,
                    "token_id": token_id(token),
                    "org": org,
                    "owner": owner,
                    "expires_at": expires,
                    "by": by,
                    **payload,
                },
                row=("publisher_tokens", "id = ?", (token_id(token),)),
            )
        (minted,) = self.tokens(ids=(token_id(token),))
        return {**minted, "token": token}

    def tokens(
        self,
        *,
        org: str | None = None,
        namespace: str | None = None,
        ids: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """The owned tokens, oldest first: of one organization, of one namespace, or all (the
        operator's view). Each with the last day it was used, never its secret or its hash."""
        clauses, params = [], []
        for column, value in (("org", org), ("namespace", namespace)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if ids is not None:
            clauses.append(f"id IN ({', '.join('?' * len(ids))})" if ids else "1 = 0")
            params.extend(ids)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._db.session(exclusive=False) as db:
            if not db.columns("hub_publisher_tokens"):  # read-only, from before owned tokens
                return []
            rows = db.all(
                f"SELECT {_TOKEN_FIELDS} FROM hub_publisher_tokens{where} ORDER BY created_at, id",
                tuple(params),
            )
            used = {
                str(r["token_id"]): str(r["last_used_day"])
                for r in db.all("SELECT token_id, last_used_day FROM hub_token_use")
            }
        return [{**row, "last_used_day": used.get(str(row["id"]))} for row in rows]

    def revoke_token(
        self, id: str, *, org: str | None, by: str, reason: str | None = None
    ) -> dict[str, Any]:
        """Revoke an owned token, at once and for good. `org` is the organization acting: a
        token of another one answers as one that does not exist (None: the operator, any
        organization). Chained `token.revoked` with the row as it now stands; revoking a
        revoked token changes nothing and chains nothing."""
        payload: dict[str, Any] = {}
        if reason is not None:
            payload["reason"] = _label(reason, "the reason", 200)
        with self._db.session(exclusive=True) as db:
            row = (
                db.one("SELECT * FROM hub_publisher_tokens WHERE id = ?", (id,))
                if _TOKEN_ID.fullmatch(id)
                else None
            )
            if row is None or (org is not None and row["org"] != org):
                raise HubError("no such token", 404)
            if row["revoked_at"] is None:
                db.all(
                    "UPDATE hub_publisher_tokens SET revoked_at = ?, revoked_by = ? WHERE id = ?",
                    (_now(), by, id),
                )
                record_event(
                    db,
                    "token.revoked",
                    f"namespace:{row['namespace']}",
                    {
                        "namespace": row["namespace"],
                        "token_id": id,
                        "org": row["org"],
                        "by": by,
                        **payload,
                    },
                    row=("publisher_tokens", "id = ?", (id,)),
                )
        (revoked,) = self.tokens(ids=(id,))
        return revoked

    def publisher(self, token: str) -> dict[str, str] | None:
        """Whose an owned token is (`id`, `namespace`, `org`, `owner`), in one read; None for a
        token that is revoked, expired, or not an owned token at all."""
        with self._db.session(exclusive=False) as db:
            row = db.one(
                "SELECT id, namespace, org, owner, expires_at, revoked_at "
                "FROM hub_publisher_tokens WHERE token_sha256 = ?",
                (_sha(token),),
            )
        if row is None or row["revoked_at"] is not None:
            return None
        if datetime.fromisoformat(row["expires_at"]) <= datetime.fromisoformat(_now()):
            return None
        return {key: str(row[key]) for key in ("id", "namespace", "org", "owner")}

    def token_used(self, id: str) -> None:
        """Note that an owned token was used today (the deployment's day, `core/clock.py`).
        Outside the chain, as the rate buckets are, and a write only on a token's first use of
        a day."""
        day = clock.today().isoformat()
        with self._db.session(exclusive=False) as db:
            row = db.one("SELECT last_used_day FROM hub_token_use WHERE token_id = ?", (id,))
            if row is not None and str(row["last_used_day"]) >= day:
                return
            db.all(
                "INSERT INTO hub_token_use (token_id, last_used_day) VALUES (?, ?) "
                "ON CONFLICT (token_id) DO UPDATE SET last_used_day = excluded.last_used_day "
                "WHERE hub_token_use.last_used_day < excluded.last_used_day",
                (id, day),
            )

    # -- packs ----------------------------------------------------------------------------------

    def publish(
        self, namespace: str, name: str, version: str, data: bytes, *, by: str = "admin"
    ) -> dict[str, Any]:
        keys = {
            k["key_id"]: archive.load_public_key(k["public_pem"].encode())
            for k in self.namespace(namespace, REVIEWER)["keys"]
        }
        if not keys:
            raise HubError(f"namespace {namespace!r} has no publisher key")
        own = _signing_key()  # a key that is set and is not one fails here, before any work
        self._refuse_existing(namespace, name, version)  # before any work on the archive
        verified = archive.verify(data, keys)
        if (verified.manifest.get("id"), verified.manifest.get("version")) != (
            f"{namespace}/{name}",
            version,
        ):
            raise HubError("the archive is not this pack and version")
        with tempfile.TemporaryDirectory() as scratch:
            try:
                # Checked on the engine its pullers run, against this release's reference packs.
                pack = archive.extract(verified, Path(scratch) / "pack", origin="hub")
                metadata = describe(pack, verified)
                if regression.enabled():  # in a child process of its own (`hub/regression.py`)
                    with regression.slot():  # at most a few at once; else 503, try again
                        metadata["regression"] = regression.check(
                            Path(scratch) / "pack", self._previous_check(namespace, name, version)
                        )
            except HubError:
                raise
            except MendariError as error:
                raise HubError(f"the pack does not load: {error}") from None
            except Exception as error:  # the publisher's content: named, never a 500
                raise HubError(f"the pack does not load ({type(error).__name__})") from None
        if len(str(metadata.get("card_markdown") or "").encode()) > MAX_CARD_BYTES:
            raise HubError(f"the pack's Decision Card is larger than {MAX_CARD_BYTES} bytes")
        try:  # as it is stored and every answer serializes it, first: versions are immutable
            stored = json.dumps(metadata, sort_keys=True)
            to_json(metadata)
        except Exception as error:
            raise HubError(
                f"the pack's metadata cannot be served ({type(error).__name__})"
            ) from None
        with self._db.session(exclusive=True) as db:
            if db.one(
                "SELECT version FROM hub_versions WHERE namespace = ? AND name = ? AND version = ?",
                (namespace, name, version),
            ):
                raise HubError(
                    f"{namespace}/{name}@{version} is published; versions are immutable", 409
                )
            if not db.one(
                "SELECT archive_sha256 FROM hub_archives WHERE archive_sha256 = ?",
                (verified.sha256,),
            ):
                db.all(
                    "INSERT INTO hub_archives (archive_sha256, data) VALUES (?, ?)",
                    (verified.sha256, data),
                )
            db.all(
                "INSERT INTO hub_versions (namespace, name, version, created_at, archive_sha256, "
                "metadata) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    namespace,
                    name,
                    version,
                    _now(),
                    verified.sha256,
                    stored,
                ),
            )
            claim = db.one("SELECT org, status FROM hub_claims WHERE namespace = ?", (namespace,))
            record_event(
                db,
                "pack.published",
                _pack_subject(namespace, name, version),
                {
                    "pack": f"{namespace}/{name}",
                    "version": version,
                    "digest": metadata["digest"],
                    "archive_sha256": verified.sha256,
                    "key_id": verified.key_id,
                    "by": by,
                    # The organization the namespace's claim links it to (None: no claim), and
                    # whether the version was listed when it was published.
                    "org": None if claim is None else claim["org"],
                    "listed": claim is None or claim["status"] == "approved",
                },
                row=(
                    "versions",
                    "namespace = ? AND name = ? AND version = ?",
                    (namespace, name, version),
                ),
            )
            if own is not None and (claim is None or claim["status"] == "approved"):
                # Listed as it is published: counter-signed with it. A pending namespace's
                # versions are signed at approval, a refused one's never.
                written = db.one(
                    "SELECT * FROM hub_versions WHERE namespace = ? AND name = ? AND version = ?",
                    (namespace, name, version),
                )
                assert written is not None
                _countersign(db, own, written, by, verified=True)
        return self.version(namespace, name, version, REVIEWER)

    def checkpoint(self, *, previous: Mapping[str, Any] | None = None) -> dict[str, str]:
        """The signed statement of the chain's head today (UTC), for the operator to keep
        where the registry cannot write (`hub/checkpoint.py`). Read-only: nothing is stored or
        chained. It vouches for the chain, so it refuses one that does not verify, and, given
        the `previous` statement, one that no longer holds that statement's head."""
        own = _signing_key()
        if own is None:
            raise HubError(
                f"this registry has no signing key: set {yank.KEY_ENV} (32 random bytes in "
                "base64) to sign a checkpoint"
            )
        head = None if previous is None else stated_head(previous, own.public_key())
        chain = self.verify(head=head)
        if not chain["ok"]:
            raise HubError(
                f"no checkpoint was signed: the registry's chain does not verify "
                f"({chain['problem']}). A statement of its head would vouch for it",
                409,
            )
        if not chain["events"]:
            raise HubError("no checkpoint was signed: the registry's chain has no event yet", 409)
        return chain_checkpoint.sign(str(chain["head"]), datetime.now(UTC).date(), own)

    def countersign(
        self, *, by: str, checkpoint: Mapping[str, Any] | None = None
    ) -> dict[str, list[str]]:
        """Counter-sign every listed version that has no record (LP-19i): those from before
        the registry counter-signed, or published while it had no signing key. Never a deleted
        version, nor one of a pending or refused namespace. Run again it signs nothing.

        It signs after the fact, so it trusts nothing it has not checked: it refuses to run on
        a database whose chain does not verify (`verify`: a planted or changed row), and each
        version is checked again from what is stored (`_not_genuine`: its archive, signed by a
        key of its namespace, naming this pack, version and digest). `signed` names the versions
        signed; `skipped` those that failed that check, each with why.

        The chain is unkeyed, so one rewritten whole verifies. Given a `checkpoint` (a statement
        `checkpoint` signed earlier, kept outside the database), the chain must also still hold
        the head it names, or nothing is signed."""
        own = _signing_key()
        if own is None:
            raise HubError(
                f"this registry has no signing key: set {yank.KEY_ENV} (32 random bytes in "
                "base64) to counter-sign"
            )
        head = None if checkpoint is None else stated_head(checkpoint, own.public_key())
        chain = self.verify(head=head)
        if not chain["ok"]:
            raise HubError(
                f"nothing was counter-signed: the registry's chain does not verify "
                f"({chain['problem']}). Find out why with `mendari registry verify` first: "
                "counter-signing would vouch for rows the registry never accepted",
                409,
            )
        by = _label(by, "who counter-signs", 200)
        signed: list[str] = []
        skipped: list[str] = []
        with self._db.session(exclusive=True) as db:
            unlisted = self._unlisted(db)
            waiting = [v for v in db.all(_NOT_COUNTERSIGNED) if v["namespace"] not in unlisted]
            for version in sorted(
                waiting, key=lambda v: (v["namespace"], v["name"], version_key(v["version"]))
            ):
                why = _not_genuine(db, version)
                if why is None:
                    _countersign(db, own, version, by, verified=True)
                    signed.append(_pack_subject(*_key(version)))
                else:
                    skipped.append(f"{_pack_subject(*_key(version))}: {why}")
        return {"signed": signed, "skipped": skipped}

    def _accepted(
        self, db: Session, where: str, params: tuple[str, ...]
    ) -> dict[tuple[str, str, str], dict[str, Any]]:
        """The counter-signature of each version the clause selects that has one. None in a
        database from before them that is read as it is (`read_only`)."""
        if self._db.read_only and not db.columns("hub_acceptances"):
            return {}
        rows = db.all(f"SELECT * FROM hub_acceptances {where}", params)
        return {_key(row): json.loads(row["record"]) for row in rows}

    def _refuse_existing(self, namespace: str, name: str, version: str) -> None:
        with self._db.session(exclusive=False) as db:
            if db.one(
                "SELECT version FROM hub_versions WHERE namespace = ? AND name = ? AND version = ?",
                (namespace, name, version),
            ):
                raise HubError(
                    f"{namespace}/{name}@{version} is published; versions are immutable", 409
                )

    def _previous_check(self, namespace: str, name: str, version: str) -> dict[str, Any] | None:
        """The measured regression check of the newest version before `version`, with its
        version and whether it is recalled since: the gate's baseline. A recalled version stays
        one, so recalling a good version cannot hide a later regression."""
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT version, metadata FROM hub_versions WHERE namespace = ? AND name = ?",
                (namespace, name),
            )
            recalls = _recalls(db, "WHERE namespace = ? AND name = ?", (namespace, name))
        earlier = []
        for row in rows:
            check = json.loads(row["metadata"]).get("regression")
            if (
                version_key(row["version"]) < version_key(version)
                and isinstance(check, dict)
                and check.get("status") == "measured"
            ):
                earlier.append((version_key(row["version"]), row["version"], check))
        if not earlier:
            return None
        _, found, check = max(earlier, key=lambda item: item[0])
        return {**check, "version": found, "recalled": (namespace, name, found) in recalls}

    def versions(self, namespace: str, name: str, viewer: Viewer = PUBLIC) -> list[dict[str, Any]]:
        with self._db.session(exclusive=False) as db:
            unlisted = self._unlisted(db)
            if not viewer.reads(namespace, unlisted):
                return []
            rows = db.all(
                "SELECT * FROM hub_versions WHERE namespace = ? AND name = ?", (namespace, name)
            )
            reviews = _reviews(db, "WHERE namespace = ? AND name = ?", (namespace, name))
            recalls = _recalls(db, "WHERE namespace = ? AND name = ?", (namespace, name))
            accepted = self._accepted(db, "WHERE namespace = ? AND name = ?", (namespace, name))
        listed = namespace not in unlisted
        return sorted(
            (
                _version_row(
                    r, reviews.get(_key(r)), recalls.get(_key(r)), listed, accepted.get(_key(r))
                )
                for r in rows
            ),
            key=lambda v: version_key(v["version"]),
        )

    def version(
        self, namespace: str, name: str, version: str, viewer: Viewer = PUBLIC
    ) -> dict[str, Any]:
        with self._db.session(exclusive=False) as db:
            row = db.one(
                "SELECT * FROM hub_versions WHERE namespace = ? AND name = ? AND version = ?",
                (namespace, name, version),
            )
            selected = "WHERE namespace = ? AND name = ? AND version = ?"
            reviews = _reviews(db, selected, (namespace, name, version))
            recalls = _recalls(db, selected, (namespace, name, version))
            accepted = self._accepted(db, selected, (namespace, name, version))
            unlisted = self._unlisted(db)
        if row is None or not viewer.reads(namespace, unlisted):
            raise HubError(f"no pack {namespace}/{name}@{version}", 404)
        return _version_row(
            row,
            reviews.get(_key(row)),
            recalls.get(_key(row)),
            namespace not in unlisted,
            accepted.get(_key(row)),
        )

    def archive(self, sha256: str, viewer: Viewer = PUBLIC) -> bytes:
        """An archive's bytes, for a viewer who reads a namespace with a version of it."""
        with self._db.session(exclusive=False) as db:
            row = db.one("SELECT data FROM hub_archives WHERE archive_sha256 = ?", (sha256,))
            unlisted = self._unlisted(db)
            holders = (
                db.all("SELECT namespace FROM hub_versions WHERE archive_sha256 = ?", (sha256,))
                if row is not None and unlisted
                else []
            )
        if row is None or (
            holders and not any(viewer.reads(str(h["namespace"]), unlisted) for h in holders)
        ):
            raise HubError("archive not found", 404)
        return bytes(row["data"])

    def add_report(
        self,
        namespace: str,
        name: str,
        version: str,
        site: str,
        submitted_by: str,
        report: SiteReport,
        *,
        by: str = "admin",
        viewer: Viewer = PUBLIC,
    ) -> dict[str, Any]:
        """A site's evaluation of a published version (`mendari.site-report/1`, already
        validated: nothing in it but names and numbers). `viewer` is the submitter: a version
        they cannot read answers as one that does not exist."""
        found = self.version(namespace, name, version, viewer)
        if (report.pack.id, report.pack.version) != (f"{namespace}/{name}", version):
            raise HubError("the report is about another pack or version")
        names = found.get("names")
        if not isinstance(names, dict):
            raise HubError(
                f"{namespace}/{name}@{version} was published before site reports were checked "
                "against their pack; publish a new version to receive them",
                409,
            )
        composite = any(p.type in JOINERS for p in report.providers.values()) or any(
            failed_members(key) for key in report.errors
        )
        if composite and not isinstance(names.get("members"), dict):
            raise HubError(
                f"{namespace}/{name}@{version} was published before reports from composite "
                "providers were checked against their pack; publish a new version to receive them",
                409,
            )
        problem = _unknown_names(report, names, found.get("primary"))
        if problem:
            raise HubError(f"the report names {problem} the pack does not have")
        shared = report.model_dump(mode="json")
        report_id = secrets.token_hex(8)
        with self._db.session(exclusive=True) as db:
            db.all(
                "INSERT INTO hub_reports (id, namespace, name, version, site, submitted_by, "
                "created_at, report) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    report_id,
                    namespace,
                    name,
                    version,
                    site,
                    submitted_by,
                    _now(),
                    json.dumps(shared),
                ),
            )
            record_event(
                db,
                "report.shared",
                _pack_subject(namespace, name, version),
                {
                    "report_id": report_id,
                    "pack": f"{namespace}/{name}",
                    "version": version,
                    "site": site,
                    "submitted_by": submitted_by,
                    "by": by,
                },
                row=("reports", "id = ?", (report_id,)),
            )
        return {"id": report_id, "site": site, "version": version, **summary(shared)}

    def reports(self, namespace: str, name: str, viewer: Viewer = PUBLIC) -> list[dict[str, Any]]:
        with self._db.session(exclusive=False) as db:
            if not viewer.reads(namespace, self._unlisted(db)):
                return []
            rows = db.all(
                "SELECT id, version, site, submitted_by, created_at, report FROM hub_reports "
                "WHERE namespace = ? AND name = ? ORDER BY created_at",
                (namespace, name),
            )
        shown = ("id", "version", "site", "submitted_by", "created_at")
        listed = []
        for r in rows:
            # A row stored before reports were checked (mendari.site-report/1) may hold case ids
            # or be malformed: it is not served.
            try:
                report = SiteReport.model_validate_json(r["report"])
            except ValidationError:
                continue
            # Unverified: any publisher token may submit under any site name until sites sign
            # what they share (the site evaluation network). The Hub does not show them.
            shared = report.model_dump(mode="json")
            listed.append({**{k: r[k] for k in shown}, "verified": False, **summary(shared)})
        return listed

    def review(
        self,
        namespace: str,
        name: str,
        version: str,
        *,
        risk_level: int,
        note: str,
        reviewer: str,
    ) -> dict[str, Any]:
        """A registry reviewer's review of one version: the risk level the review found (which
        may differ from the one the publisher declared) and a short note. The newest review of a
        version is the one shown; each is chained."""
        found = self.version(namespace, name, version, REVIEWER)
        review_id = secrets.token_hex(8)
        with self._db.session(exclusive=True) as db:
            ordinal = db.one("SELECT COALESCE(MAX(ordinal), 0) + 1 AS n FROM hub_reviews")
            db.all(
                "INSERT INTO hub_reviews (id, namespace, name, version, digest, risk_level, "
                "reviewer, note, created_at, ordinal) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    review_id,
                    namespace,
                    name,
                    version,
                    found["digest"],
                    risk_level,
                    reviewer,
                    note,
                    _now_ms(),
                    int(ordinal["n"]) if ordinal else 1,
                ),
            )
            record_event(
                db,
                "pack.reviewed",
                _pack_subject(namespace, name, version),
                {
                    "review_id": review_id,
                    "pack": f"{namespace}/{name}",
                    "version": version,
                    "digest": found["digest"],
                    "risk_level": risk_level,
                    "declared_risk_level": found["claimed"]["risk"]["level"],
                    "reviewer": reviewer,
                    "note": note,
                },
                row=("reviews", "id = ?", (review_id,)),
            )
        return self.version(namespace, name, version, REVIEWER)

    def recalled(self, viewer: Viewer = PUBLIC) -> list[dict[str, Any]]:
        """Every recalled version the viewer reads: pack, version, its yank record and its
        deletion."""
        with self._db.session(exclusive=False) as db:
            recalls = _recalls(db, "", ())
            unlisted = self._unlisted(db)
        return [
            {"pack": f"{ns}/{name}", "version": version, **{"yank": None, "deleted": None, **r}}
            for (ns, name, version), r in sorted(recalls.items())
            if viewer.reads(ns, unlisted)
        ]

    def registry_key(self) -> dict[str, str] | None:
        """The registry's own signing key, as published (None when it has none)."""
        key = yank.registry_key()
        return None if key is None else yank.registry_public(key)

    def recall(
        self, namespace: str, name: str, version: str, record: Mapping[str, Any], *, by: str
    ) -> dict[str, Any]:
        """Store a yank record for one version, after checking it: its fields name exactly this
        version (pack, version, digest, archive), its time is now (within `MAX_SKEW_S`), and its
        signature holds with a key of the namespace's (`signer: publisher`) or the registry's
        own (`signer: registry`). A version is recalled once (409)."""
        found = self.version(namespace, name, version, REVIEWER)
        problem = yank.shape_problem(record)
        if problem:
            raise HubError(f"not a yank: {problem}")
        expected = {
            "pack": f"{namespace}/{name}",
            "version": version,
            "digest": found["digest"],
            "archive_sha256": found["archive_sha256"],
        }
        if any(record[field] != value for field, value in expected.items()):
            raise HubError("the yank is not about this version (its pack, digest or archive)")
        when = yank.at(record)
        assert when is not None  # shape_problem checked it
        if abs((datetime.now(UTC) - when).total_seconds()) > yank.MAX_SKEW_S:
            raise HubError("the yank's time is not now: sign it again")
        if record["signer"] == "registry":
            own = yank.registry_key()
            if own is None:
                raise HubError("this registry has no signing key", 409)
            public = own.public_key()
            key_id = yank.registry_public(own)["key_id"]
        else:
            keys = {
                k["key_id"]: k["public_pem"] for k in self.namespace(namespace, REVIEWER)["keys"]
            }
            key_id = str(record["key_id"])
            if key_id not in keys:
                raise HubError("the yank is signed by a key that is not this namespace's")
            public = archive.load_public_key(keys[key_id].encode())
        problem = yank.problem(record, public, key_id)
        if problem:
            raise HubError(f"the yank does not verify: {problem}")
        stored = {field: record[field] for field in (*yank.FIELDS, "signature")}
        where = ("namespace = ? AND name = ? AND version = ?", (namespace, name, version))
        with self._db.session(exclusive=True) as db:
            if db.one(f"SELECT version FROM hub_yanks WHERE {where[0]}", where[1]):
                raise HubError(f"{namespace}/{name}@{version} is already recalled", 409)
            db.all(
                "INSERT INTO hub_yanks (namespace, name, version, record, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (namespace, name, version, json.dumps(stored, sort_keys=True), _now_ms()),
            )
            record_event(
                db,
                "pack.yanked",
                _pack_subject(namespace, name, version),
                {
                    **expected,
                    "severity": record["severity"],
                    "reason": record["reason"],
                    "signer": record["signer"],
                    "key_id": key_id,
                    "by": by,
                },
                row=("yanks", *where),
            )
        return self.version(namespace, name, version, REVIEWER)

    def delete(
        self, namespace: str, name: str, version: str, *, reason: str, by: str
    ) -> dict[str, Any]:
        """Remove a version's archive (what must not be served: content it should never have
        carried), keeping its row and a tombstone; it is never served, and its version never
        published again. The archive's bytes go when no version that is not deleted needs
        them."""
        found = self.version(namespace, name, version, REVIEWER)
        where = ("namespace = ? AND name = ? AND version = ?", (namespace, name, version))
        with self._db.session(exclusive=True) as db:
            if db.one(f"SELECT version FROM hub_deletions WHERE {where[0]}", where[1]):
                raise HubError(f"{namespace}/{name}@{version} is already deleted", 409)
            db.all(
                "INSERT INTO hub_deletions (namespace, name, version, reason, deleted_by, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (namespace, name, version, reason, by, _now_ms()),
            )
            needed = db.one(
                "SELECT v.version FROM hub_versions v LEFT JOIN hub_deletions d ON "
                "d.namespace = v.namespace AND d.name = v.name AND d.version = v.version "
                "WHERE v.archive_sha256 = ? AND d.version IS NULL",
                (found["archive_sha256"],),
            )
            removed = needed is None
            if removed:
                db.all(
                    "DELETE FROM hub_archives WHERE archive_sha256 = ?", (found["archive_sha256"],)
                )
            record_event(
                db,
                "pack.deleted",
                _pack_subject(namespace, name, version),
                {
                    "pack": f"{namespace}/{name}",
                    "version": version,
                    "archive_sha256": found["archive_sha256"],
                    "archive_removed": removed,
                    "reason": reason,
                    "by": by,
                },
                row=("deletions", *where),
            )
        return self.version(namespace, name, version, REVIEWER)

    def verify(self, *, head: str | None = None) -> dict[str, Any]:
        """Check the chain and the rows it covers. Each event's hash and its order; then every
        namespace, key, token, version, report, review and counter-signature must be the row its
        newest event hashed (none changed, none removed, none without an event); every archive
        must hash to its name and every version's archive be there; every key's id must be its
        key's. Rows a
        registry held when its chain started (`registry.chain_started`) are hashed there. The
        head (`head`, `events`) is what an outside copy compares, as the chain is unkeyed: a
        writer of the database could rewrite it whole, and only a head kept elsewhere shows it.

        With `head` (a signed checkpoint's, `stated_head`), the chain must also hold an event
        with that hash: a chain rewritten or cut short from before the checkpoint does not.
        `after_checkpoint` then counts the events appended since, which the checkpoint says
        nothing about."""
        with self._db.session(exclusive=False) as db:
            if not db.columns("hub_events"):
                problem = "no chain yet: run `mendari registry serve` on this database once"
                return _broken(None, problem, [], None)
            events = db.all("SELECT * FROM hub_events ORDER BY seq")
            rows = {  # a database from before a kind, read as it is, has no table for it
                kind: db.all(f"SELECT * FROM {table}") if db.columns(table) else []
                for kind, (table, _) in ROWS.items()
            }
            archives = {
                str(r["archive_sha256"]): hashlib.sha256(bytes(r["data"])).hexdigest()
                for r in db.all("SELECT archive_sha256, data FROM hub_archives")
            }
            started = db.one("SELECT value FROM hub_meta WHERE key = 'chain'")
        prev = GENESIS
        chained: dict[tuple[str, str], str] = {}
        for event in events:
            seq = int(event["seq"])
            if event["prev_hash"] != prev:
                return _broken(seq, "an event is missing or out of order", events, prev)
            digest = _chain_hash(
                prev, event["subject"], event["event"], event["payload"], event["created_at"]
            )
            if digest != event["hash"]:
                return _broken(seq, "an event was changed", events, prev)
            prev = digest
            payload = json.loads(event["payload"])
            if event["event"] == "registry.chain_started":
                if seq != int(events[0]["seq"]):
                    return _broken(seq, "the chain was started again", events, prev)
                for kind, held in payload["rows"].items():
                    chained.update({(kind, key): sha for key, sha in held.items()})
            elif isinstance(payload.get("row"), dict):
                written = payload["row"]
                chained[(written["kind"], written["key"])] = written["sha256"]
        if started is not None and not events:
            return _broken(None, "the chain was emptied", events, prev)
        since: dict[str, int] = {}
        if head is not None:
            at = next((i for i, event in enumerate(events) if event["hash"] == head), None)
            if at is None:
                return _broken(None, _NOT_THE_CHECKPOINTS, events, prev)
            since = {"after_checkpoint": len(events) - 1 - at}
        found = {
            (kind, ROWS[kind][1](row)): _row_sha(row)
            for kind, listed in rows.items()
            for row in listed
        }
        for kind in ROWS:
            mine = {key for (k, key) in found if k == kind}
            named = {key for (k, key) in chained if k == kind}
            if mine - named:
                return _broken(
                    None, f"{len(mine - named)} of the {kind} have no chained event", events, prev
                )
            if named - mine:
                return _broken(
                    None, f"{len(named - mine)} of the {kind} were removed", events, prev
                )
            changed = [key for key in mine if found[(kind, key)] != chained[(kind, key)]]
            if changed:
                return _broken(
                    None,
                    f"{len(changed)} of the {kind} changed since their chained event",
                    events,
                    prev,
                )
        if any(sha != data for sha, data in archives.items()):
            return _broken(None, "an archive is not the bytes its name hashes", events, prev)
        gone = {ROWS["deletions"][1](row) for row in rows["deletions"]}
        if any(
            str(v["archive_sha256"]) not in archives and ROWS["versions"][1](v) not in gone
            for v in rows["versions"]
        ):
            return _broken(None, "a version's archive is missing", events, prev)
        for key in rows["keys"]:
            try:
                same = archive.key_id(archive.load_public_key(str(key["public_pem"]).encode()))
            except HubError:
                same = None
            if same != key["key_id"]:
                return _broken(None, "a key is not the key its id names", events, prev)
        return {"ok": True, "seq": None, "problem": None, **_head(events, prev), **since}

    def head(self) -> dict[str, Any]:
        """The chain's head: how many events, and the last one's hash (published, so a copy kept
        elsewhere can show the chain was not cut short or rewritten)."""
        with self._db.session(exclusive=False) as db:
            last = db.one("SELECT seq, hash FROM hub_events ORDER BY seq DESC LIMIT 1")
            count = db.one("SELECT COUNT(*) AS n FROM hub_events")
        return {
            "events": int(count["n"]) if count else 0,
            "head": last["hash"] if last else GENESIS,
        }

    def latest(self, viewer: Viewer = PUBLIC) -> list[dict[str, Any]]:
        """The newest version of every pack the viewer reads that is not recalled (yanked or
        deleted), with its namespace's publisher type."""
        with self._db.session(exclusive=False) as db:
            rows = db.all("SELECT * FROM hub_versions")
            unlisted = self._unlisted(db)
            reviews = _reviews(db, "", ())
            recalls = _recalls(db, "", ())
            accepted = self._accepted(db, "", ())
            types = {
                r["name"]: r["publisher_type"]
                for r in db.all("SELECT name, publisher_type FROM hub_namespaces")
            }
        newest: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            if _key(row) in recalls:  # a yanked or deleted version is never the latest
                continue
            if not viewer.reads(row["namespace"], unlisted):
                continue
            item = _version_row(
                row,
                reviews.get(_key(row)),
                listed=row["namespace"] not in unlisted,
                accepted=accepted.get(_key(row)),
            )
            key = (row["namespace"], row["name"])
            if key not in newest or version_key(item["version"]) > version_key(
                newest[key]["version"]
            ):
                newest[key] = item
        return [
            {**item, "publisher_type": types.get(item["namespace"])} for item in newest.values()
        ]


def _migrate(db: Session) -> None:
    """New tables come from the schema (`CREATE TABLE IF NOT EXISTS`). The first time a registry
    opens with a chain, it marks itself (`hub_meta`) and, if it held rows from before, hashes
    them once in its first event (`registry.chain_started`). Marked, it never does again: a
    chain emptied later is reported by `verify`, never started anew."""
    if db.one("SELECT value FROM hub_meta WHERE key = 'chain'") is not None:
        return
    db.all("INSERT INTO hub_meta (key, value) VALUES ('chain', ?)", (_now_ms(),))
    if db.one("SELECT seq FROM hub_events LIMIT 1") is not None:
        return
    held = {
        kind: {name(row): _row_sha(row) for row in db.all(f"SELECT * FROM {table}")}
        for kind, (table, name) in ROWS.items()
    }
    if any(held.values()):
        record_event(db, "registry.chain_started", "registry", {"rows": held})


def _broken(
    seq: int | None, problem: str, events: list[dict[str, Any]], prev: str | None
) -> dict[str, Any]:
    return {"ok": False, "seq": seq, "problem": problem, **_head(events, prev)}


def _head(events: list[dict[str, Any]], head: str | None) -> dict[str, Any]:
    return {"events": len(events), "head": head}


def _key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (row["namespace"], row["name"], row["version"])


def _reviews(
    db: Session, where: str, params: tuple[str, ...]
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """The newest review of each version the clause selects."""
    newest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in db.all(f"SELECT * FROM hub_reviews {where} ORDER BY ordinal", params):
        newest[_key(row)] = row
    return newest


def _recalls(
    db: Session, where: str, params: tuple[str, ...]
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Each recalled version the clause selects: its yank record and its deletion, if any."""
    found: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in db.all(f"SELECT * FROM hub_yanks {where}", params):
        found.setdefault(_key(row), {})["yank"] = json.loads(row["record"])
    for row in db.all(f"SELECT * FROM hub_deletions {where}", params):
        found.setdefault(_key(row), {})["deleted"] = {
            "reason": row["reason"],
            "at": row["created_at"],
        }
    return found


def _version_row(
    row: Mapping[str, Any],
    review: Mapping[str, Any] | None = None,
    recall: Mapping[str, Any] | None = None,
    listed: bool = True,
    accepted: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """A version as every read serves it: what was stored at publish, without anything the pack
    carried about itself (a version published before the registry rendered its own card stored
    the uploaded one, its benchmark and leaderboard), and the two groups (ADR-0012)."""
    stored = json.loads(row["metadata"])
    rendered = stored.get("format") == VERSION_FORMAT
    served = {
        "namespace": row["namespace"],
        "name": row["name"],
        "id": f"{row['namespace']}/{row['name']}",
        "version": row["version"],
        "published_at": row["created_at"],
        "archive_sha256": row["archive_sha256"],
        # The raw regression check is served only as `checked.regression` (its accuracy as a
        # figure, labelled), never beside it.
        **{k: v for k, v in stored.items() if k not in UPLOADED and k != "regression"},
        "card_markdown": stored.get("card_markdown") if rendered else None,
    }
    recall = recall or {}
    regression_of = stored.get("regression")
    return {
        **served,
        # A recall (`hub/yank.py`): the signed record, checked when it was stored, and a deletion.
        "yank": recall.get("yank"),
        "deleted": recall.get("deleted"),
        # False while its namespace's claim is not approved: only its own token and a reviewer
        # read it then (LP-19c).
        "listed": listed,
        "checked": _checked(served, rendered, review, regression_of, accepted),
        "claimed": _claimed(served, review),
    }


def _review(version: Mapping[str, Any], review: Mapping[str, Any] | None) -> dict[str, Any]:
    """A version's review status: `unreviewed`, or the newest review of exactly its digest."""
    if review is None or review["digest"] != version.get("digest"):
        return {"status": "unreviewed"}
    declared = (version.get("risk") or {}).get("level")
    return {
        "status": "reviewed",
        "risk_level": int(review["risk_level"]),
        # None when the version records no declared level (one stored before it did).
        "matches_declared": None
        if not isinstance(declared, int)
        else int(review["risk_level"]) == declared,
        "reviewer": review["reviewer"],
        "reviewed_at": review["created_at"],
        "note": review["note"],
    }


def _checked(
    version: Mapping[str, Any],
    rendered: bool,
    review: Mapping[str, Any] | None = None,
    regression_of: Any = None,
    accepted: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """What the registry verified or worked out itself, at publish."""
    return {
        # The archive's signature, by this key of the namespace's, over a manifest that lists
        # every file with its SHA-256 (`archive.verify`).
        "signature": {"algorithm": "Ed25519", "key_id": version.get("key_id"), "verified": True},
        "archive_sha256": version.get("archive_sha256"),
        # The digest the signed manifest names, which the pack loaded with, on `engine`.
        "digest": version.get("digest"),
        "engine": version.get("engine"),
        # The registry's release whose reference packs that engine was chosen against: a puller
        # on another release may run a copy of a reference pack on the bounded engine.
        "engine_release": version.get("engine_release"),
        # Rendered here from the files the digest covers, never the uploaded card.
        "card": "rendered" if rendered else "not_rendered",
        # Derived by the code from the manifest's provenance: it only ever lowers a claim.
        "status_label": version.get("status_label"),
        "review": _review(version, review),
        # Worked out here on the publisher's own test set (`hub/regression.py`): a regression
        # check against the earlier version, never a ranking. None: published without it.
        "regression": _regression(regression_of),
        # The registry's counter-signature (`hub/acceptance.py`), written when the version
        # became listed: the signed record, or None (no signing key then, or never listed).
        "accepted": None if accepted is None else dict(accepted),
    }


def _regression(stored: Any) -> dict[str, Any] | None:
    """A version's regression check as served, its accuracy as k of n (`figures.counted`)."""
    if not isinstance(stored, dict):
        return None
    shown = {
        k: stored.get(k) for k in ("label", "status", "reason", "compared_with", "regressions")
    }
    measured = stored.get("summary") if stored.get("status") == "measured" else None
    if isinstance(measured, dict):
        question = measured.get("questions", {}).get(measured.get("primary"), {})
        shown["test_set"] = measured.get("dataset")
        shown["accuracy"] = figures.guarded(
            lambda: figures.counted(question.get("accuracy"), question.get("n"))
        )
        shown["rows"] = stored.get("rows")
    return shown


def _claimed(version: Mapping[str, Any], review: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """What the publisher says about the pack, as its manifest says it: not checked."""
    risk = version.get("risk")
    risk = risk if isinstance(risk, dict) else {}
    return {
        "publisher": version.get("claimed_publisher"),
        "risk": {
            "level": risk.get("level"),
            "category": risk.get("category"),
            "declared_by": "publisher",
            # Whether a registry review found the same level (None until one does).
            "confirmed": _review(version, review).get("matches_declared"),
        },
        "license": version.get("license"),
        "summary": version.get("summary"),
    }


def _unknown_names(report: SiteReport, names: Mapping[str, Any], primary: Any) -> str | None:
    """What in a site's report the published pack does not name (a kind of name, never the
    name itself: it would be echoed to whoever sent it), or None."""
    questions: Mapping[str, list[str]] = names.get("questions") or {}
    slots: Mapping[str, Mapping[str, str]] = names.get("slots") or {}
    composites: Mapping[str, Mapping[str, list[str]]] = names.get("members") or {}
    if report.primary != primary:
        return "a primary question"
    for key, metrics in report.questions.items():
        labels = questions.get(key)
        if labels is None:
            return "a question"
        if not set(metrics.per_label) <= set(labels):
            return "a label"
        if not set(metrics.confusion) <= set(labels):
            return "a label"
        if any(not set(row) <= {*labels, "none"} for row in metrics.confusion.values()):
            return "a label"
    for item in report.decisions.critical:
        if item.label not in questions.get(item.question, ()):
            return "a critical label"
    ran: list[list[str]] = []  # the members of each composite that ran
    for slot, provider in report.providers.items():
        if slots.get(slot, {}).get(provider.variant) != provider.type:
            return "a provider slot or variant"
        declared = composites.get(slot, {}).get(provider.variant, [])
        listed = composite_members(provider)
        if listed is not None and listed != declared:
            return "a composite member"
        ran.append(declared)
    for key in report.errors:
        # As a composite records its failed members: each once, in its own order.
        failed = failed_members(key)
        if failed and not any(_in_order(failed, members) for members in ran):
            return "a composite member"
    return None


def _in_order(names: list[str], members: list[str]) -> bool:
    """Whether `names` are some of `members`, each once, in their order."""
    remaining = iter(members)
    return all(name in remaining for name in names)


def describe(pack: Pack, verified: archive.Verified) -> dict[str, Any]:
    """What the registry shows and searches: from the files the pack's digest covers, never from
    what it carries about itself (the uploaded card, benchmark and leaderboard, ADR-0012)."""
    from mendari.core.card import render_card

    m = pack.manifest
    return {
        "format": VERSION_FORMAT,
        "pack_name": m.name,
        "summary": " ".join(m.summary.split()),
        "license": m.license,
        "claimed_publisher": {"name": m.publisher.name, "type": m.publisher.type},
        # Derived from the manifest's `card.provenance` (`core/status.py`): a pack only ever
        # lowers what it claims here.
        "status_label": pack.status_label,
        "risk": {"level": int(m.risk.level), "category": m.risk.category},
        "questions": sorted(m.questions),
        "primary": m.policy.primary,
        # The only names a site's report may use (`add_report`): questions and their labels,
        # provider slots with their variants and types, and each composite variant's members
        # in order (its model lists them, and a failed one names itself in an error).
        "names": {
            "questions": {key: m.labels(key) for key in m.questions},
            "slots": {
                slot: {variant: v.type for variant, v in provider.variants.items()}
                for slot, provider in m.providers.items()
            },
            "members": {
                slot: {
                    variant: list(members)
                    for variant, v in provider.variants.items()
                    if v.type in JOINERS
                    and isinstance(members := (v.model_extra or {}).get("members"), dict)
                    # YAML reads a key like 2024-01-01 as a date: such a composite never runs
                    and all(isinstance(member, str) for member in members)
                }
                for slot, provider in m.providers.items()
            },
        },
        "standards": sorted(
            [
                *(["FHIR R4"] if m.inputs.fhir else []),
                *([f"CDS Hooks {m.cds_hooks.hook}"] if m.cds_hooks else []),
            ]
        ),
        "digest": pack.digest,
        "key_id": verified.key_id,
        # The engine its pullers run it on (ADR-0009): a copy of a reference pack of this
        # registry's release keeps the maintained one; every other pack runs bounded.
        "engine": "bounded" if pack.bounded else "maintained",
        "engine_release": __version__,
        "card_markdown": render_card(pack, report=None, board=None, rendered_by="registry"),
    }


# -- the HTTP API ---------------------------------------------------------------------------------


class NamespaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=63)
    publisher_type: PublisherType = "community"
    display_name: str = Field(default="", max_length=200)


MAX_REPORT_BYTES = 2_000_000


class ReportRequest(BaseModel):
    site: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")
    report: SiteReport


class PublisherTypeRequest(BaseModel):
    publisher_type: PublisherType


class ReviewRequest(BaseModel):
    risk_level: int = Field(ge=0, le=4)
    # Chained for good and shown on the Hub: a short line about the pack, never case text.
    note: str = Field(default="", max_length=300)

    @field_validator("note")
    @classmethod
    def _one_printable_line(cls, note: str) -> str:
        """No control, format or separator character (C0, C1, bidirectional overrides, line and
        paragraph separators): the note reads as the characters it holds."""
        if any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp", "Co", "Cs") for c in note):
            raise ValueError("a review note is one line of printable text")
        return note


class YankRequest(BaseModel):
    # A publisher's signed record (`hub/yank.py`), or an administrator's reason and severity.
    record: dict[str, Any] | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=yank.MAX_REASON)
    severity: yank.Severity | None = None

    @field_validator("reason")
    @classmethod
    def _reason_is_a_line(cls, reason: str | None) -> str | None:
        why = None if reason is None else yank.printable_line(reason, yank.MAX_REASON)
        if why:
            raise ValueError(f"a reason is {why}")
        return reason


class DeleteRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=yank.MAX_REASON)

    @field_validator("reason")
    @classmethod
    def _reason_is_a_line(cls, reason: str) -> str:
        why = yank.printable_line(reason, yank.MAX_REASON)
        if why:
            raise ValueError(f"a reason is {why}")
        return reason


Small = TypeVar("Small", bound=BaseModel)
# A review, a recall or a deletion: a valid one is under 2 KB.
MAX_SMALL_BYTES = 16_000


def _body_of(model: type[BaseModel]) -> dict[str, Any]:
    """The request body a route reads itself, for its OpenAPI description."""
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": model.model_json_schema()}},
        }
    }


class KeyRequest(BaseModel):
    public_pem: str = Field(min_length=40, max_length=2000)


# The registry answers JSON and archives, never a page: its CSP loads nothing and no one frames
# it. With the engine's own (`server/app.py` `SECURITY_HEADERS`).
REGISTRY_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
REGISTRY_HEADERS = {
    **SECURITY_HEADERS,
    "Content-Security-Policy": REGISTRY_CSP,
    "Cross-Origin-Embedder-Policy": "require-corp",
    "Cache-Control": "no-store",
}
REGISTRY_RATES: dict[str, Rate] = {
    "registry_read": Rate(600, 60, 200),  # per address
    "registry_write": Rate(120, 3600, 30),  # per token, or address without one
    "registry_report": Rate(60, 3600, 10),  # a site's report, per token
}
_REPORTS = re.compile(r"^/v1/packs/[^/]+/[^/]+/[^/]+/reports$")


def _answer_headers(request: Request) -> dict[str, str]:
    """The headers every answer carries: `REGISTRY_HEADERS`, and HSTS but on this machine
    (browsers ignore it over plain http)."""
    if is_local_host(request.url.hostname):
        return REGISTRY_HEADERS
    return {**REGISTRY_HEADERS, "Strict-Transport-Security": HSTS}


def registry_limit_settings() -> LimitSettings:
    """From `MENDARI_REGISTRY_RATE_LIMITS`: a mode (`on` unless set, `log`, `off`), then
    overrides of `REGISTRY_RATES` in the engine's syntax (`registry_read=1200/min:300`)."""
    raw = os.environ.get("MENDARI_REGISTRY_RATE_LIMITS", "").strip()
    mode, rates = "on", dict(REGISTRY_RATES)
    for item in (part.strip() for part in raw.split(",") if part.strip()):
        name, sep, value = item.partition("=")
        if not sep and item.lower() in ("on", "log", "off"):
            mode = item.lower()
        elif sep and name.strip() in REGISTRY_RATES:
            try:
                rates[name.strip()] = parse_rate(value.strip(), name.strip())
            except MendariError as error:
                raise MendariError(
                    str(error).replace("MENDARI_RATE_LIMITS", "MENDARI_REGISTRY_RATE_LIMITS")
                ) from None
        else:
            raise MendariError(
                "MENDARI_REGISTRY_RATE_LIMITS: a mode (on, log or off), then overrides of "
                f"{', '.join(REGISTRY_RATES)} such as registry_write=240/h:40"
            )
    return LimitSettings(mode=cast(Literal["on", "log", "off"], mode), rates=rates)


def _rate_class(request: Request, known: Callable[[str], bool]) -> tuple[str, str]:
    """A request's limit and its bucket's key: reads per address; writes and reports per token
    (by its id) when the token is one the registry issued (or the admin's), else per address, so
    made-up tokens share their sender's bucket."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return "registry_read", f"ip:{client_ip(request)}"
    header = request.headers.get("authorization", "")
    token = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
    key = f"token:{token_id(token)}" if token and known(token) else f"ip:{client_ip(request)}"
    kind = "registry_report" if _REPORTS.match(request.url.path) else "registry_write"
    return kind, key


def create_registry_app(
    target: str | Path, *, admin_token: str | None = None, migrate_url: str | None = None
) -> FastAPI:
    store = RegistryStore(target, migrate_url=migrate_url)
    admin = (
        admin_token
        if admin_token is not None
        else os.environ.get("MENDARI_REGISTRY_ADMIN_TOKEN", "")
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        store.close()  # the Postgres pool, closed with the app rather than by the collector

    app = FastAPI(title="Mendari Hub registry", version=__version__, lifespan=lifespan)
    limiter = Limiter(store, registry_limit_settings())

    def publisher_namespace(token: str, *, used: bool = False) -> str | None:
        """The namespace a token publishes to: an owned token that is neither revoked nor
        expired, or a token from before owned tokens. `used` notes the day an owned token was
        used, for a request it was let through on."""
        owned = store.publisher(token)
        if owned is None:
            return store.token_namespace(token)
        if used:
            store.token_used(owned["id"])
        return owned["namespace"]

    def publishes_to(token: str, namespace: str) -> bool:
        owned = store.publisher(token)
        if owned is None:
            return store.token_namespace(token) == namespace
        if owned["namespace"] == namespace:
            store.token_used(owned["id"])
        return owned["namespace"] == namespace

    def known(token: str) -> bool:
        """A token the registry issued, or the admin's."""
        if admin and hmac.compare_digest(token, admin):
            return True
        return publisher_namespace(token) is not None

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, error: Exception) -> JSONResponse:
        """An error no handler names: a 500 with nothing of the error, and the headers every
        answer carries, HSTS included (Starlette answers it outside the middleware)."""
        body = {"error": "InternalError", "detail": "internal error"}
        return JSONResponse(body, 500, headers=_answer_headers(request))

    @app.middleware("http")
    async def _limits_and_headers(request: Request, call_next: Any) -> Response:
        """The rate first (no body read, nothing stored for a refused request), then every
        answer's headers, refusals and errors included."""
        refused = None
        if limiter.on:  # database work, off the event loop
            refused = await run_in_threadpool(lambda: limiter.hit(*_rate_class(request, known)))
        if refused is not None:
            response: Response = JSONResponse(
                {"error": "RateLimited", "detail": f"too many requests ({refused.limit})"},
                429,
                headers={"Retry-After": str(refused.retry_after)},
            )
        else:
            response = await call_next(request)
        for name, value in _answer_headers(request).items():
            response.headers[name] = value
        return response

    @app.exception_handler(HubError)
    async def _hub_error(_: Request, error: HubError) -> JSONResponse:
        return JSONResponse({"error": "HubError", "detail": str(error)}, error.status)

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(_: Request, error: RequestValidationError) -> JSONResponse:
        """FastAPI's 422 without the `input` and `ctx` it would echo (a report can carry what
        a site meant to keep, and NaN in it made the echo a 500), and with mapping keys, which
        are data, left out of the location."""
        problems = [
            {"type": e.get("type"), "loc": where(e.get("loc", ())), "msg": e.get("msg")}
            for e in error.errors()
        ]
        return JSONResponse({"detail": problems[:20]}, 422)

    def bearer(request: Request) -> str:
        header = request.headers.get("authorization", "")
        return header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""

    def require_admin(request: Request) -> None:
        if not admin or not hmac.compare_digest(bearer(request), admin):
            raise HTTPException(401, "admin token required")

    def require_publisher(request: Request, namespace: str) -> None:
        token = bearer(request)
        if admin and token and hmac.compare_digest(token, admin):
            return
        if not token or not publishes_to(token, namespace):
            raise HTTPException(403, f"a token for namespace {namespace!r} is required")

    def actor(request: Request) -> str:
        """Who a checked request acts as, as the chain names them: `admin`, or a token by its
        id (`token:<id>`, never the token)."""
        token = bearer(request)
        if admin and token and hmac.compare_digest(token, admin):
            return "admin"
        return f"token:{token_id(token)}"

    def viewer_of(request: Request) -> Viewer:
        """Who reads: a reviewer (the admin token, until reviewers sign in), a namespace's own
        token, or the public (no token, or one that is unknown, revoked or expired). A read
        never notes a token as used."""
        token = bearer(request)
        if not token:
            return PUBLIC
        if admin and hmac.compare_digest(token, admin):
            return REVIEWER
        return Viewer(namespace=publisher_namespace(token))

    @app.get("/v1/chain/head")
    def chain_head() -> dict[str, Any]:
        """The chain's head (`events`, `head`): kept elsewhere, it shows a later chain was not cut
        short or rewritten, which the unkeyed chain cannot show by itself."""
        return store.head()

    @app.get("/v1/health")
    def health() -> dict[str, Any]:  # the listed packs, whoever asks
        return {
            "status": "ok",
            "version": __version__,
            "storage": store.backend,
            "packs": len(store.latest()),
        }

    @app.post("/v1/namespaces", status_code=201)
    def create_namespace(request: Request, body: NamespaceRequest) -> dict[str, Any]:
        require_admin(request)
        return store.create_namespace(
            body.name, body.publisher_type, body.display_name, by=actor(request)
        )

    @app.patch("/v1/namespaces/{namespace}")
    def verify_namespace(
        request: Request, namespace: str, body: PublisherTypeRequest
    ) -> dict[str, Any]:
        require_admin(request)
        return store.set_publisher_type(namespace, body.publisher_type, by=actor(request))

    @app.post("/v1/namespaces/{namespace}/tokens", status_code=201)
    def issue_token(request: Request, namespace: str) -> dict[str, str]:
        require_admin(request)
        return {"namespace": namespace, "token": store.issue_token(namespace, by=actor(request))}

    @app.post("/v1/namespaces/{namespace}/keys", status_code=201)
    def add_key(request: Request, namespace: str, body: KeyRequest) -> dict[str, str]:
        require_publisher(request, namespace)
        key_id = store.add_key(namespace, body.public_pem, by=actor(request))
        return {"namespace": namespace, "key_id": key_id}

    @app.get("/v1/namespaces/{namespace}")
    def namespace(request: Request, namespace: str) -> dict[str, Any]:
        viewer = viewer_of(request)
        found = store.namespace(namespace, viewer)
        packs = sorted(p["id"] for p in store.latest(viewer) if p["namespace"] == namespace)
        return {**found, "packs": packs}

    @app.put("/v1/packs/{namespace}/{name}/{version}", status_code=201)
    async def publish(request: Request, namespace: str, name: str, version: str) -> dict[str, Any]:
        require_publisher(request, namespace)
        if not _NAME.match(name) or not _VERSION.match(version):
            raise HubError("not a pack name and version")
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > archive.MAX_ARCHIVE_BYTES:
                raise HTTPException(413, "the archive is too large")
        # Off the event loop: a publish runs the regression check, up to its time limit.
        return await run_in_threadpool(
            store.publish, namespace, name, version, bytes(body), by=actor(request)
        )

    async def small_body(request: Request, model: type[Small]) -> Small:
        """A small JSON body, read only after its sender was checked and at most
        `MAX_SMALL_BYTES` (a body FastAPI parses for a route is read before the route runs)."""
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_SMALL_BYTES:
                raise HTTPException(413, "the body is too large")
        try:
            parsed = from_json(bytes(body), allow_inf_nan=False)
        except ValueError:
            problem = {"type": "json_invalid", "loc": ("body",), "msg": "the body is not JSON"}
            raise RequestValidationError([problem]) from None
        try:
            return model.model_validate(parsed)
        except ValidationError as error:
            raise RequestValidationError(error.errors()) from None

    @app.post(
        "/v1/packs/{namespace}/{name}/{version}/review",
        status_code=201,
        openapi_extra=_body_of(ReviewRequest),
    )
    async def review(request: Request, namespace: str, name: str, version: str) -> dict[str, Any]:
        """A registry reviewer records the risk level a version's review found, with a short
        note (chained `pack.reviewed`). Until reviewer accounts exist (LP-19), the admin token
        records it. The newest review of a version is the one shown."""
        require_admin(request)
        body = await small_body(request, ReviewRequest)
        store.review(
            namespace,
            name,
            version,
            risk_level=body.risk_level,
            note=body.note,
            reviewer=actor(request),
        )
        return served(store.version(namespace, name, version, REVIEWER), REVIEWER)

    @app.post(
        "/v1/packs/{namespace}/{name}/{version}/yank",
        status_code=201,
        openapi_extra=_body_of(YankRequest),
    )
    async def recall(request: Request, namespace: str, name: str, version: str) -> dict[str, Any]:
        """Recall a version. The namespace's publisher sends a `mendari.yank/1` record it signed
        with one of the namespace's keys (`mendari yank`); an administrator sends a reason and a
        severity, and the registry signs the record with its own key
        (`MENDARI_REGISTRY_SIGNING_KEY`). Chained `pack.yanked`; a version is recalled once."""
        token = bearer(request)
        as_admin = bool(admin and token and hmac.compare_digest(token, admin))
        if not as_admin:
            require_publisher(request, namespace)
        body = await small_body(request, YankRequest)
        if as_admin:
            if body.record is not None:
                raise HubError("an administrator's recall is signed by the registry: send a reason")
            if body.reason is None or body.severity is None:
                raise HubError("a recall needs a reason and a severity")
            own = yank.registry_key()
            if own is None:
                raise HubError(
                    "this registry has no signing key (MENDARI_REGISTRY_SIGNING_KEY)", 409
                )
            found = store.version(namespace, name, version, REVIEWER)
            record: dict[str, Any] = yank.sign(
                pack=f"{namespace}/{name}",
                version=version,
                digest=found["digest"],
                archive_sha256=found["archive_sha256"],
                reason=body.reason,
                severity=body.severity,
                signer="registry",
                key=own,
                key_id=yank.registry_public(own)["key_id"],
            )
        else:
            if body.record is None:
                raise HubError("a publisher's recall is a record signed with a namespace key")
            if body.record.get("signer") != "publisher":
                raise HubError("a publisher's recall is signed by the publisher")
            record = body.record
        # Checked above: the namespace's own publisher, or an administrator.
        recalled = store.recall(namespace, name, version, record, by=actor(request))
        return served(recalled, REVIEWER)

    @app.post(
        "/v1/packs/{namespace}/{name}/{version}/delete", openapi_extra=_body_of(DeleteRequest)
    )
    async def delete(request: Request, namespace: str, name: str, version: str) -> dict[str, Any]:
        """Delete a version (administrators): its archive is no longer served, and its bytes
        are removed when no other version needs them. The row and a tombstone with the reason
        stay; chained `pack.deleted`."""
        require_admin(request)
        body = await small_body(request, DeleteRequest)
        deleted = store.delete(namespace, name, version, reason=body.reason, by=actor(request))
        return served(deleted, REVIEWER)

    @app.get("/v1/registry-key")
    def registry_key() -> dict[str, str]:
        """The registry's own public key, which signs the recalls it makes."""
        published = store.registry_key()
        if published is None:
            raise HubError("this registry has no signing key", 404)
        return published

    @app.get("/v1/yanks")
    def yanks(request: Request) -> list[dict[str, Any]]:
        """Every recalled version: its signed yank record and its deletion, if any. From LP-18e
        a running server checks the packs it pulled against this list."""
        return store.recalled(viewer_of(request))

    @app.get("/v1/packs/{namespace}/{name}")
    def pack(request: Request, namespace: str, name: str) -> dict[str, Any]:
        """A pack: its versions, the recalled ones apart, and its newest version that is not
        recalled (`latest`, null when every version is)."""
        viewer = viewer_of(request)
        versions = store.versions(namespace, name, viewer)
        if not versions:
            raise HubError(f"no pack {namespace}/{name}", 404)
        live = [v for v in versions if v["yank"] is None and v["deleted"] is None]
        return {
            "id": f"{namespace}/{name}",
            "versions": [v["version"] for v in live],
            "recalled": [
                {"version": v["version"], "yank": v["yank"], "deleted": v["deleted"]}
                for v in versions
                if v not in live
            ],
            "latest": served(live[-1], viewer) if live else None,
        }

    @app.get("/v1/packs/{namespace}/{name}/reports")
    def reports(request: Request, namespace: str, name: str) -> dict[str, Any]:
        """Each site's shared results, version by version (unverified until sites sign them)."""
        viewer = viewer_of(request)
        if not store.versions(namespace, name, viewer):
            raise HubError(f"no pack {namespace}/{name}", 404)
        return {"id": f"{namespace}/{name}", "sites": store.reports(namespace, name, viewer)}

    @app.get("/v1/packs/{namespace}/{name}/{version}")
    def pack_version(request: Request, namespace: str, name: str, version: str) -> dict[str, Any]:
        viewer = viewer_of(request)
        return served(store.version(namespace, name, version, viewer), viewer)

    def served(version: dict[str, Any], viewer: Viewer) -> dict[str, Any]:
        """A version with what the registry knows of its namespace now: the publisher type its
        administrators set after checking who publishes there (a checked fact, unlike the type
        the pack claims). Figures are worked out on read (mendari.evals.figures), never stored."""
        publisher_type = store.namespace(version["namespace"], viewer)["publisher_type"]
        return {
            **version,
            "publisher_type": publisher_type,
            "checked": {**version["checked"], "publisher_type": publisher_type},
            "figures": figures.registry(version),
        }

    @app.get("/v1/packs/{namespace}/{name}/{version}/archive")
    def pack_archive(request: Request, namespace: str, name: str, version: str) -> Response:
        viewer = viewer_of(request)
        found = store.version(namespace, name, version, viewer)
        if found["deleted"] is not None:
            raise HubError(
                f"{namespace}/{name}@{version} was deleted: {found['deleted']['reason']}", 410
            )
        return Response(
            store.archive(found["archive_sha256"], viewer),
            media_type="application/octet-stream",
            headers={"x-archive-sha256": found["archive_sha256"]},
        )

    @app.post("/v1/packs/{namespace}/{name}/{version}/reports", status_code=201)
    async def add_report(
        request: Request, namespace: str, name: str, version: str
    ) -> dict[str, Any]:
        """A site shares its local evaluation (`mendari.site-report/1`, checked before anything
        is stored). Any publisher token may submit; the site name is the site's choice, so
        reports are listed as unverified. The token is checked before the body is read, and the
        body is read only up to `MAX_REPORT_BYTES`."""
        token = bearer(request)
        submitter = "admin" if admin and token and hmac.compare_digest(token, admin) else None
        viewer = REVIEWER if submitter else PUBLIC
        submitter = submitter or (publisher_namespace(token, used=True) if token else None)
        if submitter is None:
            raise HTTPException(401, "a registry token is required to share a report")
        if viewer is PUBLIC:  # a version in a namespace that is not listed: its own token's only
            viewer = Viewer(namespace=submitter)
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_REPORT_BYTES:
                raise HTTPException(413, "the report is too large")
        # Parsed first, then checked: JSON-mode validation of a long list costs far more memory
        # than its bytes (a 1.8 MB body near 900 MB).
        try:
            parsed = from_json(bytes(body), allow_inf_nan=False)
        except ValueError:
            problem = {"type": "json_invalid", "loc": ("body",), "msg": "the body is not JSON"}
            raise RequestValidationError([problem]) from None
        try:
            shared = ReportRequest.model_validate(parsed)
        except ValidationError as error:
            raise RequestValidationError(error.errors()) from None
        return store.add_report(
            namespace,
            name,
            version,
            shared.site,
            submitter,
            shared.report,
            by=actor(request),
            viewer=viewer,
        )

    @app.get("/v1/search")
    def search(
        request: Request,
        q: str = "",
        risk: int | None = None,
        standard: str = "",
        publisher_type: str = "",
    ) -> list[dict[str, Any]]:
        words = q.lower().split()
        found = []
        for item in store.latest(viewer_of(request)):
            text = " ".join(
                [item["id"], item["pack_name"], item["summary"], *item["questions"]]
            ).lower()
            if words and not all(word in text for word in words):
                continue
            if risk is not None and (item.get("risk") or {}).get("level") != risk:
                continue
            if standard and not any(standard.lower() in s.lower() for s in item["standards"]):
                continue
            if publisher_type and item["publisher_type"] != publisher_type:
                continue
            kept = {k: v for k, v in item.items() if k != "card_markdown"}
            found.append(
                {
                    **kept,
                    "checked": {**item["checked"], "publisher_type": item["publisher_type"]},
                    "figures": figures.registry(item, with_leaderboard=False),
                }
            )
        return sorted(found, key=lambda item: item["id"])

    return app
