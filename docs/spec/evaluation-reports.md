# Evaluation reports, ledgers and paired comparisons

This page specifies what an evaluation run writes: the report (`mendari.eval/2`), the per-case
ledger its result is computed from (`mendari.ledger/1`), and a paired comparison of two runs
(`mendari.compare/1`). It supersedes the report format of the
[Decision Pack spec 0.2](decision-pack-0.2.md) (`mendari.eval/1`), which still loads. The key
words MUST, SHOULD and MAY are used as in that spec.

## The report: `mendari.eval/2`

A report has two parts.

- **The result:** every key except `run`, `comparison`, `result_sha256` and `figures` (what a
  server serves beside a report, never stored). It MUST be computed
  only from the run's ledger and the seeds it names, so two runs on the same cases with the same
  pack, providers and seeds give byte-identical results (canonical JSON: sorted keys, no spaces),
  and anyone holding the ledger can recompute it.
- **The run:** `run` holds what differs between two runs on the same cases: its `id`,
  `created_at`, `latency_ms` (mean, p50, p95, max), the engine version, and a governance
  override's approver (kept locally, never in the result). `result_sha256` is the SHA-256 of the
  canonical result.

`comparison`, outside the result, is the local validation against the pack's benchmark. For a
pack pulled from a registry it is never made with the benchmark the pack carries, which is its
publisher's upload
([ADR-0012](../decisions/ADR-0012-a-registry-that-shows-only-what-it-checked.md)): it is
`{"status": "unknown", "reason": ..., "findings": []}`, as for a benchmark that cannot be
compared, and says why.

The result MUST carry everything a `mendari.eval/1` report carries, except case ids, and adds:

- `ledger`: the ledger's id (`sha256:` of its bytes) and its number of cases;
- `seeds`: the bootstrap seed, the number of resamples (2,000) and the interval level (0.95);
- `proportions`: each proportion as `{k, n, low, high}`, with its exact Clopper-Pearson interval
  (never a normal approximation). Primary accuracy, automation (AUTO over all cases), false
  automation (AUTO with a wrong recommendation, over all cases labelled for the primary
  question), accuracy of what was automated, abstention, recommendation accuracy, and each
  critical label's detection (`critical`, keyed `question=label`);
- per question, `proportion` beside `accuracy`, `brier_interval` (a seeded percentile bootstrap
  of the mean of per-case Brier scores), and `calibration.equal_mass`: the expected calibration
  error over ten equal-count bins, tied confidences averaged, with `status: not_estimable` when a
  bin would hold fewer than 30 cases. The equal-width bins table is kept;
- per critical label, `proportion` beside `detection_rate`, and `missed_cases` (ledger hashes)
  in place of `missed_ids`;
- `misclassified`, its cases named by ledger hash (`case`), never by id;
- `models`: per question, the models that answered and how often (each answer's provider
  model, [Decision Pack spec 0.2](decision-pack-0.2.md), §4.2), beside `providers`, the
  configured model per slot. Reports and ledgers made before this was recorded (2026-10-07) by
  an `llm` slot (alone or in a composite) or a `vote` slot name the configured model there
  instead; a report's engine version does not tell them apart.

Interval bounds and probabilities are stored to 6 decimal places, so a result is the same on
every platform. Changing what a figure measures MUST change the format and say which published
results become stale.

## The ledger: `mendari.ledger/1`

JSON Lines in canonical form (sorted keys, no spaces, ASCII, LF line endings, a final newline).
Its id is `sha256:` and the SHA-256 of its bytes. A ledger MUST NOT hold input text or a case id.

Line 1, the header: the pack (id, version, digest); the dataset (name, SHA-256 of the whole
file, size, `limit`); the case-hash scheme (`hmac-sha256-128`, key `dataset/v1`) and the number
of cases; the primary question; each question with its labels in order; the critical labels; the
subgroup attributes; the providers (variant, type and configured model per slot); the policy
(risk level, thresholds, and a governance override as whether it allows AUTO and the SHA-256 of
its reference); and `models`, the answering models a case line refers to by index.

Then one line per case, in the dataset's order:

| Key | Holds |
|---|---|
| `c` | the case hash |
| `o` | the outcome: AUTO, HUMAN_REVIEW, ABSTAIN or BLOCK |
| `r` | the recommendation, or null |
| `q` | per question the case is labelled for or the run answered: `[truth, predicted, confidence, probabilities, model, error]`, probabilities in the header's label order |
| `k` | the indices of the critical labels the case is a positive of |
| `u` | `[cost_usd, input_tokens, output_tokens]` |
| `a` | the declared subgroup attributes only, each at most 64 characters |

The case hash is HMAC-SHA256 over a fixed prefix and the case id as canonical JSON, keyed by a
key derived (HMAC-SHA256) from the dataset file's own bytes, and cut to 128 bits. Whoever holds
the dataset can match a hash to its case; without it, a small dataset's ids cannot be guessed
from the hashes. A dataset with two cases of one id is refused.

`mendari eval --out <dir>/report.json` writes the ledger to `<dir>/ledgers/<hex>.jsonl`; a
server stores it beside the report, and chains an `eval.recorded` event (the report, result and
ledger ids, the pack digest, the dataset's hash and the organization: ids and hashes only).

## How a figure is shown

Every proportion a person reads, on a card, on the command line or on the dashboard, is shown
as k of n with its exact interval: `596 of 600 [98.3-99.8%]`. The interval is worked out from k
and n each time it is shown, never read from a stored (6-place) bound, so every surface prints
the same text. A bound is printed with as many digits as it needs (up to 4) never to read 0 or
100% when it is neither; one closer still reads `<0.0001` or `>99.9999`. A share of no cases
says "no cases"; a share whose count is not known (a rate stored without it) says "n/a". Next to a critical label with fewer than 100 positives (the evidence rule), the
figure says "insufficient evidence". The only other percentages are thresholds and tolerances,
which are settings, not shares of cases.

A pack's known failure modes are its author's words, not computed. The reference packs state no
share of cases as a percentage there: they state the finding and, for a local-site example, the
command that measures it.

## Figures as served: `mendari.figures/1`

A server works out the figures of what it serves, in Python (`mendari.evals.figures`), so a
browser only formats them. A figure is `{k, n, low, high}`: k of n with its unrounded exact
interval (`low` and `high` null when n is 0). A figure is null when what it is of is not known,
or when its counts are not a whole k of a whole n (a rate outside [0, 1] or not a share of its n
cases, a count above 10^9). The card and the command line follow the same rule. Figures are never stored: they are recomputed from the stored report,
leaderboard or registry version on every read, so one published before needs no migration.

| Where | Key | Holds |
|---|---|---|
| `GET /api/packs/{pack}` | `benchmark.figures` | the benchmark report's figures |
| `GET /api/packs/{pack}` | `leaderboard` | the leaderboard as served: `ranking`, `groups` (one per set of records, with `about` and `label`) and each row's `rank`, `standing`, `standing_text`, `group` and `figures` |
| `POST /api/packs/{pack}/evals`, `GET /api/evals` | `figures` | each report's figures (null when a stored report's cannot be read) |
| `GET /api/stats` | `periods` | the last 7 and 30 days and all time (local days), each with the window before it: automated, overridden and agreed shares and each outcome |
| `GET /api/monitoring` | `weeks[].figures`, `last_7_days.figures`, `benchmark.figures` | each week's and the last 7 days' automated (of decisions made: refused inputs left out), override, accuracy, `reviewed_accuracy` (decisions sent to a person whose recommendation the reviewer kept) and `automated_accuracy` (automated decisions with an outcome recorded downstream) shares, and the `provider_errors` (decisions a provider failed, from LP-21d on) and `abstained` shares, over the served version's decisions; the benchmark's accuracy and its `reviewed_accuracy` (one minus its expected override rate) and `automated_accuracy`, which the alerts compare, stratum by stratum, only when `benchmark.comparable` is true; otherwise `benchmark.reason` says why (`from_registry`: a pack pulled from a registry, whose benchmark is never read; `no_benchmark`, `another_build`, `providers_unknown`, `other_providers`, `other_policy`), and a pack from a registry's benchmark figures are null |
| `GET /v1/packs/{pack}`, `GET /v1/packs/{pack}/{version}` | `latest.checked.regression.accuracy`, `checked.regression.accuracy` | the registry's own regression check on the pack's test set, labelled "publisher's own test set: regression check, not a ranking" |
| `GET /v1/packs/{pack}` | `latest.figures` | `benchmark` and `leaderboard`, always null: a registry shows no uploaded figure |
| `GET /v1/packs/{pack}/{version}` | `figures` | the same, always null |
| `GET /v1/search` | each item's `figures` | `benchmark`, always null |

