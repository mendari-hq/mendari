# Security and safety

Mendari routes clinical work, so security reports get priority.

## Reporting a vulnerability

Please report privately, not in a public issue:

- use GitHub's **private vulnerability reporting** on this repository (Security → Report a
  vulnerability), or write to the maintainers privately;
- include what you found, how to reproduce it, the affected version and what it exposes;
- don't test against deployments you don't own, and never use real patient data.

We acknowledge a report within 3 working days and aim to fix critical issues within 14 days. We
credit reporters who want it, once a fix is released.

**In scope:** the engine (`src/mendari`), the dashboard, the registry and Hub client, the Hub
website (`hub/`), the container images and the deployment templates (`deploy/`).

**Out of scope:**
- a site's own deployment: TLS, network, identity provider, database and key custody belong to
  the site;
- denial of service by volume;
- the clinical content of packs. Report it in a normal issue, since it needs clinician review,
  not a security fix.

## Safety

- Mendari is **not a medical device** and makes no clinical claims.
- Everything here runs on **synthetic data**. Don't send real protected health information
  (PHI) to it, to the sandbox FHIR server or to remote providers until a deployment follows
  [`docs/deploying-with-phi.md`](docs/deploying-with-phi.md) and the owner approves real-patient
  use.

## Deployment

Each bullet is one control; the threat model names the row and the tests behind it.

- **Open by default.** Without an identity provider the server has no sign-in of its own.
  Locally it binds to `127.0.0.1`, and the docker sandbox publishes ports on localhost only. The
  project's production deployment has sign-in on (Google, owner-only) and, until the launch,
  also stays behind Vercel Authentication set to **all deployments** (`ssoProtection: all`). The
  default, "all except custom domains", leaves the production domain public. External machine
  callers, such as an EHR calling CDS Hooks, would need Protection Bypass for Automation there,
  which is not set up.
- **Sign-in and API keys.** Set `MENDARI_OIDC_ISSUER` and `MENDARI_OIDC_CLIENT_ID`, and every
  route needs a role (deny by default). Machines (an EHR calling CDS Hooks, a worker) use API
  keys, sent as `Authorization: Bearer ohk_…` and nowhere else: an `Authorization` header decides
  alone (no fallback to the session cookie), a credential in a URL or a form is refused, and
  refusals carry `WWW-Authenticate`. Only a signed-in person manages keys, never a key. A key is
  a `viewer` or a `service`, expires (90 days unless asked, 365 at most; keys from before this
  rule expire 30 days after the release that brought it first starts), and can be narrowed by
  scopes (SMART v2 `system/` reads, Mendari's decide and outcome URIs): a scoped key reaches
  only the routes its scopes name. Creating and revoking keys are recorded in the audit chain.
  `/api` answers are `Cache-Control: no-store`.
- **Sessions.** A person's session (and an EHR launch's) is a server-side row behind an opaque
  HttpOnly, Secure, SameSite=Lax cookie with no expiry of its own, named `__Host-mendari_session`
  (`__Host-mendari_smart`) so only this exact host can set it; loopback and dotless hosts keep
  the plain names. It ends after 30 minutes without a request (`MENDARI_SESSION_IDLE_MINUTES`,
  5 to 120) and 8 hours after sign-in (`MENDARI_SESSION_MAX_HOURS`, 1 to 12), both held in the
  row's expiry, which a request moves forward at most every 5 minutes and never past the limit.
  Roles are worked out on every request from the claims kept at sign-in and the current
  settings, so a role taken away ends access at once. A sign-in (or an EHR launch) is tied to
  the browser that started it by a ten-minute cookie whose digest the pending row keeps,
  compared in constant time at the callback (login CSRF). The audit chain names a person by
  their verified email, else `<issuer>|<subject>`; a key as `key <id>`, a client as
  `client <id>`. For one release the plain cookie name is still read on other hosts, for
  sessions from before Phase 4b only, so the upgrade signs nobody out.
- **Requests from other sites.** A browser request that another site starts (`Sec-Fetch-Site`
  `cross-site` or `same-site`; in a browser without Fetch Metadata, an `Origin` that is `null`
  or not this site) reaches only public routes and pages, and only with GET or HEAD: a link to
  the dashboard works, a link to an API route or a form that signs you out gets a `403`, logged
  as `auth.origin.refused`. This applies to requests with a Mendari cookie and no
  `Authorization` header, and in open mode to all. This site is the request's own host,
  `MENDARI_PUBLIC_URL`, and any alias listed in `MENDARI_TRUSTED_ORIGINS`. Scripts that send
  neither header (curl, the pilot check) are not affected.
