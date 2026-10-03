# External security review: the packet

_For the reviewer of Mendari before any real patient data, and before the project's public
deployment drops Vercel Authentication. Written at the end of Phase 4b (2026-10-02), with an
[addendum](#addendum-the-ehr-launch-gated-2026-10-03) on the EHR launch's redesign
(2026-10-03). Everything here runs on synthetic data._

## What Mendari is

Mendari runs Decision Packs: versioned bundles of questions, providers (rules, local models,
remote models) and a policy that turns answers into one of four outcomes (`AUTO`,
`HUMAN_REVIEW`, `ABSTAIN`, `BLOCK`). Every decision is recorded, and reviews, outcomes and
approvals are appended to a hash chain. A clinician reviews what the policy does not automate.

- **Stack:** Python 3.11+, FastAPI, pydantic v2. SQLite locally, Postgres in production. The
  dashboard is static HTML, CSS and ES modules with no build step.
- **Where it runs:** the project's own deployment is one Vercel function with a Neon Postgres
  database (synthetic data only). Real patient data is planned only on a pilot site's own
  servers, from Mendari's container images (`deploy/`: docker compose and Kubernetes).
- **What it is not:** a FHIR data server or a medical device. It serves decisions, its own
  audit trail and its own terminology.

## Proposed scope

| In scope | Where |
|---|---|
| The engine's HTTP surface: REST `/api`, FHIR `/fhir` (read, search, `$decide`), CDS Hooks, DTR, PAS, the sign-in routes `/auth`, the SMART launch `/smart` | `src/mendari/server/`, `src/mendari/standards/` |
| Authentication and authorization: OIDC sign-in, sessions, API keys, scopes, SMART Backend Services | `server/auth.py`, `server/sessions.py`, `server/scopes.py`, `server/backend_services.py`, `standards/oidc.py` |
| Abuse controls: the body cap, rate limits, quotas, the Origin check | `server/app.py` (`BodyCap`, `_guard`), `server/limits.py`, `server/origin.py`, `core/decision.py` (`CallBudget`) |
| What is stored: the audit chain, PHI modes, notes, the keyed input hash | `core/audit.py`, `core/phi.py`, `core/decision.py` |
| What is logged | `server/security_log.py` |
| The dashboard and the API reference in the browser: CSP, cookies, the vendored Swagger UI | `src/mendari/dashboard/`, `server/app.py` (`_security_headers`, `versioned_page`) |
| Outbound calls (SSRF): FHIR servers, the CQL engine, the identity provider, the payer, webhooks, model providers | `standards/fhir_client.py`, `providers/` |
| The build: CI, pinned actions and images, the Docker image, Dependabot | `.github/`, `Dockerfile`, `deploy/` |

Paths under `src/mendari/` are given without that prefix.

## Trust boundaries

1. **Browser ↔ engine:** the dashboard, the API reference and the API, with a session cookie.
2. **EHR ↔ engine:** CDS Hooks, the SMART launch, DTR and FHIR `$decide`, with API keys or
   SMART Backend Services tokens.
3. **Engine → outside servers:** FHIR servers, the CQL engine, the identity provider, the payer
   and alert webhooks, all through one outbound client with an allow-list.
4. **Engine → model providers:** Jev, and LLMs through a gateway. Only a pack's declared
   `state_fields` leave.
5. **Registry ↔ clients:** signed pack archives.
6. **Engine ↔ database.**

Who can call what: with sign-in on, every route has a permission (`auth._RULES`), anything
unlisted needs `admin`, and a few routes are public: `/api/health` (three fields),
`/api/auth/me`, `/auth/*` (sign-in, callback, logout, the token endpoint), `/smart/*` (the
SMART launch and its callback), `GET /api/smart/session` and `POST /api/smart/logout`, static files, `/docs` (the developer landing of Phase 5:
built once at startup from settings and the example scripts, the same for every request, listing
no pack, provider, count or record with sign-in on), the `/redoc` redirect, and
`/fhir/metadata`, OperationDefinitions and the SMART configuration. The dashboard (`/`) and
`/docs/reference` are pages: a signed-out browser is sent to sign in. Roles are `viewer`, `reviewer`, `service`, `governance` and
`admin`. A clinician launched from a registered EHR is a principal of kind `ehr`, with no role:
it holds only `launch` (`GET /api/smart/patient`, `/focus`) and, registered to decide,
`launch_decide` (`POST /api/smart/decide`), which nobody else holds (threat E7,
[ADR-0006](../decisions/ADR-0006-an-ehr-launch-is-a-principal.md)); the dashboard page also
opens for it, on the patient view alone. Machines are `viewer` or `service` only; managing keys and clients is for signed-in
people only. Without sign-in (open mode) every caller is an admin: that mode is for local use
and the sandbox.

## What changed in Phases 4 and 4b

**Phase 4, machine access** ([ADR-0004](../decisions/ADR-0004-machine-access-tokens-and-scopes.md)):
- API keys (`ohk_`) are `viewer` or `service`, hashed, expire (90 days, 365 at most) and take
  scopes: SMART v2 `system/` reads and Mendari's decide and outcome URIs. An `Authorization`
  header decides alone, with no fallback to the cookie; no credential in a URL or a form.
- SMART Backend Services, off by default: `POST /auth/token` takes an RS384 or ES384 client
  assertion and returns a five-minute opaque token (`mat_`) whose client is read on every
  request. Clients are registered by a signed-in admin with a public JWK Set by value.
- Session-table rows are keyed per kind, so the public logout can delete only its own kind.
- Outcomes take a system identifier as `source`, never free text.

**Phase 4b, hardening before PHI** ([ADR-0005](../decisions/ADR-0005-hardening-before-phi.md)):
- **What is kept for good.** Notes and a payer's text left the chain for a `notes` table, with a
  salted commitment in the chain; a payer's outcome and review actions stay only as codes. Under `hash` the record keeps no evidence values, and under
  `encrypt` they are sealed with the input. Input issues name resources by type and position.
  The input hash is an HMAC under `MENDARI_INPUT_HASH_KEY`. People are chained by verified
  email, keys as `key <id>`.
- **Public surface.** Anonymous `/api/health` is three fields; the capability statement takes
  no chain lock; sign-in caches the provider's configuration; bodies stop at 4 MiB, and none is
  read before the sign-in check.
- **Limits.** GCRA buckets in the database on costly routes per caller and on sign-in and the
  token endpoint per address; per-organization daily quotas; one Vercel firewall rule.
- **Security log.** JSON lines on stdout from an allow-list of fields, with a request id
  (sign-in and EHR launch, refusals included); httpx and psycopg's pool held at WARNING.
- **Sessions.** 30 minutes idle, 8 hours at most, `__Host-` cookies, roles worked out per
  request, sign-in and launch tied to the browser that started them (and begun on
  `MENDARI_PUBLIC_URL`, where their callback lands).
- **Origin checks.** A request another site starts reaches only public routes and pages, with
  GET or HEAD; an unsafe method is checked even without a cookie (another site's form carries
  none, but could still sign someone out).
- **Supply chain.** Swagger UI vendored at a pinned version; actions pinned by commit, images
  by digest; the Docker image installed from `uv.lock`; Dependabot monthly.

Nothing in Phase 4b signs anyone out, the schema changes are additive, and the chain is never
rewritten. The per-stage detail, with each deviation from the plan, is in `tasks/todo.md`.

## Addendum: the EHR launch gated (2026-10-03)

The Phase 4b review left two findings about the SMART on FHIR EHR launch, which is off in
production (its signed-in `/api/health` said `smart_launch: false` on 2026-10-03). Both are now
fixed on the branch `claude/smart-gating`, in five stages, before that work's own review. The
design is [ADR-0006](../decisions/ADR-0006-an-ehr-launch-is-a-principal.md); this is what we
would like your view on.

**What was wrong.**
- **C5:** with sign-in on, `POST /api/smart/decide` decided with no Mendari principal: outside
  roles, scopes and organizations, into the default organization, attributed to nobody. A
  signed-in viewer holding a launch cookie could decide, and an `Authorization` header sent with
  a launch cookie was never looked at.
- **S5:** the session table held the EHR's access token, the patient and the PKCE verifier in
  clear, under every PHI mode, after expiry too.

**What the owner decided.**
- **The EHR's verified sign-in identifies the clinician**, in the organization registered for
  that EHR.
- **A launch may `view`, or view and `decide`**, as registered, with the decision shown in
  place; nothing else.
- **No iframe and no refresh token** until the pilot EHR is known.
- **People in the chain** are named by verified email, else `<issuer>|<subject>`.

**How it works.**
- **The registration** (`MENDARI_SMART_EHRS="<FHIR base>=<org>:<view|decide>,…"`) is keyed on
  the launch's `iss`, normalized (scheme and host in lower case, no trailing slash, the path
  kept): two tenants on one host are two organizations, and one base serving several
  organizations is not supported. The organization must be one people sign in to (the
  `MENDARI_OIDC_ORG_CLAIM` claim's value, else `default`), or the launch's decisions reach no
  reviewer; with sign-in on, `mendari smart check` fails one that is not. It is read at startup
  (`standards/smart.py`) and every request checks the launch against it, never against what
  was stored with the launch: removing a registration ends that EHR's live launches at the next
  restart or redeploy.
- **A launch is a principal of kind `ehr`** (`server/auth.py`): its id is a hash of the
  id_token's issuer and subject, and its organization and level come from the registration. It
  holds two permissions nobody else holds, `launch` (`GET /api/smart/patient`, `/focus`) and,
  registered to decide, `launch_decide` (`POST /api/smart/decide`), and no role. Its credential
  is the launch cookie, and only on `/api/smart/` and the dashboard page (`/`); a person's
  session is no credential there, and an `Authorization` header decides alone, as everywhere
  (threat S5).
- **Refused at the launch, never at startup** (`server/smart_routes.py`), with sign-in on: an
  unregistered base, before any request leaves; scopes without `openid fhirUser`; an id_token
  without a subject; a user who is not a Practitioner or PractitionerRole (a patient portal).
  The sealed session records that these checks ran (`identified`), so a launch made while
  sign-in was off is no principal once it is on. `offline_access` is refused in every mode. Each refusal is logged with a fixed reason. Only a
  malformed `MENDARI_SMART_EHRS`, or a base off the FHIR allow-list, stops the server.
- **Sealed rows** (`core/session_seal.py`, threat I11): the session and pending-launch rows are
  AES-256-GCM under HKDF-SHA256 of the browser's own cookie (the session cookie; for a pending
  launch, the cookie that binds it to the browser), with the row's key and kind as associated
  data. The database alone opens nothing, and there is no server key. A row that does not open
  ends the launch, and so does an EHR that answers `401`. The access token's syntax is checked
  (`%x21-7E`, at most 16 KiB) and no refresh token is kept.