Since LP-18 a registry stores and shows no benchmark or leaderboard a publisher uploads
([ADR-0012](../decisions/ADR-0012-a-registry-that-shows-only-what-it-checked.md)): `figures`
keeps its shape for clients that read it, with null values. Its one figure is the regression
check's, which nothing ranks on.

A report's figures are each over what the card shows it over: accuracy over the labelled
cases, each outcome and automation over all cases, false automation over the labelled cases,
precision over the cases predicted with the label and recall over its support, a subgroup's
accuracy over its labelled members, each calibration bin's correct cases, and each sweep row's
automated cases over the scored ones and correct cases over the automated. A report from
before its counts were stored has each figure it can prove: the sweep's counts from the
calibration bins, a subgroup's accuracy only when every case is labelled. `figures` is a run
key: a served report's `result_sha256` is still its result's hash.

A browser that prints a figure (the dashboard's `ui.js`) ports `render.shown` and is checked
against `tests/fixtures/shown.json` (written and checked by `scripts/shown_fixture.py`): the text
Python prints for 2,401 figures and 601 bounds, including the exact rounding ties where
JavaScript's `toFixed` and Python's formatting disagree. Its bounds' last bits are the
platform's maths library's, so it is checked by what it prints, never byte for byte.

## Statistics: `mendari.evals.stats`

Every interval and test on this page comes from one module, written with Python's standard
library only and checked against scipy and statsmodels (`tests/test_stats.py`). Its bootstrap
resamples come from `random.Random(seed)`, the one part of Python's random module whose
sequence is fixed for a seed, so anyone with the cases and the seed draws the same resamples.

Three functions were added for the scoreboard (LP-17). No format on this page uses them yet, so
no stored result changes:

- **The stratified bootstrap** (`stratified_bootstrap`) draws each case of a resample from its
  own stratum's cases, from the same seeded draws. Every stratum keeps its size, and with one
  stratum it is the plain bootstrap. It is for macro-F1, resampled within each true label, so
  every label keeps its support and every resample's macro-F1 is defined.
- **A weighted share** (`weighted_proportion`) is for cases drawn with unequal probabilities,
  such as a hidden set's sampling weights (N_h / n_h). It gives the Hájek estimate (the hits'
  weight over the total weight), Kish's effective n (the total weight squared over the sum of
  the squared weights) and the Korn-Graubard interval: Clopper-Pearson at the effective counts,
  x hits and effective n - x misses, where x is the estimate times the effective n. The counts
  are used as they are, not rounded, so at no hits and at all hits it is Korn-Graubard's too.
  The beta quantile takes shapes of 1 or more only: where x is under 1 the lower bound is 0,
  and where the misses are under 1 the upper bound is 1, wider than Korn-Graubard's there and
  never narrower. The sums are exact, so with one weight for every case it is the exact
  interval of k of n. A weighted share is shown beside the unweighted k of n, never in its
  place.