- **SMART Backend Services (off by default).** `MENDARI_BACKEND_SERVICES=on` (refused without
  sign-in and `MENDARI_PUBLIC_URL`) lets clients that a signed-in admin registers, with a public
  JWK Set by value, exchange a signed assertion (RS384 or ES384, `aud` exactly the token URL,
  five minutes at most, its `jti` once) for a five-minute opaque token (`Bearer mat_…`). Every
  assertion check runs before anything is written, and every failure is the same
  `invalid_client`. A token reads its client's role, scopes, organization and revocation on
  each request, so revoking a client stops its tokens at once. Machines are `viewer` or
  `service`; no key or token registers a client. Registering and revoking clients are chained.
  Mendari fetches no client key (no JWKS URLs).
- **What a caller without a credential can do.** Public routes cost little and take no lock:
  `/api/health` answers only `status`, `version` and `sign_in` (its settings need a principal
  that may read), `/fhir/metadata` reads its version without the audit chain's lock, and
  sign-in keeps the identity provider's configuration for an hour. No request body passes
  `MENDARI_MAX_BODY_BYTES` (4 MiB unless set): a larger declared length is refused unread, a
  chunked body once it passes the limit, both with `413` and after the sign-in check.
- **Rate limits and quotas.** Two layers. In the app (`MENDARI_RATE_LIMITS`: `on` by default
  with sign-in, `off` in open mode, `log` to count without refusing), the costly routes are
  limited per caller (decisions 600 a minute with bursts of 100 at every entry point, the AI
  guard 60 a minute, evaluations 6 an hour, exports 10 an hour), sign-in and the token endpoint
  per address (60 a minute), and failed assertions per address and client (10 in 10 minutes),
  with one GCRA row per bucket in the database, so every instance shares the count. The check
  runs in the guard after the sign-in check: a refused request reads no body, calls no provider
  and writes nothing to the chain. A `429` carries `Retry-After` and `no-store`. Buckets are
  named by class and an opaque id (`key:<id>`, `user:<hash>`, `ip:<address>`), never a
  credential. Reads are not limited in the app. The client address is Vercel's
  `X-Forwarded-For`, or, self-hosted, the connection's unless the proxy is trusted
  (`mendari serve --forwarded-allow-ips`). Per-organization daily quotas
  (`MENDARI_ORG_DAILY_LIMITS`) count decisions and their remote calls, never evaluations; over
  one, decisions fall back to human review, and one alert goes to the webhook a day. At the
  edge, one Vercel firewall rule limits `/auth/` and `/smart/` to 120 a minute per IP
  (`deploy/vercel/`), first in log mode for 7 days; there is no per-IP rule on `/api` or
  `/fhir`, since a hospital sends everyone's traffic from one address.
- **Security log.** JSON lines on stdout (`server/security_log.py`), into Vercel's runtime logs
  or the site's own log system: one access line per `/api`, `/fhir`, `/cds-services` and
  `/auth` request (the route's template, the status, the caller's opaque id, organization and
  address, the latency), and the security events: sign-ins and their refusals with a reason,
  refused credentials and permissions, requests refused from other sites, an EHR launch
  finished in another browser, tokens issued and refused, rate limits hit, quotas reached, key,
  client and governance changes, exports, evaluations and, under `hash` or `encrypt`, the
  decisions whose input or notes a reader was shown (their ids and a count). Fields come from
  an allow-list: never a credential, a query value, a body, a header or case text. Every
  response carries an `X-Request-Id` (Vercel's id there, else a well-formed client one, else a
  new uuid), repeated in JSON errors as `request_id`. An unhandled exception is logged as its
  type and frames; under PHI mode `hash` or `encrypt` its message never reaches a log.
  `mendari serve` turns off uvicorn's access log, which prints query strings. What must last
  (input and note reads, key, client and governance changes) is in the audit chain too.
- **PHI mode and the keyed input hash.** `MENDARI_PHI_MODE=hash` stores no input; `encrypt`
  stores inputs encrypted apart from the audit chain, readable only by signed-in reviewers and
  admins, and every read is chained (`input.read`) and logged. Without PHI mode, audit records
  keep full inputs by default (`audit.store_input: full`); use `hash` for any pack that might
  see sensitive text. In both modes the chained record keeps no evidence values (sealed with
  the input under `encrypt`) and no resource ids (an input issue names `Patient (entry 2)`), and
  the input hash is an HMAC-SHA256 under `MENDARI_INPUT_HASH_KEY` (`hmac-sha256:<key id>:<hex>`),
  without which the engine refuses to start: a plain SHA-256 of a short input can be found by
  guessing. The two keys must differ.
