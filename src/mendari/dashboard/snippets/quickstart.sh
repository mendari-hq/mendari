#!/usr/bin/env bash
# Mendari's REST API in three calls: list the agents, ask the bundled demo agent for a decision on a
# synthetic patient message, and read the decision's audit record. The body is synthetic, one of
# the agent's own behaviour tests.
#
# MENDARI is the server (`mendari serve` listens on http://localhost:8000). With sign-in on,
# MENDARI_KEY is an API key (`ohk_…`, the `service` role); open mode ignores it. Needs curl and
# jq. Each "# step:" is shown on its own (the README has them verbatim), and CI runs this file
# against a local server, in open mode and with a service key (scripts/check_doc_snippets.py).

# step: agents — List the agents this server runs
curl -sS --fail-with-body "$MENDARI/api/agents" -H "Authorization: Bearer $MENDARI_KEY" \
  | jq '.[] | {id, version, risk}'

# step: decide — Decide on a synthetic patient message
decision=$(curl -sS --fail-with-body -X POST \
  "$MENDARI/api/agents/mendari/portal-message-agent/decide" \
  -H "Authorization: Bearer $MENDARI_KEY" -H 'content-type: application/json' \
  -d '{"message": {"body": "Could you refill my lisinopril? I am running low."}}')
jq '{id, outcome, recommendation, priority, reasons}' <<<"$decision"
id=$(jq -r .id <<<"$decision")

# step: record — Read the decision's audit record
curl -sS --fail-with-body "$MENDARI/api/audit/$id" -H "Authorization: Bearer $MENDARI_KEY" \
  | jq '{outcome, review_status, events: [.events[].event]}'
