# SPDX-License-Identifier: Apache-2.0
"""Sign-in for organizations: OpenID Connect with the organization's identity provider, roles,
and API keys for machines.

**Off until configured.** Without `MENDARI_OIDC_ISSUER` and `MENDARI_OIDC_CLIENT_ID` the
server is open, as before (production sits behind Vercel Authentication). When configured:

- **People** sign in at `/auth/login`: authorization code with PKCE, and the id_token verified
  with the issuer's JWKS (`standards/oidc.py`). The session is server-side, behind an opaque
  HttpOnly, SameSite=Lax `__Host-` cookie, and ends after 30 minutes idle or 8 hours
  (`server/sessions.py`, which also ties a sign-in to the browser that started it). Roles come
  from a claim (`MENDARI_OIDC_ROLE_CLAIM`, default `roles`), optionally renamed by
  `MENDARI_OIDC_ROLE_MAP` ("clinicians=reviewer,it=admin"), and are worked out again on every
  request from the claims kept with the session and the settings as they are then: a role taken
  out of the map ends access at once. With an identity provider that has no roles (Google, say),
  the claim can be `email`, mapped person by person ("owner@example.org=admin"); an email gives a
  role only when the provider says it is verified (`email_verified`).
  The organization comes from `MENDARI_OIDC_ORG_CLAIM` (default: one organization). This is
  claims mode, the default (`MENDARI_ACCOUNTS=claims`).
- **Memberships mode** (`MENDARI_ACCOUNTS=memberships`, ADR-0008) takes a person's
  organization and roles from records instead (`core/audit.py`: users, organizations,
  memberships), never from claims: the role and organization claims and the role map are
  ignored. A sign-in records the person only when a membership waits for their verified
  address, they hold one, or they were recorded before; it binds what waits, and selects their
  organization when they hold exactly one. The session (its own kind, `member`, sealed) keeps
  who they are and the organization and membership they chose, never roles: each request
  reads that membership as it is then, so a removed membership or changed roles apply at the
  next request. Without one in force they act for no organization (never `default`): they may
  choose one of theirs (`POST /api/auth/organization`), ask to join one
  (`POST /api/auth/access-requests`), or accept an invitation made for their verified address
  (`POST /api/auth/invitations/accept`), and are refused (`NoOrganization`, 403) before every
  other route but the public ones and the pages. An organization's administrators (people,
  `keys`) invite, list and revoke invitations, change roles, remove members and handle
  requests to join. Keys, clients and EHR launches of an organization with no record are
  refused. Switching mode, either way, signs everyone out.
- **Who the audit chain names** (`Principal.actor`, permanent): a person by their verified email,
  else by the identity provider's subject qualified by its issuer (`<issuer>|<subject>`); a key
  as `key <id>`, a client as `client <id>`.
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
- **Clinicians launched from an EHR** (SMART on FHIR, `server/smart_routes.py`, ADR-0006) are
  principals of kind `ehr`: the EHR's verified id_token names them (`ehr:<hash>` of its issuer
  and subject; the chain's name for them is a verified email, else `<issuer>|<subject>`), and
  the organization and level (`view` or `decide`) are the EHR's registration
  (`MENDARI_SMART_EHRS`, read at startup), looked up on every request, never stored with the
  launch: removing a registration ends that EHR's live launches at the next restart or
  redeploy. Only a launch whose callback made the checks sign-in on makes (a subject, a
  Practitioner, `openid fhirUser`) is one: a launch made while sign-in was off starts again.
  They hold two permissions of their own and nothing else: `launch` (the launch's
  patient and the items to decide about) and, registered to decide, `launch_decide`. No other
  principal holds either. Their credential is the launch cookie, and only on `/api/smart/`
  (and the dashboard page, `/`): anywhere else it is not a credential.
- **An EHR calling CDS Hooks with its own signed token** (LP-23b, ADR-0014,
  `server/cds_clients.py`) is a principal of kind `cds` once the token is verified against the
  client registered for its `iss` (`MENDARI_CDS_CLIENTS`, read at startup): `cds:<id>`, named
  `cds <id>` in the chain, in the client's registered organization, holding `discover` and
  `decide` only, and only on `/cds-services` and its children (`CDS_PATH`). There a JWT-shaped
  bearer credential (one with a `.`, which no credential Mendari issues has) is verified in
  every mode, before open mode's shortcut: without a registry it is refused, and so is every
  token that does not verify, with one answer and the reason in the security log
  (`cds_<reason>`). Its organization's record (memberships mode), the route's permission and,
  from a browser page, that the page is on one of the client's own `origins` or this site
  (LP-23c) are checked before its `jti` is claimed. Anywhere else a JWT is what it was before:
  refused with sign-in on, and ignored in open mode, which reads no credential there. Mendari's
  own keys and tokens do not change.
- **Open mode answers `/cds-services` on this machine only** (LP-23c, ADR-0014): with sign-in
  off, before any credential is read, a request there goes on only from this machine
  (`cds_clients.on_this_machine`: a loopback address, a local `Host` and no forwarding header)
  or under `MENDARI_CDS_SANDBOX=1`; anything else is a `403` (`open_mode_remote`), a verified
  token included. The rest of open mode is as before.
- **An `Authorization` header decides alone** (ADR-0004): with one there is no fallback to the
  session cookie, nor to the launch cookie. The scheme is `Bearer` (any case), and the
  credential's prefix says what it is: `ohk_` a key, `mat_` an access token (refused unless
  Backend Services is on); on `/cds-services` a JWT is a CDS client's token (above); anything
  else is refused. A credential is never accepted in a URL or a form (`access_token`).
- **Every route has a permission**, and anything not listed needs `admin` (deny by default).
  Managing keys, clients and (memberships mode) an organization's members and invitations
  (`keys`) is for people who signed in, never for a key or token, whatever its role: a stolen
  credential must not mint one that outlives it, nor a membership.

| Role | May |
|---|---|
| viewer (people and machines) | read (every role also holds `discover`: the lists of packs) |
| reviewer | read (inputs too, under PHI mode), decide, review, record outcomes, give feedback |
| service (machines) | read, decide, record outcomes |
| governance | read, approve automation |
| admin | the rest: evaluations, exports; people only: keys, members, invitations, `curate` |
| labeller (people, memberships mode) | `label`: labelling hidden evaluation sets |
| a grading key (the operator's, not a role a person holds) | `grade`, and nothing else |

An administrator holds neither `label` nor `grade` (hidden evaluation sets, LP-11).
| an EHR launch (`ehr`, not a role) | `launch`; registered to decide, also `launch_decide` |
| a CDS client's token (`cds`, not a role) | `discover` and `decide`, on `/cds-services` only |
| any person, memberships mode | `person`: choose one, ask to join, accept an invitation |
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote, urlencode, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from mendari.core import accounts, tenancy
from mendari.core.audit import (
    KEY_DAYS,
    KEY_MAX_DAYS,
    KEY_MAX_OVERLAP_HOURS,
    KEY_OVERLAP_HOURS,
    MACHINE_ROLES,
    AuditStore,
)
from mendari.errors import (
    AuthError,
    FhirError,
    InvitationRefused,
    MendariError,
    NoOrganization,
    OrganizationChanged,
    ScopeError,
)
from mendari.server import presets, scopes, security_log, sessions
from mendari.server.scopes import Grant
from mendari.standards import oidc, smart
from mendari.standards.fhir_client import GuardedHttp, allowed_servers
from mendari.standards.smart import Registration

if TYPE_CHECKING:  # server/cds_clients.py imports this module
    from mendari.server.cds_clients import CdsClient, CdsOpenMode, CdsRegistry

COOKIE = sessions.SESSION  # `__Host-mendari_session` except on local hosts (server/sessions.py)
REALM = "mendari"
KEY_PREFIX = "ohk_"
TOKEN_PREFIX = "mat_"  # SMART Backend Services access tokens (ADR-0004)
ROLES = ("viewer", "reviewer", "service", "governance", "admin")
# `discover` (every role; never an EHR launch): the lists of the packs a principal may decide
# with, `GET /api/packs` and `GET /cds-services`, which a decide-scoped credential also reaches
# (server/scopes.py, ADR-0004 as amended).
PERMISSIONS: dict[str, frozenset[str]] = {
    "viewer": frozenset({"read", "discover"}),
    "reviewer": frozenset({"read", "discover", "read_input", "decide", "review", "outcome"}),
    "service": frozenset({"read", "discover", "decide", "outcome"}),
    "governance": frozenset({"read", "discover", "approve"}),
    "admin": frozenset(
        {
            "read",
            "discover",
            "read_input",
            "decide",
            "review",
            "outcome",
            "approve",
            "evaluate",
            "admin",
            "keys",
            "curate",
        }
    ),
    # Hidden evaluation sets (LP-11, ADR-0011). `labeller` is a reserved role a membership holds
    # (memberships mode only, `GRANTING_RESERVED`); `grading` is held only by a grading key the
    # operator mints (`mendari access grader-key`), never by a person or a membership.
    "labeller": frozenset({"label"}),
    "grading": frozenset({"grade"}),
}
# Permissions only a person who signed in holds, whatever a key's (or a token's) role says:
# managing keys, clients, members and invitations; managing hidden evaluation sets (`curate`)
# and labelling their cases (`label`).
PEOPLE_ONLY = frozenset({"keys", "curate", "label"})
# What only a grading key holds (LP-11): reading a hidden set to grade against it. No person,
# administrator, other key, token or EHR launch ever holds it (the mirror of EHR_ONLY).
GRADING_ONLY = frozenset({"grade"})
GRADING = "grading"
# What open mode (no sign-in) never holds, though it holds everything else: hidden evaluation
# sets need sign-in (core/evalset_keys.py refuses the keys without it).
NEVER_OPEN = frozenset({"grade", "label", "curate"})
# What an EHR launch may do, by its EHR's registered level; no other principal holds these (the
# mirror of PEOPLE_ONLY), and an EHR launch holds nothing else.
EHR_LEVELS: dict[str, frozenset[str]] = {
    "view": frozenset({"launch"}),
    "decide": frozenset({"launch", "launch_decide"}),
}
EHR_ONLY = frozenset({"launch", "launch_decide"})
LAUNCH_PATH = "/api/smart/"  # where the launch cookie is a credential (and on the page, `/`)
# A CDS client's verified token (LP-23b, ADR-0014): what it holds, and the only paths where a
# JWT is read at all, and where its principal may go.
CDS = "cds"
CDS_PERMISSIONS = frozenset({"discover", "decide"})
CDS_PATH = re.compile(r"^/cds-services(/|$)")
# Open mode's answer on `/cds-services` to a request that is not from this machine (LP-23c), and
# a CDS client's token used by a page on an origin that is neither the client's nor this site's.
OPEN_MODE_REMOTE = (
    "with sign-in off, the CDS services answer on this machine only: turn sign-in on for an EHR "
    "that calls over a network (docs/deploying-with-phi.md)"
)
CDS_ORIGIN = "this CDS client does not call from this origin"
# One refusal for a scoped credential outside its scopes (the challenge names the scope needed).
OUT_OF_SCOPE = "this credential's scopes do not cover this request"
DEFAULT_ORG = "default"
ACCOUNTS_MODES = ("claims", "memberships")  # MENDARI_ACCOUNTS (ADR-0008)
MEMBER = "member"  # a person's session row in memberships mode (sealed; `user` in claims mode)
# A person in memberships mode, with or without an organization: what choosing one, asking to
# join and accepting an invitation need. In claims mode these routes are not mounted, and the
# rule reads as `admin`.
PERSON = "person"
# The organization a dashboard window shows, sent with each of its requests in memberships mode:
# a request from a window left on another organization than the one chosen since is refused.
ORG_PIN = "x-mendari-org"
auth_logger = logging.getLogger("mendari.auth")

# Memberships mode's own paths: a person's own routes, an organization's people, and a labeller's
# and its curators' routes over hidden evaluation sets (LP-11j, LP-11k, mounted with the
# evaluation-set keys too). Not mounted in claims mode, where they read as any route no rule
# lists, `admin` (`Auth.permission`). The grading route (LP-11l, `GRADING_PATH`) is under the
# curators' path but mounted with the keys in both account modes: it is not one of them.
GRADING_PATH = r"^/api/evalsets/[^/]+/runs$"
_MEMBERSHIPS_ONLY = re.compile(
    rf"^(?!{GRADING_PATH.removeprefix('^')})"
    r"/api/(auth/(organization|access-requests|invitations|members)|labelling|evalsets)(/|$)"
)
# (method, path pattern) -> permission, first match wins; unlisted routes need "admin".
_RULES: list[tuple[str, re.Pattern[str], str]] = [
    (method, re.compile(pattern), permission)
    for method, pattern, permission in [
        ("GET", r"^/api/health$", "public"),
        # Readiness (status pages, uptime checks, probes): one cached, bounded read per instance.
        ("GET", r"^/api/health/ready$", "public"),
        ("HEAD", r"^/api/health/ready$", "public"),
        ("GET", r"^/api/auth/me$", "public"),
        ("POST", r"^/api/auth/(organization|access-requests|invitations/accept)$", PERSON),
        ("*", r"^/auth/", "public"),
        # The EHR launch and its callback; the launch's context (`{"active": false}` without
        # one) and ending it. The launch's own routes need an EHR launch (`ehr` principals).
        ("*", r"^/smart/", "public"),
        ("GET", r"^/api/smart/session$", "public"),
        ("POST", r"^/api/smart/logout$", "public"),
        ("GET", r"^/api/smart/(patient|focus)$", "launch"),
        ("POST", r"^/api/smart/decide$", "launch_decide"),
        ("GET", r"^/(static/.*|favicon\.ico)$", "public"),
        ("GET", r"^/$", "page"),
        # The developer landing (static, the same for every deployment of a release) and a
        # redirect from ReDoc's old address to the reference.
        ("GET", r"^/(docs|redoc)$", "public"),
        # The invitation page (memberships mode; not mounted otherwise): public and static,
        # never a redirect, so a signed-out browser never carries the token in the link's
        # fragment on to the identity provider (a redirect keeps it, RFC 9110 §10.2.2).
        ("GET", r"^/invite$", "public"),
        # The status page (static; it reads readiness in the browser).
        ("GET", r"^/status$", "public"),
        ("GET", r"^/docs/reference$", "page"),  # a signed-out browser is sent to sign in
        ("GET", r"^/openapi\.json$", "read"),
        # The scheduled purge (Vercel Cron): its own bearer secret, CRON_SECRET, checked there.
        ("GET", r"^/api/internal/retention$", "public"),
        # The scheduled checkpoint likewise (LP-21j, 21k), and the signed one it publishes.
        ("GET", r"^/api/internal/checkpoint$", "public"),
        ("GET", r"^/api/audit/checkpoint$", "public"),
        # The scheduled monitor (LP-21f): the same secret, checked there.
        ("GET", r"^/api/internal/monitor$", "public"),
        ("GET", r"^/api/notice$", "read"),
        ("GET", r"^/api/holds$", "read"),
        ("POST", r"^/api/holds$", "approve"),
        ("DELETE", r"^/api/holds/[^/]+$", "approve"),
        ("POST", r"^/api/notice/acknowledge$", "decide"),  # what deciding asks first
        ("POST", r"^/api/packs/[^/]+/[^/]+/(decide|guard|fhir/decide|pas)$", "decide"),
        ("POST", r"^/cds-services/[^/]+$", "decide"),
        ("POST", r"^/fhir/Questionnaire/\$questionnaire-package$", "decide"),  # Da Vinci DTR
        ("POST", r"^/fhir/\$decide$", "decide"),
        # How to talk to this FHIR base: public, so clients can discover it. The SMART
        # configuration is mounted only when Backend Services is on (server/backend_services.py).
        ("GET", r"^/fhir/(metadata|OperationDefinition/[^/]+)$", "public"),
        ("GET", r"^/fhir/\.well-known/smart-configuration$", "public"),
        # The terminology carries the packs' labels and criteria: read, like a pack's detail.
        ("GET", r"^/fhir/(CodeSystem|NamingSystem)/[^/]+$", "read"),
        # Decisions and their audit trail: read, like /api/audit, and never public: without a
        # signed-in organization the store's scope is every organization's rows.
        ("GET", r"^/fhir/Task/[^/]+(/_history/[^/]+)?$", "read"),
        ("GET", r"^/fhir/(AuditEvent|Provenance)/[^/]+$", "read"),
        # Search, also as POST (a form body keeps the criteria out of URLs): a read.
        ("GET", r"^/fhir/(Task|AuditEvent|Provenance|CodeSystem)$", "read"),
        ("POST", r"^/fhir/(Task|AuditEvent|Provenance|CodeSystem)/_search$", "read"),
        # The lists of packs to decide with: every role, and a decide-scoped key or token.
        ("GET", r"^/(api/packs|cds-services)$", "discover"),
        ("POST", r"^/api/packs/[^/]+/[^/]+/evals$", "evaluate"),
        ("POST", r"^/api/review/[^/]+$", "review"),
        ("POST", r"^/api/audit/[^/]+/feedback$", "review"),
        ("POST", r"^/api/audit/[^/]+/outcome$", "outcome"),
        ("POST", r"^/api/audit/[^/]+/erase$", "admin"),  # a kept input, on request
        ("GET", r"^/api/packs/[^/]+/[^/]+/feedback/export$", "admin"),  # case inputs, in bulk
        ("*", r"^/api/auth/(keys|clients)", "keys"),
        # Memberships mode: an organization's people, managed by its administrators (people
        # only). Before the reads below: these lists hold e-mail addresses.
        ("*", r"^/api/auth/(members|invitations)(/|$)", "keys"),
        ("GET", r"^/api/auth/access-requests$", "keys"),
        ("POST", r"^/api/auth/access-requests/[^/]+$", "keys"),
        # Hidden evaluation sets (LP-11j; memberships mode with the evaluation-set keys, not
        # mounted otherwise): a labeller's own counts, next case and label, people only. Every
        # method and path under it, before the reads below: the next case is a hidden case.
        ("*", r"^/api/labelling(/|$)", "label"),
        # A grading run over one of an organization's hidden sets (LP-11l; mounted with the
        # evaluation-set keys in both account modes): its grading key only, which holds nothing
        # else. Every method on its path, before the curators' rule below, which would take it.
        ("*", GRADING_PATH, "grade"),
        # And an organization's curators' (LP-11k, mounted likewise): its sets listed and read,
        # their cases drawn, assigned and adjudicated, people only (an administrator). Every
        # method and path under it, before the reads below: a set's answer names its strata,
        # languages and labellers.
        ("*", r"^/api/evalsets(/|$)", "curate"),
        # The whole chain, read or exported, and an organization's administrative events (its
        # keys, clients, people, approvals and holds): administrators only.
        ("GET", r"^/api/audit/(verify|export/fhir|administrative(/export/fhir)?)$", "admin"),
        # The organization's remote calls and their cost (the usage ledger): administrators.
        ("GET", r"^/api/usage$", "admin"),
        ("POST", r"^/api/governance$", "approve"),
        ("DELETE", r"^/api/governance/[^/]+$", "approve"),
        ("POST", r"^/api/registry-approvals$", "approve"),
        ("DELETE", r"^/api/registry-approvals/[^/]+$", "approve"),
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
    accounts: Literal["claims", "memberships"] = "claims"  # MENDARI_ACCOUNTS (ADR-0008)


def accounts_mode() -> Literal["claims", "memberships"]:
    """`MENDARI_ACCOUNTS`: `claims` (the default) or `memberships`; anything else fails at
    startup."""
    raw = os.environ.get("MENDARI_ACCOUNTS", "").strip().lower() or "claims"
    if raw == "claims":
        return "claims"
    if raw == "memberships":
        return "memberships"
    raise MendariError("MENDARI_ACCOUNTS is claims or memberships")


def auth_settings() -> AuthSettings | None:
    """From the environment; None (the server stays open) unless issuer and client id are set."""
    mode = accounts_mode()
    issuer = os.environ.get("MENDARI_OIDC_ISSUER", "").strip().rstrip("/")
    client_id = os.environ.get("MENDARI_OIDC_CLIENT_ID", "").strip()
    if not issuer and not client_id:
        if mode == "memberships":
            raise MendariError(
                "MENDARI_ACCOUNTS=memberships needs sign-in (MENDARI_OIDC_ISSUER, "
                "MENDARI_OIDC_CLIENT_ID)"
            )
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
    ignored = [
        name
        for name in ("MENDARI_OIDC_ROLE_CLAIM", "MENDARI_OIDC_ROLE_MAP", "MENDARI_OIDC_ORG_CLAIM")
        if os.environ.get(name, "").strip()
    ]
    if mode == "memberships" and ignored:
        auth_logger.warning(
            "MENDARI_ACCOUNTS=memberships: %s ignored (roles and organizations come from "
            "memberships)",
            ", ".join(ignored),
        )
    return AuthSettings(
        issuer=issuer,
        client_id=client_id,
        client_secret=os.environ.get("MENDARI_OIDC_CLIENT_SECRET", "").strip() or None,
        role_claim=os.environ.get("MENDARI_OIDC_ROLE_CLAIM", "").strip() or "roles",
        role_map=role_map,
        org_claim=os.environ.get("MENDARI_OIDC_ORG_CLAIM", "").strip() or None,
        public_url=os.environ.get("MENDARI_PUBLIC_URL", "").strip().rstrip("/") or None,
        endpoint_origins=allowed_servers("MENDARI_OIDC_ENDPOINT_ORIGINS"),
        accounts=mode,
    )


@dataclass(frozen=True)
class Principal:
    # "open", "user", "key", "client" (an access token's), "ehr" (an EHR launch) or "cds" (a
    # CDS client's verified token)
    kind: str
    name: str
    # Stable and opaque, for counting and logging per principal: `user:<16 hex>` (person_id),
    # `key:<key id>`, `client:<client id>`, `ehr:<16 hex>` (ehr_id), `cds:<registered id>` or
    # `open`. Never a secret, a name or an email.
    id: str = field(kw_only=True)
    # Who the audit chain records (reviewer, reporter, approver, reader, creator): a person's
    # verified email or `<issuer>|<subject>` (chain_name; an EHR launch's user likewise, by the
    # EHR's issuer), `key <id>`, `client <id>`, `cds <id>`. Permanent.
    actor: str = field(kw_only=True)
    roles: frozenset[str] = field(default_factory=frozenset)
    # None only for a person of no organization, in memberships mode: refused before every route
    # that reads or writes an organization's data (`Auth.check`, `org_of`).
    org: str | None = DEFAULT_ORG
    grant: Grant | None = None  # a credential's scopes; None: the role's permissions, unnarrowed
    expires_at: str | None = None  # a key's or an access token's expiry
    level: str | None = None  # an EHR launch's registered level (`view` or `decide`); no role

    def may(self, permission: str) -> bool:
        if self.kind == "open":
            return permission not in NEVER_OPEN
        return permission in self.permissions()

    def role_permissions(self) -> frozenset[str]:
        """What the roles grant; for a key (or a token), never a people-only permission. An EHR
        launch has its level's permissions only, and nobody else ever has those; a grading key
        has `grade` only, and nobody else ever has that; a CDS client has `discover` and
        `decide` and nothing else. Open mode never holds `NEVER_OPEN`."""
        if self.kind == "ehr":
            return EHR_LEVELS.get(self.level or "", frozenset())
        if self.kind == CDS:
            return CDS_PERMISSIONS
        if self.is_grading_key:
            return GRADING_ONLY
        granted = frozenset().union(*(PERMISSIONS.get(role, frozenset()) for role in self.roles))
        granted -= EHR_ONLY | GRADING_ONLY
        if self.kind == "open":
            return granted - NEVER_OPEN
        return granted if self.kind == "user" else granted - PEOPLE_ONLY

    @property
    def is_grading_key(self) -> bool:
        """A grading key: an API key of role `grading` and no other, unscoped (only the
        operator mints one, `AuditStore.create_grading_key`)."""
        return self.kind == "key" and self.roles == {GRADING} and self.grant is None

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
            "actor": self.actor,
        }
        if self.kind in ("key", "client"):
            scope = None if self.grant is None else self.grant.scope
            shown |= {"scope": scope, "expires_at": self.expires_at}
        if self.kind == "ehr":
            shown["level"] = self.level
        return shown


@dataclass(frozen=True)
class Launch:
    """An EHR launch this request's launch cookie names: its session (the sealed row's data:
    the EHR's token, the patient, who launched) and, with sign-in on, its `ehr` principal."""

    session: dict[str, Any]
    principal: Principal | None


