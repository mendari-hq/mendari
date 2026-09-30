"""Sign-in for organizations: OpenID Connect with the organization's identity provider, roles,
and API keys for machines.

**Off until configured.** Without `OPENHEALTH_OIDC_ISSUER` and `OPENHEALTH_OIDC_CLIENT_ID` the
server is open, as before (production sits behind Vercel Authentication). When configured:

- **People** sign in at `/auth/login`: authorization code with PKCE, and the id_token verified
  with the issuer's JWKS (`standards/oidc.py`). The session is server-side, behind an opaque
  HttpOnly, SameSite=Lax cookie. Roles come from a claim (`OPENHEALTH_OIDC_ROLE_CLAIM`, default
  `roles`), optionally renamed by `OPENHEALTH_OIDC_ROLE_MAP` ("clinicians=reviewer,it=admin").
  The organization comes from `OPENHEALTH_OIDC_ORG_CLAIM` (default: one organization).
- **Machines** send `Authorization: Bearer ohk_...`: API keys that admins create, which are
  shown once, stored as SHA-256, carry a role and can be revoked.
- **Every route has a permission**, and anything not listed needs `admin` (deny by default).

| Role | May |
|---|---|
| viewer | read |
| reviewer | read (inputs too, under PHI mode), decide, review, record outcomes, give feedback |
| service (machines) | read, decide, record outcomes |
| governance | read, approve automation |
| admin | everything, including evaluations, exports and API keys |
"""

from __future__ import annotations

import os
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, Field

from openhealth.core.audit import AuditStore
from openhealth.errors import AuthError, OpenHealthError
from openhealth.standards import oidc
from openhealth.standards.fhir_client import GuardedHttp, allowed_servers

COOKIE = "openhealth_session"
ROLES = ("viewer", "reviewer", "service", "governance", "admin")
PERMISSIONS: dict[str, frozenset[str]] = {
    "viewer": frozenset({"read"}),
    "reviewer": frozenset({"read", "read_input", "decide", "review", "outcome"}),
    "service": frozenset({"read", "decide", "outcome"}),
    "governance": frozenset({"read", "approve"}),
    "admin": frozenset(
        {"read", "read_input", "decide", "review", "outcome", "approve", "evaluate", "admin"}
    ),
}
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
        ("GET", r"^/(docs|redoc|openapi\.json)$", "read"),
        ("POST", r"^/api/packs/[^/]+/[^/]+/(decide|guard|fhir/decide|pas)$", "decide"),
        ("POST", r"^/cds-services/[^/]+$", "decide"),
        ("POST", r"^/fhir/Questionnaire/\$questionnaire-package$", "decide"),  # Da Vinci DTR
        ("GET", r"^/cds-services$", "read"),
        ("POST", r"^/api/packs/[^/]+/[^/]+/evals$", "evaluate"),
        ("POST", r"^/api/review/[^/]+$", "review"),
        ("POST", r"^/api/audit/[^/]+/feedback$", "review"),
        ("POST", r"^/api/audit/[^/]+/outcome$", "outcome"),
        ("GET", r"^/api/packs/[^/]+/[^/]+/feedback/export$", "admin"),  # case inputs, in bulk
        ("*", r"^/api/auth/keys", "admin"),
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
    scopes: str = "openid profile email"


def auth_settings() -> AuthSettings | None:
    """From the environment; None (the server stays open) unless issuer and client id are set."""
    issuer = os.environ.get("OPENHEALTH_OIDC_ISSUER", "").strip().rstrip("/")
    client_id = os.environ.get("OPENHEALTH_OIDC_CLIENT_ID", "").strip()
    if not issuer and not client_id:
        return None
    if not issuer or not client_id:
        raise OpenHealthError("set both OPENHEALTH_OIDC_ISSUER and OPENHEALTH_OIDC_CLIENT_ID")
    parts = urlsplit(issuer)
    host = parts.hostname or ""
    local = host in ("localhost", "127.0.0.1") or "." not in host  # a compose service, say
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise OpenHealthError("OPENHEALTH_OIDC_ISSUER must use https")
    role_map = {}
    for pair in os.environ.get("OPENHEALTH_OIDC_ROLE_MAP", "").split(","):
        if pair.strip():
            source, _, role = pair.partition("=")
            if role.strip() not in ROLES:
                raise OpenHealthError(f"OPENHEALTH_OIDC_ROLE_MAP: {role.strip()!r} is not a role")
            role_map[source.strip()] = role.strip()
    return AuthSettings(
        issuer=issuer,
        client_id=client_id,
        client_secret=os.environ.get("OPENHEALTH_OIDC_CLIENT_SECRET", "").strip() or None,
        role_claim=os.environ.get("OPENHEALTH_OIDC_ROLE_CLAIM", "").strip() or "roles",
        role_map=role_map,
        org_claim=os.environ.get("OPENHEALTH_OIDC_ORG_CLAIM", "").strip() or None,
        public_url=os.environ.get("OPENHEALTH_PUBLIC_URL", "").strip().rstrip("/") or None,
    )


@dataclass(frozen=True)
class Principal:
    kind: str  # "open", "user" or "key"
    name: str
    roles: frozenset[str] = field(default_factory=frozenset)
    org: str = DEFAULT_ORG

    def may(self, permission: str) -> bool:
        if self.kind == "open":
            return True
        return any(permission in PERMISSIONS.get(role, frozenset()) for role in self.roles)

    def to_dict(self) -> dict[str, Any]:
        granted = set().union(*(PERMISSIONS.get(role, frozenset()) for role in self.roles))
        return {
            "kind": self.kind,
            "name": self.name,
            "roles": sorted(self.roles),
            "permissions": sorted(granted),
            "org": self.org,
        }


