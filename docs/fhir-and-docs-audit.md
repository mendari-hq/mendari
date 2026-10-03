# FHIR and API docs audit

Audited 2026-10-01 on branch `claude/openhealth-fhir-audit-8823f7` (base `560d14e`). Mendari
was called OpenHealth Hub until 2026-09-30. The deployment under review is
`openhealth-hub-gray.vercel.app`.

## How this audit was run

- **Production, signed in.** To an anonymous client, every path on
  `openhealth-hub-gray.vercel.app`, including `/docs`, `/openapi.json` and `/api/health`, answers
  `302` to `vercel.com/sso-api` (Vercel Authentication). With the owner signed in to Vercel in
  the browser, I called every read endpoint, ran Swagger's "Try it out" on `/decide`, and made
  one successful FHIR call (results below). Production runs release `560d14efa38c`, the same
  commit this branch starts from, so the local findings apply to production unchanged.
  - Production configuration from `/api/health`: Postgres storage, sign-in **off**, SMART
    launch off, FHIR allow-list empty, FHIR validation `warn`. The packs run on remote models:
    Jev `jev-1.13.0` for the inbox and referral routers and parts of the others, and
    `anthropic/claude-sonnet-5` through the AI Gateway for the AI Guard.
  - **Production writes made by this audit:** two decisions in the production audit trail, both
    synthetic. `ac3ce9f1-eddb-4fcb-a520-401d1fb590d9` (`BLOCK`, Swagger's default body) and
    `f965e5a8-9142-4dfd-a79f-dfc9a09e91fb` (`AUTO`, the US Core inbox fixture, one Jev call).
    The `500` checks fail before a decision is stored.
- **Locally**, the same app was built by the same `create_app` that
  `src/mendari/server/vercel.py` calls, with SQLite in place of Postgres and local providers in
  place of Jev. This was for endpoints I didn't want to call in production (evaluations, review,
  feedback, governance writes) and for the crash reproduction.
- **HL7 FHIR Validator 6.10.4** (the version CI pins, checksum verified) ran in the
  `eclipse-temurin:21-jre` image through `scripts/fhir_validate.py --docker`.
- **HAPI FHIR public R4 server** (`hapi.fhir.org/baseR4`) was used for comparison with read-only
  requests and one `$validate` call, which does not store anything. Only synthetic content was
  sent.
- **No real PHI was used or seen.** The data comes from `mendari.sandbox.synthetic_*` and the
  repository's fixtures. In production, all 142 stored decisions (2026-09-28 and 09-29, under the
  pre-rename `openhealth/…` pack ids) and 41 feedback entries were checked: 33 distinct inputs,
  all pack examples or hand-typed test messages ("Jev credential check: can I pay my bill
  online?"). A scan for SSNs, phone numbers, emails, MRNs, dates of birth, addresses, person
  names and identifiers found only medication and lab names, a payer id `acme`, and one
  `birthDate` of `1975-01-01` in a synthetic CDS Hooks example. Reviewers are `sandbox-nurse`
  and `verification-rn`.

## Verdict

**What Mendari is:** a decision engine. Organizations put healthcare workflow decisions
("which team gets this patient message", "is this prior-auth packet complete") into versioned
**Decision Packs**. Each decision ends as `AUTO`, `HUMAN_REVIEW`, `ABSTAIN` or `BLOCK`, with a
hash-chained audit record, a review queue and an evaluation harness. Its own REST API (`/api/…`)
is plain JSON, not FHIR.

**What it is not:** a FHIR server. It keeps decision records, not FHIR resources, and serves none. There is no
`/metadata`, no `/fhir/Patient`, no search, no create/update/history. Every one of those paths
returns `404 {"detail":"Not Found"}`. It also isn't a proxy or a facade over a FHIR server.

**Where FHIR actually appears:** at the edges, as an integration layer. That layer is more
sophisticated than "a thin wrapper" and has been done carefully:

| Role | What it does | Where |
|---|---|---|
| FHIR **consumer** | Takes a Bundle, a resource, or CDS Hooks prefetch; maps it onto a pack's input with a declarative `fhir/mapping.yaml` (a small FHIRPath subset) | `standards/fhir.py`, `packs/*/fhir/mapping.yaml` |
| Input **checker** | Checks inputs against a US Core 6.1.0 subset generated from the official packages; `warn` / `enforce` / `off` | `standards/us_core.py`, `us_core_6_1_0.json`, `scripts/us_core_subset.py` |
| FHIR **producer** | Emits a `Task` per decision; the audit trail as `AuditEvent` + `Provenance` (Bundle or NDJSON); a record as a `collection` Bundle for CQL | `standards/fhir.py`, `audit_fhir.py`, `record_fhir.py` |
| **CDS Hooks** service | Discovery and invocation, prefetch, cards | `standards/cds_hooks.py` |
| **SMART on FHIR** *app* (client) | EHR launch with PKCE, reads the patient from the EHR | `standards/smart.py`, `server/smart_routes.py` |
| FHIR **client** | Allow-listed, SSRF-guarded reads; Bulk Data `$export` client with SMART Backend Services | `standards/fhir_client.py`, `bulk.py` |
| **Da Vinci** CRD / DTR / PAS | CRD cards, a DTR `$questionnaire-package` operation, PAS request Bundles | `standards/davinci.py`, `pas.py` |
| Reference server | HAPI FHIR R4 in `docker-compose.yml` is the sandbox's FHIR server, not Mendari | `docker-compose.yml` |

**FHIR version:** R4 (4.0.1), applied consistently. `FhirMapping.fhir_version` only accepts
`"R4"`, US Core is 6.1.0 (R4), the validator runs with `-version 4.0.1`, and HAPI is configured
for R4. Nothing produces R4B or R5.

### Grades

| Judged as… | Grade | Why |
|---|---|---|
| **A FHIR server / FHIR API** (what the task asks about) | **F** | It doesn't exist. No CapabilityStatement, no RESTful resource interactions, no search, no versioning. The product doesn't claim to be one, but nothing on the docs page says so either. |
| **A FHIR-integrated decision service** (what it actually is) | **B−** | Clean R4 mapping; US Core checks derived from the official profiles and cross-checked by the HL7 validator in CI; SSRF-guarded client; CDS Hooks, SMART app launch, Bulk export, Da Vinci CRD/DTR/PAS. Weaknesses: errors are never `OperationOutcome`; malformed resources crash two code paths with `500`; parsing is hand-rolled `dict` walking; no CapabilityStatement for the FHIR operation it does expose. |
| **The `/docs` page** | **D** | The default Swagger UI with a one-line description, no explanation of the product, 39 untagged paths in one "default" group, 11 of 41 operations described, 1 example, no auth documented, light theme only. In production, "Try it out" pre-fills a body that gets `BLOCK`, and its curl doesn't work outside the browser. |

**Overall: C.** The FHIR work under the hood is better than the docs page suggests, but a
visitor can't tell what the product does, and anyone looking for a FHIR API finds nothing at the
usual paths.

## Endpoint status

**In production** (signed in to Vercel), every `GET` below returned the status listed, with the
same body shapes as locally: the FHIR server paths, packs, CDS Hooks discovery, audit (and
`verify`: `ok`, 292 events), stats, review, evals, deployments, governance, feedback,
monitoring, auth, SMART session, `/docs`, `/redoc` and `/openapi.json` (the same 39 paths, 41
operations, 11 described, no tags, no `securitySchemes`). `POST`s that change state were
exercised locally only, except the two noted. Anonymous callers get `302` to the Vercel login on
every path.

- **Working:** succeeds end to end with synthetic data.
- **Partial:** works, with a defect listed in the notes.
- **Needs config:** needs settings or an external service I didn't have (covered by the test
  suite, not verified here).