OPEN = Principal(
    kind="open", name="open mode", roles=frozenset({"admin"}), id="open", actor="open mode"
)


class CdsRefused(Exception):
    """A CDS client's token that does not verify (`server/cds_clients.py`): `reason`, one of
    `cds_clients.REASONS`, and the registered client its `iss` names once that is known, for the
    security log. The caller is told neither: every refusal is the one `invalid_token` answer."""

    def __init__(self, reason: str, client_id: str | None = None):
        self.reason = reason
        self.client_id = client_id
        super().__init__(reason)


def key_reaches(
    role: str, scope: str | None, routes: Iterable[tuple[str, str]]
) -> list[tuple[str, str]]:
    """The routes a key of `role` and `scope` gets past the guard on (`Auth.check`): its role's
    permission (people-only ones never), and, scoped, the scope each route needs; the pages
    (static HTML for browsers) too, unscoped (a scope names none). Public routes aside: they
    need no key."""
    grant = None if scope is None else scopes.parse(scope)
    key = Principal(
        kind="key", name="preset", roles=frozenset({role}), grant=grant, id="key:-", actor="-"
    )
    reached = []
    for method, path in routes:
        permission = permission_for(method, path)
        if permission == "public":
            continue
        if permission == "page":
            if grant is None:
                reached.append((method, path))
            continue
        if not key.may(permission):
            continue
        if grant is not None:
            needed = scopes.needed(method, path)
            if needed is None or not grant.covers(needed):
                continue
        reached.append((method, path))
    return reached


