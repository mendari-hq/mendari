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
from typing import Any

import jwt

from openhealth.errors import AuthError
from openhealth.standards.fhir_client import GuardedHttp

ALGORITHMS = ["RS256", "ES384"]


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
    if config.get("issuer", issuer).rstrip("/") != issuer.rstrip("/"):
        raise AuthError("the identity provider's configuration names another issuer")
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
    keys = [k for k in http.get_json(jwks_uri).get("keys", []) if isinstance(k, dict)]
    matching = [k for k in keys if k.get("kid") == header.get("kid")] if header.get("kid") else keys
    if not matching:
        raise AuthError("no key in the issuer's JWKS matches the id_token")
    last: jwt.PyJWTError | None = None
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
        except jwt.PyJWTError as error:
            last = error
    else:
        raise AuthError(f"the id_token is not valid: {type(last).__name__}")
    if claims.get("nonce") != nonce:
        raise AuthError("the id_token's nonce does not match this sign-in")
    return claims