- **Not present:** the route doesn't exist.

### FHIR server paths (what a FHIR client would try first)

| Endpoint | Method | Status | Notes |
|---|---|---|---|
| `/metadata`, `/fhir/metadata` | GET | Not present | `404 {"detail":"Not Found"}`, not an `OperationOutcome`. HAPI returns a CapabilityStatement. |
| `/fhir/{type}`, `/fhir/{type}/{id}` | GET/POST/PUT/PATCH/DELETE | Not present | No resource storage. |

### Decisions and packs

| Endpoint | Method | Status | Notes |
|---|---|---|---|
| `/api/health` | GET | Working | Version, packs, storage, FHIR validation mode, allow-list, sign-in, provider settings. |
| `/api/packs` | GET | Working | Five reference packs. |
| `/api/packs/{ns}/{name}` | GET | Working | Manifest, schemas, benchmark, leaderboard, Decision Card. |
| `/api/packs/{ns}/{name}/datasets` | GET | Working | |
| `/api/packs/{ns}/{name}/examples` | GET | Working | The pack's behaviour tests, synthetic. |
| `/api/packs/{ns}/{name}/decide` | POST | Partial | **Prod: verified through Swagger "Try it out".** Works. Input that fails the pack's schema returns **`200` with `outcome: BLOCK`** (by design, so it is audited), but it isn't documented, and clients checking only the status code will miss it. A non-JSON body returns FastAPI's `422`, which **echoes the request body** in `input`. |
| `/api/packs/{ns}/{name}/guard` | POST | Working | AI Guard pack. |
| `/api/packs/{ns}/{name}/fhir/decide` | POST | Partial | **Prod: verified.** The US Core inbox fixture → `200`, `AUTO` → `medical_assistant` (0.98, Jev, ~300 ms), Task `status: ready`; `meta` as a string → `500 text/plain` in production too. Works with Bundles, single resources and prefetch maps; accepts `application/fhir+json`. **Crashes with `500`** when `meta` or `subject` is a string (`us_core.py:105`, `fhir.py` `to_task`). Unknown `resourceType`s are silently ignored. A wrong `status` code under the default `warn` mode returns `200` and a decision, with the issue only in `input_issues`. Missing focus resource → `422 {"error":"FhirError"}`, not an `OperationOutcome`. `application/fhir+xml` → `422`. |
| `/api/packs/{ns}/{name}/evals` | POST | Working | Ran on `test`, limit 20. |
| `/api/packs/{ns}/{name}/pas` | POST | Needs config | Builds the PAS Bundle; `submit: true` needs `MENDARI_PAS_PAYER_URL`. |
| `/api/packs/{ns}/{name}/feedback/export` | GET | Working | NDJSON. |
| `/api/providers/jev/models` | GET | Working | **Prod: verified** (`200`, model list). Unconfigured (locally): `400 ProviderError`; `503` would be more accurate. |

### Review, audit, governance, monitoring

| Endpoint | Method | Status | Notes |
|---|---|---|---|
| `/api/review` | GET | Working | |
| `/api/review/{decision_id}` | POST | Working | Covered by `tests/test_server.py`. |
| `/api/audit` | GET | Working | Filters, limit/offset paging. |
| `/api/audit/verify` | GET | Working | Hash chain verified. |
| `/api/audit/{id}` | GET | Working | Unknown id → `404 {"detail":…}`. |
| `/api/audit/{id}/fhir` | GET | Working | `collection` Bundle of AuditEvent + Provenance, `application/fhir+json`. |
| `/api/audit/export/fhir` | GET | Working | `application/fhir+ndjson`, streamed. |
| `/api/audit/{id}/outcome` | POST | Working | |
| `/api/audit/{id}/feedback` | POST | Working | |
| `/api/feedback` | GET | Working | |
| `/api/stats` | GET | Working | |
| `/api/evals` | GET | Working | |
| `/api/monitoring` | GET | Working | |
| `/api/deployments` | GET | Working | |
| `/api/governance` | GET / POST | Working | |
| `/api/governance/{id}` | DELETE | Working | |

### Standards endpoints

| Endpoint | Method | Status | Notes |
|---|---|---|---|
| `/cds-services` | GET | Working | Discovery for the packs that declare a hook. |
| `/cds-services/{id}` | POST | Partial | Works with prefetch. Errors are `{"error","detail"}`; CDS Hooks doesn't require `OperationOutcome`, but fetching missing prefetch needs `MENDARI_FHIR_ALLOWED_SERVERS` (empty by default). |
| `/fhir/Questionnaire/$questionnaire-package` | POST | Partial | Da Vinci DTR, `application/fhir+json` on success. The only route under `/fhir/`, but its errors are `422 {"error":"FhirError"}`, not an `OperationOutcome`, and no CapabilityStatement or OperationDefinition advertises it. |

### Sign-in and SMART

