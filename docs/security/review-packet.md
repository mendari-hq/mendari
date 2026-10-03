# External security review: the packet

_For the reviewer of Mendari before any real patient data, and before the project's public
deployment drops Vercel Authentication. Written at the end of Phase 4b (2026-10-02). Everything
here runs on synthetic data._

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
opens for it. Machines are `viewer` or `service` only; managing keys and clients is for signed-in
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

## Where the evidence is

- **The threat model** ([`threat-model.md`](threat-model.md)): every threat by STRIDE id, its
  mitigation and the tests that check it. Every test it names exists.
- **Tests:** `tests/`, about 1,800 on Postgres. The security-relevant files:
  `test_auth.py` (the route × principal matrix), `test_api_keys.py`, `test_scopes.py`,
  `test_backend_services.py` (37 kinds of bad assertion), `test_sessions.py`,
  `test_session_store.py`, `test_origin.py` (every route × 12 kinds of request × each mode),
  `test_limits.py`, `test_public_surface.py`, `test_security_log.py`, `test_security_headers.py`,
  `test_vendor.py`, `test_supply_chain.py`, `test_phi.py`, `test_notes.py`,
  `test_record_hygiene.py`, `test_fhir_client.py`, and the browser suite in `tests/browser/`.
  Tests marked "SQLite and Postgres" run on both when `MENDARI_TEST_DATABASE_URL` is set.
- **CI** (`.github/workflows/ci.yml`): the job `security` (pip-audit, npm audit, an OWASP ZAP
  scan with sign-in on), `pilot-check` (a pilot organization end to end against a mock identity
  provider, PHI mode `encrypt`) and `fhir-conformance` (the HL7 validator).
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
  who asked for it in the chain (threat R1). The dashboard has no view yet for a browser that
  only launched (stage 4 of the plan). The launch is off unless configured. We would like your view on the design
  ([ADR-0006](../decisions/ADR-0006-an-ehr-launch-is-a-principal.md)).
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
