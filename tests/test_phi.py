"""PHI mode (core/phi.py): inputs never stored, or stored encrypted, read by role and logged,
and purged by a retention policy without breaking the hash chain."""

from __future__ import annotations

import base64
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, Pack
from mendari.cli import main
from mendari.core.phi import PhiPolicy, phi_policy
from mendari.errors import MendariError
from mendari.server.app import create_app
from mendari.server.auth import Principal
from tests.conftest import message, signed_in

KEY = os.urandom(32)
SECRET_WORDS = "my neighbour Rosalind Q. Example has chest pain"


def encrypting(tmp_path: Path, name: str = "a.db") -> AuditStore:
    return AuditStore(tmp_path / name, phi=PhiPolicy(mode="encrypt", key=KEY))


def test_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MENDARI_PHI_MODE", raising=False)
    assert phi_policy().mode == "off"
    monkeypatch.setenv("MENDARI_PHI_MODE", "encrypt")
    monkeypatch.delenv("MENDARI_PHI_KEY", raising=False)
    with pytest.raises(MendariError, match="needs MENDARI_PHI_KEY"):
        phi_policy()
    monkeypatch.setenv("MENDARI_PHI_KEY", base64.b64encode(b"short").decode())
    with pytest.raises(MendariError, match="32 random bytes"):
        phi_policy()
    monkeypatch.setenv("MENDARI_PHI_KEY", base64.b64encode(KEY).decode())
    assert phi_policy().mode == "encrypt"
    monkeypatch.setenv("MENDARI_PHI_MODE", "plaintext")
    with pytest.raises(MendariError, match="off, hash or encrypt"):
        phi_policy()


def test_hash_mode_stores_no_input(tmp_path: Path, inbox_pack: Pack) -> None:
    store = AuditStore(tmp_path / "a.db", phi=PhiPolicy(mode="hash"))
    result = Decision(inbox_pack, store=store)(message(SECRET_WORDS))
    item = store.get(result.id, reader="anyone")
    assert item is not None and item["record"]["input"] is None
    assert item["record"]["input_hash"].startswith("sha256:")
    with store._db.session(exclusive=False) as db:
        assert db.all("SELECT * FROM decision_inputs") == []


def test_encrypt_mode_keeps_only_ciphertext_and_reveals_it_to_readers(
    tmp_path: Path, inbox_pack: Pack
) -> None:
    store = encrypting(tmp_path)
    result = Decision(inbox_pack, store=store)(message(SECRET_WORDS))
    with store._db.session(exclusive=False) as db:
        rows = db.all("SELECT ciphertext FROM decision_inputs") + db.all(
            "SELECT record FROM decisions"
        )
    assert all("Rosalind" not in str(row) for row in rows)  # nowhere in plain text
    hidden = store.get(result.id)
    assert (
        hidden is not None
        and hidden["record"]["input"] is None
        and hidden["input_restricted"] is True
    )
    shown = store.get(result.id, reader="Nurse Kim")
    assert shown is not None and shown["record"]["input"] == message(SECRET_WORDS)
    assert shown["events"][-1]["event"] == "input.read"
    assert shown["events"][-1]["payload"] == {"reader": "Nurse Kim"}
    assert store.verify()["ok"] is True


def test_a_ciphertext_only_opens_for_its_own_decision(tmp_path: Path, inbox_pack: Pack) -> None:
    store = encrypting(tmp_path)
    decide = Decision(inbox_pack, store=store)
    first, second = decide(message(SECRET_WORDS)), decide(message("refill please"))
    with store._db.session(exclusive=True) as db:
        stolen = db.one("SELECT ciphertext FROM decision_inputs WHERE decision_id = ?", (first.id,))
        assert stolen is not None
        db.all(
            "UPDATE decision_inputs SET ciphertext = ? WHERE decision_id = ?",
            (stolen["ciphertext"], second.id),
        )
    with pytest.raises(MendariError, match="does not decrypt"):
        store.get(second.id, reader="Nurse Kim")
    wrong_key = AuditStore(tmp_path / "a.db", phi=PhiPolicy(mode="encrypt", key=os.urandom(32)))
    with pytest.raises(MendariError, match="does not decrypt"):
        wrong_key.get(first.id, reader="Nurse Kim")


