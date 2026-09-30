"""PHI mode: how a deployment keeps decision inputs ($OPENHEALTH_PHI_MODE).

- `off` (default): as before. A pack's `audit.store_input` decides whether the input is kept
  inside the decision record.
- `hash`: inputs are never stored, whatever the pack says. Only their hash is kept (every record
  has it).
- `encrypt`: inputs are kept apart from the chained record, encrypted with AES-256-GCM under
  `$OPENHEALTH_PHI_KEY` (32 bytes, base64) and bound to their decision id. Only roles that may
  read inputs get them back, and each read is written to the hash chain. Because the chained
  record never holds them, a retention policy can delete them (`openhealth phi purge`) without
  breaking the chain.
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

from openhealth.errors import OpenHealthError

PhiMode = Literal["off", "hash", "encrypt"]


@dataclass(frozen=True)
class PhiPolicy:
    mode: PhiMode = "off"
    key: bytes = field(default=b"", repr=False)

    def encrypt(self, decision_id: str, inputs: Any) -> str:
        nonce = secrets.token_bytes(12)
        plain = json.dumps(inputs, sort_keys=True, separators=(",", ":"), default=str).encode()
        sealed = AESGCM(self.key).encrypt(nonce, plain, decision_id.encode())
        return base64.b64encode(nonce + sealed).decode()

    def decrypt(self, decision_id: str, token: str) -> Any:
        raw = base64.b64decode(token)
        try:
            plain = AESGCM(self.key).decrypt(raw[:12], raw[12:], decision_id.encode())
        except InvalidTag:
            raise OpenHealthError("a stored input does not decrypt with this key") from None
        return json.loads(plain)


def phi_policy() -> PhiPolicy:
    mode = os.environ.get("OPENHEALTH_PHI_MODE", "").strip().lower() or "off"
    if mode not in ("off", "hash", "encrypt"):
        raise OpenHealthError("OPENHEALTH_PHI_MODE must be off, hash or encrypt")
    if mode != "encrypt":
        return PhiPolicy(mode=mode)  # type: ignore[arg-type]
    try:
        key = base64.b64decode(os.environ.get("OPENHEALTH_PHI_KEY", ""), validate=True)
    except (binascii.Error, ValueError):
        key = b""
    if len(key) != 32:
        raise OpenHealthError(
            "OPENHEALTH_PHI_MODE=encrypt needs OPENHEALTH_PHI_KEY: 32 random bytes, base64 "
            '(e.g. `python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`)'
        )
    return PhiPolicy(mode="encrypt", key=key)