- **Notes.** Review and rating notes and a payer's words are kept apart from the audit chain,
  which keeps only a salted commitment to each: sealed with inputs under `encrypt`, refused
  under `hash`, and readable as inputs are. Retention and `mendari notes erase` erase them, and
  the chain still verifies (`mendari audit verify --notes` checks the commitments). Notes chained
  by earlier releases are shown only to those who may read inputs. A governance approval's
  `reference` (a pointer to minutes) stays chained.
- **Headers and the API reference.** Every response has a strict CSP, pages and the API
  reference included: each page's own inline scripts by hash only, nothing from another origin.
  Every response also gets no framing, nosniff, no referrer and cross-origin isolation, and HSTS
  (two years) on any host but `localhost` or a loopback address. Cookies say `SameSite=Lax`
  explicitly. The API reference (`/docs/reference`, behind sign-in when it is on) is Swagger UI
  served from files vendored at a pinned version, never a CDN, and its page allows no inline
  script or style. `/redoc` redirects there.
- **The developer landing** (`/docs`) is public, also with sign-in on, so it is static: built at
  startup from settings and the example scripts, never from a request, it shows the same for
  every deployment of a release plus whether sign-in, the SMART launch and Backend Services are
  on. It lists no packs (open mode aside), providers, counts or records, loads only its own
  files, and allows no inline style.

## Remote providers

The Jev adapter gets its key from a Vercel Connect connector (exchanged at runtime with the
deployment's OIDC identity, cached in memory only) or, locally, from `JEV_API_KEY`. Remote
providers:
- send only a pack's declared `state_fields`;
- require HTTPS (plain HTTP only for localhost);
- refuse redirects and cap response size.

Credentials and provider error bodies never reach audit records or error messages. The same
outbound guard (allow-listed origins, no redirects, timeouts, size caps) covers every other
call the engine makes: FHIR servers, the CQL engine, the identity provider, the payer and alert
webhooks.

## Pack supply chain

Packs contain behaviour: patterns, models, CQL and policies. Review a pack like code before
running it.

- Registry packs are **signed archives** (Ed25519, deterministic). `mendari pull` verifies
  them and pins the publisher's key on first use, and versions are immutable.
- Every decision and report records the pack's digest. A governance approval to automate at
  risk level 2–3 covers one digest only.

## What CI checks on every change

The threat model ([`docs/security/threat-model.md`](docs/security/threat-model.md)) lists each
threat, its mitigation and the test that checks it. The CI job `security` runs:
- `pip-audit` on the locked Python dependencies (pip-audit itself is locked in the `dev` group);
- `npm audit` on the Hub website;
- an OWASP ZAP scan of the running API with sign-in on, where medium and high risks fail (the
  rate limits run in `log` mode there, so the scan is counted, not refused).

The job `pilot-check` plays a pilot organization end to end against a mock identity provider:
sign-in, PHI mode `encrypt` with a generated input hash key, a key for the worker, a reviewer's
chained input reads, a viewer refused, a governance approval and `mendari audit verify`. The job
`fhir-conformance` runs the HL7 validator over what Mendari renders, the audit export included.

The build itself is pinned, and `tests/test_supply_chain.py` (in the default suite) fails on any
change that loosens it:
- every GitHub Action is pinned to a full commit SHA, with its version in a comment, and every
  checkout drops its credentials (`persist-credentials: false`); CI installs one pinned uv;
- every image (CI services, `docker run`, the Dockerfile's bases, both compose files) is pinned
  by digest;
- every file a workflow downloads is checked against a pinned sha256;
- the Docker image installs exactly `uv.lock` (`uv sync --frozen`), and the build backend
  (hatchling) is pinned;
- the vendored Swagger UI matches its pin: the npm integrity value in its lockfile, and each
  file's sha256 (`tests/test_vendor.py`).

Dependabot (`.github/dependabot.yml`) proposes version updates monthly, grouped, for uv, Actions,
the Dockerfile, the compose files, the Hub website and the vendored Swagger UI, and only of
versions at least 7 days old; its branches get no Vercel preview. A Swagger UI update fails CI
until someone runs `scripts/vendor_docs_ui.py` in its pull request and reviews what it reports.
Security updates are not delayed; they need Dependabot alerts and security updates turned on in
the repository settings.

## Supported versions

Until 1.0 ships, only the latest `main` gets fixes.
