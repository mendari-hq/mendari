# SPDX-License-Identifier: Apache-2.0
"""Grading rounds of written output (ADR-0025): clinicians grade, blind, a sample of what an
agent wrote in one run, in the console; closing the round computes the report and erases the
texts.

A round is made from a run (`evals/written.py` `run`): the run's ledger (hashes and the judge's
grades, no text) is kept whole, and a seeded sample of its outputs is kept to grade, each text
with the context a grader reads beside it. Those follow notes (`core/phi.py`): sealed under the
PHI key in mode `encrypt`, bound to their own row; kept in the clear in `off`, never in a
protected database; refused in `hash`, which keeps no patient text.

Each sampled item goes to one grader, and a seeded share of them (one in four unless said) also
to a second, different grader. Graders hold the `labeller` role, and whoever administers the
organization is never assigned (`accounts.CURATING`: separation of duties).

Blindness:

- A grader's next item (`next_item`) is the assignment's id, the rubric's text, the context and
  the written text. Nothing else: not the judge's grade, not another grader's, not the agent's
  answers, not which items are graded twice.
- The judge's grades are in the round's ledger, which no grader's read opens.
- One submission per assignment, never changed (`record_grades`). The answer is how many
  remain, never how the grade compares.
- Nothing compares anyone until the round closes: the report is computed by `close` and exists
  nowhere before.

Chained under `written:<round id>`, each naming the round's organization: the round's making,
each assignment, each showing of an item to its grader (before it is returned), each grade as a
salted commitment, the closing and any erasure by retention. Ids, hashes and counts only.

Closing (`close`) ends the open assignments, computes the report from the ledger and the grades
(grader: the person's user id), stores it, and erases every text the round still keeps. Grades
and ids remain.

What a round stores is held to what the chain committed to. The round's seed is fixed when it
is made and chained (`written.round_created`), so nobody chooses the bootstrap's draws once
grades exist. A closed round's report is served only while its hash is the one its
`written.round_closed` chained (`rounds`), and `AuditStore.verify` checks every closed round's
report and every stored grade's commitment and grader (`check_stored`)."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import secrets
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from mendari.core import accounts
from mendari.core.audit import AuditStore, _add_usage, _canonical, _now
from mendari.errors import LabelError, MendariError, NoAssignment
from mendari.evals import stats, written

if TYPE_CHECKING:
    from mendari.core.agent import Agent
    from mendari.core.db import Session

OPEN, CLOSING, CLOSED = "open", "closing", "closed"
MAX_ITEMS = 2_000  # the outputs one round keeps to grade
DOUBLE = 0.25  # the share of a round's items a second grader also grades, unless said
_GRADE_PREFIX = b"mendari-written-grade-ref-v1\x00"
_INSERT_BATCH = 200


class NoRound(MendariError):
    """A grading round that is unknown, or another organization's: one answer for both."""


NO_ROUND = "no such grading round"
# What a round says of itself where its state needs a word (`rounds`, `progress`).
CLOSING_NOTE = (
    "closing: its assignments are ended and its report is not stored yet. Run `mendari written "
    "round close` on it again to finish; until then it takes no assignment and no grade"
)
REPORT_MISMATCH = "its stored report does not match the audit trail, so it is not shown"
# One refusal for an assignment that is unknown, another grader's, another organization's or no
# longer open (`NoAssignment`, a `404` on a grader's route, as an id that names nothing is).
NO_ASSIGNMENT = "no open assignment of this labeller has that id"


def check_user_id(user: Any) -> str:
    """`user` when it is a person's user id (`accounts.person_id`); a `MendariError` that never
    repeats what was given otherwise."""
    if not (isinstance(user, str) and accounts.USER_ID.fullmatch(user)):
        raise MendariError(
            "a labeller is named by their user id (user: and 16 hex digits), as the members "
            "list shows it"
        )
    return user


def user_ids(labellers: Sequence[str], *, at_least: int) -> list[str]:
    """The labellers given, each a user id, sorted and each once; at least `at_least` of them."""
    if isinstance(labellers, str):
        labellers = [labellers]
    users = sorted({check_user_id(user) for user in labellers})
    if len(users) < at_least:
        raise MendariError(f"give at least {at_least} different labellers (their user ids)")
    return users


def labeller_membership(db: Session, org: str, user_id: str) -> Mapping[str, Any] | None:
    """`user_id`'s membership of `org` in force when it holds the `labeller` role and no role
    that administers the organization (`accounts.CURATING`); None otherwise."""
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


def labeller_in(db: Session, org: str, user: str) -> str:
    """The id of the membership `user` grades `org`'s rounds on; a `MendariError` saying why not:
    they administer `org` (separation of duties), or hold no labeller role there."""
    row = db.one(
        "SELECT id, roles FROM memberships WHERE org = ? AND user_id = ? AND removed_at IS NULL",
        (org, user),
    )
    roles = set(json.loads(row["roles"])) if row is not None else set()
    if roles & set(accounts.CURATING):
        raise MendariError(
            f"{user} administers {org}: who manages the organization is never assigned its items"
        )
    if row is None or accounts.LABELLER not in roles:
        raise MendariError(f"{user} holds no labeller role on a membership in force in {org}")
    return str(row["id"])


