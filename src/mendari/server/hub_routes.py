# SPDX-License-Identifier: Apache-2.0
"""Finding and adding an agent from the console: the deployment's registry (the Agent Hub, or a
site's own), searched and read here, and a version of it added to an organization.

`GET /api/hub` is always served and says what is configured. The rest is mounted only where
`MENDARI_REGISTRY_URL` names a registry (`settings.registry_url`), and adding only where
organizations' agent stores are served too (`MENDARI_AGENT_STORE=on`, memberships mode).

- **Reads** (`GET /api/hub/search`, `GET /api/hub/agents/{namespace}/{name}`, any reader): what
  the registry lists, read from it at each call with a short timeout and never stored. A
  registry's words are someone else's: every answer is rebuilt here from the fields this file
  names, each held to a kind and a length (`_listing`), so nothing it sends reaches a page as
  anything but text of a known size, and its Decision Card is served as text. A registry that
  does not answer, answers an error or answers something that is not a registry's answer is one
  `502` in fixed words: none of its own are repeated.
- **Adding** (`POST /api/store/pulls`, the store's authors, people only): the version is
  downloaded and verified exactly as `mendari pull` verifies one, by the same code
  (`hub/client.py` `fetch`): its archive's SHA-256 against the registry's record, the publisher's
  signature by a key of its namespace, and the registry's counter-signature where it signs
  (`MENDARI_REGISTRY_COUNTERSIGNATURE`). The keys are pinned for the organization in the
  database (`core/pack_store.py` `OrgPins`), so a publisher's changed key is refused (`409`,
  naming the key ids) until an author accepts it. What was verified is then kept in the
  organization's agent store (`PackStore.admit_pulled`), under every check, limit and quota a
  version there passes: a draft, served to nobody until someone who did not add it promotes it.
  A recalled or deleted version, a version signed with a revoked key, a tampered archive and a
  registry that cannot be reached each add nothing.

Each route acts for the organization the guard let the caller through for (`org_of`), never one
a body, a query or a path names. The security log names the agent's id and digest only."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from mendari.core.pack import Pack
from mendari.core.pack_store import HELD, HUB, OrgPins, PackStore, PhiRejected, StoreError
from mendari.core.spec import is_pack_id, is_version
from mendari.errors import HubError, MendariError
from mendari.hub.client import RegistryClient, fetch, pinned_in, pinned_keys
from mendari.server import security_log
from mendari.server.auth import Auth, org_of
from mendari.server.store_routes import HELD_MESSAGE, _detail

TAG = "Agents"
STORE_TAG = "Agent store"
READ_TIMEOUT_S = 10.0  # a listing or a search: a person is waiting
MAX_RESULTS = 200
MAX_CARD_CHARS = 200_000
MAX_VERSIONS = 200
MAX_EVALUATIONS = 50
NO_ANSWER = (
    "the registry did not answer: check that this server can reach it, then try again. "
    "Nothing was changed"
)
NOT_A_REGISTRY = (
    "the registry answered something this server could not read as a registry's answer. "
    "Nothing was changed"
)
NOT_LISTED = "the registry lists no such agent, or no such version of it"
SEVERITIES = ("low", "medium", "high", "critical")
_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_KEY_CHANGED = re.compile(r"is signed by (ed25519:[0-9a-f]{16}), which this machine has not seen")
T = TypeVar("T")


class PullRequest(BaseModel):
    ref: str = Field(
        max_length=200,
        description="the agent to add: `namespace/name` (its newest version that is not "
        "recalled) or `namespace/name@version`",
        examples=["mendari/portal-message-agent@1.0.0"],
    )
    accept_new_key: bool = Field(
        default=False,
        description="accept a signing key of this publisher that your organization has not "
        "seen before (the publisher rotated its key): send it only after checking the new key "
        "with the publisher",
    )


# -- a registry's answer, as this server passes it on ---------------------------------------------


def _text(value: Any, limit: int = 300) -> str | None:
    """A registry's text, or None: one string, cut to `limit` characters."""
    return value[:limit] if isinstance(value, str) else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _recall(version: Mapping[str, Any]) -> dict[str, Any] | None:
    """A version's recall as the registry serves it (None: none): its severity from a closed
    set, its reason, who signed it and when; or that the version was deleted."""
    yank, deleted = version.get("yank"), version.get("deleted")
    if isinstance(yank, Mapping):
        stated = yank.get("severity")
        return {
            "severity": stated if stated in SEVERITIES else "unknown",
            "reason": _text(yank.get("reason"), 500),
            "signer": _text(yank.get("signer"), 40),
            "at": _text(yank.get("at"), 40),
            "deleted": isinstance(deleted, Mapping),
        }
    if isinstance(deleted, Mapping):
        return {
            "severity": "critical",
            "reason": _text(deleted.get("reason"), 500),
            "signer": "registry",
            "at": _text(deleted.get("at"), 40),
            "deleted": True,
        }
    return None


