# SPDX-License-Identifier: Apache-2.0
"""The inputs of Inferno's scripted suites against Mendari (LP-23l, sandbox/inferno/README.md,
"The scripted suites"), on synthetic data only: each a preset file, as the DTR suite's
(`scripts/inferno_dtr_inputs.py`), for `scripts/inferno_session.py` to start the runs with.

- `crd` (`crd_server_v221`, Inferno as the EHR's CDS client against `engine-signin`): discovery
  with authentication; the prior-authorization pack's `order-sign` for the vendor-shaped EHR
  profile's prior-authorization patients (`sandbox/ehr_profile.py`), each as an EHR sends it
  (the context and the FHIR server, no prefetch, so the engine reads the record from the Mock
  EHR: `mock_ehr_bundle`, those patients and what they refer to); and the specific coverage
  answers of group 4: one patient's order with Inferno's FHIR server failing (Technical Issues),
  and patients without a member id, without a Coverage, and with a cancelled Coverage, made
  here from the first patient. The other packs' services (trial, referral and inbox) are on the
  ignore list: their hooks are not CRD's order hooks.
- `smart` (`smart_client_stu2_2`, public client, Inferno as the EHR): the engine's client id,
  launch URL and redirect URI, and a synthetic patient as the launch's context, which Inferno's
  FHIR server serves to the launch's token.
- `bulk` (`bulk_data_v200_client`, Inferno as the Bulk Data server): a patient-level export and
  the client id. The client's public keys (`smart_jwk_set`) are made per run by the driver,
  which keeps the private half in memory.

    uv run python scripts/inferno_scripted_inputs.py crd \\
      --out var/inferno/crd_server_v221_inputs.json
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from mendari.core.registry import resolve_pack
from mendari.sandbox import ehr_profile
from mendari.sandbox.cds_client import hook_request
from mendari.standards.cds_hooks import service_id

CRD_SUITE = "crd_server_v221"
SMART_SUITE = "smart_client_stu2_2"
BULK_SUITE = "bulk_data_v200_client"
# The engines as Inferno reaches them (sandbox/inferno/scripted/docker-compose.yml): each is its
# own MENDARI_PUBLIC_URL, so a CRD token's `aud` is this URL plus the path called.
SIGNIN_ENGINE = "http://engine-signin:8000"
OPEN_ENGINE = "http://engine:8000"
# Inferno replaces each request's `fhirServer` with its own FHIR server's URL and the access
# token with its own; these stand in for both until it does.
CRD_FHIR = "http://nginx/custom/crd_server_v221/fhir"
PLACEHOLDER_TOKEN = "replaced-by-inferno"  # not a secret: Inferno writes its own over it
SERVICE = service_id(resolve_pack(ehr_profile.PRIOR_AUTH))
# The packs whose services discovery lists beside the prior-authorization pack's: not CRD
# services (patient-view, and two order-sign services for referrals and messages), so the CRD
# suite leaves them out. Inferno splits this list on ", ".
IGNORED_PACKS = (
    "mendari/clinical-trial-prescreener",
    "mendari/referral-router",
    "mendari/patient-inbox-router",
)
# The SMART scopes an EHR grants a CDS service for its reads (CDS Hooks' fhirAuthorization),
# one for each resource type the prior-authorization pack's prefetch reads, and its links.
CRD_SCOPES = " ".join(
    f"user/{kind}.rs"
    for kind in (
        "Patient",
        "Coverage",
        "Condition",
        "Procedure",
        "DocumentReference",
        "Binary",
        "ServiceRequest",
        "Organization",
        "Practitioner",
    )
)
# Group 4's patients, each made from the profile's first prior-authorization patient with its
# Coverage changed: the coverage answer CRD expects for each (`no-member-found`,
# `coverage-not-found`, `no-active-coverage`).
COVERAGE_CASES = {
    "no_member_found": "nomember",
    "coverage_not_found": "nocoverage",
    "no_active_coverage": "inactive",
}
SMART_CLIENT_ID = "mendari-sandbox"  # the open engine's MENDARI_SMART_CLIENT_ID
SMART_CLIENT_TYPE = "SMART,authorization_code,public"
BULK_CLIENT_ID = "mendari-bulk-sandbox"
BULK_UNUSED_GROUP = "1a"  # the suite's "US Core Data" Group, unused by a patient-level export


class InputsError(RuntimeError):
    """The profile does not hold what the suite needs: no preset is written."""


def preset(suite: str, inputs: dict[str, Any]) -> dict[str, Any]:
    """A preset as Inferno reads one (and as the DTR inputs script writes it): every value a
    string, a JSON value written out."""
    return {
        "id": f"mendari-{suite}",
        "title": "Mendari, synthetic data",
        "test_suite_id": suite,
        "inputs": [
            {"name": name, "value": value if isinstance(value, str) else json.dumps(value)}
            for name, value in inputs.items()
        ],
    }


# -- crd_server_v221 ----------------------------------------------------------------------------


def drafted(patient: ehr_profile.ProfilePatient) -> ehr_profile.ProfilePatient:
    """The patient as the EHR holds it while the orders are signed: each order a draft, naming
    its ordering clinician (CRD's ServiceRequest requires one; the profile's one Practitioner,
    who is also the hook's user). The Mock EHR serves the same orders the hook sends, as an
    EHR's FHIR server does, so a coverage answer can be checked against either."""
    requester = {"reference": f"Practitioner/{ehr_profile.PRESCRIBER_ID}"}
    resources = tuple(
        {**r, "status": "draft", "requester": r.get("requester", requester)}
        if r["resourceType"] == "ServiceRequest"
        else r
        for r in patient.resources
    )
    return ehr_profile.ProfilePatient(patient.pack_id, patient.id, patient.case, resources)


def order_sign(
    patient: ehr_profile.ProfilePatient, *, number: int, prefetched: bool = False
) -> dict[str, Any]:
    """The `order-sign` request an EHR sends for the patient's draft orders (`cds_client`), with
    the FHIR authorization CDS Hooks describes, which Inferno completes with its own token, and a
    fixed hookInstance (Inferno gives each call a new one). `prefetched`: with the Patient and
    the active Coverage under the pack's prefetch keys, as the kit's own Technical Issues
    example sends them, so the reads that fail are the rest of the record."""
    body = hook_request("order-sign", drafted(patient), CRD_FHIR)
    if prefetched:
        coverages = [r for r in patient.resources if r["resourceType"] == "Coverage"]
        body["prefetch"] = {
            "patient": patient.patient,
            "coverage": {
                "resourceType": "Bundle",
                "type": "searchset",
                "entry": [
                    {
                        "fullUrl": f"{CRD_FHIR}/Coverage/{c['id']}",
                        "resource": c,
                        "search": {"mode": "match"},
                    }
                    for c in coverages
                ],
            },
        }
    body["hookInstance"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"mendari-crd/{patient.id}/{number}"))
    body["fhirAuthorization"] = {
        "access_token": PLACEHOLDER_TOKEN,
        "token_type": "Bearer",
        "expires_in": 300,
        "scope": CRD_SCOPES,
        "subject": SERVICE,
        "patient": patient.id,
    }
    for entry in body["context"]["draftOrders"]["entry"]:
        order = entry["resource"]
        entry["fullUrl"] = f"{CRD_FHIR}/{order['resourceType']}/{order['id']}"
    return body


