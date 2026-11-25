# Wave 6, phase 3.2: the five feature groups

Each engineer takes one group (S1 to S5). Read `phase32-common.md` first. The lists below say
where the lead found the group; they are a start, not the whole: search for every use
(`git grep -n -i`), in `src`, `tests`, `scripts`, `docs`, `mkdocs.yml`, `.github`, `hub/`,
`web/`, `deploy/`, `sandbox/`, `examples/`, `Dockerfile`, `docker-compose.yml`,
`pyproject.toml`, `NOTICE` and `licenses/`.

## S1. Hidden evaluation sets and the scoreboard (decision G13)

**Remove:**
- `core/evalsets.py`, `core/evalset_keys.py`;
- in `evals/`: `arms`, `bands`, `grade`, `hidden`, `method`, `results_store`, `runs`,
  `scoreboard`, `signing`. The inventory says these serve only the hidden sets and the
  scoreboard: **check each importer first**. A module that something kept uses (written output,
  the leaderboard, site results, evaluation pages, publisher evaluations, monitoring, store
  jobs, actions, the card, the packet) stays, emptied of what only the removed features used;
- the server's hidden-set, labelling-of-hidden-sets and grading routes, the hidden-set run
  route, and what `server/app.py`, `scopes.py`, `presets.py` and `auth.py` hold for them;
- the commands `mendari evalset ...`, `mendari scoreboard ...`, `mendari access grader-key`,
  and `cli._site_configuration`'s use of `evals/arms.py`;
- the settings `MENDARI_EVALSET_KEY`, `MENDARI_EVALSET_READ_KEYS`, `MENDARI_EVALSET_INDEX_KEY`,
  and the grading key's (`MENDARI_GRADING_SIGNING_KEY`) if only grading uses it;
- the evalset tables, their verification and their events in `core/audit.py`, the hidden-set
  holds in `core/pack_store.py` (`store_matches`), and what `hub/` (Python) holds for them;
- `errors.py`: `NoEvalset`, `GradingRefused` and whatever only hidden sets raise;
- the console: `dashboard/evalsets.js`, the hidden-set view of `labelling.js`, the
  hidden-match holds of `store.js`, the scoreboard runbook link of `leaderboard.js`, the menu
  entries and routes;
- docs: `docs/operations/evalsets.md`, `docs/operations/scoreboard.md`, `docs/grading/` as far
  as it serves hidden sets, the scoreboard parts of `docs/spec/evaluation-reports.md` and
  `docs/architecture.md`, every link to them, `mkdocs.yml`; `web/` mentions of the scoreboard;
- tests: `tests/conftest.py` `evalset_on`, `evalsets_are_on`, `EVALSET_*`, `_no_evalset_keys`
  and the evalset names in `LIVE_SETTINGS`; the hidden-set and grading parts of `test_auth`,
  `test_memberships`, `test_shipped`, `test_vocabulary`, `test_probe_store`, `test_key_presets`,
  `test_limits`, `test_chain_orgs`, `test_two_organizations` (their `REMOVED_*` tables list
  routes and presets wave 6 removes: once a route is gone, the table and its test go),
  `test_cli_errors`, `browser/*`.

**Keep:** the `labeller` role and blind grading of written output: move `NO_ASSIGNMENT`,
`labeller_in`, `labeller_membership` and `user_ids` (imported by `core/written.py`) out of
`core/evalsets.py` into a kept module first; keep `labellingScreen` in `dashboard/labelling.js`
(`written.js` imports it); keep `NoAssignment` and `LabelError` if written output raises them.
Written output's own grading (`evals/written.py`, `providers/judge.py`) stays whole.

## S2. The removed providers and the training commands (G1, G2, G3)

**Remove:**
- providers `jev`, `naive_bayes`, `cql`, `criteria`, `dosing`, `any_of`, `cascade`, `vote`
  (`src/mendari/providers/`), and every manifest key, setting, schema entry and message that
  only they use: `settings.JEV_DEFAULTS`, `MENDARI_JEV_DAILY_LIMIT`, `JEV_API_URL`,
  `JEV_API_KEY`, `JEV_MODEL`, `JEV_CONNECTOR`, `credentials.ConnectorToken` (Vercel connectors
  for Jev) if nothing else uses it, `load_pack(missing_models_ok=...)` (Naive Bayes models), the
  `criteria` 0.3 key check in `core/pack.py`, the Jev defaults the servers turn on;
