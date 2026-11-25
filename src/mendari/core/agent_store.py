# SPDX-License-Identifier: Apache-2.0
"""An organization's agent store (LP-20e, ADR-0016): versions of agents kept in the engine's own
database, keyed by content digest.

- **A version never changes.** Its row's files, id, version and digest are written once
  (`audit.STORE_VERSION_COLUMNS`, hashed into its `store_version.created` event), and only its
  `state` moves, each move one chained event naming its organization: `draft`, `withdrawn`;
  LP-20h adds `promoted` and `retired`. An edit is a new version whose parent is the one it was
  made from. `AuditStore.verify` fails on a row changed past the store, a state no event moved it
  to, or a version with no event.
- **The engine follows the bytes** (ADR-0009). `put` writes the files to a scratch directory,
  loads them as origin `store` (on the bounded engine: RE2, the caps, the CPU budget) and keys
  the version by the digest `load_agent` hashed, never one it is told. Files the digest leaves out
  (`evals/`, a `DECISION_CARD.md`, hidden entries) are not kept, so an uploaded card or
  benchmark is never stored; the card is rendered here (`card`). Each version records what the
  one validation function (`evals/validation.py`) found.
- **Each file is kept once** (`store_files`, by SHA-256), whichever versions and organizations
  hold it; `purge` removes a file no version holds but one withdrawn more than
  `PURGE_AFTER_DAYS` ago. An organization keeps at most `settings.store_org_bytes()` of files
  and datasets, and adds at most its `store_uploads` quota of versions and its `store_datasets`
  quota of datasets a day (`MENDARI_ORG_DAILY_LIMITS`), each quota checked before the costly
  load and checks, and again in the write's own transaction.
- **`load` rebuilds from the database alone**: a serverless instance has no persistent disk,
  only a scratch one, so a version is written there on its first use in a process, checked
  against its digest, and kept in a cache bounded in count and bytes.

- **A version pulled from a registry is kept here too** (`admit_pulled`, source `hub`): what
  `hub/client.py` `fetch` verified as `mendari pull` verifies it, with the record of where it
  came from (`pulled`: the registry, the namespace, the signing key, the archive's SHA-256, what
  was checked), hashed into its `store_version.created` event. It keeps its publisher's id, loads
  as an agent from a registry (origin `hub`, on the same bounded engine), and passes every other
  check, limit and quota a version passes. It arrives a draft like any other: served only once
  someone who did not add it promotes it, and then held to what any agent from a registry is
  (its organization's approval of its digest, its registry's recalls). The keys it was verified
  with are pinned per organization in the database (`OrgPins`), not in a file.

Nothing here is reached by a route: LP-20f mounts them, behind `MENDARI_AGENT_STORE=on` and
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
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Final

from mendari.core import accounts, clock
from mendari.core.agent import MANIFEST, Agent, agent_digest, load_agent
from mendari.core.audit import _canonical, _now, store_version_sha256
from mendari.core.origin import Origin
from mendari.errors import HubError, MendariError

if TYPE_CHECKING:
    from mendari.core.audit import AuditStore
    from mendari.hub.client import Fetched

DRAFT, PROMOTED, RETIRED, WITHDRAWN = "draft", "promoted", "retired", "withdrawn"
STATES = (DRAFT, PROMOTED, RETIRED, WITHDRAWN)
# Where a version came from: an author's signed archive, a copy of an agent served here, an
# edit of another version, or a registry (`HUB`: verified as `mendari pull` verifies one).
HUB: Final = "hub"
SOURCES = ("upload", "copy", "edit", HUB)
RECALL_SEVERITIES = ("low", "medium", "high", "critical")
# A version's files: as many and as large as an archive may hold (`hub/archive.py`).
MAX_FILES = 2000
MAX_FILE_BYTES = 50 * 1024 * 1024
PURGE_AFTER_DAYS = 30  # a withdrawn version's files are freed after this (owner decision 14)
# The versions `load` keeps built in one process, and the bytes their files take on its disk.
CACHE_VERSIONS = 32
CACHE_BYTES = 256 * 1024 * 1024
MAX_REASON_CHARS = 500
MAX_KEYS = 20  # an organization's authors' keys that are not revoked
MAX_PENDING_UPLOADS = 3  # an organization's archives under way in parts at once
MAX_PARTS = 64
# Namespaces no store pack takes, whatever an organization is called.
STORE_RESERVED = ("mendari", "openhealth")


class StoreError(MendariError):
    """A store request refused: `status` is the HTTP status a route answers with (404 no such
    version of this organization, 409 a conflict with what is stored, 413 over a limit, 429
    over a quota, 503 a check that cannot run now, 422 otherwise)."""

    def __init__(self, message: str, status: int = 422):
        self.status = status
        super().__init__(message)


@dataclass(frozen=True)
class Stored:
    """A version as `put` wrote it."""

    id: str
    org: str
    digest: str
    agent_id: str
    version: str
    state: str
    valid: bool


def _kept(path: str) -> bool:
    """Whether an agent's file is part of what its digest covers: not an attestation about the
    agent (`evals/`, a `DECISION_CARD.md`), a hidden entry or a cache (`core/agent.py`)."""
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
        raise StoreError(f"{shown}: not a path inside an agent")


def _write(files: Mapping[str, bytes], root: Path) -> None:
    """Write an agent's files under `root`. A path that cannot be a file there (a file that is
    also another's directory, a name longer than the filesystem takes) is a `StoreError` naming
    it, as `archive.extract` refuses one, never an `OSError` a route answers with a 500."""
    root.mkdir(parents=True, exist_ok=True)
    base = root.resolve()
    for path, data in files.items():
        target = (base / path).resolve()
        if target == base or not target.is_relative_to(base):
            raise StoreError(f"{path}: not a path inside an agent")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except OSError as error:
            shown = path if path.isprintable() else ascii(path)
            raise StoreError(f"{shown}: cannot be written ({type(error).__name__})") from None


def _load(root: Path, origin: Origin = "store") -> Agent:
    """The agent at `root` as a store's (or, for a version pulled from a registry, as one from a
    registry: `origin` `hub`), on the bounded engine; a `StoreError` naming why not."""
    try:
        return load_agent(root, origin=origin)
    except MendariError as error:
        raise StoreError(f"the agent does not load: {error}") from None
    except Exception as error:  # someone else's files: named, never a 500
        raise StoreError(f"the agent does not load ({type(error).__name__})") from None


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
        "agent_id": found["agent_id"],
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
        # Where a version pulled from a registry came from (None for every other).
        "pulled": None if found.get("pulled") is None else json.loads(found["pulled"]),
    }


def _check_source(source: str, parent: str | None, pulled: Mapping[str, Any] | None = None) -> None:
    if source not in SOURCES:
        raise ValueError(f"source is one of {', '.join(SOURCES)}")
    if (source == "edit") != (parent is not None):
        raise ValueError("an edit, and only an edit, names its parent")
    if (source == HUB) != (pulled is not None):
        raise ValueError("a version from a registry, and only one, records where it came from")


@dataclass(frozen=True)
class Prepared:
    """An agent's files as the store keeps them (those its digest covers), loaded on the bounded
    engine, listed by path, hash and size; what the validation found; and, read while its files
    were on disk, its declared datasets' checks."""

    agent: Agent
    kept: dict[str, bytes]
    listed: list[dict[str, Any]]
    problems: list[dict[str, str]]
    datasets: list[dict[str, Any]]


