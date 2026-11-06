# SPDX-License-Identifier: Apache-2.0
"""Signed scoreboard results and the public verifier (LP-17e, `mendari.evals.signing`; ADR-0013
and the grading method's section 11): a result signed with the grading key verifies with its
public half alone, and any changed byte fails; a publisher's, the registry's or another
Mendari signature never passes as a result's; the key is the key holder's, offline, never CI's,
and differs from every other key. Every key here is made at run time."""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import math
import os
import secrets
import tarfile
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mendari.cli import main
from mendari.core import checkpoint
from mendari.core.checkpoint import key_id, public_text
from mendari.core.evalset_keys import evalset_keys
from mendari.core.pack import Pack
from mendari.errors import MendariError
from mendari.evals import method, signing
from mendari.evals.ledger import canonical
from mendari.hub import archive, yank
from mendari.hub.client import config_dir
from tests.conftest import LIVE_SETTINGS

ROOT = Path(__file__).resolve().parents[1]
KEY_ENV = signing.KEY_ENV
MEASURED_ON = "2026-10-18"


def result(**changes: Any) -> dict[str, Any]:
    """A synthetic scoreboard entry: the fields signing reads (format, scope.method,
    scope.measured_on), and others for a byte to change in, non-ASCII text included."""
    entry: dict[str, Any] = {
        "format": "mendari.scoreboard/1",
        "pack": {"id": "mendari/patient-inbox-router", "version": "0.4.2", "digest": "sha256:0"},
        "set": {"kind": "public", "name": "test", "sha256": "0" * 64, "n": 600, "distinct": 81},
        "arm": "deterministic",
        "configuration": {"router": "naive_bayes", "safety_screen": "rules"},
        "scope": {
            "method": {"version": method.METHOD, "sha256": method.METHOD_SHA256},
            "measured_on": MEASURED_ON,
            "disclaimer": "engineering evaluation, not clinical validation",
            "disclosure": "no model provider",
        },
        "figures": {
            "accuracy": {"k": 512, "n": 600, "low": 0.8209, "high": 0.8815},
            "brier": {"value": 0.2381, "low": 0.2154, "high": 0.2617},
            "ece": {"value": 0.0412},
        },
        "standing": "public set: regression check, not a ranking",
        "note": "Synthétique, für Tests",
    }
    entry.update(changes)
    return entry


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def seed_of(key: Ed25519PrivateKey) -> str:
    """The key as $MENDARI_GRADING_SIGNING_KEY holds it: its 32-byte seed in base64."""
    return b64(
        key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    )


def keys_file(
    *keys: Ed25519PrivateKey, valid_from: str = "2026-10-01", valid_until: str | None = None
) -> dict[str, Any]:
    return {
        "format": signing.KEYS_FORMAT,
        "keys": [
            {
                "key_id": key_id(key.public_key()),
                "public_key": public_text(key.public_key()),
                "valid_from": valid_from,
                "valid_until": valid_until,
            }
            for key in keys
        ],
    }


def listed(key: Ed25519PrivateKey, **dates: Any) -> dict[str, signing.GradingKey]:
    return signing.load_keys(keys_file(key, **dates))


def written(path: Path, value: Any) -> Path:
    path.write_text(signing.dump(value) if isinstance(value, dict) else value, encoding="utf-8")
    return path


def mutated(text: str, position: int) -> str:
    """`text` with the character at `position` changed (its lowest bit flipped: a digit stays a
    digit of another value, a letter another letter, punctuation breaks the JSON)."""
    return text[:position] + chr(ord(text[position]) ^ 1) + text[position + 1 :]


@pytest.fixture
def key_holder(monkeypatch: pytest.MonkeyPatch) -> Ed25519PrivateKey:
    """The key holder's own machine: not CI, whatever runs these tests, and a grading key made
    for this test."""
    for name in signing.CI_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    key = Ed25519PrivateKey.generate()
    monkeypatch.setenv(KEY_ENV, seed_of(key))
    return key


