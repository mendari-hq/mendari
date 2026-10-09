# SPDX-License-Identifier: Apache-2.0
"""Hidden evaluation sets, stored (LP-11e, ADR-0011): sets and their hand-written cases, sealed in
the deployment's own database, in the `evalset_` tables of `core/audit.py`, and nowhere else.

- A set (`create_set`) belongs to one organization and one pack. Its header (the pack's id, the
  set's name, the pack's member and derived questions with their labels, and its critical labels)
  is sealed; its state starts `draft`. Chained: `evalset.created {set, org}`.
- A case (`add_cases`) is a dataset record (`{id, input, labels, attributes}`) written by hand:
  `attributes.source` is `hand_written` (de-identified cases wait for B20), `attributes.template`
  is null, and `author`, `language` and `stratum` are given. A record holding what a model
  decided or people rated (a `system` or `feedback` block, as `mendari feedback export` writes)
  is refused, and so is an input equal, exactly or under `norm/v1`, to one of the public cases:
  the reference packs' data and `datasets/` as this release bundles them, and each generator's
  output at its default seed. The store mints each case's id (a UUID4); the author's id and an
  intended label, if any, are kept among its attributes. Its input is sealed, and its attributes
  sealed with a 32-byte salt; its keyed case hash and its two index hashes (`index_hashes`) are
  kept beside them. Chained, one per case in one exclusive append: `evalset.case_added {set, org,
  case: <case hash>, case_ref: <commitment>}`, where the commitment is the SHA-256 of
  `CASE_REF_PREFIX`, the salt and the canonical `{set, case, input, attributes}` (`case_ref`),
  so `mendari audit verify --evalsets` checks each sealed row against it and the chain says
  nothing of the case.
- A draft's case can be removed (`remove_case`), with its index rows: `evalset.case_removed`.
- The hidden-case index (`hidden_matches`) answers, across every organization, which cases have
  an input equal to a value under either form, for the pack store's upload check (LP-20); it is
  read here only, never by a route or a command.

Every write first checks the keys against the database (`AuditStore._evalset_ready`) in its own
transaction, and the first index row under a new index key starts its epoch
(`evalset_index.epoch`, a deployment event). Messages name ids, line numbers and JSON paths,
never a value. PHI retention, erasure and key rotation never touch these rows.

The keys' lifecycle (LP-11f), on the sealed columns `SEALED_COLUMNS` lists:
- `rotate` re-seals under the current key what another key sealed, in batches, as the PHI key's
  rotation does (`AuditStore.rotate_phi`): each batch chains `evalset.rotated` (key ids and
  counts, a deployment event under `KEYS_CHAIN`), and the keys it moved values off are retired.
- `check` proves, reading only, that everything opens with the current key alone, recomputes
  each case's commitment and checks the index rows of the current index key.
- `reindex` rebuilds the whole hidden-case index under the current index key in one
  transaction (`evalset_index.rebuilt`) and retires the earlier index keys, which index nothing
  more here; chained case hashes keep the key id they were made under, and the commitments
  carry the binding across index keys.
- `export` copies the sealed rows as they are stored, ciphertext and ids, opening nothing.
Each prints ids, case hashes, key ids and counts only, and only `rotate` and `reindex` chain."""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import re
import secrets
import uuid
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

from mendari.core import accounts
from mendari.core.audit import (
    CHECK_FAILURES_SHOWN,
    EVALSET_INDEX_EPOCH,
    EVALSET_INDEX_RETIRED,
    EVALSET_RETIRED,
    EVALSET_TARGET,
    KEYS_CHAIN,
    ROTATE_BATCH,
    AuditStore,
    _mark,
    _now,
)
from mendari.core.db import Session
from mendari.core.evalset_keys import INDEX_ENV, KEY_ENV, READ_ENV, EvalsetKeys, Kind
from mendari.core.pack import Pack, not_json
from mendari.core.phi import key_id, token_key
from mendari.errors import MendariError
from mendari.evals.canonical import canonical_input, normalised_input

# A case's commitment (`case_ref`): unkeyed, hiding through its salt (kept only sealed in the
# case's attributes) and binding through SHA-256. No prefix of another of the deployment's
# hashes, keyed or not, nor of a sealed value's associated data.
CASE_REF_PREFIX = b"mendari-evalset-case-ref-v1\x00"
DRAFT = "draft"
# What a set's state may be: `draft` until LP-11i freezes it, then `frozen`, `active`, `retired`.
STATES = (DRAFT, "frozen", "active", "retired")
SOURCE = "hand_written"  # the only source until B20 brings de-identified cases
REQUIRED = ("author", "language", "stratum")
# The attributes the store adds to a case's own: the id its author gave it, its intended labels.
ADDED = ("author_case_id", "intended")
RECORD_KEYS = frozenset({"id", "input", "labels", "attributes"})
# Blocks a feedback export writes (`evals/feedback.py`): what the system decided and the ratings.
REFUSED_BLOCKS = ("system", "feedback")
NAME_CHARS = 200
_INSERT_BATCH = 200  # rows per insert
_CASE_HASH = re.compile(r"^hmac-sha256:[0-9a-f]{16}:[0-9a-f]{64}$")
OFF = (
    f"hidden evaluation sets are off: they need {KEY_ENV} and {INDEX_ENV}, the deployment's "
    "own (docs/operations/evalsets.md)"
)
# What `export` writes, line by line: this format's header, then each row as stored.
EXPORT_FORMAT = "mendari.evalset_export/1"


