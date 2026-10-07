# SPDX-License-Identifier: Apache-2.0
"""Sealing session-table rows under the secret that names them (ADR-0006).

An EHR launch's rows hold what must not be readable at rest: the session's EHR access token and
patient, the pending launch's PKCE verifier and nonce. The database keeps each row under
sha256(kind || 0x00 || value) (`AuditStore`), and the value itself is a secret only the browser
holds: the session cookie for a launch's session, the binding cookie for a pending launch. So
the row is sealed under a key derived from that value:

- **key**: HKDF-SHA256(ikm = the value, salt = `mendari-session-seal-v1`, info = the kind), 32
  bytes, domain-separated from the row key's plain SHA-256;
- **cipher**: AES-256-GCM with a random 96-bit nonce (one key per row and a few writes per row);
- **associated data**: `mendari-session-v1` NUL kind NUL row key, so a sealed value opens
  neither as another kind nor in another row;
- **stored as** `{"sealed": "s1:<base64(nonce || ciphertext || tag)>"}`, still JSON, so a
  release that predates sealing fails closed on it instead of reading it as a session.

A database reader, a backup, the WAL or a point-in-time copy opens nothing, and there is no key
to provision, rotate or lose: the server itself can open a row only while a request carries its
cookie. Anything that does not open (another row's or kind's value, a changed byte, a plaintext
row from before sealing) is a `SealError`; the store deletes such a row and treats it as absent.
"""

from __future__ import annotations

import base64
import binascii
import json
import secrets
from collections.abc import Mapping
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

SALT = b"mendari-session-seal-v1"
AAD = b"mendari-session-v1\x00"
PREFIX = "s1:"
FIELD = "sealed"
_NONCE = 12


class SealError(Exception):
    """A stored row does not open under this secret, kind and row (or was never sealed)."""


def seal(data: Mapping[str, Any], *, secret: str, kind: str, row_key: str) -> dict[str, str]:
    """`data` sealed for the row `row_key` of `kind`, under a key derived from `secret`."""
    nonce = secrets.token_bytes(_NONCE)
    plain = json.dumps(dict(data), sort_keys=True, separators=(",", ":")).encode()
    sealed = AESGCM(_key(secret, kind)).encrypt(nonce, plain, _aad(kind, row_key))
    return {FIELD: PREFIX + base64.b64encode(nonce + sealed).decode()}


def unseal(stored: Any, *, secret: str, kind: str, row_key: str) -> dict[str, Any]:
    """The data `seal` sealed for this row, or a `SealError`."""
    token = stored.get(FIELD) if isinstance(stored, Mapping) and len(stored) == 1 else None
    if not isinstance(token, str) or not token.startswith(PREFIX):
        raise SealError("the row is not sealed")
    if not secret:
        raise SealError("no secret to open the row with")
    try:
        raw = base64.b64decode(token.removeprefix(PREFIX), validate=True)
        plain = AESGCM(_key(secret, kind)).decrypt(raw[:_NONCE], raw[_NONCE:], _aad(kind, row_key))
        data = json.loads(plain)
    except (binascii.Error, InvalidTag, ValueError):
        raise SealError("the row does not open here") from None
    if not isinstance(data, dict):
        raise SealError("the row does not open here")
    return data


def _key(secret: str, kind: str) -> bytes:
    if not secret:
        raise ValueError("a sealed row needs the secret that names it")
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=SALT, info=kind.encode())
    return hkdf.derive(secret.encode())


def _aad(kind: str, row_key: str) -> bytes:
    return AAD + kind.encode() + b"\x00" + row_key.encode()