- `mendari train`, `mendari improve`, `mendari agreement`; `evals/training.py`,
  `evals/improve.py`, `evals/agreement.py`. First move `improve.not_writable` (used by
  `evals/probe_tests.py`) into `probe_tests.py`. `mendari feedback export` stops printing its
  hint about `improve`;
- the provider configuration schema's entries for them (`scripts/provider_config_schema.py`
  regenerates the committed schema); its completeness check becomes an equality;
- `docs/security/threat-model.md`: the "Agent content" rows of removed providers
  (`tests/test_threat_model.py` pins those tables to the code);
- docs: provider lists and pages (`docs/guides/authoring-an-agent.md` and the other guides that
  list Jev, Naive Bayes, CQL, criteria or dosing), `mkdocs.yml`; `web/` and `hub/` mentions;
- tests: `tests/conftest.py` `_no_jev` (keep what it does for `MENDARI_AGENT_URL`,
  `MENDARI_AGENT_TOKEN` and `MENDARI_PROVIDERS` under a new fixture name) and the Jev names in
  `LIVE_SETTINGS`; `test_isolation`'s Jev settings; the removed providers' tests in
  `test_providers`, `test_llm`, `test_provider_configs`, `test_provider_egress`,
  `test_usage_ledger`, `test_settings`, `test_limits`, `test_cli`, `test_cli_errors`,
  `test_feedback`, `test_probe*`, `browser/*`.

**Keep:** `llm`, `agent`, `rules`, `fixture`, `judge`, `composite` (the base of `check`),
`batch`, `check`, `repeat`, and every other provider.

## S3. Da Vinci, PAS, X12, OMOP and the fictional code systems (G4, G7, G8)

**Remove:**
- `standards/davinci.py`, `pas.py`, `omop.py`, `record_fhir.py`. First move
  `davinci.question_system` (used by `task_fhir.py`, `decide_op.py`, `terminology.py`, every
  agent's Task and `$decide`) into `standards/terminology.py`;
- `sandbox/payer.py`, `sandbox/omop_check.py`; `mendari pas ...`, `mendari omop ...`,
  `mendari sandbox payer`, `sandbox omop-write`, `sandbox omop-check`;
- the routes `/api/agents/{ns}/{name}/pas` and `$questionnaire-package`, the CRD and DTR CDS
  services and the DTR operation in the CapabilityStatement (`standards/capability.py`,
  `cds_hooks.py`: only CRD and DTR; linked reads and order hooks stay, G6);
- the manifest's `coverage_information` and `questionnaire` (`core/spec.py`, their validation,
  their docs); the `MENDARI_PAS_*` settings; `pas.submitted` and `LEGACY_TEXT` in
  `core/audit.py`; the `unmigrated` and `connect(baseline=None)` remnants that served OMOP;
- the five fictional code systems in `standards/terminology.py` (G7: a test that needs a local
  code system declares it in its fixture, never in the product), and in `sandbox/code_systems.py`
  what only they used;
- FHIR packages: Da Vinci, CDex, HRex, CARIN and PAS lines out of `scripts/fhir_packages.lock`
  and `scripts/fhir_packages.py`; `sandbox/inferno` and `scripts/inferno_*` what only CRD or DTR
  used (the scripted SMART and Bulk client suites stay);
- `NOTICE` and `licenses/notice.toml`: the X12 lines (rebuild `NOTICE` with
  `scripts/build_notice.py`);
- docs: `docs/guides/connecting-an-ehr.md`'s CRD and DTR text, pages about prior
  authorization, PAS, OMOP; `mkdocs.yml`;
- tests: `tests/conftest.py`'s `MENDARI_PAS_PAYER_URL`; `test_isolation`'s; the removed
  features' tests in `test_capability`, `test_cds_hooks`, `test_fhir_packages`,
  `test_terminology`, `test_code_systems`, `test_task_fhir`, `test_decide_op`, `test_notes`,
  `test_key_presets`, `test_shipped` (`create_payer_app`), `test_sandbox`, `test_inferno_*`,
  `test_cli_errors`.

