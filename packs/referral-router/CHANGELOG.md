# Changelog: mendari/referral-router

Renamed on 2026-09-30, when the project became Mendari: this pack was `openhealth/referral-router`.
Its id, its publisher's name and its `https://mendari.co/...` identifiers changed; its criteria,
rules, models, data and results did not.

## 0.1.4

- `code_systems` declares the systems the pack uses: SNOMED CT, with its licence terms (a pack
  naming a licensed code system now has to say them), and LOINC.
- Criteria, rules, providers, policy, models and data are unchanged; every decision is identical.
  The digest changes because the manifest does.

## 0.1.3

- Every surface now says what the pack is: "Synthetic example. Not clinically reviewed. Not for
  patient care." It is derived from the new `card.provenance: synthetic`, never written by the
  pack (the Decision Card's first line, the dashboard, Try it, the review queue, CDS Hooks cards
  and discovery, the registry and the Hub).
- Each question has a `display` name, so people read words instead of keys, and one label has a name (`return_for_info`).
- Criteria, rules, providers, policy, models and data are unchanged; every decision is identical.
  The digest changes because the manifest does.

## 0.1.2

- `specialty_reader`: a `cascade` variant (naive Bayes when it is at least 95% sure, else Jev),
  as the Patient Inbox Router's router has. When Jev cannot answer, the referral still gets
  naive Bayes' specialty and goes to a person, instead of abstaining. A Mendari server with Jev
  set up runs it. The default (`naive_bayes`), questions, policy, rules, data and model are
  unchanged.

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
