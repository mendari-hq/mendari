# Decision Card: Patient Inbox Router

**Synthetic example. Not clinically reviewed. Not for patient care.**

*Research preview. Not a medical device. Not for emergencies: patients should call 911.*

`mendari/patient-inbox-router` · version **0.4.3** · Apache-2.0 · published by Mendari reference packs (community, self-declared)

> Routes patient portal and SMS messages to the front desk, a medical assistant, a nurse, a physician or billing, and escalates possible emergencies to stat nurse triage.

| | |
|---|---|
| Source | Maintained by Mendari |
| Risk level | **0**, administrative |
| Automation | AUTO permitted by policy thresholds. Only while `safety_screen` is `rules_and_jev` or `rules_and_llm`; otherwise a person reviews. |
| Agent digest | `sha256:2b2ce45275cb7cd81d06578c3b14d76223856daf1258ebb0e9e2514a2c3370df` |
| Last validation | 2026-11-23 on `test` (n=600, sha256 `2997856d17a5…`) |

## Intended use

First-pass routing of incoming patient portal or SMS messages in an outpatient primary-care practice, with an explicit escalation path for messages that may describe an emergency.

**Why risk level 0:** The decision is which team reads a message first. The pack never replies to patients or makes a clinical judgement. A misroute delays a message but does not by itself change care. A message the urgency screen flags as a possible emergency is never auto-routed: it goes to a nurse for stat human review. Nothing is automated unless a model screens beside the keyword rules, which miss emergencies phrased in ways they do not list.

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

**`urgency`**: Could the situation in this message be a medical emergency that is happening now or getting worse?

| Label | Criteria |
|---|---|
| `urgent` | Possibly an emergency now, even when described casually or with uncertainty: chest pain or pressure; trouble breathing; stroke signs such as face drooping, arm weakness or slurred speech; thoughts of suicide or self-harm, or an intentional overdose; heavy bleeding, including vomiting or coughing up blood, or bleeding in pregnancy; a severe allergic reaction; fever in a baby under 3 months; a person who is hard to wake, fainting or newly confused; a head injury followed by vomiting or confusion; sudden severe pain in the head or belly. |
| `routine` | Not an emergency now: administrative requests, refills, questions, mild or long-standing symptoms, and past episodes that have already been treated or assessed, such as asking for records from an emergency room visit. |

Every decision is one of `AUTO`, `HUMAN_REVIEW`, `ABSTAIN` or `BLOCK`, with a priority (`routine`, `urgent`, `asap`, `stat`) and an audit record.

## Decision policy

- AUTO when `destination` confidence ≥ **0.90**; human review ≥ **0.50**; abstain below.
- `front_desk` needs ≥ **0.95** for AUTO.
- `billing` needs ≥ **0.95** for AUTO.
- AUTO only while `safety_screen` is `rules_and_jev` or `rules_and_llm`; otherwise a person reviews.
- Rule **possible-emergency**: P(`urgency` is `urgent`) ≥ 0.35 ⇒ `HUMAN_REVIEW` → `nurse`, priority `stat`. Possible emergency symptoms. Nurse triage now; do not leave in a routine queue.
- Fallback (ABSTAIN / BLOCK): Leave in the shared inbox for manual triage by the front desk (the existing workflow).

## Providers

| Slot | Answers | Default | Alternatives |
|---|---|---|---|
| `safety_screen` | urgency | `rules` (rules) | `rules_and_jev` (any_of), `jev` (jev), `rules_and_llm` (any_of), `llm` (llm) |
| `router` | destination | `naive_bayes` (naive_bayes) | `rules` (rules), `jev` (jev), `llm` (llm), `cascade` (cascade) |

## Validation

Benchmark `test` (n=600) with safety_screen=`rules`, router=`naive_bayes`. Each proportion: k of n [exact 95% interval, Clopper-Pearson].

