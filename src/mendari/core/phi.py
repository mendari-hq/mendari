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

Notes (a reviewer's or a rater's free text, the payer's answer to a prior authorization) follow
inputs: sealed under the same key in `encrypt`, kept in the clear in `off`, refused in `hash`
(`core/audit.py`, "notes"). So does an answer's evidence, which can quote the input (a lab value
and its date): kept in the record only when the input is, sealed beside a kept input in
`encrypt`, dropped otherwise. Each kind of sealed data has its own associated data (its kind,
then its ids), so a ciphertext never opens as another kind, nor for another decision or note.

The input hash in every record is keyed in `hash` and `encrypt`: an HMAC-SHA256 under
`$MENDARI_INPUT_HASH_KEY` (32 bytes, base64; its own key, so rotating the PHI key leaves hashes
alone, and `hash` mode, which has no PHI key, still has one). A plain SHA-256 of a short or
structured input can be found by guessing (an age, a sex and an order code take a few thousand
tries), and anyone who may read a decision sees its hash. Without the key (mode `off` only) the
hash stays the plain `sha256:` of earlier releases.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
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

    def encrypt(self, decision_id: str, inputs: Any) -> str:
        return self._seal(decision_id.encode(), inputs)

    def decrypt(self, decision_id: str, token: str) -> Any:
        return self._open(decision_id.encode(), token, "a stored input")

    def seal_note(self, note_id: str, decision_id: str, body: Any) -> str:
        return self._seal(_note_aad(note_id, decision_id), body)

    def open_note(self, note_id: str, decision_id: str, token: str) -> Any:
        return self._open(_note_aad(note_id, decision_id), token, "a stored note")

    def seal_details(self, decision_id: str, details: Any) -> str:
        """Seal what a record leaves out beside its kept input (each answer's evidence)."""
        return self._seal(DETAILS_AAD + decision_id.encode(), details)

    def open_details(self, decision_id: str, token: str) -> Any:
        return self._open(DETAILS_AAD + decision_id.encode(), token, "a stored record's details")

    def _seal(self, aad: bytes, value: Any) -> str:
        nonce = secrets.token_bytes(12)
        plain = _canonical(value).encode()
        sealed = AESGCM(self.key).encrypt(nonce, plain, aad)
        return base64.b64encode(nonce + sealed).decode()

    def _open(self, aad: bytes, token: str, what: str) -> Any:
        raw = base64.b64decode(token)
        try:
            plain = AESGCM(self.key).decrypt(raw[:12], raw[12:], aad)
        except (InvalidTag, ValueError):  # ValueError: no key (not in mode encrypt)
            raise MendariError(f"{what} does not decrypt with this key") from None
        return json.loads(plain)


# A note's associated data: its kind, then its id and its decision's, NUL-separated (ids never
# hold a NUL). An input's is its bare decision id, which never starts with these prefixes.
NOTE_AAD = b"mendari-note-v1\x00"
# A record's sealed details (`seal_details`): the kind, then the decision id.
DETAILS_AAD = b"mendari-details-v1\x00"
# What the input hash's HMAC covers before the input, so the hash key signs nothing else alike.
INPUT_HASH_PREFIX = b"mendari-input-v1\x00"
_GENERATE = '`python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`'


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


def phi_policy() -> PhiPolicy:
    """The deployment's PHI policy, checked at startup: `hash` and `encrypt` need the input hash
    key, `encrypt` the PHI key too, and the two keys differ."""
    mode = os.environ.get("MENDARI_PHI_MODE", "").strip().lower() or "off"
    if mode not in ("off", "hash", "encrypt"):
        raise MendariError("MENDARI_PHI_MODE must be off, hash or encrypt")
    hash_key = _key("MENDARI_INPUT_HASH_KEY")
    if os.environ.get("MENDARI_INPUT_HASH_KEY", "").strip() and not hash_key:
        raise MendariError(f"MENDARI_INPUT_HASH_KEY must be 32 random bytes, base64 ({_GENERATE})")
    if mode != "off" and not hash_key:
        raise MendariError(
            f"MENDARI_PHI_MODE={mode} needs MENDARI_INPUT_HASH_KEY: 32 random bytes, base64 "
            f"({_GENERATE}), apart from MENDARI_PHI_KEY"
        )
    if mode != "encrypt":
        return PhiPolicy(mode=mode, hash_key=hash_key)  # type: ignore[arg-type]
    key = _key("MENDARI_PHI_KEY")
    if not key:
        raise MendariError(
            f"MENDARI_PHI_MODE=encrypt needs MENDARI_PHI_KEY: 32 random bytes, base64 ({_GENERATE})"
        )
    if hmac.compare_digest(key, hash_key):
        raise MendariError("MENDARI_INPUT_HASH_KEY must differ from MENDARI_PHI_KEY")
    return PhiPolicy(mode="encrypt", key=key, hash_key=hash_key)