class SealedTable(NamedTuple):
    """An `evalset_` table's sealed values: each sealed column with the kind it is sealed as,
    the columns holding the ids each value is bound to, and the column naming a row in what is
    printed beside its set (None: the set's id alone; never a case's id)."""

    columns: Mapping[str, Kind]
    set_id: str
    case_id: str | None
    shown: str | None


# Every sealed column of the `evalset_` tables, by table (LP-11f): a rotation re-seals, its check
# opens, `verify` opens and the export copies exactly these. Every other column of an `evalset_`
# table is in the clear (ids, organizations, states, times, keyed hashes, commitments), and a
# test pins each column to one list or the other, so a sealed column a later step adds is never
# left out. Each table here has an `id` (its primary key, which a rotation pages by), an `org`
# and a `created_at`.
SEALED_COLUMNS: dict[str, SealedTable] = {
    "evalset_sets": SealedTable(
        {"header": "header", "freeze_record": "freeze", "aggregates": "aggregates"},
        set_id="id",
        case_id=None,
        shown=None,
    ),
    "evalset_cases": SealedTable(
        {"input": "input", "attributes": "attributes"},
        set_id="set_id",
        case_id="id",
        shown="case_hash",
    ),
}


def case_ref(salt: bytes, set_id: str, case_id: str, value: Any, attributes: Any) -> str:
    """A case's commitment, as `evalset.case_added` chains it: SHA-256 of `CASE_REF_PREFIX`, the
    salt and the canonical JSON (`canonical_input`) of the case's set, id, input and attributes
    (its own, with what the store added, without the salt)."""
    body = {"set": set_id, "case": case_id, "input": value, "attributes": attributes}
    return hashlib.sha256(CASE_REF_PREFIX + salt + canonical_input(body).encode()).hexdigest()


