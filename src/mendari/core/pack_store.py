# SPDX-License-Identifier: Apache-2.0
"""An organization's pack store (LP-20e, ADR-0016): versions of packs kept in the engine's own
database, keyed by content digest, as `EvalsetStore` keeps hidden sets.

- **A version never changes.** Its row's files, id, version and digest are written once
  (`audit.STORE_VERSION_COLUMNS`, hashed into its `pack_version.created` event), and only its
  `state` moves, each move one chained event naming its organization: `draft`, `held` (for the
  operator's review), `withdrawn`; LP-20h adds `promoted` and `retired`. An edit is a new
  version whose parent is the one it was made from. `AuditStore.verify` fails on a row changed
  past the store, a state no event moved it to, or a version with no event.
- **The engine follows the bytes** (ADR-0009). `put` writes the files to a scratch directory,
  loads them as origin `store` (on the bounded engine: RE2, the caps, the CPU budget) and keys
  the version by the digest `load_pack` hashed, never one it is told. Files the digest leaves out
  (`evals/`, a `DECISION_CARD.md`, hidden entries) are not kept, so an uploaded card or
  benchmark is never stored; the card is rendered here (`card`). Each version records what the
  one validation function (`evals/validation.py`) found.
- **Each file is kept once** (`store_files`, by SHA-256), whichever versions and organizations
  hold it; `purge` removes a file no version holds but one withdrawn more than
  `PURGE_AFTER_DAYS` ago. An organization keeps at most `settings.store_org_bytes()` of files
  and uploads at most its `store_uploads` quota a day (`MENDARI_ORG_DAILY_LIMITS`).
- **`load` rebuilds from the database alone**: a serverless instance has no persistent disk,
  only a scratch one, so a version is written there on its first use in a process, checked
  against its digest, and kept in a cache bounded in count and bytes.

Nothing here is reached by a route: LP-20f mounts them, behind `MENDARI_PACK_STORE=on` and
memberships mode, once `prerequisites` finds nothing missing."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from mendari.core import accounts, clock
from mendari.core.audit import STORE_STATES, _canonical, _now, store_version_sha256
from mendari.core.pack import Pack, load_pack, pack_digest
from mendari.errors import MendariError

if TYPE_CHECKING:
    from mendari.core.audit import AuditStore

DRAFT, HELD, PROMOTED, RETIRED, WITHDRAWN = "draft", "held", "promoted", "retired", "withdrawn"
STATES = (DRAFT, HELD, PROMOTED, RETIRED, WITHDRAWN)
SOURCES = ("upload", "copy", "edit")
# A version's files: as many and as large as an archive may hold (`hub/archive.py`).
MAX_FILES = 2000
MAX_FILE_BYTES = 50 * 1024 * 1024
PURGE_AFTER_DAYS = 30  # a withdrawn version's files are freed after this (owner decision 14)
# The versions `load` keeps built in one process, and the bytes their files take on its disk.
CACHE_VERSIONS = 32
CACHE_BYTES = 256 * 1024 * 1024
MAX_REASON_CHARS = 500
# Uploads stop after this many held versions in this many days, until the operator clears them
# (owner decision 12): a held answer still tells an uploader one bit.
HELD_STOP = 3
HELD_WINDOW_DAYS = 30
SYSTEM = "mendari"  # who holds a version the hidden-set check matched
MAX_KEYS = 20  # an organization's authors' keys that are not revoked
MAX_PENDING_UPLOADS = 3  # an organization's archives under way in parts at once
MAX_PARTS = 64
# Namespaces no store pack takes, whatever an organization is called.
STORE_RESERVED = ("mendari", "openhealth", "community")


class StoreError(MendariError):
    """A store request refused: `status` is the HTTP status a route answers with (404 no such
    version of this organization, 409 a conflict with what is stored, 413 over a limit, 429
    over a quota, 422 otherwise)."""

    def __init__(self, message: str, status: int = 422):
        self.status = status
        super().__init__(message)


@dataclass(frozen=True)
class Stored:
    """A version as `put` wrote it."""

    id: str
    org: str
    digest: str
    pack_id: str
    version: str
    state: str
    valid: bool


def _kept(path: str) -> bool:
    """Whether a pack's file is part of what its digest covers: not an attestation about the
    pack (`evals/`, a `DECISION_CARD.md`), a hidden entry or a cache (`core/pack.py`)."""
    parts = path.split("/")
    if parts[0] == "evals" or parts[-1] == "DECISION_CARD.md":
        return False
    return not any(part.startswith(".") or part == "__pycache__" for part in parts)


def _check_path(path: str) -> None:
    """Plain relative segments: no absolute path, `..`, empty segment or backslash."""
    parts = path.split("/")
    if (
        not path
        or "\\" in path
        or "\0" in path
        or PurePosixPath(path).is_absolute()
        or any(part in ("", ".", "..") for part in parts)
    ):
        shown = path if path.isprintable() else ascii(path)
        raise StoreError(f"{shown}: not a path inside a pack")


def _write(files: Mapping[str, bytes], root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    base = root.resolve()
    for path, data in files.items():
        target = (base / path).resolve()
        if target == base or not target.is_relative_to(base):
            raise StoreError(f"{path}: not a path inside a pack")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _load(root: Path) -> Pack:
    """The pack at `root` as a store's, on the bounded engine; a `StoreError` naming why not."""
    try:
        return load_pack(root, origin="store")
    except MendariError as error:
        raise StoreError(f"the pack does not load: {error}") from None
    except Exception as error:  # someone else's files: named, never a 500
        raise StoreError(f"the pack does not load ({type(error).__name__})") from None


def _actor(by: str) -> str:
    by = by.strip()
    if not by or len(by) > 200 or not by.isprintable():
        raise StoreError("who acts is named: one line of 1 to 200 characters")
    return by


def _reason(reason: str) -> str:
    reason = " ".join(reason.split())
    if not reason or len(reason) > MAX_REASON_CHARS:
        raise StoreError(f"a reason is one line of 1 to {MAX_REASON_CHARS} characters")
    return reason


def _clear_matches(db: Any, org: str, item_id: str, by: str) -> None:
    db.all(
        "UPDATE store_matches SET cleared_at = ?, cleared_by = ? WHERE org = ? "
        "AND version_id = ? AND cleared_at IS NULL",
        (_now(), by, org, item_id),
    )


def _org(org: str) -> str:
    try:
        return accounts.check_org_id(org)
    except ValueError as error:
        raise StoreError(str(error)) from None


def _row(found: Mapping[str, Any]) -> dict[str, Any]:
    """A version's row as the store answers it: its files and validation parsed."""
    return {
        "id": found["id"],
        "org": found["org"],
        "digest": found["digest"],
        "pack_id": found["pack_id"],
        "version": found["version"],
        "files": json.loads(found["files"]),
        "parent_digest": found["parent_digest"],
        "source": found["source"],
        "copied_from": None if found["copied_from"] is None else json.loads(found["copied_from"]),
        "validation": json.loads(found["validation"]),
        "checks": None if found["checks"] is None else json.loads(found["checks"]),
        "created_by": found["created_by"],
        "created_at": found["created_at"],
        "state": found["state"],
        "state_at": found["state_at"],
    }


def _check_source(source: str, parent: str | None) -> None:
    if source not in SOURCES:
        raise ValueError(f"source is one of {', '.join(SOURCES)}")
    if (source == "edit") != (parent is not None):
        raise ValueError("an edit, and only an edit, names its parent")


@dataclass(frozen=True)
class Prepared:
    """A pack's files as the store keeps them (those its digest covers), loaded on the bounded
    engine, listed by path, hash and size; what the validation found; and, read while its files
    were on disk, every case input it carries and its declared datasets' checks."""

    pack: Pack
    kept: dict[str, bytes]
    listed: list[dict[str, Any]]
    problems: list[dict[str, str]]
    cases: list[Any]
    datasets: list[dict[str, Any]]


