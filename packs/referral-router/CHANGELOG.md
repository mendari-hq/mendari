# Changelog: openhealth/referral-router

## 0.1.1

- `rules_and_jev` and `rules_and_llm`: for `clear_question` the more generous reader now wins
  (`escalate: yes`), so a referral goes back to the referrer only if neither the rules nor the
  model finds a request. Before, the keyword rules alone could send a clear referral back
  ("wants to discuss biologic options" has no request keyword), which delays the patient's
  care. Found in the provider comparison on the held-out set; `urgency` is unchanged.

## 0.1.0

- First release. Reads a specialty referral as it is signed (CDS Hooks `order-sign`) or at a
  referral office and suggests the next step: `urgent_review`, `coordinator_review`,
  `return_for_info` (with the missing items listed) or `schedule`.
- `specialty` (eight specialties or `other`): naive Bayes trained on synthetic referrals by
  default, with `jev` and `llm` variants.
- `urgency` and `clear_question`: English keyword rules by default, with `jev`, `llm`,
  `rules_and_jev` and `rules_and_llm` variants.
- A checklist computed in code: recent visit note, ECG, imaging, labs, and the priority the
  referrer chose. Which items a specialty requires is part of the decision table (`when`).
- Risk level 1: complete, routine referrals with a confident specialty are scheduled without a
  person reading them; anything possibly urgent is always reviewed (P(urgent) >= 0.3).
- FHIR mapping (ServiceRequest referrals, Patient, DocumentReference, DiagnosticReport,
  Observation) and a CDS Hooks `order-sign` service with one card per signed referral.
