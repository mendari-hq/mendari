# SPDX-License-Identifier: Apache-2.0
"""Da Vinci PAS (Prior Authorization Support, STU 2.2): a prior-authorization request, submitted.

When a prior-authorization agent finds a request complete (`submit_ready`), the practice
can send it to the payer as a PAS request Bundle: a Claim with `use = preauthorization`, followed
by what it references (the patient, the Coverage, the payer, the requesting organization and the
order). The resources come from the EHR. Nothing is filled in: a missing element is an error that
names it.

A request that needs attention (`needs_attention`: documentation the payer asks for is missing)
is built the same way, and carries the DTR QuestionnaireResponse a clinician completed for it, when
the request gives one, in `Claim.supportingInfo` (the owner's decision of 2026-11-07). It must be
completed and answer the decision's own DTR questionnaire. A complete request is sent as before:
nothing is attached to it.

X12's code lists are X12's work, under X12's terms: Mendari carries the code systems' names (the
urls in HL7's guide) and none of their code values (LP-14, the owner's decision of 2026-10-07).
The X12 codes a request carries come from the request or the order, else from the site's settings
(`X12_SETTINGS`). PAS requires three: the request type, the certification type and the service
type, and a fourth for an attached QuestionnaireResponse, its report type; one that nothing gives
is an error naming its setting. The others (the Coverage's
relationship code, the organizations' entity codes) are optional in PAS, sent only when set.

`submit` posts the Bundle to the payer's `Claim/$submit` through the outbound guard (the payer
must be on `MENDARI_FHIR_ALLOWED_SERVERS`) and reads the decision in the ClaimResponse. The
payer's own words, its authorization number (tied to one patient) and its disposition, are kept
as a note apart from the audit chain (`payer_note`); the chain keeps the codes, and only codes:
an outcome that is not one of R4's, or a review action that is not shaped like an X12 306 code,
is dropped, so a payer's text never reaches the chain.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from mendari.errors import FhirError, MendariError
from mendari.standards.fhir_client import GuardedHttp

PAS = "http://hl7.org/fhir/us/davinci-pas"
X12 = "https://codesystem.x12.org/005010"
POS = "https://www.cms.gov/Medicare/Coding/place-of-service-codes/Place_of_Service_Code_Set"
CPT = "http://www.ama-assn.org/go/cpt"
HCPCS = "http://www.cms.gov/Medicare/Coding/HCPCSReleaseCodeSets"  # as PAS's value set spells it
ICD10_PCS = "http://www.cms.gov/Medicare/Coding/ICD10"
# The requested service as a payer reads it: PAS binds Claim.item.productOrService (required) to
# X12 278 Requested Service Type, which takes these systems; the first the order is coded in is
# sent. A site sends its own CPT codes (under its licence from the AMA) or HCPCS. ICD-10-PCS is
# HIPAA's standard for hospital inpatient procedures only (45 CFR 162.1002), so on this
# professional claim it stands in for a CPT code in the sandbox alone: the test fixtures'
# fictional codes are in no payer's system, so its synthetic orders also carry a public-domain
# ICD-10-PCS code (`sandbox/synthetic_prior_auth.py`), and the samples validate without CPT.
PROCEDURE_SYSTEMS = (CPT, HCPCS, ICD10_PCS)
ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"
RELATIONSHIP = "http://terminology.hl7.org/CodeSystem/subscriber-relationship"
PROFILE = {
    "Bundle": f"{PAS}/StructureDefinition/profile-pas-request-bundle",
    "Claim": f"{PAS}/StructureDefinition/profile-claim",
    "Patient": f"{PAS}/StructureDefinition/profile-beneficiary",
    "Coverage": f"{PAS}/StructureDefinition/profile-coverage",
    "insurer": f"{PAS}/StructureDefinition/profile-insurer",
    "requestor": f"{PAS}/StructureDefinition/profile-requestor",
    # What the DTR route serves and a DTR client completes (`standards/davinci.py`).
    "QuestionnaireResponse": (
        "http://hl7.org/fhir/us/davinci-dtr/StructureDefinition/dtr-questionnaireresponse"
    ),
}
# PAS 2.2.1's own codes (CodeSystem PASTempCodes), and the extension describing an attachment.
PAS_CODES = f"{PAS}/CodeSystem/PASTempCodes"
DOCUMENT_INFORMATION = f"{PAS}/StructureDefinition/extension-documentInformation"
# The statuses of a QuestionnaireResponse a clinician has completed (R4's value set).
COMPLETED = frozenset({"completed", "amended"})
# The longest payer text kept: an authorization number, and a disposition (free text).
AUTHORIZATION_MAX = 100
DISPOSITION_MAX = 1000
# The shape of an X12 code value: an identifier of 1 to 3 capital letters or digits.
X12_CODE = re.compile(r"[A-Z0-9]{1,3}")
# The codes the chain keeps: R4's ClaimResponse.outcome, and a review action shaped as X12's.
OUTCOMES = frozenset({"queued", "complete", "error", "partial"})
REVIEW_ACTION = X12_CODE
# The recommendations PAS sends: a complete request, and one that needs attention, which carries
# the completed DTR QuestionnaireResponse when the request gives one.
READY = "submit_ready"
ATTENTION = "needs_attention"
# The X12 codes a site sends, by name, and the setting that supplies each (`settings.pas_codes`).
X12_SETTINGS = {
    # Required by PAS: given with the request, else the setting.
    "request_type": "MENDARI_PAS_REQUEST_TYPE",  # 1525, each item's request type
    "certification_type": "MENDARI_PAS_CERTIFICATION_TYPE",  # 1322, each item's
    "service_type": "MENDARI_PAS_SERVICE_TYPE",  # 1365, when neither request nor order gives one
    # Required by PAS when a QuestionnaireResponse is attached.
    "report_type": "MENDARI_PAS_REPORT_TYPE",  # 755, the attachment's report type
    # Optional in PAS: sent only when set.
    "self_relationship": "MENDARI_PAS_SELF_RELATIONSHIP",  # 1069, the patient's own coverage
    "payer_entity": "MENDARI_PAS_PAYER_ENTITY",  # 98, the payer
    "requester_entity": "MENDARI_PAS_REQUESTER_ENTITY",  # 98, the requesting organization
}


class PasError(MendariError):
    """A PAS request can't be built or sent: what is missing, or what the payer answered."""


