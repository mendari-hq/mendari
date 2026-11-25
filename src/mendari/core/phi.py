# SPDX-License-Identifier: Apache-2.0
"""PHI mode: how a deployment keeps decision inputs ($MENDARI_PHI_MODE).

- `off` (default): as before. A pack's `audit.store_input` decides whether the input is kept
  inside the decision record.
- `hash`: inputs are never stored, whatever the pack says. Only their hash is kept (every record
  has it).
- `encrypt`: inputs are kept apart from the chained record, encrypted with AES-256-GCM under
  `$MENDARI_PHI_KEY` (32 bytes, base64) and bound to their decision id. Only roles that may
  read inputs get them back, and each read is written to the hash chain. Because the chained
  record never holds them, a retention policy can delete them (`mendari phi purge`) without
  breaking the chain.

Notes (a reviewer's or a rater's free text) follow inputs: sealed under the same key in
`encrypt`, kept in the clear in `off`, refused in `hash` (`core/audit.py`, "notes"). So does an
answer's evidence, which can quote the input (a lab value and its date): kept in the record only
when the input is, sealed beside a kept input in `encrypt`, dropped otherwise. Each kind of
sealed data has its own associated data (its kind, then its ids), so a ciphertext never opens as
another kind, nor for another decision or note.

Key rotation (LP-09). `$MENDARI_PHI_KEY` is the current key, the only one that seals.
`$MENDARI_PHI_READ_KEYS` lists other keys this deployment opens but never seals with: the previous
key while a rotation re-seals what it holds (`mendari phi rotate`), or the next key before it
becomes current. Each token names its key, `v2:<key id>:<base64>`, and opens only with the key it
names. A key id is public (`key_id`), never a fragment of the key.

The input hash in every record is keyed in `hash` and `encrypt`: an HMAC-SHA256 under
`$MENDARI_INPUT_HASH_KEY` (32 bytes, base64; its own key, so rotating the PHI key leaves hashes
alone, and `hash` mode, which has no PHI key, still has one). A plain SHA-256 of a short or
structured input can be found by guessing (an age, a sex and an order code take a few thousand
tries), and anyone who may read a decision sees its hash. Without the key (mode `off` only) the
hash is the plain `sha256:`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from mendari.errors import MendariError

PhiMode = Literal["off", "hash", "encrypt"]


@dataclass(frozen=True)
class PhiPolicy:
    mode: PhiMode = "off"
    key: bytes = field(default=b"", repr=False)
    # $MENDARI_INPUT_HASH_KEY; empty: the unkeyed `sha256:` (allowed in mode `off` only, which
    # `phi_policy` enforces).
    hash_key: bytes = field(default=b"", repr=False)
    # $MENDARI_PHI_READ_KEYS: keys that open, never seal (a rotation's previous or next key).
    read_keys: tuple[bytes, ...] = field(default=(), repr=False)

    @property
    def key_ids(self) -> dict[str, str | list[str]]:
        """The ring's public names, for the health details: the current key and the read keys."""
        return {"current": key_id(self.key), "read": [key_id(k) for k in self.read_keys]}

    def current_only(self) -> PhiPolicy:
        """This policy without its read keys: what opens once they are dropped."""
        return PhiPolicy(self.mode, self.key, self.hash_key, ())

    @property
    def hash_kind(self) -> str:
        """Which input hash new records get, for the health details: `hmac-sha256:<key id>` or
        `sha256` (unkeyed)."""
        return f"hmac-sha256:{key_id(self.hash_key)}" if self.hash_key else "sha256"

    def input_hash(self, inputs: Any) -> str:
        """`hmac-sha256:<key id>:<64 hex>` under the hash key, else the plain `sha256:<64 hex>`.
        The key id names the key without revealing it, so a record says which key made its hash
        and a new key never collides with an old one's hashes."""
        canonical = _canonical(inputs).encode()
        if not self.hash_key:
            return "sha256:" + hashlib.sha256(canonical).hexdigest()
        digest = hmac.new(self.hash_key, INPUT_HASH_PREFIX + canonical, hashlib.sha256)
        return f"hmac-sha256:{key_id(self.hash_key)}:{digest.hexdigest()}"

    def arguments_hash(self, arguments: Any) -> str:
        """The hash of a proposed action's arguments (spec 0.4), as `input_hash` hashes an
        input: keyed under the hash key, with its own prefix so it never equals the input hash
        of the same value, else the plain `sha256:<64 hex>`."""
        canonical = _canonical(arguments).encode()
        if not self.hash_key:
            return "sha256:" + hashlib.sha256(canonical).hexdigest()
        digest = hmac.new(self.hash_key, ACTION_HASH_PREFIX + canonical, hashlib.sha256)
        return f"hmac-sha256:{key_id(self.hash_key)}:{digest.hexdigest()}"

    def encrypt(self, decision_id: str, inputs: Any) -> str:
        return self.seal("input", inputs, decision_id)

    def decrypt(self, decision_id: str, token: str) -> Any:
        return self.open("input", token, decision_id)[0]

    def seal_note(self, note_id: str, decision_id: str, body: Any) -> str:
        return self.seal("note", body, note_id, decision_id)

    def open_note(self, note_id: str, decision_id: str, token: str) -> Any:
        return self.open("note", token, note_id, decision_id)[0]

    def seal_details(self, decision_id: str, details: Any) -> str:
        """Seal what a record leaves out beside its kept input (each answer's evidence)."""
        return self.seal("details", details, decision_id)

    def open_details(self, decision_id: str, token: str) -> Any:
        return self.open("details", token, decision_id)[0]

    def seal(self, kind: Sealed, value: Any, *ids: str) -> str:
        """Seal one value of a kind (`input` and `details`: a decision's id; `note`: the note's
        id, then its decision's; `dataset`: its id; `connection`: its row's id, its
        organization and its agent; `written`: its item row's id) under the current key."""
        return self._seal(_aad(kind, ids), value)

    def open(self, kind: Sealed, token: str, *ids: str) -> tuple[Any, str]:
        """Open one value of a kind (as `seal`): the value, and the id of the key that opened
        it."""
        return self._open(_aad(kind, ids), token, _WHAT[kind])

    @property
    def ring_ids(self) -> frozenset[str]:
        """The ids of every key this policy opens with."""
        return frozenset(key_id(k) for k in self._ring())

    def _ring(self) -> tuple[bytes, ...]:
        return tuple(k for k in (self.key, *self.read_keys) if k)

    def _seal(self, aad: bytes, value: Any) -> str:
        return seal_with(self.key, aad, value)

    def _open(self, aad: bytes, token: str, what: str) -> tuple[Any, str]:
        return open_with(self._ring(), aad, token, what)


