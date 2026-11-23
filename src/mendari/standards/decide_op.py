# SPDX-License-Identifier: Apache-2.0
"""`POST /fhir/$decide`: a decision as FHIR, through a FHIR operation Mendari defines.

Why a custom, system-level operation and not `PlanDefinition/$apply`: R4's `$apply` resolves
its `subject` against the server's own data, and Mendari holds none; CPG's `$apply` adds `data`
but returns a different shape; and an agent's logic (models, rules, LLM providers, policy)
cannot be expressed as PlanDefinition actions, so serving packs as PlanDefinitions would promise
what no other engine could run. R4 has a resource for a decision-support result, though:

- `GuidanceResponse` (first in the Bundle): the decision. `status` is `success` when the pack
  evaluated the input, whatever the outcome, and `failure` when the input was refused before any
  provider ran. **Whether to act is `outputParameters.outcome`, never `status`.**
- `RequestGroup` → `Task`: the proposed work item (R4 allows only a CarePlan or RequestGroup as
  `GuidanceResponse.result`). Under BLOCK and ABSTAIN it is the pack's fallback, never the
  blocked action.
- `Parameters`: outcome, recommendation, priority, confidence and every question's answer, with
  labels coded in the pack's question CodeSystems (`terminology.py`).
- `OperationOutcome`, when there is anything to say: R4/US Core findings, provider failures and
  why an input was refused, by path and rule, never by value.
- `Provenance`: the audit trail's own for this decision, with the GuidanceResponse and Task as
  targets.

References between the entries are their `urn:uuid` full URLs. References to the input (the
Task's `for` and `focus`, the GuidanceResponse's and RequestGroup's `subject`) are the absolute
URL `data` gives the resource (its entry's RESTful `fullUrl`, e.g.
`http://example.org/fhir/Patient/1`). When it gives none, the reference is `Type/id`: relative
to the client's own FHIR server, since it has no base in this Bundle.

The input resources are not echoed. An answer's `evidence` can name what matched in them (a
keyword, a code, a value): it explains the decision to the client that sent the data.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mendari import __version__
from mendari.core.pack import Pack
from mendari.errors import FhirError, FhirRequestError, FhirStructureError
from mendari.standards import davinci, fhir_structure
from mendari.standards.capability import DECIDE_URL
from mendari.standards.fhir import (
    DECISION_ID_SYSTEM,
    OUTCOME_SYSTEM,
    PACK_ID_SYSTEM,
    PACK_TAG_SYSTEM,
    display,
    iter_entries,
    iter_resources,
    narrative,
)

if TYPE_CHECKING:
    from mendari.core.decision import DecisionResult

_NAMESPACE = uuid.UUID("6f1c1f0e-3d7a-5b7e-9c55-6d656e646172")  # fixed: fullUrls are stable
_PROVIDER_ISSUE = {
    "budget_exhausted": "throttled",
    "quota_exhausted": "throttled",
    "timeout": "timeout",
}
_PARAMETERS = {"pack": "valueString", "version": "valueString", "focus": "valueString"}
_ABSOLUTE = ("http://", "https://")


@dataclass(frozen=True)
class DecideRequest:
    pack: str
    version: str | None
    data: dict[str, Any]
    focus: str | None


def parse(body: Any) -> DecideRequest:
    """The operation's `Parameters`, checked: structurally first (the whole body, `data` too),
    then each parameter. Problems are a `400` OperationOutcome naming the parameter."""
    if not isinstance(body, Mapping) or body.get("resourceType") != "Parameters":
        _refuse(
            "invalid",
            "resourceType",
            "send a Parameters resource (pack, version, data, focus); to send a bare Bundle, "
            "use /api/agents/{namespace}/{name}/fhir/decide",
        )
    malformed = fhir_structure.blocking(fhir_structure.check(body))
    if malformed:
        raise FhirStructureError(
            [
                {"code": i.code, "expression": i.expression, "diagnostics": i.diagnostics}
                for i in malformed
            ]
        )
    found: dict[str, Any] = {}
    for index, parameter in enumerate(body.get("parameter") or []):
        name = parameter.get("name")
        where = f"Parameters.parameter[{index}]"
        if name not in (*_PARAMETERS, "data"):
            _refuse("not-supported", f"{where}.name", "not a parameter of $decide")
        if name in found:
            _refuse("invalid", f"{where}.name", f"{name} is given more than once")
        if name == "data":
            resource = parameter.get("resource")
            if not isinstance(resource, Mapping) or resource.get("resourceType") != "Bundle":
                _refuse("invalid", f"{where}.resource", "data must be a Bundle")
            found[name] = resource
            continue
        value = parameter.get(_PARAMETERS[name])
        if not isinstance(value, str):
            _refuse("invalid", where, f"{name} must be a {_PARAMETERS[name]}")
        found[name] = value
    for required in ("pack", "data"):
        if required not in found:
            _refuse("required", "Parameters.parameter", f"the {required} parameter is required")
    return DecideRequest(
        pack=found["pack"],
        version=found.get("version"),
        data=dict(found["data"]),
        focus=found.get("focus"),
    )


def focus_in(data: Mapping[str, Any], reference: str | None) -> dict[str, Any] | None:
    """The resource `focus` (`Type/id`) names in `data`, or None without a `focus`."""
    if reference is None:
        return None
    kind, _, resource_id = reference.partition("/")
    for resource in iter_resources(data):
        if resource.get("resourceType") == kind and resource.get("id") == resource_id:
            return dict(resource)
    raise FhirError("focus names no resource in data (it must be ResourceType/id of an entry)")


def response(
    result: DecisionResult,
    task: Mapping[str, Any],
    pack: Pack,
    provenance: Mapping[str, Any],
    data: Mapping[str, Any],
) -> dict[str, Any]:
    """The `collection` Bundle `$decide` returns, linked by `urn:uuid` full URLs. `provenance` is
    the audit trail's Provenance for the decision (`audit_fhir.provenance`); `data` is the input
    Bundle, whose full URLs make the references to it absolute."""
    urls = {kind: _urn(result, kind) for kind in ("GuidanceResponse", "RequestGroup", "Task")}
    urls |= {kind: _urn(result, kind) for kind in ("Parameters", "OperationOutcome")}
    task = _located(task, data)
    subject = task.get("for")
    engine = _engine(result)
    issues = _issues(result)
    pack_ref = f"{result.pack_id}@{result.pack_version}"
    guidance: dict[str, Any] = {
        "resourceType": "GuidanceResponse",
        "text": narrative(
            f"Decision {result.id} by {pack_ref}: {result.outcome.value}, priority "
            f"{result.priority.value}, recommendation {result.recommendation or 'none'}.",
        ),
        "identifier": [{"system": DECISION_ID_SYSTEM, "value": result.id}],
        "moduleCodeableConcept": {"coding": [{"system": PACK_TAG_SYSTEM, "code": pack_ref}]},
        "status": "failure" if result.rejected else "success",
        "occurrenceDateTime": result.created_at,
        "performer": engine,
        "outputParameters": {"reference": urls["Parameters"]},
        "result": {"reference": urls["RequestGroup"]},
    }
    if subject:
        guidance["subject"] = subject
    if result.reasons:
        guidance["note"] = [{"text": reason} for reason in result.reasons]
    if issues:
        guidance["evaluationMessage"] = [{"reference": urls["OperationOutcome"]}]
    request_group: dict[str, Any] = {
        "resourceType": "RequestGroup",
        "text": narrative(f"The work item decision {result.id} proposes ({result.outcome.value})."),
        "status": "active",
        "intent": "proposal",
        "priority": result.priority.value,
        "authoredOn": result.created_at,
        "author": engine,
        "action": [
            {
                "title": task["description"],
                "priority": result.priority.value,
                "resource": {"reference": urls["Task"]},
            }
        ],
    }
    if subject:
        request_group["subject"] = subject
    targets = [{"reference": urls["GuidanceResponse"]}, {"reference": urls["Task"]}]
    entries: list[tuple[str, Mapping[str, Any]]] = [
        (urls["GuidanceResponse"], guidance),
        (urls["RequestGroup"], request_group),
        (urls["Task"], {**task, "basedOn": [{"reference": urls["RequestGroup"]}]}),
        (urls["Parameters"], _output(result, pack)),
    ]
    if issues:
        outcome = {
            "resourceType": "OperationOutcome",
            "text": narrative(f"{len(issues)} finding(s) on decision {result.id}."),
            "issue": issues,
        }
        entries.append((urls["OperationOutcome"], outcome))
    with_targets = {**provenance, "target": [*provenance.get("target", []), *targets]}
    entries.append((f"urn:uuid:{uuid.uuid5(_NAMESPACE, str(provenance['id']))}", with_targets))
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "timestamp": result.created_at,
        "entry": [{"fullUrl": url, "resource": resource} for url, resource in entries],
    }


def operation_definition() -> dict[str, Any]:
    """`$decide`'s OperationDefinition, served at `/fhir/OperationDefinition/decide`."""
    return {
        "resourceType": "OperationDefinition",
        "id": "decide",
        "text": narrative(
            "POST [base]/$decide: decide one case with a Mendari agent. In: pack, "
            "version, data (a Bundle), focus. Out: a collection Bundle with a GuidanceResponse, "
            "a RequestGroup and Task, a Parameters, an OperationOutcome when there are findings, "
            "and a Provenance."
        ),
        "url": DECIDE_URL,
        "version": __version__,
        "name": "Decide",
        "title": "Decide with an agent",
        "status": "draft",
        "kind": "operation",
        "experimental": True,
        "publisher": "Mendari",
        "description": _DESCRIPTION,
        "affectsState": True,
        "code": "decide",
        "comment": "Every call records a decision in Mendari's audit trail, so it is POST only. "
        "This is not PlanDefinition/$apply: agents are not PlanDefinitions, and their "
        "logic is not expressed in FHIR. Which packs a deployment serves with FHIR input is in "
        "its CapabilityStatement in open mode; with sign-in on, it is for signed-in callers.",
        "system": True,
        "type": False,
        "instance": False,
        "parameter": [
            _parameter("pack", "in", 1, "string", _PACK_DOC),
            _parameter("version", "in", 0, "string", _VERSION_DOC),
            _parameter("data", "in", 1, "Bundle", _DATA_DOC),
            _parameter("focus", "in", 0, "string", _FOCUS_DOC),
            _parameter("return", "out", 1, "Bundle", _RETURN_DOC),
        ],
    }


