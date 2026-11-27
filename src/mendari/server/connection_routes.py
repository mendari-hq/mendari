# SPDX-License-Identifier: Apache-2.0
"""An agent's connection, in the console (ADR-0021, `core/connections.py`): the service that
answers for an agent in one organization, read by anyone who may read, and set, changed, cleared
and checked by that organization's administrators.

- `GET /api/connections` (`read`): each agent the organization is served: whether it has a
  service to connect, where its answers come from now, and its connection.
- `GET /api/connections/{namespace}/{name}` (`read`): one agent's, slot by slot.
- `PUT /api/connections/{namespace}/{name}` (`keys`): set or change it.
- `DELETE /api/connections/{namespace}/{name}` (`keys`): clear it.
- `POST /api/connections/{namespace}/{name}/check` (`keys`): send one synthetic example and say
  what came back.

`keys` is an organization's administrators, people only (`server/auth.py`): never an API key, a
client or an EHR launch, so no machine's credential can point an agent's cases somewhere else
or plant a token. Each route acts for the organization the guard let the caller through for
(`org_of`), never one a body, a query or a path names, and an agent is one that organization is
served or holds in its agent store: any other answers as an agent that does not exist.

**An address the deployment's list does not name.** Where the deployment lists the origins it
may call (`MENDARI_PROVIDER_ALLOWED_ORIGINS`) and a connection's is not one of them, the `PUT`
is refused, unless the caller is an administrator, signed in as a person, of the organization
the deployment names as its operator (`MENDARI_OPERATOR_ORG`) and sends `allow_origin: true`.
That allows the origin for this agent in their organization, chained
`agent_connection.origin_allowed`, and it leads to public addresses only, as on a deployment
with no list. `allow_origin` from anyone else is a `403`, and changes nothing. A deployment
that names no operator organization has nobody who may allow one (`may_allow`): where sign-in
is by claims every administrator holds `operate` then, and going round the deployment's own
list is not every administrator's to do. The refusal says what to set.

**No answer holds a token.** A connection is shown as where it leads and who set it when
(`token: "set"`, with who set that and when); the endpoint's path is shown to those who may set
it, its origin to every reader. The security log names the agent and the actor, and for a
refusal a fixed reason. With `MENDARI_AGENT_CONNECTIONS=off` the reads still answer (they say
where each agent's answers come from) and every write is a `409`.

**The check** sends the agent's first behaviour test (a synthetic example the agent carries,
never a decision's input) to each of its `agent` variants, as a decision would, with what is
saved: nothing is recorded, and the answer is said in fixed words (`SAID`), with how long it
took and the model and version the service reports (ids only). Nothing the service answered is
passed on."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from mendari.core.agent import Agent
from mendari.core.connections import (
    KEEP,
    NOT_LISTED_NOBODY,
    SWITCH_ENV,
    ConnectionRefused,
    ConnectionStore,
)
from mendari.core.spec import is_agent_id
from mendari.errors import MendariError, ProviderError
from mendari.evals.behaviour_tests import load_cases
from mendari.providers.agent import ENDPOINT_ENV, AgentProvider
from mendari.providers.deadline import decision_deadline
from mendari.providers.http import allowed_origins
from mendari.server import security_log
from mendari.server.auth import Auth, Principal, org_of
from mendari.standards.fhir_client import origin_of

TAG = "Agents"
CHECK_DEADLINE_S = 20.0  # one check, each of its calls: a person is waiting
NOT_AN_OPERATOR = (
    "only an administrator of the organization that operates this deployment, signed in as a "
    "person, may allow an address its list does not name. Nothing was changed"
)
NO_OPERATOR_NAMED = (
    "this deployment names no organization as its operator, so nobody may allow an address its "
    "list does not name. Whoever runs it sets MENDARI_OPERATOR_ORG to the organization whose "
    "administrators may, or adds the address's origin to MENDARI_PROVIDER_ALLOWED_ORIGINS. "
    "Nothing was changed"
)
OFF = (
    f"connections are switched off on this deployment ({SWITCH_ENV}=off): its agents answer "
    "through the service whoever runs it configured"
)
NO_SERVICE = "this agent has no service to connect: it decides on this server, with nothing to call"
# What a check found, in words a person reads; by the provider's fixed code, never its detail.
SAID: Mapping[str, str] = {
    "ok": "It answered this agent's questions for a synthetic example.",
    "canned": (
        "No live service is connected: it answered from the canned examples the agent "
        "carries, so a person decides every case."
    ),
    "timeout": "It did not answer in time.",
    "credential_unavailable": "It refused the request: the token is missing, wrong or expired.",
    "unreadable": (
        "The saved token could not be opened with this deployment's keys: set the token again."
    ),
    "provider_unavailable": "It could not be reached, or it answered with an error.",
    "invalid_response": "It answered, but not in the form this agent's questions need.",
    "invalid_endpoint": "Its address is not one this deployment may call.",
    "rate_limited": "It is turning requests away for now: too many requests.",
    "insufficient_credit": "It refused the request for lack of credit.",
    "not_configured": "No live service is connected, and the agent carries no canned examples.",
    "invalid_input": (
        "The example could not be sent: it is larger than a request may be, or holds a value "
        "JSON cannot carry."
    ),
}
OTHER = "It could not answer."


class ConnectionRequest(BaseModel):
    endpoint: str = Field(
        max_length=2048,
        description="where the agent's service answers: https (plain http to this machine "
        "only), with no user, password, query or fragment",
        examples=["https://agents.example.org/decide"],
    )
    token: str | None = Field(
        default=None,
        max_length=4096,
        description="the bearer token to send with each call. Write-only: no route returns "
        "it. Leave it out to keep the one that is set (which holds only while the endpoint "
        "stays the same); send null to remove it",
    )
    allow_origin: bool = Field(
        default=False,
        description="allow this endpoint's origin for this agent in your organization, where "
        "the deployment's list of addresses it may call does not name it. Only an "
        "administrator of the organization the deployment names as its operator "
        "(`MENDARI_OPERATOR_ORG`), signed in as a person, may send it (`403` otherwise, and "
        "from everyone where it names none); the endpoint must still lead to public "
        "addresses only, and what was allowed is chained",
    )


Providers = Callable[[str, str], tuple[Agent, list[AgentProvider]] | None]


def add_connection_routes(
    app: FastAPI,
    auth: Auth,
    connections: ConnectionStore,
    *,
    enabled: bool,
    agents: Callable[[str], list[str]],
    providers: Providers,
    changed: Callable[[], None],
) -> None:
    """Mount the connection routes. `enabled`: whether organizations may set connections here
    (`MENDARI_AGENT_CONNECTIONS`); `agents(org)`: the ids of the agents `org` is served;
    `providers(org, agent_id)`: an agent `org` is served or holds in its store, with the `agent`
    variants it runs (a composite's members too), or None for any other; `changed()`: what was
    read of connections is forgotten (a change takes effect at once on this instance)."""

    def may_set(principal: Principal) -> bool:
        return principal.may("keys")

    def may_allow(principal: Principal) -> bool:
        """Whether this caller may allow an origin the deployment's list does not name: an
        administrator, signed in as a person, of the organization the deployment names as its
        operator (`MENDARI_OPERATOR_ORG`). It fails closed: with no organization named nobody
        may, though every administrator holds `operate` then where sign-in is by claims
        (ADR-0022). Never open mode, where nobody signs in: whoever runs such a server lists
        the origin in its settings."""
        return (
            auth.operator_org is not None and principal.kind == "user" and principal.may("operate")
        )

    def shown(row: Mapping[str, Any] | None, principal: Principal) -> dict[str, Any] | None:
        """A connection as this caller sees it: its origin, who set it and when, whether it
        carries a token; and, for who may set it, the whole endpoint."""
        if row is None:
            return None
        seen = {key: value for key, value in row.items() if key not in ("agent_id", "endpoint")}
        return {**seen, "endpoint": row["endpoint"]} if may_set(principal) else seen

    def slots(found: list[AgentProvider]) -> list[dict[str, Any]]:
        """Where each `agent` variant's answers come from now (`AgentProvider.live`'s order,
        asked of the provider itself), and whether it carries canned examples."""
        listed = []
        for leaf in found:
            try:
                live = leaf.live()
                source, problem = ("none" if live is None else live.source), None
            except ProviderError as error:
                source, problem = "organization", error.code
            listed.append(
                {
                    "slot": leaf.info.slot,
                    "variant": leaf.info.variant,
                    "source": source,
                    "canned_examples": leaf.has_canned_examples,
                    **({"problem": problem} if problem else {}),
                }
            )
        return listed

    def state(org: str, agent_id: str, principal: Principal) -> dict[str, Any] | None:
        """One agent's connection state, or None for an agent `org` neither is served nor
        holds."""
        found = providers(org, agent_id)
        if found is None:
            return None
        listed = slots(found[1])
        sources = {slot["source"] for slot in listed}
        return {
            "agent_id": agent_id,
            # Whether the agent has a service to connect at all (an `agent` variant in use).
            "applies": bool(listed),
            # Where its answers come from now: one source, or `mixed` when its slots differ.
            "source": None if not listed else sources.pop() if len(sources) == 1 else "mixed",
            # No live service for some slot: it answers from canned examples there, which never
            # automate where people sign in.
            "canned": any(slot["source"] == "none" for slot in listed),
            "slots": listed,
            "connection": shown(connections.get(org, agent_id) if enabled else None, principal),
        }

    def deployment(principal: Principal) -> dict[str, Any]:
        """What the deployment allows, for the form: whether connections may be set here,
        whether a token can be kept, the origins it may call (null: no list, so the public
        internet only), and whether it has an endpoint of its own for every agent."""
        try:
            listed = allowed_origins()
        except MendariError:
            listed = frozenset()
        return {
            "enabled": enabled,
            "may_set": enabled and may_set(principal),
            "seals": connections.seals,
            "allowed_origins": None if listed is None else sorted(listed),
            # Whether this caller may allow an origin that list does not name, for an agent
            # here (`allow_origin`); false where there is no list: nothing needs allowing.
            "may_allow_origin": enabled and listed is not None and may_allow(principal),
            # Whether the deployment names the organization whose administrators may: where
            # it names none, nobody allows an origin, and the form says what to set.
            "operator_named": auth.operator_org is not None,
            "deployment_endpoint": bool(os.environ.get(ENDPOINT_ENV, "").strip()),
        }

    def agent_of(request: Request, namespace: str, name: str) -> tuple[Principal, str, str]:
        principal = auth.acting(request)
        agent_id = f"{namespace}/{name}"
        if not is_agent_id(agent_id):
            raise HTTPException(404, "no such agent here")
        return principal, org_of(principal), agent_id

    def writable(request: Request, namespace: str, name: str) -> tuple[Principal, str, str]:
        """The caller, their organization and the agent, for a route that sets, clears or checks
        a connection: refused where connections are off, for an agent that is not theirs and
        for one with no service to connect."""
        principal, org, agent_id = agent_of(request, namespace, name)
        if principal.kind not in ("user", "open"):  # the guard lets people only through
            raise HTTPException(403, "only a person who signed in may do this")
        found = providers(org, agent_id)
        if found is None:
            raise HTTPException(404, "no such agent here")
        if not found[1]:
            raise HTTPException(409, NO_SERVICE)
        return principal, org, agent_id

    @app.get("/api/connections", tags=[TAG], summary="Your organization's agent connections")
    def list_connections(request: Request) -> dict[str, Any]:
        """For each agent your organization is served: whether it has a service to connect
        (`applies`: an `agent` variant in use), where its answers come from now (`source`:
        `organization`, a connection set here; `deployment`, the deployment's own endpoint;
        `agent`, an endpoint its own manifest names; `none`, its canned examples, which never
        automate where people sign in), and its connection: where it leads, who set it and
        when, and whether it carries a token. Never the token. With it, what the deployment
        allows (`enabled`, `seals`, `allowed_origins`, `may_allow_origin`: whether you may
        allow an address that list does not name, and `operator_named`: whether the deployment
        names the organization whose administrators may; where it names none, nobody does)."""
        principal = auth.acting(request)
        org = org_of(principal)
        listed = [state(org, agent_id, principal) for agent_id in sorted(agents(org))]
        return {**deployment(principal), "agents": [item for item in listed if item is not None]}

    @app.get(
        "/api/connections/{namespace}/{name}",
        tags=[TAG],
        summary="One agent's connection",
    )
    def get_connection(request: Request, namespace: str, name: str) -> dict[str, Any]:
        """One agent's connection state in your organization, as `GET /api/connections` lists
        it, for an agent you are served or hold in your agent store. Any other agent, another
        organization's included, is a `404`."""
        principal, org, agent_id = agent_of(request, namespace, name)
        found = state(org, agent_id, principal)
        if found is None:
            raise HTTPException(404, "no such agent here")
        return {**deployment(principal), **found}

    @app.put(
        "/api/connections/{namespace}/{name}",
        tags=[TAG],
        summary="Set an agent's connection",
    )
    def set_connection(
        request: Request, namespace: str, name: str, body: ConnectionRequest
    ) -> dict[str, Any]:
        """Set or change where this agent's service answers for your organization, and the
        bearer token to send it. From then on its decisions here call that service, before the
        deployment's own endpoint; nothing else of the agent changes, and it automates only as
        its policy, its approvals and its holds allow.

        The endpoint must be one this deployment may call: on its provider allow-list where it
        has one, else on the public internet only (`422` saying which rule refused it). An
        origin the list does not name is accepted when an administrator of the organization
        the deployment names as its operator sends `allow_origin: true` (`403` from anyone
        else, and from everyone where the deployment names none): it is then allowed for this
        agent in your organization, to public addresses only, and chained
        `agent_connection.origin_allowed`. A token is kept sealed and never shown
        again; a deployment with no key to seal it under refuses one (`409`). Changing the
        endpoint drops the token unless a new one is sent with it, and what was allowed holds
        for its origin alone. Chained `agent_connection.set`: who, when, the agent and the
        endpoint's origin."""
        if not enabled:
            raise HTTPException(409, OFF)
        principal, org, agent_id = writable(request, namespace, name)
        if body.allow_origin and not may_allow(principal):
            unnamed = auth.operator_org is None and auth.enabled
            security_log.event(
                "admin.connection.refused",
                outcome="refused",
                reason="no_operator_named" if unnamed else "not_operator",
                actor=principal.id,
                org=org,
                ids=[agent_id],
            )
            raise HTTPException(403, NO_OPERATOR_NAMED if unnamed else NOT_AN_OPERATOR)
        token: Any = body.token if "token" in body.model_fields_set else KEEP
        before = connections.get(org, agent_id)
        try:
            made = connections.set(
                org,
                agent_id,
                endpoint=body.endpoint,
                token=token,
                by=principal.actor,
                allow_origin=body.allow_origin,
            )
        except ConnectionRefused as error:
            security_log.event(
                "admin.connection.refused",
                outcome="refused",
                reason=error.code or "checks",
                actor=principal.id,
                org=org,
                ids=[agent_id],
            )
            said = str(error)
            if error.code == "origin_not_listed" and auth.enabled and auth.operator_org is None:
                # Nobody here may allow it: say what to set, not who to ask.
                said = NOT_LISTED_NOBODY.format(origin=origin_of(body.endpoint))
            raise HTTPException(error.status, said) from None
        changed()
        if made["origin_allowed_at"] and made["origin_allowed_at"] != (before or {}).get(
            "origin_allowed_at"
        ):
            security_log.event(
                "admin.connection.origin_allowed", actor=principal.id, org=org, ids=[agent_id]
            )
        security_log.event("admin.connection.set", actor=principal.id, org=org, ids=[agent_id])
        return {"agent_id": agent_id, "connection": shown(made, principal)}

    @app.delete(
        "/api/connections/{namespace}/{name}",
        status_code=204,
        tags=[TAG],
        summary="Clear an agent's connection",
    )
    def clear_connection(request: Request, namespace: str, name: str) -> Response:
        """Clear this agent's connection in your organization: its endpoint and its token are
        removed, and the agent answers as it did before one was set (the deployment's own
        endpoint, else its canned examples). Chained `agent_connection.cleared`."""
        if not enabled:
            raise HTTPException(409, OFF)
        principal, org, agent_id = agent_of(request, namespace, name)
        if principal.kind not in ("user", "open"):
            raise HTTPException(403, "only a person who signed in may do this")
        try:
            connections.clear(org, agent_id, by=principal.actor)
        except ConnectionRefused as error:
            raise HTTPException(error.status, str(error)) from None
        changed()
        security_log.event("admin.connection.cleared", actor=principal.id, org=org, ids=[agent_id])
        return Response(status_code=204)

    @app.post(
        "/api/connections/{namespace}/{name}/check",
        tags=[TAG],
        summary="Check an agent's connection",
    )
    def check_connection(request: Request, namespace: str, name: str) -> dict[str, Any]:
        """Send one synthetic example (the agent's first behaviour test, never a patient's case)
        to each service this agent would call for your organization now, and say what came
        back: `ok`, how long it took, and the model and version the service reports. A failure
        is said in fixed words (`said`) with its code (`problem`); nothing the service answered
        is repeated. Nothing is recorded, and no decision is made."""
        principal, org, agent_id = writable(request, namespace, name)
        found = providers(org, agent_id)
        assert found is not None  # `writable` found it
        agent, leaves = found
        example = _example(agent)
        if example is None:
            raise HTTPException(409, "this agent carries no synthetic example to check with")
        case, inputs = example
        checked = [_checked(leaf, inputs) for leaf in leaves]
        passed = all(item["ok"] for item in checked)
        security_log.event(
            "admin.connection.checked",
            outcome="ok" if passed else "error",
            actor=principal.id,
            org=org,
            ids=[agent_id],
        )
        return {"agent_id": agent_id, "ok": passed, "example": case, "checked": checked}


