"""SMART Backend Services: Mendari's own token endpoint for machines (ADR-0004).

**Off unless a deployment turns it on** (`MENDARI_BACKEND_SERVICES=on`), and then only with
sign-in configured and `MENDARI_PUBLIC_URL` set; otherwise the server refuses to start. When on:

- **Clients** are registered by a signed-in person with the `keys` permission
  (`POST /api/auth/clients`; never by a key or a token): one organization, a machine's role
  (`viewer` or `service`), the scopes it may be granted (`server/scopes.py`, parsed strictly)
  and a public JWK Set by value. Mendari fetches nothing on a client's behalf. Registering and
  revoking are chained (`client:<id>`).
- **`POST /auth/token`** takes a `client_credentials` grant with a `private_key_jwt` client
  assertion (RFC 7523, SMART's client-confidential-asymmetric), checked in this order, each step
  before anything that costs more: the form; the assertion's header (`RS384` or `ES384`; only
  `alg`, `kid` and `typ`, and `typ` `JWT` if present); the client (`iss` = `sub`, a registered,
  unrevoked client: the one lookup an unsigned request can cause); exactly one registered key
  for the `kid` and algorithm; the signature; `aud` exactly `{MENDARI_PUBLIC_URL}/auth/token`;
  `exp` (required, at most five minutes ahead, 30 s of skew); `jti` (1 to 256 characters). Only
  then is the `jti` claimed, once per client, until the assertion expires. Every failure of the
  assertion is the same `invalid_client` (401); the reason is logged with the client's id only
  (and only once the client is known), never with the assertion or a library's message.
- **Access tokens** are opaque (`mat_…`), live five minutes, and are stored as SHA-256 with the
  client, the scope granted (what was asked, within what the client registered) and the expiry.
  Each request reads the client's role, scopes, organization and revocation afresh (one join in
  `AuditStore.access_token`), and acts in the client's organization.
- **Discovery** is `GET /fhir/.well-known/smart-configuration`, mounted only when on.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from mendari.core.audit import AuditStore
from mendari.errors import AuthError, ClientError, MendariError, ScopeError
from mendari.server import scopes
from mendari.server.auth import OPEN, PERMISSIONS, AuthSettings, Principal

logger = logging.getLogger("mendari.auth")

ALGORITHMS = {"RS384": "RSA", "ES384": "EC"}  # the signing algorithm, and its key type
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
TOKEN_PATH = "/auth/token"
DISCOVERY_PATH = "/fhir/.well-known/smart-configuration"
TOKEN_SECONDS = 300  # SMART's recommended lifetime; no refresh token
MAX_LIFETIME = 300  # an assertion's exp is at most five minutes ahead
SKEW = 30  # seconds of clock skew tolerated on exp, iat and nbf
MAX_FORM_BYTES = 16 * 1024
MAX_ASSERTION_BYTES = 8 * 1024
MAX_JTI = 256
MAX_KEYS = 10
_HEADER_FIELDS = frozenset({"alg", "kid", "typ"})  # jku, x5u, x5c, jwk, crit, … are refused
_CLIENT_ID = re.compile(r"[0-9a-f]{24}")  # as AuditStore.register_client makes them
_KID = re.compile(r"[\x21-\x7e]{1,256}")  # visible ASCII
_NO_STORE = {"cache-control": "no-store", "pragma": "no-cache"}
_FORM = "application/x-www-form-urlencoded"
# What a JWK may carry, by type; anything private (d, p, q, dp, dq, qi, oth) or symmetric (k) is
# refused by name, and other members are dropped, so only the public key is stored.
_PUBLIC_MEMBERS = {
    "RSA": ("kty", "kid", "alg", "use", "key_ops", "n", "e"),
    "EC": ("kty", "kid", "alg", "use", "key_ops", "crv", "x", "y"),
}
_PRIVATE_MEMBERS = ("d", "p", "q", "dp", "dq", "qi", "oth", "k")


@dataclass(frozen=True)
class BackendSettings:
    public_url: str  # MENDARI_PUBLIC_URL, without a trailing slash

    @property
    def token_url(self) -> str:
        """The token endpoint, which an assertion's `aud` must be exactly (never the Host
        header, which a caller chooses)."""
        return f"{self.public_url}{TOKEN_PATH}"


def backend_settings(sign_in: AuthSettings | None) -> BackendSettings | None:
    """From the environment; None unless `MENDARI_BACKEND_SERVICES=on`. On, it needs sign-in
    (in open mode a token would mean nothing) and `MENDARI_PUBLIC_URL` (the assertions'
    audience), or the server refuses to start."""
    mode = os.environ.get("MENDARI_BACKEND_SERVICES", "").strip().lower() or "off"
    if mode not in ("on", "off"):
        raise MendariError("MENDARI_BACKEND_SERVICES must be on or off")
    if mode == "off":
        return None
    if sign_in is None:
        raise MendariError(
            "MENDARI_BACKEND_SERVICES=on needs sign-in (MENDARI_OIDC_ISSUER and "
            "MENDARI_OIDC_CLIENT_ID): Mendari issues no tokens in open mode"
        )
    public = os.environ.get("MENDARI_PUBLIC_URL", "").strip().rstrip("/")
    if not public:
        raise MendariError(
            "MENDARI_BACKEND_SERVICES=on needs MENDARI_PUBLIC_URL: client assertions are "
            "addressed to its /auth/token"
        )
    parts = urlsplit(public)
    local = (parts.hostname or "") in ("localhost", "127.0.0.1")
    if (
        not parts.netloc
        or parts.query
        or parts.fragment
        or not (parts.scheme == "https" or (parts.scheme == "http" and local))
    ):
        raise MendariError("MENDARI_PUBLIC_URL must be an https URL without a query or fragment")
    return BackendSettings(public_url=public)


def smart_configuration(settings: BackendSettings) -> dict[str, Any]:
    """`.well-known/smart-configuration` (SMART App Launch 2.2): what is true here. No
    `issuer` or `jwks_uri` (only for sso-openid-connect) and no `authorization_endpoint`
    (there is no user or patient launch); `code_challenge_methods_supported` is required of
    every SMART server, though no flow here uses it."""
    return {
        "token_endpoint": settings.token_url,
        "grant_types_supported": ["client_credentials"],
        "token_endpoint_auth_methods_supported": ["private_key_jwt"],
        "token_endpoint_auth_signing_alg_values_supported": list(ALGORITHMS),
        "scopes_supported": scopes.supported(),
        "capabilities": ["client-confidential-asymmetric", "permission-v2", "permission-v1"],
        "code_challenge_methods_supported": ["S256"],
    }


# -- registering a client ---------------------------------------------------------------------


def check_jwks(jwks: Any) -> list[dict[str, Any]]:
    """A client's JWK Set, checked when it is registered: 1 to 10 keys with distinct `kid`s,
    public only (a private or symmetric key is refused, never stored), RSA of 2048 bits or more
    for RS384 or EC P-384 for ES384, for signatures. Returns the keys with their public members
    only. A `ClientError` (422) names the key that is refused, never its material."""
    keys = jwks.get("keys") if isinstance(jwks, Mapping) else None
    if not isinstance(keys, list) or not 1 <= len(keys) <= MAX_KEYS:
        raise ClientError(f"jwks must be a JWK Set with 1 to {MAX_KEYS} keys")
    checked: list[dict[str, Any]] = []
    for number, key in enumerate(keys, start=1):
        if not isinstance(key, Mapping):
            raise ClientError(f"key {number} is not a JWK")
        kid = key.get("kid")
        if not isinstance(kid, str) or not _KID.fullmatch(kid):
            raise ClientError(f"key {number} needs a kid (1 to 256 visible ASCII characters)")
        if any(member in key for member in _PRIVATE_MEMBERS) or key.get("kty") == "oct":
            raise ClientError(
                f"key {number} is private or symmetric: register public keys only (and treat "
                "the one sent as exposed)"
            )
        if kid in (k["kid"] for k in checked):
            raise ClientError(f"key {number}: another key has the same kid")
        kty = key.get("kty")
        if kty not in _PUBLIC_MEMBERS:
            raise ClientError(f"key {number}: kty must be RSA (RS384) or EC (ES384)")
        algorithm = "RS384" if kty == "RSA" else "ES384"
        if key.get("alg", algorithm) != algorithm:
            raise ClientError(f"key {number}: a {kty} key signs {algorithm} here")
        if key.get("use", "sig") != "sig":
            raise ClientError(f"key {number}: use must be sig")
        key_ops = key.get("key_ops", ["verify"])
        if not isinstance(key_ops, list) or not key_ops or set(key_ops) != {"verify"}:
            raise ClientError(f"key {number}: key_ops may only be verify")
        if kty == "EC" and key.get("crv") != "P-384":
            raise ClientError(f"key {number}: an EC key must be on P-384 (ES384)")
        public = {m: key[m] for m in _PUBLIC_MEMBERS[kty] if m in key}
        if not all(isinstance(public.get(m), str) for m in public if m != "key_ops"):
            raise ClientError(f"key {number}: its members must be strings")
        try:
            loaded = jwt.PyJWK(public, algorithm=algorithm).key
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, binascii.Error):
            raise ClientError(f"key {number} is not a valid {kty} public key") from None
        if kty == "RSA" and (
            not isinstance(loaded, rsa.RSAPublicKey) or not 2048 <= loaded.key_size <= 8192
        ):
            raise ClientError(f"key {number}: an RSA key needs 2048 to 8192 bits")
        if kty == "EC" and not (
            isinstance(loaded, ec.EllipticCurvePublicKey) and loaded.curve.name == "secp384r1"
        ):
            raise ClientError(f"key {number}: an EC key must be a P-384 public key")
        checked.append(public)
    return checked


def default_scope(role: str) -> str:
    """A client registered without scopes may be granted everything its role can use."""
    usable = [s for s in scopes.supported() if scopes.ACTIONS.get(s, "read") in PERMISSIONS[role]]
    return scopes.parse(" ".join(usable), permissions=PERMISSIONS[role]).scope


class ClientRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    # A machine's roles (core/audit.py MACHINE_ROLES): review and approval are a person's.
    role: Literal["viewer", "service"]
    scope: str | None = Field(
        default=None,
        max_length=1000,
        description="Space-separated scopes the client may be granted: SMART v2 reads "
        "(`system/Task.rs`, `system/*.rs`, …) and Mendari's decide and outcome URIs. Left out, "
        "everything its role can use.",
    )
    jwks: dict[str, Any] = Field(
        description='The client\'s public JWK Set, by value (`{"keys": [...]}`): RSA keys of '
        "2048 bits or more (RS384) or EC P-384 keys (ES384), each with a distinct `kid`. "
        "Private or symmetric keys are refused."
    )


# -- the token endpoint -----------------------------------------------------------------------


class OAuthError(Exception):
    """An OAuth error answer (RFC 6749 §5.2): one code, a fixed description, never the input."""

    def __init__(self, error: str, status: int = 400):
        self.error = error
        self.status = status
        super().__init__(error)


class _Refused(Exception):
    """An assertion that does not authenticate its client: `reason` (a fixed code) and the
    client's id once it is known, for the log; the answer is always `invalid_client`."""

    def __init__(self, reason: str, client_id: str | None = None):
        self.reason = reason
        self.client_id = client_id
        super().__init__(reason)


