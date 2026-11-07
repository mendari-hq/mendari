<!-- Planned by a mapping workflow on 2026-10-17 against main 06d5332f (after #251, LP-11n). One reader mapped the code LP-17 builds on: statistics and ledgers, hidden-set grading, arms, cost and latency, signing, the shipped scoreboards and their surfaces, docs and gates. Two planners split the work into pull requests, one putting safety first and one cutting thin vertical slices. A judge checked both drafts against the code and wrote this plan from them. It keeps the slices' order (the early scoreboard comes from local ledgers, before anything remote), and it takes the safety draft's statistics, signing, store and display rules. The conflicts below are the defaults each PR follows unless the owner chooses otherwise. tasks/todo.md tracks progress. -->

# LP-17 build plan: the scoreboard and the grading service

> **Numbers allocated across the lanes built together on 2026-10-17** (so parallel PRs never take the
> same id; `tests/test_threat_model.py` still checks the order): ADR-0013 is LP-17's and ADR-0014
> LP-23's. Threat-model rows: LP-18h takes S18, S19, T7, R3, D11, D12, E12 and E13; LP-17 takes
> S20, T8, R4, I14, I15 and E14; LP-23 takes S21, T9, R5, I16, I17, I18, D13, D14, E15 and E16.

## Summary

LP-17 adds a published grading method, a scoreboard computed from ledgers, and scoped grades given only on frozen hidden sets. It delivers:
- `docs/grading/method-1.0.md`:
  - Every rule is written down before any code.
  - The document is pinned by its hash.
  - Its status reads "draft, not in effect" until the owner sets an effective date.
- Arms by provider class, derived from each pack's manifest. An arm a pack cannot build shows "not applicable".
- `mendari.scoreboard/1` entries, computed from a ledger, its dataset and a run record:
  - every proportion as k of n with its exact 95% interval;
  - every mean with a seeded bootstrap interval;
  - evidence decided on distinct positives;
  - a scope statement and a disclosure on every entry.
- An append-only store of results under `scoreboard/`:
  - each result is named by its content hash and may name a result it supersedes;
  - a step in the required `reference-pack` job checks the store;
  - the owner signs results offline with a Mendari grading key, and anyone can check them with a public verifier.
- The early scoreboard (stage 3):
  - every local configuration of the five packs on their 15 public scoring sets;
  - unranked and ungraded, with the insufficient-evidence banner.
- Public template sets relabelled "public set: regression check, not a ranking" on the Decision Card, the dashboard and `web/`. The relabel removes every Rank column.
- Remote arms on public sets, with 3 to 5 replicates, their spread and the case flip rate. The code is built here; the live run is the owner's.
- Grades and band ranking:
  - A grade is a set of attestations, each met, not met or insufficient evidence.
  - Ranking uses bands, a stated Holm family and rank intervals.
  - Both are given only on frozen hidden sets, through the existing grading route.
- The developer flow: banded answers, a budget, and a sealed confirmation split. It is blocked on LP-20.

It is built in **13 PRs, 17a to 17m**: 12 build PRs and one data PR that the owner runs (17i).

Rules every PR follows:
- These stay unchanged, byte for byte:
  - `mendari.eval/2`, `mendari.ledger/1`, `mendari.compare/1` and `Proportion.to_dict`;
  - `mendari.rank/1` as data;
  - `mendari.hidden_result/1` (the answer to a request without `arms`);
  - every committed benchmark and ledger, and every pack decision.

  `scripts/check_eval_results.py` stays green. The only display change is 17g, a stated format bump.
- Nothing is ranked or graded on a public set: every shipped dataset is public and in the repository.
- Nothing is ranked on selection data. Grades and ranks are given only on frozen hidden sets, over configurations declared before grading. In 17l they are given on the sealed confirmation part only.
- CI never calls a live provider and never holds the signing key. The signing key is never a server setting.
- Every figure shown carries its interval and scope:
  - A proportion is k of n with its exact Clopper-Pearson 95% interval (Definition of shipped §3).
  - A mean (macro-F1, Brier, cost, latency) has a seeded percentile bootstrap interval: 2,000 resamples, seed 0.
  - ECE is a description only, with no interval, as the prompt says.
  - Every table carries its scope statement.
- Whatever waits on the owner says so where it shows:
  - "method draft, not in effect";
  - "unsigned";
  - "not measured: owner's run pending";
  - "not published: disclosure pending";
  - a `pending` or `attested` gate probe;
  - an owner line in `tasks/todo.md`.
- Every PR:
  - updates `docs/`, `README.md`, `CHANGELOG.md` and `tasks/todo.md`;
  - gives each new source file `# SPDX-License-Identifier: Apache-2.0`;
  - signs off each commit (`git commit -s`);
  - states its effect on each reference pack's critical-label figures (none for every PR in this plan).

Order of work, and what can be built in parallel:
- 17a comes first.
- After 17a, three PRs can be built in parallel: **17b** (statistics), **17c** (arms) and **17e** (signing). Their files do not overlap except the `mendari scoreboard` group in `cli.py`; whichever lands second rebases onto it.
- 17d needs 17b and 17c.
- 17j needs 17d. It can be built in parallel with 17f, 17g and 17h.
- 17f needs 17d and 17e.
- 17g and 17h need 17f and can be built in parallel. Both touch `scripts/scoreboard_page.py`; the second rebases.
- 17k needs 17f and 17j. It can be built in parallel with 17g and 17h.
- 17i follows 17h. The owner runs it (owner decisions 6 and 7).
- 17l follows 17k and LP-20 (owner decisions 4 and 10).
- 17m follows 17g, 17h and 17k. It does not wait for 17i or 17l, and its gate probes name both as pending.

The prompt's six tests, and where each lands:

| The prompt's test | Where it lands |
|---|---|
| Every interval, test and adjustment against scipy and statsmodels | 17a, 17b, 17d and 17j |
| The band order is a total preorder | 17j, with hypothesis |
| A re-run is byte-identical | 17d, 17f, 17j and 17k |
| 20 emergency positives give "insufficient evidence" | 17d on a public set, 17k on a hidden set |
| A tampered result fails, and a publisher-key signature is refused | 17e |
| No route returns a per-case field from a hidden set | 17k, and the sweep in 17l |

Acceptance: the method document (17a), the scoreboard for all five packs on public sets (17f for the deterministic arm, 17i for the remote arms), and these tests in CI (17m).

## Pull requests, in order

- [ ] **17a** The plan entry, the grading method 1.0 (draft), ADR-0013 (Proposed) and the method's pinned constants.
  - **What it builds:**
    - The `tasks/todo.md` LP-17 entry: this plan, its conflicts and the owner decisions.
    - `docs/grading/method-1.0.md`, version `mendari.grading/1.0`:
      - Its status line reads "draft, not in effect". Its effective-date line is left empty for the owner. It has a changelog.
      - One section per requirement, 1 to 14, written before the code that follows them:
        - arms;
        - selection: declared configurations, and the salted split for submissions;
        - which figures are estimates with intervals and which are descriptions;
        - statistics: two-sided Clopper-Pearson at 1 − 0.05/L for the grade's bounds and 95% for display, exact McNemar, the paired bootstrap, and the Holm family;
        - the attestations;
        - the bands;
        - the evidence rule;
        - the scope statement;
        - the inputs of a reproducible entry;
        - signing;
        - freshness;
        - the developer flow;
        - the early scoreboard.
      - A fenced, machine-readable parameter block:
        - level 0.95, alpha 0.05, 2,000 resamples, seed 0;
        - at least 100 distinct positives;
        - recall floor 0.95, on the simultaneous lower bound;
        - false-automation ceiling 0.02, on the 95% upper bound;
        - the band edges and the cost bands;
        - stale after 45 days;
        - 3 to 5 replicates;
        - canary size 30;
        - split fraction 0.5.

        Every number is marked "owner to confirm" (owner decision 5).
      - The evidence rule: `n_min(floor, L) = max(100, the smallest n whose n-of-n lower bound at 1 − 0.05/L reaches the floor)`. Its table, computed on main with `stats.clopper_pearson`:

        | Floor | L = 1 | L = 2 | L = 4 | L = 5 | L = 6 |
        |---|---|---|---|---|---|
        | 0.95 | 100 | 100 | 100 | 104 | 107 |
        | 0.97 | 122 | 144 | 167 | | |
        | 0.98 | 183 | 217 | 252 | | |
        | 0.99 | 368 | 437 | 505 | | |

        The false-automation ceiling of 0.02 needs at least 183 labelled cases with no wrong AUTO.
    - `src/mendari/evals/method.py`:
      - Constants: `METHOD`, `METHOD_PATH`, `METHOD_SHA256` (the document's bytes), `EFFECTIVE: date | None = None` and the parameters.
      - Functions: `simultaneous_level(labels)`, `min_positives(floor, labels)` and `in_effect(today)`.
      - It imports only `stats`, and nothing imports it yet.
    - `docs/decisions/ADR-0013-scoreboard-and-scoped-grades.md` (Proposed). It records:
      - a separate scoreboard format, with `rank/1` kept as data;
      - grades and ranks on frozen hidden sets only;
      - local providers only on hidden sets until owner decision 2;
      - the offline signing key and its domain separation;
      - the repository store;
      - the declared-configuration route to selection.
    - `mkdocs.yml`:
      - A new "Grading" section holds the method.
      - The ADR goes under Decisions.
      - `validation.omitted_files: warn` together with `--strict` fails any `.md` page left out of the nav.
    - `launch/gates.yaml`, `grading-method`:
      - Add `file: docs/grading/method-1.0.md`.
      - Its `pending` line names the rest of LP-17.
      - The attestation stays empty.
  - **Files:**
    - New: `docs/grading/method-1.0.md`, `src/mendari/evals/method.py`, the ADR, `tests/test_grading_method.py`.
    - Changed: `mkdocs.yml`, `launch/gates.yaml`, `docs/spec/evaluation-reports.md` (a cross-link), `README.md`, `CHANGELOG.md`, `tasks/todo.md`.
  - **Tests** (`tests/test_grading_method.py`):
    - `test_the_pinned_hash_is_the_method_documents`
    - `test_every_parameter_in_the_code_is_in_the_documents_block`
    - `test_the_method_has_a_version_an_effective_date_line_and_a_changelog`
    - `test_the_minimum_positives_reach_the_floor_with_a_perfect_result`: at `n_min` the bound passes and at `n_min − 1` it fails, for L = 1 to 8. Checked against scipy `beta.ppf` and statsmodels `proportion_confint(method="beta")`.
    - `test_nothing_is_in_effect_without_an_effective_date`
    - The existing `tests/test_gates.py::test_every_test_the_file_names_exists` and `::test_no_build_session_attested_for_the_owner` stay green.
  - **Safe alone because:** it adds docs and a constants module that nothing imports. No format, route or pack changes.

- [ ] **17b** Statistics additions (pure), in `src/mendari/evals/stats.py`.
  - **What it builds:**
    - `stratified_resample_indices(strata, resamples, seed)` and `stratified_bootstrap(records, strata, statistic, resamples=2000, seed=0, level=0.95)`:
      - They resample within each stratum and keep each stratum's size, seeded as `resample_indices` (line 302) is.
      - The interval is the percentile interval through `percentile` (line 314).
      - Macro-F1 uses it, stratified by the true label.
    - `weighted_proportion(hits, weights, level=0.95)`:
      - It gives the Hájek estimate, the Kish effective n, and the Korn-Graubard interval: Clopper-Pearson at the effective counts, through `beta_ppf` (line 210).
      - x is floored for the lower bound and ceiled for the upper, so both shapes stay at 1 or more (`tests/test_stats.py::test_the_beta_quantile_takes_shapes_of_at_least_one`).
      - With unit weights it equals `clopper_pearson`.
    - `brier_reliability(distributions, truths, labels, bins=10, min_bin=30)`:
      - Murphy's reliability term over equal-mass bins of top-label confidence: the squared distance between each bin's mean forecast vector and its outcome frequencies, weighted by the bin's share.
      - It reads `not_estimable` as `equal_mass_ece` (line 417) does.
    - Unchanged: every existing function, `LEVEL`, `DIGITS`, `interval` and `Proportion.to_dict`.
  - **Files:** `src/mendari/evals/stats.py`, `tests/test_stats.py`, the spec's statistics note, `CHANGELOG.md`.
  - **Tests** (`tests/test_stats.py`):
    - `test_stratified_bootstrap_matches_numpy_on_the_same_resamples`
    - `test_stratified_bootstrap_keeps_each_stratums_size`
    - `test_stratified_bootstrap_with_one_stratum_is_the_plain_bootstrap`
    - `test_weighted_proportion_matches_scipy_beta_at_the_effective_counts`
    - `test_weighted_proportion_with_unit_weights_is_clopper_pearson` (against statsmodels)
    - `test_brier_reliability_matches_an_independent_numpy_implementation`
    - `test_brier_reliability_is_not_estimable_under_30_per_bin`
    - The existing references stay green:
      - `::test_clopper_pearson_matches_scipy_and_statsmodels`
      - `::test_clopper_pearson_at_extreme_levels`
      - `::test_mcnemar_matches_statsmodels_and_exact_arithmetic`
      - `::test_holm_matches_statsmodels`
      - `::test_bootstrap_matches_numpy_and_scipy_on_the_same_resamples`
      - `::test_paired_bootstrap_matches_numpy_and_scipy_on_the_same_resamples`
  - **Registers in:** `CORE` (`tests/conftest.py:71`).
  - **Safe alone because:** nothing calls the new functions, and every committed result recomputes to the same bytes.

- [ ] **17c** Arms by provider class (pure, with one read-only command).
  - **What it builds:**
    - `src/mendari/evals/arms.py` (`mendari.arms/1`):
      - `ARMS`: `deterministic` ("Mendari only"), `classifier` ("+ specialised classifier"), `llm` ("+ general LLM") and `classifier_and_llm` ("+ both").
      - `variant_types(variant)` walks one level of a composite's `members`, as `providers.uses_model` does (`providers/__init__.py:202`); composites cannot nest.
      - `arm_of(types)` uses `providers.MODEL_TYPES` (line 55).
      - `configurations(pack)` is the product of each slot's variants. Each configuration carries its arm, whether it is local (`origin.is_local`, `core/origin.py:297`), and its remote types.
      - `arms(pack)` gives each arm its configurations, or "not applicable".
    - A new `mendari scoreboard` command group, with `mendari scoreboard arms PACK [--json]`.
    - `providers/__init__.py` does not change.
  - **Files:** new `src/mendari/evals/arms.py` and `tests/test_arms.py`; changed `src/mendari/cli.py`, the spec (an Arms section), `README.md` (CLI), `CHANGELOG.md`, `tests/test_cli_errors.py`, `tests/test_read_only_commands.py`.
  - **Tests** (`tests/test_arms.py`):
    - `test_the_five_packs_arms_are_their_manifests`. The table, reproduced on main by walking the manifests:

      | Pack | Deterministic | + classifier | + LLM | + both |
      |---|---|---|---|---|
      | ai-guard | 1 | 2 | 2 | not applicable |
      | clinical-trial-prescreener | 2 (one is `structured=cql`) | 4 | 4 | not applicable |
      | patient-inbox-router | 2 (`router` is `naive_bayes` or `rules`) | 10 | 7 | 6 |
      | prior-auth-preflight | 1 | 2 | 2 | not applicable |
      | referral-router | 1 | 8 | 5 | 6 |

    - `test_an_arm_no_configuration_reaches_is_not_applicable`
    - `test_the_both_arm_exists_only_across_slots`
    - `test_a_composite_member_sets_the_arm`
    - `test_naive_bayes_is_deterministic_and_local_and_cql_is_deterministic_but_remote`
    - `test_every_configuration_is_in_exactly_one_arm`
    - `test_the_arm_agrees_with_uses_model_on_every_variant`
    - The existing `tests/test_shipped.py::test_no_vendor_or_model_name_in_product_code_outside_the_providers` stays green.
  - **Registers in:** `BROKEN['scoreboard arms']` and `test_read_only_commands.COMMANDS`.
  - **Safe alone because:** it is a read-only derivation and a read-only command.

- [ ] **17d** Scoreboard entries (`mendari.scoreboard/1`) and run records (`mendari.run/1`), pure.
  - **What it builds:**
    - `src/mendari/evals/scoreboard.py`, with `entry(ledger, dataset, run, *, arm, configuration, prices, disclosures)`.
      - **What it refuses:**
        - a ledger whose pack digest, dataset name, sha256 or n differ from the dataset's;
        - a run record that disagrees with the ledger: a different n, or per-case token sums that are not the ledger's `u`.
      - **Figures.** Where `result_from_ledger` already computes one, it is reused.
        - **Each critical label:**
          - detected, as raw k of n with its 95% interval;
          - its distinct positives: cases grouped by `canonical.normalised_input` (norm/v1) over the dataset's inputs. A group is positive if any of its cases is, and detected only if every positive case in it is.
          - the grade's bound: Clopper-Pearson at `method.simultaneous_level(L)` over the distinct groups, stored as its own field with its level;
          - its evidence: sufficient when its distinct positives reach `method.min_positives(floor, L)`.
        - **Critical cases automated:** `auto_missed` k of n.
        - **False automation:** wrong AUTO over labelled cases (`result.py:161`).
        - **AUTO error rate:** wrong AUTO over AUTO cases, k of n (from `automated_correct`, `result.py:162`).
        - **Automation and accuracy:** k of n.
        - **Macro-F1:** with its stratified bootstrap interval.
        - **Brier:** with its existing bootstrap interval, and its reliability term, with a bootstrap interval when estimable.
        - **ECE:** equal-mass, a description only, with no interval, never a criterion.
        - **Cost per 1,000 decisions:**
          - priced from the run record's per-case, per-type tokens and a dated price table, with a bootstrap interval;
          - "0 (no metered calls)" for a configuration with no remote call;
          - "unknown" when any remote call failed (a provider error in the ledger) or a model is missing from the table.
        - **Latency p50 and p95:** from the run record, each with a bootstrap interval, labelled "measured on the runner, not reproducible", never ranked.
        - **Subgroups:** the pack's declared subgroups (the ledger's `a`), with accuracy, automation and AUTO error rate as k of n. A cell under `share.MIN_GROUP` (10) reads "too few".
        - **Weighted figures:** when the dataset's records carry `attributes.weight` (hidden sets), Korn-Graubard versions of accuracy, false automation, automation and AUTO error rate, shown beside the unweighted k of n. Recall is never weighted. A public set reads "as sampled".
      - **Scope:**
        - pack id, version and digest;
        - set kind, name, sha256 (or commitment), n and distinct inputs;
        - the models as reported (the ledger's `models`, and the run record by type);
        - the method's version and sha256, and whether it is in effect;
        - `measured_on`, the run record's date;
        - the words "engineering evaluation, not clinical validation";
        - the disclosure;
        - the synthetic-example label (`core/status.py:13`) for a pack that is not clinician-reviewed.
      - **Standing on a public set:** not ranked and not graded, with the reason "public set: regression check, not a ranking".
      - `body_sha256`: the sha256 of the canonical bytes, without any signature.
    - `board(entries, current)`:
      - One table per pack and set.
      - The insufficient-evidence banner shows when any entry's critical label is short of evidence.
      - Each arm reads measured, "not measured" or "not applicable".
      - Freshness is worked out when the board is built, never stored. An entry is stale when:
        - its pack digest, set sha256 or method sha256 is not the current one;
        - a later run of the same provider type reports another model;
        - or 45 days have passed since `measured_on`, shown as "stale after DATE".

        No clock is read.
    - `src/mendari/evals/runs.py`:
      - The `mendari.run/1` record:
        - what it holds: the replicate's number, the engine's version and the date;
        - per case, in the ledger's order: `latency_ms` and each type's `[input, output]` tokens;
        - the model each type reported;
        - never a key, an endpoint or case text.
      - `agrees(run, ledger)`.
      - The price table, `mendari.prices/1`: its date and source, and per provider type and model id the USD per 1,000 input and per 1,000 output tokens. It is cited by its sha256 and refused from under `src/`.
      - The disclosures file, `mendari.disclosures/1`.
    - `render.py` gains `scoreboard_markdown`, through `shown` (95%) and `interval_text`. The grade's bound prints as "lower bound X at Y%, simultaneous across L labels".
  - **Files:**
    - New: `src/mendari/evals/{scoreboard.py, runs.py}`, `tests/test_scoreboard.py`.
    - Changed: `src/mendari/evals/render.py`, the spec (sections for `mendari.scoreboard/1`, `mendari.run/1` and `mendari.prices/1`), `CHANGELOG.md`.
  - **Tests** (`tests/test_scoreboard.py`):
    - `test_a_rebuild_from_the_same_inputs_is_byte_identical`
    - `test_twenty_emergency_positives_show_insufficient_evidence`: inbox `red_flags_holdout` (20 of 20 `urgency=urgent`) with a local Decision, as the existing `tests/test_ranking.py::test_twenty_emergency_positives_say_insufficient_evidence` does.
    - `test_evidence_counts_distinct_positives_never_raw`: ai-guard `test`, `verdict=block`, 164 raw against 30 distinct.
    - `test_every_critical_label_needs_enough_not_just_one`
    - `test_the_grades_bound_is_simultaneous_and_over_distinct_groups`: against scipy and statsmodels at L = 1, 2, 4 and 6.
    - `test_every_figure_is_k_of_n_with_its_interval_or_a_bootstrap_interval`
    - `test_ece_is_described_never_given_an_interval`
    - `test_cost_per_1000_is_from_tokens_and_the_dated_table_or_unknown`
    - `test_a_pack_set_or_built_in_price_is_never_used`
    - `test_latency_comes_from_the_run_record_and_is_never_ranked`
    - `test_a_run_record_must_agree_with_its_ledger`
    - `test_a_run_record_holds_no_key_endpoint_or_case_text`
    - `test_the_scope_names_digest_set_models_method_date_disclaimer_and_disclosure`
    - `test_a_public_set_is_a_regression_check_with_no_rank_and_no_grade`
    - `test_weighted_figures_match_a_hand_computation_and_recall_is_never_weighted`
    - `test_arms_are_measured_not_measured_or_not_applicable`
    - `test_freshness_is_worked_out_when_shown_and_never_stored`
    - `test_a_ledger_of_another_digest_or_dataset_is_refused`
    - The existing `tests/test_ledger.py::test_a_second_run_gives_byte_identical_results_and_ledgers` stays green.
  - **Safe alone because:** it is a new format that nothing calls. `rank/1`, the card and the leaderboards are untouched.

- [ ] **17e** Signing and the public verifier (off without the key).
  - **What it builds:**
    - `src/mendari/evals/signing.py`:
      - **The key:**
        - `KEY_ENV = "MENDARI_GRADING_SIGNING_KEY"`.
        - `grading_key()` follows `checkpoint_key()` (`core/checkpoint.py:45`): unset means off, and a malformed value stops the command with a message that names the variable and no value.
        - It must differ from the raw values of `MENDARI_CHECKPOINT_KEY`, `MENDARI_REGISTRY_SIGNING_KEY` (`hub/yank.py:40`), `MENDARI_PHI_KEY`, each `MENDARI_PHI_READ_KEYS` entry, `MENDARI_INPUT_HASH_KEY` and the three evaluation-set keys.
      - **What is signed:** `sign(result, key)` adds `signature: {key_id, value}` over `b"mendari-grading-result-v1\0"` followed by `ledger.canonical(result without its signature)`, which has `allow_nan=False`. The checkpoint's `_signed` is not reused: it allows NaN and has no prefix.
      - **The verifier:** `problem(result, keys, *, publisher_ids)` names one of these:
        - another format;
        - a method sha256 that is not its version's;
        - an unknown key id, or one outside its valid dates;
        - a key id that is a publisher's (`trusted_keys.json`, `hub/client.py:390`, or the registry's namespace keys);
        - a changed byte;
        - a body that is not canonical.

        `key_id`, `public_text` and `load_public` are reused (`core/checkpoint.py:62, 68, 79`).
    - `core/evalset_keys.py:44 OTHER_KEYS` and `tests/conftest.py:52 LIVE_SETTINGS` gain the key.
    - `scoreboard/keys.json` (`mendari.grading-keys/1`) is empty until the owner adds a public key.
    - Commands:
      - `mendari scoreboard sign FILE`: the key holder's; it refuses without the key, and when `CI` or `GITHUB_ACTIONS` is set.
      - `mendari scoreboard verify FILE [--keys scoreboard/keys.json | --public-key B64]`: needs no secret.
    - `docs/grading/verifying.md`, in the nav.
    - Threat-model rows, using the next free ids at build time:
      - S: a result forged with a publisher or checkpoint key, or replayed across domains.
      - E: the signing key held by a server or by CI.
  - **Files:**
    - New: `src/mendari/evals/signing.py`, `scoreboard/keys.json`, `docs/grading/verifying.md`, `tests/test_scoreboard_signing.py`.
    - Changed: `src/mendari/core/evalset_keys.py`, `src/mendari/cli.py`, `tests/conftest.py`, `tests/test_cli_errors.py`, `tests/test_read_only_commands.py`, `docs/security/threat-model.md`, `mkdocs.yml`, `CHANGELOG.md`.
  - **Tests** (`tests/test_scoreboard_signing.py`):
    - `test_a_signed_result_verifies_and_any_changed_byte_fails` (the prompt's tampering test)
    - `test_a_result_signed_by_a_publisher_key_is_refused`, with a `hub/archive.py` key.
    - `test_a_checkpoint_statement_or_archive_signature_never_verifies_as_a_result`
    - `test_the_key_must_differ_from_every_other_key`
    - `test_unset_means_off_and_a_malformed_key_stops_naming_no_value`
    - `test_nan_or_a_non_canonical_body_is_never_signed`
    - `test_verifying_needs_no_secret`
    - `test_signing_refuses_to_run_in_ci`
    - They follow the patterns of `tests/test_signed_checkpoint.py::test_a_statement_names_the_head_and_the_day_and_nothing_can_change_unnoticed` and `tests/test_registry_yank.py::test_a_forged_or_replayed_yank_is_refused`.
  - **Safe alone because:** nothing is signed unless the key holder runs `sign`, no server reads the key, and the verifier reads public data only.

- [ ] **17f** The results store and the early scoreboard (stage 3).
  - **What it builds:**
    - `src/mendari/evals/results_store.py`. It stores:
      - results as `scoreboard/results/<body sha256>.json`;
      - ledgers as `scoreboard/ledgers/<id>.jsonl` (`Ledger.file_name`);
      - run records as `scoreboard/runs/<sha>.json`.

      `add` refuses to overwrite. `supersedes` must name an earlier result of the same pack, set, arm and configuration. `heads` lists the current results.
    - Commands:
      - `mendari scoreboard run PACK --dataset NAME [--config slot=variant ...]` runs one configuration over a public set and writes its ledger, its run record (latency and tokens from each `DecisionResult`) and its entry. In this PR it refuses every remote configuration (17h lifts that outside CI), and it refuses any hidden set.
      - `mendari scoreboard add FILE [--supersedes HASH]` stores a result made elsewhere: 17k's answers.
    - `scripts/build-scoreboard.sh` runs every local configuration on each pack's public scoring sets:
      - Local configurations (6): ai-guard 1, the prescreener's `structured=criteria`, inbox 2, prior-auth 1 and referral 1.
      - Public scoring sets (15): each pack's `test`, its enriched set and that set's holdout.

      That gives 18 committed entries, all unsigned. The prescreener's `structured=cql` reads "not measured" until 17h.
    - `scripts/check_scoreboard.py --base REF` checks that:
      - each name is its body hash;
      - nothing under `scoreboard/results/` is removed or changed against the base, except a valid signature added to an unsigned result whose body is the same;
      - each public result recomputes byte for byte from its committed ledger, its dataset (sha256 checked), its run record, its price table and the method;
      - each local ledger on its pack's current digest re-runs to the same bytes, while a ledger on an older digest is kept and not re-run;
      - `supersedes` links name earlier results and form a forest;
      - each signature verifies against `scoreboard/keys.json`, and a publisher key's signature is refused;
      - each hidden-set result fits its closed schema (from 17k on).
    - `scripts/scoreboard_page.py [--check]` writes `docs/grading/scoreboard.md` from the store. For each pack and set, the page shows:
      - a table with its scope statement;
      - the banner where evidence is short;
      - the arms, as measured, "not measured" or "not applicable";
      - the marks "unsigned", "method draft, not in effect" and "stale after DATE" (or "stale: pack changed");
      - no Rank column, no "ranked" and no bare percentage.

      Its bytes depend only on the repository.
    - CI: two steps in the required `reference-pack` job (`.github/workflows/ci.yml:103`). The step fetches the base commit (the PR's base, or `github.event.before` on a push).
      - `check_scoreboard.py --base`
      - `scoreboard_page.py --check`
    - Docs:
      - nav entries "Scoreboard (public sets)" and the runbook `docs/operations/scoreboard.md`;
      - `scoreboard/README.md`;
      - `README.md` "Project layout";
      - the spec.
    - Threat-model rows:
      - T: a stored result edited or removed.
      - R: a superseded result disowned.
    - What the page shows today, on distinct positives:
      - referral-router `test` is the only set with enough evidence (`urgency=urgent` 113, `next_step=urgent_review` 124);
      - every other set shows the banner. For example: ai-guard `test` (block 30, emergency 39, unsafe 30, dose 4 distinct), inbox `test` (23), prior-auth `test` (25), the prescreener `test` (at most 29), and every enriched set (at most 28).
  - **Files:**
    - New: `src/mendari/evals/results_store.py`, `scripts/{build-scoreboard.sh, check_scoreboard.py, scoreboard_page.py}`, `scoreboard/{README.md, results/, ledgers/, runs/}`, `docs/grading/scoreboard.md`, `docs/operations/scoreboard.md`, `tests/test_scoreboard_store.py`, `tests/test_scoreboard_page.py`.
    - Changed: `src/mendari/cli.py`, `.github/workflows/ci.yml`, `mkdocs.yml`, `docs/security/threat-model.md`, `README.md`, `CHANGELOG.md`, `tests/test_cli_errors.py`.
  - **Tests:**
    - `tests/test_scoreboard_store.py`:
      - `test_a_result_is_stored_once_by_its_body_hash`
      - `test_an_edited_or_removed_result_fails_the_check`
      - `test_only_adding_a_valid_signature_may_change_a_result`
      - `test_supersedes_names_an_earlier_result_of_the_same_entry`
      - `test_each_committed_result_is_what_its_inputs_give`
      - `test_a_ledger_on_an_older_digest_is_kept_and_not_rerun`
      - `test_running_a_remote_configuration_without_replicates_is_refused`
    - `tests/test_scoreboard_page.py`:
      - `test_the_page_is_regenerated_from_the_store`
      - `test_the_early_scoreboard_is_unranked_with_intervals_banner_and_scope`
      - `test_all_five_packs_appear_on_every_public_scoring_set`
      - `test_the_page_has_no_bare_percentage_rank_or_ranked`
      - `test_unsigned_and_draft_are_said_on_the_page`
    - The existing `tests/test_site_notice.py` passes, and `mkdocs build --strict` passes.
  - **Safe alone because:** it works offline and its output is deterministic. The card, the dashboard and the leaderboards do not change, and CI only recomputes.

- [ ] **17g** Public template sets relabelled: the card, the dashboard and `web/`. This is a display format bump.
  - **What it builds:**
    - `core/card.py`, lines 255 to 268:
      - The caption becomes "public set: regression check, not a ranking".
      - The section links `docs/grading/scoreboard.md`.
    - `evals/leaderboard.py:207 markdown()` loses its Rank column. Rows stay in `rank/1` order, documented as a listing order.
    - `dashboard/leaderboard.js`:
      - The "Ranked" caption goes (line 38), and so does the rank column (line 119).
      - The chart's naming rule, `r.rank === 1` (line 145), becomes "deployed, or not beaten".
    - The five `DECISION_CARD.md` files are regenerated with `mendari card --write`. Data, models and benchmarks stay byte-identical.
    - `web/index.html`, lines 98 to 122:
      - The typed table is removed: bare percentages, `class="best"`, vendor names, "100 cases" and "pack 0.3–0.4".
      - In its place, a block between markers that `scoreboard_page.py` generates and `--check` checks: inbox `test`, every arm, measured entries as k of n with intervals and the banner, and every other arm "not measured".
      - It names no remote model until 17i lands and a disclosure exists. Deploying it stays the owner's (after gate G1-2's `patent-decision`, owner decision 11).
    - The spec's §Leaderboards. The data (`rank`, `standing`) and the API do not change, and `tests/test_server.py:149` stays.
  - **Files:** `src/mendari/core/card.py`, `src/mendari/evals/leaderboard.py`, `src/mendari/dashboard/leaderboard.js`, `packs/*/DECISION_CARD.md`, `web/index.html`, `scripts/scoreboard_page.py`, `docs/spec/evaluation-reports.md`, `README.md`, `CHANGELOG.md`.
  - **Tests:**
    - Updated: `tests/test_leaderboard.py::test_markdown_and_card`.
    - Changed in `tests/browser/test_dashboard.py`:
      - `test_a_ranked_board_shows_its_ranks_and_keeps_them_when_sorted` becomes `test_a_template_board_shows_no_rank_and_keeps_its_order_when_sorted`.
      - `test_the_leaderboard_keeps_rank_and_configuration_in_view_on_a_phone` becomes `test_the_leaderboard_keeps_the_configuration_in_view_on_a_phone`.
      - `test_the_leaderboard_shows_the_one_ranking_and_marks_the_deployed_row` becomes `test_the_leaderboard_shows_its_listing_and_marks_the_deployed_row`.
    - New:
      - `tests/test_ranking.py::test_no_card_or_board_prints_a_rank_for_a_public_set`
      - `tests/test_web_scores.py::test_the_sites_scores_are_the_stores`
      - `tests/test_web_scores.py::test_the_site_shows_no_bare_percentage_or_best_row`
    - Must stay green: `tests/test_ranking.py::test_the_reference_cards_hold_no_bare_percentage`, and `tests/test_server.py:149`.
  - **Safe alone because:** only the display changes, and no decision does. The PR states that the comparison sections of all five published cards become stale (Definition of shipped §9).

- [ ] **17h** Remote arms on public sets: replicates, the flip rate, model freshness, the canary and disclosures. Built, but off.
  - **What it builds:**
    - `mendari scoreboard run`:
      - It accepts remote configurations, but refuses to start when `CI` or `GITHUB_ACTIONS` is set.
      - `--replicates N` must be 3 to 5 for a remote configuration, and is 1 for a local one.
      - `--prices FILE` is required for a remote configuration.
      - Replicate 1 is the headline, as declared before the run.
    - `scoreboard/declared/<pack name>.json` (`mendari.declared/1`): one configuration per arm, committed before any run. Each entry cites its sha256, and a run whose configuration is not declared is refused.
    - `runs.replicates(...)`:
      - the spread: the minimum to maximum of each figure across replicates;
      - the flip rate: k of n cases whose primary prediction or outcome differs between any two replicates, with its interval. Cases are matched by ledger hash; a public set's case key comes from its bytes, so the hashes line up.
    - Freshness: another reported model, in a later run or a canary of the same type, marks the arm stale.
    - `mendari scoreboard canary PACK --config ... --size 30 --seed S`:
      - It is manual, runs outside CI only, and has no schedule.
      - It runs a seeded draw from `test`.
      - Drift: the canary's flip-rate lower bound is above the replicates' upper bound, or the canary reports another model. Either marks the arm stale.
    - Disclosures: an entry with remote calls whose provider type has no statement in `scoreboard/disclosures.json` reads "not published: disclosure pending". Its scope is shown; its figures are kept off the page and `web/`.
    - The build session measures the prescreener's `structured=cql` entries on its three public sets with Mendari's own CQL engine container (docker compose's `fhir` service, as the `cql` CI job runs it), after checking which container owns the port. This needs no key and no spend. The ledgers and run records are committed.
  - **Files:** `src/mendari/evals/{runs.py, scoreboard.py}`, `src/mendari/cli.py`, `scripts/scoreboard_page.py`, `scoreboard/{disclosures.json, declared/}`, the new `scoreboard/` cql entries, `docs/operations/scoreboard.md`, the spec, `README.md`, `CHANGELOG.md`, `tests/test_scoreboard_runs.py`.
  - **Tests** (`tests/test_scoreboard_runs.py`; fakes through `tests/conftest.py:190 calm_jev` and `tests/test_llm.py:73 install`):
    - `test_replicates_report_their_spread_and_flip_rate`
    - `test_a_local_configuration_has_one_replicate_and_no_flips`
    - `test_a_remote_entry_needs_three_to_five_replicates`
    - `test_the_headline_is_the_declared_first_replicate`
    - `test_a_configuration_not_declared_before_its_run_is_refused`
    - `test_a_run_refuses_to_start_in_ci`
    - `test_another_reported_model_marks_the_arm_stale`
    - `test_canary_drift_marks_its_arm_stale`
    - `test_the_canary_never_runs_in_ci`
    - `test_an_undisclosed_provider_is_not_published`
    - The existing `tests/test_isolation.py::test_the_tests_reach_nothing_the_shell_names` stays green.
  - **Safe alone because:** no remote ledger is committed, so every remote arm still reads "not measured", and the runner refuses to start in CI.

- [ ] **17i** (the owner's run, data only) Remote-arm ledgers on the public `test` sets.
  - **What it builds:** the owner runs 17h's command once, outside CI, with the deployment's keys:
    - one declared configuration per remote arm;
    - Jev's deployed model, and at most two LLM model ids;
    - 3 replicates, on the `test` sets only.

    The owner commits the ledgers, the run records, a dated price table and the disclosures, regenerates the page, and signs. CI only recomputes. Decisions per pack (checked on main), 31,500 in all:

    | Pack | Decisions |
    |---|---|
    | ai-guard | 5,400 |
    | clinical-trial-prescreener | 2,700 |
    | patient-inbox-router | 9,000 |
    | prior-auth-preflight | 5,400 |
    | referral-router | 9,000 |

  - **Safe alone because:** it adds data only. It needs owner decisions 6 and 7.

- [ ] **17j** Grades and band ranking (pure).
  - **What it builds:**
    - `src/mendari/evals/grade.py`, with `attestations(entry)`. Each attestation is met, not met or insufficient evidence:
      - **No critical case automated.**
      - **Each critical label's floor:** its simultaneous lower bound, over distinct groups, at or above the floor.
      - **The false-automation ceiling:** the 95% upper bound at or below the ceiling.

      "Insufficient evidence" means that even a perfect result on these n could not meet the attestation. For example: distinct positives under `min_positives`, or fewer than 183 labelled cases for the 0.02 ceiling. There is no letter and no composite. While the method is not in effect, every attestation is marked "draft".
    - `src/mendari/evals/bands.py`:
      - **The band tuple**, from the method's bands:
        - the worst critical label's simultaneous lower bound;
        - the false-automation upper bound;
        - accuracy's lower bound;
        - Brier's upper bound;
        - the cost-per-1,000 band.
      - **The order:**
        - An arm that fails a safety attestation (critical automated, or a recall floor), or lacks the evidence, is listed unranked with its reason.
        - Equal tuples share a competition rank (1, 2, 2, 4), listed by name.
        - ECE and latency never enter the order.
      - **The Holm family**, per pack and set: every pair of ranked arms by each critical label's recall (exact McNemar on that label's positives), by false automation and accuracy (exact McNemar), and by Brier (the paired bootstrap's p-value). It runs through `stats.holm_family` (line 514), and the output names the family and its size. `paired.py` keeps its own family, and `mendari.compare/1`'s bytes do not change.
      - **Rank intervals**, from the Holm decisions:
        - Arm j is significantly ahead of arm i when their tuples differ and the family's test on the first criterion where they differ rejects in j's favour.
        - The lower bound is 1 plus the number of arms significantly ahead; the upper bound is the number of arms minus the number significantly behind.
        - The tests never reorder the ranks, and nothing is resampled.
    - Wired into `board`: on a public set, `ranking: {status: "off", reason: "public set: regression check, not a ranking"}` and `grade: null`.
  - **Files:** new `src/mendari/evals/{grade.py, bands.py}` and `tests/test_scoreboard_rank.py`; changed `src/mendari/evals/scoreboard.py`, the spec, `README.md`, `CHANGELOG.md`.
  - **Tests** (`tests/test_scoreboard_rank.py`):
    - `test_the_band_order_is_a_total_preorder_on_generated_arms` (hypothesis, as `tests/test_provider_configs.py` uses it)
    - `test_equal_band_tuples_share_a_rank`
    - `test_an_arm_failing_a_safety_floor_or_short_of_evidence_is_listed_unranked`
    - `test_ece_and_latency_never_enter_the_order`
    - `test_the_holm_family_is_every_pair_by_every_criterion_and_label_and_matches_statsmodels`, against `multipletests(method="holm")`
    - `test_rank_intervals_match_hand_computed_references`
    - `test_tests_never_reorder_ranks`
    - `test_attestations_are_met_not_met_or_insufficient_never_a_letter_or_score`
    - `test_an_attestation_is_insufficient_when_a_perfect_result_could_not_meet_it`
    - `test_a_rerun_from_the_same_ledgers_and_seeds_is_byte_identical`
    - `test_nothing_is_ranked_or_graded_on_a_public_set`
  - **Safe alone because:** it is pure, and every surface says ranking is off.

- [ ] **17k** The hidden-set scoreboard, through the existing grading route.
  - **What it builds:**
    - **The request.** `GradingRequest` (`server/app.py:699`) gains an optional `arms: {arm: {slot: variant}}` and `declared: sha256`. Without `arms`, the route answers `mendari.hidden_result/1` exactly as today.
    - **Checks before any case is read.** Each declared configuration goes through `grading_decision` (line 2942) and is refused as today: `wrong_pack`, `unknown_variant` or `remote_provider`. Arms that are not declared read "not graded here" when their configurations are remote, or "not applicable".
    - **One grading read** (`core/evalsets.py:1234 grade_read`): `run(dataset)` decides every declared arm on the same in-memory records.
      - They share one case key (line 1336), so their ledgers pair.
      - It is one chained `evalset.graded`, and one of `max_runs`.
      - `core/evalsets.py`, `GradingRead` and the chain payload do not change.
    - **Computed in memory, never stored:**
      - ledgers (`ledger.build`);
      - entries (17d), from the records' inputs (distinct groups) and their `attributes.weight` (aligned to the ledger's order);
      - the attestations and the ranking (17j).

      Ranking reads "off: one arm graded" while only the deterministic arm is local. Grades read "draft: method not in effect" until it is.
    - **The answer** is a closed `mendari.hidden_scoreboard/1` in `evals/hidden.py`, built from `share.py`'s models.
      - What it holds:
        - per arm: the 17d figures, the attestations, the evidence and the scope (with the set's commitment, never a hash);
        - the ranking and the family;
        - the set id, its commitment and n;
        - `run` and `runs_left`, in a run block outside the body's hash.
      - What it never holds:
        - cost, latency or subgroups;
        - equal-mass bins (ECE as a single number only);
        - any case id or hash, the dataset's name or hash, or the ledger's id.
    - **Two reads** of one frozen set give the same body.
    - **Run time.** Measure the run time on a 600-case synthetic set. If it exceeds the route's deadline, the run moves to a key holder's command, `mendari evalset grade`, with the same chained read. A figure is never dropped.
    - **Docs:**
      - The runbook: post the committed declaration, `scoreboard add` the answer, and the owner signs.
      - A spec section.
      - ADR-0011 notes, and `docs/operations/evalsets.md` at lines 344 and 410: hidden sets grade local providers only (owner decision 2), and freeze commitments stay unsigned because a signed result carries the commitment.
      - `server/api_docs.py`.
      - Threat-model rows:
        - I: a hidden set learned through scoreboard answers;
        - I: selection and ranking on the same cases.
  - **Files:**
    - Code: `src/mendari/server/{app.py, api_docs.py}`, `src/mendari/evals/{hidden.py, scoreboard.py}`, `scripts/check_scoreboard.py` (the closed-schema check).
    - Docs: `docs/spec/evaluation-reports.md`, ADR-0011, `docs/operations/{evalsets.md, scoreboard.md}`, `docs/security/threat-model.md`, `README.md`, `CHANGELOG.md`.
  - **Tests** (`tests/test_evalset_scoreboard.py`):
    - `test_every_declared_arm_is_decided_on_the_same_cases_in_one_read`
    - `test_a_declared_remote_arm_is_refused_before_any_case_is_read`
    - `test_undeclared_arms_say_not_graded_here_or_not_applicable`
    - `test_the_scoreboard_answer_holds_no_per_case_field`
    - `test_a_set_with_twenty_urgent_positives_says_insufficient_evidence`
    - `test_weights_reach_prevalence_dependent_figures_and_never_recall`
    - `test_two_reads_of_a_set_give_the_same_body`
    - `test_no_grade_is_final_before_the_method_is_in_effect`
    - `test_without_arms_the_answer_is_unchanged`
    - `test_audit_verify_passes_after_a_scoreboard_run[sqlite, postgres]`
  - **Registers in:**
    - `tests/test_evalset_leaks.py`: `aggregates_of` covers the new answer, and its refusals stay `wordless`.
    - These stay green:
      - `::test_no_answer_log_export_or_command_shows_a_hidden_set`
      - `tests/test_evalset_grading.py::test_the_result_holds_no_per_case_field`
      - `::test_a_remote_variant_is_refused_before_any_case_is_read`
      - `::test_runs_are_counted_and_the_rotation_rule_refuses`
    - The `tests/test_shipped.py` `CHAINED` row does not change: the same route chains the same events.
  - **Safe alone because:**
    - The old answer stays the default.
    - Providers are local only, and the answer holds aggregates only.
    - Each run is one chained read.
    - Only the operator's command mints a grading key, and production has no evaluation-set keys.

- [ ] **17l** The developer flow. Blocked on LP-20 and owner decisions 4 and 10.
  - **What it builds:**
    - `src/mendari/evals/split.py`:
      - `SPLIT_VERSION = "split/v1"`.
      - The key is HMAC-SHA256 under the freeze salt, over `b"mendari-evalset-split-key-v1\0"`.
      - Each case is ordered by HMAC(key, its UUID), within strata of critical-label membership, at fraction 0.5 with a pinned rounding rule.
      - It is computed in-process inside `frozen_holds` (`core/evalsets.py:2758`). It never reaches `GradingRead`, the chain, a log or an answer, and it never uses the index key or `case_hash`.
    - A `submission` key role:
      - for one organization and one pack lineage, for at most 365 days;
      - minted only by `mendari access submission-key`, as `grader-key` is (`cli.py:918`);
      - ADR-0004 is amended.
    - A budget per organization, lineage and set rotation, counted from chained events, inside `max_runs`.
    - `POST /api/evalsets/{set_id}/submissions`: a store version (LP-20), local providers only, answered with bands and attestation states only, on the selection part.
    - A confirmation that scores the ranking part once per set, lineage and version. It chains `evalset.confirmed` and answers 17k's result on that part only.
    - The caller's own run log.
    - It may land as two PRs: first the split, the credential and the budget, with no route; then the routes.
  - **Files:** `src/mendari/evals/split.py` (new), `src/mendari/core/{evalsets.py, audit.py}`, `src/mendari/server/{app.py, auth.py, limits.py}`, `src/mendari/cli.py`, ADR-0004 and ADR-0013, the spec, `docs/security/threat-model.md`.
  - **Tests** (`tests/test_evalset_submissions.py`):
    - `test_a_submission_answers_bands_only`
    - `test_the_budget_per_organization_and_lineage_refuses_the_next_run`
    - `test_the_split_comes_from_the_freeze_salt_and_survives_an_index_key_rotation`
    - `test_the_split_is_stratified_by_critical_label_and_its_bytes_are_pinned`
    - `test_selection_and_confirmation_never_share_a_case`
    - `test_the_confirmation_part_is_scored_once`
    - `test_no_split_key_or_part_leaves_the_process`
    - `test_every_run_is_in_the_log`
    - `test_audit_verify_passes_after_a_submission[sqlite, postgres]`
  - **Registers in:** the leak sweep, `CHAINED`, `test_auth`, `test_two_organizations` and `test_limits.EXPECTED_CLASSES`. Threat-model rows: D (submission flooding) and I (the banded answer as an oracle, bounded by the budget).
  - **Safe alone because:** it needs LP-20's store, the operator's key and the evaluation-set keys, and no answer holds more than bands.

- [ ] **17m** Close-out.
  - **What it builds:**
    - `launch/gates.yaml`:
      - `a9-scoreboard`: the pending probe is replaced by tests from 17b, 17d, 17e, 17f, 17j and 17k, plus the existing no-leak tests. It keeps `pending: "LP-17l, the developer flow, after LP-20 and owner decisions 4 and 10"`.
      - `grading-method`: the pending probe is replaced by the 17a tests and `tests/test_scoreboard_page.py::test_the_page_is_regenerated_from_the_store`. The `file:` probe stays, and the attestation stays empty.
    - The threat model's residual risks:
      - a signed hidden-set result is the owner's attestation and cannot be recomputed by anyone else;
      - raw intervals on duplicated template sets look narrower than they are;
      - the key holder could sign an altered result.
    - ADR-0013 becomes Accepted.
    - The complete owner's runbook: key, prices, disclosures, declarations, replicates, signing, the effective date and publishing.
    - `README.md`, `CHANGELOG.md`, the todo build log, and one owner line for each owner action below.
  - **Tests:** `tests/test_scoreboard_end_to_end.py::test_a_local_run_to_a_signed_verified_result`:
    - `scoreboard run`, `add`, `sign` with a throwaway key, `verify`, and tampering that fails;
    - then a hidden-set run over HTTP, `add`, and `mendari audit verify --evalsets`, on SQLite and Postgres.
  - **Safe alone because:** it adds docs, gate probes and one test.

## Conflicts written before building, and the default each PR follows

1. **(17d, 17g, 17j) `rank/1` against bands.**
   - What `rank/1` does today (`ranking.py:72-83`): it ranks lexicographically on raw bounds, `auto_missed` first, with a strict name tiebreak. It has no Brier, no bands, no shared ranks and no intervals.
   - Default: `mendari.rank/1` stays as the leaderboard's data and listing order. The scoreboard ranks only under `mendari.grading/1.0`, and only on hidden sets. Changing `rank/1` would make every published card stale (Definition of shipped §9).
2. **(17d, 17f) Public sets are never ranked or graded.**
   - Every shipped dataset is public, in the repository and in the wheel.
   - No public record carries a `template` or `source` attribute (checked on main), so a template set cannot be told from a hand-written one.
   - Default: every public set is "public set: regression check, not a ranking". The acceptance line "the scoreboard for all five packs on public sets" is met by the unranked early scoreboard (A9.data-1, requirement 14).
3. **(17d) "Any" against "each" critical label.** `ranking.py:106` ranks a group when any label reaches 100. Default: each label.
4. **(17d, 17k) Raw against distinct positives** (LP-11 owner decision 5).
   - Default: evidence is decided on distinct positives under norm/v1.
   - The grade's bound is computed over distinct groups: a group is detected only if every positive case in it is.
   - Figures stay as raw k of n, with the distinct count beside them. `mendari.eval/2` does not change.
   - On main, only referral-router `test` has enough on every label (113 and 124). Those counts support a recall floor of at most 0.962 at L = 2: 113 of 113 gives 0.9620, and one miss gives 0.9449.
5. **(17a, 17d) A one-sided or two-sided bound at 1 − 0.05/L.**
   - Default: two-sided, as written, which is conservative.
   - Display stays k of n with its 95% interval, as Definition of shipped §3 says. `render.shown`, the JavaScript ports and `tests/fixtures/shown.json` do not change.
   - The grade's bound is a separate field that names its level. `Proportion.to_dict` does not change: adding `level` would change every benchmark's bytes.
6. **(17j) The Holm family against ranking by bands.**
   - Default: the tests sit beside the rank and set the rank intervals; they never reorder the ranks.
   - Brier's paired-bootstrap p-value joins this family.
   - `paired.compare` keeps Brier outside its own family (lines 188 to 201) and does not change.
7. **(17j) "Then cost" against "equal band tuples share a rank".**
   - Default: cost is banded and is the tuple's last element.
   - On hidden sets every graded configuration is local, so its cost is 0 by construction (no metered call). The spec still keeps cost out of the hidden-set answer.
8. **(17k) Remote providers on hidden sets** (LP-11 conflict 52 and owner decision 4).
   - `hidden.graded` (line 193) and `grading_decision` refuse them.
   - Default: local providers only. A remote arm reads "not graded here", which is not "not applicable".
   - "Rank only on hidden sets" plus "local only" leaves one arm per pack, so ranking is built but off until owner decision 2.
9. **(17c, 17h) Arm classes.**
   - `naive_bayes` is deterministic and local.
   - `cql` is deterministic but remote: Mendari's own CQL engine at `MENDARI_CQL_URL`, not a model provider. The build session measures it with Mendari's container, and hidden sets never run it.
   - "Specialised classifier" means `jev`.
   - The "both" arm exists only across slots: in the inbox and referral packs.
10. **(17k, 17l) Selection.**
    - Default: method 1.0 takes the prompt's declared alternative for Mendari's own grading. Each arm's configuration is declared in `scoreboard/declared/` before grading, and the result cites its sha256. Nothing is selected, so the whole set is reported.
    - The salted split comes with the developer flow (17l):
      - it comes from the freeze salt, in-process;
      - `GradingRead` does not change;
      - it never uses the index key or `case_hash`;
      - its confirmation part is scored once.
    - Halving the set for declared configurations would halve the evidence for nothing.
11. **(17k) Each grading read has its own random case key** (`evalsets.py:1336`), so two reads cannot be paired. Default: every declared arm is decided inside one read, which uses one of `max_runs`.
12. **(17d, 17k) Weights.**
    - `ledger.build` keeps only the pack's declared subgroups, so it drops the weight and stratum.
    - Default: no ledger format change. The weights are read from the dataset's records, aligned to the ledger's order.
    - Korn-Graubard figures sit beside the unweighted k of n, never in place of them. Recall attestations stay exact Clopper-Pearson.
    - Public sets are "as sampled".
13. **(17d) The ledger has no inputs, no weights, no per-type tokens and no latency.**
    - Default: `mendari.ledger/1` does not change. An entry is a pure function of five committed inputs: the ledger, the dataset (by sha256), the run record, the price table and the method.
    - A hidden-set entry cannot be recomputed by anyone else, since no ledger is kept (LP-11 conflict 51). Its determinism is tested on synthetic frozen sets.
14. **(17d) Latency is not reproducible.** Default: per-case latency is a committed input in the run record. It is recomputed from that record, labelled "measured on the runner, not reproducible", never ranked, and never on a hidden set.
15. **(17d, 17k) Subgroups on hidden sets.** Requirement 4 asks for them; LP-11 conflict 51 and the spec ban them. Default: none on hidden sets. Public sets show the pack's declared subgroups, with cells under 10 marked "too few".
16. **(17d, 17h) The price table names vendors**, and `test_no_vendor_or_model_name_in_product_code_outside_the_providers` scans `src/mendari`.
    - Default: `scoreboard/prices/<YYYY-MM-DD>.json`, outside product code, cited by its sha256.
    - Never Jev's undated constant (`jev.py:70`), and never a price a pack sets (`jev.py:89`).
    - Cost is "unknown" when any remote call failed or a model is missing from the table, as `tests/test_usage_ledger.py::test_a_cost_is_unknown_unless_every_call_was_priced` rules elsewhere.
17. **(17e) "Grading key" means two things.** LP-11's is a bearer API key; LP-17's signs.
    - Default name: `MENDARI_GRADING_SIGNING_KEY`.
    - It is domain-separated and signs `ledger.canonical`.
    - It is distinct from every other key, held offline only, and never a server or CI setting.
    - It does not sign freeze commitments (ADR-0011, lines 152 and 183): a signed result carries the commitment instead.
18. **(17f) CI has no key.** Default: results committed by a build session are "unsigned" and marked so on the page. The owner signs them locally. The store check allows only that one change to a stored file.
19. **(17h, 17i) Replicates and live calls.**
    - Default: the owner runs them from the command line, outside CI.
    - The route cannot run a 600-case set: `REMOTE_EVAL_LIMIT = 200` (`app.py:331`).
    - The headline is replicate 1. The spread is the minimum to maximum, and the flip rate is k of n with its interval.
20. **(17f) The stale shipped leaderboards.** All five are `mendari.leaderboard/1`: 166 rows, n of 100 or less, no ledgers, mostly older digests. Default: they are never reused, and stay as the packs' own regression record, relabelled in 17g.
21. **(17f) Where the store lives.**
    - Not in `packs/<pack>/evals/`: that directory is outside the digest (`pack.py:50`) but inside the wheel and any archive, so a publisher's pack could carry a forged scoreboard file, and vendor model ids would ship.
    - Not in `docs/`: a stray `.md` file fails `mkdocs --strict` (`omitted_files: warn`).
    - Default: `scoreboard/` at the repository root, outside the wheel, the sdist and the site. The site page is generated from it.
22. **(17f) Required checks.** Ruleset 24751249 requires `reference-pack` but not `docs`. Default: the store and page checks are steps in `reference-pack`, so the ruleset does not change.
23. **(17f) A pack change makes its entries stale.**
    - The check re-runs only the ledgers on the current digest. Older entries are kept and shown as "stale: pack changed".
    - Refreshing them is `scripts/build-scoreboard.sh`, never `build-reference-pack.sh`: run records hold latency, which no rebuild reproduces.
    - The PR names the stale entries, as Definition of shipped §9 asks.
24. **(17d) Comparisons on public sets.** Default: no paired tests on public boards in method 1.0. Each entry stands alone with its intervals, and comparisons that decide anything are made on hidden sets (A9.data-1).
25. **(17d, 17k) ECE.** Default: equal-mass, a description only, with no interval and no place in the order. Equal-width ECE is not shown. On hidden sets it is a single number, never bins: the spec keeps equal-mass bins out of hidden answers.
26. **(17d) "False automation" means two things.** `result.py:161` divides wrong AUTO by labelled cases; lines 174 and 247 divide it by AUTO cases. Default: the first is "false automation" and the second is "AUTO error rate", with its n. The method defines both.
27. **(17g) Template sets are ranked today**, with a Rank column (`card.py:255-268`, `leaderboard.py:207`, `leaderboard.js:38, 119, 145`). Default: the column goes from every surface, as a display bump. The data and the API keep `rank` and `standing`.
28. **(17g) `web/index.html:98-122`** has typed percentages, a "best" row, vendor names and stale versions (todo, lines 2544 and 2545). Default: the block is generated from the store and checked. Deploying it stays the owner's (after G1-2's `patent-decision`, owner decision 11).
29. **(17g) A dashboard scoreboard screen.**
    - The server cannot read a store at the repository root from an installed wheel.
    - A hosted view would serve unsigned files and ship vendor ids.
    - `tests/test_invite_page.py:161` bans the word "grading" in dashboard sources.
    - Default: no new screen. The card and the dashboard link the docs page, with the word "scoreboard". The Hub page waits for LP-19.
30. **(17a, 17m) The public method URL** needs the docs site, which the owner publishes. Default:
    - The method stays a draft with `EFFECTIVE = None`.
    - Any edit re-pins its hash, and a result made under an earlier hash is stale.
    - The effective date is the owner's PR, at least 30 days after the URL is attested (B30).
31. **(17a, 17j) Floors fixed before data.**
    - Default: one floor for every critical label, one ceiling and the bands are method parameters, pinned by the document's hash before any hidden set is graded.
    - A different floor for one pack needs a method version.
    - Floors are grading parameters, never policy: `core/policy.py` and the packs are untouched (ADR-0002).
32. **(17k) Bootstrap time inside a request** (LP-11 conflict 43). Default: measure it. If the run is too slow, move it to a key holder's command with the same chained read, rather than drop a figure.
33. **(17d, 17h) The disclosure field.** Default: a configuration with no remote calls reads "no model provider". An entry with remote calls is withheld until its provider type has a statement (todo, lines 2538 to 2540).
34. **(17h) A scheduled re-run would be a live call** from LP-21n's hosted schedule (#217, merged). Default: no schedule. Freshness is worked out when the page or board is built, and the canary is run by hand.
35. **(17k) Binding a hidden answer to its run.** Default: no new event kind. A signed result cites the set, its commitment, the pack digest and the run number of the chained `evalset.graded`. The owner's attestation covers the numbers; this is a residual risk in 17m.
36. **(17l) The developer flow.**
    - The route grades only packs the server serves (`packs.get`).
    - Its answer is the full aggregate result, and grading keys are minted by the operator.
    - Default: the `/runs` route stays the operator's tool. Submissions get an operator-minted `submission` key and bands only, after LP-20.
37. **(17e) Key distinctness.** Nothing checks the registry key against the checkpoint key today. Default: 17e checks only the new key against all the others. The older pair is noted and left out of scope.
38. **(17e to 17l) Threat-model ids may collide** with LP-11o and LP-18h. Default: each PR claims the next free ids when it is built. On main those are S18, T7, R3, I14, D11 and E12.

## Owner decisions still open

**2026-11-07:** the owner wrote, in chat, "the patent gate is clear and I approve everything on my
part". That confirms the default of decisions 1, 2, 3, 5 (the numbers and the 30 days' rule; the
effective date itself is still the owner's to set), 6, 7, 9, 10 and 11, and of the conflicts
above. Still open: 4, which has no house default, and 8, whose rule (no remote entry published
until its type has a statement) is built while the statements are the owner's to write. Whether
11 waits for all of G1-2 or only its `patent-decision` was the owner's to say (no default),
answered below. The owner's actions below stay the owner's.

**2026-11-07, the owner's picks:** decision 4 takes the suggested numbers: at most 20 grading runs
and 180 days per hidden set, 3 submissions per organization and pack lineage per rotation, the
confirmation once per version (= LP-11 decision 2). Decision 11 waits for gate G1-2's
`patent-decision` only, not all of G1-2, and the owner asked Claude to run the production deploy
of `web/` once that attestation is in and main is green. For 17i (decision 7) the LLM
arm's models are OpenAI's (the owner: "OpenAI"); the exact model ids, at most two, are still
needed before any spend. Decision 8 stays open.

1. **Which parts are built in a private repository:** the runner, its provider budget, the signing key. Default: none. All the code is open; only the key (offline), hidden-set content (in the database) and the budgets (in the environment) stay out of it.
2. **Remote providers on hidden sets.** Default: local only for method 1.0. Revisit with a data agreement per provider, or a private runner. Until then, nothing is ranked on hidden sets.
3. **Raw or distinct positives in the evidence rule.** Default: distinct, under norm/v1, with the conservative group rule. `mendari.eval/2` does not change.
4. **Rotation numbers and the submission budget.** Default: no house default (LP-11 conflict 47); both are set per set at freeze. Suggested:
   - 20 runs and 180 days per set;
   - 3 submissions per organization and lineage per rotation;
   - the confirmation once per version.
5. **The floors, the ceiling, the bands and the effective date.** Default:
   - a recall floor of 0.95 on the simultaneous lower bound;
   - a false-automation ceiling of 0.02 on the 95% upper bound;
   - no critical case automated;
   - recall lower-bound bands of 0.99, 0.98, 0.97, 0.95 and 0.90;
   - false-automation upper-bound bands of 0.005, 0.01, 0.02 and 0.05;
   - accuracy (lower bound) and Brier (upper bound) bands in steps of 0.05;
   - cost bands per 1,000 decisions of 0, 1, 10 and 100 USD;
   - an effective date at least 30 days after the URL is attested.
6. **One-sided or two-sided bounds.** Default: two-sided, as written.
7. **Spend for 17i.** Default: one declared configuration per remote arm, Jev's deployed model and at most two LLM ids, 3 replicates, `test` sets only: 31,500 decisions, run once and re-run only on request.
8. **Benchmark-publication terms and the commercial-relationship disclosure** for Jev, Claude and gpt-oss. Default: no remote entry is published until its type has a statement.
9. **The signing key.** Default:
   - generate it offline, with a copy in `~/Documents/mendari-private`;
   - publish its public key in `scoreboard/keys.json`;
   - never set it on a server or in CI;
   - it does not sign freeze commitments.
10. **Who a submitting developer is, and through which route** (LP-19, LP-20). Default: 17l waits. Method 1.0 grades the reference packs by Mendari's own declaration.
11. **Publishing the docs site, and deploying the corrected `web/index.html`.** Default: after gate G1-2, with the method and the early scoreboard together. The owner's decision (2026-11-07): after gate G1-2's `patent-decision` only, not all of G1-2.

Owner actions that are not decisions. Each gets an owner line in the todo, and each is shown pending where it applies:
- Generate the signing key, publish its public key, and sign the committed results.
- Freeze the hidden sets (gate `hidden-sets-frozen`), and mint a grading key once 17k lands.
- Write the price table, the disclosures and the declarations, then run 17i.
- Attest `grading-method` once the site is published, and set the effective date.

## Risks

- **gitleaks and the public key.** A base64 public key in `scoreboard/keys.json` may trip `generic-api-key`. Scan locally first, and allowlist that one path only (see the memory note on PR secret scans).
- **Vendor names.** Price tables, disclosures and run records must stay outside `src/mendari/`, and so out of the wheel.
- **Method hash churn.** Any PR that edits the method re-pins its hash. Such edits land one at a time, and earlier results go stale.
- **Repository size.** The deterministic store is about 3 MB. After 17i, ledgers grow by roughly 15 to 20 MB (each 600-case ledger is 200 to 520 KB on main).
- **Request time.** Pure-Python stratified bootstraps inside the hidden-set route are slow (conflict 32).
- **Overconfident intervals.** Template duplicates make raw intervals look narrower than they are (ai-guard `test` has 600 cases but 81 distinct inputs). The scope shows distinct n, and the evidence banner decides.
- **The oracle.** Banded answers on the selection part still leak information. The budget and the single confirmation bound it.
- **Flaky latency.** No CI job regenerates run records, so CI only recomputes from them.

Checked against main (06d5332f):
- **What was verified:**
  - No PRs are open. #217, #219, #248, #250 and #251 are merged, though `tasks/todo.md` still lists some as open.
  - The arm table, re-derived from the five manifests.
  - Raw and distinct critical positives on all 15 public scoring sets.
  - The evidence minimums, and 183 labelled cases for the 0.02 ceiling.
  - 31,500 decisions for 17i.
  - The five committed benchmark ledgers are the deterministic default configurations, with zero cost and zero tokens.
  - The required checks.
  - The next free ids: ADR-0013, and threat-model rows S18, T7, R3, I14, D11 and E12.
  - Every existing test this plan names.
- **Fixes to the two drafts:**
  - Plan A's `docs/grading/prices/` README, and Plan B's extra docs pages, would fail `mkdocs --strict` unless they are in the nav.
  - Plan B's `packs/<pack>/evals/scoreboard.json` would ship vendor ids in the wheel and could be forged by a publisher's pack: moved to `scoreboard/`.
  - Plan A's `mendari.ledger/2` is not needed: the dataset and the run record give distinct counts, weights, per-type tokens and latency.
  - Plan A's signing key on the server and its new results table are dropped: signing is offline, and the repository is the one store.
  - Plan A reported declared configurations on only half a hidden set, which halves the evidence.
  - Plan B's display of figures at their own level, with new JavaScript ports, is dropped: Definition of shipped §3 shows 95%.
  - Plan A's bootstrap of band tuples for rank intervals is replaced by Plan B's construction from the Holm decisions, which resamples nothing.
  - Plan B's rewrite of `providers.uses_model` is replaced by a consistency test.
  - Plan A's new `scoreboard` CI job would not be a required check: its steps go in `reference-pack`.
  - Both drafts' dashboard view is dropped (conflict 29).
  - The code map's coverage test name is `tests/test_coverage.py::test_ai_guards_test_set_has_30_distinct_blocks_of_164`.