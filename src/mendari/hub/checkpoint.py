# SPDX-License-Identifier: Apache-2.0
"""A registry checkpoint: the signed statement that the registry's chain (`hub_events`) had a
given head on a given day (LP-19, owner decision of 2026-11-10), as the engine's daily
checkpoint is for the audit chain (`core/checkpoint.py`, LP-21k).

    {"format": "mendari.registry-checkpoint/1", "date": "2026-11-10",
     "head": "<the head's 64 hex>", "key_id": "ed25519:<16 hex>", "signature": "<base64>"}

The registry's chain is unkeyed: whoever can write its whole database can rewrite it, and it
then verifies. A statement kept anywhere the registry cannot write shows it: the head it names
must still be an event of the chain (`RegistryStore.verify(head=…)`, `mendari registry verify
--checkpoint`). It shows nothing about events appended after it.

The signature is Ed25519 over the canonical JSON of the other four fields (sorted keys, no
spaces), by the registry's own key (`MENDARI_REGISTRY_SIGNING_KEY`, `hub/yank.py`
`registry_key`). The `format` is inside what is signed and is this statement's own, so no
recall, acceptance, engine checkpoint or scoreboard signature by the same key is ever one, and
this is none of them. The day is UTC, as every time the registry stores is. The statement names
the head only, never a count. Nothing of it is stored in the registry's database: a copy there
could be rewritten with the chain.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from datetime import date
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from mendari.core.checkpoint import key_id as raw_key_id

FORMAT = "mendari.registry-checkpoint/1"
FIELDS = ("format", "date", "head", "key_id")
_HEAD = re.compile(r"[0-9a-f]{64}")


def _signed(fields: Mapping[str, Any]) -> bytes:
    return json.dumps(dict(fields), sort_keys=True, separators=(",", ":")).encode()


def sign(head: str, day: date, key: Ed25519PrivateKey) -> dict[str, str]:
    """The statement that the registry's chain had the head `head` on `day`."""
    if not _HEAD.fullmatch(head):
        raise ValueError("a checkpoint names a chain hash")
    fields = {
        "format": FORMAT,
        "date": day.isoformat(),
        "head": head,
        "key_id": raw_key_id(key.public_key()),
    }
    signature = base64.b64encode(key.sign(_signed(fields))).decode("ascii")
    return {**fields, "signature": signature}


def problem(statement: Any, public: Ed25519PublicKey) -> str | None:
    """Why `statement` is not a registry checkpoint signed with `public` (None: it is one)."""
    if not isinstance(statement, Mapping) or statement.get("format") != FORMAT:
        return f"not a {FORMAT} statement"
    if set(statement) != {*FIELDS, "signature"}:
        return f"its fields are {', '.join(FIELDS)} and signature"
    fields = {name: statement[name] for name in FIELDS}
    if not all(isinstance(value, str) for value in fields.values()):
        return "a statement's fields are text"
    if not _HEAD.fullmatch(fields["head"]):
        return "its head is not a chain hash"
    try:
        date.fromisoformat(fields["date"])
    except ValueError:
        return "its date is not a day"
    if fields["key_id"] != raw_key_id(public):
        return "it was signed with another key"
    try:
        signature = base64.b64decode(str(statement["signature"]), validate=True)
        public.verify(signature, _signed(fields))
    except (binascii.Error, ValueError, InvalidSignature):
        return "its signature does not hold"
    return None
