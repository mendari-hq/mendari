# Choosing an automation threshold

An agent's manifest says from which confidence a decision is automated
(`policy.thresholds.auto`). That number decides how much work the agent takes off people, and
how often it acts alone when it should not have. `mendari eval` tells you which threshold your
evaluation data supports, with a stated guarantee, for example:

```text
Recommended automation threshold 0.93 (now 0.90, which these cases do not support): critical
misses at most 2%, 95% confidence, 63.3% automated.
```

The recommendation is advice. Nothing edits your manifest: the report prints the one line you
would change.

This page is for the author who sets the threshold and for the hospital that decides whether to
accept it.

## The two risks

Both are counted exactly as the evaluation report counts them, so the two never disagree.

| Risk | What is counted | Of what |
|---|---|---|
| Critical misses | Cases of a critical label that are automated, so no person sees them. The report's row "Critical ... N automated". | That label's cases (the default), or all cases. |
| False automation | Cases automated with a wrong answer. The report's row "False automation". | The cases labelled for the primary question. |

A critical label is one the manifest lists under `evaluation.critical`. A target on critical
misses holds for each critical label on its own, so a rare label cannot hide behind a common
one.

## What every evaluation reports

With no target, the report still says what the agent's own threshold lets through, each risk
with its exact upper bound:

```bash
mendari eval mendari/portal-message-agent --dataset test
```

```text
Operating point
  At the agent's automation threshold 0.90, 326 of 600 cases are automated: critical
    urgent_symptoms=yes automated 9 of 114 critical cases (at most 13.4%), 9 of all 600 cases (at
    most 2.7%); false automation 9 of 600 labelled cases (at most 2.7%).
  64 of the 600 cases repeat another case's input. Each counts as its own evidence here, so the
    bounds are narrower than the distinct cases alone would support.
  Each "at most" is the exact one-sided bound (Clopper-Pearson) of its count at 95% confidence. It
    is about cases like these, labelled as these are, and about this version and configuration of
    the agent.
```

Read "9 of 114 critical cases (at most 13.4%)" as: 9 of the 114 urgent messages in this set
were automated, and with 95% confidence the true share is no more than 13.4%. A bound is
rounded up, never down. It is exact as long as the threshold was not picked by looking at these
same cases; if it was, ask for a recommendation, which accounts for the choosing.

The line about repeated cases is there because this set has them. A case whose input is the
same as another's is not new evidence, and the report says how many there are.

## Asking for a recommendation

Give one target or both, and the confidence you want:

```bash
mendari eval my-clinic/my-agent --dataset reviewed-cases.jsonl \
  --target-critical-miss 1% --target-false-automation 2% --target-confidence 95%
```

| Option | Meaning |
|---|---|
| `--target-critical-miss SHARE` | The most critical misses the threshold may leave, as a share: `1%` or `0.01`. |
| `--target-critical-miss-of` | What that share is of: `critical`, each critical label's cases (the default), or `all`, all cases. |
| `--target-false-automation SHARE` | The most false automation the threshold may leave, as a share of the labelled cases. |
| `--target-confidence LEVEL` | The confidence everything is stated with. The default is `95%`. |

