# SPDX-License-Identifier: Apache-2.0
"""Records a site signs (LP-22e and 22f, ADR-0017), and the site's own files.

A site record is a JSON object signed with one of the site's Ed25519 keys (`hub/sites.py`):

    {"format": "mendari.site-ticket-request/1", "site": "<16 hex>",
     "key_id": "ed25519:<16 hex>", "nonce": "<32 hex>", "at": "2026-11-15T12:00:00Z",
     ...the format's own fields..., "signature": "<base64>"}

The signature is Ed25519 over `PREFIX`, the format and a zero byte, then the canonical JSON of
every other field (sorted keys, no spaces, ASCII). The prefix and the formats are a site's own:
a recall (`hub/yank.py`, bare canonical JSON), the registry's counter-signature and a
publisher's archive never sign these bytes, so none of their signatures is a site's; and as the
format is signed, a ticket request is never a submission (the rule of S19 and S20). A site key
is never a publisher's key either (`sites.add_site_key`). `nonce` is 16 random bytes; the
registry refuses one a site has used (LP-22e), and `at` within `MAX_SKEW_S` of its clock, as a
recall's time is checked.

Formats:
- `mendari.site-ticket-request/1` (22e): the agent (id, version, digest), the configuration
  (each slot's variant), `remote_consent`, the commitment and the dataset's size `n`;
- `mendari.site-ticket-withdrawal/1` (22e): the ticket given up;
- `mendari.site-submission/1` (22f): the ticket and the `mendari.site-report/1` payload.

The site's own files, in the client's configuration directory (`client.config_dir()`), written
private (0600) as credentials are: `site.json` (the site's id and its key's id),
`site_key.pem` (the private key) and `site_secret` (32 random bytes in base64, the commitment
secret). A key `mendari site key --new` replaces is kept beside them as `site_key.<hex>.pem`
(its key id's hex), never overwritten. None of them ever leaves the site.

The commitment to a dataset is HMAC-SHA256 of the dataset file's bytes under the commitment
secret: only the site can make it, the same file gives the same value, and nobody without the
secret can test a guessed dataset against it. The file's plain hash is never sent.
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
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from mendari.errors import HubError, MendariError
from mendari.hub import archive

PREFIX = b"mendari-site-record-v1\x00"
TICKET_REQUEST = "mendari.site-ticket-request/1"
TICKET_WITHDRAWAL = "mendari.site-ticket-withdrawal/1"
SUBMISSION = "mendari.site-submission/1"
FORMATS = (TICKET_REQUEST, TICKET_WITHDRAWAL, SUBMISSION)
COMMON = ("format", "site", "key_id", "nonce", "at")
MAX_SKEW_S = 600  # as a recall's (`yank.MAX_SKEW_S`)
SECRET_BYTES = 32

_SITE_ID = re.compile(r"^[0-9a-f]{16}\Z")
_KEY_ID = re.compile(r"^ed25519:[0-9a-f]{16}\Z")
_NONCE = re.compile(r"^[0-9a-f]{32}\Z")
_COMMITMENT = re.compile(r"^hmac-sha256:[0-9a-f]{64}\Z")

KEY_FILE_FORMAT = "mendari.site-key/1"
SITE_FILE = "site.json"
KEY_FILE = "site_key.pem"
SECRET_FILE = "site_secret"


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical(fields: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(fields), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()


def signed_bytes(fields: Mapping[str, Any]) -> bytes:
    """What a site's signature is over: the prefix, the format, a zero byte, and the canonical
    JSON of every field but the signature."""
    body = {key: value for key, value in fields.items() if key != "signature"}
    return PREFIX + str(body.get("format", "")).encode() + b"\x00" + canonical(body)


def sign(
    format: str,
    fields: Mapping[str, Any],
    *,
    site: str,
    key: Ed25519PrivateKey,
    at: str | None = None,
    nonce: str | None = None,
) -> dict[str, Any]:
    """A site record of `format`, signed with `key`: `fields` with the common fields added."""
    if format not in FORMATS:
        raise MendariError(f"not a site record format: {format}")
    if set(fields) & {*COMMON, "signature"}:
        raise MendariError("a record's own fields cannot be one of its common ones")
    record = {
        **fields,
        "format": format,
        "site": site,
        "key_id": archive.key_id(key.public_key()),
        "nonce": nonce or secrets.token_hex(16),
        "at": at or now(),
    }
    signature = base64.b64encode(key.sign(signed_bytes(record))).decode("ascii")
    return {**record, "signature": signature}


def at(record: Mapping[str, Any]) -> datetime | None:
    """The record's time, or None if it is not a UTC ISO time."""
    try:
        when = datetime.fromisoformat(str(record.get("at", "")).replace("Z", "+00:00"))
    except ValueError:
        return None
    offset = when.utcoffset()
    return when if offset is not None and offset.total_seconds() == 0 else None


def shape_problem(record: Any, format: str) -> str | None:
    """Why `record` is not a site record of `format` by its common fields (None: it is). Its
    own fields are the format's model's to check."""
    if not isinstance(record, Mapping):
        return "not a JSON object"
    if record.get("format") != format:
        return f"not a {format} record"
    if not all(isinstance(record.get(name), str) for name in (*COMMON, "signature")):
        return f"its fields include {', '.join(COMMON)} and signature, as text"
    if not _SITE_ID.match(record["site"]):
        return "its site is a site's id"
    if not _KEY_ID.match(record["key_id"]):
        return "its key_id is a key's id"
    if not _NONCE.match(record["nonce"]):
        return "its nonce is 16 random bytes in hex"
    if at(record) is None:
        return "its time is not a UTC time"
    return None