- **The Brier score's reliability term** (`brier_reliability`) is Murphy's: over ten
  equal-mass bins of top-label confidence (a case's largest probability), the sum of each
  bin's share of the cases times the squared distance between its mean forecast and how often
  each label was the truth there. Tied confidences are averaged, as the calibration error's
  are, and it is not estimable, as that error is, when a bin would hold fewer than 30 cases.

## Leaderboards: `mendari.leaderboard/2` and `mendari.rank/1`

A leaderboard row carries its pack's version and digest, the dataset's name, hash, size and
`limit`, the report's ledger, and `accuracy_proportion` and `false_automation` as k of n with
their intervals. A row is keyed by pack digest, dataset hash, size and configuration: a new run
of one configuration on the same pack and records replaces its row; a run on another pack
version adds one.

`build` writes the pack's digest and each row's `rank` and `standing`, from one ranking
function, `mendari.rank/1` (`mendari.evals.ranking`). Rows are ranked against the rows on the
same records (dataset hash and size), in this order:

1. fewer critical positives automated;
2. a higher lower bound of the worst critical label's detection, over the critical labels with
   at least 100 positives;
3. a lower upper bound of false automation;
4. a higher lower bound of accuracy;
5. a lower cost per decision, then the configuration's name, so the order is total.

Accuracy never ranks alone. Each bound is worked out from the row's k and n, never read from
the row: a board can come from a registry upload. A row measured on another pack digest has the standing
`earlier version` and no rank: its AUTO and false automation followed that version's policy. When
no critical label on those records has 100 positives, every row has the standing
`insufficient evidence` and none ranks.

## Paired comparisons: `mendari.compare/1`

`mendari compare A B --dataset X` compares two runs of one pack on the same dataset, from their
ledgers, on the cases both ran. For each figure (each critical label's detection, false
automation, primary accuracy, automation) it gives each run's k of n with its 95% interval, the
discordant cases (only A, only B), the exact two-sided McNemar p-value, the p-value adjusted with
Holm over the family the output names, and `different`, Holm's decision at alpha. A figure with
too few discordant cases for the exact test ever to reach alpha (fewer than 6 at 0.05), or a
critical label with fewer than 100 positives, also says "insufficient evidence"; that qualifies
`different` and never overrides it. The primary question's
Brier score is compared with a seeded paired bootstrap (2,000 resamples), outside the Holm family.
The cases that changed are listed by ledger hash (and, on the command line, by id from the
dataset X): critical detections first, then false automation, correctness and automation.

## Coverage: `mendari.coverage/1`

`mendari data coverage PACK --dataset NAME|FILE` says what a labelled set covers, for the pack
whose questions it is labelled for, without running the pack (LP-11). It reads a pack's dataset
or any JSONL file, and refuses one the pack cannot read as `mendari eval` does (by case id, never
case text).

Two cases are the same case when their inputs are, under one of two forms
(`evals/canonical.py`, pinned byte for byte, and the forms the hidden-case index and the pack
store's upload check use):

- **exact**: the input's canonical JSON (sorted keys, no spaces, ASCII escapes);
- **`norm/v1`**: the same, after every string value is folded (Unicode NFKC, case folded, NFKC
  again; invisible format characters such as zero-width spaces removed; each run of characters
  that are not letters, combining marks, digits or `_` made one space, trimmed) and every whole
  number is written as an integer (57.0 is 57). Keys, booleans, null and the nesting are kept,
  so "take 5 mg" and "take 50 mg" stay two cases. Punctuation and symbols are dropped, signs and
  comparison symbols too: "eGFR < 30" and "eGFR > 30" are one case under it.

The report counts duplicates under `norm/v1`, the stricter rule:

| Field | What it holds |
|---|---|
| `cases` | The cases in the set. |
| `distinct` | `{exact, normalised}`: how many distinct inputs under each form. |
| `duplicate_groups`, `conflicting_groups` | Groups of two or more cases with one input, and how many of them give one question different labels. |
| `questions` | Per question: how many cases are labelled, and per label `{cases, distinct}`. |
| `critical` | Per critical label: `cases`, `distinct` (positives once duplicates are removed) and `insufficient`, true under 100 distinct positives. |
| `mix` | The `language`, `source` and `stratum` of the cases, from their `attributes` (cut to 64 characters); a missing value is `unknown`, never inferred. |
| `warnings` | A critical label under 100 distinct positives; duplicates that disagree. |
| `duplicates` | Only with `--ids`: the case ids of each duplicate group, for a file a person holds. |

It names labels, counts and attribute values, never a case's input. The evaluation report
(`mendari.eval/2`) still counts every positive: AI Guard's test set has 164 `verdict=block`
cases, 30 of them distinct.

## Rater agreement: `mendari.agreement/1`

How far raters who label the same cases blind agree, per question of the pack (LP-11,
`evals/rater_agreement.py`). The input is each case's first-pass labels by rater: raters label
the pack's member questions, and a derived question's label for a rater comes from the pack's
decision table over that rater's member labels. An adjudicator's label is not a first pass.

The report holds `format`, `pack`, `level` (0.95), `seeds` (`{bootstrap, resamples}`: the seed,
and 2,000 resamples unless set) and `questions`: for each of the pack's questions, `cases` (the
cases with at least one rating) and these:

| Field | What it holds |
|---|---|
| `percent` | Of the cases two or more raters labelled, those whose labels all agree: k of n with its exact interval, as every share is shown. |
| `kappa` | One entry per pair of raters who labelled a case in common: `raters`, `n` (the cases both labelled), `labels` (each rater's label counts on them), Cohen's kappa as `estimate`, and its `interval`. |
| `alpha` | Krippendorff's alpha (nominal) across the pool, as pairs vary from case to case and ratings may be missing: `cases` and `ratings` counted (cases with two or more ratings), `estimate`, `interval`. |

Kappa and alpha are computed exactly (fractions) and rounded to 6 places. Their intervals are the
percentile bootstrap over seeded resamples of the question's cases, the same index matrix for
every coefficient of the question. A coefficient the cases leave undefined is `null`, with
`status: not_estimable` and its `reason` (kappa: both raters gave every case one and the same
label; alpha: no case has two ratings, or every rating is one label). An interval is
`not_estimable` when any resample leaves its coefficient undefined, and `dropped` says in how
many; otherwise it is `{status: ok, low, high, dropped: 0}`.

## Hidden-set grading: `mendari.hidden_result/1`

What a grading run over a hidden evaluation set answers (LP-11l, `evals/hidden.py`,
[ADR-0011](../decisions/ADR-0011-hidden-evaluation-sets.md)): `POST /api/evalsets/{set}/runs`,
for the set's organization's grading key only, over a frozen or active set. The run decides each
of the set's cases in the server's process with local providers only and no store, and computes
a `mendari.eval/2` result from its ledger in memory; the answer is that result projected onto
this closed schema, and nothing is stored (no decision, report, ledger or usage). A field not
listed here MUST NOT appear: a server MUST refuse to answer a run whose result does not fit it,
and say nothing of it.

| Field | What it holds |
|---|---|
| `format` | `mendari.hidden_result/1`. |
| `set`, `commitment` | The set's id, and the salted commitment chained when it froze (never its content hash). |
| `n`, `run`, `runs_left` | How many cases were graded; this run's number (its chained `evalset.graded`); the runs the rotation rule leaves after it. |
| `pack`, `providers`, `policy` | As a site report holds them (`mendari.site-report/1`): the pack's id, version and digest; each slot's variant, type and model (local only); the risk level, thresholds and whether a governance override applied (never: a run measures the pack's own policy). |
| `seeds` | `{bootstrap, resamples, level}`, as `mendari.eval/2`'s. |
| `primary` | The pack's primary question. |
| `questions` | Per question, as a site report's: `n`, `accuracy`, `macro_f1`, `per_label` (precision, recall, F1, support), `confusion`, `brier`, `calibration` (`ece` and the equal-width bins of 10 cases or more only); and as `mendari.eval/2`'s, `proportion` (accuracy as k of n) and `brier_interval`, null over fewer than 10 scored cases. |
| `decisions` | The outcome counts and rates, the abstention rate, automation and review, recommendation accuracy, and per critical label its `question`, `label`, `n`, `detected`, `detection_rate`, `auto_missed` and `proportion` (detection as k of n). |
| `threshold_sweep` | Per threshold: `threshold`, `coverage`, `accuracy`, `errors`. |
| `errors` | Error codes and how many answers each failed (Mendari's closed set). |

It MUST NOT hold a case's id or hash, a dataset's name or hash, the ledger's id, a critical
label's missed cases, misclassified cases, subgroups, equal-mass calibration, latency or cost. A
calibration bin, and a Brier interval, over fewer than 10 cases is left out, as a site report
leaves out small bins: over a few cases their figures are single cases'. Rates are rounded to 4
places and other figures to 3, as a site report's are. It names the pack's labels beside counts
and rates, never beside a case.

A refused run is answered with a fixed `reason` (`no_set`, `wrong_pack`, `unknown_variant`,
`remote_provider`, `pack_unavailable`, `not_gradable`, `no_runs_left`, `expired`,
`set_changed`, `set_unavailable`, `run_failed`) and one fixed sentence, never a store's or a
run's message: a set whose rows no longer give its commitment is `409` `set_changed`
([the runbook](../operations/evalsets.md)). A pack whose own names or sizes this schema cannot
hold (more than 64 questions, slots or critical labels, a question with more than 64 labels, or
a question, label, slot or variant name that is not 1 to 64 letters, digits, `_`, `.` or `-`
starting with a letter or digit) is refused `pack_unavailable` before the set's cases are read,
so it uses none of the set's runs.
