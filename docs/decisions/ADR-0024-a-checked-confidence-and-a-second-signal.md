# ADR-0024: A confidence that was checked, and a second signal that only lowers automation

## Status
Accepted. Terminology updated 2026-11-25: the service's fields are `service_id` and
`service_version` ([ADR-0026](ADR-0026-one-release-baseline-agents-only.md), point 6).

Decided by the maintainer on 2026-11-24, before the repository was public, so no comment period ran; open to revision when it is.

Specified in [agent manifest spec 0.4](../spec/agent-manifest-0.4.md), sections 8 to 13. Built
beside [ADR-0023](ADR-0023-an-agent-proposes-actions.md) (actions); point 5 below is where the
two meet.

## Context
The policy automates a decision when the confidence of the answer to the primary question is at
or above a threshold (`core/policy.py`, spec section 6). Everything else the policy does is a
cap on that: a risk level, a label that is never automated, a provider that failed.

For the engine's own model variants that confidence has been checked. A naive Bayes model is
trained with a calibration split, and an `llm` variant can carry a temperature per question
fitted by `mendari calibrate`. For an `agent` variant, the one this product is about, it has
not. The confidence is whatever number the external service reports about itself, and language
models report their confidence badly: an answer stated at 0.97 is often right four times in
five. So the one number the threshold cuts on was the one number nobody had measured.

Two things follow from that, and this record decides both.

1. **The number should be measured and corrected where it can be.** Calibration does that, but
   a calibration is a statement about one agent at one time. An agent's service is outside
   Mendari and changes without the manifest changing: a new model behind the same endpoint, a
   new prompt. A calibration that silently carries over to a changed agent is worse than none,
   because the card then says "calibrated" about numbers it was never fitted on.
2. **One number is one signal.** Even a calibrated confidence is the agent's opinion of itself.
   Two cheap, independent checks exist: ask the same question again and see whether the answer
   holds, and ask a second model. Neither can show that an answer is right. Either can show
   that it is unstable.

The codebase's rule for anything of this kind is already written down: policy only lowers
automation (ADR-0002). The question was how to add a signal without giving any manifest a way
to raise automation through it.

## Decision

### 1. An agent's confidence is calibrated by one temperature per question
`mendari calibrate` works for an `agent` variant as it does for an `llm` variant. It asks the
live agent about each labelled case once and fits, for each question, one temperature T by the
existing fit (`providers/calibration.py`): the reported distribution p becomes
softmax(log p / T).

- When the agent reports a distribution over the labels, that is temperature scaling of it.
- When it reports only its answer's confidence c, the provider has always spread the rest
  evenly over the other labels, and the same formula then scales the log-odds of c against each
  other label, log(c (K - 1) / (1 - c)) / T. For two labels that is the plain log-odds of c.
  The plain log-odds were not used for more labels: a confidence of 0.4 over three labels is
  above chance and has negative log-odds, so sharpening it would push it under the other
  labels' share and change which answer the distribution favours.

One parameter per question cannot overfit a few hundred cases, is monotone, and never changes
which answer wins. A question is fitted on at least 100 labelled cases the agent answered
(`MIN_CASES`), and is left as reported when it has fewer, or when no temperature is the best
one (an agent right on every case can always be sharpened further: the cases do not place its
confidence).

The fit is a file of the agent (`mendari.agent-calibration/1`), named by the variant's
`calibration` key as an `llm` variant's is. So the agent's digest, archive and signature cover
it, a registry's approval is of the agent with that calibration, and changing it is changing
the agent. It records what it was fitted on (the dataset's name and sha256, the number of
cases, the date) and for which agent: the variant's `service_id` and the `service_version` the
service reported while it was asked.

The fitting cases are meant to be separate from the cases an evaluation reports on, as they are
for an `llm` variant: the default set is the agent's `calibration` set. `mendari calibrate` says
so when it is given one of the agent's evaluation sets, for either kind of variant, and an
evaluation run on the very file a calibration was fitted on says its "after" figures are
optimistic. Neither is refused: a site with one small set may still want the fit, and should
see what it is worth.