_DESCRIPTIONS = {
    "invalid_request": "the token request is malformed",
    "unsupported_grant_type": "only grant_type=client_credentials is supported",
    "invalid_client": "client authentication failed",
    "invalid_scope": "none of the requested scopes can be granted to this client",
}


def oauth_error(error: OAuthError) -> JSONResponse:
    body = {"error": error.error, "error_description": _DESCRIPTIONS[error.error]}
    return JSONResponse(body, error.status, headers=_NO_STORE)


def read_form(content_type: str, body: bytes) -> dict[str, str]:
    """The token request's form: urlencoded UTF-8, at most 16 KiB, no parameter twice
    (RFC 6749 §3.2), else `invalid_request`."""
    if content_type.split(";")[0].strip().lower() != _FORM or len(body) > MAX_FORM_BYTES:
        raise OAuthError("invalid_request")
    try:
        items = parse_qsl(body.decode("utf-8"), keep_blank_values=True, max_num_fields=20)
    except (UnicodeDecodeError, ValueError):
        raise OAuthError("invalid_request") from None
    names = [name for name, _ in items]
    if len(set(names)) != len(names):
        raise OAuthError("invalid_request")
    return dict(items)


def verify_assertion(
    assertion: str, *, audience: str, client_for: Callable[[str], dict[str, Any] | None]
) -> tuple[dict[str, Any], str, int]:
    """The client a `private_key_jwt` assertion authenticates, its `jti` and `exp`; `_Refused`
    otherwise. Nothing here writes, and the one lookup is the client named by `iss`."""
    if not 1 <= len(assertion) <= MAX_ASSERTION_BYTES:
        raise _Refused("size")
    parts = assertion.split(".")
    if len(parts) != 3:
        raise _Refused("format")
    header, unverified = _segment(parts[0]), _segment(parts[1])
    if header is None or unverified is None:
        raise _Refused("format")
    alg = header.get("alg")
    if not set(header) <= _HEADER_FIELDS or not isinstance(alg, str) or alg not in ALGORITHMS:
        raise _Refused("header")
    kid = header.get("kid")
    if header.get("typ", "JWT") != "JWT" or not isinstance(kid, str) or not _KID.fullmatch(kid):
        raise _Refused("header")
    issuer = unverified.get("iss")
    if not isinstance(issuer, str) or not _CLIENT_ID.fullmatch(issuer):
        raise _Refused("identity")
    if unverified.get("sub") != issuer:
        raise _Refused("identity")
    client = client_for(issuer)
    if client is None:
        raise _Refused("client")
    client_id = str(client["client_id"])
    matching = [k for k in client["jwks"] if k["kid"] == kid and k["kty"] == ALGORITHMS[alg]]
    if len(matching) != 1:
        raise _Refused("key", client_id)
    try:
        key = jwt.PyJWK(matching[0], algorithm=alg).key
        claims: dict[str, Any] = jwt.decode(
            assertion,
            key,
            algorithms=[alg],
            audience=audience,
            issuer=client_id,
            leeway=SKEW,
            options={"require": ["iss", "sub", "aud", "exp", "jti"], "strict_aud": True},
        )
    except jwt.InvalidSignatureError:
        raise _Refused("signature", client_id) from None
    except jwt.InvalidAudienceError:
        raise _Refused("audience", client_id) from None
    except jwt.ExpiredSignatureError:
        raise _Refused("expired", client_id) from None
    except jwt.ImmatureSignatureError:
        raise _Refused("not-yet-valid", client_id) from None
    except (jwt.PyJWTError, ValueError, TypeError):
        raise _Refused("claims", client_id) from None
    expires = claims["exp"]
    if not isinstance(expires, int | float) or expires > time.time() + MAX_LIFETIME + SKEW:
        raise _Refused("lifetime", client_id)
    jti = claims["jti"]
    if not isinstance(jti, str) or not 1 <= len(jti) <= MAX_JTI:
        raise _Refused("jti", client_id)
    return client, jti, int(expires)


