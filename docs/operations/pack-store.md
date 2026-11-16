# The pack store

Each organization can keep its own versions of packs on the server: written and improved by its
authors, checked, evaluated and, once someone other than the author signs off, served to that
organization alone (ADR-0016, Proposed). LP-20 builds it in steps; this page grows with them.

**Status:** off unless `MENDARI_PACK_STORE=on`, and then only in memberships mode: with the
switch on in claims or open mode, or with a prerequisite below missing, the server stops at
start-up and says why. Its authors' routes are mounted (LP-20f); no request is answered by a
store version yet (promotion is LP-20h). Its tables are created empty in every database.

## Who does what

- **Authors** hold the reserved role `pack_author` (`author`, people only). An administrator
  gives it, themself included: `admin` does not hold it. Authors upload signed archives, copy a
  pack this deployment serves, edit text files into new drafts, run a draft's tests and
  withdraw a draft (`docs/guides/api.md`, "An organization's pack store").
- **Administrators** (`keys`) register and revoke the authors' Ed25519 public keys: an upload
  must be signed by one of the organization's keys that is not revoked.
- **The operator** reads and answers held versions with the commands below.

## The checks of a new version

Before any row is written, in this order: the archive's own (size, signature, names,
expansion), the PHI detector over every text file (a refusal answers `422` with kinds, files,
lines and counts, never a value), the load on the bounded engine, the namespace rule (the id is
`<organization>/<name>`, or a namespace the organization holds by an approved registry claim;
`mendari`, `openhealth` and `community` never), then the validation and the dataset checks of
each declared dataset (kept with the version), then the hidden-set check.

**The hidden-set check.** With the hidden evaluation sets' keys set, every case the version
carries (each JSON Lines record's input and each behaviour test's) is looked up in the keyed
hidden-case index, exactly and under `norm/v1`. On any match the version is stored `held`: it
is not tested, edited from or served, and its uploader is told only that it is held for the
operator's review. The match (set and case ids) is kept for the operator only. After three
held versions in 30 days an organization's uploads stop until the operator answers them.
Without the keys the check is "not applicable": the deployment holds no hidden case.
Once keys are set, `mendari store recheck` runs it over every draft kept before.

A signed-in administrator's `/api/health` shows `store.held`, how many of their organization's
versions wait for the operator.

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
| `MENDARI_STORE_MAX_ARCHIVE_BYTES` | 32 MiB | An archive, sent in parts each under `MENDARI_MAX_BODY_BYTES` |

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
| Read the held versions, with the hidden sets and cases each matched | `mendari store held --db …` |
| Release a held version (a draft again), or withdraw it | `mendari store release` or `dismiss DIGEST --db … --org ORG --reason …` |
| Run the hidden-set check again over every draft | `mendari store recheck --db …` (needs the evaluation-set keys) |

Every command takes `--db` (a SQLite path or a `postgresql://` URL) or reads `DATABASE_URL`; the
reads open the database read-only.

## Prerequisites

`mendari.core.pack_store.prerequisites` runs what the store rests on and names what is missing:
memberships mode (LP-12), a store pack on the bounded engine whose rules stop at the budget
(LP-16), the exact statistics and the ledger's canonical JSON answering pinned values (LP-10),
and the canonical forms of a case answering their pinned vectors (LP-11). With the switch on,
the server does not start unless it finds nothing missing.

## Scratch disk

A version is written to the process's temporary directory the first time it is loaded there,
checked against its digest, and kept in a cache of at most 32 versions and 256 MiB; past that
the least recently used is removed. Hosted functions have a writable temporary directory; how
much of it a deployment may use is the hosting platform's limit.
