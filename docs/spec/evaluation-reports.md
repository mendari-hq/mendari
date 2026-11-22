# Evaluation reports, ledgers and paired comparisons

This page specifies what an evaluation run writes: the report (`mendari.eval/2`), the per-case
ledger its result is computed from (`mendari.ledger/1`), and a paired comparison of two runs
(`mendari.compare/1`). It supersedes the report format of the
[agent spec 0.2](agent-manifest-0.2.md) (`mendari.eval/1`), which still loads. The key
words MUST, SHOULD and MAY are used as in that spec.

The scoreboard and its scoped grades (LP-17) follow the
[grading method 1.0](../grading/method-1.0.md), a draft not in effect: the arms, the figures and
their statistics, the evidence rule, and why, under it, a public set is a regression check, never
a ranking. The scoreboard's own formats are made under it: its arms (`mendari.arms/1`), its
entries (`mendari.scoreboard/1`), the run records and price tables an entry is computed from
(`mendari.run/1`, `mendari.prices/1`), and the grades and band rankings given from entries on
frozen hidden sets only. The other formats on this page do not change with it, and
the leaderboards below are not made under it: every set they are measured on is public, so each
of their tables is shown as a regression check, not a ranking, as their section says.

## The report: `mendari.eval/2`

A report has two parts.

- **The result:** every key except `run`, `comparison`, `result_sha256` and `figures` (what a
  server serves beside a report, never stored). It MUST be computed
  only from the run's ledger and the seeds it names, so two runs on the same cases with the same
  agent, providers and seeds give byte-identical results (canonical JSON: sorted keys, no spaces),
  and anyone holding the ledger can recompute it.
- **The run:** `run` holds what differs between two runs on the same cases: its `id`,
  `created_at`, `latency_ms` (mean, p50, p95, max), the engine version, and a governance
  override's approver (kept locally, never in the result). `result_sha256` is the SHA-256 of the
  canonical result.

`comparison`, outside the result, is the local validation against the agent's benchmark. For a
agent pulled from a registry it is never made with the benchmark the agent carries, which is its
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
  model, [agent spec 0.2](agent-manifest-0.2.md), §4.2), beside `providers`, the
  configured model per slot. Reports and ledgers made before this was recorded (2026-10-07) by
  an `llm` slot (alone or in a composite) or a `vote` slot name the configured model there
  instead; a report's engine version does not tell them apart.

Interval bounds and probabilities are stored to 6 decimal places, so a result is the same on
every platform. Changing what a figure measures MUST change the format and say which published
results become stale.

## The ledger: `mendari.ledger/1`

JSON Lines in canonical form (sorted keys, no spaces, ASCII, LF line endings, a final newline).
Its id is `sha256:` and the SHA-256 of its bytes. A ledger MUST NOT hold input text or a case id.

Line 1, the header: the agent (id, version, digest); the dataset (name, SHA-256 of the whole
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
ledger ids, the agent digest, the dataset's hash and the organization: ids and hashes only).

## How a figure is shown

Every proportion a person reads, on a card, on the command line or on the dashboard, is shown
as k of n with its exact interval: `596 of 600 [98.3-99.8%]`. The interval is worked out from k
and n each time it is shown, never read from a stored (6-place) bound, so every surface prints
the same text. A bound is printed with as many digits as it needs (up to 4) never to read 0 or
100% when it is neither; one closer still reads `<0.0001` or `>99.9999`. A share of no cases
says "no cases"; a share whose count is not known (a rate stored without it) says "n/a". Next to a critical label with fewer than 100 positives (the evidence rule), the
figure says "insufficient evidence". The only other percentages are thresholds and tolerances,
which are settings, not shares of cases.

An agent's known failure modes are its author's words, not computed. The bundled demo agent
states no share of cases as a percentage there: it states the finding.

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
| `GET /api/monitoring` | `weeks[].figures`, `last_7_days.figures`, `benchmark.figures` | each week's and the last 7 days' automated (of decisions made: refused inputs left out), override, accuracy, `reviewed_accuracy` (decisions sent to a person whose recommendation the reviewer kept) and `automated_accuracy` (automated decisions with an outcome recorded downstream) shares, and the `provider_errors` (decisions a provider failed, from LP-21d on) and `abstained` shares, over the served version's decisions; the benchmark's accuracy and its `reviewed_accuracy` (one minus its expected override rate) and `automated_accuracy`, which the alerts compare, stratum by stratum, only when `benchmark.comparable` is true; otherwise `benchmark.reason` says why (`from_registry`: an agent pulled from a registry, whose benchmark is never read; `no_benchmark`, `another_build`, `providers_unknown`, `other_providers`, `other_policy`), and an agent from a registry's benchmark figures are null |
| `GET /v1/packs/{pack}`, `GET /v1/packs/{pack}/{version}` | `latest.checked.regression.accuracy`, `checked.regression.accuracy` | the registry's own regression check on the agent's test set, labelled "publisher's own test set: regression check, not a ranking" |
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

## Shared site reports: `mendari.site-report/2`

What a site sends a registry about its own evaluation (LP-22c,
[ADR-0017](../decisions/ADR-0017-a-site-network-of-signed-counts.md)): whole counts, names and
flags only. No rate, no fraction and no case leaves the site; the registry computes every figure
it shows from the counts (`mendari.evals.figures.site`). It is counted from the run's ledger
(`mendari.evals.share.counted_report`), never as a rate times n, and every count passes one
suppression function (`suppress`) that the sender runs before writing or sending and the
registry runs again, refusing a payload it would change. A registry refuses
`mendari.site-report/1`, which carried rates, and names this format in its answer.

It holds the agent (id, version, digest), each slot's provider (variant, type, model), the
policy (risk level, thresholds as whole basis points: 0.85 is 8500, and whether a governance
override applied), whether the site consented to a remote arm, the dataset's size, the primary
question, per question its labelled cases (`n`), how many were right (`right`), its confusion
matrix over answered cases (truth by predicted label) and its ten confidence deciles (how many
right, how many wrong), the outcome counts, how many cases were automated with a wrong
recommendation (`false_automation`), each critical label's `n`, `detected` and `auto_missed`,
and the tokens used.

What stays with the site: calibration error and Brier scores (they need each case's
confidence), latency and dollar cost (measurements, not counts), per-label precision and
recall (the registry works them out where it shows them), error counts (with every case
labelled they would say how many confusion cells are missing, so a suppressed cell could be
worked out), the threshold sweep's finer rows (0.85, 0.95, 0.97, 0.99: their bands would cut
a suppressed decile by subtraction; the registry works out the sweep at 0.5 to 0.9 from the
deciles), and subgroups (their values come from a site's data, until an agent declares them).

Each field's rule (`share.SUPPRESSION_RULES`; a test holds every field to one):

| Field | Rule |
|---|---|
| `format` | as it is |
| `pack` | as it is: the published version's id, version and digest |
| `providers` | as it is: names the agent declares |
| `policy` | as it is |
| `remote_consent` | as it is |
| `dataset.n` | total: shared exactly; a run on fewer than 100 cases is not shared at all |
| `primary` | as it is |
| `questions.n` | total: shared exactly |
| `questions.right` | total: shared exactly |
| `questions.confusion` | cell: null when 1 to 9, and complementary suppression; a critical label's own cell is its detected count, so it is shown |
| `questions.calibration.bin` | as it is |
| `questions.calibration.right` | cell: null when 1 to 9, and complementary suppression |
| `questions.calibration.wrong` | cell: null when 1 to 9, and complementary suppression |
| `decisions.outcomes` | cell: null when 1 to 9, and complementary suppression |
| `decisions.false_automation` | safety count: shared exactly, never suppressed |
| `decisions.critical.question` | as it is |
| `decisions.critical.label` | as it is |
| `decisions.critical.n` | safety count: shared exactly, never suppressed |
| `decisions.critical.detected` | safety count: shared exactly, never suppressed |
| `decisions.critical.auto_missed` | safety count: shared exactly, never suppressed |
| `tokens.input` | total: shared exactly |
| `tokens.output` | total: shared exactly |

A cell is a count of cases that describes people; zeros are shown (owner decision 3 of the
plan: cells of 1 to 9, and no sharing under 100 cases). Safety counts are never suppressed: a
missed critical case must not disappear under a privacy rule.

**Complementary suppression.** The payload states equations about its cells: per question, the
confusion's diagonal adds up to `right`, and so do the deciles' `right`; its off-diagonal cells
and the deciles' `wrong` count the same cases; the outcomes add up to the dataset's n. No cell is
in two equations. A null cell is also bounded: no count is negative, and a critical label's
row holds at most its `n` cases. While some null can take only one value, given its equation,
the others' bounds and that no count is negative, the smallest shown cell of its equation (a
zero first; never one a safety count states) is made null too. A null whose equation has no
shown cell left to suppress is known from the others whatever is done, so it is shown. Applied
to its own output the function changes nothing, which is what the registry checks.
## Pooled site evidence: `mendari.evals.pooling`