def _refuse(code: str, expression: str, diagnostics: str) -> None:
    raise FhirRequestError([{"code": code, "expression": expression, "diagnostics": diagnostics}])


def _located(task: Mapping[str, Any], data: Mapping[str, Any]) -> dict[str, Any]:
    """The Task with `for` and `focus` addressed as `_addresser` says."""
    address = _addresser(data)
    located = dict(task)
    for element in ("for", "focus"):
        if element in task:
            located[element] = {**task[element], "reference": address(task[element]["reference"])}
    return located


def _addresser(data: Mapping[str, Any]) -> Callable[[str], str]:
    """How the output refers to a resource the input refers to. An absolute reference stays.
    Otherwise it is the absolute URL `data` gives the resource, its entry's `fullUrl` when that is
    the resource's RESTful URL (`http(s)://[base]/Type/id`), or else `Type/id`, relative to the
    client's own FHIR server. A `urn:uuid` reference is resolved to its entry first: the output
    holds no entry with that full URL. The first entry for a type and id decides, as the adapter
    keeps the first of duplicates."""
    full_urls: dict[str, str | None] = {}  # Type/id → the first such entry's fullUrl
    named: dict[str, str] = {}  # fullUrl → Type/id
    for full_url, resource in iter_entries(data):
        kind, resource_id = resource.get("resourceType"), resource.get("id")
        if isinstance(kind, str) and isinstance(resource_id, str):
            full_urls.setdefault(f"{kind}/{resource_id}", full_url)
            if full_url is not None:
                named.setdefault(full_url, f"{kind}/{resource_id}")

    def address(reference: str) -> str:
        if reference.startswith(_ABSOLUTE):
            return reference
        local = named.get(reference, reference)
        full_url = full_urls.get(local) or ""
        restful = full_url.startswith(_ABSOLUTE) and full_url.endswith(f"/{local}")
        return full_url if restful else local

    return address


