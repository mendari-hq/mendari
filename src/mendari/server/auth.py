"""Sign-in for organizations: OpenID Connect with the organization's identity provider, roles,
and API keys for machines.

**Off until configured.** Without `MENDARI_OIDC_ISSUER` and `MENDARI_OIDC_CLIENT_ID` the
server is open, as before (production sits behind Vercel Authentication). When configured:

- **People** sign in at `/auth/login`: authorization code with PKCE, and the id_token verified
  with the issuer's JWKS (`standards/oidc.py`). The session is server-side, behind an opaque
  HttpOnly, SameSite=Lax cookie. Roles come from a claim (`MENDARI_OIDC_ROLE_CLAIM`, default
  `roles`), optionally renamed by `MENDARI_OIDC_ROLE_MAP` ("clinicians=reviewer,it=admin").
  With an identity provider that has no roles (Google, say), the claim can be `email`, mapped
  person by person ("owner@example.org=admin"); an email gives a role only when the provider
  says it is verified (`email_verified`).
  The organization comes from `MENDARI_OIDC_ORG_CLAIM` (default: one organization).
- **The identity provider's endpoints** must be on the issuer's origin, or listed in
  `MENDARI_OIDC_ENDPOINT_ORIGINS` (Google's token and key endpoints are on other hosts).
- **Machines** send `Authorization: Bearer ohk_...`: API keys that admins create, which are
  shown once, stored as SHA-256, carry a machine's role (viewer or service), optionally scopes
  that narrow it (`server/scopes.py`), and an expiry (90 days unless asked, 365 at most), and
  can be revoked. A scoped key reaches only the routes its scopes name.
- **Machines may also use SMART Backend Services** when a deployment turns it on
  (`server/backend_services.py`): a registered client exchanges a signed assertion for a
  five-minute access token (`Bearer mat_...`). Each request reads the client's current role,
  scopes, organization and revocation; a token reaches only the routes its scopes name.
- **An `Authorization` header decides alone** (ADR-0004): with one there is no fallback to the
  session cookie. The scheme is `Bearer` (any case), and the credential's prefix says what it is:
  `ohk_` a key, `mat_` an access token (refused unless Backend Services is on); anything else
  is refused. A credential is never accepted in a URL or a form (`access_token`).
- **Every route has a permission**, and anything not listed needs `admin` (deny by default).
  Managing keys and clients (`keys`) is for people who signed in, never for a key or token,
  whatever its role: a stolen credential must not mint one that outlives it.

| Role | May |
|---|---|
| viewer (people and machines) | read |
| reviewer | read (inputs too, under PHI mode), decide, review, record outcomes, give feedback |
| service (machines) | read, decide, record outcomes |
| governance | read, approve automation |
| admin | everything, including evaluations, exports and (people only) API keys |
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote, urlencode, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, Field

from mendari.core.audit import KEY_DAYS, KEY_MAX_DAYS, MACHINE_ROLES, AuditStore
from mendari.errors import AuthError, FhirError, MendariError, ScopeError
from mendari.server import scopes
from mendari.server.scopes import Grant
from mendari.standards import oidc
from mendari.standards.fhir_client import GuardedHttp, allowed_servers

COOKIE = "mendari_session"
REALM = "mendari"
KEY_PREFIX = "ohk_"
TOKEN_PREFIX = "mat_"  # SMART Backend Services access tokens (ADR-0004)
ROLES = ("viewer", "reviewer", "service", "governance", "admin")
PERMISSIONS: dict[str, frozenset[str]] = {
    "viewer": frozenset({"read"}),
    "reviewer": frozenset({"read", "read_input", "decide", "review", "outcome"}),
    "service": frozenset({"read", "decide", "outcome"}),
    "governance": frozenset({"read", "approve"}),
    "admin": frozenset(
        {
            "read",
            "read_input",
            "decide",
            "review",
            "outcome",
            "approve",
            "evaluate",
            "admin",
            "keys",
        }
    ),
}
# Permissions only a person who signed in holds, whatever a key's (or a token's) role says.
PEOPLE_ONLY = frozenset({"keys"})
# One refusal for a scoped credential outside its scopes (the challenge names the scope needed).
OUT_OF_SCOPE = "this credential's scopes do not cover this request"
DEFAULT_ORG = "default"
SESSION_SECONDS = 8 * 3600

# (method, path pattern) -> permission, first match wins; unlisted routes need "admin".
_RULES: list[tuple[str, re.Pattern[str], str]] = [
    (method, re.compile(pattern), permission)
    for method, pattern, permission in [
        ("GET", r"^/api/health$", "public"),
        ("GET", r"^/api/auth/me$", "public"),
        ("*", r"^/auth/", "public"),
        ("*", r"^/(smart|api/smart)/", "public"),  # SMART launches carry their own session
        ("GET", r"^/(static/.*|favicon\.ico)$", "public"),
        ("GET", r"^/$", "page"),
        ("GET", r"^/(docs|redoc)$", "page"),  # a signed-out browser is sent to sign in
        ("GET", r"^/openapi\.json$", "read"),
        ("POST", r"^/api/packs/[^/]+/[^/]+/(decide|guard|fhir/decide|pas)$", "decide"),
        ("POST", r"^/cds-services/[^/]+$", "decide"),
        ("POST", r"^/fhir/Questionnaire/\$questionnaire-package$", "decide"),  # Da Vinci DTR
        ("POST", r"^/fhir/\$decide$", "decide"),
        # How to talk to this FHIR base: public, so clients can discover it. The SMART
        # configuration is mounted only when Backend Services is on (server/backend_services.py).
        ("GET", r"^/fhir/(metadata|OperationDefinition/[^/]+)$", "public"),
        ("GET", r"^/fhir/\.well-known/smart-configuration$", "public"),
        # The terminology carries the packs' labels and criteria: read, like /api/packs.
        ("GET", r"^/fhir/(CodeSystem|NamingSystem)/[^/]+$", "read"),
        # Decisions and their audit trail: read, like /api/audit, and never public: without a
        # signed-in organization the store's scope is every organization's rows.
        ("GET", r"^/fhir/Task/[^/]+(/_history/[^/]+)?$", "read"),
        ("GET", r"^/fhir/(AuditEvent|Provenance)/[^/]+$", "read"),
        # Search, also as POST (a form body keeps the criteria out of URLs): a read.
        ("GET", r"^/fhir/(Task|AuditEvent|Provenance|CodeSystem)$", "read"),
        ("POST", r"^/fhir/(Task|AuditEvent|Provenance|CodeSystem)/_search$", "read"),
        ("GET", r"^/cds-services$", "read"),
        ("POST", r"^/api/packs/[^/]+/[^/]+/evals$", "evaluate"),
        ("POST", r"^/api/review/[^/]+$", "review"),
        ("POST", r"^/api/audit/[^/]+/feedback$", "review"),
        ("POST", r"^/api/audit/[^/]+/outcome$", "outcome"),
        ("GET", r"^/api/packs/[^/]+/[^/]+/feedback/export$", "admin"),  # case inputs, in bulk
        ("*", r"^/api/auth/(keys|clients)", "keys"),
        # The whole chain, read or exported: administrators only.
        ("GET", r"^/api/audit/(verify|export/fhir)$", "admin"),
        ("POST", r"^/api/governance$", "approve"),
        ("DELETE", r"^/api/governance/[^/]+$", "approve"),
        ("GET", r"^/api/", "read"),
    ]
]


@dataclass(frozen=True)
class AuthSettings:
    issuer: str
    client_id: str
    client_secret: str | None
    role_claim: str
    role_map: dict[str, str]
    org_claim: str | None
    public_url: str | None
    endpoint_origins: frozenset[str] = frozenset()
    scopes: str = "openid profile email"


def auth_settings() -> AuthSettings | None:
    """From the environment; None (the server stays open) unless issuer and client id are set."""
    issuer = os.environ.get("MENDARI_OIDC_ISSUER", "").strip().rstrip("/")
    client_id = os.environ.get("MENDARI_OIDC_CLIENT_ID", "").strip()
    if not issuer and not client_id:
        return None
    if not issuer or not client_id:
        raise MendariError("set both MENDARI_OIDC_ISSUER and MENDARI_OIDC_CLIENT_ID")
    parts = urlsplit(issuer)
    host = parts.hostname or ""
    local = host in ("localhost", "127.0.0.1") or "." not in host  # a compose service, say
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise MendariError("MENDARI_OIDC_ISSUER must use https")
    role_map = {}
    for pair in os.environ.get("MENDARI_OIDC_ROLE_MAP", "").split(","):
        if pair.strip():
            source, _, role = pair.partition("=")
            if role.strip() not in ROLES:
                raise MendariError(f"MENDARI_OIDC_ROLE_MAP: {role.strip()!r} is not a role")
            role_map[source.strip()] = role.strip()
    return AuthSettings(
        issuer=issuer,
        client_id=client_id,
        client_secret=os.environ.get("MENDARI_OIDC_CLIENT_SECRET", "").strip() or None,
        role_claim=os.environ.get("MENDARI_OIDC_ROLE_CLAIM", "").strip() or "roles",
        role_map=role_map,
        org_claim=os.environ.get("MENDARI_OIDC_ORG_CLAIM", "").strip() or None,
        public_url=os.environ.get("MENDARI_PUBLIC_URL", "").strip().rstrip("/") or None,
        endpoint_origins=allowed_servers("MENDARI_OIDC_ENDPOINT_ORIGINS"),
    )


@dataclass(frozen=True)
class Principal:
    kind: str  # "open", "user", "key" or "client" (an access token's)
    name: str
    # Stable and opaque, for counting and logging per principal: `user:<16 hex>` (person_id),
    # `key:<key id>`, `client:<client id>` or `open`. Never a secret, a name or an email.
    id: str = field(kw_only=True)
    roles: frozenset[str] = field(default_factory=frozenset)
    org: str = DEFAULT_ORG
    grant: Grant | None = None  # a credential's scopes; None: the role's permissions, unnarrowed
    expires_at: str | None = None  # a key's or an access token's expiry

    def may(self, permission: str) -> bool:
        if self.kind == "open":
            return True
        return permission in self.permissions()

    def role_permissions(self) -> frozenset[str]:
        """What the roles grant; for a key (or a token), never a people-only permission."""
        granted = frozenset().union(*(PERMISSIONS.get(role, frozenset()) for role in self.roles))
        return granted if self.kind in ("user", "open") else granted - PEOPLE_ONLY

    def permissions(self) -> frozenset[str]:
        """The effective permissions: the role's, narrowed by the scopes (never widened)."""
        granted = self.role_permissions()
        return granted if self.grant is None else granted & self.grant.permissions

    def to_dict(self) -> dict[str, Any]:
        shown: dict[str, Any] = {
            "kind": self.kind,
            "name": self.name,
            "roles": sorted(self.roles),
            "permissions": sorted(self.permissions()),
            "org": self.org,
        }
        if self.kind in ("key", "client"):
            scope = None if self.grant is None else self.grant.scope
            shown |= {"scope": scope, "expires_at": self.expires_at}
        return shown