def seal_with(key: bytes, aad: bytes, value: Any) -> str:
    """`value` (as canonical JSON) sealed with AES-256-GCM under `key`, bound to `aad`: a fresh
    nonce for every seal, re-seals included; `v2:<key id>:<base64>`. Shared by the PHI policy
    and the evaluation-set keys (`core/evalset_keys.py`), which each bind their own associated
    data."""
    nonce = secrets.token_bytes(12)
    plain = _canonical(value).encode()
    sealed = base64.b64encode(nonce + AESGCM(key).encrypt(nonce, plain, aad)).decode()
    return f"v2:{key_id(key)}:{sealed}"


def open_with(ring: Sequence[bytes], aad: bytes, token: str, what: str) -> tuple[Any, str]:
    """A token `seal_with` made, opened with the key of `ring` it names: the value, and the id
    of the key that opened it. A token that names no key opens with none. A `MendariError`
    names `what`, never the token or a key."""
    named, raw = parse_token(token, what)
    if named is None:
        raise MendariError(f"{what} does not name its key")
    key = next((k for k in ring if key_id(k) == named), None)
    if key is None:
        raise MendariError(f"{what} is sealed under key {named}, which this deployment lacks")
    try:
        plain = AESGCM(key).decrypt(raw[:12], raw[12:], aad)
    except InvalidTag:
        raise MendariError(f"{what} does not decrypt with this deployment's keys") from None
    try:
        return json.loads(plain), named
    except ValueError:
        raise MendariError(f"{what} is not a stored value") from None


# A note's associated data: its kind, then its id and its decision's, NUL-separated (ids never
# hold a NUL). An input's is its bare decision id, which never starts with these prefixes.
NOTE_AAD = b"mendari-note-v1\x00"
# A record's sealed details (`seal_details`): the kind, then the decision id.
DETAILS_AAD = b"mendari-details-v1\x00"
# A pack store's dataset (LP-20g): its id after this.
DATASET_AAD = b"mendari-store-dataset-v1\x00"
# An agent's connection token (ADR-0021): its row's id, its organization and its agent after
# this, NUL-separated, so a token never opens for another row, organization or agent.
CONNECTION_AAD = b"mendari-agent-connection-v1\x00"
# A written output to grade, with its context (`core/written.py`): its item row's id after this.
WRITTEN_AAD = b"mendari-written-v1\x00"
# What the input hash's HMAC covers before the input, so the hash key signs nothing else alike.
INPUT_HASH_PREFIX = b"mendari-input-v1\x00"
# What the hash of a proposed action's arguments covers before them (`arguments_hash`).
ACTION_HASH_PREFIX = b"mendari-action-v1\x00"
GENERATE = '`python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`'
_GENERATE = GENERATE


