# Writing a Decision Pack: a guide for clinicians

A Decision Pack is a clinical decision written down so a computer can apply it and a person can
check it: the questions you would ask, what each answer means, when a person must look, and the
evidence that it works. You write it in YAML, a plain-text format, with no programming. This guide
builds a small pack that routes patient portal messages, one step at a time, in about 30 minutes.

You need Mendari installed and a text editor. Until the first release is on PyPI, install it
from a clone of the repository (`git clone https://github.com/mendari-hq/mendari.git`, then
`uv sync` in it and put `uv run` before each command below, or `pip install .` in it); after
that, `pip install mendari`. Every command is run in a terminal. Your pack can live in any
folder: commands take its path, and `MENDARI_PACKS_PATH` lets `mendari serve` find it.

## 1. Start from the template

```bash
mendari new refill-router --id my-clinic/refill-router --publisher "My Clinic"
```

This creates a folder with everything a pack needs:

| File | What it is |
|---|---|
| `pack.yaml` | The pack: what it decides, its questions, its policy |
| `logic/rules.yaml` | How the offline provider answers the questions |
| `schemas/input.schema.json` | The shape of the input (here, a portal message) |
| `tests/cases.yaml` | Behaviour tests: an input and what must happen |
| `data/examples.jsonl` | A small labelled dataset, for evaluation |

A pack is self-contained. Every file it names stays inside its folder (a symbolic link out of
it is refused), and the input schema refers only to its own parts: a `$ref` starts with `#`,
`$schema` (2020-12) and `$id` appear only at the top, and `$dynamicAnchor` and `format: regex`
are not allowed. Checking an input never fetches a URL or reads another file. The schema's shape
is bounded too: it nests at most 32 levels deep, a chain of `$ref`s runs at most 16 long, one
value is checked against at most 1,000 schema objects (two `$ref`s to a schema that holds two
more double it at each step), and a schema refers back to itself only as a recursive list or
tree does, once, through `properties` or `items` (a node that lists its children is fine; one
whose `left` and `right` are both the node is not). At most one schema on any path holds
`unevaluatedProperties` or `unevaluatedItems`: each validates again everything below it, so one
inside another multiplies the work. The version is a
semantic version of at most 64 characters, and YAML anchors are fine within reason: a file that
uses them may expand to at most 1,000,000 nodes and 32,000,000 characters of text (as JSON
writes it: a control character counts six) once its aliases are counted where they are used. A
number may be at most 4,300 characters long (underscores aside) and have at most 4 `:` parts. A
`.venv` or other hidden folder beside the pack's files is not part of the pack, and is ignored.

Everything the pack decides or tests with must be inside its digest, the identity a governance
approval covers: the files named by `inputs.schema`, `inputs.fhir`, `policy`, `tests`,
`evaluation.datasets` and each provider's `config`, `library`, `model_file` and `calibration`.
None of them may go through a hidden folder or `__pycache__`, or be in `evals/` or a
`DECISION_CARD.md`, however the path is spelled (on macOS and Windows `EVALS/` is `evals/`).
`evaluation.benchmark` is the opposite: a published result about the pack, it lives in `evals/`.
Behaviour tests must be plain JSON values: quote a date (`"2024-01-01"`). Each is a mapping with
an `input` mapping, an `expect` mapping and, optionally, a `name`. `expect` says what the pack
must decide with at least one of `outcome`, `outcome_in`, `not_outcome`, `recommendation`,
`priority` and `answers` (a mapping of question to label), and nothing else: a misspelt key, an
empty `expect`, or a value the pack can never decide (an unknown outcome, label or question) is
named by `mendari validate` and `test`, which also fails when a pack has no tests (spec §10).
`mendari validate` reads every dataset `evaluation.datasets` declares as well. No YAML file of the pack may use `!!binary`, `!!set`, `!!omap`, `!!pairs` or
a lone surrogate escape (`"\ud800"`); `mendari validate` names the file, line and column. If you
edit a file of a pack a server has loaded, restart the server: until then reads of
the edited file are refused, since it is no longer what the pack's digest describes.

