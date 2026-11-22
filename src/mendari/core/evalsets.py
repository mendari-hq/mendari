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
  the bundled agents' data and `datasets/` as this release bundles them, and each generator's
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
Each prints ids, case hashes, key ids and counts only, and only `rotate` and `reindex` chain.

Blind labelling (LP-11h), on a draft set: the key holder's command line, and a labeller's three
routes (LP-11j, `server/app.py` `add_labelling_routes`: `next_case`, `record_label` and their own
counts, `labelling`):
- `sample` draws, seeded, within each stratum of the set's cases (a number per stratum, or a
  number in all shared out by the strata's sizes), from those the set's latest draw did not
  take; each drawn case's status becomes `sampled` and its sealed attributes gain its weight,
  N_h / n_h (the stratum's cases over those drawn from it, for every drawn case of the stratum,
  at each draw), with a salt new at each draw. Chained: `evalset.sampled {set, org, n, seed,
  weight_refs}`, counts and each drawn case's weight's commitment by its keyed hash
  (`weight_ref`, LP-11p), never a stratum or a weight.
- `assign` gives each sampled case two different labellers, balanced across pairs (within one
  case of each other) and across labellers (within two), from the user ids it is given,
  deterministically for a seed; `adjudicate` gives each disputed case a third who is neither of
  its first two. Each refuses anyone who does not hold the `labeller` role on a membership in
  force in the set's organization, and anyone who manages hidden sets there (`curate`: an
  administrator, separation of duties); an open assignment whose membership no longer holds is
  withdrawn, and its case given to another, on the next run. Each chooses cases by status, so
  each first checks, opening nothing, that every status is one the case's draw and its chained
  labels allow (LP-11p, below), and refuses a set where one is not, naming no case. Chained:
  `evalset.assigned {set, org, case, assignment, labeller, round}`.
- `next_case` serves a labeller, while their membership holds, their oldest open assignment in
  a draft set: the case's input and the set's questions with their labels, nothing else (no
  attribute, intended label, other label or model output). Chained in the same transaction,
  before it returns: `evalset.case_read {set, org, case, assignment, labeller}`.
- `record_label` takes one submission per assignment, every question answered, and never
  changes it: the values sealed with a 32-byte salt, bound to the label's own id. Chained:
  `evalset.labelled {set, org, case, label_ref, labeller, round}`, with `label_ref` the label's
  id and the SHA-256 of `LABEL_REF_PREFIX`, the salt and the canonical `{set, case, label,
  assignment, values}` (`label_ref`). A case's second first-pass label settles it `agreed` or
  `disputed`; its adjudicator's, by the majority on each question, `resolved`, or `unresolved`
  where any question has none (a three-way split), and only on a case whose two first-pass
  labels differ (LP-11p). The labeller is told how many cases are left,
  never how their label compares. A refused label is a `NoAssignment` (one refusal for every
  assignment not theirs to answer now) or a `LabelError` (naming the question, never a value).
- `labelling` counts a labeller's own assignments, set by set: how many wait and how many they
  labelled; `assignment_set` names the set of one of their assignments (what the security log
  says of a labelling route: set ids only).
- `progress` counts a set's cases by status and each labeller's assignments; `measure` computes
  the coverage report (`mendari.coverage/1`, over the drawn cases and their settled labels) and
  rater agreement (`mendari.agreement/1`, over the first-pass labels of the cases both labellers
  answered), seals them as the set's aggregates with a salt of their own and chains
  `evalset.measured {set, org, n, aggregates_ref}` (`aggregates_ref`, LP-11p).
Every opening of a case's input, attributes or labels is chained (sampling, a labeller's read,
labelling, measuring); opening a set's header is not. A case's status is `draft` until drawn,
then `sampled`, `agreed`, `disputed`, `resolved` or `unresolved`. Removing a case removes its
assignments and labels with it.

The lifecycle (LP-11i): `draft`, then `frozen`, `active` and `retired`. Every write above
refuses a set that is not a draft, in its own exclusive transaction; a rotation and a reindex
change ciphertext and keyed hashes only, so they run in every state (conflict 31).
- `freeze` fixes a draft set, the key holder's command line only (conflict 49). Every case is
  drawn (or, with `drop_unsampled`, an undrawn one is removed, each removal chained) and settled,
  agreed or resolved; then its content (`CONTENT_FORMAT`: the header's pack id, questions with
  their labels, derived questions and critical labels, the rotation rule's two limits and when
  it froze, and each case by id with its input, its final labels, the members' and those the
  pack derives from them, its status and its source, stratum, weight, language, author and
  template) is hashed (`content_hash`) and committed to with a 32-byte salt
  (`freeze_commitment`). The salt, the content hash and the derived labels are sealed as the
  set's freeze record, and the coverage and agreement of its final labels as its aggregates.
  Chained: `evalset.frozen {set, org, commitment, n, max_runs, max_age_days, aggregates_ref}`;
  the unsalted content hash never leaves the sealed record (conflict 44). A set whose rows the
  commitments below do not give is refused first, naming no case.
- `grade_read` is the one read of a frozen or active set's cases, in the process only (the
  grading route calls it, LP-11l, `server/app.py` `add_grading_routes`): for a live grading key
  of the set's organization, while the rotation rule holds (fewer chained `evalset.graded` than
  `max_runs`, and less than `max_age_days` since `frozen_at`), and the set's rows still give
  the content its commitment covers (a set whose chain holds its `evalset.retired` is retired,
  whatever its row says). In one exclusive transaction it checks all that, activates a frozen
  set (`evalset.activated`) and chains `evalset.graded {set, org, commitment, grader,
  pack_digest, n, run}`; only then does `run` receive the cases, in memory, and only what `run`
  returns comes back (with, for its answer, what was chained: `GradingRead`). Nothing is
  stored. A refusal is a `GradingRefused` with a fixed reason, which the route answers in place
  of the message; `grading_header` reads, unchained, the header of one of an organization's
  sets (its pack and questions), for the route to check the pack it is asked to run before any
  case is read.
- `retire` ends a frozen or active set's use, once, its content checked once more:
  `evalset.retired {set, org, commitment}`. Nothing is published.
- `show` reads a set's state, commitment, runs used and left, and its stored aggregates, when the
  commitment chained when they were stored covers them.
`mendari audit verify --evalsets` and `check` recompute each frozen set's content against the
commitment its `evalset.frozen` chained (`check`: each set the chain froze, whatever its row's
state now).

A draft's draws and statuses, and every set's stored aggregates (LP-11p): a case's status is clear
text, so it is checked against what commitments cover, its labels (`label_ref`) and the set's
latest draw (`weight_refs`, which names every drawn case), by `given_statuses`; a drawn case's
weight against its draw's commitment (`weight_holds`); a set's stored aggregates against the
commitment the latest `evalset.measured` or `evalset.frozen` chained (`stored_aggregates`). A
case's labels are those the chain recorded, each in the round and by the labeller its
`evalset.labelled` chained (`_chain`), never as an assignment row says now: a label the chain
holds and the rows lack, or a label row it never recorded, fails the case (`labels`), and an
adjudicator's label settles only a case whose two first-pass labels differ. `freeze` and
`measure` refuse a set whose labels, statuses or weights do not hold (`_mismatches`; they read
outside the chain's lock, so they read the set again under it first, and a set that changed
meanwhile is "run it again", never a refusal); `assign` and `adjudicate`, which open nothing,
refuse a set whose labels or statuses its chain does not allow (`_shape_mismatches`: they cannot
tell `agreed` from `disputed` without the labels' values, so an agreed case's status edited to
`disputed` is still adjudicated, and found afterwards); `show` refuses aggregates that do not
hold, and `check` and `mendari audit verify --evalsets` check all of it. A draw or a store from
before these commitments is read as before, and counted (`predating`).

An organization's curators (`curate`: its administrators, people only) manage its sets through
their routes (LP-11k, `server/app.py` `add_evalset_routes`): `named_sets` and `summary` read
(headers and stored aggregates, opened unchained, conflict 39; ids, counts and user ids), and
`sample`, `assign` and `adjudicate` write, each given the organization the route acts for
(`org`), so another organization's set is refused as an unknown one is (`NoEvalset`, `NO_SET`,
naming no id), in the write's own transaction. No route computes statistics, freezes or
retires: those stay the key holder's commands (conflicts 43 and 49)."""

from __future__ import annotations

import base64
import copy
import functools
import hashlib
import json
import random
import re
import secrets
import uuid
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from itertools import combinations
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, NoReturn, TypeVar

