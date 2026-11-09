Drafted by a build session for a clinician to review. Not clinically reviewed.

# The referral router's and prior authorization's requirements: a brief for the clinician reviewers

_Written 2026-11-07 (US Eastern). Part 1 is for a referral coordinator and a cardiologist
(launch prerequisite B21.2); part 2 is for a utilization-management reviewer (B21.3); both are
under launch gate `clinician-review`. On 2026-11-07 the owner, a licensed physician who wrote the
five reference packs, decided that their own disclosed physician review counts for these five
packs. Nothing here has been reviewed by a clinician. Each review is recorded by the owner in
the gate `clinician-review`'s attestation, never by a build session._

## The packs this brief describes

| | Referral router | Prior authorization preflight |
|---|---|---|
| Pack | `mendari/referral-router` | `mendari/prior-auth-preflight` |
| Version | 0.3.0 | 0.3.0 |
| Digest | `sha256:539e0ff237c2b84e9a3baed670cb608ff95d730f35bbda555ffb2c8b4b47ddee` | `sha256:678f401a4ab0e388deb6387e9dd9680f6d5fafd85434b367538d333d1dd8160c` |
| Read with | `uv run mendari validate packs/referral-router` | `uv run mendari validate packs/prior-auth-preflight` |

Both read on `main` at `af26a868`. Work in progress on 2026-11-07 changes both packs' cards. If
any pull request changes a pack after this brief, its version and digest change: re-read the
quoted lines and refresh this brief before that pack's review is recorded.

Both packs state that their requirements are synthetic and were not written or reviewed by a
clinician (`referral-router/pack.yaml:55-56`; `prior-auth-preflight/pack.yaml:56-58`, which adds
"or a payer"). A site sets its own.

---

# Part 1: the referral router

Covered: the documents each specialty requires, and the urgency criteria with the keyword rules
that screen for them. Paths are under `packs/referral-router/`.

## What the pack does, and who sees its result

When a primary-care clinician signs a specialty referral (CDS Hooks `order-sign`) or it reaches a
referral office, the pack suggests the specialty (one of eight, or `other`), screens for a
referral that should be seen within two weeks, checks the record for what that specialty needs,
and suggests the next step (`pack.yaml:5-8`, `:35-39`):

| Next step | Meaning | Who sees it | Where |
|---|---|---|---|
| `urgent_review` | A person reviews it today: possibly urgent, or marked urgent by the referrer | The referral office, at urgent priority; a **critical** card to the clinician signing | `pack.yaml:180-182`, `policy.yaml:25-46`, `pack.yaml:344` |
| `return_for_info` | Sent back to the referrer with what is missing | A warning card to the clinician signing | `pack.yaml:184-186`, `:343` |
| `coordinator_review` | None of the eight specialties fits | The referral office; no card | `pack.yaml:183` |
| `schedule` | Complete and routine: goes to the specialty's scheduling queue | The specialty's schedulers; no card | `pack.yaml:187` |

Only `return_for_info` and `urgent_review` show a card (`pack.yaml:347-352`). The pack is at risk
level 1 (`pack.yaml:22-30`): a confident `schedule` may be automated, but only while a model
screens beside the keyword rules; with the rules alone every referral goes to a person
(`policy.yaml:19-23`). `urgent_review` is never automated (`policy.yaml:14-15`). Each decision
becomes a FHIR Task in the specialty's referral queue (`fhir/mapping.yaml:87-91`).

## The required documents, exactly as built

Which items a specialty needs is the decision table (`pack.yaml:198-215`); whether each is on
file is computed from the record (`logic/checklist.yaml`). An urgent or marked-urgent referral,
or one for `other`, goes to a person before any of these are checked (`pack.yaml:199-201`).

| Requirement | Applies to | On file when | Where |
|---|---|---|---|
| A clear question for the specialist | Every specialty | The referral states a request ("please assess", "?angina", "pls eval") | `pack.yaml:142-150`, `:202` |
| Visit note in the last 180 days | Every specialty | A document coded LOINC `11506-3` (progress note) or `11488-4` (consult note), dated in the last 180 days | `pack.yaml:202`, `checklist.yaml:8-16` |
| ECG in the last 365 days | Cardiology | A report or document coded LOINC `11524-6` (EKG study), dated in the last 365 days | `pack.yaml:203-206`, `checklist.yaml:18-22` |
| Imaging report in the last 365 days | Orthopedics | A report coded LOINC `18748-4` (diagnostic imaging study) or `18726-0` (radiology studies), dated in the last 365 days | `pack.yaml:207-210`, `checklist.yaml:24-32` |
| Labs in the last 90 days | Endocrinology, gastroenterology, rheumatology | **Any** laboratory result dated in the last 90 days (code `*`) | `pack.yaml:211-214`, `checklist.yaml:34-37` |
| (nothing further) | Dermatology, neurology, pulmonology | | `pack.yaml:215` |

