# Agent manifest

Status: draft. This page specifies an agent: a directory whose manifest, `agent.yaml`, says
`spec_version: "0.4"`, the one spec version Mendari reads. The key words MUST, SHOULD and MAY
are used as in RFC 2119. The reference implementation is `mendari.core.spec` (the manifest),
`mendari.core.agent` (loading an agent and its files), `mendari.core.policy` (a decision's
outcome, the order of the caps and what an action needs), `mendari.core.actions` (checking a
proposed action), the providers in `mendari.providers`, `mendari.standards.fhir` (the FHIR
mapping), `mendari.standards.cds_hooks` (CDS Hooks) and `mendari.evals.written` (written
output). Where this page and the implementation disagree, file an issue; the page is intended to
be normative.

## 1. Purpose

An agent packages one healthcare decision so it can be executed, evaluated, governed and shared
independently of the models that implement it. An agent MUST be self-describing: a reviewer can
determine what it decides, for whom, with what inputs, under what automation limits, what it may
ask to have done, and how well it performed on which data, without reading code.

## 2. Layout

An agent is a directory containing its manifest, `agent.yaml`. A directory that also holds the
name spelled another way, differing only in case (`Agent.yaml`), is refused: an agent has one
manifest. All paths in the manifest are relative to the agent directory and MUST NOT resolve
outside it.

| Path (conventional) | Required | Content |
|---|---|---|
| `agent.yaml` | yes | Manifest (§3) |
| `schemas/input.schema.json` | yes | JSON Schema 2020-12 for the agent's input (§3.3) |
| `fhir/mapping.yaml` | no | FHIR R4 mapping (§9) |
| `logic/`, `fixtures/`, `models/` | per provider | Provider configuration, canned answers and calibrations (§5.2) |
| `data/*.jsonl` | no | Evaluation datasets (§8), clinicians' grades (§15.6) |
| `tests/cases.yaml` | SHOULD | Behaviour tests (§10) |
| `tests/probe.yaml` | MAY | Regression tests `mendari probe --write` keeps, run with the behaviour tests (§10) |
| `tests/probe-unconfirmed.yaml` | MAY | Tests the probe found whose expected answer a model changed: never run until a person confirms each (§10) |
| `evals/benchmark.json` | SHOULD | Published evaluation report (§8) |
| `evals/leaderboard.json` | MAY | Provider configurations compared on the same datasets (§8) |
| `CHANGELOG.md` | SHOULD | Version history, which the Decision Card shows |
| `DECISION_CARD.md` | generated | Rendered Decision Card |

## 3. The manifest

```yaml
spec_version: "0.4"           # the one version Mendari reads, quoted
id: namespace/name            # lowercase letters, digits and dashes
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
  intended_users: [...]       # at least one
  patient_population: ...
  out_of_scope: [...]
  known_failure_modes: [...]
  evidence: [{title, url?, note?}]
  provenance: synthetic | de_identified | site_records   # optional; the status line (§4.3)
inputs: {schema: <path>, fhir: <path>?}
questions:
  <key>:
    type: choice | boolean | score  # default choice (§4.1)
    instructions: ...
    display: What a person reads      # optional, at most 80 characters
    names: {<label>: What a person reads}  # optional, the question's own labels only
    criteria: {<label>: <criterion>}          # choice; optional for boolean
    levels: {<label>: {score, criterion}}     # score only
providers: {<slot>: {answers: [<question>...], use: <variant>, variants: {<variant>: {type, ...}}}}
policy:                       # §6
  primary: <question>
  thresholds: {auto: 0.90, review: 0.50}
  rules: [{name, when: {question, labels, min_confidence, min_probability, min_score, max_score},
           outcome, recommendation, priority, reason}]
  labels: {<label>: {allow_auto, auto_threshold}}
  automation_requires: {<slot>: [<variant>...]}
  fallback: What happens when there is no recommendation.
  default_priority: routine
evaluation:                   # §8
  datasets: {<name>: <path>}
  benchmark: <path>           # in evals/
  benchmark_dataset: <name>
  critical: [{question, label}]
  subgroups: [<attribute>]
  degradation_tolerance: 0.03
  grades: {<rubric>: {dataset: <name>, file: <path>}}   # §15.6
tests: <path>
audit: {store_input: full | hash}
cds_hooks:                    # §9.12
  hook: ...
  title: ...
  description: ...
  prefetch: {<key>: <FHIR query template>}
  card: {summaries, indicators, show, also_show, suggestion}   # §9.13
actions: {<name>: {description, effect, allow_auto, schema}}  # §13
outputs: {<name>: {description, slot or input, max_chars}}     # §15.1
rubrics: {<name>: {output, context_fields, criteria, judge, floors}}  # §15.3
```

The manifest is closed: a key this page does not name is refused, at every level but a provider
variant's, whose keys each provider type names (§5.6).

### 3.1 Validation rules

All MUST hold:

- `spec_version` is the text `"0.4"`, quoted (unquoted, YAML reads a number): any other value
  is refused, and the problem says so.
- `id` is `namespace/name`, each part lowercase letters, digits and dashes, starting with a
  letter or a digit.
- Question keys and labels match `^[a-z][a-z0-9_]*$`. A question has 2–255 labels, each with a
  non-empty criterion. No question is keyed `recommendation` or `priority`: those are the
  templates' built-in placeholders (§9.11).
- Every question is answered by exactly one provider slot. Slots answer only declared questions.
  A slot's `use` names one of its variants.
- `policy.primary` is a question. Rule conditions reference existing questions and labels;
  `min_score`/`max_score` conditions reference score questions. Rule recommendations and
  `policy.labels` keys are labels of the primary question. `automation_requires` names slots,
  each with variants of that slot.
- `thresholds.review ≤ thresholds.auto`, both in [0, 1]. A label's `auto_threshold`, if given,
  is at least `thresholds.review`.
- Agents at risk level ≥ 2 MUST NOT contain policy rules whose outcome is `AUTO`.
- `evaluation.critical` names labels of the questions it names, and `evaluation.benchmark_dataset`
  one of `evaluation.datasets`.
- `version` is a semantic version (`MAJOR.MINOR.PATCH`, optionally followed by `-` and a
  pre-release) of at most 64 characters.
- `license` is an SPDX license expression of at most 200 characters: identifiers (or
  `LicenseRef-…`, optionally after `DocumentRef-…:`) joined by `AND` and `OR`, an exception
  after `WITH`, and parentheses. Its syntax is checked, not each identifier against SPDX's list.
