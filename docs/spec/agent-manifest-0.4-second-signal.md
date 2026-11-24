# Agent manifest spec 0.4: a calibrated confidence and a second signal

Status: draft. A section of spec 0.4, written apart so that it can be folded into the 0.4 page.
It adds three things to a provider variant and nothing else: `calibration` on an `agent`
variant, `repeat` on an `agent` or an `llm` variant, and the composite type `check`. A manifest
that uses any of them MUST say `spec_version: "0.4"`; an engine MUST refuse it otherwise, naming
each place it is used:

```text
['providers.reader.variants.agent.repeat'] use spec 0.4 features; set spec_version: '0.4'
```

Every other part of an agent is as [0.3](agent-manifest-0.3.md) and
[0.2](agent-manifest-0.2.md) say. The key words MUST, SHOULD and MAY are used as in RFC 2119.
The reasons for each rule are in
[ADR-0022](../decisions/ADR-0022-a-checked-confidence-and-a-second-signal.md); the reference
implementation is `mendari.providers.agent_calibration`, `mendari.providers.repeat`,
`mendari.providers.check` and `mendari.core.policy.lowered`.

## 1. What a cap is

Each feature below can find a reason that a decision may not be automated. Such a reason is a
**cap**. After the policy of 0.2 section 6 has decided, a runtime MUST apply the caps the
decision's providers found:

- a decision that is `AUTO` becomes `HUMAN_REVIEW`, and each cap is added to its reasons,
  followed by `; a person decides`;
- a decision that is not `AUTO` keeps its outcome, recommendation, priority and rule, and each
  cap is added to its reasons as it is.

A cap MUST NOT be lifted by a governance override, by the risk level, or by any key of the
manifest, and a runtime MUST NOT offer a manifest key that disables one. No feature of this
section can make a decision `AUTO` that the policy alone would not have.

A **deciding question** is the primary question, a question named in `evaluation.critical`, a
question a policy rule's `when` reads, or a question any of those is derived from. A
disagreement (sections 3 and 4) is a cap only on a deciding question; on any other question it
is written in the answer's evidence.

## 2. `calibration` on an `agent` variant

```yaml
providers:
  reader:
    answers: [request, urgent_symptoms]
    use: agent
    variants:
      agent:
        type: agent
        agent_id: portal-agent
        state_fields: [message.subject, message.body]
        calibration: models/reader-agent-calibration.json
```

`calibration` names a file of the agent, so the agent's digest covers it. The file is
`mendari.agent-calibration/1`:

```json
{
  "format": "mendari.agent-calibration/1",
  "agent": {"id": "portal-agent", "version": "2.1.0", "model": null},
  "method": "temperature",
  "temperatures": {"request": 1.66},
  "questions": {"request": {"cases": 400, "nll_before": 0.7571, "nll_after": 0.6703,
                            "ece_before": 0.1246, "ece_after": 0.0269}},
  "not_fitted": {"urgent_symptoms": "its 400 answers place no temperature: ..."},
  "fitted_on": {"dataset": "calibration", "sha256": "…", "cases": 400, "date": "2026-11-24"},
  "failures": {},
  "cost_usd": 0.4
}
```

A reader of the file MUST find `format`, `agent.version` (a string), `temperatures` (each a
positive finite number, by question) and `fitted_on` with `dataset`, `cases` and `date`. A file
without them is not a calibration, and a variant that names it MUST NOT run. The other keys are
a record of the fit.

**The map.** For a question with a temperature T, the distribution p the provider would have
given becomes p'(label) proportional to p(label)^(1/T), and the answer's confidence is p' of
the answer's own label. When the agent's response gave only a confidence c for its answer over
K labels, p is c for that label and (1 - c) / (K - 1) for each other label (0.2 section 5), so
the map scales log(c (K - 1) / (1 - c)) by 1 / T. The answer's label MUST be kept as the agent
gave it; the map keeps the order of the labels, so no other label has a larger p'. A question
with no temperature is left as reported.

**The version.** A runtime MUST compare, for each call, the variant's `agent_id` and the
`agent_version` in the agent's response with `agent.id` and `agent.version` of the file.

- Equal: the map is applied.
- Not equal, or the response has no `agent_version`: the map MUST NOT be applied, and the
  decision carries the cap
  `the agent changed since its confidence was calibrated (was X, is Y)`, where X is the file's
  version and Y the reported one, or `not reported`. When the two `agent_id`s differ, X and Y
  each give the id and then the version.