OPEN = Principal(kind="open", name="open mode", roles=frozenset({"admin"}), id="open")


def person_id(issuer: str, subject: str) -> str:
    """A signed-in person's `Principal.id`: their subject at their identity provider, which is
    stable and unique only together with the issuer, hashed so it names nobody."""
    digest = hashlib.sha256(f"{issuer}|{subject}".encode()).hexdigest()
    return f"user:{digest[:16]}"


# RFC 6750's b64token, at a length no credential Mendari issues comes near.
_BEARER = re.compile(r"[A-Za-z0-9\-._~+/]{1,512}={0,2}")


def challenge(error: str | None = None, *, scope: str | None = None) -> str:
    """A `WWW-Authenticate` value (RFC 6750): the realm, and the error when a credential was
    sent (`invalid_token`), was not enough (`insufficient_scope`, with the `scope` that would
    be, when one would) or was sent wrongly (`invalid_request`)."""
    value = f'Bearer realm="{REALM}"' + (f', error="{error}"' if error else "")
    return value + (f', scope="{scope}"' if scope else "")


def invalid_token() -> AuthError:
    """One answer for every credential that is not valid: unknown, revoked, expired, malformed or
    of a kind not accepted here. It never says which, nor echoes the credential."""
    return AuthError("the credential is not valid", 401, challenge=challenge("invalid_token"))