def coverage_variant(patient: ehr_profile.ProfilePatient, case: str) -> ehr_profile.ProfilePatient:
    """The patient again, under ids of its own (`<id>-<suffix>`), with its Coverage changed
    for one of group 4's coverage answers: no member id, no Coverage, or a cancelled one."""
    suffix = COVERAGE_CASES[case]
    old = patient.id
    renamed = json.loads(json.dumps(list(patient.resources)).replace(old, f"{old}-{suffix}"))
    resources = []
    for resource in renamed:
        if resource["resourceType"] == "Coverage":
            if case == "coverage_not_found":
                continue
            if case == "no_member_found":
                resource.pop("subscriberId", None)
            if case == "no_active_coverage":
                resource["status"] = "cancelled"
        resources.append(resource)
    return ehr_profile.ProfilePatient(
        pack_id=patient.pack_id, id=f"{old}-{suffix}", case=patient.case, resources=tuple(resources)
    )


def mock_ehr_bundle(patients: Sequence[ehr_profile.ProfilePatient]) -> dict[str, Any]:
    """What Inferno's Mock EHR serves while a hook is called: the patients' resources, their
    orders in draft (`drafted`), and what they refer to (the Practitioner and the payers), each
    under a RESTful fullUrl."""
    resources = [*ehr_profile.shared(), *(r for p in patients for r in drafted(p).resources)]
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [
            {"fullUrl": f"{ehr_profile.BASE}/{r['resourceType']}/{r['id']}", "resource": r}
            for r in resources
        ],
    }


