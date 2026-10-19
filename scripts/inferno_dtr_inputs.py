# SPDX-License-Identifier: Apache-2.0
"""Build the Inferno preset for the Da Vinci DTR payer suite (`dtr_payer_server_v220`), over HTTP
against a running engine, on synthetic data only (sandbox/inferno/README.md).

1. Sign two orders through CDS Hooks `order-sign`, built as `scripts/davinci_sample.py` builds
   them: a lumbar MRI whose documentation is missing (its decision is `needs_attention`, so CRD's
   coverage information links a questionnaire) and one that is complete. The orders come back
   updated, with that coverage information, in `systemActions`.
2. Write the four `$questionnaire-package` requests Inferno sends, each with the patient's
   Coverage (as a DTR client sends it: CRD's `profile-coverage`, with the member id and the
   relationship) and one kind of input alone (the suite checks that each kind is given apart):
   the updated order, its CRD assertion id as `context`, the questionnaire canonical CRD linked,
   and the blank canonical case (the knee arthroscopy questionnaire by its canonical alone,
   without a version, as `davinci_sample.py`'s `dtr-package-blank`).
3. Write the source-data-error request (a `context` that names no decision), a static token
   (the engine runs open, and never reads it), the canonical `$log-questionnaire-errors` names,
   and the invalid QuestionnaireResponse the suite sends to `$next-question` (the blank
   questionnaire, contained, marked completed with its required questions unanswered). Mendari
   serves neither operation: those tests are known gaps (the expected results say why).

Each request is sent once here first, and must get the answer the suite expects (a package, or
a `422` OperationOutcome), so a preset that would test nothing is never written.

    uv run python scripts/inferno_dtr_inputs.py --engine http://127.0.0.1:8090 \\
      --out var/inferno/dtr_payer_server_v220_preset.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))  # davinci_sample, beside this one

from davinci_sample import order, prefetch

from mendari.core.registry import resolve_pack
from mendari.standards import davinci

SUITE = "dtr_payer_server_v220"
PACK = "mendari/prior-auth-preflight"
SERVICE = PACK.replace("/", ".")
PACKAGE = "/fhir/Questionnaire/$questionnaire-package"
# What Inferno sends as its bearer token. The engine runs with sign-in off, where it never reads
# the Authorization header; Backend Services (the suite's group 2) stays off until LP-23.
STATIC_TOKEN = "inferno-static-token-open-mode"  # not a secret: nothing checks it
# A decision id that no engine holds: the source data the server cannot resolve (spec-130).
UNKNOWN_CONTEXT = "synthetic-unknown-decision"
# The member id davinci_sample.py's PAS sample gives the same synthetic Coverage.
MEMBER_SYSTEM = "https://acme.example/member"
MEMBER_ID = "ACME-778899"
# The notes of davinci_sample.py's two lumbar MRI orders: one leaves the conservative therapy
# undocumented (`needs_attention`), the other documents it (`submit_ready`).
MISSING_NOTE = "Back pain for two weeks."
COMPLETE_NOTE = "Completed 8 weeks of physical therapy."
# The four kinds of `$questionnaire-package` request, in the order Inferno sends them. Inferno's
# questionnaire-not-found test (3.6.01) adds an unknown canonical to the first.
KINDS = ("order", "context", "canonical", "blank canonical")


class InputsError(RuntimeError):
    """The engine did not answer as the suite needs: no preset is written."""


def sign(client: httpx.Client, name: str, payer: str, note: str, draft: dict[str, Any]) -> Any:
    """The draft order, updated with CRD's coverage information, from an `order-sign` call."""
    request = {
        "hook": "order-sign",
        "hookInstance": f"inferno-{name}",
        "context": {
            "userId": "Practitioner/pa-pr1",
            "patientId": "pa-p1",
            "draftOrders": {"resourceType": "Bundle", "entry": [{"resource": draft}]},
        },
        "prefetch": prefetch(payer, note),
    }
    answer = client.post(f"/cds-services/{SERVICE}", json=request)
    if answer.status_code != 200:
        raise InputsError(f"order-sign {name}: HTTP {answer.status_code}")
    actions = answer.json().get("systemActions") or []
    if len(actions) != 1 or actions[0].get("type") != "update":
        raise InputsError(f"order-sign {name}: expected one update, got {len(actions)} actions")
    return actions[0]["resource"]


def coverage_information(updated: dict[str, Any]) -> dict[str, Any]:
    """The parts of an updated order's CRD coverage information, by name."""
    for extension in updated.get("extension") or []:
        if extension.get("url") == davinci.COVERAGE_INFORMATION:
            return {
                part["url"]: next(v for k, v in part.items() if k.startswith("value"))
                for part in extension.get("extension") or []
            }
    raise InputsError(f"order {updated.get('id')}: no coverage information")


