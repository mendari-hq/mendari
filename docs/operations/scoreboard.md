# Running the scoreboard

The scoreboard's results live in the repository, in the results store under `scoreboard/`. The
[early scoreboard](../grading/scoreboard.md) is generated from it. This page says how results
are run, stored and checked, and what to do when a pack changes. The rules are the
[grading method](../grading/method-1.0.md)'s, sections 11, 12 and 14; the formats are in the
[spec](../spec/evaluation-reports.md#the-results-store-scoreboard).

!!! warning "Engineering evaluation, not clinical validation"
    Every set here is public and synthetic or hand-written. A table on the page is a regression
    check, not a ranking, and nothing on it is graded.

## What the store holds

```text
scoreboard/
  results/<sha256>.json    a result, named by its body's SHA-256
  ledgers/<sha256>.jsonl   each result's ledger, named by its id
  runs/<sha256>.json       each result's run record: every case's latency and tokens
  prices/<date>.json       a dated price table (none yet: no local configuration is priced)
  keys.json                the public grading keys (none yet)
  disclosures.json         the disclosures (none yet: every remote type's statement is pending)
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
- A remote configuration is refused before anything runs. That includes the Clinical Trial
  Prescreener's `structured=cql`, which asks no model but calls Mendari's CQL engine: the page
  says it is not measured yet.
- `--again` runs a result that is still current again. The new result supersedes it.
- `--store DIR` uses another store. Without it, the command must be run from the repository's
  root: it never makes a new store.
- The pack is the repository's, from `packs/`. A copy on `MENDARI_PACKS_PATH`, or a pack pulled
  from a registry, is never run, checked or shown, whatever its version.

## Adding a result made elsewhere

```bash
uv run mendari scoreboard add RESULT.json
uv run mendari scoreboard add RESULT.json --supersedes <sha256>
```

`add` stores a result under its body's SHA-256, and never writes over a file. A public set's
result is stored only when its ledger and run record are in the store already and it is what
they give, on its pack's current digest, set and method, measured no later than today. It must
be of a local configuration: a remote one's ledger could not be re-run without a call off the
machine, so nothing would check its decisions. A remote configuration's disclosure is the one in
`scoreboard/disclosures.json`, never one the result brings. `--supersedes` names the stored
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
- a public result is not what its stored inputs give, byte for byte, or its ledger does not
  re-run to the same bytes;
- a new result is of a remote configuration, or is measured after today.

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
