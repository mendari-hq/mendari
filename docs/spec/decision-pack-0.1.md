# Agent manifest spec 0.1

(formerly the Decision Pack spec; the format is unchanged)

Status: draft. The key words MUST, SHOULD and MAY are used as in RFC 2119. The reference
implementation is `mendari.core.spec` (manifest) and `mendari.core.policy` (semantics).
Where this document and the implementation disagree, file an issue; the document is intended
to be normative.

## 1. Purpose

An agent packages one healthcare decision so it can be executed, evaluated, governed
and shared independently of the models that implement it. An agent MUST be self-describing: a
reviewer can determine what it decides, for whom, with what inputs, under what automation
limits, and how well it performed on which data, without reading code.

## 2. Layout

An agent is a directory containing `pack.yaml`. All paths in the manifest are relative to the
agent directory and MUST NOT resolve outside it.

| Path (conventional) | Required | Content |
|---|---|---|
| `pack.yaml` | yes | Manifest (§3) |
| `policy.yaml` | no | Policy (§6). It may instead be inline in `pack.yaml` |
| `schemas/input.schema.json` | yes | JSON Schema 2020-12 for the agent input |
| `fhir/mapping.yaml` | no | FHIR R4 mapping (§9) |
| `logic/`, `models/` | per provider | Provider configuration and artifacts |
| `data/*.jsonl` | no | Evaluation datasets (§8) |
| `tests/cases.yaml` | SHOULD | Behaviour tests (§10) |
| `evals/benchmark.json` | SHOULD | Published evaluation report (§8) |
| `CHANGELOG.md` | SHOULD | Version history |
| `DECISION_CARD.md` | generated | Rendered Decision Card |

## 3. Manifest

```yaml
spec_version: "0.1"
id: namespace/name            # lowercase, digits, dashes
version: 1.2.3                # semantic version
name: Human-readable name
summary: One or two sentences.
publisher: {name: ..., type: community | verified | institutional, url: ...}
license: SPDX identifier
risk: {level: 0-4, category: ..., rationale: ...}
card:
  intended_use: ...
  intended_users: [...]
  patient_population: ...
  out_of_scope: [...]
  known_failure_modes: [...]
  evidence: [{title, url?, note?}]
inputs: {schema: <path>, fhir: <path>?}
questions: {<key>: {instructions, criteria: {<label>: <criterion>}}}
providers: {<slot>: {answers: [<question>...], use: <variant>, variants: {<variant>: {type, ...}}}}
policy: <inline mapping> | <path>
evaluation: {datasets: {<name>: <path>}, benchmark: <path>, benchmark_dataset: <name>,
             critical: [{question, label}], subgroups: [<attribute>], degradation_tolerance: 0.03}
tests: <path>
audit: {store_input: full | hash}
cds_hooks: {hook, title, description, prefetch: {<key>: <FHIR query template>}}
```

Validation rules (all MUST hold):

- Question keys and labels match `^[a-z][a-z0-9_]*$`. A question has 2–255 labels, each with a
  non-empty criterion.
- Every question is answered by exactly one provider slot. Slots answer only declared
  questions. A slot's `use` names one of its variants.
- `policy.primary` is a question. Rule conditions reference existing questions and labels.
  Rule recommendations and `policy.labels` keys are labels of the primary question.
- `thresholds.review ≤ thresholds.auto`, both in [0, 1].
- Agents at risk level ≥ 2 MUST NOT contain policy rules whose outcome is `AUTO`.
- Every referenced file exists inside the agent.

## 4. Questions

A question is the unit of meaning. Its `criteria` are written for humans (they appear in the
Decision Card) and are also passed verbatim to providers that accept natural-language
instructions (Jev, LLMs). Changing a label set is a breaking change (MAJOR version). Rewording a
criterion changes behaviour for instruction-following providers and SHOULD be a MINOR version.

## 5. Providers

A provider answers every question in its slot with a probability distribution over the
question's labels. The answer's `label` is the most probable label, and `confidence` is the
provider's confidence in it. Providers MUST either answer every question in the slot or raise
a provider error with a stable, audit-safe code (e.g. `timeout`, `rate_limited`,
`credential_unavailable`, `invalid_response`, `invalid_input`). Error codes MUST NOT carry
credentials or remote error bodies.

Variants of a slot are interchangeable implementations of the same contract. Selecting a
variant (`providers={"router": "jev"}`) MUST NOT change the agent's questions, policy or
tests.

Built-in types in v0.1: `rules` (deterministic patterns with negation handling and optional
typo tolerance), `naive_bayes` (local, temperature-calibrated text classifier), `jev`.

## 6. Policy and outcomes

Every decision has exactly one outcome:

| Outcome | Meaning |
|---|---|
| `AUTO` | The action may be taken without a human in the loop |
| `HUMAN_REVIEW` | A human confirms or corrects the recommendation before action |
| `ABSTAIN` | No recommendation; the case goes to the pre-existing manual workflow (`fallback`) |
| `BLOCK` | The action must not be taken (invalid input, or a blocking rule) |

and a priority from FHIR `request-priority`: `routine`, `urgent`, `asap`, `stat`.

Evaluation order (MUST):

1. If the input fails the input schema, the outcome is `BLOCK` and no provider is called.
2. Policy `rules` are evaluated in order over the available answers. The first rule whose
   question's answer has a label in `when.labels` with confidence ≥ `when.min_confidence`
   determines outcome, recommendation (default: the primary answer's label) and priority.
3. Otherwise, on the primary question: confidence ≥ the label's `auto_threshold` (default
   `thresholds.auto`) gives `AUTO`; ≥ `thresholds.review` gives `HUMAN_REVIEW`; else
   `ABSTAIN`. A missing primary answer gives `ABSTAIN`.
4. Caps apply only to `AUTO` and only ever lower it to `HUMAN_REVIEW`: a label with
   `allow_auto: false`; any provider failure in the decision; the risk level (§7).
5. `ABSTAIN` carries no recommendation. `ABSTAIN` and `BLOCK` carry the policy `fallback`.

## 7. Risk levels

| Level | Category | Runtime rule |
|---|---|---|
| 0 | Administrative | AUTO permitted |
| 1 | Clinical information processing | AUTO permitted; decisions MUST be audited |
| 2 | Clinical support | AUTO capped to HUMAN_REVIEW unless a governance override is supplied |
| 3 | Patient-specific prediction | As level 2 |
| 4 | Time-critical / diagnostic / treatment | AUTO always capped; no override |

A governance override is supplied by the *deploying* organization at runtime, never by the
agent. It names an approver and a reference, and it is recorded in every affected decision.

## 8. Evaluation

Dataset format: JSON Lines. Each line has an `input` (the agent input), optional `labels`
(`{question: label}`), optional `attributes` (subgroup metadata such as `language` or
`site`), and an optional `id`.

An evaluation report (`format: mendari.eval/1`) MUST include the agent id, version and
digest; the provider variant and model of each slot; the dataset name, SHA-256 and size; per
question accuracy, macro-F1, per-label precision/recall/F1, confusion matrix, ECE with
reliability bins, and Brier score; outcome rates; automation accuracy and false automation
rate; expected override rate among reviewed cases; for each `critical` label, the detection
count and the number of true critical cases that were automated; latency percentiles; cost;
subgroup metrics for each configured attribute; and a threshold sweep. Reports MUST NOT
contain case inputs.

**Local validation** compares a report on local data with the agent's published benchmark. It
MUST report the overall delta of primary accuracy and SHOULD localize degradation by label
recall, subgroup, calibration and false automation. Any automated true-critical case is always
a finding.

## 9. FHIR and CDS Hooks

`fhir/mapping.yaml` declares the `focus` resource type, the required and optional resource
types, and a mapping from input fields to FHIRPath-subset paths (dotted elements, where a
list-valued element yields all its items and `[n]` selects one), with optional transforms
(`join`, `age_band`) and value maps. A decision maps to a FHIR R4 `Task`: `status` `ready`
for AUTO or `requested` otherwise, `priority` from the decision, `businessStatus` carrying the
outcome, `focus` and `for` references, the recommendation as `performerType`, and the
decision id as an identifier.

An agent with `cds_hooks` is exposed as a CDS Hooks 2.0 service (id `namespace.name`). v0.1
requires all declared prefetch keys and never queries the caller's FHIR server.

## 10. Behaviour tests

`tests/cases.yaml` is a list of `{name, input, expect}`. `expect` may contain `outcome`,
`outcome_in`, `not_outcome`, `recommendation`, `priority` and `answers.<question>`. Tests
describe behaviour that every provider variant is expected to preserve, especially the
safety behaviour.

## 11. Identity and versioning

The agent digest is the SHA-256 over every file in the agent (path and content, in sorted order)
except `evals/` and `DECISION_CARD.md`, which are attestations *about* the agent. Every decision
and evaluation report records the digest, so an audit record identifies the exact behaviour
that produced it.

## 12. Audit records

Every decision produces a record with id, timestamp, agent id/version/digest, input (or only its
hash when `audit.store_input: hash`), per-answer provider type/variant/model, label, confidence,
probabilities and evidence, outcome, recommendation, priority, reasons, fallback, provider
errors, latency, cost and token usage, and any governance override. Human review results and
downstream outcomes are appended as events. Events form a SHA-256 hash chain that also
commits to each decision record, so modification or deletion is detectable.
