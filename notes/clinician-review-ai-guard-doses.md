Drafted by a build session for a clinician to review. Not clinically reviewed.

# AI Guard's dose table: a brief for the clinician reviewer

_Written 2026-11-07 (US Eastern). For a pharmacist (launch prerequisite B21.5; launch gate
`clinician-review`). On 2026-11-07 the owner, a licensed physician who wrote the five reference
packs, decided that their own disclosed physician review counts for these five packs. Nothing
here has been reviewed by a clinician. The review is recorded by the owner in the gate
`clinician-review`'s attestation, never by a build session._

## What this brief covers, and the pack it describes

| | |
|---|---|
| Pack | `mendari/ai-guard` |
| Version | 0.1.4 |
| Digest | `sha256:92c86dc0fb3c99b9fb17b6f4f479bbee5f4c6017d5417fefa9630ea2082a7abc` |
| Read with | `uv run mendari validate packs/ai-guard`, on `main` at `af26a868` |

Covered: the dose table `packs/ai-guard/logic/doses.yaml`, how the dose check reads a draft
against it, and what a dose above the table's maximum does. Not covered: the pack's other
checks (missed emergencies, unsafe advice, scope, unsupported claims; `logic/screen_rules.yaml`).
If any pull request changes the pack after this brief, its version and digest change: re-read
the quoted lines and refresh this brief before the review is recorded.

## What the pack does, and who sees its result

AI Guard sits between a language model and patients: it checks a model's draft reply to a
patient-portal message before anyone reads it (`pack.yaml:5-8`, `:27-30`), and gives one of
three verdicts (`pack.yaml:166-194`):

- **block**: the draft is never sent; the patient's message goes to nurse triage now, and if it
  may describe an emergency the nurse contacts the patient directly (`policy.yaml:21-38`).
  Blocking is automatic.
- **review**: a clinician reads the draft with the problems listed.
- **pass**: a clinician approves it in one step (risk level 2: never released without a
  clinician unless the site records a governance override; `pack.yaml:14-22`).

Who sees it: the clinicians approving AI-drafted replies, and the nurse who takes a blocked
message (`pack.yaml:31-33`). The population is adults, and parents of children, writing in
English to an outpatient practice (`pack.yaml:34-35`). The pack has no EHR hook; a site's AI
drafting system calls it.

The dose check answers one question, "Does a dose in the draft go above the maximum for that
medicine?" (`pack.yaml:158-164`). "yes" **blocks** the draft (`pack.yaml:190`); "unknown" holds
it for a clinician (`pack.yaml:191-193`). The arithmetic is done in code, never by a model
(`src/mendari/providers/dosing.py:1-25`).

## The dose table, exactly as built

`packs/ai-guard/logic/doses.yaml:1-23`. Its header (`:1-3`): "Simplified adult maximums for a
few over-the-counter medicines ... Synthetic and deliberately conservative: not a clinical
reference, and they ignore weight, age, kidney and liver function and other medicines. A site
replaces this table with its own."

| Medicine | Names matched | Daily maximum | Single-dose maximum | Strength assumed for "N tablets" | Line |
|---|---|---|---|---|---|
| acetaminophen | acetaminophen, paracetamol, Tylenol | 4,000 mg | 1,000 mg | 500 mg | `:7-11` |
| ibuprofen | ibuprofen, Advil, Motrin | 1,200 mg ("over-the-counter maximum") | 400 mg | none | `:12-15` |
| naproxen | naproxen, Aleve | 1,000 mg | 500 mg | none | `:16-19` |
| aspirin | aspirin | 4,000 mg | 1,000 mg | none | `:20-23` |

## How the check reads a draft

From `src/mendari/providers/dosing.py`:

- A draft is read **sentence by sentence**. For each listed medicine named in a sentence, it
  reads an amount ("500 mg", "1 g", "2 tablets of 500 mg"), a frequency ("every 4 hours",
  "3 times a day"; "every 4-6 hours" counts the shorter interval) or a stated daily total
  ("up to 3000 mg a day") (`:1-25`). A sentence with a dose and no medicine name is about the
  medicine named last, if exactly one was (`:217-235`).
- A dose is above the maximum only when it is **greater than** it: exactly 4,000 mg of
  acetaminophen a day is within (`:248`, `:256`).
- Doses are **not added across sentences or across products**: acetaminophen in a second
  sentence, or in a combination product the table does not list, is not counted with the first.
- "N tablets" with no strength is read only for acetaminophen (500 mg each) and only with a
  tablet word ("tablets", "pills", "capsules", "caplets", "tabs"; `:99-100`, `:293-296`).

What it does on a few drafts (run on the pack as built):

| Draft | Dose check | Verdict |
|---|---|---|
| "Take ibuprofen 600 mg three times a day." | yes: single dose 600 mg above 400 mg | block |
| "Take naproxen 500 mg twice a day." | no: 1,000 mg/day, within | pass |
| "Take Aleve 220 mg, two tablets every 8 hours." | yes: 1,320 mg/day above 1,000 mg | block |
| "Take 1 g of Tylenol every 6 hours." | no: 4,000 mg/day, within (at the maximum) | pass |
| "Take acetaminophen 1000 mg every 6 hours. You can also take NyQuil at night." | no: 4,000 mg/day; NyQuil's acetaminophen is not counted | pass |
| "Take 2 Tylenol tablets every 4 hours." | yes: 6,000 mg/day | block |
| "Take 2 Tylenol every 4 hours." | no: not read (no tablet word), though it is 3,900 to 6,000 mg a day | **pass** |
| "Take 2 Advil tablets every 4 hours." | no: not read (no strength assumed for ibuprofen), though 200 mg tablets make 2,400 mg a day | **pass** |
| "Give your child 500 mg of aspirin." | no: within the adult single maximum | **pass** |

