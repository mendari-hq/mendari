# Changelog: openhealth/prior-auth-preflight

## 0.1.2

- Da Vinci DTR (`cds_hooks.questionnaire`): `Questionnaire/$questionnaire-package` serves a
  questionnaire of the documentation the payer asks for, for the order's service and payer (the
  members of `next_step`'s table that can make it `needs_attention`), with a response pre-answered
  from the pack's decision. The decision is found by the CRD assertion id the order carries. The
  CRD coverage information for `needs_attention` links the questionnaire. Questions, criteria,
  rules, policy and data are unchanged.

## 0.1.1

- Da Vinci CRD: the `order-sign` service also returns, for each decided order, a system action
  updating the order with CRD's `coverage-information` (`cds_hooks.coverage_information`):
  `submit_ready` and `needs_attention` are covered with prior authorization needed (and clinical
  documentation needed for `needs_attention`), `not_required` needs no authorization, and
  `not_checked` asserts nothing. The assertion id is the decision id. Cards are unchanged.
  Questions, criteria, rules, policy and data are unchanged.

## 0.1.0

- First release. Checks an order against the patient's payer before prior authorization:
  `submit_ready`, `needs_attention` (the gaps listed), `not_required` or `not_checked`.
- Synthetic requirement sets for two synthetic payers (Acme, Beacon) and three services
  (lumbar spine MRI, knee arthroscopy, sleep study). Beacon does not require authorization for
  sleep studies; Acme asks for prior imaging.
- Structured elements computed in code (service, payer, supporting diagnosis, prior imaging)
  and note elements read by English keyword rules by default, with `jev`, `llm`,
  `rules_and_jev` and `rules_and_llm` variants: conservative treatment, red flags, knee
  mechanical symptoms and sleep apnoea symptoms. A clear red flag waives conservative
  treatment before a lumbar MRI.
- Risk level 1: complete requests may be queued for submission automatically, but only while
  the notes are also read by a model; a possible red flag always goes to a person.
- FHIR mapping (ServiceRequest, Coverage, Condition, Procedure, DocumentReference) and a
  CDS Hooks `order-sign` service with one card per order the pack covers.
