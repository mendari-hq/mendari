<!-- Planned on 2026-11-10 against main ca7e016b (LP-19a, 19b and 19h merged; 19c in another
session). Three readers mapped the code read-only: pack loading, origins and the two approvals;
validation, datasets, the statistics, the ledger, the hidden-case index and the PHI heuristics;
roles, the guard, the audit chain, the dashboard, the gates and the threat model. The lines this
plan cites were then read again. Nothing was built or run but the three checks the plan PR names.
Where the build prompt, an earlier owner decision or an earlier plan disagrees with the code, the
conflict and the default each PR follows are written below. tasks/todo.md tracks progress. -->

# LP-20 build plan: the pack store (A7)

> **Numbers this plan takes** (free at ca7e016b; other lanes are open, so each PR re-checks them
> before it writes anything, and `tests/test_threat_model.py` checks the order): **ADR-0016**.
> Threat-model rows **S24, S25, T12, T13, R7, I21, I22, I23, D16, D17, E18, E19**. The last rows
> on main are S22, T10, R6, I18, D14 and E16; `notes/lp19-plan.md` holds S22, S23, T11, R6, I19,
> I20, D15 and E17 for LP-19, so LP-20 starts after them. A row LP-20 ends up not needing is
> given back in 20k.

## Summary

LP-20 lets the people of one organization create, improve and test packs on the server, and
decide by a recorded sign-off which version their organization serves. Build prompt:
`notes/vision-audit.md:2452-2508`. Gate: `a7-pack-store` in G3-4 (`launch/gates.yaml:290-294`).

Today the server reads packs from the file system once, at start-up, into one dict every
organization shares (`src/mendari/server/app.py:968-980`). There is no store, no author
permission, no upload, no dataset check, no PHI detector over uploads and no promotion.

LP-20 delivers:
- **One validation function** in the library, called by `mendari validate`, the community check,
  the registry's publish and the store. Today the registry's publish loads a pack and never runs
  the validator the command line runs (`src/mendari/hub/registry.py:611-617`;
  `src/mendari/evals/pack_tests.py:116-143`).
- **Dataset checks** as pure functions, shipped first as `mendari data check PACK [FILE]` and in
  the packs workflow.
- **A deterministic PHI detector** over uploads and archive contents. It reports the kind and
  the place, never the value.
- **A hidden-set check** with LP-11's keyed index (`src/mendari/core/evalsets.py:2210-2221`). A
  match is held without a reason and reported to the operator only.
- **A store per organization in the database**, keyed by content digest: versions that never
  change, a new origin `store` that runs on the bounded engine
  (`src/mendari/core/origin.py:56-60`), and uploaded cards and benchmarks thrown away and
  rendered again by the server.
- **The `pack_author` role** given its permission (it grants nothing today,
  `src/mendari/core/accounts.py:11-14`, `:38`). Authors upload a signed archive or copy a
  built-in pack, edit, validate and test a draft.
- **Evaluation as a stored job with a ledger**, and a draft compared with the current version on
  the same cases with the statistics LP-10 and LP-17 already ship
  (`src/mendari/evals/paired.py:120-128`). LP-20 adds no new statistic.
- **Promotion**: its own chained record, bound to a digest, needed at every risk level, signed
  off by someone other than the author, naming the evaluation reports it relied on. It is not
  the automation approval (`src/mendari/core/audit.py:2888-2896`).
- **An improvement dataset** from reviewers' corrections, each label with where it came from,
  kept in the organization, and offered to a draft as proposed behaviour tests.
- **Dashboard screens** for upload, results, comparison and promotion.

It is built in **11 PRs, 20a to 20k**.

Order of work:
- 20a first.
- **In parallel once 20a is merged:** 20b (the one validation function), 20c (dataset checks) and
  20d (the PHI detector). They share no file except `src/mendari/cli.py`, where each adds its own
  subcommand.
- 20e (the store, as a library with no route) needs 20b and 20d.
- 20f (the author role and the upload and copy routes) needs 20e and 20c.
- **In parallel once 20f is merged:** 20g (evaluation jobs and the comparison) and 20i (the
  improvement dataset).
- 20h (promotion and serving) needs 20g.
- 20j (the dashboard) needs 20f to 20i. Its first screens can be built beside 20h.
- 20k closes LP-20.

What LP-20 needs from LP-19 (`notes/lp19-plan.md`):
- **Nothing in LP-19 blocks 20b to 20e's design**, but three files are shared, so the merge order
  matters (the table below).
- **20b needs 19c merged first** (19c is being built now, and both change `publish` in
  `src/mendari/hub/registry.py`).
- **20f needs 19d and 19e merged first** if they are open when it starts: all three add rules to
  `_RULES` and 19e and 20f both change the reserved roles (`src/mendari/core/accounts.py:35-39`,
  `src/mendari/server/auth.py:1045`). If LP-19 is still at 19c when 20f is ready, 20f goes first
  and 19e rebases; the changes are a few lines each.
- **20f's namespace rule reads LP-19c's claims** when the registry is mounted (conflict 6); with
  no registry it does not need them.
- **20j needs 19f merged first** if 19f is open (both add tabs and modules to the dashboard).
- Sending a promoted version to a registry is not in LP-20 (see "Left out, on purpose").

What LP-17l needs from LP-20 (`notes/lp17-plan.md:605-633`): a store version it can load and
grade for one organization and one lineage. That is 20e's `PackStore.load` and 20f's author and
lineage records. **17l can start once 20f is merged**; it does not wait for promotion, the
dashboard or the close-out.

Where PRs collide, merge in this order:

| File | Merge order |
|---|---|
| `src/mendari/hub/registry.py` | 19c, then 20b (a few lines in `publish`), then 19i, 19d, 19e, 19g |
| `src/mendari/server/auth.py` (`_RULES`, the roles) | 19d, 19e, then 20f, 20g, 20h, 20i |
| `src/mendari/core/accounts.py` (`GRANTING_RESERVED`) | 19e, then 20f |
| `src/mendari/cli.py` | 20b, 20c, 20d in any order (separate subcommands), then 20e |
| `src/mendari/core/audit.py` (`_TABLES`, the kinds) | 20e, 20f, 20g, 20h, 20i |
| `src/mendari/server/app.py` | 20f, 20g, 20h (the only one that changes how a pack is picked) |
| `tests/test_shipped.py` (`CHAINED`) | 19d, 19e, 19g, then 20f, 20g, 20h, 20i |
| `tests/test_two_organizations.py` | 20f, 20g, 20h, 20i |
| `src/mendari/dashboard/` | 19f, then 20j |

These stay off until the owner acts:
- With `MENDARI_PACK_STORE` unset, the engine mounts no store route, creates no store row and
  serves packs exactly as today. The store's tables exist but stay empty (the audit store makes
  every table up front, `src/mendari/core/audit.py:244`).
- The store needs memberships mode. In claims mode and open mode it is refused at start-up with
  the reason, as the evaluation-set keys are without sign-in
  (`src/mendari/core/evalset_keys.py:191-216`).
- With no hidden-case index key, the hidden-set check says "not applicable" (conflict 9).
- Production gets none of these settings from a build PR.

What LP-20 does not change:
- What a valid pack decides, any reference pack's digest, any policy. No reference pack is
  rebuilt.
- The risk caps (ADR-0002) and the automation approval at levels 2 and 3. Promotion never lets a
  pack automate above them: a promoted level 2 or 3 version still needs
  `approve_automation` for its digest before it automates.
- The engine a pack the deployment does not own runs on (ADR-0009). A store pack is one of them.
- The archive format (`src/mendari/hub/archive.py:49-60`) and the registry's own checks.
- The audit chain's rules. Every store write is one chained event that names its organization
  (`src/mendari/core/audit.py:1801-1804`), and `mendari audit verify` passes after each.

Figures: every proportion the store shows is k of n with its exact interval, every comparison is
paired on the same cases, and a figure on too few cases reads "insufficient evidence"
(Definition of shipped 3, `notes/vision-audit.md:1328-1333`). They come from
`src/mendari/evals/stats.py` and `src/mendari/evals/paired.py`, through `figures.counted`
(`src/mendari/evals/figures.py:59-64`); no number is formatted by hand.

Every PR:
- passes the Definition of shipped (`notes/vision-audit.md:1310`);
- runs its tests on SQLite and on Postgres (the `db_target` fixture, `tests/conftest.py:254-272`),
  with warnings as errors;
- updates `README.md`, `CHANGELOG.md`, the LP-20 entry in `tasks/todo.md` and the docs it names;
- puts the SPDX line on new source files and signs off every commit (`git commit -s`);
- uses synthetic data only (`mendari.sandbox.synthetic_*`), and makes its test keys at runtime;
- writes no production hostname, project id or deployment setting in the tree
  (`tests/test_public_tree.py`).

## What the code is today (the facts the plan rests on)

