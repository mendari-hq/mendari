# Organizations, memberships and invitations

A deployment that serves several teams can keep each in its own organization without anyone
editing its settings or redeploying (ADR-0008). It does that from **records**: people,
organizations and memberships, each holding a set of roles. A deployment that runs one
organization from its identity provider's claims needs none of this: it stays as it is
(`MENDARI_ACCOUNTS=claims`, the default).

The records are being built in steps (LP-12). This page covers the command line, signing in by
records (`MENDARI_ACCOUNTS=memberships`) with the dashboard's access page and organization
switcher, how an organization's administrators invite and manage their people in the
dashboard's Access view or through the API, and the invitation page (`/invite`) a link opens.
The same Access view holds an organization's API keys and Backend Services clients, in claims
mode too (for an administrator who signed in). Claims mode never reads or writes the records.

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
  the link is shown once to whoever made it, who passes it on. The operator invites from the
  command line; an organization's administrators (people who signed in, never a key) invite to
  their own organization, `default`'s included.
- Every change appends a chained event of ids and digests (`organization.created`,
  `membership.created`, `membership.roles_changed`, `membership.removed`,
  `invitation.created`, `invitation.revoked`, `invitation.accepted`, `access.requested`,
  `access.handled`, ...); `mendari audit verify` covers them. An administrator is named there
  by their id (`user:<hash>`), never their address.

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
owner's at their first sign-in in memberships mode with that address verified by their
identity provider (it never picks a person by an address stored from an earlier sign-in). Running it again adds
nothing twice, and it never rewrites a row the deployment already holds.

## Inviting an organization's first administrator

```bash
mendari access invite --org clinic-a --email lead@example.org --role admin --db "$DATABASE_URL"
```

The link is printed once, on its own line, as `<the deployment's address>/invite#<token>`: put the
deployment's public address before it (`MENDARI_PUBLIC_URL` when it has one, the address people
sign in at), and pass it on yourself. A link on another of its names loses its token when
signing in moves the browser to the public address. It waits until it is accepted, revoked or
expires on its day. From then on that organization's administrators invite their own people.

## Signing in by records

`MENDARI_ACCOUNTS=memberships` takes each person's organization and roles from their
membership, never from the identity provider's claims. Switching a deployment is the owner's
decision; in order:

1. `mendari access bootstrap --email <the owner's address>` (it gives `default` its record),
   and `mendari access org create` for each other organization the preflight names;
2. `mendari access check` against its database exits 0 (every organization in use has a
   record);
3. set `MENDARI_ACCOUNTS=memberships` and redeploy. **Everyone signs in again**, both ways: a
   session of one mode is no session in the other. The owner's membership binds at their first
   sign-in then. Rolling back is unsetting it: the records stay, and claims mode never reads
   them.

Then:

- `MENDARI_OIDC_ROLE_CLAIM`, `MENDARI_OIDC_ROLE_MAP` and `MENDARI_OIDC_ORG_CLAIM` are ignored
  (the server logs a warning when one is set). A person's roles are their membership's.
- A sign-in records the person (a `users` row, `user.recorded`) only when a membership waits for
  their address, verified by the identity provider, when they hold one, or when they were
  recorded before. Anyone else who signs in leaves no record.
