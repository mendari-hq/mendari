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
never a value. PHI retention, erasure and key rotation never touch these rows."""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import re
import secrets
import uuid
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from mendari.core import accounts
from mendari.core.audit import EVALSET_INDEX_EPOCH, KEYS_CHAIN, AuditStore, _now
from mendari.core.db import Session
from mendari.core.evalset_keys import INDEX_ENV, KEY_ENV, EvalsetKeys
from mendari.core.pack import Pack, not_json
from mendari.core.phi import key_id
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
