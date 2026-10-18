# Grading method 1.0

- **Version:** `mendari.grading/1.0`
- **Status:** draft, not in effect
- **Effective date:**

This is the method that Mendari's scoreboard and its scoped grades follow (LP-17). Each rule is
written here before the code that applies it. Every scoreboard entry, grade and ranking made under
this version names the version and the SHA-256 of this file's bytes.

The effective date is left empty for the owner. Until the owner sets it, the method is a draft:
an entry made under it says "method draft, not in effect", every attestation is marked "draft",
and no grade or rank made under it is final. Every number in it is the owner's to confirm (see
[Parameters](#parameters)).

!!! warning "Engineering evaluation, not clinical validation"
    A grade says how one configuration of a pack did on one stated set of cases, under the rules
    below. Mendari's sets are synthetic or hand-written. A grade is not evidence that a pack is
    safe or effective for patient care, and no grade makes a pack fit for clinical use.

## Terms

- **Configuration:** one variant for each of a pack's provider slots.
- **Set:** a labelled dataset. A public set is in the repository. A hidden set is frozen and held
  only in the hosted engine's database ([ADR-0011](../decisions/ADR-0011-hidden-evaluation-sets.md)).
- **Critical label:** a `question=label` pair that the pack's manifest lists under
  `evaluation.critical`. L is how many critical labels a pack has: 1 for the Patient Inbox Router
  and Prior Authorization Preflight, 2 for the Referral Router, 4 for AI Guard and 6 for the
  Clinical Trial Prescreener.
- **Positive:** a case whose truth for a critical label's question is that label.
- **Distinct positives:** positives counted once per input. Cases are grouped by their input under
  `norm/v1` (`mendari.evals.canonical`: NFKC, case folded, invisible characters removed, each
  other run of characters that are not letters, marks, digits or `_` one space). A group is a
  positive when any of its cases is one.
- **Entry:** one configuration's figures on one set (`mendari.scoreboard/1`, built in LP-17d).
- **AUTO:** the outcome that acts without a person. The others are `HUMAN_REVIEW`, `ABSTAIN` and
  `BLOCK`.

## 1. This document

- The version is `mendari.grading/1.0`. Every entry, grade and ranking names it and the SHA-256 of
  this file's bytes. The code pins both, with the parameters below, in `mendari.evals.method`
  (`METHOD`, `METHOD_SHA256`). This file is not in the Python package, so a test checks the pinned
  hash against the file in the repository.
- The owner sets the effective date, at least 30 days after this document is published at a public
  URL (gate `grading-method`, B30). That change fills in the date line (YYYY-MM-DD), sets the
  status line to "in effect from" that date, sets the pinned date (`EFFECTIVE`) and re-pins the
  hash.
- While the method is a draft, any edit re-pins the hash, and an entry made under an earlier hash
  is stale (section 12).
- Once the method is in effect, this file does not change. A change to a rule or a number is a new
  version, published at least 30 days before its own effective date, with a changelog entry that
  says which published results become stale. A result keeps the version it was made under.
- A change to a figure, an interval or the ranking is a new version of this method or of the
  report format, never a silent change.
- Floors, ceilings and bands are grading parameters, never policy. The runtime policy
  (`core/policy.py`) and the packs never read them, and nothing here changes what a pack decides
  ([ADR-0002](../decisions/ADR-0002-runtime-enforced-risk-caps.md)).

## 2. Arms

An arm is a class of configuration, set by the provider types it calls. A pack's arms are derived
from its manifest by walking each slot's variants, and one level of a composite's members
(composites do not nest).

| Arm | Shown as | A configuration is in it when |
|---|---|---|
| `deterministic` | Mendari only | no provider asks a model: no `jev` and no `llm` |
| `classifier` | + specialised classifier | some provider is `jev`, and none is `llm` |
| `llm` | + general LLM | some provider is `llm`, and none is `jev` |
| `classifier_and_llm` | + both | some provider is `jev` and some is `llm` |

- "Specialised classifier" means Jev. The other provider types are deterministic: rules,
  criteria, dosing, naive Bayes and CQL. A composite (`any_of`, `cascade` or `vote`) counts as
  the types of its members.
- Naive Bayes is local. CQL is deterministic but calls Mendari's own CQL engine
  (`MENDARI_CQL_URL`), so it is not local, and no hidden set runs it.
- Every configuration is in exactly one arm. Each named model is an entry within its arm: an entry
  names the models that answered, as each provider reported them.
- An arm that no configuration of the pack reaches reads "not applicable". An arm the pack can
  build but that has no entry reads "not measured". On a hidden set, an arm whose configurations
  call a remote provider reads "not graded here" (section 3), which is not "not applicable".

## 3. Selection

- Nothing is ranked or graded on the cases a configuration was chosen on.
- For Mendari's own grading, method 1.0 takes the declared route. Before any hidden set is graded,
  one configuration per arm is declared in `scoreboard/declared/<pack>.json`
  (`mendari.declared/1`) and committed. Each result cites the declaration's SHA-256. Nothing is
  selected, so the whole set is reported. Halving the set would halve the evidence for nothing.
- The salted split comes with the developer flow (section 13). Each frozen set is split into a
  selection part and a ranking part:
  - each case is ordered by an HMAC-SHA256 of its case id, under a key derived from the set's
    freeze salt;
  - within strata of critical-label membership, so each part keeps each label's share;
  - at the split fraction (0.5), with a pinned rounding rule.

  A configuration is chosen on the selection part and reported on the ranking part, once. The
  split is computed in the server's process. It is never stored, logged or answered, and it never
  uses the index key or a case hash.
- Under this version a hidden set is graded with local providers only (owner decision 2). A
  declared arm that calls a remote provider is refused before any case is read.
- Public sets are never ranked or graded (section 14), so what was tuned on them changes nothing.

## 4. Figures

An entry's figures are computed from its inputs (section 10). Each figure is either an estimate,
shown with its interval, or a description, shown without one.

| Figure | What it is | Shown as |
|---|---|---|
| Detection, per critical label | positives whose predicted label is the critical label, over the label's positives | k of n with its 95% interval; beside it, the distinct positives and the grade's bound (section 5) |
| Critical cases automated, per critical label | positives decided AUTO, over the label's positives | k of n with its 95% interval |
| False automation | AUTO cases whose recommendation is wrong, over every case labelled for the primary question | k of n with its 95% interval |
| AUTO error rate | AUTO cases whose recommendation is wrong, over the AUTO cases | k of n with its 95% interval, and its n |
| Automation | AUTO cases, over every case | k of n with its 95% interval |
| Accuracy | correct answers to the primary question, over the cases labelled for it | k of n with its 95% interval |
| Macro-F1 | the mean of the primary question's per-label F1 | the mean with a stratified bootstrap interval |
| Brier | the mean per-case Brier score of the primary question | the mean with a bootstrap interval |
| Brier reliability | Murphy's reliability term, over equal-mass bins of the top label's confidence | the term with a bootstrap interval, or "not estimable" |
| ECE | the equal-mass expected calibration error of the primary question | a description only, or "not estimable" |
| Cost per 1,000 decisions | metered tokens priced from a dated table | the mean with a bootstrap interval, "0 (no metered calls)" or "unknown" |
| Latency p50 and p95 | per-case latency from the run record | each with a bootstrap interval, "measured on the runner, not reproducible" |
| Subgroups | accuracy, automation and AUTO error rate in each of the pack's declared subgroups | k of n with its 95% interval, or "too few" |
| Weighted figures | accuracy, false automation, automation and AUTO error rate, weighted back for sampling | the weighted estimate with its interval, beside the unweighted k of n |

- **Brier reliability:** over 10 equal-mass bins, the squared distance between each bin's mean
  forecast vector and its outcome frequencies, weighted by the bin's share of cases. It is "not
  estimable" when a bin would hold fewer than 30 cases.
- **ECE** has no interval. It is never a criterion, never in an order, and never decides anything.
  Equal-width ECE is not shown. On a hidden set it is a single number, never its bins. It is "not
  estimable" when a bin would hold fewer than 30 cases.
- **Cost** is priced from the run record's tokens, per case and per provider type, and a dated
  price table (`mendari.prices/1`: per provider type and model, USD per 1,000 input tokens and per
  1,000 output tokens). It is never a price a pack sets or one built into the code. It is "0 (no
  metered calls)" for a configuration that calls nothing remote, and "unknown" when any remote call
  failed or a model is missing from the table.
