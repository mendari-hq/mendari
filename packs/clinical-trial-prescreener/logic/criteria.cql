// The GLYCEMIA-2 structured criteria in CQL: the same rules as criteria.yaml, for sites whose
// eligibility logic lives in CQL. The pack's `cql` provider variant evaluates it on a CQL engine
// with the patient's record as FHIR R4 (standards/record_fhir.py). CI checks that every answer
// equals the `criteria` variant's.
//
// Each definition answers a question with one of its labels: 'yes', 'no' or 'unknown'. Missing,
// stale or unrecognised data gives 'unknown', never a guess.
library GlycemiaTwoCriteria version '0.4.0'

using FHIR version '4.0.1'

include FHIRHelpers version '4.0.1' called FHIRHelpers

parameter "Screening Date" Date

context Patient

// -- helpers ---------------------------------------------------------------------------------

define function "Codes"(concept FHIR.CodeableConcept):
  concept.coding C return C.code.value

// A code ending in a prefix family (ICD-10 E11 covers E11.9) or equal to one of the exact codes.
define function "Matches"(concept FHIR.CodeableConcept, prefixes List<String>, exact List<String>):
  exists (("Codes"(concept)) X
    where exists (prefixes P where StartsWith(X, P)) or X in exact)

// The latest dated result with one of the codes, no later than the screening date and at most
// `window` days before it.
define function "Latest"(codes List<String>, window Integer):
  Last(
    [Observation] O
      let observed: date from (O.effective as FHIR.dateTime).value
      where exists (("Codes"(O.code)) X where X in codes)
        and observed is not null
        and observed <= "Screening Date"
        and days between observed and "Screening Date" <= window
      return Tuple {
        observed: observed,
        value: (O.value as FHIR.Quantity).value.value,
        unit: (O.value as FHIR.Quantity).unit.value
      }
      sort by observed
  )

define function "Named"(M FHIR.MedicationStatement, name String):
  PositionOf(Lower(name), Lower(Coalesce((M.medication as FHIR.CodeableConcept).text.value, ''))) >= 0

// An active medication with no end date counts as current only when it started in the 365 days
// before screening (criteria.yaml's `current_within_days: 365`): with no start date, or an
// earlier one, it is not known to be current.
define function "Not Known Current"(M FHIR.MedicationStatement):
  M.status.value = 'active'
    and (M.effective as FHIR.Period).end.value is null
    and (
      (M.effective as FHIR.Period).start.value is null
        or days between (date from (M.effective as FHIR.Period).start.value)
          and "Screening Date" > 365
    )

// -- age_18_75 -------------------------------------------------------------------------------

define "Age":
  if Patient.birthDate is null then null else AgeInYearsAt("Screening Date")

define "Age 18 To 75":
  if "Age" is null then 'unknown'
  else if "Age" between 18 and 75 then 'yes'
  else 'no'

// -- t2d_diagnosis ---------------------------------------------------------------------------

define "T2D Diagnosis":
  if exists ([Condition] C where "Matches"(C.code, {'E11'}, {'FC101'})) then 'yes'
  else if exists ([Condition] C where "Matches"(C.code, {'E10'}, {'FC102'})) then 'no'
  else 'unknown'

// -- hba1c_in_range: 7.0 to 10.5 % in the last 90 days (IFCC mmol/mol converted to NGSP %) -----

define "HbA1c":
  "Latest"({'4548-4'}, 90)

define "HbA1c Percent":
  case
    when "HbA1c".unit is null or "HbA1c".unit = '%' then "HbA1c".value
    when "HbA1c".unit = 'mmol/mol' then "HbA1c".value * 0.0915 + 2.15
    else null
  end

define "HbA1c In Range":
  if "HbA1c" is null or "HbA1c Percent" is null then 'unknown'
  else if "HbA1c Percent" between 7.0 and 10.5 then 'yes'
  else 'no'

// -- stable_metformin: on metformin for at least 90 days --------------------------------------

define "Metformin":
  First([MedicationStatement] M where M.status.value = 'active' and "Named"(M, 'metformin'))

define "Metformin Start":
  date from ("Metformin".effective as FHIR.Period).start.value

define "Stable Metformin":
  if "Metformin" is null then 'no'
  else if "Metformin Start" is null then 'unknown'
  else if "Not Known Current"("Metformin") then 'unknown'
  else if days between "Metformin Start" and "Screening Date" < 90 then 'no'
  else 'yes'

// -- egfr_30_or_more: eGFR of 30 or more in the last 365 days ---------------------------------

define "eGFR":
  "Latest"({'33914-3', '62238-1', '98979-8'}, 365)

define "eGFR 30 Or More":
  if "eGFR" is null or "eGFR".value is null then 'unknown'
  else if "eGFR".unit is not null and "eGFR".unit != 'mL/min/1.73m2' then 'unknown'
  else if "eGFR".value >= 30 then 'yes'
  else 'no'

// -- no_insulin: no insulin now or in the last 90 days ----------------------------------------

// The first insulin that is active or ended in the last 90 days: it decides, as in criteria.yaml.
define "Insulin In The Last 90 Days":
  First(
    [MedicationStatement] M
      let ended: date from (M.effective as FHIR.Period).end.value
      where "Named"(M, 'insulin')
        and (
          M.status.value = 'active'
            or (ended is not null and ended <= "Screening Date"
              and days between ended and "Screening Date" <= 90)
        )
  )

define "No Insulin":
  if "Insulin In The Last 90 Days" is null then 'yes'
  else if "Not Known Current"("Insulin In The Last 90 Days") then 'unknown'
  else 'no'

// -- no_recent_cv_event: no MI or stroke in the last 180 days ---------------------------------

define "CV Conditions":
  [Condition] C
    where "Matches"(C.code, {'I21', 'I22', 'I63'}, {'FC201', 'FC202', 'FC203'})

define "Recent CV Event":
  exists (
    "CV Conditions" C
      let onset: date from (C.onset as FHIR.dateTime).value
      where onset is not null
        and onset <= "Screening Date"
        and days between onset and "Screening Date" <= 180
  )

define "Undated CV Event":
  exists ("CV Conditions" C where (C.onset as FHIR.dateTime) is null)

define "No Recent CV Event":
  if "Recent CV Event" then 'no'
  else if "Undated CV Event" then 'unknown'
  else 'yes'
