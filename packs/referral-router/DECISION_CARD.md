# Decision Card: Referral Router

`openhealth/referral-router` · version **0.1.1** · Apache-2.0 · published by OpenHealth Hub reference packs (community)

> Reads a specialty referral as it is signed: suggests the specialty, spots referrals that should be seen within two weeks, checks that the referral has what that specialty needs, and sends complete routine referrals straight to the specialty's scheduling queue.

| | |
|---|---|
| Risk level | **1**, clinical information processing (clinical information) |
| Automation | AUTO permitted by policy thresholds; every decision is audited. |
| Pack digest | `sha256:6090a20166b9e81d54ee05c21ce24ad565d36f16826e7354931786a4b9fbc436` |
| Last validation | 2026-09-29 on `test` (n=600, sha256 `f085362d6bc2…`) |

## Intended use

First-pass handling of outpatient specialty referrals from primary care, when the order is signed (CDS Hooks `order-sign`) or when it reaches a referral office: suggest the specialty, flag possibly urgent referrals for same-day review, list missing information, and queue complete routine referrals for scheduling.

**Why risk level 1:** The pack decides where a referral goes next, not whether or how a patient is treated. Only complete, routine referrals with a confident specialty are sent to a scheduling queue without a person reading them. Anything that may need to be seen within two weeks is always reviewed by a person, and incomplete referrals go back to the referrer with the missing items listed. A wrong routing delays a referral; it does not by itself change care.

## Intended users

- Referral coordinators and specialty clinic schedulers
- Primary-care clinicians signing referrals
- Health IT teams integrating referral management with the EHR

## Patient population

Adults referred from primary care to one of eight outpatient specialties, with referral reasons written in English.

## Out of scope

- Emergencies. A referral is not the route for a patient who needs the emergency department today; the urgency screen only speeds up referrals that arrive anyway.
- Deciding whether a referral is clinically appropriate, or triaging within a specialty.
- Checking the specialty the referrer chose against the suggested one (the card shows the suggestion; the comparison is future work).
- Any real clinic's intake requirements. The required documents and urgency criteria are synthetic and were not written or reviewed by a clinician; a site sets its own.

## Inputs

| Field | Type | Required |
|---|---|---|
| `as_of` | string | no |
| `patient` | object | no |
| `patient.age` | integer | no |
| `patient.sex` | `female` \| `male` \| `other` \| `unknown` | no |
| `referral` | object | no |
| `referral.reason` | string | no |
| `referral.priority` | `routine` \| `urgent` \| `asap` \| `stat` | no |
| `documents` | any | no |
| `reports` | any | no |
| `labs` | any | no |

FHIR R4 mapping: `fhir/mapping.yaml`.

## Outputs

**`specialty`**: Which specialty should see the patient for the problem described in the referral?

| Label | Criteria |
|---|---|
| `cardiology` | Heart and circulation, e.g. chest pain, palpitations, murmurs, heart failure, high blood pressure. |
| `dermatology` | Skin, hair and nails, e.g. rashes, moles and skin lesions, eczema, psoriasis, acne. |
| `endocrinology` | Hormones and metabolism, e.g. diabetes, thyroid, calcium, sodium, bone density. |
| `gastroenterology` | Digestive system and liver, e.g. swallowing, reflux, bowel habit, bleeding, anaemia from the gut, jaundice. |
| `neurology` | Brain, nerves and muscles, e.g. headaches, seizures, weakness, tremor, numbness, memory. |
| `orthopedics` | Bones and joints needing orthopaedic care, e.g. osteoarthritis, back pain, fractures, joint infection. |
| `pulmonology` | Lungs and breathing, e.g. asthma, COPD, cough, lung nodules, coughing up blood, sleep apnoea. |
| `rheumatology` | Inflammatory and autoimmune conditions, e.g. inflammatory arthritis, gout, giant cell arteritis, polymyalgia. |
| `other` | None of these specialties, e.g. ENT, eye, urology or audiology. |

**`urgency`**: Should the patient be seen by the specialist within two weeks?

| Label | Criteria |
|---|---|
| `urgent` | Possibly: a suspected cancer (a changing or bleeding skin lesion, a new lung mass or nodule, coughing up blood in a smoker, difficulty swallowing or rectal bleeding with weight loss, iron-deficiency anaemia, painless jaundice, a persistent hoarse voice or visible blood in the urine); a progressive neurological problem or a first seizure; new exertional chest pain, fainting on exertion or new heart failure; suspected giant cell arteritis; a hot swollen joint with fever; back pain with saddle numbness or bladder change; being unable to bear weight after a fall; a dangerous calcium, sodium or glucose level. |
| `routine` | Not time-critical: stable or long-standing problems, symptoms already investigated with normal results, surveillance, requests for advice or medication review, and past events that have resolved. |

**`clear_question`** *(boolean)*: Does the referral say what the referrer wants from the specialist?

| Label | Criteria |
|---|---|
| `yes` | The referral states a request or question, e.g. "please assess", "please advise on treatment", "?angina", or "pls eval". |
| `no` | The referral only names a problem, e.g. "Knee pain.", with no request. |

