<!-- Planned on 2026-11-10 against main ca7e016b (LP-19a, 19b and 19h merged; 19c in another
session; LP-20's plan being written in another session: only its note of reserved numbers was
read). One reader went through
LP-22's prompt and Definition of shipped (notes/vision-audit.md), the gates, LP-19's plan, the
share schema (src/mendari/evals/share.py), the registry's report storage and routes
(src/mendari/hub/registry.py), the client and the command line, the statistics module, the Hub's
pack page and the scoreboard page script, read-only. Nothing was built or run. Where the build
prompt, the owner's notes and the code disagree, the conflict and the default each PR follows are
written below. tasks/todo.md tracks progress. -->

# LP-22 build plan: the site evaluation network (A11)

> **Numbers this plan takes** (free at ca7e016b; other lanes are open, so each PR re-checks them
> before it writes anything): **ADR-0017** (ADR-0016 is left for LP-20's plan, which is being
> written at the same time). Threat-model rows **S26, S27, T14, T15, R9, I24, I25, I26, D18,
> E20**. The last rows on main are S22, T10, R6, I18, D14, E16; `notes/lp19-plan.md` reserves
> S22, S23, T11, R6, I19, I20, D15, E17. LP-20's plan session reserved **S24, S25, T12, T13, R7,
> I21, I22, I23, D16, D17, E18, E19** (its note in the sessions' shared folder, 2026-11-10); this
> plan starts after them and after the next two in each letter, so R8 stays unused. The order
> test allows gaps (`tests/test_threat_model.py:42-50` checks that ids are unique and sorted,
> not consecutive).

## Summary

LP-22 lets a health system evaluate a pack on its own chart-reviewed data, inside its own
environment, and share signed counts with the registry, in a way that resists forged results
and results picked after the fact. Today a site is a name typed on the command line, nothing
is signed, and the registry serves every report marked `verified: false`
(`src/mendari/hub/registry.py:834-837`).

LP-22 delivers:
- **Sites and site keys.** A site is a record in the registry, owned by an organization and
  verified by a registry reviewer, with Ed25519 public keys that can be added and revoked.
- **Counts in, rates out.** A new payload, `mendari.site-report/2`, holds whole counts only.
  The registry computes every rate and interval. One suppression function, with a written rule
  per field, runs on both sides.
- **Tickets.** Before a run the site asks for a ticket bound to the site, the pack digest, the
  configuration, the size of the dataset and a keyed commitment to the dataset (an HMAC under a
  secret that never leaves the site). A ticket with no result is shown as unreported. There is
  one live result per site, digest, configuration and commitment.
- **Signed submissions.** The payload is signed over canonical JSON with a key id and a nonce,
  and checked on receipt. A forged signature, an unregistered or revoked key, a signature over
  another body, a replay, and a digest that is not a published version's are each refused.
- **Deterministic by default.** A site evaluation uses the deterministic arm unless the site
  records its consent to a remote arm, in the ticket and in the payload.
- **Aggregation.** Per pack digest and configuration: each site's k of n with its exact
  interval; a pooled fixed-effect figure from summed counts, labelled as such; a random-effects
  estimate beside it; the range; the number of sites and total cases; and a homogeneity warning
  (an exact or seeded permutation test when any expected count is under 5, chi-square otherwise,
  alpha 0.05).
- **Display.** The Hub shows site evidence in its own section, as a forest plot with a table,
  with the site count and total cases. It never enters the hidden-set rank.
- **Review before sending, and an offline path.** `mendari eval --share-out` writes the signed
  submission to read first; a submission and a ticket request can each be carried as a file.

It is built in **10 PRs, 22a to 22j**.

What each step needs merged first:

| Step | Needs from LP-22 | Needs from LP-19 | Needs from LP-20 |
|---|---|---|---|
| 22a plan | nothing | nothing | nothing |
| 22b statistics | 22a | nothing | nothing |
| 22c counts-only payload | 22a | nothing (19b is merged) | nothing |
| 22d sites and keys (store) | 22a | 19c (claims: which organization holds a namespace, and which namespaces are listed) | nothing |
| 22e tickets | 22d | 19c | nothing |
| 22f signed submissions | 22c, 22e | 19g (the admin token's branch of the report route is gone) | nothing |
| 22g aggregation | 22b, 22f | nothing more | nothing |
| 22h the Hub | 22g | 19i (it edits the same Hub page) | nothing |
| 22i people's routes and the dashboard | 22d | 19d, 19e, 19f, and 19g | nothing known (see below) |
| 22j close-out | all of the above | 19k (it rewords the same gate line) | nothing |

No LP-22 step needs an LP-20 step, as LP-20 is described today (`notes/vision-audit.md:2452`;
`notes/lp19-plan.md:677-678`: who a submitting developer is, what `pack_author` grants, large
archives, object storage). LP-20's plan was not read. If it moves the people's routes under
`/api/registry/` or changes who administers a namespace, 22i follows it.

Order of work:
- 22a first.
- **In parallel once 22a is merged, and before LP-19 is finished:** 22b (pure statistics, no
  registry code) and 22c (the payload and its suppression).
- 22d follows 19c. 22e follows 22d. 22f follows 22c, 22e and 19g.
- **In parallel once 22d is merged:** 22e then 22f on one side, 22i on the other (22i also
  waits for 19d to 19g).
- 22g follows 22b and 22f. 22h follows 22g and 19i.
- 22j closes LP-22.

Where PRs collide, merge in this order:

| File | Merge order |
|---|---|
| `src/mendari/hub/registry.py` | 19c, 19i, 19d, 19e, 19g (LP-19's order), with 22c anywhere among them; then 22d, 22e, 22f, 22g |
| `src/mendari/evals/share.py` | 22c, then 22f |
| `src/mendari/hub/client.py` | 19i, then 22e, 22f |
| `src/mendari/cli.py` | 22d, 22e, 22f |
| `hub/app/packs/[namespace]/[name]/page.tsx` | 19i, then 22h |
| `src/mendari/server/registry_routes.py`, `src/mendari/dashboard/registry.js` | 19d, 19e, 19f, then 22i |
| `tests/test_shipped.py` (`CHAINED`) | 19d, 19e, 19g, then 22d, 22e, 22f, 22i |

To keep the collisions small, LP-22's registry code goes in new modules
(`src/mendari/hub/sites.py`, `site_signing.py`, `site_evidence.py`); `registry.py` gains only the
schema text, the `ROWS` entries and the routes.

These stay off until the owner acts:
- A registry with no verified site issues no ticket and accepts no report (fail closed). No
  build PR creates or verifies a site anywhere but in a test.
- A site is verified only by a registry reviewer (LP-19e) or by the operator's command with a
  reason.
- No new setting is needed. The network exists wherever the registry does
  (`MENDARI_REGISTRY`, LP-19d); production gets nothing from a build PR.

What LP-22 does not change:
- What a valid pack decides, any pack's digest, any policy. No reference pack is rebuilt.
- The hidden-set results, the grading method and the rank (`src/mendari/evals/ranking.py:90`).
  Site evidence is read by none of them, and a test pins that.
- The registry's unkeyed chain and `mendari registry verify`: every new write is chained in
  `hub_events` with its row's hash, as the writes today are (`registry.py:276-294`).
- Whether a deployment may automate. LP-22 gives a signed report to cite; it does not make the
  engine ask for one (conflict 14).

Figures: every proportion LP-22 shows is k of n with its exact Clopper-Pearson interval
(`stats.clopper_pearson`, `src/mendari/evals/stats.py:232-247`, through `figures.of`,
`src/mendari/evals/figures.py:41-48`). The registry computes each from counts it verified; none
is typed in or taken from a site. A figure on too few cases or too few sites reads
"insufficient evidence". Each new statistical function has a fixture test against scipy or
statsmodels, which are development dependencies only (`pyproject.toml:49-50`).

Every PR:
- passes the Definition of shipped (`notes/vision-audit.md:1310`);
- runs its tests on SQLite and on Postgres, with warnings as errors;
- uses synthetic site data only (`mendari.sandbox.synthetic_*`), and keys, secrets and tokens
  made at runtime, none committed;
- updates `README.md`, `CHANGELOG.md`, the LP-22 entry in `tasks/todo.md` and the docs it names;
- puts the SPDX line on new source files and signs off every commit (`git commit -s`);
- writes no production hostname, site name or agreement in the tree.

## What the code is today (the facts the plan rests on)

1. **A site is free text.** `mendari eval --share SITE` takes any name
   (`src/mendari/cli.py:2763-2765`); the registry accepts 1 to 80 characters of letters, digits,
   spaces, dots, dashes and underscores (`ReportRequest`, `registry.py:1421-1423`) and stores it
   in `hub_reports.site` (`:143-152`). No table holds a site, and no key belongs to one.
2. **Nothing is signed.** The client posts `{"site": …, "report": …}` with a bearer token
   (`src/mendari/hub/client.py:226-234`). The registry checks the token only: the admin token
   or any publisher token of any namespace (`registry.py:1893-1897`). It reads at most 2 MB
   after that check (`MAX_REPORT_BYTES`, `:1418`, `:1898-1902`).
3. **An identical body can be sent again.** Each accepted report gets a new random id and a
   new row (`registry.py:786-801`); nothing compares it with an earlier one.
4. **Every report is served unverified, and the Hub shows none.** `reports()` marks each row
   `verified: False` (`registry.py:818-838`); the Hub's pack page explains why it shows nothing
   (`hub/app/packs/[namespace]/[name]/page.tsx:107-113`). The threat model and the registry's
   operations page say the same (`docs/security/threat-model.md:400`,
   `docs/operations/registry.md:187`).
5. **`mendari.site-report/1` carries rates as well as counts.** Accuracy, macro-F1, precision,
   recall, F1, ECE, Brier, outcome rates, the sweep's coverage and accuracy, latency and dollar
   cost are floats the site computed, rounded to 4 or 3 places (`share.py:59-64`, `:137-141`,
   `:144-166`, `:193-221`). The registry's summary of a report copies the site's accuracy,
   macro-F1, automation rate, false-automation rate and ECE (`summary`, `share.py:362-375`;
   used at `registry.py:816`, `:837`).
6. **Suppression covers calibration bins only.** A bin under `MIN_GROUP = 10` is dropped by
   the sender (`share.py:41`, `:276-280`) and refused by the receiver (`:147`). Subgroups are
   left out whole, because nothing can tell a category from a name until a pack declares each
   subgroup's values (`share.py:9-11`). Confusion-matrix cells, outcome counts and the critical
   counts go as they are. There is no minimum dataset size.
7. **The registry already checks a report against its pack.** The report's pack id and version
   must be the route's (`registry.py:763-765`), and every question, label, slot, variant and
   composite member it names must be one the published version declared (`_unknown_names`,
   `:1310-1345`; recorded at publish by `describe`, `:1372-1392`). It does not check that the
   report's digest is the published version's digest.
8. **The counts a counts-only payload needs are in the run.** A run keeps a per-case ledger
   (`mendari.ledger/1`, `src/mendari/evals/ledger.py:1-25`), and the command line has it in
   hand when it shares (`cli.py:226-244`). The report holds the outcome counts, each critical
   label's `n`, `detected` and `auto_missed`, and the false-automation proportion as counts
   (`src/mendari/evals/result.py:121-125`, `:161`, `:168`).
9. **The dataset's plain hash exists and stays home.** `Dataset.sha256` is the SHA-256 of the
   file (`src/mendari/evals/harness.py:98`); `site_report` sends only `dataset.n`
   (`share.py:299`). The ledger's case key is an HMAC under a fixed, public constant
   (`ledger.py:42`, `:54-56`), so it is no secret and cannot serve as a commitment.
10. **Key and signing helpers exist.** `hub/archive.py` makes and loads Ed25519 keys and names
    one by `ed25519:` plus 16 hex of its SHA-256 (`:74-111`). Its own sign and verify are for
    archives (`build`, `:133`; `verify`, `:174`). A record signed over canonical JSON, with a
    format of its own, is how a recall is signed (`hub/yank.py:39`, `:96-130`), and the
    scoreboard's signature has a domain prefix (`src/mendari/evals/signing.py:56`).
11. **The chain covers rows by hashing every column** (`_row_sha`, `registry.py:271-273`;
    `ROWS`, `:249-268`), so a row that changes without an event fails `verify`
    (`notes/lp19-plan.md:114-117`). A revocation is an update with its own event
    (`revoke_token`, `registry.py:541-558`).
12. **The registry knows an organization only as a label.** An owned publisher token records
    `org` (`registry.py:195-206`, `:562-575`); there is no organizations table in the registry.
    LP-19c adds `hub_claims`, which links one organization to a namespace
    (`notes/lp19-plan.md:216-218`).
13. **The statistics module has no pooling and no homogeneity test.** It has the exact
    binomial interval, the incomplete beta function, exact McNemar, seeded resampling and Holm
    (`stats.py:144`, `:232`, `:255`, `:310`). A search of `src`, the Hub and `scripts` finds no
    chi-square, permutation test of homogeneity, random-effects estimate or forest plot.
14. **Which provider types are local is a fixed set** (`LOCAL_PROVIDER_TYPES`,
    `src/mendari/core/origin.py:82`), and a variant counts as remote unless every type it uses
    is local; a composite whose members cannot be read counts as remote
    (`src/mendari/evals/arms.py:83-90`). The deterministic arm has a name (`arms.py:40`).
    Today `--share` shares whatever configuration ran, with no question of consent
    (`cli.py:241-244`).
15. **The scoreboard page is generated from the repository's results store** and reads no
    registry (`scripts/scoreboard_page.py:69-71`). Site reports live in a registry's database.
16. **Limits.** Site reports have a rate of their own, 60 an hour per token
    (`registry_report`, `registry.py:1503`, `:1549`). Under LP-19's mount the engine caps a
    body at 4 MiB (`src/mendari/settings.py:17`), above the 2 MB a report may be.
17. **The route inventory names the report route** and its event
    (`tests/test_shipped.py:182`, `report.shared`).

## Pull requests, in order

- [ ] **22a** The plan, ADR-0017 (Proposed), and the conflicts written to `tasks/todo.md`.
  - **What it builds:** `notes/lp22-plan.md` (this plan);
    `docs/decisions/ADR-0017-a-site-network-of-signed-counts.md` (Proposed); the LP-22 entry in
    `tasks/todo.md` with the steps, the conflicts and the open decisions, written before any
    build, as the prompt requires.
  - **Files:** new: the plan, the ADR. Changed: `mkdocs.yml` (the ADR's nav entry),
    `notes/README.md`, `tasks/todo.md`.
  - **Tests:** none new; `tests/test_threat_model.py`, the gates check and the docs build stay
    green.
  - **Safe alone because:** documents only.

- [ ] **22b** The statistics of pooling (requirement 6, the functions only). Parallel with
  everything else.
  - **What it builds**, in a new module `src/mendari/evals/pooling.py`, pure Python, with what
    it needs added to `stats.py`:
    - `pooled_fixed(sites)`: summed k of summed n with its exact interval, named
      `pooled (fixed effect, summed counts)`.
    - `random_effects(sites)`: DerSimonian-Laird on the logit scale, 0.5 added to both cells
      of a site whose k is 0 or n, with a 95% interval; `insufficient evidence` under 3 sites
      (decision 4).
    - `homogeneity(sites)`: the sites by (hit, miss) table. When every expected count is 5 or
      more: Pearson's chi-square, with its p-value from a regularized incomplete gamma function
      added to `stats.py`. Otherwise: the exact test by enumerating every table with the same
      margins when there are at most 1,000,000, else a permutation test of 10,000 resamples
      whose seed is derived from the inputs and written in the output. Alpha 0.05. Never
      chi-square on small expected counts.
    - `range_of(sites)`: the lowest and highest site rate, each with its k of n.
    - Each takes whole counts and refuses anything else, as `figures.of` does.
  - **Files:** `src/mendari/evals/pooling.py` (new), `src/mendari/evals/stats.py`,
    `docs/spec/evaluation-reports.md` (a "Pooled site evidence" part: each formula and what it
    is called). New: `tests/test_pooling.py`.
  - **Tests** (`tests/test_pooling.py`):
    - `::test_the_pooled_interval_matches_a_hand_computed_reference`;
    - `::test_the_random_effects_estimate_matches_a_hand_computed_reference_and_statsmodels`;
    - `::test_chi_square_matches_scipy` (statistic and p-value, over a grid of tables);
    - `::test_the_incomplete_gamma_matches_scipy`;
    - `::test_small_expected_counts_never_use_chi_square`;
    - `::test_the_exact_test_matches_a_scipy_reference` (the reference sums scipy's
      multivariate hypergeometric probabilities in the test);
    - `::test_the_permutation_test_is_seeded_and_writes_its_seed` (two runs, the same bytes);
    - `::test_one_site_or_two_reads_insufficient_evidence_where_the_method_says`;
    - `::test_rates_and_fractions_are_refused`.
  - **Safe alone because:** pure functions that nothing calls yet.

- [ ] **22c** Counts in, rates out: `mendari.site-report/2` and one suppression function
  (requirement 4; the "left for LP-22" of `tasks/todo.md:73-74`). Parallel with 22b.
  - **What it builds:**
    - `SiteReportV2` in `src/mendari/evals/share.py`: closed, strict, and every number a whole
      count (or null where suppressed). The pack, providers and policy as in `/1`. Per
      question: `n`, the confusion matrix. Calibration: per bin, `n` and how many were right.
      Outcome counts. Automation: `n` and how many were wrong. Review: `n` and how many would
      be overridden. Each critical label: `n`, `detected`, `auto_missed`. The sweep: per
      threshold, how many were covered and how many of those were right. Error counts, token
      counts. **Not in it** (not counts): accuracy, precision, recall, F1, macro-F1, ECE,
      Brier, any rate, latency and dollar cost (conflict 2).
    - It is built from the run's ledger (fact 8), so each count is exact, never a rate times n.
    - `suppress(report) -> SiteReportV2`, one function, with the rule for each field written in
      a table in the spec and in the function's docstring:
      - **A minimum dataset size:** a run on fewer than 100 cases is not shared (decision 3).
      - **Cells that describe people** (confusion cells, calibration bins, outcome counts,
        sweep rows): a count of 1 to 9 is suppressed (null). Then complementary suppression:
        while a row or column whose total is shared has exactly one suppressed cell, its next
        smallest cell is suppressed too, so no suppressed cell can be worked out by
        subtraction.
      - **Safety counts** (each critical label's `n`, `detected` and `auto_missed`): shared
        exactly, never suppressed, whenever the dataset passes the minimum size. A missed
        critical case must not disappear under a privacy rule; the rule is stated beside them.
      - **Totals** (`dataset.n`, a question's `n`): shared exactly.
    - Both sides run it: the sender before writing or sending; the registry refuses a payload
      that `suppress` would change (as both sides check small bins today, fact 6).
    - `evals/figures.py` gains `site(report)`: every figure the registry shows of one report,
      computed from its counts.
    - The registry accepts `/2` and answers 422 to a new `/1` (no fallback for old shapes, as
      LP-07 decided). Rows stored as `/1` stay stored and served as today until 22f. A `/2`
      report is still `verified: false` in this PR.
    - `mendari eval --share` and `--share-out` send and write `/2`.
  - **Files:** `src/mendari/evals/share.py`, `src/mendari/evals/figures.py`,
    `src/mendari/hub/registry.py` (the request model and `reports()`), `src/mendari/cli.py`,
    `docs/spec/decision-pack-0.2.md` (the shared-report paragraph at `:532`),
    `docs/deploying-with-phi.md` (the row at `:828`). Changed tests: `tests/test_share.py`,
    `tests/test_registry.py` (`:140-240`), `tests/test_hub_client.py` (`:494`).
  - **Tests** (`tests/test_share.py` unless named):
    - `::test_a_shared_report_holds_whole_counts_and_no_rate` (every leaf is an int, a name, a
      boolean or null);
    - `::test_a_rate_or_a_fraction_anywhere_is_refused_on_both_sides` (parametrized over every
      numeric position);
    - `::test_a_suppressed_cell_cannot_be_recovered_from_the_totals` (a fixture; the test
      solves for each suppressed cell's possible values and finds more than one);
    - `::test_safety_counts_are_shared_exactly_and_small_runs_are_not_shared`;
    - `::test_the_receiver_refuses_what_suppression_would_change`;
    - `::test_every_field_has_a_written_suppression_rule` (the model's fields against the
      rule table);
    - `::test_the_counts_are_the_ledgers` (each count recomputed from the ledger);
    - `::test_every_shipped_benchmark_can_be_shared` (kept, on `/2`);
    - `::test_an_unknown_key_anywhere_is_refused` and
      `::test_free_text_in_any_string_is_refused` (kept, on `/2`);
    - `tests/test_registry.py::test_the_registry_computes_every_rate_it_shows`;
    - `tests/test_registry.py::test_a_site_report_of_the_old_format_is_refused_and_names_the_new_one`.
  - **Safe alone because:** it only narrows what leaves a site and what the registry takes.
    No public registry exists (`docs/operations/registry.md:10`), so no deployed client loses
    a path; an older client is told which format to send.
  - **Breaking:** for the command line (the payload's format). `CHANGELOG.md` says so.
  - **Threat model:** I24.

- [ ] **22d** Sites and site keys in the registry's store, and the operator's commands
  (requirement 1). Needs 19c.
  - **What it builds**, in `src/mendari/hub/sites.py`:
    - `hub_sites (id, org, name, status, created_at, created_by, decided_at, decided_by)`,
      status `pending`, `verified` or `closed`. `id` is random. `org` is the organization as
      the registry names it (fact 12).
    - `hub_site_keys (key_id PRIMARY KEY, site, public_pem, created_at, added_by, revoked_at,
      revoked_by, compromised)`. A key is loaded and named by `archive.load_public_key` and
      `archive.key_id` (fact 10). A key that is a namespace's publisher key, or another site's,
      is refused (409), so one key never signs in two roles. At most 5 live keys a site.
    - `register_site(org, name, by)`, `decide_site(id, verify, by, note)`,
      `add_site_key(site, public_pem, org, by)`, `revoke_site_key(key_id, org, by, reason,
      compromised)`, `close_site(id, org, by, reason)`, `sites(org=None)`.
    - Rotation is adding a key and revoking the old one. A revocation says whether the key is
      thought compromised; if so, every report signed with it leaves the pool and is listed as
      such (decision 9). Otherwise reports received before the revocation stand.
    - Chained events `site.registered`, `site.verified`, `site.refused`, `site.closed`,
      `site.key_added`, `site.key_revoked`, each with `org` and the row's hash; `ROWS` gains
      `sites` and `site_keys`.
    - One rule for who a site is to a pack: a site whose organization holds the pack's
      namespace (its claim, LP-19c) is **the publisher's own site**.
    - Operator commands on the registry's database, for a registry with no sign-in:
      `mendari registry site --db … --org … --name … --reason …`, `site-verify`,
      `site-key --site … --key FILE`, `site-revoke-key`, `sites`. They record `operator` and
      the reason, as `mendari registry token` does.
    - Public read: `GET /v1/sites/{id}`: the verified site's name, status and key ids with
      their dates. A pending or closed site answers as one that does not exist.
  - **Files:** `src/mendari/hub/sites.py` (new), `src/mendari/hub/registry.py` (schema, `ROWS`,
    one route), `src/mendari/cli.py`, `tests/test_shipped.py`, `docs/operations/registry.md`.
    New: `tests/test_registry_sites.py`.
  - **Tests** (`tests/test_registry_sites.py`):
    - `::test_a_site_belongs_to_an_organization_and_is_pending_until_verified`;
    - `::test_another_organization_neither_sees_nor_changes_a_site_or_its_keys`;
    - `::test_a_site_key_is_ed25519_and_never_a_publishers_or_another_sites`;
    - `::test_a_key_is_rotated_and_the_old_one_revoked_with_a_reason`;
    - `::test_a_site_holds_five_live_keys_at_most`;
    - `::test_a_pending_or_closed_site_answers_as_one_that_does_not_exist`;
    - `::test_registration_verification_keys_and_revocation_are_chained_with_the_organization`;
    - `::test_the_operators_site_records_operator_and_why`;
    - `::test_a_database_from_before_sites_still_verifies`;
    - `tests/test_registry_chain.py::test_a_changed_chain_or_a_row_written_past_the_api_does_not_verify`
      gains a planted site key and a site flipped to `verified`.
  - **Safe alone because:** additive. No report is accepted or shown differently; a database
    from before opens and verifies.
  - **Threat model:** R9, E20 (first half), T15 (rows written past the API).

- [ ] **22e** Evaluation tickets and the site's side of the command line (requirement 3, and
  the default arm of requirement 5).
  - **What it builds:**
    - `src/mendari/hub/site_signing.py`: a record signed with a site key, Ed25519 over a
      domain prefix and the canonical JSON of every other field, with `format`, `site`,
      `key_id`, `nonce` (16 random bytes) and `at`. Its formats and prefix are its own, so a
      publisher's, the registry's or the grading key's signature is never a site's, and a
      ticket request is never a submission (the rule of S19 and S20).
    - The site's own files, in the client's configuration directory, written private as
      credentials are (`client.py:57`, `:77-82`): the site's id, its private key, and its
      **commitment secret** (32 random bytes). `mendari site init` makes them and prints the
      public key to register; `mendari site key --new` rotates.
    - **The commitment:** HMAC-SHA256 of the dataset file's bytes under the commitment secret.
      Only the site can make it; the same file gives the same value; nobody else can test a
      guessed dataset against it. The plain hash is never sent (fact 9).
    - `POST /v1/tickets`: a signed request `mendari.site-ticket-request/1` with the pack (id,
      version, digest), the configuration (each slot's variant), `remote_consent`, the
      commitment and the dataset's size `n`. The registry reads at most 8 KB, then checks, in
      this order: the key is a live key of a verified site; the signature; the clock skew (as
      a recall's, `yank.py:60`); the nonce is new for that site; the version is published,
      listed, not recalled or deleted, and **its digest is the one sent**; the configuration
      is one the version declares (`names.slots`, fact 7); a configuration that is not wholly
      local (fact 14) has `remote_consent: true`; `n` passes the minimum size; and no live
      ticket or report exists for the same site, digest, configuration and commitment.
    - `hub_tickets (id, site, key_id, pack, version, digest, configuration, remote_consent,
      commitment, n, nonce, created_at, expires_at, status, submit_sha256)`, status `open`,
      `reported` or `withdrawn`. The answer holds the ticket and a one-time submission secret
      (stored as its SHA-256), which lets the registry refuse a submission before reading its
      body. A ticket lasts 30 days (decision 5); "expired" is worked out on read, never
      written, since the chain refuses a row that changes without an event (fact 11).
    - `POST /v1/tickets/{id}/withdraw` (signed): the site gives up a ticket. It stays visible.
    - `GET /v1/packs/{namespace}/{name}/tickets`: per verified site, digest and configuration,
      how many tickets are reported, unreported (open or expired) and withdrawn, with dates.
      No commitment and no secret is served.
    - `describe` records each composite member's provider type at publish
      (`names.member_types`), so the registry can tell a local composite from a remote one; a
      version published before counts a composite as remote, as `arms.remote_types` does.
    - `mendari eval --share` asks for the ticket **before** the run, and refuses a
      configuration with a remote provider unless `--consent-remote` is given. With no
      configuration named, it shares the deterministic arm.
    - Chained `site.ticket_issued` and `site.ticket_withdrawn`; `ROWS` gains `tickets`.
    - Rates: ticket requests per address before the signature is checked, and per site key
      after (`REGISTRY_RATES`, `registry.py:1500-1504`).
  - **Files:** `src/mendari/hub/site_signing.py` (new), `src/mendari/hub/sites.py`,
    `src/mendari/hub/registry.py`, `src/mendari/hub/client.py`, `src/mendari/cli.py`,
    `tests/test_shipped.py`, `docs/operations/registry.md`. New:
    `tests/test_site_tickets.py`.
  - **Tests** (`tests/test_site_tickets.py`):
    - `::test_a_ticket_binds_site_digest_configuration_size_and_commitment`;
    - `::test_a_ticket_request_with_a_forged_signature_or_an_unregistered_key_is_refused`;
    - `::test_a_revoked_key_and_a_pending_site_get_no_ticket`;
    - `::test_a_replayed_ticket_request_is_refused` (the same nonce);
    - `::test_a_digest_that_is_not_the_published_versions_is_refused`;
    - `::test_a_recalled_deleted_or_unlisted_version_gets_no_ticket`;
    - `::test_a_remote_configuration_needs_recorded_consent_and_the_default_is_deterministic`;
    - `::test_one_live_ticket_per_site_digest_configuration_and_commitment`;
    - `::test_a_ticket_with_no_result_is_listed_as_unreported_and_an_expired_one_stays_listed`;
    - `::test_the_commitment_is_keyed_and_the_plain_hash_is_never_sent` (the request's bytes
      hold neither the file's SHA-256 nor the secret; another secret gives another value);
    - `::test_the_sites_key_and_secret_are_written_private`;
    - `::test_a_recall_or_a_scoreboard_signature_is_not_a_sites`;
    - `::test_a_ticket_request_is_read_to_8_kb_and_limited_per_address_and_key`.
  - **Safe alone because:** a ticket changes nothing a reader sees but the new tickets list;
    the report route is untouched until 22f.
  - **Breaking:** `--share` takes the site from `mendari site init`, no longer a free name
    (conflict 4). `CHANGELOG.md` says so.
  - **Threat model:** S27 (ticket half), T14 (first half), I25, I26, D18 (first half).

- [ ] **22f** Signed submissions, verified on receipt (requirement 2, and requirement 8).
  **This PR gets one adversarial security review.** Needs 19g.
  - **What it builds:**
    - `mendari.site-submission/1`: `{format, site, key_id, ticket, nonce, at, report,
      signature}`, `report` the `/2` payload of 22c with `remote_consent`, signed as 22e signs.
    - `POST /v1/packs/{namespace}/{name}/{version}/reports` takes nothing else. In order: the
      ticket's one-time secret as the bearer credential, before any body is read; at most
      2 MB read (fact 2); the body parsed, then validated (as today, `registry.py:1903-1913`);
      the key is live and is a key of the ticket's site; the signature over the exact fields;
      the nonce is new; the report's pack, version, **digest**, configuration, `n` and
      `remote_consent` are the ticket's; the names are the pack's (fact 7); `suppress` changes
      nothing. Then the row and the ticket's `reported` status are written in one transaction.
      Nothing it refuses is echoed.
    - `hub_reports` is not altered (fact 11). A new table
      `hub_site_reports (id, site, key_id, ticket, namespace, name, version, digest,
      configuration, body_sha256, body, signature, created_at, supersedes)` holds signed
      reports; `ROWS` gains `site_reports`. `hub_reports` rows (unsigned, `/1` or `/2`) stay
      stored and are no longer served: `reports()` lists signed reports only, each
      `verified: true` with its site, key id and ticket.
    - **One live result.** A ticket takes one report, once. A correction needs a new ticket
      that names the report it supersedes; the old report stays stored and listed as
      superseded, and leaves the pool. The count of superseded reports is shown.
    - `report.shared` (chained) gains the site's id, the key id, the ticket and the body's
      SHA-256; it never held the body. The stored signature lets anyone check a served report
      against the site's public key (`GET …/reports/{id}` serves the signed record).
    - **Review before sending:** `mendari eval --share-out FILE` writes the signed submission
      and a readable summary, and sends nothing. `mendari site submit FILE` sends it later,
      from any machine.
    - **Offline:** `mendari site ticket-request --out FILE` and
      `mendari site ticket --request FILE --out FILE` let the ticket be fetched from another
      machine; `mendari eval --ticket FILE --share-out FILE` runs without a network; the
      operator's `mendari registry accept-report --db … FILE` takes a submission carried by
      hand, through the same checks.
    - The publisher-token path for reports is removed; a token there is a 401 that names
      `mendari site init`.
  - **Files:** `src/mendari/hub/sites.py`, `src/mendari/hub/site_signing.py`,
    `src/mendari/hub/registry.py`, `src/mendari/evals/share.py`, `src/mendari/hub/client.py`,
    `src/mendari/cli.py`, `tests/test_shipped.py`, `tests/conftest.py` (a `site` fixture),
    `docs/operations/registry.md`, `docs/guides/` (a new "Evaluate at your site" guide and its
    nav entry). New: `tests/test_site_reports.py`.
  - **Tests** (`tests/test_site_reports.py`):
    - `::test_a_forged_signature_is_rejected`;
    - `::test_an_unregistered_or_revoked_key_is_rejected`;
    - `::test_a_valid_signature_over_a_different_body_is_rejected`;
    - `::test_a_replay_is_rejected` (the same bytes twice; and a new nonce on a used ticket);
    - `::test_a_report_for_another_digest_configuration_size_or_site_than_its_ticket_is_rejected`
      (parametrized);
    - `::test_a_payload_with_any_case_text_fails_on_both_sides`;
    - `::test_a_publisher_token_no_longer_shares_a_report`;
    - `::test_nothing_is_read_past_the_limit_and_nothing_before_the_tickets_secret`;
    - `::test_a_verified_report_names_its_site_key_and_ticket_and_its_signature_checks`;
    - `::test_a_correction_supersedes_and_both_stay_listed`;
    - `::test_reports_under_a_compromised_key_leave_the_pool`;
    - `::test_share_out_writes_exactly_what_is_sent_and_sends_nothing`;
    - `::test_an_offline_ticket_and_submission_are_accepted_through_the_same_checks`;
    - `::test_unsigned_reports_from_before_are_kept_and_not_served`;
    - `::test_the_registry_chain_verifies_after_a_ticket_a_report_and_a_supersession`.
  - **Safe alone because:** the Hub shows no site report before or after this PR (22h does);
    what changes is that the reports route lists only what was signed and checked.
  - **Needs the owner's yes:** it removes the unsigned path, a breaking change to a published
    route.
  - **Threat model:** S26, S27 (submission half), T14 (second half), D18 (second half).

- [ ] **22g** Aggregation, served by the registry (requirement 6).
  - **What it builds:**
    - `src/mendari/hub/site_evidence.py`: `mendari.site-evidence/1`, worked out on read from
      the live signed reports of one pack, **per digest and configuration**, never across
      them. For each pooled figure (decision 8: the primary question's accuracy, each critical
      label's detection, critical cases automated, false automation): each site's k of n with
      its exact interval first; then `pooled_fixed`, `random_effects`, `range_of` and
      `homogeneity` from 22b; the number of sites; the total cases; the seed and the test
      used; and the counts of unreported tickets and superseded reports from 22e and 22f.
    - **Out of the pool, and listed with the reason:** the publisher's own site (22d), a
      report under a compromised key, a superseded report, a remote-arm report when the group
      is a local configuration (they are different configurations anyway).
    - A site figure on fewer cases than the method's minimum, a pool of one site, and a
      random-effects estimate under 3 sites read `insufficient evidence`.
    - The homogeneity warning is a sentence in the answer when p is under 0.05: the sites
      disagree by more than chance, so the pooled figure is not a summary of them.
    - `GET /v1/packs/{namespace}/{name}/site-evidence`: public. The answer is cached per set
      of report ids, so a read does no resampling twice.
  - **Files:** `src/mendari/hub/site_evidence.py` (new), `src/mendari/hub/registry.py` (one
    route), `src/mendari/evals/figures.py`, `docs/spec/evaluation-reports.md`. New:
    `tests/test_site_evidence.py`.
  - **Tests** (`tests/test_site_evidence.py`, on synthetic sites made by signing real
    evaluations of a reference pack on `mendari.sandbox.synthetic_*` data):
    - `::test_each_site_is_k_of_n_with_its_interval_before_any_pooled_figure`;
    - `::test_the_pooled_figure_is_the_summed_counts_and_says_so`;
    - `::test_two_digests_or_two_configurations_are_never_pooled`;
    - `::test_sites_that_disagree_raise_the_homogeneity_warning_and_alike_sites_do_not`;
    - `::test_the_publishers_own_site_is_listed_and_left_out_of_the_pool`;
    - `::test_unreported_tickets_and_superseded_reports_are_counted_beside_the_figures`;
    - `::test_too_few_sites_or_cases_read_insufficient_evidence`;
    - `::test_two_reads_give_the_same_bytes_and_the_seed_is_in_the_answer`;
    - `::test_no_rate_in_the_answer_came_from_a_site`.
  - **Safe alone because:** a new read, computed from rows 22f verified.
  - **Threat model:** T15 (pooling across versions), D18 (the cost of a public read).

- [ ] **22h** Site evidence on the Hub, apart from everything else (requirement 7).
  - **What it builds:**
    - The pack page's "Site reports" paragraph (`page.tsx:107-113`) becomes a **Site evidence**
      section: for the version shown, per configuration, a forest plot (inline SVG drawn on the
      server, each site's interval and the pooled figures), the same figures as a table for
      screen readers, the site count and total cases in the heading, the homogeneity warning
      in words, the counts of unreported and superseded, and one sentence: these are sites'
      own data, shared as signed counts; they are not part of any hidden-set result or rank.
    - Every share is k of n through `hub/lib/figures.ts`, as the page's other figures are.
    - The versioned notice from the single source, as every page carries it.
    - The scoreboard page gets one sentence and a link per pack to the Hub's section, when a
      registry is configured for the docs build, and nothing otherwise (conflict 6).
    - A test pins the separation: `evals/ranking.py`, `evals/scoreboard.py` and
      `evals/hidden.py` import nothing of `hub/site_evidence.py` or `evals/pooling.py`.
  - **Files:** `hub/app/packs/[namespace]/[name]/page.tsx`, `hub/lib/registry.ts`, a new
    `hub/app/packs/[namespace]/[name]/forest.tsx`, `hub/tests/smoke.spec.ts`,
    `scripts/hub_seed.py` (two synthetic sites), `scripts/scoreboard_page.py`. New test file:
    `tests/test_site_separation.py`.
  - **Tests:**
    - `hub/tests/smoke.spec.ts`: the section shows two sites, the site count, total cases and
      a plot with a table; a pack with no site evidence says so in words;
    - `hub/tests/figures.spec.ts`: no bare percentage in the section;
    - `tests/test_site_separation.py::test_site_evidence_never_enters_the_hidden_set_rank`;
    - `tests/test_site_separation.py::test_the_ranking_modules_import_no_site_code`;
    - `tests/test_scoreboard_page.py::test_the_page_points_to_site_evidence_and_ranks_none_of_it`.
  - **Safe alone because:** a page section that reads a public route.

- [ ] **22i** People register sites and keys in the dashboard, and reviewers verify them.
  Needs 19d, 19e, 19f and 19g. Parallel with 22e to 22h.
  - **What it builds:**
    - In `src/mendari/server/registry_routes.py` (LP-19d), people only, for the organization
      the guard let through: `GET` and `POST /api/registry/sites`,
      `POST /api/registry/sites/{id}/keys`, `DELETE /api/registry/sites/{id}/keys/{key_id}`
      `{reason, compromised}`, under the `keys` permission, as LP-19d's token routes are.
    - Under LP-19e's reviewer permission: `GET /api/registry/review/sites` (the queue) and
      `POST /api/registry/review/sites/{id}` `{decision, note}`. A reviewer never decides a
      site their own organization registered.
    - `src/mendari/dashboard/registry.js` (LP-19f) gains a **Sites** tab (the site, its status
      in words, its keys with dates, add and revoke with a confirmation) and the sites queue
      in the Registry review view. Tokens only for styles; keyboard and screen-reader names.
    - A site's private key is never typed into or made by the dashboard: the page asks for
      the public key `mendari site init` printed.
  - **Files:** `src/mendari/server/registry_routes.py`, `src/mendari/server/auth.py` (`_RULES`
    only, if LP-19d's rule does not already cover the paths),
    `src/mendari/dashboard/registry.js`, `tests/test_shipped.py`,
    `docs/operations/accounts.md`. New: `tests/test_registry_site_accounts.py`,
    `tests/browser/test_registry_sites.py`.
  - **Tests:**
    - `tests/test_registry_site_accounts.py::test_a_member_registers_a_site_and_a_reviewer_verifies_it`;
    - `::test_one_organization_never_sees_or_changes_anothers_sites`;
    - `::test_a_key_a_client_and_a_viewer_cannot_register_a_site`;
    - `::test_a_reviewer_does_not_verify_their_own_organizations_site`;
    - `::test_an_administrator_elsewhere_is_not_a_site_reviewer`;
    - `::test_another_sites_page_cannot_register_or_verify`;
    - `tests/browser/test_registry_sites.py::test_from_a_registered_site_to_a_verified_one`;
    - `tests/browser/test_registry_sites.py::test_a_revoked_key_says_so_and_gets_no_ticket`.
  - **Safe alone because:** routes and tabs that exist only when the registry is mounted and
    sign-in is on; the store's rules are 22d's.
  - **Threat model:** E20 (second half).

- [ ] **22j** Close-out.
  - `launch/gates.yaml`:
    - `a11-site-network` (`:594-598`): the pending line becomes the tests of 22b to 22h (the
      list in the next section).
    - `site-validation` (`:681-686`): the pending line goes; the owner's attestation stays, so
      the prerequisite stays unmet until the owner fills it in per site.
    - `site-agreements` (`:676-680`): not touched. It is the owner's alone.
  - ADR-0017 to Accepted once the owner confirms the decisions.
  - `docs/security/threat-model.md`: the ten rows, each naming its tests; R3 reworded (a site
    report is now signed); the residual-risk paragraph at `:400` replaced by what is left: a
    site can still choose which cases it reviews before any ticket; the registry's chain is
    still unkeyed; a site's name is as good as the reviewer's check of it.
  - `docs/spec/decision-pack-0.2.md` (or the spec document current then): the shared report
    is `/2`, signed, with a ticket.
  - `docs/roadmap.md:68`, `README.md`, the `/docs` landing where they name sharing.
  - `tasks/todo.md`: LP-22 checked, the owner steps listed.
  - `tests/test_gates.py` and `tests/test_shipped.py` green.

## Which pending probes each PR replaces

| `launch/gates.yaml` | Today | Replaced by |
|---|---|---|
| `:598` a11-site-network | pending: LP-22, after LP-07 (done), LP-10 and LP-19 | 22j, with tests from 22b (`tests/test_pooling.py`: the pooled, random-effects and homogeneity references), 22c (`tests/test_share.py`: counts only, the suppressed cell), 22d (`tests/test_registry_sites.py`), 22e (`tests/test_site_tickets.py`), 22f (`tests/test_site_reports.py`: forged, unregistered, different body, replay, case text), 22g (`tests/test_site_evidence.py`), 22h (`tests/test_site_separation.py`). LP-19k may reword the line first (it drops "and LP-19"); 22j replaces whatever is there |
| `:685` site-validation | pending: LP-22 (sharing signed results), then an owner attestation | 22j removes the pending line and adds `tests/test_site_reports.py::test_a_verified_report_names_its_site_key_and_ticket_and_its_signature_checks`; the attestation stays the owner's |
| `:680` site-agreements | an owner attestation only | no PR |

No LP-22 PR fills in an attestation or marks an owner prerequisite met.

## Conflicts written before building, and the default each PR follows

1. **"Reuse the signing code in `src/mendari/hub/archive.py`."** Its key helpers fit; its sign
   and verify are for archives (fact 10). Default: keys through `archive.py`; a site's record
   is signed as a recall is (`yank.py:96-130`), with formats and a prefix of its own. (22e)
2. **`mendari.site-report/1` holds rates; the prompt says to accept counts only.** LP-07 left
   "counts in, rates out" to LP-22 (`tasks/todo.md:73-74`). Some of `/1` is not a count at all:
   ECE and Brier need each case's confidence, latency and dollar cost are measurements. Default:
   `/2` holds whole counts only; calibration is shown bin by bin as k of n, with no ECE; Brier,
   latency and dollar cost stay with the site. New `/1` reports are refused. (22c)
3. **Subgroups.** LP-07 took them out until packs declare each subgroup's values
   (`share.py:9-11`); LP-22's prompt does not bring them back. Default: still out of `/2`. They
   return with a spec change that declares the values, which is not LP-22. (22c)
4. **How a site proves who it is.** Today a publisher token submits (fact 2), and a token
   belongs to a namespace; a hospital has none. Default: a site signs. The ticket's one-time
   secret lets the registry refuse before it reads a body. Publisher tokens no longer share
   reports, and `--share` no longer takes a free name. (22e, 22f)
5. **"A verified site record owned by an organization", and a registry with no sign-in.**
   A self-hosted registry and CI have no one signed in (LP-19's conflict 1,
   `notes/lp19-plan.md:589-592`). Default: the operator's commands register and verify a site,
   with a reason on the chain; the web path needs sign-in and a registry reviewer. (22d, 22i)
6. **"On the scoreboard and Hub."** The scoreboard page is made from the repository's results
   store and reads no registry (fact 15). Default: the Hub shows site evidence; the scoreboard
   page links to it and ranks none of it; a test pins that no ranking module imports site
   code. (22h)
7. **"Matches a scipy reference", in a product with no scipy.** scipy is a development
   dependency only, and `stats.py` has no chi-square (fact 13). Default: pure Python, with an
   incomplete gamma function added, checked against scipy in tests. (22b)
8. **"An exact or seeded permutation test."** The prompt allows either. Default: exact by
   enumeration when the tables are few enough, a seeded permutation test otherwise, the seed in
   the output. (22b)
9. **"A random-effects estimate"** names no method. Default: DerSimonian-Laird on the logit
   scale, shown from 3 sites, labelled with its method (decision 4). (22b)
10. **"One live submission per site, digest, configuration and commitment", and mistakes.**
    A site that finds an error needs a way to correct it that is not a silent second try.
    Default: a new ticket that names the report it supersedes; both stay listed. (22f)
11. **A ticket expires, and the chain refuses a row that changes without an event** (fact 11).
    Default: expiry is worked out on read from the ticket's dates; only issue, report and
    withdrawal write. (22e)
12. **The digest.** The prompt says a digest that is not a published version's is refused;
    today the registry checks the id and version only (fact 7). Default: the ticket and the
    report must carry the published version's digest, and the version must be listed and not
    recalled or deleted. (22e, 22f)
13. **"Site evaluation defaults to the deterministic-only arm", and a registry that cannot see
    a site's configuration files.** The registry knows provider types, not endpoints (fact 14).
    Default: the registry asks for consent whenever a configuration uses a type outside the
    local set; the command line refuses to run such a share without `--consent-remote`. (22e)
14. **"Before automation" (gate `site-validation`, B42).** Nothing in LP-22's prompt makes the
    engine ask for a site report before AUTO, and the audit found nothing that does
    (`notes/vision-audit.md:272`). Default: LP-22 does not change what may automate; the signed
    report's id is what the owner's attestation cites. (decision 10)
15. **Old reports.** Default: unsigned rows stay stored, chained and unserved; none is
    converted or deleted. No hosted registry exists, so this touches local databases only.
    (22f)

## Owner steps (not done by any PR)

1. **Agreements with each site** (gate `site-agreements`): the pilot agreement, the BAA, the IRB
   or QI determination and the site's governance approval. They are the owner's and counsel's;
   no PR drafts, records or attests them.
2. Check, outside Mendari, that each organization asking for a site is who it says, then verify
   the site as a registry reviewer (after 22i), or with the operator's command.
3. Tell each site how to run `mendari site init` and where its key and commitment secret must
   be kept (they never leave the site; losing the secret only means a new commitment).
4. Decide per site whether its name is shown (decision 1) before its first report.
5. Attest `site-validation` per site, with the pack digest, the reviewed set's size and method
   and the signed report's id.
6. LP-19's own steps come first: the registry mounted, the review organization named, the
   registry's key made (`notes/lp19-plan.md:635-647`).

## Owner decisions still open (each PR follows the default until told otherwise)

1. **Are sites named in public.** Default: yes, by the name the reviewer verified; a result
   nobody stands behind resists little. Alternative: "Site 1" to "Site N", the names known to
   reviewers only.
2. **The publisher's own site.** Default: its reports are listed, labelled as the publisher's,
   and left out of the pool. Alternative: refused.
3. **The suppression numbers.** Default: cells of 1 to 9 suppressed (the existing
   `MIN_GROUP = 10`), zeros shown, and no sharing under 100 cases. Alternatives: suppress zeros
   too; a minimum of 50.
4. **The random-effects method.** Default: DerSimonian-Laird, logit scale, from 3 sites.
   Alternative: the Hartung-Knapp adjustment (wider, safer with few sites), or none shown under
   5 sites.
5. **A ticket's life.** Default: 30 days; an unreported ticket is listed for good.
6. **Who registers and who verifies.** Default: an organization's administrators register; a
   registry reviewer verifies, never for their own organization; the operator's command is the
   way out, with a reason.
7. **Remote-arm site evidence.** Default: accepted with recorded consent, and never pooled with
   a local configuration. Alternative: not accepted until the provider's benchmark-publication
   terms are read (the scoreboard's open decision 8).
8. **Which figures are pooled.** Default: the primary question's accuracy, each critical
   label's detection, critical cases automated, and false automation.
9. **A compromised key.** Default: every report signed with it leaves the pool and stays listed
   as such. Alternative: only reports received after a date the site gives.
10. **Does a deployment need its own site report before it may automate (B42).** Default: not
    in LP-22; the gate's attestation is the check. Alternative: a later item makes the engine
    ask.
11. **How many figures does the homogeneity test cover.** Default: each pooled figure is tested
    alone at 0.05, and the answer says how many tests were run. Alternative: Holm over the
    family (`stats.holm`).
12. **Is the commitment shown.** Default: never served; the registry uses it only to refuse a
    second live result on the same data.

## Left out of LP-22, on purpose

- Subgroup cells (conflict 3); ECE, Brier, latency and cost across sites (conflict 2).
- Making the engine require a site report before automation (decision 10).
- Any agreement, BAA or IRB text (owner step 1).
- A keyed registry chain or a signed checkpoint of its head (LP-19 leaves it out too).
- Proof that a site's cases were chosen fairly: a ticket binds a dataset before the run, not
  how the dataset was drawn.
- A site running the hidden sets, or site evidence in any rank.

## Risks

1. **A site can still choose its cases before it asks for a ticket.** Tickets and commitments
   stop picking a result after the run; they cannot show how charts were sampled. The guide
   says so, and the Hub's sentence does not claim more.
2. **Small numbers in public.** Suppression with a written rule per field, checked on both
   sides, and a test that tries to recover a suppressed cell. Safety counts are shared exactly
   by a stated rule; that is a choice (decision 3) the owner should see.
3. **A wrong pooled figure looks authoritative.** Per-site figures come first, the pooled one
   is labelled with its method, sites that disagree raise a warning in words, and nothing is
   pooled across digests or configurations.
4. **Statistics written from scratch.** Each new function is checked against scipy or
   statsmodels and a hand-computed fixture; the permutation test is seeded and its seed shown.
5. **A public read that does real work.** The exact and permutation tests are bounded
   (1,000,000 tables, 10,000 resamples) and cached per set of reports; reads are rate-limited
   per address (`registry.py:1501`).
6. **A lost site key or secret.** A lost key is revoked and replaced by the organization; a
   lost commitment secret cannot be recovered, and old commitments can no longer be re-proved.
7. **`registry.py` is being rewritten by LP-19.** LP-22's code lives in new modules, and the
   merge order above keeps each edit to that file small.
8. **Breaking the command line twice** (22c, then 22e and 22f). No hosted registry exists, and
   each change is named in the changelog with what to run instead.
9. **Parallel lanes and ids.** ADR-0017 and the ten threat rows were free at ca7e016b only;
   LP-20's plan picks its own at the same time.

## Open points not verified by the planning read

1. **LP-20's plan** was being written and was not read, beyond its session's note of the ADR
   number and threat rows it reserved: whether it changes the people's routes or the roles 22i
   uses is not known.
2. **Whether every count 22c needs is in the ledger as stored**, in particular how many
   reviewed cases would be overridden and how many cases in each calibration bin were right.
   The ledger's format was read from its docstring (`ledger.py:1-25`), not line by line.
3. **Whether scipy 1.15's `fisher_exact` takes tables larger than 2 by 2.** The exact test's
   reference is planned from the multivariate hypergeometric distribution instead, so the test
   does not depend on it.
4. **How LP-19c's `listed` rule is exposed by the store**, which 22e uses to refuse a ticket
   for an unlisted namespace: 19c was not merged when this was written.
5. **LP-19d's rule for `/api/registry/…`** (`notes/lp19-plan.md:288`) names `namespaces` and
   `tokens` only; 22i must add `sites` there, in `server/auth.py`, which was not read here.
6. **Whether the docs build can know a registry's address** for the scoreboard page's link
   (22h). If it cannot, the page carries the sentence and no link.
7. **The Hub's CI seed** (`scripts/hub_seed.py`) was not read; 22h's two synthetic sites are
   added to whatever LP-19g leaves there.
8. **The manifest's subgroup declarations** were not read; fact 6 rests on the share module's
   own account of them.
