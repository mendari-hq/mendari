# Changelog: openhealth/patient-inbox-router

## 0.1.0

- First release.
- Questions: `destination` (5 teams) and `urgency` (urgent / routine).
- Providers: deterministic red-flag screen (`rules`); naive Bayes router trained and
  temperature-calibrated on synthetic data (default); keyword router (`rules`) and Jev
  (`jev-1.13.0`) variants for both slots.
- Policy: stat nurse review for possible emergencies; AUTO at ≥0.90 confidence (≥0.95 for
  non-clinical destinations); abstain below 0.50.