| Metric | Value |
|---|---|
| `destination` accuracy | 596 of 600 [98.3-99.8%] |
| Macro-F1 | 0.992 |
| Expected calibration error | 0.005 |
| Expected calibration error, equal-mass bins | 0.001 |
| Brier score | 0.009 [0.002-0.017, bootstrap] |
| AUTO | 0 of 600 [0.0-0.6%] |
| HUMAN_REVIEW | 598 of 600 [98.8-99.96%] |
| ABSTAIN | 2 of 600 [0.04-1.2%] |
| BLOCK | 0 of 600 [0.0-0.6%] |
| AUTO decisions correct | no cases |
| False automation (of labelled cases) | 0 of 600 [0.0-0.6%] |
| Expected human override (of reviewed) | 3 of 598 [0.1-1.5%] |
| Latency p50 / p95 | 0.19 / 0.59 ms |
| Cost per decision | $0.00000 |
| Critical `urgency=urgent` | 23 of 23 [85.2-100.0%] detected, 0 automated (insufficient evidence: under 100 positives) |

Per-label performance:

| Label | Precision | Recall | F1 |
|---|---|---|---|
| `front_desk` | 189 of 189 [98.1-100.0%] | 189 of 193 [94.8-99.4%] | 0.990 |
| `medical_assistant` | 161 of 161 [97.7-100.0%] | 161 of 161 [97.7-100.0%] | 1.000 |
| `nurse` | 117 of 117 [96.9-100.0%] | 117 of 117 [96.9-100.0%] | 1.000 |
| `physician` | 67 of 67 [94.6-100.0%] | 67 of 67 [94.6-100.0%] | 1.000 |
| `billing` | 62 of 66 [85.2-98.3%] | 62 of 62 [94.2-100.0%] | 0.969 |

Subgroups:

| Subgroup | n | Accuracy | AUTO | Abstention |
|---|---|---|---|---|
| language=en | 548 | 548 of 548 [99.3-100.0%] | 0 of 548 [0.0-0.7%] | 0 of 548 [0.0-0.7%] |
| language=es | 52 | 48 of 52 [81.5-97.9%] | 0 of 52 [0.0-6.8%] | 2 of 52 [0.5-13.2%] |
| channel=portal | 482 | 478 of 482 [97.9-99.8%] | 0 of 482 [0.0-0.8%] | 2 of 482 [0.1-1.5%] |
| channel=sms | 118 | 118 of 118 [96.9-100.0%] | 0 of 118 [0.0-3.1%] | 0 of 118 [0.0-3.1%] |
| age_band=0-17 | 22 | 22 of 22 [84.6-100.0%] | 0 of 22 [0.0-15.4%] | 0 of 22 [0.0-15.4%] |
| age_band=18-39 | 175 | 174 of 175 [96.9-99.99%] | 0 of 175 [0.0-2.1%] | 0 of 175 [0.0-2.1%] |
| age_band=40-64 | 246 | 244 of 246 [97.1-99.9%] | 0 of 246 [0.0-1.5%] | 1 of 246 [0.01-2.2%] |
| age_band=65+ | 157 | 156 of 157 [96.5-99.98%] | 0 of 157 [0.0-2.3%] | 1 of 157 [0.02-3.5%] |
| topic=anaphylaxis | 2 | 2 of 2 [15.8-100.0%] | 0 of 2 [0.0-84.2%] | 0 of 2 [0.0-84.2%] |
| topic=bill | 34 | 34 of 34 [89.7-100.0%] | 0 of 34 [0.0-10.3%] | 0 of 34 [0.0-10.3%] |
| topic=bleeding | 2 | 2 of 2 [15.8-100.0%] | 0 of 2 [0.0-84.2%] | 0 of 2 [0.0-84.2%] |
| topic=breathing | 10 | 10 of 10 [69.2-100.0%] | 0 of 10 [0.0-30.8%] | 0 of 10 [0.0-30.8%] |
| topic=cancel | 22 | 22 of 22 [84.6-100.0%] | 0 of 22 [0.0-15.4%] | 0 of 22 [0.0-15.4%] |
| topic=chest_pain | 5 | 5 of 5 [47.8-100.0%] | 0 of 5 [0.0-52.2%] | 0 of 5 [0.0-52.2%] |
| topic=contact_update | 14 | 14 of 14 [76.8-100.0%] | 0 of 14 [0.0-23.2%] | 0 of 14 [0.0-23.2%] |
| topic=diagnosis | 11 | 11 of 11 [71.5-100.0%] | 0 of 11 [0.0-28.5%] | 0 of 11 [0.0-28.5%] |
| topic=forms | 12 | 12 of 12 [73.5-100.0%] | 0 of 12 [0.0-26.5%] | 0 of 12 [0.0-26.5%] |
| topic=infant_fever | 2 | 2 of 2 [15.8-100.0%] | 0 of 2 [0.0-84.2%] | 0 of 2 [0.0-84.2%] |
| topic=insurance | 11 | 11 of 11 [71.5-100.0%] | 0 of 11 [0.0-28.5%] | 0 of 11 [0.0-28.5%] |
| topic=logistics | 22 | 22 of 22 [84.6-100.0%] | 0 of 22 [0.0-15.4%] | 0 of 22 [0.0-15.4%] |
| topic=mixed_clinical | 7 | 7 of 7 [59.0-100.0%] | 0 of 7 [0.0-41.0%] | 0 of 7 [0.0-41.0%] |
| topic=order_status | 17 | 17 of 17 [80.5-100.0%] | 0 of 17 [0.0-19.5%] | 0 of 17 [0.0-19.5%] |
| topic=outside_records | 24 | 24 of 24 [85.8-100.0%] | 0 of 24 [0.0-14.2%] | 0 of 24 [0.0-14.2%] |
| topic=payment | 17 | 17 of 17 [80.5-100.0%] | 0 of 17 [0.0-19.5%] | 0 of 17 [0.0-19.5%] |
| topic=portal | 20 | 20 of 20 [83.2-100.0%] | 0 of 20 [0.0-16.8%] | 0 of 20 [0.0-16.8%] |
| topic=referral_status | 20 | 20 of 20 [83.2-100.0%] | 0 of 20 [0.0-16.8%] | 0 of 20 [0.0-16.8%] |
| topic=refill | 88 | 88 of 88 [95.9-100.0%] | 0 of 88 [0.0-4.1%] | 0 of 88 [0.0-4.1%] |
| topic=reschedule | 37 | 37 of 37 [90.5-100.0%] | 0 of 37 [0.0-9.5%] | 0 of 37 [0.0-9.5%] |
| topic=results | 41 | 41 of 41 [91.4-100.0%] | 0 of 41 [0.0-8.6%] | 0 of 41 [0.0-8.6%] |
| topic=schedule | 59 | 55 of 59 [83.5-98.1%] | 0 of 59 [0.0-6.1%] | 2 of 59 [0.4-11.7%] |
| topic=side_effects | 23 | 23 of 23 [85.2-100.0%] | 0 of 23 [0.0-14.8%] | 0 of 23 [0.0-14.8%] |
| topic=stroke | 2 | 2 of 2 [15.8-100.0%] | 0 of 2 [0.0-84.2%] | 0 of 2 [0.0-84.2%] |
| topic=symptoms | 42 | 42 of 42 [91.6-100.0%] | 0 of 42 [0.0-8.4%] | 0 of 42 [0.0-8.4%] |
| topic=treatment_change | 15 | 15 of 15 [78.2-100.0%] | 0 of 15 [0.0-21.8%] | 0 of 15 [0.0-21.8%] |
| topic=vaccines | 11 | 11 of 11 [71.5-100.0%] | 0 of 11 [0.0-28.5%] | 0 of 11 [0.0-28.5%] |
| topic=vague | 19 | 19 of 19 [82.4-100.0%] | 0 of 19 [0.0-17.6%] | 0 of 19 [0.0-17.6%] |
| topic=wound_care | 11 | 11 of 11 [71.5-100.0%] | 0 of 11 [0.0-28.5%] | 0 of 11 [0.0-28.5%] |

Calibration (reliability):

| Confidence bin | Mean confidence | Correct |
|---|---|---|
| 0.4 to 0.5 | 0.464 | 1 of 2 [1.3-98.7%] |
| 0.5 to 0.6 | 0.593 | 1 of 1 [2.5-100.0%] |
| 0.6 to 0.7 | 0.649 | 0 of 2 [0.0-84.2%] |
| 0.7 to 0.8 | 0.734 | 1 of 2 [1.3-98.7%] |
| 0.8 to 0.9 | 0.860 | 3 of 3 [29.2-100.0%] |
| 0.9 to 1.0 | 0.999 | 590 of 590 [99.4-100.0%] |

Threshold sweep (if everything ≥ threshold were automated):

