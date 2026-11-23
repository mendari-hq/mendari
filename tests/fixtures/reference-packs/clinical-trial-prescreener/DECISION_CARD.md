# Decision Card: Clinical Trial Prescreener

**Synthetic example. Not clinically reviewed. Not for patient care.**

*Research preview. Not a medical device. Not for emergencies: patients should call 911.*

`mendari/clinical-trial-prescreener` · version **0.4.0** · Apache-2.0 · published by Mendari reference packs (community, self-declared)

> Checks a patient's record against the eligibility criteria of a synthetic type 2 diabetes study (GLYCEMIA-2): structured criteria are computed from coded data, and exclusions are looked for in the clinic notes. Every result goes to a research coordinator for review.

| | |
|---|---|
| Source | Maintained by Mendari |
| Risk level | **2**, clinical support |
| Automation | Human review required by default; AUTO only with a recorded governance override. |
| Agent digest | `sha256:1f6da0fd13bb44a4a632e3e1092cb0c97fac3914e7100fd54ff0991299f15b18` |
| Last validation | 2026-11-07 on `test` (n=300, sha256 `fd328b4fe0d0…`) |

## Intended use

Prescreening patients for a clinical study when their chart is opened or in a batch run: shows which eligibility criteria are met, not met or undetermined, so a research coordinator can decide whom to review first. GLYCEMIA-2 is a synthetic study written for this reference pack; it is not a real protocol.

**Why risk level 2:** The pack suggests whether a patient may be eligible for a study and lists the criteria behind the suggestion. It never contacts patients or enrols anyone. A wrong suggestion can lead a coordinator to approach an ineligible patient or to miss an eligible one, so every result is reviewed by a person, and possible pregnancy or a possible recent cardiovascular event is always flagged for review.

## Intended users

- Research coordinators and study nurses
- Investigators reviewing prescreening lists
- Health IT and research informatics teams integrating prescreening with the EHR

## Patient population

Adults with diabetes in outpatient care whose record has coded conditions, lab results and medications, and English clinic notes.

## Out of scope

- Enrolling patients or confirming eligibility. A person always confirms eligibility against the full protocol and the source documents before any contact.
- Any real study. The criteria are synthetic and were not written or reviewed by a clinician; a real study needs its own criteria, reviewed by its investigators.
- Notes in languages other than English with the default `rules` provider.

## Inputs

| Field | Type | Required |
|---|---|---|
| `as_of` | string | no |
| `patient` | object | no |
| `patient.age` | integer | no |
| `patient.sex` | `female` \| `male` \| `other` \| `unknown` | no |
| `conditions` | array | no |
| `observations` | array | no |
| `medications` | array | no |
| `notes` | string | no |

FHIR R4 mapping: `fhir/mapping.yaml`.

Code systems:

- ICD-10-CM (U.S. National Center for Health Statistics: free to use; adapted from the World Health Organization's copyrighted ICD-10, with WHO's authorization, for U.S. government purposes) (`http://hl7.org/fhir/sid/icd-10-cm`).
- Mendari fictional condition codes: invented for the synthetic local-site example; not SNOMED CT. A site maps its own codes to these. (`https://mendari.co/fhir/CodeSystem/fictional-conditions`).
- LOINC (Regenstrief Institute; the LOINC License, whose notice is in NOTICE) (`http://loinc.org`).
- RxNorm (U.S. National Library of Medicine: its codes and normalized names are in the public domain; NLM's acknowledgement is in NOTICE) (`http://www.nlm.nih.gov/research/umls/rxnorm`).

## Outputs

**`age_18_75`**: Is the patient aged 18 to 75 on the screening date?

| Label | Criteria |
|---|---|
| `yes` | Aged 18 to 75 inclusive. |
| `no` | Younger than 18 or older than 75. |
| `unknown` | Age is not recorded. |

**`t2d_diagnosis`**: Does the record contain a coded diagnosis of type 2 diabetes?

| Label | Criteria |
|---|---|
| `yes` | Type 2 diabetes is coded (ICD-10-CM E11, or the local-site example's fictional FC101). |
| `no` | Type 1 diabetes is coded and type 2 diabetes is not. |
| `unknown` | No diabetes diagnosis is coded. |

**`hba1c_in_range`**: Was the most recent HbA1c in the 90 days before screening between 7.0 % and 10.5 %?

| Label | Criteria |
|---|---|
| `yes` | The latest HbA1c in the window is 7.0 % to 10.5 % inclusive. |
| `no` | The latest HbA1c in the window is below 7.0 % or above 10.5 %. |
| `unknown` | No HbA1c in the last 90 days, or one with an unrecognised code or unit. |

**`stable_metformin`**: Has the patient been taking metformin for at least 90 days before screening?

| Label | Criteria |
|---|---|
| `yes` | Metformin is active and was started at least 90 days before screening (and, with no end date, at most 365 days before). |
| `no` | Metformin is not listed, was stopped, or was started less than 90 days ago. |
| `unknown` | Metformin is active but has no start date, or has no end date and was started more than 365 days before screening. |

**`egfr_30_or_more`**: Was the most recent eGFR in the 365 days before screening at least 30 mL/min/1.73m2?

| Label | Criteria |
|---|---|
| `yes` | The latest eGFR in the window is 30 or more. |
| `no` | The latest eGFR in the window is below 30. |
| `unknown` | No eGFR in the last 365 days, or one in an unrecognised unit. |

**`no_insulin`**: Has the patient been off insulin for the 90 days before screening?

| Label | Criteria |
|---|---|
| `yes` | No insulin is listed, or it ended more than 90 days before screening. |
| `no` | Insulin is active (with an end date, or started in the 365 days before screening), or ended in the last 90 days. |
| `unknown` | Insulin is active with no end date and has no start date or was started more than 365 days before screening. |

**`no_recent_cv_event`**: Is the record free of a coded heart attack or stroke with onset in the 180 days before screening?

| Label | Criteria |
|---|---|
| `yes` | No heart attack or stroke is coded, or only ones with onset more than 180 days ago. |
| `no` | A heart attack or stroke is coded with onset in the last 180 days. |
| `unknown` | A heart attack or stroke is coded without an onset date. |

**`note_pregnancy`** *(boolean)*: Do the notes say that the patient is pregnant or breastfeeding now?

| Label | Criteria |
|---|---|
| `yes` | The patient is pregnant now (a confirmed pregnancy, weeks of gestation, or an expected delivery) or is breastfeeding now. |
| `no` | Anything else: no mention, a negative pregnancy test, a past pregnancy, plans to become pregnant, menopause, or someone else's pregnancy, such as a partner's. |

**`note_pancreatitis`** *(boolean)*: Do the notes say that the patient has ever had pancreatitis?

| Label | Criteria |
|---|---|
| `yes` | The patient has had acute or chronic pancreatitis, at any time. |
| `no` | No mention, pancreatitis denied or ruled out, or pancreatitis in a family member. |

**`note_active_cancer_treatment`** *(boolean)*: Do the notes say that the patient is receiving treatment for cancer now?

| Label | Criteria |
|---|---|
| `yes` | Chemotherapy, radiation, immunotherapy, targeted or hormone therapy for cancer is ongoing, or has started and not finished. |
| `no` | No mention, cancer in remission, treatment finished, surveillance only, a cancer that needed no further treatment, or cancer in a family member. |

**`note_recent_cv_event`** *(boolean)*: Do the notes say that the patient had a heart attack, stroke or acute coronary syndrome in the 6 months before the screening date?

| Label | Criteria |
|---|---|
| `yes` | The patient had a heart attack (including NSTEMI or STEMI), a stroke or an acute coronary syndrome in the 6 months before the screening date. |
| `no` | No mention, an event more than 6 months before the screening date, symptoms that were found not to be a heart attack or stroke, or an event in a family member. |

**`eligibility`** (primary): What is the prescreen result for the GLYCEMIA-2 study?

Derived from `age_18_75`, `t2d_diagnosis`, `hba1c_in_range`, `stable_metformin`, `egfr_30_or_more`, `no_insulin`, `no_recent_cv_event`, `note_pregnancy`, `note_pancreatitis`, `note_active_cancer_treatment`, `note_recent_cv_event` by a decision table (the first matching row wins; the members' answers are treated as independent):

1. any of `age_18_75`, `t2d_diagnosis`, `hba1c_in_range`, `stable_metformin`, `egfr_30_or_more`, `no_insulin`, `no_recent_cv_event` is `no` → `not_eligible`
2. any of `note_pregnancy`, `note_pancreatitis`, `note_active_cancer_treatment`, `note_recent_cv_event` is `yes` → `not_eligible`
3. any member is `unknown` → `needs_review`
4. otherwise → `candidate`

| Label | Criteria |
|---|---|
| `candidate` | Every structured criterion is met and the notes document no exclusion. A research coordinator confirms eligibility before any contact. |
| `not_eligible` | A structured criterion is not met, or the notes document an exclusion. |
| `needs_review` | No criterion fails, but at least one cannot be determined from the record. |

Every decision is one of `AUTO`, `HUMAN_REVIEW`, `ABSTAIN` or `BLOCK`, with a priority (`routine`, `urgent`, `asap`, `stat`) and an audit record.

## Decision policy

- AUTO when `eligibility` confidence ≥ **0.95**; human review ≥ **0.40**; abstain below.
- `candidate` is never automated.
- Rule **possible-pregnancy**: P(`note_pregnancy` is `yes`) ≥ 0.30 ⇒ `HUMAN_REVIEW`. The notes may document a current pregnancy or breastfeeding. Confirm before any study contact.
- Rule **possible-recent-cardiovascular-event**: P(`note_recent_cv_event` is `yes`) ≥ 0.30 ⇒ `HUMAN_REVIEW`. The notes may document a heart attack, stroke or acute coronary syndrome in the last 6 months. Confirm the date in the chart.
- Fallback (ABSTAIN / BLOCK): Manual chart review by the research coordinator (the existing prescreening workflow).

## Providers

| Slot | Answers | Default | Alternatives |
|---|---|---|---|
| `structured` | age_18_75, t2d_diagnosis, hba1c_in_range, stable_metformin, egfr_30_or_more, no_insulin, no_recent_cv_event | `criteria` (criteria) | `cql` (cql) |
| `notes` | note_pregnancy, note_pancreatitis, note_active_cancer_treatment, note_recent_cv_event | `rules` (rules) | `jev` (jev), `llm` (llm), `rules_and_jev` (any_of), `rules_and_llm` (any_of) |

## Validation

Benchmark `test` (n=300) with structured=`criteria`, notes=`rules`. Each proportion: k of n [exact 95% interval, Clopper-Pearson].

| Metric | Value |
|---|---|
| `eligibility` accuracy | 298 of 300 [97.6-99.9%] |
| Macro-F1 | 0.977 |
| Expected calibration error | 0.083 |
| Expected calibration error, equal-mass bins | 0.083 |
| Brier score | 0.048 [0.035-0.066, bootstrap] |
| AUTO | 0 of 300 [0.0-1.2%] |
| HUMAN_REVIEW | 300 of 300 [98.8-100.0%] |
| ABSTAIN | 0 of 300 [0.0-1.2%] |
| BLOCK | 0 of 300 [0.0-1.2%] |
| AUTO decisions correct | no cases |
| False automation (of labelled cases) | 0 of 300 [0.0-1.2%] |
| Expected human override (of reviewed) | 2 of 300 [0.1-2.4%] |
| Latency p50 / p95 | 0.44 / 1.30 ms |
| Cost per decision | $0.00000 |
| Critical `note_pregnancy=yes` | 12 of 13 [64.0-99.8%] detected, 0 automated (insufficient evidence: under 100 positives) |
| Critical `note_pancreatitis=yes` | 6 of 6 [54.1-100.0%] detected, 0 automated (insufficient evidence: under 100 positives) |
| Critical `note_active_cancer_treatment=yes` | 16 of 17 [71.3-99.9%] detected, 0 automated (insufficient evidence: under 100 positives) |
| Critical `note_recent_cv_event=yes` | 13 of 13 [75.3-100.0%] detected, 0 automated (insufficient evidence: under 100 positives) |
| Critical `egfr_30_or_more=no` | 29 of 29 [88.1-100.0%] detected, 0 automated (insufficient evidence: under 100 positives) |
| Critical `no_recent_cv_event=no` | 17 of 17 [80.5-100.0%] detected, 0 automated (insufficient evidence: under 100 positives) |

Per-label performance:

| Label | Precision | Recall | F1 |
|---|---|---|---|
| `candidate` | 8 of 8 [63.1-100.0%] | 8 of 9 [51.8-99.7%] | 0.941 |
| `not_eligible` | 197 of 198 [97.2-99.99%] | 197 of 198 [97.2-99.99%] | 0.995 |
| `needs_review` | 93 of 94 [94.2-99.97%] | 93 of 93 [96.1-100.0%] | 0.995 |

Subgroups:

| Subgroup | n | Accuracy | AUTO | Abstention |
|---|---|---|---|---|
| site=public | 300 | 298 of 300 [97.6-99.9%] | 0 of 300 [0.0-1.2%] | 0 of 300 [0.0-1.2%] |
| language=en | 286 | 286 of 286 [98.7-100.0%] | 0 of 286 [0.0-1.3%] | 0 of 286 [0.0-1.3%] |
| language=es | 14 | 12 of 14 [57.2-98.2%] | 0 of 14 [0.0-23.2%] | 0 of 14 [0.0-23.2%] |
| sex=female | 148 | 147 of 148 [96.3-99.98%] | 0 of 148 [0.0-2.5%] | 0 of 148 [0.0-2.5%] |
| sex=male | 152 | 151 of 152 [96.4-99.98%] | 0 of 152 [0.0-2.4%] | 0 of 152 [0.0-2.4%] |
| age_band=18-39 | 68 | 67 of 68 [92.1-99.96%] | 0 of 68 [0.0-5.3%] | 0 of 68 [0.0-5.3%] |
| age_band=40-64 | 154 | 154 of 154 [97.6-100.0%] | 0 of 154 [0.0-2.4%] | 0 of 154 [0.0-2.4%] |
| age_band=65+ | 69 | 68 of 69 [92.2-99.96%] | 0 of 69 [0.0-5.2%] | 0 of 69 [0.0-5.2%] |
| age_band=missing | 9 | 9 of 9 [66.4-100.0%] | 0 of 9 [0.0-33.6%] | 0 of 9 [0.0-33.6%] |

Calibration (reliability):

| Confidence bin | Mean confidence | Correct |
|---|---|---|
| 0.7 to 0.8 | 0.760 | 101 of 102 [94.7-99.98%] |
| 0.9 to 1.0 | 0.988 | 197 of 198 [97.2-99.99%] |

Threshold sweep (if everything ≥ threshold were automated):

| Threshold | Would be automated | Of those, correct |
|---|---|---|
| 0.50 | 300 of 300 [98.8-100.0%] | 298 of 300 [97.6-99.9%] |
| 0.60 | 300 of 300 [98.8-100.0%] | 298 of 300 [97.6-99.9%] |
| 0.70 | 300 of 300 [98.8-100.0%] | 298 of 300 [97.6-99.9%] |
| 0.80 | 198 of 300 [60.3-71.3%] | 197 of 198 [97.2-99.99%] |
| 0.85 | 198 of 300 [60.3-71.3%] | 197 of 198 [97.2-99.99%] |
| 0.90 | 198 of 300 [60.3-71.3%] | 197 of 198 [97.2-99.99%] |
| 0.95 | 198 of 300 [60.3-71.3%] | 197 of 198 [97.2-99.99%] |
| 0.97 | 183 of 300 [55.2-66.6%] | 183 of 183 [98.0-100.0%] |
| 0.99 | 72 of 300 [19.3-29.2%] | 72 of 72 [95.0-100.0%] |

## Provider comparison

Public set: regression check, not a ranking. Nothing here is graded: the agent's own sets are public, in the repository and in the package, so a configuration can be tuned to them, and grades and rankings are given only on frozen hidden sets. The rows are a listing order, not a ranking: those measured on this version of the agent, on records where a critical label has at least 100 positives, come first, the critical cases caught first and never accuracy alone; every other row follows, by configuration name.

Each table below is one dataset at one size, and the configurations in a table that records the dataset's hash ran on exactly the same records. Remote models are named because they are a deployment choice. Synthetic and hand-written data: compare configurations with each other, not with performance at a real site. A row measured on an earlier version of the agent says so beside its configuration: its AUTO and false automation followed that version's policy.

**`exclusions`** (n=32; dataset hash not recorded; k of n [exact 95% interval, Clopper-Pearson])

| Configuration | Critical detected | False automation | Accuracy | AUTO | Macro-F1 | ECE | p50 latency | Cost / decision | Errors |
|---|---|---|---|---|---|---|---|---|---|
| structured=criteria · notes=jev (jev-1.13.0) (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 5 of 5 [47.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 32 [0.0-10.9%] | 1.000 | 0.137 | 201 ms | $0.00003 | 0 |
| structured=criteria · notes=llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 5 of 5 [47.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 32 [0.0-10.9%] | 1.000 | 0.081 | 3,957 ms | $0.00376 | 0 |
| structured=criteria · notes=llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 4 of 5 [28.4-99.5%] (0 automated; insufficient evidence); note_pancreatitis=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 32 [0.0-10.9%] | 0.968 | 0.148 | 2,325 ms | $0.00405 | 0 |
| structured=criteria · notes=llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 5 of 5 [47.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 32 [0.0-10.9%] | 1.000 | 0.100 | 1,244 ms | $0.00017 | 0 |
| structured=criteria · notes=rules (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 5 of 5 [47.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 32 [0.0-10.9%] | 1.000 | 0.169 | 0.30 ms | $0.00000 | 0 |
| structured=criteria · notes=rules_and_jev (jev-1.13.0) (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 5 of 5 [47.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 32 [0.0-10.9%] | 1.000 | 0.166 | 219 ms | $0.00003 | 0 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 5 of 5 [47.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 32 [0.0-10.9%] | 1.000 | 0.162 | 4,477 ms | $0.00384 | 0 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 5 of 5 [47.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 32 [0.0-10.9%] | 1.000 | 0.168 | 2,294 ms | $0.00404 | 0 |
| structured=criteria · notes=rules_and_llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 5 of 5 [47.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 32 [0.0-10.9%] | 1.000 | 0.163 | 1,145 ms | $0.00017 | 0 |

Accuracy and false automation are n/a on a row recorded before leaderboards kept the counts they are of (mendari.leaderboard/1).

Other questions (accuracy / ECE):

| Configuration | `note_active_cancer_treatment` | `note_pancreatitis` | `note_pregnancy` | `note_recent_cv_event` |
|---|---|---|---|---|
| structured=criteria · notes=jev (jev-1.13.0) (version 0.1.0, its agent digest not recorded) | 32 of 32 [89.1-100.0%] / 0.027 | 32 of 32 [89.1-100.0%] / 0.028 | 32 of 32 [89.1-100.0%] / 0.022 | 32 of 32 [89.1-100.0%] / 0.030 |
| structured=criteria · notes=llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | 32 of 32 [89.1-100.0%] / 0.005 | 32 of 32 [89.1-100.0%] / 0.003 | 32 of 32 [89.1-100.0%] / 0.004 | 32 of 32 [89.1-100.0%] / 0.004 |
| structured=criteria · notes=llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | 32 of 32 [89.1-100.0%] / 0.023 | 32 of 32 [89.1-100.0%] / 0.021 | 31 of 32 [83.8-99.9%] / 0.047 | 32 of 32 [89.1-100.0%] / 0.023 |
| structured=criteria · notes=llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | 32 of 32 [89.1-100.0%] / 0.009 | 32 of 32 [89.1-100.0%] / 0.009 | 32 of 32 [89.1-100.0%] / 0.012 | 32 of 32 [89.1-100.0%] / 0.010 |
| structured=criteria · notes=rules (version 0.1.0, its agent digest not recorded) | 32 of 32 [89.1-100.0%] / 0.046 | 32 of 32 [89.1-100.0%] / 0.047 | 32 of 32 [89.1-100.0%] / 0.044 | 32 of 32 [89.1-100.0%] / 0.043 |
| structured=criteria · notes=rules_and_jev (jev-1.13.0) (version 0.1.0, its agent digest not recorded) | 32 of 32 [89.1-100.0%] / 0.046 | 32 of 32 [89.1-100.0%] / 0.044 | 32 of 32 [89.1-100.0%] / 0.042 | 32 of 32 [89.1-100.0%] / 0.043 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | 32 of 32 [89.1-100.0%] / 0.044 | 32 of 32 [89.1-100.0%] / 0.041 | 32 of 32 [89.1-100.0%] / 0.041 | 32 of 32 [89.1-100.0%] / 0.043 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | 32 of 32 [89.1-100.0%] / 0.046 | 32 of 32 [89.1-100.0%] / 0.046 | 32 of 32 [89.1-100.0%] / 0.044 | 32 of 32 [89.1-100.0%] / 0.043 |
| structured=criteria · notes=rules_and_llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | 32 of 32 [89.1-100.0%] / 0.044 | 32 of 32 [89.1-100.0%] / 0.042 | 32 of 32 [89.1-100.0%] / 0.041 | 32 of 32 [89.1-100.0%] / 0.043 |

**`exclusions_holdout`** (n=24; dataset hash not recorded; k of n [exact 95% interval, Clopper-Pearson])

| Configuration | Critical detected | False automation | Accuracy | AUTO | Macro-F1 | ECE | p50 latency | Cost / decision | Errors |
|---|---|---|---|---|---|---|---|---|---|
| structured=criteria · notes=jev (jev-1.13.0) (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 24 [0.0-14.2%] | 1.000 | 0.141 | 206 ms | $0.00003 | 0 |
| structured=criteria · notes=llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 24 [0.0-14.2%] | 1.000 | 0.074 | 5,141 ms | $0.00399 | 0 |
| structured=criteria · notes=llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 24 [0.0-14.2%] | 1.000 | 0.121 | 2,304 ms | $0.00403 | 0 |
| structured=criteria · notes=llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 24 [0.0-14.2%] | 1.000 | 0.095 | 1,193 ms | $0.00017 | 0 |
| structured=criteria · notes=rules (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 1 of 2 [1.3-98.7%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 1 of 3 [0.8-90.6%] (0 automated; insufficient evidence); note_recent_cv_event=yes 2 of 3 [9.4-99.2%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 24 [0.0-14.2%] | 0.791 | 0.060 | 0.30 ms | $0.00000 | 0 |
| structured=criteria · notes=rules_and_jev (jev-1.13.0) (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 24 [0.0-14.2%] | 0.916 | 0.185 | 229 ms | $0.00003 | 0 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 24 [0.0-14.2%] | 0.916 | 0.165 | 4,499 ms | $0.00388 | 0 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 24 [0.0-14.2%] | 0.916 | 0.177 | 2,312 ms | $0.00403 | 0 |
| structured=criteria · notes=rules_and_llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 24 [0.0-14.2%] | 0.916 | 0.169 | 1,127 ms | $0.00017 | 0 |

Accuracy and false automation are n/a on a row recorded before leaderboards kept the counts they are of (mendari.leaderboard/1).

Other questions (accuracy / ECE):

| Configuration | `note_active_cancer_treatment` | `note_pancreatitis` | `note_pregnancy` | `note_recent_cv_event` |
|---|---|---|---|---|
| structured=criteria · notes=jev (jev-1.13.0) (version 0.1.0, its agent digest not recorded) | 24 of 24 [85.8-100.0%] / 0.028 | 24 of 24 [85.8-100.0%] / 0.048 | 24 of 24 [85.8-100.0%] / 0.020 | 24 of 24 [85.8-100.0%] / 0.029 |
| structured=criteria · notes=llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | 24 of 24 [85.8-100.0%] / 0.004 | 24 of 24 [85.8-100.0%] / 0.005 | 24 of 24 [85.8-100.0%] / 0.004 | 24 of 24 [85.8-100.0%] / 0.003 |
| structured=criteria · notes=llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | 24 of 24 [85.8-100.0%] / 0.023 | 24 of 24 [85.8-100.0%] / 0.022 | 24 of 24 [85.8-100.0%] / 0.022 | 24 of 24 [85.8-100.0%] / 0.027 |
| structured=criteria · notes=llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | 24 of 24 [85.8-100.0%] / 0.013 | 24 of 24 [85.8-100.0%] / 0.011 | 24 of 24 [85.8-100.0%] / 0.014 | 24 of 24 [85.8-100.0%] / 0.013 |
| structured=criteria · notes=rules (version 0.1.0, its agent digest not recorded) | 22 of 24 [73.0-99.0%] / 0.036 | 23 of 24 [78.9-99.9%] / 0.006 | 24 of 24 [85.8-100.0%] / 0.047 | 23 of 24 [78.9-99.9%] / 0.043 |
| structured=criteria · notes=rules_and_jev (jev-1.13.0) (version 0.1.0, its agent digest not recorded) | 24 of 24 [85.8-100.0%] / 0.048 | 24 of 24 [85.8-100.0%] / 0.067 | 24 of 24 [85.8-100.0%] / 0.042 | 24 of 24 [85.8-100.0%] / 0.071 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | 24 of 24 [85.8-100.0%] / 0.042 | 24 of 24 [85.8-100.0%] / 0.048 | 24 of 24 [85.8-100.0%] / 0.041 | 24 of 24 [85.8-100.0%] / 0.069 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | 24 of 24 [85.8-100.0%] / 0.048 | 24 of 24 [85.8-100.0%] / 0.047 | 24 of 24 [85.8-100.0%] / 0.046 | 24 of 24 [85.8-100.0%] / 0.074 |
| structured=criteria · notes=rules_and_llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | 24 of 24 [85.8-100.0%] / 0.046 | 24 of 24 [85.8-100.0%] / 0.046 | 24 of 24 [85.8-100.0%] / 0.042 | 24 of 24 [85.8-100.0%] / 0.070 |

**`test`** (n=100; dataset hash not recorded; k of n [exact 95% interval, Clopper-Pearson])

| Configuration | Critical detected | False automation | Accuracy | AUTO | Macro-F1 | ECE | p50 latency | Cost / decision | Errors |
|---|---|---|---|---|---|---|---|---|---|
| structured=criteria · notes=llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 5 of 5 [47.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 7 of 7 [59.0-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); egfr_30_or_more=no 12 of 12 [73.5-100.0%] (0 automated; insufficient evidence); no_recent_cv_event=no 7 of 7 [59.0-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 1.000 | 0.044 | 4,254 ms | $0.00375 | 0 |
| structured=criteria · notes=llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 5 of 5 [47.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 7 of 7 [59.0-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); egfr_30_or_more=no 12 of 12 [73.5-100.0%] (0 automated; insufficient evidence); no_recent_cv_event=no 7 of 7 [59.0-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 1.000 | 0.065 | 2,369 ms | $0.00404 | 0 |
| structured=criteria · notes=llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 5 of 5 [47.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 7 of 7 [59.0-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); egfr_30_or_more=no 12 of 12 [73.5-100.0%] (0 automated; insufficient evidence); no_recent_cv_event=no 7 of 7 [59.0-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 1.000 | 0.054 | 1,092 ms | $0.00016 | 0 |
| structured=criteria · notes=rules (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 4 of 5 [28.4-99.5%] (0 automated; insufficient evidence); note_pancreatitis=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 7 of 7 [59.0-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); egfr_30_or_more=no 12 of 12 [73.5-100.0%] (0 automated; insufficient evidence); no_recent_cv_event=no 7 of 7 [59.0-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 1.000 | 0.086 | 0.31 ms | $0.00000 | 0 |
| structured=criteria · notes=rules_and_jev (jev-1.13.0) (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 5 of 5 [47.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 7 of 7 [59.0-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); egfr_30_or_more=no 12 of 12 [73.5-100.0%] (0 automated; insufficient evidence); no_recent_cv_event=no 7 of 7 [59.0-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 1.000 | 0.089 | 178 ms | $0.00003 | 0 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 5 of 5 [47.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 7 of 7 [59.0-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); egfr_30_or_more=no 12 of 12 [73.5-100.0%] (0 automated; insufficient evidence); no_recent_cv_event=no 7 of 7 [59.0-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 1.000 | 0.084 | 3,957 ms | $0.00364 | 0 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 5 of 5 [47.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 7 of 7 [59.0-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); egfr_30_or_more=no 12 of 12 [73.5-100.0%] (0 automated; insufficient evidence); no_recent_cv_event=no 7 of 7 [59.0-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 1.000 | 0.086 | 2,239 ms | $0.00403 | 0 |
| structured=criteria · notes=rules_and_llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 5 of 5 [47.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 7 of 7 [59.0-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); egfr_30_or_more=no 12 of 12 [73.5-100.0%] (0 automated; insufficient evidence); no_recent_cv_event=no 7 of 7 [59.0-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 1.000 | 0.085 | 1,068 ms | $0.00016 | 0 |

Accuracy and false automation are n/a on a row recorded before leaderboards kept the counts they are of (mendari.leaderboard/1).

Other questions (accuracy / ECE):

| Configuration | `note_active_cancer_treatment` | `note_pancreatitis` | `note_pregnancy` | `note_recent_cv_event` |
|---|---|---|---|---|
| structured=criteria · notes=llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | 100 of 100 [96.4-100.0%] / 0.006 | 100 of 100 [96.4-100.0%] / 0.004 | 100 of 100 [96.4-100.0%] / 0.004 | 100 of 100 [96.4-100.0%] / 0.003 |
| structured=criteria · notes=llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | 100 of 100 [96.4-100.0%] / 0.027 | 100 of 100 [96.4-100.0%] / 0.020 | 100 of 100 [96.4-100.0%] / 0.021 | 100 of 100 [96.4-100.0%] / 0.022 |
| structured=criteria · notes=llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | 100 of 100 [96.4-100.0%] / 0.015 | 100 of 100 [96.4-100.0%] / 0.011 | 100 of 100 [96.4-100.0%] / 0.012 | 100 of 100 [96.4-100.0%] / 0.012 |
| structured=criteria · notes=rules (version 0.1.0, its agent digest not recorded) | 100 of 100 [96.4-100.0%] / 0.047 | 99 of 100 [94.6-99.97%] / 0.037 | 99 of 100 [94.6-99.97%] / 0.037 | 100 of 100 [96.4-100.0%] / 0.046 |
| structured=criteria · notes=rules_and_jev (jev-1.13.0) (version 0.1.0, its agent digest not recorded) | 100 of 100 [96.4-100.0%] / 0.055 | 99 of 100 [94.6-99.97%] / 0.037 | 100 of 100 [96.4-100.0%] / 0.046 | 100 of 100 [96.4-100.0%] / 0.047 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | 100 of 100 [96.4-100.0%] / 0.045 | 99 of 100 [94.6-99.97%] / 0.037 | 100 of 100 [96.4-100.0%] / 0.046 | 100 of 100 [96.4-100.0%] / 0.046 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | 100 of 100 [96.4-100.0%] / 0.047 | 99 of 100 [94.6-99.97%] / 0.037 | 100 of 100 [96.4-100.0%] / 0.046 | 100 of 100 [96.4-100.0%] / 0.046 |
| structured=criteria · notes=rules_and_llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | 100 of 100 [96.4-100.0%] / 0.046 | 99 of 100 [94.6-99.97%] / 0.036 | 100 of 100 [96.4-100.0%] / 0.046 | 100 of 100 [96.4-100.0%] / 0.046 |

**`trial-local-site-example`** (n=100; dataset hash not recorded; k of n [exact 95% interval, Clopper-Pearson])

| Configuration | Critical detected | False automation | Accuracy | AUTO | Macro-F1 | ECE | p50 latency | Cost / decision | Errors |
|---|---|---|---|---|---|---|---|---|---|
| structured=criteria · notes=llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence); egfr_30_or_more=no 8 of 8 [63.1-100.0%] (0 automated; insufficient evidence); no_recent_cv_event=no 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 0.846 | 0.081 | 4,833 ms | $0.00387 | 0 |
| structured=criteria · notes=llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence); egfr_30_or_more=no 8 of 8 [63.1-100.0%] (0 automated; insufficient evidence); no_recent_cv_event=no 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 0.846 | 0.077 | 2,283 ms | $0.00402 | 0 |
| structured=criteria · notes=llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence); egfr_30_or_more=no 8 of 8 [63.1-100.0%] (0 automated; insufficient evidence); no_recent_cv_event=no 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 0.846 | 0.069 | 1,042 ms | $0.00016 | 0 |
| structured=criteria · notes=rules (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 1 of 2 [1.3-98.7%] (0 automated; insufficient evidence); note_pancreatitis=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 1 of 2 [1.3-98.7%] (0 automated; insufficient evidence); egfr_30_or_more=no 8 of 8 [63.1-100.0%] (0 automated; insufficient evidence); no_recent_cv_event=no 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 0.808 | 0.115 | 0.30 ms | $0.00000 | 0 |
| structured=criteria · notes=rules_and_jev (jev-1.13.0) (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence); egfr_30_or_more=no 8 of 8 [63.1-100.0%] (0 automated; insufficient evidence); no_recent_cv_event=no 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 0.818 | 0.127 | 187 ms | $0.00003 | 0 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence); egfr_30_or_more=no 8 of 8 [63.1-100.0%] (0 automated; insufficient evidence); no_recent_cv_event=no 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 0.818 | 0.124 | 4,161 ms | $0.00378 | 0 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence); egfr_30_or_more=no 8 of 8 [63.1-100.0%] (0 automated; insufficient evidence); no_recent_cv_event=no 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 0.818 | 0.122 | 2,259 ms | $0.00402 | 0 |
| structured=criteria · notes=rules_and_llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | note_pregnancy=yes 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence); note_pancreatitis=yes 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence); note_active_cancer_treatment=yes 4 of 4 [39.8-100.0%] (0 automated; insufficient evidence); note_recent_cv_event=yes 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence); egfr_30_or_more=no 8 of 8 [63.1-100.0%] (0 automated; insufficient evidence); no_recent_cv_event=no 3 of 3 [29.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 0.818 | 0.124 | 1,140 ms | $0.00016 | 0 |

Accuracy and false automation are n/a on a row recorded before leaderboards kept the counts they are of (mendari.leaderboard/1).

Other questions (accuracy / ECE):

| Configuration | `note_active_cancer_treatment` | `note_pancreatitis` | `note_pregnancy` | `note_recent_cv_event` |
|---|---|---|---|---|
| structured=criteria · notes=llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | 100 of 100 [96.4-100.0%] / 0.005 | 100 of 100 [96.4-100.0%] / 0.005 | 100 of 100 [96.4-100.0%] / 0.005 | 100 of 100 [96.4-100.0%] / 0.005 |
| structured=criteria · notes=llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | 100 of 100 [96.4-100.0%] / 0.023 | 100 of 100 [96.4-100.0%] / 0.021 | 100 of 100 [96.4-100.0%] / 0.020 | 100 of 100 [96.4-100.0%] / 0.021 |
| structured=criteria · notes=llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | 100 of 100 [96.4-100.0%] / 0.011 | 100 of 100 [96.4-100.0%] / 0.012 | 100 of 100 [96.4-100.0%] / 0.013 | 100 of 100 [96.4-100.0%] / 0.011 |
| structured=criteria · notes=rules (version 0.1.0, its agent digest not recorded) | 100 of 100 [96.4-100.0%] / 0.047 | 93 of 100 [86.1-97.1%] / 0.023 | 99 of 100 [94.6-99.97%] / 0.037 | 99 of 100 [94.6-99.97%] / 0.037 |
| structured=criteria · notes=rules_and_jev (jev-1.13.0) (version 0.1.0, its agent digest not recorded) | 100 of 100 [96.4-100.0%] / 0.056 | 93 of 100 [86.1-97.1%] / 0.023 | 100 of 100 [96.4-100.0%] / 0.047 | 100 of 100 [96.4-100.0%] / 0.048 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | 100 of 100 [96.4-100.0%] / 0.046 | 93 of 100 [86.1-97.1%] / 0.024 | 100 of 100 [96.4-100.0%] / 0.047 | 100 of 100 [96.4-100.0%] / 0.046 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | 100 of 100 [96.4-100.0%] / 0.047 | 93 of 100 [86.1-97.1%] / 0.023 | 100 of 100 [96.4-100.0%] / 0.047 | 100 of 100 [96.4-100.0%] / 0.048 |
| structured=criteria · notes=rules_and_llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | 100 of 100 [96.4-100.0%] / 0.046 | 93 of 100 [86.1-97.1%] / 0.024 | 100 of 100 [96.4-100.0%] / 0.047 | 100 of 100 [96.4-100.0%] / 0.047 |


## Known failure modes

- Criteria are computed only from the codes, units and names listed in logic/criteria.yaml. A lab result under another LOINC code, a unit without a declared conversion, or a condition under an unlisted code is `unknown` or not found. Conditions are listed in ICD-10-CM only (plus the local-site example's fictional codes). Through FHIR a Condition is read by its ICD-10-CM coding wherever it sits among its codings (then by a fictional code), a lab by its LOINC coding and a drug by its RxNorm coding, so a site's own coding beside the standard one is passed over; but until the site adds its codes, a diagnosis coded only in SNOMED CT or a site's own system is not found (type 2 diabetes is `unknown`, a heart attack or stroke is missed), and neither is a lab with no LOINC coding. A metformin brand name (such as Glucophage) is not matched, so the patient is wrongly `not_eligible` (medications are matched by name, not by their RxNorm code). On the bundled local-site example (datasets/trial-local-site-example.jsonl), which has both, plus Spanish notes, eligibility is answered wrong far more often than on the benchmark (`mendari eval mendari/clinical-trial-prescreener --dataset datasets/trial-local-site-example.jsonl` compares it with the benchmark, by label and by subgroup). Add a site's codes, units and names before use, and validate locally.
- An incomplete medication list reads as "not on metformin", which fails a criterion.
- Through FHIR, medications come from MedicationStatement and MedicationRequest. When a MedicationRequest's drug is taken is a rule Mendari states (spec 0.3 §2.7). The owner, a physician, answered the clinician brief on it (notes/clinician-review-medication-period.md) on 2026-11-07 with "acceptable with changes"; this version builds those changes, which the owner is to confirm as built. A request starts at the first dosage instruction's `timing.repeat.boundsPeriod.start`, else on the day it was written (`authoredOn`). It stops at that `boundsPeriod.end`; else, when its start is a full date, at the start plus that timing's `boundsDuration` (in hours, days, weeks, months or years: UCUM `h`, `d`, `wk`, `mo`, `a`, a month being 30.4375 days and a year 365.25, rounded up to a whole day); else its stop is unknown. The stop is never inferred from the request's status (`completed`, `stopped`), the dispensing window (`dispenseRequest.validityPeriod`) is not read, and only the first dosage instruction counts. A request entered in error, one whose intent is a proposal, a plan or an option, and one saying not to give the drug (`doNotPerform`) are left out. The criteria count a medication, from a request or a statement, as current when its status is `active`, but one with no end date only when it started in the 365 days before screening (`current_within_days` in logic/criteria.yaml): an older one, or one with no start date, reads `unknown`, so the chart goes to review. So a patient on metformin for years whose record holds only an old active request or statement with no end date is `needs_review`, not `candidate`, and an active insulin started more than a year ago with no end date is `needs_review`, not `not_eligible`. An `active` medication with an end date counts as current whatever that date, so an active request written two years ago for 30 days (`boundsDuration`) reads as metformin taken for two years (`stable_metformin` "yes"). A medication that is not `active` and has no end date reads as not taken: insulin on a `completed` request written 20 days ago reads as off insulin (`no_insulin` "yes", where its stop is in fact unknown), and an `on-hold` metformin as not on metformin (`stable_metformin` "no", the prescreen `not_eligible`, which shows no card). Renewals are not joined: a renewed prescription with no bounds starts on the day it was renewed, which can read as metformin started less than 90 days ago (`not_eligible`), and of several requests for one drug the first in the record's order decides, which may be a renewal rather than the earliest. A MedicationRequest naming its drug by reference needs the Medication, which a CDS hook or the SMART launch reads from the EHR; where it cannot be read, the pack abstains.
- Through FHIR, a note is read only as plain text or HTML, inline or in the Binary its url names (read from the EHR on a CDS hook or the SMART launch). An RTF, PDF or CDA note, or one that cannot be read, makes the pack abstain, and EHRs often keep notes as RTF. So do notes longer than 20,000 characters in all, which the input does not allow (`too_large`; the text is never cut), and more links than one decision reads (20, `cap`). A hook reads every clinical note of the patient, each linked note's Binary and each Medication a MedicationRequest names counting as one link, so a long record may reach either.
- Old data is not used. An HbA1c older than 90 days or an eGFR older than 365 days gives `unknown`, so the patient needs review rather than being guessed.
- The default note rules are English keyword patterns with a fixed-window negation and family-member heuristic. On hand-written notes they found 15/15 exclusions in the set they were written on but 8/12 in the held-out set (all 4 pregnancies; 1 of 2 pancreatitis, 1 of 3 cancer treatments, 2 of 3 recent cardiovascular events). They miss most notes in other languages and misread Spanish negation ("Sin antecedentes de pancreatitis"), and cannot compare dates written in the text, so any mention of a heart attack or stroke without a recency cue is flagged for review. That flag also tips the overall result to `not_eligible` at about 50 % confidence, which keeps the patient-view card silent for a patient who may be a candidate (2 of 24 held-out patients). The rules are an offline fallback, not a deployment choice.
- Deploy with a model reading the notes. In the provider comparison (measured on an earlier version), Jev and each LLM alone found every held-out exclusion; adding the rules to a model (`rules_and_jev`, `rules_and_llm`) kept every exclusion but answered eligibility wrong more often, held out and at the local site, through the rules' flags and Spanish negation errors. If the model is unavailable the pack abstains and the coordinator screens the chart as usual; nothing is automated at risk level 2.
- The overall probability treats every criterion as independent. Its confidence is a guide for ordering review, not a calibrated chance of eligibility.
- The public benchmark is generated from templates written with the pack, so it overstates real-world accuracy. The hand-written exclusion sets are the better guide, and the held-out one was written before the note rules.

## Evidence and provenance

- **Synthetic public benchmark**: evals/benchmark.json, the `test` split generated by mendari.sandbox.synthetic_trial; ground truth comes from the generated facts.
- **Hand-written exclusion sets**: data/exclusions.jsonl (used to write the note rules) and data/exclusions_holdout.jsonl (written before the rules and never used to tune them): pregnancy, pancreatitis, cancer treatment and cardiovascular events, with negated, family-history and remote look-alikes.

## Version history

Renamed on 2026-09-30, when the project became Mendari: this pack was `openhealth/clinical-trial-prescreener`.
Its id, its publisher's name and its `https://mendari.co/...` identifiers changed; its criteria,
rules, models, data and results did not.

#### 0.4.0

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

### 0.3.0

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

### 0.2.0

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

### 0.1.5

- Two known failure modes (the local site, and rules added to a model) state their findings,
  and the command that measures the first, instead of percentages typed in by hand (LP-10: a
  share of cases on a card is k of n with its interval, computed). Criteria, rules, providers,
  policy, models and data are unchanged; every decision is identical. The digest changes
  because the manifest does.

### 0.1.4

- `code_systems` declares the systems the pack uses: ICD-10-CM, LOINC and SNOMED CT, with SNOMED
  CT's licence terms (a pack naming a licensed code system now has to say them).
- Criteria, rules, providers, policy, models and data are unchanged; every decision is identical.
  The digest changes because the manifest does.

### 0.1.3

- Every surface now says what the pack is: "Synthetic example. Not clinically reviewed. Not for
  patient care." It is derived from the new `card.provenance: synthetic`, never written by the
  pack (the Decision Card's first line, the dashboard, Try it, the review queue, CDS Hooks cards
  and discovery, the registry and the Hub).
- Each question has a `display` name, so people read words instead of keys.
- Criteria, rules, providers, policy, models and data are unchanged; every decision is identical.
  The digest changes because the manifest does.

### 0.1.2

- A `cql` variant of the `structured` slot: the seven criteria written in CQL
  (`logic/criteria.cql`), evaluated on a CQL engine (`$MENDARI_CQL_URL`, e.g. HAPI FHIR with
  Clinical Reasoning). It agrees with `criteria` on every test and dataset case, which CI
  checks. `criteria` stays the default. Questions, criteria, rules, policy and data are
  unchanged.

### 0.1.1

- Decision Card: deployment guidance from the provider comparison (evals/leaderboard.json,
  measured on 0.1.0). Deploy with a model reading the notes (`notes=jev` or `notes=llm`);
  adding the keyword rules to a model lowered eligibility accuracy without finding more
  exclusions. Questions, criteria, rules, policy and data are unchanged.

### 0.1.0

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

---

*Generated by `mendari card`. A Decision Card describes validation on the data named above. It is not evidence of performance at your site; run local validation first.*