def org_of(principal: Principal) -> str:
    """The organization `principal` acts for. A person of no organization never reaches a route
    that asks (`Auth.check`); should one, it is refused here too (`NoOrganization`, 403), never
    widened to every organization or to `default`."""
    if principal.org is None:
        raise NoOrganization()
    return principal.org


def let_through(request: Request) -> Principal:
    """Who the guard let through, on a route that is mounted only with sign-in on (Backend
    Services): there is no open mode to fall back to, so none is refused, never served as
    every organization."""
    principal: Principal | None = getattr(request.state, "principal", None)
    if principal is None:
        raise AuthError("sign in first, or send an API key", 401, challenge=challenge())
    return principal


def scope_of(principal: Principal) -> str | None:
    """What a store method that filters by organization is given: None (every organization) in
    open mode only, else `org_of`."""
    return None if principal.kind == "open" else org_of(principal)


person_id = accounts.person_id  # a signed-in person's `Principal.id` (core/accounts.py)


def ehr_id(issuer: str, subject: str) -> str:
    """An EHR launch's `Principal.id`: the user's subject at the EHR's identity provider, with
    its issuer (the verified id_token's `iss`), hashed so it names nobody."""
    digest = hashlib.sha256(f"{issuer}|{subject}".encode()).hexdigest()
    return f"ehr:{digest[:16]}"


def chain_name(issuer: str, claims: Mapping[str, Any]) -> str:
    """How the audit chain names a person, for good: their email when the identity provider says
    it is verified, else their subject qualified by the issuer (a subject is unique only within
    its issuer). Never the display name, which anyone can change at the provider."""
    email = claims.get("email")
    if claims.get("email_verified") is True and isinstance(email, str) and email.strip():
        return email.strip()
    return f"{issuer}|{claims.get('sub')}"


def kept_claims(claims: Mapping[str, Any], settings: AuthSettings) -> dict[str, Any]:
    """What a session keeps of the id_token, so its roles can be worked out again on every
    request: the role claim's values, the email and whether the provider verified it."""
    kept = {name: claims.get(name) for name in (settings.role_claim, "email", "email_verified")}
    return {name: value for name, value in kept.items() if value is not None}


def safe_next(target: str) -> str:
    """Where to go after signing in: a path on this site, else `/`. Not `//host` (another site),
    and nothing with a backslash or a control character, which a browser may read as one."""
    if not target.startswith("/") or target.startswith("//"):
        return "/"
    if "\\" in target or any(ord(c) < 0x20 or ord(c) == 0x7F for c in target):
        return "/"
    return target


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


def refused_credential(
    reason: str, error: AuthError | None = None, *, actor: str | None = None
) -> AuthError:
    """`error` (by default the one `invalid_token` answer), with the reason the caller is not
    told in the security log: `malformed`, `unknown_prefix`, `key` (unknown, revoked and expired
    alike, as the lookup finds none of them), `key_scope`, `token`, `token_scope`, `scheme`,
    `headers`, `in_url_or_form`, and for a CDS client's token `cds_<reason>`
    (`cds_clients.REASONS`, `cds_unregistered`, `cds_headers`, `cds_no_organization_record`),
    with the client it names as `actor` (`cds:<id>`) once that is known. (An EHR launch whose
    EHR is no longer registered is logged with `unregistered`, `Auth.launch`.)"""
    security_log.event("auth.credential.refused", outcome="refused", reason=reason, actor=actor)
    return error or invalid_token()


def jwt_credential(header: str) -> str | None:
    """The credential of an `Authorization: Bearer` header that is JWT-shaped (it holds a `.`,
    which no credential Mendari issues does, `ohk_` and `mat_` being base64url), else None. Any
    whitespace separates the scheme here, so no spacing of the header takes a JWT past the
    verifier."""
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    credential = parts[1].strip()
    return credential if "." in credential else None