def _urn(result: DecisionResult, kind: str) -> str:
    if kind == "GuidanceResponse":
        return f"urn:uuid:{result.id}"
    return f"urn:uuid:{uuid.uuid5(_NAMESPACE, f'{result.id}/{kind}')}"


def _engine(result: DecisionResult) -> dict[str, Any]:
    pack = f"{result.pack_id}@{result.pack_version}"
    return {
        "identifier": {"system": PACK_ID_SYSTEM, "value": pack},
        "display": f"Mendari engine · {pack}",
    }


def _issues(result: DecisionResult) -> list[dict[str, Any]]:
    """Findings by path and rule (input issues and refusal reasons never carry values)."""
    issues: list[dict[str, Any]] = [
        {
            "severity": issue["severity"],
            "code": issue["code"],
            "diagnostics": f"{issue['resource']}: {issue['diagnostics']}",
            "expression": [issue["expression"]],
        }
        for issue in result.input_issues
    ]
    issues += [
        {
            "severity": "warning",
            "code": _provider_issue(code),
            "diagnostics": f"question {question}: its provider failed ({code})",
        }
        for question, code in sorted(result.errors.items())
    ]
    issues += [
        {"severity": "error", "code": "processing", "diagnostics": f"input rejected: {problem}"}
        for problem in result.schema_problems
    ]
    return issues