def _example(agent: Agent) -> tuple[str, dict[str, Any]] | None:
    """The agent's first behaviour test with an input: its name and that input (synthetic: an
    agent's tests are part of the agent, published with it)."""
    try:
        cases = load_cases(agent)
    except MendariError:
        return None
    for case in cases:
        if isinstance(case, dict) and isinstance(case.get("input"), dict):
            return str(case.get("name") or "the agent's first example")[:200], case["input"]
    return None


def _checked(leaf: AgentProvider, inputs: Mapping[str, Any]) -> dict[str, Any]:
    """One `agent` variant asked once, as a decision asks it: whether it answered, how long it
    took, where the answer came from, and the model and version it reports."""
    started = time.perf_counter()
    answered: dict[str, Any] = {"slot": leaf.info.slot, "variant": leaf.info.variant}
    try:
        with decision_deadline(CHECK_DEADLINE_S):
            live = leaf.live()
            answered["source"] = "none" if live is None else live.source
            leaf.preflight()
            output = leaf.answer(inputs)
    except ProviderError as error:
        unreadable = error.code == "credential_unavailable" and "source" not in answered
        code = "unreadable" if unreadable else error.code
        answered.setdefault("source", "organization")
        return {
            **answered,
            "ok": False,
            "problem": code,
            "said": SAID.get(code, OTHER),
            "latency_ms": round((time.perf_counter() - started) * 1000),
        }
    latency = round((time.perf_counter() - started) * 1000)
    if output.canned is not None:
        return {
            **answered,
            "ok": False,
            "problem": "canned",
            "said": SAID["canned"],
            "latency_ms": latency,
        }
    return {
        **answered,
        "ok": True,
        "said": SAID["ok"],
        "latency_ms": latency,
        "model": output.extra.get("model"),
        "service_version": output.extra.get("service_version"),
    }
