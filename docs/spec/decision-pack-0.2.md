# Decision Pack Specification, version 0.2

Status: draft. Supersedes [0.1](decision-pack-0.1.md); every valid 0.1 pack is a valid 0.2
pack with unchanged behaviour (§13). The key words MUST, SHOULD and MAY are used as in RFC 2119. The reference
implementation is `openhealth.core.spec` (manifest), `openhealth.core.derive` (derived
questions) and `openhealth.core.policy` (semantics).
Where this document and the implementation disagree, file an issue; the document is intended
to be normative.

## 1. Purpose

A Decision Pack packages one healthcare decision so it can be executed, evaluated, governed
and shared independently of the models that implement it. A pack MUST be self-describing: a
reviewer can determine what it decides, for whom, with what inputs, under what automation
limits, and how well it performed on which data, without reading code.

## 2. Layout

A pack is a directory containing `pack.yaml`. All paths in the manifest are relative to the
pack directory and MUST NOT resolve outside it.

| Path (conventional) | Required | Content |
|---|---|---|
| `pack.yaml` | yes | Manifest (§3) |
| `policy.yaml` | no | Policy (§6). It may instead be inline in `pack.yaml` |
| `schemas/input.schema.json` | yes | JSON Schema 2020-12 for the pack input |
| `fhir/mapping.yaml` | no | FHIR R4 mapping (§9) |
| `logic/`, `models/` | per provider | Provider configuration and artifacts |
| `data/*.jsonl` | no | Evaluation datasets (§8) |
| `tests/cases.yaml` | SHOULD | Behaviour tests (§10) |
| `evals/benchmark.json` | SHOULD | Published evaluation report (§8) |
| `evals/leaderboard.json` | MAY | Provider configurations compared on the same datasets (§8) |
| `CHANGELOG.md` | SHOULD | Version history |
| `DECISION_CARD.md` | generated | Rendered Decision Card |

## 3. Manifest

```yaml
spec_version: "0.2"          # "0.1" is still accepted (§13)
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
questions:
  <key>:
    type: choice | boolean | score  # default choice (§4)
    instructions: ...
    criteria: {<label>: <criterion>}          # choice; optional for boolean
    levels: {<label>: {score, criterion}}     # score only
    derive: {from: [<question>...], table: [<row>...]}  # derived questions only (§4.3)
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
- Every question that is not derived is answered by exactly one provider slot. Derived
  questions MUST NOT be answered by a slot. Slots answer only declared questions. A slot's
  `use` names one of its variants.
- `policy.primary` is a question (it MAY be derived). Rule conditions reference existing
  questions and labels; `min_score`/`max_score` conditions reference score questions. Rule
  recommendations and `policy.labels` keys are labels of the primary question.
- Derived questions satisfy §4.3.
- A pack declaring `spec_version: "0.1"` MUST NOT use `type`, `levels`, `derive`,
  `min_probability`, `min_score` or `max_score`.
- `thresholds.review ≤ thresholds.auto`, both in [0, 1].
- Packs at risk level ≥ 2 MUST NOT contain policy rules whose outcome is `AUTO`.
- Every referenced file exists inside the pack.

## 4. Questions

A question is the unit of meaning. Its `criteria` are written for humans (they appear in the
Decision Card) and are also passed verbatim to providers that accept natural-language
instructions (Jev, LLMs). Changing a label set is a breaking change (MAJOR version). Rewording a
criterion changes behaviour for instruction-following providers and SHOULD be a MINOR version.

### 4.1 Question types

| `type` | Labels | Answer |
|---|---|---|
| `choice` (default) | the keys of `criteria` | a distribution over the labels |
| `boolean` | always `yes` and `no` | a distribution over `yes`/`no` |
| `score` | the keys of `levels`, ordered by `score` | a distribution over the levels, plus `value` |

A `boolean` question MAY give `criteria` for `yes` and/or `no` (default criteria "Yes." and
"No."); any other label is invalid. A question that needs a third answer such as `unknown` is a
`choice` question.

A `score` question lists its labels as `levels: {<label>: {score: <number>, criterion: ...}}`.
Its answer carries `value`, the **expected score** Σ p(label) × score(label), so that policy can
act on graded severity without collapsing the distribution to one label (§6). Criteria are taken
from the levels.

```yaml
severity:
  type: score
  instructions: How severe is the diabetes?
  levels:
    mild: {score: 1, criterion: Well controlled.}
    moderate: {score: 2, criterion: Partially controlled.}
    severe: {score: 3, criterion: Uncontrolled or complicated.}
