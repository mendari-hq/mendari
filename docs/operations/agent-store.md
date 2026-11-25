# The agent store

Each organization can keep its own versions of agents on the server: written and improved by its
authors, checked, evaluated and, once someone other than the author signs off, served to that
organization alone (ADR-0016, Proposed). LP-20 builds it in steps; this page grows with them.

**Status:** off unless `MENDARI_AGENT_STORE=on`, and then only in memberships mode: with the
switch on in claims or open mode, or with a prerequisite below missing, the server stops at
start-up and says why. Its authors' routes are mounted (LP-20f), and a promoted version answers
its own organization's requests (LP-20h). Its tables are created empty in every database.

## Who does what

- **Authors** hold the reserved role `pack_author` (`author`, people only; the role keeps the
  name it had before, which memberships record). An administrator
  gives it, themself included: `admin` does not hold it. Authors upload signed archives (the
  `.mendari` file `mendari archive build` writes; one named `.ohpack`, as it was before
  2026-11, is the same file), copy an agent this deployment serves, edit text files into new drafts, run a draft's tests and
  withdraw a draft (`docs/guides/api.md`, "An organization's agent store").
- **Administrators** (`keys`) register and revoke the authors' Ed25519 public keys: an upload
  must be signed by one of the organization's keys that is not revoked.
- **The operator** checks, withdraws and frees versions with the commands below.

## The checks of a new version

Before any row is written, in this order: the archive's own (size, signature, names,
expansion), the PHI detector over every text file (a refusal answers `422` with kinds, files,
lines and counts, never a value), the load on the bounded engine, the namespace rule (the id is
`<organization>/<name>`, or a namespace the organization holds by an approved registry claim;
`mendari` and `openhealth` never), then the validation and the dataset checks of
each declared dataset (kept with the version).

## What a version is

- **Kept in the engine's database**, never on disk: a serverless instance has only a scratch
  disk. Each file is kept once by its SHA-256 (`store_files`), whichever versions and
  organizations hold it, and each version is a row (`store_versions`) that lists its files.
- **Keyed by its digest**: the store writes the files to a scratch directory, loads them and
  keys the version by the digest that load hashed, never one it is told. Two versions with the
  same digest are the same agent.
- **Never changed.** Its files, id, version, parent and source are written once, and the hash of
  that row is in its `store_version.created` event. Only its state moves (draft, withdrawn;
  promotion comes later), each move one chained event that names the organization, the digest
  and who acted. `mendari audit verify` fails on a row changed past the store, a state no event
  moved it to, or a version with no event. An edit is a new version whose parent is the old one.
- **Without anything uploaded about it.** `evals/`, a `DECISION_CARD.md` and hidden entries are
  outside an agent's digest, so the store drops them: no uploaded benchmark, leaderboard or card is
  ever kept. The card is rendered by the server from the agent's own files, and says that it shows
  no figure it did not compute itself.
- **Validated** by the same function as `mendari validate` (each problem a kind, a file and a
  place), and the result is kept with the version.

## The engine it runs on

A store version is an agent the deployment does not own (origin `store`, "From this organization's
store"): it runs on the bounded engine (RE2, the caps, the input limits, one second of CPU per
decision; ADR-0009), its model calls are capped as a pulled agent's are, and its id is never a
bundled agent's at a tying version. Beyond what a pulled agent is held to, the deployment chooses
whether its model questions share a call, and an LLM's temperature: the agent's own values are
dropped. Only a provider setting that names it chooses its variants: unscoped
`MENDARI_PROVIDERS` settings do not reach it.

## Evaluating a draft (LP-20g)

There is no worker: a **job** is a row that runs when an author asks, a slice at a time.

- **Datasets.** An author uploads a labelled dataset (JSON Lines) checked against a version:
  the PHI detector and the dataset checks (kept and shown; a schema or label problem refuses
  it), as a version is. It is kept sealed under the deployment's PHI policy
  (`encrypt`), and `mendari phi rotate` re-seals it with the inputs and notes. A job may also run
  on a dataset the version declares, by name.
- **A job** evaluates one store version, or an agent served here (the current one a draft was
  copied from), on one dataset, with the version's own variants unless others are named. Each
  `POST /api/store/jobs/{id}/advance` runs as many cases as fit about 20 seconds, claimed in
  one transaction: two calls at once never decide a case twice, and a slice cut short is run
  again by the next call. The last slice writes one evaluation report with its ledger
  (`eval.recorded`), the same bytes a single run would write. A remote variant needs the
  organization's evaluation quota (`eval_llm`) set and runs 200 cases at most; a job
  runs 5,000 at most, and `store_jobs` bounds the jobs an organization makes a day.
