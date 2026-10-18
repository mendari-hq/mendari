# Hidden evaluation sets

A **hidden evaluation set** is a set of hand-written cases that a pack's authors never see, kept
to measure the pack honestly (LP-11,
[ADR-0011](../decisions/ADR-0011-hidden-evaluation-sets.md)). Clinicians label its cases blind,
and a grading run answers aggregates only. This runbook covers what is built so far: making
a set, adding and removing its cases, drawing them for labelling, assigning them to labellers,
following the labelling, adjudicating disputes and measuring agreement and coverage, freezing a
set under its rotation rule, reading it, retiring it, listing sets and verifying them, rotating
the keys, rebuilding the hidden-case index and the sealed export, all from the key holder's
command line; how a labeller labels, through their own routes (LP-11j); and how an
organization's administrators list and read its sets and draw, assign and adjudicate their
cases through theirs (LP-11k); how the organization's grading key grades a pack against a
frozen set through its route (LP-11l, [Grading a hidden set](#grading-a-hidden-set)); and the
dashboard's Labelling and Hidden sets screens over the labellers' and curators' routes
(LP-11m, LP-11n).

## Where sets live

Sets live only in the deployment's own database (locally, a developer's SQLite or Postgres with
synthetic sets), in the tables `evalset_sets`, `evalset_cases`, `evalset_index`,
`evalset_assignments` and `evalset_labels`: never in this repository, a pack directory, a
shipped fixture or a pack digest. Each set belongs to one organization and one pack.

Everything about a case is sealed with AES-256-GCM under `MENDARI_EVALSET_KEY`, each value bound
to its kind, its set and its case: its input, and its attributes (the author, language, stratum
and source, the id its author gave it, an intended label if any) with a 32-byte salt. A set's
header (the pack's id, the set's name, the pack's questions with their labels and its critical
labels) is sealed too, and so is each label, bound to its own id as well, the set's measured
agreement and coverage, and, once it is frozen, its freeze record (the salt and the content hash
behind its commitment). What stays in the clear is ids, states, a case's status, times, keyed
hashes, a frozen set's commitment and rotation rule, and who labels which case in which round. A
case's id is a UUID the store mints.

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
| `evalset.frozen` | the set, the organization, its commitment (the SHA-256 of a fixed prefix, the freeze salt and the content hash), how many cases it holds, and its rotation rule (`max_runs`, `max_age_days`) |
| `evalset.activated` | the set and the organization: its first grading read made it active |
| `evalset.graded` | the set, the organization, its commitment, the grading key (`key:<id>`), the digest of the pack graded, how many cases, and the run's number: each grading read, before its run receives the cases |
| `evalset.retired` | the set, the organization and its commitment |

An organization's administrators see their own sets' events in the administrative list and its
FHIR export, as ids and hashes: they can tell that cases were added, removed, read and labelled,
by whom (a user id), and which went to adjudication, when a set was frozen, graded (by which
grading key, for which pack digest) and retired, never what they hold or how anyone labelled
them. The command line prints ids, line numbers, case hashes and counts, never a case's text or
a set's name; only `evalset agreement`, `evalset coverage`, `evalset freeze` and `evalset show`
name labels (counts and coefficients by label) and `evalset coverage` and `evalset show` the
strata and languages, as aggregates that name no case.

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
command line, or the set organization's administrators do through their routes ([Curating from
the dashboard API](#curating-from-the-dashboard-api)); the labellers label through their own
routes ([How a labeller labels](#how-a-labeller-labels)); the dashboard's Labelling and Hidden
sets screens are over those routes. Each command takes the server's database from `--db` or
`$DATABASE_URL`, works on a draft set only, and prints ids, case hashes, user ids and counts.

**Who labels.** A labeller is a person with the `labeller` role on a membership of the set's
organization (memberships mode, [accounts](accounts.md)), named by their user id
(`user:` and 16 hex digits, as the members list shows it). No one who manages the
organization's hidden sets (an administrator) is ever assigned a case: separation of duties.
Agreement and coverage are shown to the key holder and to the set organization's administrators
(`GET /api/evalsets/{set}`), never to a labeller.

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
withdrawals apply. An unresolved case keeps the set from being frozen
([Freezing a set](#freezing-a-set)) until it is removed (`mendari evalset remove-case`), which
removes its assignments and labels with it; what settles a three-way split instead is the
owner's decision still open.

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

### How a labeller labels

A labeller labels through three routes of the server (LP-11j). They are mounted only on a server
in memberships mode ([accounts](accounts.md)) with both evaluation-set keys set: the hosted site
runs claims mode without the keys, so it mounts none of them, and how labellers will reach it is
the owner's decision still open. Only a person who signed in, acting for the set's organization
with the `labeller` role, reaches them: never a key, a token, an EHR launch or an administrator
(and the keys refuse a server without sign-in), and someone who also manages the organization's
sets (an administrator, whatever other role they hold) is served nothing. Each acts for the
labeller and the organization they act for, never one a body, a query or a path names.

| Route | What it does |
|---|---|
| `GET /api/labelling` | Their own work, set by set: `{"sets": [{"set", "open", "labelled"}], "remaining"}`: how many of their cases wait in each set and how many they labelled. Counts only: nothing is opened or recorded. |
| `GET /api/labelling/next` | Their oldest open case in a set still a draft: `{"assignment", "questions", "input"}`, the case's input and the set's questions with their labels, never its attributes, its author's intended label, anyone's label or a model's output. Each time a case is shown, the chain records the read (`evalset.case_read`) before it is sent. `204` when nothing waits for them. |
| `POST /api/labelling/{assignment}` | Their label: `{"labels": {"<question>": "<label>", ...}}`, every question answered with one of its labels. The answer is `{"remaining"}`, how many of their cases still wait, never how their label compares. One answer per case, sealed and never changed (`evalset.labelled`). |

An assignment that is unknown, someone else's, another organization's, withdrawn, already
answered, or in a set that is no longer a draft is one answer, `404`, so the routes never tell
whose a case is. A label that leaves a question out, names one the set does not ask, or answers
one with a word not among its labels is a `422` naming the question, never the value; nothing is
written, and the case still waits. The notice does not gate labelling. A labeller's calls count
against the `label` rate limit, for each labeller (60 a minute, a burst of 30, unless
`MENDARI_RATE_LIMITS` sets it). The security log says `evalset.case.read` and `evalset.labelled`
with the set's id only (a refused label: `evalset.labelled`, `no_assignment`, with no id), and
the access line names the route's template, never an assignment's id.

### Curating from the dashboard API

An organization's administrators follow and run its sets' labelling through five routes of the
server (LP-11k), mounted as a labeller's are: only on a server in memberships mode with both
evaluation-set keys (the hosted site mounts none of them). Only a person who signed in as an
administrator of the organization they act for (`curate`) reaches them: never a key, a token,
an EHR launch, a labeller or another role. Each acts for that organization, never one a body, a
query or a path names: another organization's set, an unknown id and a malformed one are one
answer, `404`, naming no id, and nothing is written. Making a set, adding or removing its cases,
measuring agreement and coverage, freezing and retiring stay the key holder's commands above: no
route computes statistics, freezes or retires.

| Route | What it does |
|---|---|
| `GET /api/evalsets` | The organization's sets, oldest first: `[{"set", "name", "pack", "state", "created_at", "cases"}]`. Nothing is recorded. |
| `GET /api/evalsets/{set}` | One set: its `name`, `pack` and `questions` (with their labels); its `state`, `created_at`, `frozen_at`, `commitment`, `max_runs`, `max_age_days`, `runs` (`used`, `left`), `graded_until` and `retired_at`; its `cases` and their `statuses`; each labeller's assignments by user id (`labellers`: in each round, assigned, labelled, open and withdrawn); and its `aggregates`, the coverage and agreement the key holder last stored (`agreement` or `coverage`, or at freeze) with when they were measured (`measured_at`), or null before, each pair of labellers' kappa without the pair's label counts (where a pair shares few cases, those are what each answered; the key holder's commands keep them). Never a case, an input, a label given, an attribute or a case id. |
| `POST /api/evalsets/{set}/sample` | Step 1 above: `{"per_stratum": N}` or `{"cases": N}`, and `"seed"` (0 unless given). The answer: `{"set", "drawn", "sampled", "seed"}`. Chained: `evalset.sampled`. |
| `POST /api/evalsets/{set}/assignments` | Step 2: `{"labellers": ["user:…", ...], "seed": 0}`. The answer: `{"set", "round", "assigned", "withdrawn"}`, counts only. Chained: `evalset.assigned`, one per assignment. |
| `POST /api/evalsets/{set}/adjudication` | Step 4: the same body and answer, for each disputed case's third labeller. |

**In the dashboard.** The section list's **Hidden sets** (`#evalsets`, LP-11n) is a screen over
these routes and the members list, shown only to such an administrator on such a server (a
labeller, another role, claims mode or a server without the keys sees neither it nor its link).
It lists the organization's sets; a set opens on its facts (state, commitment, evaluation runs
used and left) and its tabs: **Progress** (its cases by status, and each labeller's first and
third labels by name, read again each time the tab is shown), **Sample**, **Assign** and
**Adjudicate** (steps 1, 2 and 4, for a draft only: per stratum or in all, with a seed; only
members holding the labeller role and no administrator's are offered), **Agreement** and
**Coverage** (what the key holder last stored, step 5, or "Not measured yet": percent agreement
as k of n with its exact interval, each pair's kappa and the pool's alpha with their intervals or
"not estimable" and why, critical labels with too few distinct positives marked "insufficient
evidence"). A refusal is said in the form, in the server's words. It shows ids, counts, names and
the stored aggregates only, never a case.

Each write works on a draft set only (a frozen, active or retired one is refused, `400`), with the
same refusals as its command: a labeller who does not hold the labeller role in the organization,
or who is one of its administrators (the caller too: an administrator is never assigned), refuses
the whole run (`400`, naming the user id), as does a case left without enough labellers who qualify
(`400`, naming no case), and nothing is written. The aggregates name the pack's labels, strata and
languages beside counts and coefficients, never a case: keep what the set's answer shows as you
would the set. The notice does not gate these routes: they take no data in and mint no credential.
Like an administrator's other writes, they have no rate-limit class. The security log says
`evalset.sampled`, `evalset.assigned` and `evalset.adjudication.assigned` with the set's id and how
many (a set refused as another organization's or unknown: `no_set`, with no id), and the access
line names the route's template.

## Freezing a set

A set is `draft` while it is written and labelled, then `frozen`: its content and its rotation
rule are fixed, and from then on every command that would change it (`add`, `remove-case`,
`sample`, `assign`, `adjudicate`, `agreement`, `coverage`, `freeze` again) refuses it, writing
and chaining nothing, and no labeller is served its cases. Only the key holder freezes:

```bash
mendari evalset freeze <set id> --max-runs 20 --max-age-days 180 --db "$DATABASE_URL"
```

Every case of the set is drawn and settled first: agreed, or resolved by its adjudicator. The
command refuses, counting them by why and naming none, while a drawn case is unlabelled (it
waits for its two first-pass labels), disputed (it waits for its adjudicator) or unresolved
(remove it with `mendari evalset remove-case`). A case never drawn refuses it too: draw and
label it, or add `--drop-unsampled`, which removes each undrawn case (with its index rows) in
the freeze's own transaction, chaining `evalset.case_removed` for each. The drawn cases keep
the weights their draws gave them, so the weights still stand for the cases left out.

`--max-runs` and `--max-age-days` are the [rotation rule](#the-rotation-rule): both are
required, and there is no default (the owner's decision, for each set).

Freezing takes each case's final labels (an agreed case's, a resolved case's majority on each
question) and the labels the pack derives from them, and hashes the set's content
(`mendari.evalset/1`, in canonical JSON): the pack's id, its questions with their labels, its
derived questions and critical labels, the rotation rule and when the set froze, then every case by
id with its input, its final labels, its status (agreed or resolved) and its source, stratum,
weight, language, author and template. The set's name is not part of it. A 32-byte salt is drawn,
and the commitment is the SHA-256 of a fixed prefix, the salt and the content hash. The salt, the
content hash and the derived labels are sealed as the set's freeze record; the coverage of the final
labels and the first-pass agreement are sealed as its aggregates, as `evalset agreement` stores
them. Only the commitment is chained (`evalset.frozen`), with the set's size and rotation rule: the
content hash never leaves the sealed record, so no one can confirm a guessed set against the chain.
No signature is made yet: one waits for LP-17's grading-key decision.

The pack is the one the set was made for, found by its id (give `--pack` if it does not resolve
where you run the command); a later version of it grades the set too, since the content keeps
the derived labels as they were. A set whose cases or labels change while it is being frozen is
refused: run it again.

`freeze` prints the set's id, state, time, commitment, how many cases it holds and how many were
left out, its rotation rule, and the distinct positives of each critical label. A critical label
with fewer than 100 distinct positives is a warning (too few to measure it), never a refusal:
the set freezes, and LP-17 decides what may be published from it. Keep the commitment with the
set's records. The output names the pack's labels, as `evalset coverage` does, and no case.

```bash
mendari evalset show <set id> --db "$DATABASE_URL"
```

`show` prints a set's state, how many cases it holds, when it froze, its commitment, its
rotation rule with the grading runs used and left and until when it grades, when it retired,
and its stored aggregates (opened with the keys; without them it prints the rest). It reads
only, chains nothing and needs no key.

## The rotation rule

A frozen set grades a limited number of times, for a limited time, so a pack is never tuned
against it by grading it again and again. Both limits are fixed at freeze, kept in the clear on
the set and inside its content (so editing them breaks its commitment), and chained:

- **Runs.** One run is one chained `evalset.graded`. The set grades while it has had fewer than
  `--max-runs` (1 to 100,000).
- **Age.** The set grades while less than `--max-age-days` days (1 to 3,650) have passed since it
  froze.

Whichever comes first ends its grading: each later run is refused (`no_runs_left` or `expired`,
[Grading a hidden set](#grading-a-hidden-set)), and nothing is read or chained. Nothing retires a
set on its own; retire it, and freeze a new one to grade against. `mendari evalset show` tells how
many runs are left and until when, and each run's answer how many are left after it.

## Grading a hidden set

An organization's grading key grades a pack against one of its frozen or active sets through one
route of the server (LP-11l), mounted with both evaluation-set keys in either account mode. Only
that key reaches it, and only the operator makes one, from the command line:

```bash
mendari access grader-key --org <org> --reason "LP-17's runner" --db "$DATABASE_URL"
```

It holds `grade` and nothing else, for 365 days at most, and its secret is printed once: keep it
as you would the sets. No person (an administrator included), other key or token reaches the
route (`403`, `grading_only`); an EHR launch's cookie is no credential there (`401`), and a
server without sign-in never starts with the keys. An administrator lists and revokes the key
(the Access view, `DELETE /api/auth/keys/{id}`), never rotates it.

```bash
curl -sS -X POST "$MENDARI/api/evalsets/<set id>/runs" \
  -H "Authorization: Bearer $GRADING_KEY" -H "Content-Type: application/json" \
  -d '{"pack": "mendari/patient-inbox-router", "variants": {"router": "naive_bayes"}}'
```

- `pack` is the id of the pack the set was made for, as this server serves it. A later version
  of it grades the set too, as long as it asks the set's questions with their labels and
  critical labels.
- `variants` may name a variant for any of the pack's provider slots; the others run the
  deployment's choice, else the pack's default. **Every slot runs a local variant** (rules,
  criteria, dosing, naive Bayes, or a composite of those only): a hidden set's cases never leave
  the server (owner decision 4 is open: LP-17 decides on remote providers). Where the deployment
  runs a slot remotely (Jev, an LLM), name a local variant for it here.

Before any case is read, the set's header is checked against the pack asked for, and each slot's
variant is checked to be local. Then, in one exclusive transaction, the store's grading read
checks that the set is frozen or active and that its [rotation rule](#the-rotation-rule) allows
another run, recomputes its content against the commitment chained when it froze, makes a frozen
set `active` on its first run (`evalset.activated`) and chains the read (`evalset.graded`: the
grading key, the pack's digest, how many cases and the run's number). Reads made at the same time
never exceed `--max-runs`: each one's check and its chained read are one transaction. Only then
are the cases handed, in memory, to the run, named by their case hashes: each decided with no
store, so no decision is recorded, no report saved (`/api/evals` does not change), no ledger kept
and nothing spent. A run that fails still counts.

The answer is `mendari.hidden_result/1` ([the evaluation reports'
spec](../spec/evaluation-reports.md)): the set's id and commitment, how many cases were graded,
this run's number and the runs left, the pack and its providers, and aggregates only: each
question's metrics (its accuracy as k of n, per-label precision, recall and F1, the confusion
matrix, the Brier score and its interval, calibration bins of 10 cases or more), the outcome
counts and rates, each critical label's counts, the threshold sweep and error counts. Never a
case, its id or hash, a missed or misclassified case, a subgroup, or the dataset's or the
ledger's identity. It names the pack's labels beside counts and rates: keep it as you would the
set's aggregates.

A refusal is answered with its fixed `reason` and one sentence, never more (a store's message
names the set, and a run's could name a case):

| `reason` | Status | Why |
|---|---|---|
| `no_set` | 404 | No set of the key's organization has that id: another organization's set, an unknown id and a malformed one are one answer, naming no id. |
| `wrong_pack` | 422 | `pack` is not the set's, or the pack served here no longer asks the set's questions. |
| `unknown_variant` | 422 | `variants` names a slot or a variant the pack does not have. |
| `remote_provider` | 422 | A slot would run a remote provider. |
| `pack_unavailable` | 409 | The set's pack is not served here, its providers do not build, or what it names does not fit `mendari.hidden_result/1` (more than 64 questions, slots or critical labels, a question with more than 64 labels, or a question, label, slot or variant name the result does not take), so no run of it could be answered. |
| `not_gradable` | 409 | The set is a draft, or retired (its chain's word, whatever its row says). |
| `no_runs_left` | 409 | The set has had its `--max-runs`: retire it and freeze another. |
| `expired` | 409 | The set froze `--max-age-days` days ago or more: retire it and freeze another. |
| `set_changed` | 409 | Its rows no longer give the commitment chained at freeze: run `mendari audit verify --evalsets`, and restore the database from a backup if it was altered. |
| `set_unavailable` | 503 | A value of the set is sealed under a key this deployment lacks (a rotation's reader not deployed): deploy it in `MENDARI_EVALSET_READ_KEYS`, or finish the rotation (`mendari evalset rotate --check`). |
| `run_failed` | 500 | The run failed after its read, which counts. |

No case is read and nothing is chained for a refusal but `run_failed`. The server's log (`mendari.evalsets`, a
warning) names the set, and only the set, for `set_changed` and `set_unavailable`, and the set and
the error's type for `run_failed`. A grading run counts against the grading key's `evaluate` rate
limit (6 an hour unless `MENDARI_RATE_LIMITS` sets it). The notice does not gate it: a key is
never asked. The security log says `evalset.graded` with the set's id and how many cases were
graded (a refusal: its reason, with the set's id, or with no id for `no_set`), and the access line
names the route's template.

## Retiring a set

```bash
mendari evalset retire <set id> --db "$DATABASE_URL"
```

`retire` ends a frozen or active set's use: it checks the set's rows against its commitment once
more (a set that no longer gives it is refused: run `mendari audit verify --evalsets`, and restore
the database from a backup if it was altered; a set holding values sealed under a key this database
knows and the shell lacks is refused naming that key's id: add it to `MENDARI_EVALSET_READ_KEYS`),
marks it `retired` and chains `evalset.retired` with its commitment. It grades nothing more, even
with its row's state edited back (the read checks the chain's `evalset.retired`), it retires once,
and nothing about it is published: its cases stay sealed, and indexed (the hidden-case index still
finds them for the upload check). It prints the set's id, state, time, commitment and the grading
runs it had.

## A second lifecycle

The next set follows the same steps while the first is in use: make it, add its cases, draw,
assign, label, adjudicate and measure it, freeze it, then retire the first when the new one
takes over. Each set keeps its own rotation rule, runs and commitment; writing one never touches
the other, and `mendari audit verify --evalsets` holds between every step.

## Verifying

```bash
mendari audit verify --evalsets --db "$DATABASE_URL"
```

With both keys in the shell, a full verification also checks the sets and cases: each set's sealed
values (its header; its aggregates once measured, and its freeze record once frozen) open under its
own set id; a frozen set is frozen once, never a draft again, keeps the commitment and rotation rule
its `evalset.frozen` chained, takes none of a draft's writes after it (adding, removing, drawing,
assigning, a labeller's read, labelling, measuring: one chained after it breaks verification there,
even once the state is back), is activated once and retired once, is active only after its
`evalset.activated` and retired only after its `evalset.retired`, is neither activated nor graded
after its `evalset.retired`, and its rows, opened, still give the content its commitment covers (a
case's weight or status, the rotation rule or the freeze time changed behind the store's back breaks
it at its `evalset.frozen`; a case's input or a label changed breaks it earlier, at that case's or
label's own event); each case has its row (or a later removal), in its set and organization, and
each row its event; its keyed hash is recomputed when it is under the current index key; its input
and attributes open and give the commitment its event chained. Each assignment has its row, with its
case, labeller and round, and each label its row, answering that labeller's assignment in that
round; each label opens under its own set, case and id and gives the commitment its event chained
(unless its case was removed since, when they went with it). A row altered, moved or forged stops
the verification at its event (`broken_at`); a row, or a set's state, without its event is counted
(`unchained`); a row sealed under a key this database knew and the shell lacks is counted by key id
(`unopenable`). The counts are of sets, cases checked, removed and unchecked, assignments, labels
and frozen sets recomputed. A draft set's header is only checked to open under its own set id: it
has no chained commitment until the set is frozen, so a draft's header re-sealed by someone holding
the key passes; a frozen set's header is part of its content. Without the keys, each case, each
label and each frozen set is counted `unchecked`, never a break. It reads only, marks nothing, and
cannot be combined with `--since-checkpoint`.

## Rotating the evaluation-set key

`mendari evalset rotate` re-seals under the new key every sealed value of every set (headers,
case inputs and attributes, labels, aggregates and freeze records), as
`mendari phi rotate` does for the PHI key ([Deploying with PHI](../deploying-with-phi.md),
section 3, Rotating the keys). It works in batches, each recorded in the chain
(`evalset.rotated`: key ids and counts, never a key). A re-seal changes the ciphertext only,
never the value, so it runs on sets in every state, frozen, active and retired ones included.
The chain's case hashes and commitments stay as they are, and a frozen set still gives its
commitment afterwards. In this order:

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
   the new key alone, recomputes each case's and each label's commitment against the chain and each
   case's keyed hash, checks its rows in the hidden-case index under the current index key, and
   recomputes the content of each set the chain froze, whatever its row's state now, against the
   commitment its first `evalset.frozen` chained (a set whose row is gone, is a draft again or holds
   no freeze record fails). It reads only: nothing is written, marked or chained. `"ready": true`
   (exit 0) means nothing needs the old key. A failure names its kind (`set` for a frozen set's
   content), its set, a case by its hash or a label by its id, and the key it names or what did not
   match (`commitment`, `case_hash`, `index`), never a value. A key in `read_not_retired` had
   nothing to move, so it was not retired: make sure no deployment still seals with it before you
   drop it. Then run `mendari audit verify --evalsets`.
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
