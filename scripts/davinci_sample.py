# SPDX-License-Identifier: Apache-2.0
"""Write samples of the Da Vinci resources Mendari produces, for the HL7 validator (CI job
`fhir-conformance`), each checked against its implementation guide.

- `crd-*.json`: the orders a Prior Authorization Preflight `order-sign` response updates with
  CRD `coverage-information` (a complete request, one with documentation missing, and a service
  the payer doesn't require authorization for).
- `dtr-*.json`: `Questionnaire/$questionnaire-package` answers: for the order with documentation
  missing (found by its CRD assertion id, pre-answered from the decision), for a
  questionnaire asked for by its canonical alone, and for that order with a questionnaire that
  is not found here (`dtr-package-not-found.json`: its package and a warning in `outcome`, DTR
  oper-9).
- `pas-request.json`, `pas-response.json`: the PAS request Bundle for the complete request, with
  every X12 code a site may set, and the sandbox payer's answer to it with a review action code;
  `pas-request-required.json`, `pas-response-no-review-code.json`: the same request with only the
  X12 codes PAS requires, and an answer with no review action code.

    uv run python scripts/davinci_sample.py var/fhir-samples/davinci
"""

from __future__ import annotations

import base64
import functools
import json
import sys
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from mendari import AuditStore, Decision
from mendari.core.pack import Pack
from mendari.sandbox import payer as sandbox_payer
from mendari.sandbox.synthetic_prior_auth import procedure_codings
from mendari.standards import cds_hooks, davinci, pas

TODAY = date.today()


def order(id_: str, code: str) -> dict[str, Any]:
    codings = procedure_codings(code)  # the pack's fictional code, then a payer's ICD-10-PCS
    display = codings[0]["display"]
    return {
        "resourceType": "ServiceRequest",
        "id": id_,
        "status": "draft",
        "intent": "order",
        "code": {"coding": codings, "text": display},
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

    def procedure(id_: str, code: str, **extra: Any) -> dict[str, Any]:
        return {
            "resourceType": "Procedure",
            "id": id_,
            "code": {"coding": procedure_codings(code)},
            **extra,
        }

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
            procedure(
                "pa-x1",
                "FX111",
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
    with tempfile.TemporaryDirectory() as scratch:
        store = AuditStore(Path(scratch) / "audit.db")
        write(out, Decision.from_hub("mendari/prior-auth-preflight", store=store), store)
        store.close()


def write(out: Path, decision: Decision, store: AuditStore) -> None:
    cases = {
        "crd-submit-ready": (
            "acme",
            "Completed 8 weeks of physical therapy.",
            order("mri", "FX101"),
        ),
        "crd-needs-attention": (
            "acme",
            "Back pain for two weeks.",
            order("mri2", "FX101"),
        ),
        "crd-not-required": (
            "beacon",
            "Loud snoring and daytime sleepiness.",
            order("psg", "FX301"),
        ),
    }
    out.mkdir(parents=True, exist_ok=True)
    updated: dict[str, dict[str, Any]] = {}
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
        updated[name] = actions[0]["resource"]
        (out / f"{name}.json").write_text(
            json.dumps(updated[name], indent=2) + "\n", encoding="utf-8"
        )
    print(f"{len(cases)} CRD orders written to {out}")

    pack = decision.pack

    def lookup(decision_id: str) -> tuple[Pack, dict[str, str]] | None:
        record = store.get(decision_id)
        if record is None:
            return None
        return pack, {k: str(a["label"]) for k, a in record["record"]["answers"].items()}

    coverage = prefetch("acme", "")["coverage"]["entry"][0]["resource"]
    authored = datetime.now(UTC).isoformat(timespec="seconds")
    ordered = {"name": "order", "resource": updated["crd-needs-attention"]}
    elsewhere = "https://payer.example.org/Questionnaire/not-here"  # synthetic, found nowhere
    asks = {
        "dtr-package": ([ordered], ["packagebundle"]),
        "dtr-package-blank": (
            [
                {
                    "name": "questionnaire",
                    "valueCanonical": davinci.questionnaire_url(
                        pack, {"service": "knee_arthroscopy", "payer": "acme"}
                    ),
                }
            ],
            ["packagebundle"],
        ),
        "dtr-package-not-found": (
            [ordered, {"name": "questionnaire", "valueCanonical": elsewhere}],
            ["packagebundle", "outcome"],
        ),
    }
    for name, (extra, expected) in asks.items():
        request = {
            "resourceType": "Parameters",
            "parameter": [{"name": "coverage", "resource": coverage}, *extra],
        }
        answer = davinci.questionnaire_package(request, lookup, {pack.id: pack}, authored)
        if [p["name"] for p in answer["parameter"]] != expected:
            sys.exit(f"{name}: expected {', '.join(expected)}")
        (out / f"{name}.json").write_text(json.dumps(answer, indent=2) + "\n", encoding="utf-8")
    print(f"{len(asks)} DTR packages written to {out}")

    # PAS: the complete request, with the resources a practice's EHR holds (synthetic). Mendari
    # ships no X12 code value: these are a site's (its settings, `settings.pas_codes`), here only
    # to make the sample. `pas-request.json` carries every one a site may set, and
    # `pas-request-required.json` only those PAS requires; the payer answers the first with a
    # review action code and the second without one (PAS makes both optional).
    ready = updated["crd-submit-ready"]
    record = store.get(davinci.assertion_id(ready) or "")
    if record is None:
        sys.exit("pas: the complete request's decision is not in the store")
    npi = "http://hl7.org/fhir/sid/us-npi"
    member = {"system": "http://terminology.hl7.org/CodeSystem/v2-0203", "code": "MB"}
    required = {"request_type": "HS", "certification_type": "I", "service_type": "62"}
    optional = {"self_relationship": "18", "payer_entity": "PR", "requester_entity": "1P"}
    build = functools.partial(
        pas.request_bundle,
        decision={"id": record["id"], "recommendation": record["recommendation"]},
        order=ready,
        patient={
            "resourceType": "Patient",
            "identifier": [{"system": "https://mendari.co/fhir/sandbox/mrn", "value": "PA-P1"}],
            "name": [{"family": "Synthetic", "given": ["Case"]}],
            "gender": "female",
            "birthDate": "1975-01-01",
        },
        coverage={
            **coverage,
            "identifier": [
                {
                    "type": {"coding": [member]},
                    "system": "https://acme.example/member",
                    "value": "ACME-778899",
                }
            ],
        },
        insurer={
            "resourceType": "Organization",
            "name": "Acme Health Plan (synthetic)",
            "identifier": [{"system": npi, "value": "1234567893"}],
        },
        requester={
            "resourceType": "Organization",
            "name": "Riverside Clinic (synthetic)",
            "identifier": [{"system": npi, "value": "1245319599"}],
        },
        place_of_service="22",  # on-campus outpatient hospital
        diagnoses=["M54.16"],
    )
    request = build(site_codes=required | optional)
    answer = sandbox_payer.respond(request, review_action="A1")
    bare = build(site_codes=required)
    samples = {
        "pas-request": request,
        "pas-response": answer,
        "pas-request-required": bare,
        "pas-response-no-review-code": sandbox_payer.respond(bare),
    }
    for name, resource in samples.items():
        (out / f"{name}.json").write_text(json.dumps(resource, indent=2) + "\n", encoding="utf-8")
    print(f"{len(samples)} PAS requests and responses written to {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "var/fhir-samples/davinci"))
