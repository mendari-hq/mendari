# ADR-0008: Accounts and memberships beside the claims a sign-in carries

## Status
Accepted (2026-11-07: the hosted site runs memberships mode since that day, switched after the
owner chose "Now, after this PR"). Proposed 2026-10-07. Built (LP-12, 12a to 12k, 2026-10-08),
each default below as `tasks/todo.md` records it, each confirmed by the owner on 2026-11-07.
The owner's decisions that had no default were settled that day: the operator is the owner, on
the command line; the local datasets folder holds the shipped synthetic examples, shared by
every organization; and no organization in use had an id no record can have (the preflight,
`mendari access check`, found only `default`, recordable but without a record until the
bootstrap gave it one), so what becomes of such an organization was moot. Gate G2-3's
prerequisite `a6-accounts` (`launch/gates.yaml`) names the tests that hold it. Amended by
[ADR-0011](ADR-0011-hidden-evaluation-sets.md) (LP-11d, 2026-10-09): the reserved role
`labeller` grants `label` on a membership (memberships mode only; claims mode knows no such
role); `pack_author` still grants nothing.

## Context
A request acts for one organization, taken from the identity provider's claim at sign-in
(`MENDARI_OIDC_ORG_CLAIM`), and its roles come from `MENDARI_OIDC_ROLE_MAP`, read at start-up.
There are no user, organization or membership records. A sign-in with no organization claim
lands in `default`, so `default` is a catch-all, not the owner's own organization; a partner
team needs the owner to edit settings and redeploy to get an organization of its own. The
audit chain is one chain for the deployment, with no organization column; decision events name
no organization, while a few administrative kinds (`governance.approved`, `automation.held`,
`key.created`, `client.registered`, `eval.recorded`, `notice.acknowledged`) carry it in their
hashed payload.

The hosted site needs each partner team in its own organization without a redeploy, and nobody
in an organization by default. A self-hosted site that runs one organization from its identity
provider's claims needs nothing to change.

## Decision
- **Two modes.** `MENDARI_ACCOUNTS=claims` (the default) is today's behaviour, unchanged.
  `MENDARI_ACCOUNTS=memberships` is for the hosted site: users (issuer, subject, verified
  e-mail), organizations as records, and memberships, each holding a set of roles.
- **No fallback organization.** In memberships mode a sign-in without an active membership acts
  for no organization: `tenancy.acting_for_nobody`, in which every read and write that resolves
  its organization through `tenancy.current()` raises `NoOrganization` (403). It is never None,
  which means every organization (open mode, the command line). The routes that pass the
  principal's organization to the store themselves (holds, quotas, keys, clients, the health
  summary) and the streamed audit export do not go through it: memberships mode refuses a
  principal of no organization before them. `default` is reserved for the owner's existing
  data; an idempotent bootstrap command adds the owner's membership and rewrites no row.
- **Invitations** are single-use, expiring links bound to a verified e-mail address, shown to
  the inviting admin to pass on. Mendari sends no e-mail. The operator creates organizations
  until self-service.
- **Every route is filtered by the caller's organization**, with a reviewed allow-list of the
  deployment-wide routes and the reason each holds no organization data
  (`tests/test_two_organizations.py`, which calls every mounted route as two organizations). Each chained event of
  an organization names it in its hashed payload (memberships mode; new event kinds always), so
  `verify` can cross-check it against the row it is about; older events are attributed by the
  row they name, never rewritten. Memberships mode, here, is the database's: a
  memberships-mode server marks it (`org_bound`), and every store that opens it afterwards (the
  command line's, a claims-mode server's after a rollback) names the organization too. The
  kinds that already carry it keep it in both modes. Each organization gets a reader and an
  export of its own administrative events.
- **Reserved roles.** `pack_author` and `labeller` may be held on a membership and grant nothing
  until LP-20 and LP-11 give them permissions; `grading` is held by no person (LP-11: a machine
  role only). None of them joins the claims-mode roles, whose sign-in and role map stay as
  they are.
- **Credentials.** One Access screen for API keys and Backend Services clients (create, scope,
  rotate, revoke, history). Rotating a client replaces its key set. Key presets, one per
  documented integration path, live in one table that drives the API, the screen and the docs.
  A decide-scoped credential may read the read-only lists of the packs it may decide with
  (`GET /api/packs`, `GET /cds-services`; ADR-0004 as amended, LP-12c). A daily cap per principal sits beside the
  per-organization quota and answers 429 with `Retry-After`; it is off unless configured.

## Alternatives rejected
- **A second hash formula that covers the organization.** The previous release's `verify`
  would fail on a database written by this one, so rolling back would break the chain.
- **An `org` column on `audit_events`.** It marks every older database as unmigrated and
  builds an index on the largest table under the start-up lock.
- **Falling back to `default` in memberships mode.** It is the catch-all this decision removes.

## Consequences
- Claims mode is unchanged: the tests that pin it stay as they are, and an acceptance test
  says so. Additive, claims-visible changes (new routes, the Access screen, presets, the cap
  off by default) count as "behaves as before".
- The operator, by default the command line only, is the one who sees the whole chain verified,
  deployment-wide totals and holds set for the whole deployment. In memberships mode
  `/api/audit/verify` and `/api/deployments` answer `404`, `/api/health` leaves out the
  deployment's call totals and purge counts, and a deployment hold shows only what it covers
  and since when. An organization reads and exports its own administrative events
  (`/api/audit/administrative`), attributed through each row it owns, without `seq` or
  another organization's or the deployment's events; a person's notice acknowledgement is not
  one organization's.
- Switching the hosted site to memberships mode, the owner's bootstrap, and the cap's value are
  the owner's, after a preflight (`mendari access check`) against the production database.
- Memberships mode keeps its sessions apart (a sealed `member` row; claims mode's are `user`
  rows): switching mode, either way, signs everyone out. The session keeps the organization and
  membership chosen, never roles: each request reads that membership, so a removal or a change
  of roles applies at the next request. One check in the guard refuses a principal of no
  organization before every route that is not public, a page or a person's own (choosing an
  organization, asking to join, accepting an invitation), and the guard then acts for nobody.
- Accepting an invitation to an organization one is already a member of is refused (`409`), and
  the link stays open: roles change only by an administrator (LP-12f).
- A sign-in records the person only when a membership waits for their verified address, they
  hold one, or they were recorded before; a request to join answers with the same status and
  body whether or not the organization exists (its timing differs, and asking one that exists
  leaves a request its administrators see).
- A request naming another organization than the one chosen since (`X-Mendari-Org`) is refused
  (`409`); the dashboard sends the organization a window shows with every request.
- `verify` answers `"reason": "organization"` at the first event whose organization is not its
  row's: a row moved to another organization, or deleted. So every row an event names is
  permanent: an expired invitation, a handled request or an organization that leaves keeps its
  rows. That holds in claims mode too for the rows whose events already named their
  organization (keys, clients, automation approvals, holds, evaluation reports): moving one to
  another organization by SQL breaks `verify`, so a re-attribution (an organization in use that
  no record can have) must be a chained event of its own.
- Events chained before the mark name no organization, so `verify` cannot tell where their row
  belonged: a row moved by SQL while none of its events named its organization goes unnoticed,
  and its later events name the organization it was moved to (threat model T1).
- An instance of the previous deployment still serving while memberships mode is switched on
  read the mark when it started, so until it stops its events of the kinds of before name no
  organization; `verify` and the organization's reader attribute those by their row. After a
  rollback to claims mode, events keep naming organizations.
