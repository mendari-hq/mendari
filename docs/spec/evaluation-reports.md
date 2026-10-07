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
- `models`: per question, the models that answered and how often.

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
| `GET /api/monitoring` | `weeks[].figures`, `last_7_days.figures`, `benchmark.figures` | each week's and the last 7 days' automated, override and accuracy shares, and the benchmark's accuracy |
| `GET /v1/packs/{pack}` | `latest.figures` | the latest version's benchmark accuracy and leaderboard |
| `GET /v1/packs/{pack}/{version}` | `figures` | that version's benchmark accuracy and leaderboard |
| `GET /v1/search` | each item's `figures` | the benchmark accuracy only |

A registry's leaderboard figures are null when the board is malformed, or would hold more than
5,000 figures (`MAX_BOARD_FIGURES`; a shipped one holds a few hundred), so one upload cannot make
every read of it slow.

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
