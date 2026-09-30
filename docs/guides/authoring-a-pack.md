# Writing a Decision Pack: a guide for clinicians

A Decision Pack is a clinical decision written down so a computer can apply it and a person can
check it: the questions you would ask, what each answer means, when a person must look, and the
evidence that it works. You write it in YAML, a plain-text format, with no programming. This guide
builds a small pack that routes patient portal messages, one step at a time, in about 30 minutes.

You need OpenHealth installed (`pip install openhealth`, or `uv sync` in a checkout) and a text
editor. Every command is run in a terminal.

## 1. Start from the template

```bash
openhealth new refill-router --id my-clinic/refill-router --publisher "My Clinic"
```

This creates a folder with everything a pack needs:

| File | What it is |
|---|---|
| `pack.yaml` | The pack: what it decides, its questions, its policy |
| `logic/rules.yaml` | How the offline provider answers the questions |
| `schemas/input.schema.json` | The shape of the input (here, a portal message) |
| `tests/cases.yaml` | Behaviour tests: an input and what must happen |
| `data/examples.jsonl` | A small labelled dataset, for evaluation |

It already works. Check it:

```bash
openhealth validate refill-router    # the pack is well formed
openhealth test refill-router        # its behaviour tests pass
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

## 3. Choose the risk level honestly

```yaml
risk:
  level: 1
  category: clinical information
  rationale: >-
    The pack only routes messages inside the clinic. A wrong route costs staff a minute; a
    message with urgent symptoms always goes to a nurse.
```

| Level | For | What OpenHealth does |
|---|---|---|
| 0 | Administrative work | May automate |
| 1 | Clinical information (routing, extracting) | May automate; every decision is audited |
| 2 | Clinical support (eligibility, suggestions) | A person reviews every decision, unless your organization records an approval |
| 3 | Patient-specific prediction | As level 2 |
| 4 | Time-critical, diagnostic or treatment decisions | A person always decides |

The runtime enforces this, whatever the rest of the pack says. When in doubt, choose the higher
level.

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
openhealth test refill-router
```

## 8. Measure it on real-looking data

`data/examples.jsonl` has one labelled case per line: the input, and the right answer to each
question.

```bash
openhealth eval refill-router --dataset examples
```

The report shows:
- accuracy per question and calibration;
- how many decisions were automated, and how many of those were wrong;
- how many critical cases were caught (`evaluation.critical`: here, every `urgent_symptoms = yes`).

Make this run the pack's published benchmark. Write it to a file, then add
`benchmark: evals/benchmark.json` under `evaluation:` in `pack.yaml`:

```bash
openhealth eval refill-router --dataset examples --baseline none --out refill-router/evals/benchmark.json
```

Before anyone uses the pack, **evaluate it on your own site's messages** (de-identified, or in a
secure environment). OpenHealth compares the result with the benchmark, says where it degrades,
and you read every misclassified case:

```bash
openhealth eval refill-router --dataset my-site.jsonl
```

To compare providers, add a variant to the slot in `pack.yaml` (for example `llm: {type: llm}`
under `reader.variants`). Then run
`openhealth leaderboard refill-router --run reader=rules --run reader=llm --dataset my-site.jsonl`.

## 9. Publish the card

```bash
openhealth card refill-router --write    # writes DECISION_CARD.md
```

The Decision Card gathers the pack's purpose, risk, questions, policy and measured performance
into one page. Share it with your AI or clinical decision support committee. At risk level 2 or
3, their approval (recorded in the dashboard) is what allows automation.

## 10. Share it

- **With colleagues:** open a pull request with the folder in `packs/`. CI validates, tests and
  evaluates it, and shows its card ([`CONTRIBUTING.md`](../../CONTRIBUTING.md)).
- **On a registry:** `openhealth push refill-router --key my-key.pem` signs the pack and
  publishes it, and others `openhealth pull my-clinic/refill-router` and verify it.

## Checklist before real use

- [ ] The card's intended use, population and failure modes are honest and complete.
- [ ] The risk level is the highest that could apply.
- [ ] Every policy rule has a behaviour test.
- [ ] Local evaluation shows no missed critical cases, and you read every misclassified case.
- [ ] A clinician other than the author reviewed the criteria.
- [ ] At level 2 or 3, the committee's approval is recorded.

The full format is in the [Decision Pack specification](../spec/decision-pack-0.2.md).
