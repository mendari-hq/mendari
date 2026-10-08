# Changelog: mendari/clinical-trial-prescreener

Renamed on 2026-09-30, when the project became Mendari: this pack was `openhealth/clinical-trial-prescreener`.
Its id, its publisher's name and its `https://mendari.co/...` identifiers changed; its criteria,
rules, models, data and results did not.

## 0.2.0

- No SNOMED CT: the five SNOMED CT codes the pack's criteria, CQL library and criterion text
  listed beside ICD-10-CM are replaced one for one by invented codes (`FC101` type 2 diabetes,
  `FC102` type 1 diabetes, `FC201` myocardial infarction, `FC202` stroke, `FC203` cerebral
  infarction) in a code system named as fictional,
  `https://mendari.co/fhir/CodeSystem/fictional-conditions`, and so are the codes of the bundled
  local-site example (`datasets/trial-local-site-example.jsonl`), which plays a site that codes
  conditions in its own system. SNOMED CT's licence terms are a question for counsel (LP-14),
  and no package carries its codes any more. The study's condition (`fhir/research-study.json`)
  is ICD-10-CM E11.
  **The pack now reads real diagnoses only through ICD-10-CM.** Until a site code map exists
  (LP-23), a diagnosis coded only in SNOMED CT is not found (type 2 diabetes `unknown`, a heart
  attack or stroke missed): a site that codes in SNOMED CT adds its codes to
  `logic/criteria.yaml` and `logic/criteria.cql`, as it already adds any code the pack does not
  list. The known failure modes say so.
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
