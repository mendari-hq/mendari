# ADR-0015: Registry accounts, under the engine

*Written when agents were called Decision Packs; the decision applies to agents as they are now.*

## Status
Accepted (2026-11-15, the owner, in chat: "Accept all defaults", which accepts each of the ten
defaults in `notes/lp19-plan.md`, "Owner decisions still open"). Proposed 2026-11-09 (LP-19a).
Built, 19a to 19k, 2026-11-09 to
2026-11-15, each step as `tasks/todo.md` records it: owned publisher tokens (19b), namespace
claims (19c), the mount and the people's routes (19d), reviewers (19e), the dashboard (19f), the
administrator token removed (19g), https only (19h), the counter-signature (19i), the
deployment configuration (19j) and the close-out (19k). It follows the owner's choice of
2026-11-07 that the registry runs under the engine app (LP-19's option A). It replaces
[ADR-0012](ADR-0012-a-registry-that-shows-only-what-it-checked.md)'s default that a review is
recorded with the registry's administrator token (ADR-0012's defaults 3 and 5 are amended).
Gate G3-4's `a10-registry-hub` names its tests; the registry turned on and the Hub deployed are
the owner's, and stay pending there.

## Context
The registry ([ADR-0012](ADR-0012-a-registry-that-shows-only-what-it-checked.md)) shows only
what it checked, but who may publish is still a matter of shared secrets:
- A publisher token is random and bound to one namespace, and nothing else. It has no owner, no
  expiry and no way to revoke it.
- One administrator token creates namespaces, mints tokens, records reviews, deletes versions,
  and also passes as a publisher of every namespace.
- A first publish to a new namespace is listed to everyone at once.
- A client trusts a registry's key the first time it sees it, and will talk to a registry over
  plain http.
- No deployment configuration exists for the registry or the Hub.

The engine already has what the registry lacks: sign-in, organizations and memberships
([ADR-0008](ADR-0008-accounts-and-memberships.md)), people-only permissions, and a guard
in front of every route.

## Decision
1. **The registry is mounted under the engine**, at `/registry/v1/...`, on the engine's
   database, behind one switch (`MENDARI_REGISTRY`). Unset, the engine mounts nothing and opens
   no registry table. The standalone `mendari registry serve` stays for self-hosting and tests.
2. **A publisher token belongs to a person and an organization.** It records its owner, its
   organization, its namespace, when it was made, when it expires (90 days unless asked, 365 at
   most) and its revocation. The last day it was used is kept outside the registry's chain,
   because the chain refuses a row that changes without an event. An organization's
   administrators mint, list and revoke tokens in the dashboard's Access view.
3. **A namespace is claimed, and the claim is the organization's link to it.** `mendari` and
   `openhealth` are reserved. One organization holds a namespace.
4. **One approval, at the first publish.** A claimed namespace, its keys and its versions are
   invisible to search, pull, the Hub and every other public read until a registry reviewer
   approves it, with a published version in front of them. To the public a pending namespace
   answers exactly as one that does not exist. A namespace from before this decision stays
   listed.
5. **A reviewer is a person**: a member of the one organization the deployment names
   (`MENDARI_REGISTRY_REVIEW_ORG`), holding a role that counts only there and cannot be granted
   anywhere else. With no review organization, nobody reviews through the web and a first
   publish stays invisible. A reviewer never decides a claim they filed.
6. **The operator's commands on the database are the only path without sign-in** (a self-hosted
   registry, CI, the seed script). Each records `operator` and a reason on the chain.
7. **The administrator token is removed**, all of it, after every act it performed has a tested
   replacement.
8. **A session is never a publish credential.** The mounted registry reads no session cookie;
   publishing is by publisher token only, and minting one is a people-only engine route under
   the origin check.
9. **The registry counter-signs each version it lists** with its own key, under a format of its
   own so no other signature can be replayed as one. The client trusts a registry key that
   ships in the package (public keys only, no hostname) or that it pinned on first use, and
   then refuses a version with no valid counter-signature.
10. **The client refuses plain http**, except to this machine, before any token is sent.

A registry reviewer's approval lists a pack. It never lets one automate: the regression check,
recalls, the cap on a pulled pack and each deploying organization's own approval are unchanged.

## Consequences
- Publishing is tied to a person, an organization and a claimed namespace, and every such act is
  on the registry's chain with the organization's id.
- The registry shares the engine's functions and database. A publish can hold a function for up
  to three minutes, and archives grow the database. Under the mount an archive is limited to
  the engine's request body limit; larger archives wait for the pack store (LP-20).
- Publisher bytes are served from the dashboard's origin, as JSON or as an attachment under a
  content security policy that loads nothing.
- Removing the administrator token breaks anyone who runs a registry with it. No hosted
  registry exists; a self-hosted one is told which command replaces each act.
- Nothing changes what a valid pack decides, and no pack's digest changes.

## Alternatives Considered
- **A separate project for the registry** (option B): its own database and sessions, with
  organizations mirrored from the engine. It isolates load and failure, at the cost of two
  account systems to keep in step. The owner chose A.
- **Two approvals**, one for the claim and one for the first publish. A claim alone gives a
  reviewer nothing to look at, so it only holds the name, capped per organization.
- **A plain reviewer role in any organization.** Any organization's administrator can grant any
  membership role, so every organization could approve itself.
- **Keeping the administrator token for operations only.** A shared secret that reviews and
  deletes is the same weakness with a smaller name; the operator's commands cover those acts
  with a reason on the chain.
- **A shipped key bound to a hostname.** It would put a production hostname in the tree.
