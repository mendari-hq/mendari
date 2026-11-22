# SPDX-License-Identifier: Apache-2.0
"""`mendari smart check <FHIR base>`: before go-live, whether an EHR's launches will work here,
with this deployment's settings as the server would read them (ADR-0006).

It reports, in order:
1. the registration key: the base as `MENDARI_SMART_EHRS` keys it (normalized);
2. the allow-list: the base's origin on `MENDARI_FHIR_ALLOWED_SERVERS`;
3. the registration: the organization and level the base is registered to, whether people can
   sign in to that organization (without `MENDARI_OIDC_ORG_CLAIM` everyone is in `default`, so
   a launch registered elsewhere decides where no reviewer reads; in memberships mode,
   `MENDARI_ACCOUNTS=memberships`, people reach it by membership, and it needs a record, which
   this check does not read: `mendari access check`), and whether an unregistered base may
   launch (open mode with nothing registered);
4. the SMART client: `MENDARI_SMART_CLIENT_ID` set, and scopes the launch accepts (no
   `offline_access`; with sign-in on, `openid fhirUser`);
5. discovery: the EHR's `.well-known/smart-configuration`, its endpoints on the allow-list;
6. scopes (SMART on): each scope the launch asks for among the EHR's `scopes_supported`, in
   SMART v1 or v2 form (`patient/*.*` covers `patient/*.rs`, a type wildcard covers a type,
   and scopes published apart count together);
7. PKCE: `S256` among the methods the EHR names;
8. the JWKS: the keys that verify the id_token, at least one usable (RS256 or ES384).

Only the EHR's own servers are called, through the same allow-listed client as a launch (none
when the base is not allowed), and only for its public configuration and keys: no launch, no
token. Each finding is `ok`, `warn` (works, but check it), `unknown` (the EHR does not say, so
nothing is assumed: ask it) or `fail` (launches will be refused, or their decisions reach no
reviewer). Only `fail` makes the command exit 1, and `unknown` is never `ok`.

A scope the EHR does not publish is `fail` (LP-23 plan, decision 56), though SMART 2.2.0 says
`scopes_supported` need not be exhaustive, so an EHR may grant a scope it does not list: the
detail says so. Whether that should be `warn` instead is the owner's question.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mendari.core import accounts
from mendari.errors import FhirError, MendariError
from mendari.server import scopes
from mendari.server.auth import DEFAULT_ORG, auth_settings
from mendari.standards import oidc, smart
from mendari.standards.fhir_client import GuardedHttp, allowed_servers, origin_of


@dataclass(frozen=True)
class Finding:
    status: str  # "ok", "warn", "unknown" or "fail"
    check: str
    detail: str


Say = Callable[[str, str, str], None]  # (status, check, detail)


def check(base: str) -> list[Finding]:
    """What a launch from `base` (the `iss` the EHR sends) would meet here."""
    found: list[Finding] = []

    def say(status: str, what: str, detail: str) -> None:
        found.append(Finding(status, what, detail))

    key = smart.normalize_base(base)
    if key is None:
        say("fail", "registration key", "not an http(s) URL without credentials, query or fragment")
        return found
    say("ok", "registration key", key)
    try:
        allowed = allowed_servers()
        ehrs = smart.smart_ehrs()
        signing_in = auth_settings()
    except MendariError as error:  # the server would not start with these settings
        say("fail", "settings", str(error))
        return found
    origin = origin_of(key)
    if origin in allowed:
        say("ok", "allow-list", f"{origin} is on MENDARI_FHIR_ALLOWED_SERVERS")
    else:
        say("fail", "allow-list", f"add {origin} to MENDARI_FHIR_ALLOWED_SERVERS")
    signed_in = signing_in is not None
    memberships = signing_in is not None and signing_in.accounts == "memberships"
    registered = ehrs.get(key)
    org_claim = os.environ.get("MENDARI_OIDC_ORG_CLAIM", "").strip()
    if memberships and registered is not None:
        if not accounts.is_org_id(registered.org):
            say(
                "fail",
                "registration",
                f"organization {registered.org}: in memberships mode no record can have that id "
                "(lower-case letters, digits and hyphens), and launches of an organization "
                "without one are refused",
            )
        else:
            say(
                "warn",
                "registration",
                f"organization {registered.org}, level {registered.level}; in memberships mode "
                "its launches are refused until it has a record: `mendari access check`",
            )
        if org_claim:
            say("warn", "sign-in", "MENDARI_OIDC_ORG_CLAIM is ignored in memberships mode")
    elif registered is not None and registered.org != DEFAULT_ORG and not org_claim:
        # Sign-in puts everyone in `default` (server/auth.py): no reviewer could read these.
        say(
            "fail" if signed_in else "warn",
            "registration",
            f"organization {registered.org}, but without MENDARI_OIDC_ORG_CLAIM everyone who "
            f"signs in is in {DEFAULT_ORG}: no reviewer would see these launches' decisions. "
            f"Register it to {DEFAULT_ORG}, or set MENDARI_OIDC_ORG_CLAIM",
        )
    elif registered is not None:
        say("ok", "registration", f"organization {registered.org}, level {registered.level}")
    elif signed_in or ehrs:
        say("fail", "registration", f"add {key}=<organization>:<view|decide> to MENDARI_SMART_EHRS")
    else:
        say("warn", "registration", "not registered: open mode lets any allowed EHR launch")
    settings = smart.smart_settings()
    if settings is None:
        say("fail", "SMART client", "SMART launch is off: set MENDARI_SMART_CLIENT_ID")
    else:
        asked = set(settings.scopes.split())
        if "offline_access" in asked:
            say("fail", "SMART client", "MENDARI_SMART_SCOPES asks for offline_access")
        elif signed_in and not {"openid", "fhirUser"} <= asked:
            say("fail", "SMART client", "with sign-in on, the scopes must include openid fhirUser")
        else:
            say("ok", "SMART client", f"client {settings.client_id}, scopes {settings.scopes}")
    if origin not in allowed:
        return found  # nothing is sent to a server off the allow-list
    with GuardedHttp(allowed=allowed) as http:
        config = _discovery(key, http, say)
        if config is not None:
            if settings is not None:  # SMART on: there are scopes to compare
                _scopes(config, settings.scopes, say)
            _pkce(config, say)
            _jwks(config, http, say)
    return found


def _discovery(key: str, http: GuardedHttp, say: Say) -> dict[str, Any] | None:
    url = f"{key}/.well-known/smart-configuration"
    try:
        config = http.get_json(url)
    except FhirError as error:
        say("fail", "discovery", f"{url}: {error}")
        return None
    for name in ("authorization_endpoint", "token_endpoint"):
        endpoint = config.get(name)
        if not isinstance(endpoint, str):
            say("fail", "discovery", f"the SMART configuration has no {name}")
            return None
        if origin_of(endpoint) not in http.allowed:
            say("fail", "discovery", f"{name} is on {origin_of(endpoint)}: allow-list it")
            return None
    issuer = config.get("issuer") or key
    say("ok", "discovery", f"issuer {issuer}; authorize and token on the allow-list")
    return config


def _scopes(config: dict[str, Any], asked: str, say: Say) -> None:
    """Each scope a launch asks for (`MENDARI_SMART_SCOPES`) among those the EHR publishes. Only
    Mendari's own scopes are named, never the EHR's text. An empty list says nothing either."""
    wanted = list(dict.fromkeys(asked.split()))
    published = config.get("scopes_supported")
    if (
        not published
        or not isinstance(published, list)
        or not all(isinstance(s, str) for s in published)
    ):
        say(
            "unknown",
            "scopes",
            "the SMART configuration lists no scopes_supported, so nothing is assumed: ask the "
            f"EHR whether it grants {' '.join(wanted)}",
        )
        return
    missing = [scope for scope in wanted if not _published(scope, published)]
    if missing:
        say(
            "fail",
            "scopes",
            f"the EHR does not publish {' '.join(missing)}. Its scopes_supported need not list "
            "every scope it grants (SMART 2.2.0): ask the EHR, or change MENDARI_SMART_SCOPES",
        )
    else:
        say(
            "ok",
            "scopes",
            f"{' '.join(wanted)}: each among the {len(published)} the EHR publishes",
        )


def _published(asked: str, published: list[str]) -> bool:
    """Whether a scope a launch asks for is among an EHR's published scopes: the same scope, or
    the resource scopes of the same context (patient/, user/, system/) for the same type or `*`
    whose permissions together include every one it asks, SMART v1 or v2 either side
    (`patient/*.*`, or `patient/*.r` with `patient/*.s`, covers `patient/*.rs`); a granular
    scope (`?param=`) also by the same without its query."""
    if asked in published:
        return True
    wanted = scopes.resource_scope(asked)
    if wanted is None:
        return False
    offered: set[str] = set()
    for found in map(scopes.resource_scope, published):
        if (
            found is not None
            and found.context == wanted.context
            and found.type in ("*", wanted.type)
            and found.query in ("", wanted.query)
        ):
            offered |= found.letters
    return wanted.letters <= offered


def _pkce(config: dict[str, Any], say: Say) -> None:
    methods = config.get("code_challenge_methods_supported")
    if methods is None:
        say("warn", "PKCE S256", "not advertised: Mendari sends S256, the EHR must accept it")
    elif not isinstance(methods, list):
        # A string would answer `in` by substring ("S256x"), anything else not at all.
        say("fail", "PKCE S256", "code_challenge_methods_supported is not a list")
    elif "S256" in methods:
        say("ok", "PKCE S256", "supported")
    else:
        say("fail", "PKCE S256", f"not supported (only {methods})")


def _jwks(config: dict[str, Any], http: GuardedHttp, say: Say) -> None:
    uri = config.get("jwks_uri")
    if not isinstance(uri, str):
        say("fail", "JWKS", "the SMART configuration has no jwks_uri: no id_token can be verified")
        return
    try:
        listed = http.get_json(uri).get("keys", [])
        keys = [k for k in listed if isinstance(k, dict)] if isinstance(listed, list) else []
    except FhirError as error:
        say("fail", "JWKS", f"{uri}: {error}")
        return
    usable = [k for k in keys if _usable(k)]
    if usable:
        say("ok", "JWKS", f"{len(usable)} of {len(keys)} keys verify {'/'.join(oidc.ALGORITHMS)}")
    else:
        say("fail", "JWKS", f"none of {len(keys)} keys is RSA or EC P-384 for signing")


def _usable(jwk: dict[str, Any]) -> bool:
    """A key that can verify an RS256 or ES384 id_token (oidc.ALGORITHMS)."""
    if jwk.get("use", "sig") != "sig" or jwk.get("alg") not in (None, *oidc.ALGORITHMS):
        return False
    return jwk.get("kty") == "RSA" or (jwk.get("kty") == "EC" and jwk.get("crv") == "P-384")