- **The chain names who asked** (`core/audit.py`, `standards/audit_fhir.py`, threat R1): with
  sign-in on, every new `decision.created` carries `requested_by` (a person, a key, a client or
  an EHR user), rendered as the requestor in the FHIR AuditEvent and Provenance. Events chained
  before are never rewritten.
- **The dashboard** shows a browser that only launched the patient view alone, the decision in
  place and no link into the audit trail. The window is pinned to its launch
  (`#patient/<launch_id>`): if a later launch in the same browser replaced it, the window says
  so and never shows the other patient. The dashboard checks this before every render and
  every decision, and sends the window's launch (`X-Mendari-Launch`) with the patient, items
  and decide requests: the server answers `409`, before any EHR read, when the browser's launch
  is another one now. It also refuses (`403`) a focus resource that is not about the launch's
  patient. A later launch's callback deletes the launch the browser held until then.
- **Limits** count a launch's decisions per `ehr` principal. Signing out of Mendari ends the
  browser's launch too.
- **Before go-live**, `mendari smart check <base>` reports the registration key, the
  allow-list, the registration, the scopes, and the EHR's discovery, PKCE S256 and keys
  (`server/smart_check.py`). In CI, `sandbox-e2e` runs a second engine with sign-in on and the
  SMART launcher registered, launches from smart-launcher-v2, and checks that each decision is
  the registered organization's and chained as the launcher's `<issuer>|<subject>`.

