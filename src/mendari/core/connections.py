# SPDX-License-Identifier: Apache-2.0
"""An agent's connection: the service that answers for it in one organization, set in the
console by that organization's administrators (ADR-0021).

Before this, a deployment had one agent endpoint for every agent and every organization
(`MENDARI_AGENT_URL`, with `MENDARI_AGENT_TOKEN`), set in its environment; without it an agent
answers from its canned examples, which never automate where people sign in. A connection is
the same two things, an endpoint and a bearer token, kept per organization and agent in the
engine's database, so a hospital connects the agent it adopted without a terminal or a restart.
Where a live call goes is decided in one place, `providers/agent.py` (`AgentProvider.live`).

- **The endpoint** is https (plain http to this machine only), with no user, password, query or
  fragment, and is one the deployment may call (`admitted`): on its provider allow-list where it
  has one (`MENDARI_PROVIDER_ALLOWED_ORIGINS`), else on the public internet only
  (`providers/reach.py`).
- **An origin the list does not name** can be allowed for one agent in one organization by an
  operator of the deployment (an administrator of the organization that operates it, signed in
  as a person: `server/auth.py` `operate`), when they set the connection. What they allow is
  kept on the connection's row and chained (`agent_connection.origin_allowed`), and it leads to
  the public internet only, exactly as on a deployment with no list: this machine and the
  site's own network stay out of reach unless the list names them. It holds for that origin
  alone: a connection changed to another origin, or cleared, has lost it. Anyone else stays
  bound by the list.
- **The token** is write-only. It is sealed under the deployment's PHI key (`core/phi.py`, the
  kind `connection`), bound to its row, its organization and its agent, and is opened only to
  call that endpoint. No method here returns it but `resolve`, which the provider calls; a
  deployment without a PHI key (`MENDARI_PHI_MODE` other than `encrypt`) refuses one. A token
  stays with the endpoint it was set for: changing the endpoint drops it unless a new one is
  given, so nobody can have the server send a token they cannot read to an address of theirs.
- **Each change is chained** (`agent_connection.set`, `agent_connection.cleared`): who, when,
  the agent and the endpoint's origin, and whether it carries a token; never the token, nor the
  endpoint's path.
- **Each organization has its own**: every read and write names its organization, and none
  takes one from a request's body, query or path.

`MENDARI_AGENT_CONNECTIONS=off` switches the feature off for a deployment that wants only its
own endpoint (`enabled`): nothing is set or changed, and what was set before is not used.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from mendari.core import accounts
from mendari.core.audit import _now
from mendari.core.spec import is_pack_id
from mendari.errors import MendariError, ProviderError
from mendari.providers import reach
from mendari.providers.agent import Connection
from mendari.providers.http import ALLOWED_ORIGINS_ENV, allowed_origins, require_https
from mendari.standards.fhir_client import origin_of

if TYPE_CHECKING:
    from mendari.core.audit import AuditStore

SWITCH_ENV = "MENDARI_AGENT_CONNECTIONS"
MAX_ENDPOINT_CHARS = 2048
MAX_TOKEN_CHARS = 4096
# A bearer token as a header carries it: visible ASCII, no space, so it can never break out of
# its header line.
_TOKEN = re.compile(r"[\x21-\x7e]+\Z")
# An address is written in the characters a URL is made of (RFC 3986), and its host is a name of
# letters, digits, hyphens and dots, or an IP address. Nothing two parsers could read two ways:
# no backslash, no accent or look-alike of a dot or a slash, no percent sign in the host. What
# is checked here is then what is called.
_ADDRESS = re.compile(r"[A-Za-z0-9\-._~:/\[\]@!$&'()*+,;=%?#]+\Z")
_HOST = re.compile(r"(?:[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?\.)*[a-z0-9_-]+\.?\Z|[0-9a-f:.]+\Z", re.I)


class _Keep:
    """`set`'s default for the token: keep the one there is, if the endpoint stays the same."""