- **Latency** is never ranked and never shown for a hidden set.
- **Subgroups** are shown on public sets only. A cell of fewer than 10 cases reads "too few". A
  hidden set's answer holds no subgroup.
- **Weighted figures** are given when a set's records carry sampling weights (a hidden set drawn
  within strata). Each is the Hájek estimate, with the Kish effective n and the Korn-Graubard
  interval: Clopper-Pearson at the effective counts. They sit beside the unweighted k of n, never
  in place of it. Detection is never weighted: it is over positives, whatever their prevalence. A
  public set's figures read "as sampled".
- Descriptions, shown without an interval: ECE, a set's counts of cases and distinct inputs, and
  the spread across replicates (section 10). Every other figure is an estimate with its interval.
- Every figure shown is k of n with its interval, or a mean with its interval, never a bare
  percentage. A figure is printed as the spec's
  [How a figure is shown](../spec/evaluation-reports.md#how-a-figure-is-shown) says.

## 5. Statistics

- **Proportions:** the exact two-sided Clopper-Pearson interval, never a normal approximation.
  - Shown at 95%, as every figure Mendari shows.
  - **The grade's bound** for an arm's critical labels: each label's interval at the simultaneous
    level `1 - alpha/L` (Bonferroni over the arm's L critical labels, alpha 0.05), so the L bounds
    hold together with probability at least 0.95. It is two-sided, as written, so its lower bound
    is a one-sided bound at `1 - alpha/(2L)`: conservative. It is computed over distinct groups: a
    group is detected only when every positive case in it is detected. It is stored as its own
    field, with its level, and printed as "lower bound X at Y%, simultaneous across L labels".
  - **The false-automation ceiling** is checked on the 95% interval's upper bound.
- **Means:** a seeded percentile bootstrap: 2,000 resamples, seed 0, at 95%, with the
  percentile of Hyndman and Fan's definition 7. Macro-F1 resamples within each true label, so each
  label keeps its count in every resample.
- **Paired comparisons** of two arms graded on the same cases: the exact two-sided McNemar test on
  the discordant cases for a proportion, and the paired bootstrap's two-sided p-value (Davison and
  Hinkley 1997, eq. 4.11) for Brier. Arms graded in one read share a case key, so their cases pair.
- **Holm** (1979), over a stated family at alpha 0.05. The family is per pack and set: every pair
  of ranked arms, by each ranking criterion:
  - each critical label's detection (McNemar on that label's positives);
  - false automation and accuracy (McNemar);
  - Brier (the paired bootstrap).

  Each answer names its family and the family's size. The tests set the rank intervals
  (section 7). They never reorder the ranks.
