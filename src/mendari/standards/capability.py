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
- no clinical resources: Mendari stores and serves none. Its FHIR is a decision operation, its
  decisions as read-only Tasks (ADR-0003) with their audit trail, the conformance resources
  that describe it, and the Da Vinci operation;
- no conditional read, no `_history` Bundles, no previous or last links: what is not done is
  declared (`conditionalRead: not-supported`) or not advertised.

The search parameters come from `search.SPECS`, the table the parser and the store mapping use,
so the statement lists exactly the parameters that are searched.
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
from mendari.standards.search import DEFAULT_COUNT, MAX_COUNT, SPECS, search_params

DECIDE_URL = "https://mendari.co/fhir/OperationDefinition/decide"
DTR_QUESTIONNAIRE_PACKAGE = (
    "http://hl7.org/fhir/us/davinci-dtr/OperationDefinition/questionnaire-package|2.2.0"
)
NOT_A_DATA_SERVER = (
    "Mendari is a decision engine, not a FHIR data server: it stores and serves no clinical "
    "resources. Its FHIR is the $decide operation; its decisions as read-only Tasks and their "
    "hash-chained audit trail as AuditEvents and Provenances (read and search, each caller "
    "seeing only its organization's); the conformance resources that describe it; and Da Vinci "
    "DTR's $questionnaire-package. Outside this FHIR base it also offers "
    "/api/packs/{namespace}/{name}/fhir/decide (FHIR in, Mendari JSON out), CDS Hooks at "
    "/cds-services, the whole audit trail as FHIR NDJSON at /api/audit/export/fhir, and a SMART "
    "on FHIR app launch at /smart/launch."
)
SEARCH_RULES = (
    f"Search: GET [type] or POST [type]/_search with a form body. _count is {DEFAULT_COUNT} by "
    f"default and at most {MAX_COUNT}; pages follow the next link (an opaque cursor), with no "
    "previous or last links. Bundle.total only for _total=accurate (none and estimate give "
    "none); _summary=count or _count=0 gives the total and no entries. A parameter R4 does not "
    "define for the type is ignored, with a warning OperationOutcome entry (search.mode "
    "outcome). A standard R4 parameter Mendari does not support (patient, subject, focus, "
    "owner, agent, _sort, _include, ...) is a 400 unless Prefer: handling=lenient: a deliberate "
    "deviation from R4's SHOULD ignore, since an ignored filter returns more than the client "
    "asked for. An unsupported modifier or chain is always a 400, and Prefer: handling=strict "
    "makes an unknown parameter a 400 too. Dates are UTC: a date alone is that UTC day, a time "
    "needs a zone (Z or +hh:mm), and the prefixes are eq, ne, gt, lt, ge, le, sa and eb (ap is "
    "a 400). Tokens match exactly, in the system the element's codes carry."
)
# What each served resource is here, its default sort, and its version policy (R4
# ResourceVersionPolicy) when it carries versions.
RESOURCES: dict[str, tuple[str, str | None]] = {
    "Task": (
        "One Task per Mendari decision (ADR-0003), derived on read from the audit trail: the "
        "decision itself, not the work item it routes. $decide returns that work item, with the "
        "same decision-id identifier, for the EHR to own; this Task is what Mendari knows. "
        "status: requested while the decision waits for a reviewer in Mendari, completed once "
        "its answer is final, failed when the input was refused; businessStatus is the outcome "
        "(AUTO, HUMAN_REVIEW, ABSTAIN, BLOCK). Its versions are the events that change it "
        "(made, review resolved, outcome recorded), read at _history/{n}, each with its "
        "Provenance in relevantHistory. No input text, subject or focus, so patient, subject "
        "and focus searches are refused. Search order: newest first (authoredOn, then id).",
        "versioned",
    ),
    "AuditEvent": (
        "Every event chained on a decision in the hash-chained audit trail (id {decision "
        "id}-{chain position}), immutable; entity refers to the decision's Task. Search order: "
        "chain order, oldest first, the feed to sync from.",
        "versioned",
    ),
    "Provenance": (
        "The events that change a decision's result (id {decision id}-p{chain position}): made "
        "by the engine, verified or changed by a reviewer, an outcome recorded downstream. Each "
        "targets the Task version it made. Immutable. Search order: chain order, oldest first.",
        "versioned",
    ),
    "CodeSystem": (
        "Mendari's CodeSystems (decision outcomes, audit events, packs, work queues, each served "
        "pack's question labels), generated from the code and the served packs; the same for "
        "every organization. Search order: by id.",
        None,
    ),
}


