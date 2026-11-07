# ADR-0011: Hidden evaluation sets, blind labelling and agreement

## Status
Proposed (2026-10-09). Built (LP-11, 11a to 11p, 2026-10-09 to 2026-10-18) by the defaults of
the plan's conflicts 1 to 57 (`notes/lp11-plan.md`), as `tasks/todo.md` records them, each
confirmed by the owner on 2026-11-07 but what decisions 2 and 3 leave open; decisions 4 to 6
were confirmed then too. Accepted once the four owner decisions still open there (1, 2, 3 and 7:
how labellers reach the hosted site among them) are decided. Gate G2-3's prerequisite
`labelling-underway` and gate G3-4's `hidden-sets-frozen` (`launch/gates.yaml`) name the tests
that hold what was built; each still waits for the owner's attestation. What each step built,
in the order they landed:
- 11a, one canonical form of a case's input and the coverage report over any JSONL file;
- 11b, rater agreement (percent agreement, Cohen's kappa per pair, Krippendorff's alpha, with
  seeded bootstrap intervals), pure;
- 11c, the evaluation-set keys and sealing (`core/evalset_keys.py`), nothing stored yet;
- 11d, the permissions: `label` (the `labeller` role, memberships mode), `curate` (people;
  administrators), and `grade`, held only by a grading key the operator mints; no route needs
  them yet.
