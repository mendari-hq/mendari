# The trust core, to the state of the art

Started 2026-11-27, at the owner's request ("let's make the trust core SOTA, starting with what
you recommended; review the recommendation again to see if there are other things to add").
This note is the reviewed recommendation and the order of work. Each wave has its own decision
record and its own pull request; the plan and status are in `tasks/todo.md`.

## What the trust core is

The machinery that decides when an agent may act on its own, and keeps proving that decision
still holds: a confidence that is checked and tied to a service version (ADR-0024), an
automation threshold with a stated guarantee (`evals/operating_point.py`), a second signal that
only lowers automation, a judge of written output believed only as far as it agrees with
clinicians (ADR-0025), and monitoring after deployment. The harness makes the evidence, the
wrapper enforces it on every decision, and monitoring checks that it stays true.

## Where it stood (read from the code at `c25b64b8`, 2026-11-27)

| Part | What exists | What is missing |
|---|---|---|
| Written output | Clinicians grade blind, a judge grades every text, a per-criterion gate withholds unvalidated figures | Nothing looks at a text at run time: an AUTO decision releases it, and its `write`/`contact` actions, unread |
| Monitoring | Weekly windows against the benchmark with exact intervals, drift, override rate | It re-tests a rolling window every day with no correction; it alerts and nothing else; alerts are not de-duplicated or chained; the daily job skips an organization's store versions |
| Evidence on what is automated | Outcomes and thumbs, when someone gives them | No random check of automated decisions: an AUTO decision cannot even be reviewed. "Reviewed cases are not a sample of what an agent sees" (`evals/feedback.py`) |
| Inside the agent | The answer, proposed actions, usage, the names of its tool calls | No steps: what it looked up, in what order, how long each took |
| Statistics | Exact binomial bounds, exact McNemar, fixed-sequence testing | Every bootstrap is the percentile one (slightly liberal on small samples, ADR-0025); a judged figure is all or nothing |
| Calibration | Fitted on a labelled set, tied to the service version | Nothing learns from reviewed decisions; the reported distribution is not kept, so it could not |
| Real outcomes | Synthetic data everywhere | No figure has been seen to hold on a real site's cases |

## The reviewed recommendation

The first recommendation had four items. Reading the code and the current research added four
more (marked **new**); none was dropped.

1. **new: no free text is released unread.** Found by the review of the judge gate's first
   design (`trust-core-judge-gate-draft.md`): a held text still went out when a reviewer
   confirmed the label, an agent could move its words into an action's arguments, and a
   `contact` action with a free-text message was released with AUTO. So first: a text is
   handed only to a person (an automated decision returns none of what its agent wrote),
   free-text arguments need a person, a held text is kept and shown to its reviewer, and free
   text is released by name. Nothing statistical.
   ADR-0027. *Wave T1, being built.*
2. **A judge clears a text at run time** (was item 2), with **new: a stated guarantee**: the
   share of cleared texts a clinician would fail is bounded (exact, 95%), as the operating
   point bounds false automation. It needs what the review listed: a round the server runs on
   the organization's own connection and on cases like those that would automate; the writer,
   the judge and their settings bound; a design fixed before grades exist; three graders.
   Prior work: learn-then-test and conformal risk control applied to release decisions; the
   clinical work stresses that the unattended release of a failing case is the error that
   matters, and that the count of failures seen limits what can be claimed. *Wave T2.*
3. **new: spot checks.** A random, unpredictable share of cleared texts and of automated
   decisions goes to a person, blind to why, and the verdict is recorded (T1's verdicts are
   the measure). Without it, monitoring is blind exactly where the agent acts alone, and no
   later evidence corrects a guarantee. The research is blunt on this: the share of human
   labels cannot go to zero without losing valid inference, however good the judge. *Wave T2
   for texts, T3 for labels.*
4. **The loop closes** (was item 4). A confirmed alert puts automation on hold by itself; a
   person lifts it. ADR-0010 and ADR-0019 left this as the owner's question, and the owner has
   now answered it. *Wave T3.*
5. **new: monitoring that stays valid when checked every day.** A daily re-test of a rolling
   window has no bound on false alarms over a year, which an automatic hold cannot be built
   on. Confidence sequences (anytime-valid bounds) against the benchmark less the tolerance give
   one: the hold fires wrongly with a stated probability over the whole deployment. *Wave T3.*
6. **The steps inside an agent** (was item 3). The contract takes a trace (steps, tools,
   timings), kept as inputs are, and evaluations and policy rules can read it. *Wave T4.*
7. **new: sharper statistics.** Prediction-powered inference for judged figures (a valid
   interval from many judge grades and a few clinician grades, always, where today the figure
   is withheld or shown); a bias-corrected interval where the percentile bootstrap is liberal;
   adjudication where two clinicians differ. *Wave T5.*
8. **Real outcomes** (was item 1). A design-partner site and clinician labels. The owner's; no
   engineering replaces it. Waves T1 to T4 are what make a site's evidence cheap to collect
   and hard to fool.

