# Revoking access

When a key leaks, a client is compromised or someone must stop reaching Mendari, take their
access away at once. An organization's administrators do it for their own organization from the
dashboard's Access view or the API. The operator does it for any organization from the command
line, which writes to the database directly, so it works with the server down or unreachable
(the break-glass).

Every server reads each key, client, membership and session from the database on every request,
with nothing cached in memory, so a revocation takes effect on the next request, on every
instance, without a restart.

## What takes access away

| To stop | Do | Takes away |
|---|---|---|
| One API key | the Access view, `DELETE /api/auth/keys/{id}`, or `mendari access revoke key <id>` | the key, at once, during a rotation's overlap too |
| One Backend Services client | the Access view, `DELETE /api/auth/clients/{id}`, or `mendari access revoke client <id>` | the client and every access token it holds |
| Every key and client of an organization | `mendari access revoke all --org <org>` | each one still in force (an expired key is left as it is) |
| One person, in memberships mode | remove their membership in the Access view (`DELETE /api/auth/members/{id}`) | their access to that organization, at their next request |
| One person, in claims mode | remove them at the identity provider, or take their group out of `MENDARI_OIDC_ROLE_MAP`, then end people's sessions | a session keeps the claims it signed in with: their roles are re-read under the settings in force on every request, but the claims change only at the next sign-in |
| Everyone signed in | `mendari access end-sessions --kind people` | every person's session and every sign-in under way |
| Every EHR launch | `mendari access end-sessions --kind launches` | every launch's session and every launch under way |

Ending sessions signs people out; it does not stop them signing in again. Take away what lets
them in first (a membership, a group at the identity provider, an EHR registration), then end
the sessions.

A person's session in memberships mode and an EHR launch's are sealed under their cookie, so
the database cannot tell whose they are: `end-sessions` ends a whole kind, never one person's or
one organization's. Everyone signs in again; nothing else is lost. It never touches the record
of client assertions already used, which stops one being replayed.

## From the command line (break-glass)

The commands take the server's database from `--db` or `$DATABASE_URL`, say which one they wrote
to, and refuse one that does not exist or does not have Mendari's tables (nothing is created in
it). Each needs a reason (one line, at most 200 characters), chained with the event:
`key.revoked` and `client.revoked` (with `revoked_by: command line (<user>)`) and
`sessions.ended` (the kinds, how many live sessions ended, who and why). `mendari audit verify`
covers them. They need no PHI key: they write no input and no address.

```bash
mendari access keys --org clinic-a --db "$DATABASE_URL"      # ids, names, roles, status
mendari access clients --org clinic-a --db "$DATABASE_URL"
mendari access revoke key 3f9a2c1b7d4e --reason "leaked in a support ticket" --db "$DATABASE_URL"
mendari access revoke client 5b6c0d1e2f3a4b5c6d7e8f90 --reason "incident 7" --db "$DATABASE_URL"
mendari access revoke all --org clinic-a --reason "incident 7" --db "$DATABASE_URL"
mendari access end-sessions --kind people --reason "incident 7" --db "$DATABASE_URL"
```

`keys` and `clients` only read (never a secret or its hash); without `--org` they list every
organization's. `revoke key` and `revoke client` say so and exit 1 for an id that is unknown or
already revoked, as `revoke all` does for an organization with nothing left in force; running
one again changes nothing.

An organization's administrators see these revocations in their administrative events
(`/api/audit/administrative`), as they see their own; `sessions.ended` is the deployment's, read
by the operator (`mendari audit export --administrative`).

## Afterwards

- Replace what you revoked: a new key (`POST /api/auth/keys`), or a client registered anew.
- To stop decisions being automated while you find out what happened, hold automation as well
  ([Holding automation](automation-hold.md)).
