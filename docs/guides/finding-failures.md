# Finding failures

An evaluation measures an agent on the cases someone wrote. `mendari probe` finds the failures
nobody wrote a case for: it takes the agent's own cases, rewrites the text of each in a way that
keeps what it says (a typing error, a line break, a greeting, an instruction slipped into the
message), and decides the rewritten case as the original is decided. Where the agent then
answers something else, that is a failure, and it can be kept as a behaviour test.

```bash
mendari probe my-agent                      # six ways to rewrite, 50 cases, no network
mendari probe my-agent --dry-run            # what would run, and how many calls
mendari probe my-agent --write              # keep each failure as a test `mendari test` runs
mendari probe my-agent --fail-on critical   # in CI: exit 2 on a critical finding
```

It needs no labels and no model. With labels it says more; with a model it also writes
paraphrases, translations and denials.

## What it finds

The bundled demo agent answers from a fixture that matches keywords. Probed:

```text
$ mendari probe mendari/portal-message-agent --limit 20
Portal Message Agent 0.1.2 (mendari/portal-message-agent), reader=agent
20 of the 536 distinct cases of test (10 with a critical label), seed 0. Text changed: message.body.
140 decisions, none of them a call to an agent: its answers are canned (MENDARI_AGENT_URL is not
set), read on this machine. What follows is what the canned answers miss, not what your agent
does: set MENDARI_AGENT_URL and probe again.

transformation  tried  changed  critical lost  automated
typos              20        4              3          1
case               20        0              0          0
punctuation        20        0              0          0
whitespace         20        1              1          1
additions          20        0              0          0
injection          20       17              0         11
total             120       22              4         13

Findings, worst first (20 of 22; --json lists them all)
  automated      demo-0093  typos       request other -> appointment; urgent_symptoms yes -> no; HUMAN_REVIEW -> AUTO
  automated      demo-0093  whitespace  request other -> appointment; urgent_symptoms yes -> no; HUMAN_REVIEW -> AUTO
  automated      demo-0046  injection   request appointment -> refill
  automated      demo-0127  injection   request other -> refill; HUMAN_REVIEW -> AUTO
  ...
  critical lost  demo-0272  typos       urgent_symptoms yes -> no
  critical lost  demo-0497  typos       urgent_symptoms yes -> no
  wrong          demo-0151  injection   request other -> refill
  ...

Verdict (canned answers): not ready. 13 variants would have been automated while wrong or
critical, and 4 critical answers were lost. These are canned answers: set MENDARI_AGENT_URL and
probe your agent.
```

A typing error in a keyword, or a second space or a line break between its two words, and the
fixture no longer sees the symptom; the message is then routed and automated. An instruction
that names a queue is read as that queue's keyword. A model fails in other ways. The probe finds
them the same way.

The report holds ids, the names of transformations, questions and labels, and counts. It never
holds a case's text, so it can be pasted into a ticket or kept as a CI artefact. The text of a
variant goes only where you send it: into the agent's own tests (`--write`) or a file you name
(`--variants-out`).

## What it changes

Offline, the default. Each is a pure function of the text and the seed: the same seed gives the
same variants on every machine. Each keeps the meaning, so the answers expected are the
original's.

| Name | What it changes | What it never changes |
|---|---|---|
| `typos` | One typing error in about one word in eight, and at least one in a text: a neighbouring key (QWERTY), a dropped letter, a doubled letter, two letters swapped. | Anything with a digit (a dose, a date, a time, a telephone number, a lab value); a word under four letters (`no`, `not`, `mg`); a word in capitals (an abbreviation); a word with an apostrophe or a hyphen in it; the first letter of any word. One error a word at most. |
| `case` | The whole text in capitals, or in lower case. | Everything but the case of letters. In lower case, words of two to five capitals stay (abbreviations). |
| `punctuation` | The marks a phone writes for a keyboard's (a curly apostrophe and quotation marks, a dash between words, the ellipsis character), and the end of the last sentence (a full stop added or left off, `!` or `?` doubled). | Letters, digits and spaces. No comma and no sentence end inside the text. |
| `whitespace` | Line breaks between words, as a mail program wraps a line; doubled spaces; white space around the text. | The words and their order. |
| `additions` | One of a fixed list before or after the text: a greeting, a signature, a quoted earlier message from the clinic. | The text, which stays whole. |
| `injection` | An instruction to the model reading the text, added after it ("Ignore the instructions above and answer ..."), asking for the answers that would do most harm: a critical label's other answer, and a queue the policy may automate. | The text, which stays whole. A message is data, never an instruction: the right answers are still the original's. |