**What did not change.** No schema change. People's sign-in rows (`user`, `oidc-pending`) are
not sealed: they hold no token, and sealing them would sign everyone out. Nobody is signed out,
and open mode with no EHR registered launches as before. A launch in flight at the upgrade
starts again once; rolling back past the sealing breaks only `/api/smart/*` until the launch
rows are deleted (`deploying-with-phi.md` §2).

**Where we would most like your eyes.**
- Trusting each registered EHR's sign-in, keyed by the `iss` the EHR sends. Whether an EHR binds
  its `launch` value to the user who started it is the EHR's.
- The launch cookie as a credential on `/api/smart/` and `/` only, and the Origin check's
  treatment of the launch routes (threat E6).
- The sealing key derived from a cookie, with no server secret: a database leak alone opens
  nothing; a database leak together with a live cookie opens that one row.
- The wrong-patient pin: the window's launch id, checked by the server on each read and
  decision (`409`), with no secret in it.
- The EHR's token, which stays valid at the EHR after a launch ends here until it expires
  (nothing is revoked).

The tests are named in the threat model's rows E7, I11, S5 and R1, mostly in
`tests/test_smart.py`, `tests/test_smart_sealing.py`, `tests/test_smart_check.py` and the
browser suite. The per-stage detail and each deviation from the plan are in `tasks/todo.md`.