| Endpoint | Method | Status | Notes |
|---|---|---|---|
| `/api/auth/me` | GET | Working | Open mode: an "open mode" principal with `admin`. |
| `/api/auth/keys` | GET / POST / DELETE | Needs config | Needs OIDC configured. |
| `/auth/login`, `/auth/callback`, `/auth/logout` | GET / POST | Needs config | Hidden from OpenAPI. |
| `/smart/launch`, `/smart/callback` | GET | Needs config | Needs `MENDARI_SMART_CLIENT_ID` and an EHR; covered by `tests/test_smart*.py` and CI `sandbox-e2e`. |
| `/api/smart/session`, `/logout`, `/patient`, `/focus`, `/decide` | GET / POST | Needs config | Same. |

### Pages

| Endpoint | Method | Status | Notes |
|---|---|---|---|
| `/` | GET | Working | The dashboard (has its own design system, light and dark). |
| `/docs`, `/redoc`, `/openapi.json` | GET | Working | Default FastAPI pages. Load Swagger UI from `cdn.jsdelivr.net`; exempted from the CSP. |

## FHIR conformance scorecard

| # | Item | Grade | Evidence |
|---|---|---|---|
| 1 | **Conformance** (`/metadata`) | **Missing** | No CapabilityStatement anywhere (`server/app.py` has no such route). For what Mendari is, the right statement is a small one: no resource interactions, the `$questionnaire-package` operation, the SMART app, and CDS Hooks discovery. |
| 2 | **Resources and interactions** | **Missing as a server** | read / vread / search / create / update / patch / delete / history: none, for any type. As a *consumer*, mappings read Patient, Communication, ServiceRequest, Condition, Observation, MedicationStatement / MedicationRequest, DiagnosticReport, DocumentReference, Coverage (`packs/*/fhir/mapping.yaml`). As a *producer*: Task, AuditEvent, Provenance, Bundle, Da Vinci Claim / QuestionnaireResponse / Parameters. The sandbox worker uses `If-Match` conditional updates against HAPI (`sandbox/fhir_workflow.py`). |
| 3 | **Validation** | **Partial** | Strong on profiles: US Core 6.1.0 required elements, enumerable required bindings, category slices and `us-core-*` invariants, generated from the official package (`us_core.py`), and CI checks that everything flagged is also an error for the HL7 validator (`ci.yml` `fhir-conformance`). Weak on structure: there is no parse step, so wrong JSON types reach code that assumes dicts and crash (`500`). `operation_outcome()` exists in `us_core.py` but no endpoint returns it. The default is `warn`, so invalid input is decided on anyway. |
| 4 | **Search** | **Missing** (as a server) | No search parameters, modifiers, chaining, `_include`, `_sort`, `_count` or Bundle paging. As a client, `FhirClient` follows `next` links (up to 10 pages) under the base URL (`fhir_client.py`). |
| 5 | **Bundles** | **Partial** | Produces correct `collection` Bundles (audit, record), PAS Bundles that pass the Da Vinci IGs (see results), and reads `searchset` Bundles. No `searchset` is served; no `transaction` / `batch` endpoint. |
| 6 | **Versioning** | **Missing** | No `meta.versionId` / `lastUpdated` on produced resources, no ETags, no conditional requests served. The audit chain gives tamper evidence, which is a different property. |
| 7 | **Terminology** | **Partial, mostly correct** | Real systems with plausible codes: ICD-10-CM (`E11.9`), SNOMED CT (`44054006`), LOINC (`4548-4`), RxNorm (`record_fhir.py`, `sandbox/synthetic_trial.py`). Weak spots: `record_fhir._coding` guesses ICD-10 vs SNOMED by regex; Task `performerType`, `businessStatus` and audit subtypes use `https://mendari.co/fhir/CodeSystem/…` systems that are not published anywhere; `Task.code` is `text` only; the inbox mapping reads `Communication.medium` code without checking its system. No terminology server. |
| 8 | **Security** | **Partial** | No SMART *resource-server* scopes, because there are no FHIR resources to protect. Mendari is a SMART *app*: PKCE, default scopes `launch openid fhirUser patient/*.rs` (`smart.py`). Its own API has optional OIDC sign-in, roles and API keys with deny-by-default rules (`server/auth.py`). **Without OIDC configured, the API is open with admin rights.** Production is configured that way (`/api/health`: `sign_in: false`; `/api/auth/me`: an open-mode principal with `admin`), so its only protection is Vercel Authentication. Anyone on the Vercel team can approve automation, export case inputs, and run evaluations that call Jev and the LLM with no daily limit (`daily_limit: null`). The same gate also keeps out every external developer. No OAuth scheme in OpenAPI. Synthetic data only: every sandbox resource is tagged `https://mendari.co/fhir/sandbox|synthetic`, and the generators are template-based (not Synthea). No real PHI found. |
| 9 | **Interoperability test** | **Implemented for outputs; missing for a server** | HL7 validator: 0 errors across 18 files (Mendari's own outputs, sandbox inputs, Da Vinci samples), already enforced in CI. HAPI comparison: no equivalent behaviour, because there is no FHIR API. See next section. |
| 10 | **Errors** | **Partial** | Three error shapes: `{"error","detail"}` (Mendari errors), `{"detail": "…"}` (`HTTPException`, unknown routes), FastAPI's `{"detail":[…]}` validation list, which echoes the input. None is an `OperationOutcome`, including under `/fhir/`. Two crash paths return `500` with a plain-text body. Status codes are otherwise sensible (`404`, `409`, `422`), except `400` for an unconfigured provider and `200` for a blocked input. |

### Status after Phase 2 (2026-10-02)

The scorecard above is the audit as of 2026-10-01. After Phases 1 and 2:

| # | Item | Then | Now | What changed |
|---|---|---|---|---|
| 1 | Conformance | Missing | **Implemented** for what Mendari serves | `GET /fhir/metadata`: a CapabilityStatement built from the mounted routes (a test checks both ways), ETag and `304`, `date` from when the packs and release went live. No `implementationGuide`, `instantiates` or SMART claims. |
| 2 | Resources | Missing as a server | Unchanged **by design** | Mendari serves the conformance resources that describe it (CodeSystem, NamingSystem, OperationDefinition: read) and no clinical resources. It is a decision engine, not a FHIR data server. |
| 3 | Validation | Partial | **Implemented** for structure | A structural R4 gate from HL7's R4 JSON Schema (digest-pinned, patched, iterative): a `400` in every mode. Base-R4 required elements and enumerated codes, and US Core 6.1.0, follow `fhir_validation`. **Deviation from this audit:** a bad required code is a finding, not a `400` (the schema enumerates only some value sets, so making them a `400` would be inconsistent). |
| 4 | Search | Missing | Missing | Phase 3. |
| 5 | Bundles | Partial | Partial | `$decide` returns a `collection` Bundle that passes the HL7 validator. No `transaction`/`batch` (not planned: nothing stored to change). |
| 6 | Versioning | Missing | **Partial** | `meta.lastUpdated` on Task, AuditEvent and Provenance; `versionId` only on the immutable AuditEvent and Provenance; ETag on the CapabilityStatement. The Task's `versionId` waits for Phase 3; no `meta.profile` (no Mendari profiles exist). |
| 7 | Terminology | Partial | **Implemented** for Mendari's own codes | CodeSystems and NamingSystems generated from the code and served packs, served at `/fhir/CodeSystem/{id}` and `/fhir/NamingSystem/{id}`, and loaded into CI's validator, so a code Mendari emits that its CodeSystem lacks fails CI. External code systems are still checked without a terminology server. |
| 8 | Security | Partial | **Implemented** for this deployment | Google sign-in in production (Phase 1). The CapabilityStatement says in prose what authentication is, and that Mendari is not a SMART resource server. |
| 9 | Interoperability | Outputs only | **Wider** | CI validates the CapabilityStatement, the OperationDefinition, `$decide` Bundles (including a refused input), Tasks, the record Bundle and the terminology, and fails on any unexpected warning. |
| 10 | Errors | Partial | **Implemented** on FHIR routes | OperationOutcome everywhere on FHIR routes, `406`/`415` for non-JSON or non-R4 media types, never a `500` for bad input. |

Also fixed: rejection reasons quoted the input (case text in the audit record and the Task); they
are now built from the schema alone.

### Status after Phase 3 (2026-10-02)

| # | Item | After Phase 2 | Now | What changed |
|---|---|---|---|---|
| 2 | Resources | Conformance reads only | **Read-only decisions and audit trail** | A **Task per decision** (read, vread; [ADR-0003](decisions/ADR-0003-a-served-task-is-a-decision.md)): `requested` while it waits for a Mendari reviewer, `completed`, `failed` for refused input; no input text, no patient or subject references. **AuditEvent** and **Provenance** read, with absolute references to the Task (Provenance: the version it made). Still no clinical resources, no writes. |
| 4 | Search | Missing | **Implemented** for what is served | Task (`_id`, `identifier`, `status`, `business-status`, `priority`, `authored-on`, `_lastUpdated`, `_tag`), AuditEvent (`_id`, `date`, `entity`, `subtype`, `_lastUpdated`), Provenance (`_id`, `recorded`, `target`, `_lastUpdated`), CodeSystem (`_id`, `url`, `version`). GET and POST `_search`; UTC dates with prefixes; `_count`, `_total`, `_summary=count`; stateless keyset paging with `next` links; absolute fullUrls. Unknown parameters are ignored with a warning entry; unsupported standard ones (`patient`, `subject`, `focus`, …) and modifiers are a `400` (a deliberate deviation from R4's SHOULD-ignore, so a client never gets every patient's decisions while it believes it filtered by one). No chaining, `_include`, `_sort` or history Bundles. |
| 5 | Bundles | Partial | Partial | `searchset` Bundles that pass the HL7 validator. Still no `transaction`/`batch` (nothing stored to change). |
| 6 | Versioning | Partial | **Implemented** for what is served | Task versions follow the audit chain (`meta.versionId` 1, 2, 3; vread); ETag `W/"n"`; no conditional read, declared as such. The work-item Task `$decide` returns still has no `versionId` (the EHR's server assigns it). |

Tested by an oracle: for random searches over seeded decisions and events, the SQL result equals
filtering the rendered resources, page by page, on SQLite and Postgres, while organizations,
reviews and outcomes vary. Every read and search is scoped to the caller's organization in SQL,
needs the `read` permission (never public), and never writes. Also fixed: reviewing an ABSTAIN
rendered the reviewer as Provenance `verifier`; it is `author`.

### Status after Phase 4 (2026-10-02)

| # | Item | After Phase 3 | Now | What changed |
|---|---|---|---|---|
| 8 | Security | Implemented for this deployment | **Implemented**: sign-in, scoped machine access, SMART Backend Services (off by default) | API keys are `viewer` or `service`, expire (90 days, 365 at most; older keys 30 days after deploy) and take SMART scopes (`system/<Type>.rs`) or Mendari's URI scopes (decide, outcome). A deployment can turn on **SMART Backend Services**: `POST /auth/token` takes an RS384/ES384 client assertion (checked before any write; jti once per client) and returns a five-minute opaque token whose authorization is read on every request; `/fhir/.well-known/smart-configuration` and the CapabilityStatement say so only when it is on ([ADR-0004](decisions/ADR-0004-machine-access-tokens-and-scopes.md)). Managing keys and clients is people-only and chained. A bearer credential decides alone, never with a cookie fallback; never in a URL. |

Also fixed, found by the Phase 4 research: the public logout deleted session-table rows of any
kind by a key the caller chose (a pending sign-in, or replay protection); rows are now keyed per
kind, and deletes are scoped to a kind. Outcomes record who recorded them, take a system
identifier as `source` (no free text into the chain or FHIR) and a label of the pack. Still
behind Vercel Authentication: nothing external reaches the API until the owner lifts it.
**Phase 4b** (before that, or any PHI): review and feedback notes out of the chain, a keyed input
hash, an access log, rate limits and quotas, session idle timeout, Origin checks, supply-chain
pinning; then the external security review.

### Status after Phase 4b (2026-10-02)

| # | Item | After Phase 4 | Now | What changed |
|---|---|---|---|---|
| 8 | Security | Implemented: sign-in, scoped machine access, SMART Backend Services | **Hardened for real patient data on a site's own servers**, pending the external review | The chain commits to notes instead of copying them; the input hash is keyed; rate limits, quotas and a security log; 30-minute idle sessions under `__Host-` cookies, tied to the browser at sign-in; cross-site requests refused; the API reference vendored, every action and image pinned ([ADR-0005](decisions/ADR-0005-hardening-before-phi.md)). |

What changed, in three lanes and then integrated:
- **What is kept for good.** Review and rating notes and a payer's text left the chain for a
  `notes` table (sealed under `encrypt`, refused under `hash`), with a salted commitment in the
  chain, so a note can be erased (`note.erased`) and the chain still verifies. Under `hash` the
  record keeps no evidence values, and under `encrypt` they are sealed with the input. Input
  issues name a resource by type and position, not its id. The input hash is an HMAC under
  `MENDARI_INPUT_HASH_KEY`. People are chained by verified email, keys as `key <id>`. The FHIR
  audit export carries codes only; its CodeSystem gained `note.erased`, and the validator still
  passes.
- **What anyone can make Mendari do.** Public health is three fields; the capability statement
  takes no chain lock; sign-in caches the provider's configuration; bodies stop at 4 MiB.
  Costly routes are limited per caller, sign-in and the token endpoint per address, in the
  database (`429` with `Retry-After`, an OperationOutcome `throttled` on FHIR routes), with
  daily quotas per organization and one Vercel firewall rule on `/auth/` and `/smart/`.
- **What the browser and the build trust.** Sessions end after 30 minutes idle and 8 hours at
  most; roles are worked out per request; cookies are `__Host-`; a sign-in or launch works only
  in the browser that started it; a request another site starts reaches only public routes and
  pages, to read; HSTS. Swagger UI is vendored at a pinned version with its checksums, under
  the same CSP as every page. Actions are pinned by commit, images by digest, and Dependabot
  proposes updates monthly.
- **What is recorded for an incident.** JSON lines on stdout from an allow-list of fields: one
  access line per API request and the security events, a request id on every response.

**Reviewed** by five reviewers and a skeptic, who confirmed 12 findings (3 medium, 9 low; two
were found twice), all fixed: a CDS order hook is charged one decision per draft order, the
compose template trusts the proxy it ships with, the sign-in limit fits a hospital behind one
address, the deployment guide gives the real daily-limit defaults, a sign-in or launch started
on another host name is sent to the public URL, logout refuses a cross-site POST without a
cookie, library loggers no longer log URLs, the SMART launch is logged, the chain keeps a
payer's outcome and review actions only as codes, and `mendari decide --db` records under the
deployment's PHI mode.

Nobody is signed out by this release, the schema changes are additive (an index, the tables
`rate_limits`, `org_usage` and `notes`, the column `decision_inputs.details`), and the chain is
never rewritten. Still open:
- **The external security review**, with the packet in
  [`security/review-packet.md`](security/review-packet.md); then the owner's launch order:
  ceilings and quotas set, the firewall rule enforced after 7 days in log mode, then Vercel
  Authentication lifted.
- **The SMART launch** stays a second authenticator (its cookie reaches the EHR's data and
  decides without a Mendari principal), and the EHR's token is stored unsealed. Both are fixed
  before the SMART launch is turned on anywhere with sign-in.
- **The old cookie names** (`mendari_session`, `mendari_smart`) are still read on hosts that use
  `__Host-` names, for sessions from before Phase 4b; the next release stops reading them.

## Validator and reference-server comparison

### HL7 FHIR Validator 6.10.4 (R4 4.0.1, US Core 6.1.0, no terminology server)

This covers what CI checks, plus the Task and audit Bundle captured from the running endpoints
in this audit.

| File | What it is | Errors |
|---|---|---|
| `tests/fixtures/us-core/{inbox,prior-auth,referral,trial}.json` | US Core inputs the packs accept | 0 each |
| `var/fhir-samples/sandbox-inbox.json` (40 resources) | Sandbox Patients and Communications | 0 |
| `var/fhir-samples/sandbox-referral.json` (179 resources) | Sandbox referral records (ServiceRequest, Condition, Observation, DocumentReference, …) | 0 |
| `var/fhir-samples/audit-export.json` (17 resources) | AuditEvent and Provenance export | 0 |
| `Task` from `POST …/fhir/decide` (captured live) | Decision output | 0 (3 warnings) |
| `Bundle` from `GET /api/audit/{id}/fhir` (captured live) | One decision's audit trail | 0 (4 warnings) |
| `var/fhir-samples/davinci/` (7 files; with CRD 2.2.1, DTR 2.2.0, PAS 2.2.1) | CRD cards, DTR packages, PAS request and response | 0 each |
| `tests/fixtures/us-core/invalid/{records,referral}.json` | Resources the in-process checks reject (8 + 3 flagged) | the validator also errors on every flagged resource, so the in-process subset rejects nothing that US Core accepts |

**Warnings on the Task and audit Bundle** (`fhir_validate.py` counted only errors then, so I tallied
these by hand):
- Mendari's CodeSystems (`decision-outcome`, `inbox-destination`, `audit-event`) can't be found,
  so their codes can't be validated (roadmap Phase 2: publish them).
- `AuditEvent.subtype` uses a Mendari code that isn't in the (extensible) Audit Event Sub-Type
  value set.
- `dom-6`: no narrative (`text`) on Task, AuditEvent or Provenance.

**What this proves:** what Mendari emits is structurally valid R4, US Core-conformant where it
claims to be, and Da Vinci-conformant for the samples tested. **What it doesn't prove:** these are
the outputs Mendari chose to produce. The validator says nothing about the missing server
behaviour (items 1, 2, 4, 6), and it never sees the malformed inputs that crash `…/fhir/decide`.
Terminology was off (`-tx n/a`, as in CI), so LOINC, SNOMED and RxNorm codes were checked for
system and shape, not for existence.

### HAPI FHIR public server (`hapi.fhir.org/baseR4`) compared with Mendari

| Request | HAPI | Mendari |
|---|---|---|
| `GET /metadata` | `200` CapabilityStatement, `fhirVersion 4.0.1`, `application/fhir+json` | `404 {"detail":"Not Found"}`, `application/json` |
| `GET /Patient?_count=2` | `200` searchset Bundle, `self` + `next` links | `404` (no FHIR resource API) |
| `GET /Patient/does-not-exist` | `404` OperationOutcome | `404 {"detail":"Not Found"}` |
| `GET /Patient?birthdate=notadate` | `400` OperationOutcome | not applicable |
| `GET /Patient?unknownparam=x` | `400` OperationOutcome | not applicable |
| `GET /Observation?code=http://loinc.org\|4548-4&_include=Observation:subject` | `200` searchset with the included Patient | not applicable |
| Communication with `status: "bogus-status"` | `$validate`: `400` OperationOutcome (parse error on the code) | `/fhir/decide`, default `warn`: **`200`**, `ABSTAIN`, issue in `decision.input_issues`; `enforce`: **`200`**, `BLOCK` |
| Communication with `subject: "bad"` (wrong JSON type) | rejected at parse, `400` OperationOutcome | **`500 Internal Server Error`** |
| Unknown `resourceType` in a Bundle | rejected at parse | silently ignored |

HAPI is a FHIR server and behaves like one. Mendari isn't, so the comparison mostly shows the
absence of a FHIR API, plus the two places where Mendari takes FHIR input and is laxer than a
conformant parser.

## Docs page findings

### What a first-time visitor needs in two minutes

**A developer** needs to know:
1. What it does, in one sentence, with a picture.
2. One request that works: a `curl` to `/decide` with a synthetic body, and the response.
3. How to authenticate.
4. Where FHIR fits: "send a Bundle to `…/fhir/decide`, get a decision and a FHIR Task back", plus
   CDS Hooks and SMART.

**A clinician** needs to know:
1. What decisions it makes, and what happens when it isn't sure (`HUMAN_REVIEW`, `ABSTAIN`).
2. Where to try one without code: the dashboard's Try it.

The page gives none of this. It gives a list of URLs.

### Assessment

| Area | Finding |
|---|---|
| **Overview** | **Missing.** The page header is "Mendari 0.1.0 OAS 3.1" and the one line "Decision Packs: run, review, audit and evaluate healthcare decisions." No explanation of a pack, the four outcomes, or FHIR. No architecture diagram, although a good one already exists in the README and `docs/architecture.md`. |
| **Endpoints** | **Ungrouped.** 39 paths (41 operations) in one "default" tag, in code order. 11 operations have a description; 1 has an example. Request bodies for `/decide`, `/fhir/decide`, `/guard` and `/cds-services/{id}` are typed as a bare `object`, so Swagger shows `{}` as the example. Responses are `object` too. The CDS Hooks, DTR, SMART and sign-in routes aren't explained, and some are hidden. |
| **Getting started** | **Missing.** No quickstart, no curl, no auth explanation (no `securitySchemes`), no CapabilityStatement to link to. The good quickstart is in the README and the mkdocs site (`mkdocs.yml`), and the API docs don't link to either. |
| **Try it out** | **Works in production, for the team only, and the first result is a failure.** Signed in to Vercel, "Try it out" on `/decide` pre-fills `{"additionalProp1": {}}` and leaves the path parameters empty. With `mendari` / `patient-inbox-router` typed in, Execute returns **`200` with `outcome: BLOCK`** ("input rejected"), and that is recorded in the production audit trail. A valid body is in the `examples` endpoint, which the page doesn't mention. The "Curl" box Swagger shows **doesn't work outside the browser**: without the Vercel cookie it gets a `302` to the Vercel login. So no external developer can try anything. If OIDC sign-in is turned on, Swagger also has no way to send a bearer key (no `securitySchemes`). |
| **Visual quality** | Default Swagger UI (confirmed in production): light only, FastAPI's favicon, third-party CDN assets (the dashboard otherwise makes no third-party requests and has a strict CSP, which `/docs` is exempted from). Nothing ties it to the dashboard's design system. |

### Redesign plan

Keep the generated OpenAPI as the source of truth, and put a real front door in front of it.

1. **A developer landing at `/docs`**, served by the app, using the dashboard's tokens from
   `src/mendari/dashboard/style.css` (section 1) and its theme script, so light and dark both
   work and `tests/test_dashboard_contrast.py` covers it. No build step, an ES module like the
   dashboard's views. Sections:
   - **What Mendari is:** two sentences, the four outcomes as the dashboard's outcome chips, and
     the README's architecture diagram as inline SVG.
   - **Quickstart in 3 calls**, copy buttons, real synthetic bodies taken from each pack's
     `examples` endpoint: `GET /api/packs` → `POST …/decide` → `GET /api/audit/{id}`.
   - **FHIR in, FHIR out:** a Bundle to `…/fhir/decide` and the Task that comes back; links to
     `/metadata`, `/cds-services` and the SMART launch.
   - **Authentication:** open vs signed-in mode, API keys (`Authorization: Bearer ohk_…`), roles.
   - **Reference:** a link to the full API reference, below.
   - **For clinicians:** a link to the dashboard's Try it and to each pack's Decision Card.
2. **API reference at `/docs/reference`**: a self-hosted reference renderer (Scalar or Redoc's
   standalone bundle, vendored under `/static`, so no CDN and no CSP exemption), themed with the
   same tokens. Swagger UI stays at `/docs/swagger` for "Try it out".
3. **Better OpenAPI** (makes every renderer better): tags (Decisions, FHIR & CDS Hooks, Review,
   Audit, Evaluation, Governance, Sign-in, SMART); a summary and description on every operation;
   request/response models with `examples` drawn from pack examples; `securitySchemes` (bearer
   API key, OIDC) applied per route from `auth._RULES`; documented error responses.

## Roadmap to a credible FHIR API and docs

**A decision first.** "Production-grade FHIR API" can mean two things, and they cost very
different amounts:

- **(A) Make Mendari's FHIR-facing surface conformant**: CapabilityStatement, OperationOutcome,
  strict parsing, a `$decide` FHIR operation, versioned outputs. This fits what Mendari is.
  **Recommended.**
- **(B) Make Mendari a FHIR data server** (CRUD, search, history, transactions). I recommend
  against building this. HAPI FHIR already does it, and it's already in `docker-compose.yml`.
  If a deployment needs a FHIR data API next to Mendari, run HAPI (or a managed FHIR service) and
  have Mendari read from it, as the sandbox already does. A hand-rolled FHIR server in FastAPI
  would take months to approach HAPI's conformance, and it would make Mendari a system of record
  for clinical data, when today it keeps only decision records (inputs hashed or encrypted
  under PHI mode).

The phases below assume (A).

**Library choice.** Add **`fhir.resources`** (pydantic v2, like the rest of the codebase) as a
structural parse gate on FHIR input: wrong JSON types, unknown resource types and bad required
codes become a `400 OperationOutcome` instead of a crash or a silent pass. Keep the generated US
Core subset for profile rules, and keep the HL7 validator in CI as the source of truth.
**Caveat to check first:** current `fhir.resources` releases default to R5 and ship R4B as a
subpackage. R4B is close to R4 for the resources Mendari reads, but not identical, so pin the
version and run the existing fixtures and the HL7-validated samples through it before relying on
it. If R4B differs where it matters, `fhir-core`-based R4 models or a JSON Schema check
against the official `fhir.schema.json` for R4 are the fallbacks. Hand-rolled parsing is what
produced the two `500`s.

### Phase 1: quick wins (≈ 2–3 days)

| Change | Files | Effort |
|---|---|---|
| Fix the two `500`s: reject non-object `meta` / `subject` / references with a `400` | `standards/us_core.py`, `standards/fhir.py`, `tests/test_fhir.py`, `tests/test_us_core.py` | 0.5 d |
| `OperationOutcome` for every error on FHIR routes (`/fhir/…`, `…/fhir/decide`, `/cds-services` stays CDS-shaped), `application/fhir+json` | `server/app.py` (exception handler by path), `standards/us_core.py` (`operation_outcome` reuse), `tests/test_server.py` | 0.5 d |
| Stop echoing request bodies in `422` validation errors (custom `RequestValidationError` handler) | `server/app.py`, `tests/test_server.py` | 0.25 d |
| Unconfigured provider → `503`; document `200 BLOCK` for schema-invalid input | `server/app.py`, `errors.py` | 0.25 d |
| OpenAPI tags, summaries and descriptions on every route; `examples` from pack examples; `securitySchemes` | `server/app.py`, `server/auth.py`, `server/smart_routes.py` | 1 d |

**Status (2026-10-01): built.** Changes from the plan above:
- A test that mistypes every element of the four US Core fixtures (`tests/test_fhir_errors.py`)
  found **8** crash sites, not 2. All are fixed, and the test fails on the old code.
- Structure checks (`us_core.structure`) cover the elements whose JSON type every R4 resource
  shares (`resourceType`, `id`, `meta`, `subject`, every `coding`, Bundle entries). They run in
  every validation mode, `off` included, and a failure is a `400` `OperationOutcome` with
  `code: structure`. Elements specific to a profile (a `Patient.name` that is a string, say)
  are reported as that profile's issues, under `warn` / `enforce`. Full structural validation
  stays in Phase 2.
- Provider errors map by their stable code: not configured or no credentials `503`, upstream
  failure `502`, daily budget used up `429`.
- Any unexpected error is a JSON `500` (an `OperationOutcome` on FHIR routes), never plain text.
- The API docs code is in `server/api_docs.py`. Examples come from the packs this deployment
  serves, so "Try it out" starts with a request that decides. The introduction says what Mendari
  is, that it is not a FHIR server, and whether sign-in is on here.
- Sign-in in production (Google, see `docs/deploying-with-phi.md`) needed two code changes:
  `MENDARI_OIDC_ENDPOINT_ORIGINS` for a provider's endpoints on other hosts, and a role from
  the `email` claim only when `email_verified` is `true`. `/docs` sends a signed-out browser to
  sign in.
- Reviewed before shipping by five independent reviewers, each finding checked by a skeptic.
  The structure rules hold for all 658 R4 StructureDefinitions, and sign-in had no findings.
  Six low-severity problems were confirmed and fixed: a null-padded `meta.profile` (valid FHIR
  JSON) was rejected; nesting deeper than about 500 levels was a `500` (the walk is now
  iterative); and four places where the API docs said more than the code does.

### Phase 2: core conformance (≈ 4–6 days)

| Change | Files | Effort |
|---|---|---|
| `GET /fhir/metadata`: a CapabilityStatement generated from what is mounted (no resource interactions; `$questionnaire-package`; a `$decide` operation; SMART app, CDS Hooks link), and a test that every advertised operation exists | new `standards/capability.py`, `server/app.py`, `tests/test_capability.py` | 1.5 d |
| A FHIR-native decision endpoint: `POST /fhir/PlanDefinition/{pack}/$decide` (or `$apply`), `Parameters` in, `Bundle` (Task + Provenance) out, with an `OperationDefinition` | `server/app.py`, `standards/fhir.py`, new `OperationDefinition` JSON | 1.5 d |
| `fhir.resources` parse gate on all FHIR input (after the R4/R4B check above) | `pyproject.toml`, `uv.lock`, `standards/fhir.py`, `core/decision.py` | 1 d |
| `meta.versionId`, `meta.lastUpdated` and `meta.profile` on produced Task / AuditEvent / Provenance | `standards/fhir.py`, `standards/audit_fhir.py` | 0.5 d |
| Publish Mendari's CodeSystems (outcome, destinations, audit subtypes) as resources at their URLs; bind them in the CapabilityStatement | new `standards/codesystems/`, `server/app.py`, `web/` | 1 d |
| Add the produced Task and the record Bundle to CI's validator run | `.github/workflows/ci.yml`, `scripts/` | 0.25 d |

**Status (2026-10-02): built**, after research by three researchers and a critic (evidence from
the HL7 validator and the official R4 definitions). Changes from the plan above:
- `fhir.resources` was not used: its last R4 release needs pydantic v1, and its R4B models
  rejected 62 of HL7's 5,309 official R4 examples. The gate is HL7's R4 JSON Schema instead,
  patched (`scripts/fhir_r4_schema.py`) and checked by an iterative walker, with `jsonschema` as
  the test oracle. It replaces Phase 1's hand-written structure checks.
- The decision operation is `POST /fhir/$decide` (system level), not `PlanDefinition/$apply`:
  R4's `$apply` reads the subject from the server's own data, and a pack's logic is not a
  PlanDefinition. It returns a GuidanceResponse, the R4 resource for a decision-support result.
- `meta.profile` is not set (no profiles exist), and the Task gets no `versionId` (the server that
  stores it assigns one).
- CodeSystems are served by each deployment, not published on mendari.co (a launch decision).
- Added: FHIR media types (`406`/`415`), and the privacy fix for rejection reasons.
- Reviewed before shipping by five independent reviewers, each finding checked by a skeptic.
  Ten problems were confirmed, none high: text with a vertical tab or form feed was a `400`
  (the HL7 validator only warns); `Expression.language` was treated as a required binding
  (it is the only one of the schema's 246 enumerations R4 binds as extensible); a required
  canonical carried by its extension alone was flagged; `$decide` claimed the input is never
  echoed (answer evidence can name what matched) and promised a `503` it never returns; provider
  failures under combined providers lost their issue type; `$decide` was advertised for packs
  without FHIR input; one pack's broken FHIR mapping stopped the server; with sign-in on, the
  CodeSystems exposed pack criteria publicly (they now need `read`); and the
  CapabilityStatement's `date` could stay or go back when its content changed (its `version` is
  now a digest of the whole statement, and `date` when that version went live here). All fixed,
  each with a test.

### Phase 3: search and Bundles (≈ 3–5 days, only what fits Mendari)

| Change | Files | Effort |
|---|---|---|
| Expose decisions as FHIR `Task`s: `GET /fhir/Task?identifier=…&status=…&_lastUpdated=…&_count=…`, with `searchset` Bundles, `self`/`next` links and `_total` | `server/app.py`, `core/audit.py` (query by fields it already stores), `standards/fhir.py` | 2–3 d |
| `GET /fhir/AuditEvent` and `/fhir/Provenance` search over the audit chain (`entity`, `date`, `_count`), the FHIR face of `/api/audit/export/fhir` | `server/app.py`, `standards/audit_fhir.py` | 1–2 d |
| `transaction` / `batch`: **not recommended** (no stored resources to change). Accept a `batch` of `$decide` calls only if a customer needs it | — | — |

**Status (2026-10-02): built** as planned after research (three researchers and a critic), with
the owner's choice of a Task per decision. The `$decide` work-item Task's relative references
(a Phase 2 defect the research found) were fixed separately, in #108.
- Reviewed before shipping by five independent reviewers, each finding checked by a skeptic.
  No leak across organizations was found: reads, searches, counts and forged or replayed
  cursors stayed within the caller's organization on SQLite and Postgres, and no case text
  reached any resource under the three PHI modes. Five problems were confirmed, all low, and
  fixed with tests: very long value lists or many repeats reached the database as a `500` (now
  a `400` too-costly: at most 100 values and 20 repeats per parameter); `Prefer:
  handling=strict` in a second Prefer field was ignored; AuditEvent and Provenance ids with a
  leading zero read another id; and a refused input was inferred (a BLOCK without
  confidence), which a pack with a BLOCK rule on another question would have broken: it is
  now recorded with the decision (`decisions.refused`, an additive column; older rows keep
  the inference). Reviewing an ABSTAIN now renders the reviewer as Provenance `author`, in the
  `/api/audit` export too, without a new `versionId`: a rendering change, nothing stored.

### Phase 4: security (≈ 4–6 days)

| Change | Files | Effort |
|---|---|---|
| Turn OIDC sign-in on in production so the API can be opened to developers without Vercel Authentication; scoped API keys | Vercel env, `server/auth.py`, `docs/deploying-with-phi.md` | 1 d |
| SMART Backend Services / `system/*.rs` scopes accepted as bearer tokens for the FHIR routes (Phase 2–3), mapped onto Mendari roles | `server/auth.py`, `standards/oidc.py`, `standards/smart.py` | 2–3 d |
| `.well-known/smart-configuration` only if Mendari becomes a SMART resource server; otherwise state in the CapabilityStatement that it is a SMART app | `server/smart_routes.py` | 0.5 d |
| The planned security review before any PHI (README status note) | — | external |

**Status (2026-10-02): built** after research (three researchers and a security-minded critic),
with the owner's choices: the token endpoint plus scoped keys, older keys expiring 30 days after
deploy, and URI scopes. Signed JWT access tokens, JWKS URLs and external authorization servers
are deferred (ADR-0004); hardening before PHI is Phase 4b.

**Status (2026-10-02): Phase 4b built** ([ADR-0005](decisions/ADR-0005-hardening-before-phi.md);
"Status after Phase 4b" above): notes out of the chain, a keyed input hash, rate limits and
quotas, a security log, idle sessions and `__Host-` cookies, Origin checks, a vendored API
reference and a pinned supply chain. The external security review comes next.
- Reviewed before shipping by five independent reviewers, each finding checked by a skeptic.
  No bypass was found: the token endpoint held against forged algorithms, keys, headers,
  claims and concurrent replays on Postgres, and no key, token or scope reached a route,
  organization or permission it should not. Two problems were confirmed and fixed with tests.
  The check for a credential in a form read every form body before sign-in was checked, so a
  caller without a credential could make the server read and parse a large body (on a
  self-hosted install, enough to exhaust its memory). Now no body is read before the sign-in
  check: the routes that read a form refuse a credential in it, and read at most 16 KiB (the
  token form) or 256 KiB (a search form), a chunked body only that far. And a decision with
  no answer under a pack id from before the rename, or of a pack another instance serves,
  could no longer be reviewed: its labels now come from every installed pack, by its current
  id.

### Phase 5: docs experience (≈ 4–5 days)

| Change | Files | Effort |
|---|---|---|
| Developer landing at `/docs` (plan above), light and dark, dashboard tokens, contrast-tested | new `src/mendari/dashboard/docs.html` + `docs.js`, `style.css`, `server/app.py`, `tests/test_dashboard_contrast.py`, `tests/browser/` | 2 d |
| Self-hosted reference at `/docs/reference`, Swagger at `/docs/swagger`, no CDN, CSP exemption removed | `server/app.py` (`docs_url=None`, own routes), vendored assets under `dashboard/vendor/`, `tests/test_security_headers.py` | 1 d |
| Quickstart, auth and FHIR pages linked between the landing, the mkdocs site and the README; curl examples checked in CI | `docs/`, `mkdocs.yml`, `README.md`, a CI step that runs the curl snippets against a local server | 1–1.5 d |
| A public, unauthenticated read-only demo deployment with synthetic data, so "Try it out" works for visitors (a launch decision; see `tasks/todo.md`) | Vercel project settings | decision |

**Total for (A): about 3–4 weeks of focused work.** After Phases 1–2, the honest one-line
description would be: "a FHIR R4 decision service with a CapabilityStatement, `$decide`, US
Core-checked input, OperationOutcome errors, CDS Hooks and SMART launch". That is credible and
true.
