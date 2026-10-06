"""The registry: namespaces, publisher keys, immutable signed pack versions, and search.

- **Namespaces** are owned by publishers. Each has a publisher type (community, verified,
  institutional) that registry admins set after verification; a pack's own `publisher.type` is
  only what it claims. Each namespace registers Ed25519 public keys.
- **Publishing** (`PUT /v1/packs/{ns}/{name}/{version}`, with a namespace token) accepts an
  archive only if:
  - its signature verifies with one of the namespace's keys;
  - its manifest names this pack and version;
  - it extracts to a pack that loads with the signed digest;
  - that version does not exist yet.

  Versions are immutable.
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
import math
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
from mendari.evals.share import SiteReport, summary, where
from mendari.hub import archive

PublisherType = Literal["community", "verified", "institutional"]
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_VERSION = re.compile(r"^(?=.{1,64}$)\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
# A leaderboard the registry shows as the pack carries it, within these bounds: a shipped one
# nests 5 levels and holds about 10 KB. Deeper than about 255 levels, the response serializer
# fails, and the version is immutable.
MAX_LEADERBOARD_DEPTH = 32
MAX_LEADERBOARD_BYTES = 1_000_000
MAX_DATASET_NAME = 200
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
                pack = archive.extract(verified, Path(scratch) / "pack")
                metadata = describe(pack, verified)
            except HubError:
                raise
            except MendariError as error:
                raise HubError(f"the pack does not load: {error}") from None
            except Exception as error:  # the publisher's content: named, never a 500
                raise HubError(f"the pack does not load ({type(error).__name__})") from None
        try:  # as every answer will serialize it, before anything is stored: versions are immutable
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
                    json.dumps(metadata, sort_keys=True),
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
    return {
        "namespace": row["namespace"],
        "name": row["name"],
        "id": f"{row['namespace']}/{row['name']}",
        "version": row["version"],
        "published_at": row["created_at"],
        "archive_sha256": row["archive_sha256"],
        **json.loads(row["metadata"]),
    }


def _unknown_names(report: SiteReport, names: Mapping[str, Any], primary: Any) -> str | None:
    """What in a site's report the published pack does not name (a kind of name, never the
    name itself: it would be echoed to whoever sent it), or None."""
    questions: Mapping[str, list[str]] = names.get("questions") or {}
    slots: Mapping[str, Mapping[str, str]] = names.get("slots") or {}
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
    for slot, provider in report.providers.items():
        if slots.get(slot, {}).get(provider.variant) != provider.type:
            return "a provider slot or variant"
    return None


def describe(pack: Pack, verified: archive.Verified) -> dict[str, Any]:
    """What the registry shows and searches: from the pack itself, never from the uploader."""
    m = pack.manifest
    benchmark = _json(pack.attestation("evals/benchmark.json"))
    leaderboard = _json(pack.attestation("evals/leaderboard.json"))
    questions = (benchmark or {}).get("questions")
    primary = questions.get(m.policy.primary) if isinstance(questions, dict) else None
    primary = primary if isinstance(primary, dict) else {}
    dataset = (benchmark or {}).get("dataset")
    card = _text(pack.attestation("DECISION_CARD.md"), "DECISION_CARD.md")
    return {
        "pack_name": m.name,
        "summary": " ".join(m.summary.split()),
        "license": m.license,
        "claimed_publisher": {"name": m.publisher.name, "type": m.publisher.type},
        "risk": {"level": int(m.risk.level), "category": m.risk.category},
        "questions": sorted(m.questions),
        "primary": m.policy.primary,
        # The only names a site's report may use (`add_report`): questions and their labels,
        # and provider slots with their variants and types.
        "names": {
            "questions": {key: m.labels(key) for key in m.questions},
            "slots": {
                slot: {variant: v.type for variant, v in provider.variants.items()}
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
        "benchmark": {
            "dataset": _short_text(dataset.get("name")) if isinstance(dataset, dict) else None,
            "n": _count(primary.get("n")),
            "accuracy": _number(primary.get("accuracy")),
        }
        if benchmark
        else None,
        "leaderboard": leaderboard if _bounded(leaderboard) else None,
        "card_markdown": card,
    }


def _short_text(value: Any) -> str | None:
    return value if isinstance(value, str) and len(value) <= MAX_DATASET_NAME else None


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:  # an integer past the float range
        return None


def _bounded(document: dict[str, Any] | None) -> bool:
    """Whether a JSON document the pack carries nests at most `MAX_LEADERBOARD_DEPTH` levels and
    serializes to at most `MAX_LEADERBOARD_BYTES`."""
    if document is None:
        return False
    stack: list[tuple[Any, int]] = [(document, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_LEADERBOARD_DEPTH:
            return False
        children = (
            node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()
        )
        stack.extend((child, depth + 1) for child in children)
    return len(json.dumps(document)) <= MAX_LEADERBOARD_BYTES


def _json(path: Path) -> dict[str, Any] | None:
    """A JSON object the pack carries about itself, or None if it is missing, not JSON, nested
    too deep or not an object."""
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return found if isinstance(found, dict) else None


def _text(path: Path, name: str) -> str | None:
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HubError(f"{name} is not UTF-8 text") from None


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
        publisher_type = store.namespace(namespace)["publisher_type"]
        return {
            "id": f"{namespace}/{name}",
            "versions": [v["version"] for v in versions],
            "latest": {**versions[-1], "publisher_type": publisher_type},
        }

    @app.get("/v1/packs/{namespace}/{name}/reports")
    def reports(namespace: str, name: str) -> dict[str, Any]:
        """Each site's results next to the pack's own benchmark, version by version."""
        versions = store.versions(namespace, name)
        if not versions:
            raise HubError(f"no pack {namespace}/{name}", 404)
        benchmark = {v["version"]: v.get("benchmark") for v in versions}
        return {
            "id": f"{namespace}/{name}",
            "benchmark": benchmark,
            "sites": store.reports(namespace, name),
        }

    @app.get("/v1/packs/{namespace}/{name}/{version}")
    def pack_version(namespace: str, name: str, version: str) -> dict[str, Any]:
        return store.version(namespace, name, version)

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
            found.append(
                {k: v for k, v in item.items() if k not in ("card_markdown", "leaderboard")}
            )
        return sorted(found, key=lambda item: item["id"])

    return app
