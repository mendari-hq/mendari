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

The registry's reviewers (LP-19e) have routes of their own, `/api/registry/review/...`
(`add_registry_review_routes`): the queue of pending claims and the acts that are a reviewer's
(approve or refuse a claim, set a publisher type, review, recall and delete a version; a shared
administrator token did them until LP-19g removed it). They are mounted only in memberships
mode with `MENDARI_REGISTRY_REVIEW_ORG` naming an organization that has a record, and answer
only a person who holds `registry_reviewer` in that organization (`registry_review`: never a
key, a client, open mode or claims mode, and never an administrator of another organization).
They act on
every organization's namespaces, which is why the role counts in one organization only.

Imported inside `create_app` only: `mendari.hub.registry` imports from `server/app.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, TypeVar

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from mendari.errors import HubError
from mendari.hub import yank
from mendari.hub.registry import (
    _NAME,
    _VERSION,
    MAX_TOKEN_DAYS,
    REVIEWER,
    DeleteRequest,
    PublisherTypeRequest,
    RegistryStore,
    ReviewRequest,
)
from mendari.server import security_log
from mendari.server.auth import Auth, Principal, org_of

TAG = "Registry accounts"
REVIEW_TAG = "Registry review"
# What one answer of the reviewers' queue holds: the oldest pending claims, and of each
# namespace its newest versions, without their cards (a card is up to 1 MB, and a publisher
# chooses its size: one version's is read on its own). What is left out is counted.
MAX_QUEUE = 50
MAX_QUEUE_VERSIONS = 5
_QUEUE_FIELDS = (
    "id",
    "name",
    "version",
    "digest",
    "archive_sha256",
    "published_at",
    "checked",
    "claimed",
    "yank",
    "deleted",
)
DEFAULT_TOKEN_DAYS = 90  # as an engine API key's (`core/audit.py`)
# What one organization's administrators may make (each a row and a chained event, for good): a
# namespace's tokens that work, the tokens an organization mints in a day (so revoking and
# minting again is bounded too), and a namespace's publisher keys.
MAX_LIVE_TOKENS = 20
MAX_TOKENS_A_DAY = 50
MAX_KEYS = 20
MAX_SITES = 20  # an organization's sites (LP-22i): each a row and a chained event, for good
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


class SiteRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120, description="the site's name, as shown")


class SiteKeyRequest(BaseModel):
    """The key file `mendari site init` writes: the site's public key and its proof."""

    public_pem: str = Field(min_length=40, max_length=2000)
    proof: str = Field(min_length=40, max_length=200)


class SiteKeyRevokeRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=yank.MAX_REASON)
    compromised: bool = False

    @field_validator("reason")
    @classmethod
    def _reason_is_a_line(cls, reason: str) -> str:
        why = yank.printable_line(reason, yank.MAX_REASON)
        if why or not reason.strip():
            raise ValueError(f"a reason is {why or 'not only spaces'}")
        return reason


def _site_shown(site: dict[str, Any]) -> dict[str, Any]:
    """A site as its organization's administrators see it: never its organization (their own)
    nor a key's PEM (the dashboard shows ids)."""
    return {
        **{k: site[k] for k in ("id", "name", "status", "created_at", "created_by", "decided_at")},
        "note": site.get("note"),
        "closed_at": site.get("closed_at"),
        "keys": [
            {k: key[k] for k in ("key_id", "created_at", "added_by", "revoked_at", "compromised")}
            for key in site["keys"]
        ],
    }


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
        key_id = _asked(
            lambda: registry.add_key(namespace, body.public_pem, by=principal.id, most=MAX_KEYS)
        )
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
        minted = _asked(
            lambda: registry.mint_token(
                body.namespace,
                org=org,
                owner=principal.id,
                name=body.name,
                days=body.days,
                by=principal.id,
                live_most=MAX_LIVE_TOKENS,
                day_most=MAX_TOKENS_A_DAY,
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

    # -- sites (LP-22i) ---------------------------------------------------------------------

    @app.get("/api/registry/sites", tags=[TAG], summary="List this organization's sites")
    def sites(request: Request) -> list[dict[str, Any]]:
        """This organization's evaluating sites, oldest first (LP-22): each `pending` until a
        registry reviewer verifies it, `verified`, `refused` or `closed`, with its keys by id
        and their dates, revoked ones too."""
        _, org = person(request)
        return [_site_shown(site) for site in registry.sites(org=org)]

    @app.post("/api/registry/sites", status_code=201, tags=[TAG], summary="Register a site")
    def register_site(request: Request, body: SiteRequest) -> dict[str, Any]:
        """Register an evaluating site for this organization. It is `pending`, gets no ticket
        and shares nothing until a registry reviewer verifies it. Chained `site.registered`
        with the organization and your id."""
        principal, org = person(request)
        made = _asked(
            lambda: registry.register_site(org=org, name=body.name, by=principal.id, most=MAX_SITES)
        )
        security_log.event(
            "admin.registry_site.registered", actor=principal.id, org=org, ids=[made["id"]]
        )
        return _site_shown(made)

    @app.post(
        "/api/registry/sites/{site_id}/keys",
        status_code=201,
        tags=[TAG],
        summary="Add a site key",
    )
    def add_site_key(request: Request, site_id: str, body: SiteKeyRequest) -> dict[str, Any]:
        """Add a site's Ed25519 public key with its proof that the site holds it: the key file
        `mendari site init` writes. A site's private key is never typed or made here. Another
        organization's site, or none: `404`; no proof: `403`; a publisher's key, another
        site's, or a sixth live key: `409`. Chained `site.key_added`."""
        principal, org = person(request)
        added = _asked(
            lambda: registry.add_site_key(
                site_id, body.public_pem, body.proof, org=org, by=principal.id
            )
        )
        security_log.event(
            "admin.registry_site_key.added", actor=principal.id, org=org, ids=[added["key_id"]]
        )
        return _site_shown(registry.site_record(site_id, org=org))

    @app.delete(
        "/api/registry/sites/{site_id}/keys/{key_id}",
        tags=[TAG],
        summary="Revoke a site key",
    )
    def revoke_site_key(
        request: Request, site_id: str, key_id: str, body: SiteKeyRevokeRequest
    ) -> dict[str, Any]:
        """Revoke a site key at once, saying why and whether it is thought compromised: then
        every report it signed leaves the pooled figures. Another organization's, or not this
        site's: `404`. Chained `site.key_revoked`."""
        principal, org = person(request)
        found = _asked(lambda: registry.site_record(site_id, org=org))
        if key_id not in {key["key_id"] for key in found["keys"]}:
            raise HTTPException(404, "no such site key")
        revoked = _asked(
            lambda: registry.revoke_site_key(
                key_id, org=org, by=principal.id, reason=body.reason, compromised=body.compromised
            )
        )
        security_log.event(
            "admin.registry_site_key.revoked", actor=principal.id, org=org, ids=[key_id]
        )
        return _site_shown(revoked)


class ClaimDecisionRequest(BaseModel):
    decision: Literal["approve", "refuse"]
    # Chained for good and shown to the claim's organization: one short printable line.
    note: str = Field(default="", max_length=yank.MAX_REASON)

    @field_validator("note")
    @classmethod
    def _one_printable_line(cls, note: str) -> str:
        why = yank.printable_line(note, yank.MAX_REASON) if note else None
        if why or (note and not note.strip()):
            raise ValueError(f"a note is {why or 'not only spaces'}")
        return note


class RegistryRecallRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=yank.MAX_REASON)
    severity: yank.Severity

    @field_validator("reason")
    @classmethod
    def _reason_is_a_line(cls, reason: str) -> str:
        why = yank.printable_line(reason, yank.MAX_REASON)
        if why or not reason.strip():
            raise ValueError(f"a reason is {why or 'not only spaces'}")
        return reason


def _a_version(namespace: str, name: str, version: str) -> None:
    """A path that names a version as the registry names one: anything else is a version that
    does not exist (never a lookup, never an echo of what was sent)."""
    if not (_NAME.match(namespace) and _NAME.match(name) and _VERSION.match(version)):
        raise HTTPException(404, "no such pack version")


def add_registry_review_routes(
    app: FastAPI, auth: Auth, registry: RegistryStore, review_org: str
) -> None:
    """The reviewers' routes (LP-19e). The guard lets through only a person who holds
    `registry_review`, which `Auth._member` grants only for a `registry_reviewer` membership of
    `review_org`; each route checks all three again, so a change to the guard's rules alone
    never makes someone a reviewer."""

    def reviewer(request: Request) -> Principal:
        principal = auth.acting(request)
        if (
            principal.kind != "user"
            or principal.org != review_org
            or "registry_reviewer" not in principal.roles
            or not principal.may("registry_review")
        ):
            raise HTTPException(403, "only a reviewer of this registry may do this")
        return principal

    def logged(name: str, principal: Principal, *ids: str) -> None:
        security_log.event(name, actor=principal.id, org=review_org, ids=list(ids))

    def not_theirs(principal: Principal, namespace: str) -> None:
        """A reviewer never acts on a namespace whose claim they filed themselves: not its
        decision, and not what the registry then shows as checked (its publisher type, a
        version's review) or does to its versions. Another reviewer does."""
        found = registry.claims(namespace=namespace)
        if found and found[0]["claimed_by"] == principal.id:
            raise HTTPException(403, "another reviewer acts on a namespace you claimed")

    def shown(version: dict[str, Any], *, card: bool = False) -> dict[str, Any]:
        """A version as a reviewer is shown it: what the registry checked apart from what its
        publisher claims (ADR-0012) and, read on its own (`card`), the card the registry
        rendered (never an uploaded one: `_version_row`)."""
        fields = (*_QUEUE_FIELDS, "card_markdown") if card else _QUEUE_FIELDS
        return {field: version.get(field) for field in fields}

    @app.get("/api/registry/review", tags=[REVIEW_TAG], summary="The reviewers' queue")
    def queue(request: Request) -> dict[str, Any]:
        """The pending namespace claims, oldest first (50 at most; `more` counts the rest):
        each with its organization, who claimed it (their id, never an address), whether that
        is you (`own`: you cannot decide it), its publisher keys by id, and its newest versions
        (5 at most; `more_versions` counts the rest), each with what the registry `checked`
        apart from what the publisher `claimed`. A version's card is read with the version
        (`GET /api/registry/review/packs/...`). A claim with no version cannot be approved
        yet."""
        principal = reviewer(request)
        pending = registry.claims(status="pending")
        items = []
        for claim in pending[:MAX_QUEUE]:
            space = registry.namespace(claim["namespace"], REVIEWER)
            versions, total = registry.namespace_versions(
                claim["namespace"], REVIEWER, limit=MAX_QUEUE_VERSIONS
            )
            items.append(
                {
                    "namespace": claim["namespace"],
                    "org": claim["org"],
                    "claimed_by": claim["claimed_by"],
                    "created_at": claim["created_at"],
                    "own": claim["claimed_by"] == principal.id,
                    "display_name": space["display_name"],
                    "publisher_type": space["publisher_type"],
                    "keys": [k["key_id"] for k in space["keys"]],
                    "versions": [shown(v) for v in versions],
                    "more_versions": max(0, total - len(versions)),
                }
            )
        return {"pending": items, "more": max(0, len(pending) - MAX_QUEUE)}

    @app.get(
        "/api/registry/review/packs/{namespace}/{name}/{version}",
        tags=[REVIEW_TAG],
        summary="One version, with its card",
    )
    def one_version(request: Request, namespace: str, name: str, version: str) -> dict[str, Any]:
        """One version of any namespace, listed or not, as the queue shows it, with the card
        the registry rendered from its archive (`card_markdown`; null for a version from before
        the registry rendered its own)."""
        reviewer(request)
        _a_version(namespace, name, version)
        found = _asked(lambda: registry.version(namespace, name, version, REVIEWER))
        return shown(found, card=True)

    @app.get("/api/registry/review/sites", tags=[REVIEW_TAG], summary="Sites waiting")
    def site_queue(request: Request) -> dict[str, Any]:
        """The pending sites of every organization, oldest first (50 at most; `more` counts
        the rest): each with its organization, its name, who registered it (their id), whether
        that is you (`own`: you cannot decide it) and its keys by id. Check outside Mendari that
        the organization is who it says before you verify its site (LP-22)."""
        principal = reviewer(request)
        pending = registry.sites(status="pending")
        return {
            "pending": [
                {
                    "id": site["id"],
                    "org": site["org"],
                    "name": site["name"],
                    "created_at": site["created_at"],
                    "created_by": site["created_by"],
                    "own": site["created_by"] == principal.id or site["org"] == principal.org,
                    "keys": [k["key_id"] for k in site["keys"] if k["revoked_at"] is None],
                }
                for site in pending[:MAX_QUEUE]
            ],
            "more": max(0, len(pending) - MAX_QUEUE),
        }

    @app.post(
        "/api/registry/review/sites/{site_id}",
        tags=[REVIEW_TAG],
        summary="Verify or refuse a site",
    )
    def decide_site(request: Request, site_id: str, body: ClaimDecisionRequest) -> dict[str, Any]:
        """Verify or refuse a pending site, once, with a note (chained and shown to its
        organization). You never decide a site you registered or your organization's (`403`).
        No such site: `404`; decided already: `409`. Chained `site.verified` or
        `site.refused`."""
        principal = reviewer(request)
        found = _asked(lambda: registry.site_record(site_id))
        if found["created_by"] == principal.id:
            raise HTTPException(403, "another reviewer decides a site you registered")
        decided = _asked(
            lambda: registry.decide_site(
                site_id,
                verify=body.decision == "approve",
                by=principal.id,
                note=body.note or ("verified" if body.decision == "approve" else "refused"),
                by_org=principal.org,
            )
        )
        logged(f"admin.registry_site.{decided['status']}", principal, site_id)
        return {k: decided[k] for k in ("id", "org", "name", "status", "decided_at", "note")}

    @app.post(
        "/api/registry/review/namespaces/{namespace}",
        tags=[REVIEW_TAG],
        summary="Approve or refuse a namespace claim",
    )
    def decide(request: Request, namespace: str, body: ClaimDecisionRequest) -> dict[str, Any]:
        """Approve or refuse a pending claim, once. Approval needs a published version (`409`
        without one); from then the namespace is listed, with every version it has and will
        have. A refused namespace stays taken and is never listed. You never decide a claim you
        filed yourself (`403`), nor act on its namespace in any other way here. No claim:
        `404`; already decided: `409`. Chained
        `namespace.approved` or `namespace.refused` with the claim's organization, your id and
        the note."""
        principal = reviewer(request)
        if not _NAME.match(namespace):
            raise HTTPException(404, "no such namespace")
        not_theirs(principal, namespace)
        decided = _asked(
            lambda: registry.decide_claim(
                namespace, approve=body.decision == "approve", by=principal.id, note=body.note
            )
        )
        logged(f"admin.registry_claim.{decided['status']}", principal, namespace)
        return decided

    @app.post(
        "/api/registry/review/namespaces/{namespace}/publisher-type",
        tags=[REVIEW_TAG],
        summary="Set a namespace's publisher type",
    )
    def publisher_type(
        request: Request, namespace: str, body: PublisherTypeRequest
    ) -> dict[str, Any]:
        """Record who publishes in a namespace, after checking it: `community`, `verified` or
        `institutional`. Shown with each of its versions as a checked fact. Chained
        `namespace.publisher_type_set` with your id."""
        principal = reviewer(request)
        if not _NAME.match(namespace):
            raise HTTPException(404, "no such namespace")
        not_theirs(principal, namespace)
        now = _asked(lambda: registry.namespace(namespace, REVIEWER))
        if now["publisher_type"] == body.publisher_type:  # nothing to chain
            return {"namespace": namespace, "publisher_type": body.publisher_type}
        changed = _asked(
            lambda: registry.set_publisher_type(namespace, body.publisher_type, by=principal.id)
        )
        logged("admin.registry_namespace.publisher_type_set", principal, namespace)
        return {"namespace": namespace, "publisher_type": changed["publisher_type"]}

    @app.post(
        "/api/registry/review/packs/{namespace}/{name}/{version}",
        status_code=201,
        tags=[REVIEW_TAG],
        summary="Record a version's review",
    )
    def review(
        request: Request, namespace: str, name: str, version: str, body: ReviewRequest
    ) -> dict[str, Any]:
        """Record the risk level your review of one version found (which may differ from the
        one its publisher declared) and a short note about the pack: never case text. The
        newest review of a version is the one shown. Chained `pack.reviewed` with your id."""
        principal = reviewer(request)
        _a_version(namespace, name, version)
        not_theirs(principal, namespace)
        reviewed = _asked(
            lambda: registry.review(
                namespace,
                name,
                version,
                risk_level=body.risk_level,
                note=body.note,
                reviewer=principal.id,
            )
        )
        logged("admin.registry_pack.reviewed", principal, f"{namespace}/{name}@{version}")
        return shown(reviewed)

    @app.post(
        "/api/registry/review/packs/{namespace}/{name}/{version}/recall",
        status_code=201,
        tags=[REVIEW_TAG],
        summary="Recall a version",
    )
    def recall(
        request: Request, namespace: str, name: str, version: str, body: RegistryRecallRequest
    ) -> dict[str, Any]:
        """Recall one version with a reason and a severity: the registry signs the record with
        its own key, and every server that pulled the version stops automating with it. Without
        a signing key (`MENDARI_REGISTRY_SIGNING_KEY`) nothing is recalled: `409`, and it says
        so. A version is recalled once (`409`). Chained `pack.yanked` with your id."""
        principal = reviewer(request)
        _a_version(namespace, name, version)
        not_theirs(principal, namespace)
        recalled = _asked(
            lambda: registry.recall_as_registry(
                namespace,
                name,
                version,
                reason=body.reason,
                severity=body.severity,
                by=principal.id,
            )
        )
        logged("admin.registry_pack.recalled", principal, f"{namespace}/{name}@{version}")
        return shown(recalled)

    @app.post(
        "/api/registry/review/packs/{namespace}/{name}/{version}/delete",
        tags=[REVIEW_TAG],
        summary="Delete a version",
    )
    def delete(
        request: Request, namespace: str, name: str, version: str, body: DeleteRequest
    ) -> dict[str, Any]:
        """Delete one version (content it should never have carried): its archive is no longer
        served, and its bytes are removed when no other version needs them. The row and a
        tombstone with the reason stay, and the version is never published again. Once (`409`).
        Chained `pack.deleted` with your id."""
        principal = reviewer(request)
        _a_version(namespace, name, version)
        not_theirs(principal, namespace)
        deleted = _asked(
            lambda: registry.delete(namespace, name, version, reason=body.reason, by=principal.id)
        )
        logged("admin.registry_pack.deleted", principal, f"{namespace}/{name}@{version}")
        return shown(deleted)
