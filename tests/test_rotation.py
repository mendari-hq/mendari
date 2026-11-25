# SPDX-License-Identifier: Apache-2.0
"""Key rotation (LP-09): the PHI key, the input hash key and API keys each change without losing
data or breaking the audit chain.

- The PHI key: a ring (`MENDARI_PHI_KEY` seals, `MENDARI_PHI_READ_KEYS` only opens); from the
  first rotation on each token names its key (`v2:<key id>:`); `mendari phi rotate` re-seals in
  batches, each chained, and an interrupted run resumes; `--check` proves everything opens with
  the current key alone before the read keys are dropped; a key a rotation moved values off
  seals nothing more.
- The input hash key: a new key starts an epoch (chained); old hashes stay as they were.
- API keys: a rotation makes a new key and lets the old one work for an overlap, then expire.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, Pack
from mendari.cli import main
from mendari.core import audit as audit_module
from mendari.core.phi import LEGACY, PhiPolicy, key_id, phi_policy, token_key
from mendari.errors import AuthError, MendariError
from mendari.server.app import create_app, mounted_routes
from mendari.server.auth import permission_for
from tests.conftest import (
    HASH_KEY_B64,
    INPUT_HASH_KEY,
    encrypt_mode,
    evalset_on,
    evalsets_are_on,
    message,
    security_lines,
    signed_in,
)

OLD, NEW, OTHER = os.urandom(32), os.urandom(32), os.urandom(32)
SECRET = "my neighbour Rosalind Q. Example has chest pain"
URGENT = "Crushing chest pain since an hour, " + SECRET


def b64(key: bytes) -> str:
    return base64.b64encode(key).decode()


def ring(current: bytes, *read: bytes) -> PhiPolicy:
    return PhiPolicy(mode="encrypt", key=current, hash_key=INPUT_HASH_KEY, read_keys=read)


def keep_inputs(store: AuditStore, n: int, *, start: int = 0) -> list[str]:
    """`n` kept inputs (every other with sealed details), sealed as `store` seals."""
    ids = [f"d-{i:05d}" for i in range(start, start + n)]
    with store._db.session(exclusive=True) as db:
        for i, decision_id in enumerate(ids):
            details = (
                store.phi.seal_details(decision_id, {"evidence": {"q": [f"value {i}"]}})
                if i % 2
                else None
            )
            db.all(
                "INSERT INTO decision_inputs (decision_id, created_at, ciphertext, details) "
                "VALUES (?, ?, ?, ?)",
                (
                    decision_id,
                    "2026-10-01T00:00:00+00:00",
                    store.phi.encrypt(decision_id, {"n": i}),
                    details,
                ),
            )
    return ids


def reviewed(store: AuditStore, pack: Pack, n: int) -> list[str]:
    """`n` real decisions, each reviewed with a note (chained, with its commitment)."""
    decide = Decision(pack, store=store)
    ids = []
    for i in range(n):
        decided = decide(message(f"{URGENT} ({i})"))
        store.resolve_review(decided.id, final_label="nurse", reviewer="rn-1", note=f"{SECRET} {i}")
        ids.append(decided.id)
    return ids


def chained(store: AuditStore, event: str) -> list[dict[str, Any]]:
    with store._db.session(exclusive=False) as db:
        rows = db.all("SELECT payload FROM audit_events WHERE event = ? ORDER BY seq", (event,))
    return [json.loads(row["payload"]) for row in rows]


# -- the token and the ring --------------------------------------------------------------------


def test_tokens_name_their_key_only_from_the_first_rotation(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "a.db", phi=ring(OLD))
    before = store.phi.encrypt("d-1", {"x": 1})
    assert token_key(before) == LEGACY and ":" not in before  # what a release before reads
    rotating = AuditStore(tmp_path / "a.db", phi=ring(NEW, OLD))  # a rotation is under way
    tagged = rotating.phi.encrypt("d-2", {"x": 2})
    assert tagged.startswith(f"v2:{key_id(NEW)}:")
    assert AuditStore(tmp_path / "a.db", phi=ring(NEW)).phi.tag_tokens  # it stays so


def test_a_token_opens_only_with_its_key_as_its_kind_for_its_decision() -> None:
    old, both = ring(OLD), ring(NEW, OLD)
    legacy = old.encrypt("d-1", {"x": 1})
    assert both.decrypt("d-1", legacy) == {"x": 1}  # sealed before key ids: the ring is tried
    tagged = PhiPolicy("encrypt", NEW, INPUT_HASH_KEY, (), tag_tokens=True).encrypt("d-1", {"x": 1})
    assert both.open("input", tagged, "d-1") == ({"x": 1}, key_id(NEW))
    for wrong in (
        lambda: both.decrypt("d-2", tagged),  # another decision
        lambda: both.open_details("d-1", tagged),  # another kind
        lambda: both.open_note("n-1", "d-1", tagged),
        lambda: ring(OTHER).decrypt("d-1", tagged),  # a key it does not name
    ):
        with pytest.raises(MendariError) as refused:
            wrong()
        assert tagged not in str(refused.value)
    with pytest.raises(MendariError, match=f"key {key_id(NEW)}, which this deployment lacks"):
        ring(OLD).decrypt("d-1", tagged)
    for garbage in ("", "v2:zz:abc", f"v2:{key_id(NEW)}:not base64!", "c2hvcnQ=", "{}"):
        with pytest.raises(MendariError, match="not a sealed token"):
            both.decrypt("d-1", garbage)


def test_read_keys_are_checked_and_never_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENDARI_PHI_MODE", "encrypt")
    monkeypatch.setenv("MENDARI_PHI_KEY", b64(NEW))
    monkeypatch.setenv("MENDARI_INPUT_HASH_KEY", HASH_KEY_B64)
    monkeypatch.setenv("MENDARI_PHI_READ_KEYS", f"{b64(OLD)}, {b64(OTHER)}")
    assert phi_policy().read_keys == (OLD, OTHER)
    for listed, says in (
        (f"{b64(OLD)},not-a-key", "entry 2 must be 32 random bytes"),
        (f"{b64(OLD)},", "entry 2 must be 32 random bytes"),
        (b64(NEW), "entry 1 is the current PHI key or the input hash key"),
        (HASH_KEY_B64, "entry 1 is the current PHI key"),
        (f"{b64(OLD)},{b64(OLD)}", "entry 2 repeats an earlier one"),
        (",".join(b64(os.urandom(32)) for _ in range(5)), "at most 4 keys"),
    ):
        monkeypatch.setenv("MENDARI_PHI_READ_KEYS", listed)
        with pytest.raises(MendariError, match=says) as refused:
            phi_policy()
        assert b64(OLD) not in str(refused.value) and b64(NEW) not in str(refused.value)
    monkeypatch.setenv("MENDARI_PHI_READ_KEYS", b64(OLD))
    monkeypatch.setenv("MENDARI_PHI_MODE", "hash")
    with pytest.raises(MendariError, match="for PHI mode encrypt only"):
        phi_policy()


def test_health_names_the_keys_by_id_under_encrypt_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_PHI_MODE", "encrypt")
    monkeypatch.setenv("MENDARI_PHI_KEY", b64(NEW))
    monkeypatch.setenv("MENDARI_INPUT_HASH_KEY", HASH_KEY_B64)
    monkeypatch.setenv("MENDARI_PHI_READ_KEYS", b64(OLD))
    health = TestClient(create_app(db_path=tmp_path / "a.db")).get("/api/health")
    assert health.json()["phi_keys"] == {"current": key_id(NEW), "read": [key_id(OLD)]}
    assert b64(NEW) not in health.text and b64(OLD) not in health.text
    monkeypatch.setenv("MENDARI_PHI_MODE", "hash")
    monkeypatch.delenv("MENDARI_PHI_READ_KEYS")
    health = TestClient(create_app(db_path=tmp_path / "b.db")).get("/api/health")
    assert "phi_keys" not in health.json()


# -- rotating the PHI key -----------------------------------------------------------------------


def test_a_thousand_values_rotate_in_batches_and_everything_still_opens(
    db_target: str, inbox_pack: Pack
) -> None:
    before = AuditStore(db_target, phi=ring(OLD))
    ids = keep_inputs(before, 1000)
    noted = reviewed(before, inbox_pack, 5)
    before.close()
    store = AuditStore(db_target, phi=ring(NEW, OLD))  # the swap: new seals, old opens
    assert not store.phi_check()["ready"]  # sealed under the old key: not yet
    rotated = store.rotate_phi(to=key_id(NEW), batch=64)
    assert (rotated["inputs"], rotated["details"], rotated["notes"]) == (1005, 505, 5)
    assert rotated["unopenable"] == [] and set(rotated["from"]) == {key_id(OLD)}
    events = chained(store, "phi.rotated")
    assert len(events) == rotated["batches"] and sum(e["inputs"] for e in events) == 1005
    assert all(b64(OLD) not in json.dumps(e) and b64(NEW) not in json.dumps(e) for e in events)
    checked = store.phi_check()
    assert checked["ready"] is True and checked["failures"] == 0
    assert checked["by_key"]["inputs"] == {key_id(NEW): 1005 + 505}
    alone = AuditStore(db_target, phi=ring(NEW))  # the read key dropped
    assert alone.phi.decrypt(ids[7], _cipher(alone, ids[7])) == {"n": 7}
    item = alone.get(noted[0], reader="Nurse Kim")
    assert item is not None and SECRET in json.dumps(item)
    assert alone.verify(notes=True)["ok"] is True
    assert store.rotate_phi(to=key_id(NEW))["inputs"] == 0  # nothing left: a rerun is a no-op
    store.close()


def _cipher(store: AuditStore, decision_id: str) -> str:
    with store._db.session(exclusive=False) as db:
        row = db.one("SELECT ciphertext FROM decision_inputs WHERE decision_id = ?", (decision_id,))
    assert row is not None
    return str(row["ciphertext"])


def test_an_interrupted_rotation_resumes(db_target: str, monkeypatch: pytest.MonkeyPatch) -> None:
    keep_inputs(AuditStore(db_target, phi=ring(OLD)), 300)
    store = AuditStore(db_target, phi=ring(NEW, OLD))
    original, calls = AuditStore._chain_rotation, {"n": 0}

    def fails_on_the_third(self: AuditStore, *args: Any, **kwargs: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("the function timed out")
        original(self, *args, **kwargs)

    monkeypatch.setattr(AuditStore, "_chain_rotation", fails_on_the_third)
    with pytest.raises(RuntimeError):
        store.rotate_phi(to=key_id(NEW), batch=50)
    assert sum(e["inputs"] for e in chained(store, "phi.rotated")) == 100  # two batches stayed
    monkeypatch.setattr(AuditStore, "_chain_rotation", original)
    resumed = store.rotate_phi(to=key_id(NEW), batch=50)
    assert resumed["inputs"] == 200
    assert sum(e["inputs"] for e in chained(store, "phi.rotated")) == 300
    assert store.phi_check()["ready"] and store.verify()["ok"]
    store.close()


def test_a_value_changed_while_it_was_re_sealed_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "a.db"
    ids = keep_inputs(AuditStore(db, phi=ring(OLD)), 3)
    store = AuditStore(db, phi=ring(NEW, OLD))
    apply = AuditStore._apply_inputs

    def erase_one_first(self: AuditStore, changes: list[Any], summary: dict[str, Any]) -> None:
        with self._db.session(exclusive=True) as db:  # purged between the read and the write
            db.all("DELETE FROM decision_inputs WHERE decision_id = ?", (ids[1],))
        apply(self, changes, summary)

    monkeypatch.setattr(AuditStore, "_apply_inputs", erase_one_first)
    assert store.rotate_phi(to=key_id(NEW))["inputs"] == 2
    with store._db.session(exclusive=False) as db:
        assert db.one("SELECT 1 AS x FROM decision_inputs WHERE decision_id = ?", (ids[1],)) is None


def test_a_value_no_key_opens_is_listed_and_the_rest_rotates(
    tmp_path: Path,
    inbox_pack: Pack,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "a.db"
    keep_inputs(AuditStore(db, phi=ring(OLD)), 3)
    Decision(inbox_pack, store=AuditStore(db, phi=ring(OLD)))(message("refill please"))
    keep_inputs(AuditStore(db, phi=ring(OTHER)), 1, start=10)  # sealed under a key now lost
    monkeypatch.setenv("MENDARI_PHI_MODE", "encrypt")
    monkeypatch.setenv("MENDARI_PHI_KEY", b64(NEW))
    monkeypatch.setenv("MENDARI_INPUT_HASH_KEY", HASH_KEY_B64)
    monkeypatch.setenv("MENDARI_PHI_READ_KEYS", b64(OLD))
    assert main(["phi", "rotate", "--db", str(db), "--to", key_id(NEW)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["inputs"] == 4
    assert main(["phi", "rotate", "--db", str(tmp_path / "none.db"), "--check"]) == 1
    other = tmp_path / "other.db"  # another application's database: refused, left as it was
    with closing(sqlite3.connect(other)) as connection:
        connection.execute("CREATE TABLE things (id INTEGER)")
        connection.commit()
    assert main(["phi", "rotate", "--db", str(other), "--check"]) == 1
    assert "is not a Mendari database" in capsys.readouterr().err
    assert out["unopenable"] == [{"kind": "input", "id": "d-00010", "key": LEGACY}]
    assert main(["phi", "rotate", "--db", str(db), "--check"]) == 1  # not ready: one fails
    checked = json.loads(capsys.readouterr().out)
    assert checked["failed"] == [{"kind": "input", "id": "d-00010", "key": LEGACY}]
    with AuditStore(db, phi=ring(NEW, OLD))._db.session(exclusive=True) as conn:  # erased
        conn.all("DELETE FROM decision_inputs WHERE decision_id = 'd-00010'")
    assert main(["phi", "rotate", "--db", str(db), "--check"]) == 0


def test_a_rotation_refuses_the_wrong_key_and_a_retired_one(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    keep_inputs(AuditStore(db, phi=ring(OLD)), 3)
    store = AuditStore(db, phi=ring(NEW, OLD))
    with pytest.raises(MendariError, match=f"is key {key_id(NEW)}, not {key_id(OLD)}"):
        store.rotate_phi(to=key_id(OLD))
    with pytest.raises(MendariError, match="1 value or more"):
        store.rotate_phi(to=key_id(NEW), batch=0)
    store.rotate_phi(to=key_id(NEW), batch=1)
    back = AuditStore(db, phi=ring(OLD, NEW))  # back to the key just retired
    with pytest.raises(MendariError, match=f"key {key_id(OLD)} was retired by an earlier rotation"):
        back.rotate_phi(to=key_id(OLD))
    assert back.phi_check()["current_retired"] is True and not back.phi_check()["ready"]
    with pytest.raises(MendariError, match="runs under MENDARI_PHI_MODE=encrypt"):
        AuditStore(db, phi=PhiPolicy("hash", hash_key=INPUT_HASH_KEY)).rotate_phi(to="x")
    fresh = tmp_path / "fresh.db"
    with pytest.raises(MendariError, match="never kept under PHI mode"):
        AuditStore(fresh, phi=ring(NEW), marks=False).rotate_phi(to=key_id(NEW))


def test_rotations_follow_one_another_by_the_documented_steps(
    tmp_path: Path, inbox_pack: Pack
) -> None:
    """Old to new, then new to next: each step as docs/deploying-with-phi.md has it, decisions
    kept between the swap and the re-seal (under the key just swapped in) included."""
    db = tmp_path / "a.db"
    first, second, third = OLD, NEW, OTHER
    Decision(inbox_pack, store=AuditStore(db, phi=ring(first)))(message("one"))
    for current, previous in ((second, first), (third, second)):
        AuditStore(db, phi=ring(previous, current))  # reader first
        swapped = AuditStore(db, phi=ring(current, previous))  # the swap
        Decision(inbox_pack, store=swapped)(message("kept between swap and re-seal"))
        rotated = swapped.rotate_phi(to=key_id(current))
        assert rotated["unopenable"] == [] and key_id(current) not in rotated["from"]
        alone = AuditStore(db, phi=ring(current))
        assert alone.phi_check()["ready"] and alone.verify(notes=True)["ok"]
        alone.ready_to_keep()  # the key now deployed is never the one retired
    checked = AuditStore(db, phi=ring(third)).phi_check()
    assert checked["by_key"]["inputs"] == {key_id(third): 3}


def test_the_check_writes_nothing(tmp_path: Path, inbox_pack: Pack) -> None:
    db = tmp_path / "a.db"
    Decision(inbox_pack, store=AuditStore(db, phi=ring(OLD)))(message("refill please"))

    def written() -> list[dict[str, Any]]:
        with AuditStore(db, phi=ring(OLD), marks=False)._db.session(exclusive=False) as conn:
            return conn.all("SELECT name, value FROM settings ORDER BY name")

    before = written()
    store = AuditStore(db, phi=ring(NEW, OLD), marks=False)  # as `--check` opens it
    store.phi_check()
    assert written() == before and not store.phi.tag_tokens


def test_a_retired_key_seals_nothing_more(tmp_path: Path, inbox_pack: Pack) -> None:
    db = tmp_path / "a.db"
    stale = AuditStore(db, phi=ring(OLD))  # a deployment still on the previous key
    Decision(inbox_pack, store=stale)(message("refill please"))
    AuditStore(db, phi=ring(NEW, OLD)).rotate_phi(to=key_id(NEW))
    with pytest.raises(MendariError, match=f"key {key_id(OLD)} was retired by a key rotation"):
        Decision(inbox_pack, store=stale)(message(SECRET))  # refused before any provider
    with pytest.raises(MendariError, match="was retired"):
        stale.ready_to_keep()
    assert SECRET not in json.dumps(stale.query())
    Decision(inbox_pack, store=AuditStore(db, phi=ring(NEW)))(message("new key: fine"))


def test_the_check_catches_a_value_that_does_not_match_its_hash(
    tmp_path: Path, inbox_pack: Pack
) -> None:
    db = tmp_path / "a.db"
    store = AuditStore(db, phi=ring(NEW))
    decided = Decision(inbox_pack, store=store)(message("refill please"))
    assert store.phi_check()["ready"]
    with store._db.session(exclusive=True) as conn:  # another input, sealed under the right key
        conn.all(
            "UPDATE decision_inputs SET ciphertext = ? WHERE decision_id = ?",
            (store.phi.encrypt(decided.id, message("something else")), decided.id),
        )
    checked = store.phi_check()
    assert not checked["ready"]
    assert checked["failed"] == [{"kind": "input", "id": decided.id, "key": "input_hash"}]


def test_an_altered_note_still_breaks_verification(tmp_path: Path, inbox_pack: Pack) -> None:
    """Never rotated (one key, legacy tokens): an altered note breaks verification, as before.
    A forged key id, one no deployment of this database held, is an alteration too."""
    db = tmp_path / "a.db"
    store = AuditStore(db, phi=ring(OLD))
    reviewed(store, inbox_pack, 1)
    with store._db.session(exclusive=True) as conn:
        sealed = conn.one("SELECT id, sealed FROM notes")
        assert sealed is not None
        flipped = base64.b64decode(sealed["sealed"])
        flipped = flipped[:-1] + bytes([flipped[-1] ^ 1])
        conn.all("UPDATE notes SET sealed = ?", (base64.b64encode(flipped).decode(),))
    assert "broken_at" in store.verify(notes=True)
    forged = f"v2:{'0' * 16}:{base64.b64encode(flipped).decode()}"
    with store._db.session(exclusive=True) as conn:
        conn.all("UPDATE notes SET sealed = ?", (forged,))
    assert "broken_at" in store.verify(notes=True)


def test_verify_says_which_notes_no_key_here_opens(tmp_path: Path, inbox_pack: Pack) -> None:
    db = tmp_path / "a.db"
    rotating = AuditStore(db, phi=ring(OTHER, OLD))
    reviewed(rotating, inbox_pack, 2)  # sealed under OTHER, named (`v2:`)
    alone = AuditStore(db, phi=ring(NEW))
    verified = alone.verify(notes=True)
    assert verified["ok"] is False and "broken_at" not in verified
    assert verified["unopenable"] == {key_id(OTHER): 2}
    named = PhiPolicy("encrypt", OTHER, INPUT_HASH_KEY, (), tag_tokens=True)
    with alone._db.session(exclusive=True) as conn:  # altered: it names a key the ring has
        conn.all("UPDATE notes SET sealed = ?", (named.seal_note("x", "y", {"text": "?"}),))
    assert "broken_at" in AuditStore(db, phi=ring(OTHER)).verify(notes=True)


# -- the input hash key ---------------------------------------------------------------------------


def test_a_new_input_hash_key_starts_an_epoch(tmp_path: Path, inbox_pack: Pack) -> None:
    db = tmp_path / "a.db"
    first, second = secrets.token_bytes(32), secrets.token_bytes(32)
    one = AuditStore(db, phi=PhiPolicy("hash", hash_key=first))
    decide = Decision(inbox_pack, store=one)
    decide(message("refill please"))
    decide(message("refill again"))
    two = AuditStore(db, phi=PhiPolicy("hash", hash_key=second))
    later = Decision(inbox_pack, store=two)(message("refill please"))
    assert chained(one, "input_hash.epoch") == [
        {"key_id": key_id(first)},
        {"key_id": key_id(second)},
    ]
    for store in (one, two, one, two):  # two deployments on two keys, alternating
        Decision(inbox_pack, store=AuditStore(db, phi=store.phi))(message("refill"))
    assert len(chained(one, "input_hash.epoch")) == 2
    assert later.input_hash.startswith(f"hmac-sha256:{key_id(second)}:")  # never re-keyed
    epochs = two.phi_check()["hash_epochs"]
    assert {k: v["decisions"] for k, v in epochs.items()} == {key_id(first): 4, key_id(second): 3}
    assert two.verify()["ok"]


# -- API keys -------------------------------------------------------------------------------------


def _at(monkeypatch: pytest.MonkeyPatch, hours: float) -> None:
    """The store's clock, `hours` from now."""
    moment = (datetime.now(UTC) + timedelta(hours=hours)).isoformat(timespec="milliseconds")
    monkeypatch.setattr(audit_module, "_now", lambda: moment)