```

### 4.2 Answers

An answer has `label` (the most probable label), `confidence` (its probability),
`probabilities` (every label of the question), `evidence`, the provider that produced it, and,
for score questions only, `value`.

### 4.3 Derived questions

A derived question is computed from other questions' answers by a decision table; no provider
answers it. It is how a pack expresses one outcome over many criteria (trial eligibility,
prior-authorization completeness, AI output checks) while keeping each criterion a separate,
separately evaluated question.

```yaml
eligibility:
  instructions: Overall prescreen result.
  criteria:
    candidate: Every criterion is met.
    not_eligible: At least one criterion is not met.
    needs_review: At least one criterion cannot be determined.
  derive:
    from: [inc_adult, inc_t2d, not_pregnant]
    table:
      - {any: "no", then: not_eligible}
      - {any: unknown, then: needs_review}
      - {else: candidate}
```

Rows are evaluated top-down and the first matching row gives the result:

- `{any: L, then: X}` matches when at least one member's label is `L`;
- `{all: L, then: X}` matches when every member's label is `L`;
- `{else: X}` always matches. It MUST be the last row and MUST NOT appear elsewhere.

A row MAY add `in: [<member>...]` to consider only those members, a subset of `from`. This is
for tables whose members answer in different directions, e.g. criteria that fail on `no` and
exclusions that fail on `yes`:

```yaml
table:
  - {any: "no",  in: [hba1c_in_range, egfr_30_or_more], then: not_eligible}
  - {any: "yes", in: [note_pancreatitis],               then: not_eligible}
  - {any: unknown, then: needs_review}
  - {else: candidate}
```

A row MAY also add `when: {<member>: [<label>...]}`: the row counts only while that member's
answer is one of the labels. This is for requirements that depend on another answer, e.g. a
document only one specialty needs:

```yaml
table:
  - {any: "no", in: [ecg_on_file], when: {specialty: [cardiology]}, then: return_for_info}
  - {else: schedule}
