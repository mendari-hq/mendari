# Hidden evaluation sets

A **hidden evaluation set** is a set of hand-written cases that a pack's authors never see, kept
to measure the pack honestly (LP-11,
[ADR-0011](../decisions/ADR-0011-hidden-evaluation-sets.md)). Clinicians label its cases blind,
and a grading run will answer aggregates only. This runbook covers what is built so far: making
a set, adding and removing its cases, drawing them for labelling, assigning them to labellers,
following the labelling, adjudicating disputes and measuring agreement and coverage, listing
sets and verifying them, rotating the keys, rebuilding the hidden-case index and the sealed
export, all from the key holder's command line. No route serves a set yet, so no person can
label: the labellers' screen, freezing and grading come in later steps of LP-11.

## Where sets live

Sets live only in the deployment's own database (locally, a developer's SQLite or Postgres with
synthetic sets), in the tables `evalset_sets`, `evalset_cases`, `evalset_index`,
`evalset_assignments` and `evalset_labels`: never in this repository, a pack directory, a
shipped fixture or a pack digest. Each set belongs to one organization and one pack.

Everything about a case is sealed with AES-256-GCM under `MENDARI_EVALSET_KEY`, each value bound
to its kind, its set and its case: its input, and its attributes (the author, language, stratum
and source, the id its author gave it, an intended label if any) with a 32-byte salt. A set's
header (the pack's id, the set's name, the pack's questions with their labels and its critical
labels) is sealed too, and so is each label, bound to its own id as well, and the set's measured
agreement and coverage. What stays in the clear is ids, states, a case's status, times, keyed
hashes, and who labels which case in which round. A case's id is a UUID the store mints.

The chain records each change with ids, keyed hashes and commitments only, under the set's own
chain id (`evalset:<set id>`):

| Event | Payload |
|---|---|
| `evalset.created` | the set's id and organization |
| `evalset.case_added` | the set, the organization, the case's keyed hash (`hmac-sha256:<index key id>:<hex>`) and its commitment (`case_ref`: the SHA-256 of a fixed prefix, the case's salt and its set, id, input and attributes) |
| `evalset.case_removed` | the set, the organization and the case's keyed hash |
| `evalset_index.epoch` | the index key's id, the first time a case is indexed under it (a deployment event, under `keys`) |
| `evalset.rotated` | one batch of a key rotation: the key re-sealed under (`to`), how many values each other key had sealed (`from`), and how many rows of each table (`counts`) (a deployment event, under `keys`) |
| `evalset_index.rebuilt` | the hidden-case index rebuilt: the index key's id and how many cases (a deployment event, under `keys`) |
| `evalset.sampled` | the set, the organization, how many cases were drawn and the draw's seed (never a stratum) |
| `evalset.assigned` | the set, the organization, the case's keyed hash, the assignment's id, the labeller's user id and the round (`first` or `adjudication`) |
| `evalset.case_read` | the set, the organization, the case's keyed hash, the assignment and the labeller: each time a labeller is shown a case |
| `evalset.labelled` | the set, the organization, the case's keyed hash, the labeller, the round and the label's commitment (`label_ref`: the label's id and the SHA-256 of a fixed prefix, the label's salt and its set, case, id, assignment and values) |
| `evalset.measured` | the set, the organization and how many cases its agreement and coverage were measured over |

An organization's administrators see their own sets' events in the administrative list and its
FHIR export, as ids and hashes: they can tell that cases were added, removed, read and labelled,
by whom (a user id), and which went to adjudication, never what they hold or how anyone
labelled them. The command line prints ids, line numbers, case hashes and counts, never a
case's text or a set's name; only `evalset agreement` and `evalset coverage` name labels (counts
and coefficients by label) and `evalset coverage` the strata and languages, as aggregates that
name no case.

## The keys

