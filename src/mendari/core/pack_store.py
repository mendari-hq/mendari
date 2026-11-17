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
import shutil
import tempfile
import threading
import uuid
from collections import OrderedDict
from collections.abc import Mapping
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
        "created_by": found["created_by"],
        "created_at": found["created_at"],
        "state": found["state"],
        "state_at": found["state_at"],
    }


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
        passed."""
        org, by = _org(org), _actor(by)
        if source not in SOURCES:
            raise ValueError(f"source is one of {', '.join(SOURCES)}")
        if (source == "edit") != (parent is not None):
            raise ValueError("an edit, and only an edit, names its parent")
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
        listed: list[dict[str, Any]] = [
            {"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
            for path, data in sorted(kept.items())
        ]
        return self._insert(
            org,
            pack,
            kept,
            listed,
            found,
            by=by,
            source=source,
            parent=parent,
            copied_from=copied_from,
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
            if parent is not None:
                above = db.one(
                    "SELECT state FROM store_versions WHERE org = ? AND digest = ?", (org, parent)
                )
                if above is None:
                    raise StoreError(f"no version {parent} in this organization's store", 404)
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
                "created_by": by,
                "created_at": now,
            }
            db.all(
                f"INSERT INTO store_versions ({', '.join(row)}, state, state_at) "
                f"VALUES ({', '.join('?' for _ in row)}, ?, ?)",
                (*row.values(), DRAFT, now),
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
        return Stored(version_id, org, pack.digest, pack.id, pack.version, DRAFT, not validation)

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
    ) -> dict[str, Any]:
        org, by = _org(org), _actor(by)
        if reason is not None:
            reason = " ".join(reason.split())
            if not reason or len(reason) > MAX_REASON_CHARS:
                raise StoreError(f"a reason is one line of 1 to {MAX_REASON_CHARS} characters")
        with self.store._db.session(exclusive=True) as db:
            found = db.one(
                "SELECT id, state FROM store_versions WHERE org = ? AND digest = ?", (org, digest)
            )
            if found is None:
                raise StoreError(f"no version {digest} in this organization's store", 404)
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
        self, org: str, digest: str, *, by: str, reason: str | None = None
    ) -> dict[str, Any]:
        """Withdraw a draft or a held version: its row and events stay, and its files are freed
        `PURGE_AFTER_DAYS` later (`purge`)."""
        return self._move(
            org, digest, "pack_version.withdrawn", (DRAFT, HELD), by=by, reason=reason
        )

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
            raise StoreError(f"no version {digest} in this organization's store", 404)
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
            files = self.files(org, digest)
            root = self._scratch_dir() / digest.replace(":", "-") / uuid.uuid4().hex
            _write(files, root)
            pack = _load(root)
            if pack.digest != digest:
                shutil.rmtree(root, ignore_errors=True)
                raise StoreError(f"version {digest}'s files hash to {pack.digest}", 500)
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
