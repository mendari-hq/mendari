# Wave 6 inventory: what stays, what goes, what is renamed

Survey of `main` at `33b32e4b` (2026-11-24), read-only. Every count is from the tracked tree
(`git ls-files`): lines are `wc -l`, test functions are `def test_` (a parametrized function counts
once). Companion documents: `wave6-fixtures.md` (the new synthetic agents),
`wave6-build-plan.md` (phases), `wave6-decisions-needed.md` (the GRAY items, G1 to G16).

## 0. How to read this

**Classes.**

- **KEEP**: stays. At most it is renamed (section 4) and loses compatibility and pre-release
  code (sections 2 and 3).
- **REMOVE**: deleted. The evidence is what reaches it. Nothing in KEEP does: the demo agent
  (`agents/portal-message-agent`), what `mendari new` writes, the three starters and the agent
  kit, the documented agent workflow (README's five-minute path and the 14 guides the lead
  named), or the cross-cutting platform.
- **REWRITE**: stays, but its tests move off the former packs onto new fixtures.
- **GRAY**: the lead decides. Each GRAY item has an ID (G1 to G16 in
  `wave6-decisions-needed.md`), evidence and a recommended default. **The counts in this
  document assume every recommended default.** Section 0.3 shows what changes if a default is
  turned down.

**Baseline.**

- 1,322 tracked files.
- `src/mendari`: 285 files, 126,073 lines (Python about 107,500).
- `tests`: 587 files, with 4,875 test functions in 335 files. Of those, 377 functions are in 35
  browser files.
- Lines that hold the word (as a word or part of an identifier, not inside the allowed words of
  section 4.13), by area:

  | Area | Lines | Files |
  |---|---|---|
  | `src` | 5,326 | 201 |
  | `tests` | 9,753 | 374 |
  | `scripts` | 598 | 34 |
  | `docs` | 227 | 43 |
  | `CHANGELOG.md` | 319 | 1 |
  | `hub` (Next.js) | 91 | 11 |
  | `.github` | 57 | 6 |
  | `sandbox` | 46 | 5 |
  | `examples` | 9 | 5 |
  | `CLAUDE.md`, `README.md`, `SECURITY.md`, `CONTRIBUTING.md` | 35 | 4 |
  | `web`, `agents`, `docker-compose.yml` | 0 | 0 |

### 0.1 GRAY items and their recommended defaults

| ID | Item | Default |
|---|---|---|
| G1 | Providers `llm`, `jev`, `naive_bayes`, `cql`, `criteria`, `dosing` | keep `llm`; remove the other five |
| G2 | Composites `any_of`, `cascade`, `vote`, `composite`, `batch` | remove `any_of`, `cascade`, `vote`; keep `composite` (base of `check`) and `batch` (with `llm`) |
| G3 | `mendari train`, `mendari improve`, `evals/training.py`, `mendari agreement` | remove |
| G4 | `standards/davinci.py`, `pas.py`, X12, `omop.py`, `record_fhir.py` | remove; move `davinci.question_system` into `terminology.py` |
| G5 | `standards/bulk.py`, `us_core.py`, `data_coverage.py` | keep |
| G6 | Record reading: `linked.py`, `ehr_declarations.py`, `core/site_codes.py`, spec 0.3 mapping extensions, order hooks | remove |
| G7 | `terminology.py` code systems | keep the generator and Mendari's systems; remove the five fictional systems |
| G8 | `sandbox/` commands | keep `seed`, `worker`, `smart-launch`, `cds-client`, `bulk-check`, `pilot-check`; remove `payer`, `omop-write`, `omop-check` |
| G9 | `sandbox/inferno`, CI jobs, `terminology.yml`, `conformance.yml` | keep and rewire `fhir-conformance`, `r4-structure`, `sandbox-e2e` (demo half), `pilot-check`, `quickstart`, `terminology.yml`, the `smart-client` and `bulk-client` suites; remove `cql`, `dtr-payer`, `crd-server` |
| G10 | `community/`, `community-agents/` | remove the contribution path; keep the bounded engine |
| G11 | `templates/base` | keep (merge with `templates/agent`) |
| G12 | Spec versions 0.1 to 0.3; manifest sections only the packs used | one version, `0.4`; remove `guard`, `derive`, `code_systems`, CRD/DTR keys, `policy:` as a file |
| G13 | Hidden evaluation sets and the scoreboard | remove; keep the labeller role and the blind grading screen |
| G14 | The agent store (`core/pack_store.py`) | keep |
| G15 | Site results | keep |
| G16 | Other findings (`openhealth` reserved, the `server` extra, `synthetic_cases.py`, notes-backed tests) | see `wave6-decisions-needed.md` |

### 0.2 Totals, with every default

| What | Files | Lines | Test functions |
|---|---|---|---|
| Deleted, whole files: `src` | 44 | 19,291 | |
| Deleted, whole files: fixtures (former packs, scoreboard, datasets, minis, US Core, audit, community) | about 205 | about 61,000 (data) | |
| Deleted, whole files: test modules | 66 | about 33,000 | 887 |
| Deleted, whole files: test support | 10 | 4,615 | |
| Deleted, whole files: scripts | 5 | 1,372 | |
| Deleted, whole files: docs pages, templates, Inferno kit | about 25 | about 5,000 | |
| Rewritten onto the new fixtures | 164 test files | | 2,699 (about 200 of them deleted in place) |
| Kept, renamed only | 100 test files | | 1,247 |
| Vocabulary tests (rewritten as one rule) | 4 | | 39 |

### 0.3 What turning down a default moves

| If the lead keeps | Deleted test files return as REWRITE | Test functions | `src` lines kept |
|---|---|---|---|
| G13, hidden sets and scoreboard | 26 | 340 | 10,642 (12 files) |
| G10, community | 4 | 126 | 2,040 (8 files) |
| G6, record reading | 8 | 93 | 1,148 (3 files), plus about 900 in-file |
| G1, G2 and G3 (any of them) | up to 9 | up to 113 | up to 3,268 (11 files) |
| G4, Da Vinci, PAS, OMOP | 5 | 62 | 1,799 (6 files) |
| G12, `guard` | 1 | 6 | 63 (`guard.py`); `derive` also keeps 172 (`core/derive.py`) |

Keeping a feature also keeps its docs, its CI job and, where noted in `wave6-fixtures.md`, one
more fixture.

## 1. Inventory by area

### 1.1 `src/mendari` (package root)

| Module | Lines | Class | Notes and evidence |
|---|---|---|---|
| `__init__.py` | 39 | KEEP | Drop `adopt_legacy_settings`; export `Agent`, `load_agent`, `resolve_agent` |
| `agent_kit.py` | 689 | KEEP | Contract keys renamed (4.10); copied byte for byte into `examples/*/mendari_agent_kit.py` |
| `alerts.py`, `notices.py`, `parallel.py` | 347, 61, 37 | KEEP | |
| `cli.py` | 6,793 | KEEP | Loses about 1,200 lines. Commands go: `train`, `improve`, `agreement`, `scoreboard *`, `evalset *`, `access grader-key`, `pas build`, `omop *`, `sandbox payer`, `sandbox omop-*`, `community-check`, `new --community`. The rename parser goes: `_Parser.renamed`, `--pack`, `--pack-dir`, `mendari pack`, the hidden `new --agent` |
| `errors.py` | 252 | KEEP | `PackValidationError` and `PackNotFoundError` renamed; `NoEvalset`, `GradingRefused`, `NoAssignment`, `LabelError` move with G13 (the labeller errors stay) |
| `guard.py` | 63 | REMOVE (G12) | Only `server/app.py` imports it (the `/guard` route). Only `ai-guard` and `guard-mini` declare `guard:`. `docs/guides/api.md:160` says the demo agent declares none |
| `install.py` | 1,188 | KEEP | Holds no old name |
| `legacy.py` | 65 | REMOVE (compatibility) | Section 2 |
| `mcp.py` | 377 | KEEP | `QUEUE_FIELDS` keys renamed |
| `settings.py` | 513 | KEEP | Loses `legacy.setting`, `JEV_DEFAULTS`, `MENDARI_PAS_*`, `MENDARI_JEV_DAILY_LIMIT`, `community_packs_enabled`, `MENDARI_SITE_CODES`, `MENDARI_EHR_DECLARATIONS` |