1. **Packs are read once and shared.** `create_app` calls `discover(packs_paths)` at start-up,
   keeps the newest version of each id in `installed`, and serves `packs`, a dict closed over by
   every route (`src/mendari/server/app.py:968-980`). Each served pack is built once at start-up
   and "never in a request" (`:995-1000`). `pack_or_404` reads that dict (`:1003-1007`). Nothing
   filters packs by organization: `/api/packs` lists `packs.values()` (`:1561`).
2. **A pack is a directory.** `Pack.root` is a `Path` (`src/mendari/core/pack.py:75-95`), and
   `load_pack(path, *, origin)` hashes the files it then reads (`:248-415`). The digest leaves
   out `evals/` and `DECISION_CARD.md` (`:49-52`). On Vercel there is no persistent disk
   (`src/mendari/server/vercel.py:5-7`). So a pack kept in the database must be written to a
   scratch directory before it loads.
3. **Origins.** `Origin` is `maintained`, `community`, `hub` or `local`, and `NOT_OWNED` is
   `{"hub"}` with the comment "LP-20 adds `store`" (`src/mendari/core/origin.py:56-60`).
   `bounded` puts a pack of a not-owned origin on the bounded engine unless its digest is a
   reference pack's (`:143-153`). `_shadow_problem` already refuses a not-owned pack that takes
   a reference pack's id and version with other files, and names the store
   (`src/mendari/core/pack.py:436-446`).
4. **Two approvals exist, and neither is promotion.**
   - The automation approval: `approve_automation` takes levels 2 and 3 only, per organization
     and digest, with an expiry, chained `governance.approved`
     (`src/mendari/core/audit.py:2888-2925`); the runtime reads it through `approved_override`
     (`src/mendari/server/app.py:1153-1158`).
   - The registry approval: per organization and digest, for a pulled pack
     (`src/mendari/core/audit.py:3063-3094`; `src/mendari/server/app.py:1850-1875`); the runtime
     holds an unapproved `hub` pack for a person (`src/mendari/core/decision.py:315-333`).
5. **Validation is in three places.** `load_pack` checks structure, patterns and caps
   (`src/mendari/core/pack.py:248-415`). `validate_pack` adds the provider builds, the FHIR
   mapping, the behaviour tests' inputs and the declared datasets
   (`src/mendari/evals/pack_tests.py:116-143`); its callers are `cmd_validate`
   (`src/mendari/cli.py:128-134`) and the community check (`src/mendari/community/check.py:877`).
   The registry's publish verifies the archive, extracts it (which calls `load_pack`), describes
   it and runs the regression check, and does not call `validate_pack`
   (`src/mendari/hub/registry.py:595-626`).
6. **Datasets.** `load_dataset` checks the encoding, JSON per line and an `input` object per
   record (`src/mendari/evals/harness.py:57-100`); `record_problems` checks ids, labels and
   attributes (`:103-138`). `src/mendari/evals/dataset_checks.py` does not exist. `mendari data`
   has one subcommand, `coverage` (`src/mendari/cli.py:2773-2785`), which already counts
   duplicates under both canonical forms (`src/mendari/evals/coverage.py:44-47`). The packs
   workflow runs `validate`, `test`, `card` and `eval` (`.github/workflows/packs.yml:58-63`).
7. **The canonical forms and the hidden-case index are built** (LP-11).
   `canonical_input` and `normalised_input` (`norm/v1`) say they are the forms the store's
   upload check must use (`src/mendari/evals/canonical.py:1-21`, `:42-45`, `:63-86`).
   `EvalsetStore.hidden_matches(value)` returns the set and case ids, of every organization's
   sets, that equal a value under either form, and "no route or command answers it"
   (`src/mendari/core/evalsets.py:2210-2221`). The index is keyed by
   `MENDARI_EVALSET_INDEX_KEY` (`src/mendari/core/evalset_keys.py:41-43`, `:111-121`), and the
   evaluation-set routes are mounted only with the keys (`src/mendari/server/app.py:3224-3229`).
8. **The statistics are built** (LP-10, LP-17b). Clopper-Pearson, exact McNemar, Holm and the
   paired bootstrap are in `src/mendari/evals/stats.py` (`:232-247`, `:255-274`, `:282-302`,
   `:392-417`). `paired.compare(a, b, *, dataset_sha256, seed, resamples, alpha)` compares two
   ledgers on the same cases and lists the cases that changed by ledger hash
   (`src/mendari/evals/paired.py:120-224`). The macro-F1 interval is a stratified bootstrap in
   the scoreboard (`src/mendari/evals/scoreboard.py:556-596`), and the Brier interval is in
   `src/mendari/evals/result.py:57-61`.
9. **An evaluation runs inside one request.** `POST /api/packs/{namespace}/{name}/evals` runs
   and answers in the request (`src/mendari/server/app.py:1951-2011`), caps a remote arm at 200
   cases so it "must fit one function invocation" (`:366-368`), and saves a report with its
   ledger in `eval_reports` (`src/mendari/core/audit.py:280-288`, `:1580-1619`), chained
   `eval.recorded`. There is no job table, queue or worker; the only scheduled work is three
   cron routes (`vercel.json:8-12`).
10. **PHI.** `src/mendari/core/phi.py` is the storage policy (off, hash, encrypt); it detects
    nothing. The heuristics that exist are the community check's
    (`src/mendari/community/phi_scan.py:24-57`, `scan` at `:68-86`): Social Security numbers,
    phones, record numbers, a date of birth next to a name, and e-mail addresses. They are
    warnings only, they use Python's `re`, they have no street address and no name list.
11. **Roles.** `pack_author` is a reserved membership role that grants nothing
    (`src/mendari/core/accounts.py:35-39`); `Auth._member` keeps only the server's roles and the
    granting reserved ones (`src/mendari/server/auth.py:1043-1046`); `PERMISSIONS` has no entry
    for it (`:158-183`). `governance` holds `approve` and `admin` holds everything a person can
    (`:158-183`). People-only permissions are `keys`, `curate` and `label` (`:187`). A route no
    rule lists needs `admin` (`:684-692`), and new wildcard rules go before the catch-all read
    (`:337`).
12. **The chain.** `_append` refuses an event that does not name its organization
    (`src/mendari/core/audit.py:1801-1804`). A new kind is listed in `ADMINISTRATIVE_KINDS`
    (`:169-211`) with its table in `SUBJECT_TABLES` (`:155`), and a new table goes in `_TABLES`
    (`:244-598`). The audit store keeps no binary column today; the registry does
    (`hub_archives.data`, `src/mendari/hub/registry.py:139-142`), through `{blob}`
    (`src/mendari/core/db.py:105`, `:246`).
13. **Every write route is in an inventory.** `tests/test_shipped.py` lists each state-changing
    route with the event it chains (`CHAINED`, `:166-234`), and `NOT_CHAINED_YET` must stay
    empty (`:274`). `tests/test_two_organizations.py` calls every mounted route as the other
    organization and fails on a route no call covers (`:2206-2216`).
14. **Body size.** Every request body is capped at 4 MiB by default
    (`src/mendari/settings.py:111-122`; `BodyCap`, `src/mendari/server/app.py:3884-3926`), under
    the platform's own limit. An archive may be 100 MB (`src/mendari/hub/archive.py:52`).
    LP-19 left larger archives to LP-20 (`notes/lp19-plan.md:606-610`, `:677-678`).
15. **Budgets.** A bounded pack's rules stop at 1 s of CPU per decision
    (`src/mendari/core/patterns.py:62-65`, `:73-82`). An organization's daily quotas are
    `decisions`, `jev`, `llm`, `eval_jev` and `eval_llm` (`src/mendari/settings.py:269`,
    `:272-292`); nothing counts uploads, stored bytes or evaluation jobs. Rate limits are chosen
    by a route's permission, not declared on the route
    (`src/mendari/server/limits.py:326-347`).
16. **Corrections.** A reviewer's correction is on the decision's row (`final_label`,
    `human_override`, `reviewer`, `resolved_at`), written by `resolve_review`
    (`src/mendari/core/audit.py:1323-1372`). The export that exists reads thumbs-up and
    thumbs-down ratings only (`src/mendari/evals/feedback.py:17-65`); nothing exports
    corrections.
17. **Cards.** The registry already throws away an uploaded card, benchmark and leaderboard and
    renders its own (`UPLOADED`, `src/mendari/hub/registry.py:105`; `describe`, `:1354-1406`).
    The store does the same with `render_card` (`src/mendari/core/card.py:88-105`).
18. **Three reference packs cannot run on the bounded engine yet.** The trial pack and the
    referral pack still hold lookahead patterns
    (`packs/clinical-trial-prescreener/logic/note_rules.yaml:52`, `:82`;
    `packs/referral-router/logic/triage_rules.yaml:82`), which RE2 refuses. Their rewrite
    (16c-2) is open and the owner's choice between its two forms is open
    (`tasks/todo.md:587-595`). See conflict 5.
