# Deploying Mendari with real patient data

Mendari ships with synthetic data only. Before a pilot uses real protected health
information (PHI), set it up as below. This guide covers what Mendari itself does. Your
organization's own obligations (a BAA with your hosting provider, a risk assessment, your
privacy officer's review) come first.

## 1. Run it where your PHI is allowed to be

Run Mendari inside your organization's network or an approved cloud account:
- the engine's container image, or a per-pack image that serves only one pack
  (`docker build --build-arg PACK=mendari/patient-inbox-router .`, which sets
  `MENDARI_PACKS`);
- Postgres (not SQLite) for the audit store.

[`deploy/`](https://github.com/mendari-hq/mendari/tree/main/deploy) has two templates: docker compose for one server (set `MENDARI_IMAGE`
in its `.env`, pinned by digest), and Kubernetes. Both include the daily retention, checkpoint and monitoring jobs.

The public sandbox and the project's own Vercel deployment are **synthetic only**.

**Health checks and request size.** Two public routes, for two questions:

- *Is the process alive?* With sign-in on, `/api/health` answers without a credential with
  `status`, `version` and `sign_in` only, and does no database work, so it stays `ok` through a
  database outage. Use it for liveness (the Docker `HEALTHCHECK`, Kubernetes'
  `livenessProbe`): restarting a server never fixes its database. The settings (PHI mode,
  packs, providers, the FHIR servers Mendari calls, today's remote calls) are shown to someone
  signed in, or a key, that may read; in memberships mode today's calls are the operator's
  (`mendari usage`). In open mode (no sign-in) every caller gets the settings, which read the
  database, so there `/api/health` answers `500` while the database is down.
- *Can it serve decisions now?* `GET` or `HEAD /api/health/ready` reads Mendari's table once
  and reports the packs as start-up found them: `200` with `status` `ready`, or `degraded` while
  a pack did not load or build (signed-in `/api/health` lists it under `invalid`); `503`
  `unready` when the database does not answer within about 3.5 seconds or no pack is served. It
  names fixed codes only (`checks.database`: `ok` or `unreachable`; `checks.packs`: `ok`,
  `degraded` or `none`), never an error's text. Use it for readiness (Kubernetes'
  `readinessProbe`, a load balancer's target health) and for status pages and uptime checks.
  Each instance reads the database at most once every 5 seconds whoever asks and keeps the
  answer, a failure too. A read the database never answers (a network black hole, its
  connection left open) holds one thread, and none starts again until it returns; a caller
  that comes while a check runs gets the last answer at once. So it keeps answering through an
  outage, and a flood of it never takes the threads liveness needs. There is no per-address
  limit, which would fail a probe that shares its address with other callers (a kubelet behind
  SNAT, a load balancer's health checker): floods are your proxy's or firewall's, as for
  `/api/health`. A check that calls it more often than your database's idle suspend (Neon's
  default is 5 minutes) keeps the database awake. On Vercel, an instance that starts while the
  database is down fails before any route runs (the app opens its database at import), so a
  status page sees an error there, not a `503`. `/status` shows readiness to people, and
  [status checks](operations/status-checks.md) lists what an external status page should check
  (readiness, sign-in, a decision, the registry, the Hub) and how to give its decide check a key
  of its own.

A request body is 4 MiB at most: raise `MENDARI_MAX_BODY_BYTES` (in
bytes, 64 KiB or more) if your FHIR Bundles are larger, and give your proxy the same limit
(nginx's `client_max_body_size`, say). A larger body gets a `413`.

**Behind a proxy.** Serve Mendari over https only: its cookies are Secure, and any host but
`localhost` gets HSTS. The engine needs the client's address, not your proxy's (sign-in and the
token endpoint are limited per address, and the security log records it). `mendari serve`
takes `X-Forwarded-For` (`--proxy-headers`, on by default) only from the proxies in
`--forwarded-allow-ips` (or `FORWARDED_ALLOW_IPS`; default `127.0.0.1`): the address the
engine sees the proxy connect from, which is not always the one the proxy connects to. In the
compose template, a proxy on the host connects to `127.0.0.1:8000` but reaches the engine from
the compose network's gateway: the template fixes that network (`172.30.0.0/24`) and trusts its
gateway, `172.30.0.1` (change both if the subnet is taken on your host). In Kubernetes, set the
ConfigMap's value to your ingress controller's pod network. Signed in, `/api/health` shows
where the address is read (`rate_limits.client_ip_source`), the address seen on that very
request (`rate_limits.client_ip`) and whether `X-Forwarded-For` gave it
(`rate_limits.x_forwarded_for_used`): from a browser outside, it must be that browser's own
address. If it is the proxy's (a `172.x` or `10.x` address, say), everyone shares one sign-in
limit and the log cannot tell people apart. Limit per address at the proxy too, on `/auth/` and
`/smart/` only (a hospital sends everyone's traffic from one address):
[`deploy/onprem/nginx-limits.conf`](https://github.com/mendari-hq/mendari/blob/main/deploy/onprem/nginx-limits.conf)
is an example, at 120 a minute per address. If your people and EHRs all leave through one
address (NAT, a Citrix farm), size both for your busiest minute of sign-ins and EHR launches:
the proxy's rate and burst, and Mendari's own limit (§4). A proxy must not replace Mendari's
`Content-Security-Policy` header.

## 2. Sign-in and roles

Set your identity provider, so nobody reaches the dashboard or the API without an account:

```bash
MENDARI_OIDC_ISSUER=https://login.example.org/realms/clinic
MENDARI_OIDC_CLIENT_ID=mendari
MENDARI_OIDC_CLIENT_SECRET=...      # for a confidential client; from your secret manager
MENDARI_OIDC_ROLE_MAP="clinicians=reviewer,informatics=admin,ai-committee=governance"
MENDARI_OIDC_ORG_CLAIM=org          # only if one deployment serves several organizations
MENDARI_PUBLIC_URL=https://mendari.example.org
```

**With an identity provider that has no roles, such as Google**, map people one by one from
their email. Mendari gives a role to an email only when the provider says it is verified
(`email_verified`), and Google's token and key endpoints are on other hosts than its issuer, so
they are listed:

```bash
MENDARI_OIDC_ISSUER=https://accounts.google.com
MENDARI_OIDC_CLIENT_ID=1234567890-abc.apps.googleusercontent.com
MENDARI_OIDC_CLIENT_SECRET=...      # from your secret manager
MENDARI_OIDC_ENDPOINT_ORIGINS=https://oauth2.googleapis.com,https://www.googleapis.com
MENDARI_OIDC_ROLE_CLAIM=email
MENDARI_OIDC_ROLE_MAP="owner@example.org=admin,nurse@example.org=reviewer"
MENDARI_PUBLIC_URL=https://mendari.example.org
```

In Google Cloud Console, create an OAuth client of type *Web application* with the redirect URI
`https://mendari.example.org/auth/callback`. While its consent screen is in *Testing*, only the
test users listed there can sign in at all, which is a second gate.

**Organizations as records instead of claims.** `MENDARI_ACCOUNTS=memberships` (the default
is `claims`, everything above) gives each person the organization and roles of their
membership, managed with `mendari access` and by each organization's administrators; the role
and organization claims are then ignored, and someone with no membership acts for no
organization. Switch only after `mendari access check` passes and the owner is bootstrapped:
[the runbook](operations/accounts.md).

**Sessions.** A person's session ends after 30 minutes without a request and 8 hours after
signing in, whichever comes first, and when the browser closes (the cookie has no expiry of its
own; the server decides). Set your own policy within bounds; a value outside them stops the
server at startup:

```bash
MENDARI_SESSION_IDLE_MINUTES=30     # 5 to 120
MENDARI_SESSION_MAX_HOURS=8         # 1 to 12
```

Signed in, `/api/health` shows them (`sessions`). An EHR launch's session follows the same
rules and also ends when the EHR's token does, or as soon as the EHR answers `401` to it. More
about sessions:

- **Roles apply at once.** Each request works out the person's roles again from the claims kept
  at sign-in and the role settings as they are then, so taking a group out of
  `MENDARI_OIDC_ROLE_MAP` (and redeploying) ends that access on the next request, not when the
  session ends. A person left with no role gets a `401` and must sign in again. Changing
  `MENDARI_OIDC_ROLE_CLAIM` does the same to everyone whose role came from the old claim.
- **Cookies** are `__Host-mendari_session` (and `__Host-mendari_smart` for an EHR launch):
  HttpOnly, Secure, SameSite=Lax, for this exact host only, so another subdomain of your domain
  cannot set or replace them. On `localhost`, `127.0.0.1` and dotless hosts (a compose service)
  they keep their plain names, `mendari_session` and `mendari_smart`. Serve Mendari over https:
  browsers refuse Secure cookies on plain http anywhere but localhost.
- **A sign-in works only in the browser that started it.** `/auth/login` (and `/smart/launch`)
  set a ten-minute cookie (`__Host-mendari_signin`, `__Host-mendari_launch`) that the callback
  must bring back, so a callback link sent to someone else signs nobody in. A second sign-in
  started in another tab replaces the first, which then asks to sign in again. A script that
  signs in over plain http carries the cookies by hand, as `mendari sandbox pilot-check` does.
  That cookie belongs to one host, and the callback comes back to `MENDARI_PUBLIC_URL`, so a
  sign-in or launch started on any other name for the same deployment (a `*.vercel.app` alias,
  an internal name, an address) is first sent to `MENDARI_PUBLIC_URL` and starts there. It
  is sent once (the query then carries `moved=1`), so a proxy that rewrites `Host` cannot make
  it loop.
- **An EHR launch's rows are sealed, with no key to manage.** The launch's session row (the
  EHR's access token, the patient, the encounter) and its pending row (the PKCE verifier) are
  encrypted with AES-256-GCM under a key derived from the browser's own cookie, which the
  database never holds: a database reader, a backup or the database's history opens nothing.
  There is no setting and nothing to provision, rotate or back up, in every PHI mode. A row
  that does not open (changed, or written before this release) ends the launch: the clinician
  opens Mendari again from the EHR. No refresh token is kept, and `offline_access` in
  `MENDARI_SMART_SCOPES` refuses every launch (the server still starts). People's sign-in rows
  hold no token and are not sealed.
- **The dashboard says so when a session ends** ("Your session ended. Sign in again.") and
  brings the person back to the same view after signing in; what they were typing stays on the
  page.

**Who the audit chain names.** Reviews, ratings, approvals, outcomes, input reads and key and
client changes are chained under the signed-in person's **verified email** (the provider says
`email_verified: true`), else their identity provider's subject qualified by its issuer
(`https://login.example.org/realms/clinic|8f2c…`), never their display name. Keys are named
`key <id>` and Backend Services clients `client <id>`. Chained events are never rewritten, so
an email in the chain stays there; ask your identity provider to send verified emails (most
do with the `email` scope, which Mendari asks for) if you want readable names. In open mode
(no sign-in, synthetic data only) the client gives the reviewer, reporter and approver, and each
must be an identifier: no spaces, at most 100 characters.

Integrations (the EHR, CDS Hooks, a message worker) use API keys with the `service` role (or
`viewer`, to read only; review and approval are a person's, so no key has those roles). Admins
create them in `POST /api/auth/keys` while signed in (no key, an admin key included, may manage
keys); a key is shown once and can be revoked at any time. Send it as
`Authorization: Bearer ohk_…` only: Mendari refuses a credential in a URL or a form, where it
would end up in logs.

Give each key the least it needs:

- **Scopes** narrow a key's role to the routes they name, and a scoped key reaches nothing
  else. FHIR reads use SMART v2 scopes for what is served (`system/Task.rs`,
  `system/AuditEvent.rs`, `system/Provenance.rs`, `system/CodeSystem.rs`,
  `system/NamingSystem.r`, or `system/*.rs`); deciding, at every entry point (`$decide`, CDS
  Hooks, DTR, PAS and the REST routes), needs `https://mendari.co/fhir/scope/decide`, and
  recording an outcome `https://mendari.co/fhir/scope/outcome`. An EHR that only calls CDS
  Hooks services needs `{"role": "service", "preset": "cds-hooks"}` (below).
  Deciding implies finding what to decide with: a decide-scoped key also reads the two lists of
  the packs it may decide with, `GET /api/packs` and the CDS Hooks discovery list
  (`GET /cds-services`), and nothing else under `/api` (ADR-0004 as amended). For any other
  read, sign in, or use an unscoped key.
- **Expiry**: `days`, 90 unless asked, 365 at most. Rotate a key before it expires
  (`POST /api/auth/keys/{id}/rotate`, section 3): `GET /api/auth/keys` shows when each does.
- **Presets**: one per documented integration path, so a key gets what its path needs and no
  more. Send `preset` with the preset's `role`, and no `scope`: the key gets the preset's scope
  and, unless you send `days`, its expiry. `GET /api/auth/keys/presets` lists them with every
  route a key made from each reaches on your deployment (several reach the same routes: every
  decision entry point needs the one decide scope, so the name says what the key is for, not a
  boundary; `rest` is the whole `service` role, as no scope reaches Mendari's own audit reads).
  The preset is a label on the key: what the key may do is its role and scope as created, so a
  later release that changes a preset never changes a key you hold.

<!-- presets:start (generated from src/mendari/server/presets.py; a test keeps it equal) -->
| Preset | For | Role | Scope | Calls |
|---|---|---|---|---|
| `rest` | REST API | `service` | none: the role's own | `GET /api/packs`<br>`POST /api/packs/{namespace}/{name}/decide`<br>`GET /api/audit/{decision_id}` |
| `fhir` | FHIR in, FHIR out | `service` | `https://mendari.co/fhir/scope/decide system/AuditEvent.rs system/Provenance.rs system/Task.rs` | `POST /fhir/$decide`<br>`GET /fhir/Task`<br>`GET /fhir/Task/{resource_id}`<br>`GET /fhir/Task/{resource_id}/_history/{version_id}`<br>`GET /fhir/AuditEvent`<br>`GET /fhir/Provenance` |
| `ai-guard` | AI Guard | `service` | `https://mendari.co/fhir/scope/decide` | `POST /api/packs/{namespace}/{name}/guard` |
| `cds-hooks` | CDS Hooks | `service` | `https://mendari.co/fhir/scope/decide` | `GET /cds-services`<br>`POST /cds-services/{service_id}` |
| `da-vinci` | Da Vinci CRD, DTR and PAS | `service` | `https://mendari.co/fhir/scope/decide` | `POST /cds-services/{service_id}`<br>`POST /fhir/Questionnaire/$questionnaire-package`<br>`POST /api/packs/{namespace}/{name}/pas` |
| `outcomes` | Outcomes | `service` | `https://mendari.co/fhir/scope/outcome` | `POST /api/audit/{decision_id}/outcome` |
| `fhir-reader` | FHIR reader | `viewer` | `system/AuditEvent.rs system/Provenance.rs system/Task.rs` | `GET /fhir/Task`<br>`GET /fhir/Task/{resource_id}`<br>`GET /fhir/AuditEvent`<br>`GET /fhir/Provenance` |
<!-- presets:end -->

```bash
curl -sS -X POST "$MENDARI/api/auth/keys" -H "Cookie: <your session>" \
  -H 'content-type: application/json' \
  -d '{"name": "EHR CDS Hooks", "role": "service", "preset": "cds-hooks"}'
```

Creating, rotating and revoking a key are recorded in the audit chain (who, when, the role,
scope and expiry; never the key).

**Upgrading from a release without key expiry (before Phase 4).** The first start of this
release gives every existing key an expiry 30 days later, once (`GET /api/auth/keys` shows it).
Existing keys keep their role until then, even `reviewer`, `governance` or `admin`, but no key
may manage keys. Replace them with `viewer` or `service` keys, with scopes, before they expire,
and revoke any you no longer need now. A key written by an older instance still running during
the rollout has no expiry and is refused until the next start gives it one.

**EHR launches (SMART on FHIR, optional, off by default).** With sign-in on, a clinician the
EHR launches Mendari for is named by the EHR's own sign-in, in the organization you register
for that EHR's FHIR base, and may view the patient, or view and decide, as you register it:

```bash
MENDARI_SMART_CLIENT_ID=<the client id the EHR gave Mendari>
MENDARI_FHIR_ALLOWED_SERVERS=https://ehr.example.org
MENDARI_SMART_EHRS=https://ehr.example.org/fhir/r4=default:decide   # <FHIR base>=<org>:<view|decide>
```

- **Register the organization your people's sign-in gives them**: the launch's decisions,
  HUMAN_REVIEW ones included, are that organization's, and only its reviewers read them. With
  `MENDARI_OIDC_ORG_CLAIM` unset (one organization), everyone who signs in is in `default`, so
  register `default`, as above; with it set, register the claim's value for the organization
  the EHR serves. In memberships mode, register an organization with a record: a launch of one
  without is refused. An organization nobody signs in to fails closed: the decisions are recorded,
  but no reviewer ever sees them. With sign-in on, `mendari smart check` fails such a
  registration (in memberships mode it warns instead, since a launch needs the organization's
  record, which it does not read, and fails only an id no record can have).
- **Register the base exactly as the EHR sends it** in the launch's `iss` (scheme and host in
  any case, a trailing slash ignored, the path as it is), then check it with the deployment's
  settings: `mendari smart check https://ehr.example.org/fhir/r4` prints the registration key,
  whether its origin is allowed and how it is registered (with sign-in on, `fail` when it is
  registered to an organization other than `default` and `MENDARI_OIDC_ORG_CLAIM` is unset;
  `warn` in open mode; in memberships mode `warn`, and `fail` for an id no record can have),
  the SMART client
  and scopes, and the EHR's discovery, PKCE S256 and keys, each `ok`, `warn` or `fail` (exit 1
  on any `fail`). It
  reads only the EHR's public configuration and keys. Two tenants on one EHR host are two
  bases, so two organizations; one base serving several organizations is not supported.
  Register only an EHR whose sign-in you trust: its id_token is what names the clinician.
- **What is refused, at the launch, never at startup:** a base not registered (before anything
  is sent to it), scopes without `openid fhirUser` in `MENDARI_SMART_SCOPES` (the default has
  both), an id_token without a subject, and a user who is not a Practitioner or
  PractitionerRole (a patient portal's). The security log says which (`smart.launch.refused`,
  reason `unregistered`, `configuration`, `id_token` or `user_type`). Only a malformed
  `MENDARI_SMART_EHRS`, or a base whose origin is not on `MENDARI_FHIR_ALLOWED_SERVERS`, stops
  the server. A launch made while sign-in was off (none of these checks ran) ends when sign-in
  is turned on: the clinician launches again.
- **A launch reaches only its own routes** (the patient, the items to decide about, and
  deciding when registered to), never the review queue, the audit trail or packs, and its
  decisions are the registered organization's, limited per launched clinician. Your reviewers
  read them in the audit trail as usual. The dashboard shows the clinician the patient view
  alone, with each decision in place and its id to quote.
- **Launch in a new window or the EHR's embedded window**, not an iframe (an iframe launch fails
  with `browser`). One browser holds one launch at a time: a later launch ends the earlier
  one, a window whose launch a later one replaced says so and shows no patient (the server
  refuses its reads with `409`), and a launch that ended (its token, idle, or the EHR's `401`)
  says to open Mendari again from the EHR.
- **The registration is read at startup**, and every request checks the launch against it:
  take an EHR out of `MENDARI_SMART_EHRS` (or register it to `view`) and restart or redeploy,
  and its live launches lose that access from then on. Signing out of Mendari ends the
  browser's launch too.
- Signed in, `/api/health` lists your organization's registrations (`smart_ehrs`): an empty
  list there means the EHR is registered to another organization than yours.

**SMART Backend Services (optional, off by default).** For an EHR, an integration engine or a
Da Vinci client that expects SMART Backend Services instead of an API key, turn on Mendari's
own token endpoint. It needs sign-in (above) and the public URL, or the server refuses to start:

```bash
MENDARI_BACKEND_SERVICES=on
MENDARI_PUBLIC_URL=https://mendari.example.org   # assertions are addressed to its /auth/token
```

Then an admin, signed in, registers each client with its **public** JWK Set (the client keeps
its private key; Mendari fetches no key from a URL), a machine's role and the scopes it may be
granted (the same scopes as keys; left out, everything the role can use):

```bash
curl -X POST https://mendari.example.org/api/auth/clients \
  -H 'cookie: __Host-mendari_session=…' -H 'content-type: application/json' \
  -d '{"name": "ehr-crd", "role": "service",
       "scope": "https://mendari.co/fhir/scope/decide system/Task.rs",
       "jwks": {"keys": [{"kty": "RSA", "kid": "ehr-2026", "n": "…", "e": "AQAB"}]}}'
```

The answer gives the `client_id` and the `token_endpoint`. Keys are RSA of 2048 bits or more
(RS384) or EC P-384 (ES384), each with its own `kid`; a JWK Set holding a private or symmetric
key is refused (treat that key as exposed). The client finds the token endpoint at
`/fhir/.well-known/smart-configuration`, signs an assertion (`iss` and `sub` its `client_id`,
`aud` exactly `https://mendari.example.org/auth/token`, `exp` at most five minutes ahead, a new
`jti` each time), and posts `grant_type=client_credentials` with it and a `scope`. It gets a
five-minute token (`Authorization: Bearer mat_…`) for what it asked within what it was
registered for, in the organization of the admin who registered it. There is no refresh token,
no authorization endpoint (no user or patient launch) and no `patient/` or `user/` scope.

- `GET /api/auth/clients` lists clients with their keys' ids and thumbprints and when their
  keys were last replaced; `DELETE /api/auth/clients/{client_id}` revokes one, and its tokens
  stop working on the next request. Registering, replacing keys and revoking are recorded in the
  audit chain (keys by id, type and thumbprint, never their material).
- `POST /api/auth/clients/{client_id}/keys` with `{"jwks": {"keys": [...]}}` replaces a client's
  public keys whole, checked as at registration: same client id, role and scopes. The tokens it
  holds stop at once, and a token issued against the old keys while the replacement ran is
  refused too. There is no overlap: to switch without a gap, replace with the old and new keys,
  move the client to the new one, then replace with the new one alone. A revoked client is a
  `409`; another organization's, a `404`. Its daily cap and rate limits carry across.
- `GET /api/auth/clients/{client_id}/history` (and `GET /api/auth/keys/{key_id}/history` for an
  API key) reads its audit chain: registered, each replacement, revoked, with who did each.
- The dashboard's Access view does all of this for an administrator who signed in (the
  **Clients** tab, beside **API keys**), in claims mode too; it turns a pasted PEM public key
  into a JWK and refuses the private keys it recognises (a PEM `PRIVATE KEY` block, a JWK with
  private members, a private key's DER) before anything is sent.
- To change a client's scopes, register a new client and revoke the old one.
- The token endpoint takes 60 requests a minute per client address. After 10 failed
  assertions in 10 minutes from one address for one client, that pair gets a `429` until the
  oldest failure has aged out; another client, or the same client from another address, is not
  affected (never by the client alone: anyone who knows a client's id could lock it out).
- A deployment's tokens and one-time `jti` claims live in its database. Don't share a database
  (and a public URL) between preview and production.

**Requests from other sites.** A browser request another site starts reaches only public
routes and pages, and only to read: a link to the dashboard works, while a link to an API
route, or a form on another site that posts here, gets a `403` ("cross-site request refused"),
whether or not the browser sends the session cookie with it (it does not, for another site's
form: the cookie is SameSite=Lax), so another site cannot sign anyone out.
This site is the address the request was sent to, the origin of `MENDARI_PUBLIC_URL`, and any
alias you list. If people reach Mendari through a name that a proxy rewrites before it gets
here (so the request arrives for another host), list that name, or the dashboard's own buttons
get that `403`:

```bash
MENDARI_TRUSTED_ORIGINS=https://mendari.example.org,https://decisions.example.org   # origins only
```

A value that is not exactly an origin stops the server at startup. Scripts with a session
cookie and no `Origin` or `Sec-Fetch-Site` header (curl, the pilot check) are not affected. A
refusal is logged as `auth.origin.refused` (§7), with a reason code and the request id.

Sessions and pending sign-ins are server-side rows keyed by their kind and value. A release
that changes how they are keyed (Phase 4 did) ends every session: people sign in again, and an
EHR launches the app again. Phase 4b signs nobody out: sessions from before it keep their
8 hours (and take up the idle limit at their next request), their old cookie name is still read
for them for one release, their roles are the ones they signed in with, and a sign-in or launch
in flight at the deploy completes once without the browser cookie.

The release that seals EHR launch rows (ADR-0006) signs nobody out either: people's sessions
and sign-ins are untouched. An EHR launch in flight at the upgrade, or a launch session from
before it, does not open and ends: the clinician launches once more from the EHR. Rolling back
past that release leaves rows the older code cannot read, which breaks only `/api/smart/*`;
delete them after the rollback, `DELETE FROM smart_sessions WHERE kind IN ('session',
'pending')`, and EHR launches start again.

## 3. PHI mode

```bash
MENDARI_PHI_MODE=encrypt
MENDARI_PHI_KEY=$(python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())")
MENDARI_INPUT_HASH_KEY=$(python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())")
MENDARI_RETENTION_DAYS=90
```

| Mode | What is kept of each input |
|---|---|
| `off` | As the pack says (`audit.store_input`). For synthetic data only. |
| `hash` | Nothing but a keyed hash of it (HMAC-SHA256). Reviewers see the decision and its reasons, not the message or record. |
| `encrypt` | The input, encrypted (AES-256-GCM, bound to its decision), apart from the audit chain. |

**A protected database stays protected.** Once a server or command opens a database under
`hash` or `encrypt`, the database is marked so (`settings`, `phi_protected`). From then on a
server under `off` refuses to start on it, and a command under `off` (`mendari decide --db`, a
note) refuses to keep input text in it: run it with the deployment's mode and keys.

**Sign-in means `hash` or `encrypt`.** With sign-in on (`MENDARI_OIDC_ISSUER` and
`MENDARI_OIDC_CLIENT_ID`), the engine refuses to start under `off`, whether written out or left
unset, on SQLite and Postgres alike: people bring other people's cases, and an input kept in
plaintext can never be erased without retiring the database (below). `off` is for a local sandbox
without sign-in. A signed-in sandbox on synthetic data runs `encrypt` with two keys generated for
it (the compose sandbox's `signin` profile, in the README, reads them from your shell or `.env`).

**Choosing is required for the EHR launch.** With the EHR launch on and sign-in off
(`MENDARI_SMART_CLIENT_ID`: a launch fetches the patient's record), the engine refuses to start
until `MENDARI_PHI_MODE` is set: `encrypt` or `hash` for anything real, `off` written out only for
a local sandbox on synthetic data (the engine logs a warning; this case is not refused yet). On
Vercel (Production and Preview alike; Vercel's entrypoint,
the root `app.py`, says so even when the project hides Vercel's system variables) it refuses
`off` itself: its mode is `encrypt` unless set to `hash`, so set the keys for both environments
before deploying (an unset key stops it, naming both).

What the decision record (whose digest is chained for good) keeps of an input, in `hash` and
`encrypt`:
- **no evidence values.** An answer's evidence can quote the input (`4548-4 8.1 % on
  2026-08-01`). Under `hash`, and for a pack whose `audit.store_input` is `hash`, the record's
  evidence is empty; under `encrypt` it is sealed with the input (bound to its decision), comes
  back with it to those who may read inputs, and is deleted with it by retention. The caller's
  own answer (`/decide`) still carries it;
- **no resource ids.** An input issue (a US Core finding) names its resource by type and
  position, `Patient (entry 2)`, in the record and in the reasons that refuse an input; the
  OperationOutcome returned to the caller keeps the id, to find the resource;
- **a keyed input hash.** `hmac-sha256:<key id>:<64 hex>`, an HMAC-SHA256 under
  `MENDARI_INPUT_HASH_KEY`. A plain SHA-256 of a short or structured input (an age, a sex and an
  order code) is found by guessing in seconds, and anyone who may read a decision sees its hash;
  without the key, guesses match nothing. The engine refuses to start in `hash` or `encrypt`
  without this key, and so does `mendari decide --db`, which records a decision under the PHI
  mode set where it runs (set the deployment's). In `off` mode without it, the hash stays the
  plain `sha256:` of earlier releases, as on records already chained.

Under `encrypt`:
- only signed-in **reviewers** and **admins** get an input back: in a decision's record, the
  review queue and the feedback list. Every input shown is written to the hash chain
  (`input.read`, with who);
- viewers, services and governance see that an input exists, not what it says;
- the audit trail's list never carries inputs;
- a decision recorded under `off`, before the switch, kept its input (and its evidence) inside
  its chained record, and every record written before 2026-10-02 kept its evidence there, in any
  mode. Under `hash` and `encrypt` these are shown as a sealed input is: to reviewers and admins
  only, each read chained. Nothing can delete them without breaking the chain, so retention and
  erasure leave them (those older records' input issues and reasons can also name a resource by
  its logical id, which is withheld the same way). Choose one: keep that database, read-gated as
  described, or retire it: note the chain's head hash (`mendari audit verify`), keep an ids-only
  FHIR export if you want the history (`/api/audit/export/fhir`), and start a new, empty database
  under `encrypt`;
- **without sign-in, inputs stay sealed for everyone**, since there is no one to log a read
  against. Encrypt mode needs step 2.

**Retention.** Schedule `mendari phi purge --db "$DATABASE_URL"` (daily, for example). It
deletes kept inputs and erases notes older than `MENDARI_RETENTION_DAYS` (30 unless set;
1 to 36500), in batches of 500, and records each deletion in the chain, then a summary
(`retention.purged`: how many, after how many days, and whether the schedule or the command ran
it). Decisions, reviews and the chain itself stay, and `mendari audit verify` still passes.
`--days 0 --yes` deletes and erases everything kept; no setting can ask for that, so a typo in a
scheduled job never does.

**Checkpoints.** Schedule `mendari audit checkpoint --db "$DATABASE_URL"` daily too, after the
purge (the `deploy/` templates do). It verifies the chain from its last checkpoint (the whole of
it, the first time) and, when it holds, records the head as the next checkpoint, one row outside
the chain; it migrates the database as every command that writes does. `mendari audit verify
--since-checkpoint` then reads only the events after the last one, and a full `mendari audit
verify`, which reads everything, also checks that every checkpoint still names its event. A
checkpoint is in the same database as the chain, so keep a copy of the head somewhere else too,
and run a full verification now and then.

**The signed checkpoint.** Set `MENDARI_CHECKPOINT_KEY` (32 random bytes in base64,
`openssl rand -base64 32`; an Ed25519 key's seed) and each day's first checkpoint is also signed:
a statement `{format, date, head, key_id, signature}` that the chain's head was that hash on that
local day, with no position and no count. The server publishes the newest at
`GET /api/audit/checkpoint`, public, with the key's public half; publish the public key somewhere
of your own too, and keep the statements (anyone can). Later, `mendari audit verify --against
<statement.json> --public-key <key>` checks the signature, then the whole chain, and fails
(`reason: statement`) if the chain no longer holds that head: rewritten from before it, or cut
short of it, whatever the database itself says (`--against` reads the route's answer as it is
saved, or the statement alone). On a server the scheduled route `GET /api/internal/checkpoint`
(with `CRON_SECRET`) records and signs, as the command does. A chain that does not verify
records and signs nothing and goes to `MENDARI_ALERT_WEBHOOK` (`audit.broken`); so does a day's
stored statement that is not this key's for that day (someone with the database put it there),
which is never published. On Vercel nothing calls that route until a `crons` entry names it,
which is the owner's to add (the hosted schedule); until then run `mendari audit checkpoint`
with the key. Keep the key as a secret: whoever holds it can sign a statement for any head. To
rotate it, set the new one and run the checkpoint: it signs the day's statement anew, and only
statements the current key signed are published, so keep the old public key published too, for
the statements it signed.

On Vercel the schedule is in `vercel.json` (`crons`: daily at 08:00 UTC, 4 am Eastern in summer
and 3 am in winter). Set `CRON_SECRET` (at least 16 random characters) for Production: Vercel Cron sends it as a bearer token to
`/api/internal/retention`, which purges as the command does and answers `401` to anything
else. Without `CRON_SECRET` (or with one too short, or with spaces around it, which the server
ignores with a warning rather than refusing to start) the route does not exist and nothing is
purged; `/api/health`
shows `retention_days`, `retention_scheduled` and `last_purge` (the last summary, with when) to
those who may read.

**Erasing one decision's input.** On request, or for a case recorded by mistake:

```bash
mendari phi erase --db "$DATABASE_URL" --decision <id> --reason request --by dpo@example.org
```

or, as an admin, `POST /api/audit/{id}/erase` with `{"reason": "request"}` (or `"error"`). It
deletes the decision's sealed input and evidence and erases its notes, and chains, with the
reason and who, `input.erased` when it held a sealed input and one `note.erased` per note. The
record and the chain stay, and `mendari audit verify --notes` still passes. What the chain itself holds (see
above) stays: the answer's `still_in_chain` says so, and a decision that keeps nothing else is
refused (`422`).

**The keys.**
- Keep `MENDARI_PHI_KEY` and `MENDARI_INPUT_HASH_KEY` in your secret manager, never in the
  repository or an image. They are two different keys (the engine refuses one used as both): the
  input hash key is needed in `hash` mode, which has no PHI key, and rotating the PHI key leaves
  hashes alone.
- Losing the PHI key makes stored inputs, evidence and notes unreadable. The decisions and the
  chain are unaffected.
- To change either key, see **Rotating the keys** below.
- The input hash key's id (16 hex digits of an HMAC under it, which reveal nothing about it) is
  in every keyed hash and in `/api/health` (`input_hash`).

**Rotating the keys.** Each of the three changes without losing data or breaking the chain.

*The PHI key* (`mendari phi rotate`). Every kept input, its evidence and every note is re-sealed
under the new key in batches, each recorded in the chain (`phi.rotated`: key ids and counts,
never a key); decisions and the chain itself are never touched. In this order:

1. Make the new key (`python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`)
   and keep it in your secret manager beside the old one.
2. **Reader first.** Set `MENDARI_PHI_READ_KEYS=<new key>` for Production and Preview (and every
   deployment that shares the database) and redeploy. Every instance can now open what the new
   key will seal; nothing seals with it yet. From here on, new values name their key
   (`v2:<key id>:…`): never roll back below this release.
3. **Swap.** Set `MENDARI_PHI_KEY=<new key>` and `MENDARI_PHI_READ_KEYS=<old key>`, and redeploy.
   `/api/health` (signed in) shows the new key's id as `phi_keys.current`. Delete or protect the
   deployments built before the swap (on Vercel, never Instant Rollback to one): once the
   rotation begins they refuse to seal with the old key, so their decisions fail rather than
   leave values behind.
4. **Re-seal.** From a machine that reaches the database, outside the daily purge (08:00 UTC),
   with a `0600` file holding `MENDARI_PHI_MODE=encrypt`, `MENDARI_PHI_KEY`,
   `MENDARI_PHI_READ_KEYS` and `MENDARI_INPUT_HASH_KEY` as deployed:

   ```bash
   mendari phi rotate --db "$DATABASE_URL" --to <phi_keys.current>
   ```

   An interrupted run is run again the same way: what is done stays done. A value no key opens
   is left as it is and listed (exit 1): add the key it names to `MENDARI_PHI_READ_KEYS`, or
   erase it (`mendari phi erase`), and run again.
5. **Check.** `mendari phi rotate --db "$DATABASE_URL" --check` opens every value with the new
   key alone, recomputes each input hash made under the current hash key and checks each note
   against its commitment. `"ready": true` (exit 0) means nothing needs the old key. A key in
   `read_not_retired` had no value to move, so it was not retired: make sure no deployment still
   seals with it (step 3) before you drop it. Then `mendari audit verify --notes`.
6. **Drop the old key.** Remove `MENDARI_PHI_READ_KEYS`, redeploy, delete the file from step 4,
   and run the check again a day later. Keep the old key in the secret manager until every
   backup and the database's point-in-time history from before step 4 have expired: they still
   hold values sealed under it.

With Neon preview branches, each branch is a copy: run steps 4 and 5 on any branch a deployment
still uses, or recreate the branches afterwards.

*The input hash key.* Set the new `MENDARI_INPUT_HASH_KEY` and redeploy. The first record
hashed under it starts a new epoch, chained (`input_hash.epoch`, with the key id before it).
Hashes are never re-keyed: a keyed hash cannot be recomputed without its input, and the chain
holds each record's digest. Records keep the key id they were hashed under; hashes from two
epochs never match, and nothing in Mendari compares them. `mendari phi rotate --check` lists the
epochs (key id, first and last decision, how many). The old key may be destroyed once nothing
needs to recompute an old hash (nothing in Mendari does). If a key leaks, whoever also holds the
database can attack the hashes made under it: switch at once, and treat those hashes as no
better than plain SHA-256.

*API keys.* `POST /api/auth/keys/{id}/rotate` (a signed-in admin) makes a new key with the same
name, role and scope, shown once, and lets the old one work for `overlap_hours` (24 unless asked,
168 at most, never past its own expiry); then the old key is refused, as an expired key is.
Move the machine to the new key within the overlap: the security log's `auth.key.replaced_used`
(once per instance) shows a client still on the old one. A leaked key: rotate it with
`{"overlap_hours": 0}`, or revoke it (`DELETE /api/auth/keys/{id}`). The chain has both
(`key.created` with `replaces`, `key.rotated`). If the answer carrying the new key is lost,
revoke the replacement (`GET /api/auth/keys` lists it as the old key's `replaced_by`) and rotate
the old key again. The dashboard's Access view (API keys tab) rotates, revokes and shows a key's
history too.

*Backend Services clients.* A client keeps its id and replaces its keys
(`POST /api/auth/clients/{client_id}/keys`, or **Replace keys…** in the Access view's Clients
tab). There is no overlap, so switch in two replacements: first the old and the new public keys
(both sign), then move the client to its new private key, then the new public key alone (the old
one is refused from then). Each replacement drops the client's tokens, so the client asks for a
new one; the chain has each step (`client.keys_replaced`, with the keys before and after by
thumbprint and how many tokens were dropped). A leaked private key: replace with the new key
alone at once, or revoke the client.

**Notes.** A reviewer's or a rater's note, and a payer's answer to a prior authorization (its
authorization number and disposition), are free text. They are kept in a `notes` table, never in
the audit chain:
- the chain keeps `note: null` and a `note_ref`: the note's id and a salted SHA-256 commitment.
  The salt is only in the note's row, so the commitment says nothing about the text;
- under `encrypt` a note is sealed with the same key as inputs (AES-256-GCM, bound to the note
  and its decision). Like inputs, it opens only for **reviewers** and **admins**, and each read
  is chained (`input.read` names the notes read);
- under `hash`, a review or rating with a note is refused (`422`): `hash` keeps no free text.
  The dashboard offers no note field there, and a payer's words are not kept;
- in every mode, the chain keeps a payer's outcome and review actions only as codes (R4's
  `queued`, `complete`, `error` or `partial`; the payer's X12 306 code, 1 to 3 capital letters
  or digits): text in their place is dropped;
- under `off`, notes are kept in the clear for whoever may read the decision;
- notes written into the chain by earlier releases stay there (the chain is never rewritten),
  but are shown only to those who may read inputs.

`mendari phi purge` also erases notes older than `MENDARI_RETENTION_DAYS`. To erase one note now:

```bash
mendari notes erase --db "$DATABASE_URL" --id <note_ref.id> --reason request --by dpo@example.org
```

Erasing keeps the note's row (id, decision, when) and drops its text and salt. It chains
`note.erased`, with the reason and who. The chain still verifies, and
`mendari audit verify --notes` (with `MENDARI_PHI_KEY` set) checks every kept note against its
commitment. **Erasure is not instant everywhere:** database backups, your database's
point-in-time history (Neon keeps deleted rows for its history window) and exports made earlier
still hold the note until they expire or you delete them.

## 4. Limits and quotas

**Rate limits.** With sign-in on, Mendari limits what costs something, counted in the database
so every replica shares the count. Reads are not limited. Over a limit the answer is a `429`
with `Retry-After` (seconds), before any body is read, model called or record written: JSON on
`/api`, an OperationOutcome (`throttled`) on FHIR routes, OAuth's `temporarily_unavailable` at
the token endpoint.

| What | Counted per | Default |
|---|---|---|
| Signing in (`/auth/login`, the SMART launch) | client address | 300 a minute, 120 at once |
| The token endpoint, before its body is read | client address | 60 a minute |
| Failed assertions at the token endpoint | client address and client | 10 in 10 minutes |
| Decisions, at every entry point (REST, FHIR `$decide`, CDS Hooks, DTR, PAS, SMART); each draft order a CDS Hooks order hook decides is one | API key, client or person | 600 a minute, 100 at once |
| The AI guard (an LLM call) | API key, client or person | 60 a minute |
| Evaluations | caller | 6 an hour |
| Exports (the audit FHIR export, the feedback export) | caller | 10 an hour |

`MENDARI_RATE_LIMITS` is `on` (the default with sign-in), `log` (count and log what would be
refused, refuse nothing) or `off` (the default in open mode), optionally followed by overrides:
`MENDARI_RATE_LIMITS=on,decide=1200/min:200,evaluate=20/h` (`count/[n]unit[:burst]`, unit `s`,
`min` or `h`). A bad value stops the server at startup. Signed in, `/api/health` shows the mode,
every limit, `client_ip_source` and the address it saw for you. The client address must be
right (§1, *Behind a proxy*).

**Sign-in behind one address.** Sign-in and the SMART launch are counted per client address,
because nobody has signed in yet. A hospital usually sends everyone from one egress address,
so 300 a minute with bursts of 120 is shared by every clinician signing in and every EHR launch
there. Size it from your busiest minute (a shift change, a clinic opening), with room to spare,
and raise your proxy's per-address limit to match: for example
`MENDARI_RATE_LIMITS=on,signin=1200/min:400`. Never set it below the proxy's or the firewall's
own per-address limit. Over the limit people see "Too many sign-ins from your network" and can
retry after the seconds shown; anyone else behind that address can use it up too, so keep it
generous rather than tight.

**CDS Hooks order hooks.** An `order-select` or `order-sign` request decides each draft order
the pack is about, each one a decision against the decide limit, all counted before the first is
made. One request decides at most 50 (or the decide burst, if you set it lower); more is a
`400`, and the EHR sends them in several requests.

**Daily quotas per organization.** `MENDARI_ORG_DAILY_LIMITS=decisions=5000,jev=2000,llm=200`
caps what each organization uses a day: decisions, and the remote model calls they make. Unset,
there is none, and each one left out has no limit. An organization's evaluations (run on the
server) count against their own quotas, `eval_jev` and `eval_llm`
(`...,eval_jev=100,eval_llm=20`), never the decisions' ones. They still spend the deployment's
daily budgets (`MENDARI_JEV_DAILY_LIMIT`, `MENDARI_LLM_DAILY_LIMIT`), which live decisions share:
once those run out, every organization's live decisions go to people (`budget_exhausted`) until
midnight. So set each evaluation quota well below them (the server warns at startup when one is
not), and the quotas of all organizations together below them too. Over an evaluation quota the
evaluation's remote calls fail (`quota_exhausted`) and its report shows it. Over a quota a decision is still
made and recorded, but no provider runs and it goes to the review queue (`ABSTAIN` or
`HUMAN_REVIEW`, with `quota_exhausted` in its errors), as over a daily budget: a quota never
makes anything more automatic. The first time an organization reaches a quota each day,
Mendari logs it and posts one alert to `MENDARI_ALERT_WEBHOOK` (the organization, the quota and
its limit; no case data). Quotas start over at local midnight (`MENDARI_TIMEZONE`).

**A daily cap per principal.** `MENDARI_PRINCIPAL_DAILY_LIMIT=1000` caps the decisions each
principal asks for a day: a person, an API key, a Backend Services client, an EHR launch. Each
request that decides counts one, and a CDS Hooks order hook one per draft order, whatever the
answer; acknowledging the notice, a PAS submission and a DTR package do not count, and neither
does a request the rate limits refuse. Unlike a quota, it refuses: past it the request is
answered `429` with `Retry-After` (until local midnight) before any work, so one leaked key, or
one runaway integration, cannot spend its organization's day. Like a rate limit, the refusal
covers every message, an emergency included: an integration must send what Mendari refuses to a
person, as it must for any error. Unset, there is none; it needs sign-in (open mode has no
principals). `/api/health` shows a principal that may read its own cap and what it used today,
and each instance logs the first refusal of the day per principal (`quota.reached`, reason
`principal_decisions`).

**What was used: the usage ledger.** Each decision's remote model calls (Jev, an LLM) are counted
in the database in the decision's own transaction, by local day, organization, provider type and
purpose: `decision`, or `evaluation` for an evaluation run on the server (or `mendari eval
--db`), counted for the organization that ran it. Each row holds the calls sent, how many of
them answered with a price, the tokens the answered ones reported, and the cost of the priced
ones. A cost is shown only when every call was priced (Jev's published price, unless a variant
sets another, at most $1 per 1,000 tokens; an LLM's when its gateway reports one, believed up to
$1,000 a call); otherwise it is unknown, never a partial sum and never zero. A call that a
budget, a quota, the deadline or the request's size stops is never sent, so it is not counted; a
call sent that then failed, or whose answer was refused, is counted, without a price and without
tokens. The ledger holds no case data. Providers never write to the database. An
organization's administrators see theirs in the dashboard's Access view (**Usage**) and at
`GET /api/usage?days=30` (1 to 366 local days, with totals by provider and purpose); the operator
sees every organization's with `mendari usage --db "$DATABASE_URL" --days 30` (`--org` for one).

**A deadline per decision.** A decision's remote calls (Jev, an LLM, the CQL engine) share one
wall-clock deadline, `MENDARI_DECISION_DEADLINE_S` (whole seconds, 10 to 270; 120 unless set):
a call that would start after it is not made (no key fetched, no budget used), and one under
way, its retry included, waits only for what remains. Either is recorded as `timeout`, so the
decision goes to a person; the local providers still answer, so a keyword safety screen still
flags a possible emergency. Keep it below your platform's limit on a request, with room for the
audit write: on Vercel, `maxDuration` is 300 s (`vercel.json`). A request that makes several
decisions (a CDS Hooks order hook's draft orders) gives each its own deadline.

**At the edge.** A per-address limit in front of Mendari takes floods before they reach it, on
`/auth/` and `/smart/` only: your proxy (§1), or on Vercel one firewall rule, 120 requests a
minute per address
([`deploy/vercel/`](https://github.com/mendari-hq/mendari/tree/main/deploy/vercel), with its
runbook). Start such a rule in log mode, read what it would have refused for 7 days, then
enforce it. Never limit `/api` or `/fhir` per address.

## 5. What leaves your network

By default, nothing. Each of these is off until you configure it:

| What | Setting | Leaves with it |
|---|---|---|
| Jev | Jev's settings (`JEV_API_URL`, `JEV_MODEL` and `JEV_API_KEY`; `JEV_CONNECTOR` works on Vercel only): the inbox, referral and prior-auth packs then run Mendari + Jev in every slot `MENDARI_PROVIDERS` chooses nothing for | The pack's declared `state_fields` only (a pack you own that declares none sends the whole input; a community or registry pack must declare them) |
| Other remote models (LLMs through Vercel AI Gateway) | `MENDARI_PROVIDERS` | The pack's declared `state_fields` only (as for Jev) |
| Reading your FHIR server (CDS Hooks fetch, SMART, Bulk) | `MENDARI_FHIR_ALLOWED_SERVERS` | Nothing leaves; Mendari reads from servers you list |
| Alerts | `MENDARI_ALERT_WEBHOOK` | Pack ids and alert messages; an organization's id when it reaches a daily quota (no case data) |
| Shared evaluation reports | `mendari eval --share` (manual) | Metrics only (`mendari.site-report/1`, checked by the registry): no cases, ids, dataset name, approver or subgroups; `--share-out FILE` writes it to read first |
| Da Vinci PAS submissions (`submit` on `POST /api/packs/{pack}/pas`) | `MENDARI_PAS_PAYER_URL`, also on `MENDARI_FHIR_ALLOWED_SERVERS` | The PAS request Bundle: the patient, Coverage, payer, requesting organization and order the request gives, with its codes |

**Da Vinci PAS codes.** A PAS request carries X12 codes, and Mendari ships none of their values
(X12's code lists are under X12's terms; the owner's decision of 2026-10-07). Set your own, from
the X12 implementation guide you are licensed for and as your payer expects them. A request (or
`mendari pas build`'s flags) may give its own, and a service type in the order wins over the
setting:

| Setting | X12 element | In PAS |
|---|---|---|
| `MENDARI_PAS_REQUEST_TYPE` | 1525, each item's request type | Required: without it (or `request_type`), building the request is refused (`422`) naming the setting |
| `MENDARI_PAS_CERTIFICATION_TYPE` | 1322, each item's certification type | Required, as above (`certification_type`) |
| `MENDARI_PAS_SERVICE_TYPE` | 1365, the service type, when neither the request nor the order's category gives one | Required, as above (`service_type`) |
| `MENDARI_PAS_SELF_RELATIONSHIP` | 1069, the Coverage's code for self, beside FHIR's `self` | Optional: unset, it is left out, and the Coverage names the patient as its subscriber (PAS's rule `self-beneficiary`) |
| `MENDARI_PAS_PAYER_ENTITY`, `MENDARI_PAS_REQUESTER_ENTITY` | 98, the payer's and the requesting organization's entity codes | Optional: unset, the organizations go untyped |

Each is 1 to 3 capital letters or digits; anything else is refused naming the setting. The
sandbox's mock payer answers with `MENDARI_SANDBOX_PAYER_REVIEW_ACTION` (an X12 306 code), or
with no review action code when it is unset or empty. The compose sandbox sets all of these for
its synthetic requests except the service type: its synthetic orders carry none, and they are not
all one kind of service, so each request there gives its own (`service_type`).

To hold remote models to the places your agreements cover, set
`MENDARI_PROVIDER_ALLOWED_ORIGINS` to the origins they may be called at, comma-separated as for
`MENDARI_FHIR_ALLOWED_SERVERS` (for example `https://models.internal.example.org` for a model
server on your network; add `https://ai-gateway.vercel.sh` for the AI Gateway, and
`https://api.vercel.com` for `JEV_CONNECTOR`). A Jev, LLM or Vercel Connect request to any other
origin is then refused before it is sent (`invalid_endpoint`: the question fails and the decision
goes to a person), and an `LLM_BASE_URL` or `JEV_API_URL` off the list stops the server at
start-up. Unset, there is no such list. Signed in, `/api/health` shows it
(`provider_allowed_origins`).

For PHI, prefer local providers (rules, naive Bayes, criteria, dosing), or a local model server
(an OpenAI-compatible `base_url` on your network). Use a remote model only under an agreement
that covers PHI. Setting up Jev sends Jev the inbox messages, the referral reasons (and the
patient's age) and the prior-auth notes those packs read, in every slot `MENDARI_PROVIDERS`
chooses nothing for (README, "Jev"). To keep a slot on site, set it there: `notes=rules` keeps
the prior-auth notes local, and that pack then automates nothing. The trial prescreener and AI
Guard send Jev nothing unless `MENDARI_PROVIDERS` names a Jev reader for them. A deployment that
already had Jev set up for some slots runs it in these too after the upgrade.

Unless the screen (or the notes) is the keyword rules plus a model (`rules_and_jev` or
`rules_and_llm`), the inbox, referral and prior-auth packs automate nothing, and AI Guard
releases nothing: every case goes to a person, and each decision's reasons say what is missing.
Signed in, `/api/health` shows which variant each slot runs (`providers`), whether the Jev
defaults are on (`jev.defaults`) and the slots that run no model at all (`without_model`); that
Jev actually answers shows in `jev.calls_today` (in memberships mode, `mendari usage`) and in
the decisions' errors. The server also
logs, at startup, each slot the Jev defaults turn on.

## 6. Monitoring and governance

- **Monitoring:** the Monitoring view shows the served version's accuracy, drift and
  overrides, week by week. It compares them with the benchmark only when that measured this
  build of the pack with the providers and models this deployment runs, under the same
  governance (an approval in force or not), over the decisions made since that configuration
  started, and stratum by stratum (reviewed decisions, automated ones); otherwise it says why
  (`benchmark.reason` in `/api/monitoring`: `no_benchmark`, `another_build`,
  `other_providers`, `providers_unknown` or `other_policy`), and only the checks that need no
  benchmark run: automated decisions rated wrong, overrides that doubled against the week
  before, and operational health. A deployment that runs other providers than the benchmark
  did (Jev where it measured `naive_bayes`, say), or automates a risk level 2 or 3 pack under an
  approval the benchmark was measured without, gets only those: the benchmark says nothing about
  how that configuration should do. Operational health, from what each decision stored, alerts
  week over week (50 decisions or more this week):

  | Alert | Fires when |
  |---|---|
  | `provider_errors` | 5% or more of decisions had a provider error recorded (the bottom of its 95% interval), and that share doubled against the week before; on that level alone when the week before had fewer than 50 counted (just after upgrading, or a new configuration) |
  | `abstention` | with 50 or more the week before too: the abstention share doubled and rose 5 points or more (the bottom of its interval) |
  | `latency` | with 50 or more the week before too: 5% or more of decisions (the bottom of the interval) took twice the week before's p95, and 2 s or more |

  A provider error never makes a decision more automatic (policy only lowers automation): it
  goes to a person, or a rule blocks it. These alerts say the service got worse, not less safe.
  A provider error is recorded when a provider failed, or a composite's member did (the
  survivor may still have answered). Decisions recorded before this count existed are left out
  of the provider error share. Schedule it to be told when a pack degrades:
  - on a server, `GET /api/internal/monitor` with `Authorization: Bearer $CRON_SECRET` (as the
    scheduled purge) monitors each organization with decisions in the last 30 days on its own,
    leaves out the status checks' organization (`MENDARI_STATUS_ORG`), and posts one alert per
    organization to `MENDARI_ALERT_WEBHOOK`, recorded; it first sends again the alerts still
    pending, for 60 seconds at most (those left stay due). A Vercel function stops at 300
    seconds, so it starts no organization after 180, which leaves the last one time for its
    posts; the rest are counted and an alert names them, and each day's run starts at another
    organization, so none is left every day. Its answer holds counts only. On Vercel this is a
    `crons` entry in `vercel.json` (the project's own schedule is the owner's to turn on);
  - or `mendari monitor --db "$DATABASE_URL" --each-org` (the templates in `deploy/` run it
    daily), which reads which providers ran from the deployment records the server keeps and
    records nothing (it opens the database read-only), so it never sends a pending alert
    again. It exits 0 when every alert it found was posted, 1 on an error (one organization's
    failure is its own line, and the others still run) or on alerts with no webhook to post
    them, and 3 when one could not be posted. The Kubernetes job fails on 1 or 3 and is never
    re-run (a re-run would post every organization's alert again).
- **Governance:** packs at risk level 2 or 3 automate only after someone with the `governance`
  role approves that exact version on its Decision Card (a reference such as committee minutes,
  and an expiry). Level 4 never automates.
- **Alerts** (`MENDARI_ALERT_WEBHOOK`): the monitor's alerts and the quota alert are posted
  there as JSON (`MENDARI_ALERT_FORMAT=mendari`, the default: `source`, `delivery`, `event`,
  `organization`, `alerts`) or as a Slack message (`slack`: `{"text": ...}`). Each carries ids,
  counts and Mendari's own messages, never case text. Set `MENDARI_ALERT_SECRET` (32 random
  characters or more, `openssl rand -hex 32`) and every post is signed:
  `Mendari-Signature: t=<unix seconds>,v1=<HMAC-SHA256>`, with `Mendari-Delivery` (the alert's
  id) and `Mendari-Event`. Without a secret the server warns at startup. A post that gets no
  answer, `408`, `429` or a `5xx` is tried again (any other answer is final): `mendari monitor`
  tries up to three times and exits `3` when its alerts could not be posted (`1` when it found
  alerts and posted them; with `--each-org`, `0`); the quota alert is tried once on the
  decision's path. The server
  records each alert it sends (`alert_deliveries`: its body, status, tries, last HTTP status and
  an error code; never the URL or the secret), so one that may yet go through (no answer, `408`,
  `429`, `5xx`) stays `pending` for another try, five in all (any other answer is `failed` at
  once). Each run of `GET /api/internal/monitor` first sends the due ones again;
  `mendari monitor` records nothing and sends none again, so a deployment that wants pending
  alerts tried again schedules the route, with `CRON_SECRET`.
  A receiver checks the signature over the raw body:

  <!-- receiver: the recipe tests/test_alerts.py runs -->
  ```python
  import hashlib
  import hmac
  import time


  def is_mendari(secret: str, header: str, body: bytes, now: float | None = None) -> bool:
      """True when `header` (Mendari-Signature) signs `body` with `secret`, within 5 minutes."""
      fields = dict(part.split("=", 1) for part in header.split(","))
      sent = int(fields["t"])
      if abs((now or time.time()) - sent) > 300:
          return False  # an old post, replayed
      expected = hmac.new(secret.encode(), f"{sent}.".encode() + body, hashlib.sha256)
      return hmac.compare_digest(expected.hexdigest(), fields["v1"])
  ```

## 7. Logs

Mendari writes a security log to **stdout**, one JSON object per line, for incident response.
Send the container's stdout to your log system (Docker's logging driver, your Kubernetes log
collector, journald) and keep it as your policy says; Mendari keeps no log table of its own.

- **One access line per request** to `/api`, `/fhir`, `/cds-services`, `/auth` and `/smart`
  (`"event":"request"`), refused ones included, not `/api/health`, `/api/health/ready` (probes)
  or static files: the method, the route's template (`/api/audit/{decision_id}`, never the path or its query), the status,
  the caller (`user:<hash>`, `key:<id>`, `client:<id>`, `open` or `anonymous`), its organization
  and address, and the latency.
- **Security events**, each with a fixed `reason` code: `auth.signin.ok|refused`,
  `auth.logout`, `auth.credential.refused`, `auth.permission.refused`, `auth.origin.refused`
  (a request another site started, §2), in memberships mode `auth.organization.selected` (a
  person choosing one of their organizations), `auth.access.requested` (asking to join one,
  refused as `unknown_organization`, `member` or `open_requests`), `auth.invitation.accepted`
  (accepting an invitation, refused as `unverified`, `invalid`, `other_address` or `member`,
  with no organization) and `admin.invitation.created|revoked`,
  `admin.membership.roles_changed|removed` and `admin.access.granted|refused` (an
  organization's administrators managing its people), `smart.launch.ok|refused` (an EHR
  launch that started a session, or did not: `state_unknown`, `browser`, `token`, `id_token`,
  `issuer`, `pack`, `ehr_error` and the like), `auth.token.issued|refused`, `ratelimit.hit` (once per bucket until it has room
  again), `quota.reached`, `admin.key.created|revoked`, `admin.client.registered|revoked`,
  `governance.approved|revoked`, `admin.key.rotated`, `auth.key.replaced_used` (a rotated key
  still in use during its overlap, once per instance), `eval.run`, `export.audit|feedback`,
  `input.erased`,
  `retention.purged|refused` (the scheduled purge, with the count deleted), `phi.inputs.read` (under
  `hash` or `encrypt`: the decisions whose input or notes a reader was shown, and how many, as
  the chain's `input.read` has them) and `error.unhandled` (the exception's type and stack
  frames).
- **Never in a line:** a credential, a token, an assertion, a query value, a request body, a
  header, a name or an email, case text, or an exception's message (fields come from an
  allow-list). Under PHI mode `hash` or `encrypt`, an unhandled exception reaches the server
  as one that names only its type, so its own traceback print carries no message either.
- **Request ids.** Every response carries `X-Request-Id`, and JSON errors on `/api` repeat it
  as `request_id` (a sign-in error page shows it as a reference). Mendari takes your proxy's or
  client's `X-Request-Id` when it is 8 to 128 of `A-Za-z0-9._:-`, else makes a uuid4; on
  Vercel it is Vercel's own id. Ask users to quote it.
- **Client addresses** are only right behind a trusted proxy (`--forwarded-allow-ips`, §1).
- **Other libraries' lines.** Mendari holds httpx and psycopg's pool at WARNING, since they log
  each request's URL (a FHIR read names a Patient id) or a connection at INFO, and a host that
  gives the root logger INFO and a handler (Vercel's runtime does) would print them. Treat
  `MENDARI_ALERT_WEBHOOK` as a secret, like a credential: most webhook URLs carry their token
  in the path, and anyone who has it can post to your channel.
- **`mendari serve`** turns off uvicorn's access log, which prints query strings (a sign-in
  callback's code, a search's values). If your proxy logs requests, check what it keeps of
  query strings: credentials are refused in URLs, and FHIR searches can send their criteria in
  a POST form (`/fhir/{type}/_search`).
- **What must last** is in the audit chain as well: every input or note read, and key, client
  and governance changes. The log is for finding what happened around them.
- **The project's own Vercel deployment** (synthetic data only) keeps stdout as runtime logs:
  an hour on Hobby, a day on Pro, 30 days with Observability Plus. A log drain (Pro) keeps
  them longer; add one when the first outside developer gets a key.

## 8. Settings at a glance

The security settings, with their defaults. A value outside its bounds stops the server at
startup, so a typo never runs with a weaker setting.

| Setting | Default | What it does | § |
|---|---|---|---|
| `MENDARI_OIDC_ISSUER`, `MENDARI_OIDC_CLIENT_ID` | unset: open mode | Sign-in; every route then needs a role | 2 |
| `MENDARI_ACCOUNTS` | `claims` | `memberships`: organizations and roles from records, not claims (needs sign-in; [runbook](operations/accounts.md)) | 2 |
| `MENDARI_PACKS_PATH` | none | Folders of your own packs (`:`-separated), searched first; when one id is found in several places the newest version is served; a relative folder is read from the working directory, and one that does not exist is skipped | 1 |
| `MENDARI_PACKS` | every pack found | Only these pack ids are served (comma-separated; each must be found on the path): one pack per image | 1 |
| `MENDARI_PUBLIC_URL` | unset | The address people use; required for Backend Services | 2 |
| `MENDARI_SESSION_IDLE_MINUTES` | 30 (5 to 120) | A session ends after this long without a request | 2 |
| `MENDARI_SESSION_MAX_HOURS` | 8 (1 to 12) | ...and this long after signing in | 2 |
| `MENDARI_TRUSTED_ORIGINS` | none | Other origins people use (a name a proxy rewrites), comma-separated | 2 |
| `MENDARI_SMART_CLIENT_ID` | unset: no EHR launch | The SMART app's client id at the EHR; turns the launch on | 2 |
| `MENDARI_SMART_EHRS` | none: with sign-in on, every launch refused | The EHRs that may launch: `<FHIR base>=<org>:view` or `:decide`, comma-separated; `<org>` is one your people sign in to (`default` without `MENDARI_OIDC_ORG_CLAIM`); read at startup | 2 |
| `MENDARI_SMART_SCOPES` | `launch openid fhirUser patient/*.rs` | What a launch asks the EHR for; with sign-in on it must keep `openid fhirUser`, and `offline_access` refuses every launch | 2 |
| `MENDARI_PHI_MODE` | `encrypt` on Vercel; elsewhere `off` without sign-in or the EHR launch, and with either none (required) | `hash` or `encrypt` for real patient data; Vercel refuses `off`, so does sign-in anywhere, and so does a database once kept under `hash` or `encrypt` | 3 |
| `MENDARI_PHI_KEY` | none | 32 bytes, base64; seals inputs, evidence and notes under `encrypt` | 3 |
| `MENDARI_INPUT_HASH_KEY` | none | 32 bytes, base64, not the PHI key; required under `hash` and `encrypt` | 3 |
| `MENDARI_PHI_READ_KEYS` | none | Under `encrypt`, up to 4 keys (base64, comma-separated) the deployment opens with but never seals: the previous key during a rotation, or the next one before it becomes current | 3 |
| `MENDARI_RETENTION_DAYS` | 30 (1 to 36500) | How long kept inputs and notes are kept, by `mendari phi purge` and the scheduled purge | 3 |
| `CRON_SECRET` | unset: no scheduled purge, checkpoint or monitor | At least 16 random characters, no spaces around them: Vercel Cron's bearer token for `/api/internal/retention`, `/api/internal/checkpoint` and `/api/internal/monitor` (otherwise unused, with a warning: they stay off) | 3 |
| `MENDARI_CHECKPOINT_KEY` | unset: nothing signed or published | 32 random bytes in base64, the Ed25519 key that signs the daily checkpoint (`GET /api/audit/checkpoint`); a malformed one stops the server | 3 |
| `MENDARI_MAX_BODY_BYTES` | 4 MiB (64 KiB at least) | The largest request body; a `413` above it | 1 |
| `MENDARI_DECISION_DEADLINE_S` | 120 (10 to 270, whole seconds) | How long one decision's remote calls may take together; past it they fail with `timeout` and a person decides, while local providers still answer | 4 |
| `MENDARI_RATE_LIMITS` | `on` with sign-in, `off` without | `on`, `log` or `off`, then overrides (`decide=1200/min:200`) | 4 |
| `MENDARI_NOTICE_ACKNOWLEDGEMENT` | `required` | `required`: a signed-in person who may decide or review acknowledges the notice (research preview, synthetic data only) once per version when the dashboard opens, chained as `notice.acknowledged`; until then deciding, reviewing, feedback and minting an API key or client are refused (`403`, header `Mendari-Notice-Required`); `off`: a site whose own agreements replace it. Keys, clients, EHR launches and open mode are never asked | 4 |
| `MENDARI_ORG_DAILY_LIMITS` | unset: none | Per-organization daily quotas: `decisions=…,jev=…,llm=…`, and its evaluations' calls `eval_jev=…,eval_llm=…` | 4 |
| `MENDARI_PRINCIPAL_DAILY_LIMIT` | unset: none | Decisions each principal may ask for a day (an order hook's draft orders each); past it `429` until midnight (sign-in only) | 4 |
| `JEV_API_URL`, `JEV_MODEL`, `JEV_API_KEY` | unset: no Jev | All three set: the inbox, referral and prior-auth packs send their `state_fields` to Jev in every slot `MENDARI_PROVIDERS` chooses nothing for; a model id or an endpoint Jev cannot be called with stops startup | 5 |
| `MENDARI_PROVIDERS` | unset: each pack's default, or the Jev defaults | The provider variant of each slot it applies to: `slot=variant`, or `<pack>:slot=variant` for one pack | 5 |
| `MENDARI_JEV_DAILY_LIMIT`, `MENDARI_LLM_DAILY_LIMIT` | 500 each; `none`: no limit, still counted | The deployment's daily remote calls, evaluations included; the three packs' Jev calls share one budget | 4 |
| `mendari serve --proxy-headers` | on | Read the client address from `X-Forwarded-For` | 1 |
| `--forwarded-allow-ips`, `FORWARDED_ALLOW_IPS` | `127.0.0.1` (the compose template: its gateway, `172.30.0.1`) | The proxies trusted for `X-Forwarded-For`, as the engine sees them | 1 |
| `MENDARI_ALERT_WEBHOOK` | unset: no alerts posted | Where the monitor's and the quota alerts go (https; keep it as a secret) | 6 |
| `MENDARI_ALERT_FORMAT` | `mendari` | `mendari` (JSON) or `slack` | 6 |
| `MENDARI_ALERT_SECRET` | unset: unsigned, with a warning | Signs every alert (`Mendari-Signature`); 32 characters or more | 6 |
| `MENDARI_STATUS_ORG` | unset | The organization the status checks decide in, which the scheduled monitor leaves out | 6 |

## 9. Before go-live

In this order: the build, the settings as the server reports them, the browser, the limits,
reads of PHI, then operations. Keep the answers with your go-live record.

- [ ] Validate the pack on your own data, with the providers your server runs:
      `mendari eval <pack> --dataset local.jsonl --provider <slot>=<variant> ...` (with Jev set
      up, e.g. `--provider safety_screen=rules_and_jev --provider router=cascade` for the inbox;
      the command alone runs the pack's offline defaults), or run it from the dashboard's
      Evaluations view, which uses the deployed providers. With Jev, the dataset goes to Jev.
      Bulk FHIR: `mendari bulk export` then `bulk dataset`. Compare with the benchmark.
- [ ] The engine image is deployed by digest (`<image>:<tag>@sha256:…`), not by tag alone.
- [ ] `MENDARI_PHI_KEY` and `MENDARI_INPUT_HASH_KEY` come from your secret manager, and they
      differ.
- [ ] Without signing in, `/api/health` shows only `status`, `version` and `sign_in: true`.
- [ ] Signed in, `/api/health` `providers` names, for every pack, only remote readers your
      agreements cover (`jev.defaults: true` means Jev's settings are set), and
      `without_model` lists the slots you meant to keep without a model.
- [ ] Signed in as an admin, `/api/health` shows:
      `phi_mode: encrypt`; `input_hash: hmac-sha256:<key id>` (keyed, not `sha256`);
      `rate_limits.mode: on`; `rate_limits.client_ip` equal to your browser's own public
      address (what a "what is my IP" page shows from the same machine), with
      `x_forwarded_for_used: true` behind a proxy (not the proxy's or Docker's gateway's
      address); the `sessions` policy you chose; `smart_launch: false` unless you
      need it, and then `smart_ehrs` listing exactly the EHRs you registered (§2); and only the
      providers and FHIR servers you expect.
- [ ] Daily budgets and quotas are set and visible there (`jev.daily_limit`,
      `llm.daily_limit`, `org_quotas`). Choose them from a week of your traffic.
- [ ] Only if you turn on EHR launches: each EHR is registered in `MENDARI_SMART_EHRS` (§2),
      to the organization your people's sign-in gives them (`default` without
      `MENDARI_OIDC_ORG_CLAIM`), and `mendari smart check <base>`, run with the server's
      settings, reports no `fail` for it. Then launch from the EHR in a new window or its
      embedded window (not an iframe, which fails with `browser`): the clinician sees the
      patient view alone, a decision there is in the audit trail of the registered
      organization, under the EHR's `<issuer>|<subject>` (or verified email), and a second
      launch in the same browser leaves the first window saying it was replaced.
- [ ] Only if you turn on EHR launches: a reviewer, signed in as usual, opens the launched
      decision's id (the one the clinician's window shows, "Recorded in your organization's
      audit trail as …") in the audit trail and sees it; a HUMAN_REVIEW one is in their review
      queue. A `404` there means the EHR is registered to an organization nobody signs in to.
- [ ] The session cookie is `__Host-mendari_session` (in the browser's developer tools), and a
      session left alone past the idle time is refused ("Your session ended. Sign in again.").
- [ ] On each address people use, signed in: review a case on the dashboard and sign out, with
      no `403` "cross-site request refused" (list a rewritten name in
      `MENDARI_TRUSTED_ORIGINS`). The answers carry `Strict-Transport-Security`.
- [ ] Signed out, `/docs` shows the developer landing with "Sign-in is on here" and no pack
      names from this deployment (the examples use a fixed synthetic pack); signed in,
      `/docs/reference` shows the API reference. Neither page's browser console reports a CSP
      violation (a proxy in front must not replace Mendari's `Content-Security-Policy`).
- [ ] One API key past its decide limit gets a `429` with `Retry-After` (lower `decide` in
      `MENDARI_RATE_LIMITS` on a test instance to check), and your proxy limits `/auth/` per
      address.
- [ ] A viewer can't read an input; a reviewer can, and the read is chained: an `input.read`
      under the reviewer's verified email in the audit chain, and a `phi.inputs.read` line in
      the log.
- [ ] A viewer sees a review note only as sealed; a reviewer reads it, and
      `mendari audit verify --notes` passes.
- [ ] Integrations that record outcomes send `source` as an identifier (`ehr`, not a sentence)
      and a label of the pack's primary question.
- [ ] The security log reaches your log system: a refused request (a wrong key) appears there as
      an `auth.credential.refused` line and a `request` line with the `X-Request-Id` the
      response carried, and no line holds a key, a query value or case text.
- [ ] Retention, monitoring and `mendari audit checkpoint` are scheduled (§3, *Checkpoints*);
      `mendari audit verify` passes.

For the project's own public deployment (synthetic data, Vercel), the owner's launch adds, in
order:

- [ ] All of Phase 4b, then the external security review and its fixes.
- [ ] `MENDARI_JEV_DAILY_LIMIT=2000`, `MENDARI_LLM_DAILY_LIMIT=200` and
      `MENDARI_ORG_DAILY_LIMITS=decisions=5000`, set for Production and confirmed in
      `/api/health` signed in.
- [ ] The firewall rule in [`deploy/vercel/`](https://github.com/mendari-hq/mendari/tree/main/deploy/vercel)
      added in log mode, its findings read for 7 days, then enforced.
- [ ] Then Vercel Authentication lifted.