from mendari.core import accounts
from mendari.core.audit import (
    CHECK_FAILURES_SHOWN,
    EVALSET_INDEX_EPOCH,
    EVALSET_INDEX_RETIRED,
    EVALSET_KNOWN,
    EVALSET_RETIRED,
    EVALSET_TARGET,
    GRADING_ROLE,
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
from mendari.errors import GradingRefused, LabelError, MendariError, NoAssignment, NoEvalset
from mendari.evals.canonical import canonical_input, normalised_input

if TYPE_CHECKING:
    from mendari.core.spec import PackManifest
    from mendari.evals.harness import Dataset

T = TypeVar("T")

# A case's commitment (`case_ref`): unkeyed, hiding through its salt (kept only sealed in the
# case's attributes) and binding through SHA-256. No prefix of another of the deployment's
# hashes, keyed or not, nor of a sealed value's associated data.
CASE_REF_PREFIX = b"mendari-evalset-case-ref-v1\x00"
# A label's commitment (`label_ref`), as a case's: its salt is sealed only in the label's body.
LABEL_REF_PREFIX = b"mendari-evalset-label-ref-v1\x00"
# A frozen set's commitment (`freeze_commitment`, LP-11i): its salt and its content hash are
# sealed only in the set's freeze record (conflict 44: committed, never signed, in LP-11).
FREEZE_PREFIX = b"mendari-evalset-freeze-v1\x00"
# A drawn case's weight's commitment (`weight_ref`, LP-11p), as a case's: its salt, new at each
# draw, is sealed only in the case's attributes, beside the weight.
WEIGHT_REF_PREFIX = b"mendari-evalset-weight-ref-v1\x00"
# A set's stored aggregates' commitment (`aggregates_ref`, LP-11p): its salt, new at each store,
# is sealed only with the aggregates.
AGGREGATES_REF_PREFIX = b"mendari-evalset-aggregates-ref-v1\x00"
# What a frozen set's content hash is over (`content_hash`).
CONTENT_FORMAT = "mendari.evalset/1"
DRAFT = "draft"
FROZEN, ACTIVE, RETIRED = "frozen", "active", "retired"
# What a set's state may be: `draft` until it is frozen (LP-11i), then `frozen`; `active` from
# its first grading read; `retired` once the key holder retires it.
STATES = (DRAFT, FROZEN, ACTIVE, RETIRED)
GRADED = (FROZEN, ACTIVE)  # the states a grading read serves, and that a set retires from
# The rotation rule's limits (conflict 47: both required at freeze, no default): whole numbers,
# at most these, so a mistyped one is refused rather than kept (and fits an INTEGER column).
RUNS_CAP = 100_000
DAYS_CAP = 3_650
# A grader, as the chain names it: a grading key's principal (`key:` and the id the store mints).
_GRADER = re.compile(r"^key:[0-9a-f]{12}$")
_PACK_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
# What a frozen set's content holds of each case's attributes (the store's own, `ADDED`, never).
FROZEN_ATTRIBUTES = ("source", "stratum", "language", "author", "template")
# What a case's status may be (LP-11h): `draft` until a draw takes it, then `sampled`; once both
# first-pass labels are in, `agreed` or `disputed`; once a disputed case's adjudicator answers,
# `resolved` (a majority on every question) or `unresolved` (a three-way split on one).
SAMPLED, AGREED, DISPUTED, RESOLVED, UNRESOLVED = (
    "sampled",
    "agreed",
    "disputed",
    "resolved",
    "unresolved",
)
CASE_STATUSES = (DRAFT, SAMPLED, AGREED, DISPUTED, RESOLVED, UNRESOLVED)
# A drawn case's statuses once its labels settle it (LP-11i): a set freezes with these only.
SETTLED = (AGREED, RESOLVED)
# An assignment's round: a case's two blind first passes, then a disputed case's third label.
FIRST, ADJUDICATION = "first", "adjudication"
ROUNDS = (FIRST, ADJUDICATION)
# What `measure` seals as a set's aggregates: its coverage and its raters' agreement.
AGGREGATES_FORMAT = "mendari.evalset_aggregates/1"
# The events that store a set's aggregates, each chaining their commitment (`aggregates_ref`,
# LP-11p).
STORING = ("evalset.measured", "evalset.frozen")
# The one answer to an assignment that is unknown, another labeller's or closed (LP-11j: one 404).
NO_ASSIGNMENT = "no open assignment of this labeller has that id"
# A route acting for an organization (LP-11k): an unknown set and another organization's are one
# refusal, naming no id.
NO_SET = "no evaluation set of this organization has that id"
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
    the columns holding the ids each value is bound to (a label's own id too), and the column
    naming a row in what is printed beside its set (None: the set's id alone, or a label's id;
    never a case's id)."""

    columns: Mapping[str, Kind]
    set_id: str
    case_id: str | None
    shown: str | None
    label_id: str | None = None


# Every sealed column of the `evalset_` tables, by table (LP-11f): a rotation re-seals, its check
# opens, `verify` opens and the export copies exactly these. Every other column of an `evalset_`
# table is in the clear (ids, organizations, states, times, keyed hashes, commitments), and a
# test pins each column to one list or the other, so a sealed column a later step adds is never
# left out. Each table here has an `id` (its primary key, which a rotation pages by), an `org`
# and a `created_at`. A label's body (LP-11h) is bound to its set, its case and its own id.
# (`evalset_assignments` holds nothing sealed: ids, a round and times.)
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
    "evalset_labels": SealedTable(
        {"body": "label"},
        set_id="set_id",
        case_id="case_id",
        shown=None,
        label_id="id",
    ),
}


class GradingRead(NamedTuple):
    """What a grading read chained (`evalset.graded`), for its run's answer (LP-11l): the set,
    its commitment, how many cases the run receives, the run's number, and the runs the set has
    left after it."""

    set: str
    commitment: str
    n: int
    run: int
    runs_left: int


def case_ref(salt: bytes, set_id: str, case_id: str, value: Any, attributes: Any) -> str:
    """A case's commitment, as `evalset.case_added` chains it: SHA-256 of `CASE_REF_PREFIX`, the
    salt and the canonical JSON (`canonical_input`) of the case's set, id, input and attributes
    (its own, with what the store added, without the salt)."""
    body = {"set": set_id, "case": case_id, "input": value, "attributes": attributes}
    return hashlib.sha256(CASE_REF_PREFIX + salt + canonical_input(body).encode()).hexdigest()


def label_ref(
    salt: bytes, set_id: str, case_id: str, label_id: str, assignment_id: str, values: Any
) -> str:
    """A label's commitment, as `evalset.labelled` chains it (`label_ref.sha256`): SHA-256 of
    `LABEL_REF_PREFIX`, the salt and the canonical JSON of the label's set, case, own id,
    assignment and values. The salt keeps a value a guess could not confirm."""
    body = {
        "set": set_id,
        "case": case_id,
        "label": label_id,
        "assignment": assignment_id,
        "values": values,
    }
    return hashlib.sha256(LABEL_REF_PREFIX + salt + canonical_input(body).encode()).hexdigest()


def weight_ref(salt: bytes, set_id: str, case_id: str, weight: Any) -> str:
    """A drawn case's weight's commitment, as `evalset.sampled` chains it (`weight_refs`, by the
    case's keyed hash, LP-11p): SHA-256 of `WEIGHT_REF_PREFIX`, the salt and the canonical JSON
    of the case's set, id and weight. The salt, new at each draw, keeps one draw's commitments
    from being matched to another's, so the chain says nothing of which weights changed (which
    would show which cases share a stratum)."""
    body = {"set": set_id, "case": case_id, "weight": weight}
    return hashlib.sha256(WEIGHT_REF_PREFIX + salt + canonical_input(body).encode()).hexdigest()


def aggregates_ref(salt: bytes, set_id: str, aggregates: Any) -> str:
    """A set's stored aggregates' commitment, as `evalset.measured` and `evalset.frozen` chain it
    (`aggregates_ref`, LP-11p): SHA-256 of `AGGREGATES_REF_PREFIX`, the salt and the canonical
    JSON of the set's id and its aggregates."""
    body = {"set": set_id, "aggregates": aggregates}
    return hashlib.sha256(AGGREGATES_REF_PREFIX + salt + canonical_input(body).encode()).hexdigest()


def content_hash(content: Any) -> str:
    """A frozen set's content hash (LP-11i): the SHA-256 of its content (`CONTENT_FORMAT`) in
    canonical JSON (`canonical_input`). Kept only sealed, in the set's freeze record."""
    return hashlib.sha256(canonical_input(content).encode()).hexdigest()


def freeze_commitment(salt: bytes, hashed: str) -> str:
    """A frozen set's commitment, as `evalset.frozen` chains it: the SHA-256 of
    `FREEZE_PREFIX`, the salt and the content hash's 32 bytes. The salt keeps a guessed content
    from being confirmed against it."""
    return hashlib.sha256(FREEZE_PREFIX + salt + bytes.fromhex(hashed)).hexdigest()


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
    for number, line in enumerate(text.split("\n"), start=1):  # not splitlines: U+2028 etc.
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
    deployment's evaluation-set keys. Without keys (the feature off) it lists sets and counts
    their labelling's progress only: nothing sealed is opened. `clock` (UTC, the current time
    by default) is when a set freezes or retires and how old a frozen set is: a test moves it."""

    def __init__(
        self,
        store: AuditStore,
        keys: EvalsetKeys | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.keys = keys
        self.clock = clock or (lambda: datetime.now(UTC))
        self._index_epoch: str | None = None  # the index key id this store last recorded under

    def _ring(self) -> EvalsetKeys:
        if self.keys is None:
            raise MendariError(OFF)
        return self.keys

    def _stamp(self) -> str:
        """The clock's time as the store writes times (`_now`'s form)."""
        return self.clock().astimezone(UTC).isoformat(timespec="milliseconds")

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
        its id), with its index rows, and its assignments and labels (LP-11h: an unresolved case
        is removed so its set can freeze); `evalset.case_removed` is chained."""
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
            db.all("DELETE FROM evalset_labels WHERE case_id = ?", (row["id"],))
            db.all("DELETE FROM evalset_assignments WHERE case_id = ?", (row["id"],))
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

    # -- blind labelling (LP-11h) --------------------------------------------------------------

    def sample(
        self,
        set_id: str,
        *,
        per_stratum: int | None = None,
        cases: int | None = None,
        seed: int = 0,
        org: str | None = None,
    ) -> dict[str, Any]:
        """Draw cases of a draft set to label (`mendari evalset sample`, and its curators' route,
        LP-11k, for `org`: another organization's set is refused as an unknown one is,
        `NoEvalset`, in the write's own transaction): `per_stratum` more from
        each stratum (all that are left of a smaller one), or `cases` more in all, shared out
        among the strata in proportion to the cases left in each (largest remainders, ties by
        stratum). The draw is seeded (`seed`) and runs over the cases in id order, the strata in
        name order, so the same set and seed draw the same cases. Which cases were drawn before
        is what the set's latest draw committed to (LP-11p; each case's status, for a draw from
        before those commitments), so a status edited behind the store's back is never taken
        for a draw. Each drawn case becomes `sampled`, and every drawn case, of this draw and
        the earlier ones, is re-sealed with its weight N_h / n_h (the stratum's cases over those
        drawn from it so far) and a new 32-byte salt, beside its attributes, outside what its
        own commitment covers. `evalset.sampled` is chained with how many were drawn, the seed
        and each drawn case's weight's commitment by its keyed hash (`weight_refs`,
        `weight_ref`): counts and commitments only, never a stratum or a weight, and every
        commitment new, so the chain does not show which weights changed. The set's id, how
        many were drawn now and in all, and the seed."""
        keys = self._ring()
        _check_set_id(set_id, org)
        if (per_stratum is None) == (cases is None):
            raise MendariError(
                "give how many cases to draw from each stratum, or how many in all: one of the two"
            )
        count = per_stratum if per_stratum is not None else cases
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise MendariError("a draw is of 1 case or more")
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise MendariError("a seed is a whole number")
        with self.store._db.session(exclusive=True) as db:
            self.store._evalset_ready(db, keys)
            head = _draft(
                set_id, db.one("SELECT org, state FROM evalset_sets WHERE id = ?", (set_id,)), org
            )
            rows = db.all(
                "SELECT id, case_hash, status, attributes FROM evalset_cases WHERE set_id = ? "
                "ORDER BY id",
                (set_id,),
            )
            if not rows:
                raise MendariError(f"evaluation set {set_id} holds no case to draw")
            bodies = {
                str(row["id"]): keys.open(
                    "attributes", str(row["attributes"]), set_id, str(row["id"])
                )[0]
                for row in rows
            }
            hashes = {str(row["id"]): str(row["case_hash"]) for row in rows}
            committed = _latest_draw(db, set_id)
            drawn_before = (
                {str(row["id"]) for row in rows if row["status"] != DRAFT}
                if committed is None  # a draw from before its commitments: the statuses say
                else {case for case, hashed in hashes.items() if hashed in committed}
            )
            strata: dict[str, list[str]] = {}
            for row in rows:
                stratum = str(bodies[str(row["id"])]["attributes"]["stratum"])
                strata.setdefault(stratum, []).append(str(row["id"]))
            left = {
                h: [c for c in ids if c not in drawn_before] for h, ids in sorted(strata.items())
            }
            if per_stratum is not None:
                take = {h: min(per_stratum, len(ids)) for h, ids in left.items()}
            else:
                available = sum(len(ids) for ids in left.values())
                if count > available:
                    raise MendariError(
                        f"evaluation set {set_id} has {available} case(s) left to draw, not {count}"
                    )
                take = _shared_out(count, {h: len(ids) for h, ids in left.items()})
            rng = random.Random(seed)
            drawn = [case for h, ids in left.items() for case in rng.sample(ids, take[h])]
            if not drawn:
                raise MendariError(f"every case of evaluation set {set_id} is drawn already")
            for start in range(0, len(drawn), _INSERT_BATCH):
                batch = drawn[start : start + _INSERT_BATCH]
                marks = ", ".join("?" for _ in batch)
                db.all(
                    f"UPDATE evalset_cases SET status = ? WHERE id IN ({marks})",
                    (SAMPLED, *batch),
                )
            every = drawn_before | set(drawn)
            weights: dict[str, str] = {}  # each drawn case's weight's commitment, by its hash
            for ids in strata.values():
                chosen = [case for case in ids if case in every]
                if not chosen:
                    continue
                weight = len(ids) / len(chosen)
                for case in chosen:
                    salt = secrets.token_bytes(32)
                    body = {
                        **bodies[case],
                        "weight": weight,
                        "weight_salt": base64.b64encode(salt).decode(),
                    }
                    resealed = keys.seal("attributes", body, set_id, case)
                    db.all("UPDATE evalset_cases SET attributes = ? WHERE id = ?", (resealed, case))
                    weights[hashes[case]] = weight_ref(salt, set_id, case, weight)
            payload = {
                "set": set_id,
                "n": len(drawn),
                "seed": seed,
                "weight_refs": dict(sorted(weights.items())),
            }
            chained = self.store._bound("evalset.sampled", payload, str(head["org"]))
            self.store._append(db, f"evalset:{set_id}", "evalset.sampled", chained)
        return {"set": set_id, "drawn": len(drawn), "sampled": len(every), "seed": seed}

    def assign(
        self, set_id: str, labellers: Sequence[str], *, seed: int = 0, org: str | None = None
    ) -> dict[str, Any]:
        """Give each sampled case of a draft set its two blind first-pass labellers
        (`mendari evalset assign`), from `labellers` (user ids): two different people a case,
        the pair of them that has shared the fewest cases, then the one whose busier member has
        the fewest, counting the assignments the set already has (while the same labellers are
        named, pairs differ by one case at most and labellers by two); deterministic for a
        seed. Each labeller must hold the `labeller` role on a membership in force in the set's
        organization, and none may manage hidden sets there (`accounts.CURATING`): either
        refuses the run. Cases are chosen by status (`sampled` here, `disputed` to adjudicate),
        clear text: so first, before anything is written and opening nothing, the set's rows
        are checked against what the chain recorded (`_shape_mismatches`, LP-11p: each case's
        labels, and a status its draw and its labels' rounds allow), and a set where one does
        not hold is refused, counted and naming no case. An open assignment whose membership no
        longer holds (ended, or no longer a labeller's) is withdrawn first, and its case given
        to another labeller here. `evalset.assigned` is chained for each. The set's id, how many
        were assigned and withdrawn, and each new assignment: its case by its hash, its id, its
        labeller and its round. Its curators' route (LP-11k) gives `org`, the organization it
        acts for: another organization's set is refused as an unknown one is (`NoEvalset`)."""
        return self._assign(set_id, labellers, seed, FIRST, org)

    def adjudicate(
        self, set_id: str, labellers: Sequence[str], *, seed: int = 0, org: str | None = None
    ) -> dict[str, Any]:
        """Give each disputed case of a draft set a third, blind labeller who is neither of its
        first two (`mendari evalset adjudicate`, and its curators' route for `org`, LP-11k), as
        `assign` does: the same refusals (a set whose rows its chain does not allow among them:
        a case is `disputed` only with two first-pass labels and no adjudicator's), the same
        withdrawals, balanced across `labellers`. The adjudicator labels from the input alone,
        and the majority on each question settles the case, if its first two labels differ
        (else nothing settles it, and the status is found, `given_statuses`)."""
        return self._assign(set_id, labellers, seed, ADJUDICATION, org)

    def _assign(
        self, set_id: str, labellers: Sequence[str], seed: int, round_: str, org: str | None
    ) -> dict[str, Any]:
        keys = self._ring()
        _check_set_id(set_id, org)
        routed = org is not None  # a curator's route: its answers name no case (LP-11k)
        users = _user_ids(labellers, at_least=2 if round_ == FIRST else 1)
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise MendariError("a seed is a whole number")
        waiting = SAMPLED if round_ == FIRST else DISPUTED
        with self.store._db.session(exclusive=True) as db:
            self.store._evalset_ready(db, keys)
            head = _draft(
                set_id, db.one("SELECT org, state FROM evalset_sets WHERE id = ?", (set_id,)), org
            )
            org = str(head["org"])
            pool = {user: _labeller_in(db, org, user) for user in users}
            # The cases are chosen by status (`waiting`), clear text: first the set's rows are
            # checked against what the chain recorded, opening nothing (LP-11p, I13), so a
            # status its draw and labels do not allow never gives a case to a labeller.
            found = _shape_mismatches(
                db.all(
                    "SELECT id, case_hash, status FROM evalset_cases WHERE set_id = ? ORDER BY id",
                    (set_id,),
                ),
                db.all(
                    "SELECT id, case_id FROM evalset_labels WHERE set_id = ? ORDER BY id",
                    (set_id,),
                ),
                _chain(db, set_id),
            )
            if found:
                raise _refused(set_id, Counter(what for _, what in found), "assigned")
            withdrawn = _withdraw(db, set_id, org, round_)
            if not db.one(
                "SELECT id FROM evalset_cases WHERE set_id = ? AND status <> ? LIMIT 1",
                (set_id, DRAFT),
            ):
                raise MendariError(
                    f"no case of evaluation set {set_id} is drawn: draw them first "
                    "(`mendari evalset sample`)"
                )
            cases = db.all(
                "SELECT id, case_hash FROM evalset_cases WHERE set_id = ? AND status = ? "
                "ORDER BY id",
                (set_id, waiting),
            )
            held = _held(db, set_id)
            rng = random.Random(seed)
            rank = {user: place for place, user in enumerate(rng.sample(users, len(users)))}
            order = list(cases)
            rng.shuffle(order)
            load: Counter[str] = Counter()  # every round's assignments held, by labeller
            load_here: Counter[str] = Counter()  # this round's
            pairs: Counter[tuple[str, str]] = Counter()
            for (_, round_held), people in held.items():
                load.update(people)
                if round_held == round_:
                    load_here.update(people)
                if round_held == FIRST and len(people) == 2:
                    pairs[_pair(*people)] += 1
            made: list[tuple[str, str, str, str]] = []  # assignment, case, its hash, labeller
            for row in order:
                case_id = str(row["id"])
                first = held.get((case_id, FIRST), [])
                have = held.get((case_id, round_), [])
                need = (2 if round_ == FIRST else 1) - len(have)
                if need <= 0:
                    continue
                barred = set(have) | (set(first) if round_ == ADJUDICATION else set())
                candidates = [user for user in users if user not in barred]
                if len(candidates) < need:
                    who = "two different labellers" if round_ == FIRST else "a third labeller"
                    case = (
                        f"a {'drawn' if round_ == FIRST else 'disputed'} case"
                        if routed
                        else f"case {row['case_hash']}"
                    )
                    raise MendariError(
                        f"{case} needs {who}, neither of those it has: give more labellers"
                    )
                if need == 2:
                    # The pair that has shared the fewest cases first (pairs stay within one
                    # case of each other), then the one whose busier member has the fewest
                    # (labellers within two): the other way round, a pool of six or more
                    # gives the same pair case after case while others share none.
                    chosen: tuple[str, ...] = min(
                        combinations(candidates, 2),
                        key=lambda p: (
                            pairs[_pair(*p)],
                            max(load_here[p[0]], load_here[p[1]]),
                            load_here[p[0]] + load_here[p[1]],
                            sorted((rank[p[0]], rank[p[1]])),
                        ),
                    )
                else:
                    other = have[0] if round_ == FIRST else None
                    chosen = (
                        min(
                            candidates,
                            key=lambda u: (
                                load_here[u],
                                pairs[_pair(other, u)] if other else 0,
                                load[u],
                                rank[u],
                            ),
                        ),
                    )
                for user in chosen:
                    made.append((str(uuid.uuid4()), case_id, str(row["case_hash"]), user))
                    load[user] += 1
                    load_here[user] += 1
                    have = [*have, user]
                held[(case_id, round_)] = have
                if round_ == FIRST and len(have) == 2:
                    pairs[_pair(*have)] += 1
            now = _now()
            _insert(
                db,
                "evalset_assignments",
                (
                    "id",
                    "org",
                    "set_id",
                    "case_id",
                    "user_id",
                    "membership_id",
                    "round",
                    "created_at",
                ),
                [(a, org, set_id, c, u, pool[u], round_, now) for a, c, _, u in made],
            )
            entries = [
                (
                    f"evalset:{set_id}",
                    self.store._bound(
                        "evalset.assigned",
                        {
                            "set": set_id,
                            "case": hashed,
                            "assignment": assignment,
                            "labeller": user,
                            "round": round_,
                        },
                        org,
                    ),
                )
                for assignment, _, hashed, user in made
            ]
            self.store._append_many(db, "evalset.assigned", entries)
        return {
            "set": set_id,
            "round": round_,
            "assigned": len(made),
            "withdrawn": withdrawn,
            "assignments": [
                {"case": hashed, "assignment": assignment, "labeller": user, "round": round_}
                for assignment, _, hashed, user in made
            ],
        }

    def next_case(self, user_id: str, *, org: str) -> dict[str, Any] | None:
        """What a labeller labels next (`GET /api/labelling/next`, LP-11j): their oldest open
        assignment in a draft set of `org`, the organization they act for, while the membership
        it was made on is in force and holds the `labeller` role and no role that manages hidden
        sets. Only the case's input is opened, and `evalset.case_read` is chained in the same
        transaction, before anything is returned: the assignment's id, the set's questions with
        their labels (from its header, which is not a case's) and the input. Never an
        attribute, the intended label, another label or a model's output. None: nothing waits
        for them."""
        keys = self._ring()
        _check_user_id(user_id)
        with self.store._db.session(exclusive=True) as db:
            self.store._evalset_ready(db, keys)
            membership = _labeller_membership(db, org, user_id)
            if membership is None:
                return None
            row = db.one(
                "SELECT a.id, a.set_id, a.case_id, s.header, c.case_hash, c.input "
                "FROM evalset_assignments a JOIN evalset_sets s ON s.id = a.set_id "
                "JOIN evalset_cases c ON c.id = a.case_id "
                "WHERE a.user_id = ? AND a.org = ? AND a.membership_id = ? "
                "AND a.ended_at IS NULL AND s.state = ? AND s.org = a.org "
                "AND c.set_id = a.set_id "
                "AND NOT EXISTS (SELECT 1 FROM evalset_labels l WHERE l.assignment_id = a.id) "
                "ORDER BY a.created_at, a.id LIMIT 1",
                (user_id, org, str(membership["id"]), DRAFT),
            )
            if row is None:
                return None
            set_id, case_id = str(row["set_id"]), str(row["case_id"])
            header, _ = keys.open("header", str(row["header"]), set_id)
            value, _ = keys.open("input", str(row["input"]), set_id, case_id)
            payload = {
                "set": set_id,
                "case": str(row["case_hash"]),
                "assignment": str(row["id"]),
                "labeller": user_id,
            }
            chained = self.store._bound("evalset.case_read", payload, org)
            self.store._append(db, f"evalset:{set_id}", "evalset.case_read", chained)
        return {"assignment": str(row["id"]), "questions": header["questions"], "input": value}

    def record_label(
        self, assignment_id: str, user_id: str, values: Mapping[str, Any], *, org: str
    ) -> dict[str, Any]:
        """A labeller's answer to one assignment (`POST /api/labelling/{id}`, LP-11j), for `org`,
        the organization they act for. Refused with one message (`NoAssignment`, `NO_ASSIGNMENT`)
        whether the assignment is unknown, another labeller's, another organization's, closed
        (answered, or withdrawn), in a set no longer a draft, or on a membership no longer in
        force as a labeller's; then every question of the set must be answered with one of its
        labels (a `LabelError` names the question, never the value). One submission per
        assignment, never changed: the values are sealed with a 32-byte salt, bound to the
        label's own id, and `evalset.labelled` is chained with the label's commitment. A case's
        second first-pass label settles it `agreed` or `disputed`; its adjudicator's label,
        `resolved` or `unresolved` (`_settle`). The answer is how many cases are left for the
        labeller, never how their label compares."""
        keys = self._ring()
        if not (
            _is_uuid(assignment_id)
            and isinstance(user_id, str)
            and accounts.USER_ID.fullmatch(user_id)
        ):
            raise NoAssignment(NO_ASSIGNMENT)
        with self.store._db.session(exclusive=True) as db:
            self.store._evalset_ready(db, keys)
            row = db.one(
                "SELECT a.id, a.org, a.set_id, a.case_id, a.user_id, a.membership_id, a.round, "
                "a.ended_at, s.state, s.header, c.case_hash FROM evalset_assignments a "
                "JOIN evalset_sets s ON s.id = a.set_id "
                "JOIN evalset_cases c ON c.id = a.case_id WHERE a.id = ?",
                (assignment_id,),
            )
            if (
                row is None
                or row["user_id"] != user_id
                or row["org"] != org
                or row["ended_at"] is not None
                or row["state"] != DRAFT
                or db.one("SELECT id FROM evalset_labels WHERE assignment_id = ?", (assignment_id,))
            ):
                raise NoAssignment(NO_ASSIGNMENT)
            org, set_id, case_id = str(row["org"]), str(row["set_id"]), str(row["case_id"])
            membership = _labeller_membership(db, org, user_id)
            if membership is None or str(membership["id"]) != row["membership_id"]:
                raise NoAssignment(NO_ASSIGNMENT)
            header, _ = keys.open("header", str(row["header"]), set_id)
            problem = _label_problem(values, header["questions"])
            if problem is not None:
                raise LabelError(problem)
            answered = {question: values[question] for question in header["questions"]}
            label_id = str(uuid.uuid4())
            salt = secrets.token_bytes(32)
            body = {"salt": base64.b64encode(salt).decode(), "values": answered}
            now = _now()
            db.all(
                "INSERT INTO evalset_labels (id, org, set_id, case_id, assignment_id, body, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    label_id,
                    org,
                    set_id,
                    case_id,
                    assignment_id,
                    keys.seal("label", body, set_id, case_id, label_id),
                    now,
                ),
            )
            db.all("UPDATE evalset_assignments SET ended_at = ? WHERE id = ?", (now, assignment_id))
            ref = label_ref(salt, set_id, case_id, label_id, assignment_id, answered)
            payload = {
                "set": set_id,
                "case": str(row["case_hash"]),
                "label_ref": {"id": label_id, "sha256": ref},
                "labeller": user_id,
                "round": str(row["round"]),
            }
            chained = self.store._bound("evalset.labelled", payload, org)
            self.store._append(db, f"evalset:{set_id}", "evalset.labelled", chained)
            _settle(db, keys, set_id, case_id)
            remaining = db.one(
                "SELECT count(*) AS n FROM evalset_assignments a "
                "JOIN evalset_sets s ON s.id = a.set_id WHERE a.user_id = ? AND a.org = ? "
                "AND a.membership_id = ? AND a.ended_at IS NULL AND s.state = ?",
                (user_id, org, str(membership["id"]), DRAFT),
            )
        return {"remaining": int(remaining["n"]) if remaining else 0}

    def labelling(self, user_id: str, *, org: str) -> dict[str, Any]:
        """A labeller's own work in `org`, the organization they act for (`GET /api/labelling`,
        LP-11j): each set they hold an assignment in, oldest first, with how many of their cases
        wait there (`open`: as `next_case` serves them, on the labeller's membership in force,
        in a set still a draft) and how many they labelled; and how many wait in all
        (`remaining`, as `record_label` answers). Nothing for anyone who labels nothing in `org`
        now (no labeller's membership in force there, or one that manages its sets). Ids and
        counts only: nothing sealed is opened, so nothing is chained and no key is needed."""
        _check_user_id(user_id)
        with self.store._db.session(exclusive=False) as db:
            membership = _labeller_membership(db, org, user_id)
            if membership is None:
                return {"sets": [], "remaining": 0}
            rows = db.all(
                "SELECT a.set_id, a.membership_id, a.ended_at, s.state, l.id AS label "
                "FROM evalset_assignments a JOIN evalset_sets s ON s.id = a.set_id "
                "LEFT JOIN evalset_labels l ON l.assignment_id = a.id "
                "WHERE a.user_id = ? AND a.org = ? AND s.org = a.org ORDER BY a.created_at, a.id",
                (user_id, org),
            )
        counted: dict[str, dict[str, int]] = {}
        for row in rows:
            held = counted.setdefault(str(row["set_id"]), {"open": 0, "labelled": 0})
            if row["label"] is not None:
                held["labelled"] += 1
            elif (
                row["ended_at"] is None
                and row["state"] == DRAFT
                and str(row["membership_id"]) == str(membership["id"])
            ):
                held["open"] += 1
        sets = [{"set": set_id, **held} for set_id, held in counted.items() if any(held.values())]
        return {"sets": sets, "remaining": sum(held["open"] for held in sets)}

    def assignment_set(self, assignment_id: str, user_id: str, *, org: str) -> str | None:
        """The set of one of `user_id`'s own assignments in `org`, open or answered: what the
        security log names of a labelling route's work (set ids only, LP-11j). None for an id
        that is unknown, another labeller's or another organization's. Ids only."""
        if not _is_uuid(assignment_id):
            return None
        with self.store._db.session(exclusive=False) as db:
            row = db.one(
                "SELECT set_id FROM evalset_assignments WHERE id = ? AND user_id = ? AND org = ?",
                (assignment_id, user_id, org),
            )
        return None if row is None else str(row["set_id"])

    def _refuse_unless_changed(self, keys: EvalsetKeys, set_id: str, done: str) -> NoReturn:
        """Refuse a draft set whose rows, as `freeze` or `measure` read them outside the chain's
        lock (`done`: what was not done), are not those the commitments give (`_mismatches`).
        Its rows and chain are read again under the lock, where no write of the store lands
        between the reads: if they still do not hold, the set is refused as they are, counted
        by what and naming no case (`_refused`); if they do, a draw, a label or a removal landed
        while the set was read, and it changed while it was measured (or frozen): run it again,
        never a refusal that sends the operator looking for a tamper (LP-11p). Writes
        nothing."""
        with self.store._db.session(exclusive=True) as db:
            _draft(set_id, db.one("SELECT state FROM evalset_sets WHERE id = ?", (set_id,)))
            cases, labels = _case_rows(db, set_id), _label_rows(db, set_id)
            chain = _chain(db, set_id)
        counted = Counter(what for _, what in _mismatches(keys, set_id, cases, labels, chain))
        if counted:
            raise _refused(set_id, counted, done)
        raise MendariError(f"evaluation set {set_id} changed while it was {done}: run it again")

    def measure(
        self,
        set_id: str,
        *,
        pack: Pack | None = None,
        seed: int = 0,
        resamples: int = 2000,
    ) -> dict[str, Any]:
        """The aggregates of a draft set's labelling (`mendari evalset agreement` and
        `coverage`): the coverage report (`evals/coverage.py`) of its drawn cases, each with its
        settled labels (an agreed case's, or a resolved one's majority; none while it waits)
        and those its pack derives from them; and its raters' agreement
        (`evals/rater_agreement.py`, seeded) over the first-pass labels of the cases both
        labellers answered, raters by user id. Each label is one the chain recorded, in the
        round and by the labeller its `evalset.labelled` chained (`_as_chained`, LP-11p), never
        as an assignment row says now. The pack is the set's (its header's id, resolved here)
        unless `pack` is given; one whose questions, labels or critical labels differ from the
        header's is refused, and so is a set holding a case whose labels are not those its chain
        recorded, whose status its labels and draw do not give, or whose weight its draw's
        commitment does not cover (LP-11p, `_mismatches`: the aggregates would count it as it
        stands), once read again under the chain's lock (`_refuse_unless_changed`: a set that
        changed while it was read is "run it again"). Inputs, attributes and labels are opened
        and the statistics computed outside the chain's lock; then, in one transaction, if the
        set's drawn cases, its labels and what its chain says of them are still those that were
        read, the aggregates are sealed as the set's with a new 32-byte salt and
        `evalset.measured` is chained (how many cases, and the aggregates' commitment,
        `aggregates_ref`). The aggregates: counts and coefficients by question and label, the
        mix of languages, sources and strata, never a case."""
        keys = self._ring()
        _check_set_id(set_id)
        if not isinstance(resamples, int) or isinstance(resamples, bool) or resamples < 1:
            raise MendariError("a bootstrap takes 1 resample or more")
        with self.store._db.session(exclusive=False) as db:
            head = _draft(
                set_id, db.one("SELECT state, header FROM evalset_sets WHERE id = ?", (set_id,))
            )
            every = _case_rows(db, set_id)
            label_rows = _label_rows(db, set_id)
            chain = _chain(db, set_id)
        header, _ = keys.open("header", str(head["header"]), set_id)
        if pack is None:
            from mendari.core.registry import resolve_pack

            pack = resolve_pack(str(header["pack"]))
        problem = pack_problem(pack, header)
        if problem is not None:
            raise MendariError(problem)
        if _mismatches(keys, set_id, every, label_rows, chain):
            self._refuse_unless_changed(keys, set_id, "measured")
        labelled = _as_chained(label_rows, chain)
        rows = [row for row in every if row["status"] != DRAFT]
        if not rows:
            raise MendariError(
                f"no case of evaluation set {set_id} is drawn: draw them first "
                "(`mendari evalset sample`)"
            )
        read = (_measured_rows(rows, labelled), chain)
        aggregates = _aggregates(keys, pack, set_id, rows, labelled, seed, resamples)
        with self.store._db.session(exclusive=True) as db:
            self.store._evalset_ready(db, keys)
            again = _draft(
                set_id, db.one("SELECT org, state FROM evalset_sets WHERE id = ?", (set_id,))
            )
            now_rows = db.all(
                "SELECT id, status FROM evalset_cases WHERE set_id = ? AND status <> ? ORDER BY id",
                (set_id, DRAFT),
            )
            now_labels = db.all(
                "SELECT id FROM evalset_labels WHERE set_id = ? ORDER BY id", (set_id,)
            )
            if (_measured_rows(now_rows, now_labels), _chain(db, set_id)) != read:
                raise MendariError(
                    f"evaluation set {set_id} changed while it was measured: run it again"
                )
            sealed, ref = _sealed_aggregates(keys, set_id, aggregates)
            db.all("UPDATE evalset_sets SET aggregates = ? WHERE id = ?", (sealed, set_id))
            payload = {"set": set_id, "n": len(rows), "aggregates_ref": ref}
            chained = self.store._bound("evalset.measured", payload, str(again["org"]))
            self.store._append(db, f"evalset:{set_id}", "evalset.measured", chained)
        return aggregates

    # -- the lifecycle (LP-11i) ----------------------------------------------------------------

    def freeze(
        self,
        set_id: str,
        *,
        max_runs: int,
        max_age_days: int,
        drop_unsampled: bool = False,
        pack: Pack | None = None,
        seed: int = 0,
        resamples: int = 2000,
    ) -> dict[str, Any]:
        """Freeze a draft set (`mendari evalset freeze`, the key holder's only): its content and
        its rotation rule fixed, committed to and chained. Refused while a case's labels are not
        those its chain recorded (each in its chained round, `_as_chained`: an assignment row's
        round is never read), its status is not the one those labels and the set's latest draw
        give it, or a drawn case's weight is not the one that draw's commitment covers (LP-11p,
        `_mismatches`: what freezing would commit to as it stands; a draw from before those
        commitments has its weights taken as they are), naming no case, once read again under
        the chain's lock (`_refuse_unless_changed`: a set that changed while it was read is
        "run it again"); while a case is not drawn (unless `drop_unsampled`: each such case is
        removed, with its index rows, and `evalset.case_removed` chained for it), or a drawn
        one is unlabelled, disputed or unresolved; and without both limits
        (`max_runs` grading runs, `max_age_days` days from now, by `clock`, which time the
        content holds: no default, conflict 47). The pack is the set's, as `measure` finds it,
        for the labels it derives from the final member labels (`derive_answer`). Inputs,
        attributes and labels are opened, the content hashed (`content_hash`,
        `_frozen_content`) and the aggregates computed (the coverage of the final labels and the
        first-pass agreement, as `measure` stores them) outside the chain's lock; then, in one
        transaction, if the set's cases, its labels and what its chain says of them are still
        those that were read, the set becomes `frozen`: its freeze record (the salt, the content
        hash and the derived labels) and aggregates sealed (the aggregates with a new 32-byte
        salt), its commitment, limits and time kept, and `evalset.frozen` chained with the
        aggregates' commitment (`aggregates_ref`). The set's id, state, time, commitment, how
        many cases it holds and how many were dropped, its limits, and the distinct positives
        of each critical label (`critical`, which names the pack's label words: conflict 48,
        never chained) with the coverage report's warnings; a critical label under
        `result.MIN_POSITIVES` warns, never refuses. Never the content hash or the salt."""
        keys = self._ring()
        _check_set_id(set_id)
        _check_limits(max_runs, max_age_days)
        if not isinstance(resamples, int) or isinstance(resamples, bool) or resamples < 1:
            raise MendariError("a bootstrap takes 1 resample or more")
        with self.store._db.session(exclusive=False) as db:
            head = _draft(
                set_id, db.one("SELECT state, header FROM evalset_sets WHERE id = ?", (set_id,))
            )
            rows = _case_rows(db, set_id)
            label_rows = _label_rows(db, set_id)
            chain = _chain(db, set_id)
        header, _ = keys.open("header", str(head["header"]), set_id)
        if pack is None:
            from mendari.core.registry import resolve_pack

            pack = resolve_pack(str(header["pack"]))
        problem = pack_problem(pack, header)
        if problem is not None:
            raise MendariError(problem)
        if _mismatches(keys, set_id, rows, label_rows, chain):
            self._refuse_unless_changed(keys, set_id, "frozen")
        labelled = _as_chained(label_rows, chain)
        undrawn = [row for row in rows if row["status"] == DRAFT]
        if undrawn and not drop_unsampled:
            raise MendariError(
                f"evaluation set {set_id} has {len(undrawn)} case(s) not drawn: draw and label "
                "them (`mendari evalset sample`), or leave them out of the frozen set "
                "(--drop-unsampled removes them)"
            )
        kept = [row for row in rows if row["status"] != DRAFT]
        if not kept:
            raise MendariError(f"evaluation set {set_id} has no drawn case to freeze")
        waiting = Counter(str(row["status"]) for row in kept if row["status"] not in SETTLED)
        if waiting:
            raise MendariError(_unsettled(set_id, waiting))
        frozen_at = self._stamp()
        limits = (max_runs, max_age_days, frozen_at)
        derived = _derived(keys, pack, set_id, kept, labelled)
        content, _ = _frozen_content(keys, set_id, header, limits, kept, labelled, derived)
        hashed = content_hash(content)
        salt = secrets.token_bytes(32)
        commitment = freeze_commitment(salt, hashed)
        record = {
            "salt": base64.b64encode(salt).decode(),
            "content_hash": hashed,
            "derived": derived,
        }
        aggregates = _aggregates(keys, pack, set_id, kept, labelled, seed, resamples)
        sealed_aggregates, aggregates_commitment = _sealed_aggregates(keys, set_id, aggregates)
        read = (_measured_rows(rows, labelled), chain)
        dropped = [str(row["id"]) for row in undrawn]
        with self.store._db.session(exclusive=True) as db:
            self.store._evalset_ready(db, keys)
            again = _draft(
                set_id, db.one("SELECT org, state FROM evalset_sets WHERE id = ?", (set_id,))
            )
            org = str(again["org"])
            now_rows = db.all(
                "SELECT id, status FROM evalset_cases WHERE set_id = ? ORDER BY id", (set_id,)
            )
            now_labels = db.all(
                "SELECT id FROM evalset_labels WHERE set_id = ? ORDER BY id", (set_id,)
            )
            if (_measured_rows(now_rows, now_labels), _chain(db, set_id)) != read:
                raise MendariError(
                    f"evaluation set {set_id} changed while it was frozen: run it again"
                )
            for start in range(0, len(dropped), _INSERT_BATCH):
                batch = dropped[start : start + _INSERT_BATCH]
                marks = ", ".join("?" for _ in batch)
                for table, column in (
                    ("evalset_index", "case_id"),
                    ("evalset_labels", "case_id"),
                    ("evalset_assignments", "case_id"),
                    ("evalset_cases", "id"),
                ):
                    db.all(f"DELETE FROM {table} WHERE {column} IN ({marks})", batch)
            removed = [
                (
                    f"evalset:{set_id}",
                    self.store._bound(
                        "evalset.case_removed", {"set": set_id, "case": str(row["case_hash"])}, org
                    ),
                )
                for row in undrawn
            ]
            self.store._append_many(db, "evalset.case_removed", removed)
            db.all(
                "UPDATE evalset_sets SET state = ?, frozen_at = ?, commitment = ?, "
                "freeze_record = ?, max_runs = ?, max_age_days = ?, aggregates = ? "
                "WHERE id = ? AND state = ?",
                (
                    FROZEN,
                    frozen_at,
                    commitment,
                    keys.seal("freeze", record, set_id),
                    max_runs,
                    max_age_days,
                    sealed_aggregates,
                    set_id,
                    DRAFT,
                ),
            )
            payload = {
                "set": set_id,
                "commitment": commitment,
                "n": len(kept),
                "max_runs": max_runs,
                "max_age_days": max_age_days,
                "aggregates_ref": aggregates_commitment,
            }
            chained = self.store._bound("evalset.frozen", payload, org)
            self.store._append(db, f"evalset:{set_id}", "evalset.frozen", chained)
        covered = aggregates["coverage"]
        return {
            "set": set_id,
            "state": FROZEN,
            "frozen_at": frozen_at,
            "commitment": commitment,
            "n": len(kept),
            "dropped": len(dropped),
            "max_runs": max_runs,
            "max_age_days": max_age_days,
            "critical": covered["critical"],
            "warnings": covered["warnings"],
        }

    def grade_read(
        self,
        set_id: str,
        grader: str,
        pack_digest: str,
        run: Callable[[Dataset], T],
        *,
        receipt: Callable[[GradingRead], None] | None = None,
    ) -> T:
        """The one read of a frozen or active set's cases (the grading route's run calls it, in
        the process, LP-11l; no command does). `grader` is the grading key's principal
        (`key:<id>`): a live key of role `grading` of the set's organization, or the set is
        named as unknown (another organization's, a revoked or expired key, any other key).
        In one exclusive transaction: the set is frozen or active, and its chain holds no
        `evalset.retired` (a row edited back from `retired` grades nothing); the rotation rule
        holds (fewer chained `evalset.graded` than its `max_runs`, and less than `max_age_days`
        since `frozen_at`, by `clock`); its rows still give the content its commitment covers
        (else it is refused naming no case, or naming the keys it lacks when a value is sealed
        under a key this database knows and the ring does not hold); a frozen set becomes
        `active`, `evalset.activated`
        chained; and `evalset.graded {set, org, commitment, grader, pack_digest, n, run}` is
        chained. Only once that transaction commits does `run` receive the cases, as an
        in-memory dataset (each case by its keyed hash, with its input, final labels and frozen
        attributes; its own random case key, no file, no digest), so concurrent reads never
        exceed `max_runs` and a run whose read was not chained never sees a case. `receipt`, if
        given, is told what was chained (`GradingRead`) once it is, before `run` is called. Only
        what `run` returns comes back; nothing is stored. A run that fails still counts. Each
        refusal is a `GradingRefused` with its fixed reason: `no_set` (unknown, another
        organization's, a key that is not a live grading key of its organization),
        `not_gradable` (a draft or retired set), `no_runs_left`, `expired`, `set_changed` and
        `set_unavailable` (a value under a key the ring lacks)."""
        keys = self._ring()
        _check_set_id(set_id)
        if not (isinstance(grader, str) and _GRADER.fullmatch(grader)):
            raise MendariError("a grader is a grading key, named key: and its id")
        if not (isinstance(pack_digest, str) and _PACK_DIGEST.fullmatch(pack_digest)):
            raise MendariError("a pack digest is sha256: and 64 hex digits")
        from mendari.evals.harness import Dataset

        with self.store._db.session(exclusive=True) as db:
            self.store._evalset_ready(db, keys)
            head = db.one(f"SELECT {_FROZEN_COLUMNS} FROM evalset_sets WHERE id = ?", (set_id,))
            key = db.one(
                "SELECT org FROM api_keys WHERE id = ? AND role = ? AND revoked_at IS NULL "
                "AND expires_at > ?",
                (grader.removeprefix("key:"), GRADING_ROLE, _now()),
            )
            if head is None or key is None or str(key["org"]) != str(head["org"]):
                raise GradingRefused(
                    "no_set", f"no evaluation set {set_id} is graded with {grader}"
                )
            org, state = str(head["org"]), str(head["state"])
            if _retired(db, set_id):
                state = RETIRED  # the chain's word, whatever the row says
            if state not in GRADED:
                raise GradingRefused(
                    "not_gradable",
                    f"evaluation set {set_id} is {state}: only a frozen or active set is graded",
                )
            max_runs, max_age_days, frozen_at = _rule(set_id, head)
            used = _runs(db, set_id)
            if used >= max_runs:
                raise GradingRefused(
                    "no_runs_left",
                    f"evaluation set {set_id} has had its {max_runs} grading run(s): retire it "
                    "(`mendari evalset retire`) and freeze another",
                )
            if self.clock() - frozen_at >= timedelta(days=max_age_days):
                raise GradingRefused(
                    "expired",
                    f"evaluation set {set_id} was frozen {max_age_days} day(s) ago or more: "
                    "retire it (`mendari evalset retire`) and freeze another",
                )
            records = _intact(db, keys, set_id, head)
            if state == FROZEN:
                db.all(
                    "UPDATE evalset_sets SET state = ? WHERE id = ? AND state = ?",
                    (ACTIVE, set_id, FROZEN),
                )
                chained = self.store._bound("evalset.activated", {"set": set_id}, org)
                self.store._append(db, f"evalset:{set_id}", "evalset.activated", chained)
            payload = {
                "set": set_id,
                "commitment": str(head["commitment"]),
                "grader": grader,
                "pack_digest": pack_digest,
                "n": len(records),
                "run": used + 1,
            }
            chained = self.store._bound("evalset.graded", payload, org)
            self.store._append(db, f"evalset:{set_id}", "evalset.graded", chained)
        if receipt is not None:
            receipt(
                GradingRead(
                    set_id, str(head["commitment"]), len(records), used + 1, max_runs - used - 1
                )
            )
        dataset = Dataset(
            name=set_id,
            path=Path(set_id),
            records=records,
            sha256="",  # no hash of the hidden cases, keyed or not
            case_key=secrets.token_bytes(32),
        )
        return run(dataset)

    def retire(self, set_id: str) -> dict[str, Any]:
        """Retire a frozen or active set (`mendari evalset retire`, the key holder's only): its
        rows checked once more against its commitment (refused, naming no case, when they no
        longer give it), its state `retired` and its time kept, and `evalset.retired {set,
        org, commitment}` chained, in one exclusive transaction. It grades nothing more, whatever
        its row says later (the chain's retirement is read too), and it retires once; nothing is
        published. The set's id, state, time, commitment and runs used."""
        keys = self._ring()
        _check_set_id(set_id)
        with self.store._db.session(exclusive=True) as db:
            self.store._evalset_ready(db, keys)
            head = db.one(f"SELECT {_FROZEN_COLUMNS} FROM evalset_sets WHERE id = ?", (set_id,))
            if head is None:
                raise MendariError(f"no evaluation set {set_id}")
            org, state = str(head["org"]), str(head["state"])
            if _retired(db, set_id):
                state = RETIRED  # the chain's word, whatever the row says
            if state not in GRADED:
                raise MendariError(
                    f"evaluation set {set_id} is {state}: only a frozen or active set is retired"
                )
            _intact(db, keys, set_id, head)
            retired_at = self._stamp()
            used = _runs(db, set_id)
            db.all(
                "UPDATE evalset_sets SET state = ?, retired_at = ? WHERE id = ? AND state = ?",
                (RETIRED, retired_at, set_id, state),
            )
            payload = {"set": set_id, "commitment": str(head["commitment"])}
            chained = self.store._bound("evalset.retired", payload, org)
            self.store._append(db, f"evalset:{set_id}", "evalset.retired", chained)
        return {
            "set": set_id,
            "state": RETIRED,
            "retired_at": retired_at,
            "commitment": str(head["commitment"]),
            "runs": used,
        }

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
                bound = _bound_to(sealed, row)
                resealed: dict[str, str] = {}
                froms: list[str] = []
                failed = None
                for column, kind in sealed.columns.items():
                    token = row[column]
                    if token is None or str(token).startswith(prefix):
                        continue
                    try:
                        value, opened = keys.open(kind, str(token), *bound)
                    except MendariError:
                        failed = {**_named(sealed, row), "kind": kind, "key": token_key(str(token))}
                        break
                    resealed[column] = keys.seal(kind, value, *bound)
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
        case without them waits for `reindex`); for each label (LP-11h), the commitment its
        `evalset.labelled` chained (`label_ref`); for each set the chain froze, whatever its
        row's state now, and each set row in a state of the lifecycle (LP-11i), the content its
        first `evalset.frozen` committed to, recomputed from its rows (`frozen_holds`), with the
        limits it chained (a set whose row is gone, a draft again, or holds no freeze record,
        fails); and for each set in every state (LP-11p), each case's labels against those the
        chain recorded (each in its chained round), its status against those labels and the
        set's latest draw, each drawn case's weight against that draw's commitment
        (`_mismatches`), and its stored aggregates against the commitment chained when they
        were stored (`stored_aggregates`). Counts the index rows by key id, and the rows a draw
        or a store from before LP-11p's commitments holds (`predating`: each drawn case of a
        set whose latest draw committed to no weight, and each set whose stored aggregates no
        commitment covers), which are not failures. A failure names the value's kind (`case`
        for a case's checks, `label` for a label's, `set` for a set's), its set, a case by its
        hash or a label by its id, and the key it names or what did not match (`commitment`,
        `case_hash`, `index`, `labels`, `status`, `weight`, `aggregates`). `ready` only when
        everything opened and matched and the current key is not retired. Nothing is chained,
        marked or written."""
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
            labelling = "evalset_labels" in present and bool(db.columns("evalset_assignments"))
            refs = {
                str(payload.get("case")): payload.get("case_ref")
                for row in db.all(
                    "SELECT payload FROM audit_events WHERE event = ?", ("evalset.case_added",)
                )
                if isinstance(payload := json.loads(row["payload"]), Mapping)
            }
            # Each label's commitment by its id, and each set's labels as the chain recorded them
            # (their cases, rounds and labellers, `_Chain.labels`: LP-11p).
            label_refs: dict[str, Any] = {}
            chained_labels: dict[str, dict[str, tuple[str, str, str]]] = {}
            for row in db.all(
                "SELECT payload FROM audit_events WHERE event = ? ORDER BY seq",
                ("evalset.labelled",),
            ):
                payload = json.loads(row["payload"])
                if not isinstance(payload, Mapping):
                    continue
                if isinstance(ref := payload.get("label_ref"), Mapping):
                    label_refs[str(ref.get("id"))] = ref.get("sha256")
                    chained_labels.setdefault(str(payload.get("set")), {})[str(ref.get("id"))] = (
                        str(payload.get("case")),
                        str(payload.get("round")),
                        str(payload.get("labeller")),
                    )
            # Each set's first `evalset.frozen` (a set freezes once: a later one is no commitment),
            # its latest draw and the latest event that stored its aggregates (LP-11p).
            freezes: dict[str, Mapping[str, Any]] = {}
            draws: dict[str, Mapping[str, Any]] = {}
            stores: dict[str, Mapping[str, Any]] = {}
            for row in db.all(
                "SELECT event, payload FROM audit_events WHERE event IN (?, ?, ?) ORDER BY seq",
                ("evalset.sampled", *STORING),
            ):
                if not isinstance(payload := json.loads(row["payload"]), Mapping):
                    continue
                named = str(payload.get("set"))
                if row["event"] == "evalset.sampled":
                    draws[named] = payload
                    continue
                stores[named] = payload
                if row["event"] == "evalset.frozen":
                    freezes.setdefault(named, payload)
            sets = (
                {
                    str(row["id"]): row
                    for row in db.all(
                        f"SELECT {_FROZEN_COLUMNS}, aggregates FROM evalset_sets ORDER BY id"
                    )
                }
                if "evalset_sets" in present
                else {}
            )
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
            # What a label's commitment binds besides its values: the assignment it answers.
            answers = ", assignment_id" if table == "evalset_labels" else ""
            after = ""
            while True:
                with self.store._db.session(exclusive=False) as db:
                    rows = db.all(
                        f"SELECT {_selected(sealed)}, org, created_at{answers} FROM {table} "
                        "WHERE id > ? ORDER BY id LIMIT ?",
                        (after, batch),
                    )
                    cases = [str(row["id"]) for row in rows] if table == "evalset_cases" else []
                    indexed = _index_rows(db, index_now, cases)
                if not rows:
                    break
                after = rows[-1]["id"]
                for row in rows:
                    bound = _bound_to(sealed, row)
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
                            values[column] = alone.open(kind, str(token), *bound)[0]
                        except MendariError:
                            failures.append({"kind": kind, **_named(sealed, row), "key": named})
                    if table == "evalset_cases" and len(values) == len(sealed.columns):
                        failures += [
                            {"kind": "case", **_named(sealed, row), "key": problem}
                            for problem in _case_problems(keys, row, values, refs, indexed)
                        ]
                    if (
                        table == "evalset_labels"
                        and values
                        and _label_ref_of(row, values["body"]) != label_refs.get(str(row["id"]))
                    ):
                        failures.append(
                            {"kind": "label", **_named(sealed, row), "key": "commitment"}
                        )
        # Each set the chain froze, whatever its row's state now, and each row in a state of the
        # lifecycle (LP-11i): its row is still there, no draft, and keeps the commitment and the
        # limits its first `evalset.frozen` chained, and its rows, opened with the current key
        # alone, still give the content that commitment covers.
        lifecycle = {set_id for set_id, head in sets.items() if head["state"] != DRAFT}
        for set_id in sorted(set(freezes) | lifecycle):
            head, chained = sets.get(set_id), freezes.get(set_id)
            if head is None or chained is None or head["state"] == DRAFT:
                failures.append({"kind": "set", "set": set_id, "key": "commitment"})
                continue
            with self.store._db.session(exclusive=False) as db:
                kept, labelled = _frozen_rows(db, set_id)
            try:
                held = (
                    kept_as_chained(head, chained)
                    and frozen_holds(alone, set_id, head, kept, labelled, chained["commitment"])[0]
                )
            except MendariError:
                # A value the current key alone does not open is listed above, by its set; one
                # that is not there (a freeze record removed) was not.
                if any(failure.get("set") == set_id for failure in failures):
                    continue
                held = False
            except (ValueError, KeyError, TypeError, AttributeError):
                held = False
            if not held:
                failures.append({"kind": "set", "set": set_id, "key": "commitment"})
        # Each set's rows against what LP-11p's commitments cover, in every state: each case's
        # status against its labels and the set's latest draw and each drawn case's weight
        # against that draw's commitment (`status`, `weight`), and the stored aggregates
        # against the commitment chained when they were stored (`aggregates`). Rows a draw or a
        # store from before those commitments holds are counted (`predating`), never failed.
        predating = 0
        for set_id, head in sets.items():
            chained = stores.get(set_id)
            if (
                head["aggregates"] is not None
                and chained is not None
                and "aggregates_ref" not in chained
            ):
                predating += 1  # stored before the commitments: read as before
            try:
                value = (
                    None
                    if head["aggregates"] is None
                    else alone.open("aggregates", str(head["aggregates"]), set_id)[0]
                )
            except MendariError:
                value = _UNOPENED  # listed above, by the key it names
            if value is not _UNOPENED:
                try:
                    stored_aggregates(set_id, value, chained)
                except MendariError:
                    failures.append({"kind": "set", "set": set_id, "key": "aggregates"})
            draw = draws.get(set_id)
            chain = _Chain(
                {} if draw is None else weight_refs_of(draw), chained_labels.get(set_id, {})
            )
            with self.store._db.session(exclusive=False) as db:
                its_cases = _case_rows(db, set_id)
                # An older database, from before labelling (LP-11h), holds no label.
                its_labels = _label_rows(db, set_id) if labelling else []
            if chain.drawn is None:  # drawn before the commitments: its weights read as before
                predating += sum(1 for row in its_cases if row["status"] != DRAFT)
            try:
                mismatched = _mismatches(alone, set_id, its_cases, its_labels, chain)
            except MendariError:
                continue  # a value the current key alone does not open: listed above
            failures += [
                {"kind": "case", "set": set_id, "case": str(row["case_hash"]), "key": what}
                for row, what in mismatched
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
            "predating": predating,
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
        out: `reindex` rebuilds it from the sealed inputs, and so are assignments (LP-11h), which
        hold nothing sealed. Labels are read before cases and cases before sets, so every case's
        set is in it (a set is never deleted). Nothing is chained or marked."""
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

    def progress(self, set_id: str, *, org: str | None = None) -> dict[str, Any]:
        """How a set's labelling stands (`mendari evalset progress`, LP-11h; and, for `org`, its
        curators' read, LP-11k: another organization's set is refused as an unknown one is,
        `NoEvalset`): its cases counted by status, and each labeller's assignments (by user id)
        in each round, counted as assigned, labelled, open and withdrawn. Ids and counts only:
        no case is named and nothing sealed is opened, so no key is needed; a database from
        before labelling (read as it is) has no assignment."""
        _check_set_id(set_id, org)
        with self.store._db.session(exclusive=False) as db:
            head = _owned(
                set_id,
                db.one("SELECT org, state FROM evalset_sets WHERE id = ?", (set_id,))
                if db.columns("evalset_sets")
                else None,
                org,
            )
            statuses = dict.fromkeys(CASE_STATUSES, 0)
            for row in db.all(
                "SELECT status, count(*) AS n FROM evalset_cases WHERE set_id = ? GROUP BY status",
                (set_id,),
            ):
                statuses[str(row["status"])] = int(row["n"])
            rows = (
                db.all(
                    "SELECT a.user_id, a.round, a.ended_at, l.id AS label FROM "
                    "evalset_assignments a LEFT JOIN evalset_labels l ON l.assignment_id = a.id "
                    "WHERE a.set_id = ?",
                    (set_id,),
                )
                if db.columns("evalset_assignments") and db.columns("evalset_labels")
                else []
            )
        labellers: dict[str, dict[str, dict[str, int]]] = {}
        for row in rows:
            counted = labellers.setdefault(
                str(row["user_id"]),
                {
                    round_: {"assigned": 0, "labelled": 0, "open": 0, "withdrawn": 0}
                    for round_ in ROUNDS
                },
            )[str(row["round"])]
            counted["assigned"] += 1
            if row["label"] is not None:
                counted["labelled"] += 1
            elif row["ended_at"] is None:
                counted["open"] += 1
            else:
                counted["withdrawn"] += 1
        return {
            "set": set_id,
            "org": head["org"],
            "state": head["state"],
            "cases": sum(statuses.values()),
            "statuses": statuses,
            "labellers": dict(sorted(labellers.items())),
        }

    def show(self, set_id: str, *, org: str | None = None) -> dict[str, Any]:
        """A set as the key holder reads it (`mendari evalset show`, LP-11i; and, for `org`, its
        curators' read, LP-11k: another organization's set is refused as an unknown one is,
        `NoEvalset`): its state, how many cases it holds, when it froze, its commitment, its
        rotation rule with the runs used (the chained `evalset.graded`) and left and until when
        it grades, when it retired, and its stored aggregates (opened with the keys, None
        without them or before any measure: counts and coefficients by label, and the strata
        and languages, naming no case). The aggregates are read with the latest event that
        stored them, in one statement, and shown only when the commitment it chained covers
        them (LP-11p, `stored_aggregates`; as they are when that event predates the
        commitments): aggregates re-sealed, put back from an earlier copy, removed or written
        without their event refuse the read, naming the set only. Opening the aggregates is not
        chained (conflict 39); nothing is written."""
        _check_set_id(set_id, org)
        with self.store._db.session(exclusive=False) as db:
            head = _owned(
                set_id,
                db.one(
                    "SELECT org, state, created_at, frozen_at, commitment, max_runs, "
                    "max_age_days, retired_at, aggregates, (SELECT e.payload FROM audit_events e "
                    "WHERE e.decision_id = ? AND e.event IN (?, ?) ORDER BY e.seq DESC LIMIT 1) "
                    "AS stored FROM evalset_sets WHERE id = ?",
                    (f"evalset:{set_id}", *STORING, set_id),
                )
                if db.columns("evalset_sets")
                else None,
                org,
            )
            counted = db.one("SELECT count(*) AS n FROM evalset_cases WHERE set_id = ?", (set_id,))
            used = _runs(db, set_id)
        aggregates = None
        if self.keys is not None:
            sealed = head["aggregates"]
            value = None if sealed is None else self.keys.open("aggregates", str(sealed), set_id)[0]
            stored = json.loads(head["stored"]) if head["stored"] is not None else None
            aggregates = stored_aggregates(set_id, value, stored)
        max_runs, max_age_days = head["max_runs"], head["max_age_days"]
        until = None
        if head["frozen_at"] is not None and max_age_days is not None:
            ends = datetime.fromisoformat(str(head["frozen_at"])) + timedelta(days=max_age_days)
            until = ends.isoformat(timespec="milliseconds")
        return {
            "set": set_id,
            "org": head["org"],
            "state": head["state"],
            "created_at": head["created_at"],
            "cases": int(counted["n"]) if counted else 0,
            "frozen_at": head["frozen_at"],
            "commitment": head["commitment"],
            "max_runs": max_runs,
            "max_age_days": max_age_days,
            "runs": {
                "used": used,
                "left": None if max_runs is None else max(int(max_runs) - used, 0),
            },
            "graded_until": until,
            "retired_at": head["retired_at"],
            "aggregates": aggregates,
        }

    # -- what an organization's curators read (LP-11k) ---------------------------------------

    def named_sets(self, *, org: str) -> list[dict[str, Any]]:
        """`org`'s sets, oldest first, as its curators list them (`GET /api/evalsets`): each
        one's id, name and pack (its header, opened: not chained, conflict 39), state, when it
        was made and how many cases it holds. Ids, names and counts only."""
        keys = self._ring()
        with self.store._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT s.id, s.state, s.created_at, s.header, (SELECT count(*) FROM "
                "evalset_cases c WHERE c.set_id = s.id) AS cases FROM evalset_sets s "
                "WHERE s.org = ? ORDER BY s.created_at, s.id",
                (org,),
            )
        listed = []
        for row in rows:
            header, _ = keys.open("header", str(row["header"]), str(row["id"]))
            listed.append(
                {
                    "set": str(row["id"]),
                    "name": header["name"],
                    "pack": header["pack"],
                    "state": row["state"],
                    "created_at": row["created_at"],
                    "cases": int(row["cases"]),
                }
            )
        return listed

    def summary(self, set_id: str, *, org: str) -> dict[str, Any]:
        """One of `org`'s sets as its curators read it (`GET /api/evalsets/{set_id}`): its name,
        pack and questions with their labels (its header, opened); what `show` reads (its state,
        commitment, rotation rule, runs used and left, and its stored aggregates with the time
        they were measured: None until `mendari evalset agreement`, `coverage` or freeze stores
        them); and what `progress` counts (its cases by status, each labeller's assignments by
        user id). An unknown set, a malformed id and another organization's set are one
        `NoEvalset`. Ids, counts, user ids, the commitment and the stored aggregates without
        each rater pair's label counts (`_curators_aggregates`): no input, label given,
        attribute or case id. Opening the header and the aggregates is not chained (conflict
        39); nothing is written."""
        keys = self._ring()
        shown = self.show(set_id, org=org)
        counted = self.progress(set_id, org=org)
        with self.store._db.session(exclusive=False) as db:
            row = _owned(
                set_id,
                db.one("SELECT org, header FROM evalset_sets WHERE id = ?", (set_id,)),
                org,
            )
        header, _ = keys.open("header", str(row["header"]), set_id)
        return {
            "set": set_id,
            "name": header["name"],
            "pack": header["pack"],
            "questions": header["questions"],
            "state": shown["state"],
            "created_at": shown["created_at"],
            "cases": counted["cases"],
            "statuses": counted["statuses"],
            "labellers": counted["labellers"],
            "frozen_at": shown["frozen_at"],
            "commitment": shown["commitment"],
            "max_runs": shown["max_runs"],
            "max_age_days": shown["max_age_days"],
            "runs": shown["runs"],
            "graded_until": shown["graded_until"],
            "retired_at": shown["retired_at"],
            "aggregates": _curators_aggregates(shown["aggregates"]),
        }

    # -- what the grading route reads before a run (LP-11l) -------------------------------------

    def grading_header(self, set_id: str, *, org: str) -> dict[str, Any]:
        """One of `org`'s sets' header, as the grading route checks the pack it is asked to run
        before any case is read (`POST /api/evalsets/{set_id}/runs`): the pack's id, its
        questions with their labels, its derived questions and critical labels (what
        `pack_problem` compares a pack with); never the set's name. Opened, not chained
        (conflict 39: a header holds no case); nothing is written. An unknown set, a malformed
        id and another organization's set are one `NoEvalset`. A header that does not open is
        refused as the grading read refuses it (`_intact`), naming the set only: `set_unavailable`
        when it is sealed under a key this database knows and the ring lacks, else
        `set_changed`."""
        keys = self._ring()
        _check_set_id(set_id, org)
        with self.store._db.session(exclusive=False) as db:
            row = _owned(
                set_id,
                db.one("SELECT org, header FROM evalset_sets WHERE id = ?", (set_id,)),
                org,
            )
            try:
                header, _ = keys.open("header", str(row["header"]), set_id)
            except MendariError:
                lacking = _lacking(db, keys, [row["header"]])
                raise (_unavailable(set_id, lacking) if lacking else _changed(set_id)) from None
        return {name: header.get(name) for name in ("pack", "questions", "derived", "critical")}

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
                        "its input is a public case's (the bundled agents' data, datasets/ or "
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
    named = (sealed.set_id, sealed.case_id, sealed.shown, sealed.label_id)
    return ", ".join(dict.fromkeys(("id", *filter(None, named), *sealed.columns)))


