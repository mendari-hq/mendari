# Decision Card: Clinical Trial Prescreener

`openhealth/clinical-trial-prescreener` · version **0.1.0** · Apache-2.0 · published by OpenHealth Hub reference packs (community)

> Checks a patient's record against the eligibility criteria of a synthetic type 2 diabetes study (GLYCEMIA-2): structured criteria are computed from coded data, and exclusions are looked for in the clinic notes. Every result goes to a research coordinator for review.

| | |
|---|---|
| Risk level | **2**, clinical support |
| Automation | Human review required by default; AUTO only with a recorded governance override. |
| Pack digest | `sha256:c1547808d23552e2dc5f377f9cfa0311912774f53e847cb5c250b93177e0a992` |
| Last validation | 2026-09-29 on `test` (n=300, sha256 `12d2fb4b741a…`) |

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
| `yes` | Type 2 diabetes is coded (ICD-10 E11 or SNOMED CT 44054006). |
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
| `yes` | Metformin is active and was started at least 90 days before screening. |
| `no` | Metformin is not listed, was stopped, or was started less than 90 days ago. |
| `unknown` | Metformin is active but has no start date. |

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
| `no` | Insulin is active, or ended in the last 90 days. |
| `unknown` | Cannot tell from the medication list. |

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
| `structured` | age_18_75, t2d_diagnosis, hba1c_in_range, stable_metformin, egfr_30_or_more, no_insulin, no_recent_cv_event | `criteria` (criteria) | none |
| `notes` | note_pregnancy, note_pancreatitis, note_active_cancer_treatment, note_recent_cv_event | `rules` (rules) | `jev` (jev), `llm` (llm), `rules_and_jev` (any_of), `rules_and_llm` (any_of) |

## Validation

Benchmark `test` (n=300) with structured=`criteria`, notes=`rules`:

| Metric | Value |
|---|---|
| `eligibility` accuracy | 99.3% |
| Macro-F1 | 0.992 |
| Expected calibration error | 0.083 |
| Brier score | 0.049 |
| AUTO rate | 0.0% |
| HUMAN_REVIEW rate | 100.0% |
| ABSTAIN rate | 0.0% |
| BLOCK rate | 0.0% |
| Accuracy of AUTO decisions | n/a |
| False automation rate | n/a |
| Expected human override rate | 0.7% |
| Latency p50 / p95 | 0.30 / 0.33 ms |
| Cost per decision | $0.00000 |
| Critical `note_pregnancy=yes` | 12/13 detected, 0 automated |
| Critical `note_pancreatitis=yes` | 6/6 detected, 0 automated |
| Critical `note_active_cancer_treatment=yes` | 16/17 detected, 0 automated |
| Critical `note_recent_cv_event=yes` | 13/13 detected, 0 automated |
| Critical `egfr_30_or_more=no` | 29/29 detected, 0 automated |
| Critical `no_recent_cv_event=no` | 17/17 detected, 0 automated |

Per-label performance:

| Label | Precision | Recall | F1 | n |
|---|---|---|---|---|
| `candidate` | 98.2% | 98.2% | 0.982 | 56 |
| `not_eligible` | 99.5% | 99.5% | 0.995 | 208 |
| `needs_review` | 100.0% | 100.0% | 1.000 | 36 |

Subgroups:

| Subgroup | n | Accuracy | AUTO rate | Abstention |
|---|---|---|---|---|
| site=public | 300 | 99.3% | 0.0% | 0.0% |
| language=en | 286 | 100.0% | 0.0% | 0.0% |
| language=es | 14 | 85.7% | 0.0% | 0.0% |
| sex=female | 148 | 99.3% | 0.0% | 0.0% |
| sex=male | 152 | 99.3% | 0.0% | 0.0% |
| age_band=18-39 | 68 | 98.5% | 0.0% | 0.0% |
| age_band=40-64 | 154 | 100.0% | 0.0% | 0.0% |
| age_band=65+ | 69 | 98.6% | 0.0% | 0.0% |
| age_band=missing | 9 | 100.0% | 0.0% | 0.0% |

Calibration (reliability):

| Confidence bin | n | Mean confidence | Accuracy |
|---|---|---|---|
| 0.7 to 0.8 | 92 | 73.3% | 98.9% |
| 0.9 to 1.0 | 208 | 98.9% | 99.5% |

Threshold sweep (if everything ≥ threshold were automated):

| Threshold | Coverage | Accuracy | Errors |
|---|---|---|---|
| 0.50 | 100.0% | 99.3% | 2 |
| 0.60 | 100.0% | 99.3% | 2 |
| 0.70 | 100.0% | 99.3% | 2 |
| 0.80 | 69.3% | 99.5% | 1 |
| 0.85 | 69.3% | 99.5% | 1 |
| 0.90 | 69.3% | 99.5% | 1 |
| 0.95 | 69.3% | 99.5% | 1 |
| 0.97 | 64.7% | 100.0% | 0 |
| 0.99 | 27.0% | 100.0% | 0 |

## Known failure modes

- Criteria are computed only from the codes, units and names listed in logic/criteria.yaml. A lab result under another LOINC code, a unit without a declared conversion, or a condition under an unlisted code is `unknown` or not found. A metformin brand name (such as Glucophage) is not matched, so the patient is wrongly `not_eligible`. On the bundled local-site example (datasets/trial-local-site-example.jsonl), which has both, plus 27 % Spanish notes, eligibility accuracy falls from 99 % to 84 %. Add a site's codes, units and names before use, and validate locally.
- An incomplete medication list reads as "not on metformin", which fails a criterion.
- Old data is not used. An HbA1c older than 90 days or an eGFR older than 365 days gives `unknown`, so the patient needs review rather than being guessed.
- The default note rules are English keyword patterns with a fixed-window negation and family-member heuristic. On hand-written notes they found 15/15 exclusions in the set they were written on but 8/12 in the held-out set (all 4 pregnancies; 1 of 2 pancreatitis, 1 of 3 cancer treatments, 2 of 3 recent cardiovascular events). They miss most notes in other languages and misread Spanish negation ("Sin antecedentes de pancreatitis"), and cannot compare dates written in the text, so any mention of a heart attack or stroke without a recency cue is flagged for review. Deploy with a model as a second reader (`notes=rules_and_jev` or `notes=rules_and_llm`).
- The overall probability treats every criterion as independent. Its confidence is a guide for ordering review, not a calibrated chance of eligibility.
- The public benchmark is generated from templates written with the pack, so it overstates real-world accuracy. The hand-written exclusion sets are the better guide, and the held-out one was written before the note rules.

## Evidence and provenance

- **Synthetic public benchmark**: evals/benchmark.json, the `test` split generated by openhealth.sandbox.synthetic_trial; ground truth comes from the generated facts.
- **Hand-written exclusion sets**: data/exclusions.jsonl (used to write the note rules) and data/exclusions_holdout.jsonl (written before the rules and never used to tune them): pregnancy, pancreatitis, cancer treatment and cardiovascular events, with negated, family-history and remote look-alikes.

## Version history

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

*Generated by `openhealth card`. A Decision Card describes validation on the data named above. It is not evidence of performance at your site; run local validation first.*