def test_a_rotated_key_overlaps_then_expires(
    db_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AuditStore(db_target)
    old = store.create_api_key(
        name="worker", role="service", org="clinic", created_by="admin@x.org", scopes="system/*.rs"
    )
    new = store.rotate_api_key(old["id"], "clinic", rotated_by="admin@x.org", overlap_hours=2)
    assert (new["name"], new["role"], new["org"], new["scope"]) == (
        "worker",
        "service",
        "clinic",
        "system/*.rs",
    )
    assert new["replaces"]["id"] == old["id"] and new["key"] != old["key"]
    assert store.api_key(old["key"]) is not None and store.api_key(new["key"]) is not None
    _at(monkeypatch, 3)  # past the overlap
    assert store.api_key(old["key"]) is None and store.api_key(new["key"]) is not None
    created = chained(store, "key.created")[-1]
    rotated = chained(store, "key.rotated")[-1]
    assert created["replaces"] == old["id"] and "key" not in created
    assert rotated["replaced_by"] == new["id"] and rotated["overlap_hours"] == 2
    assert rotated["expires_at"] == new["replaces"]["expires_at"]
    assert store.verify()["ok"]
    store.close()


def test_what_a_rotation_refuses(db_target: str, monkeypatch: pytest.MonkeyPatch) -> None:
    store = AuditStore(db_target)

    def key(**kwargs: Any) -> dict[str, Any]:
        return store.create_api_key(name="k", role="viewer", org="clinic", created_by="a", **kwargs)

    def rotate(which: str, org: str | None = "clinic", **kwargs: Any) -> dict[str, Any]:
        return store.rotate_api_key(which, org, rotated_by="a", **kwargs)

    for status, unknown, org in ((404, "nope", "clinic"), (404, key()["id"], "other")):
        with pytest.raises(AuthError) as refused:
            rotate(unknown, org)
        assert refused.value.status == status
    revoked = key()
    store.revoke_api_key(revoked["id"], revoked_by="a")
    replaced = key()
    replacement = rotate(replaced["id"])
    for stale, says in ((revoked, "revoked"), (replaced, "already replaced")):
        with pytest.raises(AuthError, match=says) as refused:
            rotate(stale["id"])
        assert refused.value.status == 409
    store.revoke_api_key(replacement["id"], revoked_by="a")  # its answer was lost
    assert rotate(replaced["id"])["replaces"]["id"] == replaced["id"]
    legacy = key()
    with store._db.session(exclusive=True) as db:  # a key from before keys were machines only
        db.all("UPDATE api_keys SET role = 'admin' WHERE id = ?", (legacy["id"],))
    with pytest.raises(AuthError, match="role, admin, is not one a key holds now"):
        rotate(legacy["id"])
    asks: list[tuple[dict[str, Any], str]] = [
        ({"overlap_hours": 200}, "0 to 168"),
        ({"overlap_hours": 30, "days": 1}, "longer"),
    ]
    for asked, says in asks:
        with pytest.raises(ValueError, match=says):
            rotate(key()["id"], "clinic", **asked)
    instant = key()
    rotate(instant["id"], overlap_hours=0)
    assert store.api_key(instant["key"]) is None  # a leaked key: refused at once
    expiring = key(days=1)
    _at(monkeypatch, 25)
    with pytest.raises(AuthError, match="expired"):
        rotate(expiring["id"])
    store.close()


@pytest.fixture
def admin_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, AuditStore, dict[str, str]]:
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    client = TestClient(create_app(db_path=tmp_path / "a.db"), base_url="https://mendari.test")
    store = AuditStore(tmp_path / "a.db")
    return client, store, signed_in(store, role="admin")


