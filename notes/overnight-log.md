# Overnight log: the roadmap to completion

> Written before the project was renamed from OpenHealth Hub to **Mendari** (2026-09-30).
> Names, commands and paths below are as they were: `openhealth` is now `mendari`,
> `OPENHEALTH_*` settings are `MENDARI_*` (the old names still work), and pack ids start with
> `mendari/`.

_Started 2026-09-29, late evening ET. Scope: every item in `notes/design-grade.md`, then (at your
request during the night) the whole project roadmap, v0.4 to v1.0, all approved in advance.
Rules: one focused PR per item, merged only on green CI after a self-review, squash merge. The
final state and self-grade are at the end._

## Starting point

Phases A, B (B1–B3) and C and the re-grade were already merged by an earlier session
(#57–#61, CI green on `main`, re-graded C+ → B+). What remained in `notes/design-grade.md`:
the production-deploy check and the four "small remaining gaps" under "What an A still needs".

## Pull requests

| # | Title | Closes | CI |
|---|---|---|---|
| — | Production deploy check (no PR) | todo: "Check the production deploy" | — |
| [#62](https://github.com/Junebugg1214/mendari/pull/62) | Plain words in criteria tables and Try-it answer headings | Small gap: "plain words in the Try-it criteria table"; category 4 | Red once (a test line over 100 columns, edited after lint ran), then green; preview checked; merged |
| [#63](https://github.com/Junebugg1214/mendari/pull/63) | Links carry the pack (?pack= in the address) | Small gap: "links that carry the pack"; category 5 | Green first time; preview checked (including `/?pack=`); merged |
| [#64](https://github.com/Junebugg1214/mendari/pull/64) | A view fades in where its skeleton was | Small gap: "a view transition from skeleton to content"; category 9 | Green first time; preview checked; merged |
| [#65](https://github.com/Junebugg1214/mendari/pull/65) | Sorting moves the rows instead of redrawing the table | Small gap: "fewer whole-subtree re-renders"; category 10 | Green first time; preview checked; merged |
| [#66](https://github.com/Junebugg1214/mendari/pull/66) | v0.4 plan | v0.4 planning step | Green; merged (the API answered 502 on merge, the merge had gone through; the branch was deleted by hand) |
| [#67](https://github.com/Junebugg1214/mendari/pull/67) | US Core 6.1.0 validation of FHIR inputs (warn by default, enforce on request) | v0.4 item 1 | Green first time, including the new `fhir-conformance` job; preview checked with a real FHIR decision on Vercel (profile data loads, issues returned); merged |
| [#68](https://github.com/Junebugg1214/mendari/pull/68) | Audit export as FHIR AuditEvent and Provenance | v0.4 item 2 | Green first time; preview streamed NDJSON from Postgres; merged |
| [#69](https://github.com/Junebugg1214/mendari/pull/69) | Sandbox: referral cases in FHIR as US Core resources (and a seeding fix) | v0.4 item 3 | Green first time; preview checked; merged |
| [#70](https://github.com/Junebugg1214/mendari/pull/70) | CDS Hooks fetch missing prefetch from allow-listed FHIR servers | v0.4 item 4 | Green first time; preview checked; merged |
| [#71](https://github.com/Junebugg1214/mendari/pull/71) | SMART on FHIR EHR launch: backend (PKCE, OIDC, server-side sessions, patient context) | v0.4 item 5, part 1 of 3 | Green; one follow-up commit before merge (`/api/smart/session` answers `{"active": false}` instead of 401, found while building the view); preview checked (SMART off: inactive, launch 404); merged |
| [#72](https://github.com/Junebugg1214/mendari/pull/72) | Dashboard: This patient, after a SMART launch from the EHR | v0.4 item 5, part 2 of 3 | Red once (a real dashboard race exposed by the new boot request, plus a racy skeleton test); fixed at the root with a regression test, then green; preview checked; merged |
| [#73](https://github.com/Junebugg1214/mendari/pull/73) | Sandbox: SMART launcher and a scripted EHR launch (CI job sandbox-e2e) | v0.4 item 5, part 3 of 3 (and the SMART half of item 7) | Green, including the new `sandbox-e2e` job (the SMART exit, proven in CI); rebased after #72 (log and todo only), green again; merged |
| [#74](https://github.com/Junebugg1214/mendari/pull/74) | Bulk FHIR $export into local-validation datasets | v0.4 item 6 (and the Bulk half of item 7) | Green first time, including `sandbox-e2e` with `bulk-check` (the Bulk exit, proven in CI); merged. **v0.4 done.** |
| [#75](https://github.com/Junebugg1214/mendari/pull/75) | v0.4 done; v0.5 plan (the Hub) | v0.4 closeout, v0.5 planning step | Green; merged |
| [#76](https://github.com/Junebugg1214/mendari/pull/76) | Share the database layer: core/db.py | v0.5 groundwork (the registry reuses it) | Green first time; merged |
| [#77](https://github.com/Junebugg1214/mendari/pull/77) | Signed pack archives (.ohpack) and publisher keys | v0.5 item 1 | Opened with a stale diff (my mistake, see decisions), fixed by force-pushing the feature branch; green; merged |
| [#78](https://github.com/Junebugg1214/mendari/pull/78) | Registry service: namespaces, publisher keys, immutable signed versions, search | v0.5 item 2 | Green first time; merged |
| [#79](https://github.com/Junebugg1214/mendari/pull/79) | Hub client: login, push, pull (verified, pinned keys, cached), search; Decision.from_hub from the registry | v0.5 item 3 | Red once on Python 3.11: a flaky teardown in a test merged with #78, whose unclosed Postgres pool was collected on another thread. Fixed at the root (registry stores close; the app closes its store at shutdown), then green; merged |
| [#80](https://github.com/Junebugg1214/mendari/pull/80) | Shareable local-validation reports (metrics only) and site comparison | v0.5 item 4 | Green first time; merged |
| [#81](https://github.com/Junebugg1214/mendari/pull/81) | Hub website (Next.js): search, pack pages, publishers; CI job hub-site | v0.5 item 5 | Red once (`npm ci` on Node 22 / npm 10 found optional Linux packages missing from the lockfile), fixed by regenerating it and running CI on Node 24; green; merged |
| [#82](https://github.com/Junebugg1214/mendari/pull/82) | v0.5 end to end: a clean machine installs, pulls and tests; registry in the sandbox; docs | v0.5 item 6 (the exit) | Green first time (the new `hub-e2e` job included); preview checked; merged. v0.5 done |
| [#83](https://github.com/Junebugg1214/mendari/pull/83) | v0.6 plan: readiness for health organizations | v0.6 plan | Green first time; preview checked; merged |
| [#84](https://github.com/Junebugg1214/mendari/pull/84) | Sign-in for organizations: OIDC, roles, API keys, tenancy | v0.6 item 1 | Green first time; preview checked (open mode, `/api/auth/me` answers `open`); merged |
| [#85](https://github.com/Junebugg1214/mendari/pull/85) | Governance approvals in the Decision Card, and the deployment registry | v0.6 item 2 | Red once: the browser job's 24 px check failed on an inline link in the card's facts (on Linux the sentence didn't wrap, so the link was 20 px tall). Replaced the link with words; lesson added; then green |
| [#86](https://github.com/Junebugg1214/mendari/pull/86) | Monitoring: observed accuracy, overrides, drift and alerts over time | v0.6 item 3 | Green first time; preview checked (`/api/monitoring` answers with real preview traffic); merged |
| [#87](https://github.com/Junebugg1214/mendari/pull/87) | PHI mode: hash-only or encrypted inputs, reads logged, retention; deployment guide | v0.6 item 4 | Green first time; preview checked (`phi_mode: off`, so production is unchanged); merged. Opened once ahead of a failed local gate (see lessons); fixed before CI finished |
| [#88](https://github.com/Junebugg1214/mendari/pull/88) | Per-pack container images (GHCR on release tags) and on-prem templates | v0.6 item 5 | Green first time (the `docker` job built the per-pack image and saw it serve only its pack); preview checked; merged |
| [#89](https://github.com/Junebugg1214/mendari/pull/89) | v0.6 pilot check in CI: a real OIDC provider, keys, PHI, review, governance, monitoring | v0.6 item 6 (the exit) | Green first time (the new `pilot-check` job passed against mock-oauth2-server); preview checked; merged. v0.6 done |
| [#90](https://github.com/Junebugg1214/mendari/pull/90) | v0.7 plan: CQL, OMOP, Da Vinci CRD/DTR/PAS | v0.7 plan | Green first time; preview checked; merged |
| [#91](https://github.com/Junebugg1214/mendari/pull/91) | CQL provider; the trial pack's criteria in CQL, agreeing with `criteria` in CI | v0.7 item 1 | Green first time (the new `cql` job: 578 + 172 cases agree, 22/22 pack tests with CQL); preview checked; merged |
| [#92](https://github.com/Junebugg1214/mendari/pull/92) | OMOP CDM as pack inputs, population evaluation, round trip in CI | v0.7 item 2 | Green first time (`omop-check` identical in the reference-pack job); preview checked; merged |
| [#93](https://github.com/Junebugg1214/mendari/pull/93) | Da Vinci CRD: coverage information on the PA pack's order-sign response | v0.7 item 3 | Green first time (the validator passed the three CRD orders against CRD 2.2.1 in CI); preview checked; merged |
| [#94](https://github.com/Junebugg1214/mendari/pull/94) | Da Vinci DTR: questionnaire package for the PA pack, pre-answered from the decision | v0.7 item 4 | Green first time (all five CRD/DTR samples valid in CI); preview checked; merged |
| [#95](https://github.com/Junebugg1214/mendari/pull/95) | Da Vinci PAS: request Bundle, sandbox payer, submission chained on the decision | v0.7 item 5 (closes v0.7) | Green first time (PAS request and response valid in CI with CRD and DTR); preview checked; merged. v0.7 done |
| [#96](https://github.com/Junebugg1214/mendari/pull/96) | v1.0 plan: public open-source launch | v1.0 plan | Green first time; preview checked; merged |
| [#97](https://github.com/Junebugg1214/mendari/pull/97) | Security review: threat model, headers, dependency audit and ZAP scan in CI | v1.0 item 1 | Green first time (the new `security` job: pip-audit and npm audit clean, ZAP 114 passes, no medium or high); preview checked (headers live, every module 200); merged |
| [#98](https://github.com/Junebugg1214/mendari/pull/98) | `openhealth new` and a clinician's pack-authoring guide | v1.0 item 2 | Green first time (`hub-e2e` ran `openhealth new` and its tests from the wheel); preview checked; merged |
| [#99](https://github.com/Junebugg1214/mendari/pull/99) | The documentation site (MkDocs Material, strict build in CI) | v1.0 item 3 | Red once for a reason outside the PR (the uv cache-lock timeout, fixed in #100); after the rebase green, the new `docs` job included; preview checked; merged |
| [#100](https://github.com/Junebugg1214/mendari/pull/100) | ci: background servers run from the venv, not `uv run` (uv 0.12 holds the cache lock) | CI root cause found on #99 | Green first time (no cache-lock timeouts in hub-e2e, hub-site, security, pilot-check); preview checked; merged |
| [#101](https://github.com/Junebugg1214/mendari/pull/101) | The contribution flow: pack-PR workflow with the eval gate, community files | v1.0 item 4 | Green first time; preview checked; merged |
| [#102](https://github.com/Junebugg1214/mendari/pull/102) | Release workflow (PyPI trusted publishing on tags) and the README quickstart timed in CI | v1.0 item 5 | Green first time (the new `quickstart` job ran the README's quickstart cold in 5 s); preview checked; merged. v1.0 built |
| [#103](https://github.com/Junebugg1214/mendari/pull/103) | Overnight log: final state and self-grade | the log's final state | pending |

## Decisions made on your behalf

- **Your later messages widened the run** (2026-09-29, before you went to bed): the whole project
  roadmap, v0.4 to v1.0, is approved, and so is new infrastructure where I recommend it. Every
  milestone still starts with its plan committed to `notes/plans/`, as the roadmap requires.

- **Production check without a PR.** `vercel curl` (authenticated CLI) returned 200 for `/`,
  `/api/health` and `/static/main.js` on the production alias; health reports Postgres, 5 packs,
  `America/New_York`. Unauthenticated requests still get 302 (lesson 2026-09-28). The rendered
  dashboard in your Chrome showed the Phase C UI.
- **Plain words come from each question's `instructions`**, first sentence: the pack already writes
  them for people, so no new pack field or digest change is needed. The code stays as secondary
  monospace text and the full instructions stay in the tooltip.
- **Screenshots are described in the PRs, not attached.** `gh` cannot upload images to a PR, and
  committing screenshots would add unrelated binaries to the repo. Every change was checked in
  the running app at 1440 and 390 px, light and dark.
- **The pack goes in the query, not the hash** (`?pack=openhealth/ai-guard#review`). Every
  in-app link is a bare `#view` link and keeps the query, so no link had to change. A pack change
  replaces the history entry rather than adding one, as before. Back across an earlier pack
  change shows that pack. An unknown pack falls back to the last one chosen, and the address is
  corrected.
- **Skeleton to content is a 200 ms fade-in of the view, not a crossfade of two layers.** It
  matches the Evaluations tab crossfade from Phase C, needs no second copy of the view in the
  page, and uses opacity only: a transform on `main` would move sticky and fixed children while
  it runs.
- **"Fewer whole-subtree re-renders" is scoped to sorting.** Sorting was the one frequent
  interaction that redrew content without new data: every table now moves its rows. The other
  `innerHTML` writes either draw a view the first time or swap in different data (another
  period, dataset or report), where a redraw is correct; the form's add and remove already
  restore focus. Going further needs a diffing layer, which means a framework and a build step,
  against the project's no-build rule.
- **v0.4 order:** validation and audit export first (no new outbound calls), then the referral
  sandbox that SMART and Bulk need, then the features that call out (CDS fetch, SMART, Bulk)
  behind one allow-list client, then the end-to-end CI job. Details in
  `notes/plans/v0.4-healthcare-standards.md`.
- **US Core checks are generated, not hand-written.** `scripts/us_core_subset.py` reads the
  official `hl7.fhir.us.core#6.1.0` and `hl7.fhir.r4.core#4.0.1` packages (pinned by sha256)
  and writes the required elements, enumerable required bindings, required category slices and
  `us-core-*` invariants. Cardinalities and codes therefore can't be misremembered.
- **The HL7 FHIR validator 6.10.4 (pinned jar, sha256 checked) runs in a new CI job**, without a
  terminology server (`-tx n/a`), so the result doesn't depend on tx.fhir.org. Conforming
  fixtures must have zero errors. Every resource the in-process checks flag must be an error for
  the validator too, so `enforce` can never block what US Core accepts.
- **`warn` is the default.** It changes no outcome and only adds `input_issues` (paths, no
  values) to the response and the audit record. `enforce` is opt-in.
- **Provenance activities use R4's own value set** (v3-DataOperation `CREATE` / `UPDATE`) instead
  of the iso-21089 lifecycle codes named in the plan, so they bind cleanly in R4. Confirming a
  review makes the reviewer a `verifier`; overriding makes them an `author`.
- **The audit export leaves free text out.** Review and feedback notes stay in the store, and the
  input is referred to by its hash. Each AuditEvent carries `seq`, `prev_hash` and `hash`, so the
  chain's linkage survives export and each hash can be checked against the store. The chain can't
  be recomputed from the export alone, since that needs the payloads, which may hold notes.
- **Sandbox referral cases are moved to today.** The FHIR mappings have no as-of date and decide
  as of today, so every date shifts by the same gap and birth dates keep each age. A test checks
  that FHIR → mapping gives back each of the 300 dataset inputs exactly, and that 60 of them
  decide the same from FHIR as from JSON.
- **Sandbox resources claim the US Core profiles**, and CI validates them with the HL7 validator.
  Patients are named "Synthetic, Case <id>". Lab panels without results carry
  `dataAbsentReason = unknown`, since the synthetic records have no values. Document attachments
  say "Synthetic … (no clinical content)".
- **Found and fixed:** `openhealth sandbox seed` checked "already seeded?" through HAPI's search
  cache, so a second seed within about a minute loaded everything again (seen against the local
  HAPI). The check now asks for fresh results.
- **One outbound FHIR client for everything that calls a server someone else named**
  (`standards/fhir_client.py`): CDS fetch now, SMART and Bulk next. Its rules:
  - the allow-list matches exact origins (so `127.0.0.1` isn't `localhost`);
  - plain http only for loopback and single-label compose hosts;
  - paths confined to the base URL, `next` links included;
  - no redirects, a 10 s timeout, a 5 MB JSON cap;
  - errors carry no server body and no token.
- **CDS prefetch templates accept FHIR ids only** in `{{context.x}}` (`[A-Za-z0-9-.]{1,64}`),
  so a hostile context can't add search parameters. Templates with other tokens
  (`{{userPractitionerId}}`) are refused rather than guessed.
- **The docker sandbox allow-lists its own HAPI** (`http://fhir:8080`) by default. Production
  keeps an empty list.
- **SMART ships as three PRs:** the backend (launch, session, patient-context API), the dashboard's
  "This patient" view, then the sandbox launcher with an end-to-end check.
- **SMART choices:**
  - public client with PKCE S256 by default, and a confidential client when a secret is set;
  - scopes `launch openid fhirUser patient/*.rs`, overridable;
  - `id_token` verified with the server's JWKS, RS256 or ES384 only (so `none` and HS256 are
    refused), plus `iss`, `aud`, `exp` and `nonce`, with 60 s leeway;
  - `openid` requested means an `id_token` is required;
  - a launch without a patient is refused;
  - no refresh tokens, and sessions capped at 12 h;
  - state and session ids stored as SHA-256, and a launch's state works once;
  - launch errors are an HTML page, since they land in the EHR's browser, with OAuth error codes
    shown only when they look like codes.
- **Found and fixed: a race in the dashboard.** A pack whose details arrived after the user had
  already switched again overwrote the newer pack, and overlapping renders finished in any order.
  CI caught it on #72: the boot's extra request moved the timing. A pack's late answer is now
  dropped, and renders run one after another. A deterministic browser test holds AI Guard's
  answer until after going back, and it fails without the fix. The same PR fixed a race in a
  skeleton test, which released held requests before the view had made them.
- **"This patient" appears only after a launch.** Its nav link is hidden otherwise, so nothing
  changes for anyone not launched from an EHR. Work items are decided on request (a button),
  not on page load, so viewing a patient never writes decisions to the audit trail.
- **The sandbox's SMART server is smart-launcher-v2, pinned by digest.** Its launch value can skip
  login and consent, so `openhealth sandbox smart-launch` plays the EHR and the browser over
  plain HTTP inside the compose network. That's how CI proves the roadmap's exit ("the inbox and
  referral packs run from a SMART launch against the HAPI sandbox") on every change, with
  `--strict`, which fails on any US Core issue in a decided input. A human with a browser would
  need the compose hostnames (`engine`, `launcher`) to resolve on their machine, so the README
  shows the scripted launch.
- **Bulk labels come from the site, not the EHR.** `openhealth bulk dataset` joins a site's
  labels file (any JSONL of `{"id", "labels"}`; an evaluation dataset works as it is) by an
  identifier system, on the focus resource or its patient. Unlabelled cases are left out and
  counted. The export supplies inputs, and chart review supplies the answers.
- **The Bulk exit check is strict.** `openhealth sandbox bulk-check` exports the sandbox, builds
  the dataset, and requires the evaluation to be identical to the same cases read from the
  dataset file: decisions, per-question metrics and misclassified cases. Locally all 40 referral
  cases matched exactly.
- **Access tokens are stored in the database unencrypted** for their lifetime. There is no key to
  encrypt them with until v0.6's PHI mode; this is noted in the architecture's trust boundaries.
- **A new dependency, `pyjwt[crypto]` (>=2.10.1,<3; 2.15.1 locked)**, verifies SMART `id_token`s.
  The roadmap's OAuth 2 / OIDC calls for it, and it will sign the Bulk backend-services assertion.
- **v0.5 is built and verified locally and in CI. It is not made public.** A public registry,
  the public Hub site and a PyPI release need production signing keys and credentials, and they
  make the project public. That can't be undone, and the launch belongs with the v1.0 security
  review. It's logged under "Skipped or blocked" with what's needed.
- **The registry reuses the audit store's database layer**, rather than keeping a second copy.
  `core/db.py` is a pure move with the schema, migration and lock key passed in. It was checked
  line for line (lesson 2026-09-29), and the full suite passes on SQLite and Postgres.
- **A mistake of mine, caught before it reached `main`:** a failed gate was masked by a pipe
  during a conflicted rebase. PR #77 briefly showed a stale diff, and it was fixed by redoing the
  rebase and force-pushing the feature branch. Lesson added to `tasks/lessons.md`.
- **Pack archives are deterministic gzip tars signed with Ed25519** over a manifest of file
  hashes. Ed25519 comes from `cryptography`, already a dependency. Signatures are deterministic,
  so the same pack and key give the same archive bytes. Verification happens in memory before
  anything touches disk.
- **Registry choices:**
  - archives live in the database (a pack is a few MB), with Vercel Blob left for later if sizes
    call for it;
  - the publisher type comes from registry admins, never from the pack's own claim;
  - search runs over the latest versions in memory, since there are few packs;
  - an admin token comes from the environment, and without it the admin routes are closed.
- **`pull` pins namespace keys on first use, like SSH host keys.** A pack signed by an unpinned
  key is refused unless the user passes `--accept-new-key`. Pulled packs are cached on the pack
  search path, so every command and the server find them like local packs. Tests give each test
  its own config and cache directories, so a developer's real cache never leaks in.
- **Shared reports keep aggregates only.** The misclassified list (case ids), the dataset's name
  and digest, and the report's own id are dropped. So are subgroup cells and calibration bins
  with fewer than 10 cases, since a tiny cell can reveal one case's outcome. The registry applies
  the same filter to whatever it receives.
- **Hub website stack:**
  - Next.js 16 with React 19, TypeScript 5.9 (not 7, whose Next support is unproven), and
    `react-markdown` for Decision Cards, rendered safely with the card's headings demoted;
  - ESLint 9: ESLint 10 breaks `eslint-plugin-react`, which `eslint-config-next` bundles.
  - All versions are pinned, with a lockfile.
  - Server-rendered from the registry API, no client state: search is a GET form.
- **Every new v0.4 capability is off until configured**, and no production environment variable
  is changed. Production has no login of its own until v0.6, so outbound FHIR calls and SMART
  stay disabled there.
- **The engine's reason lines stay as they are** ("not_eligible: hba1c_in_range is no"). They
  come from the runtime, and changing them is business logic, out of scope.

- **v0.6 shape** (details in `notes/plans/v0.6-org-readiness.md`):
  - every control is off until configured, so production is unchanged;
  - a `service` role for API keys (decide and record outcomes, never resolve a review);
  - tenancy in a context variable that the store applies to every read and write;
  - governance approvals bound to the pack's digest, so a new version needs a new approval;
  - deployments recorded at startup only when something changed (serverless starts often);
  - PHI encryption is AES-256-GCM through `cryptography` (already a dependency), bound to the
    decision id;
  - encrypted inputs open only for a signed-in reader, since without sign-in there is no one to
    log the read against. So encrypt mode requires sign-in to be useful, and the guide says so;
  - container images are published to GHCR only when a release is tagged, never from pull
    requests or `main`. No tag has been pushed, so nothing is published. The templates ask for
    an image rather than pointing at a release that doesn't exist;
  - the pilot check uses `ghcr.io/navikt/mock-oauth2-server:6.0.4` (MIT, pinned) as the
    identity provider. It's a real third-party OpenID Connect implementation, so the check tests
    interoperability, not only our own fake. Its scriptable sign-in form lets one script sign
    in as each role.

- **v0.7 shape** (details in `notes/plans/v0.7-later-standards.md`):
  - CQL runs on HAPI FHIR's Clinical Reasoning module (`Library/$evaluate`), the maintained
    successor of cqf-ruler, rather than a new CQL engine in Python. I checked it by hand
    against HAPI 8.12 before the plan;
  - the trial pack gets a `cql` variant that must agree with `criteria` in CI. `criteria` stays
    the default;
  - OMOP is read from a CDM 5.4 database with a minimal concept table, and checked by a round
    trip;
  - Da Vinci CRD, DTR and PAS wrap the PA pack, each validated by the HL7 validator against its
    IG;
  - the trial pack goes to 0.1.2 for its new `cql` variant (a content change, so a new version
    for the registry's immutable versions). Only its artifacts were regenerated: the other
    packs' benchmarks differed by run id, timestamp and latency alone, so they stay as they
    were;
  - OMOP reads the CDM tables and the `concept` table by code, never by concept id, so it works
    with any vocabulary load. Synthetic data uses local concept ids (2,000,000,000 and up).
    Population reports suppress cells under 10 people, like shared reports;
  - Da Vinci guides are pinned to their latest releases (CRD 2.2.1, DTR 2.2.0, PAS 2.2.1). The
    PA pack says `covered` with `pa-needed: auth-needed`, not `conditional`, because the pack
    knows the payer requires authorization. `conditional` would need `info-needed` and means
    coverage depends on unknowns. It leaves out `doc-purpose: withpa`: CRD 2.2.1's constraint
    crd-ci-q4 has a FHIRPath defect (an empty condition fails) that rejects `withpa` next to
    `auth-needed`, so the `reason` says it in words;
  - the PA pack goes to 0.1.1 for its CRD mapping, then 0.1.2 for DTR;
  - DTR finds the pack's decision through CRD's assertion id (the decision id) on the order,
    so the questionnaire comes pre-answered from what the pack found. The documentation asked
    for is derived from the pack's own decision table, not written out a second time;
  - `$questionnaire-package` needs the `decide` permission (the EHR's service key), not `read`,
    because it is an integration endpoint that reveals decisions;
  - PAS builds only from what the EHR supplies. The X12 service type and place of service come
    from the request or the order, and a missing element is an error, never a default. The
    request type (health services review) and certification type (initial) default to what a
    new outpatient request is. Dependent coverage (a subscriber who isn't the patient) is
    refused for now;
  - the sandbox payer certifies every well-formed request (A1). It exists to exercise the
    protocol, and decides nothing clinically.

- **v1.0 shape** (details in `notes/plans/v1.0-public-launch.md`):
  - OWASP ZAP (pinned image) for the API penetration test, run in CI against the engine with
    sign-in on. Medium and high risks fail;
  - MkDocs Material for the docs site;
  - Contributor Covenant 2.1;
  - a release workflow that stays inert until PyPI is configured and a tag is pushed;
  - the security review found one real gap: missing cross-origin isolation headers (ZAP
    90004). Every response now has COOP and CORP, and COEP too except on the API docs pages,
    which load Swagger UI from a CDN. The dashboard's two inline scripts are allowed by hash,
    so no `unsafe-inline` script is needed;
  - ZAP's two remaining low alerts are accepted: the HTML root in an API scan, and ZAP's own
    version notice. Medium and high alerts fail the build;
  - the docs site uses system fonts, so it makes no third-party requests. Links from docs to
    files outside `docs/` point at the repository, so they work on GitHub and on the site.
    Hosting waits for the public launch;
  - governance: a patient-safety objection blocks a change until resolved, and is not
    outvoted. Reference packs need a named clinical reviewer. Publisher types say who
    publishes, not that a pack is validated for a site;
  - the code of conduct adds one line to Contributor Covenant 2.1: sharing real patient
    information anywhere in the community is unacceptable.

## Skipped or blocked

- **Items in "What an A still needs" that code can't deliver:** a visual identity (type pairing,
  a designed mark), usability sessions with clinicians and evaluators, production-scale data,
  and a VoiceOver/NVDA, Safari/Firefox and real-device pass. These need people, devices or a
  designer.
- **Going public (the v0.5 exit, the v1.0 launch):** a public repository, a publicly reachable
  registry and Hub website, the hosted docs site, and the PyPI release. Everything up to the
  launch is built and runs in CI. What's needed from you:
  - the go-ahead to make the project public, then making the repository public and turning on
    GitHub's private vulnerability reporting (`SECURITY.md` points to it);
  - on PyPI, a trusted publisher for this repository, workflow `release.yml`, environment
    `pypi`, then a tag such as `v0.1.0`. The same tag publishes the container images to GHCR
    (`images.yml`);
  - the production registry's admin token and the `openhealth` namespace's signing key, created
    and stored as Vercel secrets;
  - a decision on whether the registry, Hub site and docs site sit outside Vercel
    Authentication, and where the docs site is hosted.
- **Clinical content:** a clinician's review of the reference packs' criteria and a harder,
  clinician-reviewed inbox benchmark (open items in `tasks/todo.md`). The tooling is built.
- **Paid model calls:** the trial pack's Jev-only leaderboard rows (about 200 calls). Left
  until you ask, per the "run once, then on request" rule.
- **Branches left in place:** three local branches that belong to other checkouts, all
  merged:
  - `feat/dashboard-tables` (#49) is your main checkout's current branch;
  - `claude/design-c` (#61) is checked out by another session's worktree;
  - `claude/autonomous-roadmap-execution-d448e1` is this session's own base branch.

  Every other local branch was merged (by PR, or superseded by merged PRs) and is deleted. So
  is the merged remote `feat/trial-leaderboard` (#24). The remote has only `main`.

## Final state

_2026-09-30, about 5:45 AM ET. `main` at #102, green; production (Vercel) healthy after every
merge._

**Pull requests:** 41 in this run (#62–#102), each merged on green CI after a self-review and a
preview check. Three went red first:
- #81: the lockfile, fixed at the root;
- #85: a font-dependent target size, now a lesson;
- #99: an upstream uv change, fixed at the root in #100.

No test or check was skipped, weakened or deleted to get green. `tasks/lessons.md` has five
new lessons, and two repeats noted where the rule already existed. Both repeats were a chained
command running past a failure, caught before any merge.

**CI now:** 730 tests and 119 browser tests; 13 jobs on every change:
- test (Python 3.11 to 3.13), browser, reference-pack, fhir-conformance, sandbox-e2e;
- hub-site, hub-e2e, cql, security, docs, quickstart, pilot-check, docker.

On pack PRs, the `packs` workflow runs the eval gate. On tags, `images` and `release` run.

### Roadmap: done and remaining

| Milestone | Status | Proof in CI |
|---|---|---|
| Design grade gaps | ✅ #62–#65 | browser suite |
| v0.4 Healthcare standards | ✅ #66–#74 | `sandbox-e2e`, `fhir-conformance` |
| v0.5 The Hub | ✅ built; going public is yours | `hub-e2e`, `hub-site` |
| v0.6 Readiness for health organizations | ✅ #83–#89 | `pilot-check` against a real OIDC provider |
| v0.7 Later standards (CQL, OMOP, Da Vinci CRD/DTR/PAS) | ✅ #90–#95 | `cql`; OMOP round trip; HL7 validator against CRD 2.2.1, DTR 2.2.0 and PAS 2.2.1 |
| v1.0 Public open-source launch | ✅ built (#96–#102); the launch itself is yours | `security`, `docs`, `quickstart`, `packs` |

**Remaining:** only what is listed under "Skipped or blocked". The roadmap's scorecard has
every goal-area row at ✅ except developer experience: the PyPI release and the first
published images, both one tag away.

### Self-grade

Against Apple and Airbnb for product design, top-tier data visualization (the FT, the New York
Times, Datawrapper) for charts, and the best open-source infrastructure projects for the
platform. These are honest grades: an A needs evidence from people using it, which a night of
engineering can't produce.

| Category | Grade | Why, and what an A still needs |
|---|---|---|
| Visual design | **B** | A disciplined, tokenized, system-font dashboard with a clear hierarchy. It has no identity of its own yet (type pairing, a designed mark), which needs a designer. |
| Consistency and design system | **A−** | Colour, spacing and layers are tokens, and CI enforces them and WCAG AA in both themes. The new views (sign-in, governance, deployments, monitoring) use the same patterns. Component sizes are still raw values. |
| Data visualization | **B+** | Honest axes, named configurations and direct labels, and every chart is keyboard-reachable with a data table. The weekly monitoring trend leaves gaps instead of inventing points. Not yet seen at production volume, and there are no annotations. |
| Interaction and UX logic | **A−** | Undo on overrides, links that carry the pack, skeleton-to-content fades, and roles that hide what the server would refuse. Reviews carry the signed-in name. Missing: usability sessions with clinicians. |
| Accessibility | **A−** | No control under 24 px, 44 px targets on touch, reduced motion, `aria-busy`, and focus management. Still needs a real screen-reader pass (VoiceOver, NVDA) and forced-colors. |
| Responsiveness | **A−** | Measured at 390, 768, 1440 and 1920 px in both themes. Never tested on real devices, Safari or Firefox. |
| Healthcare standards and interoperability | **A−** | FHIR R4 with US Core 6.1.0, SMART, CDS Hooks, Bulk FHIR, AuditEvent and Provenance, CQL, OMOP, and Da Vinci CRD, DTR and PAS. All of it is validated in CI, with the HL7 validator where an IG exists. Not yet run against a production EHR or payer. |
| Security and privacy | **B+** | A threat model with a test for every mitigation, OIDC with roles, tenancy, PHI encryption with logged reads, a strict CSP, and a ZAP scan and dependency audits in CI. An A needs an independent review or penetration test, and a real deployment's key custody. |
| Engineering quality | **A−** | 849 tests, types everywhere, and conformance and end-to-end checks against real third-party software: HAPI, smart-launcher, mock-oauth2-server, the HL7 validator and ZAP. Several larger modules could be split. |
| Developer and clinician experience | **A−** | `openhealth new` and a clinician's guide get a pack from nothing to evaluated in minutes, and the README quickstart runs cold in seconds, checked in CI. Not published to PyPI yet. |
| Documentation and community | **B+** | A docs site built strictly in CI, a threat model, contribution flow, eval gate, code of conduct and governance. None of it has met a community yet, because the repository isn't public. |
| **Overall** | **A− platform, B+ product design** | The platform stands with serious open-source health infrastructure. The design needs a designer and real users for the last step. |
