# OpenHealth Hub roadmap: v0.1 → v1.0

_Approved 2026-09-28. Living document: update it as milestones land._

## Context

The goal is an open infrastructure layer, a "Hugging Face for healthcare decisions": Decision
Packs that are packaged, evaluated, shared, versioned, governed and deployed across rules, Jev,
ML, local models, LLMs and combinations of them, with healthcare standards, human deferral and
local validation built in.

v0.1 is done and running in production on Vercel. Decisions for what follows: **packs
first** (the Hub only matters once it has five strong packs), **LLMs through Vercel AI Gateway**
(OIDC, no stored keys), and a **public open-source launch at v1.0**.

**Progress (2026-09-29):** v0.1 ✅, v0.2 ✅, v0.3 ✅, and the dashboard redesign
([plan](ui-ux-review.md), 8 PRs) ✅. Next: **v0.4 Healthcare standards**, starting with its plan.

## Where we are against the goal

| Goal area | Status | Main gap |
|---|---|---|
| Decision Pack spec (schemas, FHIR mapping, policy, tests, evals, card, versions) | ✅ 0.2 | `choice`, `boolean` and `score` questions; derived questions (decision tables) |
| Runtime + AUTO / HUMAN_REVIEW / ABSTAIN / BLOCK + risk levels 0–4 | ✅ | Governance approvals in the UI (v0.6) |
| Providers: rules, naive Bayes, Jev, LLMs (Vercel AI Gateway; OpenAI-compatible local models), criteria, dosing; `any_of`, `cascade`, `vote` | ✅ | **CQL** (v0.7) |
| Reference packs | ✅ 5 of 5 | A harder, clinician-reviewed benchmark |
| Evaluation (accuracy, calibration, abstention, false automation, overrides, latency, cost, subgroups) + local validation + provider leaderboards for every pack | ✅ | Drift over time, multi-site comparison, a harder benchmark |
| Audit trail (hash chain), review queue, feedback (right / wrong, with the right answer, changeable) | ✅ | FHIR AuditEvent/Provenance export (v0.4) |
| FHIR R4, CDS Hooks (prefetch only; `order-sign`, `patient-view`, message hook), synthetic sandbox (HAPI) | Partial | US Core validation, SMART on FHIR, Bulk FHIR, OAuth/OIDC, CDS Hooks fetch (v0.4); later CQL, OMOP, Da Vinci CRD/DTR/PAS |
| Decision Cards | ✅ | Validation history, hub-hosted cards |
| Dashboard (every pack; review, audit, evaluations, feedback, card, Try it; both themes; keyboard and accessibility-tree checked) | ✅ redesigned | A VoiceOver check; roles (v0.6) |
| Hub: publish / pull / search, publisher types, datasets, eval suites, benchmarks | ❌ | The whole registry and website |
| Developer experience (`pip install`, `from_hub`, `pull`, container per pack) | Partial | PyPI release, remote hub, container images |
| Org readiness (auth, tenancy, PHI handling, monitoring) | Minimal (Vercel SSO only) | All of it |
| Community (contribution flow, verification, public repo) | ❌ | v1.0 |

## Milestones

Each milestone gets its own detailed plan for your approval before any code is written, and
ships as several reviewed PRs.