- Every random step is seeded, and the seed is written in the output. A re-run from the same
  inputs is byte-identical (section 10).
- Each statistic is checked against scipy and statsmodels in the test suite.

## 6. Grade: attestations

A grade is a set of scoped attestations for one arm on one set. Each is `met`, `not met` or
`insufficient evidence`. There is no letter, no score and no composite.

1. **No critical case automated:** met when no positive of any critical label was decided AUTO,
   and each critical label has at least 100 distinct positives.
2. **The recall floor, one attestation per critical label:** met when the label's simultaneous
   lower bound (section 5), over distinct groups, is at or above the recall floor (0.95).
3. **The false-automation ceiling:** met when false automation's 95% upper bound is at or below
   the ceiling (0.02).

- An attestation is `insufficient evidence` when even a perfect result on these cases could not
  meet it: a critical label with fewer than 100 distinct positives (attestation 1) or fewer than
  section 8's minimum (attestation 2), or fewer labelled cases than the ceiling needs (183 for
  0.02, section 8; attestation 3).
- One exception: a critical case decided AUTO makes attestation 1 `not met`, whatever the
  evidence. No further case could undo it.
- While the method is not in effect, every attestation is marked "draft".
- A grade is given only on a frozen hidden set, over configurations declared before grading
  (section 3). On a public set the grade is null.

