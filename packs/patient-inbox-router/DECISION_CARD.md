# Decision Card: Patient Inbox Router

`openhealth/patient-inbox-router` · version **0.2.0** · Apache-2.0 · published by OpenHealth Hub reference packs (community)

> Routes patient portal and SMS messages to the front desk, a medical assistant, a nurse, a physician or billing, and escalates possible emergencies to stat nurse triage.

| | |
|---|---|
| Risk level | **0**, administrative |
| Automation | AUTO permitted by policy thresholds. |
| Pack digest | `sha256:70c12a78dccfa914a22e3b401dd5c496dc166ae19d86959cfff9e766fc03fdfb` |
| Last validation | 2026-09-28 on `test` (n=600, sha256 `2997856d17a5…`) |

## Intended use

First-pass routing of incoming patient portal or SMS messages in an outpatient primary-care practice, with an explicit escalation path for messages that may describe an emergency.

**Why risk level 0:** The decision is which team reads a message first. The pack never replies to patients or makes a clinical judgement. A misroute delays a message but does not by itself change care, and messages that may describe an emergency are never auto-routed: a deterministic screen always sends them to a nurse for stat human review.

## Intended users

- Front-desk staff and practice managers
- Medical assistants, nurses and physicians working the shared inbox
- Health IT teams integrating the inbox with the EHR

## Patient population

Adult and pediatric primary-care patients, or their caregivers, writing in English or Spanish to a US outpatient practice.

## Out of scope

- Replying to patients, giving medical advice or deciding on care.
- Acting as the safety net for emergencies. Portals must keep telling patients to call 911 for emergencies; the red-flag screen only speeds up triage of messages that arrive anyway.
- Specialty, inpatient or behavioral-health-only inboxes, and languages other than English and Spanish.

## Inputs

| Field | Type | Required |
|---|---|---|
| `message` | object | yes |
| `message.subject` | string | no |
| `message.body` | string | yes |
| `message.channel` | `portal` \| `sms` \| `email` | no |
| `message.sent_at` | string | no |
| `patient` | object | no |
| `patient.age_band` | `0-17` \| `18-39` \| `40-64` \| `65+` | no |
| `patient.preferred_language` | string | no |

FHIR R4 mapping: `fhir/mapping.yaml`.

## Outputs

**`destination`** (primary): Which team should handle this patient message first? Route to the least specialised team that can fully handle it. If a message mixes administrative and clinical requests, choose the most clinically qualified team needed.

| Label | Criteria |
|---|---|
| `front_desk` | Scheduling, rescheduling or cancelling appointments; hours, directions and parking; portal access; updating contact details; general requests that need no clinical data. |
| `medical_assistant` | Routine prescription refills and renewals, requests for or transfers of medical records, referral and lab-order status, and forms that need clinical information filled in. |
| `nurse` | New or ongoing symptoms, medication side effects, wound and post-procedure care, vaccine questions, and anything that needs clinical triage, including possible emergencies. |
| `physician` | Questions that need a physician's judgement: interpreting results, changing a diagnosis or treatment plan, dose changes, and requests for the physician's opinion. |
| `billing` | Bills, statements, copays, charges, refunds, payment plans, insurance claims, coverage and cost estimates. |

**`urgency`**: Does this message describe symptoms that may need emergency or same-day clinical attention? Answer "urgent" if in doubt.

| Label | Criteria |
|---|---|
| `urgent` | Possible emergency: chest pain or pressure, trouble breathing, stroke signs, suicidal thoughts or intentional overdose, severe bleeding, severe allergic reaction, fever in a young infant, or a person who is hard to wake. |
| `routine` | No sign of an emergency or time-critical clinical problem. |

Every decision is one of `AUTO`, `HUMAN_REVIEW`, `ABSTAIN` or `BLOCK`, with a priority (`routine`, `urgent`, `asap`, `stat`) and an audit record.

## Decision policy

- AUTO when `destination` confidence ≥ **0.90**; human review ≥ **0.50**; abstain below.
- `front_desk` needs ≥ **0.95** for AUTO.
- `billing` needs ≥ **0.95** for AUTO.
- Rule **possible-emergency**: `urgency` is `urgent` (p ≥ 0.35) ⇒ `HUMAN_REVIEW` → `nurse`, priority `stat`. Possible emergency symptoms. Nurse triage now; do not leave in a routine queue.
- Fallback (ABSTAIN / BLOCK): Leave in the shared inbox for manual triage by the front desk (the existing workflow).

