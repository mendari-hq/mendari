# SPDX-License-Identifier: Apache-2.0
"""What `/docs/reference` and `/openapi.json` say about this deployment.

The OpenAPI document is generated from the routes; this adds what generation cannot know: a
description of the product, the tags, synthetic examples from the packs this deployment serves
(so "Try it out" starts from a request that works), and, only when sign-in is on, how to
authenticate. Nothing is claimed that the deployment does not do. What Mendari is and its four
outcomes are said once, here, for this page and the developer landing (`server/landing.py`).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from mendari import notices
from mendari.core.pack import Pack
from mendari.evals.pack_tests import load_cases
from mendari.server import scopes

TAGS = [
    {"name": "Decisions", "description": "Run a Decision Pack on an input and get its decision."},
    {
        "name": "FHIR and CDS Hooks",
        "description": "FHIR R4 in and out: the $decide operation and the CapabilityStatement "
        "(/fhir/metadata), deciding from a Bundle, CDS Hooks services, decisions as Tasks and "
        "the audit trail as AuditEvent and Provenance (read and search), Mendari's CodeSystems, "
        "Da Vinci DTR and PAS. Mendari is not a FHIR data server: it stores and serves no "
        "clinical resources.",
    },
    {"name": "Packs", "description": "The Decision Packs this deployment serves."},
    {"name": "Review and feedback", "description": "The human review queue, outcomes, ratings."},
    {"name": "Audit", "description": "The hash-chained audit trail of every decision."},
    {"name": "Evaluation and monitoring", "description": "Evaluations, drift, deployments."},
    {"name": "Governance", "description": "Approvals for higher-risk packs to automate."},
    {
        "name": "Sign-in",
        "description": "Who is calling, API keys for machines, and (memberships mode) members "
        "and invitations.",
    },
    {
        "name": "Labelling",
        "description": "Hidden evaluation sets, labelled blind (memberships mode): a labeller's "
        "next case, their label and their own counts. A labeller sees a case's input and the "
        "questions, never another label, the case's attributes or a model's output.",
    },
    {
        "name": "Evaluation sets",
        "description": "Hidden evaluation sets, managed by the organization's administrators "
        "(memberships mode): its sets listed and read (state, labelling progress by user id, "
        "and the coverage and rater agreement the key holder stored), and their cases drawn, "
        "assigned and adjudicated. Ids and counts: never a case, an input or a label given.",
    },
    {
        "name": "Grading",
        "description": "A pack graded against one of the organization's hidden evaluation sets, "
        "frozen and committed to, by its grading key only (which only the operator makes): each "
        "run counted against the set's rotation rule and recorded in the audit chain, with "
        "local providers only, answered with aggregates only (`mendari.hidden_result/1`): "
        "never a case, its id or a missed case. A run that declares its arms (`arms`, with the "
        "declaration's SHA-256 as `declared`) is the hidden-set scoreboard: every declared arm "
        "decided on the same cases in that one run, answered `mendari.hidden_scoreboard/1`, each "
        "arm's figures with their intervals, its attestations and the ranking, never cost, "
        "latency, a subgroup or a case. Nothing is stored.",
    },
    {"name": "SMART on FHIR", "description": "Mendari launched as a SMART app from an EHR."},
    {"name": "Service", "description": "Health and settings of this deployment."},
]

# A synthetic patient message as FHIR: what the inbox pack's FHIR mapping reads.
FHIR_EXAMPLE: dict[str, Any] = {
    "resourceType": "Bundle",
    "type": "collection",
    "entry": [
        {
            "fullUrl": "urn:uuid:7f1c2a4e-0d1b-4c55-9a51-3c1e2d000001",
            "resource": {
                "resourceType": "Patient",
                "id": "example-patient",
                "meta": {
                    "tag": [{"system": "https://mendari.co/fhir/sandbox", "code": "synthetic"}]
                },
                "identifier": [{"system": "https://mendari.co/fhir/synthetic-case", "value": "1"}],
                "name": [{"family": "Synthetic", "given": ["Example"]}],
                "gender": "unknown",
                "birthDate": "1984-06-15",
            },
        },
        {
            "fullUrl": "urn:uuid:7f1c2a4e-0d1b-4c55-9a51-3c1e2d000002",
            "resource": {
                "resourceType": "Communication",
                "id": "example-message",
                "status": "in-progress",
                "subject": {"reference": "Patient/example-patient"},
                "sent": "2026-09-29T13:00:00Z",
                "payload": [{"contentString": "Can I get a refill of my blood pressure medicine?"}],
            },
        },
    ],
}
FHIR_EXAMPLE_PACK = "mendari/patient-inbox-router"
# A pack's example is in the reference only if its JSON is at most this (the shipped packs' are
# under 1 KB); a larger one is left out, as is one from tests that do not load.
MAX_EXAMPLE_BYTES = 64 * 1024

# What Mendari is, in one sentence; the reference's introduction and the /docs landing open with it.
PRODUCT = (
    "Mendari runs Decision Packs: versioned, tested healthcare workflow decisions such as which "
    "team should handle a patient's message, or whether a prior-authorization request is complete."
)
# The four outcomes every decision ends in: the code, the dashboard's word for it (ui.js
# OUTCOME_TEXT), and what it means for the caller.
OUTCOMES = (
    ("AUTO", "Automated", "safe to act on"),
    ("HUMAN_REVIEW", "Human review", "a person decides, with the pack's recommendation"),
    ("ABSTAIN", "Abstained", "not confident enough to say"),
    ("BLOCK", "Blocked", "the input or a safety rule stops it"),
)


def description(
    *,
    sign_in: bool,
    default_pack: str | None,
    token_url: str | None = None,
    rate_limits: Mapping[str, str] | None = None,
) -> str:
    """The page's introduction: what this is, a first request, how to authenticate (and, when
    SMART Backend Services is on, `token_url`: how machines get tokens), and, when they are on,
    the `rate_limits` (class: rate)."""
    pack = default_pack or "{namespace}/{name}"
    auth = (
        "**Sign-in is on.** People sign in at `/auth/login`; the dashboard and these pages use "
        "that session, which ends after 30 minutes without a request or 8 hours (unless the "
        "deployment sets otherwise). Reviews, ratings, approvals and outcomes are recorded for "
        "whoever is signed in: a person by their verified email, else `<issuer>|<subject>`; a "
        "key as `key <id>`. Machines send `Authorization: Bearer ohk_…`, an API key an admin "
        "creates with `POST /api/auth/keys`. Each key has one role, `viewer` or `service`, and "
        "expires (90 days unless asked, 365 at most). Optional scopes narrow it: SMART v2 resource "
        "scopes for the FHIR reads (`system/Task.rs`, `system/*.rs`, …) and "
        f"`{scopes.DECIDE}` or `{scopes.OUTCOME}` for Mendari's own actions; a scoped key "
        'reaches only what its scopes name (a `403` with `error="insufficient_scope"` and the '
        "scope needed otherwise). A key for a documented integration path can be made from a preset"
        " instead (`preset` in `POST /api/auth/keys`; `GET /api/auth/keys/presets` lists them, with"
        " what each reaches here). A key's history, from the audit chain, is"
        " `GET /api/auth/keys/{key_id}/history`. Each operation below names the permission and"
        " the scope it needs. An `Authorization` header decides alone: a credential in it that is"
        " not valid is a "
        "`401`, even with a session cookie. Credentials are never accepted in a URL or a form "
        "(`access_token` is a `400`). Refusals carry `WWW-Authenticate` (RFC 6750), except a "
        "signed-in person's who has not acknowledged the notice (`GET /api/notice`): a `403` with "
        "`Mendari-Notice-Required` on routes that take data in. `/api` "
        "answers are `Cache-Control: no-store`. Only a signed-in person manages keys, members and "
        "invitations, never a key. "
        "A clinician an EHR launches (from an EHR registered here, `smartLaunch`) reaches only "
        "the launch's own routes: `GET /api/smart/patient` and `/focus`, and, registered to "
        "decide, `POST /api/smart/decide`." + (_backend_services(token_url) if token_url else "")
        if sign_in
        else "**Sign-in is off here**: anyone who can reach this deployment can call every "
        "operation. Set `MENDARI_OIDC_ISSUER` and `MENDARI_OIDC_CLIENT_ID` to require it."
    )
    outcomes = [f"**`{code}`** ({meaning})" for code, _, meaning in OUTCOMES]
    return f"""
