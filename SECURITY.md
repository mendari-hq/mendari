# Security and safety

Mendari routes clinical work, so security reports get priority.

## Reporting a vulnerability

Please report privately, never in a public issue, pull request or discussion. The one channel
is GitHub's **private vulnerability reporting** on this repository: the **Security** tab, then
**Report a vulnerability** ([direct link](https://github.com/mendari-hq/mendari/security/advisories/new)). Only you and the maintainers see the report,
and we can discuss it, fix it in a private fork and publish an advisory from there. The project
has no security e-mail address.

- Include what you found, how to reproduce it, the affected version or commit and what it
  exposes.
- Don't test against deployments you don't own, and never use real patient data.

We acknowledge a report within 3 working days and aim to fix critical issues within 14 days. We
credit reporters who want it, once a fix is released.

**Coordinated disclosure, 90 days.** Please keep a report private until we publish its advisory
or 90 days have passed since you reported it, whichever comes first. We publish the advisory
when the fix is released, and by day 90 at the latest, with the known mitigations if no fix is
ready yet. If a fix is close at day 90 we may ask you for a short extension, which is yours to
grant; if the issue is being exploited, we may agree with you to disclose sooner.

The same policy is linked from `https://mendari.co/.well-known/security.txt` (RFC 9116), which
names this reporting form as the only contact.

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
  project's own deployment has sign-in on and, until the launch, also stays behind Vercel
  Authentication set to **all deployments**. If you deploy on Vercel yourself, note that the
  default, "all except custom domains", leaves the production domain public.
- **Sign-in and API keys.** Set `MENDARI_OIDC_ISSUER` and `MENDARI_OIDC_CLIENT_ID`, and every
  route needs a role (deny by default). Machines (an EHR calling CDS Hooks, a worker) use API
  keys (or, for an EHR you register, its own signed CDS Hooks token, below), sent as
  `Authorization: Bearer ohk_…` and nowhere else: an `Authorization` header decides
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
  `MENDARI_PUBLIC_URL`, and any alias listed in `MENDARI_TRUSTED_ORIGINS`. A read with a
  Mendari cookie and neither `Sec-Fetch-Site` nor `Origin` (a browser without Fetch Metadata,
  such as Safari before 16.4, following another site's link) reaches API routes only with
  `X-Mendari-Request`, which Mendari's own pages send and another site cannot add without a
  CORS preflight. Scripts that send neither header (curl, the pilot check) are affected only
  there: their reads with a session cookie send `X-Mendari-Request: 1` too. The one exception is
  `/cds-services` and its children, for the origins CDS clients registered, without a Mendari
  cookie: CORS lets such a page read the answers there, never with credentials (LP-23c).
- **SMART Backend Services (off by default).** `MENDARI_BACKEND_SERVICES=on` (refused without
  sign-in and `MENDARI_PUBLIC_URL`) lets clients that a signed-in admin registers, with a public
  JWK Set by value, exchange a signed assertion (RS384 or ES384, `aud` exactly the token URL,
  five minutes at most, its `jti` once) for a five-minute opaque token (`Bearer mat_…`). Every
  assertion check runs before anything is written, and every failure is the same
  `invalid_client`. A token reads its client's role, scopes, organization and revocation on
  each request, so revoking a client stops its tokens at once. Machines are `viewer` or
  `service`; no key or token registers a client. Replacing a client's public keys
  (`POST /api/auth/clients/{id}/keys`, people only) drops its tokens in the same transaction, and
  a token issued against the old keys while it ran is refused (each token carries the version of
  the key set its assertion was checked against). Registering, replacing keys and revoking
  clients are chained, with keys by id and thumbprint. Mendari fetches no Backend Services
  client's key (no JWKS URLs).
- **An EHR's own CDS Hooks token (off by default).** `MENDARI_CDS_CLIENTS`, a JSON list checked
  at startup (refused without `MENDARI_PUBLIC_URL`), registers the EHRs that call CDS Hooks with
  the short-lived JWT they sign themselves (CDS Hooks 2.0): each one's `iss`, organization, the
  FHIR bases it may send as `fhirServer`, and its public keys, by value or from one `jwks_uri` on
  `MENDARI_FHIR_ALLOWED_SERVERS` (kept an hour, fetched again at most once a minute per
  instance). A token is RS384 or ES384, its header `alg`, `kid`, `typ` and `jku` only (`jku` the
  registered URL exactly, never followed), `aud` exactly `MENDARI_PUBLIC_URL` plus the path
  called, living at most 330 seconds, with a `jti` claimed once per client across instances,
  after every other check. A verified token is the principal `cds:<id>` of the registered
  organization: it holds `discover` and `decide`, reaches `/cds-services` only, and sends only
  its registered `fhirServer`. Every token that does not verify is the one `401`, its reason in
  the security log (`cds_<reason>`). Unset, a JWT on `/cds-services` is refused in every mode,
  open mode included; elsewhere a JWT is never a credential. With sign-in off the CDS services
  answer this machine only (a loopback address, a local `Host`, no forwarding header), a valid
  token included, unless `MENDARI_CDS_SANDBOX=1`, which stops the server on Vercel and with
  sign-in on. A client's registered `origins` get CORS there, and its token is refused from a
  page on any origin but those and this site's own (the request's host, `MENDARI_PUBLIC_URL`,
  `MENDARI_TRUSTED_ORIGINS`) ([Connecting an EHR](docs/guides/connecting-an-ehr.md), threats
  S21, D13, E15, I16 and E16).
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
  credential. Reads are not limited in the app, but for a labeller's (60 a minute, bursts of 30,
  per person: each case shown is chained). The client address is Vercel's `X-Forwarded-For`,
  or, self-hosted, the connection's unless the proxy is trusted
  (`mendari serve --forwarded-allow-ips`; the compose template trusts its network's fixed
  gateway, where a proxy on the host connects from). Signed in, `/api/health` shows the address
  seen on the request and whether `X-Forwarded-For` gave it. Per-organization daily quotas
  (`MENDARI_ORG_DAILY_LIMITS`) count decisions and their remote calls, and the organization's
  evaluations' remote calls under their own `eval_jev` and `eval_llm` (unset: no limit), never
  the decisions' quotas; over a decision quota, decisions fall back to human review, over an
  evaluation quota the evaluation's remote calls fail, and one alert goes to the webhook a day. A daily
  cap per principal (`MENDARI_PRINCIPAL_DAILY_LIMIT`, off unless set) counts the decisions it
  asks for (a CDS Hooks order hook's draft orders each) and refuses its requests past it (`429`
  until local midnight), so one leaked key cannot spend its organization's day. At the
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
  see sensitive text. With sign-in on, the engine refuses to start without one (`off`, set or
  unset, is for a local sandbox without sign-in). In both modes the chained record keeps no evidence values (sealed with
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
- send only a pack's declared `state_fields` (a pack the deployment does not own, a community
  or registry pack, must declare them on every model variant, or it is refused at load; an owned
  pack that omits them sends the whole input);
- read their endpoint, model and key from fixed environment variables (`JEV_API_URL`,
  `JEV_MODEL`, `JEV_API_KEY`, `JEV_CONNECTOR`; `LLM_BASE_URL`, `LLM_MODEL`, `AI_GATEWAY_API_KEY`,
  `LLM_API_KEY`), which a pack cannot change;
- require HTTPS (plain HTTP only for localhost);
- reach only the origins on `MENDARI_PROVIDER_ALLOWED_ORIGINS` when a deployment sets it (the AI
  Gateway's default and Vercel Connect included): any other request is refused before it is
  opened (`invalid_endpoint`), and an `LLM_BASE_URL` or `JEV_API_URL` off the list stops startup;
- refuse redirects and cap response size;
- share one wall-clock deadline per decision, the CQL engine included
  (`MENDARI_DECISION_DEADLINE_S`, 120 s unless set): past it a call is not made, one under way
  is cut, and the decision records `timeout` and goes to a person; local providers still answer.

Credentials and provider error bodies never reach audit records or error messages. The same
outbound guard (allow-listed origins, no redirects, timeouts, size caps) covers every other
call the engine makes: FHIR servers, the CQL engine, the identity provider, the payer and alert
webhooks.

## Pack supply chain

Packs contain behaviour: patterns, models, CQL and policies. Review a pack like code before
running it: installing a pack means trusting its author, as with any plugin. The patterns of the
packs a deployment owns (the reference packs, and its own local packs) run on Python `re` with
no time limit, so such a pack can slow or stall a worker; on `re`, a crafted 10,000-character
message to the inbox pack can hold the interpreter for about 0.55 s (the rate limits bound how
often). Their input schemas are bounded in shape at load instead of in time: nested at most 32
deep, at most 16 `$ref`s in a chain, at most 1,000 schema objects applied to one value, and no
reference back to itself but a recursive list's or tree's. Packs the deployment does not own run
on the community engine instead
([ADR-0009](docs/decisions/ADR-0009-bounded-evaluation-for-packs-the-deployment-does-not-own.md)):
community packs, and packs pulled from a registry unless one is a copy of a reference pack of
the installed release, chosen by the digest of its files, never by its manifest or the registry.
A pack from a registry never takes a reference pack's place: one with a reference pack's id and
version but other files is refused at publish, pull and load. Their patterns run on RE2, which
never backtracks, within caps, input limits and a CPU budget per decision (below), in the
server's process: there is no isolated worker process. To trust a pulled pack, copy it into
`MENDARI_PACKS_PATH`, where it is a local pack. What a pack cannot do today is below.

- Registry packs are **signed archives** (Ed25519, deterministic). `mendari pull` verifies
  them and pins the publisher's key on first use, and versions are immutable.
- **A registry shows only what it checked**
  ([ADR-0012](docs/decisions/ADR-0012-a-registry-that-shows-only-what-it-checked.md)). It
  renders each version's Decision Card itself, from the files the digest covers, with the
  publisher's text as plain text, and never shows an uploaded card, benchmark or leaderboard.
  Each version answers what the registry verified (`checked`) apart from what the publisher
  says (`claimed`, the risk level "declared by the publisher" until a review). Its one figure is
  its own regression check on the publisher's test set, labelled as such and never a ranking.
  Where Mendari serves a pulled pack it shows the registry's card too, and never monitors it
  against, or compares its evaluations with, the benchmark it carries. Every registry write is
  a hash-chained event (`mendari registry verify`); the registry limits reads and writes and
  sends a strict CSP, as the Hub does.
- **A pulled pack waits for its organization, and stops at a recall.** Served, a pack from a
  registry never automates until its organization approves its exact digest (the Decision Card,
  or `POST /api/registry-approvals`; chained, revoked at once), at any risk level, and its own
  rule's BLOCK goes to a person until then. A recall is a signed record (`mendari yank`, or the
  registry's own key): a recalled version leaves the latest and search, and every running server
  that serves it stops automating it from its next read of the registry, as it does for a pack
  whose registry it could not read for a day. A decision for one of the registry's packs starts
  that read once the last is five minutes old (unless set), and it and any made while the read
  runs use the list read before; a hold stops a pack within seconds. Nothing in a pack lifts
  this, a copy of a reference pack included. The runbook is
  [`docs/operations/registry.md`](docs/operations/registry.md).
- Every decision and report records the pack's digest. A governance approval to automate at
  risk level 2–3 covers one digest only.
- A pack cannot choose a provider's endpoint or credentials. Which environment variables
  supply them is fixed in code and belongs to the deployment; a pack whose provider config
  names one (`api_key_env`, `base_url_env`, `endpoint_env` and the like) is refused at load,
  so it cannot send a process secret to an address of its choosing.
- A pack reads only its own files. Every path it names must resolve, symbolic links followed,
  inside the pack's directory; a directory pack holding a symbolic link that leads outside it
  is refused at load (hidden entries and `__pycache__` are not part of a pack, never digested,
  archived or read, so a `.venv` beside its files is fine). Whatever a pack decides or tests
  with is inside its digest, the identity a governance approval covers: `load_pack` hashes the
  pack first, and every file it or a provider then reads (`Pack.path`) must be one of the files
  hashed, compared by the file on disk (device and inode), not by how the path is spelled. So
  a case variant on a case-insensitive filesystem (`EVALS/rules.yaml`, `__PYCACHE__/x.yaml`,
  `logic/decision_card.md` on macOS or Windows), a link, a file added after the load or a key
  the load does not list cannot reach a file the digest leaves out. Its content must still be
  the bytes hashed: each read takes the file's bytes once and checks them against the SHA-256
  the load took of that file (`Pack.read_bytes`), and parses those bytes, so a file edited in
  place after the load, or while it runs, is refused naming the file (its decisions answer
  `422`), never used under a digest that does not describe it; loading the pack again picks the
  edit up under its new digest. The manifest fields naming
  such a file (`inputs.schema`, `inputs.fhir`, `policy`, `tests`, `evaluation.datasets`, and a
  provider's `config`, `library`, `model_file` and `calibration`) are checked at load, and a
  refusal names the field and why (through a hidden entry or `__pycache__`, in `evals/` or a
  `DECISION_CARD.md`, or not a file the digest covers). The attestations, left out of the
  digest, are read only through a separate accessor (`Pack.attestation`) by their fixed names
  (`evals/benchmark.json`, `evals/leaderboard.json`, `DECISION_CARD.md`) and
  `evaluation.benchmark`, the one manifest path that names one, which must be in `evals/` (not
  a `DECISION_CARD.md`); `mendari card --write` does not write through a link. A signed archive holds regular files only (no symbolic or hard
  link, device, FIFO or sparse file), each named `pack/` and then plain path segments (none
  empty, `.`, `..` or hidden, no backslash), no two of them one file where case and Unicode
  normalization are not significant (`A` and `a/x.yaml`, `pack.yaml` and `PACK.yaml`, NFC and
  NFD spellings), so it extracts to the same files on macOS, Windows and Linux, and extracting
  it writes nothing outside the destination (a file it cannot write is named, never a
  traceback). An archive is read as a stream with its caps checked while it expands, before
  the signature is checked: at most 2,000 files of at most 50 MB each, and the gzip expands to
  at most 100 times its size (64 MB for any archive) and 400 MB in all, so a small archive
  cannot make the registry, `mendari pull` or `mendari pack verify` hold more than that in
  memory. `mendari pull` (and `Decision.from_hub`) builds no cache path from what the registry
  answers until the version is a semantic version, the path stays inside the cache and the
  signed manifest names that pack and version, so a compromised registry cannot delete or write
  outside the cache. Nor can a pulled pack take a reference pack's place: one with the id of a
  pack this release bundles and a version equal to its (`0.4.1-x` and `0.04.1` are `0.4.1`), but
  other files, is refused at load, so the registry refuses its publish (`422`), `mendari pull`
  refuses it before it is cached, and one cached before is listed under `/api/health`
  `invalid`, with the directory to remove, while the reference pack is served.
- A pack cannot reach another pack's CQL. The engine at `MENDARI_CQL_URL` is shared, so a pack's
  library is uploaded under a resource id and a declared name that end in a hash of the pack id
  and of the library's text: a library that declares another pack's library name and version
  neither replaces nor shadows it. The declaration must be the library's first statement.
- A pack's patterns are checked at load. Every rules pattern (of every variant and composite
  member) and every input-schema `pattern` and `patternProperties` key is compiled as it will
  run (with `re`; a community pack's also for RE2, which must be able to run it), is at most
  2,000 characters, and a pack holds at most 500, counted before any is compiled; one that
  fails makes the pack invalid, named by file and rule (`mendari validate` prints no
  traceback; a server lists the pack under `/api/health` `invalid`). This does not bound how
  long a pattern takes on an input: see above.
- An owned pack's input schema cannot multiply the work of checking an input. At load it nests
  at most 32 deep, chains at most 16 `$ref`s and applies at most 1,000 schema objects to one
  value (`$ref`s that fan out: 28 steps of two applied one schema 2**28 times), refers back to
  itself only as a recursive list or tree does (once, through `properties` or `items`), and holds
  no `unevaluatedProperties` or `unevaluatedItems` below another (each such check validates again
  everything below it, so nested ones multiply); one beyond these is invalid, named by path. A pack the deployment does not own has the caps and
  the time budget instead (above).
- A pack's content cannot keep the server from starting or from serving the other packs (a
  slow pattern can still stall a worker while it runs: see above). Any error while a pack is
  loaded or its terminology built
  (Mendari's own errors, and any other: YAML or JSON nested too deep, a file that is not
  UTF-8) lists that pack in `/api/health` (`invalid`; an error that is not Mendari's by its type
  alone, as its text is the pack's), and every other pack is served; a served pack whose
  providers cannot be built (at start-up, never in a request) is listed there too, and its
  decisions answer `422`, never `/api/health` or another pack's. A YAML file of a pack that uses anchors expands
  to at most 1,000,000 nodes and 32,000,000 characters of scalar text as JSON writes it (a
  control character counts six), counted on the parsed graph before anything walks it, so a few
  lines of aliases cannot make loading exponential, or make a page that shows the file
  gigabytes long; the API reference embeds a pack's example only when it is at most 64 KB as
  JSON. A YAML number is bounded before it is built, in every base: its text (underscores
  removed) at most 4,300 characters and 4 `:` parts, an integer at most 4,300 decimal digits,
  so a base-60 number cannot make loading quadratic. A YAML value Python cannot build (a date
  such as 2024-02-30, a tagged `!!bool maybe`, an unknown tag) is the pack's error too, named by
  the file, the error's type and the value's line and column, never the value; a file that does
  not parse keeps the parser's message (what it expected, with the line around it). A pack's
  YAML and JSON hold only what a JSON response can carry: a string with a lone surrogate
  (`"\ud800"`, in YAML or JSON), `!!binary`, `!!set`, `!!omap`, `!!pairs`, and in JSON a number
  that is not finite or an integer too large to be one, are refused when the file is read (the manifest, policy, input schema and
  rules at load; a model, calibration, FHIR mapping or the tests when first read), named by the
  file and, in YAML, the line and column, so no pack can make the list of packs, a pack's
  detail or a decision a `500`. A JSON file that is not JSON, holds an integer of more than
  4,300 digits or nests too deep, a naive_bayes model of the wrong shape, a CQL library or
  `CHANGELOG.md` that is not UTF-8 and a file that cannot be read are named the same way:
  `mendari validate`, `test` and `card` print no traceback, and the server answers `422`. A
  behaviour test of the wrong shape (each is a mapping with an `input` mapping, an optional text
  `name` and an optional `expect` mapping whose `answers` is a mapping) or that is not JSON (a
  date, a number that is not finite) is the pack's validation problem, named by case number:
  `mendari validate` and `mendari test` report it and `GET /api/packs/{id}/examples` answers
  `422`, never a `500` or a traceback; the API reference picks each pack's example under its
  own guard, so one pack's tests never make it a `500`. A benchmark or leaderboard that is not
  JSON, holds what a JSON response cannot or is not the report the Decision Card shows is
  treated as absent, as the registry treats it, and the card says why. A dataset line that is
  not UTF-8 or not JSON (an integer of more than 4,300 digits, nested too deep), or holds a
  lone surrogate or NaN, is named by file and line (`GET …/datasets`, `mendari eval`). A
  version has at most 64 characters, so comparing versions never fails. Publishing a pack to
  the registry that does not load, whose changelog is not UTF-8 or whose card would be over
  1 MB, is refused with a `422` and its reason, never a server error; the registry never reads
  a pack's uploaded card, benchmark or leaderboard (it renders the card itself, above), and
  serializes what it will show before storing anything, so a pack's attestations cannot make
  search or its version's page a server error.
- A pack's input schema is self-contained: every `$ref` points inside it (`#...`) at a schema,
  `$schema` and `$id` appear only at its root (the dialect 2020-12), and it holds no
  `$dynamicAnchor` and no `format: regex` (which jsonschema checks by compiling the caller's
  string). The validator retrieves nothing, so checking an input never fetches a URL or reads a
  file.

## Community packs

Outside developers contribute packs by pull request, into `community-packs/<name>/` with the id
`community/<name>` ([ADR-0007](docs/decisions/ADR-0007-community-packs-by-pull-request.md); the
contributor's guide is [`docs/guides/contributing-a-pack.md`](docs/guides/contributing-a-pack.md)).
They are not clinically reviewed, so the runtime limits what they can do, whatever their
manifest says (threat model S13, T5, I12, D8, E10). Two of these limits, the input limits and
the patterns on RE2 with their caps and budget, also hold every pack pulled from a registry
that is not a copy of a reference pack of the installed release
([ADR-0009](docs/decisions/ADR-0009-bounded-evaluation-for-packs-the-deployment-does-not-own.md),
threat model D8 and S15): they are the limits of community and registry packs. The others are
the community packs' alone: a pulled pack is labelled "From a registry", and it automates
under its own policy only once its organization approves its digest (above).

- **Served only where a deployment turns them on** (`MENDARI_COMMUNITY_PACKS=on`, default off,
  one switch for all of them), and never shipped in the Python package or the images.
- **Labelled by origin, never by manifest.** Every surface says "Community · not clinically
  reviewed" (the review queue and the SMART patient view, where a person confirms, included);
  `publisher.type` is shown only as self-declared. A community pack's id is
  `community/<name>`, never one of the namespaces Mendari maintains (`mendari/`,
  `openhealth/`), and a second claim to its id is refused and reported, never served in place
  of the first. Its FHIR systems live under its own url
  (`https://mendari.co/packs/community/<name>/`), published by its publisher as experimental and
  labelled, never in a CodeSystem of Mendari's. CI refuses a name, CDS title or publisher that
  copies a maintained pack's name or CDS title, or uses the word Mendari (or OpenHealth) as
  whole words; a look-alike of either, or a word mixing alphabets, is a warning for the owner's
  review (the look-alike tables are not complete; the label by origin is the protection).
- **Recommend only.** A community pack never automates: the policy caps AUTO to HUMAN_REVIEW,
  and a policy rule that blocks is refused (a block is final at once, and no person sees it).
- **No spending without the owner.** Its default providers are local; unscoped provider settings
  never reach it, only `community/<name>:slot=variant` runs it on a remote variant (an
  evaluation request through the API included), and its pinned model and `max_tokens` give way
  to the deployment's.
- **The deployment's model, caps and provider configs** (community and registry packs). The
  model is the deployment's; `max_tokens` and `timeout_ms` are held to the provider defaults
  (LLM 4,096 tokens and 30 s, Jev 5 s; a lower value stands) and a `reasoning_effort` above
  `low` is dropped. Every variant and member is checked at load against its provider's closed
  config (`docs/spec/provider-configs.schema.json`): a key its provider does not read, or a value
  it refuses, refuses the pack. An owned pack loads with such problems as warnings (`mendari
  list`, the start-up log, `/api/health` `pack_warnings`), and `mendari validate` refuses both.
- **At most 10,000 characters of text** (community and registry packs), alone or together
  (keys too, where its input schema runs patterns on them: `patternProperties`,
  `propertyNames`), and at most 10,000 values
  nested at most 32 deep: a larger input is refused before any pattern or schema check runs.
- **Patterns on RE2** (community and registry packs). Its patterns (rules and input schema)
  run on RE2 (`google-re2`), which never backtracks, so no pattern can backtrack for seconds:
  their time grows with the text's
  length times the pattern's size, held to RE2's 8 MiB a pattern. A pattern RE2
  cannot run (a lookaround, a back reference, a conditional or atomic group, a possessive
  quantifier) makes the pack invalid at load. The settings that multiply that work are capped
  at load (`core/caps.py`): each text field read once, a `negation_window` of at most 200
  characters and at most 50 `negation_cues`, at most 50 schema patterns run on one value or key
  and no schema reference cycle where patterns run, at most 500 runs by the rules on an
  input and the schema on one value together, and those runs' patterns at most 3,000 in size
  together (the characters, classes and `.`s each holds, as often as its counts repeat them).
  And its patterns and schema checks share a time budget of one second of CPU per decision:
  past it its rules or its schema check stop and the decision abstains, for a person (an input
  the schema had already found wrong is refused), so one decision runs them for at most the
  budget plus one step under way (about 3.5 s of CPU on 10,000 characters at worst; the
  shipped packs' patterns take milliseconds). Its schema uses only keywords whose work stays
  within the budget (the guide lists them; `$ref` only to its own `$defs`, whose names a `$ref`
  names exactly; short `required` and `enum` lists of short texts); `unevaluatedProperties`,
  `unevaluatedItems`, `contains`, `uniqueItems` and the others jsonschema works out on its own
  are refused. Turning
  community packs on for the hosted
  platform is the owner's step (`tasks/todo.md`, B9).
- **At most risk level 1, and only a hash of each input** (`store_input: hash`), refused at load
  otherwise.
- **Checked before review.** The `pack-check` job (`pull_request`, no secrets, a read-only token,
  never `pull_request_target`) runs `mendari community-check`: scope (the pack's own folder
  only, no git submodule, a removal by the owner only), identity, manifest policy, tests,
  evaluation floors and regressions against the base branch's benchmark and tolerance (on the
  base's benchmark dataset too), a benchmark, card and models CI builds again and compares,
  every pattern compiled on RE2 (none is timed in CI: RE2 never backtracks, and at run time the time budget bounds a decision), size limits, and warnings for text that
  looks like a real person's details. Then the owner reads it, including its clinical content
  and where that content comes from.
- **Removal.** Revert the merge and deploy for a safety problem or real patient data (and
  follow GitHub's sensitive-data removal; a public history cannot be fully cleaned), or set
  `MENDARI_COMMUNITY_PACKS=off` to remove them all. `MENDARI_PACKS` is no kill switch: naming a
  pack that is not installed stops the server from starting.

Report a vulnerability in a community pack (a pattern that stalls, content that leaks or harms)
privately, as above; report a wrong clinical answer with the "Pack content" issue form.

## What CI checks on every change

The threat model ([`docs/security/threat-model.md`](docs/security/threat-model.md)) lists each
threat, its mitigation and the test that checks it. The CI job `security` runs:
- `pip-audit` on the locked Python dependencies (pip-audit itself is locked in the `dev` group);
- `npm audit` on the Hub website: its runtime dependencies at `high`, and all of them, dev tools
  included, at `critical` (a dev tool never reaches the site, and a high advisory in one with no
  fixed version must not stop every change);
- an OWASP ZAP scan of the running API with sign-in on, where medium and high risks fail (the
  rate limits run in `log` mode there, so the scan is counted, not refused).

The job `hub-site` builds the Hub website against a registry holding the five reference packs
and one whose text tries to run a script (`scripts/hub_seed.py --hostile-example`), runs its
Playwright tests (every page's headers by exact value, the publisher's text shown as text),
then an OWASP ZAP baseline scan of every page (the pinned image), where medium and high risks
fail; `tests/test_registry_limits.py` pins those steps.

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