It already works. Check it:

```bash
mendari validate refill-router       # the pack is well formed
mendari test refill-router           # its behaviour tests pass
```

## 2. Say what the pack is for

The top of `pack.yaml` is the pack's identity and its **card**, the part a reader sees first.
Write it the way you would explain the tool to a colleague:

```yaml
card:
  intended_use: >-
    Route portal messages to the refill queue, the scheduling queue or the front desk, and
    send messages describing same-day symptoms to a nurse.
  intended_users:
    - Front-desk and nursing staff
  patient_population: Adult patients who write to the clinic's portal, in English.
  out_of_scope:
    - Deciding whether a refill is appropriate; a clinician decides.
  known_failure_modes:
    - Keyword rules miss phrasings they don't list. Evaluate on your own messages before use.
```

`known_failure_modes` matters most. List every way you know the pack can be wrong. Reviewers
and future users read it before they trust the pack.

Say what the pack was built from with `provenance` (`synthetic`, `de_identified` or
`site_records`). Mendari derives the line every surface shows from it: a pack built on synthetic
data reads "Synthetic example. Not clinically reviewed. Not for patient care." Give each question
a `display` name, and a label a name in `names` when its key does not read well as words
(`lumbar_mri`: "Lumbar MRI"), so people read words instead of keys. The dashboard shows these
names in Try it's result and on the review queue's cards, with each key one click away ("Show
codes"): a name is your claim about what a label means, and a reviewer can check it against the
key the decision really chose. The engine's detail (each label's probability, the providers, the
thresholds and the reasons as the engine gave them) is in the decision record.

## 3. Choose the risk level honestly

```yaml
risk:
  level: 0
  category: administrative
  rationale: >-
    The decision is which queue reads a message first. The pack never replies to patients or
    makes a clinical judgement, and a message that may describe same-day symptoms always goes to
    a nurse for review.
```

| Level | For | Precedent | What Mendari does |
|---|---|---|---|
| 0 | Administrative work: which team or queue sees something first, with no clinical judgement in the outcome and anything possibly urgent sent to a person | `mendari/patient-inbox-router` | May automate |
| 1 | Clinical information: reading clinical facts to extract them, check them or decide where they go | `mendari/referral-router`, `mendari/prior-auth-preflight` | May automate; every decision is audited |
| 2 | Clinical support: eligibility, suggestions, patient-facing clinical content | `mendari/clinical-trial-prescreener`, `mendari/ai-guard` | A person reviews every decision, unless your organization records an approval |
| 3 | Patient-specific prediction | | As level 2 |
| 4 | Time-critical, diagnostic or treatment decisions | | A person always decides |

This pack only chooses which queue reads a message, and sends anything that may be urgent to a
nurse, so it is level 0 like the inbox router. A pack that read the message to decide something
clinical (whether a refill is due, what a symptom means) would be level 1 or higher. The runtime
enforces the level, whatever the rest of the pack says. When in doubt, choose the higher level.

## 4. Write the questions in words

Questions are the heart of a pack. Each has **criteria**, written for people: they document the
pack, and they are the instructions a language model reads if one is used.

```yaml
questions:
  request:
    instructions: What does the patient ask for?
    criteria:
      refill: A refill or renewal of a medication they already take.
      appointment: To book, move or cancel a visit.
      other: Anything else, or more than one thing.
  urgent_symptoms:
    type: boolean
    instructions: Does the message describe symptoms that need attention today?
    criteria:
      "yes": For example chest pain, trouble breathing, or signs of a stroke.
      "no": No such symptoms.
```

