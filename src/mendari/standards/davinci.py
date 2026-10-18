# SPDX-License-Identifier: Apache-2.0
"""Da Vinci prior-authorization standards around a pack's decision on an order.

**CRD** (Coverage Requirements Discovery, STU 2.2): when a pack declares
`cds_hooks.coverage_information`, its order-hook response carries, for each decided order, a
system action that updates the order with CRD's `coverage-information` extension: covered or
not, whether prior authorization is needed, which documentation is needed and why, the patient's
Coverage, the date and an assertion id. The assertion id is the decision id, so the assertion
traces back to its audit record. Cards are unchanged, so an EHR that ignores system actions sees
what it saw before.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

from mendari.core.decision import DecisionResult
from mendari.core.origin import COMMUNITY_LABEL
from mendari.core.pack import Pack
from mendari.core.spec import CoverageInformation
from mendari.errors import FhirError, FhirRequestError, FhirSourceNotFound
from mendari.standards.fhir import display

CRD = "http://hl7.org/fhir/us/davinci-crd"
COVERAGE_INFORMATION = f"{CRD}/StructureDefinition/ext-coverage-information"


def coverage_reference(prefetch: Mapping[str, Any]) -> dict[str, str] | None:
    """The patient's Coverage from the `coverage` prefetch (a resource or a search Bundle)."""
    found = prefetch.get("coverage")
    if isinstance(found, Mapping) and found.get("resourceType") == "Bundle":
        entries = [e.get("resource") for e in found.get("entry") or [] if isinstance(e, Mapping)]
        found = next((r for r in entries if isinstance(r, Mapping)), None)
    if isinstance(found, Mapping) and found.get("resourceType") == "Coverage" and found.get("id"):
        return {"reference": f"Coverage/{found['id']}"}
    return None


def coverage_information(
    info: CoverageInformation,
    result: DecisionResult,
    coverage: Mapping[str, str],
    today: date,
    questionnaire: str | None = None,
) -> dict[str, Any]:
    """The `coverage-information` extension for one decided order; `questionnaire` is the DTR
    questionnaire for what is missing (a canonical)."""
    parts: list[dict[str, Any]] = [
        {"url": "coverage", "valueReference": dict(coverage)},
        {"url": "covered", "valueCode": info.covered},
    ]
    if info.pa_needed is not None:
        parts.append({"url": "pa-needed", "valueCode": info.pa_needed})
    parts += [{"url": "doc-needed", "valueCode": code} for code in info.doc_needed]
    parts += [{"url": "doc-purpose", "valueCode": code} for code in info.doc_purpose]
    parts += [{"url": "info-needed", "valueCode": code} for code in info.info_needed]
    if info.reason:
        parts.append({"url": "reason", "valueCodeableConcept": {"text": info.reason}})
    if questionnaire is not None and info.doc_needed:
        parts.append({"url": "questionnaire", "valueCanonical": questionnaire})
    parts += [
        {"url": "date", "valueDate": today.isoformat()},
        {"url": "coverage-assertion-id", "valueString": result.id},
    ]
    return {"url": COVERAGE_INFORMATION, "extension": parts}


def system_action(order: Mapping[str, Any], extension: Mapping[str, Any]) -> dict[str, Any]:
    """A CDS Hooks `update` of the draft order, with the coverage information added."""
    updated = copy.deepcopy(dict(order))
    updated["extension"] = [*updated.get("extension", []), dict(extension)]
    return {
        "type": "update",
        "description": "Coverage information for this order (Da Vinci CRD)",
        "resource": updated,
    }


# -- DTR (Documentation Templates and Rules, STU 2.2) --------------------------------------------
# A pack that declares `cds_hooks.questionnaire` serves `Questionnaire/$questionnaire-package`:
# for an order it decided (found by CRD's assertion id, which is the decision id), a package
# Bundle holding a Questionnaire of the documentation the payer asks for, for the order's service
# and payer, and a QuestionnaireResponse pre-answered from what the pack found in the record.

DTR = "http://hl7.org/fhir/us/davinci-dtr"
PACKS = "https://mendari.co/packs"


