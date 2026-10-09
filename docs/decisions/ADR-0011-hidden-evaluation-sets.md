# ADR-0011: Hidden evaluation sets, blind labelling and agreement

## Status
Proposed (2026-10-09). Built in steps (LP-11, `tasks/todo.md`, planned in
`notes/lp11-plan.md`); each step adds what it built here, in the order they land:
- 11a, one canonical form of a case's input and the coverage report over any JSONL file.

## Context
Every dataset Mendari has is a JSONL file tracked in git: 22 named in pack manifests and inside
their digests, and 5 local-site examples under `datasets/`. All 27 are public, so none can
measure a pack honestly once its author has seen it. LP-11 (vision audit, amending A8) asks for held-out sets that clinicians label blind,
with measured agreement, that no route, export or log can leak, and that a later step (LP-17)
grades packs against. Two things that look close are not it: `mendari agreement` compares two
machine variants, and the review queue shows the model's answer and takes one resolution per
decision.

## Decision
**Where sets live (the owner, 2026-10-07).** Set content, labels, assignments and the
hidden-case index live only in the hosted engine's own database (locally, a developer's SQLite
or Postgres with synthetic sets), in tables prefixed `evalset_`: never in this repository, a
pack directory, a shipped fixture or a pack digest. Hand-written cases only until B20.

**Sealed, under keys of their own.** Every case input, label and attribute is sealed
(AES-256-GCM, bound to its kind, set id and case id, and a label also to its label id) under
`MENDARI_EVALSET_KEY`, a ring with `MENDARI_EVALSET_READ_KEYS`, rotated as the PHI key is
(LP-09: its own rotate command and read-only check). `MENDARI_EVALSET_INDEX_KEY` keys only the
hidden-case index and the chained case-id hashes. Label values are chained as salted commitments,
as notes are, never under the index key, so that key alone recovers no label. Both keys differ
from each other and from every PHI key and the checkpoint key. The owner sets them as Secret for
the hosted Production environment only, so a Preview copy of the database holds only ciphertext.
Without both, the feature is off: no evaluation-set route is mounted and no command opens a set.

**One canonical form.** Two cases are the same case when their canonical inputs are equal:
exactly (the ledger's canonical JSON) or under `norm/v1` (string values folded: NFKC, case
folded, invisible characters removed, runs of characters other than letters, marks, digits and
`_` one space; whole numbers written as integers). The coverage report, the hidden-case index and
the pack store's upload check (LP-20) use the same functions (`evals/canonical.py`), pinned byte
for byte; a change to `norm/v1` means a new version and reindexing from the sealed inputs.

**Roles and permissions.** Labellers hold `label` through the reserved `labeller` role, in
memberships mode. A grading credential is a machine's only: an API key of role `grading`, minted
by the operator's command line, never by a person or an administrator, and an administrator
holds neither `label` nor `grade`. Set management is a people-only `curate`. A labeller's routes
return the case input only, never another label, a model output, the author's intended label or
the case's attributes, and labelling never reuses the review queue's screens.

**Lifecycle.** A set is `draft` (mutable), `frozen` (immutable: a content hash over each case's
id, canonical input and final labels, and the set's header, chained as a salted commitment at
freeze), `active` (frozen and in use for grading) or `retired`. Only frozen or active sets
grade. A set stops grading after a maximum number of grading runs or a maximum age, whichever
comes first, both fixed at freeze; the key holder then retires it. A signature on the commitment waits for LP-17's grading-key
decision.

**The chain.** Each label event chains a keyed hash of the case id and a salted commitment of
the label values, with the labeller (by user id) and the time; the clear values stay sealed.
Every opening of a case's input, attributes or labels by a labeller, or by sampling, measuring,
freezing or grading, is chained. The key holder's `mendari audit verify --evalsets` (which
checks sealed rows against their chained commitments) and rotation check open sealed rows and
print counts only, chaining nothing; the sealed export copies ciphertext only.

**Aggregates only.** No route returns frozen or active content to anyone: grading runs in the
server and answers aggregates only (no per-case field, no missed ids), with local providers only
until LP-17 decides on remote ones.

## Consequences
- The key holder can decrypt offline, so "only grading reads active sets" is a rule the software
  enforces, not cryptography.
- Agreement (percent agreement with its Clopper-Pearson interval, Cohen's kappa per rater pair,
  Krippendorff's alpha across the pool, with seeded percentile bootstrap intervals) is computed
  by the command line and at freeze and stored sealed; routes serve only the stored aggregates.
- The coverage report counts distinct positives; the evaluation report (`mendari.eval/2`) still
  counts every positive (AI Guard's test set: 164 `verdict=block`, 30 distinct). Which one the
  evidence rule should use is the owner's, for LP-17.
- Paraphrases of public text pass the equality check; review and the threat model cover them.

## Alternatives considered
- **Sets in a separate store or service.** Rejected by the owner: one database, one backup, one
  chain.
- **Labelling in the review queue.** It shows the model's answer, so labels would not be blind.
- **Signing the freeze commitment now.** Waits for LP-17, which decides the grading key.
- **A third-party agreement library.** The coefficients are small and pinned against
  hand-computed references; no dependency is added.
