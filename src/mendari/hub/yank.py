# SPDX-License-Identifier: Apache-2.0
"""A yank: the signed record that recalls one published version (LP-18c, ADR-0012).

    {"format": "mendari.yank/1", "pack": "site-a/inbox", "version": "1.0.0",
     "digest": "sha256:…", "archive_sha256": "<64 hex>", "reason": "…", "severity": "high",
     "signer": "publisher", "key_id": "ed25519:<16 hex>", "at": "2026-10-09T12:00:00Z",
     "signature": "<base64>"}

The signature is Ed25519 over the canonical JSON of every other field (sorted keys, no spaces).
Whoever recalls signs: the publisher with one of its namespace's keys (`mendari yank`), or the
registry with its own key (`MENDARI_REGISTRY_SIGNING_KEY`, 32 random bytes in base64, `openssl
rand -base64 32`) for a recall the registry makes. The registry checks the signature before it
stores the record; a client checks it again against the keys it pinned.

A yanked version leaves the registry's latest version, search and the namespace's list, and is
still served by its exact version with the record beside it. A yank is final: a version is
recalled once, and an archive is never deleted by a yank.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import unicodedata
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal, get_args

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from mendari.core.checkpoint import key_id as raw_key_id
from mendari.core.checkpoint import public_text
from mendari.errors import MendariError

FORMAT = "mendari.yank/1"
KEY_ENV = "MENDARI_REGISTRY_SIGNING_KEY"
Severity = Literal["low", "medium", "high", "critical"]
Signer = Literal["publisher", "registry"]
SEVERITIES: tuple[str, ...] = get_args(Severity)
SIGNERS: tuple[str, ...] = get_args(Signer)
FIELDS = (
    "format",
    "pack",
    "version",
    "digest",
    "archive_sha256",
    "reason",
    "severity",
    "signer",
    "key_id",
    "at",
)
MAX_REASON = 300
# How far a publisher's `at` may be from the registry's clock: a record signed long ago, or for
# later, is refused (a replay of an old record is refused anyway: a version is recalled once).
MAX_SKEW_S = 600
_SHA = re.compile(r"[0-9a-f]{64}")


def printable_line(text: str, limit: int) -> str | None:
    """Why `text` is not one line of printable text of at most `limit` characters (None: it is).
    Control, format and separator characters (C0, C1, bidirectional overrides, line and
    paragraph separators) would make it read as something else."""
    if len(text) > limit:
        return f"at most {limit} characters"
    if any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp", "Co", "Cs") for c in text):
        return "one line of printable text"
    return None


def registry_key() -> Ed25519PrivateKey | None:
    """The registry's own signing key ($MENDARI_REGISTRY_SIGNING_KEY), or None when unset."""
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
            "Ed25519 key that signs the registry's recalls"
        )
    return Ed25519PrivateKey.from_private_bytes(seed)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _signed(fields: Mapping[str, Any]) -> bytes:
    return json.dumps(dict(fields), sort_keys=True, separators=(",", ":")).encode()


def sign(
    *,
    pack: str,
    version: str,
    digest: str,
    archive_sha256: str,
    reason: str,
    severity: str,
    signer: str,
    key: Ed25519PrivateKey,
    key_id: str,
    at: str | None = None,
) -> dict[str, str]:
    """A yank record, signed with `key` (whose id, as the signer names its keys, is `key_id`)."""
    fields = {
        "format": FORMAT,
        "pack": pack,
        "version": version,
        "digest": digest,
        "archive_sha256": archive_sha256,
        "reason": reason,
        "severity": severity,
        "signer": signer,
        "key_id": key_id,
        "at": at or now(),
    }
    problem = shape_problem(fields)
    if problem:
        raise MendariError(f"not a yank: {problem}")
    signature = base64.b64encode(key.sign(_signed(fields))).decode("ascii")
    return {**fields, "signature": signature}


def shape_problem(record: Any) -> str | None:
    """Why `record` (without its signature) is not a yank's fields (None: it is)."""
    if not isinstance(record, Mapping):
        return "not a JSON object"
    if set(record) - {*FIELDS, "signature"} or not set(FIELDS) <= set(record):
        return f"its fields are {', '.join(FIELDS)} and signature"
    if not all(isinstance(record[name], str) for name in FIELDS):
        return "its fields are text"
    if record["format"] != FORMAT:
        return f"not a {FORMAT} record"
    if record["severity"] not in SEVERITIES:
        return f"its severity is one of {', '.join(SEVERITIES)}"
    if record["signer"] not in SIGNERS:
        return f"its signer is one of {', '.join(SIGNERS)}"
    if not _SHA.fullmatch(record["archive_sha256"]):
        return "its archive_sha256 is a SHA-256"
    if not record["reason"].strip():
        return "it needs a reason"
    why = printable_line(record["reason"], MAX_REASON)
    if why:
        return f"its reason is {why}"
    if at(record) is None:
        return "its time is not a UTC time"
    return None


def at(record: Mapping[str, Any]) -> datetime | None:
    """The record's time, or None if it is not a UTC ISO time."""
    try:
        when = datetime.fromisoformat(str(record.get("at", "")).replace("Z", "+00:00"))
    except ValueError:
        return None
    offset = when.utcoffset()
    return when if offset is not None and offset.total_seconds() == 0 else None


def problem(record: Any, public: Ed25519PublicKey, key_id: str) -> str | None:
    """Why `record` is not a yank signed with `public`, named `key_id` (None: it is one)."""
    shape = shape_problem(record)
    if shape:
        return shape
    if record["key_id"] != key_id:
        return "it was signed with another key"
    fields = {name: record[name] for name in FIELDS}
    try:
        signature = base64.b64decode(str(record.get("signature", "")), validate=True)
        public.verify(signature, _signed(fields))
    except (binascii.Error, ValueError, InvalidSignature):
        return "its signature does not hold"
    return None


def registry_public(key: Ed25519PrivateKey) -> dict[str, str]:
    """The registry's key as it publishes it: its id and its public key in base64."""
    public = key.public_key()
    return {"key_id": raw_key_id(public), "public_key": public_text(public)}