## Providers

| Slot | Answers | Default | Alternatives |
|---|---|---|---|
| `safety_screen` | urgency | `rules` (rules) | `rules_and_jev` (any_of), `jev` (jev), `rules_and_llm` (any_of), `llm` (llm) |
| `router` | destination | `naive_bayes` (naive_bayes) | `rules` (rules), `jev` (jev), `llm` (llm), `cascade` (cascade) |

## Validation

Benchmark `test` (n=600) with safety_screen=`rules`, router=`naive_bayes`:

| Metric | Value |
|---|---|
| `destination` accuracy | 99.3% |
| Macro-F1 | 0.992 |
| Expected calibration error | 0.005 |
| Brier score | 0.009 |
| AUTO rate | 94.5% |
| HUMAN_REVIEW rate | 5.2% |
| ABSTAIN rate | 0.3% |
| BLOCK rate | 0.0% |
| Accuracy of AUTO decisions | 100.0% |
| False automation rate | 0.0% |
| Expected human override rate | 9.7% |
| Latency p50 / p95 | 0.14 / 0.19 ms |
| Cost per decision | $0.00000 |
| Critical `urgency=urgent` | 23/23 detected, 0 automated |

Per-label performance:

| Label | Precision | Recall | F1 | n |
|---|---|---|---|---|
| `front_desk` | 100.0% | 97.9% | 0.990 | 193 |
| `medical_assistant` | 100.0% | 100.0% | 1.000 | 161 |
| `nurse` | 100.0% | 100.0% | 1.000 | 117 |
| `physician` | 100.0% | 100.0% | 1.000 | 67 |
| `billing` | 93.9% | 100.0% | 0.969 | 62 |

Subgroups:

| Subgroup | n | Accuracy | AUTO rate | Abstention |
|---|---|---|---|---|
| language=en | 548 | 100.0% | 95.6% | 0.0% |
| language=es | 52 | 92.3% | 82.7% | 3.8% |
| channel=portal | 482 | 99.2% | 94.2% | 0.4% |
| channel=sms | 118 | 100.0% | 95.8% | 0.0% |
| age_band=0-17 | 22 | 100.0% | 90.9% | 0.0% |
| age_band=18-39 | 175 | 99.4% | 93.7% | 0.0% |
| age_band=40-64 | 246 | 99.2% | 95.1% | 0.4% |
| age_band=65+ | 157 | 99.4% | 94.9% | 0.6% |
| topic=anaphylaxis | 2 | 100.0% | 0.0% | 0.0% |
| topic=bill | 34 | 100.0% | 100.0% | 0.0% |
| topic=bleeding | 2 | 100.0% | 0.0% | 0.0% |
| topic=breathing | 10 | 100.0% | 0.0% | 0.0% |
| topic=cancel | 22 | 100.0% | 100.0% | 0.0% |
| topic=chest_pain | 5 | 100.0% | 0.0% | 0.0% |
| topic=contact_update | 14 | 100.0% | 100.0% | 0.0% |
| topic=diagnosis | 11 | 100.0% | 100.0% | 0.0% |
| topic=forms | 12 | 100.0% | 100.0% | 0.0% |
| topic=infant_fever | 2 | 100.0% | 0.0% | 0.0% |
| topic=insurance | 11 | 100.0% | 100.0% | 0.0% |
| topic=logistics | 22 | 100.0% | 100.0% | 0.0% |
| topic=mixed_clinical | 7 | 100.0% | 100.0% | 0.0% |
| topic=order_status | 17 | 100.0% | 100.0% | 0.0% |
| topic=outside_records | 24 | 100.0% | 100.0% | 0.0% |
| topic=payment | 17 | 100.0% | 100.0% | 0.0% |
| topic=portal | 20 | 100.0% | 100.0% | 0.0% |
| topic=referral_status | 20 | 100.0% | 100.0% | 0.0% |
| topic=refill | 88 | 100.0% | 100.0% | 0.0% |
| topic=reschedule | 37 | 100.0% | 100.0% | 0.0% |
| topic=results | 41 | 100.0% | 100.0% | 0.0% |
| topic=schedule | 59 | 93.2% | 84.7% | 3.4% |
| topic=side_effects | 23 | 100.0% | 100.0% | 0.0% |
| topic=stroke | 2 | 100.0% | 0.0% | 0.0% |
| topic=symptoms | 42 | 100.0% | 100.0% | 0.0% |
| topic=treatment_change | 15 | 100.0% | 100.0% | 0.0% |
| topic=vaccines | 11 | 100.0% | 100.0% | 0.0% |
| topic=vague | 19 | 100.0% | 94.7% | 0.0% |
| topic=wound_care | 11 | 100.0% | 100.0% | 0.0% |

