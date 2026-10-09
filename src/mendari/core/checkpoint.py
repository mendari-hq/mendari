# SPDX-License-Identifier: Apache-2.0
"""The signed daily checkpoint (LP-21k, ADR-0010 item 7): a statement that the audit chain's head
was a given hash on a given day, signed with the deployment's Ed25519 key and published, so a
copy kept anywhere else shows later whether the chain was rewritten from before it. A
checkpoint in the database (`audit_checkpoints`, LP-21j) cannot show that: whoever can rewrite
the chain can rewrite it too.

    {"format": "mendari.checkpoint/1", "date": "2026-10-08", "head": "<the head's 64 hex>",
     "key_id": "ed25519:<16 hex>", "signature": "<base64>"}

The signature is Ed25519 over the canonical JSON of the other four fields (sorted keys, no
spaces). The statement names the head only: no sequence number and no count, so it tells
nobody how many decisions a deployment makes. The local day is the deployment's
(`MENDARI_TIMEZONE`).

The key is `MENDARI_CHECKPOINT_KEY`: 32 random bytes in base64 (`openssl rand -base64 32`), the
Ed25519 private key's seed. Unset, nothing is signed or published. A malformed one stops the
server and the command (a statement that cannot be checked is worse than none).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
from collections.abc import Mapping
from datetime import date
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from mendari.errors import MendariError

FORMAT = "mendari.checkpoint/1"
KEY_ENV = "MENDARI_CHECKPOINT_KEY"
_HEAD = re.compile(r"[0-9a-f]{64}")
_FIELDS = ("format", "date", "head", "key_id")


def checkpoint_key() -> Ed25519PrivateKey | None:
    """The deployment's signing key ($MENDARI_CHECKPOINT_KEY), or None when it is unset."""
    raw = os.environ.get(KEY_ENV, "").strip()
    if not raw:
        return None
    try:
        seed = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        seed = b""
    if len(seed) != 32:
        raise MendariError(
            f"{KEY_ENV} must be 32 random bytes in base64 (openssl rand -base64 32), the "
            "Ed25519 key that signs the daily checkpoint"
        )
    return Ed25519PrivateKey.from_private_bytes(seed)


def public_text(key: Ed25519PublicKey) -> str:
    """The public key as published: its 32 raw bytes in base64."""
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def load_public(text: str) -> Ed25519PublicKey:
    """A public key as `public_text` gives it; a `MendariError` for anything else."""
    try:
        raw = base64.b64decode(text.strip(), validate=True)
    except (binascii.Error, ValueError):
        raw = b""
    if len(raw) != 32:
        raise MendariError("a checkpoint's public key is 32 bytes in base64")
    return Ed25519PublicKey.from_public_bytes(raw)


def key_id(key: Ed25519PublicKey) -> str:
    """As a publisher key's (`hub/archive.py`): `ed25519:` and 16 hex of its SHA-256."""
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return "ed25519:" + hashlib.sha256(raw).hexdigest()[:16]


def _signed(fields: Mapping[str, Any]) -> bytes:
    return json.dumps(dict(fields), sort_keys=True, separators=(",", ":")).encode()


def sign(head: str, day: date, key: Ed25519PrivateKey) -> dict[str, str]:
    """The statement that the chain's head was `head` on `day`."""
    if not _HEAD.fullmatch(head):
        raise ValueError("a checkpoint names a chain hash")
    fields = {
        "format": FORMAT,
        "date": day.isoformat(),
        "head": head,
        "key_id": key_id(key.public_key()),
    }
    signature = base64.b64encode(key.sign(_signed(fields))).decode("ascii")
    return {**fields, "signature": signature}


def problem(statement: Any, public: Ed25519PublicKey) -> str | None:
    """Why `statement` is not a checkpoint signed with `public` (None: it is one)."""
    if not isinstance(statement, Mapping) or statement.get("format") != FORMAT:
        return f"not a {FORMAT} statement"
    fields = {name: statement.get(name) for name in _FIELDS}
    if not all(isinstance(value, str) for value in fields.values()):
        return "a statement's fields are text"
    if not _HEAD.fullmatch(str(fields["head"])):
        return "its head is not a chain hash"
    try:
        date.fromisoformat(str(fields["date"]))
    except ValueError:
        return "its date is not a day"
    if fields["key_id"] != key_id(public):
        return "it was signed with another key"
    try:
        signature = base64.b64decode(str(statement.get("signature", "")), validate=True)
        public.verify(signature, _signed(fields))
    except (binascii.Error, ValueError, InvalidSignature):
        return "its signature does not hold"
    return None
