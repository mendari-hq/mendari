# Changelog: mendari/prior-auth-preflight

Renamed on 2026-09-30, when the project became Mendari: this pack was `openhealth/prior-auth-preflight`.
Its id, its publisher's name and its `https://mendari.co/...` identifiers changed; its criteria,
rules, models, data and results did not.

## 0.3.0

- **Through FHIR, the pack reads EHR data on spec 0.3** (LP-23k, after the owner's go-ahead on
  2026-11-06; `spec_version: "0.3"`, `fhir/mapping.yaml` only). A MINOR bump, by CONTRIBUTING.md's
  rule (MINOR for a change in behaviour): what the pack decides through FHIR changes where the 0.2
  mapping misread the data, and nothing else does.
  - The order (and `focus_where`, which orders are the pack's), the diagnoses and the prior
    procedures are read by their code system wherever the coding sits: the fictional procedure
    codes and ICD-10-CM. With none of the pack's systems, the order's, a diagnosis's and a
    procedure's first coding is read as before (a site's own code, which
    `logic/structured.yaml` matches once the site lists it). An order is the pack's when one of
    its codings is a fictional code the pack has a requirement set for, wherever it sits (the
    0.2 mapping read the first coding only); one with none is not, as before.
  - The payer is read through the Coverage's first payor: the Organization it names
    (contained, in the record, or read from the EHR on a CDS hook or the SMART launch), by its
    identifier in a new, fictional payer identifier system the pack declares
    (`https://mendari.co/fhir/sid/fictional-payers`); else the payor's own identifier as the
    Coverage writes it, which is how the synthetic records name their payers and all the 0.2
    mapping read. A payer named by reference read as `other` before. A site's own payer
    identifier is neither, and no site code map translates an identifier (codings only), so it
    still reads as `other` and the order as `not_checked`: how a payer field is to read a
    site's payer id is the owner's open question. A payor reference that could not be read
    (`fhir/decide` and `$decide` never read one) makes the decision abstain (`could not read
    coverage.payer: <reason>`), where it read as `other` before.
  - Notes are read through `attachment_text`: text/plain and text/html, inline or in the
    Binary the attachment's url names. An RTF, PDF or CDA note, or one that cannot be read,
    makes the decision abstain; an inline note with no content type is now unreadable, and an
    inline HTML note is read as its text.
- Every case of the FHIR decisions golden (the bundled local-site example through FHIR, 300
  cases, 41 of them refused for an order with no requirement set) is decided as before, input
  hash included. On the vendor-shaped EHR profile, the patients whose linked note now reads are
  decided `not_checked`, since their payer is the site's own payer id: no card and no coverage
  information, where they abstained before. Inferno's CRD server suite, which runs the pack on
  those patients, says so (`sandbox/inferno/crd_server_v221_expected.json`: 36 pass, 5 fail,
  12 skip, where 0.2.1 gave 34, 11 and 8).
- Criteria, rules, providers, policy, models and data are unchanged; the benchmark and its
  ledger differ only in the run, the version and the digest. The digest changes because the
  manifest and the mapping do.

## 0.2.1

- The local-site known failure mode states its finding and the command that measures it,
  instead of percentages typed in by hand (LP-10: a share of cases on a card is k of n with
  its interval, computed). Criteria, rules, providers, policy, models and data are unchanged;
  every decision is identical. The digest changes because the manifest does.

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