| Threshold | Would be automated | Of those, correct |
|---|---|---|
| 0.50 | 598 of 600 [98.8-99.96%] | 595 of 598 [98.5-99.9%] |
| 0.60 | 597 of 600 [98.5-99.9%] | 594 of 597 [98.5-99.9%] |
| 0.70 | 595 of 600 [98.1-99.7%] | 594 of 595 [99.1-99.996%] |
| 0.80 | 593 of 600 [97.6-99.5%] | 593 of 593 [99.4-100.0%] |
| 0.85 | 591 of 600 [97.2-99.3%] | 591 of 591 [99.4-100.0%] |
| 0.90 | 590 of 600 [97.0-99.2%] | 590 of 590 [99.4-100.0%] |
| 0.95 | 589 of 600 [96.7-99.1%] | 589 of 589 [99.4-100.0%] |
| 0.97 | 587 of 600 [96.3-98.8%] | 587 of 587 [99.4-100.0%] |
| 0.99 | 578 of 600 [94.5-97.7%] | 578 of 578 [99.4-100.0%] |

## Provider comparison

Public set: regression check, not a ranking. Nothing here is graded: the agent's own sets are public, in the repository and in the package, so a configuration can be tuned to them, and grades and rankings are given only on frozen hidden sets. The rows are a listing order, not a ranking: those measured on this version of the agent, on records where a critical label has at least 100 positives, come first, the critical cases caught first and never accuracy alone; every other row follows, by configuration name.

Each table below is one dataset at one size, and the configurations in a table that records the dataset's hash ran on exactly the same records. Remote models are named because they are a deployment choice. Synthetic and hand-written data: compare configurations with each other, not with performance at a real site. A row measured on an earlier version of the agent says so beside its configuration: its AUTO and false automation followed that version's policy.

**`red_flags`** (n=38; dataset hash not recorded; k of n [exact 95% interval, Clopper-Pearson])