def dtr_coverage(coverage: dict[str, Any]) -> dict[str, Any]:
    """The Coverage the order was signed with, as a DTR client sends it: CRD's `profile-coverage`
    also asks for the member id and the patient's relationship to the subscriber (HL7's codes)."""
    member = {"system": "http://terminology.hl7.org/CodeSystem/v2-0203", "code": "MB"}
    relationship = "http://terminology.hl7.org/CodeSystem/subscriber-relationship"
    return {
        **coverage,
        "identifier": [{"type": {"coding": [member]}, "system": MEMBER_SYSTEM, "value": MEMBER_ID}],
        "relationship": {"coding": [{"system": relationship, "code": "self"}]},
    }


def parameters(coverage: dict[str, Any], *given: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceType": "Parameters",
        "parameter": [{"name": "coverage", "resource": coverage}, *given],
    }


def build(client: httpx.Client, fhir_base: str) -> dict[str, Any]:
    """The preset, for an engine `client` reaches; `fhir_base` is its FHIR base as Inferno sees
    it (inside the compose network)."""
    missing = sign(client, "needs-attention", "acme", MISSING_NOTE, order("mri2", "FX101"))
    ready = sign(client, "submit-ready", "acme", COMPLETE_NOTE, order("mri", "FX101"))
    linked = coverage_information(missing)
    if "questionnaire" not in linked or "questionnaire" in coverage_information(ready):
        raise InputsError("CRD links a questionnaire only when documentation is missing")
    pack = resolve_pack(PACK)
    blank = davinci.questionnaire_url(pack, {"service": "knee_arthroscopy", "payer": "acme"})
    coverage = dtr_coverage(prefetch("acme", "")["coverage"]["entry"][0]["resource"])
    requests = {
        "order": parameters(coverage, {"name": "order", "resource": missing}),
        "context": parameters(
            coverage, {"name": "context", "valueString": linked["coverage-assertion-id"]}
        ),
        "canonical": parameters(
            coverage, {"name": "questionnaire", "valueCanonical": linked["questionnaire"]}
        ),
        "blank canonical": parameters(coverage, {"name": "questionnaire", "valueCanonical": blank}),
    }
    forms = {}
    for kind, body in requests.items():
        answer = client.post(PACKAGE, json=body)
        given = answer.json().get("parameter") or []
        names = [p.get("name") for p in given]
        if answer.status_code != 200 or names != ["packagebundle"]:
            raise InputsError(f"{kind}: HTTP {answer.status_code}, {names}, not one package")
        forms[kind] = given[0]["resource"]["entry"][0]["resource"]
    unresolvable = parameters(coverage, {"name": "context", "valueString": UNKNOWN_CONTEXT})
    refused = client.post(PACKAGE, json=unresolvable)
    if refused.status_code != 422 or refused.json().get("resourceType") != "OperationOutcome":
        raise InputsError(f"source data error: HTTP {refused.status_code}, not a 422 outcome")
    return {
        "id": f"mendari-{SUITE}",
        "title": "Mendari, synthetic data, open mode",
        "test_suite_id": SUITE,
        "inputs": [
            {"name": "url", "type": "text", "value": fhir_base},
            {
                "name": "backend_services_smart_auth_info",
                "type": "auth_info",
                "value": {"auth_type": "backend_services", "access_token": STATIC_TOKEN},
            },
            {
                "name": "questionnaire_package_request_parameters",
                "type": "textarea",
                "value": json.dumps([requests[kind] for kind in KINDS], indent=2),
            },
            {
                "name": "source_data_error_request",
                "type": "textarea",
                "value": json.dumps(unresolvable, indent=2),
            },
            {"name": "questionnaire_canonical", "type": "text", "value": linked["questionnaire"]},
            {
                "name": "invalid_questionnaire_response",
                "type": "textarea",
                "value": json.dumps(unanswered(forms["blank canonical"]), indent=2),
            },
        ],
    }


def unanswered(form: dict[str, Any]) -> dict[str, Any]:
    """A QuestionnaireResponse that is invalid by its contained Questionnaire's own rules: marked
    completed, with every required question unanswered."""
    if not any(item.get("required") for item in form.get("item") or []):
        raise InputsError(f"{form.get('url')}: no required question to leave unanswered")
    return {
        "resourceType": "QuestionnaireResponse",
        "id": "synthetic-invalid-response",
        "contained": [{**form, "id": "form"}],
        "questionnaire": "#form",
        "status": "completed",
        "subject": {"reference": "Patient/pa-p1"},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--engine", required=True, help="where this script reaches the engine")
    parser.add_argument(
        "--fhir-base",
        default="http://engine:8000/fhir",
        help="the engine's FHIR base as Inferno reaches it (default: %(default)s)",
    )
    parser.add_argument("--out", type=Path, required=True, help="the preset file to write")
    args = parser.parse_args(argv)
    try:
        with httpx.Client(base_url=args.engine, timeout=60) as client:
            preset = build(client, args.fhir_base)
    except (InputsError, httpx.HTTPError, ValueError) as error:  # ValueError: not JSON
        print(f"inferno inputs: {error}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(preset, indent=2) + "\n", encoding="utf-8")
    print(f"{SUITE} preset written to {args.out}: {len(KINDS)} package requests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