**Keep:** record reading (G6): `standards/linked.py`, `ehr_declarations.py`,
`core/site_codes.py`, `follow`, `attachment_text`, `code_sets`, `paths`, `[system=]`
predicates, multi-type lists, `medication_period`, order hooks (`order-sign`), their settings
and formats, and the manifest's `code_systems`. US Core, Bulk Data, SMART, CDS Hooks, `$decide`,
Task, AuditEvent, Provenance, `data_coverage`, terminology of Mendari's own code systems.

## S4. The community contribution path (G10, G16a)

**Remove:**
- `src/mendari/community/`; `mendari community-check`; `mendari new --community`;
  `src/mendari/templates/community/`;
- the `community` origin and `"openhealth"` in `core/origin.py` (keep `openhealth` reserved in
  the registry and the agent store); `MENDARI_COMMUNITY_AGENTS` and
  `settings.community_packs_enabled`; the community search path in `core/registry.py` and
  `core/reference.py`; the community URL;
- compatibility remnants in the same code: `origin.pulled_record`'s marker from before LP-18d,
  `hub/client.py` `_pins_of`'s fallbacks, and their tests
  (`test_registry_origin`'s pre-LP-18d marker test,
  `test_pins_filed_under_an_address_as_it_was_typed_are_still_read`);
- `community-agents/` (the folder); `docs/guides/contributing-an-agent.md` and every link to
  it, in docs, messages and front ends; `.github/PULL_REQUEST_TEMPLATE/agent.md`,
  `.github/ISSUE_TEMPLATE/propose-agent.yml`, the community line of `.github/CODEOWNERS`, the
  community steps of `.github/workflows/agents.yml`; `scripts/changed_packs.py`'s
  `community-agents/` mapping;
- the console's, the Hub's and mendari.co's community origin badges, filters and words, where
  they mean the contribution path;
- the threat model's rows and citations (D8 cites `test_community_*`);
- tests: `tests/conftest.py` `_no_community_packs`; the community path's tests wherever they
  are (`test_origin`, `test_registry_origin`, `test_cli`, `test_templates`, `test_new_pack`,
  `test_action_decisions`, `test_cds_hooks`, `test_hub_client`, `browser/*`).

**Keep and rename:** the bounded engine (ADR-0009): RE2 patterns, the caps, the bounded input
schema, its PHI scan if the engine uses it. Its `COMMUNITY_*` constants become `BOUNDED_*`,
and its tests (`test_community_re2`, `test_community_caps`, `test_community_budget`) become
`test_bounded_*`: update every reference to their node ids (`tests/test_isolation.py`,
`tests/test_vercel.py`, `tests/conftest.py` `CORE`, the threat model).

## S5. One spec version, and guard, derive and the policy file (G11, G12)

**Remove:**
- every spec version but `"0.4"`: `core/spec.py` accepts `spec_version: "0.4"` only and says
  so when refused; `spec.at_least` and the per-version checks (`spec 0.2 features`,
  `spec 0.3 card features`, `spec 0.3 mapping features` in `standards/fhir.py`,
  `providers/agent.py`) collapse into what 0.4 allows. Leave the ones inside other groups'
  modules (`providers/criteria.py`, `evals/improve.py`, `evals/runs.py`): they go with those;
- `guard.py`, the `/guard` route, the manifest's `guard`, the console's guard mode
  (`dashboard/try.js`) and its docs;
- `core/derive.py` and the manifest's `derive` (users: `core/decision.py`,
  `evals/rater_agreement.py` for hidden-set labelling: leave a use inside S1's code to S1);
- `policy:` as a separate file: the policy is the manifest's own section only;
- templates (G11): merge `templates/base` and `templates/agent` into one template that
  `mendari new` lays down, writing `spec_version: "0.4"`; `templates/ci` stays. (S4 deletes
  `templates/community`; leave it.) Keep `agent_id:` for the service: phase 4 renames it;
- tests: `tests/probe_agents.py` writes `"0.4"`; version-gating tests in `test_spec`,
  `test_spec_02`, `test_spec_04`, `test_templates`, `test_new_pack`, `test_server`, the guard's
  and derive's tests, `browser/test_try_modes`; docs pages and `docs/guides/` text about guard,
  derive, the policy file and spec versions (not the `docs/spec/agent-manifest-*.md` pages:
  phase 5 consolidates them into one).

**Keep:** everything 0.4 offers, actions, calibration, the second signal, written output,
`code_systems`, record reading (G6), `cds_hooks` cards.