def credential_in_url_or_form() -> AuthError:
    """RFC 6750 lets a client send its credential as an `access_token` query or form parameter;
    Mendari reads neither, and refuses both: they end up in logs and histories."""
    return AuthError(
        "send credentials in the Authorization header, never in a URL or a form",
        400,
        challenge=challenge("invalid_request"),
    )


def names_a_credential(names: Iterable[str]) -> bool:
    """Whether query or form parameter names include `access_token`, in any case."""
    return any(name.strip().lower() == "access_token" for name in names)


async def read_body(request: Request, limit: int) -> bytes | None:
    """A request body of at most `limit` bytes, else None: a declared length over the limit is
    refused unread, and a body without one (chunked) is read only until it passes the limit.
    Only routes read bodies, after the sign-in check, so a caller without a credential never
    makes Mendari read one."""
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > limit):
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return None
    return bytes(body)


def permission_for(method: str, path: str) -> str:
    for rule_method, pattern, permission in _RULES:
        if rule_method in ("*", method) and pattern.match(path):
            return permission
    return "admin"


def roles_from(claims: Mapping[str, Any], settings: AuthSettings) -> frozenset[str]:
    if settings.role_claim == "email" and claims.get("email_verified") is not True:
        return frozenset()  # anyone can claim an address they have not proven
    raw = claims.get(settings.role_claim) or []
    values = [raw] if isinstance(raw, str) else [str(v) for v in raw if isinstance(v, str | int)]
    mapped = {settings.role_map.get(v, v) for v in values}
    return frozenset(role for role in mapped if role in ROLES)


class KeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    # A machine's roles (core/audit.py MACHINE_ROLES): review and approval are a person's.
    role: Literal["viewer", "service"]
    scope: str | None = Field(
        default=None,
        max_length=1000,
        description="Space-separated scopes that narrow the role: SMART v2 reads "
        "(`system/Task.rs`, `system/*.rs`, …) and Mendari's decide and outcome URIs. Left out, "
        "the key has its role's permissions.",
    )
    days: int = Field(default=KEY_DAYS, ge=1, le=KEY_MAX_DAYS, description="Days until it expires.")


class Auth:
    """Resolves the principal of a request and guards every route."""

    def __init__(self, settings: AuthSettings | None, store: AuditStore, *, tokens: bool = False):
        self.settings = settings
        self.store = store
        self.tokens = tokens  # SMART Backend Services is on: access tokens are looked up
        self.discovery = oidc.Discovery()  # the identity provider's configuration, for an hour

    @property
    def enabled(self) -> bool:
        return self.settings is not None

    def principal(self, request: Request) -> Principal | None:
        """Who sends this request; None for no one, or for a credential that is not valid."""
        try:
            return self.authenticate(request)
        except AuthError:
            return None

    def authenticate(self, request: Request) -> Principal | None:
        """The request's principal; None when it carries no credential at all. An
        `Authorization` header decides alone: a credential in it that is not valid is an
        `AuthError` (401), never a fall back to the session cookie."""
        if self.settings is None:
            return OPEN
        headers = request.headers.getlist("authorization")
        if headers:
            return self._bearer(headers)
        sid = request.cookies.get(COOKIE)
        session = self.store.smart_get(sid, "user") if sid else None
        if session is None:
            return None
        roles = frozenset(session["roles"])
        # Sessions from before the issuer was stored are of the issuer configured now.
        issuer = str(session.get("iss") or self.settings.issuer)
        return Principal(
            kind="user",
            name=session["name"],
            roles=roles,
            org=session["org"],
            id=person_id(issuer, str(session["sub"])),
        )

    def _bearer(self, headers: list[str]) -> Principal:
        """The principal an `Authorization` header names, by the credential's prefix."""
        if len(headers) != 1:
            raise AuthError(
                "send one Authorization header", 400, challenge=challenge("invalid_request")
            )
        scheme, _, credential = headers[0].strip().partition(" ")
        if scheme.lower() != "bearer":
            # Not a bearer credential: RFC 6750 gives no error code to an unsupported scheme.
            raise AuthError("send Authorization: Bearer <credential>", 401, challenge=challenge())
        credential = credential.strip()
        if not _BEARER.fullmatch(credential):
            raise invalid_token()
        if credential.startswith(KEY_PREFIX):
            key = self.store.api_key(credential)  # None too when revoked or expired
            if key is None:
                raise invalid_token()
            try:
                grant = None if key["scope"] is None else scopes.parse(key["scope"])
            except ScopeError:
                raise invalid_token() from None  # a scope no longer granted: refuse, never widen
            return Principal(
                kind="key",
                name=f"key {key['name']}",
                roles=frozenset({key["role"]}),
                org=key["org"],
                grant=grant,
                expires_at=key["expires_at"],
                id=f"key:{key['id']}",
            )
        if credential.startswith(TOKEN_PREFIX):
            return self._token(credential)
        raise invalid_token()  # no credential Mendari issues

    def _token(self, credential: str) -> Principal:
        """An access token's client as it is now: its role, organization and scopes (the scope
        granted, within what the client registers today); refused once the client is revoked,
        and always when Backend Services is off."""
        token = self.store.access_token(credential) if self.tokens else None
        if token is None or token["role"] not in MACHINE_ROLES:
            raise invalid_token()
        try:
            registered = scopes.parse(token["registered"], permissions=PERMISSIONS[token["role"]])
        except ScopeError:
            raise invalid_token() from None  # a registration no longer granted: refuse
        expires = datetime.fromtimestamp(token["expires_at"], UTC)
        return Principal(
            kind="client",
            name=f"client {token['client_id']}",
            roles=frozenset({token["role"]}),
            org=token["org"],
            grant=scopes.parse_lenient(token["scope"]).narrow(registered),
            expires_at=expires.isoformat(timespec="milliseconds"),
            id=f"client:{token['client_id']}",
        )

    def check(self, request: Request) -> Response | None:
        """None when the request may go on; otherwise the response that refuses it."""
        permission = permission_for(request.method, request.url.path)
        if permission == "public" or self.settings is None:
            return None
        principal = self.authenticate(request)
        if permission == "page":
            if principal is None:
                target = f"/auth/login?next={quote(request.url.path)}"
                return RedirectResponse(target, status_code=302)
            _within_scope(principal, request.method, request.url.path)
            return None
        if principal is None:
            raise AuthError("sign in first, or send an API key", 401, challenge=challenge())
        if permission not in principal.role_permissions():
            refused = challenge("insufficient_scope")
            if permission in PEOPLE_ONLY and principal.kind != "user":
                raise AuthError("only a person who signed in may do this", 403, challenge=refused)
            raise AuthError(f"your role may not {permission} here", 403, challenge=refused)
        _within_scope(principal, request.method, request.url.path)
        if not principal.may(permission):  # implied by the scope check: fail closed if not
            raise AuthError(OUT_OF_SCOPE, 403, challenge=challenge("insufficient_scope"))
        request.state.principal = principal
        return None