# What a sealed token is, and nothing else: `v2:<16 hex>:` then strict base64 of at least a
# nonce and a tag (28 bytes). Strict base64 of that size without the prefix names no key, and
# opens with none. A plain-JSON note (mode `off`) starts with "{" and is never opened.
_V2 = re.compile(r"v2:([0-9a-f]{16}):")
_MIN_SEALED = 12 + 16
READ_KEYS_MAX = 4
Sealed = Literal["input", "details", "note", "dataset", "connection", "written"]
_WHAT: dict[str, str] = {
    "input": "a stored input",
    "details": "a stored record's details",
    "note": "a stored note",
    "dataset": "a stored dataset",
    "connection": "an agent connection's token",
    "written": "a stored written output",
}


def token_key(token: str) -> str:
    """The key id a token names, `invalid` for a value that is no sealed token or names no key.
    Reads the token's prefix only: nothing is opened."""
    try:
        named, _ = parse_token(token, "a token")
    except MendariError:
        return "invalid"
    return "invalid" if named is None else named


def _aad(kind: Sealed, ids: tuple[str, ...]) -> bytes:
    """A sealed value's associated data: an input's is its bare decision id, a record's
    details' and a note's their kind, then their ids (`DETAILS_AAD`, `NOTE_AAD`)."""
    if kind == "input":
        (decision_id,) = ids
        return decision_id.encode()
    if kind == "details":
        (decision_id,) = ids
        return DETAILS_AAD + decision_id.encode()
    if kind == "dataset":
        (dataset_id,) = ids
        return DATASET_AAD + dataset_id.encode()
    if kind == "written":
        (item_row_id,) = ids
        return WRITTEN_AAD + item_row_id.encode()
    if kind == "connection":
        connection_id, org, pack_id = ids
        return CONNECTION_AAD + b"\x00".join(
            part.encode() for part in (connection_id, org, pack_id)
        )
    note_id, decision_id = ids
    return _note_aad(note_id, decision_id)


def parse_token(token: str, what: str) -> tuple[str | None, bytes]:
    """A token's key id (None: it names none, and opens with none) and its nonce and
    ciphertext."""
    named = _V2.match(token)
    body = token[named.end() :] if named else token
    try:
        raw = base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError):
        raise MendariError(f"{what} is not a sealed token") from None
    if len(raw) < _MIN_SEALED:
        raise MendariError(f"{what} is not a sealed token")
    return (named.group(1) if named else None), raw


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _note_aad(note_id: str, decision_id: str) -> bytes:
    return NOTE_AAD + note_id.encode() + b"\x00" + decision_id.encode()


def key_id(key: bytes) -> str:
    """A key's public name: the first 8 bytes, in hex, of an HMAC of a fixed text under it."""
    return hmac.new(key, b"mendari-key-id", hashlib.sha256).hexdigest()[:16]


def _key(name: str) -> bytes:
    """A 32-byte key from a base64 environment variable; empty when unset or malformed."""
    try:
        key = base64.b64decode(os.environ.get(name, "").strip(), validate=True)
    except (binascii.Error, ValueError):
        return b""
    return key if len(key) == 32 else b""


def require_protection(policy: PhiPolicy, *, sign_in: bool, launch: bool) -> None:
    """Refuse, at startup, a deployment that would keep inputs in plaintext where it must not.
    The hosted site (`VERCEL` set: Production and Preview; its entrypoint, the root `app.py`,
    sets it too) runs `encrypt` (its default, `phi_policy`) or `hash`, never `off`. Elsewhere,
    with `sign_in` on, people bring other people's cases, so the mode is `encrypt` or `hash`,
    never `off`, written out or defaulted, whatever the database. With only the EHR `launch`
    on (a launch fetches the patient's record), the mode is chosen, never defaulted: `off`
    written out is for a local sandbox on synthetic data only (the server logs a warning)."""
    hosted = bool(os.environ.get("VERCEL"))
    if hosted and policy.mode == "off":
        raise MendariError(
            "The hosted deployment keeps no input in plaintext: set MENDARI_PHI_MODE=encrypt "
            "(with MENDARI_PHI_KEY and MENDARI_INPUT_HASH_KEY) or hash (with "
            "MENDARI_INPUT_HASH_KEY) for Production and Preview, then redeploy."
        )
    if hosted:
        return
    if sign_in and policy.mode == "off":
        raise MendariError(
            "Sign-in is on, so inputs must be protected: set MENDARI_PHI_MODE=encrypt (with "
            "MENDARI_PHI_KEY and MENDARI_INPUT_HASH_KEY) or hash (with MENDARI_INPUT_HASH_KEY); "
            "off is for a local sandbox without sign-in (docs/deploying-with-phi.md)."
        )
    if launch and not os.environ.get("MENDARI_PHI_MODE", "").strip():
        raise MendariError(
            "The EHR launch is on, so choose how inputs are kept: MENDARI_PHI_MODE=encrypt (with "
            "MENDARI_PHI_KEY and MENDARI_INPUT_HASH_KEY), hash (with MENDARI_INPUT_HASH_KEY), or "
            "off for a local sandbox without sign-in on synthetic data only "
            "(docs/deploying-with-phi.md)."
        )