def _listing(version: Mapping[str, Any]) -> dict[str, Any]:
    """One version as the registry lists it, rebuilt field by field: what the registry checked
    itself (`checked`: the signature at publish, the digest, the engine, its review, its
    counter-signature, the publisher type its reviewers set) apart from what the publisher says
    (`claimed`: who it is, the risk level it declares, the licence, the summary)."""
    checked, claimed = _mapping(version.get("checked")), _mapping(version.get("claimed"))
    signature, review = _mapping(checked.get("signature")), _mapping(checked.get("review"))
    publisher, risk = _mapping(claimed.get("publisher")), _mapping(claimed.get("risk"))
    level = risk.get("level")
    found = review.get("risk_level")
    standards = version.get("standards")
    return {
        "id": _text(version.get("id"), 130),
        "name": _text(version.get("pack_name"), 200),
        "version": _text(version.get("version"), 60),
        "published_at": _text(version.get("published_at"), 40),
        "summary": _text(claimed.get("summary") or version.get("summary"), 1000),
        "standards": [s[:60] for s in standards if isinstance(s, str)][:20]
        if isinstance(standards, list)
        else [],
        "checked": {
            # The registry verified the publisher's signature when it took the version; this
            # server verifies it again when the version is added.
            "signature": {
                "algorithm": _text(signature.get("algorithm"), 20),
                "key_id": _text(signature.get("key_id"), 60),
                "verified": signature.get("verified") is True,
            },
            "digest": _text(checked.get("digest"), 100),
            "archive_sha256": _text(checked.get("archive_sha256"), 100),
            "engine": _text(checked.get("engine"), 20),
            "status_label": _text(checked.get("status_label"), 200),
            "publisher_type": _text(checked.get("publisher_type"), 40),
            "countersigned": isinstance(checked.get("accepted"), Mapping),
            "review": {
                "status": "reviewed" if review.get("status") == "reviewed" else "unreviewed",
                "risk_level": found if isinstance(found, int) and 0 <= found <= 9 else None,
                "matches_declared": review.get("matches_declared")
                if isinstance(review.get("matches_declared"), bool)
                else None,
            },
        },
        "claimed": {
            "publisher": {
                "name": _text(publisher.get("name"), 200),
                "type": _text(publisher.get("type"), 40),
            },
            "risk": {
                "level": level if isinstance(level, int) and 0 <= level <= 9 else None,
                "category": _text(risk.get("category"), 100),
            },
            "license": _text(claimed.get("license"), 100),
        },
        "recall": _recall(version),
    }


def _evaluations(answer: Any) -> dict[str, Any]:
    """Whether the publisher published evaluations of a version, and of what: how many, each
    one's dataset (its name and size), its models and whether the registry ran it again. No
    figure: the Hub's own page shows them with their intervals."""
    listed = answer.get("evaluations") if isinstance(answer, Mapping) else None
    if not isinstance(listed, list):
        return {"published": 0, "listed": []}
    shown = []
    for item in listed[:MAX_EVALUATIONS]:
        if not isinstance(item, Mapping):
            continue
        dataset = _mapping(item.get("dataset"))
        cases = dataset.get("n")
        models = item.get("models")
        shown.append(
            {
                "dataset": _text(dataset.get("name"), 100),
                "cases": cases if isinstance(cases, int) and cases >= 0 else None,
                "models": [m[:130] for m in models if isinstance(m, str)][:10]
                if isinstance(models, list)
                else [],
                "rerun": _text(item.get("rerun_words"), 200),
            }
        )
    return {"published": len(listed), "listed": shown}