KEEP: Any = _Keep()


class ConnectionRefused(MendariError):
    """A connection that is not set, changed, cleared or read: `status` is the HTTP status a
    route answers with (404 no such connection of this organization, 409 not possible on this
    deployment as it is configured, 422 otherwise). Its words never hold a token."""

    def __init__(self, message: str, status: int = 422, *, code: str | None = None):
        self.status = status
        self.code = code  # `origin_not_listed`: an operator could allow it; None otherwise
        super().__init__(message)


def enabled() -> bool:
    """Whether organizations may set connections here (`MENDARI_AGENT_CONNECTIONS`: `on`,
    which it is unset, or `off`). Anything else fails at startup."""
    raw = os.environ.get(SWITCH_ENV, "").strip().lower()
    if raw in ("", "on"):
        return True
    if raw == "off":
        return False
    raise MendariError(f"{SWITCH_ENV} is on or off")


# How an endpoint's origin is one the deployment may call (`admitted`).
LISTED = "listed"  # on the deployment's allow-list: the operator's own word, called as its own
PUBLIC = "public"  # the deployment lists no origins: the public internet only
ALLOWED = "allowed"  # off the list, allowed for this agent here by an operator: public only
# What refuses an origin the deployment's list does not name, to whoever may not allow it.
NOT_LISTED = (
    "{origin} is not on the list of addresses this deployment may call. An administrator of "
    "the organization that operates this deployment can allow it for this agent when setting "
    "its connection (public addresses only); otherwise whoever runs the deployment adds that "
    f"origin to {ALLOWED_ORIGINS_ENV}"
)


def admitted(
    endpoint: str, *, allowed_origin: str | None = None, allow: bool = False
) -> tuple[str, str, str]:
    """An endpoint as it is kept, its origin and how that origin is admitted (`LISTED`,
    `PUBLIC` or `ALLOWED`), or a `ConnectionRefused` that says why not, in this order: its
    shape (https, or plain http to this machine; no user, password, query or fragment); then
    where it leads: an origin on the deployment's provider allow-list where it has one, else
    the public internet only, looked up now (`providers/reach.py`). An origin the list does not
    name is admitted, to the public internet only, when an operator allowed it for this agent
    here before (`allowed_origin`) or allows it now (`allow`: the caller is one, and asked)."""
    endpoint = endpoint.strip()
    if not endpoint or len(endpoint) > MAX_ENDPOINT_CHARS or not endpoint.isprintable():
        raise ConnectionRefused(f"an address is 1 to {MAX_ENDPOINT_CHARS} printable characters")
    if any(character.isspace() for character in endpoint):
        raise ConnectionRefused("an address has no space in it")
    if not _ADDRESS.fullmatch(endpoint):
        raise ConnectionRefused(
            "an address is written in plain URL characters: no backslash, and a name with an "
            "accent in its xn-- form"
        )
    try:
        require_https(endpoint, "Agent")
        parts = urlsplit(endpoint)
        parts.port  # noqa: B018 - a port that is not a number raises here
    except ProviderError as error:
        raise ConnectionRefused(_said(error)) from None
    except ValueError:
        raise ConnectionRefused("the address is not a valid URL") from None
    if parts.query or parts.fragment or "?" in endpoint or "#" in endpoint:
        raise ConnectionRefused(
            "an address has no query (?...) or fragment (#...): a credential goes in the token"
        )
    if not _HOST.fullmatch(parts.hostname or ""):
        raise ConnectionRefused("the address's host is a name or an IP address")
    origin = origin_of(endpoint)
    if origin is None:
        raise ConnectionRefused("the address is not a valid URL")
    try:
        allowed = allowed_origins()
    except MendariError:
        raise ConnectionRefused(
            f"this deployment's {ALLOWED_ORIGINS_ENV} cannot be read, so no address is accepted: "
            "whoever runs this deployment corrects it",
            409,
        ) from None
    if allowed is not None and origin in allowed:
        return endpoint, origin, LISTED
    if allowed is not None and not (allow or origin == allowed_origin):
        raise ConnectionRefused(NOT_LISTED.format(origin=origin), code="origin_not_listed")
    try:
        reach.check_public(endpoint)
    except ProviderError as error:
        raise ConnectionRefused(_said(error)) from None
    return endpoint, origin, PUBLIC if allowed is None else ALLOWED