19. **The dashboard.** Views register themselves in `views`
    (`src/mendari/dashboard/router.js:10`), the navigation is in
    `src/mendari/dashboard/index.html:107-128`, the
    Evaluations view has four tabs (`src/mendari/dashboard/evals.js:12-17`), and a view learns
    what the person may do from `/api/auth/me` (`src/mendari/server/auth.py:1544-1561`;
    `may`, `src/mendari/dashboard/ui.js:335`). Browser tests already run a second organization
    (`tests/browser/test_audit_activity.py:74-76`).

## Pull requests, in order

- [ ] **20a** The plan, ADR-0016 (Proposed), and the conflicts written to `tasks/todo.md`.
  - **What it builds:**
    - `notes/lp20-plan.md` (this plan).
    - `docs/decisions/ADR-0016-an-agent-store-per-organization.md` (Proposed; the file was
      named `ADR-0016-a-pack-store-per-organization.md` until 2026-11-23).
    - The LP-20 entry in `tasks/todo.md`, with the steps, the conflicts and the owner decisions,
      written before any build, as the prompt requires (`notes/vision-audit.md:2465`).
  - **Files:** new: the plan, the ADR. Changed: `mkdocs.yml` (the ADR's nav entry, after
    ADR-0015's at `mkdocs.yml:100`), `notes/README.md`, `tasks/todo.md`.
  - **Tests:** none new; `tests/test_threat_model.py`, `scripts/check_gates.py` and the docs
    build stay green.
  - **Safe alone because:** documents only.

- [ ] **20b** One validation function (requirement 1). Parallel with 20c and 20d. Needs 19c
  merged.
  - **What it builds:**
    - `src/mendari/evals/validation.py`: `problems(pack) -> list[Problem]` and
      `validate(pack) -> None`. It is today's `validate_pack` (fact 5) moved, with each problem
      given a kind and a place instead of a bare line, so the store and the dashboard can show
      it. `validate_pack` stays as a name that calls it.
    - Callers: `cmd_validate`, the community check, the registry's `publish` (after
      `archive.extract`, inside the same `try`, so a problem is a refused publish that names
      it), and from 20e the store.
    - A pack the deployment does not own is validated inside the CPU budget
      (`src/mendari/core/patterns.py:73-82`), as `input_problems` already does for its own part.
  - **Files:** `src/mendari/evals/validation.py` (new), `src/mendari/evals/pack_tests.py`,
    `src/mendari/cli.py`, `src/mendari/community/check.py`, `src/mendari/hub/registry.py`
    (`publish` only), `docs/guides/authoring-a-pack.md`, `docs/operations/registry.md`. New:
    `tests/test_validation.py`.
  - **Tests** (`tests/test_validation.py`):
    - `::test_the_command_the_community_check_and_the_registry_call_one_function` (each caller
      is given a pack with one planted problem and reports the same kind and place);
    - `::test_every_reference_pack_is_valid` (the five shipped packs, unchanged);
    - `::test_a_publish_the_validator_refuses_stores_nothing_and_names_the_problem`;
    - `::test_a_valid_publish_is_stored_as_before` (the stored metadata is equal to main's for
      the same archive);
    - `::test_a_not_owned_pack_is_validated_inside_the_budget`;
    - `::test_a_problem_names_a_file_and_a_place_and_never_case_text`.
  - **Safe alone because:** the command line and the community check answer as before. The
    registry becomes stricter (conflict 1): a publish that loads but does not validate is now
    refused. No hosted registry exists (`docs/operations/registry.md:11`).
  - **Needs the owner's yes:** the registry refusing what it took before (decision 1).

- [ ] **20c** Dataset checks and `mendari data check` (requirement 2). Parallel with 20b and 20d.
  - **What it builds:**
    - `src/mendari/evals/dataset_checks.py`, pure functions over a pack's manifest and records,
      no file or database access:
      - `schema_problems`: each case's input against the pack's input schema, named by case id;
      - `label_problems`: every label is one of the declared answers of its question;
      - `label_counts`: per label, n and its exact interval as a share of the set, and the mark
        "too few to estimate" under the method's floor instead of a refusal;
      - `duplicates`: groups of cases equal under `canonical_input`, and under
        `normalised_input` (`norm/v1`), using LP-11's functions and no copy of them (fact 7);
      - `training_overlap`: cases equal, under either form, to a record of the pack's own
        training data;
      - `check(pack, dataset) -> Report`, a JSON report `mendari.dataset-check/1` that holds
        ids, counts and intervals, never an input.
    - `mendari data check PACK [FILE] [--json]`: every declared dataset, or one file. Exit 1 on
      a schema or label problem; duplicates, overlap and thin labels are reported and do not
      fail (conflict 2).
    - `.github/workflows/packs.yml`: `mendari data check "$dir"` after `mendari validate`, for
      maintained and community packs.
  - **Files:** `src/mendari/evals/dataset_checks.py` (new), `src/mendari/cli.py`,
    `.github/workflows/packs.yml`, `docs/guides/authoring-a-pack.md`. New:
    `tests/test_dataset_checks.py`.
  - **Tests** (`tests/test_dataset_checks.py`):
    - `::test_a_case_that_breaks_the_schema_is_named_by_id_and_never_by_text`;
    - `::test_a_label_outside_the_declared_answers_is_a_problem`;
    - `::test_each_label_has_its_count_and_exact_interval` (against a value computed with scipy);
    - `::test_a_thin_label_is_marked_too_few_and_is_not_refused`;
    - `::test_duplicates_are_found_exactly_and_under_norm_v1` (the pinned vectors of
      `tests/test_canonical.py`);
    - `::test_the_checks_use_lp11s_canonical_functions` (the module imports them; no second
      implementation);
    - `::test_overlap_with_the_packs_training_data_is_found`;
    - `::test_every_shipped_dataset_is_checked_and_the_report_holds_no_input`;
    - `::test_the_command_exits_1_on_a_schema_problem_and_0_on_duplicates`.
  - **Safe alone because:** a new command and a new workflow step. The step can fail a pack PR
    only on a schema or label problem, which `mendari validate` already refuses
    (`src/mendari/evals/pack_tests.py:150-173`).

