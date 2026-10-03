"""SMART on FHIR EHR launch (App Launch 2.x): OAuth 2 authorization code with PKCE, and OpenID
Connect for who the user is.

1. `begin`: the EHR opens `/smart/launch?iss=<FHIR base>&launch=<opaque>`. The server's
   `.well-known/smart-configuration` names its authorization and token endpoints; the app keeps a
   random `state`, a PKCE verifier and a `nonce`, and sends the browser to `authorize`. Scopes
   with `offline_access` are refused here, before any request: a launch never outlives the
   EHR's token, so no refresh token is asked for or kept.
2. `complete`: the browser comes back to `/smart/callback?code=&state=`. The code is exchanged at
   the token endpoint (with the verifier), and the `id_token` is verified: signature against the
   server's JWKS (RS256 or ES384), `iss`, `aud` (this client), `exp` and the `nonce`. The access
   token must be printable ASCII without spaces (`%x21-7E`, so it is safe in a header) and at
   most 16 KiB; the refresh token, the id_token and the rest of the answer are dropped.

Every request goes through `GuardedHttp`, so the FHIR server and every endpoint it names must be
on the deployment's allow-list. Tokens stay on the server, in a session row sealed under the
browser's opaque session id (`core/session_seal.py`); the browser holds only that id. SMART is
off until `MENDARI_SMART_CLIENT_ID` is set.
"""

from __future__ import annotations

import base64
import os
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from mendari.errors import AuthError, FhirError, SmartError
from mendari.standards import oidc
from mendari.standards.fhir_client import GuardedHttp

DEFAULT_SCOPES = "launch openid fhirUser patient/*.rs"
PENDING_SECONDS = 600  # a launch must come back from the authorization server within this
MAX_TOKEN_BYTES = 16 * 1024
_TOKEN = re.compile(r"[\x21-\x7e]+")  # RFC 6749 VSCHAR without the space: safe in a header


@dataclass(frozen=True)
class SmartSettings:
    client_id: str
    client_secret: str | None
    scopes: str
    public_url: str | None  # where the EHR's browser reaches this app; else the request's URL


def smart_settings() -> SmartSettings | None:
    """From the environment; None (SMART off) unless MENDARI_SMART_CLIENT_ID is set."""
    client_id = os.environ.get("MENDARI_SMART_CLIENT_ID", "").strip()
    if not client_id:
        return None
    return SmartSettings(
        client_id=client_id,
        client_secret=os.environ.get("MENDARI_SMART_CLIENT_SECRET", "").strip() or None,
        scopes=os.environ.get("MENDARI_SMART_SCOPES", "").strip() or DEFAULT_SCOPES,
        public_url=os.environ.get("MENDARI_PUBLIC_URL", "").strip().rstrip("/") or None,
    )


def discover(iss: str, http: GuardedHttp) -> dict[str, Any]:
    """The server's SMART configuration, with the endpoints this flow needs."""
    config = http.get_json(f"{iss.rstrip('/')}/.well-known/smart-configuration")
    for key in ("authorization_endpoint", "token_endpoint"):
        if not isinstance(config.get(key), str):
            raise SmartError(f"the SMART configuration has no {key}", reason="configuration")
        http.check(config[key])  # every endpoint on the allow-list, not just the FHIR base
    methods = config.get("code_challenge_methods_supported")
    if methods is not None and "S256" not in methods:
        raise SmartError(
            "the authorization server does not support PKCE with S256", reason="configuration"
        )
    if config.get("jwks_uri"):
        http.check(config["jwks_uri"])
    return config


def begin(
    iss: str, launch: str, *, redirect_uri: str, settings: SmartSettings, http: GuardedHttp
) -> tuple[str, str, dict[str, Any]]:
    """The authorization URL to send the browser to, its `state`, and what to keep until it
    comes back (sealed in a row keyed by the `state`, which therefore is not in it)."""
    if "offline_access" in settings.scopes.split():
        raise SmartError(
            "MENDARI_SMART_SCOPES asks for offline_access: a launch keeps no refresh token",
            reason="configuration",
        )
    config = discover(iss, http)
    state = secrets.token_urlsafe(32)
    verifier, challenge = oidc.pkce()
    nonce = secrets.token_urlsafe(24)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.client_id,
            "redirect_uri": redirect_uri,
            "launch": launch,
            "scope": settings.scopes,
            "state": state,
            "aud": iss,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "nonce": nonce,
        }
    )
    endpoint = config["authorization_endpoint"]
    pending = {
        "iss": iss,
        "issuer": config.get("issuer") or iss,
        "token_endpoint": config["token_endpoint"],
        "jwks_uri": config.get("jwks_uri"),
        "verifier": verifier,
        "nonce": nonce,
        "redirect_uri": redirect_uri,
        "scopes": settings.scopes,
    }
    return f"{endpoint}{'&' if '?' in endpoint else '?'}{query}", state, pending


def complete(
    pending: Mapping[str, Any], code: str, *, settings: SmartSettings, http: GuardedHttp
) -> dict[str, Any]:
    """Exchange the code; return the session to keep (token, patient, encounter, user)."""
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pending["redirect_uri"],
        "code_verifier": pending["verifier"],
    }
    headers = {"accept": "application/json"}
    if settings.client_secret:
        pair = f"{settings.client_id}:{settings.client_secret}".encode()
        headers["authorization"] = f"Basic {base64.b64encode(pair).decode()}"
    else:
        form["client_id"] = settings.client_id
    try:
        token = http.post_form(pending["token_endpoint"], form, headers)
    except FhirError as error:
        raise SmartError(f"the token exchange failed: {error}", reason="token") from None
    if str(token.get("token_type", "")).lower() != "bearer" or not token.get("access_token"):
        raise SmartError("the token response has no Bearer access token", reason="token")
    access = token["access_token"]
    if not isinstance(access, str) or len(access) > MAX_TOKEN_BYTES or not _TOKEN.fullmatch(access):
        raise SmartError("the access token is not a valid OAuth token", reason="token")
    if not isinstance(token.get("patient"), str):
        raise SmartError("the EHR did not send a patient with the launch", reason="no_patient")
    user = None
    if "openid" in str(pending["scopes"]).split():
        if not token.get("id_token"):
            raise SmartError(
                "openid was requested but the token response has no id_token", reason="id_token"
            )
        claims = verify_id_token(str(token["id_token"]), pending, settings, http)
        user = claims.get("fhirUser") or claims.get("profile")
    expires_in = token.get("expires_in")
    lifetime = int(expires_in) if isinstance(expires_in, int | float) and expires_in > 0 else 3600
    return {
        "iss": pending["iss"],
        "access_token": access,
        "scope": token.get("scope") or pending["scopes"],
        "patient": token["patient"],
        "encounter": token.get("encounter") if isinstance(token.get("encounter"), str) else None,
        "fhir_user": user if isinstance(user, str) else None,
        "expires_at": int(time.time()) + min(lifetime, 12 * 3600),
    }


def verify_id_token(
    id_token: str, pending: Mapping[str, Any], settings: SmartSettings, http: GuardedHttp
) -> dict[str, Any]:
    if not pending.get("jwks_uri"):
        raise SmartError(
            "the SMART configuration has no jwks_uri to verify the id_token", reason="id_token"
        )
    try:
        return oidc.verify_id_token(
            id_token,
            issuer=pending["issuer"],
            audience=settings.client_id,
            nonce=pending["nonce"],
            jwks_uri=pending["jwks_uri"],
            http=http,
        )
    except AuthError as error:
        raise SmartError(
            str(error).replace("this sign-in", "this launch"), reason="id_token"
        ) from None
