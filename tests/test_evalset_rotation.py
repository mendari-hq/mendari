# SPDX-License-Identifier: Apache-2.0
"""The evaluation-set keys' lifecycle (LP-11f, `core/evalsets.py`, ADR-0011).

- `mendari evalset rotate` re-seals under the current key what another key sealed, in batches,
  each chained (`evalset.rotated`: key ids and counts), compare-and-set, in every state of a set;
  an interrupted run resumes; a key it moved values off seals nothing more. Twice in a row, by
  the documented steps, with writes between the swap and the re-seal (lesson of 2026-10-06).
- `--check` reads only: everything opens with the current key alone, each case's commitment and
  keyed hash match, and its index rows under the current index key are there.
- `mendari evalset reindex` rebuilds the hidden-case index under the current index key in one
  transaction: a reader sees the index before or after, never a mix of key ids; an index key it
  replaced indexes nothing more.
- `mendari evalset export` copies the sealed rows as stored, opening nothing, needing no key.
- Every column of an `evalset_` table is sealed (`SEALED_COLUMNS`, rotated, checked, verified and
  exported) or pinned here as clear, so a later sealed column cannot be left out.
- PHI rotation never touches evaluation-set rows, nor the reverse.
On SQLite and Postgres (`db_target`); keys are made per test, cases are synthetic."""

from __future__ import annotations

import base64
import itertools
import json
import os
import re
import stat
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from mendari import AuditStore, Decision, Pack
from mendari.core import audit as audit_module
from mendari.core import evalsets as evalsets_module
from mendari.core.audit import EVALSET_RETIRED, EVALSET_TARGET, KEYS_CHAIN, RETIRED
from mendari.core.evalset_keys import EvalsetKeys
from mendari.core.evalsets import EXPORT_FORMAT, SEALED_COLUMNS, EvalsetStore
from mendari.core.phi import PhiPolicy, key_id
from mendari.errors import MendariError
from mendari.standards import audit_fhir, fhir_structure
from tests.conftest import evalset_keys_for_test, evalset_on, hand_written, message
from tests.test_evalset_labelling import label_all, labellers
from tests.test_evalset_store import (
    CLINIC,
    INBOX,
    MARKS,
    a_set,
    cases_file,
    cases_of,
    chained,
    planted,
    run,
    served,
    tamper,
)

KEY_ID = re.compile(r"^[0-9a-f]{16}$")
# Every table the stores write that a key lifecycle might touch: what "writes nothing" compares.
EVERY_TABLE = (
    "evalset_sets",
    "evalset_cases",
    "evalset_index",
    "evalset_assignments",
    "evalset_labels",
    "audit_events",
    "settings",
    "decisions",
    "decision_inputs",
    "notes",
)
# The columns of the `evalset_` tables kept in the clear: ids, organizations, states, times, keyed
# hashes and commitments. Every other column is sealed (`SEALED_COLUMNS`). A column a later step
# adds goes in one list or the other, or this file fails.
CLEAR_COLUMNS = {
    "evalset_sets": {
        "id",
        "org",
        "state",
        "created_at",
        "created_by",
        "frozen_at",
        "commitment",
        "max_runs",
        "max_age_days",
        "retired_at",
    },
    "evalset_cases": {"id", "set_id", "org", "case_hash", "status", "created_at"},
    "evalset_index": {"hash", "variant", "key_id", "set_id", "case_id", "org"},
    # Blind labelling (LP-11h): who labels which case, in which round, and when; a label's ids
    # and time (its values are its sealed `body`).
    "evalset_assignments": {
        "id",
        "org",
        "set_id",
        "case_id",
        "user_id",
        "membership_id",
        "round",
        "created_at",
        "ended_at",
    },
    "evalset_labels": {"id", "org", "set_id", "case_id", "assignment_id", "created_at"},
}


def b64(key: bytes) -> str:
    return base64.b64encode(key).decode()


def fresh() -> bytes:
    return evalset_keys_for_test().key


def ring(current: bytes, index: bytes, *read: bytes) -> EvalsetKeys:
    return EvalsetKeys(current, index, read)


def deployed(monkeypatch: pytest.MonkeyPatch, keys: EvalsetKeys) -> None:
    """The command line's environment holds these keys, as the deployment's do."""
    monkeypatch.setenv("MENDARI_EVALSET_KEY", b64(keys.key))
    monkeypatch.setenv("MENDARI_EVALSET_INDEX_KEY", b64(keys.index_key))
    if keys.read_keys:
        monkeypatch.setenv("MENDARI_EVALSET_READ_KEYS", ",".join(b64(k) for k in keys.read_keys))
    else:
        monkeypatch.delenv("MENDARI_EVALSET_READ_KEYS", raising=False)


def everything(store: AuditStore) -> str:
    """Every row a key lifecycle might write, as text."""
    with store._db.session(exclusive=False) as db:
        return json.dumps(
            {table: db.all(f"SELECT * FROM {table} ORDER BY 1") for table in EVERY_TABLE},
            default=str,
        )


def tokens(store: AuditStore) -> dict[tuple[str, str, str], str]:
    """Every sealed value, by table, column and row id."""
    found = {}
    with store._db.session(exclusive=False) as db:
        for table, sealed in SEALED_COLUMNS.items():
            for row in db.all(f"SELECT id, {', '.join(sealed.columns)} FROM {table}"):
                for column in sealed.columns:
                    if row[column] is not None:
                        found[(table, column, str(row["id"]))] = str(row[column])
    return found


def named_keys(store: AuditStore) -> set[str]:
    """The key ids the sealed values name."""
    return {token.split(":")[1] for token in tokens(store).values()}


def events(store: AuditStore, kind: str) -> list[dict[str, Any]]:
    return [e for e in chained(store) if e["event"] == kind]


def marks(store: AuditStore) -> set[str]:
    with store._db.session(exclusive=False) as db:
        return {str(row["name"]) for row in db.all("SELECT name FROM settings")}


def index_key_ids(store: AuditStore) -> set[str]:
    with store._db.session(exclusive=False) as db:
        return {str(row["key_id"]) for row in db.all("SELECT key_id FROM evalset_index")}


