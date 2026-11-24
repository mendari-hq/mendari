# Contributing an agent

A community agent is an agent you contribute by pull request, for anyone running Mendari to
use. It lives in `community-packs/<name>/`, its id is `community/<name>`, and it is yours: you
are its maintainer. This guide is the whole path, from the idea to the merge and the updates
after it. How to write an agent in the first place is in
[Authoring an agent](authoring-an-agent.md); read that first.

**What a community agent can do.** A deployment serves community agents only when it sets
`MENDARI_COMMUNITY_PACKS=on` (off by default). There, every surface that shows the agent (the
API, the dashboard, Try it, the review queue, the SMART patient view, its Decision Card, CDS
Hooks, its FHIR CodeSystems, the `/docs` page) labels it
**"Community · not clinically reviewed"**, and the runtime holds it to these limits, whatever
its manifest says:

- it **recommends and never automates**: a person confirms every decision
  ([ADR-0002](../decisions/ADR-0002-runtime-enforced-risk-caps.md): policy only lowers
  automation), so no policy rule of its own has `outcome: BLOCK` (a block is final at once):
  send such cases to a person with `HUMAN_REVIEW` and a priority;
- by default it decides with **local providers** (rules, criteria, dosing, naive Bayes, or a
  composite of those); a remote variant (an `agent`, `llm` or `jev` variant) runs only if a
  deployment names the agent in its settings (`community/<name>:slot=variant`), for decisions
  and evaluations alike. Each remote variant names the input fields it sends (`state_fields`)
  and runs on the deployment's endpoint, model, answer length, timeout and effort: an `agent`
  variant may not set `endpoint` and calls the deployment's `MENDARI_AGENT_URL`;
- every variant, the ones it does not use by default too, sets only the keys its provider type
  reads (the [provider config schema](../spec/provider-configs.schema.json)) and can run;
- it keeps only a **hash** of each input (`audit.store_input: hash`);
- it reads at most **10,000 characters** of text in all (its texts, each and all together, and
  its keys too where its input schema runs patterns on them, with `patternProperties` or
  `propertyNames`; a longer input is refused before any pattern runs, and typo correction never
  makes a text longer than that), and at most **10,000 values**, nested at most **32** deep;
- its settings do not multiply its patterns' work: each field is named once in a
  `text_fields` (a provider's or a question's), `negation_window` is at most **200** characters
  with at most **50** `negation_cues`, its input schema runs at most **50** patterns on any one
  value or key (two `$ref`s to the same schema run its patterns twice, and each
  `patternProperties` pattern counts three times where `additionalProperties` is also set, which
  searches them again and for its error message) and never refers back to itself where patterns
  run, and its rules (on an input) and its schema (on one value) run at most **500** patterns
  together (a rules pattern counts once for each question it answers and each composite member
  holding it);
- those same pattern runs come to a **size** of at most **3,000** together: a pattern's size is
  the characters, classes and `.`s it holds, each as often as a count repeats it (`a{1,50}` is
  50, `a{5,}` is 5, `\w+` and `x*` are 1, `(?:ab|cd){1,10}` is 40), so RE2's time on each
  character grows with it. `community-check` prints your agent's;
- its patterns and its schema's checks, together, take at most **one second** of CPU in a
  decision: past that its rules or its schema check stop and the decision abstains, for a
  person (the error `time_budget`; `mendari test` says so), or, when the schema had already
  found the input wrong, the input is refused. Ordinary agents take milliseconds;
  if your tests run out of time, shorten the long counts (`.{0,999}`) and classes repeated many
  times;
- its input schema uses only the keywords Mendari keeps within the time budget: `type`,
  `properties`, `required` (at most 100 names), `additionalProperties`, `patternProperties`,
  `propertyNames`, `items`, `prefixItems`, `minItems`/`maxItems`,
  `minProperties`/`maxProperties`, `enum` (at most 500 values) and `const` (texts, numbers,
  `true`, `false` or `null`), `minLength`/`maxLength`, `pattern`, `format`, the number limits
  (`minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`, `multipleOf`), `allOf`,
  `anyOf`, `oneOf`, `not`, `if`/`then`/`else`, `$defs` at the root (names of letters, digits,
  `_`, `.` and `-`) and `$ref` to one of them (`#/$defs/<name>`), the annotations (`title`,
  `description`, `default`, `examples`, `$comment`, `deprecated`, `readOnly`, `writeOnly`,
  `x-...`), and `$schema`/`$id` at the root. A `required` name and an `enum` or `const` text
  are at most 200 characters.
  Others are refused: jsonschema works some out on its own, outside the budget
  (`unevaluatedProperties`: use `additionalProperties`; `unevaluatedItems`: use `items`;
  `contains`), or compares every pair of items at once (`uniqueItems`); check those in a rule;
