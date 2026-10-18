# SPDX-License-Identifier: Apache-2.0
"""Signed scoreboard results and their public verifier (LP-17e, ADR-0013; the grading method's
section 11). Off without the key: nothing is signed unless its holder runs
`mendari scoreboard sign`, and checking a result needs no secret.

A result is a scoreboard entry (`mendari.scoreboard/1`, LP-17d) or a hidden set's scoreboard
answer (`mendari.hidden_scoreboard/1`, LP-17k). A signed result carries one more field,

    "signature": {"key_id": "ed25519:<16 hex>", "value": "<base64>"}

an Ed25519 signature over `mendari-grading-result-v1`, a zero byte, then the result's canonical
JSON without its signature (`ledger.canonical`: sorted keys, no spaces, ASCII, no NaN). Adding
it changes nothing else: the result's body, and the SHA-256 that names it in the store, stay the
same. Every other signature Mendari makes (a checkpoint statement, a pack archive, a recall) is
over JSON with no prefix, so none of them verifies as a result.

The verifier reads two fields of the result's scope: `scope.method` (`version` and `sha256`),
which must be a method version this engine knows with that version's hash, and
`scope.measured_on` (YYYY-MM-DD), the day the result was measured, which must fall within the
signing key's valid dates.

The key is `MENDARI_GRADING_SIGNING_KEY`: 32 random bytes in base64 (`openssl rand -base64 32`),
the Ed25519 private key's seed. The owner makes it and holds it offline. Only
`mendari scoreboard sign` reads it, and that refuses to run in CI; no server reads it. It must
differ from every other key Mendari uses: a malformed or reused one stops the command, with a
message that names variables, never a value. Its public half is published in
`scoreboard/keys.json` (`mendari.grading-keys/1`), with the dates it is valid for.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from mendari.core import checkpoint, evalset_keys
from mendari.core.checkpoint import key_id, load_public
from mendari.errors import MendariError
from mendari.evals import method
from mendari.evals.ledger import canonical
from mendari.hub import yank

KEY_ENV = "MENDARI_GRADING_SIGNING_KEY"
PREFIX = b"mendari-grading-result-v1\x00"
FORMATS = ("mendari.scoreboard/1", "mendari.hidden_scoreboard/1")
KEYS_FORMAT = "mendari.grading-keys/1"
KEYS_PATH = "scoreboard/keys.json"  # from the repository's root
UNSIGNED = "unsigned"  # what `problem` says of a result with no signature
# The method versions this engine knows, each with the SHA-256 of its document.
METHODS: Mapping[str, str] = {method.METHOD: method.METHOD_SHA256}
# Every other key Mendari uses, by its variable; a ring's entries by their position.
OTHER_KEYS = (
    checkpoint.KEY_ENV,
    yank.KEY_ENV,
    "MENDARI_PHI_KEY",
    "MENDARI_INPUT_HASH_KEY",
    evalset_keys.KEY_ENV,
    evalset_keys.INDEX_ENV,
)
OTHER_RINGS = ("MENDARI_PHI_READ_KEYS", evalset_keys.READ_ENV)
CI_SETTINGS = ("CI", "GITHUB_ACTIONS")
_KEY_ID = re.compile(r"ed25519:[0-9a-f]{16}")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_KEY_FIELDS = {"key_id", "public_key", "valid_from", "valid_until"}
_NOT_CANONICAL = (
    "its body is not canonical JSON: it holds NaN or an infinity, or a value that does not read "
    "back as the same JSON"
)


@dataclass(frozen=True)
class GradingKey:
    """A grading key's public half, and the days it signs results measured on (None: no bound;
    a key given on the command line has none)."""

    key_id: str
    public: Ed25519PublicKey
    valid_from: date | None = None
    valid_until: date | None = None

    def valid_on(self, day: date) -> bool:
        if self.valid_from is not None and day < self.valid_from:
            return False
        return self.valid_until is None or day <= self.valid_until

    @property
    def dates(self) -> str:
        if self.valid_from is None:
            return "valid on any day"
        end = self.valid_until.isoformat() if self.valid_until else "no end date"
        return f"valid from {self.valid_from.isoformat()} to {end}"


# -- the key ------------------------------------------------------------------------------------


def grading_key() -> Ed25519PrivateKey | None:
    """The key holder's signing key ($MENDARI_GRADING_SIGNING_KEY), or None when it is unset.
    A `MendariError` when it is not 32 bytes in base64 or equals another key Mendari uses."""
    raw = os.environ.get(KEY_ENV, "").strip()
    if not raw:
        return None
    seed = _decoded(raw)
    if len(seed) != 32:
        raise MendariError(
            f"{KEY_ENV} must be 32 random bytes in base64 (openssl rand -base64 32), the "
            "Ed25519 key that signs scoreboard results"
        )
    for name, other in _other_keys():
        if hmac.compare_digest(seed, other):
            raise MendariError(f"{KEY_ENV} must differ from {name}: a grading key is its own")
    return Ed25519PrivateKey.from_private_bytes(seed)


def refuse_in_ci() -> None:
    """Signing is the key holder's, on their own machine: never in CI (`CI` or `GITHUB_ACTIONS`
    set), whatever the job's settings hold."""
    for name in CI_SETTINGS:
        if os.environ.get(name, "").strip():
            raise MendariError(
                f"{name} is set: scoreboard results are never signed in CI; the key holder signs "
                "them on their own machine"
            )


