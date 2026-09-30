# Deploying OpenHealth with real patient data

OpenHealth ships with synthetic data only. Before a pilot uses real protected health
information (PHI), set it up as below. This guide covers what OpenHealth itself does. Your
organization's own obligations (a BAA with your hosting provider, a risk assessment, your
privacy officer's review) come first.

## 1. Run it where your PHI is allowed to be

Run OpenHealth inside your organization's network or an approved cloud account:
- the engine's container image, or a per-pack image that serves only one pack
  (`docker build --build-arg PACK=openhealth/patient-inbox-router .`, which sets
  `OPENHEALTH_PACKS`);
- Postgres (not SQLite) for the audit store.

[`deploy/`](../deploy) has two templates: docker compose for one server (set `OPENHEALTH_IMAGE`
in its `.env`), and Kubernetes. Both include the daily retention and monitoring jobs.

The public sandbox and the project's own Vercel deployment are **synthetic only**.

## 2. Sign-in and roles

Set your identity provider, so nobody reaches the dashboard or the API without an account:

```bash
OPENHEALTH_OIDC_ISSUER=https://login.example.org/realms/clinic
OPENHEALTH_OIDC_CLIENT_ID=openhealth
OPENHEALTH_OIDC_CLIENT_SECRET=...      # for a confidential client; from your secret manager
OPENHEALTH_OIDC_ROLE_MAP="clinicians=reviewer,informatics=admin,ai-committee=governance"
OPENHEALTH_OIDC_ORG_CLAIM=org          # only if one deployment serves several organizations
OPENHEALTH_PUBLIC_URL=https://openhealth.example.org
```

Integrations (the EHR, CDS Hooks, a message worker) use API keys with the `service` role.
Admins create them in `POST /api/auth/keys`; a key is shown once and can be revoked at any time.

## 3. PHI mode

```bash
OPENHEALTH_PHI_MODE=encrypt
OPENHEALTH_PHI_KEY=$(python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())")
OPENHEALTH_RETENTION_DAYS=90
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

**Retention.** Schedule `openhealth phi purge --db "$DATABASE_URL"` (daily, for example). It
deletes kept inputs older than `OPENHEALTH_RETENTION_DAYS` and records each deletion in the
chain. Decisions, reviews and the chain itself stay, and `openhealth audit verify` still passes.

**The key.**
- Keep `OPENHEALTH_PHI_KEY` in your secret manager, never in the repository or an image.
- Losing it makes stored inputs unreadable. The decisions and the chain are unaffected.
- To rotate it: purge (or let retention expire) the inputs kept under the old key, then switch.
  Re-encrypting in place is not provided.

## 4. What leaves your network

By default, nothing. Each of these is off until you configure it:

| What | Setting | Leaves with it |
|---|---|---|
| Remote model providers (Jev, LLMs through Vercel AI Gateway) | `OPENHEALTH_PROVIDERS` | The pack's declared `state_fields` only |
| Reading your FHIR server (CDS Hooks fetch, SMART, Bulk) | `OPENHEALTH_FHIR_ALLOWED_SERVERS` | Nothing leaves; OpenHealth reads from servers you list |
| Alerts | `OPENHEALTH_ALERT_WEBHOOK` | Pack ids and alert messages (no case data) |
| Shared evaluation reports | `openhealth eval --share` (manual) | Metrics only; no cases, ids or small groups |

For PHI, prefer local providers (rules, naive Bayes, criteria, dosing), or a local model server
(an OpenAI-compatible `base_url` on your network). Use a remote model only under an agreement
that covers PHI.

## 5. Monitoring and governance

- **Monitoring:** the Monitoring view shows accuracy against the benchmark, drift and overrides.
  Schedule `openhealth monitor --db "$DATABASE_URL"` with `OPENHEALTH_ALERT_WEBHOOK` to be told
  when a pack degrades.
- **Governance:** packs at risk level 2 or 3 automate only after someone with the `governance`
  role approves that exact version on its Decision Card (a reference such as committee minutes,
  and an expiry). Level 4 never automates.

## 6. Before go-live

- [ ] Validate the pack on your own data: `openhealth eval <pack> --dataset local.jsonl` (or
      Bulk FHIR: `openhealth bulk export` then `bulk dataset`), and compare with the benchmark.
- [ ] `/api/health` shows `sign_in: true`, `phi_mode: encrypt`, and only the providers and FHIR
      servers you expect.
- [ ] A viewer can't read an input; a reviewer can, and the read appears in the audit chain.
- [ ] Retention and monitoring are scheduled; `openhealth audit verify` passes.
