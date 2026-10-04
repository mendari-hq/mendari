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
- **EHR launches.** With sign-in on, a clinician launched from an EHR (SMART on FHIR) is a
  principal of their own kind: the EHR's verified id_token names them, in the organization
  registered for that EHR's FHIR base (`MENDARI_SMART_EHRS="<base>=<org>:<view|decide>"`). A
  launch may view the patient, or view and decide, as registered, and nothing else: no Mendari
  role, no other route. Its cookie is a credential on `/api/smart/` only, never with an
  `Authorization` header, and a person's session is no credential there. A launch from an EHR
  that is not registered is refused before anything leaves for it; so is one whose scopes lack
  `openid fhirUser`, whose id_token names no subject, or whose user is not a Practitioner or
  PractitionerRole (a patient portal). These refuse the launch, never startup. A launch made
  while sign-in was off is no principal once sign-in is on: it starts again. Register an EHR to
  an organization your people sign in to (the `MENDARI_OIDC_ORG_CLAIM` claim's value, else
  `default`), or no reviewer reads its launches' decisions. `MENDARI_SMART_EHRS` is read at
  startup and every request checks the launch against it, so unregistering an EHR ends its
  live launches at the next restart or redeploy. Signing out of Mendari ends the browser's
  launch too, and a later launch in the same browser ends the earlier one. The dashboard shows
  a browser that only launched the patient view alone, pinned to its launch: a window whose
  launch a later one replaced says so and shows no patient (it sends its launch with its reads
  and decisions, and the server refuses a replaced one with `409`). Launch in a new window or the EHR's embedded window: an iframe
  launch fails safely (`browser`), and a launch ends with the EHR's token. Before go-live,
  `mendari smart check <base>` reports what a launch from that base would meet here
  (registration and whether people can sign in to its organization, allow-list, scopes, the
  EHR's discovery, PKCE S256 and keys), and CI launches
  from a real SMART launcher with sign-in on. Open mode with no EHR registered launches as
  before.
- **Sessions.** A person's session (and an EHR launch's) is a server-side row behind an opaque
  HttpOnly, Secure, SameSite=Lax cookie with no expiry of its own, named `__Host-mendari_session`
  (`__Host-mendari_smart`) so only this exact host can set it; loopback and dotless hosts keep
  the plain names. It ends after 30 minutes without a request (`MENDARI_SESSION_IDLE_MINUTES`,
  5 to 120) and 8 hours after sign-in (`MENDARI_SESSION_MAX_HOURS`, 1 to 12), both held in the
  row's expiry, which a request moves forward at most every 5 minutes and never past the limit.
  Roles are worked out on every request from the claims kept at sign-in and the current
  settings, so a role taken away ends access at once. A sign-in (or an EHR launch) is tied to
  the browser that started it by a ten-minute cookie (login CSRF): a pending sign-in keeps its
  digest, compared in constant time at the callback, and a pending launch is sealed under it,
  so it opens only in that browser. That cookie is host-only, so a sign-in or launch that
  reaches another name than `MENDARI_PUBLIC_URL`'s (an alias, a second DNS name) is first sent
  there, once, before anything is bound. An EHR launch's rows (its access token, patient and
  PKCE verifier) are sealed at rest with AES-256-GCM under a key derived from the browser's own
  cookie: the database alone opens nothing and there is no key to manage. A row that does not
  open ends the launch, and so does an EHR that answers `401`. No refresh token is kept
  (`offline_access` in the scopes refuses the launch). People's sign-in rows hold no token and
  are not sealed. The audit chain names a person by their verified email, else
  `<issuer>|<subject>` (a launched clinician too, by the EHR's issuer); a key as `key <id>`, a
  client as `client <id>`. For one release the
  plain cookie name is still read on other hosts, for sessions from before Phase 4b only, so
  the upgrade signs nobody out.
- **Requests from other sites.** A browser request that another site starts (`Sec-Fetch-Site`
  `cross-site` or `same-site`; in a browser without Fetch Metadata, an `Origin` that is `null`
  or not this site) reaches only public routes and pages, and only with GET or HEAD: a link to
  the dashboard works, a link to an API route or a form that signs you out gets a `403`, logged
  as `auth.origin.refused`. This applies to requests without an `Authorization` header that
  carry a Mendari cookie or use POST, PUT, PATCH or DELETE (a browser leaves the SameSite=Lax
  cookie off another site's form, but the answer could still clear it), and in open mode to
  all. This site is the request's own host,
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
  limited per caller (decisions 600 a minute with bursts of 100 at every entry point, each
  draft order of a CDS Hooks order hook one decision and at most 50 a request; the AI guard 60
  a minute, evaluations 6 an hour, exports 10 an hour), sign-in per address (300 a minute,
  bursts of 120, never below the edge's limit: a hospital signs in from one address), the token
  endpoint per address (60 a minute), and failed assertions per address and client (10 in 10
  minutes),
  with one GCRA row per bucket in the database, so every instance shares the count. The check
  runs in the guard after the sign-in check: a refused request reads no body, calls no provider
  and writes nothing to the chain. A `429` carries `Retry-After` and `no-store`. Buckets are
  named by class and an opaque id (`key:<id>`, `user:<hash>`, `ehr:<hash>`, `ip:<address>`), never a
  credential. Reads are not limited in the app. The client address is Vercel's
  `X-Forwarded-For`, or, self-hosted, the connection's unless the proxy is trusted
  (`mendari serve --forwarded-allow-ips`; the compose template trusts its network's fixed
  gateway, where a proxy on the host connects from). Signed in, `/api/health` shows the address
  seen on the request and whether `X-Forwarded-For` gave it. Per-organization daily quotas
  (`MENDARI_ORG_DAILY_LIMITS`) count decisions and their remote calls, never evaluations; over
  one, decisions fall back to human review, and one alert goes to the webhook a day. At the
  edge, one Vercel firewall rule limits `/auth/` and `/smart/` to 120 a minute per IP
  (`deploy/vercel/`), first in log mode for 7 days; there is no per-IP rule on `/api` or
  `/fhir`, since a hospital sends everyone's traffic from one address.
- **Security log.** JSON lines on stdout (`server/security_log.py`), into Vercel's runtime logs
  or the site's own log system: one access line per `/api`, `/fhir`, `/cds-services`, `/auth`
  and `/smart` request (the route's template, the status, the caller's opaque id, organization
  and address, the latency), and the security events: sign-ins and EHR launches and their
  refusals with a reason, refused credentials and permissions, requests refused from other
  sites, tokens issued and refused, rate limits hit, quotas reached, key,
  client and governance changes, exports, evaluations and, under `hash` or `encrypt`, the
  decisions whose input or notes a reader was shown (their ids and a count). Fields come from
  an allow-list: never a credential, a query value, a body, a header or case text. Every
  response carries an `X-Request-Id` (Vercel's id there, else a well-formed client one, else a
  new uuid), repeated in JSON errors as `request_id`. An unhandled exception is logged as its
  type and frames; under PHI mode `hash` or `encrypt` its message never reaches a log.
  `mendari serve` turns off uvicorn's access log, which prints query strings. Libraries that
  log URLs at INFO (httpx, psycopg's pool) are held at WARNING, since Vercel's runtime prints
  every library's INFO lines: a FHIR read's URL names a Patient id. Treat the alert webhook URL
  (`MENDARI_ALERT_WEBHOOK`) as a secret: most carry their token in the path. What must last
  (input and note reads, key, client and governance changes) is in the audit chain too.
- **PHI mode and the keyed input hash.** `MENDARI_PHI_MODE=hash` stores no input; `encrypt`
  stores inputs encrypted apart from the audit chain, readable only by signed-in reviewers and
  admins, and every read is chained (`input.read`) and logged. Without PHI mode, audit records
  keep full inputs by default (`audit.store_input: full`); use `hash` for any pack that might
  see sensitive text. In both modes the chained record keeps no evidence values (sealed with
  the input under `encrypt`) and no resource ids (an input issue names `Patient (entry 2)`), and
  the input hash is an HMAC-SHA256 under `MENDARI_INPUT_HASH_KEY` (`hmac-sha256:<key id>:<hex>`),
  without which the engine (and `mendari decide --db`) refuses to start: a plain SHA-256 of a short input can be found by
  guessing. The two keys must differ.
- **Notes.** Review and rating notes and a payer's words are kept apart from the audit chain,
  which keeps only a salted commitment to each: sealed with inputs under `encrypt`, refused
  under `hash`, and readable as inputs are. Retention and `mendari notes erase` erase them, and
  the chain still verifies (`mendari audit verify --notes` checks the commitments). Notes chained
  by earlier releases are shown only to those who may read inputs. A payer's outcome and review
  actions are chained only as codes; text in their place is dropped. A governance approval's
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
- read their endpoint, model and key from fixed environment variables (`JEV_API_URL`,
  `JEV_MODEL`, `JEV_API_KEY`, `JEV_CONNECTOR`; `LLM_BASE_URL`, `LLM_MODEL`, `AI_GATEWAY_API_KEY`,
  `LLM_API_KEY`), which a pack cannot change;
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
- A pack cannot choose a provider's endpoint or credentials. Which environment variables
  supply them is fixed in code and belongs to the deployment; a pack whose provider config
  names one (`api_key_env`, `base_url_env`, `endpoint_env` and the like) is refused at load,
  so it cannot send a process secret to an address of its choosing.
- A pack reads only its own files. Every path it names must resolve, symbolic links followed,
  inside the pack's directory; a directory pack holding a symbolic link that leads outside it
  (hidden entries included) is refused at load, the files read by a fixed name (`CHANGELOG.md`,
  `DECISION_CARD.md`, `evals/*.json`) go through the same check, and `mendari card --write`
  does not write through a link. A signed archive holds regular files only (no symbolic or hard
  link, device, FIFO or sparse file), each named `pack/` and then plain path segments (none
  empty, `.`, `..` or hidden, no backslash), and extracting it writes nothing outside the
  destination. An archive is read as a stream with its caps checked while it expands, before
  the signature is checked: at most 2,000 files of at most 50 MB each, and the gzip expands to
  at most 100 times its size (64 MB for any archive) and 400 MB in all, so a small archive
  cannot make the registry, `mendari pull` or `mendari pack verify` hold more than that in
  memory. `mendari pull` (and `Decision.from_hub`) builds no cache path from what the registry
  answers until the version is a semantic version, the path stays inside the cache and the
  signed manifest names that pack and version, so a compromised registry cannot delete or write
  outside the cache.
- A pack cannot stop the server. Any error while a pack is loaded or its terminology built
  (Mendari's own errors, and any other: YAML or JSON nested too deep, a file that is not
  UTF-8) lists that pack in `/api/health` (`invalid`; an error that is not Mendari's by its type
  alone, as its text is the pack's), and every other pack is served; a served pack whose
  providers cannot be built when first used is listed there too, and its decisions answer
  `422`, never `/api/health` or another pack's. A YAML file of a pack expands to at most
  1,000,000 nodes, counted on the parsed graph before anything walks it, so a few lines of
  aliases cannot make loading exponential; a version has at most 64 characters, so comparing
  versions never fails. Publishing a pack to the registry that does not load, or whose card is
  not text, is refused with a `422` and its reason, never a server error.
- A pack's input schema is self-contained: every `$ref` points inside it (`#...`) at a schema,
  `$schema` and `$id` appear only at its root (the dialect 2020-12), and it holds no
  `$dynamicAnchor` and no `format: regex` (which jsonschema checks by compiling the caller's
  string). The validator retrieves nothing, so checking an input never fetches a URL or reads a
  file.

## What CI checks on every change

The threat model ([`docs/security/threat-model.md`](docs/security/threat-model.md)) lists each
threat, its mitigation and the test that checks it. The CI job `security` runs:
- `pip-audit` on the locked Python dependencies (pip-audit itself is locked in the `dev` group);
- `npm audit` on the Hub website: its runtime dependencies at `high`, and all of them, dev tools
  included, at `critical` (a dev tool never reaches the site, and a high advisory in one with no
  fixed version must not stop every change);
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
