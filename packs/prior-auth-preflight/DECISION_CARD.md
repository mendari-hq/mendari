# Decision Card: Prior Authorization Preflight

**Synthetic example. Not clinically reviewed. Not for patient care.**

*Research preview. Not a medical device. Not for emergencies: patients should call 911.*

`mendari/prior-auth-preflight` · version **0.2.0** · Apache-2.0 · published by Mendari reference packs (community, self-declared)

> Checks an order against the patient's payer before it is sent for prior authorization: does this payer require authorization for this service, and does the record have what the payer asks for? Complete requests are queued for submission; the gaps in the others are listed.

| | |
|---|---|
| Source | Maintained by Mendari |
| Risk level | **1**, clinical information processing (clinical information) |
| Automation | AUTO permitted by policy thresholds; every decision is audited. Only while `notes` is `rules_and_jev` or `rules_and_llm`; otherwise a person reviews. |
| Pack digest | `sha256:36ba77e89092654b012bd69b62b49fe645017d1b0b8544dcace1b02751a0f1af` |
| Last validation | 2026-10-07 on `test` (n=600, sha256 `e74e45ea35d5…`) |

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

Benchmark `test` (n=600) with structured=`criteria`, notes=`rules`:

| Metric | Value |
|---|---|
| `next_step` accuracy | 100.0% |
| Macro-F1 | 1.000 |
| Expected calibration error | 0.045 |
| Brier score | 0.004 |
| AUTO rate | 0.0% |
| HUMAN_REVIEW rate | 100.0% |
| ABSTAIN rate | 0.0% |
| BLOCK rate | 0.0% |
| Accuracy of AUTO decisions | n/a |
| False automation rate | n/a |
| Expected human override rate | 0.0% |
| Latency p50 / p95 | 3.44 / 3.62 ms |
| Cost per decision | $0.00000 |
| Critical `red_flag=yes` | 25/25 detected, 0 automated |

Per-label performance:

| Label | Precision | Recall | F1 | n |
|---|---|---|---|---|
| `submit_ready` | 100.0% | 100.0% | 1.000 | 180 |
| `needs_attention` | 100.0% | 100.0% | 1.000 | 221 |
| `not_required` | 100.0% | 100.0% | 1.000 | 57 |
| `not_checked` | 100.0% | 100.0% | 1.000 | 142 |

Subgroups:

| Subgroup | n | Accuracy | AUTO rate | Abstention |
|---|---|---|---|---|
| site=public | 600 | 100.0% | 0.0% | 0.0% |
| service=knee_arthroscopy | 160 | 100.0% | 0.0% | 0.0% |
| service=lumbar_mri | 214 | 100.0% | 0.0% | 0.0% |
| service=other | 89 | 100.0% | 0.0% | 0.0% |
| service=sleep_study | 137 | 100.0% | 0.0% | 0.0% |
| payer=acme | 283 | 100.0% | 0.0% | 0.0% |
| payer=beacon | 255 | 100.0% | 0.0% | 0.0% |
| payer=other | 62 | 100.0% | 0.0% | 0.0% |
| style=prose | 600 | 100.0% | 0.0% | 0.0% |
| age_band=18-39 | 188 | 100.0% | 0.0% | 0.0% |
| age_band=40-64 | 253 | 100.0% | 0.0% | 0.0% |
| age_band=65+ | 159 | 100.0% | 0.0% | 0.0% |
| sex=female | 277 | 100.0% | 0.0% | 0.0% |
| sex=male | 323 | 100.0% | 0.0% | 0.0% |

Calibration (reliability):

| Confidence bin | n | Mean confidence | Accuracy |
|---|---|---|---|
| 0.9 to 1.0 | 600 | 95.5% | 100.0% |

Threshold sweep (if everything ≥ threshold were automated):

| Threshold | Coverage | Accuracy | Errors |
|---|---|---|---|
| 0.50 | 100.0% | 100.0% | 0 |
| 0.60 | 100.0% | 100.0% | 0 |
| 0.70 | 100.0% | 100.0% | 0 |
| 0.80 | 100.0% | 100.0% | 0 |
| 0.85 | 100.0% | 100.0% | 0 |
| 0.90 | 100.0% | 100.0% | 0 |
| 0.95 | 60.2% | 100.0% | 0 |
| 0.97 | 41.2% | 100.0% | 0 |
| 0.99 | 1.5% | 100.0% | 0 |