An undated document counts as not on file (`checklist.yaml`, `undated: "no"`). A visit note under
another code, for example a history and physical (LOINC `34117-2`), reads as missing, so a
complete referral goes back to the referrer (`pack.yaml:65-70`).

## The urgency criteria, exactly as built

The question is "Should the patient be seen by the specialist within two weeks?"
(`pack.yaml:127`).

| Label | Criteria (verbatim) | Where |
|---|---|---|
| urgent | Possibly: a suspected cancer (a changing or bleeding skin lesion, a new lung mass or nodule, coughing up blood in a smoker, difficulty swallowing or rectal bleeding with weight loss, iron-deficiency anaemia, painless jaundice, a persistent hoarse voice or visible blood in the urine); a progressive neurological problem or a first seizure; new exertional chest pain, fainting on exertion or new heart failure; suspected giant cell arteritis; a hot swollen joint with fever; back pain with saddle numbness or bladder change; being unable to bear weight after a fall; a dangerous calcium, sodium or glucose level. | `pack.yaml:129-137` |
| routine | Not time-critical: stable or long-standing problems, symptoms already investigated with normal results, surveillance, requests for advice or medication review, and past events that have resolved. | `pack.yaml:138-141` |

A referral marked urgent, ASAP or stat by the referrer also goes to urgent review
(`checklist.yaml:39-42`, `policy.yaml:39-46`). A possibly urgent referral (P(urgent) >= 0.3)
goes to a person today with "Review it today; do not leave it in the routine queue"
(`policy.yaml:25-38`). Emergencies are out of scope: "A referral is not the route for a patient
who needs the emergency department today" (`pack.yaml:48-49`).

## The keyword urgency rules, exactly as built

`logic/triage_rules.yaml:18-126`. Routine starts at 4.0; every pattern weighs 7, so any one
match gives P(urgent) 0.95 (`:8-10`). Patterns are negatable within 30 characters by "no",
"not", "without", "denies", "denied", "never", "normal" (`:15-16`), except `low-sodium`. On the
held-out hand-written referrals the rules alone caught 7 of 12 (`pack.yaml:77-82`).

| Area | Rule | Matches, in short | Line |
|---|---|---|---|
| Heart | exertional-chest-pain | chest pain, tightness or pressure with exertion, walking, stairs, exercise; "?angina", "r/o angina" | `:25-28` |
| Heart | exertional-syncope | fainted, syncope, collapsed, passed out with exercise, exertion, running | `:29-32` |
| Heart | new-heart-failure | "?heart failure", new heart failure, breathlessness with ankle or leg swelling | `:33-36` |
| Skin | changing-skin-lesion | mole or lesion grown, changed, bleeding, enlarging; "?melanoma" | `:38-41` |
| Skin | possible-skin-cancer | non-healing, ulcerated lesion, rapidly enlarging, "?SCC", crusted nodule | `:42-45` |
| Gut | dysphagia-with-weight-loss | food sticking, difficulty swallowing or dysphagia with weight loss | `:47-50` |
| Gut | gi-bleeding-or-anaemia | iron-deficiency anaemia, IDA, dark or black stools, rectal bleeding with weight loss, BRBPR | `:51-54` |
| Gut | jaundice | jaundice | `:55-58` |
| Nerves | progressive-deficit | progressive or worsening weakness or numbness; double vision getting worse | `:60-63` |
| Nerves | red-flag-headache | headache that wakes, morning vomiting, headache with vomiting | `:64-67` |
| Nerves | first-seizure | first seizure or fit, new seizure | `:68-71` |
| Nerves | giant-cell-arteritis | jaw pain or claudication, "?GCA", temporal headache, sudden visual blurring or loss | `:72-75` |
| Lungs | haemoptysis | coughing up blood, haemoptysis | `:77-80` |
| Lungs | new-lung-mass | a new nodule or mass, spiculated | `:81-84` |
| Lungs | breathlessness-with-weight-loss | worsening breathlessness with weight loss | `:85-88` |
| Bones | cauda-equina | saddle numbness or anaesthesia, urinary retention, difficulty passing urine | `:90-93` |
| Bones | hot-joint-with-fever | hot swollen joint with fever | `:94-97` |
| Bones | cannot-bear-weight | unable to bear weight | `:98-101` |
| Bones | night-bone-pain | bone pain at night, night pain with weight loss | `:102-105` |
| Metabolic | high-calcium | "calcium" or "Ca" followed by a value from 2.7 to 9.9 written with one decimal | `:107-110` |
| Metabolic | low-sodium | "sodium" or "Na" followed by 100 to 125 (not negatable) | `:111-113` |
| Metabolic | new-type-1-diabetes | ketones, "?type 1 diabetes" | `:114-117` |
| Other | visible-haematuria | blood in the urine, haematuria | `:119-122` |
| Other | persistent-hoarseness | hoarse for some weeks | `:123-126` |

