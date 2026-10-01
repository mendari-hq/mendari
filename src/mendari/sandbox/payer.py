"""A mock payer for the sandbox: `Claim/$submit` for Da Vinci PAS requests.

It certifies every well-formed prior-authorization request (X12 306 review action A1) with a
synthetic authorization number derived from the request, so a PAS submission can run end to
end without a real payer. It decides nothing clinically: it only checks the request's shape.

    mendari sandbox payer --port 8200
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse

from mendari.standards.pas import PAS, X12

RESPONSE_PROFILE = f"{PAS}/StructureDefinition/profile-pas-response-bundle"
CLAIM_RESPONSE_PROFILE = f"{PAS}/StructureDefinition/profile-claimresponse"


def create_payer_app() -> FastAPI:
    app = FastAPI(title="Mendari sandbox payer (synthetic)", docs_url=None, redoc_url=None)

    @app.post("/Claim/$submit")
    def submit(bundle: Annotated[dict[str, Any], Body()]) -> JSONResponse:
        answer = respond(bundle)
        status = 200 if answer["resourceType"] == "Bundle" else 400
        return JSONResponse(answer, status_code=status, media_type="application/fhir+json")

    return app


def respond(bundle: Mapping[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """A PAS response Bundle certifying the request, or an OperationOutcome if it isn't one."""
    entries = [e for e in bundle.get("entry") or [] if isinstance(e, Mapping)]
    claim = entries[0].get("resource") if entries else None
    if (
        bundle.get("resourceType") != "Bundle"
        or not isinstance(claim, Mapping)
        or claim.get("resourceType") != "Claim"
        or claim.get("use") != "preauthorization"
    ):
        return {
            "resourceType": "OperationOutcome",
            "issue": [
                {
                    "severity": "error",
                    "code": "invalid",
                    "diagnostics": "a PAS request Bundle starts with a preauthorization Claim",
                }
            ],
        }
    now = now or datetime.now(UTC)
    by_url = {str(e.get("fullUrl")): e.get("resource") for e in entries}
    number = "SANDBOX-" + hashlib.sha256(str(claim.get("identifier")).encode()).hexdigest()[:10]
    review = {
        "url": f"{PAS}/StructureDefinition/extension-reviewAction",
        "extension": [
            {"url": "number", "valueString": number},
            {
                "url": f"{PAS}/StructureDefinition/extension-reviewActionCode",
                "valueCodeableConcept": {"coding": [{"system": f"{X12}/306", "code": "A1"}]},
            },
        ],
    }
    response = {
        "resourceType": "ClaimResponse",
        "meta": {"profile": [CLAIM_RESPONSE_PROFILE]},
        "identifier": list(claim.get("identifier") or []),
        "status": "active",
        "type": claim["type"],
        "use": "preauthorization",
        "patient": claim["patient"],
        "created": now.isoformat(timespec="seconds"),
        "insurer": claim["insurer"],
        "requestor": claim["provider"],
        "request": {"reference": str(entries[0].get("fullUrl"))},
        "outcome": "complete",
        "disposition": "Certified in total (synthetic sandbox payer)",
        "preAuthRef": number,
        "item": [
            {
                "extension": [
                    {
                        "url": f"{PAS}/StructureDefinition/extension-itemPreAuthIssueDate",
                        "valueDate": now.date().isoformat(),
                    }
                ],
                "itemSequence": item.get("sequence", n),
                "adjudication": [
                    {
                        "extension": [review],
                        "category": {
                            "coding": [
                                {
                                    "system": "http://terminology.hl7.org/CodeSystem/adjudication",
                                    "code": "submitted",
                                }
                            ]
                        },
                    }
                ],
            }
            for n, item in enumerate(claim.get("item") or [], start=1)
        ],
    }
    kept = [
        str(ref)
        for ref in (
            claim["patient"]["reference"],
            claim["insurer"]["reference"],
            claim["provider"]["reference"],
        )
        if ref in by_url
    ]
    return {
        "resourceType": "Bundle",
        "meta": {"profile": [RESPONSE_PROFILE]},
        "identifier": bundle.get("identifier"),
        "type": "collection",
        "timestamp": now.isoformat(timespec="seconds"),
        "entry": [
            {"fullUrl": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, number)}", "resource": response}
        ]
        + [{"fullUrl": ref, "resource": by_url[ref]} for ref in kept],
    }