@dataclass(frozen=True)
class FhirCapability:
    """One thing a `/fhir/` route does, keyed by the route that does it."""

    method: str
    path: str  # the FastAPI route path, e.g. "/fhir/CodeSystem/{resource_id}"
    # An interaction (R4 TypeRestfulInteraction) or an operation. GET [type] and POST
    # [type]/_search are both `search-type`: one interaction, two routes.
    kind: Literal["read", "vread", "search-type", "operation"]
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
    FhirCapability("GET", "/fhir/Task/{resource_id}", "read", resource="Task"),
    FhirCapability(
        "GET", "/fhir/Task/{resource_id}/_history/{version_id}", "vread", resource="Task"
    ),
    FhirCapability("GET", "/fhir/AuditEvent/{resource_id}", "read", resource="AuditEvent"),
    FhirCapability("GET", "/fhir/Provenance/{resource_id}", "read", resource="Provenance"),
    *(
        FhirCapability(method, path.format(resource), "search-type", resource=resource)
        for resource in SPECS
        for method, path in (("GET", "/fhir/{}"), ("POST", "/fhir/{}/_search"))
    ),
)
_INTERACTIONS = ("read", "vread", "search-type")  # in the order a statement lists them


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
    searched = any(c.kind == "search-type" for c in offered)
    rest: dict[str, Any] = {
        "mode": "server",
        "documentation": f"{NOT_A_DATA_SERVER} {SEARCH_RULES}" if searched else NOT_A_DATA_SERVER,
        "security": {"cors": False, "description": _security(sign_in)},
    }
    by_type: dict[str, list[FhirCapability]] = {}
    for capability in offered:
        if capability.resource is not None:
            by_type.setdefault(capability.resource, []).append(capability)
    if by_type:
        rest["resource"] = [_resource(name, by_type[name]) for name in sorted(by_type)]
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


def _resource(name: str, offered: list[FhirCapability]) -> dict[str, Any]:
    """A `rest.resource` entry: what the type is here, its interactions (one `search-type` for
    the GET and POST routes), version policy, search parameters and operations."""
    kinds = {c.kind for c in offered}
    entry: dict[str, Any] = {"type": name}
    documentation, versioning = RESOURCES.get(name, ("", None))
    if documentation:
        entry["documentation"] = documentation
    interactions = [{"code": kind} for kind in _INTERACTIONS if kind in kinds]
    if interactions:
        entry["interaction"] = interactions
    if versioning is not None:
        entry["versioning"] = versioning
        entry["readHistory"] = "vread" in kinds
    if "read" in kinds:
        entry["conditionalRead"] = "not-supported"
    if "search-type" in kinds:
        entry["searchParam"] = search_params(SPECS[name])
    operations = [_operation(c) for c in offered if c.kind == "operation"]
    if operations:
        entry["operation"] = operations
    return entry


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
            "and $questionnaire-package need the decide permission (reviewer, service, admin). "
            "Reading and searching Tasks, AuditEvents and Provenances, and the CodeSystems and "
            "NamingSystems, need the read permission (any role), and every read, search page and "
            "total covers only the caller's organization (another's resource is a 404, as an "
            "unknown one is). Metadata and the OperationDefinition need none. Answers about "
            "decisions are Cache-Control: private, no-cache. This is not a SMART on FHIR "
            "resource server: it accepts no SMART access tokens and has no "
            ".well-known/smart-configuration. Toward EHRs, Mendari is a SMART app."
        )
    return (
        "Mendari's sign-in is off on this deployment, so it does not authenticate requests "
        "itself and has one organization: whoever can reach it reads every decision. The "
        "hosting platform may restrict who can reach it. This is not a SMART on "
        "FHIR resource server. Toward EHRs, Mendari is a SMART app."
    )


def _implementation(place: tuple[str, str] | None) -> str:
    if place is None:
        return "Mendari"
    environment, release = place
    return f"Mendari ({environment}, {release})"


def _narrative(packs: list[str], offered: list[FhirCapability]) -> dict[str, str]:
    operations = ", ".join(f"${c.name}" for c in offered if c.kind == "operation") or "none"

    def kinds(kind: str) -> str:
        return ", ".join(sorted({c.resource or "" for c in offered if c.kind == kind})) or "none"

    text = (
        f"<p>Mendari {html.escape(__version__)}: FHIR R4 (JSON). Operations: "
        f"{html.escape(operations)}. Read: {html.escape(kinds('read'))}. Version read: "
        f"{html.escape(kinds('vread'))}. Search: {html.escape(kinds('search-type'))}. Packs "
        f"with FHIR input: {html.escape(', '.join(packs) or 'none')}. Decisions are served as "
        "read-only Tasks with their audit trail; no clinical resources are stored or served."
        "</p>"
    )
    return {"status": "generated", "div": f'<div xmlns="http://www.w3.org/1999/xhtml">{text}</div>'}
