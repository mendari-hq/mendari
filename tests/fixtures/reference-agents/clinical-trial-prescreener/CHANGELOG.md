# Changelog: mendari/clinical-trial-prescreener

Renamed on 2026-09-30, when the project became Mendari: this pack was `openhealth/clinical-trial-prescreener`.
Its id, its publisher's name and its `https://mendari.co/...` identifiers changed; its criteria,
rules, models, data and results did not.

## 0.4.0

- **The medication rule, with the owner's changes** (B21). The owner, a physician, answered the
  clinician brief `notes/clinician-review-medication-period.md` on 2026-11-07: the rule is
  "acceptable with changes". This version builds those changes. A MINOR bump, by
  CONTRIBUTING.md's rule (MINOR for a change in behaviour): labels are unchanged, and what the
  pack decides changes for the medications below.
  - **An active medication with no end date counts as current only when it started in the 365
    days before screening** (the owner's answers 2 and 7). `stable_metformin` and `no_insulin`
    carry `current_within_days: 365` in `logic/criteria.yaml` (a spec 0.3 key, §3): an active
    medication, from a MedicationRequest or a MedicationStatement, with no end date and a start
    more than 365 days before screening, or no start, reads `unknown`, so the chart goes to
    review. The 365th day is inside. Metformin started 90 days to 12 months ago still reads
    "yes". An insulin with no start that read "no" (on insulin) now reads `unknown`. The CQL
    library (`logic/criteria.cql`, now version 0.4.0) states the same rule, and agrees with
    `criteria` on every test and dataset case (584, and 178 on the local-site example, on HAPI
    FHIR 8.12).
  - **Through FHIR, a MedicationRequest that is not an order to give the drug is left out**
    (answer 5): its `intent` is `proposal`, `plan` or `option`, or its `doNotPerform` is true.
  - **A `boundsDuration` gives the stop** (answer 6): when the first dosage instruction has no
    `boundsPeriod.end` and the start is a full date, the stop is the start's day plus the
    duration in whole days, rounded up, in UCUM's `h`, `d`, `wk`, `mo` (30.4375 days) or `a`
    (365.25 days). Any other unit leaves the stop unknown. So a `completed` insulin written 60
    days ago for 30 days reads as ended 30 days ago (`no_insulin` "no"), where its stop was
    unknown (`no_insulin` "yes").
  - Unchanged, as the owner chose: the start (answer 1); a medication that is not `active` with
    no end date reads as not taken (answer 3); renewals are separate items, the first matching
    one in the record's order deciding (answer 4); a request entered in error is left out;
    `dispenseRequest.validityPeriod` is not read; only the first dosage instruction is read.
  - The card's known failure modes state the rule and the owner's answer. They also state one
    thing the brief did not ask: an `active` medication with an end date counts as current
    whatever that date, so an active request written two years ago for 30 days reads as
    metformin taken for two years.
- **The synthetic data's ground truth follows the rule**; no record changes. Of the benchmark's
  300 patients, 192 have active metformin started more than 365 days before screening with no
  end date, and 23 active insulin started 400 days before (the generator's one start for it):
  those criteria now read `unknown` in the truth and in the pack. Eligibility is answered right
  for 298 of 300, as before, but 47 of the 56 candidates and 10 of the 208 not eligible are now
  `needs_review` (candidates 9, needs review 93, not eligible 198). Every critical label's
  figure is as it was.
- **The hand-written exclusion sets and the behaviour tests' base record** had metformin since
  2025-03-01, 18 months before their screening date, which the rule reads `unknown`. Its start
  is now 2026-03-01 (6 months), so each case's eligibility still turns on its notes, as the sets
  were written for. No note or label changes. Six behaviour tests are new: metformin started 366
  days and exactly 365 days ago, metformin with an end date still to come, and insulin started
  in the last year, more than a year ago, and with no dates.
- **The FHIR decisions golden**: 91 of the 150 local-site cases change an answer, their input
  hashes unchanged (their medications are MedicationStatements, with no intent or duration). 22
  change their result: 21 `candidate` and 1 `not_eligible` become `needs_review`.
- The digest changes because the manifest, the criteria, the CQL library, the mapping, the data
  and the tests do; the benchmark and its ledger are run again.

## 0.3.0

- **Through FHIR, the pack reads EHR data on spec 0.3** (LP-23k, after the owner's go-ahead on
  2026-11-06; `spec_version: "0.3"`, `fhir/mapping.yaml` only). A MINOR bump, by CONTRIBUTING.md's
  rule (MINOR for a change in behaviour): what the pack decides through FHIR changes where the 0.2
  mapping misread the data, and nothing else does.
  - A Condition is read by its ICD-10-CM coding wherever it sits among its codings, then by the
    fictional condition code, and only when it has neither, by its first coding as before (a
    site's own code, which `logic/criteria.yaml` matches once the site lists it). So a diagnosis
    with a site's coding before its ICD-10-CM one is found: the first-coding miss the 0.2.0
    entry describes, which the owner accepted this selector would fix (2026-10-08). A lab is
    read by its LOINC coding, else its first; a MedicationStatement's drug by its RxNorm coding
    only.
  - Medications come from MedicationRequest too, with a new CDS Hooks prefetch key
    (`medication_requests: MedicationRequest?patient={{context.patientId}}`). Its drug is the
    Medication it names (contained, in the record, or read from the EHR on a CDS hook or the
    SMART launch), else its own coded concept; its name the Medication's text, else the
    concept's, else the reference's display. When it is taken is spec 0.3's stated rule (§2.7):
    from the first dosage instruction's `timing.repeat.boundsPeriod.start`, else `authoredOn`,
    to that `boundsPeriod.end`, else an unknown stop, never inferred from the status; the
    dispensing window is not read, and a request entered in error is left out. **The rule is not
    yet clinically reviewed** (a pharmacist's or research coordinator's review is owed, B21);
    the card's known failure modes say so, and what follows from it (a renewal read as a recent
    start, `intent` and `doNotPerform` not read; a request that is not `active` and has no
    bounds end read as not taken, since the criteria count only `active` as current, so a
    `completed` insulin written 20 days ago reads `no_insulin` "yes"; the first matching request
    in the record's order deciding). A drug named by a reference that could not be read makes
    the decision abstain (`could not read medications.code: <reason>`).
  - Notes are read through `attachment_text`: text/plain and text/html, inline or in the
    Binary the attachment's url names, read from the EHR on a CDS hook or the SMART launch. An
    RTF, PDF or CDA note, or one that cannot be read, makes the decision abstain, and so do notes
    longer than 20,000 characters in all, which the input schema does not allow (`could not read
    notes: too_large`; a long inline note was refused before as invalid input, with no card), and
    more links than a decision reads (20, `cap`: each linked note's Binary and each Medication a
    MedicationRequest names). Two smaller changes: an inline note with no content type (R4's att-1
    requires one with data) is now unreadable, where it was decoded as text; and an inline HTML note
    is read as its text, its markup dropped.
  - A CDS client that sends a prefetch without the new key and no `fhirServer` now gets the
    `412` every left-out key gets: an EHR sends it (null for no data), as discovery lists it.
- Every case of the FHIR decisions golden (the bundled local-site example through FHIR, 150
  cases) is decided as before, input hash included: the synthetic records carry each code in
  the pack's own system, their medications are MedicationStatements and their notes inline
  text. On the vendor-shaped EHR profile (`tests/test_ehr_profile.py`) the patients with a
  text/plain or text/html note are now decided on it, and only the one with an RTF note abstains.
- Criteria, rules, providers, policy, models and data are unchanged; the benchmark and its
  ledger differ only in the run, the version and the digest. The digest changes because the
  manifest and the mapping do.

## 0.2.0

- No SNOMED CT: the five SNOMED CT codes the pack's criteria, CQL library and criterion text
  listed beside ICD-10-CM are replaced one for one by invented codes (`FC101` type 2 diabetes,
  `FC102` type 1 diabetes, `FC201` myocardial infarction, `FC202` stroke, `FC203` cerebral
  infarction) in a code system named as fictional,
  `https://mendari.co/fhir/CodeSystem/fictional-conditions`, and so are the codes of the bundled
  local-site example (`datasets/trial-local-site-example.jsonl`), which plays a site that codes
  conditions in its own system. Its strokes, whose text says cerebral infarction, carry `FC203`,
  so each code's display in the published CodeSystem is what the record says. SNOMED CT's
  licence terms are a question for counsel (LP-14), and no package carries its codes any more.
  The study's condition (`fhir/research-study.json`) is ICD-10-CM E11.
  **The pack now reads real diagnoses only through ICD-10-CM, and through FHIR only from a
  Condition's first coding** (`code.coding[0]`, as before). Until a site code map exists
  (LP-23), a diagnosis coded only in SNOMED CT is not found, and neither is one whose first
  coding is SNOMED CT even when an ICD-10-CM coding follows it, which 0.1.5 found through the
  SNOMED CT code: type 2 diabetes is `unknown`, a heart attack or stroke is missed. A site that
  codes in SNOMED CT adds its codes to `logic/criteria.yaml` and `logic/criteria.cql`, as it
  already adds any code the pack does not list. The known failure modes say so.
- `code_systems` drops SNOMED CT and declares the fictional conditions and RxNorm (the
  medication codes the pack's data uses; public domain, NLM's acknowledgement in NOTICE).
- The CQL library is `GlycemiaTwoCriteria` 0.2.0 (its codes changed).
- Every decision is the same as in 0.1.5 with the codes swapped: each answer, probability,
  rule and outcome of the 728 data, dataset and behaviour-test cases, compared case by case.
  The data and the local-site example were rebuilt from the generator: only the example's codes
  changed. The digest changes.

## 0.1.5

- Two known failure modes (the local site, and rules added to a model) state their findings,
  and the command that measures the first, instead of percentages typed in by hand (LP-10: a
  share of cases on a card is k of n with its interval, computed). Criteria, rules, providers,
  policy, models and data are unchanged; every decision is identical. The digest changes
  because the manifest does.

## 0.1.4

- `code_systems` declares the systems the pack uses: ICD-10-CM, LOINC and SNOMED CT, with SNOMED
  CT's licence terms (a pack naming a licensed code system now has to say them).
- Criteria, rules, providers, policy, models and data are unchanged; every decision is identical.
  The digest changes because the manifest does.

## 0.1.3

- Every surface now says what the pack is: "Synthetic example. Not clinically reviewed. Not for
  patient care." It is derived from the new `card.provenance: synthetic`, never written by the
  pack (the Decision Card's first line, the dashboard, Try it, the review queue, CDS Hooks cards
  and discovery, the registry and the Hub).
- Each question has a `display` name, so people read words instead of keys.
- Criteria, rules, providers, policy, models and data are unchanged; every decision is identical.
  The digest changes because the manifest does.

## 0.1.2

- A `cql` variant of the `structured` slot: the seven criteria written in CQL
  (`logic/criteria.cql`), evaluated on a CQL engine (`$MENDARI_CQL_URL`, e.g. HAPI FHIR with
  Clinical Reasoning). It agrees with `criteria` on every test and dataset case, which CI
  checks. `criteria` stays the default. Questions, criteria, rules, policy and data are
  unchanged.

## 0.1.1

- Decision Card: deployment guidance from the provider comparison (evals/leaderboard.json,
  measured on 0.1.0). Deploy with a model reading the notes (`notes=jev` or `notes=llm`);
  adding the keyword rules to a model lowered eligibility accuracy without finding more
  exclusions. Questions, criteria, rules, policy and data are unchanged.

## 0.1.0

- First release. Prescreens patients for GLYCEMIA-2, a synthetic type 2 diabetes study.
- Seven structured criteria computed in code by the `criteria` provider (age, diagnosis,
  HbA1c, metformin, eGFR, insulin, coded heart attack or stroke). Missing, stale or
  unrecognised data gives `unknown`.
- Four exclusions read from the clinic notes (pregnancy or breastfeeding, pancreatitis, cancer
  treatment, recent cardiovascular event), with English keyword rules as the offline default
  and `jev`, `llm`, `rules_and_jev` and `rules_and_llm` variants.
- `eligibility` is derived from all eleven: `candidate`, `not_eligible` or `needs_review`, with
  the deciding criteria named in the decision's reasons.
- Risk level 2: every result is reviewed by a person. Possible pregnancy and possible recent
  cardiovascular events are flagged for review at P(yes) >= 0.3.
- FHIR mapping (Patient, Condition, Observation, MedicationStatement, DocumentReference), a
  synthetic ResearchStudy, and a CDS Hooks `patient-view` service.