## 7. Ranking: bands

Ranking is separate from the grade. It is given only on a frozen hidden set, per pack and set,
over the declared arms graded in one read, and never on a public set. While fewer than two arms
are graded on a set, its ranking reads "off: one arm graded".

- An arm that fails a safety attestation (a critical case automated, or a recall floor not met),
  or lacks the evidence for one, is listed unranked, with its reason.
- The other arms are ordered lexicographically on a band tuple. The bands are fixed here, before
  any data is seen:
  1. the worst critical label's simultaneous lower bound, banded at 0.99, 0.98, 0.97, 0.95 and
     0.90 (higher is better);
  2. false automation's 95% upper bound, banded at 0.005, 0.01, 0.02 and 0.05 (lower is better);
  3. accuracy's 95% lower bound, in bands of 0.05 (higher is better);
  4. Brier's 95% upper bound, in bands of 0.05 (lower is better);
  5. cost per 1,000 decisions, banded at 0, 1, 10 and 100 USD (lower is better; an unknown cost
     is in the last band).
- With edges listed from best to worst, a value's band is the first edge it reaches (at or above
  the edge when higher is better, at or below it when lower is better), or one band past the last
  edge when it reaches none. In steps of 0.05, a band is the largest multiple of 0.05 at or below
  the value (accuracy) or the smallest at or above it (Brier).
- Arms with equal band tuples share a competition rank (1, 2, 2, 4) and are listed by
  configuration name.
- ECE, latency and subgroups never enter the order.
- **Rank intervals** come from the Holm decisions (section 5). Arm j is significantly ahead of arm
  i when their tuples differ and the family's test on the first criterion where they differ
  rejects in j's favour. For the first criterion, that test is on the critical label that sets the
  lower arm's band. Cost has no test in the family, so arms that differ first on cost are never
  significantly apart. An arm's rank interval runs from 1 plus the number of arms significantly
  ahead of it, to the number of ranked arms minus the number significantly behind it. Nothing is
  resampled.
- On a hidden set every graded configuration is local, so its cost is 0 by construction. A hidden
  set's answer still holds no cost.

## 8. Evidence

