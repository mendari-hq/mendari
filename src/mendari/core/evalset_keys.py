# SPDX-License-Identifier: Apache-2.0
"""The keys of hidden evaluation sets (LP-11, ADR-0011): what seals a set's content, and what
keys the hidden-case index and the chained case hashes. Nothing is stored with them yet (11c);
the store (11e) and the routes come later.

- `MENDARI_EVALSET_KEY` (32 random bytes, base64) seals, with AES-256-GCM: a set's header, each
  case's input and attributes, each label, the freeze record and the stored aggregates. Each
  token names its key (`v2:<key id>:`) from the first seal, and is bound to its kind, its set,
  its case (empty for what belongs to the set) and, for a label, the label: a token never opens
  as another kind, for another set, case or label, so two labels cannot be swapped.
- `MENDARI_EVALSET_READ_KEYS` (comma-separated, at most 4): keys that open, never seal (a
  rotation's previous or next key).
- `MENDARI_EVALSET_INDEX_KEY` (32 random bytes, base64) keys the hidden-case index (the
  canonical and `norm/v1` forms of each input, for the pack store's upload check, LP-20) and
  the chained hashes of case ids: HMAC-SHA256, written `hmac-sha256:<key id>:<hex>`. It seals
  nothing and is never applied to a label, so it alone recovers no label.

Both keys set: the feature is on. Both unset: off (`evalset_keys` returns None), and nothing
changes. Anything else stops the server and every command that reads them: a key that is not 32
bytes in base64, one set without the other, read keys without the key, or any two equal, among
these and every other key of the deployment (the PHI key and its read keys, the input hash key,
the checkpoint key), whatever the PHI mode. A message names variables and positions, never a
value. The owner sets both as Secret for the hosted Production environment only, so a Preview
copy of the database holds only ciphertext (the server refuses them elsewhere on Vercel)."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from mendari.core.phi import GENERATE, key_id, open_with, parse_token, read_key_ring, seal_with
from mendari.errors import MendariError

KEY_ENV = "MENDARI_EVALSET_KEY"
READ_ENV = "MENDARI_EVALSET_READ_KEYS"
INDEX_ENV = "MENDARI_EVALSET_INDEX_KEY"
# Every other key a deployment holds: an evaluation-set key may equal none of them.
OTHER_KEYS = ("MENDARI_PHI_KEY", "MENDARI_INPUT_HASH_KEY", "MENDARI_CHECKPOINT_KEY")
OTHER_RINGS = ("MENDARI_PHI_READ_KEYS",)

Kind = Literal["header", "input", "attributes", "label", "freeze", "aggregates"]
SET_KINDS = frozenset({"header", "freeze", "aggregates"})  # bound to the set, an empty case id
CASE_KINDS = frozenset({"input", "attributes", "label"})
AAD = b"mendari-evalset-v1\x00"
CASE_PREFIX = b"mendari-evalset-case-v1\x00"
INDEX_PREFIX = b"mendari-evalset-index-v1\x00"
_WHAT = "a sealed evaluation-set value"


@dataclass(frozen=True)
class EvalsetKeys:
    key: bytes = field(repr=False)
    index_key: bytes = field(repr=False)
    read_keys: tuple[bytes, ...] = field(default=(), repr=False)

    @property
    def key_ids(self) -> dict[str, str | list[str]]:
        """The keys' public names, for the health details: the one that seals, those it only
        opens with, and the index key's."""
        return {
            "current": key_id(self.key),
            "read": [key_id(k) for k in self.read_keys],
            "index": key_id(self.index_key),
        }

    def current_only(self) -> EvalsetKeys:
        """These keys without the read keys: what opens once they are dropped."""
        return EvalsetKeys(self.key, self.index_key)

    @property
    def ring_ids(self) -> frozenset[str]:
        """The ids of every key that opens a token."""
        return frozenset(key_id(k) for k in (self.key, *self.read_keys))

    def seal(
        self, kind: Kind, value: Any, set_id: str, case_id: str = "", label_id: str = ""
    ) -> str:
        """One value of a kind sealed under the current key, bound to its ids (`aad`)."""
        return seal_with(self.key, aad(kind, set_id, case_id, label_id), value)

    def open(
        self, kind: Kind, token: str, set_id: str, case_id: str = "", label_id: str = ""
    ) -> tuple[Any, str]:
        """One value of a kind (as `seal`): the value, and the id of the key that opened it.
        Only a token that names its key opens: none was sealed before key ids."""
        named, _ = parse_token(token, _WHAT)
        if named is None:
            raise MendariError(f"{_WHAT} does not name its key")
        ring = (self.key, *self.read_keys)
        return open_with(ring, aad(kind, set_id, case_id, label_id), token, _WHAT)

    def case_hash(self, set_id: str, case_id: str) -> str:
        """A case's keyed hash, as the chain records it: never its id in the clear."""
        message = CASE_PREFIX + _uuid(set_id, "set").encode() + b"\x00"
        message += _uuid(case_id, "case").encode()
        return self._hashed(message)

    def index_hashes(self, value: Any) -> dict[str, str]:
        """An input's two keyed hashes for the hidden-case index: its canonical form, and its
        `norm/v1` form (`evals/canonical.py`, the forms the upload check computes too)."""
        from mendari.evals.canonical import canonical_input, normalised_input

        return {
            "canonical": self._hashed(
                INDEX_PREFIX + b"canonical\x00" + canonical_input(value).encode()
            ),
            "text": self._hashed(INDEX_PREFIX + b"text\x00" + normalised_input(value).encode()),
        }

    def _hashed(self, message: bytes) -> str:
        digest = hmac.new(self.index_key, message, hashlib.sha256).hexdigest()
        return f"hmac-sha256:{key_id(self.index_key)}:{digest}"


