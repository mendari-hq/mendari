# The pack store

Each organization can keep its own versions of packs on the server: written and improved by its
authors, checked, evaluated and, once someone other than the author signs off, served to that
organization alone (ADR-0016, Proposed). LP-20 builds it in steps; this page grows with them.

**Status:** the store exists as a library and as the operator's commands below (LP-20e). No
route reaches it yet, nothing turns it on, and no request is answered by a store version. Its
tables are created empty in every database.

## What a version is

- **Kept in the engine's database**, never on disk: a serverless instance has only a scratch
  disk. Each file is kept once by its SHA-256 (`store_files`), whichever versions and
  organizations hold it, and each version is a row (`store_versions`) that lists its files.
- **Keyed by its digest**: the store writes the files to a scratch directory, loads them and
  keys the version by the digest that load hashed, never one it is told. Two versions with the
  same digest are the same pack.
- **Never changed.** Its files, id, version, parent and source are written once, and the hash of
  that row is in its `pack_version.created` event. Only its state moves (draft, held, withdrawn;
  promotion comes later), each move one chained event that names the organization, the digest
  and who acted. `mendari audit verify` fails on a row changed past the store, a state no event
  moved it to, or a version with no event. An edit is a new version whose parent is the old one.
- **Without anything uploaded about it.** `evals/`, a `DECISION_CARD.md` and hidden entries are
  outside a pack's digest, so the store drops them: no uploaded benchmark, leaderboard or card is
  ever kept. The card is rendered by the server from the pack's own files, and says that it shows
  no figure it did not compute itself.
- **Validated** by the same function as `mendari validate` (each problem a kind, a file and a
  place), and the result is kept with the version.

## The engine it runs on

A store version is a pack the deployment does not own (origin `store`, "From this organization's
store"): it runs on the bounded engine (RE2, the caps, the input limits, one second of CPU per
decision; ADR-0009), its model calls are capped as a pulled pack's are, and its id is never a
reference pack's at a tying version. Beyond what a pulled pack is held to, the deployment chooses
its Jev price (so the recorded cost is the deployment's), whether its model questions share a
call, and an LLM's temperature: the pack's own values are dropped. Only a provider setting that
names it chooses its variants: unscoped `MENDARI_PROVIDERS` settings and the Jev defaults do not
reach it, as for a community pack.

## Limits

| Setting | Default | What it bounds |
|---|---|---|
| `MENDARI_STORE_ORG_BYTES` | 256 MiB | The bytes of files one organization's store keeps, each file counted once |
| `MENDARI_ORG_DAILY_LIMITS` `store_uploads=N` | no limit | The versions an organization adds a day |

A version has at most 2,000 files of at most 50 MB each, an archive's own limits. A withdrawn
version's files are freed 30 days after it was withdrawn (`mendari store purge`); its row and
events stay.

## The operator's commands

| Task | Command |
|---|---|
| List an organization's versions, newest first | `mendari store versions --db … --org ORG [--pack ID]` |
| Check every kept file and version against its hash and digest | `mendari store verify --db …` |
| Withdraw a draft or held version, with a reason on the chain | `mendari store withdraw DIGEST --db … --org ORG --reason …` |
| Free the files of versions withdrawn more than 30 days ago | `mendari store purge --db …` |

Every command takes `--db` (a SQLite path or a `postgresql://` URL) or reads `DATABASE_URL`; the
reads open the database read-only.

## Prerequisites

`mendari.core.pack_store.prerequisites` runs what the store rests on and names what is missing:
memberships mode (LP-12), a store pack on the bounded engine whose rules stop at the budget
(LP-16), the exact statistics and the ledger's canonical JSON answering pinned values (LP-10),
and the canonical forms of a case answering their pinned vectors (LP-11). The routes (LP-20f)
are not mounted unless it finds nothing missing.

## Scratch disk

A version is written to the process's temporary directory the first time it is loaded there,
checked against its digest, and kept in a cache of at most 32 versions and 256 MiB; past that
the least recently used is removed. Hosted functions have a writable temporary directory; how
much of it a deployment may use is the hosting platform's limit.