def _segment(segment: str) -> dict[str, Any] | None:
    """A JWS segment as a JSON object (unverified), or None."""
    try:
        value = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def exchange(
    form: Mapping[str, str],
    *,
    has_authorization: bool,
    settings: BackendSettings,
    store: AuditStore,
) -> dict[str, Any]:
    """A token request, checked in order; the token response, or an `OAuthError`."""
    if has_authorization or "client_secret" in form:
        raise OAuthError("invalid_request")  # one client authentication method (RFC 6749 §2.3)
    grant_type = form.get("grant_type")
    if not grant_type:
        raise OAuthError("invalid_request")
    if grant_type != "client_credentials":
        raise OAuthError("unsupported_grant_type")
    if not form.get("scope", "").strip():
        raise OAuthError("invalid_request")  # SMART: the scope is required
    try:
        if form.get("client_assertion_type") != ASSERTION_TYPE:
            raise _Refused("method")
        assertion = form.get("client_assertion", "")
        client, jti, expires = verify_assertion(
            assertion, audience=settings.token_url, client_for=store.client
        )
        client_id = str(client["client_id"])
        if form.get("client_id", client_id) != client_id:
            raise _Refused("identity", client_id)
        # Only now, with the assertion verified, the one write it may cause before the token.
        until = max(expires + SKEW, int(time.time()) + 1)
        if not store.smart_claim("jti", f"{client_id}\n{jti}", until):
            raise _Refused("replayed", client_id)
        try:
            registered = scopes.parse(client["scope"], permissions=PERMISSIONS[client["role"]])
        except (ScopeError, KeyError):
            raise _Refused("registration", client_id) from None  # refuse, never widen
    except _Refused as refused:
        logger.info(
            "token refused: %s (client %s)", refused.reason, refused.client_id or "not known"
        )
        raise OAuthError("invalid_client", 401) from None
    granted = scopes.parse_lenient(form["scope"]).narrow(registered)
    if not granted.scope:
        logger.info("token refused: scope (client %s)", client_id)
        raise OAuthError("invalid_scope")
    token, _ = store.issue_token(client_id, granted.scope, TOKEN_SECONDS)
    logger.info("token issued (client %s)", client_id)
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": TOKEN_SECONDS,
        "scope": granted.scope,
    }