def credential_in_url_or_form() -> AuthError:
    """RFC 6750 lets a client send its credential as an `access_token` query or form parameter;
    Mendari reads neither, and refuses both: they end up in logs and histories."""
    return refused_credential(
        "in_url_or_form",
        AuthError(
            "send credentials in the Authorization header, never in a URL or a form",
            400,
            challenge=challenge("invalid_request"),
        ),
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
    """The permission a route needs. HEAD needs what GET needs: the static files (and any route
    that serves both) answer it as GET, and the rules name GET."""
    method = "GET" if method == "HEAD" else method
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
    preset: str | None = Field(
        default=None,
        max_length=40,
        json_schema_extra={"enum": [*presets.PRESETS, None]},
        description="A documented integration path the key is for (`GET /api/auth/keys/presets`): "
        "its scope, with `role` the preset's and no `scope` sent; `days` defaults to the "
        "preset's. A label only: what the key may do is its role and scope as created.",
    )
    days: int = Field(default=KEY_DAYS, ge=1, le=KEY_MAX_DAYS, description="Days until it expires.")

    @field_validator("preset")
    @classmethod
    def _a_preset_for_this_role(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        chosen = presets.PRESETS.get(value)
        if chosen is None:
            raise ValueError(f"no such preset: one of {', '.join(presets.PRESETS)}")
        if info.data.get("scope") is not None:
            raise ValueError("send a scope or a preset, not both")
        role = info.data.get("role")
        if role is not None and role != chosen.role:
            raise ValueError(f"the {value} preset is a {chosen.role} key: send role {chosen.role}")
        return value


class RotateKeyRequest(BaseModel):
    overlap_hours: int = Field(
        default=KEY_OVERLAP_HOURS,
        ge=0,
        le=KEY_MAX_OVERLAP_HOURS,
        description="Hours the old key keeps working (0: refused at once).",
    )
    days: int = Field(
        default=KEY_DAYS, ge=1, le=KEY_MAX_DAYS, description="Days until the new key expires."
    )

    @model_validator(mode="after")
    def _overlap_within_the_new_key(self) -> RotateKeyRequest:
        if self.overlap_hours > self.days * 24:
            raise ValueError("overlap_hours is longer than the new key lives")
        return self


class OrganizationRequest(BaseModel):
    org: str = Field(
        min_length=2,
        max_length=63,
        description="An organization's id: lower-case letters, digits and hyphens.",
    )

    @field_validator("org")
    @classmethod
    def _an_organization_id(cls, value: str) -> str:
        return accounts.check_org_id(value)


def _roles_field(**default: Any) -> Any:
    """The roles a membership may be given: the server's, `pack_author` (which grants nothing
    yet) and `labeller` (`label` only, memberships mode), never `grading`. Each model checks them
    (`accounts.check_roles`: sorted, each once, a 422 otherwise)."""
    return Field(
        **default,
        min_length=1,
        max_length=len(accounts.MEMBERSHIP_ROLES),
        description="Roles: viewer, reviewer, service, governance, admin, pack_author (reserved: "
        "grants nothing yet) and labeller (labels hidden evaluation sets: label only). Never "
        "grading.",
    )


class InvitationRequest(BaseModel):
    email: str = Field(
        min_length=3,
        max_length=254,
        description="The address it is for: whoever signs in with it, verified by their "
        "identity provider, may accept it.",
    )
    roles: list[str] = _roles_field()
    days: int = Field(
        default=accounts.INVITE_DAYS,
        ge=1,
        le=accounts.INVITE_MAX_DAYS,
        description="Days until it expires.",
    )

    @field_validator("email")
    @classmethod
    def _an_address(cls, value: str) -> str:
        return accounts.normalise_email(value)

    @field_validator("roles")
    @classmethod
    def _membership_roles(cls, value: list[str]) -> list[str]:
        return accounts.check_roles(value)


class InvitationAcceptRequest(BaseModel):
    # `ohi_` and `secrets.token_urlsafe(32)`, which is always 43 characters.
    token: str = Field(
        pattern=r"^ohi_[A-Za-z0-9_-]{43}$",
        description="The invitation's token, from its link's fragment (`/invite#<token>`).",
    )


class RolesRequest(BaseModel):
    roles: list[str] = _roles_field()

    @field_validator("roles")
    @classmethod
    def _membership_roles(cls, value: list[str]) -> list[str]:
        return accounts.check_roles(value)


class AccessDecision(BaseModel):
    outcome: Literal["granted", "refused"]
    roles: list[str] = _roles_field(default_factory=lambda: ["viewer"])  # when granted

    @field_validator("roles")
    @classmethod
    def _membership_roles(cls, value: list[str]) -> list[str]:
        return accounts.check_roles(value)


class Auth:
    """Resolves the principal of a request and guards every route."""

    def __init__(
        self,
        settings: AuthSettings | None,
        store: AuditStore,
        *,
        tokens: bool = False,
        lifetime: sessions.Lifetime | None = None,
        ehrs: Mapping[str, Registration] | None = None,
        cds: CdsRegistry | None = None,
        cds_open: CdsOpenMode | None = None,
    ):
        self.settings = settings
        # The registered CDS clients (`MENDARI_CDS_CLIENTS`, LP-23b); None: no JWT is accepted.
        self.cds = cds
        # Where open mode answers `/cds-services` (LP-23c): this machine, or anywhere under
        # `MENDARI_CDS_SANDBOX=1`; None (a guard made without it): from nowhere.
        self.cds_open = cds_open
        self._replaced_seen: set[str] = set()  # rotated keys already logged in use
        self.store = store
        self.tokens = tokens  # SMART Backend Services is on: access tokens are looked up
        self.lifetime = lifetime or sessions.Lifetime()  # idle and absolute session expiry
        self.discovery = oidc.Discovery()  # the identity provider's configuration, for an hour
        # The registered EHRs (`MENDARI_SMART_EHRS`) when the SMART launch is on; None when off.
        self.ehrs = None if ehrs is None else dict(ehrs)
        # Memberships mode (ADR-0008): people's organizations and roles from records.
        self.memberships = settings is not None and settings.accounts == "memberships"
        self._recorded: set[str] = set()  # organizations seen with a record (never deleted)

    @property
    def enabled(self) -> bool:
        return self.settings is not None

    def permission(self, method: str, path: str) -> str:
        """The permission a request needs here: the route rules' (`permission_for`), except that
        in claims mode memberships mode's own routes (a person's own, `person`, an
        organization's people, `keys`, a labeller's, `label`, and its curators', `curate`: none
        mounted there) read as any route no rule lists, `admin`. The guard, the notice check and
        the rate limits all ask this, so they never disagree about a request."""
        permission = permission_for(method, path)
        if not self.memberships and (permission == PERSON or _MEMBERSHIPS_ONLY.match(path)):
            return "admin"
        return permission

    def acting(self, request: Request) -> Principal:
        """Who the guard let through to a route that acts for an organization: open mode's
        principal only while sign-in is off. With sign-in on, a route reached without one is a
        mistake in the rules: refused, never served as every organization."""
        principal: Principal | None = getattr(request.state, "principal", None)
        if principal is not None:
            return principal
        if self.enabled:
            raise AuthError("sign in first, or send an API key", 401, challenge=challenge())
        return OPEN

    def recorded(self, org: str) -> bool:
        """Memberships mode: whether `org` has a record. Credentials of an organization without
        one (a key, a client, an EHR registration from claims mode) are refused there."""
        if org in self._recorded:
            return True
        if self.store.organization(org) is None:
            return False
        self._recorded.add(org)
        return True

    def principal(self, request: Request) -> Principal | None:
        """Who sends this request; None for no one, or for a credential that is not valid."""
        try:
            return self.authenticate(request)
        except AuthError:
            return None

    def authenticate(self, request: Request) -> Principal | None:
        """The request's principal; None when it carries no credential at all. An
        `Authorization` header decides alone: a credential in it that is not valid is an
        `AuthError` (401), never a fall back to a cookie. Without one, the launch cookie is the
        credential on `/api/smart/` (never the person's session there), and the dashboard page
        (`/`) takes a launch when no one is signed in."""
        if self.settings is None:
            return OPEN
        headers = request.headers.getlist("authorization")
        if headers:
            return self._bearer(headers)
        path = request.url.path
        if path.startswith(LAUNCH_PATH):
            return self._launched(request)
        person = self._person(request)
        if person is None and path == "/":
            return self._launched(request)
        return person

    def _launched(self, request: Request) -> Principal | None:
        launch = self.launch(request)
        return None if launch is None else launch.principal

    def launch(self, request: Request) -> Launch | None:
        """The EHR launch this request's launch cookie names, once per request (`/api/smart/*`
        and the guard share it); None for none: SMART off, no cookie or an ended launch, an
        `Authorization` header with sign-in on (it decides alone), and, with sign-in on or with
        EHRs registered, a launch whose EHR is not registered (in the registrations this
        process started with) or, with sign-in on, one whose callback did not make sign-in on's
        checks (made while sign-in was off, or before launches named who launched)."""
        if self.ehrs is None:
            return None
        cached: Launch | bool | None = getattr(request.state, "launch", False)
        if cached is not False:
            return cached if isinstance(cached, Launch) else None
        found = self._resolve(request)
        request.state.launch = found
        return found

    def _resolve(self, request: Request) -> Launch | None:
        assert self.ehrs is not None
        if self.enabled and request.headers.getlist("authorization"):
            return None
        session, _ = sessions.resume(self.store, request, sessions.SMART, "session", self.lifetime)
        if session is None:
            return None
        if not self.enabled and not self.ehrs:
            return Launch(session, None)  # open mode, no registrations: as before
        registered = smart.registration(self.ehrs, str(session.get("iss", "")))
        if registered is None:
            security_log.event("auth.credential.refused", outcome="refused", reason="unregistered")
            return None
        if not self.enabled:
            return Launch(session, None)  # open mode: the registration gates the launch only
        if self.memberships and not self.recorded(registered.org):
            security_log.event(
                "auth.credential.refused", outcome="refused", reason="no_organization_record"
            )
            return None
        identity = session.get("identity")
        actor = session.get("actor")
        if session.get("identified") is not True:
            # Launched while sign-in was off (no Practitioner or `openid fhirUser` check ran),
            # or before launches named who launched: launch again.
            return None
        if not isinstance(identity, dict) or not isinstance(actor, str):
            return None
        principal = Principal(
            kind="ehr",
            name=str(session.get("fhir_user") or "EHR user"),
            id=ehr_id(str(identity["iss"]), str(identity["sub"])),
            actor=actor,
            org=registered.org,
            level=registered.level,
        )
        return Launch(session, principal)

    def _person(self, request: Request) -> Principal | None:
        """A person signed in, by the session cookie; None for none."""
        assert self.settings is not None
        if self.memberships:
            return self._member(request)
        session, sent = sessions.resume(self.store, request, COOKIE, "user", self.lifetime)
        if session is None:
            if sent:  # ended, expired or never made: a dashboard left open, or a guess
                security_log.event("auth.credential.refused", outcome="refused", reason="session")
            return None
        # Sessions from before the issuer was stored are of the issuer configured now.
        issuer = str(session.get("iss") or self.settings.issuer)
        person = person_id(issuer, str(session["sub"]))
        claims = session.get("claims")
        # The roles as the settings give them now; a session from before claims were kept has
        # the roles of its sign-in, and ends within 8 hours of the deploy.
        if isinstance(claims, dict):
            roles = roles_from(claims, self.settings)
        else:
            roles = frozenset(session["roles"])
        if not roles:  # taken out of the role map since signing in: access ends at once
            security_log.event(
                "auth.credential.refused", outcome="refused", reason="no_role", actor=person
            )
            return None
        return Principal(
            kind="user",
            name=session["name"],
            roles=roles,
            org=session["org"],
            id=person,
            actor=str(session.get("actor") or f"{issuer}|{session['sub']}"),
        )

    def _member(self, request: Request) -> Principal | None:
        """A person signed in, in memberships mode: who they are from their session, and their
        organization and roles from the membership they chose, as it is now (one read, nothing
        cached). None for no session. Without that membership in force (none chosen, removed,
        or removed and given again: a new one is chosen anew) they act for no organization,
        with no role: never `default`, never another of their memberships. Of the reserved
        roles, `labeller` grants `label` (LP-11); `pack_author` grants nothing yet."""
        session, sent = sessions.resume(self.store, request, COOKIE, MEMBER, self.lifetime)
        if session is None:
            if sent:
                security_log.event("auth.credential.refused", outcome="refused", reason="session")
            return None
        issuer, subject = session.get("iss"), session.get("sub")
        if not isinstance(issuer, str) or not isinstance(subject, str):
            return None
        person = person_id(issuer, subject)
        org: str | None = None
        roles: frozenset[str] = frozenset()
        chosen, held = session.get("org"), session.get("membership")
        if isinstance(chosen, str) and isinstance(held, str):
            found = self.store.membership_in_force(person, chosen)
            if found is not None and found["id"] == held:
                granting = frozenset(ROLES) | frozenset(accounts.GRANTING_RESERVED)
                org, roles = chosen, frozenset(found["roles"]) & granting
        request.state.member_session = session  # what choosing an organization rewrites
        return Principal(
            kind="user",
            name=str(session.get("name") or subject),
            roles=roles,
            org=org,
            id=person,
            actor=str(session.get("actor") or f"{issuer}|{subject}"),
        )

    def _bearer(self, headers: list[str]) -> Principal:
        """The principal an `Authorization` header names, by the credential's prefix."""
        if len(headers) != 1:
            raise refused_credential(
                "headers",
                AuthError(
                    "send one Authorization header", 400, challenge=challenge("invalid_request")
                ),
            )
        scheme, _, credential = headers[0].strip().partition(" ")
        if scheme.lower() != "bearer":
            # Not a bearer credential: RFC 6750 gives no error code to an unsupported scheme.
            raise refused_credential(
                "scheme",
                AuthError("send Authorization: Bearer <credential>", 401, challenge=challenge()),
            )
        credential = credential.strip()
        if not _BEARER.fullmatch(credential):
            raise refused_credential("malformed")
        if credential.startswith(KEY_PREFIX):
            key = self.store.api_key(credential)  # None too when revoked or expired
            if key is None:
                raise refused_credential("key")
            if self.memberships and not self.recorded(key["org"]):
                raise refused_credential("no_organization_record")
            if key["replaced_by"] and key["id"] not in self._replaced_seen:
                # A rotated key still in its overlap: say once (per instance) that it is used.
                self._replaced_seen.add(key["id"])
                security_log.event(
                    "auth.key.replaced_used",
                    actor=f"key:{key['id']}",
                    org=key["org"],
                    ids=[str(key["id"]), str(key["replaced_by"])],
                )
            try:
                grant = None if key["scope"] is None else scopes.parse(key["scope"])
            except ScopeError:  # a scope no longer granted: refuse, never widen
                raise refused_credential("key_scope") from None
            return Principal(
                kind="key",
                name=f"key {key['name']}",
                roles=frozenset({key["role"]}),
                org=key["org"],
                grant=grant,
                expires_at=key["expires_at"],
                id=f"key:{key['id']}",
                actor=f"key {key['id']}",  # its name is free text an admin may reuse
            )
        if credential.startswith(TOKEN_PREFIX):
            return self._token(credential)
        raise refused_credential("unknown_prefix")  # no credential Mendari issues

    def _token(self, credential: str) -> Principal:
        """An access token's client as it is now: its role, organization and scopes (the scope
        granted, within what the client registers today); refused once the client is revoked,
        and always when Backend Services is off."""
        token = self.store.access_token(credential) if self.tokens else None
        if token is None or token["role"] not in MACHINE_ROLES:
            raise refused_credential("token")
        if self.memberships and not self.recorded(token["org"]):
            raise refused_credential("no_organization_record")
        try:
            registered = scopes.parse(token["registered"], permissions=PERMISSIONS[token["role"]])
        except ScopeError:  # a registration no longer granted: refuse
            raise refused_credential("token_scope") from None
        expires = datetime.fromtimestamp(token["expires_at"], UTC)
        return Principal(
            kind="client",
            name=f"client {token['client_id']}",
            roles=frozenset({token["role"]}),
            org=token["org"],
            grant=scopes.parse_lenient(token["scope"]).narrow(registered),
            expires_at=expires.isoformat(timespec="milliseconds"),
            id=f"client:{token['client_id']}",
            actor=f"client {token['client_id']}",
        )

    def check(self, request: Request) -> Response | None:
        """None when the request may go on; otherwise the response that refuses it. On
        `/cds-services` open mode answers this machine only (or anyone, under the sandbox flag),
        before any credential is read; then a JWT there is a CDS client's token, verified first,
        in every mode: above open mode's shortcut, so open mode never answers one it did not
        verify."""
        if CDS_PATH.match(request.url.path):
            if self.settings is None and (
                self.cds_open is None or not self.cds_open.answers(request)
            ):
                _refused_permission(OPEN, "open_mode_remote")
                raise AuthError(OPEN_MODE_REMOTE, 403)
            token = self._cds_token(request)
            if token is not None:
                self._cds(request, token)
                return None
        permission = self.permission(request.method, request.url.path)
        if permission == "public" or self.settings is None:
            return None
        principal = self.authenticate(request)
        if principal is not None and principal.kind == CDS and not CDS_PATH.match(request.url.path):
            # Only `_cds` makes one, on `CDS_PATH`: should one reach anywhere else, refuse it.
            _refused_permission(principal, "cds_only")
            refused = challenge("insufficient_scope")
            raise AuthError("a CDS client reaches /cds-services only", 403, challenge=refused)
        if permission == "page":
            if principal is None:
                target = f"/auth/login?next={quote(request.url.path)}"
                return RedirectResponse(target, status_code=302)
            _within_scope(principal, request.method, request.url.path)
            return None
        if principal is None:
            if permission in EHR_ONLY:
                ended = "no EHR launch here: open Mendari again from the EHR"
                raise AuthError(ended, 401, challenge=challenge())
            raise AuthError("sign in first, or send an API key", 401, challenge=challenge())
        if permission == PERSON:  # memberships mode: choosing an organization, asking to join
            if principal.kind != "user":
                _refused_permission(principal, "people_only")
                refused = challenge("insufficient_scope")
                raise AuthError("only a person who signed in may do this", 403, challenge=refused)
            request.state.principal = principal
            return None
        if principal.org is None:  # before any role, body, limit, route or stream
            _refused_permission(principal, "no_organization")
            raise NoOrganization()
        if self.memberships and principal.kind == "user":
            shown = request.headers.get(ORG_PIN)
            if shown is not None and shown != principal.org:
                _refused_permission(principal, "org_changed")
                raise OrganizationChanged()
        if permission not in principal.role_permissions():
            refused = challenge("insufficient_scope")
            if permission in PEOPLE_ONLY and principal.kind != "user":
                _refused_permission(principal, "people_only")
                raise AuthError("only a person who signed in may do this", 403, challenge=refused)
            if permission in EHR_ONLY and principal.kind != "ehr":
                _refused_permission(principal, "ehr_only")
                raise AuthError(
                    "only a launch from an EHR may do this: open Mendari from the EHR",
                    403,
                    challenge=refused,
                )
            if permission in GRADING_ONLY and not principal.is_grading_key:
                _refused_permission(principal, "grading_only")
                raise AuthError(
                    "only the deployment's grading key may do this", 403, challenge=refused
                )
            _refused_permission(principal, permission)
            if principal.kind == "ehr":
                raise AuthError(
                    "this EHR is registered here to view, not to decide", 403, challenge=refused
                )
            raise AuthError(f"your role may not {permission} here", 403, challenge=refused)
        _within_scope(principal, request.method, request.url.path)
        if not principal.may(permission):  # implied by the scope check: fail closed if not
            _refused_permission(principal, "out_of_scope")
            raise AuthError(OUT_OF_SCOPE, 403, challenge=challenge("insufficient_scope"))
        request.state.principal = principal
        return None

    def _cds_token(self, request: Request) -> str | None:
        """On `/cds-services`: the JWT-shaped bearer credential, when an `Authorization` header
        holds one (with another header beside it, the request is refused, as `_bearer` refuses
        two); else None, and the request goes on as any other (a key, a token, a session, or
        open mode, which reads no other credential)."""
        headers = request.headers.getlist("authorization")
        tokens = [token for token in map(jwt_credential, headers) if token is not None]
        if not tokens:
            return None
        if len(headers) != 1:
            raise refused_credential(
                "cds_headers",
                AuthError(
                    "send one Authorization header", 400, challenge=challenge("invalid_request")
                ),
            )
        return tokens[0]

    def _cds(self, request: Request, token: str) -> None:
        """A CDS client's token, verified against the registry for this request's path (its
        `aud` is `MENDARI_PUBLIC_URL` plus the path), made a `cds` principal of the client's
        organization, which may list and call the CDS services and do nothing else. Every
        refusal of the token is the one `invalid_token` answer (`401`), its reason in the
        security log; a route it holds no permission for is a `403`, and so is a browser page on
        an origin that is neither one of the client's own nor this site (LP-23c). The
        organization's record, the route's permission and the page's origin are checked before
        the `jti` is claimed (`admit`), so a token refused for any of them claims nothing."""
        path = request.url.path
        registry = self.cds
        if registry is None:
            raise refused_credential("cds_unregistered")
        permission = self.permission(request.method, path)

        def admit(client: CdsClient) -> None:
            principal = _cds_principal(client)
            if self.memberships and not self.recorded(client.org):
                raise refused_credential("cds_no_organization_record", actor=principal.id)
            if permission not in principal.role_permissions():
                _refused_permission(principal, permission)
                scope = challenge("insufficient_scope")
                raise AuthError(f"a CDS client may not {permission} here", 403, challenge=scope)
            if not registry.from_its_origin(client, request):
                security_log.event(
                    "auth.origin.refused",
                    outcome="refused",
                    reason="cds_origin",
                    actor=principal.id,
                    org=principal.org,
                )
                raise AuthError(CDS_ORIGIN, 403)

        try:
            caller = registry.verify(token, path=path, store=self.store, admit=admit)
        except CdsRefused as refused:
            named = None if refused.client_id is None else f"cds:{refused.client_id}"
            raise refused_credential(f"cds_{refused.reason}", actor=named) from None
        request.state.principal = _cds_principal(caller.client)


def _cds_principal(client: CdsClient) -> Principal:
    """The `cds` principal a CDS client's verified token is: `cds:<id>`, named `cds <id>` in the
    chain, in the client's registered organization."""
    return Principal(
        kind=CDS,
        name=f"CDS client {client.id}",
        id=f"cds:{client.id}",
        actor=f"cds {client.id}",
        org=client.org,
    )


def _within_scope(principal: Principal, method: str, path: str) -> None:
    """A scoped principal reaches only the routes its scopes name (server/scopes.py)."""
    if principal.grant is None:
        return
    needed = scopes.needed(method, path)
    if needed is None or not principal.grant.covers(needed):
        _refused_permission(principal, "out_of_scope")
        refused = challenge("insufficient_scope", scope=needed)
        raise AuthError(OUT_OF_SCOPE, 403, challenge=refused)


def _refused_permission(principal: Principal, reason: str) -> None:
    """A security log line for a principal refused a route: the permission it lacks, or
    `people_only`, `ehr_only`, `grading_only`, `out_of_scope`, `no_organization` (memberships
    mode: a person of none), `org_changed` (a window on another organization than the one
    chosen since), `not_a_member` (choosing an organization one holds no membership of),
    `cds_only` (a CDS client off `/cds-services`) or `open_mode_remote` (open mode's
    `/cds-services` asked from another machine, LP-23c)."""
    security_log.event(
        "auth.permission.refused",
        outcome="refused",
        reason=reason,
        actor=principal.id,
        org=principal.org,
    )


def _signin_refused(reason: str, *, actor: str | None = None) -> None:
    """A sign-in that did not complete: `idp_configuration`, `idp_unavailable`, `idp_error`,
    `state_unknown`, `browser` (the callback reached a browser that did not start the sign-in),
    `token_exchange`, `id_token_invalid`, `no_subject` or `no_role` (with the person's opaque
    id)."""
    security_log.event("auth.signin.refused", outcome="refused", reason=reason, actor=actor)


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
    def login(request: Request, next: str = "/", account: str = "") -> Response:
        options = configured()
        target = safe_next(next)
        # "Use another account": the identity provider asks which (OpenID Connect's
        # `prompt=select_account`). Any other value is ignored.
        choose = {"account": "choose"} if account == "choose" else {}
        # Started on another name: begin again on the public URL, where the callback lands.
        moved = sessions.to_public_url(
            request, options.public_url, "/auth/login", {"next": target, **choose}
        )
        if moved is not None:
            return moved
        with http_for(options) as http:
            try:
                config = auth.discovery.get(options.issuer, http)
            except AuthError:
                _signin_refused("idp_configuration")
                raise
            except FhirError as failure:  # the guarded client's errors (unreachable, not allowed)
                _signin_refused("idp_unavailable")
                raise AuthError(
                    f"the identity provider cannot be used ({failure}); its endpoints must be on "
                    "the issuer's origin or listed in MENDARI_OIDC_ENDPOINT_ORIGINS",
                    502,
                ) from None
        verifier, challenge = oidc.pkce()
        state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        redirect_uri = f"{options.public_url or str(request.base_url).rstrip('/')}/auth/callback"
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
                **({"prompt": "select_account"} if choose else {}),
            }
        )
        endpoint = config["authorization_endpoint"]  # may carry a query of its own (RFC 6749 3.1)
        response = RedirectResponse(f"{endpoint}{'&' if '?' in endpoint else '?'}{query}", 302)
        pending = {
            "verifier": verifier,
            "nonce": nonce,
            "redirect_uri": redirect_uri,
            "next": target,
            "token_endpoint": config["token_endpoint"],
            "jwks_uri": config["jwks_uri"],
            # Only this browser may finish the sign-in (login CSRF): its cookie's digest.
            "browser": sessions.bind(response, request, sessions.SIGNIN)[0],
        }
        store.smart_put(state, "oidc-pending", pending, int(time.time()) + sessions.PENDING_SECONDS)
        return response

    @app.get("/auth/callback", include_in_schema=False)
    def callback(request: Request, state: str = "", code: str = "", error: str = "") -> Response:
        options = configured()
        pending = store.smart_take(state, "oidc-pending") if state else None
        if error:
            reason = error if re.fullmatch(r"[a-z_]{1,40}", error) else "error"
            _signin_refused("idp_error")
            raise AuthError(f"the identity provider did not sign you in ({reason})")
        if pending is None or not code:
            _signin_refused("state_unknown")
            raise AuthError("this sign-in is unknown, used or expired: sign in again")
        if not sessions.bound(request, sessions.SIGNIN, pending):
            # A callback URL from someone else's sign-in (or one started in another tab since).
            _signin_refused("browser")
            raise AuthError("this sign-in was started in another browser or tab: sign in again")
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
                _signin_refused("token_exchange")
                raise AuthError(f"the token exchange failed: {failure}") from None
            try:
                claims = oidc.verify_id_token(
                    str(token.get("id_token", "")),
                    issuer=options.issuer,
                    audience=options.client_id,
                    nonce=pending["nonce"],
                    jwks_uri=pending["jwks_uri"],
                    http=http,
                )
            except MendariError:
                _signin_refused("id_token_invalid")
                raise
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            _signin_refused("no_subject")
            raise AuthError("the id_token names no subject")  # OpenID Connect requires one
        person = person_id(options.issuer, claims["sub"])
        if auth.memberships:
            return by_records(request, options, claims, person, pending)
        roles = roles_from(claims, options)
        if not roles:
            _signin_refused("no_role", actor=person)
            raise AuthError("your account has no Mendari role; ask an administrator", 403)
        org = (
            str(claims.get(options.org_claim) or DEFAULT_ORG) if options.org_claim else DEFAULT_ORG
        )
        name = str(claims.get("name") or claims.get("email") or claims.get("sub"))
        session, expires_at = sessions.begin(
            {
                "iss": options.issuer,
                "sub": claims.get("sub"),
                "name": name,
                "actor": chain_name(options.issuer, claims),
                "claims": kept_claims(claims, options),
                "roles": sorted(roles),  # what the release before this one reads
                "org": org,
            },
            int(time.time()),
            auth.lifetime,
        )
        previous, _ = sessions.read(request, COOKIE)
        if previous:  # this browser's earlier session ends: one browser, one person
            store.smart_delete(previous, "user")
        sid = secrets.token_urlsafe(32)
        store.smart_put(sid, "user", session, expires_at)
        security_log.event("auth.signin.ok", actor=person, org=org)
        response = RedirectResponse(pending["next"], status_code=302)
        sessions.set_cookie(response, request, COOKIE, sid)
        sessions.clear_cookie(response, request, sessions.SIGNIN)  # used: one sign-in, once
        return response

    def by_records(
        request: Request,
        options: AuthSettings,
        claims: Mapping[str, Any],
        person: str,
        pending: Mapping[str, Any],
    ) -> Response:
        """Memberships mode's sign-in (ADR-0008): no claim gives a role or an organization. The
        person is recorded only when a membership waits for their verified address, they hold
        one, or they were recorded before (anyone else who signs in leaves no record); what
        waits is bound; with exactly one membership it is selected. Never refused for having
        none, never placed in `default`: they act for no organization until they choose one."""
        email = claims.get("email")
        address = None
        if claims.get("email_verified") is True and isinstance(email, str):
            try:
                address = accounts.normalise_email(email)
            except ValueError:  # not one an invitation can name: they sign in without it
                address = None
        name = str(claims.get("name") or claims.get("email") or claims["sub"])
        actor = chain_name(options.issuer, claims)
        with tenancy.acting_for_nobody(actor):  # nothing here reads an organization's data
            waiting = address is not None and store.pending_for(address)
            if waiting or store.member_of(person) or store.user(person) is not None:
                store.record_user(
                    issuer=options.issuer, subject=claims["sub"], email=address, name=name
                )
            if waiting and address is not None:
                store.bind_pending(person, address)
            held = store.member_of(person)
            chosen = store.membership_in_force(person, held[0]["org"]) if len(held) == 1 else None
        session, expires_at = sessions.begin(
            {
                "iss": options.issuer,
                "sub": claims["sub"],
                "name": name,
                "actor": actor,
                "email": address,  # verified at this sign-in, for an invitation (LP-12f)
                "org": None if chosen is None else chosen["org"],
                "membership": None if chosen is None else chosen["id"],
            },
            int(time.time()),
            auth.lifetime,
        )
        previous, _ = sessions.read(request, COOKIE)
        if previous:  # this browser's earlier session ends: one browser, one person
            store.smart_delete(previous, MEMBER)
        sid = secrets.token_urlsafe(32)
        store.smart_put(sid, MEMBER, session, expires_at)
        security_log.event("auth.signin.ok", actor=person, org=session["org"])
        response = RedirectResponse(pending["next"], status_code=302)
        sessions.set_cookie(response, request, COOKIE, sid)
        sessions.clear_cookie(response, request, sessions.SIGNIN)  # used: one sign-in, once
        return response

    @app.post("/auth/logout", include_in_schema=False)
    def logout(request: Request) -> Response:
        """Sign out, and end this browser's EHR launch too (a shared workstation)."""
        sid, _ = sessions.read(request, COOKIE)
        launch, _ = sessions.read(request, sessions.SMART)
        if sid:
            store.smart_delete(sid, MEMBER if auth.memberships else "user")
            security_log.event("auth.logout")
        if launch:
            store.smart_delete(launch, "session")
        response = Response(status_code=204)
        sessions.clear_cookie(response, request, COOKIE)
        if launch:
            sessions.clear_cookie(response, request, sessions.SMART)
        return response

    @app.get(
        "/api/auth/me",
        tags=["Sign-in"],
        summary="Who am I",
    )
    def me(request: Request) -> dict[str, Any]:
        """Whether sign-in is on here, and the signed-in person or key with their roles. In
        memberships mode, also `accounts: "memberships"` and, for a person, `memberships`: the
        organizations they hold a membership of (theirs only), each with its name and roles;
        `principal.org` is null while they act for none."""
        principal = auth.principal(request)
        shown: dict[str, Any] = {"accounts": "memberships"} if auth.memberships else {}
        if principal is None:
            return {"mode": "sign-in", "signed_in": False, **shown}
        signed_in = principal.kind != "open"
        mode = "sign-in" if signed_in else "open"
        if auth.memberships and principal.kind == "user":
            shown["memberships"] = store.member_of(principal.id)
        return {"mode": mode, "signed_in": signed_in, "principal": principal.to_dict(), **shown}

    if auth.memberships:
        add_membership_routes(app, auth)

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
        is recorded in the audit chain, without the key. Or name a `preset`
        (`GET /api/auth/keys/presets`): its scope and default expiry, for the role it names."""
        principal = auth.acting(request)
        scope = None
        days = body.days
        if body.preset is not None:
            chosen = presets.PRESETS[body.preset]
            if chosen.scope is not None:
                scope = scopes.parse(chosen.scope, permissions=PERMISSIONS[body.role]).scope
            days = body.days if "days" in body.model_fields_set else chosen.days
        elif body.scope is not None:
            scope = scopes.parse(body.scope, permissions=PERMISSIONS[body.role]).scope
        key = store.create_api_key(
            name=body.name,
            role=body.role,
            org=org_of(principal),
            created_by=principal.actor,
            scopes=scope,
            days=days,
            preset=body.preset,
        )
        security_log.event(
            "admin.key.created", actor=principal.id, org=principal.org, ids=[str(key["id"])]
        )
        return key

    @app.get(
        "/api/auth/keys",
        tags=["Sign-in"],
        summary="List API keys",
    )
    def list_keys(request: Request) -> list[dict[str, Any]]:
        """Keys of this organization, oldest first: name, role, scope, the preset it was made
        from, who created it and when, when it expires, its replacement if it was rotated
        (`replaced_by`), when and by whom it was revoked, and its `status` now: `active`,
        `replaced` (rotated, still working until its `expires_at`), `expired` or `revoked`.
        Never the key."""
        return store.api_keys(scope_of(auth.acting(request)))

    @app.get(
        "/api/auth/keys/presets",
        tags=["Sign-in"],
        summary="Key presets",
    )
    def key_presets(request: Request) -> list[dict[str, Any]]:
        """One preset per documented integration path: its role, its scope (null: the role's
        own), its default expiry and the bounds a key's has, the calls its documentation
        describes, every route a key made from it reaches here (computed from the route rules,
        never written by hand), and its documentation: the page's source path under docs/ (the
        site serves it without `.md`) with the heading's anchor. Create a key from one with
        `POST /api/auth/keys` and `preset`."""
        auth.acting(request)
        routes = presets.served(request.app.routes)
        return [
            preset.as_dict(key_reaches(preset.role, preset.scope, routes))
            for preset in presets.PRESETS.values()
        ]

    @app.get(
        "/api/auth/keys/{key_id}/history",
        tags=["Sign-in"],
        summary="An API key's history",
    )
    def key_history(request: Request, key_id: str) -> dict[str, Any]:
        """What happened to this organization's key, from the audit chain (`key:<id>`), oldest
        first: created (with its role, scope or preset and expiry), rotated (to which key, until
        when the old one works, the overlap asked) and revoked, each with who did it and when;
        never the key or its hash. A rotation's new key names the old one (`replaces`), the old
        one its replacement (`replaced_by`): each has its own history. At most the 200 newest
        events (`truncated` says if there were more). Unknown or another organization's key:
        `404`."""
        found = store.subject_history("key", key_id, scope_of(auth.acting(request)))
        if found is None:
            raise AuthError("no such key", 404)
        return found

    @app.post(
        "/api/auth/keys/{key_id}/rotate",
        status_code=201,
        tags=["Sign-in"],
        summary="Rotate an API key",
    )
    def rotate_key(
        request: Request, key_id: str, body: RotateKeyRequest | None = None
    ) -> dict[str, Any]:
        """Replace a key with a new one of the same name, role and scope, expiring after `days`.
        The old key keeps working for `overlap_hours` (24 unless asked, a week at most, never
        past its own expiry), then is refused as an expired key is, so a machine can switch
        without a gap; `replaces` says when it stops. The new key is in this response only.
        Recorded in the audit chain: the new key's creation (with `replaces`) and the rotation.
        A leaked key: rotate it with `overlap_hours: 0`, or revoke it. Unknown or another
        organization's key: `404`; revoked, expired or already replaced: `409` (unless that
        replacement was revoked: if this answer was lost, revoke the replacement and rotate
        again); a scope its role no longer allows: `422`."""
        principal = auth.acting(request)
        asked = body or RotateKeyRequest()

        def check_scope(scope: str | None, role: str) -> str | None:
            return (
                None if scope is None else scopes.parse(scope, permissions=PERMISSIONS[role]).scope
            )

        key = store.rotate_api_key(
            key_id,
            scope_of(principal),
            rotated_by=principal.actor,
            overlap_hours=asked.overlap_hours,
            days=asked.days,
            check_scope=check_scope,
        )
        security_log.event(
            "admin.key.rotated", actor=principal.id, org=principal.org, ids=[key_id, str(key["id"])]
        )
        return key

    @app.delete(
        "/api/auth/keys/{key_id}",
        status_code=204,
        tags=["Sign-in"],
        summary="Revoke an API key",
    )
    def revoke_key(request: Request, key_id: str) -> Response:
        """Revoke a key; requests that send it are refused from now on. Revoking is recorded in
        the audit chain."""
        principal = auth.acting(request)
        if not store.revoke_api_key(key_id, scope_of(principal), revoked_by=principal.actor):
            raise AuthError("no such key", 404)
        security_log.event("admin.key.revoked", actor=principal.id, org=principal.org, ids=[key_id])
        return Response(status_code=204)


def add_membership_routes(app: FastAPI, auth: Auth) -> None:
    """Memberships mode's routes: a person's own, with or without an organization (`person`:
    choosing one of theirs, asking to join one, accepting an invitation), and an organization's
    people, managed by its administrators (`keys`: people only). The administrators' routes act
    on the organization the guard let them through for (`org_of`), never one a body or a path
    names, and the chain names who acted by their id (`user:<hash>`), never their address. Not
    mounted in claims mode."""
    store = auth.store

    def member_session(request: Request) -> tuple[Principal, str, dict[str, Any]]:
        principal = auth.acting(request)
        session: dict[str, Any] | None = getattr(request.state, "member_session", None)
        sid, _ = sessions.read(request, COOKIE)
        if principal.kind != "user" or session is None or sid is None:
            raise AuthError("sign in first", 401, challenge=challenge())
        return principal, sid, session

    def asker_of(session: Mapping[str, Any]) -> dict[str, Any]:
        """Who signed in, from their session: what records them when they have no record."""
        return {
            "issuer": session["iss"],
            "subject": session["sub"],
            "email": session.get("email"),  # verified at sign-in
            "name": session.get("name") or session["sub"],
        }

    def select(sid: str, session: Mapping[str, Any], org: str, membership_id: str) -> bool:
        """Act for `org`'s `membership_id` from now on, in this browser; the session's time
        limits stay as they were. False when the session has ended since."""
        until = int(time.time()) + auth.lifetime.idle
        absolute = session.get("absolute_at")
        if isinstance(absolute, int):
            until = min(until, absolute)
        chosen = {**session, "org": org, "membership": membership_id}
        return store.smart_replace(sid, MEMBER, chosen, until)

    def organization_name(org: str) -> str:
        record = store.organization(org)
        return org if record is None else str(record["name"])

    def refusal(error: accounts.MembershipRefused) -> AuthError:
        """A membership change the store refused, as its answer: unknown, another
        organization's or ended (`404`), or one that would leave the organization without an
        administrator, or make someone a member twice (`409`)."""
        status = {"no_membership": 404, "last_admin": 409, "member": 409}[error.reason]
        return AuthError(str(error), status)

    @app.post(
        "/api/auth/organization",
        tags=["Sign-in"],
        summary="Choose your organization",
    )
    def choose_organization(request: Request, body: OrganizationRequest) -> dict[str, Any]:
        """Memberships mode: act for `org` from now on, in this browser. A membership of it in
        force is required (`403` otherwise, and the session stays as it was); the session's
        time limits stay as they were. Its roles apply at once. Windows still showing the
        organization before are refused (`409`) until reloaded."""
        principal, sid, session = member_session(request)
        found = store.membership_in_force(principal.id, body.org)
        if found is None:
            _refused_permission(principal, "not_a_member")
            raise AuthError("you hold no membership of that organization in force", 403)
        if not select(sid, session, body.org, found["id"]):  # ended since: sign in again
            raise AuthError("your session has ended: sign in again", 401, challenge=challenge())
        security_log.event("auth.organization.selected", actor=principal.id, org=body.org)
        return {"org": body.org, "name": organization_name(body.org), "roles": found["roles"]}

    @app.post(
        "/api/auth/access-requests",
        status_code=202,
        tags=["Sign-in"],
        summary="Ask to join an organization",
    )
    def ask_to_join(request: Request, body: OrganizationRequest) -> JSONResponse:
        """Memberships mode: ask the administrators of `org` to let you in. The answer is the
        same whether or not that organization exists, you are already a member, or you have too
        many requests open (5): only a request to an organization with a record, from someone
        not a member of it, is recorded (and chained, `access.requested`), once while it is
        open, with the person who asked if they have no record yet (`user.recorded`). The
        answer is the same; how long it takes is not: asking an organization that exists leaves
        a request its administrators see."""
        principal, _, session = member_session(request)
        try:
            made = store.request_access(user_id=principal.id, org=body.org, asker=asker_of(session))
        except accounts.AccessRefused as refused:
            security_log.event(
                "auth.access.requested",
                outcome="refused",
                reason=refused.reason,
                actor=principal.id,
            )
        else:
            security_log.event(
                "auth.access.requested", actor=principal.id, org=body.org, ids=[str(made["id"])]
            )
        return JSONResponse({"org": body.org, "requested": True}, status_code=202)

    @app.post(
        "/api/auth/invitations/accept",
        tags=["Sign-in"],
        summary="Accept an invitation",
    )
    def accept_invitation(request: Request, body: InvitationAcceptRequest) -> dict[str, Any]:
        """Memberships mode: join the organization an invitation is for, with its roles. The
        `token` is the link's fragment (`/invite#<token>`). It is accepted once, before it
        expires, by a person who signed in with the address it names, verified by their identity
        provider (`403` otherwise; unverified or another address, which leaves it open). Unknown,
        used, revoked and expired are one answer (`404`); already a member of its organization:
        `409`, and it stays open (roles change only by an administrator). Recorded in the audit
        chain (`invitation.accepted`, `membership.created`, the person when they have no record
        yet, and their open request to join it, closed: `access.handled`). `selected`: whether
        this browser now acts for it, which it does only when it acted for no organization;
        otherwise choose it (`POST /api/auth/organization`)."""
        principal, sid, session = member_session(request)
        try:
            found = store.accept_invitation(
                body.token,
                user_id=principal.id,
                verified_email=session.get("email"),
                asker=asker_of(session),
            )
        except InvitationRefused as refused:
            # No organization: a bad link learns none from the log either.
            security_log.event(
                "auth.invitation.accepted",
                outcome="refused",
                reason=refused.reason,
                actor=principal.id,
            )
            raise
        # Accepted, for good: a session that ended since is answered, not refused (the link is
        # used, and asking again would be a 404).
        selected = principal.org is None and select(sid, session, found["org"], found["id"])
        security_log.event(
            "auth.invitation.accepted", actor=principal.id, org=found["org"], ids=[found["id"]]
        )
        return {
            "org": found["org"],
            "name": organization_name(found["org"]),
            "roles": found["roles"],
            "selected": selected,
        }

    @app.post(
        "/api/auth/invitations",
        status_code=201,
        tags=["Sign-in"],
        summary="Invite someone to your organization",
    )
    def create_invitation(request: Request, body: InvitationRequest) -> dict[str, Any]:
        """Memberships mode: an invitation to this organization with `roles`, for whoever signs
        in with `email` verified, valid for `days` (7 unless asked, 30 at most). Mendari sends no
        e-mail: pass the `link` on yourself. Its token is in this response only (in `link`, a
        path on this site to put after its address), stored hashed. `grading` is never given,
        `pack_author` grants nothing yet, and `labeller` grants `label` only. Recorded in the
        audit chain (`invitation.created`, with a digest of the address)."""
        principal = auth.acting(request)
        made = store.create_invitation(
            org=org_of(principal),
            email=body.email,
            roles=body.roles,
            created_by=principal.id,
            days=body.days,
        )
        token = made.pop("token")
        security_log.event(
            "admin.invitation.created", actor=principal.id, org=principal.org, ids=[made["id"]]
        )
        # Relative, never built from the request's Host: an address someone else chose would
        # end up in a link an administrator sends on.
        return {**made, "status": "open", "link": f"/invite#{token}"}

    @app.get(
        "/api/auth/invitations",
        tags=["Sign-in"],
        summary="List invitations",
    )
    def list_invitations(request: Request) -> list[dict[str, Any]]:
        """Memberships mode: this organization's invitations, newest first: the address, roles,
        who made it and when, when it expires, when and by whom it was accepted or revoked, and
        its `status` (`open`, `accepted`, `revoked` or `expired`). Never a token."""
        return store.invitations(org_of(auth.acting(request)))

    @app.delete(
        "/api/auth/invitations/{invitation_id}",
        status_code=204,
        tags=["Sign-in"],
        summary="Revoke an invitation",
    )
    def revoke_invitation(request: Request, invitation_id: str) -> Response:
        """Memberships mode: revoke an open invitation of this organization (expired ones too);
        its link is refused from now on. Unknown, another organization's, accepted or already
        revoked: `404`. Recorded in the audit chain (`invitation.revoked`)."""
        principal = auth.acting(request)
        if not store.revoke_invitation(
            invitation_id, org=org_of(principal), revoked_by=principal.id
        ):
            raise AuthError("no open invitation", 404)
        security_log.event(
            "admin.invitation.revoked", actor=principal.id, org=principal.org, ids=[invitation_id]
        )
        return Response(status_code=204)

    @app.get(
        "/api/auth/members",
        tags=["Sign-in"],
        summary="List your organization's members",
    )
    def list_members(request: Request) -> list[dict[str, Any]]:
        """Memberships mode: this organization's memberships in force, oldest first: each
        person's name and verified address, or, `pending`, the address a membership waits for
        (bound at that address's next sign-in, verified), with its roles, who gave it and when.
        `you`: the caller's own."""
        principal = auth.acting(request)
        return [
            {**membership, "you": membership["user_id"] == principal.id}
            for membership in store.members(org_of(principal))
        ]

    @app.put(
        "/api/auth/members/{membership_id}/roles",
        tags=["Sign-in"],
        summary="Change a member's roles",
    )
    def change_roles(request: Request, membership_id: str, body: RolesRequest) -> dict[str, Any]:
        """Memberships mode: replace a membership's roles; they apply at that person's next
        request. Never leaves the organization without an administrator who signed in (`409`);
        unknown, another organization's or ended: `404`. Recorded in the audit chain
        (`membership.roles_changed`, with the roles before)."""
        principal = auth.acting(request)
        try:
            changed = store.set_membership_roles(
                membership_id, body.roles, org=org_of(principal), changed_by=principal.id
            )
        except accounts.MembershipRefused as refused:
            raise refusal(refused) from None
        security_log.event(
            "admin.membership.roles_changed",
            actor=principal.id,
            org=principal.org,
            ids=[membership_id],
        )
        return changed

    @app.delete(
        "/api/auth/members/{membership_id}",
        status_code=204,
        tags=["Sign-in"],
        summary="Remove a member",
    )
    def remove_member(request: Request, membership_id: str) -> Response:
        """Memberships mode: end a membership; that person acts for no organization (or chooses
        another of theirs) from their next request. This organization's open invitations made out
        to their address are revoked with it, so no link left open earlier lets them back in.
        The invitations, keys and clients they made stay as they were: revoke them too if they
        should not. Never the last administrator who signed in (`409`); unknown, another
        organization's or ended: `404`. Recorded in the audit chain (`membership.removed`, and
        `invitation.revoked` for each one revoked)."""
        principal = auth.acting(request)
        try:
            removed = store.remove_membership(
                membership_id, org=org_of(principal), removed_by=principal.id
            )
        except accounts.MembershipRefused as refused:
            raise refusal(refused) from None
        if not removed:
            raise AuthError("no such membership", 404)
        security_log.event(
            "admin.membership.removed", actor=principal.id, org=principal.org, ids=[membership_id]
        )
        return Response(status_code=204)

    @app.get(
        "/api/auth/access-requests",
        tags=["Sign-in"],
        summary="List requests to join",
    )
    def list_access_requests(request: Request) -> list[dict[str, Any]]:
        """Memberships mode: the open requests to join this organization, oldest first, each
        with the name and verified address of the person who asked (none when their identity
        provider verified none)."""
        return store.open_access_requests(org_of(auth.acting(request)))

    @app.post(
        "/api/auth/access-requests/{request_id}",
        tags=["Sign-in"],
        summary="Grant or refuse a request to join",
    )
    def handle_access_request(
        request: Request, request_id: str, body: AccessDecision
    ) -> dict[str, Any]:
        """Memberships mode: grant an open request to join this organization (a membership with
        `roles`, `viewer` unless asked) or refuse it. Unknown, handled or another
        organization's: `404` (a request closes by itself when its person joins another way).
        Granting one left open from before that, of someone who is a member already: `409`,
        and it stays open (refuse it to close it). Recorded in the audit chain
        (`access.handled`, and `membership.created` when granted)."""
        principal = auth.acting(request)
        org = org_of(principal)
        try:
            handled = store.handle_access_request(
                request_id,
                org=org,
                outcome=body.outcome,
                handled_by=principal.id,
                roles=body.roles,
            )
        except accounts.MembershipRefused as refused:
            if refused.reason != "member":
                raise
            raise AuthError(
                f"already a member of {org!r}: refuse the request to close it", 409
            ) from None
        if handled is None:
            raise AuthError("no open request", 404)
        membership = handled["membership"]
        security_log.event(
            "admin.access.granted" if body.outcome == "granted" else "admin.access.refused",
            actor=principal.id,
            org=org,
            ids=[request_id, *([membership["id"]] if membership else [])],
        )
        return handled
