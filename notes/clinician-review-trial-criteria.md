Drafted by a build session for a clinician to review. Not clinically reviewed.

# GLYCEMIA-2's eligibility criteria: a brief for the clinician reviewer

_Written 2026-11-07 (US Eastern). For a clinical research coordinator (launch prerequisite B21.4;
launch gate `clinician-review`). On 2026-11-07 the owner, a licensed physician who wrote the five
reference packs, decided that their own disclosed physician review counts for these five packs.
Nothing here has been reviewed by a clinician. The review is recorded by the owner in the gate
`clinician-review`'s attestation, never by a build session._

## What this brief covers, and the pack it describes

| | |
|---|---|
| Pack | `mendari/clinical-trial-prescreener` |
| Version | 0.3.0 |
| Digest | `sha256:1d0e003753fc10fa7a8d128f7a3f3bf17c89ea8ac49e3366a1302145847d82dc` |
| Read with | `uv run mendari validate packs/clinical-trial-prescreener`, on `main` at `af26a868` |

Covered: every eligibility criterion of the synthetic GLYCEMIA-2 study, the exclusions read
from the notes, how they combine into the prescreen result, and the safety flags. **Not
covered:** when a MedicationRequest's drug counts as taken (its start and stop dates), which
has its own brief, [`clinician-review-medication-period.md`](clinician-review-medication-period.md).
The metformin and insulin criteria below are covered only for their thresholds and how a drug
is recognised.

Work in progress on 2026-11-07 changes this pack's medication rule. If any pull request
changes the pack after this brief, its version and digest change: re-read the quoted lines and
refresh this brief before the review is recorded.

## What the pack does, and who sees its result

When a chart is opened (CDS Hooks `patient-view`) or in a batch run, the pack checks the record
against GLYCEMIA-2's criteria and suggests one of three results (`pack.yaml:250-291`):
`candidate`, `needs_review` or `not_eligible`. GLYCEMIA-2 is a synthetic add-on therapy study
in type 2 diabetes on stable metformin, written for this pack; it is not a real protocol
(`pack.yaml:44-48`, `fhir/research-study.json:11`).

- **Who sees it:** a research coordinator. The pack is at risk level 2 (`pack.yaml:31-39`), so
  every result goes to a person; the coordinator confirms eligibility against the protocol and
  the source documents before any contact (`pack.yaml:57-58`). A FHIR Task is written to the
  research coordinator's prescreening queue (`fhir/mapping.yaml:98-102`).
- **The EHR card** shows only `candidate` and `needs_review` (`pack.yaml:419`); a
  `not_eligible` chart stays quiet.
- **Automation:** `candidate` is never automated, even under a site's governance override
  (`policy.yaml:15-16`). Under a recorded override, a confident `not_eligible` or
  `needs_review` may be (`policy.yaml:1-5`).
- **Structured criteria** are computed in code from coded data (`logic/criteria.yaml`); the
  **note exclusions** are read from clinic notes by keyword rules offline, or by a model in a
  deployment (`pack.yaml:322-369`).

## The structured criteria, exactly as built

"yes" means the criterion is met. Each check is tried in order; the first that applies decides;
missing or stale data gives `unknown` (`logic/criteria.yaml:1-9`).

