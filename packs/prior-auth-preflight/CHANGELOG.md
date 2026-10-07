# Changelog: mendari/prior-auth-preflight

Renamed on 2026-09-30, when the project became Mendari: this pack was `openhealth/prior-auth-preflight`.
Its id, its publisher's name and its `https://mendari.co/...` identifiers changed; its criteria,
rules, models, data and results did not.

## 0.2.1

- Known failure modes state their findings and the command that measures them, no longer
  figures typed in by hand (LP-10: every figure on a card is computed). Criteria, rules,
  providers, policy, models and data are unchanged; every decision is identical. The digest
  changes because the manifest does.

## 0.2.0

- The procedure codes are fictional: the 15 CPT codes the pack, its data and its tests used are
  replaced one for one by invented codes (`FX101` to `FX903`) in a code system named as
  fictional, `https://mendari.co/fhir/CodeSystem/fictional-procedures`. CPT belongs to the
  American Medical Association, and the pack no longer carries any. Its other code systems,
  and the notices they need, are in `code_systems` and NOTICE.
  **A site that sends CPT codes maps them to these** (in `logic/structured.yaml` and the FHIR
  mapping's `focus_where`), as it already maps any code the pack does not list.
- `code_systems` declares the systems the pack uses (the fictional procedures, ICD-10-CM).
- Every decision is the same as in 0.1.3 with the codes swapped: each requirement set,
  answer, probability and outcome of the 1,272 examples, datasets and tests. The data and the
  model were rebuilt from the generator (only the codes changed); the digest changes.

## 0.1.3

- Every surface now says what the pack is: "Synthetic example. Not clinically reviewed. Not for
  patient care." It is derived from the new `card.provenance: synthetic`, never written by the
  pack (the Decision Card's first line, the dashboard, Try it, the review queue, CDS Hooks cards
  and discovery, the registry and the Hub).
- Each question has a `display` name, so people read words instead of keys, and two labels have names (`lumbar_mri`, `submit_ready`).
- Criteria, rules, providers, policy, models and data are unchanged; every decision is identical.
  The digest changes because the manifest does.

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