def case_ids(store: AuditStore) -> list[str]:
    with store._db.session(exclusive=False) as db:
        return [str(row["id"]) for row in db.all("SELECT id FROM evalset_cases")]


@pytest.fixture
def store(db_target: str) -> Iterator[AuditStore]:
    with AuditStore(db_target) as opened:
        yield opened


# -- rotation -------------------------------------------------------------------------------------


def test_rotations_follow_one_another_by_the_documented_steps(
    store: AuditStore, inbox_pack: Pack
) -> None:
    """Old to new, then new to next, each step as docs/operations/evalsets.md has it: the reader
    first (a deployment starting with it), a write, the swap, writes between the swap and the
    re-seal (sealed under the key just swapped in, a new set, a case removed), the re-seal, the
    check, the read key dropped."""
    index = fresh()
    first, second, third = fresh(), fresh(), fresh()
    begun = EvalsetStore(store, ring(first, index))
    set_id = a_set(begun, inbox_pack)
    begun.add_cases(set_id, planted(3))
    sets = [set_id]
    for current, previous in ((second, first), (third, second)):
        reading = ring(previous, index, current)
        store.evalset_ready(reading)  # reader first: a deployment starts with it
        EvalsetStore(store, reading).add_cases(sets[0], [hand_written(f"Before {key_id(current)}")])
        swapped = EvalsetStore(store, ring(current, index, previous))  # the swap
        swapped.add_cases(sets[0], [hand_written(f"Between, under {key_id(current)}")])
        sets.append(a_set(swapped, inbox_pack))
        swapped.add_cases(sets[-1], [hand_written(f"A new set's, {key_id(current)}")])
        removed = swapped.add_cases(sets[0], [hand_written(f"Removed, {key_id(current)}")])
        swapped.remove_case(sets[0], removed["cases"][0]["case"])
        assert not swapped.check()["ready"]  # sealed under the previous key: not yet
        rotated = swapped.rotate(to=key_id(current), batch=2)
        assert rotated["unopenable"] == [] and set(rotated["from"]) == {key_id(previous)}
        assert rotated["counts"]["evalset_sets"] == len(sets) - 1  # each but the newest
        dropped = ring(current, index)  # the read key dropped
        alone = EvalsetStore(store, dropped)
        checked = alone.check()
        assert checked["ready"] and checked["failures"] == 0, checked
        assert checked["read_not_retired"] == [] and checked["current_retired"] is False
        assert store.verify(evalsets=dropped)["ok"]
        store.evalset_ready(dropped)  # the key now deployed is never the one retired
        alone.add_cases(sets[0], [hand_written(f"After, under {key_id(current)} alone")])
        assert named_keys(store) == {key_id(current)}
    assert marks(store) >= {EVALSET_RETIRED + key_id(first), EVALSET_RETIRED + key_id(second)}
    assert EVALSET_RETIRED + key_id(third) not in marks(store)
    by_key = EvalsetStore(store, ring(third, index)).check()["by_key"]
    assert set(by_key["evalset_cases"]) == set(by_key["evalset_sets"]) == {key_id(third)}
    assert store.verify(evalsets=ring(third, index))["evalsets"]["checked"] == 11


def test_rotation_works_in_batches_and_chains_key_ids_and_counts_only(
    store: AuditStore, inbox_pack: Pack
) -> None:
    index, old, new = fresh(), fresh(), fresh()
    before = EvalsetStore(store, ring(old, index))
    set_id = a_set(before, inbox_pack)
    before.add_cases(set_id, planted(5))
    store.evalset_ready(ring(old, index, new))
    hidden = EvalsetStore(store, ring(new, index, old))
    rotated = hidden.rotate(to=key_id(new), batch=2)
    assert rotated["counts"] == {"evalset_sets": 1, "evalset_cases": 5, "evalset_labels": 0}
    assert rotated["from"] == {key_id(old): 11}  # a header, 5 inputs, 5 attributes
    assert rotated["batches"] == 4  # the set; then the cases, two by two
    batches = [e for e in chained(store) if e["event"] == "evalset.rotated"]
    assert len(batches) == 4
    assert sum(e["payload"]["counts"]["evalset_cases"] for e in batches) == 5
    for event in batches:
        # The deployment's, under the keys' chain: no organization.
        assert event["decision_id"] == KEYS_CHAIN
        assert set(event["payload"]) == {"to", "from", "counts"}
        assert event["payload"]["to"] == key_id(new)
        assert all(KEY_ID.fullmatch(k) for k in event["payload"]["from"])
        assert set(event["payload"]["counts"]) == set(SEALED_COLUMNS)
    text = json.dumps(chained(store))
    for secret in (b64(old), b64(new), b64(index), *MARKS.values(), *case_ids(store)):
        assert secret not in text
    with store._db.session(exclusive=False) as db:
        target = db.one("SELECT value FROM settings WHERE name = ?", (EVALSET_TARGET,))
    assert target == {"value": key_id(new)}
    assert hidden.rotate(to=key_id(new))["batches"] == 0  # nothing left: a rerun is a no-op
    assert len(events(store, "evalset.rotated")) == 4
    administrative = [item["event"] for item in store.administrative_events(None, limit=500).rows]
    assert administrative.count("evalset.rotated") == 4  # the operator's view
    assert "evalset.rotated" not in {
        item["event"] for item in store.administrative_events(CLINIC, limit=500).rows
    }  # never an organization's


