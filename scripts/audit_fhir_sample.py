"""Write a sample of the FHIR audit export for the HL7 validator (CI job `fhir-conformance`).

A throwaway store (in PHI mode `encrypt`) gets synthetic decisions from the inbox pack: one
confirmed in review, one overridden, one automated with a downstream outcome and feedback. One
input is read and then purged by retention. The full export (AuditEvent and Provenance) is
written as a `collection` Bundle.

    uv run python scripts/audit_fhir_sample.py var/fhir-samples/audit-export.json
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from openhealth import AuditStore, Decision
from openhealth.core.phi import PhiPolicy
from openhealth.standards import audit_fhir


def main(out: Path) -> None:
    with tempfile.TemporaryDirectory() as scratch:
        phi = PhiPolicy(mode="encrypt", key=os.urandom(32))
        store = AuditStore(Path(scratch) / "audit.db", phi=phi)
        decide = Decision.from_hub("openhealth/patient-inbox-router", store=store)
        emergency = {"message": {"body": "Crushing chest pain and my left arm is numb"}}
        confirmed, overridden = decide(emergency), decide(emergency)
        routine = decide({"message": {"body": "Can you send a refill for metformin?"}})
        store.resolve_review(confirmed.id, final_label="nurse", reviewer="rn-1")
        store.resolve_review(overridden.id, final_label="physician", reviewer="rn-2")
        store.record_outcome(routine.id, final_label="front_desk", source="staff re-route")
        expected = {"question": "destination", "label": "front_desk"}
        store.record_feedback(routine.id, rating="down", expected=expected)
        store.get(confirmed.id, reader="rn-1")  # an access, logged
        store.purge_inputs(0)  # retention
        resources = list(audit_fhir.resources(store.iter_events()))
        store.close()
    bundle = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{"fullUrl": audit_fhir.full_url(r), "resource": r} for r in resources],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
    print(f"{len(resources)} resources written to {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "var/fhir-samples/audit-export.json"))