def requirements(pack: Pack, context: Mapping[str, str]) -> list[str]:
    """The members of the derived primary question that can make the decision `missing` for
    these context answers (e.g. service and payer), in table order."""
    dtr = pack.manifest.cds_hooks.questionnaire if pack.manifest.cds_hooks else None
    spec = pack.manifest.questions[pack.manifest.policy.primary].derive
    if dtr is None or spec is None:
        return []
    known = set(dtr.context)
    found: list[str] = []
    for row in spec.table[:-1]:
        scope = [m for m in spec.members if row.applies_to(m)]
        gated = {g: labels for g, labels in row.gates.items() if g in known}
        if any(context.get(g) not in labels for g, labels in gated.items()):
            continue  # a row for another service or payer
        if set(scope) <= known and set(row.gates) <= known:
            holds = any(context.get(m) in row.labels for m in scope)
            if row.any is None:
                holds = all(context.get(m) in row.labels for m in scope)
            if holds and row.result != dtr.missing:
                break  # decided by the context alone (e.g. no authorization needed)
            continue
        if row.result == dtr.missing:
            found += [m for m in scope if m not in known and m not in found]
    return found


def questionnaire_url(pack: Pack, context: Mapping[str, str]) -> str:
    dtr = pack.manifest.cds_hooks.questionnaire if pack.manifest.cds_hooks else None
    names = [context.get(key, "any") for key in (dtr.context if dtr else [])]
    return f"{PACKS}/{pack.id}/Questionnaire/{'-'.join(names)}"


def context_of(pack: Pack, answers: Mapping[str, str]) -> dict[str, str]:
    dtr = pack.manifest.cds_hooks.questionnaire if pack.manifest.cds_hooks else None
    return {key: answers[key] for key in (dtr.context if dtr else []) if key in answers}


def questionnaire(pack: Pack, context: Mapping[str, str]) -> dict[str, Any] | None:
    """A DTR standard Questionnaire of the documentation the payer asks for; None when the
    context needs none (e.g. a service that doesn't require authorization)."""
    dtr = pack.manifest.cds_hooks.questionnaire if pack.manifest.cds_hooks else None
    needed = requirements(pack, context)
    if dtr is None or not needed:
        return None
    url = questionnaire_url(pack, context)
    items = []
    for key in needed:
        question = pack.manifest.questions[key]
        options = [label for label in question.labels if label != "unknown"]
        items.append(
            {
                "linkId": key,
                "text": question.instructions,
                "type": "choice",
                "required": True,
                "answerOption": [{"valueCoding": _coding(pack, key, label)} for label in options],
            }
        )
    shown = ", ".join(f"{k} {v}" for k, v in context.items())
    title = f"{dtr.title} ({shown})"
    labelled: dict[str, Any] = {}
    if pack.community:
        # An outside developer's criteria: published by them, experimental, and labelled.
        title = f"{title} ({COMMUNITY_LABEL})"
        labelled = {"experimental": True, "publisher": pack.manifest.publisher.name}
    return {
        "resourceType": "Questionnaire",
        "id": url.rsplit("/", 1)[-1].replace("_", "-"),
        "meta": {"profile": [f"{DTR}/StructureDefinition/dtr-std-questionnaire"]},
        "extension": [
            {
                "url": "http://hl7.org/fhir/StructureDefinition/artifact-versionAlgorithm",
                "valueCoding": {
                    "system": "http://hl7.org/fhir/version-algorithm",
                    "code": "semver",
                },
            }
        ],
        "url": url,
        "version": pack.version,
        "name": "".join(
            part.title() for part in url.rsplit("/", 1)[-1].replace("-", "_").split("_")
        ),
        "title": title,
        "status": "active",
        **labelled,
        "subjectType": ["Patient"],
        "item": items,
    }


