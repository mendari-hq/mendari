# ADR-0001: Questions, not models, are the core abstraction of an agent

## Status
Accepted (v0.1)

## Context
Mendari must let the same healthcare decision run on deterministic rules, Jev, local ML
models, LLMs, or combinations of them, and must evaluate and govern all of them the same way.
If a pack's contract were "a model plus its output format", every provider change would
change the contract, the evaluation and the policy.

## Decision
A pack declares **questions**: each has a fixed label set and a human-readable criterion per
label. Providers are grouped into **slots**. A slot answers one or more questions with a
probability distribution over their labels. Slots have interchangeable **variants**. The
**policy** is written only against questions and labels.

The criterion text serves twice. It documents the decision for clinicians in the Decision
Card, and it is the instruction passed verbatim to instruction-following providers such as
Jev.

## Consequences
- Swapping `router=naive_bayes` for `router=jev` changes neither the policy, the tests nor the
  evaluation format. The comparison between variants is apples to apples.
- Every provider must produce a distribution. For rules we use a softmax over pattern weights,
  so "nothing matched" is honestly low-confidence rather than a confident default.
- Calibration becomes a first-class property of providers, because thresholds consume
  confidences.
- Multi-output decisions (e.g. trial prescreening with per-criterion yes/no/unknown) are
  multiple questions in one pack.

## Alternatives Considered
- **Free-form provider output plus per-provider parsers.** Rejected: evaluation and policy
  would be per-provider, and prompt-dependent parsing is a failure mode in itself.
- **A single "label" output per pack.** Rejected: safety screens (urgency) and routing
  (destination) are different questions with different providers and must be evaluated
  separately.
