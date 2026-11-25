# SPDX-License-Identifier: Apache-2.0
"""An acceptance: the registry's counter-signature on a version it accepted (LP-19i, ADR-0015).

    {"format": "mendari.registry-acceptance/1", "agent": "site-a/inbox", "version": "1.0.0",
     "digest": "sha256:…", "archive_sha256": "<64 hex>",
     "publisher_key_id": "<the namespace key that signed the archive>",
     "key_id": "ed25519:<16 hex>", "at": "2026-11-10T12:00:00Z", "signature": "<base64>"}

The signature is Ed25519 over the canonical JSON of every other field (sorted keys, no spaces),
by the registry's own key (`MENDARI_REGISTRY_SIGNING_KEY`, `hub/yank.py` `registry_key`), as a
recall the registry makes is signed. The `format` is inside what is signed and is this record's
own, so no recall or checkpoint signature by the same key is ever an acceptance.

The registry writes one when a version becomes listed (`hub/registry.py`): a version written
straight into its database has none. A client that trusts the registry's key refuses a version
whose record is missing, does not verify, or names another agent, version, digest, archive or
publisher key (`hub/client.py`).

The keys a client trusts without first use ship in `registry_keys.json`, beside this module:

    {"format": "mendari.registry-keys/1",
     "keys": [{"key_id": "ed25519:<16 hex>", "public_key": "<base64, 32 bytes>"}]}

Public keys only, never a hostname: whichever registry presents one of them and signs with it
is trusted. It ships the Mendari registry's key, made by its owner on 2026-11-15.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from mendari.core.checkpoint import key_id as raw_key_id
from mendari.core.checkpoint import load_public
from mendari.errors import HubError, MendariError
from mendari.hub import yank

FORMAT = "mendari.registry-acceptance/1"
KEYS_FORMAT = "mendari.registry-keys/1"
KEYS_FILE = Path(__file__).with_name("registry_keys.json")
# What an acceptance names: the version, exactly (`NAMED`), then who signed it and when.
NAMED = ("agent", "version", "digest", "archive_sha256", "publisher_key_id")
FIELDS = ("format", *NAMED, "key_id", "at")
_SHA = re.compile(r"[0-9a-f]{64}")


def _signed(fields: Mapping[str, Any]) -> bytes:
    return json.dumps(dict(fields), sort_keys=True, separators=(",", ":")).encode()


def sign(
    *,
    agent: str,
    version: str,
    digest: str,
    archive_sha256: str,
    publisher_key_id: str,
    key: Ed25519PrivateKey,
    at: str | None = None,
) -> dict[str, str]:
    """An acceptance of one version, signed with the registry's key."""
    fields = {
        "format": FORMAT,
        "agent": agent,
        "version": version,
        "digest": digest,
        "archive_sha256": archive_sha256,
        "publisher_key_id": publisher_key_id,
        "key_id": raw_key_id(key.public_key()),
        "at": at or yank.now(),
    }
    problem = shape_problem(fields)
    if problem:
        raise MendariError(f"not an acceptance: {problem}")
    signature = base64.b64encode(key.sign(_signed(fields))).decode("ascii")
    return {**fields, "signature": signature}


def shape_problem(record: Any) -> str | None:
    """Why `record` (without its signature) is not an acceptance's fields (None: it is)."""
    if not isinstance(record, Mapping):
        return "not a JSON object"
    if set(record) - {*FIELDS, "signature"} or not set(FIELDS) <= set(record):
        return f"its fields are {', '.join(FIELDS)} and signature"
    if not all(isinstance(record[name], str) and record[name] for name in FIELDS):
        return "its fields are text"
    if record["format"] != FORMAT:
        return f"not a {FORMAT} record"
    if not _SHA.fullmatch(record["archive_sha256"]):
        return "its archive_sha256 is a SHA-256"
    if yank.at(record) is None:
        return "its time is not a UTC time"
    return None


def problem(record: Any, public: Ed25519PublicKey, key_id: str) -> str | None:
    """Why `record` is not an acceptance signed with `public`, named `key_id` (None: it is)."""
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


def names_problem(record: Mapping[str, Any], **named: Any) -> str | None:
    """Why a well-formed `record` is not about exactly this agent, version, digest, archive and
    publisher key (`NAMED`, each given by name; None: it is)."""
    assert set(named) == set(NAMED), sorted(named)
    other = [name for name in NAMED if record.get(name) != named[name]]
    return f"it names another {' and '.join(other)}".replace("_", " ") if other else None


def shipped_keys(path: Path | None = None) -> dict[str, Ed25519PublicKey]:
    """The registry keys shipped with this package (`registry_keys.json`, or the file at
    `path`), by id: each trusted without first use. Read strictly: a file that is not one, or a
    key filed under an id that is not its own, is refused whole rather than trusted in part."""
    source = path or KEYS_FILE
    try:
        listing = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HubError(f"the registry keys file {source.name} cannot be read") from None
    keys = listing.get("keys") if isinstance(listing, dict) else None
    if not isinstance(listing, dict) or set(listing) != {"format", "keys"}:
        raise HubError(f"{source.name} is not a {KEYS_FORMAT} file")
    if listing["format"] != KEYS_FORMAT or not isinstance(keys, list):
        raise HubError(f"{source.name} is not a {KEYS_FORMAT} file")
    found: dict[str, Ed25519PublicKey] = {}
    for entry in keys:
        if not isinstance(entry, dict) or set(entry) != {"key_id", "public_key"}:
            raise HubError(f"{source.name}: a key is its key_id and public_key, nothing else")
        try:
            key = load_public(str(entry["public_key"]))
        except MendariError:
            raise HubError(f"{source.name}: a public key is 32 bytes in base64") from None
        if raw_key_id(key) != entry["key_id"] or entry["key_id"] in found:
            raise HubError(f"{source.name}: a key is filed once, under its own id")
        found[str(entry["key_id"])] = key
    return found