def _provider_issue(failure: str) -> str:
    """The issue type for a question's provider failure: its stable code, or for a combined
    provider (`any_of`, `vote`) the members' codes (`member:code, ...`)."""
    codes = {part.rpartition(":")[2].strip() for part in failure.split(",")}
    for code, issue in _PROVIDER_ISSUE.items():
        if code in codes:
            return issue
    return "exception"


def _output(result: DecisionResult, pack: Pack) -> dict[str, Any]:
    params: list[dict[str, Any]] = [
        {
            "name": "outcome",
            "valueCoding": {"system": OUTCOME_SYSTEM, "code": result.outcome.value},
        },
        {"name": "priority", "valueCode": result.priority.value},
    ]
    if result.recommendation is not None:
        coding = _label(pack, result.primary, result.recommendation)
        params.append({"name": "recommendation", "valueCoding": coding})
    if result.confidence is not None:
        params.append({"name": "confidence", "valueDecimal": round(result.confidence, 6)})
    if result.rule:
        params.append({"name": "rule", "valueString": result.rule})
    if result.fallback:
        params.append({"name": "fallback", "valueString": result.fallback})
    params.append({"name": "packDigest", "valueString": result.pack_digest})
    for key, answer in result.answers.items():
        parts: list[dict[str, Any]] = [
            {"name": "question", "valueCode": key},
            {"name": "label", "valueCoding": _label(pack, key, answer.label)},
            {"name": "confidence", "valueDecimal": round(answer.confidence, 6)},
        ]
        if answer.value is not None:
            parts.append({"name": "value", "valueDecimal": round(answer.value, 6)})
        for label, probability in answer.probabilities.items():
            parts.append(
                {
                    "name": "probability",
                    "part": [
                        {"name": "label", "valueCoding": _label(pack, key, label)},
                        {"name": "value", "valueDecimal": round(probability, 6)},
                    ],
                }
            )
        parts += [{"name": "evidence", "valueString": e} for e in answer.evidence if e]
        provider = answer.provider
        parts.append({"name": "provider", "valueString": f"{provider.slot}/{provider.variant}"})
        if provider.model:
            parts.append({"name": "model", "valueString": provider.model})
        params.append({"name": "answer", "part": parts})
    return {"resourceType": "Parameters", "parameter": params}


def _label(pack: Pack, question: str, label: str) -> dict[str, str]:
    """A label in its question's CodeSystem, with the display that CodeSystem gives it."""
    criteria = pack.manifest.questions[question].criteria
    coding = {"system": davinci.question_system(pack.id, question), "code": label}
    if criteria.get(label):
        coding["display"] = display(criteria[label])
    return coding