**New here?** [Start at `/docs`](/docs): what Mendari is, a quickstart in three calls, FHIR in
and out, and authentication, on one page.

{PRODUCT} Every decision ends as {", ".join(outcomes[:-1])} or {outcomes[-1]}, and is recorded
in a hash-chained audit trail.

{notices.NOTICE} All data here is synthetic.

### A first request

1. `GET /api/packs` lists the packs.
2. `GET /api/packs/{pack}/examples` gives synthetic inputs.
3. `POST /api/packs/{pack}/decide` with an example's `input` returns the decision; its `id`
   opens the audit record at `GET /api/audit/{{id}}`.

A request body is 4 MiB at most unless the deployment sets `MENDARI_MAX_BODY_BYTES`; a larger
one is a `413`. Every answer carries an `X-Request-Id`, and JSON errors on `/api` repeat it as
`request_id`: quote it when you report a problem. Off Vercel, an `X-Request-Id` you send (8 to
128 of `A-Za-z0-9._:-`) is used.{_limits(rate_limits)}

### FHIR

The FHIR base is `/fhir`; [`GET /fhir/metadata`](/fhir/metadata) says what it does.
`POST /fhir/$decide` takes `Parameters` (`pack`, `data` as a Bundle) and returns the decision as
a FHIR Bundle (a `GuidanceResponse` first; act on its `outcome`, not its `status`). To send a
bare Bundle, one resource or CDS Hooks prefetch and get Mendari's JSON back, use
`POST /api/packs/{{namespace}}/{{name}}/fhir/decide`. FHIR input first passes a structural R4
check (a `400` otherwise), then R4 and US Core 6.1.0 rules; an error on a FHIR route is an
`OperationOutcome`.

