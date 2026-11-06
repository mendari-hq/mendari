# Running the scoreboard

The scoreboard's results live in the repository, in the results store under `scoreboard/`. The
[early scoreboard](../grading/scoreboard.md) is generated from it. This page says how results
are run, stored and checked, what to do when a pack changes, and how a hidden set's scoreboard
is graded and stored. The rules are the [grading method](../grading/method-1.0.md)'s, sections
3 to 9, 11, 12 and 14; the formats are in the
[spec](../spec/evaluation-reports.md#the-results-store-scoreboard).

!!! warning "Engineering evaluation, not clinical validation"
    Every set on the page is public and synthetic or hand-written. A table on the page is a
    regression check, not a ranking, and nothing on it is graded. Grades are given only on a
    frozen hidden set ([below](#a-hidden-sets-scoreboard)), and the page does not show them.

## What the store holds

```text
scoreboard/
  results/<sha256>.json    a result, named by its body's SHA-256 (a public set's entry, or a
                           hidden set's answer, which has no ledger or run record here)
  ledgers/<sha256>.jsonl   each result's ledgers (one per replicate), named by their ids
  runs/<sha256>.json       each result's run records: every case's latency and tokens
  prices/<date>.json       a dated price table (none yet: nothing measured calls a model)
  canaries/<sha256>.json   a canary, named by its body's SHA-256 (none yet)
  declared/<name>.json     a pack's declared configurations, one per arm
  keys.json                the public grading keys (none yet)
  disclosures.json         the disclosures (no statement yet: every remote type's is pending)
  README.md
```

The store is append-only. A result is never edited and never taken out. The only change a stored
file may take is a valid signature, added to an unsigned result whose body stays the same. A
result that is out of date is run again, and the new result supersedes it: both stay, and the
page shows the new one and lists the old one at the end.

## Running the early scoreboard

From the repository's root:

```bash
./scripts/build-scoreboard.sh
```

It runs every local configuration of each reference pack on each of its public scoring sets: its
`test` set, its enriched set and that set's holdout. Today that is 18 results, about 2.7 MB with
their ledgers and run records. A result that is still current is kept, not run again: its
pack's digest, its set and the method are unchanged, and it is not past its stale date. Then it
writes the page, and the scores on the public page (`web/index.html`). Commit `scoreboard/`,
`docs/grading/scoreboard.md` and `web/index.html` together.

## The scores on the public page

`scripts/scoreboard_page.py` also writes the scores `web/index.html` shows, between two marker
comments; the rest of that page is written by hand. They are the Patient Inbox Router's public
`test` set, from the same results as the page: every arm, each result's figures as k of n with
their exact 95% interval, the insufficient-evidence banner, and "not measured" (or "not
applicable") for each arm with no result. Each row says "unsigned", "method draft, not in
effect" and the day it goes stale, as the page does. There is no Rank column and no best row:
the set is public, a regression check, not a ranking.

A remote model is named there only once its provider type's disclosure is stated in
`scoreboard/disclosures.json`. Until then neither is a configuration that calls it (a variant's
name, `rules_and_jev`, can name its provider): a result's row shows its arm and marks and "not
published: disclosure pending" in place of its figures, and the arm's configurations not yet
measured are counted in one row rather than named. Deploying `web/` is the owner's, after gate
G1-2.

The Decision Cards, `mendari leaderboard` and the dashboard's Provider comparison say the same
of a pack's own leaderboard: "public set: regression check, not a ranking", no rank printed,
the order the rows are listed in, and a link to the scoreboard (the dashboard links this page,
which links the scoreboard).

It is never part of `scripts/build-reference-pack.sh`. A run record holds each case's latency,
as the runner measured it, and no rebuild gives the same latency twice.

## One configuration, or one set

```bash
uv run mendari scoreboard run mendari/patient-inbox-router --dataset red_flags_holdout
uv run mendari scoreboard run mendari/patient-inbox-router --config router=rules
uv run mendari scoreboard run mendari/referral-router --every-local --again
uv run python scripts/scoreboard_page.py
```

- `--dataset NAME` names a public scoring set of the pack (repeatable). Without it, each of them
  is run. `train` and `calibration` fit the pack's models, so they are refused, and so is
  anything that is not a set the pack names: a file, and so any hidden set.
- `--config slot=variant` chooses a slot's variant; the other slots take the pack's defaults.
  `--every-local` runs each local configuration instead. The environment's `MENDARI_PROVIDERS`
  is never read: what runs is what the command names.
- A remote configuration is run only by hand, as the next section says, and is refused in CI
  before anything runs.
- `--again` runs a result that is still current again. The new result supersedes it.
- `--store DIR` uses another store. Without it, the command must be run from the repository's
  root: it never makes a new store.
- The pack is the repository's, from `packs/`. A copy on `MENDARI_PACKS_PATH`, or a pack pulled
  from a registry, is never run, checked or shown, whatever its version.

## A remote configuration: replicates, by hand

A remote configuration calls something off the machine: Jev, an LLM, or Mendari's own CQL
engine (the Clinical Trial Prescreener's `structured=cql`, which asks no model). Its answers can
change from one run to the next, so it runs 3 to 5 replicates (the method's section 10), on
your own machine, never in CI:

```bash
uv run mendari scoreboard run mendari/patient-inbox-router --config router=jev \
  --replicates 3 --prices scoreboard/prices/2026-11-05.json --dataset test
```

Before anything runs, it is refused:

- when `CI` or `GITHUB_ACTIONS` is set: a remote configuration is never run in CI;
- without `--replicates 3` to `5`. A local configuration is deterministic, so it runs one
  replicate and never takes `--replicates` above 1;
- unless the pack's declaration names it as its arm's remote configuration, and the
  declaration is committed as it is (the next section);
- without `--prices FILE` when it calls a model (Jev or an LLM): its cost is priced from that
  dated table, which the store keeps under `prices/<date>.json` when a result is priced from it.
  A configuration that calls no model (CQL) needs none: its cost is "0 (no metered calls)". A
  local configuration is never given one;
- when `scoreboard/prices/` already holds another table of the same date: a stored table never
  changes, so date the new one.

A table that cannot price the run (it lacks the model Jev or the LLM reported, or the model
changed during replicate 1, so two are reported) does not lose it: the result is stored with its
cost "unknown", and the reason, and cites no table.

The replicates run one after another, each with its own providers. Replicate 1 is the headline:
the result's figures are its figures, whichever replicate looks better. The result lists every
replicate's ledger and run record (each stored), and shows the case flip rate (k of n cases
whose primary prediction or outcome differs between any two replicates, matched by ledger hash,
with its interval) and the spread of each figure in its column (its minimum to maximum across
them). Subgroups and weighted figures have no spread: a subgroup's figures move only where its
cases flip, which the flip rate counts, and the public sets are as sampled. Replicates that
report more than one model of a type mark the result "stale: another model reported" from the
start.

A result keeps the statements `scoreboard/disclosures.json` held, for each remote provider type
the configuration calls, when it was stored. The page shows its figures only when each type had
a statement then and the file still holds that statement, word for word. Otherwise the result
reads "not published: disclosure pending": the page shows its scope, and none of its figures,
and its labels stay out of the insufficient-evidence banner. So write the statement first, then
run. A result stored while its statement was pending, or whose statement was taken out or
rewritten since (a commercial relationship begun, say), is run again (`--again`) once the file
holds the statement it should carry. The Clinical Trial Prescreener's three `structured=cql`
results were stored pending: once `cql` has a statement, run them again with `--again`, with the
sandbox's HAPI FHIR up (`docker compose up fhir`), and commit them with the page.

### Declaring a configuration

Each pack's declaration is `scoreboard/declared/<pack name>.json` (`mendari.declared/1`):

```json
{
  "format": "mendari.declared/1",
  "pack": "mendari/clinical-trial-prescreener",
  "date": "2026-11-05",
  "arms": {
    "deterministic": {"remote": {"notes": "rules", "structured": "cql"}}
  }
}
```

Per arm it names one remote configuration (`remote`), the one run on public sets with
replicates, and may name one local configuration (`local`), the one a hidden set grades. Commit
it before the run: the run refuses a declaration that is not committed as it is, so a run cannot
pick its configuration after seeing results. The result cites the declaration's SHA-256. The
check refuses a declaration that names a configuration the pack does not have, one in another
arm, or one that is not remote (or local) where it says so; and a new remote result that does
not cite the declaration as it is, or whose configuration it does not declare. A declaration
changed later leaves the results stored before as they were.

## Canaries

A canary (the method's section 12) checks, by hand, that a remote configuration still answers
as its replicates did. It is never run in CI, and nothing schedules it:

```bash
uv run mendari scoreboard canary mendari/patient-inbox-router --config router=jev --seed 7
```

It draws 30 cases of the pack's `test` set by the seed (the same seed draws the same cases),
decides them once, and compares each with replicate 1 of the configuration's current result on
`test`, which must hold replicates, on the pack's current digest and set. It stores its ledger,
its run record and the canary (`canaries/<sha256>.json`, `mendari.canary/1`): its flip rate
against replicate 1, with its interval, and whether it shows drift (that interval's lower bound
above the upper bound of the replicates' flip rate) or reports another model (for a type both it
and the replicates named: a canary whose calls all failed names none, which is not another).

- Drift marks "stale: canary drift" the remote results of the pack's arm, of whichever
  configuration was declared for it and on any set, measured before the canary's day, or no
  later than the replicates it was compared with. A local configuration's results are never
  marked: they are deterministic.
- Another model, from a canary or any later result, marks "stale: another model reported" every
  result, of any pack or configuration, whose runs named another model of that type and that
  was measured no later (the same day included: a day holds no order). The deployment has one
  `JEV_MODEL` and one `LLM_MODEL`, whatever calls them.

`scoreboard run` does not read these marks: a result they mark is still current to it (its pack,
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
they give, on its pack's current digest, set and method, measured no later than today. A remote
configuration's result must also hold 3 to 5 replicates, each one's ledger and run record in the
store, and cite its pack's declaration as the store holds it: its ledgers are recomputed from,
never re-run, which would call off the machine. Its disclosure is the one in
`scoreboard/disclosures.json`, never one the result brings; the page shows its figures only while
the file still holds that statement. `--supersedes` names the stored
result it replaces. The link is part of the result's body, so it is refused for a signed result:
link it first, then sign it.

## What CI checks

Two steps of the required `reference-pack` job, against the pull request's base (or, on a push,
the commit it was pushed onto):

```bash
uv run python scripts/scoreboard_page.py --check
uv run python scripts/check_scoreboard.py --base origin/main
```

The first fails when the page, or the scores between the markers in `web/index.html`, are not
what the store gives. It runs before `scripts/build-reference-pack.sh`, which writes the page
again. The second fails when:

- a file's name is not its content's hash, or its bytes are not in that kind's form;
- a result of the base is removed or changed, but for a valid signature added to an unsigned
  result;
- a `supersedes` link names no stored result, one of another pack, set, arm or configuration,
  one measured later, or one another result supersedes too; or one pack, set, arm and
  configuration has two current results;
- a current result's signature does not verify against `scoreboard/keys.json`;
- a public result is not what its stored inputs give, byte for byte, or its local ledger does
  not re-run to the same bytes (a remote configuration's ledgers are never re-run);
- a new result is measured after today, or is of a remote configuration without 3 to 5
  replicates and its pack's declaration of it;
- a declaration names a configuration its pack does not have, in another arm, or not remote
  (or local) where it says;
- a canary of the base is removed or changed, or a canary is not what its ledger, its run
  record and the result it was compared with give;
- a hidden set's result does not fit `mendari.hidden_scoreboard/1` field by field, is not of a
  pack Mendari maintains or, when new, is under another method or measured after today. It is
  never recomputed: no ledger of a hidden set is kept.

It needs no secret, writes nothing and calls nothing off the machine. Both read the packs from
the repository's `packs/`, never from `MENDARI_PACKS_PATH` or the registry cache.

A result signed with a publisher's or a registry's key is refused where that key is pinned:
by `mendari scoreboard verify`, and by the check run on a machine that pulled packs from that
registry (`mendari pull` pins its keys). CI pins none. There, the review of each change to
`scoreboard/keys.json` is what keeps such a key out of the keys file.

## When a pack, a set or the method changes

A pack change gives the pack another digest, and its results go stale. The check keeps them:
it recomputes them from their own inputs, and does not re-run their ledgers, which were made
by the earlier digest. The page shows them as "stale: pack changed". So a pull request that
changes a pack also regenerates the page, and the page names the stale results.
`./scripts/build-reference-pack.sh` writes it last, after the packs' data, models, benchmarks
and cards, and runs no result: commit the page with them.

A result whose set or method changed is kept as it is, and shown as "stale: set changed" or
"stale: method changed". Any edit to the method re-pins its hash while it is a draft, so every
result goes stale with it.

To refresh, run `./scripts/build-scoreboard.sh` again, in the same pull request or a later one.
Each stale result is run again, and the new result supersedes it. A new result must be on its
pack's current digest, set and method, so the check can recompute it and re-run its ledger.

## Signing

Results are stored unsigned, and the page says so. The owner signs them on their own machine,
never in CI ([verifying a scoreboard result](../grading/verifying.md)):

```bash
MENDARI_GRADING_SIGNING_KEY="$(cat ~/private/mendari-grading.key)" \
  mendari scoreboard sign scoreboard/results/*.json
```

Signing adds the signature and changes nothing else, so the result keeps its name, and the check
accepts the change once the public key is in `scoreboard/keys.json`. A current result's
signature must keep verifying. When a key leaks and is taken out of the keys file, its results
are run again with `--again`, in the same pull request: the new results supersede them, and the
old ones stay in the store, as they were signed.

## A hidden set's scoreboard

Grades and ranks are given only on a frozen hidden set (the method, sections 3 to 9), through the
hidden-set grading route ([Grading a hidden set](evalsets.md#the-hidden-set-scoreboard)). The
grading key is the deployment's, minted by the operator; the set's cases never leave the
deployment's database, and the answer holds aggregates only (`mendari.hidden_scoreboard/1`).

1. **Declare.** Before any hidden set of the pack is graded, the owner declares one
   configuration per arm and commits the declaration (`scoreboard/declared/<pack>.json`, the
   method's section 3). Under the method 1.0 a hidden set is graded with local providers only
   (owner decision 2), so the deterministic arm is the one declared arm that runs. Take the
   SHA-256 of the committed file:

    ```bash
    shasum -a 256 scoreboard/declared/patient-inbox-router.json
    ```

    The declaration's format, `mendari.declared/1`, comes with LP-17h, with the
    `scoreboard/declared/` folder and the store's check of it: until it lands, nothing defines
    what the file holds and no check reads it, so a declaration cannot be committed yet. The
    grading route never reads the file either: it only cites the SHA-256 sent, and the answer
    records the configuration each arm ran. Until LP-17h, keep to runs you do not mean to
    store and sign.

2. **Post it.** Send the declaration's arms and its SHA-256 to the set's grading route, with
   the deployment's grading key, as the [evaluation-set runbook](evalsets.md#the-hidden-set-scoreboard)
   shows. Each declared arm names a variant for every slot of the pack, as the declaration
   does, and is checked before any case is read: one that calls a remote provider is refused
   `remote_provider`, one that leaves a slot out or is declared under another arm `wrong_arm`,
   and nothing is read or counted. Then one grading read decides every declared arm on the same
   cases: one chained `evalset.graded`, one of the set's runs. Save the answer as it came:
   `scoreboard-answer.json`. Undeclared arms read "not graded here" (their configurations call a
   remote provider) or "not applicable", and the ranking reads "off: one arm graded" while only
   the deterministic arm is local.

3. **Store it.** From the repository's root:

    ```bash
    uv run mendari scoreboard add scoreboard-answer.json
    uv run mendari scoreboard add scoreboard-answer.json --supersedes <sha256>
    ```

    It is stored as it was answered, under its body's SHA-256, which leaves the run block
    (`run`, `runs_left`) out: a second read of the same set on the same day is the same result,
    stored already. One pack and set has one current result, its arms in it; a later day's
    answer supersedes the earlier with `--supersedes`. It is refused when it does not fit its
    closed schema, is of a pack Mendari does not maintain, is under another method, is
    measured after today or says the method is in effect when it is not (or the reverse), or
    does not agree with its own figures: each interval with its k and n, each critical label's
    evidence with its distinct positives, each grade (every attestation's state and draft) and
    the banner with the figures, and the ranking with the grades. Its k and n are never
    recomputed, since no ledger of a hidden set is kept, and the page of public sets does not
    show it.

4. **Sign it.** The owner signs it on their own machine, as any result
   ([Signing](#signing)). The signature covers its body and its run block, so a signed hidden-set
   result is the owner's attestation of that run's numbers: no one else can recompute it, since
   no ledger of a hidden set is kept. It carries the set's freeze commitment, which the grading
   key does not sign on its own.

While the method has no effective date, the answer's scope says it is not in effect, and every
attestation (and a ranking, once one is on) is a draft. A 600-case set runs in about 5 seconds
through the route, its read included, well within the route's deadline (300 seconds), so the
run stays in the route and no figure is dropped.
