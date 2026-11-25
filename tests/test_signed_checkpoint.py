# SPDX-License-Identifier: Apache-2.0
"""The signed daily checkpoint (LP-21k): `record_checkpoint(signer=...)` signs the verified head
once a local day; the statement names the head and the day only (no sequence number, no
count); a full verification against it fails for a chain rewritten from before it, even with
every hash and every checkpoint row rewritten too. On SQLite and Postgres (`db_target`)."""

from __future__ import annotations

import base64
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mendari import Agent, AuditStore
from mendari.cli import main
from mendari.core import checkpoint
from mendari.core.checkpoint import FORMAT, KEY_ENV, checkpoint_key, public_text
from mendari.errors import MendariError
from tests.test_chain_orgs import tamper
from tests.test_checkpoints import decide, rewrite_from

SEED = bytes(range(32))


def key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(SEED)


def test_a_statement_names_the_head_and_the_day_and_nothing_can_change_unnoticed() -> None:
    signer = key()
    statement = checkpoint.sign("a" * 64, date(2026, 10, 8), signer)
    assert set(statement) == {"format", "date", "head", "key_id", "signature"}
    assert (statement["format"], statement["date"]) == (FORMAT, "2026-10-08")
    public = signer.public_key()
    assert checkpoint.problem(statement, public) is None
    for field, value in (("head", "b" * 64), ("date", "2026-10-09")):
        assert checkpoint.problem({**statement, field: value}, public) == (
            "its signature does not hold"
        )
    other = Ed25519PrivateKey.generate().public_key()
    assert checkpoint.problem(statement, other) == "it was signed with another key"
    assert checkpoint.problem({**statement, "head": "x"}, public) == "its head is not a chain hash"
    assert checkpoint.problem({**statement, "format": "v0"}, public) == (
        f"not a {FORMAT} statement"
    )
    assert checkpoint.problem({**statement, "signature": "!!"}, public) == (
        "its signature does not hold"
    )
    assert checkpoint.load_public(public_text(public)).public_bytes_raw() == (
        public.public_bytes_raw()
    )


def test_the_key_is_32_bytes_in_base64_or_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(KEY_ENV, raising=False)
    assert checkpoint_key() is None
    monkeypatch.setenv(KEY_ENV, base64.b64encode(SEED).decode())
    found = checkpoint_key()
    assert found is not None and public_text(found.public_key()) == public_text(key().public_key())
    for bad in ("short", base64.b64encode(b"x" * 31).decode()):
        monkeypatch.setenv(KEY_ENV, bad)
        with pytest.raises(MendariError, match=KEY_ENV):
            checkpoint_key()


def test_the_day_s_statement_is_signed_once_and_a_rewritten_chain_fails_against_it(
    db_target: str, triage_agent: Agent
) -> None:
    with AuditStore(db_target) as store:
        assert store.record_checkpoint(signer=key())["signed"] is None  # an empty chain
        assert store.latest_statement() is None
        decide(store, triage_agent, 3)
        first = store.record_checkpoint(signer=key())
        signed = first["signed"]
        assert signed["head"] == first["head"] and store.latest_statement() == signed
        assert "seq" not in signed and "events" not in signed  # no count, no position
        decide(store, triage_agent, 1)
        later = store.record_checkpoint(signer=key())  # the same day: the morning's statement
        assert later["recorded"] is not None and later["signed"] == signed
        assert store.verify(head=signed["head"])["ok"]
        # An attacker rewrites the chain from its first event, every hash after it, and the
        # checkpoint rows and statements the database holds: only the copy kept elsewhere tells.
        rewrite_from(store, 1)
        tamper(store, "DELETE FROM audit_checkpoints")
        tamper(store, "DELETE FROM checkpoint_statements")
        assert store.verify()["ok"]
        against = store.verify(head=signed["head"])
        assert (against["ok"], against["reason"]) == (False, "statement")
        with pytest.raises(ValueError, match="full verification"):
            store.verify(since_checkpoint=True, head=signed["head"])


