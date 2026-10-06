# Holding automation

An **automation hold** stops a deployment from automating, at once and without a redeploy.
While a hold covers a decision, a decision that would have been `AUTO` goes to a person
(`HUMAN_REVIEW`) instead, with the same recommendation and priority. Decisions still run and are
recorded; rules still raise priority (a possible emergency still reaches a nurse at `stat`);
`ABSTAIN` and `BLOCK` are unchanged. A hold only ever lowers automation.

Use one when something looks wrong and you need people to check every decision while you find
out: a missed emergency, a provider behaving oddly, a pack version you are unsure of, an incident.

## What a hold covers

| Hold | Covers | Set and lifted |
|---|---|---|
| One version of a pack (`pack_id` and `pack_digest`) | that digest, in your organization | the API, the dashboard |
| A pack (`pack_id`) | every version of it, in your organization | the API, the dashboard |
| Every pack | every decision in your organization | the API |
| Every organization (`--all-orgs`) | every decision on the deployment | the command line only |

Holds are rows in the deployment's database, read by each decision (and reused on a server for
at most five seconds), so a hold set anywhere reaches every instance within seconds. Setting and
lifting each append a chained event (`automation.held`, `automation.lifted`) with who and when;
`mendari audit verify` covers them. `/api/health` shows how many holds are in force.

## From the dashboard or the API

People who may approve automation (`governance` and `admin` roles) see the holds in force on the
pack's Decision Card, can hold the pack with a reason, and can lift their organization's holds.
The overview says when a hold covers the pack.

```bash
curl -X POST "$MENDARI/api/holds" -H "authorization: Bearer $KEY" \
  -H "content-type: application/json" \
  -d '{"pack_id": "mendari/patient-inbox-router", "reason": "a missed emergency, checking"}'
curl "$MENDARI/api/holds" -H "authorization: Bearer $KEY"
curl -X DELETE "$MENDARI/api/holds/<id>" -H "authorization: Bearer $KEY"
```

## From the command line (break-glass)

The command line writes to the database directly, so it works with the server down or
unreachable. A hold it sets for every organization can only be lifted there.

```bash
mendari automation hold --all-orgs --reason "incident 7" --db "$DATABASE_URL"
mendari automation hold --org clinic-a --pack mendari/referral-router --reason "checking" --db "$DATABASE_URL"
mendari automation list --db "$DATABASE_URL"
mendari automation lift <id> --db "$DATABASE_URL"
```

## Over an organization's daily quota

A decision over its organization's daily decisions quota is held the same way: its remote calls
(Jev, language models) fail with the quota, its local providers (the keyword rules, naive Bayes,
criteria, dosing, and a CQL engine the deployment runs) still answer, and any decision that
would have been automated goes to a person. An emergency rule still raises its message to `stat`.