def add_hub_routes(
    app: FastAPI,
    auth: Auth,
    *,
    registry: str | None,
    served: Callable[[], Mapping[str, Pack]],
    served_here: Callable[[], Mapping[str, Pack]],
    store: PackStore | None,
    hidden: Any = None,
) -> None:
    """Mount the console's registry routes. `registry`: the deployment's registry address, or
    None (then only `GET /api/hub` is served); `served`: the agents this deployment serves from
    its own files, whoever asks; `served_here`: the agents the request's organization is served
    (those, and its promoted store versions); `store`: organizations' agent stores, or None
    where they are off (then nothing is added here); `hidden`: as the store's own routes take
    it."""

    @app.get("/api/hub", tags=[TAG], summary="The registry agents are added from")
    def hub(request: Request) -> dict[str, Any]:
        """Whether this deployment names a registry to find agents in (`registry`: its address,
        or null), and whether an agent found there can be added to your organization from here
        (`add.available`); when it cannot, `add.reason` says what the deployment lacks. Adding
        also needs the store's author role (`author` in `/api/auth/me`)."""
        auth.acting(request)
        if registry is None:
            reason = "no_registry"
        elif store is None:
            reason = "no_store"
        else:
            reason = None
        return {
            "registry": None if registry is None else _shown(registry),
            "add": {"available": reason is None, "reason": reason},
        }

    if registry is None:
        return
    address = registry

    def asked(run: Callable[[RegistryClient], T], *, timeout: float = READ_TIMEOUT_S) -> T:
        """What `run` reads from the registry, or the one refusal of a registry that could not
        be read: its own words are never passed on."""
        try:
            with RegistryClient(address, timeout=timeout) as client:
                return run(client)
        except HubError as error:
            if error.status in (404, 410):
                raise HTTPException(404, NOT_LISTED) from None
            raise HTTPException(502, NOT_A_REGISTRY) from None
        except httpx.HTTPError:
            raise HTTPException(502, NO_ANSWER) from None
        except (MendariError, TypeError, ValueError, KeyError, AttributeError):
            raise HTTPException(502, NOT_A_REGISTRY) from None

    def installed(org: str, pack_id: str | None, mine: Mapping[str, Pack]) -> dict[str, Any] | None:
        """What `org` has of an agent already: the version it is served, or the newest version
        in its store and that version's state. None: nothing."""
        if pack_id is None:
            return None
        pack = mine.get(pack_id)
        if pack is not None:
            return {"version": pack.version, "digest": pack.digest, "state": "served"}
        if store is None:
            return None
        for row in store.versions(org, pack_id):
            if row["state"] != "withdrawn":
                return {"version": row["version"], "digest": row["digest"], "state": row["state"]}
        return None

    @app.get("/api/hub/search", tags=[TAG], summary="Search the registry for agents")
    def hub_search(
        request: Request,
        q: str = Query(default="", max_length=200, description="words to find"),
        risk: int | None = Query(default=None, ge=0, le=9, description="a declared risk level"),
        standard: str = Query(default="", max_length=60, description="e.g. FHIR, CDS Hooks"),
        publisher_type: str = Query(default="", max_length=40),
    ) -> dict[str, Any]:
        """The agents the deployment's registry lists that match, newest version of each: what
        the registry checked and what the publisher claims, apart (`checked`, `claimed`), any
        recall, and `installed`: what your organization has of that agent already (the version
        it is served, or the state of the one in its store), or null. Read from the registry
        now; a registry that does not answer is a `502`."""
        org = org_of(auth.acting(request))
        found = asked(
            lambda client: client.search(
                q=q, risk=risk, standard=standard, publisher_type=publisher_type
            )
        )
        mine = served_here()
        agents = []
        for item in found[:MAX_RESULTS]:
            if not isinstance(item, Mapping):
                continue
            shown = _listing(item)
            if shown["id"] is None or not is_pack_id(shown["id"]):
                continue
            agents.append({**shown, "installed": installed(org, shown["id"], mine)})
        return {"registry": _shown(address), "agents": agents, "more": len(found) > MAX_RESULTS}

    @app.get(
        "/api/hub/agents/{namespace}/{name}",
        tags=[TAG],
        summary="An agent as the registry lists it",
    )
    def hub_agent(
        request: Request,
        namespace: str,
        name: str,
        version: str | None = Query(default=None, max_length=60, description="else the newest"),
    ) -> dict[str, Any]:
        """One agent of the deployment's registry: the version asked for, or its newest that is
        not recalled, as `GET /api/hub/search` lists one, with its Decision Card as the registry
        rendered it (`card_markdown`: text, to show as text), the versions it lists
        (`versions`) and the ones it recalled (`recalled`), whether the publisher published
        evaluations of it (`evaluations`), and what your organization has of it (`installed`).
        `404` for an agent or version the registry does not list."""
        org = org_of(auth.acting(request))
        if not _SEGMENT.fullmatch(namespace) or not _SEGMENT.fullmatch(name):
            raise HTTPException(404, NOT_LISTED)
        if version is not None and not is_version(version):
            raise HTTPException(404, NOT_LISTED)
        pack_id = f"{namespace}/{name}"

        def read(client: RegistryClient) -> dict[str, Any]:
            listed = client.agent(namespace, name)
            if version is not None:
                shown = client.version(namespace, name, version)
            elif isinstance(listed.get("latest"), Mapping):
                shown = dict(listed["latest"])
            else:
                shown = None  # every version is recalled: nothing to show but the recalls
            evaluations: Any = None
            if shown is not None and is_version(shown.get("version")):
                try:
                    evaluations = client.evaluations(namespace, name, str(shown["version"]))
                except (HubError, httpx.HTTPError):
                    evaluations = None  # a registry from before them, or one that did not say
            return {"listed": listed, "shown": shown, "evaluations": evaluations}

        found = asked(read)
        listed, shown = found["listed"], found["shown"]
        versions = listed.get("versions")
        recalled = listed.get("recalled")
        answer: dict[str, Any] = {
            "registry": _shown(address),
            "id": pack_id,
            "versions": [v[:60] for v in versions if isinstance(v, str)][:MAX_VERSIONS]
            if isinstance(versions, list)
            else [],
            "recalled": [
                {"version": _text(item.get("version"), 60), **(_recall(item) or {})}
                for item in (recalled if isinstance(recalled, list) else [])[:MAX_VERSIONS]
                if isinstance(item, Mapping)
            ],
            "installed": installed(org, pack_id, served_here()),
            "agent": None,
        }
        if shown is not None:
            answer["agent"] = {
                **_listing({**shown, "id": pack_id}),
                "card_markdown": _text(shown.get("card_markdown"), MAX_CARD_CHARS),
                "evaluations": _evaluations(found["evaluations"]),
            }
        return answer

    if store is None:
        return
    kept = store

    @app.post(
        "/api/store/pulls",
        status_code=201,
        tags=[STORE_TAG],
        summary="Add an agent from the registry",
    )
    def pull_agent(request: Request, body: PullRequest) -> JSONResponse:
        """Add a version of an agent from the deployment's registry to your organization's agent
        store. It is downloaded and verified first, as `mendari pull` verifies one: its
        archive against the registry's record, the publisher's signature, and the registry's
        counter-signature where it signs. Then it is kept as a draft, under every check a
        version in the store passes: it answers no request until someone who did not add it
        promotes it, and never automates until your organization approves its digest.

        Nothing is added, and the answer says why, when the registry does not answer (`502`),
        the version is recalled or deleted (`409`, `404`), its archive or signature does not
        verify (`422`), the publisher's signing key is one your organization has not seen
        (`409` with `key_changed`: check it with the publisher, then send `accept_new_key`), your
        organization holds that version already (`409`), or a check, limit or quota of the
        store refuses it. Answers the new version (`201`), `held` if it is held for the
        operator's review. Chained `pack_version.created`."""
        principal = auth.acting(request)
        if principal.kind != "user":  # the guard lets people only through: fail closed
            raise HTTPException(403, "only a person who signed in may do this")
        by, org = principal.id, org_of(principal)
        pack_id, at, wanted = body.ref.partition("@")
        if not is_pack_id(pack_id) or (at and not is_version(wanted)):
            raise HTTPException(422, "name the agent as namespace/name or namespace/name@version")
        namespace = pack_id.partition("/")[0]
        if pack_id in served():
            raise HTTPException(
                409,
                f"this deployment serves {pack_id} from its own files: a version from the "
                "registry never replaces it",
            )
        try:
            # The day's quota and the stop first: nothing is downloaded for a refused request.
            kept.refuse_before_adding(org)
        except StoreError as error:
            raise HTTPException(error.status, str(error)) from None
        pins = OrgPins(kept.store, org, by)

        def refused(reason: str) -> None:
            security_log.event(
                "store.pull.refused", outcome="refused", reason=reason, actor=by, org=org
            )

        try:
            with RegistryClient(address) as client, pinned_in(pins):
                fetched = fetch(body.ref, client, accept_new_key=body.accept_new_key)
        except HubError as error:
            if error.status in (404, 410):
                refused("not_listed")
                raise HTTPException(404, NOT_LISTED) from None
            changed = _KEY_CHANGED.search(str(error))
            if changed is not None:
                refused("key_changed")
                return JSONResponse(
                    {
                        "detail": "this publisher signed the version with a key your "
                        "organization has not seen before. Check the new key with the "
                        "publisher before you accept it",
                        "key_changed": {
                            "namespace": namespace,
                            "key_id": changed[1],
                            "pinned": pinned_keys(pins, address, namespace),
                        },
                    },
                    status_code=409,
                )
            refused("verification")
            # Mendari's own words for what it checked (`hub/client.py`, `hub/archive.py`): the
            # archive, the signature, the counter-signature, a revoked key.
            raise HTTPException(422, f"not added: {_own_words(error)}") from None
        except httpx.HTTPError:
            refused("unreachable")
            raise HTTPException(502, NO_ANSWER) from None
        except MendariError:
            refused("verification")
            raise HTTPException(422, "not added: the agent does not load here") from None
        except (TypeError, ValueError, KeyError, AttributeError):
            refused("registry_answer")
            raise HTTPException(502, NOT_A_REGISTRY) from None
        try:
            made = kept.admit_pulled(org, fetched, by=by, hidden=hidden, served_ids=served())
        except PhiRejected as error:
            refused("phi")
            return JSONResponse({"detail": str(error), "phi": error.report}, status_code=422)
        except StoreError as error:
            refused("checks")
            raise HTTPException(error.status, str(error)) from None
        if made.state == HELD:
            security_log.event("store.version.held", actor=by, org=org, ids=[made.digest])
        else:
            security_log.event("store.pull.added", actor=by, org=org, ids=[made.digest])
        row = kept.version(org, made.digest)
        shown = _detail(row)
        if row["state"] == HELD:
            shown = {**shown, "message": HELD_MESSAGE}
        return JSONResponse({**shown, "source": HUB}, status_code=201)


def _shown(url: str) -> str:
    """The registry's address as an answer shows it: scheme, host, port and path (it holds no
    user or password: `settings.registry_url` refuses one)."""
    from mendari.hub.client import _shown_url

    return _shown_url(url)


# What a verification's refusal may say to a person: Mendari's own sentences about what it
# checked. A registry's error (its HTTP status and `detail`) is never one of them.
_REGISTRY_SAID = re.compile(r"the registry answered HTTP \d+")


def _own_words(error: HubError) -> str:
    said = str(error)
    if _REGISTRY_SAID.search(said):
        return "the registry refused the request"
    # One line of printable text: an id or a path it quotes is shown as text, and only so much.
    return "".join(ch if ch.isprintable() else " " for ch in said)[:300]