def questionnaire_response(
    pack: Pack,
    form: Mapping[str, Any],
    answers: Mapping[str, str],
    *,
    subject: Mapping[str, str],
    coverage: Mapping[str, str],
    order: Mapping[str, str] | None,
    authored: str,
) -> dict[str, Any]:
    """The Questionnaire pre-answered with the pack's answers (never `unknown`), each marked as
    filled in automatically on the server."""
    items = []
    for item in form["item"]:
        label = answers.get(item["linkId"])
        if label is None or label == "unknown":
            continue
        origin = {
            "url": f"{DTR}/StructureDefinition/information-origin",
            "extension": [{"url": "source", "valueCode": "auto-server"}],
        }
        items.append(
            {
                "linkId": item["linkId"],
                "text": item["text"],
                "answer": [
                    {"valueCoding": _coding(pack, item["linkId"], label), "extension": [origin]}
                ],
            }
        )
    extensions: list[dict[str, Any]] = [
        {"url": f"{DTR}/StructureDefinition/qr-coverage", "valueReference": dict(coverage)},
        {
            "url": f"{DTR}/StructureDefinition/intendedUse",
            "valueCodeableConcept": {
                "coding": [
                    {"system": f"{CRD}/CodeSystem/coverage-information-codes", "code": "withpa"}
                ]
            },
        },
    ]
    if order is not None:
        extensions.append(
            {"url": f"{DTR}/StructureDefinition/qr-context", "valueReference": dict(order)}
        )
    response: dict[str, Any] = {
        "resourceType": "QuestionnaireResponse",
        "id": f"{form['id']}-response",
        "meta": {"profile": [f"{DTR}/StructureDefinition/dtr-questionnaireresponse"]},
        "extension": extensions,
        "questionnaire": f"{form['url']}|{form['version']}",
        "status": "in-progress",
        "subject": dict(subject),
        "authored": authored,
    }
    if items:
        response["item"] = items
    return response


def package(form: Mapping[str, Any], response: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "resourceType": "Bundle",
        "meta": {"profile": [f"{DTR}/StructureDefinition/DTR-QPackageBundle"]},
        "type": "collection",
        "entry": [
            {"fullUrl": f"urn:uuid:{uuid.uuid4()}", "resource": dict(form)},
            {"fullUrl": f"urn:uuid:{uuid.uuid4()}", "resource": dict(response)},
        ],
    }


def assertion_id(order: Mapping[str, Any]) -> str | None:
    """CRD's coverage-assertion-id on an order (the decision id), if the EHR kept it."""
    for extension in order.get("extension") or []:
        if isinstance(extension, Mapping) and extension.get("url") == COVERAGE_INFORMATION:
            for part in extension.get("extension") or []:
                if part.get("url") == "coverage-assertion-id":
                    return str(part.get("valueString"))
    return None


def question_system(pack_id: str, question: str) -> str:
    """The CodeSystem of a question's labels (`terminology.py` generates it)."""
    return f"{PACKS}/{pack_id}/CodeSystem/{question}"


def _coding(pack: Pack, question: str, label: str) -> dict[str, str]:
    return {
        "system": question_system(pack.id, question),
        "code": label,
        "display": display(pack.manifest.questions[question].criteria.get(label, label)),
    }


Lookup = Callable[[str], tuple[Pack, dict[str, str]] | None]

# What the operation answers, besides its packages (`outcome`, DTR oper-8 and oper-9), and when
# no package can be built for the source data (spec-130, a 4xx). The refusal names no id, so an
# id that is unknown and one of another organization's get the same answer.
NO_DOCUMENTATION = "the payer needs no documentation for this order"
NO_DECISION = (
    "no Mendari decision for these orders or this context: sign the order first (CDS Hooks "
    "order-sign), then send the order with the coverage information it came back with"
)


def asked_for(canonical: str, packs: Mapping[str, Pack]) -> tuple[Pack, dict[str, str]] | None:
    """The pack and the context answers a questionnaire canonical names (as `questionnaire_url`
    makes it; its version is not read), or None when it names no questionnaire here: another
    server's url, a pack not served or without a questionnaire, or names that are not one label
    of each context question (`any`: a question the decision had no answer to)."""
    url = canonical.split("|", 1)[0]
    if not url.startswith(f"{PACKS}/"):
        return None
    pack_id, _, names = url.removeprefix(f"{PACKS}/").rpartition("/Questionnaire/")
    pack = packs.get(pack_id)
    hooks = pack.manifest.cds_hooks if pack is not None else None
    dtr = hooks.questionnaire if hooks is not None else None
    if pack is None or dtr is None:
        return None
    given = names.split("-")
    if len(given) != len(dtr.context):
        return None
    for key, name in zip(dtr.context, given, strict=True):
        if name != "any" and name not in pack.manifest.questions[key].labels:
            return None
    return pack, dict(zip(dtr.context, given, strict=True))