def read_cases(path: Path) -> list[tuple[int, Any]]:
    """The records of a JSONL file of cases, each with its line number (blank lines skipped). A
    line that is not UTF-8 or not JSON is a `MendariError` naming its line, never its text."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        number = raw.count(b"\n", 0, error.start) + 1
        raise MendariError(f"line {number}: not UTF-8 text") from None
    records = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append((number, json.loads(line)))
        except (ValueError, RecursionError):  # JSONDecodeError, over 4,300 digits, too deep
            raise MendariError(f"line {number}: not JSON") from None
    if not records:
        raise MendariError("the file holds no case")
    return records


class EvalsetStore:
    """Hidden evaluation sets over an `AuditStore` (its database, chain and marks), with the
    deployment's evaluation-set keys. Without keys (the feature off) it lists sets only."""

    def __init__(self, store: AuditStore, keys: EvalsetKeys | None = None) -> None:
        self.store = store
        self.keys = keys
        self._index_epoch: str | None = None  # the index key id this store last recorded under

    def _ring(self) -> EvalsetKeys:
        if self.keys is None:
            raise MendariError(OFF)
        return self.keys

    # -- writes ---------------------------------------------------------------------------

    def create_set(self, org: str, pack: Pack, name: str, *, created_by: str) -> dict[str, Any]:
        """A new draft set of `org` for `pack`, its header sealed; its id and state."""
        keys = self._ring()
        try:
            accounts.check_org_id(org)
        except ValueError as error:
            raise MendariError(str(error)) from None
        name = " ".join(name.split())
        if not name or len(name) > NAME_CHARS:
            raise MendariError(f"a set's name is one line of 1 to {NAME_CHARS} characters")
        if not created_by.strip():
            raise MendariError("who creates a set is named")
        manifest = pack.manifest
        derived = manifest.derived
        header = {
            "pack": pack.id,
            "name": name,
            "questions": {q: manifest.labels(q) for q in manifest.questions if q not in derived},
            "derived": {q: manifest.labels(q) for q in derived},
            "critical": [
                {"question": item.question, "label": item.label}
                for item in manifest.evaluation.critical
            ],
        }
        set_id = str(uuid.uuid4())
        sealed = keys.seal("header", header, set_id)
        with self.store._db.session(exclusive=True) as db:
            self.store._evalset_ready(db, keys)
            created_at = _now()
            db.all(
                "INSERT INTO evalset_sets (id, org, state, created_at, created_by, header) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (set_id, org, DRAFT, created_at, created_by, sealed),
            )
            chained = self.store._bound("evalset.created", {"set": set_id}, org)
            self.store._append(db, f"evalset:{set_id}", "evalset.created", chained)
        return {"set": set_id, "org": org, "state": DRAFT, "created_at": created_at}

    def add_cases(
        self, set_id: str, records: Sequence[Any], *, lines: Sequence[int] | None = None
    ) -> dict[str, Any]:
        """Add hand-written cases to a draft set, all or none: any record refused (each named by
        its line in `lines`, else its position) refuses them all, before anything is written.
        The set's id, how many were added, and each one's line and case hash."""
        keys = self._ring()
        _check_set_id(set_id)
        where = list(lines) if lines is not None else list(range(1, len(records) + 1))
        if len(where) != len(records):
            raise ValueError("one line number per record")
        if not records:
            raise MendariError("no case to add")
        with self.store._db.session(exclusive=False) as db:
            found = db.one("SELECT state, header FROM evalset_sets WHERE id = ?", (set_id,))
        header, _ = keys.open("header", str(_draft(set_id, found)["header"]), set_id)
        problems = case_problems(records, header)
        if problems:
            first, more = problems[0], len(problems) - 1
            line = "line" if lines is not None else "record"
            extra = f" (and {more} more)" if more else ""
            raise MendariError(f"{line} {where[first[0]]}: {first[1]}{extra}")
        index_key = key_id(keys.index_key)
        cases, index, made = [], [], []
        for at, record in zip(where, records, strict=True):
            case_id = str(uuid.uuid4())
            salt = secrets.token_bytes(32)
            attributes = dict(record["attributes"])
            if record.get("id") is not None:
                attributes["author_case_id"] = record["id"]
            if record.get("labels"):
                attributes["intended"] = record["labels"]
            value = record["input"]
            sealed_attributes = {"salt": base64.b64encode(salt).decode(), "attributes": attributes}
            hashed = keys.case_hash(set_id, case_id)
            cases.append(
                (
                    case_id,
                    keys.seal("input", value, set_id, case_id),
                    keys.seal("attributes", sealed_attributes, set_id, case_id),
                    hashed,
                )
            )
            for variant, digest in keys.index_hashes(value).items():
                index.append((digest, variant, index_key, case_id))
            made.append((at, hashed, case_ref(salt, set_id, case_id, value, attributes)))
        with self.store._db.session(exclusive=True) as db:
            self.store._evalset_ready(db, keys)
            found = db.one("SELECT org, state FROM evalset_sets WHERE id = ?", (set_id,))
            org = str(_draft(set_id, found)["org"])  # again, in the write's own transaction
            created_at = _now()
            _insert(
                db,
                "evalset_cases",
                ("id", "set_id", "org", "input", "attributes", "case_hash", "status", "created_at"),
                [(c, set_id, org, i, a, h, DRAFT, created_at) for c, i, a, h in cases],
            )
            _insert(
                db,
                "evalset_index",
                ("hash", "variant", "key_id", "set_id", "case_id", "org"),
                [(d, v, k, set_id, c, org) for d, v, k, c in index],
            )
            epoch = self._mark_index_epoch(db, keys)
            entries = [
                (
                    f"evalset:{set_id}",
                    self.store._bound(
                        "evalset.case_added", {"set": set_id, "case": hashed, "case_ref": ref}, org
                    ),
                )
                for _, hashed, ref in made
            ]
            self.store._append_many(db, "evalset.case_added", entries)
        if epoch is not None:
            self._index_epoch = epoch
        return {
            "set": set_id,
            "added": len(made),
            "cases": [{"line": at, "case": hashed} for at, hashed, _ in made],
        }

    def remove_case(self, set_id: str, case: str) -> dict[str, Any]:
        """Remove a case of a draft set (named by its case hash, as `add_cases` gave it, or by
        its id), with its index rows; `evalset.case_removed` is chained."""
        keys = self._ring()
        _check_set_id(set_id)
        if not (_CASE_HASH.fullmatch(case) or _is_uuid(case)):
            raise MendariError("a case is named by its case hash, as `mendari evalset add` printed")
        with self.store._db.session(exclusive=True) as db:
            self.store._evalset_ready(db, keys)
            head = _draft(
                set_id, db.one("SELECT org, state FROM evalset_sets WHERE id = ?", (set_id,))
            )
            row = db.one(
                "SELECT id, case_hash FROM evalset_cases WHERE set_id = ? "
                "AND (case_hash = ? OR id = ?)",
                (set_id, case, case),
            )
            if row is None:
                raise MendariError(f"no case {case} in evaluation set {set_id}")
            db.all("DELETE FROM evalset_index WHERE case_id = ?", (row["id"],))
            db.all("DELETE FROM evalset_cases WHERE id = ?", (row["id"],))
            payload = {"set": set_id, "case": str(row["case_hash"])}
            chained = self.store._bound("evalset.case_removed", payload, str(head["org"]))
            self.store._append(db, f"evalset:{set_id}", "evalset.case_removed", chained)
        return {"set": set_id, "removed": str(row["case_hash"])}

    def _mark_index_epoch(self, db: Session, keys: EvalsetKeys) -> str | None:
        """A new index key starts an epoch: the first index row under a key id this database has
        not seen chains `evalset_index.epoch` (the key id, a deployment event under
        `KEYS_CHAIN`), once per key, whichever deployment writes it. The key id, for the caller
        to remember once its transaction commits (None: already remembered)."""
        current = key_id(keys.index_key)
        if self._index_epoch == current:
            return None
        added = db.all(
            "INSERT INTO settings (name, value) VALUES (?, ?) ON CONFLICT (name) DO NOTHING "
            "RETURNING name",
            (EVALSET_INDEX_EPOCH + current, _now()),
        )
        if added:
            self.store._append(db, KEYS_CHAIN, "evalset_index.epoch", {"key_id": current})
        return current

    # -- the keys' lifecycle (LP-11f) ----------------------------------------------------------

    def rotate(self, *, to: str, batch: int = ROTATE_BATCH) -> dict[str, Any]:
        """Re-seal under the current evaluation-set key every sealed value sealed under another
        (`mendari evalset rotate`), as `AuditStore.rotate_phi` does for the PHI key. `to` is the
        current key's id, as the operator read it off `/api/health` (`evalset_keys.current`): a
        run whose key is another refuses, and so does one to a key an earlier rotation retired;
        the target is recorded (`EVALSET_TARGET`). Rows are read in pages by id, table by table
        (`SEALED_COLUMNS`), opened with the ring and re-sealed outside the chain lock, then
        written in a short exclusive session that checks the keys again (`_evalset_ready`) and
        changes a row only if its sealed values are still what was read (compare-and-set),
        chaining one `evalset.rotated` with what it changed; the keys values moved off are
        marked retired in that session, so nothing seals with them here again. A re-seal
        changes the ciphertext, never the value, so it runs in every state of a set, a frozen
        one's included (conflict 31): `check` recomputes the commitments afterwards. An
        interrupted run is run again: what is done stays done. A row holding a value no key
        opens is left as it is and listed in `unopenable` (its set, a case by its hash, the
        value's kind and the key it names)."""
        keys = self._ring()
        if batch < 1:
            raise MendariError("a batch is 1 row or more")
        current = key_id(keys.key)
        if to != current:
            raise MendariError(
                f"this run's {KEY_ENV} is key {current}, not {to}: run with the key the deployment "
                "seals with (its id is evalset_keys.current in /api/health)"
            )
        with self.store._db.session(exclusive=True) as db:
            if db.one("SELECT name FROM settings WHERE name = ?", (EVALSET_RETIRED + to,)):
                raise MendariError(
                    f"key {to} was retired by an earlier rotation: make a new key and rotate to "
                    "it (docs/operations/evalsets.md, Rotating the evaluation-set key)"
                )
            self.store._evalset_ready(db, keys)
            db.all(
                "INSERT INTO settings (name, value) VALUES (?, ?) "
                "ON CONFLICT (name) DO UPDATE SET value = excluded.value",
                (EVALSET_TARGET, to),
            )
        summary: dict[str, Any] = {
            "to": to,
            "counts": dict.fromkeys(SEALED_COLUMNS, 0),
            "batches": 0,
            "from": Counter(),
            "unopenable": [],
        }
        for table in SEALED_COLUMNS:
            self._rotate_table(table, f"v2:{to}:", batch, summary)
        return {**summary, "from": dict(sorted(summary["from"].items()))}

    def _rotate_table(self, table: str, prefix: str, batch: int, summary: dict[str, Any]) -> None:
        """One table's rows holding a value sealed under another key than `prefix` names,
        re-sealed a page at a time (`_apply_rotation` writes each page)."""
        keys = self._ring()
        sealed = SEALED_COLUMNS[table]
        stale = " OR ".join(
            f"({column} IS NOT NULL AND substr({column}, 1, 20) <> ?)" for column in sealed.columns
        )
        after = ""
        while True:
            with self.store._db.session(exclusive=False) as db:
                rows = db.all(
                    f"SELECT {_selected(sealed)} FROM {table} WHERE id > ? AND ({stale}) "
                    "ORDER BY id LIMIT ?",
                    (after, *(prefix for _ in sealed.columns), batch),
                )
            if not rows:
                return
            after = rows[-1]["id"]
            changes = []
            for row in rows:
                set_id, case_id = _bound_to(sealed, row)
                resealed: dict[str, str] = {}
                froms: list[str] = []
                failed = None
                for column, kind in sealed.columns.items():
                    token = row[column]
                    if token is None or str(token).startswith(prefix):
                        continue
                    try:
                        value, opened = keys.open(kind, str(token), set_id, case_id)
                    except MendariError:
                        failed = {**_named(sealed, row), "kind": kind, "key": token_key(str(token))}
                        break
                    resealed[column] = keys.seal(kind, value, set_id, case_id)
                    froms.append(opened)
                if failed is not None:
                    summary["unopenable"].append(failed)
                    continue
                changes.append((row, resealed, froms))
            if changes:
                self._apply_rotation(table, changes, summary)

    def _apply_rotation(self, table: str, changes: list[Any], summary: dict[str, Any]) -> None:
        """Write one page's re-sealed values where the row still holds what was read, and chain
        the batch (`evalset.rotated`: key ids and counts, never a key), retiring each key it
        moved values off."""
        keys = self._ring()
        sealed = SEALED_COLUMNS[table]
        moved: Counter[str] = Counter()
        done = 0
        with self.store._db.session(exclusive=True) as db:
            self.store._evalset_ready(db, keys)
            for row, resealed, froms in changes:
                changed = ", ".join(f"{column} = ?" for column in resealed)
                same = " AND ".join(
                    f"{column} IS NULL" if row[column] is None else f"{column} = ?"
                    for column in sealed.columns
                )
                old = [row[column] for column in sealed.columns if row[column] is not None]
                written = db.all(
                    f"UPDATE {table} SET {changed} WHERE id = ? AND {same} RETURNING id",
                    (*resealed.values(), row["id"], *old),
                )
                if written:  # still what was read: neither removed nor re-sealed since
                    done += 1
                    moved.update(froms)
            if done:
                for retired in sorted(moved):
                    if retired != summary["to"]:
                        _mark(db, EVALSET_RETIRED + retired, _now())
                counts = {name: done if name == table else 0 for name in SEALED_COLUMNS}
                payload = {
                    "to": summary["to"],
                    "from": dict(sorted(moved.items())),
                    "counts": counts,
                }
                self.store._append(db, KEYS_CHAIN, "evalset.rotated", payload)
        summary["counts"][table] += done
        summary["from"].update(moved)
        summary["batches"] += bool(done)

    def check(self, *, batch: int = 500) -> dict[str, Any]:
        """Reads only (`mendari evalset rotate --check`, on a store opened read-only): whether
        every sealed value opens with the current key alone, as dropping the read keys
        requires, as `AuditStore.phi_check` does for the PHI key. Counts the sealed values by
        the key each names, with the newest under each other key; opens each with the current
        key only (`current_only`); for each case, recomputes the commitment its
        `evalset.case_added` chained (`case_ref`), its keyed hash when it is under the current
        index key, and its two index rows under the current index key (the current epoch's: a
        case without them waits for `reindex`). Counts the index rows by key id. A failure
        names the value's kind (`case` for a case's checks), its set, a case by its hash, and
        the key it names or what did not match (`commitment`, `case_hash`, `index`). `ready`
        only when everything opened and matched and the current key is not retired. Nothing is
        chained, marked or written."""
        keys = self._ring()
        alone = keys.current_only()
        current = key_id(keys.key)
        index_now = key_id(keys.index_key)
        by_key: dict[str, Counter[str]] = {table: Counter() for table in SEALED_COLUMNS}
        newest: dict[str, str] = {}
        failures: list[dict[str, str]] = []
        retired: set[str] = set()
        with self.store._db.session(exclusive=False) as db:
            present = [table for table in SEALED_COLUMNS if db.columns(table)]
            refs = {
                str(payload.get("case")): payload.get("case_ref")
                for row in db.all(
                    "SELECT payload FROM audit_events WHERE event = ?", ("evalset.case_added",)
                )
                if isinstance(payload := json.loads(row["payload"]), Mapping)
            }
            if "settings" not in self.store.unmigrated:  # an older database, read as it is
                retired = {
                    name[len(EVALSET_RETIRED) :]
                    for row in db.all(
                        "SELECT name FROM settings WHERE name LIKE ?", (EVALSET_RETIRED + "%",)
                    )
                    if (name := str(row["name"])).startswith(EVALSET_RETIRED)
                }
            indexed_by_key = (
                {
                    str(row["key_id"]): int(row["n"])
                    for row in db.all(
                        "SELECT key_id, count(*) AS n FROM evalset_index GROUP BY key_id"
                    )
                }
                if db.columns("evalset_index")
                else {}
            )
        for table in present:
            sealed = SEALED_COLUMNS[table]
            after = ""
            while True:
                with self.store._db.session(exclusive=False) as db:
                    rows = db.all(
                        f"SELECT {_selected(sealed)}, org, created_at FROM {table} WHERE id > ? "
                        "ORDER BY id LIMIT ?",
                        (after, batch),
                    )
                    cases = [str(row["id"]) for row in rows] if table == "evalset_cases" else []
                    indexed = _index_rows(db, index_now, cases)
                if not rows:
                    break
                after = rows[-1]["id"]
                for row in rows:
                    set_id, case_id = _bound_to(sealed, row)
                    values: dict[str, Any] = {}
                    for column, kind in sealed.columns.items():
                        token = row[column]
                        if token is None:
                            continue
                        named = token_key(str(token))
                        by_key[table][named] += 1
                        if named != current:
                            newest[named] = max(newest.get(named, ""), str(row["created_at"]))
                        try:
                            values[column] = alone.open(kind, str(token), set_id, case_id)[0]
                        except MendariError:
                            failures.append({"kind": kind, **_named(sealed, row), "key": named})
                    if table == "evalset_cases" and len(values) == len(sealed.columns):
                        failures += [
                            {"kind": "case", **_named(sealed, row), "key": problem}
                            for problem in _case_problems(keys, row, values, refs, indexed)
                        ]
        read = sorted(keys.ring_ids - {current})
        return {
            "current": current,
            "read": read,
            # A read key no rotation moved values off is not retired: a deployment that still
            # seals with it (built before the swap) would go on doing so; delete those first.
            "read_not_retired": [key for key in read if key not in retired],
            "current_retired": current in retired,
            "by_key": {table: dict(sorted(counts.items())) for table, counts in by_key.items()},
            "newest_not_current": dict(sorted(newest.items())),
            "index": {"current": index_now, "by_key": dict(sorted(indexed_by_key.items()))},
            "failures": len(failures),
            "failed": failures[:CHECK_FAILURES_SHOWN],
            "ready": not failures and current not in retired,
        }

    def reindex(self) -> dict[str, Any]:
        """Rebuild the whole hidden-case index under the current index key
        (`mendari evalset reindex`), in one transaction: every case's input is opened with the
        ring and indexed under both forms, every other index row is deleted, and
        `evalset_index.rebuilt` (the index key's id and how many cases) is chained, after
        `evalset_index.epoch` when the key is new. A reader sees the index before or after,
        never a mix of key ids. Every earlier index key (each with an epoch here, rows or none)
        is retired in the same transaction (`EVALSET_INDEX_RETIRED`), so a deployment or shell
        still holding one indexes nothing more under it, where `hidden_matches` would not look.
        A case input no key opens refuses it all, before anything is written, and so does an
        index key older than one that started an epoch here since (it would retire the newer
        key, `_refuse_an_older_index_key`). Chained case
        hashes keep the index key id they were made under: a case's commitment, which no index
        key keys, carries its binding across epochs. The index key's id, how many cases were
        indexed, and the index rows it replaced by key id."""
        keys = self._ring()
        index_key = key_id(keys.index_key)
        with self.store._db.session(exclusive=True) as db:
            self.store._evalset_ready(db, keys)
            _refuse_an_older_index_key(db, index_key)
            rows = db.all("SELECT id, set_id, org, input FROM evalset_cases ORDER BY id")
            entries: list[tuple[str, str, str, str, str, str]] = []
            unopenable: Counter[str] = Counter()
            for row in rows:
                set_id, case_id = str(row["set_id"]), str(row["id"])
                try:
                    value, _ = keys.open("input", str(row["input"]), set_id, case_id)
                except MendariError:
                    unopenable[token_key(str(row["input"]))] += 1
                    continue
                for variant, digest in keys.index_hashes(value).items():
                    entries.append((digest, variant, index_key, set_id, case_id, str(row["org"])))
            if unopenable:
                named = ", ".join(f"{key} ({count})" for key, count in sorted(unopenable.items()))
                raise MendariError(
                    f"{sum(unopenable.values())} case input(s) no key here opens, sealed under "
                    f"{named}: nothing was reindexed; add that key to {READ_ENV}, then run again"
                )
            replaced = {
                str(row["key_id"]): int(row["n"])
                for row in db.all("SELECT key_id, count(*) AS n FROM evalset_index GROUP BY key_id")
            }
            db.all("DELETE FROM evalset_index")
            _insert(
                db,
                "evalset_index",
                ("hash", "variant", "key_id", "set_id", "case_id", "org"),
                entries,
            )
            epoch = self._mark_index_epoch(db, keys) if entries else None
            earlier = set(replaced) | {
                name[len(EVALSET_INDEX_EPOCH) :]
                for row in db.all(
                    "SELECT name FROM settings WHERE name LIKE ?", (EVALSET_INDEX_EPOCH + "%",)
                )
                if (name := str(row["name"])).startswith(EVALSET_INDEX_EPOCH)
            }
            for retired in sorted(earlier - {index_key}):
                _mark(db, EVALSET_INDEX_RETIRED + retired, _now())
            rebuilt = {"key_id": index_key, "count": len(rows)}
            self.store._append(db, KEYS_CHAIN, "evalset_index.rebuilt", rebuilt)
        if epoch is not None:
            self._index_epoch = epoch
        return {**rebuilt, "replaced": dict(sorted(replaced.items()))}

    def export(self) -> list[dict[str, Any]]:
        """The sealed export (`mendari evalset export`), opening nothing and needing no key: a
        header (`EXPORT_FORMAT`, when, how many rows of each table, and how many sealed values
        each key id sealed, the keys a restore needs), then every row of each table holding
        sealed values (`SEALED_COLUMNS`) as stored: each sealed value its ciphertext, the rest
        ids, organizations, states, times and keyed case hashes. The hidden-case index is left
        out: `reindex` rebuilds it from the sealed inputs. Cases are read before sets, so every
        case's set is in it (a set is never deleted). Nothing is chained or marked."""
        tables: dict[str, list[dict[str, Any]]] = {}
        with self.store._db.session(exclusive=False) as db:
            for table in reversed(SEALED_COLUMNS):
                present = db.columns(table)
                tables[table] = db.all(f"SELECT * FROM {table} ORDER BY id") if present else []
        sealed_by: Counter[str] = Counter(
            token_key(str(row[column]))
            for table, sealed in SEALED_COLUMNS.items()
            for row in tables[table]
            for column in sealed.columns
            if row.get(column) is not None
        )
        header = {
            "format": EXPORT_FORMAT,
            "exported_at": _now(),
            "rows": {table: len(tables[table]) for table in SEALED_COLUMNS},
            "key_ids": dict(sorted(sealed_by.items())),
        }
        return [header, *({"table": t, "row": row} for t in SEALED_COLUMNS for row in tables[t])]

    # -- reads ----------------------------------------------------------------------------

    def sets(self, org: str | None = None) -> list[dict[str, Any]]:
        """Every set (of `org`; None: every organization's, the operator's view) with its state
        and how many cases it holds: ids and counts, nothing sealed is opened."""
        with self.store._db.session(exclusive=False) as db:
            if not (db.columns("evalset_sets") and db.columns("evalset_cases")):
                return []  # an older database, read as it is
            where, params = ("WHERE s.org = ?", (org,)) if org is not None else ("", ())
            rows = db.all(
                "SELECT s.id, s.org, s.state, s.created_at, (SELECT count(*) FROM evalset_cases "
                f"c WHERE c.set_id = s.id) AS cases FROM evalset_sets s {where} "
                "ORDER BY s.created_at, s.id",
                params,
            )
        return [
            {
                "set": row["id"],
                "org": row["org"],
                "state": row["state"],
                "created_at": row["created_at"],
                "cases": int(row["cases"]),
            }
            for row in rows
        ]

    def hidden_matches(self, value: Any) -> list[dict[str, str]]:
        """The cases, of every organization's sets, whose input equals `value` exactly or under
        `norm/v1`, by set and case id: the hidden-case index, for the pack store's upload check
        (LP-20). In the store only: no route or command answers it."""
        hashes = self._ring().index_hashes(value)
        with self.store._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT DISTINCT set_id, case_id FROM evalset_index WHERE hash IN (?, ?) "
                "ORDER BY set_id, case_id",
                (hashes["canonical"], hashes["text"]),
            )
        return [{"set": str(row["set_id"]), "case": str(row["case_id"])} for row in rows]