LP-22 ([ADR-0017](../decisions/ADR-0017-a-site-network-of-signed-counts.md)) puts several
sites' results on one agent side by side. Each site gives a whole k of n (cases right, detected
or automated); every function here takes the sites as `(k, n)` pairs and refuses a rate, a
fraction or anything that is not a whole k of a whole n of at least one case, as a figure does.
Each answer names its method, and reads `insufficient evidence` (with the number of sites it
needs) when it has too few sites. Derived numbers are rounded to six places, as stored bounds
are, so an answer is the same bytes on every platform.

| Function | What it is called | What it computes | Needs |
|---|---|---|---|
| `pooled_fixed` | pooled (fixed effect, summed counts) | summed k of summed n, with its exact Clopper-Pearson interval | 2 sites |
| `random_effects` | random effects (DerSimonian-Laird, logit scale) | see below | 3 sites |
| `range_of` | range | the lowest and the highest site rate, each as its k of n with its interval (rates compared exactly; of two equal, the first site) | 2 sites |
| `homogeneity` | Pearson chi-square, exact, or permutation (seeded) | whether the sites differ by more than chance, at 0.05 | 2 sites |

- **The pooled figure** weighs each case once, so a large site counts for more than a small
  one. Its name says so wherever it is shown.
- **Random effects** (DerSimonian and Laird 1986). Site i gives the log odds
  y_i = log(k / (n - k)) and its variance v_i = 1/k + 1/(n - k); a site whose k is 0 or n has
  0.5 added to both cells first. With weights w_i = 1/v_i, Cochran's Q is the weighted sum of
  squares of y_i about the weighted mean; the between-site variance is
  tau² = max(0, (Q - (K - 1)) / (Σw - Σw² / Σw)); the estimate is the mean of y weighted by
  1/(v_i + tau²), its 95% interval that mean ± 1.959964 standard errors (1 over the root of
  the summed weights), each turned back into a proportion. I² = max(0, (Q - (K - 1)) / Q) is
  given beside it. Under 3 sites tau² cannot be told from noise, so it reads insufficient
  evidence (owner decision 4 of the plan).
