<!-- Planned by a mapping workflow on 2026-10-09 against main 2536ec1c: six readers mapped the
code LP-11 builds on (sealing and keys, access, statistics, the audit chain, the CLI and dashboard,
the lifecycle and the LP-17 and LP-20 hand-offs), two planners split it into pull requests (safety
first, and thin slices), and a judge wrote this plan from both. The conflicts below are the
defaults each PR follows unless the owner chooses otherwise; tasks/todo.md tracks progress. -->

# LP-11 build plan: hidden evaluation sets, blind labelling and agreement

## Summary

LP-11 adds hidden evaluation sets to the hosted engine's own database. It delivers:
- Sealed sets and cases.
- Blind labelling: two clinicians per case, and a blind third clinician when they disagree.
- Rater agreement: percent agreement, Cohen's kappa per pair and Krippendorff's alpha, with intervals.
- A coverage report, which also runs over any JSONL file.
- A lifecycle (draft, frozen, active, retired) with a salted freeze commitment and a rotation rule.
- A grading credential for machines only, whose runs return aggregates only.
- A keyed index of hidden inputs for LP-20.

It is built in **15 PRs, 11a to 11o**, in this order:
1. Pure tools.
2. The keys and the permissions.
3. The sealed store and its key lifecycle.
4. A generated no-leak sweep, landed before any evaluation-set route exists.
5. The store's labelling and lifecycle.
6. The routes.
7. The screens.

Every PR keeps the feature off unless both `MENDARI_EVALSET_KEY` and `MENDARI_EVALSET_INDEX_KEY` are set. Production has neither until the owner sets them.

Order of work:
- 11a comes first.
- 11b and 11d can be built beside 11c. 11c needs 11a's canonical module.
- 11e needs 11a and 11c.
- 11f and 11g follow 11e. 11g also needs 11d.
- 11h needs 11b and 11g. 11i needs 11h.
- 11j, 11k and 11l can run in parallel once 11i is merged.
- 11m follows 11j, and 11n follows 11k. 11o closes LP-11.

Hidden data reaches the store only through the operator's command line, or through tests with hand-written cases and throwaway keys.

## Pull requests, in order

