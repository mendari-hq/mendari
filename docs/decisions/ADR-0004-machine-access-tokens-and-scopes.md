# ADR-0004: Machines get scoped keys or short-lived SMART tokens; one scope language for both

## Status
Accepted (Phase 4 of the FHIR work, 2026-10-02). Amended by
[ADR-0011](ADR-0011-hidden-evaluation-sets.md) (LP-11d, 2026-10-09): one key of a role outside
the machine roles exists, an organization's grading key, which only the operator mints from the
command line (`mendari access grader-key`); it holds `grade` and nothing else, no HTTP route
makes one, and an administrator may list and revoke it, never rotate it.

## Context
People sign in with the organization's identity provider (OIDC). Machines used API keys that
carried one role and one organization, never expired, and could be of any role, admin included.
EHRs, integration engines and Da Vinci clients expect a different mechanism for machine access
to a FHIR server: SMART Backend Services. The client signs a short assertion with its own private
key, and the server's token endpoint exchanges it for a short-lived access token.

Two ways to offer that were considered: Mendari issues the tokens itself, or it accepts tokens an
organization's identity provider issues. A SMART server advertises one token endpoint in its
`.well-known/smart-configuration`, and identity providers differ in how they carry scopes and
roles. So accepting external tokens means one authorization server per deployment, with
per-provider handling.

## Decision
- **Mendari issues the tokens** at `POST /auth/token`: `client_credentials` with a
  `private_key_jwt` client assertion (RS384 or ES384, per SMART Backend Services and RFC 7523).
  It is **off unless a deployment turns it on**, and it refuses to start in open mode or without
  `MENDARI_PUBLIC_URL`.
- **Access tokens are opaque** (`mat_…`), stored hashed, valid for five minutes, with no refresh
  token. Each request reads the client's current role, scopes, organization and revocation, so a
  change applies at once. No signed JWT access tokens, so there is no signing key to hold or
  rotate.
- **Clients register a JWKS by value.** Mendari fetches nothing on a client's behalf.
- **One scope language for keys and tokens:**
  - FHIR reads use SMART v2 resource scopes for what is served: `system/Task.rs`,
    `system/AuditEvent.rs`, `system/Provenance.rs`, `system/CodeSystem.rs`,
    `system/NamingSystem.r`, and `system/*.rs`. SMART v1 forms map as SMART specifies.
  - Mendari's own actions use URI scopes on mendari.co: `https://mendari.co/fhir/scope/decide`
    (every decision entry point, `$decide` included) and `https://mendari.co/fhir/scope/outcome`
    (recording what happened downstream). `system/Task.c` and `.u` would tell a SMART client that
    `POST /fhir/Task` works; it does not (ADR-0003).
  - A scope only narrows: effective permissions = the role's ∩ the scopes'. A key or client with
    no scopes keeps its role's permissions.
- **Machines are `viewer` or `service`**, never `reviewer`, `governance` or `admin`. Review and
  approval are a person's accountability. Managing keys and clients is a people-only permission,
  so a stolen key cannot mint a credential that outlives it. Creating and revoking keys and
  clients is recorded in the hash chain.
- **Keys expire:** 90 days by default, 365 at most. Keys from before this decision expire 30
  days after it is deployed.
- **A bearer credential decides alone.** With an `Authorization` header there is no fallback to
  the session cookie. The credential's prefix decides whether it is a key (`ohk_`) or a token
  (`mat_`), and anything else is refused. Credentials are never accepted in a URL or a form.

## Amendment (2026-10-07, LP-12c): deciding implies discovering
Approved by the owner, in its narrow form ("usable packs only"). The decide scope implies a
read-only `discover` permission, with no scope URI of its own. It reaches exactly two routes,
`GET /api/packs` and `GET /cds-services`, and each lists only the packs the credential may decide
with: every pack this deployment serves from disk, and the caller's organization's promoted
pack store versions (amended by LP-20h, 2026-11: a version one organization promoted is never in
another's lists, decisions, CDS Hooks discovery or FHIR operation), with the summary people see of
each (its id, name, version and the like). A pack's detail, its examples and datasets, `/openapi.json` and the audit stay
closed to a scoped credential. Every role holds `discover`, so people and unscoped credentials
reach the lists as before; an EHR launch does not. `GET /api/auth/me` lists it.

## Consequences
- An EHR or integration engine that uses one decide-scoped key or token for discovery and
  invocation is no longer refused on discovery; EHR-signed CDS Hooks JWTs come with LP-23.
- An EHR or DTR client can use SMART Backend Services against Mendari once the deployment turns it
  on, and Mendari's own Bulk client (`standards/bulk.py`) can test the whole exchange end to end.
- Vercel Authentication no longer covers Production, so the token endpoint is reachable there,
  behind the firewall's per-address rule and the app's own limit (60 a minute per address).
- The mendari.co scope URIs are permanent once clients are configured, so the owner keeps that
  domain.
- Older admin keys stop managing keys at once, and stop working 30 days after deploy.

## Alternatives Considered
- **Accept an external authorization server's tokens.** Deferred until a customer names one.
- **Signed JWT access tokens (RFC 9068).** Rejected for now: a signing key to protect and rotate,
  and stale authorization until a token expires.
- **JWKS URLs.** Deferred: an outbound fetch on an unauthenticated request, and an SSRF and
  amplification surface.
- **Pack-scoped keys, last-used tracking, a new key format.** Deferred until external
  developers use the API. (Rotation with an overlap was added by LP-09, 2026-10:
  `POST /api/auth/keys/{id}/rotate`. A Backend Services client's key set is replaced in place by
  LP-12h, 2026-10: `POST /api/auth/clients/{id}/keys`, same client id, its tokens dropped and a
  token raced against the old set refused; no overlap, so a switch without a gap is two
  replacements.)