The feature is off unless both `MENDARI_EVALSET_KEY` and `MENDARI_EVALSET_INDEX_KEY` are set
([Deploying with PHI](../deploying-with-phi.md), section 8). The owner makes them, keeps a copy off the laptop,
and sets them as Secret for the hosted Production environment only; the commands below run with
the same two values in the shell. The index key keys the hidden-case index and the chained case
hashes, never a label; the two differ from each other and from every PHI key.

The database remembers the evaluation-set keys a writer held, so a stray key is caught. Before
every write, and when a server with the keys starts, Mendari refuses:

- a current key that a rotation retired (it seals nothing more);
- an index key that a reindex replaced (it indexes nothing more);
- any of the keys that the database knows as a PHI key or input hash key;
- a key in the other role: a key of the ring that the database knows as an index key, or an
  index key it knows as a key of the ring, earlier keys included;
- once a set exists, a ring holding none of the keys the database knows: whatever such a key
  sealed, no one reading with the others could open.

To change keys, follow [Rotating the evaluation-set key](#rotating-the-evaluation-set-key):
the new key is added as a read key first, then swapped in, and a rotation re-seals everything
under it before the old one is dropped. A PHI key or input hash key the database knows as an
evaluation-set key is refused too, and never marked a PHI key: a server with one does not
start, and the sets stay writable with their own keys.

## Making a set and adding its cases

Each command takes the server's database from `--db` or `$DATABASE_URL`. `create`, `add` and
`remove-case` refuse a database that is not the server's (one that never held a decision, a
deployment, a hold or a set). `list` opens it read-only and refuses one that is not Mendari's;
an unused one lists nothing.

```bash
mendari evalset create mendari/patient-inbox-router --org clinic-a --name "Inbox, spring" --db "$DATABASE_URL"
mendari evalset add <set id> ~/hidden/inbox-spring.jsonl --db "$DATABASE_URL"
mendari evalset remove-case <set id> <case hash> --db "$DATABASE_URL"
mendari evalset list --db "$DATABASE_URL"
```

`create` prints the set's id. The organization must have a record if the database has any.

A file of cases is JSON Lines, one case a line, as a pack's dataset is: the pack's `input`, and
the case's `attributes`. Keep it outside Mendari's package and any checkout of its repository:
`add` refuses a file there, before reading it.

```json
{"id": "inbox-u01", "input": {"message": {"body": "...", "channel": "portal"}}, "labels": {"urgency": "routine"}, "attributes": {"author": "a-clinician", "language": "en", "stratum": "routine", "source": "hand_written", "template": null}}
```

- `attributes.source` is `hand_written`: de-identified cases wait for B20.
- `attributes.template` is null or left out: a case written from a template is refused.
- `attributes.author`, `attributes.language` and `attributes.stratum` are given.
- `id` (the author's own) and `labels` (an intended label, of the pack's questions) are optional;
  both are kept sealed among the attributes.
- A `system` or `feedback` block, as `mendari feedback export` writes, is refused: it holds what
  a model decided or what people rated.
- An input equal to a public case's is refused, exactly or once its text is folded (`norm/v1`):
  the reference packs' data and the example datasets as this release bundles them, and each
  synthetic generator's output at its default seed. A paraphrase of a public case passes this
  check: the author's care and review stand in for it.

`add` takes the whole file or nothing: a refused line refuses it all, named by its line number,
never its text. It prints each case's line and keyed hash; `remove-case` takes that hash, and
works on a draft set only. Removing a case removes its index rows and chains
`evalset.case_removed`.