### 1.2 `core`

**KEEP, renamed only:** `__init__`(1), `accounts`(147), `actions`(404), `checkpoint`(123),
`clock`(37), `connections`(459), `crosswalk.yaml`(820), `https_only`(37), `input_schema`(587),
`patterns`(303), `phi_detect`(424), `phi_names.txt`(1,297), `policy`(572), `query`(63),
`rate_buckets`(86), `re2_syntax`(586), `schema_graph`(172), `status`(23), `templates`(174),
`tenancy`(86), `text`(97), `validation_brief`(432).

**KEEP, with edits:**

| Module | Lines | Edits |
|---|---|---|
| `audit.py` | 7,655 | Pre-release code out (section 3); evalset tables and verification out (G13); `pas.submitted` and `LEGACY_TEXT` out (G4); columns, indexes and event kinds renamed (4.6, 4.7). About −2,000 lines |
| `caps.py` | 320 | Bounded engine; rename the `COMMUNITY_*` constants (G10) |
| `card.py` | 800 | Calibration wording uses `service_id`/`service_version`; leaderboard section stays |
| `db.py` | 396 | Gains the baseline marker check (section 3.3) |
| `decision.py` | 1,202 | Loses linked reads, EHR declarations and site codes (G6) |
| `origin.py` | 384 | Loses the `community` origin (G10) and `"openhealth"`; community URL out |
| `pack.py` → `agent.py` | 1,209 | Loses `LEGACY_MANIFEST`, `MANIFEST_NAMES`, the `manifest_name` two-name logic, and the `criteria` 0.3 check (G1/G12) |
| `pack_store.py` → `agent_store.py` | 1,782 | G14 keep. Loses the hidden-set check and `store_matches` holds (G13) |
| `packet.py` | 3,108 | Calibration entries renamed (`service_id`, `service_version`) |
| `phi.py` | 396 | Loses legacy tokens without key ids (section 3) |
| `reference.py`, `registry.py` | 78, 270 | Lose the old `packs/` cache, `legacy.setting` and the community search path |
| `session_seal.py` | 83 | Reword the "row from before sealing" note; the tamper handling stays |
| `site_cases.py` | 81 | Keep: `feedback export` marks cases, and `hub/archive.py` refuses them. Users `training` and `improve` go |
| `spec.py` | 1,517 | One spec version; `guard`, `derive`, `code_systems`, `coverage_information`, `questionnaire` out (G12) |
| `written.py` | 896 | Imports the labeller helpers from their new home (G13) |

**REMOVE:**

| Module | Lines | Evidence |
|---|---|---|
| `derive.py` | 172 | G12. Only `derive:` in former packs and the guard, prescreen, referral and trial minis (manifest survey). Imported by `decision.py` and `rater_agreement.py` (hidden-set labelling, G13) |
| `evalset_keys.py` | 255 | G13 |
| `evalsets.py` | 3,528 | G13. Before deleting, move `NO_ASSIGNMENT`, `labeller_in`, `labeller_membership` and `user_ids` (`core/written.py:56`) into a KEEP module |
| `site_codes.py` | 287 | G6. Used only by the record-reading path (`decision.py`, `smart_routes.py`, `app.py`) for the trial and referral records |

### 1.3 `providers`

**KEEP:** `__init__`(232, trimmed), `agent`(950), `agent_calibration`(357), `base`(423),
`batch`(91, G2), `calibration`(58, the `llm` temperature fit), `check`(171), `composite`(188,
G2), `credentials`(171), `deadline`(101), `fixture`(88), `http`(274), `judge`(304), `llm`(632,
G1), `reach`(242), `repeat`(190), `rules`(276), `signals`(146).

**REMOVE:**

| Module | Lines | Evidence (manifests using the type; who imports it) |
|---|---|---|
| `jev.py` | 528 | All five former packs and nothing else. `settings.JEV_DEFAULTS` is `{}` in a release (`settings.py:347`). KEEP guides name Jev only in lists (`authoring-an-agent.md:556`, `publishing-evaluations.md:125`, `evaluate-at-your-site.md:76`) |
| `naive_bayes.py` | 342 | `patient-inbox-router`, `referral-router` (`models/*-nb.json`); `training`, `improve`, `probe_cases` |
| `cql.py` | 339 | `clinical-trial-prescreener` only (`logic/criteria.cql`, CI job `cql`) |
| `criteria.py` | 548 | Trial, prior-auth and referral packs, plus the prescreen and referral minis. `core/pack.py:517` imports it for its 0.3 key check |
| `dosing.py` | 326 | `ai-guard`, `guard-mini` |
| `any_of.py` | 70 | All five packs (safety screens) |
| `cascade.py` | 87 | Inbox and referral |
| `vote.py` | 99 | No manifest. Tests only |

### 1.4 `evals`

**KEEP:** `__init__`, `canonical`, `compare`, `confidence`, `corrections`, `coverage`
(`mendari data coverage`, `choosing-a-threshold.md:162`), `dataset_checks`, `feedback`,
`figures`, `gate`, `harness`, `leaderboard` (`authoring-an-agent.md:431`), `ledger`, `metrics`,
`monitoring`, `operating_point`, `pooling`, all seven `probe*`, `ranking`, `rater_agreement`
(written output's weighted kappa), `render` (scoreboard parts out), `result` (`FORMATS_READ`
out), `share` (site-report/1 out), `stats`, `store_jobs`, `validation`, `written`.
`pack_tests.py` (245) is renamed `behaviour_tests.py`.

**REMOVE:**

| Module | Lines | Evidence |
|---|---|---|
| `agreement.py` | 74 | G3. `mendari agreement` exists for the CQL-against-criteria CI job (`ci.yml:506-518`) |
| `training.py`, `improve.py` | 165, 690 | G3. They work only on `naive_bayes`. Move `improve.not_writable` (used by `probe_tests.py:88`) into `probe_tests.py` |
| `arms`, `bands`, `grade`, `hidden`, `method`, `results_store`, `runs`, `scoreboard`, `signing` | 216, 566, 167, 1,144, 146, 1,707, 717, 1,193, 395 | G13. The scoreboard of the five packs (owner decision of 2026-11-21: "No scoreboard for the demo agent"). Hidden-set grading and the grading method. No KEEP guide names them |

### 1.5 `standards`

**KEEP:** `__init__`, `audit_fhir`(592), `bulk`(367, G5), `capability`(370, DTR operation
out), `cds_hooks`(909, CRD/DTR, linked reads and order hooks out), `data_coverage`(520, G5,
0.3 parts out), `decide_op`(459), `fhir`(1,667, 0.3 record extensions out with G6),
`fhir_client`(416), `fhir_r4_structure.json`, `fhir_search`(303), `fhir_structure`(336),
`oidc`(129), `search`(718), `smart`(321), `task_fhir`(184), `terminology`(567, G7),
`us_core`(364, G5: `decision.py:969` checks every FHIR input), `us_core_6_1_0.json`(1,302).

**REMOVE:**