Deferred, with the reason: **recalibration from reviewed decisions.** A calibration can raise a
confidence, so "only lowers automation" does not cover it; a refit is a new digest and resets
approvals; and the distribution a service reported is not stored. It becomes a proposal a
person promotes, after T2 keeps what it needs.

## Sources consulted (2026-11-27)

- Prediction-powered inference for model-judge evaluation, and its limits:
  [GLIDE](https://arxiv.org/pdf/2605.31278),
  [bias and uncertainty in judge estimation](https://arxiv.org/pdf/2605.06939),
  [adaptively collected human feedback](https://arxiv.org/pdf/2611.13186),
  [anytime-valid inference from model annotations with human auditing](https://arxiv.org/pdf/2611.15197).
- Anytime-valid monitoring of deployed models:
  [statistically valid post-deployment monitoring for digital health](https://arxiv.org/html/2506.05701v2),
  [WATCH](https://arxiv.org/html/2505.04608v3),
  [anytime-valid calibration monitoring](https://arxiv.org/html/2603.13156),
  [online detection of relevant deviations](https://arxiv.org/abs/2309.15187).
- Risk control for released model output:
  [CARE, a conformal safety layer for medical summarization](https://arxiv.org/html/2606.08969),
  [when conformal risk control can certify model outputs](https://arxiv.org/html/2606.29054v2),
  [release-side risk in conformal triage](https://arxiv.org/pdf/2605.20956),
  [risk-constrained stopping for clinical diagnosis agents](https://arxiv.org/html/2609.09678).

Most are preprints; they were read for the method, not for a figure.

## Wave T1: no free text is released unread (ADR-0027)

Built on branch `trust/t1-written-gate`, test first: three core parts in parallel worktrees,
combined, then the console and the words.

| Part | Files | What |
|---|---|---|
| A1. A text is handed only to a person | `core/decision.py`, the two places an answer carried texts | `DecisionResult.drafts()` and `written`: the draft goes back only with a decision that awaits a person. (The first build sent every decision carrying a text to a person; measured, it stopped the demo agent automating anything, and was replaced.) |
| A2. Free-text arguments | `core/spec.py`, `core/policy.py`, the actions fixture | `ActionSpec.free_text` (already on the branch); `gate_of(free_text=...)`; `action_stops` |
| A3. Kept texts and the review | `core/audit.py`, `core/actions.py`, `server/app.py` (review routes) | `output.held` with the text as its note; the review's verdict on each text, `output.resolved`; free text released by name; retention, erasure and `verify` |
| B1. The console | `dashboard/review.js` and its tests | The held texts in the review, a verdict on each, free-text actions released by name |
| B2. The words | `docs/`, `core/card.py`, `core/packet.py`, `CHANGELOG.md`, `CLAUDE.md` | The spec (sections 13 and 15, the policy's steps), the guides, the threat model, the Card and the packet |

Then one engineer who built none of it reviews the combined change, the whole suite runs on
SQLite and Postgres with the browser suite, and it merges as one pull request.

### Found while the combined change was checked (2026-11-28)

- **A review released an action its reviewer never saw.** `POST …/actions` on a decision that
  waits for review holds the action with the rest, and a label confirmed from a queue read a
  moment earlier released it. The decision's own actions are chained with the decision, so a
  reviewer who sees the decision sees them; one asked for afterwards (`source: request`) is
  now released only by name. This is the same rule as for free text, for the same reason: a
  label speaks only for what its reviewer was shown. What it leaves: a caller that asked for
  an action knows its id and can name it for a reviewer it never showed it to (threat model
  E26). A review that says what it was shown (the queue as of an event) was considered and
  not built: the action's own record already tells the two cases apart.
- **The count beside "Review queue" read the queue itself**, on every view, so under PHI mode
  `encrypt` the chain recorded a read of up to 500 inputs (and now held texts) per page
  change. The read log is only evidence if a read is a read: the count now comes from
  `/api/review/summary`. Predates the wave; `server/origin.py` already named the cost of a
  queue read.
- **A CI job that no local test runs whole can break unseen.** `mendari sandbox pilot-check`
  resolved its review with a label alone, on the demo agent, whose every decision carries a
  text. The local suite passed; the job would have failed on the pull request. The lesson is
  in `tasks/lessons.md`: before a pull request, grep the product for every caller of a route
  or a store method whose contract changed, scripts and sandbox commands included, and run
  the ones CI runs in a container.
- **An upgrade does not reach back.** A decision recorded by 0.1.2 that still waits has no
  held text and no `free_text` mark on its actions, so it is reviewed under 0.1.2's rules.
  The record names no engine version, so nothing tells such a decision apart; the changelog
  says to work the queue down first. If a real deployment ever upgrades with a queue that
  matters, the stricter choice is to chain `free_text` on every proposal (true or false) and
  to treat its absence as free text.