```

An `else` row takes no `in` and no `when`.

Validation (MUST): `from` lists one or more distinct, existing questions that are not themselves derived;
each `any`/`all` label is a label of at least one member the row considers; each result is a label of the derived
question; a table has at most 10 rows plus `else`; a `when` names one member of `from` and
labels of that member, and every `when` in a table names the same member. Derived questions MAY
be of any type.

**Probabilities.** The derived answer is not the table applied to each member's most likely
label, which would hide uncertainty. Its `probabilities` are the exact probability that each
result wins, computed over the members' full distributions under the assumption that the
members' answers are **independent**. (The reference implementation walks the members once,
tracking which rows are satisfied so far and whether each `when` is open:
O(members × labels × 2^rows).) Members answered by
the same provider from the same text are not truly independent, so a pack SHOULD put
conservative rows first (`not_eligible`, then `needs_review`) and SHOULD evaluate the derived
question on labelled data like any other.

**Evidence** names the row that the members' most likely labels select, followed by each
member's label and confidence, e.g. `table: else -> candidate`, `inc_adult=yes (0.98)`. When
the derived question is the primary one, the decision's reasons list every row that the
members' most likely labels satisfy, with the members that satisfy it, e.g.
`return_for_info: ecg_on_file is no (specialty is cardiology)`.

**Failures.** If any member has no answer (its provider failed), the derived question has no
answer and the decision records the error `derived_input_unavailable` for it. A missing primary
answer gives `ABSTAIN` (§6).

## 5. Providers

A provider answers every question in its slot with a probability distribution over the
question's labels. The answer's `label` is the most probable label, and `confidence` is the
provider's confidence in it. Providers MUST either answer every question in the slot or raise
a provider error with a stable, audit-safe code (e.g. `timeout`, `rate_limited`,
`credential_unavailable`, `invalid_response`, `invalid_input`). Error codes MUST NOT carry
credentials or remote error bodies.

Variants of a slot are interchangeable implementations of the same contract. Selecting a
variant (`providers={"router": "jev"}`) MUST NOT change the pack's questions, policy or
tests.

Built-in types: `rules` (deterministic patterns with negation handling and optional typo
tolerance), `criteria` (deterministic yes/no/unknown checks on structured data: numbers,
lab values in date windows with unit conversion, coded conditions, medication durations, an
item of a given type in any input list within a date window (`exists`, e.g. a document on
file), and a field having one of some values (`value`); missing data answers `unknown`),
`naive_bayes` (local, temperature-calibrated text classifier), `jev`, `llm` (a chat
model through an OpenAI-compatible API, asked for one probability per label and optionally
calibrated per question), and three
composites whose inline `members` are other (non-composite) variants: `any_of` (the most
alarming answer wins; for safety screens), `cascade` (members in order, the first whose
confidence on every slot question is ≥ `accept_at` is accepted; the last always is) and `vote`
(a weighted average of the members' distributions). A failed member of a composite is
recorded as a failure of the slot's questions (`<member>:<code>`), which caps `AUTO` (§6). Providers answer
`boolean` and `score` questions like `choice` questions, with a distribution over their labels;
the runtime adds the expected score.

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
2. Provider slots answer their questions; then derived questions are computed (§4.3) and
   score answers receive their expected `value`.
3. Policy `rules` are evaluated in order over the available answers. A rule's condition
   matches an answer when the answer's confidence is ≥ `when.min_confidence`, its label is in
   `when.labels` (if given), and its expected score is ≥ `when.min_score` and ≤
   `when.max_score` (if given; inclusive). With `when.min_probability`, the label test is
   replaced by a probability test: the answer's total probability over `when.labels` MUST be
   ≥ `min_probability`, whichever label is most likely. This is how a pack escalates on a
   meaningful chance of a critical label (e.g. P(urgent) ≥ 0.35) rather than only when that
   label wins; `min_probability` needs `labels` and cannot be combined with
   `min_confidence`. A condition needs at least one of `labels`, `min_score` or `max_score`. The first matching rule determines outcome, recommendation
   (default: the primary answer's label) and priority.
4. Otherwise, on the primary question: confidence ≥ the label's `auto_threshold` (default
   `thresholds.auto`) gives `AUTO`; ≥ `thresholds.review` gives `HUMAN_REVIEW`; else
   `ABSTAIN`. A missing primary answer gives `ABSTAIN`.
5. Caps apply only to `AUTO` and only ever lower it to `HUMAN_REVIEW`: a label with
   `allow_auto: false`; any provider failure in the decision; the risk level (§7).
6. `ABSTAIN` carries no recommendation. `ABSTAIN` and `BLOCK` carry the policy `fallback`.

## 7. Risk levels

| Level | Category | Runtime rule |
|---|---|---|
| 0 | Administrative | AUTO permitted |
| 1 | Clinical information processing | AUTO permitted; decisions MUST be audited |
| 2 | Clinical support | AUTO capped to HUMAN_REVIEW unless a governance override is supplied |
| 3 | Patient-specific prediction | As level 2 |
| 4 | Time-critical / diagnostic / treatment | AUTO always capped; no override |

A governance override is supplied by the *deploying* organization at runtime, never by the
pack. It names an approver and a reference, and it is recorded in every affected decision.

## 8. Evaluation

Dataset format: JSON Lines. Each line has an `input` (the pack input), optional `labels`
(`{question: label}`), optional `attributes` (subgroup metadata such as `language` or
`site`), and an optional `id`.

An evaluation report (`format: openhealth.eval/1`) MUST include the pack id, version and
digest; the provider variant and model of each slot; the dataset name, SHA-256 and size; per
question accuracy (derived questions included), macro-F1, per-label precision/recall/F1, confusion matrix, ECE with
reliability bins, and Brier score; outcome rates; automation accuracy and false automation
rate; expected override rate among reviewed cases; for each `critical` label, the detection
count and the number of true critical cases that were automated; latency percentiles; cost;
subgroup metrics for each configured attribute; and a threshold sweep. Reports MUST NOT
contain case inputs.

**Local validation** compares a report on local data with the pack's published benchmark. It
MUST report the overall delta of primary accuracy and SHOULD localize degradation by label
recall, subgroup, calibration and false automation. Any automated true-critical case is always
a finding.

A **leaderboard** (`format: openhealth.leaderboard/1`) compares provider configurations. Each
row summarises one report: the configuration (slot variants, naming the model of remote
providers), dataset and size, primary accuracy, macro-F1 and ECE, AUTO and false automation
rates, critical detection and automated misses, p50/p95 latency, cost per decision and error
count. Rows for one dataset MUST come from the same records. Like reports, a leaderboard MUST
NOT contain case inputs.

## 9. FHIR and CDS Hooks

`fhir/mapping.yaml` declares the `focus` resource type, the required and optional resource
types, and a mapping from input fields to FHIRPath-subset paths (dotted elements, where a
list-valued element yields all its items and `[n]` selects one), with optional transforms
(`join`, `age_band`) and value maps. A decision maps to a FHIR R4 `Task`: `status` `ready`
for AUTO or `requested` otherwise, `priority` from the decision, `businessStatus` carrying the
outcome, `focus` and `for` references, the recommendation as `performerType`, and the
decision id as an identifier.

A mapping MAY also declare `lists`: each target becomes a list of objects, one per resource of
the given type, with field paths relative to the resource. This is how a Patient-focused pack
receives a record with many Conditions, Observations or MedicationStatements:

```yaml
focus: Patient
fields:
  patient.age: {path: Patient.birthDate, transform: age_years}
  notes: {path: DocumentReference.content.attachment.data, transform: base64_text}
