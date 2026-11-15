# SPDX-License-Identifier: Apache-2.0
"""The registry's signed checkpoint (`hub/checkpoint.py`): a statement of the chain's head for a
day, signed with the registry's key and kept outside its database. The chain is unkeyed, so one
rewritten whole verifies alone; against a statement taken before the rewrite it does not, and
`mendari registry countersign --checkpoint` then signs nothing. On SQLite and Postgres. Every
key here is made at runtime; none is kept."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from mendari.cli import main
from mendari.core import checkpoint as engine_checkpoint
from mendari.core.audit import GENESIS
from mendari.errors import HubError
from mendari.hub import acceptance, yank
from mendari.hub import checkpoint as registry_checkpoint
from mendari.hub.registry import RegistryStore, stated_head
from tests.conftest import Registry
from tests.test_registry_acceptance import (
    OTHER_SEED,
    REFERRAL,
    SUBJECT,
    _events,
    _key,
    _keyed,
    _plant,
    _record,
    _restart_chain,
    _verify,
)


def _run(capsys: pytest.CaptureFixture[str], *command: str) -> tuple[int, str, str]:
    capsys.readouterr()
    code = main(list(command))
    printed = capsys.readouterr()
    return code, printed.out, printed.err


def _taken(capsys: pytest.CaptureFixture[str], db_target: str, path: Path) -> dict[str, str]:
    """`mendari registry checkpoint`, its statement kept in a file outside the database."""
    code, out, err = _run(capsys, "registry", "checkpoint", "--db", db_target)
    assert code == 0, err
    path.write_text(out)
    statement: dict[str, str] = json.loads(out)
    return statement


def test_a_checkpoint_states_the_head_and_the_chain_holds_it(
    registry: Registry,
    db_target: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _keyed(monkeypatch)
    assert registry.publish().status_code == 201
    before = _events(db_target)
    kept = tmp_path / "checkpoint.json"
    statement = _taken(capsys, db_target, kept)
    assert statement == {
        "format": "mendari.registry-checkpoint/1",
        "date": datetime.now(UTC).date().isoformat(),
        "head": before[-1]["hash"],
        "key_id": yank.registry_public(_key())["key_id"],
        "signature": statement["signature"],
    }
    assert registry_checkpoint.problem(statement, _key().public_key()) is None
    assert _events(db_target) == before  # nothing stored, nothing chained
    assert _taken(capsys, db_target, tmp_path / "again.json") == statement  # the same day's
    command = ("registry", "verify", "--db", db_target, "--checkpoint", str(kept))
    code, out, _ = _run(capsys, *command)
    assert code == 0 and f"it holds the checkpoint of {statement['date']}; 0 events since" in out
    assert "after_checkpoint" not in _verify(db_target)  # without one, as it always answered
    # The chain grows: the statement still holds, and says nothing about what came after it.
    registry.operator.create_namespace("site-a", "community", "Site A")
    code, out, _ = _run(capsys, *command)
    assert code == 0 and "1 events since, which the checkpoint says nothing about" in out
    with RegistryStore(db_target, read_only=True) as store:
        assert store.verify(head=statement["head"])["after_checkpoint"] == 1
        later = store.checkpoint(previous=statement)  # the routine: each day against the last
        assert later["head"] != statement["head"]
    # Someone without the private key checks with the public one, as the registry publishes it.
    _keyed(monkeypatch, None)
    code, _, err = _run(capsys, *command)
    assert code != 0 and yank.KEY_ENV in err and "or give its public key with --public-key" in err
    public = yank.registry_public(_key())["public_key"]
    assert _run(capsys, *command, "--public-key", public)[0] == 0
    code, _, err = _run(capsys, "registry", "verify", "--db", db_target, "--public-key", public)
    assert code != 0 and "--public-key goes with --checkpoint" in err
    other = yank.registry_public(_key(OTHER_SEED))["public_key"]
    code, _, err = _run(capsys, *command, "--public-key", other)
    assert code != 0 and "it was signed with another key" in err


def test_a_rewritten_chain_verifies_alone_and_fails_against_an_earlier_checkpoint(
    registry: Registry,
    db_target: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T11's residual: a writer of the database plants a version and rewrites the unkeyed chain
    so it verifies. A statement of the head from before, kept elsewhere, is what shows it."""
    _keyed(monkeypatch, None)
    assert registry.publish().status_code == 201  # published while the registry had no key
    _keyed(monkeypatch)
    kept = tmp_path / "checkpoint.json"
    _taken(capsys, db_target, kept)
    _plant(db_target, "mendari")
    _restart_chain(db_target)
    rewritten = _events(db_target)
    code, out, _ = _run(capsys, "registry", "verify", "--db", db_target)
    assert code == 0 and "registry chain verified" in out  # alone, the rewrite passes
    assert "checked without a checkpoint: a chain rewritten whole verifies too" in out
    code, out, err = _run(
        capsys, "registry", "verify", "--db", db_target, "--checkpoint", str(kept)
    )
    assert code == 1 and out == ""
    assert "registry chain broken: the chain does not hold the checkpoint's head" in err
    assert "rewritten or cut short from before the checkpoint" in err
    # The backfill signs nothing on it, and no new statement vouches for it.
    code, out, err = _run(
        capsys, "registry", "countersign", "--db", db_target, "--checkpoint", str(kept)
    )
    assert code != 0 and out == ""
    assert "nothing was counter-signed" in err and "the checkpoint's head" in err
    code, out, err = _run(
        capsys, "registry", "checkpoint", "--db", db_target, "--previous", str(kept)
    )
    assert code != 0 and out == "" and "no checkpoint was signed" in err
    assert _events(db_target) == rewritten and _record(registry) is None
    # Without the statement the backfill runs as before (its archive check still skips the
    # planted row), and says plainly that it ran without one.
    code, out, err = _run(capsys, "registry", "countersign", "--db", db_target)
    assert json.loads(out) == {
        "signed": [SUBJECT],
        "skipped": [f"{REFERRAL.id}@9.9.9: its archive is another pack or version"],
        "events_since_checkpoint": None,
        "checkpoint": None,
    }
    assert code != 0 and "ran without a checkpoint" in err
    assert "a chain rewritten whole verifies too" in err


