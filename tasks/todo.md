# Mendari: task tracking

Roadmap to the full goal: [`docs/roadmap.md`](../docs/roadmap.md).

## Done: the rename to Mendari (2026-09-30)
`openhealth` was taken on PyPI by another project, so OpenHealth Hub became **Mendari**
(mendari.co).
- [x] Repository renamed to `Junebugg1214/mendari` (still private)
- [x] Package, import and CLI `mendari`; settings `MENDARI_*` (the `OPENHEALTH_*` names still
  work); pack ids `mendari/…`; identifiers `https://mendari.co/…`; images `mendari-engine`
- [x] Kept: the Vercel project and URLs (`openhealth-hub`), the Jev connector
  (`jev/openhealth-hub`) and the Neon database name; dated records (the overnight log and the
  milestone plans) carry a rename note instead of being rewritten
- [ ] A "coming soon" page on mendari.co (a separate Vercel project; DNS at Porkbun)

## Done (built; the launch is the owner's): v1.0 Public open-source launch ([plan](../docs/plans/v1.0-public-launch.md))
Approved in advance with v0.5 to v1.0 (overnight run, 2026-09-29).
- [x] Plan
- [x] Security review: threat model, security headers, dependency audit and ZAP scan in CI,
  SECURITY.md
- [x] `mendari new` and a clinician's pack-authoring guide
- [x] Docs site (MkDocs Material, `mkdocs build --strict` in CI)
- [x] Contribution flow: pack-PR workflow with the eval gate; CONTRIBUTING, CODE_OF_CONDUCT,
  GOVERNANCE, templates
- [x] Release workflow (PyPI trusted publishing on tags) and the 10-minute quickstart in CI
- [ ] Going public: public repo, PyPI release, live Hub and docs site (the owner's decision;
  logged)

## Done: v0.7 Later standards ([plan](../docs/plans/v0.7-later-standards.md))
Approved in advance with v0.5 to v1.0 (overnight run, 2026-09-29).
- [x] Plan
- [x] CQL provider (Library/$evaluate on HAPI FHIR with Clinical Reasoning); the trial pack's
  criteria in CQL, checked against `criteria` in CI (job `cql`; `mendari agreement`)
- [x] OMOP CDM inputs and population evaluation; a synthetic OMOP round trip in CI
- [x] Da Vinci CRD: `coverage-information` on the PA pack's order-sign response (validated
  against CRD 2.2.1)
- [x] Da Vinci DTR: questionnaire package for the PA pack's gaps (validated against DTR 2.2.0)
- [x] Da Vinci PAS: request Bundle and submission to a mock payer (validated against PAS 2.2.1)

## Done: v0.6 Readiness for health organizations ([plan](../docs/plans/v0.6-org-readiness.md))
Approved in advance with v0.5 to v1.0 (overnight run, 2026-09-29).
- [x] Plan
- [x] OIDC login, roles and API keys; tenancy
- [x] Deployments and governance approvals
- [x] Monitoring: observed accuracy, overrides, drift, alerts; Monitoring view
- [x] PHI mode (hash / encrypt), retention, input reads logged; deployment guide
- [x] Container image per pack; on-prem templates
- [x] Pilot check in CI (a real OpenID Connect provider, navikt/mock-oauth2-server; job `pilot-check`)

## Done: design grade phases ([`docs/design-grade.md`](../docs/design-grade.md))
Graded C+ against Apple, Airbnb and best-in-class data viz on 2026-09-29; the user approved every
phase (A, B, C) to be done and merged when CI passes.
- [x] Phase A quick wins: layout shift at 768 px 0.50 -> 0 (the pack picker's width is fixed from
  first paint); content up to 1,440 px, centred (was 1,180 px, left); view titles 28/34 px (24/30
  on phones); one-line intros; rate axes start at 0, or zoom with a break mark and "The vertical
  axis starts at 90%"; configurations named once ("Rules + Jev 1.13.0"), critical labels and
  report stats in words, drawer titles in words; 44 px targets on touch screens, 28 px sort
  buttons and summaries for a mouse; hover transitions and a 1 px press; no text under 12 px
