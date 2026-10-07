# Organizations, memberships and invitations

A deployment that serves several teams can keep each in its own organization without anyone
editing its settings or redeploying (ADR-0008). It does that from **records**: people,
organizations and memberships, each holding a set of roles. A deployment that runs one
organization from its identity provider's claims needs none of this: it stays as it is
(`MENDARI_ACCOUNTS=claims`, the default).

The records are being built in steps (LP-12). This page covers what the operator runs today, on
the command line, against the server's database; signing in by records
(`MENDARI_ACCOUNTS=memberships`), the no-access page, invitations in the dashboard and the
Access screen follow. **Until signing in by records ships, nobody can accept an invitation or
take up a membership made here**: the records wait for it, and claims mode never reads them.

## The rules

- An organization has an id of lower-case letters, digits and hyphens (`clinic-a`) and a name.
  `default` is reserved for the data the deployment already holds: only the bootstrap creates
  its record.
- A person is their identity provider's issuer and subject. The e-mail address it verified is
  kept to match invitations; the audit chain holds a digest of it, never the address (keyed
  with `MENDARI_INPUT_HASH_KEY` when the deployment has one). An address here is ASCII, and
  matched without regard to the case of its letters; an internationalised one is refused.
- A membership holds the server's roles (`viewer`, `reviewer`, `service`, `governance`,
  `admin`), and may also hold `pack_author` and `labeller`, which grant nothing yet. `grading`
  is a machine's and is never held by a membership. An organization always keeps an
  administrator who has signed in: removing or demoting its last one is refused (a pending
  membership, not yet taken up, does not count).
- An invitation is a link, valid once, for 7 days by default (at most 30), for whoever signs in
  with the address it names, verified by their identity provider. **Mendari sends no e-mail**:
  the link is shown once to whoever made it, who passes it on.
- Every change appends a chained event of ids and digests (`organization.created`,
  `membership.created`, `invitation.created`, `invitation.accepted`, `access.requested`, ...);
  `mendari audit verify` covers them.

## Before switching a deployment to memberships mode

Run the preflight against the server's database. It opens the database read-only and writes
nothing; it lists every organization that keys, clients, approvals, holds, decisions and
evaluation reports name, with counts, and which have no record yet. It also counts the EHR
registrations (`MENDARI_SMART_EHRS`) **of the environment it runs in**: run it with the
deployment's settings, or those are not the deployment's.

```bash
mendari access check --db "$DATABASE_URL"
```

It exits 1 while one has no record. Give each a record, then run it again:

```bash
mendari access org create clinic-a --name "Clinic A" --db "$DATABASE_URL"
```

An organization in use whose id no record can have (`"recordable": false`: not lower-case
letters, digits and hyphens, as a claim may have given it) is named on its own line. Its keys,
clients and data stay under that id; do not switch the deployment to memberships mode until
the owner decides what becomes of it.

The commands that write (`org create`, `bootstrap`, `invite`) open the database under the
deployment's PHI settings without marking it. In a database a PHI mode protects they need the
deployment's `MENDARI_INPUT_HASH_KEY`, so the chain holds keyed digests of addresses, and
refuse without it.

## The owner's bootstrap

Give the owner an administrator's membership of `default`, the data the deployment already
holds, for the address they sign in with:

```bash
mendari access bootstrap --email owner@example.org --db "$DATABASE_URL"
```

It creates `default`'s record if it has none, and the membership, pending: it becomes the
owner's the next time they sign in with that address verified by their identity provider (it
never picks a person by an address stored from an earlier sign-in). Running it again adds
nothing twice, and it never rewrites a row the deployment already holds.

## Inviting an organization's first administrator

```bash
mendari access invite --org clinic-a --email lead@example.org --role admin --db "$DATABASE_URL"
```

The link is printed once, on its own line: pass it on yourself. It can be accepted once signing
in by records ships; until then it waits, and expires on its day. From then on that
organization's administrators invite their own people.

## Reading what is there

```bash
mendari access org list --db "$DATABASE_URL"
mendari access members --org clinic-a --db "$DATABASE_URL"
mendari access requests --db "$DATABASE_URL"   # people who asked to join, not yet handled
```

These open the database read-only.
