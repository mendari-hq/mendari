# SPDX-License-Identifier: Apache-2.0
"""Da Vinci PAS (Prior Authorization Support, STU 2.2): a complete request, submitted.

When the Prior Authorization Preflight finds a request complete (`submit_ready`), the practice
can send it to the payer as a PAS request Bundle: a Claim with `use = preauthorization`, followed
by what it references (the patient, the Coverage, the payer, the requesting organization and the
order). The resources come from the EHR, and the X12 codes the payer needs come from the request
or the order. Nothing is filled in: a missing element is an error that names it.

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
# The Prior Authorization Preflight's synthetic orders: invented procedure codes, named as such,
# never billed (LP-14). A site sends its own CPT codes, under its own licence from the AMA.
FICTIONAL_PROCEDURES = "https://mendari.co/fhir/CodeSystem/fictional-procedures"
PROCEDURE_SYSTEMS = (CPT, FICTIONAL_PROCEDURES)
ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"
RELATIONSHIP = "http://terminology.hl7.org/CodeSystem/subscriber-relationship"
PROFILE = {
    "Bundle": f"{PAS}/StructureDefinition/profile-pas-request-bundle",
    "Claim": f"{PAS}/StructureDefinition/profile-claim",
    "Patient": f"{PAS}/StructureDefinition/profile-beneficiary",
    "Coverage": f"{PAS}/StructureDefinition/profile-coverage",
    "insurer": f"{PAS}/StructureDefinition/profile-insurer",
    "requestor": f"{PAS}/StructureDefinition/profile-requestor",
}
# The longest payer text kept: an authorization number, and a disposition (free text).
AUTHORIZATION_MAX = 100
DISPOSITION_MAX = 1000
# The codes the chain keeps: R4's ClaimResponse.outcome, and the shape of an X12 306 code.
OUTCOMES = frozenset({"queued", "complete", "error", "partial"})
REVIEW_ACTION = re.compile(r"[A-Z0-9]{1,3}")


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
    request_type: str = "HS",
    certification_type: str = "I",
    now: datetime | None = None,
) -> dict[str, Any]:
    """The PAS request Bundle for a decided order. `decision` is the pack's decision record (its
    id identifies the request). `service_type` is the X12 service type (1365) and
    `place_of_service` the CMS place-of-service code, unless the order carries them.
    `request_type` (X12 1525) and `certification_type` (1322) default to a health services review
    for an initial request."""
    now = now or datetime.now(UTC)
    if decision.get("recommendation") != "submit_ready":
        raise PasError("only a complete request (submit_ready) is sent for prior authorization")
    service_type = service_type or _code(order.get("category"), f"{X12}/1365")
    place_of_service = place_of_service or _code(order.get("locationCode"), POS)
    procedure_system, procedure = _procedure(order.get("code"))
    problems = _missing(patient, coverage, insurer, requester)
    problems += [
        f"the order's {name}"
        for name, value in (
            ("procedure code (CPT, or the reference pack's fictional code)", procedure),
            ("X12 service type (1365)", service_type),
            ("place of service", place_of_service),
        )
        if not value
    ]
    if problems:
        raise PasError(f"a PAS request needs {', '.join(problems)}")

    request_id = str(decision["id"])
    refs = {
        kind: f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'{request_id}/{kind}')}"
        for kind in ("patient", "coverage", "insurer", "requester", "order", "claim")
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
    resources = [
        (refs["claim"], claim),
        (refs["patient"], _profiled(patient, PROFILE["Patient"])),
        (refs["coverage"], _coverage(coverage, refs)),
        (refs["insurer"], _organization(insurer, PROFILE["insurer"], "PR")),
        (refs["requester"], _organization(requester, PROFILE["requestor"], "1P")),
        (refs["order"], _order(order, refs["patient"])),
    ]
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
    each item's review action (X12 306: A1 certified, A3 not certified, A4 pended...). An
    outcome or a review action that is not a code (a payer's mistake) is dropped."""
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


def _coverage(coverage: Mapping[str, Any], refs: Mapping[str, str]) -> dict[str, Any]:
    """The patient's own coverage, with the X12 relationship code PAS reads (1069: 18, self)."""
    relationship = _code(coverage.get("relationship"), RELATIONSHIP) or "self"
    if relationship != "self":
        raise PasError("PAS requests for a dependent's coverage need the subscriber: not supported")
    copied = _profiled(coverage, PROFILE["Coverage"])
    copied["relationship"] = {
        "coding": [
            {"system": RELATIONSHIP, "code": "self"},
            {"system": f"{X12}/1069", "code": "18"},
        ]
    }
    copied["beneficiary"] = {"reference": refs["patient"]}
    copied["payor"] = [{"reference": refs["insurer"]}]
    copied.pop("subscriber", None)
    return copied


def _organization(organization: Mapping[str, Any], profile: str, entity: str) -> dict[str, Any]:
    copied = _profiled(organization, profile)
    copied["active"] = True
    copied["type"] = [{"coding": [{"system": f"{X12}/98", "code": entity}]}]
    return copied


def _order(order: Mapping[str, Any], patient: str) -> dict[str, Any]:
    copied = {k: v for k, v in order.items() if k not in ("meta", "id", "text", "extension")}
    return {"resourceType": "ServiceRequest", **copied, "subject": {"reference": patient}}