Calibration (reliability):

| Confidence bin | n | Mean confidence | Accuracy |
|---|---|---|---|
| 0.4 to 0.5 | 2 | 46.4% | 50.0% |
| 0.5 to 0.6 | 1 | 59.3% | 100.0% |
| 0.6 to 0.7 | 2 | 64.9% | 0.0% |
| 0.7 to 0.8 | 2 | 73.4% | 50.0% |
| 0.8 to 0.9 | 3 | 86.0% | 100.0% |
| 0.9 to 1.0 | 590 | 99.9% | 100.0% |

Threshold sweep (if everything ≥ threshold were automated):

| Threshold | Coverage | Accuracy | Errors |
|---|---|---|---|
| 0.50 | 99.7% | 99.5% | 3 |
| 0.60 | 99.5% | 99.5% | 3 |
| 0.70 | 99.2% | 99.8% | 1 |
| 0.80 | 98.8% | 100.0% | 0 |
| 0.85 | 98.5% | 100.0% | 0 |
| 0.90 | 98.3% | 100.0% | 0 |
| 0.95 | 98.2% | 100.0% | 0 |
| 0.97 | 97.8% | 100.0% | 0 |
| 0.99 | 96.3% | 100.0% | 0 |

## Provider comparison

Every configuration below ran on the same records. Remote models are named because they are a deployment choice. Synthetic and hand-written data: compare configurations with each other, not with performance at a real site.

**`red_flags`** (n=38)

| Configuration | Accuracy | Macro-F1 | ECE | AUTO | False automation | Critical detected | p50 latency | Cost / decision | Errors |
|---|---|---|---|---|---|---|---|---|---|
| safety_screen=rules_and_llm (anthropic/claude-sonnet-5) · router=llm (anthropic/claude-sonnet-5) | 100.0% | 1.000 | 0.089 | 2.6% | n/a | 28/28 (0 automated) | 2,340 ms | $0.00324 | 0 |
| safety_screen=rules · router=naive_bayes | 75.0% | 0.857 | 0.181 | 13.2% | n/a | 28/28 (0 automated) | 0.08 ms | $0.00000 | 0 |
| safety_screen=rules · router=rules | 21.4% | 0.353 | 0.300 | 0.0% | n/a | 28/28 (0 automated) | 0.12 ms | $0.00000 | 0 |

**`red_flags_holdout`** (n=30)

| Configuration | Accuracy | Macro-F1 | ECE | AUTO | False automation | Critical detected | p50 latency | Cost / decision | Errors |
|---|---|---|---|---|---|---|---|---|---|
| safety_screen=rules_and_llm (anthropic/claude-sonnet-5) · router=llm (anthropic/claude-sonnet-5) | 100.0% | 1.000 | 0.072 | 3.3% | n/a | 20/20 (0 automated) | 2,313 ms | $0.00324 | 0 |
| safety_screen=rules · router=naive_bayes | 95.0% | 0.974 | 0.194 | 40.0% | 0.0% | 4/20 (7 automated) | 0.10 ms | $0.00000 | 0 |
| safety_screen=rules · router=rules | 10.0% | 0.182 | 0.278 | 0.0% | n/a | 4/20 (0 automated) | 0.13 ms | $0.00000 | 0 |

**`test`** (n=100)