def _prepare(files: Mapping[str, bytes]) -> Prepared:
    for path in files:
        _check_path(path)
    kept = {path: bytes(data) for path, data in files.items() if _kept(path)}
    if not kept:
        raise StoreError("no file of a pack: a pack.yaml and the files it names")
    if len(kept) > MAX_FILES:
        raise StoreError(f"more than {MAX_FILES} files", 413)
    large = sorted(path for path, data in kept.items() if len(data) > MAX_FILE_BYTES)
    if large:
        raise StoreError(f"{large[0]}: larger than {MAX_FILE_BYTES} bytes", 413)
    with tempfile.TemporaryDirectory(prefix="mendari-store-") as scratch:
        root = Path(scratch) / "pack"
        _write(kept, root)
        pack = _load(root)
        from mendari.evals.validation import problems

        found = [problem.to_json() for problem in problems(pack)]
        cases, datasets = _cases_of(pack, kept), _dataset_reports(pack)
    listed: list[dict[str, Any]] = [
        {"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        for path, data in sorted(kept.items())
    ]
    return Prepared(pack, kept, listed, found, cases, datasets)


def _cases_of(pack: Pack, kept: Mapping[str, bytes]) -> list[Any]:
    """Every case input the pack carries, whatever a file is called: each line of any file that
    reads as a JSON object with an `input` object (a dataset may have any name), and each
    behaviour test's input, read case by case (one malformed case hides no other). A line that
    does not read is skipped: the validation names it."""
    found: list[Any] = []
    for _, data in sorted(kept.items()):
        for line in data.decode("utf-8", "replace").split("\n"):
            if not line.lstrip().startswith("{"):
                continue
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(record, dict) and isinstance(record.get("input"), dict):
                found.append(record["input"])
    if pack.manifest.tests is not None:
        try:
            cases = pack.read_yaml(pack.manifest.tests)
        except MendariError:
            cases = None
        for case in cases if isinstance(cases, list) else []:
            if isinstance(case, dict) and isinstance(case.get("input"), dict):
                found.append(case["input"])
    return found


def _dataset_reports(pack: Pack) -> list[dict[str, Any]]:
    """The dataset checks (`evals/dataset_checks.py`) of each dataset the pack declares that
    reads: ids and counts, never an input."""
    from mendari.evals import dataset_checks
    from mendari.evals.harness import resolve_dataset

    reports = []
    for name in pack.manifest.evaluation.datasets:
        try:
            dataset = resolve_dataset(pack, name)
        except MendariError:
            continue
        reports.append(dataset_checks.check(pack, dataset).data)
    return reports


def _hidden_matches(cases: list[Any], hidden: Any) -> list[dict[str, str]]:
    """The hidden cases, of every organization's sets, equal to one of `cases` under either
    canonical form (`EvalsetStore.hidden_matches`), by set and case id."""
    found: dict[tuple[str, str], dict[str, str]] = {}
    for value in cases:
        try:
            matched = hidden.hidden_matches(value)
        except MendariError:
            continue  # not JSON a case can hold: the validation names it
        for match in matched:
            found[(match["set"], match["case"])] = match
    return [found[key] for key in sorted(found)]


def _reserved(namespace: str) -> bool:
    """A namespace no store pack takes: each of `STORE_RESERVED`, and any name that starts with
    one and a dash, as the registry reserves them (`hub/registry.py`)."""
    return any(namespace == held or namespace.startswith(f"{held}-") for held in STORE_RESERVED)


NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def _said(state: str) -> str:
    return "held for the operator's review" if state == HELD else state


def _promotion_shown(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "pack_id": row["pack_id"],
        "digest": row["digest"],
        "version": row["version"],
        "authors": json.loads(row["authors"]),
        "approver": row["approver"],
        "reports": json.loads(row["reports"]),
        "reference": row["reference"],
        "note": row["note"],
        "automate": bool(int(row["automate"])),
        "created_at": row["created_at"],
        "retired_at": row["retired_at"],
        "retired_by": row["retired_by"],
    }


_DATASET_METADATA = (
    "id, name, state, created_at, kind, pack_id, digest, sha256, cases, sealed, report, created_by"
)


def _dataset_shown(row: Mapping[str, Any]) -> dict[str, Any]:
    shown = {
        "id": row["id"],
        "name": row["name"],
        "state": row["state"],
        "created_at": row["created_at"],
    }
    if row["state"] != "ready":
        return {**shown, "message": _said(row["state"])}
    return {
        **shown,
        "kind": row["kind"],
        "pack_id": row["pack_id"],
        "digest": row["digest"],
        "sha256": row["sha256"],
        "cases": int(row["cases"]),
        "sealed": bool(int(row["sealed"])),
        "report": json.loads(row["report"]),
        "created_by": row["created_by"],
    }


class PhiRejected(StoreError):
    """An upload refused for what reads as a person's details: `report` is the detector's
    (`mendari.phi-check/1`: kinds, files, lines and counts, never a value)."""

    def __init__(self, report: dict[str, Any]):
        self.report = report
        counts = ", ".join(f"{kind} {n}" for kind, n in report["counts"].items())
        super().__init__(
            f"refused: files hold what reads as a person's details ({counts}); use synthetic "
            "data only",
            422,
        )


class PackStore:
    """The pack stores of every organization, over an `AuditStore` (its database and chain)."""

    def __init__(self, store: AuditStore, *, scratch: Path | None = None) -> None:
        self.store = store
        # This process's scratch directory for loaded versions (`load`), made on first use.
        self._scratch = scratch
        self._lock = threading.Lock()
        self._building: dict[str, threading.Lock] = {}
        self._cache: OrderedDict[str, tuple[Pack, int]] = OrderedDict()

    # -- writes ---------------------------------------------------------------------------------

    def put(
        self,
        org: str,
        files: Mapping[str, bytes],
        *,
        by: str,
        source: str,
        parent: str | None = None,
        copied_from: Mapping[str, str] | None = None,
    ) -> Stored:
        """A new draft version of `org` from a pack's files (path under its root -> content):
        loaded as origin `store`, keyed by the digest that load hashed, validated, chained
        `pack_version.created`. Files outside the digest are dropped first. Refused (a
        `StoreError`, nothing written) when the pack does not load, `org` holds this digest
        already, `parent` is not one of `org`'s versions it may build on, or a limit or quota is
        passed. The checks of an upload (PHI, namespace, hidden cases) are `admit`'s."""
        org, by = _org(org), _actor(by)
        _check_source(source, parent)
        ready = _prepare(files)
        return self._insert(
            org,
            ready.pack,
            ready.kept,
            ready.listed,
            ready.problems,
            by=by,
            source=source,
            parent=parent,
            copied_from=copied_from,
        )

    def admit(
        self,
        org: str,
        files: Mapping[str, bytes],
        *,
        by: str,
        source: str,
        parent: str | None = None,
        copied_from: Mapping[str, str] | None = None,
        hidden: Any = None,
        claimed: Callable[[str, str], bool] | None = None,
        expect_digest: str | None = None,
    ) -> Stored:
        """A new version of `org` that passed every check an upload, a copy or an edit passes,
        in this order, before any row is written (LP-20f): the organization's uploads are not
        stopped (`HELD_STOP` held versions in `HELD_WINDOW_DAYS`); no file reads as a person's
        details (`core/phi_detect.py`; `PhiRejected`, with the report: kinds, files and lines,
        never a value); the pack loads on the bounded engine; its namespace is `org`'s
        (`claimed(org, namespace)` may allow one more, an approved registry claim) and never a
        reserved one; it is the pack `expect_digest` names, if one does; then the validation
        and dataset checks, recorded with it; and the hidden-set check: with `hidden` (an
        `EvalsetStore` with the index key) each case the pack carries is looked up, and on any
        match the version is stored `held` and the match recorded for the operator only.
        Without `hidden` the check is "not applicable": this deployment holds no hidden set."""
        from mendari.core import phi_detect

        org, by = _org(org), _actor(by)
        _check_source(source, parent)
        self._refuse_stopped(org)
        for path in files:
            _check_path(path)
        found = phi_detect.detect_files({p: d for p, d in files.items() if _kept(p)})
        if phi_detect.rejects(found):
            raise PhiRejected(found.to_json())
        ready = _prepare(files)
        pack = ready.pack
        namespace = pack.id.partition("/")[0]
        if _reserved(namespace) or (
            namespace != org and not (claimed is not None and claimed(org, namespace))
        ):
            raise StoreError(
                f"a pack in this organization's store is named {org}/<name>: {pack.id!r} is "
                "not (a copy is renamed on the way in)"
            )
        if expect_digest is not None and pack.digest != expect_digest:
            raise StoreError("the archive's files are not the pack its manifest names")
        checks: dict[str, Any] = {
            "phi": {"files": found.files, "findings": len(found.findings)},
            "datasets": ready.datasets,
            "hidden": "not_applicable" if hidden is None else "checked",
        }
        matches = None if hidden is None else _hidden_matches(ready.cases, hidden)
        return self._insert(
            org,
            pack,
            ready.kept,
            ready.listed,
            ready.problems,
            by=by,
            source=source,
            parent=parent,
            copied_from=copied_from,
            checks=checks,
            matches=matches or None,
        )

    def _refuse_stopped(self, org: str, db: Any = None) -> None:
        """A `StoreError` (429) when `org` has `HELD_STOP` held versions or datasets in
        `HELD_WINDOW_DAYS` that the operator has not cleared; read in `db` when given."""
        since = (datetime.now(UTC) - timedelta(days=HELD_WINDOW_DAYS)).isoformat()
        query = (
            "SELECT COUNT(*) AS n FROM store_matches WHERE org = ? AND created_at >= ? "
            "AND cleared_at IS NULL"
        )
        if db is None:
            with self.store._db.session(exclusive=False) as session:
                held = session.one(query, (org, since))
        else:
            held = db.one(query, (org, since))
        if held is not None and int(held["n"]) >= HELD_STOP:
            raise StoreError(
                "uploads are stopped until the operator reviews this organization's held uploads",
                429,
            )

    def _insert(
        self,
        org: str,
        pack: Pack,
        kept: Mapping[str, bytes],
        listed: list[dict[str, Any]],
        validation: list[dict[str, str]],
        *,
        by: str,
        source: str,
        parent: str | None,
        copied_from: Mapping[str, str] | None,
        checks: Mapping[str, Any] | None = None,
        matches: list[dict[str, str]] | None = None,
    ) -> Stored:
        from mendari import settings

        uploads = settings.org_daily_limits().get("store_uploads")
        allowed_bytes = settings.store_org_bytes()
        by_sha = {item["sha256"]: kept[item["path"]] for item in listed}
        day = clock.today().isoformat()
        with self.store._db.session(exclusive=True) as db:
            if db.one(
                "SELECT id FROM store_versions WHERE org = ? AND digest = ?", (org, pack.digest)
            ):
                raise StoreError(
                    f"this organization's store holds {pack.digest} already: a version never "
                    "changes",
                    409,
                )
            if matches:  # the stop again, in this transaction: parallel uploads count too
                self._refuse_stopped(org, db)
            if parent is not None:
                above = db.one(
                    "SELECT state FROM store_versions WHERE org = ? AND digest = ?", (org, parent)
                )
                if above is None:
                    raise StoreError("no such version in this organization's store", 404)
                if above["state"] in (HELD, WITHDRAWN):
                    raise StoreError(f"version {parent} is {above['state']}: nothing builds on it")
            used = db.one("SELECT uploads FROM store_usage WHERE org = ? AND day = ?", (org, day))
            if uploads is not None and (used["uploads"] if used else 0) >= uploads:
                raise StoreError(
                    f"this organization has added its {uploads} versions for today", 429
                )
            held = self._org_shas(db, org)
            sizes = {
                str(row["sha256"]): int(row["size"])
                for row in db.all("SELECT sha256, size FROM store_files")
                if str(row["sha256"]) in held
            }
            new = {sha: len(data) for sha, data in by_sha.items() if sha not in held}
            if sum(sizes.values()) + sum(new.values()) > allowed_bytes:
                raise StoreError(
                    f"this organization's store would hold more than {allowed_bytes} bytes of "
                    "files (MENDARI_STORE_ORG_BYTES)",
                    413,
                )
            for sha, data in by_sha.items():
                db.all(
                    "INSERT INTO store_files (sha256, size, data) VALUES (?, ?, ?) "
                    "ON CONFLICT (sha256) DO NOTHING",
                    (sha, len(data), data),
                )
            now, version_id = _now(), str(uuid.uuid4())
            row = {
                "id": version_id,
                "org": org,
                "digest": pack.digest,
                "pack_id": pack.id,
                "version": pack.version,
                "files": _canonical(listed),
                "parent_digest": parent,
                "source": source,
                "copied_from": None if copied_from is None else _canonical(dict(copied_from)),
                "validation": _canonical(validation),
                "checks": None if checks is None else _canonical(dict(checks)),
                "created_by": by,
                "created_at": now,
            }
            state = HELD if matches else DRAFT
            db.all(
                f"INSERT INTO store_versions ({', '.join(row)}, state, state_at) "
                f"VALUES ({', '.join('?' for _ in row)}, ?, ?)",
                (*row.values(), state, now),
            )
            db.all(
                "INSERT INTO store_usage (org, day, uploads, bytes) VALUES (?, ?, 1, ?) "
                "ON CONFLICT (org, day) DO UPDATE SET uploads = store_usage.uploads + 1, "
                "bytes = store_usage.bytes + excluded.bytes",
                (org, day, sum(new.values())),
            )
            payload = {
                "digest": pack.digest,
                "pack": pack.id,
                "version": pack.version,
                "source": source,
                "parent": parent,
                "by": by,
                "valid": not validation,
                "version_sha256": store_version_sha256(row),
            }
            chained = self.store._bound("pack_version.created", payload, org)
            self.store._append(db, f"pack_version:{version_id}", "pack_version.created", chained)
            if matches:  # held at once, in the same transaction; the event names no case
                payload = {"digest": pack.digest, "by": SYSTEM}
                marked = self.store._bound("pack_version.held", payload, org)
                self.store._append(db, f"pack_version:{version_id}", "pack_version.held", marked)
                db.all(
                    "INSERT INTO store_matches (id, org, version_id, digest, matches, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), org, version_id, pack.digest, _canonical(matches), now),
                )
        return Stored(version_id, org, pack.digest, pack.id, pack.version, state, not validation)

    def _org_shas(self, db: Any, org: str) -> set[str]:
        held: set[str] = set()
        for row in db.all("SELECT files FROM store_versions WHERE org = ?", (org,)):
            held.update(item["sha256"] for item in json.loads(row["files"]))
        return held

    def _move(
        self,
        org: str,
        digest: str,
        kind: str,
        allowed: tuple[str, ...],
        *,
        by: str,
        reason: str | None = None,
        also: Callable[[Any, str], None] | None = None,
    ) -> dict[str, Any]:
        """Move a version's state, one chained event; `also(db, version_id)` runs in the same
        transaction (a match recorded or cleared with it)."""
        org, by = _org(org), _actor(by)
        if reason is not None:
            reason = _reason(reason)
        with self.store._db.session(exclusive=True) as db:
            found = db.one(
                "SELECT id, state FROM store_versions WHERE org = ? AND digest = ?", (org, digest)
            )
            if found is None:
                raise StoreError("no such version in this organization's store", 404)
            if found["state"] not in allowed:
                raise StoreError(f"version {digest} is {found['state']}", 409)
            state, now = STORE_STATES[kind], _now()
            db.all(
                "UPDATE store_versions SET state = ?, state_at = ? WHERE id = ?",
                (state, now, found["id"]),
            )
            payload: dict[str, Any] = {"digest": digest, "by": by}
            if reason is not None:
                payload["reason"] = reason
            chained, chain = self.store._bound(kind, payload, org), f"pack_version:{found['id']}"
            # Each kind written out, so the inventory of chained events reads it (test_shipped).
            if kind == "pack_version.held":
                self.store._append(db, chain, "pack_version.held", chained)
            elif kind == "pack_version.released":
                self.store._append(db, chain, "pack_version.released", chained)
            else:
                self.store._append(db, chain, "pack_version.withdrawn", chained)
            if found["state"] == HELD:
                _clear_matches(db, org, str(found["id"]), by)
            if also is not None:
                also(db, str(found["id"]))
        if state != DRAFT:
            self._forget(digest)
        return {"org": org, "digest": digest, "state": state, "state_at": now}

    def hold(self, org: str, digest: str, *, by: str) -> dict[str, Any]:
        """Hold a draft for the operator's review (LP-20f's hidden-set check): it is not tested,
        evaluated, edited from or promoted. The chained event names the digest, never why."""
        return self._move(org, digest, "pack_version.held", (DRAFT,), by=by)

    def release(self, org: str, digest: str, *, by: str, reason: str) -> dict[str, Any]:
        """The operator releases a held version: a draft again."""
        return self._move(org, digest, "pack_version.released", (HELD,), by=by, reason=reason)

    def withdraw(
        self,
        org: str,
        digest: str,
        *,
        by: str,
        reason: str | None = None,
        allowed: tuple[str, ...] = (DRAFT, HELD),
    ) -> dict[str, Any]:
        """Withdraw a draft (`allowed`: an author's route takes drafts only) or a held version
        (the operator's): its row and events stay, its files are freed `PURGE_AFTER_DAYS` later
        (`purge`)."""
        return self._move(org, digest, "pack_version.withdrawn", allowed, by=by, reason=reason)

    def purge(self, *, now: datetime | None = None) -> int:
        """Remove each kept file that no version holds but one withdrawn more than
        `PURGE_AFTER_DAYS` ago; how many were removed. The rows and events stay."""
        cutoff = ((now or datetime.now(UTC)) - timedelta(days=PURGE_AFTER_DAYS)).isoformat()
        with self.store._db.session(exclusive=True) as db:
            needed: set[str] = set()
            for row in db.all("SELECT files, state, state_at FROM store_versions"):
                if row["state"] == WITHDRAWN and str(row["state_at"]) < cutoff:
                    continue
                needed.update(item["sha256"] for item in json.loads(row["files"]))
            gone = [
                str(row["sha256"])
                for row in db.all("SELECT sha256 FROM store_files")
                if str(row["sha256"]) not in needed
            ]
            for sha in gone:
                db.all("DELETE FROM store_files WHERE sha256 = ?", (sha,))
        return len(gone)

    # -- reads ----------------------------------------------------------------------------------

    def versions(self, org: str, pack_id: str | None = None) -> list[dict[str, Any]]:
        """`org`'s versions (of `pack_id`, if named), newest first; never another
        organization's."""
        org = _org(org)
        where, params = "org = ?", [org]
        if pack_id is not None:
            where, params = f"{where} AND pack_id = ?", [org, pack_id]
        with self.store._db.session(exclusive=False) as db:
            rows = db.all(
                f"SELECT * FROM store_versions WHERE {where} ORDER BY created_at DESC, id",
                tuple(params),
            )
        return [_row(row) for row in rows]

    def version(self, org: str, digest: str) -> dict[str, Any]:
        """One of `org`'s versions; a `StoreError` (404) for one it does not hold."""
        org = _org(org)
        with self.store._db.session(exclusive=False) as db:
            found = db.one(
                "SELECT * FROM store_versions WHERE org = ? AND digest = ?", (org, digest)
            )
        if found is None:
            raise StoreError("no such version in this organization's store", 404)
        return _row(found)

    def files(self, org: str, digest: str) -> dict[str, bytes]:
        """One of `org`'s versions' files, by path, from the database."""
        listed = self.version(org, digest)["files"]
        with self.store._db.session(exclusive=False) as db:
            blobs = {}
            for item in listed:
                row = db.one("SELECT data FROM store_files WHERE sha256 = ?", (item["sha256"],))
                if row is None:
                    raise StoreError(f"version {digest}'s files are no longer kept", 410)
                blobs[item["path"]] = bytes(row["data"])
        for item in listed:
            if hashlib.sha256(blobs[item["path"]]).hexdigest() != item["sha256"]:
                raise StoreError(f"version {digest}: a stored file does not match its hash", 500)
        return blobs

    def load(self, org: str, digest: str) -> Pack:
        """One of `org`'s versions as a `Pack` of origin `store`: written from the database to
        this process's scratch directory on its first use, its digest checked, and kept in a
        cache bounded in count and bytes. A request that asks while it is built waits for that
        build and never starts a second."""
        self.version(org, digest)  # `org` holds it, whoever else does
        with self._lock:
            cached = self._cache.get(digest)
            if cached is not None and cached[0].root.is_dir():
                self._cache.move_to_end(digest)
                return cached[0]
            building = self._building.setdefault(digest, threading.Lock())
        with building:
            with self._lock:
                cached = self._cache.get(digest)
                if cached is not None and cached[0].root.is_dir():
                    return cached[0]
            root = self._scratch_dir() / digest.replace(":", "-") / uuid.uuid4().hex
            try:
                files = self.files(org, digest)
                _write(files, root)
                pack = _load(root)
                if pack.digest != digest:
                    raise StoreError(f"version {digest}'s files hash to {pack.digest}", 500)
            except BaseException:
                shutil.rmtree(root, ignore_errors=True)
                with self._lock:
                    self._building.pop(digest, None)
                raise
            size = sum(len(data) for data in files.values())
            with self._lock:
                self._cache[digest] = (pack, size)
                self._building.pop(digest, None)
                self._evict()
        return pack

    def card(self, org: str, digest: str) -> str:
        """The version's Decision Card, rendered here from its files (`card.card_of`): nothing
        uploaded about it is shown."""
        from mendari.core.card import card_of

        return card_of(self.load(org, digest))

    def _scratch_dir(self) -> Path:
        if self._scratch is None:
            self._scratch = Path(tempfile.mkdtemp(prefix="mendari-store-"))
        self._scratch.mkdir(parents=True, exist_ok=True)
        return self._scratch

    def _evict(self) -> None:
        """Called under the lock: drop the least recently used versions past the bounds."""
        while self._cache and (
            len(self._cache) > CACHE_VERSIONS
            or sum(size for _, size in self._cache.values()) > CACHE_BYTES
        ):
            _, (pack, _) = self._cache.popitem(last=False)
            shutil.rmtree(pack.root, ignore_errors=True)

    def _forget(self, digest: str) -> None:
        with self._lock:
            cached = self._cache.pop(digest, None)
        if cached is not None:
            shutil.rmtree(cached[0].root, ignore_errors=True)

    # -- authors' keys (LP-20f) -----------------------------------------------------------------

    def add_key(self, org: str, public_pem: str, *, by: str) -> dict[str, Any]:
        """Register an author's Ed25519 public key (PEM) for `org`'s store: an upload must be
        signed by one of `org`'s keys that is not revoked. Chained `store_key.added`."""
        from mendari.errors import HubError
        from mendari.hub import archive

        org, by = _org(org), _actor(by)
        try:
            key = archive.load_public_key(public_pem.encode())
        except (HubError, ValueError, UnicodeError):
            raise StoreError("not an Ed25519 public key in PEM") from None
        key_id = archive.key_id(key)
        pem = archive.public_pem(key).decode()
        with self.store._db.session(exclusive=True) as db:
            if db.one("SELECT id FROM store_keys WHERE org = ? AND key_id = ?", (org, key_id)):
                raise StoreError(f"key {key_id} is registered already", 409)
            if (
                db.one(
                    "SELECT COUNT(*) AS n FROM store_keys WHERE org = ? AND revoked_at IS NULL",
                    (org,),
                )["n"]
                >= MAX_KEYS
            ):
                raise StoreError(f"an organization's store has {MAX_KEYS} keys at most", 409)
            row_id, now = str(uuid.uuid4()), _now()
            db.all(
                "INSERT INTO store_keys (id, org, key_id, public_pem, added_by, added_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (row_id, org, key_id, pem, by, now),
            )
            chained = self.store._bound("store_key.added", {"key_id": key_id, "by": by}, org)
            self.store._append(db, f"store_key:{row_id}", "store_key.added", chained)
        return {"key_id": key_id, "added_by": by, "added_at": now, "revoked_at": None}

    def revoke_key(self, org: str, key_id: str, *, by: str) -> dict[str, Any]:
        """Revoke one of `org`'s author keys: no upload signed by it is taken from now on (the
        versions it signed stay). Chained `store_key.revoked`."""
        org, by = _org(org), _actor(by)
        with self.store._db.session(exclusive=True) as db:
            found = db.one(
                "SELECT id, revoked_at FROM store_keys WHERE org = ? AND key_id = ?",
                (org, key_id),
            )
            if found is None:
                raise StoreError("no such key in this organization's store", 404)
            if found["revoked_at"] is not None:
                raise StoreError(f"key {key_id} is revoked already", 409)
            now = _now()
            db.all("UPDATE store_keys SET revoked_at = ? WHERE id = ?", (now, found["id"]))
            chained = self.store._bound("store_key.revoked", {"key_id": key_id, "by": by}, org)
            self.store._append(db, f"store_key:{found['id']}", "store_key.revoked", chained)
        return {"key_id": key_id, "revoked_at": now}

    def keys(self, org: str) -> list[dict[str, Any]]:
        """`org`'s author keys, oldest first: id, who added it and when, and when revoked."""
        org = _org(org)
        with self.store._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT key_id, added_by, added_at, revoked_at FROM store_keys WHERE org = ? "
                "ORDER BY added_at, key_id",
                (org,),
            )
        return [dict(row) for row in rows]

    def trusted(self, org: str) -> dict[str, Any]:
        """`org`'s keys that are not revoked, by key id, as `archive.verify` takes them."""
        from mendari.hub import archive

        org = _org(org)
        with self.store._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT key_id, public_pem FROM store_keys WHERE org = ? AND revoked_at IS NULL",
                (org,),
            )
        return {
            str(row["key_id"]): archive.load_public_key(str(row["public_pem"]).encode())
            for row in rows
        }

    # -- an archive in parts (LP-20f) -----------------------------------------------------------

    def begin_upload(self, org: str, *, by: str, size: int, sha256: str) -> dict[str, Any]:
        """Start an archive of `size` bytes with this SHA-256 sent in parts, each under the
        engine's body limit. At most `MAX_PENDING_UPLOADS` an organization at once; one left a
        day is dropped."""
        from mendari import settings

        org, by = _org(org), _actor(by)
        limit = settings.store_max_archive_bytes()
        if not 0 < size <= limit:
            raise StoreError(
                f"an archive is 1 to {limit} bytes (MENDARI_STORE_MAX_ARCHIVE_BYTES)", 413
            )
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise StoreError("sha256 is 64 lower-case hexadecimal digits")
        stale = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        with self.store._db.session(exclusive=True) as db:
            for row in db.all("SELECT id FROM store_uploads WHERE created_at < ?", (stale,)):
                db.all("DELETE FROM store_upload_parts WHERE upload_id = ?", (row["id"],))
                db.all("DELETE FROM store_uploads WHERE id = ?", (row["id"],))
            pending = db.one("SELECT COUNT(*) AS n FROM store_uploads WHERE org = ?", (org,))
            if int(pending["n"]) >= MAX_PENDING_UPLOADS:
                raise StoreError(
                    f"this organization has {MAX_PENDING_UPLOADS} uploads under way: finish one",
                    429,
                )
            upload_id, now = str(uuid.uuid4()), _now()
            db.all(
                "INSERT INTO store_uploads (id, org, created_by, size, sha256, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (upload_id, org, by, size, sha256, now),
            )
        return {"upload": upload_id, "size": size, "sha256": sha256, "created_at": now}

    def _upload(self, db: Any, org: str, upload_id: str) -> Any:
        found = db.one(
            "SELECT id, size, sha256 FROM store_uploads WHERE id = ? AND org = ?", (upload_id, org)
        )
        if found is None:
            raise StoreError("no such upload under way in this organization", 404)
        return found

    def add_part(self, org: str, upload_id: str, n: int, data: bytes) -> dict[str, Any]:
        """Part `n` (from 0) of an upload under way; a part sent again replaces it. Refused past
        the archive's declared size."""
        org = _org(org)
        if not 0 <= n < MAX_PARTS:
            raise StoreError(f"a part's number is 0 to {MAX_PARTS - 1}")
        if not data:
            raise StoreError("a part holds at least one byte")
        with self.store._db.session(exclusive=True) as db:
            found = self._upload(db, org, upload_id)
            others = db.one(
                "SELECT COALESCE(SUM(LENGTH(data)), 0) AS n FROM store_upload_parts "
                "WHERE upload_id = ? AND n <> ?",
                (upload_id, n),
            )
            received = int(others["n"]) + len(data)
            if received > int(found["size"]):
                raise StoreError("the parts are larger than the archive's declared size", 413)
            db.all("DELETE FROM store_upload_parts WHERE upload_id = ? AND n = ?", (upload_id, n))
            db.all(
                "INSERT INTO store_upload_parts (upload_id, n, data) VALUES (?, ?, ?)",
                (upload_id, n, data),
            )
        return {"upload": upload_id, "part": n, "received": received}

    def finish_upload(self, org: str, upload_id: str) -> bytes:
        """The archive an upload's parts make, joined in order and checked against its declared
        size and SHA-256; the upload is gone afterwards whatever the check found."""
        org = _org(org)
        with self.store._db.session(exclusive=True) as db:
            found = self._upload(db, org, upload_id)
            parts = db.all(
                "SELECT n, data FROM store_upload_parts WHERE upload_id = ? ORDER BY n",
                (upload_id,),
            )
            db.all("DELETE FROM store_upload_parts WHERE upload_id = ?", (upload_id,))
            db.all("DELETE FROM store_uploads WHERE id = ?", (upload_id,))
        if [int(part["n"]) for part in parts] != list(range(len(parts))):
            raise StoreError("a part is missing: parts are numbered from 0 with none left out")
        data = b"".join(bytes(part["data"]) for part in parts)
        if len(data) != int(found["size"]) or hashlib.sha256(data).hexdigest() != found["sha256"]:
            raise StoreError("the parts do not join into the archive declared (size or sha256)")
        return data

    # -- datasets (LP-20g) ------------------------------------------------------------------

    def add_dataset(
        self,
        org: str,
        raw: bytes,
        *,
        by: str,
        name: str,
        digest: str,
        hidden: Any = None,
        kind: str = "upload",
    ) -> dict[str, Any]:
        """One of `org`'s datasets (JSON Lines), checked exactly as a version is: the PHI
        detector (`PhiRejected`), the dataset checks against version `digest` (kept and shown;
        a schema or label problem refuses it) and the hidden-set check (held on any match, for
        the operator, its uploader told only that). Kept sealed under the PHI policy when the
        mode is `encrypt`. Chained `store_dataset.created` (and `store_dataset.held`), with
        counts only."""
        from mendari.core import phi_detect
        from mendari.evals import dataset_checks
        from mendari.evals.harness import load_dataset

        org, by = _org(org), _actor(by)
        if not NAME_RE.fullmatch(name):
            raise StoreError("a dataset's name is lower-case letters, digits and hyphens")
        if kind not in ("upload", "improvement"):
            raise ValueError("a dataset is an upload or an improvement dataset")
        # An improvement dataset (LP-20i) is the organization's own reviewed decisions: patients'
        # inputs by construction, so neither the PHI detector nor the hidden-set check is for it;
        # it is always sealed where the policy seals.
        if kind == "upload":
            self._refuse_stopped(org)
            found = phi_detect.detect_files({f"{name}.jsonl": raw})
            if phi_detect.rejects(found):
                raise PhiRejected(found.to_json())
        row = self.version(org, digest)
        if row["state"] in (HELD, WITHDRAWN):
            raise StoreError(f"version {digest} is {row['state']}", 409)
        pack = self.load(org, digest)
        try:
            data = load_dataset(f"{name}.jsonl", name=name, raw=raw, label=f"{name}.jsonl")
        except MendariError as error:
            raise StoreError(str(error)) from None
        report = dataset_checks.check(pack, data, training={})
        if not report.passed:
            raise StoreError("the dataset does not fit the pack: schema or label problems", 422)
        matches = (
            []
            if hidden is None or kind != "upload"
            else _hidden_matches([record["input"] for record in data.records], hidden)
        )
        dataset_id, now = str(uuid.uuid4()), _now()
        state = HELD if matches else "ready"
        text = raw.decode("utf-8")
        kept = (
            self.store.phi.seal("dataset", text, dataset_id)
            if self.store.phi.mode == "encrypt"
            else text
        )
        with self.store._db.session(exclusive=True) as db:
            if kind == "upload":
                self._refuse_stopped(org, db)
            db.all(
                "INSERT INTO store_datasets (id, org, name, pack_id, digest, sha256, cases, "
                "data, sealed, report, created_by, created_at, state, kind) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (dataset_id, org, name, pack.id, digest, data.sha256, len(data.records), kept,
                 int(self.store.phi.mode == "encrypt"), _canonical(report.data), by, now, state,
                 kind),
            )  # fmt: skip
            payload = {"dataset": dataset_id, "sha256": data.sha256, "kind": kind,
                       "cases": len(data.records), "by": by}  # fmt: skip
            chained = self.store._bound("store_dataset.created", payload, org)
            self.store._append(db, f"store_dataset:{dataset_id}", "store_dataset.created", chained)
            if matches:
                marked = self.store._bound("store_dataset.held", {"dataset": dataset_id}, org)
                self.store._append(db, f"store_dataset:{dataset_id}", "store_dataset.held", marked)
                db.all(
                    "INSERT INTO store_matches (id, org, version_id, digest, matches, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), org, dataset_id, f"dataset:{dataset_id}",
                     _canonical(matches), now),
                )  # fmt: skip
        return self.dataset(org, dataset_id)

    def _dataset_row(self, org: str, dataset_id: str, *, with_data: bool = False) -> Any:
        org = _org(org)
        columns = "name, state, sealed, data" if with_data else _DATASET_METADATA
        with self.store._db.session(exclusive=False) as db:
            found = db.one(
                f"SELECT {columns} FROM store_datasets WHERE id = ? AND org = ?", (dataset_id, org)
            )
        if found is None:
            raise StoreError("no such dataset in this organization's store", 404)
        return found

    def dataset(self, org: str, dataset_id: str) -> dict[str, Any]:
        """One of `org`'s datasets as its uploader may see it: its name, SHA-256, how many cases,
        the version it was checked against and its check report (ids and counts). A held one:
        its state and nothing else. Never a case."""
        return _dataset_shown(self._dataset_row(org, dataset_id))

    def datasets(self, org: str) -> list[dict[str, Any]]:
        org = _org(org)
        with self.store._db.session(exclusive=False) as db:
            rows = db.all(
                f"SELECT {_DATASET_METADATA} FROM store_datasets WHERE org = ? "
                "ORDER BY created_at DESC, id",
                (org,),
            )
        return [_dataset_shown(row) for row in rows]

    def dataset_bytes(self, org: str, dataset_id: str) -> tuple[bytes, str]:
        """A ready dataset's JSON Lines and its name, opened under the PHI policy: for a job
        (`evals/store_jobs.py`), never a route."""
        row = self._dataset_row(org, dataset_id, with_data=True)
        if row["state"] != "ready":
            raise StoreError(f"dataset {dataset_id} is {_said(row['state'])}", 409)
        text = (
            self.store.phi.open("dataset", str(row["data"]), dataset_id)[0]
            if int(row["sealed"])
            else str(row["data"])
        )
        return str(text).encode("utf-8"), str(row["name"])

    # -- promotion (LP-20h) ---------------------------------------------------------------------

    def authors(self, org: str, digest: str) -> list[str]:
        """Everyone who made a version on the chain from `digest` back to its upload or copy:
        none of them may promote it (an edit by a second person frees nobody)."""
        found: list[str] = []
        seen: set[str] = set()
        current: str | None = digest
        with self.store._db.session(exclusive=False) as db:
            while current is not None and current not in seen:
                seen.add(current)
                row = db.one(
                    "SELECT created_by, parent_digest FROM store_versions WHERE org = ? "
                    "AND digest = ?",
                    (org, current),
                )
                if row is None:
                    break
                if row["created_by"] not in found:
                    found.append(str(row["created_by"]))
                current = row["parent_digest"]
        return found

    def promote(
        self,
        org: str,
        digest: str,
        *,
        approver: str,
        reports: list[str],
        reference: str,
        note: str | None = None,
        automate: bool = False,
        served_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Sign a version off to be served to `org` (LP-20h), chained `pack_version.promoted`
        with the digest, its authors, the approver and the reports relied on; the promoted
        version of the same pack before it is retired in the same transaction. Refused when
        the approver made any version on its chain, no report is named or a named report is not
        `org`'s evaluation of exactly this digest, the version is not a validated draft (held,
        withdrawn or with validation problems), the deployment serves its id from disk, or a
        pack of risk level 4 or above is promoted to automate (it never does, ADR-0002)."""
        org, approver = _org(org), _actor(approver)
        reference = " ".join(reference.split())
        if not reference or len(reference) > MAX_REASON_CHARS:
            raise StoreError(f"a reference is one line of 1 to {MAX_REASON_CHARS} characters")
        if note is not None and len(note) > 2000:
            raise StoreError("a note is 2,000 characters at most")
        if not reports:
            raise StoreError("a promotion names the evaluation reports it relied on")
        row = self.version(org, digest)
        authors = self.authors(org, digest)
        if approver in authors:
            raise StoreError(
                "the author of a version, or of any version it was made from, cannot promote "
                "it: someone else signs it off",
                403,
            )
        if row["state"] not in (DRAFT, RETIRED):
            raise StoreError(f"version {digest} is {_said(row['state'])}", 409)
        if row["validation"]:
            raise StoreError("a version that does not validate is not promoted", 409)
        if row["pack_id"] in set(served_ids):
            raise StoreError(
                f"this deployment serves {row['pack_id']} from disk: a store version never "
                "replaces it",
                409,
            )
        pack = self.load(org, digest)
        if automate and int(pack.manifest.risk.level) >= 4:
            raise StoreError("a pack of risk level 4 or above never automates (ADR-0002)")
        with self.store._db.session(exclusive=True) as db:
            for report_id in reports:
                found = db.one(
                    "SELECT report FROM eval_reports WHERE id = ? AND org = ?", (report_id, org)
                )
                named = None if found is None else json.loads(found["report"]).get("pack", {})
                if named is None or named.get("digest") != digest:
                    raise StoreError(
                        f"report {report_id} is not this organization's evaluation of this version",
                        422,
                    )
            current = db.one("SELECT state FROM store_versions WHERE id = ?", (row["id"],))
            if current is None or current["state"] not in (DRAFT, RETIRED):
                raise StoreError(f"version {digest} is no longer a draft", 409)
            now, promotion_id = _now(), str(uuid.uuid4())
            before = db.one(
                "SELECT id, version_id, digest FROM store_promotions WHERE org = ? AND pack_id = ? "
                "AND retired_at IS NULL",
                (org, row["pack_id"]),
            )
            if before is not None:
                db.all(
                    "UPDATE store_promotions SET retired_at = ?, retired_by = ? WHERE id = ?",
                    (now, approver, before["id"]),
                )
                db.all(
                    "UPDATE store_versions SET state = ?, state_at = ? WHERE id = ?",
                    (RETIRED, now, before["version_id"]),
                )
                payload = {"digest": before["digest"], "by": approver, "promotion": before["id"],
                           "replaced_by": promotion_id}  # fmt: skip
                chained = self.store._bound("pack_version.retired", payload, org)
                self.store._append(
                    db, f"pack_version:{before['version_id']}", "pack_version.retired", chained
                )
            db.all(
                "INSERT INTO store_promotions (id, org, pack_id, digest, version, version_id, "
                "authors, approver, reports, reference, note, automate, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (promotion_id, org, row["pack_id"], digest, row["version"], row["id"],
                 _canonical(authors), approver, _canonical(sorted(set(reports))), reference,
                 note, int(automate), now),
            )  # fmt: skip
            db.all(
                "UPDATE store_versions SET state = ?, state_at = ? WHERE id = ?",
                (PROMOTED, now, row["id"]),
            )
            payload = {
                "digest": digest,
                "promotion": promotion_id,
                "authors": authors,
                "approver": approver,
                "reports": sorted(set(reports)),
                "automate": automate,
            }
            chained = self.store._bound("pack_version.promoted", payload, org)
            self.store._append(db, f"pack_version:{row['id']}", "pack_version.promoted", chained)
        if before is not None:
            self._forget(str(before["digest"]))
        return self.promotion(org, promotion_id)

    def retire(self, org: str, promotion_id: str, *, by: str) -> dict[str, Any]:
        """Withdraw a promotion: its version is `retired`, and the organization serves no version
        of that pack until another is promoted. Chained `pack_version.retired`."""
        org, by = _org(org), _actor(by)
        with self.store._db.session(exclusive=True) as db:
            found = db.one(
                "SELECT id, version_id, digest, retired_at FROM store_promotions WHERE id = ? "
                "AND org = ?",
                (promotion_id, org),
            )
            if found is None:
                raise StoreError("no such promotion in this organization", 404)
            if found["retired_at"] is not None:
                raise StoreError("this promotion is retired already", 409)
            now = _now()
            db.all(
                "UPDATE store_promotions SET retired_at = ?, retired_by = ? WHERE id = ?",
                (now, by, promotion_id),
            )
            db.all(
                "UPDATE store_versions SET state = ?, state_at = ? WHERE id = ?",
                (RETIRED, now, found["version_id"]),
            )
            payload = {"digest": found["digest"], "by": by, "promotion": promotion_id}
            chained = self.store._bound("pack_version.retired", payload, org)
            self.store._append(
                db, f"pack_version:{found['version_id']}", "pack_version.retired", chained
            )
        self._forget(str(found["digest"]))
        return self.promotion(org, promotion_id)

    def promotion(self, org: str, promotion_id: str) -> dict[str, Any]:
        with self.store._db.session(exclusive=False) as db:
            found = db.one(
                "SELECT * FROM store_promotions WHERE id = ? AND org = ?", (promotion_id, org)
            )
        if found is None:
            raise StoreError("no such promotion in this organization", 404)
        return _promotion_shown(found)

    def promotions(self, org: str) -> list[dict[str, Any]]:
        """`org`'s promotions, newest first, the ones in force and the retired."""
        org = _org(org)
        with self.store._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT * FROM store_promotions WHERE org = ? ORDER BY created_at DESC, id", (org,)
            )
        return [_promotion_shown(row) for row in rows]

    def promoted(self, org: str) -> dict[str, tuple[str, str, bool]]:
        """What `org` is served from its store: pack id -> (digest, promotion id, whether it was
        promoted to automate), the promotions in force. One indexed read."""
        with self.store._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT pack_id, digest, id, automate FROM store_promotions WHERE org = ? "
                "AND retired_at IS NULL",
                (org,),
            )
        return {
            str(r["pack_id"]): (str(r["digest"]), str(r["id"]), bool(int(r["automate"])))
            for r in rows
        }

    # -- proposed tests from the improvement dataset (LP-20i) -------------------------------------

    def dataset_records(self, org: str, dataset_id: str) -> list[dict[str, Any]]:
        """A ready dataset's records, opened: for a job or a proposed test, never a route."""
        raw, _ = self.dataset_bytes(org, dataset_id)
        return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]

    def proposals(self, org: str, digest: str) -> dict[tuple[str, str], str]:
        """What `org`'s authors decided of the proposed tests for version `digest`: (dataset,
        case) -> `accepted` or `dismissed`."""
        with self.store._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT dataset_id, case_id, state FROM store_proposals WHERE org = ? "
                "AND digest = ?",
                (_org(org), digest),
            )
        return {(str(r["dataset_id"]), str(r["case_id"])): str(r["state"]) for r in rows}

    def decide_proposal(
        self, org: str, digest: str, dataset_id: str, case_id: str, *, by: str, accept: bool
    ) -> dict[str, Any]:
        """Accept or dismiss one proposed test (a case of an improvement dataset) for a version:
        accepted ones run with its own tests and are shown apart. They stay beside it in the
        store, never in its files, so its digest never changes. Chained, naming the dataset,
        the case and the digest, never an input."""
        org, by = _org(org), _actor(by)
        self.version(org, digest)
        row = self._dataset_row(org, dataset_id)
        if row["kind"] != "improvement":
            raise StoreError("proposed tests come from an improvement dataset", 422)
        if case_id not in {str(r.get("id")) for r in self.dataset_records(org, dataset_id)}:
            raise StoreError("no such case in that dataset", 404)
        state, now = ("accepted" if accept else "dismissed"), _now()
        with self.store._db.session(exclusive=True) as db:
            db.all(
                "INSERT INTO store_proposals (id, org, digest, dataset_id, case_id, state, "
                "decided_by, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (org, digest, dataset_id, case_id) DO UPDATE SET "
                "state = excluded.state, decided_by = excluded.decided_by, "
                "decided_at = excluded.decided_at",
                (str(uuid.uuid4()), org, digest, dataset_id, case_id, state, by, now),
            )
            payload = {"dataset": dataset_id, "case": case_id, "digest": digest, "by": by}
            chained = self.store._bound(f"store_dataset.proposal_{state}", payload, org)
            if accept:
                self.store._append(
                    db, f"store_dataset:{dataset_id}", "store_dataset.proposal_accepted", chained
                )
            else:
                self.store._append(
                    db, f"store_dataset:{dataset_id}", "store_dataset.proposal_dismissed", chained
                )
        return {"dataset": dataset_id, "case": case_id, "digest": digest, "state": state}

    # -- held versions: the operator's (LP-20f) -------------------------------------------------

    def held_count(self, org: str) -> int:
        """How many of `org`'s versions are held for the operator's review."""
        with self.store._db.session(exclusive=False) as db:
            found = db.one(
                "SELECT COUNT(*) AS n FROM store_versions WHERE org = ? AND state = ?",
                (org, HELD),
            )
        return int(found["n"])

    def held(self) -> list[dict[str, Any]]:
        """Every match the hidden-set check recorded that the operator has not cleared, oldest
        first: the organization, the version's digest (or `dataset:<id>`) and state, and the set
        and case ids it matched. For the operator's command only: no route answers it."""
        with self.store._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT m.org, m.digest, m.matches, m.created_at, "
                "COALESCE(v.state, d.state) AS state FROM store_matches m "
                "LEFT JOIN store_versions v ON v.id = m.version_id "
                "LEFT JOIN store_datasets d ON d.id = m.version_id "
                "WHERE m.cleared_at IS NULL ORDER BY m.created_at, m.id"
            )
        return [
            {
                "org": row["org"],
                "digest": row["digest"],
                "state": row["state"],
                "matches": json.loads(row["matches"]),
                "held_at": row["created_at"],
            }
            for row in rows
        ]

    def clear(
        self, org: str, digest: str, *, by: str, release: bool, reason: str
    ) -> dict[str, Any]:
        """Resolve a held version or dataset as identified by the operator's held list."""
        if not digest.startswith("dataset:"):
            kind = "pack_version.released" if release else "pack_version.withdrawn"
            return self._move(org, digest, kind, (HELD,), by=by, reason=reason)

        org, by, reason = _org(org), _actor(by), _reason(reason)
        dataset_id = digest.removeprefix("dataset:")
        state = "ready" if release else WITHDRAWN
        with self.store._db.session(exclusive=True) as db:
            row = db.one(
                "SELECT state FROM store_datasets WHERE id = ? AND org = ?", (dataset_id, org)
            )
            if row is None:
                raise StoreError("no such dataset in this organization's store", 404)
            if row["state"] != HELD:
                raise StoreError(f"dataset {dataset_id} is {row['state']}", 409)
            db.all("UPDATE store_datasets SET state = ? WHERE id = ?", (state, dataset_id))
            _clear_matches(db, org, dataset_id, by)
            chain = f"store_dataset:{dataset_id}"
            kind = "store_dataset.released" if release else "store_dataset.withdrawn"
            payload = self.store._bound(
                kind, {"dataset": dataset_id, "by": by, "reason": reason}, org
            )
            if release:
                self.store._append(db, chain, "store_dataset.released", payload)
            else:
                self.store._append(db, chain, "store_dataset.withdrawn", payload)
        return {"org": org, "digest": digest, "state": state}

    def recheck(self, hidden: Any) -> list[dict[str, Any]]:
        """The hidden-set check again over every draft of every store (once hidden sets are
        frozen and their keys set, `mendari store recheck`): a draft that now matches is held,
        and its match recorded, in one transaction. A draft that cannot be read again is skipped
        and listed. The versions held, by organization and digest."""
        with self.store._db.session(exclusive=False) as db:
            drafts = db.all("SELECT org, digest FROM store_versions WHERE state = ?", (DRAFT,))
        held: list[dict[str, Any]] = []
        for row in drafts:
            org, digest = str(row["org"]), str(row["digest"])
            try:
                files = self.files(org, digest)
                with tempfile.TemporaryDirectory(prefix="mendari-store-") as scratch:
                    root = Path(scratch) / "pack"
                    _write(files, root)
                    cases = _cases_of(_load(root), files)
            except StoreError:
                held.append({"org": org, "digest": digest, "skipped": "cannot be read again"})
                continue
            matches = _hidden_matches(cases, hidden)
            if not matches:
                continue

            def matched(
                db: Any,
                version_id: str,
                org: str = org,
                digest: str = digest,
                matches: list[dict[str, str]] = matches,
            ) -> None:
                db.all(
                    "INSERT INTO store_matches (id, org, version_id, digest, matches, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), org, version_id, digest, _canonical(matches), _now()),
                )

            try:
                self._move(org, digest, "pack_version.held", (DRAFT,), by=SYSTEM, also=matched)
            except StoreError:  # no longer a draft since it was read
                continue
            held.append({"org": org, "digest": digest})
        return held

    # -- the operator's check -------------------------------------------------------------------

    def verify(self) -> dict[str, Any]:
        """Every kept file hashes to its key and has its size, and every version whose files are
        kept hashes to its digest (`mendari store verify`). Read only."""
        problems: list[str] = []
        with self.store._db.session(exclusive=False) as db:
            kept = {
                str(row["sha256"]): bytes(row["data"])
                for row in db.all("SELECT sha256, data FROM store_files")
            }
            sizes = {
                str(row["sha256"]): int(row["size"])
                for row in db.all("SELECT sha256, size FROM store_files")
            }
            versions = db.all("SELECT id, org, digest, files FROM store_versions")
        for sha, data in kept.items():
            if hashlib.sha256(data).hexdigest() != sha or len(data) != sizes[sha]:
                problems.append(f"file {sha}: its content does not hash to its key")
        checked = unkept = 0
        for row in versions:
            listed = json.loads(row["files"])
            if not all(item["sha256"] in kept for item in listed):
                unkept += 1
                continue
            with tempfile.TemporaryDirectory(prefix="mendari-store-") as scratch:
                root = Path(scratch) / "pack"
                _write({item["path"]: kept[item["sha256"]] for item in listed}, root)
                if pack_digest(root) != row["digest"]:
                    problems.append(f"version {row['id']}: its files do not hash to its digest")
            checked += 1
        return {
            "ok": not problems,
            "files": len(kept),
            "versions": checked,
            "freed": unkept,
            "problems": problems,
        }