def case_problems(records: Sequence[Any], header: Mapping[str, Any]) -> list[tuple[int, str]]:
    """Why records cannot be hidden cases of a set with this header, by position: (index, what),
    the first problem of each refused record. What names a JSON path, never a value."""
    problems: list[tuple[int, str]] = []
    checked: list[tuple[int, Any]] = []
    for index, record in enumerate(records):
        problem = _problem(record, header)
        if problem is None:
            checked.append((index, record["input"]))
        else:
            problems.append((index, problem))
    if checked:
        exact, folded = _public_forms()
        for index, value in checked:
            if (
                _digest(canonical_input(value)) in exact
                or _digest(normalised_input(value)) in folded
            ):
                problems.append(
                    (
                        index,
                        "its input is a public case's (the reference packs' data, datasets/ or "
                        "a generator's output): a hidden case is written by hand",
                    )
                )
    return sorted(problems)


def _problem(record: Any, header: Mapping[str, Any]) -> str | None:
    if not isinstance(record, dict):
        return "a case is a JSON object"
    for block in REFUSED_BLOCKS:
        if block in record:
            return (
                f"a `{block}` block is refused: it holds what a model decided or what people "
                "rated, and a hidden case is written by hand"
            )
    if set(record) - RECORD_KEYS:
        return "a case holds only id, input, labels and attributes"
    what = not_json(record)
    if what is not None:
        return f"not JSON, it holds {what}"
    if not isinstance(record.get("input"), dict):
        return "each case needs an 'input' object"
    author_id = record.get("id")
    if author_id is not None and not (
        (isinstance(author_id, str) and author_id.strip())
        or (isinstance(author_id, int) and not isinstance(author_id, bool))
    ):
        return "id is a string or a whole number"
    attributes = record.get("attributes")
    if not isinstance(attributes, dict):
        return "`attributes` must be a mapping (with author, language, stratum and source)"
    if attributes.get("source") != SOURCE:
        return f"attributes.source must be {SOURCE} (de-identified cases wait for B20)"
    if attributes.get("template") is not None:
        return "attributes.template must be null: a case written from a template is refused"
    for name in REQUIRED:
        given = attributes.get(name)
        if not (isinstance(given, str) and given.strip()):
            return f"attributes.{name} is missing: a non-empty string"
    for name in ADDED:
        if name in attributes:
            return f"attributes.{name} is the store's own: leave it out"
    labels = record.get("labels", {})
    if not isinstance(labels, dict):
        return "`labels` must be a mapping of question to label"
    questions = {**header["questions"], **header["derived"]}
    for question, label in labels.items():
        # Only what the pack defines is named: a label written here can hold case text.
        if question not in questions:
            return "labels name a question the pack does not have"
        if label not in questions[question]:
            return f"labels.{question} is not one of its labels"
    return None


