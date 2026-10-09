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
- **Reads** (no token): packs, versions, archives, namespaces and their keys, and search.
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
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError
from pydantic_core import from_json, to_json

from mendari import __version__
from mendari.core.db import Backend, Session, connect
from mendari.core.pack import Pack
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
from mendari.hub import archive

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

SCHEMA = """
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
)
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class RegistryStore:
    def __init__(self, target: str | Path, *, migrate_url: str | None = None):
        self._db: Backend = connect(
            str(target),
            schema=SCHEMA,
            migrate=_no_migration,
            lock_key=_LOCK,
            migrate_url=migrate_url,
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

    def create_namespace(self, name: str, publisher_type: str, display_name: str) -> dict[str, Any]:
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
        return self.namespace(name)

    def set_publisher_type(self, name: str, publisher_type: str) -> dict[str, Any]:
        with self._db.session(exclusive=True) as db:
            db.all(
                "UPDATE hub_namespaces SET publisher_type = ? WHERE name = ?",
                (publisher_type, name),
            )
        return self.namespace(name)

    def namespace(self, name: str) -> dict[str, Any]:
        with self._db.session(exclusive=False) as db:
            row = db.one("SELECT * FROM hub_namespaces WHERE name = ?", (name,))
            if row is None:
                raise HubError(f"no namespace {name!r}", 404)
            keys = db.all(
                "SELECT key_id, public_pem, created_at FROM hub_keys WHERE namespace = ? "
                "ORDER BY created_at",
                (name,),
            )
        return {**row, "keys": keys}

    def add_key(self, namespace: str, public_pem: str) -> str:
        key = archive.load_public_key(public_pem.encode())
        key_id = archive.key_id(key)
        self.namespace(namespace)
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
        return key_id

    def issue_token(self, namespace: str) -> str:
        self.namespace(namespace)
        token = "ohr_" + secrets.token_urlsafe(32)
        with self._db.session(exclusive=True) as db:
            db.all(
                "INSERT INTO hub_tokens (token_sha256, namespace, created_at) VALUES (?, ?, ?)",
                (_sha(token), namespace, _now()),
            )
        return token

    def token_namespace(self, token: str) -> str | None:
        with self._db.session(exclusive=False) as db:
            row = db.one("SELECT namespace FROM hub_tokens WHERE token_sha256 = ?", (_sha(token),))
        return None if row is None else str(row["namespace"])

    # -- packs ----------------------------------------------------------------------------------

    def publish(self, namespace: str, name: str, version: str, data: bytes) -> dict[str, Any]:
        keys = {
            k["key_id"]: archive.load_public_key(k["public_pem"].encode())
            for k in self.namespace(namespace)["keys"]
        }
        if not keys:
            raise HubError(f"namespace {namespace!r} has no publisher key")
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
        return self.version(namespace, name, version)

    def versions(self, namespace: str, name: str) -> list[dict[str, Any]]:
        with self._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT * FROM hub_versions WHERE namespace = ? AND name = ?", (namespace, name)
            )
        return sorted((_version_row(r) for r in rows), key=lambda v: version_key(v["version"]))

    def version(self, namespace: str, name: str, version: str) -> dict[str, Any]:
        with self._db.session(exclusive=False) as db:
            row = db.one(
                "SELECT * FROM hub_versions WHERE namespace = ? AND name = ? AND version = ?",
                (namespace, name, version),
            )
        if row is None:
            raise HubError(f"no pack {namespace}/{name}@{version}", 404)
        return _version_row(row)

    def archive(self, sha256: str) -> bytes:
        with self._db.session(exclusive=False) as db:
            row = db.one("SELECT data FROM hub_archives WHERE archive_sha256 = ?", (sha256,))
        if row is None:
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
    ) -> dict[str, Any]:
        """A site's evaluation of a published version (`mendari.site-report/1`, already
        validated: nothing in it but names and numbers)."""
        found = self.version(namespace, name, version)
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
        return {"id": report_id, "site": site, "version": version, **summary(shared)}

    def reports(self, namespace: str, name: str) -> list[dict[str, Any]]:
        with self._db.session(exclusive=False) as db:
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

    def latest(self) -> list[dict[str, Any]]:
        """The newest version of every pack, with its namespace's publisher type."""
        with self._db.session(exclusive=False) as db:
            rows = db.all("SELECT * FROM hub_versions")
            types = {
                r["name"]: r["publisher_type"]
                for r in db.all("SELECT name, publisher_type FROM hub_namespaces")
            }
        newest: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            item = _version_row(row)
            key = (row["namespace"], row["name"])
            if key not in newest or version_key(item["version"]) > version_key(
                newest[key]["version"]
            ):
                newest[key] = item
        return [
            {**item, "publisher_type": types.get(item["namespace"])} for item in newest.values()
        ]


def _no_migration(_: Session) -> None:
    """The registry's schema has had no change since its first release."""