What the metabolic rules do on a few referrals (run on the pack as built):

| Referral reason | Escalated by the rules |
|---|---|
| "Calcium 3.1 mmol/L, please assess." | yes |
| "Calcium 9.8, please advise on bone density." | **yes** (a normal value in mg/dL reads as high in mmol/L) |
| "Calcium 13.2 mg/dL, please assess." | **no** (a high value in mg/dL is not matched) |
| "Sodium 122, please assess." | yes |
| "Glucose 480, please assess." | **no** (no glucose rule; the criteria name glucose) |

## Questions for the referral coordinator and the cardiologist

Pick one option per question (any answer may instead be written in). The current behaviour is
marked "(as built)". Where a value looks clinically questionable, the note says why; nothing
here is a recommendation.

**R1. How recent the visit note must be.**
- a. 180 days (as built).
- b. 90 days.
- c. 365 days.

**R2. Which notes count as a visit note.** As built, a history and physical reads as missing.
- a. Progress and consult notes (as built).
- b. Also a history and physical (LOINC `34117-2`).
- c. Any clinical note.

**R3. Cardiology's ECG.**
- a. An ECG in the last 365 days, for every cardiology referral (as built).
- b. An ECG in the last 90 days, for every cardiology referral.
- c. An ECG in the last 365 days, only when the reason is chest pain, palpitations, fainting or an arrhythmia (a pack and code change).

**R4. Anything else cardiology needs.**
- a. Nothing else (as built).
- b. An echocardiogram report for a murmur or suspected heart failure.
- c. As b, and a lipid panel in the last 365 days.

**R5. Orthopedics' imaging.**
- a. Any imaging report in the last 365 days (as built).
- b. An X-ray of the affected area in the last 180 days.
- c. No imaging requirement.

**R6. Labs for endocrinology, gastroenterology and rheumatology.** As built, any lab result in
the last 90 days satisfies it, whatever the test.
- a. Any lab in 90 days (as built).
- b. Specialty-specific labs in 90 days (for example a TSH or HbA1c for endocrinology, a blood count and liver panel for gastroenterology, a blood count and ESR or CRP for rheumatology).
- c. No lab requirement.

**R7. Dermatology, neurology and pulmonology.**
- a. A visit note and a clear question only (as built).
- b. Also, for pulmonology, a chest imaging report when the reason is a nodule or mass.

**R8. How urgency is split.** As built, one label: "possibly urgent, review today". Several of
the listed problems are commonly assessed the same day or in an emergency department rather
than within two weeks (back pain with saddle numbness or bladder change; a hot swollen joint
with fever; suspected giant cell arteritis with visual symptoms; a dangerous calcium, sodium or
glucose level).
- a. One label, review today (as built).
- b. Two labels: same-day (those listed above), whose card tells the signer to arrange same-day assessment, and within two weeks for the rest (a pack change).
- c. One label, with the card saying that some of these need same-day assessment.

**R9. The urgent list.**
- a. As built.
- b. Add a suspected transient ischaemic attack, and new atrial fibrillation.
- c. As b, and unexplained weight loss on its own.

**R10. Lab values in the keyword rules.** As built, calcium is read as mmol/L, so US values in
mg/dL are misread both ways, and there is no glucose rule.
- a. As built.
- b. Read calcium in mg/dL too, at the same level (2.7 mmol/L is about 10.8 mg/dL).
- c. As b, and add a glucose rule (the reviewer gives the levels).

**R11. Verdict on the referral router's requirements and urgency criteria.**
- a. Acceptable as a synthetic reference pack.
- b. Acceptable with the changes answered above.
- c. Not acceptable (say why).

---

# Part 2: prior authorization preflight

Covered: which services each synthetic payer requires prior authorization for, and the
documentation each requires. Paths are under `packs/prior-auth-preflight/`.

## What the pack does, and who sees its result

When an order that may need prior authorization is signed (CDS Hooks `order-sign`) or before an
authorization specialist submits it, the pack finds the payer's requirement set for the service,
checks each required element in the record, and suggests the next step (`pack.yaml:5-8`,
`:42-46`):

