# Evaluation reports, ledgers and paired comparisons

This page specifies what an evaluation run writes: the report (`mendari.eval/2`), the per-case
ledger its result is computed from (`mendari.ledger/1`), and a paired comparison of two runs
(`mendari.compare/1`). It supersedes the report format of the
[Decision Pack spec 0.2](decision-pack-0.2.md) (`mendari.eval/1`), which still loads. The key
words MUST, SHOULD and MAY are used as in that spec.

The scoreboard and its scoped grades (LP-17) follow the
[grading method 1.0](../grading/method-1.0.md), a draft not in effect: the arms, the figures and
their statistics, the evidence rule, and why, under it, a public set is a regression check, never
a ranking. The scoreboard's own formats are made under it: its arms (`mendari.arms/1`), its
entries (`mendari.scoreboard/1`), the run records and price tables an entry is computed from
(`mendari.run/1`, `mendari.prices/1`), and the grades and band rankings given from entries on
frozen hidden sets only. The other formats on this page do not change with it, and
the leaderboards below are not made under it: until LP-17g relabels them, they still rank rows
on public sets with `mendari.rank/1`, as their section says.

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

Three functions were added for the scoreboard (LP-17). Only its entries
(`mendari.scoreboard/1`) use them, so no other stored result changes:

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

## Arms: `mendari.arms/1`

The scoreboard (LP-17) puts a pack's provider configurations in four arms, by the class of the
models they ask (`evals/arms.py`). `mendari scoreboard arms PACK` lists them from the pack's
manifest alone: nothing is run, called or written (`--json` prints the report).

A configuration is one variant for each of the pack's slots, so a pack's configurations are the
product of its slots' variants. Its arm comes from the provider types it runs, in any slot,
counting a composite's members as its own (one level: composites cannot nest). Jev is the
specialised classifier and an LLM the general model; every other type asks no model.

| Arm | Name | Its configurations |
|---|---|---|
| `deterministic` | Mendari only | Ask no model: rules, criteria, dosing, naive Bayes and CQL. |
| `classifier` | + specialised classifier | Jev in at least one slot, and no LLM. |
| `llm` | + general LLM | An LLM in at least one slot, and no Jev. |
| `classifier_and_llm` | + both | Jev and an LLM, in two slots, or in one through a composite with both. |

The arm says what a configuration asks, not where it runs. Each configuration also says whether
it is local: every slot's variant decides on the machine (`core.origin.is_local`: rules,
criteria, dosing and naive Bayes, or a composite of only those). A configuration that is not
local names its remote types, the types it calls off the machine. CQL is deterministic but
remote: it calls Mendari's own CQL engine (`MENDARI_CQL_URL`), not a model. An arm none of a
pack's configurations reaches is `not applicable`. A pack with more than 10,000 configurations is
refused, never listed.

The report holds `format`, `pack` (its `id`, `version` and `digest`), `configurations` (how many
the pack has) and `arms`: for each arm, in the order above, its `name`, its `status`
(`applicable` or `not applicable`) and its `configurations`. Each configuration has `providers`
(slot to variant, in the manifest's order), `local` and `remote_types` (sorted, and empty when it
is local). The command line writes each configuration as `mendari leaderboard --run` takes it
(`slot=variant,slot=variant`). The report names the pack, its slots and their variants, never a
case.

The five reference packs' arms, from their manifests:

| Pack | Mendari only | + specialised classifier | + general LLM | + both |
|---|---|---|---|---|
| ai-guard | 1 | 2 | 2 | not applicable |
| clinical-trial-prescreener | 2 (one is `structured=cql`) | 4 | 4 | not applicable |
| patient-inbox-router | 2 (`router` is `naive_bayes` or `rules`) | 10 | 7 | 6 |
| prior-auth-preflight | 1 | 2 | 2 | not applicable |
| referral-router | 1 | 8 | 5 | 6 |

No variant in these packs asks both Jev and an LLM, so "+ both" takes two slots, and only the
inbox and referral packs have it.

