# Mendari roadmap: v0.1 → v1.0

_Approved 2026-09-28. Living document: update it as milestones land._

## Context

The goal is an open infrastructure layer for healthcare AI agents: agents that are wrapped,
evaluated, shared, versioned, governed and deployed across rules, Jev, ML, local models, LLMs,
external HTTP agents and combinations of them, with healthcare standards, human deferral and
local validation built in.

The product bundles one demo agent, `mendari/portal-message-agent`. The milestones below are
kept as they shipped: the five agents of v0.1 to v0.3 are no longer bundled, and are now test
fixtures.

v0.1 is done and running in production on Vercel. Decisions for what follows: **agents
first** (the Hub only matters once it has strong agents), **LLMs through Vercel AI Gateway**
(OIDC, no stored keys), and a **public open-source launch at v1.0**.

**Progress (2026-09-30):**
- v0.1 ✅, v0.2 ✅, v0.3 ✅;
- the dashboard redesign ([plan][plan-dashboard], 8 PRs) ✅, then the design grade phases A–C
  (C+ → B+);
- **v0.4 Healthcare standards ✅** ([plan][plan-v0.4], #66–#74): both
  exits run in CI on every change (`sandbox-e2e`).

- **v0.5 The Hub ✅** ([plan][plan-v0.5], #75–#82), built and verified: signed
  archives, the registry, push and pull, shared reports and the website. A clean-venv install
  and pull runs in CI (`hub-e2e`). Going public (a public registry, the site, PyPI) waits for
  your decision.

- **v0.6 Readiness for health organizations ✅** ([plan][plan-v0.6],
  #83–#89): sign-in with the organization's identity provider, roles, API keys and tenancy;
  governance approvals and the deployment registry; monitoring with alerts; PHI mode; per-agent
  images and on-prem templates. The pilot runs in CI against a real OpenID Connect provider
  (`pilot-check`). Nothing is switched on in production, which stays behind Vercel
  Authentication.

- **v0.7 Later standards ✅** ([plan][plan-v0.7], #90–#95):
  - a CQL provider, with eligibility criteria in CQL agreeing with `criteria` on every case;
  - OMOP CDM inputs with population reports;
  - Da Vinci CRD, DTR and PAS around a prior-authorization agent, each profile-validated against
    its guide in CI (HL7 validator, offline): CRD 2.2.1, DTR 2.2.0 and PAS 2.2.1. That checks what Mendari
    produces. How DTR's `$questionnaire-package` behaves is checked weekly by the Inferno DTR
    payer suite v0.18.0, with the gaps it lists (`sandbox/inferno/`). Neither shows whether a
    payer accepts what Mendari sends.

- **v1.0 Public open-source launch, built** ([plan][plan-v1.0], #96–#101):
  - a security review (threat model; dependency audit and an OWASP ZAP scan in CI; headers);
  - the clinician's authoring guide and `mendari new`;
  - the docs site;
  - the contribution flow with its eval gate, and the community files;
  - a release workflow, and the README quickstart timed in CI (it finishes well under 10
    minutes).

Now: **the launch itself**, which waits for your go-ahead: the public repository, the PyPI
release, the live Hub and the hosted docs site. The overnight log lists what's needed.

## Where we are against the goal

| Goal area | Status | Main gap |
|---|---|---|
| Agent spec (schemas, FHIR mapping, policy, tests, evals, card, versions) | ✅ 0.2 | `choice`, `boolean` and `score` questions; derived questions (decision tables) |
| Runtime + AUTO / HUMAN_REVIEW / ABSTAIN / BLOCK + risk levels 0–4, governance approvals in the UI | ✅ | none |
| Providers: rules, naive Bayes, Jev, LLMs (Vercel AI Gateway; OpenAI-compatible local models), criteria, CQL, dosing; `any_of`, `cascade`, `vote` | ✅ | none |
| Bundled agents | ✅ the demo agent | A harder, clinician-reviewed benchmark |
| Evaluation (accuracy, calibration, abstention, false automation, overrides, latency, cost, subgroups) + local validation + provider leaderboards for every agent; monitoring over time with drift and alerts | ✅ | A harder benchmark |
| Audit trail (hash chain), review queue, feedback (right / wrong, with the right answer, changeable), FHIR AuditEvent/Provenance export | ✅ | none |
| FHIR R4 with US Core 6.1.0 checks, CDS Hooks (prefetch, or fetch from allow-listed servers), SMART on FHIR EHR launch (OAuth 2 PKCE, OIDC), Bulk FHIR `$export` into local datasets, CQL, OMOP CDM, Da Vinci CRD/DTR/PAS, synthetic sandbox (HAPI with Clinical Reasoning, SMART launcher, mock payer) | ✅ v0.7 | none |
| Decision Cards | ✅ | Validation history, hub-hosted cards |
| Dashboard (every agent; review, audit, evaluations, monitoring, feedback, card, Try it; roles; both themes; keyboard and accessibility-tree checked) | ✅ redesigned | A VoiceOver check |
| Hub: publish / pull / search, publisher types, signed archives, the site evaluation network (verified sites, tickets, signed counts, pooled site evidence apart from every rank), website | ✅ v0.5 (not public yet; the site network built, LP-22) | Going public (a public registry and site); datasets and eval suites as first-class entries |
| Developer experience (`pip install`, `from_hub`, `pull`, container per agent) | Partial | The PyPI release and the first published images (both at a release tag) |
| Org readiness (auth, tenancy, PHI handling, monitoring) | ✅ v0.6 (off until configured) | A security review before real PHI (v1.0) |
| Community (contribution flow, verification, public repo) | ✅ built (v1.0) | The repo going public (your decision) |

## Milestones

Each milestone gets its own detailed plan for your approval before any code is written, and
ships as several reviewed PRs.

### v0.2 Model-agnostic core (foundation for the new agents) ✅
_Done 2026-09-29. Question groups shipped as derived questions (decision tables with exact
probabilities). The harder message-routing benchmark did not ship and is an open item in
`tasks/todo.md`._
- **Spec 0.2:** new question types `boolean` (maps to Jev `noul`) and `score` alongside
  `choice`; **question groups with aggregation policies** (e.g. "any criterion = no → not
  eligible; any = unknown → review"), which eligibility, prior-authorization and draft-checking agents all
  need; derived
  outputs. Migrate the message-routing agent, with a spec migration note.
- **LLM provider** through Vercel AI Gateway (OIDC, as the Jev connector already works): Claude
  by default (removed in LP-10c: the deployment's `LLM_MODEL` names the model), with
  structured output of per-label probabilities, calibration fitted on a held-out split, and
  strict validation with safe failure like Jev. The same provider with an OpenAI-compatible
  `base_url` covers **local/open-source models** (Ollama, vLLM).
- **Composition:** add `cascade` (a cheap provider first, escalating to an expensive one only
  when uncertain) and weighted voting, next to `any_of`.
- **Jev:** send every Jev-answered question of a decision in one call (today the router and the
  emergency screen make 2), run slots in parallel, and support the `noul` and `score` types.
- **Harder message-routing benchmark:** paraphrased messages reviewed by a clinician, plus held-out splits
  for every critical label (from lesson 2026-09-28).
- **Exit:** a provider leaderboard for the message-routing agent (rules vs NB vs Jev vs Claude vs a local
  model, with accuracy, calibration, false automation, cost and latency); one Jev call per
  decision; all red-flag sets fully caught with the deployed provider mix.

### v0.3 Four more agents (content release) ✅
_Done 2026-09-29: all five agents deployed with provider leaderboards and the providers chosen
from them. Risk levels as shipped: L1 for the referral and prior-authorization agents, L2 for
the prescreening agent and the draft checker (`POST /api/agents/{id}/guard`)._
Each agent gets questions and criteria, rules + Jev + LLM variants, a policy with a justified
risk level, synthetic data (public, local-shift and held-out safety sets), behaviour tests, a
benchmark, a Decision Card, a FHIR mapping and a CDS Hook.
- **A referral agent** (L0–1): ServiceRequest + Condition + DocumentReference. Answers
  specialty, workflow destination, a per-item missing-documentation checklist, and needs-review.
  Hook: `order-sign`.
- **A prior-authorization agent** (L1): order + Coverage + attached documents, checked against
  synthetic payer requirement sets. Each element is present / missing / unclear. Complete →
  submit-ready; otherwise review with the gaps listed. Hook: `order-sign`. This is the bridge to
  Da Vinci later.
- **A clinical-trial prescreening agent** (L2, so human review by default): synthetic ResearchStudy
  criteria plus the patient record (Condition, Observation, MedicationStatement). Each
  criterion is yes / no / unknown; uncertain cases go to review with the criteria listed. Hook:
  `patient-view`.
- **A draft checker for LLM replies** (L1–2): a question, an LLM draft reply and optional context. Checks for unsafe
  advice, dosing errors, missing emergency escalation, scope violations and claims unsupported
  by the context. Outcome is pass / review / **block**. Exposed as an API middleware.
- **Exit:** all five agents pass tests, benchmarks and held-out safety sets, and run through the
  same runtime, API, dashboard (every agent in one) and deployment.

### v0.4 Healthcare standards ✅
_Done 2026-09-30 (#66–#74). Both exits run in CI on every change: a scripted SMART launch of the
message-routing and referral agents against HAPI and the SMART launcher, and a Bulk export
whose evaluation must be identical to the same cases read from the dataset file._
- US Core profile validation of FHIR inputs; audit export as FHIR AuditEvent + Provenance.
- **SMART on FHIR** EHR launch (the dashboard opens in patient/encounter context), using
  **OAuth 2 / OpenID Connect**.
- **CDS Hooks** that fetch from `fhirServer` with `fhirAuthorization` (SMART backend services,
  with a server allow-list against SSRF), in addition to prefetch.
- **Bulk FHIR** `$export` ingestion into local-validation datasets.
- Exit: the message-routing and referral agents run from a SMART launch against the HAPI
  sandbox; Bulk export → `mendari eval` works end to end.

### v0.5 The Hub (the "Hugging Face" part) ✅
_Built and verified 2026-09-30 (#75–#82): signed deterministic archives, the registry (namespaces,
publisher types, keys, immutable versions, search), `login / push / pull / search` with verified
and pinned pulls, shared site reports, and the Next.js website. CI runs the exit on a clean
virtual environment. It is not deployed publicly: that needs production signing keys and the
go-ahead to publish (see the overnight log)._
- An agent distribution format: an immutable, versioned archive with a digest and signature.
  `mendari login / push / pull / search`; `Decision.from_hub` fetches, verifies and caches
  remote agents.
- **Registry service** (FastAPI + Neon + Vercel Blob) with namespaces and publisher types
  (**community / verified / institutional**), plus a verification process.
- **Hub website** (Next.js on Vercel: a public, searchable site where React is the right tool)
  with pages for agents (Decision Card, versions, benchmarks and provider leaderboards),
  datasets, eval suites, policies, evidence and FHIR mappings, and search by task, risk level,
  standard and publisher.
- Shareable local-validation reports (metrics only; reports already contain no case text), for
  site-to-site comparison.
- Exit: the five agents are published; on a clean machine
  `pip install mendari && mendari pull <agent id>` works.

### v0.6 Readiness for health organizations ✅
_Built and verified 2026-09-30 (#83–#89). Every control is off until configured. The exit runs
in CI (`pilot-check`): through a real OpenID Connect provider, an admin creates a key, a worker
decides with it, a reviewer reads encrypted inputs (logged) and resolves, a viewer can't, the
committee approves a level 2 agent, and monitoring and the chain check out._
- OIDC login with roles (viewer / reviewer / admin / governance), per-organization tenancy, and
  API keys for machine clients. This replaces relying only on Vercel SSO. Running an evaluation
  becomes admin-only in the dashboard and the API (today it asks for confirmation when it would
  call remote models; dashboard decision 4).
- A deployment registry (which agent version runs where), and **governance approvals in the UI**
  (level 2–3 AUTO overrides recorded; level 4 locked).
- **Monitoring:** confidence drift and observed accuracy over time (from reviews, outcomes and
  👎), alerts, site-specific performance, override trends, and cost per decision from provider
  pricing.
- **PHI mode:** hash-only or encrypted inputs, retention policies, access logging, and a
  deployment guide.
- A container image per agent (GHCR) and on-prem templates.
- Exit: a pilot organization could run the message-routing agent with its own IdP, PHI-safe settings,
  monitoring and governance approvals.

### v0.7 Later standards ✅
_Built and verified 2026-09-30 (#90–#95). The CQL port agrees with `criteria` on 750 cases in
CI (job `cql`); OMOP round-trips identically; the seven Da Vinci samples are profile-validated
against CRD 2.2.1, DTR 2.2.0 and PAS 2.2.1 (HL7 validator, offline)._
- A **CQL** provider (a CQL engine as an agent provider type).
- **OMOP** CDM → agent inputs, for population-level evaluation.
- **Da Vinci CRD / DTR / PAS** wired into the prior-authorization agent. Since 2026-10-17, DTR's
  `$questionnaire-package` answers as DTR 2.2.0 asks: the union of the orders' packages and the
  questionnaires asked for, a warning for a questionnaire not found (oper-9) or an order that
  names no decision, and a `422` OperationOutcome for orders or a context that name no
  decision, with nothing to return (spec-130). Since 2026-10-18 the Inferno DTR payer suite
  v0.18.0 runs against the engine weekly and by hand (CI workflow `conformance`), with Inferno
  as the DTR client: every test must give its expected result, and each known gap has its
  requirement id and reason (`sandbox/inferno/`).

### v1.0 Public open-source launch (built; the launch is yours)
_Built 2026-09-30 (#96–#101). The security scans, docs build, the eval gate on agent pull
requests and the README quickstart run in CI; `docker compose up` runs in `sandbox-e2e`. Left for the owner: making the
repository public, the PyPI release (trusted publishing is set up in `release.yml`), and hosting
the Hub and the docs site._
- Security review (threat model, dependency audit, API penetration test).
- A docs site, including a **YAML-first authoring guide for clinicians**.
- A contribution flow: agent PRs are automatically validated, tested, evaluated and given a card.
- CODE_OF_CONDUCT and community governance.
- PyPI release, public GitHub repo, launch.
- Exit: a new user goes from README to their own local validation in under 10 minutes; the
  sandbox runs with `docker compose up`; the Hub is live with its agents.

## How every milestone is built
- **PR flow:** branch → PR → required CI checks → preview on its own Neon branch → verification
  → merge → production smoke tests, as we do today.
- **Eval gates:** no merge that regresses an agent's critical metrics, and every critical label
  has a held-out set that is never tuned on.
- **Feedback loop:** 👎 feedback and reported misses become behaviour tests, then a new agent
  version.
- **Record keeping:** `tasks/todo.md` tracks the plan; `tasks/lessons.md` records every
  correction.

## Risks
- **Synthetic data flatters models**, as we saw with the red-flag screen. Mitigation: held-out
  sets, paraphrase sets, clinician review.
- **Clinical correctness of agent content.** Clinician reviewers are needed for eligibility criteria,
  draft-reply checks and red flags.
- **LLM calibration and cost.** These are measured per agent; cascades and budgets control them.
- **Regulatory positioning** (FDA CDS guidance) for L2+ agents. Mitigation: clinician-facing,
  human review by default, and documented intended use.
- **PHI and security** once real organizations pilot. That is v0.6, and the repo stays private
  and synthetic-only until then.

## Open needs (not blocking v0.2's first PRs)
- Enable Vercel AI Gateway on the team, for the LLM provider.
- Clinician reviewers for agent content (by v0.3).
- Later: a Hub domain name and a publisher-verification policy.

## Next step
Publish the v1.0 open-source release (public repository, PyPI package, container images, and
hosted docs/Hub) and focus on real-world adoption of Mendari as an open-source healthcare AI
governance and evaluation platform — clinician-reviewed evaluation sets, transparent provider
benchmarking, auditable human-in-the-loop policies, and community-contributed agents.

## Verification of the roadmap itself
Each milestone's exit criteria are testable (commands, datasets, metrics). The roadmap counts
as achieved when every goal-area row in the scorecard above is ✅ and the v1.0 exit criteria
pass on a clean machine.

<!-- The plans are project notes, kept in the repository outside the docs site. -->
[plan-dashboard]: https://github.com/mendari-hq/mendari/blob/main/notes/ui-ux-review.md
[plan-v0.4]: https://github.com/mendari-hq/mendari/blob/main/notes/plans/v0.4-healthcare-standards.md
[plan-v0.5]: https://github.com/mendari-hq/mendari/blob/main/notes/plans/v0.5-the-hub.md
[plan-v0.6]: https://github.com/mendari-hq/mendari/blob/main/notes/plans/v0.6-org-readiness.md
[plan-v0.7]: https://github.com/mendari-hq/mendari/blob/main/notes/plans/v0.7-later-standards.md
[plan-v1.0]: https://github.com/mendari-hq/mendari/blob/main/notes/plans/v1.0-public-launch.md
