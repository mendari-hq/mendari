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

- **Open by default.** Without an identity provider the server has no sign-in of its own.
  Locally it binds to `127.0.0.1`, and the docker sandbox publishes ports on localhost only. On
  Vercel it must stay behind Vercel Authentication set to **all deployments**
  (`ssoProtection: all`). The default, "all except custom domains", leaves the production
  domain public. External machine callers, such as an EHR calling CDS Hooks, would need
  Protection Bypass for Automation there, which is not set up.
- **Sign-in for organizations.** Set `MENDARI_OIDC_ISSUER` and `MENDARI_OIDC_CLIENT_ID`,
  and every route needs a role. Machines (an EHR calling CDS Hooks, a worker) use API keys, sent
  as `Authorization: Bearer ohk_…` and nowhere else: an `Authorization` header decides alone (no
  fallback to the session cookie), a credential in a URL or a form is refused, and refusals
  carry `WWW-Authenticate`. Only a signed-in person manages keys, never a key. A key is a
  `viewer` or a `service`, expires (90 days unless asked, 365 at most; keys from before this
  rule expire 30 days after the release that brought it first starts), and can be narrowed by
  scopes (SMART v2 `system/` reads, Mendari's decide and outcome URIs): a scoped key reaches
  only the routes its scopes name. Creating and revoking keys are recorded in the audit chain.
  `/api` answers are `Cache-Control: no-store`.
- **SMART Backend Services (off by default).** `MENDARI_BACKEND_SERVICES=on` (refused without
  sign-in and `MENDARI_PUBLIC_URL`) lets clients that a signed-in admin registers, with a public
  JWK Set by value, exchange a signed assertion (RS384 or ES384, `aud` exactly the token URL,
  five minutes at most, its `jti` once) for a five-minute opaque token (`Bearer mat_…`). Every
  assertion check runs before anything is written, and every failure is the same
  `invalid_client`. A token reads its client's role, scopes, organization and revocation on
  each request, so revoking a client stops its tokens at once. Machines are `viewer` or
  `service`; no key or token registers a client. Registering and revoking clients are chained.
  Mendari fetches no client key (no JWKS URLs) and the endpoint has no rate limit of its own
  yet, so it stays behind Vercel Authentication.
- **PHI mode.** `MENDARI_PHI_MODE=hash` stores no input; `encrypt` stores inputs encrypted
  apart from the audit chain, readable only by signed-in reviewers and admins, and every read
  is logged. Without PHI mode, audit records keep full inputs by default
  (`audit.store_input: full`); use `hash` for any pack that might see sensitive text.
- **Headers.** Every response has a strict CSP, pages and the API reference included: each
  page's own inline scripts by hash only, nothing from another origin. Every response also gets
  no framing, nosniff, no referrer and cross-origin isolation.
- **The API reference** (`/docs/reference`, behind sign-in when it is on) is Swagger UI served from
  files vendored at a pinned version, never a CDN, and its page allows no inline script or
  style. `/docs` and `/redoc` redirect there.

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
- an OWASP ZAP scan of the running API with sign-in on, where medium and high risks fail.

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