- its FHIR systems (a `task.performer_system` in `fhir/mapping.yaml`) live under its own url,
  `https://mendari.co/packs/community/<name>/`, e.g.
  `https://mendari.co/packs/community/<name>/CodeSystem/performer`;
- it is at most **risk level 1**.

Its name, CDS Hooks title and publisher are yours: they may not copy the name or CDS Hooks
title of an agent Mendari maintains, nor use the word Mendari (or its old name, OpenHealth) as
whole words: `MendariCare`, `Men dari` and `Open Health` count, while `Open healthcare intake`
and `Mend a rift` do not. Both are compared on ASCII letters and digits, ignoring case,
punctuation and compatibility forms (full-width or circled letters), and `community-check` fails
on them. A name that only looks like one of those (letters from another alphabet, accents, `l`
for `I`, `0` for `O`) or a word that mixes alphabets is a warning saying what it looks like, for the owner to
review: say in the pull request why your name is spelled that way. Greek letters in clinical
words (`β-blocker`, `µg`) are fine.

Why these limits, and what was decided, is in
[ADR-0007](../decisions/ADR-0007-community-agents-by-pull-request.md). The label comes from
where the agent lives, never from `publisher.type`, which every agent declares for itself.

## The path

1. **Propose it (recommended for a new agent).** Open a "Propose a pack" issue (the form's
    name): the use case, who would use it, the risk level you intend and how you will make the data. The owner answers
    yes, no or "changes needed" before you build anything.

2. **Fork and set up.** Fork the repository on GitHub, clone your fork, and run `uv sync` in it.
    Mendari is not on PyPI yet, so every command below starts with `uv run`.