- With exactly one membership, it is selected at sign-in. With several, or with one given since
  they signed in, the person chooses one: the dashboard shows only that choice until they do
  (beside an EHR launch's patient, if the browser holds one), and a switcher in the top bar
  afterwards, with a Switch button (the choice is the browser's; a window left on the
  organization before says so instead of acting). With none, they act for **no organization**:
  never `default`. The dashboard then shows who they are signed in as, a form to ask to join an
  organization by its id, and a way to use another account. They are refused (`403`,
  `NoOrganization`) everywhere but the public routes, the dashboard page, choosing one of their
  organizations, asking to join one and accepting an invitation.
- Asking to join answers with the same status and body whether or not the organization
  exists, the person is already a member, or they have five requests open: only a request to
  an organization with a record, from someone not in it, is recorded, with the person if they
  have no record yet. Its timing is not the same, and asking an organization that exists
  leaves a request its administrators see.
- A membership removed, or its roles changed, applies at the person's next request. Keys and
  clients a removed person created belong to the organization and keep working: revoke them
  too if they should not (`mendari access members` lists who is in it; the Access view's API
  keys and Clients tabs list who created each). So do the open invitations a removed or demoted administrator
  made: they stay valid until they are accepted, expire or are revoked (the list of
  invitations names who made each). Removing someone does revoke that organization's open
  invitations made out to their address, or to a pending membership's (each chained as
  `invitation.revoked`), so a link left open earlier, one they could not accept while a member,
  does not let them back in.
- Keys, Backend Services clients and EHR registrations of an organization with no record are
  refused.
- The whole-chain verify, deployment-wide deployment records and some health fields are still
  shown to every organization's administrators until LP-12g scopes them: switch the hosted
  site after LP-12g.

## Managing an organization's people

In memberships mode an organization's administrators (the `admin` role, a person who signed in;
never an API key, whatever its role) manage its people through the API, under `/api/auth/`, or
in the dashboard's Access view (`#admin`, "Access" in the section list, shown to them only).
Each acts on the organization the administrator acts for, never one a request names: another
organization's invitation, member or request is a `404`.

The Access view has a tab for each list: **Members** (change roles, remove), **Invitations**
(invite someone, revoke a link) and **Requests to join** (grant with the roles chosen, or
refuse), then **API keys** and, with SMART Backend Services on, **Clients**. The last two are
also an administrator's in claims mode, where they are the only tabs; open mode has no Access
view (the command line and the API remain). Changing roles, removing a member, revoking a link, and granting or refusing a request
ask first, in a dialog that says what it does, and a refusal of one is said in that dialog, in
words (removing or demoting the last administrator who signed in, say). Inviting makes a link at
once, without asking; a refusal to make one (an address that is not one, say) shows as a message.
The link is shown once, whole (with the deployment's address), in a dialog with a Copy button;
closing the dialog or leaving the view takes it off the page, and it is never shown again: a lost
link is revoked and made anew. Who made each invitation and membership is named by their name, "You", or
"Former member" for someone removed since.

**API keys** are made from a preset (what the key is for: its role, scope and expiry, the routes
it reaches and a link to the guide that uses it) or by hand (a role and an optional scope). A
new key is shown once, as an invitation link is; rotating one (with the hours the old key keeps
working) shows the new one once and says when the old one stops. Revoking asks first. **Clients**
are registered with a preset's scope or one chosen by hand, and their public keys: a JWK Set, a
JWK, or a PEM public key, which the page turns into a JWK (its `kid` the key's RFC 7638
thumbprint). The page refuses the private keys it recognises before anything is sent, and clears
them from the page: a PEM `PRIVATE KEY` block, JSON holding a JWK with private or symmetric
members (as a set, an array or a lone JWK), and a private key's DER in base64, bare or under a
`PUBLIC KEY` header. Treat a key refused that way as exposed. Text it cannot read as a public key
is not sent either; what it sends, the server checks again, and it refuses any private or
symmetric member by name. **Replace keys…** replaces a client's key set (its tokens stop at
once); revoking asks first. Each key and client has a **History**: its audit chain events, oldest
first, with who did each (a rotated key's history links to its replacement's). Who made a key or
client is "You", a member's name, or what the chain recorded (an e-mail address or
`issuer|subject`).

