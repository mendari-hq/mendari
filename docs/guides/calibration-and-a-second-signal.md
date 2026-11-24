# Calibration and a second signal

An agent's manifest automates a decision from a confidence (`policy.thresholds.auto`). For an
external agent that confidence is a number the agent reports about itself, and a language model
that says 0.97 is often right four times in five. This page is about making that number mean
what it says, and about two checks that send an unstable answer to a person:

- **calibration**: `mendari calibrate` measures how often the agent is right at each confidence
  it states, and corrects the confidence before the policy sees it;
- **repeated runs**: ask each case several times, and never automate when the runs disagree;
- **a second model**: answer with one variant, check with another, and never automate when
  they disagree.

The first changes the number. The other two never do: they can only turn an automated decision
into one a person reviews. The reasons behind each rule are in
[ADR-0022](../decisions/ADR-0022-a-checked-confidence-and-a-second-signal.md), and the manifest
keys in [spec 0.4](../spec/agent-manifest-0.4-second-signal.md). All three need
`spec_version: "0.4"`.

This page is for the author of an agent and for the hospital deciding whether to trust its
automation.

## What calibration is, and is not

A confidence is calibrated when, of all the answers stated at 0.9, nine in ten are right.
`mendari eval` has always measured this (the row "Calibration", and the reliability table in
the JSON report). Calibration fixes it: for each question, one number T (a temperature) that
stretches or squeezes the agent's confidence toward what its answers were actually worth.

What it is:

- **One parameter per question**, fitted on labelled cases. A few hundred cases cannot be
  overfitted by one number.
- **Monotone.** A more confident answer stays more confident than a less confident one, and the
  agent's answer is never changed: only how sure the policy takes it to be.
- **For one version of one agent.** The file records the agent's id and the version its
  service reported while it was asked.
- **Part of the agent.** The fit is a file in the agent's folder, so its digest, its archive
  and its signature cover it. A hospital that approves a digest approves that calibration.

What it is not:

- **Not accuracy.** A calibrated agent is wrong as often as before. It now says so: fewer
  decisions clear the threshold, and the ones that do are right about as often as claimed.
- **Not a cap.** A temperature under 1 raises a confidence. An under-confident agent is
  automated more after calibration, and that is correct.
- **Not a guarantee about your patients.** It describes the cases it was fitted on. Fit it on
  cases like the ones the agent will see, and evaluate it on other cases.
- **Not a substitute for choosing the threshold.** After calibrating, run
  `mendari eval --target-false-automation ...` again
  ([choosing a threshold](choosing-a-threshold.md)): the old threshold was chosen on the old
  numbers.

## A worked example

A synthetic agent that routes portal messages (`mendari new --agent`), behind a service that
over-states its confidence. It has two labelled sets: `calibration` (400 cases) to fit on and
`test` (600 cases) to report on. Every figure below is the tool's own output.

### 1. Before: what the threshold was cutting on

```bash
mendari eval ./portal-agent --dataset test
```

```text
Request accuracy          449 of 600 [71.2-78.3%]   macro-F1 0.533
Calibration               ECE 0.126   equal-mass ECE 0.126   Brier 0.421
Confidence                reader/agent: not calibrated
Outcome auto              377 of 600 [58.8-66.7%]
Outcome review            223 of 600 [33.3-41.2%]
Automated, correct        304 of 377 [76.3-84.5%]
False automation          73 of 600 [9.7-15.1%] of labelled cases
```

The row "Confidence" says the agent's confidence was never checked. At the threshold 0.90 it
automates 377 cases and gets 73 of them wrong.

### 2. Calibrate

The agent's service must be live (`MENDARI_AGENT_URL`, or the agent's connection in the
console): canned answers are not the agent's confidence, and are refused.

```bash
mendari calibrate ./portal-agent --slot reader --variant agent --limit 0
```

```text
asking starter-portal-agent about 400 calibration cases...
  urgent_symptoms: not fitted (its 400 answers place no temperature: too few of them are wrong, or too few are right)
  request: T=1.66 on 400 cases (NLL 0.7571 -> 0.6703, ECE 0.1246 -> 0.0269 on the same cases)
cost: $0.4
fitted for starter-portal-agent version 2.1.0: when its service reports another version, nothing is rescaled and no decision is automated until it is calibrated again
-> models/reader-agent-calibration.json
add `calibration: models/reader-agent-calibration.json` to the reader/agent variant to use it (it needs spec_version: '0.4')
```

T above 1 means the agent was over-confident. `--dataset` defaults to the agent's `calibration`
set and `--limit` to 200 cases (0 is all of them). A question needs 100 labelled cases the
agent answered. `urgent_symptoms` was not fitted: the agent was right on all 400, and an agent
that is never wrong gives nothing to measure its confidence against. It is left as reported.

