Drafted by a build session for the owner to review. Not clinically reviewed.

# The harder inbox benchmark: a brief for the owner's review

_Written 2026-11-07 (US Eastern), for the open item "Harder inbox benchmark" in
`tasks/todo.md`, which the owner asked on 2026-11-07 to have its generation tooling built for
the owner's own later review. Nothing in the draft has been read by a clinician. The review is
recorded by the owner in the table at the end of this note, never by a build session; until
then every file of the draft says "Draft, not clinically reviewed", and nothing may describe it
as reviewed._

## What was built

- **The generator**, `src/mendari/sandbox/synthetic_inbox_harder.py`: local, seeded and
  deterministic (no network, no model calls; the same seed writes the same bytes, whatever
  Python's hash seed). It writes Patient Inbox Router cases from **families**: each family is
  one way of asking for one thing, with its labels, written as a few frames (sentence skeletons
  with slots) for this set, not copied from the shipped generator. Each family belongs to one
  split. Every **paraphrase source** (slot values, hedges, mixed-topic asides, greetings,
  sign-offs, subjects, texting style, typo operators) has one bank per split, with its own id.
- **Two splits**: `train` (may be used to train or tune) and `heldout` (must never be used to
  write or tune anything, as the pack's `red_flags_holdout`). 436 cases each, 152 of them
  possible emergencies (`urgency=urgent`, the pack's one critical label): 12 English cases for
  each of the 10 signs the `urgent` criterion lists, and 8 Spanish cases for each of chest pain,
  trouble breathing, stroke and self-harm. Beside them, routine families for every team and
  look-alike families: routine messages that use red-flag words (a past emergency already
  assessed, plain negation, figures of speech about a bill, questions about warning signs,
  fever in an older child, a small cut or bump that has settled).
- **The leakage check** (`leakage_check`), run on what was generated: no family and no source
  in both splits; no case filed under the other split's family; no sentence of 16 characters or
  more and no run of 6 words shared by the two splits; none shared by the held-out split and the
  shipped pack's training and tuning data (`data/train.jsonl`, `calibration.jsonl`,
  `red_flags.jsonl`) or the text of any shipped template; none shared by the train split and the
  shipped hand-written held-out set (`red_flags_holdout.jsonl`). It passes with 0 problems. It
  caught three real overlaps while the frames were written, which were reworded.
- **The coverage check** (`coverage_problems`): in each split, every label of both questions,
  the critical label in English and Spanish, every red-flag sign and every frame.
- **The files**, in `datasets/drafts/patient-inbox-harder/`: `train.jsonl`, `heldout.jsonl`,
  `REPORT.md` (counts per label and split, both checks' results, how each family was made) and
  `FAMILIES.md` (the review sheet: every frame, with a case drawn from it, and every slot's
  values). `uv run python scripts/harder_inbox_draft.py` writes them and `--check` proves the
  committed files are the generator's; `tests/test_synthetic_inbox_harder.py` checks the bytes,
  plants a leak of each kind in a copy to prove the check finds it, and checks coverage.

What it is not:

- **Not the pack's benchmark.** The Patient Inbox Router's data, model, benchmark and digest
  are as shipped (`mendari/patient-inbox-router@0.4.3`,
  `sha256:d076c4d9e59eda856fa8246daa8e2aea3f5533fe7523daaf61024871f593dfc2`). Adopting the draft
  is a separate decision (question 7 and "What adoption would do", below).
- **Not a hidden evaluation set.** It is public and generated from frames, so it lives in the
  repository, beside the example datasets, and the hidden-set store refuses its cases as public
  (LP-11's public-source rule, `docs/operations/evalsets.md`).
- **Not prevalence.** A third of each split is urgent, far above a real inbox: the set measures
  the critical label, so its precision and accuracy are not a site's.

## What the shipped pack scores on it

Measured once by the build session on 2026-11-07, at the digest above, with the pack's offline
variants (the keyword rules as the safety screen, the naive Bayes router). No model screen was
run: Jev and LLM calls are remote and paid, and run only when the owner asks (about 436 calls a
split).

| Dataset | Cases | Team right | Emergencies flagged stat | Routine flagged stat |
|---|---|---|---|---|
| The shipped benchmark (`test`) | 600 | 99.3% | 23 of 23 | 0 |
| Draft `train` | 436 | 72.7% | 8 of 152 | 12 |
| Draft `heldout` | 436 | 73.9% | 5 of 152 | 6 |

The keyword screen alone misses almost every emergency phrased the way these frames phrase
them, as the pack's card already says of its hand-written held-out set (4 of 20). That is why
the pack's policy automates nothing on the rules alone: every one of these messages would still
reach a person, without the stat priority. The number that would matter is a model screen's
(`rules_and_jev`, `rules_and_llm`) on the held-out split, which needs the paid run.

## How to review it

About an hour. Everything to read is in `datasets/drafts/patient-inbox-harder/`.

1. **`REPORT.md`** (5 minutes): the counts per label, sign, language and split, the leakage and
   coverage results, and one line per family on how it paraphrases.
2. **`FAMILIES.md`**, the review sheet (most of the hour). Each family lists its team and
   urgency, then its frames numbered, each followed by a case drawn from it. Read in this order:
   1. the possible emergencies, 10 English families a split and 4 Spanish: is every frame a
      possible emergency now, by the pack's `urgent` criterion?
   2. the look-alikes (`past_assessed`, `negated`, `idiom`, `education`, `older_child_fever`,
      `minor_injury`): is every frame routine?
   3. the routine families: is the team right?
   4. the slot values at the end: medications, temperatures, a baby's age, weeks of pregnancy,
      lab values. A frame is filled from its own split's values only.
3. **Optionally, cases**: each case in `heldout.jsonl` names its family and frame
   (`attributes.family`, `attributes.frame`), what was done to it (`attributes.transforms`:
   hedge, aside, texting, typo) and the banks it drew on (`attributes.sources`).

To check the files are the generator's: `uv run python scripts/harder_inbox_draft.py --check`.
To see the shipped pack on a split:
`uv run mendari eval packs/patient-inbox-router --dataset datasets/drafts/patient-inbox-harder/heldout.jsonl --baseline none`.

## What to answer

Each question has quick-pick options. Reply in one line, for example
`1A 2B (train.en.idiom 3) 3A 4A 5A 6A 7C`, with any family and frame numbers after a B. The
frames named under a question are the ones this session was least sure of; they are not the
only ones to read.

1. **The emergencies.** Is every frame of the urgent families a possible emergency now, under
   the pack's criterion? Least sure: `train.en.bleeding` 4 and `heldout.en.bleeding` 5 (a long
   nosebleed on a blood thinner), `train.en.altered` 2 and `heldout.en.altered` 2 (a blackout,
   now awake), `train.en.severe_pain` 3 (sudden pain low on the right side).
   - A. Yes, all of them.
   - B. Yes, except the frames I list.
   - C. No: the urgent families need rework (I'll say how).
2. **The look-alikes.** Is every look-alike frame routine, not an emergency now? Least sure:
   `train.en.idiom` 3 and `heldout.en.idiom` 1 (death as a figure of speech),
   `train.en.minor_injury` 2 (a child's bump on the head, normal since), `heldout.en.education`
   3 (a newborn's temperature, asked before the birth), `heldout.en.past_assessed` 1 (a past
   blood clot in the lung).
   - A. Yes, all routine.
   - B. Routine except the frames I list (to be urgent, or dropped).
   - C. Drop the figures of speech: any death language should read as worth a person's look.
3. **The teams.** Are the routine families' teams right, and the mixed-topic rule: an
   administrative aside (a second, smaller request) never changes the team, because it joins
   only a family whose team outranks the aside's (front desk and billing, then medical
   assistant, then nurse and physician)?
   - A. Yes.
   - B. Yes, except the families I list.
   - C. The mixed-topic rule is wrong (I'll say how).
4. **The phrasing.** Most emergency frames avoid the usual words on purpose, and cases add
   hedges ("Not a big deal, I think."), typos and texting. Is that a fair stress test?
   - A. Yes, realistic enough for a stress test.
   - B. Too indirect: add frames that use the usual words ("chest pain", "can't breathe") so
     the set also measures the plain case.
   - C. Not realistic: rework (I'll say what).
5. **Spanish.** Spanish covers 4 of the 10 signs and 5 routine topics, and no native speaker
   has read it.
   - A. Fine as a draft; a native speaker reads it before adoption.
   - B. Extend Spanish to all 10 signs before adoption.
   - C. Drop Spanish from this set.
6. **Size.** 436 cases a split, 12 per English sign; more cases from the same frames only vary
   the slots.
   - A. Keep it.
   - B. Write more frames for the signs I list before adoption.
7. **Adoption** (answer once 1 to 6 are settled).
   - A. Add the held-out split to the pack as an extra dataset for the critical label, beside
     the shipped benchmark, which stays `test`.
   - B. Make the held-out split the pack's benchmark (`benchmark_dataset`).
   - C. Not yet: revise it as answered, and ask me again.
   - D. Do not adopt it.

The build session's suggestion, for the owner to take or leave: A, after any changes asked for.
The shipped `test` split is a regression test of the pack, whose published numbers the card,
the scoreboard and the leaderboard name; the held-out split measures the critical label, which
those numbers overstate.

## What adoption would do

A separate pull request, after the owner's answers, and only as answered:

1. Apply the answers to the generator (families, frames, banks), write the draft again, and
   have the owner read the families that changed.
2. Copy the splits into the pack as `data/harder_train.jsonl` and `data/harder_heldout.jsonl`
   and declare them under `evaluation.datasets` in `pack.yaml` (under 7B, also
   `benchmark_dataset: harder_heldout`). The held-out split is never used to write or tune rules
   or train the model.
3. Write them from `scripts/build-reference-pack.sh`, so CI's byte-reproducibility check covers
   them, and run it: the pack's digest changes, so its version goes up, and its benchmark (under
   7B) and Decision Card are written again. The card's evidence and known failure modes then
   name the held-out split and what the offline variants score on it.
4. Mark the cases as reviewed only as the owner's record says (the table below), with the
   reviewed file's SHA-256. Gate `clinician-review` (`launch/gates.yaml`) stays the owner's to
   attest; a build session never marks it met.
5. With the owner's go-ahead for the paid calls, measure the model screens on the held-out
   split (a release requires no automated miss of a critical label, `auto_missed == 0`, on the
   datasets it is measured on).

## The review

To be filled in by the owner. A build session leaves it blank. A reviewer's name, if anyone
other than the owner reviews, is kept in a note outside the repository.

| Field | Value |
|---|---|
| Reviewer's role | |
| Date | |
| Generator version and seed (`REPORT.md`) | |
| SHA-256 of `heldout.jsonl` reviewed (`REPORT.md`) | |
| Answers to the questions above | |
| Verdict (adopt as answered in 7, revise, do not adopt) | |