| Route | Does |
|---|---|
| `POST /api/auth/invitations` | Invites an address (`email`) with `roles`, valid `days` (7, at most 30). The answer holds the `link` (`/invite#<token>`: put the deployment's address before it; the dashboard shows the whole URL), once. |
| `GET /api/auth/invitations` | Lists them, newest first, with their `status`: `open`, `accepted`, `revoked` or `expired`. Never a token. |
| `DELETE /api/auth/invitations/{id}` | Revokes an open (or expired) one. |
| `POST /api/auth/invitations/accept` | A person's own: accepts one with its `token`, signed in with the address it names, verified. |
| `GET /api/auth/members` | Lists the memberships in force: each person's name and address, or the address a pending one waits for. |
| `PUT /api/auth/members/{id}/roles` | Replaces a membership's roles; they apply at that person's next request. |
| `DELETE /api/auth/members/{id}` | Ends a membership, and revokes the organization's open invitations made out to that person's address (a pending one's too). |
| `GET /api/auth/access-requests` | Lists the open requests to join, with who asked. |
| `POST /api/auth/access-requests/{id}` | Grants one (`outcome: granted`, with `roles`, `viewer` unless asked) or refuses it. |

- **The link is shown once.** Only its digest is kept. Unknown, used, revoked and expired links
  get one answer (`404`); someone signed in with another address, or without a verified one, is
  refused (`403`) and the invitation stays open.
- **Accepting while already a member** of that organization is refused (`409`), and the
  invitation stays open for an administrator to revoke: roles change only by
  `PUT /api/auth/members/{id}/roles`. A person who acted for no organization acts for the new
  one at once; one who acted for another keeps it, and chooses the new one when they want it.
- **An organization keeps an administrator who signed in.** Removing or demoting the last one,
  yourself included, is a `409`; a pending administrator's membership does not count.
- **A request closes by itself** when its person joins another way (accepting an invitation, a
  membership given or bound at sign-in): it is chained as handled, outcome `joined`, and no
  longer counts against their five open requests. Granting one left open from before this,
  of someone who is a member already, is a `409`: refuse it to close it.
- `grading` is never given (`422`), and `pack_author` and `labeller` may be given but grant
  nothing yet.
- Inviting and handling a request to join give someone access, so they wait for the notice
  acknowledgement as creating a key does; accepting an invitation does not.
- On the command line `mendari access members` and `mendari access requests` only read:
  changing roles, removing members and handling requests are done through these routes.

## The invitation page (`/invite`)

A link opens `/invite`, a static page served in memberships mode only (open and claims mode
answer `404`). It is public and never a redirect, so a browser that is not signed in never
carries the link on to the identity provider. What it does:

- **The token stays in the tab.** The page's first script takes the token from the link's
  fragment (the part after `#`, which a browser never sends to a server), clears it from the
  address before anything else loads, and keeps it in that tab's session storage only. It is
  forgotten once the answer is final: joined, not valid, already a member.
- **Signed out:** a Sign in button. Sign in with the address the invitation was sent to: the
  browser comes back to `/invite`, in the same tab, and the invitation is still there.
- **Signed in:** who you are signed in as, and Accept invitation. Nothing is accepted until it
  is pressed, so a link scanner or a preview that opens the page uses nothing up.
- **Joined:** with your roles, and a way into the dashboard. Someone who already acts for another
  organization in this browser is offered to switch to the new one, or to stay.
- **Another address:** the invitation is for another one; Use another account signs out and asks
  the identity provider which account (the invitation waits in the tab, and stays open).
- **Not verified:** the identity provider did not verify the address; sign in with an account
  whose provider does.
- **Not valid:** used, revoked or expired, one answer: ask whoever invited you for a new link.
- **Already a member:** roles change only by the organization's administrators.
- **Open the link on the deployment's public address** (`MENDARI_PUBLIC_URL`). Session storage
  belongs to one address: if signing in moves the browser to another, the page there says there
  is no invitation in this tab, and the link opened again there works.

## Reading what is there

```bash
mendari access org list --db "$DATABASE_URL"
mendari access members --org clinic-a --db "$DATABASE_URL"
mendari access requests --db "$DATABASE_URL"   # people who asked to join, not yet handled
```

These open the database read-only.
