# OpenHealth Hub

**Open infrastructure for reusable healthcare decision logic: a "Hugging Face for healthcare
decisions."**

OpenHealth turns healthcare workflow decisions (route this message, is this prior-auth packet
complete, does this AI answer need a clinician) into standardized, testable, versioned,
governed software components called **Decision Packs**. A pack does not depend on any one
model. Rules, [Jev](#jev), classical ML and (soon) LLMs all answer the same questions through
the same interface, and every decision ends in an explicit `AUTO`, `HUMAN_REVIEW`, `ABSTAIN` or
`BLOCK` with a hash-chained audit record.

> **Status: v0.1, research preview.** Everything here runs on synthetic data. It is not a
> medical device, has no authentication, and must not be used with real PHI or real patients.

```text
Clinical/EHR data (FHIR R4, CDS Hooks, REST)
        │
        ▼
  input schema ──► provider slots ──► decision policy ──► AUTO | HUMAN_REVIEW | ABSTAIN | BLOCK
                   rules │ Jev │ ML      thresholds, rules,        │
                   (swappable variants)  risk-level caps           ▼
                                                  audit trail · review queue · FHIR Task
```

## What's in v0.1

| MVP item | Where |
|---|---|
| Decision Pack specification | [`docs/spec/decision-pack-0.2.md`](docs/spec/decision-pack-0.2.md) (0.1: [`decision-pack-0.1.md`](docs/spec/decision-pack-0.1.md)), [`src/openhealth/core/spec.py`](src/openhealth/core/spec.py) |
| OpenHealth runtime | [`src/openhealth/core/decision.py`](src/openhealth/core/decision.py), [`policy.py`](src/openhealth/core/policy.py) |
| Jev provider adapter | [`src/openhealth/providers/jev.py`](src/openhealth/providers/jev.py) |
| FHIR adapter + CDS Hooks | [`src/openhealth/standards/`](src/openhealth/standards) |
| Patient Inbox Router reference pack | [`packs/patient-inbox-router/`](packs/patient-inbox-router) ([Decision Card](packs/patient-inbox-router/DECISION_CARD.md)) |
| Clinical Trial Prescreener reference pack (v0.3) | [`packs/clinical-trial-prescreener/`](packs/clinical-trial-prescreener) ([Decision Card](packs/clinical-trial-prescreener/DECISION_CARD.md)) |
| Referral Router reference pack (v0.3) | [`packs/referral-router/`](packs/referral-router) ([Decision Card](packs/referral-router/DECISION_CARD.md)) |
| Prior Authorization Preflight reference pack (v0.3) | [`packs/prior-auth-preflight/`](packs/prior-auth-preflight) ([Decision Card](packs/prior-auth-preflight/DECISION_CARD.md)) |
| AI Guard reference pack (v0.3) | [`packs/ai-guard/`](packs/ai-guard) ([Decision Card](packs/ai-guard/DECISION_CARD.md)) |
| Human review / abstention | policy outcomes + [`core/audit.py`](src/openhealth/core/audit.py) review queue |
| Evaluation harness | [`src/openhealth/evals/`](src/openhealth/evals) |
| Audit trail | SQLite, append-only hash-chained events, `openhealth audit verify` |
| Synthetic healthcare dataset | [`src/openhealth/sandbox/synthetic_inbox.py`](src/openhealth/sandbox/synthetic_inbox.py), [`synthetic_trial.py`](src/openhealth/sandbox/synthetic_trial.py), [`synthetic_referral.py`](src/openhealth/sandbox/synthetic_referral.py), [`synthetic_prior_auth.py`](src/openhealth/sandbox/synthetic_prior_auth.py), [`synthetic_guard.py`](src/openhealth/sandbox/synthetic_guard.py) |
| Developer dashboard | `openhealth serve` → http://127.0.0.1:8000: every pack on the search path, with a criteria table for derived decisions and Try-it examples from each pack's behaviour tests (`GET /api/packs/{id}/examples`) |

## Quickstart

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run openhealth demo
```

The demo routes synthetic patient messages, simulates reviewers working the queue, verifies
the audit chain, and then runs **local validation** of the public pack against a simulated
deploying site:

```text
Public benchmark (test, n=600):                 99.3%
Local performance (local-site-example, n=400):  93.2%

Performance degradation detected (-6.1 pts overall; tolerance 3.0):
  - topic=bill: 100.0% -> 73.7% (n=38)
  - label billing: 100.0% -> 80.8% (n=52)
  - topic=refill: 100.0% -> 86.5% (n=52)
  - language=es: 92.3% -> 79.8% (n=119)
  ...
Emergency screen at the local site: 18/18 detected, 0 automated.
```

The site writes Spanish billing and refill requests in ways the public data never saw. The
comparison finds where the pack degrades, and the policy sent every misrouted message to a
human instead of automating it. (The synthetic public benchmark is template-generated and
overstates real-world accuracy. It is a regression test, not a performance estimate.)

### Python

```python
from openhealth import Decision

router = Decision.from_hub("openhealth/patient-inbox-router")
result = router({"message": {"body": "I've had chest pain for 30 minutes spreading to my arm"}})

result.outcome          # Outcome.HUMAN_REVIEW
result.recommendation   # 'nurse'
result.priority         # Priority.STAT
result.reasons          # ('rule possible-emergency: Possible emergency symptoms. ...',)
result.answers["urgency"].evidence   # ('urgent:chest-pain',)
```

Swap a provider without touching the pack's contract, policy or tests:

```python
Decision.from_hub("openhealth/patient-inbox-router", providers={"router": "jev"})
```

### CLI

```bash
uv run openhealth list                                   # packs on the search path
uv run openhealth validate openhealth/patient-inbox-router
uv run openhealth test openhealth/patient-inbox-router   # the pack's own behaviour tests
uv run openhealth decide openhealth/patient-inbox-router --text "Can I pay my bill online?"
uv run openhealth eval openhealth/patient-inbox-router --dataset datasets/local-site-example.jsonl
uv run openhealth eval openhealth/patient-inbox-router --dataset my-site.jsonl --fail-on-degradation
uv run openhealth card openhealth/patient-inbox-router   # render the Decision Card
uv run openhealth test openhealth/clinical-trial-prescreener
uv run openhealth eval openhealth/clinical-trial-prescreener --dataset exclusions_holdout
uv run openhealth serve                                  # API + CDS Hooks + dashboard
uv run openhealth audit verify --db var/openhealth.db
```

### Guarding an LLM's replies

A pack with a `guard` section (the AI Guard reference pack is one) checks a model's draft
before anyone reads it. The draft is `released` only when the decision is automated with a
release recommendation; otherwise it is `held_for_review` or `blocked`, with the reasons:

```python
from openhealth import Decision
from openhealth.guard import check

guard = Decision.from_hub("openhealth/ai-guard")
draft = my_llm(question)  # any model
result = check(guard, {"question": question, "draft": draft})
if result.status == "released":
    send(result.reply)
elif result.status == "blocked":
    escalate(result.decision.fallback)  # never send a blocked draft
else:
    queue_for_clinician(draft, result.decision.reasons)
```

Over HTTP: `POST /api/packs/{id}/guard` with the same input returns `{status, reply, outcome,
recommendation, reasons, fallback, decision_id}`.

### Docker sandbox

```bash
docker compose up --build
```

This starts a HAPI FHIR R4 server seeded with synthetic Patients and Communications, the
OpenHealth engine (REST, FHIR, CDS Hooks, Jev adapter, review queue, audit, dashboard at
http://localhost:8000), and a workflow worker. The worker routes each in-progress
Communication and writes a FHIR `Task` back to the server. No real PHI is involved.

## Decision Packs

A pack is a directory:

```text
packs/patient-inbox-router/
├── pack.yaml              # identity, risk, card, questions, provider slots, evaluation config
├── policy.yaml            # thresholds, rules, per-label limits, fallback
├── schemas/               # input JSON Schema (the output schema is generated)
├── fhir/mapping.yaml      # FHIR R4 → input, decision → Task
├── logic/                 # deterministic rules (red flags, keyword routing)
├── models/                # trained, calibrated local model + training provenance
├── data/                  # synthetic train / calibration / test splits
├── tests/cases.yaml       # behaviour tests every variant must pass
├── evals/benchmark.json   # published evaluation report
├── DECISION_CARD.md       # generated by `openhealth card`
└── CHANGELOG.md
```

The core idea is that **a pack declares questions, not models.** Each question has a fixed set
of labels with human-readable criteria. Providers answer questions with a probability
distribution. The criteria text documents the pack for clinicians and is also, verbatim, the
instruction that Jev or an LLM receives. The policy then turns answers into an outcome:

1. **Rules first.** For example, `urgency=urgent` (p ≥ 0.35) becomes `HUMAN_REVIEW` → nurse,
   priority `stat`.
2. **Thresholds** on the primary question: AUTO ≥ 0.90 (≥ 0.95 for non-clinical
   destinations), review ≥ 0.50, abstain below.
3. **Caps that only reduce automation:** per-label limits, any provider failure, and the risk
   level.

### Risk levels are enforced by the runtime

| Level | Kind | Automation |
|---|---|---|
| 0 | Administrative (scheduling, routing, billing) | AUTO allowed by policy |
| 1 | Clinical information processing | AUTO allowed; audited |
| 2 | Clinical support | Human review by default; AUTO needs a recorded `GovernanceOverride` |
| 3 | Patient-specific prediction | As level 2, with stronger validation expected |
| 4 | Time-critical / diagnostic / treatment | Never autonomous |

A deploying organization lifts the level 2–3 cap with a named approver and a reference, for
example `GovernanceOverride(approved_by="CMIO", reference="AI-committee-2026-09")`. Every
affected audit record then carries it.

### Evaluation is part of the product

`openhealth eval` reports accuracy, per-label precision/recall/F1, calibration (ECE, Brier,
reliability bins), outcome rates, abstention, false automation rate, expected human override
rate, critical-label safety (for example "urgent cases automated: 0"), latency, cost, subgroup
performance and a threshold sweep. Reports contain case ids but never case text, so a site can
share an evaluation of its local data without sharing the data.

`openhealth leaderboard` compares provider configurations on the same records and publishes
the result in `evals/leaderboard.json` and the Decision Card. Configurations whose credentials
exist only on a deployment (such as Jev through Vercel Connect) run through the deployment's
budgeted evaluation endpoint, and their reports are merged in with `--report`:

```bash
uv run openhealth leaderboard openhealth/patient-inbox-router --dataset test --limit 100 \
  --dataset red_flags --dataset red_flags_holdout \
  --run router=naive_bayes,safety_screen=rules --run router=llm,safety_screen=rules_and_llm
vercel curl /api/packs/openhealth/patient-inbox-router/evals --deployment <url> -- -s -X POST \
  -H 'content-type: application/json' \
  -d '{"dataset": "test", "limit": 100, "providers": {"router": "jev"}}' > jev-test.json
uv run openhealth leaderboard openhealth/patient-inbox-router --report jev-test.json
```

## Feedback loop

Anyone using the dashboard can rate any decision, automated ones included, with 👍 or 👎 and an
optional note ("what was wrong?"). Each vote is appended to the decision's hash-chained audit
trail. The **Feedback** view lists the 👎 cases, and a 👎 on an AUTO decision counts as
"confidently wrong".

The system never retrains or rewrites itself from votes. Instead, the 👎 cases are downloaded
as a dataset to label:

```bash
openhealth feedback export openhealth/patient-inbox-router --db "$DATABASE_URL" --out down.jsonl
```

Fill in each record's `labels`. The file then works with `openhealth eval --dataset`, and
confirmed misses become behaviour tests in the pack's `tests/cases.yaml`. Improvements ship as
a new, reviewed and versioned pack.

## Jev

Jev answers bounded, typed choice questions ("which team?", "is documentation missing?",
"does this need review?") and returns a probability for every label. That makes it a natural
decision primitive for packs. The adapter:

- maps pack questions 1:1 onto Jev questions (`choice`; `boolean` → `noul`; `score` → `score`,
  at most 10 levels), with every Jev question of a decision in one call;
- sends only `state_fields` (minimum necessary), over HTTPS;
- uses the chosen label's probability as the answer's confidence, like every other provider.
  Jev's own `confidence` summarises the shape of the whole distribution, so it is kept as
  evidence (`jev confidence 0.81`) rather than compared with policy thresholds;
- validates responses strictly: model match, label sets, probabilities sum to 1, choice is the
  argmax, token usage bounds, 64 KB cap. A malformed answer fails only the question it answers;
- retries once after `429` or `529 Overloaded` (a short backoff or a short `Retry-After`);
- records cost at the published price, $0.042 per million input tokens (output tokens are free);
- turns every failure (`not_configured`, `timeout`, `rate_limited`,
  `credential_unavailable`, `budget_exhausted`, `invalid_response`, …) into a safe policy
  outcome, never an automated guess.

`jev-latest` is an alias (currently `jev-1.13.0`). Every answer records the version that
actually answered; pin a version in `JEV_MODEL` when thresholds have been tuned against it.
Jev's documented weak spots for 1.13 (docs.typesafe.ai, "Jev 1.13 jaggedness") matter for pack
authors: it reads instructions literally, it is hurt by instructions that contradict the
criteria, adversarial text in the input can move its answers, and English is its strongest
language.

The endpoint and model are deployment settings, not part of any pack:

| Variable | Purpose |
|---|---|
| `JEV_API_URL` | Endpoint to call (required) |
| `JEV_MODEL` | Model id (required; recorded in every answer's provenance) |
| `JEV_CONNECTOR` | Vercel Connect connector that supplies the key, e.g. `jev/openhealth-hub` |
| `JEV_API_KEY` | Key for local use instead of a connector |
| `OPENHEALTH_JEV_DAILY_LIMIT` | Daily call budget (default 500; `none` for no limit) |

```bash
JEV_API_URL=... JEV_MODEL=... JEV_API_KEY=... \
  uv run openhealth eval openhealth/patient-inbox-router --dataset test --provider router=jev
```

## LLMs and local models

The `llm` provider asks a chat model for a probability for every label, through any
OpenAI-compatible API. By default it calls the **Vercel AI Gateway**, which serves Claude,
open-weight models such as `openai/gpt-oss-120b`, and others behind one API. On Vercel it
authenticates with the deployment's OIDC token, so no key is stored. Point `LLM_BASE_URL` at
Ollama, vLLM or llama.cpp to run an open-source model on your own hardware instead.

- A JSON schema fixes the answer's shape: one probability per label per question. Anything
  else is an `invalid_response`, and so are probabilities that don't sum to about 1.
- Input text is data. The pack's questions and criteria form the system prompt; the input is
  sent as JSON, and the model is told to classify it, not to follow it.
- Verbalised probabilities are not logprobs. `openhealth calibrate` fits a temperature per
  question on the pack's calibration split (live calls), and the evaluation report shows ECE.
- Cost per decision is the gateway's reported cost, recorded in every audit record.

| Variable | Purpose |
|---|---|
| `LLM_MODEL` | Default model for `llm` variants (default `anthropic/claude-sonnet-5`; a variant's `model` wins) |
| `LLM_BASE_URL` | OpenAI-compatible endpoint instead of the AI Gateway, e.g. `http://localhost:11434/v1` |
| `AI_GATEWAY_API_KEY` | Gateway key for local use (on Vercel, the OIDC token is used) |
| `LLM_API_KEY` | Key for a non-gateway endpoint, if it needs one |
| `OPENHEALTH_LLM_DAILY_LIMIT` | Daily call budget (default 500; `none` for no limit) |

```bash
vercel env pull .env.local   # a fresh OIDC token for local calls through the gateway
vercel env run -- uv run openhealth calibrate <pack> --slot router --variant llm --limit 100
```

## Deploying to Vercel

The whole engine (API, FHIR endpoints, CDS Hooks, review queue, audit, dashboard) deploys as one
FastAPI function (`[tool.vercel] entrypoint = "openhealth.server.vercel:app"`).

1. Connect a Postgres database (Neon via the Vercel Marketplace) for Production and Preview.
   It provides `DATABASE_URL`. On Vercel the app refuses to start without it, because functions
   have no persistent disk.
2. Attach the Jev connector to the project and set `JEV_CONNECTOR`, `JEV_API_URL`, `JEV_MODEL`
   and `OPENHEALTH_PROVIDERS=router=jev`. The key is fetched at runtime with the deployment's
   OIDC identity; it is never stored in the project's environment variables. With several
   packs, scope a setting to one pack (`openhealth/clinical-trial-prescreener:notes=jev`); a
   setting a pack cannot use is skipped and listed in `/api/health`.
3. Set Vercel Authentication to protect **all deployments**: the app has no login of its own,
   and the default setting leaves the production domain public. After each deploy, check that
   an unauthenticated request to production gets redirected to login.
4. Push a branch to get a preview deployment, check it with
   `vercel curl /api/health --deployment <url>`, then merge to `main` for production.

`/api/health` reports the storage backend, active providers, Jev configuration and today's Jev
usage (never secrets). The FHIR sandbox (HAPI) stays in local `docker compose`.

Days and times people see (the dashboard's times and daily volume, daily call counters, "today"
for criteria and ages, card dates) use `OPENHEALTH_TIMEZONE`, an IANA name defaulting to
`America/New_York`. Stored timestamps, the audit hash chain, API JSON and FHIR stay UTC.

Preview and production deployments share the same Neon database unless Neon's preview branching
is enabled, so preview traffic appears in the production audit trail. With preview branching,
each preview gets a `preview/<git branch>` database branch. The Vercel-managed integration only
deletes it when Vercel deletes the deployment (months later), and a full branch limit makes
previews fail with "Resource provisioning failed". The `neon-preview-cleanup` workflow deletes
the branch when its pull request closes; it needs a project-scoped Neon API key as the
`NEON_API_KEY` repository secret and the Neon project id as the `NEON_PROJECT_ID` variable.

## Project layout

```text
src/openhealth/
├── core/        spec, pack loading/digest, registry, runtime, policy, audit, Decision Card
├── providers/   rules, naive_bayes (local ML), jev
├── standards/   fhir (R4 mapping, Task output), cds_hooks
├── evals/       metrics, harness, local-vs-benchmark comparison, pack behaviour tests
├── server/      FastAPI app (REST, FHIR, CDS Hooks, review, audit, evals)
├── dashboard/   dependency-free HTML/JS developer dashboard
└── sandbox/     synthetic data generator, FHIR seed/worker, demo
packs/           reference Decision Packs
datasets/        example "local site" dataset for validation
docs/            spec, architecture, ADRs
```

## Development

```bash
uv sync
uv run pytest                          # 112 tests
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy src tests
./scripts/build-reference-pack.sh      # regenerate data → train → test → benchmark → card
```

## Roadmap

- **v0.2:** LLM provider (structured output with calibration), CQL provider, remote hub
  (`openhealth pull`, signed packs), `AuditEvent`/`Provenance` export, authentication on the
  server, drift monitoring from recorded outcomes, a harder clinician-reviewed synthetic
  benchmark.
- **Standards:** SMART on FHIR launch, Bulk FHIR evaluation, OMOP, Da Vinci CRD/DTR/PAS.

## License

Apache-2.0. See [LICENSE](LICENSE).
