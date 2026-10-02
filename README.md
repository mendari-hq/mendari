# Mendari

**Open infrastructure for reusable healthcare decision logic: a "Hugging Face for healthcare
decisions."**

Mendari turns healthcare workflow decisions (route this message, is this prior-auth packet
complete, does this AI answer need a clinician) into standardized, testable, versioned,
governed software components called **Decision Packs**. A pack does not depend on any one
model. Rules, [Jev](#jev), classical ML and (soon) LLMs all answer the same questions through
the same interface, and every decision ends in an explicit `AUTO`, `HUMAN_REVIEW`, `ABSTAIN` or
`BLOCK` with a hash-chained audit record.

> **Status: research preview** (roadmap milestones v0.1–v0.5 done; package 0.1.0, not released).
> Everything here runs on synthetic data. It is not a medical device. Organization sign-in and PHI
> mode are new (v0.6) and have not had a security review, so it must not be used with real PHI or
> real patients yet.

> Mendari was called **OpenHealth Hub** until 2026-09-30. Settings under the old `OPENHEALTH_`
> prefix still work (a `MENDARI_` setting wins when both are set), and so do old pack ids
> (`openhealth/…`) in the providers and packs settings, so existing deployments need no change.

```text
Clinical/EHR data (FHIR R4, CDS Hooks, REST)
        │
        ▼
  input schema ──► provider slots ──► decision policy ──► AUTO | HUMAN_REVIEW | ABSTAIN | BLOCK
                   rules │ Jev │ ML      thresholds, rules,        │
                   (swappable variants)  risk-level caps           ▼
                                                  audit trail · review queue · FHIR Task
```

## What's in it

| Item | Where |
|---|---|
| Decision Pack specification | [`docs/spec/decision-pack-0.2.md`](docs/spec/decision-pack-0.2.md) (0.1: [`decision-pack-0.1.md`](docs/spec/decision-pack-0.1.md)), [`src/mendari/core/spec.py`](src/mendari/core/spec.py) |
| Mendari runtime | [`src/mendari/core/decision.py`](src/mendari/core/decision.py), [`policy.py`](src/mendari/core/policy.py) |
| Providers: rules, naive Bayes, Jev, LLMs (Vercel AI Gateway, or OpenAI-compatible local models), criteria, CQL, dosing; combined with `any_of`, `cascade`, `vote` | [`src/mendari/providers/`](src/mendari/providers) |
| FHIR adapter + CDS Hooks | [`src/mendari/standards/`](src/mendari/standards) |
| Patient Inbox Router reference pack | [`packs/patient-inbox-router/`](packs/patient-inbox-router) ([Decision Card](packs/patient-inbox-router/DECISION_CARD.md)) |
| Clinical Trial Prescreener reference pack (v0.3) | [`packs/clinical-trial-prescreener/`](packs/clinical-trial-prescreener) ([Decision Card](packs/clinical-trial-prescreener/DECISION_CARD.md)) |
| Referral Router reference pack (v0.3) | [`packs/referral-router/`](packs/referral-router) ([Decision Card](packs/referral-router/DECISION_CARD.md)) |
| Prior Authorization Preflight reference pack (v0.3) | [`packs/prior-auth-preflight/`](packs/prior-auth-preflight) ([Decision Card](packs/prior-auth-preflight/DECISION_CARD.md)) |
| AI Guard reference pack (v0.3) | [`packs/ai-guard/`](packs/ai-guard) ([Decision Card](packs/ai-guard/DECISION_CARD.md)) |
| Human review / abstention | policy outcomes + [`core/audit.py`](src/mendari/core/audit.py) review queue |
| Evaluation harness | [`src/mendari/evals/`](src/mendari/evals) |
| Audit trail | SQLite or Postgres, append-only hash-chained events, `mendari audit verify` |
| Synthetic healthcare dataset | [`src/mendari/sandbox/synthetic_inbox.py`](src/mendari/sandbox/synthetic_inbox.py), [`synthetic_trial.py`](src/mendari/sandbox/synthetic_trial.py), [`synthetic_referral.py`](src/mendari/sandbox/synthetic_referral.py), [`synthetic_prior_auth.py`](src/mendari/sandbox/synthetic_prior_auth.py), [`synthetic_guard.py`](src/mendari/sandbox/synthetic_guard.py) |
| Dashboard | `mendari serve` → http://127.0.0.1:8000: every pack on the search path. Overview, a review queue by priority, the audit trail with each record in a drawer, evaluations (provider leaderboard, benchmark report with charts, runs on your datasets), feedback with the right answer, the Decision Card, and Try it (a form for every pack, generated from its input schema for record packs, starting from the pack's behaviour tests, with the JSON one click away). Light and dark themes; times in the deployment's time zone (US Eastern by default). |

## Quickstart

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run mendari demo
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

### Write your own pack

No programming needed: a pack is YAML. The [authoring guide](docs/guides/authoring-a-pack.md)
walks a clinician through it:

```bash
uv run mendari new refill-router --id my-clinic/refill-router
uv run mendari test refill-router
uv run mendari eval refill-router --dataset examples
```

### Python

```python
from mendari import Decision

router = Decision.from_hub("mendari/patient-inbox-router")
result = router({"message": {"body": "I've had chest pain for 30 minutes spreading to my arm"}})

result.outcome          # Outcome.HUMAN_REVIEW
result.recommendation   # 'nurse'
result.priority         # Priority.STAT
result.reasons          # ('rule possible-emergency: Possible emergency symptoms. ...',)
result.answers["urgency"].evidence   # ('urgent:chest-pain',)
```

Swap a provider without touching the pack's contract, policy or tests:

```python
Decision.from_hub("mendari/patient-inbox-router", providers={"router": "jev"})
```

### CLI

```bash
uv run mendari list                                      # packs on the search path
uv run mendari validate mendari/patient-inbox-router
uv run mendari test mendari/patient-inbox-router         # the pack's own behaviour tests
uv run mendari decide mendari/patient-inbox-router --text "Can I pay my bill online?"
uv run mendari eval mendari/patient-inbox-router --dataset datasets/local-site-example.jsonl
uv run mendari eval mendari/patient-inbox-router --dataset my-site.jsonl --fail-on-degradation
uv run mendari card mendari/patient-inbox-router         # render the Decision Card
uv run mendari test mendari/clinical-trial-prescreener
uv run mendari eval mendari/clinical-trial-prescreener --dataset exclusions_holdout
uv run mendari serve                                     # API + CDS Hooks + dashboard
uv run mendari audit verify --db var/mendari.db
```

### Guarding an LLM's replies

A pack with a `guard` section (the AI Guard reference pack is one) checks a model's draft
before anyone reads it. The draft is `released` only when the decision is automated with a
release recommendation; otherwise it is `held_for_review` or `blocked`, with the reasons:

```python
from mendari import Decision
from mendari.guard import check

guard = Decision.from_hub("mendari/ai-guard")
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

### Container images and on-prem templates

`docker build .` builds the engine with every pack. `--build-arg PACK=mendari/referral-router`
builds one that serves only that pack (`MENDARI_PACKS`). Tagging a release (`v*`) publishes
both kinds to GitHub Container Registry (workflow `images`), after testing each; nothing is
published before that. [`deploy/`](deploy) has templates for a site's own infrastructure:
docker compose on one server and Kubernetes. Both come with Postgres, your identity provider,
PHI mode and the daily retention and monitoring jobs.

### Docker sandbox

```bash
docker compose up --build
```

This starts a HAPI FHIR R4 server seeded with synthetic cases as US Core 6.1.0 resources:
inbox Patients and Communications, and referral Patients, ServiceRequests, notes, reports and
labs. Referral dates are moved to today, so the windows the pack checks stay the same. Then it
starts the Mendari engine (REST, FHIR, CDS Hooks, Jev adapter, review queue, audit, dashboard at
http://localhost:8000), a SMART launcher (smart-launcher-v2, in front of HAPI) and a workflow
worker. The worker routes each in-progress Communication and writes a FHIR `Task` back to the
server. No real PHI is involved.

A SMART EHR launch, scripted end to end (EHR and browser in one), decides a patient's referrals
or messages:

```bash
docker compose exec engine mendari sandbox smart-launch --pack mendari/referral-router \
  --engine http://engine:8000 --launcher http://launcher --fhir http://fhir:8080/fhir
```

**Bulk FHIR into local validation.** Export a site's data, then evaluate a pack on it with the
site's own labels (ground truth from chart review, joined by an identifier system):

```bash
mendari bulk export --fhir https://ehr.example.org/fhir --group my-cohort --out export/ \
  --client-id my-app --private-key key.pem --key-id k1        # SMART Backend Services
mendari bulk dataset --pack mendari/referral-router --ndjson export/ \
  --labels chart-review.jsonl --key-system https://site.example/mrn --out local.jsonl
mendari eval mendari/referral-router --dataset local.jsonl
```

The CI job `sandbox-e2e` runs the scripted launch for the inbox and referral packs on every
change. It also runs `mendari sandbox bulk-check`: an export of the sandbox must evaluate
exactly like the same cases read from the dataset file.

## Healthcare standards

FHIR inputs (`/api/packs/{pack}/fhir/decide`, CDS Hooks, `mendari decide --fhir`) first pass a
**structural R4 check** against HL7's R4 JSON Schema (digest-pinned and patched by
`scripts/fhir_r4_schema.py`): an unknown element or resource type, an element of the wrong JSON
type, or a primitive without its format is a `400` in every mode. This is a structural check,
not validation. Then they are checked against **US Core 6.1.0** in process. The checks cover
required elements, required bindings, the required category slices and the profiles'
`us-core-*` invariants. They are generated from the official packages by
`scripts/us_core_subset.py`. A complex element R4 itself requires, or a code outside an R4
required value set, is an issue of the same kind, labelled with the base R4 profile.
`MENDARI_FHIR_VALIDATION` chooses what happens to these issues:

| Setting | Effect |
|---|---|
| `warn` (default) | The decision is unchanged. The issues come back in `decision.input_issues` and are audited, as paths without values. |
| `enforce` | An input with an error is blocked before any provider sees it, like a JSON Schema failure. |
| `off` | No profile or required-element checks (the structural check still runs). |

The audit trail exports as FHIR R4. Each event in the hash chain becomes an **AuditEvent**, and
each decision, review and downstream outcome becomes a **Provenance** on the decision's `Task`.
They carry ids, codes and the chain's hashes, never case text:

```bash
curl localhost:8000/api/audit/<decision id>/fhir                        # one decision, a Bundle
curl "localhost:8000/api/audit/export/fhir?pack_id=mendari/referral-router&since=2026-09-01"
mendari audit export --db var/mendari.db --out audit.ndjson             # FHIR NDJSON
```

CDS Hooks services fetch the prefetch data an EHR leaves out, using the pack's own prefetch
templates, the request's `fhirServer` and its `fhirAuthorization` token. They do this only when
that server's origin is listed in `MENDARI_FHIR_ALLOWED_SERVERS` (for example
`https://ehr.example.org,http://fhir:8080`). The list is empty by default, and every other server
is refused.

**SMART on FHIR EHR launch.** Register `https://<mendari>/smart/launch?pack=<pack id>` as
the app's launch URL and `https://<mendari>/smart/callback` as its redirect URI in the EHR.
Then set `MENDARI_SMART_CLIENT_ID`, `MENDARI_SMART_CLIENT_SECRET` (for a confidential
client), `MENDARI_PUBLIC_URL`, and put the EHR's FHIR server and authorization server on
`MENDARI_FHIR_ALLOWED_SERVERS`.

The launch is OAuth 2 with PKCE (S256). The `id_token` is verified against the server's JWKS
(`iss`, `aud`, `exp`, `nonce`). The access token stays on the server; the browser holds an
opaque, HttpOnly, SameSite=Lax session cookie. The dashboard opens on the launch's patient:
- `GET /api/smart/patient`;
- their messages or referrals: `GET /api/smart/focus?pack_id=`;
- deciding one: `POST /api/smart/decide`, which reads the record through the pack's CDS prefetch
  templates and audits the decision.

SMART is off until the client id is set.

**CQL.** A pack can compute answers with a CQL library (provider type `cql`), evaluated on a
CQL engine at `MENDARI_CQL_URL` (HAPI FHIR with Clinical Reasoning; the sandbox's HAPI has
it on). The trial pack's `cql` variant holds its criteria in CQL. `mendari agreement` shows
that it answers like `criteria` on every case, and the CI job `cql` checks it.

**Da Vinci CRD.** The Prior Authorization Preflight's `order-sign` service also returns, for
each order it decides, a system action that updates the order with CRD's `coverage-information`:
covered, whether prior authorization and documentation are needed, the patient's Coverage and
an assertion id, which is the decision id. The HL7 validator checks it against CRD 2.2.1 in CI.
A pack declares this with `cds_hooks.coverage_information`.

**Da Vinci DTR.** When documentation is missing, that coverage information links a questionnaire.
`POST /fhir/Questionnaire/$questionnaire-package` (with the Coverage and the order) returns it: the
documentation the payer asks for, for that service and payer, pre-answered from the pack's
decision, which is found by the CRD assertion id on the order. A clinician completes only what is
missing. It is validated against DTR 2.2.0 in CI, and a pack declares it with
`cds_hooks.questionnaire`.

**Da Vinci PAS.** For a complete request (`submit_ready`), `POST /api/packs/{pack}/pas` (or
`mendari pas build`) builds the PAS request Bundle. It holds a `preauthorization` Claim, the
patient, Coverage, payer, requesting organization and order from the EHR, and the X12 service
type and place of service. Nothing is filled in: a missing element is an error that names it.
With `submit`, it goes to the payer's `Claim/$submit` (`MENDARI_PAS_PAYER_URL`, allow-listed),
and the payer's decision (outcome, authorization number, review actions) is chained on the
decision. The sandbox has a mock payer (`mendari sandbox payer`, compose service `payer`).
The request and the payer's answer validate against PAS 2.2.1 in CI.

**OMOP CDM.** Research data in OMOP (SQLite or Postgres, with an optional schema such as `cdm`)
becomes record-pack inputs, one per person, for population-level evaluation:

```bash
mendari omop run --db "$OMOP_URL" --schema cdm --pack mendari/clinical-trial-prescreener
mendari omop dataset --db "$OMOP_URL" --schema cdm --pack mendari/clinical-trial-prescreener \
  --labels chart-review.jsonl --out local.jsonl    # then: mendari eval ... --dataset local.jsonl
```

`omop run` reports the outcome mix and each question's answers, with cells under 10 people
suppressed. `mendari sandbox omop-check` writes a dataset into OMOP, reads it back and checks
that it evaluates identically (in CI).

The CI job `fhir-conformance` runs the HL7 FHIR validator over the same fixtures and a sample of
the audit export. Everything
Mendari treats as conforming must pass it, and everything the in-process checks flag must
fail it.

## Documentation

Every page in [`docs/`](docs) builds into a documentation site with MkDocs Material:

```bash
uv sync --group docs
uv run mkdocs serve    # http://127.0.0.1:8000
```

CI builds it in strict mode, so a broken link fails. It will be hosted once the repository is
public.

## Security

[`SECURITY.md`](SECURITY.md) says how to report a vulnerability and how to deploy safely. The
threat model ([`docs/security/threat-model.md`](docs/security/threat-model.md)) lists every
threat with its mitigation and the test that checks it. On every change, the CI job `security`
audits the Python and Hub-site dependencies and runs an OWASP ZAP scan of the API with sign-in
on. Every response carries a strict CSP and the usual isolation headers.

## The Hub

Packs travel as **signed archives**. A registry keeps them with **immutable versions**, and anyone
can pull and verify them:

```bash
mendari keys generate --out keys/                           # a publisher key pair (Ed25519)
mendari login --registry https://hub.example.org --token <publisher token>
mendari keys register --namespace my-site --public-key keys/publisher.pub
mendari push packs/my-pack --key keys/publisher.key          # build, sign, publish

pip install mendari && mendari pull mendari/referral-router            # anyone
mendari search referral --standard "CDS Hooks"
mendari eval mendari/referral-router --dataset local.jsonl --share "My site"
```

- **An archive** (`.ohpack`) is deterministic and carries a manifest of file hashes signed with
  the publisher's key.
- **`pull`** checks the archive digest the registry recorded, every file hash, the signature and
  the pack digest before caching the pack. Namespace keys are pinned on first use, and a changed
  key needs `--accept-new-key`. `Decision.from_hub` pulls on its own when a pack isn't local.
- **The registry** (`mendari registry serve`) has namespaces with a publisher type
  (community, verified, institutional) that its admins set after verification. `--share` sends
  a site's metrics, never its cases.
- **The website** (`hub/`, Next.js) searches packs and shows their cards, leaderboards and site
  reports.

The docker sandbox runs a registry on port 8100 (`scripts/hub_seed.py` publishes the five packs).

## Sign-in for organizations

By default the server is open, as production sits behind Vercel Authentication. Setting an
identity provider switches on sign-in:

```bash
MENDARI_OIDC_ISSUER=https://login.example.org/realms/clinic   # your IdP (OpenID Connect)
MENDARI_OIDC_CLIENT_ID=mendari
MENDARI_OIDC_ROLE_MAP="clinicians=reviewer,informatics=admin" # IdP role or group -> role
MENDARI_PUBLIC_URL=https://mendari.example.org
```

With a provider that has no roles, such as Google, set `MENDARI_OIDC_ROLE_CLAIM=email` and map
verified emails to roles ([the Google setup](docs/deploying-with-phi.md#2-sign-in-and-roles)).

People sign in with their organization's account, using PKCE and an `id_token` verified with the
IdP's keys. Machines use **API keys**, which admins create in `POST /api/auth/keys`; they are
shown once, stored hashed and revocable. Every route has a permission, and anything unlisted
needs admin:

| Role | May |
|---|---|
| viewer | read the dashboard and API |
| reviewer | also decide (Try it), resolve reviews, record outcomes, give feedback |
| service | an integration: read, decide, record outcomes |
| governance | read, approve automation |
| admin | everything: evaluations, exports, API keys |

With sign-in on, reviews and ratings are recorded for whoever is signed in, and the dashboard
names them and offers only what their role may do. `MENDARI_OIDC_ORG_CLAIM` names the claim
that carries the person's organization: one deployment can serve several, and each sees only
its own decisions, reviews, feedback and reports.

**Governance approvals.** At risk level 2 or 3 a person reviews every decision (ADR-0002) until
the organization approves automation. Someone with the `governance` role gives the approval
from the Decision Card, with a reference such as committee minutes and an expiry. It covers one
exact pack version: a new digest needs a new approval. It is written into every decision it
affects, chained in the audit log, and can be revoked at once. Level 4 can't be approved.

**Monitoring.** The dashboard's Monitoring view follows a pack in use, for the organization you
act for. It shows observed accuracy against the benchmark (from reviews, downstream outcomes and
thumbs-downs), the human override trend, confidence drift (PSI against the benchmark's
confidences), the automated share and cost per decision, week by week. Alerts fire only on
enough evidence:

- accuracy below the benchmark by more than the pack's tolerance;
- drift of 0.25 or more;
- an automated decision rated wrong;
- an override rate that doubled.

For a scheduler (cron, a Kubernetes CronJob), `mendari monitor` exits 1 on any alert and
posts them to `--webhook` / `MENDARI_ALERT_WEBHOOK` (https only):

```bash
mendari monitor --db "$DATABASE_URL" --org clinic-a --webhook https://alerts.example.org/hook
```

**PHI mode.** `MENDARI_PHI_MODE=hash` keeps no input at all, only its hash.
`MENDARI_PHI_MODE=encrypt` keeps inputs encrypted (AES-256-GCM, `MENDARI_PHI_KEY`) apart
from the audit chain. Only signed-in reviewers and admins read them, and each read is chained.
`mendari phi purge` applies `MENDARI_RETENTION_DAYS` without breaking the chain. Read
[`docs/deploying-with-phi.md`](docs/deploying-with-phi.md) before any real patient data.

**Deployments.** Each server records which pack version it runs, with which providers, in
which environment (`MENDARI_ENVIRONMENT`) and release, whenever that changes. The Decision
Card lists them (`/api/deployments`).

**The pilot, end to end.** `mendari sandbox pilot-check --engine URL` plays a pilot
organization against an engine with sign-in and PHI mode `encrypt`. People sign in through the
real OpenID Connect flow, an admin creates a key for the message worker, and the worker
decides. A reviewer reads the queue (each input read is logged) and resolves; a viewer can't.
The AI committee approves a level 2 pack, then monitoring and the audit chain are checked. CI
runs it against [mock-oauth2-server](https://github.com/navikt/mock-oauth2-server) (job
`pilot-check`).

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
├── DECISION_CARD.md       # generated by `mendari card`
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

`mendari eval` reports accuracy, per-label precision/recall/F1, calibration (ECE, Brier,
reliability bins), outcome rates, abstention, false automation rate, expected human override
rate, critical-label safety (for example "urgent cases automated: 0"), latency, cost, subgroup
performance and a threshold sweep. Reports contain case ids but never case text, so a site can
share an evaluation of its local data without sharing the data.

`mendari leaderboard` compares provider configurations on the same records and publishes
the result in `evals/leaderboard.json` and the Decision Card. Configurations whose credentials
exist only on a deployment (such as Jev through Vercel Connect) run through the deployment's
budgeted evaluation endpoint, and their reports are merged in with `--report`:

```bash
uv run mendari leaderboard mendari/patient-inbox-router --dataset test --limit 100 \
  --dataset red_flags --dataset red_flags_holdout \
  --run router=naive_bayes,safety_screen=rules --run router=llm,safety_screen=rules_and_llm
vercel curl /api/packs/mendari/patient-inbox-router/evals --deployment <url> -- -s -X POST \
  -H 'content-type: application/json' \
  -d '{"dataset": "test", "limit": 100, "providers": {"router": "jev"}}' > jev-test.json
uv run mendari leaderboard mendari/patient-inbox-router --report jev-test.json
```

## Feedback loop

Anyone using the dashboard can rate any decision, automated ones included, as right (thumbs
up) or wrong (thumbs down), with an optional note. A thumbs-down can also say what the answer
should have been, for the decision itself or for one of its checks
(`POST /api/audit/{id}/feedback` with `"expected": {"question": ..., "label": ...}`); the label
must be one the decision could have given. Each rating is appended to the decision's
hash-chained audit trail. A person can change their rating: their latest one counts, and the
audit trail keeps both (people are told apart by reporter name; unnamed ratings of a decision
count as one person). The **Feedback** view lists the thumbs-down cases, and a thumbs-down on an
AUTO decision counts as "confidently wrong".

The system never retrains or rewrites itself from ratings. Instead, the thumbs-down cases are
downloaded as a dataset:

```bash
mendari feedback export mendari/patient-inbox-router --db "$DATABASE_URL" --out down.jsonl
```

Records whose raters gave the right answer (and agree) already carry it in `labels`, so they are
ready-made test cases; fill in the others' `labels`. The file then works with
`mendari eval --dataset`, and confirmed misses become behaviour tests in the pack's
`tests/cases.yaml`. Improvements ship as a new, reviewed and versioned pack.

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
| `MENDARI_JEV_DAILY_LIMIT` | Daily call budget (default 500; `none` for no limit) |

```bash
JEV_API_URL=... JEV_MODEL=... JEV_API_KEY=... \
  uv run mendari eval mendari/patient-inbox-router --dataset test --provider router=jev
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
- Verbalised probabilities are not logprobs. `mendari calibrate` fits a temperature per
  question on the pack's calibration split (live calls), and the evaluation report shows ECE.
- Cost per decision is the gateway's reported cost, recorded in every audit record.

| Variable | Purpose |
|---|---|
| `LLM_MODEL` | Default model for `llm` variants (default `anthropic/claude-sonnet-5`; a variant's `model` wins) |
| `LLM_BASE_URL` | OpenAI-compatible endpoint instead of the AI Gateway, e.g. `http://localhost:11434/v1` |
| `AI_GATEWAY_API_KEY` | Gateway key for local use (on Vercel, the OIDC token is used) |
| `LLM_API_KEY` | Key for a non-gateway endpoint, if it needs one |
| `MENDARI_LLM_DAILY_LIMIT` | Daily call budget (default 500; `none` for no limit) |

```bash
vercel env pull .env.local   # a fresh OIDC token for local calls through the gateway
vercel env run -- uv run mendari calibrate <pack> --slot router --variant llm --limit 100
```

## Deploying to Vercel

The whole engine (API, FHIR endpoints, CDS Hooks, review queue, audit, dashboard) deploys as one
FastAPI function (`[tool.vercel] entrypoint = "mendari.server.vercel:app"`).

1. Connect a Postgres database (Neon via the Vercel Marketplace) for Production and Preview.
   It provides `DATABASE_URL`. On Vercel the app refuses to start without it, because functions
   have no persistent disk.
2. Attach the Jev connector to the project and set `JEV_CONNECTOR`, `JEV_API_URL`, `JEV_MODEL`
   and `MENDARI_PROVIDERS=router=jev`. The key is fetched at runtime with the deployment's
   OIDC identity; it is never stored in the project's environment variables. With several
   packs, scope a setting to one pack (`mendari/clinical-trial-prescreener:notes=jev`); a
   setting a pack cannot use is skipped and listed in `/api/health`.
3. Set Vercel Authentication to protect **all deployments**: the app has no login of its own,
   and the default setting leaves the production domain public. After each deploy, check that
   an unauthenticated request to production gets redirected to login.
4. Push a branch to get a preview deployment, check it with
   `vercel curl /api/health --deployment <url>`, then merge to `main` for production.

`/api/health` reports the storage backend, active providers, Jev configuration and today's Jev
usage (never secrets). The FHIR sandbox (HAPI) stays in local `docker compose`.

Days and times people see (the dashboard's times and daily volume, daily call counters, "today"
for criteria and ages, card dates) use `MENDARI_TIMEZONE`, an IANA name defaulting to
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
src/mendari/
├── core/        spec, pack loading/digest, registry, runtime, policy, audit, Decision Card
├── providers/   rules, naive_bayes (local ML), jev
├── standards/   fhir (R4 mapping, Task output), cds_hooks
├── evals/       metrics, harness, local-vs-benchmark comparison, pack behaviour tests
├── server/      FastAPI app (REST, FHIR, CDS Hooks, review, audit, evals)
├── dashboard/   developer dashboard: HTML, CSS and ES modules, no dependencies or build step
└── sandbox/     synthetic data generator, FHIR seed/worker, demo
packs/           reference Decision Packs
datasets/        example "local site" dataset for validation
docs/            spec, architecture, ADRs
```

## Development

```bash
uv sync
uv run pytest                          # unit and integration tests
uv run playwright install chromium     # once, for the dashboard's browser tests
uv run pytest -m browser               # the dashboard in a real browser (free: default providers)
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy src tests
./scripts/build-reference-pack.sh      # regenerate data → train → test → benchmark → card
```

## Roadmap

Full plan and status: [`docs/roadmap.md`](docs/roadmap.md). Done:
- v0.1 runtime and inbox pack;
- v0.2 model-agnostic core (LLMs, composition, leaderboards);
- v0.3 the other four packs;
- the dashboard redesign;
- v0.4 healthcare standards: US Core, AuditEvent/Provenance, SMART on FHIR, CDS Hooks fetch,
  Bulk FHIR;
- v0.5 the Hub: signed pack archives, `mendari push / pull`, a registry and website (built, not
  public yet);
- v0.6 readiness for organizations: sign-in with roles and API keys, tenancy, governance
  approvals, monitoring and drift, PHI mode, container images and on-prem templates;
- v0.7 later standards: a CQL provider, OMOP CDM inputs, Da Vinci CRD, DTR and PAS;
- v1.0, everything up to the launch: a security review with scans in CI, the pack-authoring
  guide and `mendari new`, the docs site, the contribution flow with its eval gate,
  community files, a release workflow and the 10-minute quickstart check.

Next: **the public launch**. The repository goes public, 0.1.0 goes to PyPI, and the Hub and
docs site go live. That is the owner's decision.

## License

Apache-2.0. See [LICENSE](LICENSE).