def test_an_interrupted_rotation_resumes(
    store: AuditStore, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    index, old, new = fresh(), fresh(), fresh()
    before = EvalsetStore(store, ring(old, index))
    before.add_cases(a_set(before, inbox_pack), planted(7))
    store.evalset_ready(ring(old, index, new))
    hidden = EvalsetStore(store, ring(new, index, old))
    original, calls = EvalsetStore._apply_rotation, {"n": 0}

    def fails_on_the_third(self: EvalsetStore, *args: Any, **kwargs: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("the function timed out")
        original(self, *args, **kwargs)

    monkeypatch.setattr(EvalsetStore, "_apply_rotation", fails_on_the_third)
    with pytest.raises(RuntimeError):
        hidden.rotate(to=key_id(new), batch=3)
    done = events(store, "evalset.rotated")
    assert [e["payload"]["counts"] for e in done] == [
        {"evalset_sets": 1, "evalset_cases": 0, "evalset_labels": 0},
        {"evalset_sets": 0, "evalset_cases": 3, "evalset_labels": 0},
    ]  # two batches stayed
    monkeypatch.setattr(EvalsetStore, "_apply_rotation", original)
    resumed = hidden.rotate(to=key_id(new), batch=3)
    assert resumed["counts"] == {"evalset_sets": 0, "evalset_cases": 4, "evalset_labels": 0}
    assert EvalsetStore(store, ring(new, index)).check()["ready"]
    assert store.verify(evalsets=ring(new, index))["ok"]


def test_a_row_changed_while_it_was_re_sealed_is_left_alone(
    store: AuditStore, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between the read and the write, one case is removed and another's attributes are
    re-sealed by someone else (as a second rotation would): neither is written back, and a
    rerun re-seals what is still under the old key."""
    index, old, new = fresh(), fresh(), fresh()
    before = EvalsetStore(store, ring(old, index))
    set_id = a_set(before, inbox_pack)
    added = before.add_cases(set_id, planted(3))
    store.evalset_ready(ring(old, index, new))
    keys = ring(new, index, old)
    hidden = EvalsetStore(store, keys)
    apply = EvalsetStore._apply_rotation
    gone, changed = added["cases"][1]["case"], cases_of(store)[2]
    body, _ = keys.open("attributes", changed["attributes"], set_id, changed["id"])
    elsewhere = keys.seal("attributes", body, set_id, changed["id"])  # the same value, re-sealed

    def change_two_first(self: EvalsetStore, table: str, *args: Any) -> None:
        if table == "evalset_cases":  # between the read and the write
            hidden.remove_case(set_id, gone)
            tamper(
                store,
                "UPDATE evalset_cases SET attributes = ? WHERE id = ?",
                (elsewhere, changed["id"]),
            )
        apply(self, table, *args)

    monkeypatch.setattr(EvalsetStore, "_apply_rotation", change_two_first)
    assert hidden.rotate(to=key_id(new))["counts"] == {
        "evalset_sets": 1,
        "evalset_cases": 1,
        "evalset_labels": 0,
    }
    monkeypatch.setattr(EvalsetStore, "_apply_rotation", apply)
    rows = {row["id"]: row for row in cases_of(store)}
    assert gone not in {row["case_hash"] for row in rows.values()}  # not written back
    assert rows[changed["id"]]["attributes"] == elsewhere  # not overwritten
    assert rows[changed["id"]]["input"].startswith(f"v2:{key_id(old)}:")  # left as it was
    assert hidden.rotate(to=key_id(new))["counts"] == {
        "evalset_sets": 0,
        "evalset_cases": 1,
        "evalset_labels": 0,
    }
    assert named_keys(store) == {key_id(new)}
    assert store.verify(evalsets=ring(new, index))["ok"]


def test_each_batch_checks_the_keys_again(
    store: AuditStore, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch writes only if the keys still pass, in its own transaction: once another
    rotation retired this run's key, its next batch refuses, and what earlier ones did stays."""
    index, old, new = fresh(), fresh(), fresh()
    before = EvalsetStore(store, ring(old, index))
    before.add_cases(a_set(before, inbox_pack), planted(4))
    store.evalset_ready(ring(old, index, new))
    apply, calls = EvalsetStore._apply_rotation, {"n": 0}

    def retired_after_the_first(self: EvalsetStore, *args: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 2:  # as a rotation to yet another key would
            retired = EVALSET_RETIRED + key_id(new)
            tamper(store, "INSERT INTO settings (name, value) VALUES (?, ?)", (retired, "x"))
        apply(self, *args)

    monkeypatch.setattr(EvalsetStore, "_apply_rotation", retired_after_the_first)
    hidden = EvalsetStore(store, ring(new, index, old))
    with pytest.raises(MendariError, match=f"evaluation-set key {key_id(new)} was retired"):
        hidden.rotate(to=key_id(new), batch=2)
    assert [e["payload"]["counts"] for e in events(store, "evalset.rotated")] == [
        {"evalset_sets": 1, "evalset_cases": 0, "evalset_labels": 0}
    ]


def test_a_value_no_key_opens_is_listed_and_the_rest_rotates(
    db_target: str,
    inbox_pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    index, old, new, lost = fresh(), fresh(), fresh(), fresh()
    served(db_target, inbox_pack)
    with AuditStore(db_target) as store:
        before = EvalsetStore(store, ring(old, index))
        set_id = a_set(before, inbox_pack)
        added = before.add_cases(set_id, planted(3))
        stray = cases_of(store)[0]  # its attributes sealed under a key now lost
        body = {"salt": "", "attributes": {"author": "nobody"}}
        sealed = ring(lost, index).seal("attributes", body, set_id, stray["id"])
        tamper(store, "UPDATE evalset_cases SET attributes = ? WHERE id = ?", (sealed, stray["id"]))
        store.evalset_ready(ring(old, index, new))
    deployed(monkeypatch, ring(new, index, old))
    code, out, err = run(capsys, "evalset", "rotate", "--to", key_id(new), "--db", db_target)
    rotated = json.loads(out)
    assert code == 1 and "no key here opens were left as they are" in err
    assert rotated["unopenable"] == [
        {
            "set": set_id,
            "case": added["cases"][0]["case"],
            "kind": "attributes",
            "key": key_id(lost),
        }
    ]
    assert rotated["counts"] == {"evalset_sets": 1, "evalset_cases": 2, "evalset_labels": 0}
    assert stray["id"] not in out + err  # a case is named by its hash, never its id
    code, out, _ = run(capsys, "evalset", "rotate", "--check", "--db", db_target)
    checked = json.loads(out)
    assert code == 1 and not checked["ready"]
    assert {"kind": "input", "set": set_id, "case": stray["case_hash"], "key": key_id(old)} in (
        checked["failed"]
    )  # the row's input was left under the old key with it
    with AuditStore(db_target) as store:
        EvalsetStore(store, ring(new, index, old)).remove_case(set_id, stray["case_hash"])
    code, out, _ = run(capsys, "evalset", "rotate", "--check", "--db", db_target)
    assert code == 0 and json.loads(out)["ready"]


def test_what_a_rotation_refuses(store: AuditStore, inbox_pack: Pack) -> None:
    index, old, new = fresh(), fresh(), fresh()
    before = EvalsetStore(store, ring(old, index))
    before.add_cases(a_set(before, inbox_pack), planted(2))
    store.evalset_ready(ring(old, index, new))
    hidden = EvalsetStore(store, ring(new, index, old))
    with pytest.raises(MendariError, match=f"is key {key_id(new)}, not {key_id(old)}"):
        hidden.rotate(to=key_id(old))
    with pytest.raises(MendariError, match="1 row or more"):
        hidden.rotate(to=key_id(new), batch=0)
    with pytest.raises(MendariError, match="hidden evaluation sets are off"):
        EvalsetStore(store).rotate(to=key_id(new))
    stray = ring(fresh(), index)
    with pytest.raises(MendariError, match="none of this ring's evaluation-set keys"):
        EvalsetStore(store, stray).rotate(to=key_id(stray.key))
    hidden.rotate(to=key_id(new))
    back = EvalsetStore(store, ring(old, index, new))  # back to the key just retired
    with pytest.raises(MendariError, match=f"key {key_id(old)} was retired by an earlier rotation"):
        back.rotate(to=key_id(old))
    checked = back.check()
    assert checked["current_retired"] is True and not checked["ready"]


def test_a_retired_key_seals_nothing(store: AuditStore, inbox_pack: Pack) -> None:
    """A deployment still on the previous key, once a rotation moved values off it, refuses
    every write instead of sealing what the rotation would leave behind."""
    index, old, new = fresh(), fresh(), fresh()
    stale = EvalsetStore(store, ring(old, index))
    set_id = a_set(stale, inbox_pack)
    stale.add_cases(set_id, planted(1))
    store.evalset_ready(ring(old, index, new))
    EvalsetStore(store, ring(new, index, old)).rotate(to=key_id(new))
    before = everything(store)
    retired = f"evaluation-set key {key_id(old)} was retired by a key rotation"
    lacks = f"sealed under key {key_id(new)}, which this deployment lacks"
    for write, refused in (
        (lambda: stale.create_set(CLINIC, inbox_pack, "x", created_by="ops"), retired),
        (lambda: stale.remove_case(set_id, cases_of(store)[0]["case_hash"]), retired),
        (lambda: stale.reindex(), retired),
        (lambda: store.evalset_ready(ring(old, index, new)), retired),  # a server: no start
        # A case for a set the rotation re-sealed: its header no longer opens with the old key.
        (lambda: stale.add_cases(set_id, [hand_written("A stale deployment's case")]), lacks),
    ):
        with pytest.raises(MendariError, match=refused):
            write()
    assert everything(store) == before
    assert named_keys(store) == {key_id(new)}
    EvalsetStore(store, ring(new, index)).add_cases(set_id, [hand_written("New key: fine")])


# -- the check, verify and the export -------------------------------------------------------------


def test_the_check_verify_and_the_export_write_nothing_and_mark_nothing(
    db_target: str,
    tmp_path: Path,
    inbox_pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Halfway through a rotation (values under both keys), from the store read-only and from
    the command line: nothing is written, marked or chained, whatever ring asks."""
    index, old, new = fresh(), fresh(), fresh()
    served(db_target, inbox_pack)
    with AuditStore(db_target) as store:
        before = EvalsetStore(store, ring(old, index))
        set_id = a_set(before, inbox_pack)
        before.add_cases(set_id, planted(3))
        store.evalset_ready(ring(old, index, new))
        EvalsetStore(store, ring(new, index, old)).add_cases(set_id, planted(1))
        state = everything(store)
    rings = (ring(new, index, old), ring(new, index), ring(old, index), evalset_keys_for_test())
    with AuditStore(db_target, read_only=True) as reading:
        for keys in rings:
            EvalsetStore(reading, keys).check()
            reading.verify(evalsets=keys)
        EvalsetStore(reading).export()
    deployed(monkeypatch, ring(new, index, old))
    assert run(capsys, "evalset", "rotate", "--check", "--db", db_target)[0] == 1  # not ready
    assert run(capsys, "audit", "verify", "--evalsets", "--db", db_target)[0] == 0
    out = tmp_path / "hidden" / "export.jsonl"
    out.parent.mkdir()
    assert run(capsys, "evalset", "export", "--out", str(out), "--db", db_target)[0] == 0
    with AuditStore(db_target) as store:
        assert everything(store) == state
        assert EVALSET_TARGET not in marks(store)  # a check is no rotation


def test_the_check_finds_what_does_not_match(store: AuditStore, inbox_pack: Pack) -> None:
    """With the current key alone: a value sealed for another row, a commitment that does not
    match, a keyed hash that does not, and index rows that are missing or wrong."""
    index, key = fresh(), fresh()
    hidden = EvalsetStore(store, ring(key, index))
    set_id = a_set(hidden, inbox_pack)
    added = hidden.add_cases(set_id, planted(4))
    assert hidden.check()["ready"]
    rows = cases_of(store)
    hashes = [case["case"] for case in added["cases"]]
    keys = ring(key, index)
    tamper(  # another case's input: sealed under the right key, bound to the other case
        store, "UPDATE evalset_cases SET input = ? WHERE id = ?", (rows[1]["input"], rows[0]["id"])
    )
    other = keys.seal("input", {"message": {"body": "Not what was added"}}, set_id, rows[1]["id"])
    tamper(store, "UPDATE evalset_cases SET input = ? WHERE id = ?", (other, rows[1]["id"]))
    tamper(store, "DELETE FROM evalset_index WHERE case_id = ?", (rows[2]["id"],))
    forged = f"hmac-sha256:{key_id(index)}:{'0' * 64}"  # under the current index key
    tamper(store, "UPDATE evalset_cases SET case_hash = ? WHERE id = ?", (forged, rows[3]["id"]))
    checked = hidden.check()
    assert not checked["ready"]
    found = {(f["kind"], f.get("case"), f["key"]) for f in checked["failed"]}
    assert ("input", hashes[0], key_id(key)) in found  # does not open as its own
    assert ("case", hashes[1], "commitment") in found  # opens, but is not what was chained
    assert ("case", hashes[1], "index") in found  # nor what was indexed
    assert ("case", hashes[2], "index") in found  # its index rows are gone
    assert {f["key"] for f in checked["failed"] if f.get("case") not in hashes} == {
        "commitment",
        "case_hash",
    }  # the forged hash: no event chained it, and it is not the case's
    assert all(set(f) <= {"kind", "set", "case", "key"} for f in checked["failed"])
    text = json.dumps(checked)
    for secret in (*MARKS.values(), *case_ids(store)):
        assert secret not in text


def test_the_export_file_holds_no_mark(
    db_target: str,
    tmp_path: Path,
    inbox_pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The export copies ciphertext, ids and key ids as stored: no planted mark, no key; to a
    new file only its owner reads, outside the package and any checkout; with no key set."""
    key, index = evalset_on(monkeypatch)
    served(db_target, inbox_pack)
    db = ("--db", db_target)
    code, out, _ = run(
        capsys, "evalset", "create", INBOX, "--org", CLINIC, "--name", MARKS["name"], *db
    )
    set_id = json.loads(out)["set"]
    run(capsys, "evalset", "add", set_id, str(cases_file(tmp_path, planted(3))), *db)
    with AuditStore(db_target) as store:  # labelled blind, at the store (LP-11h)
        hidden = EvalsetStore(store, EvalsetKeys(key, index))
        hidden.sample(set_id, per_stratum=3)
        people = labellers(store, 2)
        hidden.assign(set_id, people)
        assert label_all(hidden, people) == 6
    for name in ("MENDARI_EVALSET_KEY", "MENDARI_EVALSET_INDEX_KEY"):
        monkeypatch.delenv(name)  # the export needs no key
    path = tmp_path / "backup" / "evalsets.jsonl"
    path.parent.mkdir()
    code, out, err = run(capsys, "evalset", "export", "--out", str(path), *db)
    assert code == 0 and "10 row(s) written" in err
    header = json.loads(out)
    assert header["format"] == EXPORT_FORMAT
    assert header["rows"] == {"evalset_sets": 1, "evalset_cases": 3, "evalset_labels": 6}
    assert header["key_ids"] == {key_id(key): 13}  # a header, 3 inputs, 3 attributes, 6 labels
    raw = path.read_bytes()
    for secret in (*MARKS.values(), b64(key), b64(index)):
        assert secret.encode() not in raw
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    lines = [json.loads(line) for line in raw.decode().splitlines()]
    assert lines[0] == header
    with AuditStore(db_target) as store:
        assert {
            (line["table"], column, line["row"]["id"]): line["row"][column]
            for line in lines[1:]
            for column in SEALED_COLUMNS[line["table"]].columns
            if line["row"][column] is not None
        } == tokens(store)  # every sealed value, as stored
    assert {line["table"] for line in lines[1:]} == set(SEALED_COLUMNS)  # the index left out
    code, _, err = run(capsys, "evalset", "export", "--out", str(path), *db)
    assert code == 1 and "exists: an export never overwrites a file" in err
    assert path.read_bytes() == raw
    checkout = tmp_path / "a-clone"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text('[project]\nname = "mendari"\n')
    inside = checkout / "notes" / "evalsets.jsonl"
    code, _, err = run(capsys, "evalset", "export", "--out", str(inside), *db)
    assert code == 1 and "an export of hidden sets is kept outside Mendari's package" in err
    assert not inside.exists()


# -- the pin: every sealed column ----------------------------------------------------------------


def evalset_tables() -> list[str]:
    return re.findall(r"CREATE TABLE IF NOT EXISTS (evalset_\w+)", audit_module._TABLES)


def test_every_sealed_evalset_column_is_rotated_checked_verified_and_exported(
    store: AuditStore, inbox_pack: Pack
) -> None:
    """Each column of an `evalset_` table is sealed (`SEALED_COLUMNS`) or in the clear
    (`CLEAR_COLUMNS`), never both and never neither. Every sealed column, each filled (a frozen
    set's freeze record and aggregates included, as freezing fills them, LP-11i, and each
    label's body, LP-11h), is re-sealed by a rotation in every state (one set frozen, the other
    active), counted and opened by its check, opened by `verify` (a value moved to another row
    breaks it), and copied by the export as stored."""
    with store._db.session(exclusive=False) as db:
        columns = {table: db.columns(table) for table in evalset_tables()}
    assert set(SEALED_COLUMNS) <= set(columns) == set(CLEAR_COLUMNS)
    for table, present in columns.items():
        held = set(SEALED_COLUMNS[table].columns) if table in SEALED_COLUMNS else set()
        assert not held & CLEAR_COLUMNS[table], table
        assert present == held | CLEAR_COLUMNS[table], table
    index, old, new = fresh(), fresh(), fresh()
    before = ring(old, index)
    hidden = EvalsetStore(store, before)
    sets = [a_set(hidden, inbox_pack), a_set(hidden, inbox_pack, org="clinic-b")]
    for set_id, org in zip(sets, (CLINIC, "clinic-b"), strict=True):
        hidden.add_cases(set_id, planted(2))
        # Labelled blind (LP-11h), each case by two labellers of its organization.
        hidden.sample(set_id, per_stratum=2)
        people = labellers(store, 2, org=org)
        hidden.assign(set_id, people)
        label_all(hidden, people, org=org)
        # Frozen (LP-11i), its freeze record and aggregates sealed; the second set graded once.
        hidden.freeze(set_id, max_runs=3, max_age_days=30, resamples=50)
    grading = store.create_grading_key(org="clinic-b", created_by="ops", reason="the pin")
    hidden.grade_read(sets[1], f"key:{grading['id']}", "sha256:" + "d" * 64, lambda _: None)
    filled = tokens(store)
    assert {(table, column) for table, column, _ in filled} == {
        (table, column) for table, sealed in SEALED_COLUMNS.items() for column in sealed.columns
    }  # every sealed column holds a value here
    store.evalset_ready(ring(old, index, new))
    rotated = EvalsetStore(store, ring(new, index, old)).rotate(to=key_id(new), batch=1)
    assert rotated["from"] == {key_id(old): len(filled)}
    after = tokens(store)
    assert set(after) == set(filled)
    assert all(token.startswith(f"v2:{key_id(new)}:") for token in after.values())  # rotated
    dropped = ring(new, index)
    alone = EvalsetStore(store, dropped)
    checked = alone.check()
    assert checked["ready"], checked  # each opened with the current key alone
    for table in SEALED_COLUMNS:
        counted = sum(1 for (t, _, _) in after if t == table)
        assert checked["by_key"][table] == {key_id(new): counted}
    assert store.verify(evalsets=dropped)["ok"]
    exported = alone.export()
    copied = {
        (line["table"], column, line["row"]["id"]): line["row"][column]
        for line in exported[1:]
        for column in SEALED_COLUMNS[line["table"]].columns
        if line["row"][column] is not None
    }
    assert copied == after  # exported as stored
    assert all(set(line["row"]) == columns[line["table"]] for line in exported[1:])
    # Each sealed column moved to another row: the check and verify both find it.
    for table, sealed in SEALED_COLUMNS.items():
        ids = sorted({row for t, _, row in after if t == table})
        for column, kind in sealed.columns.items():
            mine, theirs = after[(table, column, ids[0])], after[(table, column, ids[1])]
            tamper(store, f"UPDATE {table} SET {column} = ? WHERE id = ?", (theirs, ids[0]))
            failed = {(f["kind"], f["key"]) for f in alone.check()["failed"]}
            assert (kind, key_id(new)) in failed, (table, column)
            verified = store.verify(evalsets=dropped)
            assert not verified["ok"] and "broken_at" in verified, (table, column)
            tamper(store, f"UPDATE {table} SET {column} = ? WHERE id = ?", (mine, ids[0]))
    assert store.verify(evalsets=dropped)["ok"]


# -- the index ------------------------------------------------------------------------------------


def test_the_reindex_is_atomic_and_never_mixes_key_ids(
    store: AuditStore, db_target: str, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    key, first, second = fresh(), fresh(), fresh()
    hidden = EvalsetStore(store, ring(key, first))
    sets = [a_set(hidden, inbox_pack), a_set(hidden, inbox_pack, org="clinic-b")]
    elsewhere = [hand_written("A clinic-b case, a rash"), hand_written("A clinic-b case, a cough")]
    old_cases = [hidden.add_cases(sets[0], planted(2)), hidden.add_cases(sets[1], elsewhere)]
    probe = planted(1)[0]["input"]  # the first set's first case's input, now hidden
    renewed_keys = ring(key, second)  # a new index key, deployed
    renewed = EvalsetStore(store, renewed_keys)
    renewed.add_cases(sets[0], [hand_written("A case under the new index key")])
    assert index_key_ids(store) == {key_id(first), key_id(second)}  # until the reindex
    assert renewed.hidden_matches(probe) == []  # the old cases are under the old index key
    checked = renewed.check()
    assert not checked["ready"]
    assert {f["key"] for f in checked["failed"]} == {"index"}
    assert len(checked["failed"]) == 4  # the four cases indexed under the old key
    before = everything(store)
    seen: list[set[str]] = []
    real_insert = evalsets_module._insert

    def look_then_fail(db: Any, table: str, *args: Any) -> None:
        """After the old rows are deleted, before the new ones go in: another reader still
        sees the index as it was; then the run fails."""
        if table == "evalset_index":
            with AuditStore(db_target, read_only=True) as reader:
                seen.append(index_key_ids(reader))
            raise RuntimeError("the function timed out")
        real_insert(db, table, *args)

    monkeypatch.setattr(evalsets_module, "_insert", look_then_fail)
    with pytest.raises(RuntimeError):
        renewed.reindex()
    assert seen == [{key_id(first), key_id(second)}]  # never empty, never a mix of new rows
    assert everything(store) == before  # nothing stayed: no row, no event
    monkeypatch.setattr(evalsets_module, "_insert", real_insert)
    rebuilt = renewed.reindex()
    assert rebuilt == {
        "key_id": key_id(second),
        "count": 5,
        "replaced": {key_id(first): 8, key_id(second): 2},
    }
    assert index_key_ids(store) == {key_id(second)}
    matched = renewed.hidden_matches(probe)
    assert [m["set"] for m in matched] == [sets[0]]
    assert renewed.check()["ready"] and store.verify(evalsets=renewed_keys)["ok"]
    kept = [c["case"] for added in old_cases for c in added["cases"]]
    assert all(h.startswith(f"hmac-sha256:{key_id(first)}:") for h in kept)
    assert {row["case_hash"] for row in cases_of(store)} >= set(kept)  # chained hashes kept
    (event,) = events(store, "evalset_index.rebuilt")
    assert event["decision_id"] == KEYS_CHAIN
    assert event["payload"] == {"key_id": key_id(second), "count": 5}
    assert len(events(store, "evalset_index.epoch")) == 2  # one per index key, not per rebuild
    # A third index key, reindexed straight away: the rebuild starts its epoch.
    third = fresh()
    EvalsetStore(store, ring(key, third)).reindex()
    kinds = [e["event"] for e in chained(store)][-2:]
    assert kinds == ["evalset_index.epoch", "evalset_index.rebuilt"]
    assert events(store, "evalset_index.epoch")[-1]["payload"] == {"key_id": key_id(third)}


def test_a_reindex_that_cannot_open_a_case_changes_nothing(
    store: AuditStore, inbox_pack: Pack
) -> None:
    index, old, new = fresh(), fresh(), fresh()
    before = EvalsetStore(store, ring(old, index))
    set_id = a_set(before, inbox_pack)
    before.add_cases(set_id, planted(2))
    store.evalset_ready(ring(old, index, new))
    EvalsetStore(store, ring(new, index, old)).add_cases(set_id, planted(1))
    state = everything(store)
    early = EvalsetStore(store, ring(new, index))  # the old key dropped too early
    with pytest.raises(MendariError, match=r"^2 case input\(s\) no key here opens") as refused:
        early.reindex()
    assert key_id(old) in str(refused.value) and "nothing was reindexed" in str(refused.value)
    assert everything(store) == state


def stale_writes(
    store: AuditStore, stale: EvalsetStore, set_id: str, pack: Pack
) -> list[Callable[[], object]]:
    """Every write a deployment, a server or a shell holding `stale`'s keys could try."""
    held = stale._ring()
    return [
        lambda: stale.add_cases(set_id, [hand_written("A stale deployment's case")]),
        lambda: stale.create_set(CLINIC, pack, "x", created_by="ops"),
        lambda: stale.remove_case(set_id, cases_of(store)[0]["case_hash"]),
        lambda: stale.reindex(),
        lambda: stale.rotate(to=key_id(held.key)),
        lambda: store.evalset_ready(held),  # a server: no start
    ]


def test_an_index_key_a_reindex_replaced_indexes_nothing_more(
    store: AuditStore, inbox_pack: Pack
) -> None:
    """A new index key, twice in a row by the documented steps, with the writes a live system
    makes before the reindex (an old deployment's and the new one's; lesson of 2026-10-06): once
    the index is rebuilt, whoever still holds the previous index key refuses every write, its
    own reindex included, instead of indexing a case under a key `hidden_matches` no longer
    reads. An index key with an epoch but no rows left is retired as well."""
    key = fresh()
    indexes = [fresh(), fresh(), fresh()]
    begun = EvalsetStore(store, ring(key, indexes[0]))
    set_id = a_set(begun, inbox_pack)
    begun.add_cases(set_id, planted(2))
    for previous, current in itertools.pairwise(indexes):
        stale = EvalsetStore(store, ring(key, previous))
        renewed = EvalsetStore(store, ring(key, current))
        earlier = hand_written(f"An old deployment's case, before the reindex to {key_id(current)}")
        stale.add_cases(set_id, [earlier])  # not yet replaced: it still writes
        renewed.add_cases(set_id, [hand_written(f"The new deployment's, {key_id(current)}")])
        renewed.reindex()
        assert renewed.check()["ready"]
        assert len(renewed.hidden_matches(earlier["input"])) == 1  # indexed under the new key
        state = everything(store)
        replaced = (
            f"evaluation-set index key {key_id(previous)} was replaced by a reindex: deploy the "
            "current MENDARI_EVALSET_INDEX_KEY"
        )
        for write in stale_writes(store, stale, set_id, inbox_pack):
            with pytest.raises(MendariError, match=re.escape(replaced)):
                write()
        assert everything(store) == state
        assert index_key_ids(store) == {key_id(current)}
    # An index key whose one case was removed: its epoch is marked, no row names it.
    emptied = EvalsetStore(store, ring(key, fresh()))
    added = emptied.add_cases(set_id, [hand_written("Indexed, then removed")])
    emptied.remove_case(set_id, added["cases"][0]["case"])
    gone = key_id(emptied._ring().index_key)
    rebuilt = EvalsetStore(store, ring(key, fresh())).reindex()
    assert gone not in rebuilt["replaced"]
    with pytest.raises(MendariError, match=f"index key {gone} was replaced by a reindex"):
        emptied.add_cases(set_id, [hand_written("Too late")])


def test_a_reindex_never_goes_back_to_an_older_index_key(
    store: AuditStore, inbox_pack: Pack
) -> None:
    """Before any reindex, the new index key has indexed a case: a shell still holding the old
    one refuses to reindex, rather than retiring the newer key (the deployments holding it would
    then refuse to start). Nothing is written; the newest key's reindex then goes ahead."""
    key, old, new = fresh(), fresh(), fresh()
    stale = EvalsetStore(store, ring(key, old))
    set_id = a_set(stale, inbox_pack)
    stale.add_cases(set_id, planted(1))
    renewed = EvalsetStore(store, ring(key, new))
    renewed.add_cases(set_id, [hand_written("The new index key's first case")])
    state = everything(store)
    older = (
        f"index key {key_id(old)} is older than index key {key_id(new)}, which already indexes "
        "cases here: reindex with the newest MENDARI_EVALSET_INDEX_KEY"
    )
    with pytest.raises(MendariError, match=re.escape(older)):
        stale.reindex()
    assert everything(store) == state
    assert not {m for m in marks(store) if m.startswith("evalset_index_retired:")}
    assert renewed.reindex()["replaced"] == {key_id(old): 2, key_id(new): 2}
    assert renewed.check()["ready"] and index_key_ids(store) == {key_id(new)}


# -- PHI rotation and the evaluation-set keys -----------------------------------------------------


def test_phi_rotation_never_touches_evalset_rows_and_the_reverse(
    db_target: str, inbox_pack: Pack
) -> None:
    phi_old, phi_new, hash_key = fresh(), fresh(), fresh()
    index, old, new = fresh(), fresh(), fresh()

    def phi_of(store: AuditStore) -> str:
        with store._db.session(exclusive=False) as db:
            return json.dumps(
                {
                    table: db.all(f"SELECT * FROM {table} ORDER BY 1")
                    for table in ("decisions", "decision_inputs", "notes")
                },
                default=str,
            )

    def evalsets_of(store: AuditStore) -> str:
        with store._db.session(exclusive=False) as db:
            return json.dumps(
                {
                    table: db.all(f"SELECT * FROM {table} ORDER BY 1")
                    for table in ("evalset_sets", "evalset_cases", "evalset_index")
                },
                default=str,
            )

    with AuditStore(db_target, phi=PhiPolicy("encrypt", phi_old, hash_key)) as store:
        urgent = message("Crushing chest pain and my left arm is numb")  # waits for a reviewer
        decided = Decision(inbox_pack, store=store)(urgent).id
        store.resolve_review(decided, final_label="nurse", reviewer="rn-1", note="a note")
        hidden = EvalsetStore(store, ring(old, index))
        hidden.add_cases(a_set(hidden, inbox_pack), planted(2))
        store.evalset_ready(ring(old, index, new))
        phi_rows, phi_marks = phi_of(store), {m for m in marks(store) if m.startswith("phi_")}
        EvalsetStore(store, ring(new, index, old)).rotate(to=key_id(new))
        assert phi_of(store) == phi_rows  # the evaluation-set rotation: no PHI row
        assert {m for m in marks(store) if m.startswith("phi_")} == phi_marks
        assert store.phi_check()["ready"] and events(store, "phi.rotated") == []
    rotating = PhiPolicy("encrypt", phi_new, hash_key, (phi_old,))
    with AuditStore(db_target, phi=rotating) as store:
        sets_rows = evalsets_of(store)
        evalset_marks = {m for m in marks(store) if m.startswith("evalset_")}
        assert store.rotate_phi(to=key_id(phi_new))["inputs"] == 1
        assert evalsets_of(store) == sets_rows  # the PHI rotation: no evaluation-set row
        assert {m for m in marks(store) if m.startswith("evalset_")} == evalset_marks
        assert RETIRED + key_id(phi_old) in marks(store)
        assert EVALSET_RETIRED + key_id(phi_old) not in marks(store)
        assert len(events(store, "evalset.rotated")) == 2  # the earlier ones only
        assert EvalsetStore(store, ring(new, index)).check()["ready"]
        assert store.verify(notes=True, evalsets=ring(new, index))["ok"]


# -- the command line -----------------------------------------------------------------------------


def test_the_key_holder_rotates_checks_reindexes_and_exports_from_the_command_line(
    db_target: str,
    tmp_path: Path,
    inbox_pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """LP-11f's acceptance, by the runbook: the next key read first, swapped in, the sets
    re-sealed and checked, the read key dropped; then a new index key and the reindex; output
    ids, case hashes, key ids and counts only."""
    key, index = evalset_on(monkeypatch)
    served(db_target, inbox_pack)
    db = ("--db", db_target)
    code, out, _ = run(capsys, "evalset", "create", INBOX, "--org", CLINIC, "--name", "x", *db)
    set_id = json.loads(out)["set"]
    run(capsys, "evalset", "add", set_id, str(cases_file(tmp_path, planted(3))), *db)
    nxt = fresh()
    with AuditStore(db_target) as store:  # reader first: a deployment starts with it
        store.evalset_ready(ring(key, index, nxt))
        hidden_ids = case_ids(store)
    deployed(monkeypatch, ring(nxt, index, key))  # the swap

    def ran(*argv: str) -> tuple[int, Any, str]:
        code, out, err = run(capsys, *argv, *db)
        for case_id in hidden_ids:
            assert case_id not in out + err, argv
        return code, json.loads(out) if out.strip() else None, err

    code, checked, _ = ran("evalset", "rotate", "--check")
    assert code == 1 and checked["by_key"]["evalset_cases"] == {key_id(key): 6}
    assert checked["read_not_retired"] == [key_id(key)]  # nothing moved off it yet
    assert run(capsys, "evalset", "rotate", *db)[2].startswith("error: give --to <key id>")
    code, _, err = ran("evalset", "rotate", "--to", key_id(key))
    assert code == 1 and f"is key {key_id(nxt)}, not {key_id(key)}" in err
    code, rotated, err = ran("evalset", "rotate", "--to", key_id(nxt), "--batch", "2")
    assert code == 0 and "run `mendari evalset rotate --check`" in err
    assert rotated["counts"] == {"evalset_sets": 1, "evalset_cases": 3, "evalset_labels": 0}
    code, checked, err = ran("evalset", "rotate", "--check")
    assert code == 0 and checked["ready"] and checked["read_not_retired"] == []
    assert "ready: everything opens with the current key alone" in err
    deployed(monkeypatch, ring(nxt, index))  # the read key dropped
    assert ran("evalset", "rotate", "--check")[0] == 0
    assert ran("audit", "verify", "--evalsets")[1]["ok"]
    new_index = fresh()
    deployed(monkeypatch, ring(nxt, new_index))  # a new index key: reindex at once
    code, checked, _ = ran("evalset", "rotate", "--check")
    assert code == 1 and {f["key"] for f in checked["failed"]} == {"index"}
    code, rebuilt, err = ran("evalset", "reindex")
    assert code == 0 and rebuilt["key_id"] == key_id(new_index) and rebuilt["count"] == 3
    assert f"3 case(s) indexed under index key {key_id(new_index)}" in err
    assert ran("evalset", "rotate", "--check")[0] == 0
    assert ran("audit", "verify", "--evalsets")[1]["ok"]
    out_file = tmp_path / "backup.jsonl"
    code, header, _ = ran("evalset", "export", "--out", str(out_file))
    assert code == 0 and header["key_ids"] == {key_id(nxt): 7}


def test_the_commands_refuse_without_the_keys_and_the_export_needs_none(
    tmp_path: Path, inbox_pack: Pack, capsys: pytest.CaptureFixture[str]
) -> None:
    db = str(tmp_path / "mendari.db")
    served(db, inbox_pack)
    before = Path(db).read_bytes()
    for argv in (
        ("evalset", "rotate", "--to", "0" * 16),
        ("evalset", "rotate", "--check"),
        ("evalset", "reindex"),
    ):
        code, _, err = run(capsys, *argv, "--db", db)
        assert code == 1 and err.startswith("error: hidden evaluation sets are off"), err
    out = tmp_path / "export.jsonl"
    code, printed, _ = run(capsys, "evalset", "export", "--out", str(out), "--db", db)
    assert code == 0 and json.loads(printed)["rows"] == {
        "evalset_sets": 0,
        "evalset_cases": 0,
        "evalset_labels": 0,
    }
    assert Path(db).read_bytes() == before
    assert os.path.getsize(out) > 0


def test_each_rotation_event_renders_as_key_ids_and_counts() -> None:
    created = "2026-10-01T00:00:00.000+00:00"
    rotated = {"to": "k2", "from": {"k1": 3}, "counts": {"evalset_sets": 1, "evalset_cases": 1}}
    rebuilt = {"key_id": "i2", "count": 5}
    for kind, payload, expected in (
        (
            "evalset.rotated",
            rotated,
            {"to": "k2", "from": "k1=3", "counts": "evalset_sets=1,evalset_cases=1"},
        ),
        ("evalset_index.rebuilt", rebuilt, {"key_id": "i2", "count": "5"}),
    ):
        item = {"subject": KEYS_CHAIN, "event": kind, "payload": payload}
        event = audit_fhir.administrative_event({**item, "created_at": created, "hash": "a" * 64})
        details = {d["type"]: d["valueString"] for d in event["entity"][0]["detail"]}
        assert {k: details[k] for k in expected} == expected, kind
        assert event["action"] == "U" and fhir_structure.check(event) == [], kind