A target is advice: it never fails a run, whatever the report finds. To fail a run on what it
measured (in CI, say), state a limit instead, or as well: a limit names the same two figures
and is held against this run's own counts
([Evaluate an agent on every change](evaluate-in-ci.md#limits)).

The report then recommends the lowest threshold, the one that automates the most, at which
every target is met with that confidence:

```text
Operating point
  Recommended automation threshold 0.93 (now 0.90, which these cases do not support): critical
    misses at most 2%, 95% confidence, 63.3% automated.
  At 0.93, 380 of 600 cases would be automated: critical urgent_symptoms=yes automated 0 of 200
    critical cases (at most 1.5%), 0 of all 600 cases (at most 0.50%); false automation 0 of 600
    labelled cases (at most 0.50%).
  At the agent's automation threshold 0.90, 383 of 600 cases are automated: critical
    urgent_symptoms=yes automated 3 of 200 critical cases (at most 3.9%), 3 of all 600 cases (at
    most 1.3%); false automation 3 of 600 labelled cases (at most 1.3%).
  The walk down from 1.00 stopped at 0.92, which cannot be certified: 1 of 200 critical cases
    (urgent_symptoms=yes) is automated (at most 2.4%).
  Guarantee: with 95% confidence, at the automation threshold 0.93 critical misses stay at or below
    2% of critical cases, provided the cases the agent will see resemble these, the labels are
    right, and the agent, its configuration and its version do not change; after any of those
    changes, evaluate again.
  To adopt it, change one line of the agent's manifest, under `policy.thresholds`: `auto: 0.93` (now
    0.90). Nothing changes it for you.
  Each "at most" is the exact one-sided bound (Clopper-Pearson) of its count at 95% confidence. It
    is about cases like these, labelled as these are, and about this version and configuration of
    the agent.
```

When the manifest's own threshold already meets the targets and no certified threshold
automates more, the report says the threshold is supported and there is nothing to change.

`mendari eval --json` holds the same as data, in the report's `run.operating_point`
([the block's format](../spec/evaluation-reports.md#the-operating-point-mendarioperating-point1)),
and `mendari eval --markdown` writes it for a CI job's summary, which is where
[Mendari's action](evaluate-in-ci.md#a-target) shows it. The console shows the section under
each evaluation report, and a Decision Card shows the bound at the threshold its benchmark ran
with.

## What the recommendation means

**It is what your policy would do.** For each threshold, the policy is applied again to the
answers the evaluation recorded, with only `policy.thresholds.auto` changed. Rules, labels that
are never automated, risk caps and provider failures decide as they did. The agent is not
called again. A label that has a threshold of its own keeps it.

**It is honest about trying many thresholds on the same cases.** The thresholds tested are
fixed before a case is seen: 1.00, 0.99 and so on down to the review threshold. Starting from
the strictest, a threshold is certified when each targeted risk's exact upper bound is within
its target. The walk stops at the first threshold that cannot be certified, and the last
certified one is the lowest the cases support. Because a threshold is reached only through
every stricter one, trying many of them does not weaken the guarantee: the chance that the
recommended threshold's true risk is above its target is at most 5% at 95% confidence, however
many thresholds were tried.

**"95% confidence" is about the method.** Of 100 evaluations made this way, on 100 sets of
cases drawn like yours, 5 at most on average would recommend a threshold whose true risk is
above the target. It is not a 95% chance that this particular threshold is safe.

**The guarantee is about the target.** Beside the recommended threshold's counts the report
prints the bound that was tested, "0 of 200 critical cases (at most 1.5%)" above. That
threshold was chosen on these cases, so what holds there with 95% confidence is that the risk
is within the target, 2%, and not within that tighter number.

**Two targets share the error level.** Each is tested at half of it (97.5% each for 95%
overall), so both hold together. That costs cases: see the table below.

**Where thresholds automate the same cases, the strictest is recommended.** If nothing in the
set lies between 0.70 and 0.50, the set says nothing more for 0.50 than for 0.70.

**A target you did not give is not held.** A threshold chosen for false automation alone may
automate critical cases. The report says so, with their count.

## What it assumes

The guarantee is a statement about cases like the ones evaluated. It holds if:

1. **The cases resemble the ones the agent will see.** Same site, same patients, same mix of
   requests, same way of writing. A set collected elsewhere, or written by hand, says less.
2. **The labels are right.** A critical case labelled as routine is not counted as a miss.
3. **The agent, its configuration and its version do not change.** A new model, a new prompt, a
   changed rule or a new version of the agent is a different agent.
4. **Each case is its own evidence.** A case that is in the set twice counts twice. The report
   says how many cases repeat another's input, and `mendari data coverage` lists them.

After any of these changes, evaluate again. A recommendation does not carry over.

## When no threshold can be certified

The report says why, and what would change it.

**Too few cases.** A target needs a minimum number of cases even when none is automated:

```text
Showing at most 1% with 95% confidence needs at least 299 critical cases (urgent_symptoms=yes)
with none automated; this set has 114.
```

**The threshold cannot fix it.** If the agent misses critical cases at any confidence, every
threshold that automates anything automates some of them:

```text
The threshold cannot fix this: at 0.95, the strictest threshold that automates any case, 3 of 114
critical cases (urgent_symptoms=yes) are automated (2.6%, above the 1% target), and every lower
threshold is above it too. What can: the agent itself, a rule that sends such cases to a person,
or a second signal.
```

A rule in the manifest's policy can send every case with a given answer to a person, whatever
the confidence. A second signal is another check on the same case, for example a second model
or a keyword screen, that only ever sends more cases to a person.

**The policy automates nothing.** At risk levels 2 and 3 every decision goes to a person unless
your organization records a governance approval, and at level 4 always. A rule, or a label that
is never automated, can do the same to every case of a set. With nothing automated at any
threshold there is no threshold to recommend, and the report says so instead of asking for
more cases.

## How many cases a target needs

The fewest cases that can show a risk is within its target, when none of them has it. For a
target on critical misses these are cases of each critical label; for false automation, cases
labelled for the primary question.

| Target | 90% confidence | 95% confidence | 95%, two targets | 99% confidence |
|---|---|---|---|---|
| at most 5% | 45 | 59 | 72 | 90 |
| at most 2% | 114 | 149 | 183 | 228 |
| at most 1% | 230 | 299 | 368 | 459 |
| at most 0.5% | 460 | 598 | 736 | 919 |
| at most 0.1% | 2302 | 2995 | 3688 | 4603 |

At 95% confidence the rule of thumb is three divided by the target: 300 cases for 1%.

Every case that does have the risk raises the number. At 95% confidence:

| Target | none | 1 case | 2 cases | 5 cases |
|---|---|---|---|---|
| at most 5% | 59 | 93 | 124 | 208 |
| at most 2% | 149 | 236 | 313 | 523 |
| at most 1% | 299 | 473 | 628 | 1049 |
| at most 0.5% | 598 | 947 | 1258 | 2100 |
| at most 0.1% | 2995 | 4742 | 6294 | 10511 |

So a 1% target on critical misses needs about 300 critical cases, not 300 cases. If one case in
five is critical, that is 1,500 cases.

## At a hospital: run it again on your own cases

A publisher's recommendation is evidence about the publisher's cases. Your patients, your
portal and your staff are not theirs, and the first assumption above is the one that fails
most often. Before an agent decides anything alone at your site:

1. Run it in shadow on real work: every decision goes to a person, and the record keeps what
   the agent would have done.
2. Export the reviewed cases as a labelled set, with the critical question labelled too. A case
   counts as critical only if its labels say so.

   ```bash
   mendari feedback export my-clinic/my-agent --source reviews --out reviewed-cases.jsonl
   ```

3. Evaluate on that set with your own targets:

   ```bash
   mendari eval my-clinic/my-agent --dataset reviewed-cases.jsonl --target-critical-miss 1%
   ```

4. Set the threshold the report supports. If it supports none, the report says how many cases
   are missing or what the threshold cannot fix.

Run it again when the agent, its model or its configuration changes, and when your case mix
does. [Evaluate an agent at your site](evaluate-at-your-site.md) covers the run itself and how
to share its counts.

## The method, for the record

- Bounds are Clopper-Pearson, exact and one-sided: the upper bound of k of n at confidence c is
  the Beta(k + 1, n - k) quantile at c. Certifying a threshold is the exact binomial test of
  "the risk is above the target" at error level 1 - c.
- The walk is fixed-sequence testing (Bauer 1991), as used to pick a model's operating point
  with a risk guarantee in "Learn then Test" (Angelopoulos, Bates, Candès, Jordan and Lei,
  2021).
- With two targets the error level is split equally between them. Each critical label is tested
  at its target's level and all must pass.
- The block is `mendari.operating-point/1`, kept in the report's `run` and not in its result:
  `result_sha256`, the ledger and every signed record are unchanged by it.