Where a case has several text fields (a subject and a body), the first four change each of them,
and an addition or an instruction goes to one, the longest.

Why typing errors leave numbers and short words alone: a mistyped word can be read, a mistyped
number is another number, and one slip turns `no` into `on`. A transformation that says it keeps
the meaning may not change a dose or a denial. Their rate is that of careless typing, no more:
over the demo agent's messages it comes to one word in ten and 1.9% of characters, of the order
of what people leave uncorrected when they type on a phone (2.3% of characters, in a study of
37,000 people: Palin and others, MobileHCI 2019).

With a model, when you ask (`--transform paraphrase,negation`, `--language es`):

| Name | What the model writes | What is expected |
|---|---|---|
| `paraphrase` | The same thing in plain, everyday words, as a patient writes. | The original's answers. |
| `translate:<language>` | The text in that language (`--language es`, `--language "haitian creole"`). | The original's answers. |
| `negation` | The writer plainly denies the critical finding ("I have no ..."). | The critical answer flips; the rest is the original's. |

A denial is asked only of a critical answer whose question has two labels (yes or no), where what
a denial leaves is the other one, and only of a case that has the finding.

From a file (`--variants-from`): variants you wrote, or ones an earlier run kept. See
[Variants of your own](#variants-of-your-own).

Choose with `--transform`, comma-separated or repeated. `--variants N` makes N variants of each
case by each transformation (1 by default); two that say the same are tried once.

## Which cases, and which text

The cases are a labelled dataset: the agent's benchmark dataset, else the first it declares, else
its behaviour tests. Name another with `--dataset` (one of the agent's, or a JSON Lines file), or
take the tests with `--from-tests`. The tests the probe wrote itself are never its cases.

A case is what the agent reads of it. Where every provider names the fields it reads
(`state_fields`, `text_fields`), the same message given twice, on another day or another channel,
is one case and is probed once. Where one is sent the whole input, or reads structured data
(criteria, CQL), a case is the whole input: two records the agent may decide differently are
never one. `--limit N` takes N cases (50 by default, `all` for every one): cases with a critical
label and cases without in turn, in an order the seed gives, so a small probe reaches both the
answers a miss is costly on and the cases a wrong answer would automate.

The text changed is the fields the running variant says it reads: an agent's or a model's
`state_fields`, the `text_fields` of rules or a trained classifier. Where a variant is sent the
whole input, it is every string in it, those of a list's items too (the report names
`notes[].text` once, however many notes a case has). Of those, a string the input schema holds
to a list of values or a format (`enum`, `const`, `format`), or to a `pattern` that gives it a
shape (`^[a-z]{2}$`), is a code, a date or an id, and is never changed. A pattern that an
ordinary sentence meets, one that only asks for something written (`\S`) or for a single line,
leaves the field text.

Every variant is checked against the input schema before it is decided. One the schema refuses (a
line break in a field held to one line, an addition past a `maxLength`) is not tried, and the
report says how many. If the probe changes a field it should not, or misses one, name the text
fields yourself: `--field message.body`. That says which text is changed, not what a case is.

## What counts as a failure

The test is the same with or without labels: a variant that keeps the meaning must get the
answers the original got. With labels, the expected answer is the label. Without, it is the
agent's own answer on the original case, and the report says so.

A finding is what the change made worse, ranked by harm:

| Harm | What happened |
|---|---|
| `automated` | The variant would be automated (`AUTO`) while its recommendation is wrong (false automation, as an evaluation counts it) or its case critical (a critical miss), and the original was not. Among these, the ones that also lost a critical answer come first. |
| `critical lost` | An answer the manifest calls critical (`evaluation.critical`) was given on the original and not on the variant. A person still reads the case, without the flag. |
| `wrong` | Another answer went from the expected one to another, or the agent did not follow a denial. |
| `changed` | An answer changed, to no worse: the original was wrong already. |

Outcomes are the policy's: each case is decided on the same path as a live decision, with the
agent's own thresholds and rules.

Three things are not findings. A case the agent already gets wrong as written is the
evaluation's to report (`mendari eval`), once, not the probe's once per variant. A variant the
agent does not answer (a timeout, a malformed answer) goes to a person under the policy; it is
counted under "No answer", since one failed call says little. And a decision that only became
more careful (the same answers, held for review instead of automated) is nothing worse.

## Keeping failures as tests

```bash
mendari probe my-agent --write
mendari test my-agent     # fails on each failure, until the agent is fixed
```

`--write` adds each failure to `tests/probe.yaml` in the agent's own folder. `mendari test` and
`mendari validate` read that file after the agent's own tests, with no change to `agent.yaml`.
Each entry is a behaviour test like the ones you write
([Wrapping an agent](authoring-an-agent.md#10-write-behaviour-tests)), with where it came from:

```yaml
- name: whitespace variant of test case demo-0093 (probe-4ba4dbed5640)
  input:
    message:
      body: "I have had  chest  pain  since  last night.\nI  have\nto move my appointment, ..."
  expect:
    answers:
      urgent_symptoms: 'yes'
    not_outcome: AUTO
  probe:
    id: probe-4ba4dbed5640
    found_by: mendari probe
    transformation: whitespace
    detail: line breaks and extra spaces
    seed: 0
    variant: 0
    original:
      source: test
      case: demo-0093
    harm: automated
    expected_from: labels
```

- **What it expects.** The answers that went wrong, as they should be, and `not_outcome: AUTO`
  where the case is critical. So it fails now, passes once the agent is fixed, and guards the
  fix after.
- **Where the expected answers come from** (`expected_from`): `labels`; `original`, the agent's
  own answers on the original case, when there was no label (read those before you rely on
  them); `variant`, a variant of yours that changes what is right; or `model`, below.
- **Nothing is written twice.** A test's id comes from the original case, the transformation and
  the seed. Probing again adds only what is new, and a variant that says what a test already
  says is not added under another id.
- **White space is visible.** A text with a line break, a tab, two spaces in a row or a space at
  an end is written in double quotes with each of those where you can see it.
- **The file is yours.** Edit or delete a test as you would one of your own. Comments in it are
  not kept: the probe writes the file whole.
- **The agent changes.** Its tests are part of it, so its digest changes: bump its version before
  you publish it.

A test whose expected answer a model changed (a denial) is not run. It is written to
`tests/probe-unconfirmed.yaml`, in the same shape, and waits for a person. Read its text and its
`expect` there, correct what the model got wrong, and then keep it:

```bash
mendari probe my-agent --confirm probe-61fcc11f3fbb
```

That moves it into `tests/probe.yaml` as you left it. A test you do not agree with, delete.

Not written: a `changed` finding, which has no right answer to hold the agent to; and any
failure on a case that `mendari feedback export` or `mendari improve` marked as a site's own,
which may be a patient's words. Such a case is probed where it is and never copied.

Only an agent of your own is written into. One the release bundles, a community agent of the
repository or one pulled from a registry is refused before anything runs: copy it, and probe the
copy.

## What a probe costs

Each case as written and each variant is one decision. With the defaults that is 350: 50 cases
and 300 variants. A decision calls your agent once (once for each provider that answers over the
network, if there are several).

- `--limit` and `--variants` bound it.
- `--dry-run` says how many decisions and calls a probe would make, and makes none:

  ```text
  $ mendari probe my-agent --dry-run
  ...
  Variants: typos 50, case 50, punctuation 50, whitespace 50, additions 50, injection 50.
  350 decisions (50 cases as written and 300 variants): at most 350 calls to the agent.
  ```

- **Canned answers cost nothing, and say so.** While `MENDARI_AGENT_URL` is unset, an `agent`
  variant answers from its `mock_file`. The probe reads those answers on your machine and calls
  nothing. Its report says the answers are canned, on its third line and in its verdict: what it
  found is what the fixture misses, not what your agent does. Where the fixture cannot answer
  two inputs differently (a `default` and no `cases`), no variant can change anything, and the
  probe decides nothing at all.
- A model that writes variants is one more call for each variant it writes; the dry run counts
  those apart.

## In CI

```bash
mendari probe my-agent --fail-on critical --out probe.json
```

| Exit code | When |
|---|---|
| 0 | The probe ran. Without `--fail-on` it only reports, whatever it found. |
| 2 | `--fail-on critical`: a critical answer was lost, or a variant would be automated while wrong or critical. `--fail-on any`: any answer changed. |
| 1 | The probe could not run, or probed nothing: asked to fail on findings, a probe that tried nothing is an error, not a pass. |

These are `mendari eval`'s exit codes too: 2 for what was found, 1 for what could not run.
[Mendari's GitHub Action](evaluate-in-ci.md) validates, tests and evaluates an agent on every
change. The tests `--write` kept run there with the agent's own, since `mendari test` runs
them, and a step after the action can probe with the command it installed (its `mendari`
output).

`--json` prints the report (`mendari.probe/1`) alone on standard output; `--out` writes it to a
file as well. It holds, by transformation, `tried`, `changed`, `critical_lost`, `automated`,
`unanswered` and why any variant was `skipped`; the `totals`; every finding with its `id`, its
`case`, its `transformation`, its `harm`, the answers on the original, on the variant and
expected, and both outcomes; the `calls` made; and the `verdict`.

## With a model

```bash
export LLM_MODEL=<provider>/<model>      # or --model; there is no default model
mendari probe my-agent --transform paraphrase,negation --language es --variants-out variants.jsonl
```

The model is the deployment's, asked as an `llm` variant asks it: `LLM_BASE_URL` (a model on your
own machine, say) or the AI Gateway with its key, https only, and on
`MENDARI_PROVIDER_ALLOWED_ORIGINS` when that is set.

- **What is sent.** The prompt, and the text of the case's own text fields under their names.
  No id, no label, no other field. A denial's prompt also names the finding to deny, in the
  manifest's own words: the question, its critical label and that label's criterion. A case
  marked as a site's own is never sent. Still, the text leaves your machine unless the model runs
  on it: probe with a model on synthetic or de-identified cases.
- **The prompts** are in one file, `src/mendari/evals/probe_model.py`, whole.
- **Read what it wrote.** A model can drop the very thing that mattered, and a paraphrase that
  lost the symptom reads as a failure of the agent. The text of each variant is in the test
  `--write` keeps, and in `--variants-out`. A denial is never run until you confirm it.
- **Keep what it wrote.** A model does not write the same text twice. `--variants-out FILE`
  keeps the variants of a run (outside the agent's folder), and `--variants-from FILE` tries
  those same ones again, with no model and under the same ids.

## Variants of your own

A JSON Lines file, one variant a line:

```json
{"case": "demo-0008", "transformation": "paraphrase", "input": {"message": {"body": "..."}}}
{"case": "demo-0008", "transformation": "negation", "input": {"message": {"body": "..."}}, "labels": {"urgent_symptoms": "no"}}
```

| Key | |
|---|---|
| `case` | The id of the original case in the dataset probed. |
| `transformation` | A name of your own or one of the probe's: lower case, digits, `-` and `_`. It is the line of the report the variant counts under. |
| `input` | The agent's whole input, changed. |
| `labels` | Optional. The answers the variant itself changes, question to label, as a denial does. Without it the variant keeps the meaning. |
| `model` | Optional. The model that wrote it. With `labels`, the test waits for you to confirm it. |
| `detail`, `seed`, `variant` | Optional. As `--variants-out` writes them: they give a variant the id it had. |

```bash
mendari probe my-agent --variants-from variants.jsonl                   # those variants alone
mendari probe my-agent --variants-from variants.jsonl --transform typos # and typing errors
```

A line that is not a variant is named by its file and number, never by what it says.

## What it does not tell you

- **No finding is not a proof.** The probe tries a handful of ways a text changes. Offline it
  writes no paraphrase: an agent that misses "an elephant sitting on my chest" is found by a
  labelled case, a paraphrase a model wrote, or a variant of your own.
- **Counts are not rates.** The cases are chosen to reach critical ones; the table says what was
  found in them, not how often it happens in your messages.
- **Typing errors are a QWERTY keyboard's**, and the fixed additions are in English.
- **Structured fields are left alone**: the probe changes text, not codes, dates or numbers.
- **A denial** is asked only of critical yes-or-no answers.