def _selected(sealed: SealedTable) -> str:
    """The columns a rotation or its check reads of a table: its id, the ids its values are
    bound to, the column that names it, and its sealed columns."""
    named = (sealed.set_id, sealed.case_id, sealed.shown)
    return ", ".join(dict.fromkeys(("id", *filter(None, named), *sealed.columns)))


def _bound_to(sealed: SealedTable, row: Mapping[str, Any]) -> tuple[str, str]:
    """The set and case ids a row's sealed values are bound to (an empty case id: the set's)."""
    case_id = "" if sealed.case_id is None else str(row[sealed.case_id])
    return str(row[sealed.set_id]), case_id


def _named(sealed: SealedTable, row: Mapping[str, Any]) -> dict[str, str]:
    """A row as printed: its set's id and, for a case's values, the case's keyed hash (never
    its id)."""
    named = {"set": str(row[sealed.set_id])}
    if sealed.shown is not None:
        named["case"] = str(row[sealed.shown])
    return named


def _refuse_an_older_index_key(db: Session, index_key: str) -> None:
    """A reindex never goes back to an older index key (`reindex`): one that already started an
    epoch here, when a later epoch started under another. That reindex would retire the newer
    key, and the deployments holding it would then refuse to start; a new key has no epoch yet
    and passes."""
    if not db.one("SELECT name FROM settings WHERE name = ?", (EVALSET_INDEX_EPOCH + index_key,)):
        return
    newest = db.one(
        "SELECT payload FROM audit_events WHERE decision_id = ? AND event = ? "
        "ORDER BY seq DESC LIMIT 1",
        (KEYS_CHAIN, "evalset_index.epoch"),
    )
    later = json.loads(newest["payload"]).get("key_id") if newest else None
    if later is not None and later != index_key:
        raise MendariError(
            f"index key {index_key} is older than index key {later}, which already indexes "
            f"cases here: reindex with the newest {INDEX_ENV}"
        )


