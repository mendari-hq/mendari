# Evaluation reports, ledgers and paired comparisons

This page specifies what an evaluation run writes: the report (`mendari.eval/2`), the per-case
ledger its result is computed from (`mendari.ledger/1`), and a paired comparison of two runs
(`mendari.compare/1`). It supersedes the report format of the
[Decision Pack spec 0.2](decision-pack-0.2.md) (`mendari.eval/1`), which still loads. The key
words MUST, SHOULD and MAY are used as in that spec.

## The report: `mendari.eval/2`

A report has two parts.

- **The result:** every key except `run`, `comparison` and `result_sha256`. It MUST be computed
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
