# SPDX-License-Identifier: Apache-2.0
"""The people's routes of the registry under the engine (LP-19d, ADR-0015).

Mounted only with `MENDARI_REGISTRY=on` and sign-in on. An organization's administrators (the
`keys` permission, people only: never a key, a client or a publisher token) claim namespaces,
register publisher keys and mint, list and revoke publisher tokens. Each route acts for the
organization the guard let the person through for (`org_of`), never one a body or a path names:
another organization's namespace or token answers as one that does not exist.

These routes are the only way a signed-in browser changes the registry. The mounted registry
itself (`/registry/v1/...`) never reads a session cookie: publishing is by publisher token
(`ohr_`) only, so no page can make a signed-in browser publish. Here the engine's origin check
(`server/origin.py`) stands in front of every route, as on the other people's routes.

What is written is chained in the registry's own chain (`hub_events`), naming the person by
their id (`user:<hash>`), never by an address. The security log gets
`admin.registry_namespace.claimed`, `admin.registry_key.added` and
`admin.registry_token.created|revoked`, with ids only: never a token.

Imported inside `create_app` only: `mendari.hub.registry` imports from `server/app.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from mendari.errors import HubError
from mendari.hub.registry import _NAME, MAX_TOKEN_DAYS, REVIEWER, RegistryStore
from mendari.server import security_log
from mendari.server.auth import Auth, Principal, org_of

TAG = "Registry accounts"
DEFAULT_TOKEN_DAYS = 90  # as an engine API key's (`core/audit.py`)
# What one organization's administrators may make (each a row and a chained event, for good): a
# namespace's tokens that work, the tokens an organization mints in a day (so revoking and
# minting again is bounded too), and a namespace's publisher keys.
MAX_LIVE_TOKENS = 20
MAX_TOKENS_A_DAY = 50
MAX_KEYS = 20
# What a claim's holder sees of it: never the organization (their own) or a column added later.
_CLAIM_FIELDS = ("namespace", "status", "claimed_by", "created_at", "decided_at", "note")
Answer = TypeVar("Answer")


def _namespace(name: str) -> str:
    """A namespace's name, whole (the registry's own rule): nothing after it, not a newline."""
    if not _NAME.match(name):
        raise ValueError("a namespace is lowercase letters, digits and dashes")
    return name


class ClaimRequest(BaseModel):
    name: str = Field(min_length=1, max_length=63, description="lowercase letters, digits, dashes")
    display_name: str = Field("", max_length=200)

    _a_name = field_validator("name")(_namespace)


class PublisherKeyRequest(BaseModel):
    public_pem: str = Field(min_length=40, max_length=2000)


class PublisherTokenRequest(BaseModel):
    namespace: str = Field(min_length=1, max_length=63)
    name: str = Field(min_length=1, max_length=80)
    days: int = Field(DEFAULT_TOKEN_DAYS, ge=1, le=MAX_TOKEN_DAYS)

    _a_name = field_validator("namespace")(_namespace)


def _asked(call: Callable[[], Answer]) -> Answer:
    """The registry store's refusal (`HubError`: 404, 409, 422) as this API answers one."""
    try:
        return call()
    except HubError as error:
        raise HTTPException(error.status, str(error)) from None