- [ ] **20d** A deterministic PHI detector (requirement 3). Parallel with 20b and 20c.
  - **What it builds:**
    - `src/mendari/core/phi_detect.py`: `detect(text) -> list[Finding]` and
      `detect_files(files) -> Report`. A finding is a kind, a file and a line; a report
      `mendari.phi-check/1` holds findings and counts per kind, **never the matched text**.
    - Kinds: Social Security number, phone, record-number-like number, e-mail address, date of
      birth, street address (new), and a name from a list (new). The first five move from
      `src/mendari/community/phi_scan.py` (fact 10), which keeps its name and calls the new
      module, so the community check's warnings do not change.
    - Every pattern is compiled with RE2 (`src/mendari/core/patterns.py:170`), because the
      detector reads hostile uploads. A pattern that RE2 cannot hold is rewritten, and a test
      pins that the two engines agree on every shipped dataset.
    - `src/mendari/core/phi_names.txt`: the name list, shipped in the wheel (decision 3 and open
      point 2). A name alone is never a finding: it counts only on a line that also holds
      another kind, or a word that says it is a person's (conflict 3).
    - The verdict: `rejects(report) -> bool`, true when any structural kind is found at all, or
      name findings pass a threshold written in the module with the measurement that set it.
    - `mendari data check` (20c) gains `--phi`, and the store calls it from 20f.
  - **Files:** `src/mendari/core/phi_detect.py` (new), `src/mendari/core/phi_names.txt` (new),
    `src/mendari/community/phi_scan.py`, `src/mendari/cli.py`, `pyproject.toml` (the data file),
    `NOTICE` and the licence inventory LP-14 keeps (`licenses/`), `docs/security/threat-model.md`
    (I21).
    New: `tests/test_phi_detect.py`.
  - **Tests** (`tests/test_phi_detect.py`):
    - `::test_each_kind_is_found_with_its_file_and_line` (parametrized over the seven kinds);
    - `::test_no_report_log_or_error_holds_a_matched_value` (the planted values are searched
      for in the report, the logs and the exception text);
    - `::test_every_shipped_synthetic_dataset_and_pack_file_passes` (the prompt's rule);
    - `::test_a_name_alone_is_not_a_finding_and_a_name_beside_a_birth_date_is`;
    - `::test_the_fictional_phone_range_and_example_domains_pass`;
    - `::test_a_long_hostile_line_costs_one_pass` (a bound on CPU time, on RE2);
    - `::test_the_same_bytes_give_the_same_report`;
    - `::test_the_community_checks_warnings_are_unchanged`.
  - **Safe alone because:** a library and a flag. Nothing is refused by it until 20f calls it.
  - **Needs the owner's yes:** the source of the name list (decision 3).
  - **Threat model:** I21.

- [ ] **20e** The store, as a library with no route (requirement 5).
  - **What it builds:**
    - `src/mendari/core/pack_store.py`: `PackStore(audit_store)`, holding its rows in the
      engine's database, as `EvalsetStore` does (`src/mendari/core/evalsets.py:409`).
    - Tables, in `_TABLES`:
      - `store_files (sha256 PRIMARY KEY, size, data {blob})`: each file once, whichever
        version or organization holds it;
      - `store_versions (org, digest, pack_id, version, files, parent_digest, source,
        created_by, created_at, state, state_at, PRIMARY KEY (org, digest))`. `files` is the
        sorted list of path, sha256 and size. `source` is `upload`, `copy` or `edit`. `state`
        is `draft`, `held`, `promoted`, `retired` or `withdrawn`;
      - `store_usage (org, day, uploads, bytes)`, outside the chain, as `rate_limits` is.
    - **A version never changes.** Its row's files, id and digest are written once; only
      `state` moves, each move one chained event. An edit is a new version with a parent.
    - `PackStore.put(org, files, *, by, source, parent)`: writes the files to a scratch
      directory, loads them with `load_pack(origin="store")`, and requires the digest
      `load_pack` hashed to equal the row's key (the engine follows the bytes, ADR-0009). It
      drops `evals/` and `DECISION_CARD.md` from what it keeps (they are outside the digest,
      fact 2), so no uploaded card or benchmark is ever stored. It runs 20b's `validate` and
      records the result with the version.
    - `PackStore.load(org, digest) -> Pack`: the files written to a scratch directory of this
      process, checked against the digest, loaded as `store`. Cached per process by digest,
      bounded in count and bytes, so a serverless instance with no persistent disk rebuilds it
      from the database (fact 2).
    - `PackStore.card(org, digest)`: rendered by the server from the pack and the store's own
      reports (fact 17), never from an uploaded file.
    - `Origin` gains `"store"`, `NOT_OWNED` becomes `{"hub", "store"}` (fact 3), and
      `ORIGIN_LABELS` gains "From this organization's store". So a store pack runs on the
      bounded engine, its provider settings are capped as a pulled pack's are
      (`src/mendari/core/origin.py:84-95`), and `_shadow_problem` covers it.
    - **The residuals LP-16 left for LP-20** (`tasks/todo.md:762-767`;
      `docs/security/threat-model.md:215-232`): for a `store` pack the recorded cost uses the
      deployment's price and never the pack's, the pack cannot turn batching off or set a
      temperature, and unscoped provider settings and Jev defaults do not reach it by id, each
      held to what a community pack is held to today. `hub` packs are not changed (that would
      change their decisions). See open point 4.
    - Quotas: `MENDARI_ORG_DAILY_LIMITS` gains `store_uploads`, and a new
      `MENDARI_STORE_ORG_BYTES` bounds what one organization keeps (default 256 MiB). Per
      version: the archive's own limits (`src/mendari/hub/archive.py:52-55`).
    - Chained kinds, each naming `org`, the digest and the actor's id: `pack_version.created`,
      `pack_version.held`, `pack_version.withdrawn`. Listed in `ADMINISTRATIVE_KINDS` and
      `SUBJECT_TABLES`, so `verify` checks each row against its newest event.
    - The prerequisites, as the prompt asks ("checked by a test at start, not by reading"):
      `pack_store.prerequisites() -> list[str]` returns what is missing of LP-16 (a `store`
      pack is bounded and its rules stop at the budget), LP-12 (memberships mode), LP-10 (the
      ledger and the statistics import and answer a pinned value) and LP-11 (the canonical
      forms answer their pinned vectors). 20f calls it at start-up before it mounts a route.
    - Operator commands on the database: `mendari store versions --db … --org …`,
      `mendari store verify --db …` (every stored file hashes to its key and every version to
      its digest), `mendari store withdraw --db … --org … DIGEST --reason …`.
  - **Files:** `src/mendari/core/pack_store.py` (new), `src/mendari/core/audit.py`,
    `src/mendari/core/origin.py`, `src/mendari/core/pack.py` (the docstrings that name the
    store), `src/mendari/providers/` where the four residuals are applied,
    `src/mendari/settings.py`, `src/mendari/cli.py`, `docs/operations/pack-store.md` (new),
    `docs/security/threat-model.md` (T12, D16; the Pack content part's `store` sentence at
    `:186-189`). New: `tests/test_pack_store.py`, `tests/test_pack_store_prerequisites.py`.
  - **Tests** (`tests/test_pack_store.py` unless named):
    - `tests/test_pack_store_prerequisites.py::test_the_stores_prerequisites_are_met` (LP-10,
      LP-11, LP-12 and LP-16 by behaviour, each with its own assertion);
    - `tests/test_pack_store_prerequisites.py::test_a_missing_prerequisite_is_named`
      (each one removed in turn);
    - `::test_a_version_is_keyed_by_the_digest_the_loader_hashed_and_never_one_it_is_told`;
    - `::test_a_stored_version_never_changes` (a second `put` of other files under the same
      digest is refused; an update past the API fails `audit verify`);
    - `::test_an_uploaded_card_and_benchmark_are_not_stored_and_the_card_is_rendered_here`
      (the prompt's "Do not store an uploaded card or benchmark");
    - `::test_a_store_pack_runs_on_the_bounded_engine_and_its_rules_stop_at_the_budget`;
    - `::test_a_store_pack_never_takes_a_reference_packs_id_and_version`;
    - `::test_a_store_pack_does_not_set_its_own_cost_batching_or_temperature`;
    - `::test_a_version_of_one_organization_is_not_loaded_for_another`;
    - `::test_the_same_file_is_kept_once_and_removed_with_its_last_version`;
    - `::test_an_organization_over_its_bytes_or_its_daily_uploads_is_refused`;
    - `::test_a_fresh_process_loads_a_version_from_the_database_alone`;
    - `::test_audit_verify_passes_after_every_store_event` (the prompt's test, extended by each
      later PR);
    - `::test_a_database_from_before_the_store_opens_and_verifies`.
  - **Safe alone because:** no route, no setting read by the server, no caller. A database from
    before gains empty tables.
  - **Threat model:** T12 (a stored version changed in place), D16 (storage and scratch-disk
    exhaustion).

- [ ] **20f** The author role, and the upload and copy routes with their checks (requirements
  3, 4 and 6). **This PR gets one adversarial security review.**
  - **What it builds:**
    - `MENDARI_PACK_STORE` (`on`; unset or `off`: nothing mounted), read in `create_app` beside
      the other start-up settings. On, the server stops at start-up, naming the reason, when it
      is not in memberships mode or `pack_store.prerequisites()` is not empty (the prompt's
      "Do not open any upload route if the prerequisite test fails").
    - `pack_author` grants a new people-only permission `author`
      (`GRANTING_RESERVED`, `PERMISSIONS`, `PEOPLE_ONLY`, `NEVER_OPEN`). `admin` does not hold
      it by default (decision 4): an administrator who writes packs gives themself the role,
      and it shows.
    - Author keys: `store_keys (org, key_id, public_pem, added_by, added_at, revoked_at)`. An
      administrator (`keys`) registers an author's public key; an upload must be signed by a
      key of the caller's organization that is not revoked (conflict 7).
    - New module `src/mendari/server/store_routes.py`, people only, acting for the
      organization the guard let through (`org_of`, `src/mendari/server/auth.py:544-550`),
      never one a body names. Rule: `("*", r"^/api/store(/|$)", "author")`, with the narrower
      rules of 20g, 20h and 20i before it.
      - `GET /api/store/packs`, `GET /api/store/packs/{namespace}/{name}`: this organization's
        lineages and versions, their state, validation result and server-rendered card;
      - `POST /api/store/uploads` (begin: declared size and sha256),
        `PUT /api/store/uploads/{id}/parts/{n}` (each part under the body limit),
        `POST /api/store/uploads/{id}/complete`: the parts are joined, the sha256 checked, and
        the archive verified against the organization's author keys
        (`src/mendari/hub/archive.py:174-220`). One small archive may be sent in one request
        to `POST /api/store/versions`. See conflict 4;
      - `POST /api/store/copies` `{pack, name}`: copy a pack this deployment serves into a
        draft under a new id (conflict 6), recording `copied_from` (id, version, digest);
      - `POST /api/store/versions/{digest}/edits` `{path, content}` or a small set of them: a
        new draft whose parent is `digest`, with those text files replaced. Binary and trained
        files are not edited here (conflict 8);
      - `POST /api/store/versions/{digest}/tests`: run the draft's behaviour tests
        (`run_pack_tests`), answered as the command line answers them;
      - `DELETE /api/store/versions/{digest}`: withdraw a draft (the row and its events stay).
    - The checks every new version passes, in this order, before any row is written:
      the archive's own (size, signature, names, expansion), the PHI detector over every text
      file (20d), the load on the bounded engine, the namespace rule, 20b's validation, the
      dataset checks over datasets the pack carries (20c), then the hidden-set check.
    - A PHI rejection answers 422 with the report: kinds, files, lines and counts, no value.
      Nothing is stored; the refusal is logged with the report's counts only.
    - **The hidden-set check** (requirement 4). Every case in the pack's data, tests and
      examples is looked up with `hidden_matches` (fact 7). On any match the version is stored
      as `held`: it cannot be tested, evaluated, edited from or promoted. The uploader sees
      "held for the operator's review" and nothing else: not that a hidden set matched, not
      which file, not how many (the prompt's "Do not tell an uploader which hidden case
      matched"). The chained `pack_version.held` carries the digest and no case id. The match
      report (set and case ids) is written to `store_matches`, read only by the operator's
      command `mendari store held --db …`, which also releases or withdraws with a reason.
      Because a held answer is still one bit, uploads are budgeted: `store_uploads` a day, and
      after 3 held versions in 30 days an organization's uploads stop until the operator
      clears them (LP-11's hand-off, `notes/lp11-plan.md:680`). With no index key, see
      conflict 9.
    - Rate limits: a class `store` in `DEFAULT_RATES`, chosen for the `author` permission
      (`src/mendari/server/limits.py:108-117`, `:326-347`).
    - `/api/auth/me` says the store is on, so the dashboard knows whether to show it.
  - **Files:** `src/mendari/server/store_routes.py` (new), `src/mendari/server/app.py`,
    `src/mendari/server/auth.py`, `src/mendari/server/limits.py`,
    `src/mendari/core/accounts.py`, `src/mendari/core/pack_store.py`,
    `src/mendari/core/audit.py`, `src/mendari/server/vercel.py` (its settings list),
    `src/mendari/server/api_docs.py`, `src/mendari/dashboard/ui.js` (`MEMBERSHIP_ROLES`,
    `:340-348`: the role is no longer "reserved"), `tests/test_shipped.py`,
    `tests/test_two_organizations.py`, `tests/test_memberships.py`
    (`test_pack_author_grants_nothing`, `:451`, becomes what it grants),
    `docs/operations/accounts.md`, `docs/operations/pack-store.md`, `docs/guides/api.md`,
    ADR-0008's reserved-roles line. New: `tests/test_store_uploads.py`.
  - **Tests** (`tests/test_store_uploads.py`, signed in as `tests.conftest.member` makes
    sessions):
    - `::test_without_the_switch_nothing_is_mounted_and_packs_are_served_as_before`;
    - `::test_the_store_refuses_to_start_outside_memberships_mode_or_with_a_prerequisite_missing`;
    - `::test_an_author_uploads_a_signed_archive_and_it_is_a_draft`;
    - `::test_an_archive_signed_by_no_key_of_this_organization_is_refused`;
    - `::test_an_upload_with_phi_like_content_is_rejected_with_a_report_that_holds_no_value`
      (the prompt's test);
    - `::test_an_upload_containing_a_hidden_set_case_is_quarantined` (the prompt's test: held,
      and the answer, the chain, the logs and the list hold no set id, case id or count);
    - `::test_a_reworded_hidden_case_is_held_under_norm_v1`;
    - `::test_the_operators_report_names_the_set_and_case_and_no_route_does`;
    - `::test_three_held_uploads_stop_an_organizations_uploads`;
    - `::test_a_draft_in_organization_a_is_invisible_to_organization_b` (the prompt's test:
      lists, reads, edits, tests and the copy source, with the other organization's digest);
    - `::test_a_copy_takes_a_new_id_in_the_organizations_namespace_and_records_its_source`;
    - `::test_a_copy_of_a_pack_the_bounded_engine_refuses_says_why` (conflict 5);
    - `::test_an_edit_is_a_new_version_and_its_parent_is_unchanged`;
    - `::test_an_archive_in_parts_is_joined_checked_and_bounded`;
    - `::test_a_key_a_client_and_an_ehr_launch_never_author` (people only);
    - `::test_a_viewer_a_reviewer_and_an_administrator_without_the_role_cannot_upload`;
    - `::test_a_held_or_withdrawn_version_is_never_tested_edited_or_served`;
    - `::test_another_sites_page_cannot_upload` (the origin check);
    - `tests/test_shipped.py`: each new write route in `CHAINED` with its event;
    - `tests/test_two_organizations.py`: every new route called as the other organization.
  - **Safe alone because:** off unless `MENDARI_PACK_STORE=on`; the first test proves the engine
    is unchanged without it. A draft is never served: no decide route reads the store until
    20h.
  - **Needs the owner's yes before it is turned on anywhere:** it accepts other people's files
    into the engine's database.
  - **Threat model:** S24 (an upload passed off as another organization's or another author's),
    I22 (the hidden-set check as an oracle), I23 (a draft or dataset of one organization read
    by another), D17 (upload flooding and archive bombs through the parts route), E18 (the
    author role reaching beyond the store).

- [ ] **20g** Evaluation as a stored job with a ledger, and the comparison (requirement 7).
  Parallel with 20i.
  - **What it builds:**
    - Datasets in the store: `store_datasets (org, id, pack_id, name, sha256, cases, sealed,
      created_by, created_at, state)`. `POST /api/store/datasets` uploads a JSONL file (in
      parts when large), checked by the PHI detector, the dataset checks (the report is kept
      and shown) and the hidden-set check, exactly as a version is. Kept sealed under the PHI
      policy when the mode is `encrypt`, as a decision's input is
      (`src/mendari/core/audit.py:309-314`).
    - Jobs: `store_jobs (id, org, kind, digest, baseline_digest, dataset_sha256, config,
      state, done, total, report_id, created_by, created_at, finished_at)`, state `queued`,
      `running`, `done`, `failed` or `cancelled`.
      - `POST /api/store/jobs` `{version, dataset, baseline, config}` creates one, chained
        `store_job.created`. Local providers by default. A remote arm needs the organization's
        `eval_jev` or `eval_llm` quota to be set (unset means none here, stricter than the
        existing route) and keeps the 200-case cap (fact 9).
      - `POST /api/store/jobs/{id}/advance` runs the next slice: as many cases as fit a time
        bound well under the function's limit, each decision under LP-16's budget and the
        deployment's deadline, with `origin_cap=False` as every evaluation has
        (`src/mendari/server/app.py:1984-1991`). The partial ledger lines are kept with the
        job. It is safe to call twice: a slice is claimed in one transaction. See conflict 10.
      - `GET /api/store/jobs/{id}`: state and progress, as counts.
      - Finished, the job writes one `eval_reports` row with its ledger through `save_report`
        (fact 9), chained `eval.recorded`, and the job row names it. Two runs of the same
        version, dataset, configuration and seed give the same ledger bytes.
    - The comparison: `GET /api/store/comparisons?draft=…&current=…&dataset=…` over two
      finished jobs on the same dataset sha256. It is `paired.compare` (fact 8) plus what the
      prompt lists and the method already computes elsewhere:
      - accuracy, per-label recall, false automation and automation rate, each k of n with its
        Clopper-Pearson interval, for each side;
      - macro-F1 and Brier with a seeded bootstrap interval (the scoreboard's and the result's
        functions, fact 8);
      - the difference: exact McNemar per proportion, Holm-adjusted over the family written in
        the answer, and the paired bootstrap for Brier;
      - the cases that changed, by case id. The ids are the uploader's own dataset's, and the
        answer is the organization's alone.
      A figure under the method's floor reads "insufficient evidence".
    - "Current" is the organization's promoted version of the lineage, or the pack the draft
      was copied from when none is promoted yet.
    - Quotas: `MENDARI_ORG_DAILY_LIMITS` gains `store_jobs`.
  - **Files:** `src/mendari/core/pack_store.py`, `src/mendari/core/audit.py`,
    `src/mendari/server/store_routes.py`, `src/mendari/server/auth.py`,
    `src/mendari/server/limits.py`, `src/mendari/evals/paired.py` (only if a figure the prompt
    lists is not in its answer today), `src/mendari/settings.py`, `tests/test_shipped.py`,
    `tests/test_two_organizations.py`, `docs/operations/pack-store.md`, `docs/guides/api.md`,
    `docs/grading/` where the method is cited. New: `tests/test_store_jobs.py`.
  - **Tests** (`tests/test_store_jobs.py`):
    - `::test_a_job_is_stored_runs_in_slices_and_ends_in_one_report_with_its_ledger`;
    - `::test_two_runs_give_the_same_ledger_bytes`;
    - `::test_a_slice_called_twice_evaluates_each_case_once`;
    - `::test_a_job_stopped_half_way_continues_from_its_ledger`;
    - `::test_draft_against_current_is_paired_on_the_same_cases` (a different dataset sha256 is
      refused);
    - `::test_every_figure_matches_an_independent_computation` (scipy and statsmodels, as
      LP-10's fixtures do);
    - `::test_too_few_cases_reads_insufficient_evidence`;
    - `::test_the_changed_cases_are_listed_by_id_and_nothing_else_of_them`;
    - `::test_a_draft_runs_under_the_budget_and_a_slow_rule_ends_as_a_persons_decision`;
    - `::test_a_remote_arm_needs_the_organizations_quota_and_stops_at_it`;
    - `::test_a_dataset_with_a_hidden_case_or_phi_like_content_is_held_or_refused`;
    - `::test_another_organization_reads_no_job_report_dataset_or_comparison`;
    - `::test_audit_verify_passes_after_a_job[sqlite, postgres]`.
  - **Safe alone because:** it evaluates drafts and writes reports; no version is served and
    no decision is made for a patient. The existing evaluation route is unchanged.
  - **Threat model:** D17 (second half: jobs), I23 (second half: reports and datasets).

- [ ] **20h** Promotion, and serving the promoted version (requirement 8). **Adversarial
  security review.**
  - **What it builds:**
    - `store_promotions (id, org, pack_id, digest, version, author, approver, reports,
      reference, note, created_at, retired_at, retired_by)`. `reports` is the list of
      `eval_reports` ids it relied on.
    - `PackStore.promote(org, digest, *, approver, reports, reference, note)`, chained
      `pack_version.promoted` with the digest, the author, the approver and the report ids.
      It refuses when:
      - the approver is the version's author. The author is everyone who created a version on
        the chain from this digest back to its upload or copy, so an edit by a second person
        does not make the first a free approver (the prompt's "The author cannot approve their
        own promotion");
      - no report is named, a named report is not this organization's, is not for exactly this
        digest, or its job did not finish;
      - the version is `held`, `withdrawn` or not validated;
      - the deployment already serves that pack id from disk (conflict 6);
      - the pack is level 4 or above and the request claims automation (it never automates,
        ADR-0002; it may still be promoted to recommend).
    - A new people-only permission `promote`, held by `governance` and `admin` (decision 5).
      Rule: `("POST", r"^/api/store/promotions", "promote")`. It is not the `approve`
      permission and the record is not a `governance_approvals` row (the prompt's "Do not reuse
      the automation approval as promotion"). At levels 2 and 3 both records are needed before
      a promoted version automates: promotion to be served, the automation approval to
      automate.
    - Promoting a new version of a lineage retires the one before (`pack_version.retired`),
      in the same transaction. `POST /api/store/promotions/{id}/retire` withdraws a promoted
      version: the organization then serves no version of that pack until another is promoted.
    - **Serving.** `pack_or_404`, `decision_for`, the packs list and CDS discovery take the
      caller's organization: the packs read from disk at start-up, as today, plus that
      organization's promoted versions. A promoted version is loaded and built on its first
      use in a process and kept in a bounded cache keyed by digest (conflict 11); a version
      another organization promoted is never in the answer. This is what ADR-0004 promised:
      a credential lists "never another organization's" packs
      (`docs/decisions/ADR-0004-machine-access-tokens-and-scopes.md:57-59`).
    - A decision by a store pack records its origin (`store`), its digest and the promotion's
      id, so the audit trail leads from a decision to who signed off its version.
    - The hold (LP-06) and recalls keep working on a store pack's id and digest. A store pack
      has no registry recall; withdrawing the promotion is its recall.
  - **Files:** `src/mendari/core/pack_store.py`, `src/mendari/core/audit.py`,
    `src/mendari/server/app.py` (how a pack is picked), `src/mendari/server/store_routes.py`,
    `src/mendari/server/auth.py`, `src/mendari/core/decision.py` (the promotion on the record),
    `tests/test_shipped.py`, `tests/test_two_organizations.py`, ADR-0004's amendment,
    `docs/operations/pack-store.md`, `docs/guides/api.md`, `docs/security/threat-model.md`
    (S25, T13, R7, E19). New: `tests/test_store_promotion.py`.
  - **Tests** (`tests/test_store_promotion.py`):
    - `::test_the_author_cannot_approve_their_own_promotion` (the prompt's test; also an author
      of an earlier version in the chain of edits);
    - `::test_a_promotion_names_reports_of_this_digest_and_this_organization` (parametrized
      over none, another digest's, another organization's, an unfinished job's);
    - `::test_every_risk_level_needs_a_promotion` (levels 0 to 3: an unpromoted draft is never
      served);
    - `::test_the_automation_approval_is_not_a_promotion_and_a_promotion_is_not_one`
      (a level 2 version with only one of the two never automates);
    - `::test_a_promoted_version_is_served_to_its_organization_and_to_no_other` (decide, the
      packs list, CDS discovery, the FHIR operation, with a key, a client and a person);
    - `::test_promotion_is_bound_to_the_digest` (the next draft is not served until promoted);
    - `::test_promoting_retires_the_version_before_and_both_are_chained`;
    - `::test_a_retired_promotion_stops_serving_at_once_in_this_process`;
    - `::test_a_store_pack_never_replaces_a_pack_the_deployment_serves_from_disk`;
    - `::test_a_decision_names_the_promotion_that_let_its_version_serve`;
    - `::test_a_held_pack_and_the_kill_switch_still_stop_a_store_pack`;
    - `::test_health_answers_within_its_bound_while_a_store_pack_is_built`;
    - `::test_a_key_a_client_and_an_author_without_the_role_never_promote`;
    - `::test_audit_verify_passes_after_a_promotion[sqlite, postgres]`.
  - **Safe alone because:** with no promotion row (every database today) every organization
    is served exactly the packs read from disk. Only an organization that promotes a version
    is served anything new, and only that organization.
  - **Needs the owner's yes:** it changes which pack answers a decide request (clinical
    behaviour), so it is not merged on a green run alone.
  - **Threat model:** S25 (a promotion forged or replayed for another digest), T13 (a served
    version that is not the promoted bytes), R7 (who promoted, and on what evidence), E19
    (an author promoting their own work, alone or through a second account).

- [ ] **20i** The improvement dataset, and proposed behaviour tests (requirement 9). Parallel
  with 20g.
  - **What it builds:**
    - `src/mendari/evals/corrections.py`: `corrections_dataset(store, *, pack_id, since,
      limit, reader)`. One record per decision a reviewer resolved (fact 16): its input, the
      final label per question, and each label's provenance: the decision's id, the reviewer's
      id (never an address), the time, whether it confirmed or overrode the system, and the
      version that decided. Decisions kept as a hash only, and encrypted inputs without a
      reader, are counted as skipped, as the ratings export does
      (`src/mendari/evals/feedback.py:25-29`).
    - `POST /api/store/packs/{namespace}/{name}/improvement`: builds it into a `store_datasets`
      row of kind `improvement`, sealed under the PHI policy, chained
      `store_dataset.created` with counts only. It needs `author` and `read_input`, because it
      reads patients' inputs. It is never downloadable as a file and never leaves the
      organization: no export route, no registry path, no site report (LP-07) carries it.
    - Proposed tests: `GET /api/store/versions/{digest}/proposed-tests` turns each corrected
      case into a behaviour-test case (`name`, `input`, `expect`,
      `src/mendari/evals/pack_tests.py:31-54`) and runs it against the draft. The proposals
      are kept beside the draft in the store. **They are not written into the pack's files**
      (conflict 12). `POST …/proposed-tests/{id}` accepts or dismisses one; accepted ones run
      with the draft's own tests in 20f's test route and are shown apart.
    - It can also be the dataset of a 20g job, so a draft is compared with the current version
      on the cases reviewers corrected.
  - **Files:** `src/mendari/evals/corrections.py` (new), `src/mendari/core/pack_store.py`,
    `src/mendari/core/audit.py`, `src/mendari/server/store_routes.py`,
    `src/mendari/server/auth.py`, `tests/test_shipped.py`, `tests/test_two_organizations.py`,
    `docs/operations/pack-store.md`. New: `tests/test_store_improvement.py`.
  - **Tests** (`tests/test_store_improvement.py`):
    - `::test_each_label_names_the_decision_the_reviewer_and_whether_it_overrode`;
    - `::test_a_hashed_input_is_skipped_and_counted`;
    - `::test_the_dataset_is_sealed_and_no_route_returns_its_cases`;
    - `::test_an_author_who_may_not_read_inputs_cannot_build_it`;
    - `::test_another_organizations_corrections_are_never_in_it`;
    - `::test_a_proposed_test_runs_against_the_draft_and_is_not_in_the_packs_files` (the
      draft's digest is unchanged);
    - `::test_an_accepted_proposal_runs_with_the_drafts_tests`;
    - `::test_no_chained_event_or_log_holds_an_input`.
  - **Safe alone because:** it reads what the organization's reviewers already see and writes
    rows only that organization reads.
  - **Threat model:** I23 (third part: corrections).

- [ ] **20j** The dashboard: upload, results, comparison and promotion (requirement 10).
  - **What it builds:**
    - `src/mendari/dashboard/store.js` (an ES module, registered in `views`, fact 19), shown
      when the server says the store is on and the person holds `author` or `promote`:
      - **Packs**: the organization's lineages and versions with their state in words
        ("Draft", "Held for the operator's review", "Serving", "Retired", "Withdrawn"), copy a
        pack, upload an archive (in parts, with progress), and the PHI report when an upload
        is refused (kinds, files and lines);
      - **A version**: its files, an editor for text files that saves as a new draft, the
        validation result, the behaviour tests and the proposed tests, the server-rendered
        card;
      - **Results**: datasets with their check report, jobs with their progress, a report's
        figures as k of n with intervals;
      - **Compare**: draft against current, side by side, the differences with their tests and
        adjusted p-values, "insufficient evidence" where it applies, and the changed cases by
        id;
      - **Promote**: the reports to rely on, a reference and a note, and a confirmation that
        says in words what will be served and to whom. The control is absent for the author,
        with the reason.
    - Styles from tokens only (`CLAUDE.md`, "Dashboard styles use tokens only"); no new colour.
      Empty, loading, refused and error states for each list; every control reachable by
      keyboard and named for a screen reader.
    - The `/docs` landing and its snippets gain the store's API examples
      (`scripts/check_doc_snippets.py`).
  - **Files:** `src/mendari/dashboard/store.js` (new), `main.js`, `router.js`, `index.html`,
    `style.css`, `ui.js`, `src/mendari/dashboard/docs.html`, `src/mendari/dashboard/snippets`.
    New: `tests/browser/test_pack_store.py`.
  - **Tests** (`tests/browser/test_pack_store.py`, marked `browser`):
    - `::test_copy_edit_test_compare_and_promote_in_a_second_organization` (the prompt's
      browser test: in clinic-b an author copies the inbox pack, edits a rule, runs the tests
      and two jobs, reads the comparison; a second person promotes; a decide request from
      clinic-b is answered by the promoted digest and one from clinic-a by the built-in pack);
    - `::test_the_author_sees_no_promote_control_and_why`;
    - `::test_a_refused_upload_shows_kinds_and_places_and_no_value`;
    - `::test_a_held_upload_says_only_that_it_is_held`;
    - `::test_a_comparison_on_too_few_cases_says_insufficient_evidence`;
    - `::test_without_the_store_the_view_is_not_there`;
    - `tests/browser/test_accessibility.py` covers the new views;
      `tests/test_dashboard_contrast.py` stays green.
  - **Safe alone because:** the view appears only when the server says the store is on.

- [ ] **20k** Close-out.
  - `launch/gates.yaml`: `a7-pack-store` (`:290-294`): the pending line becomes the tests of
    20b to 20j. `:403` (LP-17l) is reworded by whichever of 19k and 20k merges second.
    `external-review` (`:470-474`) is the owner's and is not touched.
  - ADR-0016 to Accepted once the owner confirms the decisions; ADR-0008's reserved-roles line
    and ADR-0009's "LP-20 adds `store`" updated to what is built.
  - `docs/security/threat-model.md`: the rows, each naming its tests; the Pack content part's
    trust boundary (`:186-189`) and the three residuals marked "for LP-20" (`:215-232`)
    reworded to what 20e did; the residual at `:507-509` (a reworded hidden case passes the
    index) kept, since the store inherits it; new residuals: a name list finds listed names
    only, hosted training is not built, the held answer is one bit.
  - `docs/security/review-packet.md:346` (the external review's scope) names the store's
    routes and tables.
  - `docs/guides/authoring-a-pack.md`, `docs/guides/api.md`, `docs/operations/pack-store.md`,
    `README.md`, the `/docs` landing.
  - `tasks/todo.md`: LP-20 checked, the owner steps listed, LP-17l unblocked on it.
  - `tests/test_gates.py` and `tests/test_shipped.py` green; `NOT_CHAINED_YET` still empty.

## Which pending probes each PR replaces

| `launch/gates.yaml` | Today | Replaced by |
|---|---|---|
| `:294` a7-pack-store | pending: LP-20 | 20k, with tests from 20b (`tests/test_validation.py`), 20c (`tests/test_dataset_checks.py`), 20d (`tests/test_phi_detect.py`), 20e (`tests/test_pack_store.py`, `tests/test_pack_store_prerequisites.py`), 20f (`tests/test_store_uploads.py`), 20g (`tests/test_store_jobs.py`), 20h (`tests/test_store_promotion.py`), 20i (`tests/test_store_improvement.py`) and 20j (`tests/browser/test_pack_store.py`) |
| `:403` LP-17l | pending: after LP-19 and LP-20 | reworded by 19k (to "after LP-20") and then by 20k (to what 17l itself still needs); if 20f merges before 19k, 20k says "after LP-19" |
| `:474` external-review | owner attestation, empty | not replaced: the owner's. 20k makes the review packet name the store |

No build PR fills an attestation or marks an owner prerequisite met. `tests/test_gates.py:38-48`
pins seven prerequisites in G3-4; LP-20 adds none.

## Conflicts written before building, and the default each PR follows

1. **"One validation function … called by the registry", and what the registry takes today.**
   The registry's publish never ran the validator (fact 5), so calling it makes the registry
   refuse packs it accepts now. Default: it refuses them, and names the problem. A test pins
   that a valid publish is stored byte for byte as before. (20b)
2. **"Too few to estimate" instead of a hard minimum, and the command's exit code.** The prompt
   forbids a hard minimum per label. Default: `mendari data check` fails only on a schema or
   label problem; thin labels, duplicates and overlap are in the report and never fail the
   command or the workflow. Promotion reads the report; the approver decides. (20c, 20h)
3. **"A name list", and synthetic data that is full of made-up names.** A list of common names
   matches ordinary words and the shipped synthetic datasets, which must pass. Default: a name
   counts only on a line that also holds another identifier or a word that says it is a
   person's; structural identifiers reject alone. 20d measures every shipped dataset first and
   writes the counts and the threshold in the module. (20d)
4. **Archive size.** LP-19 left "archives larger than a function's body limit" and "object
   storage for archives" to LP-20 (`notes/lp19-plan.md:677-678`). The body limit is 4 MiB
   (fact 14). Default: an upload in parts, each under the body limit, joined and checked by its
   sha256 on the server, kept in the database; 32 MiB an archive at most
   (`MENDARI_STORE_MAX_ARCHIVE_BYTES`), under the format's own 100 MB. No object storage: it
   would be a second place to back up, to bind to an organization and to erase. The registry's
   own publish keeps the body limit; raising it with the same parts route is a later change to
   the registry, after LP-19g. (20f; decision 6)
5. **"Copy a built-in pack", and the bounded engine.** A copy takes a new id (conflict 6), so
   its digest is no reference pack's and it runs on RE2. The trial pack and the referral pack
   hold lookaheads RE2 refuses (fact 18), and the owner decided their rewrite is needed "before
   a reference version that differs from the installed one is … copied into the store"
   (`tasks/todo.md:587-595`). Default: LP-20 does not do 16c-2. Copying those two packs is
   refused with the reason; the inbox, prior-authorisation and AI Guard packs copy. The browser
   test copies the inbox pack. (20f; decision 7)
6. **Whose id a store pack carries.** A pack's id is in its manifest and so in its digest. A
   copy of `mendari/patient-inbox-router` that kept the id would pass as Mendari's, and would
   compete with the built-in pack every organization is served. Default: a store pack's
   namespace is its organization's id (organization ids and pack namespaces use the same
   characters, `src/mendari/core/accounts.py:30`, `src/mendari/core/spec.py:32`), or, when the
   registry is mounted, a namespace that organization holds by an approved LP-19c claim.
   `mendari`, `openhealth` and `community` are refused. A copy is renamed on the way in, and a
   promotion is refused when the deployment already serves that id from disk. (20f, 20h)
7. **"A signed archive": signed by whom.** `archive.verify` needs a set of trusted keys
   (`src/mendari/hub/archive.py:174-220`), and the engine has none for an organization. Default:
   the store keeps author keys per organization, registered by an administrator; the signature says
   which key holder built the archive, and the signed-in person says who uploaded it. Both are
   on the chain. The registry's namespace keys are not reused: the registry may be off.
   (20f; decision 8)
8. **"Copy a pack, edit, test", and "drafts are immutable".** Default: an edit makes a new
   draft with the old one as its parent. Only text files are edited on the server (the
   manifest, the policy, rules, criteria, wording, tests). A pack whose trained model must
   change is trained locally and uploaded: hosted scaffold, train and calibrate (the audit's
   A7.closed-code, `notes/vision-audit.md:458`) are not in the LP-20 prompt and are not built.
   (20f; left out)
9. **A hidden-set check on a deployment with no hidden sets.** The index needs
   `MENDARI_EVALSET_INDEX_KEY`, and the hosted site has no evaluation-set keys yet
   (`src/mendari/server/app.py:3220-3223`). Without the key the deployment holds no hidden
   case to leak. Default: the check records "not applicable: this deployment holds no hidden
   sets" on the version, and uploads proceed. When keys are set later, `mendari store recheck`
   runs the check over what is stored and holds what matches. (20f)
10. **"Evaluation as a stored job", with no worker.** There is no queue or worker, and the
    owner chose no worker process for now (ADR-0009, `tasks/todo.md:565`). Default: a job is a
    row that advances in slices, each slice one request the dashboard or the command line
    makes; nothing runs when nobody asks. A stopped job continues from its ledger. No new cron
    route. (20g; decision 9)
11. **"Never built in a request" (LP-16d), and packs that arrive after start-up.** Every served
    pack is built once at start-up so a slow build never holds a decide request (fact 1). A
    promoted version does not exist at start-up. Default: it is built once per process on its
    first use, under a lock, in a bounded cache keyed by digest; a request that arrives while
    it is being built waits for that build and never starts a second; health stays within its
    bound (a test). Start-up builds stay as they are. (20h)
12. **"Offer them as proposed behaviour tests", and patients' inputs in a pack's files.** A
    corrected case is a real input wherever the server holds real data. Written into a pack's
    tests it would be part of an archive that can be exported and published. Default: proposals
    live beside the draft in the store, sealed, and run with its tests; they are never written
    into the pack's files, so the digest does not change and nothing a pack carries holds a
    patient's input. (20i)
13. **"Reported to the operator", with no outbound channel for it.** Mendari sends no e-mail.
    Default: the match report is a row only the operator's command reads, the held count is in
    the signed-in administrator's health answer for the deployment's own organization, and
    the security log gets one line with the digest. (20f)
14. **Promotion "that every risk level needs", and packs read from disk.** Built-in, local and
    pulled packs are served today with no promotion. Default: promotion is the rule for store
    versions only; packs the operator installed are served as today. A pulled pack keeps its
    own per-organization approval (fact 4). Making promotion the only way any pack is served is
    a later decision. (20h; decision 10)
15. **Where the events are chained.** Definition of shipped 5 asks for a chained event bound to
    its organization. Default: the engine's own chain, with the administrative kinds each
    organization can read in its Activity tab, unlike the registry's separate chain in LP-19.
    (20e to 20i)
16. **The existing evaluation route and a store pack.** It lets a caller choose any variant of
    a pack that is not a community pack (`tasks/todo.md:762-767`). Default: for a `store` pack
    the choice is limited as it is for a community pack; the existing route is otherwise
    unchanged, and drafts are evaluated only through jobs. (20e, 20g)

## Owner steps (not done by any PR)

1. Decide the name list's source and confirm its licence (decision 3), before 20d merges.
2. Set `MENDARI_PACK_STORE=on` on the engine, after 20f to 20h are merged and reviewed, with
   `store_uploads` and `store_jobs` in `MENDARI_ORG_DAILY_LIMITS`.
3. Give `pack_author` to the people who write packs, and make sure each organization has a
   second person who holds `promote`.
4. Register each author's public key in the Access view.
5. When hidden sets are frozen and their keys set, run `mendari store recheck` once.
6. Read `mendari store held` when the health answer shows a held version, and release or
   withdraw each with a reason.
7. Give the external reviewers the store's scope (gate `external-review`), then attest it.
8. Choose 16c-2's form (S or D) if the trial and referral packs should be copyable.

## Owner decisions still open (each PR follows the default until told otherwise)

1. **The registry refuses a publish the validator refuses.** Default: yes, from 20b.
   Alternative: warn for one release.
2. **Does the store need memberships mode.** Default: yes, and it stops the server at start-up
   otherwise. A self-hosted single-organization site keeps using packs on disk.
3. **The name list.** Default: the most common given names and surnames from United States
   government publications (Census surnames, Social Security given names), cut to a few
   thousand, recorded in the licence inventory. Alternative: no list until counsel has read
   the sources; the detector then ships with structural kinds only and the gate line says so.
4. **Who may author.** Default: only a member holding `pack_author`; an administrator gives
   themself the role. Alternative: every administrator authors.
5. **Who may promote.** Default: `governance` and `admin`, people only, never the author.
   Alternative: a new role `pack_approver`.
6. **Archive size and where archives live.** Default: 32 MiB, in the database, uploaded in
   parts. Alternative: object storage with the platform's private store.
7. **16c-2.** Default: not part of LP-20; two reference packs cannot be copied until it is done.
8. **Author keys.** Default: registered by an administrator, per organization. Alternative:
   an author registers their own.
9. **Jobs with no worker.** Default: slices driven by the caller. Alternative: a cron route
   that advances queued jobs once a minute (a new scheduled route on the hosted project).
10. **Promotion for packs on disk.** Default: no; store versions only.
11. **A one-person organization.** Default: it cannot promote; there is no way round on the web
    or by command. Alternative: an operator's command with a reason on the chain, as LP-19
    gives the registry.
12. **How many held uploads stop an organization.** Default: 3 in 30 days, and 20 uploads a day.
13. **Remote providers on drafts.** Default: only when the organization's evaluation quota is
    set, 200 cases a job at most. Alternative: local only, as hidden sets are.
14. **What an organization may keep.** Default: 256 MiB of files, no limit on the number of
    versions. Withdrawn drafts free their files after 30 days; the rows and events stay.
15. **Does a promoted version from the store ever keep the maintained engine.** Default: no;
    every store pack is bounded, since its id differs from a reference pack's.

## Left out, on purpose

- **Publishing a promoted version to a registry** from the store. It needs LP-19d's mount and
  LP-19c's claims, and the author's own key signs archives, not the server. After LP-19.
- **Paid or private packs in a registry** (`notes/lp19-plan.md:677-678`). A store pack is
  private to its organization by construction; selling or sharing one is not in the prompt.
- **Hosted scaffold, training, calibration** (A7.closed-code). Conflict 8.
- **LP-17l's submission route, its key and its budget.** 17l builds them on 20e and 20f.
- **16c-2.** Conflict 5.
- **Sharing a version between organizations**, moving a lineage, deleting an organization's
  store (the erasure runbook gains a line in 20k; the operation is LP-21's).
- **A keyed checkpoint of the store's files.** The chain covers each version's row and digest;
  `mendari store verify` re-hashes the files.

## Risks

1. **Serving changes for the first time since LP-16d.** 20h changes how a pack is picked for a
   request. Kept in check by: off without the switch; unchanged for any organization with no
   promotion; a test that the answers of every existing route are equal with the store on and
   empty; the owner's yes before merge.
2. **Other people's files in the engine's database.** Bounded by the archive's limits, the
   per-organization bytes, the daily uploads, RE2 and the CPU budget. This is the surface the
   external review should see with the registry.
3. **The hidden-set check leaks one bit per upload.** A held answer tells an uploader that
   something matched. Bounded by the upload budget and the stop after three; written as a
   residual. A reworded case passes the index (`docs/security/threat-model.md:507-509`).
4. **The PHI detector is a filter, not a guarantee.** It finds shapes and listed names. The
   threat model says so; the store's terms (LP-13's texts) must still forbid real data.
5. **A scratch directory per process.** A serverless instance writes each loaded version to
   temporary disk. The cache is bounded in bytes; open point 1.
6. **Slices and time limits.** A slice that outlives the function loses its work for that
   slice only; the claim expires and the next call repeats it. Determinism is tested across a
   stop.
7. **The improvement dataset holds patients' inputs.** Sealed, organization-bound, no export,
   and never in a pack's files (conflict 12).
8. **Shared files with LP-19 in flight.** `src/mendari/hub/registry.py`,
   `src/mendari/server/auth.py`, `src/mendari/core/accounts.py`, `tests/test_shipped.py` and the
   dashboard. The merge table above.
9. **Plan length.** Eleven PRs give room for drift. Each PR updates the todo entry, and the
   conflicts above are the defaults unless the owner changes them.
10. **Parallel lanes and ids.** ADR-0016 and the threat rows were free at ca7e016b only.

## Open points not verified by the planning read

1. **Whether a function on the hosted platform can write a scratch directory, and how much.**
   The code says only "no persistent disk" (`src/mendari/server/vercel.py:5-7`). 20e must check
   before it relies on it; if it cannot, `load_pack` needs a way to read from memory, which is
   a larger change.
2. **The licence and exact form of any name list.** No list was chosen or read.
3. **How many findings the detector's kinds give on the shipped datasets today.**
   `src/mendari/community/phi_scan.py` was read, not run over `datasets/` and `packs/`.
4. **Where the four LP-16 residuals are applied in code** (cost, batching, temperature, unscoped
   provider settings): the todo entry and the threat model were read
   (`tasks/todo.md:762-767`; `docs/security/threat-model.md:215-232`), the provider code was
   not. 20e finds the lines.
5. **Whether `paired.compare`'s answer already holds every figure the prompt lists** (per-label
   recall, false automation, automation rate, macro-F1). Its signature and its docstring were
   read (`src/mendari/evals/paired.py:120-224`), not each key of its answer.
6. **How long validating a reference pack takes inside the registry's publish request** (20b
   adds provider builds and dataset reads to it). Not measured.
7. **Whether Postgres holds 32 MiB of archive parts comfortably in the pooled connection the
   hosted site uses.** Not tested; the registry already keeps whole archives in one row
   (`src/mendari/hub/registry.py:139-142`).
8. **The exact lines of the decide routes, CDS discovery and the FHIR operation that 20h must
   make organization-aware.** `pack_or_404` and the list were read (fact 1); every caller of
   `packs` was not listed.
9. **`docs/guides/authoring-a-pack.md`, `docs/guides/api.md`, `docs/operations/accounts.md` and
   the `/docs` landing** were not read; the plan names them as places to update.
10. **Whether LP-22's plan (in another session) takes ADR-0016 or any row above.** A note with
    these numbers was left in the sessions' shared folder; 20a re-checks before it pushes.