def err_lines(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return [line for line in capsys.readouterr().err.splitlines() if line.strip()]


# -- what is signed, and what verifies ---------------------------------------------------------


def test_a_signed_result_verifies_and_any_changed_byte_fails() -> None:
    """The prompt's tampering test: every character of the signed body, and of the signature's
    value, changed in turn, fails; a result with no change verifies, and signing is
    deterministic (Ed25519)."""
    key = Ed25519PrivateKey.generate()
    keys = listed(key)
    signed = signing.sign(result(), key)
    assert set(signed) == {*result(), "signature"}
    assert signed["signature"]["key_id"] == key_id(key.public_key())
    assert signing.body(signed) == json.loads(canonical(result()))
    # The signature leaves the body as it was, and its hash.
    assert signing.body_sha256(signed) == hashlib.sha256(canonical(result()).encode()).hexdigest()
    assert signing.body_sha256(signed) == signing.body_sha256(result())
    assert signing.signed_message(signed) == signing.PREFIX + canonical(result()).encode()
    assert signing.problem(signed, keys) is None
    assert signing.sign(result(), key) == signed
    assert signing.read(signing.dump(signed)) == signed

    body = canonical(signing.body(signed))
    for position in range(len(body)):
        try:
            changed = signing.read(mutated(body, position))
        except MendariError:
            continue  # no longer JSON at all
        if isinstance(changed, dict):
            tampered = {**changed, "signature": signed["signature"]}
            assert signing.problem(tampered, keys) is not None, body[: position + 1]
    value = signed["signature"]["value"]
    for position in range(len(value)):
        signature = {**signed["signature"], "value": mutated(value, position)}
        assert signing.problem({**signed, "signature": signature}, keys) is not None, position
    for field, changed in (("arm", "llm"), ("note", "Synthetique, fur Tests")):
        assert signing.problem({**signed, field: changed}, keys) == (
            "its signature does not hold: it changed after it was signed"
        )
    figures = {**signed["figures"], "accuracy": {**signed["figures"]["accuracy"], "k": 513}}
    assert "does not hold" in str(signing.problem({**signed, "figures": figures}, keys))
    for extra in ({"ranked": True}, {"figures": {**signed["figures"], "rank": 1}}):
        assert "does not hold" in str(signing.problem({**signed, **extra}, keys))
    assert signing.problem(result(), keys) == signing.UNSIGNED


def test_a_result_signed_by_a_publisher_key_is_refused(
    tmp_path: Path, key_holder: Ed25519PrivateKey, capsys: pytest.CaptureFixture[str]
) -> None:
    """A publisher's key (`hub/archive.py`) never signs a result, even listed as a grading key:
    the verifier refuses every key id this machine pinned (each namespace's, and the registry's
    own as `@registry`), and `sign` refuses to sign with one."""
    private_pem, public_pem = archive.generate_key()
    publisher = archive.load_private_key(private_pem)
    publisher_id = archive.key_id(archive.load_public_key(public_pem))
    assert publisher_id == key_id(publisher.public_key())  # one way of naming keys
    signed = signing.sign(result(), publisher)
    keys = listed(publisher)  # as if someone had listed it as a grading key
    assert signing.problem(signed, keys, publisher_ids={publisher_id}) == (
        f"it is signed with {publisher_id}, a publisher's or a registry's key: never a result's"
    )
    assert signing.problem(signed, listed(key_holder)) == (
        f"it is signed with {publisher_id}, which is not a grading key in the keys given"
    )

    # On the command line: the pins `mendari pull` keeps.
    registry_key = Ed25519PrivateKey.generate()
    pins = {
        "https://registry.example": {
            "site-a": [publisher_id],
            "@registry": key_id(registry_key.public_key()),
        }
    }
    config_dir().mkdir(parents=True)
    (config_dir() / "trusted_keys.json").write_text(json.dumps(pins), encoding="utf-8")
    assert signing.read_pins(config_dir() / "trusted_keys.json") == {
        publisher_id,
        key_id(registry_key.public_key()),
    }
    by_publisher = written(tmp_path / "publisher.json", signed)
    by_registry = written(tmp_path / "registry.json", signing.sign(result(), registry_key))
    listing = written(tmp_path / "keys.json", keys_file(publisher, registry_key))
    assert main(["scoreboard", "verify", str(by_publisher), "--keys", str(listing)]) == 1
    assert main(["scoreboard", "verify", str(by_registry), "--keys", str(listing)]) == 1
    lines = err_lines(capsys)
    assert len(lines) == 2 and all("a publisher's or a registry's key" in line for line in lines)

    # The key holder's key pinned as a publisher's: `sign` refuses it.
    pins["https://registry.example"]["site-b"] = [key_id(key_holder.public_key())]
    (config_dir() / "trusted_keys.json").write_text(json.dumps(pins), encoding="utf-8")
    unsigned = written(tmp_path / "unsigned.json", result())
    before = unsigned.read_bytes()
    assert main(["scoreboard", "sign", str(unsigned)]) == 1
    assert "pinned as a publisher's or a registry's key" in err_lines(capsys)[0]
    assert unsigned.read_bytes() == before


def test_a_checkpoint_statement_or_archive_signature_never_verifies_as_a_result(
    inbox_pack: Pack,
) -> None:
    """One Ed25519 key signs a checkpoint statement, a pack archive, a recall and a result:
    none of the others' signatures verifies as the result's (they are over JSON with no prefix),
    and the result's verifies as none of theirs."""
    key = Ed25519PrivateKey.generate()
    keys, signer = listed(key), key_id(key.public_key())
    signed = signing.sign(result(), key)

    def as_result(value: str) -> dict[str, Any]:
        return {**result(), "signature": {"key_id": signer, "value": value}}

    held = "its signature does not hold: it changed after it was signed"
    # The bytes every other Mendari signature is over: canonical JSON with no prefix.
    plain = checkpoint._signed(signing.body(result()))
    assert plain == canonical(result()).encode()
    assert signing.problem(as_result(b64(key.sign(plain))), keys) == held

    statement = checkpoint.sign("a" * 64, date(2026, 10, 18), key)
    assert signing.problem(statement, keys) == (
        "not a scoreboard result (mendari.scoreboard/1 or mendari.hidden_scoreboard/1)"
    )
    assert signing.problem(as_result(statement["signature"]), keys) == held
    transplanted = {**statement, "signature": signed["signature"]["value"]}
    assert checkpoint.problem(transplanted, key.public_key()) == "its signature does not hold"

    built = archive.build(inbox_pack, key)
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(built))) as tar:
        member = tar.extractfile(archive.SIGNATURE)
        assert member is not None
        archive_signature = json.loads(member.read())
    assert archive.verify(built, {signer: key.public_key()}).key_id == signer
    assert signing.problem(as_result(archive_signature["signature"]), keys) == held

    record = yank.sign(
        pack="site-a/inbox",
        version="1.0.0",
        digest=inbox_pack.digest,
        archive_sha256="0" * 64,
        reason="a recall for the test",
        severity="high",
        signer="publisher",
        key=key,
        key_id=signer,
    )
    assert yank.problem(record, key.public_key(), signer) is None
    assert signing.problem(as_result(record["signature"]), keys) == held
    recalled = {**record, "signature": signed["signature"]["value"]}
    assert yank.problem(recalled, key.public_key(), signer) == "its signature does not hold"
    assert signing.problem(signed, keys) is None


