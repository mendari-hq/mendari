#!/usr/bin/env bash
# CDS Hooks: the services, one per agent that declares a hook, and a call to the demo agent's
# (the Portal Message Agent's) with a synthetic patient and message as prefetch (the API reference's own FHIR example,
# server/api_docs.py FHIR_EXAMPLE), answered with cards.
#
# MENDARI is the server; with sign-in on, MENDARI_KEY is an API key (`ohk_…`, the `service` role
# or the `cds-hooks` preset); open mode ignores it, and answers CDS Hooks on this machine only, so
# 127.0.0.1 and localhost need no flag. An EHR that signs its own tokens is registered instead
# (docs/guides/connecting-an-ehr.md). Needs curl and jq. CI runs this file in open mode, with a
# service key and with a key from the `cds-hooks` preset.

# step: discover — The CDS services this server offers
curl -sS --fail-with-body "$MENDARI/cds-services" -H "Authorization: Bearer $MENDARI_KEY" \
  | jq '[.services[] | {id, hook}]'

# step: hook — Call the agent's hook with the patient and the message as prefetch
curl -sS --fail-with-body -X POST "$MENDARI/cds-services/mendari.portal-message-agent" \
  -H "Authorization: Bearer $MENDARI_KEY" -H 'content-type: application/json' \
  -d '{"hook": "mendari-message-received", "hookInstance": "7f1c2a4e-0d1b-4c55-9a51-3c1e2d000003",
    "context": {"patientId": "example-patient", "communicationId": "example-message"},
    "prefetch": {
      "patient": {
        "resourceType": "Patient", "id": "example-patient",
        "meta": {"tag": [{"system": "https://mendari.co/fhir/sandbox", "code": "synthetic"}]},
        "identifier": [{"system": "https://mendari.co/fhir/synthetic-case", "value": "1"}],
        "name": [{"family": "Synthetic", "given": ["Example"]}],
        "gender": "unknown", "birthDate": "1984-06-15"},
      "communication": {
        "resourceType": "Communication", "id": "example-message", "status": "in-progress",
        "subject": {"reference": "Patient/example-patient"}, "sent": "2026-09-29T13:00:00Z",
        "payload": [{"contentString": "Can I get a refill of my blood pressure medicine?"}]}}}' \
  | jq '.cards[] | {summary, indicator}'
