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
in its `.env`), and Kubernetes. Both include the daily retention and monitoring jobs.

The public sandbox and the project's own Vercel deployment are **synthetic only**.

**Health checks and request size.** Probes and load balancers call `/api/health` without a
credential and get `status`, `version` and `sign_in` only. The settings (PHI mode, packs,
providers, the FHIR servers Mendari calls, today's remote calls) are shown to someone signed in,
or a key, that may read. A request body is 4 MiB at most: raise `MENDARI_MAX_BODY_BYTES` (in
bytes, 64 KiB or more) if your FHIR Bundles are larger, and give your proxy the same limit
(nginx's `client_max_body_size`, say). A larger body gets a `413`.

**Rate limits.** With sign-in on, Mendari limits what costs something, counted in the database
so every replica shares the count. Reads are not limited. Over a limit the answer is a `429`
with `Retry-After` (seconds), before any body is read, model called or record written: JSON on
`/api`, an OperationOutcome (`throttled`) on FHIR routes, OAuth's `temporarily_unavailable` at
the token endpoint.

| What | Counted per | Default |
|---|---|---|
| Signing in (`/auth/login`, the SMART launch) | client address | 60 a minute |
| The token endpoint, before its body is read | client address | 60 a minute |
| Failed assertions at the token endpoint | client address and client | 10 in 10 minutes |
| Decisions, at every entry point (REST, FHIR `$decide`, CDS Hooks, DTR, PAS, SMART) | API key, client or person | 600 a minute, 100 at once |
| The AI guard (an LLM call) | API key, client or person | 60 a minute |
| Evaluations | caller | 6 an hour |
| Exports (the audit FHIR export, the feedback export) | caller | 10 an hour |

`MENDARI_RATE_LIMITS` is `on` (the default with sign-in), `log` (count and log what would be
refused, refuse nothing) or `off` (the default in open mode), optionally followed by overrides:
`MENDARI_RATE_LIMITS=on,decide=1200/min:200,evaluate=20/h` (`count/[n]unit[:burst]`, unit `s`,
`min` or `h`). A bad value stops the server at startup. Signed in, `/api/health` shows the mode,
every limit and `client_ip_source`.

**Behind a proxy.** Sign-in and the token endpoint count per client address, so the engine must
see the client's, not your proxy's. `mendari serve` takes `X-Forwarded-For` only from the
proxies in `--forwarded-allow-ips` (or `FORWARDED_ALLOW_IPS`; default `127.0.0.1`), and the
templates set it. Limit per address at the proxy too, on `/auth/` and `/smart/` only (a
hospital sends everyone's traffic from one address):
[`deploy/onprem/nginx-limits.conf`](https://github.com/Junebugg1214/mendari/blob/main/deploy/onprem/nginx-limits.conf)
is an example.

**Daily quotas per organization.** `MENDARI_ORG_DAILY_LIMITS=decisions=5000,jev=2000,llm=200`
caps what each organization uses a day: decisions, and the remote model calls they make. Unset,
there is none. Evaluations never count against them (they spend the deployment's daily budgets,
`MENDARI_JEV_DAILY_LIMIT` and `MENDARI_LLM_DAILY_LIMIT`, only). Over a quota a decision is still
made and recorded, but no provider runs and it goes to the review queue (`ABSTAIN` or
`HUMAN_REVIEW`, with `quota_exhausted` in its errors), as over a daily budget: a quota never
makes anything more automatic. The first time an organization reaches a quota each day,
Mendari logs it and posts one alert to `MENDARI_ALERT_WEBHOOK` (the organization, the quota and
its limit; no case data). Quotas start over at local midnight (`MENDARI_TIMEZONE`).

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
- The token endpoint takes 60 requests a minute per client address. After 10 failed
  assertions in 10 minutes from one address for one client, that pair gets a `429` until the
  oldest failure has aged out; another client, or the same client from another address, is not
  affected (never by the client alone: anyone who knows a client's id could lock it out).
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
| Alerts | `MENDARI_ALERT_WEBHOOK` | Pack ids and alert messages; an organization's id when it reaches a daily quota (no case data) |
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

## 6. Logs

Mendari writes a security log to **stdout**, one JSON object per line, for incident response.
Send the container's stdout to your log system (Docker's logging driver, your Kubernetes log
collector, journald) and keep it as your policy says; Mendari keeps no log table of its own.

- **One access line per request** to `/api`, `/fhir`, `/cds-services` and `/auth`
  (`"event":"request"`), refused ones included, not `/api/health` or static files: the method,
  the route's template (`/api/audit/{decision_id}`, never the path or its query), the status,
  the caller (`user:<hash>`, `key:<id>`, `client:<id>`, `open` or `anonymous`), its organization
  and address, and the latency.
- **Security events**, each with a fixed `reason` code: `auth.signin.ok|refused`,
  `auth.logout`, `auth.credential.refused`, `auth.permission.refused`,
  `auth.token.issued|refused`, `ratelimit.hit` (once per bucket until it has room again),
  `quota.reached`, `admin.key.created|revoked`, `admin.client.registered|revoked`,
  `governance.approved|revoked`, `eval.run`, `export.audit|feedback`, `phi.inputs.read` (the
  decision ids a reader was shown, and how many) and `error.unhandled` (the exception's type and
  stack frames).
- **Never in a line:** a credential, a token, an assertion, a query value, a request body, a
  header, a name or an email, case text, or an exception's message (fields come from an
  allow-list). Under PHI mode `hash` or `encrypt`, an unhandled exception reaches the server
  as one that names only its type, so its own traceback print carries no message either.
- **Request ids.** Every response carries `X-Request-Id`, and JSON errors on `/api` repeat it
  as `request_id` (a sign-in error page shows it as a reference). Mendari takes your proxy's or
  client's `X-Request-Id` when it is 8 to 128 of `A-Za-z0-9._:-`, else makes a uuid4; on
  Vercel it is Vercel's own id. Ask users to quote it.
- **Client addresses** are only right behind a trusted proxy (`--forwarded-allow-ips`, §1).
- **`mendari serve`** turns off uvicorn's access log, which prints query strings (a sign-in
  callback's code, a search's values). If your proxy logs requests, check what it keeps of
  query strings: credentials are refused in URLs, and FHIR searches can send their criteria in
  a POST form (`/fhir/{type}/_search`).
- **What must last** is in the audit chain as well: every input read under `encrypt`, and key,
  client and governance changes. The log is for finding what happened around them.
- **The project's own Vercel deployment** (synthetic data only) keeps stdout as runtime logs:
  an hour on Hobby, a day on Pro, 30 days with Observability Plus. A log drain (Pro) keeps
  them longer; add one when the first outside developer gets a key.

## 7. Before go-live

- [ ] Validate the pack on your own data: `mendari eval <pack> --dataset local.jsonl` (or
      Bulk FHIR: `mendari bulk export` then `bulk dataset`), and compare with the benchmark.
- [ ] Without signing in, `/api/health` shows only `status`, `version` and `sign_in: true`.
      Signed in as an admin, it shows `phi_mode: encrypt` and only the providers and FHIR
      servers you expect.
- [ ] Signed in, `/api/health` shows `rate_limits.mode: on` and a `client_ip_source` that is your
      proxy's `X-Forwarded-For` (not the connection). One API key past its decide limit gets a
      `429` with `Retry-After` (lower `decide` in `MENDARI_RATE_LIMITS` on a test instance to
      check), and your proxy limits `/auth/` per address.
- [ ] Daily budgets and quotas are set and visible in `/api/health` (`jev`, `llm`,
      `org_quotas`). The project's own public deployment launches with
      `MENDARI_JEV_DAILY_LIMIT=2000`, `MENDARI_LLM_DAILY_LIMIT=200` and
      `MENDARI_ORG_DAILY_LIMITS=decisions=5000`, its firewall rule enforced after 7 days in log
      mode ([`deploy/vercel/`](https://github.com/Junebugg1214/mendari/tree/main/deploy/vercel)); choose your own numbers from a
      week of your traffic.
- [ ] A viewer can't read an input; a reviewer can, and the read appears in the audit chain.
- [ ] Integrations that record outcomes send `source` as an identifier (`ehr`, not a sentence)
      and a label of the pack's primary question.
- [ ] The security log reaches your log system: a refused request (a wrong key) appears there as
      an `auth.credential.refused` line and a `request` line with the `X-Request-Id` the
      response carried, and no line holds a key, a query value or case text.
- [ ] Retention and monitoring are scheduled; `mendari audit verify` passes.