lists:
  observations:
    resource: Observation
    fields:
      code: {path: code.coding[0].code}
      value: {path: valueQuantity.value}
      unit: {path: valueQuantity.unit}
      date: {path: effectiveDateTime}
```

Transforms: `join` (all values, newline-separated), `age_band`, `age_years`, and `base64_text`
(decode every attachment and join them). Input MAY be a Bundle, a single resource, or a CDS
prefetch object whose values are resources or search Bundles; all are flattened and resources
are de-duplicated by type and id. Only the focus must be unique. The Task's `description` and
`performer` MAY be set in the mapping's `task` section.

**Templates.** Task `description` and `performer`, and card `summaries`, are templates:
`{recommendation}`, `{priority}` and `{<question>}` (that question's label, e.g.
`{specialty}`) are replaced; an unanswered question reads `unknown`. Any other placeholder is
refused when the pack loads.

A pack with `cds_hooks` is exposed as a CDS Hooks 2.0 service (id `namespace.name`). The reference runtime
requires all declared prefetch keys and never queries the caller's FHIR server. A pack MAY word
its cards with `cds_hooks.card`: a `summaries` template per recommendation (keys are primary
labels, or `none`), optional `indicators`, a `suggestion` label, and `show`, the
recommendations that produce a card at all (e.g. a `patient-view` service stays silent for
patients who are clearly not eligible).

## 10. Behaviour tests

`tests/cases.yaml` is a list of `{name, input, expect}`. `expect` may contain `outcome`,
`outcome_in`, `not_outcome`, `recommendation`, `priority` and `answers.<question>`. Tests
describe behaviour that every provider variant is expected to preserve, especially the
safety behaviour.

## 11. Identity and versioning

The pack digest is the SHA-256 over every file in the pack (path and content, in sorted order)
except `evals/` and `DECISION_CARD.md`, which are attestations *about* the pack. Every decision
and evaluation report records the digest, so an audit record identifies the exact behaviour
that produced it.

## 12. Audit records

Every decision produces a record with id, timestamp, pack id/version/digest, input (or only its
hash when `audit.store_input: hash`), per-answer provider type/variant/model, label, confidence,
probabilities, evidence and (score questions) value, with derived answers recorded under the
provider type `derived`, outcome, recommendation, priority, reasons, fallback, provider
errors, latency, cost and token usage, and any governance override. Human review results and
downstream outcomes are appended as events. Events form a SHA-256 hash chain that also
commits to each decision record, so modification or deletion is detectable.

## 13. Changes from 0.1 and migration

0.2 is backwards compatible. A manifest with `spec_version: "0.1"` still loads, validates and
produces identical decisions; it simply cannot use the new fields.

| Change | Section |
|---|---|
| `type: boolean` and `type: score` questions; `levels`; `Answer.value` (expected score) | §4.1, §4.2 |
| Derived questions (`derive:` decision tables with exact probabilities) | §4.3 |
| `min_score` / `max_score` policy conditions; `labels` becomes optional | §6 |
| `min_probability` policy conditions (a label's probability, not only the winning label) | §6 |
| Derive rows `in` (scoped to members) and `when` (gated on another answer) | §4.3 |
| `criteria` provider, including `exists` and `value` checks | §5 |
| FHIR `lists`, `age_years`/`base64_text`; card and Task templates naming answers | §9 |
| The decision output schema adds an optional `value` to each answer | §4.2 |

To migrate a pack:

1. Set `spec_version: "0.2"`.
2. Optionally turn yes/no questions into `type: boolean` (only if they have no third label such
   as `unknown`) and graded questions into `type: score` with `levels`. Changing a label set is
   a MAJOR version (§4); changing only the type while keeping labels is a MINOR version.
3. Where a primary question was really a combination of several criteria, split the criteria
   into member questions and derive the primary one; re-run evaluation and republish the
   benchmark, since the primary question's behaviour changes.