## Scoreboard entries: `mendari.scoreboard/1`

An entry is one configuration's figures on one set, under the
[grading method 1.0](../grading/method-1.0.md) (`evals/scoreboard.py`, LP-17d). It is a pure
function of five inputs: the ledger (`mendari.ledger/1`), the dataset (by its SHA-256), the run
record (`mendari.run/1`), the price table (`mendari.prices/1`, by its SHA-256) and the method (by
its SHA-256). The same inputs give the same bytes, and every bootstrap is the method's: 2,000
resamples, seed 0, at 95%. Where `mendari.eval/2`'s result already holds a figure, the entry
reuses it. No format on this page changes. Entries are kept in the
[results store](#the-results-store-scoreboard), and the
[early scoreboard](../grading/scoreboard.md) is generated from it.

An entry is refused, with a message that names ids and hashes and never a case, when:

- the ledger is of another pack (its id, version or digest) or another dataset (its name,
  SHA-256, size or `limit`), holds another number of cases than the dataset, or its case hashes
  are not the dataset's cases in the dataset's order;
- the configuration is not the ledger's (slot by slot), or not in the arm named;
- the run record is not the ledger's (see [run records](#run-records-mendarirun1)), or names
  tokens or models of a type the configuration does not call;
- `supersedes` is not `sha256:` and 64 hex digits.

| Field | What it holds |
|---|---|
| `format` | `mendari.scoreboard/1`. |
| `arm`, `configuration` | The arm (`mendari.arms/1`), and the configuration: `providers` (slot to variant), `local` and `remote_types`. |
| `inputs` | The ledger's id, the dataset's SHA-256, the run record's SHA-256 with its replicate and engine version, the price table's SHA-256 and date when the cost is priced from it (else null, so an entry no table priced is the same whichever table is at hand), and the method's SHA-256. |
| `seeds` | `{bootstrap, resamples, level}`: 0, 2,000 and 0.95. |
| `scope` | What the figures are of (below). |
| `ranking`, `grade` | On a public set, `{status: "off", reason: "public set: regression check, not a ranking"}` and null (see [Grades and band ranking](#grades-and-band-ranking)). |
| `figures` | The figures (below). |
| `supersedes` | The earlier result this one supersedes in the results store, `sha256:` and its body's SHA-256, or null. It is part of the body, so its hash and any signature cover it. |
| `body_sha256` | `sha256:` and the SHA-256 of the entry's canonical JSON without `body_sha256` and `signature`. |

The scope holds the pack (`id`, `version`, `digest`) and its `status` line (the synthetic-example
label for a pack built from synthetic data); the `set` (`kind`, `public` for every shipped set;
`name`, `sha256`, `n`, `distinct_inputs` and `norm`, the `norm/v1` rule they are counted under);
the ledger's `providers`; the `models` as reported (`answered`, the ledger's list, and `by_type`,
the run record's); the `method` (`version`, `sha256`, and `in_effect` on the date measured, false
while it is a draft); `measured_on`, the run record's date; `evaluation`, the words "engineering
evaluation, not clinical validation"; and the `disclosure`. A local configuration's disclosure is
"no model provider". A remote one's holds, for each remote type it calls, that type's statement
from the disclosures file, and reads "not published: disclosure pending" while any is missing:
its scope is shown and its figures are not.

| Figure | What it holds |
|---|---|
| `critical` | Per critical label: `detected` and `automated` (its positives decided AUTO), each raw k of n; `distinct`, its distinct `positives` and how many are `detected`; `bound`, the grade's bound; and `evidence`, `sufficient` or `insufficient evidence`, with the minimum it `needed`. |
| `false_automation` | Wrong AUTO over the cases labelled for the primary question, k of n. |
| `auto_error_rate` | Wrong AUTO over the AUTO cases, k of n. |
| `automation`, `accuracy` | k of n, as `mendari.eval/2`'s. |
| `macro_f1` | `{estimate, low, high}`, the bootstrap resampled within each true label. |
| `brier` | `{estimate, low, high}`, `mendari.eval/2`'s mean and interval. |
| `brier_reliability` | Murphy's reliability term with its bootstrap interval, or `not_estimable` with its `reason` (a bin under 30 cases). |
| `ece` | The equal-mass ECE as a `value`, a description with no interval, or `not_estimable`. |
| `cost_per_1000` | `priced`, in USD per 1,000 decisions with its bootstrap interval; `none`, "0 (no metered calls)"; or `unknown`, with its `reason`. |
| `latency_ms` | `p50` and `p95` (Hyndman and Fan's definition 7), each with its bootstrap interval, and the `note` "measured on the runner, not reproducible". |
| `subgroups` | Per declared subgroup and value: its cases, and its accuracy, automation and AUTO error rate as k of n, each `too few` over fewer than 10 cases. |
| `weighted` | `as sampled`, or, when every record carries `attributes.weight`, the Korn-Graubard accuracy, false automation, automation and AUTO error rate (`estimate`, `effective_n`, `low`, `high`), beside the unweighted figures. |

- **Distinct positives:** the dataset's cases are grouped by their input under `norm/v1`. A
  group is a positive of a critical label when any of its cases is, and detected only when every
  positive case in it is.
- **The grade's bound:** the Clopper-Pearson interval of the detected groups over the positive
  groups, at the simultaneous level `1 - 0.05/L` for a pack with L critical labels, stored with
  its `level` and `labels`. It is shown as "lower bound X at Y%, simultaneous across L labels".
- **Evidence:** a critical label has enough when its distinct positives reach
  `method.min_positives(0.95, L)`: 100 for L of 1 to 4, and 107 for the Clinical Trial
  Prescreener's 6. Every critical label needs enough, not just one.
- **Cost** is priced from the run record's tokens, per case and per model type, and the dated
  price table. It is never the ledger's cost, a price a pack sets or one built into a provider.
  It is "0 (no metered calls)" for a configuration that calls no model (CQL included), or whose
  model types made no call on the set (a cascade whose first member was sure of every case). It
  is "unknown" when any provider call failed, or there is no table, or a type that spent tokens
  reported no model (a composite kept none of its answers), more than one (whose tokens a run
  record does not split), or a model that is not in the table. A type that spent no token costs
  nothing, whatever its model.
- **Latency** is read from the run record, never measured again, and never ranked.
- **Weighted figures:** detection is never weighted. A set with weights on some records and not
  others is refused.

### The board

`board(entries, current)` shows entries one table per pack and set, in a listing order (by arm,
then configuration, then date), never an order of any figure. `current` is, per pack, its
digest, its arms' statuses and its sets' SHA-256 (`Current.of(pack)`). It reads each entry as its
canonical JSON holds it (keys sorted), so entries just built and the same entries read back from
a store give the same board. A configuration is written `slot=variant`, its slots in sorted
order. Each table holds:

- its ranking (`bands.ranking`): on a public set, off, "public set: regression check, not a
  ranking"; and the words "engineering evaluation, not clinical validation";
- the insufficient-evidence banner when any entry's critical label is short of evidence, naming
  each such label, its distinct positives and the minimum;
- each arm's status: measured, "not measured", or "not applicable" for an arm the pack cannot
  build; and each configuration of a measured arm that has no entry in the table (`not_measured`,
  "not measured yet"), such as the Clinical Trial Prescreener's `structured=cql`, which is
  deterministic but calls Mendari's CQL engine;
- each entry with its marks, worked out when the board is built and never stored: "unsigned"
  (no signature), "method draft, not in effect", "stale: pack changed", "stale: set changed" or
  "stale: method changed" (its digest, set SHA-256 or method SHA-256 is not the current one),
  "stale: another model reported" (a later run of the same configuration of the pack reports
  another model of a type), and "stale after DATE", 45 days after it was measured. No clock is
  read, so the date is shown and never checked;
- each entry's grade (`grade.grade`): null on a public set.

A board holds no ledger, so it never ranks a hidden set's arms: a hidden table of one entry
reads "off: one arm graded", and one of two or more is refused.

`render.scoreboard_markdown(board)` writes it in Markdown: per table its standing, the banner, the
arms, then one column per entry with its marks, its scope and its figures. A proportion is k of n
with its exact 95% interval (as [How a figure is shown](#how-a-figure-is-shown) says), a mean is
its value with its bootstrap interval, and ECE is "a description: no interval". It has no Rank
column, no bare percentage, and names no case.

### Grades and band ranking

A grade and a ranking are given only on a frozen hidden set, under the
[grading method 1.0](../grading/method-1.0.md) (sections 6 and 7; `evals/grade.py` and
`evals/bands.py`, LP-17j). On a public set the grade is null and the ranking is off. Neither is a
stored format: the hidden-set scoreboard (LP-17k) computes both in memory, in the grading read
that decides every declared arm. While the method is not in effect, every attestation and every
ranking is marked draft.

`grade.grade(entry)` is the entry's attestations, each `met`, `not met` or
`insufficient evidence`, with the method it is made under. There is no letter, no score and no
composite.

| Attestation | Met when | Insufficient evidence when |
|---|---|---|
| `no_critical_automated` | no positive of any critical label was decided AUTO | a critical label has fewer than 100 distinct positives (a positive decided AUTO makes it `not met` whatever the evidence) |
| `recall_floor`, one per critical label | the grade's bound, the lower bound at `1 - 0.05/L` over the distinct groups, is at or above 0.95 | the label has fewer distinct positives than `method.min_positives(0.95, L)` |
| `false_automation_ceiling` | false automation's 95% upper bound is at or below 0.02 | fewer than 183 cases are labelled for the primary question (`method.min_labelled(0.02)`) |

Insufficient evidence means that even a perfect result on these cases could not meet the
attestation. Each attestation holds the entry's figures it was decided on, each k of n with its
interval (the grade's bound at its own level), and the minimum it needed. Every decision is
worked out from k and n, never from a stored (rounded) bound, so a bound just under a floor never
rounds up to meet it. An entry made under
another method's SHA-256, or whose grade's bound is not at the method's level, is refused.

`bands.ranking(entries, ledgers=..., records=...)` ranks the declared arms graded on one set,
one configuration per arm. Their ledgers must pair: the same cases in the same order (one
grading read, so one case key), with the same positives. The records give the distinct groups,
and must give each entry's distinct counts. It is off on a public set, and off with one arm
graded. Otherwise it holds:

- `ranked`: each arm whose safety attestations (`no_critical_automated` and each
  `recall_floor`) are all met, with its `rank`, its rank `interval`, its `bands` and its `key`;
- `unranked`: each other arm, with each safety attestation it did not meet and that
  attestation's state;
- `family`: the Holm family, what it is `of`, its `alpha`, its `size`, its seeds and its
  `tests`;
- `method` and `draft`.

The band tuple, in this order, each element a band position, 0 the best:

1. the worst critical label's grade's bound, banded at 0.99, 0.98, 0.97, 0.95 and 0.90;
2. false automation's 95% upper bound, banded at 0.005, 0.01, 0.02 and 0.05;
3. accuracy's 95% lower bound, in bands of 0.05;
4. Brier's 95% upper bound, in bands of 0.05;
5. cost per 1,000 decisions, its estimate, banded at 0, 1, 10 and 100 USD (0 with no metered
   call, and an unknown cost in the last band).

A value's band is the first edge it reaches, or one past the last edge. In bands of 0.05 it is
the largest multiple at or below the value (accuracy) or the smallest at or above it (Brier). A
value is read as the decimal it is written as, so 0.95 is in the band of 0.95, never the one
below. A figure that is not estimable sits after every band. ECE, latency and subgroups never
enter the tuple. Arms are ordered on their tuples, lexicographically. Equal tuples share a
competition rank (1, 2, 2, 4) and are listed by configuration.

The family is every pair of ranked arms, by each critical label's detection (exact McNemar on
that label's distinct positive groups), false automation and accuracy (exact McNemar on the
cases labelled for the primary question) and Brier (the paired bootstrap's p-value, on the cases
both arms scored), adjusted with Holm at 0.05. With R ranked arms and L critical labels it holds
R(R - 1)/2 times (L + 3) tests. Each test names its pair, its criterion (and label), its p-value,
its adjusted p-value, whether it is rejected and which arm it favours. `mendari.compare/1` keeps
its own family.

Arm j is significantly ahead of arm i when j's tuple is the better one and the family's test on
the first criterion where the two differ rejects, in j's favour. On detection it is the test on
the label that sets the lower arm's band. Cost has no test, so arms apart first on cost are never
significantly apart. An arm's rank interval runs from 1 plus the arms significantly ahead of it
to the ranked arms less the arms significantly behind it. The tests set the intervals and never
reorder the ranks, so every interval holds its arm's rank. Nothing is resampled for them. A
re-run from the same ledgers and seeds gives the same bytes.

## Run records: `mendari.run/1`

A run record holds what a ledger does not, and what no rebuild reproduces (`evals/runs.py`). It
is a closed schema: anything else in it is refused. It never holds a key, an endpoint or case
text. Its bytes are its canonical JSON (sorted keys, no spaces, no NaN) and a newline, and its
SHA-256 is of those bytes: a record whose bytes are not canonical is refused when read. A
refusal names the field that fails and never a value: a key the input chose (a model id, a type,
an extra field) is named `<key>` unless it is one of the schema's own words.

| Field | What it holds |
|---|---|
| `format` | `mendari.run/1`. |
| `replicate` | The replicate's number, 1 to 5. |
| `engine` | The engine's version. |
| `date` | The day it ran (YYYY-MM-DD, US Eastern). |
| `ledger` | The id of the ledger it is the run record of. |
| `models` | Per model type (`jev`, `llm`), the model ids its answers reported, sorted. |
| `cases` | Per case, in the ledger's order: `latency_ms` (3 places) and `tokens`, per model type `[input, output]`. |

A model id is as its provider reports it, never a URL. `runs.record` builds one from a run's
results: each result's latency and its model types' tokens, and the models its answers name. A
composite's answer names its member and the member's model, and a member of a model type counts
as that type. A model whose answers a composite never kept is not reported, so a cost that needs
it is unknown, never guessed.

`runs.agrees(run, ledger)` holds when the record names the ledger's id and has its number of
cases, and:

- **its tokens:** each case's tokens, summed over its types, are the ledger's `u` (input and
  output tokens). On a case where a provider call failed they may be more, never fewer: a call
  whose answer was refused still spent its tokens, and `u` holds only the answers'. That case's
  cost is unknown either way.
- **its models:** every model it names is one the ledger's answers name (an answer's model, or a
  composite member's in the answer's `member:model`), and every model the ledger's answers name
  is in it, unless it is a deterministic provider's: its type, `@` and the fingerprint of its
  pack file (`rules@…`, `cql:<library>@…`). Jev's and the LLM's model ids never hold an `@`.

An entry is never made from a record that does not agree, so its tokens are never priced as
another model's.

## Price tables and disclosures: `mendari.prices/1` and `mendari.disclosures/1`

A price table is a dated file outside Mendari's code (`scoreboard/prices/<YYYY-MM-DD>.json`),
cited by the SHA-256 of its bytes. A table read from the package, the repository's `src/` or a
pack's directory is refused before it is read.

| Field | What it holds |
|---|---|
| `format` | `mendari.prices/1`. |
| `date`, `source` | When the prices were read, and where from. |
| `prices` | Per model type and model id: `input_per_1000` and `output_per_1000`, USD per 1,000 tokens. |

The disclosures file (`scoreboard/disclosures.json`) holds `format` (`mendari.disclosures/1`), its
`date`, and `statements`: per remote provider type (`jev`, `llm`, `cql`), the statement of any
commercial relationship between Mendari and that provider. Both are closed schemas, and a
refusal of either names a key the input chose as `<key>`, as a run record's does.

## The results store: `scoreboard/`

Scoreboard results are kept in the repository, under `scoreboard/` at its root
(`evals/results_store.py`, LP-17f; the [grading method](../grading/method-1.0.md), section 11).
It is outside the wheel, the sdist and the site: a pack's own `evals/` is in every archive, where
a publisher's pack could carry a forged result. Each file is named by its content:

| Path | What it holds |
|---|---|
| `results/<sha256>.json` | A result, named by its body's SHA-256 (`body_sha256` without `sha256:`), sorted and indented, with one newline at the end. |
| `ledgers/<sha256>.jsonl` | A ledger (`mendari.ledger/1`), named by its id. |
| `runs/<sha256>.json` | A run record (`mendari.run/1`), named by the SHA-256 of its bytes. |
| `prices/<YYYY-MM-DD>.json` | A dated price table (`mendari.prices/1`), cited by its SHA-256. |
| `keys.json` | The public grading keys (`mendari.grading-keys/1`). |
| `disclosures.json` | The disclosures (`mendari.disclosures/1`): none yet, so every remote type's statement is pending. |

- **Append-only.** Nothing stored changes, except that a valid signature may be added to an
  unsigned result whose body stays the same. `mendari scoreboard add FILE [--supersedes HASH]`
  never writes over a file.
- **Supersedes.** A result may name the earlier result it supersedes: one of the same pack, set,
  arm and configuration, measured no later, which no other result supersedes. Both stay. The
  later is the current one, and one pack, set, arm and configuration has one current result at
  most.
- **Running.** `mendari scoreboard run PACK [--dataset NAME ...] [--config slot=variant ... |
  --every-local] [--again]` runs a configuration of a pack Mendari maintains over its public
  scoring sets (every set it declares but `train` and `calibration`), and stores the ledger, the
  run record (each case's latency and tokens) and the entry, unsigned. A result that is still
  current (the pack's digest, the set's SHA-256 and the method's unchanged, and not past its
  stale date) is kept unless `--again`; a new one supersedes it. It refuses a remote
  configuration, a fitting set and anything but a set the pack names, so a file or a hidden set
  is never run. The pack is read from the repository's `packs/` (the bundled packs), never from
  `MENDARI_PACKS_PATH` or the registry cache. The check and the page read it from there too.
- **The check.** `scripts/check_scoreboard.py --base REF`, a step of the required
  `reference-pack` job, names each file whose name is not its content's, each result of the base
  removed or changed (but for a valid signature added), each `supersedes` link that breaks the
  rules above, and each current result whose signature does not verify against `keys.json`, or
  was made with a publisher's or a registry's key this machine pinned (CI pins none). It
  recomputes every public result from its stored ledger, its dataset (SHA-256 checked), its run
  record, its price table and the method, byte for byte, and re-runs every local ledger on its
  pack's current digest. A result of an older digest is recomputed and kept, never re-run; one
  whose set or method changed is kept as it is, and shown stale. A new result must be of a local
  configuration (a remote one's ledger could not be re-run without a call off the machine), on
  its pack's current digest, set and method, and measured no later than today. A remote
  configuration's disclosure is recomputed from `disclosures.json` while its result is new, never
  taken from the result; once stored, it keeps the statements it holds. A superseded result
  keeps the signature it had, so a key taken out of the keys file leaves what it signed as
  history.
- **The page.** `scripts/scoreboard_page.py` writes
  [the early scoreboard](../grading/scoreboard.md) from the current results, through `board`
  and `render.scoreboard_markdown`, and lists each superseded result; `--check` (also in
  `reference-pack`, before `scripts/build-reference-pack.sh` writes the page again) fails when
  the page is not the store's. Its bytes depend only on the repository: the store and `packs/`.

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