## Provider comparison

Every configuration below ran on the same records. Remote models are named because they are a deployment choice. Synthetic and hand-written data: compare configurations with each other, not with performance at a real site.

**`documentation_notes`** (n=32)

| Configuration | Accuracy | Macro-F1 | ECE | AUTO | False automation | Critical detected | p50 latency | Cost / decision | Errors |
|---|---|---|---|---|---|---|---|---|---|
| structured=criteria · notes=jev (jev-1.13.0) (measured on 0.1.0) | 100.0% | 1.000 | 0.058 | 0.0% | n/a | 7/7 (0 automated) | 250 ms | $0.00003 | 0 |
| structured=criteria · notes=llm (anthropic/claude-haiku-4.5) (measured on 0.1.0) | 100.0% | 1.000 | 0.067 | 0.0% | n/a | 7/7 (0 automated) | 5,546 ms | $0.00447 | 0 |
| structured=criteria · notes=llm (anthropic/claude-sonnet-5) (measured on 0.1.0) | 100.0% | 1.000 | 0.119 | 0.0% | n/a | 7/7 (0 automated) | 2,638 ms | $0.00468 | 0 |
| structured=criteria · notes=llm (openai/gpt-oss-120b) (measured on 0.1.0) | 100.0% | 1.000 | 0.081 | 0.0% | n/a | 7/7 (0 automated) | 2,388 ms | $0.00022 | 0 |
| structured=criteria · notes=rules (measured on 0.1.0) | 100.0% | 1.000 | 0.074 | 0.0% | n/a | 7/7 (0 automated) | 3.20 ms | $0.00000 | 0 |
| structured=criteria · notes=rules_and_jev (jev-1.13.0) (measured on 0.1.0) | 100.0% | 1.000 | 0.061 | 46.9% | 0.0% | 7/7 (0 automated) | 265 ms | $0.00003 | 0 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-haiku-4.5) (measured on 0.1.0) | 100.0% | 1.000 | 0.066 | 46.9% | 0.0% | 7/7 (0 automated) | 6,236 ms | $0.00464 | 0 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-sonnet-5) (measured on 0.1.0) | 100.0% | 1.000 | 0.081 | 46.9% | 0.0% | 7/7 (0 automated) | 2,661 ms | $0.00468 | 0 |
| structured=criteria · notes=rules_and_llm (openai/gpt-oss-120b) (measured on 0.1.0) | 100.0% | 1.000 | 0.077 | 46.9% | 0.0% | 7/7 (0 automated) | 2,079 ms | $0.00022 | 0 |

Other questions (accuracy / ECE):

| Configuration | `conservative_therapy` | `mechanical_symptoms` | `red_flag` | `sleep_symptoms` |
|---|---|---|---|---|
| structured=criteria · notes=jev (jev-1.13.0) (measured on 0.1.0) | 100.0% / 0.018 | 100.0% / 0.003 | 100.0% / 0.024 | 100.0% / 0.007 |
| structured=criteria · notes=llm (anthropic/claude-haiku-4.5) (measured on 0.1.0) | 100.0% / 0.024 | 100.0% / 0.016 | 100.0% / 0.020 | 100.0% / 0.021 |
| structured=criteria · notes=llm (anthropic/claude-sonnet-5) (measured on 0.1.0) | 84.4% / 0.133 | 96.9% / 0.113 | 100.0% / 0.078 | 100.0% / 0.107 |
| structured=criteria · notes=llm (openai/gpt-oss-120b) (measured on 0.1.0) | 75.0% / 0.192 | 68.8% / 0.282 | 96.9% / 0.007 | 84.4% / 0.080 |
| structured=criteria · notes=rules (measured on 0.1.0) | 100.0% / 0.055 | 100.0% / 0.082 | 100.0% / 0.050 | 100.0% / 0.081 |
| structured=criteria · notes=rules_and_jev (jev-1.13.0) (measured on 0.1.0) | 100.0% / 0.047 | 100.0% / 0.079 | 100.0% / 0.054 | 100.0% / 0.080 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-haiku-4.5) (measured on 0.1.0) | 100.0% / 0.058 | 100.0% / 0.080 | 100.0% / 0.046 | 100.0% / 0.086 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-sonnet-5) (measured on 0.1.0) | 100.0% / 0.069 | 100.0% / 0.082 | 100.0% / 0.062 | 100.0% / 0.081 |
| structured=criteria · notes=rules_and_llm (openai/gpt-oss-120b) (measured on 0.1.0) | 96.9% / 0.080 | 96.9% / 0.097 | 96.9% / 0.070 | 100.0% / 0.083 |