def report_hash(report: Any) -> str | None:
    """The hash of a stored report's result, recomputed from the report itself (everything but
    its `run` and its own `result_sha256`); None for what is no report."""
    if not isinstance(report, dict):
        return None
    found = {key: value for key, value in report.items() if key not in ("run", "result_sha256")}
    try:
        return written.result_sha256(found)
    except (TypeError, ValueError):
        return None


def check_stored(
    events: Sequence[Mapping[str, Any]],
    rounds: Mapping[str, Mapping[str, Any]],
    assignments: Mapping[str, Mapping[str, Any]],
    items: Mapping[str, str],
) -> tuple[int | None, dict[str, int]]:
    """What the rounds' rows hold, against what the chain committed to (`AuditStore.verify`):
    each stored grade against the commitment its `written.graded` chained, and its round, item
    and grader against that event's, each closed round's stored report against the hash its
    `written.round_closed` chained, and no stored grade or closed round without its event.
    `events`: the chain's `written.graded` and `written.round_closed`, in order; `rounds` and
    `assignments` by id; `items`: an item row's id to its item id. The `seq` of the first event
    its row does not match (0: a row no event accounts for), or None; and what was checked,
    counted."""
    counts = {"rounds": 0, "grades": 0}
    graded: set[str] = set()
    closed: set[str] = set()
    for event in events:
        payload = json.loads(event["payload"])
        seq = int(event["seq"])
        if event["event"] == "written.graded":
            assignment = str(payload.get("assignment"))
            row = assignments.get(assignment)
            ref = payload.get("grade_ref") or {}
            try:
                body = json.loads(row["grades"]) if row and row["grades"] else None
                salt = base64.b64decode(str(body["salt"]), validate=True) if body else None
            except (ValueError, TypeError, KeyError):
                return seq, counts
            if body is None or salt is None or row is None:
                return seq, counts
            item = items.get(str(row["item_id"]), "")
            said = grade_ref(salt, str(row["round_id"]), item, assignment, body.get("values") or {})
            # The commitment holds no grader: whose grade it is, is the event's own word.
            chained = (payload.get("round"), payload.get("item"), payload.get("grader"))
            if said != ref.get("sha256") or chained != (row["round_id"], item, row["user_id"]):
                return seq, counts
            graded.add(assignment)
            counts["grades"] += 1
        else:
            round_id = str(payload.get("round"))
            row = rounds.get(round_id)
            try:
                stored = json.loads(row["report"]) if row and row["report"] else None
            except ValueError:
                return seq, counts
            if row is None or row["state"] != CLOSED:
                return seq, counts
            if report_hash(stored) != payload.get("report_sha256"):
                return seq, counts
            closed.add(round_id)
            counts["rounds"] += 1
    unchained = any(
        row["grades"] is not None and key not in graded for key, row in assignments.items()
    )
    unclosed = any(
        (row["state"] == CLOSED or row["report"] is not None) and key not in closed
        for key, row in rounds.items()
    )
    return (0 if unchained or unclosed else None), counts


def grade_ref(
    salt: bytes, round_id: str, item: str, assignment_id: str, values: Mapping[str, str]
) -> str:
    """What the chain holds of one grade: a SHA-256, under the grade's own salt, of the round,
    the item, the assignment and the levels given. It commits to the grade without showing it."""
    said = {"round": round_id, "item": item, "assignment": assignment_id, "values": dict(values)}
    return hashlib.sha256(_GRADE_PREFIX + salt + _canonical(said).encode()).hexdigest()


def rubric_header(agent: Agent, rubric_name: str) -> dict[str, Any]:
    """What a round keeps of its rubric, in the clear: the manifest's own words, as a grader
    reads them (`next_item`), and the hash grades are bound to."""
    manifest = agent.manifest
    rubric = manifest.rubrics[rubric_name]
    output = manifest.outputs[rubric.output]
    return {
        "rubric": rubric_name,
        "rubric_sha256": written.rubric_sha256(manifest, rubric_name),
        "output": {"name": rubric.output, "description": output.description},
        "context_fields": list(rubric.context_fields),
        "criteria": {
            key: {
                "type": criterion.type,
                "instructions": criterion.instructions,
                "display": criterion.display,
                "names": dict(criterion.names),
                "levels": [[level, criterion.criteria[level]] for level, _ in criterion.scale()],
                "pass_from": criterion.passing(),
                "must_pass": criterion.must_pass,
            }
            for key, criterion in rubric.criteria.items()
        },
    }


