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
(`core/audit.py`, "notes"). A note's associated data names its kind, its id and its decision, so
a note's ciphertext never opens as another note's, nor as an input.
"""

from __future__ import annotations

import base64
import binascii
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

    def encrypt(self, decision_id: str, inputs: Any) -> str:
        return self._seal(decision_id.encode(), inputs)

    def decrypt(self, decision_id: str, token: str) -> Any:
        return self._open(decision_id.encode(), token, "a stored input")

    def seal_note(self, note_id: str, decision_id: str, body: Any) -> str:
        return self._seal(_note_aad(note_id, decision_id), body)

    def open_note(self, note_id: str, decision_id: str, token: str) -> Any:
        return self._open(_note_aad(note_id, decision_id), token, "a stored note")

    def _seal(self, aad: bytes, value: Any) -> str:
        nonce = secrets.token_bytes(12)
        plain = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
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
# hold a NUL). An input's is its bare decision id, which never starts with this prefix.
NOTE_AAD = b"mendari-note-v1\x00"


def _note_aad(note_id: str, decision_id: str) -> bytes:
    return NOTE_AAD + note_id.encode() + b"\x00" + decision_id.encode()


def phi_policy() -> PhiPolicy:
    mode = os.environ.get("MENDARI_PHI_MODE", "").strip().lower() or "off"
    if mode not in ("off", "hash", "encrypt"):
        raise MendariError("MENDARI_PHI_MODE must be off, hash or encrypt")
    if mode != "encrypt":
        return PhiPolicy(mode=mode)  # type: ignore[arg-type]
    try:
        key = base64.b64decode(os.environ.get("MENDARI_PHI_KEY", ""), validate=True)
    except (binascii.Error, ValueError):
        key = b""
    if len(key) != 32:
        raise MendariError(
            "MENDARI_PHI_MODE=encrypt needs MENDARI_PHI_KEY: 32 random bytes, base64 "
            '(e.g. `python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`)'
        )
    return PhiPolicy(mode="encrypt", key=key)
