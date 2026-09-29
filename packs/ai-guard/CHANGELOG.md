# Changelog: openhealth/ai-guard

## 0.1.2

- Synthetic drafts now fit their questions. Each routine question is answered only with drafts
  written for its topic: dose advice for headache and back pain, antibiotics for a cold,
  stopping a blood-pressure pill for dizziness on a new one. The patient's medicines appear in
  the context only where the question concerns them. Before, drafts were paired with random
  questions (ibuprofen advice for a question about blood-pressure pills), and readers that check
  the draft against the question were marked wrong for flagging such pairs.
- Regenerated calibration, test and local-site data; new rules-only numbers in the card. The
  hand-written sets, criteria, rules and policy are unchanged.

## 0.1.1

- Clearer criteria, found by running a model reader on the tuning set before the provider
  comparison. Over-the-counter dose advice, advice to keep taking a medicine and explaining a
  result are in scope; a model read them as prescribing, which sent safe drafts to review.
- A dose that is too high is now unsafe advice too, in the criteria and in every dataset's
  labels (three hand-written tuning drafts and three held-out drafts, all with
  `dose_exceeds_limit` yes). A model reader then flags overdoses of medicines the dose table
  does not list. Verdicts are unchanged.
- Waiting, a routine visit or a later message is unsafe advice when the patient's message may
  describe an emergency, as the labels already said.

## 0.1.0

- First release. Checks a language model's draft reply to a patient before it is sent:
  `pass`, `review` (the problems listed) or `block`.
- Six checks. A possible emergency in the patient's message, whether the draft escalates it,
  unsafe advice, a diagnosis, prescription or treatment change, and a claim about the record
  the context does not show are read by English keyword rules by default, with `jev`, `llm`,
  `rules_and_jev` and `rules_and_llm` variants. Doses are checked in code by the `dosing`
  provider against simplified adult maximums for acetaminophen, ibuprofen, naproxen and
  aspirin.
- Blocks drafts that miss an emergency, give a dose above the maximum or give unsafe advice,
  and returns a fallback: route the patient's message to nurse triage.
- Risk level 2: a clinician approves every passing draft; releasing drafts automatically
  needs a recorded governance override and a model in the screen.
- Guard interface (`guard: {draft: draft, release: [pass]}`) for `POST
  /api/packs/openhealth/ai-guard/guard`. No CDS Hooks service: the guard sits in front of a
  language model, not in an EHR workflow.
