# Decision Card: Referral Router

**Synthetic example. Not clinically reviewed. Not for patient care.**

*Research preview. Not a medical device. Not for emergencies: patients should call 911.*

`mendari/referral-router` · version **0.2.1** · Apache-2.0 · published by Mendari reference packs (community, self-declared)

> Reads a specialty referral as it is signed: suggests the specialty, spots referrals that should be seen within two weeks, checks that the referral has what that specialty needs, and sends complete routine referrals straight to the specialty's scheduling queue.

| | |
|---|---|
| Source | Maintained by Mendari |
| Risk level | **1**, clinical information processing (clinical information) |
| Automation | AUTO permitted by policy thresholds; every decision is audited. Only while `triage_screen` is `rules_and_jev` or `rules_and_llm`; otherwise a person reviews. |
| Pack digest | `sha256:fc843d27f5a93a962a49f512a7870391ec5cf0707f73c13f1e73f2c279e33630` |
| Last validation | 2026-11-06 on `test` (n=600, sha256 `f085362d6bc2…`) |

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

Code systems:

- Mendari fictional referral codes: invented for the synthetic examples; not SNOMED CT. A site maps the code its EHR marks a referral with to these. (`https://mendari.co/fhir/CodeSystem/fictional-referrals`).
- LOINC (Regenstrief Institute; the LOINC License, whose notice is in NOTICE) (`http://loinc.org`).

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
| `specialty_reader` | specialty | `naive_bayes` (naive_bayes) | `jev` (jev), `llm` (llm), `cascade` (cascade) |
| `triage_screen` | urgency, clear_question | `rules` (rules) | `rules_and_jev` (any_of), `jev` (jev), `rules_and_llm` (any_of), `llm` (llm) |
| `checklist` | recent_visit_note, ecg_on_file, imaging_on_file, recent_labs, marked_urgent | `criteria` (criteria) | none |

## Validation

Benchmark `test` (n=600) with specialty_reader=`naive_bayes`, triage_screen=`rules`, checklist=`criteria`. Each proportion: k of n [exact 95% interval, Clopper-Pearson].

| Metric | Value |
|---|---|
| `next_step` accuracy | 597 of 600 [98.5-99.9%] |
| Macro-F1 | 0.981 |
| Expected calibration error | 0.056 |
| Expected calibration error, equal-mass bins | 0.054 |
| Brier score | 0.013 [0.006-0.023, bootstrap] |
| AUTO | 0 of 600 [0.0-0.6%] |
| HUMAN_REVIEW | 600 of 600 [99.4-100.0%] |
| ABSTAIN | 0 of 600 [0.0-0.6%] |
| BLOCK | 0 of 600 [0.0-0.6%] |
| AUTO decisions correct | no cases |
| False automation (of labelled cases) | 0 of 600 [0.0-0.6%] |
| Expected human override (of reviewed) | 3 of 600 [0.1-1.5%] |
| Latency p50 / p95 | 1.89 / 2.05 ms |
| Cost per decision | $0.00000 |
| Critical `urgency=urgent` | 113 of 113 [96.8-100.0%] detected, 0 automated |
| Critical `next_step=urgent_review` | 124 of 124 [97.1-100.0%] detected, 0 automated |

Per-label performance:

| Label | Precision | Recall | F1 |
|---|---|---|---|
| `urgent_review` | 124 of 124 [97.1-100.0%] | 124 of 124 [97.1-100.0%] | 1.000 |
| `coordinator_review` | 21 of 21 [83.9-100.0%] | 21 of 24 [67.6-97.3%] | 0.933 |
| `return_for_info` | 172 of 175 [95.1-99.6%] | 172 of 172 [97.9-100.0%] | 0.991 |
| `schedule` | 280 of 280 [98.7-100.0%] | 280 of 280 [98.7-100.0%] | 1.000 |

Subgroups:

| Subgroup | n | Accuracy | AUTO | Abstention |
|---|---|---|---|---|
| site=public | 600 | 597 of 600 [98.5-99.9%] | 0 of 600 [0.0-0.6%] | 0 of 600 [0.0-0.6%] |
| specialty=cardiology | 108 | 108 of 108 [96.6-100.0%] | 0 of 108 [0.0-3.4%] | 0 of 108 [0.0-3.4%] |
| specialty=dermatology | 75 | 75 of 75 [95.2-100.0%] | 0 of 75 [0.0-4.8%] | 0 of 75 [0.0-4.8%] |
| specialty=endocrinology | 48 | 48 of 48 [92.6-100.0%] | 0 of 48 [0.0-7.4%] | 0 of 48 [0.0-7.4%] |
| specialty=gastroenterology | 87 | 87 of 87 [95.8-100.0%] | 0 of 87 [0.0-4.2%] | 0 of 87 [0.0-4.2%] |
| specialty=neurology | 75 | 75 of 75 [95.2-100.0%] | 0 of 75 [0.0-4.8%] | 0 of 75 [0.0-4.8%] |
| specialty=orthopedics | 81 | 81 of 81 [95.5-100.0%] | 0 of 81 [0.0-4.5%] | 0 of 81 [0.0-4.5%] |
| specialty=other | 30 | 27 of 30 [73.5-97.9%] | 0 of 30 [0.0-11.6%] | 0 of 30 [0.0-11.6%] |
| specialty=pulmonology | 55 | 55 of 55 [93.5-100.0%] | 0 of 55 [0.0-6.5%] | 0 of 55 [0.0-6.5%] |
| specialty=rheumatology | 41 | 41 of 41 [91.4-100.0%] | 0 of 41 [0.0-8.6%] | 0 of 41 [0.0-8.6%] |
| kind=look_alike | 66 | 66 of 66 [94.6-100.0%] | 0 of 66 [0.0-5.4%] | 0 of 66 [0.0-5.4%] |
| kind=routine | 372 | 372 of 372 [99.0-100.0%] | 0 of 372 [0.0-1.0%] | 0 of 372 [0.0-1.0%] |
| kind=urgent | 113 | 113 of 113 [96.8-100.0%] | 0 of 113 [0.0-3.2%] | 0 of 113 [0.0-3.2%] |
| kind=vague | 49 | 46 of 49 [83.1-98.7%] | 0 of 49 [0.0-7.3%] | 0 of 49 [0.0-7.3%] |
| style=prose | 600 | 597 of 600 [98.5-99.9%] | 0 of 600 [0.0-0.6%] | 0 of 600 [0.0-0.6%] |
| age_band=18-39 | 159 | 158 of 159 [96.5-99.98%] | 0 of 159 [0.0-2.3%] | 0 of 159 [0.0-2.3%] |
| age_band=40-64 | 247 | 246 of 247 [97.8-99.99%] | 0 of 247 [0.0-1.5%] | 0 of 247 [0.0-1.5%] |
| age_band=65+ | 194 | 193 of 194 [97.2-99.99%] | 0 of 194 [0.0-1.9%] | 0 of 194 [0.0-1.9%] |
| sex=female | 302 | 300 of 302 [97.6-99.9%] | 0 of 302 [0.0-1.2%] | 0 of 302 [0.0-1.2%] |
| sex=male | 298 | 297 of 298 [98.1-99.99%] | 0 of 298 [0.0-1.2%] | 0 of 298 [0.0-1.2%] |

Calibration (reliability):

| Confidence bin | Mean confidence | Correct |
|---|---|---|
| 0.4 to 0.5 | 0.481 | 0 of 1 [0.0-97.5%] |
| 0.9 to 1.0 | 0.942 | 597 of 599 [98.8-99.96%] |

Threshold sweep (if everything ≥ threshold were automated):

| Threshold | Would be automated | Of those, correct |
|---|---|---|
| 0.50 | 599 of 600 [99.1-99.996%] | 597 of 599 [98.8-99.96%] |
| 0.60 | 599 of 600 [99.1-99.996%] | 597 of 599 [98.8-99.96%] |
| 0.70 | 599 of 600 [99.1-99.996%] | 597 of 599 [98.8-99.96%] |
| 0.80 | 599 of 600 [99.1-99.996%] | 597 of 599 [98.8-99.96%] |
| 0.85 | 599 of 600 [99.1-99.996%] | 597 of 599 [98.8-99.96%] |
| 0.90 | 599 of 600 [99.1-99.996%] | 597 of 599 [98.8-99.96%] |
| 0.95 | 170 of 600 [24.8-32.1%] | 170 of 170 [97.9-100.0%] |
| 0.97 | 68 of 600 [8.9-14.1%] | 68 of 68 [94.7-100.0%] |
| 0.99 | 57 of 600 [7.3-12.1%] | 57 of 57 [93.7-100.0%] |

## Provider comparison