def _bound_to(sealed: SealedTable, row: Mapping[str, Any]) -> tuple[str, str, str]:
    """The set, case and label ids a row's sealed values are bound to (an empty case id: the
    set's; an empty label id: no label's)."""
    case_id = "" if sealed.case_id is None else str(row[sealed.case_id])
    label_id = "" if sealed.label_id is None else str(row[sealed.label_id])
    return str(row[sealed.set_id]), case_id, label_id


def _named(sealed: SealedTable, row: Mapping[str, Any]) -> dict[str, str]:
    """A row as printed: its set's id and, for a case's values, the case's keyed hash (never
    its id); for a label, the label's id (LP-11h)."""
    named = {"set": str(row[sealed.set_id])}
    if sealed.shown is not None:
        named["case"] = str(row[sealed.shown])
    if sealed.label_id is not None:
        named["label"] = str(row[sealed.label_id])
    return named


def _label_ref_of(row: Mapping[str, Any], body: Any) -> str | None:
    """The commitment a label row's opened body gives (`label_ref`); None for a body that is
    not a label's."""
    try:
        salt = base64.b64decode(str(body.get("salt", "")), validate=True)
        return label_ref(
            salt,
            str(row["set_id"]),
            str(row["case_id"]),
            str(row["id"]),
            str(row["assignment_id"]),
            body.get("values"),
        )
    except (MendariError, ValueError, AttributeError):
        return None


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


