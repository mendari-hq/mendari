# Wave 6 build plan: packs gone

## Why the order differs from the suggested shape

The suggested shape renames first. The code argues for renaming last:

- **Wasted renames.** About 37% of today's test lines (66 files, about 33,000 lines) and
  19,300 lines of `src` (44 files) are deleted in this wave. Renaming them first is wasted work.
- **Artifacts that would need regenerating.** The former packs carry committed records with the
  old keys and formats that a rename-first order would have to regenerate before deleting:
  5 benchmarks (`mendari.eval/2`), 5 leaderboards, 5 ledgers, 73 scoreboard fixtures, FHIR
  goldens, `task-versions.json`.
- **The rename itself.** It is a codemod across every file. It is safest as one atomic change
  on the final, smaller tree, when no other phase is in flight.

What the suggested shape wanted early still happens early: compatibility helpers and pre-release
migrations go in Phase 1.

| Suggested | Here |
|---|---|
| A | Phase 1 (compatibility, storage baseline) and Phase 4 (rename) |
| B | Phase 1 |
| C | Phase 2 |
| D | Phase 3 |
| E | Phase 5 |
| F | Phase 6 |

If the lead prefers rename-first, Phase 4 moves to the front. It then also regenerates the
artifacts above.

## Ground rules

- **Worktrees and ownership.** One worktree per engineer, branched from the phase's start. A
  file has one owner per phase. Ownership is listed per phase below. A file not listed is
  touched by nobody in that phase.
- **When a module may be deleted.** Only by the engineer who owns every file that imports it,
  or in a phase's cleanup step. Until then a removed feature stays as dead code.