def _version_row(row: Mapping[str, Any]) -> dict[str, Any]:
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
        **{key: value for key, value in stored.items() if key not in UPLOADED},
        "card_markdown": stored.get("card_markdown") if rendered else None,
    }
    return {**served, "checked": _checked(served, rendered), "claimed": _claimed(served)}


def _checked(version: Mapping[str, Any], rendered: bool) -> dict[str, Any]:
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
        "review": {"status": "unreviewed"},
    }


def _claimed(version: Mapping[str, Any]) -> dict[str, Any]:
    """What the publisher says about the pack, as its manifest says it: not checked."""
    risk = version.get("risk")
    risk = risk if isinstance(risk, dict) else {}
    return {
        "publisher": version.get("claimed_publisher"),
        "risk": {
            "level": risk.get("level"),
            "category": risk.get("category"),
            "declared_by": "publisher",
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


class KeyRequest(BaseModel):
    public_pem: str = Field(min_length=40, max_length=2000)


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
        if not token or store.token_namespace(token) != namespace:
            raise HTTPException(403, f"a token for namespace {namespace!r} is required")

    @app.get("/v1/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "storage": store.backend,
            "packs": len(store.latest()),
        }

    @app.post("/v1/namespaces", status_code=201)
    def create_namespace(request: Request, body: NamespaceRequest) -> dict[str, Any]:
        require_admin(request)
        return store.create_namespace(body.name, body.publisher_type, body.display_name)

    @app.patch("/v1/namespaces/{namespace}")
    def verify_namespace(
        request: Request, namespace: str, body: PublisherTypeRequest
    ) -> dict[str, Any]:
        require_admin(request)
        store.namespace(namespace)
        return store.set_publisher_type(namespace, body.publisher_type)

    @app.post("/v1/namespaces/{namespace}/tokens", status_code=201)
    def issue_token(request: Request, namespace: str) -> dict[str, str]:
        require_admin(request)
        return {"namespace": namespace, "token": store.issue_token(namespace)}

    @app.post("/v1/namespaces/{namespace}/keys", status_code=201)
    def add_key(request: Request, namespace: str, body: KeyRequest) -> dict[str, str]:
        require_publisher(request, namespace)
        return {"namespace": namespace, "key_id": store.add_key(namespace, body.public_pem)}

    @app.get("/v1/namespaces/{namespace}")
    def namespace(namespace: str) -> dict[str, Any]:
        found = store.namespace(namespace)
        packs = sorted(p["id"] for p in store.latest() if p["namespace"] == namespace)
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
        return store.publish(namespace, name, version, bytes(body))

    @app.get("/v1/packs/{namespace}/{name}")
    def pack(namespace: str, name: str) -> dict[str, Any]:
        versions = store.versions(namespace, name)
        if not versions:
            raise HubError(f"no pack {namespace}/{name}", 404)
        return {
            "id": f"{namespace}/{name}",
            "versions": [v["version"] for v in versions],
            "latest": served(versions[-1]),
        }

    @app.get("/v1/packs/{namespace}/{name}/reports")
    def reports(namespace: str, name: str) -> dict[str, Any]:
        """Each site's shared results, version by version (unverified until sites sign them)."""
        if not store.versions(namespace, name):
            raise HubError(f"no pack {namespace}/{name}", 404)
        return {"id": f"{namespace}/{name}", "sites": store.reports(namespace, name)}

    @app.get("/v1/packs/{namespace}/{name}/{version}")
    def pack_version(namespace: str, name: str, version: str) -> dict[str, Any]:
        return served(store.version(namespace, name, version))

    def served(version: dict[str, Any]) -> dict[str, Any]:
        """A version with what the registry knows of its namespace now: the publisher type its
        administrators set after checking who publishes there (a checked fact, unlike the type
        the pack claims). Figures are worked out on read (mendari.evals.figures), never stored."""
        publisher_type = store.namespace(version["namespace"])["publisher_type"]
        return {
            **version,
            "publisher_type": publisher_type,
            "checked": {**version["checked"], "publisher_type": publisher_type},
            "figures": figures.registry(version),
        }

    @app.get("/v1/packs/{namespace}/{name}/{version}/archive")
    def pack_archive(namespace: str, name: str, version: str) -> Response:
        found = store.version(namespace, name, version)
        return Response(
            store.archive(found["archive_sha256"]),
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
        submitter = submitter or (store.token_namespace(token) if token else None)
        if submitter is None:
            raise HTTPException(401, "a registry token is required to share a report")
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
        return store.add_report(namespace, name, version, shared.site, submitter, shared.report)

    @app.get("/v1/search")
    def search(
        q: str = "", risk: int | None = None, standard: str = "", publisher_type: str = ""
    ) -> list[dict[str, Any]]:
        words = q.lower().split()
        found = []
        for item in store.latest():
            text = " ".join(
                [item["id"], item["pack_name"], item["summary"], *item["questions"]]
            ).lower()
            if words and not all(word in text for word in words):
                continue
            if risk is not None and item["risk"]["level"] != risk:
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
