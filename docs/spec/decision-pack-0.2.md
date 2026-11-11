# Decision Pack Specification, version 0.2

Status: draft. Supersedes [0.1](decision-pack-0.1.md); every valid 0.1 pack is a valid 0.2
pack with unchanged behaviour (§13). [0.3](decision-pack-0.3.md) changes only the FHIR mapping
(§9): what it adds is there, and the rest of this document holds for 0.3 too. The key words MUST, SHOULD and MAY are used as in RFC 2119. The reference
implementation is `mendari.core.spec` (manifest), `mendari.core.derive` (derived
questions) and `mendari.core.policy` (semantics).
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
version: 1.2.3                # semantic version, at most 64 characters
name: Human-readable name
summary: One or two sentences.
publisher: {name: ..., type: community | verified | institutional, url: ...}  # self-declared
license: SPDX license expression   # e.g. Apache-2.0, or MIT OR Apache-2.0
code_systems:                 # optional: the code systems the inputs, rules and data use
  - {url: http://loinc.org, name: LOINC}
  - {url: http://snomed.info/sct, name: SNOMED CT, licence_note: ...}  # a licensed one says its terms
risk: {level: 0-4, category: ..., rationale: ...}
card:
  intended_use: ...
  intended_users: [...]
  patient_population: ...
  out_of_scope: [...]
  known_failure_modes: [...]
  evidence: [{title, url?, note?}]
  provenance: synthetic | de_identified | site_records   # optional; the status line (below)
inputs: {schema: <path>, fhir: <path>?}
questions:
  <key>:
    type: choice | boolean | score  # default choice (§4)
    instructions: ...
    display: What a person reads      # optional, at most 80 characters
    names: {<label>: What a person reads}  # optional, the question's own labels only
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
- `version` is a semantic version of at most 64 characters.
- `license` is an SPDX license expression of at most 200 characters: identifiers (or
  `LicenseRef-…`, optionally after `DocumentRef-…:`) joined by `AND` and `OR`, an exception after
  `WITH`, and parentheses. Its syntax is checked, not each identifier against SPDX's list.
- `code_systems` lists each system once: a `url` (`http(s)://…` or any `urn:…`) and a `name`. A
  code system whose codes are licensed rather than free to redistribute everywhere (CPT,
  SNOMED CT, MedDRA, X12's code lists and CDT) MUST have a `licence_note` saying the terms the
  pack uses it under. A pack any of whose digested files names such a system by its url or OID
  (bare or as `urn:oid:`, a JSON-escaped `\/` read as `/`) MUST declare it so, whatever file
  names it. A bare code value names no system, so no check can find it: declaring the system it
  comes from is the author's to do.
- A YAML file of the pack (manifest, policy, provider configs, tests) that uses anchors has
  at most 1,000,000 nodes and 32,000,000 characters of scalar text, counted as JSON writes them
  (a control character counts six), once every alias is expanded (an alias counts each time it
  is used), and no alias inside its own anchor. A file without an anchor has no alias, and is
  not counted. Every value in it is one the safe loader can build: a value it cannot (a date
  such as 2024-02-30, an explicitly tagged `!!bool maybe`) makes the pack invalid, named by the
  file, the error's type and the value's line and column. So does a value JSON cannot carry: a
  string with a lone surrogate (`"\ud800"`), `!!binary`, `!!set`, `!!omap` or `!!pairs`. So does a
  number (an integer or a float, in any base, implicit or tagged) whose text, underscores
  removed, is longer than 4,300 characters or has more than 4 `:` parts (base 60, as in
  `1:30:00`), and an integer of more than 4,300 decimal digits: each is refused before it is
  built.
- Every referenced file exists inside the pack. A directory pack MUST NOT hold a symbolic link
  that resolves outside the pack's directory. Hidden entries (a name starting with `.`) and
  `__pycache__` are not part of the pack (not digested, archived or read), so a `.venv` in the
  pack's folder is fine.
- Every file a pack decides or tests with MUST be one its digest hashes (§11). The manifest
  fields naming one are `inputs.schema`, `inputs.fhir`, `policy` (when it names a file),
  `tests`, `evaluation.datasets.<name>`, and, in a provider variant or a composite member,
  `config`, `library`, `model_file` and `calibration`. Such a path MUST NOT go through a hidden
  entry or `__pycache__` (as written or once links are resolved) nor be in `evals/` or a
  `DECISION_CARD.md`, and the file it reaches MUST be one the digest hashes, compared as a file
  on disk rather than by spelling: on a case-insensitive filesystem `EVALS/rules.yaml` is
  `evals/rules.yaml`, and is refused. A file read under any other key is held to the same rule
  when it is read. Its content MUST be the content the digest hashed: an implementation reads
  each file once, checks its bytes against the digest's hash of that file and uses those bytes,
  so a file edited after the pack was loaded is refused until the pack is loaded again.
- A JSON file of the pack (the input schema, a model, a calibration) is JSON in UTF-8 with no
  string holding a lone surrogate, no number that is not finite and no integer too large to be
  a finite number (and so none of more than 4,300 digits); a calibration's `temperatures` is a
  mapping; a `naive_bayes` model has the shape `mendari train` writes; a text file a
  provider or the card reads (a CQL library, `CHANGELOG.md`) is UTF-8.
- `evaluation.benchmark` is the one manifest path that MUST be an attestation: in `evals/`
  (it may not exist yet; a `DECISION_CARD.md` elsewhere is refused). A leaderboard
  (`evals/leaderboard.json`) and the card (`DECISION_CARD.md`) are read by those fixed names. A
  benchmark or leaderboard that is not JSON, holds what JSON cannot carry or is not the report
  the Decision Card shows is treated as absent (the card says why), not as an invalid pack.
- The input schema is self-contained: `$ref`, `$dynamicRef` and `$recursiveRef` start with `#`
  and resolve to a schema (an object or a boolean) in the document; `$schema`, if present, is at
  the root only and names `https://json-schema.org/draft/2020-12/schema`; `$id` is at the root
  only; it holds no `$dynamicAnchor` and no `format: "regex"`. These keys are looked for in the
  whole document, since a `$ref` can make any object in it a schema. Inputs are checked with a
  validator that retrieves nothing, so checking an input never fetches a URL or reads a file.
- The input schema of a pack the deployment owns (a reference pack, or an operator's local pack;
  one it does not own has the caps and the time budget of §5 instead) is bounded in shape: its
  document nests objects and lists at most 32 deep; no path through it, following its
  applicators, holds more than 16 `$ref`s; and it applies at most 1,000 schema objects to one
  value of an input, a schema reached twice (two `$ref`s to it, an `allOf` listing two that
  reach it) counted twice. A schema MAY refer back to itself only as a recursive list or tree
  does: each schema of the loop leads back into it exactly once, the loop passes through a
  keyword that applies to a property or an item (`properties`, `items` and the like), and it
  leads to no other loop. A schema that applies itself to the same value, a node that applies
  its child twice and a binary tree (`left` and `right` both the node) are invalid. A schema
  holding `unevaluatedProperties` or `unevaluatedItems` MUST NOT lead, through any applicator, to
  another schema holding one, nor back to itself: to find what is unevaluated, a validator checks
  again every schema below it, so each one more below another multiplies the work.

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

```

**Status line.** Every surface that shows a pack (its Decision Card's first line, the dashboard,
Try it, the review queue, CDS Hooks discovery and cards, the registry and the Hub) shows a line
derived from `card.provenance`, never written by the pack: with `synthetic`, "Synthetic example.
Not clinically reviewed. Not for patient care."; otherwise "Not clinically reviewed." There is
not yet a way to record a clinician's review of a pack's digest, so no pack can claim one.
**Display names.** `display` names a question and `names` its labels for the people who read
them; without them a key reads as words (`medical_assistant`: "Medical assistant"). They change
no decision.yaml
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

The provider's `model` is the model that answered:

- `llm`: the id the endpoint reports answering with (a dated snapshot behind an alias, say)
  when it is well formed; otherwise the configured model, and the call stands.
- `jev`: the version Jev reports; one missing or malformed is an `invalid_response`, and the
  slot's questions fail (§5).
- A provider computed from a file in the pack (rules, criteria, naive Bayes, dosing, CQL): the
  fingerprint of that file.
- A composite: `member:model`, for `any_of` and `cascade` the member whose answer was used, for
  `vote` each member that answered.

The model asked for stays in the deployment's configured providers.

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
- `L` MAY be a list of labels: `{any: ["no", unknown], then: X}` matches a member whose label is
  either;
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

A row MAY also add `when: {<member>: [<label>...], ...}`: the row counts only while each named
member's answer is one of its labels. This is for requirements that depend on other answers,
e.g. a document only one specialty needs, or a requirement one payer has for one service
unless there is a red flag:

```yaml
table:
  - {any: "no", in: [ecg_on_file], when: {specialty: [cardiology]}, then: return_for_info}
  - any: ["no", unknown]
    in: [conservative_therapy]
    when: {service: [lumbar_mri], red_flag: ["no"]}
    then: needs_attention
  - {else: schedule}
```

An `else` row takes no `in` and no `when`.

Validation (MUST): `from` lists one or more distinct, existing questions that are not themselves derived;
each `any`/`all` label is a label of at least one member the row considers; each result is a label of the derived
question; a table has at most 10 rows plus `else`; a `when` names members of `from`, each with
labels of that member. Derived questions MAY be of any type.

**Probabilities.** The derived answer is not the table applied to each member's most likely
label, which would hide uncertainty. Its `probabilities` are the exact probability that each
result wins, computed over the members' full distributions under the assumption that the
members' answers are **independent**. (The reference implementation walks the members once,
tracking which rows are satisfied so far and whether each `when` is still open:
O(members × labels × 4^rows).) Members answered by
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
a provider error with one of these stable, audit-safe codes: `budget_exhausted`,
`credential_unavailable`, `insufficient_credit`, `invalid_endpoint`, `invalid_input`,
`invalid_response`, `not_configured`, `provider_unavailable`, `quota_exhausted`,
`rate_limited`, `time_budget`, `timeout` or `unavailable`. A derived question whose inputs
failed is recorded as `derived_input_unavailable`. No other code is recorded. Error codes MUST
NOT carry credentials or remote error bodies.

A decision's remote calls share a wall-clock deadline the deployment sets
(`MENDARI_DECISION_DEADLINE_S`, default 120 s): a call that would start after it is not made,
and one under way is cut at it (each wait it makes, a retry included, is bounded by what
remains); either is the provider error `timeout`. Local providers always answer. A pack cannot
set it.

Variants of a slot are interchangeable implementations of the same contract. Selecting a
variant (`providers={"router": "jev"}`) MUST NOT change the pack's questions, policy or
tests.

Built-in types: `rules` (deterministic patterns with negation handling and optional typo
tolerance), `criteria` (deterministic yes/no/unknown checks on structured data: numbers,
lab values in date windows with unit conversion, coded conditions, medication durations, an
item of a given type in any input list within a date window (`exists`, e.g. a document on
file), and a field having one of some values (`value`); missing data answers `unknown`;
answers MAY be any label of the question, e.g. the service an order code is for, and a check
MAY carry `when: {field, in}` to apply only while an input field has one of those values),
`dosing` (dose arithmetic on free text: for each medicine in a pack table, the largest daily
amount a sentence allows, e.g. "1000 mg every 4 hours" = 6000 mg/day, compared with the maximum;
`yes` / `no` / `unknown`), `cql` (a CQL library in the pack, evaluated on a CQL engine with the
input as FHIR R4; each answered question maps to a definition whose String result is the label,
or whose Boolean result is `yes` / `no`, null being `unknown`; the library's declaration,
`library Name version '...'`, MUST be its first statement, comments aside, and the runtime
uploads it to the engine under a name and id unique to the pack and the text), `naive_bayes` (local,
temperature-calibrated text classifier), `jev`, `llm` (a chat
model through an OpenAI-compatible API, asked for one probability per label and optionally
calibrated per question), and three
composites whose inline `members` are other (non-composite) variants: `any_of` (the most
alarming answer wins; for safety screens), `cascade` (members in order, the first whose
confidence on every slot question is ≥ `accept_at` is accepted; the last always is) and `vote`
(a weighted average of the members' distributions). A failed member of a composite is
recorded as a failure of the slot's questions (`<member>:<code>`), which caps `AUTO` (§6). Providers answer
`boolean` and `score` questions like `choice` questions, with a distribution over their labels;
the runtime adds the expected score.

The built-in types that read free text on the machine read it in its **plain form**: `rules`,
`dosing` and `naive_bayes` their text fields, and `criteria` a medication's name. The plain form
is the same words in the characters a keyboard types, so that text which differs only in
typography decides the same:

1. invisible format characters are left out (Unicode category Cf: zero-width spaces and joiners,
   the byte order mark, the soft hyphen, direction marks);
2. a letter and an accent written as two characters become the one character (Unicode NFC),
   unless composing would make the text longer, where the text stays as it is;
3. apostrophes and single quotation marks (U+2018 to U+201B, the prime U+2032, U+02BC, and the
   acute and grave accents U+00B4 and U+0060) become `'`; double quotation marks (U+201C to
   U+201F, the double prime U+2033) become `"`; hyphens, dashes and the minus sign (U+2010 to
   U+2015, U+2212) become `-`; the ellipsis character (U+2026) becomes `.`;
4. every line end (a carriage return with or without a newline, a vertical tab, a form feed,
   U+0085, U+2028, U+2029) becomes a newline, and every other run of white space one space.

A newline stays a newline, and nothing else changes: case is kept (rules patterns ignore it
themselves), and so are compatibility characters (a ligature, a full-width letter, the micro
sign). The plain form is never longer than the text. A pack's patterns, a medication check's
`names` and a trained model's tokens are matched against it, so they MUST be written in it: a
character the plain form never holds, such as a curly apostrophe or a no-break space, matches
nothing. The decision's record, its input hash, the input schema's checks and what a remote
provider is sent keep the text as it was received.

A pack's patterns (every `rules` pattern, of every variant and composite member, and every
`pattern` and `patternProperties` key in its input schema) are Python `re` regular expressions,
run as `re` runs them (rules patterns with case ignored). Each MUST compile and be at most 2,000
characters, and a pack MUST hold at most 500 of them; the runtime compiles every one at load, so
a pack with an invalid one is invalid, named by file and rule. `re` has no time limit: a pattern that backtracks (`(a+)+$`)
can take seconds or longer on a crafted input: a pack's patterns are code its author wrote, and
installing a pack means trusting its author. The patterns of a pack the deployment does not own
(a community pack, id `community/...`, or one pulled from a registry whose digest is not that of
a reference pack in the installed release) are written the same way but run on RE2, which never
backtracks, rewritten to mean what they mean in `re` (what still differs is listed in
`core/re2_syntax.py`); such a pack holding a pattern RE2 cannot run (a lookahead or lookbehind,
a back reference, a conditional or atomic group, a possessive quantifier, or one too large once
compiled) is invalid, and so is an input
schema that uses a keyword outside those whose work Mendari bounds (the contributor's guide
lists them: `unevaluatedProperties` or `unevaluatedItems`, which jsonschema works out on its own
and with `re`, `contains`, `uniqueItems` and the like are refused; `$ref` names one of the
root's `$defs`, whose names are letters, digits, `_`, `.` and `-`; `required` lists at most 100
names and `enum` at most 500 values, texts of at most 200 characters). So is one whose settings
multiply its patterns' work: a field named twice in a `text_fields` (a provider's or a
question's), a `negation_window` over 200 characters or more than 50 `negation_cues`, an input
schema that can run more than 50 patterns on one value or key (through `$ref`, `allOf` and the
other applicators; each `patternProperties` pattern counts three times where
`additionalProperties` is also set, which searches them again and for its error message) or that
refers back to itself where patterns run, more than 500 patterns run by its rules on an input
(each once for each question it answers and each composite member holding it) and its schema on
one value together, or those same runs coming to a size of more than 3,000 together, a pattern's
size being the characters, classes and `.`s it holds, each as often as a count repeats it
(`a{1,50}` is 50, `\w+` is 1, alternatives added together): RE2's time on each character grows
with it. At run time such a pack refuses an input whose texts are longer than 10,000 characters,
alone or together, or that holds more than 10,000 values or is nested more than 32 deep, before
any pattern runs, and its patterns and schema checks share a time budget of one second of CPU
per decision: past it the decision abstains with the error `time_budget`, for a person (an input
the schema had already found wrong is refused). Which engine a pack runs on is never read from
its manifest; the pack format is the same for every pack. A pack from a registry whose id is that
of a reference pack in the installed release, and whose version is equal to that pack's as
versions are ordered (by their numbers: `0.4.1-x` and `0.04.1` equal `0.4.1`), MUST be that
pack (its digest): any other is invalid, since it would be served in the reference pack's place.

A remote provider's endpoint, model and credentials belong to the deployment, which supplies
them in fixed environment variables (`jev`: `JEV_API_URL`, `JEV_MODEL`, `JEV_API_KEY`,
`JEV_CONNECTOR`; `llm`: `LLM_BASE_URL`, `LLM_MODEL`, `AI_GATEWAY_API_KEY`, `LLM_API_KEY`;
`cql`: `MENDARI_CQL_URL`). A variant MUST NOT name the variables a provider reads: a `jev` or
`llm` variant (or composite member) that sets `api_key_env`, `base_url_env`, `connector_env`, `endpoint_env`,
`local_key_env` or `model_env` is invalid, whatever the pack's `spec_version`. A deployment MAY
also list the origins its providers may be called at (in the reference runtime,
`MENDARI_PROVIDER_ALLOWED_ORIGINS`): a call to any other is not made, and the question fails
(`invalid_endpoint`). A variant MAY pin
`model`, and `llm` variants MAY tune their own requests (`timeout_ms`, `max_tokens`,
`temperature`, `reasoning_effort`).

Each provider type's config is closed: a variant or composite member MUST set only the keys its
type reads, with values it accepts, and its pack files MUST be ones it can run with (a rules,
criteria or dosing file of that shape, answering the slot's questions with their labels; a
trained naive_bayes model or LLM calibration file; a CQL library declared first). The keys of
every type are published as a JSON Schema,
[`provider-configs.schema.json`](provider-configs.schema.json), generated from the code.
Every variant is checked at load, the ones a pack does not use by default too: a pack the
deployment does not own (a community pack, or one pulled from a registry that is not a
reference pack) with any problem is invalid; any other loads, with the problems as warnings,
and `mendari validate` refuses it. For a pack the deployment does not own, every `jev` and `llm`
variant and member MUST name its `state_fields` (at least one), and the deployment chooses
what the pack may only suggest: its `model` is ignored, its `max_tokens` and `timeout_ms` are
held to the provider defaults (`llm` 4,096 tokens and 30,000 ms, `jev` 5,000 ms; a lower value
stands), and a `reasoning_effort` above `low` is ignored.

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
   `allow_auto: false`; any provider failure in the decision; a slot named in
   `automation_requires` running another variant; the risk level (§7).

`automation_requires: {<slot>: [<variant>...]}` allows `AUTO` only while each named slot runs
one of the listed variants, e.g. a safety screen that includes a model:

```yaml
automation_requires:
  triage_screen: [rules_and_jev, rules_and_llm]
```

Other variants still answer and decide, but every decision goes to a person. A runtime that
does not know which variants are running MUST NOT automate such a pack.
6. `ABSTAIN` carries no recommendation. `ABSTAIN` and `BLOCK` carry the policy `fallback`.

## 7. Risk levels

| Level | Category | Runtime rule |
|---|---|---|
| 0 | Administrative | AUTO permitted |
| 1 | Clinical information processing | AUTO permitted; decisions MUST be audited |
| 2 | Clinical support | AUTO capped to HUMAN_REVIEW unless a governance override is supplied |
| 3 | Patient-specific prediction | As level 2 |
| 4 | Time-critical / diagnostic / treatment | AUTO always capped; no override |

An **automation hold** is the deployment's, never the pack's: while one covers a decision (an
organization, a pack, a digest, or the whole deployment), an outcome that would be `AUTO` becomes
`HUMAN_REVIEW` with the same recommendation and priority, and nothing else changes. A decision
over its organization's daily quota is held the same way.

A governance override is supplied by the *deploying* organization at runtime, never by the
pack. It names an approver and a reference, and it is recorded in every affected decision.

## 8. Evaluation

Dataset format: JSON Lines. Each line has an `input` (the pack input), optional `labels`
(`{question: label}`), optional `attributes` (subgroup metadata such as `language` or
`site`), and an optional `id`.

**Critical labels** (`evaluation.critical`) name the (question, label) pairs whose true cases
matter most. In the pack's default deployment (no governance approval) a true critical case
MUST NOT be automated: a person sees every one. Under a governance approval (risk levels 2–3),
an automated true critical case MUST carry the correct primary recommendation: the trial
prescreener may automate `not_eligible` on an exclusion it found, never `candidate`. Mendari's
tests hold every maintained pack to both, in every configuration that runs without calling a
service, on every dataset the pack declares. Reports, local validation and the pack gate still
count every automated true critical case.

An evaluation report (`format: mendari.eval/1`) MUST include the pack id, version and
digest; the provider variant and model of each slot; the dataset name, SHA-256 and size; per
question accuracy (derived questions included), macro-F1, per-label precision/recall/F1, confusion matrix, ECE with
reliability bins, and Brier score; outcome rates; automation accuracy and false automation
rate; expected override rate among reviewed cases; for each `critical` label, the detection
count and the number of true critical cases that were automated; latency percentiles; cost;
subgroup metrics for each configured attribute; and a threshold sweep. Reports MUST NOT
contain case inputs. Superseded by `mendari.eval/2`, whose result is computed from a per-case
ledger and carries each proportion with its exact interval: see
[Evaluation reports, ledgers and paired comparisons](evaluation-reports.md). A
`mendari.eval/1` report still loads.

**Local validation** compares a report on local data with the pack's published benchmark. It
MUST report the overall delta of primary accuracy and SHOULD localize degradation by label
recall, subgroup, calibration and false automation. Any automated true-critical case is always
a finding. A pack pulled from a registry MUST NOT be compared with the benchmark it carries,
which is outside its digest and is its publisher's upload
([ADR-0012](../decisions/ADR-0012-a-registry-that-shows-only-what-it-checked.md)); a report
the caller gives MAY be.

A **leaderboard** (`format: mendari.leaderboard/2`) compares provider configurations. Each
row summarises one report: the configuration (slot variants, naming the model of remote
providers), the pack version and digest, the dataset with its hash and size, primary accuracy,
macro-F1 and ECE, AUTO and false automation, critical detection and automated misses, p50/p95
latency, cost per decision, error count and the report's ledger. A row is keyed by pack digest,
dataset hash, size and configuration, and is ranked by one function
([`mendari.rank/1`](evaluation-reports.md#leaderboards-mendarileaderboard2-and-mendarirank1)),
whose order the card, the command line and the dashboard all list the rows in. Every set a
leaderboard is measured on is public, so they show it as a listing order and print no rank: a
regression check, not a ranking. Rows for one dataset MUST
come from the same records. Like reports, a leaderboard MUST NOT contain case inputs. A
`mendari.leaderboard/1` file still loads; its rows have no digest, so none ranks.

A **shared report** (`format: mendari.site-report/1`) is what a site sends a registry about
its own evaluation. It is a closed schema of names and rounded numbers: the pack (id, version,
digest), the providers and policy, per-question metrics, outcome counts and rates, each critical
label's counts, the threshold sweep, latency, cost, error counts and the dataset's size. It MUST
NOT contain a case id, case text, the dataset's name or hash, who approved a governance override
(only whether one applied), or subgroup cells (their values come from a site's data).
Calibration bins with fewer than 10 cases are dropped. Its question, label, slot, variant and
composite member names are the published pack's own, and a registry MUST refuse a report that
names any other; model names are short ASCII names, and error codes are those of §5. A
composite provider
(`any_of`, `cascade`, `vote`) is shared as it is recorded: its model lists every member's as
`member:model`, in the pack's order, joined by ` + ` (` → ` for a `cascade`), and a member that
failed is counted under `member:code` (`, ` between members of one composite that failed
together, in the same order). A registry MUST NOT echo what it refuses.

## 9. FHIR and CDS Hooks

Spec [0.3](decision-pack-0.3.md) extends the mapping below; a 0.2 pack reads as this section
says. `fhir/mapping.yaml` declares the `focus` resource type, the required and optional resource
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
`{specialty}`) are replaced; an unanswered question reads `unknown`. On a card, a person reads
it, so each label is its display name (the question's `names`, else the label as words:
`lumbar_mri` reads `Lumbar MRI` with `names`, `cardiology` reads `Cardiology`), and no
recommendation reads `manual triage`; a Task keeps the labels. Every text a card summary can
give this way MUST be under 140 characters (at most 139), CDS Hooks' limit, checked when the
pack loads. Any other placeholder is refused when the pack loads. A placeholder is a bare name:
no conversion (`{priority!r}`), format spec (`{priority:>9}`), attribute (`{priority.x}`) or
index (`{priority[0]}`). A pack the
deployment does not own (a community pack, or one from a registry that is not a reference pack
of the release) is invalid with one, its Task text included, or with a card summary that can
read 140 characters or more; any other pack loads with a warning for either in its card
summaries (a card cuts a summary to 139 characters), its Task text renders as it always did, and
`mendari validate` refuses both.

A mapping MAY declare `focus_where: {path, in: [...]}` to say which resources of the focus type
the pack is about, e.g. referrals among ServiceRequests (lab orders are ServiceRequests too):

```yaml
focus: ServiceRequest
focus_where: {path: category[0].coding[0].code, in: ["FR101"]}  # the code a site marks referrals with
```

Other resources of the focus type are ignored. (`FR101` is the Referral Router's fictional
referral code; a site lists the code its own EHR uses.)

A pack with `cds_hooks` is exposed as a CDS Hooks 2.0 service (id `namespace.name`). Each
`prefetch` template is a read or a search relative to the EHR's FHIR server: `Type/<id>`,
`Type/{{context.<name>}}` or `Type?<query>` (a resource type, then at most one id or context
token, then a query with no `/`, `?`, `#`, backslash or whitespace), with no `..` anywhere: no
absolute URL, `//`, leading `/`, operation (`$everything`) or fragment. A pack the deployment does
not own is invalid with any other; any other pack loads with a warning, and `mendari validate`
refuses it. The reference runtime fetches a declared prefetch key the request leaves out (or
sends empty) only from a `fhirServer` the deployment allows (`MENDARI_FHIR_ALLOWED_SERVERS`); with
no `fhirServer` the request is refused with `412`, as CDS Hooks 2.0 says. A key whose value is
null has no data and is not fetched. A declared key that cannot be read (a failed read, an
OperationOutcome, a value that is not a resource, a resource or search page the runtime read that
is not R4 JSON), and a `base64_text` attachment that is only a `url` or whose data does not
decode, make the decision `ABSTAIN` before any provider runs, with the reason
`could not read <key>: <reason>` (a closed set of reason codes); refused input still gives
`BLOCK`. The hook still answers, with a card that says so whatever `show` says. Every decision
made from FHIR records what the mapping looked for and found, as counts (`data_coverage`,
`mendari.data-coverage/1`). For the order
hooks (`order-select`, `order-sign`), the orders come in `context.draftOrders`: every draft order
that is a focus resource gets its own decision, with the prefetched record, and its own card. A
draft with none of them returns no cards. A pack MAY word
its cards with `cds_hooks.card`: a `summaries` template per recommendation (keys are primary
labels, or `none`), optional `indicators`, a `suggestion` label, and `show`, the
recommendations that produce a card at all (e.g. a `patient-view` service stays silent for
patients who are clearly not eligible). The reference runtime's cards are CDS Hooks 2.0's: a
card's `uuid` is its decision's id, its one suggestion lets the clinician take at most one
(`selectionBehavior: at-most-one`), and it offers Mendari's override reasons. Its detail is the
pack's status, the notice and one plain sentence for the outcome, never a percentage or the
confidence; where the deployment has a public address, the reasons are behind a link to the
decision's record. The EHR MAY send back what the clinician did with each card
(`POST /cds-services/{id}/feedback`), which is chained on the decision as codes and ids, never a
comment.

An order-hook pack MAY map recommendations to **Da Vinci CRD coverage information** with
`cds_hooks.coverage_information: {<primary label>: {covered, pa_needed, doc_needed, doc_purpose,
info_needed, reason}}` (CRD's codes; `reason` is plain words and MUST NOT carry case data). It
needs a `coverage` prefetch. For each decided order whose recommendation is mapped, the response
adds a `systemActions` entry: an `update` of the draft order with CRD's `coverage-information`
extension, naming the patient's Coverage, the date and an assertion id (the decision id). Labels
without a mapping assert nothing, and cards are unchanged. Such a service declares CRD's version
(`davinci-crd.version`) and one configuration option, `coverage-info`, in discovery, and each of
its cards says it is `coverage-info` (`source.topic`); a request whose
`extension.davinci-crd.configuration` sets `coverage-info` to false gets neither its cards nor
its system actions, and each order is still decided and recorded.

A pack whose primary question is derived MAY serve **Da Vinci DTR** questionnaires with
`cds_hooks.questionnaire: {missing: <primary label>, context: [<questions>], title}`. For the
answers of the `context` questions (e.g. service and payer), the documentation the payer asks
for is every member of the primary table that can turn the decision into `missing` (rows gated
on other context values don't count, and a row decided by the context alone ends the search).
`Questionnaire/$questionnaire-package` returns a DTR package Bundle: that Questionnaire, and a
QuestionnaireResponse pre-answered from the decision the order's CRD assertion id names.
Coverage information for `missing` links the questionnaire.

### 9.1 Guard

A pack MAY declare `guard: {draft: <input field>, release: [<primary labels>]}` to sit between a
language model and its reader, e.g. a patient. The runtime turns each decision into a status:
`released` (AUTO with a `release` recommendation; the draft is returned to send), `held_for_review`
(HUMAN_REVIEW or ABSTAIN) or `blocked` (BLOCK; the draft MUST NOT be sent and the policy
`fallback` applies). Decisions are audited as usual. At risk level 2 or above, `released`
requires a recorded governance override (§7). The reference server exposes this as
`POST /api/packs/{id}/guard`.

## 10. Behaviour tests

`tests/cases.yaml` is a list of `{name, input, expect}`: each case is a mapping with an `input`
mapping, an optional `name` (text) and an `expect` mapping, and no other key. `expect` holds at
least one of `outcome`, `outcome_in` (a non-empty list), `not_outcome`, `recommendation`,
`priority` and `answers` (a non-empty mapping of question to label), and nothing else, so a
misspelt key or an empty expectation cannot pass by accident. Each value is one the pack can
decide: an outcome (`AUTO`, `HUMAN_REVIEW`, `ABSTAIN`, `BLOCK`), a priority (`routine`,
`urgent`, `asap`, `stat`), a label of the primary question for `recommendation` (or null), and,
in `answers`, the pack's own questions and their labels. Tests describe behaviour that every
provider variant is expected to preserve, especially the safety behaviour. Every case MUST have
that shape and be JSON once loaded: a date or a number that is not finite (or, refused by the
loader with its line and column, binary, a set or a string with a lone surrogate) makes the
tests invalid, named by the case's number (and name) and what is wrong. Running a pack's tests
when it has none is a failure, not a pass.

`mendari validate` also reads every dataset `evaluation.datasets` declares: each line is JSON
with an `input` that matches the input schema as a decision checks it, and each record's
`labels` (if any) map the pack's questions to their labels (§8).

## 11. Identity and versioning

The pack digest is the SHA-256 over every file in the pack (path and content, in sorted order)
except `evals/` and `DECISION_CARD.md`, which are attestations *about* the pack, and hidden
entries and `__pycache__`, which are not part of it (§3). Every file the pack decides or tests
with is one the digest hashes, compared as a file on disk, and is used only with the content the
digest hashed (§3), so the digest covers everything the pack decides with; `evaluation.benchmark`, an attestation, is in `evals/`. Every decision and
evaluation report records the digest, so an audit record identifies the exact behaviour that
produced it.

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
produces identical decisions; it simply cannot use the new fields. One restriction applies to
packs of every version: a `jev` or `llm` variant may not name the environment variables its
provider reads (§5); a directory pack may not hold a symbolic link out of it, and an input
schema must be self-contained, a version has at most 64 characters, a YAML file that uses
anchors expands to at most 1,000,000 nodes and 32,000,000 characters as JSON and holds no
number over 4,300 characters or 4 `:` parts (§3), every file the pack decides or tests with is
one its digest hashes, however a path spells it, and `evaluation.benchmark` is in `evals/` (§3),
YAML and JSON hold only what JSON can carry (§3), every behaviour test has the shape of §10 and is
JSON (§10), and every pattern (input-schema `patternProperties` keys
included) compiles, at most 2,000 characters and 500 per pack (§5). No published pack did any
of these.

| Change | Section |
|---|---|
| `type: boolean` and `type: score` questions; `levels`; `Answer.value` (expected score) | §4.1, §4.2 |
| Derived questions (`derive:` decision tables with exact probabilities) | §4.3 |
| `min_score` / `max_score` policy conditions; `labels` becomes optional | §6 |
| `min_probability` policy conditions (a label's probability, not only the winning label) | §6 |
| `automation_requires` (AUTO only while given slots run given variants) | §6 |
| `card.provenance` (the status line every surface shows) and question `display` and `names` | §3 |
| Derive rows `in` (scoped to members), `when` (gated on other answers) and label lists | §4.3 |
| `criteria` provider: `exists`/`value` checks, any question label, per-check `when` | §5 |
| FHIR `lists`, `age_years`/`base64_text`; card and Task templates naming answers | §9 |
| FHIR `focus_where`; one decision per draft order for `order-select`/`order-sign` | §9 |
| `dosing` provider; `guard` section and statuses | §5, §9.1 |
| `cql` provider (a library in the pack, a CQL engine at `$MENDARI_CQL_URL`) | §5 |
| `cds_hooks.coverage_information` (Da Vinci CRD system actions on order hooks) | §9 |
| `cds_hooks.questionnaire` (Da Vinci DTR `$questionnaire-package`) | §9 |
| The decision output schema adds an optional `value` to each answer | §4.2 |
| `jev` and `llm` variants may not name the environment variables a provider reads (`*_env`; every spec version) | §5 |
| A `cql` library's declaration is its first statement (every spec version) | §5 |
| Every pack pattern (rules patterns, input-schema `pattern`s and `patternProperties` keys) compiles with `re`, at most 2,000 characters, at most 500 per pack, checked at load (every spec version) | §5 |
| The built-in types that read free text on the machine (`rules`, `dosing`, `naive_bayes`, and `criteria` a medication's name) read it in its plain form: typography only (quotation marks, dashes, white space, line ends, invisible format characters, composed accents), so a pack's patterns, names and tokens are written in it (every spec version) | §5 |
| A version of at most 64 characters; YAML using anchors of at most 1,000,000 nodes and 32,000,000 characters as JSON, aliases expanded; YAML numbers of at most 4,300 characters and 4 `:` parts; every file a pack decides or tests with is one its digest hashes, compared on disk, and `evaluation.benchmark` is in `evals/` (every spec version) | §3 |
| `license` is an SPDX license expression; `code_systems`, and a licensed code system (CPT, SNOMED CT, MedDRA, X12, CDT) named by url or OID anywhere in the pack is declared there with a `licence_note` (every spec version) | §3 |
| A behaviour test has an `expect` of at least one known key and no other, each value one the pack can decide, and no key beyond `name`, `input` and `expect`; a test run with no test fails; declared datasets are read by `validate` (every spec version) | §10 |
| Every behaviour test is a mapping with an `input` mapping, an optional text `name` and an optional `expect` mapping, and is JSON: no date, binary, set, lone surrogate or non-finite number (every spec version) | §10 |
| No lone surrogate, `!!binary`, `!!set`, `!!omap` or `!!pairs` in a pack's YAML, no lone surrogate or non-finite number in its JSON; a file is used only with the content its digest hashed (every spec version) | §3 |
| No symbolic link out of a directory pack; a self-contained input schema (local references only, no nested `$schema` or `$id`, no `$dynamicAnchor`, no `format: regex`; every spec version) | §3 |
| Provider configs are closed (the published JSON Schema) and checked at load for every variant: a community or registry pack with a problem is invalid, any other is warned; such a pack's model variants name their `state_fields`, and its model, `max_tokens`, `timeout_ms` and `reasoning_effort` are the deployment's to choose or cap (every spec version) | §5 |
| An owned pack's input schema nests at most 32 deep, chains at most 16 `$ref`s and applies at most 1,000 schema objects to one value, referring back to itself only as a recursive list or tree, with no `unevaluatedProperties` or `unevaluatedItems` below another; a pack from a registry with a reference pack's id and an equal version is that pack (every spec version) | §3, §5 |
| A prefetch template is a read or search relative to the EHR's FHIR server, a wording template's placeholders are bare names, and every text a card summary can give with display names is under 140 characters: a community or registry pack with one that is not is invalid (its FHIR Task text included), any other is warned of its prefetch and card summaries, and `mendari validate` refuses both (every spec version) | §9 |

To migrate a pack:

1. Set `spec_version: "0.2"`.
2. Optionally turn yes/no questions into `type: boolean` (only if they have no third label such
   as `unknown`) and graded questions into `type: score` with `levels`. Changing a label set is
   a MAJOR version (§4); changing only the type while keeping labels is a MINOR version.
3. Where a primary question was really a combination of several criteria, split the criteria
   into member questions and derive the primary one; re-run evaluation and republish the
   benchmark, since the primary question's behaviour changes.