Decisions are also read and searched as FHIR, your organization's only: each is a read-only
`Task` (`GET /fhir/Task/{{id}}`; `/_history/{{n}}` for a version; `GET /fhir/Task?status=requested`
for the review queue), and its audit trail is `/fhir/AuditEvent` and `/fhir/Provenance`.
`POST /fhir/{{type}}/_search` takes the same parameters as a form. Mendari is a decision engine,
**not a FHIR data server**: it stores and serves no clinical resources.

### Authentication

{auth}

A browser request that another site starts (`Sec-Fetch-Site: cross-site` or `same-site`, or
without it an `Origin` that is `null` or not this site) reaches only public routes and pages,
with `GET` or `HEAD`; anything else is a `403` ("cross-site request refused"), and a form on
another site that posts here gets it whether or not the browser sends the session cookie (so
another site cannot sign anyone out). Integrations with a key or token, and scripts without a
session cookie, are not affected; a script that reads with a person's session cookie sends
`X-Mendari-Request: 1`, as Mendari's own pages do.

[Open the dashboard](/) to try packs without code. The OpenAPI document itself is
[`/openapi.json`](/openapi.json).
""".strip()


def _limits(rates: Mapping[str, str] | None) -> str:
    if not rates:
        return ""
    return (
        "\n\n**Rate limits.** Costly operations are limited per caller (an API key, a client, a "
        "person), counted across every instance of this deployment: decisions "
        f"{rates['decide']} (at every entry point; each draft order a CDS Hooks order hook "
        f"decides is one, at most 50 a request), the AI guard {rates['guard']}, evaluations "
        f"{rates['evaluate']} (a grading run is one), exports {rates['export']} and a labeller's "
        f"calls {rates['label']}; signing in {rates['signin']} and the token endpoint "
        f"{rates['token']} per address. Reads are not limited here, but for a labeller's. Over a "
        "limit the answer is a `429` with `Retry-After` (seconds) and `Cache-Control: "
        "no-store`: JSON on `/api`, an OperationOutcome (`throttled`) on FHIR routes, OAuth's "
        "`temporarily_unavailable` at the token endpoint. A daily budget or an organization's "
        "daily quota never refuses a decision: over it, decisions fall back to human review."
    )


def _backend_services(token_url: str) -> str:
    return (
        "\n\n**SMART Backend Services is on.** A client a signed-in admin registers "
        "(`POST /api/auth/clients`: a role, `viewer` or `service`, the scopes it may be granted, "
        "and its public JWK Set) gets a five-minute access token at "
        f"`POST {token_url}`: a form with `grant_type=client_credentials`, "
        "`client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer`, "
        "`client_assertion` (a JWT signed RS384 or ES384 with the client's private key: `iss` "
        f"and `sub` the `client_id`, `aud` exactly `{token_url}`, `exp` at most five minutes "
        "ahead, a `jti` used once) and `scope`. The token (`Authorization: Bearer mat_…`) gets "
        "what was asked within what the client registered, reaches only the routes its scopes "
        "name, and acts in the client's organization; revoking the client stops its tokens at "
        "once. Replacing its public keys (`POST /api/auth/clients/{client_id}/keys`: the same "
        "client id, role and scopes) stops them too, and a token issued against the old keys "
        "while it ran is refused; there is no overlap, so a switch without a gap is two "
        "replacements (old and new keys, then the new alone). "
        "`GET /fhir/.well-known/smart-configuration` describes this. There is no "
        "authorization endpoint and no refresh token."
    )


def document(
    schema: dict[str, Any],
    *,
    packs: Mapping[str, Pack],
    default_pack: str | None,
    permission_for: Callable[[str, str], str] | None,
    token_url: str | None = None,
    limit_for: Callable[[str, str], str | None] | None = None,
    session_cookie: str = "mendari_session",
    launch_cookie: str = "mendari_smart",
) -> dict[str, Any]:
    """Add examples and (when `permission_for` is given, i.e. sign-in is on) security to a
    generated OpenAPI document; with `token_url` (SMART Backend Services on), the token flow
    too, on the operations a token's scope can reach; with `limit_for` (rate limits on, the
    class of an operation or None), the `429` a limited operation can answer. `session_cookie`
    is the session cookie's name at the public URL (server/sessions.py), `launch_cookie` an EHR
    launch's."""
    ordered = sorted(packs, key=lambda pack_id: (pack_id != default_pack, pack_id))
    inputs = {pack_id: _example_input(packs[pack_id]) for pack_id in ordered}
    for path, operations in schema.get("paths", {}).items():
        fitting = [pack_id for pack_id in ordered if _fits(packs[pack_id], path)]
        for method, operation in operations.items():
            for parameter in operation.get("parameters", []):
                if parameter.get("in") == "path" and parameter["name"] in ("namespace", "name"):
                    part = 0 if parameter["name"] == "namespace" else 1
                    values = dict.fromkeys(pack_id.split("/")[part] for pack_id in fitting)
                    parameter["examples"] = {value: {"value": value} for value in values}
            body = operation.get("requestBody", {}).get("content", {}).get("application/json")
            examples = _body_examples(path, inputs, packs)
            if body is not None and examples:
                body["examples"] = examples
            if permission_for is not None:
                permission = permission_for(method.upper(), path)
                _secure(operation, method.upper(), path, permission, tokens=token_url is not None)
            if limit_for is not None and limit_for(method.upper(), path) is not None:
                operation.setdefault("responses", {})["429"] = TOO_MANY
    if permission_for is not None:
        schemes: dict[str, Any] = {
            "apiKey": {
                "type": "http",
                "scheme": "bearer",
                "description": "An API key (`ohk_…`) from `POST /api/auth/keys`: a role "
                "(viewer or service), optional scopes, an expiry.",
            },
            "session": {
                "type": "apiKey",
                "in": "cookie",
                "name": session_cookie,
                "description": "The session cookie a person gets by signing in at `/auth/login` "
                "(`mendari_session` without the prefix on localhost and dotless hosts). It ends "
                "after 30 minutes without a request or 8 hours, whichever is first (unless the "
                "deployment sets otherwise), and with the browser. On this page your browser "
                "sends it by itself: there is nothing to enter.",
            },
            "smartLaunch": {
                "type": "apiKey",
                "in": "cookie",
                "name": launch_cookie,
                "description": "The cookie an EHR launch sets (`/smart/launch`, then "
                "`/smart/callback`): a clinician the EHR's sign-in names, from an EHR this "
                "deployment registers (`MENDARI_SMART_EHRS`) to view, or to view and decide, in "
                "its registered organization. It is a credential on `/api/smart/` only, and never "
                "with an `Authorization` header (which decides alone). It ends with the EHR's "
                "token, after 30 minutes without a request or 8 hours (unless the deployment "
                "sets otherwise), and with the browser.",
            },
        }
        if token_url is not None:
            schemes["accessToken"] = {
                "type": "http",
                "scheme": "bearer",
                "description": "A SMART Backend Services access token (`mat_…`) that your "
                f"client got at `POST {token_url}` (see `smartBackendServices`).",
            }
            schemes["smartBackendServices"] = {
                "type": "oauth2",
                "description": "SMART Backend Services: a registered client's five-minute access "
                "token (`mat_…`), from a `client_credentials` grant authenticated with a "
                "`private_key_jwt` assertion (RS384 or ES384), never a client secret. This page "
                "cannot run that flow (it would ask for a client secret, which Mendari refuses): "
                "get a token with your client, then enter it under `accessToken`.",
                "flows": {
                    "clientCredentials": {
                        "tokenUrl": token_url,
                        "scopes": {name: _SCOPE_TEXT[name] for name in scopes.supported()},
                    }
                },
            }
        schema.setdefault("components", {})["securitySchemes"] = schemes
    return schema