def _other_keys() -> list[tuple[str, bytes]]:
    """The other keys as they are set, each named by its variable (a ring's entry by its
    position). One that does not decode is left to its own check."""
    found = []
    for name in OTHER_KEYS:
        value = _decoded(os.environ.get(name, ""))
        if value:
            found.append((name, value))
    for name in OTHER_RINGS:
        for position, entry in enumerate(os.environ.get(name, "").split(","), start=1):
            value = _decoded(entry)
            if value:
                found.append((f"{name} entry {position}", value))
    return found


def _decoded(raw: str) -> bytes:
    try:
        return base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError):
        return b""


# -- what is signed -----------------------------------------------------------------------------


def body(result: Mapping[str, Any]) -> dict[str, Any]:
    """The result without its signature: what a signature covers and the store names."""
    return {name: value for name, value in result.items() if name != "signature"}


def body_bytes(result: Mapping[str, Any]) -> bytes:
    """The canonical JSON of the result's body (`ledger.canonical`). A `MendariError` when it has
    none that reads back as the same JSON: NaN or an infinity, keys that collide as text, a value
    JSON has no type for."""
    try:
        text = canonical(body(result))
        if canonical(json.loads(text)) != text:
            raise ValueError("not canonical")
    except (ValueError, TypeError, RecursionError):
        raise MendariError(_NOT_CANONICAL) from None
    return text.encode("ascii")


def body_sha256(result: Mapping[str, Any]) -> str:
    """The SHA-256 of the result's body, which a signature leaves as it is."""
    return hashlib.sha256(body_bytes(result)).hexdigest()


def signed_message(result: Mapping[str, Any]) -> bytes:
    """What a result's signature is over: the domain prefix, then its body's canonical JSON."""
    return PREFIX + body_bytes(result)


def read(text: str) -> Any:
    """A result's JSON as a file holds it, read strictly: a key twice in one object, NaN or an
    infinity is refused (a `MendariError`), never read as one of its meanings."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        found: dict[str, Any] = {}
        for name, value in items:
            if name in found:
                raise MendariError("its JSON is not canonical: a key appears twice in one object")
            found[name] = value
        return found

    def constant(_: str) -> Any:
        raise MendariError("its JSON is not canonical: it holds NaN or an infinity")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except json.JSONDecodeError as error:
        raise MendariError(
            f"not valid JSON (line {error.lineno}, column {error.colno}: {error.msg})"
        ) from None
    except RecursionError:
        raise MendariError("its JSON is nested too deeply") from None


def dump(result: Mapping[str, Any]) -> str:
    """A result as a file holds it: indented, sorted, ASCII, no NaN, one newline at the end. Its
    body reads back to the same canonical bytes."""
    return json.dumps(result, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n"


def _body_problem(result: Any) -> str | None:
    """Why `result` is not a result any key may sign or verify (None: it is one)."""
    if not isinstance(result, Mapping) or result.get("format") not in FORMATS:
        return f"not a scoreboard result ({' or '.join(FORMATS)})"
    try:
        body_bytes(result)
    except MendariError as error:
        return str(error)
    scope = result.get("scope")
    named = scope.get("method") if isinstance(scope, Mapping) else None
    if not isinstance(named, Mapping) or not all(
        isinstance(named.get(field), str) for field in ("version", "sha256")
    ):
        return "its scope names no method (scope.method: version and sha256)"
    expected = METHODS.get(named["version"])
    if expected is None:
        return "its scope names a method version this engine does not know"
    if named["sha256"] != expected:
        return f"its method's SHA-256 is not that of {named['version']}"
    if _measured_on(result) is None:
        return "its scope names no day it was measured (scope.measured_on: YYYY-MM-DD)"
    return None


def _measured_on(result: Mapping[str, Any]) -> date | None:
    scope = result.get("scope")
    return _day(scope.get("measured_on")) if isinstance(scope, Mapping) else None


def _day(value: Any) -> date | None:
    if not isinstance(value, str) or not _DAY.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def sign(result: Mapping[str, Any], key: Ed25519PrivateKey) -> dict[str, Any]:
    """The result with its signature. A `MendariError` for anything a verifier would refuse
    before the signature: another format, a body that is not canonical (NaN included), a method
    hash that is not its version's, no day measured; and for a result signed already (a stored
    result is signed once)."""
    if isinstance(result, Mapping) and "signature" in result:
        raise MendariError("it is signed already: a result is signed once")
    found = _body_problem(result)
    if found is not None:
        raise MendariError(f"not signed: {found}")
    message = signed_message(result)
    signature = {
        "key_id": key_id(key.public_key()),
        "value": base64.b64encode(key.sign(message)).decode("ascii"),
    }
    return {**json.loads(message[len(PREFIX) :]), "signature": signature}


# -- the public keys ----------------------------------------------------------------------------


def load_keys(data: Any) -> dict[str, GradingKey]:
    """The grading keys a keys file lists (`mendari.grading-keys/1`), by id. A `MendariError`
    for a file that is not one: each key is its id, its public key (32 bytes in base64, as
    `public_text` gives it), the first day it signs results measured on, and the last (or
    null), and nothing else; no id twice."""
    if not isinstance(data, Mapping) or data.get("format") != KEYS_FORMAT:
        raise MendariError(f"a keys file is {KEYS_FORMAT}")
    if set(data) != {"format", "keys"} or not isinstance(data["keys"], list):
        raise MendariError(f"a {KEYS_FORMAT} file holds its format and a list of keys, only")
    found: dict[str, GradingKey] = {}
    for position, entry in enumerate(data["keys"], start=1):
        where = f"key {position} of the keys file"
        if not isinstance(entry, Mapping) or set(entry) != _KEY_FIELDS:
            raise MendariError(f"{where} holds {', '.join(sorted(_KEY_FIELDS))}, only")
        try:
            public = load_public(str(entry["public_key"]))
        except MendariError:
            raise MendariError(f"{where}: its public key is not 32 bytes in base64") from None
        if entry["key_id"] != key_id(public):
            raise MendariError(f"{where}: its key_id is not its public key's")
        start = _day(entry["valid_from"])
        end = None if entry["valid_until"] is None else _day(entry["valid_until"])
        if start is None or (entry["valid_until"] is not None and end is None):
            raise MendariError(f"{where}: valid_from is a day, valid_until a day or null")
        if end is not None and end < start:
            raise MendariError(f"{where}: valid_until is before valid_from")
        if entry["key_id"] in found:
            raise MendariError(f"{where}: its key_id is listed twice")
        found[entry["key_id"]] = GradingKey(entry["key_id"], public, start, end)
    return found


def given_key(text: str) -> dict[str, GradingKey]:
    """One public key given as text (`--public-key`), trusted on any day."""
    try:
        public = load_public(text)
    except MendariError:
        raise MendariError("--public-key is a public key: 32 bytes in base64") from None
    return {key_id(public): GradingKey(key_id(public), public)}


def pinned_publisher_ids(pins: Any) -> frozenset[str]:
    """Every key id a trusted-keys file (`trusted_keys.json`, `hub/client.py`) pinned: each
    registry's namespace keys and its own key (`@registry`). Never a grading key."""
    found: set[str] = set()
    if not isinstance(pins, Mapping):
        return frozenset()
    for registry in pins.values():
        if not isinstance(registry, Mapping):
            continue
        for ids in registry.values():
            listed = [ids] if isinstance(ids, str) else ids if isinstance(ids, list) else []
            found |= {kid for kid in listed if isinstance(kid, str) and _KEY_ID.fullmatch(kid)}
    return frozenset(found)