- 11e, the store (`core/evalsets.py`, the operator's `mendari evalset create|add|remove-case|list`):
  sets and hand-written cases sealed in the `evalset_` tables, each case chained as its keyed hash
  and a salted commitment of its input and attributes; the hidden-case index; the keys checked
  against the database's marks before every write; `mendari audit verify --evalsets`. No route
  reads the tables yet.
- 11f, the keys' lifecycle (`mendari evalset rotate [--check]`, `reindex`, `export`): rotation
  copied from the PHI key's, in chained batches and in every state of a set, with a read-only
  check; the hidden-case index rebuilt under a new index key in one transaction, the index keys
  it replaced refused from then on; the sealed export (the rows as stored: ciphertext, ids,
  states, times and case hashes; no key needed); every sealed column listed once
  (`SEALED_COLUMNS`) and pinned by a test.
- 11g, the generated no-leak sweep (`tests/test_evalset_leaks.py`), tests only: every mounted
  route and method as every kind of principal, the logs, every `mendari evalset` command, the
  audit and feedback exports, the sealed export and every table hold no planted mark but where
  the plan's conflicts 38, 55 and 56 allow it, and each event's payload keys are pinned; every
  route inventory runs with the feature on; threat model I13.
- 11h, blind labelling in the store and on the key holder's command line (`mendari evalset
  sample|assign|adjudicate|progress|agreement|coverage`): a seeded draw within strata, each
  drawn case weighted; two labellers a case, balanced, and a third for a disputed case, never an
  administrator of the set's organization; a labeller is shown the input and the questions only,
  each read chained before it is returned; one sealed label per assignment, bound to its id and
  chained as a salted commitment, settling the case agreed, disputed, resolved or unresolved;
  agreement and coverage measured and sealed as the set's aggregates; `audit verify --evalsets`
  recomputes each label. No route yet, so no person labels.
- 11i, the lifecycle in the store and on the key holder's command line (`mendari evalset
  freeze|retire|show`): every write refuses a set that is not a draft, in its own exclusive
  transaction; freezing (every drawn case agreed or resolved, an undrawn one removed with
  `--drop-unsampled`, both limits of the rotation rule required) hashes the content
  (`mendari.evalset/1`: the header, the rule and its freeze time, and every case by id with its
  input, final and derived labels, status and attributes), seals the salt, the content hash and the
  derived labels as the freeze record and the final labels' coverage and agreement as the
  aggregates, and chains only the salted commitment (`evalset.frozen`); the in-process grading
  read (`grade_read`, no route or command calls it until 11l) serves a live grading key of the
  set's organization while the rule holds, recomputes the content, activates a frozen set and
  chains each read in the same exclusive transaction, before its run receives the cases;
  retiring checks the content once more and publishes nothing; `audit verify --evalsets` and
  `rotate --check` recompute every set the chain froze against its commitment. Threat model T6.
- 11j, a labeller's routes (`GET /api/labelling`, `GET /api/labelling/next`,
  `POST /api/labelling/{assignment_id}`), mounted only in memberships mode with both keys (the
  hosted site, in claims mode without the keys, mounts none; owner decision 1 stays open): only
  a person holding `label` passes the guard (never a key, a token, an administrator or open
  mode), and whoever also manages the organization's sets is served nothing; the next case is
  its input and the set's questions only, its read chained before it is sent; a label answers
  `{remaining}`; an assignment that is unknown, another's or closed is one `404`, and a `422`
  names the question, never the value; closed answer models; a `label` rate limit per labeller;
  not gated by the notice; the security log names set ids only. The 11g sweep labels through
  them and allows a case's input only in a labeller's own next case.
- 11m, the labelling screen (`#labelling`, `dashboard/labelling.js`, over 11j's routes only):
  shown to a person holding `label` where those routes are served; someone who only labels sees
  it alone (`labelOnly`: no pack, no notice, no other section); their counts set by set, then
  one case at a time, its input escaped by `record.js` (the generic renderer, taken out of
  `pack.js`) and the set's questions as radio groups with nothing chosen. Blind: nothing of the
  case but its input, nor anyone's label, reaches the page, and nothing is kept in the
  browser's storage.
- 11k, an organization's curators' routes (`GET /api/evalsets`, `GET /api/evalsets/{set_id}`,
  `POST /api/evalsets/{set_id}/sample`, `/assignments` and `/adjudication`), mounted as 11j's are
  (memberships mode with both keys only): only a person holding `curate` (an administrator of the
  organization they act for: never a key, a token, a labeller or open mode) passes the guard,
  under one rule before the `GET` catch-all; each acts for that organization, so another
  organization's set, an unknown one and a malformed id are one `404` naming no id, decided in the
  store's own transaction (`org` given to every store call); answers are closed models of ids,
  counts, user ids, the commitment and the stored aggregates with when they were measured (each
  rater pair's kappa without the pair's label counts, which would show what each answered where a
  pair shares few cases), never an input, a label given, an attribute or a case id; draws and
  assignments are chained as the commands chain them and logged with set ids only; a curator is
  never assigned, nor any administrator; no route computes statistics, freezes or retires
  (conflicts 43 and 49); not gated by the notice, as they take no data in and mint no credential,
  and, like an administrator's other writes, no rate-limit class. Conflict 57 amended: a set's
  answer to its organization's curators may name label words (its questions and aggregates), and
  the 11g sweep allows them there only, its curators drawing, assigning and adjudicating through
  the routes.
- 11l, the grading route (`POST /api/evalsets/{set_id}/runs`), mounted with both keys in either
  account mode: only an organization's grading key passes the guard (`grade`, under a rule of its
  own before the curators', and not theirs in claims mode), for its own organization's sets
  (another's, an unknown and a malformed id are one `404`); before any case is read, the set's
  header is checked against the pack asked for (its id; a later version asking the same
  questions grades the set too) and every provider slot must run a local variant (conflict 52,
  owner decision 4, confirmed 2026-11-07); the store's grading read then chains the run and
  hands the cases in memory to a Decision with no store, whose `mendari.eval/2` result,
  computed from its in-memory ledger, is projected onto the closed `mendari.hidden_result/1`
  (`evals/hidden.py`, built from a site report's parts: aggregates only, calibration bins and
  Brier intervals over 10 cases or
  more, never a case, its id or hash, a missed or misclassified case, a subgroup or the dataset's
  identity); nothing is stored (no decision, report, ledger or usage); every refusal and failure
  is answered with a fixed reason (`GradingRefused`), a changed set with a `409` `set_changed`
  and an operator's log line naming the set only; the `evaluate` rate-limit class; the security
  log's `evalset.graded` names set ids only. Conflict 57 amended: a run's answer to its
  organization's grading key may name label words, as aggregates, and the 11g sweep grades each
  organization's frozen set through the route in both modes, its answer holding no mark
  (conflict 35).
- 11n, the management screen (`#evalsets`, `dashboard/evalsets.js`, over 11k's routes and the
  members list only): shown, with its link, only to a person who signed in holding `curate` in
  memberships mode where the deployment has the keys (never a labeller, another role, claims
  mode or a deployment without them, whose pages ask for no set); the organization's sets, then
  one set's facts and tabs (the Access view's pattern, copied): its progress by status and by
  labeller (named from the members list), the draw, the first and third assignments (only
  labellers holding no curating role are offered), and the stored agreement and coverage
  (percent agreement as k of n with its exact interval, kappa and alpha as numbers, "not
  estimable" in words, the insufficient-evidence mark); a set that is no longer a draft shows no
  form. Ids, counts, names and stored aggregates only; the wording says "in use" and "evaluation
  runs".
- 11o, the close-out, tests and docs only: gate G2-3's `labelling-underway` probed by the 11g,
  11h, 11j, 11k, 11m and 11n tests and gate G3-4's `hidden-sets-frozen` by the 11a, 11g, 11i,
  11k, 11l and 11n tests, each with the end-to-end test, both attestations left to the owner;
  the threat model's residual risks for hidden sets; the runbook's owner's steps (the keys, a
  grading key, the rotation rule's numbers and the two attestations) and a set from start to
  end; and one end-to-end test (`tests/test_evalset_end_to_end.py`, SQLite and Postgres: a set
  made and filled on the command line, drawn, assigned and adjudicated through the curators'
  routes, labelled blind through the labellers', followed and measured on the command line,
  frozen, graded through the grading route with a grading key and a body as the runbook writes
  them, shown and retired, then `mendari audit verify --evalsets` and `mendari evalset rotate
  --check` pass and the set's chain holds its events in the lifecycle's order; no answer, log
  or command output holds a case's content but a labeller's own next case its input).
- 11p, what 11o's review found no commitment covered: each draw chains a salted commitment to
  every drawn case's weight by its keyed hash (`weight_refs` on `evalset.sampled`, new at each
  draw, so the chain does not show whose weight changed), and each store of a set's aggregates a
  commitment to them (`aggregates_ref` on `evalset.measured` and `evalset.frozen`, the salt
  sealed with them); a case's status is checked against its labels and the set's latest draw,
  each label in the round its `evalset.labelled` chained (never an assignment row's, and every
  label the chain recorded must be there), and an adjudicator's label settles only a case whose
  two first-pass labels differ. A draw takes what the chain drew before, never a status;
  `freeze` and `measure` refuse a set whose labels, statuses or weights do not hold, naming no
  case (read again under the chain's lock first, so a write landing as they read is "run it
  again"); `assign` and `adjudicate`, which open nothing, refuse a status the chain's draw and
  labels do not allow; `show` and the curators' read refuse aggregates their commitment does
  not cover, naming the set; `mendari audit verify --evalsets` and `rotate --check` check all
  of it, and count the rows from before these commitments (`predating`), read as before.

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
id, canonical input, final labels, status and attributes, and the set's header with its rotation
rule, chained as a salted commitment at freeze), `active` (frozen and in use for grading, from its
first grading read) or `retired`. Only frozen or active sets grade. A set stops grading after a
maximum number of grading runs or a maximum age, whichever comes first, both fixed at freeze
with no default; the key holder then retires it, which publishes nothing. The content hash never
leaves the sealed freeze record. A signature on the commitment waited for LP-17's grading-key
decision. LP-17 note (ADR-0013, the grading method's section 11): the freeze commitment stays
unsigned. The grading key signs results only, never a commitment, and a signed hidden-set result
(`mendari.hidden_scoreboard/1`, LP-17k) carries the set's commitment instead, so the signature
binds the commitment to the figures graded on it.

**The chain.** Each label event chains a keyed hash of the case id and a salted commitment of
the label values, with the labeller (by user id) and the time; the clear values stay sealed.
Every opening of a case's input, attributes or labels by a labeller, or by sampling, measuring,
freezing or grading, is chained. The key holder's `mendari audit verify --evalsets` (which
checks sealed rows against their chained commitments) and rotation check open sealed rows and
print ids, case hashes, key ids and counts only, never a value, chaining nothing; the sealed
export copies the rows as stored, sealed values as ciphertext beside their ids, organizations,
states, times and keyed case hashes, and needs no key.

**Aggregates only.** No route returns frozen or active content to anyone: grading runs in the
server and answers aggregates only (no per-case field, no missed ids), with local providers only
until LP-17 decides on remote ones. LP-17 note: under the grading method 1.0 a hidden set is
graded with local providers only (LP-17's owner decision 2, confirmed 2026-11-07; ADR-0013,
decision 5). The same route grades the hidden-set scoreboard (LP-17k): a run that declares its
arms decides every declared arm on the same cases in one grading read (one chained
`evalset.graded`, one of the set's runs), refuses a declared arm that calls a remote provider
before any case is read, and
answers `mendari.hidden_scoreboard/1`, aggregates only: an arm that is not declared reads "not
graded here" when its configurations are remote, or "not applicable".

## Consequences
- The key holder can decrypt offline, so "only grading reads active sets" is a rule the software
  enforces, not cryptography.
- Agreement (percent agreement with its Clopper-Pearson interval, Cohen's kappa per rater pair,
  Krippendorff's alpha across the pool, with seeded percentile bootstrap intervals) is computed
  by the command line and at freeze and stored sealed; routes serve only the stored aggregates.
- The coverage report counts distinct positives; the evaluation report (`mendari.eval/2`) still
  counts every positive (AI Guard's test set: 164 `verdict=block`, 30 distinct). The grading
  method's evidence rule uses distinct positives (LP-17 decision 3, confirmed by the owner on
  2026-11-07), and `mendari.eval/2` does not change.
- An organization's administrators can link one case's events by its keyed hash, in the
  administrative list and its FHIR export, and see who read and labelled it (by user id) and
  that it went to adjudication, which shows that its first two labellers disagreed; never what
  it holds or how anyone labelled it.
- The chain keeps each labeller's user id for as long as the chain is kept, a labeller who
  leaves included.
- The database refuses no write itself: its DDL holds no trigger (Postgres splits it on `;`),
  so the refusals are the store's. An edit made by SQL to what a chained commitment covers (a
  case's input and own attributes, a label, each drawn case's weight, which every draw commits to
  again, a set's stored aggregates, a frozen set's content) is caught when the set is next read
  or verified, not prevented. A case's status has no commitment of its own: verification and
  `rotate --check` check it against its labels (each in its chained round) and the set's latest
  draw, freezing and measuring refuse a set whose labels, statuses or weights do not hold, and
  assigning and adjudicating a status the chain does not allow, so a disputed case edited to
  `agreed`, or a label deleted and its round moved, is found and never frozen with one
  labeller's answer (LP-11p). Assigning opens no label, so an agreed case edited to `disputed`
  still reaches an adjudicator, whose label then settles nothing, and the edit stays found. What
  no commitment covers is not caught at all: until a set is frozen, its header; an open
  assignment ended by SQL, which looks like a withdrawal; and the rows of a draw or a store from
  before LP-11p, read as before and counted (`predating`), whose weights, whether an unlabelled
  case was drawn, and aggregates are taken as they are.
- The freeze commitment is a salted hash, not a signature. LP-17 keeps it so: a signed hidden-set
  result carries the commitment, and the grading key signs nothing else (ADR-0013, decision 7).
- Paraphrases of public text pass the equality check; the hand-written rule (`template` null)
  and review stand against them. The threat model's residual risks name this risk and the
  others above.

## Alternatives considered
- **Sets in a separate store or service.** Rejected by the owner: one database, one backup, one
  chain.
- **Labelling in the review queue.** It shows the model's answer, so labels would not be blind.
- **Signing the freeze commitment now.** Waited for LP-17, which decided against it (ADR-0013,
  decision 7): a signed hidden-set result carries the commitment instead.
- **A third-party agreement library.** The coefficients are small and pinned against
  hand-computed references; no dependency is added.
