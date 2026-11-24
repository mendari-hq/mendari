# Running the scoreboard

The scoreboard engine (`mendari scoreboard ...`) runs an agent's configurations on its public
scoring sets, grades frozen hidden sets through the grading route, and keeps every result in a
results store: a folder, `scoreboard/` in the working directory unless `--store` names another.
Mendari publishes no scoreboard of its own and scores no agent: this page is for a deployment
that keeps a store of its own. It says how results are run, stored and signed, what to do when
an agent changes, and how a hidden set's scoreboard is graded and stored. The rules are the
[grading method](../grading/method-1.0.md)'s, sections 3 to 9, 11, 12 and 14; the formats are in
the [spec](../spec/evaluation-reports.md#the-results-store-scoreboard).

!!! warning "Engineering evaluation, not clinical validation"
    A public set is synthetic or hand-written, and anything can be tuned to it. A table of
    public-set results is a regression check, not a ranking, and nothing on it is graded. Grades
    are given only on a frozen hidden set ([below](#a-hidden-sets-scoreboard)).

## What the store holds

```text
scoreboard/
  results/<sha256>.json    a result, named by its body's SHA-256 (a public set's entry, or a
                           hidden set's answer, which has no ledger or run record here)
  ledgers/<sha256>.jsonl   each result's ledgers (one per replicate), named by their ids
  runs/<sha256>.json       each result's run records: every case's latency and tokens
  prices/<date>.json       a dated price table
  canaries/<sha256>.json   a canary, named by its body's SHA-256
  declared/<name>.json     an agent's declared configurations, one per arm
  keys.json                the public grading keys
  disclosures.json         the disclosures, per remote provider type
```

The store is append-only. A result is never edited and never taken out. The only change a stored
file may take is a valid signature, added to an unsigned result whose body stays the same. A
result that is out of date is run again, and the new result supersedes it: both stay, and a page
shows the new one and lists the old one at the end.

## Rendering a page

```bash
uv run python scripts/scoreboard_page.py scoreboard --out scoreboard.md
```

It writes the store as a Markdown page, from the store and nothing else, so the same store gives
the same page: every figure k of n with its exact 95% interval, the insufficient-evidence banner,
"not measured" (or "not applicable") for each arm with no result, whether each result is signed,
and the day it goes stale. There is no Rank column and no best row: a public set is a
regression check, not a ranking.

A remote model is named there only once its provider type's disclosure is stated in the store's
`disclosures.json`. Until then neither is a configuration that calls it (a variant's name,
`rules_and_jev`, can name its provider): a result's row shows its arm and marks and "not
published: disclosure pending" in place of its figures, and the arm's configurations not yet
measured are counted in one row rather than named. A result's figures and models are shown only
while the disclosures file still holds, word for word, the statement each type it calls had when
it was stored, as the section on replicates below says.

The Decision Cards, `mendari leaderboard` and the dashboard's Provider comparison say the same
of an agent's own leaderboard: "public set: regression check, not a ranking", no rank printed,
the order the rows are listed in, and the dashboard links this page.

A store is never rebuilt with an agent's data: a run record holds each case's latency, as the
runner measured it, and no rebuild gives the same latency twice.

## One configuration, or one set

```bash
uv run mendari scoreboard run mendari/portal-message-agent --dataset test
uv run mendari scoreboard run mendari/portal-message-agent --config reader=rules
uv run mendari scoreboard run mendari/portal-message-agent --every-local --again
uv run python scripts/scoreboard_page.py scoreboard --out scoreboard.md
```

- `--dataset NAME` names a public scoring set of the agent (repeatable). Without it, each of them
  is run. `train` and `calibration` fit the agent's models, so they are refused, and so is
  anything that is not a set the agent names: a file, and so any hidden set.
- `--config slot=variant` chooses a slot's variant; the other slots take the agent's defaults.
  `--every-local` runs each local configuration instead. The environment's `MENDARI_PROVIDERS`
  is never read: what runs is what the command names.
- A remote configuration is run only by hand, as the next section says, and is refused before
  anything runs in CI, or when its provider is not set up on the machine (the CQL engine's URL,
  Jev's endpoint and model, an LLM's model), since every case would fail.
- `--again` runs a result that is still current again. The new result supersedes it.
- `--store DIR` uses another store. Without it, the store is `scoreboard/` in the working
  directory.
- The agent is one bundled with Mendari, from `agents/`. A copy on `MENDARI_AGENTS_PATH`, or an agent pulled
  from a registry, is never run, checked or shown, whatever its version.

## A remote configuration: replicates, by hand

A remote configuration calls something off the machine: an agent, Jev, an LLM, or Mendari's own
CQL engine (which asks no model). The demo agent's `reader=agent` is one. Its answers can
change from one run to the next, so it runs 3 to 5 replicates (the method's section 10), on
your own machine, never in CI:

```bash
MENDARI_AGENT_URL=https://your-agent.example/decide \
  uv run mendari scoreboard run mendari/portal-message-agent --config reader=agent \
  --replicates 3 --prices scoreboard/prices/2026-11-05.json --dataset test
```

Before anything runs, it is refused:

- when `CI` or `GITHUB_ACTIONS` is set: a remote configuration is never run in CI;
- without `--replicates 3` to `5`. A local configuration is deterministic, so it runs one
  replicate and never takes `--replicates` above 1;
- unless the agent's declaration names it as its arm's remote configuration, and the
  declaration is committed as it is (the next section);
- without `--prices FILE` when it calls a model (an agent, Jev or an LLM): its cost is priced from that
  dated table, which the store keeps under `prices/<date>.json` when a result is priced from it.
  A configuration that calls no model (CQL) needs none: its cost is "0 (no metered calls)". A
  local configuration is never given one;
- when `scoreboard/prices/` already holds another table of the same date: a stored table never
  changes, so date the new one.

A table that cannot price the run (it lacks the model Jev or the LLM reported, or the model
changed during replicate 1, so two are reported) does not lose it: the result is stored with its
cost "unknown", and the reason, and cites no table. The reason reads the same whichever
table is at hand, for every model type the configuration calls: a configuration of Jev and
an LLM whose table prices only one of their models reads "jev, llm: no price table holds all
their models", and one whose type reported two models reads that type's reason.

The replicates run one after another, each with its own providers. Replicate 1 is the headline:
the result's figures are its figures, whichever replicate looks better. The result lists every
replicate's ledger and run record (each stored), and shows the case flip rate (k of n cases
whose primary prediction or outcome differs between any two replicates, matched by ledger hash,
with its interval) and the spread of each figure in its column (its minimum to maximum across
them). Subgroups and weighted figures have no spread: a subgroup's figures move only where its
cases flip, which the flip rate counts, and the public sets are as sampled. Replicates that
report more than one model of a type mark the result "stale: another model reported" from the
start.

A result keeps the statements the store's `disclosures.json` held, for each remote provider type
the configuration calls, when it was stored. A page shows its figures only when each type had
a statement then and the file still holds that statement, word for word. Otherwise the result
reads "not published: disclosure pending": a page shows its scope, and none of its figures,
and its labels stay out of the insufficient-evidence banner. So write the statement first, then
run. A result stored while its statement was pending, or whose statement was taken out or
rewritten since (a commercial relationship begun, say), is run again (`--again`) once the file
holds the statement it should carry.

### Declaring a configuration

Each agent's declaration is `declared/<agent name>.json` in the store (`mendari.declared/1`):

```json
{
  "format": "mendari.declared/1",
  "pack": "mendari/portal-message-agent",
  "date": "YYYY-MM-DD",
  "arms": {
    "llm": {"remote": {"reader": "agent"}}
  }
}
```

`pack` is the agent's id (the key keeps the word Mendari used before, as signed results hold
it). Per arm it names one remote configuration (`remote`), the one run on public sets with
replicates, and may name one local configuration (`local`), the one a hidden set grades. Commit
it before the run: the run refuses a declaration that is not committed as it is, so a run cannot
pick its configuration after seeing results. The result cites the declaration's SHA-256. The
check refuses a declaration that names a configuration the agent does not have, one in another
arm, or one that is not remote (or local) where it says so; and a new remote result that does
not cite the declaration as it is, or whose configuration it does not declare. A declaration
changed later leaves the results stored before as they were.

## Canaries

A canary (the method's section 12) checks, by hand, that a remote configuration still answers
as its replicates did. It is never run in CI, and nothing schedules it:

```bash
uv run mendari scoreboard canary mendari/portal-message-agent --config reader=agent --seed 7
```

It draws 30 cases of the agent's `test` set by the seed (the same seed draws the same cases),
decides them once, and compares each with replicate 1 of the configuration's current result on
`test`, which must hold replicates, on the agent's current digest and set. It stores its ledger,
its run record and the canary (`canaries/<sha256>.json`, `mendari.canary/1`): its flip rate
against replicate 1, with its interval, and whether it shows drift (that interval's lower bound
above the upper bound of the replicates' flip rate) or reports another model (for a type both it
and the replicates named: a canary whose calls all failed names none, which is not another).

- Drift marks "stale: canary drift" the remote results of the agent's arm, of whichever
  configuration was declared for it and on any set, measured before the canary's day, or no
  later than the replicates it was compared with. A local configuration's results are never
  marked: they are deterministic.
- Another model, from a canary or any later result, marks "stale: another model reported" every
  result, of any agent or configuration, whose runs named another model of that type and that
  was measured no later (the same day included: a day holds no order). The deployment has one
  `JEV_MODEL` and one `LLM_MODEL`, whatever calls them.

`scoreboard run` does not read these marks: a result they mark is still current to it (its agent,
set and method are unchanged), and it prints "kept". Run it again with `--again`; a result run
again from the canary's day on is not marked by it. Canaries are kept as they were run: the
check recomputes each from its inputs, and refuses one removed or changed.

## Adding a result made elsewhere

```bash
uv run mendari scoreboard add RESULT.json
uv run mendari scoreboard add RESULT.json --supersedes <sha256>
```

`add` stores a result under its body's SHA-256, and never writes over a file. A public set's
result is stored only when its ledger and run record are in the store already and it is what
they give, on its agent's current digest, set and method, measured no later than today. A remote
configuration's result must also hold 3 to 5 replicates, each one's ledger and run record in the
store, and cite its agent's declaration as the store holds it: its ledgers are recomputed from,
never re-run, which would call off the machine. Its disclosure is the one in
the store's `disclosures.json`, never one the result brings; a page shows its figures only while
the file still holds that statement. `--supersedes` names the stored
result it replaces. The link is part of the result's body, so it is refused for a signed result:
link it first, then sign it.

## When an agent, a set or the method changes

An agent change gives the agent another digest, and its results go stale. The store keeps them:
it recomputes them from their own inputs, and does not re-run their ledgers, which were made by
the earlier digest. A page shows them as "stale: agent changed".

A result whose set or method changed is kept as it is, and shown as "stale: set changed" or
"stale: method changed". Any edit to the method re-pins its hash while it is a draft, so every
result goes stale with it.

To refresh, run the stale configurations again (`mendari scoreboard run ... --again`). The new
result supersedes the stale one. A new result must be on its agent's current digest, set and
method, so the store can recompute it and re-run its ledger.

## Signing

Results are stored unsigned, and a page says so. The key holder signs them on their own
machine, never in CI ([verifying a scoreboard result](../grading/verifying.md)):

```bash
MENDARI_GRADING_SIGNING_KEY="$(cat ~/private/mendari-grading.key)" \
  mendari scoreboard sign scoreboard/results/<sha256>.json      # one or more files
```

Name the results to sign. `sign` checks every file before it writes any, and refuses the whole
batch for one result that is signed already, or that was made under an earlier hash of the
method (which no longer verifies), so `scoreboard/results/*.json` works only while no result is
signed and the method has not changed since any was made.

Signing adds the signature and changes nothing else, so the result keeps its name, and the store
accepts the change once the public key is in its `keys.json`. A page counts the signed results
and marks each unsigned one, and `sign` writes no page: write it again. A current result's
signature must keep verifying. When a key leaks and is taken out of the keys file, its results
are run again with `--again`, in the same pull request: the new results supersede them, and the
old ones stay in the store, as they were signed.

## A hidden set's scoreboard

Grades and ranks are given only on a frozen hidden set (the method, sections 3 to 9), through the
hidden-set grading route ([Grading a hidden set](evalsets.md#the-hidden-set-scoreboard)). The
grading key is the deployment's, minted by the operator; the set's cases never leave the
deployment's database, and the answer holds aggregates only (`mendari.hidden_scoreboard/1`).

1. **Declare.** Before any hidden set of the agent is graded, the deployment declares the arm's
   local configuration, the one a hidden set grades, as its `local` entry in the agent's
   declaration (`scoreboard/declared/<agent name>.json`, `mendari.declared/1`: the method's
   section 3, and [Declaring a configuration](#declaring-a-configuration)), and commits it.
   Under the method 1.0 a hidden set is graded with local providers only (owner decision 2), so
   the deterministic arm is the one declared arm that runs:

    ```json
    {
      "format": "mendari.declared/1",
      "pack": "mendari/portal-message-agent",
      "date": "YYYY-MM-DD",
      "arms": {
        "deterministic": {"local": {"reader": "rules"}}
      }
    }
    ```

    Take the SHA-256 of the committed file:

    ```bash
    shasum -a 256 scoreboard/declared/portal-message-agent.json
    ```

    The grading route never reads the file: it cites the SHA-256 sent, and the answer records
    the configuration each arm ran. The store reads it when the answer is added (step 3).

2. **Post it.** Send each arm's `local` configuration as `arms`, and the declaration's SHA-256 as
   `declared`, to the set's grading route, with the deployment's grading key, as the [evaluation-set
   runbook](evalsets.md#the-hidden-set-scoreboard) shows. Each declared arm names a variant for
   every slot of the agent, as the declaration does, and is checked before any case is read: one that
   calls a remote provider is refused `remote_provider`, one that leaves a slot out or is declared
   under another arm `wrong_arm`, and nothing is read or counted. Then one grading read decides
   every declared arm on the same cases: one chained `evalset.graded`, one of the set's runs. Save
   the answer as it came: `scoreboard-answer.json`. Undeclared arms read "not graded here" (their
   configurations call a remote provider) or "not applicable", and the ranking reads "off: one arm
   graded" while only the deterministic arm is local.

3. **Store it.** From the folder that holds the store:

    ```bash
    uv run mendari scoreboard add scoreboard-answer.json
    uv run mendari scoreboard add scoreboard-answer.json --supersedes <sha256>
    ```

    It is stored as it was answered, under its body's SHA-256, which leaves the run block (`run`,
    `runs_left`) out: a second read of the same set on the same day is the same result, stored
    already. One agent and set has one current result, its arms in it; a later day's answer
    supersedes the earlier with `--supersedes`. It is refused when it does not fit its closed
    schema, is of an agent Mendari does not maintain, is under another method, is measured after today
    or says the method is in effect when it is not (or the reverse), does not cite its agent's
    declaration as the store holds it or grades other arms or configurations than the `local` ones
    it declares (a declaration changed after an answer was stored leaves that answer as it was), or
    does not agree with its own figures: each interval with its k and n, each critical label's
    evidence with its distinct positives, each grade (every attestation's state and draft) and the
    banner with the figures, and the ranking with the grades. Its k and n are never recomputed,
    since no ledger of a hidden set is kept, and the page of public sets does not show it.

4. **Sign it.** The key holder signs it on their own machine, as any result
   ([Signing](#signing)). The signature covers its body and its run block, so a signed hidden-set
   result is the key holder's attestation of that run's numbers: no one else can recompute it, since
   no ledger of a hidden set is kept. It carries the set's freeze commitment, which the grading
   key does not sign on its own.

While the method has no effective date, the answer's scope says it is not in effect, and every
attestation (and a ranking, once one is on) is a draft. A 600-case set runs in about 5 seconds
through the route, its read included, well within the route's deadline (300 seconds), so the
run stays in the route and no figure is dropped.