| Next step | Meaning | Card | Where |
|---|---|---|---|
| `submit_ready` | Required, and everything the payer asks for is in the record | info | `pack.yaml:213-215` |
| `needs_attention` | Something the payer asks for is missing or unclear (listed) | warning | `pack.yaml:216-218` |
| `not_required` | This payer does not require prior authorization for this service | info | `pack.yaml:219` |
| `not_checked` | No requirement set for this payer or service | none | `pack.yaml:220` |

Who sees it: prior-authorization specialists and front-office staff, through a FHIR Task in the
prior-auth queue (`fhir/mapping.yaml:72-76`), and the clinician signing the order, through the
card (`pack.yaml:352-365`). The pack is at risk level 1 (`pack.yaml:30-37`): `submit_ready` and
`not_required` (at 0.95) may be automated, but only while a model reads the notes beside the
keyword rules (`policy.yaml:13-24`). A possible red flag (P >= 0.3) always goes to a person at
urgent priority with "Consider urgent imaging and an expedited review instead of the routine
request" (`policy.yaml:26-38`). The pack does not decide medical necessity; the payer does
(`pack.yaml:55`).

## The payer lists and documentation requirements, exactly as built

Services and payers are read from codes (`logic/structured.yaml:8-19`). The fictional procedure
codes (`FX...`) are not CPT (`pack.yaml:15-18`); Acme Health and Beacon Care are synthetic payers
(`pack.yaml:131-137`).

| Service (codes) | Acme Health | Beacon Care |
|---|---|---|
| Lumbar spine MRI (`FX101`-`FX103`) | Required. Supporting diagnosis; 6 weeks of conservative treatment unless a red flag; lumbar spine X-ray (`FX111`) in the last 180 days | Required. Supporting diagnosis; 6 weeks of conservative treatment unless a red flag |
| Knee arthroscopy (`FX201`, `FX202`) | Required. Supporting diagnosis; 6 weeks of conservative treatment; knee MRI (`FX211`-`FX213`) in the last 180 days; mechanical symptoms | Required. Supporting diagnosis; 6 weeks of conservative treatment; mechanical symptoms |
| Attended sleep study (`FX301`, `FX302`) | Required. Supporting diagnosis; sleep apnoea symptoms | **Not required** |

From the decision table, `pack.yaml:231-257`: a red flag waives conservative treatment only for a
lumbar MRI (`:235-239`), and Acme's X-ray is required even when a red flag is documented
(`:244-248`).

Each element, verbatim from the pack:

| Element | Met ("yes") | Not met, or unclear | Where |
|---|---|---|---|
| Supporting diagnosis | Lumbar MRI `M54.1x` or `M51.x`; knee arthroscopy `S83.2x` or `M23.2x`; sleep study `G47.33` or `R06.83` (ICD-10-CM) | Not coded | `pack.yaml:138-146`, `structured.yaml:21-29` |
| Required imaging (Acme) | A lumbar spine X-ray for a lumbar MRI, or a knee MRI for a knee arthroscopy, in the last 180 days | None, older than 180 days, or undated | `pack.yaml:147-155`, `structured.yaml:31-37` |
| 6 weeks of conservative treatment | "At least 6 weeks of physical therapy, a home exercise programme, anti-inflammatory or pain medicines, or injections, e.g. "8 weeks of physical therapy" or "physiotherapy for 2 months"." | Less than 6 weeks or none; or mentioned with no length ("has tried some physical therapy") | `pack.yaml:158-172` |
| Red flag (waives conservative treatment for a lumbar MRI) | "A history of cancer, unexplained weight loss, fever with back pain, injection drug use, significant trauma such as a fall from height or a car accident, a progressive neurological deficit such as worsening weakness or foot drop, or saddle numbness or new bladder or bowel dysfunction." | None documented, documented as absent, resolved and unrelated, or only in a family member | `pack.yaml:173-187` |
| Mechanical knee symptoms | "The knee locks, catches, gives way or buckles." | Pain only, swelling, or painless clicking | `pack.yaml:188-196` |
| Sleep apnoea symptoms | "Loud snoring, witnessed pauses in breathing during sleep, or excessive daytime sleepiness such as falling asleep at work or while driving." | Insomnia without these; tiredness or poor sleep alone is unclear | `pack.yaml:197-205` |

