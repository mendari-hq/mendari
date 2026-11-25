# Changelog: mendari-test/chart-summary

Synthetic test fixture. Not clinically reviewed. Not for patient care.

## 1.0.0

- A small agent that reads a patient's FHIR record, for the tests of record reading (wave 6,
  fixture F3): when the patient should next be seen (`follow_up`, `soon` never automated) and
  whether an anticoagulant is listed (`on_anticoagulant`), from a chart read with `[system=...]`
  predicates, `paths`, `follow`, `attachment_text`, code sets, a list of two resource types and
  `medication_period`. A `patient-view` service with search prefetch. Its data and records are
  made by `tests/support/fixture_records.py`.