def _prepare(files: Mapping[str, bytes], origin: Origin = "store") -> Prepared:
    for path in files:
        _check_path(path)
    kept = {path: bytes(data) for path, data in files.items() if _kept(path)}
    if not kept:
        raise StoreError(f"no file of an agent: an {MANIFEST} and the files it names")
    if len(kept) > MAX_FILES:
        raise StoreError(f"more than {MAX_FILES} files", 413)
    large = sorted(path for path, data in kept.items() if len(data) > MAX_FILE_BYTES)
    if large:
        raise StoreError(f"{large[0]}: larger than {MAX_FILE_BYTES} bytes", 413)
    with tempfile.TemporaryDirectory(prefix="mendari-store-") as scratch:
        root = Path(scratch) / "agent"
        _write(kept, root)
        agent = _load(root, origin)
        from mendari.evals.validation import problems

        found = [problem.to_json() for problem in problems(agent)]
        datasets = _dataset_reports(agent)
    listed: list[dict[str, Any]] = [
        {"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        for path, data in sorted(kept.items())
    ]
    return Prepared(agent, kept, listed, found, datasets)


def _dataset_reports(agent: Agent) -> list[dict[str, Any]]:
    """The dataset checks (`evals/dataset_checks.py`) of each dataset the agent declares that
    reads: ids and counts, never an input."""
    from mendari.evals import dataset_checks
    from mendari.evals.harness import resolve_dataset

    reports = []
    for name in agent.manifest.evaluation.datasets:
        try:
            dataset = resolve_dataset(agent, name)
        except MendariError:
            continue
        reports.append(dataset_checks.check(agent, dataset).data)
    return reports


def _reserved(namespace: str) -> bool:
    """A namespace no store agent takes: each of `STORE_RESERVED`, and any name that starts with
    one and a dash, as the registry reserves them (`hub/registry.py`)."""
    return any(namespace == held or namespace.startswith(f"{held}-") for held in STORE_RESERVED)


# Each daily store quota (`MENDARI_ORG_DAILY_LIMITS`): its `store_usage` column, and what it
# counts, as a refusal names it.
_DAILY = {"store_uploads": ("uploads", "versions"), "store_datasets": ("datasets", "datasets")}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def _promotion_shown(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "agent_id": row["agent_id"],
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


def _dataset_shown(row: Mapping[str, Any]) -> dict[str, Any]:
    shown = {
        "id": row["id"],
        "name": row["name"],
        "state": row["state"],
        "created_at": row["created_at"],
    }
    return {
        **shown,
        "kind": row["kind"],
        "agent_id": row["agent_id"],
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


class AgentStore:
    """The agent stores of every organization, over an `AuditStore` (its database and chain)."""

    def __init__(self, store: AuditStore, *, scratch: Path | None = None) -> None:
        self.store = store
        # This process's scratch directory for loaded versions (`load`), made on first use.
        self._scratch = scratch
        self._lock = threading.Lock()
        self._building: dict[str, threading.Lock] = {}
        self._cache: OrderedDict[str, tuple[Agent, int]] = OrderedDict()

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
        """A new draft version of `org` from an agent's files (path under its root -> content):
        loaded as origin `store`, keyed by the digest that load hashed, validated, chained
        `store_version.created`. Files outside the digest are dropped first. Refused (a
        `StoreError`, nothing written) when the agent does not load, `org` holds this digest
        already, `parent` is not one of `org`'s versions it may build on, or a limit or quota is
        passed. The checks of an upload (PHI, namespace) are `admit`'s."""
        org, by = _org(org), _actor(by)
        _check_source(source, parent)
        self._refuse_over_quota(org, "store_uploads")
        ready = _prepare(files)
        return self._insert(
            org,
            ready.agent,
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
        claimed: Callable[[str, str], bool] | None = None,
        expect_digest: str | None = None,
        expect_version: tuple[str, str] | None = None,
        pulled: Mapping[str, Any] | None = None,
    ) -> Stored:
        """A new version of `org` that passed every check an upload, a copy or an edit passes,
        in this order, before any row is written (LP-20f): no file reads as a person's details
        (`core/phi_detect.py`; `PhiRejected`, with the report: kinds, files and lines, never a
        value); the agent loads on the bounded engine; its namespace is `org`'s
        (`claimed(org, namespace)` may allow one more, an approved registry claim) and never a
        reserved one; it is the agent `expect_digest` names, if one does; then the validation
        and dataset checks, recorded with it.

        A version from a registry (`source` `hub`, with `pulled`: where it came from, which
        `admit_pulled` gives) passes the same checks with two differences: it loads as an agent
        from a registry, and it keeps its publisher's id, whatever the organization is called.
        `expect_version` is the id and version it must have (the ones its signed manifest and
        its registry name)."""
        from mendari.core import phi_detect

        org, by = _org(org), _actor(by)
        _check_source(source, parent, pulled)
        self._refuse_over_quota(org, "store_uploads")
        for path in files:
            _check_path(path)
        found = phi_detect.detect_files({p: d for p, d in files.items() if _kept(p)})
        if phi_detect.rejects(found):
            raise PhiRejected(found.to_json())
        ready = _prepare(files, HUB if source == HUB else "store")
        agent = ready.agent
        namespace = agent.id.partition("/")[0]
        if source != HUB and (
            _reserved(namespace)
            or (namespace != org and not (claimed is not None and claimed(org, namespace)))
        ):
            raise StoreError(
                f"an agent in this organization's store is named {org}/<name>: {agent.id!r} is "
                "not (a copy is renamed on the way in)"
            )
        if expect_digest is not None and agent.digest != expect_digest:
            raise StoreError("the archive's files are not the agent its manifest names")
        if expect_version is not None and (agent.id, agent.version) != expect_version:
            raise StoreError("the archive's files are not the agent and version it names")
        checks: dict[str, Any] = {
            "phi": {"files": found.files, "findings": len(found.findings)},
            "datasets": ready.datasets,
        }
        return self._insert(
            org,
            agent,
            ready.kept,
            ready.listed,
            ready.problems,
            by=by,
            source=source,
            parent=parent,
            copied_from=copied_from,
            checks=checks,
            pulled=pulled,
        )

    def admit_pulled(
        self,
        org: str,
        fetched: Fetched,
        *,
        by: str,
        served_ids: Iterable[str] = (),
    ) -> Stored:
        """A version `hub/client.py` `fetch` verified (its archive's SHA-256 against the
        registry's record, its signature by a key of its namespace, the registry's
        counter-signature where it signs), kept as a draft of `org` (source `hub`) with the
        record of where it came from, after every check `admit` makes. Refused, with nothing
        written, when:

        - its registry recalled it (a recall only ever stops an agent: one is never added to
          start it), or the registry's record and the signed manifest name different digests;
        - this deployment serves that agent's id from its own files (a store version never
          replaces one, so it could never be promoted);
        - it holds a site's cases (`archive.refuse_site_cases`), as a registry refuses one;
        - or any check of `admit` fails: files that read as a person's details, an agent that
          does not load on the engine it would run on, a digest, id or version that is not the
          signed one, a limit or a quota passed."""
        from mendari.hub import archive

        recall = fetched.meta.get("yank")
        if isinstance(recall, Mapping):
            stated = recall.get("severity")
            severity = stated if stated in RECALL_SEVERITIES else "unknown"
            raise StoreError(
                f"{fetched.agent_id}@{fetched.version} was recalled by its registry (severity: "
                f"{severity}): a recalled version is not added",
                409,
            )
        signed = fetched.verified.manifest.get("digest")
        if not isinstance(signed, str) or signed != fetched.meta.get("digest"):
            raise StoreError("the archive is not the build the registry recorded")
        if fetched.agent_id in set(served_ids):
            raise StoreError(
                f"this deployment serves {fetched.agent_id} from its own files: a version from "
                "a registry never replaces it",
                409,
            )
        try:
            archive.refuse_site_cases(fetched.verified.files)
        except HubError as error:
            raise StoreError(str(error)) from None
        return self.admit(
            org,
            fetched.verified.files,
            by=by,
            source=HUB,
            expect_digest=signed,
            expect_version=(fetched.agent_id, fetched.version),
            pulled=fetched.origin,
        )

    def refuse_before_adding(self, org: str) -> None:
        """Refused (a `StoreError`, 429) before a version is downloaded for `org`: it has added
        what its `store_uploads` quota allows today. `admit` checks it again."""
        org = _org(org)
        self._refuse_over_quota(org, "store_uploads")

    def _refuse_over_quota(self, org: str, metric: str, db: Any = None) -> None:
        """A `StoreError` (429) when `org` has added today what its `metric` quota allows
        (`store_uploads` versions, `store_datasets` datasets; `MENDARI_ORG_DAILY_LIMITS`, none
        unset); read in `db` when given. Checked before the costly load and checks, and again
        in the write's transaction, where parallel writes count too."""
        from mendari import settings

        quota = settings.org_daily_limits().get(metric)
        if quota is None:
            return
        column, what = _DAILY[metric]
        query = f"SELECT {column} AS n FROM store_usage WHERE org = ? AND day = ?"
        params = (org, clock.today().isoformat())
        if db is None:
            with self.store._db.session(exclusive=False) as session:
                used = session.one(query, params)
        else:
            used = db.one(query, params)
        if used is not None and int(used["n"] or 0) >= quota:
            raise StoreError(f"this organization has added its {quota} {what} for today", 429)

    def refuse_over_dataset_quota(self, org: str, adding: int = 0) -> None:
        """Refused (`StoreError`) before a dataset is read, parsed or built: 429 when `org` has
        added its `store_datasets` datasets today, 413 when `adding` bytes (what is known of its
        size before it is sealed, which only adds to it) would pass `MENDARI_STORE_ORG_BYTES`.
        `add_dataset` checks both again with the size as kept, in its write."""
        org = _org(org)
        self._refuse_over_quota(org, "store_datasets")
        if adding:
            with self.store._db.session(exclusive=False) as db:
                self._refuse_over_bytes(db, org, adding, self._org_shas(db, org))

    def _refuse_over_bytes(self, db: Any, org: str, adding: int, held: set[str]) -> None:
        """A `StoreError` (413) when `org`'s store would keep more than
        `settings.store_org_bytes()` with `adding` more bytes: the files its versions hold
        (`held`, by SHA-256, each counted once) and its datasets as kept."""
        from mendari import settings

        allowed = settings.store_org_bytes()
        files = sum(
            int(row["size"])
            for row in db.all("SELECT sha256, size FROM store_files")
            if str(row["sha256"]) in held
        )
        datasets = db.one(
            "SELECT COALESCE(SUM(COALESCE(size, LENGTH(data))), 0) AS n FROM store_datasets "
            "WHERE org = ?",
            (org,),
        )
        if files + int(datasets["n"] if datasets else 0) + adding > allowed:
            raise StoreError(
                f"this organization's store would hold more than {allowed} bytes of files and "
                "datasets (MENDARI_STORE_ORG_BYTES)",
                413,
            )

    def _insert(
        self,
        org: str,
        agent: Agent,
        kept: Mapping[str, bytes],
        listed: list[dict[str, Any]],
        validation: list[dict[str, str]],
        *,
        by: str,
        source: str,
        parent: str | None,
        copied_from: Mapping[str, str] | None,
        checks: Mapping[str, Any] | None = None,
        pulled: Mapping[str, Any] | None = None,
    ) -> Stored:
        by_sha = {item["sha256"]: kept[item["path"]] for item in listed}
        day = clock.today().isoformat()
        with self.store._db.session(exclusive=True) as db:
            if db.one(
                "SELECT id FROM store_versions WHERE org = ? AND digest = ?", (org, agent.digest)
            ):
                raise StoreError(
                    f"this organization's store holds {agent.digest} already: a version never "
                    "changes",
                    409,
                )
            if parent is not None:
                above = db.one(
                    "SELECT state FROM store_versions WHERE org = ? AND digest = ?", (org, parent)
                )
                if above is None:
                    raise StoreError("no such version in this organization's store", 404)
                if above["state"] == WITHDRAWN:
                    raise StoreError(f"version {parent} is {above['state']}: nothing builds on it")
            self._refuse_over_quota(org, "store_uploads", db)
            held = self._org_shas(db, org)
            new = {sha: len(data) for sha, data in by_sha.items() if sha not in held}
            self._refuse_over_bytes(db, org, sum(new.values()), held)
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
                "digest": agent.digest,
                "agent_id": agent.id,
                "version": agent.version,
                "files": _canonical(listed),
                "parent_digest": parent,
                "source": source,
                "copied_from": None if copied_from is None else _canonical(dict(copied_from)),
                "validation": _canonical(validation),
                "checks": None if checks is None else _canonical(dict(checks)),
                "created_by": by,
                "created_at": now,
            }
            if pulled is not None:  # a version from a registry: where it came from, hashed too
                row["pulled"] = _canonical(dict(pulled))
            state = DRAFT
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
            payload: dict[str, Any] = {
                "digest": agent.digest,
                "agent": agent.id,
                "version": agent.version,
                "source": source,
                "parent": parent,
                "by": by,
                "valid": not validation,
                "version_sha256": store_version_sha256(row),
            }
            if pulled is not None:  # which registry, signing key and archive it was verified as
                payload["pulled"] = {
                    name: pulled.get(name)
                    for name in ("registry", "namespace", "key_id", "archive_sha256", "accepted")
                }
            chained = self.store._bound("store_version.created", payload, org)
            self.store._append(db, f"store_version:{version_id}", "store_version.created", chained)
        return Stored(version_id, org, agent.digest, agent.id, agent.version, state, not validation)

    def _org_shas(self, db: Any, org: str) -> set[str]:
        held: set[str] = set()
        for row in db.all("SELECT files FROM store_versions WHERE org = ?", (org,)):
            held.update(item["sha256"] for item in json.loads(row["files"]))
        return held

    def withdraw(
        self, org: str, digest: str, *, by: str, reason: str | None = None
    ) -> dict[str, Any]:
        """Withdraw a draft, one chained event: its row and events stay, its files are freed
        `PURGE_AFTER_DAYS` later (`purge`)."""
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
                raise StoreError("no such version in this organization's store", 404)
            if found["state"] != DRAFT:
                raise StoreError(f"version {digest} is {found['state']}", 409)
            now = _now()
            db.all(
                "UPDATE store_versions SET state = ?, state_at = ? WHERE id = ?",
                (WITHDRAWN, now, found["id"]),
            )
            payload: dict[str, Any] = {"digest": digest, "by": by}
            if reason is not None:
                payload["reason"] = reason
            chained = self.store._bound("store_version.withdrawn", payload, org)
            self.store._append(
                db, f"store_version:{found['id']}", "store_version.withdrawn", chained
            )
        self._forget(digest)
        return {"org": org, "digest": digest, "state": WITHDRAWN, "state_at": now}

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

    def versions(self, org: str, agent_id: str | None = None) -> list[dict[str, Any]]:
        """`org`'s versions (of `agent_id`, if named), newest first; never another
        organization's."""
        org = _org(org)
        where, params = "org = ?", [org]
        if agent_id is not None:
            where, params = f"{where} AND agent_id = ?", [org, agent_id]
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

    def load(self, org: str, digest: str) -> Agent:
        """One of `org`'s versions as an `Agent` of origin `store`: written from the database to
        this process's scratch directory on its first use, its digest checked, and kept in a
        cache bounded in count and bytes. A request that asks while it is built waits for that
        build and never starts a second. A version pulled from a registry is an `Agent` of origin
        `hub` that carries where it came from (`Agent.origin_record`, the database's): built and
        kept for its organization alone, since that record is its own."""
        row = self.version(org, digest)  # `org` holds it, whoever else does
        pulled = row["pulled"] if row["source"] == HUB else None
        key = digest if pulled is None else f"{digest}@{org}"
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached[0].root.is_dir():
                self._cache.move_to_end(key)
                return cached[0]
            building = self._building.setdefault(key, threading.Lock())
        with building:
            with self._lock:
                cached = self._cache.get(key)
                if cached is not None and cached[0].root.is_dir():
                    return cached[0]
            root = self._scratch_dir() / digest.replace(":", "-") / uuid.uuid4().hex
            try:
                files = self.files(org, digest)
                _write(files, root)
                agent = _load(root, "store" if pulled is None else HUB)
                if agent.digest != digest:
                    raise StoreError(f"version {digest}'s files hash to {agent.digest}", 500)
                if pulled is not None:
                    agent = replace(agent, origin_record=dict(pulled))
            except BaseException:
                shutil.rmtree(root, ignore_errors=True)
                with self._lock:
                    self._building.pop(key, None)
                raise
            size = sum(len(data) for data in files.values())
            with self._lock:
                self._cache[key] = (agent, size)
                self._building.pop(key, None)
                self._evict()
        return agent

    def card(self, org: str, digest: str) -> str:
        """The version's Decision Card, rendered here from its files (`card.card_of`): nothing
        uploaded about it is shown."""
        from mendari.core.card import card_of

        return card_of(self.load(org, digest))

    def _scratch_dir(self) -> Path:
        with self._lock:
            if self._scratch is None:
                self._scratch = Path(tempfile.mkdtemp(prefix="mendari-store-"))
            scratch = self._scratch
        scratch.mkdir(parents=True, exist_ok=True)
        return scratch

    def _evict(self) -> None:
        """Called under the lock: drop the least recently used versions past the bounds,
        never evicting the sole entry just inserted."""
        while len(self._cache) > 1 and (
            len(self._cache) > CACHE_VERSIONS
            or sum(size for _, size in self._cache.values()) > CACHE_BYTES
        ):
            _, (agent, _) = self._cache.popitem(last=False)
            shutil.rmtree(agent.root, ignore_errors=True)

    def _forget(self, digest: str) -> None:
        """Drop a version's built copies: the one every organization that holds it shares, and
        each organization's own of one pulled from a registry (`load`)."""
        with self._lock:
            keys = [key for key in self._cache if key == digest or key.startswith(f"{digest}@")]
            dropped = [self._cache.pop(key) for key in keys]
        for agent, _ in dropped:
            shutil.rmtree(agent.root, ignore_errors=True)

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
        kind: str = "upload",
    ) -> dict[str, Any]:
        """One of `org`'s datasets (JSON Lines), checked exactly as a version is: the PHI
        detector (`PhiRejected`) and the dataset checks against version `digest` (kept and
        shown; a schema or label problem refuses it). Kept sealed under the PHI policy when the
        mode is `encrypt`. It counts against `org`'s quotas as a version does: its `store_datasets`
        quota a day (429), checked first, and its bytes as kept with the files its versions
        hold (`MENDARI_STORE_ORG_BYTES`, 413). Chained `store_dataset.created`, with counts
        only."""
        from mendari.core import phi_detect
        from mendari.evals import dataset_checks
        from mendari.evals.harness import load_dataset

        org, by = _org(org), _actor(by)
        if not NAME_RE.fullmatch(name):
            raise StoreError("a dataset's name is lower-case letters, digits and hyphens")
        if kind not in ("upload", "improvement"):
            raise ValueError("a dataset is an upload or an improvement dataset")
        self.refuse_over_dataset_quota(org, len(raw))  # before the detector, load and checks
        # An improvement dataset (LP-20i) is the organization's own reviewed decisions: patients'
        # inputs by construction, so the PHI detector is not for it; it is always sealed where
        # the policy seals.
        if kind == "upload":
            found = phi_detect.detect_files({f"{name}.jsonl": raw})
            if phi_detect.rejects(found):
                raise PhiRejected(found.to_json())
        elif self.store.phi.mode == "hash":
            raise StoreError(
                "PHI mode hash keeps no patient text: switch to encrypt mode to store "
                "improvement datasets",
                409,
            )
        if self.store.phi.mode == "off":
            self.store.refuse_plaintext()
        row = self.version(org, digest)
        if row["state"] == WITHDRAWN:
            raise StoreError(f"version {digest} is {row['state']}", 409)
        agent = self.load(org, digest)
        try:
            data = load_dataset(f"{name}.jsonl", name=name, raw=raw, label=f"{name}.jsonl")
        except MendariError as error:
            raise StoreError(str(error)) from None
        report = dataset_checks.check(agent, data, training={})
        if not report.passed:
            raise StoreError("the dataset does not fit the agent: schema or label problems", 422)
        dataset_id, now = str(uuid.uuid4()), _now()
        state = "ready"
        text = raw.decode("utf-8")
        kept = (
            self.store.phi.seal("dataset", text, dataset_id)
            if self.store.phi.mode == "encrypt"
            else text
        )
        size = len(kept.encode("utf-8"))
        with self.store._db.session(exclusive=True) as db:
            if self.store.phi.mode == "off":
                self.store._refuse_plaintext(db)
            if self.store.phi.mode == "encrypt":
                self.store._refuse_retired(db)
            self._refuse_over_quota(org, "store_datasets", db)
            self._refuse_over_bytes(db, org, size, self._org_shas(db, org))
            db.all(
                "INSERT INTO store_datasets (id, org, name, agent_id, digest, sha256, cases, "
                "data, sealed, report, created_by, created_at, state, kind, size) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (dataset_id, org, name, agent.id, digest, data.sha256, len(data.records), kept,
                 int(self.store.phi.mode == "encrypt"), _canonical(report.data), by, now, state,
                 kind, size),
            )  # fmt: skip
            db.all(
                "INSERT INTO store_usage (org, day, uploads, bytes, datasets) "
                "VALUES (?, ?, 0, ?, 1) ON CONFLICT (org, day) DO UPDATE SET "
                "datasets = COALESCE(store_usage.datasets, 0) + 1, "
                "bytes = store_usage.bytes + excluded.bytes",
                (org, clock.today().isoformat(), size),
            )
            payload = {"dataset": dataset_id, "sha256": data.sha256, "kind": kind,
                       "cases": len(data.records), "by": by}  # fmt: skip
            chained = self.store._bound("store_dataset.created", payload, org)
            self.store._append(db, f"store_dataset:{dataset_id}", "store_dataset.created", chained)
        return self.dataset(org, dataset_id)

    def _dataset_row(self, org: str, dataset_id: str) -> Any:
        org = _org(org)
        with self.store._db.session(exclusive=False) as db:
            found = db.one(
                "SELECT * FROM store_datasets WHERE id = ? AND org = ?", (dataset_id, org)
            )
        if found is None:
            raise StoreError("no such dataset in this organization's store", 404)
        return found

    def dataset(self, org: str, dataset_id: str) -> dict[str, Any]:
        """One of `org`'s datasets as its uploader may see it: its name, SHA-256, how many cases,
        the version it was checked against and its check report (ids and counts). Never a
        case."""
        return _dataset_shown(self._dataset_row(org, dataset_id))

    def datasets(self, org: str) -> list[dict[str, Any]]:
        org = _org(org)
        with self.store._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT * FROM store_datasets WHERE org = ? ORDER BY created_at DESC, id", (org,)
            )
        return [_dataset_shown(row) for row in rows]

    def dataset_bytes(self, org: str, dataset_id: str) -> tuple[bytes, str]:
        """A ready dataset's JSON Lines and its name, opened under the PHI policy: for a job
        (`evals/store_jobs.py`), never a route."""
        row = self._dataset_row(org, dataset_id)
        if row["state"] != "ready":
            raise StoreError(f"dataset {dataset_id} is {row['state']}", 409)
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
        """Sign a version off to be served to `org` (LP-20h), chained `store_version.promoted`
        with the digest, its authors, the approver and the reports relied on; the promoted
        version of the same agent before it is retired in the same transaction. Refused when
        the approver made any version on its chain, no report is named or a named report is not
        `org`'s evaluation of exactly this digest, the version is not a validated draft
        (withdrawn or with validation problems), the deployment serves its id from disk, or an
        agent of risk level 4 or above is promoted to automate (it never does, ADR-0002)."""
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
            raise StoreError(f"version {digest} is {row['state']}", 409)
        if row["validation"]:
            raise StoreError("a version that does not validate is not promoted", 409)
        if row["agent_id"] in set(served_ids):
            raise StoreError(
                f"this deployment serves {row['agent_id']} from disk: a store version never "
                "replaces it",
                409,
            )
        agent = self.load(org, digest)
        if automate and int(agent.manifest.risk.level) >= 4:
            raise StoreError("an agent of risk level 4 or above never automates (ADR-0002)")
        with self.store._db.session(exclusive=True) as db:
            for report_id in reports:
                found = db.one(
                    "SELECT report FROM eval_reports WHERE id = ? AND org = ?", (report_id, org)
                )
                named = None if found is None else json.loads(found["report"]).get("agent", {})
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
                "SELECT id, version_id, digest FROM store_promotions WHERE org = ? AND agent_id = "
                "? "
                "AND retired_at IS NULL",
                (org, row["agent_id"]),
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
                chained = self.store._bound("store_version.retired", payload, org)
                self.store._append(
                    db, f"store_version:{before['version_id']}", "store_version.retired", chained
                )
            db.all(
                "INSERT INTO store_promotions (id, org, agent_id, digest, version, version_id, "
                "authors, approver, reports, reference, note, automate, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (promotion_id, org, row["agent_id"], digest, row["version"], row["id"],
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
            chained = self.store._bound("store_version.promoted", payload, org)
            self.store._append(db, f"store_version:{row['id']}", "store_version.promoted", chained)
        if before is not None:
            self._forget(str(before["digest"]))
        return self.promotion(org, promotion_id)

    def retire(self, org: str, promotion_id: str, *, by: str) -> dict[str, Any]:
        """Withdraw a promotion: its version is `retired`, and the organization serves no version
        of that agent until another is promoted. Chained `store_version.retired`."""
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
            chained = self.store._bound("store_version.retired", payload, org)
            self.store._append(
                db, f"store_version:{found['version_id']}", "store_version.retired", chained
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
        """What `org` is served from its store: agent id -> (digest, promotion id, whether it was
        promoted to automate), the promotions in force. One indexed read."""
        with self.store._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT agent_id, digest, id, automate FROM store_promotions WHERE org = ? "
                "AND retired_at IS NULL",
                (org,),
            )
        return {
            str(r["agent_id"]): (str(r["digest"]), str(r["id"]), bool(int(r["automate"])))
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
                root = Path(scratch) / "agent"
                _write({item["path"]: kept[item["sha256"]] for item in listed}, root)
                if agent_digest(root) != row["digest"]:
                    problems.append(f"version {row['id']}: its files do not hash to its digest")
            checked += 1
        return {
            "ok": not problems,
            "files": len(kept),
            "versions": checked,
            "freed": unkept,
            "problems": problems,
        }


class OrgPins:
    """The registry keys one organization pinned (`hub/client.py` `Pins`), in the engine's
    database (`store_pins`): a namespace's key ids and each registry's own key id, pinned on
    first use when its organization first verified an agent from that registry, and from then on
    what a changed key is refused against. Per organization: one organization accepting a
    publisher's new key changes nothing for another. Outside the chain, as the command line's
    pins are in a file; the version each was pinned for names its key in its own event."""

    where = "this organization's pinned keys (the deployment's database, table store_pins)"

    def __init__(self, store: AuditStore, org: str, by: str) -> None:
        self.store, self.org, self.by = store, _org(org), _actor(by)

    def read(self) -> dict[str, Any]:
        with self.store._db.session(exclusive=False) as db:
            rows = db.all("SELECT registry, name, value FROM store_pins WHERE org = ?", (self.org,))
        pins: dict[str, Any] = {}
        for row in rows:
            pins.setdefault(str(row["registry"]), {})[str(row["name"])] = json.loads(row["value"])
        return pins

    def write(self, pins: Mapping[str, Any]) -> None:
        held = self.read()
        changed = [
            (str(registry), str(name), _canonical(value))
            for registry, names in pins.items()
            for name, value in dict(names).items()
            if held.get(registry, {}).get(name) != value
        ]
        if not changed:
            return
        now = _now()
        with self.store._db.session(exclusive=True) as db:
            for registry, name, value in changed:
                db.all(
                    "INSERT INTO store_pins (org, registry, name, value, pinned_by, pinned_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (org, registry, name) DO UPDATE SET "
                    "value = excluded.value, pinned_by = excluded.pinned_by, "
                    "pinned_at = excluded.pinned_at",
                    (self.org, registry, name, value, self.by, now),
                )


def prerequisites(mode: str) -> list[str]:
    """What the store needs that is missing, each found by running it, not by reading
    (LP-20's prompt): memberships mode (LP-12); a store agent on the bounded engine, whose rules
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
    if "store" not in NOT_OWNED or not bounded("store", "sha256:" + "0" * 64):
        found.append("LP-16: an agent from a store does not run on the bounded engine")
    try:
        with time_budget(0.0):
            check_time()
        found.append("LP-16: a bounded agent's rules do not stop at the CPU budget")
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