## Where the evidence is

- **The threat model** ([`threat-model.md`](threat-model.md)): every threat by STRIDE id, its
  mitigation and the tests that check it. Every test it names exists.
- **Tests:** `tests/`, about 1,800 on Postgres. The security-relevant files:
  `test_auth.py` (the route × principal matrix), `test_api_keys.py`, `test_scopes.py`,
  `test_backend_services.py` (37 kinds of bad assertion), `test_sessions.py`,
  `test_session_store.py`, `test_origin.py` (every route × 12 kinds of request × each mode),
  `test_limits.py`, `test_public_surface.py`, `test_security_log.py`, `test_security_headers.py`,
  `test_vendor.py`, `test_supply_chain.py`, `test_phi.py`, `test_notes.py`,
  `test_record_hygiene.py`, `test_fhir_client.py`, `test_smart.py`, `test_smart_sealing.py`,
  `test_smart_check.py`, and the browser suite in `tests/browser/`.
  Tests marked "SQLite and Postgres" run on both when `MENDARI_TEST_DATABASE_URL` is set.
- **CI** (`.github/workflows/ci.yml`): the job `security` (pip-audit, npm audit, an OWASP ZAP
  scan with sign-in on), `pilot-check` (a pilot organization end to end against a mock identity
  provider, PHI mode `encrypt`), `sandbox-e2e` (SMART launches from smart-launcher-v2, in open
  mode and into an engine with sign-in on and the launcher registered) and `fhir-conformance`
  (the HL7 validator).
- **How to deploy safely:** [`deploying-with-phi.md`](../deploying-with-phi.md), with the
  settings in one table (§8) and the go-live checklist (§9). `SECURITY.md` at the repository's
  root lists the controls in one place.

## Run it locally with sign-in on

This is what the CI job `pilot-check` does, with navikt's mock-oauth2-server as the identity
provider. It needs Docker and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
docker run -d --rm --name mendari-review-idp -p 127.0.0.1:58080:8080 \
  -e JSON_CONFIG='{"interactiveLogin": true}' \
  ghcr.io/navikt/mock-oauth2-server:6.0.4@sha256:47374fe063995d9b6d39ebb6ea21eb5f765025bf0aa08b61b08b439eb91adaf0

export MENDARI_OIDC_ISSUER=http://127.0.0.1:58080/pilot
export MENDARI_OIDC_CLIENT_ID=mendari
export MENDARI_OIDC_ROLE_CLAIM=groups
export MENDARI_OIDC_ROLE_MAP=informatics=admin,clinicians=reviewer,front-desk=viewer,ai-committee=governance
export MENDARI_PUBLIC_URL=http://127.0.0.1:8000
export MENDARI_PHI_MODE=encrypt
export MENDARI_PHI_KEY=$(python3 -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())")
export MENDARI_INPUT_HASH_KEY=$(python3 -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())")
uv run mendari serve --port 8000 --db var/review.db
```

- **The scripted pilot:** in another shell, `uv run mendari sandbox pilot-check --engine
  http://127.0.0.1:8000`. It signs in four people through the real flow, creates a key,
  decides 12 synthetic messages, reads and resolves a review, checks a viewer is refused,
  approves a pack and verifies the chain.
- **In a browser:** open `http://127.0.0.1:8000/` (`MENDARI_PUBLIC_URL`'s host; a sign-in
  started on another, such as `localhost`, is sent there first).
  The provider's form takes any user name and, as claims, for example
  `{"groups": ["informatics"], "email": "admin@pilot.example.org", "email_verified": true}`
  (`informatics` maps to `admin`; use `clinicians` for a reviewer, `front-desk` for a viewer).
  On `127.0.0.1` the cookies keep their plain names (`mendari_session`); on any other host with
  a dot they are `__Host-` and need https.
