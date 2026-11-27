# SPDX-License-Identifier: Apache-2.0
"""The inputs of Inferno's scripted suites against Mendari (LP-23l, sandbox/inferno/README.md,
"The scripted suites"), on synthetic data only: each a preset file, for
`scripts/inferno_session.py` to start the runs with.

- `smart` (`smart_client_stu2_2`, public client, Inferno as the EHR): the open engine's client
  id, launch URL and redirect URI, and a synthetic patient as the launch's context, which
  Inferno's FHIR server serves to the launch's token. The patient is made as the sandbox makes
  its own (`mendari.sandbox.fhir_records`): obviously synthetic, tagged as such.
- `bulk` (`bulk_data_v200_client`, Inferno as the Bulk Data server): a patient-level export and
  the client id. The client's public keys (`smart_jwk_set`) are made per run by the driver,
  which keeps the private half in memory.

    uv run python scripts/inferno_scripted_inputs.py smart \\
      --out var/inferno/smart_client_stu2_2_inputs.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from mendari.sandbox import fhir_records

SMART_SUITE = "smart_client_stu2_2"
BULK_SUITE = "bulk_data_v200_client"
# The engine as Inferno reaches it (sandbox/inferno/scripted/docker-compose.yml): its own
# MENDARI_PUBLIC_URL.
OPEN_ENGINE = "http://engine:8000"
# The fullUrl base of what Inferno's FHIR server is given to serve: a reserved name, which points
# nowhere real.
EHR_BASE = "https://ehr.example.org/fhir"
SMART_CLIENT_ID = "mendari-sandbox"  # the open engine's MENDARI_SMART_CLIENT_ID
# The launch's patient: a synthetic one of the EHR Inferno plays, about 45 on the run's day.
SMART_PATIENT = "ehr-smart-1"
SMART_PATIENT_AGE = 45
BULK_CLIENT_ID = "mendari-bulk-sandbox"
BULK_UNUSED_GROUP = "1a"  # the suite's "US Core Data" Group, unused by a patient-level export


def preset(suite: str, inputs: dict[str, Any]) -> dict[str, Any]:
    """A preset as Inferno reads one: every value a string, a JSON value written out."""
    return {
        "id": f"mendari-{suite}",
        "title": "Mendari, synthetic data",
        "test_suite_id": suite,
        "inputs": [
            {"name": name, "value": value if isinstance(value, str) else json.dumps(value)}
            for name, value in inputs.items()
        ],
    }


# -- smart_client_stu2_2 ------------------------------------------------------------------------


def smart(today: date | None = None) -> dict[str, Any]:
    """A public client's launch: the open engine's registration, and a synthetic patient as the
    launch's patient, which Inferno's FHIR server serves to the launch's token."""
    year = (today or datetime.now(UTC).date()).year - SMART_PATIENT_AGE
    patient = fhir_records.patient(SMART_PATIENT, gender="unknown", birth_date=f"{year}-06-15")
    patient = {"resourceType": "Patient", "id": SMART_PATIENT, **patient}
    resources = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{"fullUrl": f"{EHR_BASE}/Patient/{SMART_PATIENT}", "resource": patient}],
    }
    return preset(
        SMART_SUITE,
        {
            "client_id": SMART_CLIENT_ID,
            "smart_launch_urls": f"{OPEN_ENGINE}/smart/launch",
            "smart_redirect_uris": f"{OPEN_ENGINE}/smart/callback",
            "launch_context": {"patient": SMART_PATIENT},
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
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("suite", choices=("smart", "bulk"))
    parser.add_argument("--out", type=Path, required=True, help="the preset file to write")
    args = parser.parse_args(argv)
    today = datetime.now(UTC).date()
    made = smart(today) if args.suite == "smart" else bulk()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(made, indent=2) + "\n", encoding="utf-8")
    print(f"{made['test_suite_id']} inputs written to {args.out}: {len(made['inputs'])} inputs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