def questionnaire_package(
    parameters: Mapping[str, Any], lookup: Lookup, packs: Mapping[str, Pack], authored: str
) -> dict[str, Any]:
    """`Questionnaire/$questionnaire-package`: a package Bundle for each order the pack decided
    (found by CRD's assertion id on the order, or the `context` id), else for each `questionnaire`
    canonical asked for, unanswered. `lookup(decision id)` gives the decision's pack and answers,
    the caller's organization's only.

    `outcome` notes a decided order that needs no documentation (DTR oper-8, information) and
    warns of each questionnaire asked for that is not found here (oper-9, `not-found`), with
    packages or without. Orders or a context that name no decision, with no package to return,
    are `FhirSourceNotFound` (spec-130: a 4xx); a request that names no order, context or
    questionnaire is `FhirRequestError` (oper-6), and one without the Coverage `FhirError`.
    """
    given = [p for p in parameters.get("parameter") or [] if isinstance(p, Mapping)]
    coverage = next((p.get("resource") for p in given if p.get("name") == "coverage"), None)
    if not isinstance(coverage, Mapping) or coverage.get("resourceType") != "Coverage":
        raise FhirError("$questionnaire-package needs the patient's Coverage (`coverage`)")
    coverage_ref = {"reference": f"Coverage/{coverage.get('id', 'unknown')}"}
    orders = [
        p["resource"]
        for p in given
        if p.get("name") == "order" and isinstance(p.get("resource"), Mapping)
    ]
    context = next(
        (
            str(p["valueString"])
            for p in given
            if p.get("name") == "context" and p.get("valueString")
        ),
        None,
    )
    asked = [
        str(p["valueCanonical"])
        for p in given
        if p.get("name") == "questionnaire" and p.get("valueCanonical")
    ]
    if not orders and context is None and not asked:
        raise FhirRequestError(
            [
                {
                    "code": "required",
                    "expression": "Parameters.parameter",
                    "diagnostics": "$questionnaire-package needs an `order`, a `context` or a "
                    "`questionnaire` (DTR oper-6)",
                }
            ]
        )
    beneficiary = (
        coverage.get("beneficiary") if isinstance(coverage.get("beneficiary"), Mapping) else None
    )

    bundles: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []
    decided = False  # a source named a decision of the caller's (packaged or not)
    wanted = [(order, assertion_id(order) or context) for order in orders] or [(None, context)]
    for order, decision_id in wanted:
        found = lookup(decision_id) if decision_id else None
        if found is None:
            continue
        decided = True
        pack, answers = found
        form = questionnaire(pack, context_of(pack, answers))
        if form is None:
            issues.append(_issue("information", "informational", NO_DOCUMENTATION))
            continue
        subject = (order or {}).get("subject") or beneficiary or {"reference": "Patient/unknown"}
        order_ref = {"reference": f"{order['resourceType']}/{order.get('id')}"} if order else None
        response = questionnaire_response(
            pack,
            form,
            answers,
            subject=subject,
            coverage=coverage_ref,
            order=order_ref,
            authored=authored,
        )
        bundles.append(package(form, response))
    for_orders = bool(bundles)
    for canonical in asked:
        named = asked_for(canonical, packs)
        form = questionnaire(*named) if named is not None else None
        if named is None or form is None:  # none here, or none needed: no such questionnaire
            why = "" if named is None else " (the payer needs no documentation there)"
            text = f"questionnaire not found: {canonical}{why}"
            issues.append(_issue("warning", "not-found", text))
            continue
        if for_orders:
            continue  # the orders' packages answer: a questionnaire asked for is not added
        response = questionnaire_response(
            named[0],
            form,
            {},
            subject=beneficiary or {"reference": "Patient/unknown"},
            coverage=coverage_ref,
            order=None,
            authored=authored,
        )
        bundles.append(package(form, response))
    if not bundles and (orders or context is not None) and not decided:
        raise FhirSourceNotFound(NO_DECISION)
    output: list[dict[str, Any]] = [{"name": "packagebundle", "resource": b} for b in bundles]
    if issues:
        output.append(
            {"name": "outcome", "resource": {"resourceType": "OperationOutcome", "issue": issues}}
        )
    return {
        "resourceType": "Parameters",
        "meta": {"profile": [f"{DTR}/StructureDefinition/dtr-qpackage-output-parameters"]},
        "parameter": output,
    }


def _issue(severity: str, code: str, diagnostics: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "diagnostics": diagnostics}