def test_retention_purges_inputs_and_keeps_the_chain(tmp_path: Path, inbox_pack: Pack) -> None:
    store = encrypting(tmp_path)
    decide = Decision(inbox_pack, store=store)
    old, new = decide(message(SECRET_WORDS)), decide(message("refill please"))
    long_ago = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    with store._db.session(exclusive=True) as db:
        db.all(
            "UPDATE decision_inputs SET created_at = ? WHERE decision_id = ?", (long_ago, old.id)
        )
    assert store.purge_inputs(365) == 1
    purged = store.get(old.id, reader="Nurse Kim")
    assert purged is not None and "input_restricted" not in purged  # nothing left to reveal
    assert purged["events"][-1]["event"] == "input.purged"
    kept = store.get(new.id, reader="Nurse Kim")
    assert kept is not None and kept["record"]["input"] == message("refill please")
    assert store.verify()["ok"] is True


def test_only_reviewers_and_admins_read_inputs() -> None:
    def principal(kind: str, role: str) -> Principal:
        return Principal(kind=kind, name=role, roles=frozenset({role}), id=f"{kind}:{role}")

    assert principal("user", "viewer").may("read_input") is False
    assert principal("key", "service").may("read_input") is False
    assert principal("user", "reviewer").may("read_input") is True
    assert principal("user", "admin").may("read_input") is True


def test_the_server_reports_the_mode_and_reads_through_the_policy(
    tmp_path: Path, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_PHI_MODE", "encrypt")
    monkeypatch.setenv("MENDARI_PHI_KEY", base64.b64encode(KEY).decode())
    client = TestClient(create_app(db_path=tmp_path / "a.db"))
    assert client.get("/api/health").json()["phi_mode"] == "encrypt"
    decided = client.post(f"/api/packs/{inbox_pack.id}/decide", json=message(SECRET_WORDS)).json()
    record = client.get(f"/api/audit/{decided['id']}").json()
    assert record["record"]["input"] is None and record["input_restricted"] is True
    # without sign-in there is no one to log the read against, so the input stays sealed
    listed = client.get("/api/audit").json()
    assert "Rosalind" not in str(listed)  # lists never carry inputs in this mode


def test_signed_in_reviewers_read_inputs_and_each_read_is_chained(
    tmp_path: Path, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_PHI_MODE", "encrypt")
    monkeypatch.setenv("MENDARI_PHI_KEY", base64.b64encode(KEY).decode())
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    db = tmp_path / "a.db"
    client = TestClient(create_app(db_path=db), base_url="https://mendari.test")
    viewer = encrypting(tmp_path).create_api_key(
        name="nurse viewer", role="viewer", org="default", created_by="test"
    )
    keys = {
        "reviewer": signed_in(
            encrypting(tmp_path), role="reviewer"
        ),  # a person: "default reviewer"
        "viewer": {"authorization": f"Bearer {viewer['key']}"},
    }
    urgent = message("Crushing chest pain, " + SECRET_WORDS)  # waits for review
    path = f"/api/packs/{inbox_pack.id}/decide"
    decided = client.post(path, json=urgent, headers=keys["reviewer"]).json()
    client.post(
        f"/api/audit/{decided['id']}/feedback", json={"rating": "up"}, headers=keys["reviewer"]
    )

    for url in (f"/api/audit/{decided['id']}", "/api/review", "/api/feedback"):
        seen = client.get(url, headers=keys["viewer"]).json()
        assert "Rosalind" not in str(seen), url
        read = client.get(url, headers=keys["reviewer"]).json()
        assert "Rosalind" in str(read), url

    events = client.get(f"/api/audit/{decided['id']}", headers=keys["viewer"]).json()["events"]
    readers = [e["payload"]["reader"] for e in events if e["event"] == "input.read"]
    assert readers == ["default reviewer"] * 3  # the reviewer's three reads; none for the viewer
    assert encrypting(tmp_path).verify()["ok"] is True


def test_the_cli_purges(
    tmp_path: Path,
    inbox_pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MENDARI_PHI_MODE", "encrypt")
    monkeypatch.setenv("MENDARI_PHI_KEY", base64.b64encode(KEY).decode())
    store = encrypting(tmp_path)
    Decision(inbox_pack, store=store)(message(SECRET_WORDS))
    assert main(["phi", "purge", "--db", str(tmp_path / "a.db"), "--days", "30"]) == 0
    assert "0 stored input(s) older than 30 days deleted" in capsys.readouterr().out
    monkeypatch.delenv("MENDARI_RETENTION_DAYS", raising=False)
    assert main(["phi", "purge", "--db", str(tmp_path / "a.db")]) == 1
