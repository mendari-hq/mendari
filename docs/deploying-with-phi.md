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

[`deploy/`](https://github.com/Junebugg1214/mendari/tree/main/deploy) has two templates: docker compose for one server (set `MENDARI_IMAGE`
in its `.env`, pinned by digest), and Kubernetes. Both include the daily retention and monitoring jobs.

The public sandbox and the project's own Vercel deployment are **synthetic only**.

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
  Hooks services needs `{"role": "service", "scope": "https://mendari.co/fhir/scope/decide"}`.
  No scope reaches Mendari's own reads under `/api` or the CDS Hooks discovery list
  (`GET /cds-services`): read those signed in, or with an unscoped key.
- **Expiry**: `days`, 90 unless asked, 365 at most. Make a new key before the old one expires;
  `GET /api/auth/keys` shows when each does.

Creating and revoking a key are recorded in the audit chain (who, when, the role, scope and
expiry; never the key).

**Upgrading from a release without key expiry (before Phase 4).** The first start of this
release gives every existing key an expiry 30 days later, once (`GET /api/auth/keys` shows it).
Existing keys keep their role until then, even `reviewer`, `governance` or `admin`, but no key
may manage keys. Replace them with `viewer` or `service` keys, with scopes, before they expire,
and revoke any you no longer need now. A key written by an older instance still running during
the rollout has no expiry and is refused until the next start gives it one.

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
  -H 'cookie: mendari_session=…' -H 'content-type: application/json' \
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

- `GET /api/auth/clients` lists clients with their keys' ids and thumbprints; `DELETE
  /api/auth/clients/{client_id}` revokes one, and its tokens stop working on the next request.
  Registering and revoking are recorded in the audit chain.
- To change a client's keys or scopes, register a new client and revoke the old one.
- The token endpoint has no rate limit of its own yet: keep the deployment behind its access
  gate (Vercel Authentication, a VPN) until one is in place.
- A deployment's tokens and one-time `jti` claims live in its database. Don't share a database
  (and a public URL) between preview and production.

Sessions and pending sign-ins are server-side rows keyed by their kind and value. A release
that changes how they are keyed (Phase 4 did) ends every session: people sign in again, and an
EHR launches the app again.

## 3. PHI mode

```bash
MENDARI_PHI_MODE=encrypt
MENDARI_PHI_KEY=$(python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())")
MENDARI_RETENTION_DAYS=90
```

| Mode | What is kept of each input |
|---|---|
| `off` | As the pack says (`audit.store_input`). For synthetic data only. |
| `hash` | Nothing but its SHA-256. Reviewers see the decision and its reasons, not the message or record. |
| `encrypt` | The input, encrypted (AES-256-GCM, bound to its decision), apart from the audit chain. |

Under `encrypt`:
- only signed-in **reviewers** and **admins** get an input back: in a decision's record, the
  review queue and the feedback list. Every input shown is written to the hash chain
  (`input.read`, with who);
- viewers, services and governance see that an input exists, not what it says;
- the audit trail's list never carries inputs;
- **without sign-in, inputs stay sealed for everyone**, since there is no one to log a read
  against. Encrypt mode needs step 2.

**Retention.** Schedule `mendari phi purge --db "$DATABASE_URL"` (daily, for example). It
deletes kept inputs older than `MENDARI_RETENTION_DAYS` and records each deletion in the
chain. Decisions, reviews and the chain itself stay, and `mendari audit verify` still passes.

**The key.**
- Keep `MENDARI_PHI_KEY` in your secret manager, never in the repository or an image.
- Losing it makes stored inputs unreadable. The decisions and the chain are unaffected.
- To rotate it: purge (or let retention expire) the inputs kept under the old key, then switch.
  Re-encrypting in place is not provided.

## 4. What leaves your network

By default, nothing. Each of these is off until you configure it:

| What | Setting | Leaves with it |
|---|---|---|
| Remote model providers (Jev, LLMs through Vercel AI Gateway) | `MENDARI_PROVIDERS` | The pack's declared `state_fields` only |
| Reading your FHIR server (CDS Hooks fetch, SMART, Bulk) | `MENDARI_FHIR_ALLOWED_SERVERS` | Nothing leaves; Mendari reads from servers you list |
| Alerts | `MENDARI_ALERT_WEBHOOK` | Pack ids and alert messages (no case data) |
| Shared evaluation reports | `mendari eval --share` (manual) | Metrics only; no cases, ids or small groups |

For PHI, prefer local providers (rules, naive Bayes, criteria, dosing), or a local model server
(an OpenAI-compatible `base_url` on your network). Use a remote model only under an agreement
that covers PHI.

## 5. Monitoring and governance

- **Monitoring:** the Monitoring view shows accuracy against the benchmark, drift and overrides.
  Schedule `mendari monitor --db "$DATABASE_URL"` with `MENDARI_ALERT_WEBHOOK` to be told
  when a pack degrades.
- **Governance:** packs at risk level 2 or 3 automate only after someone with the `governance`
  role approves that exact version on its Decision Card (a reference such as committee minutes,
  and an expiry). Level 4 never automates.

## 6. Before go-live

- [ ] Validate the pack on your own data: `mendari eval <pack> --dataset local.jsonl` (or
      Bulk FHIR: `mendari bulk export` then `bulk dataset`), and compare with the benchmark.
- [ ] `/api/health` shows `sign_in: true`, `phi_mode: encrypt`, and only the providers and FHIR
      servers you expect.
- [ ] A viewer can't read an input; a reviewer can, and the read appears in the audit chain.
- [ ] Signed in, `/docs/reference` shows the API reference and the browser console reports no
      CSP violation (a proxy in front must not replace Mendari's `Content-Security-Policy`).
- [ ] Integrations that record outcomes send `source` as an identifier (`ehr`, not a sentence)
      and a label of the pack's primary question.
- [ ] Retention and monitoring are scheduled; `mendari audit verify` passes.
- [ ] The engine image is deployed by digest (`<image>:<tag>@sha256:…`), not by tag alone.