**`recent_visit_note`** *(boolean)*: Is there a visit note from the last 180 days?

| Label | Criteria |
|---|---|
| `yes` | Yes. |
| `no` | No. |

**`ecg_on_file`** *(boolean)*: Is there an ECG from the last 365 days?

| Label | Criteria |
|---|---|
| `yes` | Yes. |
| `no` | No. |

**`imaging_on_file`** *(boolean)*: Is there an imaging report from the last 365 days?

| Label | Criteria |
|---|---|
| `yes` | Yes. |
| `no` | No. |

**`recent_labs`** *(boolean)*: Are there laboratory results from the last 90 days?

| Label | Criteria |
|---|---|
| `yes` | Yes. |
| `no` | No. |

**`marked_urgent`** *(boolean)*: Did the referrer mark the referral urgent, ASAP or stat?

| Label | Criteria |
|---|---|
| `yes` | Yes. |
| `no` | No. |

**`next_step`** (primary): What should happen to the referral next?

Derived from `urgency`, `marked_urgent`, `specialty`, `clear_question`, `recent_visit_note`, `ecg_on_file`, `imaging_on_file`, `recent_labs` by a decision table (the first matching row wins; the members' answers are treated as independent):

1. any of `urgency` is `urgent` → `urgent_review`
2. any of `marked_urgent` is `yes` → `urgent_review`
3. any of `specialty` is `other` → `coordinator_review`
4. any of `clear_question`, `recent_visit_note` is `no` → `return_for_info`
5. any of `ecg_on_file` is `no`, when `specialty` is `cardiology` → `return_for_info`
6. any of `imaging_on_file` is `no`, when `specialty` is `orthopedics` → `return_for_info`
7. any of `recent_labs` is `no`, when `specialty` is `endocrinology` or `gastroenterology` or `rheumatology` → `return_for_info`
8. otherwise → `schedule`

| Label | Criteria |
|---|---|
| `urgent_review` | A person reviews it today: it may need to be seen within two weeks, or the referrer marked it urgent. |
| `coordinator_review` | A referral coordinator decides, because none of the eight specialties fits. |
| `return_for_info` | Send it back to the referrer: the request is unclear, or information the specialty needs is missing (listed in the reasons). |
| `schedule` | Complete and routine, so it goes to the specialty's scheduling queue. |

Every decision is one of `AUTO`, `HUMAN_REVIEW`, `ABSTAIN` or `BLOCK`, with a priority (`routine`, `urgent`, `asap`, `stat`) and an audit record.

## Decision policy

- AUTO when `next_step` confidence ≥ **0.90**; human review ≥ **0.40**; abstain below.
- `urgent_review` is never automated.
- `return_for_info` needs ≥ **0.95** for AUTO.
- AUTO only while `triage_screen` is `rules_and_jev` or `rules_and_llm`; otherwise a person reviews.
- Rule **possible-urgent**: P(`urgency` is `urgent`) ≥ 0.30 ⇒ `HUMAN_REVIEW` → `urgent_review`, priority `urgent`. The referral may need to be seen within two weeks. Review it today; do not leave it in the routine queue.
- Rule **marked-urgent**: `marked_urgent` is `yes` ⇒ `HUMAN_REVIEW` → `urgent_review`, priority `urgent`. The referrer marked the referral urgent.
- Fallback (ABSTAIN / BLOCK): Referral office queue for manual handling (the existing workflow).

## Providers

| Slot | Answers | Default | Alternatives |
|---|---|---|---|
| `specialty_reader` | specialty | `naive_bayes` (naive_bayes) | `jev` (jev), `llm` (llm) |
| `triage_screen` | urgency, clear_question | `rules` (rules) | `rules_and_jev` (any_of), `jev` (jev), `rules_and_llm` (any_of), `llm` (llm) |
| `checklist` | recent_visit_note, ecg_on_file, imaging_on_file, recent_labs, marked_urgent | `criteria` (criteria) | none |

## Validation

Benchmark `test` (n=600) with specialty_reader=`naive_bayes`, triage_screen=`rules`, checklist=`criteria`:

| Metric | Value |
|---|---|
| `next_step` accuracy | 99.5% |
| Macro-F1 | 0.981 |
| Expected calibration error | 0.056 |
| Brier score | 0.013 |
| AUTO rate | 0.0% |
| HUMAN_REVIEW rate | 100.0% |
| ABSTAIN rate | 0.0% |
| BLOCK rate | 0.0% |
| Accuracy of AUTO decisions | n/a |
| False automation rate | n/a |
| Expected human override rate | 0.5% |
| Latency p50 / p95 | 2.71 / 2.86 ms |
| Cost per decision | $0.00000 |
| Critical `urgency=urgent` | 113/113 detected, 0 automated |
| Critical `next_step=urgent_review` | 124/124 detected, 0 automated |

Per-label performance:

| Label | Precision | Recall | F1 | n |
|---|---|---|---|---|
| `urgent_review` | 100.0% | 100.0% | 1.000 | 124 |
| `coordinator_review` | 100.0% | 87.5% | 0.933 | 24 |
| `return_for_info` | 98.3% | 100.0% | 0.991 | 172 |
| `schedule` | 100.0% | 100.0% | 1.000 | 280 |

Subgroups:

| Subgroup | n | Accuracy | AUTO rate | Abstention |
|---|---|---|---|---|
| site=public | 600 | 99.5% | 0.0% | 0.0% |
| specialty=cardiology | 108 | 100.0% | 0.0% | 0.0% |
| specialty=dermatology | 75 | 100.0% | 0.0% | 0.0% |
| specialty=endocrinology | 48 | 100.0% | 0.0% | 0.0% |
| specialty=gastroenterology | 87 | 100.0% | 0.0% | 0.0% |
| specialty=neurology | 75 | 100.0% | 0.0% | 0.0% |
| specialty=orthopedics | 81 | 100.0% | 0.0% | 0.0% |
| specialty=other | 30 | 90.0% | 0.0% | 0.0% |
| specialty=pulmonology | 55 | 100.0% | 0.0% | 0.0% |
| specialty=rheumatology | 41 | 100.0% | 0.0% | 0.0% |
| kind=look_alike | 66 | 100.0% | 0.0% | 0.0% |
| kind=routine | 372 | 100.0% | 0.0% | 0.0% |
| kind=urgent | 113 | 100.0% | 0.0% | 0.0% |
| kind=vague | 49 | 93.9% | 0.0% | 0.0% |
| style=prose | 600 | 99.5% | 0.0% | 0.0% |
| age_band=18-39 | 159 | 99.4% | 0.0% | 0.0% |
| age_band=40-64 | 247 | 99.6% | 0.0% | 0.0% |
| age_band=65+ | 194 | 99.5% | 0.0% | 0.0% |
| sex=female | 302 | 99.3% | 0.0% | 0.0% |
| sex=male | 298 | 99.7% | 0.0% | 0.0% |

Calibration (reliability):

| Confidence bin | n | Mean confidence | Accuracy |
|---|---|---|---|
| 0.4 to 0.5 | 1 | 48.1% | 0.0% |
| 0.9 to 1.0 | 599 | 94.2% | 99.7% |

Threshold sweep (if everything ≥ threshold were automated):

| Threshold | Coverage | Accuracy | Errors |
|---|---|---|---|
| 0.50 | 99.8% | 99.7% | 2 |
| 0.60 | 99.8% | 99.7% | 2 |
| 0.70 | 99.8% | 99.7% | 2 |
| 0.80 | 99.8% | 99.7% | 2 |
| 0.85 | 99.8% | 99.7% | 2 |
| 0.90 | 99.8% | 99.7% | 2 |
| 0.95 | 28.3% | 100.0% | 0 |
| 0.97 | 11.3% | 100.0% | 0 |
| 0.99 | 9.5% | 100.0% | 0 |

## Known failure modes

- The checklist looks only for the document codes listed in logic/checklist.yaml. A visit note filed under another code (e.g. a history and physical, LOINC 34117-2) reads as missing, so a complete referral goes back to the referrer; the bundled local-site example shows this. Add a site's codes before use.
- The keyword urgency screen misses phrasings it does not list. On hand-written referrals it caught 16/16 urgent ones in the set it was written on but 7/12 in the held-out set. So the policy allows AUTO only while the screen includes a model (`automation_requires` with `triage_screen=rules_and_jev` or `rules_and_llm`, where either one flagging a referral as possibly urgent sends it to review). With the rules alone, every referral goes to a person.
- The naive Bayes specialty reader was trained on synthetic referrals only. Shorthand and a site's own case mix lower its accuracy; with a less confident specialty, fewer referrals are automated.
- Requirements depend on the suggested specialty. If the specialty is wrong, the wrong requirements are checked, but that referral's lower confidence usually keeps it out of automation.
- The overall probability treats every answer as independent.
- The public benchmark is generated from templates written with the pack, so it overstates real-world accuracy. The hand-written urgent sets are the better guide, and the held-out one was written before the rules.

## Evidence and provenance

- **Synthetic public benchmark**: evals/benchmark.json, the `test` split generated by openhealth.sandbox.synthetic_referral; ground truth comes from the generated facts.
- **Hand-written urgent referral sets**: data/urgent_referrals.jsonl (used to write the urgency rules) and data/urgent_referrals_holdout.jsonl (written before the rules and never used to tune them): urgent referral texts across the eight specialties, and routine look-alikes that mention alarming words about something remote, stable or reassuring.

## Version history

### 0.1.1

- `rules_and_jev` and `rules_and_llm`: for `clear_question` the more generous reader now wins
  (`escalate: yes`), so a referral goes back to the referrer only if neither the rules nor the
  model finds a request. Before, the keyword rules alone could send a clear referral back
  ("wants to discuss biologic options" has no request keyword), which delays the patient's
  care. Found in the provider comparison on the held-out set; `urgency` is unchanged.

### 0.1.0

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

---

*Generated by `openhealth card`. A Decision Card describes validation on the data named above. It is not evidence of performance at your site; run local validation first.*