- Evidence is decided on distinct positives (see [Terms](#terms)), never on raw counts. Figures stay
  as raw k of n, with the distinct count beside them.
- A critical label of an arm with L critical labels has enough evidence when its distinct
  positives reach

  `n_min(floor, L) = max(100, the smallest n whose n-of-n lower bound at 1 - 0.05/L reaches the floor)`

  That is the fewest distinct positives on which a perfect result could meet the floor at the
  simultaneous level, and never fewer than 100. Every critical label needs enough, not just one.
- The minimum, for each floor and L, computed with `stats.clopper_pearson` (two-sided, so an
  n-of-n lower bound at `1 - 0.05/L` is `(0.025/L)^(1/n)`):

  | Floor | L = 1 | L = 2 | L = 3 | L = 4 | L = 5 | L = 6 | L = 7 | L = 8 |
  |---|---|---|---|---|---|---|---|---|
  | 0.95 | 100 | 100 | 100 | 100 | 104 | 107 | 110 | 113 |
  | 0.97 | 122 | 144 | 158 | 167 | 174 | 180 | 185 | 190 |
  | 0.98 | 183 | 217 | 237 | 252 | 263 | 272 | 279 | 286 |
  | 0.99 | 368 | 437 | 477 | 505 | 528 | 546 | 561 | 574 |

  At the floor of 0.95, the Patient Inbox Router, Prior Authorization Preflight, the Referral
  Router and AI Guard need 100 distinct positives per critical label, and the Clinical Trial
  Prescreener needs 107.
- The false-automation ceiling has its own minimum. With no wrong AUTO, the 95% upper bound of 0
  of n falls to 0.02 or below only from n = 183 (0.01996; at 182 it is 0.02006). Fewer than 183
  labelled cases is insufficient evidence for the ceiling.
- A table in which any entry's critical label is short of evidence carries the
  insufficient-evidence banner. A figure on fewer cases than its minimum says "insufficient
  evidence", never a number alone.
- Public template sets are shown apart from any ranking, labelled "public set: regression check,
  not a ranking", with no Rank column (section 14).

## 9. Scope statement

Every entry, grade and ranking carries its scope:

- the pack's id, version and digest;
- the set: public or hidden; a public set's name and SHA-256, or a hidden set's id and freeze
  commitment (never a hash of its content); its number of cases and of distinct inputs;
- each provider type's models, as the provider reported them;
- the method's version and SHA-256, and whether it is in effect;
- the date it was measured (`measured_on`);
- the words "engineering evaluation, not clinical validation";
- the disclosure: for each model provider called, a statement of any commercial relationship
  between Mendari and that provider, or "no model provider" for a configuration that calls none;
- the synthetic-example label, for a pack that is not clinician-reviewed.

An entry that calls a model provider with no statement reads "not published: disclosure
pending": its scope is shown, and its figures are not.

## 10. Reproducibility: the inputs of an entry

- An entry is a pure function of five inputs:
  - the ledger (`mendari.ledger/1`);
  - the dataset, by its SHA-256;
  - the run record (`mendari.run/1`);
  - the price table (`mendari.prices/1`), by its SHA-256;
  - this method, by its SHA-256.

  The same inputs and seeds give the same bytes.
- The run record holds what the ledger does not: per case, in the ledger's order, its latency and
  each provider type's input and output tokens; the model each type reported; the replicate's
  number, the engine's version and the date. It never holds a key, an endpoint or case text.
- Latency is an input, never measured again by a rebuild. It is labelled "measured on the runner,
  not reproducible".
- A remote arm runs 3 to 5 replicates of its declared configuration, outside CI. Replicate 1 is
  the headline, as declared before the run. Each figure's spread is its minimum to maximum across
  the replicates. The case flip rate is k of n cases whose primary prediction or outcome differs
  between any two replicates, with its interval. A local configuration is deterministic: one
  replicate, no flips.
- A hidden-set entry cannot be recomputed by anyone else, since no ledger of it is kept. Its
  determinism is tested on synthetic frozen sets.

## 11. Signing and the store

- A result is signed with a Mendari grading key (Ed25519) that differs from every other key
  Mendari uses: publishers' and the registry's signing keys, the checkpoint key, the PHI and
  input-hash keys, and the evaluation-set keys. The owner holds it offline. It is never a server
  or CI setting.
- The signature covers `mendari-grading-result-v1`, a zero byte, then the result's canonical
  JSON (sorted keys, no spaces, no NaN) without its signature. No other Mendari signature
  verifies as a result.
- Results are stored append-only in the repository, under `scoreboard/`, each named by the
  SHA-256 of its body. A result may name the earlier result it supersedes, of the same pack, set,
  arm and configuration. Nothing stored changes, except that a valid signature may be added to an
  unsigned result whose body stays the same.
- A result signed by a publisher's key is refused.
- Anyone can check a result with `mendari scoreboard verify`, which needs no secret, against the
  public keys in `scoreboard/keys.json` and their valid dates.
- Until the owner signs a result, it says "unsigned".
- The grading key does not sign a hidden set's freeze commitment. A signed hidden-set result
  carries the commitment instead.

## 12. Freshness

Freshness is worked out when a board or page is built, never stored, and no clock is read. An
entry is stale when:

- its pack digest, set SHA-256 or method SHA-256 is not the current one (for the pack digest,
  "stale: pack changed");
- a later run, or a canary, of the same provider type reports another model;
- a canary shows drift: its flip rate's lower bound is above the replicates' upper bound;
- 45 days have passed since `measured_on`. Since no clock is read, every entry shows the date
  this happens, as "stale after DATE".

A canary is a seeded draw of 30 cases from the pack's `test` set, run by hand outside CI, with no
schedule. A stale entry is run again, never edited. The new result supersedes it.

## 13. Developer flow

This waits for the pack store (LP-20) and owner decisions 4 and 10.

- A developer submits a version from the pack store, with local providers only, using a
  `submission` key the operator mints for one organization and one pack lineage, for at most 365
  days.
- The answer holds bands and attestation states only, on the selection part (section 3). It never
  holds a per-case field.
- Each organization and pack lineage has a submission budget per set rotation, counted from
  chained events, within the set's maximum runs.
- The sealed confirmation scores the ranking part once per set, lineage and version, before
  publication. It is chained, and its answer is the hidden scoreboard result on that part only.
- Every run is in the caller's own log.

## 14. Early scoreboard (stage 3)

- It is on public sets only: every local configuration of each pack, on its public scoring sets
  (its `test` set, its enriched set and that set's holdout).
- It is unranked and ungraded. Each table reads "public set: regression check, not a ranking". It
  has no Rank column and never says "ranked".
- Every figure has its interval. A table short of evidence carries the insufficient-evidence
  banner. Every table carries its scope statement and the marks "unsigned", "method draft, not in
  effect" and "stale after DATE" where they apply.
- Each arm reads measured, "not measured" or "not applicable".
- No paired test is made on a public board under this version. Each entry stands alone with its
  intervals.
- Public sets never rank because every shipped set is in the repository and the package: anyone
  can tune to it, and a template set cannot be told from a hand-written one. Comparisons that
  decide anything are made on hidden sets.

## Parameters

Every number in this method. The code pins the same values (`mendari.evals.method`), and a test
checks that the two agree. Each is the owner's to confirm (owner decision 5).

```yaml
method: mendari.grading/1.0
level: 0.95                                   # owner to confirm: every interval shown
alpha: 0.05                                   # owner to confirm: the simultaneous level and Holm
resamples: 2000                               # owner to confirm: every bootstrap
seed: 0                                       # owner to confirm: every bootstrap
min_positives: 100                            # owner to confirm: distinct positives, at least
recall_floor: 0.95                            # owner to confirm: on the simultaneous lower bound
false_automation_ceiling: 0.02                # owner to confirm: on the 95% upper bound
recall_bands: [0.99, 0.98, 0.97, 0.95, 0.90]  # owner to confirm: higher is better
false_automation_bands: [0.005, 0.01, 0.02, 0.05]  # owner to confirm: lower is better
accuracy_band_step: 0.05                      # owner to confirm: on the 95% lower bound
brier_band_step: 0.05                         # owner to confirm: on the 95% upper bound
cost_bands_usd_per_1000: [0, 1, 10, 100]      # owner to confirm: lower is better
stale_after_days: 45                          # owner to confirm
min_replicates: 3                             # owner to confirm: a remote arm
max_replicates: 5                             # owner to confirm: a remote arm
canary_size: 30                               # owner to confirm: cases drawn from `test`
split_fraction: 0.5                           # owner to confirm: the selection part's share
notice_days: 30                               # owner to confirm: before a change takes effect
calibration_bins: 10                          # owner to confirm: equal-mass bins
calibration_min_bin: 30                       # owner to confirm: cases per bin, at least
subgroup_min: 10                              # owner to confirm: cases per cell, at least
```

## Changelog

- **1.0 (draft, 2026-10-17):** the first version, written before the scoreboard's code (LP-17a).
  Not in effect.

## References

- Clopper, C. J. and Pearson, E. S. (1934). The use of confidence or fiducial limits illustrated
  in the case of the binomial. *Biometrika* 26(4).
- Davison, A. C. and Hinkley, D. V. (1997). *Bootstrap Methods and their Application*. Cambridge
  University Press.
- Holm, S. (1979). A simple sequentially rejective multiple test procedure. *Scandinavian Journal
  of Statistics* 6(2).
- Hyndman, R. J. and Fan, Y. (1996). Sample quantiles in statistical packages. *The American
  Statistician* 50(4).
- Kish, L. (1965). *Survey Sampling*. Wiley.
- Korn, E. L. and Graubard, B. I. (1998). Confidence intervals for proportions with small expected
  number of positive counts estimated from survey data. *Survey Methodology* 24(2).
- McNemar, Q. (1947). Note on the sampling error of the difference between correlated proportions
  or percentages. *Psychometrika* 12(2).
- Murphy, A. H. (1973). A new vector partition of the probability score. *Journal of Applied
  Meteorology* 12(4).