- [x] Phase B1: `app.js` (1,847 lines) split into 15 ES modules (`main.js` boots; one per view;
  `ui.js`, `router.js`, `drawer.js`, `pack.js`, `theme.js` shared), and `table.js`, `charts.js`,
  `form.js` import what they use instead of reading globals; the server fills an import map with
  content-stamped URLs and preloads every module. Spacing on the `--space-*` scale (half steps
  2 and 6 px added), layers on `--z-*`, one toggle-group pattern (joined bar or pills, two
  sizes); a test fails on any raw spacing or z-index value in components
- [x] Phase B2: the section list runs across the top up to 1,024 px (content gets the full width;
  four stats in two rows of two, never three and one); phones get a 56 px top bar (was 104) with
  a Settings sheet for theme and pack; views load into skeletons shaped like them, marked
  aria-busy (no more collapsing to "Loading…"); evaluation runs show an indeterminate bar and the
  elapsed time; any label but the recommendation waits 5 s with a focused Undo (leaving the view
  or the page sends it)
- [x] Phase B3: `/api/stats` daily entries gain `reviewed`, `overridden`, `labelled`, `agreed`
  (read-only, counted on the decision's day; tested on both backends). Overview: a 7 / 30 days /
  All period for the stats and the outcome mix, each rate with its change against the previous
  period, sparklines for Automated and Decisions; under 7 days of history a line per day instead
  of a mostly empty chart; outcome-mix segments with room carry their name and share. Cost chart:
  configurations at the same point share one named mark, near ones are dodged with a leader, and
  the deployed, safety-first leader and cost/accuracy frontier are named; the sweep names its
  lines at their ends
- [x] Phase C: motion (the drawer slides in and out, the dialog fades and scales in, a resolved
  review card folds away, Evaluations tabs crossfade, chart highlights and the settings sheet
  ease in; none under reduced motion); controls and the Decision Card's text sections unboxed
  (boxes kept for tables, charts and review items); the favicon's mark in the top bar in the
  theme's accent; stat labels 12 px, 500, uppercase, 0.04 em; empty states with an icon, one line
  and one action
- [x] Re-grade with the same measurements (110 captures, same script): **B+** (was C+), as
  projected. No layout shift at any width, no control under 24 px on any capture, 56 px phone
  bar; an A needs identity work, clinician testing, production-scale data and a real
  screen-reader and cross-browser pass (`docs/design-grade.md`, "Re-grade")
- [x] Check the production deploy after the last merge: `ad9d3d3` live, `/`, `/api/health` and
  the modules 200 through `vercel curl` (Postgres, 5 packs), unauthenticated requests 302 to login
- [x] Small gaps from the re-grade (`docs/design-grade.md`, "What an A still needs"), one PR each;
  log in `docs/overnight-log.md`:
  - [x] Plain words in criteria tables and Try-it answer headings
  - [x] Links that carry the pack (`?pack=` in the address)
  - [x] A transition from skeleton to content (the view fades in where its skeleton was)
  - [x] Fewer whole-subtree re-renders: sorting moves rows instead of redrawing the table

## Done: v0.5 The Hub ([plan](../docs/plans/v0.5-the-hub.md))
- [x] Plan
- [x] Shared database layer (`core/db.py`)
- [x] Signed pack archives and publisher keys
- [x] Registry service (namespaces, publisher types, keys, immutable versions, search)
- [x] CLI login / push / pull / search; `Decision.from_hub` from the registry
- [x] Shareable local-validation reports
- [x] Hub website (Next.js)
- [x] End to end: five packs published, clean-venv install and pull (CI job `hub-e2e`)
- [ ] Going public: a public registry, the site and the PyPI release (the user's decision; logged)

## Done: v0.4 Healthcare standards ([plan](../docs/plans/v0.4-healthcare-standards.md))
Approved in advance with v0.5 to v1.0 (overnight run, 2026-09-29).
- [x] Plan
- [x] US Core validation of FHIR inputs (warn by default, enforce on request; HL7 validator in CI)
- [x] Audit export as FHIR AuditEvent and Provenance
- [x] Sandbox: referral cases in FHIR (US Core; the inbox's Patients too)
- [x] CDS Hooks that fetch, under an allow-list (one outbound FHIR client for all later calls)
- [x] SMART on FHIR EHR launch (PKCE, OIDC), patient context in the dashboard
  - [x] Backend: launch, callback, server-side sessions, patient-context API
  - [x] Dashboard "This patient" view
  - [x] Sandbox launcher (smart-launcher-v2) and a scripted launch; CI job `sandbox-e2e`
- [x] Bulk FHIR `$export` into local-validation datasets (backend services auth; `mendari sandbox bulk-check` in CI)
- [x] End-to-end exit checks in CI (`sandbox-e2e`); docs

## Open items outside a milestone
- [ ] Harder inbox benchmark: paraphrased messages and held-out splits for every critical label,
  reviewed by a clinician (from v0.2; the generation tooling can be built without one)
- [ ] Trial pack: Jev-alone leaderboard rows on test and the local site (about 200 paid Jev calls;
  only when asked)
- [x] Try it on record packs: a form generated from the pack's input schema (`form.js`: fields,
  dropdowns and date pickers, editable tables for lists, JSON for anything else), with the JSON
  one click away; every behaviour-test example of the four record packs round-trips unchanged
- [ ] VoiceOver spot check of the redesigned dashboard (a person at the Mac; script in the QA report)

## Done: Dashboard redesign ([`docs/ui-ux-review.md`](../docs/ui-ux-review.md))
Plan approved 2026-09-29 (8 PRs, about 10 working days); the user's decisions are in the plan's
section 8 (both audiences, safety-first leaderboard, Playwright, admin-only evaluations with v0.6
roles, feedback that captures the right answer, Eastern time everywhere people look).
- [x] Phase 0: browser smoke suite (`pytest-playwright`, marker `browser`, own CI job) and
  accessibility/correctness fixes: dark-mode primary buttons and light badges reach AA, form
  fields 3:1 borders, audit records open from the keyboard, ids no longer wrap one letter per
  line, a new view starts at the top with its heading focused, no `aria-live` on `main`, every
  control named, Decide shows progress and can't be submitted twice, the sandbox notice stays on
  phones, reduced motion respected, the record example picker matches its input
- [x] Phase 0b: Eastern time everywhere people look. `MENDARI_TIMEZONE` (default
  `America/New_York`, `tzdata` added) sets calendar days (Overview daily volume), the daily call
  counters, "today" for criteria and FHIR ages, card dates and every time the dashboard shows
  (with EDT/EST, whatever the viewer's clock); `/api/health` reports it. Stored timestamps, the
  audit chain, API JSON and FHIR stay UTC
- [x] Phase 1: design tokens and theming. `style.css` rebuilt on semantic tokens (colour for both
  themes, type, space, radii, elevation, motion; no hex outside the token blocks); System / Light /
  Dark toggle, saved, applied before first paint by a head script (no flash) and following the
  system setting live; Lucide icons (ISC, vendored sprite) replace emoji; plain-word outcome and
  priority labels with the codes in tooltips; stat is a solid red chip; inline styles replaced by
  classes (only data-driven bar widths remain); `tests/test_dashboard_contrast.py`; browser tests
  for the toggle, no flash and the live system setting
- [x] Phase 2: tables and leaderboard, audit drawer. `table.js` (caption, sticky header, sortable
  keyboard headers with aria-sort, right-aligned numbers, rank, sticky first columns with edge
  shadows); leaderboard ranked safety-first with the deployed row marked, one column per critical
  label, constant columns and slots said once above the table, datasets remembered (held-out set
  first); Evaluations sub-views with URLs (`#evals/compare|report|run|history`, which also fixes
  the picker/report mismatch); per-check accuracy behind a disclosure; subgroups grouped, lowest
  first, small n marked; audit rows with compact ET times open a drawer at `#audit/<id>` (focus
  held inside, Escape returns to the row) with a hash-chain timeline and the record's JSON folded;
  feedback moved from every audit row into the drawer
- [x] Phase 3: charts. `charts.js` (no library): stacked columns, lines, scatter and horizontal
  bars as SVG drawn at the container's real width (text stays 12 px on a phone, redrawn on resize;
  colours are token classes, so a theme switch needs no redraw). Marks are one Tab stop per chart
  (arrow keys, Home, End), with tooltips on hover and focus and exact values in each aria-label;
  every chart with marks has a "View data" table. Wired in: Overview daily chart (last 30 days,
  starts at the first decision so a young deployment isn't a run of empty days; the table lists
  every day), report reliability scatter (dot area = cases, diagonal = calibrated) and threshold
  sweep (the pack's automation threshold marked, and a what-if note when the run automated
  nothing), Try-it probability bars coloured by what the label does under the pack's policy
  (block = danger, review = warning; a derived decision's criteria by their effect in its table;
  no more green block verdicts),
  and the optional ones: leaderboard cost-against-accuracy scatter (log cost, "$0" slot for free
  configurations, diamonds for configurations that missed a critical case, deployed one ringed)
  and a report confusion matrix (only mistakes shaded). Deviations from the plan text: the SVG is
  `role="group"` (not `img`) so its focusable marks stay exposed to assistive technology; the bars
  are static `role="img"` with every value in one label and printed on the chart, so no data table.
  Tests: 12 new browser tests, chart colour pairs in the contrast test, `charts.js` in the asset
  versioning. A review after the PR opened fixed a sweep label that claimed automation on the four
  packs that automate nothing, axis text read out by screen readers, a focus ring that looked like
  the deployed ring, a hover-only confusion-matrix hint and a vague cost tooltip (lessons.md)
- [x] Phase 4a: feedback backend (the right answer on a thumbs-down). `POST /api/audit/{id}/feedback`
  takes an optional `expected: {question, label}` on a thumbs-down, checked against what the
  decision answered (one of its questions, one of that question's labels, not the answer it
  gave; a derived decision's checks included). Ratings can be changed: a person's new rating
  replaces their earlier one in counts, lists and exports (`superseded_by`, set in the same
  serialised write, so "latest" never depends on timestamps), and the hash chain keeps both
  (`expected` and `replaces` in the event). Unnamed ratings of a decision count as one person, so
  a mis-click can be put right. `stats.feedback.down_with_expected`. The export fills `labels`
  where raters agree and leaves disagreements visible in `feedback`. Additive migration on both
  backends under the schema lock (three nullable columns, one more than the plan: `superseded_by`),
  which also resolves earlier duplicate ratings to each person's latest. Checked on a copy of a
  pre-4a database (chain verifies before and after). UI comes in Phase 4
- [x] Phase 4: screen redesigns. Overview: a header naming the risk level and what is deployed in
  words (codes one line below), four primary stats with what each is out of (Automated, Waiting for
  review with a link and a warning tone, Override rate, Observed accuracy with its n), one row of
  secondary facts (volume, latency, cost at a readable scale, audit chain with an icon, feedback),
  and an empty state with next steps instead of a wall of zeros. Review queue: grouped by priority
  with counts, a solid banner on stat cards with the rule's reason, the recommendation as the
  primary button, focus moving to the next card. Try it: the result leads with the outcome in
  words and colour ("Blocked · urgent", solid for BLOCK and stat), the fallback as a callout,
  criteria effects and probability bars coloured by consequence (block, review, including labels
  the policy never automates), the result focused. Feedback (decision 5): Right / Wrong pressed and
  changeable, Wrong opens an optional "What should it have been?" (the decision or one of its
  checks), audit rows show current ratings read-only, the Feedback page leads with "Wrong on
  automated" and "Ready to become tests" and links each case to its record. Decision Card: a
  document with a table of contents whose links are routes (`#card/policy`), facts as a definition
  list with a copyable digest, prose at 72ch, policy in words, and an "Automates" line that says
  whether this deployment automates at all (risk level, required providers). Evaluations: report
  stats in the same hierarchy; "Run evaluation" asks first when a remote model is deployed, naming
  the dataset, its size and the models (decision 4). Small read-only API additions:
  `GET /api/packs/{id}/datasets` (sizes), each audit row's current ratings, `stats.labelled`.
  Fixed on the way: a drawer replaced by another ran the old one's close handler and closed both
  (Phase 2), and a deep link to a section is focused on first load
- [x] Phase 5: QA pass. Keyboard walkthrough of every view on a message and a record pack (every
  Tab stop, focus indicators, unreachable or hidden focus, the main flows), an accessibility-tree
  audit of every view on three packs (headings, landmarks, names, table headers, alerts), 72
  screenshots (9 screens, 2 packs, 1440/390, light/dark) reviewed as contact sheets, contrast tests
  confirmed in required CI jobs. Fixed: a "Skip to content" button (12 Tab stops before content);
  a white focus ring on solid danger headers (blue measured 1.06:1 / 2.0:1); the phone section list
  shows edge shadows and keeps the current section in view; `[hidden]` always hides (a derived
  decision's follow-up showed every check's choices, and taps outside the selected one were
  ignored); errors announced as alerts and shown 8 s, a failed view with "Try again"; report
  panels at h2 (was h1 -> h3); header rows on the card's question tables; `aria-current` on the
  active section; no focus rectangle on headings the app focuses. 8 new browser tests (62 in all).
  QA report with the screenshots: https://claude.ai/artifact/GmDM3pEDMEEnYkb9GBcL4P (private).
- [x] Favicon (#54): `/favicon.ico` (16/32/48 px, from `favicon.svg` via `scripts/make-favicon.py`)
  ended the only 404 left in production after the redesign
  VoiceOver itself is for a person at the Mac (the report has a two-minute script)

## Done: v0.3 Reference packs: AI Guard (the fifth and last)
Plan approved 2026-09-29 (3 PRs): runtime → the pack (risk level 2, the user's choice) →
leaderboard and deployed choice.
- [x] PR 1 runtime: `dosing` provider (dose arithmetic in code), `guard` section,
  `mendari.guard.check` and `POST /api/packs/{id}/guard` (released / held_for_review / blocked)
- [x] PR 2 `mendari/ai-guard` 0.1.0 (L2): six checks (screen: rules default, jev, llm,
  rules_and_jev, rules_and_llm; dosing in code), derived `verdict` pass / review / block,
  possible-block rule (P >= 0.3) with a nurse-triage fallback; synthetic data (calibration 300 /
  test 600, local 300) and hand-written draft sets. Rules: 11/12 blocks tuning, 4/9 held out;
  nothing released without a model (`automation_requires`) and a governance override (L2).
- [x] 0.1.1 criteria: over-the-counter advice is in scope, a dose that is too high is unsafe
  advice (labels relabelled uniformly), found by a model reader on the tuning set
- [x] PR 3 leaderboard (`evals/leaderboard.json`, 34 rows on 0.1.1): rules; Jev, Claude Sonnet 5,
  Claude Haiku 4.5 and gpt-oss-120b, alone and with the rules. With the rules every model-backed
  screen blocked 21/21 hand-written drafts to block; none released
- [x] `MENDARI_JEV_DAILY_LIMIT=none` (no limit, still counted), at the user's request
- [x] Deployed with the user's choice: `mendari/ai-guard:screen=rules_and_jev`. Live: a
  missed emergency, a stroke in words the rules don't list and an unlisted-brand overdose are
  blocked; a safe draft waits for one-step approval
- [x] 0.1.2: synthetic drafts fit their questions; leaderboard re-measured (LLM screens 0.90-0.98
  verdict accuracy, rules + Jev 0.80-0.88)
- [x] Switched to the user's new choice: `mendari/ai-guard:screen=rules_and_llm` with
  `LLM_MODEL=anthropic/claude-sonnet-5` and `MENDARI_LLM_DAILY_LIMIT=none` (production and
  preview). Sonnet + rules was the only screen that never missed a draft to block in any run

## Done: v0.3 Prior Authorization Preflight
Plan approved 2026-09-29 (3 PRs): runtime → the pack → leaderboard and deployed choice.
- [x] PR 1 runtime: derive `when` over several members (AND) and label lists in `any`/`all`;
  `criteria` answers any question label and checks may carry a field `when`
- [x] PR 2 `mendari/prior-auth-preflight` 0.1.0 (L1): service/payer requirement sets as
  derive rows, structured elements (criteria) and note elements (rules default), synthetic
  data (calibration 300 / test 600, local 300) and hand-written note sets. Rules: 7/7 red flags
  tuning, 3/6 held out; nothing automated without a model (`automation_requires`).
- [x] PR 3 leaderboard: rules; Jev with rules (all sets) and alone (hand-written sets); 3 LLMs
  alone and with rules. Every model-backed config caught 6/6 held-out red flags, none
  automated, 100 % next step held out, 0 false automation; rules_and_jev automates 33-47 %
  at $0.00003 and 0.2 s.
- [x] Deployed with the user's choice: `mendari/prior-auth-preflight:notes=rules_and_jev`
  (production and preview); redeployed; `/api/health` confirms. Live order-sign: a complete
  lumbar MRI is submitted automatically, a knee arthroscopy with gaps goes to a person, the
  lab order is ignored, and a red flag the rules miss is caught by Jev (urgent review).

## Done: v0.3 Referral Router
Plan approved 2026-09-29 (4 PRs): runtime → FHIR `order-sign` → the pack → leaderboard and
deployed choice.
- [x] PR 1 runtime: derive rows gated with `when`, `criteria` checks `exists` and `value`,
  Task/card templates naming answers (`{specialty}`), spec 0.2 doc
- [x] PR 2 FHIR `focus_where` and CDS `order-sign` (draftOrders in context, one card per referral);
  `exists.undated`; fixture pack `referral-mini`
- [x] PR 3 `mendari/referral-router` 0.1.0 (L1): specialty (naive Bayes), urgency and clear
  question (rules), checklist (criteria), derived `next_step` with `when` rows; synthetic
  referrals (train 1500 / calibration 300 / test 600, local 300) and hand-written urgent sets.
  Rules: 16/16 tuning, 7/12 held out, and all 5 misses would have been automated, so the new
  `policy.automation_requires` allows AUTO only with a model in the urgency screen. Also:
  `mendari train` on an untrained pack, YAML errors in pack/policy reported cleanly,
  `validate` checks behaviour-test inputs against the schema.
- [x] PR 4a (#29) 0.1.1: `clear_question` escalates `yes` in rules_and_* (the rules alone sent
  clear referrals back; found in the comparison)
- [x] PR 4 leaderboard: offline, Jev (alone, with rules), 3 LLMs (alone, with rules) on
  holdout/tuning/test-100/local-100. Every model-backed config caught every urgent referral,
  none automated. rules_and_jev: 100 % next_step held out, AUTO 27-47 % with 0 false
  automation, 0.2 s, $0.00004. Jev daily limit raised to 2000 (production) with the user.
- [x] Deployed with the user's choice: `mendari/referral-router:specialty_reader=jev` and
  `mendari/referral-router:triage_screen=rules_and_jev` (production and preview); redeployed;
  `/api/health` confirms; live order-sign gives return_for_info, urgent_review and an AUTO
  schedule on three referrals, and ignores the lab order.

## Done: v0.3 Clinical Trial Prescreener
Plan approved (5 PRs): runtime prerequisites → FHIR/CDS generalised → the pack → multi-pack
dashboard and deployment → leaderboard and deployed choice.
- [x] PR 1 runtime: derive rows scoped with `in`, `criteria` provider, reasons naming the
  deciding criteria, pack-scoped provider settings
- [x] PR 2 FHIR lists/transforms and multi-resource index; CDS card wording from the pack
- [x] PR 3 `mendari/clinical-trial-prescreener` 0.1.0 (synthetic GLYCEMIA-2 study):
  7 structured criteria (`criteria`), 4 note exclusions (English rules default; Jev/LLM
  variants), derived `eligibility`, L2 policy with pregnancy/CV review flags, FHIR mapping,
  ResearchStudy, `patient-view` card; `synthetic_trial.py` (test 300, calibration 200, local
  site 150); hand-written `exclusions` (tuning) and `exclusions_holdout` (written first).
  Rules: 15/15 tuning, 8/12 holdout; local site 84% vs 99% public. Local datasets are now
  offered only to the pack whose questions they label.
- [x] PR 4 multi-pack dashboard: record inputs rendered generically (message packs unchanged),
  criteria table for a derived primary (answer, confidence, effect in the table, source,
  evidence) in Try-it, review, feedback and audit; Try-it JSON editor with the pack's behaviour
  tests (`GET /api/packs/{id}/examples`); local-site dataset preselected per pack; top-bar
  overflow at phone width fixed. Deployed with the trial pack on `notes=rules` until PR 5.
- [x] PR 5 leaderboard for the trial pack (rules, Jev, rules_and_jev, 3 LLMs alone and with
  rules; test-100, local-100, exclusions, exclusions_holdout). Live Jev `noul`: 0 errors, note
  ECE <= 0.05. Every model alone found 12/12 held-out exclusions (100% eligibility); adding
  the rules lowered eligibility to 92% held out / 87% local. Card 0.1.1 says so.
- [ ] Jev-alone rows on test and the local site (200 Jev calls; needed the next day's budget;
  now under "Open items" above)
- [x] Deployed notes reader chosen with the user: `notes=jev`. MENDARI_PROVIDERS (production
  and preview) = `router=jev,safety_screen=rules_and_jev,mendari/clinical-trial-prescreener:notes=jev`

## Done: v0.2 Model-agnostic core
- [x] Detailed v0.2 plan approved (5 PRs: spec, Jev batching, composition, LLM, leaderboard)
- [x] Spec 0.2: `boolean` / `score` question types, derived questions (decision tables with exact
  probabilities), `min_score`/`max_score` rules; `docs/spec/decision-pack-0.2.md`; fixture pack
  `tests/fixtures/packs/trial-mini`
- [x] LLM provider via Vercel AI Gateway (Claude default; OpenAI-compatible local models),
  `mendari calibrate`, gateway-reported cost, `MENDARI_LLM_DAILY_LIMIT`
- [x] Composition: `cascade` and weighted `vote` (shared composite base; shared temperature
  calibration in `providers/calibration.py`)
- [x] Jev: one call per decision (per-decision batching, one budget unit), `noul` / `score`
  mapping (live check of `score`/`noul` deferred to the trial pack in v0.3)
- [ ] Harder inbox benchmark + held-out splits for every critical label (now under "Open items"
  above)
- [x] Leaderboard (`mendari leaderboard`, card + dashboard), inbox pack 0.2.0 variants
  (`llm`, `cascade`, `rules_and_llm`)
- [x] Leaderboard LLM rows: Claude Sonnet 5, Claude Haiku 4.5, gpt-oss-120b (each with
  `rules_and_llm` and LLM-only screens)
- [x] Jev adapter aligned with Jev's docs: answer confidence = label probability (Jev's
  `confidence` is a distribution-shape statistic), published price, 429/529 retry, real limits
- [x] Leaderboard Jev rows (jev-1.13.0, measured on production): `router=jev,
  safety_screen=rules_and_jev` on all three datasets, `router=cascade` on `test`
- [x] Deployed mix confirmed: `router=jev,safety_screen=rules_and_jev`; `JEV_MODEL=jev-1.13.0`
  pinned (Production and Preview)
- [x] Emergency threshold: spec 0.2 `min_probability`; inbox 0.3.0 escalates at P(urgent) ≥ 0.35
- [x] `urgency` reworded (instructions and criteria agree; doubt handled by the policy)
- [x] Leaderboard Jev rows for pack 0.3.0 (production, jev-1.13.0; `MENDARI_JEV_DAILY_LIMIT`
  raised to 700 in production)
- [x] Preview databases: Neon branch per preview again, deleted on PR close
  (`neon-preview-cleanup` workflow); evaluations keep the request context (OIDC) in threads
- [x] Exit: 10 configurations published incl. Claude and an open-weight model; one Jev call per
  decision in production; deployed mix catches 48/48 red-flag emergencies, none automated


---

# Done: Mendari v0.1 — Decision Pack runtime + Patient Inbox Router

## Objective
A runnable, fully synthetic reference implementation of Mendari v0.1: a Decision Pack
spec, a model-agnostic runtime, and one high-quality reference pack (Patient Inbox Router) that
routes synthetic patient messages while measuring accuracy, calibration, abstention, overrides,
latency and cost.

## Plan
- [x] Step 1: Repo scaffold (uv, pyproject, ruff, mypy, pytest, CI)
- [x] Step 2: Decision Pack spec (pydantic models + validation + risk-level rules)
- [x] Step 3: Providers: rules, naive_bayes (local ML), jev (HTTP adapter) → common `Answer` contract
- [x] Step 4: Policy engine: AUTO / HUMAN_REVIEW / ABSTAIN / BLOCK with risk caps and governance overrides
- [x] Step 5: Audit trail (SQLite, hash-chained events) + human-review queue
- [x] Step 6: Synthetic dataset generator (public benchmark + shifted "local site" dataset)
- [x] Step 7: Patient Inbox Router pack (schemas, FHIR mapping, rules, trained model, policy, tests, card)
- [x] Step 8: Evaluation harness + local validation (degradation localisation)
- [x] Step 9: FHIR R4 adapter (Communication/Patient → input, decision → Task) + CDS Hooks service
- [x] Step 10: FastAPI server + developer dashboard (overview, review, audit, evals, card, try it)
- [x] Step 11: CLI (`list validate test decide eval train card schema serve audit sandbox demo`)
- [x] Step 12: docker compose sandbox (HAPI FHIR v8.12 + engine + workflow worker)
- [x] Step 13: Docs (README, spec, architecture, ADRs) and private GitHub repo

## Notes
- Jev adapter: typed `choice` questions, ≤8 per call, 2–255 labels, probabilities must sum
  to 1, chosen label must be the argmax. Endpoint, model and key are deployment settings.
- Mendari is fully separate from other projects: its own repo, Vercel project, connector
  (`jev/openhealth-hub`), database and test containers. Nothing is shared.
- Core abstraction: a pack declares *questions* (labels + criteria). Providers answer questions
  with probability distributions. Policy turns answers into an outcome (ADR-0001).
- Risk caps live in the runtime; level 2–3 AUTO needs a deployer's GovernanceOverride (ADR-0002).

## Results

### Completed
- All 13 plan steps. 112 tests; ruff, ruff format and mypy clean.
- Benchmark (synthetic test, n=600): 99.3% routing accuracy, ECE 0.005, 0 false automations,
  23/23 urgent detected, 0 urgent automated.
- Local validation (local-site example, n=400): 93.2%; degradation localized to Spanish
  billing/refill phrasing; 18/18 urgent detected, 0 automated; 0 false automations.
- Docker sandbox verified: 40 seeded messages → 40 decisions → 40 FHIR Tasks
  (4 stat escalations, 10 in the review queue), exactly once.

### Deployed (2026-09-28)
- Vercel project `openhealth-hub` (FastAPI function, Fluid compute, iad1), Neon Postgres
  `openhealth-hub-db`, Jev via connector `jev/openhealth-hub` (`JEV_MODEL=jev-latest`, which
  currently resolves to `jev-1.13.0`). Vercel Authentication on all deployments.
- Production smoke tests passed: live Jev routing, stat escalation, review persistence,
  audit chain, Jev eval (100% on local-site sample), CDS discovery, dashboard assets.

### Deferred (v0.2+)
- [x] Pin a concrete Jev model: `JEV_MODEL=jev-1.13.0` (v0.2).
- [x] Neon preview branching so previews don't write to the production database (v0.2).
- [x] LLM provider (v0.2). The CQL provider is roadmap v0.7.
- [ ] Remote hub (`mendari pull`, signed packs): roadmap v0.5. Server authentication: v0.6.
  AuditEvent export: v0.4.
- [x] Other four reference packs (Referral Router, PA Preflight, Trial Prescreener, AI Guard)
  (v0.3).
- [ ] Harder, clinician-reviewed synthetic benchmark: under "Open items" at the top.

### Discovered
- Keyword red-flag screens miss typos → added deterministic typo tolerance (lessons.md).
- HAPI search caching breaks naive FHIR work queues → If-Match claims (lessons.md).
- Cosmetic template edits change derived data → reproducibility CI job (lessons.md).
