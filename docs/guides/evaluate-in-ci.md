# Evaluate an agent on every change

An agent changes: its prompt, its model, its policy, its rules. Each change can make it fail on
a case it used to get right. Mendari's GitHub Action runs the same three checks on every pull
request and push that you run by hand: `mendari validate`, `mendari test` and `mendari eval` on
a labelled dataset. It writes what it found to the job's summary, keeps the result file as an
artifact, and fails the job when the agent is not valid, a behaviour test fails, the evaluation
does not meet a limit you state, or it is worse than a baseline.

It needs no secret and no permission beyond reading your repository, so it is safe to run on a
pull request from a fork. It prints counts and case ids, never a case's text.

!!! note "Before the first release"
    The action is in Mendari's repository (`action.yml`), and it is used by the repository's
    name and a release's tag. Until the repository is public and its first release is tagged,
    the lines below cannot be resolved from another repository.

## The few lines

`mendari new` writes this file into the new agent's folder, as `.github/workflows/mendari.yml`,
with the tag of the release you installed. To add it to an agent you already have:

```yaml
name: mendari

on:
  pull_request:
  push:
    branches: [main]

permissions:
  contents: read

jobs:
  agent:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1
        with:
          persist-credentials: false
      - uses: mendari-hq/mendari@v0.1.0
        with:
          agent: .
          dataset: examples
          min-critical-detection: "90%"
          max-critical-misses: "0"
```

GitHub reads workflows from a repository's own `.github/workflows/`. If the agent's folder is
the repository, `agent: .` is right. If the agent is a folder of a larger repository, the file
goes in that repository's `.github/workflows/` and `agent` names the folder.

The action installs Mendari from its own checkout at that tag, with the dependencies the
release locked, each checked against its hash. It installs into an environment of its own and
leaves the job's Python and packages alone; it does put `uv` on the runner's path. Mendari's own
CI runs it on `ubuntu-latest`; it has not been run on a macOS or Windows runner. A tag can be
moved by whoever controls it: to pin harder, name the release's commit instead, with the tag in
a comment (`mendari-hq/mendari@<commit>  # v0.1.0`), which Dependabot keeps up to date.

## What it takes

Every input has a default.

| Input | Default | What it is |
|---|---|---|
| `agent` | `.` | The agent's folder, where its `agent.yaml` is |
| `dataset` | `examples` | The labelled dataset: a name under `evaluation.datasets` in `agent.yaml`, or the path of a JSONL file |
| `provider` | the agent's own | Provider variants to use instead, as `SLOT=VARIANT` (`reader=rules`), separated by spaces or new lines |
| `min-critical-detection` | `90%` | A limit: the least share of each critical label's cases the agent must detect |
| `max-critical-misses` | `0` | A limit: the most critical misses, the cases of each critical label that may be automated, so that no person reads them |
| `min-accuracy` | none | A limit: the least accuracy on the agent's primary question |
| `max-false-automation` | none | A limit: the most false automation, the cases automated with a wrong answer, as a share of the labelled cases |
| `baseline` | none | An earlier result file the evaluation must be no worse than |
| `target-critical-miss` | none | A target: the most critical misses a recommended automation threshold may leave, as a share of each critical label's cases |
| `target-critical-miss-of` | none | What that share is of: `critical`, each critical label's cases (as when empty), or `all`, all cases |
| `target-false-automation` | none | A target: the most false automation a recommended automation threshold may leave, as a share of the labelled cases |
| `target-confidence` | none | The confidence the targets are held with, `95%` when empty |
| `python-version` | `3.12` | The Python that runs Mendari, 3.11 or later |
| `artifact` | `mendari-evaluation` | The artifact's name; empty uploads nothing |