def test_a_chain_cut_short_of_the_checkpoint_fails_against_it(
    registry: Registry,
    db_target: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _keyed(monkeypatch)
    early = _taken(capsys, db_target, tmp_path / "early.json")
    assert registry.publish().status_code == 201
    late = _taken(capsys, db_target, tmp_path / "late.json")
    assert early["head"] != late["head"]
    with RegistryStore(db_target) as store:
        assert store.verify(head=early["head"])["ok"]
        assert store.verify(head=late["head"])["after_checkpoint"] == 0
        # A head that is no event of this chain: another registry's, or one since removed.
        elsewhere = registry_checkpoint.sign("ab" * 32, date(2026, 11, 9), _key())
        refused = store.verify(head=stated_head(elsewhere))
        assert not refused["ok"] and "does not hold the checkpoint's head" in refused["problem"]
        assert refused["head"] == late["head"]  # the head it does have, to compare
        with pytest.raises(HubError, match="no checkpoint was signed") as error:
            store.checkpoint(previous=elsewhere)
        assert error.value.status == 409


def test_a_statement_by_another_key_or_of_another_format_is_refused(
    registry: Registry,
    db_target: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The registry's key signs recalls, acceptances and checkpoints; each names its own
    `format` inside what is signed, so none is ever read as another. A statement signed by
    another key, changed after it was signed, or carrying a field nobody signed is refused
    too, before the database is opened."""
    _keyed(monkeypatch, None)
    assert registry.publish().status_code == 201
    _keyed(monkeypatch)
    own, today = _key(), datetime.now(UTC).date()
    head = _events(db_target)[-1]["hash"]
    good = registry_checkpoint.sign(head, today, own)
    record = acceptance.sign(
        pack=REFERRAL.id,
        version=REFERRAL.version,
        digest=REFERRAL.digest,
        archive_sha256=head,
        publisher_key_id="ed25519:" + "0" * 16,
        key=own,
    )
    recall = yank.sign(
        pack=REFERRAL.id,
        version=REFERRAL.version,
        digest=REFERRAL.digest,
        archive_sha256=head,
        reason="a recall",
        severity="high",
        signer="registry",
        key=own,
        key_id=yank.registry_public(own)["key_id"],
    )
    engine = engine_checkpoint.sign(head, today, own)  # the same key, head and day
    relabelled = {**engine, "format": registry_checkpoint.FORMAT}
    refused: dict[str, tuple[Any, str]] = {
        "another key": (
            registry_checkpoint.sign(head, today, _key(OTHER_SEED)),
            "it was signed with another key",
        ),
        "an engine checkpoint": (engine, "not a mendari.registry-checkpoint/1 statement"),
        "an engine checkpoint relabelled": (relabelled, "its signature does not hold"),
        "an acceptance": (record, "not a mendari.registry-checkpoint/1 statement"),
        "a recall": (recall, "not a mendari.registry-checkpoint/1 statement"),
        "another head": ({**good, "head": "0" * 64}, "its signature does not hold"),
        "another day": ({**good, "date": "2026-01-01"}, "its signature does not hold"),
        "not a head": ({**good, "head": "head"}, "its head is not a chain hash"),
        "not a day": ({**good, "date": "today"}, "its date is not a day"),
        "an unsigned field": ({**good, "events": 3}, "its fields are format, date, head, key_id"),
        "no signature": (
            {name: good[name] for name in registry_checkpoint.FIELDS},
            "its fields are format, date, head, key_id",
        ),
        "not text": ({**good, "head": 7}, "a statement's fields are text"),
        "a list": ([good], "expected a JSON object"),
    }
    before = _events(db_target)
    for name, (statement, why) in refused.items():
        if isinstance(statement, dict):
            assert why in str(registry_checkpoint.problem(statement, own.public_key())), name
        path = tmp_path / "statement.json"
        path.write_text(json.dumps(statement))
        for command in (
            ("registry", "verify", "--db", db_target, "--checkpoint", str(path)),
            ("registry", "countersign", "--db", db_target, "--checkpoint", str(path)),
            ("registry", "checkpoint", "--db", db_target, "--previous", str(path)),
        ):
            code, out, err = _run(capsys, *command)
            assert code != 0 and out == "" and why in err, (name, command[1], err)
            assert command[-2] in err  # the flag that named the file
    assert _events(db_target) == before and _record(registry) is None
    # And the other way: a registry checkpoint is no engine checkpoint and no acceptance.
    assert (
        engine_checkpoint.problem(good, own.public_key()) == "not a mendari.checkpoint/1 statement"
    )
    key_id = yank.registry_public(own)["key_id"]
    assert acceptance.problem(good, own.public_key(), key_id) is not None
    with pytest.raises(ValueError, match="names a chain hash"):
        registry_checkpoint.sign("head", today, own)


def test_countersign_takes_a_checkpoint_and_says_which(
    registry: Registry,
    db_target: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _keyed(monkeypatch, None)
    assert registry.publish().status_code == 201
    _keyed(monkeypatch)
    kept = tmp_path / "checkpoint.json"
    statement = _taken(capsys, db_target, kept)
    code, out, err = _run(
        capsys, "registry", "countersign", "--db", db_target, "--checkpoint", str(kept)
    )
    assert code == 0, err
    assert json.loads(out) == {
        "signed": [SUBJECT],
        "skipped": [],
        "events_since_checkpoint": 0,
        "checkpoint": statement["date"],
    }
    assert "without a checkpoint" not in err and "behind the chain" not in err
    assert _record(registry) is not None
    # The same statement later is behind the chain: the run says by how much, so a backfill
    # against an old checkpoint never reads as covered by it.
    code, out, err = _run(
        capsys, "registry", "countersign", "--db", db_target, "--checkpoint", str(kept)
    )
    assert code == 0, err
    assert json.loads(out) == {
        "signed": [],
        "skipped": [],
        "events_since_checkpoint": 1,
        "checkpoint": statement["date"],
    }
    assert f"the checkpoint of {statement['date']} is 1 events behind the chain" in err
    assert "it says nothing about those events" in err
    with RegistryStore(db_target, read_only=True) as store:
        assert store.verify(head=statement["head"])["after_checkpoint"] == 1  # `pack.accepted`


def test_a_checkpoint_needs_the_key_a_chain_that_verifies_and_a_database_that_is_there(
    registry: Registry,
    db_target: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    command = ("registry", "checkpoint", "--db", db_target)
    _keyed(monkeypatch, None)
    assert registry.publish().status_code == 201
    before, verified = _events(db_target), _verify(db_target)
    code, out, err = _run(capsys, *command)
    assert code != 0 and out == "" and "has no signing key" in err and yank.KEY_ENV in err
    # Each command's error is its own: no flag it does not have, no step it is not taking.
    earlier = tmp_path / "earlier.json"
    head = _events(db_target)[-1]["hash"]
    earlier.write_text(json.dumps(registry_checkpoint.sign(head, date(2026, 11, 9), _key())))
    with_previous = (*command, "--previous", str(earlier))
    with_checkpoint = ("registry", "verify", "--db", db_target, "--checkpoint", str(earlier))
    code, out, err = _run(capsys, *with_previous)
    assert code != 0 and out == "" and yank.KEY_ENV in err and "public key" not in err
    secret = "not-a-key-" + base64.b64encode(b"never shown").decode()
    monkeypatch.setenv(yank.KEY_ENV, secret)
    for broken in (command, with_previous, with_checkpoint):
        code, out, err = _run(capsys, *broken)
        assert code != 0 and out == "" and yank.KEY_ENV in err and secret not in err, broken
        assert "counter-sign" not in err, broken
    assert "the registry cannot check a checkpoint" in err
    with RegistryStore(db_target, read_only=True) as store:
        with pytest.raises(HubError, match="the registry cannot sign a checkpoint") as unsigned:
            store.checkpoint()
        assert unsigned.value.status == 503 and secret not in str(unsigned.value)
    _keyed(monkeypatch)
    # A database that is not there is never made, by any of the three.
    missing = tmp_path / "missing.db"
    kept = tmp_path / "checkpoint.json"
    _taken(capsys, db_target, kept)
    for broken in (
        ("registry", "checkpoint", "--db", str(missing)),
        ("registry", "verify", "--db", str(missing), "--checkpoint", str(kept)),
        ("registry", "countersign", "--db", str(missing), "--checkpoint", str(kept)),
        ("registry", "checkpoint", "--db", db_target, "--previous", str(tmp_path / "none.json")),
    ):
        code, out, err = _run(capsys, *broken)
        assert code != 0 and out == "" and err.startswith("error:"), broken
        assert not missing.exists()
    # A chain that does not verify gets no statement: it would vouch for the planted row.
    _plant(db_target, "mendari")
    code, out, err = _run(capsys, *command)
    assert code != 0 and out == "" and "no checkpoint was signed" in err
    assert "1 of the versions have no chained event" in err
    assert _events(db_target) == before
    assert {**_verify(db_target), "ok": True, "problem": None} == verified  # read as before


def test_taking_and_checking_a_checkpoint_leaves_the_database_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing is added to the registry's database for checkpoints (no table, no column, no
    row), so one made before them reads exactly as it did. On SQLite, by its bytes."""
    _keyed(monkeypatch)
    db = tmp_path / "registry.db"
    with RegistryStore(db) as store:
        assert store.head() == {"events": 0, "head": GENESIS}
        with pytest.raises(HubError, match="no checkpoint was signed"):
            store.checkpoint()  # a chain with no event has no head to state
        store.create_namespace("site-a", "community", "Site A")
    held = hashlib.sha256(db.read_bytes()).hexdigest()
    kept = tmp_path / "checkpoint.json"
    _taken(capsys, str(db), kept)
    assert main(["registry", "verify", "--db", str(db), "--checkpoint", str(kept)]) == 0
    assert hashlib.sha256(db.read_bytes()).hexdigest() == held
