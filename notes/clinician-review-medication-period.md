Drafted by a build session for a clinician to review. Not clinically reviewed.
The owner answered it in chat on 2026-11-07 ([The review](#the-review)): acceptable with
changes, not built yet.

# When a MedicationRequest's drug is taken: a brief for the clinician reviewer

_Written 2026-11-06 (US Eastern) for LP-23k, the step that adopts the rule in a shipped pack.
For a pharmacist or a clinical research coordinator (launch prerequisite B21.4; launch gate
`clinician-review`). Nothing here had been reviewed by a clinician when it was written. The
review is recorded by the owner in the gate `clinician-review`'s attestation, never by a build
session._

## Why this needs a clinician

A clinical-trial prescreen asks questions such as "has the patient taken metformin for at least
90 days?" and "has the patient been off insulin for the last 90 days?". An EHR rarely says when a
patient started or stopped a drug. It says when a prescription was written, how it is to be
taken and when it may be dispensed. Turning those into "taken from" and "taken until" is a
clinical judgement, which Mendari's build plan recorded as such (`notes/lp23-plan.md`, conflict
39) and which a clinician should check before any real use.

## The rule, exactly as built

Spec 0.3 of Mendari's Decision Pack format, §2.7 (`docs/spec/decision-pack-0.3.md`), implemented
in `src/mendari/standards/fhir.py` (`medication_period`, `MEDICATION_START`, `MEDICATION_END`,
`listed`):

1. **Start**: the first dosage instruction's `timing.repeat.boundsPeriod.start`; if there is
   none, `authoredOn` (the day the prescription was written).
2. **Stop**: that same `boundsPeriod.end`; if there is none, the stop is **unknown** (left out).
   It is never inferred from the request's `status`, whether `completed` or `stopped`.
3. A request whose `status` is `entered-in-error` is left out of the medication list entirely.
4. `dispenseRequest.validityPeriod` is not read.
5. Only the first dosage instruction is read. Its `boundsDuration` and `boundsRange` (the other
   forms a bound may take) are not read.

The request's `status` is passed on unchanged beside the start and stop. The pack's criteria use
it: a medication counts as current only when its status is `active`, or when it ended within the
window a criterion looks at.

## Which pack adopts it, and where

- **`mendari/clinical-trial-prescreener` 0.3.0** (the GLYCEMIA-2 study, a synthetic study written
  for the reference pack, not a real protocol). Its FHIR mapping,
  `packs/clinical-trial-prescreener/fhir/mapping.yaml`, lists
  `lists.medications.resources.MedicationRequest.medication_period: {start: start, end: end}`,
  and its CDS Hooks prefetch reads `MedicationRequest?patient={{context.patientId}}`. Its card's
  known failure modes (`pack.yaml`, `card.known_failure_modes`) state the rule and that it is
  not yet clinically reviewed.
- The criteria that read the result are in `packs/clinical-trial-prescreener/logic/criteria.yaml`:
  - `stable_metformin`: "yes" when an active medication whose name contains "metformin" started
    at least 90 days before screening; "no" when it started less than 90 days ago, was stopped,
    or is not listed; "unknown" when it is active with no start date.
  - `no_insulin`: "no" when a medication whose name contains "insulin" is active, or ended in
    the last 90 days; otherwise "yes".
- The pack's result is not automated by default: it is at risk level 2, so every result goes to
  a research coordinator, who confirms eligibility against the protocol and the source documents.
  A recorded governance override (`core/policy.py`) may let a site automate a confident result
  other than `candidate`, `not_eligible` included, which this rule can decide (a renewal with no
  bounds reads as metformin started recently, so `stable_metformin` is "no"). `candidate` is
  never automated, override or not (the pack's `policy.yaml`: `allow_auto: false`).
- No other shipped pack reads medications.

## What each FHIR element means (FHIR R4)

| Element | What R4 says it is | How the rule uses it |
|---|---|---|
| `authoredOn` | When the request was initially authored (written or signed). | The start, when the dosage has no bounds. |
| `dosageInstruction[0].timing.repeat.boundsPeriod` | The period over which the dosage applies: when the patient is to start and stop taking it. | Its `start` is the start and its `end` the stop. |
| `dispenseRequest.validityPeriod` | The time period over which the prescription may be dispensed (filled), not when it is taken. | Not read. |
| `status` | The state of the order: active, on-hold, cancelled, completed, entered-in-error, stopped, draft, unknown. | Passed on as written; never turned into a stop date. `entered-in-error` is left out of the list. |
| `intent` | Whether this is a proposal, a plan, an order, an original order, a reflex order, a filler order or an instance order. | Not read. |
| `doNotPerform` | True when the request says the drug is **not** to be given. | Not read. |

## Why `validityPeriod` is not used

`dispenseRequest.validityPeriod` is the window in which a pharmacy may fill the prescription.
A patient may start taking the drug days or weeks after it opens, may keep taking it after it
closes (from earlier supply, or on a renewal), and may never fill it at all. Reading it as "taken
from ... until" would make the window of a pharmacy's permission look like the patient's
exposure. The plan's drafts considered it and set it aside (conflict 39).

## What happens on missing dates

| The request holds | Start | Stop | What the trial criteria then say |
|---|---|---|---|
| `boundsPeriod` with start and end | `boundsPeriod.start` | `boundsPeriod.end` | As the dates say. |
| `boundsPeriod.start` only | `boundsPeriod.start` | unknown | An active drug is current from its start. |
| `boundsPeriod.end` only | `authoredOn` | `boundsPeriod.end` | As the dates say. |
| No bounds, `authoredOn` given | `authoredOn` | unknown | An active metformin started on the day it was written. |
| No bounds, no `authoredOn` | unknown | unknown | An active metformin with no start: `stable_metformin` is "unknown" and the patient needs review. |
| Bounds on the second dosage instruction only | `authoredOn` | unknown | The second instruction's bounds are not read. |

## What happens on each status

| `status` | In the list | Stop date | What the trial criteria then say |
|---|---|---|---|
| `active` | yes | from the bounds only | Current. |
| `completed` | yes | from the bounds only, never from the status | Not current (the criteria count only `active` as current), unless it ended within a window a criterion looks at, which needs a bounds end. |
| `stopped` | yes | from the bounds only, never from the status | As `completed`. |
| `on-hold`, `cancelled`, `draft`, `unknown` | yes | from the bounds only | Not current. |
| `entered-in-error` | **no** | none | As if the request did not exist. |

So a `completed` or `stopped` insulin with no bounds end is not current, and its stop is
unknown: `no_insulin` does not see that it ended in the last 90 days, and reads "yes" (off
insulin). A request with a bounds end in the last 90 days is seen.

## Situations the reviewer should know about

These follow from the rule as built. Each is a reason the review matters; none is settled here.

- **Renewals.** An EHR often writes a new MedicationRequest for each renewal. A renewal with no
  bounds starts on the day it was renewed, so a patient on metformin for years can read as having
  started it last month (`stable_metformin` "no", the prescreen `not_eligible`).
- **Several requests for one drug.** Each request is its own item. The criteria take the first
  matching item in the record's order, which may not be the earliest.
- **Intent and `doNotPerform`.** A plan, a proposal, or a request saying not to give the drug, if
  its status is `active`, reads as a drug being taken.
- **Status without dates.** A `completed` or `stopped` request without a bounds end gives no stop
  date, so "ended within the last 90 days" cannot be seen from it.
- **Free-text sigs.** A dosage given only as text ("take as directed until review") has no bounds.
- **Statements and requests together.** A record may hold a MedicationStatement and a
  MedicationRequest for the same drug. Both are listed; the statement's own `effectivePeriod` is
  read as before.

## The alternatives the plan's drafts considered (conflict 39)

1. `dispenseRequest.validityPeriod` as the period taken. Rejected: it is the dispensing window.
2. A stop inferred from the status (`completed` or `stopped` meaning "stopped by now", or on the
   day the status changed). Rejected: R4 records no date for a status change, and a status does
   not say when the drug was last taken.
3. `authoredOn` alone as the start. Rejected in favour of the dosage bounds first, which say when
   the patient is to take the drug; `authoredOn` is the fallback.
4. The adopted default: bounds first, `authoredOn` for the start, an unknown stop, no inference
   from the status, entered in error dropped, stated in the spec and the trial card, and this
   clinician review still owed.

## Questions for the reviewer

1. Is the first dosage instruction's `boundsPeriod.start`, else `authoredOn`, a fair start for
   "has taken metformin for at least 90 days" in a prescreen a coordinator then checks?
2. Should a request with no bounds, whose stop is unknown, count as current when its status is
   `active`?
3. Should `completed` or `stopped` ever give a stop date (for example the request's last update),
   or is "unknown" right? And while the stop is unknown, should a listed insulin or metformin
   whose status is not `active` read as "unknown" (cannot tell from the list) rather than as not
   taken, as it does now (`no_insulin` "yes" for a `completed` insulin written 20 days ago; the
   card states this)?
4. Should renewals be joined, for example by taking the earliest start among requests for the
   same drug?
5. Should a request whose `intent` is `plan` or `proposal`, or whose `doNotPerform` is true, be
   left out?
6. Should other dosage instructions, or `boundsDuration`, be read?
7. Is anything about MedicationStatement (its `effectivePeriod`, read as before) also wrong?
8. Is the rule acceptable for real use in this pack, acceptable with changes (which), or not
   acceptable?

## The review

Filled in by a build session from the answers the owner, the reviewer, gave in chat on
2026-11-07 by picking options. It is recorded by the owner in gate `clinician-review`'s
attestation once the changes are built and the owner confirms them; until then the gate stays
unattested. The reviewer's name is not written here: the gate keeps it in a note outside the
repository.

| Field | Value |
|---|---|
| Reviewer's role (pharmacist, research coordinator, other) | physician (the owner; also the pack's recorded author, disclosed) |
| Date | 2026-11-07 |
| Pack and version reviewed | mendari/clinical-trial-prescreener 0.3.0 (the rule as built in #282) |
| Pack digest reviewed (`mendari validate packs/clinical-trial-prescreener`) | recorded after the changes below merge (they change the digest) |
| Answers to the questions above | Q1: fair as built (the first dosage line's `boundsPeriod.start`, else `authoredOn`).<br>Q2: an `active` request with no end is current only if written or started in the last 12 months; an older one reads unknown and goes to review (the owner: "Current if ≤ 12 months , older reads old than 12 months need review").<br>Q3: not taken, as built; no stop is inferred from the status.<br>Q4: renewals stay separate, as built.<br>Q5: leave out both: intent `proposal`, `plan` and `option`, and `doNotPerform` true (`order`, `original-order`, `reflex-order`, `filler-order` and `instance-order` stay).<br>Q6: also read `boundsDuration` (the start plus the duration is the stop, when there is a start); still the first dosage line only.<br>Q7: the same 12-month rule for an `active` MedicationStatement with no end, started more than 12 months ago (unknown, review); nothing else changes.<br>Q8: acceptable with changes (below). |
| Verdict (acceptable, acceptable with changes, not acceptable) | acceptable with changes: Q2's and Q7's 12-month rule, Q5 and Q6, once built and seen by the owner |
