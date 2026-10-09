# SPDX-License-Identifier: Apache-2.0
"""The keys of hidden evaluation sets (LP-11c, `core/evalset_keys.py`, ADR-0011). Unset, the
feature is off and nothing changes. A malformed, half-set or reused key stops the server and the
command line, naming variables and positions only. With the keys, the server needs sign-in and,
on Vercel, Production. Health shows key ids only, and only to those who may read. A token opens
only for its kind, set, case and label, and always names its key. The PHI tokens sealed before
the sealing code moved still open. Keys are made per test; no value is real."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari.cli import main
from mendari.core.evalset_keys import (
    INDEX_PREFIX,
    EvalsetKeys,
    evalset_keys,
)
from mendari.core.phi import PhiPolicy, key_id
from mendari.errors import MendariError
from mendari.evals.canonical import canonical_input, normalised_input
from mendari.server.app import create_app
from tests.conftest import (
    HASH_KEY_B64,
    PHI_KEY_B64,
    encrypt_mode,
    encrypted_store,
    evalset_on,
    signed_in,
)
from tests.test_hosted_data import sign_in_env

SET = "6f1f3b1c-7f4e-4a52-9a0e-0d6c1c2b3a41"
OTHER_SET = "1b2c3d4e-5f60-4718-8293-a4b5c6d7e8f9"
CASE = "0e6b6a52-3a3f-4c55-8f59-7e2a1d0c9b8a"
OTHER_CASE = "9a8b7c6d-5e4f-4a3b-9c2d-1e0f2a3b4c5d"
LABEL = "3c4d5e6f-7a8b-4c9d-8e0f-1a2b3c4d5e6f"
OTHER_LABEL = "7e6d5c4b-3a29-4817-9605-f4e3d2c1b0a9"
HASHED = re.compile(r"hmac-sha256:[0-9a-f]{16}:[0-9a-f]{64}")


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def keys() -> EvalsetKeys:
    return EvalsetKeys(secrets.token_bytes(32), secrets.token_bytes(32))


def refused_everywhere(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], message: str, *secret: str
) -> None:
    """`message` stops `evalset_keys`, the server and `mendari serve`, and no value of `secret`
    is in what any of them says."""
    with pytest.raises(MendariError, match=re.escape(message)) as checked:
        evalset_keys()
    with pytest.raises(MendariError, match=re.escape(message)) as served:
        create_app(db_path=tmp_path / "a.db")
    assert main(["serve", "--db", str(tmp_path / "b.db")]) == 1
    printed = capsys.readouterr().err
    assert message in printed
    for said in (str(checked.value), str(served.value), printed):
        for value in secret:
            assert value not in said


def test_unset_keys_mean_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert evalset_keys() is None
    encrypt_mode(monkeypatch)
    sign_in_env(monkeypatch)
    client = TestClient(create_app(db_path=tmp_path / "a.db"), base_url="https://mendari.test")
    admin = signed_in(encrypted_store(tmp_path / "a.db"), role="admin")
    assert "evalset_keys" not in client.get("/api/health", headers=admin).json()


KEY, INDEX, ANOTHER = (b64(secrets.token_bytes(32)) for _ in range(3))
SHORT, NOT_BASE64 = b64(secrets.token_bytes(16)), "not+base64!"


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        (
            {"MENDARI_EVALSET_KEY": NOT_BASE64, "MENDARI_EVALSET_INDEX_KEY": INDEX},
            "MENDARI_EVALSET_KEY must be 32 random bytes, base64",
        ),
        (
            {"MENDARI_EVALSET_KEY": SHORT, "MENDARI_EVALSET_INDEX_KEY": INDEX},
            "MENDARI_EVALSET_KEY must be 32 random bytes, base64",
        ),
        (
            {"MENDARI_EVALSET_KEY": KEY, "MENDARI_EVALSET_INDEX_KEY": SHORT},
            "MENDARI_EVALSET_INDEX_KEY must be 32 random bytes, base64",
        ),
        (
            {"MENDARI_EVALSET_KEY": KEY},
            "MENDARI_EVALSET_KEY and MENDARI_EVALSET_INDEX_KEY are set together or not at all",
        ),
        (
            {"MENDARI_EVALSET_INDEX_KEY": INDEX},
            "MENDARI_EVALSET_KEY and MENDARI_EVALSET_INDEX_KEY are set together or not at all",
        ),
        (
            {"MENDARI_EVALSET_READ_KEYS": ANOTHER},
            "MENDARI_EVALSET_READ_KEYS is set without MENDARI_EVALSET_KEY",
        ),
        (
            {
                "MENDARI_EVALSET_KEY": KEY,
                "MENDARI_EVALSET_INDEX_KEY": INDEX,
                "MENDARI_EVALSET_READ_KEYS": f"{ANOTHER},{SHORT}",
            },
            "MENDARI_EVALSET_READ_KEYS entry 2 must be 32 random bytes, base64",
        ),
    ],
)
def test_a_malformed_or_half_set_key_stops_the_server_and_the_command_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: dict[str, str],
    message: str,
) -> None:
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    refused_everywhere(tmp_path, capsys, message, *settings.values())


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        (
            {"MENDARI_EVALSET_KEY": KEY, "MENDARI_EVALSET_INDEX_KEY": KEY},
            "MENDARI_EVALSET_INDEX_KEY must differ from MENDARI_EVALSET_KEY",
        ),
        (
            {"MENDARI_EVALSET_KEY": PHI_KEY_B64, "MENDARI_EVALSET_INDEX_KEY": INDEX},
            "MENDARI_EVALSET_KEY must differ from MENDARI_PHI_KEY",
        ),
        (
            {"MENDARI_EVALSET_KEY": KEY, "MENDARI_EVALSET_INDEX_KEY": HASH_KEY_B64},
            "MENDARI_EVALSET_INDEX_KEY must differ from MENDARI_INPUT_HASH_KEY",
        ),
        (
            {
                "MENDARI_EVALSET_KEY": KEY,
                "MENDARI_EVALSET_INDEX_KEY": INDEX,
                "MENDARI_CHECKPOINT_KEY": KEY,
            },
            "MENDARI_EVALSET_KEY must differ from MENDARI_CHECKPOINT_KEY",
        ),
        (
            {
                "MENDARI_EVALSET_KEY": KEY,
                "MENDARI_EVALSET_INDEX_KEY": INDEX,
                "MENDARI_PHI_MODE": "encrypt",  # where PHI read keys are allowed
                "MENDARI_PHI_READ_KEYS": f"{ANOTHER},{INDEX}",
            },
            "MENDARI_EVALSET_INDEX_KEY must differ from MENDARI_PHI_READ_KEYS entry 2",
        ),
        (
            {
                "MENDARI_EVALSET_KEY": KEY,
                "MENDARI_EVALSET_INDEX_KEY": INDEX,
                "MENDARI_EVALSET_READ_KEYS": KEY,
            },
            "MENDARI_EVALSET_READ_KEYS entry 1 is the current evaluation-set key, the index key "
            "or another of this deployment's keys",
        ),
        (
            {
                "MENDARI_EVALSET_KEY": KEY,
                "MENDARI_EVALSET_INDEX_KEY": INDEX,
                "MENDARI_EVALSET_READ_KEYS": f"{ANOTHER},{PHI_KEY_B64}",
            },
            "MENDARI_EVALSET_READ_KEYS entry 2 is the current evaluation-set key, the index key "
            "or another of this deployment's keys",
        ),
    ],
)
def test_the_keys_differ_from_each_other_and_from_every_other_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: dict[str, str],
    message: str,
) -> None:
    """Whatever the PHI mode: `off` here, with the PHI and hash keys still set (a deployment
    may set them before turning `encrypt` on), but for PHI read keys, which only `encrypt`
    takes."""
    monkeypatch.setenv("MENDARI_PHI_KEY", PHI_KEY_B64)
    monkeypatch.setenv("MENDARI_INPUT_HASH_KEY", HASH_KEY_B64)
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    refused_everywhere(tmp_path, capsys, message, *settings.values(), PHI_KEY_B64)


def test_the_keys_need_sign_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    evalset_on(monkeypatch)
    with pytest.raises(MendariError, match="turn on hidden evaluation sets, which need sign-in"):
        create_app(db_path=tmp_path / "a.db")


@pytest.mark.parametrize("environment", ["preview", "development", ""])
def test_the_keys_are_refused_on_vercel_outside_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    evalset_on(monkeypatch)
    encrypt_mode(monkeypatch)
    sign_in_env(monkeypatch)
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("VERCEL_ENV", environment)
    with pytest.raises(MendariError, match="for the hosted Production environment only"):
        create_app(db_path=tmp_path / "a.db")
    monkeypatch.setenv("VERCEL_ENV", "production")
    create_app(db_path=tmp_path / "b.db")  # it starts


def test_health_shows_key_ids_only_and_only_to_readers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key, index = evalset_on(monkeypatch)
    encrypt_mode(monkeypatch)
    sign_in_env(monkeypatch)
    client = TestClient(create_app(db_path=tmp_path / "a.db"), base_url="https://mendari.test")
    admin = signed_in(encrypted_store(tmp_path / "a.db"), role="admin")
    answer = client.get("/api/health", headers=admin)
    assert answer.json()["evalset_keys"] == {
        "current": key_id(key),
        "read": [],
        "index": key_id(index),
    }
    for value in (b64(key), b64(index), key.hex(), index.hex()):
        assert value not in answer.text
    assert "evalset_keys" not in client.get("/api/health").json()  # signed out: three fields


def test_a_token_opens_only_for_its_kind_set_case_and_label() -> None:
    sealed = keys()
    label = sealed.seal("label", {"q": "yes"}, SET, CASE, LABEL)
    other = sealed.seal("label", {"q": "no"}, SET, CASE, OTHER_LABEL)
    assert sealed.open("label", label, SET, CASE, LABEL)[0] == {"q": "yes"}
    for kind, ids in (
        ("label", (SET, CASE, OTHER_LABEL)),  # the two labels' tokens swapped
        ("label", (SET, OTHER_CASE, LABEL)),
        ("label", (OTHER_SET, CASE, LABEL)),
        ("attributes", (SET, CASE)),
        ("input", (SET, CASE)),
    ):
        with pytest.raises(MendariError, match="does not decrypt"):
            sealed.open(kind, label, *ids)  # type: ignore[arg-type]
    with pytest.raises(MendariError, match="does not decrypt"):
        sealed.open("label", other, SET, CASE, LABEL)
    header = sealed.seal("header", {"name": "synthetic"}, SET)
    assert sealed.open("header", header, SET)[0] == {"name": "synthetic"}
    for kind in ("freeze", "aggregates"):
        with pytest.raises(MendariError, match="does not decrypt"):
            sealed.open(kind, header, SET)  # type: ignore[arg-type]
    with pytest.raises(MendariError, match="belongs to no case or label"):
        sealed.seal("header", {}, SET, CASE)
    with pytest.raises(MendariError, match="belongs to no label"):
        sealed.seal("input", {}, SET, CASE, LABEL)
    with pytest.raises(MendariError, match="a case id must be a UUID"):
        sealed.seal("input", {}, SET)  # a case's value needs its case
    for bad in ("set-1", SET.upper(), SET.replace("-", "")):
        with pytest.raises(MendariError, match="a set id must be a UUID") as refused:
            sealed.seal("header", {}, bad)
        assert bad not in str(refused.value)


def test_every_token_names_its_key_and_read_keys_open() -> None:
    old, new_key, index = (secrets.token_bytes(32) for _ in range(3))
    before = EvalsetKeys(old, index)
    token = before.seal("input", {"text": "synthetic"}, SET, CASE)
    assert token.startswith(f"v2:{key_id(old)}:")
    with pytest.raises(MendariError, match="does not name its key"):
        before.open("input", token.split(":", 2)[2], SET, CASE)  # as a legacy PHI token is
    rotating = EvalsetKeys(new_key, index, read_keys=(old,))
    assert rotating.open("input", token, SET, CASE) == ({"text": "synthetic"}, key_id(old))
    assert rotating.ring_ids == {key_id(new_key), key_id(old)}
    assert rotating.seal("input", {}, SET, CASE).startswith(f"v2:{key_id(new_key)}:")
    with pytest.raises(MendariError, match="which this deployment lacks"):
        rotating.current_only().open("input", token, SET, CASE)


def test_case_and_index_hashes_are_keyed_and_name_the_index_key() -> None:
    sealed = keys()
    hashed = sealed.case_hash(SET, CASE)
    assert HASHED.fullmatch(hashed) and hashed.split(":")[1] == key_id(sealed.index_key)
    assert hashed != sealed.case_hash(SET, OTHER_CASE) != sealed.case_hash(OTHER_SET, CASE)
    assert hashed != EvalsetKeys(sealed.key, secrets.token_bytes(32)).case_hash(SET, CASE)
    value: dict[str, Any] = {"message": {"body": "Chest pain, since Monday!"}, "age": 57}
    alike: dict[str, Any] = {"message": {"body": "chest PAIN since monday"}, "age": 57.0}
    first, second = sealed.index_hashes(value), sealed.index_hashes(alike)
    assert first["canonical"] != second["canonical"] and first["text"] == second["text"]
    for kind, form in (("canonical", canonical_input), ("text", normalised_input)):
        message = INDEX_PREFIX + kind.encode() + b"\x00" + form(value).encode()
        digest = hmac.new(sealed.index_key, message, hashlib.sha256).hexdigest()
        assert first[kind] == f"hmac-sha256:{key_id(sealed.index_key)}:{digest}"


# Tokens sealed by the PHI policy before its sealing moved into `seal_with` and `open_with`
# (made with the previous code, under these fixed test keys): they must open unchanged.
FIXED_KEY, FIXED_HASH_KEY = bytes(range(32)), bytes(range(32, 64))
LEGACY_INPUT = "cBcxTgstq8TCrALODYWnpSQ7sPMi01YsGBhDrtcfq2HJTLa0o/du2rCMJQmccgV/jZeyprxg"
TAGGED_INPUT = (
    "v2:1e325e07b495123f:D7Lg/0+q9pRADE7EzOSR5uY8k19mG/H/5ZI/pFiuKWhPSq+LMOZK0v7j+Qn86UHp1cbHg5h6"
)
TAGGED_NOTE = "v2:1e325e07b495123f:ejelvWyNJ9Yig4XU79toEIFX05b7AC9GTcw9KSaMYhhrSD4Y4Ue4g0mW9u5AaQ=="
TAGGED_DETAILS = "v2:1e325e07b495123f:APA+QfD5ZVw/oi9bZHbfy8Hnt77+ZMk+OoS53T/Pd+IrLyL48+lWEkHf"


def test_phi_tokens_sealed_before_the_move_still_open() -> None:
    policy = PhiPolicy("encrypt", FIXED_KEY, FIXED_HASH_KEY)
    for token in (LEGACY_INPUT, TAGGED_INPUT):
        assert policy.decrypt("d-1", token) == {"n": 1, "text": "synthetic"}
    assert policy.open_note("n-1", "d-1", TAGGED_NOTE) == "a synthetic note"
    assert policy.open_details("d-1", TAGGED_DETAILS) == [["evidence"]]
    tagged = PhiPolicy("encrypt", FIXED_KEY, FIXED_HASH_KEY, tag_tokens=True)
    assert tagged.encrypt("d-1", {}).startswith("v2:1e325e07b495123f:")
    assert ":" not in policy.encrypt("d-1", {})  # untagged before the first rotation, as before
