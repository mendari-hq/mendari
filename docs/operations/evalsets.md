# Hidden evaluation sets

A **hidden evaluation set** is a set of hand-written cases that a pack's authors never see, kept
to measure the pack honestly (LP-11,
[ADR-0011](../decisions/ADR-0011-hidden-evaluation-sets.md)). Clinicians will label its cases
blind, and a grading run will answer aggregates only. This runbook covers what is built so far:
making a set, adding and removing its cases, listing sets and verifying them, all from the key
holder's command line. No route serves a set yet; labelling, freezing, grading, key rotation and
the sealed export come in later steps of LP-11.

## Where sets live

Sets live only in the deployment's own database (locally, a developer's SQLite or Postgres with
synthetic sets), in the tables `evalset_sets`, `evalset_cases` and `evalset_index`: never in
this repository, a pack directory, a shipped fixture or a pack digest. Each set belongs to one
organization and one pack.

Everything about a case is sealed with AES-256-GCM under `MENDARI_EVALSET_KEY`, each value bound
to its kind, its set and its case: its input, and its attributes (the author, language, stratum
and source, the id its author gave it, an intended label if any) with a 32-byte salt. A set's
header (the pack's id, the set's name, the pack's questions with their labels and its critical
labels) is sealed too. What stays in the clear is ids, states, times and keyed hashes. A case's
id is a UUID the store mints.

The chain records each change with ids, keyed hashes and commitments only, under the set's own
chain id (`evalset:<set id>`):

| Event | Payload |
|---|---|
| `evalset.created` | the set's id and organization |
| `evalset.case_added` | the set, the organization, the case's keyed hash (`hmac-sha256:<index key id>:<hex>`) and its commitment (`case_ref`: the SHA-256 of a fixed prefix, the case's salt and its set, id, input and attributes) |
| `evalset.case_removed` | the set, the organization and the case's keyed hash |
| `evalset_index.epoch` | the index key's id, the first time a case is indexed under it (a deployment event, under `keys`) |

An organization's administrators see their own sets' events in the administrative list and its
FHIR export, as ids and hashes: they can tell that cases were added or removed, never what they
hold. The command line prints ids, line numbers, case hashes and counts, never a case's text or
a set's name.

## The keys

The feature is off unless both `MENDARI_EVALSET_KEY` and `MENDARI_EVALSET_INDEX_KEY` are set
([Deploying with PHI](../deploying-with-phi.md), section 8). The owner makes them, keeps a copy off the laptop,
and sets them as Secret for the hosted Production environment only; the commands below run with
the same two values in the shell. The index key keys the hidden-case index and the chained case
hashes, never a label; the two differ from each other and from every PHI key.

The database remembers the evaluation-set keys a writer held, so a stray key is caught. Before
every write, and when a server with the keys starts, Mendari refuses:

- a current key that a rotation retired (it seals nothing more);
- any of the keys that the database knows as a PHI key or input hash key;
- a key in the other role: a key of the ring that the database knows as an index key, or an
  index key it knows as a key of the ring, earlier keys included;
- once a set exists, a ring holding none of the keys the database knows: whatever such a key
  sealed, no one reading with the others could open.

To change keys, add the new one to `MENDARI_EVALSET_READ_KEYS` first (it opens, never seals),
run once, then swap it in as `MENDARI_EVALSET_KEY` with the old one as the read key. Keep the old
key in the ring until a rotation has re-sealed everything under the new one (the rotation command
comes with LP-11f). A PHI key or input hash key the database knows as an evaluation-set key is
refused too, and never marked a PHI key: a server with one does not start, and the sets stay
writable with their own keys.

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

## Verifying

```bash
mendari audit verify --evalsets --db "$DATABASE_URL"
```

With both keys in the shell, a full verification also checks the sets and cases: each set's
header opens under its own set id; each case has its row (or a later removal), in its set and
organization, and each row its event; its keyed hash is recomputed when it is under the current
index key; its input and attributes open and give the commitment its event chained. A case row
altered, moved or forged stops the verification at its event (`broken_at`); a row without its
event is counted (`unchained`); a row sealed under a key this database knew and the shell lacks
is counted by key id (`unopenable`). A set's header is only checked to open under its own set
id: it has no chained commitment until the set is frozen (LP-11i), so a header re-sealed by
someone holding the key passes. Without the keys, each case is counted `unchecked`, never a
break. It reads only, marks nothing, and cannot be combined with `--since-checkpoint`.

## What leaves sets alone

PHI retention (`mendari phi purge` and the scheduled purge), `mendari phi erase` and
`mendari phi rotate` never touch the `evalset_` tables: they work on kept inputs and notes only.
There is no erasure path for a hidden case yet; one is needed before B20 brings de-identified
cases.
