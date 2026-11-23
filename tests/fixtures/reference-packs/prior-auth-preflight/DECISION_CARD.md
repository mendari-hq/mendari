# Decision Card: Prior Authorization Preflight

**Synthetic example. Not clinically reviewed. Not for patient care.**

*Research preview. Not a medical device. Not for emergencies: patients should call 911.*

`mendari/prior-auth-preflight` · version **0.3.1** · Apache-2.0 · published by Mendari reference packs (community, self-declared)

> Checks an order against the patient's payer before it is sent for prior authorization: does this payer require authorization for this service, and does the record have what the payer asks for? Complete requests are queued for submission; the gaps in the others are listed.

| | |
|---|---|
| Source | Maintained by Mendari |
| Risk level | **1**, clinical information processing (clinical information) |
| Automation | AUTO permitted by policy thresholds; every decision is audited. Only while `notes` is `rules_and_jev` or `rules_and_llm`; otherwise a person reviews. |
| Agent digest | `sha256:e2b2503fb11ea54a4196804bb4d04e6be3612908d7253c9ed4e295e2e77a7e77` |
| Last validation | 2026-11-07 on `test` (n=600, sha256 `e74e45ea35d5…`) |

## Intended use

Preflight of outpatient orders that need prior authorization, when the order is signed (CDS Hooks `order-sign`) or before an authorization specialist submits it: identify the payer's requirement set for the service, check each required element in the record, queue complete requests for submission and list the gaps in the others.

**Why risk level 1:** The pack decides whether a prior-authorization request is ready to submit, not whether the service is appropriate. A wrong "ready" leads to a denial and a resubmission; a wrong gap costs staff a few minutes. A possible red flag (which may call for urgent imaging and an expedited review) always goes to a person.

## Intended users

- Prior-authorization specialists and front-office staff
- Clinicians signing orders
- Health IT teams integrating payer requirements with the EHR

## Patient population

Adults with an order for a lumbar spine MRI, a knee arthroscopy or a sleep study, covered by one of the two synthetic payers the pack knows, with clinic notes in English.

## Out of scope

- Deciding medical necessity or approving anything; the payer decides.
- Any real payer's policy. Acme and Beacon, their prior-authorization lists and their documentation requirements are synthetic, loosely modelled on common imaging and procedure criteria, and were not written or reviewed by a clinician or a payer.
- Medication prior authorization (orders are ServiceRequests only), and submitting the request itself (Da Vinci PAS is later standards work).

## Inputs

| Field | Type | Required |
|---|---|---|
| `as_of` | string | no |
| `patient` | object | no |
| `patient.age` | integer | no |
| `patient.sex` | `female` \| `male` \| `other` \| `unknown` | no |
| `order` | object | no |
| `order.code` | string | no |
| `order.name` | string | no |
| `coverage` | object | no |
| `coverage.payer` | string | no |
| `conditions` | array | no |
| `procedures` | array | no |
| `notes` | string | no |

FHIR R4 mapping: `fhir/mapping.yaml`.

Code systems:

- Mendari fictional procedure codes: invented for this synthetic example; not CPT, never for billing. A site maps its own codes to these. (`https://mendari.co/fhir/CodeSystem/fictional-procedures`).
- ICD-10-CM (U.S. National Center for Health Statistics: free to use; adapted from the World Health Organization's copyrighted ICD-10, with WHO's authorization, for U.S. government purposes) (`http://hl7.org/fhir/sid/icd-10-cm`).
- Mendari fictional payer identifiers: the synthetic payers' own identifiers (acme, beacon), by which the FHIR mapping reads a payer's Organization; not a real payer identifier system. (`https://mendari.co/fhir/sid/fictional-payers`).

## Outputs

**`service`**: Which requirement set does the ordered service fall under?

| Label | Criteria |
|---|---|
| `lumbar_mri` | MRI of the lumbar spine (codes FX101, FX102, FX103). |
| `knee_arthroscopy` | Knee arthroscopy (codes FX201, FX202). |
| `sleep_study` | Attended sleep study (codes FX301, FX302). |
| `other` | Any other service; the pack has no requirement set for it. |

**`payer`**: Which payer covers the patient?

| Label | Criteria |
|---|---|
| `acme` | Acme Health (synthetic). |
| `beacon` | Beacon Care (synthetic). |
| `other` | Any other payer; the pack has no requirement set for it. |

**`diagnosis_supports`**: Is a diagnosis that supports the ordered service coded?

| Label | Criteria |
|---|---|
| `yes` | A supporting diagnosis is coded (lumbar MRI M54.1x or M51.x; knee arthroscopy S83.2x or M23.2x; sleep study G47.33 or R06.83). |
| `no` | No supporting diagnosis is coded. |
| `unknown` | The record does not say. |

**`prior_imaging`**: Is the imaging the payer asks for on file from the last 180 days?

| Label | Criteria |
|---|---|
| `yes` | A lumbar spine X-ray (code FX111) for a lumbar MRI, or a knee MRI (FX211 to FX213) for a knee arthroscopy, in the last 180 days. |
| `no` | None on file, or older than 180 days. |
| `unknown` | Imaging is on file without a date. |

**`conservative_therapy`**: Do the notes document at least 6 weeks of conservative treatment for the problem?

| Label | Criteria |
|---|---|
| `yes` | At least 6 weeks of physical therapy, a home exercise programme, anti-inflammatory or pain medicines, or injections, e.g. "8 weeks of physical therapy" or "physiotherapy for 2 months". |
| `no` | No conservative treatment, or less than 6 weeks of it, e.g. "started PT 2 weeks ago" or "no treatment tried yet". |
| `unknown` | Treatment is mentioned but its length is not stated or cannot be worked out, e.g. "has tried some physical therapy". |