- **When a phase is done.** All worktrees merge to the integration branch, and these pass:
  - `uv run pytest -n 6` (SQLite);
  - the same with `MENDARI_TEST_DATABASE_URL` set (Postgres, Mendari's own container);
  - `uv run pytest -m core`;
  - `uv run pytest -m browser`;
  - `uv run ruff check src tests scripts && uv run ruff format --check src tests scripts`;
  - `uv run mypy src tests`;
  - the generator's `--check`.

  In phases that touch the Hub, also run `npm run lint && npm run typecheck && npm run build && npm test`
  in `hub/` against `scripts/hub_seed.py --hostile-example --sites --evaluations`.
- **Shared inventories.** `tests/test_threat_model.py` fails when the threat model cites a
  deleted test. The other inventory tests (`test_shipped.py`, `test_doc_blocks.py`,
  `test_workflow_safety.py`, `test_supply_chain.py`, `test_deploy.py`, `test_excluded_paths.py`)
  are edited by whoever owns them in the phase that changes what they inventory.
- **`-m core`.** CI's `test (3.11)` and `test (3.13)` run `-m core`, and both are required.
  Whoever deletes a file in `conftest.CORE` adds its replacement to `CORE` in the same phase.

## Phase 0. Decisions (lead, about a day)

Answer `wave6-decisions-needed.md`. Confirm the production facts in `wave6-inventory.md`
section 5.3.

**Size:** none. **Check:** every G, N and O item has an answer.

## Phase 1. New fixtures, storage baseline, compatibility out (5 engineers in parallel)

### 1A. Fixtures F1 and F2, the generator, the conftest helpers

- **Owns:**
  - new: `tests/fixtures/reference-agents/{triage, clinical-review}/**`,
    `tests/support/fixture_agents.py`, `tests/test_fixture_agents.py`, `tests/test_triage_fixture.py`
    (a core test of F1's decisions, replacing the five pack tests in `CORE`);
  - edits: `tests/conftest.py` (add helpers only; delete nothing).
- **Must not touch:** existing tests, `src`.
- **Checks:** `python -m tests.support.fixture_agents --check`; validate, test and eval on F1 and
  F2; the full suite.
- **Size:** about 25 files, about 2,500 lines added (about 1,100 generated).

### 1B. Fixture F3 and the FHIR records

- **Owns (all new):** `tests/fixtures/reference-agents/chart-summary/**`,
  `tests/support/fixture_records.py`, `tests/support/fixture_golden.py`,
  `tests/fixtures/us-core/{chart,triage}.json`, `tests/fixtures/us-core/invalid/chart.json`.
- **Helpers:** exposed from its own module, never from `conftest.py`.
- **Checks:** as 1A; `scripts/fhir_validate.py --offline --valid …` locally if the validator is
  cached.
- **Size:** about 15 files, about 2,000 lines added.

### 1C. Storage baseline and the pre-release refusal

- **Owns:** `src/mendari/core/db.py`, `core/audit.py`, `hub/registry.py` (including its
  `paths.route` calls, which become plain decorators), `core/phi.py`, `core/session_seal.py`,
  `server/sessions.py`, `core/written.py`, `tests/fixtures/audit/**`, new
  `tests/test_schema_baseline.py`, and the migration tests inside `test_action_audit`,
  `test_written_store`, `test_api_keys`, `test_chain_orgs`, `test_read_only_commands`,
  `test_registry_chain`, `test_registry_tokens`, `test_session_store`, `test_sessions`.
- **Does:** `wave6-inventory.md` section 3. The new tests cover SQLite and Postgres, write and
  read-only modes, a shared database, and a newer marker.
- **Size:** about 18 files, about −2,500 and +500 lines.

### 1D. Compatibility names out

- **Owns:**
  - `src/mendari/legacy.py` (delete), `__init__.py`, `settings.py`, `core/registry.py`,
    `core/reference.py`, `core/pack.py` (only `manifest_name` and the two names),
    `core/origin.py` (only `openhealth`), `hub/archive.py`, `hub/client.py`, `cli.py` (only
    the rename parser, `--pack`, the hidden flags), `server/paths.py` (neuter it: register the
    current path only; deleted in 3.3);
  - every other `server/*.py` that calls `paths`, `server/api_docs.py` (`TITLED`),
    `evals/result.py`, `evals/render.py` (eval/1 branches), `hub/evaluations.py` (V1),
    `evals/share.py` (site-report/1), `dashboard/main.js`, `hub/app/packs/**` (delete),
    `hub/tests/{smoke,headers}.spec.ts`, `pyproject.toml` (`server` extra), `uv.lock`;
  - `scripts/changed_packs.py`, `.github/workflows/{agents,ci}.yml` (`pack.yaml` lines only);
  - deletes `tests/test_{legacy, legacy_routes, manifest_names, renamed_settings, former_packs}.py`
    and `tests/fixtures/mini-agents/referral-mini`;
  - edits `tests/test_{hub_client, archive, cli_vocabulary, shipped, vocabulary}.py` for what
    the helpers did.
- **Must not touch:** `conftest.py`. The unused `FORMER_ROUTES` definitions stay until 3.1.
- **Size:** about 45 files, about −2,800 lines, 64 test functions deleted.

### 1E. The threat model and other docs citations

- **Owns:** `docs/security/threat-model.md`, `docs/security/review-packet.md`.
- **Does:** removes every "Checked by" citation of a test that Phases 1D, 2 and 3 delete (the
  list is `wave6-inventory.md` section 1.14), and the rows of removed features. Row numbers stay
  in order.
- **Size:** about −700 and +100 lines.

## Phase 2. Tests onto the new fixtures (5 engineers in parallel)

**Each engineer:**

- moves their files off the former packs, using the `conftest` and `fixture_records` helpers;
- deletes, inside those files, the tests of features the lead removes (about 200 in all);
- leaves the 61 to-be-deleted files of `wave6-inventory.md` section 1.14 untouched (1D already
  deleted the five compatibility files).

`conftest.py` is frozen. A new shared helper goes in the area's own `tests/support/<area>.py`.

| Engineer | Owns | Files | Test functions (about) |
|---|---|---|---|
| 2A, engine and evaluation | decision, policy, providers, spec, spec_02, manifest_hostile, pack_content, pack_isolation, pack_status, input_schema, patterns, text, templates, provider_configs, provider_egress, llm, decision_deadline, second_signal, agent_calibration, safety_invariant, community_budget, community_caps, community_re2, evals, ledger, figures, ranking, gate, leaderboard, coverage, dataset_checks, validate_datasets, validation, validation_brief, monitoring, monitor_cron, feedback, reviewed_cases, outcomes, rater_agreement, code_systems, governance, pilot_check | 43 | 900 |
| 2B, platform and server | access_cli, accounts_store, action_audit, action_routes, administrative_audit, alerts, audit, audit_hardening, audit_integrity, auth, backend_services, break_glass, chain_orgs, checkpoints, clock, hold, hosted_data, key_presets, landing, limits, memberships, notes, notice, origin, phi, phi_detect, public_surface, read_only_commands, readiness, record_hygiene, restore, rotation, security_log, server, sessions, settings, signed_checkpoint, status_checks, tenancy, two_organizations, usage_ledger, written_store, cli, cli_errors, deploy | 45 | 950 |
| 2C, FHIR, CDS, SMART and sandbox | fhir, fhir_client, fhir_decisions, fhir_errors, fhir_media, fhir_routes, fhir_search, fhir_structure, fhir_lists, decide_op, capability, task_fhir, audit_fhir, terminology, us_core, cds_auth, cds_cards, cds_clients, cds_feedback, cds_hooks, cds_open_mode, cds_unreadable, data_coverage, bulk, smart, smart_sealing, ehr_baseline, sandbox, smart_launch, cds_client_sandbox, inferno_scripted_inputs; plus `tests/support/fhir_decisions_golden.py` | 32 | 560 |
| 2D, registry, hub, store and sites | registry, registry_acceptance, registry_chain, registry_checkpoint, registry_claims, registry_engine, registry_limits, registry_origin, registry_regression, registry_tokens, registry_yank, archive, hub_client, hub_routes, share, site_cases, site_evidence, site_reports, site_tickets, pack_store, store_improvement, store_jobs, store_promotion, store_uploads, agent_connections | 25 | 520 |
| 2E, browser | `tests/browser/*` (the 20 rewritten files), `tests/browser/conftest.py`, `tests/browser/access_site.py` | 22 | 284 |

All file names above are `tests/test_<name>.py`, except for 2E.

**Size:** 164 files, about 15,000 lines changed.

**Check:** the full suite, browser included.

## Phase 3. Removal

### 3.1 Tests and fixtures out (1 engineer, about a day)

**Owns:**

- the 61 remaining DELETE test files;
- `tests/fixtures/{reference-agents/{ai-guard, clinical-trial-prescreener, patient-inbox-router, prior-auth-preflight, referral-router}, scoreboard, datasets, us-core/{inbox, prior-auth, referral, trial}.json, us-core/invalid/{records, referral}.json, mini-agents/{guard, prescreen, trial}-mini, community-agents, eval-v1-inbox.json}`;
- the support modules listed in `wave6-inventory.md` section 1.13;
- `tests/conftest.py`: delete the old helpers; rename the folder `reference-agents` →
  `agents`; update `CORE`;
- `.github/workflows/{ci,conformance,terminology}.yml`: fixture paths, the fixtures half of
  `sandbox-e2e`, `cql`, `dtr-payer`, `crd-server`, the Da Vinci steps, `pilot-check --approve`;
- `sandbox/fixtures.compose.yml`, the `sandbox/inferno` DTR and CRD files;
- `scripts/{davinci_sample, inferno_dtr_inputs, scoreboard_page, community_check, synthetic_cases}.py`;
- `scripts/fhir_packages.lock` (regenerated).

**Size:** about 290 files, about −100,000 lines.

### 3.2 Unhooking `src` by layer (5 engineers in parallel)

Each engineer removes, from their own files, every use of the removed features (G-defaults).
Deleting a module is allowed only when all of its importers are the engineer's own files.

| Engineer | Owns |
|---|---|
| 3.2a | `cli.py`, `templates/**` (merge `base` and `agent`; delete `community`), `action.yml` |
| 3.2b | `server/*.py`, `mcp.py`, `settings.py`, `install.py` |
| 3.2c | `core/*`, `evals/*`, `errors.py`. Moves the labeller helpers and `not_writable` |
| 3.2d | `providers/*`, `standards/*`, `sandbox/*`, `guard.py`. Moves `question_system` into `terminology.py` |
| 3.2e | `dashboard/*`, `hub/` (Next.js), `docs/**` (deletes the removed pages; `mkdocs.yml` nav), `community-agents/` (delete), `.github/{PULL_REQUEST_TEMPLATE/agent.md, ISSUE_TEMPLATE/propose-agent.yml, CODEOWNERS}` |

**Size:** about 120 files, about −8,000 lines in-file.

### 3.3 Dead modules out (1 engineer, half a day)

`git rm` every module that nothing imports any more (`wave6-inventory.md` section 1, REMOVE,
plus `server/paths.py`).

**Check:** `ruff`, `mypy`, a script that imports every `src` module, and the full suite.

**Size:** about 45 files, about −20,000 lines.

## Phase 4. The rename

### 4.1 Codemod (1 engineer, 1 to 2 days, alone)

A one-off script, kept out of the repository and attached to the pull request, applies
`wave6-inventory.md` section 4:

- in table order, with the service keys first (4.10);
- word-boundary aware, on identifiers and string keys;
- `git mv` for the file renames;
- then regenerates the derived files: goldens, `task-versions.json`, `evaluation_pages.json`,
  `shown.json`, `PINNED`, `provider-configs.schema.json`, the starters' kit copies, the demo
  agent's mock and manifest, `scripts/demo_agent_data.py --check`.

**Owns:** everything. Nobody else works during 4.1.

**Check:** the full suite, browser, Hub, and `grep` for the word outside the allowed words in
code (prose may remain).

**Size:** about 600 files, about 12,000 lines.

### 4.2 Prose sweep (4 engineers in parallel, by directory)

Docstrings, comments, messages, UI copy and test names.

| Engineer | Owns |
|---|---|
| 4.2a | `src/mendari/{core, evals}` |
| 4.2b | `src/mendari/{server, hub, providers, standards, sandbox}`, root `src/mendari/*.py` |
| 4.2c | `tests/**` (browser excluded), `scripts/**` |
| 4.2d | `src/mendari/dashboard/**`, `tests/browser/**`, `hub/**`, `examples/**`, `deploy/**`, `sandbox/**` |

**Size:** about 300 files, about 4,000 lines.

## Phase 5. Documentation and the vocabulary rule (3 engineers in parallel)

**5A, root files:**

- `README.md`: provider and standards lines, the "JSON names the agent under `pack`"
  paragraph, the contributing row.
- `CLAUDE.md`: the former-pack, old-names and four-vocabulary-test rules become one rule; the
  layout and spec lines.
- `CONTRIBUTING.md`, `SECURITY.md`, `GOVERNANCE.md`.
- `CHANGELOG.md` (decision O5).
- `tasks/todo.md` (wave 6 entry), `tasks/lessons.md`.

**5B, the docs site:**

- the guides, operations pages and architecture;
- `docs/spec/agent-manifest.md` (the consolidation: 0.2's base, 0.3's mapping, 0.4's sections,
  minus the removed sections; one `spec_version`);
- `evaluation-reports.md`;
- `docs/index.md`, `roadmap.md`, `deploying-with-phi.md`;
- `mkdocs.yml`.

**5C, decisions and the rule:**

- `docs/decisions/ADR-0026-one-release-baseline-agents-only.md` (the next free number). It
  covers:
  - packs gone; no compatibility before 0.1.0;
  - the baseline schema and the refusal;
  - every format at `/1`;
  - the `agent`/`service` naming;
  - one spec version;
  - the GRAY outcomes.
- ADR status notes (decision O4).
- `tests/test_vocabulary.py` rewritten as one rule over every tracked text file:
  - excludes `notes/`, `tasks/`, `licenses/`, the vendored folders, the lockfiles and binaries;
  - allows the words of `wave6-inventory.md` section 4.13 and nothing else;
  - per decision O7, also bans the five former ids.

  `test_cli_vocabulary.py`, `test_vocabulary_docs.py` and `test_vocabulary_frontends.py` are
  deleted; their API-description and help-text surfaces need no list any more.

**Check:**

- `uv run mkdocs build --strict`;
- `python3 scripts/check_excluded.py site` and `check_site_notice.py site`;
- `scripts/doc_blocks.py run cli`, `run python` and `run install`;
- `scripts/check_doc_snippets.py`;
- `scripts/run_quickstart.py`;
- the vocabulary test.

**Size:** about 90 files, about −6,000 and +2,500 lines.

## Phase 6. Verification and production (lead, with one engineer)

**1. Locally:**

- everything in "When a phase is done";
- `uv run mendari demo`;
- `scripts/run_quickstart.py --wheel` on a built wheel;
- `docker build`, then `docker run … mendari demo`;
- `docker compose --profile signin up` with the demo half of `sandbox-e2e` by hand;
- `MENDARI_SITE_E2E=1 uv run pytest tests/test_site_e2e.py -n0`;
- `scripts/check_sdist.py`, `check_excluded.py`, `check_snomed.py`;
- the Hub's CI steps.

**2. Pull request and merge.** One pull request (squash), with the codemod script attached.
CI must be green. Required checks are unchanged:

- `test (3.11)`, `test (3.12)`, `test (3.13)`;
- `demo-agent`, `docker`, `agent-check`, `secrets`, `dco`.

Run `conformance.yml` and `terminology.yml` by hand once (`workflow_dispatch`).

**3. Production:** `wave6-inventory.md` section 5, in order: settings, merge, wipe, refill,
checks.

**4. Branches.** On the owner's go (O8), delete:

- `codex/platform-audit` and `claude/repo-links-mendari-hq` on GitHub;
- the nine local branches that hold `packs/`;
- the eight `t3/*` worktrees.

`archive/decision-packs` does not exist. `refs/pull/*` cannot be deleted.

## CI and branch protection

- **Required checks:** unchanged.
- **Jobs removed:**
  - `cql` (`ci.yml`);
  - `dtr-payer` and `crd-server` (`conformance.yml`, not required).
- **Jobs changed:**
  - `test`: fixture loop over `tests/fixtures/agents/*`; community loop out;
  - `fhir-conformance` and `terminology.yml`: `--fixture triage=…`, `--fixture chart=…`, Da
    Vinci steps out;
  - `sandbox-e2e`: fixtures half out;
  - `pilot-check`: `--approve tests/fixtures/agents/clinical-review`;
  - `agent-check`: renamed scripts, community steps out;
  - `conformance.yml`: `MENDARI_AGENTS_PATH`.
- **Expected:** CI time drops. About 1,100 fewer test functions, including the slowest families
  (hidden sets, scoreboard, Epic data fit, the EHR profile).

## Traps

1. **Collision order.** The codemod must rename the service's `agent_id` and `agent_version`
   to `service_*` before `pack_id` and `pack_version` become `agent_*`. Likewise the kit's
   `Case.agent` → `agent_id`. Locals named `agent` (a service or a provider) must be renamed
   before locals named `pack`.
2. **Old settings switch features off silently.** An old setting name in production, such as
   `MENDARI_PACK_STORE=on`, turns the feature off with no error once `legacy.setting` is gone.
3. **The pull request's own preview fails.** It copies production's pre-release database and
   will be refused at start. Expect it, and use it as the refusal's live check.
4. **Deployed starters.** An old starter ignores the new request keys and reports
   `agent_version`, which the engine no longer reads. A calibrated variant is then capped.
   Redeploy every starter, including the one behind the README's live figures.
5. **The threat model test.** It fails the moment a cited test is deleted, which is why 1E goes
   first.
6. **`-m core` shrinks silently.** It drops when a `CORE` file is deleted without a
   replacement, and 3.11 and 3.13 run only `-m core`.
7. **One bundled folder.** `bundled_dir()` reads one folder. New fixtures must sit in
   `reference-agents/` until 3.1 renames it.
8. **`davinci.question_system` is used by every agent's Task and `$decide`.** Move it before
   deleting `davinci.py`.
9. **`core/written.py` and `dashboard/written.js` import from hidden sets.** Move the labeller
   helpers and keep `labellingScreen` before deleting `evalsets`.
10. **The Hub and the engine deploy separately.** For the minutes between the two deploys the
    Hub may read keys the registry no longer sends.
11. **Neon keeps the past.** Its history window and preview branches keep the old rows after the
    wipe.