def phi_policy() -> PhiPolicy:
    """The deployment's PHI policy, checked at startup: `hash` and `encrypt` need the input hash
    key, `encrypt` the PHI key too, and the two keys differ. The mode defaults to `off`, and to
    `encrypt` on the hosted site (`VERCEL`), where a missing key names both keys."""
    chosen = os.environ.get("MENDARI_PHI_MODE", "").strip().lower()
    hosted_default = not chosen and bool(os.environ.get("VERCEL"))
    mode = chosen or ("encrypt" if hosted_default else "off")
    if mode not in ("off", "hash", "encrypt"):
        raise MendariError("MENDARI_PHI_MODE must be off, hash or encrypt")
    hash_key = _key("MENDARI_INPUT_HASH_KEY")
    if os.environ.get("MENDARI_INPUT_HASH_KEY", "").strip() and not hash_key:
        raise MendariError(f"MENDARI_INPUT_HASH_KEY must be 32 random bytes, base64 ({_GENERATE})")
    key = _key("MENDARI_PHI_KEY") if mode == "encrypt" else b""
    if hosted_default and not (key and hash_key):
        raise MendariError(
            "The hosted deployment runs PHI mode encrypt unless told hash: set MENDARI_PHI_KEY "
            "and MENDARI_INPUT_HASH_KEY, two different keys of 32 random bytes, base64 "
            f"({_GENERATE}), for Production and Preview (or MENDARI_PHI_MODE=hash with "
            "MENDARI_INPUT_HASH_KEY), then redeploy."
        )
    if mode != "off" and not hash_key:
        raise MendariError(
            f"MENDARI_PHI_MODE={mode} needs MENDARI_INPUT_HASH_KEY: 32 random bytes, base64 "
            f"({_GENERATE}), apart from MENDARI_PHI_KEY"
        )
    listed = os.environ.get("MENDARI_PHI_READ_KEYS", "").strip()
    if mode != "encrypt":
        if listed:
            raise MendariError("MENDARI_PHI_READ_KEYS is for PHI mode encrypt only")
        return PhiPolicy(mode=mode, hash_key=hash_key)  # type: ignore[arg-type]
    if not key:
        raise MendariError(
            f"MENDARI_PHI_MODE=encrypt needs MENDARI_PHI_KEY: 32 random bytes, base64 ({_GENERATE})"
        )
    if hmac.compare_digest(key, hash_key):
        raise MendariError("MENDARI_INPUT_HASH_KEY must differ from MENDARI_PHI_KEY")
    read_keys = _read_keys(listed, key, hash_key)
    return PhiPolicy(mode="encrypt", key=key, hash_key=hash_key, read_keys=read_keys)


def _read_keys(listed: str, key: bytes, hash_key: bytes) -> tuple[bytes, ...]:
    """$MENDARI_PHI_READ_KEYS (`read_key_ring`): unlike the current key and the input hash key."""
    return read_key_ring(
        "MENDARI_PHI_READ_KEYS",
        listed,
        (key, hash_key),
        "the current PHI key or the input hash key",
    )


def read_key_ring(
    variable: str, listed: str, against: Sequence[bytes], against_text: str
) -> tuple[bytes, ...]:
    """A read-key ring (`variable`, comma-separated): each 32 random bytes in base64, unlike
    each key of `against` (`against_text` names them) and each other; at most `READ_KEYS_MAX`.
    A problem names the entry's position, never its value."""
    if not listed:
        return ()
    entries = [entry.strip() for entry in listed.split(",")]
    if len(entries) > READ_KEYS_MAX:
        raise MendariError(f"{variable} holds at most {READ_KEYS_MAX} keys")
    keys: list[bytes] = []
    for position, entry in enumerate(entries, start=1):
        try:
            read = base64.b64decode(entry, validate=True) if entry else b""
        except (binascii.Error, ValueError):
            read = b""
        if len(read) != 32:
            raise MendariError(f"{variable} entry {position} must be 32 random bytes, base64")
        if any(hmac.compare_digest(read, other) for other in against if other):
            raise MendariError(f"{variable} entry {position} is {against_text}")
        if any(hmac.compare_digest(read, other) for other in keys):
            raise MendariError(f"{variable} entry {position} repeats an earlier one")
        keys.append(read)
    return tuple(keys)