| Criterion | yes | no | unknown | Where |
|---|---|---|---|---|
| Age 18 to 75 | Aged 18 to 75 inclusive on the screening date (age from `Patient.birthDate`) | Younger than 18 or older than 75 | Age not recorded | `pack.yaml:149-155`, `criteria.yaml:13-15` |
| Type 2 diabetes | ICD-10-CM `E11*` coded (or the local example's fictional `FC101`) | `E10*` (type 1) coded and no `E11*` | No diabetes code | `pack.yaml:156-162`, `criteria.yaml:17-20` |
| HbA1c 7.0 to 10.5 % | The latest HbA1c (LOINC `4548-4`) in the 90 days before screening is 7.0 to 10.5 % inclusive | Below 7.0 % or above 10.5 % | None in 90 days, or an unrecognised code or unit | `pack.yaml:163-170`, `criteria.yaml:22-31` |
| Metformin for at least 90 days | An active medication whose name contains "metformin", started at least 90 days ago | Not listed, stopped, or started less than 90 days ago | Active, with no start date | `pack.yaml:171-177`, `criteria.yaml:33-39` |
| eGFR 30 or more | The latest eGFR in the 365 days before screening is at least 30 mL/min/1.73m2 | Below 30 | None in 365 days, or an unrecognised unit | `pack.yaml:178-185`, `criteria.yaml:41-47` |
| No insulin in 90 days | No medication whose name contains "insulin" is active or ended in the last 90 days | Insulin active, or ended in the last 90 days | (The criteria file never gives it) | `pack.yaml:186-192`, `criteria.yaml:49-55` |
| No heart attack or stroke in 180 days | No `I21*`, `I22*` or `I63*` (or fictional `FC201`-`FC203`) with onset in the last 180 days | One coded with onset in the last 180 days | One coded without an onset date | `pack.yaml:193-201`, `criteria.yaml:57-65` |

Details that follow from the code (`src/mendari/providers/criteria.py`, `_condition`,
`_observation`, `_medication`):

- HbA1c in mmol/mol (IFCC) is converted to % (NGSP) by `% = 0.0915 x mmol/mol + 2.15`
  (`criteria.yaml:31`).
- eGFR counts under three LOINC codes: `33914-3` (MDRD), `62238-1` (CKD-EPI 2009) and
  `98979-8` (CKD-EPI 2021) (`criteria.yaml:44`).
- A condition counts **whatever its status**: the condition checks set no status filter, so a
  resolved or inactive `E11` reads as type 2 diabetes. The FHIR mapping reads
  `clinicalStatus` (`fhir/mapping.yaml:41`) and not `verificationStatus`, so a refuted or
  entered-in-error diagnosis counts too.
- A record with both `E10*` and `E11*` reads as type 2 diabetes ("yes"): the `E11` check comes
  first.
- Drugs are matched by name: a name containing "metformin" or "insulin". A brand name that
  does not contain the word (for example Glucophage, or an insulin brand name alone) is not
  matched (`pack.yaml:72-73`).

## The exclusions read from the notes, exactly as built

"yes" means the exclusion is documented (`pack.yaml:203-247`). Each is one literal question.

| Exclusion | Counts ("yes") | Does not count ("no") | Where |
|---|---|---|---|
| Pregnant or breastfeeding | Pregnant now (a confirmed pregnancy, weeks of gestation, or an expected delivery) or breastfeeding now | No mention, a negative pregnancy test, a past pregnancy, plans to become pregnant, menopause, or someone else's pregnancy | `pack.yaml:205-215` |
| Pancreatitis ever | Acute or chronic pancreatitis, at any time | No mention, denied or ruled out, or in a family member | `pack.yaml:216-223` |
| Cancer treatment now | Chemotherapy, radiation, immunotherapy, targeted or hormone therapy for cancer is ongoing, or has started and not finished | No mention, remission, treatment finished, surveillance only, a cancer that needed no further treatment, or in a family member | `pack.yaml:224-234` |
| Heart attack, stroke or ACS in 6 months | A heart attack (NSTEMI or STEMI included), a stroke or an acute coronary syndrome in the 6 months before screening | No mention, an event more than 6 months before, symptoms found not to be one, or in a family member | `pack.yaml:235-247` |

The offline keyword rules for these (`logic/note_rules.yaml`) are English only; on the
held-out hand-written notes they found 8 of 12 exclusions (`pack.yaml:111-120`). A deployment
reads the notes with a model (`pack.yaml:121-126`).

## How the result is formed, and the safety flags

| Step | Rule | Where |
|---|---|---|
| 1 | Any structured criterion "no" gives `not_eligible` | `pack.yaml:273-282` |
| 2 | Any note exclusion "yes" gives `not_eligible` | `pack.yaml:283-289` |
| 3 | Otherwise, any `unknown` gives `needs_review` | `pack.yaml:290` |
| 4 | Otherwise `candidate` | `pack.yaml:291` |
| Flag | A possible pregnancy (P >= 0.3) goes to a person with "Confirm before any study contact" | `policy.yaml:21-29` |
| Flag | A possible heart attack, stroke or ACS in 6 months (P >= 0.3) goes to a person with "Confirm the date in the chart" | `policy.yaml:30-38` |
| Threshold | Below 0.40 confidence there is no suggestion: the coordinator screens the chart as usual | `policy.yaml:9-11` |

The bundled FHIR ResearchStudy lists the same criteria in plain words
(`fhir/research-study.json:20-29`).

## Found while writing this brief (not a clinical question)

The `patient-view` hook asks the EHR only for Observations coded `4548-4`, `33914-3` and
`62238-1` (`pack.yaml:407`), and the hook reads only what its prefetch query asks for
(`src/mendari/standards/cds_hooks.py:4-6`). An eGFR coded only `98979-8` (CKD-EPI 2021), which
the criteria accept, is therefore not read on the hook, and the eGFR criterion reads `unknown`
(the patient goes to `needs_review`, not to a wrong answer). Fixing it is a pack change, for a
build session.

## Questions for the reviewer

Pick one option per question (any answer may instead be written in). The current behaviour is
marked "(as built)". Where a value looks clinically questionable, the note says why; nothing
here is a recommendation.

**T1. A record with both a type 1 (`E10*`) and a type 2 (`E11*`) code.**
- a. Type 2 diabetes "yes" (as built).
- b. "unknown": the patient goes to `needs_review`.
- c. "no": the patient is `not_eligible`.

**T2. A record with no diabetes code at all.**
- a. "unknown": `needs_review` (as built).
- b. "no": `not_eligible`, with no card.

**T3. A condition's status.** As built, any status counts, and a refuted or entered-in-error
diagnosis counts too.
- a. Any status counts (as built).
- b. Leave out refuted and entered-in-error diagnoses (`verificationStatus`); any clinical status counts.
- c. As b, and for type 2 diabetes count only an active, recurrent or relapsed condition (heart attack and stroke stay judged by onset date).

**T4. HbA1c range and window.**
- a. 7.0 to 10.5 %, latest in 90 days (as built).
- b. 7.5 to 10.5 %, latest in 90 days.
- c. 7.0 to 10.0 %, latest in 90 days.

**T5. eGFR threshold and window.**
- a. 30 or more, latest in 365 days (as built).
- b. 45 or more, latest in 365 days.
- c. 30 or more, latest in 90 days.

**T6. eGFR codes.** As built: MDRD `33914-3`, CKD-EPI 2009 `62238-1`, CKD-EPI 2021 `98979-8`.
- a. These three (as built).
- b. Also the race-specific MDRD codes some lab systems still send (`48642-3`, `48643-1`).

**T7. Heart attack and stroke codes.** As built: `I21*` (acute MI), `I22*` (subsequent MI),
`I63*` (cerebral infarction). The notes' exclusion also names acute coronary syndrome, which
these codes do not fully cover (unstable angina is `I20.0`).
- a. `I21*`, `I22*`, `I63*` (as built).
- b. Also `I20.0` (unstable angina) and `I24*` (other acute ischaemic heart disease), to match the notes' acute coronary syndrome.
- c. As b, and also haemorrhagic stroke (`I60*` to `I62*`) and TIA (`G45*`).

**T8. Other glucose-lowering drugs.** As built, insulin in the last 90 days is the only drug
exclusion.
- a. Insulin only (as built).
- b. Insulin, and a GLP-1 receptor agonist in the last 90 days.
- c. Insulin, and any other glucose-lowering class in the last 90 days (GLP-1 receptor agonist, SGLT2 inhibitor, DPP-4 inhibitor, sulfonylurea, thiazolidinedione).

**T9. How a drug is recognised.** As built, by name ("metformin", "insulin"); a brand name
alone is missed, which for metformin makes the patient wrongly `not_eligible` and for insulin
makes the patient read as off insulin.
- a. By name, a site adding its own names (as built).
- b. By name, with common brand names added to the pack's lists.
- c. By RxNorm ingredient code, beside the name.

**T10. Planning a pregnancy.** As built, "plans to become pregnant" is not an exclusion.
- a. Not an exclusion (as built).
- b. An exclusion.
- c. Flagged for the coordinator's review, not an exclusion.

**T11. Cancer.** As built, only cancer treatment now excludes.
- a. Treatment now only (as built).
- b. Any cancer diagnosed or treated in the last 5 years, except non-melanoma skin cancer.

**T12. A further note exclusion.** (The study drug is not named; pancreatitis is already an
exclusion.)
- a. None: the four as built.
- b. A personal or family history of medullary thyroid carcinoma or MEN2.
- c. A history of diabetic ketoacidosis.

**T13. A `not_eligible` chart.** As built, it shows no card, so a wrong `not_eligible` is
silent to the clinician who opens the chart (the coordinator's queue still has it).
- a. No card (as built).
- b. A card for `not_eligible` too.

**T14. Automation under a site's override.** As built, `candidate` is never automated; a
confident `not_eligible` or `needs_review` may be, under a recorded governance override.
- a. As built.
- b. Never automate any result of this pack, override or not.

**T15. Verdict on the criteria as a whole** (the medication rule apart).
- a. Acceptable as a synthetic reference study.
- b. Acceptable with the changes answered above.
- c. Not acceptable (say why).

## The review

To be filled in by the reviewer, and recorded by the owner in gate `clinician-review`'s
attestation. A build session leaves it blank. The reviewer's name is not written here: the gate
keeps it in a note outside the repository.

| Field | Value |
|---|---|
| Reviewer's role (research coordinator, physician, other) | |
| Date | |
| Pack and version reviewed | |
| Pack digest reviewed (`mendari validate packs/clinical-trial-prescreener`) | |
| Answers to the questions above | |
| Verdict (acceptable, acceptable with changes, not acceptable) | |
