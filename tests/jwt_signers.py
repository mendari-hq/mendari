# SPDX-License-Identifier: Apache-2.0
"""Signed JWTs for the tests, with keys each test module makes at run time (never a key in the
repository): a signer, a SMART Backend Services client assertion, a forged token of any header
and claims, and a CDS Hooks token as an EHR signs one (CDS Hooks 2.0, "Trusting CDS Clients").
Moved here from tests/test_backend_services.py (LP-23b), which imports them back unchanged."""

from __future__ import annotations

import base64
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization

PUBLIC_URL = "https://mendari.test"
TOKEN_URL = f"{PUBLIC_URL}/auth/token"
DROP = object()  # a claim left out of a token


@dataclass(frozen=True)
class Signer:
    alg: str
    kid: str
    private: Any

    @property
    def jwk(self) -> dict[str, Any]:
        public = self.private.public_key()
        family = jwt.algorithms.RSAAlgorithm if self.alg == "RS384" else jwt.algorithms.ECAlgorithm
        return {**json.loads(family.to_jwk(public)), "kid": self.kid}

    def pem(self) -> str:
        """As a site keeps it: PKCS#8 for RSA, SEC 1 for EC (what `bulk.backend_token` reads)."""
        layout = (
            serialization.PrivateFormat.PKCS8
            if self.alg == "RS384"
            else serialization.PrivateFormat.TraditionalOpenSSL
        )
        encoding = serialization.Encoding.PEM
        return self.private.private_bytes(encoding, layout, serialization.NoEncryption()).decode()


def assertion(
    signer: Signer,
    client_id: str,
    *,
    headers: dict[str, Any] | None = None,
    algorithm: str | None = None,
    lifetime: int = 120,
    **claims: Any,
) -> str:
    """A client assertion for Mendari's token endpoint: `iss` and `sub` the client, `aud` the
    endpoint, two minutes, a fresh `jti`; each claim overridden by `claims`, or left out with
    `DROP`."""
    now = int(time.time())
    body: dict[str, Any] = {"iss": client_id, "sub": client_id, "aud": TOKEN_URL}
    body |= {"exp": now + lifetime, "iat": now, "jti": uuid.uuid4().hex}
    body |= claims
    body = {name: value for name, value in body.items() if value is not DROP}
    return jwt.encode(
        body,
        signer.private,
        algorithm=algorithm or signer.alg,
        headers={"kid": signer.kid, **(headers or {})},
    )


def forged(header: dict[str, Any], claims: dict[str, Any], signature: bytes = b"sig") -> str:
    """A token of exactly this header and these claims, with a signature that is not one."""

    def part(value: Any) -> str:
        raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{part(header)}.{part(claims)}.{part(signature)}"


def cds_token(
    signer: Signer,
    iss: str,
    aud: str,
    *,
    jku: str | None = None,
    headers: dict[str, Any] | None = None,
    algorithm: str | None = None,
    lifetime: int = 300,
    **claims: Any,
) -> str:
    """A CDS Hooks token as an EHR (or Inferno's CRD kit) signs one: the header `alg`, `typ`
    (`JWT`), `kid` and, when given, `jku`; the claims `iss`, `aud` (the service's URL), `exp`
    (five minutes unless asked), `iat` and a fresh `jti`, each overridden by `claims`, or left
    out with `DROP`."""
    sent = {"typ": "JWT", **({"jku": jku} if jku is not None else {}), **(headers or {})}
    body = {"aud": aud, "sub": DROP, **claims}  # no `sub` unless asked: CDS Hooks has none
    return assertion(signer, iss, headers=sent, algorithm=algorithm, lifetime=lifetime, **body)