def test_admins_rotate_keys_through_the_api(
    admin_app: tuple[TestClient, AuditStore, dict[str, str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    client, _, admin = admin_app
    made = client.post("/api/auth/keys", json={"name": "worker", "role": "service"}, headers=admin)
    old = made.json()
    capsys.readouterr()
    rotated = client.post(
        f"/api/auth/keys/{old['id']}/rotate", json={"overlap_hours": 1}, headers=admin
    )
    assert rotated.status_code == 201 and rotated.json()["replaces"]["id"] == old["id"]
    lines = security_lines(capsys.readouterr().out)
    assert [(e["event"], e["ids"]) for e in lines if e["event"] == "admin.key.rotated"] == [
        ("admin.key.rotated", [old["id"], rotated.json()["id"]])
    ]
    for _ in range(2):  # the old key, in its overlap: works, and is logged once
        assert (
            client.get("/api/agents", headers={"authorization": f"Bearer {old['key']}"}).status_code
            == 200
        )
    used = [
        e for e in security_lines(capsys.readouterr().out) if e["event"] == "auth.key.replaced_used"
    ]
    assert len(used) == 1 and used[0]["ids"] == [old["id"], rotated.json()["id"]]
    again = client.post(f"/api/auth/keys/{old['id']}/rotate", headers=admin)
    assert again.status_code == 409
    assert client.post("/api/auth/keys/nope/rotate", headers=admin).status_code == 404
    as_key = {"authorization": f"Bearer {rotated.json()['key']}"}
    assert client.post(f"/api/auth/keys/{old['id']}/rotate", headers=as_key).status_code == 403
    long = client.post(
        f"/api/auth/keys/{rotated.json()['id']}/rotate",
        json={"overlap_hours": 48, "days": 1},
        headers=admin,
    )
    assert long.status_code == 422
    listed = {k["id"]: k for k in client.get("/api/auth/keys", headers=admin).json()}
    assert listed[old["id"]]["replaced_by"] == rotated.json()["id"]


def test_a_stale_scope_is_refused_and_nothing_changes(
    admin_app: tuple[TestClient, AuditStore, dict[str, str]],
) -> None:
    client, store, admin = admin_app
    old = store.create_api_key(name="k", role="viewer", org="default", created_by="a")
    with store._db.session(exclusive=True) as db:  # a scope Mendari no longer grants
        db.all("UPDATE api_keys SET scopes = 'system/Patient.cud' WHERE id = ?", (old["id"],))
    refused = client.post(f"/api/auth/keys/{old['id']}/rotate", headers=admin)
    assert refused.status_code == 422
    listed = {k["id"]: k for k in client.get("/api/auth/keys", headers=admin).json()}
    assert listed[old["id"]]["replaced_by"] is None and len(listed) == 1


def test_every_post_that_mints_a_credential_waits_for_the_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_backend_services import RSA_KEY

    monkeypatch.setenv("MENDARI_NOTICE_ACKNOWLEDGEMENT", "required")
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    monkeypatch.setenv("MENDARI_PUBLIC_URL", "https://mendari.test")
    monkeypatch.setenv("MENDARI_BACKEND_SERVICES", "on")  # clients' routes too
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    evalset_on(monkeypatch)  # and the routes hidden evaluation sets mount (LP-11g, conflict 37)
    app = create_app(db_path=tmp_path / "a.db")
    minting = sorted(
        path
        for method, path in mounted_routes(app)
        if method == "POST" and permission_for(method, path) == "keys"
    )
    assert "/api/auth/keys/{key_id}/rotate" in minting and "/api/auth/keys" in minting
    assert "/api/auth/clients" in minting and "/api/auth/clients/{client_id}/keys" in minting
    client = TestClient(app, base_url="https://mendari.test")
    store = AuditStore(tmp_path / "a.db")
    admin = signed_in(store, role="admin")
    evalsets_are_on(client, admin)
    key = store.create_api_key(name="k", role="viewer", org="default", created_by="a")
    registered = store.register_client(
        name="c", role="viewer", org="default", created_by="a", scopes="system/*.rs",
        jwks=[RSA_KEY.jwk],
    )  # fmt: skip
    for path in minting:
        concrete = path.replace("{key_id}", key["id"])
        concrete = concrete.replace("{client_id}", registered["client_id"])
        refused = client.post(concrete, json={}, headers=admin)
        assert refused.status_code == 403 and "mendari-notice-required" in refused.headers, path
    assert store.client(registered["client_id"])["keys_version"] == 0  # type: ignore[index]


# -- what reads stays a read ------------------------------------------------------------------


def test_verifying_notes_marks_no_key(
    tmp_path: Path, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mendari audit verify --notes` run with a stray key records nothing: it would otherwise be
    'known', and weaken how later verifications tell an altered note from a missing key."""
    db = tmp_path / "a.db"
    reviewed(AuditStore(db, phi=ring(OLD)), inbox_pack, 1)

    def settings() -> list[dict[str, Any]]:
        with AuditStore(db, marks=False)._db.session(exclusive=False) as conn:
            return conn.all("SELECT name, value FROM settings ORDER BY name")

    before = settings()
    monkeypatch.setenv("MENDARI_PHI_MODE", "encrypt")
    monkeypatch.setenv("MENDARI_PHI_KEY", b64(OTHER))  # not this database's key
    monkeypatch.setenv("MENDARI_INPUT_HASH_KEY", HASH_KEY_B64)
    monkeypatch.setenv("MENDARI_PHI_READ_KEYS", b64(NEW))
    assert main(["audit", "verify", "--db", str(db), "--notes"]) == 1
    assert settings() == before


def test_an_epoch_is_chained_even_when_the_first_record_under_its_key_fails(
    tmp_path: Path, inbox_pack: Pack
) -> None:
    db = tmp_path / "a.db"
    first, second = secrets.token_bytes(32), secrets.token_bytes(32)
    decided = Decision(inbox_pack, store=AuditStore(db, phi=PhiPolicy("hash", hash_key=first)))(
        message("refill please")
    )
    two = AuditStore(db, phi=PhiPolicy("hash", hash_key=second))
    with pytest.raises(sqlite3.IntegrityError):  # the same decision again: all rolls back
        two.record(decided, message("refill please"))
    Decision(inbox_pack, store=two)(message("refill again"))
    assert [e["key_id"] for e in chained(two, "input_hash.epoch")] == [
        key_id(first),
        key_id(second),
    ]


def test_the_database_check_reads_the_file_it_names(
    tmp_path: Path,
    inbox_pack: Pack,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path with "#" or "?" names that file, not a shorter one: a Mendari database at `prod`
    never vouches for another application's `prod#2.db`, which is left as it was."""
    Decision(inbox_pack, store=AuditStore(tmp_path / "prod", phi=ring(OLD)))(message("refill"))
    other = tmp_path / "prod#2.db"
    with closing(sqlite3.connect(other)) as connection:
        connection.execute("CREATE TABLE app_things (id INTEGER)")
        connection.commit()
    monkeypatch.setenv("MENDARI_PHI_MODE", "encrypt")
    monkeypatch.setenv("MENDARI_PHI_KEY", b64(OLD))
    monkeypatch.setenv("MENDARI_INPUT_HASH_KEY", HASH_KEY_B64)
    assert main(["phi", "rotate", "--db", str(other), "--check"]) == 1
    assert "is not a Mendari database" in capsys.readouterr().err
    with closing(sqlite3.connect(other)) as connection:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
    assert tables == {"app_things"}
    asked = tmp_path / "app?x.db"
    assert main(["phi", "rotate", "--db", str(asked), "--check"]) == 1  # no such file
    assert not (tmp_path / "app").exists()