**`documentation_notes_holdout`** (n=24)

| Configuration | Accuracy | Macro-F1 | ECE | AUTO | False automation | Critical detected | p50 latency | Cost / decision | Errors |
|---|---|---|---|---|---|---|---|---|---|
| structured=criteria · notes=jev (jev-1.13.0) (measured on 0.1.0) | 100.0% | 1.000 | 0.048 | 0.0% | n/a | 6/6 (0 automated) | 213 ms | $0.00003 | 0 |
| structured=criteria · notes=llm (anthropic/claude-haiku-4.5) (measured on 0.1.0) | 100.0% | 1.000 | 0.060 | 0.0% | n/a | 6/6 (0 automated) | 6,211 ms | $0.00468 | 0 |
| structured=criteria · notes=llm (anthropic/claude-sonnet-5) (measured on 0.1.0) | 100.0% | 1.000 | 0.101 | 0.0% | n/a | 6/6 (0 automated) | 2,614 ms | $0.00468 | 0 |
| structured=criteria · notes=llm (openai/gpt-oss-120b) (measured on 0.1.0) | 100.0% | 1.000 | 0.082 | 0.0% | n/a | 6/6 (0 automated) | 2,100 ms | $0.00023 | 0 |
| structured=criteria · notes=rules_and_jev (jev-1.13.0) (measured on 0.1.0) | 100.0% | 1.000 | 0.063 | 33.3% | 0.0% | 6/6 (0 automated) | 283 ms | $0.00003 | 0 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-haiku-4.5) (measured on 0.1.0) | 100.0% | 1.000 | 0.070 | 29.2% | 0.0% | 6/6 (0 automated) | 6,752 ms | $0.00463 | 0 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-sonnet-5) (measured on 0.1.0) | 100.0% | 1.000 | 0.091 | 25.0% | 0.0% | 6/6 (0 automated) | 2,665 ms | $0.00468 | 0 |
| structured=criteria · notes=rules_and_llm (openai/gpt-oss-120b) (measured on 0.1.0) | 100.0% | 1.000 | 0.093 | 25.0% | 0.0% | 6/6 (0 automated) | 2,270 ms | $0.00023 | 0 |
| structured=criteria · notes=rules (measured on 0.1.0) | 75.0% | 0.748 | 0.176 | 0.0% | n/a | 3/6 (0 automated) | 3.22 ms | $0.00000 | 0 |

Other questions (accuracy / ECE):

| Configuration | `conservative_therapy` | `mechanical_symptoms` | `red_flag` | `sleep_symptoms` |
|---|---|---|---|---|
| structured=criteria · notes=jev (jev-1.13.0) (measured on 0.1.0) | 100.0% / 0.022 | 100.0% / 0.004 | 100.0% / 0.022 | 100.0% / 0.002 |
| structured=criteria · notes=llm (anthropic/claude-haiku-4.5) (measured on 0.1.0) | 100.0% / 0.023 | 100.0% / 0.030 | 100.0% / 0.025 | 95.8% / 0.048 |
| structured=criteria · notes=llm (anthropic/claude-sonnet-5) (measured on 0.1.0) | 95.8% / 0.062 | 95.8% / 0.064 | 100.0% / 0.071 | 100.0% / 0.104 |
| structured=criteria · notes=llm (openai/gpt-oss-120b) (measured on 0.1.0) | 70.8% / 0.217 | 83.3% / 0.097 | 100.0% / 0.049 | 87.5% / 0.085 |
| structured=criteria · notes=rules_and_jev (jev-1.13.0) (measured on 0.1.0) | 100.0% / 0.052 | 100.0% / 0.079 | 100.0% / 0.052 | 95.8% / 0.041 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-haiku-4.5) (measured on 0.1.0) | 100.0% / 0.058 | 100.0% / 0.082 | 100.0% / 0.048 | 95.8% / 0.041 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-sonnet-5) (measured on 0.1.0) | 100.0% / 0.073 | 100.0% / 0.087 | 100.0% / 0.063 | 95.8% / 0.043 |
| structured=criteria · notes=rules_and_llm (openai/gpt-oss-120b) (measured on 0.1.0) | 87.5% / 0.140 | 100.0% / 0.087 | 95.8% / 0.082 | 100.0% / 0.110 |
| structured=criteria · notes=rules (measured on 0.1.0) | 91.7% / 0.018 | 95.8% / 0.043 | 87.5% / 0.071 | 91.7% / 0.004 |