def prerequisites(mode: str) -> list[str]:
    """What the store needs that is missing, each found by running it, not by reading
    (LP-20's prompt): memberships mode (LP-12); a store pack on the bounded engine, whose rules
    stop at the CPU budget (LP-16); the statistics and the ledger answering pinned values
    (LP-10); and the canonical forms answering their pinned vectors (LP-11). Empty when the
    store may open."""
    missing = []
    if mode != "memberships":
        missing.append(f"memberships mode (LP-12): sign-in is in {mode} mode")
    missing += _bounded_problems() + _statistics_problems() + _canonical_problems()
    return missing


def _bounded_problems() -> list[str]:
    from mendari.core.origin import NOT_OWNED, bounded
    from mendari.core.patterns import OutOfTime, check_time, time_budget

    found = []
    if "store" not in NOT_OWNED or not bounded("acme/x", "store", "sha256:" + "0" * 64):
        found.append("LP-16: a pack from a store does not run on the bounded engine")
    try:
        with time_budget(0.0):
            check_time()
        found.append("LP-16: a bounded pack's rules do not stop at the CPU budget")
    except OutOfTime:
        pass
    return found


def _statistics_problems() -> list[str]:
    from mendari.evals import stats
    from mendari.evals.ledger import canonical

    found = []
    low, high = stats.clopper_pearson(1, 2)
    if (round(low, 4), round(high, 4)) != (0.0126, 0.9874):
        found.append("LP-10: the exact interval does not answer its pinned value")
    if stats.mcnemar_exact(0, 5) != 0.0625:
        found.append("LP-10: the exact McNemar test does not answer its pinned value")
    if canonical({"b": 1, "a": [2]}) != '{"a":[2],"b":1}':
        found.append("LP-10: the ledger's canonical JSON does not answer its pinned value")
    return found


def _canonical_problems() -> list[str]:
    from mendari.evals.canonical import NORM_VERSION, canonical_input, normalised_input

    found = []
    if canonical_input({"t": "A", "a": 1}) != '{"a":1,"t":"A"}':
        found.append("LP-11: the strict canonical form does not answer its pinned vector")
    if NORM_VERSION != "norm/v1" or normalised_input({"t": "Chest  PAIN, since Monday!"}) != (
        '{"t":"chest pain since monday"}'
    ):
        found.append("LP-11: the norm/v1 form does not answer its pinned vector")
    return found