Tips:
- One question, one idea. "Is it urgent and about a refill?" is two questions.
- Always include a way out (`other`, `unknown`), so nothing is forced into a wrong box.
- A `boolean` question answers `yes` or `no`. A `score` question has ordered levels.
- A question can be **derived** from others with a decision table. The trial prescreener's
  `eligibility` is: any criterion `no` means not eligible, any `unknown` means review.

## 5. Decide how questions get answered

A **provider** answers questions. The template uses keyword **rules**, which work offline and
explain every answer:

```yaml
questions:
  urgent_symptoms:
    default: "no"
    default_score: 2.0
    labels:
      "yes":
        - {name: chest-pain, pattern: "chest (pain|pressure|tightness)", weight: 6, negatable: true}
```

Each matching pattern adds its weight to a label's score, so a message saying "chest pressure"
scores `yes`. `negatable: true` means "no chest pain" doesn't count, and the audit record names
the pattern that matched.

Patterns are Python regular expressions (`re`), matched with case ignored. `mendari validate`
compiles every one, so a typo is reported with its file and rule; a pattern may be at most
2,000 characters, and a pack may hold 500 (input-schema `pattern`s and `patternProperties` keys
included). Keep them simple:
`re` has no time limit, and a pattern that can match the same text in many ways, such as
`(a+)+$`, can take seconds on a short message and hold the worker that runs it. (A community
pack's patterns run on RE2, which never backtracks: see
[Contributing a community pack](contributing-a-pack.md#patterns-run-on-re2).) So do the
patterns of a pack you publish to a registry, once someone pulls it: a pulled pack runs on the
same engine, with the same limits (the caps, 10,000 characters of input and a one-second budget
per decision), unless it is a copy of a reference pack of the puller's release
([ADR-0009](../decisions/ADR-0009-bounded-evaluation-for-packs-the-deployment-does-not-own.md)).
Check yours before you publish: `mendari pack verify` loads an archive as a puller will, and the
registry refuses a publish whose patterns RE2 cannot run. A changed copy of a reference pack
needs a version of its own: under the reference pack's id and version it would take that pack's
place, so the registry, `mendari pull` and `mendari pack verify` refuse it.

Rules are the floor, not the ceiling. The same pack can switch to a language model, to Jev, to a
CQL library your organization already has, or to computed criteria for dates and lab values.
Each is a provider **variant** in `pack.yaml`, and you compare them on the same data (step 8).

## 6. Set the policy: when a person must look

```yaml
policy:
  primary: request
  thresholds:
    auto: 0.90    # automate from this confidence
    review: 0.50  # below this, no suggestion at all
  labels:
    other:
      allow_auto: false  # the front desk always reads these
  rules:
    - name: same-day-symptoms
      when: {question: urgent_symptoms, labels: ["yes"], min_probability: 0.3}
      outcome: HUMAN_REVIEW
      priority: urgent
      reason: The message may describe symptoms that need a nurse today.
  fallback: The front desk reads the message.
```

Every decision ends in one of four outcomes: `AUTO`, `HUMAN_REVIEW`, `ABSTAIN` or `BLOCK`.

- **Rules come first:** the first one that matches decides.
- **`min_probability: 0.3`** acts on a 30 % chance of urgent symptoms, not only when urgent is
  the most likely answer. Be sensitive where a miss is costly.
- **`fallback`** says what happens when the pack can't decide. It is usually today's process.

## 7. Write behaviour tests

A test is an input and what must happen. Write one for every rule, and one for every mistake
you find later:

```yaml
- name: chest pain goes to a nurse today, whatever else the message asks
  input: {message: {body: "Please refill my inhaler. Also I have had chest pain since last night."}}
  expect: {outcome: HUMAN_REVIEW, priority: urgent, answers: {urgent_symptoms: "yes"}}
```

```bash
mendari test refill-router
```

## 8. Measure it on real-looking data

`data/examples.jsonl` has one labelled case per line: the input, and the right answer to each
question.

```bash
mendari eval refill-router --dataset examples
```

The report shows:
- accuracy per question and calibration;
- how many decisions were automated, and how many of those were wrong;
- how many critical cases were caught (`evaluation.critical`: here, every `urgent_symptoms = yes`).

Make this run the pack's published benchmark. Write it to a file (its per-case ledger, from which
its figures are computed, goes beside it in `evals/ledgers/`), then add
`benchmark: evals/benchmark.json` under `evaluation:` in `pack.yaml`:

```bash
mendari eval refill-router --dataset examples --baseline none --out refill-router/evals/benchmark.json
```

Before anyone uses the pack, **evaluate it on your own site's messages** (de-identified, or in a
secure environment). Mendari compares the result with the benchmark, says where it degrades,
and you read every misclassified case:

```bash
mendari eval refill-router --dataset my-site.jsonl
```

To compare providers, add a variant to the slot in `pack.yaml` (for example
`llm: {type: llm, state_fields: [message.subject, message.body]}` under `reader.variants`). Then
run
`mendari leaderboard refill-router --run reader=rules --run reader=llm --dataset my-site.jsonl`.
A remote variant's endpoint and key come from the deployment, never from the pack: a variant
may pin `model`, but one that names environment variables (`api_key_env`, `endpoint_env` and
the like) is refused.

`state_fields` lists the input fields a model variant sends (dotted, `message.body`): the
minimum necessary. Without it the whole input is sent. Name them on every `llm` and `jev`
variant and composite member: a pack you publish to a registry or contribute as a community
pack is refused without them, and there its pinned `model` gives way to the deployment's, its
`max_tokens` and `timeout_ms` are held to the provider defaults and a `reasoning_effort` above
`low` is dropped.

Each provider type takes only its own keys, listed in the
[provider config schema](../spec/provider-configs.schema.json). `mendari validate` refuses a
variant with any other key, or one that cannot run (a label the question does not have, a model
trained for another question), even a variant the pack does not use by default; a server still
loads your own pack with it and lists it under `pack_warnings` in `/api/health`, but a registry
refuses it.

The same holds for a pack's CDS Hooks and FHIR wording. Each `cds_hooks.prefetch` template is a
read or search relative to the EHR's FHIR server (`Patient/{{context.patientId}}`,
`Condition?patient={{context.patientId}}`): no absolute URL, `..`, operation (`$everything`) or
fragment. A card summary or Task text uses bare placeholders (`{specialty}`, never
`{specialty!r}` or `{specialty:>20}`). `mendari validate` refuses anything else and a registry
refuses it; your own server lists such a prefetch or card summary under `pack_warnings`.

## 9. Publish the card

```bash
mendari card refill-router --write       # writes DECISION_CARD.md
```

The Decision Card gathers the pack's purpose, risk, questions, policy and measured performance
into one page. Share it with your AI or clinical decision support committee. At risk level 2 or
3, their approval (recorded in the dashboard) is what allows automation.

## 10. Share it

- **With everyone, as a community pack:** open a pull request with the folder in
  `community-packs/<name>/`. [Contributing a community pack](contributing-a-pack.md) is the
  path, from the proposal to the merge; CI checks the pack and shows its card.
- **In your own deployment:** keep the folder anywhere and put its parent on
  `MENDARI_PACKS_PATH`. The packs in `packs/` are the ones Mendari maintains.
- **On a registry:** `mendari push refill-router --key my-key.pem` signs the pack and
  publishes it, and others `mendari pull my-clinic/refill-router` and verify it.

## Checklist before real use

- [ ] The card's intended use, population and failure modes are honest and complete.
- [ ] The risk level is the highest that could apply.
- [ ] Every policy rule has a behaviour test.
- [ ] Local evaluation shows no missed critical cases, and you read every misclassified case.
- [ ] A clinician other than the author reviewed the criteria.
- [ ] At level 2 or 3, the committee's approval is recorded.

The full format is in the [Decision Pack specification](../spec/decision-pack-0.2.md).