| Configuration | Accuracy | Macro-F1 | ECE | AUTO | False automation | Critical detected | p50 latency | Cost / decision | Errors |
|---|---|---|---|---|---|---|---|---|---|
| safety_screen=rules · router=naive_bayes | 100.0% | 1.000 | 0.001 | 98.0% | 0.0% | 2/2 (0 automated) | 0.15 ms | $0.00000 | 0 |
| safety_screen=rules_and_llm (anthropic/claude-sonnet-5) · router=llm (anthropic/claude-sonnet-5) | 99.0% | 0.989 | 0.124 | 40.0% | 0.0% | 2/2 (0 automated) | 2,397 ms | $0.00335 | 0 |
| safety_screen=rules · router=rules | 89.0% | 0.883 | 0.282 | 11.0% | 0.0% | 2/2 (0 automated) | 0.21 ms | $0.00000 | 0 |


## Known failure modes

- The keyword red-flag screen alone misses most emergencies phrased in ways it does not list. On hand-written paraphrases it caught 28/28 of the set its patterns were tuned on but only 4/20 of a held-out set. Jev caught 48/48 across both with no false alarms. Deploy with safety_screen=rules_and_jev; keyword rules are an offline floor, not the safety net. The rules can also over-trigger on symptoms that happened in the past.
- Negation handling is a fixed-window heuristic. "No chest pain" is handled; complex or long-distance negation is not.
- Messages that mix administrative and clinical requests are sometimes sent to the administrative team.
- Requests for outside records that mention the ER, imaging or specialists can be mistaken for clinical questions (demonstrated by the bundled local-site validation example).
- The statistical router was trained only on synthetic messages. A real site's phrasing, language mix and case mix will differ. Validate locally before any deployment.
- The public benchmark is generated from templates, so it overstates real-world accuracy. Treat it as a regression test for the pack, not as an estimate of performance at a site.
- LLM variants report verbalised probabilities, and every model is calibrated differently. Check the chosen model in the provider comparison (ECE, false automation) and fit `openhealth calibrate` before relying on it. A remote model can fail or time out, so never deploy safety_screen=llm without the rules floor (use rules_and_llm).

## Evidence and provenance

- **Synthetic public benchmark**: evals/benchmark.json, the held-out test split generated by openhealth.sandbox.synthetic_inbox.
- **Routing principle**: Route to the least specialised team that can fully handle the request. Anything that needs clinical judgement goes to a clinical team, and possible emergencies go to nurse triage.

## Version history

### 0.2.0

- New provider variants, so deployments can compare and choose models:
  - `router`: `llm` (any chat model via the AI Gateway or a local OpenAI-compatible
    server) and `cascade` (naive Bayes, then Jev when naive Bayes is below 95%);
  - `safety_screen`: `llm` and `rules_and_llm` (the keyword rules stay as the offline floor).
- With `router=jev` and `safety_screen=rules_and_jev`, one Jev call answers both questions.
- Provider comparison published in `evals/leaderboard.json` and the Decision Card.
- Questions, labels, policy, rules, data and the naive Bayes model are unchanged.

### 0.1.1

- Safety fix: "I just started to vomit a lot of blood" was screened `routine` and auto-routed.
  The keyword screen only knew exact phrasings such as "vomiting up blood".
- New `rules_and_jev` variant for `safety_screen`: rules and Jev both run and the higher
  P(urgent) wins; recommended for every deployment.
- Red-flag rules extended (GI bleeding, coughing blood, bleeding in pregnancy, head injury with
  vomiting, acute abdomen, sudden worst headache, near-fainting, overdose, stroke and
  anaphylaxis phrasings); word gaps never span sentences.
- New evaluation datasets `red_flags` (tuning) and `red_flags_holdout` (never used for
  tuning). Keyword rules alone: 28/28 and 4/20. Jev: 28/28 and 20/20, no false alarms.

### 0.1.0

- First release.
- Questions: `destination` (5 teams) and `urgency` (urgent / routine).
- Providers: deterministic red-flag screen (`rules`); naive Bayes router trained and
  temperature-calibrated on synthetic data (default); keyword router (`rules`) and Jev variants
  for both slots. The Jev endpoint and model are deployment settings (`JEV_API_URL`,
  `JEV_MODEL`), not part of the pack.
- Policy: stat nurse review for possible emergencies; AUTO at ≥0.90 confidence (≥0.95 for
  non-clinical destinations); abstain below 0.50.

---

*Generated by `openhealth card`. A Decision Card describes validation on the data named above. It is not evidence of performance at your site; run local validation first.*