def test_a_result_under_another_methods_hash_or_outside_its_keys_dates_is_refused() -> None:
    """The verifier's other refusals, each named: another format, a method hash that is not its
    version's, a method this engine does not know, no day measured, a key unknown or not valid on
    the day measured, a signature that is not a key id and a canonical value; `sign` refuses all
    it can see before signing."""
    key = Ed25519PrivateKey.generate()
    keys = listed(key)
    signed = signing.sign(result(), key)

    def scoped(**changes: Any) -> dict[str, Any]:
        return {**signed, "scope": {**signed["scope"], **changes}}

    hashed = {"version": method.METHOD, "sha256": "0" * 64}
    unknown = {"version": "mendari.grading/0.9", "sha256": method.METHOD_SHA256}
    cases = {
        "other format": (
            {**signed, "format": "mendari.eval/2"},
            "not a scoreboard result (mendari.scoreboard/1 or mendari.hidden_scoreboard/1)",
        ),
        "another hash": (
            scoped(method=hashed),
            f"its method's SHA-256 is not that of {method.METHOD}",
        ),
        "unknown method": (
            scoped(method=unknown),
            "its scope names a method version this engine does not know",
        ),
        "no method": (
            {**signed, "scope": {"measured_on": MEASURED_ON}},
            "its scope names no method (scope.method: version and sha256)",
        ),
        "no day": (
            scoped(measured_on="18 October 2026"),
            "its scope names no day it was measured (scope.measured_on: YYYY-MM-DD)",
        ),
    }
    for name, (value, expected) in cases.items():
        assert signing.problem(value, keys) == expected, name
        unsigned = signing.body(value)
        with pytest.raises(MendariError, match=r"^not signed: "):
            signing.sign(unsigned, key)
    assert signing.problem(signed, {}) == (
        f"it is signed with {key_id(key.public_key())}, which is not a grading key in the keys "
        "given"
    )
    other = Ed25519PrivateKey.generate()
    assert "does not hold" in str(
        signing.problem(
            {**signed, "signature": {**signed["signature"], "key_id": key_id(other.public_key())}},
            {**keys, **listed(other)},
        )
    )
    # Valid dates: from valid_from to valid_until, both included.
    signer = key_id(key.public_key())
    for dates, verifies in (
        ({"valid_from": MEASURED_ON}, True),
        ({"valid_from": "2026-10-01", "valid_until": MEASURED_ON}, True),
        ({"valid_from": "2026-10-19"}, False),
        ({"valid_from": "2026-10-01", "valid_until": "2026-10-17"}, False),
    ):
        found = signing.problem(signed, listed(key, **dates))
        assert (found is None) == verifies, dates
    assert signing.problem(signed, listed(key, valid_from="2026-10-19")) == (
        f"it is signed with {signer}, not valid on {MEASURED_ON} (valid from 2026-10-19 to no "
        "end date)"
    )
    # A key given on the command line is trusted on any day.
    assert signing.problem(signed, signing.given_key(public_text(key.public_key()))) is None
    # A signature is a key id and a value, only, in one spelling.
    value = signed["signature"]["value"]
    for signature, expected in (
        ({**signed["signature"], "at": "2026-10-18"}, "its signature is not a key_id and a value"),
        ({"key_id": signer, "value": 1}, "its signature is not a key_id and a value"),
        ({"key_id": "ed25519:XYZ", "value": value}, "does not name a key id"),
        ({"key_id": signer, "value": value.rstrip("=")}, "is not 64 bytes in base64"),
        ({"key_id": signer, "value": b64(b"\x00" * 63)}, "is not 64 bytes in base64"),
        (None, "its signature is not a key_id and a value"),
    ):
        assert expected in str(signing.problem({**signed, "signature": signature}, keys))
    # A value whose last character differs only in bits base64 leaves unused: another spelling.
    last = value.rstrip("=")[-1]
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    twin = alphabet[alphabet.index(last) ^ 1]
    respelled = value.rstrip("=")[:-1] + twin + "=="
    assert base64.b64decode(respelled) == base64.b64decode(value)
    respelled_signature = {"key_id": signer, "value": respelled}
    assert "is not 64 bytes in base64" in str(
        signing.problem({**signed, "signature": respelled_signature}, keys)
    )
    with pytest.raises(MendariError, match="signed already"):
        signing.sign(signed, key)