def add_registry_routes(app: FastAPI, auth: Auth, registry: RegistryStore) -> None:
    def person(request: Request) -> tuple[Principal, str]:
        """Who the guard let through, and the organization they act for. The guard lets only a
        signed-in person with `keys` reach these routes; anyone else is refused here too,
        never served."""
        principal = auth.acting(request)
        if principal.kind != "user":
            raise HTTPException(403, "only a person who signed in may do this")
        return principal, org_of(principal)

    def held(org: str, namespace: str) -> dict[str, Any]:
        """`org`'s claim on `namespace`, one that was not refused. A namespace that does not
        exist, has no claim or is another organization's: one answer."""
        found = registry.claims(org=org, namespace=namespace)
        if not found:
            raise HTTPException(404, f"no namespace {namespace!r}")
        if found[0]["status"] == "refused":
            raise HTTPException(409, f"the claim on {namespace!r} was refused")
        return found[0]

    def shown(claim: dict[str, Any]) -> dict[str, Any]:
        space = registry.namespace(claim["namespace"], REVIEWER)
        return {
            **{field: claim[field] for field in _CLAIM_FIELDS},
            "display_name": space["display_name"],
            "publisher_type": space["publisher_type"],
            "listed": space["listed"],
            "keys": [{"key_id": k["key_id"], "created_at": k["created_at"]} for k in space["keys"]],
        }

    @app.get("/api/registry/namespaces", tags=[TAG], summary="List this organization's namespaces")
    def namespaces(request: Request) -> list[dict[str, Any]]:
        """The namespaces this organization claimed, oldest first: each with its `status`
        (`pending` until a reviewer approves its first publish, `approved` or `refused`), who
        claimed it (their id, never an address), whether it is `listed` (served to everyone),
        and its publisher keys by id."""
        _, org = person(request)
        return [shown(claim) for claim in registry.claims(org=org)]

    @app.post(
        "/api/registry/namespaces",
        status_code=201,
        tags=[TAG],
        summary="Claim a namespace",
    )
    def claim(request: Request, body: ClaimRequest) -> dict[str, Any]:
        """Claim a namespace for this organization. It is `pending`, and nothing of it is
        served to anyone but its own tokens, until a reviewer approves its first publish.
        Refused: a reserved name (`422`), a name that exists (`409`), and a fourth pending
        claim (`409`). Chained in the registry's chain (`namespace.created`,
        `namespace.claimed`) with the organization and the claimant's id."""
        principal, org = person(request)
        made = _asked(
            lambda: registry.claim(
                body.name, org=org, by=principal.id, display_name=body.display_name
            )
        )
        security_log.event(
            "admin.registry_namespace.claimed", actor=principal.id, org=org, ids=[made["namespace"]]
        )
        return shown(made)

    @app.post(
        "/api/registry/namespaces/{namespace}/keys",
        status_code=201,
        tags=[TAG],
        summary="Register a publisher key",
    )
    def add_key(request: Request, namespace: str, body: PublisherKeyRequest) -> dict[str, str]:
        """Register an Ed25519 public key (PEM) that signs this namespace's archives. Only for
        a namespace this organization claimed (another's, or none: `404`). A key belongs to one
        namespace (`409` otherwise). Chained `key.added`."""
        principal, org = person(request)
        held(org, namespace)
        if len(registry.namespace(namespace, REVIEWER)["keys"]) >= MAX_KEYS:
            raise HTTPException(409, f"a namespace has {MAX_KEYS} publisher keys at most")
        key_id = _asked(lambda: registry.add_key(namespace, body.public_pem, by=principal.id))
        security_log.event("admin.registry_key.added", actor=principal.id, org=org, ids=[key_id])
        return {"namespace": namespace, "key_id": key_id}

    @app.get("/api/registry/tokens", tags=[TAG], summary="List publisher tokens")
    def tokens(request: Request) -> list[dict[str, Any]]:
        """This organization's publisher tokens, oldest first: id, namespace, name, who made it
        (their id), when it was made, when it expires, when and by whom it was revoked, and the
        last day it was used. Never the token or its hash."""
        _, org = person(request)
        return registry.tokens(org=org)

    @app.post(
        "/api/registry/tokens",
        status_code=201,
        tags=[TAG],
        summary="Mint a publisher token",
    )
    def mint(request: Request, body: PublisherTokenRequest) -> dict[str, Any]:
        """A publisher token (`ohr_…`) for a namespace this organization claimed (another's, or
        none: `404`), living `days` (90 unless asked, 365 at most). It publishes to that
        namespace only, at `/registry/v1/...`, and is no credential anywhere else. The token is
        in this response only; the registry keeps its SHA-256. Chained `token.issued` with the
        organization, the owner's id and the expiry, never the token."""
        principal, org = person(request)
        held(org, body.namespace)
        now = datetime.now(UTC)
        made = registry.tokens(org=org)
        live = [
            token
            for token in made
            if token["namespace"] == body.namespace
            and token["revoked_at"] is None
            and datetime.fromisoformat(token["expires_at"]) > now
        ]
        if len(live) >= MAX_LIVE_TOKENS:
            raise HTTPException(
                409, f"a namespace has {MAX_LIVE_TOKENS} working tokens at most: revoke one first"
            )
        today = [
            token
            for token in made
            if now - datetime.fromisoformat(token["created_at"]) < timedelta(days=1)
        ]
        if len(today) >= MAX_TOKENS_A_DAY:
            raise HTTPException(
                429, f"an organization mints {MAX_TOKENS_A_DAY} tokens a day at most"
            )
        minted = _asked(
            lambda: registry.mint_token(
                body.namespace,
                org=org,
                owner=principal.id,
                name=body.name,
                days=body.days,
                by=principal.id,
            )
        )
        security_log.event(
            "admin.registry_token.created", actor=principal.id, org=org, ids=[str(minted["id"])]
        )
        return minted

    @app.delete(
        "/api/registry/tokens/{token_id}",
        status_code=204,
        tags=[TAG],
        summary="Revoke a publisher token",
    )
    def revoke(request: Request, token_id: str) -> Response:
        """Revoke a publisher token, at once and for good: the registry refuses it from its
        next request. Unknown, or another organization's: `404`. Chained `token.revoked`."""
        principal, org = person(request)
        _asked(lambda: registry.revoke_token(token_id, org=org, by=principal.id))
        security_log.event(
            "admin.registry_token.revoked", actor=principal.id, org=org, ids=[token_id]
        )
        return Response(status_code=204)