3. **Create it.**

    ```bash
    uv run mendari new community-packs/<name> --community --publisher "<you>"
    ```

    The folder name is lowercase letters, digits and dashes, and it is the agent's name.
    (`--community` and `--agent` do not combine: the community scaffold decides with keyword
    rules; add an `agent` variant to its slot as step 4 of
    [Authoring an agent](authoring-an-agent.md#5-declare-the-agent-slot) shows, and keep a local
    variant as its `use`.) The
    scaffold already has the community defaults: `license: Apache-2.0`, `store_input: hash`,
    local providers, a `CHANGELOG.md`, a benchmark, its Decision Card and a critical label
    with tests. It passes the checks as written.

4. **Write it.** The criteria, the rules, the synthetic data
    ([below](#synthetic-data-without-python)) and the behaviour tests in `tests/cases.yaml`: at
    least one test case for each critical label (`evaluation.critical`), and at least five
    benchmark cases for each. Then regenerate the benchmark (with its ledger, in
    `evals/ledgers/`, which CI checks too) and the card, after every change:

    ```bash
    uv run mendari eval community-packs/<name> --dataset examples --baseline none \
        --out community-packs/<name>/evals/benchmark.json
    uv run mendari card community-packs/<name> --write
    ```

5. **List yourself as its maintainer** in
    [`community-packs/MAINTAINERS.yaml`](https://github.com/mendari-hq/mendari/blob/main/community-packs/MAINTAINERS.yaml),
    with your GitHub user name: `{id: community/<name>, maintainers: [<you>]}`.

6. **Check it locally**, with the checks CI runs:

    ```bash
    uv run mendari community-check community-packs/<name> --base origin/main
    ```

    Each problem says what to fix. Run it until it says "passed: ready for review".

7. **Open the pull request.** Sign off every commit (`git commit -s`, the
    [Developer Certificate of Origin](https://github.com/mendari-hq/mendari/blob/main/CONTRIBUTING.md#sign-off-the-developer-certificate-of-origin)).
    Change only `community-packs/<name>/` and your entry in `MAINTAINERS.yaml`. Use the pack
    pull request template: add `?template=pack.md` to the address of the "Open a pull request" page, or run
    `gh pr create --template pack.md`. It asks for the risk level and why, who reviewed the
    clinical content, how the data was made, the providers, and the CHANGELOG entry.

8. **CI runs.** The `pack-check` job runs `community-check` on your agent, and fails if the pull
    request changes anything outside your agent's folder and its `MAINTAINERS.yaml` entry (or
    adds a git submodule: the agent's files are in the repository, where they are read). Its
    summary shows the metrics and the rendered Decision Card. GitHub may ask the owner to
    approve the first run of a first-time contributor. There is no preview deployment for a
    fork: CI is the preview.

9. **Review.** The owner reads what CI cannot judge ([below](#what-the-reviewer-reads)) and
    labels the pull request: `pack:new` or `pack:update`; `clinical-review:provided` when a
    clinician who is not the author reviewed the content and the pull request says who, else
    `needs-clinical-review`; `risk-level-change` when the level changes. Changes go back to
    step 4.

10. **Merge.** The owner squash-merges it. The agent is then served wherever community agents are
    turned on, labelled as above.

11. **Updates.** Every later change to what the agent decides with raises its version and adds a
    `CHANGELOG.md` entry; `community-check` refuses one that doesn't. An update must not do
    worse than the benchmark on `main`, within the tolerance `main` set (at most 0.05), on its
    own benchmark dataset and on `main`'s, and keeps `main`'s critical labels. Benchmark cases
    `main` has that the update removes or relabels are warnings: say why in the pull request.

## Risk level

The level is self-declared, so it is the first thing a reviewer reads. Choose the highest that
could apply, and say why in `risk.rationale`. A community agent is level 0 or 1.

| Level | For | Community agent |
|---|---|---|
| 0 | Administrative: which team or queue sees something first, with no clinical judgement in the outcome and anything possibly urgent sent to a person | Yes |
| 1 | Clinical information: reading clinical facts to extract them, check them or decide where they go | Yes |
| 2 | Clinical support: eligibility, suggestions, patient-facing clinical content | No |
| 3 | Patient-specific prediction | No |
| 4 | Time-critical, diagnostic or treatment decisions | No |

The demo agent, `mendari/portal-message-agent`, is level 0 although messages describe symptoms:
it only chooses who reads a message first, never replies or judges, and a rule sends any
message that may describe same-day symptoms to a nurse. An agent that reads a referral to decide whether it is complete or urgent
is level 1. Under-declaring is the main way around the limits, so a reviewer who thinks a
level is too low asks you to raise it, and an agent that belongs at level 2 or above is not a
community agent for now.

## Synthetic data without Python

**Never use a real person's data**, not even "de-identified": no real names, dates of birth,
addresses, record numbers, phone numbers or messages copied from a chart or an inbox. Real data
merged into a public repository cannot be fully taken back. Write every case yourself, the way
a teaching case is written, and keep rare-condition and date combinations vague. `community-check`
warns on text in `data/`, `tests/` and a trained model's vocabulary that looks like a real
person's details; act on every warning. It fails on any file that holds a case `mendari
improve` added to a pack or `mendari feedback export` wrote, or a model built from one: those
are a site's own cases. A trained model (a composite's members too) must be what `mendari
train` gives from the agent's own data, byte for byte.

Two ways to make the data, neither needs Python:

- **By hand.** `data/examples.jsonl` is one case per line: an `input`, the right `labels` and an
  `id`. Copy a line of the scaffold's file and change it.

- **From templates.** Write the cases as templates in YAML, with placeholders and the values
  they take, and let the script expand them, the same way every time:

    ```yaml
    # community-packs/<name>/data/cases.yaml
    - input: {message: {body: "Please refill my {drug}, I have {left} left."}}
      labels: {request: refill, urgent_symptoms: "no"}
      fill:
        drug: [metformin, lisinopril, atorvastatin]
        left: [two pills, "none"]
    - input: {message: {body: "Crushing chest pain since this morning."}}
      labels: {request: other, urgent_symptoms: "yes"}
    ```

    ```bash
    uv run python scripts/synthetic_cases.py community-packs/<name>/data/cases.yaml \
        --out community-packs/<name>/data/examples.jsonl --prefix ex
    ```

    The first template makes six cases (three drugs times two amounts), the second one. A
    placeholder the input never uses is refused, and so is a file that makes more than 5,000
    cases. Commit the templates with the data, so a reviewer reads them instead of every line.

Make the hard cases too: misspellings, negations ("no chest pain"), two requests in one
message, other languages your users write in. A benchmark of easy cases measures nothing.

## ABSTAIN carries no recommendation

`ABSTAIN` means the agent has nothing to suggest: the case goes to the existing manual workflow,
the policy's `fallback`. The runtime drops any recommendation from an `ABSTAIN` outcome, so a
rule `outcome: ABSTAIN` with a `recommendation` loses it, and a test must not expect one. When
you want a person to see a suggestion, use `HUMAN_REVIEW`; for a community agent every outcome
that would have been `AUTO` becomes `HUMAN_REVIEW` anyway, with its recommendation.

## Patterns run on RE2

Write patterns in Python's `re` syntax, as every agent does. A community agent's patterns (its
rules and its input schema's `pattern`, `patternProperties` and `propertyNames`) then run on
[RE2](https://github.com/google/re2/wiki/Syntax), which never backtracks: its time grows with
the text's length times the pattern's compiled size, never exponentially, so no pattern can
stall the server the way `(a+)+$` stalls `re` on a long message. Nothing times your patterns
before they are merged; at run time they share the one-second budget above, which only very
long counts on long text in other scripts come near. Mendari rewrites each pattern for RE2 to
mean what it means in `re`.

**Not available on RE2:** lookahead and lookbehind (`(?=...)`, `(?!...)`, `(?<=...)`,
`(?<!...)`), back references (`\1`, `(?P=name)`), conditional groups (`(?(1)...|...)`), atomic
groups (`(?>...)`) and possessive quantifiers (`*+`, `++`, `?+`, `{m,n}+`). Counts nested inside
each other may multiply to at most 1,000, and a pattern must fit in RE2's memory for one pattern
once compiled. `mendari validate` refuses a pattern that breaks one of these, and
`community-check` names it under its `patterns` rule; each says what to write instead:

| Instead of | Write | Why |
|---|---|---|
| `pain(?= now)`, `(?<=chest )pain` | `pain now`, `chest pain` | Lookahead and lookbehind: match the text itself (a rule reads only whether, and where, it matches) |
| `(?<!no )pain` | `pain`, with `negatable: true` and `no` in `negation_cues` | Negative lookbehind: the negation cues say what must not come before |
| `\d+(?!\s*%)` | `\d+\s*(?:mg\|mcg\|ml)` | Negative lookahead: say what must come, not what must not |
| `^(?!.*\burgent\b).*refill` | `refill`, and a second rule `\burgent\b` with more weight on the label it means | "Matches one thing but not another" is two rules |
| `([a-z])\1`, `(?P<d>\d)(?P=d)` | the text itself, e.g. `(?:aa\|bb\|cc)` | Back references |
| `(a)?(?(1)b\|c)` | `ab\|c` | Conditional groups: write the alternatives with `\|` |
| `(?>...)`, `a++` | `(?:...)`, `a+` | Atomic groups and possessive quantifiers: RE2 never backtracks, so they are not needed |
| `(?:\w{1,20}\s?){1,60}` | `(?:\w{1,20}\s?){1,20}` | Counts nested inside each other may multiply to at most 1,000 |
| `\w{0,1000}` | `\w*`, or a smaller count | Too large once compiled: `\w` is every letter and digit in Unicode, a large class |

**What still differs from `re`.** `\w`, `\d`, `\s` (and `\W`, `\D`, `\S`) are `re`'s Unicode
classes, `$` is `re`'s (the end, or before a newline that ends the text), `\Z` is the end only,
`a{,3}` is `a{0,3}`, and counts above 1,000 are split into counts RE2 runs. Case is ignored
exactly as `re` ignores it: `ilaç` matches "İLAÇ", `ağrı` matches "AĞRI", and `\W` never
matches a Greek `ι`. Two things differ:

- **Word boundaries.** RE2 sees only ASCII letters, digits and `_` as word characters *in the
  text just outside a match*, so a letter or digit outside ASCII there does not continue the
  word: `\bpie` also matches in "épie" and `\bma\b` inside "mañana". In a script other than
  Latin, whose letters are all outside ASCII, that is the usual case: `\bрак\b` (cancer) also
  matches inside "практика" (practice), and `\bболь\b` (pain) inside "больница" (hospital).
  `community-check` warns on a boundary next to a letter or digit outside ASCII (or a class
  that may match one, such as `\w`). Write `(?:^|\W)рак(?:\W|$)` instead of `\bрак\b`: `\W` is `re`'s, so it matches no
  letter in any script (the match then starts one character earlier).
- **Lone surrogates.** A text holding one (a JSON string can) has it read as U+FFFD before a
  pattern runs.

A community agent also refuses an input whose texts are longer than 10,000 characters, alone or
together, before a pattern runs, and the settings that multiply its patterns' work are capped
(the limits at the top of this guide).

The same rules apply to an agent pulled from a registry that is not a copy of an agent bundled
with the installed release: RE2, the caps, the schema keywords, the input limits and the time budget
([ADR-0009](../decisions/ADR-0009-bounded-evaluation-for-agents-the-deployment-does-not-own.md)).

Habits that keep patterns precise and honest:

- Bound every gap: `{0,5}` words, not `*`, e.g.
  `chest(?:[^\w.!?\n]+\w+){0,5}?[^\w.!?\n]+pain` for at most five words between, never across
  a sentence.
- Leave `.*` off either end: a rule matches anywhere in the text already.
- Match whole phrases a person writes ("can't breathe"), not single letters or fragments.
- Name every pattern: the audit record says which one matched.
- Mark a pattern `negatable: true` when "no chest pain" must not count, and test it.

## What the reviewer reads

CI checks the mechanics. The owner then reads what it cannot judge, and may ask for changes:

- **Clinical content.** Are the criteria, patterns, `known_failure_modes` and `out_of_scope`
  sound, are the critical labels the right ones, and is anything dangerous missing?
- **Risk level.** Does it match the intended use (the table above)?
- **Clinician review.** Who reviewed the content, their role, and what they reviewed. With
  none named, the agent can still merge, as `needs-clinical-review`; it is labelled "not
  clinically reviewed" in the product either way.
- **Synthetic data.** Nothing that looks like a real person, and the PHI warnings answered.
- **Instructions in the text.** Criteria are also instructions to a model if a deployment ever
  runs the agent on one, so nothing like "always answer no".
- **Provenance and licensing.** Nothing copied from proprietary criteria (InterQual, MCG, payer
  policies) or copyrighted instruments; code systems with licence terms (CPT, SNOMED CT) only
  as their licences allow, each declared in `code_systems` with its `licence_note`; the
  sources of `card.evidence` given (see
  [CONTRIBUTING.md](https://github.com/mendari-hq/mendari/blob/main/CONTRIBUTING.md#content-provenance)).
- **The card.** Intended use, users, population, out-of-scope uses and failure modes, written
  for a clinician.
- **Fit.** It does not duplicate an agent already there, and you answer the review.

## After the merge

- **You maintain it.** Maintainers are expected to answer a safety issue about their agent
  within 14 days. An issue labelled `patient-safety` blocks every other change to the agent
  until it is answered.
- **Core changes.** CI runs every community agent's behaviour tests on every change to Mendari.
  A change that breaks an agent fixes the agent or removes it in the same pull request, and says
  so.
- **Promotion.** A community agent may become one Mendari maintains, after clinical review,
  with a new `mendari/` id, as a new major version.
- **Removal.** Only the owner removes an agent (CI fails anyone else's removal; to propose one,
  open an issue). The owner removes an agent that is unmaintained, superseded or the subject of a
  licence complaint with a pull request that deletes its folder and keeps its
  `MAINTAINERS.yaml` entry as a tombstone, so its id is never used again. An agent with a safety
  problem or real patient data is removed at once (the merge reverted and deployed), and a
  deployment can turn every community agent off with `MENDARI_COMMUNITY_PACKS=off`.