`list` prints every set (or `--org`'s) with its state and number of cases. It reads only, and
needs no key.

## Labelling a set

Labelling is blind: each drawn case is labelled by two clinicians who never see each other's
answer, the case's attributes, its author's intended label or a model's output; a case they
disagree on goes to a third, who works from the input alone, and the majority on each question
settles it. The key holder draws the cases, assigns them and adjudicates disputes from the
command line; the labellers label through a screen that a later step of LP-11 adds (no route
serves a case yet). Each command takes the server's database from `--db` or `$DATABASE_URL`,
works on a draft set only, and prints ids, case hashes, user ids and counts.

**Who labels.** A labeller is a person with the `labeller` role on a membership of the set's
organization (memberships mode, [accounts](accounts.md)), named by their user id
(`user:` and 16 hex digits, as the members list shows it). No one who manages the
organization's hidden sets (an administrator) is ever assigned a case: separation of duties.
Agreement and coverage are shown to the key holder only, never to a labeller.

**1. Draw the cases.** A seeded draw within each stratum (the cases' `attributes.stratum`):

```bash
mendari evalset sample <set id> --per-stratum 25 --seed 0 --db "$DATABASE_URL"
mendari evalset sample <set id> --cases 100 --seed 0 --db "$DATABASE_URL"
```

`--per-stratum N` draws N more from each stratum (all that are left of a smaller one); `--cases
N` draws N more in all, shared out in proportion to the cases left in each stratum. The same set
and seed draw the same cases. Each drawn case's status becomes `sampled`, and every drawn case
of a stratum is given its weight, the stratum's cases over those drawn from it (N_h / n_h),
sealed beside its attributes; a later draw weighs them again. The chain records how many were
drawn and the seed, never a stratum. A case not drawn is not labelled.

**2. Assign them.** Two labellers a case, balanced across labellers and across pairs:

```bash
mendari evalset assign <set id> --labeller user:0123456789abcdef --labeller user:fedcba9876543210 \
  --labeller user:00112233445566ff --seed 0 --db "$DATABASE_URL"
```

The pair of the labellers named that has shared the fewest cases (counting the assignments the
set already has) is given the next one: while the same labellers are named, pairs differ by one
case at most, and labellers by two at most.
Give two labellers or more. Each must hold the labeller role in the set's organization now, and
none may be one of its administrators: any other refuses the whole run, and nothing is written.
The assignments the set already has count, so running it again gives only the cases that still
lack a labeller. An open assignment whose membership has ended, been replaced, or lost the
labeller role (or gained an administrator's) is withdrawn on the next run, and its case given to
another labeller of those named, never the case's other labeller.

**3. Follow the labelling.**

```bash
mendari evalset progress <set id> --db "$DATABASE_URL"
```

It prints the set's cases by status, and each labeller's assignments in each round, by user id:
assigned, labelled, open and withdrawn. It names no case, reads only and needs no key. A case is
`draft` (not drawn), `sampled` (waiting for its first-pass labels), `agreed` or `disputed` (both
are in: the same on every question, or not), `resolved` or `unresolved` (its adjudicator's label
is in: a majority on every question, or a three-way split on one).

**4. Adjudicate.** Each disputed case is given a third labeller, who is neither of its first two:

```bash
mendari evalset adjudicate <set id> --labeller user:0123456789abcdef --labeller user:fedcba9876543210 \
  --labeller user:00112233445566ff --db "$DATABASE_URL"
```

Each disputed case goes to a labeller named who is not one of its first two, so the list must
hold such a person for every disputed case, or the whole run refuses and nothing is written:
with three first-pass labellers, name all three. The adjudicator is shown what the first
labellers were, the input and the questions only, never their labels. The same refusals and
withdrawals apply. An unresolved case keeps the set from being frozen (a later step) until it is
removed (`mendari evalset remove-case`), which removes its assignments and labels with it; what
settles a three-way split instead is the owner's decision still open.

**5. Measure agreement and coverage.**

```bash
mendari evalset agreement <set id> [--seed 0] [--resamples 2000] [--json] --db "$DATABASE_URL"
mendari evalset coverage <set id> [--json] --db "$DATABASE_URL"
```

Each computes both and stores them sealed as the set's aggregates (chained: `evalset.measured`),
then prints its part. Agreement is over the first-pass labels of the cases both labellers
answered: percent agreement as k of n with its exact interval, Cohen's kappa for each pair of
labellers and Krippendorff's alpha across them, with seeded bootstrap intervals
(`mendari.agreement/1`). Coverage is over the drawn cases and their settled labels (an agreed
case's, a resolved case's majority): distinct cases and positives, the warning under 100
distinct positives of a critical label, and the mix of languages, sources and strata
(`mendari.coverage/1`). Both name labels and counts, never a case; `coverage` also names the
strata and languages, so keep its output as you would the set. The pack is the one the set was
made for, found by its id; give `--pack` (an id or a directory) if it does not resolve where you
run the command. `coverage` uses seed 0 and 2,000 resamples for the agreement it stores beside
it: run `agreement` afterwards to store other settings. A set whose drawn cases or labels change
while it is measured is refused: run it again.

**A second round.** To label more, add cases (`mendari evalset add`), draw again (`sample`), and
assign again (`assign`): only the new draws get labellers, and the first round's cases keep
theirs, even while their labels are still coming in. Then follow, adjudicate and measure as
above. `mendari audit verify --evalsets` holds between every step.

## Verifying

```bash
mendari audit verify --evalsets --db "$DATABASE_URL"
```

With both keys in the shell, a full verification also checks the sets and cases: each set's
sealed values (its header; its aggregates once measured, and its freeze record once frozen)
open under its own set id; each case has its row (or a later removal), in its set and
organization, and each row its event; its keyed hash is recomputed when it is under the current
index key; its input and attributes open and give the commitment its event chained. Each
assignment has its row, with its case, labeller and round, and each label its row, answering
that labeller's assignment in that round; each label opens under its own set, case and id and
gives the commitment its event chained (unless its case was removed since, when they went with
it). A row altered, moved or forged stops the verification at its event (`broken_at`); a row
without its event is counted (`unchained`); a row sealed under a key this database knew and the
shell lacks is counted by key id (`unopenable`). The counts are of sets, cases checked,
removed and unchecked, assignments and labels. A set's header is only checked to open under its own set
id: it has no chained commitment until the set is frozen (LP-11i), so a header re-sealed by
someone holding the key passes. Without the keys, each case and each label is counted
`unchecked`, never a break. It reads only, marks nothing, and cannot be combined with `--since-checkpoint`.

## Rotating the evaluation-set key

`mendari evalset rotate` re-seals under the new key every sealed value of every set (headers,
case inputs and attributes, labels, aggregates, and, once a later step fills them, freeze
records), as
`mendari phi rotate` does for the PHI key ([Deploying with PHI](../deploying-with-phi.md),
section 3, Rotating the keys). It works in batches, each recorded in the chain
(`evalset.rotated`: key ids and counts, never a key). A re-seal changes the ciphertext only,
never the value, so it runs on sets in every state, frozen ones included. The chain's case hashes
and commitments stay as they are. In this order:

1. Make the new key (`python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`)
   and keep it in your secret manager beside the old one. It differs from every other key.
2. **Reader first.** Set `MENDARI_EVALSET_READ_KEYS=<new key>` for Production and redeploy.
   The server starts with the new key in its ring, and the database marks it known; nothing
   seals with it yet.
3. **Swap.** Set `MENDARI_EVALSET_KEY=<new key>` and `MENDARI_EVALSET_READ_KEYS=<old key>`, and
   redeploy. `/api/health` (signed in) shows the new key's id as `evalset_keys.current`. Delete
   the deployments built before the swap: once the rotation begins they refuse to seal with the
   old key.
4. **Re-seal.** From a machine that reaches the database, with `MENDARI_EVALSET_KEY`,
   `MENDARI_EVALSET_READ_KEYS` and `MENDARI_EVALSET_INDEX_KEY` as deployed in the shell (from a
   `0600` file, as for the PHI key):

   ```bash
   mendari evalset rotate --db "$DATABASE_URL" --to <evalset_keys.current>
   ```

   `--to` must be the key the shell seals with; a key an earlier rotation retired is refused.
   Each batch (`--batch`, 200 rows by default) is written only where a row still holds what was
   read, and retires the keys it moved values off: nothing seals with them here again. An
   interrupted run is run again the same way: what is done stays done. A row holding a value no
   key opens is left as it is and listed, by its set, its case hash and the key it names (exit
   1): add that key to `MENDARI_EVALSET_READ_KEYS`, or remove the case if it is a draft's, and
   run again.
5. **Check.** `mendari evalset rotate --db "$DATABASE_URL" --check` opens every sealed value with
   the new key alone, recomputes each case's and each label's commitment against the chain and
   each case's keyed hash, and checks its rows in the hidden-case index under the current index
   key. It reads only:
   nothing is written, marked or chained. `"ready": true` (exit 0) means nothing needs the old
   key. A failure names its kind, its set, a case by its hash or a label by its id, and the key
   it names or what did not match (`commitment`, `case_hash`, `index`), never a value. A key in `read_not_retired`
   had nothing to move, so it was not retired: make sure no deployment still seals with it
   before you drop it. Then run `mendari audit verify --evalsets`.
6. **Drop the old key.** Remove `MENDARI_EVALSET_READ_KEYS`, redeploy, delete the file from step
   4, and run the check again. Keep the old key in the secret manager until every backup, every
   export (below) and the database's point-in-time history from before step 4 have expired:
   they still hold values sealed under it.

The second rotation follows the same steps, and so does every one after it: the key in use is
never one a rotation retired.

**A new index key.** The index key has no read keys: a hash cannot be re-keyed without what it
hashes, so a new index key means rebuilding the index from the sealed inputs. Set the new
`MENDARI_EVALSET_INDEX_KEY` (different from every other key) in the deployment and the shell,
redeploy, then at once:

```bash
mendari evalset reindex --db "$DATABASE_URL"
mendari evalset rotate --db "$DATABASE_URL" --check
```

`reindex` opens every case's input with the ring and rebuilds the whole index under the current
index key in one transaction: a reader sees the old index or the new one, never a mix of key
ids. A case input no key opens refuses it all, and nothing changes. The chain records the new
key's epoch (`evalset_index.epoch`) and the rebuild (`evalset_index.rebuilt`). Until the reindex,
the check lists each case indexed under the old key (`index`). Case hashes already chained keep
the index key id they were made under: `mendari audit verify --evalsets` recomputes only those
under the current index key, and each case's commitment, which no index key keys, still binds it.

The reindex also retires every earlier index key: from then on a deployment or shell still
holding one refuses every write (a case it indexed would be one the hidden-case index never
finds), and a server with it does not start. Before you destroy the old index key, delete the
deployments built before the change and remove the old key from every shell and file that holds
it. A reindex never goes back: once the new key has indexed a case, a shell still holding the old
one refuses to reindex (it would retire the newer key), and names the newer key's id.

## The sealed export

```bash
mendari evalset export --db "$DATABASE_URL" --out ~/backups/evalsets-2026-10-09.jsonl
```

`export` copies every row of `evalset_sets`, `evalset_cases` and `evalset_labels` as stored, one
JSON line each, after a header line (`mendari.evalset_export/1`: when, how many rows of each
table, and how many values each key id sealed). Sealed values stay ciphertext; the rest is ids,
organizations, states, times and keyed case hashes. The hidden-case index is left out:
`reindex` rebuilds it; so are the assignments, which hold nothing sealed (the whole-database
backup keeps them).
It needs no key, opens nothing, chains nothing and marks nothing; it opens the database
read-only. It writes a new file only its owner may read (`0600`), never over an existing one,
and refuses a path inside Mendari's package or a checkout of its repository.

The export is a copy to keep, not a way back in: there is no import command. A database is
restored whole, as the [incident runbook](incident-runbook.md#backup-and-restore) describes.
Opening an export's values needs the keys it names, so keep those keys as long as the export.

## What leaves sets alone

PHI retention (`mendari phi purge` and the scheduled purge), `mendari phi erase` and
`mendari phi rotate` never touch the `evalset_` tables, assignments and labels included: they
work on kept inputs and notes only.
`mendari evalset rotate` never touches a kept input, a note or a PHI key's mark, and its keys
are never PHI keys.
There is no erasure path for a hidden case yet; one is needed before B20 brings de-identified
cases.