REGISTRY_TAGS = [
    {
        "name": "Registry",
        "description": "The pack registry served under this deployment (`MENDARI_REGISTRY=on`): "
        "search, a pack's versions, its signed archive, recalls and site reports. Reads need no "
        "credential. Writes need a publisher token (`Authorization: Bearer ohr_…`), which "
        "publishes to its own namespace only; a session cookie or an API key is never a "
        "credential here. A request body is this deployment's limit at most (a `413` names it).",
    },
    {
        "name": "Registry accounts",
        "description": "An organization's namespaces and publisher tokens in the pack registry, "
        "managed by its administrators (people only): claim a namespace, register a publisher "
        "key, and mint, list and revoke publisher tokens.",
    },
]
PUBLISHER_TOKEN = {
    "type": "http",
    "scheme": "bearer",
    "description": "A publisher token (`ohr_…`) an organization's administrator mints with "
    "`POST /api/registry/tokens`: one namespace, an expiry, revocable. A credential on the "
    "registry's routes only.",
}
_SCHEMA_REF = '"#/components/schemas/'


def mounted(schema: dict[str, Any], registry: Mapping[str, Any], *, prefix: str) -> None:
    """Add the mounted registry's operations to this deployment's document, under `prefix`
    (LP-19d). Its schemas are renamed (`Registry…`: the two apps name some alike), its reads
    are marked as needing no credential and its writes a publisher token. Called after
    `document`, which knows only the engine's own rules and examples."""
    renamed = json.loads(json.dumps(registry).replace(_SCHEMA_REF, f"{_SCHEMA_REF}Registry"))
    for path, operations in renamed.get("paths", {}).items():
        for method, operation in operations.items():
            operation["tags"] = [REGISTRY_TAGS[0]["name"]]
            operation["operationId"] = f"registry_{operation.get('operationId', method)}"
            reads = method.upper() in ("GET", "HEAD")
            operation["security"] = [] if reads else [{"publisherToken": []}]
        schema.setdefault("paths", {})[f"{prefix}{path}"] = operations
    components = schema.setdefault("components", {})
    for name, model in renamed.get("components", {}).get("schemas", {}).items():
        components.setdefault("schemas", {})[f"Registry{name}"] = model
    components.setdefault("securitySchemes", {})["publisherToken"] = PUBLISHER_TOKEN
    schema["tags"] = [*schema.get("tags", []), *REGISTRY_TAGS]


