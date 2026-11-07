# SPDX-License-Identifier: Apache-2.0
"""Da Vinci prior-authorization standards around a pack's decision on an order.

**CRD** (Coverage Requirements Discovery, STU 2.2): when a pack declares
`cds_hooks.coverage_information`, its order-hook response carries, for each decided order, a
system action that updates the order with CRD's `coverage-information` extension: covered or
not, whether prior authorization is needed, which documentation is needed and why, the patient's
Coverage, the date and an assertion id. The assertion id is the decision id, so the assertion
traces back to its audit record. Cards are unchanged, so an EHR that ignores system actions sees
what it saw before. An order that abstained because part of the record could not be read gets
`covered: indeterminate` for a `technical` reason instead (`indeterminate`, LP-23d), when the
Coverage itself was read.
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


# CRD's reason for a determination that could not be made for technical reasons (CRD 2.2.1's
# `coverageAssertionReasons` value set, from its CodeSystem `temp`), and the fixed words that go
# with it: never the key, the reason code or anything the EHR answered.
TECHNICAL = {"system": f"{CRD}/CodeSystem/temp", "code": "technical"}
TECHNICAL_TEXT = (
    "Mendari could not read part of the patient's record from the EHR, so it did not check the "
    "coverage requirements for this order. A person checks them."
)


def indeterminate(
    result: DecisionResult, coverage: Mapping[str, str], today: date
) -> dict[str, Any]:
    """The `coverage-information` extension for an order that abstained because part of the
    record could not be read (LP-23d): `covered` is `indeterminate`, for a `technical` reason
    with its text (crd-ci-q9). The engine emits it; a pack cannot declare it
    (`CoverageInformation.covered`)."""
    return {
        "url": COVERAGE_INFORMATION,
        "extension": [
            {"url": "coverage", "valueReference": dict(coverage)},
            {"url": "covered", "valueCode": "indeterminate"},
            {
                "url": "reason",
                "valueCodeableConcept": {"coding": [dict(TECHNICAL)], "text": TECHNICAL_TEXT},
            },
            {"url": "date", "valueDate": today.isoformat()},
            {"url": "coverage-assertion-id", "valueString": result.id},
        ],
    }


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


def record_answers(record: Mapping[str, Any]) -> dict[str, str]:
    """A stored decision's answers (`AuditStore.get`), each question's label."""
    answers = record["record"].get("answers") or {}
    return {key: str(answer.get("label")) for key, answer in answers.items()}


def decision_questionnaire(pack: Pack, answers: Mapping[str, str]) -> str | None:
    """The canonical (without version) of the DTR questionnaire for a decision with these
    answers, which a QuestionnaireResponse attached to its PAS request answers; None when the
    pack serves none, or the decision's context needs no documentation."""
    form = questionnaire(pack, context_of(pack, answers))
    return str(form["url"]) if form is not None else None


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
# no package can be built for the source data (spec-130, a 4xx). Neither names the decision id,
# so an id that is unknown and one of another organization's get the same answer.
NO_DOCUMENTATION = "the payer needs no documentation for this order"
SIGN_FIRST = (
    "sign the order first (CDS Hooks order-sign), then send the order with the coverage "
    "information it came back with"
)
NO_DECISION = f"no Mendari decision for these orders or this context: {SIGN_FIRST}"


def asked_for(canonical: str, packs: Mapping[str, Pack]) -> tuple[Pack, dict[str, str]] | None:
    """The pack and the context answers a questionnaire canonical names (as `questionnaire_url`
    makes it, with or without `|version`), or None when it names no questionnaire here: another
    server's url, a pack not served or without a questionnaire, a version other than the pack's
    (the only one served; no version is the current one), or names that are not one label of
    each context question (`any`: a question the decision had no answer to)."""
    url, _, version = canonical.partition("|")
    if not url.startswith(f"{PACKS}/"):
        return None
    pack_id, _, names = url.removeprefix(f"{PACKS}/").rpartition("/Questionnaire/")
    pack = packs.get(pack_id)
    hooks = pack.manifest.cds_hooks if pack is not None else None
    dtr = hooks.questionnaire if hooks is not None else None
    if pack is None or dtr is None or version not in ("", pack.version):
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
    (found by CRD's assertion id on the order, or the `context` id), pre-answered, and one for
    each `questionnaire` canonical asked for, unanswered: the union DTR asks for, each
    Questionnaire version once (an order's package stands for a canonical of its questionnaire).
    `lookup(decision id)` gives the decision's pack and answers, the caller's organization's only.

    `outcome` notes a decided order that needs no documentation (DTR oper-8, information), and
    warns (`not-found`) of each questionnaire asked for that is not found here (oper-9) and of
    each order, or the context, that names no decision (DTR: a warning for an order it cannot
    understand), with packages or without. Orders or a context that name no decision, with no
    package to return, are `FhirSourceNotFound` (spec-130: a 4xx); a request that names no
    order, context or questionnaire is `FhirRequestError` (oper-6), and one without the Coverage
    `FhirError`.
    """
    given = [
        (index, p)
        for index, p in enumerate(parameters.get("parameter") or [])
        if isinstance(p, Mapping)
    ]
    coverage = next((p.get("resource") for _, p in given if p.get("name") == "coverage"), None)
    if not isinstance(coverage, Mapping) or coverage.get("resourceType") != "Coverage":
        raise FhirError("$questionnaire-package needs the patient's Coverage (`coverage`)")
    coverage_ref = {"reference": f"Coverage/{coverage.get('id', 'unknown')}"}
    orders = [
        (index, p["resource"])
        for index, p in given
        if p.get("name") == "order" and isinstance(p.get("resource"), Mapping)
    ]
    contexts = [
        (index, str(p["valueString"]))
        for index, p in given
        if p.get("name") == "context" and p.get("valueString")
    ][:1]
    context = contexts[0][1] if contexts else None
    asked = [
        str(p["valueCanonical"])
        for _, p in given
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
    served: set[str] = set()  # each package's questionnaire, `url|version`
    issues: list[dict[str, Any]] = []
    decided = False  # a source named a decision of the caller's (packaged or not)
    wanted = [(index, order, assertion_id(order) or context) for index, order in orders] or [
        (index, None, value) for index, value in contexts
    ]
    for index, order, decision_id in wanted:
        found = lookup(decision_id) if decision_id else None
        if found is None:  # unknown or another organization's: one text, which names no id
            source = "this order" if order is not None else "this context"
            text = f"no Mendari decision for {source}: {SIGN_FIRST}"
            issues.append(_issue("warning", "not-found", text, f"Parameters.parameter[{index}]"))
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
        served.add(_versioned(form))
    for canonical in asked:
        named = asked_for(canonical, packs)
        form = questionnaire(*named) if named is not None else None
        if named is None or form is None:  # none here, or none needed: no such questionnaire
            why = "" if named is None else " (the payer needs no documentation there)"
            text = f"questionnaire not found: {canonical}{why}"
            issues.append(_issue("warning", "not-found", text))
            continue
        if _versioned(form) in served:
            continue  # already returned, by an order's package or an earlier canonical
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
        served.add(_versioned(form))
    if not bundles and wanted and not decided:
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


def _versioned(form: Mapping[str, Any]) -> str:
    return f"{form['url']}|{form['version']}"


def _issue(severity: str, code: str, diagnostics: str, where: str | None = None) -> dict[str, Any]:
    issue: dict[str, Any] = {"severity": severity, "code": code, "diagnostics": diagnostics}
    if where is not None:
        issue["expression"] = [where]  # FHIRPath: the parameter, by its place in the request
    return issue