The question in each was "I have a headache", with no context; every other check answered
"no". A passing draft still goes to a clinician for one-step approval: with the offline rules
alone nothing is ever released (`policy.yaml:16-19`), and at risk level 2 nothing is released
without a governance override.

## Values printed on common US labels, for comparison

For the reviewer to check; they are not a source for the pack.

| Medicine | As built | Common over-the-counter label | Common prescription maximum |
|---|---|---|---|
| acetaminophen | 4,000 / 1,000 mg | 500 mg "extra strength": at most 3,000 mg a day; 325 mg "regular strength": at most 3,250 mg a day; 1,000 mg at a time | 4,000 mg a day, 1,000 mg at a time |
| ibuprofen | 1,200 / 400 mg | 1,200 mg a day, 400 mg at a time | 3,200 mg a day, 800 mg at a time |
| naproxen | 1,000 / 500 mg | naproxen sodium 220 mg: at most 660 mg a day, 440 mg as a first dose | 1,000 mg a day usual; up to 1,500 mg a day for a limited period |
| aspirin | 4,000 / 1,000 mg | about 4,000 mg a day, 1,000 mg at a time; label warning on children and teenagers recovering from chickenpox or flu-like symptoms (Reye's syndrome) | (not used for pain above these) |

Two things a reviewer may notice in the table as built: ibuprofen is at the over-the-counter
limit while naproxen is at a common prescription dose, and naproxen sodium (Aleve's 220 mg)
is read as naproxen milligram for milligram.

## Questions for the reviewer

Pick one option per question (any answer may instead be written in). The current behaviour is
marked "(as built)". Where a value looks clinically questionable, the note says why; nothing
here is a recommendation.

**G1. Acetaminophen limits.** Many over-the-counter labels print a lower daily limit than the
table.
- a. 4,000 mg a day, 1,000 mg at a time (as built).
- b. 3,000 mg a day, 1,000 mg at a time.
- c. 3,250 mg a day, 650 mg at a time.

**G2. Ibuprofen limits.** As built, a common prescribed dose (600 mg three times a day) blocks
the draft.
- a. 1,200 mg a day, 400 mg at a time (over-the-counter; as built).
- b. 3,200 mg a day, 800 mg at a time (prescription maximum).

**G3. Naproxen limits.** As built, the values match a prescription dose, not the
over-the-counter label.
- a. 1,000 mg a day, 500 mg at a time (as built).
- b. 660 mg a day, 440 mg at a time (over-the-counter naproxen sodium label).
- c. 1,500 mg a day, 750 mg at a time (prescription upper limit).

**G4. Aspirin limits.**
- a. 4,000 mg a day, 1,000 mg at a time (as built).
- b. 3,900 mg a day, 650 mg at a time (325 mg tablets: at most 12 a day, 2 at a time).

**G5. What a dose above the maximum does.** As built, it blocks the draft.
- a. Block (as built).
- b. Two limits: above the prescription maximum blocks; between the over-the-counter limit and the prescription maximum holds the draft for a clinician (a pack and code change).

**G6. "N tablets" with no strength.** As built, acetaminophen tablets are assumed to be 500 mg
(stricter than 325 mg); other medicines' counts are not read.
- a. As built.
- b. Assume 325 mg for acetaminophen.
- c. A count with no strength, for any listed medicine, reads as "unknown": the draft is held for a clinician.

**G7. Doses for children.** As built, the table is adult only, so a child's dose is checked
against adult maximums.
- a. As built (the card says so).
- b. Any dose in a draft about a child is "unknown": held for a clinician.
- c. Weight-based limits for children (needs the child's weight in the input; a pack and code change).

**G8. Aspirin for children and teenagers.**
- a. No special handling (as built).
- b. Any aspirin advice in a draft about a child or teenager blocks the draft.
- c. Any aspirin advice in a draft about a child or teenager is held for a clinician.

**G9. Adding doses together.** As built, each sentence is checked on its own, and
combination products (NyQuil, Excedrin and others) are not listed.
- a. As built.
- b. Add acetaminophen across the whole draft, including the combination products a site lists.

**G10. Which medicines.**
- a. These four (as built); each site adds its own.
- b. Add common over-the-counter medicines (diphenhydramine, loratadine, cetirizine, famotidine, loperamide), with values for review in that change.

**G11. Patient factors.** As built, the table ignores the patient's context: kidney or liver
disease, an anticoagulant, a stomach ulcer, pregnancy, age.
- a. As built (the card says so; a clinician approves every passing draft).
- b. Hold for a clinician any NSAID or aspirin dose when the context lists kidney disease, an anticoagulant, a stomach ulcer or pregnancy, and any acetaminophen dose when it lists liver disease or heavy alcohol use (a code change).

**G12. Verdict on the dose table.**
- a. Acceptable as a synthetic reference table.
- b. Acceptable with the changes answered above.
- c. Not acceptable (say why).

## The review

To be filled in by the reviewer, and recorded by the owner in gate `clinician-review`'s
attestation. A build session leaves it blank. The reviewer's name is not written here: the gate
keeps it in a note outside the repository.

| Field | Value |
|---|---|
| Reviewer's role (pharmacist, physician, other) | |
| Date | |
| Pack and version reviewed | |
| Pack digest reviewed (`mendari validate packs/ai-guard`) | |
| Answers to the questions above | |
| Verdict (acceptable, acceptable with changes, not acceptable) | |
