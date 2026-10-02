"""The CapabilityStatement at `GET /fhir/metadata`: what this deployment's FHIR routes do.

It is built from the routes that are mounted, through `CAPABILITIES`: an entry is advertised
only when its route exists (and, for an operation that needs a kind of pack, when such a pack is
served), and `tests/test_capability.py` checks the other way, that every `/fhir/` route has an
entry. So the statement can neither claim an interaction Mendari lacks nor forget one it adds.

What it does not claim, on purpose:
- no `implementationGuide` or `instantiates`: R4 reads `implementationGuide` as support "in
  their entirety", and Mendari implements one of the four operations a Da Vinci DTR payer
  service must offer, and serves no US Core resources;
- no `security.service` code: Mendari's API keys and OIDC browser sessions are neither OAuth
  resource-server security nor SMART on FHIR, so `security.description` says what they are;
- no clinical resources: Mendari stores and serves none. Its FHIR is a decision operation, the
  conformance resources that describe it, and the Da Vinci operation.
"""

from __future__ import annotations

import hashlib
import html
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from mendari import __version__
from mendari.core.pack import Pack

DECIDE_URL = "https://mendari.co/fhir/OperationDefinition/decide"
DTR_QUESTIONNAIRE_PACKAGE = (
    "http://hl7.org/fhir/us/davinci-dtr/OperationDefinition/questionnaire-package|2.2.0"
)
NOT_A_DATA_SERVER = (
    "Mendari is a decision engine, not a FHIR data server: it stores and serves no clinical "
    "resources. Its FHIR is the $decide operation, the conformance resources that describe it, "
    "and Da Vinci DTR's $questionnaire-package. Outside this FHIR base it also offers "
    "/api/packs/{namespace}/{name}/fhir/decide (FHIR in, Mendari JSON out), CDS Hooks at "
    "/cds-services, the audit trail as FHIR at /api/audit/export/fhir, and a SMART on FHIR app "
    "launch at /smart/launch."
)


@dataclass(frozen=True)
class FhirCapability:
    """One thing a `/fhir/` route does, keyed by the route that does it."""

    method: str
    path: str  # the FastAPI route path, e.g. "/fhir/CodeSystem/{resource_id}"
    kind: Literal["read", "operation"]
    resource: str | None = None  # None: a system-level operation
    name: str | None = None  # the operation's code
    definition: str | None = None  # the operation's canonical (with |version)
    documentation: str = ""
    # Advertised only when a served pack needs it (e.g. DTR, for packs with a questionnaire).
    serves: Callable[[Iterable[Pack]], bool] | None = None


def _has_questionnaire(packs: Iterable[Pack]) -> bool:
    return any(p.manifest.cds_hooks and p.manifest.cds_hooks.questionnaire for p in packs)


def _takes_fhir(packs: Iterable[Pack]) -> bool:
    return any(p.manifest.inputs.fhir for p in packs)


CAPABILITIES: tuple[FhirCapability, ...] = (
    FhirCapability(
        "POST",
        "/fhir/$decide",
        "operation",
        name="decide",
        definition=f"{DECIDE_URL}|{__version__}",
        documentation="Decide one case with a Decision Pack: Parameters (pack, version, data, "
        "focus) in, a collection Bundle (GuidanceResponse, RequestGroup, Task, Parameters, "
        "OperationOutcome, Provenance) out. Every call is recorded in the audit trail.",
        serves=_takes_fhir,
    ),
    FhirCapability(
        "POST",
        "/fhir/Questionnaire/$questionnaire-package",
        "operation",
        resource="Questionnaire",
        name="questionnaire-package",
        definition=DTR_QUESTIONNAIRE_PACKAGE,
        documentation="Da Vinci DTR: the documentation a payer asks for an order a pack decided "
        "(found by the CRD assertion id), pre-answered from the decision. Reads coverage, order, "
        "context and questionnaire; ignores changedsince. DTR's other payer operations "
        "($next-question, $log-questionnaire-errors, ValueSet/$expand) are not implemented.",
        serves=_has_questionnaire,
    ),
    FhirCapability("GET", "/fhir/CodeSystem/{resource_id}", "read", resource="CodeSystem"),
    FhirCapability("GET", "/fhir/NamingSystem/{resource_id}", "read", resource="NamingSystem"),
    FhirCapability(
        "GET", "/fhir/OperationDefinition/{resource_id}", "read", resource="OperationDefinition"
    ),
)


