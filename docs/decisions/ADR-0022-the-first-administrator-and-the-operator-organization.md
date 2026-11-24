# ADR-0022: The first administrator and the operator organization, named in the deployment's settings

## Status
Accepted (2026-11-24, with the change that builds the one-machine install, `deploy/site`).
Decided by the maintainer on 2026-11-24, before the repository was public, so no comment period ran; open to revision when it is.

It amends [ADR-0008](ADR-0008-accounts-and-memberships.md) in two places: how an organization
gets its first administrator, which was the operator's command only, and who operates a
deployment, which was whoever holds its command line and nobody through the web.
[ADR-0021](ADR-0021-an-agents-connection-set-in-the-console.md) gives the permission decided
here one more use.

## Context
Memberships mode keeps people, organizations and roles in the engine's database
([ADR-0008](ADR-0008-accounts-and-memberships.md)). Two things about it were the operator's,
at a terminal:

- **The first administrator.** An organization's first membership was made with `mendari
  access bootstrap` on the machine. Every later one is an invitation an administrator sends from the
  console, but nobody can invite the first.
- **Operating the deployment.** What concerns the whole deployment and not one organization
  (verifying the whole audit chain, the deployment records, whether the scheduled jobs ran,
  whether alerts are delivered) was readable from the command line and from the JSON of
  `/api/health`. In memberships mode no role held it through the web, on purpose: an
  administrator of one organization on a shared server must not see the server's own state.

A hospital that installs Mendari on one machine has neither a person who knows the command
line nor a reason to separate "the organization" from "whoever runs the server": they are the
same people. The install was to end with an administrator signed in through the hospital's own
identity provider and a console that says what is left to do, with no command typed after it.

Both changes move trust. The first lets a line of configuration make an administrator. The
second lets a role held through the web see and do what only the machine's owner could.

## Decision
1. **A deployment names the organization that operates it**: `MENDARI_OPERATOR_ORG`, an
   organization's id. Its administrators, signed in as people, hold a permission of their own,
   `operate`. It is people's only, as `keys` is (`server/auth.py` `PEOPLE_ONLY`): no API key,
   Backend Services client, EHR launch or CDS client holds it, whatever its role or scopes.
2. **Who holds `operate`, by mode.**
   - *Memberships mode*: nobody without the setting, as before this record; with it, the
     administrators of that one organization, and never an administrator of another.
   - *Claims mode* (roles from the identity provider's groups, one set of administrators):
     every administrator, as before, unless the setting names an organization, and then only
     that organization's.
   - *Open mode* (no sign-in, synthetic data on one machine): whoever reaches the server, as
     for everything else there.
3. **What `operate` gives**, and nothing else does:
   - the deployment's readiness as checks, and the System view that shows them
     (`GET /api/system/checks`): each check names settings and public facts only, never a
     secret, a key, the alert webhook's address or any organization's data;
   - the operator's actions that needed a terminal: verifying the whole audit chain now,
     sending a test alert, checking an EHR's connection (`POST /api/system/...`), each recorded
     as the engine records it;
   - the deployment's own views, which span organizations: the whole chain's verification
     (`/api/audit/verify`) and the deployment records (`/api/deployments`). They answer its
     holders and are a `404` to everyone else;
   - allowing, for one agent of the operator organization, a service address that the
     deployment's list of addresses it may call does not name
     ([ADR-0021](ADR-0021-an-agents-connection-set-in-the-console.md), point 5): to public
     addresses only, and chained. This one needs the organization **named**: where sign-in is
     by claims and the setting names none, every administrator holds `operate` and none of
     them may allow an address (narrowed on 2026-11-24; ADR-0021 says why).
4. **What it does not give.** No other organization's decisions, inputs, people, keys, agents
   or connections: every route still acts for the organization the person is signed in to. No
   setting can be changed through the web: the settings file stays the machine's. Nothing an
   administrator did not already hold in their own organization, beyond the list above.
5. **A fresh install names its first administrator**: `MENDARI_BOOTSTRAP_ADMIN`, a sign-in
   e-mail address, in memberships mode. The first person who signs in with that address,
   **verified by the identity provider at that sign-in** (`email_verified`), is made an
   administrator of the operator organization (`default` where none is named), whose record is
   made with it and called what `MENDARI_BOOTSTRAP_ORG_NAME` says.
6. **It works once.** The membership and a mark in the database are written in one
   transaction, under the chain's lock. From then on the setting gives nothing: not to the
   same address again, not to another address written in its place, not after a restart. It
   also gives nothing where the organization already has an administrator who signed in, so a
   database restored from before its mark cannot be used to make a second one: an organization
   never loses its last administrator. The server says at start-up that the setting is being
   ignored, and it can be taken out.
7. **It is on the record.** The organization's record, the person's and the membership are
   chained as made by `first-administrator setting`, never as made by a person; the security
   log has `auth.first_admin.granted` once, and `auth.first_admin.ignored` when the address
   signs in to a database that no longer takes it.
8. **What stops a start.** `MENDARI_BOOTSTRAP_ADMIN` that is not an e-mail address, or that is
   set where people are not managed as memberships (there is no membership to give),
   `MENDARI_OPERATOR_ORG` that no organization could have as its id, and
   `MENDARI_BOOTSTRAP_ORG_NAME` longer than 200 characters each stop the server, naming the
   setting. A first administrator set without an operator organization starts, with a warning:
   that person would administer `default` and nobody would see the System view.
9. **The one-machine install sets both to the site's own organization** (`default`): a
   hospital's administrators are its deployment's operators. A deployment shared by several
   organizations names the one that runs it, or none.

## Consequences
- A hospital installs, signs in and finishes setting up in the console. The first
  administrator invites the second from **Access**; nothing is typed on the machine again.
- Whoever can write the deployment's settings can name the first administrator. They could
  already do anything to the deployment: read its database, replace its keys. The setting adds
  no power to that position; it makes its use visible in the chain.
- Whoever controls the named mailbox at the identity provider before that first sign-in
  becomes the administrator. The identity provider must say the address is verified; a
  provider that does not is refused for this, as it is for invitations. If the address was
  wrong, the install is run again with the right one before anyone has signed in with it.
- On a server shared by several organizations, the operator organization's administrators see
  that the deployment is healthy and can verify the whole chain, which says whether every
  organization's events are intact and nothing of what they hold. They see no other
  organization's data. An organization that should not have that view is not named.
- `operate` is one more people-only permission the guard, the notice check, the rate limits
  and the route tests know. A route under `/api/system/` is the operator's by its path: one
  added there needs no rule of its own.
- The operator's command line keeps everything it had. Nothing was removed from it.

## Alternatives considered
- **Keep the first membership on the command line.** It is why an install could not end in a
  browser, and it asks a hospital's IT to run a command inside a container.
- **Make whoever signs in first the administrator.** No setting to get wrong, and a race
  anyone on the network can win.
- **A one-time token printed by the installer**, typed into the console. It proves the person
  read the installer's output, not who they are; the address ties the administrator to an
  identity the hospital's own provider verified, and leaves nothing secret in a terminal's
  scrollback.
- **Let the setting apply at every start** until it is removed. A restore, or a line left in a
  file for a year, would then make administrators nobody decided on.
- **Give every administrator `operate` in memberships mode.** On a shared server, each
  organization's administrators would see the server's state and verify every organization's
  chain.
- **A separate "operator" role** an administrator grants. One more role to explain and to
  audit, for a permission that follows from which organization runs the server.