Then name the file in the manifest:

```yaml
spec_version: "0.4"
providers:
  reader:
    variants:
      agent:
        type: agent
        calibration: models/reader-agent-calibration.json
```

### 3. After: the same cases, the honest number

```bash
mendari eval ./portal-agent --dataset test
```

```text
Request accuracy          449 of 600 [71.2-78.3%]   macro-F1 0.533
Calibration               ECE 0.039   equal-mass ECE 0.036   Brier 0.396
Confidence                reader/agent: calibrated on 400 cases of calibration (2026-11-24) for starter-portal-agent version 2.1.0
Outcome auto              88 of 600 [11.9-17.8%]
Outcome review            512 of 600 [82.2-88.1%]
Automated, correct        82 of 88 [85.7-97.5%]
False automation          6 of 600 [0.4-2.2%] of labelled cases
```

and, under the report:

```text
Confidence
  reader/agent: calibrated on 400 cases of calibration (2026-11-24) for starter-portal-agent version 2.1.0
    urgent_symptoms: not calibrated (its 400 answers place no temperature: too few of them are wrong, or too few are right)
    request (n=600): ECE 0.126 -> 0.039   equal-mass ECE 0.126 -> 0.036   Brier reliability 0.0084 -> 0.0024
      stated   before: cases   right  after: cases   right
      0.5-0.6              0                   120   65.0%
      0.6-0.7              0                   103   65.0%
      0.7-0.8            120   65.0%           102   67.7%
      0.8-0.9            103   65.0%           187   81.8%
      0.9-1.0            377   80.6%            88   93.2%
```

Read the table by row. Before, 377 answers were stated at 0.9 or more and 80.6% of them were
right. After, 88 are, and 93.2% of them are right. The accuracy did not move (449 of 600): the
agent answers as it did. What moved is which answers the policy trusts. Automation fell from
377 cases to 88, and false automation from 73 cases to 6.

That is a large loss of automation, and it is the true size of what this agent could safely do
at 0.90. If it is too little, the remedies are a better agent or a threshold chosen again on
the calibrated numbers, not the uncalibrated ones.

The figures are measured on `test`, which the fit never saw. Run on the fitting cases
themselves, the report says so:

```text
    these are the cases it was fitted on, so the figures after it are optimistic: evaluate on a set of its own
```

`mendari calibrate` also says when it is given one of the agent's evaluation sets instead of a
set kept for fitting.

### 4. The Decision Card

```text
## Confidence

- `reader` / `agent`: confidence **calibrated** on 400 cases of `calibration` on 2026-11-24, for `starter-portal-agent` version `2.1.0` (questions: `request`). When the agent's service reports another version, no decision is automated until it is calibrated again.
  - `urgent_symptoms`: not calibrated (its 400 answers place no temperature: too few of them are wrong, or too few are right)
```

An agent with no calibration says so in the same place:

```text
- `reader` / `agent`: **not calibrated**. Its confidence is the agent's own report, and the automation threshold cuts on it as reported.
```

## When to recalibrate

**When the agent changes.** This one is enforced. The calibration is for the version the
service reported. When the service reports another version, or none, nothing is rescaled and
every decision that would have been automated goes to a person, with the reason on the record:

```text
"reasons": [
  "confidence 0.90 >= auto threshold 0.90",
  "the agent changed since its confidence was calibrated (was 2.1.0, is 2.2.0); a person decides"
]
```

No key of the manifest turns this off and no governance approval lifts it. Calibrate the new
version, ship the new file (a new version of the agent, approved where digests are approved),
and automation returns. So an agent's service should change its `agent_version` whenever its
model or its prompt changes, and plan a calibration into each release. A service that reports
no version cannot be calibrated.

**When the cases change.** Not enforced, because the engine cannot see it: a new clinic, a new
kind of message, a season. The sign is in `mendari eval` on recent labelled cases: the
"after" ECE creeping up, or the top row of the reliability table falling under what it states.
Evaluate on a schedule ([evaluate in CI](evaluate-in-ci.md)) and refit when it drifts.

**When you have more labelled cases.** A question that was "not fitted" for want of cases, or
of wrong answers, can be fitted later.

## A second signal: what each costs and buys

A calibrated confidence is still the agent's opinion of itself. A second signal asks a
different question: is this answer stable? It cannot show that an answer is right. Three runs
that agree, or two models that agree, can agree on a wrong answer, so agreement changes
nothing: no confidence is raised and no cap is lifted. Disagreement sends the decision to a
person.

A disagreement counts on a question the decision turns on: the primary question, a critical
question (`evaluation.critical`), a question a policy rule reads, and the questions any of
those is derived from. On any other question it is noted in the answer's evidence.

### Repeated runs

