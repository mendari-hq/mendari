"""Write samples of the Da Vinci resources OpenHealth produces, for the HL7 validator (CI job
`fhir-conformance`), each checked against its implementation guide.

- `crd-*.json`: the orders a Prior Authorization Preflight `order-sign` response updates with
  CRD `coverage-information` (a complete request, one with documentation missing, and a service
  the payer doesn't require authorization for).

    uv run python scripts/davinci_sample.py var/fhir-samples/davinci
"""

from __future__ import annotations

import base64
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from openhealth import Decision
from openhealth.standards import cds_hooks

TODAY = date.today()
CPT = "http://www.ama-assn.org/go/cpt"


def order(id_: str, code: str, display: str) -> dict[str, Any]:
    return {
        "resourceType": "ServiceRequest",
        "id": id_,
        "status": "draft",
        "intent": "order",
        "code": {"coding": [{"system": CPT, "code": code, "display": display}], "text": display},
        "subject": {"reference": "Patient/pa-p1"},
        "authoredOn": TODAY.isoformat(),
        "requester": {"reference": "Practitioner/pa-pr1"},
    }


def prefetch(payer: str, note: str) -> dict[str, Any]:
    def bundle(*resources: dict[str, Any]) -> dict[str, Any]:
        return {
            "resourceType": "Bundle",
            "type": "searchset",
            "entry": [{"resource": r} for r in resources],
        }

    def coded(kind: str, id_: str, system: str, code: str, **extra: Any) -> dict[str, Any]:
        coding = {"system": system, "code": code}
        return {"resourceType": kind, "id": id_, "code": {"coding": [coding]}, **extra}

    ago = (TODAY - timedelta(days=40)).isoformat()
    icd = "http://hl7.org/fhir/sid/icd-10-cm"
    return {
        "patient": {"resourceType": "Patient", "id": "pa-p1", "birthDate": "1975-01-01"},
        "coverage": bundle(
            {
                "resourceType": "Coverage",
                "id": "pa-c1",
                "status": "active",
                "beneficiary": {"reference": "Patient/pa-p1"},
                "payor": [{"identifier": {"value": payer}, "display": payer.title()}],
            }
        ),
        "conditions": bundle(
            coded("Condition", "pa-d1", icd, "M54.16", subject={"reference": "Patient/pa-p1"})
        ),
        "procedures": bundle(
            coded(
                "Procedure",
                "pa-x1",
                CPT,
                "72100",
                status="completed",
                performedDateTime=ago,
                subject={"reference": "Patient/pa-p1"},
            )
        ),
        "notes": bundle(
            {
                "resourceType": "DocumentReference",
                "id": "pa-n1",
                "status": "current",
                "content": [
                    {
                        "attachment": {
                            "contentType": "text/plain",
                            "data": base64.b64encode(note.encode()).decode(),
                        }
                    }
                ],
            }
        ),
    }


def main(out: Path) -> None:
    decision = Decision.from_hub("openhealth/prior-auth-preflight")
    cases = {
        "crd-submit-ready": (
            "acme",
            "Completed 8 weeks of physical therapy.",
            order("mri", "72148", "MRI lumbar spine"),
        ),
        "crd-needs-attention": (
            "acme",
            "Back pain for two weeks.",
            order("mri2", "72148", "MRI lumbar spine"),
        ),
        "crd-not-required": (
            "beacon",
            "Loud snoring and daytime sleepiness.",
            order("psg", "95810", "Polysomnography"),
        ),
    }
    out.mkdir(parents=True, exist_ok=True)
    for name, (payer, note, draft) in cases.items():
        request = {
            "hook": "order-sign",
            "hookInstance": f"sample-{name}",
            "context": {
                "userId": "Practitioner/pa-pr1",
                "patientId": "pa-p1",
                "draftOrders": {"resourceType": "Bundle", "entry": [{"resource": draft}]},
            },
            "prefetch": prefetch(payer, note),
        }
        actions = cds_hooks.invoke(decision, request).get("systemActions", [])
        if len(actions) != 1:
            sys.exit(f"{name}: expected one system action, got {len(actions)}")
        (out / f"{name}.json").write_text(
            json.dumps(actions[0]["resource"], indent=2) + "\n", encoding="utf-8"
        )
    print(f"{len(cases)} CRD orders written to {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "var/fhir-samples/davinci"))