**`prior-auth-local-site-example`** (n=100)

| Configuration | Accuracy | Macro-F1 | ECE | AUTO | False automation | Critical detected | p50 latency | Cost / decision | Errors |
|---|---|---|---|---|---|---|---|---|---|
| structured=criteria · notes=llm (anthropic/claude-haiku-4.5) (measured on 0.1.0) | 98.0% | 0.984 | 0.022 | 0.0% | n/a | 5/5 (0 automated) | 6,948 ms | $0.00478 | 0 |
| structured=criteria · notes=llm (anthropic/claude-sonnet-5) (measured on 0.1.0) | 98.0% | 0.984 | 0.048 | 0.0% | n/a | 5/5 (0 automated) | 2,626 ms | $0.00470 | 0 |
| structured=criteria · notes=llm (openai/gpt-oss-120b) (measured on 0.1.0) | 98.0% | 0.984 | 0.028 | 0.0% | n/a | 5/5 (0 automated) | 2,184 ms | $0.00022 | 0 |
| structured=criteria · notes=rules_and_jev (jev-1.13.0) (measured on 0.1.0) | 98.0% | 0.984 | 0.019 | 45.0% | 0.0% | 5/5 (0 automated) | 226 ms | $0.00003 | 0 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-haiku-4.5) (measured on 0.1.0) | 98.0% | 0.984 | 0.024 | 44.0% | 0.0% | 5/5 (0 automated) | 7,986 ms | $0.00503 | 0 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-sonnet-5) (measured on 0.1.0) | 98.0% | 0.984 | 0.028 | 43.0% | 0.0% | 5/5 (0 automated) | 2,666 ms | $0.00472 | 0 |
| structured=criteria · notes=rules_and_llm (openai/gpt-oss-120b) (measured on 0.1.0) | 98.0% | 0.984 | 0.025 | 44.0% | 0.0% | 5/5 (0 automated) | 2,070 ms | $0.00022 | 0 |
| structured=criteria · notes=rules (measured on 0.1.0) | 96.0% | 0.968 | 0.005 | 0.0% | n/a | 3/5 (0 automated) | 3.24 ms | $0.00000 | 0 |

Other questions (accuracy / ECE):

| Configuration | `conservative_therapy` | `mechanical_symptoms` | `red_flag` | `sleep_symptoms` |
|---|---|---|---|---|
| structured=criteria · notes=llm (anthropic/claude-haiku-4.5) (measured on 0.1.0) | 98.0% / 0.025 | 99.0% / 0.025 | 100.0% / 0.025 | 100.0% / 0.021 |
| structured=criteria · notes=llm (anthropic/claude-sonnet-5) (measured on 0.1.0) | 77.0% / 0.183 | 96.0% / 0.072 | 99.0% / 0.075 | 100.0% / 0.080 |
| structured=criteria · notes=llm (openai/gpt-oss-120b) (measured on 0.1.0) | 73.0% / 0.259 | 81.0% / 0.149 | 91.0% / 0.048 | 83.0% / 0.117 |
| structured=criteria · notes=rules_and_jev (jev-1.13.0) (measured on 0.1.0) | 100.0% / 0.040 | 100.0% / 0.072 | 100.0% / 0.063 | 100.0% / 0.071 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-haiku-4.5) (measured on 0.1.0) | 97.0% / 0.052 | 100.0% / 0.076 | 100.0% / 0.065 | 100.0% / 0.078 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-sonnet-5) (measured on 0.1.0) | 100.0% / 0.063 | 100.0% / 0.076 | 100.0% / 0.071 | 100.0% / 0.079 |
| structured=criteria · notes=rules_and_llm (openai/gpt-oss-120b) (measured on 0.1.0) | 94.0% / 0.024 | 98.0% / 0.059 | 97.0% / 0.028 | 96.0% / 0.069 |
| structured=criteria · notes=rules (measured on 0.1.0) | 93.0% / 0.015 | 100.0% / 0.076 | 98.0% / 0.036 | 99.0% / 0.065 |

**`test`** (n=100)