def test_the_keys_file_is_checked(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The committed keys file is `mendari.grading-keys/1` and holds public keys only (none until
    the owner adds one); a file that is anything else stops the verifier, naming the key."""
    committed = json.loads((ROOT / signing.KEYS_PATH).read_text(encoding="utf-8"))
    assert committed["format"] == signing.KEYS_FORMAT
    loaded = signing.load_keys(committed)
    fields = {"key_id", "public_key", "valid_from", "valid_until"}
    assert all(set(entry) == fields for entry in committed["keys"])
    assert sorted(loaded) == sorted(entry["key_id"] for entry in committed["keys"])

    key, other = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    good = keys_file(key)
    entry = good["keys"][0]
    broken = {
        "a keys file is": {**good, "format": "mendari.keys/0"},
        "holds its format and a list of keys, only": {**good, "note": "x"},
        "valid_from, valid_until, only": {
            **good,
            "keys": [{**entry, "private_key": seed_of(key)}],
        },
        "its key_id is not its public key's": {
            **good,
            "keys": [{**entry, "key_id": key_id(other.public_key())}],
        },
        "its public key is not 32 bytes in base64": {
            **good,
            "keys": [{**entry, "public_key": b64(b"\x01" * 31)}],
        },
        "valid_from is a day, valid_until a day or null": {
            **good,
            "keys": [{**entry, "valid_until": "soon"}],
        },
        "valid_until is before valid_from": {
            **good,
            "keys": [{**entry, "valid_from": "2026-10-18", "valid_until": "2026-10-17"}],
        },
        "its key_id is listed twice": {**good, "keys": [entry, entry]},
    }
    for expected, data in broken.items():
        with pytest.raises(MendariError, match=expected) as raised:
            signing.load_keys(data)
        assert seed_of(key) not in str(raised.value)  # a private key listed is never shown
    # On the command line: one line naming the file and the key.
    signed = written(tmp_path / "result.json", signing.sign(result(), key))
    listing = written(tmp_path / "keys.json", broken["its key_id is not its public key's"])
    assert main(["scoreboard", "verify", str(signed), "--keys", str(listing)]) == 1
    assert err_lines(capsys) == [
        f"error: --keys {listing}: key 1 of the keys file: its key_id is not its public key's"
    ]
    listing.write_text("{", encoding="utf-8")
    assert main(["scoreboard", "verify", str(signed), "--keys", str(listing)]) == 1
    (line,) = err_lines(capsys)
    assert line.startswith(f"error: --keys {listing}: not valid JSON")


def test_a_keys_file_is_read_as_strictly_as_a_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A field twice in the keys file (a key's end date written beside the old one, which plain
    JSON reads as its last value), NaN or an infinity stops the verifier, naming the file: never
    a key trusted on days the file seems to end it."""
    key = Ed25519PrivateKey.generate()
    signed = written(tmp_path / "result.json", signing.sign(result(), key))  # measured 10-18
    ended = signing.dump(keys_file(key, valid_until="2026-10-05"))
    listing = written(tmp_path / "ended.json", ended)
    assert main(["scoreboard", "verify", str(signed), "--keys", str(listing)]) == 1
    assert "not valid on 2026-10-18 (valid from 2026-10-01 to 2026-10-05)" in err_lines(capsys)[0]
    ending = '"valid_until": "2026-10-05"'
    reopened = ended.replace(ending, f'{ending},\n      "valid_until": null')
    # Plain JSON reads it as a key with no end date, which would verify the result.
    assert json.loads(reopened)["keys"][0]["valid_until"] is None
    twice = "a key appears twice in one object"
    for text, expected in (
        (reopened, twice),
        (ended.replace('"format"', '"keys": [],\n  "format"'), twice),
        (ended.replace(ending, '"valid_until": NaN'), "it holds NaN or an infinity"),
    ):
        assert text != ended
        listing = written(tmp_path / "keys.json", text)
        assert main(["scoreboard", "verify", str(signed), "--keys", str(listing)]) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == f"error: --keys {listing}: its JSON is not canonical: {expected}\n"
    listing.write_bytes(b"\xff" + ended.encode("ascii"))
    missing = tmp_path / "missing.json"
    for given, line in (
        (listing, f"error: --keys {listing}: not UTF-8 text; save it as UTF-8"),
        (missing, f"error: --keys {missing}: No such file or directory; check the path"),
    ):
        assert main(["scoreboard", "verify", str(signed), "--keys", str(given)]) == 1
        assert err_lines(capsys) == [line]


def test_each_result_is_its_own_line_one_that_cannot_be_read_included(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`verify` checks every file it is given: one that cannot be read (not JSON, a key twice,
    NaN, a whole number too long to read, not UTF-8, missing, a folder) is one `error:` line
    naming it and why, and every other result still gets its line. With --debug the first such
    error is let through."""
    key = Ed25519PrivateKey.generate()
    keys = written(tmp_path / "keys.json", keys_file(key))
    first = written(tmp_path / "first.json", signing.sign(result(), key))
    last = written(tmp_path / "last.json", signing.sign(result(arm="llm"), key))
    text = signing.dump(signing.sign(result(arm="classifier"), key))
    assert '"k": 512' in text
    # Each file's content (bytes as they are; None: no file; "folder": a folder) and why.
    broken: dict[str, tuple[str | bytes | None, str]] = {
        "notjson.json": (text[:-3], "not valid JSON"),
        "twice.json": (
            text.replace('"k": 512', '"k": 512, "k": 600'),
            "its JSON is not canonical: a key appears twice in one object",
        ),
        "nan.json": (
            text.replace('"k": 512', '"k": NaN'),
            "its JSON is not canonical: it holds NaN or an infinity",
        ),
        "huge.json": (
            text.replace('"k": 512', '"k": ' + "9" * 5000),
            "its JSON holds a whole number too long to read",
        ),
        "latin1.json": (b"\xff" + text.encode("ascii"), "not UTF-8 text; save it as UTF-8"),
        "missing.json": (None, "No such file or directory; check the path"),
        "folder.json": ("folder", "Is a directory; check the path"),
    }
    paths = []
    for name, (content, _) in broken.items():
        made = tmp_path / name
        if isinstance(content, bytes):
            made.write_bytes(content)
        elif content == "folder":
            made.mkdir()
        elif content is not None:
            made.write_text(content, encoding="utf-8")
        paths.append(str(made))
    with pytest.raises(MendariError, match="too long to read"):
        signing.read("[" + "9" * 5000 + "]")
    argv = ["scoreboard", "verify", str(first), *paths, str(last), "--keys", str(keys)]
    assert main(argv) == 1
    captured = capsys.readouterr()
    signer = key_id(key.public_key())
    assert captured.out.splitlines() == [
        f"verified: {path}, signed with {signer} (valid from 2026-10-01 to no end date), under "
        f"{method.METHOD}"
        for path in (first, last)
    ]
    lines = captured.err.splitlines()
    assert len(lines) == len(broken)
    for line, shown, (_, why) in zip(lines, paths, broken.values(), strict=True):
        assert line.startswith(f"error: {shown}: {why}"), line
    assert "unexpected" not in captured.err
    # --debug lets the first error through, as for any command (LP-15).
    with pytest.raises(MendariError, match="not valid JSON"):
        main([*argv, "--debug"])
    monkeypatch.setenv("MENDARI_DEBUG", "1")
    with pytest.raises(MendariError, match="not valid JSON"):
        main(argv)


# -- the key -----------------------------------------------------------------------------------


@pytest.mark.parametrize("name", [*signing.OTHER_KEYS, *signing.OTHER_RINGS])
def test_the_key_must_differ_from_every_other_key(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each other key Mendari uses, as the grading key's value (a ring's as its second entry):
    the key is refused, naming the variable, never the value; distinct, it signs."""
    assert {*signing.OTHER_KEYS, *signing.OTHER_RINGS} == {
        "MENDARI_CHECKPOINT_KEY",
        "MENDARI_REGISTRY_SIGNING_KEY",
        "MENDARI_PHI_KEY",
        "MENDARI_PHI_READ_KEYS",
        "MENDARI_INPUT_HASH_KEY",
        "MENDARI_EVALSET_KEY",
        "MENDARI_EVALSET_INDEX_KEY",
        "MENDARI_EVALSET_READ_KEYS",
    }
    for other in (*signing.OTHER_KEYS, *signing.OTHER_RINGS):
        monkeypatch.delenv(other, raising=False)
    seed = b64(secrets.token_bytes(32))
    monkeypatch.setenv(KEY_ENV, seed)
    if name in signing.OTHER_RINGS:
        monkeypatch.setenv(name, f"{b64(secrets.token_bytes(32))},{seed}")
        named = f"{name} entry 2"
    else:
        monkeypatch.setenv(name, f" {seed}\n")  # the same key, spaced otherwise
        named = name
    with pytest.raises(MendariError) as raised:
        signing.grading_key()
    assert str(raised.value) == f"{KEY_ENV} must differ from {named}: a grading key is its own"
    assert seed not in str(raised.value)
    monkeypatch.setenv(name, b64(secrets.token_bytes(32)))
    found = signing.grading_key()
    assert found is not None and seed_of(found) == seed


def test_no_evaluation_set_key_may_be_the_grading_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other way round: the evaluation-set keys' own check (`core/evalset_keys.py`) refuses
    one equal to the grading key."""
    seed = b64(secrets.token_bytes(32))
    monkeypatch.setenv(KEY_ENV, seed)
    monkeypatch.setenv("MENDARI_EVALSET_INDEX_KEY", b64(secrets.token_bytes(32)))
    monkeypatch.setenv("MENDARI_EVALSET_KEY", seed)
    with pytest.raises(MendariError) as raised:
        evalset_keys()
    assert str(raised.value) == f"MENDARI_EVALSET_KEY must differ from {KEY_ENV}"
    monkeypatch.setenv("MENDARI_EVALSET_KEY", b64(secrets.token_bytes(32)))
    assert evalset_keys() is not None


def test_unset_means_off_and_a_malformed_key_stops_naming_no_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in signing.CI_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    path = written(tmp_path / "result.json", result())
    before = path.read_bytes()
    monkeypatch.delenv(KEY_ENV, raising=False)
    assert signing.grading_key() is None
    assert main(["scoreboard", "sign", str(path)]) == 1
    assert err_lines(capsys) == [
        f"error: {KEY_ENV} is not set: signing is the key holder's, on their own machine "
        "(docs/grading/verifying.md)"
    ]
    monkeypatch.setenv(KEY_ENV, "  ")
    assert signing.grading_key() is None  # blank is unset
    for bad in (
        b64(secrets.token_bytes(31)),
        b64(secrets.token_bytes(33)),
        "%" + secrets.token_hex(22),
        seed_of(Ed25519PrivateKey.generate()).rstrip("="),
    ):
        monkeypatch.setenv(KEY_ENV, bad)
        with pytest.raises(MendariError) as raised:
            signing.grading_key()
        assert KEY_ENV in str(raised.value) and bad not in str(raised.value)
        assert main(["scoreboard", "sign", str(path)]) == 1
        (line,) = err_lines(capsys)
        assert line.startswith(f"error: {KEY_ENV} must be 32 random bytes in base64")
        assert bad not in line
        assert path.read_bytes() == before


def test_nan_or_a_non_canonical_body_is_never_signed(
    tmp_path: Path, key_holder: Ed25519PrivateKey, capsys: pytest.CaptureFixture[str]
) -> None:
    """NaN, an infinity, keys that collide once they are text, or a value JSON has no type for
    is never signed; a file that holds a key twice, NaN or an infinity is refused by `sign` and
    `verify` alike (never read as one of its meanings), and left as it was."""
    for value in (math.nan, math.inf, -math.inf, {1: "a", "1": "b"}, {"set": {1, 2}}):
        with pytest.raises(MendariError, match=r"^not signed: its body is not canonical JSON"):
            signing.sign(result(figures=value), key_holder)
        assert signing.problem(result(figures=value), {}) == (
            "its body is not canonical JSON: it holds NaN or an infinity, or a value that does "
            "not read back as the same JSON"
        )
    text = signing.dump(result(figures={"brier": 0.25}))
    files = {
        "twice": text[: text.rindex("}")].rstrip() + ',\n  "note": "again"\n}\n',
        "nan": text.replace("0.25", "NaN"),
        "infinity": text.replace("0.25", "-Infinity"),
    }
    keys = written(tmp_path / "keys.json", keys_file(key_holder))
    for name, content in files.items():
        path = written(tmp_path / f"{name}.json", content)
        with pytest.raises(MendariError, match="its JSON is not canonical"):
            signing.read(content)
        assert main(["scoreboard", "sign", str(path)]) == 1
        assert main(["scoreboard", "verify", str(path), "--keys", str(keys)]) == 1
        lines = err_lines(capsys)
        assert len(lines) == 2 and all("its JSON is not canonical" in line for line in lines)
        assert path.read_text(encoding="utf-8") == content
    # A number too large for a float reads as an infinity, which no canonical body holds.
    signed = signing.sign(result(figures={"brier": 0.25}), key_holder)
    huge = written(tmp_path / "huge.json", signing.dump(signed).replace("0.25", "1e400"))
    assert main(["scoreboard", "verify", str(huge), "--keys", str(keys)]) == 1
    assert "its body is not canonical JSON" in err_lines(capsys)[0]


def test_verifying_needs_no_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no secret in the environment, and a grading key that would stop `sign` (so it is
    never read), `verify` checks a result against the keys file or one public key; in CI too."""
    key = Ed25519PrivateKey.generate()
    path = written(tmp_path / "result.json", signing.sign(result(), key))
    keys = written(tmp_path / "keys.json", keys_file(key))
    for name in [*signing.OTHER_KEYS, *signing.OTHER_RINGS]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(KEY_ENV, "%" + secrets.token_hex(22))
    monkeypatch.setenv("CI", "true")
    assert main(["scoreboard", "verify", str(path), "--keys", str(keys)]) == 0
    signer = key_id(key.public_key())
    assert capsys.readouterr().out == (
        f"verified: {path}, signed with {signer} (valid from 2026-10-01 to no end date), under "
        f"{method.METHOD}\n"
    )
    given = public_text(key.public_key())
    assert main(["scoreboard", "verify", str(path), "--public-key", given]) == 0
    assert "(valid on any day)" in capsys.readouterr().out
    assert main(["scoreboard", "verify", str(path), "--public-key", "x" + given[1:-2]]) == 1
    assert err_lines(capsys) == ["error: --public-key is a public key: 32 bytes in base64"]
    # The default keys file is the repository's, read from its root: no key in it yet.
    monkeypatch.chdir(ROOT)
    assert main(["scoreboard", "verify", str(path)]) == 1
    assert err_lines(capsys) == [
        f"error: {path}: it is signed with {signer}, which is not a grading key in the keys given"
    ]
    monkeypatch.chdir(tmp_path)
    assert main(["scoreboard", "verify", str(path)]) == 1
    assert err_lines(capsys) == [
        "error: no keys file at scoreboard/keys.json: run this from the repository's root, or "
        "give --keys or --public-key"
    ]


def test_signing_refuses_to_run_in_ci(
    tmp_path: Path,
    key_holder: Ed25519PrivateKey,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """In CI (`CI` or `GITHUB_ACTIONS` set) `sign` refuses before it reads the key, whatever the
    job holds, and the file is left as it was."""
    path = written(tmp_path / "result.json", result())
    before = path.read_bytes()
    for name in signing.CI_SETTINGS:
        for setting in signing.CI_SETTINGS:
            monkeypatch.delenv(setting, raising=False)
        monkeypatch.setenv(name, "true")
        for held in (seed_of(key_holder), "%" + secrets.token_hex(22)):  # a malformed one too
            monkeypatch.setenv(KEY_ENV, held)
            with pytest.raises(MendariError, match=f"^{name} is set"):
                signing.refuse_in_ci()
            assert main(["scoreboard", "sign", str(path)]) == 1
            assert err_lines(capsys) == [
                f"error: {name} is set: scoreboard results are never signed in CI; the key "
                "holder signs them on their own machine"
            ]
            assert path.read_bytes() == before


def test_the_command_line_signs_in_place_and_verifies(
    tmp_path: Path, key_holder: Ed25519PrivateKey, capsys: pytest.CaptureFixture[str]
) -> None:
    """`sign` checks every file before it writes any, replaces each whole, and prints the key's
    id and public half, never the key; `verify` names each result that fails, on its own line."""
    first = written(tmp_path / "first.json", result())
    second = written(tmp_path / "second.json", result(arm="llm"))
    assert main(["scoreboard", "sign", str(first), str(second)]) == 0
    out = capsys.readouterr().out
    signer, public = key_id(key_holder.public_key()), public_text(key_holder.public_key())
    assert out.splitlines() == [
        f"signed {first} (body {signing.body_sha256(result())})",
        f"signed {second} (body {signing.body_sha256(result(arm='llm'))})",
        f"with {signer}, public key {public}: list it once in scoreboard/keys.json with the "
        "dates it is valid for",
        "then: uv run python scripts/scoreboard_page.py (the page), and commit both with the keys "
        "file",
    ]
    assert seed_of(key_holder) not in out
    assert first.read_text(encoding="utf-8") == signing.dump(signing.sign(result(), key_holder))
    assert sorted(path.name for path in tmp_path.iterdir()) == ["first.json", "second.json"]
    keys = written(tmp_path / "keys.json", keys_file(key_holder))
    assert main(["scoreboard", "verify", str(first), str(second), "--keys", str(keys)]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 2

    # Signed once: a second `sign` refuses, and writes none of the files it was given.
    third = written(tmp_path / "third.json", result(arm="classifier"))
    before = third.read_bytes()
    assert main(["scoreboard", "sign", str(third), str(first)]) == 1
    assert err_lines(capsys) == [f"error: {first}: it is signed already: a result is signed once"]
    assert third.read_bytes() == before
    link = tmp_path / "link.json"
    link.symlink_to(third)
    assert main(["scoreboard", "sign", str(link)]) == 1
    assert "is a symbolic link" in err_lines(capsys)[0]

    # One changed figure: that result fails, the other still verifies.
    tampered = json.loads(second.read_text(encoding="utf-8"))
    tampered["figures"]["accuracy"]["k"] = 600
    written(second, tampered)
    assert main(["scoreboard", "verify", str(first), str(second), "--keys", str(keys)]) == 1
    captured = capsys.readouterr()
    assert captured.out.startswith(f"verified: {first}, signed with {signer}")
    assert captured.err == (
        f"error: {second}: its signature does not hold: it changed after it was signed\n"
    )
    assert main(["scoreboard", "verify", str(third), "--keys", str(keys)]) == 1
    assert err_lines(capsys) == [f"error: {third}: unsigned"]


def test_no_server_workflow_or_deployment_names_the_key() -> None:
    """The key is the key holder's, offline: only `mendari scoreboard sign` reads it (the
    evaluation-set keys' check compares against it, and holds none), no workflow, deployment
    template or Vercel setting names it, and the test suite removes it before anything runs."""
    package = ROOT / "src" / "mendari"
    named = sorted(
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if KEY_ENV in path.read_text(encoding="utf-8")
    )
    assert named == ["cli.py", "core/evalset_keys.py", "evals/signing.py"]
    readers = sorted(
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if "grading_key()" in path.read_text(encoding="utf-8")
    )
    assert readers == ["cli.py", "evals/signing.py"]
    cli = (package / "cli.py").read_text(encoding="utf-8")
    assert cli.count("signing.grading_key()") == 1
    assert cli.index("signing.grading_key()") > cli.index("def cmd_scoreboard_sign(")
    assert cli.index("signing.grading_key()") < cli.index("def cmd_scoreboard_verify(")
    places = [
        *(ROOT / ".github").rglob("*"),
        *(ROOT / "deploy").rglob("*"),
        *(ROOT / "packs").rglob("*.yaml"),
        ROOT / "vercel.json",
        ROOT / "app.py",
        ROOT / "Dockerfile",
        ROOT / "docker-compose.yml",
    ]
    for path in places:
        if path.is_file():
            assert KEY_ENV not in path.read_text(encoding="utf-8", errors="replace"), path
    assert KEY_ENV in LIVE_SETTINGS and KEY_ENV not in os.environ