# What the provider's own checks say of an endpoint (`providers/http.py` `require_https`), as
# the person who typed the address reads it.
_REWORDED = {
    "Agent endpoints must use https (http only for localhost)": (
        "an address starts with https:// (plain http for this machine only)"
    ),
    "Agent endpoint must not carry credentials in its address": (
        "an address holds no user or password: a credential goes in the token"
    ),
    "Agent endpoint is not a valid URL": "the address is not a valid URL",
}


def _said(error: ProviderError) -> str:
    """A provider's refusal of an endpoint as a person reads it (never a remote answer: these
    are the checks made before anything is sent)."""
    detail = error.detail or "the address is not one this deployment may call"
    return _REWORDED.get(detail, detail)


def _org(org: str) -> str:
    try:
        return accounts.check_org_id(org)
    except ValueError as error:
        raise ConnectionRefused(str(error)) from None


def _agent(pack_id: str) -> str:
    if not is_pack_id(pack_id):
        raise ConnectionRefused("an agent is named namespace/name", 404)
    return pack_id


def _actor(by: str) -> str:
    by = by.strip()
    if not by or len(by) > 200 or not by.isprintable():
        raise ConnectionRefused("who acts is named: one line of 1 to 200 characters")
    return by


def _shown(row: Mapping[str, Any]) -> dict[str, Any]:
    """A connection as every read shows it: where it leads, who set it and when, and whether it
    carries a token (and who set that, and when). Never the token."""
    return {
        "pack_id": row["pack_id"],
        "endpoint": row["endpoint"],
        "origin": origin_of(str(row["endpoint"])),
        "token": "set" if row["token"] is not None else "none",
        "token_set_by": row["token_set_by"],
        "token_set_at": row["token_set_at"],
        "set_by": row["changed_by"],
        "set_at": row["changed_at"],
        # Who allowed this origin for this agent here, and when, where the deployment's list
        # does not name it; null where the list does, or where there is no list.
        "origin_allowed_by": row["allowed_by"] if _allowed(row) else None,
        "origin_allowed_at": row["allowed_at"] if _allowed(row) else None,
    }


def _allowed(row: Mapping[str, Any]) -> bool:
    """Whether a row's endpoint is at the origin an operator allowed for it."""
    return row["endpoint"] is not None and row["allowed_origin"] == origin_of(str(row["endpoint"]))