TOO_MANY = {
    "description": "Too many requests from this caller: retry after `Retry-After` seconds.",
    "headers": {"Retry-After": {"description": "Seconds to wait", "schema": {"type": "integer"}}},
}

_SCOPE_TEXT = {
    **{
        f"system/{kind}.{letters}": f"read{' and search' if 's' in letters else ''} {kind}"
        for kind, letters in scopes.SERVED.items()
    },
    "system/*.rs": "read and search every type served",
    scopes.DECIDE: "decide, at every decision entry point",
    scopes.OUTCOME: "record a decision's downstream outcome",
}


def _fits(pack: Pack, path: str) -> bool:
    """Whether "Try it out" should offer this pack on this path: the guard route needs a pack
    with a guard, the FHIR route one with a FHIR mapping."""
    if path.endswith("/guard"):
        return pack.manifest.guard is not None
    if path.endswith("/fhir/decide"):
        return pack.manifest.inputs.fhir is not None
    return True


def _example_input(pack: Pack) -> tuple[str, dict[str, Any]] | None:
    """The pack's first behaviour test that automates (the friendliest first try), or its
    first one; None if it has none, its tests do not load, or the example is not JSON or is
    over `MAX_EXAMPLE_BYTES` as JSON. Whatever one pack's tests hold is that pack's problem,
    never the API reference's (which holds each example more than once, and in which a control
    character is six bytes): anything raised choosing its example leaves the pack out."""
    try:
        return _first_example(pack)
    except Exception:  # its tests do not load (named by `mendari validate` and …/examples)
        return None