| Configuration | Accuracy | Macro-F1 | ECE | AUTO | False automation | Critical detected | p50 latency | Cost / decision | Errors |
|---|---|---|---|---|---|---|---|---|---|
| structured=criteria · notes=llm (anthropic/claude-haiku-4.5) (measured on 0.1.0) | 100.0% | 1.000 | 0.046 | 0.0% | n/a | 2/2 (0 automated) | 6,731 ms | $0.00481 | 0 |
| structured=criteria · notes=llm (anthropic/claude-sonnet-5) (measured on 0.1.0) | 100.0% | 1.000 | 0.064 | 0.0% | n/a | 2/2 (0 automated) | 2,620 ms | $0.00469 | 0 |
| structured=criteria · notes=rules (measured on 0.1.0) | 100.0% | 1.000 | 0.045 | 0.0% | n/a | 2/2 (0 automated) | 3.24 ms | $0.00000 | 0 |
| structured=criteria · notes=rules_and_jev (jev-1.13.0) (measured on 0.1.0) | 100.0% | 1.000 | 0.043 | 43.0% | 0.0% | 2/2 (0 automated) | 229 ms | $0.00003 | 0 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-haiku-4.5) (measured on 0.1.0) | 100.0% | 1.000 | 0.045 | 43.0% | 0.0% | 2/2 (0 automated) | 7,413 ms | $0.00481 | 0 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-sonnet-5) (measured on 0.1.0) | 100.0% | 1.000 | 0.050 | 43.0% | 0.0% | 2/2 (0 automated) | 2,633 ms | $0.00470 | 0 |
| structured=criteria · notes=rules_and_llm (openai/gpt-oss-120b) (measured on 0.1.0) | 100.0% | 1.000 | 0.050 | 43.0% | 0.0% | 2/2 (0 automated) | 2,028 ms | $0.00022 | 0 |
| structured=criteria · notes=llm (openai/gpt-oss-120b) (measured on 0.1.0) | 99.0% | 0.992 | 0.045 | 0.0% | n/a | 2/2 (0 automated) | 2,034 ms | $0.00022 | 0 |

Other questions (accuracy / ECE):

| Configuration | `conservative_therapy` | `mechanical_symptoms` | `red_flag` | `sleep_symptoms` |
|---|---|---|---|---|
| structured=criteria · notes=llm (anthropic/claude-haiku-4.5) (measured on 0.1.0) | 94.0% / 0.025 | 100.0% / 0.030 | 100.0% / 0.032 | 100.0% / 0.036 |
| structured=criteria · notes=llm (anthropic/claude-sonnet-5) (measured on 0.1.0) | 72.0% / 0.200 | 98.0% / 0.064 | 99.0% / 0.071 | 100.0% / 0.086 |
| structured=criteria · notes=rules (measured on 0.1.0) | 100.0% / 0.054 | 100.0% / 0.082 | 100.0% / 0.056 | 100.0% / 0.071 |
| structured=criteria · notes=rules_and_jev (jev-1.13.0) (measured on 0.1.0) | 100.0% / 0.047 | 100.0% / 0.080 | 100.0% / 0.058 | 100.0% / 0.070 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-haiku-4.5) (measured on 0.1.0) | 97.0% / 0.074 | 100.0% / 0.086 | 99.0% / 0.068 | 100.0% / 0.070 |
| structured=criteria · notes=rules_and_llm (anthropic/claude-sonnet-5) (measured on 0.1.0) | 100.0% / 0.060 | 100.0% / 0.082 | 100.0% / 0.070 | 100.0% / 0.077 |
| structured=criteria · notes=rules_and_llm (openai/gpt-oss-120b) (measured on 0.1.0) | 88.0% / 0.076 | 93.0% / 0.029 | 93.0% / 0.007 | 91.0% / 0.013 |
| structured=criteria · notes=llm (openai/gpt-oss-120b) (measured on 0.1.0) | 71.0% / 0.238 | 77.0% / 0.170 | 89.0% / 0.059 | 79.0% / 0.138 |


## Known failure modes

- Only the codes listed in logic/structured.yaml count. A lumbar X-ray billed under another code (for example FX112, four or more views) reads as missing imaging. On the bundled local-site example (datasets/prior-auth-local-site-example.jsonl), which has such X-rays and shorthand notes, prior imaging is 88 % correct and the next step 93 % (86 % on shorthand notes). Add a site's codes before use.
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

#### 0.2.0

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
