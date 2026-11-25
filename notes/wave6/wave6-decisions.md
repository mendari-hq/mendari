# Wave 6: the lead's decisions (2026-11-24)

These settle `wave6-decisions-needed.md`. Where this file and the inventory, fixtures or build
plan differ, this file wins. The owner's instruction: packs and everything only they use go;
the infrastructure agents need stays; no compatibility with anything pre-release; old data is
wiped and replaced by new synthetic data.

## GRAY items

- **G1.** Keep `llm`. Remove `jev`, `naive_bayes`, `cql`, `criteria`, `dosing`.
- **G2.** Keep `composite` (base of `check`), `batch`, `check`, `repeat`. Remove `any_of`,
  `cascade`, `vote`. Batching's generic tests move to `test_llm`.
- **G3.** Remove `mendari train`, `mendari improve`, `mendari agreement`, `evals/training.py`,
  `evals/improve.py`, `evals/agreement.py`. `improve.not_writable` moves into `probe_tests.py`.
- **G4.** Remove `standards/davinci.py`, `pas.py`, X12, `omop.py`, `record_fhir.py`, their
  routes, commands, sandbox parts, FHIR packages and Inferno suites. `question_system` moves
  into `standards/terminology.py` first.
- **G5.** Keep `bulk.py`, `us_core.py`, `data_coverage.py`.
- **G6. Overridden: record reading stays.** Reading an EHR's records into an agent's inputs is
  agent infrastructure for real patient workflows, not pack logic: clinical notes
  (`attachment_text`), references (`follow`), a site's local codes (site codes), what a site's
  EHR lacks (EHR declarations) and order hooks (`order-sign`) are what a hospital's agents
  need, and they were built and tested against Epic's sandbox. Keep `standards/linked.py`,
  `ehr_declarations.py`, `core/site_codes.py`, the record-reading mapping features (`paths`,
  `[system=]` predicates, `follow`, `attachment_text`, `code_sets`, multi-type lists,
  `medication_period`) and order hooks, with their settings (`MENDARI_SITE_CODES`,
  `MENDARI_EHR_DECLARATIONS`) and formats (`site-codes/1`, `ehr-declarations/1`). Remove only
  what belongs to a removed provider (for example `criteria.current_within_days`).
  - Fixture F3 (`chart-summary`) carries all of it, as `wave6-fixtures.md` says under "If G6
    keeps record reading": `paths`, a `[system=…]` predicate, `follow` (`medicationReference`),
    `code_sets`, `attachment_text` (a DocumentReference note), a site code map, an EHR
    declaration and an order hook (`order-sign` on a ServiceRequest).
  - Tests: `test_fhir_linked`, `test_fhir_mapping_03` (rename it `test_fhir_mapping_records`),
    `test_site_codes`, `test_order_sign` move onto F3; `test_epic_data_fit` is narrowed to what
    F3 shows; `test_ehr_profile` and `tests/support/ehr_profile.py` stay only as far as
    record reading's tests need a vendor-shaped server, rewritten onto F3; `test_ehr_pilot_e2e`
    stays if it runs on F3, else goes; `test_ehr_scorecard` goes (it grades `notes/`).
- **G7.** Keep the generator and Mendari's own code systems. Remove the five fictional systems
  from the product (`terminology.py`); a test that needs a local code system declares it in
  its fixture, never in the product.
- **G8.** Keep `seed`, `worker`, `smart-launch`, `cds-client`, `bulk-check`, `pilot-check`.
  Remove `payer`, `omop-write`, `omop-check`.
- **G9.** As recommended: keep and rewire `fhir-conformance`, `r4-structure`, `sandbox-e2e`
  (demo half), `pilot-check`, `quickstart`, `terminology.yml`, `smart-client`, `bulk-client`;
  remove `cql`, `dtr-payer`, `crd-server`, the fixtures half of `sandbox-e2e`.
- **G10.** Remove the contribution path (`mendari new --community`, `community-check`, the
  `community` origin, `MENDARI_COMMUNITY_AGENTS`, `community-agents/`, `community/`, the
  contributing-an-agent guide, the agent PR and issue templates, `agent-check`'s community
  steps). Keep the bounded engine (ADR-0009), its constants renamed `BOUNDED_*`.
- **G11.** Keep `templates/base`, merged with `templates/agent`; the template writes spec 0.4.
- **G12.** Only `spec_version: "0.4"` is accepted. One spec page, `docs/spec/agent-manifest.md`.
  Remove `guard` (with `guard.py`, `/guard`, the console's guard mode), `derive`
  (`core/derive.py`), `coverage_information`, `questionnaire`, `policy:` as a separate file.
  **`code_systems` stays** (checked in phase 1B): record reading's `[system=…]` predicates and
  code sets depend on it (G6).
- **G13.** Remove hidden evaluation sets and the scoreboard. Keep the `labeller` role, the
  labeller helpers (moved out of `evalsets.py` first) and the blind grading screen that written
  output uses.
- **G14.** Keep the agent store, without hidden-set holds.
- **G15.** Keep site results.
- **G16.** (a) Drop `openhealth` from `core/origin.py`; keep it reserved in the registry and the
  store. (b) Delete the `server = []` extra and every `mendari[server]` mention. (c) Remove
  `scripts/synthetic_cases.py`. (d) Delete `test_ehr_scorecard`.

## Naming

N1 to N6 as recommended: `agent_id`, `agent_version`, `agent_digest`, `agent` for the governed
unit; `service_id`, `service_version`, `service` for the process that answers for it; keep
`type: agent`, `MENDARI_AGENT_URL`, `MENDARI_AGENT_TOKEN`, "the agent HTTP contract"; event
kinds `store_version.*`, `registry_agent.*`, registry `agent.*`; `agent_author`,
`agent_unavailable`; each format keeps its shape; `mendari.agent-calibration/1` keeps its name
and its key `agent` becomes `service`.

**Until the Phase 4 codemod, everything is written with today's names** (`agent_id:` for the
service in a variant, `agent_version` in mock files, `pack_id` in JSON): the fixtures'
outlines in `wave6-fixtures.md` show the final names, but a fixture built in Phase 1 must pass
today's suite. The codemod renames everything at once.

## Other decisions

- **O1.** Rename last (Phase 4), compatibility and pre-release migrations first (Phase 1).
- **O2.** Every format the first release writes is `/1`.
- **O3.** `"0.4"` is the only accepted spec version.
- **O4.** ADRs of removed features stay, marked "Superseded by ADR-0026"; the word is edited out
  of all of them.
- **O5.** CHANGELOG `0.1.0` rewritten as what the first release contains, agents only.
- **O6.** The vocabulary rule covers every tracked text file except `notes/`, `tasks/`,
  `licenses/`, vendored folders, lockfiles and binaries; `CLAUDE.md` included.
- **O7.** The vocabulary test also bans the five former ids outside `notes/` and `tasks/`.
- **O8.** The lead deleted `archive/decision-packs` (its one unmerged commit was already in main
  through #351). The lead handles the other branches; no history rewrite.
- **O9.** The demo agent stays 0.3.0.
- **O10.** Production: settings, merge, wipe, refill, checks; a few minutes down is fine
  (synthetic data, research preview).
- **O11.** No detection of old environment names; the deploy checklist lists them.

## Execution

- One integration branch, `wave6/packs-gone`, from `main` at `33b32e4b`; one pull request at
  the end; one production wipe after it merges.
- Each phase's engineers work in their own worktrees from the phase's start commit and own
  disjoint files; the lead integrates each phase and runs the whole suite before the next.
- Commits: `git commit -s`, each message ending with the line
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Never push.