def _first_example(pack: Pack) -> tuple[str, dict[str, Any]] | None:
    cases = load_cases(pack)  # each a mapping with an `input` mapping (or this raises)
    automated = [c for c in cases if c.get("expect", {}).get("outcome") == "AUTO"]
    if not (automated or cases):
        return None
    chosen = (automated or cases)[0]
    example = str(chosen.get("name", "example")), chosen["input"]
    try:
        encoded = json.dumps(example, ensure_ascii=False, allow_nan=False).encode()
    except (TypeError, ValueError, RecursionError):  # a YAML date, NaN, too deep
        return None
    return example if len(encoded) <= MAX_EXAMPLE_BYTES else None


def _body_examples(
    path: str, inputs: Mapping[str, tuple[str, dict[str, Any]] | None], packs: Mapping[str, Pack]
) -> dict[str, Any]:
    if path in ("/api/packs/{namespace}/{name}/fhir/decide", "/fhir/$decide"):
        if FHIR_EXAMPLE_PACK not in packs:
            return {}
        value: dict[str, Any] = FHIR_EXAMPLE
        if path == "/fhir/$decide":
            value = {
                "resourceType": "Parameters",
                "parameter": [
                    {"name": "pack", "valueString": FHIR_EXAMPLE_PACK},
                    {"name": "data", "resource": FHIR_EXAMPLE},
                ],
            }
        return {
            "message": {
                "summary": f"{FHIR_EXAMPLE_PACK}: a patient's message (synthetic)",
                "value": value,
            }
        }
    if path not in ("/api/packs/{namespace}/{name}/decide", "/api/packs/{namespace}/{name}/guard"):
        return {}
    guards = path.endswith("/guard")
    return {
        pack_id.split("/")[1]: {"summary": f"{pack_id}: {name}", "value": value}
        for pack_id, example in inputs.items()
        if example is not None and (packs[pack_id].manifest.guard is not None or not guards)
        for name, value in [example]
    }