### v0.2 Model-agnostic core (foundation for the new packs) ✅
_Done 2026-09-29. Question groups shipped as derived questions (decision tables with exact
probabilities). The harder inbox benchmark did not ship and is an open item in
`tasks/todo.md`._
- **Spec 0.2:** new question types `boolean` (maps to Jev `noul`) and `score` alongside
  `choice`; **question groups with aggregation policies** (e.g. "any criterion = no → not
  eligible; any = unknown → review"), which the trial, PA and AI Guard packs all need; derived
  outputs. Migrate the inbox pack, with a spec migration note.
- **LLM provider** through Vercel AI Gateway (OIDC, as the Jev connector already works): Claude
  by default, with structured output of per-label probabilities, calibration fitted on a
  held-out split, and strict validation with safe failure like Jev. The same provider with an
  OpenAI-compatible `base_url` covers **local/open-source models** (Ollama, vLLM).
- **Composition:** add `cascade` (a cheap provider first, escalating to an expensive one only
  when uncertain) and weighted voting, next to `any_of`.
- **Jev:** send every Jev-answered question of a decision in one call (today the router and the
  emergency screen make 2), run slots in parallel, and support the `noul` and `score` types.
- **Harder inbox benchmark:** paraphrased messages reviewed by a clinician, plus held-out splits
  for every critical label (from lesson 2026-09-28).
- **Exit:** a provider leaderboard for the inbox pack (rules vs NB vs Jev vs Claude vs a local
  model, with accuracy, calibration, false automation, cost and latency); one Jev call per
  decision; all red-flag sets fully caught with the deployed provider mix.

### v0.3 The other four reference packs (content release) ✅
_Done 2026-09-29: all five packs deployed with provider leaderboards and the providers chosen
from them. Risk levels as shipped: Referral Router L1, Prior Authorization Preflight L1,
Clinical Trial Prescreener L2, AI Guard L2 (`POST /api/packs/{id}/guard`)._
Each pack gets questions and criteria, rules + Jev + LLM variants, a policy with a justified
risk level, synthetic data (public, local-shift and held-out safety sets), behaviour tests, a
benchmark, a Decision Card, a FHIR mapping and a CDS Hook.
- **Referral Router** (L0–1): ServiceRequest + Condition + DocumentReference. Answers
  specialty, workflow destination, a per-item missing-documentation checklist, and needs-review.
  Hook: `order-sign`.
- **Prior Authorization Preflight** (L1): order + Coverage + attached documents, checked against
  synthetic payer requirement sets. Each element is present / missing / unclear. Complete →
  submit-ready; otherwise review with the gaps listed. Hook: `order-sign`. This is the bridge to
  Da Vinci later.
- **Clinical Trial Prescreener** (L2, so human review by default): synthetic ResearchStudy
  criteria plus the patient record (Condition, Observation, MedicationStatement). Each
  criterion is yes / no / unknown; uncertain cases go to review with the criteria listed. Hook:
  `patient-view`.
- **AI Guard** (L1–2): a question, an LLM draft reply and optional context. Checks for unsafe
  advice, dosing errors, missing emergency escalation, scope violations and claims unsupported
  by the context. Outcome is pass / review / **block**. Exposed as an API middleware.
- **Exit:** all five packs pass tests, benchmarks and held-out safety sets, and run through the
  same runtime, API, dashboard (multi-pack) and deployment.

### v0.4 Healthcare standards
- US Core profile validation of FHIR inputs; audit export as FHIR AuditEvent + Provenance.
- **SMART on FHIR** EHR launch (the dashboard opens in patient/encounter context), using
  **OAuth 2 / OpenID Connect**.
- **CDS Hooks** that fetch from `fhirServer` with `fhirAuthorization` (SMART backend services,
  with a server allow-list against SSRF), in addition to prefetch.
- **Bulk FHIR** `$export` ingestion into local-validation datasets.
- Exit: the inbox and referral packs run from a SMART launch against the HAPI sandbox; Bulk
  export → `openhealth eval` works end to end.

### v0.5 The Hub (the "Hugging Face" part)
- A pack distribution format: an immutable, versioned archive with a digest and signature.
  `openhealth login / push / pull / search`; `Decision.from_hub` fetches, verifies and caches
  remote packs.
- **Registry service** (FastAPI + Neon + Vercel Blob) with namespaces and publisher types
  (**community / verified / institutional**), plus a verification process.
- **Hub website** (Next.js on Vercel: a public, searchable site where React is the right tool)
  with pages for packs (Decision Card, versions, benchmarks and provider leaderboards),
  datasets, eval suites, policies, evidence and FHIR mappings, and search by task, risk level,
  standard and publisher.
- Shareable local-validation reports (metrics only; reports already contain no case text), for
  site-to-site comparison.
- Exit: the five packs are published; on a clean machine
  `pip install openhealth && openhealth pull openhealth/referral-router` works.

### v0.6 Readiness for health organizations
- OIDC login with roles (viewer / reviewer / admin / governance), per-organization tenancy, and
  API keys for machine clients. This replaces relying only on Vercel SSO. Running an evaluation
  becomes admin-only in the dashboard and the API (today it asks for confirmation when it would
  call remote models; dashboard decision 4).
- A deployment registry (which pack version runs where), and **governance approvals in the UI**
  (level 2–3 AUTO overrides recorded; level 4 locked).
- **Monitoring:** confidence drift and observed accuracy over time (from reviews, outcomes and
  👎), alerts, site-specific performance, override trends, and cost per decision from provider
  pricing.
- **PHI mode:** hash-only or encrypted inputs, retention policies, access logging, and a
  deployment guide.
- A container image per pack (GHCR) and on-prem templates.
- Exit: a pilot organization could run the inbox router with its own IdP, PHI-safe settings,
  monitoring and governance approvals.

### v0.7 Later standards
- A **CQL** provider (a CQL engine as a pack provider type).
- **OMOP** CDM → pack inputs, for population-level evaluation.
- **Da Vinci CRD / DTR / PAS** wired into PA Preflight.

### v1.0 Public open-source launch
- Security review (threat model, dependency audit, API penetration test).
- A docs site, including a **YAML-first pack-authoring guide for clinicians**.
- A contribution flow: pack PRs are automatically validated, tested, evaluated and given a card.
- CODE_OF_CONDUCT and community governance.
- PyPI release, public GitHub repo, launch.
- Exit: a new user goes from README to their own local validation in under 10 minutes; the
  sandbox runs with `docker compose up`; the Hub is live with five packs.

## How every milestone is built
- **PR flow:** branch → PR → required CI checks → preview on its own Neon branch → verification
  → merge → production smoke tests, as we do today.
- **Eval gates:** no merge that regresses a pack's critical metrics, and every critical label
  has a held-out set that is never tuned on.
- **Feedback loop:** 👎 feedback and reported misses become behaviour tests, then a new pack
  version.
- **Record keeping:** `tasks/todo.md` tracks the plan; `tasks/lessons.md` records every
  correction.

## Risks
- **Synthetic data flatters models**, as we saw with the red-flag screen. Mitigation: held-out
  sets, paraphrase sets, clinician review.
- **Clinical correctness of pack content.** Clinician reviewers are needed for trial criteria,
  AI Guard checks and red flags.
- **LLM calibration and cost.** These are measured per pack; cascades and budgets control them.
- **Regulatory positioning** (FDA CDS guidance) for L2+ packs. Mitigation: clinician-facing,
  human review by default, and documented intended use.
- **PHI and security** once real organizations pilot. That is v0.6, and the repo stays private
  and synthetic-only until then.

## Open needs (not blocking v0.2's first PRs)
- Enable Vercel AI Gateway on the team, for the LLM provider.
- Clinician reviewers for pack content (by v0.3).
- Later: a Hub domain name and a publisher-verification policy.

## Next step
v0.2, planned in detail and approved before code is written.

## Verification of the roadmap itself
Each milestone's exit criteria are testable (commands, datasets, metrics). The roadmap counts
as achieved when every goal-area row in the scorecard above is ✅ and the v1.0 exit criteria
pass on a clean machine.