def _index_rows(
    db: Session, index_key: str, case_ids: Sequence[str]
) -> dict[str, dict[str, tuple[str, str, str]]]:
    """The index rows of these cases under one index key: by case id, each variant's hash, set
    id and organization."""
    found: dict[str, dict[str, tuple[str, str, str]]] = {}
    if not case_ids:
        return found
    marks = ", ".join("?" for _ in case_ids)
    for row in db.all(
        "SELECT case_id, variant, hash, set_id, org FROM evalset_index "
        f"WHERE key_id = ? AND case_id IN ({marks})",
        (index_key, *case_ids),
    ):
        found.setdefault(str(row["case_id"]), {})[str(row["variant"])] = (
            str(row["hash"]),
            str(row["set_id"]),
            str(row["org"]),
        )
    return found


def _case_problems(
    keys: EvalsetKeys,
    row: Mapping[str, Any],
    values: Mapping[str, Any],
    refs: Mapping[str, Any],
    indexed: Mapping[str, Mapping[str, tuple[str, str, str]]],
) -> list[str]:
    """What a case's opened values do not match (`check`): the commitment its event chained
    (`commitment`), its keyed hash when it is under the current index key (`case_hash`), and
    its two index rows under the current index key (`index`)."""
    problems = []
    set_id, case_id, hashed = str(row["set_id"]), str(row["id"]), str(row["case_hash"])
    body = values["attributes"]
    try:
        salt = base64.b64decode(str(body.get("salt", "")), validate=True)
        made: str | None = case_ref(salt, set_id, case_id, values["input"], body.get("attributes"))
    except (MendariError, ValueError, AttributeError):
        made = None
    if made is None or made != refs.get(hashed):
        problems.append("commitment")
    index_key = key_id(keys.index_key)
    if hashed.startswith(f"hmac-sha256:{index_key}:") and keys.case_hash(set_id, case_id) != hashed:
        problems.append("case_hash")
    try:
        expected = {
            variant: (digest, set_id, str(row["org"]))
            for variant, digest in keys.index_hashes(values["input"]).items()
        }
    except MendariError:
        expected = {}
    if not expected or indexed.get(case_id, {}) != expected:
        problems.append("index")
    return problems