def crd(today: date | None = None) -> dict[str, Any]:
    patients = ehr_profile.patients(today, ehr_profile.PRIOR_AUTH)
    if not patients or not all(p.orders() for p in patients):
        raise InputsError("the EHR profile has no prior-authorization patient with an order")
    first = patients[0]
    variants = {case: coverage_variant(first, case) for case in COVERAGE_CASES}
    signed = [order_sign(p, number=n) for n, p in enumerate(patients, start=1)]
    inputs: dict[str, Any] = {
        "base_url": SIGNIN_ENGINE,
        "authentication_required": "yes",
        "encryption_method": "ES384",
        "crd_discovery_service_ignore_list": ", ".join(
            service_id(resolve_pack(pack)) for pack in IGNORED_PACKS
        ),
        "any_hook_service_ids": SERVICE,
        "any_hook_request_body": signed[0],
        "order_sign_service_ids": SERVICE,
        "order_sign_request_bodies": signed,
        "technical_issues_service_ids": SERVICE,
        "technical_issues_request_body": order_sign(first, number=0, prefetched=True),
        "mock_ehr_bundle": mock_ehr_bundle([*patients, *variants.values()]),
        "manual_continuation": "no",
    }
    for case, patient in variants.items():
        inputs[f"{case}_service_ids"] = SERVICE
        inputs[f"{case}_request_body"] = order_sign(patient, number=0)
    return preset(CRD_SUITE, inputs)


# -- smart_client_stu2_2 ------------------------------------------------------------------------


def smart(today: date | None = None) -> dict[str, Any]:
    """A public client's launch: the open engine's registration, and the profile's first referral
    patient as the launch's patient, which Inferno's FHIR server serves to the launch's token."""
    patient = copy.deepcopy(ehr_profile.patients(today, ehr_profile.REFERRAL_PACK)[0].patient)
    resources = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{"fullUrl": f"{ehr_profile.BASE}/Patient/{patient['id']}", "resource": patient}],
    }
    return preset(
        SMART_SUITE,
        {
            "client_id": SMART_CLIENT_ID,
            "smart_launch_urls": f"{OPEN_ENGINE}/smart/launch",
            "smart_redirect_uris": f"{OPEN_ENGINE}/smart/callback",
            "launch_context": {"patient": patient["id"]},
            "fhir_read_resources_bundle": resources,
        },
    )


# -- bulk_data_v200_client ----------------------------------------------------------------------


def bulk() -> dict[str, Any]:
    """A patient-level export (Mendari's client asks for `Patient/$export` or a Group's, never a
    system-level one), for this client id; its keys are the driver's. The suite asks for a
    Group id whatever the export, and uses it only for a Group export: its US Core one."""
    return preset(
        BULK_SUITE,
        {"client_id": BULK_CLIENT_ID, "export_type": "Patient", "group_id": BULK_UNUSED_GROUP},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("suite", choices=("crd", "smart", "bulk"))
    parser.add_argument("--out", type=Path, required=True, help="the preset file to write")
    args = parser.parse_args(argv)
    today = datetime.now(UTC).date()
    try:
        made = {"crd": lambda: crd(today), "smart": lambda: smart(today), "bulk": bulk}[
            args.suite
        ]()
    except InputsError as error:
        print(f"inferno inputs: {error}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(made, indent=2) + "\n", encoding="utf-8")
    print(f"{made['test_suite_id']} inputs written to {args.out}: {len(made['inputs'])} inputs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
