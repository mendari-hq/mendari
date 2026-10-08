#!/usr/bin/env bash
# FHIR in, FHIR out: what the FHIR base does, $decide on a synthetic Bundle, the decision read
# back as a Task, and its audit trail as AuditEvent and Provenance. The Bundle is the API
# reference's own example (server/api_docs.py FHIR_EXAMPLE): synthetic.
#
# MENDARI is the server; with sign-in on, MENDARI_KEY is an API key (`ohk_…`, the `service`
# role); open mode ignores it. Needs curl and jq. CI runs this file in open mode and with a
# service key; a step only an administrator may run is skipped with the key.

# step: metadata — What this FHIR base does (public)
curl -sS --fail-with-body "$MENDARI/fhir/metadata" | jq '{fhirVersion, software}'

# step: decide — Decide from a FHIR Bundle, and get the decision as FHIR
bundle=$(curl -sS --fail-with-body -X POST "$MENDARI/fhir/\$decide" \
  -H "Authorization: Bearer $MENDARI_KEY" -H 'content-type: application/fhir+json' \
  -d '{"resourceType": "Parameters", "parameter": [
    {"name": "pack", "valueString": "mendari/patient-inbox-router"},
    {"name": "data", "resource": {"resourceType": "Bundle", "type": "collection", "entry": [
      {"fullUrl": "urn:uuid:7f1c2a4e-0d1b-4c55-9a51-3c1e2d000001", "resource": {
        "resourceType": "Patient", "id": "example-patient",
        "meta": {"tag": [{"system": "https://mendari.co/fhir/sandbox", "code": "synthetic"}]},
        "identifier": [{"system": "https://mendari.co/fhir/synthetic-case", "value": "1"}],
        "name": [{"family": "Synthetic", "given": ["Example"]}],
        "gender": "unknown", "birthDate": "1984-06-15"}},
      {"fullUrl": "urn:uuid:7f1c2a4e-0d1b-4c55-9a51-3c1e2d000002", "resource": {
        "resourceType": "Communication", "id": "example-message", "status": "in-progress",
        "subject": {"reference": "Patient/example-patient"}, "sent": "2026-09-29T13:00:00Z",
        "payload": [{"contentString": "Can I get a refill of my blood pressure medicine?"}]}}]}}]}')
jq '[.entry[].resource.resourceType]' <<<"$bundle"  # a GuidanceResponse first
jq '.entry[].resource | select(.resourceType == "Parameters").parameter[]
  | select(.name == "outcome")' <<<"$bundle"       # act on the outcome, not on `status`
id=$(jq -r '.entry[0].resource.identifier[0].value' <<<"$bundle")  # the decision id

# step: search — The review queue, and the decision as a Task with its audit trail
curl -sS --fail-with-body "$MENDARI/fhir/Task?status=requested&_total=accurate" \
  -H "Authorization: Bearer $MENDARI_KEY" | jq .total                   # the review queue
curl -sS --fail-with-body "$MENDARI/fhir/Task/$id/_history/1" \
  -H "Authorization: Bearer $MENDARI_KEY" | jq '{id, status, meta}'     # as first decided
curl -sS --fail-with-body "$MENDARI/fhir/AuditEvent?entity=Task/$id" \
  -H "Authorization: Bearer $MENDARI_KEY" | jq '[.entry[].resource.id]' # its audit trail
curl -sS --fail-with-body "$MENDARI/fhir/Provenance?target=Task/$id" \
  -H "Authorization: Bearer $MENDARI_KEY" | jq '[.entry[].resource.id]'

# step: trail — One decision's audit trail as a FHIR Bundle (Mendari's own API: an unscoped service key)
curl -sS --fail-with-body "$MENDARI/api/audit/$id/fhir" -H "Authorization: Bearer $MENDARI_KEY" \
  | jq '[.entry[].resource.resourceType]'

# step: export — The whole audit trail as FHIR NDJSON (open mode, or an administrator signed in)
curl -sS --fail-with-body \
  "$MENDARI/api/audit/export/fhir?pack_id=mendari/patient-inbox-router&since=2026-09-01" \
  | jq -c '{resourceType, id}'