def request_bundle(
    *,
    decision: Mapping[str, Any],
    order: Mapping[str, Any],
    patient: Mapping[str, Any],
    coverage: Mapping[str, Any],
    insurer: Mapping[str, Any],
    requester: Mapping[str, Any],
    service_type: str | None = None,
    place_of_service: str | None = None,
    diagnoses: Sequence[str] = (),
    request_type: str | None = None,
    certification_type: str | None = None,
    site_codes: Mapping[str, str] | None = None,
    questionnaire_response: Mapping[str, Any] | None = None,
    questionnaire: str | None = None,
    report_type: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The PAS request Bundle for a decided order. `decision` is the pack's decision record (its
    id identifies the request). `service_type` is the X12 service type (1365) and
    `place_of_service` the CMS place-of-service code, unless the order carries them.
    `request_type` (X12 1525) and `certification_type` (1322) are the request's own.
    `site_codes` are the site's X12 codes by name (`X12_SETTINGS`, from `settings.pas_codes`):
    what the request and the order do not give.

    `questionnaire_response`, for a decision that needs attention, is the DTR
    QuestionnaireResponse a clinician completed; it must answer `questionnaire`, the canonical
    (without version) of the decision's own DTR questionnaire (`davinci.decision_questionnaire`),
    and is attached with `report_type` (X12 755) as its report type. A complete request is sent
    without it."""
    now = now or datetime.now(UTC)
    recommendation = decision.get("recommendation")
    if recommendation not in (READY, ATTENTION):
        raise PasError(
            f"only a complete request ({READY}) or one that needs attention ({ATTENTION}) is "
            "sent for prior authorization"
        )
    site = site_codes or {}
    # The owner's decision of 2026-11-07: the completed DTR QuestionnaireResponse goes with a
    # request that needs attention, and only with one; a complete request is what it was.
    attached = questionnaire_response if recommendation == ATTENTION else None
    if attached is not None:
        _check_completed(attached, questionnaire)
        report_type = report_type or site.get("report_type")
    request_type = request_type or site.get("request_type")
    certification_type = certification_type or site.get("certification_type")
    service_type = (
        service_type or _code(order.get("category"), f"{X12}/1365") or site.get("service_type")
    )
    place_of_service = place_of_service or _code(order.get("locationCode"), POS)
    procedure_system, procedure = _procedure(order.get("code"))
    problems = _missing(patient, coverage, insurer, requester)
    problems += [
        f"the order's {name}"
        for name, value in (
            ("procedure code in CPT, HCPCS or ICD-10-PCS", procedure),
            ("place of service", place_of_service),
        )
        if not value
    ]
    problems += [
        f"the X12 {key.replace('_', ' ')} ({element}: from {source}, or set {X12_SETTINGS[key]})"
        for key, element, source, value in (
            ("service_type", 1365, "the order or the request", service_type),
            ("request_type", 1525, "the request", request_type),
            ("certification_type", 1322, "the request", certification_type),
            *((("report_type", 755, "the request", report_type),) if attached else ()),
        )
        if not value
    ]
    if problems:
        raise PasError(f"a PAS request needs {', '.join(problems)}")

    request_id = str(decision["id"])
    refs = {
        kind: f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'{request_id}/{kind}')}"
        for kind in ("patient", "coverage", "insurer", "requester", "order", "claim", "response")
    }
    day = now.date().isoformat()
    claim: dict[str, Any] = {
        "resourceType": "Claim",
        "meta": {"profile": [PROFILE["Claim"]]},
        "identifier": [
            {"system": "https://mendari.co/fhir/decision", "value": str(decision["id"])}
        ],
        "status": "active",
        "type": {
            "coding": [
                {
                    "system": "http://terminology.hl7.org/CodeSystem/claim-type",
                    "code": "professional",
                }
            ]
        },
        "use": "preauthorization",
        "patient": {"reference": refs["patient"]},
        "created": now.isoformat(timespec="seconds"),
        "insurer": {"reference": refs["insurer"]},
        "provider": {"reference": refs["requester"]},
        "priority": {
            "coding": [
                {
                    "system": "http://terminology.hl7.org/CodeSystem/processpriority",
                    "code": "normal",
                }
            ]
        },
        "insurance": [{"sequence": 1, "focal": True, "coverage": {"reference": refs["coverage"]}}],
        "item": [
            {
                "extension": [
                    {
                        "url": f"{PAS}/StructureDefinition/extension-serviceItemRequestType",
                        "valueCodeableConcept": {
                            "coding": [{"system": f"{X12}/1525", "code": request_type}]
                        },
                    },
                    {
                        "url": f"{PAS}/StructureDefinition/extension-certificationType",
                        "valueCodeableConcept": {
                            "coding": [{"system": f"{X12}/1322", "code": certification_type}]
                        },
                    },
                ],
                "sequence": 1,
                "category": {"coding": [{"system": f"{X12}/1365", "code": service_type}]},
                "productOrService": {"coding": [{"system": procedure_system, "code": procedure}]},
                "servicedDate": day,
                "locationCodeableConcept": {"coding": [{"system": POS, "code": place_of_service}]},
            }
        ],
    }
    if diagnoses:
        claim["diagnosis"] = [
            {
                "sequence": n,
                "diagnosisCodeableConcept": {"coding": [{"system": ICD10, "code": code}]},
            }
            for n, code in enumerate(diagnoses, start=1)
        ]
    if attached is not None:
        # PAS 2.2.1, profile-claim (http://hl7.org/fhir/us/davinci-pas/StructureDefinition/
        # profile-claim), slice Claim.supportingInfo:AdditionalInformation: category
        # PASTempCodes#additionalInformation, extension-documentInformation with its required
        # reportTypeCode (X12 755, the site's), and value[x] a Reference only, whose targets
        # include a QuestionnaireResponse. The response is not contained in the Claim: it is an
        # entry of the request Bundle, referenced by its fullUrl, as PAS's referral example
        # (Bundle-ReferralAuthorizationBundleExample) carries its DocumentReference.
        claim["supportingInfo"] = [
            {
                "extension": [
                    {
                        "url": DOCUMENT_INFORMATION,
                        "extension": [
                            {
                                "url": "reportTypeCode",
                                "valueCodeableConcept": {
                                    "coding": [{"system": f"{X12}/755", "code": report_type}]
                                },
                            }
                        ],
                    }
                ],
                "sequence": 1,
                "category": {"coding": [{"system": PAS_CODES, "code": "additionalInformation"}]},
                "valueReference": {"reference": refs["response"]},
            }
        ]
    resources = [
        (refs["claim"], claim),
        (refs["patient"], _profiled(patient, PROFILE["Patient"])),
        (refs["coverage"], _coverage(coverage, refs, site.get("self_relationship"))),
        (refs["insurer"], _organization(insurer, PROFILE["insurer"], site.get("payer_entity"))),
        (
            refs["requester"],
            _organization(requester, PROFILE["requestor"], site.get("requester_entity")),
        ),
        (refs["order"], _order(order, refs["patient"])),
    ]
    if attached is not None:
        resources.append((refs["response"], _response(attached, refs["patient"])))
    return {
        "resourceType": "Bundle",
        "meta": {"profile": [PROFILE["Bundle"]]},
        "identifier": {
            "system": "https://mendari.co/fhir/pas-request",
            "value": str(decision["id"]),
        },
        "type": "collection",
        "timestamp": now.isoformat(timespec="seconds"),
        "entry": [{"fullUrl": url, "resource": resource} for url, resource in resources],
    }


def submit(bundle: Mapping[str, Any], payer: str, http: GuardedHttp) -> dict[str, Any]:
    """Post the Bundle to the payer's `Claim/$submit`; the payer's decision, in short."""
    try:
        answer = http.send_json("POST", f"{payer.rstrip('/')}/Claim/$submit", bundle)
    except FhirError as failure:
        raise PasError(f"the payer did not take the request: {failure}") from None
    return outcome(answer)


def outcome(answer: Mapping[str, Any]) -> dict[str, Any]:
    """The ClaimResponse in a PAS response Bundle: its outcome, the authorization number, and
    each item's review action (an X12 306 code, as the payer sends it). An outcome or a review
    action that is not a code (a payer's mistake) is dropped."""
    entries = [e.get("resource") for e in answer.get("entry") or [] if isinstance(e, Mapping)]
    response = next(
        (r for r in entries if isinstance(r, Mapping) and r.get("resourceType") == "ClaimResponse"),
        None,
    )
    if response is None:
        raise PasError("the payer's answer has no ClaimResponse")
    actions = []
    for item in response.get("item") or []:
        for adjudication in item.get("adjudication") or []:
            for extension in adjudication.get("extension") or []:
                if extension.get("url", "").endswith("extension-reviewAction"):
                    for part in extension.get("extension") or []:
                        if part.get("url", "").endswith("extension-reviewActionCode"):
                            actions.append(_code(part.get("valueCodeableConcept"), f"{X12}/306"))
    code = response.get("outcome")
    return {
        "outcome": code if isinstance(code, str) and code in OUTCOMES else None,
        "authorization": _text(response.get("preAuthRef"), AUTHORIZATION_MAX),
        "review_actions": [a for a in actions if a and REVIEW_ACTION.fullmatch(a)],
        "disposition": _text(response.get("disposition"), DISPOSITION_MAX),
    }


def payer_note(answer: Mapping[str, Any]) -> str | None:
    """The payer's words in an answer (`outcome`), as one note (compact, sorted JSON), or None
    when it gave neither an authorization number nor a disposition."""
    words = {key: answer.get(key) for key in ("authorization", "disposition")}
    if not any(words.values()):
        return None
    return json.dumps(words, sort_keys=True, separators=(",", ":"))


def _text(value: Any, limit: int) -> str | None:
    """A payer's string, cut to `limit` characters; anything else (a payer's mistake) is
    dropped rather than kept."""
    return value[:limit] if isinstance(value, str) and value else None


def _procedure(concept: Any) -> tuple[str, str | None]:
    """The order's procedure as (system, code), from the first system in `PROCEDURE_SYSTEMS` it is
    coded in; (CPT, None) when it is coded in none."""
    for system in PROCEDURE_SYSTEMS:
        code = _code(concept, system)
        if code:
            return system, code
    return CPT, None


def _code(concept: Any, system: str) -> str | None:
    concepts = concept if isinstance(concept, list) else [concept]
    for item in concepts:
        if isinstance(item, Mapping):
            for coding in item.get("coding") or []:
                if (
                    isinstance(coding, Mapping)
                    and coding.get("system") == system
                    and coding.get("code")
                ):
                    return str(coding["code"])
    return None


def _missing(
    patient: Mapping[str, Any],
    coverage: Mapping[str, Any],
    insurer: Mapping[str, Any],
    requester: Mapping[str, Any],
) -> list[str]:
    problems = []
    for key, what in (
        ("name", "name"),
        ("identifier", "identifier"),
        ("gender", "gender"),
        ("birthDate", "birth date"),
    ):
        if not patient.get(key):
            problems.append(f"the patient's {what}")
    member = [
        i
        for i in coverage.get("identifier") or []
        if _code(i.get("type"), "http://terminology.hl7.org/CodeSystem/v2-0203") == "MB"
    ]
    if not member:
        problems.append("the coverage's member id (identifier of type MB)")
    for organization, who in ((insurer, "payer"), (requester, "requesting organization")):
        if not organization.get("name"):
            problems.append(f"the {who}'s name")
    return problems


def _profiled(resource: Mapping[str, Any], profile: str) -> dict[str, Any]:
    copied = {k: v for k, v in resource.items() if k not in ("meta", "id", "text")}
    return {"resourceType": resource["resourceType"], "meta": {"profile": [profile]}, **copied}


def _coverage(
    coverage: Mapping[str, Any], refs: Mapping[str, str], x12_self: str | None
) -> dict[str, Any]:
    """The patient's own coverage, with the site's X12 code for self (1069) beside FHIR's when it
    sets one. PAS's rule `self-beneficiary` asks for the subscriber unless that X12 code is
    there: without it the subscriber is named, the patient, whose own coverage this is."""
    relationship = _code(coverage.get("relationship"), RELATIONSHIP) or "self"
    if relationship != "self":
        raise PasError("PAS requests for a dependent's coverage need the subscriber: not supported")
    copied = _profiled(coverage, PROFILE["Coverage"])
    coding = [{"system": RELATIONSHIP, "code": "self"}]
    if x12_self:
        coding.append({"system": f"{X12}/1069", "code": x12_self})
    copied["relationship"] = {"coding": coding}
    copied["beneficiary"] = {"reference": refs["patient"]}
    copied["payor"] = [{"reference": refs["insurer"]}]
    copied.pop("subscriber", None)
    if not x12_self:
        copied["subscriber"] = {"reference": refs["patient"]}
    return copied


def _organization(
    organization: Mapping[str, Any], profile: str, entity: str | None
) -> dict[str, Any]:
    """The payer or the requesting organization, typed with the site's X12 entity code (98) when
    it sets one, else untyped, as PAS allows. The EHR's own type is never sent: PAS binds the
    type to X12's list (required)."""
    copied = _profiled(organization, profile)
    copied["active"] = True
    if entity:
        copied["type"] = [{"coding": [{"system": f"{X12}/98", "code": entity}]}]
    else:
        copied.pop("type", None)
    return copied


def _order(order: Mapping[str, Any], patient: str) -> dict[str, Any]:
    copied = {k: v for k, v in order.items() if k not in ("meta", "id", "text", "extension")}
    return {"resourceType": "ServiceRequest", **copied, "subject": {"reference": patient}}


def _check_completed(response: Mapping[str, Any], questionnaire: str | None) -> None:
    """A QuestionnaireResponse is attached once a clinician has completed it, and only when it
    answers the decision's own DTR questionnaire. The errors name Mendari's canonical, never what
    the request sent."""
    if response.get("resourceType") != "QuestionnaireResponse":
        raise PasError("questionnaire_response must be a FHIR QuestionnaireResponse")
    status = response.get("status")
    if not isinstance(status, str) or status not in COMPLETED:
        raise PasError(
            "the QuestionnaireResponse is attached once a clinician has completed it (status "
            "completed or amended)"
        )
    if questionnaire is None:
        raise PasError("this decision has no DTR questionnaire for a QuestionnaireResponse")
    answered = response.get("questionnaire")
    if not isinstance(answered, str) or answered.partition("|")[0] != questionnaire:
        raise PasError(
            f"the QuestionnaireResponse answers another questionnaire: this decision's is "
            f"{questionnaire}"
        )


def _response(response: Mapping[str, Any], patient: str) -> dict[str, Any]:
    """The completed QuestionnaireResponse as the EHR gave it, its subject the request's patient."""
    copied = _profiled(response, PROFILE["QuestionnaireResponse"])
    copied["subject"] = {"reference": patient}
    return copied