def _check_set_id(set_id: str, org: str | None = None) -> None:
    """`set_id` names a set as the store minted it; for a route acting for `org`, anything else
    is refused as an unknown set is (`NoEvalset`)."""
    if not _is_uuid(set_id):
        if org is not None:
            raise NoEvalset(NO_SET)
        raise MendariError("a set is named by its id, as `mendari evalset create` printed it")


def _owned(set_id: str, row: Mapping[str, Any] | None, org: str | None) -> Mapping[str, Any]:
    """A set's row (it names its `org`); a `NoEvalset` when there is none, or, for a route acting
    for `org`, when it is another organization's: one refusal for both, naming no id then."""
    if row is None or (org is not None and row["org"] != org):
        raise NoEvalset(NO_SET if org is not None else f"no evaluation set {set_id}")
    return row


def _draft(set_id: str, row: Mapping[str, Any] | None, org: str | None = None) -> Mapping[str, Any]:
    """A set's row when it is a draft (of `org`, when given: `_owned` first, so another
    organization's set says nothing of its state); a `MendariError` when there is none or it
    is not one."""
    row = _owned(set_id, row, org)
    if row["state"] != DRAFT:
        raise MendariError(f"evaluation set {set_id} is {row['state']}: only a draft changes")
    return row


def _curators_aggregates(aggregates: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The stored aggregates as a set's curators read them (`summary`): each rater pair's kappa
    without its raters' label counts (`labels`), which, where the pair shares few cases or a
    rater gave one label throughout, are what each of them answered. The key holder's
    `evalset show` and `evalset agreement` keep them."""
    if aggregates is None:
        return None
    shown = copy.deepcopy(dict(aggregates))
    for question in shown["agreement"]["questions"].values():
        for pair in question["kappa"]:
            pair.pop("labels", None)
    return shown


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
    """Every public case's input: each JSONL record's under the bundled agents and the example
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
    for line in text.split("\n"):  # not splitlines: a U+2028 inside a string is not a line end
        try:
            record = json.loads(line) if line.strip() else None
        except (ValueError, RecursionError):
            continue
        if isinstance(record, dict) and isinstance(record.get("input"), dict):
            yield record["input"]


# -- blind labelling's helpers (LP-11h) ----------------------------------------------------------


def _check_user_id(user: Any) -> str:
    """`user` when it is a person's user id (`accounts.person_id`); a `MendariError` that never
    repeats what was given otherwise."""
    if not (isinstance(user, str) and accounts.USER_ID.fullmatch(user)):
        raise MendariError(
            "a labeller is named by their user id (user: and 16 hex digits), as the members "
            "list shows it"
        )
    return user


def _user_ids(labellers: Sequence[str], *, at_least: int) -> list[str]:
    """The labellers given, each a user id, sorted and each once; at least `at_least` of them."""
    if isinstance(labellers, str):
        labellers = [labellers]
    users = sorted({_check_user_id(user) for user in labellers})
    if len(users) < at_least:
        raise MendariError(f"give at least {at_least} different labellers (their user ids)")
    return users


def _labeller_membership(db: Session, org: str, user_id: str) -> Mapping[str, Any] | None:
    """`user_id`'s membership of `org` in force when it holds the `labeller` role and no role
    that manages hidden sets (`accounts.CURATING`); None otherwise."""
    row = db.one(
        "SELECT id, roles FROM memberships WHERE org = ? AND user_id = ? AND removed_at IS NULL",
        (org, user_id),
    )
    if row is None:
        return None
    roles = set(json.loads(row["roles"]))
    if accounts.LABELLER not in roles or roles & set(accounts.CURATING):
        return None
    return row


def _labeller_in(db: Session, org: str, user: str) -> str:
    """The id of the membership `user` labels `org`'s sets on; a `MendariError` saying why not:
    they manage hidden sets there (separation of duties), or hold no labeller role there."""
    row = db.one(
        "SELECT id, roles FROM memberships WHERE org = ? AND user_id = ? AND removed_at IS NULL",
        (org, user),
    )
    roles = set(json.loads(row["roles"])) if row is not None else set()
    if roles & set(accounts.CURATING):
        raise MendariError(
            f"{user} manages hidden sets in {org}: who curates a set is never assigned its cases"
        )
    if row is None or accounts.LABELLER not in roles:
        raise MendariError(f"{user} holds no labeller role on a membership in force in {org}")
    return str(row["id"])


def _withdraw(db: Session, set_id: str, org: str, round_: str) -> int:
    """End the open assignments of a set's round whose membership no longer holds (ended,
    replaced by a new one, or no longer a labeller's without a curating role): their cases are
    then given to others. How many."""
    stale = [
        str(row["id"])
        for row in db.all(
            "SELECT id, user_id, membership_id FROM evalset_assignments "
            "WHERE set_id = ? AND round = ? AND ended_at IS NULL",
            (set_id, round_),
        )
        if (held := _labeller_membership(db, org, str(row["user_id"]))) is None
        or str(held["id"]) != row["membership_id"]
    ]
    now = _now()
    for start in range(0, len(stale), _INSERT_BATCH):
        batch = stale[start : start + _INSERT_BATCH]
        marks = ", ".join("?" for _ in batch)
        db.all(f"UPDATE evalset_assignments SET ended_at = ? WHERE id IN ({marks})", (now, *batch))
    return len(stale)


def _held(db: Session, set_id: str) -> dict[tuple[str, str], list[str]]:
    """Who holds each case of a set in each round, by (case id, round): the labellers of its
    open and answered assignments, never a withdrawn one's."""
    held: dict[tuple[str, str], list[str]] = {}
    for row in db.all(
        "SELECT a.case_id, a.round, a.user_id FROM evalset_assignments a WHERE a.set_id = ? "
        "AND (a.ended_at IS NULL OR EXISTS "
        "(SELECT 1 FROM evalset_labels l WHERE l.assignment_id = a.id)) "
        "ORDER BY a.created_at, a.id",
        (set_id,),
    ):
        held.setdefault((str(row["case_id"]), str(row["round"])), []).append(str(row["user_id"]))
    return held


def _pair(first: str, second: str) -> tuple[str, str]:
    one, other = sorted((first, second))
    return one, other


def _label_problem(values: Any, questions: Mapping[str, Sequence[str]]) -> str | None:
    """Why `values` is not a label of a set asking `questions` (each with its labels): a
    question it does not ask, one unanswered, or an answer not among a question's labels. It
    names questions (the pack's own words), never a value given."""
    if not isinstance(values, Mapping):
        return "labels are a mapping of each question to one of its labels"
    if any(question not in questions for question in values):
        return "labels name a question this set does not ask"
    unanswered = [question for question in questions if question not in values]
    if unanswered:
        return f"labels answer every question: {', '.join(unanswered)} unanswered"
    for question, labels in questions.items():
        given = values[question]
        if not isinstance(given, str) or given not in labels:
            return f"labels.{question} is not one of its labels"
    return None


def _majority(labels: Sequence[Mapping[str, str]]) -> dict[str, str] | None:
    """Each question's label that more than half of `labels` give; None when a question has
    none (with three labels, a three-way split)."""
    settled: dict[str, str] = {}
    for question in sorted({question for label in labels for question in label}):
        counted = Counter(label[question] for label in labels if question in label)
        chosen, votes = counted.most_common(1)[0]
        if votes * 2 <= len(labels):
            return None
        settled[question] = chosen
    return settled


def _opened_labels(
    keys: EvalsetKeys, set_id: str, rows: Sequence[Mapping[str, Any]]
) -> list[tuple[Mapping[str, Any], dict[str, str]]]:
    """Each label row with its values, opened (bound to its set, case and own id)."""
    return [
        (
            row,
            keys.open("label", str(row["body"]), set_id, str(row["case_id"]), str(row["id"]))[0][
                "values"
            ],
        )
        for row in rows
    ]


def _settle(db: Session, keys: EvalsetKeys, set_id: str, case_id: str) -> None:
    """A case's status once its labels are in, in the labelling's transaction: with its two
    first-pass labels, `agreed` when they are the same on every question, else `disputed`;
    with its adjudicator's too, `resolved` when every question has a majority of the three,
    else `unresolved` (a three-way split, which keeps the set from freezing until the case is
    removed). One label alone settles nothing."""
    rows = db.all(
        "SELECT l.id, l.case_id, l.body, a.round FROM evalset_labels l "
        "JOIN evalset_assignments a ON a.id = l.assignment_id WHERE l.case_id = ? "
        "ORDER BY l.created_at, l.id",
        (case_id,),
    )
    opened = _opened_labels(keys, set_id, rows)
    first = [values for row, values in opened if row["round"] == FIRST]
    third = [values for row, values in opened if row["round"] == ADJUDICATION]
    status = _labelled_status(first, third)
    if status is not None:
        db.all("UPDATE evalset_cases SET status = ? WHERE id = ?", (status, case_id))


def _labelled_status(
    first: Sequence[Mapping[str, str]], third: Sequence[Mapping[str, str]]
) -> str | None:
    """The status a case's labels settle it at (`_settle`, and what `given_statuses` checks a
    status against): with an adjudicator's label on a disputed case (two first-pass labels that
    differ, `_disputed`), `resolved` when every question has a majority of the three, else
    `unresolved`; with two first-pass labels, `agreed` when they are the same on every
    question, else `disputed`; None while they settle nothing, and for an adjudicator's label on
    a case its first two did not dispute, which no store write gives (an assignment's round
    edited by SQL: `given_statuses` then allows no status, LP-11p)."""
    if third:
        if not _disputed(first):
            return None
        return RESOLVED if _majority([*first, *third]) is not None else UNRESOLVED
    if len(first) >= 2:
        return AGREED if first[0] == first[1] else DISPUTED
    return None


def _settled(
    status: str, first: Sequence[Mapping[str, str]], third: Sequence[Mapping[str, str]]
) -> dict[str, str]:
    """A case's final member labels: an agreed case's, a resolved case's majority; none while
    it waits, is disputed or is unresolved."""
    if status == AGREED and first:
        return dict(first[0])
    if status == RESOLVED:
        return _majority([*first, *third]) or {}
    return {}


def _shared_out(total: int, sizes: Mapping[str, int]) -> dict[str, int]:
    """`total` cases shared out among strata in proportion to `sizes` (the cases each has left),
    by largest remainders, ties by stratum: each stratum gets at most its size."""
    pool = sum(sizes.values())
    quotas = {name: Fraction(total * size, pool) for name, size in sizes.items()}
    shares = {name: int(quota) for name, quota in quotas.items()}
    left = total - sum(shares.values())
    for name in sorted(sizes, key=lambda name: (-(quotas[name] - shares[name]), name))[:left]:
        shares[name] += 1
    return shares


def pack_problem(pack: Pack, header: Mapping[str, Any]) -> str | None:
    """Why `pack` is not the set's (its header's pack id, questions with their labels, derived
    questions and critical labels); None when it is."""
    manifest = pack.manifest
    derived = manifest.derived
    made = {
        "pack": pack.id,
        "questions": {q: list(manifest.labels(q)) for q in manifest.questions if q not in derived},
        "derived": {q: list(manifest.labels(q)) for q in derived},
        "critical": [
            {"question": item.question, "label": item.label}
            for item in manifest.evaluation.critical
        ],
    }
    if all(made[name] == header.get(name) for name in made):
        return None
    return (
        f"pack {pack.id} does not ask the set's questions with their labels and critical "
        "labels: give the pack the set was made for (--pack)"
    )


def _measured_rows(
    cases: Sequence[Mapping[str, Any]], labels: Sequence[Mapping[str, Any]]
) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    """What a measure read (the drawn cases with their statuses, and the labels' ids), to find
    it unchanged when its aggregates are written."""
    return (
        tuple((str(row["id"]), str(row["status"])) for row in cases),
        tuple(sorted(str(row["id"]) for row in labels)),
    )


def _aggregates(
    keys: EvalsetKeys,
    pack: Pack,
    set_id: str,
    cases: Sequence[Mapping[str, Any]],
    labels: Sequence[Mapping[str, Any]],
    seed: int,
    resamples: int,
) -> dict[str, Any]:
    """A set's aggregates (`AGGREGATES_FORMAT`), from its drawn cases and their labels, opened
    here: the coverage report of the cases with their settled labels (and those the pack derives
    from them), named by the set and its case hashes, never an id; and the agreement of the
    first-pass labels of each case both its labellers answered (conflict 41), raters by user
    id. Counts and coefficients only."""
    from mendari.evals.coverage import coverage
    from mendari.evals.harness import Dataset
    from mendari.evals.rater_agreement import agreement, with_derived

    manifest: PackManifest = pack.manifest
    given: dict[str, list[tuple[str, str, dict[str, str]]]] = {}
    for row, values in _opened_labels(keys, set_id, labels):
        given.setdefault(str(row["case_id"]), []).append(
            (str(row["user_id"]), str(row["round"]), values)
        )
    records: list[dict[str, Any]] = []
    rated: list[dict[str, dict[str, str]]] = []
    for row in cases:
        case_id = str(row["id"])
        value, _ = keys.open("input", str(row["input"]), set_id, case_id)
        body, _ = keys.open("attributes", str(row["attributes"]), set_id, case_id)
        mine = given.get(case_id, [])
        first = {user: values for user, round_, values in mine if round_ == FIRST}
        if len(first) >= 2:
            rated.append(first)
        settled = _settled(
            str(row["status"]),
            [values for _, round_, values in mine if round_ == FIRST],
            [values for _, round_, values in mine if round_ == ADJUDICATION],
        )
        records.append(
            {
                "id": str(row["case_hash"]),
                "input": value,
                "labels": with_derived(manifest, settled) if settled else {},
                "attributes": dict(body["attributes"]),
            }
        )
    dataset = Dataset(name=set_id, path=Path(set_id), records=records, sha256="")
    covered = coverage(manifest, dataset, pack=pack.id)
    covered["dataset"] = {"name": set_id}  # no hash of the hidden cases, keyed or not
    return {
        "format": AGGREGATES_FORMAT,
        "set": set_id,
        "measured_at": _now(),
        "n": len(cases),
        "coverage": covered,
        "agreement": agreement(manifest, rated, pack=pack.id, seed=seed, resamples=resamples),
    }


# -- the lifecycle's helpers (LP-11i) ------------------------------------------------------------

# What the lifecycle reads of a set's row: its organization and state, its sealed header and
# freeze record, its commitment, its rotation rule's limits and when it froze.
_FROZEN_COLUMNS = (
    "id, org, state, header, freeze_record, commitment, max_runs, max_age_days, frozen_at"
)
_UNOPENED = object()  # a sealed value the ring did not open (`check`)
# What a drawn case is called, by its status, while it keeps its set from freezing.
_WAITING = {SAMPLED: "unlabelled", DISPUTED: "disputed", UNRESOLVED: "unresolved"}


def _check_limits(max_runs: Any, max_age_days: Any) -> None:
    """The rotation rule's two limits, each a whole number from 1 to its cap: both are given,
    and there is no default (conflict 47)."""
    for flag, value, cap in (
        ("--max-runs", max_runs, RUNS_CAP),
        ("--max-age-days", max_age_days, DAYS_CAP),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= cap:
            raise MendariError(
                f"{flag} is a whole number from 1 to {cap}: a frozen set's rotation rule has "
                "no default"
            )


def _unsettled(set_id: str, waiting: Mapping[str, int]) -> str:
    """Why a set does not freeze yet: how many of its drawn cases wait, by why (no case named)."""
    listed = ", ".join(
        f"{waiting[status]} {name}" for status, name in _WAITING.items() if waiting.get(status)
    )
    return (
        f"evaluation set {set_id} has drawn cases not settled ({listed}): each is agreed or "
        "resolved before it freezes (label it, adjudicate a disputed one, and remove an "
        "unresolved one with `mendari evalset remove-case`)"
    )


def _final_labels(
    keys: EvalsetKeys,
    set_id: str,
    cases: Sequence[Mapping[str, Any]],
    labels: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, str]]:
    """Each case's final member labels, by its id (`_settled`, from its labels opened here): an
    agreed case's, a resolved case's majority; none for a case of any other status."""
    given: dict[str, tuple[list[dict[str, str]], list[dict[str, str]]]] = {}
    for row, values in _opened_labels(keys, set_id, labels):
        first, third = given.setdefault(str(row["case_id"]), ([], []))
        (first if row["round"] == FIRST else third).append(values)
    return {
        str(row["id"]): _settled(str(row["status"]), *given.get(str(row["id"]), ([], [])))
        for row in cases
    }


def _derived(
    keys: EvalsetKeys,
    pack: Pack,
    set_id: str,
    cases: Sequence[Mapping[str, Any]],
    labels: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, str]]:
    """The labels the pack derives from each case's final member labels (its decision tables,
    `derive_answer`), by case id: what a frozen set's freeze record keeps, so its content is
    recomputed later without the pack (a later version of it grades the set too)."""
    from mendari.evals.rater_agreement import with_derived

    derived: dict[str, dict[str, str]] = {}
    for case_id, settled in _final_labels(keys, set_id, cases, labels).items():
        whole = with_derived(pack.manifest, settled)
        derived[case_id] = {q: label for q, label in whole.items() if q not in settled}
    return derived