Codes as matched: `M54.1*` covers radiculopathy at every level of the spine, cervical and
thoracic included (`M54.12`, `M54.14`), and `M51.*` covers thoracic and thoracolumbar disc
disorders as well as lumbar ones. Common lumbar diagnoses such as low back pain (`M54.5x`),
sciatica (`M54.3x`, `M54.4x`) and lumbar spinal stenosis (`M48.06x`) are not listed. On the
held-out hand-written notes the keyword rules found 3 of 6 red flags (`pack.yaml:94-99`).

## Questions for the utilization-management reviewer

Pick one option per question (any answer may instead be written in). The current behaviour is
marked "(as built)". Where a value looks clinically questionable, the note says why; nothing
here is a recommendation.

**P1. Diagnoses supporting a lumbar MRI.**
- a. `M54.1*` and `M51.*` (as built).
- b. Only their lumbar and lumbosacral codes (`M54.16`, `M54.17`, and `M51`'s lumbar and lumbosacral codes).
- c. As b, and sciatica (`M54.3x`, `M54.4x`) and lumbar spinal stenosis (`M48.06x`).
- d. As c, and low back pain (`M54.5x`).

**P2. What counts as conservative treatment.**
- a. 6 weeks of any of: physical therapy, home exercise, anti-inflammatory or pain medicines, injections (as built).
- b. 6 weeks including physical therapy or a supervised or home exercise programme; medicines alone are not enough.

**P3. The red-flag list.**
- a. As built.
- b. Add immunosuppression or long-term steroid use, and osteoporosis with minor trauma.
- c. As b, and age over 70 with new back pain.

**P4. Saddle numbness or new bladder or bowel dysfunction.** As built, the reason given is
"Consider urgent imaging and an expedited review instead of the routine request", the same as
for every red flag.
- a. As built.
- b. For these, the reason says the patient needs emergency assessment today.

**P5. Acme's lumbar X-ray.** As built, required in the last 180 days, even when a red flag is
documented.
- a. As built.
- b. Waived when a red flag is documented.
- c. Within 365 days, and waived when a red flag is documented.

**P6. Knee arthroscopy and osteoarthritis.** As built, a coded knee osteoarthritis does not
change anything. Arthroscopy for a degenerative tear in an arthritic knee is a common reason
real payers deny it.
- a. As built.
- b. `needs_attention` when knee osteoarthritis (`M17.x`) is coded.
- c. As b, and a current traumatic tear (`S83.2x`) with locking waives the 6 weeks of conservative treatment.

**P7. Mechanical knee symptoms.** As built, giving way or buckling counts, which can also come
from ligament instability or muscle weakness rather than a meniscal tear.
- a. Locks, catches, gives way or buckles (as built).
- b. Locks or catches only.

**P8. Acme's knee imaging.**
- a. A knee MRI in the last 180 days (as built).
- b. Weight-bearing knee X-rays in the last 180 days.
- c. Either.

**P9. Diagnoses supporting a sleep study.**
- a. `G47.33` (obstructive sleep apnoea) or `R06.83` (snoring) (as built).
- b. Also `G47.30` (sleep apnoea, unspecified) and `R06.81` (apnoea, not elsewhere classified).

**P10. An attended (in-lab) sleep study under Acme.** As built, the diagnosis and symptoms are
enough.
- a. As built.
- b. Also a reason a home sleep apnoea test is unsuitable, or a home test that was negative or inconclusive.

**P11. The payer lists.** As built, Beacon requires no prior authorization for an attended sleep
study, which is how the pack shows `not_required`.
- a. As built.
- b. Beacon requires it too (the pack then has no `not_required` example).

**P12. Verdict on prior authorization's payer lists and documentation requirements.**
- a. Acceptable as a synthetic reference pack.
- b. Acceptable with the changes answered above.
- c. Not acceptable (say why).

---

## The reviews

To be filled in by each reviewer, and recorded by the owner in gate `clinician-review`'s
attestation. A build session leaves them blank. The reviewers' names are not written here: the
gate keeps them in a note outside the repository.

### Referral router

| Field | Value |
|---|---|
| Reviewer's role (referral coordinator, cardiologist, physician, other) | |
| Date | |
| Pack and version reviewed | |
| Pack digest reviewed (`mendari validate packs/referral-router`) | |
| Answers to questions R1 to R11 | |
| Verdict (acceptable, acceptable with changes, not acceptable) | |

### Prior authorization preflight

| Field | Value |
|---|---|
| Reviewer's role (utilization-management reviewer, physician, other) | |
| Date | |
| Pack and version reviewed | |
| Pack digest reviewed (`mendari validate packs/prior-auth-preflight`) | |
| Answers to questions P1 to P12 | |
| Verdict (acceptable, acceptable with changes, not acceptable) | |