**`red_flag`**: Do the notes document a red flag that calls for imaging without waiting for conservative treatment?

| Label | Criteria |
|---|---|
| `yes` | A history of cancer, unexplained weight loss, fever with back pain, injection drug use, significant trauma such as a fall from height or a car accident, a progressive neurological deficit such as worsening weakness or foot drop, or saddle numbness or new bladder or bowel dysfunction. |
| `no` | None documented, documented as absent ("no red flags", "denies fever"), resolved and unrelated, or only in a family member. |
| `unknown` | A possible red flag is mentioned but unclear. |

**`mechanical_symptoms`**: Do the notes document mechanical symptoms of the knee?

| Label | Criteria |
|---|---|
| `yes` | The knee locks, catches, gives way or buckles. |
| `no` | None documented or documented as absent, e.g. pain only, swelling, or painless clicking. |
| `unknown` | Mentioned but unclear. |

**`sleep_symptoms`**: Do the notes document symptoms of obstructive sleep apnoea?

| Label | Criteria |
|---|---|
| `yes` | Loud snoring, witnessed pauses in breathing during sleep, or excessive daytime sleepiness such as falling asleep at work or while driving. |
| `no` | None documented, documented as absent, or insomnia without these symptoms. |
| `unknown` | Tiredness or poor sleep without any of these symptoms. |

**`next_step`** (primary): What should happen to the prior-authorization request next?