def _check_set_id(set_id: str) -> None:
    if not _is_uuid(set_id):
        raise MendariError("a set is named by its id, as `mendari evalset create` printed it")


def _draft(set_id: str, row: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """A set's row when it is a draft; a `MendariError` when there is none or it is not one."""
    if row is None:
        raise MendariError(f"no evaluation set {set_id}")
    if row["state"] != DRAFT:
        raise MendariError(f"evaluation set {set_id} is {row['state']}: only a draft changes")
    return row


def _is_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


def _insert(db: Session, table: str, columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    """Insert `rows` into `table`, `_INSERT_BATCH` to a statement."""
    marks = "(" + ", ".join("?" for _ in columns) + ")"
    for start in range(0, len(rows), _INSERT_BATCH):
        batch = rows[start : start + _INSERT_BATCH]
        db.all(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES " + ", ".join(marks for _ in batch),
            [value for row in batch for value in row],
        )


def _digest(form: str) -> bytes:
    return hashlib.sha256(form.encode()).digest()


@functools.cache
def _public_forms() -> tuple[frozenset[bytes], frozenset[bytes]]:
    """The digests of every public case's input in both forms (strict, `norm/v1`), read once per
    process: about 17,000 inputs, under a second."""
    exact: set[bytes] = set()
    folded: set[bytes] = set()
    for value in _public_inputs():
        try:
            exact.add(_digest(canonical_input(value)))
            folded.add(_digest(normalised_input(value)))
        except MendariError:
            continue
    return frozenset(exact), frozenset(folded)


def _public_inputs() -> Iterator[Any]:
    """Every public case's input: each JSONL record's under the reference packs and the example
    datasets as this release bundles them (`mendari/_packs`, `mendari/_datasets`, or a checkout's
    `packs/` and `datasets/`), and each generator's at its default seed."""
    from mendari.core.reference import bundled_dir
    from mendari.sandbox import (
        example_datasets_dir,
        synthetic_guard,
        synthetic_inbox,
        synthetic_prior_auth,
        synthetic_referral,
        synthetic_trial,
    )

    for root in (bundled_dir(), example_datasets_dir()):
        if root is None:
            continue
        for path in sorted(root.rglob("*.jsonl")):
            yield from _inputs_of(path)
    for generator in (
        synthetic_inbox,
        synthetic_trial,
        synthetic_referral,
        synthetic_prior_auth,
        synthetic_guard,
    ):
        for records in generator.public_splits().values():
            yield from (record["input"] for record in records)
        yield from (record["input"] for record in generator.local_site())


def _inputs_of(path: Path) -> Iterator[Any]:
    """The `input` of each record of a JSONL file that has one (a ledger's lines have none)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    for line in text.splitlines():
        try:
            record = json.loads(line) if line.strip() else None
        except (ValueError, RecursionError):
            continue
        if isinstance(record, dict) and isinstance(record.get("input"), dict):
            yield record["input"]