def _parameter(name: str, use: str, minimum: int, kind: str, doc: str) -> dict[str, Any]:
    return {
        "name": name,
        "use": use,
        "min": minimum,
        "max": "1",
        "documentation": doc,
        "type": kind,
    }


_DESCRIPTION = (
    "Runs one Mendari agent on FHIR R4 data the client sends, records the decision in "
    "Mendari's audit trail, and returns it as FHIR. Mendari is a decision engine, not a FHIR "
    "data server: it reads no data of its own, so everything the pack needs comes in `data`, "
    "which the pack's FHIR mapping turns into its input. `data` first passes a structural R4 "
    "check (a 400 otherwise), then R4 and US Core 6.1.0 rules under the deployment's "
    "`fhir_validation` setting (findings are returned; under `enforce` they refuse the input).\n\n"
    "The result is a `collection` Bundle. Its first entry, a `GuidanceResponse`, is the decision: "
    "`status` is `success` when the pack evaluated the input, whatever the outcome, and "
    "`failure` when the input was refused before evaluation (the outcome is then BLOCK). Whether "
    "to act is `outputParameters.outcome` (AUTO, HUMAN_REVIEW, ABSTAIN or BLOCK, in "
    f"{OUTCOME_SYSTEM}), never `status`. `GuidanceResponse.result` is a `RequestGroup` whose one "
    "action is the `Task` for the work queue; under BLOCK and ABSTAIN that is the pack's "
    "fallback, never the blocked action. The `Parameters` carry the recommendation, priority, "
    "confidence and every question's answer, labels coded in the pack's question CodeSystems. "
    "An `OperationOutcome` lists findings and provider failures by path and rule. A `Provenance` "
    "records the engine as author and the input by hash. The Task's `for` and `focus`, and the "
    "`subject` of the GuidanceResponse and RequestGroup, refer to resources in `data` by the "
    "absolute URL `data` gives them (the entry's RESTful `fullUrl`, e.g. "
    "`http://example.org/fhir/Patient/1`); when it gives none, by `Type/id`, which is relative "
    "to the client's own FHIR server (in the returned Bundle it has no base). "
    "The input resources are not echoed; an answer's evidence can name what matched in them (a "
    "keyword, a code, a value).\n\n"
    "Errors are an `OperationOutcome`: 400 for a malformed request or FHIR JSON of the wrong "
    "shape, 404 for an unknown pack or a version this deployment does not serve, 422 when the "
    "pack takes no FHIR input, when `data` lacks a resource the pack needs, or when it holds "
    "several resources to decide about without `focus`. A provider that cannot answer does not "
    "fail the call: the decision is recorded, the failure is a warning in the OperationOutcome, "
    "and the policy treats the question as unanswered."
)
_PACK_DOC = "The agent, as `namespace/name` (e.g. `mendari/portal-message-agent`)."
_VERSION_DOC = (
    "The pack version the client expects. When the deployment serves another version, the call "
    "fails with 404 and decides nothing."
)
_DATA_DOC = (
    "The FHIR R4 resources to decide on, as a Bundle of any type. Nested search Bundles are "
    "flattened; resources are de-duplicated by type and id. Give each entry its server's "
    "`fullUrl` (`[base]/Type/id`, as a search returns) so the result refers to it absolutely."
)
_FOCUS_DOC = (
    "`ResourceType/id` of the resource in `data` to decide about, when `data` holds several of "
    "the pack's focus type (e.g. one of several draft orders)."
)
_RETURN_DOC = (
    "A collection Bundle: GuidanceResponse (first), RequestGroup, Task, Parameters, "
    "OperationOutcome (when there are findings) and Provenance, linked by urn:uuid full URLs. "
    "References to `data` are the absolute URLs its entries give, else `Type/id`, relative to "
    "the client's own FHIR server. GuidanceResponse.identifier and Task.identifier carry the "
    f"decision id ({DECISION_ID_SYSTEM}), the key for review, audit and feedback."
)