```yaml
      agent:
        type: agent
        repeat: 3        # 2 to 5; also on an llm variant
```

The answer is the majority's. Its confidence is the lowest calibrated confidence among the runs
that agree, so the luckiest run never decides. A run that fails counts as a disagreement.

- **Costs:** N times the calls, the tokens and the price, and N calls made in turn inside the
  decision's one deadline. Every run counts toward the daily budget and the organization's
  quota. With `repeat: 3`, a daily limit of 3,000 calls is 1,000 decisions.
- **Buys:** the cases where the agent does not give the same answer twice. It buys nothing
  from a service that answers deterministically (a temperature of 0, a cache): the runs will
  agree, and that agreement is not evidence.

On the example agent, calibrated, with `repeat: 3`:

```text
Second signal
  reader/agent: each case asked 3 times (1,200 extra calls)
    disagreed on 95 of 600 cases: 77 wrong or critical (caught), 18 right (extra review)
  kept 4 of 600 cases from automation: 2 wrong or critical (caught), 2 right (extra review)
```

Two lines, two different questions. The runs disagreed on 95 cases, and 77 of those were cases
the agent got wrong: disagreement is a good sign of a wrong answer here. But only 4 of the 95
would have been automated, because calibration had already put the rest under the threshold. So
on this agent, after calibration, tripling the calls caught 2 wrong automated decisions and
cost 2 extra reviews. False automation went from 6 cases to 4, not to 0: the other four are
wrong answers the agent gave the same way three times.

Whether that is worth 1,200 calls per 600 decisions is a judgement the report now lets you
make with numbers. The same signal on an uncalibrated agent, or at a lower threshold, keeps
many more cases from automation.

### A second model

```yaml
      checked:
        type: check
        members:
          agent: {type: agent, agent_id: portal-agent, state_fields: [message.body]}
          second: {type: llm, state_fields: [message.body]}
```

The first member answers; the second checks. The slot's answer, confidence and distribution
are the first member's, exactly. Where the second gives another answer, the reason names both:

```text
agent and second disagree on request (agent: refill, second: appointment); a person decides
```

- **Costs:** one more call per decision, to a second service with its own price and latency,
  and a second place the case's data goes (name its `state_fields`).
- **Buys:** the cases a different reader reads differently. It buys the most when the second
  member has other mistakes: another model family, another prompt, or rules. Two copies of one
  model behind one prompt mostly agree with each other, mistakes included.

A second member that fails is recorded as any composite member's failure is, and the decision
is not automated. A first member that fails fails the slot: the member that checks never
answers in its place.

`check` is not `vote`, `any_of` or `cascade`. `vote` averages the members' distributions,
`any_of` lets the more alarming answer win, and `cascade` asks the second only when the first
is unsure. Use those when you want the second member to change the answer; use `check` when
you want it only to object.

### Canned answers

An agent that answers from its `mock_file` (no live service set) gives the same answer every
time. A repeated variant asks it once and says so, and the evaluation reports it instead of
claiming agreement:

```text
Second signal
  reader/agent: each case asked 3 times (0 extra calls)
    not checked on 12 cases: their answers are canned and cannot vary, so no agreement is claimed
  kept 0 of 12 cases from automation
```

Canned answers are not calibrated either, and on a server with sign-in they never automate.

## Which to use

1. **Calibrate first.** It is one command, costs one call per fitting case once, and makes
   every other number on the page mean something. An uncalibrated agent says "not calibrated"
   on its card and in every evaluation.
2. **Choose the threshold again** on the calibrated numbers.
3. **Add a second signal if the evaluation shows it earns its calls.** Run the evaluation with
   it on and read the two lines: what it caught, what extra review it cost. Keep it where a
   wrong automated decision is expensive; drop it where it catches nothing.

All three leave the operating point exact: `mendari eval --target-...` works out what another
threshold would do by applying the policy again to each case, caps included, so a recommended
threshold already accounts for the decisions a second signal or a changed version holds back.

## Reference

| What | Where |
|---|---|
| Fit a calibration | `mendari calibrate <agent> --slot <slot> --variant <variant> [--dataset calibration] [--limit 200]` |
| The file | `models/<slot>-<variant>-calibration.json` in the agent, `mendari.agent-calibration/1` |
| Use it | `calibration: <file>` on the `agent` variant |
| Repeated runs | `repeat: <2 to 5>` on an `agent` or `llm` variant |
| A second model | a variant of `type: check` with two `members`: the first answers, the second checks |
| In a report | `run.calibration` and `run.second_signal` (JSON); the row "Confidence" and the sections under the report (text); `--markdown` for a CI summary |
| In a record | the decision's `reasons`, and each answer's `evidence` (`calibrated T=1.66`, `repeat: 2 of 3 runs agreed`, `check: second agrees`) |