def capability_statement(
    routes: Iterable[tuple[str, str]],
    packs: Mapping[str, Pack],
    *,
    sign_in: bool,
    since: Callable[[str], str],
    public_url: str | None = None,
    place: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """The statement for the mounted `routes` ((method, path) pairs) and the served `packs`.
    `version` is a digest of everything else in it, so any change makes a new one, and `date` is
    `since(version)`: when that version went live here (`AuditStore.capability_since`)."""
    mounted = set(routes)
    offered = [
        c
        for c in CAPABILITIES
        if (c.method, c.path) in mounted and (c.serves is None or c.serves(packs.values()))
    ]
    pack_refs = sorted(f"{p.id}@{p.version}" for p in packs.values() if p.manifest.inputs.fhir)
    rest: dict[str, Any] = {
        "mode": "server",
        "documentation": NOT_A_DATA_SERVER,
        "security": {"cors": False, "description": _security(sign_in)},
    }
    resources: dict[str, dict[str, Any]] = {}
    for capability in offered:
        if capability.resource is None:
            continue
        entry = resources.setdefault(capability.resource, {"type": capability.resource})
        if capability.kind == "read":
            entry.setdefault("interaction", []).append({"code": "read"})
        else:
            entry.setdefault("operation", []).append(_operation(capability))
    if resources:
        rest["resource"] = [resources[name] for name in sorted(resources)]
    system = [_operation(c, packs=pack_refs) for c in offered if c.resource is None]
    if system:
        rest["operation"] = system
    statement: dict[str, Any] = {
        "resourceType": "CapabilityStatement",
        "id": "mendari",
        "text": _narrative(pack_refs, offered),
        "version": "",
        "name": "MendariCapabilityStatement",
        "title": "Mendari FHIR R4 capabilities",
        "status": "active",
        "experimental": False,
        "date": "",
        "publisher": "Mendari",
        "description": NOT_A_DATA_SERVER,
        "kind": "instance",
        "software": {"name": "Mendari", "version": __version__},
        "implementation": {"description": _implementation(place)},
        "fhirVersion": "4.0.1",
        "format": ["json"],
        "rest": [rest],
    }
    if public_url:
        statement["implementation"]["url"] = f"{public_url.rstrip('/')}/fhir"
    content = json.dumps({**statement, "date": ""}, sort_keys=True, separators=(",", ":"))
    statement["version"] = f"{__version__}+{hashlib.sha256(content.encode()).hexdigest()[:8]}"
    statement["date"] = since(statement["version"])
    return statement


def _operation(capability: FhirCapability, packs: list[str] | None = None) -> dict[str, Any]:
    documentation = capability.documentation
    if packs is not None:
        documentation += f" Packs with FHIR input served here: {', '.join(packs) or 'none'}."
    operation: dict[str, Any] = {"name": capability.name, "definition": capability.definition}
    if documentation:
        operation["documentation"] = documentation
    return operation


def _security(sign_in: bool) -> str:
    if sign_in:
        return (
            "Mendari's sign-in is on. Send an API key (Authorization: Bearer ohk_...) or use a "
            "browser session from /auth/login (OpenID Connect). Roles decide access: $decide "
            "and $questionnaire-package need the decide permission (reviewer, service, admin), the "
            "CodeSystems and NamingSystems the read permission (any role); metadata and the "
            "OperationDefinition need none. This is not a SMART on FHIR "
            "resource server: it accepts no SMART access tokens and has no "
            ".well-known/smart-configuration. Toward EHRs, Mendari is a SMART app."
        )
    return (
        "Mendari's sign-in is off on this deployment, so it does not authenticate requests "
        "itself; the hosting platform may restrict who can reach it. This is not a SMART on "
        "FHIR resource server. Toward EHRs, Mendari is a SMART app."
    )


def _implementation(place: tuple[str, str] | None) -> str:
    if place is None:
        return "Mendari"
    environment, release = place
    return f"Mendari ({environment}, {release})"


def _narrative(packs: list[str], offered: list[FhirCapability]) -> dict[str, str]:
    operations = ", ".join(f"${c.name}" for c in offered if c.kind == "operation") or "none"
    reads = ", ".join(sorted({c.resource or "" for c in offered if c.kind == "read"})) or "none"
    text = (
        f"<p>Mendari {html.escape(__version__)}: FHIR R4 (JSON). Operations: "
        f"{html.escape(operations)}. Read: {html.escape(reads)}. Packs with FHIR input: "
        f"{html.escape(', '.join(packs) or 'none')}. No clinical resources are stored or "
        "served.</p>"
    )
    return {"status": "generated", "div": f'<div xmlns="http://www.w3.org/1999/xhtml">{text}</div>'}