Calibration is refused where it would not mean anything: on canned answers (they are not the
agent's confidence), on a service that reports no `service_version`, and on one that reported two
versions while it was asked.

It is applied inside the provider, for every call (`AgentProvider.answer`), so before the
policy sees the confidence and on every path a decision is made by: REST, FHIR, CDS Hooks, MCP,
the command line, an evaluation. An evaluation therefore measures what production does.
Canned answers are left alone.

Calibration is a measurement, not a cap. A fitted temperature below 1 raises a confidence, and
an under-confident agent is then automated more. That is the point of measuring. What keeps it
honest is that the fit is in the agent's signed files, that the evaluation reports the
calibration error before and after on cases of its own, and the next decision.

### 2. A calibration fitted for one version does not bless another
When the agent's service reports a version other than the one the calibration was fitted for,
or none, or the variant's `service_id` is not the one it was fitted for:

- nothing is rescaled (the confidence is as reported, and the evidence says "not calibrated");
- the decision is capped: AUTO becomes HUMAN_REVIEW, with the reason
  `the agent changed since its confidence was calibrated (was X, is Y)`.

No key of a manifest switches this off, and nothing lifts it: not a governance approval, not a
risk level that needs none. The way out is to calibrate the agent again.

### 3. A second signal is a cap, and only a cap
Two are added, both spec 0.4.

**Repeated runs** (`repeat: N`, 2 to 5, on an `agent` or `llm` variant). The same question is
asked N times. The answer is the majority's; its confidence is the lowest calibrated confidence
among the runs that agree with it; a run that fails gave no answer, so it counts as a
disagreement, never as agreement. Each run is the provider's own single call, so each is
charged to the daily budget and the organization's quota, counted and priced by the decision's
meter, and held to the decision's one deadline.

**A second model** (`type: check`, a composite of exactly two members). The first member's
answers are the slot's, as it gave them. The second member is asked the same questions and only
its labels are read.

For both: where the runs, or the two members, do not give the same answer to a question the
decision turns on, AUTO becomes HUMAN_REVIEW, with a reason that says how many of the N agreed,
or names the two members and what each answered. Agreement changes nothing: it raises no
confidence and lifts no cap.

"A question the decision turns on" is the primary question, a critical question
(`evaluation.critical`), a question a policy rule reads, and the questions any of those is
derived from. The brief named the first two; rule questions were added because a rule's
question decides the outcome as surely as the primary one, and adding them can only send more
decisions to a person. A disagreement on any other question is written in the answer's evidence
and changes nothing.

Canned answers cannot vary. A repeated variant asks them once and says in the evidence that no
agreement is claimed; a check with a canned member says its agreement is not evidence.

### 4. Where the caps live
A provider reports what it found beside its answers (`providers/signals.py`: `Cap`, a kind and
a reason). The decision collects them into what its policy decided from
(`policy.PolicyInputs.caps`), and `apply_policy` applies them as its seventh step
(`policy.lowered`), after every cap it already had: with any cap, AUTO becomes HUMAN_REVIEW;
every other outcome keeps its outcome, recommendation, priority and rule, and gets the reason.
The reason ends "; a person decides" wherever a person does, so on every outcome but a BLOCK,
which gets it as a note. They are given in one order whatever order they were found in: the
version cap, then repeated runs, then a check. The actions' cap of ADR-0023 is the eighth step
and the last: what the answers are worth is settled before what the agent asks to do.
`apply_policy` is the one function every decision and every replay of one goes through
(`PolicyInputs.apply`), so the operating point, which applies the policy again to what each
case kept, reproduces the run case for case with these features on.

Nothing stored, hashed or signed changes shape. The caps reach the record as reasons. What an
evaluation reports about calibration and second signals is in the report's `run` block, beside
the operating point, outside the result that `result_sha256` covers.

### 5. A second signal that did not agree on a proposed action holds the action
An agent on spec 0.4 may propose actions with its answers (ADR-0023). A second signal treats
them as it treats answers: what the runs did not agree on is not automated.

- **Repeated runs.** The slot proposes each distinct proposal (a name with its arguments) that
  any run made. One that every run made is taken as proposed. One that only some runs made (a
  run that failed made none) is **disputed**: the decision is capped, the reason
  `2 of 3 runs proposed action <name>`, and the action's gate becomes `person`. So it is held
  for the decision's reviewer, and never released without one, whatever its effect: a `read`
  that any other decision would release is held too.
- **A check.** The first member's proposals are the slot's, and the second member's own are
  never taken, as its answers are not. Where the second member also proposes actions (an
  `agent` variant), a proposal it did not make with the same arguments is disputed, and any
  difference between the two caps the decision. A second member that cannot propose (an `llm`)
  checks the answers and not the actions. A `check` is therefore the one composite whose
  proposals are taken; a member of a `vote`, `cascade` or `any_of` still has none taken.

The cap is the second signal's (a reason among `PolicyInputs.caps`), so the evaluation counts
such a case among those its second signal kept from automation, and the operating point
re-applies it. The gate is where every other gate is (`policy.gate_of`).

Why held and not blocked: the action may be the right one, and a reviewer is about to read the
decision anyway. Why not released on a `read`: an agent that reads a chart on one run in three
is doing something its author did not evaluate, and nothing is lost by waiting for the person
who now decides.

## Consequences
- An agent's Decision Card says whether its confidence is calibrated, on how many cases and
  when, or "not calibrated". `mendari eval` says the same in one row, and for a calibrated
  variant prints the expected calibration error and the reliability table before and after.
- Upgrading an agent's service stops its automation until someone calibrates it again and
  ships the new file. That is deliberate and it is a cost: a site that upgrades often will
  review more. A service that reports no version cannot be calibrated at all.
- A second signal costs calls: N - 1 more for each repeated case, one more for each checked
  case, and the latency of making them in turn within one deadline. `mendari eval` reports
  what it bought on the evaluated cases: on how many it disagreed, how many of those the agent
  got wrong or were critical, how many it got right, and how many decisions it kept from
  automation, so the cost can be weighed against a number.
- Repeating a model that answers deterministically proves little, and two members that are the
  same model behind the same prompt prove little. The engine cannot tell; the guide says so.
- A new manifest surface: `calibration` and `repeat` on an `agent` variant, `repeat` on an
  `llm` variant, and the `check` composite, all requiring `spec_version: "0.4"`.

## Alternatives considered
- **A calibration that is simply not used for another version**, as an `llm` variant's is not
  used for another model. Rejected for agents: the decision would then be automated on the raw
  confidence the calibration was added to correct, with nothing on the record to say why the
  agent's behaviour changed. For an `llm` variant the model is the deployment's setting and is
  on the record; an agent's version changes outside Mendari.
- **Enforcing the model the service reports as well as its version.** The model is recorded in
  the calibration file and not enforced. What a version means belongs to the agent's service,
  and a service that swaps models under one version is misreporting its version; enforcing two
  identifiers would cap agents whose gateway renames a snapshot. Open to revision.
- **Letting agreement raise confidence** (the usual self-consistency scoring, or averaging two
  models as `vote` does). Rejected: three runs of one model that agree share that model's
  mistakes, and the evaluation in the guide shows agreed-on wrong answers. A signal that can
  raise automation would also have to be calibrated itself. `vote` remains for authors who
  want an average and will evaluate it as one.
- **Giving `vote`, `any_of` or `cascade` the cap.** None of them keeps the first member's
  answer: `vote` averages, `any_of` takes the more alarming, `cascade` asks the second only
  when the first is unsure. Changing any of them would change decisions of agents that use
  them today. `check` reuses the composite machinery they share and nothing else.
- **Turning a rule's BLOCK into HUMAN_REVIEW under these caps**, as the canned and site-code
  caps do. Not done: a block stops an action, which is the cautious direction, and the brief
  asked for AUTO to be capped. The reason is still added to a blocked decision's record.
- **Isotonic regression or a two-parameter fit.** More flexible, and on a few hundred cases
  more able to fit the sample. One temperature is what the engine's other variants use, so one
  fit and one file format are understood everywhere.
- **Running the repeated calls in parallel.** Faster, and the budget, meter and deadline are
  per-thread context that would have to be carried across. Left for when latency is measured
  to matter.
