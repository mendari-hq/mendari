"""What `/docs`, `/redoc` and `/openapi.json` say about this deployment.

The OpenAPI document is generated from the routes; this adds what generation cannot know: a
description of the product, the tags, synthetic examples from the packs this deployment serves
(so "Try it out" starts from a request that works), and, only when sign-in is on, how to
authenticate. Nothing is claimed that the deployment does not do.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

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
    {"name": "Sign-in", "description": "Who is calling, and API keys for machines."},
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


def description(*, sign_in: bool, default_pack: str | None) -> str:
    """The page's introduction: what this is, a first request, how to authenticate."""
    pack = default_pack or "{namespace}/{name}"
    auth = (
        "**Sign-in is on.** People sign in at `/auth/login`; the dashboard and these pages use "
        "that session. Machines send `Authorization: Bearer ohk_…`, an API key an admin creates "
        "with `POST /api/auth/keys`. Each key has one role, `viewer` or `service`, and expires "
        "(90 days unless asked, 365 at most). Optional scopes narrow it: SMART v2 resource "
        "scopes for the FHIR reads (`system/Task.rs`, `system/*.rs`, …) and "
        f"`{scopes.DECIDE}` or `{scopes.OUTCOME}` for Mendari's own actions; a scoped key "
        'reaches only what its scopes name (a `403` with `error="insufficient_scope"` and the '
        "scope needed otherwise). Each operation below names the permission and the scope it "
        "needs. An `Authorization` header decides alone: a credential in it that is not valid "
        "is a `401`, even with a session cookie. Credentials are never accepted in a URL or a form "
        "(`access_token` is a `400`). Refusals carry `WWW-Authenticate` (RFC 6750), and `/api` "
        "answers are `Cache-Control: no-store`. Only a signed-in person manages keys, never a key."
        if sign_in
        else "**Sign-in is off here**: anyone who can reach this deployment can call every "
        "operation. Set `MENDARI_OIDC_ISSUER` and `MENDARI_OIDC_CLIENT_ID` to require it."
    )
    return f"""
Mendari runs **Decision Packs**: versioned, tested healthcare workflow decisions such as which
team should handle a patient's message, or whether a prior-authorization request is complete.
Every decision ends as **`AUTO`** (safe to act on), **`HUMAN_REVIEW`** (a person decides, with
the pack's recommendation), **`ABSTAIN`** (not confident enough to say) or **`BLOCK`** (the input
or a safety rule stops it), and is recorded in a hash-chained audit trail.

All data here is synthetic. This is a research preview, not a medical device.

### A first request

1. `GET /api/packs` lists the packs.
2. `GET /api/packs/{pack}/examples` gives synthetic inputs.
3. `POST /api/packs/{pack}/decide` with an example's `input` returns the decision; its `id`
   opens the audit record at `GET /api/audit/{{id}}`.

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

[Open the dashboard](/) to try packs without code, or [read this reference in ReDoc](/redoc).
""".strip()


def document(
    schema: dict[str, Any],
    *,
    packs: Mapping[str, Pack],
    default_pack: str | None,
    permission_for: Callable[[str, str], str] | None,
) -> dict[str, Any]:
    """Add examples and (when `permission_for` is given, i.e. sign-in is on) security to a
    generated OpenAPI document."""
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
                _secure(operation, method.upper(), path, permission_for(method.upper(), path))
    if permission_for is not None:
        schema.setdefault("components", {})["securitySchemes"] = {
            "apiKey": {
                "type": "http",
                "scheme": "bearer",
                "description": "An API key (`ohk_…`) from `POST /api/auth/keys`: a role "
                "(viewer or service), optional scopes, an expiry.",
            },
            "session": {
                "type": "apiKey",
                "in": "cookie",
                "name": "mendari_session",
                "description": "The session cookie a person gets by signing in at `/auth/login`.",
            },
        }
    return schema


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
    first one."""
    cases = [
        c for c in load_cases(pack) if isinstance(c, dict) and isinstance(c.get("input"), dict)
    ]
    automated = [c for c in cases if (c.get("expect") or {}).get("outcome") == "AUTO"]
    if not (automated or cases):
        return None
    chosen = (automated or cases)[0]
    return str(chosen.get("name", "example")), chosen["input"]


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


def _secure(operation: dict[str, Any], method: str, path: str, permission: str) -> None:
    if permission == "public":
        operation["security"] = []
        return
    if permission == "keys":  # people only (server/auth.py PEOPLE_ONLY)
        operation["security"] = [{"session": []}]
        needed = "a signed-in person with the `keys` permission (an administrator; never a key)"
        operation["description"] = f"{operation.get('description', '')}\n\n**Needs** {needed}."
        return
    operation["security"] = [{"apiKey": []}, {"session": []}]
    needed = "any signed-in role" if permission == "page" else f"the `{permission}` permission"
    scope = scopes.needed(method, path)
    needed += (
        f" and, for a scoped key, the scope `{scope}`"
        if scope
        else "; a scoped key may not call this"
    )
    operation["description"] = f"{operation.get('description', '')}\n\n**Needs** {needed}.".strip()