def _frozen_content(
    keys: EvalsetKeys,
    set_id: str,
    header: Mapping[str, Any],
    limits: tuple[int, int, str],
    cases: Sequence[Mapping[str, Any]],
    labels: Sequence[Mapping[str, Any]],
    derived: Mapping[str, Mapping[str, str]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """A frozen set's content (`CONTENT_FORMAT`), from its rows opened with `keys`: its header
    (the pack's id, its questions with their labels, its derived questions and critical
    labels) with the rotation rule (`limits`: the most grading runs, the most days, and when
    the days count from); then each case, in id order whatever order its rows come in, with its
    input, its final labels (the members' settled ones and the pack's derived ones, `derived`),
    its status (agreed or resolved: conflict 45, every case field) and its source, stratum,
    weight, language, author and template. The set's name is not in it. And the same cases as
    a dataset's records, each named by its keyed hash, never its id (and without its status)."""
    max_runs, max_age_days, frozen_at = limits
    finals = _final_labels(keys, set_id, cases, labels)
    entries: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for row in sorted(cases, key=lambda row: str(row["id"])):
        case_id = str(row["id"])
        value, _ = keys.open("input", str(row["input"]), set_id, case_id)
        body, _ = keys.open("attributes", str(row["attributes"]), set_id, case_id)
        own = body.get("attributes") or {}
        described = {name: own.get(name) for name in FROZEN_ATTRIBUTES}
        described["weight"] = body.get("weight")
        final = {**finals[case_id], **derived.get(case_id, {})}
        entries.append(
            {
                "id": case_id,
                "input": value,
                "labels": final,
                "status": str(row["status"]),
                **described,
            }
        )
        records.append(
            {
                "id": str(row["case_hash"]),
                "input": value,
                "labels": dict(final),
                "attributes": dict(described),
            }
        )
    content = {
        "format": CONTENT_FORMAT,
        "header": {
            "pack": header.get("pack"),
            "questions": header.get("questions"),
            "derived": header.get("derived"),
            "critical": header.get("critical"),
            "max_runs": max_runs,
            "max_age_days": max_age_days,
            "frozen_at": frozen_at,
        },
        "cases": entries,
    }
    return content, records


def frozen_holds(
    keys: EvalsetKeys,
    set_id: str,
    row: Mapping[str, Any],
    cases: Sequence[Mapping[str, Any]],
    labels: Sequence[Mapping[str, Any]],
    commitment: str,
) -> tuple[bool, list[dict[str, Any]]]:
    """Whether a frozen set's rows still give the content `commitment` covers (LP-11i): its
    freeze record, opened with `keys`, holds a salt and a content hash that give the
    commitment (`freeze_commitment`), and the content recomputed from its set row (`row`, its
    header, limits and time), its cases and their labels (`_frozen_content`) hashes to that
    content hash. With the cases as a dataset's records when it holds. A value `keys` does not
    open raises `MendariError`; a freeze record of another shape, `ValueError`, `KeyError`,
    `TypeError` or `AttributeError`."""
    header, _ = keys.open("header", str(row["header"]), set_id)
    record, _ = keys.open("freeze", str(row["freeze_record"]), set_id)
    salt = base64.b64decode(str(record["salt"]), validate=True)
    hashed = str(record["content_hash"])
    if freeze_commitment(salt, hashed) != commitment:
        return False, []
    limits = (int(row["max_runs"]), int(row["max_age_days"]), str(row["frozen_at"]))
    content, records = _frozen_content(
        keys, set_id, header, limits, cases, labels, record["derived"]
    )
    if content_hash(content) != hashed:
        return False, []
    return True, records


def kept_as_chained(row: Mapping[str, Any], chained: Mapping[str, Any]) -> bool:
    """Whether a frozen set's row keeps the commitment and the limits its `evalset.frozen`
    chained."""
    return (row["commitment"], row["max_runs"], row["max_age_days"]) == (
        chained.get("commitment"),
        chained.get("max_runs"),
        chained.get("max_age_days"),
    )


def _case_rows(db: Session, set_id: str) -> list[dict[str, Any]]:
    """A set's cases, in id order, as `frozen_holds` and `_mismatches` read them (and `freeze`,
    `measure`, `assign` and `check`)."""
    return db.all(
        "SELECT id, case_hash, status, input, attributes FROM evalset_cases WHERE set_id = ? "
        "ORDER BY id",
        (set_id,),
    )


def _frozen_rows(db: Session, set_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """A set's cases and labels, each label with its round, as `frozen_holds` reads them."""
    cases = _case_rows(db, set_id)
    labels = db.all(
        "SELECT l.id, l.case_id, l.body, a.round FROM evalset_labels l "
        "JOIN evalset_assignments a ON a.id = l.assignment_id WHERE l.set_id = ? ORDER BY l.id",
        (set_id,),
    )
    return cases, labels


def _frozen_payload(db: Session, set_id: str) -> Mapping[str, Any] | None:
    """What a set's `evalset.frozen` chained (None: it was never frozen)."""
    row = db.one(
        "SELECT payload FROM audit_events WHERE decision_id = ? AND event = ? ORDER BY seq LIMIT 1",
        (f"evalset:{set_id}", "evalset.frozen"),
    )
    payload = json.loads(row["payload"]) if row else None
    return payload if isinstance(payload, Mapping) else None


def _runs(db: Session, set_id: str) -> int:
    """How many grading runs a set has had: its chained `evalset.graded` (conflict 47)."""
    row = db.one(
        "SELECT count(*) AS n FROM audit_events WHERE decision_id = ? AND event = ?",
        (f"evalset:{set_id}", "evalset.graded"),
    )
    return int(row["n"]) if row else 0


def _retired(db: Session, set_id: str) -> bool:
    """Whether a set's chain holds its `evalset.retired`: then it is retired, whatever its row
    says (a state edited back grades nothing, and retires nothing again)."""
    return (
        db.one(
            "SELECT 1 AS n FROM audit_events WHERE decision_id = ? AND event = ? LIMIT 1",
            (f"evalset:{set_id}", "evalset.retired"),
        )
        is not None
    )


def _lacking(db: Session, keys: EvalsetKeys, tokens: Sequence[Any]) -> list[str]:
    """The key ids a set's sealed values (`tokens`; None is no value) name that `keys` does not
    hold and this database knows (`EVALSET_KNOWN`): values `keys` cannot open, though no one
    altered them (a rotation's read key not deployed), as `verify` counts them `unopenable`. A
    key id this database never knew is not among them: such a value was altered."""
    absent = sorted(
        {token_key(str(token)) for token in tokens if token is not None} - keys.ring_ids
    )
    if not absent:
        return []
    names = [EVALSET_KNOWN + key for key in absent]
    marks = ", ".join("?" for _ in names)
    found = db.all(f"SELECT name FROM settings WHERE name IN ({marks})", names)
    return sorted(str(row["name"])[len(EVALSET_KNOWN) :] for row in found)


def _changed(set_id: str) -> GradingRefused:
    """The refusal of a set whose rows no longer give what it was frozen with: the set alone is
    named, never a case (the grading route answers it `set_changed`, LP-11l)."""
    return GradingRefused(
        "set_changed",
        f"evaluation set {set_id} does not hold what it was frozen with: nothing was read or "
        "changed; run `mendari audit verify --evalsets`",
    )


def _unavailable(set_id: str, lacking: Sequence[str]) -> GradingRefused:
    """The refusal of a set holding values sealed under keys this database knows and this
    deployment lacks (`_lacking`): the set and those key ids are named, never a case. It cannot
    be checked here, which is not a change (the grading route answers it `set_unavailable`)."""
    return GradingRefused(
        "set_unavailable",
        f"evaluation set {set_id} holds values sealed under key(s) {', '.join(lacking)}, "
        f"which this deployment lacks: nothing was read or changed; deploy them in {READ_ENV} "
        "or run `mendari evalset rotate --check`",
    )


def _rule(set_id: str, head: Mapping[str, Any]) -> tuple[int, int, datetime]:
    """A frozen set's rotation rule as its row keeps it: the most runs, the most days, and when
    it froze (a row that lost them was edited: `_changed`)."""
    try:
        frozen_at = datetime.fromisoformat(str(head["frozen_at"]))
        if frozen_at.tzinfo is None:
            raise ValueError("a time without its offset")
        return int(head["max_runs"]), int(head["max_age_days"]), frozen_at
    except (TypeError, ValueError):
        raise _changed(set_id) from None


def _intact(
    db: Session, keys: EvalsetKeys, set_id: str, head: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """A frozen or active set's cases as a dataset's records, read in the caller's transaction,
    when its row keeps what its `evalset.frozen` chained and its rows still give the content
    that commitment covers (`frozen_holds`); else `_changed`, naming the set only. A set holding
    a value sealed under a key this database knows and `keys` lacks (`_lacking`: its header and
    freeze record, its cases' inputs and attributes, its labels) is refused first
    (`_unavailable`): it cannot be checked here, which is not a change."""
    chained = _frozen_payload(db, set_id)
    cases, labels = _frozen_rows(db, set_id)
    tokens = [head["header"], head["freeze_record"]]
    tokens += [row[column] for row in cases for column in ("input", "attributes")]
    tokens += [label["body"] for label in labels]
    lacking = _lacking(db, keys, tokens)
    if lacking:
        raise _unavailable(set_id, lacking)
    try:
        held, records = (
            frozen_holds(keys, set_id, head, cases, labels, str(chained["commitment"]))
            if chained is not None and kept_as_chained(head, chained)
            else (False, [])
        )
    except (MendariError, ValueError, KeyError, TypeError, AttributeError):
        held, records = False, []
    if not held:
        raise _changed(set_id)
    return records


# -- a draft's draws and statuses, and every set's aggregates, committed to (LP-11p) -------------


def weight_refs_of(payload: Mapping[str, Any]) -> dict[str, str] | None:
    """The weight commitments an `evalset.sampled` chained, by case hash (`weight_refs`); None
    for a draw from before them (LP-11p), which committed to no weight."""
    refs = payload.get("weight_refs")
    if not isinstance(refs, Mapping):
        return None
    return {str(hashed): str(ref) for hashed, ref in refs.items()}


def _latest_draw(db: Session, set_id: str) -> dict[str, str] | None:
    """The weight commitments, by case hash, of the set's latest `evalset.sampled`: each case
    it names is drawn, and every drawn case is named, since each draw commits to them all.
    Empty without a draw; None when the latest draw is from before the commitments (LP-11p),
    when a case's status alone says whether it was drawn."""
    row = db.one(
        "SELECT payload FROM audit_events WHERE decision_id = ? AND event = ? "
        "ORDER BY seq DESC LIMIT 1",
        (f"evalset:{set_id}", "evalset.sampled"),
    )
    if row is None:
        return {}
    payload = json.loads(row["payload"])
    return weight_refs_of(payload) if isinstance(payload, Mapping) else None


class _Chain(NamedTuple):
    """What a set's chain says of its draft rows (LP-11p), as `_chain` reads it: its latest
    draw's weight commitments by case hash (`drawn`: empty without a draw, None for a draw from
    before them, as `_latest_draw` gives them), and each label it chained, by the label's id:
    its case's keyed hash, its round and its labeller, as its `evalset.labelled` recorded them
    (`labels`), never as an assignment row says now."""

    drawn: dict[str, str] | None
    labels: dict[str, tuple[str, str, str]]


def _chain(db: Session, set_id: str) -> _Chain:
    """A set's `_Chain`, read from its own events in one statement."""
    drawn: dict[str, str] | None = {}
    labels: dict[str, tuple[str, str, str]] = {}
    for row in db.all(
        "SELECT event, payload FROM audit_events WHERE decision_id = ? AND event IN (?, ?) "
        "ORDER BY seq",
        (f"evalset:{set_id}", "evalset.sampled", "evalset.labelled"),
    ):
        payload = json.loads(row["payload"])
        if row["event"] == "evalset.sampled":
            drawn = weight_refs_of(payload) if isinstance(payload, Mapping) else None
        elif isinstance(payload, Mapping) and isinstance(ref := payload.get("label_ref"), Mapping):
            labels[str(ref.get("id"))] = (
                str(payload.get("case")),
                str(payload.get("round")),
                str(payload.get("labeller")),
            )
    return _Chain(drawn, labels)


def _label_rows(db: Session, set_id: str) -> list[dict[str, Any]]:
    """A set's label rows, in id order, without their assignments: each label's round and
    labeller are the chain's (`_Chain.labels`, `_as_chained`)."""
    return db.all(
        "SELECT id, case_id, body FROM evalset_labels WHERE set_id = ? ORDER BY id", (set_id,)
    )


def _as_chained(labels: Sequence[Mapping[str, Any]], chain: _Chain) -> list[dict[str, Any]]:
    """Each label row the chain recorded, with the round and the labeller (`user_id`) its
    `evalset.labelled` chained: what `freeze` and `measure` read once `_mismatches` found the
    rows and the chain to agree."""
    return [
        {**row, "round": chained[1], "user_id": chained[2]}
        for row in labels
        if (chained := chain.labels.get(str(row["id"]))) is not None
    ]


def _disputed(first: Sequence[Mapping[str, str]]) -> bool:
    """Whether a case's first-pass labels are two that differ: the only case adjudicated."""
    return len(first) == 2 and first[0] != first[1]


def shaped_statuses(drawn: bool | None, firsts: int, thirds: int) -> tuple[str, ...]:
    """The statuses a case may hold by its draw and how many first-pass and adjudication labels
    it holds, opening none (LP-11p; what `assign` and `adjudicate` check, `_shape_mismatches`):
    `draft` for a case no draw took (none, if it holds labels: only a drawn case is assigned);
    for a drawn one, `sampled` until it holds two first-pass labels, then `agreed` or
    `disputed`, then, with an adjudicator's label, `resolved` or `unresolved`; none for an
    adjudicator's label on a case without two first-pass labels. `drawn` is None for a set whose
    latest draw is from before the commitments: an unlabelled case may then be `draft` or
    `sampled`."""
    if drawn is False:
        return () if firsts or thirds else (DRAFT,)
    if thirds:
        return (RESOLVED, UNRESOLVED) if firsts == 2 else ()
    if firsts >= 2:
        return (AGREED, DISPUTED)
    if drawn is None and not firsts:
        return (DRAFT, SAMPLED)
    return (SAMPLED,)


def given_statuses(
    drawn: bool | None,
    first: Sequence[Mapping[str, str]],
    third: Sequence[Mapping[str, str]],
) -> tuple[str, ...]:
    """The statuses a case's labels (the first-pass and adjudication values, opened) and its draw
    allow (LP-11p): those its shape allows (`shaped_statuses`), and of a settled one's, the one
    its labels settle (`_labelled_status`), so none for an adjudicator's label on a case whose
    two first-pass labels do not differ (only a disputed case is adjudicated)."""
    shaped = shaped_statuses(drawn, len(first), len(third))
    if set(shaped) <= {DRAFT, SAMPLED}:
        return shaped  # no label settles it
    settled = _labelled_status(first, third)
    return (settled,) if settled in shaped else ()


def weight_holds(body: Any, set_id: str, case_id: str, ref: str | None) -> bool:
    """Whether a case's opened attributes (`body`) hold the weight its draw committed to (`ref`,
    `weight_ref`: the weight and its salt, sealed beside the attributes); for a case no draw
    took (`ref` None), whether they hold no weight."""
    if not isinstance(body, Mapping):
        return False
    if ref is None:
        return "weight" not in body and "weight_salt" not in body
    try:
        salt = base64.b64decode(str(body.get("weight_salt", "")), validate=True)
        return weight_ref(salt, set_id, case_id, body.get("weight")) == ref
    except (ValueError, MendariError):
        return False


def row_mismatches(
    set_id: str,
    case: Mapping[str, Any],
    body: Any,
    first: Sequence[Mapping[str, str]],
    third: Sequence[Mapping[str, str]],
    committed: Mapping[str, str] | None,
) -> list[str]:
    """What of a case's row the commitments do not give (LP-11p), from its row (`case`: its id,
    keyed hash and status), its opened attributes (`body`) and labels (`first`, `third`), and
    its set's latest draw (`committed`: its weight commitments by case hash, `_Chain.drawn`):
    `status`, a status its labels and that draw do not give (`given_statuses`); `weight`, a
    drawn case's weight that draw's commitment does not cover, or a weight on a case it did not
    draw (`weight_holds`). A draw from before the commitments (`committed` None) checks the
    status against the labels alone and no weight."""
    hashed = str(case["case_hash"])
    drawn = None if committed is None else hashed in committed
    problems = []
    if str(case["status"]) not in given_statuses(drawn, first, third):
        problems.append("status")
    if committed is not None and not weight_holds(
        body, set_id, str(case["id"]), committed.get(hashed)
    ):
        problems.append("weight")
    return problems


def _labels_as_chained(
    cases: Sequence[Mapping[str, Any]], labels: Sequence[Mapping[str, Any]], chain: _Chain
) -> tuple[dict[str, list[tuple[Mapping[str, Any], str]]], set[str]]:
    """A set's label rows (`labels`, `_label_rows`) as its chain recorded them (LP-11p): by
    each case's hash (`cases`, every case the set holds), its label rows the chain recorded,
    each with the round its `evalset.labelled` chained (never an assignment row's); and the
    hashes of the cases missing a label the chain recorded (a row deleted) or holding a label
    row it never recorded. Opens nothing."""
    hashes = {str(case["id"]): str(case["case_hash"]) for case in cases}
    held = {str(row["id"]) for row in labels}
    unheld = {hashed for label, (hashed, _, _) in chain.labels.items() if label not in held}
    given: dict[str, list[tuple[Mapping[str, Any], str]]] = {}
    for row in labels:
        hashed = hashes.get(str(row["case_id"]), "")  # "": a label of no case the set holds
        chained = chain.labels.get(str(row["id"]))
        if chained is None or chained[0] != hashed or chained[1] not in ROUNDS:
            unheld.add(hashed)  # a label row the chain never recorded, for this case
            continue
        given.setdefault(hashed, []).append((row, chained[1]))
    return given, unheld


def _shape_mismatches(
    cases: Sequence[Mapping[str, Any]], labels: Sequence[Mapping[str, Any]], chain: _Chain
) -> list[tuple[Mapping[str, Any], str]]:
    """Each of a set's cases with what of its row its chain does not allow, opening nothing
    (what `assign` and `adjudicate` check before they choose cases by status, LP-11p; threat
    model I13: their routes open no sealed value): `labels` (`_labels_as_chained`), and
    `status`, a status its draw and the number of labels of each round the chain recorded do not
    allow (`shaped_statuses`). It cannot tell `agreed` from `disputed`, nor `resolved` from
    `unresolved`: only the labels' values do (`_mismatches`)."""
    given, unheld = _labels_as_chained(cases, labels, chain)
    found: list[tuple[Mapping[str, Any], str]] = []
    for case in cases:
        hashed = str(case["case_hash"])
        rounds = [round_ for _, round_ in given.get(hashed, [])]
        drawn = None if chain.drawn is None else hashed in chain.drawn
        allowed = shaped_statuses(drawn, rounds.count(FIRST), rounds.count(ADJUDICATION))
        found += [(case, "labels")] if hashed in unheld else []
        found += [] if str(case["status"]) in allowed else [(case, "status")]
    return found


def _mismatches(
    keys: EvalsetKeys,
    set_id: str,
    cases: Sequence[Mapping[str, Any]],
    labels: Sequence[Mapping[str, Any]],
    chain: _Chain,
) -> list[tuple[Mapping[str, Any], str]]:
    """Each of a set's cases (`cases`, every one it holds) with what of its row the commitments
    do not give: (its row, what). `labels` (`_labels_as_chained`); then, from the labels the
    chain recorded, each in its chained round, `status` and `weight` (`row_mismatches`, against
    the latest draw). Attributes and those labels (`labels`: the set's label rows,
    `_label_rows`) are opened with `keys`; a value it does not open raises `MendariError`."""
    given, unheld = _labels_as_chained(cases, labels, chain)
    found: list[tuple[Mapping[str, Any], str]] = []
    for case in cases:
        case_id, hashed = str(case["id"]), str(case["case_hash"])
        body, _ = keys.open("attributes", str(case["attributes"]), set_id, case_id)
        mine = given.get(hashed, [])
        opened = _opened_labels(keys, set_id, [row for row, _ in mine])
        rounds = [round_ for _, round_ in mine]
        first = [values for (_, values), r in zip(opened, rounds, strict=True) if r == FIRST]
        third = [values for (_, values), r in zip(opened, rounds, strict=True) if r != FIRST]
        problems = row_mismatches(set_id, case, body, first, third, chain.drawn)
        found += [(case, what) for what in (["labels"] if hashed in unheld else []) + problems]
    return found


def _refused(set_id: str, counted: Mapping[str, int], done: str) -> MendariError:
    """Why a set is refused (`done`: what was not done) when its rows are not those the
    commitments give (`_mismatches`, `_shape_mismatches`): how many cases of each kind, naming
    no case."""
    kinds = (
        ("labels", "whose labels are not those its chain recorded"),
        ("status", "with a status its labels and draw do not give"),
        ("weight", "with a weight its draw's commitment does not cover"),
    )
    said = " and ".join(
        f"{counted[kind]} case(s) {what}" for kind, what in kinds if counted.get(kind)
    )
    return MendariError(
        f"evaluation set {set_id} holds {said}: nothing was {done}; "
        "run `mendari audit verify --evalsets`"
    )


def _sealed_aggregates(keys: EvalsetKeys, set_id: str, aggregates: Any) -> tuple[str, str]:
    """A set's aggregates sealed with a new 32-byte salt beside them, and their commitment
    (`aggregates_ref`), for the event that stores them to chain (LP-11p)."""
    salt = secrets.token_bytes(32)
    wrapped = {"salt": base64.b64encode(salt).decode(), "aggregates": aggregates}
    return keys.seal("aggregates", wrapped, set_id), aggregates_ref(salt, set_id, aggregates)


def stored_aggregates(set_id: str, value: Any, chained: Mapping[str, Any] | None) -> Any:
    """A set's stored aggregates, from its opened `aggregates` value (None: none stored) and
    the latest event that stored them (`chained`: an `evalset.measured` or `evalset.frozen`
    payload, None for none), as `show` reads them and `verify` and `check` check them
    (LP-11p): the aggregates its commitment (`aggregates_ref`) covers; as they are, in the shape
    they were sealed in then, when that event is from before the commitments; None when none
    are stored and none were committed to. Aggregates the commitment does not cover (re-sealed,
    or put back from an earlier copy), gone though committed to, or stored without an event
    raise `MendariError`, naming the set only."""
    stored = chained if isinstance(chained, Mapping) else None
    ref = stored.get("aggregates_ref") if stored is not None else None
    if value is None and ref is None:
        return None
    if value is not None and stored is not None and ref is None:  # from before the commitments
        if isinstance(value, Mapping) and value.get("format") == AGGREGATES_FORMAT:
            return value
    elif value is not None and ref is not None:
        try:
            salt = base64.b64decode(str(value["salt"]), validate=True)
            if aggregates_ref(salt, set_id, value["aggregates"]) == ref:
                return value["aggregates"]
        except (KeyError, TypeError, ValueError, MendariError):
            pass
    raise MendariError(
        f"evaluation set {set_id}'s stored aggregates are not those its chain committed to "
        "when they were stored: nothing is shown; run `mendari audit verify --evalsets`"
    )