def test_the_commands_sign_and_verify_against_a_statement(
    tmp_path: Path,
    triage_agent: Agent,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = str(tmp_path / "a.db")
    with AuditStore(db) as store:
        decide(store, triage_agent, 2)

    def run(*argv: str) -> tuple[int, Any, str]:
        code = main(list(argv))
        out, err = capsys.readouterr()
        return code, json.loads(out) if out.strip() else None, err

    monkeypatch.delenv(KEY_ENV, raising=False)
    code, unsigned, _ = run("audit", "checkpoint", "--db", db)
    assert code == 0 and "signed" not in unsigned  # no key: nothing signed
    monkeypatch.setenv(KEY_ENV, base64.b64encode(SEED).decode())
    code, done, _ = run("audit", "checkpoint", "--db", db)
    assert code == 0 and done["signed"]["head"] == done["head"]
    statement = tmp_path / "statement.json"
    statement.write_text(json.dumps(done["signed"]))
    public = public_text(key().public_key())
    code, verified, _ = run(
        "audit", "verify", "--db", db, "--against", str(statement), "--public-key", public
    )
    assert code == 0 and verified["ok"]
    published = tmp_path / "published.json"  # the route's answer, saved as it is
    published.write_text(json.dumps({"statement": done["signed"], "public_key": public,
                                     "key_id": done["signed"]["key_id"]}))  # fmt: skip
    code, _, _ = run(
        "audit", "verify", "--db", db, "--against", str(published), "--public-key", public
    )
    assert code == 0
    forged = tmp_path / "forged.json"
    forged.write_text(json.dumps({**done["signed"], "head": "c" * 64}))
    code, _, err = run(
        "audit", "verify", "--db", db, "--against", str(forged), "--public-key", public
    )
    assert code == 1 and "--against: its signature does not hold" in err
    code, _, err = run("audit", "verify", "--db", db, "--against", str(statement))
    assert code == 1 and "go together" in err
    code, _, err = run(
        "audit", "verify", "--db", db, "--against", str(statement), "--public-key", public,
        "--since-checkpoint",
    )  # fmt: skip
    assert code == 1 and "full verification" in err
    monkeypatch.setenv(KEY_ENV, "not-a-key")
    code, _, err = run("audit", "checkpoint", "--db", db)
    assert code == 1 and KEY_ENV in err
    with AuditStore(db) as store:  # the chain rewritten, its checkpoints and statements too
        rewrite_from(store, 1)
        tamper(store, "DELETE FROM audit_checkpoints")
        tamper(store, "DELETE FROM checkpoint_statements")
    assert run("audit", "verify", "--db", db)[0] == 0  # the chain alone holds
    code, verified, _ = run(
        "audit", "verify", "--db", db, "--against", str(statement), "--public-key", public
    )
    assert code == 1 and (verified["ok"], verified["reason"]) == (False, "statement")


# -- the routes ------------------------------------------------------------------------------------

CRON = "a-cron-secret-of-some-length-0123456789"
HOOK = "https://hooks.example.org/services/T000/B000/WebhookSecretToken"


def serving(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, signing: bool = True) -> Any:
    from fastapi.testclient import TestClient

    from mendari.server.app import create_app
    from tests.conftest import encrypt_mode
    from tests.test_hosted_data import sign_in_env

    encrypt_mode(monkeypatch)
    sign_in_env(monkeypatch)
    monkeypatch.setenv("CRON_SECRET", CRON)
    monkeypatch.setenv("MENDARI_ALERT_WEBHOOK", HOOK)
    if signing:
        monkeypatch.setenv(KEY_ENV, base64.b64encode(SEED).decode())
    else:
        monkeypatch.delenv(KEY_ENV, raising=False)
    return TestClient(create_app(db_path=tmp_path / "a.db"), base_url="https://mendari.test")


def test_the_scheduled_checkpoint_signs_the_day_and_publishes_it_to_anyone(
    tmp_path: Path, triage_agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.conftest import encrypted_store

    client = serving(tmp_path, monkeypatch)
    assert client.get("/api/audit/checkpoint").status_code == 404  # nothing signed yet
    assert client.get("/api/internal/checkpoint").status_code == 401  # the secret only
    store = encrypted_store(tmp_path / "a.db")
    decide(store, triage_agent, 2)
    ran = client.get("/api/internal/checkpoint", headers={"authorization": f"Bearer {CRON}"})
    assert ran.json() == {"ok": True, "recorded": True, "signed": True}
    published = client.get("/api/audit/checkpoint")  # public: no sign-in
    assert published.status_code == 200
    body = published.json()
    assert set(body) == {"statement", "public_key", "key_id"}
    public = checkpoint.load_public(body["public_key"])
    assert checkpoint.problem(body["statement"], public) is None
    assert body["statement"]["head"] == store.verify()["head"]


def test_a_broken_chain_signs_nothing_and_alerts_the_operator(
    tmp_path: Path, triage_agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    from mendari import alerts as alerts_module
    from tests.conftest import encrypted_store

    sent: list[dict[str, Any]] = []

    def post(url: str, **kwargs: Any) -> httpx.Response:
        sent.append(json.loads(kwargs["content"]))
        return httpx.Response(204, request=httpx.Request("POST", url))

    monkeypatch.setattr(alerts_module.httpx, "post", post)
    client = serving(tmp_path, monkeypatch)
    store = encrypted_store(tmp_path / "a.db")
    decide(store, triage_agent, 2)
    tamper(store, "UPDATE audit_events SET payload = '{}' WHERE seq = 1")
    ran = client.get("/api/internal/checkpoint", headers={"authorization": f"Bearer {CRON}"})
    assert ran.json() == {"ok": False, "recorded": False, "signed": False}
    assert store.checkpoints() == [] and store.latest_statement() is None
    (alert,) = sent
    assert (alert["event"], alert["organization"]) == ("audit.broken", None)
    assert alert["alerts"][0]["kind"] == "audit_broken"
    assert client.get("/api/audit/checkpoint").status_code == 404


def test_without_a_key_nothing_is_signed_or_published(
    tmp_path: Path, triage_agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.conftest import encrypted_store

    client = serving(tmp_path, monkeypatch, signing=False)
    decide(encrypted_store(tmp_path / "a.db"), triage_agent, 1)
    ran = client.get("/api/internal/checkpoint", headers={"authorization": f"Bearer {CRON}"})
    assert ran.json() == {"ok": True, "recorded": True, "signed": False}
    missing = client.get("/api/audit/checkpoint")
    assert missing.status_code == 404 and "no key" in missing.text


def test_the_published_checkpoint_is_read_once_a_minute_whoever_asks(
    tmp_path: Path, triage_agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time as real_time

    from mendari.core.audit import AuditStore as Store
    from mendari.server import app as app_module
    from tests.conftest import encrypted_store

    class Clock:
        now = 0.0

        def monotonic(self) -> float:
            return self.now

        def __getattr__(self, name: str) -> Any:
            return getattr(real_time, name)

    clock = Clock()
    monkeypatch.setattr(app_module, "time", clock)
    client = serving(tmp_path, monkeypatch)
    decide(encrypted_store(tmp_path / "a.db"), triage_agent, 1)
    client.get("/api/internal/checkpoint", headers={"authorization": f"Bearer {CRON}"})
    reads: list[int] = []
    real = Store.statements

    def counted(self: Store, **kwargs: Any) -> list[dict[str, Any]]:
        reads.append(1)
        return real(self, **kwargs)

    monkeypatch.setattr(Store, "statements", counted)
    for _ in range(5):
        assert client.get("/api/audit/checkpoint").status_code == 200
    assert len(reads) == 1
    clock.now += 61  # a minute on: read again
    assert client.get("/api/audit/checkpoint").status_code == 200
    assert len(reads) == 2


def test_the_published_answer_is_the_statement_and_the_key_and_nothing_else(
    tmp_path: Path, triage_agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.conftest import encrypted_store

    client = serving(tmp_path, monkeypatch)
    decide(encrypted_store(tmp_path / "a.db"), triage_agent, 1)
    client.get("/api/internal/checkpoint", headers={"authorization": f"Bearer {CRON}"})
    body = client.get("/api/audit/checkpoint").json()
    assert set(body) == {"statement", "public_key", "key_id"}
    assert set(body["statement"]) == {"format", "date", "head", "key_id", "signature"}


def test_a_statement_planted_in_the_database_is_never_signed_over_or_published(
    tmp_path: Path, triage_agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Someone who can write the database but has no key copies a valid statement under later
    days' keys, to freeze signing: the next run signs nothing, alerts, and the route keeps
    publishing only a statement this key made for the day it is filed under."""
    import httpx

    from mendari import alerts as alerts_module
    from mendari.core import clock as clock_module
    from tests.conftest import encrypted_store

    sent: list[dict[str, Any]] = []

    def post(url: str, **kwargs: Any) -> httpx.Response:
        sent.append(json.loads(kwargs["content"]))
        return httpx.Response(204, request=httpx.Request("POST", url))

    monkeypatch.setattr(alerts_module.httpx, "post", post)
    client = serving(tmp_path, monkeypatch)
    store = encrypted_store(tmp_path / "a.db")
    decide(store, triage_agent, 2)
    run = {"authorization": f"Bearer {CRON}"}
    assert client.get("/api/internal/checkpoint", headers=run).json()["signed"] is True
    published = client.get("/api/audit/checkpoint").json()["statement"]
    today = clock_module.today()
    tomorrow, far = (today + timedelta(days=1)).isoformat(), "9999-12-31"
    for day in (tomorrow, far):
        tamper(store, "INSERT INTO checkpoint_statements (day, seq, statement) VALUES (?, 1, ?)",
               (day, json.dumps(published)))  # fmt: skip
    monkeypatch.setattr(clock_module, "today", lambda: today + timedelta(days=1))
    decide(store, triage_agent, 1)
    answer = client.get("/api/internal/checkpoint", headers=run).json()
    assert answer == {"ok": False, "recorded": True, "signed": False}
    (alert,) = sent
    assert alert["event"] == "audit.broken" and "stored checkpoint statement" in str(alert)
    shown = client.get("/api/audit/checkpoint").json()["statement"]
    assert shown == published  # the real one, never the copy filed under another day


def test_after_a_key_rotation_only_the_new_key_s_statements_are_published(
    tmp_path: Path, triage_agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from mendari.server.app import create_app
    from tests.conftest import encrypted_store

    client = serving(tmp_path, monkeypatch)
    store = encrypted_store(tmp_path / "a.db")
    decide(store, triage_agent, 1)
    run = {"authorization": f"Bearer {CRON}"}
    client.get("/api/internal/checkpoint", headers=run)
    rotated = bytes(range(1, 33))
    monkeypatch.setenv(KEY_ENV, base64.b64encode(rotated).decode())
    client = TestClient(create_app(db_path=tmp_path / "a.db"), base_url="https://mendari.test")
    assert client.get("/api/audit/checkpoint").status_code == 404  # none signed with it yet
    assert client.get("/api/internal/checkpoint", headers=run).json()["signed"] is True
    body = client.get("/api/audit/checkpoint").json()
    new = Ed25519PrivateKey.from_private_bytes(rotated).public_key()
    assert body["key_id"] == checkpoint.key_id(new)
    assert checkpoint.problem(body["statement"], checkpoint.load_public(body["public_key"])) is None
    (row,) = store.statements(until="9999-12-31")
    assert row["statement"]["key_id"] == checkpoint.key_id(new)  # the day's, replaced


def test_a_malformed_key_stops_the_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mendari.server.app import create_app

    monkeypatch.setenv(KEY_ENV, "not a key")
    with pytest.raises(MendariError, match=KEY_ENV):
        create_app(db_path=tmp_path / "a.db")


def test_health_names_the_signing_key_s_id_to_those_who_may_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dashboard asks for the checkpoint only when a key is set (`checkpoint_key`): its id,
    never the key, and not in the public answer."""
    from fastapi.testclient import TestClient

    from mendari.server.app import create_app

    monkeypatch.delenv(KEY_ENV, raising=False)
    off = TestClient(create_app(db_path=tmp_path / "a.db"))  # open mode: it may read
    assert off.get("/api/health").json()["checkpoint_key"] is None
    monkeypatch.setenv(KEY_ENV, base64.b64encode(SEED).decode())
    on = TestClient(create_app(db_path=tmp_path / "b.db"))
    assert on.get("/api/health").json()["checkpoint_key"] == checkpoint.key_id(key().public_key())
    signed_out = serving(tmp_path, monkeypatch)  # sign-in on, no one signed in
    assert "checkpoint_key" not in signed_out.get("/api/health").json()