- **Homogeneity** looks at the sites by (hit, miss) table and its expected counts
  (n_i times the column's total over N):
  - when every expected count is 5 or more, Pearson's chi-square on K - 1 degrees of freedom,
    its p-value from the regularized upper incomplete gamma function (`stats.gamma_q`,
    `stats.chi2_sf`), added to the statistics module for this;
  - otherwise never chi-square, which is wrong on small expected counts (the usual case for a
    critical label): the exact test (Freeman-Halton; Fisher's for two sites), summing the
    hypergeometric probability of every table with the same margins that is no more likely
    than the observed one (ties within a relative 10⁻⁷, as scipy's), when there are at most
    1,000,000 such tables;
  - past that, the same test estimated from 10,000 seeded resamples, each dealing the smaller
    column's cases among all N at random (Floyd's sampling from `random.Random(seed).random()`),
    p = (1 + as extreme) / (1 + resamples). The seed is the first 8 bytes of the SHA-256 of the
    counts' canonical JSON and is written in the answer, so anyone can draw the same resamples.

  `differ` is true when p is under 0.05: the sites disagree by more than chance, so a pooled
  figure is not a summary of them. A table with an empty column (every site all hits, or all
  misses) has one table only, and p is 1.

Every function is checked in `tests/test_pooling.py`: the pooled interval and the
random-effects estimate against hand-computed references and statsmodels (whose DerSimonian-Laird
does not truncate tau² at 0, so where it is negative its fixed-effect row is the reference),
chi-square and the incomplete gamma against scipy, the exact test against scipy's multivariate
hypergeometric probabilities, and the permutation test for its seed and against the exact one.

## Site evidence: `mendari.site-evidence/1`

What sites' signed reports say of one agent (LP-22g), served by a registry at
`GET /v1/packs/{namespace}/{name}/site-evidence` and worked out on every read from the reports
it verified (`hub/site_evidence.py`); nothing in it is a rate a site sent.

- **Groups.** One per agent digest, configuration and remote consent, never across them: two
  digests are two agents as far as evidence goes, and a remote configuration is never pooled
  with a local one. Each group gives its version, digest, configuration and consent; the
  reports in the pool (`sites`, with the total of their cases); the reports left out with the
  reason (`left_out`: the publisher's own site, whose organization holds the agent's
  namespace; a report signed with a key later revoked as compromised; a report superseded by a
  correction); and, beside the figures, how many of the group's tickets are unreported (open,
  or expired with no report), how many were withdrawn, and how many reports were superseded.
- **Figures** (owner decision 8 of the plan): the primary question's accuracy
  (`accuracy:QUESTION`, right of n), each critical label's detection
  (`detected:QUESTION=LABEL`) and its cases automated (`automated:QUESTION=LABEL`), and
  automation with a wrong recommendation (`false_automation`, of the primary question's n).
  Each lists every pooled site's k of n with its exact interval first (a critical figure on
  fewer than 100 cases is marked `insufficient_evidence`), then the
  [pooled statistics](#pooled-site-evidence-mendarievalspooling): `pooled` (fixed effect,
  summed counts), `random_effects` (DerSimonian-Laird, from 3 sites), `range`, `homogeneity`
  (its method, p and, for a permutation test, its seed), the number of sites and cases pooled,
  and `warning`: a sentence when the sites differ by more than chance at 0.05.
- **Tests run.** Each figure is tested alone at 0.05 (owner decision 11's default); each group
  says how many tests were run (`tests_run`), so a reader can weigh one warning among many.
- **Bytes.** The same reports give the same answer: the permutation test's seed comes from the
  counts, and the pooling of a set of counts is cached, so a read does no resampling twice.
- **Never in a rank.** Site evidence is shown apart (`note`): sites choose their own cases, and
  a ticket binds a dataset before its run, not how its cases were chosen.

## Publisher evaluations: `mendari.publisher-evaluation/1`

A publisher's signed evaluation of one listed version on one of its own public datasets, in one
configuration ([ADR-0020](../decisions/ADR-0020-publisher-evaluations.md), `hub/evaluations.py`),
sent by `mendari eval AGENT --dataset NAME --publish --key publisher.key`:

```json
{"format": "mendari.publisher-evaluation/1",
 "pack": {"id": "mendari/portal-message-agent", "version": "0.1.1", "digest": "sha256:…"},
 "configuration": {"reader": "rules"},
 "dataset": {"name": "test", "sha256": "<64 hex>", "n": 600},
 "report": {"format": "mendari.site-report/2", "…": "…"},
 "claimed": {"latency_ms_p50": 0.05, "latency_ms_p95": 0.06, "cost_usd_per_decision": 0.0},
 "models": ["rules@33463eddba9c"],
 "created_at": "2026-11-22T12:00:00Z",
 "key_id": "ed25519:<16 hex>",
 "signature": "<base64>"}
```

- **Closed.** No other field is taken; every name is one the agent defines.
- **`configuration`** names a variant for every provider slot. **`dataset`** is a dataset the
  manifest declares (never a path), its file's SHA-256 and its number of cases (at least 100).
- **`report`** is the run's [`mendari.site-report/2`](#shared-site-reports-mendarisite-report2)
  counts (`counted_report`): whole, with no cell suppressed, as a public set holds no patient.
  `remote_consent` is whether the configuration calls a provider off the machine (a type that is
  not `rules`, `criteria`, `dosing` or `naive_bayes`, or a composite with one).
  `governance_override` is false: an evaluation measures the agent's own policy.
- **`claimed`** (the run's latency p50 and p95 in milliseconds, its cost per decision in USD)
  and **`models`** (the models that answered, as the ledger names them) are the publisher's own
  word, shown labelled so; they are not counts.
- **Signature.** Ed25519 with one of the namespace's publisher keys over
  `mendari-publisher-evaluation-v1\0`, the format, `\0`, then the canonical JSON of every other
  field (sorted keys, no spaces, ASCII). The evaluation's id is the SHA-256 of the canonical JSON
  of the whole record.

The registry serves the live evaluations of a version as `mendari.publisher-evaluations/1`
(`GET /v1/packs/{namespace}/{name}/{version}/evaluations`): each with `rerun` (`matched`: the
registry ran the configuration again and counted the same; `calls_a_model`: not re-run),
`excluded` (`"key revoked"`, or null) and `figures`, worked out from the counts: per critical
label `detected` and `automated` (k of n, with `automated_misses` the count) and
`insufficient_evidence` under 100 positives, then `accuracy` (the primary question's right of
n), `false_automation` (of the primary question's n) and each outcome's count and share. Every
share is k of n with its exact 95% interval; none is a rate the publisher sent.

## Leaderboards: `mendari.leaderboard/2` and `mendari.rank/1`

A leaderboard row carries its agent's version and digest, the dataset's name, hash, size and
`limit`, the report's ledger, and `accuracy_proportion` and `false_automation` as k of n with
their intervals. A row is keyed by agent digest, dataset hash, size and configuration: a new run
of one configuration on the same agent and records replaces its row; a run on another agent
version adds one.

`build` writes the agent's digest and each row's `rank` and `standing`, from one ranking
function, `mendari.rank/1` (`mendari.evals.ranking`). Rows are ranked against the rows on the
same records (dataset hash and size), in this order:

1. fewer critical positives automated;
2. a higher lower bound of the worst critical label's detection, over the critical labels with
   at least 100 positives;
3. a lower upper bound of false automation;
4. a higher lower bound of accuracy;
5. a lower cost per decision, then the configuration's name, so the order is total.

Accuracy never ranks alone. Each bound is worked out from the row's k and n, never read from
the row: a board can come from a registry upload. A row measured on another agent digest has the standing
`earlier version` and no rank: its AUTO and false automation followed that version's policy. When
no critical label on those records has 100 positives, every row has the standing
`insufficient evidence` and none ranks.

**How a leaderboard is shown (since LP-17g, a display change).** The agent's own sets are
public: they are in the repository and in the package, so a configuration can be tuned to
them. Under the [grading method](../grading/method-1.0.md) a public set is a regression check,
never a ranking, so no surface prints a rank (a board can also hold a site's own file,
`--dataset PATH`, and nothing on it is graded either):

- The Decision Card's "Provider comparison" opens with "Public set: regression check, not a
  ranking", says that nothing there is graded and the order its rows are listed in, and links
  the scoreboard. Its tables have no Rank column. A row measured on
  an earlier version of the agent says so beside its configuration.
- `mendari leaderboard` prints the same opening and the same tables.
- The dashboard's Provider comparison says the same and links the scoreboard. It has no rank
  column, and its cost chart names the deployed configuration and every configuration no other
  beats on both cost and accuracy.

The rows are listed in `ranking.grouped`'s order, a listing order: those that rank as data
(measured on this version of the agent, on records where a critical label has at least 100
positives) first, in `mendari.rank/1`'s order, the critical cases caught first; then every
other row, by configuration name. A table of rows all measured on earlier versions is therefore in order of configuration name. The data and the API do not
change: each row keeps its `rank` and `standing`, and `GET /api/packs/{pack}` serves them as
before.

## Arms: `mendari.arms/1`

The scoreboard (LP-17) puts an agent's provider configurations in four arms, by the class of the
models they ask (`evals/arms.py`). `mendari scoreboard arms PACK` lists them from the agent's
manifest alone: nothing is run, called or written (`--json` prints the report).

A configuration is one variant for each of the agent's slots, so an agent's configurations are the
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
agent's configurations reaches is `not applicable`. An agent with more than 10,000 configurations is
refused, never listed.

The report holds `format`, `pack` (its `id`, `version` and `digest`), `configurations` (how many
the agent has) and `arms`: for each arm, in the order above, its `name`, its `status`
(`applicable` or `not applicable`) and its `configurations`. Each configuration has `providers`
(slot to variant, in the manifest's order), `local` and `remote_types` (sorted, and empty when it
is local). The command line writes each configuration as `mendari leaderboard --run` takes it
(`slot=variant,slot=variant`). The report names the agent, its slots and their variants, never a
case.

The bundled demo agent's arms, from its manifest (`mendari scoreboard arms
agents/portal-message-agent`):

| Agent | Mendari only | + specialised classifier | + general LLM | + both |
|---|---|---|---|---|
| portal-message-agent | 1 (`reader=rules`) | not applicable | 1 (`reader=agent`, remote: `agent`) | not applicable |

An `agent` variant is classed with the general model (`MODEL_CLASSES` in `evals/arms.py`).

## Scoreboard entries: `mendari.scoreboard/1`

An entry is one configuration's figures on one set, under the
[grading method 1.0](../grading/method-1.0.md) (`evals/scoreboard.py`, LP-17d). It is a pure
function of five inputs: the ledger (`mendari.ledger/1`), the dataset (by its SHA-256), the run
record (`mendari.run/1`), the price table (`mendari.prices/1`, by its SHA-256) and the method (by
its SHA-256). The same inputs give the same bytes, and every bootstrap is the method's: 2,000
resamples, seed 0, at 95%. Where `mendari.eval/2`'s result already holds a figure, the entry
reuses it. No format on this page changes. Entries are kept in the
[results store](#the-results-store-scoreboard), and the
early scoreboard is generated from it.

An entry is refused, with a message that names ids and hashes and never a case, when:

- the ledger is of another agent (its id, version or digest) or another dataset (its name,
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
| `inputs` | The ledger's id, the dataset's SHA-256, the run record's SHA-256 with its replicate and engine version, the price table's SHA-256 and date when the cost is priced from it (else null, so an entry no table priced is the same whichever table is at hand), and the method's SHA-256. An entry of replicates (below) also holds `replicates`, each replicate's `{replicate, ledger, run}` (its number, its ledger's id and its run record's SHA-256), replicate 1 first; and `declared`, the SHA-256 of the declaration that names its configuration. Neither is written for an entry of one replicate. |
| `seeds` | `{bootstrap, resamples, level}`: 0, 2,000 and 0.95. |
| `scope` | What the figures are of (below). |
| `ranking`, `grade` | On a public set, `{status: "off", reason: "public set: regression check, not a ranking"}` and null (see [Grades and band ranking](#grades-and-band-ranking)). |
| `figures` | The figures (below). |
| `supersedes` | The earlier result this one supersedes in the results store, `sha256:` and its body's SHA-256, or null. It is part of the body, so its hash and any signature cover it. |
| `body_sha256` | `sha256:` and the SHA-256 of the entry's canonical JSON without `body_sha256` and `signature`. |

The scope holds the agent (`id`, `version`, `digest`) and its `status` line (the synthetic-example
label for an agent built from synthetic data); the `set` (`kind`, `public` for every shipped set;
`name`, `sha256`, `n`, `distinct_inputs` and `norm`, the `norm/v1` rule they are counted under);
the ledger's `providers`; the `models` as reported (`answered`, the ledger's list, and `by_type`,
the run record's); the `method` (`version`, `sha256`, and `in_effect` on the date measured, false
while it is a draft); `measured_on`, the run record's date; `evaluation`, the words "engineering
evaluation, not clinical validation"; and the `disclosure`. A local configuration's disclosure is
"no model provider". A remote one's holds, for each remote type it calls, that type's statement
from the disclosures file, and reads "not published: disclosure pending" while any is missing:
its scope is shown and its figures are not. A board publishes it (`published`) only while the
disclosures file, as it is when the board is built, still holds each of those statements word
for word: one taken out or rewritten since hides its figures, and its statement, until it is run
again.

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
| `replicates` | Only in an entry of replicates: their `count`, the `headline` (1), the `flip_rate` (k of n with its interval), the `spread` (per figure, `{min, max, replicates}`, or `not_estimable`) and the `models` each type reported across them. |

- **Distinct positives:** the dataset's cases are grouped by their input under `norm/v1`. A
  group is a positive of a critical label when any of its cases is, and detected only when every
  positive case in it is.
- **The grade's bound:** the Clopper-Pearson interval of the detected groups over the positive
  groups, at the simultaneous level `1 - 0.05/L` for an agent with L critical labels, stored with
  its `level` and `labels`. It is shown as "lower bound X at Y%, simultaneous across L labels".
- **Evidence:** a critical label has enough when its distinct positives reach
  `method.min_positives(0.95, L)`: 100 for L of 1 to 4, and 107 for the Clinical Trial
  Prescreener's 6. Every critical label needs enough, not just one.
- **Cost** is priced from the run record's tokens, per case and per model type, and the dated
  price table. It is never the ledger's cost, a price an agent sets or one built into a provider.
  It is "0 (no metered calls)" for a configuration that calls no model (CQL included), or whose
  model types made no call on the set (a cascade whose first member was sure of every case). It
  is "unknown" when any provider call failed, or a type that spent tokens reported no model (a
  composite kept none of its answers), more than one (whose tokens a run record does not split),
  or a model no price table holds (there is none, or it lacks the model). An unknown cost reads
  the same with no table as with any, for every model type it calls ("jev, llm: no price table
  holds all their models" for two that a table prices only in part): the entry cites none, and
  is recomputed with none. A type
  that spent no token costs nothing, whatever its model.
- **Latency** is read from the run record, never measured again, and never ranked.
- **Weighted figures:** detection is never weighted. A set with weights on some records and not
  others is refused.
- **Replicates** (LP-17h; the method's section 10): a remote configuration's entry may be made
  from 3 to 5 runs over the set, `entry(..., replicates=[(ledger, run), ...], declared=...)`.
  Its figures are replicate 1's, the headline, as declared before the run. The case flip rate
  is k of n cases whose primary prediction or outcome differs between any two replicates,
  matched by ledger hash (a public set's case key comes from its bytes, so the hashes line up),
  with its 95% interval. The spread is each figure's minimum to maximum across the replicates
  that have it, a description with no interval: detection and critical cases automated per
  critical label, false automation, the AUTO error rate, automation, accuracy, macro-F1, Brier,
  Brier's reliability term, ECE, cost and latency. Subgroups and weighted figures are left out:
  a subgroup's figures are shares of its cases, which move between replicates only where its
  cases flip (the flip rate counts every such case), and a public set is as sampled, with no
  weighted figures. The other replicates are priced from the table only when the headline is.
  Refused:
  replicates of a local configuration (it is deterministic: one replicate, no flips), more than
  5, a headline that is not replicate 1, replicates out of order or of another day, and a
  replicate of another agent, set or configuration, or of other cases.

### The board

`board(entries, current)` shows entries one table per agent and set, in a listing order (by arm,
then configuration, then date), never an order of any figure. `current` is, per agent, its
digest, its arms' statuses and its sets' SHA-256 (`Current.of(pack)`). It reads each entry as its
canonical JSON holds it (keys sorted), so entries just built and the same entries read back from
a store give the same board. A configuration is written `slot=variant`, its slots in sorted
order. Each table holds:

- its ranking (`bands.ranking`): on a public set, off, "public set: regression check, not a
  ranking"; and the words "engineering evaluation, not clinical validation";
- the insufficient-evidence banner when any published entry's critical label is short of
  evidence, naming each such label, its distinct positives and the minimum (an entry whose
  disclosure is pending shows none of its figures, so it is left out);
- each arm's status: measured, "not measured", or "not applicable" for an arm the agent cannot
  build; and each configuration of a measured arm that has no entry in the table (`not_measured`,
  "not measured yet");
- each entry with its marks, worked out when the board is built and never stored: "unsigned"
  (no signature), "method draft, not in effect", "stale: agent changed", "stale: set changed" or
  "stale: method changed" (its digest, set SHA-256 or method SHA-256 is not the current one),
  "stale: another model reported" (its replicates reported more than one model of a type, or a
  run of the same model type, an entry's or a canary's, of any agent or configuration, measured
  the same day or later, reports another model of a type its runs reported; a run that reported
  no model of a type, its calls failed, reports no other), "stale: canary drift" (a canary of
  its agent's arm shows drift, and it is a remote configuration's result of that arm, on any set,
  measured before the canary's day or no later than the replicates it was compared with; a local
  configuration's never is), and "stale after DATE", 45 days after it was
  measured. No clock is read, so the date is shown and never checked. `board(entries, current,
  canaries, disclosures=...)` takes the canaries (`mendari.canary/1`) and the disclosures file
  as it is now (none: no remote entry is published);
- each entry's grade (`grade.grade`): null on a public set.

A board holds no ledger, so it never ranks a hidden set's arms: a hidden table of one entry
reads "off: one arm graded", and one of two or more is refused.

`render.scoreboard_markdown(board)` writes it in Markdown: per table its standing, the banner, the
arms, then one column per entry with its marks, its scope and its figures, the last three its
replicates, its case flip rate and its spread ("1 (local: deterministic)" and "none: one
replicate" for a local configuration). An entry that is not published reads "not
published: disclosure pending" in its Disclosure row and every figure's row. A proportion is k of n
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
  agent file (`rules@…`, `cql:<library>@…`). Jev's and the LLM's model ids never hold an `@`.

An entry is never made from a record that does not agree, so its tokens are never priced as
another model's.

## Price tables and disclosures: `mendari.prices/1` and `mendari.disclosures/1`

A price table is a dated file outside Mendari's code (`scoreboard/prices/<YYYY-MM-DD>.json`),
cited by the SHA-256 of its bytes. A table read from the package, the repository's `src/` or a
agent's directory is refused before it is read.

| Field | What it holds |
|---|---|
| `format` | `mendari.prices/1`. |
| `date`, `source` | When the prices were read, and where from. |
| `prices` | Per model type and model id: `input_per_1000` and `output_per_1000`, USD per 1,000 tokens. |

The disclosures file (`scoreboard/disclosures.json`) holds `format` (`mendari.disclosures/1`), its
`date`, and `statements`: per remote provider type (`jev`, `llm`, `cql`), the statement of any
commercial relationship between Mendari and that provider. Both are closed schemas, and a
refusal of either names a key the input chose as `<key>`, as a run record's does.

## Declarations and canaries: `mendari.declared/1` and `mendari.canary/1`

An agent's declaration (`scoreboard/declared/<pack name>.json`, LP-17h; the method's section 3)
names, per arm, the configurations declared before any run. It is a closed schema, read as
strictly as a run record, and cited by the SHA-256 of its bytes.

| Field | What it holds |
|---|---|
| `format` | `mendari.declared/1`. |
| `pack`, `date` | The agent's id, and the day it was declared. |
| `arms` | Per arm (`mendari.arms/1`), `remote`: the one remote configuration run on public sets with replicates; and `local`: the one local configuration a hidden set grades. Each is slot to variant, and an arm names one or both. |

A remote configuration is run only as its arm's `remote` configuration, with the declaration
committed as it is before the run (`mendari scoreboard run` checks git's `HEAD`). A hidden set
grades each arm's `local` configuration: a new hidden set's answer is stored only when it cites
the declaration as the store holds it and grades every arm the declaration names a `local`
configuration for, with that configuration (LP-17k, "Stored" below). A declaration is refused
when it names a configuration the agent does not have, one in another arm, or one that is not
remote (or local) where it says.

A canary (`scoreboard/canaries/<sha256>.json`; the method's section 12) is a seeded draw of 30
cases of an agent's `test` set, decided once by hand by a remote configuration, and compared with
replicate 1 of its current result there, which holds replicates. The draw takes the cases whose
SHA-256 of a domain tag, the seed and the case's ledger hash is smallest, in the set's order.

| Field | What it holds |
|---|---|
| `format` | `mendari.canary/1`. |
| `pack`, `set`, `arm`, `configuration` | What it was run on: the replicates' agent (`id`, `version`, `digest`), the `test` set (`name`, `sha256`), the arm and the configuration. |
| `draw` | `{size, seed}`: 30 under method 1.0, and the seed. |
| `measured_on`, `models` | The day it ran, and per model type the models its answers reported. |
| `inputs` | Its ledger's id, its run record's SHA-256 and the method's SHA-256. |
| `against` | The result it was compared with (`sha256:` and its body's SHA-256), the day it was measured and its replicates' flip rate. |
| `flip_rate` | k of the drawn cases whose primary prediction or outcome differs from replicate 1's on the same case, matched by ledger hash, with its 95% interval. |
| `drift` | Whether that interval's lower bound is above the upper bound of the replicates' flip rate's 95% interval, decided from k and n. |
| `another_model` | Whether, for a model type both the canary and the result's replicates name (`figures.replicates.models`), the canary names other models. A canary whose calls all failed names none, which is not another model. |
| `evaluation`, `body_sha256` | "engineering evaluation, not clinical validation", and the SHA-256 of its canonical JSON without `body_sha256`. |

## The results store: `scoreboard/`

Scoreboard results are kept in a results store, a folder: `scoreboard/` in the working directory
unless `--store` names another (`evals/results_store.py`, LP-17f; the
[grading method](../grading/method-1.0.md), section 11). It is never part of an agent: an
agent's own `evals/` is in every archive, where a publisher's agent could carry a forged result. Each file is named by its content:

| Path | What it holds |
|---|---|
| `results/<sha256>.json` | A result, named by its body's SHA-256 (`body_sha256` without `sha256:`), sorted and indented, with one newline at the end: a public set's entry (`mendari.scoreboard/1`), with its ledger and run record beside it, or a hidden set's answer (`mendari.hidden_scoreboard/1`), with neither. |
| `ledgers/<sha256>.jsonl` | A ledger (`mendari.ledger/1`), named by its id. |
| `runs/<sha256>.json` | A run record (`mendari.run/1`), named by the SHA-256 of its bytes. |
| `prices/<YYYY-MM-DD>.json` | A dated price table (`mendari.prices/1`), cited by its SHA-256. |
| `canaries/<sha256>.json` | A canary (`mendari.canary/1`), named by its body's SHA-256. |
| `declared/<pack name>.json` | An agent's declared configurations (`mendari.declared/1`). |
| `keys.json` | The public grading keys (`mendari.grading-keys/1`). |
| `disclosures.json` | The disclosures (`mendari.disclosures/1`): no statement yet, so every remote type's is pending. |

- **Append-only.** Nothing stored changes, except that a valid signature may be added to an
  unsigned result whose body stays the same. `mendari scoreboard add FILE [--supersedes HASH]`
  never writes over a file.
- **Supersedes.** A result may name the earlier result it supersedes: one of the same agent, set,
  arm and configuration, measured no later, which no other result supersedes. Both stay. The
  later is the current one, and one agent, set, arm and configuration has one current result at
  most. A hidden set's answer has no arm or configuration of its own (its arms are in it): one
  agent and set has one current answer, as
  [the hidden-set scoreboard](#the-hidden-set-scoreboard-mendarihidden_scoreboard1) says.
- **Running.** `mendari scoreboard run PACK [--dataset NAME ...] [--config slot=variant ... |
  --every-local] [--again]` runs a configuration of an agent Mendari maintains over its public
  scoring sets (every set it declares but `train` and `calibration`), and stores the ledger, the
  run record (each case's latency and tokens) and the entry, unsigned. A result that is still
  current (the agent's digest, the set's SHA-256 and the method's unchanged, and not past its
  stale date) is kept unless `--again` (a board's canary and model marks are not read: a result
  they mark is run again with `--again`); a new one supersedes it. It refuses a fitting set and
  anything but a set the agent names, so a file or a hidden set is never run. A remote
  configuration (LP-17h) is run by hand only: it is refused when `CI` or `GITHUB_ACTIONS` is
  set, without `--replicates 3` to `5` (a local one runs 1), unless the agent's declaration names
  it for its arm and is committed as it is, and, when it calls a model, without `--prices FILE`
  (stored under `prices/` when a result is priced from it), all before any call; so is a table
  whose date another stored table holds, and a provider this machine has not set up (its
  preflight: the CQL engine's URL, Jev's endpoint and model, an LLM's model), which would fail
  every case. Every replicate's ledger and run record is stored. `mendari
  scoreboard canary PACK --config ... --size 30 --seed S` runs a canary the same way, by hand, with
  no schedule. The agent is read from the repository's `agents/` (the bundled agents), never from
  `MENDARI_PACKS_PATH` or the registry cache. The check and the page read it from there too.
- **The check.** `scripts/check_scoreboard.py --base REF`, a step of the required
  `demo-agent` job, names each file whose name is not its content's, each result of the base
  removed or changed (but for a valid signature added), each `supersedes` link that breaks the
  rules above, and each current result whose signature does not verify against `keys.json`, or
  was made with a publisher's or a registry's key this machine pinned (CI pins none). It
  recomputes every public result from its stored ledger, its dataset (SHA-256 checked), its run
  record, its price table and the method, byte for byte, and re-runs every local ledger on its
  agent's current digest. A result of an older digest is recomputed and kept, never re-run; one
  whose set or method changed is kept as it is, and shown stale. A new result must be on its
  agent's current digest, set and method, and measured no later than today. A new remote
  configuration's result must hold 3 to 5 replicates and cite its agent's declaration as the
  store holds it, which declares its configuration for its arm: it is recomputed from every
  replicate's ledger and run record, and its ledgers are never re-run, which would call off the
  machine. Each declaration is checked against its agent; each canary is recomputed from its
  ledger, its run record and the result it was compared with (and its draw, while the set is
  the one it was drawn from), and one of the base removed or changed is refused. A remote
  configuration's disclosure is recomputed from `disclosures.json` while its result is new, never
  taken from the result; once stored, it keeps the statements it holds. A superseded result
  keeps the signature it had, so a key taken out of the keys file leaves what it signed as
  history. These recomputations and the rules for a new result are a public result's: a hidden
  set's answer (`mendari.hidden_scoreboard/1`) has no ledger, run record or dataset to
  recompute from, may be of any version of an agent Mendari maintains, and is checked as
  [the hidden-set scoreboard](#the-hidden-set-scoreboard-mendarihidden_scoreboard1) says
  ("Stored").
- **The page.** `scripts/scoreboard_page.py` writes
  the early scoreboard from the current results, through `board`
  and `render.scoreboard_markdown`, and lists each superseded result; `--check` (also in
  `demo-agent`) fails when the page is not the store's. Its bytes depend only on the repository:
  the store and `agents/`.

## Paired comparisons: `mendari.compare/1`

`mendari compare A B --dataset X` compares two runs of one agent on the same dataset, from their
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

`mendari data coverage PACK --dataset NAME|FILE` says what a labelled set covers, for the agent
whose questions it is labelled for, without running the agent (LP-11). It reads an agent's dataset
or any JSONL file, and refuses one the agent cannot read as `mendari eval` does (by case id, never
case text).

Two cases are the same case when their inputs are, under one of two forms
(`evals/canonical.py`, pinned byte for byte, and the forms the hidden-case index and the agent
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
(`mendari.eval/2`) still counts every positive, so a set built from templates can report many
more positives there than it holds distinct.

## Rater agreement: `mendari.agreement/1`

How far raters who label the same cases blind agree, per question of the agent (LP-11,
`evals/rater_agreement.py`). The input is each case's first-pass labels by rater: raters label
the agent's member questions, and a derived question's label for a rater comes from the agent's
decision table over that rater's member labels. An adjudicator's label is not a first pass.

The report holds `format`, `pack`, `level` (0.95), `seeds` (`{bootstrap, resamples}`: the seed,
and 2,000 resamples unless set) and `questions`: for each of the agent's questions, `cases` (the
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
| `pack`, `providers`, `policy` | As a site report holds them (`mendari.site-report/1`): the agent's id, version and digest; each slot's variant, type and model (local only); the risk level, thresholds and whether a governance override applied (never: a run measures the agent's own policy). |
| `seeds` | `{bootstrap, resamples, level}`, as `mendari.eval/2`'s. |
| `primary` | The agent's primary question. |
| `questions` | Per question, as a site report's: `n`, `accuracy`, `macro_f1`, `per_label` (precision, recall, F1, support), `confusion`, `brier`, `calibration` (`ece` and the equal-width bins of 10 cases or more only); and as `mendari.eval/2`'s, `proportion` (accuracy as k of n) and `brier_interval`, null over fewer than 10 scored cases. |
| `decisions` | The outcome counts and rates, the abstention rate, automation and review, recommendation accuracy, and per critical label its `question`, `label`, `n`, `detected`, `detection_rate`, `auto_missed` and `proportion` (detection as k of n). |
| `threshold_sweep` | Per threshold: `threshold`, `coverage`, `accuracy`, `errors`. |
| `errors` | Error codes and how many answers each failed (Mendari's closed set). |

It MUST NOT hold a case's id or hash, a dataset's name or hash, the ledger's id, a critical
label's missed cases, misclassified cases, subgroups, equal-mass calibration, latency or cost. A
calibration bin, and a Brier interval, over fewer than 10 cases is left out, as a site report
leaves out small bins: over a few cases their figures are single cases'. Rates are rounded to 4
places and other figures to 3, as a site report's are. It names the agent's labels beside counts
and rates, never beside a case.

A refused run is answered with a fixed `reason` (`no_set`, `wrong_pack`, `unknown_variant`,
`remote_provider`, `pack_unavailable`, `not_gradable`, `no_runs_left`, `expired`,
`set_changed`, `set_unavailable`, `run_failed`) and one fixed sentence, never a store's or a
run's message: a set whose rows no longer give its commitment is `409` `set_changed`
([the runbook](../operations/evalsets.md)). An agent whose own names or sizes this schema cannot
hold (more than 64 questions, slots or critical labels, a question with more than 64 labels, or
a question, label, slot or variant name that is not 1 to 64 letters, digits, `_`, `.` or `-`
starting with a letter or digit) is refused `pack_unavailable` before the set's cases are read,
so it uses none of the set's runs.

## The hidden-set scoreboard: `mendari.hidden_scoreboard/1`

What the same route answers a run that declares its arms (LP-17k, `evals/hidden.py`; the
[grading method 1.0](../grading/method-1.0.md), sections 3 to 9). The request sends `arms`, each
declared arm (`deterministic`, `classifier`, `llm` or `classifier_and_llm`, one to four) with its
whole configuration (a variant for every slot of the agent, as `variants` names them), and
`declared`, the SHA-256 of the committed declaration
they come from (64 hex digits), in place of `variants`. `arms` and `declared` come together, and
never with `variants`. Without `arms`, the answer is `mendari.hidden_result/1`, as above.

Before any case is read, each declared configuration is resolved and refused as `variants` is
(`wrong_pack`, `pack_unavailable`, `unknown_variant`, `remote_provider`), then refused
`wrong_arm` when the configuration it would run is not the one sent, slot for slot (a slot left
out would run the deployment's choice, so one cited declaration would grade other
configurations on other deployments), or not a configuration of the arm it is declared under,
and the agent is
refused `pack_unavailable` when it has no critical label to attest (or more than 64). Under the
method 1.0 a hidden set is graded with local providers only (owner decision 2), so only the
deterministic arm can be declared. Then one grading read hands the cases, in memory, to every
declared arm: each is decided by its own Decision (local providers, no store), in the
scoreboard's order, on the same records. Their ledgers share the read's case key and pair, and
the run is one chained `evalset.graded`, one of the set's runs. Each arm's entry (as
`mendari.scoreboard/1`'s figures, with the records' inputs giving its distinct groups and their
`attributes.weight` its weighted figures), its grade and the ranking are computed in memory and
dropped once projected onto this closed schema. Nothing is stored: no decision, report, ledger or
usage. A field not listed here MUST NOT appear, and a server MUST refuse to answer a run whose
result does not fit it (`run_failed`), and say nothing of it.

| Field | What it holds |
|---|---|
| `format` | `mendari.hidden_scoreboard/1`. |
| `scope` | The agent (`id`, `version`, `digest`) and its `status` line; the `set`: `kind` `hidden`, its `id` and freeze `commitment` (never a hash of its content), `n`, `distinct_inputs` and `norm`; the `method` (`version`, `sha256`, `in_effect` on the day measured); `measured_on`, the day of the run (US Eastern); `evaluation`, "engineering evaluation, not clinical validation"; the `disclosure`, "no model provider" with no statements; and `declared`, the declaration's SHA-256 as sent. |
| `seeds` | `{bootstrap, resamples, level}`: 0, 2,000 and 0.95. |
| `arms` | Every arm of the scoreboard, once, in its order: `arm`, its `name`, its `status` and, for a graded arm only, its `configuration` (`providers`, `local`, `remote_types`), `providers` (each slot's variant, type and model), `figures` and `grade`; null for the others. |
| `banner` | The insufficient-evidence banner, or null: each graded arm's critical label short of evidence, with its configuration, distinct `positives` and the minimum `needed`. |
| `ranking` | `{status: "off", reason: "one arm graded"}` while one arm is graded; else `on`, with the `method`, `draft`, how many arms were `graded`, `ranked` (each arm's `rank`, rank `interval`, configuration and `bands` without cost), `unranked` (each arm with the safety attestations it did not meet) and the Holm `family` (see [Grades and band ranking](#grades-and-band-ranking)). |
| `supersedes` | Null as answered; the earlier stored result it supersedes once `mendari scoreboard add --supersedes` links it. |
| `run` | `{run, runs_left}`: this run's number (its chained `evalset.graded`) and the runs the set has left after it. |
| `body_sha256` | `sha256:` and the SHA-256 of the answer's canonical JSON without `body_sha256`, `signature` and `run`. |

An arm's status is `graded` when it was declared; `not graded here` when it was not and the agent
builds it (every such configuration calls a remote provider, which a hidden set never runs); and
`not applicable` when the agent cannot build it. A graded arm's figures are an entry's (see
[Scoreboard entries](#scoreboard-entries-mendariscoreboard1)) but cost, latency and subgroups:
`critical` (detection and positives decided AUTO as raw k of n, distinct positives, the grade's
bound and the evidence), `false_automation`, `auto_error_rate`, `automation` and `accuracy`
(each k of n with its exact 95% interval), `macro_f1` and `brier` (each with its bootstrap
interval; Brier's null over fewer than 10 scored cases, whose bounds would be single cases'
scores), `brier_reliability`, `ece` (one `value`, never its bins) and `weighted`. Its grade is
`grade.grade`'s: the method and the attestations, each draft while the method is not in effect.
Figures are rounded as `mendari.hidden_result/1`'s are: every share (each bound of a k of n,
ECE, a weighted share, detection's band value, a p-value) to 4 places, and every mean, score,
effective n, Brier difference and other band value to 3, so the answer's digits confirm a
guessed set no more than that answer's do. The method's levels, floors, ceilings and alpha are
kept as the method fixes them.

`weighted` is `{status: "as sampled"}` when no case carries a sampling weight. Otherwise it is
`{status: "withheld", reason: "fewer than 10 cases of a share carry one of its weights"}`
unless every weight is carried by 10 cases or more (the method's subgroup minimum) of each share
it is computed over (all cases, the labelled ones, those decided AUTO); then it is `weighted`,
with each share's Hájek estimate, Kish's effective n and Korn-Graubard interval. A weight is
its stratum's (N_h / n_h), so the weighted figures beside the unweighted k of n over the same
cases can be solved for a stratum's figures: over a few cases of one weight, that would be a
subgroup's figures on a cell smaller than the subgroup minimum.

It MUST NOT hold cost, latency, subgroups, equal-mass bins, a case's id or hash, a missed or
misclassified case, the dataset's name or hash, or a ledger's id. Its only `sha256:` values are
the agent's digest and its own body's. It names the agent's labels beside counts and rates, never
beside a case. Residual: when the weighted figures are given, a stratum's figures over 10 cases
or more can still be worked out from them, as a subgroup's at its minimum could; and two shares
over overlapping cases (all cases and the labelled ones, say) can be differenced for a count
in a smaller cell.

Two reads of one frozen set on one day give the same body, so the same `body_sha256`: only the
run block differs. A refused run is answered as a grading run's refusals are, `wrong_arm` among
them (`422`); a body that sends `arms` without `declared`, `declared` without `arms`, either
beside `variants`, an arm that is not one or a declaration that is not 64 lower-case hex digits
is a `422` of the body, and nothing is read.

**Stored.** The answer is stored in the [results store](#the-results-store-scoreboard) as it was
answered (`mendari scoreboard add FILE`), named by its body's SHA-256: one result per agent and
set, its arms in it, so a second read of the same set is the same result. The store checks it
against this closed schema field by field (its values rounded as above), against an agent Mendari
maintains (any of its versions) and, when new, the current method, a day no later than today,
`in_effect` as the method is on that day, and its agent's declaration
(`scoreboard/declared/<pack name>.json`, `mendari.declared/1`): `declared` is the SHA-256 of the
declaration as the store holds it, and the graded arms are the arms it names a `local`
configuration for, each with that configuration, so the store, which the route never reads,
holds the configurations declared before the run. Under the current method it also checks the answer
against its own figures: each k of n's interval is the one its k and n give, each critical
label's evidence the one its distinct positives give, each graded arm's grade `grade.grade`'s
of its figures under the scope's method (so every attestation's state, figures and draft, and
the grade's `in_effect`), the banner the one the evidence gives, and the ranking on with two
graded arms or more and off with one, under the scope's method, a draft while it is not in
effect, with each graded arm unranked by the safety attestations it did not meet and ranked
otherwise. What no ledger is kept to recompute is kept as it is: each k and n, and a ranking's
ranks, intervals, bands and tests are the owner's attestation once signed. Its signature
(LP-17e) covers its body and its run block, so its run's number cannot change unnoticed. The
early scoreboard, of public sets, does not show it.