def _grades_problem(values: Any, criteria: Mapping[str, Sequence[str]]) -> str | None:
    """Why `values` are not grades of a rubric with `criteria` (each with its levels): a
    criterion it does not have, one ungraded, or a grade not among a criterion's levels. It
    names criteria (the manifest's own), never a value given."""
    if not isinstance(values, Mapping):
        return "grades are a mapping of each criterion to one of its levels"
    if any(key not in criteria for key in values):
        return "grades name a criterion this rubric does not have"
    ungraded = [key for key in criteria if key not in values]
    if ungraded:
        return f"grades answer every criterion: {', '.join(ungraded)} ungraded"
    for key, levels in criteria.items():
        given = values[key]
        if not isinstance(given, str) or given not in levels:
            return f"grades.{key} is not one of its levels"
    return None


def _ranked(seed: int, *parts: str) -> str:
    """A seeded rank for a draw: the same on every machine and Python, whatever its `random`."""
    return hashlib.sha256("\x00".join((str(seed), *parts)).encode()).hexdigest()


def _is_uuid(value: Any) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


class WrittenStore:
    """Grading rounds over an `AuditStore`: its database, its chain and its PHI policy."""

    def __init__(self, store: AuditStore):
        self.store = store

    # -- the operator's -------------------------------------------------------------------------

    def create_round(
        self,
        org: str,
        agent: Agent,
        rubric_name: str,
        run: written.WrittenRun,
        *,
        sample: int,
        seed: int = 0,
        created_by: str,
    ) -> dict[str, Any]:
        """A new open round of `org` from one run of `agent` on its rubric: `sample` of the run's
        outputs, drawn with `seed` and kept in the run's order, each with its context; the
        run's ledger whole. `seed` is the round's from then on, chained with its making: its
        assignments' draws and its report's bootstrap use it, so neither is chosen later. The
        run's remote calls go to the organization's usage ledger. Refused: PHI mode `hash`; a
        run of another agent or another wording of the rubric; a sample of no item, or of more
        than the run has (or than `MAX_ITEMS`). Returns ids and counts."""
        try:
            org = accounts.check_org_id(org)
        except ValueError as error:
            raise MendariError(str(error)) from None
        phi = self.store.phi
        if phi.mode == "hash":
            raise MendariError(
                "PHI mode hash keeps no patient text: a round keeps the texts to grade; switch "
                "to encrypt"
            )
        if phi.mode == "off":
            self.store.refuse_plaintext()
        if rubric_name not in agent.manifest.rubrics:
            raise MendariError(f"{agent.id} has no rubric {rubric_name!r}")
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise MendariError("the seed is a whole number, 0 or more")
        header = {**rubric_header(agent, rubric_name), "seed": seed}
        said = run.ledger.header
        if said["agent"].get("digest") != agent.digest or said["rubric"].get("name") != rubric_name:
            raise MendariError("the run is of another agent or rubric than the round's")
        if said["rubric"].get("sha256") != header["rubric_sha256"]:
            raise MendariError("the run was made under another wording of the rubric")
        if isinstance(sample, bool) or not isinstance(sample, int) or sample < 1:
            raise MendariError("a round samples 1 output or more")
        if sample > len(run.items):
            raise MendariError(
                f"the run has {len(run.items)} outputs: a round samples at most that many"
            )
        if sample > MAX_ITEMS:
            raise MendariError(f"a round keeps at most {MAX_ITEMS:,} outputs to grade")
        known = {case["i"] for case in run.ledger.cases if case["i"] is not None}
        if any(item.item not in known for item in run.items):
            raise MendariError("the run's outputs are not those of its ledger")
        ranked = sorted(run.items, key=lambda item: _ranked(seed, item.item))
        drawn = {item.item for item in ranked[:sample]}
        chosen = [item for item in run.items if item.item in drawn]  # the run's order
        round_id, now = str(uuid.uuid4()), _now()
        ledger = run.ledger.dumps().decode("utf-8")
        sealing = phi.mode == "encrypt"
        rows = []
        for item in chosen:
            row_id = str(uuid.uuid4())
            body = {"written": item.text, "context": item.context}
            kept = phi.seal("written", body, row_id) if sealing else _canonical(body)
            rows.append((row_id, round_id, org, item.item, kept, int(sealing), now))
        with self.store._db.session(exclusive=True) as db:
            if phi.mode == "off":
                self.store._refuse_plaintext(db)
            else:
                self.store._refuse_retired(db)
            db.all(
                "INSERT INTO written_rounds (id, org, agent_id, agent_digest, rubric, "
                "rubric_sha256, header, ledger, state, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    round_id,
                    org,
                    agent.id,
                    agent.digest,
                    rubric_name,
                    header["rubric_sha256"],
                    _canonical(header),
                    ledger,
                    OPEN,
                    created_by,
                    now,
                ),
            )
            for start in range(0, len(rows), _INSERT_BATCH):
                batch = rows[start : start + _INSERT_BATCH]
                marks = ", ".join("(?, ?, ?, ?, ?, ?, ?)" for _ in batch)
                db.all(
                    "INSERT INTO written_items (id, round_id, org, item, body, sealed, "
                    f"created_at) VALUES {marks}",
                    [value for row in batch for value in row],
                )
            _add_usage(db, org, "evaluation", run.usage)
            payload = {
                "round": round_id,
                "agent_id": agent.id,
                "agent_digest": agent.digest,
                "rubric": rubric_name,
                "rubric_sha256": header["rubric_sha256"],
                "ledger": run.ledger.id,
                "items": len(rows),
                "seed": seed,
            }
            chained = self.store._bound(payload, org)
            self.store._append(db, f"written:{round_id}", "written.round_created", chained)
        return {
            "round": round_id,
            "items": len(rows),
            "ledger": run.ledger.id,
            "state": OPEN,
            "seed": seed,
        }

    def _seed(self, db: Session, round_id: str) -> int:
        """The round's seed, as its making chained it (0 for a round chained without one)."""
        row = db.one(
            "SELECT payload FROM audit_events WHERE decision_id = ? AND event = ? "
            "ORDER BY seq LIMIT 1",
            (f"written:{round_id}", "written.round_created"),
        )
        seed = json.loads(row["payload"]).get("seed") if row is not None else None
        return seed if isinstance(seed, int) and not isinstance(seed, bool) and seed >= 0 else 0

    def _unchained(self, db: Session, round_id: str) -> bool:
        """Whether the round's stored grades are not the ones its `written.graded` events
        chained (`check_stored`, the round's rows only): a grade changed, moved, given to
        another grader, or stored with no event."""
        events = db.all(
            "SELECT seq, event, payload FROM audit_events WHERE decision_id = ? AND event = ? "
            "ORDER BY seq",
            (f"written:{round_id}", "written.graded"),
        )
        assignments = {
            str(row["id"]): row
            for row in db.all(
                "SELECT id, round_id, item_id, user_id, grades FROM written_assignments "
                "WHERE round_id = ?",
                (round_id,),
            )
        }
        items = {
            str(row["id"]): str(row["item"])
            for row in db.all("SELECT id, item FROM written_items WHERE round_id = ?", (round_id,))
        }
        broken, _ = check_stored(events, {}, assignments, items)
        return broken is not None

    def assign(
        self,
        round_id: str,
        graders: Sequence[str],
        *,
        double: float = DOUBLE,
        org: str | None = None,
    ) -> dict[str, Any]:
        """Give an open round's items to `graders` (user ids): one grader an item, and
        `ceil(double x items)` seeded items a second, different one. Loads are balanced, ties by
        a seeded rank; the seed is the round's own (`create_round`). Each grader holds the
        `labeller` role on a membership in force in the round's organization and no role that
        administers it; a second grader needs two graders. Open assignments whose membership
        no longer holds are withdrawn first, and their items given again. An item that already
        has its graders gets none. Returns ids and counts, never which items are graded twice."""
        if isinstance(double, bool) or not isinstance(double, int | float):
            raise MendariError("`double` is a share, 0 to 1")
        if not (math.isfinite(double) and 0.0 <= double <= 1.0):
            raise MendariError("`double` is a share, 0 to 1")
        users = user_ids(graders, at_least=2 if double > 0 else 1)
        with self.store._db.session(exclusive=True) as db:
            found = self._round(db, round_id, org)
            if found["state"] != OPEN:
                raise MendariError(f"round {round_id} is {found['state']}: nothing is assigned")
            owner = str(found["org"])
            seed = self._seed(db, round_id)
            withdrawn = self._withdraw(db, round_id, owner)
            memberships = {user: labeller_in(db, owner, user) for user in users}
            items = db.all(
                "SELECT id, item FROM written_items WHERE round_id = ? AND body IS NOT NULL "
                "ORDER BY id",
                (round_id,),
            )
            holders: dict[str, list[str]] = {str(row["id"]): [] for row in items}
            loads: Counter[str] = Counter({user: 0 for user in users})
            for row in db.all(
                "SELECT item_id, user_id FROM written_assignments WHERE round_id = ? "
                "AND (ended_at IS NULL OR grades IS NOT NULL)",
                (round_id,),
            ):
                if str(row["item_id"]) in holders:
                    holders[str(row["item_id"])].append(str(row["user_id"]))
                if str(row["user_id"]) in loads:
                    loads[str(row["user_id"])] += 1
            hashes = {str(row["id"]): str(row["item"]) for row in items}
            ranked = sorted(holders, key=lambda row_id: _ranked(seed, "double", hashes[row_id]))
            twice = set(ranked[: math.ceil(double * len(ranked))])
            now = _now()
            made: list[tuple[str, str, str]] = []  # (assignment id, item row id, grader)
            for row_id in sorted(
                holders, key=lambda row_id: _ranked(seed, "order", hashes[row_id])
            ):
                wanted = 2 if row_id in twice else 1
                while len(holders[row_id]) < wanted:
                    free = [user for user in users if user not in holders[row_id]]
                    if not free:
                        break
                    grader = min(
                        free,
                        key=lambda user: (loads[user], _ranked(seed, hashes[row_id], user)),
                    )
                    made.append((str(uuid.uuid4()), row_id, grader))
                    holders[row_id].append(grader)
                    loads[grader] += 1
            for start in range(0, len(made), _INSERT_BATCH):
                batch = made[start : start + _INSERT_BATCH]
                marks = ", ".join("(?, ?, ?, ?, ?, ?, ?)" for _ in batch)
                db.all(
                    "INSERT INTO written_assignments (id, org, round_id, item_id, user_id, "
                    f"membership_id, created_at) VALUES {marks}",
                    [
                        value
                        for assignment, row_id, grader in batch
                        for value in (
                            assignment,
                            owner,
                            round_id,
                            row_id,
                            grader,
                            memberships[grader],
                            now,
                        )
                    ],
                )
            entries = [
                (
                    f"written:{round_id}",
                    self.store._bound(
                        {
                            "round": round_id,
                            "item": hashes[row_id],
                            "assignment": assignment,
                            "grader": grader,
                        },
                        owner,
                    ),
                )
                for assignment, row_id, grader in made
            ]
            self.store._append_many(db, "written.assigned", entries)
        given = Counter(grader for _, _, grader in made)
        return {
            "round": round_id,
            "assigned": len(made),
            "withdrawn": withdrawn,
            "graders": {user: given[user] for user in users},
        }

    def _withdraw(self, db: Session, round_id: str, org: str) -> int:
        """End a round's open assignments whose membership no longer holds (ended, replaced, or
        no longer a labeller's without an administrator's role): their items are then given to
        others."""
        stale = [
            str(row["id"])
            for row in db.all(
                "SELECT id, user_id, membership_id FROM written_assignments "
                "WHERE round_id = ? AND ended_at IS NULL AND grades IS NULL",
                (round_id,),
            )
            if (held := labeller_membership(db, org, str(row["user_id"]))) is None
            or str(held["id"]) != row["membership_id"]
        ]
        now = _now()
        for start in range(0, len(stale), _INSERT_BATCH):
            batch = stale[start : start + _INSERT_BATCH]
            marks = ", ".join("?" for _ in batch)
            db.all(
                f"UPDATE written_assignments SET ended_at = ? WHERE id IN ({marks})", (now, *batch)
            )
        return len(stale)

    def _round(self, db: Session, round_id: str, org: str | None) -> dict[str, Any]:
        """A round's row, if it is `org`'s (any, if None: the operator's command line)."""
        row = None
        if _is_uuid(round_id):
            row = db.one("SELECT * FROM written_rounds WHERE id = ?", (round_id,))
        if row is None or (org is not None and row["org"] != org):
            raise NoRound(NO_ROUND)
        return dict(row)

    def close(
        self,
        round_id: str,
        *,
        by: str,
        org: str | None = None,
        resamples: int = stats.RESAMPLES,
    ) -> dict[str, Any]:
        """Close an open round: end its open assignments, compute the report
        (`evals/written.py` `report`) from the round's ledger and the grades given (each
        grader named by user id), store it, and erase every text the round still keeps. Chained
        (`written.round_closed`: how many grades, how many texts erased, the report's hash, who
        closed it). A round already closed is refused. Three steps, so the chain's lock is not
        held while the report is computed: the round is `closing` between the first and the
        last, which takes no assignment and no grade; a `closing` round is closed by running
        this again. Returns the stored report.

        Every argument is checked before the first step, which cannot be undone: fewer than
        `stats.RESAMPLES` resamples is refused (a state is never decided on a handful of
        draws), and so is a `by` that names no one. The bootstrap's seed is the round's own,
        chained when it was made: closing takes none."""
        problem = written.resamples_problem(resamples)
        if problem is not None:
            raise MendariError(problem)
        if not isinstance(by, str) or not by.strip():
            raise MendariError("closing a round names who closes it")
        with self.store._db.session(exclusive=True) as db:
            found = self._round(db, round_id, org)
            if found["state"] == CLOSED:
                raise MendariError(f"round {round_id} is already closed")
            owner = str(found["org"])
            seed = self._seed(db, round_id)
            # Read as a ledger before anything changes: one that is not one closes nothing.
            ledger = written.WrittenLedger.loads(
                str(found["ledger"]).encode("utf-8"), where=f"round {round_id}"
            )
            # The report is built from the rows' grades and its hash then chained, so a report
            # of grades edited in the database would be served as the round's own: the round's
            # grades are checked against their events first, as `verify` checks them.
            if self._unchained(db, round_id):
                raise MendariError(
                    f"round {round_id} is not closed: its stored grades are not the ones the audit "
                    "trail holds (run `mendari audit verify`)"
                )
            now = _now()
            db.all(
                "UPDATE written_assignments SET ended_at = ? WHERE round_id = ? "
                "AND ended_at IS NULL",
                (now, round_id),
            )
            db.all("UPDATE written_rounds SET state = ? WHERE id = ?", (CLOSING, round_id))
            graded = db.all(
                "SELECT a.user_id, a.grades, i.item FROM written_assignments a "
                "JOIN written_items i ON i.id = a.item_id WHERE a.round_id = ? "
                "AND a.grades IS NOT NULL ORDER BY a.created_at, a.id",
                (round_id,),
            )
        grades = written.Grades(
            str(found["rubric_sha256"]),
            tuple(
                written.Grade(
                    str(row["item"]), str(row["user_id"]), json.loads(row["grades"])["values"]
                )
                for row in graded
            ),
        )
        report = written.run_report(
            written.report(ledger, grades, seed=seed, resamples=resamples), ledger
        )
        stored = _canonical(report)
        with self.store._db.session(exclusive=True) as db:
            again = self._round(db, round_id, org)
            if again["state"] != CLOSING:
                raise MendariError(f"round {round_id} is {again['state']}: it was closed meanwhile")
            now = _now()
            erased = db.all(
                "UPDATE written_items SET body = NULL, erased_at = ? WHERE round_id = ? "
                "AND body IS NOT NULL RETURNING id",
                (now, round_id),
            )
            db.all(
                "UPDATE written_rounds SET state = ?, closed_at = ?, report = ? WHERE id = ?",
                (CLOSED, now, stored, round_id),
            )
            payload = {
                "round": round_id,
                "graded": len(graded),
                "erased": len(erased),
                "report_sha256": report["result_sha256"],
                "by": by,
            }
            chained = self.store._bound(payload, owner)
            self.store._append(db, f"written:{round_id}", "written.round_closed", chained)
        return {
            "round": round_id,
            "state": CLOSED,
            "graded": len(graded),
            "erased": len(erased),
            "report": report,
        }

    # -- reads: ids and counts ---------------------------------------------------------------------

    def progress(self, round_id: str, *, org: str | None = None) -> dict[str, Any]:
        """A round's labelling so far: its state, its items (and how many texts are erased), its
        assignments by state, and each grader's by user id. Counts only: no grade, and never
        which items have two graders."""
        with self.store._db.session(exclusive=False) as db:
            found = self._round(db, round_id, org)
            items = db.all("SELECT id, body FROM written_items WHERE round_id = ?", (round_id,))
            rows = db.all(
                "SELECT item_id, user_id, ended_at, grades FROM written_assignments "
                "WHERE round_id = ?",
                (round_id,),
            )
        graders: dict[str, dict[str, int]] = {}
        graded_items: Counter[str] = Counter()
        for row in rows:
            own = graders.setdefault(str(row["user_id"]), {"graded": 0, "open": 0, "withdrawn": 0})
            if row["grades"] is not None:
                own["graded"] += 1
                graded_items[str(row["item_id"])] += 1
            elif row["ended_at"] is None:
                own["open"] += 1
            else:
                own["withdrawn"] += 1
        return {
            "round": round_id,
            "state": str(found["state"]),
            "note": CLOSING_NOTE if found["state"] == CLOSING else None,
            "items": len(items),
            "erased": sum(1 for item in items if item["body"] is None),
            "items_graded": len(graded_items),
            "assignments": {
                state: sum(own[state] for own in graders.values())
                for state in ("graded", "open", "withdrawn")
            },
            "graders": dict(sorted(graders.items())),
        }

    def rounds(self, org: str | None, agent_id: str | None = None) -> list[dict[str, Any]]:
        """`org`'s rounds (every organization's, if None: the command line), oldest first,
        optionally one agent's: ids, the rubric and its hash, the state, times, counts and,
        once closed, the stored report. Never a text, a context or a single grade.

        A closed round's report is served only while it is the one the round's closing chained:
        its hash is recomputed from the stored report and compared with the
        `written.round_closed` event's. One that differs (edited in the database) is not
        served: `report` is null and `note` says why. A round left `closing` says so in `note`
        too, with how to finish it."""
        with self.store._db.session(exclusive=False) as db:
            where, params = [], []
            if org is not None:
                where.append("r.org = ?")
                params.append(org)
            if agent_id is not None:
                where.append("r.agent_id = ?")
                params.append(agent_id)
            rows = db.all(
                "SELECT r.id, r.org, r.agent_id, r.agent_digest, r.rubric, r.rubric_sha256, "
                "r.state, r.created_by, r.created_at, r.closed_at, r.report, "
                "(SELECT count(*) FROM written_items i WHERE i.round_id = r.id) AS items, "
                "(SELECT count(*) FROM written_assignments a WHERE a.round_id = r.id "
                "AND a.grades IS NOT NULL) AS graded, "
                "(SELECT count(*) FROM written_assignments a WHERE a.round_id = r.id "
                "AND a.grades IS NULL AND a.ended_at IS NULL) AS waiting "
                "FROM written_rounds r"
                + (f" WHERE {' AND '.join(where)}" if where else "")
                + " ORDER BY r.created_at, r.id",
                params,
            )
            chained = {
                str(row["decision_id"]).removeprefix("written:"): json.loads(row["payload"]).get(
                    "report_sha256"
                )
                for row in db.all(
                    "SELECT decision_id, payload FROM audit_events WHERE event = ? ORDER BY seq",
                    ("written.round_closed",),
                )
            }
        listed = []
        for row in rows:
            report = None if row["report"] is None else json.loads(row["report"])
            note = CLOSING_NOTE if row["state"] == CLOSING else None
            if report is not None and report_hash(report) != chained.get(str(row["id"])):
                report, note = None, REPORT_MISMATCH
            listed.append((row, report, note))
        return [
            {
                "id": str(row["id"]),
                "org": str(row["org"]),
                "agent_id": str(row["agent_id"]),
                "agent_digest": str(row["agent_digest"]),
                "rubric": str(row["rubric"]),
                "rubric_sha256": str(row["rubric_sha256"]),
                "state": str(row["state"]),
                "created_by": str(row["created_by"]),
                "created_at": str(row["created_at"]),
                "closed_at": row["closed_at"],
                "items": int(row["items"]),
                "graded": int(row["graded"]),
                "open": int(row["waiting"]),
                "note": note,
                "report": report,
            }
            for row, report, note in listed
        ]

    # -- a grader's -------------------------------------------------------------------------------

    def next_item(self, user_id: str, *, org: str) -> dict[str, Any] | None:
        """What a grader grades next: their oldest open assignment in an open round of `org`,
        the organization they act for, while the membership it was made on is in force as a
        labeller's. The item's text and context are opened, and `written.item_read` is chained
        in the same transaction, before anything is returned: the assignment's id, each
        criterion's levels (`questions`), the rubric's words for each (`guidance`), the written
        text with what it is, and the context (`input`). Never a judge's grade, another
        grader's, or the agent's answers. None: nothing waits for them."""
        if not (isinstance(user_id, str) and accounts.USER_ID.fullmatch(user_id)):
            return None
        with self.store._db.session(exclusive=True) as db:
            membership = labeller_membership(db, org, user_id)
            if membership is None:
                return None
            row = db.one(
                "SELECT a.id, a.round_id, i.id AS item_row, i.item, i.body, i.sealed, r.header "
                "FROM written_assignments a JOIN written_rounds r ON r.id = a.round_id "
                "JOIN written_items i ON i.id = a.item_id "
                "WHERE a.user_id = ? AND a.org = ? AND a.membership_id = ? "
                "AND a.ended_at IS NULL AND a.grades IS NULL AND r.state = ? AND r.org = a.org "
                "AND i.body IS NOT NULL ORDER BY a.created_at, a.id LIMIT 1",
                (user_id, org, str(membership["id"]), OPEN),
            )
            if row is None:
                return None
            header = json.loads(row["header"])
            if int(row["sealed"]):
                body, _ = self.store.phi.open("written", str(row["body"]), str(row["item_row"]))
            else:
                body = json.loads(row["body"])
            payload = {
                "round": str(row["round_id"]),
                "item": str(row["item"]),
                "assignment": str(row["id"]),
                "grader": user_id,
            }
            chained = self.store._bound(payload, org)
            self.store._append(db, f"written:{row['round_id']}", "written.item_read", chained)
        criteria = header["criteria"]
        return {
            "assignment": str(row["id"]),
            "questions": {key: [level for level, _ in c["levels"]] for key, c in criteria.items()},
            "guidance": {
                key: {
                    "instructions": c["instructions"],
                    "display": c.get("display"),
                    "levels": {level: anchor for level, anchor in c["levels"]},
                }
                for key, c in criteria.items()
            },
            "written": {
                "name": header["output"]["name"],
                "description": header["output"]["description"],
                "text": body["written"],
            },
            "input": body["context"],
        }

    def record_grades(
        self, assignment_id: str, user_id: str, values: Mapping[str, Any], *, org: str
    ) -> dict[str, Any]:
        """A grader's grades of one assignment, for `org`, the organization they act for. Refused
        with one message (`NoAssignment`) whether the assignment is unknown, another grader's,
        another organization's, closed (graded, or withdrawn), in a round no longer open, or on
        a membership no longer in force as a labeller's; then every criterion must be graded
        with one of its levels (a `LabelError` names the criterion, never the value). One
        submission per assignment, never changed: the levels are kept with a 32-byte salt, and
        `written.graded` is chained with their commitment. The answer is how many of the
        grader's items still wait, never how the grade compares."""
        if not (
            _is_uuid(assignment_id)
            and isinstance(user_id, str)
            and accounts.USER_ID.fullmatch(user_id)
        ):
            raise NoAssignment(NO_ASSIGNMENT)
        with self.store._db.session(exclusive=True) as db:
            row = db.one(
                "SELECT a.id, a.org, a.round_id, a.user_id, a.membership_id, a.ended_at, "
                "a.grades, r.state, r.header, i.item FROM written_assignments a "
                "JOIN written_rounds r ON r.id = a.round_id "
                "JOIN written_items i ON i.id = a.item_id WHERE a.id = ?",
                (assignment_id,),
            )
            if (
                row is None
                or row["user_id"] != user_id
                or row["org"] != org
                or row["ended_at"] is not None
                or row["grades"] is not None
                or row["state"] != OPEN
            ):
                raise NoAssignment(NO_ASSIGNMENT)
            membership = labeller_membership(db, org, user_id)
            if membership is None or str(membership["id"]) != row["membership_id"]:
                raise NoAssignment(NO_ASSIGNMENT)
            criteria = json.loads(row["header"])["criteria"]
            questions = {key: [level for level, _ in c["levels"]] for key, c in criteria.items()}
            problem = _grades_problem(values, questions)
            if problem is not None:
                raise LabelError(problem)
            given = {key: values[key] for key in questions}
            round_id, item = str(row["round_id"]), str(row["item"])
            salt = secrets.token_bytes(32)
            now = _now()
            db.all(
                "UPDATE written_assignments SET grades = ?, graded_at = ?, ended_at = ? "
                "WHERE id = ?",
                (
                    _canonical({"salt": base64.b64encode(salt).decode(), "values": given}),
                    now,
                    now,
                    assignment_id,
                ),
            )
            payload = {
                "round": round_id,
                "item": item,
                "assignment": assignment_id,
                "grader": user_id,
                "grade_ref": {
                    "id": assignment_id,
                    "sha256": grade_ref(salt, round_id, item, assignment_id, given),
                },
            }
            chained = self.store._bound(payload, org)
            self.store._append(db, f"written:{round_id}", "written.graded", chained)
            remaining = db.one(
                "SELECT count(*) AS n FROM written_assignments a "
                "JOIN written_rounds r ON r.id = a.round_id "
                "JOIN written_items i ON i.id = a.item_id WHERE a.user_id = ? AND a.org = ? "
                "AND a.membership_id = ? AND a.ended_at IS NULL AND a.grades IS NULL "
                "AND r.state = ? AND i.body IS NOT NULL",
                (user_id, org, str(membership["id"]), OPEN),
            )
        return {"remaining": int(remaining["n"]) if remaining else 0}

    def labelling(self, user_id: str, *, org: str) -> dict[str, Any]:
        """A grader's own work in `org`, round by round, oldest first: how many of their items
        wait there (`open`: as `next_item` serves them) and how many they graded (`labelled`);
        and how many wait in all (`remaining`). The shape blind labelling answers with, a round
        where it has a set, so one screen shows both. Ids and counts only: nothing is opened,
        so nothing is chained."""
        if not (isinstance(user_id, str) and accounts.USER_ID.fullmatch(user_id)):
            return {"sets": [], "remaining": 0}
        with self.store._db.session(exclusive=False) as db:
            membership = labeller_membership(db, org, user_id)
            if membership is None:
                return {"sets": [], "remaining": 0}
            rows = db.all(
                "SELECT a.round_id, a.membership_id, a.ended_at, a.grades, r.state, "
                "i.body IS NOT NULL AS kept FROM written_assignments a "
                "JOIN written_rounds r ON r.id = a.round_id "
                "JOIN written_items i ON i.id = a.item_id "
                "WHERE a.user_id = ? AND a.org = ? AND r.org = a.org ORDER BY a.created_at, a.id",
                (user_id, org),
            )
        counted: dict[str, dict[str, int]] = {}
        for row in rows:
            held = counted.setdefault(str(row["round_id"]), {"open": 0, "labelled": 0})
            if row["grades"] is not None:
                held["labelled"] += 1
            elif (
                row["ended_at"] is None
                and row["state"] == OPEN
                and row["kept"]
                and str(row["membership_id"]) == str(membership["id"])
            ):
                held["open"] += 1
        sets = [{"set": key, **held} for key, held in counted.items() if any(held.values())]
        return {"sets": sets, "remaining": sum(held["open"] for held in sets)}

    def assignment_round(self, assignment_id: str, user_id: str, *, org: str) -> str | None:
        """The round of one of `user_id`'s own assignments in `org`, open or graded: what the
        security log names of a grader's work (round ids only). None for an id that is unknown,
        another grader's or another organization's."""
        if not _is_uuid(assignment_id):
            return None
        with self.store._db.session(exclusive=False) as db:
            row = db.one(
                "SELECT round_id FROM written_assignments WHERE id = ? AND user_id = ? AND org = ?",
                (assignment_id, user_id, org),
            )
        return None if row is None else str(row["round_id"])