| Configuration | Critical detected | False automation | Accuracy | AUTO | Macro-F1 | ECE | p50 latency | Cost / decision | Errors |
|---|---|---|---|---|---|---|---|---|---|
| safety_screen=llm (anthropic/claude-haiku-4.5) · router=llm (anthropic/claude-haiku-4.5) (version 0.3.0, its agent digest not recorded) | 28 of 28 [87.7-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 6 of 38 [6.0-31.3%] | 1.000 | 0.069 | 9,502 ms | $0.00461 | 0 |
| safety_screen=llm (anthropic/claude-sonnet-5) · router=llm (anthropic/claude-sonnet-5) (version 0.3.0, its agent digest not recorded) | 28 of 28 [87.7-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 2 of 38 [0.6-17.7%] | 1.000 | 0.075 | 2,475 ms | $0.00351 | 0 |
| safety_screen=llm (openai/gpt-oss-120b) · router=llm (openai/gpt-oss-120b) (version 0.3.0, its agent digest not recorded) | 27 of 28 [81.7-99.9%] (0 automated; insufficient evidence) | n/a | n/a | 1 of 38 [0.1-13.8%] | 0.880 | 0.184 | 1,257 ms | $0.00017 | 1 (invalid_response 1) |
| safety_screen=rules · router=naive_bayes (version 0.4.0, its agent digest not recorded) | 28 of 28 [87.7-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 38 [0.0-9.3%] | 0.857 | 0.181 | 0.09 ms | $0.00000 | 0 |
| safety_screen=rules · router=rules (version 0.4.0, its agent digest not recorded) | 28 of 28 [87.7-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 38 [0.0-9.3%] | 0.353 | 0.300 | 0.13 ms | $0.00000 | 0 |
| safety_screen=rules_and_jev (jev-1.13.0) · router=jev (jev-1.13.0) (version 0.3.0, its agent digest not recorded) | 28 of 28 [87.7-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 7 of 38 [7.7-34.3%] | 1.000 | 0.007 | 192 ms | $0.00003 | 0 |
| safety_screen=rules_and_llm (anthropic/claude-haiku-4.5) · router=llm (anthropic/claude-haiku-4.5) (version 0.3.0, its agent digest not recorded) | 28 of 28 [87.7-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 6 of 38 [6.0-31.3%] | 1.000 | 0.071 | 10,039 ms | $0.00477 | 0 |
| safety_screen=rules_and_llm (anthropic/claude-sonnet-5) · router=llm (anthropic/claude-sonnet-5) (version 0.3.0, its agent digest not recorded) | 28 of 28 [87.7-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 38 [0.0-9.3%] | 0.982 | 0.075 | 2,386 ms | $0.00352 | 1 (invalid_response 1) |
| safety_screen=rules_and_llm (openai/gpt-oss-120b) · router=llm (openai/gpt-oss-120b) (version 0.3.0, its agent digest not recorded) | 28 of 28 [87.7-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 1 of 38 [0.1-13.8%] | 0.923 | 0.104 | 1,276 ms | $0.00017 | 0 |

Accuracy and false automation are n/a on a row recorded before leaderboards kept the counts they are of (mendari.leaderboard/1).

**`red_flags_holdout`** (n=30; dataset hash not recorded; k of n [exact 95% interval, Clopper-Pearson])

| Configuration | Critical detected | False automation | Accuracy | AUTO | Macro-F1 | ECE | p50 latency | Cost / decision | Errors |
|---|---|---|---|---|---|---|---|---|---|
| safety_screen=llm (anthropic/claude-haiku-4.5) · router=llm (anthropic/claude-haiku-4.5) (version 0.3.0, its agent digest not recorded) | 20 of 20 [83.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 6 of 30 [7.7-38.6%] | 0.974 | 0.103 | 9,112 ms | $0.00460 | 0 |
| safety_screen=llm (anthropic/claude-sonnet-5) · router=llm (anthropic/claude-sonnet-5) (version 0.3.0, its agent digest not recorded) | 20 of 20 [83.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 1 of 30 [0.1-17.2%] | 0.974 | 0.071 | 2,356 ms | $0.00354 | 1 (invalid_response 1) |
| safety_screen=llm (openai/gpt-oss-120b) · router=llm (openai/gpt-oss-120b) (version 0.3.0, its agent digest not recorded) | 19 of 20 [75.1-99.9%] (0 automated; insufficient evidence) | n/a | n/a | 2 of 30 [0.8-22.1%] | 0.947 | 0.140 | 1,185 ms | $0.00016 | 0 |
| safety_screen=rules · router=naive_bayes (version 0.4.0, its agent digest not recorded) | 4 of 20 [5.7-43.7%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 30 [0.0-11.6%] | 0.974 | 0.194 | 0.09 ms | $0.00000 | 0 |
| safety_screen=rules · router=rules (version 0.4.0, its agent digest not recorded) | 4 of 20 [5.7-43.7%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 30 [0.0-11.6%] | 0.182 | 0.278 | 0.13 ms | $0.00000 | 0 |
| safety_screen=rules_and_jev (jev-1.13.0) · router=jev (jev-1.13.0) (version 0.3.0, its agent digest not recorded) | 20 of 20 [83.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 8 of 30 [12.3-45.9%] | 1.000 | 0.007 | 172 ms | $0.00003 | 0 |
| safety_screen=rules_and_llm (anthropic/claude-haiku-4.5) · router=llm (anthropic/claude-haiku-4.5) (version 0.3.0, its agent digest not recorded) | 20 of 20 [83.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 8 of 30 [12.3-45.9%] | 0.974 | 0.031 | 10,327 ms | $0.00452 | 0 |
| safety_screen=rules_and_llm (anthropic/claude-sonnet-5) · router=llm (anthropic/claude-sonnet-5) (version 0.3.0, its agent digest not recorded) | 20 of 20 [83.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 1 of 30 [0.1-17.2%] | 1.000 | 0.046 | 2,407 ms | $0.00352 | 0 |
| safety_screen=rules_and_llm (openai/gpt-oss-120b) · router=llm (openai/gpt-oss-120b) (version 0.3.0, its agent digest not recorded) | 20 of 20 [83.2-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 2 of 30 [0.8-22.1%] | 1.000 | 0.248 | 1,266 ms | $0.00017 | 0 |

Accuracy and false automation are n/a on a row recorded before leaderboards kept the counts they are of (mendari.leaderboard/1).

**`test`** (n=100; dataset hash not recorded; k of n [exact 95% interval, Clopper-Pearson])

| Configuration | Critical detected | False automation | Accuracy | AUTO | Macro-F1 | ECE | p50 latency | Cost / decision | Errors |
|---|---|---|---|---|---|---|---|---|---|
| safety_screen=llm (anthropic/claude-haiku-4.5) · router=llm (anthropic/claude-haiku-4.5) (version 0.3.0, its agent digest not recorded) | 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 85 of 100 [76.5-91.4%] | 0.968 | 0.034 | 6,206 ms | $0.00413 | 0 |
| safety_screen=llm (anthropic/claude-sonnet-5) · router=llm (anthropic/claude-sonnet-5) (version 0.3.0, its agent digest not recorded) | 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 47 of 100 [36.9-57.2%] | 0.989 | 0.112 | 2,365 ms | $0.00362 | 0 |
| safety_screen=llm (openai/gpt-oss-120b) · router=llm (openai/gpt-oss-120b) (version 0.3.0, its agent digest not recorded) | 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 48 of 100 [37.9-58.2%] | 0.961 | 0.086 | 1,237 ms | $0.00017 | 0 |
| safety_screen=rules · router=cascade (jev-1.13.0) (version 0.3.0, its agent digest not recorded) | 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 98 of 100 [93.0-99.8%] | 1.000 | 0.001 | 0.42 ms | $0.00000 | 0 |
| safety_screen=rules · router=naive_bayes (version 0.4.0, its agent digest not recorded) | 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 1.000 | 0.001 | 0.16 ms | $0.00000 | 0 |
| safety_screen=rules · router=rules (version 0.4.0, its agent digest not recorded) | 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 0 of 100 [0.0-3.6%] | 0.883 | 0.282 | 0.23 ms | $0.00000 | 0 |
| safety_screen=rules_and_jev (jev-1.13.0) · router=jev (jev-1.13.0) (version 0.3.0, its agent digest not recorded) | 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 91 of 100 [83.6-95.8%] | 0.984 | 0.018 | 205 ms | $0.00003 | 0 |
| safety_screen=rules_and_llm (anthropic/claude-haiku-4.5) · router=llm (anthropic/claude-haiku-4.5) (version 0.3.0, its agent digest not recorded) | 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 84 of 100 [75.3-90.6%] | 0.984 | 0.043 | 6,916 ms | $0.00423 | 0 |
| safety_screen=rules_and_llm (anthropic/claude-sonnet-5) · router=llm (anthropic/claude-sonnet-5) (version 0.3.0, its agent digest not recorded) | 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 40 of 100 [30.3-50.3%] | 0.974 | 0.116 | 2,402 ms | $0.00361 | 0 |
| safety_screen=rules_and_llm (openai/gpt-oss-120b) · router=llm (openai/gpt-oss-120b) (version 0.3.0, its agent digest not recorded) | 2 of 2 [15.8-100.0%] (0 automated; insufficient evidence) | n/a | n/a | 47 of 100 [36.9-57.2%] | 0.937 | 0.080 | 1,281 ms | $0.00017 | 0 |

Accuracy and false automation are n/a on a row recorded before leaderboards kept the counts they are of (mendari.leaderboard/1).


## Known failure modes

- The keyword red-flag screen alone misses most emergencies phrased in ways it does not list. On hand-written paraphrases it caught 28/28 of the set its patterns were tuned on but only 4/20 of a held-out set, and 7 of the emergencies it missed would have been routed without anyone reading them. Jev caught 48/48 across both with no false alarms. So the policy allows AUTO only while the screen includes a model (`automation_requires` with `safety_screen=rules_and_jev` or `rules_and_llm`); with the rules alone, every message goes to a person. The rules can also over-trigger on symptoms that happened in the past.
- Negation handling is a fixed-window heuristic. "No chest pain" is handled; complex or long-distance negation is not.
- Messages that mix administrative and clinical requests are sometimes sent to the administrative team.
- Requests for outside records that mention the ER, imaging or specialists can be mistaken for clinical questions (demonstrated by the bundled local-site validation example).
- The statistical router was trained only on synthetic messages. A real site's phrasing, language mix and case mix will differ. Validate locally before any deployment.
- The public benchmark is generated from templates, so it overstates real-world accuracy. Treat it as a regression test for the pack, not as an estimate of performance at a site.
- LLM variants report verbalised probabilities, and every model is calibrated differently. Check the chosen model in the provider comparison (ECE, false automation) and fit `mendari calibrate` before relying on it. A remote model can fail or time out, so never deploy safety_screen=llm without the rules floor (use rules_and_llm).

## Evidence and provenance

- **Synthetic public benchmark**: evals/benchmark.json, the held-out test split generated by tests/support/synthetic_inbox.py.
- **Routing principle**: Route to the least specialised team that can fully handle the request. Anything that needs clinical judgement goes to a clinical team, and possible emergencies go to nurse triage.

## Version history

Renamed on 2026-09-30, when the project became Mendari: this pack was `openhealth/patient-inbox-router`.
Its id, its publisher's name and its `https://mendari.co/...` identifiers changed; its criteria,
rules, models, data and results did not.

#### 0.4.3

- The CDS Hooks card has its own template (`cds_hooks.card`, LP-23j). A team's card keeps the
  engine's words, with the team's display name, so it still says whether the message was routed
  or needs a person to confirm the team ("Routed to Nurse", "Review: route to Nurse"). Its
  indicator is the team's: `info` for the front desk, a medical assistant and billing, `warning`
  for a nurse and a physician (it was `warning` for every message a person reviews, and `info`
  for every routed one). A possible emergency is still `critical`. A message with no suggested
  team says "Message: no suggested team; it stays in the shared inbox for manual triage",
  whether the engine abstained or refused the message as invalid input (BLOCK, a body over
  10,000 characters for one): it said "No recommendation; to manual triage" for the first and
  "Blocked; to manual triage" for the second, and the card's detail still says which. The
  suggestion is "Create a routing task" (it was "Create routing task for" the team). Every
  decision still shows a card, and the suggested Task, its text and its team's code are
  unchanged.
- Criteria, rules, providers, policy, models and data are unchanged; every decision is identical
  (the benchmark's figures, its ledger and the FHIR decisions golden). The digest changes because
  the manifest does.

### 0.4.2

- A comment in the manifest no longer names a default chat model: Mendari has none. The
  `llm` and `rules_and_llm` variants ask the model `LLM_MODEL` names; without it the `llm`
  member is `not_configured` and every decision goes to a person (the keyword rules still
  answer inside `rules_and_llm`).
- Criteria, rules, providers, policy, models and data are unchanged; every decision is identical.
  The digest changes because the manifest does.

### 0.4.1

- Every surface now says what the pack is: "Synthetic example. Not clinically reviewed. Not for
  patient care." It is derived from the new `card.provenance: synthetic`, never written by the
  pack (the Decision Card's first line, the dashboard, Try it, the review queue, CDS Hooks cards
  and discovery, the registry and the Hub).
- Each question has a `display` name, so people read words instead of keys.
- Criteria, rules, providers, policy, models and data are unchanged; every decision is identical.
  The digest changes because the manifest does.

### 0.4.0

- Safety: the policy allows AUTO only while `safety_screen` includes a model
  (`automation_requires`: `rules_and_jev` or `rules_and_llm`), as the Referral Router's does.
  On hand-written held-out messages the keyword rules alone caught 4 of 20 emergencies and
  automated 7 of the ones they missed. With the rules alone (the offline default), every
  message now goes to a person with its suggested team; nothing else about the decision changes.
- Questions, rules, data and the naive Bayes model are unchanged. The provider-comparison rows
  for the rules alone were re-run on 0.4.0. The other rows are from 0.3.0: their accuracy and
  critical detection still hold, but two kinds would now automate nothing: a model screen
  without the rules (`safety_screen=llm`) and the `cascade` router behind the rules screen.

### 0.3.0

- Safety: the `possible-emergency` rule now escalates when P(urgent) ≥ 0.35 (spec 0.2
  `min_probability`). Before, `urgent` also had to be the most likely label, so the documented
  0.35 threshold never applied and the effective threshold was 0.5.
- `urgency` reworded so its instructions and criteria describe the same thing: a possible
  emergency happening now. The old instructions also included "same-day" care and "answer
  urgent if in doubt"; the doubt threshold now lives in the policy. The criteria list common
  triage red flags and say that past, already-treated episodes are routine.
- `spec_version: "0.2"`. Routing questions, data and the naive Bayes model are unchanged.

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

*Generated by `mendari card`. A Decision Card describes validation on the data named above. It is not evidence of performance at your site; run local validation first.*