Answers read from the variant's `mock_file` are not the agent's confidence: the map MUST NOT be
applied to them, and they carry no version cap.

**The fit** (`mendari calibrate <agent> --slot S --variant V`) asks the live agent about each
case of a labelled dataset once and fits each question's T by minimising the negative
log-likelihood of the true label. A fitter:

- MUST NOT fit on canned answers, on a service that reports no `agent_version`, or on one that
  reported more than one version while it was asked;
- MUST NOT fit a question on fewer than 100 labelled cases the agent answered, and MUST leave a
  question unfitted when no temperature is the best one (the best is at the edge of its search);
- MUST record the dataset's name, its sha256, the number of cases and the date, and the
  `agent_id` and reported `agent_version`;
- SHOULD be given a dataset that no evaluation reports on (an agent's `calibration` set), and
  SHOULD say so when it is given one of the agent's evaluation sets.

## 3. `repeat` on an `agent` or an `llm` variant

```yaml
      agent:
        type: agent
        repeat: 3
```

`repeat` is an integer from 2 to 5. The variant's questions are asked `repeat` times about the
same input, each time by one call exactly as the variant would have made it, and for each
question:

- the answer is the label most of the runs that answered gave. Among labels given equally
  often, the one given by the earliest run;
- its confidence, distribution and evidence are those of the run, among the runs that gave that
  label, whose confidence (after its calibration, section 2) is lowest;
- when fewer than `repeat` runs gave that label, the runs disagreed. A run that failed gave no
  label and MUST count as a disagreement. On a deciding question the decision carries the cap
  `A of N runs agreed on <question>`, followed by ` (F failed: <codes>)` when F runs failed.

When every run gave the label, nothing else changes: agreement MUST NOT raise a confidence.
When every run failed, the slot fails with the first run's error.

Each run MUST be charged to every limit a single call is charged to (a daily budget, an
organization's quota), counted in the decision's usage, and held to the decision's one
deadline; the decision's tokens and cost are the sum of the runs. A run that was sent and did
not answer leaves the decision's cost unknown.

A repeated `llm` variant MUST NOT share a call with another slot (0.2 section 5, batching).

Answers read from a `mock_file` cannot vary. They are asked once; the evidence says
`repeat: asked once, canned answers cannot vary, so no agreement is claimed`; no cap is added
and no agreement is reported.

## 4. The `check` composite

```yaml
      checked:
        type: check
        members:
          agent: {type: agent, agent_id: portal-agent, state_fields: [message.body]}
          second: {type: llm, state_fields: [message.body]}
```

`check` is a composite (0.2 section 5) of exactly two members, in order: the first answers and
the second checks. Members are ordinary variants and MUST NOT be composites.

- The slot's answers are the first member's, unchanged: label, confidence and distribution.
- The second member is asked the same questions about the same input. Where its label differs
  from the first member's on a deciding question, the decision carries the cap
  `<first> and <second> disagree on <question> (<first>: <label>, <second>: <label>)`, the
  members named as the manifest names them.
- Where the labels are equal, nothing changes: agreement MUST NOT raise a confidence.
- A second member that fails is a failed member, as in any composite: `<second>:<code>` is
  recorded on each of the slot's questions, and the policy does not automate (0.2 section 6).
- A first member that fails fails the slot. The second member MUST NOT answer in its place.

Budgets, batching, usage, cost and the deadline reach both members as they reach any
composite's. A member MAY carry `calibration` and `repeat`; its caps hold beside the check's.

## 5. What an evaluation reports

Not part of the manifest, and stated here because a reader of an evaluation meets it. A report's
`run` block MAY hold:

- `calibration` (`mendari.calibration-report/1`): for each `agent` and `llm` variant the run
  decided with, whether it is calibrated and on what; and for the variant whose answers are a
  slot's own, each question's expected calibration error and reliability table on the run's
  labelled cases, before the calibration and after it;
- `second_signal` (`mendari.second-signal/1`): for each repeated variant and each check, the
  cases it disagreed on, how many of those were wrong or critical and how many were right, and
  its extra calls; and how many cases the second signals kept from automation.

Both are outside the result that a report's `result_sha256` covers
([evaluation reports](evaluation-reports.md)): they are worked out from what the run's
decisions kept in memory, as the operating point is.
