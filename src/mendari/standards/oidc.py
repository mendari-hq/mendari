# SPDX-License-Identifier: Apache-2.0
"""OpenID Connect pieces shared by the organization login and the SMART launch: PKCE, discovery
and id_token verification.

`verify_id_token` accepts only asymmetric signatures (RS256, ES384) by a key from the issuer's
JWKS, and checks `iss`, `aud`, `exp` and the `nonce` this client sent (with 60 s leeway for
clocks). Every request goes through `GuardedHttp`, so the issuer and its JWKS must be allowed.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from typing import Any

import jwt

from mendari.errors import AuthError
from mendari.standards.fhir_client import GuardedHttp

ALGORITHMS = ["RS256", "ES384"]
MAX_CANDIDATE_KEYS = 16


def pkce() -> tuple[str, str]:
    """A PKCE verifier and its S256 challenge."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def discover(issuer: str, http: GuardedHttp) -> dict[str, Any]:
    """The issuer's OpenID configuration, with every endpoint on the allow-list."""
    config = http.get_json(f"{issuer.rstrip('/')}/.well-known/openid-configuration")
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not isinstance(config.get(key), str):
            raise AuthError(f"the identity provider's configuration has no {key}")
        http.check(config[key])
    named = config.get("issuer", issuer)
    if not isinstance(named, str) or named.rstrip("/") != issuer.rstrip("/"):
        raise AuthError("the identity provider's configuration names another issuer")
    return config


class Discovery:
    """`discover`, remembered per issuer for `ttl` seconds: every anonymous visit to the sign-in
    page would otherwise fetch the issuer's configuration. A cached configuration's endpoints
    are checked against each caller's allow-list again, and a failed fetch is not kept."""

    def __init__(self, ttl: float = 3600) -> None:
        self.ttl = ttl
        self._lock = threading.Lock()
        self._found: dict[str, tuple[float, dict[str, Any]]] = {}

    def get(self, issuer: str, http: GuardedHttp) -> dict[str, Any]:
        with self._lock:
            found = self._found.get(issuer)
        if found is not None and time.monotonic() - found[0] < self.ttl:
            for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                http.check(found[1][key])
            return found[1]
        config = discover(issuer, http)
        with self._lock:
            self._found[issuer] = (time.monotonic(), config)
        return config


def verify_id_token(
    id_token: str, *, issuer: str, audience: str, nonce: str, jwks_uri: str, http: GuardedHttp
) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError:
        raise AuthError("the id_token is not a JWT") from None
    if header.get("alg") not in ALGORITHMS:
        raise AuthError(f"the id_token is signed with {header.get('alg')!r}, not {ALGORITHMS}")
    raw_keys = http.get_json(jwks_uri).get("keys", [])
    keys = [k for k in raw_keys if isinstance(k, dict)] if isinstance(raw_keys, list) else []
    matching = [k for k in keys if k.get("kid") == header.get("kid")] if header.get("kid") else keys
    matching = matching[:MAX_CANDIDATE_KEYS]
    if not matching:
        raise AuthError("no key in the issuer's JWKS matches the id_token")
    last: Exception | None = None
    for jwk in matching:
        try:
            claims: dict[str, Any] = jwt.decode(
                id_token,
                jwt.PyJWK(jwk, algorithm=header["alg"]).key,
                algorithms=[header["alg"]],
                audience=audience,
                issuer=issuer,
                options={"require": ["exp", "iss", "aud"]},
                leeway=60,
            )
            break
        except (jwt.PyJWTError, ValueError, TypeError, KeyError) as error:
            last = error
    else:
        raise AuthError(f"the id_token is not valid: {type(last).__name__}")
    aud = claims.get("aud")
    if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != audience:
        raise AuthError("the id_token's azp does not match this client")
    if claims.get("nonce") != nonce:
        raise AuthError("the id_token's nonce does not match this sign-in")
    return claims