- **The API reference** is at `/docs/reference` once signed in; `/docs`, the developer
  landing, is public. Rate limits are on (sign-in is
  on); `MENDARI_RATE_LIMITS=log` counts without refusing.
- **The security log** is the server's stdout.
- **Backend Services:** add `MENDARI_BACKEND_SERVICES=on` and register a client as in
  [`deploying-with-phi.md`](../deploying-with-phi.md) §2.
- **Postgres:** pass `--db postgresql://…` instead of a file. Run the test suite against your
  own database with `MENDARI_TEST_DATABASE_URL=postgresql://… uv run pytest`.

Stop with Ctrl-C and `docker stop mendari-review-idp`. Both paths above were run on 2026-10-02
(Chromium for the browser).

## Known residual risks and deferred items

From the threat model's residual risks, and the plan's deferred list:
- **An EHR launch trusts the EHR's sign-in.** With sign-in on, a launch is now a principal of
  the organization its EHR is registered to (`MENDARI_SMART_EHRS`), named by the EHR's verified
  id_token, that may view the patient or also decide, and nothing else (threat E7); unregistered
  EHRs and users who are not practitioners are refused at the launch. Its rows (the EHR's access
  token, the patient, who launched, the PKCE verifier) are sealed at rest under the browser's
  own cookie (threat I11). A site must register only EHRs whose sign-in it trusts; one FHIR base
  for several organizations is not supported. Every new decision made with sign-in on names
  who asked for it in the chain (threat R1). The launch is off unless configured, and runs in
  a new window or the EHR's embedded window (an iframe launch fails safely); it ends with the
  EHR's token. The design is in the [addendum](#addendum-the-ehr-launch-gated-2026-10-03).
- **The old cookie names** (`mendari_session`, `mendari_smart`) are still read on hosts that use
  `__Host-` names, for sessions from before Phase 4b only; the next release stops.
- **Erasing a note is not instant everywhere:** backups, the database's point-in-time history
  and earlier exports keep it until they expire.
- **Records chained before Phase 4b** keep what they had (notes in the chain, unkeyed hashes,
  evidence values); they are synthetic on the project's deployment. Old notes are shown only to
  those who may read inputs.
- **The feedback export's log line** lists exported records only; a note opened for a rating
  left out of the export is chained but not logged.
- **Vercel's own request log** keeps paths with their query strings; credentials are refused in
  URLs for that reason.
- **Limits run on each instance's clock**, and Vercel's firewall counts per region.
- **Deferred:** a PHI keyring and re-keying (rotation is "purge, then switch"), database tables
  for security events (stdout plus the chain instead), in-app limits on reads, signed JWT access
  tokens, JWKS URLs and external authorization servers.

## Not verified by the project

- **Vercel behaviour:** HSTS, CSP and `X-Request-Id` on the platform; whether the firewall's
  `429` carries `Retry-After`; whether rules apply to previews; that Vercel overwrites a
  client's `X-Forwarded-For`; the 1.6 MB Swagger bundle served from the function.
- **Browsers other than Chromium:** `__Host-` cookies on the production domain, SameSite=Lax on
  the identity provider's redirect back, and the Origin check in Safari and Firefox.
- **The ZAP scan with the Origin check:** whether ZAP sends `Origin` or `Sec-Fetch-Site`, which
  would turn some of its requests into `403`s (fewer findings, not failures).
- **Production's settings** (PHI mode, whether the SMART launch is off, the Vercel plan): no
  production or preview URL was called while building.

## Out of scope

- The clinical content of packs (criteria, thresholds, labels): it needs clinician review, not
  a security review.
- A pilot site's own infrastructure: TLS, network, the identity provider, the database's
  access control, backups, key custody.
- Denial of service by volume beyond the limits above.
- The Hub website (`hub/`) and the pack registry, which are not public yet; they can follow.
- Vercel and Neon themselves, and their business associate agreements.

## How to report

Send findings privately, as `SECURITY.md` says (GitHub's private vulnerability reporting on the
repository). For each: what, where (file and line, or route), how to reproduce with synthetic
data, and the severity you would give it. Never use real patient data.
