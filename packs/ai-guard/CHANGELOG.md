# Changelog: openhealth/ai-guard

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