- [ ] **11a** Plan entry, ADR-0011, one canonical form and the coverage report over any JSONL file (pure).
  - **What it builds:**
    - The `tasks/todo.md` LP-11 entry: this plan, its conflicts and the owner decisions.
    - `docs/decisions/ADR-0011-hidden-evaluation-sets.md` (Proposed). It records where sets live (the owner's decision), the keys, the roles, the event kinds and the aggregates-only rule.
    - `src/mendari/evals/canonical.py`:
      - `canonical_input(value)` is `ledger.canonical` applied to a value that `not_json` has checked. `ledger.py` is not changed.
      - `normalised_input(value)`, with `NORM_VERSION = "norm/v1"`. It folds string leaves only: NFKC, casefold, each run of non-word characters becomes one space, then trim. Keys, numbers, booleans, null and nesting are kept. The strict form is applied last.
      - LP-20 imports this module.
    - `src/mendari/evals/coverage.py` (`mendari.coverage/1`). It reports:
      - n, and distinct inputs counted exact and normalised;
      - raw and distinct counts per question and per label;
      - distinct positives per critical label under the normalised rule (the stricter one), marked `insufficient` under `result.MIN_POSITIVES`;
      - how many duplicate groups have conflicting labels;
      - the language, source and stratum mix, read from `attributes`, with a missing value counted as `unknown`.
    - `mendari data coverage PACK --dataset NAME|FILE [--json] [--ids]`, through `resolve_dataset` and `record_problems`. `--ids` lists duplicate case ids in local output only, for public files.
  - **Files:**
    - New: `src/mendari/evals/{canonical.py, coverage.py}`, `tests/test_canonical.py`, `tests/test_coverage.py`, the ADR.
    - Changed: `src/mendari/cli.py` (a new `data` group), `docs/spec/evaluation-reports.md`, `docs/architecture.md`, `README.md` (CLI and layout), `CHANGELOG.md`, `tasks/todo.md`, `tests/test_cli_errors.py`.
  - **Tests:**
    - Pinned bytes for key order, whitespace, escapes, NFC against NFD, and full-width forms.
    - Cases that differ only in a number stay distinct.
    - Inline JSONL built in `tmp_path`: duplicates, distinct positives, the warning under 100, unknown language and source, and no case text without `--ids`.
    - ai-guard's `test.jsonl` gives 600 rows, 81 distinct inputs, and `verdict=block` at 164 raw against 30 distinct. These numbers were checked on main. They are recorded only; `mendari.eval/2` and the card do not change.
  - **Registers in:** `BROKEN['data coverage']`, `build_parser`, the spec, and optionally `CORE`.
  - **Safe alone because:** it adds pure functions and a read-only command over a file a person names. No database, route or key is involved, and ledger bytes stay the same.

- [ ] **11b** Rater agreement statistics (pure).
  - **What it builds:** `src/mendari/evals/rater_agreement.py` (`mendari.agreement/1`):
    - Raw percent agreement per question: k of n cases whose two blind first-pass labels match, with a Clopper-Pearson interval (`stats.proportion`).
    - Cohen's kappa per rater pair, from integer counts with one division, `(nA − S)/(n² − S)`. Each pair carries its n and its marginals.
    - Krippendorff's nominal alpha across the pool, from the coincidence matrix in `Fraction`. Missing values are allowed.
    - A percentile bootstrap for kappa and alpha:
      - One `resample_indices(n, 2000, 0)` matrix per question.
      - A new `stats.bootstrap_defined(records, statistic -> float | None)` drops undefined resamples and always reports how many it dropped. `stats.bootstrap` is unchanged.
      - An estimate that is undefined on the full sample is `null` with `{status: not_estimable, reason}`.
      - The interval is `not_estimable` if any resample is undefined.
    - Derived questions: each rater's answer comes from `derive_answer` over that rater's member labels.
    - Values are rounded to `DIGITS`, and every result carries a seeds block.
  - **Files:** new `src/mendari/evals/rater_agreement.py` and `tests/test_rater_agreement.py`; changed `src/mendari/evals/stats.py`, the spec and `CHANGELOG.md`.
  - **Tests:**
    - `test_kappa_matches_hand_computed_reference`: hand-computed tables, and statsmodels `cohens_kappa` to 1e-12.
    - `test_alpha_matches_hand_computed_reference`: Krippendorff 2011 gives exactly 113/152, checked also against an independent `Fraction` implementation.
    - `test_bootstrap_interval_matches_reference`: `scipy.stats.bootstrap` on the same index matrix, through `FixedIndices`.
    - The undefined cases, derived questions, and byte-identical reruns.
  - **Registers in:** the spec and `CORE`.
  - **Safe alone because:** there is no I/O and no command. No dependency is added: krippendorff and sklearn stay out of the lock file.

- [ ] **11c** Evaluation-set keys and sealing (nothing stored yet).
  - **What it builds:**
    - `src/mendari/core/evalset_keys.py`:
      - A frozen `EvalsetKeys` with `key`, `read_keys` and `index_key`, all `repr=False`.
      - `key_ids` (`{current, read, index}`), `current_only()` and `ring_ids`.
      - `seal`/`open` for the kinds `header`, `input`, `attributes`, `label`, `freeze` and `aggregates`.
      - `case_hash(set, case)` and `index_hashes(value)`.
    - `evalset_keys()` follows `checkpoint_key()`:
      - With both keys unset it returns None, and the feature is off.
      - Server and CLI startup stops when a key is malformed or the wrong length, when only one of KEY and INDEX_KEY is set, when READ_KEYS is set without KEY, or when any two values are equal.
      - Each key must also differ from the raw values of `MENDARI_PHI_KEY`, each `MENDARI_PHI_READ_KEYS` entry, `MENDARI_INPUT_HASH_KEY` and `MENDARI_CHECKPOINT_KEY`, whatever the PHI mode.
      - Messages name variables and positions only, never a value, and give the `_GENERATE` hint.
    - `phi.py`:
      - The bodies of `PhiPolicy._seal`/`_open` move into module-level `seal_with(key, aad, value)` and `open_with(ring, aad, token, what)`. `PhiPolicy` keeps calling them, so PHI bytes do not change.
      - `_parse` and `token_key` become public.
      - `_read_keys` takes the variable name and the keys to compare against. Its PHI messages stay identical.
    - Tokens and binding:
      - Every evaluation-set token is tagged `v2:<key id>:` from its first seal. The legacy format is not copied.
      - The associated data is `b"mendari-evalset-v1\0" + kind + NUL + set + NUL + case`, with `NUL + label` added for labels.
      - Rows that belong to a set rather than a case bind an empty case id.
      - Ids are validated as UUIDs.
    - Keyed hashes are written `hmac-sha256:<index key id>:<hex>`:
      - The case hash uses the prefix `mendari-evalset-case-v1\0`.
      - The index uses `mendari-evalset-index-v1\0canonical\0` and `…\0text\0`, over `canonical_input` and `normalised_input`.
    - Server startup (`app.py`, lines 411 to 447) parses the keys after `phi_policy()` and before `AuditStore`. With the keys set, it refuses to start in either of these cases:
      - sign-in is off;
      - `VERCEL` is set and `VERCEL_ENV != production`.
    - `/api/health` shows the `evalset_keys` ids to `read` principals only.
  - **Files:**
    - Code: `src/mendari/core/{evalset_keys.py (new), phi.py}`, `src/mendari/server/app.py`, the `src/mendari/server/vercel.py` docstring.
    - Docs: `docs/deploying-with-phi.md` §8 (three rows, saying the feature is off without them), `README.md`, `CHANGELOG.md`, and `.gitleaks.toml` only if a doc gains an empty `MENDARI_EVALSET_KEY=` line.
    - `tests/conftest.py`: the three variables go in `LIVE_SETTINGS`; an autouse fixture deletes them; an `evalset_on(monkeypatch)` helper sets throwaway keys from `secrets.token_bytes`.
    - `tests/test_cli_errors.py`: `places` clears the three variables.
  - **Tests** (`tests/test_evalset_keys.py`):
    - Unset means off.
    - Every malformed or half-set form is refused by both server and CLI, with no value in the message.
    - The keys differ from each other and from every PHI, hash, read and checkpoint key.
    - The server refuses to start without sign-in, and on a Vercel Preview.
    - Health shows ids only, and only to `read`.
    - A token sealed for one kind, set, case or label does not open as another, and two labels' tokens swapped fail.
    - Every token is v2.
    - A fixed PHI vector still opens. `test_phi.py` and `test_rotation.py` pass without change.
  - **Registers in:** `LIVE_SETTINGS`, §8 of the deploying guide (`test_deploy`), and the `vercel.py` docstring.
  - **Safe alone because:** there is no table, route or write. With the keys unset nothing changes. With them set, the only additions are startup checks and key ids in health.

- [ ] **11d** Permissions, the labeller role and the grading credential (no evaluation-set route yet).
  - **What it builds** (`src/mendari/server/auth.py` unless named otherwise):
    - `label`:
      - `PERMISSIONS['labeller'] = {label}`, and `label` joins `PEOPLE_ONLY`.
      - `_member` widens its role intersection from `ROLES` to `ROLES + accounts.GRANTING_RESERVED` (`("labeller",)`), so the role grants something in memberships mode only.
      - The claims-mode `ROLES` and `MENDARI_OIDC_ROLE_MAP` do not change, and `pack_author` still grants nothing.
    - `grade`:
      - `PERMISSIONS['grading'] = {grade}`.
      - A new `GRADING_ONLY = {grade}` mirrors `EHR_ONLY`. It is removed from every principal except a `key` whose role is `grading`.
      - `Auth.check` gets a dedicated 403, with the security-log reason `grading_only`.
    - `curate` (set management): held by admin and added to `PEOPLE_ONLY`. Admin holds neither `grade` nor `label`.
    - Open mode: `Principal.may` never returns True for `grade`, `label` or `curate`.
    - The grading credential:
      - `AuditStore.create_grading_key(org, days ≤ 365, actor, reason)` goes through `_insert_key`, which accepts role `grading` on this path only. It chains `key.created {role: grading, org}`.
      - The command is `mendari access grader-key --org ORG [--days N] --reason TEXT`. It is for the operator only and prints the secret once.
      - `MACHINE_ROLES`, `KeyRequest`, `ClientRequest` and the presets do not change. Rotating the key stays refused (409). Admins may list and revoke it.
    - Dashboard:
      - `main.js` sets `state.labelOnly` for a signed-in person who has `may('label')` and not `may('discover')`. In that mode it skips the notice and the packs calls, and shows "Nothing to do here yet".
      - `router.js` skips `refreshReviewCount` without `read`.
      - `ui.js`: the labeller entry gets text in place of `reserved`.
      - `admin.js` offers no Rotate action for a role outside `MACHINE_ROLES`. It shows the role from server data, so no dashboard source contains the word "grading".
    - `landing.py`: `_ORDER` and `_WHO` gain the three permissions, and `_scope` handles `GRADING_ONLY` as it does `EHR_ONLY`.
  - **Files:**
    - Code: `src/mendari/server/{auth.py, landing.py, security_log.py docstring}`, `src/mendari/core/{accounts.py, audit.py}`, `src/mendari/cli.py`, `src/mendari/dashboard/{main.js, router.js, ui.js, admin.js}`.
    - Docs: an ADR-0004 amendment (only the operator mints a grading key); an ADR-0008 amendment (labeller grants `label` in memberships mode); `docs/operations/accounts.md`; the README role table; threat model E2 (its cited test is renamed); a new row E11 for the labeller and grading privileges.
  - **Tests:**
    - `test_auth`:
      - The `ROLE_PERMISSIONS` oracle and new `expected()` branches: `grade` only for a grading key; `label` for people only, in memberships mode only; `curate` for admin people; open mode holds none of the three.
      - The principals list gains the CLI-minted grading key.
      - `test_new_keys_are_for_machines_only` answers 422 for role `grading`.
    - `test_memberships`:
      - `test_reserved_roles_grant_nothing` now uses `pack_author`.
      - `test_reserved_roles_may_be_given_and_grant_nothing` is split, with E2 updated to match.
      - A new `test_labeller_grants_label_only`.
    - The `test_accounts_store` role pins.
    - `tests/test_grading_key.py`:
      - The key is minted by the CLI only and lasts at most 365 days.
      - Minting is chained.
      - Rotation answers 409, and revocation works.
      - Every route outside `PUBLIC` refuses it.
      - No HTTP route mints one.
    - Browser `test_access`:
      - A labeller-only member's `pins()` shows only `/api/auth/me` and `/api/health`.
      - The "grants nothing" cases use `pack_author`.
    - `BROKEN['access grader-key']`.
    - `test_invite_page` passes unchanged.
  - **Registers in:** the auth oracle, the landing wording, and the security-log reasons.
  - **Safe alone because:**
    - No route requires `label`, `grade` or `curate`, so a grading key reaches nothing.
    - Claims mode does not change.
    - Admin loses nothing it uses today.

- [ ] **11e** Store I: sealed sets and cases, the hidden-case index and `audit verify --evalsets` (operator CLI only).
  - **What it builds:**
    - Tables in `_TABLES`, with no `;` inside the DDL:
      - `evalset_sets`: id, org, state, created_at, created_by, header (sealed), frozen_at, commitment, freeze (sealed), max_runs, max_age_days, retired_at, aggregates (sealed). The columns 11i fills are nullable and created now, so no `_ADDED_COLUMNS` entry is needed later.
      - `evalset_cases`: id, set_id, org, input (sealed), attributes (sealed), case_hash, status, created_at.
      - `evalset_index`: hash, variant, key_id, set_id, case_id, org.
    - Settings marks: `evalset_key_known:`, `evalset_key_retired:` and `evalset_index_epoch:`.
    - `src/mendari/core/evalsets.py` `EvalsetStore`, built over `AuditStore` and using `_bound` and `_append_many` with literal kinds:
      - `create_set(org, pack, name)` seals the header: pack id, name, member and derived questions with their labels, and critical labels. It chains `evalset.created {org, set}`.
      - `add_cases(set, records)` refuses:
        - any input that fails `not_json`;
        - a `source` other than `hand_written` (`de_identified` is refused until B20);
        - a `template` that is not null;
        - a record missing `author`, `language` or `stratum`;
        - any `system` or `feedback` block;
        - an exact or normalised match with the reference packs' data and `datasets/` as bundled in the installed package (`mendari/_packs`, `mendari/_datasets`), or with a generator's output at its default seed. The refusal names the line only.
      - `add_cases` then stores:
        - The store mints UUID4 ids. An author's id becomes a sealed attribute. An optional intended label is sealed in the attributes.
        - The input is sealed. The attributes are sealed together with a 32-byte salt.
        - `case_hash` is set and both index rows are written.
        - One `evalset.case_added {org, set, case: <case hash>, case_ref: sha256(prefix ‖ salt ‖ canonical({set, case, input, attributes}))}` is chained per case, in one exclusive append.
      - `remove_case` works on drafts only. It deletes the row and its index rows and chains `evalset.case_removed`.
      - `evalset_ready(keys)` runs at startup and before every write. It refuses:
        - a retired current key;
        - an evaluation-set key whose id the database marked `phi_key_known:` (and `ready_to_keep` refuses a PHI key marked `evalset_key_known:`);
        - once rows exist, a ring that holds no key the database knows.
        - Only after these checks pass does it mark the ring's keys known.
      - The first index row under a new index-key id chains `evalset_index.epoch {key_id}` as a deployment event under `KEYS_CHAIN`, cached on a store attribute of its own.
      - `hidden_matches(value)` returns matching set and case ids across the deployment. It lives in the store only, with no route or command.
    - `verify(evalsets=keys)` / `_check_evalsets`, modelled on `_check_notes`:
      - Every `case_added` event has its row, unless a `case_removed` event follows it.
      - Case hashes are recomputed for the current index-key epoch, and `case_ref` is recomputed from the opened row.
      - A row under a key the database knows but this ring lacks counts as `unopenable` (ok is false).
      - A row altered under a held key gives `broken_at`.
      - Without keys, rows count as `unchecked`. A database without the tables is read as it is.
    - Event classes:
      - `evalset.created`, `evalset.case_added` and `evalset.case_removed` go in `ADMINISTRATIVE_KINDS`, with `SUBJECT_TABLES['evalset'] = ('evalset_sets', 'id', _ROW_ORG)`.
      - `evalset_index.epoch` goes in `DEPLOYMENT_KINDS`.
      - `audit_fhir._ADMINISTRATIVE` gains entries. `_ADMINISTRATIVE_DETAILS` stays unchanged.
    - PHI retention, `phi erase` and `phi rotate` never touch `evalset_` rows.
    - The CLI goes through a new `_evalset_store(target, writes)`, which uses `--db` or `$DATABASE_URL`. `_servers_database` also accepts a database that holds only `evalset_sets`.
      - `mendari evalset create|add SET FILE|remove-case|list`. Output is ids and counts only.
      - `add` refuses a file inside the installed package or the repository checkout.
      - `mendari audit verify --evalsets` is read-only, marks nothing, and cannot be combined with `--since-checkpoint`.
    - A new runbook `docs/operations/evalsets.md`, added to the `mkdocs.yml` nav.
  - **Files:** `src/mendari/core/{evalsets.py (new), audit.py}`, `src/mendari/standards/audit_fhir.py`, `src/mendari/cli.py`, the runbook, `mkdocs.yml`, `README.md`, `CHANGELOG.md`.
  - **Tests:**
    - `tests/test_evalset_store.py`:
      - Raw database bytes hold no planted mark.
      - Chained payloads hold only ids, case hashes and commitments.
      - Each refusal listed above.
      - The verify outcomes: ok, altered, unopenable and unchecked.
      - The index matches both canonical and normalised inputs.
      - Index and commitment prefixes never collide.
      - A writer whose ring holds no known key refuses.
      - Retention, `phi erase` and `phi rotate` leave `evalset_` rows alone.
      - `test_audit_verify_passes_after_cases_added[sqlite, postgres]`.
    - `tests/test_no_hidden_data_shipped.py`: no tracked file holds a sealed token or a value for any of the three key variables.
  - **Registers in:**
    - `test_audit_fhir.NOT_ON_A_DECISION`, `test_chain_orgs.every_writer`, `test_administrative_audit` and `test_terminology`.
    - `test_two_organizations`: `EXPLICIT` (the store methods that take `org`), `DEPLOYMENT_READS` (`hidden_matches`) and `WRITTEN` (the evalset tables).
    - `test_read_only_commands.COMMANDS` (`audit verify --evalsets`) and `BROKEN`.
  - **Safe alone because:**
    - No route reads the tables, and writes come only from the key holder's command line.
    - Administrative events show ids, keyed hashes and commitments only.
    - Verify without keys reports `unchecked`, never a break.

- [ ] **11f** Key lifecycle: rotation with its check, reindexing, the sealed export, and the sealed-column pin.
  - **What it builds:**
    - A `SEALED_COLUMNS` registry in `evalsets.py`.
    - `mendari evalset rotate --to <current id> [--batch 200] [--check]`, copied from `rotate_phi` and `phi_check`:
      - `--to` must be the current key, and a retired target is refused. The target is recorded as `evalset_key_target`.
      - Rows are paged by primary key, filtering on `substr(token,1,20) <> 'v2:<to>:'`.
      - Values are opened outside the chain lock and written back with compare-and-set.
      - Each batch chains one `evalset.rotated {to, from: {id: count}, counts}` (a deployment event, under `KEYS_CHAIN`).
      - Keys a batch empties are marked retired in the same session.
      - Values no key opens are listed as `unopenable`.
      - Rotation works in every set state.
    - `--check` is read-only:
      - It opens everything with `current_only()`, recomputes every commitment, and checks the index rows of the current epoch.
      - It reports `read_not_retired`, `current_retired`, the failures (capped at `CHECK_FAILURES_SHOWN`) and `ready`.
      - It exits 1 unless the database is ready.
    - `mendari evalset reindex` needs both keys. It rebuilds the whole index under the current index key in one transaction, and chains `evalset_index.epoch` (when the key is new) and `evalset_index.rebuilt {key_id, count}`. Chained case hashes keep their key id.
    - `mendari evalset export --db --out FILE` writes ciphertext, ids and key ids only. It needs no key, opens nothing and chains nothing.
    - A runbook section "Rotating the evaluation-set key" beside "Rotating the keys": add the reader first, swap, re-seal, check, drop; plus the reindex steps.
  - **Files:** `src/mendari/core/{evalsets.py, audit.py}`, `src/mendari/standards/audit_fhir.py`, `src/mendari/cli.py`, `docs/deploying-with-phi.md`, `docs/operations/evalsets.md`, `README.md`, `CHANGELOG.md`.
  - **Tests** (`tests/test_evalset_rotation.py`):
    - Two rotations in a row, by the documented steps, with writes between the swap and the re-seal (lesson of 2026-10-06).
    - `--check`, `verify` and `export` write nothing and mark nothing.
    - A retired key seals nothing.
    - PHI rotation never touches evaluation-set rows, and the reverse.
    - `test_every_sealed_evalset_column_is_rotated_checked_verified_and_exported`: every column of an `evalset_` table is either in `SEALED_COLUMNS` or in a pinned list of clear columns. This pin protects every later PR.
    - The reindex is atomic, so mixed key ids are never visible.
    - The export file holds no mark.
    - Postgres through `db_target`.
  - **Registers in:** `test_read_only_commands.COMMANDS` (`evalset rotate --check`, `evalset export`), `BROKEN`, and the kind inventories.
  - **Safe alone because:** these are operator commands over ciphertext. Nothing new becomes readable, and a rerun resumes where it stopped.

- [ ] **11g** The generated no-leak sweep (tests only, before any evaluation-set route).
  - **What it builds** (`tests/test_evalset_leaks.py`):
    - The world: memberships mode, sign-in, Backend Services, encrypt and `evalset_on`. Two organizations each hold a draft set of hand-written cases.
    - Planted marks: case inputs, author, stratum, a custom language code, the author's id, case ids, salts and sealed tokens.
    - The calls cover every mounted route and method from `mounted_routes(app)`, with evaluation-set placeholders filled once they exist. The principals are:
      - anonymous;
      - a person per claims role, and a member per role;
      - a labeller member;
      - keys for each machine role, and legacy keys of the other roles;
      - `SCOPED_KEYS` and every preset;
      - clients with all scopes and with each scope;
      - an EHR launch alone and with a session;
      - the grading key.
    - What is scanned:
      - Bodies; headers (Location, Content-Disposition, ETag, Link); NDJSON.
      - `security_lines`, `caplog` and the RequestLog line.
      - The administrative list and its FHIR export for each admin.
      - `mendari audit export [--administrative]`, the feedback export, `/api/evals`, `/api/audit/verify`, health and OpenAPI.
      - The stdout and stderr of every `evalset` command, the export file, and raw `audit_events`.
    - The assertions:
      - No mark appears anywhere.
      - Each `evalset.*` payload's keys are within a pinned allow-list for its kind.
      - Every URL answers someone.
      - Every evaluation-set route is swept.
    - Route-inventory fixtures turn the feature on and assert it. Until 11j, the assertion is that health names the key ids; from 11j on, that an evaluation-set route is mounted. The fixtures are in `test_auth`, `test_hosted_data`, `test_two_organizations` (a set per organization), `test_memberships` (`as_nobody` and the ROLES loop), `test_origin`, `test_shipped` and `test_limits`.
    - Threat model row I13 (hidden sets, information disclosure) names these tests.
  - **Files:** the tests above and `docs/security/threat-model.md`.
  - **Tests:** this file is the test.
  - **Registers in:** nothing in `src/`. From here on, each PR that adds a set state seeds it in this world, and new routes are swept by generation.
  - **Safe alone because:** it adds tests and one documentation row only.

- [ ] **11h** Store II: sampling, assignment, blind labelling, adjudication and measured agreement (store and operator CLI).
  - **What it builds:**
    - New tables:
      - `evalset_assignments`: id (UUID), org, set_id, case_id, user_id, membership_id, round (`first` or `adjudication`), created_at, ended_at.
      - `evalset_labels`: id, org, set_id, case_id, assignment_id, body (sealed), created_at.
      - Case `status` takes the values `draft`, `sampled`, `agreed`, `disputed`, `resolved` and `unresolved`.
    - `sample(set, per_stratum | n, seed)`:
      - A seeded stratified draw within the set.
      - It reseals each drawn case's attributes with its weight `N_h/n_h`.
      - It chains `evalset.sampled {org, set, n, seed}`, with counts only and never a stratum name.
    - `assign(set, labellers, seed)`:
      - Gives each sampled case two different labellers, balanced across labellers and across pairs, deterministically.
      - Refuses anyone without the labeller role in force in the set's organization, and anyone holding `curate` there.
      - Reassigns open assignments of an ended membership on the next run.
      - Chains `evalset.assigned {org, set, case: <hash>, assignment, labeller: <user id>, round}`.
    - `adjudicate(set, labellers, seed)` gives each disputed case a third labeller who is neither of the first two. The same refusals apply.
    - `next_case(user)`:
      - Serves draft sets only, while the labeller membership is in force, oldest open assignment first.
      - Opens the input only. It chains `evalset.case_read {org, set, case, assignment, labeller}` in the same exclusive session, before returning `{assignment, questions, input}`. The questions are the header's member questions and their labels.
      - Never returns attributes, the intended label, another label or a model output.
    - `record_label(assignment, user, values)`:
      - Every member question must be answered. There is one submission per assignment, and labels never change once submitted.
      - It seals `{salt, values}` bound to its label id. It chains `evalset.labelled {org, set, case, label_ref: {id, sha256(prefix ‖ salt ‖ canonical({set, case, label, assignment, values}))}, labeller, round}`.
      - When a case's second first-pass label arrives, the two are compared inside the session: the case becomes `agreed` or `disputed`.
      - After adjudication, the majority per member question gives `resolved`. No majority leaves the case `unresolved`.
    - `progress(set)` returns counts by status and per labeller user id.
    - `measure(set)`:
      - Opens attributes and labels.
      - Computes coverage (11a) and agreement (11b) over first-pass labels.
      - Seals the results as `aggregates` and chains `evalset.measured {org, set, n}`.
    - `verify --evalsets` now also recomputes the label commitments.
    - CLI: `mendari evalset sample|assign|adjudicate|progress|agreement [--seed 0] [--resamples 2000] [--json]|coverage`.
      - Output is ids, counts and aggregates only, with raters named by user id.
      - There is no labelling command.
  - **Files:** `src/mendari/core/{evalsets.py, audit.py}`, `src/mendari/standards/audit_fhir.py`, `src/mendari/cli.py`, the runbook, `CHANGELOG.md`, and the tests below.
  - **Tests** (`tests/test_evalset_labelling.py`):
    - `test_a_second_labeller_never_sees_the_first_labellers_answer_or_model_output`, at store level, with a decision on the same input planted.
    - Blind adjudication, the majority rule, and unresolved cases.
    - Separation of duties, and reassignment after a membership ends.
    - Sampling is seeded and records weights; pairs are balanced.
    - No label value or label word appears in clear in `audit_events`.
    - A tampered sealed label makes verify report a break.
    - `test_audit_verify_passes_after_labelling_and_adjudication[sqlite, postgres]`.
    - A second labelling round by the documented steps, with labels written in between.
    - `measure` gives the same results as `rater_agreement` on the same labels.
    - Progress names no case.
  - **Registers in:**
    - The sweep's world: labels planted; a labeller with and without assignments; no label word in payloads or logs.
    - `SEALED_COLUMNS`, the kind inventories and `test_chain_orgs.every_writer`.
    - `test_two_organizations` `EXPLICIT` and `WRITTEN`, and `BROKEN`.
  - **Safe alone because:** there is no route, so no person can label yet.

- [ ] **11i** Store III: the lifecycle, the freeze commitment, the in-process grading read, the rotation rule and retirement.
  - **What it builds:**
    - Every write method refuses a set that is not in draft. The check runs inside the store's exclusive session (`SELECT … WHERE state = 'draft'`).
    - `freeze(set, max_runs, max_age_days, drop_unsampled=False)`:
      - Refused while any case is unsampled (unless `--drop-unsampled` removes them, each removal chained), unlabelled, disputed or unresolved.
      - Takes the final member labels and computes the derived ones with `derive_answer`.
      - `content_hash` is the sha256 of `canonical_input` over `mendari.evalset/1`. That covers the header (pack id, questions and labels, critical labels, limits) and the cases sorted by id: `{id, input, final labels, source, stratum, weight, language, author, template}`.
      - `commitment = sha256("mendari-evalset-freeze-v1\0" ‖ salt ‖ content_hash)`.
      - `{salt, content_hash}` is sealed as `freeze`, and coverage and agreement as `aggregates`.
      - It chains `evalset.frozen {org, set, commitment, n, max_runs, max_age_days}`.
    - `grade_read(set, grader, pack_digest, run)` serves frozen or active sets only:
      - It recomputes the content hash against the sealed and chained commitment. On a mismatch it refuses without naming a case.
      - The rotation rule: the count of chained `evalset.graded` events must be below `max_runs`, and the time since `frozen_at` below `max_age_days`.
      - The first read moves a frozen set to active and chains `evalset.activated` in the same transaction.
      - It chains `evalset.graded {org, set, commitment, grader, pack digest, n, run}` before the in-process `run(dataset)` receives the cases.
      - Only what `run` returns comes back, and nothing is stored.
    - `retire(set)` recomputes the hash and chains `evalset.retired`. Nothing is published.
    - CLI:
      - `mendari evalset freeze --max-runs N --max-age-days D [--drop-unsampled]` prints the commitment and the distinct positives per critical label. It warns under 100 but does not refuse.
      - `evalset retire`.
      - `evalset show`: state, commitment, runs used and left, and the stored aggregates.
    - `verify --evalsets` and `rotate --check` recompute the freeze hash against the commitment.
    - Threat model row T6 (tampering with a frozen set).
  - **Files:** `src/mendari/core/{evalsets.py, audit.py}`, `src/mendari/standards/audit_fhir.py`, `src/mendari/cli.py`, the runbook, `docs/security/threat-model.md`, ADR-0011, `CHANGELOG.md`.
  - **Tests** (`tests/test_evalset_lifecycle.py`):
    - `test_freezing_fixes_the_content_hash`: insertion order does not matter, the recompute is equal, and the salt changes only the commitment.
    - `test_any_change_after_freeze_is_refused`: add, remove, sample, assign, adjudicate, label and refreeze are each refused, with nothing written or chained.
    - A direct SQL edit makes verify report a break and `grade_read` refuse.
    - Exhausted runs, exceeded age (with an injected clock) and a retired set are each refused.
    - A frozen set becomes active on its first read.
    - Rotating a frozen set changes ciphertext only.
    - A second lifecycle by the documented steps, with writes in between.
    - The unsalted content hash never leaves the sealed store.
    - `test_audit_verify_passes_after_labelling_adjudication_and_freeze[sqlite, postgres]` (the acceptance test).
  - **Registers in:**
    - The sweep's world: frozen sets, active sets (through `grade_read`) and retired sets.
    - `SEALED_COLUMNS`, the kind inventories, `BROKEN`, and `test_read_only_commands.COMMANDS` (`evalset show`).
  - **Safe alone because:** `grade_read` runs in-process only and nothing calls it until 11l. Freeze and retire are the key holder's commands.

- [ ] **11j** The labelling routes (`label`, memberships mode).
  - **What it builds:**
    - The routes are mounted only with sign-in, memberships mode and the keys, as `/invite` is:
      - `GET /api/labelling` returns the caller's counts per set.
      - `GET /api/labelling/next` returns `{assignment, questions, input}`, or 204. It chains `evalset.case_read`.
      - `POST /api/labelling/{assignment_id}` takes `{labels}` and answers `{remaining}`.
        - An unknown assignment, someone else's and a closed one all get the same 404.
        - A 422 names the question path, never the value.
    - Response models are closed (`extra=forbid`).
    - Each route has its `_RULES` entry before the `GET ^/api/` catch-all, and `_MEMBERSHIPS_ONLY` is extended.
    - Security-log events `evalset.case.read` and `evalset.labelled` carry set ids only in `ids`.
    - A `label` rate-limit class in `Limiter.classify`.
    - Not notice-gated.
    - An OpenAPI tag, and the `/docs` landing lists the routes under `label`.
  - **Files:** `src/mendari/server/{app.py, auth.py, limits.py, api_docs.py, landing.py, security_log.py docstring}`, `README.md`, `CHANGELOG.md`.
  - **Tests** (`tests/test_labelling_routes.py`):
    - `test_a_second_labeller_never_sees_the_first_labellers_answer_or_any_model_output`, over HTTP.
    - Someone else's assignment answers as an unknown one does.
    - A frozen set is closed to labelling, and a curator is never assigned.
    - Keys get the `people_only` 403.
    - The routes are absent in claims mode.
  - **Registers in:**
    - The `test_auth` pins, plus a test that no evaluation-set route resolves to `read` or `discover`.
    - `test_shipped` `CHAINED` and `CHAINED_READS`.
    - `test_two_organizations` (`MEMBERSHIPS_ONLY`, `covered`) and `test_hosted_data` (placeholder filling).
    - `test_limits.EXPECTED_CLASSES` and the `test_security_log` event list.
    - The sweep finds the routes by itself.
  - **Safe alone because:**
    - Production runs claims mode without the keys, so the routes are absent there.
    - Keys, admins and open mode never hold `label`.
    - A labeller reaches only their own assignments.

- [ ] **11k** The set-management routes (`curate`, memberships mode).
  - **What it builds:**
    - `GET /api/evalsets` returns id, name, pack, state and counts.
    - `GET /api/evalsets/{set_id}` returns:
      - name, pack and questions;
      - state, commitment, and runs used and left;
      - progress counts per labeller user id, and case-status counts;
      - the stored aggregates with their times.
      - It returns no input, label, attribute or case id.
    - `POST /api/evalsets/{set_id}/sample`, `…/assignments` and `…/adjudication` chain `evalset.sampled` and `evalset.assigned`.
    - Another organization's set answers 404, as an unknown one does.
    - No route computes statistics, freezes or retires.
  - **Files:** `src/mendari/server/{app.py, auth.py, landing.py, api_docs.py}`, `README.md`, `CHANGELOG.md`.
  - **Tests** (`tests/test_evalset_routes.py`):
    - Response models are pinned field by field.
    - Every role except admin is refused, the labeller included.
    - Aggregates appear only after `evalset agreement` or freeze.
    - A curator cannot assign themselves.
    - The two organizations stay isolated.
  - **Registers in:** the same inventories as 11j.
  - **Safe alone because:** it has the same gating as 11j, and its answers are ids, counts and stored aggregates only.

- [ ] **11l** The grading route and the aggregates-only result (`grade`).
  - **What it builds:**
    - `POST /api/evalsets/{set_id}/runs`:
      - Mounted with sign-in and the keys, in both account modes. Its `_RULES` entry sits before the `curate` rules.
      - Accepts a grading key of the set's organization only.
      - Body: `{pack, variant?}`. The pack must equal the header's pack id.
      - Every provider slot must be local.
    - The run:
      - `grade_read` builds the dataset in memory with `load_dataset(raw=…)` and passes it to a Decision with no store, then to `result_from_ledger`, in memory.
      - The result is projected to a closed `mendari.hidden_result/1` in `src/mendari/evals/hidden.py`, built from `share.py`'s sub-models.
      - It keeps per-question metrics, and critical counts without `missed_cases` or `misclassified`.
      - It keeps only calibration bins of `MIN_GROUP` cases or more.
      - It has no subgroups, no equal-mass bins, and no case id, dataset name or dataset hash.
      - It adds set id, commitment, pack digest, n, run number, runs left and seeds.
    - What it never does:
      - It never calls `save_report`, writes `eval_reports`, or keeps a ledger.
      - It never returns a `record_problems` message: a failure is redacted to a fixed code.
      - On a hash mismatch it answers 409 `set_changed`, and logs an operator line naming the set only.
    - `grade` maps to the `evaluate` rate-limit class.
    - A spec section for `mendari.hidden_result/1`.
  - **Files:**
    - Code: `src/mendari/server/{app.py, auth.py, limits.py, landing.py, api_docs.py}`, new `src/mendari/evals/hidden.py`, `src/mendari/core/evalsets.py`.
    - Docs: `docs/spec/evaluation-reports.md`, ADR-0011, `README.md`, `CHANGELOG.md`.
  - **Tests** (`tests/test_evalset_grading.py`):
    - Only a grading key passes. Admin people, legacy admin keys, service keys, scoped keys and clients are all refused.
    - The result has no per-case field.
    - Runs are counted, and the limits refuse.
    - A frozen set becomes active on its first run.
    - A remote variant is refused.
    - `/api/evals` is unchanged after a run.
    - A tampered set is refused without naming the case.
    - The sweep expects zero marks for the grading key too.
  - **Registers in:** `test_auth`, `test_shipped` (`CHAINED`), `test_limits`, `test_two_organizations` (a grading key per organization, `secrets_of`) and the landing `_WHO`.
  - **Safe alone because:** only the operator's command mints a grading key, on a deployment that has the keys. The answer is aggregates only.

- [ ] **11m** The labelling screen.
  - **What it builds:**
    - `labelling.js` (`views.labelling`, `#labelling`).
      - Its imports are single lines, and only of `router.js`, `ui.js` and a new `record.js`.
      - `record.js` is the generic record renderer taken out of `pack.js`, which now imports it too.
    - Navigation:
      - `labelOnly` is pinned in `route()`, as `launchOnly` is.
      - The nav shows only Labelling, through a hidden link in `index.html`.
      - A shape in `skeleton.js`, and `style.css` tokens only.
    - One case at a time:
      - Radio groups with nothing preselected, each target at least 24px.
      - Submit, then the next case, with focus moved to its heading.
      - Nothing in localStorage or sessionStorage.
      - No model output, criteria, confidence, badges, attributes or anyone's progress.
  - **Files:** `src/mendari/dashboard/{labelling.js (new), record.js (new), pack.js, main.js, router.js, index.html, skeleton.js, style.css}`.
  - **Tests** (`tests/browser/test_labelling.py`):
    - `pins()` shows only `/api/health`, `/api/auth/me` and `/api/labelling*`.
    - The second labeller is blind, and nothing is preselected.
    - axe in both themes, phone width and `UNDER_24`.
    - Storage is empty after labelling.
    - The import lines are pinned.
    - `test_server`'s import map, `test_dashboard_contrast` and `test_invite_page` still pass. The `test_access` labeller case is updated.
  - **Safe alone because:** it is a screen over 11j only.

- [ ] **11n** The management screen.
  - **What it builds:**
    - `evalsets.js` (`#evalsets[/<set id>[/<tab>]]`), available only to a signed-in person with `may('curate')` in memberships mode.
    - It copies `admin.js`'s tab pattern without importing it. The tabs:
      - Progress, with names taken from the members list.
      - Sample, Assign and Adjudicate forms.
      - Agreement: percent agreement as k of n through `figureHtml`/`shown`; kappa and alpha as numbers; "not estimable" written out in words.
      - Coverage, with the insufficient-evidence mark.
    - No per-case rows. The wording says "in use" and "evaluation runs".
  - **Files:** `src/mendari/dashboard/{evalsets.js (new), main.js, index.html, skeleton.js, style.css}`, `README.md`.
  - **Tests** (`tests/browser/test_evalsets.py`):
    - Sample, assign and track progress.
    - axe, phone width, 24px targets and `BARE_PERCENT`.
    - Labellers and people who are not admins never see the view, checked with `pins()`.
  - **Safe alone because:** it is a screen over 11k, and it shows ids, counts and stored aggregates only.

- [ ] **11o** Close-out.
  - **What it builds:**
    - `launch/gates.yaml`:
      - `labelling-underway`'s pending probe is replaced by the 11g, 11h, 11j and 11m tests.
      - `hidden-sets-frozen`'s pending probe is replaced by the 11a, 11g and 11i tests.
      - The attestations stay empty.
    - The threat model's residual risks: offline decryption by the key holder, case-hash correlation, adjudication revealing disagreement, and paraphrased templates.
    - The completed runbook, with the owner's key steps.
    - ADR-0011 records what was built.
    - `README.md`, `CHANGELOG.md` and the todo build log.
  - **Tests:** one end-to-end test, through the CLI and HTTP, followed by `mendari audit verify --evalsets` on SQLite and Postgres.
  - **Safe alone because:** it adds docs, the gate probes and one test.

## Conflicts written before building, and the default each PR follows

1. (11a) The code has two canonical JSON forms. Default: the strict `ledger.canonical`, over values `not_json` has checked, for the index, the coverage report, the freeze hash and LP-20. Sealed plaintext keeps `phi`'s form, since it round-trips.
2. (11a) The prompt does not define the normalised variant. Default: `norm/v1` folds string leaves only and keeps scalars, keys and nesting, so structured inputs do not collide. Its bytes are pinned and it is versioned.
3. (11a) Distinct positives against LP-10's raw count: ai-guard `verdict=block` is 164 raw against 30 distinct. Default: coverage counts distinct positives. `mendari.eval/2` does not change in LP-11. The gap is written down for LP-17 and the owner.
4. (11a) "A CLI over any JSONL file" needs a pack for its questions and critical labels. Default: `mendari data coverage PACK --dataset NAME|FILE`, as `mendari eval` works. A missing language or source is counted `unknown`, never inferred.
5. (11b) The prompt asks for a bootstrap interval on percent agreement. The project shows every proportion as k of n with Clopper-Pearson. Default: Clopper-Pearson for percent agreement. The bootstrap (2,000 resamples, seed 0, percentile) is for kappa and alpha only.
6. (11b) The prompt has no rule for undefined coefficients. Default: such an estimate is `null` with `not_estimable`. Undefined resamples are dropped and counted, and the interval is `not_estimable` if any resample is undefined. Each pair's n is shown, with no minimum.
7. (11b) Derived questions and weights. Default: labellers label member questions only, and derived answers come from `derive_answer`. Agreement is nominal, unweighted (the weights are kept for LP-17) and uses first-pass labels only.
8. (11b) The name `mendari agreement` is taken. Default: rater agreement is `mendari evalset agreement`. There is no command over a free labels file, and no new library.
9. (11c) Copying `phi._key` would read a malformed key as "unset". Default: the `checkpoint_key` pattern, where unset means off and a malformed or half-set key stops startup.
10. (11c, 11e) "Distinct from every PHI key" holds even under PHI mode hash or off. Default: compare against the raw environment values. From 11e on, also compare against the database's `phi_key_known:` marks, and in the other direction against `evalset_key_known:` marks.
11. (11c) Nothing enforces "Production only". Default: refuse to start when `VERCEL` is set, `VERCEL_ENV != production` and the keys are set. The root `app.py` sets `VERCEL=1`, so it refuses locally too; locally, use `mendari serve`.
12. (11c, 11d) Open mode holds every permission, and under PHI mode off unhandled error messages are printed. Default: the keys refuse a server without sign-in. Sign-in already forces hash or encrypt, so errors stay quiet. `may()` never grants `label`, `grade` or `curate` in open mode.
13. (11c) The owner binds sealed values to kind, set and case. Default: a label also binds its label id (a strict superset of the owner's binding), so two labels of one case cannot be swapped. Set-level rows bind an empty case id. All ids are UUIDs.
14. (11c) Sealing is lifted out of `PhiPolicy`. Default: module-level helpers, so PHI bytes stay identical. Evaluation-set tokens are `v2` from the first seal, and the legacy format is not copied.
15. (11d) The prompt says the roles come "through the existing role mapping". ADR-0008 and LP-12 default #2 keep reserved roles out of claims mode. Default: `labeller` grants `label` in memberships mode only. The person routes are memberships-only, and claims mode does not change.
16. (11d) Making grading a machine role would let an admin mint a grading key. Default: an `api_keys` row of role `grading`, unscoped, for one organization, lasting at most 365 days, minted only by `mendari access grader-key`. Admins may list and revoke it, never rotate it. ADR-0004 is amended. A Backend Services grading client waits for LP-17.
17. (11d) The docs say admin "may do everything". Default: admin holds neither `grade` nor `label`. Set management is a new people-only `curate`, held by admin, and the role table's wording changes.
18. (11d, 11n) `test_invite_page` bans the word "grading" in dashboard sources. Default: the test stays. The UI says "in use" and "evaluation runs", and a grading key's role is shown from server data.
19. (11d) A labeller holding only `label` would boot into `/api/packs`, which needs `discover`. Default: the `labelOnly` mode. Labellers never hold `read` or `discover`, and the review queue's screens are not reused.
20. (11d, 11j) The notice gate covers decide and review only, and `/api/notice` needs `read`. Default: labelling is not gated, and `labelOnly` skips the notice.
21. (11e) Sets could be organization-bound or deployment-wide. Default:
    - Every row carries an org.
    - Set events are administrative, under `evalset:<set id>`, and hold ids, keyed hashes, commitments and counts.
    - Key events are deployment events under `KEYS_CHAIN`.
    - A person is named by user id (`user:<16 hex>`, the same as `users.id` and `Principal.id`), never by e-mail.
    - `hidden_matches` is a deployment-wide read inside the store.
22. (11e) The owner says verify checks sealed rows against their chained hashes, but the prompt chains only labels. Default: each case's input and attributes also get a chained salted commitment (`case_ref`), so verify also catches an altered draft input.
23. (11e) Author ids are meaningful (`rfh-u00`). Default: the store mints UUID4 ids, and the author's id is kept as a sealed attribute.
24. (11e) The public-source rule. Default: `hand_written` only until B20, and `template` must be null. Exact or normalised matches with the bundled reference data and the default-seed generator outputs are refused. Paraphrases cannot be caught this way; that is a residual risk in ADR-0011 and the threat model.
25. (11e) How cases come in. Default: through the command line only, from a file outside the package and the repository. `system` and `feedback` blocks are refused, because feedback exports carry model output.
26. (11e) What the index covers. Default: every case from when it is added, drafts included. An entry is removed only with a removed draft case. Retired sets stay indexed, and the lookup stays in the store.
27. (11e) Stray keys. Default: the marks live in the database. Once rows exist, a writer refuses a ring that holds no key the database knows, and a retired key seals nothing. The documented order (reader first, then swap) still passes.
28. (11e) Verifying sealed rows needs keys. Default: a separate `--evalsets` flag, read-only, marking nothing. Without keys, rows count as `unchecked`. `GET /api/audit/verify` does not change.
29. (11e) `_servers_database` would refuse a database that holds only evaluation-set rows. Default: the evaluation-set commands also probe `evalset_sets`.
30. (11e) PHI retention, erasure and `phi rotate` work on PHI rows. Default: they never touch `evalset_` rows, and a test pins that.
31. (11f) Frozen means immutable, yet rotation rewrites ciphertext. Default: immutability covers the plaintext. Compare-and-set re-seals that change ciphertext only are allowed in every state, and the content hash is rechecked afterwards.
32. (11f) The index key has no ring of read keys. Default: a new index key means an epoch event and a full `reindex` in one transaction. Chained hashes keep their key id, and the case and label commitments carry the binding across epochs.
33. (11f) The backup. Default: `evalset export` writes ciphertext and key ids and is read-only. Restoring uses the whole-database restore from 21m's incident runbook; there is no import command. Both drafts named 21b for the restore, but 21b is the status checks.
34. (11f) Verify, `--check` and the export open or copy sealed rows outside the "every read is chained" rule. Default: these are key-holder operations that print counts only and chain nothing. `rotate` chains its batches.
35. (11g, 11l) The prompt allows reads "through the grading role". Default: no route returns frozen or active content to anyone. `grade` runs in-process and answers aggregates only, and the sweep expects zero marks for every principal, the grading key included. This is stricter than the prompt.
36. (11g) Label values are words from the pack's label list, so planted string marks cannot detect a label leak. Default: test through case references (ids, hashes, inputs), a pinned key allow-list per event kind, no label word in any payload or log, and closed response models.
37. (11g) Route inventories would pass vacuously while the routes are unmounted. Default: every route-inventory fixture turns the feature on and asserts that it is on.
38. (11g, 11k) The set's name is curator metadata in the sealed header. Default: the name is shown only to `curate` holders of its organization and on the key holder's command line, and the sweep allows its mark only there.
39. (11h) The prompt's "every read is chained" is about frozen and active sets. Default: every opening of case input, attributes or labels is chained: a labeller's fetch, sampling, measuring, freezing and grading. Opening the header or the stored aggregates is not chained.
40. (11h) An adjudicator usually sees both labels. Default: blind adjudication. The third labeller works from the input alone through the same route, and the final label is the majority. A three-way split leaves the case unresolved, which blocks freeze until the case is removed.
41. (11h) Separation of duties. Default: no assignment for anyone who holds `curate` in the organization. Agreement is computed only once both first-pass labels are in. It is shown only to `curate` holders and on the command line, never to labellers.
42. (11h) Sampling. Default: a seeded stratified draw within the set, with weight `N_h/n_h`. `evalset.sampled` chains counts and the seed, never stratum names. Freeze refuses unsampled cases unless `--drop-unsampled` removes them.
43. (11h, 11k) A pure-Python bootstrap is too slow to run inside a request. Default: agreement and coverage are computed by `evalset agreement`, `evalset coverage` and freeze, then sealed. Routes serve only the stored aggregates.
44. (11i) The prompt says "signed". The owner says a salted commitment, with any signature waiting for LP-17. Default:
    - Chain the commitment only.
    - Add no signing key, and never use `MENDARI_CHECKPOINT_KEY` for this.
    - The gate's "freeze hash" is the commitment.
    - The unsalted hash never leaves the sealed store.
45. (11i) What the content hash covers. Default: every case field plus the header (pack id, questions and labels, critical labels, limits). It does not include the pack digest, so later pack versions can still be graded.
46. (11i) The audit asked the database to refuse writes, but the DDL cannot hold triggers (Postgres splits it on `;`). Default: refusals happen in the store's exclusive session. The hash is recomputed at activation, at every grading read and at retirement, and verify catches direct edits.
47. (11i) No rotation-rule values exist. Default:
    - Both limits are required at freeze, with no shipped default, and both are in the content hash.
    - Age counts from `frozen_at`.
    - One run is one chained `evalset.graded`.
    - The first grading read activates a frozen set.
    - Retiring is an explicit step and publishes nothing.
48. (11i) A critical label can have fewer than 100 distinct positives. Default: freeze warns and the CLI prints the counts, but freeze does not refuse. The counts stay out of the chain, because they would name label words. LP-17 refuses to publish.
49. (11i) Who freezes and retires. Default: the key holder's command line only, in LP-11.
50. (11j, 11k) Default: every evaluation-set route has an explicit rule before the catch-all. Unknown and forbidden ids answer the same 404. The security log's `ids` field names set ids only.
51. (11l) Today's evaluation path leaks per-case data (`save_report`, the ledger, `missed_cases`, `misclassified`, `dataset_sha256`). Default: a Decision with no store, the closed `mendari.hidden_result/1`, and nothing stored. The chain records the set id and the commitment.
52. (11l) Remote providers would send hidden case text off the server. Default: local providers only.
53. (11m, 11n) The screens could be a tab in Access or a separate page. Default: two views inside the dashboard, `#labelling` and `#evalsets`. There is no standalone page, and no review screen is reused. The three tests that pin the Access tab lists stay unchanged.
54. The prompt names `/api/audit/export`, which does not exist. Default: the sweep covers `/api/audit/export/fhir`, the administrative list and its export, and `mendari audit export [--administrative]`.
55. (11a review; 11g, 11k) The set's coverage and agreement aggregates name stratum and language values, which planted marks would flag. Default: as for the set's name (conflict 38), the sweep allows stratum and language marks only in `curate` responses of the set's organization and in the key holder's command-line output (`evalset coverage`, `evalset show`); nowhere else, and never in a chain payload.

## Owner decisions still open

1. **How labellers reach the hosted site.** Either switch the site to memberships mode, or allow `labeller` in claims mode, which amends ADR-0008. Also: how labellers get past Vercel Authentication. This must be settled before contracting labellers. Nothing before 11j depends on it.
2. **Rotation-rule numbers.** The maximum grading runs and maximum age for each set, or a house default. Freeze requires both.
3. **Three-way adjudication splits.** A fourth label, removing the case (which biases the set), or a clinical lead's call. Also, whether adjudicators should see both labels. Until then, a split blocks freeze.
4. **Remote providers on hidden sets.** Whether Jev or the LLM gateway may grade hidden sets, which sends case text off the server. This ties to LP-17's decision on a private grading runner. Local providers only until then.
5. **The evidence rule in `mendari.eval/2`.** Raw or distinct positives (ai-guard: 164 against 30). Changing it is a format change that makes published cards stale. For LP-17.
6. **Separation of duties in a small organization.** Whether an administrator may also label. The default is no.
7. **Erasing a hidden case.** No erasure path exists. One is needed before B20 brings de-identified cases.

Owner actions that are not decisions:
- Before any real set, generate the two keys, keep a copy off the laptop, and set them as Production-only Secrets.
- Mint a grading key only when LP-17 needs one.

## Risks

- **The catch-all rule.** `("GET", r"^/api/", "read")` gives every viewer and every key any GET route that lacks its own rule. Every evaluation-set route's permission is pinned in `test_auth`, together with a test that none resolves to `read` or `discover`.
- **A vacuous sweep.** If a fixture lacks the keys, the evaluation-set routes are not mounted and every sweep passes on nothing. The feature-on assertions guard this.
- **Test time.** A full sweep of routes × principals × backends would be slow (the suite is about 6 minutes). The content sweep runs on SQLite in memberships mode only. Postgres is covered by the `audit verify` tests.
- **Offline decryption.** The key holder can decrypt offline. "Only grading reads active sets" is enforced by the software, not by cryptography.
- **Preview copies.** Preview copies of the database hold ciphertext. A Preview server with the feature off must start cleanly with `evalset_` tables present.
- **Error paths.**
  - `MendariError` messages are returned verbatim.
  - A 422's `msg` reaches the caller.
  - `record_problems` names case ids.
  - `--debug` prints tracebacks.
  - Every evaluation-set message must name only ids, line numbers and JSON paths.
- **Administrative events.** They let an admin link one case's events by its hash, and they show which cases were disputed. This is documented in the threat model. The chain also records labeller user ids permanently.
- **Paraphrases.** Paraphrases of public templates pass the equality check. Only `template: null`, review and the threat-model note cover them.
- **LP-20 drift.** LP-20 must call the same `canonical_input` and `normalised_input`, or hidden cases will pass its check. `norm/v1` must be settled before any real index is built, because a later change means reindexing from sealed inputs.
- **Literal event names.** `recorded_kinds()` requires a literal kind at every `_append` call. A helper that passes the kind as a variable fails the tests.
- **The Postgres DDL.** A `;` inside new DDL breaks schema setup on Postgres only.
- **Developer shells.** A shell holding real keys would turn the feature on in tests unless `LIVE_SETTINGS` and the autouse fixture remove them.
- **Dashboard imports.** A multi-line import is never stamped into the import map. The labelling screen must also keep nothing in storage.
- **The legacy-key path.** It honours any role stored on an `api_keys` row, so a database writer could insert a grading row. This is accepted, because such a writer already holds the keys. `key.created` shows every minted key.
- **Hand-offs to LP-17 and LP-20.**
  - LP-17's per-case split must be derived from the sealed freeze salt, never from the index key.
  - The frozen header must carry everything LP-17 weighs: pack id, label vocabulary, critical labels, strata and weights, and limits.
  - LP-20's upload check is a one-bit oracle. It needs a per-organization budget and an operator-only match report.
- **Plan length.** Fifteen PRs give room for drift. Each PR updates the todo entry, and the conflicts above are the defaults unless the owner changes them.

Checked against main (2536ec1c):
- The next free ids are ADR-0011 and threat-model rows I13, E11 and T6.
- `users.id` is `person_id`, the same as `Principal.id`.
- The wheel ships `packs/` and `datasets/` as `mendari/_packs` and `mendari/_datasets`.
- The root `app.py` sets `VERCEL`.
- The ai-guard counts were reproduced: 600 rows, 81 distinct inputs, and `verdict=block` at 164 raw against 30 distinct.
- Five fixes to the two drafts:
  - The database restore is 21m, not 21b.
  - Plan A's sweep planted the set name and stratum, while its own routes and events returned or chained them (conflicts 38 and 42).
  - Plan B chained the positives per critical label at freeze, which names label words.
  - Plan B sampled across sets, which would have re-sealed and re-hashed the moved cases.
  - Plan B's `POST …/agreement` ran the bootstrap inside a request.