# -- routes -----------------------------------------------------------------------------------


def add_backend_routes(app: FastAPI, settings: BackendSettings, store: AuditStore) -> None:
    configuration = smart_configuration(settings)

    @app.post(
        TOKEN_PATH,
        tags=["Sign-in"],
        summary="Get an access token (SMART Backend Services)",
        openapi_extra={"requestBody": _token_form()},
    )
    async def token(request: Request) -> Response:
        """A five-minute access token (`Bearer mat_…`) for a registered client:
        `grant_type=client_credentials` with a `private_key_jwt` client assertion signed RS384
        or ES384 (RFC 7523), addressed (`aud`) to this endpoint's URL. `scope` names what the
        client asks for; it gets what it asked for within what it registered, and the answer's
        `scope` says what that is. Errors are OAuth's: `invalid_request`,
        `unsupported_grant_type` and `invalid_scope` (400), `invalid_client` (401) for any
        assertion that does not authenticate the client."""
        try:
            declared = request.headers.get("content-length", "0")
            if not declared.isdigit() or int(declared) > MAX_FORM_BYTES:
                raise OAuthError("invalid_request")
            form = read_form(request.headers.get("content-type", ""), await request.body())
            answer = await run_in_threadpool(
                exchange,
                form,
                has_authorization="authorization" in request.headers,
                settings=settings,
                store=store,
            )
        except OAuthError as refused:
            return oauth_error(refused)
        return JSONResponse(answer, headers=_NO_STORE)

    @app.get(DISCOVERY_PATH, tags=["FHIR and CDS Hooks"], summary="SMART configuration")
    def discovery() -> JSONResponse:
        """How a SMART Backend Services client gets a token here (SMART App Launch 2.2), as
        `application/json` whatever the `Accept` header asks."""
        return JSONResponse(configuration, media_type="application/json")

    @app.post(
        "/api/auth/clients",
        status_code=201,
        tags=["Sign-in"],
        summary="Register a SMART Backend Services client",
    )
    def register_client(request: Request, body: ClientRequest) -> dict[str, Any]:
        """A client for a machine, with one role (`viewer` or `service`), the scopes it may be
        granted (a scope Mendari does not grant, or one the role cannot use, is a `422` naming
        it; left out, everything the role can use) and its public JWK Set (RS384 or ES384
        keys). Mendari gives the `client_id`. The client signs its assertions with its own
        private key, which never reaches Mendari. Registering is recorded in the audit chain."""
        principal: Principal = getattr(request.state, "principal", OPEN)
        role_permissions = PERMISSIONS[body.role]
        scope = (
            default_scope(body.role)
            if body.scope is None
            else scopes.parse(body.scope, permissions=role_permissions).scope
        )
        keys = check_jwks(body.jwks)
        client = store.register_client(
            name=body.name,
            role=body.role,
            org=principal.org,
            created_by=principal.name,
            scopes=scope,
            jwks=keys,
        )
        return {**client, "token_endpoint": settings.token_url}

    @app.get("/api/auth/clients", tags=["Sign-in"], summary="List SMART Backend Services clients")
    def list_clients(request: Request) -> list[dict[str, Any]]:
        """Clients of this organization: name, role, scope, their keys' ids and thumbprints,
        who registered each and when, and when and by whom it was revoked."""
        principal: Principal = getattr(request.state, "principal", OPEN)
        return store.clients(None if principal.kind == "open" else principal.org)

    @app.delete(
        "/api/auth/clients/{client_id}",
        status_code=204,
        tags=["Sign-in"],
        summary="Revoke a SMART Backend Services client",
    )
    def revoke_client(request: Request, client_id: str) -> Response:
        """Revoke a client: it gets no new token, and the tokens it holds are refused from the
        next request. Revoking is recorded in the audit chain."""
        principal: Principal = getattr(request.state, "principal", OPEN)
        org = None if principal.kind == "open" else principal.org
        if not store.revoke_client(client_id, org, revoked_by=principal.name):
            raise AuthError("no such client", 404)
        return Response(status_code=204)


def _token_form() -> dict[str, Any]:
    fields = {
        "grant_type": "client_credentials",
        "client_assertion_type": ASSERTION_TYPE,
        "client_assertion": "the signed JWT (RS384 or ES384): iss and sub the client_id, aud "
        "this endpoint's URL, exp at most five minutes ahead, a jti used once",
        "scope": "space-separated, e.g. system/Task.rs",
    }
    return {
        "required": True,
        "content": {
            _FORM: {
                "schema": {
                    "type": "object",
                    "required": list(fields),
                    "properties": {
                        name: {"type": "string", "description": text}
                        for name, text in fields.items()
                    },
                }
            }
        },
    }