def _within_scope(principal: Principal, method: str, path: str) -> None:
    """A scoped principal reaches only the routes its scopes name (server/scopes.py)."""
    if principal.grant is None:
        return
    needed = scopes.needed(method, path)
    if needed is None or not principal.grant.covers(needed):
        refused = challenge("insufficient_scope", scope=needed)
        raise AuthError(OUT_OF_SCOPE, 403, challenge=refused)


def add_auth_routes(app: FastAPI, auth: Auth) -> None:
    settings = auth.settings
    store = auth.store

    def configured() -> AuthSettings:
        if settings is None:
            raise AuthError("sign-in is not configured here (MENDARI_OIDC_ISSUER)", 404)
        return settings

    def http_for(options: AuthSettings) -> GuardedHttp:
        issuer_origin = f"{urlsplit(options.issuer).scheme}://{urlsplit(options.issuer).netloc}"
        return GuardedHttp(allowed={*allowed_servers(), issuer_origin, *options.endpoint_origins})

    @app.get("/auth/login", include_in_schema=False)
    def login(request: Request, next: str = "/") -> Response:
        options = configured()
        target = next if next.startswith("/") and not next.startswith("//") else "/"
        with http_for(options) as http:
            try:
                config = auth.discovery.get(options.issuer, http)
            except FhirError as failure:  # the guarded client's errors (unreachable, not allowed)
                raise AuthError(
                    f"the identity provider cannot be used ({failure}); its endpoints must be on "
                    "the issuer's origin or listed in MENDARI_OIDC_ENDPOINT_ORIGINS",
                    502,
                ) from None
        verifier, challenge = oidc.pkce()
        state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        redirect_uri = f"{options.public_url or str(request.base_url).rstrip('/')}/auth/callback"
        pending = {
            "verifier": verifier,
            "nonce": nonce,
            "redirect_uri": redirect_uri,
            "next": target,
            "token_endpoint": config["token_endpoint"],
            "jwks_uri": config["jwks_uri"],
        }
        store.smart_put(state, "oidc-pending", pending, int(time.time()) + 600)
        query = urlencode(
            {
                "response_type": "code",
                "client_id": options.client_id,
                "redirect_uri": redirect_uri,
                "scope": options.scopes,
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return RedirectResponse(f"{config['authorization_endpoint']}?{query}", status_code=302)

    @app.get("/auth/callback", include_in_schema=False)
    def callback(state: str = "", code: str = "", error: str = "") -> Response:
        options = configured()
        pending = store.smart_take(state, "oidc-pending") if state else None
        if error:
            reason = error if re.fullmatch(r"[a-z_]{1,40}", error) else "error"
            raise AuthError(f"the identity provider did not sign you in ({reason})")
        if pending is None or not code:
            raise AuthError("this sign-in is unknown, used or expired: sign in again")
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": pending["redirect_uri"],
            "code_verifier": pending["verifier"],
            "client_id": options.client_id,
        }
        if options.client_secret:
            form["client_secret"] = options.client_secret
        with http_for(options) as http:
            try:
                token = http.post_form(
                    pending["token_endpoint"], form, {"accept": "application/json"}
                )
            except MendariError as failure:
                raise AuthError(f"the token exchange failed: {failure}") from None
            claims = oidc.verify_id_token(
                str(token.get("id_token", "")),
                issuer=options.issuer,
                audience=options.client_id,
                nonce=pending["nonce"],
                jwks_uri=pending["jwks_uri"],
                http=http,
            )
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            raise AuthError("the id_token names no subject")  # OpenID Connect requires one
        roles = roles_from(claims, options)
        if not roles:
            raise AuthError("your account has no Mendari role; ask an administrator", 403)
        org = (
            str(claims.get(options.org_claim) or DEFAULT_ORG) if options.org_claim else DEFAULT_ORG
        )
        name = str(claims.get("name") or claims.get("email") or claims.get("sub"))
        session = {
            "iss": options.issuer,
            "sub": claims.get("sub"),
            "name": name,
            "roles": sorted(roles),
            "org": org,
        }
        sid = secrets.token_urlsafe(32)
        store.smart_put(sid, "user", session, int(time.time()) + SESSION_SECONDS)
        response = RedirectResponse(pending["next"], status_code=302)
        response.set_cookie(
            COOKIE,
            sid,
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        return response

    @app.post("/auth/logout", include_in_schema=False)
    def logout(request: Request) -> Response:
        sid = request.cookies.get(COOKIE)
        if sid:
            store.smart_delete(sid, "user")
        response = Response(status_code=204)
        response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")
        return response

    @app.get(
        "/api/auth/me",
        tags=["Sign-in"],
        summary="Who am I",
    )
    def me(request: Request) -> dict[str, Any]:
        """Whether sign-in is on here, and the signed-in person or key with their roles."""
        principal = auth.principal(request)
        if principal is None:
            return {"mode": "sign-in", "signed_in": False}
        signed_in = principal.kind != "open"
        mode = "sign-in" if signed_in else "open"
        return {"mode": mode, "signed_in": signed_in, "principal": principal.to_dict()}

    @app.post(
        "/api/auth/keys",
        status_code=201,
        tags=["Sign-in"],
        summary="Create an API key",
    )
    def create_key(request: Request, body: KeyRequest) -> dict[str, Any]:
        """A key for a machine (`Authorization: Bearer ohk_…`) with one role, `viewer` or
        `service`, optionally narrowed by `scope` (space-separated: `system/Task.rs`,
        `system/*.rs`, `https://mendari.co/fhir/scope/decide`, …; a scope Mendari does not grant,
        or one the role cannot use, is a `422` naming it), expiring after `days` (90 unless
        asked, 365 at most). The key is in this response only; it is stored hashed. Creating it
        is recorded in the audit chain, without the key."""
        principal: Principal = getattr(request.state, "principal", OPEN)
        scope = None
        if body.scope is not None:
            scope = scopes.parse(body.scope, permissions=PERMISSIONS[body.role]).scope
        return store.create_api_key(
            name=body.name,
            role=body.role,
            org=principal.org,
            created_by=principal.name,
            scopes=scope,
            days=body.days,
        )

    @app.get(
        "/api/auth/keys",
        tags=["Sign-in"],
        summary="List API keys",
    )
    def list_keys(request: Request) -> list[dict[str, Any]]:
        """Keys of this organization: name, role, scope, who created it and when, when it
        expires, and when and by whom it was revoked. Never the key."""
        principal: Principal = getattr(request.state, "principal", OPEN)
        return store.api_keys(None if principal.kind == "open" else principal.org)

    @app.delete(
        "/api/auth/keys/{key_id}",
        status_code=204,
        tags=["Sign-in"],
        summary="Revoke an API key",
    )
    def revoke_key(request: Request, key_id: str) -> Response:
        """Revoke a key; requests that send it are refused from now on. Revoking is recorded in
        the audit chain."""
        principal: Principal = getattr(request.state, "principal", OPEN)
        org = None if principal.kind == "open" else principal.org
        if not store.revoke_api_key(key_id, org, revoked_by=principal.name):
            raise AuthError("no such key", 404)
        return Response(status_code=204)
