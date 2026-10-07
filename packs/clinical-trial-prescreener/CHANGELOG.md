# Changelog: mendari/clinical-trial-prescreener

Renamed on 2026-09-30, when the project became Mendari: this pack was `openhealth/clinical-trial-prescreener`.
Its id, its publisher's name and its `https://mendari.co/...` identifiers changed; its criteria,
rules, models, data and results did not.

## 0.1.5

- Known failure modes state their findings and the command that measures them, no longer
  figures typed in by hand (LP-10: every figure on a card is computed). Criteria, rules,
  providers, policy, models and data are unchanged; every decision is identical. The digest
  changes because the manifest does.

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
