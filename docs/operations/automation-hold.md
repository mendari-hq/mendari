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
| One version of a pack (`pack_id` and `pack_digest`) | that digest, in your organization | the API, the command line (lifted in the dashboard too) |
| A pack (`pack_id`) | every version of it, in your organization | the API, the dashboard, the command line |
| Every pack | every decision in your organization | the API, the command line (lifted in the dashboard too) |
| Every organization (`--all-orgs`) | every decision on the deployment | the command line only |

Holds are rows in the deployment's database, read by each decision (and reused on a server for
at most five seconds), so a hold set anywhere reaches every instance within seconds. Setting and
lifting each append a chained event (`automation.held`, `automation.lifted`) with who and when;
`mendari audit verify` covers them. `/api/health` shows how many holds are in force. In
memberships mode an organization sees a hold set for every organization as what it covers and
since when; its reason and who set it are the operator's.

## From the dashboard or the API

Everyone who can read the pack sees the holds in force on its Decision Card, and the overview
says when one covers the pack. People who may approve automation (signed in with the
`governance` or `admin` role) can hold the pack there with a reason and lift their
organization's holds. Holding is one click; lifting asks first, in a dialog that says the pack
then decides on its own again. API keys and clients cannot hold or lift (their roles never approve); they
can read `GET /api/holds`. The routes are `POST /api/holds` (`pack_id`, optional `pack_digest`
of the version this deployment serves, `reason`) and `DELETE /api/holds/{id}`.

## From the command line (break-glass)

The command line writes to the database directly, so it works with the server down or
unreachable. A hold it sets for every organization can only be lifted there. It takes the
server's database from `--db` or `$DATABASE_URL`, says which one it wrote to, and refuses one
that does not exist or has never served a decision (a typo would otherwise hold nothing), a pack
that database never served, and a malformed digest. `mendari decide --db` honours its holds.

```bash
mendari automation hold --all-orgs --reason "incident 7" --db "$DATABASE_URL"
mendari automation hold --org clinic-a --pack mendari/referral-router --reason "checking" --db "$DATABASE_URL"
mendari automation list --db "$DATABASE_URL"
mendari automation lift <id> --db "$DATABASE_URL"
```

## What a hold means for prior authorization

A held decision is a person's to confirm: the Da Vinci PAS route (`POST /api/packs/{id}/pas`
with `submit`) sends a request to the payer only for a `submit_ready` recommendation that was
automated, or that a reviewer resolved as `submit_ready` (the label the review chained; an
outcome reported later does not count).

## Over an organization's daily quota

A decision over its organization's daily decisions quota is held the same way: its remote calls
(Jev, language models) fail with the quota, its local providers (the keyword rules, naive Bayes,
criteria, dosing, and a CQL engine the deployment runs) still answer, and any decision that
would have been automated goes to a person. An emergency rule still raises its message to `stat`.