def read_pins(path: Path) -> frozenset[str]:
    """The publisher key ids this machine pinned, from its trusted-keys file (none when it has
    none, or it cannot be read: the keys file is what decides)."""
    try:
        return pinned_publisher_ids(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, RecursionError):
        return frozenset()


# -- the verifier -------------------------------------------------------------------------------


def problem(
    result: Any, keys: Mapping[str, GradingKey], *, publisher_ids: Iterable[str] = ()
) -> str | None:
    """Why `result` is not a result signed by one of `keys` (None: it is one; `UNSIGNED`: it has
    no signature). It needs no secret. In order: another format; a body that is not canonical;
    a method hash that is not its version's; no day measured; no signature; a signature that is
    not a key id and a value; a publisher's or a registry's key (`publisher_ids`), even one the
    keys list; a key not in `keys`, or not valid on the day the result was measured; a changed
    byte."""
    found = _body_problem(result)
    if found is not None:
        return found
    if "signature" not in result:
        return UNSIGNED
    signature = result["signature"]
    if (
        not isinstance(signature, Mapping)
        or set(signature) != {"key_id", "value"}
        or not all(isinstance(value, str) for value in signature.values())
    ):
        return "its signature is not a key_id and a value, only"
    signer = signature["key_id"]
    if not _KEY_ID.fullmatch(signer):
        return "its signature does not name a key id (ed25519:<16 hex>)"
    if signer in frozenset(publisher_ids):
        return f"it is signed with {signer}, a publisher's or a registry's key: never a result's"
    key = keys.get(signer)
    if key is None:
        return f"it is signed with {signer}, which is not a grading key in the keys given"
    day = _measured_on(result)
    assert day is not None  # _body_problem checked it
    if not key.valid_on(day):
        return f"it is signed with {signer}, not valid on {day.isoformat()} ({key.dates})"
    value = _decoded(signature["value"])
    # One spelling only: base64 can say the same bytes in more than one way (unused low bits).
    if len(value) != 64 or base64.b64encode(value).decode("ascii") != signature["value"]:
        return "its signature's value is not 64 bytes in base64"
    try:
        key.public.verify(value, signed_message(result))
    except InvalidSignature:
        return "its signature does not hold: it changed after it was signed"
    return None
