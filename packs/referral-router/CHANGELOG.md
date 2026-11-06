# Changelog: mendari/referral-router

Renamed on 2026-09-30, when the project became Mendari: this pack was `openhealth/referral-router`.
Its id, its publisher's name and its `https://mendari.co/...` identifiers changed; its criteria,
rules, models, data and results did not.

## 0.2.1

- A CDS Hooks card only when the person signing the referral can act
  (`cds_hooks.card.show: [return_for_info, urgent_review]`, LP-23j; the owner confirmed the
  list on 2026-11-06): a referral to send back with what is missing, and one to review today.
  A referral to schedule, for a coordinator, or with no suggestion is decided and audited as
  before, for the referral office, and shows no card. So is a referral Mendari refuses as
  invalid input (BLOCK, a reason over 5,000 characters for one), which has no suggestion
  either: it showed "Referral: no suggestion; the referral office will review it" before, and
  it is not in Mendari's review queue, so the EHR's own referral workflow carries it. An ABSTAIN
  because part of the record could not be read still shows its `info` card for every referral;
  a referral whose input is also refused is a BLOCK, with no card. Of the 300 referrals the
  FHIR decisions golden holds (the bundled local-site example, sent through FHIR), 235 show a
  card and 65 do not (53 to schedule, 12 for a coordinator); each of the 300 showed one before.
- The out-of-scope line on checking the referrer's specialty says that only a referral to send
  back or to review today shows the suggested specialty on a card.
- Criteria, rules, providers, policy, models and data are unchanged; every decision is identical
  (the benchmark's figures, its ledger and the FHIR decisions golden). The digest changes because
  the manifest does.

## 0.2.0

- No SNOMED CT: the FHIR mapping's `focus_where`, which tells a referral from the other
  ServiceRequests (lab and imaging orders), matched the SNOMED CT code for a patient referral.
  It now matches `FR101`, an invented code in a code system named as fictional,
  `https://mendari.co/fhir/CodeSystem/fictional-referrals`, which the sandbox's synthetic
  referrals carry. SNOMED CT's licence terms are a question for counsel (LP-14), and no package
  carries its codes any more.
  **Through FHIR, the pack now matches no real referral.** Until a site code map exists
  (LP-23), a referral from a real EHR (CDS Hooks, the SMART launch, `$decide`) is not found
  and nothing is decided: a site lists the code its EHR marks a referral with in
  `fhir/mapping.yaml`. Pack inputs sent directly are not affected. The known failure modes say
  so.
- `code_systems` drops SNOMED CT and declares the fictional referral codes.
- Criteria, rules, providers, policy, models and data are unchanged. Every decision is the same
  as in 0.1.4: the 2,768 data, dataset and behaviour-test cases, and the 300 local-site
  referrals sent as FHIR (with the fictional code in place of SNOMED CT's), compared case by
  case. The digest changes.

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