def signature_holds(record: Mapping[str, Any], public: Ed25519PublicKey) -> bool:
    """Whether the record's signature is `public`'s over the record's exact fields."""
    try:
        signature = base64.b64decode(str(record.get("signature", "")), validate=True)
        public.verify(signature, signed_bytes(record))
    except (binascii.Error, ValueError, InvalidSignature):
        return False
    return archive.key_id(public) == record.get("key_id")


def skewed(record: Mapping[str, Any], *, now_: datetime | None = None) -> bool:
    when = at(record)
    current = now_ or datetime.now(UTC)
    return when is None or abs((current - when).total_seconds()) > MAX_SKEW_S


# -- the site's own files ------------------------------------------------------------------------


def site_dir() -> Path:
    from mendari.hub.client import config_dir

    return config_dir()


def _write_private_bytes(path: Path, data: bytes) -> None:
    """Written whole beside the file (mode 0600), then moved in, as credentials are."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, staged = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")  # 0600
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.chmod(staged, 0o600)
        os.replace(staged, path)
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise


def retired_key_path(where: Path, key_id: str) -> Path:
    """Where a key `mendari site key --new` replaced is kept: named by its own key id, so a
    second new key never writes over the first one's predecessor."""
    return where / f"site_key.{key_id.removeprefix('ed25519:')}.pem"


def init_site(
    site_id: str, *, directory: Path | None = None, new_key: bool = False
) -> dict[str, Any]:
    """Make this machine a site's (`mendari site init`): its id (from the registry, after it was
    registered), a new private key and a new commitment secret, each written private. With
    `new_key` (`mendari site key --new`) only the key is made again; the id and secret stay.
    Returns the key file the registry takes (`mendari registry site-key --key`, or the
    dashboard): the site, the key's id, its public key and its proof of possession."""
    from mendari.hub.sites import key_proof_bytes

    if not _SITE_ID.match(site_id):
        raise MendariError("a site's id is 16 hex characters (the registry gives it)")
    where = directory or site_dir()
    secret_path = where / SECRET_FILE
    if not new_key:
        if (where / SITE_FILE).exists():
            raise MendariError(
                f"{where / SITE_FILE} is there already: this machine is a site "
                "(`mendari site key --new` makes a new key)"
            )
        _write_private_bytes(secret_path, base64.b64encode(secrets.token_bytes(SECRET_BYTES)))
    elif not (where / SITE_FILE).exists():
        raise MendariError("this machine is not a site yet: run `mendari site init` first")
    if new_key and (where / KEY_FILE).exists():  # kept: never destroy a key
        old = (where / KEY_FILE).read_bytes()
        try:
            old_id = archive.key_id(archive.load_private_key(old).public_key())
        except (ValueError, HubError):
            raise MendariError(
                f"{where / KEY_FILE} is not a key: move it aside before making a new one"
            ) from None
        kept = retired_key_path(where, old_id)
        if not kept.exists():
            _write_private_bytes(kept, old)
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    _write_private_bytes(where / KEY_FILE, pem)
    key_id = archive.key_id(private.public_key())
    _write_private_bytes(
        where / SITE_FILE, json.dumps({"site": site_id, "key_id": key_id}, indent=2).encode()
    )
    proof = base64.b64encode(private.sign(key_proof_bytes(site_id, key_id))).decode("ascii")
    return {
        "format": KEY_FILE_FORMAT,
        "site": site_id,
        "key_id": key_id,
        "public_pem": archive.public_pem(private.public_key()).decode(),
        "proof": proof,
    }


class Site:
    """This machine's site: its id, its key and its commitment secret, read from its files."""

    def __init__(self, directory: Path | None = None):
        where = directory or site_dir()
        try:
            meta = json.loads((where / SITE_FILE).read_text(encoding="utf-8"))
            self.id = str(meta["site"])
            self.key = archive.load_private_key((where / KEY_FILE).read_bytes())
            self._secret = base64.b64decode((where / SECRET_FILE).read_bytes(), validate=True)
        except (OSError, ValueError, KeyError, TypeError, binascii.Error, HubError):
            raise MendariError(
                f"this machine is not a site ({where}): run `mendari site init --site ID` with "
                "the id the registry gave the site"
            ) from None
        if not _SITE_ID.match(self.id) or len(self._secret) != SECRET_BYTES:
            raise MendariError(f"the site's files in {where} are not a site's")
        self.key_id = archive.key_id(self.key.public_key())

    def commitment(self, data: bytes) -> str:
        """HMAC-SHA256 of a dataset file's bytes under this site's commitment secret."""
        return "hmac-sha256:" + hmac.new(self._secret, data, hashlib.sha256).hexdigest()

    def sign(self, format: str, fields: Mapping[str, Any], **options: Any) -> dict[str, Any]:
        return sign(format, fields, site=self.id, key=self.key, **options)


def is_commitment(value: Any) -> bool:
    return isinstance(value, str) and _COMMITMENT.match(value) is not None