- `code_systems` lists each system once: a `url` (`http(s)://…` or any `urn:…`) and a `name`. A
  code system whose codes are licensed rather than free to redistribute everywhere (CPT,
  SNOMED CT, MedDRA, X12's code lists and CDT) MUST have a `licence_note` saying the terms the
  agent uses it under. An agent any of whose digested files names such a system by its url or OID
  (bare or as `urn:oid:`, a JSON-escaped `\/` read as `/`) MUST declare it so, whatever file
  names it. A bare code value names no system, so no check can find it: declaring the system it
  comes from is the author's to do.
- `cds_hooks.card` is held to §9.13, `actions` to §13, and `outputs`, `rubrics` and
  `evaluation.grades` to §15.

### 3.2 Files

- Every referenced file exists inside the agent. A directory agent MUST NOT hold a symbolic link
  that resolves outside the agent's directory. Hidden entries (a name starting with `.`) and
  `__pycache__` are not part of the agent (not digested, archived or read), so a `.venv` in the
  agent's folder is fine.
- Every file an agent decides or tests with MUST be one its digest hashes (§11). The manifest
  fields naming one are `inputs.schema`, `inputs.fhir`, `tests`, `evaluation.datasets.<name>`,
  `evaluation.grades.<rubric>.file`, `rubrics.<name>.judge.mock_file`, and, in a provider variant
  or a `check`'s member, `config`, `calibration` and `mock_file`. Such a path MUST be one a file
  can have (no NUL character, no name longer than the filesystem allows), and MUST NOT go
  through a hidden entry or `__pycache__` (as written or once links are resolved) nor be in
  `evals/` or a `DECISION_CARD.md`, and the file it reaches MUST be one the digest hashes,
  compared as a file on disk rather than by spelling: on a case-insensitive filesystem
  `EVALS/rules.yaml` is `evals/rules.yaml`, and is refused. A file read under any other key is
  held to the same rule when it is read. Its content MUST be the content the digest hashed: an
  implementation reads each file once, checks its bytes against the digest's hash of that file
  and uses those bytes, so a file edited after the agent was loaded is refused until the agent is
  loaded again.
- A YAML file of the agent (its manifest, mapping, provider configs, fixtures and tests) that
  uses anchors has at most 1,000,000 nodes and 32,000,000 characters of scalar text, counted as
  JSON writes them (a control character counts six), once every alias is expanded (an alias
  counts each time it is used), and no alias inside its own anchor. A file without an anchor has
  no alias, and is not counted. Every value in it is one the safe loader can build: a value it
  cannot (a date such as 2024-02-30, an explicitly tagged `!!bool maybe`) makes the agent
  invalid, named by the file, the error's type and the value's line and column. So does a value
  JSON cannot carry: a string with a lone surrogate (`"\ud800"`), `!!binary`, `!!set`, `!!omap`
  or `!!pairs`. So does a number (an integer or a float, in any base, implicit or tagged) whose
  text, underscores removed, is longer than 4,300 characters or has more than 4 `:` parts (base
  60, as in `1:30:00`), and an integer of more than 4,300 decimal digits: each is refused before
  it is built. A mapping in any YAML file of the agent MUST NOT give one key twice: the agent is
  invalid, named by the file and the line and column of both keys (a YAML loader keeps the last,
  so a reader of the first would be misled about what the agent decides with). A key merged in
  with `<<` may be set again by the mapping that merges it.
- A JSON file of the agent (the input schema, a calibration, a fixture) is JSON in UTF-8 with no
  string holding a lone surrogate, no number that is not finite and no integer too large to be a
  finite number (and so none of more than 4,300 digits). A text file the card reads
  (`CHANGELOG.md`) is UTF-8.
- `evaluation.benchmark` is the one manifest path that MUST be an attestation: in `evals/` (it
  may not exist yet; a `DECISION_CARD.md` elsewhere is refused). A leaderboard
  (`evals/leaderboard.json`) and the card (`DECISION_CARD.md`) are read by those fixed names. A
  benchmark or leaderboard that is not JSON, holds what JSON cannot carry or is not the report the
  Decision Card shows is treated as absent (the card says why), not as an invalid agent.

### 3.3 The input schema

- The input schema is an object at its root, the schema of the input's fields: a boolean schema
  alone (`true` or `false`) is invalid.
- The input schema is self-contained: `$ref`, `$dynamicRef` and `$recursiveRef` start with `#`
  and resolve to a schema (an object or a boolean) in the document; `$schema`, if present, is at
  the root only and names `https://json-schema.org/draft/2020-12/schema`; `$id` is at the root
  only; it holds no `$dynamicAnchor` and no `format: "regex"`. These keys are looked for in the
  whole document, since a `$ref` can make any object in it a schema. Inputs are checked with a
  validator that retrieves nothing, so checking an input never fetches a URL or reads a file.
- The input schema of an agent the deployment owns (the agent bundled with the release, or an
  operator's local agent; one it does not own has the caps and the time budget of §5.4 instead)
  is bounded in shape: its document nests objects and lists at most 32 deep; no path through it,
  following its applicators, holds more than 16 `$ref`s; and it applies at most 1,000 schema
  objects to one value of an input, a schema reached twice (two `$ref`s to it, an `allOf`
  listing two that reach it) counted twice. A schema MAY refer back to itself only as a recursive
  list or tree does: each schema of the loop leads back into it exactly once, the loop passes
  through a keyword that applies to a property or an item (`properties`, `items` and the like),
  and it leads to no other loop. A schema that applies itself to the same value, a node that
  applies its child twice and a binary tree (`left` and `right` both the node) are invalid. A
  schema holding `unevaluatedProperties` or `unevaluatedItems` MUST NOT lead, through any
  applicator, to another schema holding one, nor back to itself: to find what is unevaluated, a
  validator checks again every schema below it, so each one more below another multiplies the
  work.

## 4. Questions

A question is the unit of meaning. Its `criteria` are written for humans (they appear in the
Decision Card) and are also passed verbatim to providers that accept natural-language
instructions (`llm`, `agent`). Changing a label set is a breaking change (MAJOR version).
Rewording a criterion changes behaviour for instruction-following providers and SHOULD be a
MINOR version.

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
from the levels. Only a `score` question has `levels`.

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

The provider's `model` is the model that answered:

- `llm`: the id the endpoint reports answering with (a dated snapshot behind an alias, say)
  when it is well formed; otherwise the configured model, and the call stands.
- `agent`: the `model` its service reports, else the variant's `service_id` with the
  `service_version` the service reports (`<service_id>@<service_version>`), else whichever of
  the two it has.
- `rules`: `rules@` and the fingerprint of its file.
- `check`: `member:model` of the member whose answer was used, the first (§14.4).

The model asked for stays in the deployment's configured providers.

### 4.3 Display names and the status line

**Display names.** `display` names a question and `names` its labels for the people who read
them; without them a key reads as words (`medical_assistant`: "Medical assistant"). They change
no decision.

**Status line.** Every surface that shows an agent (its Decision Card's first line, the
console, its Try it and its review queue, CDS Hooks discovery and cards, the registry and the
Agent Hub) shows a line derived from `card.provenance`, never written by the agent: with
`synthetic`, "Synthetic example. Not clinically reviewed. Not for patient care."; otherwise
"Not clinically reviewed." There is not yet a way to record a clinician's review of an agent's
digest, so no agent can claim one.

## 5. Providers

A provider answers every question in its slot with a probability distribution over the
question's labels. The answer's `label` is the most probable label, and `confidence` is the
provider's confidence in it. Providers answer `boolean` and `score` questions like `choice`
questions, with a distribution over their labels; the runtime adds the expected score.

Variants of a slot are interchangeable implementations of the same contract. Selecting a
variant (`providers={"reader": "rules"}`) MUST NOT change the agent's questions, policy or
tests.

### 5.1 Errors and the deadline

Providers MUST either answer every question in the slot or raise
a provider error with one of these stable, audit-safe codes: `budget_exhausted`,
`credential_unavailable`, `insufficient_credit`, `invalid_endpoint`, `invalid_input`,
`invalid_response`, `not_configured`, `provider_unavailable`, `quota_exhausted`, `rate_limited`,
`time_budget`, `timeout` or `unavailable`. No other code is recorded. Error codes MUST NOT carry
credentials or remote error bodies.

A decision's remote calls share a wall-clock deadline the deployment sets
(`MENDARI_DECISION_DEADLINE_S`, default 120 s): a call that would start after it is not made,
and one under way is cut at it (each wait it makes, a retry included, is bounded by what
remains); either is the provider error `timeout`. Local providers always answer. An agent cannot
set it.

### 5.2 Provider types

| `type` | What it is |
|---|---|
| `rules` | Deterministic patterns with negation handling and optional typo tolerance, on this machine. `config` names its file. |
| `llm` | A chat model through an OpenAI-compatible API, asked for one probability per label, optionally calibrated per question (`calibration`, a `mendari.llm-calibration/1` file of the agent). |
| `agent` | An external HTTP agent service, sent the slot's questions and the input, or only its `state_fields`, whose structured answers are checked against the questions. Offline, with no service configured, it answers from the agent's `mock_file`. It may be calibrated (§14.2), and it may propose actions (§13) and write texts (§15). |
| `check` | A composite of exactly two members, each a variant of another type: the first answers and the second checks (§14.4). |

An `llm` or `agent` variant MAY set `repeat`, to ask each case several times (§14.3). The
`llm` variants of a decision that call the same endpoint and model with the same settings share
one call (`batch`, on by default); a repeated variant shares none.

The `agent` type's request and response are the agent's HTTP contract
([Authoring an agent](../guides/authoring-an-agent.md#6-the-agents-http-contract)): a request
carries the agent's id, the slot, the variant's `service_id`, the slot's questions (each with
its key, type, instructions, labels and criteria), the input, or only its `state_fields`, and
the variant's `system_prompt` and `instructions` when it sets them; a response carries an
answer for every question asked, and MAY carry the `model` and `service_version` that answered,
the `tool_calls` the agent made to reach its answers (kept as evidence), its usage, the actions
it proposes (§13.6) and the texts it wrote (§15.2). A response that is not one the contract
allows is the provider error `invalid_response`, and the slot's questions fail.

A `check`'s second member that fails is recorded as a failure of the slot's questions
(`<member>:<code>`), which caps `AUTO` (§6); a first member that fails fails the slot (§14.4).

### 5.3 The plain form

A `rules` variant reads free text on this machine, and its patterns
read it in its **plain form**: the same words in the characters a keyboard types, so that text
which differs only in typography decides the same:

1. a zero-width space (U+200B) becomes a space, since it separates words, and the other
   invisible format characters are left out (Unicode category Cf: zero-width joiners, the byte
   order mark, the soft hyphen, direction marks), since they sit inside words;
2. a letter and an accent written as two characters become the one character (Unicode NFC),
   unless composing would make the text longer, where the text stays as it is;
3. apostrophes and single quotation marks (U+2018 to U+201B, the prime U+2032, U+02BC, and the
   acute and grave accents U+00B4 and U+0060) become `'`; double quotation marks (U+201C to
   U+201F, the double prime U+2033) become `"`; hyphens, dashes and the minus sign (U+2010 to
   U+2015, U+2212) become `-`; the ellipsis character (U+2026) becomes `.`, or `-` between two
   digits, where a full stop would read as a decimal point;
4. a carriage return before a newline is left out, and every run of white space that holds no
   newline becomes one space: a no-break space, a tab, a lone carriage return, a vertical tab,
   a form feed, and the line and paragraph separators are all white space.

A newline stays a newline, and nothing else changes: case is kept (rules patterns ignore it
themselves), and so are compatibility characters (a ligature, a full-width letter, the micro
sign). The plain form is never longer than the text. A pattern that matched the text as received
matches its plain form too, but for three cases, each of which decides as the same words typed
on a keyboard do: words joined by a format character other than the zero-width space, a
negation cue that a long run of spaces had kept outside a rule's `negation_window`, and a
pattern or a negation that reached across an ellipsis character. An agent's rules patterns are
matched against the plain form, so they MUST be written in it: a character the plain form never
holds, such as a curly apostrophe or a no-break space, matches nothing. The decision's record,
its input hash, the input schema's checks and what a remote provider is sent keep the text as it
was received.

### 5.4 Patterns, and agents the deployment does not own

An agent's patterns (every `rules` pattern, of every variant and `check` member, and every
`pattern` and `patternProperties` key in its input schema) are Python `re` regular expressions,
run as `re` runs them (rules patterns with case ignored). Each MUST compile and be at most 2,000
characters, and an agent MUST hold at most 500 of them; the runtime compiles every one at load,
so an agent with an invalid one is invalid, named by file and rule. `re` has no time limit: a
pattern that backtracks (`(a+)+$`) can take seconds or longer on a crafted input: an agent's
patterns are code its author wrote, and installing an agent means trusting its author.

An agent the deployment does not own is one pulled from a registry, or a version kept in an
organization's agent store, whose digest is not that of an agent bundled with the installed
release. Where an agent was found decides it, never its manifest. Its patterns are written the
same way but run on RE2, which never backtracks, rewritten to mean what they mean in `re` (what
still differs is listed in `core/re2_syntax.py`, and the authoring guide says what to write
instead: [Patterns run on RE2](../guides/authoring-an-agent.md#patterns-run-on-re2)). Such an
agent is invalid when:

- it holds a pattern RE2 cannot run: a lookahead or lookbehind, a back reference, a conditional
  or atomic group, a possessive quantifier, a `$` (not multi-line) with more of the pattern after
  it, or one too large once compiled;
- its input schema uses a keyword outside those whose work Mendari bounds: `unevaluatedProperties`
  or `unevaluatedItems`, which jsonschema works out on its own and with `re`, `contains`,
  `uniqueItems` and the like are refused; `$ref` names one of the root's `$defs`, whose names are
  letters, digits, `_`, `.` and `-`; `required` lists at most 100 names and `enum` at most 500
  values, texts of at most 200 characters;
- its settings multiply its patterns' work: a field named twice in a `text_fields` (a
  provider's or a question's), a `negation_window` over 200 characters or more than 50
  `negation_cues`, an input schema that can run more than 50 patterns on one value or key
  (through `$ref`, `allOf` and the other applicators; each `patternProperties` pattern counts
  three times where `additionalProperties` is also set, which searches them again and for its
  error message) or that refers back to itself where patterns run, more than 500 patterns run
  by its rules on an input (each once for each question it answers and each `check` member
  holding it) and its schema on one value together, or those same runs coming to a size of more
  than 3,000 together, a pattern's size being the characters, classes and `.`s it holds, each as
  often as a count repeats it (`a{1,50}` is 50, `\w+` is 1, alternatives added together): RE2's
  time on each character grows with it.

At run time such an agent refuses an input whose texts are longer than 10,000 characters, alone
or together, or that holds more than 10,000 values or is nested more than 32 deep, before any
pattern runs, and its patterns and schema checks share a time budget of one second of CPU per
decision: past it the decision abstains with the error `time_budget`, for a person (an input the
schema had already found wrong is refused). Which engine an agent runs on is never read from its
manifest; the agent format is the same for every agent.

An agent from a registry whose id is that of an agent bundled with the installed release, and
whose version is equal to that agent's as versions are ordered (by their numbers: `0.4.1-x` and
`0.04.1` equal `0.4.1`), MUST be that agent (its digest): any other is invalid, since it would be
served in the bundled agent's place.

### 5.5 The deployment's settings

A remote provider's endpoint, model and credentials belong to the deployment, which supplies them
in fixed environment variables (`llm`: `LLM_BASE_URL`, `LLM_MODEL`, `AI_GATEWAY_API_KEY`,
`LLM_API_KEY`; `agent`: `MENDARI_AGENT_URL`, `MENDARI_AGENT_TOKEN`). A variant MUST NOT name the
variables a provider reads: an `llm` or `agent` variant (or `check` member) that sets
`api_key_env`, `base_url_env`, `endpoint_env`, `local_key_env`, `model_env` or `token_env` is
invalid. A deployment MAY also list the origins its providers may be called at (in the reference
runtime, `MENDARI_PROVIDER_ALLOWED_ORIGINS`): a call to any other is not made, and the question
fails (`invalid_endpoint`). An organization MAY set its own connection for an agent in the
console ([ADR-0021](../decisions/ADR-0021-an-agents-connection-set-in-the-console.md)), which an
`agent` variant calls before the deployment's. An `llm` variant MAY pin `model` and tune its own
requests (`timeout_ms`, `max_tokens`, `temperature`, `reasoning_effort`); an `agent` variant
MAY set its `timeout_seconds`, and, in an agent the deployment owns, its own `endpoint`.

### 5.6 Closed configs

Each provider type's config is closed: a variant or `check` member MUST set only the keys its
type reads, with values it accepts, and its agent files MUST be ones it can run with (a rules
file of that shape, answering the slot's questions with their labels; a calibration file; an
`agent` variant's canned answers for the slot's questions). The keys of every type are published
as a JSON Schema, [`provider-configs.schema.json`](provider-configs.schema.json), generated from
the code. Every variant is checked at load, the ones an agent does not use by default too: an
agent the deployment does not own (§5.4) with any problem is invalid; any other loads, with the
problems as warnings, and `mendari validate` refuses it.

For an agent the deployment does not own, every `llm` and `agent` variant and member MUST name
its `state_fields` (at least one), an `agent` variant MUST NOT set `endpoint`, and the
deployment chooses what the agent may only suggest: its `model` is ignored, its `max_tokens`,
`timeout_ms` and `timeout_seconds` are held to the provider defaults (`llm` 4,096 tokens and
30,000 ms, `agent` 15 s; a lower value stands), and a `reasoning_effort` above `low` is ignored.
For a version in an organization's agent store, `batch` and an `llm`'s `temperature` are the
deployment's too.

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

1. If the input fails the input schema, or holds a number JSON cannot carry (NaN or an infinity,
   which no schema keyword bounds), the outcome is `BLOCK` and no provider is called.
2. Provider slots answer their questions, and score answers receive their expected `value`.
3. Policy `rules` are evaluated in order over the available answers. A rule's condition
   matches an answer when the answer's confidence is ≥ `when.min_confidence`, its label is in
   `when.labels` (if given), and its expected score is ≥ `when.min_score` and ≤
   `when.max_score` (if given; inclusive). With `when.min_probability`, the label test is
   replaced by a probability test: the answer's total probability over `when.labels` MUST be
   ≥ `min_probability`, whichever label is most likely. This is how an agent escalates on a
   meaningful chance of a critical label (e.g. P(urgent) ≥ 0.35) rather than only when that
   label wins; `min_probability` needs `labels` and cannot be combined with `min_confidence`. A
   condition needs at least one of `labels`, `min_score` or `max_score`. The first matching rule
   determines outcome, recommendation (default: the primary answer's label) and priority
   (default: `default_priority`).
4. Otherwise, on the primary question: confidence ≥ the label's `auto_threshold` (default
   `thresholds.auto`) gives `AUTO`; ≥ `thresholds.review` gives `HUMAN_REVIEW`; else
   `ABSTAIN`. A missing primary answer gives `ABSTAIN`.
5. Caps apply only to `AUTO` and only ever lower it to `HUMAN_REVIEW`: a label with
   `allow_auto: false`; any provider failure in the decision; a slot named in
   `automation_requires` running another variant; the risk level (§7); and what the deployment
   adds (§7): an automation hold, an agent from a registry its organization has not approved or
   that its registry recalls, a value read through a site's translation of its own code (§9.6),
   a list read without a source the site declared its EHR does not offer (§9.7), and, on a
   server with sign-in, answers read from an `agent` variant's `mock_file`. Then the caps the
   providers found (§14), and last the actions' cap (§13.7), in the order §14.5 gives.
6. `ABSTAIN` carries no recommendation. `ABSTAIN` and `BLOCK` carry the policy `fallback`.

`automation_requires: {<slot>: [<variant>...]}` allows `AUTO` only while each named slot runs
one of the listed variants, e.g. only while the slot asks the agent's service, not its offline
rules:

```yaml
automation_requires:
  reader: [agent]
```

Other variants still answer and decide, but every decision goes to a person. A runtime that
does not know which variants are running MUST NOT automate such an agent.

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

An **automation hold** is the deployment's, never the agent's: while one covers a decision (an
organization, an agent, a digest, or the whole deployment), an outcome that would be `AUTO`
becomes `HUMAN_REVIEW` with the same recommendation and priority, and nothing else changes. A
decision over its organization's daily quota is held the same way.

An agent pulled from a registry never automates until its organization approves its digest, nor
while its registry recalls it, and its own rule's `BLOCK` goes to a person instead
([ADR-0012](../decisions/ADR-0012-a-registry-that-shows-only-what-it-checked.md)). Nothing in the
agent lifts it. (`mendari test` and `mendari eval` measure the agent's own policy, without it.)

## 8. Evaluation

Dataset format: JSON Lines. Each line has an `input` (the agent input), optional `labels`
(`{question: label}`), optional `attributes` (subgroup metadata such as `language` or `site`),
and an optional `id`.

**Critical labels** (`evaluation.critical`) name the (question, label) pairs whose true cases
matter most. In the agent's default deployment (no governance approval) a true critical case
MUST NOT be automated: a person sees every one. Under a governance approval (risk levels 2–3),
an automated true critical case MUST carry the correct primary recommendation: an eligibility
screen may automate `not_eligible` on an exclusion it found, never `candidate`. Mendari's tests
hold the agent it bundles and its test fixtures to both, in every configuration that runs
without calling a service, on every dataset the agent declares. Reports, local validation and
the agent gate still count every automated true critical case.

An evaluation report (`format: mendari.eval/1`) MUST include the agent id, version and digest;
the provider variant and model of each slot; the dataset name, SHA-256 and size; per question
accuracy, macro-F1, per-label precision/recall/F1, confusion matrix, calibration (expected
calibration error with reliability bins, and the Brier score); outcome rates; automation
accuracy and false automation rate; expected override rate among reviewed cases; for each
`critical` label, the detection count and the number of true critical cases that were
automated; latency percentiles; cost; subgroup metrics for each configured attribute; and a
threshold sweep. Its result is computed from a per-case ledger and carries each proportion with
its exact interval. Reports MUST NOT contain case inputs. The formats are specified in
[Evaluation reports, ledgers and paired comparisons](evaluation-reports.md).

**Local validation** compares a report on local data with the agent's published benchmark. It
MUST report the overall delta of primary accuracy and SHOULD localize degradation by label
recall, subgroup, calibration and false automation. Any automated true-critical case is always
a finding. An agent pulled from a registry MUST NOT be compared with the benchmark it carries,
which is outside its digest and is its publisher's upload
([ADR-0012](../decisions/ADR-0012-a-registry-that-shows-only-what-it-checked.md)); a report
the caller gives MAY be.

A **leaderboard** (`format: mendari.leaderboard/1`) compares provider configurations. Each
row summarises one report: the configuration (slot variants, naming the model of remote
providers), the agent version and digest, the dataset with its hash and size, primary accuracy,
macro-F1 and ECE, AUTO and false automation, critical detection and automated misses, p50/p95
latency, cost per decision, error count and the report's ledger. A row is keyed by agent digest,
dataset hash, size and configuration, and is ordered by one function
([`mendari.rank/1`](evaluation-reports.md#leaderboards-mendarileaderboard1-and-mendarirank1)),
whose order the card, the command line and the console all list the rows in. Every set a
leaderboard is measured on is public, so they show it as a listing order and print no rank: a
regression check, not a ranking. Rows for one dataset MUST come from the same records. Like
reports, a leaderboard MUST NOT contain case inputs.

A **shared report** (`format: mendari.site-report/1`) is what a site sends a registry about its
own evaluation, specified in
[Evaluation reports](evaluation-reports.md#shared-site-reports-mendarisite-report1). It is a
closed schema of names, flags and whole counts, counted from the run's ledger: the agent (id,
version, digest), the providers and policy, per question its labelled and right cases, its
confusion matrix and confidence deciles, the outcome counts, the false automations, each
critical label's counts and the dataset's size. It MUST NOT contain a rate, a case id, case
text, the dataset's name or hash, who approved a governance override (only whether one
applied), or subgroup cells. Cells of 1 to 9 are suppressed, with their complements, by one
function the sender and the registry both run, and a run on fewer than 100 cases is not
shared; a registry MUST refuse a payload that function would change. Its question, label, slot,
variant and `check` member names are the published agent's own, and a registry MUST refuse a
report that names any other; model names are short ASCII names. A `check` is shared as it is
recorded: its model lists each member's as `member:model`, in the agent's order, joined by
` + `. A registry MUST NOT echo what it refuses.

What an evaluation reports of a calibration and a second signal is in §14.6, and of written
output in §15.7.

## 9. FHIR and CDS Hooks

An agent MAY read its input from FHIR R4 records through a mapping (`inputs.fhir`), and MAY be
served as a CDS Hooks 2.0 service (`cds_hooks`).

### 9.1 The FHIR mapping

```yaml
fhir_version: R4
focus: Patient                    # the resource type a decision is about
focus_where: {path: ..., in: [...]}   # optional: which resources of that type count
                                  # (or {path: ..., code_set: <name>}, §9.6)
resources:                        # the types the record may hold; `required` ones must be there
  Patient: {required: true}
code_sets:                        # optional (§9.6)
  t2d: {system: http://hl7.org/fhir/sid/icd-10-cm, codes: [E11.9, E11.65]}
fields:                           # input field -> one value
  patient.age: {path: Patient.birthDate, transform: age_years}
  notes: {path: DocumentReference.content.attachment, transform: attachment_text}
lists:                            # input field -> a list of objects
  conditions:
    resource: Condition
    fields:
      code: {path: code.coding, code_set: t2d}
      text: {path: code.text}
  medications:
    resources:
      MedicationStatement:
        fields:
          code: {path: "medicationCodeableConcept.coding[system=http://www.nlm.nih.gov/research/umls/rxnorm].code"}
          start: {path: effectivePeriod.start}
          end: {path: effectivePeriod.end}
      MedicationRequest:
        fields:
          code:
            follow: medicationReference
            paths:
              - "Medication.code.coding[system=http://www.nlm.nih.gov/research/umls/rxnorm].code"
              - Medication.code.text
        medication_period: {start: start, end: end}
task: {code_text: ..., performer_system: ..., performer: ..., description: ...}
```

The input MAY be a Bundle, a single resource, or a CDS Hooks prefetch object whose values are
resources or search Bundles. All are flattened, and resources are de-duplicated by type and id.
Only the focus must be unique. The mapping is YAML held to §3.2: among other rules, no mapping
in it gives a key twice. It is checked whenever it is read: when an agent the deployment does
not own loads, when a decision reads FHIR, and by `mendari validate`.

A mapping MAY declare `focus_where: {path, in: [...]}` to say which resources of the focus type
the agent is about, e.g. referrals among ServiceRequests (lab orders are ServiceRequests too):

```yaml
focus: ServiceRequest
focus_where: {path: category[0].coding[0].code, in: ["FR101"]}  # the code a site marks referrals with
```

Other resources of the focus type are ignored. (`FR101` is a made-up referral code; a site lists
the code its own EHR uses.)

### 9.2 Paths

A path is a small FHIRPath subset: dotted element names. A list-valued element yields all its
items, and `[n]` keeps the n-th of everything the path found so far (`[*]` keeps all).
`[system=<uri>]` keeps the items whose `system` is that uri: a coding of one code system
(`code.coding[system=http://loinc.org].code`), or an identifier (`identifier[system=...]`).
The uri MUST be one the agent declares in `code_systems` (§3.1), so a licensed system is
declared with its terms.

A top-level field's path starts with a resource type (`Patient.birthDate`). A list field's
path is relative to its resource (`code.text`). After a `follow`, a path starts with the type the
reference reaches (`Medication.code.text`), or with the field's own type (§9.4).
`focus_where.path` is relative to the focus. Every path is checked when the mapping is read.

### 9.3 Fields

A field gives one value. It has a `path` or `paths`, not both, and optionally:

- `transform`: `join` (every text value, one per line), `age_band`, `age_years` (from a FHIR
  date), `base64_text` (every `data`, decoded and joined, whatever its content type) or
  `attachment_text` (§9.5).
- `map`: what was found, to the agent's value. A value the map does not list gives nothing.
- `code_set`: §9.6. It takes no `transform`.
- `follow`: §9.4.

With `paths`, each path is tried in order, and the first that gives a value is the field's. A
path that finds nothing, or whose value the transform or the map turns into nothing, passes to
the next; once one gives a value, the others are not read (a value through a site's
translation is the one exception, §9.6). A field that gets no value is left out of the input.

### 9.4 Following a reference

`follow` is a path to a Reference: from a resource type in a top-level field
(`Coverage.payor`), relative to the resource in a list field (`medicationReference`). The
reference is resolved inside the record only, and never fetched:

- `#<id>`: a contained resource of the resource that holds the reference;
- the exact `fullUrl` of a Bundle entry (`urn:uuid:...`, or an absolute URL an entry has);
- `<Type>/<id>`, optionally with `/_history/<version>`: that resource among the record's.

Anything else resolves to nothing: an absolute URL no entry has, a reference by identifier
only, or a resource of another type than the field's paths start with. The field's paths are
then read in the resources the reference reached. A `follow` is one hop: what it reaches is
not followed further.

A path that starts with the field's **own type** is read where the field is, as it would be
without `follow`: a list field's resource type, or a top-level field's `follow` path's type
(`Coverage` for `Coverage.payor[0]`). So a field may fall back, in its `paths`' order, from what
the reference reaches to what its own resource says, for an element R4 lets be either a
reference or a value (a MedicationRequest's `medicationReference` or `medicationCodeableConcept`)
or a reference that may carry only an identifier:

```yaml
coverage.payer:
  follow: Coverage.payor[0]
  paths:
    - "Organization.identifier[system=https://payers.example/id].value"   # what it reaches
    - Coverage.payor[0].identifier.value                                  # its own resource
```

A field therefore never reads, through `follow`, a resource of its own type: a reference from a
resource to another of its type is not followed for its paths. A field's reference is resolved
once, whatever number of paths it tries, and a runtime SHOULD resolve it by a lookup rather than
a search of the record, so that mapping a record takes time in proportion to its size. A runtime
MAY add resources it read itself to the record before the mapping runs; the mapping still never
fetches.

A reference that reaches nothing is a **link** when it is `<Type>/<id>` (relative, or at the end
of an absolute URL with no query) of a type the field's paths read through it (not the field's
own type). `#<id>`, `urn:...`, a reference by identifier only, and a reference to another type
(a Coverage's payor that is the patient, for a field that reads an Organization) are not links:
they reach nothing, and are not unreadable. A runtime that records what it could not read counts
a link the record does not hold by why it was not read (§9.5's reasons, and those of a read that
failed), else `not_fetched`.

**Linked reads (the reference runtime).** On a CDS Hooks request with a `fhirServer`, and in the
SMART launch's decide, the reference runtime reads the links a mapping reaches that the record
does not hold, a note's Binary included (§9.5), and adds what it read to the record before the
mapping runs. Each is one read of one resource, of the type the field reads, from that server
only, under its base URL and through its allow-list (a link anywhere else is `off_server`, never
read; a request whose server is off the allow-list is refused before anything is decided, as its
prefetch read is). A decision reads at most 50 note Binaries (§9.5) and, apart from them, 50
followed references (past either, `cap`), each at most 1 MiB once decoded (`too_large`); a
request at most 100 reads, its decisions together, and 60 seconds spent in them (past them,
`cap` or `timeout`, nothing sent); and a link is read once a request whatever its spelling. The
reads run inside the decision's deadline, which starts before them, and the decision's providers
share what they leave. A resource its list leaves out (a MedicationRequest entered in error,
§9.8) is not an item: nothing it links to is read, or counted. A read that fails gives its reason
(`timeout`, `unauthorized`, `not_r4` and the others). A link that is not read makes the decision
ABSTAIN with `could not read <field>: <reason>`, whatever a later path of the field reads in its
own resource. `fhir/decide` and `$decide` never read a link: there it is `not_fetched`, and the
decision abstains.

### 9.5 Attachment text

`attachment_text` reads the path's values as Attachments
(`DocumentReference.content.attachment`). An attachment's content is its inline `data` or, with
only a `url`, a Binary the url names inside the record (resolved as in §9.4). `text/plain` is
read as it is. `text/html` is read as text: scripts and styles dropped, entities decoded, each
block on its own line. Both are read as UTF-8, and a content type's parameters
(`; charset=utf-8`) are ignored. `data` is base64 as R4 has it: spaces and line breaks between
its characters (data wrapped every 76 characters) are allowed. The texts are joined with a blank
line between them.

An attachment that cannot be read adds no text, and the reason is one of three:

| Reason | When |
|---|---|
| `unsupported_type` | Any other content type (RTF, PDF, CDA), or none; a `url` to a resource of another type than a Binary, or to a Binary with no content |
| `undecodable` | `data` that is not base64, or not UTF-8 once decoded |
| `not_fetched` | Only a `url`, to a Binary the record does not hold |

A runtime that reads links (§9.4) gives, for a url it did not read, why: `off_server`, `cap`,
`too_large`, or a failed read's reason. It reads a Binary as FHIR JSON, and only a url that is
`Binary/<id>` (relative, or under the server's base); another url under the server is
`not_fetched`, and one that names another type (relative or absolute) `unsupported_type`, never
read. Its content type is checked first, so an RTF or PDF attachment's url is never read.

A runtime that records what it could not read (a data-coverage record) uses these reasons. The
reference runtime counts each in the decision's data-coverage record and makes the decision
ABSTAIN, with the reason `could not read <field>: <reason>`, as it does for a `base64_text` note
whose data does not decode or that is only a `url`. An attachment with neither `data` nor a
`url` has nothing to read, and counts for nothing.

**Longer than the input allows.** The texts of all of a record's notes, joined, can be longer
than the `maxLength` the agent's input schema gives the field. The reference runtime never cuts
the text (a cut could lose the very sentence a question reads) and does not let the schema
refuse the whole input for it: it leaves the field out of the input and makes the decision
ABSTAIN with `could not read <field>: too_large`, counted once in the data-coverage record.
This applies to a top-level field (`fields`) whose schema property, found through `properties`,
has a `maxLength`. A runtime that reads links (§9.4) reads no more of such a field's notes once
the text it has is longer: the rest are `too_large`, not read.

**Renditions.** R4 makes the repetitions of a DocumentReference's `content` one document in
different formats, and a DiagnosticReport's `presentedForm` semantically equivalent forms of one
report. Where a path reads either, an attachment that cannot be read counts for nothing when
another attachment of the same resource was read as text: an RTF beside the HTML of one note
does not make the decision abstain, and a runtime that reads links reads no other rendition
once one reads. A note whose only renditions cannot be read still counts, and abstains, and so
does any other attachment that cannot be read (two payloads of a Communication are two
contents). A resource none of whose renditions is read counts once: by the reason of its first
text/plain or text/html rendition that has one, else `unsupported_type`.

### 9.6 Code sets

`code_sets` names sets of the agent's own codes: `{<name>: {system, codes}}`. A name is lower
case (`^[a-z][a-z0-9_]{0,63}$`). The system MUST be one the agent declares in `code_systems`,
and a code is listed once.

A field with `code_set: <name>` reads Codings, or CodeableConcepts whose codings it reads. Its
value is the first coding of the set's system whose code the set lists; another code of that
system, or the same code in another system, gives nothing. A `map` may then turn the code into
the agent's value.

`focus_where` MAY name a code set (`{path, code_set}`) in place of `in`: a resource of the focus
type counts when the codings at its path, read as a field's are, give one of the set's codes
(`category.coding` for a referral's category, wherever the agent's coding sits among the
categories' codings). The set's translations count as a field's do, so a site's own code for
the focus finds it.

A runtime MAY give the selector a site's translations into the set: `{system, code, as}`,
where `as` is one of the set's codes. A translation counts only into a code the set lists, and
only for its own set, and a coding of the set's own system wins over a translated one wherever
it is: among the codings one path finds, and across a field's `paths`. A path that gives a
code only through a translation passes to the next, for one of the set's own codes; when no
path gives one, the first translated code is the field's. A record whose own codes give a
field its value is so read the same with translations as without. Translations are
configuration, not part of the agent, so they change no digest. The reference runtime's server
reads them from `MENDARI_SITE_CODES`, one map per organization, checked at startup against the
code sets the agents declare (its command line reads none). A decision records the version and
SHA-256 of the map it read with, and one whose input took a value through a translation, or
whose focus counted only through one, is never AUTO, nor BLOCKed by a rule without a person
([Connecting an EHR](../guides/connecting-an-ehr.md#site-codes)).

### 9.7 Lists

A list gives a list of objects. It has `resource` and `fields` (one object per resource of that
type, its fields' paths relative to it), or `resources`, several types into one list:

```yaml
resources:
  <Type>: {fields: {...}, medication_period: {start: <key>, end: <key>}}
```

Items come in the order the types are given, then the record's order. A resource whose fields
all give nothing is left out.

A runtime MAY leave out a source the deployment declares its EHR does not offer: it reads the
list from its other sources, records so, and never automates on it. Without such a declaration
a source that cannot be read makes the decision ABSTAIN. (The reference runtime's server reads
the declaration from `MENDARI_EHR_DECLARATIONS`, per FHIR base and organization, and applies it
only to a type that is one of several sources of a list, whose other, undeclared source is read,
and that the agent reads nowhere else: [Connecting an
EHR](../guides/connecting-an-ehr.md#what-your-ehr-does-not-offer).)

### 9.8 A MedicationRequest's period

`medication_period: {start: <key>, end: <key>}`, under `resources.MedicationRequest` only,
writes when the drug is taken into two item keys, which the source's `fields` do not also map.
Only the first dosage instruction (`dosageInstruction[0]`) is read, and of its timing only
`repeat.boundsPeriod` and `repeat.boundsDuration`:

- the start is that `boundsPeriod.start`, else `authoredOn`, as written;
- the end is that `boundsPeriod.end`, as written. Else, when the start is a date or a dateTime
  with its day (a year or a month alone is not) and `boundsDuration` gives a number of days,
  the end is the start's day plus that many days, as a date (`YYYY-MM-DD`). Else the end is
  unknown (the key is left out). It is never inferred from the request's status, whether
  `completed` or `stopped`;
- a `boundsDuration` gives a number of days when its `value` is a number of at least 0, its
  `code` is one of the UCUM units below and its `system`, if any, is UCUM's
  (`http://unitsofmeasure.org`). The days are `value` times the unit's days, rounded up to a
  whole day, so the end is never earlier than the duration says. A duration in another unit, or
  given only as text (`unit`), gives none;

  | `code` | Unit | Days |
  |---|---|---|
  | `h` | hour | 1/24 |
  | `d` | day | 1 |
  | `wk` | week | 7 |
  | `mo` | month (UCUM's mean Julian month) | 30.4375 |
  | `a` | year (UCUM's Julian year) | 365.25 |

- a request is left out of the list when its `status` is `entered-in-error`, when its
  `intent` is `proposal`, `plan` or `option` (not an order to give the drug), or when its
  `doNotPerform` is `true` (an order not to give it). Every other request is listed, whatever
  its status, as its own item: requests for one drug, a renewal and the order it renews, are
  not joined.

`dispenseRequest.validityPeriod` is the window in which the drug may be dispensed, not when
the patient takes it, so it is not read. `boundsRange` is not read either. This rule is a
clinical judgement: an agent that adopts it SHOULD say so in its card's known failure modes, and
a clinician should review it before real use.

### 9.9 Agents the deployment does not own

An agent the deployment does not own (§5.4) is held to bounds at load: at most 4 paths a field,
and at most 200 codes a code set. `mendari validate` holds every agent to them. A `follow` is one
hop for every agent.

### 9.10 What a field looks for

A runtime MAY report, for each field and list item key, what it looks for: its paths in the
order tried, the reference it follows, the code systems it selects on and the agent's own codes
it looks for (its code set's, and its map's keys). Such a report comes from the mapping alone,
so it holds no patient's value, id or text. The reference runtime's is
`FhirAdapter.looked_for`, and its data-coverage record (`mendari.data-coverage/1`) counts what
each field found against it: the values and codings at the paths it read (in order, until one
gave the field its value), and with `follow` the references it found, how many it reached and
each link it could not reach by its reason (§9.4). A resource a linked read added counts among
what a reference reached, never among the record's resources of its type. A list of several
types has a record for each type (`sources`), a `medication_period`'s two keys among its fields.
Every decision made from FHIR records it.

### 9.11 The Task, and templates

A decision maps to a FHIR R4 `Task`: `status` `ready` for AUTO or `requested` otherwise,
`priority` from the decision, `businessStatus` carrying the outcome, `focus` and `for`
references, the recommendation as `performerType`, and the decision id as an identifier. The
mapping's `task` section gives the Task's `code_text` and `performer_system`, and MAY set its
`description` and `performer`.

**Templates.** Task `description` and `performer`, and card `summaries`, are templates:
`{recommendation}`, `{priority}` and `{<question>}` (that question's label, e.g.
`{specialty}`) are replaced; an unanswered question reads `unknown`. On a card, a person reads
it, so each label is its display name (the question's `names`, else the label as words:
`lumbar_mri` reads `Lumbar MRI` with `names`, `cardiology` reads `Cardiology`), and no
recommendation reads `manual triage`; a Task keeps the labels. Every text a card summary can
give this way MUST be under 140 characters (at most 139), CDS Hooks' limit, checked when the
agent loads. Any other placeholder is refused when the agent loads. A placeholder is a bare name:
no conversion (`{priority!r}`), format spec (`{priority:>9}`), attribute (`{priority.x}`) or
index (`{priority[0]}`). A Task `performer` template gives at most 10,000 codes (the product of
its placeholders' choices: a question's labels and `unknown`, the recommendation's labels and
`manual-triage`, the priorities), each a concept of the agent's performer CodeSystem; any agent
with more is invalid. An agent the deployment does not own (§5.4) is invalid with a placeholder
that is not a bare name, its Task text included, or with a card summary that can read 140
characters or more; any other agent loads with a warning for either in its card summaries (a
card cuts a summary to 139 characters), its Task text renders as it is, and `mendari validate`
refuses both.

### 9.12 CDS Hooks services

An agent with `cds_hooks` is exposed as a CDS Hooks 2.0 service (id `namespace.name`). Each
`prefetch` template is a read or a search relative to the EHR's FHIR server: `Type/<id>`,
`Type/{{context.<name>}}` or `Type?<query>` (a resource type, then at most one id or context
token, then a query with no `/`, `?`, `#`, backslash or whitespace), with no `..` anywhere: no
absolute URL, `//`, leading `/`, operation (`$everything`) or fragment. An agent the deployment
does not own is invalid with any other; any other agent loads with a warning, and `mendari
validate` refuses it.

The reference runtime fetches a declared prefetch key the request leaves out (or sends empty)
only from a `fhirServer` the deployment allows (`MENDARI_FHIR_ALLOWED_SERVERS`); with no
`fhirServer` the request is refused with `412`, as CDS Hooks 2.0 says. A key whose value is null
has no data and is not fetched. A declared key that cannot be read (a failed read, an
OperationOutcome, a value that is not a resource, a resource or search page the runtime read that
is not R4 JSON), and a `base64_text` attachment that is only a `url` or whose data does not
decode, make the decision `ABSTAIN` before any provider runs, with the reason
`could not read <key>: <reason>` (a closed set of reason codes); refused input still gives
`BLOCK`. The hook still answers, with a card that says so whatever `show` says. Every decision
made from FHIR records what the mapping looked for and found, as counts (`data_coverage`,
`mendari.data-coverage/1`, §9.10).

For the order hooks (`order-select`, `order-sign`), the orders come in `context.draftOrders`:
every draft order that is a focus resource gets its own decision, with the prefetched record,
and its own card. A draft with none of them returns no cards.

The EHR MAY send back what the clinician did with each card
(`POST /cds-services/{id}/feedback`), which is chained on the decision as codes and ids, never a
comment.

### 9.13 Cards

An agent MAY word its cards with `cds_hooks.card`: a `summaries` template per recommendation
(keys are primary labels, `none` for a decision with no recommendation, or `refused`, below),
optional `indicators` (`info`, `warning` or `critical`), a `suggestion` label (default "Create a
task"), and `show`, the recommendations that produce a card at all (e.g. a `patient-view`
service stays silent for patients who are clearly not eligible; without `show`, every decision
produces a card). The reference runtime's cards are CDS Hooks 2.0's: a card's `uuid` is its
decision's id, its one suggestion lets the clinician take at most one
(`selectionBehavior: at-most-one`), and it offers Mendari's override reasons. Its detail is the
agent's status, the notice and one plain sentence for the outcome, never a percentage or the
confidence; where the deployment has a public address, the reasons are behind a link to the
decision's record.

**`refused`.** The key of a decision that refused its input: a `BLOCK` made before any
provider ran, because the input failed the agent's input schema or a standards check under
`enforce`. A template MAY name it in `summaries`, `indicators` and `show` as it names a label.
One that names it in none of them keys such a decision `none`. A primary question MUST NOT have a
label `refused` where the template names the key. The reference runtime's card for it does not
use the `BLOCK` outcome's sentence (that the action must not happen): its detail is the agent's
status, the notice, fixed words that say the input was refused and nothing was checked, and each
reason as the audit record keeps it (where in the input and which rule of the schema, a resource
by its type and position: never a value or an id), whether or not the engine's reasons are behind
a link. A reason that is not why the input was refused (a part of the record that could not be
read, a list read without a source the EHR does not offer) follows apart, after `Also noted:`.

```yaml
cds_hooks:
  card:
    summaries:
      refused: "Referral not checked: Mendari refused its data as invalid, so no urgency or completeness check was made"
    indicators: {refused: warning}
    show: [return_for_info, urgent_review, refused]
```

**`also_show`.** A mapping from a policy rule's name to a card: its `summary` (a template held
to what `summaries` are: bare placeholders the manifest knows, under 140 characters however
filled) and its `indicator` (`info`, `warning` or `critical`; `warning` when left out). A
decision the rule matched (the policy's first matching rule, `rule` in its record) whose key
`show` leaves out still shows a card, with that summary and indicator. A key `show` names keeps
its own card, rule or no rule. It needs a `show` list, and every name MUST be one of the
policy's rules. Nothing about the decision changes: its outcome, recommendation, priority,
review queue and Task are what they would be without it.

```yaml
cds_hooks:
  card:
    show: [submit_ready, needs_attention, not_required]
    also_show:
      possible-red-flag:
        summary: "Possible red flag in the notes: consider urgent imaging and an expedited review"
        indicator: critical
```

## 10. Behaviour tests

`tests/cases.yaml` (the file `tests` names) is a list of `{name, input, expect}`: each case is a
mapping with an `input` mapping, an optional `name` (text) and an `expect` mapping, and no other
key. `expect` holds at least one of `outcome`, `outcome_in` (a non-empty list), `not_outcome`,
`recommendation`, `priority` and `answers` (a non-empty mapping of question to label), and
nothing else, so a misspelt key or an empty expectation cannot pass by accident. Each value is
one the agent can decide: an outcome (`AUTO`, `HUMAN_REVIEW`, `ABSTAIN`, `BLOCK`), a priority
(`routine`, `urgent`, `asap`, `stat`), a label of the primary question for `recommendation` (or
null), and, in `answers`, the agent's own questions and their labels. Tests describe behaviour
that every provider variant is expected to preserve, especially the safety behaviour. Every case
MUST have that shape and be JSON once loaded: a date or a number that is not finite (or, refused
by the loader with its line and column, binary, a set or a string with a lone surrogate) makes
the tests invalid, named by the case's number (and name) and what is wrong. Running an agent's
tests when it has none is a failure, not a pass.

An agent MAY also hold `tests/probe.yaml`: the failures `mendari probe --write` found by
changing the text of the agent's own cases
([Finding failures](../guides/finding-failures.md)), kept as regression tests. The file has
this one place in every agent and no manifest names it. Each of its cases is a behaviour test
as above with one more key, `probe`: a mapping that says where the case came from and holds at
least an `id` (text), which no two cases of the file share. Where the file exists, a runtime
MUST read it after the agent's own tests: its cases run with them (`mendari test`, a draft's
tests in an agent store), are held to the same shape and to the input schema
(`mendari validate`), and are named by file and number. A manifest whose `tests` names this
file reads it once, as the agent's own tests.

`tests/probe-unconfirmed.yaml` holds, in the same shape, the tests whose expected answer a
model changed (a denial the model wrote). They MUST NOT run, and the agent is held to nothing
by them, until a person has read each and `mendari probe --confirm <id>` moves it into
`tests/probe.yaml`. Both files are part of the agent: its digest covers them (§11), so writing
one makes another version.

`mendari validate` also reads every dataset `evaluation.datasets` declares: each line is JSON
with an `input` that matches the input schema as a decision checks it, and each record's
`labels` (if any) map the agent's questions to their labels (§8).

## 11. Identity and versioning

The agent digest is the SHA-256 over every file in the agent (path and content, in sorted order)
except `evals/` and `DECISION_CARD.md`, which are attestations *about* the agent, and hidden
entries and `__pycache__`, which are not part of it (§3.2). Every file the agent decides or
tests with is one the digest hashes, compared as a file on disk, and is used only with the
content the digest hashed (§3.2), so the digest covers everything the agent decides with;
`evaluation.benchmark`, an attestation, is in `evals/`. Every decision and evaluation report
records the digest, so an audit record identifies the exact behaviour that produced it. A
change to any file the digest covers changes the digest: a governance approval of the old
digest no longer applies.

## 12. Audit records

Every decision produces a record with id, timestamp, agent id/version/digest, input (or only its
hash when `audit.store_input: hash`), per-answer provider type/variant/model, label, confidence,
probabilities, evidence and (score questions) value, outcome, recommendation, priority, reasons,
fallback, provider errors, latency, cost and token usage, and any governance override. Human
review results and downstream outcomes are appended as events. Events form a SHA-256 hash chain
that also commits to each decision record, so modification or deletion is detectable. Proposed
actions add their own events (§13.9), and so does a text held for a reviewer (§15.8). A decision
whose agent wrote a text also says in its record where each text stands (`written`, §15.8): a
state, never the text.

## 13. Actions

An agent's answers say what it concluded. Its **actions** are what it asks to have done: a
message sent, a note filed, an order placed. The runtime authorizes actions. It runs none: the
caller runs an action, and only one that was released. The decision behind this section is
[ADR-0023](../decisions/ADR-0023-an-agent-proposes-actions.md).
[ADR-0027](../decisions/ADR-0027-no-free-text-is-released-unread.md) tightened it: an action
whose arguments can hold free text needs a person (§13.3, §13.4), and a review releases it only
by name (§13.8). It added no manifest key, so the spec version did not change: a manifest that
was valid is valid, and what changed is what a runtime does with it.

### 13.1 The `actions` section

```yaml
actions:
  <name>:
    description: What it does, in words a reviewer reads   # 1 to 300 characters
    effect: read | draft | write | contact | order        # §13.2
    allow_auto: false      # optional: never released without a person (§13.4)
    schema: {...}          # optional: a JSON Schema for its arguments (§13.3)
```

Validation rules (all MUST hold):

- A name matches `^[a-z][a-z0-9_]*$` and has at most 64 characters. An agent declares at most
  16 actions.
- `description` is 1 to 300 characters, and not blank. It is documentation and the agent's
  instructions: it is shown on the Decision Card and sent to the agent as written.
- `effect` is one of the five in §13.2.
- `schema`, if given, is held to §13.3.
- An action takes no other key. No key names a gate.
- The declared actions as one request sends them (§13.5) are at most 32,000 characters.

### 13.2 Effects

| `effect` | The action |
|---|---|
| `read` | reads data and changes nothing |
| `draft` | prepares something a person sees before it takes effect |
| `write` | changes the chart or another system of record |
| `contact` | sends something to a patient, or to anyone outside the care team |
| `order` | places an order or a prescription, or changes treatment |

An action that fits two takes the later one in this table. The effect is the author's
declaration, as the risk level is: a runtime cannot check what a tool does, so whoever deploys
an agent reviews its effects on the Decision Card.

### 13.3 The arguments schema

`schema` is a JSON Schema 2020-12 document, written inline, that uses only these keywords:
`type`, `properties`, `required`, `additionalProperties`, `items`, `enum`, `const`,
`minLength`, `maxLength`, `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`,
`minItems`, `maxItems`, `title`, `description`. And:

- every schema in it has a `type`, one name: `object`, `array`, `string`, `integer`, `number`
  or `boolean`;
- every object sets `additionalProperties: false`;
- an object has at most 32 properties, named `^[A-Za-z_][A-Za-z0-9_]{0,63}$`, and `required`
  names only its properties;
- an `enum` lists 1 to 100 values, none an object or a list, texts of at most 200 characters; a
  `const` is no object or list;
- objects nest at most 4 deep, and the schema is at most 4,000 characters as JSON.

It has no `pattern`, `format`, `$ref` or combinator: an action's arguments are written by a
model, and checking them must cost no more than reading them. Any other keyword makes the agent
invalid, named by where it is and the keyword. Without a `schema`, the arguments are any JSON
object within §13.6's limits; an action whose effect is `write`, `contact` or `order` SHOULD
have one.

**Free text.** An action's arguments **can hold free text** unless they are a **menu**: a
fixed set of values the manifest lists. They are a menu when all of these hold:

- the action has a `schema`: without one nothing checks its arguments;
- every string is limited to an `enum` or a `const`. Nothing else limits a string: a schema
  has no `pattern` and no `format`, and a length does not count, since a dose fits in a few
  characters. So an identifier that is not enumerated is free text;
- every number is limited to an `enum` or a `const`, or is an `integer` between a lower and
  an upper bound that admit at most 100 values, as many as an `enum` may list: a list the
  manifest could have written out. A number nothing lists can be any dose, and one of a
  million is not a choice from a list;
- there is no array: a list puts a text together out of its items, however each is limited;
- the schema admits at most 1,000,000 different arguments in all: the values of each property
  multiplied together, a property that is not `required` counting once more for its absence.
  Past that, the properties spell a text a character at a time.

A runtime MUST decide this from the manifest alone, by these rules, wherever in the schema a
string, a number or an array is. What an action whose arguments can hold free text needs before
it is released is in §13.4, and how a review releases it in §13.8. An action meant to be
released without a person takes a menu: every string and every number an `enum` or a `const`
(or an integer in a range of at most a hundred), and no array.

For example:

```yaml
spec_version: "0.4"
# ...
actions:
  look_up_medications:
    description: Read the patient's active medication list.
    effect: read
  draft_reply:
    description: Draft a reply for staff to read and send themselves.
    effect: draft
    schema:
      type: object
      additionalProperties: false
      required: [message]
      properties:
        message: {type: string, minLength: 1, maxLength: 2000}
  send_reply:
    description: Send a reply to the patient through the portal.
    effect: contact
    schema:
      type: object
      additionalProperties: false
      required: [message]
      properties:
        message: {type: string, minLength: 1, maxLength: 2000}
  queue_refill:
    description: Put a refill request in the pharmacy queue for a prescriber to sign.
    effect: write
    allow_auto: false
    schema:
      type: object
      additionalProperties: false
      required: [medication]
      properties:
        medication: {type: string, minLength: 1, maxLength: 200}
        days_supply: {type: integer, minimum: 1, maximum: 90}
  renew_prescription:
    description: Renew the prescription as it was last written.
    effect: order
  assign_queue:
    description: Put the message in the refill, scheduling or front-desk queue.
    effect: write
    schema:
      type: object
      additionalProperties: false
      required: [queue]
      properties:
        queue: {type: string, enum: [refill, scheduling, front_desk]}
```

Of these six, only `assign_queue` takes a menu: its one string is an `enum`. `draft_reply`,
`send_reply` and `queue_refill` each take a string that is not enumerated, and
`look_up_medications` and `renew_prescription` have no schema. In `queue_refill`,
`days_supply` alone would be a menu of ninety values: an integer between two bounds, no more
of them than a list may hold.

### 13.4 What an effect needs

The runtime sets this, and an agent cannot change it but by tightening
([ADR-0002](../decisions/ADR-0002-runtime-enforced-risk-caps.md), ADR-0023). Each declared
action has a **gate**:

| Gate | Effects | Released |
|---|---|---|
| `decision` | `read`, `draft` | with any decision that is not `BLOCK` |
| `auto` | `write`, `contact`, with arguments that hold no free text | with an `AUTO` decision; otherwise by the decision's review (§13.8) |
| `person` | `order`; `write`, `contact` with arguments that can hold free text (§13.3) | never without a person: by the decision's review (§13.8) |

A gate only tightens, in four ways:

- `allow_auto: false` gives an action the gate `person`, whatever its effect. No key loosens a
  gate: `allow_auto: true` is the default and changes nothing, on an `order` too.
- For an agent the deployment has not vouched for (one from a registry before the organization
  approves its digest, or while its registry recalls it), `read` and `draft` take the gate
  `auto`: nothing it proposes is released without a person.
- **Arguments that can hold free text (§13.3) make the gate `auto` the gate `person`.** A
  runtime MUST NOT release a `write` or a `contact` with such arguments without a person, nor a
  `read` or a `draft` with them of an agent the deployment has not vouched for: an automated
  decision would send words nobody read to a chart or to a patient. A `read` or a `draft` of an
  agent the deployment vouches for keeps the gate `decision` (a read changes nothing, and a
  person sees a draft before it takes effect), and an `order` had the gate `person` already.
- A proposal that a second signal did not agree on has the gate `person`, whatever its effect
  (§13.7).

Nothing in a manifest, and nothing an agent proposes, loosens a gate. A runtime MUST NOT
release an action a stricter reading of this section would not release. It MAY be stricter.

Since a decision is `AUTO` only where the agent's risk level allows it (§7), so are `write` and
`contact` released without a person only there, and only with arguments that hold no free text:
at levels 0 and 1; at levels 2 and 3 with the deploying organization's recorded approval, which
covers the agent's actions as it covers its answers; at level 4 never.

### 13.5 The request

The agent contract (§5.2) carries an agent's declared actions in each request to an `agent`
variant, in the manifest's order:

```json
"actions": [
  {"name": "draft_reply", "description": "Draft a reply for staff to read and send themselves.",
   "effect": "draft", "schema": {"type": "object", "additionalProperties": false,
   "required": ["message"], "properties": {"message": {"type": "string", "minLength": 1, "maxLength": 2000}}}},
  {"name": "renew_prescription", "description": "Renew the prescription as it was last written.",
   "effect": "order"}
]
```

`schema` is present when the manifest gives one. `allow_auto` is not sent: the gate is not the
agent's to read. An agent with no declared action gets no `actions` key.

### 13.6 The response

The agent MAY propose actions with its answers:

```json
"actions": [
  {"name": "draft_reply", "arguments": {"message": "Your refill request is with the pharmacy."}},
  {"name": "queue_refill", "arguments": {"medication": "lisinopril 10 mg", "days_supply": 30}}
]
```

- At most 16. Each is an object with `name` and, optionally, `arguments` (an object; `{}` when
  left out), and no other key.
- `name` has the shape of an action's name (§13.1).
- `arguments` is at most 16,000 characters as JSON, nests at most 8 deep, and holds only what
  JSON carries.
- Two proposals with the same name and the same arguments are one.

A response that breaks any of these is not one the contract allows: the provider error
`invalid_response`, and the slot's questions fail, as for any malformed answer. A proposal that
is well formed but names an action the manifest does not declare, or whose arguments do not
match its schema, is not an error: that action is blocked and the decision is not automated
(§13.7).

Proposing is asking. An agent MUST NOT run an action it proposes whose effect is `write`,
`contact` or `order`. `tool_calls` is unchanged: what the agent reports having done to reach its
answers, kept as evidence.

In a `check` (§14.4), the first member's proposals are the slot's, as its answers are.

### 13.7 The decision's result

A decision of an agent that declares actions, or whose agent proposed any, has `actions`: one
entry per proposal, in the order proposed (an empty list when none was).

```json
{"id": "0f6c2c1e-…", "name": "queue_refill", "effect": "write",
 "disposition": "held", "reason": "awaiting_review",
 "arguments": {"medication": "lisinopril 10 mg", "days_supply": 30},
 "arguments_hash": "hmac-sha256:9f1c…:5b0e…"}
```

`disposition` is `released` (the caller may run it), `held` (it waits for the decision's
reviewer) or `blocked` (it MUST NOT be run). The caller runs released actions only. An
undeclared action has `effect: null`. An action whose arguments do not match its schema also
has `problems`: where, and which rule of the schema, never a value. `arguments_hash` is hashed
as the input hash is (§12): keyed where the deployment has an input hash key.

**The actions' cap.** The policy (§6) applies, last, one more cap that only lowers `AUTO` to
`HUMAN_REVIEW`: a proposed action that could not be released with an `AUTO` decision. That is
an undeclared action, arguments that do not match their schema, or an action whose gate is
`person`: one the manifest tightens, an `order`, or one whose arguments can hold free text
where its gate would be `auto` (§13.4). The reasons say which, naming only declared actions,
each followed by `; a person decides`. An action never released without a person gets the
first of these that applies:

```text
action 'queue_refill' is never released without a person (allow_auto: false); a person decides
action 'renew_prescription' (order) is never released without a person; a person decides
action 'send_reply' can carry free text and is never released without a person; a person decides
```

The recommendation and the priority do not change, a decision already going to a person keeps
its outcome (and gets the reason), and a `BLOCK` stands and says nothing more. Then each
proposal's disposition follows from the outcome:

| The decision | gate `decision` | gate `auto` | gate `person` |
|---|---|---|---|
| `BLOCK` | blocked `decision_blocked` | blocked `decision_blocked` | blocked `decision_blocked` |
| no recommendation (`ABSTAIN`, or a review without one) | released `with_decision` | blocked `no_recommendation` | blocked `no_recommendation` |
| `AUTO` | released `with_decision` | released `automated` | (the cap above made the decision `HUMAN_REVIEW`) |
| `HUMAN_REVIEW` with a recommendation | released `with_decision` | held `awaiting_review` | held `awaiting_review` |

An undeclared action is blocked `undeclared`; arguments that do not match, `invalid_arguments`.
An action is held only on a decision that awaits review and has a recommendation: only there is
there someone to wait for.

A proposal that a second signal did not agree on (§14.3, §14.4) has the gate `person`, whatever
its effect: it is held for the decision's reviewer where the table holds, blocked where it
blocks, and never released without a person.

### 13.8 Review, and asking again

**Review.** When a reviewer resolves a decision, its held actions are resolved with it, each
once:

- A label that is not the recommendation denies every held action (`denied`, `overridden`),
  whatever else the review names.
- A reviewer MAY refuse any held action and still confirm the label (`denied`,
  `reviewer_denied`): the review is then no override.
- Confirming the label releases the others (`released`, `confirmed`), **but free text only by
  name, and what a reviewer may not have been shown.** A runtime MUST release a held action
  only when the review names that action, where:
    - its arguments can hold free text (§13.3);
    - a caller asked for it after the decision was made ("Asking again", below): its reviewer
      may have read the decision before then, so the label alone does not speak for it;
    - or the review withholds one of the decision's texts (§15.8), which holds every held
      action of that decision: a manifest does not say which action uses which text.

  A held action the review had to name and did not is `denied` (`not_named`), and the review
  is still no override.

So a held action whose arguments are a menu, that came with the agent's answer, on a decision
none of whose texts is withheld, is released when the reviewer confirms the label, unless the
review refuses it. The reference server takes the ids to refuse as `deny_actions` and the ids
to release by name as `release_actions`; an id that is no held action of the decision, or one
in both lists, refuses the whole review (`409`) and nothing is recorded. Its review queue and a
decision's record mark each action whose arguments can hold free text (`free_text: true`) and
say how each was proposed (`source`: `answer` or `request`), so whatever shows a review knows
which actions to name. A resolved action does not change again: a release is final.

**Asking again.** A caller MAY ask, for a decision already recorded, whether one action with
these arguments may run. The reference server exposes it as
`POST /api/agents/{namespace}/{name}/actions` (`{decision_id, name, arguments}`), and
`mendari mcp` as the tool `authorize_action`. A blocked action is not an error of the call.

- An action already proposed on that decision (the same declared name, the same arguments)
  answers as it stands now, and nothing new is recorded. So a caller holding a held action
  asks again to learn that it was released, and a caller about to run a released action asks
  to check that these are the arguments that were released.
- A new action is checked and gated as §13.7 says, by the decision as it stands: on an automated
  decision a `person` action is blocked (`no_reviewer`: nobody is waiting), a `write` or a
  `contact` whose arguments can hold free text among them, and an `auto` action is blocked
  while the agent may not automate now (`not_automated`: a hold, an approval no longer in
  force, a recall) or after an earlier proposal on that decision was refused
  (`earlier_refusal`); on a decision that awaits review, an `auto` or a `person` action is
  held with the rest (blocked where the decision has no recommendation), and the same review
  resolves it, releasing it only by name; on a decision already reviewed, `auto` and
  `person` actions are blocked (`review_closed`); on a decision made by another version of the
  agent, everything is (`agent_changed`).
- There is no authorization without a decision. An agent behind the contract proposes with its
  answer.

### 13.9 Audit events

Each proposal is one event on its decision, `action.proposed`, chained after
`decision.created` in the same transaction (or when a caller asks, §13.8): the action's id, its
name and effect when the manifest declares it (else null), the disposition, the reason, the
arguments' hash, whether it came with the answer or was asked for, and who asked. A declared
action's proposal also says whether its arguments can hold free text (§13.3), whatever its
gate: `free_text`, `true` or `false`. A proposal of a declared action without the key was
recorded before proposals said either way, and a runtime MUST read it, while its action is
held, as one whose arguments can: nothing says they cannot. A review reads that key and how
the action was proposed (`source`) to know which held actions it releases only by name
(§13.8). Each held action's resolution is one `action.resolved`: its id,
name, effect, disposition, reason and the reviewer.

```json
{"action_id": "b2…", "name": "queue_refill", "effect": "write", "disposition": "held",
 "reason": "awaiting_review", "arguments_hash": "hmac-sha256:9f1c…:5b0e…",
 "source": "answer", "slot": "reader", "free_text": true,
 "note_ref": {"id": "…", "sha256": "…"}, "org": "default"}
```

```json
{"action_id": "b2…", "name": "queue_refill", "effect": "write", "disposition": "released",
 "reason": "confirmed", "reviewer": "rn-1", "org": "default"}
```

An event never holds the arguments, nor a name the manifest does not declare: both are the
agent's text and can hold patient data. A runtime MAY keep them apart from the chain, and MUST
then keep them as it keeps the decision's input. The reference runtime keeps them as the
event's note (`note_ref` is its commitment): sealed under PHI mode `encrypt`, in the clear
apart from the chain under `off`, not at all under `hash` or for an agent whose
`audit.store_input` is `hash`; read as inputs are (each read chained), erased with the
decision's kept input, and removed by retention.

### 13.10 An example exchange

The manifest of §13.3, at risk level 0, automating `refill` from confidence 0.90.

Mendari to the agent (the request of §5.2, with §13.5's `actions`):

```json
{"agent_id": "my-clinic/portal-agent", "slot": "reader", "service_id": "portal-agent",
 "questions": [{"key": "request", "type": "choice", "instructions": "What does the patient ask for?",
                "choices": ["refill", "appointment", "other"], "criteria": {"refill": "…"}}],
 "input": {"message": {"body": "Could you refill my lisinopril? I am running low."}},
 "actions": [{"name": "look_up_medications", "description": "Read the patient's active medication list.", "effect": "read"},
             {"name": "draft_reply", "description": "…", "effect": "draft", "schema": {"…": "…"}},
             {"name": "send_reply", "description": "…", "effect": "contact", "schema": {"…": "…"}},
             {"name": "queue_refill", "description": "…", "effect": "write", "schema": {"…": "…"}},
             {"name": "renew_prescription", "description": "…", "effect": "order"},
             {"name": "assign_queue", "description": "…", "effect": "write", "schema": {"…": "…"}}]}
```

The agent:

```json
{"answers": {"request": {"value": "refill", "confidence": 0.95}},
 "actions": [{"name": "draft_reply", "arguments": {"message": "Your refill request is with the pharmacy."}},
             {"name": "queue_refill", "arguments": {"medication": "lisinopril 10 mg", "days_supply": 30}}]}
```

The decision. The answer alone would be `AUTO`; `queue_refill` is `allow_auto: false`, so a
person decides:

```json
{"id": "7d0b…", "outcome": "HUMAN_REVIEW", "recommendation": "refill", "priority": "routine",
 "reasons": ["confidence 0.95 >= auto threshold 0.90",
             "action 'queue_refill' is never released without a person (allow_auto: false); a person decides"],
 "actions": [
   {"id": "a1…", "name": "draft_reply", "effect": "draft", "disposition": "released",
    "reason": "with_decision", "arguments": {"message": "Your refill request is with the pharmacy."},
    "arguments_hash": "sha256:…"},
   {"id": "b2…", "name": "queue_refill", "effect": "write", "disposition": "held",
    "reason": "awaiting_review", "arguments": {"medication": "lisinopril 10 mg", "days_supply": 30},
    "arguments_hash": "sha256:…"}]}
```

The caller shows the draft. `queue_refill` takes a string that is not enumerated
(`medication`), so confirming the label alone would deny it (`not_named`). A nurse reads what
it would queue, confirms `refill` and names the action:

```json
{"final_label": "refill", "reviewer": "rn-1", "release_actions": ["b2…"]}
```

The answer of `POST /api/review/7d0b…`:

```json
{"id": "7d0b…", "final_label": "refill", "human_override": false,
 "actions": [{"id": "b2…", "name": "queue_refill", "effect": "write",
              "disposition": "released", "reason": "confirmed"}]}
```

The caller, about to queue the refill, asks with the arguments it holds
(`POST /api/agents/my-clinic/portal-agent/actions`):

```json
{"decision_id": "7d0b…", "name": "queue_refill",
 "arguments": {"medication": "lisinopril 10 mg", "days_supply": 30}}
```

```json
{"id": "b2…", "decision_id": "7d0b…", "name": "queue_refill", "effect": "write",
 "disposition": "released", "reason": "confirmed", "arguments_hash": "sha256:…",
 "repeated": true}
```

## 14. A calibrated confidence and a second signal

Three keys of a provider variant: `calibration` on an `agent` variant (§14.2), `repeat` on an
`agent` or an `llm` variant (§14.3), and the composite type `check` (§14.4). Each can find a
reason that a decision may not be automated, and none can make one automated. The decision
behind this section is
[ADR-0024](../decisions/ADR-0024-a-checked-confidence-and-a-second-signal.md).

### 14.1 What a cap is

A reason that a decision may not be automated is a **cap**. After the policy of §6 has decided, a
runtime MUST apply the caps the decision's providers found (§14.5 gives the order):

- a decision that is `AUTO` becomes `HUMAN_REVIEW`, and each cap is added to its reasons,
  followed by `; a person decides`;
- a decision that already goes to a person (`HUMAN_REVIEW`, `ABSTAIN`) keeps its outcome,
  recommendation, priority and rule, and gets each cap the same way;
- a `BLOCK` keeps them too, and each cap is added to its reasons as it is: a block is final,
  and no person decides.

A cap MUST NOT be lifted by a governance override, by the risk level, or by any key of the
manifest, and a runtime MUST NOT offer a manifest key that disables one. Nothing in §14 can make
a decision `AUTO` that the policy alone would not have.

A **deciding question** is the primary question, a question named in `evaluation.critical`, or
a question a policy rule's `when` reads. A disagreement (§14.3 and §14.4) is a cap only on a
deciding question; on any other question it is written in the answer's evidence.

### 14.2 `calibration` on an `agent` variant

```yaml
providers:
  reader:
    answers: [request, urgent_symptoms]
    use: agent
    variants:
      agent:
        type: agent
        service_id: portal-agent
        state_fields: [message.subject, message.body]
        calibration: models/reader-agent-calibration.json
```

`calibration` names a file of the agent, so the agent's digest covers it. The file is
`mendari.agent-calibration/1`:

```json
{
  "format": "mendari.agent-calibration/1",
  "service": {"id": "portal-agent", "version": "2.1.0", "model": null},
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

A reader of the file MUST find `format`, `service.version` (a string), `temperatures` (each a
positive finite number, by question) and `fitted_on` with `dataset`, `cases` and `date`. A file
without them is not a calibration, and a variant that names it MUST NOT run. The other keys are
a record of the fit.

**The map.** For a question with a temperature T, the distribution p the provider would have
given becomes p'(label) proportional to p(label)^(1/T), and the answer's confidence is p' of
the answer's own label. When the agent's response gave only a confidence c for its answer over
K labels, p is c for that label and (1 - c) / (K - 1) for each other label, so the map scales
log(c (K - 1) / (1 - c)) by 1 / T. The answer's label MUST be kept as the agent gave it; the map
keeps the order of the labels, so no other label has a larger p'. A question with no temperature
is left as reported.

**The version.** A runtime MUST compare, for each call, the variant's `service_id` and the
`service_version` in the agent's response with `service.id` and `service.version` of the file.

- Equal: the map is applied.
- Not equal, or the response has no `service_version`: the map MUST NOT be applied, and the
  decision carries the cap
  `the agent changed since its confidence was calibrated (was X, is Y)`, where X is the file's
  version and Y the reported one, or `not reported`. When the two `service_id`s differ, X and Y
  each give the id and then the version.

Answers read from the variant's `mock_file` are not the agent's confidence: the map MUST NOT be
applied to them, and they carry no version cap.

**The fit** (`mendari calibrate <agent> --slot S --variant V`) asks the live agent about each
case of a labelled dataset once and fits each question's T by minimising the negative
log-likelihood of the true label. A fitter:

- MUST NOT fit on canned answers, on a service that reports no `service_version`, or on one that
  reported more than one version while it was asked;
- MUST NOT fit a question on fewer than 100 labelled cases the agent answered, and MUST leave a
  question unfitted when no temperature is the best one (the best is at the edge of its search);
- MUST record the dataset's name, its sha256, the number of cases and the date, and the
  `service_id` and reported `service_version`;
- SHOULD be given a dataset that no evaluation reports on (an agent's `calibration` set), and
  SHOULD say so when it is given one of the agent's evaluation sets.

### 14.3 `repeat` on an `agent` or an `llm` variant

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
  label, whose confidence (after its calibration, §14.2) is lowest;
- when fewer than `repeat` runs gave that label, the runs disagreed. A run that failed gave no
  label and MUST count as a disagreement. On a deciding question the decision carries the cap
  `A of N runs agreed on <question>`, followed by ` (F failed: <codes>)` when F runs failed.

When every run gave the label, nothing else changes: agreement MUST NOT raise a confidence.
When every run failed, the slot fails with the first run's error.

Each run MUST be charged to every limit a single call is charged to (a daily budget, an
organization's quota), counted in the decision's usage, and held to the decision's one
deadline; the decision's tokens and cost are the sum of the runs. A run that was sent and did
not answer leaves the decision's cost unknown.

A repeated `llm` variant MUST NOT share a call with another slot (§5.2, `batch`).

Answers read from a `mock_file` cannot vary. They are asked once; the evidence says
`repeat: asked once, canned answers cannot vary, so no agreement is claimed`; no cap is added
and no agreement is reported.

**Proposed actions.** The runs of an `agent` variant MAY each propose actions (§13.6). The slot
proposes each distinct one (a name with its arguments), in the order first proposed:

- one that every one of the `repeat` runs proposed is taken as proposed;
- one that fewer runs proposed is **disputed**. A run that failed proposed nothing. The
  decision carries the cap `A of N runs proposed action <name>`, or
  `the N runs proposed action <name> with different arguments` when every run proposed the
  action and not with the same arguments. A disputed proposal has the gate `person` (§13.7): it
  is held for the decision's reviewer, and MUST NOT be released without one, whatever its
  effect.

A cap names a declared action only (`an action the manifest does not declare` otherwise), and
never an argument.

### 14.4 The `check` composite

```yaml
      checked:
        type: check
        members:
          agent: {type: agent, service_id: portal-agent, state_fields: [message.body]}
          second: {type: llm, state_fields: [message.body]}
```

`check` is a composite of exactly two members, in order: the first answers and the second
checks. Members are ordinary variants and MUST NOT be composites.

- The slot's answers are the first member's, unchanged: label, confidence and distribution.
- The second member is asked the same questions about the same input. Where its label differs
  from the first member's on a deciding question, the decision carries the cap
  `<first> and <second> disagree on <question> (<first>: <label>, <second>: <label>)`, the
  members named as the manifest names them.
- Where the labels are equal, nothing changes: agreement MUST NOT raise a confidence.
- A second member that fails is a failed member: `<second>:<code>` is recorded on each of the
  slot's questions, and the policy does not automate (§6).
- A first member that fails fails the slot. The second member MUST NOT answer in its place.

Budgets, batching, usage, cost and the deadline reach both members. A member MAY carry
`calibration` and `repeat`; its caps hold beside the check's.

**Proposed actions.** The first member's proposals are the slot's, as its answers are. The
second member's own are never taken: it MUST NOT propose in the first member's place.

- Where the second member is one that proposes actions (an `agent` variant), the two are
  compared. A proposal of the first member that the second did not make, with the same
  arguments, is **disputed**: it has the gate `person` (§13.7), so it is held for the decision's
  reviewer and never released without one. Any difference between the two is a cap:
  `<first> proposed action <name> and <second> did not`,
  `<second> proposed action <name> and <first> did not`, or
  `<first> and <second> proposed action <name> with different arguments`.
- A second member that failed checked nothing: every proposal of the first is disputed, with
  the cap `<second> failed, so what <first> proposed to do was not checked`.
- A second member that cannot propose (an `llm` variant, for one) checks the answers and not
  the actions: the first member's proposals are taken as proposed, each under its own gate.

### 14.5 The order of the caps

The policy (§6) applies its rules and its confidence thresholds, then the caps that only make a
decision less automated (a label that is never automated, a provider's failure, a slot running
another variant than `automation_requires` names, the risk level, and what a deployment adds).
Two steps come after all of those, in this order, and a runtime MUST apply them in it:

1. **The caps the providers found** (§14.1), themselves in this order: the calibration's version
   cap (§14.2), then repeated runs (§14.3), then a check (§14.4). They are about the answers the
   policy just decided from: how far the confidence can be believed, then whether the answer
   held when it was asked again.
2. **The actions' cap** (§13.7): what the agent asks to do, once what it answered is settled.

Each step turns `AUTO` into `HUMAN_REVIEW` and changes no other outcome, no recommendation, no
priority and no rule. Each adds its reasons after the ones already there, each once, and each
reason ends `; a person decides` wherever a person does (so not on a `BLOCK`). Several caps
read as one list:

```json
"reasons": [
  "confidence 0.97 >= auto threshold 0.90",
  "the agent changed since its confidence was calibrated (was 2.1.0, is 2.2.0); a person decides",
  "2 of 3 runs agreed on request; a person decides",
  "action 'renew_prescription' (order) is never released without a person; a person decides"
]
```

No step can raise an outcome, and no combination of them can: on the same answers, a decision
that is `AUTO` with every cap applied is `AUTO` with none. (A calibration's map, §14.2, is not a
cap: it is a measured correction of the confidence and may move it either way. What only
lowers is its version cap.)

### 14.6 What an evaluation reports

Not part of the manifest, and stated here because a reader of an evaluation meets it. A report's
`run` block MAY hold:

- `calibration` (`mendari.calibration-report/1`): for each `agent` and `llm` variant the run
  decided with, whether it is calibrated and on what; and for the variant whose answers are a
  slot's own, each question's expected calibration error and reliability table on the run's
  labelled cases, before the calibration and after it;
- `second_signal` (`mendari.second-signal/1`): for each repeated variant and each check, the
  cases it disagreed on (on a deciding question, or on a proposed action), how many of those
  were wrong or critical and how many were right, and its extra calls; and how many cases the
  second signals kept from automation.

Both are outside the result that a report's `result_sha256` covers
([evaluation reports](evaluation-reports.md)): they are worked out from what the run's
decisions kept in memory, as the operating point is.

## 15. Written output

An agent's answers are labels. A **written output** is a text it writes: a draft reply, a
summary, a note. Its **rubrics** say how clinicians and a model judge grade it, and an
evaluation says when the judge's grades are believed. The decision behind this section is
[ADR-0025](../decisions/ADR-0025-a-judge-is-trusted-by-measured-agreement.md), and
[the guide](../guides/written-output.md) walks through it. What a decision does with a text is
[ADR-0027](../decisions/ADR-0027-no-free-text-is-released-unread.md)'s: it is handed only to a
person (§15.8). That decision added no manifest key, so the spec version did not change.

### 15.1 Written outputs

```yaml
outputs:
  <name>:
    description: What the text is, in words the agent and a grader read   # 1 to 500 characters
    slot: <provider slot>      # the agent behind this slot writes it; or
    input: <dotted field>      # the input already holds it
    max_chars: 4000            # optional: 1 to 8,000, default 4,000
```

Validation rules (all MUST hold):

- A name matches `^[a-z][a-z0-9_]*$` and has at most 64 characters. An agent declares at most 8
  outputs.
- An output names exactly one of `slot` and `input`.
- `slot` names a provider slot that has a variant of type `agent`. Only an agent writes: a
  `check`'s member does not count.
- `input` is a dotted field of the input, such as `draft`. No agent is called to evaluate such
  an output.

A case with no text (the agent wrote none, the input was refused, the slot failed) has no
output. It is counted, and never graded.

### 15.2 The agent contract for written output

A request to an `agent` variant of a slot that has declared outputs carries them:

```json
"outputs": [{"name": "reply_draft", "description": "A short reply ...", "max_chars": 1200}]
```

The response MAY carry what it wrote, by name:

```json
{"answers": {"...": "..."}, "outputs": {"reply_draft": "Thank you for your message. ..."}}
```

- `outputs` is optional, and so is each output. A name the manifest does not declare for that
  slot, a value that is not text, or a text longer than its `max_chars` makes the response
  invalid: the slot fails closed, as for a malformed answer.
- Control characters other than the newline and the tab become a space, and so do the
  characters that are not seen and change what is: every code point Unicode says is not shown
  by default (its `Default_Ignorable_Code_Point` property: the soft hyphen, zero-width and
  invisible characters, fillers, the bidirectional marks, embeddings, overrides and isolates,
  the variation selectors, tag characters), the line and paragraph separators (U+2028,
  U+2029), the interlinear annotation characters (U+FFF9 to U+FFFB) and the blank braille cell
  (U+2800). A reviewer releases the text they read, and with one of these a screen shows one
  thing while a channel that drops it says another, or the text carries words no eye sees.
  Four of the first group stay, which a script or an emoji needs: the joiners (U+200C, U+200D)
  and the selectors that say how an emoji is drawn (U+FE0E, U+FE0F). A text that is then
  blank is no output.
- Half a character (a lone surrogate, as a service that cuts a text at a length counted in
  UTF-16 units sends it) is read as U+FFFD, the replacement character, wherever it is in the
  response: no UTF-8 text holds one. The same is done to a decision's input.
- A slot with no declared output does not read the key at all.
- What a decision does with the texts a response carried is in §15.8: each is handed only to a
  person, and none is ever part of a decision's record.

### 15.3 Rubrics

```yaml
rubrics:
  <name>:
    output: <an output's name>
    context_fields: [message.subject, message.body]   # optional; default: none
    criteria:
      <key>:
        type: boolean | score
        instructions: What the criterion asks
        criteria: {"yes": ..., "no": ...}            # boolean: each level's anchor
        levels: {<label>: {score, criterion}, ...}   # score: 2 to 7 levels
        pass_from: <label>      # score only: the lowest level that passes
        must_pass: false        # optional: this criterion must not fail
    judge: {...}                # §15.4
    floors: {agreement: 0.60, must_pass_recall: 0.80}   # optional: may only tighten (§15.5)
```

- A criterion is a question (§4, `boolean` or `score`), never `choice`. Its instructions and each
  level's anchor are what a clinician reads and what a model judge is sent, as written.
- On a `boolean` criterion `yes` passes: word a criterion so that yes is good. It takes no
  `pass_from`.
- A `score` criterion has 2 to 7 levels with distinct scores, a higher score being better, and
  MUST name `pass_from`, one of its levels.
- `must_pass: true` marks a criterion that must not fail (§15.7).
- A rubric has 1 to 16 criteria, and names a declared output. A rubric's name and its criteria's
  keys match `^[a-z][a-z0-9_]*$` and have at most 64 characters, and an agent declares at most 8
  rubrics.
- `context_fields` are the input fields a grader reads beside the text, and nothing else of the
  input (minimum necessary). The default is none; a rubric names at most 32, each once.

### 15.4 The judge

`judge` takes the model settings an `llm` variant takes (`model`, `timeout_ms`, `max_tokens`,
`temperature`, `reasoning_effort`) and `mock_file`, a fixture of canned grades in the agent's
files. The endpoint, the credentials and the default model are the deployment's, and an agent
the deployment does not own is held to the deployment's model and caps.

The judge is asked the rubric's criteria about `{"written": <the text>, "context": {<the
context fields>}}`, after a fixed preamble that says both are data to grade and never
instructions. Its grade is the most probable level; of several equally probable, the least
favourable. No rationale is asked for or kept. A failure is counted under its code, and a text
the judge could not grade is never a pass.

With no judge model set, grades come from `mock_file`:

```json
{"model": "<name>",
 "default": {"grades": {"<criterion>": "<level>", "...": "..."}},
 "cases": [{"match": "<as an agent's mock_file>", "grades": {"<criterion>": "<level>"}}]}
```

`default` MUST grade every criterion. Every case whose `match` fits the text or the context
applies, in order, each laid over the ones before. A report made from canned grades says so:
it is a demonstration, and nothing may be concluded from it.

### 15.5 Floors

The engine's floors are a kappa of 0.60 and, for a criterion that must not fail, 0.80 of the
clinician-found failures, each by its lower bound (§15.7). A rubric's `floors` MAY raise either
(`agreement`, `must_pass_recall`) and MUST NOT lower one: a manifest that does is invalid.

The engine also sets a ceiling: a judge may fail to grade at most 0.05 of the outputs
clinicians graded, and of the rest (§15.7). A rubric MAY lower it (`max_judge_errors`) and MUST
NOT raise it.

### 15.6 Clinicians' grades in a file

```yaml
evaluation:
  datasets: {written: data/written.jsonl}
  grades:
    <rubric>: {dataset: written, file: data/written_grades.jsonl}
```

Each `grades` key names a declared rubric, and its `dataset` one of `evaluation.datasets`. The
file is `mendari.written-grades/1` ([evaluation reports](evaluation-reports.md)): its first line
names the rubric (its name, and the SHA-256 of the rubric as a grader reads it) and the dataset
(its name and SHA-256); each other line is one grader's grades of one item. Grades given under
another wording of the rubric, or of another dataset, are refused. Grades may also be given in
the console, in a grading round ([the guide](../guides/written-output.md)).

### 15.7 What an evaluation of written output reports

Per criterion, on the outputs both a clinician and the judge graded (`mendari.written/1`):

- **Agreement over the levels**: Cohen's kappa with linear weights on the levels' declared
  scores (plain Cohen's kappa on a yes/no criterion), exact, with a percentile-bootstrap
  interval over cases; and raw agreement as k of n. Where two clinicians graded an output and
  differ, the less favourable grade stands.
- **Agreement on pass or fail.** What is published of a judge is a pass share, so agreement
  over the levels is not enough: a judge one level kinder at the pass line agrees closely on
  the scale and disagrees on who passes. A report therefore also gives, on the same outputs:
  plain Cohen's kappa on pass or fail, with its interval (the same number as the kappa above on
  a yes/no criterion); the judge's pass share beside the clinicians'; and an exact two-sided
  McNemar test on the discordant pairs (a binomial test at 0.05), which says whether the judge
  leans lenient or strict.
- **Failures found**, for a criterion that must not fail: of the outputs clinicians failed,
  those the judge failed too. An output the judge could not grade counts as one it did not
  find.
- **What the judge could not grade**: how many of the outputs clinicians graded, and how many
  of the rest. Such an output is never a pass.
- **Clinician with clinician**, on the outputs two clinicians graded: the weighted kappa, as
  the reference. It gates nothing.
- **A state**:
  - *insufficient evidence* when fewer than 50 outputs were graded by both; or fewer than 10
    clinician verdicts fall on either side of the pass line; or either kappa or its interval
    cannot be estimated; or, for must-not-fail, clinicians found fewer failures than a judge
    that found every one would need (17 at 0.80);
  - *not validated* when, the evidence being sufficient, any of these holds: the lower bound
    of the kappa over the levels, or of the kappa on pass or fail, is under the agreement
    floor; for must-not-fail, the exact lower bound of the failures found is under its floor;
    the judge leans lenient (the McNemar test rejects symmetry and it passes more outputs than
    clinicians do); or it could not grade more than 0.05 of the outputs clinicians graded, or
    of the rest;
  - *validated* otherwise. A judge that leans strict stays eligible, and the report flags it.
- **The judge's grades for the outputs no clinician graded**, only for a validated criterion,
  with how many of them it could not grade. Otherwise that figure MUST be withheld (null), with
  the state and its reasons; it MUST NOT be shown with a caveat instead.

A state MUST NOT be decided on fewer than 2,000 bootstrap resamples: with a handful the
interval is a few draws, not a bound. An implementation MUST NOT let whoever reads the result
choose the bootstrap's seed after grades exist: `mendari written eval` uses a constant, and a
grading round uses the seed fixed and recorded when the round was made. A report states its
seed and its resamples.

What is withheld is the report's figure. The run's ledger (`mendari.written-ledger/1`, written
beside a report and kept with a grading round) still holds each grade the judge gave, by case
hash: it is the record a result is recomputed from, so whoever holds a ledger can count them.
Publish a report; treat a ledger as the working record it is.

A report holds counts, coefficients, case hashes and graders' ids. It MUST NOT hold a text, a
context field or a case's id.

### 15.8 A text is handed only to a person

What an agent wrote (an output whose source is a `slot`) is free text nobody has read. A
runtime MUST NOT hand it to anyone but a person who decides, and carrying one MUST NOT change
the decision: a label, and an action whose arguments are a menu (§13.3), automate as they
would without it. An output whose source is `input` is no part of this: the input already holds it.

**Where a text stands.** A decision whose agent wrote a text has `written`, in its answer and
in its record: a `state` for each output the agent wrote, by the output's name.

```json
"written": {"reply_draft": {"state": "held"}}
```

| `state` | The decision | The text |
|---|---|---|
| `held` | awaits a person: `HUMAN_REVIEW` or `ABSTAIN` | is that person's draft, kept for them as below |
| `not_released` | is `AUTO` or `BLOCK` | is handed to nobody, and kept nowhere |

A decision whose agent wrote nothing has no `written`. `written` never holds a text, and
neither does a record, a reason, a log line or a chained event.

**The draft.** An answer MAY carry a `held` text itself, as the draft of the person who
decides, and MUST NOT carry a `not_released` one. The reference server's
`POST /api/agents/{namespace}/{name}/decide`, and `mendari decide` without `--fhir`, carry it
under `outputs`, by name:

```json
{"id": "7d0b…", "outcome": "HUMAN_REVIEW", "recommendation": "refill",
 "written": {"reply_draft": {"state": "held"}},
 "outputs": {"reply_draft": "Thank you for your message. ..."}}
```

Its FHIR routes give `written` and never a text, and its CDS Hooks cards, `$decide` and
`mendari mcp` give neither. A caller MUST treat a `held` text as a draft: it is not released
until the decision's review says so. For a `not_released` text a caller has nothing to send:
what reaches a patient or a chart goes through an action a person releases (§13.8).

**Keeping.** A runtime with an audit trail keeps each `held` text for the decision's reviewer
as it keeps an action's arguments (§13.9): apart from the chain, as the note of one event on
the decision, `output.held`. The events are chained after `decision.created` in the same
transaction, one per text in the order of the outputs' names, before the proposals. An event
holds the output's name and the note's commitment, never the text:

```json
{"output": "reply_draft", "note_ref": {"id": "…", "sha256": "…"}, "org": "default"}
```

A runtime MUST keep a held text only where it keeps the decision's input, and MUST NOT keep a
`not_released` one, which adds no event. The reference runtime keeps a held text sealed under
PHI mode `encrypt` and in the clear apart from the chain under `off`; under `hash`, and for an
agent whose `audit.store_input` is `hash`, it keeps none, and `note_ref` is null. A kept text
is read as inputs are (each read chained), erased with the decision's kept input, and removed
by retention.

**Review.** Whoever resolves the decision's review (§13.8) gives each held text a verdict:
`release` (it may be sent or filed as written) or `withhold` (it may not). A review MUST give
one for every held text and name no other; one that does not is refused whole, and nothing is
recorded. That holds where the text was not kept, too: the reviewer read the draft where the
caller showed it. A decision that holds no text takes no verdict. Each verdict is one event,
`output.resolved`, chained after `review.resolved` and before the actions' resolutions:

```json
{"output": "reply_draft", "verdict": "released", "reviewer": "rn-1", "org": "default"}
```

`verdict` is `released` or `withheld`. A verdict does not depend on the label: a reviewer MAY
release a text while correcting the label, or withhold one while confirming it. A withheld
text also holds back the decision's actions: no held action is then released unless the review
names it (§13.8).

The reference server takes the verdicts as `texts`, by the output's name, and answers with each
under `outputs`:

```json
{"final_label": "refill", "reviewer": "rn-1", "texts": {"reply_draft": "release"}}
```

```json
{"id": "7d0b…", "final_label": "refill", "human_override": false,
 "outputs": [{"name": "reply_draft", "verdict": "released"}]}
```

A review that leaves a held text out, or names an output the decision does not hold, is a
`409`; a value that is neither verdict is a `422`. Its review queue and a decision's record
list the held texts as `outputs`: each one's `name`, its `state` (`held`, then `released` or
`withheld`, read from the chain) and its `text_status`: `open`, with the `text`, for whoever
may read inputs (under PHI mode `off`, whoever may read the decision); `sealed` for anyone
else; `erased`; `not_kept` where none was kept; and `missing` where its note is gone with no
erasure chained, which `mendari audit verify --notes` reports. The record's own `written` is
what the decision said when it was made, and does not change.

**What this does not cover.** An answer's evidence (a rationale of at most 500 characters, and
the names of the tools the agent called, §5.2) goes back with every decision. It is evidence
for a reviewer, never a text for a patient or a chart, and a caller MUST NOT use it as one. An
evaluation is not affected: it grades every text the agent wrote and hands none on (§15.7). No
judge clears a text when a decision is made: a judge's grades are an evaluation's.