OPEN = Principal(kind="open", name="open mode", roles=frozenset({"admin"}))


def permission_for(method: str, path: str) -> str:
    for rule_method, pattern, permission in _RULES:
        if rule_method in ("*", method) and pattern.match(path):
            return permission
    return "admin"


def roles_from(claims: Mapping[str, Any], settings: AuthSettings) -> frozenset[str]:
    raw = claims.get(settings.role_claim) or []
    values = [raw] if isinstance(raw, str) else [str(v) for v in raw if isinstance(v, str | int)]
    mapped = {settings.role_map.get(v, v) for v in values}
    return frozenset(role for role in mapped if role in ROLES)


class KeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    role: str = Field(pattern=r"^(viewer|reviewer|service|governance|admin)$")


class Auth:
    """Resolves the principal of a request and guards every route."""

    def __init__(self, settings: AuthSettings | None, store: AuditStore):
        self.settings = settings
        self.store = store

    @property
    def enabled(self) -> bool:
        return self.settings is not None

    def principal(self, request: Request) -> Principal | None:
        if self.settings is None:
            return OPEN
        header = request.headers.get("authorization", "")
        if header.startswith("Bearer ohk_"):
            key = self.store.api_key(header.removeprefix("Bearer ").strip())
            if key is None:
                return None
            roles = frozenset({key["role"]})
            return Principal(kind="key", name=f"key {key['name']}", roles=roles, org=key["org"])
        sid = request.cookies.get(COOKIE)
        session = self.store.smart_get(sid, "user") if sid else None
        if session is None:
            return None
        roles = frozenset(session["roles"])
        return Principal(kind="user", name=session["name"], roles=roles, org=session["org"])

    def check(self, request: Request) -> Response | None:
        """None when the request may go on; otherwise the response that refuses it."""
        permission = permission_for(request.method, request.url.path)
        if permission == "public" or self.settings is None:
            return None
        principal = self.principal(request)
        if permission == "page":
            if principal is None:
                target = f"/auth/login?next={quote(request.url.path)}"
                return RedirectResponse(target, status_code=302)
            return None
        if principal is None:
            raise AuthError("sign in first, or send an API key", 401)
        if not principal.may(permission):
            raise AuthError(f"your role may not {permission} here", 403)
        request.state.principal = principal
        return None


def add_auth_routes(app: FastAPI, auth: Auth) -> None:
    settings = auth.settings
    store = auth.store

    def configured() -> AuthSettings:
        if settings is None:
            raise AuthError("sign-in is not configured here (OPENHEALTH_OIDC_ISSUER)", 404)
        return settings

    def http_for(options: AuthSettings) -> GuardedHttp:
        issuer_origin = f"{urlsplit(options.issuer).scheme}://{urlsplit(options.issuer).netloc}"
        return GuardedHttp(allowed={*allowed_servers(), issuer_origin})

    @app.get("/auth/login", include_in_schema=False)
    def login(request: Request, next: str = "/") -> Response:
        options = configured()
        target = next if next.startswith("/") and not next.startswith("//") else "/"
        with http_for(options) as http:
            config = oidc.discover(options.issuer, http)
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
            except OpenHealthError as failure:
                raise AuthError(f"the token exchange failed: {failure}") from None
            claims = oidc.verify_id_token(
                str(token.get("id_token", "")),
                issuer=options.issuer,
                audience=options.client_id,
                nonce=pending["nonce"],
                jwks_uri=pending["jwks_uri"],
                http=http,
            )
        roles = roles_from(claims, options)
        if not roles:
            raise AuthError("your account has no OpenHealth role; ask an administrator", 403)
        org = (
            str(claims.get(options.org_claim) or DEFAULT_ORG) if options.org_claim else DEFAULT_ORG
        )
        name = str(claims.get("name") or claims.get("email") or claims.get("sub"))
        session = {"sub": claims.get("sub"), "name": name, "roles": sorted(roles), "org": org}
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
            store.smart_delete(sid)
        response = Response(status_code=204)
        response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")
        return response

    @app.get("/api/auth/me")
    def me(request: Request) -> dict[str, Any]:
        principal = auth.principal(request)
        if principal is None:
            return {"mode": "sign-in", "signed_in": False}
        signed_in = principal.kind != "open"
        mode = "sign-in" if signed_in else "open"
        return {"mode": mode, "signed_in": signed_in, "principal": principal.to_dict()}

    @app.post("/api/auth/keys", status_code=201)
    def create_key(request: Request, body: KeyRequest) -> dict[str, Any]:
        principal: Principal = getattr(request.state, "principal", OPEN)
        return store.create_api_key(
            name=body.name, role=body.role, org=principal.org, created_by=principal.name
        )

    @app.get("/api/auth/keys")
    def list_keys(request: Request) -> list[dict[str, Any]]:
        principal: Principal = getattr(request.state, "principal", OPEN)
        return store.api_keys(None if principal.kind == "open" else principal.org)

    @app.delete("/api/auth/keys/{key_id}", status_code=204)
    def revoke_key(request: Request, key_id: str) -> Response:
        principal: Principal = getattr(request.state, "principal", OPEN)
        if not store.revoke_api_key(key_id, None if principal.kind == "open" else principal.org):
            raise AuthError("no such key", 404)
        return Response(status_code=204)
