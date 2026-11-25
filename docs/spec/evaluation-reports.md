# Evaluation reports, ledgers and paired comparisons

This page specifies what an evaluation run writes: the report (`mendari.eval/2`), the per-case
ledger its result is computed from (`mendari.ledger/1`), and a paired comparison of two runs
(`mendari.compare/1`). It supersedes the report format of the
[agent spec 0.2](agent-manifest-0.2.md) (`mendari.eval/1`), which still loads. The key
words MUST, SHOULD and MAY are used as in that spec. These formats name the agent with the key
`pack`, the word Mendari used before: hashed and signed records hold that key, so it keeps its
name.

## The report: `mendari.eval/2`

A report has two parts.

- **The result:** every key except `run`, `comparison`, `result_sha256` and `figures` (what a
  server serves beside a report, never stored). It MUST be computed
  only from the run's ledger and the seeds it names, so two runs on the same cases with the same
  agent, providers and seeds give byte-identical results (canonical JSON: sorted keys, no spaces),
  and anyone holding the ledger can recompute it.
- **The run:** `run` holds what differs between two runs on the same cases: its `id`,
  `created_at`, `latency_ms` (mean, p50, p95, max), the engine version, a governance
  override's approver (kept locally, never in the result), and the run's operating point
  (`operating_point`, [below](#the-operating-point-mendarioperating-point1)), which depends
  on the options the run was given. `result_sha256` is the SHA-256 of the canonical result.

`comparison`, outside the result, is the local validation against the agent's benchmark. For an
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

## The operating point: `mendari.operating-point/1`

`run.operating_point` says what the agent's automation threshold (`policy.thresholds.auto`)
lets through on the run's cases and, when the run was given targets, the lowest threshold the
cases support. It is in `run`, outside the result: it depends on the run's options (the targets
and their confidence) and on what each decision kept in memory (answers a ledger rounds), so
`result_sha256`, the ledger and every signed record are unchanged by it, and it cannot be
recomputed from the ledger. It MUST NOT hold case text or a case id. It is advice: no decision
reads it, and nothing edits a manifest from it. A report written before the block has none.

It counts two risks, each as the result counts it:

- a **critical miss**: a case of a critical label that is automated, so that no person reads it
  (the result's `auto_missed`), as a share of that label's cases and as a share of all cases;
- **false automation**: a case automated with a wrong recommendation, as a share of the cases
  labelled for the primary question (the result's `proportions.false_automation`).

Its keys, beside `format`:

- `status`: `no_target` (the run was given no target: only `current` is worked out),
  `supported` (the manifest's threshold is certified, and no certified threshold automates
  more), `recommended` (another threshold is the one the cases support), `none_certified` (no
  threshold that automates a case is certified), `nothing_automated` (the policy automates no
  case at any threshold) or `no_replay` (the run did not keep what its policy decided from, so
  the policy cannot be applied again);
- `confidence`, the confidence the run asked for, and `level`, the one-sided level each bound
  is taken at (with two targets the error level is split between them);
- `targets`: `critical_miss` (`at_most`, and `of`: `critical`, each critical label's cases, or
  `all`) and `false_automation` (`at_most`), each null when the run gave none;
- `current`, and `recommended` (null unless a threshold is supported or recommended), each a
  point: its `threshold`, the cases `automated` there (`k` of `n`), per critical label
  `of_critical` and `of_all`, and `false_automation`, each as `{k, n, upper}` where `upper` is
  the exact one-sided Clopper-Pearson bound of k of n at `level` (null when n is 0);
- `current_supported`: whether the manifest's own threshold is at or above the lowest
  certified one (null with no target);
- `change`: when another threshold is recommended, `{setting, from, to, line}`, the one line of
  the manifest an author would change (`setting` is `policy.thresholds.auto`); else null;
- `walk`: the thresholds tested, which are fixed before a case is read (1.00 down to the review
  threshold in hundredths): `from`, `to`, `step`, how many are `certified`, the threshold it
  `stopped_at`, and under `tested` the counts each certified threshold, and the one it stopped
  at, was judged on;
- `needs`: with no threshold certified, per risk that lacks cases, how many `cases` would show
  it within its target; `cannot_fix`: the risks whose share is above their target at every
  threshold that automates a case, counted at the strictest such `threshold`;
- `repeated`: how many cases have the same input as an earlier one (each still counts);
- `summary`, `lines` (with `kinds`, what each line is, in the same order) and `guarantee`: the
  sentences every surface shows as they are: the command line, `mendari eval --markdown` and
  the console.

A threshold is certified when every targeted risk's `upper` is within its target, tested from
the strictest threshold down and stopping at the first that is not, so the guarantee holds
however many thresholds were tried.
[Choosing an automation threshold](../guides/choosing-a-threshold.md) explains the method, what
the guarantee assumes and how many cases a target needs. Targets are options of `mendari eval`
(`--target-critical-miss`, `--target-critical-miss-of`, `--target-false-automation`,
`--target-confidence`). They are not limits: a limit (`--min-...`, `--max-...`,
`--no-worse-than`) is held against the run's own counts and fails the run, and nothing about it
is stored in the report ([Evaluate an agent on every change](../guides/evaluate-in-ci.md)).

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
| `GET /api/agents/{namespace}/{name}` | `benchmark.figures` | the benchmark report's figures |
| `GET /api/agents/{namespace}/{name}` | `leaderboard` | the leaderboard as served: `ranking`, `groups` (one per set of records, with `about` and `label`) and each row's `rank`, `standing`, `standing_text`, `group` and `figures` |
| `POST /api/agents/{namespace}/{name}/evals`, `GET /api/evals` | `figures` | each report's figures (null when a stored report's cannot be read) |
| `GET /api/stats` | `periods` | the last 7 and 30 days and all time (local days), each with the window before it: automated, overridden and agreed shares and each outcome |
| `GET /api/monitoring` | `weeks[].figures`, `last_7_days.figures`, `benchmark.figures` | each week's and the last 7 days' automated (of decisions made: refused inputs left out), override, accuracy, `reviewed_accuracy` (decisions sent to a person whose recommendation the reviewer kept) and `automated_accuracy` (automated decisions with an outcome recorded downstream) shares, and the `provider_errors` (decisions a provider failed, from LP-21d on) and `abstained` shares, over the served version's decisions; the benchmark's accuracy and its `reviewed_accuracy` (one minus its expected override rate) and `automated_accuracy`, which the alerts compare, stratum by stratum, only when `benchmark.comparable` is true; otherwise `benchmark.reason` says why (`from_registry`: an agent pulled from a registry, whose benchmark is never read; `no_benchmark`, `another_build`, `providers_unknown`, `other_providers`, `other_policy`), and an agent from a registry's benchmark figures are null |
| `GET /v1/agents/{namespace}/{name}`, `GET /v1/agents/{namespace}/{name}/{version}` | `latest.checked.regression.accuracy`, `checked.regression.accuracy` | the registry's own regression check on the agent's test set, labelled "publisher's own test set: regression check, not a ranking" |
| `GET /v1/agents/{namespace}/{name}` | `latest.figures` | `benchmark` and `leaderboard`, always null: a registry shows no uploaded figure |
| `GET /v1/agents/{namespace}/{name}/{version}` | `figures` | the same, always null |
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

**The Brier score's reliability term** (`brier_reliability`), which the calibration report
gives, is Murphy's: over ten equal-mass bins of top-label confidence (a case's largest
probability), the sum of each bin's share of the cases times the squared distance between its
mean forecast and how often each label was the truth there. Tied confidences are averaged, as
the calibration error's are, and it is not estimable, as that error is, when a bin would hold
fewer than 30 cases.

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
`GET /v1/agents/{namespace}/{name}/site-evidence` and worked out on every read from the reports
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

## Publisher evaluations: `mendari.publisher-evaluation/2`

A publisher's signed evaluation of one listed version on one of its own public datasets, in one
configuration ([ADR-0020](../decisions/ADR-0020-publisher-evaluations.md), `hub/evaluations.py`),
sent by `mendari push AGENT --key publisher.key --evaluate NAME` (every configuration, with the
version) or `mendari eval AGENT --dataset NAME --publish --key publisher.key` (one):

```json
{"format": "mendari.publisher-evaluation/2",
 "pack": {"id": "mendari/portal-message-agent", "version": "0.1.2", "digest": "sha256:…"},
 "configuration": {"reader": "rules"},
 "dataset": {"name": "test", "sha256": "<64 hex>", "n": 600,
             "provenance": "synthetic", "labelled_by": "The script that wrote the cases"},
 "report": {"format": "mendari.site-report/2", "…": "…"},
 "claimed": {"latency_ms_p50": 0.05, "latency_ms_p95": 0.06,
             "cost_usd_per_decision": 0.0, "failed_cases": 0},
 "models": ["rules@33463eddba9c"],
 "engine": "0.1.0",
 "created_at": "2026-11-22T12:00:00Z",
 "key_id": "ed25519:<16 hex>",
 "signature": "<base64>"}
```

- **Two formats.** `/2` adds what an evaluation page needs beside the counts:
  `dataset.provenance` (what the agent's manifest says its data was built from,
  `card.provenance`: `synthetic`, `de_identified`, `site_records`, or null; the registry
  refuses a record that says otherwise than the manifest in the signed archive),
  `dataset.labelled_by` (who labelled the cases, in the publisher's words: one printable line
  of at most 200 characters, or null), `engine` (the Mendari release that ran the evaluation)
  and `claimed.failed_cases` (how many cases a provider failed on, at most the dataset's n:
  there the engine sends the case to a person, so those cases say nothing of the
  configuration). `mendari.publisher-evaluation/1` is the same record without those four, and
  is not changed: the format is in the signed bytes, so a record of one never verifies as the
  other; a registry takes either, stores what it was sent, and shows a `/1` record with what
  it does not state as not stated. The command line signs `/2`.
- **Closed.** No other field is taken; every name is one the agent defines.
- **`configuration`** names a variant for every provider slot. **`dataset`** is a dataset the
  manifest declares (never a path), its file's SHA-256 and its number of cases (at least 100).
- **`report`** is the run's [`mendari.site-report/2`](#shared-site-reports-mendarisite-report2)
  counts (`counted_report`): whole, with no cell suppressed, as a public set holds no patient.
  `remote_consent` is whether the configuration calls a provider off the machine (a type that is
  not `rules`, `criteria`, `dosing` or `naive_bayes`, or a composite with one).
  `governance_override` is false: an evaluation measures the agent's own policy.
- **`claimed`** (the run's latency p50 and p95 in milliseconds, its cost per decision in USD,
  and in `/2` the cases a provider failed on) and **`models`** (the models that answered, as
  the ledger names them) are the publisher's own word, shown labelled so; they are not counts.
- **Signature.** Ed25519 with one of the namespace's publisher keys over
  `mendari-publisher-evaluation-v1\0`, the format, `\0`, then the canonical JSON of every other
  field (sorted keys, no spaces, ASCII). The evaluation's id is the SHA-256 of the canonical JSON
  of the whole record.

The registry serves the live evaluations of a version as `mendari.publisher-evaluations/1`
(`GET /v1/agents/{namespace}/{name}/{version}/evaluations`): each with `rerun` (`matched`: the
registry ran the configuration again and counted the same; `calls_a_model`: not re-run),
`excluded` (`"key revoked"`, or null) and `figures`, worked out from the counts: per critical
label `detected` and `automated` (k of n, with `automated_misses` the count) and
`insufficient_evidence` under 100 positives, then `accuracy` (the primary question's right of
n), `false_automation` (of the primary question's n) and each outcome's count and share. Every
share is k of n with its exact 95% interval; none is a rate the publisher sent.

Each stored record is verified again on every read (its closed shape, its id, what its row
says of it, and its signature with a key of the namespace's). One that fails is listed as not
verified (its `signature` and `excluded` fields say so), with no figure, model or claim of its
own. `GET /v1/agents/{namespace}/{name}/{version}/evaluations/{id}` serves one signed record
as stored, for anyone to check.

### An agent's evaluation in public: `mendari.agent-evaluation/1`

What the Agent Hub's evaluation page and a README's badge are built from
(`GET /v1/agents/{namespace}/{name}/evaluation`, `hub/evaluation_page.py`): the publisher's
live evaluations of the agent's newest version that is not recalled (or `?version=`), from the
signed records and nothing else.

- **`state`**: `evaluated`; `none` (no evaluation published for this version); `not_verified`
  (evaluations are stored and none can be shown); `recalled`.
- **`datasets`**: each with its name, SHA-256, size, provenance and who labelled it, and its
  `configurations`: every figure k of n with its exact 95% interval, worked out from the
  counts; the critical cases missed and automated; the outcome mix; the publisher's own
  latency, cost and failed cases, labelled as claimed; the key that signed it, and whether the
  registry ran it again.
- **`comparison`**, for each dataset: the configurations in the order of
  [`mendari.rank/1`](#leaderboards-mendarileaderboard2-and-mendarirank1) without its cost
  (fewest critical cases automated, then the worst critical label's detection, then false
  automation, then accuracy), a `status` (`ordered`, `tied`, `one_configuration`,
  `no_critical_label`, `insufficient_evidence`), the measure that decided the first against the
  second with both figures, and two fields that say what a page may claim:
  - `first_band`: the configuration listed first with every other one the counts cannot tell
    from it: one that did the same on all four measures, or one whose difference from the
    first, on the measure where the two first differ, lies inside their exact 95% intervals;
  - `best`: a configuration **only when it is alone in the first band**, else null. The order
    is total, so some configuration is always listed first; being listed first is not having
    done best. Null too for a tie, a single configuration, and when nothing ranks (no critical
    label has 100 positive cases, or the agent names none).
- **`not_shown`**: each stored evaluation that does not verify, or whose key was revoked
  since, with why, by its row's names and with nothing its record says. None takes part in a
  figure, the comparison or the badge.
- **`links.page`**: where the Agent Hub shows it, once the registry's operator says where the
  Hub is (`MENDARI_REGISTRY_HUB_URL`); null otherwise.
- **`note`**: that the evaluation is the publisher's own, on a dataset it chose, compares the
  configurations of one agent, ranks no agent against another, and is not evidence of how the
  agent does on a site's own cases.

`GET /v1/agents/{namespace}/{name}/badge.svg` says the same in one line of fixed words and
whole counts ("evaluated, 2 configurations, 600 synthetic cases, 0 to 9 critical automated",
"no evaluation published", "evaluation not verified", "recalled"): an image with no script,
style, link or reference, never green, and it names no configuration as best.

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
them. A public set is a regression check, never a ranking, so no surface prints a rank (a board
can also hold a site's own file, `--dataset PATH`, and nothing on it is graded either):

- The Decision Card's "Provider comparison" opens with "Public set: regression check, not a
  ranking", and says that nothing there is graded and the order its rows are listed in. Its
  tables have no Rank column. A row measured on
  an earlier version of the agent says so beside its configuration.
- `mendari leaderboard` prints the same opening and the same tables.
- The dashboard's Provider comparison says the same. It has no rank column, and its cost chart names the deployed configuration and every configuration no other
  beats on both cost and accuracy.

The rows are listed in `ranking.grouped`'s order, a listing order: those that rank as data
(measured on this version of the agent, on records where a critical label has at least 100
positives) first, in `mendari.rank/1`'s order, the critical cases caught first; then every
other row, by configuration name. A table of rows all measured on earlier versions is therefore in order of configuration name. The data and the API do not
change: each row keeps its `rank` and `standing`, and `GET /api/agents/{namespace}/{name}` serves them as
before.

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

`mendari data coverage AGENT --dataset NAME|FILE` says what a labelled set covers, for the agent
whose questions it is labelled for, without running the agent (LP-11). It reads an agent's dataset
or any JSONL file, and refuses one the agent cannot read as `mendari eval` does (by case id, never
case text).

Two cases are the same case when their inputs are, under one of two forms
(`evals/canonical.py`, pinned byte for byte, and the forms the dataset checks use):

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

## Written output: `mendari.written/1`, its ledger and its grades

What an evaluation of an agent's written output writes
([agent manifest spec 0.4](agent-manifest-0.4.md), sections 16 to 20;
[ADR-0025](../decisions/ADR-0025-a-judge-is-trusted-by-measured-agreement.md)). The reference
implementation is `mendari.evals.written`.

**The ledger, `mendari.written-ledger/1`.** Canonical JSON Lines, its id the SHA-256 of its
bytes. Line 1 is the header: the agent (`pack`: id, version, digest), the dataset (name,
SHA-256, size), the case-hash scheme, the rubric (its name, the SHA-256 of the rubric as a
grader reads it, its output and each criterion's levels, scores, pass line and whether it must
not fail), the floors, the providers, the judge (its model, whether its grades are canned, and
the SHA-256 of its prompt) and the models. Then one line a case, in the dataset's order:

```json
{"c": "<case hash>", "i": "<item id or null>", "n": "none | refused | error | null",
 "j": {"<criterion>": ["<level>", 0.93]}, "e": {"<criterion>": "<error code>"},
 "m": [0, 1], "u": [null, 0, 0]}
```

`i` is the item's id: an HMAC-SHA256, under the dataset's case key, of the case's id, the
output's name and the exact text, cut to 128 bits. `n` says why a case has no output. `j` holds
the judge's grade and its confidence for each criterion it graded, `e` the code of each it
could not. `m` is the agent's and the judge's model, as indices into the header's `models`; `u`
is the judge's cost and tokens. No text, no context and no case id is in a ledger.

**Grades, `mendari.written-grades/1`.** JSON Lines. Line 1:
`{"format", "rubric": {"name", "sha256"}, "dataset": {"name", "sha256"}}`, with
`"simulated": true` when the graders are no clinicians (a demonstration's). Then one line for
each item and grader: `{"item", "grader", "grades": {"<criterion>": "<level>"}}`. A grader's id
matches `^[A-Za-z0-9][A-Za-z0-9._:@-]{0,99}$`; every criterion is graded with one of its
levels; an item and grader appear once. A problem names the file and the line, never a value.

**The report, `mendari.written/1`.** Computed from the ledger, the grades and the seeds alone,
so a second run gives the same bytes; `result_sha256` covers everything but `run`.

| Key | Holds |
|---|---|
| `pack`, `dataset`, `providers` | as the ledger's header |
| `rubric` | its name, SHA-256 and output |
| `ledger` | the ledger's id and how many cases it holds |
| `judge` | its model, `canned`, `prompt_sha256`, and `same_model_as_agent` |
| `seeds` | the bootstrap's seed, its resamples and the level (0.95) |
| `floors` | `agreement`, `must_pass_recall`, `max_judge_errors`, `min_paired`, `min_each_side`: the five the report was decided on |
| `cases` | `n`; `with_output`; `no_output` by reason; `judged`; `judge_errors` by code; `graded`; `graded_twice`; `stale_grades` (grades of a text the run did not produce: counted, never used) |
| `graders` | each grader's id, with how many grades they gave |
| `simulated_graders` | whether the grades file says its graders are simulated |
| `criteria` | per criterion, the block below |
| `cost` | the judge's cost (null when a call was not priced) and tokens |

Per criterion: `scale` (levels, scores, `pass_from`); `must_pass`; `clinicians` (`passing` as k
of n with its exact interval, `levels` counted, `disputed`); `clinician_agreement` (`cases`,
`agreement`, `kappa`); `judge_agreement`; `judge_errors` (the outputs the judge could not grade:
`paired`, of those clinicians graded, and `ungraded`, of the rest, each `{k, n}`); `validation`
(`state`: `validated`, `not validated` or `insufficient evidence`, with its `reasons`); `judged`
(the judge's `passing` share and `levels` for the outputs no clinician graded, and
`could_not_grade`, how many of them it could not: present only when the state is `validated`,
null otherwise); and `cases` (ledger hashes of the outputs clinicians failed, of those the
judge passed all the same, and of the disputed ones, at most 50 each).

`judge_agreement`, on the outputs both graded: `cases`; `agreement`; `kappa` (weighted, over
the levels); `pass_fail_kappa` (plain kappa on pass or fail: what a pass share is about, and
the same as `kappa` on a yes/no criterion); `pass_fail` (the four counts); `passing` (the
`clinicians`' pass share and the `judge`'s on those same outputs); `symmetry` (`p`, the exact
two-sided McNemar p-value on the discordant pairs; `alpha`, 0.05; `leaning`: `lenient`,
`strict` or null); and `failures_found` for a criterion that must not fail, where an output
the judge could not grade counts as not found.

A report is never computed on fewer than 2,000 resamples, and `seeds` states the seed and the
resamples it was decided on. The judged share is withheld from the report, not from the
ledger: a ledger holds each grade the judge gave, by case hash, since a result is recomputed
from it.

A `kappa` is stored as its estimate and its percentile-bootstrap interval over seeded
resamples of the items, to six places (`evals/rater_agreement.py`), or `not_estimable` with its
reason: both graders gave every item one and the same level, or, for its interval, a resample
left it undefined (`dropped` says in how many). The gate is decided on the
bootstrap's bound as computed and on the exact interval's, never on a stored six-place figure,
so a bound just under a floor never rounds up to meet it.