def aad(kind: str, set_id: str, case_id: str = "", label_id: str = "") -> bytes:
    """A sealed value's associated data: `mendari-evalset-v1`, then its kind, its set, its case
    (empty for what belongs to the set) and, for a label only, the label, NUL-separated. Every
    id is a UUID, so none holds a NUL."""
    if kind not in SET_KINDS | CASE_KINDS:
        raise MendariError(f"{kind!r} is not a kind of sealed evaluation-set value")
    parts = [kind.encode(), _uuid(set_id, "set").encode()]
    if kind in SET_KINDS:
        if case_id or label_id:
            raise MendariError(f"a set's {kind} belongs to no case or label")
        parts.append(b"")
    else:
        parts.append(_uuid(case_id, "case").encode())
        if kind == "label":
            parts.append(_uuid(label_id, "label").encode())
        elif label_id:
            raise MendariError(f"a case's {kind} belongs to no label")
    return AAD + b"\x00".join(parts)


def _uuid(value: str, what: str) -> str:
    """`value` when it is a UUID as the store writes it (lowercase, hyphenated); a
    `MendariError` naming `what` otherwise, never the value."""
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise MendariError(f"a {what} id must be a UUID") from None
    if str(parsed) != value:
        raise MendariError(f"a {what} id must be a UUID, lowercase and hyphenated")
    return value


def evalset_keys() -> EvalsetKeys | None:
    """The deployment's evaluation-set keys, checked at startup, or None when both are unset
    (the feature is off)."""
    key = _key(KEY_ENV)
    index = _key(INDEX_ENV)
    listed = os.environ.get(READ_ENV, "").strip()
    if key is None and index is None:
        if listed:
            raise MendariError(f"{READ_ENV} is set without {KEY_ENV}")
        return None
    if key is None or index is None:
        raise MendariError(
            f"{KEY_ENV} and {INDEX_ENV} are set together or not at all: hidden evaluation sets "
            f"need both, two different keys of 32 random bytes, base64 ({GENERATE})"
        )
    if hmac.compare_digest(key, index):
        raise MendariError(f"{INDEX_ENV} must differ from {KEY_ENV}")
    others = _other_keys()
    for name, value in ((KEY_ENV, key), (INDEX_ENV, index)):
        for other_name, other in others:
            if hmac.compare_digest(value, other):
                raise MendariError(f"{name} must differ from {other_name}")
    read_keys = read_key_ring(
        READ_ENV,
        listed,
        (key, index, *(other for _, other in others)),
        "the current evaluation-set key, the index key or another of this deployment's keys",
    )
    return EvalsetKeys(key, index, read_keys)


def require_evalset_deployment(*, sign_in: bool) -> None:
    """Refuse, at startup, a server with the evaluation-set keys where hidden sets must not be
    served: without sign-in (open mode holds every permission), or on the hosted site outside
    Production (`VERCEL` set, `VERCEL_ENV` not `production`: a Preview runs on a copy of the
    database, which must hold only ciphertext; the root `app.py` sets `VERCEL` too, so serve
    locally with `mendari serve`)."""
    if not sign_in:
        raise MendariError(
            f"{KEY_ENV} and {INDEX_ENV} turn on hidden evaluation sets, which need sign-in: "
            "set it up (docs/operations/accounts.md), or unset both keys"
        )
    if os.environ.get("VERCEL") and os.environ.get("VERCEL_ENV", "").strip() != "production":
        raise MendariError(
            f"{KEY_ENV} and {INDEX_ENV} are for the hosted Production environment only: remove "
            "them from Preview and Development, then redeploy"
        )


def _key(name: str) -> bytes | None:
    """A 32-byte key from a base64 variable; None when unset, a `MendariError` when malformed
    (never a key read as unset)."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        key = b""
    if len(key) != 32:
        raise MendariError(f"{name} must be 32 random bytes, base64 ({GENERATE})")
    return key


def _other_keys() -> list[tuple[str, bytes]]:
    """The deployment's other keys as they are set, whatever the PHI mode: each named by its
    variable (an entry of a ring by its position). One that does not decode is left to its own
    check."""
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