Public set: regression check, not a ranking. Nothing here is graded: the pack's own sets are public, in the repository and in the package, so a configuration can be tuned to them, and grades and rankings are given only on frozen hidden sets. The rows are a listing order, not a ranking: those measured on this version of the pack, on records where a critical label has at least 100 positives, come first, the critical cases caught first and never accuracy alone; every other row follows, by configuration name. Mendari's [scoreboard](https://github.com/mendari-hq/mendari/blob/main/docs/grading/scoreboard.md) shows its reference packs' configurations on their public sets, each figure with its interval and scope.

Each table below is one dataset at one size, and the configurations in a table that records the dataset's hash ran on exactly the same records. Remote models are named because they are a deployment choice. Synthetic and hand-written data: compare configurations with each other, not with performance at a real site. A row measured on an earlier version of the pack says so beside its configuration: its AUTO and false automation followed that version's policy.

**`referral-local-site-example`** (n=100; dataset hash not recorded; k of n [exact 95% interval, Clopper-Pearson])

| Configuration | Critical detected | False automation | Accuracy | AUTO | Macro-F1 | ECE | p50 latency | Cost / decision | Errors |
|---|---|---|---|---|---|---|---|---|---|
| specialty_reader=jev (jev-1.13.0) · triage_screen=jev (jev-1.13.0) · checklist=criteria (version 0.1.0, its pack digest not recorded) | urgency=urgent 15 of 15 [78.2-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 18 of 18 [81.5-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 0.780 | 0.242 | 199 ms | $0.00004 | 0 |
| specialty_reader=jev (jev-1.13.0) · triage_screen=rules_and_jev (jev-1.13.0) · checklist=criteria (version 0.1.1, its pack digest not recorded) | urgency=urgent 15 of 15 [78.2-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 18 of 18 [81.5-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 27 of 100 [18.6-36.8%] | 0.780 | 0.258 | 210 ms | $0.00004 | 0 |
| specialty_reader=llm (anthropic/claude-haiku-4.5) · triage_screen=llm (anthropic/claude-haiku-4.5) · checklist=criteria (version 0.1.0, its pack digest not recorded) | urgency=urgent 15 of 15 [78.2-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 18 of 18 [81.5-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 0.810 | 0.206 | 10,560 ms | $0.00623 | 0 |
| specialty_reader=llm (anthropic/claude-haiku-4.5) · triage_screen=rules_and_llm (anthropic/claude-haiku-4.5) · checklist=criteria (version 0.1.1, its pack digest not recorded) | urgency=urgent 15 of 15 [78.2-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 18 of 18 [81.5-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 8 of 100 [3.5-15.2%] | 0.810 | 0.218 | 10,101 ms | $0.00605 | 0 |
| specialty_reader=llm (anthropic/claude-sonnet-5) · triage_screen=llm (anthropic/claude-sonnet-5) · checklist=criteria (version 0.1.0, its pack digest not recorded) | urgency=urgent 15 of 15 [78.2-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 18 of 18 [81.5-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 0.810 | 0.187 | 2,548 ms | $0.00515 | 0 |
| specialty_reader=llm (anthropic/claude-sonnet-5) · triage_screen=rules_and_llm (anthropic/claude-sonnet-5) · checklist=criteria (version 0.1.1, its pack digest not recorded) | urgency=urgent 15 of 15 [78.2-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 18 of 18 [81.5-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 5 of 100 [1.6-11.3%] | 0.810 | 0.172 | 2,458 ms | $0.00521 | 0 |
| specialty_reader=llm (openai/gpt-oss-120b) · triage_screen=llm (openai/gpt-oss-120b) · checklist=criteria (version 0.1.0, its pack digest not recorded) | urgency=urgent 15 of 15 [78.2-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 18 of 18 [81.5-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 0.803 | 0.175 | 1,534 ms | $0.00024 | 0 |
| specialty_reader=llm (openai/gpt-oss-120b) · triage_screen=rules_and_llm (openai/gpt-oss-120b) · checklist=criteria (version 0.1.1, its pack digest not recorded) | urgency=urgent 15 of 15 [78.2-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 18 of 18 [81.5-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 1 of 100 [0.03-5.4%] | 0.803 | 0.178 | 1,482 ms | $0.00024 | 0 |
| specialty_reader=naive_bayes · triage_screen=rules · checklist=criteria (version 0.1.0, its pack digest not recorded) | urgency=urgent 15 of 15 [78.2-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 18 of 18 [81.5-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 0.804 | 0.244 | 2.72 ms | $0.00000 | 0 |

Accuracy and false automation are n/a on a row recorded before leaderboards kept the counts they are of (mendari.leaderboard/1).

Other questions (accuracy / ECE):

| Configuration | `clear_question` | `specialty` | `urgency` |
|---|---|---|---|
| specialty_reader=jev (jev-1.13.0) · triage_screen=jev (jev-1.13.0) · checklist=criteria (version 0.1.0, its pack digest not recorded) | 98 of 100 [93.0-99.8%] / 0.071 | 98 of 100 [93.0-99.8%] / 0.009 | 97 of 100 [91.5-99.4%] / 0.023 |
| specialty_reader=jev (jev-1.13.0) · triage_screen=rules_and_jev (jev-1.13.0) · checklist=criteria (version 0.1.1, its pack digest not recorded) | 100 of 100 [96.4-100.0%] / 0.014 | 98 of 100 [93.0-99.8%] / 0.008 | 97 of 100 [91.5-99.4%] / 0.024 |
| specialty_reader=llm (anthropic/claude-haiku-4.5) · triage_screen=llm (anthropic/claude-haiku-4.5) · checklist=criteria (version 0.1.0, its pack digest not recorded) | 100 of 100 [96.4-100.0%] / 0.035 | 98 of 100 [93.0-99.8%] / 0.067 | 100 of 100 [96.4-100.0%] / 0.094 |
| specialty_reader=llm (anthropic/claude-haiku-4.5) · triage_screen=rules_and_llm (anthropic/claude-haiku-4.5) · checklist=criteria (version 0.1.1, its pack digest not recorded) | 100 of 100 [96.4-100.0%] / 0.016 | 97 of 100 [91.5-99.4%] / 0.056 | 100 of 100 [96.4-100.0%] / 0.087 |
| specialty_reader=llm (anthropic/claude-sonnet-5) · triage_screen=llm (anthropic/claude-sonnet-5) · checklist=criteria (version 0.1.0, its pack digest not recorded) | 100 of 100 [96.4-100.0%] / 0.079 | 100 of 100 [96.4-100.0%] / 0.121 | 100 of 100 [96.4-100.0%] / 0.108 |
| specialty_reader=llm (anthropic/claude-sonnet-5) · triage_screen=rules_and_llm (anthropic/claude-sonnet-5) · checklist=criteria (version 0.1.1, its pack digest not recorded) | 100 of 100 [96.4-100.0%] / 0.014 | 100 of 100 [96.4-100.0%] / 0.123 | 100 of 100 [96.4-100.0%] / 0.088 |
| specialty_reader=llm (openai/gpt-oss-120b) · triage_screen=llm (openai/gpt-oss-120b) · checklist=criteria (version 0.1.0, its pack digest not recorded) | 100 of 100 [96.4-100.0%] / 0.053 | 97 of 100 [91.5-99.4%] / 0.111 | 100 of 100 [96.4-100.0%] / 0.105 |
| specialty_reader=llm (openai/gpt-oss-120b) · triage_screen=rules_and_llm (openai/gpt-oss-120b) · checklist=criteria (version 0.1.1, its pack digest not recorded) | 100 of 100 [96.4-100.0%] / 0.016 | 96 of 100 [90.1-98.9%] / 0.131 | 100 of 100 [96.4-100.0%] / 0.087 |
| specialty_reader=naive_bayes · triage_screen=rules · checklist=criteria (version 0.1.0, its pack digest not recorded) | 100 of 100 [96.4-100.0%] / 0.012 | 90 of 100 [82.4-95.1%] / 0.071 | 100 of 100 [96.4-100.0%] / 0.022 |

**`test`** (n=100; dataset hash not recorded; k of n [exact 95% interval, Clopper-Pearson])

| Configuration | Critical detected | False automation | Accuracy | AUTO | Macro-F1 | ECE | p50 latency | Cost / decision | Errors |
|---|---|---|---|---|---|---|---|---|---|
| specialty_reader=jev (jev-1.13.0) · triage_screen=jev (jev-1.13.0) · checklist=criteria (version 0.1.0, its pack digest not recorded) | urgency=urgent 22 of 22 [84.6-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 22 of 22 [84.6-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 1.000 | 0.085 | 200 ms | $0.00004 | 0 |
| specialty_reader=jev (jev-1.13.0) · triage_screen=rules_and_jev (jev-1.13.0) · checklist=criteria (version 0.1.1, its pack digest not recorded) | urgency=urgent 22 of 22 [84.6-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 22 of 22 [84.6-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 39 of 100 [29.4-49.3%] | 1.000 | 0.072 | 195 ms | $0.00004 | 0 |
| specialty_reader=llm (anthropic/claude-haiku-4.5) · triage_screen=llm (anthropic/claude-haiku-4.5) · checklist=criteria (version 0.1.0, its pack digest not recorded) | urgency=urgent 22 of 22 [84.6-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 22 of 22 [84.6-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 0.962 | 0.144 | 10,343 ms | $0.00606 | 0 |
| specialty_reader=llm (anthropic/claude-haiku-4.5) · triage_screen=rules_and_llm (anthropic/claude-haiku-4.5) · checklist=criteria (version 0.1.1, its pack digest not recorded) | urgency=urgent 22 of 22 [84.6-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 22 of 22 [84.6-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 11 of 100 [5.6-18.8%] | 0.962 | 0.135 | 10,416 ms | $0.00621 | 0 |
| specialty_reader=llm (anthropic/claude-sonnet-5) · triage_screen=llm (anthropic/claude-sonnet-5) · checklist=criteria (version 0.1.0, its pack digest not recorded) | urgency=urgent 22 of 22 [84.6-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 22 of 22 [84.6-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 1.000 | 0.192 | 2,587 ms | $0.00518 | 0 |
| specialty_reader=llm (anthropic/claude-sonnet-5) · triage_screen=rules_and_llm (anthropic/claude-sonnet-5) · checklist=criteria (version 0.1.1, its pack digest not recorded) | urgency=urgent 22 of 22 [84.6-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 22 of 22 [84.6-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 2 of 100 [0.2-7.0%] | 1.000 | 0.161 | 2,515 ms | $0.00518 | 0 |
| specialty_reader=llm (openai/gpt-oss-120b) · triage_screen=llm (openai/gpt-oss-120b) · checklist=criteria (version 0.1.1, its pack digest not recorded) | urgency=urgent 22 of 22 [84.6-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 21 of 22 [77.2-99.9%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 0.980 | 0.168 | 1,599 ms | $0.00024 | 2 (derived_input_unavailable 1, invalid_response 1) |
| specialty_reader=llm (openai/gpt-oss-120b) · triage_screen=rules_and_llm (openai/gpt-oss-120b) · checklist=criteria (version 0.1.1, its pack digest not recorded) | urgency=urgent 22 of 22 [84.6-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 22 of 22 [84.6-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 4 of 100 [1.1-9.9%] | 0.997 | 0.151 | 1,505 ms | $0.00024 | 2 (derived_input_unavailable 1, invalid_response 1) |
| specialty_reader=naive_bayes · triage_screen=rules · checklist=criteria (version 0.1.0, its pack digest not recorded) | urgency=urgent 22 of 22 [84.6-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 22 of 22 [84.6-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 1.000 | 0.059 | 2.70 ms | $0.00000 | 0 |

Accuracy and false automation are n/a on a row recorded before leaderboards kept the counts they are of (mendari.leaderboard/1).

Other questions (accuracy / ECE):

| Configuration | `clear_question` | `specialty` | `urgency` |
|---|---|---|---|
| specialty_reader=jev (jev-1.13.0) · triage_screen=jev (jev-1.13.0) · checklist=criteria (version 0.1.0, its pack digest not recorded) | 99 of 100 [94.6-99.97%] / 0.076 | 100 of 100 [96.4-100.0%] / 0.009 | 100 of 100 [96.4-100.0%] / 0.025 |
| specialty_reader=jev (jev-1.13.0) · triage_screen=rules_and_jev (jev-1.13.0) · checklist=criteria (version 0.1.1, its pack digest not recorded) | 100 of 100 [96.4-100.0%] / 0.013 | 100 of 100 [96.4-100.0%] / 0.009 | 100 of 100 [96.4-100.0%] / 0.033 |
| specialty_reader=llm (anthropic/claude-haiku-4.5) · triage_screen=llm (anthropic/claude-haiku-4.5) · checklist=criteria (version 0.1.0, its pack digest not recorded) | 100 of 100 [96.4-100.0%] / 0.039 | 98 of 100 [93.0-99.8%] / 0.064 | 100 of 100 [96.4-100.0%] / 0.089 |
| specialty_reader=llm (anthropic/claude-haiku-4.5) · triage_screen=rules_and_llm (anthropic/claude-haiku-4.5) · checklist=criteria (version 0.1.1, its pack digest not recorded) | 100 of 100 [96.4-100.0%] / 0.014 | 99 of 100 [94.6-99.97%] / 0.076 | 100 of 100 [96.4-100.0%] / 0.070 |
| specialty_reader=llm (anthropic/claude-sonnet-5) · triage_screen=llm (anthropic/claude-sonnet-5) · checklist=criteria (version 0.1.0, its pack digest not recorded) | 100 of 100 [96.4-100.0%] / 0.085 | 99 of 100 [94.6-99.97%] / 0.136 | 100 of 100 [96.4-100.0%] / 0.087 |
| specialty_reader=llm (anthropic/claude-sonnet-5) · triage_screen=rules_and_llm (anthropic/claude-sonnet-5) · checklist=criteria (version 0.1.1, its pack digest not recorded) | 100 of 100 [96.4-100.0%] / 0.014 | 99 of 100 [94.6-99.97%] / 0.137 | 100 of 100 [96.4-100.0%] / 0.073 |
| specialty_reader=llm (openai/gpt-oss-120b) · triage_screen=llm (openai/gpt-oss-120b) · checklist=criteria (version 0.1.1, its pack digest not recorded) | 99 of 100 [94.6-99.97%] / 0.052 | 97 of 100 [91.5-99.4%] / 0.136 | 100 of 100 [96.4-100.0%] / 0.096 |
| specialty_reader=llm (openai/gpt-oss-120b) · triage_screen=rules_and_llm (openai/gpt-oss-120b) · checklist=criteria (version 0.1.1, its pack digest not recorded) | 100 of 100 [96.4-100.0%] / 0.016 | 98 of 100 [93.0-99.8%] / 0.135 | 100 of 100 [96.4-100.0%] / 0.074 |
| specialty_reader=naive_bayes · triage_screen=rules · checklist=criteria (version 0.1.0, its pack digest not recorded) | 100 of 100 [96.4-100.0%] / 0.012 | 100 of 100 [96.4-100.0%] / 0.002 | 100 of 100 [96.4-100.0%] / 0.024 |

**`urgent_referrals`** (n=30; dataset hash not recorded; k of n [exact 95% interval, Clopper-Pearson])

| Configuration | Critical detected | False automation | Accuracy | AUTO | Macro-F1 | ECE | p50 latency | Cost / decision | Errors |
|---|---|---|---|---|---|---|---|---|---|
| specialty_reader=jev (jev-1.13.0) · triage_screen=jev (jev-1.13.0) · checklist=criteria (version 0.1.0, its pack digest not recorded) | urgency=urgent 16 of 16 [79.4-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 16 of 16 [79.4-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 30 [0.0-11.6%] | 0.987 | 0.069 | 181 ms | $0.00004 | 0 |
| specialty_reader=jev (jev-1.13.0) · triage_screen=rules_and_jev (jev-1.13.0) · checklist=criteria (version 0.1.1, its pack digest not recorded) | urgency=urgent 16 of 16 [79.4-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 16 of 16 [79.4-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 14 of 30 [28.3-65.7%] | 1.000 | 0.035 | 224 ms | $0.00004 | 0 |
| specialty_reader=llm (anthropic/claude-haiku-4.5) · triage_screen=llm (anthropic/claude-haiku-4.5) · checklist=criteria (version 0.1.0, its pack digest not recorded) | urgency=urgent 16 of 16 [79.4-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 16 of 16 [79.4-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 30 [0.0-11.6%] | 1.000 | 0.102 | 9,932 ms | $0.00585 | 0 |
| specialty_reader=llm (anthropic/claude-haiku-4.5) · triage_screen=rules_and_llm (anthropic/claude-haiku-4.5) · checklist=criteria (version 0.1.1, its pack digest not recorded) | urgency=urgent 16 of 16 [79.4-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 16 of 16 [79.4-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 7 of 30 [9.9-42.3%] | 1.000 | 0.072 | 9,068 ms | $0.00581 | 0 |
| specialty_reader=llm (anthropic/claude-sonnet-5) · triage_screen=llm (anthropic/claude-sonnet-5) · checklist=criteria (version 0.1.0, its pack digest not recorded) | urgency=urgent 16 of 16 [79.4-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 16 of 16 [79.4-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 30 [0.0-11.6%] | 1.000 | 0.127 | 2,545 ms | $0.00517 | 0 |
| specialty_reader=llm (anthropic/claude-sonnet-5) · triage_screen=rules_and_llm (anthropic/claude-sonnet-5) · checklist=criteria (version 0.1.1, its pack digest not recorded) | urgency=urgent 16 of 16 [79.4-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 16 of 16 [79.4-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 3 of 30 [2.1-26.5%] | 1.000 | 0.087 | 2,460 ms | $0.00518 | 0 |
| specialty_reader=llm (openai/gpt-oss-120b) · triage_screen=llm (openai/gpt-oss-120b) · checklist=criteria (version 0.1.0, its pack digest not recorded) | urgency=urgent 16 of 16 [79.4-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 16 of 16 [79.4-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 30 [0.0-11.6%] | 1.000 | 0.143 | 1,490 ms | $0.00024 | 0 |
| specialty_reader=llm (openai/gpt-oss-120b) · triage_screen=rules_and_llm (openai/gpt-oss-120b) · checklist=criteria (version 0.1.1, its pack digest not recorded) | urgency=urgent 16 of 16 [79.4-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 16 of 16 [79.4-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 2 of 30 [0.8-22.1%] | 1.000 | 0.083 | 1,531 ms | $0.00024 | 0 |
| specialty_reader=naive_bayes · triage_screen=rules · checklist=criteria (version 0.1.0, its pack digest not recorded) | urgency=urgent 16 of 16 [79.4-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 16 of 16 [79.4-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 30 [0.0-11.6%] | 1.000 | 0.058 | 2.76 ms | $0.00000 | 0 |

Accuracy and false automation are n/a on a row recorded before leaderboards kept the counts they are of (mendari.leaderboard/1).

Other questions (accuracy / ECE):

| Configuration | `clear_question` | `specialty` | `urgency` |
|---|---|---|---|
| specialty_reader=jev (jev-1.13.0) · triage_screen=jev (jev-1.13.0) · checklist=criteria (version 0.1.0, its pack digest not recorded) | 29 of 30 [82.8-99.9%] / 0.073 | 28 of 30 [77.9-99.2%] / 0.053 | 30 of 30 [88.4-100.0%] / 0.001 |
| specialty_reader=jev (jev-1.13.0) · triage_screen=rules_and_jev (jev-1.13.0) · checklist=criteria (version 0.1.1, its pack digest not recorded) | 30 of 30 [88.4-100.0%] / 0.014 | 28 of 30 [77.9-99.2%] / 0.054 | 30 of 30 [88.4-100.0%] / 0.009 |
| specialty_reader=llm (anthropic/claude-haiku-4.5) · triage_screen=llm (anthropic/claude-haiku-4.5) · checklist=criteria (version 0.1.0, its pack digest not recorded) | 30 of 30 [88.4-100.0%] / 0.042 | 28 of 30 [77.9-99.2%] / 0.087 | 30 of 30 [88.4-100.0%] / 0.064 |
| specialty_reader=llm (anthropic/claude-haiku-4.5) · triage_screen=rules_and_llm (anthropic/claude-haiku-4.5) · checklist=criteria (version 0.1.1, its pack digest not recorded) | 30 of 30 [88.4-100.0%] / 0.025 | 28 of 30 [77.9-99.2%] / 0.075 | 30 of 30 [88.4-100.0%] / 0.038 |
| specialty_reader=llm (anthropic/claude-sonnet-5) · triage_screen=llm (anthropic/claude-sonnet-5) · checklist=criteria (version 0.1.0, its pack digest not recorded) | 30 of 30 [88.4-100.0%] / 0.082 | 29 of 30 [82.8-99.9%] / 0.136 | 30 of 30 [88.4-100.0%] / 0.059 |
| specialty_reader=llm (anthropic/claude-sonnet-5) · triage_screen=rules_and_llm (anthropic/claude-sonnet-5) · checklist=criteria (version 0.1.1, its pack digest not recorded) | 30 of 30 [88.4-100.0%] / 0.020 | 29 of 30 [82.8-99.9%] / 0.117 | 30 of 30 [88.4-100.0%] / 0.039 |
| specialty_reader=llm (openai/gpt-oss-120b) · triage_screen=llm (openai/gpt-oss-120b) · checklist=criteria (version 0.1.0, its pack digest not recorded) | 30 of 30 [88.4-100.0%] / 0.056 | 27 of 30 [73.5-97.9%] / 0.125 | 30 of 30 [88.4-100.0%] / 0.099 |
| specialty_reader=llm (openai/gpt-oss-120b) · triage_screen=rules_and_llm (openai/gpt-oss-120b) · checklist=criteria (version 0.1.1, its pack digest not recorded) | 30 of 30 [88.4-100.0%] / 0.018 | 30 of 30 [88.4-100.0%] / 0.110 | 30 of 30 [88.4-100.0%] / 0.046 |
| specialty_reader=naive_bayes · triage_screen=rules · checklist=criteria (version 0.1.0, its pack digest not recorded) | 30 of 30 [88.4-100.0%] / 0.012 | 29 of 30 [82.8-99.9%] / 0.067 | 30 of 30 [88.4-100.0%] / 0.032 |

**`urgent_referrals_holdout`** (n=24; dataset hash not recorded; k of n [exact 95% interval, Clopper-Pearson])

| Configuration | Critical detected | False automation | Accuracy | AUTO | Macro-F1 | ECE | p50 latency | Cost / decision | Errors |
|---|---|---|---|---|---|---|---|---|---|
| specialty_reader=jev (jev-1.13.0) · triage_screen=jev (jev-1.13.0) · checklist=criteria (version 0.1.0, its pack digest not recorded) | urgency=urgent 12 of 12 [73.5-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 12 of 12 [73.5-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 24 [0.0-14.2%] | 0.984 | 0.096 | 215 ms | $0.00004 | 0 |
| specialty_reader=jev (jev-1.13.0) · triage_screen=rules_and_jev (jev-1.13.0) · checklist=criteria (version 0.1.1, its pack digest not recorded) | urgency=urgent 12 of 12 [73.5-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 12 of 12 [73.5-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 10 of 24 [22.1-63.4%] | 1.000 | 0.060 | 231 ms | $0.00004 | 0 |
| specialty_reader=llm (anthropic/claude-haiku-4.5) · triage_screen=llm (anthropic/claude-haiku-4.5) · checklist=criteria (version 0.1.0, its pack digest not recorded) | urgency=urgent 12 of 12 [73.5-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 12 of 12 [73.5-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 24 [0.0-14.2%] | 1.000 | 0.123 | 9,838 ms | $0.00590 | 0 |
| specialty_reader=llm (anthropic/claude-haiku-4.5) · triage_screen=rules_and_llm (anthropic/claude-haiku-4.5) · checklist=criteria (version 0.1.1, its pack digest not recorded) | urgency=urgent 12 of 12 [73.5-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 12 of 12 [73.5-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 6 of 24 [9.8-46.7%] | 1.000 | 0.104 | 11,435 ms | $0.00644 | 0 |
| specialty_reader=llm (anthropic/claude-sonnet-5) · triage_screen=llm (anthropic/claude-sonnet-5) · checklist=criteria (version 0.1.0, its pack digest not recorded) | urgency=urgent 12 of 12 [73.5-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 12 of 12 [73.5-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 24 [0.0-14.2%] | 0.984 | 0.120 | 2,674 ms | $0.00526 | 0 |
| specialty_reader=llm (anthropic/claude-sonnet-5) · triage_screen=rules_and_llm (anthropic/claude-sonnet-5) · checklist=criteria (version 0.1.1, its pack digest not recorded) | urgency=urgent 12 of 12 [73.5-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 12 of 12 [73.5-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 1 of 24 [0.1-21.1%] | 1.000 | 0.114 | 2,460 ms | $0.00520 | 0 |
| specialty_reader=llm (openai/gpt-oss-120b) · triage_screen=llm (openai/gpt-oss-120b) · checklist=criteria (version 0.1.0, its pack digest not recorded) | urgency=urgent 12 of 12 [73.5-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 12 of 12 [73.5-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 24 [0.0-14.2%] | 1.000 | 0.150 | 1,662 ms | $0.00024 | 0 |
| specialty_reader=llm (openai/gpt-oss-120b) · triage_screen=rules_and_llm (openai/gpt-oss-120b) · checklist=criteria (version 0.1.1, its pack digest not recorded) | urgency=urgent 12 of 12 [73.5-100.0%] (0 automated; insufficient evidence); next_step=urgent_review 12 of 12 [73.5-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 2 of 24 [1.0-27.0%] | 1.000 | 0.111 | 1,907 ms | $0.00024 | 0 |
| specialty_reader=naive_bayes · triage_screen=rules · checklist=criteria (version 0.1.0, its pack digest not recorded) | urgency=urgent 7 of 12 [27.7-84.8%] (0 automated; insufficient evidence); next_step=urgent_review 7 of 12 [27.7-84.8%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 24 [0.0-14.2%] | 0.819 | 0.225 | 2.75 ms | $0.00000 | 0 |

Accuracy and false automation are n/a on a row recorded before leaderboards kept the counts they are of (mendari.leaderboard/1).

Other questions (accuracy / ECE):

| Configuration | `clear_question` | `specialty` | `urgency` |
|---|---|---|---|
| specialty_reader=jev (jev-1.13.0) · triage_screen=jev (jev-1.13.0) · checklist=criteria (version 0.1.0, its pack digest not recorded) | 23 of 24 [78.9-99.9%] / 0.083 | 24 of 24 [85.8-100.0%] / 0.010 | 24 of 24 [85.8-100.0%] / 0.001 |
| specialty_reader=jev (jev-1.13.0) · triage_screen=rules_and_jev (jev-1.13.0) · checklist=criteria (version 0.1.1, its pack digest not recorded) | 24 of 24 [85.8-100.0%] / 0.035 | 24 of 24 [85.8-100.0%] / 0.010 | 24 of 24 [85.8-100.0%] / 0.010 |
| specialty_reader=llm (anthropic/claude-haiku-4.5) · triage_screen=llm (anthropic/claude-haiku-4.5) · checklist=criteria (version 0.1.0, its pack digest not recorded) | 24 of 24 [85.8-100.0%] / 0.036 | 24 of 24 [85.8-100.0%] / 0.066 | 24 of 24 [85.8-100.0%] / 0.083 |
| specialty_reader=llm (anthropic/claude-haiku-4.5) · triage_screen=rules_and_llm (anthropic/claude-haiku-4.5) · checklist=criteria (version 0.1.1, its pack digest not recorded) | 24 of 24 [85.8-100.0%] / 0.027 | 23 of 24 [78.9-99.9%] / 0.071 | 24 of 24 [85.8-100.0%] / 0.054 |
| specialty_reader=llm (anthropic/claude-sonnet-5) · triage_screen=llm (anthropic/claude-sonnet-5) · checklist=criteria (version 0.1.0, its pack digest not recorded) | 23 of 24 [78.9-99.9%] / 0.106 | 24 of 24 [85.8-100.0%] / 0.125 | 24 of 24 [85.8-100.0%] / 0.068 |
| specialty_reader=llm (anthropic/claude-sonnet-5) · triage_screen=rules_and_llm (anthropic/claude-sonnet-5) · checklist=criteria (version 0.1.1, its pack digest not recorded) | 24 of 24 [85.8-100.0%] / 0.027 | 24 of 24 [85.8-100.0%] / 0.117 | 24 of 24 [85.8-100.0%] / 0.053 |
| specialty_reader=llm (openai/gpt-oss-120b) · triage_screen=llm (openai/gpt-oss-120b) · checklist=criteria (version 0.1.0, its pack digest not recorded) | 24 of 24 [85.8-100.0%] / 0.070 | 23 of 24 [78.9-99.9%] / 0.145 | 24 of 24 [85.8-100.0%] / 0.097 |
| specialty_reader=llm (openai/gpt-oss-120b) · triage_screen=rules_and_llm (openai/gpt-oss-120b) · checklist=criteria (version 0.1.1, its pack digest not recorded) | 24 of 24 [85.8-100.0%] / 0.015 | 23 of 24 [78.9-99.9%] / 0.135 | 24 of 24 [85.8-100.0%] / 0.072 |
| specialty_reader=naive_bayes · triage_screen=rules · checklist=criteria (version 0.1.0, its pack digest not recorded) | 22 of 24 [73.0-99.0%] / 0.071 | 21 of 24 [67.6-97.3%] / 0.096 | 19 of 24 [57.8-92.9%] / 0.182 |


## Known failure modes

- Through FHIR (CDS Hooks, the SMART launch, `$decide`), the pack decides only a ServiceRequest whose first category is the fictional referral code of its synthetic examples (fhir/mapping.yaml, `focus_where`). No real EHR referral carries it, so until a site lists the code its EHR marks a referral with (often a SNOMED CT code), no real referral is decided. Pack inputs sent directly are not affected.
- The checklist looks only for the document codes listed in logic/checklist.yaml. A visit note filed under another code (e.g. a history and physical, LOINC 34117-2) reads as missing, so a complete referral goes back to the referrer; the bundled local-site example shows this. Add a site's codes before use.
- The keyword urgency screen misses phrasings it does not list. On hand-written referrals it caught 16/16 urgent ones in the set it was written on but 7/12 in the held-out set. So the policy allows AUTO only while the screen includes a model (`automation_requires` with `triage_screen=rules_and_jev` or `rules_and_llm`, where either one flagging a referral as possibly urgent sends it to review). With the rules alone, every referral goes to a person.
- The naive Bayes specialty reader was trained on synthetic referrals only. Shorthand and a site's own case mix lower its accuracy; with a less confident specialty, fewer referrals are automated.
- Requirements depend on the suggested specialty. If the specialty is wrong, the wrong requirements are checked, but that referral's lower confidence usually keeps it out of automation.
- The overall probability treats every answer as independent.
- The public benchmark is generated from templates written with the pack, so it overstates real-world accuracy. The hand-written urgent sets are the better guide, and the held-out one was written before the rules.

## Evidence and provenance

- **Synthetic public benchmark**: evals/benchmark.json, the `test` split generated by mendari.sandbox.synthetic_referral; ground truth comes from the generated facts.
- **Hand-written urgent referral sets**: data/urgent_referrals.jsonl (used to write the urgency rules) and data/urgent_referrals_holdout.jsonl (written before the rules and never used to tune them): urgent referral texts across the eight specialties, and routine look-alikes that mention alarming words about something remote, stable or reassuring.

## Version history

Renamed on 2026-09-30, when the project became Mendari: this pack was `openhealth/referral-router`.
Its id, its publisher's name and its `https://mendari.co/...` identifiers changed; its criteria,
rules, models, data and results did not.

#### 0.2.1

- A CDS Hooks card only when the person signing the referral can act
  (`cds_hooks.card.show: [return_for_info, urgent_review]`, LP-23j; the owner confirmed the
  list on 2026-11-06): a referral to send back with what is missing, and one to review today.
  A referral to schedule, for a coordinator, or with no suggestion is decided and audited as
  before, for the referral office, and shows no card. A record that could not be read still
  shows its `info` card for every referral. Of the 300 referrals the FHIR decisions golden
  holds (the bundled local-site example, sent through FHIR), 235 show a card and 65 do not (53
  to schedule, 12 for a coordinator); each of the 300 showed one before.
- Criteria, rules, providers, policy, models and data are unchanged; every decision is identical
  (the benchmark's figures, its ledger and the FHIR decisions golden). The digest changes because
  the manifest does.

### 0.2.0

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

### 0.1.4

- `code_systems` declares the systems the pack uses: SNOMED CT, with its licence terms (a pack
  naming a licensed code system now has to say them), and LOINC.
- Criteria, rules, providers, policy, models and data are unchanged; every decision is identical.
  The digest changes because the manifest does.

### 0.1.3

- Every surface now says what the pack is: "Synthetic example. Not clinically reviewed. Not for
  patient care." It is derived from the new `card.provenance: synthetic`, never written by the
  pack (the Decision Card's first line, the dashboard, Try it, the review queue, CDS Hooks cards
  and discovery, the registry and the Hub).
- Each question has a `display` name, so people read words instead of keys, and one label has a name (`return_for_info`).
- Criteria, rules, providers, policy, models and data are unchanged; every decision is identical.
  The digest changes because the manifest does.

### 0.1.2

- `specialty_reader`: a `cascade` variant (naive Bayes when it is at least 95% sure, else Jev),
  as the Patient Inbox Router's router has. When Jev cannot answer, the referral still gets
  naive Bayes' specialty and goes to a person, instead of abstaining. A Mendari server with Jev
  set up runs it. The default (`naive_bayes`), questions, policy, rules, data and model are
  unchanged.

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

*Generated by `mendari card`. A Decision Card describes validation on the data named above. It is not evidence of performance at your site; run local validation first.*