| Module | Lines | Evidence |
|---|---|---|
| `davinci.py` | 507 | G4. CRD and DTR exist for `prior-auth-preflight`'s `coverage_information` and `questionnaire`. One function is generic and moves: `question_system(agent_id, question)` (`task_fhir.py:166`, `decide_op.py:392`, `terminology.py:455`), with `PACKS` renamed (4.9) |
| `pas.py` | 487 | G4. Prior-auth only: `/api/agents/{ns}/{name}/pas`, `mendari pas`, `sandbox/payer.py`, `pas.submitted` |
| `omop.py`, `record_fhir.py` | 453, 162 | G4. `mendari omop`, `sandbox omop-*`, and `cql`'s records; the trial pack only |
| `linked.py`, `ehr_declarations.py` | 429, 432 | G6. Built for spec 0.3 `follow` and `attachment_text` and for multi-source lists (the trial pack's medications). `connecting-an-ehr.md:22` says these apply "to an agent of your own that declares them": no KEEP agent does |

### 1.6 `server`

All KEEP except `paths.py` (94, REMOVE, compatibility). Edits by module:

- `app.py` (5,410): about −700 lines. Routes go: `/guard`, `/pas`, `$questionnaire-package`, the
  evalset, labelling and grading routes, the hidden-set run route. Also out: `JEV_DEFAULTS`,
  `current_pack_id`, `paths.route` (13 uses), site codes, EHR declarations.
- `api_docs.py` (615): `TITLED` goes once `SharedPack` is renamed.
- `auth.py` (2,264): `paths.current`, and sessions from before the issuer or claims were kept.
- `scopes.py`, `limits.py`, `presets.py`: `paths.current` and `paths.is_former` go.
- `registry_routes.py`, `store_routes.py`: `paths.route` goes; null-card-from-before wording
  goes.
- `hub_routes.py`: "a registry from before evaluations" goes.
- `smart_routes.py`: linked reads, site codes and declarations go; `?pack=` becomes `?agent=`.
- `sessions.py`: rows without `absolute_at` go.
- `system.py`: checks for removed settings go.
- `vercel.py`: docstring.

Renamed only: `backend_services`, `cds_clients`, `connection_routes`, `cors`, `landing`,
`origin`, `readiness`, `recalls`, `security_log`, `smart_check`, `snippets`, `system_routes`.

### 1.7 `hub` (registry and client)

All KEEP (G15 keeps `sites.py`, `site_reports.py`, `site_evidence.py`, `site_signing.py`).

| Module | Lines | Edits |
|---|---|---|
| `archive.py` | 405 | `.ohpack` out; the format is renamed to `mendari-agent/1` |
| `client.py` | 961 | The `packs/` cache folder out (`caches()`) |
| `evaluations.py` | 933 | `publisher-evaluation/1` out |
| `registry.py` | 2,894 | `hub_tokens`, `hub_reports`, `registry.chain_started`, the old-card branch, read-only older-table branches, `_index_chain_links` and `paths.route` (15 uses) go; event kinds renamed |

`acceptance`, `badge`, `checkpoint`, `evaluation_page`, `registry_keys.json`, `regression`,
`site_*`, `sites` and `yank` are renamed only.

### 1.8 `sandbox` (G8)

**KEEP:** `__init__`(24), `bulk_check`(99), `cds_client`(416; the EHR-profile parts are in tests),
`code_systems`(98, trimmed to `SANDBOX_TAG`, `CASE_SYSTEM`, `CHANNEL_SYSTEM`), `demo`(333),
`fhir_records`(85: portal messages), `fhir_workflow`(229), `pilot_check`(271), `smart_launch`(238).

**REMOVE:** `payer.py` (142, PAS), `omop_check.py` (48).

### 1.9 `community` (G10): REMOVE

`__init__`(4), `check`(1,633), `gitbase`(119), `maintainers`(98), `phi_scan`(50): 1,904 lines.
Only `cli.py` (`community-check`) and `scripts/community_check.py` import them.
`community-agents/MAINTAINERS.yaml` lists `agents: []`. The contributing guide is not on the
lead's KEEP list.

### 1.10 `templates`

| Template | Files / lines | Class |
|---|---|---|
| `base` | 4 / 65 | KEEP (G11): `mendari new` lays it under every new agent (`cli.py:4277`, `4342`). Merge with `agent` |
| `agent` | 2 / 246 | KEEP. `spec_version: "0.2"` becomes `"0.4"` (G12); `agent_id:` becomes `service_id:` |
| `ci` | 1 / 42 | KEEP |
| `community` | 3 / 136 | REMOVE (G10) |

### 1.11 `dashboard` (console)

49 files, KEEP except:

- `evalsets.js` (608): REMOVE (G13).
- `labelling.js` (231): keep the exported `labellingScreen`, which `written.js:19` imports;
  remove the hidden-set view.
- `try.js` (580): guard mode and the PAS panel out.
- `store.js` (497): hidden-match holds out.
- `leaderboard.js`: link to the scoreboard runbook out.
- `main.js`: `?pack=` out.
- `pack.js` (350): renamed `manifest.js`.

About 440 lines hold the word: `.pack_id` (42), `.pack` (19), `.packs` (14), `.pack_digest` (8),
`pack_version` (8), `pack_author` (2), CSS `pack-*`.

### 1.12 Former packs and test fixtures (`tests/fixtures`)

| Path | Files / lines | Class |
|---|---|---|
| `reference-agents/{ai-guard, clinical-trial-prescreener, patient-inbox-router, prior-auth-preflight, referral-router}` | 85 / 40,756 | REMOVE (the former packs) |
| `scoreboard/` | 73 / 14,201 | REMOVE (G13) |
| `datasets/` (four `*-local-site-example.jsonl`, `local-site-example.jsonl`, `drafts/patient-inbox-harder/*`) | 9 / 3,447 | REMOVE |
| `us-core/{inbox,prior-auth,referral,trial}.json`, `invalid/*` | 6 / 1,143 | REMOVE; regenerated for the new fixtures (`wave6-fixtures.md`) |
| `mini-agents/guard-mini` | 5 / 100 | REMOVE (G12) |
| `mini-agents/prescreen-mini` | 7 / 195 | REMOVE (G1, G6) |
| `mini-agents/referral-mini` | 6 / 139 | REMOVE: it keeps `pack.yaml` on purpose (compatibility) |
| `mini-agents/trial-mini` | 5 / 125 | REMOVE: its score rule moves to F2 |
| `mini-agents/actions-mini`, `written-mini` | 11 / 514 | KEEP, renamed keys |
| `community-agents/visit-sorter` | 4 / 68 | REMOVE (G10) |
| `audit/before-actions.sqlite.gz`, `main-1c8c87d2.sqlite.gz`, `make_before_actions.py` | 3 / 165 | REMOVE (pre-release databases) |
| `eval-v1-inbox.json` | 629 | REMOVE (`mendari.eval/1`) |
| `fhir/decisions-golden.json`, `fhir/task-versions.json` | 1,433 | REWRITE (regenerated on F1, F3 and the demo agent) |
| `fhir/audit-outcome.json`, `fhir/r4-search-parameters.json` | 1,023 | KEEP |
| `inferno/*_session.json` | 2 / 1,513 | KEEP (G9: smart-client, bulk-client) |
| `evaluation_pages.json`, `shown.json` | 23,211 | KEEP (regenerated) |

### 1.13 `tests/support` and test helpers

| File | Lines | Class |
|---|---|---|
| `synthetic_guard`, `synthetic_inbox`, `synthetic_inbox_harder`, `synthetic_prior_auth`, `synthetic_referral`, `synthetic_trial`, `harder_inbox_draft`, `fixture_data` | 3,904 | REMOVE |
| `ehr_profile.py`, `sandbox.py` | 711 | REMOVE (G6, and the fixtures half of `sandbox-e2e`) |
| `fhir_records.py` | 337 | REWRITE → `fixture_records.py` |
| `fhir_decisions_golden.py` | 145 | REWRITE |
| `confidence_agents.py`, `evaluation_pages.py`, `__init__.py` | 644 | KEEP |
| `tests/conftest.py` | 888 | REWRITE |

`tests/conftest.py` loses `REFERENCE_PACKS`, `INBOX_DIR`, `inbox_pack`, `router`, `pack_copy`,
`calm_jev`, `screened`, `CALM`, `SCREENED`, the `FORMER_ROUTES` tables, `as_now`, the
`OPENHEALTH_` loop and `MENDARI_COMMUNITY_PACKS`. It gains the F1, F2 and F3 helpers
(`wave6-fixtures.md`). Its `CORE` set loses 10 files.

`tests/jwt_signers.py` and `tests/probe_agents.py` are KEEP (`probe_agents` goes from spec
`"0.2"` to `"0.4"`).

### 1.14 Tests, file by file

**DELETE with a removed feature: 66 files, 887 functions.**

| Reason | Files (functions) |
|---|---|
| Former packs | `test_ai_guard_pack`(10), `test_inbox_pack`(5), `test_prior_auth_pack`(13), `test_referral_pack`(16), `test_trial_pack`(14), `test_lookaround_rewrites`(5), `test_synthetic_inbox_harder`(15) |
| Compatibility | `test_former_packs`(4), `test_legacy`(6), `test_legacy_routes`(24), `test_manifest_names`(21), `test_renamed_settings`(9) |
| G1, G2, G3 | `test_any_of`(7), `test_character_features`(14), `test_composites`(11), `test_cql`(14), `test_criteria`(16), `test_dosing`(5), `test_evals_agreement`(4), `test_improve`(28), `test_jev_batching`(14; its 4 generic grouping tests move to `test_llm`) |
| G4 | `test_davinci`(18), `test_omop`(8), `test_pas`(26), `test_x12_values`(3), `test_inferno_inputs`(7) |
| G6 | `test_ehr_pilot_e2e`(1), `test_ehr_profile`(6), `test_ehr_scorecard`(9; it grades `notes/ehr-scorecard.md`), `test_epic_data_fit`(27), `test_fhir_linked`(18), `test_fhir_mapping_03`(16), `test_site_codes`(12), `test_order_sign`(4) |
| G10 | `test_community_check`(71), `test_community_packs`(42), `test_synthetic_cases`(4), `browser/test_community`(9) |
| G12 | `test_spec_03`(5), `test_guard`(6) |
| G13 | `test_arms`(11); `test_evalset_{commitments, end_to_end, grading, keys, labelling, leaks, lifecycle, rotation, routes, scoreboard, store}` (199); `test_grading_key`(5); `test_grading_method`(9); `test_labelling_routes`(13); `test_no_hidden_data_shipped`(2); `test_scoreboard{, _end_to_end, _page, _rank, _runs, _signing, _store}`(96); `test_site_separation`(2); `browser/test_evalsets`(14); `browser/test_labelling`(9) |

**REWRITE onto the new fixtures: 164 files, 2,699 functions.** About 200 functions inside these
files test removed features or compatibility and are deleted in place.

- **F1 triage (153 files):**
  - Engine: `test_{access_cli, accounts_store, action_audit, action_routes, administrative_audit, agent_calibration, agent_connections, alerts, archive, audit, audit_fhir, audit_hardening, audit_integrity, auth, backend_services, break_glass, capability, cds_auth, cds_cards, cds_clients, cds_feedback, cds_hooks, cds_open_mode, chain_orgs, checkpoints, cli, cli_errors, clock, code_systems, community_budget, community_caps, community_re2, coverage, dataset_checks, decide_op, decision, decision_deadline, deploy, ehr_baseline, evals, feedback, fhir, fhir_client, fhir_decisions, fhir_errors, fhir_media, fhir_routes, fhir_search, figures, gate, hold, hosted_data, hub_client, hub_routes, input_schema, key_presets, landing, leaderboard, ledger, limits, llm, manifest_hostile, memberships, monitor_cron, monitoring, notes, notice, origin, outcomes, pack_content, pack_isolation, pack_status, pack_store, patterns, phi, phi_detect, policy, provider_configs, provider_egress, providers, public_surface, ranking, rater_agreement, read_only_commands, readiness, record_hygiene}`.
  - Registry, server and the rest: `test_{registry, registry_acceptance, registry_chain, registry_checkpoint, registry_claims, registry_engine, registry_limits, registry_origin, registry_regression, registry_tokens, registry_yank, restore, reviewed_cases, rotation, safety_invariant, second_signal, security_log, server, sessions, settings, share, signed_checkpoint, site_cases, site_evidence, site_reports, site_tickets, smart, smart_sealing, spec, spec_02, status_checks, store_improvement, store_jobs, store_promotion, store_uploads, task_fhir, templates, tenancy, terminology, text, two_organizations, us_core, usage_ledger, validate_datasets, validation, validation_brief, written_store}`.
  - Browser: `browser/test_{access, accessibility, actions, adoption, audit_activity, connection, credentials, dashboard, docs, hold, invitations, launch, notice, operators_views, origin, pack_store, plain_result, registry_approvals, registry_sites, try_modes}`.
- **F2 clinical-review (2 files):** `test_governance`(7), `test_pilot_check`(5).
- **F3 chart (5 files):** `test_bulk`(26), `test_cds_unreadable`(22), `test_data_coverage`(12),
  `test_fhir_lists`(9), `test_fhir_structure`(28).
- **Demo agent (3 files):** `test_sandbox`(12), `test_smart_launch`(13),
  `test_cds_client_sandbox`(9).
- **Inferno inputs (1 file):** `test_inferno_scripted_inputs`(9): CRD out; SMART and Bulk stay.

**VOCABULARY, rewritten as one rule: 4 files, 39 functions.** `test_vocabulary`,
`test_cli_vocabulary`, `test_vocabulary_docs`, `test_vocabulary_frontends`.

**GENERATOR: 1 file, 3 functions.** `test_fixture_data` → `test_fixture_agents`.

**KEEP, renamed only: 100 files, 1,247 functions.**

- `test_{action_decisions, actions, agent_kit, agent_outputs, agent_provider, agent_starters, api_keys, brief_gates, canonical, caps_together, cds_cors, ci_action, ci_scaffold, claude_agent_example, credentials, dashboard_contrast, dco, doc_blocks, doc_snippets, eval_gate, evaluation_fixture, evaluation_page, excluded_paths, fhir_packages, image_licences, inferno_check, inferno_session, inferno_setup, install, invite_page, isolation, judge, licence_inventory, mcp, neon_preview_cleanup, new_pack, notice_file, operating_point, operators, pack_store_prerequisites, packaging, packet, parallel, pooling, probe, probe_cli, probe_model, probe_store, probe_transforms, public_tree, publisher_evaluations, push_evaluate, registry_mount, registry_recalls, registry_review, registry_site_accounts, registry_sites, release_gate, scopes, search, secret_scan, security_headers, security_txt, session_store, shipped, site_adoption, site_e2e, site_install, site_notice, smart_check, spdx, spec_04, spec_04_written, stats, supply_chain, system_checks, system_routes, threat_model, usage_api, vendor, vercel, web_legal, weighted_kappa, workflow_safety, written_demo, written_leaks, written_report, written_routes}`.
- `browser/test_{agents, cds_cors, landing, operating_point, pack_status, packet, registry_accounts, site, status, system, usage, written}`.
- Some KEEP files still need small edits: `test_shipped` imports `create_payer_app` and
  `ENGINE_FORMER_ROUTES`; `test_isolation` lists `JEV_*`; `test_api_keys` tests legacy expiry.

**Browser subtotal (35 files, 377 functions):** 20 files / 284 functions rewritten, 12 / 61
kept, 3 / 32 deleted.

### 1.15 Scripts (50 files)

**KEEP:** `audit_fhir_sample`, `build_notice`, `check_{dco, doc_snippets, excluded,
image_licences, licences, release, sdist, site_notice, snomed}`, `demo_agent_data`,
`doc_blocks`, `fhir_output_sample` (fixture flags → F1, F3), `fhir_packages{.py,.lock}` (Da
Vinci, CDex, HRex, CARIN and PAS lines out with G4), `fhir_r4_schema`, `fhir_terminology`,
`fhir_validate`, `hub_seed`, `image_{licence_texts, notices, sources}`, `inferno_check`,
`inferno_scripted_inputs` (CRD out), `inferno_session`, `make-favicon`, `mkdocs_hooks`,
`neon_preview_cleanup`, `plant_fake_secret.sh`, `provider_config_schema`,
`r4_search_parameters`, `run_quickstart`, `sample_agent`, `sandbox_fhir_sample`,
`secret_scan{,_control}.sh`, `shown_fixture`, `status_checks`, `task_fhir_sample`,
`us_core_subset`, `vendor_docs_ui`, `zap_check`.

**RENAME:** `changed_packs.py` → `changed_agents.py`, `pack_gate.py` → `agent_gate.py`.

**REMOVE:**

| Script | Lines | Why |
|---|---|---|
| `community_check.py` | 191 | G10 |
| `davinci_sample.py` | 373 | G4 |
| `inferno_dtr_inputs.py` | 241 | G4 |
| `scoreboard_page.py` | 456 | G13 |
| `synthetic_cases.py` | 111 | G10/G16: its only reader is `contributing-an-agent.md` |

### 1.16 CI jobs and workflows (G9)

**`ci.yml`:**

| Job | Required | Class | Change |
|---|---|---|---|
| `test` (3.11, 3.12, 3.13) | yes | KEEP | Fixture step → `tests/fixtures/agents/*`; community step out (`ci.yml:81-95`) |
| `browser` | no | KEEP | |
| `demo-agent` | yes | KEEP | |
| `r4-structure` | no | KEEP | FHIR core |
| `fhir-conformance` | no | REWRITE | `--fixture` flags → F1, F3; Da Vinci steps out (`ci.yml:221-246`) |
| `sandbox-e2e` | no | KEEP, demo half only | Remove `ci.yml:317-361` and `sandbox/fixtures.compose.yml` |
| `hub-site`, `hub-e2e` | no | KEEP | |
| `cql` | no | REMOVE | `ci.yml:470-520` |
| `security` | no | KEEP | |
| `secrets`, `dco` | yes | KEEP | |
| `docs`, `quickstart`, `action`, `quickstart-wheel` | no | KEEP | |
| `pilot-check` | no | REWRITE | `--approve tests/fixtures/agents/clinical-review`; `MENDARI_AGENTS_PATH` → `tests/fixtures/agents` |
| `docker` | yes | KEEP | |
| `image-sbom` | no | KEEP | |

**Other workflows:**

- `agents.yml`, job `agent-check` (required): KEEP. Script renames; `pack.yaml` tests out
  (lines 61, 86); community steps out (lines 77-104).
- `terminology.yml`: REWRITE, the same changes as `fhir-conformance`.
- `conformance.yml`: keep `smart-client` and `bulk-client`; REMOVE `dtr-payer` and
  `crd-server`; `MENDARI_AGENTS_PATH` becomes `tests/fixtures/agents`.
- `docs.yml`, `neon-preview-cleanup.yml`, `release.yml`, `secrets.yml`: KEEP.
- `.github/PULL_REQUEST_TEMPLATE/agent.md`, `ISSUE_TEMPLATE/propose-agent.yml`: REMOVE (G10).
- `CODEOWNERS`: one comment line.

**`sandbox/inferno`:**

- REMOVE: the DTR kit (`Dockerfile`, `Gemfile.lock`, `docker-compose.yml`, `list_tests.rb`,
  `nginx.conf`, `offline_terminology.rb`, `dtr_payer_server_v220_expected.json`) and
  `crd_server_v221_expected.json`.
- KEEP: `scripted/*`, `smart_client_stu2_2_expected.json`, `bulk_data_v200_client_expected.json`,
  and `README.md` (trimmed).

### 1.17 Docs pages and READMEs (63 pages in `docs/`)

**KEEP, edited:**

- `index.md`, `architecture.md`, `roadmap.md`, `deploying-with-phi.md`.
- Guides: `authoring-an-agent`, `finding-failures`, `calibration-and-a-second-signal`,
  `choosing-a-threshold`, `governing-actions`, `written-output`, `evaluate-in-ci`,
  `publishing-evaluations`, `api` (guard, PAS, CRD and site-code paragraphs out),
  `install-at-your-site`, `adopting-an-agent` (hidden-set check out), `connecting-an-ehr`
  (Linked data, Site codes, the EHR's declarations, CRD and order hooks out),
  `evaluate-at-your-site`, `committee-packet`.
- Operations: `automation-hold`, `revoking-access`, `accounts`, `status-checks`, `registry`,
  `agent-store` (holds on hidden matches out), `incident-runbook` (adds "Starting over").
- `spec/evaluation-reports.md` (scoreboard, arms, run, prices, declared, canary, results store
  and hidden sections out; formats at `/1`); `spec/provider-configs.schema.json` regenerated.
- `security/threat-model.md` (about 70 of 117 rows name a removed feature: deleted or
  rewritten), `security/review-packet.md`.
- The ADRs: the word edited out; status notes per decision O4.
- Root files: `README.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `SECURITY.md`, `GOVERNANCE.md`,
  `CHANGELOG.md` (decision O5), `hub/README.md`, `web/README.md`, `deploy/README.md`.
- `examples/*/README.md`: contract keys.

**REMOVE:**

| Page | Lines |
|---|---|
| `guides/contributing-an-agent.md` | 336 |
| `operations/evalsets.md` | 843 |
| `operations/scoreboard.md` | 325 |
| `grading/method-1.0.md`, `grading/verifying.md` | 618 |
| `community-agents/README.md`, `MAINTAINERS.yaml` | 75 |

**CONSOLIDATE:** `spec/agent-manifest-0.1.md` through `0.4.md` (2,355 lines) become
`spec/agent-manifest.md` (about 1,300 lines). `mkdocs.yml` nav is updated to match.

### 1.18 Front ends, examples, agents, packaging and other files

- **Hub (`hub/`, 44 files):** KEEP.
  - REMOVE `hub/app/packs/[namespace]/[name]/route.ts` (the redirect) and its test cases
    (`hub/tests/smoke.spec.ts:151-160`, `headers.spec.ts:32-33`).
  - JSON reads renamed: `pack_name`, `pack`, `packs`.
  - Types renamed: `PackSummary`, `PackVersion`, `PackDetail`.
  - CSS classes renamed: `pack-hero`, `pack-id-pill`, `publisher-packs`.
- **`web/`:** KEEP, no change.
- **`agents/portal-message-agent`:** KEEP. `agent_id:` → `service_id:`; the mock's
  `agent_version` → `service_version`. Its digest changes; its version stays 0.3.0 (decision O9).
- **`examples/{http,langgraph,claude}-agent`:** KEEP. The kit copies are regenerated and the
  READMEs updated. Every deployed instance must be redeployed.
- **Packaging and images:** `pyproject.toml` drops `[project.optional-dependencies] server = []`.
  `Dockerfile`, `docker-compose.yml`, `deploy/*`, `action.yml`, `vercel.json` and `app.py` stay
  as they are, apart from one comment.

### 1.19 Branches and refs that hold the former packs (checked 2026-11-24)

- **On GitHub:**
  - `archive/decision-packs` **does not exist**.
  - Two branches hold a full `packs/` folder with the five packs: `codex/platform-audit`
    (`493a2592`, 2026-11-18) and `claude/repo-links-mendari-hq` (`fb726f7c`, 2026-10-08).
  - Pull-request refs (`refs/pull/*/head`) and `main`'s history also hold them. Users cannot
    delete pull-request refs.
- **Local:**
  - Nine branches hold `packs/`: `feat/full-audit-hardening-modernization` and `t3/*` ×8.
  - Eight of the `t3/*` branches are checked out as worktrees under `~/.t3/worktrees/mendari`.
  - `feat/agents-only-remove-packs` holds `community-packs/`.

## 2. Compatibility helpers to delete

| File | What goes |
|---|---|
| `src/mendari/legacy.py` (whole) | `OPENHEALTH_` adoption, `current_pack_id` (`openhealth/` → `mendari/`), `RENAMED_SETTINGS`, `setting()` for `MENDARI_PACKS_PATH`, `MENDARI_PACKS`, `MENDARI_PACK_STORE`, `MENDARI_COMMUNITY_PACKS`. Users: `__init__.py:13-16`, `settings.py:12,299,308,451`, `core/registry.py:40`, `server/app.py:119` |
| `src/mendari/server/paths.py` (whole) | Former routes `/api/packs…`, `/api/store/packs…`, `/api/registry/review/packs…`, `/v1/packs…`, `/registry/v1/packs…`. Users: `hub/registry.py` (15), `server/app.py` (13), `registry_routes.py` (5), `store_routes.py` (4), `auth.py` (3), `limits.py` (3), `scopes.py` (3), `presets.py` (2) |
| `core/pack.py` | `LEGACY_MANIFEST = "pack.yaml"`, `MANIFEST_NAMES`, the two-name logic of `manifest_name`, `Pack.manifest_file`. Also in `reference.py`, `scripts/changed_packs.py`, `agents.yml:61,86`, `ci.yml:91`, `conftest.fixture_agents` |
| `hub/archive.py` | `FORMER_EXTENSION = ".ohpack"` |
| `hub/client.py`, `core/registry.py` | The `packs/` cache folder (`packs_cache`, `caches()` second entry) |
| `cli.py` | `_Parser.renamed`, `_parse_optional`/`_get_values` overrides, `--pack`, `--pack-dir`, `mendari pack` → `archive`, hidden `new --agent` |
| `dashboard/main.js:160-164` | `?pack=`; and `cds_hooks.py:174`'s note |
| `hub/app/packs/[namespace]/[name]/route.ts` | The Hub's `/packs/` redirect |
| `core/origin.py:71` | `"openhealth": "maintained"` (decision G16 on the reserved names in `hub/registry.py:359` and `core/pack_store.py:90`) |
| `evals/result.py:31` | `FORMATS_READ` (`mendari.eval/1`, `openhealth.eval/1`); `evals/render.py:91,103` branches |
| `hub/evaluations.py` | `FORMAT_V1` (`publisher-evaluation/1`); `cli.py:565` older-registry fallback |
| `evals/share.py` | `mendari.site-report/1` models; `hub/registry.py:2750` refusal message |
| `server/api_docs.py` | `TITLED` (hides `SharedPack`) |
| `pyproject.toml` | `server = []` extra ("so `mendari[server]` keeps working") |
| `registry_routes.py:480`, `hub/lib/registry.ts:119`, `hub_routes.py:344`, `dashboard/operatingpoint.js:5`, `evals/leaderboard.py:259,267` | "From before" fallbacks |
| `tests/conftest.py` | `FORMER_ROUTES`, `STORE_FORMER_ROUTES`, `REVIEW_FORMER_ROUTES`, `REGISTRY_FORMER_ROUTES`, `ENGINE_FORMER_ROUTES`, `as_now`, the `OPENHEALTH_` loop, `MENDARI_COMMUNITY_PACKS` |

Also go: the "older than 0.4" branches that one spec version makes moot (G12):
`providers/agent.py:175`, `core/decision.py:136,860`, `agent_kit.py:145,485`, and the
version gates in `spec.py:1184-1201, 1315, 1319, 1359, 1434`.

## 3. Pre-release migration and old-data code

### 3.1 `core/audit.py`

- **Schema and migrations.** `_ADDED_COLUMNS` (lines 989-1036): fold each column into
  `_TABLES`. `_migrate` (1271): keep the hook, empty its body. `_index_chain_links`: create
  `audit_events_prev_hash` in `_TABLES`. Delete `_expire_legacy_keys` with `LEGACY_KEY_DAYS`
  (keys always expire), `_split_observed_labels` and `_supersede_earlier_ratings`.
- **Read-only older databases.** Delete `_READ_TABLES`, `_read_only_check`'s
  `unmigrated`/`require_current`, and every "older database, read as it is" branch (about 30:
  lines 1157, 1211, 1451, 1484, 2228, 2344, 2382, 2398, 4396, 4505-4507, 4800, 4823, 5399,
  5591, 6181, 6202, 6248, 6365, 6391, 6448, 6492, 7628, …).
- **Organization binding.** Delete `ORG_BOUND`, `bind_org`, `binds_org`, `BOUND_WHEN_MARKED`
  and `_ALWAYS_BOUND`: every event names its row's organization.
- **PHI tokens.** Delete `PHI_TOKENS_V2`: tokens always name their key.
- **Free text in the chain.** Delete `LEGACY_TEXT`, `_legacy_fields`, `_legacy_text`, the legacy
  branches of `_show_note` and `still_in_chain` (also `dashboard/system.js:325`).
- **Old records.** Delete the fallbacks for evidence written before 2026-10-02 (2764, 2838),
  for decisions written before the `refused` flag existed (7088, `task_fhir.py:70`), for old
  `_resource_ids` (7408-7420), for NULL `provider_errors` in monitoring
  (`evals/monitoring.py:38,407`), and for older feedback records (`evals/feedback.py:97`).

### 3.2 Other files

| File | What goes |
|---|---|
| `hub/registry.py` | `hub_tokens` and `hub_reports` tables with their `ROWS` entries; `registry.chain_started` and the `hub_meta 'chain'` mark; `_index_chain_links` (index into `SCHEMA`); `_has_claims`; read-only checks for `hub_key_revocations` and `hub_publisher_tokens`; the `VERSION_FORMAT` check for un-rendered cards |
| `core/phi.py` | `LEGACY = "legacy"` (tokens sealed before key ids); `tag_tokens` always on |
| `server/sessions.py:15-23,184` | Rows without `absolute_at` |
| `server/auth.py:1179,1183` | Sessions from before the issuer and the claims were kept |
| `core/written.py:595` | The check for a database from before the rounds' tables |
| `core/evalsets.py` | `predating` (goes with G13) |
| `tests/fixtures/audit/*` | The pre-release databases, and the tests that open them (`test_action_audit`, `test_written_store`, and one test each in about 30 other files) |

### 3.3 The baseline and the refusal (new, Phase 1C)

1. **Marker.** In the same DDL transaction that creates the schema, write a marker:
   - audit store: `settings` row `schema` = `mendari-engine/1`;
   - registry: `hub_meta` row `schema` = `mendari-registry/1`.
2. **Check at every open, read-only included, inside the schema lock.**
   - The store's anchor table (`audit_events`, `hub_events`) exists without the marker: refuse
     before reading anything, with: "This database was made by a pre-release version of Mendari
     and cannot be read or migrated. Give the server an empty database (docs/operations/incident-runbook.md,
     'Starting over')."
   - The marker names a newer schema: refuse with "made by a newer Mendari".
3. **What stays.** `CREATE TABLE IF NOT EXISTS`, the advisory or IMMEDIATE lock, and the
   `migrate` hook stay for future releases.
4. **Shared database.** Engine and registry share one database in production. Each store checks
   only its own anchor and marker, and both share `rate_limits`.

## 4. The rename

The order matters for the codemod. Rename the service-side `agent_*` first (4.10), then the
`pack*` names.

### 4.1 Modules, packages and files

| Was | Is |
|---|---|
| `core/pack.py` | `core/agent.py` |
| `core/pack_store.py` | `core/agent_store.py` |
| `evals/pack_tests.py` | `evals/behaviour_tests.py` |
| `dashboard/pack.js` | `dashboard/manifest.js` |
| `scripts/changed_packs.py` | `scripts/changed_agents.py` |
| `scripts/pack_gate.py` | `scripts/agent_gate.py` |
| `tests/test_pack_{content,isolation,status,store,store_prerequisites}.py` | `tests/test_agent_*.py` |
| `tests/test_new_pack.py` | `tests/test_new_agent.py` |
| `tests/browser/test_pack_{store,status}.py` | `tests/browser/test_agent_*.py` |
| `tests/fixtures/reference-agents/` | `tests/fixtures/agents/` |

### 4.2 Python identifiers

`Pack` → `Agent`; `PackManifest` → `AgentManifest`; `load_pack` → `load_agent`;
`resolve_pack` → `resolve_agent`; `pack_digest()` → `agent_digest()`; `PackValidationError` →
`AgentValidationError`; `PackNotFoundError` → `AgentNotFoundError`; `PackStore` → `AgentStore`;
`SharedPack` → `SharedAgent`; `ReferencePack` → `ReferenceAgent`; `reference_packs` →
`reference_agents`; `is_pack_id`/`_PACK_ID` → `is_agent_id`/`_AGENT_ID`; `pack_files` →
`agent_files`; `_PackLoader` → `_ManifestLoader`; `packs_paths` (`create_app`) → `agents_paths`;
`pack_or_404` → `agent_or_404`; `Decision.pack` → `Decision.agent`;
`DecisionResult.pack_id`/`pack_version`/`pack_digest` → `agent_id`/`agent_version`/`agent_digest`;
`run_pack_tests` → `run_behaviour_tests`; CLI `args.pack`/`dest="pack"` → `args.agent`;
`pack_command` → `agent_command`; `default_pack` → `default_agent`.

Local variables `pack` → `agent`, but first rename any local `agent` that means a service or
provider to `service` or `provider`. `packet.py`, `card.py`, `providers/agent.py`,
`agent_calibration.py`, `connection_routes.py` and `hub_routes.py` have both in one scope.

Test helpers: `inbox_pack`/`router` → `triage_agent`/`triage`; `pack_copy` → `agent_copy`;
`REFERENCE_PACKS` → `FIXTURE_AGENTS`; `calm_jev`/`screened` → `calm_agent`.

### 4.3 Wire keys (API JSON, stored and signed records)

| Was | Is |
|---|---|
| `pack` | `agent`. It is an object `{id, version, digest}` in decisions, ledgers, reports and packets, and a string id in registry records (acceptance, site evidence, `report.shared`, tickets). The shapes stay |
| `pack_id`, `pack_version`, `pack_digest`, `pack_name`, `packs` | `agent_id`, `agent_version`, `agent_digest`, `agent_name`, `agents` |
| `packs_by_origin`, `pack_warnings` (health) | `agents_by_origin`, `agent_warnings` |
| `pack_unavailable` (error code) | `agent_unavailable` |
| Request bodies `pack_id`, `pack_digest` (governance, holds, registry approvals, evals, SMART focus) | `agent_id`, `agent_digest` |

### 4.4 Query parameters and operation parameters

| Was | Is |
|---|---|
| `?pack_id=` on `/api/review`, `/api/audit`, `/api/monitoring`, `/api/governance`, `/api/registry-approvals`, `/api/evals`, `/api/stats`, `/smart/focus`, the written rounds list | `?agent_id=` |
| `/smart/launch?pack=` | `?agent=` |
| The console's `?pack=` | deleted (`?agent=` only) |
| FHIR `$decide` in-parameter `pack` | `agent` (the OperationDefinition is re-pinned) |

### 4.5 Settings, flags, commands and routes

After section 2, no setting, flag, command or route holds the word. The current names
(`MENDARI_AGENTS_PATH`, `MENDARI_AGENTS`, `MENDARI_AGENT_STORE`, `--agent`, `mendari archive`,
`/api/agents`, `/v1/agents`) stay. `MENDARI_COMMUNITY_AGENTS` goes with G10.

### 4.6 Database columns and indexes

| Table | Columns, was → is |
|---|---|
| `decisions` | `pack_id`, `pack_version` → `agent_id`, `agent_version` |
| `eval_reports`, `feedback`, `store_versions`, `agent_connections`, `store_datasets`, `store_promotions` | `pack_id` → `agent_id` |
| `governance_approvals`, `registry_approvals`, `automation_holds`, `written_rounds` | `pack_id`, `pack_digest` → `agent_id`, `agent_digest` |
| `deployments` | `pack_id`, `pack_version`, `pack_digest` → `agent_id`, `agent_version`, `agent_digest` |

| Index, was | Is |
|---|---|
| `decisions_pack` | `decisions_agent` |
| `feedback_pack` | `feedback_agent` |
| `deployments_pack` | `deployments_agent` |
| `store_versions_pack` | `store_versions_agent` |

The column lists change in `store_promotions_org`, `governance_approvals_lookup`,
`registry_approvals_lookup` and `automation_holds_active`. `STORE_VERSION_COLUMNS` (which
`store_version_sha256` hashes) has `pack_id` → `agent_id`. The registry tables have no such
columns.

### 4.7 Event kinds, chain subjects, roles

| Was | Is |
|---|---|
| `pack_version.{created, held, released, withdrawn, promoted, retired}` | `store_version.*` (`held`/`released` go with G13) |
| Subject prefix `pack_version:` | `store_version:` |
| `registry_pack.{approved, revoked}` | `registry_agent.{approved, revoked}` |
| Registry chain `pack.{published, reviewed, yanked, deleted, accepted}` | `agent.*` |
| `_pack_subject` | `_version_subject` |
| Role `pack_author` | `agent_author` |

These names are codes of the CodeSystems `audit-event` and `audit-administrative-event`.

Removed kinds: `pas.submitted` (G4); `evalset.*` (13), `evalset_index.epoch`,
`evalset_index.rebuilt`, `evalset.rotated`, `store_dataset.held` (G13); `registry.chain_started`
(section 3).

### 4.8 FHIR and CDS identifiers

| Was | Is |
|---|---|
| `https://mendari.co/fhir/CodeSystem/decision-pack` (Task.meta.tag `id@version`) | `…/CodeSystem/agent-version` |
| `https://mendari.co/fhir/NamingSystem/decision-pack` (AuditEvent.agent, Provenance.agent) | `…/NamingSystem/agent-version` |
| `PACK_TAG_SYSTEM`, `PACK_ID_SYSTEM` | `AGENT_TAG_SYSTEM`, `AGENT_ID_SYSTEM` |
| `https://mendari.co/packs/{id}/CodeSystem/{question}` | `https://mendari.co/agents/{id}/CodeSystem/{question}` |
| `https://mendari.co/packs/{id}/{version}/output.schema.json` (`core/pack.py:277`) | `https://mendari.co/agents/…` |
| `https://mendari.co/packs/community/<name>/` | removed (G10) |
| `…/packs/{id}/Library/{engine}` (CQL), `…/packs/{id}/Questionnaire/…` (DTR), `https://mendari.co/fhir/pas-request`, `https://mendari.co/fhir/decision` (PAS) | removed |

- **Fictional CodeSystems:** `fictional-{procedures, conditions, referrals, site-diagnoses,
  site-order-types}` are removed (G7).
- **Unchanged:** `decision-outcome`, `audit-event`, `audit-administrative-event`,
  `card-override-reasons`, `NamingSystem/{decision-id, input-hash, audit-subject}`,
  `fhir/sandbox`, `fhir/synthetic-case`, `CodeSystem/message-channel`, `fhir/scope/{decide,
  outcome}`, `OperationDefinition/decide`. Some of these change content (codes and
  descriptions): the audit CodeSystems' codes, and `audit-subject`'s description (prefixes).
- **Extensions:** Mendari defines none of its own. The ones it writes are Da Vinci's (CRD
  `ext-coverage-information`, PAS), which go with G4.
- **Pinned resources:** `tests/test_terminology.py` `PINNED["0.1.0"]` is re-pinned (0.1.0 is
  unreleased; the test allows it).
- **CDS service ids:** `mendari.<name>` is unchanged.

### 4.9 The archive format

| Was | Is |
|---|---|
| `mendari-pack/1` | `mendari-agent/1` |
| `mendari-pack.json`, `mendari-pack.sig` | `mendari-agent.json`, `mendari-agent.sig` |
| The tar folder `pack/` | `agent/` |

The `.mendari` extension and the `.mendari-verified` marker are unchanged.

### 4.10 Collisions: the agent and its service

The agent HTTP contract already uses `agent_id` and `agent_version` for the external service.
The scheme: the **governed unit is the agent** (`agent_id`, `agent_version`, `agent_digest`,
`agent`); **the process that answers for it is its service** (`service_id`, `service_version`,
`service`).

| Where | Governed agent: was → is | Service: was → is |
|---|---|---|
| Contract request body | `pack_id` → `agent_id` | `agent_id` → `service_id` |
| Contract response | — | `agent_version` → `service_version` |
| Manifest, `type: agent` variant | — | `agent_id:` → `service_id:`; `AGENT_ID_PATTERN` → `SERVICE_ID_PATTERN` |
| `mock_file` (root and per response) | — | `agent_version` → `service_version` |
| Calibration file `mendari.agent-calibration/1` | — | `agent: {id, version}` → `service: {id, version}` |
| `mendari.calibration-report/1` (`evals/confidence.py:83`) | — | `agent` → `service` |
| `mendari.committee-packet/1` | top-level `agent` stays | calibration and second-signal entries `agent_id`, `agent_version` → `service_id`, `service_version` |
| Connection test answer (`connection_routes.py:417,470`) | `pack_id` → `agent_id` | `agent_version` → `service_version` (`dashboard/connection.js:116`) |
| Agent kit `Case` | `.agent` (read from `pack_id`) → `.agent_id` | `.agent_id` → `.service_id` |
| Agent kit `create_app`, `/health` | — | `agent_version=` → `service_version=` |
| `ProviderOutput.extra` | — | `agent_id`, `agent_version` → `service_id`, `service_version` |
| Provider `model` | — | `<agent_id>@<agent_version>` → `<service_id>@<service_version>` |
| Decision Card and packet text | — | "agent version" → "service version" |
| Registry route `name="agent_version"` | already governed | — |

The provider type `type: agent`, the setting names `MENDARI_AGENT_URL` and
`MENDARI_AGENT_TOKEN`, and the phrase "agent HTTP contract" stay (decision N2).

### 4.11 Front ends and command-line reads

- **Console:** reads `.pack_id`, `.pack`, `.packs`, `.pack_digest`, `pack_version`,
  `pack_author` → the new keys. Identifiers `packId`, `currentPack`, `packPath`, `packOptions`,
  `defaultPack`, `.packless` → `agentId`, `currentAgent`, `agentPath`, `agentOptions`,
  `defaultAgent`, `.agentless`. CSS `pack-*` → `agent-*`.
- **Hub:** reads `pack_name`, `pack` (`Acceptance`, `SiteEvidence`, version), `packs`
  (publisher page). Identifiers `PackSummary`, `PackVersion`, `PackDetail`, `pack()`,
  `packVersion()`, `PackCard`, `PackPage`, `packId` → `Agent*`. CSS `pack-hero`,
  `pack-hero-danger`, `pack-id-pill`, `publisher-packs`, `publisher-pack-link` → `agent-*`.
- **Command line:** `health['packs']` (`cli.py:3312`), `published['pack']`, `report['pack']`,
  `a.header["pack"]`, `found = report.get("pack")` (`cli.py:356, 1100, 3014, 3103, 3582`).
- **MCP:** `QUEUE_FIELDS` `pack_id`, `pack_version` → `agent_id`, `agent_version`.
  `result.pack_id` → `result.agent_id`.

### 4.12 Report and signed formats

The rule: **the first release defines every format at `/1`.** Nothing written before has to be
read.

| Format | Wave 6 |
|---|---|
| `mendari.eval/2`, `validation-brief/2`, `registry-version/2`, `leaderboard/2`, `site-report/2`, `publisher-evaluation/2` | Restart at `/1`, with `agent` keys |
| `mendari-pack/1` | `mendari-agent/1` |
| `ledger/1`, `agent-calibration/1`, `calibration-report/1`, `committee-packet/1`, `agent-evaluation/1`, `agreement/1`, `compare/1`, `corrections/1`, `coverage/1`, `dataset-check/1`, `figures/1`, `rank/1`, `registry-acceptance/1`, `site-evidence/1`, `site-submission/1`, `site-ticket-request/1`, `site-ticket-withdrawal/1`, `yank/1`, `publisher-evaluations/1`, `origin/1`, `system-checks/1`, `written/1`, `written-ledger/1`, `written-grades/1`, `llm-calibration/1` | Stay `/1`. Keys renamed where they hold the word or a service key |
| `checkpoint/1`, `registry-checkpoint/1`, `registry-keys/1`, `regression/1`, `operating-point/1`, `probe/1`, `probe-plan/1`, `phi-check/1`, `second-signal/1`, `site-key/1`, `data-coverage/1`, `mendari-image-inventory/1` | Unchanged |
| `eval/1`, `site-report/1`, `publisher-evaluation/1`, `naive_bayes/1`, `naive_bayes/2`, `improvement/1`, `arms/1`, `scoreboard/1`, `run/1`, `prices/1`, `disclosures/1`, `declared/1`, `canary/1`, `hidden_result/1`, `hidden_scoreboard/1`, `evalset/1`, `evalset_aggregates/1`, `evalset_export/1`, `grading-keys/1`, `site-codes/1`, `ehr-declarations/1` | Removed |

### 4.13 The words that may still hold the letters

`package` and every word that begins with it (`packages`, `packaged`, `packaging`,
`PackageBundle`, `questionnaire-package`, `packagedMedicinalProduct`); `packet`/`packets`;
`unpack`/`unpacked`/`unpacks` (with `un` starting the word or after `_`); `backpack`;
`struct.pack` (`scripts/make-favicon.py`).

Rename `repack` (`tests/test_archive.py`), "re-packed" (`scripts/fhir_r4_schema.py:7,76`) and the
local `packed` (`test_two_organizations.py:739`, `test_evalset_leaks.py:1233`).

A rule that implements this:

    re.sub(r"(?i)(?<![a-z0-9])(?:un|back)pack|struct\.pack\b", "", text)

then flag `(?i)pack(?!ag|et)`. `runPackId` is caught, because its `un` follows a letter.

## 5. Production wipe and refill

Kept outside the repository: it describes the project's own deployment.