A [limit](#limits) is on what this run measured, and a result that does not meet it fails the
job. A [target](#a-target) never fails the job: it asks which automation threshold the cases
support. Both name a figure the same way: a **critical miss** is a case of a critical label
that is automated, so that no person reads it, and **false automation** is a case automated
with a wrong answer, as a share of the labelled cases.

A share is written `90%` or `0.9`: nine cases in ten. A path (the agent's folder, a dataset's
file, the baseline) is from the workspace, the repository's root. A workflow that uses the
action more than once in a run (two agents, two datasets) gives each use its own `artifact`
name.

The behaviour tests are the agent's own, in `tests/cases.yaml`, then the ones
`mendari probe --write` kept in `tests/probe.yaml`
([Finding failures](finding-failures.md#keeping-failures-as-tests)). An agent that has none
fails here: `mendari test` does not pass on nothing.

Its outputs are `validation`, `tests` and `evaluation` (`passed`, `failed`, and for the
evaluation `error` when it could not run), the paths `result` and `summary`, and `mendari`, the
path of the command it installed, for a later step that wants it (`mendari card`, say).

## What the summary shows

The job's summary page, and the log, show what each check found, then the evaluation. This is
Mendari's own demo agent, whose canned answers leave urgent messages undetected on purpose, held
to the action's own limits (the page shows the tables as tables):

```text
## Mendari agent check: failed

Validation: passed. Behaviour tests: passed. Evaluation: failed.

    ok  mendari/portal-message-agent@0.2.0  sha256:b488ef8b…
    3 passed, 0 failed

### Evaluation of mendari/portal-message-agent 0.2.0 on test (600 cases)

Providers: reader=agent. Answered by mock-portal-agent.
Proportions are k of n [exact 95% interval, Clopper-Pearson].

| Figure                                 | Result                                 |
| Request accuracy                       | 549 of 600 [89.0-93.6%]                |
| Outcome AUTO                           | 326 of 600 [50.3-58.4%]                |
| Outcome HUMAN_REVIEW                   | 274 of 600 [41.6-49.7%]                |
| Outcome ABSTAIN                        | 0 of 600 [0.0-0.6%]                    |
| Outcome BLOCK                          | 0 of 600 [0.0-0.6%]                    |
| Automated, correct                     | 317 of 326 [94.8-98.7%]                |
| False automation                       | 9 of 600 [0.7-2.8%] of labelled cases  |
| Critical urgent_symptoms=yes detected  | 81 of 114 [61.8-79.2%]                 |
| Critical urgent_symptoms=yes automated | 9 of 114 [3.7-14.5%]                   |

Critical misses: urgent_symptoms=yes cases automated, so no person read them (9): demo-0002, demo-0044, …
Critical urgent_symptoms=yes cases not detected, and read by a person (24): demo-0052, … and 4 more

### Operating point

At the agent's automation threshold 0.90, 326 of 600 cases are automated: critical
urgent_symptoms=yes automated 9 of 114 critical cases (at most 13.4%), 9 of all 600 cases (at
most 2.7%); false automation 9 of 600 labelled cases (at most 2.7%).

- 64 of the 600 cases repeat another case's input. Each counts as its own evidence here, so the
  bounds are narrower than the distinct cases alone would support.
- Each "at most" is the exact one-sided bound (Clopper-Pearson) of its count at 95% confidence.
  It is about cases like these, labelled as these are, and about this version and configuration
  of the agent.

### Gate failed: 2 limits not met

| Limit                                  | Stated       | Measured                | Verdict |
| critical urgent_symptoms=yes detected  | at least 90% | 81 of 114 [61.8-79.2%]  | not met |
| critical urgent_symptoms=yes misses    | at most 0    | 9 of 114 [3.7-14.5%]    | not met |
```

- **Counts with their intervals.** Each figure is k of n with its exact 95% interval, as
  `mendari eval` prints it. A small dataset gives a wide interval: 3 of 3 detected is between
  29% and 100%, and the summary says "insufficient evidence" beside a critical label with fewer
  than 100 cases.
- **The mix of outcomes.** How many cases were automated (`AUTO`), sent to a person
  (`HUMAN_REVIEW`), left without a recommendation (`ABSTAIN`) or refused (`BLOCK`).
- **Critical cases.** For each label under `evaluation.critical` in `agent.yaml`: how many of
  its cases the agent detected, and how many were automated, which means no person read them:
  its critical misses. The cases that went wrong are named by the id your dataset gives them,
  at most twenty of each kind.
- **The operating point.** What the agent's automation threshold lets through on these cases:
  its critical misses and its false automation, each with an exact upper bound. With a
  [target](#a-target), also the lowest threshold the cases support.
- **Who answered.** The provider variants, and the models that answered. An agent scaffolded by
  `mendari new` answers from its canned file until it is given a live endpoint (below), and the
  summary says so by naming that file's model.

No case's text is written to the summary, the log or the artifact. Case ids are: use ids that
identify no person, since everyone who can read the repository's Actions can read them.

The artifact holds `result.json` (the evaluation's result, format `mendari.eval/2`: counts,
with cases named by a hash), its ledger under `ledgers/`, and `summary.md`. The
[report format](../spec/evaluation-reports.md) describes the first two.

## Limits

A limit is what the result must be, whatever it was before. The action states two unless you
change them: every critical label is detected in at least nine of ten of its cases, and has no
critical miss, which is to say that none of its cases is automated. A new agent from
`mendari new` meets both on its twelve examples.

Change one by giving it another value, add `min-accuracy` or `max-false-automation`, or turn
one off with an empty value:

```yaml
      - uses: mendari-hq/mendari@v0.1.0
        with:
          agent: .
          dataset: test
          min-critical-detection: "95%"
          max-critical-misses: "0"
          min-accuracy: "85%"
          max-false-automation: ""
```

| Limit | What the result must show |
|---|---|
| `min-critical-detection` | Each critical label detected in at least this share of its cases |
| `max-critical-misses` | For each critical label, at most this many critical misses: its cases that were automated |
| `min-accuracy` | At least this accuracy on the primary question |
| `max-false-automation` | At most this false automation: the summary's row "False automation", a share of the labelled cases |

A limit is held to the measured share, not to its interval: on a few cases it says little. A
limit on a share needs cases to be a share of: where the dataset measures none (no case of a
critical label, no labelled case), the limit is not met, since nothing shows that it is. An
agent that declares no critical label has nothing for the two critical limits to check, and
the summary says so.

## A target

A limit is about the cases of this run. A target asks another question: which automation
threshold do cases like these support? Give one, and the summary's operating point names the
lowest threshold at which the risk stays within the target, with a stated confidence, or says
why no threshold does and what would change that.

```yaml
      - uses: mendari-hq/mendari@v0.1.0
        with:
          agent: .
          dataset: test
          target-critical-miss: "1%"
          target-false-automation: "2%"
```

| Target | What the recommended threshold must keep to |
|---|---|
| `target-critical-miss` | Critical misses at or below this share of each critical label's cases (with `target-critical-miss-of: all`, of all cases) |
| `target-false-automation` | False automation at or below this share of the labelled cases |
| `target-confidence` | The confidence both are held with, `95%` unless you give another |

A target never fails the job, and nothing edits your manifest: the recommendation is advice,
and the summary prints the one line you would change. It also needs cases: about 300 cases of
a critical label to show 1% with 95% confidence, so on a dozen examples the summary says how
many are missing. [Choosing an automation threshold](choosing-a-threshold.md) explains what
the recommendation means, what it assumes and how many cases a target needs.

A limit and a target can be on the same figure. `max-critical-misses: "0"` fails the job when
this run automated a critical case. `target-critical-miss: "1%"` asks for the threshold at
which, with 95% confidence, no more than 1% of critical cases would be.

## A baseline

A baseline is an earlier result of the same agent on the same dataset. With one, the job also
fails when the change made the agent worse, by the gate Mendari holds its own agents to:

- a critical label is detected less often than in the baseline, or is no longer measured;
- a critical label has more critical misses: more of its cases are automated;
- accuracy on the primary question, or the share of the automated cases that are wrong, is
  worse by more than the agent's `evaluation.degradation_tolerance` (3 points unless
  `agent.yaml` says otherwise).

Write the baseline once, and commit it. `evals/` is outside the agent's digest, so the file
changes nothing about the agent:

```bash
mendari eval . --dataset test --baseline none --out evals/baseline.json
```

```yaml
      - uses: mendari-hq/mendari@v0.1.0
        with:
          agent: .
          dataset: test
          baseline: evals/baseline.json
```

When a change is meant to alter the figures, run the command again and commit the new file
with the change: the pull request then shows the old figures beside the new ones, for whoever
reviews it. A baseline measured on other cases (another dataset, an edited one, or only its
first cases) is not compared, and the job fails until it is measured again.

A pull request can also be held to the baseline its base branch has, so that editing the file
in the same pull request changes nothing about what it is compared with:

```yaml
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1
        with:
          persist-credentials: false
          fetch-depth: 0
      - name: The baseline as the base branch has it
        env:
          BASE: ${{ github.event.pull_request.base.sha }}
        run: git show "$BASE:evals/baseline.json" > "$RUNNER_TEMP/baseline.json"
      - uses: mendari-hq/mendari@v0.1.0
        with:
          agent: .
          dataset: test
          baseline: ${{ runner.temp }}/baseline.json
```

That workflow runs on `pull_request` only: a push has no base to read.

To see which cases changed between two results, download both artifacts and compare them case
by case, giving the dataset's file so that cases are named by their ids:
`mendari compare old/result.json new/result.json --dataset data/test.jsonl`.

## A real model, with a secret

Without a secret, the action checks what runs offline: the manifest, the policy, the behaviour
tests, and the evaluation of whatever answers without a network. For an agent from
`mendari new` that is its canned file, `fixtures/mock_agent.json`, which says nothing about
your model.

To evaluate the live agent, give the step its endpoint and its token:

```yaml
      - uses: mendari-hq/mendari@v0.1.0
        with:
          agent: .
          dataset: test
        env:
          MENDARI_AGENT_URL: ${{ vars.MENDARI_AGENT_URL }}
          MENDARI_AGENT_TOKEN: ${{ secrets.MENDARI_AGENT_TOKEN }}
```

`MENDARI_AGENT_TOKEN` is sent only to the address in `MENDARI_AGENT_URL`, as a bearer token.
A provider variant of type `llm` takes `LLM_MODEL` and either `AI_GATEWAY_API_KEY` or, for a
server of your own, `LLM_BASE_URL` and `LLM_API_KEY`, the same way.

- **Pull requests from forks get no secret**, and neither do Dependabot's. There the variables
  are empty, the agent answers from its canned file again, and the summary's "Answered by" line
  shows it. Do not use `pull_request_target` to hand a fork's change your secret: the change is
  code and data you have not read.
- **Each case is a call.** A dataset of 600 cases is 600 calls to your agent on every push.
  Choose a dataset sized for that, and keep the larger one for a scheduled run.
- **A model can answer differently from one run to the next.** A baseline fails on any critical
  case detected before and not detected now, so a result that varies fails now and then. Hold
  a model that varies to limits rather than to a baseline, or set it to answer the same way
  each time.
- **Only synthetic or de-identified cases.** The dataset is in your repository and its cases go
  to your agent's endpoint from a runner you do not control.

## The same checks on your machine

The action runs commands you can run. A limit is an option of `mendari eval` under the name
the input has, and so are a target and the baseline:

```bash
mendari validate .
mendari test .
mendari eval . --dataset test --baseline none \
  --min-critical-detection 90% --max-critical-misses 0 \
  --target-critical-miss 1% \
  --no-worse-than evals/baseline.json
```

`mendari eval` exits 2 when a limit is not met or the result is worse than the baseline, 1 when
it could not run, and 0 otherwise; a target changes none of that. `--markdown` prints the
summary the action writes, for any other CI system: its output, appended to that system's
summary, is the whole of it.

An evaluation measures the cases someone wrote. To find the failures nobody wrote a case for,
a later step can run `mendari probe . --fail-on critical` with the command the action installed
(its `mendari` output): [Finding failures](finding-failures.md).