- **Draft against current.** `GET /api/store/comparisons?draft=…&current=…` pairs two finished
  jobs on the same dataset: each side's figures as k of n with exact intervals, each difference
  with the exact McNemar test (Holm-adjusted over the family listed), the Brier difference with
  a paired bootstrap, and the cases that changed by the dataset's own ids. A figure on too few
  cases says "insufficient evidence". The statistics are LP-10's (`evals/paired.py`).

## Promotion and serving (LP-20h)

A person who holds `promote` (`governance` and `admin`, people only) signs a version off to be
served to their organization, naming the evaluation reports of that version they relied on, a
reference (where the sign-off is recorded) and a note. It is refused when they made the version
or any version it was edited from, when a report is not their organization's evaluation of that
digest, when the version is withdrawn or does not validate, and when the deployment serves
that agent id from disk. Promoting retires the version promoted before; retiring a promotion stops
serving it (the store agent's recall). Every decision a promoted version makes records the
promotion's id.

Only the promoting organization is served the version: its decide routes, `/api/agents`, CDS
Hooks and the FHIR operation. Promotion is not the automation approval: at risk levels 2 and 3 a
promoted version automates only once its digest is approved too, and holds stop it as any agent.
A promoted version is built on its first use in a process, not at start-up; one that no longer
loads (after an engine upgrade, say) is left out of its organization's agents and logged, and
every other agent is served as before. A promotion to recommend only (`automate: false`, the
default) never automates, as under a hold. The author of a version cannot approve it for
automation either. Retiring a promotion stops serving it at once in the process that retired
it, and within 5 seconds in every other instance. Agents read from disk
are served as before; promotion is the rule for store versions only (owner decision 10).

## Learning from reviewers (LP-20i)

An author who may also read inputs (a reviewer or an administrator) builds an **improvement
dataset** for a draft: one record per decision of the agent (and of the agent it was copied
from) that a reviewer resolved in this organization, its input and final label, each label with
where it came from (the decision, the reviewer by id, when, whether they overrode the system,
the version that decided). Each input read is chained. Decisions kept as a hash only are
counted as skipped. The dataset holds patients' inputs: it is sealed under the PHI policy,
stays in the organization's store, can be a job's dataset, and is never downloadable (no route,
export, registry or site report carries it).

It is offered to the draft as **proposed behaviour tests**: each case run against the draft,
shown by its case id with the label it expects and whether the draft decides it, never its
input. An author accepts or dismisses each; accepted ones run with the draft's own tests and
are shown apart. They are never written into the agent's files, so the draft's digest does not
change and no agent carries a patient's input.

## Limits

| Setting | Default | What it bounds |
|---|---|---|
| `MENDARI_STORE_ORG_BYTES` | 256 MiB | The bytes of files and datasets one organization's store keeps, each file counted once, each dataset as kept (sealed, under `encrypt`) |
| `MENDARI_ORG_DAILY_LIMITS` `store_uploads=N` | no limit | The versions an organization adds a day |
| `MENDARI_ORG_DAILY_LIMITS` `store_datasets=N` | no limit | The datasets an organization adds a day, uploaded or built from its reviewed decisions |
| `MENDARI_ORG_DAILY_LIMITS` `store_jobs=N` | no limit | The evaluation jobs an organization makes a day |
| `MENDARI_STORE_MAX_ARCHIVE_BYTES` | 32 MiB | An archive, sent in parts each under `MENDARI_MAX_BODY_BYTES` |

Past a day's quota a version or a dataset is refused with `429` (the quota is checked before the
archive or the dataset is loaded and checked, and again as it is written); past the bytes, with
`413`. A version has at most 2,000 files of at most 50 MB each, an archive's own limits. A withdrawn
version's files are freed 30 days after it was withdrawn (`mendari store purge`); its row and
events stay.

## The operator's commands

| Task | Command |
|---|---|
| List an organization's versions, newest first | `mendari store versions --db … --org ORG [--agent ID]` |
| Check every kept file and version against its hash and digest | `mendari store verify --db …` |
| Withdraw a draft, with a reason on the chain | `mendari store withdraw DIGEST --db … --org ORG --reason …` |
| Free the files of versions withdrawn more than 30 days ago | `mendari store purge --db …` |

Every command takes `--db` (a SQLite path or a `postgresql://` URL) or reads `DATABASE_URL`; the
reads open the database read-only.

## Prerequisites

`mendari.core.agent_store.prerequisites` runs what the store rests on and names what is missing:
memberships mode (LP-12), a store agent on the bounded engine whose rules stop at the budget
(LP-16), the exact statistics and the ledger's canonical JSON answering pinned values (LP-10),
and the canonical forms of a case answering their pinned vectors (LP-11). With the switch on,
the server does not start unless it finds nothing missing.

## Scratch disk

A version is written to the process's temporary directory the first time it is loaded there,
checked against its digest, and kept in a cache of at most 32 versions and 256 MiB; past that
the least recently used is removed. Hosted functions have a writable temporary directory; how
much of it a deployment may use is the hosting platform's limit.