class ConnectionStore:
    """Every organization's agent connections, over an `AuditStore` (its database, its PHI
    policy and its chain)."""

    def __init__(self, store: AuditStore) -> None:
        self.store = store

    @property
    def seals(self) -> bool:
        """Whether this deployment can keep a token: it has a PHI key to seal it under."""
        return self.store.phi.mode == "encrypt" and bool(self.store.phi.key)

    def get(self, org: str, pack_id: str) -> dict[str, Any] | None:
        """`org`'s connection for an agent, as it is shown; None when it has none (never set,
        or cleared). Never another organization's."""
        org = _org(org)
        with self.store._db.session(exclusive=False) as db:
            row = db.one(
                "SELECT * FROM agent_connections WHERE org = ? AND pack_id = ?", (org, pack_id)
            )
        return None if row is None or row["endpoint"] is None else _shown(row)

    def listed(self, org: str) -> list[dict[str, Any]]:
        """`org`'s connections, by agent."""
        org = _org(org)
        with self.store._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT * FROM agent_connections WHERE org = ? AND endpoint IS NOT NULL "
                "ORDER BY pack_id",
                (org,),
            )
        return [_shown(row) for row in rows]

    def set(
        self,
        org: str,
        pack_id: str,
        *,
        endpoint: str,
        by: str,
        token: Any = KEEP,
        allow_origin: bool = False,
    ) -> dict[str, Any]:
        """Set or change `org`'s connection for an agent: `endpoint` as `admitted` accepts it;
        `token` a bearer token to send with each call, None for none, or left out (`KEEP`) to
        keep the one there is, which holds only while the endpoint is exactly the one it was
        set for. Chained `agent_connection.set` (the agent, the origin, whether it carries a
        token), unless nothing changed. A token is refused where there is no key to seal it
        under, before anything is written.

        `allow_origin`: `by` is an operator of the deployment (the route checked) and allows
        this endpoint's origin for this agent in `org`, where the deployment's allow-list does
        not name it. Chained `agent_connection.origin_allowed` (the agent, the origin, who)
        before the connection's own event. Without it, an origin off the list is accepted only
        if one was allowed for this connection before and it is the same one."""
        org, pack_id, by = _org(org), _agent(pack_id), _actor(by)
        with self.store._db.session(exclusive=False) as db:
            held = db.one(
                "SELECT allowed_origin FROM agent_connections WHERE org = ? AND pack_id = ? "
                "AND endpoint IS NOT NULL",
                (org, pack_id),
            )
        endpoint, origin, how = admitted(
            endpoint,
            allowed_origin=None if held is None else held["allowed_origin"],
            allow=allow_origin,
        )
        if token is not KEEP and token is not None:
            if not isinstance(token, str) or not _TOKEN.fullmatch(token):
                raise ConnectionRefused(
                    "a token is letters, digits and punctuation on one line, with no space"
                )
            if len(token) > MAX_TOKEN_CHARS:
                raise ConnectionRefused(f"a token is at most {MAX_TOKEN_CHARS} characters")
            if not self.seals:
                raise ConnectionRefused(
                    "a token is kept sealed under this deployment's PHI key, and it has none: "
                    "nothing was saved. Whoever runs this deployment sets MENDARI_PHI_MODE=encrypt "
                    "with MENDARI_PHI_KEY (docs/deploying-with-phi.md)",
                    409,
                )
        with self.store._db.session(exclusive=True) as db:
            row = db.one(
                "SELECT * FROM agent_connections WHERE org = ? AND pack_id = ?", (org, pack_id)
            )
            connection_id = str(uuid.uuid4()) if row is None else str(row["id"])
            before = None if row is None else row["endpoint"]
            sealed = None if row is None else row["token"]
            set_by = None if row is None else row["token_set_by"]
            set_at = None if row is None else row["token_set_at"]
            now = _now()
            if token is KEEP:
                if before != endpoint:  # a token never follows its endpoint to another
                    sealed = set_by = set_at = None
            elif token is None:
                sealed = set_by = set_at = None
            else:
                try:
                    self.store._refuse_retired(db)  # a retired key seals nothing more
                except MendariError as error:
                    raise ConnectionRefused(str(error), 409) from None
                sealed = self.store.phi.seal("connection", token, connection_id, org, pack_id)
                set_by, set_at = by, now
            # What an operator allowed stays with its origin: kept while the connection is
            # still at it, given now, or gone. Read again under the lock: one allowed before,
            # and cleared or moved since, allows nothing.
            kept = row is not None and before is not None and row["allowed_origin"] == origin
            if how != ALLOWED:
                allowance: tuple[Any, ...] = (None, None, None)
            elif kept:
                assert row is not None
                allowance = (origin, row["allowed_by"], row["allowed_at"])
            elif allow_origin:
                allowance = (origin, by, now)
            else:
                raise ConnectionRefused(NOT_LISTED.format(origin=origin), code="origin_not_listed")
            unchanged = (
                row is not None
                and before == endpoint
                and row["token"] == sealed
                and token is KEEP
                and row["allowed_origin"] == allowance[0]
            )
            if unchanged:
                assert row is not None
                return _shown(row)
            db.all(
                "INSERT INTO agent_connections (id, org, pack_id, endpoint, token, token_set_by, "
                "token_set_at, changed_by, changed_at, allowed_origin, allowed_by, allowed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (org, pack_id) DO UPDATE SET endpoint = excluded.endpoint, "
                "token = excluded.token, token_set_by = excluded.token_set_by, "
                "token_set_at = excluded.token_set_at, changed_by = excluded.changed_by, "
                "changed_at = excluded.changed_at, allowed_origin = excluded.allowed_origin, "
                "allowed_by = excluded.allowed_by, allowed_at = excluded.allowed_at",
                (
                    connection_id,
                    org,
                    pack_id,
                    endpoint,
                    sealed,
                    set_by,
                    set_at,
                    by,
                    now,
                    *allowance,
                ),
            )
            if how == ALLOWED and not kept:
                granted = {"pack_id": pack_id, "origin": origin, "allowed_by": by}
                self.store._append(
                    db,
                    f"agent_connection:{connection_id}",
                    "agent_connection.origin_allowed",
                    self.store._bound("agent_connection.origin_allowed", granted, org),
                )
            payload = {
                "pack_id": pack_id,
                "origin": origin,
                "token": sealed is not None,
                "changed_by": by,
            }
            chained = self.store._bound("agent_connection.set", payload, org)
            self.store._append(
                db, f"agent_connection:{connection_id}", "agent_connection.set", chained
            )
            made = db.one("SELECT * FROM agent_connections WHERE id = ?", (connection_id,))
        assert made is not None
        return _shown(made)

    def clear(self, org: str, pack_id: str, *, by: str) -> None:
        """Clear `org`'s connection for an agent: its endpoint and its token are gone from the
        row (which stays: its events name it), with what an operator allowed for it, and the
        agent answers as it did before one was set. Chained `agent_connection.cleared`. A
        `ConnectionRefused` (404) when there is none."""
        org, pack_id, by = _org(org), _agent(pack_id), _actor(by)
        with self.store._db.session(exclusive=True) as db:
            row = db.one(
                "SELECT id, endpoint FROM agent_connections WHERE org = ? AND pack_id = ?",
                (org, pack_id),
            )
            if row is None or row["endpoint"] is None:
                raise ConnectionRefused("this agent has no connection set here", 404)
            db.all(
                "UPDATE agent_connections SET endpoint = NULL, token = NULL, token_set_by = NULL, "
                "token_set_at = NULL, allowed_origin = NULL, allowed_by = NULL, "
                "allowed_at = NULL, changed_by = ?, changed_at = ? WHERE id = ?",
                (by, _now(), row["id"]),
            )
            chained = self.store._bound(
                "agent_connection.cleared", {"pack_id": pack_id, "changed_by": by}, org
            )
            self.store._append(
                db, f"agent_connection:{row['id']}", "agent_connection.cleared", chained
            )

    def resolve(self, org: str, pack_id: str) -> Connection | None:
        """What a live call of `org`'s for an agent uses: its endpoint and its token, opened;
        None when it has no connection. A token that does not open with this deployment's keys
        is a `MendariError`: the caller fails the call, and never falls back to another
        endpoint."""
        with self.store._db.session(exclusive=False) as db:
            row = db.one(
                "SELECT id, endpoint, token, allowed_origin FROM agent_connections "
                "WHERE org = ? AND pack_id = ?",
                (org, pack_id),
            )
        if row is None or row["endpoint"] is None:
            return None
        token = None
        if row["token"] is not None:
            opened, _ = self.store.phi.open(
                "connection", str(row["token"]), str(row["id"]), org, pack_id
            )
            token = str(opened)
        return Connection(endpoint=str(row["endpoint"]), token=token, allowed=_allowed(row))