def _secure(
    operation: dict[str, Any], method: str, path: str, permission: str, *, tokens: bool = False
) -> None:
    if permission == "public":
        operation["security"] = []
        return
    if permission in ("launch", "launch_decide"):  # EHR launches only (server/auth.py EHR_ONLY)
        operation["security"] = [{"smartLaunch": []}]
        needed = "an EHR launch from an EHR registered here" + (
            " to decide" if permission == "launch_decide" else ""
        )
        needed += " (never a person's session, a key or a token)"
        operation["description"] = f"{operation.get('description', '')}\n\n**Needs** {needed}."
        return
    if permission == "person":  # memberships mode: a person, with an organization or none
        operation["security"] = [{"session": []}]
        needed = (
            "a person who signed in, with an organization or none (never a key, a token or an "
            "EHR launch)"
        )
        operation["description"] = f"{operation.get('description', '')}\n\n**Needs** {needed}."
        return
    if permission == "label":  # people only, a labeller's membership (server/auth.py)
        operation["security"] = [{"session": []}]
        needed = (
            "a person who signed in holding the labeller role (memberships mode; never a key, a "
            "token or an administrator)"
        )
        operation["description"] = f"{operation.get('description', '')}\n\n**Needs** {needed}."
        return
    if permission == "registry_review":  # people only, the review organization's reviewers
        operation["security"] = [{"session": []}]
        needed = (
            "a person who signed in holding the registry_reviewer role in the pack registry's "
            "review organization (`registry_review`, memberships mode; never a key, a token "
            "or an administrator of another organization)"
        )
        operation["description"] = f"{operation.get('description', '')}\n\n**Needs** {needed}."
        return
    if permission == "curate":  # people only, an administrator (server/auth.py)
        operation["security"] = [{"session": []}]
        needed = (
            "a person who signed in as an administrator of the organization they act for "
            "(`curate`, memberships mode; never a key, a token or a labeller)"
        )
        operation["description"] = f"{operation.get('description', '')}\n\n**Needs** {needed}."
        return
    if permission == "grade":  # an organization's grading key only (server/auth.py GRADING_ONLY)
        operation["security"] = [{"apiKey": []}]
        needed = (
            "the organization's grading key (`grade`), which only the operator makes "
            "(`mendari access grader-key`); never a person, an administrator, another key or a "
            "token"
        )
        operation["description"] = f"{operation.get('description', '')}\n\n**Needs** {needed}."
        return
    if permission == "keys":  # people only (server/auth.py PEOPLE_ONLY)
        operation["security"] = [{"session": []}]
        needed = "a signed-in person with the `keys` permission (an administrator; never a key)"
        operation["description"] = f"{operation.get('description', '')}\n\n**Needs** {needed}."
        return
    scope = scopes.needed(method, path)
    operation["security"] = [{"apiKey": []}, {"session": []}]
    if tokens and scope:  # an access token reaches only what its scope names
        operation["security"][1:1] = [{"smartBackendServices": [scope]}, {"accessToken": []}]
    needed = "any signed-in role" if permission == "page" else f"the `{permission}` permission"
    credential = "a scoped key or an access token" if tokens else "a scoped key"
    needed += (
        f" and, for {credential}, the scope `{scope}`"
        if scope
        else f"; {credential} may not call this"
    )
    operation["description"] = f"{operation.get('description', '')}\n\n**Needs** {needed}.".strip()
