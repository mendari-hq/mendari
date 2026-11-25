# SPDX-License-Identifier: Apache-2.0
"""The triage test fixture's CDS hook as the tests of CDS clients, open mode, cards and card
feedback call it: `mendari-message-received` on the service `mendari-test.triage`, with a
synthetic patient message.

Synthetic test data. Not clinically reviewed. Not for patient care.

- `message_hook(n, body)`: the hook's request for message `n`, its Communication and its
  sender's Patient as prefetch. By default the message asks for a refill, which the fixture's
  rules send to the pharmacy: one card (`CARD`).
- `message_ehr(n)`: the same request without prefetch, naming an EHR's FHIR server and a token
  for it (CDS Hooks' `fhirServer` and `fhirAuthorization`), and that server: a mock FHIR server
  (`tests/support/chart_ehr.py` `ChartEhr`; no network) holding the message and its sender, for
  the engine to read them from. `install(monkeypatch)` serves it to the engine's hooks, and its
  `requests` are every request it got.
"""

from __future__ import annotations

import uuid
from typing import Any

from tests.support.chart_ehr import ChartEhr

SERVICE = "mendari-test.triage"
HOOK = f"/cds-services/{SERVICE}"
FHIR_BASE = "https://ehr.example.org/fhir"
TOKEN = "cds-messages-test-token"  # made for these tests; no server outside them takes it
PATIENT_ID = "triage-patient-1"
REFILL = "Please refill my lisinopril, I am almost out of it."
CARD = "Message for the pharmacy: a refill to check (routine)"  # what a refill's card says


def patient() -> dict[str, Any]:
    """The message's sender."""
    return {"resourceType": "Patient", "id": PATIENT_ID, "birthDate": "1970-01-01"}


def communication(n: int = 1, body: str = REFILL) -> dict[str, Any]:
    """Message `n`, from the patient, saying `body`."""
    return {
        "resourceType": "Communication",
        "id": f"triage-message-{n}",
        "status": "completed",
        "subject": {"reference": f"Patient/{PATIENT_ID}"},
        "payload": [{"contentString": body}],
    }


def message_hook(n: int = 1, body: str = REFILL) -> dict[str, Any]:
    """The hook's request for message `n`, with its prefetch."""
    message = communication(n, body)
    return {
        "hook": "mendari-message-received",
        "hookInstance": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{FHIR_BASE}/hook/{message['id']}")),
        "context": {
            "userId": "Practitioner/1",
            "patientId": PATIENT_ID,
            "communicationId": message["id"],
        },
        "prefetch": {"communication": message, "patient": patient()},
    }


def message_ehr(n: int = 1) -> tuple[dict[str, Any], ChartEhr]:
    """Message `n`'s hook request without prefetch, naming `FHIR_BASE` and `TOKEN`, and the EHR's
    FHIR server that holds the message and its sender (two reads, one for each prefetch key)."""
    request = message_hook(n)
    del request["prefetch"]
    request["fhirServer"] = FHIR_BASE
    request["fhirAuthorization"] = {
        "access_token": TOKEN,
        "token_type": "Bearer",
        "expires_in": 300,
        "scope": "patient/*.rs",
        "subject": SERVICE,
    }
    return request, ChartEhr([patient(), communication(n)], base=FHIR_BASE, token=TOKEN)