Derived from `service`, `payer`, `diagnosis_supports`, `prior_imaging`, `conservative_therapy`, `red_flag`, `mechanical_symptoms`, `sleep_symptoms` by a decision table (the first matching row wins; the members' answers are treated as independent):

1. any of `service`, `payer` is `other` → `not_checked`
2. any of `payer` is `beacon`, when `service` is `sleep_study` → `not_required`
3. any of `diagnosis_supports` is `no` or `unknown` → `needs_attention`
4. any of `conservative_therapy` is `no` or `unknown`, when `service` is `lumbar_mri` and `red_flag` is `no` or `unknown` → `needs_attention`
5. any of `conservative_therapy` is `no` or `unknown`, when `service` is `knee_arthroscopy` → `needs_attention`
6. any of `prior_imaging` is `no` or `unknown`, when `service` is `lumbar_mri` or `knee_arthroscopy` and `payer` is `acme` → `needs_attention`
7. any of `mechanical_symptoms` is `no` or `unknown`, when `service` is `knee_arthroscopy` → `needs_attention`
8. any of `sleep_symptoms` is `no` or `unknown`, when `service` is `sleep_study` → `needs_attention`
9. otherwise → `submit_ready`

| Label | Criteria |
|---|---|
| `submit_ready` | The payer requires prior authorization and everything it asks for is in the record, so the request can be submitted. |
| `needs_attention` | Something the payer asks for is missing or unclear (listed in the reasons), so staff add it before submitting. |
| `not_required` | This payer does not require prior authorization for this service. |
| `not_checked` | The pack has no requirement set for this payer or service. |

Every decision is one of `AUTO`, `HUMAN_REVIEW`, `ABSTAIN` or `BLOCK`, with a priority (`routine`, `urgent`, `asap`, `stat`) and an audit record.

## Decision policy

- AUTO when `next_step` confidence ≥ **0.90**; human review ≥ **0.40**; abstain below.
- `needs_attention` is never automated.
- `not_checked` is never automated.
- `not_required` needs ≥ **0.95** for AUTO.
- AUTO only while `notes` is `rules_and_jev` or `rules_and_llm`; otherwise a person reviews.
- Rule **possible-red-flag**: P(`red_flag` is `yes`) ≥ 0.30 ⇒ `HUMAN_REVIEW`, priority `urgent`. The notes may document a red flag. Consider urgent imaging and an expedited review instead of the routine request.
- Fallback (ABSTAIN / BLOCK): The authorization specialist checks the payer's requirements as today.

## Providers

| Slot | Answers | Default | Alternatives |
|---|---|---|---|
| `structured` | service, payer, diagnosis_supports, prior_imaging | `criteria` (criteria) | none |
| `notes` | conservative_therapy, red_flag, mechanical_symptoms, sleep_symptoms | `rules` (rules) | `jev` (jev), `llm` (llm), `rules_and_jev` (any_of), `rules_and_llm` (any_of) |

## Validation

Benchmark `test` (n=600) with structured=`criteria`, notes=`rules`. Each proportion: k of n [exact 95% interval, Clopper-Pearson].

| Metric | Value |
|---|---|
| `next_step` accuracy | 600 of 600 [99.4-100.0%] |
| Macro-F1 | 1.000 |
| Expected calibration error | 0.045 |
| Expected calibration error, equal-mass bins | 0.045 |
| Brier score | 0.004 [0.004-0.005, bootstrap] |
| AUTO | 0 of 600 [0.0-0.6%] |
| HUMAN_REVIEW | 600 of 600 [99.4-100.0%] |
| ABSTAIN | 0 of 600 [0.0-0.6%] |
| BLOCK | 0 of 600 [0.0-0.6%] |
| AUTO decisions correct | no cases |
| False automation (of labelled cases) | 0 of 600 [0.0-0.6%] |
| Expected human override (of reviewed) | 0 of 600 [0.0-0.6%] |
| Latency p50 / p95 | 3.72 / 6.02 ms |
| Cost per decision | $0.00000 |
| Critical `red_flag=yes` | 25 of 25 [86.3-100.0%] detected, 0 automated (insufficient evidence: under 100 positives) |

Per-label performance:

| Label | Precision | Recall | F1 |
|---|---|---|---|
| `submit_ready` | 180 of 180 [98.0-100.0%] | 180 of 180 [98.0-100.0%] | 1.000 |
| `needs_attention` | 221 of 221 [98.3-100.0%] | 221 of 221 [98.3-100.0%] | 1.000 |
| `not_required` | 57 of 57 [93.7-100.0%] | 57 of 57 [93.7-100.0%] | 1.000 |
| `not_checked` | 142 of 142 [97.4-100.0%] | 142 of 142 [97.4-100.0%] | 1.000 |

Subgroups:

| Subgroup | n | Accuracy | AUTO | Abstention |
|---|---|---|---|---|
| site=public | 600 | 600 of 600 [99.4-100.0%] | 0 of 600 [0.0-0.6%] | 0 of 600 [0.0-0.6%] |
| service=knee_arthroscopy | 160 | 160 of 160 [97.7-100.0%] | 0 of 160 [0.0-2.3%] | 0 of 160 [0.0-2.3%] |
| service=lumbar_mri | 214 | 214 of 214 [98.3-100.0%] | 0 of 214 [0.0-1.7%] | 0 of 214 [0.0-1.7%] |
| service=other | 89 | 89 of 89 [95.9-100.0%] | 0 of 89 [0.0-4.1%] | 0 of 89 [0.0-4.1%] |
| service=sleep_study | 137 | 137 of 137 [97.3-100.0%] | 0 of 137 [0.0-2.7%] | 0 of 137 [0.0-2.7%] |
| payer=acme | 283 | 283 of 283 [98.7-100.0%] | 0 of 283 [0.0-1.3%] | 0 of 283 [0.0-1.3%] |
| payer=beacon | 255 | 255 of 255 [98.6-100.0%] | 0 of 255 [0.0-1.4%] | 0 of 255 [0.0-1.4%] |
| payer=other | 62 | 62 of 62 [94.2-100.0%] | 0 of 62 [0.0-5.8%] | 0 of 62 [0.0-5.8%] |
| style=prose | 600 | 600 of 600 [99.4-100.0%] | 0 of 600 [0.0-0.6%] | 0 of 600 [0.0-0.6%] |
| age_band=18-39 | 188 | 188 of 188 [98.1-100.0%] | 0 of 188 [0.0-1.9%] | 0 of 188 [0.0-1.9%] |
| age_band=40-64 | 253 | 253 of 253 [98.6-100.0%] | 0 of 253 [0.0-1.4%] | 0 of 253 [0.0-1.4%] |
| age_band=65+ | 159 | 159 of 159 [97.7-100.0%] | 0 of 159 [0.0-2.3%] | 0 of 159 [0.0-2.3%] |
| sex=female | 277 | 277 of 277 [98.7-100.0%] | 0 of 277 [0.0-1.3%] | 0 of 277 [0.0-1.3%] |
| sex=male | 323 | 323 of 323 [98.9-100.0%] | 0 of 323 [0.0-1.1%] | 0 of 323 [0.0-1.1%] |

Calibration (reliability):

| Confidence bin | Mean confidence | Correct |
|---|---|---|
| 0.9 to 1.0 | 0.955 | 600 of 600 [99.4-100.0%] |

Threshold sweep (if everything ≥ threshold were automated):

| Threshold | Would be automated | Of those, correct |
|---|---|---|
| 0.50 | 600 of 600 [99.4-100.0%] | 600 of 600 [99.4-100.0%] |
| 0.60 | 600 of 600 [99.4-100.0%] | 600 of 600 [99.4-100.0%] |
| 0.70 | 600 of 600 [99.4-100.0%] | 600 of 600 [99.4-100.0%] |
| 0.80 | 600 of 600 [99.4-100.0%] | 600 of 600 [99.4-100.0%] |
| 0.85 | 600 of 600 [99.4-100.0%] | 600 of 600 [99.4-100.0%] |
| 0.90 | 600 of 600 [99.4-100.0%] | 600 of 600 [99.4-100.0%] |
| 0.95 | 361 of 600 [56.1-64.1%] | 361 of 361 [99.0-100.0%] |
| 0.97 | 247 of 600 [37.2-45.2%] | 247 of 247 [98.5-100.0%] |
| 0.99 | 9 of 600 [0.7-2.8%] | 9 of 9 [66.4-100.0%] |

## Provider comparison

Public set: regression check, not a ranking. Nothing here is graded: the agent's own sets are public, in the repository and in the package, so a configuration can be tuned to them, and grades and rankings are given only on frozen hidden sets. The rows are a listing order, not a ranking: those measured on this version of the agent, on records where a critical label has at least 100 positives, come first, the critical cases caught first and never accuracy alone; every other row follows, by configuration name.

Each table below is one dataset at one size, and the configurations in a table that records the dataset's hash ran on exactly the same records. Remote models are named because they are a deployment choice. Synthetic and hand-written data: compare configurations with each other, not with performance at a real site. A row measured on an earlier version of the agent says so beside its configuration: its AUTO and false automation followed that version's policy.

**`documentation_notes`** (n=32; dataset hash not recorded; k of n [exact 95% interval, Clopper-Pearson])

| Configuration | Critical detected | False automation | Accuracy | AUTO | Macro-F1 | ECE | p50 latency | Cost / decision | Errors |
|---|---|---|---|---|---|---|---|---|---|
| structured=criteria · notes=jev (jev-1.13.0) (version 0.1.0, its agent digest not recorded) | 7 of 7 [59.0-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 32 [0.0-10.9%] | 1.000 | 0.058 | 250 ms | $0.00003 | 0 |
| structured=criteria · notes=llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | 7 of 7 [59.0-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 32 [0.0-10.9%] | 1.000 | 0.067 | 5,546 ms | $0.00447 | 0 |
| structured=criteria · notes=llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | 7 of 7 [59.0-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 32 [0.0-10.9%] | 1.000 | 0.119 | 2,638 ms | $0.00468 | 0 |
| structured=criteria · notes=llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | 7 of 7 [59.0-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 32 [0.0-10.9%] | 1.000 | 0.081 | 2,388 ms | $0.00022 | 0 |
| structured=criteria · notes=rules (version 0.1.0, its agent digest not recorded) | 7 of 7 [59.0-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 32 [0.0-10.9%] | 1.000 | 0.074 | 3.20 ms | $0.00000 | 0 |
| structured=criteria · notes=rules_and_jev (jev-1.13.0) (version 0.1.0, its agent digest not recorded) | 7 of 7 [59.0-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 15 of 32 [29.1-65.3%] | 1.000 | 0.061 | 265 ms | $0.00003 | 0 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | 7 of 7 [59.0-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 15 of 32 [29.1-65.3%] | 1.000 | 0.066 | 6,236 ms | $0.00464 | 0 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | 7 of 7 [59.0-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 15 of 32 [29.1-65.3%] | 1.000 | 0.081 | 2,661 ms | $0.00468 | 0 |
| structured=criteria · notes=rules_and_llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | 7 of 7 [59.0-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 15 of 32 [29.1-65.3%] | 1.000 | 0.077 | 2,079 ms | $0.00022 | 0 |

Accuracy and false automation are n/a on a row recorded before leaderboards kept the counts they are of (mendari.leaderboard/1).

Other questions (accuracy / ECE):

| Configuration | `conservative_therapy` | `mechanical_symptoms` | `red_flag` | `sleep_symptoms` |
|---|---|---|---|---|
| structured=criteria · notes=jev (jev-1.13.0) (version 0.1.0, its agent digest not recorded) | 32 of 32 [89.1-100.0%] / 0.018 | 32 of 32 [89.1-100.0%] / 0.003 | 32 of 32 [89.1-100.0%] / 0.024 | 32 of 32 [89.1-100.0%] / 0.007 |
| structured=criteria · notes=llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | 32 of 32 [89.1-100.0%] / 0.024 | 32 of 32 [89.1-100.0%] / 0.016 | 32 of 32 [89.1-100.0%] / 0.020 | 32 of 32 [89.1-100.0%] / 0.021 |
| structured=criteria · notes=llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | 27 of 32 [67.2-94.7%] / 0.133 | 31 of 32 [83.8-99.9%] / 0.113 | 32 of 32 [89.1-100.0%] / 0.078 | 32 of 32 [89.1-100.0%] / 0.107 |
| structured=criteria · notes=llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | 24 of 32 [56.6-88.5%] / 0.192 | 22 of 32 [50.0-83.9%] / 0.282 | 31 of 32 [83.8-99.9%] / 0.007 | 27 of 32 [67.2-94.7%] / 0.080 |
| structured=criteria · notes=rules (version 0.1.0, its agent digest not recorded) | 32 of 32 [89.1-100.0%] / 0.055 | 32 of 32 [89.1-100.0%] / 0.082 | 32 of 32 [89.1-100.0%] / 0.050 | 32 of 32 [89.1-100.0%] / 0.081 |
| structured=criteria · notes=rules_and_jev (jev-1.13.0) (version 0.1.0, its agent digest not recorded) | 32 of 32 [89.1-100.0%] / 0.047 | 32 of 32 [89.1-100.0%] / 0.079 | 32 of 32 [89.1-100.0%] / 0.054 | 32 of 32 [89.1-100.0%] / 0.080 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | 32 of 32 [89.1-100.0%] / 0.058 | 32 of 32 [89.1-100.0%] / 0.080 | 32 of 32 [89.1-100.0%] / 0.046 | 32 of 32 [89.1-100.0%] / 0.086 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | 32 of 32 [89.1-100.0%] / 0.069 | 32 of 32 [89.1-100.0%] / 0.082 | 32 of 32 [89.1-100.0%] / 0.062 | 32 of 32 [89.1-100.0%] / 0.081 |
| structured=criteria · notes=rules_and_llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | 31 of 32 [83.8-99.9%] / 0.080 | 31 of 32 [83.8-99.9%] / 0.097 | 31 of 32 [83.8-99.9%] / 0.070 | 32 of 32 [89.1-100.0%] / 0.083 |

**`documentation_notes_holdout`** (n=24; dataset hash not recorded; k of n [exact 95% interval, Clopper-Pearson])

| Configuration | Critical detected | False automation | Accuracy | AUTO | Macro-F1 | ECE | p50 latency | Cost / decision | Errors |
|---|---|---|---|---|---|---|---|---|---|
| structured=criteria · notes=jev (jev-1.13.0) (version 0.1.0, its agent digest not recorded) | 6 of 6 [54.1-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 24 [0.0-14.2%] | 1.000 | 0.048 | 213 ms | $0.00003 | 0 |
| structured=criteria · notes=llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | 6 of 6 [54.1-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 24 [0.0-14.2%] | 1.000 | 0.060 | 6,211 ms | $0.00468 | 0 |
| structured=criteria · notes=llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | 6 of 6 [54.1-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 24 [0.0-14.2%] | 1.000 | 0.101 | 2,614 ms | $0.00468 | 0 |
| structured=criteria · notes=llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | 6 of 6 [54.1-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 24 [0.0-14.2%] | 1.000 | 0.082 | 2,100 ms | $0.00023 | 0 |
| structured=criteria · notes=rules (version 0.1.0, its agent digest not recorded) | 3 of 6 [11.8-88.2%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 24 [0.0-14.2%] | 0.748 | 0.176 | 3.22 ms | $0.00000 | 0 |
| structured=criteria · notes=rules_and_jev (jev-1.13.0) (version 0.1.0, its agent digest not recorded) | 6 of 6 [54.1-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 8 of 24 [15.6-55.3%] | 1.000 | 0.063 | 283 ms | $0.00003 | 0 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | 6 of 6 [54.1-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 7 of 24 [12.6-51.1%] | 1.000 | 0.070 | 6,752 ms | $0.00463 | 0 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | 6 of 6 [54.1-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 6 of 24 [9.8-46.7%] | 1.000 | 0.091 | 2,665 ms | $0.00468 | 0 |
| structured=criteria · notes=rules_and_llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | 6 of 6 [54.1-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 6 of 24 [9.8-46.7%] | 1.000 | 0.093 | 2,270 ms | $0.00023 | 0 |

Accuracy and false automation are n/a on a row recorded before leaderboards kept the counts they are of (mendari.leaderboard/1).

Other questions (accuracy / ECE):

| Configuration | `conservative_therapy` | `mechanical_symptoms` | `red_flag` | `sleep_symptoms` |
|---|---|---|---|---|
| structured=criteria · notes=jev (jev-1.13.0) (version 0.1.0, its agent digest not recorded) | 24 of 24 [85.8-100.0%] / 0.022 | 24 of 24 [85.8-100.0%] / 0.004 | 24 of 24 [85.8-100.0%] / 0.022 | 24 of 24 [85.8-100.0%] / 0.002 |
| structured=criteria · notes=llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | 24 of 24 [85.8-100.0%] / 0.023 | 24 of 24 [85.8-100.0%] / 0.030 | 24 of 24 [85.8-100.0%] / 0.025 | 23 of 24 [78.9-99.9%] / 0.048 |
| structured=criteria · notes=llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | 23 of 24 [78.9-99.9%] / 0.062 | 23 of 24 [78.9-99.9%] / 0.064 | 24 of 24 [85.8-100.0%] / 0.071 | 24 of 24 [85.8-100.0%] / 0.104 |
| structured=criteria · notes=llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | 17 of 24 [48.9-87.4%] / 0.217 | 20 of 24 [62.6-95.3%] / 0.097 | 24 of 24 [85.8-100.0%] / 0.049 | 21 of 24 [67.6-97.3%] / 0.085 |
| structured=criteria · notes=rules (version 0.1.0, its agent digest not recorded) | 22 of 24 [73.0-99.0%] / 0.018 | 23 of 24 [78.9-99.9%] / 0.043 | 21 of 24 [67.6-97.3%] / 0.071 | 22 of 24 [73.0-99.0%] / 0.004 |
| structured=criteria · notes=rules_and_jev (jev-1.13.0) (version 0.1.0, its agent digest not recorded) | 24 of 24 [85.8-100.0%] / 0.052 | 24 of 24 [85.8-100.0%] / 0.079 | 24 of 24 [85.8-100.0%] / 0.052 | 23 of 24 [78.9-99.9%] / 0.041 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | 24 of 24 [85.8-100.0%] / 0.058 | 24 of 24 [85.8-100.0%] / 0.082 | 24 of 24 [85.8-100.0%] / 0.048 | 23 of 24 [78.9-99.9%] / 0.041 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | 24 of 24 [85.8-100.0%] / 0.073 | 24 of 24 [85.8-100.0%] / 0.087 | 24 of 24 [85.8-100.0%] / 0.063 | 23 of 24 [78.9-99.9%] / 0.043 |
| structured=criteria · notes=rules_and_llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | 21 of 24 [67.6-97.3%] / 0.140 | 24 of 24 [85.8-100.0%] / 0.087 | 23 of 24 [78.9-99.9%] / 0.082 | 24 of 24 [85.8-100.0%] / 0.110 |

**`prior-auth-local-site-example`** (n=100; dataset hash not recorded; k of n [exact 95% interval, Clopper-Pearson])

| Configuration | Critical detected | False automation | Accuracy | AUTO | Macro-F1 | ECE | p50 latency | Cost / decision | Errors |
|---|---|---|---|---|---|---|---|---|---|
| structured=criteria · notes=llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | 5 of 5 [47.8-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 0.984 | 0.022 | 6,948 ms | $0.00478 | 0 |
| structured=criteria · notes=llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | 5 of 5 [47.8-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 0.984 | 0.048 | 2,626 ms | $0.00470 | 0 |
| structured=criteria · notes=llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | 5 of 5 [47.8-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 0.984 | 0.028 | 2,184 ms | $0.00022 | 0 |
| structured=criteria · notes=rules (version 0.1.0, its agent digest not recorded) | 3 of 5 [14.7-94.7%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 0.968 | 0.005 | 3.24 ms | $0.00000 | 0 |
| structured=criteria · notes=rules_and_jev (jev-1.13.0) (version 0.1.0, its agent digest not recorded) | 5 of 5 [47.8-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 45 of 100 [35.0-55.3%] | 0.984 | 0.019 | 226 ms | $0.00003 | 0 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | 5 of 5 [47.8-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 44 of 100 [34.1-54.3%] | 0.984 | 0.024 | 7,986 ms | $0.00503 | 0 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | 5 of 5 [47.8-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 43 of 100 [33.1-53.3%] | 0.984 | 0.028 | 2,666 ms | $0.00472 | 0 |
| structured=criteria · notes=rules_and_llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | 5 of 5 [47.8-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 44 of 100 [34.1-54.3%] | 0.984 | 0.025 | 2,070 ms | $0.00022 | 0 |

Accuracy and false automation are n/a on a row recorded before leaderboards kept the counts they are of (mendari.leaderboard/1).

Other questions (accuracy / ECE):

| Configuration | `conservative_therapy` | `mechanical_symptoms` | `red_flag` | `sleep_symptoms` |
|---|---|---|---|---|
| structured=criteria · notes=llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | 98 of 100 [93.0-99.8%] / 0.025 | 99 of 100 [94.6-99.97%] / 0.025 | 100 of 100 [96.4-100.0%] / 0.025 | 100 of 100 [96.4-100.0%] / 0.021 |
| structured=criteria · notes=llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | 77 of 100 [67.5-84.8%] / 0.183 | 96 of 100 [90.1-98.9%] / 0.072 | 99 of 100 [94.6-99.97%] / 0.075 | 100 of 100 [96.4-100.0%] / 0.080 |
| structured=criteria · notes=llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | 73 of 100 [63.2-81.4%] / 0.259 | 81 of 100 [71.9-88.2%] / 0.149 | 91 of 100 [83.6-95.8%] / 0.048 | 83 of 100 [74.2-89.8%] / 0.117 |
| structured=criteria · notes=rules (version 0.1.0, its agent digest not recorded) | 93 of 100 [86.1-97.1%] / 0.015 | 100 of 100 [96.4-100.0%] / 0.076 | 98 of 100 [93.0-99.8%] / 0.036 | 99 of 100 [94.6-99.97%] / 0.065 |
| structured=criteria · notes=rules_and_jev (jev-1.13.0) (version 0.1.0, its agent digest not recorded) | 100 of 100 [96.4-100.0%] / 0.040 | 100 of 100 [96.4-100.0%] / 0.072 | 100 of 100 [96.4-100.0%] / 0.063 | 100 of 100 [96.4-100.0%] / 0.071 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | 97 of 100 [91.5-99.4%] / 0.052 | 100 of 100 [96.4-100.0%] / 0.076 | 100 of 100 [96.4-100.0%] / 0.065 | 100 of 100 [96.4-100.0%] / 0.078 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | 100 of 100 [96.4-100.0%] / 0.063 | 100 of 100 [96.4-100.0%] / 0.076 | 100 of 100 [96.4-100.0%] / 0.071 | 100 of 100 [96.4-100.0%] / 0.079 |
| structured=criteria · notes=rules_and_llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | 94 of 100 [87.4-97.8%] / 0.024 | 98 of 100 [93.0-99.8%] / 0.059 | 97 of 100 [91.5-99.4%] / 0.028 | 96 of 100 [90.1-98.9%] / 0.069 |

**`test`** (n=100; dataset hash not recorded; k of n [exact 95% interval, Clopper-Pearson])

| Configuration | Critical detected | False automation | Accuracy | AUTO | Macro-F1 | ECE | p50 latency | Cost / decision | Errors |
|---|---|---|---|---|---|---|---|---|---|
| structured=criteria · notes=llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 1.000 | 0.046 | 6,731 ms | $0.00481 | 0 |
| structured=criteria · notes=llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 1.000 | 0.064 | 2,620 ms | $0.00469 | 0 |
| structured=criteria · notes=llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 0.992 | 0.045 | 2,034 ms | $0.00022 | 0 |
| structured=criteria · notes=rules (version 0.1.0, its agent digest not recorded) | 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 1.000 | 0.045 | 3.24 ms | $0.00000 | 0 |
| structured=criteria · notes=rules_and_jev (jev-1.13.0) (version 0.1.0, its agent digest not recorded) | 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 43 of 100 [33.1-53.3%] | 1.000 | 0.043 | 229 ms | $0.00003 | 0 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 43 of 100 [33.1-53.3%] | 1.000 | 0.045 | 7,413 ms | $0.00481 | 0 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 43 of 100 [33.1-53.3%] | 1.000 | 0.050 | 2,633 ms | $0.00470 | 0 |
| structured=criteria · notes=rules_and_llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 43 of 100 [33.1-53.3%] | 1.000 | 0.050 | 2,028 ms | $0.00022 | 0 |

Accuracy and false automation are n/a on a row recorded before leaderboards kept the counts they are of (mendari.leaderboard/1).

Other questions (accuracy / ECE):

| Configuration | `conservative_therapy` | `mechanical_symptoms` | `red_flag` | `sleep_symptoms` |
|---|---|---|---|---|
| structured=criteria · notes=llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | 94 of 100 [87.4-97.8%] / 0.025 | 100 of 100 [96.4-100.0%] / 0.030 | 100 of 100 [96.4-100.0%] / 0.032 | 100 of 100 [96.4-100.0%] / 0.036 |
| structured=criteria · notes=llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | 72 of 100 [62.1-80.5%] / 0.200 | 98 of 100 [93.0-99.8%] / 0.064 | 99 of 100 [94.6-99.97%] / 0.071 | 100 of 100 [96.4-100.0%] / 0.086 |
| structured=criteria · notes=llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | 71 of 100 [61.1-79.6%] / 0.238 | 77 of 100 [67.5-84.8%] / 0.170 | 89 of 100 [81.2-94.4%] / 0.059 | 79 of 100 [69.7-86.5%] / 0.138 |
| structured=criteria · notes=rules (version 0.1.0, its agent digest not recorded) | 100 of 100 [96.4-100.0%] / 0.054 | 100 of 100 [96.4-100.0%] / 0.082 | 100 of 100 [96.4-100.0%] / 0.056 | 100 of 100 [96.4-100.0%] / 0.071 |
| structured=criteria · notes=rules_and_jev (jev-1.13.0) (version 0.1.0, its agent digest not recorded) | 100 of 100 [96.4-100.0%] / 0.047 | 100 of 100 [96.4-100.0%] / 0.080 | 100 of 100 [96.4-100.0%] / 0.058 | 100 of 100 [96.4-100.0%] / 0.070 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-haiku-4.5) (version 0.1.0, its agent digest not recorded) | 97 of 100 [91.5-99.4%] / 0.074 | 100 of 100 [96.4-100.0%] / 0.086 | 99 of 100 [94.6-99.97%] / 0.068 | 100 of 100 [96.4-100.0%] / 0.070 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-sonnet-5) (version 0.1.0, its agent digest not recorded) | 100 of 100 [96.4-100.0%] / 0.060 | 100 of 100 [96.4-100.0%] / 0.082 | 100 of 100 [96.4-100.0%] / 0.070 | 100 of 100 [96.4-100.0%] / 0.077 |
| structured=criteria · notes=rules_and_llm (openai/gpt-oss-120b) (version 0.1.0, its agent digest not recorded) | 88 of 100 [80.0-93.6%] / 0.076 | 93 of 100 [86.1-97.1%] / 0.029 | 93 of 100 [86.1-97.1%] / 0.007 | 91 of 100 [83.6-95.8%] / 0.013 |


## Known failure modes

- Only the codes listed in logic/structured.yaml count. A lumbar X-ray billed under another code (for example FX112, four or more views) reads as missing imaging. On the bundled local-site example (datasets/prior-auth-local-site-example.jsonl), which has such X-rays and shorthand notes, prior imaging and the next step are answered wrong more often than on the benchmark, the next step most often on shorthand notes (`mendari eval mendari/prior-auth-preflight --dataset datasets/prior-auth-local-site-example.jsonl` compares the next step with the benchmark, by label and by subgroup, and its report, with `--json`, holds prior imaging). Add a site's codes before use.
- Through FHIR, the payer is read from the Organization the Coverage's first payor names, by its identifier in the synthetic payers' system, else from the payor's own identifier as written. A site's own payer identifier (an EHR's payer id on the Organization) is neither, and no site code map translates an identifier yet, so the payer reads as `other` and the result is `not_checked` (how a site's payer id is to be read is the owner's open question). On a real EHR's data, until its payer ids are read, that is every order. A `not_checked` order shows no card at `order-sign` unless its notes may document a red flag, when the clinician signing it sees a card that says so and says nothing of what the payer asks for, and the order is in the review queue at urgent priority. On a payer the pack knows, the order's own card (ready to submit, documentation missing, not required) does not name a possible red flag in its summary; its reasons do, in the detail or behind the audit link. The order, diagnoses and prior procedures are read by their code system wherever the coding sits (the fictional procedure codes, ICD-10-CM). An order is the pack's only when one of its codings is a fictional procedure code it has a requirement set for. A site's own order code listed in logic/structured.yaml counts on JSON input, but through FHIR the order gets no decision until the EHR sends the fictional code beside it. No medication is read, so spec 0.3's rule for when a MedicationRequest's drug is taken (§2.7) plays no part in this pack.
- Through FHIR, a note is read only as plain text or HTML, inline or in the Binary its url names (read from the EHR on a CDS hook or the SMART launch). An RTF, PDF or CDA note, or one that cannot be read, makes the pack abstain, and EHRs often keep notes as RTF. So do notes longer than 20,000 characters in all, which the input does not allow (`too_large`; the text is never cut), and more links than one decision reads (20, `cap`). A hook reads every clinical note of the patient, each linked note's Binary counting as one link, so a long record may reach either.
- Durations are read as written in the notes ("8 weeks of physical therapy"). A note like "PT since July" cannot be compared with dates by the keyword rules and reads as unclear.
- The keyword rules read English notes only and miss phrasings they do not list, including red flags. On hand-written notes they found 7/7 red flags in the set they were written on but 3/6 held out ("breast cancer treated 3 years ago", "numbness around the anus" and "lost 10 pounds without trying" were missed), and 9/11 at the local site. The policy therefore allows AUTO only while the notes are also read by a model (`automation_requires`), and a possible red flag always goes to a person.
- The overall probability treats every answer as independent.
- The public benchmark is generated from templates written with the pack, so it overstates real-world accuracy. The hand-written note sets are the better guide, and the held-out one was written before the rules.

## Evidence and provenance

- **Synthetic public benchmark**: evals/benchmark.json, the `test` split generated by mendari.sandbox.synthetic_prior_auth; ground truth comes from the generated facts.
- **Hand-written note sets**: data/documentation_notes.jsonl (used to write the note rules) and data/documentation_notes_holdout.jsonl (written before the rules and never used to tune them): conservative treatment with enough, too little or unclear duration; red flags and look-alikes; knee mechanical symptoms; sleep apnoea symptoms and insomnia.

## Version history

Renamed on 2026-09-30, when the project became Mendari: this pack was `openhealth/prior-auth-preflight`.
Its id, its publisher's name and its `https://mendari.co/...` identifiers changed; its criteria,
rules, models, data and results did not.

#### 0.3.1

- **A possible red flag shows a card at `order-sign` on an order the pack has no requirement set
  for** (`not_checked`; the owner's choice of 2026-11-07, "Red flag shows a card"): the
  `possible-red-flag` rule's own card (`cds_hooks.card.also_show`, spec 0.3 §5), `critical`,
  "Possible red flag in the notes: consider urgent imaging and an expedited review; prior auth not
  checked". Before, such an order showed no card, so a possible red flag in its notes reached only
  the review queue and not the clinician signing it; on a real EHR's data, under a site's own payer
  id, that is every order. Every other `not_checked` order still shows no card. The decision does
  not change: HUMAN_REVIEW, `not_checked`, urgent priority, and no Da Vinci CRD coverage
  information (a `not_checked` order asserts none). A red flag on an order whose payer the pack
  knows shows on that order's own card as before, in its reasons and not its summary, which the
  known failure modes now say. Of the 300 orders the FHIR decisions golden holds, 3 more show a
  card: 208 of the 259 the pack decides, the other 51 `not_checked` orders quiet.
- A PATCH bump, as the changelogs read CONTRIBUTING.md's rule (0.2.1 of the Referral Router
  among them): what the cards show and say, with every decision identical. Criteria, rules,
  providers, policy and data are unchanged; the benchmark's figures, its ledger and the FHIR
  decisions golden are as they were. The digest changes because the manifest does.

### 0.3.0

- **Through FHIR, the pack reads EHR data on spec 0.3** (LP-23k, after the owner's go-ahead on
  2026-11-06; `spec_version: "0.3"`, `fhir/mapping.yaml` only). A MINOR bump, by CONTRIBUTING.md's
  rule (MINOR for a change in behaviour): what the pack decides through FHIR changes where the 0.2
  mapping misread the data, and nothing else does.
  - The order (and `focus_where`, which orders are the pack's), the diagnoses and the prior
    procedures are read by their code system wherever the coding sits: the fictional procedure
    codes and ICD-10-CM. With none of the pack's systems, a diagnosis's and a procedure's first
    coding is read as before (a site's own code, which `logic/structured.yaml` matches once the
    site lists it). An order is the pack's when one of its codings is in the fictional procedure
    code system and is a code the pack has a requirement set for, wherever it sits (the 0.2
    mapping read the first coding only, whatever its system), and only that system is read for
    the order: a site's own order code that `logic/structured.yaml` lists counts on JSON input,
    never through FHIR. An order with no such coding is not the pack's, as before, and that now
    includes one whose fictional code has no system (`{code: FX101}`), which the 0.2 mapping read
    as the pack's: it gets no decision and no card (`missing required FHIR resources:
    ServiceRequest` on `fhir/decide`).
  - The payer is read through the Coverage's first payor: the Organization it names
    (contained, in the record, or read from the EHR on a CDS hook or the SMART launch), by its
    identifier in a new, fictional payer identifier system the pack declares
    (`https://mendari.co/fhir/sid/fictional-payers`); else the payor's own identifier as the
    Coverage writes it, which is how the synthetic records name their payers and all the 0.2
    mapping read. A payer named by reference read as `other` before. A site's own payer
    identifier is neither, and no site code map translates an identifier (codings only), so it
    still reads as `other` and the order as `not_checked`: how a payer field is to read a
    site's payer id is the owner's open question. Meanwhile a `not_checked` order shows no card
    at `order-sign`, so a possible red flag in its notes reaches the review queue at urgent
    priority but not the clinician signing the order; the card says so. A payor reference that
    could not be read (`fhir/decide` and `$decide` never read one) makes the decision abstain
    (`could not read coverage.payer: <reason>`), whether or not the payor also carries an
    identifier: the 0.2 mapping read it as `other`, or as that identifier when it had one.
  - Notes are read through `attachment_text`: text/plain and text/html, inline or in the
    Binary the attachment's url names. An RTF, PDF or CDA note, or one that cannot be read,
    makes the decision abstain; an inline note with no content type is now unreadable, and an
    inline HTML note is read as its text. Notes longer than 20,000 characters in all, which the
    input schema does not allow, abstain too (`could not read notes: too_large`), where a long
    inline note was refused before as invalid input, with no card; so do more links than a
    decision reads (20, `cap`).
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

### 0.2.1

- The local-site known failure mode states its finding and the command that measures it,
  instead of percentages typed in by hand (LP-10: a share of cases on a card is k of n with
  its interval, computed). Criteria, rules, providers, policy, models and data are unchanged;
  every decision is identical. The digest changes because the manifest does.

### 0.2.0

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

### 0.1.3

- Every surface now says what the pack is: "Synthetic example. Not clinically reviewed. Not for
  patient care." It is derived from the new `card.provenance: synthetic`, never written by the
  pack (the Decision Card's first line, the dashboard, Try it, the review queue, CDS Hooks cards
  and discovery, the registry and the Hub).
- Each question has a `display` name, so people read words instead of keys, and two labels have names (`lumbar_mri`, `submit_ready`).
- Criteria, rules, providers, policy, models and data are unchanged; every decision is identical.
  The digest changes because the manifest does.

### 0.1.2

- Da Vinci DTR (`cds_hooks.questionnaire`): `Questionnaire/$questionnaire-package` serves a
  questionnaire of the documentation the payer asks for, for the order's service and payer (the
  members of `next_step`'s table that can make it `needs_attention`), with a response pre-answered
  from the pack's decision. The decision is found by the CRD assertion id the order carries. The
  CRD coverage information for `needs_attention` links the questionnaire. Questions, criteria,
  rules, policy and data are unchanged.

### 0.1.1

- Da Vinci CRD: the `order-sign` service also returns, for each decided order, a system action
  updating the order with CRD's `coverage-information` (`cds_hooks.coverage_information`):
  `submit_ready` and `needs_attention` are covered with prior authorization needed (and clinical
  documentation needed for `needs_attention`), `not_required` needs no authorization, and
  `not_checked` asserts nothing. The assertion id is the decision id. Cards are unchanged.
  Questions, criteria, rules, policy and data are unchanged.

### 0.1.0

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

---

*Generated by `mendari card`. A Decision Card describes validation on the data named above. It is not evidence of performance at your site; run local validation first.*
