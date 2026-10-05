"""Write a sample of the FHIR audit export for the HL7 validator (CI job `fhir-conformance`).

A throwaway store (in PHI mode `encrypt`) gets synthetic decisions from the inbox pack, its
urgency screen run with an in-process Jev (`sample_jev`): one confirmed in review, one
overridden (with a note, later erased on request), one automated with a downstream outcome and
feedback, and one asked for by a signed-in person, so its AuditEvent and Provenance name the
requester. One input is read and then purged by retention. The full export (AuditEvent and
Provenance) is written as a `collection` Bundle.

    uv run python scripts/audit_fhir_sample.py var/fhir-samples/audit-export.json
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from sample_jev import calm_jev

from mendari import AuditStore, Decision
from mendari.core import tenancy
from mendari.core.phi import PhiPolicy
from mendari.standards import audit_fhir


def main(out: Path) -> None:
    with tempfile.TemporaryDirectory() as scratch, calm_jev() as providers:
        phi = PhiPolicy(mode="encrypt", key=os.urandom(32))
        store = AuditStore(Path(scratch) / "audit.db", phi=phi)
        decide = Decision.from_hub("mendari/patient-inbox-router", providers=providers, store=store)
        emergency = {"message": {"body": "Crushing chest pain and my left arm is numb"}}
        confirmed, overridden = decide(emergency), decide(emergency)
        routine = decide({"message": {"body": "Can you send a refill for metformin?"}})
        # As the sign-in guard sets it for a request: the organization, and who asked.
        with tenancy.acting_for("clinic-a", "nurse.lee@clinic-a.example.org"):
            decide({"message": {"body": "Please reschedule my appointment to next week"}})
        store.resolve_review(confirmed.id, final_label="nurse", reviewer="rn-1")
        note = "Synthetic note: seen by the on-call physician"
        override = store.resolve_review(
            overridden.id, final_label="physician", reviewer="rn-2", note=note
        )
        store.record_outcome(
            routine.id, final_label="front_desk", source="staff-reroute", recorded_by="rn-9"
        )
        expected = {"question": "destination", "label": "front_desk"}
        store.record_feedback(routine.id, rating="down", expected=expected)
        store.get(confirmed.id, reader="rn-1")  # an access, logged
        store.erase_note(override["note_ref"]["id"], erased_by="rn-2", reason="error")
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
